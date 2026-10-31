"""Focused Step 14 delivery-rule tests; pure rules, no model calls, no storage.

These assert only the mechanical MVP obligations over synthetic fixtures:
incomplete sections block the global review, the 2-round limit blocks, a second
top-up is refused, scope notes require impact approval, ambiguous notes require
clarification, a batch applies once and marks checks stale on factual/citation
edits, and a final approval is refused for unaccepted warnings/unsupported major
claims and invalidated by a later hash change.  They cannot establish scientific
truth.
"""

import unittest

from review_article_agent.delivery.schema import (
    GLOBAL_ROUND_LIMIT,
    DeliveryError,
    apply_notes_batch,
    approval_current,
    evaluate_global,
    final_approval_manifest,
    freshness_checkpoint,
    global_review_ready,
    inspect_omissions,
    plan_notes,
    reopen_for_import,
)


class GlobalReviewReadyTests(unittest.TestCase):
    def test_readiness_matrix(self):
        # P1 merge (S19-PRUNE-A): the original standalone methods are preserved
        # verbatim as subTest cases (labels keep their names for docs grep).
        with self.subTest(case="test_all_complete_is_ready"):
            sections = [
                {"section_id": "s1", "review_status": "complete", "origin_complete": True},
                {"section_id": "s2", "review_status": "complete", "origin_complete": True},
            ]
            self.assertEqual(global_review_ready(sections), {"ready": True, "incomplete": []})

        with self.subTest(case="test_incomplete_section_blocks"):
            sections = [
                {"section_id": "s1", "review_status": "complete", "origin_complete": True},
                {"section_id": "s2", "review_status": "in_progress", "origin_complete": True},
                {"section_id": "s3", "review_status": "complete", "origin_complete": False},
            ]
            result = global_review_ready(sections)
            self.assertFalse(result["ready"])
            self.assertEqual(result["incomplete"], ["s2", "s3"])

        with self.subTest(case="test_empty_is_not_ready"):
            self.assertEqual(global_review_ready([]), {"ready": False, "incomplete": []})

        with self.subTest(case="test_missing_action_requires_decision"):
            self.assertEqual(freshness_checkpoint("2026-01-01", {})["status"], "decision_required")
            self.assertEqual(freshness_checkpoint("2026-01-01", {"action": "extend"})["status"], "decision_required")


class InspectOmissionsTests(unittest.TestCase):
    def test_only_uncited_consequential_contrary_qualifying(self):
        evidence = [
            {"evidence_id": "e1", "source_key": "k1", "direction": "contrary", "consequential": True},
            {"evidence_id": "e2", "source_key": "k2", "direction": "qualifying", "consequential": True},
            {"evidence_id": "e3", "source_key": "k3", "direction": "contrary", "consequential": True},
            {"evidence_id": "e4", "source_key": "k4", "direction": "supporting", "consequential": True},
            {"evidence_id": "e5", "source_key": "k5", "direction": "contrary", "consequential": False},
        ]
        findings = inspect_omissions(["k3", "k4"], evidence)
        self.assertEqual(
            findings,
            [
                {"evidence_id": "e1", "source_key": "k1", "kind": "omitted_contrary", "severity": "major"},
                {"evidence_id": "e2", "source_key": "k2", "kind": "omitted_qualifying", "severity": "major"},
            ],
        )

    def test_cited_contrary_not_omitted(self):
        evidence = [
            {"evidence_id": "e1", "source_key": "k1", "direction": "contrary", "consequential": True},
        ]
        self.assertEqual(inspect_omissions(["k1"], evidence), [])


class EvaluateGlobalTests(unittest.TestCase):
    def test_no_findings_completes(self):
        self.assertEqual(evaluate_global([], 0), {"status": "complete"})
        self.assertEqual(
            evaluate_global([{"severity": "major", "status": "resolved"}], 1),
            {"status": "complete"},
        )

    def test_major_below_limit_needs_repair(self):
        findings = [{"severity": "major", "status": "open"}]
        self.assertEqual(evaluate_global(findings, 1), {"status": "needs_repair"})

    def test_limit_reached_blocks(self):
        findings = [{"severity": "critical", "status": "disputed"}]
        result = evaluate_global(findings, GLOBAL_ROUND_LIMIT)
        self.assertEqual(result["status"], "blocked")
        self.assertIn("required_human_action", result)

    def test_minor_open_does_not_block(self):
        self.assertEqual(evaluate_global([{"severity": "minor", "status": "open"}], 0), {"status": "complete"})

    def test_rounds_over_limit_raises(self):
        with self.assertRaises(DeliveryError):
            evaluate_global([], GLOBAL_ROUND_LIMIT + 1)


class FreshnessCheckpointTests(unittest.TestCase):
    def test_accept_cutoff(self):
        result = freshness_checkpoint("2026-01-01", {"action": "accept_cutoff"})
        self.assertEqual(result["status"], "cutoff_accepted")
        self.assertEqual(result["cutoff"], "2026-01-01")
        self.assertEqual(result["topups_used"], 0)

    def test_authorize_topup_adds_no_papers(self):
        result = freshness_checkpoint(
            "2026-01-01",
            {"action": "authorize_topup", "approval_id": "a1", "query_window_end": "2026-02-01"},
        )
        self.assertEqual(result["status"], "topup_authorized")
        self.assertEqual(result["papers_added"], 0)
        self.assertEqual(result["topups_used"], 1)

    def test_authorize_topup_requires_approval_id(self):
        with self.assertRaises(DeliveryError):
            freshness_checkpoint("2026-01-01", {"action": "authorize_topup"})

    def test_second_topup_refused_but_cutoff_allowed(self):
        prior = {"topups_used": 1}
        with self.assertRaises(DeliveryError):
            freshness_checkpoint("2026-01-01", {"action": "authorize_topup", "approval_id": "a2"}, prior=prior)
        self.assertEqual(
            freshness_checkpoint("2026-01-01", {"action": "accept_cutoff"}, prior=prior)["status"],
            "cutoff_accepted",
        )


class ReopenForImportTests(unittest.TestCase):
    def test_dependents_reopened_others_untouched(self):
        artifacts = [
            {"artifact_id": "a1", "revision_id": "r1", "depends_on_sources": ["k1", "k2"]},
            {"artifact_id": "a2", "revision_id": "r2", "depends_on_sources": ["k3"]},
        ]
        self.assertEqual(
            reopen_for_import(artifacts, ["k2"]),
            [{"artifact_id": "a1", "revision_id": "r1", "reason": "new_import"}],
        )

    def test_no_intersection_nothing_reopened(self):
        artifacts = [{"artifact_id": "a1", "revision_id": "r1", "depends_on_sources": ["k9"]}]
        self.assertEqual(reopen_for_import(artifacts, ["k1"]), [])


class PlanNotesTests(unittest.TestCase):
    def test_scope_note_requires_impact_approval(self):
        result = plan_notes([{"note_id": "n1", "text": "extend scope", "category": "scope"}])
        item = result["items"][0]
        self.assertEqual(item["action"], "impact_approval_required")
        self.assertTrue(result["requires_approval"])
        self.assertNotEqual(item["action"], "local_edit")

    def test_source_note_requires_impact_approval(self):
        item = plan_notes([{"note_id": "n1", "text": "add source", "category": "source"}])["items"][0]
        self.assertEqual(item["action"], "impact_approval_required")
        self.assertEqual(item["route_to"], "step05_discovery/step06_acquisition")

    def test_ambiguous_claim_note_requires_clarification(self):
        item = plan_notes([{"note_id": "n1", "text": "this seems wrong", "category": "claim"}])["items"][0]
        self.assertEqual(item["action"], "clarification_required")

    def test_missing_category_requires_clarification(self):
        item = plan_notes([{"note_id": "n1", "text": "please fix"}])["items"][0]
        self.assertEqual(item["action"], "clarification_required")

    def test_wording_and_targeted_claim(self):
        items = plan_notes(
            [
                {"note_id": "n1", "text": "tighten this sentence", "category": "wording"},
                {"note_id": "n2", "text": "support claim", "category": "claim", "target": {"paragraph_id": "p1"}},
            ]
        )["items"]
        self.assertEqual(items[0]["action"], "local_edit")
        self.assertEqual(items[1]["action"], "claim_edit")
        self.assertEqual(items[1]["revalidate"], ["support", "origin"])


def _state():
    return {
        "paragraphs": {
            "p1": {"text": "The effect was 5 units [@a].", "checks": "valid"},
        },
        "applied_batches": [],
    }


class ApplyNotesBatchTests(unittest.TestCase):
    def test_applied_once(self):
        state = apply_notes_batch(
            _state(), "b1", "appr1", [{"paragraph_id": "p1", "new_text": "The effect was 5 units [@a] overall.", "kind": "wording"}]
        )
        self.assertEqual(state["status"], "returned_to_user")
        self.assertEqual(state["applied_batches"], ["b1"])
        self.assertEqual(state["paragraphs"]["p1"]["checks"], "valid")
        with self.assertRaises(DeliveryError):
            apply_notes_batch(state, "b1", "appr1", [])

    def test_requires_approval_id(self):
        with self.assertRaises(DeliveryError):
            apply_notes_batch(_state(), "b1", "", [])

    def test_citation_edit_marks_stale(self):
        state = apply_notes_batch(
            _state(), "b1", "appr1", [{"paragraph_id": "p1", "new_text": "The effect was 5 units [@b].", "kind": "citation"}]
        )
        self.assertEqual(state["paragraphs"]["p1"]["checks"], "stale")
        self.assertEqual(state["revalidation_required"], ["p1"])

    def test_wording_with_changed_number_treated_as_claim(self):
        state = apply_notes_batch(
            _state(), "b1", "appr1", [{"paragraph_id": "p1", "new_text": "The effect was 9 units [@a].", "kind": "wording"}]
        )
        self.assertEqual(state["paragraphs"]["p1"]["checks"], "stale")
        self.assertEqual(state["revalidation_required"], ["p1"])

    def test_input_state_not_mutated(self):
        original = _state()
        apply_notes_batch(
            original, "b1", "appr1", [{"paragraph_id": "p1", "new_text": "Changed.", "kind": "claim"}]
        )
        self.assertEqual(original["paragraphs"]["p1"]["checks"], "valid")
        self.assertEqual(original["applied_batches"], [])


def _warnings():
    return [{"warning_id": "w1", "claim_id": "c1", "text": "secondary origin"}]


class FinalApprovalManifestTests(unittest.TestCase):
    def test_manifest_emitted_with_secondary_accepted(self):
        manifest = final_approval_manifest(
            "rev1", {"a1": "sha1"}, _warnings(), ["w1"], [], "human:alice"
        )
        self.assertTrue(manifest["approved"])
        self.assertEqual(manifest["accepted_warnings"][0]["status"], "secondary_accepted")
        import json

        self.assertNotIn("primary_verified", json.dumps(manifest))
        self.assertNotIn('"verified"', json.dumps(manifest))

    def test_unaccepted_warning_blocks(self):
        with self.assertRaises(DeliveryError):
            final_approval_manifest("rev1", {"a1": "sha1"}, _warnings(), [], [], "human:alice")

    def test_unsupported_major_claim_blocks(self):
        with self.assertRaises(DeliveryError):
            final_approval_manifest(
                "rev1", {"a1": "sha1"}, _warnings(), ["w1"],
                [{"claim_id": "c1"}], "human:alice",
            )


class ApprovalCurrentTests(unittest.TestCase):
    def test_current_when_hashes_match(self):
        manifest = {"artifact_hashes": {"a1": "sha1", "a2": "sha2"}}
        self.assertEqual(
            approval_current(manifest, {"a1": "sha1", "a2": "sha2"}),
            {"current": True, "changed": []},
        )

    def test_changed_hash_invalidates(self):
        manifest = {"artifact_hashes": {"a1": "sha1"}}
        self.assertEqual(approval_current(manifest, {"a1": "shaX"}), {"current": False, "changed": ["a1"]})

    def test_missing_and_extra_invalidate(self):
        manifest = {"artifact_hashes": {"a1": "sha1"}}
        result = approval_current(manifest, {"a2": "sha2"})
        self.assertFalse(result["current"])
        self.assertEqual(sorted(result["changed"]), ["a1", "a2"])


if __name__ == "__main__":
    unittest.main()
