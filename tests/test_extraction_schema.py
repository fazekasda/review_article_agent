"""Focused S9-SCHEMA regression suite; no model calls, no real evidence.

These assert *structural* obligations only (grounding, coverage accounting,
no fabrication, identity separation, no inferred independence). They cannot and
do not claim semantic completeness or correctness.
"""

import json
import unittest

from review_article_agent.extraction import (
    EXTRACTION_EXAMPLE_PASSAGES,
    EXTRACTION_INSTRUCTIONS,
    SCHEMA_VERSION,
    ExtractionError,
    extraction_example,
    raw_extraction_example,
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
        {"passage_id": "p0002", "source_key": "docA",
         "text": "The 50 mg dose reduced tumor volume by 12.5 mm3 per mouse (n=8)."},
        {"passage_id": "p0003", "source_key": "docB",
         "text": "We conclude that compound Z is broadly effective."},
    ]


def known(value):
    return {"state": "known", "value": value}


def missing():
    return {"state": "missing", "value": None}


def context(**overrides):
    base = {
        "species": known("mouse"),
        "population": missing(),
        "assay": known("tumor volume"),
        "outcome": known("tumor volume"),
        "conditions": known("vehicle comparison"),
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
        "limitations": known("single model system"),
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
            finding("F2", "p0002", "reduced tumor volume by 12.5 mm3",
                    numeric_details=[{"name": "tumor volume", "value": known(12.5),
                                      "unit": known("mm3"), "denominator": known("per mouse")}]),
            finding("F3", "p0003", "compound Z is broadly effective", source_key="docB",
                    attribution="author_interpretation", citation_flag="mixed",
                    context=context(species={"state": "unknown", "value": None})),
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


def validate(out, dims=None, pool=None):
    return validate_extraction(out, passages=pool or passages(), appraisal_dimensions=dims or [])


class GroundingTests(unittest.TestCase):
    def test_valid_output_normalizes_and_is_json_portable(self):
        result = validate(valid_output())
        json.dumps(result)
        self.assertEqual([f["finding_id"] for f in result["findings"]], ["F1", "F2", "F3"])

    def test_parsed_passages_with_extra_metadata_are_accepted(self):
        pool = passages()
        pool[0]["locator"] = {"page": 3, "span": [0, 40]}
        pool[0]["section_path"] = ["Results"]
        pool[0]["kind"] = "paragraph"
        pool[0]["text_sha256"] = "deadbeef"
        result = validate(valid_output(), pool=pool)
        self.assertEqual(result["findings"][0]["snippet"], "did not reduce tumor volume")

    def test_invalid_quote_is_refused(self):
        for snippet in ("did not reduce tumour volume", "reduced tumor volume", "Z reduced tumor volume"):
            out = valid_output()
            out["findings"][0]["snippet"] = snippet
            with self.assertRaises(ExtractionError):
                validate(out)

    def test_negation_preserved_in_snippet(self):
        self.assertIn("did not reduce", validate(valid_output())["findings"][0]["snippet"])


class CompletenessTests(unittest.TestCase):
    def _drop(self, out, target):
        out["coverage"] = [c for c in out["coverage"] if c["target_id"] != target]
        out["findings"] = [f for f in out["findings"] if f["passage_id"] != target]

    def test_unaccounted_passage_fails_closed(self):
        out = valid_output()
        self._drop(out, "p0003")
        with self.assertRaises(ExtractionError):
            validate(out)

    def test_correct_quote_alone_is_not_completeness(self):
        out = valid_output()
        self._drop(out, "p0002")  # a real, exact quote still leaves p0002 unaccounted
        with self.assertRaises(ExtractionError):
            validate(out)

    def test_omission_record_satisfies_completeness_but_requires_reason(self):
        out = valid_output()
        self._drop(out, "p0003")
        out["coverage"].append({"target_kind": "passage", "target_id": "p0003",
                                "disposition": "omitted", "reason": "not_scientific_content"})
        result = validate(out)
        omitted = {c["target_id"] for c in result["coverage"] if c["disposition"] == "omitted"}
        self.assertEqual(omitted, {"p0003", "tbl1"})
        out["coverage"][-1].pop("reason")
        with self.assertRaises(ExtractionError):
            validate(out)


class MissingValueTests(unittest.TestCase):
    def test_missing_values_stay_missing_not_fabricated(self):
        out = valid_output()
        out["findings"][0]["context"]["population"] = {"state": "missing", "value": "C57BL/6"}
        with self.assertRaises(ExtractionError):
            validate(out)
        self.assertEqual(validate(valid_output())["findings"][0]["context"]["population"],
                         {"state": "missing", "value": None})

    def test_known_state_requires_value(self):
        out = valid_output()
        out["findings"][0]["context"]["species"] = {"state": "known", "value": "  "}
        with self.assertRaises(ExtractionError):
            validate(out)


class NumberTests(unittest.TestCase):
    def test_known_value_with_unit_and_denominator(self):
        detail = validate(valid_output())["findings"][1]["numeric_details"][0]
        self.assertEqual((detail["value"]["value"], detail["unit"]["value"], detail["denominator"]["value"]),
                         (12.5, "mm3", "per mouse"))

    def test_missing_denominator_allowed_not_invented(self):
        out = valid_output()
        out["findings"][1]["numeric_details"][0]["denominator"] = missing()
        self.assertEqual(validate(out)["findings"][1]["numeric_details"][0]["denominator"]["state"], "missing")

    def test_non_numeric_and_boolean_known_values_refused(self):
        for bad in ("about twelve", True):
            out = valid_output()
            out["findings"][1]["numeric_details"][0]["value"] = {"state": "known", "value": bad}
            with self.assertRaises(ExtractionError):
                validate(out)


class ContextSpinTests(unittest.TestCase):
    def test_species_captured_and_spin_attributed(self):
        result = validate(valid_output())
        self.assertEqual(result["findings"][0]["context"]["species"]["value"], "mouse")
        spin = result["findings"][2]
        self.assertEqual((spin["attribution"], spin["citation_flag"]), ("author_interpretation", "mixed"))
        self.assertIn("broadly effective", spin["snippet"])

    def test_attribution_enum_enforced(self):
        out = valid_output()
        out["findings"][2]["attribution"] = "probably_true"
        with self.assertRaises(ExtractionError):
            validate(out)


class TableSupplementTests(unittest.TestCase):
    def test_table_asset_must_be_covered(self):
        out = valid_output()
        out["coverage"] = [c for c in out["coverage"] if c["target_id"] != "tbl1"]
        with self.assertRaises(ExtractionError):
            validate(out)

    def test_pooled_supplement_passage_is_required(self):
        pool = passages() + [{"passage_id": "supp1", "source_key": "docA", "kind": "supplement",
                              "text": "Supplementary Table S2 reports an independent cohort."}]
        out = valid_output()
        with self.assertRaises(ExtractionError):
            validate(out, pool=pool)
        out["coverage"].append({"target_kind": "passage", "target_id": "supp1",
                                "disposition": "omitted", "reason": "supplement_not_acquired"})
        self.assertIn("supp1", {c["target_id"] for c in validate(out, pool=pool)["coverage"]})


class IdentityAndLinkageTests(unittest.TestCase):
    def test_relation_target_kind_enforced(self):
        out = valid_output()
        out["links"] = [{"from": "source:docA", "to": "dataset:D1", "relation": "reports", "status": "confirmed"}]
        with self.assertRaises(ExtractionError):
            validate(out)

    def test_model_links_are_proposals_not_proof(self):
        out = valid_output()
        out["links"] = [{"from": "source:docA", "to": "study:S1", "relation": "reports", "status": "confirmed"}]
        summary = validate(out)["link_summary"]
        self.assertEqual(len(summary["confirmed_links"]), 1)
        self.assertIn("not proven identity", summary["notice"])

    def test_unlinked_sources_have_unknown_linkage_not_independence(self):
        summary = validate(valid_output())["link_summary"]
        self.assertIn(["docA", "docB"], summary["unlinked_source_pairs"])
        self.assertNotIn("independent_source_pairs", summary)
        self.assertIn("unknown linkage", summary["notice"])

    def test_two_reports_of_one_study_are_linked_not_independent(self):
        out = valid_output()
        out["links"] = [
            {"from": "source:docA", "to": "study:S1", "relation": "reports", "status": "confirmed"},
            {"from": "source:docB", "to": "study:S1", "relation": "reports", "status": "proposed"},
        ]
        summary = validate(out)["link_summary"]
        self.assertEqual(summary["unlinked_source_pairs"], [])
        self.assertEqual(len(summary["proposed_links"]), 1)

    def test_unknown_ref_and_finding_source_mismatch_refused(self):
        out = valid_output()
        out["links"] = [{"from": "source:docA", "to": "study:S9", "relation": "reports", "status": "confirmed"}]
        with self.assertRaises(ExtractionError):
            validate(out)
        out = valid_output()
        out["findings"][0]["source_key"] = "docB"  # p0001 belongs to docA
        with self.assertRaises(ExtractionError):
            validate(out)


class AppraisalAndScopeTests(unittest.TestCase):
    def test_appraisal_dimensions_and_unassessed_retention(self):
        dims = [{"dimension_id": "risk_of_bias"}, {"dimension_id": "applicability"}]
        out = valid_output()
        out["appraisal"] = [
            {"dimension_id": "risk_of_bias", "state": "assessed", "judgement": "high", "rationale": "no blinding"},
            {"dimension_id": "applicability", "state": "unassessed", "judgement": "not assessed"},
        ]
        result = validate(out, dims=dims)
        self.assertEqual([a["state"] for a in result["appraisal"]], ["assessed", "unassessed"])
        self.assertEqual(len(result["findings"]), 3)  # a weak appraisal never excludes findings

    def test_missing_dimension_and_dimension_score_refused(self):
        with self.assertRaises(ExtractionError):
            validate(valid_output(), dims=[{"dimension_id": "risk_of_bias"}])
        with self.assertRaises(ExtractionError):
            validate(valid_output(), dims=[{"dimension_id": "x", "score": 1}])

    def test_no_universal_score_or_exclusion_keys(self):
        for key in ("score", "quality_score", "global_score", "excluded"):
            out = valid_output()
            out[key] = 7
            with self.assertRaises(ExtractionError):
                validate(out)

    def test_semantic_wrongness_is_out_of_scope(self):
        out = valid_output()
        out["findings"][0]["statement"] = "Compound Z cured every subject."
        self.assertEqual(validate(out)["findings"][0]["statement"], "Compound Z cured every subject.")


class PrivacyAndFixtureTests(unittest.TestCase):
    def test_errors_are_value_free(self):
        out = valid_output()
        out["findings"][0]["snippet"] = "did not reduce tumour volume"  # paraphrase
        with self.assertRaises(ExtractionError) as ctx:
            validate(out)
        message = str(ctx.exception)
        self.assertNotIn("tumour", message)
        self.assertNotIn("p0001", message)

    def test_instructions_cover_core_grounding_rules(self):
        for phrase in ("EXACT substring", "negation", "missing", "author_interpretation", "coverage", "score", "UNKNOWN"):
            self.assertIn(phrase, EXTRACTION_INSTRUCTIONS)

    def test_example_fixture_is_valid_json_portable_and_isolated(self):
        result = extraction_example()
        json.dumps(result)
        self.assertEqual(result["validation"]["required_items"], result["validation"]["accounted_items"])
        self.assertTrue(result["link_summary"]["confirmed_links"])
        result["summary"] = "mutated"
        self.assertNotEqual(extraction_example()["summary"], "mutated")


class R2RepairTests(unittest.TestCase):
    def test_schema_version_must_be_exact_int(self):
        for bad in (True, False, "1", 1.0, None):
            out = valid_output()
            out["schema_version"] = bad
            with self.assertRaises(ExtractionError):
                validate(out)
        self.assertEqual(validate(valid_output())["schema_version"], 1)

    def test_nonportable_and_untyped_optional_values_rejected(self):
        out = valid_output()
        out["findings"][0]["context"]["note"] = {"nested": 1}  # object note refused
        with self.assertRaises(ExtractionError):
            validate(out)
        out = valid_output()
        out["findings"][0]["notes"] = float("nan")  # non-finite freeform value refused
        with self.assertRaises(ExtractionError):
            validate(out)
        out = valid_output()
        out["findings"][1]["numeric_details"][0]["value"] = {"state": "known", "value": float("inf")}
        with self.assertRaises(ExtractionError):
            validate(out)

    def test_raw_example_roundtrips_and_is_grounded(self):
        raw = raw_extraction_example()
        json.dumps(raw)  # raw example itself must be portable JSON
        self.assertIs(type(raw["schema_version"]), int)
        result = validate_extraction(raw, passages=EXTRACTION_EXAMPLE_PASSAGES, appraisal_dimensions=[])
        by_text = {p["passage_id"]: p["text"] for p in EXTRACTION_EXAMPLE_PASSAGES}
        for item in result["findings"]:
            self.assertIn(item["snippet"], by_text[item["passage_id"]])  # every snippet grounded
        number = result["findings"][1]["numeric_details"][0]["value"]["value"]
        self.assertIn("12.5", by_text[result["findings"][1]["passage_id"]])
        self.assertEqual(number, 12.5)


if __name__ == "__main__":
    unittest.main()
