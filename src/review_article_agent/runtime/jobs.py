"""Durable per-project synthetic **job / attempt** persistence.

This module is the Step 03 storage-side core for the durable worker. It sits on
top of an already-verified :class:`~review_article_agent.storage.store.ProjectStore`
and reuses its single SQLite connection and ``BEGIN IMMEDIATE`` transaction
helper, so job state is durable **in the existing project store** — there is no
second database and no in-memory-only state.

What this deliberately **is not**
---------------------------------

* It is **not** a scientific approval, execution authorization, or export gate.
  :meth:`JobRepository.submit` records a **synthetic** job only; nothing here
  authorizes a real model/external call and no artifact is scientifically
  approved.
* It is **not** the worker, the CLI, the IPC layer, or the project-activation
  arbitration. Those are separate Step 03 tasks; this repository only persists
  and fences job/attempt/result/budget state.
* It makes **no** exactly-once *external* execution claim. Fencing protects
  **accepted local writes** (C02 §8.2); it does not fence arbitrary external
  side effects that already happened.

What it guarantees
------------------

* **Explicit identity + idempotent submit.** ``job_id`` is derived from
  ``(project_id, request_id)`` and ``UNIQUE(project_id, request_id)`` means a
  repeated submit with the same request key returns the same one row; a
  *divergent* payload for an existing key raises :class:`JobConflictError`
  rather than silently creating a second job.
* **Fenced attempts.** Each new attempt for a logical job takes the next
  incrementing ``fence``; ``job.current_fence`` is the highest issued.
  Accepting a result requires the attempt's fence to be the job's current fence,
  so a **stale attempt cannot** write an accepted result
  (:class:`StaleFenceError`).
* **At-most-once accepted local result.** ``job_accepted_result`` is unique per
  ``(job, fence)`` and per logical job, and immutable, so exactly one accepted
  local result survives parallel/retried attempts (C02 §8.2).
* **Ambiguity is never silently retried.** A prior *ambiguous* / unresolved
  outcome raises :class:`AmbiguousOutcomeError` on an automatic retry; the same
  attempt reconciles from durable state or escalates to a human
  (:meth:`JobRepository.reconcile`) **without** minting a new identity.
* **Bounded safe retries.** Retries are permitted only for a **safe predispatch**
  prior attempt (terminal, unresolved-bit clear, and never externally admitted)
  and are capped at :data:`MAX_SAFE_RETRIES` (=2); a retry always requires an
  explicit ``retry_authorized_by`` actor label.
* **Reserve before dispatch.** A synthetic external dispatch must record an
  exact-decimal reserve first; the project cap (default **USD 25**) and stage cap
  (default **USD 10**) refuse any reservation that would exceed them, and an
  ``unknown`` cost **retains** its reserve. A ``known``/``estimated`` actual that
  overruns its reserve is counted at the larger amount, so "everything counts"
  against the cap (C02 §5.6/§6.3).
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, localcontext

from ..domain.errors import StoreError
from ..domain.records import canonical_json, validate_identifier

# --------------------------------------------------------------------------- #
# Vocabulary
# --------------------------------------------------------------------------- #
#: Every durable job state (C02 §8.3, plus ``interrupted`` from §8.1/step 03).
JOB_STATES = (
    "queued",
    "admitted",
    "running",
    "waiting_human",
    "waiting_external",
    "paused",
    "cancelled",
    "completed",
    "failed",
    "interrupted",
    "reconciliation_required",
)

#: Terminal *for scheduling*. ``cancelled`` is terminal for scheduling only: an
#: ambiguous remote outcome stays unresolved (C02 §8.4).
TERMINAL_JOB_STATES = frozenset({"completed", "failed", "cancelled"})

#: Attempt lifecycle states.
ATTEMPT_STATES = (
    "queued",
    "admitted",
    "running",
    "succeeded",
    "definitive_failure",
    "ambiguous",
    "interrupted",
)

#: Attempt states that still occupy the "current attempt" slot.
ACTIVE_ATTEMPT_STATES = frozenset({"queued", "admitted", "running"})

#: Dispatch kinds. ``external_synthetic`` is the only kind that may carry a
#: budget reserve; both are synthetic (no real external/model call is made here).
DISPATCH_KINDS = ("local", "external_synthetic")

#: Cost states (C02 §6.3).
COST_STATES = ("known", "estimated", "unknown", "not_incurred")

#: Reconciliation resolutions (never mint a new identity).
RECONCILIATION_RESOLUTIONS = (
    "accepted_local_result",
    "definitive_failure",
    "human_escalation",
    "keep_unresolved",
)

#: Conservative operational retry bound: at most **2** safe predispatch retries
#: per logical job. This is deliberately small and only ever applies to a prior
#: attempt that was never externally admitted and left no unresolved outcome.
MAX_SAFE_RETRIES = 2

#: Selected cost caps (C02 §5.6 / decision 002 POL-OPS-COST): exact decimals.
DEFAULT_PROJECT_CAP_USD = "25"
DEFAULT_STAGE_CAP_USD = "10"

#: Cost states whose settlement must carry an exact ``amount_usd``.
_COST_STATES_WITH_AMOUNT = frozenset({"known", "estimated"})

#: Job states that hold scheduling: a late durable event must never silently
#: leave them (C02 §8.4 — pause is not terminal, cancel is terminal for
#: scheduling only).
_SCHEDULING_HELD_STATES = frozenset({"paused", "cancelled"})

#: Bounds on an accepted exact decimal, so a hostile value such as
#: ``'1e999999999'`` (or ``'0e-999999999'``) is rejected cleanly instead of
#: format-expanding into a multi-megabyte string. ``adjusted`` is the base-10
#: position of the most significant digit.
_MAX_ADJUSTED_EXPONENT = 24
_MAX_SIGNIFICANT_DIGITS = 40
_MAX_DECIMAL_INPUT_LEN = 128
_MAX_DECIMAL_INPUT_BITS = 256

#: Job states from which a new attempt may start. Waiting gates
#: (``waiting_human``/``waiting_external``), ``reconciliation_required``, and
#: the terminal/held states must not be auto-bypassed by a fresh attempt.
STARTABLE_JOB_STATES = frozenset({"queued", "failed", "interrupted"})

#: Key under which a checkpoint carries a computed-but-uncommitted local result.
_LOCAL_RESULT_KEY = "local_result"


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #
class JobError(StoreError):
    """A durable job/attempt operation failed."""


class JobValidationError(JobError, ValueError):
    """A job/attempt/budget input was structurally invalid (fail closed)."""


class JobConflictError(JobError):
    """A divergent idempotent submit or a conflicting accepted result."""


class JobStateError(JobError):
    """The requested operation is not permitted in the current state."""


class StaleFenceError(JobStateError):
    """The attempt's fence is not the job's current fence (stale attempt)."""


class AmbiguousOutcomeError(JobStateError):
    """A prior ambiguous/unresolved outcome forbids an automatic retry."""


class RetryLimitExceeded(JobStateError):
    """The bounded safe-predispatch retry budget is exhausted."""


class BudgetExceededError(JobError):
    """A reserve would breach the project or stage cost cap."""


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _require_nonempty_str(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise JobValidationError(f"{field}: must be a string")
    if value == "" or value != value.strip():
        raise JobValidationError(
            f"{field}: must be non-empty with no leading/trailing whitespace"
        )
    return value


def _require_enum(value: object, allowed: tuple[str, ...], field: str) -> str:
    if value not in allowed:
        raise JobValidationError(f"{field}: {value!r} is not one of {list(allowed)}")
    return value  # type: ignore[return-value]


def _bounded_decimal_amount(value: object, field: str) -> Decimal:
    """Parse and bound one exact decimal, returning its :class:`Decimal`.

    Fail-closed bounds applied **before** any formatting/expansion: a bounded
    input length (strings), a bounded integer width, bounded significant digits,
    and — critically — a bounded base-10 exponent *even for zero*, so
    ``'0e-999999999'`` cannot expand into a billion-character string. Rejects
    ``float``/``bool`` (never an inexact binary amount), non-finite values, and
    negatives. Error messages never echo the offending value.
    """
    if isinstance(value, bool) or isinstance(value, float):
        raise JobValidationError(
            f"{field}: must be an exact decimal string, not a float or bool"
        )
    if isinstance(value, str):
        if len(value) > _MAX_DECIMAL_INPUT_LEN:
            raise JobValidationError(f"{field}: decimal string is too long")
        try:
            amount = Decimal(value)
        except (InvalidOperation, ValueError) as exc:
            raise JobValidationError(f"{field}: is not a valid decimal amount") from exc
    elif isinstance(value, int):
        if value.bit_length() > _MAX_DECIMAL_INPUT_BITS:
            raise JobValidationError(f"{field}: integer is out of the permitted range")
        amount = Decimal(value)
    elif isinstance(value, Decimal):
        amount = value
    else:
        raise JobValidationError(
            f"{field}: must be an exact decimal string (got {type(value).__name__})"
        )
    if not amount.is_finite():
        raise JobValidationError(f"{field}: must be a finite decimal amount")
    _sign, digits, exponent = amount.as_tuple()
    if len(digits) > _MAX_SIGNIFICANT_DIGITS:
        raise JobValidationError(f"{field}: has too many significant digits")
    # ``adjusted`` is the position of the most significant digit for a non-zero
    # value; for zero the raw exponent governs how far ``format(..., 'f')`` would
    # pad, so bound it directly.
    magnitude = exponent + len(digits) - 1 if amount != 0 else exponent
    if abs(magnitude) > _MAX_ADJUSTED_EXPONENT:
        raise JobValidationError(f"{field}: magnitude is out of the permitted range")
    if amount < 0:
        raise JobValidationError(f"{field}: must not be negative")
    return amount


def _exact_decimal(
    value: object, field: str, *, strictly_positive: bool = False
) -> str:
    """Return the canonical exact-decimal string for ``value`` (fail closed).

    See :func:`_bounded_decimal_amount` for the bounds; optionally requires a
    strictly positive amount.
    """
    amount = _bounded_decimal_amount(value, field)
    if strictly_positive and amount == 0:
        raise JobValidationError(f"{field}: must be greater than zero")
    return format(amount, "f")


def validate_reserve_usd(value: object) -> str:
    """Validate and canonicalise a dispatch reserve: an exact, strictly positive
    decimal within the bounded magnitude/significant-digit caps.

    Public wrapper around :func:`_exact_decimal` so the worker engine can reject
    an out-of-bounds reserve (a huge exponent, zero, a negative, or a float/bool)
    **before** persisting any durable job, instead of leaving a job stuck queued.
    Raises :class:`JobValidationError`.
    """
    return _exact_decimal(value, "reserve_usd", strictly_positive=True)


def _add_exact(left: Decimal, right: Decimal) -> Decimal:
    """Add bounded decimal amounts without the process context rounding cents.

    Include every fractional position and a carry digit. This also covers
    subtraction (a negative right operand) and accumulated project totals.
    """
    lowest = min(left.as_tuple().exponent, right.as_tuple().exponent)
    highest = max(left.adjusted(), right.adjusted(), 0)
    with localcontext() as context:
        context.prec = max(1, highest - lowest + 2)
        return left + right


def _fingerprint(payload: object) -> str:
    return hashlib.sha256(canonical_json(payload)).hexdigest()


def _job_id_for(project_id: str, request_id: str) -> str:
    digest = hashlib.sha256(
        (project_id + "\x00" + request_id).encode("utf-8")
    ).hexdigest()
    return "job_" + digest[:32]


def _json_dumps(value: object) -> str:
    return canonical_json(value).decode("utf-8")


def _json_loads(text: str) -> object:
    return json.loads(text)


# --------------------------------------------------------------------------- #
# Value objects
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Job:
    """One durable logical job row (raw, as stored)."""

    project_id: str
    job_id: str
    request_id: str
    fingerprint: str
    task_kind: str
    parameters: object
    state: str
    outcome_unresolved: bool
    resume_state: str | None
    current_attempt_id: str | None
    current_fence: int
    attempts_total: int
    retries_used: int
    accepted_fence: int | None
    created_at_utc: str
    updated_at_utc: str


@dataclass(frozen=True)
class Attempt:
    """One durable dispatch attempt row (raw, as stored)."""

    project_id: str
    attempt_id: str
    job_id: str
    fence: int
    attempt_no: int
    dispatch_kind: str
    state: str
    prior_outcome: str
    retry_authorized_by: str | None
    outcome_unresolved: bool
    admission_id: str | None
    external_ref: str | None
    checkpoint: object
    reserve_usd: str | None
    cost_state: str
    cost_amount_usd: str | None
    started_at_utc: str
    ended_at_utc: str | None


@dataclass(frozen=True)
class AcceptedResult:
    """The single fenced accepted local result of a logical job."""

    project_id: str
    job_id: str
    attempt_id: str
    fence: int
    result_kind: str
    result: object
    result_digest: str
    accepted_at_utc: str


@dataclass(frozen=True)
class BudgetUsage:
    """Exact-decimal budget usage against the project and stage caps."""

    project_cap_usd: str
    stage_cap_usd: str
    project_reserved_usd: str
    stage_reserved_usd: str | None
    project_remaining_usd: str
    stage_remaining_usd: str | None


@dataclass(frozen=True)
class RecoveryOutcome:
    """The disposition of a recovered attempt (no new identity is created)."""

    job_id: str
    attempt_id: str
    disposition: str  # retry_safe | reconciliation_required |
    #                   accept_pending_local_result | terminal


# --------------------------------------------------------------------------- #
# Repository
# --------------------------------------------------------------------------- #
class JobRepository:
    """Durable synthetic jobs/attempts over a verified store.

    Typical use::

        store = ProjectStore.open_at(app_dir, expected_project_id=pid)
        jobs = JobRepository(store)

        job = jobs.submit("req-1", task_kind="synthetic_probe", parameters={"n": 1})
        att = jobs.begin_attempt(
            job.job_id,
            dispatch_kind="external_synthetic",
            stage="stage-1",
            reserve_usd="0.10",
        )
        jobs.mark_admitted(att.attempt_id, admission_id="adm-1")
        result = jobs.accept_result(att.attempt_id, result_kind="synthetic", result={"ok": True})
        # -> exactly one accepted local result; a stale fence can never accept.
    """

    def __init__(self, store) -> None:
        self._store = store

    @property
    def project_id(self) -> str:
        return self._store.project_id

    def _conn(self) -> sqlite3.Connection:
        return self._store.connection

    # -- submit / reads ------------------------------------------------------ #
    def submit(
        self,
        request_id: str,
        *,
        task_kind: str,
        parameters: object | None = None,
    ) -> Job:
        """Idempotently submit one synthetic logical job.

        ``request_id`` is the client-supplied idempotency key. A repeated submit
        with the same key and the same ``(task_kind, parameters)`` fingerprint
        returns the existing row; a *divergent* payload for an existing key
        raises :class:`JobConflictError`.
        """
        request_id = validate_identifier(request_id, "request_id")
        task_kind = _require_nonempty_str(task_kind, "task_kind")
        if parameters is None:
            parameters = {}
        fingerprint = _fingerprint({"task_kind": task_kind, "parameters": parameters})
        job_id = _job_id_for(self.project_id, request_id)

        now = _utc_now()
        # The idempotency read and the insert share one BEGIN IMMEDIATE
        # transaction, so two connections racing the same request key cannot
        # both insert: the loser blocks on the write lock, then observes the
        # committed row.
        with self._store.transaction():
            existing = self._job_row_by_request(request_id)
            if existing is not None:
                if existing["fingerprint"] != fingerprint:
                    raise JobConflictError(
                        f"request {request_id!r} was already submitted with a "
                        f"different task payload; refusing to create a second job."
                    )
                return self._row_to_job(existing)
            self._conn().execute(
                "INSERT INTO job (project_id, job_id, request_id, fingerprint, "
                " task_kind, parameters_json, state, outcome_unresolved, "
                " resume_state, current_attempt_id, current_fence, attempts_total, "
                " retries_used, accepted_fence, created_at_utc, updated_at_utc) "
                "VALUES (?, ?, ?, ?, ?, ?, 'queued', 0, NULL, NULL, 0, 0, 0, NULL, ?, ?)",
                (
                    self.project_id,
                    job_id,
                    request_id,
                    fingerprint,
                    task_kind,
                    _json_dumps(parameters),
                    now,
                    now,
                ),
            )
        stored = self.get(job_id)
        assert stored is not None  # just committed
        return stored

    def get(self, job_id: str) -> Job | None:
        job_id = validate_identifier(job_id, "job_id")
        row = self._job_row(job_id)
        return None if row is None else self._row_to_job(row)

    def list(self, *, state: str | None = None) -> list[Job]:
        if state is not None:
            _require_enum(state, JOB_STATES, "state")
            rows = self._conn().execute(
                "SELECT * FROM job WHERE project_id = ? AND state = ? ORDER BY rowid",
                (self.project_id, state),
            ).fetchall()
        else:
            rows = self._conn().execute(
                "SELECT * FROM job WHERE project_id = ? ORDER BY rowid",
                (self.project_id,),
            ).fetchall()
        return [self._row_to_job(row) for row in rows]

    def attempts(self, job_id: str) -> list[Attempt]:
        job_id = validate_identifier(job_id, "job_id")
        rows = self._conn().execute(
            "SELECT * FROM job_attempt WHERE project_id = ? AND job_id = ? "
            "ORDER BY attempt_no",
            (self.project_id, job_id),
        ).fetchall()
        return [self._row_to_attempt(row) for row in rows]

    def attempt(self, attempt_id: str) -> Attempt | None:
        attempt_id = validate_identifier(attempt_id, "attempt_id")
        row = self._attempt_row(attempt_id)
        return None if row is None else self._row_to_attempt(row)

    def accepted_result(self, job_id: str) -> AcceptedResult | None:
        job_id = validate_identifier(job_id, "job_id")
        row = self._conn().execute(
            "SELECT * FROM job_accepted_result WHERE project_id = ? AND job_id = ?",
            (self.project_id, job_id),
        ).fetchone()
        return None if row is None else self._row_to_accepted(row)

    # -- attempts ------------------------------------------------------------ #
    def begin_attempt(
        self,
        job_id: str,
        *,
        dispatch_kind: str = "local",
        stage: str | None = None,
        reserve_usd: str | None = None,
        retry_authorized_by: str | None = None,
    ) -> Attempt:
        """Claim the next fenced attempt for a logical job.

        A new attempt takes ``job.current_fence + 1``. A retry (a second or later
        attempt) is only permitted for a **safe predispatch** prior attempt and
        only with an explicit ``retry_authorized_by`` actor label, and is capped
        at :data:`MAX_SAFE_RETRIES`. An ``external_synthetic`` attempt must carry
        an exact-decimal ``reserve_usd`` and a ``stage``, and the reserve is
        persisted **before** any dispatch (refused if a cap would be breached).
        """
        job_id = validate_identifier(job_id, "job_id")
        _require_enum(dispatch_kind, DISPATCH_KINDS, "dispatch_kind")

        # Validate the dispatch inputs up front (pure, no state read).
        if dispatch_kind == "external_synthetic":
            if stage is None:
                raise JobValidationError(
                    "external_synthetic dispatch requires a stage scope."
                )
            stage = validate_identifier(stage, "stage")
            if reserve_usd is None:
                raise JobValidationError(
                    "external_synthetic dispatch requires an exact-decimal "
                    "reserve_usd recorded before dispatch."
                )
            reserve = _exact_decimal(reserve_usd, "reserve_usd", strictly_positive=True)
        else:
            if reserve_usd is not None:
                raise JobValidationError(
                    "a local dispatch must not carry a budget reserve."
                )
            reserve = None

        attempt_id = "att_" + uuid.uuid4().hex
        now = _utc_now()

        # One BEGIN IMMEDIATE covers the state authorization, the fence bump, and
        # the reserve, so a concurrent pause or a racing second begin cannot slip
        # between the read and the write (no double reserve, no bypassed pause).
        with self._store.transaction():
            job = self._require_job(job_id)
            if job.state in ("paused", "cancelled", "completed"):
                raise JobStateError(
                    f"job {job_id!r} is {job.state!r}; it does not schedule new work."
                )

            prior: Attempt | None = None
            if job.current_attempt_id is not None:
                prior = self._require_attempt(job.current_attempt_id)
                if prior.state in ACTIVE_ATTEMPT_STATES:
                    raise JobStateError(
                        f"job {job_id!r} already has an active attempt "
                        f"{prior.attempt_id!r}; refusing to start a second."
                    )

            retries_used = job.retries_used
            prior_outcome = "none"
            if prior is not None:
                if prior.state == "succeeded":
                    raise JobStateError(
                        f"the prior attempt of job {job_id!r} succeeded; the job is done."
                    )
                prior_outcome = prior.state
                if prior.outcome_unresolved or prior.state == "ambiguous":
                    raise AmbiguousOutcomeError(
                        f"the prior attempt of job {job_id!r} has an ambiguous/"
                        f"unresolved outcome; reconcile the same attempt or escalate "
                        f"to a human instead of dispatching a new identity."
                    )
                if prior.admission_id is not None:
                    raise AmbiguousOutcomeError(
                        f"the prior attempt of job {job_id!r} was externally admitted; "
                        f"a safe predispatch retry is not permitted."
                    )
                if retries_used >= MAX_SAFE_RETRIES:
                    raise RetryLimitExceeded(
                        f"job {job_id!r} has used its {MAX_SAFE_RETRIES} safe "
                        f"predispatch retries; refusing a further retry."
                    )
                if retry_authorized_by is None:
                    raise JobStateError(
                        f"retrying job {job_id!r} requires an explicit "
                        f"retry_authorized_by actor label."
                    )
                retry_authorized_by = _require_nonempty_str(
                    retry_authorized_by, "retry_authorized_by"
                )
                retries_used += 1

            # A job parked at a waiting gate or awaiting reconciliation must not
            # be auto-bypassed by a fresh attempt; only queued/failed/interrupted
            # jobs may start. (An ambiguous/admitted prior already raised above.)
            if job.state not in STARTABLE_JOB_STATES:
                raise JobStateError(
                    f"job {job_id!r} is {job.state!r}; only "
                    f"{sorted(STARTABLE_JOB_STATES)} jobs may start a new attempt."
                )

            fence = job.current_fence + 1
            attempt_no = job.attempts_total + 1

            if reserve is not None:
                self._check_caps_and_reserve(
                    job_id=job_id,
                    attempt_id=attempt_id,
                    stage=stage,
                    amount=reserve,
                )
            self._conn().execute(
                "INSERT INTO job_attempt (project_id, attempt_id, job_id, fence, "
                " attempt_no, dispatch_kind, state, prior_outcome, "
                " retry_authorized_by, outcome_unresolved, admission_id, "
                " external_ref, checkpoint_json, reserve_usd, cost_state, "
                " cost_amount_usd, started_at_utc, ended_at_utc) "
                "VALUES (?, ?, ?, ?, ?, ?, 'queued', ?, ?, 0, NULL, NULL, NULL, ?, "
                " 'not_incurred', NULL, ?, NULL)",
                (
                    self.project_id,
                    attempt_id,
                    job_id,
                    fence,
                    attempt_no,
                    dispatch_kind,
                    prior_outcome,
                    retry_authorized_by,
                    reserve,
                    now,
                ),
            )
            self._conn().execute(
                "UPDATE job SET current_attempt_id = ?, current_fence = ?, "
                " attempts_total = ?, retries_used = ?, state = 'queued', "
                " resume_state = NULL, updated_at_utc = ? "
                "WHERE project_id = ? AND job_id = ?",
                (
                    attempt_id,
                    fence,
                    attempt_no,
                    retries_used,
                    now,
                    self.project_id,
                    job_id,
                ),
            )
        stored = self.attempt(attempt_id)
        assert stored is not None
        return stored

    def mark_admitted(
        self,
        attempt_id: str,
        *,
        admission_id: str,
        external_ref: str | None = None,
    ) -> Attempt:
        """Record a durable admission receipt for the attempt (admission ≠ completion).

        Requires the attempt to be the job's **current** attempt; a late event on
        a stale attempt is refused. Admission never leaves a paused/cancelled job.
        """
        attempt_id = validate_identifier(attempt_id, "attempt_id")
        admission_id = validate_identifier(admission_id, "admission_id")
        if external_ref is not None:
            external_ref = _require_nonempty_str(external_ref, "external_ref")
        with self._store.transaction():
            attempt = self._require_attempt(attempt_id)
            job = self._require_current_attempt(attempt)
            if attempt.state == "admitted" and attempt.admission_id == admission_id:
                return attempt  # idempotent replay of the same receipt
            if attempt.state != "queued":
                raise JobStateError(
                    f"attempt {attempt_id!r} is {attempt.state!r}; cannot record "
                    f"admission."
                )
            self._conn().execute(
                "UPDATE job_attempt SET state = 'admitted', admission_id = ?, "
                " external_ref = ? WHERE project_id = ? AND attempt_id = ?",
                (admission_id, external_ref, self.project_id, attempt_id),
            )
            self._set_job_state(job.job_id, "admitted", preserve_held=True)
        return self._require_attempt(attempt_id)

    def mark_running(self, attempt_id: str) -> Attempt:
        attempt_id = validate_identifier(attempt_id, "attempt_id")
        with self._store.transaction():
            attempt = self._require_attempt(attempt_id)
            job = self._require_current_attempt(attempt)
            if attempt.state == "running":
                return attempt
            if attempt.state != "admitted":
                raise JobStateError(
                    f"attempt {attempt_id!r} is {attempt.state!r}; only an admitted "
                    f"attempt can run."
                )
            self._conn().execute(
                "UPDATE job_attempt SET state = 'running' "
                "WHERE project_id = ? AND attempt_id = ?",
                (self.project_id, attempt_id),
            )
            self._set_job_state(job.job_id, "running", preserve_held=True)
        return self._require_attempt(attempt_id)

    def record_checkpoint(self, attempt_id: str, checkpoint: object) -> Attempt:
        """Persist a durable checkpoint on the current, non-terminal attempt."""
        attempt_id = validate_identifier(attempt_id, "attempt_id")
        payload = _json_dumps(checkpoint)
        with self._store.transaction():
            attempt = self._require_attempt(attempt_id)
            self._require_current_attempt(attempt)
            if attempt.state not in ACTIVE_ATTEMPT_STATES:
                raise JobStateError(
                    f"attempt {attempt_id!r} is {attempt.state!r}; "
                    f"cannot checkpoint a terminal attempt."
                )
            self._conn().execute(
                "UPDATE job_attempt SET checkpoint_json = ? "
                "WHERE project_id = ? AND attempt_id = ?",
                (payload, self.project_id, attempt_id),
            )
        return self._require_attempt(attempt_id)

    def mark_definitive_failure(self, attempt_id: str) -> Attempt:
        """Record a non-billing, definitive failure of the current attempt.

        A definitive failure is a *resolved* outcome: it clears the unresolved
        bit. From an active attempt it is direct; from an ``ambiguous``/
        ``interrupted`` attempt it is only reached through explicit
        reconciliation (:meth:`reconcile`) — never implicitly. A stale or
        already-``succeeded`` attempt is refused.
        """
        return self._mark_definitive_failure(attempt_id, allow_from_unresolved=False)

    def _mark_definitive_failure(
        self, attempt_id: str, *, allow_from_unresolved: bool
    ) -> Attempt:
        attempt_id = validate_identifier(attempt_id, "attempt_id")
        with self._store.transaction():
            attempt = self._require_attempt(attempt_id)
            job = self._require_current_attempt(attempt)
            if attempt.state == "definitive_failure":
                return attempt  # idempotent
            if attempt.state == "succeeded":
                raise JobStateError(
                    f"attempt {attempt_id!r} already succeeded; cannot mark it failed."
                )
            if attempt.state in ("ambiguous", "interrupted") and not allow_from_unresolved:
                raise JobStateError(
                    f"attempt {attempt_id!r} is {attempt.state!r}; a definitive "
                    f"failure of an unresolved attempt requires explicit "
                    f"reconciliation."
                )
            now = _utc_now()
            self._conn().execute(
                "UPDATE job_attempt SET state = 'definitive_failure', "
                " outcome_unresolved = 0, ended_at_utc = ? "
                "WHERE project_id = ? AND attempt_id = ?",
                (now, self.project_id, attempt_id),
            )
            self._conn().execute(
                "UPDATE job SET outcome_unresolved = 0 "
                "WHERE project_id = ? AND job_id = ?",
                (self.project_id, job.job_id),
            )
            self._set_job_state(job.job_id, "failed", preserve_held=True)
        return self._require_attempt(attempt_id)

    def mark_ambiguous(self, attempt_id: str) -> Attempt:
        """Mark the current attempt ambiguous: the remote outcome is unknown.

        Refused on a stale attempt or an already-``succeeded`` attempt, so it can
        never regress a completed job.
        """
        attempt_id = validate_identifier(attempt_id, "attempt_id")
        with self._store.transaction():
            attempt = self._require_attempt(attempt_id)
            job = self._require_current_attempt(attempt)
            if attempt.state == "succeeded":
                raise JobStateError(
                    f"attempt {attempt_id!r} already succeeded; refusing to mark it "
                    f"ambiguous."
                )
            if attempt.state == "definitive_failure":
                raise JobStateError(
                    f"attempt {attempt_id!r} is a definitive failure; refusing to "
                    f"mark it ambiguous."
                )
            now = _utc_now()
            self._conn().execute(
                "UPDATE job_attempt SET state = 'ambiguous', outcome_unresolved = 1, "
                " ended_at_utc = ? WHERE project_id = ? AND attempt_id = ?",
                (now, self.project_id, attempt_id),
            )
            self._conn().execute(
                "UPDATE job SET outcome_unresolved = 1 "
                "WHERE project_id = ? AND job_id = ?",
                (self.project_id, job.job_id),
            )
            self._set_job_state(
                job.job_id, "reconciliation_required", preserve_held=True
            )
        return self._require_attempt(attempt_id)

    # -- results ------------------------------------------------------------- #
    def accept_result(
        self,
        attempt_id: str,
        *,
        result_kind: str,
        result: object,
    ) -> AcceptedResult:
        """Accept the attempt's local result if its fence is current.

        A stale fence raises :class:`StaleFenceError`; a repeated acceptance of
        the same fence + same digest is idempotent; a divergent acceptance for an
        already-accepted job raises :class:`JobConflictError`. On success the job
        becomes ``completed`` and any unresolved bit is cleared, so accepting from
        a *current* ambiguous attempt is exactly the same-identity reconciliation
        the contract permits. A ``cancelled``/``paused`` job is **not** silently
        advanced: the accepted result is retained and the unresolved bit cleared,
        but the scheduling state stays ``cancelled``/``paused``.
        """
        attempt_id = validate_identifier(attempt_id, "attempt_id")
        result_kind = _require_nonempty_str(result_kind, "result_kind")
        digest = _fingerprint({"result_kind": result_kind, "result": result})
        payload = _json_dumps(result)
        now = _utc_now()

        with self._store.transaction():
            attempt = self._require_attempt(attempt_id)
            job = self._require_current_attempt(attempt)
            existing = self._conn().execute(
                "SELECT * FROM job_accepted_result WHERE project_id = ? AND job_id = ?",
                (self.project_id, job.job_id),
            ).fetchone()
            if existing is not None:
                if existing["fence"] == attempt.fence and existing["result_digest"] == digest:
                    return self._row_to_accepted(existing)
                raise JobConflictError(
                    f"job {job.job_id!r} already has an accepted result at fence "
                    f"{existing['fence']}; refusing to overwrite it."
                )
            self._conn().execute(
                "INSERT INTO job_accepted_result (project_id, job_id, attempt_id, "
                " fence, result_kind, result_json, result_digest, accepted_at_utc) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    self.project_id,
                    job.job_id,
                    attempt_id,
                    attempt.fence,
                    result_kind,
                    payload,
                    digest,
                    now,
                ),
            )
            self._conn().execute(
                "UPDATE job_attempt SET state = 'succeeded', outcome_unresolved = 0, "
                " ended_at_utc = ? WHERE project_id = ? AND attempt_id = ?",
                (now, self.project_id, attempt_id),
            )
            if job.state in _SCHEDULING_HELD_STATES:
                # Never silently undo a cancel/pause; retain the accepted write
                # and clear the remote unresolved bit only.
                self._conn().execute(
                    "UPDATE job SET outcome_unresolved = 0, accepted_fence = ?, "
                    " updated_at_utc = ? WHERE project_id = ? AND job_id = ?",
                    (attempt.fence, now, self.project_id, job.job_id),
                )
            else:
                self._conn().execute(
                    "UPDATE job SET state = 'completed', outcome_unresolved = 0, "
                    " accepted_fence = ?, resume_state = NULL, updated_at_utc = ? "
                    "WHERE project_id = ? AND job_id = ?",
                    (attempt.fence, now, self.project_id, job.job_id),
                )
        stored = self._conn().execute(
            "SELECT * FROM job_accepted_result WHERE project_id = ? AND job_id = ?",
            (self.project_id, self._require_attempt(attempt_id).job_id),
        ).fetchone()
        assert stored is not None
        return self._row_to_accepted(stored)

    # -- pause / resume / cancel / escalation -------------------------------- #
    def pause(self, job_id: str) -> Job:
        """Pause a job: stop scheduling; preserve durable and ambiguous state."""
        job_id = validate_identifier(job_id, "job_id")
        with self._store.transaction():
            job = self._require_job(job_id)
            if job.state in ("completed", "cancelled"):
                raise JobStateError(f"job {job_id!r} is {job.state!r}; cannot pause.")
            if job.state != "paused":
                self._conn().execute(
                    "UPDATE job SET resume_state = ?, state = 'paused', "
                    " updated_at_utc = ? WHERE project_id = ? AND job_id = ?",
                    (job.state, _utc_now(), self.project_id, job_id),
                )
        return self._require_job(job_id)

    def resume(self, job_id: str) -> Job:
        job_id = validate_identifier(job_id, "job_id")
        with self._store.transaction():
            job = self._require_job(job_id)
            if job.state != "paused":
                raise JobStateError(f"job {job_id!r} is not paused.")
            restored = job.resume_state or "queued"
            accepted = self._conn().execute(
                "SELECT 1 FROM job_accepted_result WHERE project_id = ? AND job_id = ?",
                (self.project_id, job_id),
            ).fetchone()
            if accepted is not None:
                # A result accepted while paused completes the job on resume.
                restored = "completed"
            self._conn().execute(
                "UPDATE job SET state = ?, resume_state = NULL, updated_at_utc = ? "
                "WHERE project_id = ? AND job_id = ?",
                (restored, _utc_now(), self.project_id, job_id),
            )
        return self._require_job(job_id)

    def cancel(self, job_id: str) -> Job:
        """Cancel scheduling; a still-unknown remote outcome stays unresolved."""
        job_id = validate_identifier(job_id, "job_id")
        with self._store.transaction():
            job = self._require_job(job_id)
            if job.state == "completed":
                raise JobStateError(f"job {job_id!r} is already completed.")
            self._conn().execute(
                "UPDATE job SET state = 'cancelled', resume_state = NULL, "
                " updated_at_utc = ? WHERE project_id = ? AND job_id = ?",
                (_utc_now(), self.project_id, job_id),
            )
        return self._require_job(job_id)

    def escalate_to_human(self, job_id: str) -> Job:
        job_id = validate_identifier(job_id, "job_id")
        with self._store.transaction():
            self._require_job(job_id)
            self._set_job_state(job_id, "waiting_human", preserve_held=True)
        return self._require_job(job_id)

    def mark_waiting_external(self, job_id: str) -> Job:
        job_id = validate_identifier(job_id, "job_id")
        with self._store.transaction():
            self._require_job(job_id)
            self._set_job_state(job_id, "waiting_external", preserve_held=True)
        return self._require_job(job_id)

    # -- trusted same-attempt resume (proven undispatched only) -------------- #
    def resume_proven_undispatched(self, attempt_id: str) -> Attempt:
        """Return a *proven-never-dispatched* current attempt to ``queued``.

        This is the **only** trusted operation that reopens an existing attempt
        identity after an interruption. It is valid only when the caller has
        proven — by ordering (no continuity/admission checkpoint was durably
        recorded before the crash a dispatch could have been issued) or by an
        authoritative backend ``absent`` lookup — that the external request was
        **never** issued. It:

        * refuses a stale (non-current) attempt and any attempt that carries an
          external admission (``admission_id`` is not ``None``): a missing local
          receipt alone is never proof that nothing was dispatched;
        * keeps the **same** ``attempt_id``/``fence`` — no new identity, no new
          reserve, no fence bump;
        * **durably records the proven-un dispatched proof** by replacing the
          attempt checkpoint with a ``proven_undispatched`` marker (dropping any
          continuity/phase), so a queued attempt is never mistaken for
          potentially-issued remote work that would pin arbitration;
        * clears the job's unresolved bit and returns it to ``queued`` unless the
          job is scheduling-held (``paused``/``cancelled``), whose state is
          preserved (a resume still cannot bypass an explicit pause/cancel).
        """
        attempt_id = validate_identifier(attempt_id, "attempt_id")
        with self._store.transaction():
            attempt = self._require_attempt(attempt_id)
            job = self._require_current_attempt(attempt)
            if attempt.admission_id is not None:
                raise JobStateError(
                    f"attempt {attempt_id!r} carries an external admission; it "
                    f"cannot be reopened as an undispatched same attempt."
                )
            if attempt.state not in ("queued", "interrupted", "ambiguous"):
                raise JobStateError(
                    f"attempt {attempt_id!r} is {attempt.state!r}; only a "
                    f"queued/interrupted/ambiguous undispatched attempt may be "
                    f"reopened."
                )
            # The trusted proof is recorded here: no continuity id/phase survives,
            # so the predicate treats the attempt as pinning no external work.
            self._conn().execute(
                "UPDATE job_attempt SET state = 'queued', outcome_unresolved = 0, "
                " ended_at_utc = NULL, checkpoint_json = ? "
                "WHERE project_id = ? AND attempt_id = ?",
                (
                    _json_dumps({"phase": "proven_undispatched"}),
                    self.project_id,
                    attempt_id,
                ),
            )
            self._conn().execute(
                "UPDATE job SET outcome_unresolved = 0 "
                "WHERE project_id = ? AND job_id = ?",
                (self.project_id, job.job_id),
            )
            self._set_job_state(job.job_id, "queued", preserve_held=True)
        return self._require_attempt(attempt_id)

    # -- recovery / reconciliation ------------------------------------------- #
    def recover(self, attempt_id: str) -> RecoveryOutcome:
        """Recover the current attempt after a worker restart, from durable state.

        * a **local** predispatch attempt (no external side effect) becomes a
          **safe** ``interrupted`` disposition, retryable;
        * a **synthetic external** attempt is **always** treated as potentially
          ambiguous: the absence of a local admission receipt is *not* proof the
          request was never sent, so the reserve and the unresolved bit are
          retained and the job becomes ``reconciliation_required`` (or
          ``accept_pending_local_result`` when a local result was checkpointed);
        * a terminal attempt is left untouched.

        No new identity is ever minted; the same attempt is reconciled. Paused or
        cancelled scheduling state is never silently undone.
        """
        attempt_id = validate_identifier(attempt_id, "attempt_id")
        with self._store.transaction():
            attempt = self._require_attempt(attempt_id)
            job = self._require_current_attempt(attempt)
            if attempt.state not in ACTIVE_ATTEMPT_STATES:
                return RecoveryOutcome(
                    job_id=attempt.job_id,
                    attempt_id=attempt_id,
                    disposition="terminal",
                )
            checkpoint = attempt.checkpoint
            has_local_result = (
                isinstance(checkpoint, dict) and _LOCAL_RESULT_KEY in checkpoint
            )
            now = _utc_now()
            if attempt.dispatch_kind != "external_synthetic":
                # Local-only: no external side effect to reconcile.
                if has_local_result:
                    self._conn().execute(
                        "UPDATE job_attempt SET state = 'interrupted', "
                        " ended_at_utc = ? WHERE project_id = ? AND attempt_id = ?",
                        (now, self.project_id, attempt_id),
                    )
                    self._set_job_state(job.job_id, "interrupted", preserve_held=True)
                    disposition = "accept_pending_local_result"
                else:
                    self._conn().execute(
                        "UPDATE job_attempt SET state = 'interrupted' "
                        "WHERE project_id = ? AND attempt_id = ?",
                        (self.project_id, attempt_id),
                    )
                    self._set_job_state(job.job_id, "interrupted", preserve_held=True)
                    disposition = "retry_safe"
            else:
                # The request may have reached the backend with no local receipt
                # recorded; retain the reserve and the unresolved bit.
                self._conn().execute(
                    "UPDATE job_attempt SET state = 'interrupted', "
                    " outcome_unresolved = 1, ended_at_utc = ? "
                    "WHERE project_id = ? AND attempt_id = ?",
                    (now, self.project_id, attempt_id),
                )
                self._conn().execute(
                    "UPDATE job SET outcome_unresolved = 1 "
                    "WHERE project_id = ? AND job_id = ?",
                    (self.project_id, job.job_id),
                )
                self._set_job_state(
                    job.job_id, "reconciliation_required", preserve_held=True
                )
                disposition = (
                    "accept_pending_local_result"
                    if has_local_result
                    else "reconciliation_required"
                )
        return RecoveryOutcome(
            job_id=attempt.job_id,
            attempt_id=attempt_id,
            disposition=disposition,
        )

    def reconcile(
        self,
        job_id: str,
        *,
        resolution: str,
        result_kind: str | None = None,
        result: object | None = None,
    ) -> Job:
        """Resolve an ambiguous job from durable state — never a new identity.

        ``resolution`` is one of :data:`RECONCILIATION_RESOLUTIONS`:

        * ``accepted_local_result`` — accept the current attempt's local result
          (requires ``result_kind``/``result``);
        * ``definitive_failure`` — record a definitive failure of the current
          attempt;
        * ``human_escalation`` — move the job to ``waiting_human``;
        * ``keep_unresolved`` — move the job to ``waiting_external``, unresolved
          bit retained.
        """
        job_id = validate_identifier(job_id, "job_id")
        _require_enum(resolution, RECONCILIATION_RESOLUTIONS, "resolution")
        job = self._require_job(job_id)
        if job.current_attempt_id is None:
            raise JobStateError(f"job {job_id!r} has no attempt to reconcile.")
        attempt_id = job.current_attempt_id

        if resolution == "accepted_local_result":
            if result_kind is None or result is None:
                raise JobValidationError(
                    "accepted_local_result reconciliation requires result_kind "
                    "and result."
                )
            self.accept_result(attempt_id, result_kind=result_kind, result=result)
        elif resolution == "definitive_failure":
            self._mark_definitive_failure(attempt_id, allow_from_unresolved=True)
        elif resolution == "human_escalation":
            self.escalate_to_human(job_id)
        else:  # keep_unresolved
            self.mark_waiting_external(job_id)
        return self._require_job(job_id)

    # -- budget -------------------------------------------------------------- #
    def set_budget_policy(
        self, *, project_cap_usd: str, stage_cap_usd: str
    ) -> None:
        """Persist the project/stage cost caps (exact decimals, strictly positive)."""
        project_cap = _exact_decimal(
            project_cap_usd, "project_cap_usd", strictly_positive=True
        )
        stage_cap = _exact_decimal(
            stage_cap_usd, "stage_cap_usd", strictly_positive=True
        )
        with self._store.transaction():
            self._conn().execute(
                "INSERT INTO job_budget_policy (project_id, project_cap_usd, "
                " stage_cap_usd, updated_at_utc) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(project_id) DO UPDATE SET project_cap_usd = "
                " excluded.project_cap_usd, stage_cap_usd = excluded.stage_cap_usd, "
                " updated_at_utc = excluded.updated_at_utc",
                (self.project_id, project_cap, stage_cap, _utc_now()),
            )

    def budget_policy(self) -> tuple[str, str]:
        """Return ``(project_cap_usd, stage_cap_usd)`` exact-decimal strings."""
        row = self._conn().execute(
            "SELECT project_cap_usd, stage_cap_usd FROM job_budget_policy "
            "WHERE project_id = ?",
            (self.project_id,),
        ).fetchone()
        if row is None:
            return DEFAULT_PROJECT_CAP_USD, DEFAULT_STAGE_CAP_USD
        return (
            _exact_decimal(row["project_cap_usd"], "stored project cap", strictly_positive=True),
            _exact_decimal(row["stage_cap_usd"], "stored stage cap", strictly_positive=True),
        )

    def budget_usage(self, *, stage: str | None = None) -> BudgetUsage:
        """Exact-decimal budget usage (reserves count until settled)."""
        project_cap, stage_cap = self.budget_policy()
        project_reserved = self._reserved_total()
        if stage is None:
            return BudgetUsage(
                project_cap_usd=project_cap,
                stage_cap_usd=stage_cap,
                project_reserved_usd=project_reserved,
                stage_reserved_usd=None,
                project_remaining_usd=format(
                    _add_exact(Decimal(project_cap), Decimal(project_reserved).copy_negate()), "f"
                ),
                stage_remaining_usd=None,
            )
        stage = validate_identifier(stage, "stage")
        stage_reserved = self._reserved_total(stage=stage)
        return BudgetUsage(
            project_cap_usd=project_cap,
            stage_cap_usd=stage_cap,
            project_reserved_usd=project_reserved,
            stage_reserved_usd=stage_reserved,
            project_remaining_usd=format(
                _add_exact(Decimal(project_cap), Decimal(project_reserved).copy_negate()), "f"
            ),
            stage_remaining_usd=format(
                _add_exact(Decimal(stage_cap), Decimal(stage_reserved).copy_negate()), "f"
            ),
        )

    def settle_cost(
        self,
        attempt_id: str,
        *,
        cost_state: str,
        amount_usd: str | None = None,
    ) -> Attempt:
        """Settle the current attempt's reserved cost (C02 §6.3).

        The reserve row is **never reduced**: the original upper bound stays
        counted for ``known``/``estimated``/``unknown`` (conservative usage), so a
        later estimate/known transition can never silently shrink a held reserve.
        A ``known``/``estimated`` amount that **exceeds** the reserve is counted at
        the larger actual, so an overrun is never under-counted against the cap
        (C02 §5.6). The reported/estimated amount is recorded on the attempt; an
        ``unknown`` cost records **no** amount (never zero), and ``not_incurred``
        records no amount either. Requires the current attempt.
        """
        attempt_id = validate_identifier(attempt_id, "attempt_id")
        _require_enum(cost_state, COST_STATES, "cost_state")
        if cost_state in _COST_STATES_WITH_AMOUNT:
            if amount_usd is None:
                raise JobValidationError(
                    f"cost_state {cost_state!r} requires an exact amount_usd."
                )
            recorded: str | None = _exact_decimal(amount_usd, "amount_usd")
        else:
            if amount_usd is not None:
                raise JobValidationError(
                    f"cost_state {cost_state!r} must not carry an amount_usd."
                )
            recorded = None

        with self._store.transaction():
            attempt = self._require_attempt(attempt_id)
            self._require_current_attempt(attempt)
            reserve = self._conn().execute(
                "SELECT * FROM job_budget_reserve WHERE project_id = ? "
                "AND attempt_id = ?",
                (self.project_id, attempt_id),
            ).fetchone()
            if reserve is None:
                raise JobStateError(
                    f"attempt {attempt_id!r} has no budget reserve to settle."
                )
            reserve_state = (
                "retained" if cost_state in ("unknown", "estimated") else "settled"
            )
            self._conn().execute(
                "UPDATE job_budget_reserve SET state = ? "
                "WHERE project_id = ? AND reserve_id = ?",
                (reserve_state, self.project_id, reserve["reserve_id"]),
            )
            self._conn().execute(
                "UPDATE job_attempt SET cost_state = ?, cost_amount_usd = ? "
                "WHERE project_id = ? AND attempt_id = ?",
                (cost_state, recorded, self.project_id, attempt_id),
            )
        return self._require_attempt(attempt_id)

    # -- internal ------------------------------------------------------------ #
    def _job_row(self, job_id: str) -> sqlite3.Row | None:
        return self._conn().execute(
            "SELECT * FROM job WHERE project_id = ? AND job_id = ?",
            (self.project_id, job_id),
        ).fetchone()

    def _job_row_by_request(self, request_id: str) -> sqlite3.Row | None:
        return self._conn().execute(
            "SELECT * FROM job WHERE project_id = ? AND request_id = ?",
            (self.project_id, request_id),
        ).fetchone()

    def _attempt_row(self, attempt_id: str) -> sqlite3.Row | None:
        return self._conn().execute(
            "SELECT * FROM job_attempt WHERE project_id = ? AND attempt_id = ?",
            (self.project_id, attempt_id),
        ).fetchone()

    def _require_job(self, job_id: str) -> Job:
        row = self._job_row(job_id)
        if row is None:
            raise JobStateError(f"job {job_id!r} does not exist in this project.")
        return self._row_to_job(row)

    def _require_attempt(self, attempt_id: str) -> Attempt:
        row = self._attempt_row(attempt_id)
        if row is None:
            raise JobStateError(
                f"attempt {attempt_id!r} does not exist in this project."
            )
        return self._row_to_attempt(row)

    def _require_current_attempt(self, attempt: Attempt) -> Job:
        """Return the attempt's job, refusing a stale (non-current) attempt.

        Every mutator routes through this so a stale attempt can never write to
        (or regress) a job whose fence has moved on.
        """
        job = self._require_job(attempt.job_id)
        if (
            job.current_attempt_id != attempt.attempt_id
            or job.current_fence != attempt.fence
        ):
            raise StaleFenceError(
                f"attempt {attempt.attempt_id!r} (fence {attempt.fence}) is not the "
                f"current attempt of job {attempt.job_id!r} "
                f"(fence {job.current_fence}); a stale attempt cannot mutate the job."
            )
        return job

    def _set_job_state(
        self, job_id: str, state: str, *, preserve_held: bool = False
    ) -> None:
        if preserve_held:
            row = self._conn().execute(
                "SELECT state FROM job WHERE project_id = ? AND job_id = ?",
                (self.project_id, job_id),
            ).fetchone()
            if row is not None and row["state"] in _SCHEDULING_HELD_STATES:
                return
        self._conn().execute(
            "UPDATE job SET state = ?, updated_at_utc = ? "
            "WHERE project_id = ? AND job_id = ?",
            (state, _utc_now(), self.project_id, job_id),
        )

    def _reserved_total(self, *, stage: str | None = None) -> str:
        """Return the counted, conservative cost exposure for the project/stage.

        Each reserve contributes ``max(original reserve, reported actual)``:
        the immutable upper bound is always counted, and a **known/estimated**
        amount that overran it is counted instead (so an overrun is never
        under-counted against the cap, C02 §5.6). ``unknown``/``not_incurred``
        attempts contribute no separate amount (their reserve bound is retained).
        Every stored value is re-validated as a bounded exact decimal, so a
        malformed stored value fails closed rather than mis-summing.
        """
        join = (
            "SELECT r.amount_usd AS reserve_amount, a.cost_amount_usd AS cost_amount "
            "FROM job_budget_reserve r "
            "LEFT JOIN job_attempt a ON a.project_id = r.project_id "
            "AND a.attempt_id = r.attempt_id "
            "WHERE r.project_id = ?"
        )
        params: list[object] = [self.project_id]
        if stage is not None:
            join += " AND r.stage = ?"
            params.append(stage)
        rows = self._conn().execute(join, params).fetchall()
        total = Decimal("0")
        for row in rows:
            counted = _bounded_decimal_amount(
                row["reserve_amount"], "stored budget reserve"
            )
            if row["cost_amount"] is not None:
                actual = _bounded_decimal_amount(
                    row["cost_amount"], "stored cost amount"
                )
                counted = max(counted, actual)
            total = _add_exact(total, counted)
        return format(total, "f")

    def _check_caps_and_reserve(
        self, *, job_id: str, attempt_id: str, stage: str, amount: str
    ) -> None:
        project_cap, stage_cap = self.budget_policy()
        project_reserved = Decimal(self._reserved_total())
        stage_reserved = Decimal(self._reserved_total(stage=stage))
        want = Decimal(amount)
        if _add_exact(project_reserved, want) > Decimal(project_cap):
            raise BudgetExceededError(
                f"reserving {amount} would exceed the project cap {project_cap} "
                f"(already reserved {format(project_reserved, 'f')}); "
                f"refusing to dispatch."
            )
        if _add_exact(stage_reserved, want) > Decimal(stage_cap):
            raise BudgetExceededError(
                f"reserving {amount} would exceed the stage cap {stage_cap} for "
                f"stage {stage!r} (already reserved {format(stage_reserved, 'f')}); "
                f"refusing to dispatch."
            )
        self._conn().execute(
            "INSERT INTO job_budget_reserve (project_id, reserve_id, job_id, "
            " attempt_id, stage, amount_usd, state, created_at_utc) "
            "VALUES (?, ?, ?, ?, ?, ?, 'held', ?)",
            (
                self.project_id,
                "rsv_" + uuid.uuid4().hex,
                job_id,
                attempt_id,
                stage,
                amount,
                _utc_now(),
            ),
        )

    # -- row mapping --------------------------------------------------------- #
    @staticmethod
    def _row_to_job(row: sqlite3.Row) -> Job:
        return Job(
            project_id=row["project_id"],
            job_id=row["job_id"],
            request_id=row["request_id"],
            fingerprint=row["fingerprint"],
            task_kind=row["task_kind"],
            parameters=_json_loads(row["parameters_json"]),
            state=row["state"],
            outcome_unresolved=bool(row["outcome_unresolved"]),
            resume_state=row["resume_state"],
            current_attempt_id=row["current_attempt_id"],
            current_fence=row["current_fence"],
            attempts_total=row["attempts_total"],
            retries_used=row["retries_used"],
            accepted_fence=row["accepted_fence"],
            created_at_utc=row["created_at_utc"],
            updated_at_utc=row["updated_at_utc"],
        )

    @staticmethod
    def _row_to_attempt(row: sqlite3.Row) -> Attempt:
        checkpoint_text = row["checkpoint_json"]
        return Attempt(
            project_id=row["project_id"],
            attempt_id=row["attempt_id"],
            job_id=row["job_id"],
            fence=row["fence"],
            attempt_no=row["attempt_no"],
            dispatch_kind=row["dispatch_kind"],
            state=row["state"],
            prior_outcome=row["prior_outcome"],
            retry_authorized_by=row["retry_authorized_by"],
            outcome_unresolved=bool(row["outcome_unresolved"]),
            admission_id=row["admission_id"],
            external_ref=row["external_ref"],
            checkpoint=None if checkpoint_text is None else _json_loads(checkpoint_text),
            reserve_usd=row["reserve_usd"],
            cost_state=row["cost_state"],
            cost_amount_usd=row["cost_amount_usd"],
            started_at_utc=row["started_at_utc"],
            ended_at_utc=row["ended_at_utc"],
        )

    @staticmethod
    def _row_to_accepted(row: sqlite3.Row) -> AcceptedResult:
        return AcceptedResult(
            project_id=row["project_id"],
            job_id=row["job_id"],
            attempt_id=row["attempt_id"],
            fence=row["fence"],
            result_kind=row["result_kind"],
            result=_json_loads(row["result_json"]),
            result_digest=row["result_digest"],
            accepted_at_utc=row["accepted_at_utc"],
        )


__all__ = [
    "ACTIVE_ATTEMPT_STATES",
    "ATTEMPT_STATES",
    "AcceptedResult",
    "AmbiguousOutcomeError",
    "Attempt",
    "BudgetExceededError",
    "BudgetUsage",
    "COST_STATES",
    "DEFAULT_PROJECT_CAP_USD",
    "DEFAULT_STAGE_CAP_USD",
    "DISPATCH_KINDS",
    "JOB_STATES",
    "Job",
    "JobConflictError",
    "JobError",
    "JobRepository",
    "JobStateError",
    "JobValidationError",
    "MAX_SAFE_RETRIES",
    "RECONCILIATION_RESOLUTIONS",
    "RecoveryOutcome",
    "RetryLimitExceeded",
    "STARTABLE_JOB_STATES",
    "StaleFenceError",
    "TERMINAL_JOB_STATES",
    "validate_reserve_usd",
]
