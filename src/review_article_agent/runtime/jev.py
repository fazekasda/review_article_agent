"""Production **direct OpenRouter Jev typed-decision adapter**.

Authority
---------

This module promotes the *retained, tested* direct-OpenRouter Jev fallback
(``spikes/runtime/live-jev/live_jev.py``) into the installed package. It
implements ``docs/implementation/04-agent-runtime.md`` deliverable 4 (adapter
with validated outputs, direct fallback) at acceptance criteria 5 and 6, and the
``DecisionRecord``/``CostRecord``/``DispatchAttempt`` representation of
``docs/contracts/02-gates-and-runtime.md`` §6.1/§6.2/§6.3/§8.1.1. ADR
``docs/decisions/001-runtime-language.md`` retains the direct-OpenRouter route as
the tested Jev route, accounted separately from OpenCode session statistics.

What it is
----------

* A **stdlib-only** typed client for the documented Decisions endpoint
  ``POST https://openrouter.ai/api/alpha/decisions``, model
  ``typesafe/jev-1.14``, bearer authentication.
* A **pure planning interface** (:func:`plan_dispatch`) that validates a typed
  request, bounds the cost from an explicit, versioned pricing/byte bound, and
  returns the exact ``reserve_usd`` the application must persist **before** a
  paid dispatch is admitted (contract §5.6).
* A **single-shot dispatcher** (:func:`dispatch_plan`/:func:`execute`) that
  performs exactly one POST, never retries, and returns **C02-friendly typed
  outcomes** — per-question decisions, envelope status, and a ``CostRecord``
  carrying provider-reported cost as an **exact decimal string**.

What it is **not**
------------------

* Not persistence, not the store, not the worker/CLI. It never writes a record;
  it returns typed values the application records (a follow-up task owns the
  durable ``DecisionRecord``/``CostRecord``/``DispatchAttempt`` rows).
* Not a scientific decision. A Jev probability/choice/score is a machine
  proposal about text; it does **not** establish scientific eligibility,
  exclusion, or selection. This adapter never emits an "exclusion" and never
  turns a missing/malformed/refused/unsupported answer into one.
* Not the native OpenCode ``generate.decisions`` route.

Safety invariants
-----------------

* **Exact endpoint only.** ``ENDPOINT`` is a module constant; a live dispatch to
  any other endpoint is refused *before* the credential is used, so a supplied
  key cannot travel to an arbitrary host. Redirects are refused and proxies are
  disabled in the real transport.
* **Explicit credential.** The key is passed in by the caller. This module never
  reads ``.env`` implicitly and never parses private credentials.
* **Bounded.** A byte bound (:data:`SAFE_MAX_REQUEST_BYTES`) and a response byte
  bound (:data:`MAX_RESPONSE_BYTES`) keep both directions finite; a timeout bounds
  the call.
* **Reserve before dispatch.** A dispatch requires an explicit, positive
  ``reserve_usd`` and refuses (no call) when the conservative upper bound cannot
  be shown ``<= reserve``. A server-side cap is sent as
  ``provider.max_price.request``.
* **Fail closed on ambiguity.** A transport failure or an ambiguous HTTP status
  (429/5xx/408/524/529) records the cost ``unknown`` and **retains** the reserve
  — never ``0``. A malformed 200 is **never** reported ``not_incurred``: it is
  ``unknown`` (reserve retained) unless the provider reported a finite,
  independently validated ``usage.cost``.
* **Raw probabilities preserved.** No threshold, cutoff, or exclusion is applied
  to a predicate probability; the raw value is retained.
* **No blind retry.** One dispatch per call, ever. Retry policy (a distinct prior
  attempt) is a later task; this adapter records ``idempotent_admission_proven =
  False`` and a single ``attempt_no``.
* **Static, redacted diagnostics.** Exceptions carry static messages; peer error
  text is only ever surfaced through :func:`redact_peer_text` in a diagnostic.

Primary sources (see the spike ``README.md`` for the fetched-page list): the Jev
hub/tutorial, the Decisions API reference, and the model page. No wire field is
invented here.
"""

from __future__ import annotations

import hashlib
import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Mapping, Sequence

from ..domain.errors import ReviewAgentError
from ..domain.records import canonical_json, validate_identifier

__all__ = [
    "ENDPOINT",
    "MODEL",
    "PRICING_VERSION",
    "SAFE_MAX_REQUEST_BYTES",
    "MAX_RESPONSE_BYTES",
    "DEFAULT_TIMEOUT_SECONDS",
    "MAX_DISPATCH_ATTEMPTS",
    "PREDICATE",
    "CHOICE",
    "SCORE",
    "BOX",
    "OUTCOMES",
    "PER_QUESTION_ERROR_REASONS",
    "COST_STATES",
    "DISPATCH_STATUSES",
    "JevError",
    "JevValidationError",
    "JevBudgetError",
    "JevAuthorizationError",
    "JevEndpointError",
    "JevCredentialError",
    "JevTransportError",
    "JevProtocolError",
    "QuestionDef",
    "Answer",
    "PerQuestionError",
    "Diagnostic",
    "DecisionOutcome",
    "Usage",
    "CostRecord",
    "ValidatedResponse",
    "DispatchPlan",
    "DispatchResult",
    "TransportResponse",
    "redact_peer_text",
    "estimate_cost_bound",
    "build_request",
    "validate_questions",
    "validate_response",
    "plan_dispatch",
    "dispatch_plan",
    "execute",
    "post_decisions",
]

# --------------------------------------------------------------------------- #
# Documented constants
# --------------------------------------------------------------------------- #

#: The only endpoint this adapter will dispatch to (checked before any call).
ENDPOINT = "https://openrouter.ai/api/alpha/decisions"
#: The pinned model ID.
MODEL = "typesafe/jev-1.14"
#: Explicit, versioned pricing/byte-bound reference (contract §5.6 "defensible
#: upper-bound reserve"). The version string names the source of the numbers.
PRICING_VERSION = "openrouter/typesafe/jev-1.14@2026-10-27"
#: Exact decimal USD per 1M input tokens (from the model page); output is free.
INPUT_USD_PER_MILLION_TOKENS = "0.042"
OUTPUT_USD_PER_MILLION_TOKENS = "0"
#: Requests above ~32 KiB (or any image) switch TypeSafe to a long-context mode;
#: staying below keeps the cheapest, default mode.
SAFE_MAX_REQUEST_BYTES = 32_768
#: Upper bound on a response body we will read (protects against unbounded input).
MAX_RESPONSE_BYTES = 1_048_576
#: Bounded wall-clock for one dispatch.
DEFAULT_TIMEOUT_SECONDS = 60.0
#: This adapter performs exactly one dispatch per call; retry is a later policy.
MAX_DISPATCH_ATTEMPTS = 1

#: Canonical question kinds and their wire spelling.
PREDICATE = "predicate"
CHOICE = "choice"
SCORE = "score"
BOX = "box"  # unsupported: no wire mapping in the retained adapter

_KIND_TO_WIRE = {PREDICATE: "noul", CHOICE: "choice", SCORE: "score"}

#: The retained adapter's documented answer envelope bounds.
OUTCOMES = (
    "answered",
    "partial_error",
    "uncertain",
    "omitted",
    "refusal",
    "malformed",
    "unsupported",
)
PER_QUESTION_ERROR_REASONS = (
    "missing_answer",
    "provider_error",
    "malformed_answer",
    "unsupported_question_kind",
)
COST_STATES = ("known", "estimated", "unknown", "not_incurred")
DISPATCH_STATUSES = (
    "completed",
    "protocol_error",
    "api_error",
    "transport_error",
    "blocked",
)

#: Cost state routing (contract §6.3).
_ROUTE = "direct_call"
_BUCKET = "direct_call"

#: HTTP statuses whose billing is ambiguous (a request may have been billed).
_API_AMBIGUOUS_STATUSES = frozenset({408, 429, 500, 502, 503, 504, 524, 529})

#: Score tolerance for a fractional index against declared levels.
_SCORE_EPS = Decimal("0.000001")

#: Bounds on an accepted exact decimal (mirrors ``runtime.jobs`` patterns): a
#: bounded input length/width, bounded significant digits, and a bounded base-10
#: exponent *even for zero*, so ``'0e-999999999'`` cannot expand into a
#: multi-megabyte string.
_MAX_ADJUSTED_EXPONENT = 24
_MAX_SIGNIFICANT_DIGITS = 40
_MAX_DECIMAL_INPUT_LEN = 128
_MAX_DECIMAL_INPUT_BITS = 256


# --------------------------------------------------------------------------- #
# Errors (static messages; never peer text, key, or source)
# --------------------------------------------------------------------------- #
class JevError(ReviewAgentError):
    """Base class for every Jev adapter failure."""


class JevValidationError(JevError, ValueError):
    """The locally-built request/plan is invalid (nothing was sent)."""


class JevBudgetError(JevError):
    """The conservative cost bound cannot fit the reserve (nothing was sent)."""


class JevAuthorizationError(JevError):
    """A dispatch was not explicitly authorized (nothing was sent)."""


class JevEndpointError(JevError):
    """A dispatch was pointed at an endpoint other than the documented one."""


class JevCredentialError(JevError):
    """No usable credential was supplied (nothing was sent)."""


class JevTransportError(JevError):
    """The request never produced a usable response (billing ambiguous)."""


class JevProtocolError(JevError):
    """A response was received but violates the documented schema."""


# --------------------------------------------------------------------------- #
# Redaction
# --------------------------------------------------------------------------- #
_KEY_RE = re.compile(r"sk-or-[A-Za-z0-9_\-]+")
_BEARER_RE = re.compile(r"Bearer\s+[A-Za-z0-9._\-]+", re.IGNORECASE)


def redact_peer_text(text: Any, *, limit: int = 300) -> str:
    """Return *text* as a credential-free, single-line, truncated string.

    Any ``sk-or-…`` run and any ``Bearer …`` header are replaced, newlines are
    flattened, and the result is truncated. This is the only way peer-provided
    text ever reaches a diagnostic.
    """
    out = str(text)
    out = _KEY_RE.sub("[REDACTED]", out)
    out = _BEARER_RE.sub("Bearer [REDACTED]", out)
    out = out.replace("\n", " ").replace("\r", " ")
    if len(out) > limit:
        out = out[:limit] + "\u2026"
    return out


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


# --------------------------------------------------------------------------- #
# Numeric helpers
# --------------------------------------------------------------------------- #
def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float, Decimal)) and not isinstance(value, bool)


def _is_finite_number(value: Any) -> bool:
    if not _is_number(value):
        return False
    try:
        return value.is_finite() if isinstance(value, Decimal) else (
            value == value and value not in (float("inf"), float("-inf"))
        )
    except Exception:  # pragma: no cover - defensive
        return False


def _as_decimal(value: Any) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _is_probability(value: Any) -> bool:
    return _is_finite_number(value) and Decimal(0) <= _as_decimal(value) <= Decimal(1)


def _bounded_decimal(
    value: object, field: str, *, strictly_positive: bool = False
) -> Decimal:
    """Parse and bound one exact decimal, returning its :class:`Decimal`.

    Fail-closed bounds applied **before** any formatting/expansion; rejects
    ``float``/``bool`` (never an inexact binary amount), non-finite values, and
    negatives. Error messages never echo the offending value. Mirrors the
    ``review_article_agent.runtime.jobs`` exact-decimal patterns.
    """
    if isinstance(value, bool) or isinstance(value, float):
        raise JevValidationError(
            f"{field}: must be an exact decimal string, not a float or bool"
        )
    if isinstance(value, str):
        if len(value) > _MAX_DECIMAL_INPUT_LEN:
            raise JevValidationError(f"{field}: decimal string is too long")
        try:
            amount = Decimal(value)
        except (InvalidOperation, ValueError) as exc:
            raise JevValidationError(f"{field}: is not a valid decimal amount") from exc
    elif isinstance(value, int):
        if value.bit_length() > _MAX_DECIMAL_INPUT_BITS:
            raise JevValidationError(f"{field}: integer is out of the permitted range")
        amount = Decimal(value)
    elif isinstance(value, Decimal):
        amount = value
    else:
        raise JevValidationError(
            f"{field}: must be an exact decimal string (got {type(value).__name__})"
        )
    if not amount.is_finite():
        raise JevValidationError(f"{field}: must be a finite decimal amount")
    _sign, digits, exponent = amount.as_tuple()
    if len(digits) > _MAX_SIGNIFICANT_DIGITS:
        raise JevValidationError(f"{field}: has too many significant digits")
    magnitude = exponent + len(digits) - 1 if amount != 0 else exponent
    if abs(magnitude) > _MAX_ADJUSTED_EXPONENT:
        raise JevValidationError(f"{field}: magnitude is out of the permitted range")
    if amount < 0:
        raise JevValidationError(f"{field}: must not be negative")
    if strictly_positive and amount == 0:
        raise JevValidationError(f"{field}: must be greater than zero")
    return amount


def _exact_decimal_string(amount: Decimal) -> str:
    return format(amount, "f")


def _parse_json(text: str) -> Any:
    """Parse JSON with ``parse_float=Decimal`` (no float round-trip).

    ``NaN``/``Infinity``/``-Infinity`` constants are rejected so the envelope
    validator never sees a non-finite number.
    """
    def _reject(_token: str) -> Any:
        raise ValueError("non-finite JSON constant")

    try:
        return json.loads(text, parse_float=Decimal, parse_constant=_reject)
    except (ValueError, TypeError) as exc:
        raise JevProtocolError("response body is not valid JSON") from exc


def estimate_cost_bound(
    request_bytes: int, *, input_usd_per_million: str = INPUT_USD_PER_MILLION_TOKENS
) -> dict:
    """Return a conservative **upper** bound on cost for a request size.

    Assumes one input token per request byte (real tokenisation is looser, so
    this over-estimates) and ignores free output tokens. All arithmetic is exact
    ``Decimal``; ``cost_upper_bound_usd`` is an exact decimal string.
    """
    if isinstance(request_bytes, bool) or not isinstance(request_bytes, int):
        raise JevValidationError("request_bytes must be an integer")
    if request_bytes < 0:
        raise JevValidationError("request_bytes must not be negative")
    if request_bytes > SAFE_MAX_REQUEST_BYTES:
        raise JevValidationError("request exceeds the default-mode byte bound")
    cost = (
        Decimal(request_bytes)
        * Decimal(input_usd_per_million)
        / Decimal(1_000_000)
    )
    return {
        "input_tokens_upper_bound": request_bytes,
        "usd_per_million_input_tokens": input_usd_per_million,
        "usd_per_million_output_tokens": OUTPUT_USD_PER_MILLION_TOKENS,
        "cost_upper_bound_usd": _exact_decimal_string(cost),
        "pricing_version": PRICING_VERSION,
    }


# --------------------------------------------------------------------------- #
# Typed question definition
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class QuestionDef:
    """One immutable typed question definition.

    ``question_kind`` is one of ``predicate``/``choice``/``score``; ``box`` is
    representable (so it can be diagnosed) but is **unsupported** and is never
    serialized to the wire. ``options`` is the declared ordered option set for a
    ``choice``; ``levels`` is the declared ordered level list for a ``score``.
    ``criteria`` optionally overrides the wire criteria.
    """

    question_id: str
    question_kind: str
    instructions: str
    options: Sequence[str] = ()
    levels: Sequence[str] = ()
    criteria: Any = None

    def __post_init__(self) -> None:
        try:
            validate_identifier(self.question_id, "question_id")
        except Exception as exc:  # RecordValidationError -> adapter error
            raise JevValidationError(
                "question_id: must be a safe non-empty identifier"
            ) from exc
        if self.question_kind not in (PREDICATE, CHOICE, SCORE, BOX):
            raise JevValidationError(
                "question_kind: must be predicate, choice, score, or box"
            )
        if not isinstance(self.instructions, str) or not self.instructions:
            raise JevValidationError("instructions: must be a non-empty string")
        options = tuple(self.options)
        levels = tuple(self.levels)
        if self.question_kind == CHOICE:
            if not options or levels:
                raise JevValidationError(
                    "choice question requires options and no levels"
                )
            for option in options:
                if not isinstance(option, str) or not option:
                    raise JevValidationError("choice options must be non-empty strings")
            if len(set(options)) != len(options):
                raise JevValidationError("choice options must be unique")
        elif self.question_kind == SCORE:
            if not levels or options:
                raise JevValidationError(
                    "score question requires levels and no options"
                )
            for level in levels:
                if not isinstance(level, str) or not level:
                    raise JevValidationError("score levels must be non-empty strings")
        elif self.question_kind == PREDICATE:
            if options or levels:
                raise JevValidationError(
                    "predicate question carries neither options nor levels"
                )
        elif options or levels:  # BOX
            raise JevValidationError("box question carries neither options nor levels")
        # frozen dataclass: normalise the tuples without breaking immutability
        object.__setattr__(self, "options", options)
        object.__setattr__(self, "levels", levels)
        if self.criteria is not None:
            self._check_criteria()

    def _check_criteria(self) -> None:
        criteria = self.criteria
        if self.question_kind == BOX:
            if criteria is not None:
                raise JevValidationError("box question must not carry criteria")
            return
        if self.question_kind == PREDICATE:
            if not isinstance(criteria, Mapping) or set(criteria) != {"true", "false"}:
                raise JevValidationError(
                    "predicate criteria must map exactly true/false"
                )
        elif self.question_kind == CHOICE:
            if not isinstance(criteria, Mapping) or set(criteria) != set(self.options):
                raise JevValidationError(
                    "choice criteria keys must equal the declared options"
                )
        else:  # SCORE
            if (
                not isinstance(criteria, (list, tuple))
                or list(criteria) != list(self.levels)
            ):
                raise JevValidationError(
                    "score criteria must equal the declared levels"
                )

    def wire_criteria(self) -> Any:
        """Return the documented ``criteria`` value for this question."""
        if self.question_kind == PREDICATE:
            return dict(self.criteria) if self.criteria is not None else {
                "true": "true",
                "false": "false",
            }
        if self.question_kind == CHOICE:
            return (
                dict(self.criteria)
                if self.criteria is not None
                else {option: option for option in self.options}
            )
        if self.question_kind == SCORE:
            return list(self.criteria) if self.criteria is not None else list(self.levels)
        raise JevValidationError("box questions have no wire mapping")

    def wire_type(self) -> str:
        if self.question_kind == BOX:
            raise JevValidationError("box questions have no wire mapping")
        return _KIND_TO_WIRE[self.question_kind]


def validate_questions(questions: Sequence[QuestionDef]) -> tuple[QuestionDef, ...]:
    """Validate an ordered question list and return it as a tuple."""
    if isinstance(questions, (str, bytes)) or not isinstance(questions, Sequence):
        raise JevValidationError("questions must be a sequence of QuestionDef")
    if not questions:
        raise JevValidationError("questions must be non-empty")
    seen: set[str] = set()
    out = []
    for question in questions:
        if not isinstance(question, QuestionDef):
            raise JevValidationError("every question must be a QuestionDef")
        if question.question_id in seen:
            raise JevValidationError("question_id must be unique")
        seen.add(question.question_id)
        out.append(question)
    return tuple(out)


def _serialize_request(
    questions: Sequence[QuestionDef],
    state: Any,
    *,
    model: str,
    server_cap_usd: str | None,
) -> dict:
    if not isinstance(model, str) or not model:
        raise JevValidationError("model must be a non-empty string")
    if state is None:
        raise JevValidationError("state is required")
    # Fail closed if the state is not portable JSON (rejects NaN, deep nesting…).
    try:
        canonical_json(state)
    except Exception as exc:  # RecordValidationError/ValueError/TypeError
        raise JevValidationError("state must be portable JSON") from exc
    supported = [q for q in questions if q.question_kind != BOX]
    if not supported:
        raise JevValidationError("no supported questions to dispatch (only box/unsupported)")
    wire: dict = {}
    for question in supported:
        wire[question.question_id] = {
            "type": question.wire_type(),
            "instructions": question.instructions,
            "criteria": question.wire_criteria(),
        }
    request: dict = {"model": model, "state": state, "questions": wire}
    if server_cap_usd is not None:
        request["provider"] = {"max_price": {"request": server_cap_usd}}
    return request


def build_request(
    questions: Sequence[QuestionDef],
    state: Any,
    *,
    model: str = MODEL,
    max_request_cost_usd: str | None = None,
) -> dict:
    """Build the documented Decisions request body (predicate -> ``noul``).

    Box/unsupported questions are omitted from the wire (they never get an
    invented answer); ``validate_response`` reports them as ``unsupported``.
    """
    canonical = validate_questions(questions)
    server_cap = None
    if max_request_cost_usd is not None:
        server_cap = _exact_decimal_string(
            _bounded_decimal(max_request_cost_usd, "max_request_cost_usd")
        )
    return _serialize_request(
        canonical, state, model=model, server_cap_usd=server_cap
    )


# --------------------------------------------------------------------------- #
# Typed decision outcomes
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Answer:
    """One canonical typed answer (contract §6.2 ``AnswerUnion``).

    A predicate answer carries the *raw* ``noul`` probability in ``[0,1]`` — no
    boolean coercion and no threshold. A choice answer's ``choice`` is one of the
    declared options. A score answer's ``score`` is an index within the declared
    levels. All numeric values are exact ``Decimal``.
    """

    answer_kind: str
    wire_type: str
    probability: Decimal | None = None
    choice: str | None = None
    score: Decimal | None = None
    legend: Mapping[str, str] | None = None
    confidence: Decimal | None = None
    probabilities: Mapping[str, Decimal] | None = None


@dataclass(frozen=True)
class PerQuestionError:
    """A per-question failure (contract §6.2 ``PerQuestionError``)."""

    question_id: str
    reason: str
    message_redacted: str
    code: str | None = None


@dataclass(frozen=True)
class Diagnostic:
    """A redacted diagnostic (contract §6.2 ``Diagnostic``)."""

    code: str
    detail_redacted: str
    raised_at: str


@dataclass(frozen=True)
class DecisionOutcome:
    """The typed outcome of one question (contract §6.2 ``DecisionRecord``)."""

    question_id: str
    question_kind: str
    outcome: str
    answer: Answer | None = None
    per_question_error: PerQuestionError | None = None
    diagnostic: Diagnostic | None = None

    def is_exclusion(self) -> bool:  # pragma: no cover - always False by design
        return False


@dataclass(frozen=True)
class Usage:
    """Token usage (no cost here; cost lives in :class:`CostRecord`)."""

    input_tokens: int | None
    output_tokens: int | None


@dataclass(frozen=True)
class CostRecord:
    """A direct-call cost (contract §6.3 ``CostRecord``).

    ``amount_usd`` is an **exact decimal string** when known; it is absent (never
    ``0``) for ``unknown``/``not_incurred``. A ``reserve_usd`` is carried for a
    paid dispatch and is retained for ``unknown``/``estimated`` costs.
    """

    state: str
    currency: str = "USD"
    route: str = _ROUTE
    accounting_bucket: str = _BUCKET
    amount_usd: str | None = None
    reserve_usd: str | None = None
    reserve_retained: bool = False
    dispatch_ref: str | None = None

    def __post_init__(self) -> None:
        if self.state not in COST_STATES:
            raise JevValidationError("cost state is not a known literal")
        if self.state in ("known", "estimated"):
            if self.amount_usd is None:
                raise JevValidationError(
                    f"cost state {self.state!r} requires amount_usd"
                )
        elif self.amount_usd is not None:
            raise JevValidationError(
                f"cost state {self.state!r} must not carry amount_usd"
            )
        if self.state == "unknown" and self.amount_usd is not None:
            raise JevValidationError("unknown cost is not zero")
        if self.accounting_bucket != _BUCKET or self.route != _ROUTE:
            raise JevValidationError(
                "direct calls must use the direct_call route/accounting bucket"
            )


@dataclass(frozen=True)
class ValidatedResponse:
    """A validated response envelope with per-question typed outcomes."""

    model: str
    provider: str | None
    response_id: str | None
    input_tokens: int
    output_tokens: int
    cost_usd: str | None
    cost_state: str
    outcomes: Mapping[str, DecisionOutcome]


# --------------------------------------------------------------------------- #
# Response validation
# --------------------------------------------------------------------------- #
def _require_probability(name: str, field_name: str, value: Any) -> Decimal:
    if not _is_probability(value):
        raise JevProtocolError(f"answer {name!r}.{field_name} must be a probability")
    return _as_decimal(value)


def _normalize_answer(name: str, question: QuestionDef, answer: Any) -> Answer:
    if not isinstance(answer, Mapping):
        raise JevProtocolError(f"answer {name!r} must be an object")
    wire_type = answer.get("type")
    expected = question.wire_type()
    if wire_type != expected:
        raise JevProtocolError(
            f"answer {name!r} type does not match its question kind"
        )
    if question.question_kind == PREDICATE:
        probability = _require_probability(name, "noul", answer.get("noul"))
        return Answer(answer_kind=PREDICATE, wire_type="noul", probability=probability)
    if question.question_kind == CHOICE:
        options = set(question.options)
        chosen = answer.get("choice")
        if not isinstance(chosen, str) or chosen not in options:
            raise JevProtocolError(
                f"answer {name!r}.choice is not one of the declared options"
            )
        confidence, probabilities = _common(answer, name, options)
        return Answer(
            answer_kind=CHOICE,
            wire_type="choice",
            choice=chosen,
            confidence=confidence,
            probabilities=probabilities,
        )
    # score
    levels = list(question.levels)
    score = answer.get("score")
    if not _is_finite_number(score):
        raise JevProtocolError(f"answer {name!r}.score must be a finite number")
    score_dec = _as_decimal(score)
    if score_dec < -_SCORE_EPS or score_dec > (len(levels) - 1) + _SCORE_EPS:
        raise JevProtocolError(
            f"answer {name!r}.score is outside the declared levels"
        )
    index_keys = {str(i) for i in range(len(levels))}
    legend = None
    raw_legend = answer.get("legend")
    if raw_legend is not None:
        if not isinstance(raw_legend, Mapping) or not set(raw_legend) <= index_keys:
            raise JevProtocolError(f"answer {name!r}.legend keys must be level indices")
        legend = {str(k): str(v) for k, v in raw_legend.items()}
    confidence, probabilities = _common(answer, name, index_keys)
    return Answer(
        answer_kind=SCORE,
        wire_type="score",
        score=score_dec,
        legend=legend,
        confidence=confidence,
        probabilities=probabilities,
    )


def _common(
    answer: Mapping[str, Any], name: str, keys: set[str]
) -> tuple[Decimal | None, Mapping[str, Decimal] | None]:
    confidence = None
    if "confidence" in answer and answer["confidence"] is not None:
        confidence = _require_probability(name, "confidence", answer["confidence"])
    probabilities = None
    raw = answer.get("probabilities")
    if raw is not None:
        if not isinstance(raw, Mapping) or not set(raw) <= keys:
            raise JevProtocolError(
                f"answer {name!r}.probabilities has undeclared keys"
            )
        probabilities = {
            str(k): _require_probability(name, f"probabilities[{k}]", v)
            for k, v in raw.items()
        }
    return confidence, probabilities


def _error_outcome(name: str, question: QuestionDef, answer: Mapping[str, Any]) -> DecisionOutcome:
    reason = answer.get("reason")
    message = redact_peer_text(answer.get("message", "")) if answer.get("message") else ""
    refused = isinstance(reason, str) and reason.lower() in ("refused", "refusal")
    outcome = "refusal" if refused else "partial_error"
    return DecisionOutcome(
        question_id=name,
        question_kind=question.question_kind,
        outcome=outcome,
        per_question_error=PerQuestionError(
            question_id=name,
            reason="provider_error",
            message_redacted=message or "provider reported a per-question error",
            code=reason if isinstance(reason, str) and reason else None,
        ),
        diagnostic=Diagnostic(
            code="provider_refusal" if refused else "provider_error",
            detail_redacted=message or "provider reported a per-question error",
            raised_at=_utc_now(),
        ),
    )


def _malformed_outcome(name: str, question: QuestionDef, detail: str) -> DecisionOutcome:
    return DecisionOutcome(
        question_id=name,
        question_kind=question.question_kind,
        outcome="malformed",
        per_question_error=PerQuestionError(
            question_id=name,
            reason="malformed_answer",
            message_redacted=detail,
        ),
        diagnostic=Diagnostic(
            code="malformed_answer", detail_redacted=detail, raised_at=_utc_now()
        ),
    )


def _unsupported_outcome(name: str, question: QuestionDef) -> DecisionOutcome:
    detail = "box question has no wire mapping in the retained adapter"
    return DecisionOutcome(
        question_id=name,
        question_kind=question.question_kind,
        outcome="unsupported",
        per_question_error=PerQuestionError(
            question_id=name,
            reason="unsupported_question_kind",
            message_redacted=detail,
        ),
        diagnostic=Diagnostic(
            code="unsupported_question_kind",
            detail_redacted=detail,
            raised_at=_utc_now(),
        ),
    )


def _extract_reported_cost(payload: Any) -> str | None:
    """Independently validate a finite, non-negative ``usage.cost``.

    Returns an exact decimal string or ``None``. Never trusts a non-finite,
    negative, or malformed value.
    """
    if not isinstance(payload, Mapping):
        return None
    usage = payload.get("usage")
    if not isinstance(usage, Mapping):
        return None
    if "cost" not in usage:
        return None
    cost = usage["cost"]
    if isinstance(cost, bool) or not _is_finite_number(cost):
        return None
    try:
        amount = _bounded_decimal(cost, "usage.cost")
    except JevValidationError:
        return None
    return _exact_decimal_string(amount)


def validate_response(
    payload: Any, questions: Sequence[QuestionDef]
) -> ValidatedResponse:
    """Validate a Decisions response against the documented schema and questions.

    Envelope problems (not an object; missing/empty/non-map ``answers``; missing
    or malformed ``usage``/``model``; undeclared question names) raise
    :class:`JevProtocolError` — a loud protocol failure, **never** a silent
    exclusion. A per-question problem (a missing answer, a defensive
    ``type:"error"`` answer, an answer that does not match its question kind, an
    out-of-range probability, a choice outside the declared options, a score
    outside the declared levels, a malformed or non-finite number, or a ``box``
    question) is preserved as a typed ``partial_error``/``refusal``/``malformed``/
    ``unsupported`` outcome with a diagnostic, and the other valid answers are
    kept.
    """
    canonical = validate_questions(questions)
    if not isinstance(payload, Mapping):
        raise JevProtocolError("response is not a JSON object")
    answers = payload.get("answers")
    if not isinstance(answers, Mapping):
        raise JevProtocolError("response.answers must be an object")
    usage = payload.get("usage")
    if not isinstance(usage, Mapping):
        raise JevProtocolError("response.usage must be an object")
    model = payload.get("model")
    if not isinstance(model, str) or not model:
        raise JevProtocolError("response.model must be a non-empty string")
    input_tokens = usage.get("input_tokens")
    output_tokens = usage.get("output_tokens")
    for label, value in (("input_tokens", input_tokens), ("output_tokens", output_tokens)):
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise JevProtocolError(
                f"response.usage.{label} must be a non-negative integer"
            )
    declared = {q.question_id for q in canonical}
    unknown = set(answers) - declared
    if unknown:
        raise JevProtocolError("response.answers contains undeclared questions")

    # ``usage.cost`` is optional; when present it MUST be a bounded, finite,
    # non-negative number (never a string/bool/float). A present-but-invalid cost
    # is an envelope defect -> loud protocol error, never a silent zero.
    raw_cost = usage.get("cost")
    if "cost" in usage and raw_cost is not None:
        cost_usd = _extract_reported_cost(payload)
        if cost_usd is None:
            raise JevProtocolError(
                "response.usage.cost must be a non-negative number"
            )
        cost_state = "known"
    else:
        cost_usd = None
        cost_state = "unknown"

    outcomes: dict[str, DecisionOutcome] = {}
    for question in canonical:
        name = question.question_id
        if question.question_kind == BOX:
            outcomes[name] = _unsupported_outcome(name, question)
            continue
        if name not in answers:
            error = PerQuestionError(
                question_id=name,
                reason="missing_answer",
                message_redacted="no answer was returned for this question",
            )
            outcomes[name] = DecisionOutcome(
                question_id=name,
                question_kind=question.question_kind,
                outcome="partial_error",
                per_question_error=error,
                diagnostic=Diagnostic(
                    code="missing_answer",
                    detail_redacted=error.message_redacted,
                    raised_at=_utc_now(),
                ),
            )
            continue
        answer = answers[name]
        if isinstance(answer, Mapping) and answer.get("type") == "error":
            outcomes[name] = _error_outcome(name, question, answer)
            continue
        try:
            normalised = _normalize_answer(name, question, answer)
        except JevProtocolError as exc:
            outcomes[name] = _malformed_outcome(name, question, redact_peer_text(exc))
            continue
        outcomes[name] = DecisionOutcome(
            question_id=name,
            question_kind=question.question_kind,
            outcome="answered",
            answer=normalised,
        )

    provider = payload.get("provider")
    response_id = payload.get("id")
    return ValidatedResponse(
        model=model,
        provider=provider if isinstance(provider, str) else None,
        response_id=response_id if isinstance(response_id, str) else None,
        input_tokens=int(input_tokens),
        output_tokens=int(output_tokens),
        cost_usd=cost_usd,
        cost_state=cost_state,
        outcomes=outcomes,
    )


# --------------------------------------------------------------------------- #
# Planning (pure; no call)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class DispatchPlan:
    """A validated, cost-bounded plan for **one** direct dispatch.

    It carries the exact ``reserve_usd`` the application must persist before the
    dispatch is admitted, plus the exact request bytes/digest and the pricing
    reference the bound was derived from. Building a plan sends nothing.
    """

    endpoint: str
    model: str
    questions: tuple[QuestionDef, ...]
    request: Mapping[str, Any]
    request_bytes: int
    request_sha256: str
    cost_upper_bound_usd: str
    pricing_version: str
    input_usd_per_million_tokens: str
    output_usd_per_million_tokens: str
    reserve_usd: str
    server_cap_usd: str | None
    timeout_seconds: float
    supported_question_ids: tuple[str, ...]
    unsupported_question_ids: tuple[str, ...]


def plan_dispatch(
    questions: Sequence[QuestionDef],
    state: Any,
    *,
    reserve_usd: object,
    model: str = MODEL,
    endpoint: str = ENDPOINT,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    server_cap: bool = True,
    max_request_bytes: int = SAFE_MAX_REQUEST_BYTES,
) -> DispatchPlan:
    """Plan one bounded dispatch. Sends nothing; raises on any defect.

    The reserve is validated as an exact, strictly positive decimal and the
    conservative cost upper bound must be ``<= reserve`` — otherwise nothing is
    issued (contract §5.6 "reserve before dispatch"). A server-side cap
    (``provider.max_price.request``) is added when ``server_cap`` is true.
    """
    if endpoint != ENDPOINT:
        raise JevEndpointError("planning is restricted to the documented endpoint")
    canonical = validate_questions(questions)
    reserve = _bounded_decimal(reserve_usd, "reserve_usd", strictly_positive=True)
    reserve_str = _exact_decimal_string(reserve)
    request = _serialize_request(
        canonical,
        state,
        model=model,
        server_cap_usd=reserve_str if server_cap else None,
    )
    body = canonical_json(request)
    if len(body) > max_request_bytes:
        raise JevValidationError("request exceeds the default-mode byte bound")
    bound = estimate_cost_bound(len(body))
    if Decimal(bound["cost_upper_bound_usd"]) > reserve:
        raise JevBudgetError("cost upper bound exceeds the reserve")
    supported = tuple(q.question_id for q in canonical if q.question_kind != BOX)
    unsupported = tuple(q.question_id for q in canonical if q.question_kind == BOX)
    return DispatchPlan(
        endpoint=endpoint,
        model=model,
        questions=canonical,
        request=request,
        request_bytes=len(body),
        request_sha256=hashlib.sha256(body).hexdigest(),
        cost_upper_bound_usd=bound["cost_upper_bound_usd"],
        pricing_version=PRICING_VERSION,
        input_usd_per_million_tokens=INPUT_USD_PER_MILLION_TOKENS,
        output_usd_per_million_tokens=OUTPUT_USD_PER_MILLION_TOKENS,
        reserve_usd=reserve_str,
        server_cap_usd=reserve_str if server_cap else None,
        timeout_seconds=float(timeout),
        supported_question_ids=supported,
        unsupported_question_ids=unsupported,
    )


# --------------------------------------------------------------------------- #
# Transport (single POST; no redirect, no proxy)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class TransportResponse:
    """A received HTTP response (or a flagged oversize body)."""

    status: int
    body_text: str
    truncated: bool = False


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse redirects so the bearer token is never forwarded elsewhere."""

    def http_error_301(self, req, fp, code, msg, headers):  # noqa: D401
        raise urllib.error.HTTPError(req.full_url, code, "redirect refused", headers, fp)

    http_error_302 = http_error_303 = http_error_307 = http_error_308 = http_error_301


def _http_opener() -> urllib.request.OpenerDirector:
    """An opener that refuses redirects and never uses a proxy.

    Passing an **empty** :class:`urllib.request.ProxyHandler` does two things: it
    makes :func:`urllib.request.build_opener` skip the ambient default
    ``ProxyHandler`` (whose ``getproxies()`` would otherwise register
    ``HTTP_PROXY``/``HTTPS_PROXY``/``no_proxy``), and the empty handler itself
    registers no ``*_open`` hook, so no request is ever routed through a proxy. A
    supplied credential therefore cannot reach an environment-configured proxy.
    """
    return urllib.request.build_opener(
        _NoRedirect(), urllib.request.ProxyHandler({})
    )


def post_decisions(
    body: bytes,
    api_key: str,
    *,
    endpoint: str = ENDPOINT,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> TransportResponse:
    """One POST attempt. Never retries; raises :class:`JevTransportError` on a
    network failure (an ambiguous, possibly-billed outcome)."""
    if endpoint != ENDPOINT:
        raise JevEndpointError("dispatch is restricted to the documented endpoint")
    if not api_key:
        raise JevCredentialError("a non-empty credential is required")
    request = urllib.request.Request(
        endpoint,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    opener = _http_opener()
    try:
        with opener.open(request, timeout=timeout) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
            truncated = len(raw) > MAX_RESPONSE_BYTES
            text = raw[:MAX_RESPONSE_BYTES].decode("utf-8", "replace")
            return TransportResponse(int(response.status), text, truncated)
    except urllib.error.HTTPError as exc:
        try:
            raw = exc.read(MAX_RESPONSE_BYTES + 1)
            truncated = len(raw) > MAX_RESPONSE_BYTES
            text = raw[:MAX_RESPONSE_BYTES].decode("utf-8", "replace")
        except Exception:  # pragma: no cover - defensive
            text, truncated = "", False
        return TransportResponse(int(exc.code), text, truncated)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise JevTransportError(
            "the request did not complete; billing is ambiguous"
        ) from exc


# --------------------------------------------------------------------------- #
# Result assembly
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class DispatchResult:
    """The typed outcome of a single dispatch attempt (C02-friendly)."""

    status: str
    calls_attempted: int
    submitted: bool
    attempt_no: int
    idempotent_admission_proven: bool
    no_automatic_retry: bool
    endpoint: str
    model: str
    actual_model: str | None
    provider: str | None
    response_id: str | None
    usage: Usage | None
    cost: CostRecord
    outcomes: Mapping[str, DecisionOutcome]
    http_status: int | None
    request_bytes: int
    request_sha256: str
    cost_upper_bound_usd: str
    reserve_usd: str
    diagnostic: Diagnostic | None = None

    def per_question_errors(self) -> Mapping[str, PerQuestionError]:
        """Every retained per-question failure (never an exclusion)."""
        return {
            name: outcome.per_question_error
            for name, outcome in self.outcomes.items()
            if outcome.per_question_error is not None
        }


def _known_cost(amount: str, reserve: str) -> CostRecord:
    return CostRecord(state="known", amount_usd=amount, reserve_usd=reserve)


def _unknown_cost(reserve: str) -> CostRecord:
    return CostRecord(state="unknown", reserve_usd=reserve, reserve_retained=True)


def _not_incurred_cost(reserve: str | None = None) -> CostRecord:
    return CostRecord(state="not_incurred", reserve_usd=None if reserve is None else reserve)


def _unsupported_outcomes(
    questions: Sequence[QuestionDef],
) -> dict[str, DecisionOutcome]:
    out: dict[str, DecisionOutcome] = {}
    for question in questions:
        if isinstance(question, QuestionDef) and question.question_kind == BOX:
            out[question.question_id] = _unsupported_outcome(
                question.question_id, question
            )
    return out


def _blocked(
    questions: Sequence[QuestionDef],
    *,
    code: str,
    detail: str,
    endpoint: str,
    model: str,
) -> DispatchResult:
    return DispatchResult(
        status="blocked",
        calls_attempted=0,
        submitted=False,
        attempt_no=0,
        idempotent_admission_proven=False,
        no_automatic_retry=True,
        endpoint=endpoint,
        model=model,
        actual_model=None,
        provider=None,
        response_id=None,
        usage=None,
        cost=_not_incurred_cost(),
        outcomes=_unsupported_outcomes(questions),
        http_status=None,
        request_bytes=0,
        request_sha256="",
        cost_upper_bound_usd="0",
        reserve_usd="0",
        diagnostic=Diagnostic(code=code, detail_redacted=detail, raised_at=_utc_now()),
    )


def dispatch_plan(
    plan: DispatchPlan,
    *,
    api_key: str,
    attempt_no: int = 1,
    timeout: float | None = None,
    transport: Callable[..., TransportResponse] = post_decisions,
) -> DispatchResult:
    """Execute one planned dispatch. Exactly one POST; never retries.

    Pre-call refusals (endpoint/credential) raise; once the request is submitted,
    no outcome raises — every result is returned so a possibly-billed call is
    never reported ``not_incurred``. A server-reported cost is an exact decimal
    string; an absent one is explicit ``unknown`` with the reserve retained.
    """
    if plan.endpoint != ENDPOINT:
        raise JevEndpointError("dispatch is restricted to the documented endpoint")
    if not api_key:
        raise JevCredentialError("a non-empty credential is required")
    if isinstance(attempt_no, bool) or not isinstance(attempt_no, int) or attempt_no < 1:
        raise JevValidationError("attempt_no must be a positive integer")

    body = canonical_json(plan.request)
    effective_timeout = plan.timeout_seconds if timeout is None else float(timeout)
    calls_attempted = 1
    try:
        response = transport(
            body, api_key, endpoint=plan.endpoint, timeout=effective_timeout
        )
    except Exception:  # noqa: BLE001 - MUST NOT escape as not_incurred
        return DispatchResult(
            status="transport_error",
            calls_attempted=calls_attempted,
            submitted=True,
            attempt_no=attempt_no,
            idempotent_admission_proven=False,
            no_automatic_retry=True,
            endpoint=plan.endpoint,
            model=plan.model,
            actual_model=None,
            provider=None,
            response_id=None,
            usage=None,
            cost=_unknown_cost(plan.reserve_usd),
            outcomes=_unsupported_outcomes(plan.questions),
            http_status=None,
            request_bytes=plan.request_bytes,
            request_sha256=plan.request_sha256,
            cost_upper_bound_usd=plan.cost_upper_bound_usd,
            reserve_usd=plan.reserve_usd,
            diagnostic=Diagnostic(
                code="transport_error",
                detail_redacted="the request did not complete; billing is ambiguous",
                raised_at=_utc_now(),
            ),
        )

    if response.truncated:
        return _protocol_error_result(
            plan,
            attempt_no,
            http_status=response.status,
            message="response body exceeded the permitted size",
            reported_cost=None,
        )

    if response.status != 200:
        definitive = response.status not in _API_AMBIGUOUS_STATUSES
        code, message = _parse_api_error(response.body_text)
        return DispatchResult(
            status="api_error",
            calls_attempted=calls_attempted,
            submitted=True,
            attempt_no=attempt_no,
            idempotent_admission_proven=False,
            no_automatic_retry=True,
            endpoint=plan.endpoint,
            model=plan.model,
            actual_model=None,
            provider=None,
            response_id=None,
            usage=None,
            cost=(
                _not_incurred_cost(plan.reserve_usd)
                if definitive
                else _unknown_cost(plan.reserve_usd)
            ),
            outcomes={},
            http_status=response.status,
            request_bytes=plan.request_bytes,
            request_sha256=plan.request_sha256,
            cost_upper_bound_usd=plan.cost_upper_bound_usd,
            reserve_usd=plan.reserve_usd,
            diagnostic=Diagnostic(
                code="api_error",
                detail_redacted=message or "provider rejected the request",
                raised_at=_utc_now(),
            ),
        )

    # --- post-submission boundary: nothing below may raise ------------------ #
    try:
        payload = _parse_json(response.body_text)
    except JevProtocolError:
        return _protocol_error_result(
            plan,
            attempt_no,
            http_status=200,
            message="response body is not valid JSON",
            reported_cost=None,
        )

    reported_cost = _extract_reported_cost(payload)
    try:
        validated = validate_response(payload, plan.questions)
    except JevProtocolError as exc:
        return _protocol_error_result(
            plan,
            attempt_no,
            http_status=200,
            message=redact_peer_text(exc),
            reported_cost=reported_cost,
        )

    cost = (
        _known_cost(validated.cost_usd, plan.reserve_usd)
        if validated.cost_usd is not None
        else _unknown_cost(plan.reserve_usd)
    )
    return DispatchResult(
        status="completed",
        calls_attempted=calls_attempted,
        submitted=True,
        attempt_no=attempt_no,
        idempotent_admission_proven=False,
        no_automatic_retry=True,
        endpoint=plan.endpoint,
        model=plan.model,
        actual_model=validated.model,
        provider=validated.provider,
        response_id=validated.response_id,
        usage=Usage(validated.input_tokens, validated.output_tokens),
        cost=cost,
        outcomes=validated.outcomes,
        http_status=200,
        request_bytes=plan.request_bytes,
        request_sha256=plan.request_sha256,
        cost_upper_bound_usd=plan.cost_upper_bound_usd,
        reserve_usd=plan.reserve_usd,
        diagnostic=None,
    )


def _protocol_error_result(
    plan: DispatchPlan,
    attempt_no: int,
    *,
    http_status: int,
    message: str,
    reported_cost: str | None,
) -> DispatchResult:
    cost = (
        _known_cost(reported_cost, plan.reserve_usd)
        if reported_cost is not None
        else _unknown_cost(plan.reserve_usd)
    )
    return DispatchResult(
        status="protocol_error",
        calls_attempted=1,
        submitted=True,
        attempt_no=attempt_no,
        idempotent_admission_proven=False,
        no_automatic_retry=True,
        endpoint=plan.endpoint,
        model=plan.model,
        actual_model=None,
        provider=None,
        response_id=None,
        usage=None,
        cost=cost,
        outcomes={},
        http_status=http_status,
        request_bytes=plan.request_bytes,
        request_sha256=plan.request_sha256,
        cost_upper_bound_usd=plan.cost_upper_bound_usd,
        reserve_usd=plan.reserve_usd,
        diagnostic=Diagnostic(
            code="protocol_error",
            detail_redacted=message,
            raised_at=_utc_now(),
        ),
    )


def _parse_api_error(body_text: str) -> tuple[int | None, str]:
    try:
        payload = json.loads(body_text)
    except (ValueError, TypeError):
        return None, redact_peer_text(body_text) or "unparseable error body"
    if isinstance(payload, Mapping):
        error = payload.get("error")
        if isinstance(error, Mapping):
            code = error.get("code") if isinstance(error.get("code"), int) else None
            return code, redact_peer_text(error.get("message", "")) or "no error message"
        if isinstance(error, str):
            return None, redact_peer_text(error)
    return None, redact_peer_text(body_text) or "unrecognized error body"


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
def execute(
    questions: Sequence[QuestionDef],
    state: Any,
    *,
    api_key: str | None,
    reserve_usd: object,
    authorized: bool,
    model: str = MODEL,
    endpoint: str = ENDPOINT,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    server_cap: bool = True,
    attempt_no: int = 1,
    transport: Callable[..., TransportResponse] = post_decisions,
) -> DispatchResult:
    """Plan and perform **one** bounded direct dispatch.

    Pre-call refusals return a typed ``blocked`` result with ``calls_attempted=0``
    and the reserve released (``not_incurred``): an unendorsed endpoint, missing
    authorization, a missing credential, or an invalid/over-budget plan. Nothing
    is sent in those cases. Once the request is submitted, exactly one attempt is
    made and no retry is ever performed.
    """
    if endpoint != ENDPOINT:
        return _blocked(
            questions,
            code="endpoint_rejected",
            detail="dispatch is restricted to the documented endpoint",
            endpoint=endpoint,
            model=model,
        )
    if not authorized:
        return _blocked(
            questions,
            code="authorization_required",
            detail="an explicit authorization is required before dispatch",
            endpoint=endpoint,
            model=model,
        )
    if not api_key:
        return _blocked(
            questions,
            code="credential_missing",
            detail="a non-empty credential is required",
            endpoint=endpoint,
            model=model,
        )
    try:
        plan = plan_dispatch(
            questions,
            state,
            reserve_usd=reserve_usd,
            model=model,
            endpoint=endpoint,
            timeout=timeout,
            server_cap=server_cap,
        )
    except JevValidationError as exc:
        return _blocked(
            questions,
            code="invalid_plan",
            detail=redact_peer_text(exc),
            endpoint=endpoint,
            model=model,
        )
    except JevBudgetError as exc:
        return _blocked(
            questions,
            code="budget_exceeded",
            detail=redact_peer_text(exc),
            endpoint=endpoint,
            model=model,
        )
    except JevError as exc:
        return _blocked(
            questions,
            code="invalid_plan",
            detail=redact_peer_text(exc),
            endpoint=endpoint,
            model=model,
        )
    return dispatch_plan(
        plan,
        api_key=api_key,
        attempt_no=attempt_no,
        timeout=timeout,
        transport=transport,
    )
