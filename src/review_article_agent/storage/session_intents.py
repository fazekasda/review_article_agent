"""Durable, immutable **pre-transport session-turn intent correlation**.

Authority
---------

* ``docs/implementation/04-agent-runtime.md`` deliverable 2 (role configuration,
  bounded task submission, **admission/result reconciliation**, and captured
  model/usage metadata) and acceptance criterion 4 ("Lost event streams reconcile
  from durable state/messages. Missing events do not lose completion or approval
  actions") and 7 (project/session/model isolation).
* ``docs/contracts/02-gates-and-runtime.md`` §6.1 (``VersionSet``: no fabricated
  version; a consumed slot MUST be ``known``), §8.1 (an ``AdmissionReceipt`` is
  **not** completion; admission ≠ completion), and §8 (unknown retained; fail
  closed).

What this module **is**
-----------------------

The storage half of "persist the correlation **before** transport". Given the
deterministic, already-validated
:class:`~review_article_agent.runtime.session_turns.SessionTurnSpec` produced by
the planner for an **already reserved** ``opencode_session`` dispatch, it records
**one immutable row** (schema migration 10) holding the *exact* spec canonical
bytes, the *exact* message bytes, their recomputed SHA-256 digests, the exact
session/dispatch/message/admission pins, the fence, and the time — so a restart
can reconstruct the spec from persisted bytes **without** a live service or
manager and without a fresh planning pass.

The untrusted spec is **never trusted on its own bytes**. It is revalidated
through the spec's own strict validator and then every projection is independently
bound to the **persisted parents** (the dispatch, the bounded task, the session
mapping, and their immutable configuration snapshots): route/state/task/
fingerprint/digests, role/model/prompt/limits/scope/target/inputs/config, the
consumed ``VersionSet`` (``route``/``input_digest``/canonical hash/known slots),
the message's own projections, and the derived ``admission_id``. A forged but
internally self-consistent spec (a relabelled role/scope/ref/model/version/message
edit) is therefore refused even when its own bytes validate.

A **new** intent is recorded only for a persisted ``opencode_session`` dispatch
in the ``reserved`` state, under the dispatch's exact fence, after the target and
every input are revalidated for existence, **actual byte content**, pinned digest,
currentness, and current storage-allowed rights through the existing
:class:`~review_article_agent.storage.agent_runtime.RuntimeRepository` helper (no
caller-supplied hash is trusted). An **identical** replay (same dispatch, same
spec bytes, same fence) returns the validated historical row — even after the
dispatch has been submitted and the task's input heads have moved — while a
**divergent** replay is refused with :class:`SessionIntentConflictError`.

A **historical read** revalidates the immutable actual pinned bytes/rights of the
recorded target/inputs (never their currentness, and never the existence of the
old filesystem/service layout), so the correlation survives a moved head. It
never calls :func:`~review_article_agent.runtime.session_turns.prepare_session_turn`,
which needs a live manager and a still-``reserved`` dispatch.

What this module **is not** (the boundary)
------------------------------------------

* It performs **no** transport, network, model, or provider call, creates **no**
  session, retrieves **no** provider API key or credential value, and queries
  **no** service or manager.
* It **grants no authority**: the row is inert correlation metadata. It neither
  admits nor completes a dispatch, and it never marks a dispatch
  submitted/admitted, never accepts a result, and never changes a cost.
* ``message`` is the exact application session-turn input, which contains the
  **private persisted source text** by design. This module makes **no**
  secret-scanner claim and **no** "contains no source" guarantee; the bytes are
  bounded, private local metadata and are never logged or echoed in an error.

Every failure is a static, value-free :class:`SessionIntentError`: a message never
echoes caller text, source bytes, a credential, an id, a ref, a model, or a
digest, and a SQL/parse failure is reported without its underlying text.
"""

from __future__ import annotations

import dataclasses
import functools
import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from ..domain.errors import StoreError
from ..domain.records import canonical_json, validate_identifier
from ..runtime.dispatch import (
    DispatchCorruptionError,
    DispatchRecord,
    RuntimeDispatchRepository,
)
from ..runtime.roles import AgentRoleError, BoundedTask
from ..runtime.session_turns import (
    MAX_SESSION_TURN_SPEC_BYTES,
    SessionTurnSpec,
    SessionTurnValidationError,
)
from .agent_runtime import (
    PureJsonVersionSet,
    RuntimeCorruptionError,
    RuntimeRepository,
    RuntimeSessionMapping,
    RuntimeTaskRecord,
    RuntimeValidationError,
)

__all__ = [
    "SESSION_INTENT_SCHEMA_VERSION",
    "SESSION_INTENT_ROUTE",
    "MAX_SESSION_INTENT_BYTES",
    "SessionIntentError",
    "SessionIntentValidationError",
    "SessionIntentConflictError",
    "SessionIntentStateError",
    "SessionIntentFenceError",
    "SessionIntentCorruptionError",
    "SessionIntentRecord",
    "SessionIntentRepository",
    "session_intent_integrity_problems",
]

# --------------------------------------------------------------------------- #
# Vocabulary / bounds
# --------------------------------------------------------------------------- #
#: Version of the *recorded intent representation*. Independent of the store
#: schema version and of the ``SessionTurnSpec`` it embeds.
SESSION_INTENT_SCHEMA_VERSION = 1

#: The only route a recorded session-turn intent may describe (an
#: ``AdmissionReceipt`` exists only on the durable OpenCode route, C02 §8.1).
SESSION_INTENT_ROUTE = "opencode_session"

#: The version slots a session turn consumes (C02 §6.1): the role model and the
#: prompt. Both MUST be ``known``; an unknown applied version is refused.
SESSION_INTENT_CONSUMED_SLOTS = ("model", "prompt")

#: Upper bound on either stored canonical blob. The spec is already bounded by
#: :data:`~review_article_agent.runtime.session_turns.MAX_SESSION_TURN_SPEC_BYTES`
#: and the message by the task's ``size_limit`` (itself <= ``MAX_TASK_SIZE_BYTES``).
MAX_SESSION_INTENT_BYTES = MAX_SESSION_TURN_SPEC_BYTES

_MAX_IDENTIFIER_CHARS = 512

_HEX_RE = re.compile(r"^[0-9a-f]{64}$")
_UTC_TIMESTAMP_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|\+00:00)$"
)

#: The exact message keys the planner writes (a forged/extra key is refused).
_MESSAGE_KEYS = frozenset(
    {
        "kind",
        "project_id",
        "role",
        "task_id",
        "task_fingerprint",
        "dispatch_id",
        "mapping_id",
        "session_id",
        "scope",
        "target",
        "input_refs",
        "prompt_version",
        "size_limit",
        "step_limit",
        "versions",
        "text",
    }
)

#: The exact dispatch-plan keys a session-turn spec pins.
_DISPATCH_PLAN_KEYS = frozenset(
    {
        "dispatch_id",
        "request_id",
        "route",
        "stage",
        "input_digest",
        "version_digest",
    }
)

#: The exact session-plan keys a session-turn spec pins.
_SESSION_PLAN_KEYS = frozenset(
    {
        "mapping_id",
        "session_id",
        "service_instance_id",
        "clean_location",
        "opencode_project_id",
        "config_snapshot_id",
        "config_digest",
        "model",
    }
)

# --------------------------------------------------------------------------- #
# Errors (static, curated, non-reflective)
# --------------------------------------------------------------------------- #
class SessionIntentError(StoreError):
    """Base class for every session-turn intent failure.

    Every message is **static and curated**: it never echoes caller text, source
    bytes, a credential, an id, a ref, a model, or a digest, and never embeds a
    SQL/parse error string.
    """


class SessionIntentValidationError(SessionIntentError, ValueError):
    """The spec is malformed, or a projection does not match its persisted parent."""


class SessionIntentConflictError(SessionIntentError):
    """A divergent replay for the same dispatch's recorded intent."""


class SessionIntentStateError(SessionIntentError):
    """A new intent was requested for a dispatch that is not reserved."""


class SessionIntentFenceError(SessionIntentStateError):
    """The supplied fence is not the dispatch's exact current fence."""


class SessionIntentCorruptionError(SessionIntentError):
    """A stored intent row fails structural/hash/column/parent revalidation on read."""


# --------------------------------------------------------------------------- #
# Static messages (never echo a value)
# --------------------------------------------------------------------------- #
_M_BAD_SPEC = "the supplied session-turn spec is not a valid, strict, canonical plan"
_M_BAD_PROJECT = "the session-turn intent project identity is not valid"
_M_BAD_ROW = "the stored session-turn intent row is malformed"
_M_BAD_CANONICAL = "the stored session-turn intent canonical bytes are malformed"
_M_CANONICAL_SIZE = "the stored session-turn intent canonical bytes exceed the permitted size"
_M_MESSAGE = "the stored session-turn intent message bytes are malformed"
_M_MISSING_DISPATCH = "the session-turn intent names a dispatch that is not persisted here"
_M_MISSING_TASK = "the session-turn intent names a task that is not persisted here"
_M_MISSING_MAPPING = "the session-turn intent names a session mapping that is not persisted here"
_M_FOREIGN = "the session-turn intent names a parent that belongs to another project"
_M_PARENT = "the session-turn intent does not match its persisted parent"
_M_PARENT_DISPATCH = "the session-turn intent does not match the dispatch it pins"
_M_PARENT_TASK = "the session-turn intent does not match the task the dispatch pins"
_M_PARENT_MAPPING = "the session-turn intent does not match its persisted session mapping"
_M_PARENT_CONFIG = "the session-turn intent configuration does not match the persisted snapshots"
_M_ROUTE = "only an opencode_session dispatch may carry a session-turn intent"
_M_STATE = "a new session-turn intent may be recorded only for a reserved dispatch"
_M_FENCE = "the supplied fence is not the dispatch's exact current fence"
_M_FENCE_ROW = "the recorded fence is not the dispatch's exact current fence"
_M_DIVERGENT = "a different session-turn intent is already recorded for this dispatch"
_M_VERSIONS = "the session-turn intent version set is not a valid consumed VersionSet"
_M_VERSION_ROUTE = "the session-turn intent version set must use the opencode_session route"
_M_VERSION_INPUT = "the version set input digest does not match the dispatch input digest"
_M_VERSION_HASH = "the canonical version-set hash does not match the dispatch version digest"
_M_VERSION_MODEL = "the version set model version does not match the recorded session model"
_M_VERSION_PROMPT = "the version set prompt version does not match the task prompt version"
_M_ADMISSION = "the recorded admission id is not the deterministic dispatch admission id"
_M_DIGEST = "a session-turn intent digest is not a lowercase hex SHA-256 digest"
_M_TIMESTAMP = "a session-turn intent timestamp is not an RFC 3339 UTC instant"
_M_COLUMNS = "a projected session-turn intent column disagrees with the canonical bytes"
_M_LIVE_REFS = "the task target or an input is missing, stale, or its bytes/rights are not valid now"
_M_DIGEST_MISMATCH = "a stored session-turn intent digest does not match its bytes"


# --------------------------------------------------------------------------- #
# Small helpers (bounded, exact, never defaulting)
# --------------------------------------------------------------------------- #
def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _bare_hex(value: object) -> str:
    if not isinstance(value, str):
        raise SessionIntentValidationError(_M_DIGEST)
    text = value[7:] if value.startswith("sha256:") else value
    if _HEX_RE.fullmatch(text) is None:
        raise SessionIntentValidationError(_M_DIGEST)
    return text


def _intent_id(project_id: str, dispatch_id: str) -> str:
    material = canonical_json([project_id, dispatch_id])
    return "sint_" + hashlib.sha256(material).hexdigest()[:32]


def _admission_id(project_id: str, dispatch_id: str) -> str:
    """Re-derive the deterministic client admission id (project + dispatch)."""
    digest = hashlib.sha256((project_id + "\x00" + dispatch_id).encode("utf-8")).hexdigest()
    return "msg_" + digest[:32]


def _identifier(value: object) -> str:
    try:
        normalized = validate_identifier(value)
    except Exception as exc:  # RecordValidationError / TypeError
        raise SessionIntentValidationError(_M_BAD_PROJECT) from exc
    if len(normalized) > _MAX_IDENTIFIER_CHARS:
        raise SessionIntentValidationError(_M_BAD_PROJECT)
    return normalized


def _plain_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _require_utc_timestamp(value: object) -> str:
    if not isinstance(value, str) or _UTC_TIMESTAMP_RE.fullmatch(value) is None:
        raise SessionIntentValidationError(_M_TIMESTAMP)
    candidate = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise SessionIntentValidationError(_M_TIMESTAMP) from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise SessionIntentValidationError(_M_TIMESTAMP)
    return value


def _bounded_json_bytes(
    data: object, *, too_large: str, malformed: str
) -> Any:
    """Parse bounded, portable JSON bytes; refuse an overlong/hostile document.

    The length is bounded **before** the parse, ``json.loads`` recursion is caught,
    and the parsed value is re-validated through :func:`canonical_json` (which
    enforces the depth/cycle/portability bounds) **before** any recursive walk.
    """
    if not isinstance(data, (bytes, bytearray)):
        raise SessionIntentValidationError(malformed)
    raw = bytes(data)
    if len(raw) == 0 or len(raw) > MAX_SESSION_INTENT_BYTES:
        raise SessionIntentValidationError(too_large)
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise SessionIntentValidationError(malformed) from exc
    try:
        canonical_json(parsed)
    except Exception as exc:  # RecordValidationError / ValueError / TypeError
        raise SessionIntentValidationError(malformed) from exc
    return parsed


def _fail_closed_row(what: str):
    """Decorate a stored-row validator so a malformed row fails closed.

    A stored row is untrusted input. Any helper that rejects it must surface as
    the single typed :class:`SessionIntentCorruptionError`. The message is static:
    it never echoes the offending value nor a SQL/parse error string.
    """

    def decorate(func):
        @functools.wraps(func)
        def wrapper(self, *args, **kwargs):
            try:
                return func(self, *args, **kwargs)
            except (SessionIntentCorruptionError, SessionIntentConflictError):
                raise
            except (
                SessionIntentError,
                SessionTurnValidationError,
                AgentRoleError,
                RuntimeValidationError,
                RuntimeCorruptionError,
                DispatchCorruptionError,
                ValueError,
                TypeError,
                KeyError,
                IndexError,
                UnicodeError,
                RecursionError,
                sqlite3.Error,
            ) as exc:
                raise SessionIntentCorruptionError(f"{what}: {_M_BAD_ROW}") from exc

        return wrapper

    return decorate


# --------------------------------------------------------------------------- #
# Value object
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class SessionIntentRecord:
    """One immutable, validated recorded session-turn correlation (as stored).

    ``canonical`` is the exact canonical plan bytes and ``message`` the exact
    canonical application message bytes stored in the row; ``spec_digest`` and
    ``message_digest`` are their recomputed SHA-256 values. ``created_at_utc`` is a
    row column only and is deliberately **not** part of the canonical bytes, so an
    identical replay is byte-identical regardless of when it was first recorded.

    Every exposed field is an immutable scalar or ``bytes``, and
    :meth:`spec`/`to_mapping` return fresh copies, so a returned record exposes no
    mutable alias.
    """

    project_id: str
    dispatch_id: str
    intent_id: str
    schema_version: int
    fence: int
    admission_id: str
    session_id: str
    mapping_id: str
    service_instance_id: str
    task_id: str
    task_fingerprint: str
    role: str
    model: str
    prompt_version: str
    input_digest: str
    version_digest: str
    config_snapshot_id: str
    config_digest: str
    spec_digest: str
    message_digest: str
    message_bytes: int
    created_at_utc: str
    canonical: bytes
    message: bytes

    def spec(self) -> SessionTurnSpec:
        """Reconstruct the exact :class:`SessionTurnSpec` from the persisted bytes.

        Pure: it reads no service, manager, or filesystem — only the stored
        canonical plan bytes and message bytes, re-run through the spec's strict
        validator.
        """
        return _spec_from_bytes(self.canonical, self.message)

    def to_mapping(self) -> dict[str, Any]:
        """A fresh, plain JSON-safe copy of the recorded plan (never an alias)."""
        return json.loads(self.canonical.decode("utf-8"))

    def message_mapping(self) -> dict[str, Any]:
        """A fresh, plain JSON-safe copy of the recorded message (never an alias)."""
        return json.loads(self.message.decode("utf-8"))


def _spec_from_bytes(canonical: bytes, message: bytes) -> SessionTurnSpec:
    """Rebuild the strict spec from persisted bytes (raises on any corruption)."""
    plan = _bounded_json_bytes(canonical, too_large=_M_CANONICAL_SIZE, malformed=_M_BAD_CANONICAL)
    if not isinstance(plan, dict):
        raise SessionIntentValidationError(_M_BAD_CANONICAL)
    return SessionTurnSpec(
        project_id=plan["project_id"],
        role=plan["role"],
        task_id=plan["task"]["task_id"],
        task_fingerprint=plan["task"]["fingerprint"],
        dispatch_id=plan["dispatch"]["dispatch_id"],
        dispatch_request_id=plan["dispatch"]["request_id"],
        mapping_id=plan["session"]["mapping_id"],
        session_id=plan["session"]["session_id"],
        service_instance_id=plan["session"]["service_instance_id"],
        clean_location=plan["session"]["clean_location"],
        config_snapshot_id=plan["session"]["config_snapshot_id"],
        config_digest=plan["session"]["config_digest"],
        model=plan["session"]["model"],
        admission_id=plan["admission_id"],
        input_digest=plan["dispatch"]["input_digest"],
        version_digest=plan["dispatch"]["version_digest"],
        prompt_version=plan["task"]["prompt_version"],
        step_limit=plan["task"]["step_limit"],
        size_limit=plan["task"]["size_limit"],
        fresh_context_required=plan["fresh_context_required"],
        message_digest=plan["message_digest"],
        canonical=bytes(canonical),
        message=bytes(message),
    )


def _revalidate_spec(spec: object) -> SessionTurnSpec:
    """Re-run the spec's own strict validator on an untrusted value.

    A hand-constructed, ``dataclasses.replace``-d, or tampered instance is refused
    with one static typed error. The returned value is a fresh, canonical copy.
    """
    if not isinstance(spec, SessionTurnSpec):
        raise SessionIntentValidationError(_M_BAD_SPEC)
    try:
        fields = {
            field.name: getattr(spec, field.name)
            for field in dataclasses.fields(SessionTurnSpec)
        }
        return SessionTurnSpec(**fields)
    except SessionTurnValidationError as exc:
        raise SessionIntentValidationError(_M_BAD_SPEC) from exc
    except (TypeError, AttributeError, KeyError) as exc:
        raise SessionIntentValidationError(_M_BAD_SPEC) from exc


# --------------------------------------------------------------------------- #
# Repository
# --------------------------------------------------------------------------- #
class SessionIntentRepository:
    """Durable, atomic pre-transport session-turn correlation over a verified store.

    Typical use::

        store = ProjectStore.open_at(app_dir, expected_project_id=pid)
        intents = SessionIntentRepository(store)

        spec = prepare_session_turn(store, task=..., dispatch=..., ...)
        record = intents.record_intent(spec, fence=dispatch.current_fence)
        # -> one immutable correlation, reconstructible before/after transport.
    """

    def __init__(
        self,
        store,
        *,
        runtime: RuntimeRepository | None = None,
        dispatch: RuntimeDispatchRepository | None = None,
    ) -> None:
        self._store = store
        self._runtime = runtime if runtime is not None else RuntimeRepository(store)
        self._dispatch = (
            dispatch if dispatch is not None else RuntimeDispatchRepository(store)
        )

    @property
    def project_id(self) -> str:
        """The store's own project id; the repository never uses a caller project."""
        return self._store.project_id

    def _conn(self) -> sqlite3.Connection:
        return self._store.connection

    # -- record (atomic: validate spec + parents + insert) -------------------- #
    def record_intent(
        self,
        spec: SessionTurnSpec,
        *,
        fence: int,
        created_at_utc: str | None = None,
    ) -> SessionIntentRecord:
        """Record one immutable correlation for a reserved ``opencode_session`` turn.

        The untrusted spec is revalidated through the spec's own strict validator
        and every projection is independently bound to the persisted parents. A
        **new** row is recorded only for a persisted ``opencode_session`` dispatch
        in the ``reserved`` state and under its exact current fence, after the
        target and every input are revalidated for live content/currentness/rights
        through the existing repository helper.

        An **identical** replay (same dispatch, same spec bytes, same fence) returns
        the validated historical row — even after submission and a moved head; a
        **divergent** replay raises :class:`SessionIntentConflictError`. A refused
        call writes nothing.
        """
        checked = _revalidate_spec(spec)
        project_id = _identifier(self.project_id)
        if checked.project_id != project_id:
            raise SessionIntentValidationError(_M_BAD_PROJECT)
        if not _plain_int(fence) or fence < 1:
            raise SessionIntentFenceError(_M_FENCE)
        created = (
            _utc_now()
            if created_at_utc is None
            else _require_utc_timestamp(created_at_utc)
        )

        try:
            with self._store.transaction():
                bound = self._bind_to_parents(project_id, checked)
                existing = self._intent_row(checked.dispatch_id)
                if existing is not None:
                    stored = self._validate_intent_row(existing)
                    if (
                        stored.canonical != checked.canonical
                        or stored.message != checked.message
                    ):
                        raise SessionIntentConflictError(_M_DIVERGENT)
                    if fence != stored.fence:
                        raise SessionIntentFenceError(_M_FENCE)
                    return stored

                if bound.dispatch.state != "reserved":
                    raise SessionIntentStateError(_M_STATE)
                if fence != bound.dispatch.current_fence:
                    raise SessionIntentFenceError(_M_FENCE)
                self._revalidate_live_refs(bound.bounded)
                self._insert_intent(bound, checked, fence, created)
        except SessionIntentError:
            raise
        except sqlite3.Error as exc:
            # A SQL failure is reported without its underlying text: it may name a
            # column or table but never echoes a value/source byte.
            raise SessionIntentError(_M_BAD_ROW) from exc
        stored = self.get_intent(checked.dispatch_id)
        assert stored is not None  # just committed
        return stored

    # -- reads --------------------------------------------------------------- #
    def get_intent(self, dispatch_id: str) -> SessionIntentRecord | None:
        dispatch_id = validate_identifier(dispatch_id, "dispatch_id")
        try:
            row = self._intent_row(dispatch_id)
        except sqlite3.Error as exc:
            raise SessionIntentCorruptionError(_M_BAD_ROW) from exc
        if row is None:
            return None
        return self._validate_intent_row(row)

    def list_intents(self) -> list[SessionIntentRecord]:
        try:
            rows = self._conn().execute(
                "SELECT * FROM session_turn_intent WHERE project_id = ? ORDER BY rowid",
                (self.project_id,),
            ).fetchall()
        except sqlite3.Error as exc:
            raise SessionIntentCorruptionError(_M_BAD_ROW) from exc
        return [self._validate_intent_row(row) for row in rows]

    def verify_intent(self, dispatch_id: str) -> bool:
        """``True`` iff the intent exists and revalidates (a corrupt row is ``False``)."""
        try:
            return self.get_intent(dispatch_id) is not None
        except SessionIntentCorruptionError:
            return False

    # -- whole-ledger integrity (shared with a later backup verifier) --------- #
    def verify_all_integrity(self) -> list[str]:
        """Return a list of integrity problems across every intent (empty = ok).

        Each entry is a short, static, non-secret description. Exported so a later
        backup verifier can reuse this instead of duplicating the structural/hash/
        parent revalidation.
        """
        return self._integrity_problems()

    def _integrity_problems(self) -> list[str]:
        problems: list[str] = []
        try:
            rows = self._conn().execute(
                "SELECT * FROM session_turn_intent ORDER BY rowid"
            ).fetchall()
        except sqlite3.Error as exc:
            raise SessionIntentCorruptionError(_M_BAD_ROW) from exc
        for index, row in enumerate(rows):
            if row["project_id"] != self.project_id:
                problems.append(
                    f"session_turn_intent: row {index} is a foreign project row"
                )
                continue
            if not self._link_exists("runtime_dispatch", "dispatch_id", row["dispatch_id"]):
                problems.append(
                    f"session_turn_intent: row {index} is a dangling intent "
                    f"(missing dispatch)"
                )
                continue
            if not self._link_exists("runtime_bounded_task", "task_id", row["task_id"]):
                problems.append(
                    f"session_turn_intent: row {index} is a dangling intent "
                    f"(missing task)"
                )
                continue
            if not self._link_exists("runtime_session_mapping", "mapping_id", row["mapping_id"]):
                problems.append(
                    f"session_turn_intent: row {index} is a dangling intent "
                    f"(missing session mapping)"
                )
                continue
            try:
                self._validate_intent_row(row)
            except Exception:  # noqa: BLE001 - a corrupt row must not abort the sweep
                problems.append(
                    f"session_turn_intent: row {index} failed integrity revalidation"
                )
        return problems

    def _link_exists(self, table: str, key: str, value: object) -> bool:
        row = self._conn().execute(
            f"SELECT 1 FROM {table} WHERE project_id = ? AND {key} = ?",
            (self.project_id, value),
        ).fetchone()
        return row is not None

    # -- internal: persisted-parent binding ---------------------------------- #
    def _bind_to_parents(self, project_id: str, spec: SessionTurnSpec) -> "_BoundParents":
        """Independently bind every spec projection to the persisted parents.

        Nothing is trusted from the spec's own bytes beyond the strict validator:
        the dispatch, task, session mapping, and their configuration snapshots are
        re-read and each projected group (identity, role, model, prompt, limits,
        scope, target, inputs, config, versions, message, admission) must equal the
        persisted values.
        """
        plan = _bounded_json_bytes(
            spec.canonical, too_large=_M_CANONICAL_SIZE, malformed=_M_BAD_CANONICAL
        )
        message = _bounded_json_bytes(spec.message, too_large=_M_MESSAGE, malformed=_M_MESSAGE)
        if not isinstance(plan, dict) or not isinstance(message, dict):
            raise SessionIntentValidationError(_M_BAD_SPEC)
        if set(plan.get("dispatch", {})) != _DISPATCH_PLAN_KEYS:
            raise SessionIntentValidationError(_M_BAD_SPEC)
        if set(plan.get("session", {})) != _SESSION_PLAN_KEYS:
            raise SessionIntentValidationError(_M_BAD_SPEC)
        if set(message) != _MESSAGE_KEYS:
            raise SessionIntentValidationError(_M_BAD_SPEC)
        if spec.project_id != project_id or plan.get("project_id") != project_id:
            raise SessionIntentValidationError(_M_FOREIGN)

        dispatch = self._load_dispatch(spec.dispatch_id)
        task = self._load_task(spec.task_id)
        mapping = self._load_mapping(spec.mapping_id)

        if (
            dispatch.project_id != project_id
            or task.project_id != project_id
            or mapping.project_id != project_id
        ):
            raise SessionIntentValidationError(_M_FOREIGN)

        task_snapshot = self._load_configuration(task.config_snapshot_id)
        mapping_snapshot = self._load_configuration(mapping.config_snapshot_id)
        bounded = self._rebuild_bounded_task(task, task_snapshot)
        self._bind_dispatch(dispatch, plan, spec)
        self._bind_task(task, bounded, plan, message, spec)
        self._bind_mapping(mapping, mapping_snapshot, bounded, plan, spec)
        self._bind_versions(plan, message, dispatch, task, mapping)
        self._bind_admission(project_id, plan, message, spec)
        return _BoundParents(
            dispatch=dispatch,
            task=task,
            mapping=mapping,
            bounded=bounded,
            plan=plan,
            message=message,
        )

    def _load_dispatch(self, dispatch_id: str) -> DispatchRecord:
        try:
            dispatch = self._dispatch.get_dispatch(dispatch_id)
        except DispatchCorruptionError as exc:
            raise SessionIntentCorruptionError(_M_MISSING_DISPATCH) from exc
        if dispatch is None:
            raise SessionIntentValidationError(_M_MISSING_DISPATCH)
        return dispatch

    def _load_task(self, task_id: str) -> RuntimeTaskRecord:
        try:
            task = self._runtime.get_task(task_id)
        except RuntimeCorruptionError as exc:
            raise SessionIntentCorruptionError(_M_MISSING_TASK) from exc
        if task is None:
            raise SessionIntentValidationError(_M_MISSING_TASK)
        return task

    def _load_mapping(self, mapping_id: str) -> RuntimeSessionMapping:
        try:
            mapping = self._runtime.get_session_mapping(mapping_id)
        except RuntimeCorruptionError as exc:
            raise SessionIntentCorruptionError(_M_MISSING_MAPPING) from exc
        if mapping is None:
            raise SessionIntentValidationError(_M_MISSING_MAPPING)
        return mapping

    def _load_configuration(self, snapshot_id: str):
        try:
            snapshot = self._runtime.get_configuration(snapshot_id)
        except (RuntimeCorruptionError, ValueError) as exc:
            raise SessionIntentCorruptionError(_M_PARENT_CONFIG) from exc
        if snapshot is None:
            raise SessionIntentValidationError(_M_PARENT_CONFIG)
        return snapshot

    @staticmethod
    def _rebuild_bounded_task(task: RuntimeTaskRecord, snapshot) -> BoundedTask:
        try:
            stored = json.loads(task.canonical.decode("utf-8"))
            bounded = BoundedTask.from_mapping(stored, configuration=snapshot.role_configuration())
        except (ValueError, TypeError, KeyError, RecursionError, UnicodeError, AgentRoleError) as exc:
            raise SessionIntentCorruptionError(_M_PARENT_TASK) from exc
        if bounded.canonical_bytes() != task.canonical or bounded.fingerprint() != task.fingerprint:
            raise SessionIntentValidationError(_M_PARENT_TASK)
        return bounded

    @staticmethod
    def _bind_dispatch(dispatch: DispatchRecord, plan: Mapping[str, Any], spec: SessionTurnSpec) -> None:
        if dispatch.route != SESSION_INTENT_ROUTE:
            raise SessionIntentValidationError(_M_ROUTE)
        if (
            dispatch.dispatch_id != spec.dispatch_id
            or dispatch.request_id != spec.dispatch_request_id
            or dispatch.task_id != spec.task_id
            or dispatch.task_fingerprint != spec.task_fingerprint
            or dispatch.input_digest != spec.input_digest
            or dispatch.version_digest != spec.version_digest
        ):
            raise SessionIntentValidationError(_M_PARENT_DISPATCH)
        if plan["dispatch"] != {
            "dispatch_id": dispatch.dispatch_id,
            "request_id": dispatch.request_id,
            "route": dispatch.route,
            "stage": dispatch.stage,
            "input_digest": dispatch.input_digest,
            "version_digest": dispatch.version_digest,
        }:
            raise SessionIntentValidationError(_M_PARENT_DISPATCH)

    @staticmethod
    def _bind_task(
        task: RuntimeTaskRecord,
        bounded: BoundedTask,
        plan: Mapping[str, Any],
        message: Mapping[str, Any],
        spec: SessionTurnSpec,
    ) -> None:
        expected_task = {
            "task_id": task.task_id,
            "request_id": task.request_id,
            "fingerprint": task.fingerprint,
            "target": dict(bounded.target),
            "input_refs": [dict(entry) for entry in bounded.input_refs],
            "scope": list(bounded.scope),
            "prompt_version": bounded.prompt_version,
            "size_limit": bounded.size_limit,
            "step_limit": bounded.step_limit,
        }
        if plan["task"] != expected_task:
            raise SessionIntentValidationError(_M_PARENT_TASK)
        if (
            plan["role"] != bounded.role
            or spec.role != bounded.role
            or task.role != bounded.role
            or spec.task_fingerprint != task.fingerprint
            or spec.prompt_version != bounded.prompt_version
            or spec.step_limit != bounded.step_limit
            or spec.size_limit != bounded.size_limit
        ):
            raise SessionIntentValidationError(_M_PARENT_TASK)
        if (
            message["task_id"] != task.task_id
            or message["task_fingerprint"] != task.fingerprint
            or message["role"] != bounded.role
            or message["scope"] != expected_task["scope"]
            or message["target"] != expected_task["target"]
            or message["input_refs"] != expected_task["input_refs"]
            or message["prompt_version"] != bounded.prompt_version
            or message["size_limit"] != bounded.size_limit
            or message["step_limit"] != bounded.step_limit
        ):
            raise SessionIntentValidationError(_M_PARENT_TASK)

    @staticmethod
    def _bind_mapping(
        mapping: RuntimeSessionMapping,
        mapping_snapshot,
        bounded: BoundedTask,
        plan: Mapping[str, Any],
        spec: SessionTurnSpec,
    ) -> None:
        """Bind the spec's session/config/model projections to the persisted mapping.

        The mapping's ``service_instance_id`` is the owned location id under the
        **current** semantics (the planner records ``location.location_id`` there);
        this module binds the spec to that persisted value and never requires the
        old location directory or service process to still exist.
        """
        if (
            mapping.mapping_id != spec.mapping_id
            or mapping.session_id != spec.session_id
            or mapping.service_instance_id != spec.service_instance_id
            or mapping.clean_location != spec.clean_location
            or mapping.config_snapshot_id != spec.config_snapshot_id
            or mapping.config_digest != spec.config_digest
            or mapping.model_ref != spec.model
            or mapping.role != spec.role
        ):
            raise SessionIntentValidationError(_M_PARENT_MAPPING)
        if plan["session"] != {
            "mapping_id": mapping.mapping_id,
            "session_id": mapping.session_id,
            "service_instance_id": mapping.service_instance_id,
            "clean_location": mapping.clean_location,
            "opencode_project_id": mapping.opencode_project_id,
            "config_snapshot_id": mapping.config_snapshot_id,
            "config_digest": mapping.config_digest,
            "model": mapping.model_ref,
        }:
            raise SessionIntentValidationError(_M_PARENT_MAPPING)
        # The mapping's immutable configuration must pin exactly the task's role
        # configuration (role model/prompt/steps) for this role.
        try:
            mapping_role_config = mapping_snapshot.role_configuration().for_role(bounded.role)
        except AgentRoleError as exc:
            raise SessionIntentValidationError(_M_PARENT_CONFIG) from exc
        if mapping_role_config != bounded.role_config:
            raise SessionIntentValidationError(_M_PARENT_CONFIG)
        if bounded.role_config.model.ref != mapping.model_ref:
            raise SessionIntentValidationError(_M_PARENT_CONFIG)

    @staticmethod
    def _bind_versions(
        plan: Mapping[str, Any],
        message: Mapping[str, Any],
        dispatch: DispatchRecord,
        task: RuntimeTaskRecord,
        mapping: RuntimeSessionMapping,
    ) -> None:
        versions = plan["versions"]
        try:
            wrapper = PureJsonVersionSet(versions, consumed=SESSION_INTENT_CONSUMED_SLOTS)
        except Exception as exc:  # RecordValidationError
            raise SessionIntentValidationError(_M_VERSIONS) from exc
        normalized = wrapper.as_mapping()
        if normalized != versions:
            raise SessionIntentValidationError(_M_VERSIONS)
        if normalized.get("route") != SESSION_INTENT_ROUTE:
            raise SessionIntentValidationError(_M_VERSION_ROUTE)
        if normalized.get("input_digest") != dispatch.input_digest:
            raise SessionIntentValidationError(_M_VERSION_INPUT)
        if _sha256_hex(canonical_json(normalized)) != dispatch.version_digest:
            raise SessionIntentValidationError(_M_VERSION_HASH)
        model_slot = normalized.get("model")
        prompt_slot = normalized.get("prompt")
        if (
            not isinstance(model_slot, dict)
            or model_slot.get("state") != "known"
            or not isinstance(model_slot.get("ref"), dict)
            or model_slot["ref"].get("version") != mapping.model_ref
        ):
            raise SessionIntentValidationError(_M_VERSION_MODEL)
        if (
            not isinstance(prompt_slot, dict)
            or prompt_slot.get("state") != "known"
            or not isinstance(prompt_slot.get("ref"), dict)
            or prompt_slot["ref"].get("version") != task.prompt_version
        ):
            raise SessionIntentValidationError(_M_VERSION_PROMPT)
        if message.get("versions") != versions:
            raise SessionIntentValidationError(_M_VERSIONS)

    @staticmethod
    def _bind_admission(
        project_id: str,
        plan: Mapping[str, Any],
        message: Mapping[str, Any],
        spec: SessionTurnSpec,
    ) -> None:
        expected = _admission_id(project_id, spec.dispatch_id)
        if spec.admission_id != expected or plan["admission_id"] != expected:
            raise SessionIntentValidationError(_M_ADMISSION)
        if _sha256_hex(spec.message) != spec.message_digest:
            raise SessionIntentValidationError(_M_DIGEST_MISMATCH)
        if plan["message_digest"] != spec.message_digest:
            raise SessionIntentValidationError(_M_DIGEST_MISMATCH)
        if message["dispatch_id"] != spec.dispatch_id:
            raise SessionIntentValidationError(_M_PARENT_DISPATCH)
        if message["mapping_id"] != spec.mapping_id or message["session_id"] != spec.session_id:
            raise SessionIntentValidationError(_M_PARENT_MAPPING)
        if message["project_id"] != project_id:
            raise SessionIntentValidationError(_M_FOREIGN)

    def _revalidate_live_refs(self, bounded: BoundedTask) -> None:
        """Revalidate the target/inputs now (bytes, digest, rights, currentness)."""
        verify = getattr(self._runtime, "_verify_new_task_refs", None)
        if not callable(verify):
            raise SessionIntentValidationError(_M_LIVE_REFS)
        try:
            verify(bounded)
        except RuntimeCorruptionError as exc:
            raise SessionIntentCorruptionError(_M_PARENT_TASK) from exc
        except (ValueError, TypeError, KeyError, AgentRoleError) as exc:
            raise SessionIntentValidationError(_M_LIVE_REFS) from exc

    # -- internal: insert + row access --------------------------------------- #
    def _insert_intent(
        self,
        bound: "_BoundParents",
        spec: SessionTurnSpec,
        fence: int,
        created: str,
    ) -> None:
        plan = bound.plan
        spec_digest = _sha256_hex(spec.canonical)
        self._conn().execute(
            "INSERT INTO session_turn_intent "
            "(project_id, dispatch_id, intent_id, schema_version, fence, admission_id, "
            " session_id, mapping_id, service_instance_id, task_id, task_fingerprint, "
            " role, model, prompt_version, input_digest, version_digest, "
            " config_snapshot_id, config_digest, spec_digest, message_digest, "
            " message_bytes, canonical, message, created_at_utc) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                self.project_id,
                spec.dispatch_id,
                _intent_id(self.project_id, spec.dispatch_id),
                SESSION_INTENT_SCHEMA_VERSION,
                fence,
                plan["admission_id"],
                spec.session_id,
                spec.mapping_id,
                spec.service_instance_id,
                spec.task_id,
                spec.task_fingerprint,
                spec.role,
                spec.model,
                spec.prompt_version,
                spec.input_digest,
                spec.version_digest,
                spec.config_snapshot_id,
                spec.config_digest,
                spec_digest,
                spec.message_digest,
                len(spec.message),
                bytes(spec.canonical),
                bytes(spec.message),
                created,
            ),
        )

    def _intent_row(self, dispatch_id: str) -> sqlite3.Row | None:
        return self._conn().execute(
            "SELECT * FROM session_turn_intent WHERE project_id = ? AND dispatch_id = ?",
            (self.project_id, dispatch_id),
        ).fetchone()

    @_fail_closed_row("session turn intent")
    def _validate_intent_row(self, row: sqlite3.Row) -> SessionIntentRecord:
        """Structurally revalidate a stored intent; fail closed and history-preserving.

        It reconstructs the spec from the persisted bytes through the spec's strict
        validator, re-derives every projected column, and re-binds the whole spec to
        the persisted parents (including the immutable actual bytes/rights of the
        recorded target/inputs) — never requiring a current head or a live service.
        """
        if row["project_id"] != self.project_id:
            raise SessionIntentCorruptionError(_M_FOREIGN)
        dispatch_id = _identifier(row["dispatch_id"])
        if row["intent_id"] != _intent_id(self.project_id, dispatch_id):
            raise SessionIntentCorruptionError(_M_PARENT_DISPATCH)
        if not _plain_int(row["fence"]) or row["fence"] < 1:
            raise SessionIntentCorruptionError(_M_FENCE_ROW)
        if (
            not _plain_int(row["schema_version"])
            or row["schema_version"] != SESSION_INTENT_SCHEMA_VERSION
        ):
            raise SessionIntentCorruptionError(_M_BAD_ROW)
        _require_utc_timestamp(row["created_at_utc"])
        canonical = bytes(row["canonical"])
        message = bytes(row["message"])
        if len(canonical) == 0 or len(canonical) > MAX_SESSION_INTENT_BYTES:
            raise SessionIntentCorruptionError(_M_CANONICAL_SIZE)
        if len(message) == 0 or len(message) > MAX_SESSION_INTENT_BYTES:
            raise SessionIntentCorruptionError(_M_MESSAGE)
        if _sha256_hex(canonical) != _bare_hex(row["spec_digest"]):
            raise SessionIntentCorruptionError(_M_DIGEST_MISMATCH)
        if _sha256_hex(message) != _bare_hex(row["message_digest"]):
            raise SessionIntentCorruptionError(_M_DIGEST_MISMATCH)
        if not _plain_int(row["message_bytes"]) or row["message_bytes"] != len(message):
            raise SessionIntentCorruptionError(_M_MESSAGE)

        spec = _spec_from_bytes(canonical, message)
        plan = json.loads(canonical.decode("utf-8"))
        bound = self._bind_to_parents(self.project_id, spec)
        if row["fence"] != bound.dispatch.current_fence:
            raise SessionIntentCorruptionError(_M_FENCE_ROW)
        self._assert_columns_match(row, spec, plan, canonical, message)
        return SessionIntentRecord(
            project_id=self.project_id,
            dispatch_id=dispatch_id,
            intent_id=row["intent_id"],
            schema_version=row["schema_version"],
            fence=row["fence"],
            admission_id=plan["admission_id"],
            session_id=plan["session"]["session_id"],
            mapping_id=plan["session"]["mapping_id"],
            service_instance_id=plan["session"]["service_instance_id"],
            task_id=plan["task"]["task_id"],
            task_fingerprint=plan["task"]["fingerprint"],
            role=plan["role"],
            model=plan["session"]["model"],
            prompt_version=plan["task"]["prompt_version"],
            input_digest=plan["dispatch"]["input_digest"],
            version_digest=plan["dispatch"]["version_digest"],
            config_snapshot_id=plan["session"]["config_snapshot_id"],
            config_digest=plan["session"]["config_digest"],
            spec_digest=row["spec_digest"],
            message_digest=row["message_digest"],
            message_bytes=row["message_bytes"],
            created_at_utc=row["created_at_utc"],
            canonical=canonical,
            message=message,
        )

    @staticmethod
    def _assert_columns_match(
        row: sqlite3.Row,
        spec: SessionTurnSpec,
        plan: Mapping[str, Any],
        canonical: bytes,
        message: bytes,
    ) -> None:
        expected = {
            "intent_id": _intent_id(plan["project_id"], plan["dispatch"]["dispatch_id"]),
            "schema_version": SESSION_INTENT_SCHEMA_VERSION,
            "admission_id": plan["admission_id"],
            "session_id": plan["session"]["session_id"],
            "mapping_id": plan["session"]["mapping_id"],
            "service_instance_id": plan["session"]["service_instance_id"],
            "task_id": plan["task"]["task_id"],
            "task_fingerprint": plan["task"]["fingerprint"],
            "role": plan["role"],
            "model": plan["session"]["model"],
            "prompt_version": plan["task"]["prompt_version"],
            "input_digest": plan["dispatch"]["input_digest"],
            "version_digest": plan["dispatch"]["version_digest"],
            "config_snapshot_id": plan["session"]["config_snapshot_id"],
            "config_digest": plan["session"]["config_digest"],
            "spec_digest": _sha256_hex(canonical),
            "message_digest": _sha256_hex(message),
            "message_bytes": len(message),
        }
        for column, value in expected.items():
            if row[column] != value:
                raise SessionIntentCorruptionError(_M_COLUMNS)


@dataclass(frozen=True)
class _BoundParents:
    """The re-read persisted parents a validated spec was bound to."""

    dispatch: DispatchRecord
    task: RuntimeTaskRecord
    mapping: RuntimeSessionMapping
    bounded: BoundedTask
    plan: Mapping[str, Any]
    message: Mapping[str, Any]


# --------------------------------------------------------------------------- #
# Read-only entry point (for a later backup verifier)
# --------------------------------------------------------------------------- #
class _ReadOnlyIntentStoreView:
    """The minimal read-only surface the integrity sweep reads from a store."""

    __slots__ = ("connection", "project_id", "schema_version")

    def __init__(self, conn: sqlite3.Connection, project_id: str) -> None:
        self.connection = conn
        self.project_id = project_id
        self.schema_version = 0


def session_intent_integrity_problems(
    conn: sqlite3.Connection, project_id: str
) -> list[str]:
    """Revalidate every recorded intent for ``project_id`` on ``conn`` (read-only).

    Reusable by a later backup verifier. Returns a list of static problem
    descriptions (empty when every intent is structurally intact, internally
    consistent, and re-binds to its persisted parents). Foreign, dangling/orphan,
    and corrupt rows are all reported. The connection is never written to.
    """
    repo = SessionIntentRepository(_ReadOnlyIntentStoreView(conn, project_id))
    return repo.verify_all_integrity()
