"""Synthetic S9-SCHEMA suite; no model calls, no real evidence.

Each test states a *structural* obligation of
``review_article_agent.extraction.schema``.  These tests cannot and do not claim
semantic completeness: they distinguish what structural validation can prove
(grounding, coverage accounting, no fabrication, identity separation) from the
human/quality audit that still owns omission and error detection.
"""

import copy
import json
import unittest

from review_article_agent.extraction import (
    EXTRACTION_INSTRUCTIONS,
    SCHEMA_VERSION,
    ExtractionError,
    extraction_example,
    validate_extraction,
)


def passages():
    return [
        {
            "passage_id": "p0001",
            "source_key": "docA",
            "text": "In mice, compound Z did not reduce tumor volume compared with vehicle.",
            "asset_ids": ["tbl1"],
        },
        {
            "passage_id": "p0002",
            "source_key": "docA",
            "text": "The 50 mg dose reduced tumor volume by 12.5 mm3 per mouse (n=8).",
        },
        {
            "passage_id": "p0003",
            "source_key": "docB",
            "text": "We conclude that compound Z is broadly effective.",
        },
    ]


def stated_known(value):
    return {"state": "known", "value": value}


def stated_missing():
    return {"state": "missing", "value": None}


def context(**overrides):
    base = {
        "species": stated_known("mouse"),
        "population": stated_missing(),
        "assay": stated_known("tumor volume"),
        "outcome": stated_known("tumor volume"),
        "conditions": stated_known("vehicle comparison"),
    }
    base.update(overrides)
    return base


def finding(fid, passage_id, snippet, **overrides):
    record = {
        "finding_id": fid,
        "source_key": "docA",
        "passage_id": passage_id,
        "snippet": snippet,
        "statement": "statement for %s" % fid,
        "attribution": "direct_observation",
        "citation_flag": "own_result",
        "context": context(),
        "secondary_warning": {"state": "not_applicable", "value": None},
        "limitations": stated_known("single model system"),
        "numeric_details": [],
    }
    record.update(overrides)
    return record


def valid_output():
    return {
        "schema_version": SCHEMA_VERSION,
        "summary": "Effects of compound Z, separating measurement from author spin.",
        "source_records": [
            {"source_key": "docA", "source_kind": "document"},
            {"source_key": "docB", "source_kind": "document"},
        ],
        "studies": [{"study_id": "S1"}],
        "datasets": [{"dataset_id": "D1"}],
        "experiments": [{"experiment_id": "E1", "study_id": "S1"}],
        "findings": [
            finding("F1", "p0001", "did not reduce tumor volume"),
            finding(
                "F2", "p0002", "reduced tumor volume by 12.5 mm3",
                numeric_details=[
                    {
                        "name": "tumor volume",
                        "value": stated_known(12.5),
                        "unit": stated_known("mm3"),
                        "denominator": stated_known("per mouse"),
                    }
                ],
            ),
            finding(
                "F3", "p0003", "compound Z is broadly effective",
                source_key="docB", attribution="author_interpretation",
                citation_flag="mixed", context=context(species={"state": "unknown", "value": None}),
            ),
        ],
        "coverage": [
            {"target_kind": "passage", "target_id": "p0001", "disposition": "finding", "finding_id": "F1"},
            {"target_kind": "passage", "target_id": "p0002", "disposition": "finding", "finding_id": "F2"},
            {"target_kind": "passage", "target_id": "p0003", "disposition": "finding", "finding_id": "F3"},
            {"target_kind": "asset", "target_id": "tbl1", "disposition": "omitted", "reason": "table_not_acquired"},
        ],
        "links": [{"from": "source:docA", "to": "study:S1", "relation": "reports", "status": "confirmed"}],
        "appraisal": [],
        "omissions": [],
    }


def validate(out, dims=None):
    return validate_extraction(out, passages=passages(), appraisal_dimensions=dims or [])


class GroundingTests(unittest.TestCase):
    def test_valid_output_normalizes_and_is_json_portable(self):
        result = validate(valid_output())
        self.assertEqual(result["schema_version"], SCHEMA_VERSION)
        self.assertEqual([f["finding_id"] for f in result["findings"]], ["F1", "F2", "F3"])
        json.dumps(result)  # must be JSON-portable

    def test_invalid_quote_is_refused(self):
        for snippet in (
            "did not reduce tumour volume",  # paraphrase / spelling change
            "reduced tumor volume",          # negation dropped
            "Z reduced tumor volume",        # re-ordered
        ):
            out = valid_output()
            out["findings"][0]["snippet"] = snippet
            with self.assertRaises(ExtractionError):
                validate(out)

    def test_negation_preserved_in_snippet(self):
        result = validate(valid_output())
        self.assertIn("did not reduce", result["findings"][0]["snippet"])

    def test_snippet_must_belong_to_its_own_passage(self):
        out = valid_output()
        out["findings"][0]["snippet"] = "reduced tumor volume by 12.5 mm3"  # belongs to p0002
        with self.assertRaises(ExtractionError):
            validate(out)

    def test_snippet_may_resolve_from_raw_text(self):
        pool = passages()
        pool[0]["text"] = "In mice, compound Z did not reduce tumor volume."  # softened
        pool[0]["raw_text"] = "In mice, compound Z did not reduce tumor volume compared with vehicle."
        out = valid_output()
        out["findings"][0]["snippet"] = "did not reduce tumor volume compared with vehicle"
        result = validate_extraction(out, passages=pool, appraisal_dimensions=[])
        self.assertEqual(result["findings"][0]["passage_id"], "p0001")


class CompletenessTests(unittest.TestCase):
    def test_unaccounted_passage_fails_closed(self):
        out = valid_output()
        out["coverage"] = [c for c in out["coverage"] if c["target_id"] != "p0003"]
        out["findings"] = [f for f in out["findings"] if f["finding_id"] != "F3"]
        with self.assertRaises(ExtractionError) as ctx:
            validate(out)
        self.assertIn("does not account", str(ctx.exception))

    def test_correct_quote_alone_is_not_completeness(self):
        # A real, exact quote does not excuse an unaccounted passage.
        out = valid_output()
        out["coverage"] = [c for c in out["coverage"] if c["target_id"] != "p0002"]
        out["findings"] = [f for f in out["findings"] if f["finding_id"] != "F2"]
        with self.assertRaises(ExtractionError):
            validate(out)

    def test_omission_record_satisfies_completeness(self):
        out = valid_output()
        out["coverage"] = [
            c for c in out["coverage"] if c["target_id"] != "p0003"
        ] + [{"target_kind": "passage", "target_id": "p0003", "disposition": "omitted", "reason": "not_scientific_content"}]
        out["findings"] = [f for f in out["findings"] if f["finding_id"] != "F3"]
        result = validate(out)
        omitted = [c for c in result["coverage"] if c["disposition"] == "omitted"]
        self.assertEqual({c["target_id"] for c in omitted}, {"p0003", "tbl1"})

    def test_omitted_requires_reason(self):
        out = valid_output()
        out["coverage"][3].pop("reason")
        with self.assertRaises(ExtractionError):
            validate(out)

    def test_duplicate_coverage_refused(self):
        out = valid_output()
        out["coverage"].append(dict(out["coverage"][0]))
        with self.assertRaises(ExtractionError):
            validate(out)

    def test_finding_not_claimed_in_coverage_refused(self):
        out = valid_output()
        out["coverage"][0] = {"target_kind": "passage", "target_id": "p0001", "disposition": "no_finding"}
        with self.assertRaises(ExtractionError):
            validate(out)


class MissingValueTests(unittest.TestCase):
    def test_missing_values_stay_missing(self):
        result = validate(valid_output())
        pop = result["findings"][0]["context"]["population"]
        self.assertEqual(pop, {"state": "missing", "value": None})
        self.assertIsNone(result["findings"][0]["numeric_details"] or None)

    def test_known_state_requires_value(self):
        out = valid_output()
        out["findings"][0]["context"]["species"] = {"state": "known", "value": "  "}
        with self.assertRaises(ExtractionError):
            validate(out)

    def test_missing_state_forbids_fabricated_value(self):
        out = valid_output()
        out["findings"][0]["context"]["population"] = {"state": "missing", "value": "C57BL/6"}
        with self.assertRaises(ExtractionError):
            validate(out)

    def test_unknown_state_is_not_known(self):
        out = valid_output()
        out["findings"][0]["context"]["species"] = {"state": "unknown", "value": None}
        result = validate(out)
        self.assertEqual(result["findings"][0]["context"]["species"]["state"], "unknown")


class NumberTests(unittest.TestCase):
    def test_known_value_with_unit_and_denominator(self):
        result = validate(valid_output())
        detail = result["findings"][1]["numeric_details"][0]
        self.assertEqual(detail["value"]["value"], 12.5)
        self.assertEqual(detail["unit"]["value"], "mm3")
        self.assertEqual(detail["denominator"]["value"], "per mouse")

    def test_missing_denominator_is_allowed_not_invented(self):
        out = valid_output()
        out["findings"][1]["numeric_details"][0]["denominator"] = stated_missing()
        result = validate(out)
        self.assertEqual(result["findings"][1]["numeric_details"][0]["denominator"]["state"], "missing")

    def test_non_numeric_known_value_refused(self):
        out = valid_output()
        out["findings"][1]["numeric_details"][0]["value"] = {"state": "known", "value": "about twelve"}
        with self.assertRaises(ExtractionError):
            validate(out)

    def test_boolean_is_not_a_number(self):
        out = valid_output()
        out["findings"][1]["numeric_details"][0]["value"] = {"state": "known", "value": True}
        with self.assertRaises(ExtractionError):
            validate(out)


class SpeciesAndSpinTests(unittest.TestCase):
    def test_species_context_is_captured_distinctly(self):
        result = validate(valid_output())
        self.assertEqual(result["findings"][0]["context"]["species"]["value"], "mouse")
        self.assertEqual(result["findings"][2]["context"]["species"]["state"], "unknown")

    def test_author_spin_is_attributed_not_laundered(self):
        result = validate(valid_output())
        spin = result["findings"][2]
        self.assertEqual(spin["attribution"], "author_interpretation")
        self.assertEqual(spin["citation_flag"], "mixed")
        self.assertIn("broadly effective", spin["snippet"])

    def test_attribution_enum_is_enforced(self):
        out = valid_output()
        out["findings"][2]["attribution"] = "probably_true"
        with self.assertRaises(ExtractionError):
            validate(out)


class TableSupplementTests(unittest.TestCase):
    def test_table_asset_must_be_covered(self):
        out = valid_output()
        out["coverage"] = [c for c in out["coverage"] if c["target_id"] != "tbl1"]
        with self.assertRaises(ExtractionError) as ctx:
            validate(out)
        self.assertIn("tbl1", str(ctx.exception))

    def test_supplement_pooled_as_passage_required(self):
        pool = passages() + [
            {
                "passage_id": "supp1",
                "source_key": "docA",
                "kind": "supplement",
                "text": "Supplementary Table S2 reports an independent cohort.",
            }
        ]
        out = valid_output()
        with self.assertRaises(ExtractionError):
            validate_extraction(out, passages=pool, appraisal_dimensions=[])
        out["coverage"].append(
            {"target_kind": "passage", "target_id": "supp1", "disposition": "omitted", "reason": "supplement_not_acquired"}
        )
        result = validate_extraction(out, passages=pool, appraisal_dimensions=[])
        self.assertIn("supp1", {c["target_id"] for c in result["coverage"]})

    def test_asset_reviewed_with_finding_is_grounded(self):
        pool = passages()
        out = valid_output()
        out["coverage"][3] = {
            "target_kind": "asset", "target_id": "tbl1", "disposition": "finding", "finding_id": "F1",
        }
        result = validate_extraction(out, passages=pool, appraisal_dimensions=[])
        self.assertEqual(result["coverage"][3]["finding_id"], "F1")


class IdentityTests(unittest.TestCase):
    def test_link_relation_target_kind_enforced(self):
        out = valid_output()
        out["links"] = [{"from": "source:docA", "to": "dataset:D1", "relation": "reports", "status": "confirmed"}]
        with self.assertRaises(ExtractionError):
            validate(out)

    def test_proposed_link_is_unproven_but_recorded(self):
        out = valid_output()
        out["links"] = [{"from": "source:docA", "to": "study:S1", "relation": "reports", "status": "proposed"}]
        result = validate(out)
        self.assertEqual(len(result["link_summary"]["proposed"]), 1)
        self.assertEqual(result["link_summary"]["confirmed"], [])

    def test_two_reports_of_one_study_are_not_independent(self):
        out = valid_output()
        out["links"] = [
            {"from": "source:docA", "to": "study:S1", "relation": "reports", "status": "confirmed"},
            {"from": "source:docB", "to": "study:S1", "relation": "reports", "status": "proposed"},
        ]
        result = validate(out)
        self.assertEqual(result["link_summary"]["independent_source_pairs"], [])

    def test_unlinked_sources_are_independent(self):
        result = validate(valid_output())
        self.assertIn(["docA", "docB"], result["link_summary"]["independent_source_pairs"])

    def test_unknown_ref_is_refused(self):
        out = valid_output()
        out["links"] = [{"from": "source:docA", "to": "study:S9", "relation": "reports", "status": "confirmed"}]
        with self.assertRaises(ExtractionError):
            validate(out)

    def test_experiment_study_relation_checked(self):
        out = valid_output()
        out["experiments"] = [{"experiment_id": "E1", "study_id": "S9"}]
        with self.assertRaises(ExtractionError):
            validate(out)

    def test_finding_source_must_match_passage_source(self):
        out = valid_output()
        out["findings"][0]["source_key"] = "docB"  # p0001 belongs to docA
        with self.assertRaises(ExtractionError):
            validate(out)


class AppraisalAndScopeTests(unittest.TestCase):
    def test_no_universal_score_key_allowed(self):
        for key in ("score", "quality_score", "global_score", "excluded"):
            out = valid_output()
            out[key] = 7
            with self.assertRaises(ExtractionError):
                validate(out)

    def test_appraisal_reports_configured_dimensions_only(self):
        dims = [{"dimension_id": "risk_of_bias"}, {"dimension_id": "applicability"}]
        out = valid_output()
        out["appraisal"] = [
            {"dimension_id": "risk_of_bias", "state": "assessed", "judgement": "high", "rationale": "no blinding"},
            {"dimension_id": "applicability", "state": "unassessed", "judgement": "not assessed"},
        ]
        result = validate(out, dims=dims)
        self.assertEqual([a["dimension_id"] for a in result["appraisal"]], ["risk_of_bias", "applicability"])

    def test_missing_appraisal_dimension_is_a_missing_check(self):
        dims = [{"dimension_id": "risk_of_bias"}]
        out = valid_output()
        out["appraisal"] = []
        with self.assertRaises(ExtractionError):
            validate(out, dims=dims)

    def test_unassessed_is_not_a_pass_and_weak_study_retained(self):
        dims = [{"dimension_id": "risk_of_bias"}]
        out = valid_output()
        out["appraisal"] = [{"dimension_id": "risk_of_bias", "state": "unassessed", "judgement": "not assessed"}]
        result = validate(out, dims=dims)
        self.assertEqual(result["appraisal"][0]["state"], "unassessed")
        self.assertEqual(len(result["findings"]), 3)  # findings are not excluded by a weak appraisal

    def test_appraisal_dimension_cannot_define_a_score(self):
        with self.assertRaises(ExtractionError):
            validate(valid_output(), dims=[{"dimension_id": "x", "score": 1}])

    def test_semantic_wrongness_is_out_of_scope(self):
        # A factually wrong but exactly-grounded, fully-covered output passes:
        # structural validation cannot judge scientific truth.
        out = valid_output()
        out["findings"][0]["statement"] = "Compound Z cured every subject."
        result = validate(out)
        self.assertEqual(result["findings"][0]["statement"], "Compound Z cured every subject.")

    def test_instructions_mention_core_grounding_rules(self):
        for phrase in ("EXACT substring", "negation", "missing", "author_interpretation", "coverage", "score"):
            self.assertIn(phrase, EXTRACTION_INSTRUCTIONS)


class ExampleFixtureTests(unittest.TestCase):
    def test_example_fixture_is_valid_and_json_portable(self):
        result = extraction_example()
        json.dumps(result)
        self.assertEqual(result["validation"]["required_items"], result["validation"]["accounted_items"])
        self.assertTrue(result["link_summary"]["confirmed"])
        self.assertIn("asset", {c["target_kind"] for c in result["coverage"]})

    def test_example_fixture_is_independent_of_caller_mutation(self):
        first = extraction_example()
        first["summary"] = "mutated"
        second = extraction_example()
        self.assertNotEqual(second["summary"], "mutated")


if __name__ == "__main__":
    unittest.main()
