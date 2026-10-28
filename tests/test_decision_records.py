"""Offline tests for the pure C02 per-question DecisionRecord projection.

Covers ``src/review_article_agent/runtime/decision_records.py`` against the task
T5c acceptance requirements and ``docs/contracts/02-gates-and-runtime.md``
§6.1/§6.2/§6.3:

* one immutable record per question, with the declared kind/options/levels and a
  per-question descriptor pin that binds the actually-submitted definition;
* predicate/choice/score answers preserved exactly (raw probability, no cutoff);
  optional confidence/probabilities/legend carried through;
* every error outcome (partial_error/refusal/malformed/unsupported/uncertain/
  omitted) retains the candidate and is never an exclusion or an eligibility;
* a ``box`` question can never carry an answer and is recorded ``unsupported``;
* malformed values are never defaulted; foreign/cross-project refs, bad/stale
  versions, a wrong route/bucket, a stale request hash, and forged cyclic/deep/
  non-finite/mutable-alias inputs all fail closed;
* two questions share one direct-call ``CostRecord`` counted once;
* the record id is stable/content-derived and dispatch-scoped.

No network, no credential, no paid call, no store. Run:

    PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_decision_records.py'
"""

from __future__ import annotations

import dataclasses
import json
import sys
import unittest
from collections.abc import Mapping
from decimal import Decimal
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from review_article_agent.runtime import decision_records as dr  # noqa: E402
from review_article_agent.runtime import jev  # noqa: E402

PROJECT = "proj_A"
TARGET = {"project_id": PROJECT, "artifact_id": "candidate_17", "revision_id": "rev_3"}
QDEF_REF = {"project_id": PROJECT, "artifact_id": "question_def", "revision_id": "qd_rev_1"}
CRIT_REF = {"project_id": PROJECT, "artifact_id": "criteria", "revision_id": "crit_rev_1"}
DISPATCH = "disp_0001"
TASK_CONTEXT = "task_1"
RAISED_AT = "2026-10-27T12:00:00+00:00"


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


def versions(**overrides):
    value = {
        "context_kind": "model_derived",
        "input_digest": "b" * 64,
        "model": {"state": "known", "ref": {"version": "jev-1.14-20261014", "digest": "a" * 64}},
        "prompt": {"state": "known", "ref": {"version": "screen/1", "digest": "c" * 64}},
        "rubric": {"state": "known", "ref": {"version": "jev-rubric/1", "digest": "d" * 64}},
        "parser": {"state": "not_applicable"},
        "adapter": {"state": "known", "ref": {"version": "jev-adapter/1", "digest": "e" * 64}},
        "route": "direct_call",
    }
    value.update(overrides)
    return value


def make_cost(state="known", amount="0.00002037", reserve="0.25"):
    return jev.CostRecord(
        state=state,
        amount_usd=amount if state in ("known", "estimated") else None,
        reserve_usd=reserve,
        reserve_retained=state in ("unknown", "estimated"),
    )


def make_result(outcomes, *, cost=None, submitted=True, calls_attempted=1, status="completed",
                actual_model="typesafe/jev-1.14-20261014", request_sha256="f" * 64,
                endpoint=jev.ENDPOINT, model=jev.MODEL, usage=None, http_status=200):
    return jev.DispatchResult(
        status=status,
        calls_attempted=calls_attempted,
        submitted=submitted,
        attempt_no=1,
        idempotent_admission_proven=False,
        no_automatic_retry=True,
        endpoint=endpoint,
        model=model,
        actual_model=actual_model,
        provider="TypeSafe",
        response_id="gen-dec-1",
        usage=usage if usage is not None else jev.Usage(485, 76),
        cost=cost if cost is not None else make_cost(),
        outcomes=outcomes,
        http_status=http_status,
        request_bytes=256,
        request_sha256=request_sha256,
        cost_upper_bound_usd="0.0001",
        reserve_usd="0.25",
    )


def predicate_answer(probability="0.83"):
    return jev.Answer(answer_kind=jev.PREDICATE, wire_type="noul", probability=Decimal(probability))


def choice_answer(choice="randomised", confidence=None, probabilities=None):
    return jev.Answer(
        answer_kind=jev.CHOICE,
        wire_type="choice",
        choice=choice,
        confidence=None if confidence is None else Decimal(confidence),
        probabilities=(
            None
            if probabilities is None
            else {k: Decimal(v) for k, v in probabilities.items()}
        ),
    )


def score_answer(score="1.09", legend=None, confidence=None, probabilities=None):
    return jev.Answer(
        answer_kind=jev.SCORE,
        wire_type="score",
        score=Decimal(score),
        legend=legend,
        confidence=None if confidence is None else Decimal(confidence),
        probabilities=(
            None
            if probabilities is None
            else {k: Decimal(v) for k, v in probabilities.items()}
        ),
    )


def answered(qid, kind, answer):
    return jev.DecisionOutcome(question_id=qid, question_kind=kind, outcome="answered", answer=answer)


def error_outcome(qid, kind, outcome, reason, code=None):
    return jev.DecisionOutcome(
        question_id=qid,
        question_kind=kind,
        outcome=outcome,
        per_question_error=jev.PerQuestionError(
            question_id=qid, reason=reason, message_redacted="static message", code=code
        ),
        diagnostic=jev.Diagnostic(code="c", detail_redacted="static detail", raised_at=RAISED_AT),
    )


def valid_outcomes():
    return {
        "is_original_report": answered("is_original_report", jev.PREDICATE, predicate_answer()),
        "study_design": answered(
            "study_design",
            jev.CHOICE,
            choice_answer("randomised", confidence="0.81",
                          probabilities={"randomised": "0.9", "observational": "0.1", "other": "0"}),
        ),
        "confidence_level": answered(
            "confidence_level",
            jev.SCORE,
            score_answer("1.09", legend={"0": "low", "1": "moderate", "2": "high"},
                         confidence="0.66", probabilities={"0": "0.1", "1": "0.6", "2": "0.3"}),
        ),
    }


def forge_question(**overrides):
    question = object.__new__(jev.QuestionDef)
    fields = dict(
        question_id="is_original_report",
        question_kind=jev.PREDICATE,
        instructions="Is it original?",
        options=(),
        levels=(),
        criteria=None,
    )
    fields.update(overrides)
    for key, value in fields.items():
        object.__setattr__(question, key, value)
    return question


def forge_cost(**overrides):
    cost = object.__new__(jev.CostRecord)
    fields = dict(
        state="known",
        currency="USD",
        route="direct_call",
        accounting_bucket="direct_call",
        amount_usd="0.00002037",
        reserve_usd="0.25",
        reserve_retained=False,
        dispatch_ref=None,
    )
    fields.update(overrides)
    for key, value in fields.items():
        object.__setattr__(cost, key, value)
    return cost


def refs_for(qs, artifact_id, revision_prefix):
    """Build an exact per-question ref mapping for *qs* (same project)."""
    return {
        q.question_id: {
            "project_id": PROJECT,
            "artifact_id": artifact_id,
            "revision_id": f"{revision_prefix}_{q.question_id}",
        }
        for q in qs
    }


def bindings_for(qs):
    """Build trusted submission-time bindings for *qs*.

    A question whose descriptor cannot be derived (a forged cyclic/deep/non-finite
    criteria) yields a sentinel digest, so the projection itself raises the typed
    error rather than the fixture.
    """
    out = {}
    for q in qs:
        try:
            digest = dr.question_descriptor_digest(q)
        except Exception:
            digest = "0" * 64
        out[q.question_id] = dr.QuestionBinding(
            question_id=q.question_id,
            question_kind=getattr(q, "question_kind", jev.PREDICATE),
            question_def_digest=digest,
            options=tuple(getattr(q, "options", ()) or ()),
            levels=tuple(getattr(q, "levels", ()) or ()),
        )
    return out


def project(result=None, qs=None, **overrides):
    questions_ = list(qs) if qs is not None else questions()
    params = dict(
        project_id=PROJECT,
        target=TARGET,
        question_def_ref=refs_for(questions_, "question_def", "qd_rev"),
        criteria_ref=refs_for(questions_, "criteria", "crit_rev"),
        dispatch_ref=DISPATCH,
        task_context=TASK_CONTEXT,
        versions=versions(),
        bindings=bindings_for(questions_),
        expected_request_sha256="f" * 64,
    )
    params.update(overrides)
    return dr.project_decision_records(
        result if result is not None else make_result(valid_outcomes()),
        questions_,
        **params,
    )


# --------------------------------------------------------------------------- #
# Basics: typed answers preserved
# --------------------------------------------------------------------------- #
class ProjectionBasicsTest(unittest.TestCase):
    def test_one_record_per_question(self):
        records = project()
        self.assertEqual([r.question_id for r in records],
                         ["is_original_report", "study_design", "confidence_level"])
        self.assertTrue(all(r.route == "direct_call" for r in records))

    def test_predicate_probability_retained_raw_no_cutoff(self):
        record = project()[0]
        mapping = record.as_mapping()
        self.assertEqual(mapping["outcome"], "answered")
        self.assertEqual(mapping["question_kind"], "predicate")
        self.assertEqual(mapping["answer"], {"answer_kind": "predicate", "wire_type": "noul",
                                             "probability": "0.83"})
        self.assertEqual(record.is_exclusion, False)
        self.assertEqual(record.retains_candidate, True)
        self.assertEqual(record.grants_eligibility, False)

    def test_choice_options_confidence_probabilities(self):
        mapping = project()[1].as_mapping()
        self.assertEqual(mapping["options"], ["randomised", "observational", "other"])
        self.assertEqual(mapping["levels"], [])
        answer = mapping["answer"]
        self.assertEqual(answer["choice"], "randomised")
        self.assertEqual(answer["confidence"], "0.81")
        self.assertEqual(answer["probabilities"],
                         {"randomised": "0.9", "observational": "0.1", "other": "0"})

    def test_score_levels_legend_confidence(self):
        mapping = project()[2].as_mapping()
        self.assertEqual(mapping["levels"], ["low", "moderate", "high"])
        answer = mapping["answer"]
        self.assertEqual(answer["score"], "1.09")
        self.assertEqual(answer["legend"], {"0": "low", "1": "moderate", "2": "high"})
        self.assertEqual(answer["confidence"], "0.66")

    def test_descriptor_pin_binds_declared_definition(self):
        record = project()[0]
        expected = dr.question_descriptor_digest(questions()[0])
        self.assertEqual(record.as_mapping()["question_def_digest"], expected)
        self.assertNotEqual(expected, dr.question_descriptor_digest(questions()[1]))

    def test_provider_metadata_recorded_from_trusted_result(self):
        mapping = project()[0].as_mapping()
        self.assertEqual(mapping["provider"]["request_model"], jev.MODEL)
        self.assertEqual(mapping["provider"]["actual_model"], "typesafe/jev-1.14-20261014")
        self.assertEqual(mapping["provider"]["input_tokens"], 485)
        self.assertEqual(mapping["provider"]["output_tokens"], 76)
        self.assertEqual(mapping["provider"]["http_status"], 200)
        self.assertEqual(mapping["request_sha256"], "f" * 64)

    def test_version_digests_preserved_not_invented(self):
        mapping = project()[0].as_mapping()
        self.assertEqual(mapping["versions"]["model"]["ref"]["digest"], "a" * 64)
        self.assertEqual(mapping["versions"]["adapter"]["ref"]["version"], "jev-adapter/1")
        self.assertEqual(mapping["versions"]["route"], "direct_call")
        self.assertEqual(mapping["consumed_versions"], ["adapter", "model", "prompt"])


# --------------------------------------------------------------------------- #
# Immutability / round-trip
# --------------------------------------------------------------------------- #
class ImmutabilityTest(unittest.TestCase):
    def test_json_roundtrip_and_immutability(self):
        record = project()[0]
        mapping = record.as_mapping()
        self.assertEqual(json.loads(record.canonical_bytes), mapping)
        self.assertEqual(record.digest, __import__("hashlib").sha256(record.canonical_bytes).hexdigest())

    def test_as_mapping_mutation_does_not_drift(self):
        record = project()[0]
        out = record.as_mapping()
        out["answer"]["probability"] = "0.0"
        out["outcome"] = "omitted"
        self.assertEqual(record.as_mapping()["answer"]["probability"], "0.83")
        self.assertEqual(record.outcome, "answered")

    def test_record_is_read_only(self):
        record = project()[0]
        with self.assertRaises(AttributeError):
            record.outcome = "omitted"  # type: ignore[misc]
        with self.assertRaises(TypeError):
            record._mapping["outcome"] = "omitted"  # type: ignore[index]

    def test_cannot_hand_construct_a_record(self):
        with self.assertRaises(dr.DecisionRecordError):
            dr.DecisionRecord(object(), mapping={}, canonical_bytes=b"{}")  # type: ignore[arg-type]

    def test_validator_accepts_projected_mapping(self):
        record = project()[0]
        self.assertEqual(dr.validate_decision_record_mapping(record.as_mapping()),
                         record.as_mapping())


# --------------------------------------------------------------------------- #
# Error outcomes / never-exclusion
# --------------------------------------------------------------------------- #
class OutcomeRetentionTest(unittest.TestCase):
    def _single(self, outcome, kind, qid):
        q = jev.QuestionDef(qid, kind, "instr") if kind != jev.CHOICE else jev.QuestionDef(
            qid, kind, "instr", options=("a", "b")
        )
        return project(result=make_result({qid: outcome}), qs=[q])[0].as_mapping()

    def test_raw_answerless_partial_error_denied_by_validator(self):
        # C02 §6.2 presence union: ``answer`` is present iff the outcome is
        # ``answered`` or ``partial_error`` — a raw canonical ``partial_error``
        # MUST carry an answer. The strict mapping validator still refuses it
        # (this contract is **not** weakened); the typed adapter->canonical
        # projection reconciles the real answerless adapter outcome separately.
        mapping = project()[0].as_mapping()
        del mapping["answer"]
        mapping["outcome"] = "partial_error"
        with self.assertRaises(dr.DecisionRecordValidationError):
            dr.validate_decision_record_mapping(mapping)

    def test_answerless_partial_error_missing_answer_normalizes_to_omitted(self):
        # The typed adapter reports an answerless ``partial_error`` for a missing
        # answer; the projection reconciles it to canonical ``omitted`` with the
        # source literal preserved, never an invented answer or uncertainty.
        mapping = self._single(
            error_outcome("p", jev.PREDICATE, "partial_error", "missing_answer"),
            jev.PREDICATE,
            "p",
        )
        self.assertEqual(mapping["outcome"], "omitted")
        self.assertEqual(mapping["adapter_outcome"], "partial_error")
        self.assertNotIn("answer", mapping)
        self.assertEqual(mapping["per_question_error"]["reason"], "missing_answer")
        self.assertIn("diagnostics", mapping)
        self.assertFalse(mapping["is_exclusion"])
        self.assertTrue(mapping["retains_candidate"])
        self.assertFalse(mapping["grants_eligibility"])

    def test_answerless_partial_error_provider_error_normalizes_to_omitted(self):
        mapping = self._single(
            error_outcome("p", jev.PREDICATE, "partial_error", "provider_error"),
            jev.PREDICATE,
            "p",
        )
        self.assertEqual(mapping["outcome"], "omitted")
        self.assertEqual(mapping["adapter_outcome"], "partial_error")
        self.assertNotIn("answer", mapping)
        self.assertEqual(mapping["per_question_error"]["reason"], "provider_error")

    def test_answerless_partial_error_foreign_reason_refused(self):
        # Only a genuine missing_answer/provider_error reason is reconciled; any
        # other reason on an answerless partial_error is refused (no fabrication).
        with self.assertRaises(dr.DecisionRecordValidationError):
            self._single(
                error_outcome("p", jev.PREDICATE, "partial_error", "malformed_answer"),
                jev.PREDICATE,
                "p",
            )

    def test_answerless_partial_error_without_reason_refused(self):
        forged = jev.DecisionOutcome(
            question_id="p", question_kind=jev.PREDICATE, outcome="partial_error"
        )
        with self.assertRaises(dr.DecisionRecordValidationError):
            self._single(forged, jev.PREDICATE, "p")

    def test_answerless_partial_error_foreign_question_refused(self):
        forged = jev.DecisionOutcome(
            question_id="p",
            question_kind=jev.PREDICATE,
            outcome="partial_error",
            per_question_error=jev.PerQuestionError(
                question_id="other", reason="missing_answer", message_redacted="static"
            ),
            diagnostic=jev.Diagnostic(code="c", detail_redacted="d", raised_at=RAISED_AT),
        )
        with self.assertRaises(dr.DecisionRecordValidationError):
            self._single(forged, jev.PREDICATE, "p")

    def test_partial_error_retains_candidate_with_answer(self):
        forged = jev.DecisionOutcome(
            question_id="p",
            question_kind=jev.PREDICATE,
            outcome="partial_error",
            answer=predicate_answer("0.4"),
            per_question_error=jev.PerQuestionError(
                question_id="p", reason="provider_error", message_redacted="static"
            ),
            diagnostic=jev.Diagnostic(code="c", detail_redacted="d", raised_at=RAISED_AT),
        )
        mapping = self._single(forged, jev.PREDICATE, "p")
        self.assertEqual(mapping["outcome"], "partial_error")
        self.assertIn("answer", mapping)
        self.assertFalse(mapping["is_exclusion"])
        self.assertTrue(mapping["retains_candidate"])
        self.assertFalse(mapping["grants_eligibility"])

    def test_refusal_and_malformed_and_unsupported_retain_candidate(self):
        cases = [
            ("refusal", jev.PREDICATE, "p", "provider_error"),
            ("malformed", jev.PREDICATE, "p", "malformed_answer"),
            ("unsupported", jev.BOX, "b", "unsupported_question_kind"),
        ]
        for outcome, kind, qid, reason in cases:
            with self.subTest(outcome=outcome):
                mapping = self._single(error_outcome(qid, kind, outcome, reason), kind, qid)
                self.assertEqual(mapping["outcome"], outcome)
                self.assertNotIn("answer", mapping)
                self.assertFalse(mapping["is_exclusion"])
                self.assertTrue(mapping["retains_candidate"])
                self.assertFalse(mapping["grants_eligibility"])

    def test_uncertain_and_omitted_have_no_answer_with_reason(self):
        # §6.2 never-exclusion: an uncertain/omitted failure is retained with a
        # persisted reason (a diagnostic or a per-question error) and no answer.
        for outcome in ("uncertain", "omitted"):
            with self.subTest(outcome=outcome):
                forged = jev.DecisionOutcome(
                    question_id="p",
                    question_kind=jev.PREDICATE,
                    outcome=outcome,
                    diagnostic=jev.Diagnostic(
                        code="c", detail_redacted="d", raised_at=RAISED_AT
                    ),
                )
                mapping = self._single(forged, jev.PREDICATE, "p")
                self.assertNotIn("answer", mapping)
                self.assertFalse(mapping["is_exclusion"])

    def test_uncertain_and_omitted_without_reason_denied(self):
        # No silent, reasonless failure: an uncertain/omitted outcome with neither
        # a diagnostic nor a per-question error is refused.
        for outcome in ("uncertain", "omitted"):
            with self.subTest(outcome=outcome):
                forged = jev.DecisionOutcome(
                    question_id="p", question_kind=jev.PREDICATE, outcome=outcome
                )
                with self.assertRaises(dr.DecisionRecordValidationError):
                    self._single(forged, jev.PREDICATE, "p")

    def test_answered_requires_an_answer(self):
        forged = jev.DecisionOutcome(
            question_id="p", question_kind=jev.PREDICATE, outcome="answered", answer=None
        )
        with self.assertRaises(dr.DecisionRecordValidationError):
            self._single(forged, jev.PREDICATE, "p")

    def test_partial_error_may_carry_a_partial_answer(self):
        forged = jev.DecisionOutcome(
            question_id="p",
            question_kind=jev.PREDICATE,
            outcome="partial_error",
            answer=predicate_answer("0.4"),
            per_question_error=jev.PerQuestionError(
                question_id="p", reason="provider_error", message_redacted="static"
            ),
            diagnostic=jev.Diagnostic(code="c", detail_redacted="d", raised_at=RAISED_AT),
        )
        mapping = self._single(forged, jev.PREDICATE, "p")
        self.assertEqual(mapping["answer"]["probability"], "0.4")
        self.assertEqual(mapping["outcome"], "partial_error")

    def test_error_outcome_without_diagnostic_denied(self):
        forged = jev.DecisionOutcome(
            question_id="p", question_kind=jev.PREDICATE, outcome="refusal",
            per_question_error=jev.PerQuestionError(
                question_id="p", reason="provider_error", message_redacted="static"
            ),
        )
        with self.assertRaises(dr.DecisionRecordValidationError):
            self._single(forged, jev.PREDICATE, "p")


# --------------------------------------------------------------------------- #
# Box
# --------------------------------------------------------------------------- #
class BoxTest(unittest.TestCase):
    def test_box_recorded_unsupported_without_answer(self):
        box = jev.QuestionDef("bx", jev.BOX, "box?")
        result = make_result(
            {"bx": error_outcome("bx", jev.BOX, "unsupported", "unsupported_question_kind")}
        )
        mapping = project(result=result, qs=[box])[0].as_mapping()
        self.assertEqual(mapping["question_kind"], "box")
        self.assertEqual(mapping["outcome"], "unsupported")
        self.assertNotIn("answer", mapping)
        self.assertEqual(mapping["options"], [])
        self.assertEqual(mapping["levels"], [])

    def test_box_cannot_answer(self):
        box = jev.QuestionDef("bx", jev.BOX, "box?")
        forged = jev.DecisionOutcome(
            question_id="bx", question_kind=jev.BOX, outcome="answered",
            answer=predicate_answer("0.5"),
        )
        with self.assertRaises(dr.DecisionRecordValidationError):
            project(result=make_result({"bx": forged}), qs=[box])

    def test_non_box_unsupported_denied(self):
        forged = error_outcome("p", jev.PREDICATE, "unsupported", "unsupported_question_kind")
        with self.assertRaises(dr.DecisionRecordValidationError):
            project(result=make_result({"p": forged}), qs=[jev.QuestionDef("p", jev.PREDICATE, "i")])


# --------------------------------------------------------------------------- #
# Malformed values never defaulted
# --------------------------------------------------------------------------- #
class MalformedValuesTest(unittest.TestCase):
    def _project_one(self, outcome, question):
        return project(result=make_result({question.question_id: outcome}), qs=[question])

    def test_predicate_missing_probability_denied(self):
        forged = answered("p", jev.PREDICATE, jev.Answer(answer_kind="predicate", wire_type="noul"))
        with self.assertRaises(dr.DecisionRecordValidationError):
            self._project_one(forged, jev.QuestionDef("p", jev.PREDICATE, "i"))

    def test_score_outside_levels_denied(self):
        q = jev.QuestionDef("s", jev.SCORE, "i", levels=("lo", "hi"))
        forged = answered("s", jev.SCORE, score_answer("9"))
        with self.assertRaises(dr.DecisionRecordValidationError):
            self._project_one(forged, q)

    def test_choice_outside_options_denied(self):
        q = jev.QuestionDef("c", jev.CHOICE, "i", options=("a", "b"))
        forged = answered("c", jev.CHOICE, choice_answer("zzz"))
        with self.assertRaises(dr.DecisionRecordValidationError):
            self._project_one(forged, q)

    def test_nonfinite_probability_denied(self):
        forged = answered("p", jev.PREDICATE, predicate_answer("NaN"))
        with self.assertRaises(dr.DecisionRecordValidationError):
            self._project_one(forged, jev.QuestionDef("p", jev.PREDICATE, "i"))

    def test_float_probability_denied(self):
        forged = answered(
            "p", jev.PREDICATE,
            jev.Answer(answer_kind="predicate", wire_type="noul", probability=0.83),
        )
        with self.assertRaises(dr.DecisionRecordValidationError):
            self._project_one(forged, jev.QuestionDef("p", jev.PREDICATE, "i"))

    def test_undeclared_probability_key_denied(self):
        q = jev.QuestionDef("c", jev.CHOICE, "i", options=("a", "b"))
        forged = answered(
            "c", jev.CHOICE,
            choice_answer("a", probabilities={"a": "0.5", "undeclared": "0.5"}),
        )
        with self.assertRaises(dr.DecisionRecordValidationError):
            self._project_one(forged, q)

    def test_bad_legend_key_denied(self):
        q = jev.QuestionDef("s", jev.SCORE, "i", levels=("lo", "hi"))
        forged = answered("s", jev.SCORE, score_answer("0", legend={"9": "oops"}))
        with self.assertRaises(dr.DecisionRecordValidationError):
            self._project_one(forged, q)

    def test_answer_kind_mismatch_denied(self):
        forged = answered("p", jev.PREDICATE, choice_answer("a"))
        with self.assertRaises(dr.DecisionRecordValidationError):
            self._project_one(forged, jev.QuestionDef("p", jev.PREDICATE, "i"))


# --------------------------------------------------------------------------- #
# Refs and versions
# --------------------------------------------------------------------------- #
class RefsAndVersionsTest(unittest.TestCase):
    def test_foreign_target_project_denied(self):
        bad = {"project_id": "proj_B", "artifact_id": "candidate_17", "revision_id": "rev_3"}
        with self.assertRaises(dr.DecisionRecordBindingError):
            project(target=bad)

    def test_cross_project_criteria_ref_denied(self):
        refs = refs_for(questions(), "criteria", "crit_rev")
        refs["study_design"] = {
            "project_id": "proj_B",
            "artifact_id": "criteria",
            "revision_id": "c1",
        }
        with self.assertRaises(dr.DecisionRecordBindingError):
            project(criteria_ref=refs)

    def test_missing_ref_key_denied(self):
        with self.assertRaises(dr.DecisionRecordBindingError):
            project(question_def_ref={"project_id": PROJECT, "artifact_id": "question_def"})

    def test_versions_adapter_unknown_denied(self):
        with self.assertRaises(dr.DecisionRecordBindingError):
            project(versions=versions(adapter={"state": "unknown"}))

    def test_versions_route_not_direct_call_denied(self):
        with self.assertRaises(dr.DecisionRecordBindingError):
            project(versions=versions(route="opencode_session"))

    def test_versions_missing_field_denied(self):
        with self.assertRaises(dr.DecisionRecordBindingError):
            project(versions={"context_kind": "model_derived"})

    def test_consumed_rubric_must_be_known(self):
        with self.assertRaises(dr.DecisionRecordBindingError):
            project(versions=versions(rubric={"state": "unknown"}), consumed=("rubric",))

    def test_consumed_rubric_known_is_recorded(self):
        records = project(
            versions=versions(parser={"state": "known", "ref": {"version": "parser/1", "digest": "e" * 64}}),
            consumed=("rubric", "parser"),
        )
        self.assertEqual(
            records[0].as_mapping()["consumed_versions"],
            ["adapter", "model", "parser", "prompt", "rubric"],
        )

    def test_bad_consumed_slot_denied(self):
        with self.assertRaises(dr.DecisionRecordBindingError):
            project(consumed=("nope",))


# --------------------------------------------------------------------------- #
# Cost and ledger
# --------------------------------------------------------------------------- #
class CostAndLedgerTest(unittest.TestCase):
    def test_cost_exact_amount_and_dispatch_ref(self):
        cost = project()[0].as_mapping()["cost"]
        self.assertEqual(cost["state"], "known")
        self.assertEqual(cost["amount_usd"], "0.00002037")
        self.assertEqual(cost["currency"], "USD")
        self.assertEqual(cost["accounting_bucket"], "direct_call")
        self.assertEqual(cost["dispatch_ref"], DISPATCH)
        self.assertEqual(cost["reserve_usd"], "0.25")

    def test_unknown_cost_is_not_zero_and_retains_reserve(self):
        result = make_result(valid_outcomes(), cost=make_cost(state="unknown"))
        cost = project(result=result)[0].as_mapping()["cost"]
        self.assertEqual(cost["state"], "unknown")
        self.assertNotIn("amount_usd", cost)
        self.assertEqual(cost["reserve_usd"], "0.25")

    def test_not_incurred_has_no_amount(self):
        result = make_result(valid_outcomes(), cost=make_cost(state="not_incurred"))
        cost = project(result=result)[0].as_mapping()["cost"]
        self.assertEqual(cost["state"], "not_incurred")
        self.assertNotIn("amount_usd", cost)

    def test_submitted_dispatch_requires_reserve(self):
        bad = forge_cost(state="unknown", amount_usd=None, reserve_usd=None, reserve_retained=True)
        with self.assertRaises(dr.DecisionRecordValidationError):
            project(result=make_result(valid_outcomes(), cost=bad))

    def test_wrong_bucket_denied(self):
        bad = forge_cost(route="opencode_session", accounting_bucket="session_statistics")
        with self.assertRaises(dr.DecisionRecordValidationError):
            project(result=make_result(valid_outcomes(), cost=bad))

    def test_two_questions_share_cost_counted_once(self):
        records = project()
        self.assertGreaterEqual(len(records), 2)
        charges = dr.ledger_charges(records)
        self.assertEqual(len(charges), 1)
        self.assertEqual(charges[0]["amount_usd"], "0.00002037")
        self.assertEqual(charges[0]["dispatch_ref"], DISPATCH)


# --------------------------------------------------------------------------- #
# Bindings and request hash
# --------------------------------------------------------------------------- #
class BindingsAndHashTest(unittest.TestCase):
    def _bindings(self):
        out = {}
        for question in questions():
            out[question.question_id] = dr.QuestionBinding(
                question_id=question.question_id,
                question_kind=question.question_kind,
                question_def_digest=dr.question_descriptor_digest(question),
                options=tuple(question.options),
                levels=tuple(question.levels),
            )
        return out

    def test_matching_bindings_accepted(self):
        records = project(bindings=self._bindings())
        self.assertEqual(len(records), 3)

    def test_stale_binding_digest_denied(self):
        bindings = self._bindings()
        first = questions()[0]
        bindings[first.question_id] = dataclasses.replace(
            bindings[first.question_id], question_def_digest="0" * 64
        )
        with self.assertRaises(dr.DecisionRecordBindingError):
            project(bindings=bindings)

    def test_binding_kind_mismatch_denied(self):
        bindings = self._bindings()
        first = questions()[0]
        bindings[first.question_id] = dataclasses.replace(
            bindings[first.question_id], question_kind=jev.CHOICE
        )
        with self.assertRaises(dr.DecisionRecordBindingError):
            project(bindings=bindings)

    def test_missing_binding_denied(self):
        bindings = self._bindings()
        del bindings["study_design"]
        with self.assertRaises(dr.DecisionRecordBindingError):
            project(bindings=bindings)

    def test_extra_binding_denied(self):
        bindings = self._bindings()
        bindings["undeclared"] = dr.QuestionBinding("undeclared", jev.PREDICATE, "0" * 64)
        with self.assertRaises(dr.DecisionRecordBindingError):
            project(bindings=bindings)

    def test_stale_request_hash_denied(self):
        with self.assertRaises(dr.DecisionRecordBindingError):
            project(expected_request_sha256="0" * 64)

    def test_matching_request_hash_accepted(self):
        records = project(expected_request_sha256="f" * 64)
        self.assertEqual(records[0].as_mapping()["request_sha256"], "f" * 64)

    def test_non_hex_request_hash_denied(self):
        with self.assertRaises(dr.DecisionRecordBindingError):
            project(expected_request_sha256="not-a-hash")


# --------------------------------------------------------------------------- #
# Result envelope coverage
# --------------------------------------------------------------------------- #
class ResultEnvelopeTest(unittest.TestCase):
    def test_not_submitted_denied(self):
        result = make_result({}, submitted=False, calls_attempted=0, status="blocked", cost=make_cost(state="not_incurred"))
        with self.assertRaises(dr.DecisionRecordProjectionError):
            project(result=result)

    def test_missing_outcome_denied(self):
        outcomes = valid_outcomes()
        del outcomes["study_design"]
        with self.assertRaises(dr.DecisionRecordProjectionError):
            project(result=make_result(outcomes))

    def test_extra_outcome_denied(self):
        outcomes = valid_outcomes()
        outcomes["undeclared"] = answered("undeclared", jev.PREDICATE, predicate_answer())
        with self.assertRaises(dr.DecisionRecordProjectionError):
            project(result=make_result(outcomes))

    def test_foreign_endpoint_denied(self):
        result = make_result(valid_outcomes(), endpoint="https://evil.example/decisions")
        with self.assertRaises(dr.DecisionRecordProjectionError):
            project(result=result)

    def test_foreign_model_denied(self):
        result = make_result(valid_outcomes(), model="typesafe/other")
        with self.assertRaises(dr.DecisionRecordProjectionError):
            project(result=result)

    def test_outcome_question_kind_mismatch_denied(self):
        outcomes = valid_outcomes()
        outcomes["is_original_report"] = answered(
            "is_original_report", jev.CHOICE, choice_answer("randomised")
        )
        with self.assertRaises(dr.DecisionRecordValidationError):
            project(result=make_result(outcomes))


# --------------------------------------------------------------------------- #
# Forged / pathological inputs
# --------------------------------------------------------------------------- #
class ForgedInputTest(unittest.TestCase):
    def _project_forged(self, question):
        result = make_result({"is_original_report": answered(
            "is_original_report", jev.PREDICATE, predicate_answer())})
        return project(result=result, qs=[question])

    def test_forged_cyclic_criteria_denied(self):
        criteria = {"true": None, "false": "x"}
        criteria["true"] = criteria  # circular
        with self.assertRaises(dr.DecisionRecordValidationError):
            self._project_forged(forge_question(criteria=criteria))

    def test_forged_deep_criteria_denied(self):
        nested = "leaf"
        for _ in range(200):
            nested = [nested]
        with self.assertRaises(dr.DecisionRecordValidationError):
            self._project_forged(forge_question(criteria={"true": nested, "false": "x"}))

    def test_forged_nonfinite_criteria_denied(self):
        with self.assertRaises(dr.DecisionRecordValidationError):
            self._project_forged(forge_question(criteria={"true": float("nan"), "false": "x"}))

    def test_mutable_criteria_alias_does_not_drift(self):
        criteria = {"true": "yes", "false": "no"}
        question = jev.QuestionDef("p", jev.PREDICATE, "i", criteria=criteria)
        result = make_result({"p": answered("p", jev.PREDICATE, predicate_answer())})
        first = project(result=result, qs=[question])[0].canonical_bytes
        criteria["true"] = "MUTATED"
        second = project(result=result, qs=[question])[0].canonical_bytes
        self.assertEqual(first, second)

    def test_mutable_input_containers_do_not_drift(self):
        target = dict(TARGET)
        version_map = versions()
        result = make_result(valid_outcomes())
        first = project(result=result, target=target, versions=version_map)[0].canonical_bytes
        target["revision_id"] = "MUTATED"
        version_map["model"]["ref"]["digest"] = "0" * 64
        second = project(result=result, target=TARGET, versions=versions())[0].canonical_bytes
        self.assertEqual(first, second)


# --------------------------------------------------------------------------- #
# Stable record id
# --------------------------------------------------------------------------- #
class RecordIdTest(unittest.TestCase):
    def test_deterministic_and_content_derived(self):
        a = project()
        b = project()
        self.assertEqual([r.decision_id for r in a], [r.decision_id for r in b])
        self.assertTrue(all(r.decision_id.startswith("dec_") for r in a))

    def test_different_question_distinct(self):
        ids = {r.question_id: r.decision_id for r in project()}
        self.assertEqual(len(set(ids.values())), len(ids))

    def test_different_dispatch_not_collapsed(self):
        first = project(dispatch_ref="d1")[0].decision_id
        second = project(dispatch_ref="d2")[0].decision_id
        self.assertNotEqual(first, second)

    def test_different_task_context_not_collapsed(self):
        first = project(task_context="t1")[0].decision_id
        second = project(task_context="t2")[0].decision_id
        self.assertNotEqual(first, second)


# --------------------------------------------------------------------------- #
# Static errors
# --------------------------------------------------------------------------- #
class StaticErrorTest(unittest.TestCase):
    CANARY = "SYNTHETIC_CANARY_LEAK"

    def test_version_error_is_static(self):
        bad = versions(model={"state": self.CANARY})
        try:
            project(versions=bad)
        except dr.DecisionRecordError as exc:
            self.assertNotIn(self.CANARY, str(exc))
        else:  # pragma: no cover
            self.fail("bad version accepted")

    def test_ref_error_is_static(self):
        refs = refs_for(questions(), "question_def", "qd_rev")
        refs["is_original_report"] = {
            "project_id": self.CANARY,
            "artifact_id": "x",
            "revision_id": "r",
        }
        try:
            project(question_def_ref=refs)
        except dr.DecisionRecordError as exc:
            self.assertNotIn(self.CANARY, str(exc))
        else:  # pragma: no cover
            self.fail("bad ref accepted")

    def test_malformed_answer_error_is_static(self):
        forged = answered(
            "p", jev.PREDICATE,
            jev.Answer(answer_kind="predicate", wire_type="noul", probability=self.CANARY),
        )
        try:
            project(result=make_result({"p": forged}),
                    qs=[jev.QuestionDef("p", jev.PREDICATE, "i")])
        except dr.DecisionRecordError as exc:
            self.assertNotIn(self.CANARY, str(exc))
        else:  # pragma: no cover
            self.fail("bad answer accepted")


# --------------------------------------------------------------------------- #
# Mapping validator
# --------------------------------------------------------------------------- #
class MappingValidatorTest(unittest.TestCase):
    def test_rejects_unknown_field(self):
        mapping = project()[0].as_mapping()
        mapping["extra"] = 1
        with self.assertRaises(dr.DecisionRecordValidationError):
            dr.validate_decision_record_mapping(mapping)

    def test_rejects_tampered_authority_flags(self):
        mapping = project()[0].as_mapping()
        mapping["is_exclusion"] = True
        with self.assertRaises(dr.DecisionRecordValidationError):
            dr.validate_decision_record_mapping(mapping)

    def test_rejects_tampered_cost(self):
        mapping = project()[0].as_mapping()
        mapping["cost"]["amount_usd"] = "9.99"
        # A valid decimal is accepted by the structural validator; a foreign
        # bucket/route is not.
        mapping["cost"]["accounting_bucket"] = "session_statistics"
        with self.assertRaises(dr.DecisionRecordValidationError):
            dr.validate_decision_record_mapping(mapping)

    def test_rejects_tampered_answer(self):
        mapping = project()[1].as_mapping()
        mapping["answer"]["choice"] = "not-an-option"
        with self.assertRaises(dr.DecisionRecordValidationError):
            dr.validate_decision_record_mapping(mapping)

    def _omitted_mapping(self):
        # A canonical ``omitted`` record with a persisted reason, built from a
        # projected record (the decision id binds only the task inputs).
        mapping = project()[0].as_mapping()
        del mapping["answer"]
        mapping["outcome"] = "omitted"
        mapping["per_question_error"] = {
            "question_id": "is_original_report",
            "reason": "missing_answer",
            "message_redacted": "static",
        }
        mapping["diagnostics"] = {
            "code": "c",
            "detail_redacted": "d",
            "raised_at": RAISED_AT,
        }
        return mapping

    def test_adapter_outcome_consistent_relation_accepted(self):
        mapping = self._omitted_mapping()
        mapping["adapter_outcome"] = "partial_error"
        self.assertEqual(dr.validate_decision_record_mapping(mapping), mapping)

    def test_adapter_outcome_on_non_omitted_denied(self):
        mapping = project()[0].as_mapping()  # answered
        mapping["adapter_outcome"] = "partial_error"
        with self.assertRaises(dr.DecisionRecordValidationError):
            dr.validate_decision_record_mapping(mapping)

    def test_adapter_outcome_wrong_value_denied(self):
        mapping = self._omitted_mapping()
        mapping["adapter_outcome"] = "uncertain"
        with self.assertRaises(dr.DecisionRecordValidationError):
            dr.validate_decision_record_mapping(mapping)

    def test_adapter_outcome_unknown_literal_denied(self):
        mapping = self._omitted_mapping()
        mapping["adapter_outcome"] = "nope"
        with self.assertRaises(dr.DecisionRecordValidationError):
            dr.validate_decision_record_mapping(mapping)

    def test_omitted_without_reason_denied_by_validator(self):
        mapping = self._omitted_mapping()
        del mapping["per_question_error"]
        del mapping["diagnostics"]
        with self.assertRaises(dr.DecisionRecordValidationError):
            dr.validate_decision_record_mapping(mapping)

    def test_returns_no_list_alias(self):
        # The validator promises a fresh plain copy; every list descendant
        # (options/levels/consumed_versions) must be copied, not aliased.
        mapping = project()[1].as_mapping()  # choice question -> options/levels lists
        returned = dr.validate_decision_record_mapping(mapping)
        for key in ("options", "levels", "consumed_versions"):
            self.assertIsInstance(returned[key], list)
            self.assertIsNot(returned[key], mapping[key])
        returned["options"].append("MUTATED")
        returned["consumed_versions"].append("MUTATED")
        self.assertNotIn("MUTATED", mapping["options"])
        self.assertNotIn("MUTATED", mapping["consumed_versions"])

    def test_returns_no_nested_list_alias(self):
        # Nested lists are copied recursively too (not only Mapping/tuple).
        source = {"a": [1, [2, 3]], "b": (4, 5)}
        copied = dr._thaw(source)
        self.assertEqual(copied, {"a": [1, [2, 3]], "b": [4, 5]})
        self.assertIsNot(copied["a"], source["a"])
        self.assertIsNot(copied["a"][1], source["a"][1])
        copied["a"][1].append(99)
        self.assertEqual(source["a"][1], [2, 3])


# --------------------------------------------------------------------------- #
# Boundary: an untrusted mapping is bounded before any walk/thaw/freeze
# --------------------------------------------------------------------------- #
class BoundaryMappingTest(unittest.TestCase):
    def test_cyclic_mapping_denied_without_recursion_error(self):
        mapping = project()[0].as_mapping()
        mapping["answer"] = mapping  # circular
        with self.assertRaises(dr.DecisionRecordValidationError):
            dr.validate_decision_record_mapping(mapping)

    def test_deep_mapping_denied_without_recursion_error(self):
        mapping = project()[0].as_mapping()
        nested = "leaf"
        for _ in range(200):
            nested = [nested]
        mapping["answer"] = nested
        with self.assertRaises(dr.DecisionRecordValidationError):
            dr.validate_decision_record_mapping(mapping)

    def test_oversize_mapping_denied(self):
        mapping = project()[0].as_mapping()
        mapping["answer"] = {
            "answer_kind": "predicate",
            "wire_type": "noul",
            "probability": "0" * (dr.MAX_DECISION_RECORD_BYTES + 1),
        }
        with self.assertRaises(dr.DecisionRecordValidationError):
            dr.validate_decision_record_mapping(mapping)

    def test_non_portable_mapping_denied(self):
        mapping = project()[0].as_mapping()
        mapping["provider"]["input_tokens"] = Decimal("1")
        with self.assertRaises(dr.DecisionRecordValidationError):
            dr.validate_decision_record_mapping(mapping)

    def test_hostile_mapping_never_leaks_recursion_error(self):
        # The canonical/size bound runs before any key walk, so a hostile Mapping
        # whose iteration would raise is converted to a static typed error.
        class _HostileMapping(Mapping):
            def __getitem__(self, key):
                raise RecursionError("hostile")

            def __iter__(self):
                raise RecursionError("hostile")

            def __len__(self):
                return 0

        with self.assertRaises(dr.DecisionRecordValidationError):
            dr.validate_decision_record_mapping(_HostileMapping())


# --------------------------------------------------------------------------- #
# Bounded numerics: a compact Decimal/int can never expand before validation
# --------------------------------------------------------------------------- #
class NumericBoundTest(unittest.TestCase):
    def _single_forged(self, probability):
        forged = answered(
            "p",
            jev.PREDICATE,
            jev.Answer(answer_kind="predicate", wire_type="noul", probability=probability),
        )
        return project(
            result=make_result({"p": forged}),
            qs=[jev.QuestionDef("p", jev.PREDICATE, "i")],
        )

    def test_tiny_zero_decimal_denied(self):
        with self.assertRaises(dr.DecisionRecordValidationError):
            self._single_forged(Decimal("0E-1000000"))

    def test_tiny_nonzero_decimal_denied(self):
        with self.assertRaises(dr.DecisionRecordValidationError):
            self._single_forged(Decimal("1e-1000000"))

    def test_huge_exponent_decimal_denied(self):
        with self.assertRaises(dr.DecisionRecordValidationError):
            self._single_forged(Decimal("1E+1000000"))

    def test_num_to_str_does_not_expand_bounded_away_decimals(self):
        self.assertEqual(dr._num_to_str(Decimal("0E-1000000")), Decimal("0E-1000000"))
        self.assertEqual(dr._num_to_str(Decimal("1e-1000000")), Decimal("1e-1000000"))
        self.assertEqual(dr._num_to_str(Decimal("0.83")), "0.83")

    def test_num_to_str_guards_huge_int(self):
        huge = 10 ** 5000
        self.assertIs(dr._num_to_str(huge), huge)
        self.assertEqual(dr._num_to_str(7), "7")


# --------------------------------------------------------------------------- #
# Per-question refs (C02 §6.2: one QuestionDef/criteria_ref per record)
# --------------------------------------------------------------------------- #
class PerQuestionRefTest(unittest.TestCase):
    def test_multi_question_single_ref_denied(self):
        with self.assertRaises(dr.DecisionRecordBindingError):
            project(question_def_ref=QDEF_REF)

    def test_multi_question_single_criteria_ref_denied(self):
        with self.assertRaises(dr.DecisionRecordBindingError):
            project(criteria_ref=CRIT_REF)

    def test_per_question_refs_recorded(self):
        qs = questions()
        refs = refs_for(qs, "question_def", "qd_rev")
        records = project(question_def_ref=refs)
        for record in records:
            self.assertEqual(record.as_mapping()["question_def_ref"], refs[record.question_id])

    def test_per_question_refs_missing_coverage_denied(self):
        refs = refs_for(questions(), "question_def", "qd_rev")
        del refs["study_design"]
        with self.assertRaises(dr.DecisionRecordBindingError):
            project(question_def_ref=refs)

    def test_per_question_refs_extra_coverage_denied(self):
        refs = refs_for(questions(), "question_def", "qd_rev")
        refs["undeclared"] = {"project_id": PROJECT, "artifact_id": "x", "revision_id": "r"}
        with self.assertRaises(dr.DecisionRecordBindingError):
            project(question_def_ref=refs)

    def test_single_ref_allowed_for_single_question(self):
        q = jev.QuestionDef("p", jev.PREDICATE, "i")
        record = project(
            result=make_result({"p": answered("p", jev.PREDICATE, predicate_answer())}),
            qs=[q],
            question_def_ref=QDEF_REF,
            criteria_ref=CRIT_REF,
        )[0]
        self.assertEqual(
            record.as_mapping()["question_def_ref"],
            {"project_id": PROJECT, "artifact_id": "question_def", "revision_id": "qd_rev_1"},
        )


# --------------------------------------------------------------------------- #
# Bindings + request digest are required for an actual projection
# --------------------------------------------------------------------------- #
class BindingsRequiredTest(unittest.TestCase):
    def test_projection_requires_bindings_and_request_digest(self):
        base = dict(
            project_id=PROJECT,
            target=TARGET,
            question_def_ref=refs_for(questions(), "question_def", "qd_rev"),
            criteria_ref=refs_for(questions(), "criteria", "crit_rev"),
            dispatch_ref=DISPATCH,
            task_context=TASK_CONTEXT,
            versions=versions(),
        )
        with self.assertRaises(TypeError):
            dr.project_decision_records(make_result(valid_outcomes()), questions(), **base)


# --------------------------------------------------------------------------- #
# Persisted task context + re-derived decision id
# --------------------------------------------------------------------------- #
class TaskContextAndIdTest(unittest.TestCase):
    def test_task_context_is_persisted(self):
        self.assertEqual(project()[0].as_mapping()["task_context"], TASK_CONTEXT)

    def test_validator_requires_task_context(self):
        mapping = project()[0].as_mapping()
        del mapping["task_context"]
        with self.assertRaises(dr.DecisionRecordValidationError):
            dr.validate_decision_record_mapping(mapping)

    def test_validator_rejects_tampered_task_context(self):
        mapping = project()[0].as_mapping()
        mapping["task_context"] = "other_task"
        with self.assertRaises(dr.DecisionRecordValidationError):
            dr.validate_decision_record_mapping(mapping)

    def test_validator_rejects_invented_decision_id(self):
        mapping = project()[0].as_mapping()
        mapping["decision_id"] = "dec_" + "0" * 32
        with self.assertRaises(dr.DecisionRecordValidationError):
            dr.validate_decision_record_mapping(mapping)


# --------------------------------------------------------------------------- #
# Presence union / envelope type strictness
# --------------------------------------------------------------------------- #
class PresenceEnvelopeTest(unittest.TestCase):
    def test_validator_rejects_empty_request_sha(self):
        mapping = project()[0].as_mapping()
        mapping["request_sha256"] = ""
        with self.assertRaises(dr.DecisionRecordValidationError):
            dr.validate_decision_record_mapping(mapping)

    def test_validator_rejects_missing_reserve(self):
        mapping = project()[0].as_mapping()
        del mapping["cost"]["reserve_usd"]
        with self.assertRaises(dr.DecisionRecordValidationError):
            dr.validate_decision_record_mapping(mapping)

    def test_float_calls_attempted_denied(self):
        with self.assertRaises(dr.DecisionRecordProjectionError):
            project(result=make_result(valid_outcomes(), calls_attempted=1.0))

    def test_bool_schema_version_denied(self):
        mapping = project()[0].as_mapping()
        mapping["schema_version"] = True
        with self.assertRaises(dr.DecisionRecordValidationError):
            dr.validate_decision_record_mapping(mapping)

    def test_float_schema_version_denied(self):
        # 1.0 == 1 but is not an actual int; a versioned representation must be
        # an exact integer literal, never a float.
        mapping = project()[0].as_mapping()
        mapping["schema_version"] = 1.0
        with self.assertRaises(dr.DecisionRecordValidationError):
            dr.validate_decision_record_mapping(mapping)

    def test_string_schema_version_denied(self):
        mapping = project()[0].as_mapping()
        mapping["schema_version"] = "1"
        with self.assertRaises(dr.DecisionRecordValidationError):
            dr.validate_decision_record_mapping(mapping)

    def test_int_schema_version_accepted(self):
        mapping = project()[0].as_mapping()
        self.assertEqual(mapping["schema_version"], 1)
        self.assertEqual(dr.validate_decision_record_mapping(mapping), mapping)


# --------------------------------------------------------------------------- #
# Ledger: a divergent charge for the same dispatch is a defect
# --------------------------------------------------------------------------- #
class LedgerConflictTest(unittest.TestCase):
    def test_divergent_charge_same_dispatch_denied(self):
        first = project()
        other = project(result=make_result(valid_outcomes(), cost=make_cost(amount="0.5")))
        self.assertEqual(first[0].dispatch_ref, other[0].dispatch_ref)
        with self.assertRaises(dr.DecisionRecordValidationError):
            dr.ledger_charges((first[0], other[0]))

    def test_identical_charge_same_dispatch_counted_once(self):
        records = project()
        self.assertEqual(len(dr.ledger_charges(records)), 1)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
