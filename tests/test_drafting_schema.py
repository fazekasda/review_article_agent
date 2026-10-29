"""Focused S11-SCHEMA regression suite; no model calls, no real evidence, no storage.

These assert *structural* obligations only (atomic grounded claims, context and
attribution fidelity, derived citations, exact draft coverage, provisional
status). They cannot and do not claim scientific correctness or that free text is
semantically true.
"""

import copy
import json
import unittest

from review_article_agent.drafting.schema import (
    DraftingError,
    PENDING_CHECKS,
    blueprint_example,
    draft_example,
    drafting_example_package,
    validate_blueprint,
    validate_draft,
)


def _known(value):
    return {"state": "known", "value": value}


def _context(**overrides):
    base = {
        "species": _known("mouse"),
        "population": _known("n=8 mice"),
        "assay": _known("tumor volume"),
        "outcome": _known("tumor volume"),
        "conditions": _known("vehicle comparison"),
    }
    base.update(overrides)
    return base


def _blueprint_payload():
    """A raw (unvalidated) blueprint payload mirroring :func:`blueprint_example`."""
    return {
        "schema_version": 1,
        "section_id": "sec2",
        "source_suggestions": ["A10:F1", "B7:F1", "C3:F1"],
        "paragraphs": [
            {
                "paragraph_id": "P1",
                "intents": [
                    {
                        "intent_id": "I1",
                        "intent": "State the multi-source reported effect with its limits.",
                        "claims": [
                            {
                                "claim_id": "K1",
                                "text": "Compound Z reduced tumor volume in a mouse model.",
                                "finding_keys": ["A10:F1", "B7:F1"],
                                "attribution": "direct_observation",
                                "classification": "reported_evidence",
                                "applicability": _context(),
                                "causal_limitations": _known("association only"),
                            }
                        ],
                    }
                ],
            },
            {
                "paragraph_id": "P2",
                "intents": [
                    {
                        "intent_id": "I2",
                        "intent": "Attribute the secondary account explicitly.",
                        "claims": [
                            {
                                "claim_id": "K2",
                                "text": "A secondary account repeats the reduction.",
                                "finding_keys": ["C3:F1"],
                                "attribution": "secondary_account",
                                "classification": "reported_evidence",
                                "applicability": _context(),
                                "causal_limitations": _known("secondary source"),
                            }
                        ],
                    },
                    {
                        "intent_id": "I3",
                        "intent": "Offer a labelled author proposal.",
                        "claims": [
                            {
                                "claim_id": "K3",
                                "text": "A shared upstream mechanism may explain the effect.",
                                "finding_keys": ["A10:F1"],
                                "attribution": "proposed_synthesis",
                                "classification": "proposed_synthesis",
                                "applicability": _context(),
                                "causal_limitations": _known("proposed only"),
                            }
                        ],
                    },
                ],
            },
        ],
    }


def _claim(payload, claim_id):
    for paragraph in payload["paragraphs"]:
        for intent in paragraph["intents"]:
            for claim in intent["claims"]:
                if claim["claim_id"] == claim_id:
                    return claim
    raise AssertionError(claim_id)


def _draft_payload(blueprint, text="prose"):
    """A raw draft payload covering the example blueprint."""
    return {
        "schema_version": 1,
        "section_id": blueprint["section_id"],
        "paragraphs": [
            {"paragraph_id": "P1", "sentences": [
                {"intent_id": "I1", "claim_ids": ["K1"], "text": text}]},
            {"paragraph_id": "P2", "sentences": [
                {"intent_id": "I2", "claim_ids": ["K2"], "text": text},
                {"intent_id": "I3", "claim_ids": ["K3"], "text": text}]},
        ],
    }


class ExampleAndDeterminismTests(unittest.TestCase):
    def test_blueprint_and_draft_are_json_portable_and_deterministic(self):
        package = drafting_example_package()
        first = blueprint_example(package)
        second = blueprint_example(drafting_example_package())
        json.dumps(first)
        self.assertEqual(json.dumps(first, sort_keys=True), json.dumps(second, sort_keys=True))
        draft = draft_example(first, package)
        json.dumps(draft)

    def test_example_package_builders_are_isolated(self):
        package = drafting_example_package()
        package["evidence"][0]["finding_key"] = "MUTATED"
        self.assertNotEqual(drafting_example_package()["evidence"][0]["finding_key"], "MUTATED")

    def test_result_is_provisional_and_not_exportable(self):
        blueprint = blueprint_example()
        draft = draft_example(blueprint)
        for result in (blueprint, draft):
            self.assertEqual(result["status"], "provisional")
            self.assertIs(result["authorizes_export"], False)
            self.assertEqual(result["pending"], list(PENDING_CHECKS))


class MalformedInputTests(unittest.TestCase):
    def test_non_object_payload_refused(self):
        with self.assertRaises(DraftingError):
            validate_blueprint(["not", "an", "object"], package=drafting_example_package())

    def test_schema_version_must_be_exact_int(self):
        for bad in (True, "1", 1.0, None, 2):
            payload = _blueprint_payload()
            payload["schema_version"] = bad
            with self.assertRaises(DraftingError):
                validate_blueprint(payload, package=drafting_example_package())

    def test_unknown_key_refused(self):
        payload = _blueprint_payload()
        payload["extra"] = "x"
        with self.assertRaises(DraftingError):
            validate_blueprint(payload, package=drafting_example_package())

    def test_section_id_must_match_package(self):
        payload = _blueprint_payload()
        payload["section_id"] = "sec9"
        with self.assertRaises(DraftingError):
            validate_blueprint(payload, package=drafting_example_package())


class ForbiddenOverrideTests(unittest.TestCase):
    def test_context_and_scope_overrides_are_refused(self):
        for key in ("framework", "scope", "definitions", "outline", "synthesis", "methods", "context"):
            payload = _blueprint_payload()
            payload[key] = {"anything": True}
            with self.assertRaises(DraftingError):
                validate_blueprint(payload, package=drafting_example_package())

    def test_final_or_approved_flags_are_refused(self):
        for key in ("status", "approved", "final", "authorizes_export"):
            payload = _blueprint_payload()
            payload[key] = "approved"
            with self.assertRaises(DraftingError):
                validate_blueprint(payload, package=drafting_example_package())

    def test_model_supplied_bibliography_is_refused(self):
        payload = _blueprint_payload()
        _claim(payload, "K1")["citation_keys"] = ["CIT-A10"]
        with self.assertRaises(DraftingError):
            validate_blueprint(payload, package=drafting_example_package())
        payload = _blueprint_payload()
        payload["bibliography"] = [{"title": "fabricated"}]
        with self.assertRaises(DraftingError):
            validate_blueprint(payload, package=drafting_example_package())


class GroundingTests(unittest.TestCase):
    def test_compound_claim_with_incomplete_support_is_refused(self):
        # K1 asserts a compound effect over two sources; drop one authorized source.
        package = drafting_example_package()
        package["evidence"] = [e for e in package["evidence"] if e["finding_key"] != "B7:F1"]
        package["citations"].pop("CIT-B7")
        payload = _blueprint_payload()
        payload["source_suggestions"] = ["A10:F1", "C3:F1"]
        with self.assertRaises(DraftingError):
            validate_blueprint(payload, package=package)

    def test_multi_source_claim_is_accepted_and_derives_citations(self):
        result = validate_blueprint(_blueprint_payload(), package=drafting_example_package())
        k1 = _claim(result, "K1")
        self.assertEqual(k1["finding_keys"], ["A10:F1", "B7:F1"])
        self.assertEqual(k1["citation_keys"], ["CIT-A10", "CIT-B7"])

    def test_ungrounded_claim_is_refused(self):
        payload = _blueprint_payload()
        _claim(payload, "K1")["finding_keys"] = []
        with self.assertRaises(DraftingError):
            validate_blueprint(payload, package=drafting_example_package())

    def test_unknown_citation_in_package_is_refused(self):
        package = drafting_example_package()
        package["evidence"][0]["citation_keys"] = ["CIT-MISSING"]
        with self.assertRaises(DraftingError):
            validate_blueprint(_blueprint_payload(), package=package)


class ContextFidelityTests(unittest.TestCase):
    def test_unsupported_generalization_is_refused(self):
        # A reported-evidence claim may not widen the source species.
        payload = _blueprint_payload()
        _claim(payload, "K1")["applicability"] = _context(species=_known("human"))
        with self.assertRaises(DraftingError):
            validate_blueprint(payload, package=drafting_example_package())

    def test_interpretation_may_extend_context_with_warning(self):
        payload = _blueprint_payload()
        claim = _claim(payload, "K1")
        claim["classification"] = "new_interpretation"
        claim["attribution"] = "author_interpretation"
        claim["applicability"] = _context(species=_known("human"))
        result = validate_blueprint(payload, package=drafting_example_package())
        self.assertIn("context_extension", _claim(result, "K1")["warnings"])
        self.assertFalse(_claim(result, "K1")["primary_verified"])


class AttributionTests(unittest.TestCase):
    def test_secondary_source_cannot_be_presented_as_primary(self):
        payload = _blueprint_payload()
        _claim(payload, "K2")["attribution"] = "direct_observation"
        with self.assertRaises(DraftingError):
            validate_blueprint(payload, package=drafting_example_package())

    def test_explicit_secondary_attribution_is_warned_and_not_primary(self):
        result = validate_blueprint(_blueprint_payload(), package=drafting_example_package())
        k2 = _claim(result, "K2")
        self.assertTrue(k2["secondary"])
        self.assertIn("secondary_origin_warning", k2["warnings"])
        self.assertFalse(k2["primary_verified"])

    def test_novel_author_proposal_is_labelled_not_established(self):
        result = validate_blueprint(_blueprint_payload(), package=drafting_example_package())
        k3 = _claim(result, "K3")
        self.assertTrue(k3["proposed_synthesis"])
        self.assertEqual(k3["classification"], "proposed_synthesis")
        self.assertFalse(k3["primary_verified"])

    def test_inconsistent_proposal_label_is_refused(self):
        payload = _blueprint_payload()
        _claim(payload, "K3")["classification"] = "reported_evidence"
        with self.assertRaises(DraftingError):
            validate_blueprint(payload, package=drafting_example_package())


class MissingContextBlockingTests(unittest.TestCase):
    def _package_with_missing_population(self):
        package = drafting_example_package()
        for entry in package["evidence"]:
            if entry["finding_key"] == "A10:F1":
                entry["finding"]["context"]["population"] = {"state": "missing", "value": None}
        return package

    def test_missing_original_context_blocks_reported_claim(self):
        with self.assertRaises(DraftingError):
            validate_blueprint(_blueprint_payload(), package=self._package_with_missing_population())

    def test_missing_original_context_blocks_interpretive_claim(self):
        # Renaming the classification must not bypass the block.
        payload = _blueprint_payload()
        claim = _claim(payload, "K1")
        claim["classification"] = "new_interpretation"
        claim["attribution"] = "author_interpretation"
        with self.assertRaises(DraftingError):
            validate_blueprint(payload, package=self._package_with_missing_population())

    def test_missing_original_context_blocks_proposed_claim(self):
        # K3 is proposed_synthesis grounded to A10:F1, whose context is missing.
        with self.assertRaises(DraftingError):
            validate_blueprint(_blueprint_payload(), package=self._package_with_missing_population())

    def test_not_applicable_context_does_not_block(self):
        package = drafting_example_package()
        for entry in package["evidence"]:
            entry["finding"]["context"]["conditions"] = {"state": "not_applicable", "value": None}
        result = validate_blueprint(_blueprint_payload(), package=package)
        self.assertFalse(_claim(result, "K1")["primary_verified"])


class AttributionFidelityTests(unittest.TestCase):
    def test_direct_observation_cannot_relabel_author_interpretation(self):
        package = drafting_example_package()
        for entry in package["evidence"]:
            if entry["finding_key"] == "A10:F1":
                entry["finding"]["attribution"] = "author_interpretation"
        with self.assertRaises(DraftingError):
            validate_blueprint(_blueprint_payload(), package=package)

    def test_accurate_author_interpretation_attribution_is_accepted(self):
        package = drafting_example_package()
        for entry in package["evidence"]:
            if entry["finding_key"] == "A10:F1":
                entry["finding"]["attribution"] = "author_interpretation"
        payload = _blueprint_payload()
        _claim(payload, "K1")["attribution"] = "author_interpretation"
        result = validate_blueprint(payload, package=package)
        self.assertEqual(_claim(result, "K1")["attribution"], "author_interpretation")

    def test_no_claim_is_primary_verified(self):
        result = validate_blueprint(_blueprint_payload(), package=drafting_example_package())
        for paragraph in result["paragraphs"]:
            for intent in paragraph["intents"]:
                for claim in intent["claims"]:
                    self.assertIs(claim["primary_verified"], False)
        self.assertIs(_claim(result, "K1")["primary_verified"], False)


class HeterogeneousContextTests(unittest.TestCase):
    def test_heterogeneous_source_context_is_not_unioned(self):
        # A10 reports mouse, B7 reports rat; a single 'mouse' reported claim
        # grounded to both must not silently generalize through a union.
        package = drafting_example_package()
        for entry in package["evidence"]:
            if entry["finding_key"] == "B7:F1":
                entry["finding"]["context"]["species"] = {"state": "known", "value": "rat"}
        with self.assertRaises(DraftingError):
            validate_blueprint(_blueprint_payload(), package=package)

    def test_heterogeneous_context_allowed_as_separate_atomic_claims(self):
        package = drafting_example_package()
        for entry in package["evidence"]:
            if entry["finding_key"] == "B7:F1":
                entry["finding"]["context"]["species"] = {"state": "known", "value": "rat"}
        payload = _blueprint_payload()
        k1 = _claim(payload, "K1")
        k1["finding_keys"] = ["A10:F1"]
        k1["applicability"] = _context(species=_known("mouse"))
        # a second, separately grounded atomic claim for the rat finding
        k1["text"] = "Compound Z reduced tumor volume in a mouse model."
        rat = copy.deepcopy(k1)
        rat["claim_id"] = "K4"
        rat["finding_keys"] = ["B7:F1"]
        rat["applicability"] = _context(species=_known("rat"))
        payload["paragraphs"][0]["intents"][0]["claims"].append(rat)
        payload["source_suggestions"] = ["A10:F1", "B7:F1", "C3:F1"]
        result = validate_blueprint(payload, package=package)
        self.assertEqual(_claim(result, "K4")["citation_keys"], ["CIT-B7"])


class DraftCoverageTests(unittest.TestCase):
    def _valid(self):
        package = drafting_example_package()
        return blueprint_example(package), package

    def test_valid_draft_derives_citations_from_package(self):
        blueprint, package = self._valid()
        result = draft_example(blueprint, package)
        self.assertEqual(result["kind"], "section_draft")
        p1 = result["paragraphs"][0]["sentences"][0]
        self.assertEqual(p1["citation_keys"], ["CIT-A10", "CIT-B7"])
        self.assertEqual(result["citations"]["CIT-A10"]["title"], "Report A10")

    def test_draft_requires_actual_sentence_text(self):
        blueprint, package = self._valid()
        for bad in (None, "", "   "):
            with self.assertRaises(DraftingError):
                validate_draft(_draft_payload(blueprint, text=bad), blueprint=blueprint, package=package)

    def test_draft_must_cover_every_blueprint_intent(self):
        blueprint, package = self._valid()
        payload = {
            "schema_version": 1,
            "section_id": blueprint["section_id"],
            "paragraphs": [
                {"paragraph_id": "P1", "sentences": [
                    {"intent_id": "I1", "claim_ids": ["K1"], "text": "prose"}]},
            ],
        }
        with self.assertRaises(DraftingError):
            validate_draft(payload, blueprint=blueprint, package=package)

    def test_draft_cannot_invent_a_claim(self):
        blueprint, package = self._valid()
        payload = _draft_payload(blueprint)
        payload["paragraphs"][0]["sentences"][0]["claim_ids"] = ["K1", "K999"]
        with self.assertRaises(DraftingError):
            validate_draft(payload, blueprint=blueprint, package=package)

    def test_draft_cannot_carry_study_details_or_bibliography(self):
        blueprint, package = self._valid()
        payload = _draft_payload(blueprint)
        payload["bibliography"] = [{"doi": "10.0/fake"}]
        with self.assertRaises(DraftingError):
            validate_draft(payload, blueprint=blueprint, package=package)


if __name__ == "__main__":
    unittest.main()
