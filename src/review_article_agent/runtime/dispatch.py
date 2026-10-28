"""Durable, atomic runtime **dispatch** and the route-separated cost ledger.

This module is the Step 04 (task T5b) storage-side core that sits on top of an
already-verified :class:`~review_article_agent.storage.store.ProjectStore` and a
persisted :class:`~review_article_agent.storage.agent_runtime.RuntimeTaskRecord`.
It records, in the **existing project store**, one durable *logical-call* row per
dispatch and its operational state — the reserve-before-dispatch reservation that
the future runner consults *before* it issues any transport.

It is the ledger half of "reserve before dispatch": a dispatch is admitted only
after its defensible upper-bound reserve is durably recorded, and the cumulative
project/stage caps count **every** route and **both** cost buckets (C02
§5.6/§6.3/§8).

What this deliberately is **not**
--------------------------------

* It is **not** a model, network, transport, or runner. No method issues a call;
  :meth:`RuntimeDispatchRepository.mark_submitted` only records that a future
  dispatch *may* now be issued. A recorded row grants **no** authority, **no**
  network access, and **no** right to spend: authorization/rights are a separate
  concern and are not claimed here.
* It does **not** validate that a *paid bound* is true. The reserve is the
  caller's declared upper bound; whether the bound is scientifically/cost
  defensible is validated by the future runner, never implied by this API.
* It makes **no** exactly-once *external* execution or billing claim. Fencing
  protects the single **accepted local result** (C02 §8.2); it does not fence
  arbitrary external side effects that already happened.
* It is **not** the worker, the CLI, the IPC layer, or the approval/decision
  layer. Those are separate tasks.

What it guarantees
------------------

* **Immutable identity + idempotent reserve.** The logical-call identity row
  (schema migration 6) pins the persisted task id and its fingerprint, the
  ``request_id`` logical-call key, the ``opencode_session``/``direct_call`` route
  and its implied ``session_statistics``/``direct_call`` accounting bucket, the
  stage, the canonical input/version digests, the exact-decimal reserve, and the
  immutable bound-evidence digest. A repeated ``request_id`` with the **same**
  identity returns the historical row and **never** double-reserves; a
  *divergent* identity under the same key raises
  :class:`DispatchConflictError`.
* **One transaction, no partial dispatch.** The reserve validates the existing
  runtime task, checks the cumulative project/stage caps, and writes both the
  identity and its initial ``reserved`` state inside a single ``BEGIN IMMEDIATE``,
  so a refused (over-cap or invalid) reserve leaves no row and no reserve behind.
* **Shared cumulative caps, both directions.** :func:`runtime_reserve_exposure`
  is a pure read-only helper summing this ledger's conservative exposure; the
  existing :class:`~review_article_agent.runtime.jobs.JobRepository` counts it, so
  a new **synthetic** reserve is refused when runtime reserves have consumed the
  cap, and this repository reads ``JobRepository.budget_usage`` so a new
  **runtime** reserve is refused when synthetic reserves have consumed it. No
  recursive budget loop is involved.
* **Explicit safe states, no blind retry.** A dispatch is one of ``reserved``,
  ``submitted``, ``admitted`` (``opencode_session`` only), ``completed``,
  ``failed``, ``unknown``. ``submitted`` is recorded **before** any future I/O and
  is **single-use** — only the caller that won the send claim may proceed, and a
  second caller is refused rather than handed a record it did not win. On a
  restart an outstanding ``submitted`` becomes ``unknown`` (never reset, never
  retried, never reclassified as a definitive failure) while an existing
  ``opencode_session`` admission is retained for same-identity reconciliation.
  The direct route never carries a durable admission. Initial scope allows **at
  most one logical dispatch per task**: a different ``request_id`` for the same
  task is refused in every state (including ``completed``/``failed``); a retry
  would be a separate, explicitly authorized future task.
* **Monotonic cost settlement.** Once a cost is submitted the dispatch can never
  claim ``not_incurred``, and an established ``known``/``estimated`` amount can
  never be discarded or lowered, so an overrun is never erased by a later weaker
  settlement (C02 §5.6/§6.3).
* **One fenced accepted local result.** :meth:`RuntimeDispatchRepository.accept_result`
  requires the dispatch's current fence; an identical replay is idempotent and a
  divergent replay is refused.
* **Route cost separation.** The accounting bucket is *derived* from the route
  and pinned at the database (``direct_call`` ⇔ ``direct_call``), so a direct-call
  cost can never be aggregated into OpenCode session statistics.
* **Fail-closed stored rows.** Every read/replay path revalidates the stored row
  (identity, digests, exact-decimal amounts, state/result/cost consistency,
  operational consistency — admitted ⇔ durable admission, direct route never
  admits, in-flight never ``not_incurred``, reserved/terminal timestamp shape,
  the single initial fence — and the linked task revalidated in full) and raises
  :class:`DispatchCorruptionError` on any mismatch.
  :func:`runtime_dispatch_integrity_problems` exports the same revalidation plus
  an operational-row sweep so a later backup verifier can reuse it (foreign,
  dangling/orphan, or corrupt rows are reported). :func:`runtime_reserve_exposure`
  likewise fails closed on a missing/malformed operational row instead of
  quietly counting the bare reserve.

No credential, endpoint, or response content is ever stored (spec lines 62-63;
``docs/storage.md``).
"""

from __future__ import annotations

import functools
import hashlib
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from ..domain.errors import StoreError
from ..domain.records import validate_identifier
from ..storage.agent_runtime import (
    RuntimeCorruptionError,
    RuntimeRepository,
    RuntimeTaskRecord,
    RuntimeValidationError,
)
from .jobs import (
    BudgetExceededError,
    BudgetUsage,
    COST_STATES,
    JobRepository,
    JobValidationError,
    _add_exact,
    _bounded_decimal_amount,
    _counted_exposure,
    _exact_decimal,
)

__all__ = [
    "ACCOUNTING_BUCKETS",
    "DISPATCH_GRANTS_AUTHORITY",
    "DISPATCH_PERFORMS_NETWORK_IO",
    "DISPATCH_ROUTES",
    "DISPATCH_STATES",
    "ROUTE_BUCKET",
    "RUNTIME_REMOTE_STATES",
    "DispatchConflictError",
    "DispatchCorruptionError",
    "DispatchError",
    "DispatchRecord",
    "DispatchStaleFenceError",
    "DispatchStateError",
    "DispatchValidationError",
    "RuntimeDispatchRepository",
    "runtime_dispatch_integrity_problems",
    "runtime_dispatch_remote_state",
    "runtime_reserve_exposure",
]

# --------------------------------------------------------------------------- #
# Vocabulary
# --------------------------------------------------------------------------- #
#: The observed routes a dispatch may take (C02 §6.1/§6.3/§8.1).
DISPATCH_ROUTES = ("opencode_session", "direct_call")

#: The accounting buckets a route implies (C02 §6.3).
ACCOUNTING_BUCKETS = ("session_statistics", "direct_call")

#: Route -> implied accounting bucket (the two are never independently chosen).
ROUTE_BUCKET = {
    "opencode_session": "session_statistics",
    "direct_call": "direct_call",
}

#: The explicit safe states of a dispatch (C02 §8.1/§8.1.1/§8.3).
#: ``admitted`` is reachable **only** on the durable ``opencode_session`` route.
DISPATCH_STATES = (
    "reserved",
    "submitted",
    "admitted",
    "completed",
    "failed",
    "unknown",
)

#: Cost states whose settlement must carry an exact ``amount_usd`` (C02 §6.3).
_COST_STATES_WITH_AMOUNT = frozenset({"known", "estimated"})

#: Dispatch states from which a fenced local result may still be accepted: a
#: result can only follow a recorded submission (``submitted``), an opencode
#: admission (``admitted``), or a restart-orphaned submission (``unknown``).
_ACCEPTABLE_STATES = frozenset({"submitted", "admitted", "unknown"})

#: Dispatch states that mean remote work **may still be executing**: the outcome
#: is not yet known, so the project still occupies the single serial remote slot
#: and keeps its execution arbitration (C02 §8.3/§8.5). ``reserved`` is *not*
#: here: a reserve is recorded **before** any I/O (ordered
#: ``mark_submitted``-before-I/O), so by ordering it cannot have been issued.
#: ``completed``/``failed`` are terminal and are likewise not remote.
RUNTIME_REMOTE_STATES = ("submitted", "admitted", "unknown")

#: A recorded dispatch row grants **no** authority and performs **no** network or
#: transport I/O; it is a durable ledger entry only (authorization/rights are a
#: separate concern and are explicitly not claimed here).
DISPATCH_GRANTS_AUTHORITY = False
DISPATCH_PERFORMS_NETWORK_IO = False

#: Upper bound on a recorded result-kind label (an opaque token, never content).
MAX_RESULT_KIND_LEN = 128

#: A lowercase hex SHA-256 digest (the contract's single digest algorithm).
_HEX_RE = re.compile(r"^[0-9a-f]{64}$")

#: The intended stored timestamp shape: ``YYYY-MM-DDThh:mm:ss[.ffffff]`` with an
#: explicit UTC designator (``Z`` or ``+00:00``).
_UTC_TIMESTAMP_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|\+00:00)$"
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _scoped_id(prefix: str, project_id: str, key: str) -> str:
    digest = _sha256_hex((project_id + "\x00" + key).encode("utf-8"))
    return f"{prefix}_{digest[:32]}"


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #
class DispatchError(StoreError):
    """A durable runtime dispatch operation failed."""


class DispatchValidationError(DispatchError, ValueError):
    """A dispatch input was structurally invalid (fail closed)."""


class DispatchConflictError(DispatchError):
    """A divergent reserve replay or a conflicting accepted result."""


class DispatchStateError(DispatchError):
    """The requested transition is not permitted in the current state."""


class DispatchStaleFenceError(DispatchStateError):
    """The attempt's fence is not the dispatch's current fence (stale attempt)."""


class DispatchCorruptionError(DispatchError):
    """A stored dispatch row fails structural/hash/link revalidation on read."""


# --------------------------------------------------------------------------- #
# Input validation helpers
# --------------------------------------------------------------------------- #
def _require_nonempty_str(value: object, field: str, *, max_len: int | None = None) -> str:
    if not isinstance(value, str):
        raise DispatchValidationError(f"{field}: must be a string")
    if value == "" or value != value.strip():
        raise DispatchValidationError(
            f"{field}: must be non-empty with no leading/trailing whitespace"
        )
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
        raise DispatchValidationError(f"{field}: must not contain control characters")
    if max_len is not None and len(value) > max_len:
        raise DispatchValidationError(f"{field}: is too long")
    return value


def _require_enum(value: object, allowed: tuple[str, ...], field: str) -> str:
    if value not in allowed:
        # Static message: never echo the caller-supplied value.
        raise DispatchValidationError(f"{field}: is not one of the allowed values")
    return value  # type: ignore[return-value]


def _require_digest(value: object, field: str) -> str:
    """Return the bare lowercase hex SHA-256 digest of ``value`` (fail closed)."""
    if not isinstance(value, str):
        raise DispatchValidationError(f"{field}: a SHA-256 digest must be a string")
    text = value[7:] if value.startswith("sha256:") else value
    if _HEX_RE.fullmatch(text) is None:
        raise DispatchValidationError(f"{field}: must be a lowercase hex SHA-256 digest")
    return text


def _require_exact_decimal(
    value: object, field: str, *, strictly_positive: bool = False
) -> str:
    """Validate an exact decimal string, surfacing a typed dispatch error.

    Wraps the shared bounded-decimal parser so a structurally invalid reserve or
    amount is a :class:`DispatchValidationError` (never a raw job error) at the
    public API boundary.
    """
    try:
        return _exact_decimal(value, field, strictly_positive=strictly_positive)
    except JobValidationError as exc:
        raise DispatchValidationError(str(exc)) from exc


def _require_utc_timestamp(value: object, field: str) -> str:
    """Validate an exact RFC 3339 **UTC** timestamp (never a bare non-empty string)."""
    text = _require_nonempty_str(value, field)
    if _UTC_TIMESTAMP_RE.fullmatch(text) is None:
        raise DispatchValidationError(f"{field}: must be an RFC 3339 UTC timestamp")
    candidate = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise DispatchValidationError(
            f"{field}: must be an RFC 3339 timestamp"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise DispatchValidationError(f"{field}: must carry an explicit UTC offset")
    return text


def _fail_closed_row(what: str):
    """Decorate a stored-row validator so a malformed row fails closed.

    A stored row is untrusted input. Any helper that rejects it — a typed error,
    a ``ValueError``, a missing column, a non-string field — must surface as the
    single typed :class:`DispatchCorruptionError`. The message is static: it never
    echoes the offending value.
    """

    def decorate(func):
        @functools.wraps(func)
        def wrapper(self, *args, **kwargs):
            try:
                return func(self, *args, **kwargs)
            except DispatchCorruptionError:
                raise
            except DispatchConflictError:
                raise
            except (
                ValueError,
                TypeError,
                KeyError,
                IndexError,
                UnicodeError,
                RuntimeValidationError,
                RuntimeCorruptionError,
                sqlite3.Error,
            ) as exc:
                raise DispatchCorruptionError(f"{what}: stored row is malformed.") from exc

        return wrapper

    return decorate


# --------------------------------------------------------------------------- #
# Value objects
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class DispatchRecord:
    """One durable dispatch (immutable identity + mutable operational state).

    The identity fields (through ``bound_evidence_digest``/``created_at_utc``) are
    pinned and can never change; the operational fields (``state`` onward) reflect
    the current safe state.
    """

    project_id: str
    dispatch_id: str
    request_id: str
    task_id: str
    task_fingerprint: str
    route: str
    accounting_bucket: str
    stage: str
    input_digest: str
    version_digest: str
    reserve_usd: str
    bound_evidence_digest: str
    created_at_utc: str
    state: str
    current_fence: int
    admission_id: str | None
    admission_ref: str | None
    result_kind: str | None
    result_ref: str | None
    result_digest: str | None
    cost_state: str
    cost_amount_usd: str | None
    started_at_utc: str | None
    ended_at_utc: str | None
    updated_at_utc: str

    def identity_tuple(self) -> tuple:
        """The exact identity a repeated ``request_id`` must match to be idempotent."""
        return (
            self.task_id,
            self.task_fingerprint,
            self.route,
            self.accounting_bucket,
            self.stage,
            self.input_digest,
            self.version_digest,
            self.reserve_usd,
            self.bound_evidence_digest,
        )

    def to_mapping(self) -> dict[str, Any]:
        """A JSON-safe view (identity, route/bucket, state, and settled cost)."""
        return {
            "project_id": self.project_id,
            "dispatch_id": self.dispatch_id,
            "request_id": self.request_id,
            "task_id": self.task_id,
            "task_fingerprint": self.task_fingerprint,
            "route": self.route,
            "accounting_bucket": self.accounting_bucket,
            "stage": self.stage,
            "input_digest": self.input_digest,
            "version_digest": self.version_digest,
            "reserve_usd": self.reserve_usd,
            "bound_evidence_digest": self.bound_evidence_digest,
            "created_at_utc": self.created_at_utc,
            "state": self.state,
            "current_fence": self.current_fence,
            "admission_id": self.admission_id,
            "admission_ref": self.admission_ref,
            "result_kind": self.result_kind,
            "result_ref": self.result_ref,
            "result_digest": self.result_digest,
            "cost_state": self.cost_state,
            "cost_amount_usd": self.cost_amount_usd,
            "started_at_utc": self.started_at_utc,
            "ended_at_utc": self.ended_at_utc,
            "updated_at_utc": self.updated_at_utc,
        }


# --------------------------------------------------------------------------- #
# Shared cumulative-cap exposure (pure; no recursive budget loop)
# --------------------------------------------------------------------------- #
def _table_present(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (name,),
    ).fetchone()
    return row is not None


def _user_version(conn: sqlite3.Connection) -> int:
    """The store's ``PRAGMA user_version`` (the runtime ledger's schema version)."""
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


#: The store schema version that introduces the runtime dispatch ledger. A store
#: **at or above** this version must carry both ledger tables; only a genuinely
#: older store (``< 6``) may legitimately have no runtime ledger to count.
RUNTIME_LEDGER_SCHEMA_VERSION = 6


def runtime_reserve_exposure(
    conn: sqlite3.Connection, project_id: str, *, stage: str | None = None
) -> Decimal:
    """Return the conservative cost exposure of the runtime dispatch ledger.

    Pure read-only helper: it never touches a
    :class:`~review_article_agent.runtime.jobs.JobRepository` (so
    ``JobRepository._reserved_total`` can call it without a recursive budget
    loop). Each dispatch contributes ``max(original reserve, settled actual)`` —
    the immutable upper bound is always counted, and a **known/estimated** amount
    that overran it is counted instead, so an overrun is never under-counted
    (C02 §5.6). ``unknown``/``not_incurred`` costs contribute no separate amount
    (their reserve bound is retained).

    Fail closed: a dispatch whose operational state row is **missing**, whose
    stored ``cost_state`` is unknown, or whose stored cost state/amount disagree
    is **not** silently counted at its bare reserve — the whole read raises
    :class:`DispatchCorruptionError` instead (a malformed ledger must never
    under-count exposure). Every stored amount is re-validated as a bounded exact
    decimal. Only a store that genuinely **predates** the runtime ledger (a schema
    ``< 6``, so no ``runtime_dispatch``/``runtime_dispatch_state`` table was ever
    created) contributes nothing; a schema ``>= 6`` store that is missing either
    ledger table is corrupt and raises :class:`DispatchCorruptionError` rather than
    quietly reporting zero exposure (C02 §5.6).
    """
    if _user_version(conn) < RUNTIME_LEDGER_SCHEMA_VERSION:
        # A genuine pre-ledger store: no runtime dispatch table was ever created.
        return Decimal("0")
    if not _table_present(conn, "runtime_dispatch"):
        raise DispatchCorruptionError(
            "runtime dispatch ledger: the identity table is missing from a store "
            "that should carry it; refusing to report zero exposure."
        )
    if not _table_present(conn, "runtime_dispatch_state"):
        raise DispatchCorruptionError(
            "runtime dispatch ledger: the operational state table is missing from a "
            "store that should carry it; refusing to report zero exposure."
        )
    sql = (
        "SELECT d.reserve_usd, s.cost_state, s.cost_amount_usd "
        "FROM runtime_dispatch d "
        "LEFT JOIN runtime_dispatch_state s ON s.project_id = d.project_id "
        "AND s.dispatch_id = d.dispatch_id "
        "WHERE d.project_id = ?"
    )
    params: list[object] = [project_id]
    if stage is not None:
        sql += " AND d.stage = ?"
        params.append(stage)
    total = Decimal("0")
    for row in conn.execute(sql, params).fetchall():
        reserve_raw, cost_state, amount_raw = row[0], row[1], row[2]
        if cost_state is None:
            raise DispatchCorruptionError(
                "runtime dispatch ledger: an operational state row is missing; "
                "refusing to count its reserve."
            )
        if cost_state not in COST_STATES:
            raise DispatchCorruptionError(
                "runtime dispatch ledger: a stored cost state is malformed; "
                "refusing to count it."
            )
        if (cost_state in _COST_STATES_WITH_AMOUNT) != (amount_raw is not None):
            raise DispatchCorruptionError(
                "runtime dispatch ledger: a stored cost amount is inconsistent with "
                "its cost state; refusing to count it."
            )
        try:
            counted = _bounded_decimal_amount(reserve_raw, "stored runtime reserve")
            if amount_raw is not None:
                actual = _bounded_decimal_amount(
                    amount_raw, "stored runtime cost amount"
                )
                counted = max(counted, actual)
        except (ValueError, TypeError) as exc:
            raise DispatchCorruptionError(
                "runtime dispatch ledger: a stored amount is malformed; refusing "
                "to count it."
            ) from exc
        total = _add_exact(total, counted)
    return total


# --------------------------------------------------------------------------- #
# Repository
# --------------------------------------------------------------------------- #
class RuntimeDispatchRepository:
    """Durable, atomic runtime dispatch over a verified :class:`ProjectStore`.

    Typical use::

        store = ProjectStore.open_at(app_dir, expected_project_id=pid)  # path layer
        dispatch = RuntimeDispatchRepository(store)

        record = dispatch.reserve_dispatch(
            task,                          # a persisted RuntimeTaskRecord
            request_id="call-1",
            route="opencode_session",
            stage="section_critique",
            input_digest=..., version_digest=...,
            reserve_usd="0.10",
            bound_evidence_digest=...,
        )
        record = dispatch.mark_submitted(record.dispatch_id)   # BEFORE future I/O
        record = dispatch.mark_admitted(record.dispatch_id, admission_id="adm-1")
        record = dispatch.accept_result(
            record.dispatch_id, fence=record.current_fence,
            result_kind="synthetic", result_ref="res_1", result_digest=...,
        )
        # -> one fenced accepted local result; the caps counted every route.
    """

    def __init__(
        self,
        store,
        *,
        runtime: RuntimeRepository | None = None,
        jobs: JobRepository | None = None,
    ) -> None:
        self._store = store
        self._runtime = runtime if runtime is not None else RuntimeRepository(store)
        self._jobs = jobs if jobs is not None else JobRepository(store)

    @property
    def project_id(self) -> str:
        """The store's own project id; the repository never uses a caller project."""
        return self._store.project_id

    def _conn(self) -> sqlite3.Connection:
        return self._store.connection

    # -- reserve (atomic: validate task + caps + identity + reserve) ---------- #
    def reserve_dispatch(
        self,
        task: RuntimeTaskRecord,
        *,
        request_id: str,
        route: str,
        stage: str,
        input_digest: str,
        version_digest: str,
        reserve_usd: str,
        bound_evidence_digest: str,
        created_at_utc: str | None = None,
    ) -> DispatchRecord:
        """Atomically reserve one logical dispatch (reserve before dispatch).

        In a single ``BEGIN IMMEDIATE`` transaction this:

        1. validates that ``task`` is the **persisted** runtime task with the same
           fingerprint (a missing/foreign/relabelled task is refused);
        2. returns the existing row when ``request_id`` already names the **same**
           identity (idempotent, **no** double reserve) or refuses a divergent one;
        3. refuses a **different** ``request_id`` for a task that already has any
           dispatch (initial scope: at most one logical dispatch per task, no
           retries);
        4. checks the cumulative project and stage caps — which count **both** the
           synthetic job reserves and this ledger's reserves — and refuses a
           reserve that would breach either;
        5. writes the immutable identity row and its initial ``reserved`` state.

        ``reserve_usd`` must be an exact, strictly positive decimal string;
        ``bound_evidence_digest`` (the digest of the defensible upper-bound
        evidence) and the canonical ``input_digest``/``version_digest`` must be
        SHA-256 digests. A missing or invalid reserve or bound evidence is refused
        **before** any row is written. A recorded row grants no authority and no
        network access.
        """
        if not isinstance(task, RuntimeTaskRecord):
            raise DispatchValidationError(
                "task: must be a persisted RuntimeTaskRecord"
            )
        if task.project_id != self.project_id:
            raise DispatchValidationError(
                "task: must belong to this project; refusing a foreign project."
            )
        request_id = validate_identifier(request_id, "request_id")
        route = _require_enum(route, DISPATCH_ROUTES, "route")
        bucket = ROUTE_BUCKET[route]
        stage = validate_identifier(stage, "stage")
        input_digest = _require_digest(input_digest, "input_digest")
        version_digest = _require_digest(version_digest, "version_digest")
        reserve = _require_exact_decimal(reserve_usd, "reserve_usd", strictly_positive=True)
        bound_evidence_digest = _require_digest(
            bound_evidence_digest, "bound_evidence_digest"
        )
        created = (
            _utc_now()
            if created_at_utc is None
            else _require_utc_timestamp(created_at_utc, "created_at_utc")
        )
        dispatch_id = _scoped_id("disp", self.project_id, request_id)

        with self._store.transaction():
            persisted = self._require_persisted_task(task)
            identity = (
                persisted.task_id,
                persisted.fingerprint,
                route,
                bucket,
                stage,
                input_digest,
                version_digest,
                reserve,
                bound_evidence_digest,
            )
            existing = self._dispatch_row_by_request(request_id)
            if existing is not None:
                record = self._validate_dispatch_row(existing, self._state_row(dispatch_id))
                if record.identity_tuple() != identity:
                    raise DispatchConflictError(
                        "request was already reserved with a different dispatch "
                        "identity; refusing a divergent retry."
                    )
                return record
            self._assert_no_other_dispatch_for_task(persisted.task_id)
            self._check_dispatch_caps(stage=stage, amount=reserve)
            self._conn().execute(
                "INSERT INTO runtime_dispatch "
                "(project_id, dispatch_id, request_id, task_id, task_fingerprint, "
                " route, accounting_bucket, stage, input_digest, version_digest, "
                " reserve_usd, bound_evidence_digest, created_at_utc) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    self.project_id,
                    dispatch_id,
                    request_id,
                    persisted.task_id,
                    persisted.fingerprint,
                    route,
                    bucket,
                    stage,
                    input_digest,
                    version_digest,
                    reserve,
                    bound_evidence_digest,
                    created,
                ),
            )
            self._conn().execute(
                "INSERT INTO runtime_dispatch_state "
                "(project_id, dispatch_id, state, current_fence, cost_state, "
                " updated_at_utc) VALUES (?, ?, 'reserved', 1, 'not_incurred', ?)",
                (self.project_id, dispatch_id, created),
            )
        stored = self.get_dispatch(dispatch_id)
        assert stored is not None  # just committed
        return stored

    def _require_persisted_task(self, task: RuntimeTaskRecord) -> RuntimeTaskRecord:
        """Return the persisted task, refusing a missing/foreign/relabelled record."""
        try:
            stored = self._runtime.get_task(task.task_id)
        except RuntimeCorruptionError as exc:
            raise DispatchCorruptionError(
                "task: the persisted runtime task is corrupt."
            ) from exc
        if stored is None:
            raise DispatchValidationError("task: is not persisted in this project.")
        if (
            stored.project_id != self.project_id
            or stored.task_id != task.task_id
            or stored.fingerprint != task.fingerprint
        ):
            raise DispatchValidationError(
                "task: the persisted task does not match the supplied record; "
                "refusing a relabelled or stale task."
            )
        return stored

    def _assert_no_other_dispatch_for_task(self, task_id: str) -> None:
        """Refuse a second logical dispatch for the same task (initial scope).

        Initial scope permits **no retries**: at most one logical dispatch may
        exist per task. A repeated reserve with the *same* ``request_id`` is an
        idempotent replay (handled earlier) and remains allowed; a *different*
        logical-call key is refused while the existing dispatch is reserved,
        submitted, in-flight/admitted, restart-orphaned/unknown, or even after it
        completed or failed. An explicitly authorized retry is a separate future
        task; it must never be smuggled in through a new request key here
        (C02 §8.1.1/§8.3).
        """
        row = self._conn().execute(
            "SELECT 1 FROM runtime_dispatch d WHERE d.project_id = ? "
            "AND d.task_id = ? LIMIT 1",
            (self.project_id, task_id),
        ).fetchone()
        if row is not None:
            raise DispatchStateError(
                "this task already has a recorded dispatch; initial scope permits "
                "at most one logical dispatch per task and no retry. Reconcile or "
                "escalate the existing dispatch instead of starting a new one."
            )

    def _check_dispatch_caps(self, *, stage: str, amount: str) -> None:
        """Refuse a reserve that would breach the cumulative project/stage caps.

        ``budget_usage`` already counts the synthetic job reserves **and** this
        ledger's reserves, so this is the single combined ceiling (C02 §5.6/§6.3).
        """
        usage = self._jobs.budget_usage(stage=stage)
        project_cap = Decimal(usage.project_cap_usd)
        stage_cap = Decimal(usage.stage_cap_usd)
        project_reserved = Decimal(usage.project_reserved_usd)
        stage_reserved = Decimal(
            usage.stage_reserved_usd if usage.stage_reserved_usd is not None else "0"
        )
        want = Decimal(amount)
        if _add_exact(project_reserved, want) > project_cap:
            raise BudgetExceededError(
                "reserving this dispatch would exceed the project cost cap; "
                "refusing to dispatch."
            )
        if _add_exact(stage_reserved, want) > stage_cap:
            raise BudgetExceededError(
                "reserving this dispatch would exceed the stage cost cap; "
                "refusing to dispatch."
            )

    # -- reads --------------------------------------------------------------- #
    def get_dispatch(self, dispatch_id: str) -> DispatchRecord | None:
        dispatch_id = validate_identifier(dispatch_id, "dispatch_id")
        row = self._dispatch_row(dispatch_id)
        if row is None:
            return None
        return self._validate_dispatch_row(row, self._state_row(dispatch_id))

    def get_dispatch_by_request(self, request_id: str) -> DispatchRecord | None:
        request_id = validate_identifier(request_id, "request_id")
        row = self._dispatch_row_by_request(request_id)
        if row is None:
            return None
        return self._validate_dispatch_row(row, self._state_row(row["dispatch_id"]))

    def list_dispatches(self) -> list[DispatchRecord]:
        rows = self._conn().execute(
            "SELECT * FROM runtime_dispatch WHERE project_id = ? ORDER BY rowid",
            (self.project_id,),
        ).fetchall()
        return [self._validate_dispatch_row(row, self._state_row(row["dispatch_id"])) for row in rows]

    def verify_dispatch(self, dispatch_id: str) -> bool:
        """``True`` iff the dispatch exists and revalidates (a corrupt row is ``False``)."""
        try:
            return self.get_dispatch(dispatch_id) is not None
        except DispatchCorruptionError:
            return False

    # -- state transitions --------------------------------------------------- #
    def mark_submitted(self, dispatch_id: str) -> DispatchRecord:
        """Record that the dispatch is submitted — **before** any future I/O.

        A ``reserved`` dispatch becomes ``submitted`` (the reserve is now held
        against a call that may be issued) and its cost state is upgraded from
        ``not_incurred`` to ``unknown``: once a submission is recorded the remote
        outcome is not yet known, so the dispatch can never claim no cost was
        incurred (C02 §6.3/§8.3). An already-settled ``known``/``estimated`` cost
        is left untouched.

        The submission is **single-use**: it is the one durable "send claim". A
        second caller (an already-``submitted`` dispatch, or any other state) is
        refused with :class:`DispatchStateError` rather than handed back a
        ``submitted`` record it did not win — otherwise two callers would each
        believe they hold the claim and might both issue I/O. Read/reconcile
        (:meth:`recover`, :meth:`get_dispatch`) is what may be replayed, never the
        send claim.
        """
        dispatch_id = validate_identifier(dispatch_id, "dispatch_id")
        with self._store.transaction():
            record = self._require_dispatch(dispatch_id)
            if record.state != "reserved":
                raise DispatchStateError(
                    "only a reserved dispatch may be submitted, and a submission is "
                    "single-use; this caller did not win the send claim. Reconcile "
                    "the existing dispatch instead of issuing I/O."
                )
            now = _utc_now()
            self._conn().execute(
                "UPDATE runtime_dispatch_state SET state = 'submitted', "
                " cost_state = CASE WHEN cost_state = 'not_incurred' THEN 'unknown' "
                " ELSE cost_state END, "
                " started_at_utc = COALESCE(started_at_utc, ?), updated_at_utc = ? "
                "WHERE project_id = ? AND dispatch_id = ?",
                (now, now, self.project_id, dispatch_id),
            )
        return self._require_dispatch(dispatch_id)

    def mark_admitted(
        self,
        dispatch_id: str,
        *,
        admission_id: str,
        admission_ref: str | None = None,
    ) -> DispatchRecord:
        """Record a durable admission receipt (**``opencode_session`` only**).

        The direct route has no proven durable/idempotent admission, so this is
        refused for a ``direct_call`` dispatch (and the database trigger refuses
        such a row outright). Admission is **not** completion. An **exact** replay
        (the same ``admission_id`` **and** the same ``admission_ref``) is
        idempotent; a divergent replay raises :class:`DispatchConflictError` instead
        of silently reusing the recorded ref.
        """
        dispatch_id = validate_identifier(dispatch_id, "dispatch_id")
        admission_id = validate_identifier(admission_id, "admission_id")
        if admission_ref is not None:
            admission_ref = validate_identifier(admission_ref, "admission_ref")
        with self._store.transaction():
            record = self._require_dispatch(dispatch_id)
            if record.route != "opencode_session":
                raise DispatchStateError(
                    "a direct_call dispatch has no durable admission; refusing to "
                    "claim an idempotent external admission."
                )
            if record.state == "admitted":
                # An exact replay (same id **and** same ref) is idempotent; a
                # divergent replay must never silently reuse the recorded ref.
                if (
                    record.admission_id == admission_id
                    and record.admission_ref == admission_ref
                ):
                    return record
                raise DispatchConflictError(
                    "the dispatch already recorded a different admission; refusing "
                    "a divergent admission replay."
                )
            if record.state != "submitted":
                raise DispatchStateError(
                    "only a submitted dispatch may record an admission; refusing to "
                    "claim a durable admission in this state."
                )
            self._conn().execute(
                "UPDATE runtime_dispatch_state SET state = 'admitted', "
                " admission_id = ?, admission_ref = ?, updated_at_utc = ? "
                "WHERE project_id = ? AND dispatch_id = ?",
                (admission_id, admission_ref, _utc_now(), self.project_id, dispatch_id),
            )
        return self._require_dispatch(dispatch_id)

    def _require_own_transaction(self) -> None:
        """Refuse a transaction-owned primitive invoked with no open transaction.

        ``ProjectStore.transaction`` is a single ``BEGIN IMMEDIATE`` and **cannot
        nest**, so the atomic composition path (the direct-Jev result commit) owns
        exactly one transaction and calls these primitives **inside** it. A
        primitive reached without an open transaction would either perform work
        nobody commits or silently autocommit a partial transition, so it fails
        closed instead of guessing.
        """
        if not self._conn().in_transaction:
            raise DispatchStateError(
                "this dispatch transition must run inside a caller-owned store "
                "transaction."
            )

    def accept_result(
        self,
        dispatch_id: str,
        *,
        fence: int,
        result_kind: str,
        result_ref: str,
        result_digest: str,
    ) -> DispatchRecord:
        """Accept the dispatch's single local terminal result if its fence is current.

        A stale fence raises :class:`DispatchStaleFenceError`. An identical replay
        (same fence + same result) is idempotent; a divergent replay for an
        already-completed dispatch raises :class:`DispatchConflictError`. Accepted
        from a ``submitted``/``admitted``/``unknown`` dispatch (the last is the
        same-identity reconciliation of a restart-orphaned submission); refused
        from ``reserved`` (no I/O was issued) or a terminal state.

        This public form owns its own transaction; a caller that must compose it
        with other writes (the direct-Jev result commit) uses the transaction-owned
        primitive :meth:`_accept_result_in_transaction` instead.
        """
        with self._store.transaction():
            return self._accept_result_in_transaction(
                dispatch_id,
                fence=fence,
                result_kind=result_kind,
                result_ref=result_ref,
                result_digest=result_digest,
            )

    def _accept_result_in_transaction(
        self,
        dispatch_id: str,
        *,
        fence: int,
        result_kind: str,
        result_ref: str,
        result_digest: str,
    ) -> DispatchRecord:
        """The transaction-owned accept primitive (caller owns the unit of work)."""
        self._require_own_transaction()
        dispatch_id = validate_identifier(dispatch_id, "dispatch_id")
        if isinstance(fence, bool) or not isinstance(fence, int) or fence < 1:
            raise DispatchValidationError("fence: must be a positive integer")
        result_kind = _require_nonempty_str(
            result_kind, "result_kind", max_len=MAX_RESULT_KIND_LEN
        )
        result_ref = validate_identifier(result_ref, "result_ref")
        result_digest = _require_digest(result_digest, "result_digest")
        record = self._require_dispatch(dispatch_id)
        if fence != record.current_fence:
            raise DispatchStaleFenceError(
                "the supplied fence is not the dispatch's current fence; a stale "
                "attempt cannot accept a result."
            )
        if record.state == "completed":
            if (
                record.result_kind,
                record.result_ref,
                record.result_digest,
            ) == (result_kind, result_ref, result_digest):
                return record
            raise DispatchConflictError(
                "the dispatch already has a different accepted result."
            )
        if record.state not in _ACCEPTABLE_STATES:
            raise DispatchStateError(
                "a local result can only follow a recorded submission; this "
                "dispatch is not in an acceptable state."
            )
        now = _utc_now()
        self._conn().execute(
            "UPDATE runtime_dispatch_state SET state = 'completed', "
            " result_kind = ?, result_ref = ?, result_digest = ?, "
            " ended_at_utc = ?, updated_at_utc = ? "
            "WHERE project_id = ? AND dispatch_id = ?",
            (
                result_kind,
                result_ref,
                result_digest,
                now,
                now,
                self.project_id,
                dispatch_id,
            ),
        )
        return self._require_dispatch(dispatch_id)

    def mark_failed(self, dispatch_id: str) -> DispatchRecord:
        """Record a definitive failure (no accepted local result).

        Idempotent for an already-failed dispatch; refused for a completed one and
        — critically — refused for an **ambiguous** (``unknown``) dispatch: an
        unresolved remote outcome must never be reclassified as a definitive
        failure, because that would make an ambiguous dispatch look retry-eligible
        (C02 §8.1.1/§8.3). Reconcile or escalate instead.

        This public form owns its own transaction; a caller that must compose it
        with other writes uses the transaction-owned primitive
        :meth:`_mark_failed_in_transaction` instead.
        """
        with self._store.transaction():
            return self._mark_failed_in_transaction(dispatch_id)

    def _mark_failed_in_transaction(
        self, dispatch_id: str, *, definitive_rejection: bool = False
    ) -> DispatchRecord:
        """The transaction-owned fail primitive (caller owns the unit of work).

        ``definitive_rejection`` may only be set by a caller holding a **validated**
        documented pre-billing rejection (a definitive HTTP 400/401/402/403/404/413
        at the send boundary): that actual evidence resolves an ambiguous
        (``unknown``) remote outcome, so it may become a definitive ``failed``. It
        is never a caller-supplied success/terminal flag — the evidence is derived
        from the recorded result envelope, never trusted as a boolean.
        """
        self._require_own_transaction()
        dispatch_id = validate_identifier(dispatch_id, "dispatch_id")
        record = self._require_dispatch(dispatch_id)
        if record.state == "failed":
            return record
        if record.state == "completed":
            raise DispatchStateError(
                "the dispatch already completed; it cannot be marked failed."
            )
        if record.state == "unknown" and not definitive_rejection:
            raise DispatchStateError(
                "an ambiguous (unknown) dispatch cannot be turned into a "
                "definitive failure; reconcile or escalate it instead."
            )
        now = _utc_now()
        self._conn().execute(
            "UPDATE runtime_dispatch_state SET state = 'failed', "
            " ended_at_utc = ?, updated_at_utc = ? "
            "WHERE project_id = ? AND dispatch_id = ?",
            (now, now, self.project_id, dispatch_id),
        )
        return self._require_dispatch(dispatch_id)

    def recover(self, dispatch_id: str) -> DispatchRecord:
        """Reconcile the dispatch after a restart from durable state (no new identity).

        * an outstanding ``submitted`` dispatch becomes ``unknown`` — never reset
          to ``reserved`` and never retried;
        * an existing ``opencode_session`` ``admitted`` dispatch is **retained**
          (``admitted``) for same-identity reconciliation;
        * a ``reserved`` dispatch (never submitted) is left untouched;
        * ``unknown``/``completed``/``failed`` are terminal and untouched.

        No new identity is ever minted and no reserve is released. This public
        form owns its own transaction; a caller that must compose it with other
        writes uses the transaction-owned primitive
        :meth:`_recover_in_transaction` instead.
        """
        with self._store.transaction():
            return self._recover_in_transaction(dispatch_id)

    def _recover_in_transaction(self, dispatch_id: str) -> DispatchRecord:
        """The transaction-owned reconcile primitive (caller owns the unit of work)."""
        self._require_own_transaction()
        dispatch_id = validate_identifier(dispatch_id, "dispatch_id")
        record = self._require_dispatch(dispatch_id)
        if record.state == "submitted":
            self._conn().execute(
                "UPDATE runtime_dispatch_state SET state = 'unknown', "
                " updated_at_utc = ? WHERE project_id = ? AND dispatch_id = ?",
                (_utc_now(), self.project_id, dispatch_id),
            )
            return self._require_dispatch(dispatch_id)
        return record

    # -- cost ----------------------------------------------------------------- #
    def settle_cost(
        self,
        dispatch_id: str,
        *,
        cost_state: str,
        amount_usd: str | None = None,
    ) -> DispatchRecord:
        """Settle the dispatch's reserved cost, monotonically (C02 §5.6/§6.3).

        The reserve is **never reduced**: the original upper bound stays counted
        for ``known``/``estimated``/``unknown``/``not_incurred`` (conservative
        usage), so a later estimate/known transition can never silently shrink a
        held reserve. A ``known``/``estimated`` amount that **exceeds** the reserve
        is counted at the larger actual, so an overrun is never under-counted.

        Settlement is **monotonic**: an established amount can never be discarded
        (``known``/``estimated`` → ``unknown``/``not_incurred``) or lowered
        (``known 2.00`` → ``known 1.00``), and an ``unknown`` cost can never be
        re-declared as ``not_incurred``. This is what stops an overrun from being
        erased by a later weaker settlement (a ``known 2.00`` over a ``0.10``
        reserve must never fall back to ``0.10``). A refused downgrade raises
        :class:`DispatchConflictError` and changes nothing.

        This public form owns its own transaction; a caller that must compose it
        with other writes uses the transaction-owned primitive
        :meth:`_settle_cost_in_transaction` instead.
        """
        with self._store.transaction():
            return self._settle_cost_in_transaction(
                dispatch_id, cost_state=cost_state, amount_usd=amount_usd
            )

    def _settle_cost_in_transaction(
        self,
        dispatch_id: str,
        *,
        cost_state: str,
        amount_usd: str | None = None,
        allow_definitive_not_incurred: bool = False,
    ) -> DispatchRecord:
        """The transaction-owned cost-settlement primitive (caller owns the unit).

        The same monotonic policy as :meth:`settle_cost`, plus the one documented
        exception: a **validated definitive pre-billing rejection** may resolve an
        already-``unknown`` cost to ``not_incurred`` (nothing was billed). That flag
        is set only by the result-commit path from the recorded result envelope; a
        standalone caller can never release a reserve this way.
        """
        self._require_own_transaction()
        dispatch_id = validate_identifier(dispatch_id, "dispatch_id")
        _require_enum(cost_state, COST_STATES, "cost_state")
        if cost_state in _COST_STATES_WITH_AMOUNT:
            if amount_usd is None:
                raise DispatchValidationError(
                    "a known/estimated cost requires an exact amount_usd."
                )
            recorded: str | None = _require_exact_decimal(amount_usd, "amount_usd")
        else:
            if amount_usd is not None:
                raise DispatchValidationError(
                    "an unknown/not_incurred cost must not carry an amount_usd."
                )
            recorded = None
        current = self._require_dispatch(dispatch_id)
        self._assert_cost_settlement_monotonic(
            current,
            cost_state,
            recorded,
            allow_definitive_not_incurred=allow_definitive_not_incurred,
        )
        self._conn().execute(
            "UPDATE runtime_dispatch_state SET cost_state = ?, "
            " cost_amount_usd = ?, updated_at_utc = ? "
            "WHERE project_id = ? AND dispatch_id = ?",
            (cost_state, recorded, _utc_now(), self.project_id, dispatch_id),
        )
        return self._require_dispatch(dispatch_id)

    def _assert_cost_settlement_monotonic(
        self,
        current: DispatchRecord,
        new_state: str,
        new_amount: str | None,
        *,
        allow_definitive_not_incurred: bool = False,
    ) -> None:
        """Refuse a settlement that would weaken or lower an established cost."""
        if new_state == "not_incurred" and current.cost_state != "not_incurred":
            if not allow_definitive_not_incurred:
                raise DispatchConflictError(
                    "an incurred or unknown cost cannot be re-declared as not incurred; "
                    "refusing to release the reserve."
                )
        if current.cost_amount_usd is not None:
            if new_amount is None:
                raise DispatchConflictError(
                    "an established cost amount cannot be discarded; refusing a "
                    "cost downgrade."
                )
            if Decimal(new_amount) < Decimal(current.cost_amount_usd):
                raise DispatchConflictError(
                    "an established cost amount cannot be reduced; refusing a cost "
                    "downgrade."
                )
        if _counted_exposure(current.reserve_usd, new_state, new_amount) < (
            _counted_exposure(
                current.reserve_usd, current.cost_state, current.cost_amount_usd
            )
        ):
            raise DispatchConflictError(
                "a cost settlement cannot reduce the dispatch's counted exposure; "
                "refusing the downgrade."
            )

    # -- shared budget view --------------------------------------------------- #
    def budget_usage(self, *, stage: str | None = None) -> BudgetUsage:
        """The combined project/stage budget usage (synthetic **and** runtime).

        Delegates to :meth:`JobRepository.budget_usage`, which counts both the
        synthetic job reserves and this ledger's reserves, so a caller sees the
        single shared ceiling (C02 §5.6).
        """
        return self._jobs.budget_usage(stage=stage)

    # -- internal row access -------------------------------------------------- #
    def _dispatch_row(self, dispatch_id: str) -> sqlite3.Row | None:
        return self._conn().execute(
            "SELECT * FROM runtime_dispatch WHERE project_id = ? AND dispatch_id = ?",
            (self.project_id, dispatch_id),
        ).fetchone()

    def _dispatch_row_by_request(self, request_id: str) -> sqlite3.Row | None:
        return self._conn().execute(
            "SELECT * FROM runtime_dispatch WHERE project_id = ? AND request_id = ?",
            (self.project_id, request_id),
        ).fetchone()

    def _state_row(self, dispatch_id: str) -> sqlite3.Row | None:
        return self._conn().execute(
            "SELECT * FROM runtime_dispatch_state WHERE project_id = ? AND "
            "dispatch_id = ?",
            (self.project_id, dispatch_id),
        ).fetchone()

    def _require_dispatch(self, dispatch_id: str) -> DispatchRecord:
        row = self._dispatch_row(dispatch_id)
        if row is None:
            raise DispatchStateError(
                "no such dispatch exists in this project."
            )
        return self._validate_dispatch_row(row, self._state_row(dispatch_id))

    # -- row validation ------------------------------------------------------- #
    @_fail_closed_row("dispatch")
    def _validate_dispatch_row(
        self, identity_row: sqlite3.Row, state_row: sqlite3.Row | None
    ) -> DispatchRecord:
        """Structurally revalidate a dispatch identity + state; fail closed."""
        if identity_row["project_id"] != self.project_id:
            raise DispatchCorruptionError("dispatch: foreign project row.")
        request_id = validate_identifier(identity_row["request_id"], "request_id")
        dispatch_id = validate_identifier(identity_row["dispatch_id"], "dispatch_id")
        # The dispatch id is derived from the request key, never free-form.
        if dispatch_id != _scoped_id("disp", self.project_id, request_id):
            raise DispatchCorruptionError("dispatch: dispatch id is not request-scoped.")
        task_id = validate_identifier(identity_row["task_id"], "task_id")
        fingerprint = _require_digest(identity_row["task_fingerprint"], "task_fingerprint")
        route = identity_row["route"]
        if route not in DISPATCH_ROUTES:
            raise DispatchCorruptionError("dispatch: unknown route.")
        bucket = identity_row["accounting_bucket"]
        if bucket not in ACCOUNTING_BUCKETS:
            raise DispatchCorruptionError("dispatch: unknown accounting bucket.")
        if ROUTE_BUCKET[route] != bucket:
            raise DispatchCorruptionError(
                "dispatch: accounting bucket does not match its route."
            )
        stage = validate_identifier(identity_row["stage"], "stage")
        input_digest = _require_digest(identity_row["input_digest"], "input_digest")
        version_digest = _require_digest(identity_row["version_digest"], "version_digest")
        reserve_usd = _exact_decimal(
            identity_row["reserve_usd"], "reserve_usd", strictly_positive=True
        )
        bound_evidence_digest = _require_digest(
            identity_row["bound_evidence_digest"], "bound_evidence_digest"
        )
        _require_utc_timestamp(identity_row["created_at_utc"], "created_at_utc")
        # The linked persisted task must fully revalidate, not merely carry a
        # matching fingerprint column: a malformed task row (tampered canonical
        # bytes or projected columns) must not survive behind an intact pin.
        try:
            linked_task = self._runtime.get_task(task_id)
        except RuntimeCorruptionError as exc:
            raise DispatchCorruptionError(
                "dispatch: the linked runtime task is corrupt."
            ) from exc
        if linked_task is None:
            raise DispatchCorruptionError("dispatch: task link row is missing.")
        if (
            linked_task.project_id != self.project_id
            or linked_task.task_id != task_id
            or linked_task.fingerprint != fingerprint
        ):
            raise DispatchCorruptionError(
                "dispatch: linked runtime task does not match the pinned identity."
            )
        if state_row is None:
            raise DispatchCorruptionError("dispatch: operational state row is missing.")
        if (
            state_row["project_id"] != self.project_id
            or state_row["dispatch_id"] != dispatch_id
        ):
            raise DispatchCorruptionError(
                "dispatch: operational state row does not match its identity."
            )
        state = state_row["state"]
        if state not in DISPATCH_STATES:
            raise DispatchCorruptionError("dispatch: unknown state.")
        fence = state_row["current_fence"]
        # Initial scope has exactly one fence: no re-fencing exists yet.
        if isinstance(fence, bool) or not isinstance(fence, int) or fence != 1:
            raise DispatchCorruptionError(
                "dispatch: the fence is not the initial single fence."
            )
        admission_id = state_row["admission_id"]
        admission_ref = state_row["admission_ref"]
        if route == "direct_call":
            # The direct route has no durable/idempotent admission at all.
            if admission_id is not None or admission_ref is not None:
                raise DispatchCorruptionError(
                    "dispatch: a direct_call dispatch carries a durable admission."
                )
        else:  # opencode_session
            if admission_id is not None:
                validate_identifier(admission_id, "admission_id")
            if admission_ref is not None:
                if admission_id is None:
                    raise DispatchCorruptionError(
                        "dispatch: an admission ref without an admission id is "
                        "malformed."
                    )
                validate_identifier(admission_ref, "admission_ref")
        if state == "admitted" and admission_id is None:
            raise DispatchCorruptionError(
                "dispatch: an admitted state requires a durable admission."
            )
        result_kind = state_row["result_kind"]
        result_ref = state_row["result_ref"]
        result_digest = state_row["result_digest"]
        if (result_kind is None) != (result_digest is None):
            raise DispatchCorruptionError("dispatch: result kind/digest disagree.")
        if (result_ref is None) != (result_digest is None):
            raise DispatchCorruptionError("dispatch: result ref/digest disagree.")
        if (state == "completed") != (result_digest is not None):
            raise DispatchCorruptionError(
                "dispatch: state/result consistency is violated."
            )
        if result_kind is not None:
            _require_nonempty_str(
                result_kind, "result_kind", max_len=MAX_RESULT_KIND_LEN
            )
        if result_ref is not None:
            validate_identifier(result_ref, "result_ref")
        if result_digest is not None:
            _require_digest(result_digest, "result_digest")
        cost_state = state_row["cost_state"]
        if cost_state not in COST_STATES:
            raise DispatchCorruptionError("dispatch: unknown cost state.")
        cost_amount = state_row["cost_amount_usd"]
        if (cost_state in _COST_STATES_WITH_AMOUNT) != (cost_amount is not None):
            raise DispatchCorruptionError(
                "dispatch: cost state/amount consistency is violated."
            )
        if cost_amount is not None:
            _exact_decimal(cost_amount, "cost_amount_usd")
        # A submitted/in-flight/ambiguous dispatch has already left the
        # not-yet-dispatched world, so it can never claim no cost was incurred.
        if (
            state in ("submitted", "admitted", "unknown")
            and cost_state == "not_incurred"
        ):
            raise DispatchCorruptionError(
                "dispatch: a submitted/ambiguous dispatch cannot claim no cost "
                "incurred."
            )
        _require_utc_timestamp(state_row["updated_at_utc"], "updated_at_utc")
        started = state_row["started_at_utc"]
        ended = state_row["ended_at_utc"]
        for optional_field in ("started_at_utc", "ended_at_utc"):
            if state_row[optional_field] is not None:
                _require_utc_timestamp(state_row[optional_field], optional_field)
        if state == "reserved":
            if started is not None or ended is not None:
                raise DispatchCorruptionError(
                    "dispatch: a reserved dispatch must not have started or ended."
                )
        elif state in ("submitted", "admitted", "unknown"):
            if started is None or ended is not None:
                raise DispatchCorruptionError(
                    "dispatch: an in-flight/ambiguous dispatch needs a start and no "
                    "end."
                )
        else:  # completed / failed
            if ended is None:
                raise DispatchCorruptionError(
                    "dispatch: a terminal dispatch needs an end timestamp."
                )
            if state == "completed" and started is None:
                raise DispatchCorruptionError(
                    "dispatch: a completed dispatch needs a start timestamp."
                )
        return DispatchRecord(
            project_id=identity_row["project_id"],
            dispatch_id=dispatch_id,
            request_id=request_id,
            task_id=task_id,
            task_fingerprint=fingerprint,
            route=route,
            accounting_bucket=bucket,
            stage=stage,
            input_digest=input_digest,
            version_digest=version_digest,
            reserve_usd=reserve_usd,
            bound_evidence_digest=bound_evidence_digest,
            created_at_utc=identity_row["created_at_utc"],
            state=state,
            current_fence=fence,
            admission_id=admission_id,
            admission_ref=admission_ref,
            result_kind=result_kind,
            result_ref=result_ref,
            result_digest=result_digest,
            cost_state=cost_state,
            cost_amount_usd=cost_amount,
            started_at_utc=state_row["started_at_utc"],
            ended_at_utc=state_row["ended_at_utc"],
            updated_at_utc=state_row["updated_at_utc"],
        )

    # -- whole-ledger integrity (shared with a later backup verifier) --------- #
    def verify_all_integrity(self) -> list[str]:
        """Return a list of integrity problems across every dispatch (empty = ok).

        Exported so a later backup verifier can reuse this instead of duplicating
        the structural/hash/link revalidation. Each entry is a short, static,
        non-secret description.
        """
        return self._integrity_problems()

    def _integrity_problems(self) -> list[str]:
        problems: list[str] = []
        rows = self._conn().execute(
            "SELECT * FROM runtime_dispatch ORDER BY rowid"
        ).fetchall()
        for index, row in enumerate(rows):
            if row["project_id"] != self.project_id:
                problems.append(f"runtime_dispatch: row {index} is a foreign project row")
                continue
            try:
                state_row = self._state_row(row["dispatch_id"])
                self._validate_dispatch_row(row, state_row)
            except Exception:  # noqa: BLE001 - a corrupt row must not abort the sweep
                problems.append(
                    f"runtime_dispatch: row {index} failed integrity revalidation"
                )
        # The identity sweep above never visits a state row whose identity row is
        # missing (dangling/orphan) or foreign, so sweep the operational rows too.
        state_rows = self._conn().execute(
            "SELECT * FROM runtime_dispatch_state ORDER BY rowid"
        ).fetchall()
        for index, state_row in enumerate(state_rows):
            if state_row["project_id"] != self.project_id:
                problems.append(
                    f"runtime_dispatch_state: row {index} is a foreign project row"
                )
                continue
            identity = self._conn().execute(
                "SELECT 1 FROM runtime_dispatch WHERE project_id = ? "
                "AND dispatch_id = ?",
                (self.project_id, state_row["dispatch_id"]),
            ).fetchone()
            if identity is None:
                problems.append(
                    f"runtime_dispatch_state: row {index} is an orphan operational row"
                )
        return problems


# --------------------------------------------------------------------------- #
# Read-only entry points (for a later backup verifier)
# --------------------------------------------------------------------------- #
class _ReadOnlyDispatchStoreView:
    """The minimal read-only surface the integrity sweep reads from a store."""

    __slots__ = ("connection", "project_id", "schema_version")

    def __init__(self, conn: sqlite3.Connection, project_id: str) -> None:
        self.connection = conn
        self.project_id = project_id
        self.schema_version = 0


def runtime_dispatch_integrity_problems(
    conn: sqlite3.Connection, project_id: str
) -> list[str]:
    """Revalidate every runtime dispatch for ``project_id`` on ``conn`` (read-only).

    Reusable by a later backup verifier. Returns a list of static problem
    descriptions (empty when every dispatch is structurally intact and internally
    consistent, including its linked task). Foreign, dangling, and corrupt rows
    are all reported. The connection is never written to.
    """
    repo = RuntimeDispatchRepository(_ReadOnlyDispatchStoreView(conn, project_id))
    return repo.verify_all_integrity()


def runtime_dispatch_remote_state(
    conn: sqlite3.Connection, project_id: str
) -> str:
    """Classify a project's durable runtime dispatches as ``remote``/``none``/``unreadable``.

    A read-only, fail-closed view of the runtime dispatch ledger for the worker's
    global arbitration and single serial remote slot (C02 §8.3/§8.5):

    * ``"remote"`` — at least one dispatch is in an outstanding state
      (:data:`RUNTIME_REMOTE_STATES`): its remote outcome is not yet known, so the
      project may still have remote work executing. This holds **regardless** of
      the settled cost state and regardless of whether any linked synthetic job is
      held.
    * ``"none"`` — every dispatch is terminal (``completed``/``failed``) or merely
      ``reserved`` (recorded before any I/O, so by ordering it cannot have been
      issued).
    * ``"unreadable"`` — the ledger cannot be **proven** intact: a missing
      ``runtime_dispatch``/``runtime_dispatch_state`` table on a store that should
      carry it, or any malformed/corrupt/foreign/dangling row (see
      :func:`runtime_dispatch_integrity_problems`), or any read error. It fails
      closed: the caller must treat it as potential remote work, never as safe.

    The connection is never written to; no identity is minted, no retry issued,
    and no reserve released.
    """
    try:
        if runtime_dispatch_integrity_problems(conn, project_id):
            return "unreadable"
        records = RuntimeDispatchRepository(
            _ReadOnlyDispatchStoreView(conn, project_id)
        ).list_dispatches()
    except (
        DispatchError,
        sqlite3.Error,
        ValueError,
        TypeError,
        KeyError,
        UnicodeError,
    ):
        return "unreadable"
    for record in records:
        if record.state in RUNTIME_REMOTE_STATES:
            return "remote"
    return "none"
