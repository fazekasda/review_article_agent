"""Cross-module integration: real ``jev.validate_response`` -> C02 projection.

Task T5d. The pure projection (``runtime/decision_records.py``) revalidates every
typed input, but C02 §6.2's presence union permits an ``answer`` **iff** the
outcome is ``answered`` or ``partial_error``. The real typed adapter
(``runtime/jev.validate_response``) reports an **answerless** ``partial_error``
for a missing answer and for a defensive ``type:"error"`` provider error, so those
genuine failures cannot be projected verbatim.

These tests drive the projection from the **actual** adapter (a validated
response wrapped into a submitted :class:`~review_article_agent.runtime.jev.
DispatchResult`), not from hand-built outcomes, and assert:

* every real adapter failure projects to an inert, valid record that retains the
  candidate, the original per-question error and diagnostic, and the raw source
  provenance (the ``adapter_outcome`` audit field);
* an answerless adapter ``partial_error`` (``missing_answer``/``provider_error``)
  reconciles to canonical ``omitted`` with **no** invented answer and **no**
  scientific-uncertainty claim; refusal/malformed/unsupported are unchanged;
* the strict mapping validator still refuses a raw answerless canonical
  ``partial_error`` and a reasonless ``uncertain``/``omitted`` (the contract is
  not weakened);
* the single direct-call charge is counted once.

No network, no credential, no paid call, no store. Run:

    PYTHONPATH=src python3 -m unittest tests.test_adapter_decision_projection
"""

from __future__ import annotations

import json
import sys
import unittest
from decimal import Decimal
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from review_article_agent.runtime import decision_records as dr  # noqa: E402
from review_article_agent.runtime import jev  # noqa: E402

PROJECT = "proj_A"
TARGET = {"project_id": PROJECT, "artifact_id": "candidate_17", "revision_id": "rev_3"}
DISPATCH = "disp_0001"
TASK_CONTEXT = "task_1"
REQUEST_SHA256 = "f" * 64
RAISED_AT = "2026-10-27T12:00:00+00:00"


# --------------------------------------------------------------------------- #
# Real adapter fixtures
# --------------------------------------------------------------------------- #
def predicate(qid):
    return jev.QuestionDef(qid, jev.PREDICATE, "Is it original?")


def choice(qid):
    return jev.QuestionDef(
        qid, jev.CHOICE, "Which design?", options=("randomised", "observational", "other")
    )


def score(qid):
    return jev.QuestionDef(qid, jev.SCORE, "How confident?", levels=("low", "moderate", "high"))


def box(qid):
    return jev.QuestionDef(qid, jev.BOX, "unsupported box")


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
            "score": 1.0,
            "confidence": 0.66,
            "probabilities": {"0": 0.1, "1": 0.6, "2": 0.3},
            "legend": {"0": "low", "1": "moderate", "2": "high"},
        },
    }


def payload(answers, **overrides):
    body = {
        "id": "gen-dec-1",
        "model": "typesafe/jev-1.14-20261014",
        "provider": "TypeSafe",
        "answers": answers,
        "usage": {"input_tokens": 485, "output_tokens": 76, "cost": Decimal("0.00002037")},
    }
    body.update(overrides)
    return body


def wire_body(body_answers, **overrides):
    """A JSON wire body (real transport text) with a numeric provider cost."""
    body = {
        "id": "gen-dec-1",
        "model": "typesafe/jev-1.14-20261014",
        "provider": "TypeSafe",
        "answers": body_answers,
        "usage": {"input_tokens": 485, "output_tokens": 76, "cost": 0.00002037},
    }
    body.update(overrides)
    return json.dumps(body)


def versions():
    return {
        "context_kind": "model_derived",
        "input_digest": "b" * 64,
        "model": {"state": "known", "ref": {"version": "jev-1.14-20261014", "digest": "a" * 64}},
        "prompt": {"state": "known", "ref": {"version": "screen/1", "digest": "c" * 64}},
        "rubric": {"state": "known", "ref": {"version": "jev-rubric/1", "digest": "d" * 64}},
        "parser": {"state": "not_applicable"},
        "adapter": {"state": "known", "ref": {"version": "jev-adapter/1", "digest": "e" * 64}},
        "route": "direct_call",
    }


def refs_for(qs, artifact_id, prefix):
    return {
        q.question_id: {
            "project_id": PROJECT,
            "artifact_id": artifact_id,
            "revision_id": f"{prefix}_{q.question_id}",
        }
        for q in qs
    }


def bindings_for(qs):
    return {
        q.question_id: dr.QuestionBinding(
            question_id=q.question_id,
            question_kind=q.question_kind,
            question_def_digest=dr.question_descriptor_digest(q),
            options=tuple(q.options),
            levels=tuple(q.levels),
        )
        for q in qs
    }


def wrap(validated, *, reserve="0.25"):
    """Wrap a real validated response into a submitted ``DispatchResult``.

    The reserve/request digest mirror the adapter's own assembly
    (``jev._finish_dispatch``); no field is invented.
    """
    return jev.DispatchResult(
        status="completed",
        calls_attempted=1,
        submitted=True,
        attempt_no=1,
        idempotent_admission_proven=False,
        no_automatic_retry=True,
        endpoint=jev.ENDPOINT,
        model=jev.MODEL,
        actual_model=validated.model,
        provider=validated.provider,
        response_id=validated.response_id,
        usage=jev.Usage(validated.input_tokens, validated.output_tokens),
        cost=jev.CostRecord(
            state="known", amount_usd=validated.cost_usd, reserve_usd=reserve
        ),
        outcomes=validated.outcomes,
        http_status=200,
        request_bytes=256,
        request_sha256=REQUEST_SHA256,
        cost_upper_bound_usd="0.0001",
        reserve_usd=reserve,
    )


def project(validated, qs):
    result = wrap(validated)
    return dr.project_decision_records(
        result,
        qs,
        project_id=PROJECT,
        target=TARGET,
        question_def_ref=refs_for(qs, "question_def", "qd_rev"),
        criteria_ref=refs_for(qs, "criteria", "crit_rev"),
        dispatch_ref=DISPATCH,
        task_context=TASK_CONTEXT,
        versions=versions(),
        bindings=bindings_for(qs),
        expected_request_sha256=REQUEST_SHA256,
    )


def project_result(result, qs, *, expected_request_sha256=None):
    """Project an already-built (real) ``DispatchResult`` for *qs*."""
    return dr.project_decision_records(
        result,
        qs,
        project_id=PROJECT,
        target=TARGET,
        question_def_ref=refs_for(qs, "question_def", "qd_rev"),
        criteria_ref=refs_for(qs, "criteria", "crit_rev"),
        dispatch_ref=DISPATCH,
        task_context=TASK_CONTEXT,
        versions=versions(),
        bindings=bindings_for(qs),
        expected_request_sha256=(
            result.request_sha256 if expected_request_sha256 is None
            else expected_request_sha256
        ),
    )


def by_id(records):
    return {r.question_id: r.as_mapping() for r in records}


def assert_inert(test, mapping):
    test.assertFalse(mapping["is_exclusion"])
    test.assertTrue(mapping["retains_candidate"])
    test.assertFalse(mapping["grants_eligibility"])


# --------------------------------------------------------------------------- #
# Real missing-answer failure -> canonical omitted
# --------------------------------------------------------------------------- #
class MissingAnswerProjectionTest(unittest.TestCase):
    def _validated(self):
        qs = [predicate("is_original_report"), predicate("missing_q")]
        answers = {"is_original_report": {"type": "noul", "noul": 0.97}}
        return qs, jev.validate_response(payload(answers), qs)

    def test_adapter_reports_answerless_partial_error(self):
        qs, validated = self._validated()
        outcome = validated.outcomes["missing_q"]
        self.assertEqual(outcome.outcome, "partial_error")
        self.assertIsNone(outcome.answer)
        self.assertEqual(outcome.per_question_error.reason, "missing_answer")

    def test_projection_normalizes_to_omitted_and_retains_provenance(self):
        qs, validated = self._validated()
        mapping = by_id(project(validated, qs))["missing_q"]
        self.assertEqual(mapping["outcome"], "omitted")
        self.assertEqual(mapping["adapter_outcome"], "partial_error")
        self.assertNotIn("answer", mapping)
        self.assertEqual(mapping["per_question_error"]["reason"], "missing_answer")
        self.assertEqual(mapping["per_question_error"]["question_id"], "missing_q")
        self.assertEqual(mapping["diagnostics"]["code"], "missing_answer")
        assert_inert(self, mapping)

    def test_valid_answer_kept_exactly(self):
        qs, validated = self._validated()
        mapping = by_id(project(validated, qs))["is_original_report"]
        self.assertEqual(mapping["outcome"], "answered")
        self.assertEqual(mapping["answer"]["probability"], "0.97")
        self.assertNotIn("adapter_outcome", mapping)


# --------------------------------------------------------------------------- #
# Real provider type:error -> canonical omitted
# --------------------------------------------------------------------------- #
class ProviderErrorProjectionTest(unittest.TestCase):
    def test_type_error_normalizes_to_omitted(self):
        qs = [predicate("provider_err_q")]
        answers = {"provider_err_q": {"type": "error", "reason": "boom"}}
        validated = jev.validate_response(payload(answers), qs)
        self.assertEqual(validated.outcomes["provider_err_q"].outcome, "partial_error")
        self.assertEqual(
            validated.outcomes["provider_err_q"].per_question_error.reason, "provider_error"
        )
        mapping = by_id(project(validated, qs))["provider_err_q"]
        self.assertEqual(mapping["outcome"], "omitted")
        self.assertEqual(mapping["adapter_outcome"], "partial_error")
        self.assertNotIn("answer", mapping)
        self.assertEqual(mapping["per_question_error"]["reason"], "provider_error")
        self.assertEqual(mapping["diagnostics"]["code"], "provider_error")
        assert_inert(self, mapping)

    def test_refusal_stays_refusal(self):
        qs = [predicate("ref_q")]
        answers = {"ref_q": {"type": "error", "reason": "refused"}}
        validated = jev.validate_response(payload(answers), qs)
        self.assertEqual(validated.outcomes["ref_q"].outcome, "refusal")
        mapping = by_id(project(validated, qs))["ref_q"]
        self.assertEqual(mapping["outcome"], "refusal")
        self.assertNotIn("adapter_outcome", mapping)
        self.assertNotIn("answer", mapping)
        self.assertEqual(mapping["per_question_error"]["reason"], "provider_error")
        assert_inert(self, mapping)


# --------------------------------------------------------------------------- #
# Real malformed answer -> canonical malformed (unchanged)
# --------------------------------------------------------------------------- #
class MalformedProjectionTest(unittest.TestCase):
    def test_type_mismatch_stays_malformed(self):
        qs = [predicate("mal_q")]
        answers = {"mal_q": {"type": "choice", "choice": "x"}}
        validated = jev.validate_response(payload(answers), qs)
        self.assertEqual(validated.outcomes["mal_q"].outcome, "malformed")
        mapping = by_id(project(validated, qs))["mal_q"]
        self.assertEqual(mapping["outcome"], "malformed")
        self.assertNotIn("adapter_outcome", mapping)
        self.assertNotIn("answer", mapping)
        self.assertEqual(mapping["per_question_error"]["reason"], "malformed_answer")
        assert_inert(self, mapping)


# --------------------------------------------------------------------------- #
# Valid answers + unsupported box + one charge
# --------------------------------------------------------------------------- #
class MixedOutcomesProjectionTest(unittest.TestCase):
    def test_valid_answers_and_unsupported_box_project(self):
        qs = [
            predicate("is_original_report"),
            choice("study_design"),
            score("confidence_level"),
            box("box_q"),
        ]
        validated = jev.validate_response(payload(valid_answers()), qs)
        records = project(validated, qs)
        mapping = by_id(records)
        self.assertEqual(set(mapping), {q.question_id for q in qs})
        self.assertEqual(mapping["is_original_report"]["answer"]["probability"], "0.97")
        self.assertEqual(mapping["study_design"]["answer"]["choice"], "randomised")
        self.assertEqual(mapping["confidence_level"]["answer"]["score"], "1.0")
        self.assertEqual(mapping["box_q"]["outcome"], "unsupported")
        self.assertNotIn("answer", mapping["box_q"])
        self.assertEqual(mapping["box_q"]["question_kind"], "box")
        for record in records:
            assert_inert(self, record.as_mapping())

    def test_direct_charge_counted_once_across_mixed_failures(self):
        qs = [
            predicate("is_original_report"),
            predicate("missing_q"),
            predicate("provider_err_q"),
        ]
        answers = {
            "is_original_report": {"type": "noul", "noul": 0.97},
            "provider_err_q": {"type": "error", "reason": "boom"},
        }
        validated = jev.validate_response(payload(answers), qs)
        records = project(validated, qs)
        self.assertEqual(len(records), 3)
        charges = dr.ledger_charges(records)
        self.assertEqual(len(charges), 1)
        self.assertEqual(charges[0]["dispatch_ref"], DISPATCH)
        self.assertEqual(charges[0]["state"], "known")


# --------------------------------------------------------------------------- #
# The strict mapping validator is not weakened
# --------------------------------------------------------------------------- #
class StrictMappingStillRefusedTest(unittest.TestCase):
    def _omitted_mapping(self):
        qs = [predicate("missing_q")]
        answers = {}
        validated = jev.validate_response(payload(answers), qs)
        return by_id(project(validated, qs))["missing_q"]

    def test_normalized_record_is_valid_canonical_mapping(self):
        mapping = self._omitted_mapping()
        self.assertEqual(dr.validate_decision_record_mapping(mapping), mapping)

    def test_raw_answerless_partial_error_refused(self):
        # Flipping the normalized record back to an answerless canonical
        # ``partial_error`` (and dropping the audit field) must be refused: the
        # §6.2 presence union is **not** weakened.
        mapping = self._omitted_mapping()
        mapping["outcome"] = "partial_error"
        del mapping["adapter_outcome"]
        with self.assertRaises(dr.DecisionRecordValidationError):
            dr.validate_decision_record_mapping(mapping)

    def test_reasonless_omitted_refused(self):
        mapping = self._omitted_mapping()
        del mapping["per_question_error"]
        del mapping["diagnostics"]
        with self.assertRaises(dr.DecisionRecordValidationError):
            dr.validate_decision_record_mapping(mapping)

    def test_adapter_outcome_relation_is_strict(self):
        mapping = self._omitted_mapping()
        mapping["adapter_outcome"] = "refusal"
        with self.assertRaises(dr.DecisionRecordValidationError):
            dr.validate_decision_record_mapping(mapping)


# --------------------------------------------------------------------------- #
# Whole-call submitted failures (real plan + injected local transport)
# --------------------------------------------------------------------------- #
API_KEY = "sk-test-not-a-real-key"
STATE = {"fixture": "synthetic-spike-fixture-not-a-real-paper", "passage": "..."}


def plan_questions():
    return [predicate("is_original_report"), choice("study_design"), box("box_q")]


class _RaisingTransport:
    def __init__(self, exc):
        self.exc = exc
        self.calls = 0

    def __call__(self, body, api_key, *, endpoint, timeout):
        self.calls += 1
        raise self.exc


class _CannedTransport:
    def __init__(self, status, body_text=""):
        self.response = jev.TransportResponse(status, body_text)
        self.calls = 0

    def __call__(self, body, api_key, *, endpoint, timeout):
        self.calls += 1
        return self.response


def run_dispatch(qs, transport):
    plan = jev.plan_dispatch(qs, STATE, reserve_usd="0.25")
    return jev.dispatch_plan(plan, api_key=API_KEY, transport=transport)


class WholeCallFailureProjectionTest(unittest.TestCase):
    """Every requested supported question is retained on a whole-call failure."""

    def _assert_failed_envelope(self, result, qs, status, cost_state):
        self.assertEqual(result.status, status)
        self.assertTrue(result.submitted)
        self.assertEqual(result.calls_attempted, 1)
        self.assertEqual(result.cost.state, cost_state)
        # The adapter retained only the global diagnostic and the box outcome.
        self.assertIsNotNone(result.diagnostic)
        self.assertEqual(set(result.outcomes), {"box_q"})

        records = project_result(result, qs)
        mapping = by_id(records)
        self.assertEqual(set(mapping), {q.question_id for q in qs})

        for q in qs:
            record = mapping[q.question_id]
            assert_inert(self, record)
            if q.question_kind == jev.BOX:
                # The existing BOX ``unsupported`` outcome is untouched.
                self.assertEqual(record["outcome"], "unsupported")
                self.assertNotIn("call_status", record)
                continue
            # A supported question with no validated outcome -> canonical omitted
            # carrying the EXISTING global diagnostic and the call status.
            self.assertEqual(record["outcome"], "omitted")
            self.assertEqual(record["call_status"], status)
            self.assertNotIn("answer", record)
            self.assertNotIn("per_question_error", record)
            self.assertNotIn("adapter_outcome", record)
            self.assertEqual(record["diagnostics"], dr._project_diagnostic(result.diagnostic))
            self.assertEqual(record["diagnostics"]["code"], status)
            self.assertEqual(dr.validate_decision_record_mapping(record), record)

        # One charge per dispatch; an unresolved cost is never zero.
        charges = dr.ledger_charges(records)
        self.assertEqual(len(charges), 1)
        self.assertEqual(charges[0]["dispatch_ref"], DISPATCH)
        self.assertEqual(charges[0]["state"], cost_state)
        if cost_state in ("unknown", "not_incurred"):
            # An unresolved / not-billed cost carries no amount (never a silent 0).
            self.assertNotIn("amount_usd", charges[0])
        return mapping

    def test_raised_timeout_transport_error_unknown_reserve_retained(self):
        qs = plan_questions()
        transport = _RaisingTransport(TimeoutError("simulated timeout"))
        result = run_dispatch(qs, transport)
        self.assertEqual(transport.calls, 1)  # exactly one POST; no blind retry
        self.assertEqual(result.cost.reserve_usd, "0.25")
        self.assertIsNone(result.cost.amount_usd)
        self.assertTrue(result.cost.reserve_retained)
        self._assert_failed_envelope(result, qs, "transport_error", "unknown")

    def test_http_401_definitive_api_error_not_incurred(self):
        qs = plan_questions()
        transport = _CannedTransport(401, "")
        result = run_dispatch(qs, transport)
        self.assertEqual(transport.calls, 1)
        self.assertEqual(result.cost.state, "not_incurred")
        self.assertIsNone(result.cost.amount_usd)
        self._assert_failed_envelope(result, qs, "api_error", "not_incurred")

    def test_http_429_ambiguous_api_error_unknown(self):
        qs = plan_questions()
        transport = _CannedTransport(429, "")
        result = run_dispatch(qs, transport)
        self.assertEqual(transport.calls, 1)
        self.assertTrue(result.cost.reserve_retained)
        self._assert_failed_envelope(result, qs, "api_error", "unknown")

    def test_http_500_ambiguous_api_error_unknown(self):
        qs = plan_questions()
        transport = _CannedTransport(500, "")
        result = run_dispatch(qs, transport)
        self._assert_failed_envelope(result, qs, "api_error", "unknown")

    def test_malformed_json_protocol_error(self):
        qs = plan_questions()
        transport = _CannedTransport(200, "{not json")
        result = run_dispatch(qs, transport)
        self.assertEqual(result.status, "protocol_error")
        self._assert_failed_envelope(result, qs, "protocol_error", "unknown")

    def test_malformed_envelope_protocol_error_vs_valid_partial_answers(self):
        # A 200 whose envelope is malformed (answers not an object) is a whole-call
        # protocol failure: every supported question is retained as omitted.
        qs = plan_questions()
        malformed = valid_answers()
        transport = _CannedTransport(200, wire_body(malformed, answers="not-an-object"))
        result = run_dispatch(qs, transport)
        self.assertEqual(result.status, "protocol_error")
        self._assert_failed_envelope(result, qs, "protocol_error", "known")

        # A 200 whose envelope is valid but carries only a partial set of answers is
        # a completed call: the missing answer is the T5d per-question failure
        # (canonical omitted via adapter_outcome), NOT a whole-call failure.
        qs2 = [predicate("is_original_report"), predicate("missing_q")]
        partial = {"is_original_report": {"type": "noul", "noul": 0.97}}
        transport2 = _CannedTransport(200, wire_body(partial))
        result2 = run_dispatch(qs2, transport2)
        self.assertEqual(result2.status, "completed")
        mapping = by_id(project_result(result2, qs2))
        self.assertEqual(mapping["missing_q"]["outcome"], "omitted")
        self.assertEqual(mapping["missing_q"]["adapter_outcome"], "partial_error")
        self.assertNotIn("call_status", mapping["missing_q"])
        self.assertEqual(mapping["is_original_report"]["outcome"], "answered")


# --------------------------------------------------------------------------- #
# Missing evidence is still refused (no silent synthesis)
# --------------------------------------------------------------------------- #
class MissingEvidenceRefusedTest(unittest.TestCase):
    def test_completed_envelope_missing_outcome_refused(self):
        # A completed envelope with a missing supported outcome (no validated call
        # failure) must still be refused.
        qs = [predicate("is_original_report"), predicate("missing_q")]
        box_only = {
            "box_q": jev.DecisionOutcome(
                question_id="box_q",
                question_kind=jev.BOX,
                outcome="unsupported",
                per_question_error=jev.PerQuestionError(
                    question_id="box_q",
                    reason="unsupported_question_kind",
                    message_redacted="static",
                ),
                diagnostic=jev.Diagnostic(
                    code="unsupported_question_kind", detail_redacted="static", raised_at=RAISED_AT
                ),
            )
        }
        result = jev.DispatchResult(
            status="completed",
            calls_attempted=1,
            submitted=True,
            attempt_no=1,
            idempotent_admission_proven=False,
            no_automatic_retry=True,
            endpoint=jev.ENDPOINT,
            model=jev.MODEL,
            actual_model="typesafe/jev-1.14-20261014",
            provider="TypeSafe",
            response_id="gen-dec-1",
            usage=jev.Usage(1, 1),
            cost=jev.CostRecord(state="known", amount_usd="0.0001", reserve_usd="0.25"),
            outcomes=box_only,
            http_status=200,
            request_bytes=256,
            request_sha256=REQUEST_SHA256,
            cost_upper_bound_usd="0.0001",
            reserve_usd="0.25",
        )
        with self.assertRaises(dr.DecisionRecordProjectionError):
            project_result(result, qs)

    def test_unsubmitted_blocked_refused(self):
        # A blocked (unsubmitted) dispatch never reached the network: it has no
        # meaning as a decision and is refused.
        qs = plan_questions()
        result = jev.execute(
            qs, STATE, api_key=None, reserve_usd="0.25", authorized=False
        )
        self.assertEqual(result.status, "blocked")
        self.assertFalse(result.submitted)
        with self.assertRaises(dr.DecisionRecordProjectionError):
            project_result(result, qs)

    def test_failed_envelope_without_global_diagnostic_refused(self):
        # A failed submitted envelope that dropped its global diagnostic is
        # malformed evidence and refused (never a silent reasonless omitted).
        qs = [predicate("is_original_report")]
        result = jev.DispatchResult(
            status="transport_error",
            calls_attempted=1,
            submitted=True,
            attempt_no=1,
            idempotent_admission_proven=False,
            no_automatic_retry=True,
            endpoint=jev.ENDPOINT,
            model=jev.MODEL,
            actual_model=None,
            provider=None,
            response_id=None,
            usage=None,
            cost=jev.CostRecord(state="unknown", reserve_usd="0.25", reserve_retained=True),
            outcomes={},
            http_status=None,
            request_bytes=256,
            request_sha256=REQUEST_SHA256,
            cost_upper_bound_usd="0.0001",
            reserve_usd="0.25",
            diagnostic=None,
        )
        with self.assertRaises(dr.DecisionRecordProjectionError):
            project_result(result, qs)

    def test_unknown_status_refused(self):
        qs = [predicate("is_original_report")]
        result = jev.DispatchResult(
            status="not_a_status",
            calls_attempted=1,
            submitted=True,
            attempt_no=1,
            idempotent_admission_proven=False,
            no_automatic_retry=True,
            endpoint=jev.ENDPOINT,
            model=jev.MODEL,
            actual_model=None,
            provider=None,
            response_id=None,
            usage=None,
            cost=jev.CostRecord(state="unknown", reserve_usd="0.25", reserve_retained=True),
            outcomes={},
            http_status=None,
            request_bytes=256,
            request_sha256=REQUEST_SHA256,
            cost_upper_bound_usd="0.0001",
            reserve_usd="0.25",
            diagnostic=jev.Diagnostic(code="x", detail_redacted="y", raised_at=RAISED_AT),
        )
        with self.assertRaises(dr.DecisionRecordProjectionError):
            project_result(result, qs)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()