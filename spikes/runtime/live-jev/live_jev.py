#!/usr/bin/env python3
"""Direct OpenRouter Jev (typesafe/jev-1.14) decision fallback spike.

Scope: task ``E-live-jev`` (implementation step 00, criterion line 19 of
``docs/implementation/00-runtime-spike.md``: typed Jev predicate/choice/score
handling plus per-question failures).  This is the *documented direct-OpenRouter
fallback*, not the OpenCode ``generate.decisions`` route, and its accounting is
**separate** from OpenCode session statistics (see
``docs/implementation/04-agent-runtime.md`` line 22).

Primary sources (fetched 2026-10-27, no field invented):

* Jev hub        https://openrouter.ai/docs/guides/community/jev
* Jev tutorial   https://openrouter.ai/docs/guides/community/jev-tutorial
* Decisions API  https://openrouter.ai/docs/api/api-reference/alphadecisions/
                 submit-a-decisions-questions-and-answers-request
* Model page     https://openrouter.ai/typesafe/jev-1.14

Documented facts this script relies on
--------------------------------------

* Endpoint ``POST https://openrouter.ai/api/alpha/decisions`` with
  ``Authorization: Bearer $OPENROUTER_API_KEY`` and ``Content-Type:
  application/json``.  The operation overrides the OpenAPI default server with
  ``https://openrouter.ai``.
* Request ``{"model","state","questions"}`` (all required).  ``questions`` is a
  name -> question map.  Question ``type`` is one of ``noul`` (the yes/no
  *predicate* primitive), ``choice``, ``score``, ``box``.  ``noul`` and
  ``choice``/``score`` carry ``criteria`` (``noul``: ``true``/``false`` map;
  ``choice``: option -> description map; ``score``: ordered list of levels).
* Response ``{"id?","model","provider?","answers","usage"}``; ``answers`` is a
  name -> answer map.  ``noul`` answer ``{"type":"noul","noul":0..1}``;
  ``choice`` answer ``{"type":"choice","choice":str,"confidence?","probabilities?"}``;
  ``score`` answer ``{"type":"score","score":float,"confidence?","probabilities?","legend?"}``.
  ``usage`` requires integer ``input_tokens``/``output_tokens`` and documents an
  optional ``cost`` (USD).
* Pricing: ``$0.042`` / 1M **input** tokens, output tokens free, context 262,144.
  Every response reports ``usage.cost`` in USD.

Honesty boundaries (see README)
-------------------------------

* The published Decisions API has **no** per-question ``error`` answer variant;
  OpenCode's internal decision schema does.  This script treats a *missing*
  answer and a (defensive, out-of-published-schema) ``type:"error"`` answer as
  per-question failures, and never as scientific exclusions.  A per-question
  failure cannot be induced against the documented API, so that path is
  mock-only here.
* Dry run is the default.  A live call needs ``--live`` **and**
  ``--i-authorize-live`` **and** an explicit ``--max-cost-usd`` ceiling.
* At most ``MAX_LIVE_CALLS`` (2) live POSTs per invocation, never retried
  automatically: any ambiguous billing fails closed and retains the reserve.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

# --- Documented constants -------------------------------------------------

ENDPOINT = "https://openrouter.ai/api/alpha/decisions"
MODEL = "typesafe/jev-1.14"
INPUT_USD_PER_MILLION_TOKENS = 0.042
OUTPUT_USD_PER_MILLION_TOKENS = 0.0
DOC_CONTEXT_TOKENS = 262_144
# Requests above ~32 KB (or any image) run in TypeSafe long-context mode; keep
# the synthetic request far below that so the default, cheapest mode is used.
SAFE_MAX_REQUEST_BYTES = 32_768
DEFAULT_TIMEOUT_SECONDS = 60.0
DEFAULT_CEILING_USD = 0.25
MAX_LIVE_CALLS = 2
KEY_NAME = "OPENROUTER_API_KEY"

PREDICATE = "predicate"
CHOICE = "choice"
SCORE = "score"
_KIND_TO_WIRE = {PREDICATE: "noul", CHOICE: "choice", SCORE: "score"}
_WIRE_TO_KIND = {"noul": PREDICATE, "predicate": PREDICATE, "choice": CHOICE, "score": SCORE}

_SCORE_EPS = 1e-6

# --- Errors ---------------------------------------------------------------


class LiveJevError(Exception):
    """Base class for every failure this spike raises."""


class LiveJevValidationError(LiveJevError):
    """The locally-built request is invalid (nothing was sent)."""


class LiveJevBudgetError(LiveJevError):
    """The conservative cost bound cannot fit the ceiling (nothing was sent)."""


class LiveJevAuthorizationError(LiveJevError):
    """Live execution was not explicitly authorized (nothing was sent)."""


class LiveJevCredentialError(LiveJevError):
    """No usable OPENROUTER_API_KEY was found (nothing was sent)."""


class LiveJevEndpointError(LiveJevError):
    """Live mode was pointed at an endpoint other than the documented one."""


class LiveJevTransportError(LiveJevError):
    """The request never produced a usable response (billing ambiguous)."""


class LiveJevProtocolError(LiveJevError):
    """A response was received but violates the documented schema."""


# --- Redaction ------------------------------------------------------------

_KEY_RE = re.compile(r"sk-or-[A-Za-z0-9_\-]+")
_BEARER_RE = re.compile(r"Bearer\s+[A-Za-z0-9._\-]+", re.IGNORECASE)


def redact(text: Any, *, limit: int = 300) -> str:
    """Return *text* with any credential-looking run removed and truncated."""
    out = str(text)
    out = _KEY_RE.sub("[REDACTED]", out)
    out = _BEARER_RE.sub("Bearer [REDACTED]", out)
    out = out.replace("\n", " ").replace("\r", " ")
    if len(out) > limit:
        out = out[:limit] + "\u2026"
    return out


# --- Numeric helpers ------------------------------------------------------


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_finite_number(value: Any) -> bool:
    if not _is_number(value):
        return False
    try:
        return value == value and value not in (float("inf"), float("-inf"))
    except Exception:  # pragma: no cover - defensive
        return False


def _is_probability(value: Any) -> bool:
    return _is_finite_number(value) and 0.0 <= float(value) <= 1.0


# --- Synthetic fixture ----------------------------------------------------


def default_state() -> dict:
    """A tiny synthetic state.  Not a real paper; no scientific claim."""
    return {
        "fixture": "synthetic-spike-fixture-not-a-real-paper",
        "passage": (
            "In this synthetic record the intervention reduced the primary "
            "endpoint at 12 weeks."
        ),
    }


def default_questions() -> dict:
    """One predicate, one choice, one score question over the same state."""
    return {
        "is_original_report": {
            "type": PREDICATE,
            "instructions": (
                "Does the passage report its own original result rather than "
                "summarising another source?"
            ),
            "criteria": {
                "true": "It reports its own data, analysis, or result.",
                "false": "It only summarises or cites another source.",
            },
        },
        "study_design": {
            "type": CHOICE,
            "instructions": "Which design best matches the passage?",
            "criteria": {
                "randomised": "A randomised or otherwise controlled experiment.",
                "observational": "An observational or cohort study.",
                "other": "Neither of the above, or unclear from the passage.",
            },
        },
        "confidence_level": {
            "type": SCORE,
            "instructions": "How confident is this classification?",
            "criteria": ["low", "moderate", "high"],
        },
    }


# --- Request validation / serialization -----------------------------------


def validate_questions(questions: Mapping[str, Any]) -> dict:
    """Validate and canonicalise the question map.  Raises on any defect."""
    if not isinstance(questions, Mapping) or not questions:
        raise LiveJevValidationError("questions must be a non-empty mapping")
    normalized: dict = {}
    for name, question in questions.items():
        if not isinstance(name, str) or not name.strip():
            raise LiveJevValidationError("question names must be non-empty strings")
        if not isinstance(question, Mapping):
            raise LiveJevValidationError(f"question {name!r} must be an object")
        raw_kind = question.get("type")
        kind = _WIRE_TO_KIND.get(raw_kind) if isinstance(raw_kind, str) else None
        if kind is None:
            raise LiveJevValidationError(
                f"question {name!r} has unsupported type {raw_kind!r}"
            )
        instructions = question.get("instructions")
        if instructions is None or isinstance(instructions, (list, dict)) and not instructions:
            raise LiveJevValidationError(f"question {name!r} needs instructions")
        entry: dict = {"type": kind, "instructions": instructions}
        criteria = question.get("criteria")
        if kind == PREDICATE:
            if criteria is not None:
                if not isinstance(criteria, Mapping) or set(criteria) != {"true", "false"}:
                    raise LiveJevValidationError(
                        f"predicate question {name!r} criteria must map exactly true/false"
                    )
                entry["criteria"] = dict(criteria)
        elif kind == CHOICE:
            if not isinstance(criteria, Mapping) or not criteria:
                raise LiveJevValidationError(
                    f"choice question {name!r} needs a non-empty criteria map"
                )
            entry["criteria"] = dict(criteria)
        elif kind == SCORE:
            if not isinstance(criteria, list) or not criteria:
                raise LiveJevValidationError(
                    f"score question {name!r} needs a non-empty criteria list"
                )
            entry["criteria"] = list(criteria)
        normalized[name] = entry
    return normalized


def serialize_questions(questions: Mapping[str, Any]) -> dict:
    """Canonical questions -> documented wire shape (predicate -> ``noul``)."""
    out: dict = {}
    for name, question in questions.items():
        wire = dict(question)
        wire["type"] = _KIND_TO_WIRE[question["type"]]
        out[name] = wire
    return out


def build_request(
    questions: Mapping[str, Any],
    state: Any,
    *,
    model: str = MODEL,
    max_request_cost_usd: float | None = None,
) -> dict:
    """Build the documented Decisions request body."""
    canonical = validate_questions(questions)
    if state is None:
        raise LiveJevValidationError("state is required")
    request: dict = {
        "model": model,
        "state": state,
        "questions": serialize_questions(canonical),
    }
    if max_request_cost_usd is not None:
        # Server-side hard cap (documented ProviderPreferences.max_price.request,
        # USD per request).  Belt to the client-side preflight braces.
        request["provider"] = {
            "max_price": {"request": f"{max_request_cost_usd:.6f}"}
        }
    return request


def serialize_request(request: Mapping[str, Any]) -> bytes:
    return json.dumps(request, separators=(",", ":"), sort_keys=True).encode("utf-8")


def estimate_cost_bound(request_bytes: int) -> dict:
    """Conservative *upper* bound on cost.

    Assumes one input token per request byte (real tokenisation is looser than
    that, so this over-estimates) and ignores free output tokens.
    """
    tokens_upper = int(request_bytes)
    cost = tokens_upper * INPUT_USD_PER_MILLION_TOKENS / 1_000_000
    return {
        "input_tokens_upper_bound": tokens_upper,
        "usd_per_million_input_tokens": INPUT_USD_PER_MILLION_TOKENS,
        "usd_per_million_output_tokens": OUTPUT_USD_PER_MILLION_TOKENS,
        "cost_upper_bound_usd": round(cost, 12),
    }


def preflight_cost(request_bytes: int, ceiling_usd: float | None) -> dict:
    """Enforce the ceiling *before* any call.  Raises when unbounded/over."""
    if request_bytes > SAFE_MAX_REQUEST_BYTES:
        raise LiveJevValidationError(
            f"request is {request_bytes} bytes, above the {SAFE_MAX_REQUEST_BYTES}-byte "
            "default-mode limit"
        )
    bound = estimate_cost_bound(request_bytes)
    if ceiling_usd is None or ceiling_usd <= 0:
        raise LiveJevBudgetError("a positive --max-cost-usd ceiling is required")
    if bound["cost_upper_bound_usd"] > ceiling_usd:
        raise LiveJevBudgetError(
            f"cost upper bound {bound['cost_upper_bound_usd']} exceeds ceiling "
            f"{ceiling_usd}"
        )
    bound["ceiling_usd"] = ceiling_usd
    bound["within_ceiling"] = True
    return bound


# --- Answer validation ----------------------------------------------------


def _require_probability(value: Any, where: str) -> float:
    if not _is_probability(value):
        raise LiveJevProtocolError(f"{where} must be a finite probability in [0,1]")
    return float(value)


def _normalize_answer(name: str, question: Mapping[str, Any], answer: Any) -> dict:
    if not isinstance(answer, Mapping):
        raise LiveJevProtocolError(f"answer {name!r} must be an object")
    wire_type = answer.get("type")
    kind = question["type"]
    if wire_type != _KIND_TO_WIRE[kind]:
        raise LiveJevProtocolError(
            f"answer {name!r} type {wire_type!r} does not match question type {kind!r}"
        )
    if kind == PREDICATE:
        prob = _require_probability(answer.get("noul"), f"answer {name!r}.noul")
        return {"type": PREDICATE, "probability": prob, "wire_type": "noul"}
    if kind == CHOICE:
        options = set(question["criteria"])
        chosen = answer.get("choice")
        if not isinstance(chosen, str) or chosen not in options:
            raise LiveJevProtocolError(
                f"answer {name!r}.choice {redact(chosen)} is not one of the declared options"
            )
        out = {"type": CHOICE, "choice": chosen, "wire_type": "choice"}
        _attach_common(name, answer, options, out)
        return out
    # score
    levels = question["criteria"]
    score = answer.get("score")
    if not _is_finite_number(score):
        raise LiveJevProtocolError(f"answer {name!r}.score must be a finite number")
    score = float(score)
    if score < -_SCORE_EPS or score > (len(levels) - 1) + _SCORE_EPS:
        raise LiveJevProtocolError(
            f"answer {name!r}.score {score} is outside the declared {len(levels)} levels"
        )
    index_keys = {str(i) for i in range(len(levels))}
    out = {"type": SCORE, "score": score, "wire_type": "score"}
    legend = answer.get("legend")
    if legend is not None:
        if not isinstance(legend, Mapping) or not set(legend) <= index_keys:
            raise LiveJevProtocolError(
                f"answer {name!r}.legend keys must be declared level indices"
            )
        out["legend"] = {str(k): v for k, v in legend.items()}
    _attach_common(name, answer, index_keys, out)
    return out


def _attach_common(name: str, answer: Mapping[str, Any], keys: set[str], out: dict) -> None:
    confidence = answer.get("confidence")
    if confidence is not None:
        out["confidence"] = _require_probability(confidence, f"answer {name!r}.confidence")
    probabilities = answer.get("probabilities")
    if probabilities is not None:
        if not isinstance(probabilities, Mapping):
            raise LiveJevProtocolError(f"answer {name!r}.probabilities must be an object")
        if not set(probabilities) <= keys:
            raise LiveJevProtocolError(
                f"answer {name!r}.probabilities has undeclared keys"
            )
        cleaned: dict = {}
        for key, value in probabilities.items():
            cleaned[str(key)] = _require_probability(value, f"answer {name!r}.probabilities[{key}]")
        out["probabilities"] = cleaned


@dataclass
class NormalizedDecisions:
    model: str
    provider: str | None
    response_id: str | None
    usage_input_tokens: int
    usage_output_tokens: int
    cost_usd: float | None
    cost_status: str
    answers: dict = field(default_factory=dict)
    errors: dict = field(default_factory=dict)

    def to_report(self) -> dict:
        return {
            "response_model": self.model,
            "provider": self.provider,
            "response_id": self.response_id,
            "usage": {
                "input_tokens": self.usage_input_tokens,
                "output_tokens": self.usage_output_tokens,
                "cost_usd": self.cost_usd,
                "cost_status": self.cost_status,
            },
            "answers": self.answers,
            "per_question_errors": self.errors,
            "valid_answer_count": len(self.answers),
            "error_answer_count": len(self.errors),
        }


def validate_response(payload: Any, questions: Mapping[str, Any]) -> NormalizedDecisions:
    """Validate a Decisions response against the documented schema and sent questions.

    Envelope/shape problems raise ``LiveJevProtocolError``.  A *missing* answer or
    a defensive ``type:"error"`` answer for a declared question is preserved as a
    per-question failure (never a scientific exclusion).
    """
    canonical = validate_questions(questions)
    if not isinstance(payload, Mapping):
        raise LiveJevProtocolError("response is not a JSON object")
    answers = payload.get("answers")
    if not isinstance(answers, Mapping):
        raise LiveJevProtocolError("response.answers must be an object")
    if not answers:
        # A degenerate empty answer map is a structural failure, not three
        # independent per-question failures.
        raise LiveJevProtocolError("response.answers is empty")
    usage = payload.get("usage")
    if not isinstance(usage, Mapping):
        raise LiveJevProtocolError("response.usage must be an object")
    model = payload.get("model")
    if not isinstance(model, str) or not model:
        raise LiveJevProtocolError("response.model must be a non-empty string")
    input_tokens = usage.get("input_tokens")
    output_tokens = usage.get("output_tokens")
    for label, value in (("input_tokens", input_tokens), ("output_tokens", output_tokens)):
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise LiveJevProtocolError(f"response.usage.{label} must be a non-negative integer")
    cost = usage.get("cost")
    if cost is None:
        cost_usd: float | None = None
        cost_status = "unknown"
    else:
        if not _is_finite_number(cost) or float(cost) < 0:
            raise LiveJevProtocolError("response.usage.cost must be a non-negative number")
        cost_usd = float(cost)
        cost_status = "provider_reported"

    unknown = set(answers) - set(canonical)
    if unknown:
        raise LiveJevProtocolError(
            f"response.answers contains undeclared questions: {sorted(unknown)}"
        )

    normalized: dict = {}
    errors: dict = {}
    for name, question in canonical.items():
        if name not in answers:
            errors[name] = {"type": "error", "reason": "missing_answer"}
            continue
        answer = answers[name]
        if isinstance(answer, Mapping) and answer.get("type") == "error":
            reason = answer.get("reason")
            errors[name] = {
                "type": "error",
                "reason": reason if isinstance(reason, str) and reason else "provider_error",
                "message": redact(answer.get("message", "")) or None,
            }
            continue
        normalized[name] = _normalize_answer(name, question, answer)

    provider = payload.get("provider")
    return NormalizedDecisions(
        model=model,
        provider=provider if isinstance(provider, str) else None,
        response_id=payload.get("id") if isinstance(payload.get("id"), str) else None,
        usage_input_tokens=int(input_tokens),
        usage_output_tokens=int(output_tokens),
        cost_usd=cost_usd,
        cost_status=cost_status,
        answers=normalized,
        errors=errors,
    )


# --- Transport ------------------------------------------------------------


@dataclass
class TransportResponse:
    status: int
    body_text: str


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse redirects so the bearer token is never forwarded elsewhere."""

    def http_error_301(self, req, fp, code, msg, headers):  # noqa: D401
        raise urllib.error.HTTPError(req.full_url, code, "redirect refused", headers, fp)

    http_error_302 = http_error_303 = http_error_307 = http_error_308 = http_error_301


def post_decisions(
    body: bytes,
    api_key: str,
    *,
    endpoint: str = ENDPOINT,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> TransportResponse:
    """One POST attempt.  Never retries; raises on transport failure."""
    for header in ("authorization",):  # keep the key out of any accidental logging
        if header in body.decode("utf-8", "ignore").lower():  # pragma: no cover
            raise LiveJevValidationError("request body unexpectedly contains a credential field")
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
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(request, timeout=timeout) as response:
            return TransportResponse(int(response.status), response.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        try:
            text = exc.read().decode("utf-8", "replace")
        except Exception:
            text = ""
        return TransportResponse(int(exc.code), text)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise LiveJevTransportError(redact(f"{type(exc).__name__}: {exc}"))


# --- Credential loading ---------------------------------------------------


def load_api_key(env_file: str | None) -> tuple[str, str]:
    """Return (key, source).  Parses only ``OPENROUTER_API_KEY``.

    Reads the explicitly authorized file locally, never sources it, and never
    prints or returns anything else from it.
    """
    if env_file:
        try:
            with open(env_file, "r", encoding="utf-8") as handle:
                text = handle.read()
        except OSError:
            raise LiveJevCredentialError("could not read the authorized env file")
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, _, value = line.partition("=")
            if name.strip() != KEY_NAME:
                continue
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            if value:
                return value, "env-file"
        raise LiveJevCredentialError(f"{KEY_NAME} not found in the authorized env file")
    value = os.environ.get(KEY_NAME, "")
    if not value:
        raise LiveJevCredentialError(f"{KEY_NAME} is not set in the environment")
    return value, "environment"


# --- Orchestration --------------------------------------------------------


def build_dry_run_report(
    questions: Mapping[str, Any],
    state: Any,
    *,
    model: str = MODEL,
    endpoint: str = ENDPOINT,
    ceiling_usd: float | None = DEFAULT_CEILING_USD,
    server_cap: bool = True,
) -> dict:
    """Plan-only report.  Sends nothing; safe to run offline."""
    request = build_request(
        questions,
        state,
        model=model,
        max_request_cost_usd=ceiling_usd if server_cap else None,
    )
    body = serialize_request(request)
    bound = preflight_cost(len(body), ceiling_usd) if ceiling_usd else estimate_cost_bound(len(body))
    return {
        "status": "dry_run",
        "mode": "dry_run",
        "endpoint": endpoint,
        "model": model,
        "request": request,
        "request_bytes": len(body),
        "request_sha256": hashlib.sha256(body).hexdigest(),
        "cost_bound": bound,
        "ceiling_usd": ceiling_usd,
        "calls_attempted": 0,
        "calls_allowed": MAX_LIVE_CALLS,
        "cost_usd": None,
        "cost_status": "not_incurred",
        "reserve_retained": True,
        "notes": ["dry run: no network request was made"],
    }


_API_AMBIGUOUS_STATUSES = {408, 429, 500, 502, 503, 504, 524, 529}


def run_live(
    questions: Mapping[str, Any],
    state: Any,
    *,
    api_key: str,
    ceiling_usd: float,
    authorized: bool,
    model: str = MODEL,
    endpoint: str = ENDPOINT,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    max_calls: int = 1,
    server_cap: bool = True,
    transport: Callable[..., TransportResponse] = post_decisions,
) -> dict:
    """Execute the live fallback.  Raises only for pre-call refusals.

    Returns a report dict whose ``status`` is one of ``completed``,
    ``protocol_error``, ``api_error`` or ``transport_error``.  Once the request
    has been submitted, no outcome -- including a malformed 200 -- can raise out
    of this function, so a billed call is never reported as ``not_incurred``.
    """
    # The supplied credential must never travel to an arbitrary host.
    if endpoint != ENDPOINT:
        raise LiveJevEndpointError(
            f"live mode is restricted to the documented endpoint {ENDPOINT!r}; "
            f"refusing {endpoint!r}"
        )
    if not authorized:
        raise LiveJevAuthorizationError("live execution requires explicit --i-authorize-live")
    if max_calls < 1:
        raise LiveJevAuthorizationError("max_calls must be at least 1")
    if max_calls > MAX_LIVE_CALLS:
        raise LiveJevAuthorizationError(f"max_calls may not exceed {MAX_LIVE_CALLS}")
    if not api_key:
        raise LiveJevCredentialError(f"{KEY_NAME} is empty")

    request = build_request(
        questions,
        state,
        model=model,
        max_request_cost_usd=ceiling_usd if server_cap else None,
    )
    body = serialize_request(request)
    bound = preflight_cost(len(body), ceiling_usd)

    base = {
        "endpoint": endpoint,
        "model": model,
        "request": request,
        "request_bytes": len(body),
        "request_sha256": hashlib.sha256(body).hexdigest(),
        "cost_bound": bound,
        "ceiling_usd": ceiling_usd,
        "calls_allowed": max_calls,
        "authorized": True,
    }

    # Exactly one call, never retried.  Ambiguous billing fails closed.
    calls_attempted = 1
    try:
        response = transport(body, api_key, endpoint=endpoint, timeout=timeout)
    except LiveJevTransportError as exc:
        return {
            **base,
            "status": "transport_error",
            "calls_attempted": calls_attempted,
            "cost_usd": None,
            "cost_status": "unknown",
            "reserve_retained": True,
            "error": {"kind": "transport_error", "message": redact(exc)},
            "notes": [
                "transport failure: billing is ambiguous, no retry was attempted",
                "the allocated reserve is retained",
            ],
        }

    if response.status != 200:
        definitive = response.status not in _API_AMBIGUOUS_STATUSES
        code, message = _parse_api_error(response.body_text)
        return {
            **base,
            "status": "api_error",
            "calls_attempted": calls_attempted,
            "http_status": response.status,
            "cost_usd": 0.0 if definitive else None,
            "cost_status": "not_incurred" if definitive else "unknown",
            "reserve_retained": not definitive,
            "error": {"kind": "api_error", "code": code, "message": message},
            "notes": [
                "http " + str(response.status) + (": request rejected; not billed" if definitive
                                                   else ": ambiguous; billing may have occurred"),
                "no retry was attempted",
            ],
        }

    # --- Post-submission boundary -----------------------------------------
    # The request has been submitted, so every outcome is reported as a result.
    # Nothing below may raise into the pre-call "not_incurred" handler: a billed
    # call must never be silently zeroed.
    try:
        payload = json.loads(response.body_text)
    except ValueError:
        return _malformed_200_report(
            base, calls_attempted, "response body is not valid JSON", None
        )

    reported_cost = _extract_reported_cost(payload)
    try:
        normalized = validate_response(payload, questions)
        return {
            **base,
            "status": "completed",
            "calls_attempted": calls_attempted,
            "http_status": response.status,
            **normalized.to_report(),
            "cost_usd": normalized.cost_usd,
            "cost_status": normalized.cost_status,
            "reserve_retained": normalized.cost_usd is None,
            "notes": (
                ["provider reported cost"]
                if normalized.cost_usd is not None
                else ["provider did not report usage.cost: cost is explicit-unknown; reserve retained"]
            ),
        }
    except Exception as exc:  # noqa: BLE001 - MUST NOT escape as not_incurred
        return _malformed_200_report(
            base,
            calls_attempted,
            f"{type(exc).__name__}: {redact(exc)}",
            reported_cost,
        )


def _extract_reported_cost(payload: Any) -> float | None:
    """Independently validate a finite reported ``usage.cost``, else ``None``."""
    if not isinstance(payload, Mapping):
        return None
    usage = payload.get("usage")
    if not isinstance(usage, Mapping):
        return None
    cost = usage.get("cost")
    if _is_finite_number(cost) and float(cost) >= 0:
        return float(cost)
    return None


def _malformed_200_report(
    base: dict, calls_attempted: int, message: str, reported_cost: float | None
) -> dict:
    """A malformed 200 is a post-submission outcome, never ``not_incurred``.

    The reserve is retained and the cost is explicit-unknown unless the provider
    reported a finite, independently validated cost.
    """
    cost_known = reported_cost is not None
    return {
        **base,
        "status": "protocol_error",
        "calls_attempted": calls_attempted,
        "http_status": 200,
        "cost_usd": reported_cost,
        "cost_status": "provider_reported" if cost_known else "unknown",
        "reserve_retained": not cost_known,
        "error": {"kind": "protocol_error", "message": message},
        "notes": [
            "the request was submitted (http 200) and the response was malformed",
            "cost is provider-reported" if cost_known
            else "cost is explicit-unknown; the allocated reserve is retained",
        ],
    }


def _parse_api_error(body_text: str) -> tuple[int | None, str]:
    try:
        payload = json.loads(body_text)
    except json.JSONDecodeError:
        return None, redact(body_text) or "unparseable error body"
    if isinstance(payload, Mapping):
        error = payload.get("error")
        if isinstance(error, Mapping):
            code = error.get("code") if isinstance(error.get("code"), int) else None
            return code, redact(error.get("message", "")) or "no error message"
        if isinstance(error, str):
            return None, redact(error)
    return None, redact(body_text) or "unrecognized error body"


# --- CLI ------------------------------------------------------------------


def _write_report(report: dict, path: str | None) -> None:
    if path:
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(report, handle, indent=2, sort_keys=True)
            handle.write("\n")


def main(
    argv: list[str] | None = None,
    *,
    transport: Callable[..., TransportResponse] = post_decisions,
) -> int:
    parser = argparse.ArgumentParser(description="Direct OpenRouter Jev decision fallback (dry run by default).")
    parser.add_argument("--live", action="store_true", help="enable a live call (requires other flags)")
    parser.add_argument("--i-authorize-live", action="store_true", help="explicit live-call authorization")
    parser.add_argument("--max-cost-usd", type=float, default=DEFAULT_CEILING_USD,
                        help=f"hard ceiling in USD (default {DEFAULT_CEILING_USD})")
    parser.add_argument("--max-calls", type=int, default=1, help=f"1..{MAX_LIVE_CALLS} (default 1)")
    parser.add_argument("--env-file", default=None, help="file to read OPENROUTER_API_KEY from")
    parser.add_argument("--endpoint", default=ENDPOINT)
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--no-server-cap", action="store_true", help="omit provider.max_price")
    parser.add_argument("--report-out", default=None, help="write the JSON report here")
    args = parser.parse_args(argv)

    questions = default_questions()
    state = default_state()

    if not args.live:
        report = build_dry_run_report(
            questions, state, endpoint=args.endpoint, ceiling_usd=args.max_cost_usd,
            server_cap=not args.no_server_cap,
        )
        _write_report(report, args.report_out)
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0

    if not args.i_authorize_live:
        print(
            "refusing: --live requires --i-authorize-live (explicit authorization). "
            "Nothing was sent.",
            file=sys.stderr,
        )
        return 2

    try:
        api_key, source = load_api_key(args.env_file)
        report = run_live(
            questions, state,
            api_key=api_key, ceiling_usd=args.max_cost_usd, authorized=True,
            endpoint=args.endpoint, timeout=args.timeout, max_calls=args.max_calls,
            server_cap=not args.no_server_cap, transport=transport,
        )
        report["api_key_source"] = source
        report["api_key_loaded"] = True
    except LiveJevError as exc:
        report = {
            "status": "blocked",
            "mode": "live",
            "endpoint": args.endpoint,
            "model": MODEL,
            "ceiling_usd": args.max_cost_usd,
            "calls_attempted": 0,
            "calls_allowed": args.max_calls,
            "cost_usd": None,
            "cost_status": "not_incurred",
            "reserve_retained": True,
            "error": {"kind": type(exc).__name__, "message": redact(exc)},
            "notes": ["refused before any call"],
        }
        _write_report(report, args.report_out)
        print(json.dumps(report, indent=2, sort_keys=True))
        return 3

    _write_report(report, args.report_out)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["status"] == "completed" else 4


if __name__ == "__main__":
    raise SystemExit(main())
