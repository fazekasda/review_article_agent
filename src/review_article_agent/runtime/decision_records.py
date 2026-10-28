"""Pure C02 §6.2 per-question ``DecisionRecord`` projection from typed Jev outcomes.

Authority
---------

* ``docs/implementation/04-agent-runtime.md`` deliverable 4 (Jev decision adapter
  with validated outputs and durable application records) and acceptance criteria
  5 and 6 ("Model/provider errors, refusals, invalid shapes, and partial Jev
  errors are not interpreted as scientific exclusions"; "Stateless decision
  responses are persisted with rubric/model/input versions. Account separately
  for direct calls not covered by session statistics").
* ``docs/contracts/02-gates-and-runtime.md`` §6.1 (``VersionSet``), §6.2
  (``DecisionRecord``/``QuestionDef``/``AnswerUnion``/``PerQuestionError``/
  ``Diagnostic``), §6.3 (``CostRecord``), and §8.1.1 (``DispatchAttempt``), read
  as the authoritative record shapes — no field is invented here.

What this module **is**
-----------------------

A **pure, in-memory projection**: given the *trusted* typed outcome of one direct
Jev dispatch (:class:`review_article_agent.runtime.jev.DispatchResult`) plus the
exact question definitions that were submitted, it produces **one immutable
:class:`DecisionRecord` per question** — each carrying the singular canonical
typed answer (or the retained failure), the declared kind/options/levels, the
exact criteria/instructions pin, the consumed ``VersionSet`` (validated through
:class:`review_article_agent.storage.agent_runtime.PureJsonVersionSet`), the
``direct_call`` route, and the single ``CostRecord`` that references the one
dispatch.

The projection **revalidates** every input it is handed, because the underlying
frozen dataclasses (:class:`~review_article_agent.runtime.jev.DecisionOutcome`,
:class:`~review_article_agent.runtime.jev.Answer`,
:class:`~review_article_agent.runtime.jev.QuestionDef`,
:class:`~review_article_agent.runtime.jev.DispatchResult`) have **no**
``__post_init__`` guard and can be forged directly: a question definition is
rebuilt through the adapter's own strict constructor, and each outcome/answer is
checked field-by-field against the declared question before anything is frozen.

What this module **is not** (the boundary)
------------------------------------------

* It performs **no I/O**: no store, no database, no file, no network, no model
  call, no money. It therefore **cannot verify that a referenced artifact
  revision actually exists**, that its bytes match a digest, or that a version is
  the current one. A later repository owns that: it resolves each
  ``target``/``question_def_ref``/``criteria_ref`` against the project store,
  recomputes the **actual ref bytes**, and compares them with the recorded pins
  before persisting. A record produced here is **inert data ready for that
  verification**, never a verified row.
* It **grants no authority**: a ``DecisionRecord`` (or a Jev probability/choice/
  score) is a machine proposal about text. It never establishes scientific
  eligibility, admission, or exclusion; only the human-gated screening decision
  maps it (§6.2 "Raw result ≠ scientific eligibility"). Every record carries
  ``is_exclusion = false``, ``retains_candidate = true``, and
  ``grants_eligibility = false``.
* It **never fabricates** an answer, probability, choice, score, version, or
  digest. A predicate probability is the raw ``noul`` value with **no** cutoff;
  a missing/malformed/refused/omitted/unsupported/uncertain decision keeps the
  candidate and its diagnostic and is never an exclusion.
* It performs **no** ledger aggregation: the same ``CostRecord`` (one dispatch)
  is referenced by every question's record; the charge is counted **once** per
  dispatch, never summed per question.

Numeric representation
----------------------

To preserve provider precision exactly (as ``CostRecord.amount_usd`` already
does in §6.3), probabilities, scores, confidences, and ``probabilities`` values
are carried as **exact decimal strings**, never floats. This is the one place the
projection fixes a representation for the contract's numeric leaves; the values
themselves are taken verbatim from the trusted adapter result.

Every failure is a static, value-free :class:`DecisionRecordError`: a message
never echoes caller text, source bytes, a credential, a model identifier, or a
diagnostic payload.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from types import MappingProxyType
from typing import Any

from ..domain.errors import ReviewAgentError
from ..domain.records import (
    canonical_json,
    validate_identifier,
    validate_ref,
)
from ..storage.agent_runtime import (
    VERSION_SLOT_NAMES,
    PureJsonVersionSet,
)
from . import jev

__all__ = [
    "DECISION_RECORD_SCHEMA_VERSION",
    "DECISION_ROUTE",
    "DECISION_ANSWER_KINDS",
    "DECISION_QUESTION_KINDS",
    "DECISION_WIRE_TYPES",
    "DIAGNOSTIC_OUTCOMES",
    "PER_QUESTION_ERROR_OUTCOMES",
    "JEV_CONSUMED_SLOTS",
    "OPTIONAL_CONSUMED_SLOTS",
    "MAX_DECISION_RECORD_BYTES",
    "DecisionRecordError",
    "DecisionRecordValidationError",
    "DecisionRecordBindingError",
    "DecisionRecordProjectionError",
    "QuestionBinding",
    "DecisionRecord",
    "question_descriptor",
    "question_descriptor_digest",
    "project_decision_records",
    "ledger_charges",
    "validate_decision_record_mapping",
]

#: Version of the *projected decision-record representation*. Independent of the
#: store schema version and of the ``VersionSet`` it embeds.
DECISION_RECORD_SCHEMA_VERSION = 1

#: The only route a Jev ``DecisionRecord`` is projected from (§6.2/§6.3).
DECISION_ROUTE = "direct_call"

#: Canonical typed answer kinds (§6.2 ``AnswerUnion``). ``box`` is excluded.
DECISION_ANSWER_KINDS = ("predicate", "choice", "score")

#: The declared question kinds a record may carry. ``box`` is representable (so an
#: unsupported question can be recorded and diagnosed) but is never an accepted
#: live answer kind (§6.2 "Box unsupported").
DECISION_QUESTION_KINDS = ("predicate", "choice", "score", "box")

#: The adapter wire kinds (§6.2 ``wire_type``); ``predicate`` -> ``noul``.
DECISION_WIRE_TYPES = ("noul", "choice", "score")

_KIND_TO_WIRE = {"predicate": "noul", "choice": "choice", "score": "score"}

#: Outcomes that MUST carry a ``Diagnostic`` (§6.2).
DIAGNOSTIC_OUTCOMES = ("partial_error", "refusal", "malformed", "unsupported")

#: Outcomes that MUST carry a ``PerQuestionError`` (a per-question failure).
PER_QUESTION_ERROR_OUTCOMES = ("partial_error", "refusal", "malformed", "unsupported")

#: Version slots every Jev/typed-decision stage consumes (§6.1).
JEV_CONSUMED_SLOTS = ("model", "prompt", "adapter")

#: Slots a caller may additionally name as consumed (``known`` iff consumed).
OPTIONAL_CONSUMED_SLOTS = ("rubric", "parser")

#: Upper bound on the canonical bytes of one projected record, so a pathological
#: options/levels/diagnostic payload cannot produce an unbounded record.
MAX_DECISION_RECORD_BYTES = 262_144

# Bounded exact-decimal parsing (mirrors ``runtime.jev``/``runtime.jobs``): a
# bounded input length/width, bounded significant digits, and a bounded base-10
# exponent *even for zero*.
_MAX_DECIMAL_INPUT_LEN = 128
_MAX_DECIMAL_INPUT_BITS = 256
_MAX_SIGNIFICANT_DIGITS = 40
_MAX_ADJUSTED_EXPONENT = 24

_MAX_JSON_DEPTH = 64
_MAX_IDENTIFIER_CHARS = 512
_MAX_META_CHARS = 256

_HEX_RE = re.compile(r"^[0-9a-f]{64}$")

#: The exact keys of a ``RefRevision`` (C01 §3.1-3.3). Used to tell a bare single
#: ref from a per-question ``{question_id: ref}`` mapping.
_REF_KEYS = frozenset({"project_id", "artifact_id", "revision_id"})

# --------------------------------------------------------------------------- #
# Errors (static, curated, non-reflective)
# --------------------------------------------------------------------------- #
class DecisionRecordError(ReviewAgentError):
    """Base class for every decision-record projection failure.

    Every message is **static and curated**: it never echoes caller text, source
    bytes, a credential, a model identifier, or a diagnostic payload.
    """


class DecisionRecordValidationError(DecisionRecordError, ValueError):
    """An input outcome/answer/question or a record mapping is malformed."""


class DecisionRecordBindingError(DecisionRecordValidationError):
    """A supplied trusted ref, version, hash, or binding does not match."""


class DecisionRecordProjectionError(DecisionRecordError):
    """The trusted dispatch result cannot be projected (shape/coverage defect)."""


# --------------------------------------------------------------------------- #
# Static messages
# --------------------------------------------------------------------------- #
_M_BAD_REF = "a decision-record reference is not an exact same-project revision reference"
_M_BAD_PROJECT = "the decision-record project identity is not a valid identifier"
_M_BAD_ID = "a decision-record identifier is not a valid identifier"
_M_BAD_QUESTIONS = "the supplied question definitions are not valid typed questions"
_M_BAD_QUESTION_LIST = "the question definitions must be a non-empty sequence"
_M_DUP_QUESTION = "question ids in the record set must be unique"
_M_BAD_RESULT = "the dispatch result is not a trusted Jev dispatch result"
_M_BAD_ENDPOINT = "the dispatch result is not the documented direct Jev endpoint"
_M_BAD_MODEL = "the dispatch result is not the pinned Jev model"
_M_NOT_SUBMITTED = "a decision record set is projected only from a submitted dispatch"
_M_OUTCOME_COVERAGE = "the dispatch outcomes do not exactly cover the declared questions"
_M_MISSING_OUTCOME = "a declared question has no typed outcome in the dispatch result"
_M_UNKNOWN_OUTCOME = "the dispatch result carries an outcome for an undeclared question"
_M_BAD_OUTCOME = "a typed outcome is malformed and cannot be projected"
_M_OUTCOME_ID = "a typed outcome does not match its declared question"
_M_OUTCOME_KIND = "a typed outcome kind does not match its declared question kind"
_M_OUTCOME_LITERAL = "a typed outcome is not one of the documented outcome literals"
_M_ANSWER_SHAPE = "a typed answer is not a valid canonical answer for its question"
_M_ANSWER_PRESENCE = "a typed answer is present for an outcome that forbids one"
_M_ANSWER_MISSING = "an answered outcome must carry a typed answer"
_M_PREDICATE = "a predicate answer must carry exactly one raw probability in [0,1]"
_M_CHOICE = "a choice answer must name one of the declared options"
_M_SCORE = "a score answer must lie within the declared levels"
_M_LEGEND = "a score legend must map declared level indices to strings"
_M_CONFIDENCE = "an answer confidence must be a probability in [0,1]"
_M_PROBABILITIES = "answer probabilities must map declared keys to probabilities in [0,1]"
_M_PQE = "a per-question error is malformed"
_M_DIAGNOSTIC = "a diagnostic is malformed"
_M_DIAGNOSTIC_REQUIRED = "this outcome requires a persisted diagnostic"
_M_PQE_REQUIRED = "this outcome requires a persisted per-question error"
_M_BOX_ANSWER = "a box question cannot carry an answer"
_M_BOX_OUTCOME = "a box question must be recorded as unsupported"
_M_UNSUPPORTED_KIND = "an unsupported outcome is only valid for a box question"
_M_BAD_COST = "the dispatch cost record is malformed for a direct call"
_M_COST_RESERVE = "a submitted direct dispatch must carry a retained reserve"
_M_BAD_PROVIDER = "the provider metadata is malformed"
_M_BAD_VERSIONS = "the supplied version set is not a valid consumed Jev version set"
_M_VERSION_ROUTE = "a Jev decision record must use the direct_call version route"
_M_VERSION_CONSUMED = "the consumed version slots are not a valid Jev consumption set"
_M_BAD_DECIMAL = "a decision-record numeric field is not an exact bounded decimal"
_M_BAD_HASH = "a decision-record digest is not a lowercase hex SHA-256 digest"
_M_BAD_TIMESTAMP = "a decision-record timestamp is not an RFC 3339 instant"
_M_STALE_REQUEST = "the dispatch request digest does not match the expected one"
_M_BAD_BINDING = "a supplied question binding is malformed"
_M_STALE_BINDING = "a supplied question binding does not match the submitted question"
_M_MISSING_BINDING = "a supplied question binding set does not cover every question"
_M_EXTRA_BINDING = "a supplied question binding names an undeclared question"
_M_RECORD_KEYS = "a decision-record mapping has an unknown or missing field"
_M_RECORD_VERSION = "the decision-record representation version is not supported"
_M_RECORD_ID = "a decision-record id is not a stable content-derived identifier"
_M_RECORD_ROUTE = "a decision-record mapping must use the direct_call route"
_M_RECORD_BOOL = "a decision record must retain its candidate and grant no authority"
_M_RECORD_SIZE = "a decision record exceeds the permitted canonical size"
_M_RECORD_DEPTH = "a decision record nests too deeply"
_M_RECORD_SHAPE = "a decision record mapping is not a portable, bounded JSON object"
_M_REF_SINGLE = "a single ref is accepted only for a single-question projection"
_M_REF_COVERAGE = "the per-question refs must cover exactly the declared questions"
_M_COST_CONFLICT = "the same dispatch is referenced with divergent cost metadata"


# --------------------------------------------------------------------------- #
# Small numeric helpers (bounded, exact, never defaulting)
# --------------------------------------------------------------------------- #
def _err(message: str, error: type[DecisionRecordError] = DecisionRecordValidationError):
    raise error(message)


def _bounded_decimal_string(amount: Decimal) -> str | None:
    """Return the exact decimal string of *amount* iff it is finite and bounded.

    The finite check, the significant-digit count, and the base-10 exponent are
    read from ``as_tuple`` **before** any formatting, so a compact input such as
    ``Decimal("0E-1000000")`` (or ``1e-1000000``) can never expand into a
    pathological ``"f"`` string. ``None`` means the value is refused.
    """
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
    """Return the canonical exact decimal string of *value*, or ``None``.

    Accepts a bounded decimal **string** only (a float is never an exact amount);
    returns ``None`` on any defect (the caller turns that into a static error).
    """
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


def _num_to_str(value: object) -> object:
    """Convert a Decimal/int leaf to its exact decimal string; pass others through.

    A ``float``/``bool``/``str``/``None`` is returned unchanged so the record
    validator rejects it (never a float round-trip, never a silent coercion). A
    Decimal or int that is out of the bounded digit/exponent/bit range is likewise
    returned unchanged (never formatted), so a hostile value is refused by the
    downstream string checks instead of allocating an enormous representation.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, Decimal):
        text = _bounded_decimal_string(value)
        return value if text is None else text
    if isinstance(value, int):
        if value.bit_length() > _MAX_DECIMAL_INPUT_BITS:
            return value
        return str(value)
    return value


def _is_probability_string(value: object) -> bool:
    text = _parse_exact_decimal(value)
    return text is not None and Decimal(0) <= Decimal(text) <= Decimal(1)


def _is_timestamp(value: object) -> bool:
    if not isinstance(value, str) or value == "":
        return False
    text = value[:-1] + "+00:00" if value.endswith(("Z", "z")) else value
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() is not None


def _is_hex_digest(value: object) -> bool:
    return isinstance(value, str) and _HEX_RE.fullmatch(value) is not None


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


def _bounded_str(value: object) -> bool:
    return isinstance(value, str) and 0 < len(value) <= _MAX_META_CHARS


# --------------------------------------------------------------------------- #
# Freeze / thaw
# --------------------------------------------------------------------------- #
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


# --------------------------------------------------------------------------- #
# Trusted question bindings
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class QuestionBinding:
    """A trusted, declared binding for one submitted question.

    The application records, at submission time, the question's declared kind and
    ordered options/levels and the digest of the exact per-question request
    descriptor it sent. The projection re-derives the descriptor from the actual
    :class:`~review_article_agent.runtime.jev.QuestionDef` and refuses a mismatch
    (a stale/replaced definition can never be projected as if it were submitted).
    """

    question_id: str
    question_kind: str
    question_def_digest: str
    options: tuple[str, ...] = ()
    levels: tuple[str, ...] = ()


def question_descriptor(question: jev.QuestionDef) -> dict[str, Any]:
    """Return the exact per-question request descriptor for *question*.

    For a supported question this is the documented wire object
    ``{"type": wire_type, "instructions": ..., "criteria": wire_criteria}`` — the
    same value :func:`review_article_agent.runtime.jev.build_request` serializes.
    A ``box`` question has **no** wire mapping and is omitted from the wire; its
    descriptor is the declared definition (``type = "box"``, no criteria) so the
    digest still binds the declared shape.
    """
    if not isinstance(question, jev.QuestionDef):
        _err(_M_BAD_QUESTIONS)
    if question.question_kind == jev.BOX:
        return {"type": "box", "instructions": question.instructions, "criteria": None}
    return {
        "type": question.wire_type(),
        "instructions": question.instructions,
        "criteria": question.wire_criteria(),
    }


def question_descriptor_digest(question: jev.QuestionDef) -> str:
    """Return the lowercase hex SHA-256 of :func:`question_descriptor`."""
    return hashlib.sha256(canonical_json(question_descriptor(question))).hexdigest()


# --------------------------------------------------------------------------- #
# Input revalidation
# --------------------------------------------------------------------------- #
def _ref(value: object, project_id: str | None = None) -> dict[str, str]:
    if not isinstance(value, Mapping):
        _err(_M_BAD_REF, DecisionRecordBindingError)
    plain = dict(value)
    expected = project_id
    if expected is None:
        candidate = plain.get("project_id")
        if not isinstance(candidate, str):
            _err(_M_BAD_REF, DecisionRecordBindingError)
        expected = candidate
    try:
        return validate_ref(plain, expected)
    except Exception as exc:  # RecordValidationError/TypeError/ValueError
        raise DecisionRecordBindingError(_M_BAD_REF) from exc


def _per_question_refs(
    value: object, questions: Sequence[jev.QuestionDef], project_id: str
) -> dict[str, dict[str, str]]:
    """Resolve a per-question ref mapping, or a bare single ref for one question.

    C02 §6.2 gives **one** ``QuestionDef``/``criteria_ref`` per record, so a
    multi-question projection must supply an exact ``{question_id: ref}`` mapping
    covering every declared question. A bare single ref is accepted only when the
    projection carries exactly one question. No ref existence is checked here (no
    I/O): a later repository resolves each ref against the project store.
    """
    if isinstance(value, Mapping) and set(value) <= _REF_KEYS:
        if len(questions) != 1:
            _err(_M_REF_SINGLE, DecisionRecordBindingError)
        return {questions[0].question_id: _ref(value, project_id)}
    if isinstance(value, Mapping):
        declared = {q.question_id for q in questions}
        if set(value) != declared:
            _err(_M_REF_COVERAGE, DecisionRecordBindingError)
        return {qid: _ref(value[qid], project_id) for qid in declared}
    _err(_M_BAD_REF, DecisionRecordBindingError)


def _identifier(value: object) -> str:
    try:
        normalized = validate_identifier(value)
    except Exception as exc:
        raise DecisionRecordValidationError(_M_BAD_ID) from exc
    if len(normalized) > _MAX_IDENTIFIER_CHARS:
        _err(_M_BAD_ID)
    return normalized


def _revalidate_questions(questions: object) -> tuple[jev.QuestionDef, ...]:
    """Rebuild every question through the adapter's own strict constructor.

    A frozen :class:`~review_article_agent.runtime.jev.QuestionDef` can be forged
    (``object.__new__``) past its ``__post_init__`` validation; reconstructing it
    re-runs every adapter check (identifier, kind, options/levels shape,
    portable/bounded criteria) and fails closed on any defect.
    """
    if isinstance(questions, (str, bytes)) or not isinstance(questions, Sequence):
        _err(_M_BAD_QUESTION_LIST)
    if not questions:
        _err(_M_BAD_QUESTION_LIST)
    out: list[jev.QuestionDef] = []
    seen: set[str] = set()
    for question in questions:
        if not isinstance(question, jev.QuestionDef):
            _err(_M_BAD_QUESTIONS)
        try:
            rebuilt = jev.QuestionDef(
                question_id=question.question_id,
                question_kind=question.question_kind,
                instructions=question.instructions,
                options=tuple(question.options),
                levels=tuple(question.levels),
                criteria=question.criteria,
            )
        except Exception as exc:  # JevValidationError/AttributeError/TypeError/...
            raise DecisionRecordValidationError(_M_BAD_QUESTIONS) from exc
        if rebuilt.question_id in seen:
            _err(_M_DUP_QUESTION)
        seen.add(rebuilt.question_id)
        out.append(rebuilt)
    return tuple(out)


def _validate_versions(versions: object, consumed: object) -> tuple[dict[str, Any], tuple[str, ...]]:
    """Validate a consumed Jev ``VersionSet`` and return ``(mapping, slots)``."""
    if isinstance(consumed, (str, bytes)) or not isinstance(consumed, Sequence):
        _err(_M_VERSION_CONSUMED, DecisionRecordBindingError)
    extra = tuple(consumed)
    for name in extra:
        if name not in OPTIONAL_CONSUMED_SLOTS:
            _err(_M_VERSION_CONSUMED, DecisionRecordBindingError)
    slots = tuple(dict.fromkeys(JEV_CONSUMED_SLOTS + extra))
    try:
        wrapper = PureJsonVersionSet(versions, consumed=slots)
    except Exception as exc:  # RecordValidationError
        raise DecisionRecordBindingError(_M_BAD_VERSIONS) from exc
    mapping = wrapper.as_mapping()
    if mapping.get("route") != DECISION_ROUTE:
        _err(_M_VERSION_ROUTE, DecisionRecordBindingError)
    return mapping, tuple(sorted(slots))


def _validate_bindings(
    bindings: object, questions: Sequence[jev.QuestionDef]
) -> dict[str, QuestionBinding]:
    if not isinstance(bindings, Mapping):
        _err(_M_BAD_BINDING, DecisionRecordBindingError)
    declared = {q.question_id for q in questions}
    out: dict[str, QuestionBinding] = {}
    for key, binding in bindings.items():
        if not isinstance(key, str) or key not in declared:
            _err(_M_EXTRA_BINDING, DecisionRecordBindingError)
        if not isinstance(binding, QuestionBinding):
            _err(_M_BAD_BINDING, DecisionRecordBindingError)
        if binding.question_id != key:
            _err(_M_BAD_BINDING, DecisionRecordBindingError)
        out[key] = binding
    if set(out) != declared:
        _err(_M_MISSING_BINDING, DecisionRecordBindingError)
    return out


def _match_binding(binding: QuestionBinding, question: jev.QuestionDef) -> None:
    digest = question_descriptor_digest(question)
    if (
        binding.question_kind != question.question_kind
        or tuple(binding.options) != tuple(question.options)
        or tuple(binding.levels) != tuple(question.levels)
        or binding.question_def_digest != digest
    ):
        _err(_M_STALE_BINDING, DecisionRecordBindingError)


# --------------------------------------------------------------------------- #
# Outcome / answer projection
# --------------------------------------------------------------------------- #
def _project_answer(answer: jev.Answer) -> dict[str, Any]:
    """Convert a typed :class:`~review_article_agent.runtime.jev.Answer` to a map.

    This is a *conversion* only; the resulting map is validated field-by-field
    against the declared question by :func:`_check_record_mapping`.
    """
    out: dict[str, Any] = {
        "answer_kind": answer.answer_kind,
        "wire_type": answer.wire_type,
    }
    if answer.probability is not None:
        out["probability"] = _num_to_str(answer.probability)
    if answer.choice is not None:
        out["choice"] = answer.choice
    if answer.score is not None:
        out["score"] = _num_to_str(answer.score)
    if answer.legend is not None:
        out["legend"] = dict(answer.legend)
    if answer.confidence is not None:
        out["confidence"] = _num_to_str(answer.confidence)
    if answer.probabilities is not None:
        out["probabilities"] = {
            key: _num_to_str(value) for key, value in answer.probabilities.items()
        }
    return out


def _project_per_question_error(error: jev.PerQuestionError) -> dict[str, Any]:
    out: dict[str, Any] = {
        "question_id": error.question_id,
        "reason": error.reason,
        "message_redacted": error.message_redacted,
    }
    if error.code is not None:
        out["code"] = error.code
    return out


def _project_diagnostic(diagnostic: jev.Diagnostic) -> dict[str, Any]:
    return {
        "code": diagnostic.code,
        "detail_redacted": diagnostic.detail_redacted,
        "raised_at": diagnostic.raised_at,
    }


def _project_provider(result: jev.DispatchResult) -> dict[str, Any]:
    """Record the real provider model/version metadata from the trusted result."""
    usage = result.usage
    return {
        "request_model": result.model,
        "actual_model": result.actual_model,
        "provider": result.provider,
        "response_id": result.response_id,
        "input_tokens": None if usage is None else usage.input_tokens,
        "output_tokens": None if usage is None else usage.output_tokens,
        "http_status": result.http_status,
    }


def _project_cost(result: jev.DispatchResult, dispatch_ref: str) -> dict[str, Any]:
    """Validate and project the single direct-call ``CostRecord`` (§6.3)."""
    cost = result.cost
    if not isinstance(cost, jev.CostRecord):
        _err(_M_BAD_COST)
    if cost.state not in jev.COST_STATES:
        _err(_M_BAD_COST)
    if cost.currency != "USD":
        _err(_M_BAD_COST)
    if cost.route != DECISION_ROUTE or cost.accounting_bucket != DECISION_ROUTE:
        _err(_M_BAD_COST)
    out: dict[str, Any] = {
        "state": cost.state,
        "currency": "USD",
        "route": DECISION_ROUTE,
        "accounting_bucket": DECISION_ROUTE,
        "dispatch_ref": dispatch_ref,
    }
    if cost.state in ("known", "estimated"):
        amount = _parse_exact_decimal(cost.amount_usd)
        if amount is None:
            _err(_M_BAD_COST)
        out["amount_usd"] = amount
    elif cost.amount_usd is not None:
        _err(_M_BAD_COST)
    if cost.reserve_usd is not None:
        reserve = _parse_exact_decimal(cost.reserve_usd, positive=True)
        if reserve is None:
            _err(_M_BAD_COST)
        out["reserve_usd"] = reserve
    elif result.submitted:
        # A paid dispatch is admitted only with a recorded reserve (§5.6/§6.3).
        _err(_M_COST_RESERVE)
    return out


# --------------------------------------------------------------------------- #
# The record
# --------------------------------------------------------------------------- #
class _MintGuard:
    """Private sentinel type for :class:`DecisionRecord` construction."""


_MINT = _MintGuard()


class DecisionRecord:
    """One immutable, validated C02 §6.2 ``DecisionRecord`` for a single question.

    * **Minted only here.** Its constructor demands the private :data:`_MINT`
      sentinel, so a caller cannot hand-build a validated-looking record from a
      mapping; only :func:`project_decision_records` mints one, and only after
      revalidating the typed inputs.
    * **Immutable.** The mapping is deep-frozen (``MappingProxyType``/tuples) and
      attributes are read-only; :meth:`as_mapping` returns a fresh plain copy.
    * **Deterministic.** ``canonical_bytes``/``digest`` bind the exact projected
      content; the id is content-derived (no random value, no wall-clock time).
    * **Inert.** It grants no authority: ``is_exclusion`` is always ``False``,
      ``retains_candidate`` always ``True``, ``grants_eligibility`` always
      ``False``. A later repository verifies the referenced ref bytes.
    """

    __slots__ = ("_mapping", "_canonical_bytes", "_digest")

    def __init__(self, _guard: object, *, mapping: Mapping, canonical_bytes: bytes) -> None:
        if _guard is not _MINT:
            raise DecisionRecordError(
                "a decision record is minted only by project_decision_records after "
                "it has revalidated the typed inputs; it cannot be built from a "
                "mapping."
            )
        object.__setattr__(self, "_mapping", _deep_freeze(dict(mapping)))
        object.__setattr__(self, "_canonical_bytes", bytes(canonical_bytes))
        object.__setattr__(
            self, "_digest", hashlib.sha256(canonical_bytes).hexdigest()
        )

    @classmethod
    def _mint(cls, *, mapping: Mapping, canonical_bytes: bytes) -> "DecisionRecord":
        return cls(_MINT, mapping=mapping, canonical_bytes=canonical_bytes)

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("DecisionRecord is immutable.")

    def __delattr__(self, name: str) -> None:
        raise AttributeError("DecisionRecord is immutable.")

    def __repr__(self) -> str:
        return "<DecisionRecord id=" + self.decision_id + ">"

    # -- read-only surface ---------------------------------------------------- #
    @property
    def decision_id(self) -> str:
        return self._mapping["decision_id"]

    @property
    def record_id(self) -> str:
        """Alias for :attr:`decision_id` (the stable content-derived id)."""
        return self._mapping["decision_id"]

    @property
    def question_id(self) -> str:
        return self._mapping["question_id"]

    @property
    def outcome(self) -> str:
        return self._mapping["outcome"]

    @property
    def route(self) -> str:
        return self._mapping["route"]

    @property
    def dispatch_ref(self) -> str:
        return self._mapping["cost"]["dispatch_ref"]

    @property
    def is_exclusion(self) -> bool:
        """Always ``False``: a Jev answer alone never excludes a candidate."""
        return False

    @property
    def retains_candidate(self) -> bool:
        """Always ``True``: every outcome retains its candidate."""
        return True

    @property
    def grants_eligibility(self) -> bool:
        """Always ``False``: a decision record grants no scientific eligibility."""
        return False

    @property
    def canonical_bytes(self) -> bytes:
        return self._canonical_bytes

    @property
    def digest(self) -> str:
        return self._digest

    def as_mapping(self) -> dict[str, Any]:
        """Return a fresh plain (JSON-safe) copy of the record."""
        return _thaw(self._mapping)


# --------------------------------------------------------------------------- #
# Record validation (structure only; no ref existence, no I/O)
# --------------------------------------------------------------------------- #
_REQUIRED_KEYS = frozenset(
    {
        "schema_version",
        "decision_id",
        "question_id",
        "task_context",
        "question_def_ref",
        "criteria_ref",
        "target",
        "question_kind",
        "options",
        "levels",
        "question_def_digest",
        "outcome",
        "versions",
        "consumed_versions",
        "route",
        "cost",
        "provider",
        "request_sha256",
        "is_exclusion",
        "retains_candidate",
        "grants_eligibility",
    }
)
_OPTIONAL_KEYS = frozenset({"answer", "per_question_error", "diagnostics"})


def _check_options_levels(mapping: Mapping[str, Any]) -> tuple[list[str], list[str]]:
    kind = mapping["question_kind"]
    options = mapping["options"]
    levels = mapping["levels"]
    for seq in (options, levels):
        if not isinstance(seq, (list, tuple)) or any(
            not isinstance(item, str) or item == "" for item in seq
        ):
            _err(_M_ANSWER_SHAPE)
        if len(set(seq)) != len(seq):
            _err(_M_ANSWER_SHAPE)
    if kind == "choice":
        if not options or levels:
            _err(_M_ANSWER_SHAPE)
    elif kind == "score":
        if not levels or options:
            _err(_M_ANSWER_SHAPE)
    else:  # predicate / box
        if options or levels:
            _err(_M_ANSWER_SHAPE)
    return list(options), list(levels)


def _check_answer(answer: object, kind: str, options: list[str], levels: list[str]) -> None:
    if not isinstance(answer, Mapping):
        _err(_M_ANSWER_SHAPE)
    for key in answer:
        if not isinstance(key, str):
            _err(_M_ANSWER_SHAPE)
    extra = set(answer) - {
        "answer_kind",
        "wire_type",
        "probability",
        "choice",
        "score",
        "legend",
        "confidence",
        "probabilities",
    }
    if extra:
        _err(_M_ANSWER_SHAPE)
    if answer.get("answer_kind") != kind:
        _err(_M_ANSWER_SHAPE)
    if answer.get("wire_type") != _KIND_TO_WIRE.get(kind):
        _err(_M_ANSWER_SHAPE)

    if kind == "predicate":
        if set(answer) - {"answer_kind", "wire_type", "probability"}:
            _err(_M_PREDICATE)
        if not _is_probability_string(answer.get("probability")):
            _err(_M_PREDICATE)
        return

    if kind == "choice":
        if set(answer) - {
            "answer_kind",
            "wire_type",
            "choice",
            "confidence",
            "probabilities",
        }:
            _err(_M_CHOICE)
        choice = answer.get("choice")
        if not isinstance(choice, str) or choice not in set(options):
            _err(_M_CHOICE)
        declared = set(options)
    else:  # score
        if set(answer) - {
            "answer_kind",
            "wire_type",
            "score",
            "legend",
            "confidence",
            "probabilities",
        }:
            _err(_M_SCORE)
        score = _parse_exact_decimal(answer.get("score"))
        if score is None or not (Decimal(0) <= Decimal(score) <= Decimal(len(levels) - 1)):
            _err(_M_SCORE)
        legend = answer.get("legend")
        if legend is not None:
            if not isinstance(legend, Mapping):
                _err(_M_LEGEND)
            index_keys = {str(i) for i in range(len(levels))}
            for key, value in legend.items():
                if not isinstance(key, str) or key not in index_keys:
                    _err(_M_LEGEND)
                if not isinstance(value, str) or value == "":
                    _err(_M_LEGEND)
        declared = {str(i) for i in range(len(levels))}

    confidence = answer.get("confidence")
    if confidence is not None and not _is_probability_string(confidence):
        _err(_M_CONFIDENCE)
    probabilities = answer.get("probabilities")
    if probabilities is not None:
        if not isinstance(probabilities, Mapping) or not set(probabilities) <= declared:
            _err(_M_PROBABILITIES)
        for value in probabilities.values():
            if not _is_probability_string(value):
                _err(_M_PROBABILITIES)


def _check_per_question_error(pqe: object, question_id: str, kind: str) -> None:
    if not isinstance(pqe, Mapping):
        _err(_M_PQE)
    for key in pqe:
        if not isinstance(key, str):
            _err(_M_PQE)
    if set(pqe) - {"question_id", "reason", "code", "message_redacted"}:
        _err(_M_PQE)
    if pqe.get("question_id") != question_id:
        _err(_M_PQE)
    reason = pqe.get("reason")
    if reason not in jev.PER_QUESTION_ERROR_REASONS:
        _err(_M_PQE)
    if not _bounded_str(pqe.get("message_redacted")):
        _err(_M_PQE)
    code = pqe.get("code")
    if code is not None and not _bounded_str(code):
        _err(_M_PQE)
    if (reason == "unsupported_question_kind") != (kind == "box"):
        _err(_M_PQE)


def _check_diagnostic(diagnostic: object) -> None:
    if not isinstance(diagnostic, Mapping):
        _err(_M_DIAGNOSTIC)
    for key in diagnostic:
        if not isinstance(key, str):
            _err(_M_DIAGNOSTIC)
    if set(diagnostic) != {"code", "detail_redacted", "raised_at"}:
        _err(_M_DIAGNOSTIC)
    if not _bounded_str(diagnostic.get("code")):
        _err(_M_DIAGNOSTIC)
    if not _bounded_str(diagnostic.get("detail_redacted")):
        _err(_M_DIAGNOSTIC)
    if not _is_timestamp(diagnostic.get("raised_at")):
        _err(_M_DIAGNOSTIC)


def _check_cost(cost: object) -> None:
    if not isinstance(cost, Mapping):
        _err(_M_BAD_COST)
    for key in cost:
        if not isinstance(key, str):
            _err(_M_BAD_COST)
    if set(cost) - {
        "state",
        "amount_usd",
        "currency",
        "route",
        "accounting_bucket",
        "reserve_usd",
        "dispatch_ref",
    }:
        _err(_M_BAD_COST)
    state = cost.get("state")
    if state not in jev.COST_STATES:
        _err(_M_BAD_COST)
    if cost.get("currency") != "USD":
        _err(_M_BAD_COST)
    if cost.get("route") != DECISION_ROUTE or cost.get("accounting_bucket") != DECISION_ROUTE:
        _err(_M_BAD_COST)
    try:
        _identifier(cost.get("dispatch_ref"))
    except DecisionRecordValidationError as exc:
        raise DecisionRecordValidationError(_M_BAD_COST) from exc
    amount = cost.get("amount_usd")
    if state in ("known", "estimated"):
        if _parse_exact_decimal(amount) is None:
            _err(_M_BAD_COST)
    elif amount is not None:
        _err(_M_BAD_COST)
    # A submitted dispatch (the only source of a projected record) is admitted
    # only with a retained reserve (§5.6/§6.3), so the reserve is mandatory.
    if _parse_exact_decimal(cost.get("reserve_usd"), positive=True) is None:
        _err(_M_COST_RESERVE)


def _check_provider(provider: object) -> None:
    if not isinstance(provider, Mapping):
        _err(_M_BAD_PROVIDER)
    for key in provider:
        if not isinstance(key, str):
            _err(_M_BAD_PROVIDER)
    if set(provider) != {
        "request_model",
        "actual_model",
        "provider",
        "response_id",
        "input_tokens",
        "output_tokens",
        "http_status",
    }:
        _err(_M_BAD_PROVIDER)
    if provider.get("request_model") != jev.MODEL:
        _err(_M_BAD_PROVIDER)
    actual = provider.get("actual_model")
    if actual is not None and not _is_jev_model(actual):
        _err(_M_BAD_PROVIDER)
    for name in ("provider", "response_id"):
        value = provider.get(name)
        if value is not None and not _bounded_str(value):
            _err(_M_BAD_PROVIDER)
    for name in ("input_tokens", "output_tokens"):
        value = provider.get(name)
        if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
            _err(_M_BAD_PROVIDER)
    status = provider.get("http_status")
    if status is not None and (
        isinstance(status, bool) or not isinstance(status, int) or not (100 <= status <= 599)
    ):
        _err(_M_BAD_PROVIDER)


def validate_decision_record_mapping(mapping: object) -> dict[str, Any]:
    """Structurally revalidate one projected record mapping (no I/O).

    This checks the record's **internal** consistency only — the declared
    kind/options/levels, the answer/outcome/per-question-error/diagnostic shapes,
    the consumed ``VersionSet``, the ``direct_call`` cost, the provider metadata,
    and the never-exclusion invariants. It **cannot** and does **not** verify that
    a referenced revision exists or that its bytes match a digest; a later
    repository resolves each ref against the project store for that. Returns a
    fresh plain copy on success; raises a static :class:`DecisionRecordError`
    otherwise.
    """
    if not isinstance(mapping, Mapping):
        _err(_M_RECORD_KEYS)
    # Bound the untrusted mapping *before* any structural walk/thaw/freeze: a
    # cyclic, over-deep, over-large, or non-portable mapping must fail closed with
    # a static typed error, never a raw RecursionError or an unbounded expansion.
    try:
        canonical = canonical_json(mapping)
    except Exception as exc:  # RecordValidationError/ValueError/TypeError
        raise DecisionRecordValidationError(_M_RECORD_SHAPE) from exc
    if len(canonical) > MAX_DECISION_RECORD_BYTES:
        _err(_M_RECORD_SIZE)
    for key in mapping:
        if not isinstance(key, str):
            _err(_M_RECORD_KEYS)
    keys = set(mapping)
    if keys - (_REQUIRED_KEYS | _OPTIONAL_KEYS) or _REQUIRED_KEYS - keys:
        _err(_M_RECORD_KEYS)
    schema_version = mapping["schema_version"]
    if (
        isinstance(schema_version, bool)
        or not isinstance(schema_version, int)
        or schema_version != DECISION_RECORD_SCHEMA_VERSION
    ):
        _err(_M_RECORD_VERSION)

    decision_id = mapping["decision_id"]
    question_id = mapping["question_id"]
    if not isinstance(decision_id, str) or not decision_id.startswith("dec_"):
        _err(_M_RECORD_ID)
    _identifier(decision_id)
    _identifier(question_id)

    target = _ref(mapping["target"])
    project_id = target["project_id"]
    _ref(mapping["question_def_ref"], project_id)
    _ref(mapping["criteria_ref"], project_id)

    kind = mapping["question_kind"]
    if kind not in DECISION_QUESTION_KINDS:
        _err(_M_ANSWER_SHAPE)
    options, levels = _check_options_levels(mapping)

    if not _is_hex_digest(mapping["question_def_digest"]):
        _err(_M_BAD_HASH)
    # A projected record always comes from a submitted dispatch, whose request
    # digest is a real non-empty SHA-256 (§8.1.1).
    if not _is_hex_digest(mapping["request_sha256"]):
        _err(_M_BAD_HASH)

    if mapping["route"] != DECISION_ROUTE:
        _err(_M_RECORD_ROUTE)
    if (
        mapping["is_exclusion"] is not False
        or mapping["retains_candidate"] is not True
        or mapping["grants_eligibility"] is not False
    ):
        _err(_M_RECORD_BOOL)

    outcome = mapping["outcome"]
    if outcome not in jev.OUTCOMES:
        _err(_M_OUTCOME_LITERAL)

    # versions
    consumed = mapping["consumed_versions"]
    if (
        not isinstance(consumed, (list, tuple))
        or any(not isinstance(name, str) or name not in VERSION_SLOT_NAMES for name in consumed)
        or len(set(consumed)) != len(consumed)
    ):
        _err(_M_VERSION_CONSUMED)
    for required in JEV_CONSUMED_SLOTS:
        if required not in consumed:
            _err(_M_VERSION_CONSUMED)
    try:
        wrapper = PureJsonVersionSet(mapping["versions"], consumed=tuple(consumed))
    except Exception as exc:  # RecordValidationError
        raise DecisionRecordValidationError(_M_BAD_VERSIONS) from exc
    if wrapper.route != DECISION_ROUTE:
        _err(_M_VERSION_ROUTE)

    _check_cost(mapping["cost"])
    _check_provider(mapping["provider"])

    # The record id is content-derived from the exact bound inputs; a mapping
    # whose id was invented (or whose task context was dropped) is refused.
    task_context = _identifier(mapping["task_context"])
    dispatch_ref = _identifier(mapping["cost"]["dispatch_ref"])
    if decision_id != _decision_id(project_id, dispatch_ref, task_context, question_id):
        _err(_M_RECORD_ID)

    answer = mapping.get("answer")
    pqe = mapping.get("per_question_error")
    diagnostic = mapping.get("diagnostics")

    if answer is not None:
        if kind == "box":
            _err(_M_BOX_ANSWER)
        _check_answer(answer, kind, options, levels)
    if pqe is not None:
        _check_per_question_error(pqe, question_id, kind)
    if diagnostic is not None:
        _check_diagnostic(diagnostic)

    if kind == "box":
        if outcome != "unsupported":
            _err(_M_BOX_OUTCOME)
    elif outcome == "unsupported":
        _err(_M_UNSUPPORTED_KIND)

    # C02 §6.2 presence union: ``answer`` is present iff the outcome is
    # ``answered`` or ``partial_error``.
    if outcome in ("answered", "partial_error"):
        if answer is None:
            _err(_M_ANSWER_MISSING)
    else:
        if answer is not None:
            _err(_M_ANSWER_PRESENCE)
    if outcome == "answered":
        if pqe is not None:
            _err(_M_ANSWER_PRESENCE)
    elif outcome in PER_QUESTION_ERROR_OUTCOMES:
        if diagnostic is None:
            _err(_M_DIAGNOSTIC_REQUIRED)
        if pqe is None:
            _err(_M_PQE_REQUIRED)

    return _thaw(mapping)


# --------------------------------------------------------------------------- #
# Stable, content-derived record id
# --------------------------------------------------------------------------- #
def _decision_id(project_id: str, dispatch_ref: str, task_context: str, question_id: str) -> str:
    material = canonical_json([project_id, dispatch_ref, task_context, question_id])
    return "dec_" + hashlib.sha256(material).hexdigest()[:32]


# --------------------------------------------------------------------------- #
# Projection
# --------------------------------------------------------------------------- #
def _validate_result_envelope(result: object) -> jev.DispatchResult:
    if not isinstance(result, jev.DispatchResult):
        _err(_M_BAD_RESULT, DecisionRecordProjectionError)
    if result.endpoint != jev.ENDPOINT:
        _err(_M_BAD_ENDPOINT, DecisionRecordProjectionError)
    if result.model != jev.MODEL:
        _err(_M_BAD_MODEL, DecisionRecordProjectionError)
    if result.submitted is not True:
        _err(_M_NOT_SUBMITTED, DecisionRecordProjectionError)
    if isinstance(result.calls_attempted, bool) or not isinstance(
        result.calls_attempted, int
    ) or result.calls_attempted != 1:
        _err(_M_BAD_RESULT, DecisionRecordProjectionError)
    return result


def project_decision_records(
    result: jev.DispatchResult,
    questions: Sequence[jev.QuestionDef],
    *,
    project_id: object,
    target: object,
    question_def_ref: object,
    criteria_ref: object,
    dispatch_ref: object,
    task_context: object,
    versions: object,
    bindings: Mapping[str, QuestionBinding],
    expected_request_sha256: str,
    consumed: Sequence[str] = (),
) -> tuple[DecisionRecord, ...]:
    """Project one immutable ``DecisionRecord`` per submitted question.

    ``result`` is the trusted typed outcome of **one** direct Jev dispatch and
    ``questions`` are the exact definitions that were submitted. Every argument
    is revalidated; nothing is trusted merely because it is typed.

    ``target`` is the exact same-project ``{project_id, artifact_id,
    revision_id}`` reference of the decision target. Because C02 §6.2 gives
    **one** ``QuestionDef``/``criteria_ref`` per record, ``question_def_ref`` and
    ``criteria_ref`` are each either a bare single ref (accepted only when exactly
    one question is projected) or an exact ``{question_id: ref}`` mapping covering
    every declared question; the criteria refs may explicitly share one rubric
    revision. No ref existence is checked here (no I/O). ``versions`` is the
    trusted consumed ``VersionSet`` (real ``version``+``digest`` values, never
    invented); ``consumed`` may additionally name ``rubric``/``parser`` as
    consumed.

    ``bindings`` and ``expected_request_sha256`` are **required**: a projection is
    only produced from proven submitted-question bindings (each matching the
    declared kind/options/levels and the exact per-question descriptor digest of
    the wire request) and the exact dispatch request digest — an unbound or
    stale-request projection is refused.

    Every returned record references the **same** dispatch and its **single**
    ``CostRecord``; the charge is counted once (see :func:`ledger_charges`), never
    summed per question.
    """
    project = _identifier(project_id)
    target_ref = _ref(target, project)
    dispatch = _identifier(dispatch_ref)
    context = _identifier(task_context)

    canonical_questions = _revalidate_questions(questions)
    trusted = _validate_result_envelope(result)

    qdef_refs = _per_question_refs(question_def_ref, canonical_questions, project)
    crit_refs = _per_question_refs(criteria_ref, canonical_questions, project)

    version_map, consumed_slots = _validate_versions(versions, consumed)

    binding_map = _validate_bindings(bindings, canonical_questions)

    if not _is_hex_digest(expected_request_sha256):
        _err(_M_BAD_HASH, DecisionRecordBindingError)
    if trusted.request_sha256 != expected_request_sha256:
        _err(_M_STALE_REQUEST, DecisionRecordBindingError)

    outcomes = trusted.outcomes
    if not isinstance(outcomes, Mapping):
        _err(_M_BAD_RESULT, DecisionRecordProjectionError)
    declared = {q.question_id for q in canonical_questions}
    if set(outcomes) - declared:
        _err(_M_UNKNOWN_OUTCOME, DecisionRecordProjectionError)
    if declared - set(outcomes):
        _err(_M_MISSING_OUTCOME, DecisionRecordProjectionError)

    cost = _project_cost(trusted, dispatch)
    provider = _project_provider(trusted)

    records: list[DecisionRecord] = []
    for question in canonical_questions:
        qid = question.question_id
        outcome = outcomes[qid]
        if not isinstance(outcome, jev.DecisionOutcome):
            _err(_M_BAD_OUTCOME)
        if outcome.question_id != qid:
            _err(_M_OUTCOME_ID)
        if outcome.question_kind != question.question_kind:
            _err(_M_OUTCOME_KIND)
        if outcome.outcome not in jev.OUTCOMES:
            _err(_M_OUTCOME_LITERAL)

        descriptor_digest = question_descriptor_digest(question)
        _match_binding(binding_map[qid], question)

        mapping: dict[str, Any] = {
            "schema_version": DECISION_RECORD_SCHEMA_VERSION,
            "decision_id": _decision_id(project, dispatch, context, qid),
            "question_id": qid,
            "task_context": context,
            "question_def_ref": qdef_refs[qid],
            "criteria_ref": crit_refs[qid],
            "target": target_ref,
            "question_kind": question.question_kind,
            "options": list(question.options),
            "levels": list(question.levels),
            "question_def_digest": descriptor_digest,
            "outcome": outcome.outcome,
            "versions": version_map,
            "consumed_versions": list(consumed_slots),
            "route": DECISION_ROUTE,
            "cost": cost,
            "provider": provider,
            "request_sha256": trusted.request_sha256,
            "is_exclusion": False,
            "retains_candidate": True,
            "grants_eligibility": False,
        }
        if outcome.answer is not None:
            if not isinstance(outcome.answer, jev.Answer):
                _err(_M_BAD_OUTCOME)
            mapping["answer"] = _project_answer(outcome.answer)
        if outcome.per_question_error is not None:
            if not isinstance(outcome.per_question_error, jev.PerQuestionError):
                _err(_M_BAD_OUTCOME)
            mapping["per_question_error"] = _project_per_question_error(
                outcome.per_question_error
            )
        if outcome.diagnostic is not None:
            if not isinstance(outcome.diagnostic, jev.Diagnostic):
                _err(_M_BAD_OUTCOME)
            mapping["diagnostics"] = _project_diagnostic(outcome.diagnostic)

        normalized = validate_decision_record_mapping(mapping)

        canonical = canonical_json(normalized)
        if len(canonical) > MAX_DECISION_RECORD_BYTES:
            _err(_M_RECORD_SIZE)
        records.append(DecisionRecord._mint(mapping=normalized, canonical_bytes=canonical))

    return tuple(records)


def ledger_charges(records: Sequence[DecisionRecord]) -> tuple[dict[str, Any], ...]:
    """Return the **one** charge per dispatch — never a per-question sum.

    Every record for the same dispatch references the same ``CostRecord``; this
    returns the distinct charges (one per ``dispatch_ref``) in first-seen order,
    so a caller can count the dispatch once.
    """
    if isinstance(records, (str, bytes)) or not isinstance(records, Sequence):
        _err(_M_BAD_COST)
    seen: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for record in records:
        if not isinstance(record, DecisionRecord):
            _err(_M_BAD_COST)
        charge = record.as_mapping()["cost"]
        ref = charge["dispatch_ref"]
        if ref not in seen:
            seen[ref] = charge
            order.append(ref)
        elif seen[ref] != charge:
            # The same dispatch must carry one identical CostRecord; a divergent
            # duplicate is a defect, never silently collapsed to the first.
            _err(_M_COST_CONFLICT)
    return tuple(seen[ref] for ref in order)
