"""Offline regression tests for the production Jev typed-decision adapter.

Covers ``src/review_article_agent/runtime/jev.py`` against the acceptance
criteria of task T3:

* valid predicate/choice/score primitives are normalized to canonical kinds;
* per-question missing/refused/error/malformed answers are retained with
  diagnostics while the other valid answers survive — never an exclusion;
* an invalid plan/endpoint/authorization makes **zero** dispatch calls;
* post-dispatch ambiguous failures keep the reserve ``unknown`` (never ``0``);
  a malformed 200 is never reported ``not_incurred``;
* provider cost is an exact decimal string (no float round-trip), including 0;
* the endpoint/redirect/proxy/key guards and exact-decimal bounds all fail
  closed;
* exactly one dispatch is attempted and nothing is ever retried.

No network, no credential, no paid call: the transport is always a local fixture.
Run:

    PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_jev_adapter.py'
"""

from __future__ import annotations

import dataclasses
import hashlib
import io
import json
import os
import sys
import unittest
import urllib.error
import urllib.request
from decimal import Decimal, localcontext
from pathlib import Path
from unittest import mock

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from review_article_agent.runtime import jev  # noqa: E402

API_KEY = "sk-or-v1-supersecret-should-never-appear"


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
def questions():
    return [
        jev.QuestionDef("is_original_report", jev.PREDICATE, "Is it original?"),
        jev.QuestionDef(
            "study_design",
            jev.CHOICE,
            "Which design?",
            options=("randomised", "observational", "other"),
        ),
        jev.QuestionDef(
            "confidence_level",
            jev.SCORE,
            "How confident?",
            levels=("low", "moderate", "high"),
        ),
    ]


STATE = {"fixture": "synthetic-spike-fixture-not-a-real-paper", "passage": "..."}


def valid_answers():
    return {
        "is_original_report": {"type": "noul", "noul": 0.97},
        "study_design": {
            "type": "choice",
            "choice": "randomised",
            "confidence": 0.81,
            "probabilities": {"randomised": 0.9, "observational": 0.1, "other": 0.0},
        },
        "confidence_level": {
            "type": "score",
            "score": 1.2,
            "confidence": 0.66,
            "probabilities": {"0": 0.1, "1": 0.6, "2": 0.3},
            "legend": {"0": "low", "1": "moderate", "2": "high"},
        },
    }


def valid_payload(**overrides):
    payload = {
        "id": "gen-dec-test",
        "model": "typesafe/jev-1.14-20261014",
        "provider": "TypeSafe",
        "answers": valid_answers(),
        "usage": {"input_tokens": 485, "output_tokens": 76, "cost": 0.00002037},
    }
    payload.update(overrides)
    return payload


class FakeTransport:
    """Records every call; returns a canned response or raises."""

    def __init__(self, response=None, raises=None):
        self.response = response
        self.raises = raises
        self.calls = []

    def __call__(self, body, api_key, *, endpoint, timeout):
        self.calls.append(
            {"body": body, "api_key": api_key, "endpoint": endpoint, "timeout": timeout}
        )
        if self.raises is not None:
            raise self.raises
        return self.response


def resp(status, obj):
    body = obj if isinstance(obj, str) else json.dumps(obj)
    return jev.TransportResponse(status, body)


def run(transport, *, qs=None, state=None, **kwargs):
    params = dict(
        api_key=API_KEY,
        reserve_usd="0.25",
        authorized=True,
        transport=transport,
    )
    params.update(kwargs)
    return jev.execute(qs or questions(), state if state is not None else STATE, **params)


# --------------------------------------------------------------------------- #
# Question definitions
# --------------------------------------------------------------------------- #
class QuestionDefTests(unittest.TestCase):
    def test_predicate_needs_no_options_or_levels(self):
        q = jev.QuestionDef("p", jev.PREDICATE, "i")
        self.assertEqual(q.wire_type(), "noul")

    def test_invalid_definitions_rejected(self):
        cases = [
            ("choice_requires_options", [
                lambda: jev.QuestionDef("c", jev.CHOICE, "i"),
                lambda: jev.QuestionDef("c", jev.CHOICE, "i", options=("a",), levels=("x",)),
            ]),
            ("score_requires_levels", [
                lambda: jev.QuestionDef("s", jev.SCORE, "i"),
            ]),
            ("duplicate_options", [
                lambda: jev.QuestionDef("c", jev.CHOICE, "i", options=("a", "a")),
            ]),
            ("unknown_kind", [
                lambda: jev.QuestionDef("k", "nope", "i"),
            ]),
        ]
        for label, builders in cases:
            with self.subTest(case=label):
                for build in builders:
                    with self.assertRaises(jev.JevValidationError):
                        build()

    def test_box_is_representable_but_unsupported(self):
        q = jev.QuestionDef("z", jev.BOX, "unsupported")
        with self.assertRaises(jev.JevValidationError):
            q.wire_type()

    def test_bad_question_id_rejected(self):
        for bad in ("", "has space", "../x", "a/b"):
            with self.assertRaises(jev.JevValidationError):
                jev.QuestionDef(bad, jev.PREDICATE, "i")

    def test_criteria_shape_enforced(self):
        with self.assertRaises(jev.JevValidationError):
            jev.QuestionDef(
                "c", jev.CHOICE, "i", options=("a", "b"), criteria={"a": "x"}
            )
        q = jev.QuestionDef(
            "c", jev.CHOICE, "i", options=("a", "b"), criteria={"a": "x", "b": "y"}
        )
        self.assertEqual(q.wire_criteria(), {"a": "x", "b": "y"})


# --------------------------------------------------------------------------- #
# Request building
# --------------------------------------------------------------------------- #
class BuildRequestTests(unittest.TestCase):
    def test_predicate_serializes_to_noul(self):
        request = jev.build_request(questions(), STATE)
        self.assertEqual(request["questions"]["is_original_report"]["type"], "noul")
        self.assertEqual(request["questions"]["study_design"]["type"], "choice")
        self.assertEqual(request["questions"]["confidence_level"]["type"], "score")
        self.assertEqual(request["model"], jev.MODEL)
        self.assertEqual(
            request["questions"]["study_design"]["criteria"],
            {"randomised": "randomised", "observational": "observational", "other": "other"},
        )
        self.assertEqual(
            request["questions"]["confidence_level"]["criteria"],
            ["low", "moderate", "high"],
        )

    def test_server_cap_optional_and_exact(self):
        with_cap = jev.build_request(questions(), STATE, max_request_cost_usd="0.25")
        without = jev.build_request(questions(), STATE)
        self.assertEqual(with_cap["provider"]["max_price"]["request"], "0.25")
        self.assertNotIn("provider", without)

    def test_box_question_omitted_from_wire(self):
        request = jev.build_request(
            [jev.QuestionDef("z", jev.BOX, "x"), questions()[0]], STATE
        )
        self.assertNotIn("z", request["questions"])
        self.assertIn("is_original_report", request["questions"])

    def test_only_box_questions_refused(self):
        with self.assertRaises(jev.JevValidationError):
            jev.build_request([jev.QuestionDef("z", jev.BOX, "x")], STATE)

    def test_non_json_state_refused(self):
        with self.assertRaises(jev.JevValidationError):
            jev.build_request(questions(), {"bad": float("nan")})


# --------------------------------------------------------------------------- #
# Planning: reserve before dispatch
# --------------------------------------------------------------------------- #
class PlanningTests(unittest.TestCase):
    def test_plan_returns_reserve_and_bound(self):
        plan = jev.plan_dispatch(questions(), STATE, reserve_usd="0.25")
        self.assertEqual(plan.reserve_usd, "0.25")
        expected = Decimal(plan.request_bytes) * Decimal("0.042") / Decimal(1_000_000)
        self.assertEqual(Decimal(plan.cost_upper_bound_usd), expected)
        self.assertLessEqual(Decimal(plan.cost_upper_bound_usd), Decimal("0.25"))
        self.assertEqual(plan.pricing_version, jev.PRICING_VERSION)
        self.assertEqual(plan.endpoint, jev.ENDPOINT)
        self.assertEqual(
            plan.request["provider"]["max_price"]["request"], plan.reserve_usd
        )
        self.assertEqual(len(plan.request_sha256), 64)

    def test_plan_is_deterministic(self):
        a = jev.plan_dispatch(questions(), STATE, reserve_usd="0.25")
        b = jev.plan_dispatch(questions(), STATE, reserve_usd="0.25")
        self.assertEqual(a.request_sha256, b.request_sha256)
        self.assertEqual(a.request_bytes, b.request_bytes)

    def test_reserve_must_be_positive(self):
        for bad in ("0", "-1", "0.0", 0, -0.5, "abc", float("inf"), True):
            with self.assertRaises(jev.JevValidationError):
                jev.plan_dispatch(questions(), STATE, reserve_usd=bad)

    def test_reserve_bounds_rejected(self):
        for bad in ("1e999999", "0e-999999", "1e25"):
            with self.assertRaises(jev.JevValidationError):
                jev.plan_dispatch(questions(), STATE, reserve_usd=bad)

    def test_over_budget_refused(self):
        with self.assertRaises(jev.JevBudgetError):
            jev.plan_dispatch(questions(), STATE, reserve_usd="1e-12")

    def test_oversize_request_refused(self):
        with self.assertRaises(jev.JevValidationError):
            jev.plan_dispatch(
                questions(), STATE, reserve_usd="0.25", max_request_bytes=10
            )

    def test_plan_restricted_to_documented_endpoint(self):
        with self.assertRaises(jev.JevEndpointError):
            jev.plan_dispatch(
                questions(), STATE, reserve_usd="0.25", endpoint="https://evil.example/x"
            )

    def test_no_server_cap_when_disabled(self):
        plan = jev.plan_dispatch(questions(), STATE, reserve_usd="0.25", server_cap=False)
        self.assertNotIn("provider", plan.request)
        self.assertIsNone(plan.server_cap_usd)

    def test_unsupported_question_ids_recorded(self):
        plan = jev.plan_dispatch(
            [questions()[0], jev.QuestionDef("z", jev.BOX, "x")],
            STATE,
            reserve_usd="0.25",
        )
        self.assertEqual(plan.unsupported_question_ids, ("z",))
        self.assertEqual(plan.supported_question_ids, ("is_original_report",))

    def test_cost_bound_helper_is_exact(self):
        bound = jev.estimate_cost_bound(1000)
        self.assertEqual(bound["cost_upper_bound_usd"], "0.000042")
        self.assertEqual(bound["input_tokens_upper_bound"], 1000)


# --------------------------------------------------------------------------- #
# Valid primitives
# --------------------------------------------------------------------------- #
class ValidPrimitivesTests(unittest.TestCase):
    def test_three_primitives_answered(self):
        transport = FakeTransport(resp(200, valid_payload()))
        result = run(transport)
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.calls_attempted, 1)
        self.assertEqual(set(result.outcomes), {
            "is_original_report", "study_design", "confidence_level"
        })
        self.assertTrue(all(o.outcome == "answered" for o in result.outcomes.values()))
        pred = result.outcomes["is_original_report"].answer
        self.assertEqual(pred.answer_kind, "predicate")
        self.assertEqual(pred.wire_type, "noul")
        self.assertEqual(pred.probability, Decimal("0.97"))
        choice = result.outcomes["study_design"].answer
        self.assertEqual(choice.choice, "randomised")
        self.assertEqual(choice.confidence, Decimal("0.81"))
        score = result.outcomes["confidence_level"].answer
        self.assertEqual(score.score, Decimal("1.2"))
        self.assertEqual(score.legend, {"0": "low", "1": "moderate", "2": "high"})

    def test_completed_cost_carries_reserve_and_bucket(self):
        result = run(FakeTransport(resp(200, valid_payload())))
        self.assertEqual(result.cost.reserve_usd, "0.25")
        self.assertEqual(result.reserve_usd, "0.25")
        self.assertEqual(result.cost.accounting_bucket, "direct_call")
        self.assertEqual(result.cost.route, "direct_call")

    def test_actual_model_provider_usage(self):
        result = run(FakeTransport(resp(200, valid_payload())))
        self.assertEqual(result.actual_model, "typesafe/jev-1.14-20261014")
        self.assertEqual(result.provider, "TypeSafe")
        self.assertEqual(result.response_id, "gen-dec-test")
        self.assertEqual(result.usage.input_tokens, 485)
        self.assertEqual(result.usage.output_tokens, 76)

    def test_raw_probability_not_thresholded(self):
        payload = valid_payload()
        payload["answers"]["is_original_report"] = {"type": "noul", "noul": 0.5}
        result = run(FakeTransport(resp(200, payload)))
        answer = result.outcomes["is_original_report"].answer
        self.assertEqual(answer.probability, Decimal("0.5"))
        self.assertNotIsInstance(answer.probability, bool)

    def test_raw_probability_zero_and_one_preserved(self):
        for value in (0, 1):
            payload = valid_payload()
            payload["answers"]["is_original_report"] = {"type": "noul", "noul": value}
            result = run(FakeTransport(resp(200, payload)))
            self.assertEqual(
                result.outcomes["is_original_report"].answer.probability,
                Decimal(value),
            )


# --------------------------------------------------------------------------- #
# Per-question failures (never exclusions)
# --------------------------------------------------------------------------- #
class PerQuestionFailureTests(unittest.TestCase):
    def _outcomes(self, payload):
        transport = FakeTransport(resp(200, payload))
        result = run(transport)
        return result, result.outcomes

    def test_missing_answer_is_per_question(self):
        payload = valid_payload()
        del payload["answers"]["study_design"]
        result, outcomes = self._outcomes(payload)
        self.assertEqual(result.status, "completed")
        self.assertEqual(outcomes["study_design"].outcome, "partial_error")
        self.assertEqual(
            outcomes["study_design"].per_question_error.reason, "missing_answer"
        )
        # the other answers survive
        self.assertEqual(outcomes["is_original_report"].outcome, "answered")
        self.assertEqual(outcomes["confidence_level"].outcome, "answered")

    def test_refused_answer_is_refusal(self):
        payload = valid_payload()
        payload["answers"]["study_design"] = {"type": "error", "reason": "refused"}
        _, outcomes = self._outcomes(payload)
        self.assertEqual(outcomes["study_design"].outcome, "refusal")
        self.assertIsNotNone(outcomes["study_design"].diagnostic)

    def test_provider_error_answer_retained(self):
        payload = valid_payload()
        payload["answers"]["study_design"] = {
            "type": "error",
            "reason": "provider_error",
            "message": "model busy",
        }
        _, outcomes = self._outcomes(payload)
        self.assertEqual(outcomes["study_design"].outcome, "partial_error")
        self.assertEqual(
            outcomes["study_design"].per_question_error.reason, "provider_error"
        )

    def test_choice_outside_options_is_malformed_not_protocol(self):
        payload = valid_payload()
        payload["answers"]["study_design"] = {"type": "choice", "choice": "meta"}
        result, outcomes = self._outcomes(payload)
        self.assertEqual(result.status, "completed")
        self.assertEqual(outcomes["study_design"].outcome, "malformed")
        self.assertEqual(
            outcomes["study_design"].per_question_error.reason, "malformed_answer"
        )
        # other answers survive
        self.assertEqual(outcomes["is_original_report"].outcome, "answered")

    def test_score_outside_levels_is_malformed(self):
        payload = valid_payload()
        payload["answers"]["confidence_level"] = {"type": "score", "score": 7.0}
        _, outcomes = self._outcomes(payload)
        self.assertEqual(outcomes["confidence_level"].outcome, "malformed")

    def test_malformed_answers(self):
        # Each former standalone malformed-answer test is preserved verbatim as
        # a labelled subTest case. (The docs-cited choice/score siblings
        # test_choice_outside_options_is_malformed_not_protocol and
        # test_score_outside_levels_is_malformed stay standalone under K1.)
        def case_test_type_mismatch_is_malformed():
            payload = valid_payload()
            payload["answers"]["study_design"] = {"type": "noul", "noul": 0.4}
            _, outcomes = self._outcomes(payload)
            self.assertEqual(outcomes["study_design"].outcome, "malformed")

        def case_test_non_object_answer_is_malformed():
            payload = valid_payload()
            payload["answers"]["study_design"] = "randomised"
            _, outcomes = self._outcomes(payload)
            self.assertEqual(outcomes["study_design"].outcome, "malformed")

        def case_test_out_of_range_probability_is_malformed():
            payload = valid_payload()
            payload["answers"]["is_original_report"] = {"type": "noul", "noul": 1.4}
            _, outcomes = self._outcomes(payload)
            self.assertEqual(outcomes["is_original_report"].outcome, "malformed")

        def case_test_bool_probability_is_malformed():
            payload = valid_payload()
            payload["answers"]["is_original_report"] = {"type": "noul", "noul": True}
            _, outcomes = self._outcomes(payload)
            self.assertEqual(outcomes["is_original_report"].outcome, "malformed")

        def case_test_undeclared_probability_key_is_malformed():
            payload = valid_payload()
            payload["answers"]["study_design"]["probabilities"] = {"randomised": 0.5, "x": 0.5}
            _, outcomes = self._outcomes(payload)
            self.assertEqual(outcomes["study_design"].outcome, "malformed")

        for label, case in (
            ("test_type_mismatch_is_malformed", case_test_type_mismatch_is_malformed),
            ("test_non_object_answer_is_malformed", case_test_non_object_answer_is_malformed),
            ("test_out_of_range_probability_is_malformed", case_test_out_of_range_probability_is_malformed),
            ("test_bool_probability_is_malformed", case_test_bool_probability_is_malformed),
            ("test_undeclared_probability_key_is_malformed", case_test_undeclared_probability_key_is_malformed),
        ):
            with self.subTest(case=label):
                case()

    def test_box_question_is_unsupported(self):
        qs = questions() + [jev.QuestionDef("z_box", jev.BOX, "unsupported")]
        payload = valid_payload()
        result, outcomes = self._outcomes_with(payload, qs)
        self.assertEqual(result.status, "completed")
        self.assertEqual(outcomes["z_box"].outcome, "unsupported")
        self.assertEqual(
            outcomes["z_box"].per_question_error.reason, "unsupported_question_kind"
        )

    def _outcomes_with(self, payload, qs):
        transport = FakeTransport(resp(200, payload))
        result = run(transport, qs=qs)
        return result, result.outcomes

    def test_error_answer_reason_and_message_never_echoed(self):
        payload = valid_payload()
        payload["answers"]["study_design"] = {
            "type": "error",
            "reason": "refused-PRIVATE_SOURCE_TEXT_CANARY",
            "message": "PRIVATE_SOURCE_TEXT_CANARY",
        }
        result, outcomes = self._outcomes(payload)
        self.assertEqual(outcomes["study_design"].outcome, "partial_error")
        self.assertIsNone(outcomes["study_design"].per_question_error.code)
        blob = json.dumps(
            {
                "detail": outcomes["study_design"].diagnostic.detail_redacted,
                "message": outcomes["study_design"].per_question_error.message_redacted,
            }
        )
        self.assertNotIn("PRIVATE_SOURCE_TEXT_CANARY", blob)
        self.assertNotIn("refused", blob)

    def test_refusal_is_static(self):
        payload = valid_payload()
        payload["answers"]["study_design"] = {"type": "error", "reason": "refused"}
        _, outcomes = self._outcomes(payload)
        self.assertEqual(outcomes["study_design"].outcome, "refusal")
        self.assertEqual(
            outcomes["study_design"].diagnostic.detail_redacted,
            "provider refused this question",
        )

    def test_never_an_exclusion(self):
        payload = valid_payload()
        payload["answers"]["study_design"] = {"type": "error", "reason": "refused"}
        del payload["answers"]["is_original_report"]
        payload["answers"]["confidence_level"] = {"type": "score", "score": 99}
        result, outcomes = self._outcomes(payload)
        for outcome in outcomes.values():
            self.assertFalse(outcome.is_exclusion())
        self.assertNotIn("exclusion", json.dumps(
            {k: o.outcome for k, o in outcomes.items()}
        ).lower())


# --------------------------------------------------------------------------- #
# Envelope protocol errors
# --------------------------------------------------------------------------- #
class EnvelopeProtocolTests(unittest.TestCase):
    def _run(self, payload):
        return run(FakeTransport(resp(200, payload)))

    def test_malformed_protocol_bodies(self):
        # Each former standalone envelope/protocol test is preserved verbatim as
        # a labelled subTest case.
        def case_test_non_object_payload():
            self.assertEqual(self._run(["not", "an", "object"]).status, "protocol_error")

        def case_test_answers_not_a_map():
            self.assertEqual(self._run({"model": "m", "answers": [], "usage": {}}).status, "protocol_error")

        def case_test_missing_answers_key():
            self.assertEqual(self._run({"model": "m", "usage": {"input_tokens": 1, "output_tokens": 1}}).status, "protocol_error")

        def case_test_missing_usage():
            payload = valid_payload()
            del payload["usage"]
            self.assertEqual(self._run(payload).status, "protocol_error")

        def case_test_bad_usage_tokens():
            payload = valid_payload()
            payload["usage"]["input_tokens"] = -3
            self.assertEqual(self._run(payload).status, "protocol_error")

        def case_test_undeclared_question_name():
            payload = valid_payload()
            payload["answers"]["unexpected"] = {"type": "noul", "noul": 0.5}
            self.assertEqual(self._run(payload).status, "protocol_error")

        def case_test_missing_model():
            payload = valid_payload()
            del payload["model"]
            self.assertEqual(self._run(payload).status, "protocol_error")

        def case_test_empty_answers_is_all_missing_not_protocol():
            payload = valid_payload()
            payload["answers"] = {}
            result = self._run(payload)
            self.assertEqual(result.status, "completed")
            self.assertTrue(all(o.outcome == "partial_error" for o in result.outcomes.values()))

        def case_test_protocol_error_never_not_incurred():
            payload = valid_payload()
            payload["answers"] = ["nope"]
            payload["usage"].pop("cost", None)
            result = self._run(payload)
            self.assertEqual(result.status, "protocol_error")
            self.assertEqual(result.calls_attempted, 1)
            self.assertNotEqual(result.cost.state, "not_incurred")
            self.assertEqual(result.cost.state, "unknown")
            self.assertTrue(result.cost.reserve_retained)

        def case_test_invalid_json_body():
            result = run(FakeTransport(resp(200, "<html>nope</html>")))
            self.assertEqual(result.status, "protocol_error")
            self.assertEqual(result.cost.state, "unknown")
            self.assertTrue(result.cost.reserve_retained)

        def case_test_non_finite_json_rejected():
            body = '{"model":"m","answers":{"q":{"type":"noul","noul":NaN}},"usage":{"input_tokens":1,"output_tokens":1}}'
            result = run(
                FakeTransport(resp(200, body)),
                qs=[jev.QuestionDef("q", jev.PREDICATE, "p")],
            )
            self.assertEqual(result.status, "protocol_error")

        for label, case in (
            ("test_non_object_payload", case_test_non_object_payload),
            ("test_answers_not_a_map", case_test_answers_not_a_map),
            ("test_missing_answers_key", case_test_missing_answers_key),
            ("test_missing_usage", case_test_missing_usage),
            ("test_bad_usage_tokens", case_test_bad_usage_tokens),
            ("test_undeclared_question_name", case_test_undeclared_question_name),
            ("test_missing_model", case_test_missing_model),
            ("test_empty_answers_is_all_missing_not_protocol", case_test_empty_answers_is_all_missing_not_protocol),
            ("test_protocol_error_never_not_incurred", case_test_protocol_error_never_not_incurred),
            ("test_invalid_json_body", case_test_invalid_json_body),
            ("test_non_finite_json_rejected", case_test_non_finite_json_rejected),
        ):
            with self.subTest(case=label):
                case()


# --------------------------------------------------------------------------- #
# Post-dispatch cost accounting
# --------------------------------------------------------------------------- #
class PostDispatchCostTests(unittest.TestCase):
    def test_post_dispatch_cost_matrix(self):
        # Each former standalone post-dispatch cost test is preserved verbatim
        # as a labelled subTest case.
        def case_test_ambiguous_500_retains_reserve_unknown():
            result = run(FakeTransport(resp(500, {"error": {"code": 500, "message": "boom"}})))
            self.assertEqual(result.status, "api_error")
            self.assertEqual(result.cost.state, "unknown")
            self.assertTrue(result.cost.reserve_retained)
            self.assertIsNone(result.cost.amount_usd)

        def case_test_ambiguous_429_retains_reserve_unknown():
            result = run(FakeTransport(resp(429, {"error": {"code": 429, "message": "slow"}})))
            self.assertEqual(result.cost.state, "unknown")
            self.assertTrue(result.cost.reserve_retained)

        def case_test_definitive_400_not_incurred():
            result = run(FakeTransport(resp(400, {"error": {"code": 400, "message": "bad"}})))
            self.assertEqual(result.status, "api_error")
            self.assertEqual(result.cost.state, "not_incurred")
            self.assertFalse(result.cost.reserve_retained)
            self.assertIsNone(result.cost.amount_usd)

        def case_test_transport_error_unknown_retained():
            transport = FakeTransport(raises=jev.JevTransportError("dropped"))
            result = run(transport)
            self.assertEqual(result.status, "transport_error")
            self.assertEqual(result.calls_attempted, 1)
            self.assertEqual(len(transport.calls), 1)
            self.assertEqual(result.cost.state, "unknown")
            self.assertTrue(result.cost.reserve_retained)

        def case_test_unexpected_transport_exception_still_unknown():
            transport = FakeTransport(raises=RuntimeError("kaboom"))
            result = run(transport)
            self.assertEqual(result.status, "transport_error")
            self.assertEqual(result.cost.state, "unknown")
            self.assertTrue(result.cost.reserve_retained)

        def case_test_malformed_200_with_valid_cost_is_known():
            payload = valid_payload()  # carries cost 0.00002037
            payload["answers"] = ["not", "a", "map"]
            result = run(FakeTransport(resp(200, payload)))
            self.assertEqual(result.status, "protocol_error")
            self.assertEqual(result.cost.state, "known")
            self.assertEqual(result.cost.amount_usd, "0.00002037")
            self.assertFalse(result.cost.reserve_retained)

        def case_test_malformed_200_without_cost_stays_unknown():
            payload = valid_payload()
            payload["answers"] = ["not", "a", "map"]
            payload["usage"].pop("cost", None)
            result = run(FakeTransport(resp(200, payload)))
            self.assertEqual(result.status, "protocol_error")
            self.assertEqual(result.cost.state, "unknown")
            self.assertTrue(result.cost.reserve_retained)

        def case_test_oversize_response_is_protocol_error():
            result = run(
                FakeTransport(jev.TransportResponse(200, "x" * 50, truncated=True))
            )
            self.assertEqual(result.status, "protocol_error")
            self.assertEqual(result.cost.state, "unknown")
            self.assertTrue(result.cost.reserve_retained)

        def case_test_completed_without_cost_is_unknown_retained():
            payload = valid_payload()
            payload["usage"] = {"input_tokens": 10, "output_tokens": 2}
            result = run(FakeTransport(resp(200, payload)))
            self.assertEqual(result.status, "completed")
            self.assertEqual(result.cost.state, "unknown")
            self.assertTrue(result.cost.reserve_retained)
            self.assertIsNone(result.cost.amount_usd)

        for label, case in (
            ("test_ambiguous_500_retains_reserve_unknown", case_test_ambiguous_500_retains_reserve_unknown),
            ("test_ambiguous_429_retains_reserve_unknown", case_test_ambiguous_429_retains_reserve_unknown),
            ("test_definitive_400_not_incurred", case_test_definitive_400_not_incurred),
            ("test_transport_error_unknown_retained", case_test_transport_error_unknown_retained),
            ("test_unexpected_transport_exception_still_unknown", case_test_unexpected_transport_exception_still_unknown),
            ("test_malformed_200_with_valid_cost_is_known", case_test_malformed_200_with_valid_cost_is_known),
            ("test_malformed_200_without_cost_stays_unknown", case_test_malformed_200_without_cost_stays_unknown),
            ("test_oversize_response_is_protocol_error", case_test_oversize_response_is_protocol_error),
            ("test_completed_without_cost_is_unknown_retained", case_test_completed_without_cost_is_unknown_retained),
        ):
            with self.subTest(case=label):
                case()


# --------------------------------------------------------------------------- #
# Exact decimal cost
# --------------------------------------------------------------------------- #
class ExactCostTests(unittest.TestCase):
    def _cost(self, raw):
        body = (
            '{"model":"m","answers":{"q":{"type":"noul","noul":0.5}},'
            '"usage":{"input_tokens":1,"output_tokens":1,"cost":%s}}' % raw
        )
        result = run(
            FakeTransport(resp(200, body)),
            qs=[jev.QuestionDef("q", jev.PREDICATE, "p")],
        )
        return result

    def test_cost_decimal_accepted_exactly(self):
        cases = [
            ("provider_precision", "0.00002037", "0.00002037"),
            # A zero cost is a known cost, never unknown, with the reserve released.
            ("zero_is_known", "0", "0"),
            ("small_precision", "0.000000016", "0.000000016"),
            # A value that would lose precision as an IEEE-754 float.
            ("no_float_roundtrip", "0.1", "0.1"),
        ]
        for label, raw, expected in cases:
            with self.subTest(case=label):
                result = self._cost(raw)
                self.assertEqual(result.cost.amount_usd, expected)
                self.assertIsInstance(result.cost.amount_usd, str)
                if label == "zero_is_known":
                    self.assertEqual(result.cost.state, "known")
                    self.assertEqual(Decimal(result.cost.amount_usd), Decimal(0))
                    self.assertFalse(result.cost.reserve_retained)

    def test_cost_decimal_rejected_protocol_error(self):
        cases = [
            ("invalid_string", '"0.01"'),
            ("bool", "true"),
            ("negative", "-1"),
            ("exponent_bounds", "1e999"),
        ]
        for label, raw in cases:
            with self.subTest(case=label):
                result = self._cost(raw)
                self.assertEqual(result.status, "protocol_error")
                if label == "exponent_bounds":
                    self.assertEqual(result.cost.state, "unknown")


# --------------------------------------------------------------------------- #
# Endpoint / redirect / proxy / key guards
# --------------------------------------------------------------------------- #
class GuardTests(unittest.TestCase):
    def test_execute_blocks_unsafe_request_with_zero_calls(self):
        cases = [
            ("foreign_endpoint", {"endpoint": "https://evil.example/collect"}),
            ("trailing_slash_endpoint", {"endpoint": jev.ENDPOINT + "/"}),
            ("unauthorized", {"authorized": False}),
            ("missing_key", {"api_key": ""}),
            ("invalid_plan", {"reserve_usd": "0"}),
            ("over_budget", {"reserve_usd": "1e-12"}),
        ]
        for label, kwargs in cases:
            with self.subTest(case=label):
                transport = FakeTransport(resp(200, valid_payload()))
                result = run(transport, **kwargs)
                self.assertEqual(result.status, "blocked")
                self.assertEqual(result.calls_attempted, 0)
                self.assertEqual(transport.calls, [])
                self.assertEqual(result.cost.state, "not_incurred")

    def test_execute_box_only_is_blocked_with_unsupported_outcome(self):
        transport = FakeTransport(resp(200, valid_payload()))
        result = run(transport, qs=[jev.QuestionDef("z", jev.BOX, "x")])
        self.assertEqual(result.status, "blocked")
        self.assertEqual(result.calls_attempted, 0)
        self.assertEqual(transport.calls, [])
        self.assertEqual(result.outcomes["z"].outcome, "unsupported")
        self.assertIsNotNone(result.outcomes["z"].diagnostic)
        self.assertIsNone(result.outcomes["z"].answer)

    def test_post_decisions_refuses_other_endpoint(self):
        with self.assertRaises(jev.JevEndpointError):
            jev.post_decisions(b"{}", API_KEY, endpoint="https://evil.example/x")

    def test_post_decisions_requires_key(self):
        with self.assertRaises(jev.JevCredentialError):
            jev.post_decisions(b"{}", "", endpoint=jev.ENDPOINT)

    def test_redirect_is_refused(self):
        opener = jev._http_opener()
        handler = next(h for h in opener.handlers if isinstance(h, jev._NoRedirect))

        class _Req:
            full_url = jev.ENDPOINT

        with self.assertRaises(urllib.error.HTTPError) as ctx:
            handler.http_error_302(_Req(), io.BytesIO(b""), 302, "Found", {})
        self.assertEqual(ctx.exception.code, 302)
        ctx.exception.close()

    def test_proxy_handler_suppressed_even_when_environment_configured(self):
        with mock.patch.dict(
            os.environ,
            {"HTTP_PROXY": "http://evil:8080", "HTTPS_PROXY": "http://evil:8080"},
        ):
            with mock.patch.object(
                urllib.request, "getproxies", return_value={"http": "http://evil:8080"}
            ):
                opener = jev._http_opener()
        self.assertFalse(
            any(isinstance(h, urllib.request.ProxyHandler) for h in opener.handlers)
        )
        self.assertTrue(any(isinstance(h, jev._NoRedirect) for h in opener.handlers))

    def test_key_never_in_body_or_result(self):
        transport = FakeTransport(resp(200, valid_payload()))
        result = run(transport)
        sent = transport.calls[0]["body"].decode("utf-8")
        self.assertNotIn(API_KEY, sent)
        self.assertNotIn(API_KEY, json.dumps(result.__dict__, default=str))

    def test_provider_error_text_never_echoed_in_diagnostic(self):
        body = json.dumps(
            {
                "error": {
                    "code": 400,
                    "message": f"bad key {API_KEY} from sk-or-v1-leak SOURCE_CANARY",
                }
            }
        )
        result = run(FakeTransport(resp(400, body)))
        detail = result.diagnostic.detail_redacted
        self.assertNotIn(API_KEY, detail)
        self.assertNotIn("sk-or-", detail)
        self.assertNotIn("SOURCE_CANARY", detail)
        self.assertIn("http 400", detail)

    def test_redact_peer_text(self):
        out = jev.redact_peer_text("failed sk-or-v1-abc123 with Bearer sk-or-v1-xyz\nnext")
        self.assertNotIn("sk-or-", out)
        self.assertNotIn("\n", out)

    def test_transport_error_message_is_static(self):
        transport = FakeTransport(raises=jev.JevTransportError("inner"))
        result = run(transport)
        self.assertNotIn("inner", result.diagnostic.detail_redacted)


# --------------------------------------------------------------------------- #
# No blind retry
# --------------------------------------------------------------------------- #
class NoRetryTests(unittest.TestCase):
    def test_single_dispatch_constant(self):
        self.assertEqual(jev.MAX_DISPATCH_ATTEMPTS, 1)

    def test_every_outcome_dispatches_once(self):
        cases = [
            ("completed", resp(200, valid_payload()), None),
            ("transport_error", None, jev.JevTransportError("dropped")),
            ("api_error", resp(503, {"error": {"code": 503, "message": "x"}}), None),
        ]
        for label, response, raises in cases:
            with self.subTest(case=label):
                transport = FakeTransport(response=response, raises=raises)
                result = run(transport)
                self.assertEqual(len(transport.calls), 1)
                if label == "completed":
                    self.assertEqual(result.calls_attempted, 1)
                    self.assertTrue(result.no_automatic_retry)
                    self.assertFalse(result.idempotent_admission_proven)

    def test_attempt_no_echoed(self):
        transport = FakeTransport(resp(200, valid_payload()))
        result = run(transport, attempt_no=3)
        self.assertEqual(result.attempt_no, 3)
        self.assertFalse(result.idempotent_admission_proven)


# --------------------------------------------------------------------------- #
# CostRecord invariants
# --------------------------------------------------------------------------- #
class CostRecordTests(unittest.TestCase):
    def test_unknown_requires_no_amount(self):
        record = jev.CostRecord(state="unknown", reserve_usd="0.25", reserve_retained=True)
        self.assertIsNone(record.amount_usd)
        self.assertEqual(record.route, "direct_call")
        self.assertEqual(record.accounting_bucket, "direct_call")

    def test_invalid_cost_records_rejected(self):
        cases = [
            ("known_requires_amount", {"state": "known"}),
            ("not_incurred_rejects_amount", {"state": "not_incurred", "amount_usd": "0.1"}),
            ("unknown_rejects_zero_amount", {"state": "unknown", "amount_usd": "0"}),
            ("bad_state", {"state": "bogus"}),
            ("bucket_mismatch", {"state": "unknown", "accounting_bucket": "session_statistics"}),
        ]
        for label, kwargs in cases:
            with self.subTest(case=label):
                with self.assertRaises(jev.JevValidationError):
                    jev.CostRecord(**kwargs)


# --------------------------------------------------------------------------- #
# Installed-package shape
# --------------------------------------------------------------------------- #
class InstalledPackageTests(unittest.TestCase):
    def test_module_is_a_package_submodule(self):
        self.assertEqual(jev.__name__, "review_article_agent.runtime.jev")

    def test_no_spike_dependency(self):
        self.assertFalse(hasattr(jev, "live_jev"))
        self.assertFalse(hasattr(jev, "default_questions"))
        self.assertFalse(hasattr(jev, "main"))


# --------------------------------------------------------------------------- #
# Focused repairs (T3-R1 reproductions)
# --------------------------------------------------------------------------- #
NESTED_BODY = "[" * 200_000 + "]" * 200_000


class RepairModelPlanTests(unittest.TestCase):
    def test_plan_and_execute_reject_unrelated_model_and_timeout(self):
        # Each former standalone model/timeout repair test is preserved verbatim
        # as a labelled subTest case.
        def case_test_plan_rejects_unrelated_model():
            with self.assertRaises(jev.JevValidationError):
                jev.plan_dispatch(
                    questions(), STATE, reserve_usd="0.25", model="openai/expensive-unrelated"
                )

        def case_test_build_request_rejects_unrelated_model():
            with self.assertRaises(jev.JevValidationError):
                jev.build_request(questions(), STATE, model="openai/expensive-unrelated")

        def case_test_execute_blocks_unrelated_model_zero_calls():
            transport = FakeTransport(resp(200, valid_payload()))
            result = run(transport, model="openai/expensive-unrelated")
            self.assertEqual(result.status, "blocked")
            self.assertEqual(result.calls_attempted, 0)
            self.assertEqual(transport.calls, [])

        def case_test_plan_rejects_unbounded_timeout():
            for bad in (float("nan"), float("inf"), float("-inf"), 0, -1, 1e9, True):
                with self.assertRaises(jev.JevValidationError):
                    jev.plan_dispatch(questions(), STATE, reserve_usd="0.25", timeout=bad)

        def case_test_execute_blocks_bad_timeout_zero_calls():
            transport = FakeTransport(resp(200, valid_payload()))
            result = run(transport, timeout=float("nan"))
            self.assertEqual(result.status, "blocked")
            self.assertEqual(transport.calls, [])

        for label, case in (
            ("test_plan_rejects_unrelated_model", case_test_plan_rejects_unrelated_model),
            ("test_build_request_rejects_unrelated_model", case_test_build_request_rejects_unrelated_model),
            ("test_execute_blocks_unrelated_model_zero_calls", case_test_execute_blocks_unrelated_model_zero_calls),
            ("test_plan_rejects_unbounded_timeout", case_test_plan_rejects_unbounded_timeout),
            ("test_execute_blocks_bad_timeout_zero_calls", case_test_execute_blocks_bad_timeout_zero_calls),
        ):
            with self.subTest(case=label):
                case()


class PlanImmutabilityTests(unittest.TestCase):
    def test_state_mutation_after_planning_does_not_change_body(self):
        state = {"text": "ORIGINAL"}
        plan = jev.plan_dispatch(questions(), state, reserve_usd="0.25")
        state["text"] = "MUTATED AFTER RESERVATION"
        body = json.loads(plan.request_body)
        self.assertEqual(body["state"]["text"], "ORIGINAL")
        # the frozen plan copy is unchanged too
        self.assertEqual(plan.request["state"]["text"], "ORIGINAL")

    def test_plan_request_is_deep_frozen(self):
        plan = jev.plan_dispatch(questions(), STATE, reserve_usd="0.25")
        with self.assertRaises(TypeError):
            plan.request["state"] = {"text": "x"}
        with self.assertRaises(TypeError):
            plan.request["state"]["text"] = "x"
        with self.assertRaises(TypeError):
            plan.request["questions"]["q"] = {}

    def test_dispatched_bytes_match_returned_hash_and_size(self):
        plan = jev.plan_dispatch(questions(), STATE, reserve_usd="0.25")
        transport = FakeTransport(resp(200, valid_payload()))
        result = jev.dispatch_plan(plan, api_key=API_KEY, transport=transport)
        sent = transport.calls[0]["body"]
        self.assertEqual(sent, plan.request_body)
        self.assertEqual(len(sent), result.request_bytes)
        self.assertEqual(hashlib.sha256(sent).hexdigest(), result.request_sha256)

    def test_dispatch_refuses_alias_mutated_request(self):
        plan = jev.plan_dispatch(questions(), STATE, reserve_usd="0.25")
        # Bypass the freeze by swapping the attribute outright (frozen dataclass).
        object.__setattr__(plan, "request", {"model": jev.MODEL, "state": {"t": 1}, "questions": {}})
        transport = FakeTransport(resp(200, valid_payload()))
        with self.assertRaises(jev.JevValidationError):
            jev.dispatch_plan(plan, api_key=API_KEY, transport=transport)
        self.assertEqual(transport.calls, [])


class ForgedPlanTests(unittest.TestCase):
    def _valid_plan(self):
        return jev.plan_dispatch(questions(), STATE, reserve_usd="0.25")

    def test_forged_plan_refused(self):
        cases = [
            ("model", {"model": "openai/expensive-unrelated"}, jev.JevValidationError),
            ("bound", {"cost_upper_bound_usd": "0"}, jev.JevValidationError),
            ("reserve", {"reserve_usd": "0"}, jev.JevValidationError),
            ("size", None, jev.JevValidationError),  # request_bytes + 1
            ("digest", {"request_sha256": "00" * 32}, jev.JevValidationError),
            ("pricing", {"pricing_version": "nope"}, jev.JevValidationError),
            ("endpoint", {"endpoint": "https://evil.example/x"}, jev.JevError),
            ("non_decimal_bound", {"cost_upper_bound_usd": "abc"}, jev.JevValidationError),
            ("empty_questions", {"questions": ()}, jev.JevValidationError),
            ("non_mapping_request", {"request": []}, jev.JevValidationError),
            ("non_int_size", {"request_bytes": "big"}, jev.JevValidationError),
        ]
        for label, overrides, exc in cases:
            with self.subTest(case=label):
                base = self._valid_plan()
                if label == "size":
                    forged = dataclasses.replace(base, request_bytes=base.request_bytes + 1)
                else:
                    forged = dataclasses.replace(base, **overrides)
                with self.assertRaises(exc):
                    jev.dispatch_plan(forged, api_key=API_KEY, transport=FakeTransport())


class StatusClassificationTests(unittest.TestCase):
    CANARY = "PRIVATE_SOURCE_TEXT_CANARY"

    def _body(self):
        return json.dumps({"error": {"code": 999, "message": self.CANARY}})

    def test_only_documented_4xx_are_not_incurred(self):
        for status in (400, 401, 402, 403, 404, 413):
            result = run(FakeTransport(jev.TransportResponse(status, self._body())))
            self.assertEqual(result.cost.state, "not_incurred", status)
            self.assertFalse(result.cost.reserve_retained)

    def test_all_other_non_200_are_ambiguous_unknown(self):
        for status in (429, 500, 501, 502, 503, 504, 507, 524, 529, 302, 301, 201, 204):
            result = run(FakeTransport(jev.TransportResponse(status, self._body())))
            self.assertEqual(result.cost.state, "unknown", status)
            self.assertTrue(result.cost.reserve_retained, status)
            self.assertIsNone(result.cost.amount_usd, status)

    def test_ambiguous_reply_never_echoes_the_canary(self):
        for status in (501, 507, 302, 201):
            result = run(FakeTransport(jev.TransportResponse(status, self._body())))
            blob = json.dumps(
                {
                    "detail": result.diagnostic.detail_redacted,
                    "code": result.diagnostic.code,
                    "outcomes": {k: o.outcome for k, o in result.outcomes.items()},
                }
            )
            self.assertNotIn(self.CANARY, blob)
            self.assertIn(str(status), result.diagnostic.detail_redacted)


class StrictValidationRepairTests(unittest.TestCase):
    def _run(self, payload):
        return run(FakeTransport(resp(200, payload)))

    def test_score_bounds_matrix(self):
        # Each former standalone score-bound test is preserved verbatim as a
        # labelled subTest case.
        def case_test_score_just_outside_range_is_malformed():
            payload = valid_payload()
            payload["answers"]["confidence_level"] = {"type": "score", "score": -0.0000001}
            result = self._run(payload)
            self.assertEqual(result.outcomes["confidence_level"].outcome, "malformed")

        def case_test_score_just_above_max_is_malformed():
            payload = valid_payload()
            payload["answers"]["confidence_level"] = {"type": "score", "score": 2.0000001}
            result = self._run(payload)
            self.assertEqual(result.outcomes["confidence_level"].outcome, "malformed")

        def case_test_score_at_bounds_is_answered():
            for value in (0, 2):
                payload = valid_payload()
                payload["answers"]["confidence_level"] = {"type": "score", "score": value}
                result = self._run(payload)
                self.assertEqual(result.outcomes["confidence_level"].outcome, "answered")

        for label, case in (
            ("test_score_just_outside_range_is_malformed", case_test_score_just_outside_range_is_malformed),
            ("test_score_just_above_max_is_malformed", case_test_score_just_above_max_is_malformed),
            ("test_score_at_bounds_is_answered", case_test_score_at_bounds_is_answered),
        ):
            with self.subTest(case=label):
                case()

    def test_legend_wrong_value_type_is_malformed_not_coerced(self):
        payload = valid_payload()
        payload["answers"]["confidence_level"]["legend"] = {"0": 42}
        result = self._run(payload)
        self.assertEqual(result.outcomes["confidence_level"].outcome, "malformed")
        self.assertIsNone(result.outcomes["confidence_level"].answer)

    def test_duplicate_json_keys_rejected(self):
        body = (
            '{"model":"%s","model":"%s","answers":{"q":{"type":"noul","noul":0.5}},'
            '"usage":{"input_tokens":1,"output_tokens":1}}' % (jev.MODEL, jev.MODEL)
        )
        result = run(
            FakeTransport(resp(200, body)),
            qs=[jev.QuestionDef("q", jev.PREDICATE, "p")],
        )
        self.assertEqual(result.status, "protocol_error")
        self.assertEqual(result.cost.state, "unknown")
        self.assertTrue(result.cost.reserve_retained)

    def test_wrong_response_model_is_protocol_error(self):
        payload = valid_payload(model="openai/gpt-4o")
        result = self._run(payload)
        self.assertEqual(result.status, "protocol_error")

    def test_dated_response_snapshot_accepted_and_recorded(self):
        payload = valid_payload(model="typesafe/jev-1.14-20261014")
        result = self._run(payload)
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.actual_model, "typesafe/jev-1.14-20261014")


class PostSubmissionNoEscapeTests(unittest.TestCase):
    def test_deeply_nested_body_does_not_escape(self):
        result = run(FakeTransport(resp(200, NESTED_BODY)))  # must not raise
        self.assertEqual(result.status, "protocol_error")
        self.assertTrue(result.submitted)
        self.assertEqual(result.cost.state, "unknown")
        self.assertTrue(result.cost.reserve_retained)

    def test_malformed_transport_response_is_unknown(self):
        for bad in ({"status": 200, "body_text": "{}"}, "nope", object()):
            result = run(FakeTransport(bad))
            self.assertEqual(result.status, "protocol_error")
            self.assertTrue(result.submitted)
            self.assertEqual(result.cost.state, "unknown")
            self.assertTrue(result.cost.reserve_retained)

    def test_non_int_status_is_unknown(self):
        result = run(FakeTransport(jev.TransportResponse("200", "{}")))
        self.assertEqual(result.status, "protocol_error")
        self.assertEqual(result.cost.state, "unknown")
        self.assertTrue(result.cost.reserve_retained)

    def test_unexpected_validation_exception_does_not_escape(self):
        payload = valid_payload()
        # An undeclared question name is an envelope defect -> protocol_error.
        payload["answers"]["boom"] = {"type": "noul", "noul": 0.5}
        result = run(FakeTransport(resp(200, payload)))
        self.assertEqual(result.status, "protocol_error")
        self.assertTrue(result.submitted)
        # The body carried a valid reported cost, which is independently honoured.
        self.assertEqual(result.cost.state, "known")
        self.assertEqual(result.cost.amount_usd, "0.00002037")

    def test_malformed_envelope_without_cost_stays_unknown(self):
        payload = valid_payload()
        payload["answers"]["boom"] = {"type": "noul", "noul": 0.5}
        payload["usage"].pop("cost", None)
        result = run(FakeTransport(resp(200, payload)))
        self.assertEqual(result.status, "protocol_error")
        self.assertEqual(result.cost.state, "unknown")
        self.assertTrue(result.cost.reserve_retained)


class MoneyMathTests(unittest.TestCase):
    def test_estimate_is_independent_of_ambient_precision(self):
        with localcontext() as ctx:
            ctx.prec = 2
            bound = jev.estimate_cost_bound(12345)
        self.assertEqual(bound["cost_upper_bound_usd"], "0.00051849")
        self.assertEqual(
            Decimal(bound["cost_upper_bound_usd"]),
            Decimal(12345) * Decimal("0.042") / Decimal(1_000_000),
        )

    def test_plan_bound_independent_of_ambient_precision(self):
        with localcontext() as ctx:
            ctx.prec = 2
            plan = jev.plan_dispatch(questions(), STATE, reserve_usd="0.25")
        expected = (
            Decimal(plan.request_bytes) * Decimal("0.042") / Decimal(1_000_000)
        )
        self.assertEqual(Decimal(plan.cost_upper_bound_usd), expected)

    def test_pricing_override_validated(self):
        for bad in (0.042, "0", "-1", "1e999999", True, "abc"):
            with self.assertRaises(jev.JevValidationError):
                jev.estimate_cost_bound(1000, input_usd_per_million=bad)


class DispatchBoundRepairTests(unittest.TestCase):
    def test_timeout_override_bounded_at_dispatch(self):
        plan = jev.plan_dispatch(questions(), STATE, reserve_usd="0.25")
        transport = FakeTransport(resp(200, valid_payload()))
        with self.assertRaises(jev.JevValidationError):
            jev.dispatch_plan(plan, api_key=API_KEY, timeout=float("inf"), transport=transport)
        self.assertEqual(transport.calls, [])

    def test_attempt_no_bounds(self):
        plan = jev.plan_dispatch(questions(), STATE, reserve_usd="0.25")
        for bad in (0, -1, 10 ** 9, 1.5, True):
            with self.assertRaises(jev.JevValidationError):
                jev.dispatch_plan(plan, api_key=API_KEY, attempt_no=bad, transport=FakeTransport())
        transport = FakeTransport(resp(200, valid_payload()))
        result = run(transport, attempt_no=2)
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.attempt_no, 2)

    def test_server_cap_disabled_still_bounded(self):
        plan = jev.plan_dispatch(questions(), STATE, reserve_usd="0.25", server_cap=False)
        self.assertNotIn("provider", plan.request)
        self.assertIsNotNone(plan.cost_upper_bound_usd)
        self.assertLessEqual(
            Decimal(plan.cost_upper_bound_usd), Decimal(plan.reserve_usd)
        )
        # still dispatchable
        result = jev.dispatch_plan(plan, api_key=API_KEY, transport=FakeTransport(resp(200, valid_payload())))
        self.assertEqual(result.status, "completed")


class CostRecordInvariantRepairTests(unittest.TestCase):
    def test_cost_record_invariant_violations_rejected(self):
        cases = [
            ("amount_must_be_string", [{"state": "known", "amount_usd": 0.001}]),
            ("amount_rejects_negative", [{"state": "known", "amount_usd": "-1"}]),
            ("currency_must_be_usd", [{"state": "unknown", "currency": "EUR"}]),
            ("reserve_retained_must_match_state", [
                {"state": "unknown", "reserve_usd": "0.25", "reserve_retained": False},
                {"state": "known", "amount_usd": "0.1", "reserve_retained": True},
            ]),
            ("reserve_must_be_positive_decimal", [
                {"state": "unknown", "reserve_usd": "0", "reserve_retained": True},
                {"state": "unknown", "reserve_usd": 0.25, "reserve_retained": True},
            ]),
        ]
        for label, kwargs_list in cases:
            with self.subTest(case=label):
                for kwargs in kwargs_list:
                    with self.assertRaises(jev.JevValidationError):
                        jev.CostRecord(**kwargs)


# --------------------------------------------------------------------------- #
# Focused repairs (T3-R2 reproductions)
# --------------------------------------------------------------------------- #
def _forged_consistent_plan(base, request_dict):
    """A plan whose request/body/size/digest are self-consistent but forged."""
    frozen = jev._deep_freeze(request_dict)
    body = jev.canonical_json(frozen)
    return dataclasses.replace(
        base,
        request=frozen,
        request_body=body,
        request_bytes=len(body),
        request_sha256=hashlib.sha256(body).hexdigest(),
        cost_upper_bound_usd=jev.estimate_cost_bound(len(body))["cost_upper_bound_usd"],
    )


class R2QuestionBindingTests(unittest.TestCase):
    def _choice_plan(self):
        q = jev.QuestionDef("q", jev.CHOICE, "choose", options=("A", "B"))
        return jev.plan_dispatch([q], "synthetic", reserve_usd="0.01")

    def test_wire_options_match_declared(self):
        plan = self._choice_plan()
        wire = json.loads(plan.request_body)["questions"]["q"]
        self.assertEqual(list(wire["criteria"]), ["A", "B"])

    def test_forged_questions_refused_before_transport(self):
        plan = self._choice_plan()
        forged = dataclasses.replace(
            plan,
            questions=(
                jev.QuestionDef("q", jev.CHOICE, "DIFFERENT CRITERIA", options=("FORGED", "B")),
            ),
        )
        calls = []

        def transport(body, key, *, endpoint, timeout):
            calls.append(body)
            return jev.TransportResponse(
                200,
                json.dumps(
                    {
                        "model": "typesafe/jev-1.14-20261014",
                        "answers": {"q": {"type": "choice", "choice": "FORGED"}},
                        "usage": {"input_tokens": 1, "output_tokens": 1},
                    }
                ),
            )

        with self.assertRaises(jev.JevValidationError):
            jev.dispatch_plan(forged, api_key="synthetic", transport=transport)
        self.assertEqual(calls, [])  # nothing was sent

    def test_forged_instruction_only_refused(self):
        plan = self._choice_plan()
        forged = dataclasses.replace(
            plan,
            questions=(jev.QuestionDef("q", jev.CHOICE, "changed", options=("A", "B")),),
        )
        with self.assertRaises(jev.JevValidationError):
            jev.dispatch_plan(forged, api_key="synthetic", transport=FakeTransport())

    def test_forged_supported_ids_refused(self):
        plan = self._choice_plan()
        forged = dataclasses.replace(plan, supported_question_ids=("other",))
        with self.assertRaises(jev.JevValidationError):
            jev.dispatch_plan(forged, api_key="synthetic", transport=FakeTransport())

    def test_criteria_alias_cannot_drift(self):
        criteria = {"A": "aa", "B": "bb"}
        q = jev.QuestionDef("q", jev.CHOICE, "choose", options=("A", "B"), criteria=criteria)
        criteria["A"] = "MUTATED"
        plan = jev.plan_dispatch([q], "synthetic", reserve_usd="0.01")
        wire = json.loads(plan.request_body)["questions"]["q"]
        self.assertEqual(wire["criteria"], {"A": "aa", "B": "bb"})
        with self.assertRaises(TypeError):
            q.criteria["A"] = "x"

    def test_non_portable_criteria_refused(self):
        with self.assertRaises(jev.JevValidationError):
            jev.QuestionDef(
                "q", jev.CHOICE, "c", options=("A", "B"),
                criteria={"A": float("nan"), "B": "b"},
            )

    def test_forged_extra_top_level_key_refused(self):
        plan = self._choice_plan()
        request = {**dict(plan.request), "tools": [{"type": "x"}]}
        forged = _forged_consistent_plan(plan, request)
        with self.assertRaises(jev.JevValidationError):
            jev.dispatch_plan(forged, api_key="synthetic", transport=FakeTransport())

    def test_forged_provider_extra_pricing_refused(self):
        plan = self._choice_plan()
        request = dict(plan.request)
        request["provider"] = {"max_price": {"request": plan.reserve_usd, "prompt": "1"}}
        forged = _forged_consistent_plan(plan, request)
        with self.assertRaises(jev.JevValidationError):
            jev.dispatch_plan(forged, api_key="synthetic", transport=FakeTransport())

    def test_state_type_enforced(self):
        q = [jev.QuestionDef("q", jev.PREDICATE, "i")]
        for bad in (123, True, 1.5):
            with self.assertRaises(jev.JevValidationError):
                jev.plan_dispatch(q, bad, reserve_usd="0.25")
            with self.assertRaises(jev.JevValidationError):
                jev.build_request(q, bad)
        # documented state types are accepted
        jev.plan_dispatch(q, "a string", reserve_usd="0.25")
        jev.plan_dispatch(q, [1, 2], reserve_usd="0.25")


class R2ResponseModelTests(unittest.TestCase):
    def _status(self, model):
        payload = valid_payload(model=model)
        return run(FakeTransport(resp(200, payload))).status

    def test_response_model_matrix(self):
        # Each former standalone response-model test is preserved verbatim as a
        # labelled subTest case.
        def case_test_real_dated_snapshot_accepted():
            self.assertEqual(self._status("typesafe/jev-1.14-20261014"), "completed")
            self.assertEqual(self._status(jev.MODEL), "completed")

        def case_test_arbitrary_suffix_rejected():
            for bad in (
                "typesafe/jev-1.14-evil",
                "typesafe/jev-1.14-2026",
                "typesafe/jev-1.14-2026101",
                "typesafe/jev-1.14-20261014-extra",
                "typesafe/jev-1.14-evil-20261014",
                "openai/gpt-4o",
            ):
                self.assertEqual(self._status(bad), "protocol_error", bad)

        def case_test_overlong_model_rejected():
            self.assertEqual(self._status("typesafe/jev-1.14-" + "9" * 300), "protocol_error")

        for label, case in (
            ("test_real_dated_snapshot_accepted", case_test_real_dated_snapshot_accepted),
            ("test_arbitrary_suffix_rejected", case_test_arbitrary_suffix_rejected),
            ("test_overlong_model_rejected", case_test_overlong_model_rejected),
        ):
            with self.subTest(case=label):
                case()


class R2MetadataAndDepthTests(unittest.TestCase):
    def test_metadata_and_depth_bounds(self):
        # Each former standalone metadata/depth test is preserved verbatim as a
        # labelled subTest case. (test_deep_body_is_protocol_error_not_escape is
        # kept standalone under K2.)
        def case_test_provider_and_id_bounded():
            for payload in (
                valid_payload(provider="P" * 300),
                valid_payload(id="x" * 300),
                valid_payload(provider=123),
                valid_payload(id={"a": 1}),
            ):
                result = run(FakeTransport(resp(200, payload)))
                self.assertEqual(result.status, "protocol_error")

        def case_test_json_depth_bound():
            # modest nesting parses; a document deeper than 64 is refused.
            jev._parse_json("[" * 32 + "]" * 32)
            with self.assertRaises(jev.JevProtocolError):
                jev._parse_json("[" * 200 + "]" * 200)

        def case_test_response_body_over_byte_bound():
            big = "x" * (jev.MAX_RESPONSE_BYTES + 1)
            result = run(FakeTransport(jev.TransportResponse(200, big)))
            self.assertEqual(result.status, "protocol_error")
            self.assertEqual(result.cost.state, "unknown")
            self.assertTrue(result.cost.reserve_retained)

        def case_test_huge_exponent_cost_is_bounded():
            body = (
                '{"model":"%s","answers":{"q":{"type":"noul","noul":0.5}},'
                '"usage":{"input_tokens":1,"output_tokens":1,"cost":1e999999999}}' % jev.MODEL
            )
            result = run(
                FakeTransport(resp(200, body)),
                qs=[jev.QuestionDef("q", jev.PREDICATE, "p")],
            )
            self.assertEqual(result.status, "protocol_error")
            self.assertEqual(result.cost.state, "unknown")
            self.assertTrue(result.cost.reserve_retained)

        def case_test_duplicate_json_key_rejected_in_depth_check_path():
            body = (
                '{"model":"%s","answers":{"q":{"type":"noul","noul":0.5,"noul":0.6}},'
                '"usage":{"input_tokens":1,"output_tokens":1}}' % jev.MODEL
            )
            result = run(
                FakeTransport(resp(200, body)),
                qs=[jev.QuestionDef("q", jev.PREDICATE, "p")],
            )
            self.assertEqual(result.status, "protocol_error")

        for label, case in (
            ("test_provider_and_id_bounded", case_test_provider_and_id_bounded),
            ("test_json_depth_bound", case_test_json_depth_bound),
            ("test_response_body_over_byte_bound", case_test_response_body_over_byte_bound),
            ("test_huge_exponent_cost_is_bounded", case_test_huge_exponent_cost_is_bounded),
            ("test_duplicate_json_key_rejected_in_depth_check_path", case_test_duplicate_json_key_rejected_in_depth_check_path),
        ):
            with self.subTest(case=label):
                case()

    def test_deep_body_is_protocol_error_not_escape(self):
        result = run(FakeTransport(resp(200, "[" * 500 + "]" * 500)))
        self.assertEqual(result.status, "protocol_error")
        self.assertEqual(result.cost.state, "unknown")
        self.assertTrue(result.cost.reserve_retained)


class CriteriaStructureBoundTests(unittest.TestCase):
    def test_cyclic_criteria_rejected_before_freezing(self):
        criteria = {"true": {}, "false": "no"}
        criteria["true"] = criteria
        with self.assertRaises(jev.JevValidationError):
            jev.QuestionDef("q", jev.PREDICATE, "p", criteria=criteria)

    def test_overdeep_criteria_rejected_before_freezing(self):
        nested = "value"
        for _ in range(100):
            nested = {"nested": nested}
        with self.assertRaises(jev.JevValidationError):
            jev.QuestionDef(
                "q", jev.PREDICATE, "p", criteria={"true": nested, "false": "no"}
            )


if __name__ == "__main__":
    unittest.main()
