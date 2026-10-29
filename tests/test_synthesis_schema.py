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

    def test_same_exact_source_revision_across_cases_is_not_independent(self):
        result = validate_synthesis(_source_identity_payload(), cases=_shared_source_cases())
        groups = result["correlation_groups"]
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["independence"], "not_independent")
        self.assertEqual(groups[0]["basis"], "shared_source_document")
        self.assertEqual(groups[0]["member_findings"], ["X1:F1", "X2:F1"])

    def test_alternate_revision_of_one_document_correlates(self):
        cases = _shared_source_cases(rev_a="v1", rev_b="v2")
        result = validate_synthesis(_source_identity_payload(), cases=cases)
        self.assertEqual(result["correlation_groups"][0]["independence"], "not_independent")

    def test_independent_claim_for_same_original_document_is_refused(self):
        payload = _source_identity_payload(independence=True)
        with self.assertRaises(SynthesisError):
            validate_synthesis(payload, cases=_shared_source_cases())


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

    def test_statement_cannot_trace_to_excluded_disposition(self):
        payload = synthesis_example_payload()
        for entry in payload["dispositions"]:
            if entry["finding"] == "A11:F1":
                entry["disposition"] = "not_used"
        with self.assertRaises(SynthesisError):
            validate_synthesis(payload, cases=synthesis_example_cases())

    def test_statement_can_trace_retained_scientific_dispositions(self):
        result = _valid()
        s2 = [s for s in result["statements"] if s["statement_id"] == "S-2"][0]
        self.assertEqual(s2["evidence"], ["A11:F1"])
        self.assertEqual(result["by_disposition"]["null_evidence"], ["A11:F1"])

    def test_incorporated_finding_must_be_traced_by_a_statement(self):
        payload = synthesis_example_payload()
        payload["statements"] = [s for s in payload["statements"] if s["statement_id"] != "S-2"]
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

    def test_approved_claim_with_missing_declared_context_is_rejected(self):
        payload = synthesis_example_payload()
        payload["statements"][0]["status"] = "approved"
        payload["statements"][0]["applicability"]["species"] = {"state": "unknown", "value": None}
        with self.assertRaises(SynthesisError):
            validate_synthesis(payload, cases=synthesis_example_cases())

    def test_proposed_claim_with_missing_original_context_is_blocked(self):
        cases = synthesis_example_cases()
        for finding in cases[0]["extraction"]["findings"]:
            finding["context"]["species"] = {"state": "missing", "value": None}
        result = validate_synthesis(synthesis_example_payload(), cases=cases)
        s1 = [s for s in result["statements"] if s["statement_id"] == "S-1"][0]
        self.assertTrue(s1["blocked"])
        self.assertEqual(s1["blocking_reasons"], ["missing_original_context"])
        self.assertEqual(s1["missing_original_context"], ["species"])

    def test_approved_claim_with_missing_original_context_is_rejected(self):
        cases = synthesis_example_cases()
        for finding in cases[0]["extraction"]["findings"]:
            finding["context"]["population"] = {"state": "unknown", "value": None}
        payload = synthesis_example_payload()
        payload["statements"][0]["status"] = "approved"
        with self.assertRaises(SynthesisError):
            validate_synthesis(payload, cases=cases)

    def test_context_mismatch_blocks_reported_evidence(self):
        cases = synthesis_example_cases()
        for finding in cases[1]["extraction"]["findings"]:
            finding["context"]["species"] = {"state": "known", "value": "human"}
        result = validate_synthesis(synthesis_example_payload(), cases=cases)
        s2 = [s for s in result["statements"] if s["statement_id"] == "S-2"][0]
        self.assertTrue(s2["blocked"])
        self.assertIn("context_mismatch", s2["blocking_reasons"])
        self.assertEqual(s2["context_mismatch"], ["species"])

    def test_context_mismatch_allowed_for_new_interpretation(self):
        cases = synthesis_example_cases()
        for finding in cases[1]["extraction"]["findings"]:
            finding["context"]["species"] = {"state": "known", "value": "human"}
        payload = synthesis_example_payload()
        payload["statements"][1]["classification"] = "new_interpretation"
        result = validate_synthesis(payload, cases=cases)
        s2 = [s for s in result["statements"] if s["statement_id"] == "S-2"][0]
        self.assertFalse(s2["blocked"])
        self.assertEqual(s2["context_mismatch"], ["species"])

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

    def test_missing_counterevidence_safeguard_leaves_unclassified(self):
        synthesis = synthesis_example()
        payload = _framework_payload()
        del payload["concepts"][0]["counterevidence_note"]
        framework = validate_framework(payload, synthesis=synthesis)
        self.assertFalse(framework["concepts"][0]["counterevidence_safeguard"])
        self.assertEqual(framework["concepts"][0]["status"], "unclassified")
        self.assertIn("C1", framework["unclassified_concepts"])

    def test_empty_counterevidence_allowed_with_explicit_note(self):
        synthesis = synthesis_example()
        payload = _framework_payload()
        payload["concepts"][0]["counterevidence"] = []
        payload["concepts"][0]["counterevidence_note"] = "no counterevidence identified"
        framework = validate_framework(payload, synthesis=synthesis)
        self.assertTrue(framework["concepts"][0]["counterevidence_safeguard"])
        self.assertEqual(framework["concepts"][0]["status"], "proposed")

    def test_established_relationship_requires_traceable_support(self):
        synthesis = synthesis_example()
        payload = _framework_payload()
        payload["relationships"][0]["status"] = "established"
        payload["relationships"][0]["justification"] = "because"
        with self.assertRaises(SynthesisError):
            validate_framework(payload, synthesis=synthesis)
        payload["relationships"][0]["evidence"] = ["A10:F1"]
        framework = validate_framework(payload, synthesis=synthesis)
        self.assertEqual(framework["relationships"][0]["status"], "established")


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


def _shared_source_cases(rev_a="v1", rev_b="v1"):
    """Two accepted cases that read the SAME original document (revision or artifact)."""

    def case(case_id, revision_id, finding_id):
        return {
            "id": case_id,
            "extraction_ref": {"project_id": "P-0001", "artifact_id": case_id, "revision_id": "e1"},
            "source_refs": [{"project_id": "P-0001", "artifact_id": "DOC", "revision_id": revision_id}],
            "extraction": {
                "source_records": [{"source_key": "s1", "source_kind": "document"}],
                "findings": [
                    {
                        "finding_id": finding_id,
                        "source_key": "s1",
                        "passage_id": "p1",
                        "snippet": "text",
                        "statement": "statement",
                        "attribution": "direct_observation",
                        "citation_flag": "own_result",
                        "context": {
                            "species": {"state": "known", "value": "mouse"},
                            "population": {"state": "known", "value": "n=8"},
                            "assay": {"state": "known", "value": "tumor volume"},
                            "outcome": {"state": "known", "value": "tumor volume"},
                            "conditions": {"state": "known", "value": "vehicle"},
                        },
                        "secondary_warning": {"state": "not_applicable", "value": None},
                        "limitations": {"state": "known", "value": "single model"},
                        "numeric_details": [],
                    }
                ],
                "links": [],
            },
        }

    return [case("X1", rev_a, "F1"), case("X2", rev_b, "F1")]


def _source_identity_payload(independence=False):
    payload = {
        "schema_version": 1,
        "memo": "two extractions of the same original document",
        "statements": [
            {
                "statement_id": "S-1",
                "text": "effect",
                "classification": "reported_evidence",
                "evidence": ["X1:F1", "X2:F1"],
                "applicability": {
                    "species": {"state": "known", "value": "mouse"},
                    "population": {"state": "known", "value": "n=8"},
                    "assay": {"state": "known", "value": "tumor volume"},
                    "outcome": {"state": "known", "value": "tumor volume"},
                    "conditions": {"state": "known", "value": "vehicle"},
                },
                "causal_limitations": {"state": "known", "value": "association only"},
                "uncertainty": {"state": "known", "value": "limited"},
                "status": "proposed",
            }
        ],
        "dispositions": [
            {"finding": "X1:F1", "disposition": "incorporated", "rationale": "used"},
            {"finding": "X2:F1", "disposition": "incorporated", "rationale": "used"},
        ],
    }
    if independence:
        payload["independence"] = [
            {
                "adjudication_id": "IND-1",
                "reviewed_a": "X1:F1",
                "reviewed_b": "X2:F1",
                "underlying_a": "document:DOC/v1",
                "underlying_b": "document:DOC/v2",
                "conclusion": "independent",
                "basis": "different revisions",
                "rationale": "mistaken adjudication",
                "adjudicated_by": {"actor_kind": "user", "actor_id": "user:r1", "confirmation_ref": None},
            }
        ]
    return payload


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
                "counterevidence_note": "no counterevidence identified",
                "applicability_limits": "limits",
                "testable_implication": "an experiment would show it",
                "status": "proposed",
            },
            {
                "concept_id": "C2",
                "definition": "def2",
                "role": "role2",
                "original_support": ["B7:F1"],
                "counterevidence_note": "no counterevidence identified",
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
