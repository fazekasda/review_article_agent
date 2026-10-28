"""Durable, immutable **full per-question direct Jev decision result observation**.

Authority
---------

* ``docs/implementation/04-agent-runtime.md`` deliverable 4 (Jev decision adapter
  with validated outputs and durable application records) and acceptance criteria
  5/6 ("Model/provider errors, refusals, invalid shapes, and partial Jev errors are
  not interpreted as scientific exclusions"; "Stateless decision responses are
  persisted with rubric/model/input versions. Account separately for direct calls
  not covered by session statistics").
* ``docs/contracts/02-gates-and-runtime.md`` §6.1 (``VersionSet``), §6.2
  (``DecisionRecord``/``QuestionDef``/``PerQuestionError``/``Diagnostic``), §6.3
  (``CostRecord``), and §8.1.1 (``DispatchAttempt``), read as the authoritative
  record shapes — no field is invented here.

What this module **is**
-----------------------

The storage half of "persist the exact, complete response to the exact question
that was recorded before dispatch". Given a persisted direct Jev dispatch and the
already-recorded :class:`~review_article_agent.storage.decision_intents.DecisionIntentRecord`
for it, and a **trusted** :class:`~review_article_agent.runtime.jev.DispatchResult`,
it records **one immutable observation row** (schema migration 9) holding:

* the complete, ordered array of per-question ``DecisionRecord`` bindings
  (``question_id``/``record_id``/descriptor digest/kind/outcome) plus
  ``question_count``, so a **partial set is detectable**; and
* one canonical C02 §6.2 ``DecisionRecord`` row per question, keyed by its
  content-derived ``record_id`` and queryable by ``(dispatch_id, question_id)``.

Every value is **re-derived** from the persisted intent and the trusted result,
never taken from a caller argument: the result's model/endpoint/request
digest/size/reserve/cost-bound are compared field-by-field with the intent, each
question is **rebuilt** from the recorded ``question_def_ref``/``criteria_ref``
(the actual stored bytes are re-hashed/re-sized and the pinned rights
re-validated via the intent repository's own bounded helper), and the per-question
records are produced by :func:`review_article_agent.runtime.decision_records.project_decision_records`
with those rebuilt questions, the intent's own ``VersionSet``/consumed slots, and
``task_context = intent.task_id``.

On every read the stored set and every stored per-question record are
**independently** revalidated: completeness is measured against the recorded
intent's exact question order/count/kinds (never the set's own array alone); each
record's refs, target, task context, descriptor digest, options/levels/kind,
versions/consumed slots, request pin, cost, and provider envelope are compared
with the intent and the set; the whole-call ``call_status``/diagnostic relation and
the adapter's documented ``status`` ↔ HTTP/cost/diagnostic relation are enforced;
and counters (attempt, calls, counts, ordinals) are **plain integers** — a bool or
float that merely compares equal to ``1`` is refused, never normalized.

What this module **is not** (the boundary)
------------------------------------------

* It performs **no** network, transport, or model call, and **no** execution. It
  records an observation of a result it is handed.
* It **grants no authority** and makes **no** completion claim: a recorded
  observation is **inert**, **not** an accepted local result, **not** a scientific
  eligibility/exclusion decision, and **not** a claim of remote quiescence
  (:data:`RESULT_GRANTS_AUTHORITY`/``RESULT_ACCEPTS_LOCAL_RESULT``/
  ``RESULT_CLAIMS_REMOTE_QUIESCENCE` are all ``False``). The inert
  :meth:`DecisionResultRepository.record_result` never calls
  ``accept_result``/``recover``/``settle_cost`` and never advances the dispatch
  state machine: a whole-call ``unknown`` result is persisted but the dispatch
  **stays** ``submitted``/``unknown`` (it is never turned into ``completed`` and no
  unknown cost is recorded as ``0``). The single atomic
  :meth:`DecisionResultRepository.commit_result` is the local bridge: in **one**
  store transaction it records the same observation, settles the exact cost
  monotonically, and applies the derived local transition (accept a completed
  result / fail a definitive pre-billing rejection / recover an ambiguous outcome
  to ``unknown``). It is still **not** a transport: it performs no network call and
  mints no new identity, and it claims nothing about remote quiescence.
* It serializes **no** credential, endpoint secret, or opaque authority, and it
  makes no network call; but it is **not** a secret scanner and makes **no**
  "never contains source" promise. Its private canonical bytes carry bounded
  caller/provider-derived text — the provider envelope, a score ``legend``, and the
  declared ``options``/``levels``, alongside opaque ids, exact-decimal amounts,
  redacted diagnostics, and SHA-256 digests (spec lines 62-63; ``docs/storage.md``).
  A caller that places a secret into submitted request state or referenced source
  content places it in the private archive; never do so.

Every failure is a static, value-free :class:`DecisionResultError`: a message never
echoes caller text, source bytes, a credential, a model identifier, a ref, or a
digest.
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
from ..domain.records import canonical_json, validate_identifier, validate_ref
from ..runtime import jev
from ..runtime.decision_records import (
    FAILED_SUBMITTED_STATUSES,
    OPTIONAL_CONSUMED_SLOTS,
    DecisionRecord,
    DecisionRecordError,
    QuestionBinding,
    _project_diagnostic,
    ledger_charges,
    project_decision_records,
    question_descriptor_digest,
    validate_decision_record_mapping,
)
from ..runtime.dispatch import (
    _ACCEPTABLE_STATES,
    DISPATCH_STATES,
    DispatchConflictError,
    DispatchCorruptionError,
    DispatchRecord,
    RuntimeDispatchRepository,
)
from .agent_runtime import (
    RuntimeCorruptionError,
    RuntimeRepository,
    RuntimeValidationError,
)
from .decision_intents import (
    DecisionIntentCorruptionError,
    DecisionIntentError,
    DecisionIntentRepository,
    _load_question,
)

__all__ = [
    "RESULT_SCHEMA_VERSION",
    "RESULT_ROUTE",
    "MAX_RESULT_SET_BYTES",
    "MAX_RESULT_RECORD_BYTES",
    "RESULT_GRANTS_AUTHORITY",
    "RESULT_ACCEPTS_LOCAL_RESULT",
    "RESULT_CLAIMS_REMOTE_QUIESCENCE",
    "RESULT_QUESTION_KINDS",
    "RESULT_OUTCOMES",
    "DecisionResultError",
    "DecisionResultValidationError",
    "DecisionResultConflictError",
    "DecisionResultStateError",
    "DecisionResultStaleFenceError",
    "DecisionResultCorruptionError",
    "RESULT_KIND",
    "COMMIT_OUTCOMES",
    "COMMIT_FAULT_SEAMS",
    "QuestionResultRef",
    "DecisionResultQuestion",
    "DecisionResultObservation",
    "DecisionCommitOutcome",
    "DecisionResultRepository",
    "decision_result_integrity_problems",
]

# --------------------------------------------------------------------------- #
# Vocabulary / bounds
# --------------------------------------------------------------------------- #
#: Version of the *recorded result observation representation*. Independent of the
#: store schema version, the embedded ``VersionSet``, and the C02 record version.
RESULT_SCHEMA_VERSION = 1

#: The only route a direct Jev result observation may describe (§8.1.1).
RESULT_ROUTE = "direct_call"

#: Upper bound on the canonical bytes of one result-set observation.
MAX_RESULT_SET_BYTES = 1_048_576

#: Upper bound on the canonical bytes of one per-question record.
MAX_RESULT_RECORD_BYTES = 262_144

#: An observation is inert: it grants no authority, is not an accepted local
#: result, and claims nothing about remote quiescence.
RESULT_GRANTS_AUTHORITY = False
RESULT_ACCEPTS_LOCAL_RESULT = False
RESULT_CLAIMS_REMOTE_QUIESCENCE = False

#: The declared question kinds a projected record may carry.
RESULT_QUESTION_KINDS = ("predicate", "choice", "score", "box")

#: The C02 §6.2 outcome literals.
RESULT_OUTCOMES = (
    "answered",
    "partial_error",
    "uncertain",
    "omitted",
    "refusal",
    "malformed",
    "unsupported",
)

#: State of the *first* direct attempt envelope (no prior attempt).
PRIOR_OUTCOME_NONE = "none"

#: The documented **definitive, pre-billing** HTTP rejections (the adapter's own
#: ``_DEFINITIVE_STATUSES`` in ``runtime.jev``, restated here as a status envelope
#: invariant): only these may be recorded ``not_incurred``. Every other non-200
#: status — including 429 and every 3xx/5xx — is ambiguous and stays ``unknown``.
DEFINITIVE_REJECTION_STATUSES = frozenset({400, 401, 402, 403, 404, 413})

#: The ``result_kind`` an accepted local direct-Jev result set is recorded under on
#: its dispatch (C02 §8.2 uses an opaque kind/ref/digest triple for the accepted
#: local result; this names the full per-question decision set).
RESULT_KIND = "jev_decision_set"

#: The derived local outcomes of one committed direct Jev result. They are
#: **derived from the validated result envelope + its HTTP status**, never taken
#: from a caller-supplied success/terminal/authenticated boolean:
#:
#: * ``completed`` — an HTTP 200 result (per-question failures included) accepted as
#:   the dispatch's single local result;
#: * ``failed`` — a documented definitive pre-billing rejection (400/401/402/403/
#:   404/413); never a scientific negative and never an accepted result;
#: * ``unknown`` — any transport/protocol failure or ambiguous API status: the
#:   remote outcome is unresolved, the reserve is retained, and no result is accepted.
COMMIT_OUTCOMES = ("completed", "failed", "unknown")

#: Fault-injection seam names of the atomic commit path (in-process tests only).
#: Each fires inside the single commit transaction, so a hook that raises must roll
#: back the whole 1+N observation, the cost settlement, and the dispatch state.
COMMIT_FAULT_SEAMS = ("after_set_insert", "after_cost", "before_accept")

#: Dispatch states in which a recorded observation remains valid history: a result
#: may follow a recorded submission/admission/unknown outcome, and a later atomic
#: bridge acceptance moves the dispatch to ``completed`` (or a remote failure to
#: ``failed``). A ``reserved`` dispatch can never have issued a request, so a set
#: row on one is corruption.
_PARENT_STATES = frozenset(_ACCEPTABLE_STATES | {"completed", "failed"})

_MAX_IDENTIFIER_CHARS = 512
_MAX_META_CHARS = 256
_MAX_DECIMAL_INPUT_LEN = 128
_MAX_SIGNIFICANT_DIGITS = 40
_MAX_ADJUSTED_EXPONENT = 24

_HEX_RE = re.compile(r"^[0-9a-f]{64}$")
_UTC_TIMESTAMP_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|\+00:00)$"
)

#: Exact keys of the canonical result-set mapping.
_SET_KEYS = frozenset(
    {
        "schema_version",
        "result_id",
        "route",
        "project_id",
        "dispatch_id",
        "task_id",
        "intent_id",
        "intent_digest",
        "input_digest",
        "version_digest",
        "request_sha256",
        "request_bytes",
        "cost_upper_bound_usd",
        "reserve_usd",
        "status",
        "attempt_no",
        "calls_attempted",
        "prior_outcome",
        "idempotent_admission_proven",
        "no_automatic_retry",
        "http_status",
        "provider",
        "response_id",
        "actual_model",
        "usage",
        "cost",
        "diagnostic",
        "questions",
        "question_ids",
        "question_count",
    }
)

_QUESTION_REF_KEYS = frozenset(
    {"question_id", "question_kind", "record_id", "record_digest", "outcome"}
)
_USAGE_KEYS = frozenset({"input_tokens", "output_tokens"})
_COST_KEYS = frozenset(
    {"state", "amount_usd", "currency", "route", "accounting_bucket", "reserve_usd", "dispatch_ref"}
)
_DIAGNOSTIC_KEYS = frozenset({"code", "detail_redacted", "raised_at"})

_COST_STATES_WITH_AMOUNT = frozenset({"known", "estimated"})


# --------------------------------------------------------------------------- #
# Errors (static, curated, non-reflective)
# --------------------------------------------------------------------------- #
class DecisionResultError(StoreError):
    """Base class for every recorded Jev result-observation failure.

    Every message is **static and curated**: it never echoes caller text, source
    bytes, a credential, a model identifier, a ref, or a digest.
    """


class DecisionResultValidationError(DecisionResultError, ValueError):
    """An input result/dispatch/intent is malformed, foreign, or inconsistent."""


class DecisionResultConflictError(DecisionResultError):
    """A divergent replay for the same dispatch's recorded result observation."""


class DecisionResultStateError(DecisionResultError):
    """The dispatch is not in a state that may carry a result observation."""


class DecisionResultStaleFenceError(DecisionResultStateError):
    """The supplied fence is not the dispatch's current fence (stale attempt)."""


class DecisionResultCorruptionError(DecisionResultError):
    """A stored result row fails structural/hash/ref/link revalidation on read."""


# --------------------------------------------------------------------------- #
# Static messages (never echo a value)
# --------------------------------------------------------------------------- #
_M_BAD_RESULT = "the supplied dispatch result is not a trusted Jev dispatch result"
_M_BAD_DISPATCH = "the supplied dispatch is not a persisted direct dispatch of this project"
_M_BAD_INTENT = "the recorded request intent for this dispatch is missing or malformed"
_M_BAD_ID = "a decision-result identifier is not a valid identifier"
_M_BAD_FENCE = "a decision-result fence is not a positive integer"
_M_ROUTE = "only a direct_call dispatch may carry a Jev result observation"
_M_STATE = "a result observation may be recorded only for a submitted, admitted, or unknown dispatch"
_M_COMPLETED = "a completed dispatch is not in a state that may carry a new result observation"
_M_STALE_FENCE = "the supplied fence is not the dispatch's current fence"
_M_DIVERGENT = "a different result observation is already recorded for this dispatch"
_M_ENDPOINT = "the dispatch result endpoint does not match the recorded request intent"
_M_MODEL = "the dispatch result model does not match the recorded request intent"
_M_STATUS = "the dispatch result status is not a documented dispatch status"
_M_NOT_SUBMITTED = "a result observation is recorded only from a submitted dispatch"
_M_ATTEMPT = "a direct result observation must be the single first attempt envelope"
_M_RETRY = "a direct result observation must carry no-automatic-retry and no proven admission"
_M_REQUEST = "the dispatch result request binding does not match the recorded request intent"
_M_RESERVE = "the dispatch result reserve does not match the recorded request intent"
_M_BOUND = "the dispatch result cost bound does not match the recorded request intent"
_M_COST = "the dispatch result cost record is malformed for a direct call"
_M_COST_RESERVE = "a submitted direct dispatch must carry a retained reserve"
_M_UNKNOWN_COST = "an unknown or not-incurred cost must never carry an amount"
_M_PARENT = "the recorded result does not match its parent dispatch/task/intent"
_M_QUESTION = "a rebuilt question does not match the recorded descriptor digest"
_M_QUESTION_COVERAGE = "the recorded questions do not cover exactly the intent's questions"
_M_COLUMNS = "a projected column disagrees with the recorded canonical bytes"
_M_CANONICAL = "the recorded result canonical bytes are malformed"
_M_CANONICAL_SIZE = "the recorded result canonical bytes exceed the permitted size"
_M_CANONICAL_ROUNDTRIP = "the recorded result bytes are not in canonical form"
_M_PARTIAL_SET = "the recorded result does not carry the complete set of question records"
_M_RECORD_DIGEST = "a recorded per-question record digest does not match its canonical bytes"
_M_BAD_ROW = "a stored decision-result row is malformed"
_M_PROJECT = "the recorded result project does not match the store project"
_M_TIMESTAMP = "a decision-result timestamp is not an RFC 3339 UTC instant"
_M_DIGEST = "a decision-result digest is not a lowercase hex SHA-256 digest"
_M_DECIMAL = "a decision-result amount is not an exact bounded decimal"
_M_CONSUMED = "the recorded consumed version slots do not match the intent"
_M_CALL_STATUS = "the recorded per-question call-status audit contradicts the set status"
_M_CONTRADICTION = (
    "the supplied dispatch result contradicts the recorded intent, status, or cost "
    "relation; nothing was written"
)
_M_BAD_SEAM = "a commit fault seam is not a documented commit seam"
_M_ACCEPTED_CONFLICT = (
    "the dispatch's recorded state or accepted result contradicts the recorded "
    "observation; refusing to report a completed commit"
)


# --------------------------------------------------------------------------- #
# Bounded helpers (never defaulting)
# --------------------------------------------------------------------------- #
def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _bare_hex(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = value[7:] if value.startswith("sha256:") else value
    return text if _HEX_RE.fullmatch(text) is not None else None


def _scoped_id(prefix: str, project_id: str, key: str) -> str:
    digest = _sha256_hex((project_id + "\x00" + key).encode("utf-8"))
    return f"{prefix}_{digest[:32]}"


def _result_id(project_id: str, dispatch_id: str) -> str:
    return _scoped_id("dres", project_id, dispatch_id)


def _identifier(value: object) -> str:
    try:
        normalized = validate_identifier(value)
    except Exception as exc:  # RecordValidationError / TypeError
        raise DecisionResultValidationError(_M_BAD_ID) from exc
    if len(normalized) > _MAX_IDENTIFIER_CHARS:
        raise DecisionResultValidationError(_M_BAD_ID)
    return normalized


def _is_hex_digest(value: object) -> bool:
    return isinstance(value, str) and _HEX_RE.fullmatch(value) is not None


def _bounded_str(value: object) -> bool:
    return isinstance(value, str) and 0 < len(value) <= _MAX_META_CHARS


def _plain_int(value: object, *, minimum: int = 0, maximum: int | None = None,
               message: str) -> int:
    """Return *value* only if it is a plain ``int`` (never ``bool`` or ``float``).

    ``True``/``False`` and ``1.0`` are rejected explicitly: a count, attempt number,
    ordinal, or HTTP status is an integer quantity, and a bool/float that compares
    equal to ``1`` must never be silently normalized into it.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise DecisionResultValidationError(message)
    if value < minimum or (maximum is not None and value > maximum):
        raise DecisionResultValidationError(message)
    return value


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


def _require_utc_timestamp(value: object) -> str | None:
    if not isinstance(value, str) or _UTC_TIMESTAMP_RE.fullmatch(value) is None:
        return None
    candidate = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        return None
    return value


def _is_jev_model(model: object) -> bool:
    """A real Jev response model: the pinned id or a dated snapshot ``-YYYYMMDD``."""
    if not isinstance(model, str) or model == "":
        return False
    if model == jev.MODEL:
        return True
    prefix = jev.MODEL + "-"
    if model.startswith(prefix) and len(model) == len(prefix) + 8:
        return model[len(prefix):].isdigit()
    return False


def _deep_freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _deep_freeze(item) for key, item in value.items()})
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
    and the parsed value is re-validated through :func:`canonical_json` (depth/
    cycle/portability) **before** any recursive walk.
    """
    if not isinstance(data, (bytes, bytearray)):
        raise DecisionResultCorruptionError(malformed)
    raw = bytes(data)
    if len(raw) > max_bytes:
        raise DecisionResultCorruptionError(too_large)
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise DecisionResultCorruptionError(malformed) from exc
    try:
        canonical_json(parsed)
    except Exception as exc:  # RecordValidationError / ValueError / TypeError
        raise DecisionResultCorruptionError(malformed) from exc
    return parsed


def _fail_closed_row(what: str):
    """Decorate a stored-row validator so a malformed row fails closed."""

    def decorate(func):
        @functools.wraps(func)
        def wrapper(self, *args, **kwargs):
            try:
                return func(self, *args, **kwargs)
            except (
                DecisionResultCorruptionError,
                DecisionResultConflictError,
            ):
                raise
            except (
                DecisionResultError,
                DecisionRecordError,
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
                raise DecisionResultCorruptionError(f"{what}: {_M_BAD_ROW}") from exc

        return wrapper

    return decorate


# --------------------------------------------------------------------------- #
# Value objects
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class QuestionResultRef:
    """One entry of the complete ordered per-question array on the set row."""

    question_id: str
    question_kind: str
    record_id: str
    record_digest: str
    outcome: str

    def to_mapping(self) -> dict[str, Any]:
        return {
            "question_id": self.question_id,
            "question_kind": self.question_kind,
            "record_id": self.record_id,
            "record_digest": self.record_digest,
            "outcome": self.outcome,
        }


@dataclass(frozen=True)
class DecisionResultObservation:
    """One immutable, revalidated full result-set observation (as stored)."""

    project_id: str
    result_id: str
    schema_version: int
    dispatch_id: str
    task_id: str
    intent_id: str
    intent_digest: str
    input_digest: str
    version_digest: str
    request_sha256: str
    request_bytes: int
    cost_upper_bound_usd: str
    reserve_usd: str
    status: str
    attempt_no: int
    calls_attempted: int
    prior_outcome: str
    idempotent_admission_proven: bool
    no_automatic_retry: bool
    http_status: int | None
    provider: str | None
    response_id: str | None
    actual_model: str | None
    usage: Mapping[str, Any] | None
    cost: Mapping[str, Any]
    diagnostic: Mapping[str, Any] | None
    questions: tuple[QuestionResultRef, ...]
    question_ids: tuple[str, ...]
    question_count: int
    content_digest: str
    created_at_utc: str
    canonical: bytes

    def __post_init__(self) -> None:
        # Strict integer quantities: no bool/float that merely compares equal to 1.
        _plain_int(self.schema_version, minimum=1, message=_M_CANONICAL)
        _plain_int(self.request_bytes, minimum=1, message=_M_REQUEST)
        if _plain_int(self.attempt_no, minimum=1, message=_M_ATTEMPT) != 1:
            raise DecisionResultValidationError(_M_ATTEMPT)
        if _plain_int(self.calls_attempted, minimum=1, message=_M_ATTEMPT) != 1:
            raise DecisionResultValidationError(_M_ATTEMPT)
        _plain_int(self.question_count, minimum=1, message=_M_QUESTION_COVERAGE)
        if self.http_status is not None:
            _plain_int(
                self.http_status, minimum=100, maximum=599, message=_M_CANONICAL
            )
        for name in ("usage", "cost", "diagnostic"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _deep_freeze(dict(value)))
        object.__setattr__(self, "questions", tuple(self.questions))
        object.__setattr__(self, "question_ids", tuple(self.question_ids))
        if len(self.questions) != self.question_count:
            raise DecisionResultValidationError(_M_QUESTION_COVERAGE)
        if len(self.question_ids) != self.question_count:
            raise DecisionResultValidationError(_M_QUESTION_COVERAGE)
        object.__setattr__(self, "canonical", bytes(self.canonical))

    @property
    def digest(self) -> str:
        """The recomputed SHA-256 of the exact canonical set bytes."""
        return self.content_digest

    def is_exclusion(self) -> bool:  # pragma: no cover - inert by design
        return False

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "result_id": self.result_id,
            "route": RESULT_ROUTE,
            "project_id": self.project_id,
            "dispatch_id": self.dispatch_id,
            "task_id": self.task_id,
            "intent_id": self.intent_id,
            "intent_digest": self.intent_digest,
            "input_digest": self.input_digest,
            "version_digest": self.version_digest,
            "request_sha256": self.request_sha256,
            "request_bytes": self.request_bytes,
            "cost_upper_bound_usd": self.cost_upper_bound_usd,
            "reserve_usd": self.reserve_usd,
            "status": self.status,
            "attempt_no": self.attempt_no,
            "calls_attempted": self.calls_attempted,
            "prior_outcome": self.prior_outcome,
            "idempotent_admission_proven": self.idempotent_admission_proven,
            "no_automatic_retry": self.no_automatic_retry,
            "http_status": self.http_status,
            "provider": self.provider,
            "response_id": self.response_id,
            "actual_model": self.actual_model,
            "usage": None if self.usage is None else _thaw(self.usage),
            "cost": _thaw(self.cost),
            "diagnostic": None if self.diagnostic is None else _thaw(self.diagnostic),
            "questions": [q.to_mapping() for q in self.questions],
            "question_ids": list(self.question_ids),
            "question_count": self.question_count,
        }


@dataclass(frozen=True)
class DecisionResultQuestion:
    """One immutable, revalidated canonical C02 §6.2 record (as stored)."""

    project_id: str
    dispatch_id: str
    result_id: str
    record_id: str
    question_id: str
    question_kind: str
    outcome: str
    ordinal: int
    request_sha256: str
    content_digest: str
    created_at_utc: str
    record: Mapping[str, Any]
    canonical: bytes

    def __post_init__(self) -> None:
        _plain_int(self.ordinal, minimum=0, message=_M_COLUMNS)
        object.__setattr__(self, "record", _deep_freeze(dict(self.record)))
        object.__setattr__(self, "canonical", bytes(self.canonical))

    @property
    def digest(self) -> str:
        return self.content_digest

    def to_mapping(self) -> dict[str, Any]:
        return _thaw(self.record)


@dataclass(frozen=True)
class DecisionCommitOutcome:
    """The result of one atomic observe + cost-settle + dispatch transition.

    ``outcome`` is the local outcome derived from the validated observation.
    ``observation_reused`` is ``True`` when this call found the identical 1+N
    observation already stored and therefore did **not** insert rows again; it may
    still have applied a pending dispatch/cost transition. ``replayed`` is ``True``
    only when the *entire* commit was already applied, i.e. nothing was written at
    all (an exact no-op). ``replayed`` therefore implies ``observation_reused``,
    never the reverse, and it can never describe a ``completed`` outcome whose
    dispatch is still ``submitted``.
    """

    observation: DecisionResultObservation
    dispatch: DispatchRecord
    outcome: str
    replayed: bool
    observation_reused: bool = False

    def __post_init__(self) -> None:
        if self.outcome not in COMMIT_OUTCOMES:
            raise DecisionResultValidationError(_M_STATUS)
        if self.replayed and not self.observation_reused:
            raise DecisionResultValidationError(_M_STATE)
        if self.outcome == "completed":
            # A returned completed commit must actually be completed: an inert
            # observation that never reached acceptance must not look like success.
            if (
                self.dispatch.state != "completed"
                or self.dispatch.result_kind != RESULT_KIND
                or self.dispatch.result_ref != self.observation.result_id
                or self.dispatch.result_digest != self.observation.digest
            ):
                raise DecisionResultValidationError(_M_COMPLETED)
        if self.replayed and self.dispatch.state == "submitted":
            raise DecisionResultValidationError(_M_STATE)


def _derive_commit_outcome(observation: DecisionResultObservation) -> str:
    """Derive the local outcome from the validated observation envelope alone.

    Only the recorded (already fully validated) ``status``/``http_status`` decide
    the outcome — never a caller boolean. A ``completed`` HTTP 200 result completes
    the dispatch; only a documented definitive pre-billing rejection fails it; every
    other submitted failure or ambiguous API status is unresolved (``unknown``).
    """
    status = observation.status
    if status == "completed":
        return "completed"
    if status == "api_error":
        if observation.http_status in DEFINITIVE_REJECTION_STATUSES:
            return "failed"
        return "unknown"
    if status in ("protocol_error", "transport_error"):
        return "unknown"
    raise DecisionResultValidationError(_M_STATUS)  # pragma: no cover - guarded


# --------------------------------------------------------------------------- #
# Question reconstruction (reuses the intent repository's bounded ref reader)
# --------------------------------------------------------------------------- #
def _rebuild_questions(intent, reader) -> tuple[jev.QuestionDef, ...]:
    """Rebuild the typed questions from the intent's verified per-question refs.

    The actual referenced bytes are re-hashed/re-sized and rights re-validated by
    ``reader`` (the intent repository's own bounded helper — no duplicated trust
    shortcut). Each rebuilt question's descriptor digest must equal the one the
    intent recorded.
    """
    rebuilt: list[jev.QuestionDef] = []
    for question in intent.questions:
        candidate = _load_question(
            question.question_id,
            question.question_def_ref,
            question.criteria_ref,
            reader=reader,
        )
        if candidate.question_kind != question.question_kind:
            raise DecisionResultCorruptionError(_M_QUESTION)
        if question_descriptor_digest(candidate) != question.question_def_digest:
            raise DecisionResultCorruptionError(_M_QUESTION)
        rebuilt.append(candidate)
    if tuple(q.question_id for q in rebuilt) != intent.question_ids():
        raise DecisionResultCorruptionError(_M_QUESTION_COVERAGE)
    return tuple(rebuilt)


def _bindings_for(questions: Sequence[jev.QuestionDef]) -> dict[str, QuestionBinding]:
    return {
        q.question_id: QuestionBinding(
            question_id=q.question_id,
            question_kind=q.question_kind,
            question_def_digest=question_descriptor_digest(q),
            options=tuple(q.options),
            levels=tuple(q.levels),
        )
        for q in questions
    }


def _optional_consumed(consumed_versions: Sequence[str]) -> tuple[str, ...]:
    return tuple(name for name in consumed_versions if name in OPTIONAL_CONSUMED_SLOTS)


# --------------------------------------------------------------------------- #
# Canonical set mapping: structural validation (no I/O)
# --------------------------------------------------------------------------- #
def _validate_question_ref(entry: object) -> dict[str, Any]:
    if not isinstance(entry, Mapping) or set(entry) != _QUESTION_REF_KEYS:
        raise DecisionResultValidationError(_M_CANONICAL)
    qid = _identifier(entry["question_id"])
    kind = entry["question_kind"]
    if kind not in RESULT_QUESTION_KINDS:
        raise DecisionResultValidationError(_M_CANONICAL)
    record_id = entry["record_id"]
    if not isinstance(record_id, str) or not record_id.startswith("dec_"):
        raise DecisionResultValidationError(_M_CANONICAL)
    if not _is_hex_digest(entry["record_digest"]):
        raise DecisionResultValidationError(_M_DIGEST)
    outcome = entry["outcome"]
    if outcome not in RESULT_OUTCOMES:
        raise DecisionResultValidationError(_M_CANONICAL)
    return {
        "question_id": qid,
        "question_kind": kind,
        "record_id": record_id,
        "record_digest": entry["record_digest"],
        "outcome": outcome,
    }


def _validate_usage(usage: object) -> dict[str, Any] | None:
    if usage is None:
        return None
    if not isinstance(usage, Mapping) or set(usage) != _USAGE_KEYS:
        raise DecisionResultValidationError(_M_CANONICAL)
    out: dict[str, Any] = {}
    for name in ("input_tokens", "output_tokens"):
        value = usage[name]
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, int) or value < 0
        ):
            raise DecisionResultValidationError(_M_CANONICAL)
        out[name] = value
    return out


def _validate_cost(cost: object, *, project_id: str, dispatch_id: str) -> dict[str, Any]:
    if not isinstance(cost, Mapping):
        raise DecisionResultValidationError(_M_COST)
    extra = set(cost) - _COST_KEYS
    if extra:
        raise DecisionResultValidationError(_M_COST)
    state = cost.get("state")
    if state not in jev.COST_STATES:
        raise DecisionResultValidationError(_M_COST)
    if cost.get("currency") != "USD":
        raise DecisionResultValidationError(_M_COST)
    if cost.get("route") != RESULT_ROUTE or cost.get("accounting_bucket") != RESULT_ROUTE:
        raise DecisionResultValidationError(_M_COST)
    if cost.get("dispatch_ref") != dispatch_id:
        raise DecisionResultValidationError(_M_COST)
    reserve = _parse_exact_decimal(cost.get("reserve_usd"), positive=True)
    if reserve is None:
        raise DecisionResultValidationError(_M_COST_RESERVE)
    amount = cost.get("amount_usd")
    out = {
        "state": state,
        "currency": "USD",
        "route": RESULT_ROUTE,
        "accounting_bucket": RESULT_ROUTE,
        "reserve_usd": reserve,
        "dispatch_ref": dispatch_id,
    }
    if state in _COST_STATES_WITH_AMOUNT:
        # ``amount_usd`` is present iff the cost is known/estimated.
        if amount is None or _parse_exact_decimal(amount) is None:
            raise DecisionResultValidationError(_M_COST)
        out["amount_usd"] = amount
    elif amount is not None:
        # A not-incurred/unknown cost must never carry an amount (never a silent 0).
        raise DecisionResultValidationError(_M_UNKNOWN_COST)
    return out


def _validate_diagnostic(diagnostic: object) -> dict[str, Any] | None:
    if diagnostic is None:
        return None
    if not isinstance(diagnostic, Mapping) or set(diagnostic) != _DIAGNOSTIC_KEYS:
        raise DecisionResultValidationError(_M_CANONICAL)
    if not _bounded_str(diagnostic.get("code")):
        raise DecisionResultValidationError(_M_CANONICAL)
    if not _bounded_str(diagnostic.get("detail_redacted")):
        raise DecisionResultValidationError(_M_CANONICAL)
    if _require_utc_timestamp(diagnostic.get("raised_at")) is None:
        raise DecisionResultValidationError(_M_TIMESTAMP)
    return {
        "code": diagnostic["code"],
        "detail_redacted": diagnostic["detail_redacted"],
        "raised_at": diagnostic["raised_at"],
    }


def _validate_status_envelope(mapping: Mapping[str, Any]) -> None:
    """Enforce the adapter's documented ``status`` <-> HTTP/cost/diagnostic relation.

    The typed ``jev.DispatchResult`` is produced by the adapter's own classifier, so
    the persisted envelope must not silently contradict it:

    * ``blocked`` was refused *before* the send boundary — it can never be a
      submitted observation, even if a forged envelope claims ``submitted``;
    * ``completed`` is an HTTP 200 with no diagnostic and real provider metadata;
    * ``api_error`` is a non-200 HTTP status with an ``api_error`` diagnostic and no
      provider metadata, and its cost is ``not_incurred`` **iff** the status is one
      of the documented definitive pre-billing rejections;
    * ``protocol_error``/``transport_error`` carry a matching diagnostic and no
      provider metadata; a transport failure has no HTTP status and an ``unknown``
      cost.
    """
    status = mapping["status"]
    http_status = mapping["http_status"]
    cost_state = mapping["cost"]["state"]
    diagnostic = mapping["diagnostic"]
    actual_model = mapping["actual_model"]
    provider = mapping["provider"]
    response_id = mapping["response_id"]
    usage = mapping["usage"]
    if status == "blocked":
        raise DecisionResultValidationError(_M_NOT_SUBMITTED)
    if status == "completed":
        if http_status != 200:
            raise DecisionResultValidationError(_M_STATUS)
        if cost_state == "not_incurred":
            raise DecisionResultValidationError(_M_STATUS)
        if diagnostic is not None:
            raise DecisionResultValidationError(_M_STATUS)
        if actual_model is None or usage is None:
            raise DecisionResultValidationError(_M_STATUS)
        return
    if diagnostic is None:
        raise DecisionResultValidationError(_M_STATUS)
    if actual_model is not None or provider is not None or response_id is not None:
        raise DecisionResultValidationError(_M_STATUS)
    if usage is not None:
        raise DecisionResultValidationError(_M_STATUS)
    if status == "api_error":
        if http_status is None or http_status == 200:
            raise DecisionResultValidationError(_M_STATUS)
        if diagnostic["code"] != "api_error":
            raise DecisionResultValidationError(_M_STATUS)
        definitive = http_status in DEFINITIVE_REJECTION_STATUSES
        if definitive != (cost_state == "not_incurred"):
            raise DecisionResultValidationError(_M_STATUS)
        return
    if status == "protocol_error":
        if diagnostic["code"] != "protocol_error":
            raise DecisionResultValidationError(_M_STATUS)
        if cost_state == "not_incurred":
            raise DecisionResultValidationError(_M_STATUS)
        return
    if status == "transport_error":
        if http_status is not None:
            raise DecisionResultValidationError(_M_STATUS)
        if cost_state != "unknown":
            raise DecisionResultValidationError(_M_STATUS)
        if diagnostic["code"] != "transport_error":
            raise DecisionResultValidationError(_M_STATUS)
        return
    raise DecisionResultValidationError(_M_STATUS)


def validate_result_set_mapping(mapping: object) -> dict[str, Any]:
    """Structurally revalidate one canonical result-set mapping (no I/O)."""
    if not isinstance(mapping, Mapping):
        raise DecisionResultValidationError(_M_CANONICAL)
    try:
        canonical_json(mapping)
    except Exception as exc:  # RecordValidationError / ValueError / TypeError
        raise DecisionResultValidationError(_M_CANONICAL) from exc
    for key in mapping:
        if not isinstance(key, str):
            raise DecisionResultValidationError(_M_CANONICAL)
    if set(mapping) != _SET_KEYS:
        raise DecisionResultValidationError(_M_CANONICAL)
    schema_version = mapping["schema_version"]
    if (
        isinstance(schema_version, bool)
        or not isinstance(schema_version, int)
        or schema_version != RESULT_SCHEMA_VERSION
    ):
        raise DecisionResultValidationError(_M_CANONICAL)
    if mapping["route"] != RESULT_ROUTE:
        raise DecisionResultValidationError(_M_ROUTE)
    project_id = _identifier(mapping["project_id"])
    dispatch_id = _identifier(mapping["dispatch_id"])
    if mapping["result_id"] != _result_id(project_id, dispatch_id):
        raise DecisionResultValidationError(_M_BAD_ID)
    _identifier(mapping["task_id"])
    _identifier(mapping["intent_id"])
    for field in (
        "intent_digest",
        "input_digest",
        "version_digest",
        "request_sha256",
    ):
        if not _is_hex_digest(mapping[field]):
            raise DecisionResultValidationError(_M_DIGEST)
    request_bytes = _plain_int(
        mapping["request_bytes"],
        minimum=1,
        maximum=jev.SAFE_MAX_REQUEST_BYTES,
        message=_M_REQUEST,
    )
    if _parse_exact_decimal(mapping["cost_upper_bound_usd"]) is None:
        raise DecisionResultValidationError(_M_DECIMAL)
    if _parse_exact_decimal(mapping["reserve_usd"], positive=True) is None:
        raise DecisionResultValidationError(_M_DECIMAL)
    status = mapping["status"]
    if status not in jev.DISPATCH_STATUSES:
        raise DecisionResultValidationError(_M_STATUS)
    attempt_no = _plain_int(mapping["attempt_no"], minimum=1, message=_M_ATTEMPT)
    calls_attempted = _plain_int(
        mapping["calls_attempted"], minimum=1, message=_M_ATTEMPT
    )
    if attempt_no != 1 or calls_attempted != 1:
        raise DecisionResultValidationError(_M_ATTEMPT)
    if mapping["prior_outcome"] != PRIOR_OUTCOME_NONE:
        raise DecisionResultValidationError(_M_ATTEMPT)
    if mapping["idempotent_admission_proven"] is not False:
        raise DecisionResultValidationError(_M_RETRY)
    if mapping["no_automatic_retry"] is not True:
        raise DecisionResultValidationError(_M_RETRY)
    http_status = mapping["http_status"]
    if http_status is not None:
        http_status = _plain_int(
            http_status, minimum=100, maximum=599, message=_M_CANONICAL
        )
    for name in ("provider", "response_id"):
        value = mapping[name]
        if value is not None and not _bounded_str(value):
            raise DecisionResultValidationError(_M_CANONICAL)
    actual_model = mapping["actual_model"]
    if actual_model is not None and not _is_jev_model(actual_model):
        raise DecisionResultValidationError(_M_MODEL)

    questions = mapping["questions"]
    if not isinstance(questions, (list, tuple)) or not questions:
        raise DecisionResultValidationError(_M_CANONICAL)
    normalized_questions = [_validate_question_ref(entry) for entry in questions]
    ids = [entry["question_id"] for entry in normalized_questions]
    if len(set(ids)) != len(ids):
        raise DecisionResultValidationError(_M_CANONICAL)
    raw_ids = mapping["question_ids"]
    if (
        not isinstance(raw_ids, (list, tuple))
        or any(not isinstance(value, str) for value in raw_ids)
        or list(raw_ids) != ids
    ):
        raise DecisionResultValidationError(_M_QUESTION_COVERAGE)
    question_count = _plain_int(
        mapping["question_count"], minimum=1, message=_M_QUESTION_COVERAGE
    )
    if question_count != len(normalized_questions):
        raise DecisionResultValidationError(_M_QUESTION_COVERAGE)

    normalized = {
        "schema_version": RESULT_SCHEMA_VERSION,
        "result_id": mapping["result_id"],
        "route": RESULT_ROUTE,
        "project_id": project_id,
        "dispatch_id": dispatch_id,
        "task_id": mapping["task_id"],
        "intent_id": mapping["intent_id"],
        "intent_digest": mapping["intent_digest"],
        "input_digest": mapping["input_digest"],
        "version_digest": mapping["version_digest"],
        "request_sha256": mapping["request_sha256"],
        "request_bytes": request_bytes,
        "cost_upper_bound_usd": mapping["cost_upper_bound_usd"],
        "reserve_usd": mapping["reserve_usd"],
        "status": status,
        "attempt_no": attempt_no,
        "calls_attempted": calls_attempted,
        "prior_outcome": PRIOR_OUTCOME_NONE,
        "idempotent_admission_proven": False,
        "no_automatic_retry": True,
        "http_status": http_status,
        "provider": mapping["provider"],
        "response_id": mapping["response_id"],
        "actual_model": actual_model,
        "usage": _validate_usage(mapping["usage"]),
        "cost": _validate_cost(
            mapping["cost"], project_id=project_id, dispatch_id=dispatch_id
        ),
        "diagnostic": _validate_diagnostic(mapping["diagnostic"]),
        "questions": normalized_questions,
        "question_ids": ids,
        "question_count": question_count,
    }
    _validate_status_envelope(normalized)
    canonical_json(normalized)
    return normalized


# --------------------------------------------------------------------------- #
# Repository
# --------------------------------------------------------------------------- #
class DecisionResultRepository:
    """Durable, atomic direct Jev result-observation recording over a store.

    Typical use::

        store = ProjectStore.open_at(app_dir, expected_project_id=pid)
        results = DecisionResultRepository(store)

        # Inert observation only (no dispatch transition):
        observation = results.record_result(
            dispatch_id, result, fence=dispatch.current_fence
        )

        # Or the one atomic observe + settle + transition commit:
        committed = results.commit_result(
            dispatch_id, result, fence=dispatch.current_fence
        )
    """

    def __init__(
        self,
        store,
        *,
        runtime: RuntimeRepository | None = None,
        dispatch: RuntimeDispatchRepository | None = None,
        intents: DecisionIntentRepository | None = None,
    ) -> None:
        self._store = store
        self._runtime = runtime if runtime is not None else RuntimeRepository(store)
        self._dispatch = (
            dispatch if dispatch is not None else RuntimeDispatchRepository(store)
        )
        self._intents = (
            intents
            if intents is not None
            else DecisionIntentRepository(store, runtime=self._runtime, dispatch=self._dispatch)
        )
        # In-process fault-injection seams for the atomic commit path (empty unless
        # a test registers a hook). They are never consulted by the inert record
        # path and never change a production outcome.
        self._commit_hooks: dict[str, list[Any]] = {
            name: [] for name in COMMIT_FAULT_SEAMS
        }

    @property
    def project_id(self) -> str:
        """The store's own project id; the repository never uses a caller project."""
        return self._store.project_id

    def _conn(self) -> sqlite3.Connection:
        return self._store.connection

    def _require_own_transaction(self) -> None:
        """Refuse a transaction-owned primitive invoked with no open transaction."""
        if not self._conn().in_transaction:
            raise DecisionResultStateError(
                "this result commit must run inside a caller-owned store transaction."
            )

    def add_commit_fault_hook(self, seam: str, hook) -> None:
        """Register an in-process fault hook at a :data:`COMMIT_FAULT_SEAMS` seam."""
        if seam not in self._commit_hooks:
            raise DecisionResultValidationError(_M_BAD_SEAM)
        self._commit_hooks[seam].append(hook)

    def _fire_commit_seam(self, seam: str, **info: Any) -> None:
        for hook in self._commit_hooks[seam]:
            hook(**info)

    # -- record (atomic: reload parents + project + insert set + rows) -------- #
    def record_result(
        self,
        dispatch_id: str,
        result: jev.DispatchResult,
        *,
        fence: int,
    ) -> DecisionResultObservation:
        """Record the complete per-question observation of one direct dispatch.

        The dispatch and its recorded pre-dispatch intent are **independently
        reloaded** from the store (a caller-supplied intent/ref/version is never
        trusted); the trusted ``result`` is compared field-by-field with the
        intent (model/endpoint/request digest/size/reserve/cost bound and the
        single first-attempt envelope); each question is rebuilt from the intent's
        recorded refs; the canonical C02 records are projected with the intent's
        own versions/consumed slots; and the full set **and** every per-question
        row are written in a single transaction, so no partial set is ever visible.
        The complete set/question/intent/status/cost cross-validation runs **inside
        that same transaction** (the just-inserted rows are revalidated with the
        shared read validators before the commit), so a result that contradicts its
        intent/status relation is refused with a static typed error and the whole
        1+N set rolls back rather than committing a corrupt observation.

        This remains the **inert observation** entry point: it never advances the
        dispatch state machine and never records an unknown cost as ``0``. An
        identical replay (same fence, same canonical bytes) returns the stored
        observation; a divergent replay raises :class:`DecisionResultConflictError`;
        a stale fence raises :class:`DecisionResultStaleFenceError`. A refused call
        writes nothing. The atomic observe+settle+transition path is
        :meth:`commit_result`.
        """
        if not isinstance(result, jev.DispatchResult):
            raise DecisionResultValidationError(_M_BAD_RESULT)
        dispatch_id = validate_identifier(dispatch_id, "dispatch_id")
        if isinstance(fence, bool) or not isinstance(fence, int) or fence < 1:
            raise DecisionResultValidationError(_M_BAD_FENCE)
        with self._store.transaction():
            _inserted, observation = self._record_result_in_transaction(
                dispatch_id, result, fence=fence
            )
            return observation

    def _record_result_in_transaction(
        self, dispatch_id: str, result: jev.DispatchResult, *, fence: int
    ) -> tuple[bool, DecisionResultObservation]:
        """The transaction-owned observe primitive (caller owns the unit of work).

        Returns ``(inserted, observation)``: ``inserted`` is ``True`` when this call
        wrote the 1+N set, ``False`` when it replayed an identical stored
        observation. It opens no transaction (``ProjectStore.transaction`` cannot
        nest) and therefore requires one already open on this connection.
        """
        self._require_own_transaction()
        project_id = _identifier(self.project_id)
        dispatch = self._require_dispatch(dispatch_id)
        if dispatch.route != RESULT_ROUTE:
            raise DecisionResultValidationError(_M_ROUTE)
        if fence != dispatch.current_fence:
            raise DecisionResultStaleFenceError(_M_STALE_FENCE)
        intent = self._require_intent(dispatch_id)
        if intent.project_id != project_id or intent.dispatch_id != dispatch_id:
            raise DecisionResultValidationError(_M_BAD_INTENT)
        if (
            intent.task_id != dispatch.task_id
            or intent.task_fingerprint != dispatch.task_fingerprint
            or intent.route != dispatch.route
            or intent.stage != dispatch.stage
            or intent.input_digest != dispatch.input_digest
            or intent.version_digest != dispatch.version_digest
            or intent.reserve_usd != dispatch.reserve_usd
            or intent.bound_evidence_digest != dispatch.bound_evidence_digest
        ):
            raise DecisionResultValidationError(_M_BAD_INTENT)
        self._require_persisted_task(dispatch.task_id)

        self._assert_result_binds_intent(result, intent, dispatch)
        built = self._build_observation(project_id, dispatch, intent, result)

        existing = self._set_row(dispatch_id)
        if existing is not None:
            stored = self._validate_set_row(existing)
            if stored.canonical != built["canonical"]:
                raise DecisionResultConflictError(_M_DIVERGENT)
            return False, stored

        if dispatch.state == "completed":
            raise DecisionResultStateError(_M_COMPLETED)
        if dispatch.state not in _ACCEPTABLE_STATES:
            raise DecisionResultStateError(_M_STATE)
        self._insert_observation(built)
        # The final full cross-validation must finish INSIDE this transaction:
        # revalidate the just-inserted set + question rows with the same shared
        # read validators so a contradictory envelope/intent/status/cost relation
        # rolls back the entire 1+N set instead of committing a corrupt record.
        row = self._set_row(dispatch_id)
        if row is None:  # pragma: no cover - just inserted
            raise DecisionResultCorruptionError(_M_PARTIAL_SET)
        try:
            return True, self._validate_set_row(row)
        except DecisionResultCorruptionError as exc:
            raise DecisionResultValidationError(_M_CONTRADICTION) from exc

    # -- atomic commit (observe + settle + dispatch transition, ONE transaction) - #
    def commit_result(
        self,
        dispatch_id: str,
        result: jev.DispatchResult,
        *,
        fence: int,
    ) -> DecisionCommitOutcome:
        """Record one direct Jev result **and** apply its local transition atomically.

        In **one** store transaction this composes three already-validated steps:

        1. the complete, independently revalidated 1+N observation (exactly as the
           inert :meth:`record_result`, sharing the same transaction-owned
           primitive);
        2. the exact cost settlement under the existing monotonic policy — the
           reserve is retained for an unresolved cost and an overrun is counted at
           the larger actual; the **only** ``not_incurred`` comes from a validated
           definitive pre-billing rejection;
        3. the dispatch transition the validated result envelope **derives** — never
           a caller terminal/success/authenticated boolean:

           * ``completed`` (HTTP 200, per-question failures included) →
             ``accept_result`` with ``result_kind=RESULT_KIND``, ``result_ref`` the
             observation's ``result_id`` and ``result_digest`` its content digest, so
             the dispatch reaches ``completed`` exactly once even when the cost is
             ``unknown`` (the reserve is still retained);
           * a documented definitive pre-billing rejection (definitive
             ``api_error``) → the cost is ``not_incurred`` and the dispatch is
             ``failed``: never a scientific negative, never an accepted result;
           * any transport/protocol failure or ambiguous API status → the dispatch
             is reconciled to ``unknown`` (retaining the reserve; a known reported
             cost does **not** resolve the operation) with no accepted result, no
             new identity and no retry.

        An observation already stored for this dispatch is **reused, not
        re-inserted**, and its required transition is still enforced against the
        current dispatch. That distinction matters: an inert
        :meth:`record_result` observation only proves *observation*, never that the
        dispatch was accepted/completed or its cost settled. So when the dispatch is
        still pending (``submitted``/``unknown``) this call applies the **same**
        atomic state + cost commit without touching the stored rows. A genuinely
        already-applied commit (state, accepted-result triple and cost all match the
        observation) is an exact no-op — bytes, timestamps, fence and cost are left
        untouched. A conflicting accepted result raises
        :class:`DecisionResultConflictError` instead of reporting false success, and
        a **divergent** observation replay raises the same error; a **stale fence**
        raises :class:`DecisionResultStaleFenceError`. Every refusal writes nothing.

        A cost already settled *more strongly* elsewhere is never downgraded or
        discarded: the observed cost is applied only as a monotonic upgrade, and the
        terminal/``unknown`` state is written first so the row is always
        read-consistent (an in-flight state can never carry ``not_incurred``). The
        single ``not_incurred`` remains the validated definitive pre-billing
        rejection.

        Every failure (a contradiction in the result envelope, a cost/incurred
        contradiction, a stale/divergent replay, or an injected fault-seam hook)
        rolls back the set, its question rows, the cost settlement, and the
        dispatch state together, so no partial observation or half-transition is
        ever visible. This performs **no** network call and touches **no** registry.
        """
        if not isinstance(result, jev.DispatchResult):
            raise DecisionResultValidationError(_M_BAD_RESULT)
        dispatch_id = validate_identifier(dispatch_id, "dispatch_id")
        if isinstance(fence, bool) or not isinstance(fence, int) or fence < 1:
            raise DecisionResultValidationError(_M_BAD_FENCE)

        with self._store.transaction():
            inserted, observation = self._record_result_in_transaction(
                dispatch_id, result, fence=fence
            )
            outcome = _derive_commit_outcome(observation)
            applied = inserted
            if inserted:
                self._fire_commit_seam(
                    "after_set_insert", dispatch_id=dispatch_id, observation=observation
                )
            # A reused observation still requires its transition to be applied: an
            # inert observation alone never proves completion or cost settlement.
            self._fire_commit_seam(
                "before_accept", dispatch_id=dispatch_id, outcome=outcome
            )
            applied = (
                self._apply_required_transition(
                    dispatch_id, observation, outcome, fence=fence
                )
                or applied
            )
            applied = (
                self._settle_observed_cost(dispatch_id, observation, outcome) or applied
            )
            self._fire_commit_seam(
                "after_cost", dispatch_id=dispatch_id, outcome=outcome
            )
            dispatch = self._require_dispatch(dispatch_id)
            return DecisionCommitOutcome(
                observation=observation,
                dispatch=dispatch,
                outcome=outcome,
                replayed=not applied,
                observation_reused=not inserted,
            )

    def _apply_required_transition(
        self,
        dispatch_id: str,
        observation: DecisionResultObservation,
        outcome: str,
        *,
        fence: int,
    ) -> bool:
        """Apply (or verify) the dispatch transition the observation requires.

        Returns ``True`` iff a state write was performed. A dispatch that already
        reached the required terminal/``unknown`` state is validated against the
        observation and left untouched; a pending ``submitted``/``unknown`` dispatch
        with an inert observation is transitioned here (same atomic commit, no row
        re-insertion). Any state/accepted-result contradiction raises
        :class:`DecisionResultConflictError` — never a silent success.
        """
        record = self._require_dispatch(dispatch_id)
        expected_triple = (
            RESULT_KIND,
            observation.result_id,
            observation.digest,
        )
        accepted_triple = (
            record.result_kind,
            record.result_ref,
            record.result_digest,
        )
        state = record.state
        if state == "completed":
            # The dispatch already carries an accepted local result: it must be
            # *this* observation's triple. Anything else is a conflicting accepted
            # result and must never be reported as a completed commit.
            if outcome != "completed" or accepted_triple != expected_triple:
                raise DecisionResultConflictError(_M_ACCEPTED_CONFLICT)
            return False
        if state == "failed":
            # A definitive failure has no accepted result by construction; an
            # ambiguous or completed observation on a failed dispatch is a
            # contradiction (only a validated definitive rejection may fail it).
            if outcome != "failed":
                raise DecisionResultConflictError(_M_ACCEPTED_CONFLICT)
            return False
        if outcome == "completed":
            self._dispatch._accept_result_in_transaction(
                dispatch_id,
                fence=fence,
                result_kind=RESULT_KIND,
                result_ref=observation.result_id,
                result_digest=observation.digest,
            )
            return True
        if outcome == "failed":
            self._dispatch._mark_failed_in_transaction(
                dispatch_id, definitive_rejection=True
            )
            return True
        # An ambiguous outcome on a submitted dispatch reconciles to unknown; one
        # that is already unknown is already recorded (no write, no new identity).
        if state == "submitted":
            self._dispatch._recover_in_transaction(dispatch_id)
            return True
        return False

    def _settle_observed_cost(
        self,
        dispatch_id: str,
        observation: DecisionResultObservation,
        outcome: str,
    ) -> bool:
        """Apply the observed cost monotonically; never downgrade a stronger one.

        Returns ``True`` iff a cost write was performed. The observed settlement is
        applied only when it is an upgrade over what the row already records; when
        the row already carries the same cost it is left byte-exact, and when an
        externally stronger settlement is already recorded it is preserved rather
        than lowered or discarded.
        """
        desired_state = observation.cost["state"]
        desired_amount = observation.cost.get("amount_usd")
        current = self._require_dispatch(dispatch_id)
        if (
            current.cost_state == desired_state
            and current.cost_amount_usd == desired_amount
        ):
            return False
        try:
            self._dispatch._settle_cost_in_transaction(
                dispatch_id,
                cost_state=desired_state,
                amount_usd=desired_amount,
                allow_definitive_not_incurred=(outcome == "failed"),
            )
        except DispatchConflictError:
            # The row already holds a stronger/established settlement; keep it
            # (monotonic evidence is preserved, never discarded or re-claimed).
            return False
        return True

    # -- reads --------------------------------------------------------------- #
    def get_result(self, dispatch_id: str) -> DecisionResultObservation | None:
        dispatch_id = validate_identifier(dispatch_id, "dispatch_id")
        row = self._set_row(dispatch_id)
        if row is None:
            return None
        return self._validate_set_row(row)

    def get_result_for_task(self, task_id: str) -> DecisionResultObservation | None:
        task_id = validate_identifier(task_id, "task_id")
        row = self._conn().execute(
            "SELECT * FROM decision_result_set WHERE project_id = ? AND task_id = ?",
            (self.project_id, task_id),
        ).fetchone()
        if row is None:
            return None
        return self._validate_set_row(row)

    def list_results(self) -> list[DecisionResultObservation]:
        rows = self._conn().execute(
            "SELECT * FROM decision_result_set WHERE project_id = ? ORDER BY rowid",
            (self.project_id,),
        ).fetchall()
        return [self._validate_set_row(row) for row in rows]

    def get_question_record(
        self, dispatch_id: str, question_id: str
    ) -> DecisionResultQuestion | None:
        """One stored per-question record, after the parent set revalidates.

        The parent observation is validated in full first, so a corrupt or
        incomplete set is refused rather than handing out a plausible-looking
        single row. Missing set/question simply returns ``None``.
        """
        dispatch_id = validate_identifier(dispatch_id, "dispatch_id")
        question_id = validate_identifier(question_id, "question_id")
        set_row = self._set_row(dispatch_id)
        if set_row is None:
            if self._question_row_exists(dispatch_id):
                raise DecisionResultCorruptionError(_M_PARTIAL_SET)
            return None
        self._validate_set_row(set_row)
        row = self._conn().execute(
            "SELECT * FROM decision_result_question WHERE project_id = ? "
            "AND dispatch_id = ? AND question_id = ?",
            (self.project_id, dispatch_id, question_id),
        ).fetchone()
        if row is None:
            return None
        return self._validate_question_row(row)

    def list_question_records(self, dispatch_id: str) -> list[DecisionResultQuestion]:
        """Every stored per-question record in planned order, after the set validates.

        The parent observation is validated in full first (coverage against the
        independent intent included), so an incomplete set never yields a short
        list.
        """
        dispatch_id = validate_identifier(dispatch_id, "dispatch_id")
        set_row = self._set_row(dispatch_id)
        if set_row is None:
            if self._question_row_exists(dispatch_id):
                raise DecisionResultCorruptionError(_M_PARTIAL_SET)
            return []
        self._validate_set_row(set_row)
        rows = self._conn().execute(
            "SELECT * FROM decision_result_question WHERE project_id = ? "
            "AND dispatch_id = ? ORDER BY ordinal",
            (self.project_id, dispatch_id),
        ).fetchall()
        return [self._validate_question_row(row) for row in rows]

    def _question_row_exists(self, dispatch_id: str) -> bool:
        row = self._conn().execute(
            "SELECT 1 FROM decision_result_question WHERE project_id = ? "
            "AND dispatch_id = ? LIMIT 1",
            (self.project_id, dispatch_id),
        ).fetchone()
        return row is not None

    def verify_result(self, dispatch_id: str) -> bool:
        """``True`` iff the observation exists and revalidates."""
        try:
            return self.get_result(dispatch_id) is not None
        except DecisionResultCorruptionError:
            return False

    # -- internal: parent links ---------------------------------------------- #
    def _require_dispatch(self, dispatch_id: str) -> DispatchRecord:
        try:
            stored = self._dispatch.get_dispatch(dispatch_id)
        except DispatchCorruptionError as exc:
            raise DecisionResultCorruptionError(_M_BAD_DISPATCH) from exc
        if stored is None or stored.project_id != self.project_id:
            raise DecisionResultValidationError(_M_BAD_DISPATCH)
        return stored

    def _require_intent(self, dispatch_id: str):
        try:
            intent = self._intents.get_intent(dispatch_id)
        except DecisionIntentCorruptionError as exc:
            raise DecisionResultCorruptionError(_M_BAD_INTENT) from exc
        if intent is None:
            raise DecisionResultValidationError(_M_BAD_INTENT)
        return intent

    def _require_persisted_task(self, task_id: str):
        try:
            task = self._runtime.get_task(task_id)
        except RuntimeCorruptionError as exc:
            raise DecisionResultCorruptionError(_M_BAD_INTENT) from exc
        if task is None or task.project_id != self.project_id or task.task_id != task_id:
            raise DecisionResultValidationError(_M_BAD_INTENT)
        return task

    def _read_verified_ref(self, ref: Mapping[str, str]) -> bytes:
        """Resolve a ref through the intent repository's own bounded ref reader."""
        return self._intents._read_verified_ref(ref, require_current=False)

    # -- internal: bind the trusted result to the recorded intent ------------ #
    def _assert_result_binds_intent(self, result, intent, dispatch) -> None:
        if result.endpoint != jev.ENDPOINT or result.endpoint != intent.endpoint:
            raise DecisionResultValidationError(_M_ENDPOINT)
        if result.model != jev.MODEL or result.model != intent.model:
            raise DecisionResultValidationError(_M_MODEL)
        if result.status not in jev.DISPATCH_STATUSES:
            raise DecisionResultValidationError(_M_STATUS)
        # ``blocked`` was refused *before* the send boundary, so it can never be a
        # recorded submitted observation — even a forged envelope claiming
        # ``submitted=True``/``calls_attempted=1`` is refused.
        if result.status == "blocked":
            raise DecisionResultValidationError(_M_NOT_SUBMITTED)
        if result.submitted is not True:
            raise DecisionResultValidationError(_M_NOT_SUBMITTED)
        # Plain integer quantities only: ``True``/``1.0`` are never normalized to 1.
        if _plain_int(result.attempt_no, minimum=1, message=_M_ATTEMPT) != 1:
            raise DecisionResultValidationError(_M_ATTEMPT)
        if _plain_int(result.calls_attempted, minimum=1, message=_M_ATTEMPT) != 1:
            raise DecisionResultValidationError(_M_ATTEMPT)
        if result.no_automatic_retry is not True:
            raise DecisionResultValidationError(_M_ATTEMPT)
        if result.idempotent_admission_proven is not False:
            raise DecisionResultValidationError(_M_ATTEMPT)
        _plain_int(result.request_bytes, minimum=1, message=_M_REQUEST)
        if result.http_status is not None:
            _plain_int(
                result.http_status, minimum=100, maximum=599, message=_M_STATUS
            )
        if (
            result.request_sha256 != intent.request_sha256
            or result.request_bytes != intent.request_bytes
        ):
            raise DecisionResultValidationError(_M_REQUEST)
        if result.reserve_usd != intent.reserve_usd or intent.reserve_usd != dispatch.reserve_usd:
            raise DecisionResultValidationError(_M_RESERVE)
        if result.cost_upper_bound_usd != intent.cost_upper_bound_usd:
            raise DecisionResultValidationError(_M_BOUND)
        if result.actual_model is not None and not _is_jev_model(result.actual_model):
            raise DecisionResultValidationError(_M_MODEL)
        cost = result.cost
        if not isinstance(cost, jev.CostRecord) or cost.state not in jev.COST_STATES:
            raise DecisionResultValidationError(_M_COST)
        if cost.currency != "USD":
            raise DecisionResultValidationError(_M_COST)
        if cost.route != RESULT_ROUTE or cost.accounting_bucket != RESULT_ROUTE:
            raise DecisionResultValidationError(_M_COST)
        if cost.reserve_usd != intent.reserve_usd:
            raise DecisionResultValidationError(_M_COST_RESERVE)
        if cost.state in _COST_STATES_WITH_AMOUNT:
            if _parse_exact_decimal(cost.amount_usd) is None:
                raise DecisionResultValidationError(_M_COST)
        elif cost.amount_usd is not None:
            # An unknown/not-incurred cost is never recorded as a silent 0.
            raise DecisionResultValidationError(_M_UNKNOWN_COST)
        _validate_status_envelope(
            {
                "status": result.status,
                "http_status": result.http_status,
                "actual_model": result.actual_model,
                "provider": result.provider,
                "response_id": result.response_id,
                "usage": None
                if result.usage is None
                else {
                    "input_tokens": result.usage.input_tokens,
                    "output_tokens": result.usage.output_tokens,
                },
                "cost": {"state": cost.state},
                "diagnostic": None
                if result.diagnostic is None
                else _project_diagnostic(result.diagnostic),
            }
        )

    # -- internal: build the canonical observation --------------------------- #
    def _build_observation(
        self, project_id: str, dispatch: DispatchRecord, intent, result: jev.DispatchResult
    ) -> dict[str, Any]:
        def reader(ref: Mapping[str, str]) -> bytes:
            # The intent repository's own bounded, byte/rights-verifying reader.
            return self._read_verified_ref(ref)

        questions = _rebuild_questions(intent, reader)
        if tuple(q.question_id for q in questions) != tuple(intent.question_ids()):
            raise DecisionResultCorruptionError(_M_QUESTION_COVERAGE)

        consumed = _optional_consumed(intent.consumed_versions)
        records = project_decision_records(
            result,
            questions,
            project_id=project_id,
            target=intent.target,
            question_def_ref={
                q.question_id: dict(q.question_def_ref) for q in intent.questions
            },
            criteria_ref={
                q.question_id: dict(q.criteria_ref) for q in intent.questions
            },
            dispatch_ref=dispatch.dispatch_id,
            task_context=intent.task_id,
            versions=intent.versions,
            bindings=_bindings_for(questions),
            expected_request_sha256=intent.request_sha256,
            consumed=consumed,
        )
        if tuple(r.question_id for r in records) != tuple(intent.question_ids()):
            raise DecisionResultCorruptionError(_M_QUESTION_COVERAGE)

        charges = ledger_charges(records)
        if len(charges) != 1:
            raise DecisionResultValidationError(_M_COST)
        charge = charges[0]
        cost = _validate_cost(
            charge, project_id=project_id, dispatch_id=dispatch.dispatch_id
        )
        if cost["reserve_usd"] != intent.reserve_usd:
            raise DecisionResultValidationError(_M_COST_RESERVE)

        mapping: dict[str, Any] = {
            "schema_version": RESULT_SCHEMA_VERSION,
            "result_id": _result_id(project_id, dispatch.dispatch_id),
            "route": RESULT_ROUTE,
            "project_id": project_id,
            "dispatch_id": dispatch.dispatch_id,
            "task_id": intent.task_id,
            "intent_id": intent.intent_id,
            "intent_digest": intent.content_digest,
            "input_digest": intent.input_digest,
            "version_digest": intent.version_digest,
            "request_sha256": intent.request_sha256,
            "request_bytes": intent.request_bytes,
            "cost_upper_bound_usd": intent.cost_upper_bound_usd,
            "reserve_usd": intent.reserve_usd,
            "status": result.status,
            "attempt_no": 1,
            "calls_attempted": 1,
            "prior_outcome": PRIOR_OUTCOME_NONE,
            "idempotent_admission_proven": False,
            "no_automatic_retry": True,
            "http_status": result.http_status,
            "provider": result.provider,
            "response_id": result.response_id,
            "actual_model": result.actual_model,
            "usage": (
                None
                if result.usage is None
                else {
                    "input_tokens": result.usage.input_tokens,
                    "output_tokens": result.usage.output_tokens,
                }
            ),
            "cost": cost,
            "diagnostic": (
                None
                if result.diagnostic is None
                else _project_diagnostic(result.diagnostic)
            ),
            "questions": [
                {
                    "question_id": record.question_id,
                    "question_kind": record.as_mapping()["question_kind"],
                    "record_id": record.decision_id,
                    "record_digest": record.digest,
                    "outcome": record.outcome,
                }
                for record in records
            ],
            "question_ids": [record.question_id for record in records],
            "question_count": len(records),
        }
        normalized = validate_result_set_mapping(mapping)
        canonical = canonical_json(normalized)
        if len(canonical) > MAX_RESULT_SET_BYTES:
            raise DecisionResultValidationError(_M_CANONICAL_SIZE)
        return {
            "mapping": normalized,
            "canonical": canonical,
            "records": tuple(records),
        }

    # -- internal: insert (single transaction; caller owns it) --------------- #
    def _insert_observation(self, built: dict[str, Any]) -> None:
        mapping = built["mapping"]
        canonical = built["canonical"]
        created = _utc_now()
        self._conn().execute(
            "INSERT INTO decision_result_set "
            "(project_id, dispatch_id, result_id, schema_version, task_id, "
            " intent_id, intent_digest, input_digest, version_digest, "
            " request_sha256, request_bytes, cost_upper_bound_usd, reserve_usd, "
            " status, attempt_no, prior_outcome, cost_state, cost_amount_usd, "
            " question_count, canonical, content_digest, created_at_utc) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                self.project_id,
                mapping["dispatch_id"],
                mapping["result_id"],
                mapping["schema_version"],
                mapping["task_id"],
                mapping["intent_id"],
                mapping["intent_digest"],
                mapping["input_digest"],
                mapping["version_digest"],
                mapping["request_sha256"],
                mapping["request_bytes"],
                mapping["cost_upper_bound_usd"],
                mapping["reserve_usd"],
                mapping["status"],
                mapping["attempt_no"],
                mapping["prior_outcome"],
                mapping["cost"]["state"],
                mapping["cost"].get("amount_usd"),
                mapping["question_count"],
                canonical,
                _sha256_hex(canonical),
                created,
            ),
        )
        for ordinal, record in enumerate(built["records"]):
            self._insert_question_row(mapping["dispatch_id"], ordinal, created, record)

    def _insert_question_row(
        self, dispatch_id: str, ordinal: int, created: str, record: DecisionRecord
    ) -> None:
        mapping = record.as_mapping()
        canonical = record.canonical_bytes
        self._conn().execute(
            "INSERT INTO decision_result_question "
            "(project_id, dispatch_id, result_id, record_id, question_id, "
            " question_kind, outcome, ordinal, request_sha256, canonical, "
            " content_digest, created_at_utc) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                self.project_id,
                dispatch_id,
                _result_id(self.project_id, dispatch_id),
                record.decision_id,
                mapping["question_id"],
                mapping["question_kind"],
                mapping["outcome"],
                ordinal,
                mapping["request_sha256"],
                canonical,
                record.digest,
                created,
            ),
        )

    # -- internal: row access ------------------------------------------------ #
    def _set_row(self, dispatch_id: str) -> sqlite3.Row | None:
        return self._conn().execute(
            "SELECT * FROM decision_result_set WHERE project_id = ? AND dispatch_id = ?",
            (self.project_id, dispatch_id),
        ).fetchone()

    def _question_row(self, record_id: str) -> sqlite3.Row | None:
        return self._conn().execute(
            "SELECT * FROM decision_result_question WHERE project_id = ? AND record_id = ?",
            (self.project_id, record_id),
        ).fetchone()

    # -- internal: row validation -------------------------------------------- #
    @_fail_closed_row("decision result set")
    def _validate_set_row(self, row: sqlite3.Row) -> DecisionResultObservation:
        """Structurally revalidate a stored observation; fail closed.

        Re-derives the independent pins: the parent dispatch and its pinned task,
        the recorded intent (fully revalidated, including its referenced bytes),
        the complete per-question array against the stored per-question rows, and
        the exact canonical bytes — never trusting the row's own columns.
        """
        if row["project_id"] != self.project_id:
            raise DecisionResultCorruptionError(_M_PROJECT)
        dispatch_id = _identifier(row["dispatch_id"])
        if row["result_id"] != _result_id(self.project_id, dispatch_id):
            raise DecisionResultCorruptionError(_M_BAD_ID)
        canonical = bytes(row["canonical"])
        if len(canonical) > MAX_RESULT_SET_BYTES:
            raise DecisionResultCorruptionError(_M_CANONICAL_SIZE)
        if _sha256_hex(canonical) != _bare_hex(row["content_digest"]):
            raise DecisionResultCorruptionError(_M_CANONICAL)
        parsed = _bounded_json_bytes(
            canonical,
            max_bytes=MAX_RESULT_SET_BYTES,
            too_large=_M_CANONICAL_SIZE,
            malformed=_M_CANONICAL,
        )
        mapping = validate_result_set_mapping(parsed)
        if mapping["project_id"] != self.project_id or mapping["dispatch_id"] != dispatch_id:
            raise DecisionResultCorruptionError(_M_PROJECT)
        if canonical_json(parsed) != canonical:
            raise DecisionResultCorruptionError(_M_CANONICAL_ROUNDTRIP)
        self._assert_columns_match(row, mapping)
        dispatch, intent = self._assert_parent_links(mapping)
        self._assert_question_set(mapping, dispatch, intent)
        return self._row_to_observation(row, mapping, canonical)

    @_fail_closed_row("decision result question")
    def _validate_question_row(
        self, row: sqlite3.Row, *, intent: Any = None
    ) -> DecisionResultQuestion:
        if row["project_id"] != self.project_id:
            raise DecisionResultCorruptionError(_M_PROJECT)
        canonical = bytes(row["canonical"])
        if len(canonical) > MAX_RESULT_RECORD_BYTES:
            raise DecisionResultCorruptionError(_M_CANONICAL_SIZE)
        if _sha256_hex(canonical) != _bare_hex(row["content_digest"]):
            raise DecisionResultCorruptionError(_M_RECORD_DIGEST)
        parsed = _bounded_json_bytes(
            canonical,
            max_bytes=MAX_RESULT_RECORD_BYTES,
            too_large=_M_CANONICAL_SIZE,
            malformed=_M_CANONICAL,
        )
        record = validate_decision_record_mapping(parsed)
        if canonical_json(parsed) != canonical:
            raise DecisionResultCorruptionError(_M_CANONICAL_ROUNDTRIP)
        if row["result_id"] != _result_id(self.project_id, _identifier(row["dispatch_id"])):
            raise DecisionResultCorruptionError(_M_BAD_ID)
        if record["target"].get("project_id") != self.project_id:
            raise DecisionResultCorruptionError(_M_PROJECT)
        if record["question_id"] != row["question_id"] or record["question_kind"] != row["question_kind"]:
            raise DecisionResultCorruptionError(_M_QUESTION)
        if record["question_id"] != validate_identifier(row["question_id"], "question_id"):
            raise DecisionResultCorruptionError(_M_QUESTION)
        if record["decision_id"] != row["record_id"]:
            raise DecisionResultCorruptionError(_M_BAD_ID)
        if record["request_sha256"] != row["request_sha256"]:
            raise DecisionResultCorruptionError(_M_REQUEST)
        if record["outcome"] != row["outcome"]:
            raise DecisionResultCorruptionError(_M_COLUMNS)
        # The per-question canonical record's own receipt binds the intent. A caller
        # validating a whole set passes the already-reloaded intent (reuse, no copy).
        if intent is None:
            intent = self._require_intent(row["dispatch_id"])
        else:
            if intent.dispatch_id != row["dispatch_id"]:
                raise DecisionResultCorruptionError(_M_PARENT)
        if record["request_sha256"] != intent.request_sha256:
            raise DecisionResultCorruptionError(_M_REQUEST)
        # Re-hash the actual referenced bytes on read (history-preserving).
        self._revalidate_record_refs(record)
        ordinal = row["ordinal"]
        if isinstance(ordinal, bool) or not isinstance(ordinal, int) or ordinal < 0:
            raise DecisionResultCorruptionError(_M_COLUMNS)
        _require_utc_timestamp(row["created_at_utc"])
        return DecisionResultQuestion(
            project_id=self.project_id,
            dispatch_id=row["dispatch_id"],
            result_id=row["result_id"],
            record_id=row["record_id"],
            question_id=row["question_id"],
            question_kind=row["question_kind"],
            outcome=row["outcome"],
            ordinal=ordinal,
            request_sha256=row["request_sha256"],
            content_digest=row["content_digest"],
            created_at_utc=row["created_at_utc"],
            record=record,
            canonical=canonical,
        )

    def _revalidate_record_refs(self, record: Mapping[str, Any]) -> None:
        for ref in (
            record["target"],
            record["question_def_ref"],
            record["criteria_ref"],
        ):
            try:
                self._read_verified_ref(ref)
            except DecisionIntentCorruptionError:
                raise
            except Exception as exc:  # DecisionIntentValidationError / ...
                raise DecisionResultCorruptionError(_M_BAD_INTENT) from exc

    def _assert_columns_match(self, row: sqlite3.Row, mapping: Mapping[str, Any]) -> None:
        expected = {
            "dispatch_id": mapping["dispatch_id"],
            "result_id": mapping["result_id"],
            "schema_version": mapping["schema_version"],
            "task_id": mapping["task_id"],
            "intent_id": mapping["intent_id"],
            "intent_digest": mapping["intent_digest"],
            "input_digest": mapping["input_digest"],
            "version_digest": mapping["version_digest"],
            "request_sha256": mapping["request_sha256"],
            "request_bytes": mapping["request_bytes"],
            "cost_upper_bound_usd": mapping["cost_upper_bound_usd"],
            "reserve_usd": mapping["reserve_usd"],
            "status": mapping["status"],
            "attempt_no": mapping["attempt_no"],
            "prior_outcome": mapping["prior_outcome"],
            "cost_state": mapping["cost"]["state"],
            "cost_amount_usd": mapping["cost"].get("amount_usd"),
            "question_count": mapping["question_count"],
        }
        for column, value in expected.items():
            if row[column] != value:
                raise DecisionResultCorruptionError(_M_COLUMNS)

    def _assert_parent_links(self, mapping: Mapping[str, Any]) -> tuple[Any, Any]:
        """Re-derive and compare the parent dispatch/task/intent pins.

        The intent read here is the one authoritative intent read for the whole set
        validation: the dispatch identity is already covered by it, so callers reuse
        the returned intent rather than loading it a second time. Returns
        ``(dispatch, intent)``.
        """
        try:
            dispatch = self._dispatch.get_dispatch(mapping["dispatch_id"])
        except DispatchCorruptionError as exc:
            raise DecisionResultCorruptionError(_M_PARENT) from exc
        if dispatch is None or dispatch.project_id != self.project_id:
            raise DecisionResultCorruptionError(_M_PARENT)
        if (
            dispatch.task_id != mapping["task_id"]
            or dispatch.route != RESULT_ROUTE
            or dispatch.reserve_usd != mapping["reserve_usd"]
        ):
            raise DecisionResultCorruptionError(_M_PARENT)
        # An observation is valid history only on a dispatch that reached the send
        # boundary (or a later accepted/terminal state); a ``reserved`` dispatch can
        # never have issued a request.
        if dispatch.state not in _PARENT_STATES:
            raise DecisionResultCorruptionError(_M_PARENT)
        try:
            task = self._runtime.get_task(mapping["task_id"])
        except RuntimeCorruptionError as exc:
            raise DecisionResultCorruptionError(_M_PARENT) from exc
        if task is None or task.project_id != self.project_id or task.task_id != mapping["task_id"]:
            raise DecisionResultCorruptionError(_M_PARENT)
        intent = self._require_intent(mapping["dispatch_id"])
        if (
            intent.project_id != self.project_id
            or intent.intent_id != mapping["intent_id"]
            or intent.content_digest != mapping["intent_digest"]
            or intent.task_id != mapping["task_id"]
            or intent.task_fingerprint != dispatch.task_fingerprint
            or intent.route != RESULT_ROUTE
            or intent.stage != dispatch.stage
            or intent.input_digest != mapping["input_digest"]
            or intent.version_digest != mapping["version_digest"]
            or intent.request_sha256 != mapping["request_sha256"]
            or intent.request_bytes != mapping["request_bytes"]
            or intent.reserve_usd != mapping["reserve_usd"]
            or intent.cost_upper_bound_usd != mapping["cost_upper_bound_usd"]
            or intent.bound_evidence_digest != dispatch.bound_evidence_digest
            or intent.endpoint != jev.ENDPOINT
        ):
            raise DecisionResultCorruptionError(_M_PARENT)
        # Completeness is measured against the INDEPENDENT recorded intent, not the
        # set's own array: exact order, count, and kind.
        intent_questions = intent.questions
        intent_ids = tuple(question.question_id for question in intent_questions)
        set_ids = tuple(mapping["question_ids"])
        if intent_ids != set_ids:
            raise DecisionResultCorruptionError(_M_QUESTION_COVERAGE)
        if len(mapping["questions"]) != len(intent_questions):
            raise DecisionResultCorruptionError(_M_QUESTION_COVERAGE)
        for entry, recorded in zip(mapping["questions"], intent_questions):
            if (
                entry["question_id"] != recorded.question_id
                or entry["question_kind"] != recorded.question_kind
            ):
                raise DecisionResultCorruptionError(_M_QUESTION_COVERAGE)
        return dispatch, intent

    def _assert_question_set(
        self, mapping: Mapping[str, Any], dispatch: Any, intent: Any
    ) -> None:
        """Validate the complete set against the stored rows, the intent, and the set.

        Every stored canonical record must match its own intent binding (refs,
        target, task context, descriptor digest, options/levels/kind), the intent's
        versions/consumed slots/request pins, the set's single dispatch cost, and the
        set's provider envelope — not merely the set's own self-consistent array.
        """
        rows = self._conn().execute(
            "SELECT * FROM decision_result_question WHERE project_id = ? "
            "AND dispatch_id = ? ORDER BY ordinal",
            (self.project_id, mapping["dispatch_id"]),
        ).fetchall()
        if len(rows) != mapping["question_count"]:
            raise DecisionResultCorruptionError(_M_PARTIAL_SET)
        expected_ids = [entry["record_id"] for entry in mapping["questions"]]
        found_ids = [row["record_id"] for row in rows]
        if found_ids != expected_ids:
            raise DecisionResultCorruptionError(_M_PARTIAL_SET)

        reader = self._read_verified_ref
        questions = _rebuild_questions(intent, reader)
        intent_questions = intent.questions
        set_cost = mapping["cost"]
        set_provider = {
            "request_model": intent.model,
            "actual_model": mapping["actual_model"],
            "provider": mapping["provider"],
            "response_id": mapping["response_id"],
            "input_tokens": None
            if mapping["usage"] is None
            else mapping["usage"]["input_tokens"],
            "output_tokens": None
            if mapping["usage"] is None
            else mapping["usage"]["output_tokens"],
            "http_status": mapping["http_status"],
        }
        status = mapping["status"]
        for index, (entry, row, question, recorded) in enumerate(
            zip(mapping["questions"], rows, questions, intent_questions)
        ):
            if row["ordinal"] != index:
                raise DecisionResultCorruptionError(_M_PARTIAL_SET)
            if (
                row["question_id"] != entry["question_id"]
                or row["question_kind"] != entry["question_kind"]
                or row["record_id"] != entry["record_id"]
                or row["content_digest"] != entry["record_digest"]
                or row["outcome"] != entry["outcome"]
                or row["result_id"] != mapping["result_id"]
            ):
                raise DecisionResultCorruptionError(_M_PARTIAL_SET)
            validated = self._validate_question_row(row, intent=intent)
            self._assert_record_binds_intent(
                validated.record,
                row=row,
                dispatch=dispatch,
                intent=intent,
                question=question,
                recorded=recorded,
                set_cost=set_cost,
                set_provider=set_provider,
            )
            # Status <-> per-question audit relation must be exact: a completed set
            # never carries a whole-call failure audit; a failed set carries the
            # retained global diagnostic on every supported question.
            if status == "completed":
                if "call_status" in validated.record:
                    raise DecisionResultCorruptionError(_M_CALL_STATUS)
            else:
                if question.question_kind == "box":
                    if validated.record["outcome"] != "unsupported":
                        raise DecisionResultCorruptionError(_M_CALL_STATUS)
                else:
                    if validated.record["outcome"] != "omitted":
                        raise DecisionResultCorruptionError(_M_CALL_STATUS)
                    if validated.record.get("call_status") != status:
                        raise DecisionResultCorruptionError(_M_CALL_STATUS)
                    if validated.record.get("diagnostics") != _thaw(mapping["diagnostic"]):
                        raise DecisionResultCorruptionError(_M_CALL_STATUS)

    def _assert_record_binds_intent(
        self,
        record: Mapping[str, Any],
        *,
        row: sqlite3.Row,
        dispatch: Any,
        intent: Any,
        question: jev.QuestionDef,
        recorded: Any,
        set_cost: Mapping[str, Any],
        set_provider: Mapping[str, Any],
    ) -> None:
        """One stored canonical record must match its intent binding and the set."""
        if record["question_id"] != question.question_id:
            raise DecisionResultCorruptionError(_M_QUESTION)
        if record["question_kind"] != question.question_kind:
            raise DecisionResultCorruptionError(_M_QUESTION)
        if record["question_def_digest"] != recorded.question_def_digest:
            raise DecisionResultCorruptionError(_M_QUESTION)
        if record["target"] != _thaw(intent.target):
            raise DecisionResultCorruptionError(_M_QUESTION)
        if record["question_def_ref"] != _thaw(recorded.question_def_ref):
            raise DecisionResultCorruptionError(_M_QUESTION)
        if record["criteria_ref"] != _thaw(recorded.criteria_ref):
            raise DecisionResultCorruptionError(_M_QUESTION)
        if record["task_context"] != intent.task_id:
            raise DecisionResultCorruptionError(_M_QUESTION)
        if list(record["options"]) != list(question.options):
            raise DecisionResultCorruptionError(_M_QUESTION)
        if list(record["levels"]) != list(question.levels):
            raise DecisionResultCorruptionError(_M_QUESTION)
        if record["versions"] != _thaw(intent.versions):
            raise DecisionResultCorruptionError(_M_QUESTION)
        if list(record["consumed_versions"]) != list(intent.consumed_versions):
            raise DecisionResultCorruptionError(_M_QUESTION)
        if record["request_sha256"] != intent.request_sha256:
            raise DecisionResultCorruptionError(_M_REQUEST)
        if record["cost"] != _thaw(set_cost):
            raise DecisionResultCorruptionError(_M_COST)
        if record["cost"]["dispatch_ref"] != dispatch.dispatch_id:
            raise DecisionResultCorruptionError(_M_COST)
        if record["provider"] != _thaw(set_provider):
            raise DecisionResultCorruptionError(_M_COLUMNS)
        if (
            record["is_exclusion"] is not False
            or record["grants_eligibility"] is not False
            or record["retains_candidate"] is not True
        ):
            raise DecisionResultCorruptionError(_M_COLUMNS)
        # The row columns must agree with the canonical record, too.
        if row["question_id"] != record["question_id"]:
            raise DecisionResultCorruptionError(_M_COLUMNS)
        if row["question_kind"] != record["question_kind"]:
            raise DecisionResultCorruptionError(_M_COLUMNS)
        if row["outcome"] != record["outcome"]:
            raise DecisionResultCorruptionError(_M_COLUMNS)
        if row["request_sha256"] != record["request_sha256"]:
            raise DecisionResultCorruptionError(_M_REQUEST)
        # ``record`` already passed the structural validator, so a completed set can
        # never contain an invented answer or an exclusion.

    def _row_to_observation(
        self, row: sqlite3.Row, mapping: Mapping[str, Any], canonical: bytes
    ) -> DecisionResultObservation:
        _require_utc_timestamp(row["created_at_utc"])
        return DecisionResultObservation(
            project_id=self.project_id,
            result_id=mapping["result_id"],
            schema_version=mapping["schema_version"],
            dispatch_id=mapping["dispatch_id"],
            task_id=mapping["task_id"],
            intent_id=mapping["intent_id"],
            intent_digest=mapping["intent_digest"],
            input_digest=mapping["input_digest"],
            version_digest=mapping["version_digest"],
            request_sha256=mapping["request_sha256"],
            request_bytes=mapping["request_bytes"],
            cost_upper_bound_usd=mapping["cost_upper_bound_usd"],
            reserve_usd=mapping["reserve_usd"],
            status=mapping["status"],
            attempt_no=mapping["attempt_no"],
            calls_attempted=mapping["calls_attempted"],
            prior_outcome=mapping["prior_outcome"],
            idempotent_admission_proven=mapping["idempotent_admission_proven"],
            no_automatic_retry=mapping["no_automatic_retry"],
            http_status=mapping["http_status"],
            provider=mapping["provider"],
            response_id=mapping["response_id"],
            actual_model=mapping["actual_model"],
            usage=mapping["usage"],
            cost=mapping["cost"],
            diagnostic=mapping["diagnostic"],
            questions=tuple(QuestionResultRef(**entry) for entry in mapping["questions"]),
            question_ids=tuple(mapping["question_ids"]),
            question_count=mapping["question_count"],
            content_digest=row["content_digest"],
            created_at_utc=row["created_at_utc"],
            canonical=canonical,
        )

    # -- whole-ledger integrity (shared with a later backup verifier) -------- #
    def verify_all_integrity(self) -> list[str]:
        """Return a list of integrity problems across every observation (empty = ok)."""
        return self._integrity_problems()

    def _integrity_problems(self) -> list[str]:
        problems: list[str] = []
        rows = self._conn().execute(
            "SELECT * FROM decision_result_set ORDER BY rowid"
        ).fetchall()
        seen_dispatches: set[str] = set()
        for index, row in enumerate(rows):
            if row["project_id"] != self.project_id:
                problems.append(f"decision_result_set: row {index} is a foreign project row")
                continue
            seen_dispatches.add(row["dispatch_id"])
            if not self._link_exists("runtime_dispatch", "dispatch_id", row["dispatch_id"]):
                problems.append(
                    f"decision_result_set: row {index} is dangling (missing dispatch)"
                )
                continue
            if not self._link_exists("runtime_bounded_task", "task_id", row["task_id"]):
                problems.append(
                    f"decision_result_set: row {index} is dangling (missing task)"
                )
                continue
            if not self._link_exists(
                "decision_request_intent", "dispatch_id", row["dispatch_id"]
            ):
                problems.append(
                    f"decision_result_set: row {index} is dangling (missing intent)"
                )
                continue
            try:
                self._validate_set_row(row)
            except Exception:  # noqa: BLE001 - a corrupt row must not abort the sweep
                problems.append(
                    f"decision_result_set: row {index} failed integrity revalidation"
                )
        # Orphan / partial per-question rows are never visited above.
        qrows = self._conn().execute(
            "SELECT * FROM decision_result_question ORDER BY rowid"
        ).fetchall()
        for index, row in enumerate(qrows):
            if row["project_id"] != self.project_id:
                problems.append(
                    f"decision_result_question: row {index} is a foreign project row"
                )
                continue
            if row["dispatch_id"] not in seen_dispatches:
                problems.append(
                    f"decision_result_question: row {index} is an orphan question row"
                )
                continue
            try:
                self._validate_question_row(row)
            except Exception:  # noqa: BLE001
                problems.append(
                    f"decision_result_question: row {index} failed integrity revalidation"
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
class _ReadOnlyResultStoreView:
    """The minimal read-only surface the integrity sweep reads from a store."""

    __slots__ = ("connection", "project_id", "schema_version")

    def __init__(self, conn: sqlite3.Connection, project_id: str) -> None:
        self.connection = conn
        self.project_id = project_id
        self.schema_version = 0


def decision_result_integrity_problems(
    conn: sqlite3.Connection, project_id: str
) -> list[str]:
    """Revalidate every recorded result observation for ``project_id`` (read-only).

    Reusable by a later backup verifier. Returns a list of static problem
    descriptions (empty when every observation is structurally intact, internally
    consistent, its parent dispatch/task/intent match, its recorded refs/versions
    revalidate, its complete per-question set matches the stored rows, and each
    record independently matches the recorded intent and the set). Foreign,
    orphan/partial, dangling, and corrupt rows are all reported. The connection is
    never written to.
    """
    repo = DecisionResultRepository(_ReadOnlyResultStoreView(conn, project_id))
    return repo.verify_all_integrity()
