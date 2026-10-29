"""Focused S10-EVIDENCE regression suite; no model calls, no real evidence.

These assert *structural* obligations only (evidence preservation, explicit
disposition of every finding, conservative correlation, explicit independence
adjudication, framework safeguards, ordered outline dependencies). They cannot
and do not claim scientific correctness or completeness.
"""

import copy
import json
import unittest

from review_article_agent.synthesis import (
    FRAMEWORK_CRITERIA,
    SynthesisError,
    framework_example,
    outline_example,
    synthesis_example,
    synthesis_example_cases,
    synthesis_example_payload,
    validate_framework,
    validate_outline,
    validate_synthesis,
)


def _valid():
    return validate_synthesis(synthesis_example_payload(), cases=synthesis_example_cases())


class ExampleAndDeterminismTests(unittest.TestCase):
    def test_example_is_json_portable_and_deterministic(self):
        first = _valid()
        second = _valid()
        json.dumps(first)
        self.assertEqual(json.dumps(first, sort_keys=True), json.dumps(second, sort_keys=True))

    def test_example_builders_are_isolated(self):
        cases = synthesis_example_cases()
        cases[0]["id"] = "MUTATED"
        self.assertNotEqual(synthesis_example_cases()[0]["id"], "MUTATED")

    def test_schema_version_must_be_exact_int(self):
        for bad in (True, "1", 1.0, None, 2):
            payload = synthesis_example_payload()
            payload["schema_version"] = bad
            with self.assertRaises(SynthesisError):
                validate_synthesis(payload, cases=synthesis_example_cases())


class PreserveEvidenceTests(unittest.TestCase):
    def test_all_findings_preserved_with_exact_refs(self):
        result = _valid()
        self.assertEqual([f["key"] for f in result["findings"]], ["A10:F1", "A11:F1", "B7:F1"])
        a10 = result["findings"][0]
        self.assertEqual(a10["extraction_ref"], {"project_id": "P-0001", "artifact_id": "A10", "revision_id": "e1"})
        self.assertEqual(a10["source_record"]["source_key"], "docA10")
        self.assertEqual(a10["record"]["passage_id"], "p0001")
        self.assertEqual(a10["record"]["attribution"], "direct_observation")
        self.assertEqual(a10["record"]["context"]["species"]["value"], "mouse")
        self.assertEqual(a10["record"]["limitations"]["state"], "known")

    def test_duplicate_local_finding_ids_are_namespaced(self):
        keys = [f["key"] for f in _valid()["findings"]]
        self.assertEqual(len(keys), len(set(keys)))
        self.assertIn("A10:F1", keys)
        self.assertIn("A11:F1", keys)

    def test_real_case_extra_keys_are_tolerated(self):
        cases = synthesis_example_cases()
        cases[0]["quality"] = {"blocked": False}
        cases[0]["attention"] = []
        validate_synthesis(synthesis_example_payload(), cases=cases)


class CorrelationTests(unittest.TestCase):
    def test_several_reports_of_one_dataset_are_not_independent(self):
        result = _valid()
        groups = result["correlation_groups"]
        self.assertEqual(groups[0]["member_findings"], ["A10:F1", "A11:F1"])
        self.assertEqual(groups[0]["independence"], "not_independent")
        self.assertEqual(groups[0]["basis"], "shared_underlying_study_or_dataset")
        self.assertIn("study:S1", groups[0]["underlying_refs"])

    def test_proposed_link_alone_still_correlates(self):
        # A11's report link is only ``proposed``; conservatively it still groups.
        groups = _valid()["correlation_groups"]
        self.assertIn("A11:F1", groups[0]["member_findings"])

    def test_unlinked_finding_independence_is_unknown_not_independent(self):
        groups = _valid()["correlation_groups"]
        b7 = [g for g in groups if g["member_findings"] == ["B7:F1"]][0]
        self.assertEqual(b7["independence"], "unknown")
        self.assertEqual(b7["basis"], "single_observation")


class DispositionTests(unittest.TestCase):
    def test_every_finding_must_be_dispositioned(self):
        payload = synthesis_example_payload()
        payload["dispositions"] = [d for d in payload["dispositions"] if d["finding"] != "A11:F1"]
        with self.assertRaises(SynthesisError):
            validate_synthesis(payload, cases=synthesis_example_cases())

    def test_unknown_disposition_target_refused(self):
        payload = synthesis_example_payload()
        payload["dispositions"].append({"finding": "A99:F1", "disposition": "not_used", "rationale": "x"})
        with self.assertRaises(SynthesisError):
            validate_synthesis(payload, cases=synthesis_example_cases())

    def test_null_evidence_is_retained_not_dropped(self):
        result = _valid()
        self.assertEqual(result["by_disposition"]["null_evidence"], ["A11:F1"])

    def test_statement_cannot_trace_to_non_incorporated_finding(self):
        payload = synthesis_example_payload()
        payload["statements"][0]["evidence"] = ["A11:F1", "B7:F1"]
        with self.assertRaises(SynthesisError):
            validate_synthesis(payload, cases=synthesis_example_cases())

    def test_incorporated_finding_must_be_traced_by_a_statement(self):
        payload = synthesis_example_payload()
        for entry in payload["dispositions"]:
            if entry["finding"] == "A11:F1":
                entry["disposition"] = "incorporated"
        with self.assertRaises(SynthesisError):
            validate_synthesis(payload, cases=synthesis_example_cases())

    def test_no_vote_or_score_keys(self):
        for key in ("score", "weight", "vote", "excluded"):
            payload = synthesis_example_payload()
            payload[key] = 1
            with self.assertRaises(SynthesisError):
                validate_synthesis(payload, cases=synthesis_example_cases())


class StatementTests(unittest.TestCase):
    def test_classification_enum_enforced(self):
        payload = synthesis_example_payload()
        payload["statements"][0]["classification"] = "probably_true"
        with self.assertRaises(SynthesisError):
            validate_synthesis(payload, cases=synthesis_example_cases())

    def test_approved_claim_with_missing_context_is_rejected(self):
        payload = synthesis_example_payload()
        payload["statements"][0]["status"] = "approved"
        payload["statements"][0]["applicability"]["species"] = {"state": "unknown", "value": None}
        with self.assertRaises(SynthesisError):
            validate_synthesis(payload, cases=synthesis_example_cases())

    def test_proposed_claim_with_missing_context_is_blocked_not_generalised(self):
        payload = synthesis_example_payload()
        payload["statements"][0]["applicability"]["species"] = {"state": "unknown", "value": None}
        result = validate_synthesis(payload, cases=synthesis_example_cases())
        self.assertTrue(result["statements"][0]["blocked"])
        self.assertEqual(result["statements"][0]["blocking_reasons"], ["missing_applicable_context"])

    def test_hypothesis_is_classified_separately(self):
        result = _valid()
        classifications = {s["statement_id"]: s["classification"] for s in result["statements"]}
        self.assertEqual(classifications["S-3"], "hypothesis")


class IndependenceTests(unittest.TestCase):
    def test_independent_claim_within_same_group_is_refused(self):
        payload = synthesis_example_payload()
        payload["independence"][0]["reviewed_a"] = "A10:F1"
        payload["independence"][0]["reviewed_b"] = "A11:F1"
        with self.assertRaises(SynthesisError):
            validate_synthesis(payload, cases=synthesis_example_cases())

    def test_independent_claim_needs_distinct_underlying_sources(self):
        payload = synthesis_example_payload()
        payload["independence"][0]["underlying_b"] = "study:S1"  # same as underlying_a
        with self.assertRaises(SynthesisError):
            validate_synthesis(payload, cases=synthesis_example_cases())

    def test_agent_independent_claim_needs_human_confirmation(self):
        payload = synthesis_example_payload()
        payload["independence"][0]["adjudicated_by"] = {"actor_kind": "agent", "actor_id": "agent:writer"}
        with self.assertRaises(SynthesisError):
            validate_synthesis(payload, cases=synthesis_example_cases())
        payload["independence"][0]["adjudicated_by"]["confirmation_ref"] = "CONF-1"
        result = validate_synthesis(payload, cases=synthesis_example_cases())
        self.assertEqual(result["independence"][0]["conclusion"], "independent")

    def test_missing_rationale_refused(self):
        payload = synthesis_example_payload()
        del payload["independence"][0]["rationale"]
        with self.assertRaises(SynthesisError):
            validate_synthesis(payload, cases=synthesis_example_cases())


class FrameworkTests(unittest.TestCase):
    def test_example_framework_validates(self):
        framework = framework_example()
        self.assertEqual([c["concept_id"] for c in framework["concepts"]], ["C1", "C2"])
        self.assertEqual(framework["relationships"][0]["relationship_id"], "R1")
        self.assertEqual(framework["hypotheses"][0]["status"], "proposed")
        self.assertEqual(framework["unclassified_concepts"], [])

    def test_all_mandatory_criteria_required(self):
        synthesis = synthesis_example()
        payload = _framework_payload()
        for name in FRAMEWORK_CRITERIA:
            bad = copy.deepcopy(payload)
            del bad["criteria"][name]
            with self.assertRaises(SynthesisError):
                validate_framework(bad, synthesis=synthesis)

    def test_missing_safeguard_leaves_concept_unclassified(self):
        synthesis = synthesis_example()
        payload = _framework_payload()
        concept = payload["concepts"][0]
        concept["original_support"] = []
        concept["testable_implication"] = None
        concept["status"] = "proposed"
        framework = validate_framework(payload, synthesis=synthesis)
        self.assertFalse(framework["concepts"][0]["admitted"])
        self.assertEqual(framework["concepts"][0]["status"], "unclassified")
        self.assertEqual(framework["unclassified_concepts"], ["C1"])

    def test_unclassified_concept_cannot_be_established(self):
        synthesis = synthesis_example()
        payload = _framework_payload()
        payload["concepts"][0]["original_support"] = []
        payload["concepts"][0]["testable_implication"] = None
        payload["concepts"][0]["status"] = "established"
        payload["concepts"][0]["justification"] = "because"
        with self.assertRaises(SynthesisError):
            validate_framework(payload, synthesis=synthesis)

    def test_established_concept_requires_justification(self):
        synthesis = synthesis_example()
        payload = _framework_payload()
        payload["concepts"][0]["status"] = "established"
        with self.assertRaises(SynthesisError):
            validate_framework(payload, synthesis=synthesis)

    def test_hypothesis_cannot_be_established(self):
        synthesis = synthesis_example()
        payload = _framework_payload()
        payload["hypotheses"][0]["status"] = "established"
        with self.assertRaises(SynthesisError):
            validate_framework(payload, synthesis=synthesis)

    def test_support_keys_must_resolve_against_synthesis(self):
        synthesis = synthesis_example()
        payload = _framework_payload()
        payload["concepts"][0]["original_support"] = ["NOPE:F1"]
        with self.assertRaises(SynthesisError):
            validate_framework(payload, synthesis=synthesis)

    def test_relationship_endpoints_must_exist(self):
        synthesis = synthesis_example()
        payload = _framework_payload()
        payload["relationships"][0]["to_concept"] = "C9"
        with self.assertRaises(SynthesisError):
            validate_framework(payload, synthesis=synthesis)

    def test_accepts_a_plain_finding_key_set(self):
        framework = validate_framework(_framework_payload(), synthesis={"A10:F1", "A11:F1", "B7:F1"})
        self.assertEqual(framework["kind"], "framework")


class OutlineTests(unittest.TestCase):
    def test_example_outline_is_ordered_with_a_dependency_index(self):
        outline = outline_example()
        self.assertEqual([s["order"] for s in outline["sections"]], [1, 2, 3])
        self.assertEqual(outline["dependency_index"]["concepts"]["C1"], ["sec1", "sec2"])
        self.assertEqual(outline["dependency_index"]["statements"]["S-1"], ["sec2"])

    def test_orders_must_be_contiguous(self):
        framework = framework_example()
        synthesis = synthesis_example()
        payload = _outline_payload()
        payload["sections"][2]["order"] = 4
        with self.assertRaises(SynthesisError):
            validate_outline(payload, framework=framework, synthesis=synthesis)

    def test_duplicate_order_refused(self):
        framework = framework_example()
        synthesis = synthesis_example()
        payload = _outline_payload()
        payload["sections"][1]["order"] = 1
        with self.assertRaises(SynthesisError):
            validate_outline(payload, framework=framework, synthesis=synthesis)

    def test_unknown_concept_dependency_refused(self):
        framework = framework_example()
        synthesis = synthesis_example()
        payload = _outline_payload()
        payload["sections"][0]["concept_dependencies"] = ["C9"]
        with self.assertRaises(SynthesisError):
            validate_outline(payload, framework=framework, synthesis=synthesis)

    def test_unknown_statement_dependency_refused(self):
        framework = framework_example()
        synthesis = synthesis_example()
        payload = _outline_payload()
        payload["sections"][1]["statement_dependencies"] = ["S-99"]
        with self.assertRaises(SynthesisError):
            validate_outline(payload, framework=framework, synthesis=synthesis)

    def test_sections_must_not_be_empty(self):
        framework = framework_example()
        synthesis = synthesis_example()
        payload = _outline_payload()
        payload["sections"] = []
        with self.assertRaises(SynthesisError):
            validate_outline(payload, framework=framework, synthesis=synthesis)


def _framework_payload():
    return {
        "schema_version": 1,
        "title": "T",
        "contribution": "C",
        "criteria": {
            "contribution": "a",
            "admission": "b",
            "support": "c",
            "revision": "d",
        },
        "concepts": [
            {
                "concept_id": "C1",
                "definition": "def",
                "role": "role",
                "original_support": ["A10:F1"],
                "applicability_limits": "limits",
                "testable_implication": "an experiment would show it",
                "status": "proposed",
            },
            {
                "concept_id": "C2",
                "definition": "def2",
                "role": "role2",
                "original_support": ["B7:F1"],
                "applicability_limits": "limits2",
                "testable_implication": {"state": "not_applicable", "reason": "methodological"},
                "status": "proposed",
            },
        ],
        "relationships": [
            {
                "relationship_id": "R1",
                "from_concept": "C1",
                "to_concept": "C2",
                "directionality": "C1 constrains C2",
                "scope": "corpus only",
                "status": "proposed",
            }
        ],
        "hypotheses": [
            {"hypothesis_id": "H1", "statement": "maybe", "status": "proposed"}
        ],
        "open_questions": [],
    }


def _outline_payload():
    return {
        "schema_version": 1,
        "title": "outline",
        "sections": [
            {"section_id": "sec1", "order": 1, "title": "one", "concept_dependencies": ["C1"]},
            {"section_id": "sec2", "order": 2, "title": "two", "statement_dependencies": ["S-1"]},
            {"section_id": "sec3", "order": 3, "title": "three"},
        ],
    }


if __name__ == "__main__":
    unittest.main()
