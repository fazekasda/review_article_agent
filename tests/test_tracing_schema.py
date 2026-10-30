"""Focused S13-T1 regression suite; pure tracing rules, no model calls, no storage.

These assert only the mechanical Step 13 obligations over synthetic fixtures built
in-file: direct (not earliest) origin tracing, attribution classification, support
assessment with contradicts/qualifies, required per-project limits, cycle/budget
stops, correction proposals never applied, warned secondary attribution, and a
fingerprint that changes with claim/source/policy.  They cannot establish
scientific truth.
"""

import json
import unittest

from review_article_agent.tracing.schema import (
    ATTRIBUTIONS,
    TracingError,
    assess_support,
    classify_attribution,
    trace_atom,
    trace_claim,
    trace_fingerprint,
    validate_policy,
)

POLICY = {
    "max_hops": 4,
    "max_sources_per_claim": 5,
    "max_sources_per_project": 20,
    "max_resolution_retries": 1,
}


def _ref(ref_id, *, doi=None, pmid=None, text="ref text"):
    identifiers = {}
    if doi:
        identifiers["doi"] = doi
    if pmid:
        identifiers["pmid"] = pmid
    return {"ref_id": ref_id, "text": text, "identifiers": identifiers}


def _link(passage_id, marker, state, ref_ids):
    return {
        "passage_id": passage_id,
        "marker": marker,
        "state": state,
        "resolved_ref_ids": list(ref_ids),
    }


def _src(key, sha, passages, references=(), links=()):
    return {
        "source_key": key,
        "source": {"sha256": sha},
        "passages": [{"passage_id": pid, "text": text} for pid, text in passages],
        "references": list(references),
        "citation_links": list(links),
    }


def _atom(atom_id, text, passage_ids):
    return {"atom_id": atom_id, "text": text, "passage_ids": list(passage_ids)}


class _Acquire:
    """Recording fake acquisition port."""

    def __init__(self, mapping=None, default=None):
        self.mapping = mapping or {}
        self.default = default or {"status": "unavailable"}
        self.calls = []

    def __call__(self, request):
        self.calls.append(request)
        entry = self.mapping.get(request["ref_id"])
        if callable(entry):
            return entry(request)
        if entry is not None:
            return entry
        return self.default


class ValidatePolicyTests(unittest.TestCase):
    def test_configured(self):
        self.assertEqual(validate_policy(dict(POLICY)), {"status": "configured", "missing": []})

    def test_missing_reported(self):
        policy = dict(POLICY)
        del policy["max_hops"]
        result = validate_policy(policy)
        self.assertEqual(result["status"], "configuration_required")
        self.assertIn("max_hops", result["missing"])

    def test_unset_policy_is_configuration_required(self):
        self.assertEqual(validate_policy({})["status"], "configuration_required")
        self.assertEqual(validate_policy(None)["status"], "configuration_required")

    def test_malformed_types_raise(self):
        for bad in ({"max_hops": True, "max_sources_per_claim": 1,
                     "max_sources_per_project": 1, "max_resolution_retries": 1},
                    {"max_hops": -1, "max_sources_per_claim": 1,
                     "max_sources_per_project": 1, "max_resolution_retries": 1},
                    {"max_hops": 1.5, "max_sources_per_claim": 1,
                     "max_sources_per_project": 1, "max_resolution_retries": 1},
                    "not-a-policy"):
            with self.assertRaises(TracingError):
                validate_policy(bad)


class ClassifyAttributionTests(unittest.TestCase):
    def test_secondary_with_resolved_marker(self):
        review = _src(
            "review", "shaR",
            [("p1", "According to the literature [1], compound X inhibits Y.")],
            [_ref("r1", doi="10.1/x")],
            [_link("p1", "[1]", "resolved", ["r1"])],
        )
        result = classify_attribution(_atom("a1", "Compound X inhibits Y.", ["p1"]), review)
        self.assertEqual(result["attribution"], "secondary")
        self.assertEqual(result["candidate_ref_ids"], ["r1"])

    def test_method_citation_is_not_candidate(self):
        review = _src(
            "review", "shaR",
            [("p1", "We found that X works using the method of [1].")],
            [_ref("r1", doi="10.1/x")],
            [_link("p1", "[1]", "resolved", ["r1"])],
        )
        result = classify_attribution(_atom("a1", "X works.", ["p1"]), review)
        self.assertEqual(result["attribution"], "own_result")
        self.assertEqual(result["candidate_ref_ids"], [])
        self.assertIn("r1", result["method_ref_ids"])

    def test_conceptual(self):
        review = _src("review", "shaR", [("p1", "We propose a new framework for Y.")])
        result = classify_attribution(_atom("a1", "Y.", ["p1"]), review)
        self.assertEqual(result["attribution"], "conceptual")

    def test_reused_data_note(self):
        review = _src(
            "review", "shaR",
            [("p1", "Our analysis of the dataset from [1] shows Z.")],
            [_ref("r1", doi="10.1/x")],
            [_link("p1", "[1]", "resolved", ["r1"])],
        )
        result = classify_attribution(_atom("a1", "Z.", ["p1"]), review)
        self.assertEqual(result["attribution"], "own_result")
        self.assertIn("reused_data", result["notes"])
        self.assertIn("r1", result["method_ref_ids"])

    def test_mixed_own_and_secondary(self):
        review = _src(
            "review", "shaR",
            [("p1", "We found X, which was previously reported [1].")],
            [_ref("r1", doi="10.1/x")],
            [_link("p1", "[1]", "resolved", ["r1"])],
        )
        result = classify_attribution(_atom("a1", "X.", ["p1"]), review)
        self.assertEqual(result["attribution"], "mixed")
        self.assertEqual(result["candidate_ref_ids"], ["r1"])

    def test_unclear_missing_markers_not_proof(self):
        review = _src("review", "shaR", [("p1", "The sky is blue.")])
        result = classify_attribution(_atom("a1", "The sky is blue.", ["p1"]), review)
        self.assertEqual(result["attribution"], "unclear")
        self.assertIn("no_markers_not_proof_of_originality", result["notes"])

    def test_nearby_citation_in_other_passage_ignored(self):
        review = _src(
            "review", "shaR",
            [("p1", "The sky is blue."), ("p2", "According to [1], X.")],
            [_ref("r1", doi="10.1/x")],
            [_link("p2", "[1]", "resolved", ["r1"])],
        )
        result = classify_attribution(_atom("a1", "The sky is blue.", ["p1"]), review)
        self.assertEqual(result["attribution"], "unclear")
        self.assertEqual(result["candidate_ref_ids"], [])


class AssessSupportTests(unittest.TestCase):
    def test_not_found(self):
        source = _src("s", "sha", [("p1", "Entirely unrelated weather today.")])
        self.assertEqual(assess_support("Compound X inhibits Y.", source)["verdict"], "not_found")

    def test_supports(self):
        source = _src("s", "sha", [("p1", "We found compound X inhibits Y.")])
        result = assess_support("Compound X inhibits Y.", source)
        self.assertEqual(result["verdict"], "supports")
        self.assertEqual(result["passage_id"], "p1")

    def test_contradicts(self):
        source = _src("s", "sha", [("p1", "We found no significant effect of X.")])
        self.assertEqual(assess_support("X has an effect.", source)["verdict"], "contradicts")

    def test_qualifies(self):
        source = _src("s", "sha", [("p1", "We found an effect in mice.")])
        self.assertEqual(assess_support("X has an effect.", source)["verdict"], "qualifies")


class TraceAtomTests(unittest.TestCase):
    def test_review_to_primary(self):
        review = _src(
            "review", "shaR",
            [("p1", "According to the literature [1], compound X inhibits Y.")],
            [_ref("r1", doi="10.1/x")],
            [_link("p1", "[1]", "resolved", ["r1"])],
        )
        primary = _src("primary", "shaP", [("p1", "We found that compound X inhibits Y in our assay.")])
        acquire = _Acquire({"r1": {"status": "acquired", "source": primary}})
        trace = trace_atom(
            _atom("a1", "Compound X inhibits Y.", ["p1"]),
            root_source=review, policy=POLICY, project_sources_used=0,
            acquire=acquire, depth0_verdict="supported",
        )
        self.assertEqual(trace["outcome"], "origin_traced")
        self.assertEqual(trace["cite_source_key"], "primary")
        self.assertEqual(trace["sources_used"], 1)
        self.assertEqual(len(acquire.calls), 1)
        self.assertFalse(trace["requires_final_human_approval"])
        depth1 = [n for n in trace["graph"]["nodes"] if n["depth"] == 1][0]
        self.assertEqual(depth1["attribution"], "own_result")
        self.assertEqual(depth1["corpus_admission"], "not_admitted")

    def test_multi_hop_chain(self):
        root = _src(
            "review", "shaR",
            [("p1", "As reported [1], X causes Z.")],
            [_ref("r1", doi="10.a")],
            [_link("p1", "[1]", "resolved", ["r1"])],
        )
        mid = _src(
            "mid", "shaM",
            [("p1", "It was previously reported [2] that X causes Z.")],
            [_ref("r2", doi="10.b")],
            [_link("p1", "[2]", "resolved", ["r2"])],
        )
        primary = _src("primary", "shaP", [("p1", "We observed that X causes Z.")])
        acquire = _Acquire({
            "r1": {"status": "acquired", "source": mid},
            "r2": {"status": "acquired", "source": primary},
        })
        trace = trace_atom(
            _atom("a1", "X causes Z.", ["p1"]),
            root_source=root, policy=POLICY, project_sources_used=0,
            acquire=acquire, depth0_verdict="supported",
        )
        self.assertEqual(trace["outcome"], "origin_traced")
        self.assertEqual(trace["cite_source_key"], "primary")
        self.assertEqual(trace["sources_used"], 2)
        self.assertEqual(len(acquire.calls), 2)

    def test_cycle_detected(self):
        root = _src(
            "A", "shaA",
            [("p1", "As reported [1], Q causes R.")],
            [_ref("r1", doi="10.a")],
            [_link("p1", "[1]", "resolved", ["r1"])],
        )
        other = _src(
            "B", "shaB",
            [("p1", "Previously reported [2] that Q causes R.")],
            [_ref("r2", doi="10.b")],
            [_link("p1", "[2]", "resolved", ["r2"])],
        )
        acquire = _Acquire({
            "r1": {"status": "acquired", "source": other},
            "r2": {"status": "acquired", "source": root},
        })
        trace = trace_atom(
            _atom("a1", "Q causes R.", ["p1"]),
            root_source=root, policy=POLICY, project_sources_used=0,
            acquire=acquire, depth0_verdict="supported",
        )
        self.assertIn("cycle", trace["stopping_reasons"])
        self.assertEqual(trace["outcome"], "secondary_warning")

    def test_multiple_candidates_one_supports(self):
        review = _src(
            "review", "shaR",
            [("p1", "According to [1,2], M inhibits N.")],
            [_ref("r1", doi="10.a"), _ref("r2", doi="10.b")],
            [_link("p1", "[1,2]", "resolved", ["r1", "r2"])],
        )
        noise = _src("noise", "shaN", [("p1", "The weather was cold and rainy today.")])
        good = _src("good", "shaG", [("p1", "We found M inhibits N.")])
        acquire = _Acquire({
            "r1": {"status": "acquired", "source": noise},
            "r2": {"status": "acquired", "source": good},
        })
        trace = trace_atom(
            _atom("a1", "M inhibits N.", ["p1"]),
            root_source=review, policy=POLICY, project_sources_used=0,
            acquire=acquire, depth0_verdict="supported",
        )
        self.assertEqual(trace["outcome"], "origin_traced")
        self.assertIn("not_supported_upstream", trace["stopping_reasons"])

    def test_missing_identifier_no_acquire(self):
        review = _src(
            "review", "shaR",
            [("p1", "According to [1], P causes Q.")],
            [_ref("r1")],
            [_link("p1", "[1]", "resolved", ["r1"])],
        )
        acquire = _Acquire({})
        trace = trace_atom(
            _atom("a1", "P causes Q.", ["p1"]),
            root_source=review, policy=POLICY, project_sources_used=0,
            acquire=acquire, depth0_verdict="supported",
        )
        self.assertIn("missing_identifier", trace["stopping_reasons"])
        self.assertEqual(len(acquire.calls), 0)

    def test_full_text_unavailable_after_retries(self):
        policy = dict(POLICY)
        policy["max_resolution_retries"] = 2
        review = _src(
            "review", "shaR",
            [("p1", "According to [1], P causes Q.")],
            [_ref("r1", doi="10.1/x")],
            [_link("p1", "[1]", "resolved", ["r1"])],
        )
        acquire = _Acquire({}, default={"status": "unavailable"})
        trace = trace_atom(
            _atom("a1", "P causes Q.", ["p1"]),
            root_source=review, policy=policy, project_sources_used=0,
            acquire=acquire, depth0_verdict="supported",
        )
        self.assertEqual(len(acquire.calls), 1 + policy["max_resolution_retries"])
        self.assertIn("full_text_unavailable", trace["stopping_reasons"])
        self.assertEqual(trace["outcome"], "secondary_warning")

    def test_pending_disposition(self):
        review = _src(
            "review", "shaR",
            [("p1", "According to [1], P causes Q.")],
            [_ref("r1", doi="10.1/x")],
            [_link("p1", "[1]", "resolved", ["r1"])],
        )
        acquire = _Acquire({"r1": {"status": "pending_disposition"}})
        trace = trace_atom(
            _atom("a1", "P causes Q.", ["p1"]),
            root_source=review, policy=POLICY, project_sources_used=0,
            acquire=acquire, depth0_verdict="supported",
        )
        self.assertIn("awaiting_disposition", trace["stopping_reasons"])
        self.assertEqual(len(acquire.calls), 1)

    def test_own_result_origin_at_depth_zero(self):
        review = _src("review", "shaR", [("p1", "We found that X causes Y.")])
        acquire = _Acquire({})
        trace = trace_atom(
            _atom("a1", "X causes Y.", ["p1"]),
            root_source=review, policy=POLICY, project_sources_used=0,
            acquire=acquire, depth0_verdict="supported",
        )
        self.assertEqual(trace["outcome"], "origin_traced")
        self.assertEqual(trace["cite_source_key"], "review")
        self.assertEqual(len(acquire.calls), 0)

    def test_method_citation_makes_no_acquire_call(self):
        review = _src(
            "review", "shaR",
            [("p1", "We found that X works using the method of [1].")],
            [_ref("r1", doi="10.1/x")],
            [_link("p1", "[1]", "resolved", ["r1"])],
        )
        acquire = _Acquire({})
        trace = trace_atom(
            _atom("a1", "X works.", ["p1"]),
            root_source=review, policy=POLICY, project_sources_used=0,
            acquire=acquire, depth0_verdict="supported",
        )
        self.assertEqual(trace["outcome"], "origin_traced")
        self.assertEqual(len(acquire.calls), 0)

    def test_conceptual_framework_origin_at_depth_zero(self):
        review = _src("review", "shaR", [("p1", "We propose a new framework for Y.")])
        acquire = _Acquire({})
        trace = trace_atom(
            _atom("a1", "Y.", ["p1"]),
            root_source=review, policy=POLICY, project_sources_used=0,
            acquire=acquire, depth0_verdict="supported",
        )
        self.assertEqual(trace["outcome"], "origin_traced")
        self.assertEqual(trace["cite_source_key"], "review")

    def test_primary_contradicts_proposes_correction(self):
        trace = self._correction_trace("We found no significant effect of X.")
        self.assertEqual(trace["outcome"], "correction_proposed")
        self.assertEqual(trace["corrections"][0]["kind"], "contradicts")
        self.assertFalse(trace["corrections"][0]["applied"])

    def test_primary_qualifies_proposes_correction(self):
        trace = self._correction_trace("We found an effect in mice.")
        self.assertEqual(trace["outcome"], "correction_proposed")
        self.assertEqual(trace["corrections"][0]["kind"], "qualifies")
        self.assertFalse(trace["corrections"][0]["applied"])

    def _correction_trace(self, primary_text):
        review = _src(
            "review", "shaR",
            [("p1", "According to [1], X has an effect.")],
            [_ref("r1", doi="10.1/x")],
            [_link("p1", "[1]", "resolved", ["r1"])],
        )
        primary = _src("primary", "shaP", [("p1", primary_text)])
        acquire = _Acquire({"r1": {"status": "acquired", "source": primary}})
        return trace_atom(
            _atom("a1", "X has an effect.", ["p1"]),
            root_source=review, policy=POLICY, project_sources_used=0,
            acquire=acquire, depth0_verdict="supported",
        )

    def test_budget_limit_secondary_warning(self):
        policy = dict(POLICY)
        policy["max_sources_per_claim"] = 0
        review = _src(
            "review", "shaR",
            [("p1", "According to [1], P causes Q.")],
            [_ref("r1", doi="10.1/x")],
            [_link("p1", "[1]", "resolved", ["r1"])],
        )
        acquire = _Acquire({})
        trace = trace_atom(
            _atom("a1", "P causes Q.", ["p1"]),
            root_source=review, policy=policy, project_sources_used=0,
            acquire=acquire, depth0_verdict="supported",
        )
        self.assertEqual(trace["outcome"], "secondary_warning")
        self.assertEqual(trace["cite_source_key"], "review")
        self.assertTrue(trace["requires_final_human_approval"])
        self.assertIn("claim_source_budget", trace["stopping_reasons"])
        self.assertEqual(len(acquire.calls), 0)

    def test_project_budget_limit(self):
        review = _src(
            "review", "shaR",
            [("p1", "According to [1], P causes Q.")],
            [_ref("r1", doi="10.1/x")],
            [_link("p1", "[1]", "resolved", ["r1"])],
        )
        acquire = _Acquire({})
        trace = trace_atom(
            _atom("a1", "P causes Q.", ["p1"]),
            root_source=review, policy=POLICY, project_sources_used=20,
            acquire=acquire, depth0_verdict="supported",
        )
        self.assertIn("project_source_budget", trace["stopping_reasons"])
        self.assertEqual(len(acquire.calls), 0)

    def test_max_hops_stop(self):
        policy = dict(POLICY)
        policy["max_hops"] = 0
        review = _src(
            "review", "shaR",
            [("p1", "According to [1], P causes Q.")],
            [_ref("r1", doi="10.1/x")],
            [_link("p1", "[1]", "resolved", ["r1"])],
        )
        acquire = _Acquire({})
        trace = trace_atom(
            _atom("a1", "P causes Q.", ["p1"]),
            root_source=review, policy=policy, project_sources_used=0,
            acquire=acquire, depth0_verdict="supported",
        )
        self.assertIn("max_hops", trace["stopping_reasons"])
        self.assertEqual(len(acquire.calls), 0)

    def test_depth0_unsupported_blocker(self):
        review = _src(
            "review", "shaR",
            [("p1", "According to [1], P causes Q.")],
            [_ref("r1", doi="10.1/x")],
            [_link("p1", "[1]", "resolved", ["r1"])],
        )
        acquire = _Acquire({}, default={"status": "unavailable"})
        trace = trace_atom(
            _atom("a1", "P causes Q.", ["p1"]),
            root_source=review, policy=POLICY, project_sources_used=0,
            acquire=acquire, depth0_verdict="unsupported",
        )
        self.assertEqual(trace["outcome"], "unsupported")
        self.assertFalse(trace["requires_final_human_approval"])
        self.assertIsNone(trace["cite_source_key"])

    def test_unset_policy_configuration_required_no_acquire(self):
        review = _src(
            "review", "shaR",
            [("p1", "According to [1], P causes Q.")],
            [_ref("r1", doi="10.1/x")],
            [_link("p1", "[1]", "resolved", ["r1"])],
        )
        acquire = _Acquire({})
        trace = trace_atom(
            _atom("a1", "P causes Q.", ["p1"]),
            root_source=review, policy={}, project_sources_used=0,
            acquire=acquire, depth0_verdict="supported",
        )
        self.assertEqual(trace["outcome"], "configuration_required")
        self.assertEqual(len(acquire.calls), 0)
        self.assertEqual(trace["requests"], [])

    def test_depth0_unsupported_overrides_own_result_origin(self):
        # Depth-0 is an own result, but Step 12 did not verify support: the atom
        # must be "unsupported", never origin_traced.
        review = _src("review", "shaR", [("p1", "We found that X causes Y.")])
        acquire = _Acquire({})
        trace = trace_atom(
            _atom("a1", "X causes Y.", ["p1"]),
            root_source=review, policy=POLICY, project_sources_used=0,
            acquire=acquire, depth0_verdict="unsupported",
        )
        self.assertEqual(trace["outcome"], "unsupported")
        self.assertIsNone(trace["cite_source_key"])
        self.assertFalse(trace["requires_final_human_approval"])

    def test_unresolved_marker_records_stop_reason(self):
        review = _src(
            "review", "shaR",
            [("p1", "According to [1], X inhibits Y.")],
            [_ref("r1", doi="10.1/x")],
            [_link("p1", "[1]", "unresolved", ["r1"])],
        )
        acquire = _Acquire({})
        trace = trace_atom(
            _atom("a1", "X inhibits Y.", ["p1"]),
            root_source=review, policy=POLICY, project_sources_used=0,
            acquire=acquire, depth0_verdict="supported",
        )
        self.assertIn("unresolved_reference", trace["stopping_reasons"])
        self.assertEqual(len(acquire.calls), 0)


class TraceClaimTests(unittest.TestCase):
    def test_compound_claim_mixed_outcomes(self):
        review = _src(
            "review", "shaR",
            [("p1", "We found that Q causes R."), ("p2", "The sky is blue.")],
        )
        acquire = _Acquire({})
        claim = {
            "claim_id": "c1",
            "atoms": [
                _atom("a1", "Q causes R.", ["p1"]),
                _atom("a2", "The sky is blue.", ["p2"]),
            ],
        }
        trace = trace_claim(
            claim, root_source=review, policy=POLICY, project_sources_used=0,
            acquire=acquire, depth0_verdict="supported",
        )
        self.assertEqual(trace["claim_id"], "c1")
        self.assertEqual(trace["outcome"], "mixed_outcomes")
        self.assertEqual(trace["atoms"][0]["outcome"], "origin_traced")
        self.assertEqual(trace["atoms"][1]["outcome"], "secondary_warning")

    def test_claim_budget_shared_across_atoms(self):
        policy = dict(POLICY)
        policy["max_sources_per_claim"] = 1
        review = _src(
            "review", "shaR",
            [
                ("p1", "As reported [1], P causes Q."),
                ("p2", "As reported [2], M causes N."),
            ],
            [_ref("r1", doi="10.a"), _ref("r2", doi="10.b")],
            [
                _link("p1", "[1]", "resolved", ["r1"]),
                _link("p2", "[2]", "resolved", ["r2"]),
            ],
        )
        acquire = _Acquire({
            "r1": {"status": "acquired", "source": _src("primary1", "sha1", [("p1", "We found P causes Q.")])},
            "r2": {"status": "acquired", "source": _src("primary2", "sha2", [("p1", "We found M causes N.")])},
        })
        claim = {
            "claim_id": "c1",
            "atoms": [
                _atom("a1", "P causes Q.", ["p1"]),
                _atom("a2", "M causes N.", ["p2"]),
            ],
        }
        trace = trace_claim(
            claim, root_source=review, policy=policy, project_sources_used=0,
            acquire=acquire, depth0_verdict="supported",
        )
        self.assertEqual(trace["atoms"][0]["outcome"], "origin_traced")
        self.assertIn("claim_source_budget", trace["atoms"][1]["stopping_reasons"])
        self.assertEqual(trace["sources_used"], 1)
        self.assertEqual(len(acquire.calls), 1)


class FingerprintTests(unittest.TestCase):
    def test_fingerprint_changes(self):
        review = _src("review", "shaR", [("p1", "According to [1], P causes Q.")])
        atom = _atom("a1", "P causes Q.", ["p1"])
        base = trace_fingerprint(atom, review, POLICY, {"u": "d"})
        self.assertNotEqual(base, trace_fingerprint(_atom("a1", "P causes Z.", ["p1"]), review, POLICY, {"u": "d"}))
        other = _src("review2", "shaR2", [("p1", "According to [1], P causes Q.")])
        self.assertNotEqual(base, trace_fingerprint(atom, other, POLICY, {"u": "d"}))
        changed = dict(POLICY)
        changed["max_hops"] = 9
        self.assertNotEqual(base, trace_fingerprint(atom, review, changed, {"u": "d"}))
        self.assertNotEqual(base, trace_fingerprint(atom, review, POLICY, {"u": "e"}))


class OutputHygieneTests(unittest.TestCase):
    def test_json_never_contains_primary_verified(self):
        review = _src(
            "review", "shaR",
            [("p1", "According to [1], P causes Q.")],
            [_ref("r1", doi="10.1/x")],
            [_link("p1", "[1]", "resolved", ["r1"])],
        )
        acquire = _Acquire({}, default={"status": "unavailable"})
        for verdict in ("supported", "unsupported"):
            trace = trace_atom(
                _atom("a1", "P causes Q.", ["p1"]),
                root_source=review, policy=POLICY, project_sources_used=0,
                acquire=acquire, depth0_verdict=verdict,
            )
            self.assertNotIn("primary_verified", json.dumps(trace))


class AttributionConstantTests(unittest.TestCase):
    def test_attributions_present(self):
        for name in ("own_result", "conceptual", "secondary", "mixed", "unclear"):
            self.assertIn(name, ATTRIBUTIONS)


if __name__ == "__main__":
    unittest.main()
