"""Focused S12-SCHEMA regression suite; pure rules, no model calls, no storage.

These assert only the mechanical Step 12 obligations (assertion-only objections
rejected with a reason, false/fabricated objections rejected, well-based
objections confirmed, repair regressions caught, secondary support explicitly
flagged and never primary-verified, stale checks invalidated on change, and
blocking state that never auto-passes).  They cannot establish scientific truth.
"""

import copy
import json
import unittest

from review_article_agent.drafting.schema import draft_example, drafting_example_package
from review_article_agent.review.schema import (
    FACTUAL_KINDS,
    ROUND_LIMITS,
    SEVERITIES,
    ReviewError,
    check_repair,
    evaluate,
    fact_check,
    normalize_finding,
    stale_checks,
    writer_response,
)


def _fixtures():
    package = drafting_example_package()
    draft = draft_example(package=package)
    return draft, package


def _sentence(draft, paragraph_id, intent_id):
    for paragraph in draft["paragraphs"]:
        if paragraph["paragraph_id"] != paragraph_id:
            continue
        for sentence in paragraph["sentences"]:
            if sentence["intent_id"] == intent_id:
                return sentence
    raise AssertionError("sentence not found")


def _finding(**overrides):
    raw = {
        "target": {"paragraph_id": "P1", "intent_id": "I1"},
        "kind": "unsupported_claim",
        "severity": "major",
        "objection": "The claim is not supported by the cited source.",
    }
    raw.update(overrides)
    return raw


class NormalizeFindingTests(unittest.TestCase):
    def test_factual_objection_by_assertion_only_rejected(self):
        draft, package = _fixtures()
        finding = normalize_finding(_finding(), draft=draft, package=package, stage="critique", round_no=0)
        self.assertEqual(finding["disposition"], "rejected")
        self.assertIn("assertion", finding["reason"])
        self.assertEqual(finding["basis"], None)

    def test_fabricated_basis_rejected(self):
        draft, package = _fixtures()
        raw = _finding(
            basis={"finding_key": "A10:F1", "quote": "text that appears nowhere in the source"}
        )
        finding = normalize_finding(raw, draft=draft, package=package, stage="critique", round_no=0)
        self.assertEqual(finding["disposition"], "rejected")
        self.assertIn("fabricated", finding["reason"])

    def test_false_numeric_objection_rejected(self):
        draft, package = _fixtures()
        sentence = _sentence(draft, "P1", "I1")
        sentence["text"] = "tumor volume was reduced by 12.5 mm3"
        sentence["citation_keys"] = ["CIT-A10"]
        raw = _finding(
            kind="numeric_mismatch",
            basis={"finding_key": "A10:F1", "quote": "tumor volume was reduced in the treated cohort"},
        )
        finding = normalize_finding(raw, draft=draft, package=package, stage="critique", round_no=0)
        self.assertEqual(finding["disposition"], "rejected")
        self.assertIn("false objection", finding["reason"])

    def test_well_based_objection_confirmed(self):
        draft, package = _fixtures()
        raw = _finding(
            basis={"finding_key": "A10:F1", "quote": "tumor volume was reduced in the treated cohort"}
        )
        finding = normalize_finding(raw, draft=draft, package=package, stage="critique", round_no=0)
        self.assertEqual(finding["disposition"], "confirmed")
        self.assertEqual(finding["basis"]["finding_key"], "A10:F1")

    def test_missing_check_stays_open(self):
        draft, package = _fixtures()
        raw = _finding(missing_check="Check supplementary Table S1 for the numeric value")
        finding = normalize_finding(raw, draft=draft, package=package, stage="critique", round_no=0)
        self.assertEqual(finding["disposition"], "open")

    def test_deterministic_finding_id(self):
        draft, package = _fixtures()
        a = normalize_finding(_finding(), draft=draft, package=package, stage="critique", round_no=0)
        b = normalize_finding(_finding(), draft=draft, package=package, stage="critique", round_no=0)
        self.assertEqual(a["finding_id"], b["finding_id"])

    def test_malformed_input_raises(self):
        draft, package = _fixtures()
        with self.assertRaises(ReviewError):
            normalize_finding(_finding(kind="not_a_kind"), draft=draft, package=package,
                              stage="critique", round_no=0)
        with self.assertRaises(ReviewError):
            normalize_finding(_finding(severity="catastrophic"), draft=draft, package=package,
                              stage="critique", round_no=0)
        with self.assertRaises(ReviewError):
            normalize_finding(_finding(target={"paragraph_id": "PX", "intent_id": "I1"}),
                              draft=draft, package=package, stage="critique", round_no=0)


class WriterResponseTests(unittest.TestCase):
    def _confirmed(self, severity="major"):
        draft, package = _fixtures()
        raw = _finding(
            severity=severity,
            basis={"finding_key": "A10:F1", "quote": "tumor volume was reduced in the treated cohort"},
        )
        return normalize_finding(raw, draft=draft, package=package, stage="critique", round_no=0)

    def test_reject_confirmed_major_is_disputed(self):
        finding = writer_response(self._confirmed("major"), accept=False, reason="The source does support it.")
        self.assertEqual(finding["disposition"], "disputed")

    def test_reject_confirmed_minor_is_rejected(self):
        finding = writer_response(self._confirmed("minor"), accept=False, reason="Preference retained.")
        self.assertEqual(finding["disposition"], "rejected")

    def test_accept_marks_fixed(self):
        finding = writer_response(self._confirmed("major"), accept=True, reason="Rewritten.")
        self.assertEqual(finding["disposition"], "fixed")

    def test_reject_without_reason_raises(self):
        with self.assertRaises(ReviewError):
            writer_response(self._confirmed("major"), accept=False, reason="   ")


class CheckRepairTests(unittest.TestCase):
    def test_qualifier_removed(self):
        _, package = _fixtures()
        result = check_repair(
            {"text": "A shared upstream mechanism may explain the effect.", "citation_keys": ["CIT-A10"]},
            {"text": "A shared upstream mechanism explains the effect.", "citation_keys": ["CIT-A10"]},
            package=package,
        )
        self.assertIn("qualifier_removed", result["violations"])

    def test_invented_number(self):
        _, package = _fixtures()
        result = check_repair(
            {"text": "tumor volume was reduced by 12.5 mm3", "citation_keys": ["CIT-A10"]},
            {"text": "tumor volume was reduced by 40 mm3", "citation_keys": ["CIT-A10"]},
            package=package,
        )
        self.assertIn("invented_number", result["violations"])

    def test_species_change(self):
        _, package = _fixtures()
        result = check_repair(
            {"text": "a reduction in the treated mouse cohort", "citation_keys": ["CIT-A10"]},
            {"text": "a reduction in the treated human cohort", "citation_keys": ["CIT-A10"]},
            package=package,
        )
        self.assertIn("species_changed", result["violations"])

    def test_unverified_citation_is_violation(self):
        _, package = _fixtures()
        result = check_repair(
            {"text": "the effect was reproduced", "citation_keys": ["CIT-A10"]},
            {"text": "the effect was reproduced", "citation_keys": ["CIT-A10", "CIT-NEW"]},
            package=package,
        )
        self.assertIn("unverified_citation", result["violations"])
        self.assertFalse(result["reopen_checks"])

    def test_corpus_citation_reopens_without_violation(self):
        _, package = _fixtures()
        result = check_repair(
            {"text": "the effect was reproduced", "citation_keys": ["CIT-A10"]},
            {"text": "the effect was reproduced", "citation_keys": ["CIT-A10", "CIT-B7"]},
            package=package,
        )
        self.assertEqual(result["violations"], [])
        self.assertTrue(result["reopen_checks"])


class FactCheckTests(unittest.TestCase):
    def test_secondary_support_and_no_primary_verified(self):
        draft, package = _fixtures()
        checks = fact_check(draft, package)
        secondary = [c for c in checks if c["intent_id"] == "I2"][0]
        self.assertEqual(secondary["verdict"], "supported")
        self.assertEqual(secondary["support_label"], "secondary_support")
        self.assertEqual(secondary["origin_flag"], "unresolved_secondary_origin")
        self.assertNotIn("primary_verified", json.dumps(checks))

    def test_primary_check_is_source_supported_pending_origin(self):
        draft, package = _fixtures()
        checks = fact_check(draft, package)
        primary = [c for c in checks if c["intent_id"] == "I1"][0]
        self.assertEqual(primary["support_label"], "source_supported")
        self.assertEqual(primary["origin_status"], "pending_origin_tracing")

    def test_proposed_synthesis_not_applicable(self):
        draft, package = _fixtures()
        checks = fact_check(draft, package)
        proposed = [c for c in checks if c["intent_id"] == "I3"][0]
        self.assertEqual(proposed["verdict"], "not_applicable")

    def test_numeric_mismatch_detected(self):
        draft, package = _fixtures()
        sentence = _sentence(draft, "P1", "I1")
        sentence["text"] = "tumor volume was reduced by 99 mm3"
        sentence["citation_keys"] = ["CIT-A10", "CIT-B7"]
        checks = fact_check(draft, package)
        self.assertEqual([c for c in checks if c["intent_id"] == "I1"][0]["verdict"], "numeric_mismatch")

    def test_changed_sentence_or_evidence_makes_check_stale(self):
        draft, package = _fixtures()
        checks = fact_check(draft, package)
        target_id = "fc:P1:I1"
        self.assertEqual(stale_checks(checks, draft, package), [])

        changed_draft = copy.deepcopy(draft)
        _sentence(changed_draft, "P1", "I1")["text"] = "an edited claim"
        self.assertIn(target_id, stale_checks(checks, changed_draft, package))

        changed_package = copy.deepcopy(package)
        for entry in changed_package["evidence"]:
            if entry["finding_key"] == "A10:F1":
                entry["original_passage"]["text"] = "a rewritten passage"
        self.assertIn(target_id, stale_checks(checks, draft, changed_package))


class EvaluateTests(unittest.TestCase):
    def test_minor_style_finding_remaining_is_complete(self):
        draft, package = _fixtures()
        checks = fact_check(draft, package)
        finding = normalize_finding(
            _finding(kind="style", severity="style", objection="Prefer a shorter sentence."),
            draft=draft, package=package, stage="critique", round_no=0,
        )
        self.assertEqual(finding["disposition"], "confirmed")
        result = evaluate([finding], checks, {})
        self.assertEqual(result["status"], "complete")
        self.assertIn(finding["finding_id"], result["unresolved"])

    def test_confirmed_major_needs_repair_then_blocked_when_exhausted(self):
        draft, package = _fixtures()
        checks = fact_check(draft, package)
        finding = normalize_finding(
            _finding(basis={"finding_key": "A10:F1", "quote": "tumor volume was reduced in the treated cohort"}),
            draft=draft, package=package, stage="critique", round_no=0,
        )
        needs_repair = evaluate([finding], checks, {})
        self.assertEqual(needs_repair["status"], "needs_repair")
        blocked = evaluate([finding], checks, {"critique": ROUND_LIMITS["critique"]})
        self.assertEqual(blocked["status"], "blocked")
        self.assertIsNotNone(blocked["required_human_action"])

    def test_disputed_major_blocks_for_human(self):
        draft, package = _fixtures()
        checks = fact_check(draft, package)
        finding = normalize_finding(
            _finding(basis={"finding_key": "A10:F1", "quote": "tumor volume was reduced in the treated cohort"}),
            draft=draft, package=package, stage="critique", round_no=0,
        )
        disputed = writer_response(finding, accept=False, reason="The source does support the claim.")
        result = evaluate([disputed], checks, {})
        self.assertEqual(result["status"], "blocked")
        self.assertIsNotNone(result["required_human_action"])

    def test_failed_check_blocks_when_fact_check_budget_exhausted(self):
        draft, package = _fixtures()
        sentence = _sentence(draft, "P1", "I1")
        sentence["text"] = "tumor volume was reduced by 99 mm3"
        sentence["citation_keys"] = ["CIT-A10", "CIT-B7"]
        checks = fact_check(draft, package)
        self.assertEqual(evaluate([], checks, {"fact_check": ROUND_LIMITS["fact_check"]})["status"], "blocked")
        self.assertEqual(evaluate([], checks, {})["status"], "needs_repair")


class ConstantsTests(unittest.TestCase):
    def test_constants(self):
        self.assertEqual(SEVERITIES, ("blocking", "major", "minor", "style"))
        self.assertEqual(ROUND_LIMITS, {"critique": 3, "fact_check": 2})
        self.assertIn("numeric_mismatch", FACTUAL_KINDS)


if __name__ == "__main__":
    unittest.main()
