"""Durable, immutable **pre-dispatch Jev decision request intent**.

Authority
---------

* ``docs/implementation/04-agent-runtime.md`` deliverable 4 (Jev decision adapter
  with validated outputs and durable application records) and acceptance criteria
  5/6 ("Model/provider errors, refusals, invalid shapes, and partial Jev errors
  are not interpreted as scientific exclusions"; "Stateless decision responses are
  persisted with rubric/model/input versions. Account separately for direct calls
  not covered by session statistics").
* ``docs/contracts/02-gates-and-runtime.md`` §6.1 (``VersionSet``), §6.2
  (``QuestionDef``/``DecisionRecord``), §6.3 (``CostRecord``), and §8.1.1
  (``DispatchAttempt``), read as the authoritative record shapes — no field is
  invented here.

What this module **is**
-----------------------

The storage half of "reserve before dispatch, and **record the exact question
before submitting it**". Given an already-persisted direct Jev dispatch (a
:class:`~review_article_agent.runtime.dispatch.DispatchRecord` in the ``reserved``
state), the persisted bounded task it pins, a **validated** Jev
:class:`~review_article_agent.runtime.jev.DispatchPlan`, the per-question
``question_def_ref``/``criteria_ref`` maps, and the consumed ``VersionSet``, it
records **one immutable canonical intent row** (schema migration 8) that binds:

* the exact same-project ``target`` and every per-question ``question_def_ref`` /
  ``criteria_ref`` (resolved and byte-verified through the
  :class:`~review_article_agent.storage.artifacts.ArtifactRepository` — the
  **actual** stored bytes are re-hashed and re-sized, and the pinned rights are
  re-validated, never trusted from metadata alone);
* the exact question order/ids and each question's descriptor digest, so a later
  runner/result can prove **completeness** (every question was asked and answered);
* the consumed ``VersionSet`` (``model``/``prompt``/``adapter`` known, plus
  ``rubric``/``parser`` when actually consumed), with
  ``versions.input_digest == dispatch.input_digest``, the canonical versions hash
  ``== dispatch.version_digest``, and ``versions.prompt.ref.version ==
  task.prompt_version``;
* the exact request binding — the plan's serialized ``request_body`` digest/bytes,
  the conservative cost upper bound, the pricing reference, and the reserve —
  **recomputed** from the validated plan, never taken from an arbitrary
  caller-supplied hash.

The reviewable question representation is the C02 §6.2 ``QuestionDef`` as a
canonical JSON object (``question_id``, ``question_kind``, ``instructions``,
``options`` iff ``choice``, ``levels`` iff ``score``, ``criteria_ref``); the
criteria revision's JSON supplies the **actual** ``criteria``. The module rebuilds
the typed :class:`~review_article_agent.runtime.jev.QuestionDef` from those refs
and compares it (and its descriptor digest) with the plan's questions exactly, so
the recorded intent describes precisely the questions the plan would send.

What this module **is not** (the boundary)
------------------------------------------

* It performs **no** network, transport, or model call, and **no** execution. It
  records an *intent* — it never submits, admits, or completes a dispatch, and it
  never writes a decision result. Those are separate tasks.
* It **grants no authority**: the row is inert metadata; it neither authorizes a
  dispatch nor asserts scientific eligibility/exclusion. A Jev answer is a machine
  proposal about text.
* It stores **no** credential, capability, endpoint secret, or response content.
  The canonical bytes hold the bounded request/question/version metadata (which
  may include the request's source state) and are treated as **private local
  metadata**: they are bounded and are never logged or echoed in an error.
* It does **not** advance the dispatch's state machine. It only reads the dispatch
  identity; the state transitions stay in ``runtime/dispatch.py``.

Every failure is a static, value-free :class:`DecisionIntentError`: a message
never echoes caller text, source bytes, a credential, a model identifier, a ref,
or a digest.
"""

from __future__ import annotations

import functools
import hashlib
import json
import re
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from types import MappingProxyType
from typing import Any

from ..domain.errors import StoreError
from ..domain.records import (
    canonical_json,
    require_storage_allowed,
    validate_identifier,
    validate_ref,
    validate_rights_record,
)
from ..runtime import jev
from ..runtime.decision_records import question_descriptor_digest
from ..runtime.dispatch import (
    DispatchCorruptionError,
    DispatchRecord,
    RuntimeDispatchRepository,
)
from .agent_runtime import (
    PureJsonVersionSet,
    RuntimeCorruptionError,
    RuntimeRepository,
    RuntimeTaskRecord,
    RuntimeValidationError,
)
from .artifacts import ArtifactRepository, sha256_digest

__all__ = [
    "INTENT_SCHEMA_VERSION",
    "INTENT_ROUTE",
    "MAX_INTENT_BYTES",
    "JEV_CONSUMED_SLOTS",
    "OPTIONAL_CONSUMED_SLOTS",
    "DecisionIntentError",
    "DecisionIntentValidationError",
    "DecisionIntentConflictError",
    "DecisionIntentStateError",
    "DecisionIntentCorruptionError",
    "QuestionIntent",
    "DecisionIntentRecord",
    "DecisionIntentRepository",
    "decision_intent_integrity_problems",
]

# --------------------------------------------------------------------------- #
# Vocabulary / bounds
# --------------------------------------------------------------------------- #
#: Version of the *recorded intent representation*. Independent of the store
#: schema version and of the ``VersionSet`` it embeds.
INTENT_SCHEMA_VERSION = 1

#: The only route a recorded Jev request intent may describe (§8.1.1).
INTENT_ROUTE = "direct_call"

#: Upper bound on the canonical bytes of one recorded intent, so a pathological
#: question/state payload cannot produce an unbounded row. The plan request is
#: already bounded by the adapter's ``SAFE_MAX_REQUEST_BYTES`` (32 KiB).
MAX_INTENT_BYTES = 262_144

#: Upper bound on a question-definition or criteria revision's JSON bytes, checked
#: **before** any parse so an overlong referenced document is refused.
MAX_QUESTION_DEF_BYTES = 131_072
MAX_CRITERIA_BYTES = 131_072

#: The canonical-JSON depth bound (matches ``domain.records``); applied to every
#: parsed stored/intent document before it is walked or thawed.
_MAX_JSON_DEPTH = 64

#: Version slots every Jev/typed-decision stage consumes (§6.1).
JEV_CONSUMED_SLOTS = ("model", "prompt", "adapter")

#: Slots a caller may additionally name as consumed (``known`` iff consumed).
OPTIONAL_CONSUMED_SLOTS = ("rubric", "parser")

_MAX_IDENTIFIER_CHARS = 512
_MAX_META_CHARS = 256
_MAX_DECIMAL_INPUT_LEN = 128
_MAX_SIGNIFICANT_DIGITS = 40
_MAX_ADJUSTED_EXPONENT = 24

_HEX_RE = re.compile(r"^[0-9a-f]{64}$")
_UTC_TIMESTAMP_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|\+00:00)$"
)

#: The exact keys of a ``RefRevision`` (C01 §3.1-3.3). Used to tell a bare single
#: ref from a per-question ``{question_id: ref}`` mapping.
_REF_KEYS = frozenset({"project_id", "artifact_id", "revision_id"})

#: The permitted keys of the reviewable C02 §6.2 ``QuestionDef`` JSON object.
_QUESTION_DEF_KEYS = frozenset(
    {"question_id", "question_kind", "instructions", "options", "levels", "criteria_ref"}
)
_QUESTION_KINDS = ("predicate", "choice", "score", "box")

_QUESTION_INTENT_KEYS = frozenset(
    {"question_id", "question_kind", "question_def_ref", "criteria_ref",
     "question_def_digest", "supported"}
)

_INTENT_KEYS = frozenset(
    {
        "schema_version",
        "intent_id",
        "route",
        "project_id",
        "dispatch_id",
        "task_id",
        "task_fingerprint",
        "stage",
        "target",
        "input_digest",
        "version_digest",
        "reserve_usd",
        "bound_evidence_digest",
        "prompt_version",
        "request_sha256",
        "request_bytes",
        "cost_upper_bound_usd",
        "pricing_version",
        "model",
        "endpoint",
        "versions",
        "consumed_versions",
        "questions",
        "question_ids",
        "supported_question_ids",
        "unsupported_question_ids",
        "plan_request",
    }
)

# --------------------------------------------------------------------------- #
# Errors (static, curated, non-reflective)
# --------------------------------------------------------------------------- #
class DecisionIntentError(StoreError):
    """Base class for every decision-request-intent failure.

    Every message is **static and curated**: it never echoes caller text, source
    bytes, a credential, a model identifier, a ref, or a digest.
    """


class DecisionIntentValidationError(DecisionIntentError, ValueError):
    """An input task/dispatch/plan/ref/version is malformed or inconsistent."""


class DecisionIntentConflictError(DecisionIntentError):
    """A divergent replay for the same dispatch's recorded intent."""


class DecisionIntentStateError(DecisionIntentError):
    """A new intent was requested for a dispatch that is not reserved."""


class DecisionIntentCorruptionError(DecisionIntentError):
    """A stored intent row fails structural/hash/ref/version revalidation on read."""


# --------------------------------------------------------------------------- #
# Static messages (never echo a value)
# --------------------------------------------------------------------------- #
_M_BAD_TASK = "the supplied task is not a persisted bounded task of this project"
_M_BAD_DISPATCH = "the supplied dispatch is not a persisted direct dispatch of this project"
_M_BAD_PLAN = "the supplied dispatch plan is not a valid, cost-bounded Jev plan"
_M_BAD_REF = "a decision-intent reference is not an exact same-project revision reference"
_M_BAD_ID = "a decision-intent identifier is not a valid identifier"
_M_BAD_PROJECT = "the decision-intent project identity is not a valid identifier"
_M_REF_SINGLE = "a single ref is accepted only for a single-question intent"
_M_REF_COVERAGE = "the per-question refs must cover exactly the planned questions"
_M_BAD_QUESTION_DEF = "the question definition revision is not a canonical C02 QuestionDef"
_M_BAD_CRITERIA = "the criteria revision is not a valid criteria value for its question"
_M_QUESTION_MISMATCH = "a rebuilt question does not match the planned question"
_M_QUESTION_ORDER = "the planned question ids/order are not the declared ones"
_M_DUP_QUESTION = "question ids in the intent must be unique"
_M_BAD_VERSIONS = "the supplied version set is not a valid consumed Jev version set"
_M_VERSION_ROUTE = "a Jev request intent must use the direct_call version route"
_M_VERSION_CONSUMED = "the consumed version slots are not a valid Jev consumption set"
_M_VERSION_INPUT = "the version set input digest does not match the dispatch input digest"
_M_VERSION_HASH = "the canonical version-set hash does not match the dispatch version digest"
_M_PROMPT_VERSION = "the version-set prompt version does not match the task prompt version"
_M_RESERVE = "the plan reserve does not match the dispatch reserve"
_M_ROUTE = "only a direct_call dispatch may carry a Jev request intent"
_M_STATE = "a new request intent may be recorded only for a reserved dispatch"
_M_DIVERGENT = "a different request intent is already recorded for this dispatch"
_M_REF_MISSING = "a referenced revision does not exist in this project"
_M_REF_INTEGRITY = "a referenced revision's content does not match its recorded digest/size"
_M_REF_STALE = "a referenced revision is not the current revision of its artifact"
_M_REF_SCOPE = "a referenced revision is outside the task's target/input scope"
_M_RIGHTS = "a referenced revision's pinned rights record is missing or not storage-allowed"
_M_DIGEST = "a decision-intent digest is not a lowercase hex SHA-256 digest"
_M_TIMESTAMP = "a decision-intent timestamp is not an RFC 3339 UTC instant"
_M_DECIMAL = "a decision-intent amount is not an exact bounded decimal"
_M_CANONICAL = "the recorded intent canonical bytes are malformed"
_M_COLUMNS = (
    "decision request intent: a projected column disagrees with the canonical bytes."
)
_M_REQUEST_BINDING = "the recorded plan request does not match the recorded request digest/size"
_M_RECORD_SIZE = "a decision-intent record exceeds the permitted canonical size"
_M_BAD_ROW = "the stored decision-intent row is malformed"
_M_TASK_DISPATCH = "the supplied task is not the task the dispatch pins"
_M_PARENT = "the recorded intent does not match its parent dispatch/task"
_M_PROJECT = "the recorded intent project does not match the store project"
_M_CANONICAL_SIZE = "the recorded intent canonical bytes exceed the permitted size"
_M_CANONICAL_ROUNDTRIP = "the recorded intent bytes are not in canonical form"
_M_DESCRIPTOR = "a recorded question descriptor does not match its referenced definition"
_M_REF_TOO_LARGE = "a referenced question/criteria revision exceeds the permitted size"


# --------------------------------------------------------------------------- #
# Small helpers (bounded, exact, never defaulting)
# --------------------------------------------------------------------------- #
def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _bare_hex(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise DecisionIntentValidationError(f"{field}: must be a digest string")
    text = value[7:] if value.startswith("sha256:") else value
    if _HEX_RE.fullmatch(text) is None:
        raise DecisionIntentValidationError(f"{field}: must be a lowercase hex SHA-256 digest")
    return text


def _intent_id(project_id: str, dispatch_id: str) -> str:
    material = canonical_json([project_id, dispatch_id])
    return "dint_" + hashlib.sha256(material).hexdigest()[:32]


def _identifier(value: object) -> str:
    try:
        normalized = validate_identifier(value)
    except Exception as exc:  # RecordValidationError / TypeError
        raise DecisionIntentValidationError(_M_BAD_ID) from exc
    if len(normalized) > _MAX_IDENTIFIER_CHARS:
        raise DecisionIntentValidationError(_M_BAD_ID)
    return normalized


def _bounded_decimal_string(amount: Decimal) -> str | None:
    if not amount.is_finite():
        return None
    _sign, digits, exponent = amount.as_tuple()
    if len(digits) > _MAX_SIGNIFICANT_DIGITS:
        return None
    magnitude = exponent + len(digits) - 1 if amount != 0 else exponent
    if abs(magnitude) > _MAX_ADJUSTED_EXPONENT:
        return None
    return format(amount, "f")


def _parse_exact_decimal(value: object, *, positive: bool = False) -> str | None:
    if not isinstance(value, str) or value == "" or len(value) > _MAX_DECIMAL_INPUT_LEN:
        return None
    try:
        amount = Decimal(value)
    except (InvalidOperation, ValueError):
        return None
    if not amount.is_finite():
        return None
    if amount < 0 or (positive and amount == 0):
        return None
    return _bounded_decimal_string(amount)


def _is_hex_digest(value: object) -> bool:
    return isinstance(value, str) and _HEX_RE.fullmatch(value) is not None


def _require_utc_timestamp(value: object, field: str) -> str:
    if not isinstance(value, str) or _UTC_TIMESTAMP_RE.fullmatch(value) is None:
        raise DecisionIntentValidationError(_M_TIMESTAMP)
    candidate = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise DecisionIntentValidationError(_M_TIMESTAMP) from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise DecisionIntentValidationError(_M_TIMESTAMP)
    return value


def _bounded_str(value: object) -> bool:
    return isinstance(value, str) and 0 < len(value) <= _MAX_META_CHARS


def _deep_freeze(value: Any) -> Any:
    """Deep-freeze a JSON-portable value to immutable containers.

    Every :class:`~collections.abc.Mapping` becomes a read-only
    :class:`~types.MappingProxyType` (deeply) and every list/tuple becomes a tuple,
    so a returned record exposes **no** mutable alias: a caller cannot mutate the
    recorded intent through a nested dict/list. :func:`_thaw` returns a fresh plain
    copy for a JSON-safe mapping.
    """
    if isinstance(value, Mapping):
        return MappingProxyType(
            {key: _deep_freeze(item) for key, item in value.items()}
        )
    if isinstance(value, (list, tuple)):
        return tuple(_deep_freeze(item) for item in value)
    return value


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_thaw(item) for item in value]
    return value


def _bounded_json_bytes(
    data: object, *, max_bytes: int, too_large: str, malformed: str
) -> Any:
    """Parse bounded, portable JSON bytes; refuse an overlong/hostile document.

    The length is bounded **before** the parse, ``json.loads`` recursion is caught,
    and the parsed value is re-validated through :func:`canonical_json` (which
    enforces the depth/cycle/portability bounds) **before** any recursive walk.
    """
    if not isinstance(data, (bytes, bytearray)):
        raise DecisionIntentValidationError(malformed)
    raw = bytes(data)
    if len(raw) > max_bytes:
        raise DecisionIntentValidationError(too_large)
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise DecisionIntentValidationError(malformed) from exc
    try:
        canonical_json(parsed)
    except Exception as exc:  # RecordValidationError / ValueError / TypeError
        raise DecisionIntentValidationError(malformed) from exc
    return parsed


def _bounded_request_mapping(request: object) -> None:
    """Bound a plan's raw request (length/depth/cycles) before any thaw/walk."""
    if not isinstance(request, Mapping):
        raise DecisionIntentValidationError(_M_BAD_PLAN)
    try:
        canonical_json(request)
    except Exception as exc:  # RecordValidationError / ValueError / TypeError
        raise DecisionIntentValidationError(_M_BAD_PLAN) from exc


def _fail_closed_row(what: str):
    """Decorate a stored-row validator so a malformed row fails closed.

    A stored row is untrusted input. Any helper that rejects it must surface as
    the single typed :class:`DecisionIntentCorruptionError`. The message is
    static: it never echoes the offending value.
    """

    def decorate(func):
        @functools.wraps(func)
        def wrapper(self, *args, **kwargs):
            try:
                return func(self, *args, **kwargs)
            except (DecisionIntentCorruptionError, DecisionIntentConflictError):
                raise
            except (
                DecisionIntentError,
                ValueError,
                TypeError,
                KeyError,
                IndexError,
                UnicodeError,
                RuntimeValidationError,
                RuntimeCorruptionError,
                DispatchCorruptionError,
                sqlite3.Error,
            ) as exc:
                raise DecisionIntentCorruptionError(f"{what}: {_M_BAD_ROW}") from exc

        return wrapper

    return decorate


# --------------------------------------------------------------------------- #
# Value objects
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class QuestionIntent:
    """One recorded per-question binding (refs + descriptor digest + support)."""

    question_id: str
    question_kind: str
    question_def_ref: Mapping[str, str]
    criteria_ref: Mapping[str, str]
    question_def_digest: str
    supported: bool

    def __post_init__(self) -> None:
        # No mutable alias: the exposed refs are deeply read-only.
        object.__setattr__(self, "question_def_ref", _deep_freeze(dict(self.question_def_ref)))
        object.__setattr__(self, "criteria_ref", _deep_freeze(dict(self.criteria_ref)))

    def to_mapping(self) -> dict[str, Any]:
        return {
            "question_id": self.question_id,
            "question_kind": self.question_kind,
            "question_def_ref": dict(self.question_def_ref),
            "criteria_ref": dict(self.criteria_ref),
            "question_def_digest": self.question_def_digest,
            "supported": self.supported,
        }


@dataclass(frozen=True)
class DecisionIntentRecord:
    """One immutable, validated recorded request intent (as stored, revalidated).

    ``canonical`` is the exact canonical JSON stored in the row; ``content_digest``
    is its recomputed SHA-256. ``created_at_utc`` is a row column only and is
    deliberately **not** part of the canonical bytes, so an identical replay is
    byte-identical regardless of when it was first recorded.
    """

    project_id: str
    intent_id: str
    schema_version: int
    dispatch_id: str
    task_id: str
    task_fingerprint: str
    route: str
    stage: str
    input_digest: str
    version_digest: str
    reserve_usd: str
    bound_evidence_digest: str
    request_sha256: str
    request_bytes: int
    cost_upper_bound_usd: str
    pricing_version: str
    prompt_version: str
    target: Mapping[str, str]
    questions: tuple[QuestionIntent, ...]
    versions: Mapping[str, Any]
    consumed_versions: tuple[str, ...]
    model: str
    endpoint: str
    plan_request: Mapping[str, Any]
    content_digest: str
    created_at_utc: str
    canonical: bytes

    def __post_init__(self) -> None:
        # Deep-freeze every exposed nested field so a returned record is truly
        # immutable and exposes no mutable alias (``versions``/``target``/
        # ``plan_request`` and each question's refs). ``to_mapping`` returns a
        # fresh plain copy.
        object.__setattr__(self, "target", _deep_freeze(dict(self.target)))
        object.__setattr__(self, "versions", _deep_freeze(self.versions))
        object.__setattr__(self, "plan_request", _deep_freeze(self.plan_request))
        object.__setattr__(self, "questions", tuple(self.questions))
        object.__setattr__(self, "consumed_versions", tuple(self.consumed_versions))
        object.__setattr__(self, "canonical", bytes(self.canonical))

    @property
    def digest(self) -> str:
        """The recomputed SHA-256 of the exact canonical intent bytes."""
        return self.content_digest

    def question_ids(self) -> tuple[str, ...]:
        """The recorded question ids in their exact planned order."""
        return tuple(q.question_id for q in self.questions)

    def supported_question_ids(self) -> tuple[str, ...]:
        return tuple(q.question_id for q in self.questions if q.supported)

    def unsupported_question_ids(self) -> tuple[str, ...]:
        return tuple(q.question_id for q in self.questions if not q.supported)

    def to_mapping(self) -> dict[str, Any]:
        """A fresh plain (JSON-safe) copy of the recorded intent canonical content."""
        return {
            "schema_version": self.schema_version,
            "intent_id": self.intent_id,
            "route": self.route,
            "project_id": self.project_id,
            "dispatch_id": self.dispatch_id,
            "task_id": self.task_id,
            "task_fingerprint": self.task_fingerprint,
            "stage": self.stage,
            "target": dict(self.target),
            "input_digest": self.input_digest,
            "version_digest": self.version_digest,
            "reserve_usd": self.reserve_usd,
            "bound_evidence_digest": self.bound_evidence_digest,
            "prompt_version": self.prompt_version,
            "request_sha256": self.request_sha256,
            "request_bytes": self.request_bytes,
            "cost_upper_bound_usd": self.cost_upper_bound_usd,
            "pricing_version": self.pricing_version,
            "model": self.model,
            "endpoint": self.endpoint,
            "versions": _thaw(self.versions),
            "consumed_versions": list(self.consumed_versions),
            "questions": [q.to_mapping() for q in self.questions],
            "question_ids": list(self.question_ids()),
            "supported_question_ids": list(self.supported_question_ids()),
            "unsupported_question_ids": list(self.unsupported_question_ids()),
            "plan_request": _thaw(self.plan_request),
        }


# --------------------------------------------------------------------------- #
# Input revalidation (pure; no I/O)
# --------------------------------------------------------------------------- #
def _ref(value: object, project_id: str) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise DecisionIntentValidationError(_M_BAD_REF)
    try:
        return validate_ref(dict(value), project_id)
    except Exception as exc:  # RecordValidationError / TypeError / ValueError
        raise DecisionIntentValidationError(_M_BAD_REF) from exc


def _per_question_refs(
    value: object, questions: Sequence[jev.QuestionDef], project_id: str
) -> dict[str, dict[str, str]]:
    """Resolve a per-question ref mapping, or a bare single ref for one question."""
    if isinstance(value, Mapping) and set(value) <= _REF_KEYS:
        if len(questions) != 1:
            raise DecisionIntentValidationError(_M_REF_SINGLE)
        return {questions[0].question_id: _ref(value, project_id)}
    if isinstance(value, Mapping):
        declared = {q.question_id for q in questions}
        if set(value) != declared:
            raise DecisionIntentValidationError(_M_REF_COVERAGE)
        return {qid: _ref(value[qid], project_id) for qid in declared}
    raise DecisionIntentValidationError(_M_BAD_REF)


def _assert_ref_in_task(ref: Mapping[str, str], task: RuntimeTaskRecord) -> None:
    """A consumed ref must be the task's target or one of its explicit inputs.

    No new scope: the intent may only reference revisions the task already pinned.
    """
    key = (ref["project_id"], ref["artifact_id"], ref["revision_id"])
    target = task.target
    if key == (target["project_id"], target["artifact_id"], target["revision_id"]):
        return
    for entry in task.input_refs:
        if key == (entry["project_id"], entry["artifact_id"], entry["revision_id"]):
            return
    raise DecisionIntentValidationError(_M_REF_SCOPE)


def _validate_versions(
    versions: object, consumed: object
) -> tuple[dict[str, Any], tuple[str, ...]]:
    """Validate a consumed Jev ``VersionSet`` and return ``(mapping, slots)``."""
    if isinstance(consumed, (str, bytes)) or not isinstance(consumed, Sequence):
        raise DecisionIntentValidationError(_M_VERSION_CONSUMED)
    extra = tuple(consumed)
    for name in extra:
        if name not in OPTIONAL_CONSUMED_SLOTS:
            raise DecisionIntentValidationError(_M_VERSION_CONSUMED)
    slots = tuple(dict.fromkeys(JEV_CONSUMED_SLOTS + extra))
    try:
        wrapper = PureJsonVersionSet(versions, consumed=slots)
    except Exception as exc:  # RecordValidationError
        raise DecisionIntentValidationError(_M_BAD_VERSIONS) from exc
    mapping = wrapper.as_mapping()
    if mapping.get("route") != INTENT_ROUTE:
        raise DecisionIntentValidationError(_M_VERSION_ROUTE)
    return mapping, tuple(sorted(slots))


def _revalidate_plan(plan: object) -> jev.DispatchPlan:
    """Re-derive and re-check the exact Jev plan (no I/O; fail closed).

    Bounds the plan through the adapter's own dispatch-time validator **first**
    (when exposed) and, in every path, through a length/depth/cycle-bounded
    :func:`canonical_json` of the raw request **before** any recursive thaw/walk, so
    a hostile/cyclic forged request is a static typed error rather than a raw
    ``RecursionError``. It then re-derives the wire body from the plan's own
    questions/state via :func:`review_article_agent.runtime.jev.plan_dispatch` and
    re-validates the reserve/bound/pricing. A forged/replaced plan (a mutated
    request body, a mismatched bound/reserve/server cap, or a question list that
    does not produce the recorded request) is refused.
    """
    if not isinstance(plan, jev.DispatchPlan):
        raise DecisionIntentValidationError(_M_BAD_PLAN)
    # (1) The adapter's own bounded dispatch-time revalidation, when exposed.
    adapter_revalidate = getattr(jev, "_revalidate_plan", None)
    if callable(adapter_revalidate):
        try:
            adapter_revalidate(plan)
        except Exception as exc:  # JevError / RecordValidationError / ...
            raise DecisionIntentValidationError(_M_BAD_PLAN) from exc
    # (2) Bound the raw request (length/depth/cycles) BEFORE any recursive walk.
    _bounded_request_mapping(plan.request)
    if not isinstance(plan.request, Mapping):
        raise DecisionIntentValidationError(_M_BAD_PLAN)
    try:
        state = _thaw(plan.request.get("state"))
    except Exception as exc:  # defensive: a hostile request must be a typed error
        raise DecisionIntentValidationError(_M_BAD_PLAN) from exc
    try:
        rebuilt = jev.plan_dispatch(
            plan.questions,
            state,
            reserve_usd=plan.reserve_usd,
            model=plan.model,
            endpoint=plan.endpoint,
            timeout=plan.timeout_seconds,
            server_cap=plan.server_cap_usd is not None,
        )
    except Exception as exc:  # JevError / TypeError / ...
        raise DecisionIntentValidationError(_M_BAD_PLAN) from exc
    if (
        bytes(rebuilt.request_body) != bytes(plan.request_body)
        or rebuilt.request_bytes != plan.request_bytes
        or rebuilt.request_sha256 != plan.request_sha256
        or rebuilt.cost_upper_bound_usd != plan.cost_upper_bound_usd
        or rebuilt.pricing_version != plan.pricing_version
        or rebuilt.input_usd_per_million_tokens != plan.input_usd_per_million_tokens
        or rebuilt.output_usd_per_million_tokens != plan.output_usd_per_million_tokens
        or rebuilt.reserve_usd != plan.reserve_usd
        or rebuilt.server_cap_usd != plan.server_cap_usd
        or tuple(rebuilt.supported_question_ids) != tuple(plan.supported_question_ids)
        or tuple(rebuilt.unsupported_question_ids) != tuple(plan.unsupported_question_ids)
        or rebuilt.endpoint != plan.endpoint
        or rebuilt.model != plan.model
    ):
        raise DecisionIntentValidationError(_M_BAD_PLAN)
    try:
        if canonical_json(plan.request) != bytes(plan.request_body):
            raise DecisionIntentValidationError(_M_BAD_PLAN)
    except DecisionIntentValidationError:
        raise
    except Exception as exc:  # RecordValidationError / ValueError / TypeError
        raise DecisionIntentValidationError(_M_BAD_PLAN) from exc
    return plan


# --------------------------------------------------------------------------- #
# Question representation (C02 §6.2 QuestionDef as a canonical JSON object)
# --------------------------------------------------------------------------- #
def _parse_question_def(
    question_id: str, raw: object
) -> tuple[str, str, tuple[str, ...], tuple[str, ...], dict[str, str]]:
    """Validate the reviewable QuestionDef JSON object and return its fields."""
    if not isinstance(raw, Mapping):
        raise DecisionIntentValidationError(_M_BAD_QUESTION_DEF)
    for key in raw:
        if not isinstance(key, str) or key not in _QUESTION_DEF_KEYS:
            raise DecisionIntentValidationError(_M_BAD_QUESTION_DEF)
    if "question_id" not in raw or "question_kind" not in raw or "instructions" not in raw:
        raise DecisionIntentValidationError(_M_BAD_QUESTION_DEF)
    if "criteria_ref" not in raw:
        raise DecisionIntentValidationError(_M_BAD_QUESTION_DEF)
    if raw["question_id"] != question_id:
        raise DecisionIntentValidationError(_M_BAD_QUESTION_DEF)
    kind = raw["question_kind"]
    if kind not in _QUESTION_KINDS:
        raise DecisionIntentValidationError(_M_BAD_QUESTION_DEF)
    instructions = raw["instructions"]
    if not isinstance(instructions, str) or instructions == "":
        raise DecisionIntentValidationError(_M_BAD_QUESTION_DEF)
    options_raw = raw.get("options")
    levels_raw = raw.get("levels")
    if kind == "choice":
        if "levels" in raw or not isinstance(options_raw, (list, tuple)):
            raise DecisionIntentValidationError(_M_BAD_QUESTION_DEF)
        options = tuple(options_raw)
    elif kind == "score":
        if "options" in raw or not isinstance(levels_raw, (list, tuple)):
            raise DecisionIntentValidationError(_M_BAD_QUESTION_DEF)
        levels = tuple(levels_raw)
    else:  # predicate / box
        if "options" in raw or "levels" in raw:
            raise DecisionIntentValidationError(_M_BAD_QUESTION_DEF)
        options, levels = (), ()
    if kind != "choice":
        options = ()
    if kind != "score":
        levels = ()
    # ``criteria_ref`` is a same-project ref; its shape is checked against the
    # supplied per-question criteria ref by the caller.
    criteria_ref_raw = raw["criteria_ref"]
    if not isinstance(criteria_ref_raw, Mapping) or set(criteria_ref_raw) != _REF_KEYS:
        raise DecisionIntentValidationError(_M_BAD_QUESTION_DEF)
    criteria_ref = {key: criteria_ref_raw[key] for key in _REF_KEYS}
    return kind, instructions, options, levels, criteria_ref  # type: ignore[return-value]


def _load_question(
    question_id: str,
    qdef_ref: Mapping[str, str],
    crit_ref: Mapping[str, str],
    *,
    reader,
) -> jev.QuestionDef:
    """Rebuild the typed :class:`jev.QuestionDef` from the two verified refs.

    Both referenced documents are length-bounded and re-validated as portable,
    depth-bounded JSON **before** any parse/walk.
    """
    qdef_bytes = reader(qdef_ref)
    qdef_raw = _bounded_json_bytes(
        qdef_bytes,
        max_bytes=MAX_QUESTION_DEF_BYTES,
        too_large=_M_REF_TOO_LARGE,
        malformed=_M_BAD_QUESTION_DEF,
    )
    kind, instructions, options, levels, embedded_crit_ref = _parse_question_def(
        question_id, qdef_raw
    )
    if dict(embedded_crit_ref) != dict(crit_ref):
        raise DecisionIntentValidationError(_M_BAD_QUESTION_DEF)
    crit_bytes = reader(crit_ref)
    criteria = _bounded_json_bytes(
        crit_bytes,
        max_bytes=MAX_CRITERIA_BYTES,
        too_large=_M_REF_TOO_LARGE,
        malformed=_M_BAD_CRITERIA,
    )
    try:
        return jev.QuestionDef(
            question_id=question_id,
            question_kind=kind,
            instructions=instructions,
            options=options,
            levels=levels,
            criteria=criteria,
        )
    except Exception as exc:  # JevValidationError / ...
        raise DecisionIntentValidationError(_M_BAD_CRITERIA) from exc


def _assert_question_matches(
    question_id: str, rebuilt: jev.QuestionDef, planned: jev.QuestionDef
) -> None:
    if planned.question_id != question_id:
        raise DecisionIntentValidationError(_M_QUESTION_ORDER)
    if (
        rebuilt.question_kind != planned.question_kind
        or rebuilt.instructions != planned.instructions
        or tuple(rebuilt.options) != tuple(planned.options)
        or tuple(rebuilt.levels) != tuple(planned.levels)
    ):
        raise DecisionIntentValidationError(_M_QUESTION_MISMATCH)
    try:
        rebuilt_criteria = canonical_json(rebuilt.criteria)
        planned_criteria = canonical_json(planned.criteria)
    except Exception as exc:  # RecordValidationError
        raise DecisionIntentValidationError(_M_QUESTION_MISMATCH) from exc
    if rebuilt_criteria != planned_criteria:
        raise DecisionIntentValidationError(_M_QUESTION_MISMATCH)
    if question_descriptor_digest(rebuilt) != question_descriptor_digest(planned):
        raise DecisionIntentValidationError(_M_QUESTION_MISMATCH)


# --------------------------------------------------------------------------- #
# Canonical intent mapping validation (structure only; no ref-byte reads)
# --------------------------------------------------------------------------- #
def _validate_intent_mapping(mapping: object) -> dict[str, Any]:
    """Structurally revalidate one canonical intent mapping (no I/O)."""
    if not isinstance(mapping, Mapping):
        raise DecisionIntentValidationError(_M_CANONICAL)
    for key in mapping:
        if not isinstance(key, str):
            raise DecisionIntentValidationError(_M_CANONICAL)
    if set(mapping) != _INTENT_KEYS:
        raise DecisionIntentValidationError(_M_CANONICAL)
    schema_version = mapping["schema_version"]
    if (
        isinstance(schema_version, bool)
        or not isinstance(schema_version, int)
        or schema_version != INTENT_SCHEMA_VERSION
    ):
        raise DecisionIntentValidationError(_M_CANONICAL)
    if mapping["route"] != INTENT_ROUTE:
        raise DecisionIntentValidationError(_M_ROUTE)
    intent_id = mapping["intent_id"]
    project_id = _identifier(mapping["project_id"])
    dispatch_id = _identifier(mapping["dispatch_id"])
    if not isinstance(intent_id, str) or intent_id != _intent_id(project_id, dispatch_id):
        raise DecisionIntentValidationError(_M_BAD_ID)
    _identifier(mapping["task_id"])
    for field in (
        "task_fingerprint",
        "input_digest",
        "version_digest",
        "bound_evidence_digest",
        "request_sha256",
    ):
        if not _is_hex_digest(mapping[field]):
            raise DecisionIntentValidationError(_M_DIGEST)
    _identifier(mapping["stage"])
    if _parse_exact_decimal(mapping["reserve_usd"], positive=True) is None:
        raise DecisionIntentValidationError(_M_DECIMAL)
    if _parse_exact_decimal(mapping["cost_upper_bound_usd"]) is None:
        raise DecisionIntentValidationError(_M_DECIMAL)
    request_bytes = mapping["request_bytes"]
    if (
        isinstance(request_bytes, bool)
        or not isinstance(request_bytes, int)
        or request_bytes < 1
        or request_bytes > jev.SAFE_MAX_REQUEST_BYTES
    ):
        raise DecisionIntentValidationError(_M_REQUEST_BINDING)
    if not _bounded_str(mapping["pricing_version"]) or mapping["pricing_version"] != jev.PRICING_VERSION:
        raise DecisionIntentValidationError(_M_CANONICAL)
    if not _bounded_str(mapping["prompt_version"]):
        raise DecisionIntentValidationError(_M_CANONICAL)
    if mapping["model"] != jev.MODEL or mapping["endpoint"] != jev.ENDPOINT:
        raise DecisionIntentValidationError(_M_CANONICAL)
    target = _ref(mapping["target"], project_id)

    # versions + bindings
    consumed = mapping["consumed_versions"]
    if (
        not isinstance(consumed, (list, tuple))
        or any(not isinstance(name, str) for name in consumed)
        or len(set(consumed)) != len(consumed)
    ):
        raise DecisionIntentValidationError(_M_VERSION_CONSUMED)
    for required in JEV_CONSUMED_SLOTS:
        if required not in consumed:
            raise DecisionIntentValidationError(_M_VERSION_CONSUMED)
    try:
        wrapper = PureJsonVersionSet(mapping["versions"], consumed=tuple(consumed))
    except Exception as exc:  # RecordValidationError
        raise DecisionIntentValidationError(_M_BAD_VERSIONS) from exc
    version_map = wrapper.as_mapping()
    if version_map.get("route") != INTENT_ROUTE:
        raise DecisionIntentValidationError(_M_VERSION_ROUTE)
    if version_map.get("input_digest") != mapping["input_digest"]:
        raise DecisionIntentValidationError(_M_VERSION_INPUT)
    if _sha256_hex(canonical_json(version_map)) != mapping["version_digest"]:
        raise DecisionIntentValidationError(_M_VERSION_HASH)

    # questions
    questions = mapping["questions"]
    if not isinstance(questions, (list, tuple)) or not questions:
        raise DecisionIntentValidationError(_M_CANONICAL)
    ids: list[str] = []
    supported: list[str] = []
    unsupported: list[str] = []
    seen: set[str] = set()
    for question in questions:
        if not isinstance(question, Mapping) or set(question) != _QUESTION_INTENT_KEYS:
            raise DecisionIntentValidationError(_M_CANONICAL)
        qid = _identifier(question["question_id"])
        if qid in seen:
            raise DecisionIntentValidationError(_M_DUP_QUESTION)
        seen.add(qid)
        kind = question["question_kind"]
        if kind not in _QUESTION_KINDS:
            raise DecisionIntentValidationError(_M_CANONICAL)
        _ref(question["question_def_ref"], project_id)
        _ref(question["criteria_ref"], project_id)
        if not _is_hex_digest(question["question_def_digest"]):
            raise DecisionIntentValidationError(_M_DIGEST)
        supported_flag = question["supported"]
        if supported_flag is not (kind != "box"):
            raise DecisionIntentValidationError(_M_CANONICAL)
        ids.append(qid)
        (supported if supported_flag else unsupported).append(qid)
    if list(mapping["question_ids"]) != ids:
        raise DecisionIntentValidationError(_M_QUESTION_ORDER)
    if list(mapping["supported_question_ids"]) != supported:
        raise DecisionIntentValidationError(_M_QUESTION_ORDER)
    if list(mapping["unsupported_question_ids"]) != unsupported:
        raise DecisionIntentValidationError(_M_QUESTION_ORDER)

    # plan request binding (recomputed from the recorded plan request, never trusted)
    plan_request = mapping["plan_request"]
    if not isinstance(plan_request, Mapping):
        raise DecisionIntentValidationError(_M_REQUEST_BINDING)
    try:
        body = canonical_json(_thaw(plan_request))
    except Exception as exc:  # RecordValidationError
        raise DecisionIntentValidationError(_M_REQUEST_BINDING) from exc
    if len(body) != request_bytes or _sha256_hex(body) != mapping["request_sha256"]:
        raise DecisionIntentValidationError(_M_REQUEST_BINDING)

    return _thaw(mapping)


# --------------------------------------------------------------------------- #
# Repository
# --------------------------------------------------------------------------- #
class DecisionIntentRepository:
    """Durable, atomic pre-dispatch request-intent recording over a verified store.

    Typical use::

        store = ProjectStore.open_at(app_dir, expected_project_id=pid)
        intents = DecisionIntentRepository(store)

        record = intents.record_intent(
            task, dispatch, plan,
            question_def_ref={qid: qdef_ref, ...},
            criteria_ref={qid: crit_ref, ...},
            versions=version_set,
            consumed=("rubric", "parser"),
        )
        # -> one immutable intent binding the exact question refs/versions/request.
    """

    def __init__(
        self,
        store,
        *,
        runtime: RuntimeRepository | None = None,
        dispatch: RuntimeDispatchRepository | None = None,
        artifacts: ArtifactRepository | None = None,
    ) -> None:
        self._store = store
        self._runtime = runtime if runtime is not None else RuntimeRepository(store)
        self._dispatch = (
            dispatch if dispatch is not None else RuntimeDispatchRepository(store)
        )
        self._artifacts = (
            artifacts if artifacts is not None else ArtifactRepository(store)
        )

    @property
    def project_id(self) -> str:
        """The store's own project id; the repository never uses a caller project."""
        return self._store.project_id

    def _conn(self) -> sqlite3.Connection:
        return self._store.connection

    # -- record (atomic: validate task + dispatch + plan + refs + insert) ----- #
    def record_intent(
        self,
        task: RuntimeTaskRecord,
        dispatch: DispatchRecord,
        plan: jev.DispatchPlan,
        *,
        question_def_ref: object,
        criteria_ref: object,
        versions: object,
        consumed: Sequence[str] = (),
        created_at_utc: str | None = None,
    ) -> DecisionIntentRecord:
        """Record one immutable intent for a reserved direct Jev dispatch.

        A **new** intent is recorded only for a persisted ``direct_call`` dispatch
        in the ``reserved`` state (before submission). The plan is revalidated, the
        consumed ``VersionSet`` is bound to the dispatch's ``input_digest``/
        ``version_digest`` and the task's ``prompt_version``, the plan reserve is
        bound to the dispatch reserve, and every per-question ``question_def_ref``/
        ``criteria_ref`` is resolved through the artifact repository (actual stored
        bytes re-hashed/re-sized and rights re-validated), rebuilt into a typed
        ``QuestionDef``, and compared with the plan's questions exactly.

        An **identical** replay (same dispatch, same task/plan/refs/versions) returns
        the validated historical row — even after the dispatch has been submitted
        and the current heads have moved; a **divergent** replay is refused with
        :class:`DecisionIntentConflictError`. A refused call writes nothing.
        """
        if not isinstance(task, RuntimeTaskRecord):
            raise DecisionIntentValidationError(_M_BAD_TASK)
        if not isinstance(dispatch, DispatchRecord):
            raise DecisionIntentValidationError(_M_BAD_DISPATCH)
        project_id = _identifier(self.project_id)
        if task.project_id != project_id or dispatch.project_id != project_id:
            raise DecisionIntentValidationError(_M_BAD_PROJECT)
        if dispatch.route != INTENT_ROUTE:
            raise DecisionIntentValidationError(_M_ROUTE)
        _revalidate_plan(plan)
        version_map, consumed_slots = _validate_versions(versions, consumed)
        created = (
            _utc_now()
            if created_at_utc is None
            else _require_utc_timestamp(created_at_utc, "created_at_utc")
        )
        qdef_refs = _per_question_refs(question_def_ref, plan.questions, project_id)
        crit_refs = _per_question_refs(criteria_ref, plan.questions, project_id)

        with self._store.transaction():
            persisted_task = self._require_persisted_task(task)
            persisted_dispatch = self._require_persisted_dispatch(dispatch)
            # The supplied task must be exactly the task the dispatch pins: a
            # second persisted task with identical canonical bytes (a different
            # request key, hence a different task id) must never be recorded
            # against this dispatch.
            if (
                task.task_id != dispatch.task_id
                or persisted_task.task_id != persisted_dispatch.task_id
                or persisted_task.fingerprint != persisted_dispatch.task_fingerprint
            ):
                raise DecisionIntentValidationError(_M_TASK_DISPATCH)
            # The consumed refs must be within the task's own pinned scope.
            _assert_ref_in_task(persisted_task.target, persisted_task)
            for qid in qdef_refs:
                _assert_ref_in_task(qdef_refs[qid], persisted_task)
                _assert_ref_in_task(crit_refs[qid], persisted_task)

            existing = self._intent_row(persisted_dispatch.dispatch_id)
            if existing is not None:
                stored = self._validate_intent_row(existing)
                fresh = self._build_intent(
                    project_id,
                    persisted_task,
                    persisted_dispatch,
                    plan,
                    version_map,
                    consumed_slots,
                    qdef_refs,
                    crit_refs,
                    require_current=False,
                )
                if fresh["canonical"] != stored.canonical:
                    raise DecisionIntentConflictError(_M_DIVERGENT)
                return stored

            if persisted_dispatch.state != "reserved":
                raise DecisionIntentStateError(_M_STATE)
            built = self._build_intent(
                project_id,
                persisted_task,
                persisted_dispatch,
                plan,
                version_map,
                consumed_slots,
                qdef_refs,
                crit_refs,
                require_current=True,
            )
            self._insert_intent(built, created)
        stored = self.get_intent(persisted_dispatch.dispatch_id)
        assert stored is not None  # just committed
        return stored

    # -- reads --------------------------------------------------------------- #
    def get_intent(self, dispatch_id: str) -> DecisionIntentRecord | None:
        dispatch_id = validate_identifier(dispatch_id, "dispatch_id")
        row = self._intent_row(dispatch_id)
        if row is None:
            return None
        return self._validate_intent_row(row)

    def get_intent_for_task(self, task_id: str) -> DecisionIntentRecord | None:
        task_id = validate_identifier(task_id, "task_id")
        row = self._conn().execute(
            "SELECT * FROM decision_request_intent WHERE project_id = ? AND task_id = ?",
            (self.project_id, task_id),
        ).fetchone()
        if row is None:
            return None
        return self._validate_intent_row(row)

    def list_intents(self) -> list[DecisionIntentRecord]:
        rows = self._conn().execute(
            "SELECT * FROM decision_request_intent WHERE project_id = ? ORDER BY rowid",
            (self.project_id,),
        ).fetchall()
        return [self._validate_intent_row(row) for row in rows]

    def verify_intent(self, dispatch_id: str) -> bool:
        """``True`` iff the intent exists and revalidates (a corrupt row is ``False``)."""
        try:
            return self.get_intent(dispatch_id) is not None
        except DecisionIntentCorruptionError:
            return False

    # -- internal: persisted links ------------------------------------------- #
    def _require_persisted_task(self, task: RuntimeTaskRecord) -> RuntimeTaskRecord:
        try:
            stored = self._runtime.get_task(task.task_id)
        except RuntimeCorruptionError as exc:
            raise DecisionIntentCorruptionError(_M_BAD_TASK) from exc
        if stored is None:
            raise DecisionIntentValidationError(_M_BAD_TASK)
        if (
            stored.project_id != self.project_id
            or stored.task_id != task.task_id
            or stored.fingerprint != task.fingerprint
        ):
            raise DecisionIntentValidationError(_M_BAD_TASK)
        return stored

    def _require_persisted_dispatch(self, dispatch: DispatchRecord) -> DispatchRecord:
        try:
            stored = self._dispatch.get_dispatch(dispatch.dispatch_id)
        except DispatchCorruptionError as exc:
            raise DecisionIntentCorruptionError(_M_BAD_DISPATCH) from exc
        if stored is None:
            raise DecisionIntentValidationError(_M_BAD_DISPATCH)
        if (
            stored.project_id != self.project_id
            or stored.dispatch_id != dispatch.dispatch_id
            or stored.identity_tuple() != dispatch.identity_tuple()
        ):
            raise DecisionIntentValidationError(_M_BAD_DISPATCH)
        return stored

    # -- internal: ref byte/rights verification ------------------------------ #
    def _read_verified_ref(
        self, ref: Mapping[str, str], *, require_current: bool
    ) -> bytes:
        """Resolve a ref and recompute its **actual** stored bytes (not metadata)."""
        revision = self._artifacts.get_revision(ref["artifact_id"], ref["revision_id"])
        if revision is None:
            raise DecisionIntentValidationError(_M_REF_MISSING)
        if revision.project_id != self.project_id:
            raise DecisionIntentValidationError(_M_REF_MISSING)
        content = self._artifacts.read_content(ref["artifact_id"], ref["revision_id"])
        if (
            len(content) != revision.byte_size
            or _sha256_hex(content) != _bare_hex(revision.content_sha256, "content_sha256")
        ):
            raise DecisionIntentValidationError(_M_REF_INTEGRITY)
        self._verify_pinned_rights(revision, require_current=require_current)
        if require_current:
            current = self._artifacts.current_revision(ref["artifact_id"])
            if current is None or current.revision_id != ref["revision_id"]:
                raise DecisionIntentValidationError(_M_REF_STALE)
        return content

    def _verify_pinned_rights(self, revision, *, require_current: bool) -> None:
        """Verify a revision's pinned rights canonical identity/subject (+storage)."""
        ref = revision.rights_ref
        row = self._conn().execute(
            "SELECT canonical, content_sha256 FROM rights_record WHERE project_id = ? "
            "AND rights_id = ? AND record_revision = ?",
            (self.project_id, ref.rights_id, ref.record_revision),
        ).fetchone()
        if row is None:
            raise DecisionIntentValidationError(_M_RIGHTS)
        canonical = bytes(row["canonical"])
        if _sha256_hex(canonical) != _bare_hex(row["content_sha256"], "content_sha256"):
            raise DecisionIntentValidationError(_M_RIGHTS)
        try:
            record = json.loads(canonical.decode("utf-8"))
        except (ValueError, UnicodeError) as exc:
            raise DecisionIntentValidationError(_M_RIGHTS) from exc
        if not isinstance(record, dict):
            raise DecisionIntentValidationError(_M_RIGHTS)
        if (
            record.get("project_id") != self.project_id
            or record.get("rights_id") != ref.rights_id
            or record.get("record_revision") != ref.record_revision
        ):
            raise DecisionIntentValidationError(_M_RIGHTS)
        subject = record.get("subject")
        if not (
            isinstance(subject, dict)
            and subject.get("kind") == "source_revision"
            and subject.get("project_id") == revision.project_id
            and subject.get("artifact_id") == revision.artifact_id
            and subject.get("revision_id") == revision.revision_id
        ):
            raise DecisionIntentValidationError(_M_RIGHTS)
        try:
            validated = validate_rights_record(record, self.project_id)
            require_storage_allowed(validated)
        except Exception as exc:  # RecordValidationError
            raise DecisionIntentValidationError(_M_RIGHTS) from exc
        if require_current:
            newest = self._conn().execute(
                "SELECT MAX(record_revision) AS newest FROM rights_record "
                "WHERE project_id = ? AND rights_id = ?",
                (self.project_id, ref.rights_id),
            ).fetchone()["newest"]
            if newest is not None and ref.record_revision < newest:
                raise DecisionIntentValidationError(_M_RIGHTS)

    # -- internal: build the canonical intent -------------------------------- #
    def _build_intent(
        self,
        project_id: str,
        task: RuntimeTaskRecord,
        dispatch: DispatchRecord,
        plan: jev.DispatchPlan,
        version_map: dict[str, Any],
        consumed_slots: tuple[str, ...],
        qdef_refs: dict[str, dict[str, str]],
        crit_refs: dict[str, dict[str, str]],
        *,
        require_current: bool,
    ) -> dict[str, Any]:
        """Assemble and validate the canonical intent (reads the consumed refs)."""
        if version_map["input_digest"] != dispatch.input_digest:
            raise DecisionIntentValidationError(_M_VERSION_INPUT)
        if _sha256_hex(canonical_json(version_map)) != dispatch.version_digest:
            raise DecisionIntentValidationError(_M_VERSION_HASH)
        prompt_slot = version_map["prompt"]
        if (
            prompt_slot.get("state") != "known"
            or prompt_slot.get("ref", {}).get("version") != task.prompt_version
        ):
            raise DecisionIntentValidationError(_M_PROMPT_VERSION)
        if plan.reserve_usd != dispatch.reserve_usd:
            raise DecisionIntentValidationError(_M_RESERVE)

        target = {
            "project_id": project_id,
            "artifact_id": task.target["artifact_id"],
            "revision_id": task.target["revision_id"],
        }
        # The target is a consumed ref too: verify its bytes (and, for a new
        # intent, that it is still the current revision).
        self._read_verified_ref(target, require_current=require_current)

        def reader(ref: Mapping[str, str]) -> bytes:
            return self._read_verified_ref(ref, require_current=require_current)

        questions: list[QuestionIntent] = []
        for planned in plan.questions:
            qid = planned.question_id
            if qid not in qdef_refs or qid not in crit_refs:
                raise DecisionIntentValidationError(_M_REF_COVERAGE)
            rebuilt = _load_question(qid, qdef_refs[qid], crit_refs[qid], reader=reader)
            _assert_question_matches(qid, rebuilt, planned)
            questions.append(
                QuestionIntent(
                    question_id=qid,
                    question_kind=planned.question_kind,
                    question_def_ref=dict(qdef_refs[qid]),
                    criteria_ref=dict(crit_refs[qid]),
                    question_def_digest=question_descriptor_digest(rebuilt),
                    supported=planned.question_kind != "box",
                )
            )

        plan_request = _thaw(plan.request)
        mapping: dict[str, Any] = {
            "schema_version": INTENT_SCHEMA_VERSION,
            "intent_id": _intent_id(project_id, dispatch.dispatch_id),
            "route": INTENT_ROUTE,
            "project_id": project_id,
            "dispatch_id": dispatch.dispatch_id,
            "task_id": task.task_id,
            "task_fingerprint": task.fingerprint,
            "stage": dispatch.stage,
            "target": target,
            "input_digest": dispatch.input_digest,
            "version_digest": dispatch.version_digest,
            "reserve_usd": dispatch.reserve_usd,
            "bound_evidence_digest": dispatch.bound_evidence_digest,
            "prompt_version": task.prompt_version,
            "request_sha256": plan.request_sha256,
            "request_bytes": plan.request_bytes,
            "cost_upper_bound_usd": plan.cost_upper_bound_usd,
            "pricing_version": plan.pricing_version,
            "model": plan.model,
            "endpoint": plan.endpoint,
            "versions": version_map,
            "consumed_versions": list(consumed_slots),
            "questions": [q.to_mapping() for q in questions],
            "question_ids": [q.question_id for q in questions],
            "supported_question_ids": [q.question_id for q in questions if q.supported],
            "unsupported_question_ids": [
                q.question_id for q in questions if not q.supported
            ],
            "plan_request": plan_request,
        }
        normalized = _validate_intent_mapping(mapping)
        canonical = canonical_json(normalized)
        if len(canonical) > MAX_INTENT_BYTES:
            raise DecisionIntentValidationError(_M_RECORD_SIZE)
        return {"mapping": normalized, "canonical": canonical}

    def _insert_intent(self, built: dict[str, Any], created: str) -> None:
        mapping = built["mapping"]
        canonical = built["canonical"]
        content_digest = _sha256_hex(canonical)
        self._conn().execute(
            "INSERT INTO decision_request_intent "
            "(project_id, dispatch_id, intent_id, schema_version, task_id, "
            " task_fingerprint, route, stage, input_digest, version_digest, "
            " reserve_usd, bound_evidence_digest, request_sha256, request_bytes, "
            " cost_upper_bound_usd, pricing_version, prompt_version, "
            " target_artifact_id, target_revision_id, question_count, canonical, "
            " content_digest, created_at_utc) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                self.project_id,
                mapping["dispatch_id"],
                mapping["intent_id"],
                mapping["schema_version"],
                mapping["task_id"],
                mapping["task_fingerprint"],
                mapping["route"],
                mapping["stage"],
                mapping["input_digest"],
                mapping["version_digest"],
                mapping["reserve_usd"],
                mapping["bound_evidence_digest"],
                mapping["request_sha256"],
                mapping["request_bytes"],
                mapping["cost_upper_bound_usd"],
                mapping["pricing_version"],
                mapping["prompt_version"],
                mapping["target"]["artifact_id"],
                mapping["target"]["revision_id"],
                len(mapping["questions"]),
                canonical,
                content_digest,
                created,
            ),
        )

    # -- internal: row access + validation ----------------------------------- #
    def _intent_row(self, dispatch_id: str) -> sqlite3.Row | None:
        return self._conn().execute(
            "SELECT * FROM decision_request_intent WHERE project_id = ? AND "
            "dispatch_id = ?",
            (self.project_id, dispatch_id),
        ).fetchone()

    @_fail_closed_row("decision request intent")
    def _validate_intent_row(self, row: sqlite3.Row) -> DecisionIntentRecord:
        """Structurally revalidate a stored intent; fail closed.

        Re-derives **actual independent pins**: the canonical project, the parent
        dispatch and its pinned task (all identity fields), the recorded target and
        per-question refs against the task's own historical pins, the recorded
        question descriptors against the referenced definitions, and the exact
        request/plan binding — never trusting the row's own columns or a stored
        hash. History-preserving: it never requires the refs to be the *current*
        heads.
        """
        if row["project_id"] != self.project_id:
            raise DecisionIntentCorruptionError("decision request intent: foreign project row.")
        dispatch_id = _identifier(row["dispatch_id"])
        if row["intent_id"] != _intent_id(self.project_id, dispatch_id):
            raise DecisionIntentCorruptionError("decision request intent: id is not dispatch-scoped.")
        canonical = bytes(row["canonical"])
        if len(canonical) > MAX_INTENT_BYTES:
            raise DecisionIntentCorruptionError(_M_CANONICAL_SIZE)
        if _sha256_hex(canonical) != _bare_hex(row["content_digest"], "content_digest"):
            raise DecisionIntentCorruptionError("decision request intent: content digest mismatch.")
        parsed = _bounded_json_bytes(
            canonical,
            max_bytes=MAX_INTENT_BYTES,
            too_large=_M_CANONICAL_SIZE,
            malformed=_M_CANONICAL,
        )
        mapping = _validate_intent_mapping(parsed)
        if mapping["project_id"] != self.project_id:
            raise DecisionIntentCorruptionError(_M_PROJECT)
        # The stored bytes must be exactly the canonical form of the parsed value
        # (no non-canonical spelling, reordered keys, or extra bytes).
        if canonical_json(parsed) != canonical:
            raise DecisionIntentCorruptionError(_M_CANONICAL_ROUNDTRIP)
        self._assert_columns_match(row, mapping)
        self._assert_parent_links(mapping)
        self._verify_recorded_refs(mapping)
        return self._row_to_record(row, mapping, canonical)

    def _assert_parent_links(self, mapping: Mapping[str, Any]) -> None:
        """Load and compare the actual parent dispatch and its pinned task."""
        dispatch_id = mapping["dispatch_id"]
        try:
            dispatch = self._dispatch.get_dispatch(dispatch_id)
        except DispatchCorruptionError as exc:
            raise DecisionIntentCorruptionError(_M_PARENT) from exc
        if dispatch is None or dispatch.project_id != self.project_id:
            raise DecisionIntentCorruptionError(_M_PARENT)
        if (
            dispatch.dispatch_id != dispatch_id
            or dispatch.task_id != mapping["task_id"]
            or dispatch.task_fingerprint != mapping["task_fingerprint"]
            or dispatch.route != mapping["route"]
            or dispatch.stage != mapping["stage"]
            or dispatch.input_digest != mapping["input_digest"]
            or dispatch.version_digest != mapping["version_digest"]
            or dispatch.reserve_usd != mapping["reserve_usd"]
            or dispatch.bound_evidence_digest != mapping["bound_evidence_digest"]
        ):
            raise DecisionIntentCorruptionError(_M_PARENT)
        try:
            task = self._runtime.get_task(dispatch.task_id)
        except RuntimeCorruptionError as exc:
            raise DecisionIntentCorruptionError(_M_PARENT) from exc
        if (
            task is None
            or task.project_id != self.project_id
            or task.task_id != mapping["task_id"]
            or task.fingerprint != mapping["task_fingerprint"]
            or task.prompt_version != mapping["prompt_version"]
        ):
            raise DecisionIntentCorruptionError(_M_PARENT)
        target = _ref(mapping["target"], self.project_id)
        pinned_target = {
            "project_id": task.target["project_id"],
            "artifact_id": task.target["artifact_id"],
            "revision_id": task.target["revision_id"],
        }
        if target != pinned_target:
            raise DecisionIntentCorruptionError(_M_PARENT)
        for question in mapping["questions"]:
            _assert_ref_in_task(_ref(question["question_def_ref"], self.project_id), task)
            _assert_ref_in_task(_ref(question["criteria_ref"], self.project_id), task)

    def _assert_columns_match(self, row: sqlite3.Row, mapping: Mapping[str, Any]) -> None:
        expected = {
            "dispatch_id": mapping["dispatch_id"],
            "intent_id": mapping["intent_id"],
            "schema_version": mapping["schema_version"],
            "task_id": mapping["task_id"],
            "task_fingerprint": mapping["task_fingerprint"],
            "route": mapping["route"],
            "stage": mapping["stage"],
            "input_digest": mapping["input_digest"],
            "version_digest": mapping["version_digest"],
            "reserve_usd": mapping["reserve_usd"],
            "bound_evidence_digest": mapping["bound_evidence_digest"],
            "request_sha256": mapping["request_sha256"],
            "request_bytes": mapping["request_bytes"],
            "cost_upper_bound_usd": mapping["cost_upper_bound_usd"],
            "pricing_version": mapping["pricing_version"],
            "prompt_version": mapping["prompt_version"],
            "target_artifact_id": mapping["target"]["artifact_id"],
            "target_revision_id": mapping["target"]["revision_id"],
            "question_count": len(mapping["questions"]),
        }
        for column, value in expected.items():
            if row[column] != value:
                raise DecisionIntentCorruptionError(_M_COLUMNS)

    def _verify_recorded_refs(self, mapping: Mapping[str, Any]) -> None:
        """Recompute the recorded ref bytes and re-derive the recorded question/plan.

        History-preserving: it never requires the refs to be the *current* heads,
        only that the recorded revisions still exist with the recorded bytes, that
        they rebuild the recorded descriptor digests (matching question kind), and
        that the recorded request wire is exactly the request those recorded
        questions would send.
        """
        target = _ref(mapping["target"], self.project_id)
        self._read_verified_ref(target, require_current=False)

        def reader(ref: Mapping[str, str]) -> bytes:
            return self._read_verified_ref(ref, require_current=False)

        rebuilt_questions: list[jev.QuestionDef] = []
        for question in mapping["questions"]:
            rebuilt = _load_question(
                question["question_id"],
                question["question_def_ref"],
                question["criteria_ref"],
                reader=reader,
            )
            if rebuilt.question_kind != question["question_kind"]:
                raise DecisionIntentCorruptionError(_M_QUESTION_MISMATCH)
            if question_descriptor_digest(rebuilt) != question["question_def_digest"]:
                raise DecisionIntentCorruptionError(_M_DESCRIPTOR)
            rebuilt_questions.append(rebuilt)

        # Reconstruct the exact Jev plan from the recorded request state/server cap
        # and the recorded questions, then revalidate the recorded request binding.
        plan_request = _thaw(mapping["plan_request"])
        if not isinstance(plan_request, Mapping):
            raise DecisionIntentCorruptionError(_M_REQUEST_BINDING)
        try:
            rebuilt_plan = jev.plan_dispatch(
                rebuilt_questions,
                plan_request.get("state"),
                reserve_usd=mapping["reserve_usd"],
                model=mapping["model"],
                endpoint=mapping["endpoint"],
                server_cap="provider" in plan_request,
            )
        except Exception as exc:  # JevError / ...
            raise DecisionIntentCorruptionError(_M_REQUEST_BINDING) from exc
        if (
            canonical_json(plan_request) != bytes(rebuilt_plan.request_body)
            or rebuilt_plan.request_sha256 != mapping["request_sha256"]
            or rebuilt_plan.request_bytes != mapping["request_bytes"]
            or rebuilt_plan.cost_upper_bound_usd != mapping["cost_upper_bound_usd"]
            or rebuilt_plan.pricing_version != mapping["pricing_version"]
            or rebuilt_plan.reserve_usd != mapping["reserve_usd"]
            or tuple(rebuilt_plan.supported_question_ids)
            != tuple(mapping["supported_question_ids"])
            or tuple(rebuilt_plan.unsupported_question_ids)
            != tuple(mapping["unsupported_question_ids"])
            or rebuilt_plan.endpoint != mapping["endpoint"]
            or rebuilt_plan.model != mapping["model"]
        ):
            raise DecisionIntentCorruptionError(_M_REQUEST_BINDING)

    def _row_to_record(
        self, row: sqlite3.Row, mapping: Mapping[str, Any], canonical: bytes
    ) -> DecisionIntentRecord:
        questions = tuple(
            QuestionIntent(
                question_id=q["question_id"],
                question_kind=q["question_kind"],
                question_def_ref=dict(q["question_def_ref"]),
                criteria_ref=dict(q["criteria_ref"]),
                question_def_digest=q["question_def_digest"],
                supported=q["supported"],
            )
            for q in mapping["questions"]
        )
        _require_utc_timestamp(row["created_at_utc"], "created_at_utc")
        return DecisionIntentRecord(
            project_id=self.project_id,
            intent_id=mapping["intent_id"],
            schema_version=mapping["schema_version"],
            dispatch_id=mapping["dispatch_id"],
            task_id=mapping["task_id"],
            task_fingerprint=mapping["task_fingerprint"],
            route=mapping["route"],
            stage=mapping["stage"],
            input_digest=mapping["input_digest"],
            version_digest=mapping["version_digest"],
            reserve_usd=mapping["reserve_usd"],
            bound_evidence_digest=mapping["bound_evidence_digest"],
            request_sha256=mapping["request_sha256"],
            request_bytes=mapping["request_bytes"],
            cost_upper_bound_usd=mapping["cost_upper_bound_usd"],
            pricing_version=mapping["pricing_version"],
            prompt_version=mapping["prompt_version"],
            target=dict(mapping["target"]),
            questions=questions,
            versions=_deep_freeze(mapping["versions"]),
            consumed_versions=tuple(mapping["consumed_versions"]),
            model=mapping["model"],
            endpoint=mapping["endpoint"],
            plan_request=_deep_freeze(mapping["plan_request"]),
            content_digest=row["content_digest"],
            created_at_utc=row["created_at_utc"],
            canonical=canonical,
        )

    # -- whole-ledger integrity (shared with a later backup verifier) --------- #
    def verify_all_integrity(self) -> list[str]:
        """Return a list of integrity problems across every intent (empty = ok).

        Exported so a later backup verifier can reuse this instead of duplicating
        the structural/hash/ref/version revalidation. Each entry is a short, static,
        non-secret description.
        """
        return self._integrity_problems()

    def _integrity_problems(self) -> list[str]:
        problems: list[str] = []
        rows = self._conn().execute(
            "SELECT * FROM decision_request_intent ORDER BY rowid"
        ).fetchall()
        for index, row in enumerate(rows):
            if row["project_id"] != self.project_id:
                problems.append(
                    f"decision_request_intent: row {index} is a foreign project row"
                )
                continue
            if not self._link_exists("runtime_dispatch", "dispatch_id", row["dispatch_id"]):
                problems.append(
                    f"decision_request_intent: row {index} is a dangling intent "
                    f"(missing dispatch)"
                )
                continue
            if not self._link_exists("runtime_bounded_task", "task_id", row["task_id"]):
                problems.append(
                    f"decision_request_intent: row {index} is a dangling intent "
                    f"(missing task)"
                )
                continue
            try:
                self._validate_intent_row(row)
            except Exception:  # noqa: BLE001 - a corrupt row must not abort the sweep
                problems.append(
                    f"decision_request_intent: row {index} failed integrity revalidation"
                )
        return problems

    def _link_exists(self, table: str, key: str, value: object) -> bool:
        row = self._conn().execute(
            f"SELECT 1 FROM {table} WHERE project_id = ? AND {key} = ?",
            (self.project_id, value),
        ).fetchone()
        return row is not None


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


def decision_intent_integrity_problems(
    conn: sqlite3.Connection, project_id: str
) -> list[str]:
    """Revalidate every recorded intent for ``project_id`` on ``conn`` (read-only).

    Reusable by a later backup verifier. Returns a list of static problem
    descriptions (empty when every intent is structurally intact, internally
    consistent, and its recorded refs/versions/request binding revalidate).
    Foreign, dangling/orphan, and corrupt rows are all reported. The connection is
    never written to.
    """
    repo = DecisionIntentRepository(_ReadOnlyIntentStoreView(conn, project_id))
    return repo.verify_all_integrity()
