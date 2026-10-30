"""S15 fault-injection suite: the seven deliberate faults of AC2.

Covers ``docs/implementation/15-release.md`` acceptance criterion 2: inject an
unavailable source, a false exclusion, an omitted finding, an unsupported claim,
a false critic objection, an ambiguous citation chain, and a post-approval edit,
and verify both the **required stop** (blocked status / refusal / open blocking
finding / required human action) and the **correction path** (after the
documented human or repair action the stop clears or the correction is routed).

One test method per injection.  Each method names the injection and the owning
step in its docstring, and reuses the existing synthetic fakes/fixtures by
import (no duplication of provider or storage fakes).

**Fixture disclosure:** every source is a synthetic in-file document and every
model, reviewer, writer, critic, and acquisition port is a local fake.  No real
model, network, provider, or OpenCode session call is made.

Run::

    PYTHONPATH=src python3 -m unittest tests.test_step15_injected_faults -v
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
TESTS = Path(__file__).resolve().parent
if str(TESTS) not in sys.path:
    sys.path.insert(0, str(TESTS))

# -- tracing (Step 13) schema-level fakes/fixtures -------------------------- #
from test_tracing_schema import (  # noqa: E402
    POLICY,
    _Acquire,
    _atom,
    _link,
    _ref,
    _src,
)
from review_article_agent.tracing.schema import trace_atom  # noqa: E402

# -- screening (Step 07) fixture -------------------------------------------- #
from test_acquisition_manifest import ManifestBase  # noqa: E402
from test_screening import config as screening_config, response as screening_response  # noqa: E402
from review_article_agent.parsing import ParsingRepository  # noqa: E402
from review_article_agent.screening import ScreeningError, ScreeningRepository  # noqa: E402

# -- review (Step 12) fixture ----------------------------------------------- #
from test_review_workflow import (  # noqa: E402
    FakeCritic,
    FakeWriter,
    ReviewBase,
    _clean_draft,
    _major,
)

# -- delivery (Step 14) fixture --------------------------------------------- #
from test_delivery_workflow import (  # noqa: E402
    CORPUS,
    DeliveryBase,
    _ref as _delivery_ref,
    _reviewer,
    _writer,
)
from review_article_agent.delivery.schema import DeliveryError  # noqa: E402


class UnavailableSourceTests(unittest.TestCase):
    def test_unavailable_source_stops_and_a_later_supply_corrects(self):
        """Inject an unavailable cited source (owner: Step 06 acquisition / Step 13 tracing)."""
        policy = dict(POLICY)
        policy["max_resolution_retries"] = 2
        review = _src(
            "review",
            "shaR",
            [("p1", "According to [1], compound X inhibits Y.")],
            [_ref("r1", doi="10.1/x")],
            [_link("p1", "[1]", "resolved", ["r1"])],
        )
        atom = _atom("a1", "Compound X inhibits Y.", ["p1"])

        # required stop: retries are exhausted, the origin is unresolved, and a
        # final human approval is required.
        unavailable = _Acquire({}, default={"status": "unavailable"})
        trace = trace_atom(
            atom,
            root_source=review,
            policy=policy,
            project_sources_used=0,
            acquire=unavailable,
            depth0_verdict="supported",
        )
        self.assertEqual(len(unavailable.calls), 1 + policy["max_resolution_retries"])
        self.assertIn("full_text_unavailable", trace["stopping_reasons"])
        self.assertEqual(trace["outcome"], "secondary_warning")
        self.assertTrue(trace["requires_final_human_approval"])

        # correction: once the source becomes available the chain resolves.
        primary = _src("primary", "shaP", [("p1", "We found that compound X inhibits Y in our assay.")])
        supplied = _Acquire({"r1": {"status": "acquired", "source": primary}})
        fixed = trace_atom(
            atom,
            root_source=review,
            policy=policy,
            project_sources_used=0,
            acquire=supplied,
            depth0_verdict="supported",
        )
        self.assertEqual(fixed["outcome"], "origin_traced")
        self.assertFalse(fixed["requires_final_human_approval"])
        self.assertNotIn("full_text_unavailable", fixed["stopping_reasons"])


class AmbiguousCitationChainTests(unittest.TestCase):
    def test_ambiguous_citation_chain_requires_human_and_resolves(self):
        """Inject an ambiguous citation chain (owner: Step 13 tracing)."""
        review = _src(
            "review",
            "shaR",
            [("p1", "According to [1,2], M inhibits N.")],
            [_ref("r1", doi="10.a"), _ref("r2", doi="10.b")],
            [_link("p1", "[1,2]", "resolved", ["r1", "r2"])],
        )
        atom = _atom("a1", "M inhibits N.", ["p1"])

        # required stop: neither candidate link resolves the origin; the chain is
        # ambiguous and a final human approval is required.
        noise_a = _src("noise1", "shaN1", [("p1", "The weather was cold and rainy today.")])
        noise_b = _src("noise2", "shaN2", [("p1", "Entirely unrelated weather today.")])
        ambiguous = _Acquire(
            {
                "r1": {"status": "acquired", "source": noise_a},
                "r2": {"status": "acquired", "source": noise_b},
            }
        )
        trace = trace_atom(
            atom,
            root_source=review,
            policy=POLICY,
            project_sources_used=0,
            acquire=ambiguous,
            depth0_verdict="supported",
        )
        self.assertIn("not_supported_upstream", trace["stopping_reasons"])
        self.assertEqual(trace["outcome"], "secondary_warning")
        self.assertTrue(trace["requires_final_human_approval"])

        # correction: one candidate link resolves to the supporting origin.
        good = _src("good", "shaG", [("p1", "We found M inhibits N.")])
        resolved = _Acquire(
            {
                "r1": {"status": "acquired", "source": noise_a},
                "r2": {"status": "acquired", "source": good},
            }
        )
        fixed = trace_atom(
            atom,
            root_source=review,
            policy=POLICY,
            project_sources_used=0,
            acquire=resolved,
            depth0_verdict="supported",
        )
        self.assertEqual(fixed["outcome"], "origin_traced")
        self.assertFalse(fixed["requires_final_human_approval"])


class _ScreeningBase(ManifestBase):
    """Screening fixture mirroring ``test_screening.WorkflowTests`` (no tests)."""

    def setUp(self):
        super().setUp()
        self.screening = ScreeningRepository(self.store)
        _, _, self.source, entries = self.standard()
        self.manifest = self.make(entries)
        self.gate(self.manifest)
        self.report = ParsingRepository(self.store).parse_stored_document(
            self.source,
            report_artifact_id="parsed",
            report_revision_id="parsed-r1",
            rights=self.rights("parsed", "parsed-r1"),
        )["report_ref"]
        self.config_ref = self.put("config", screening_config())
        self.case = {
            "id": "paper1",
            "report_ref": self.report,
            "passage_id": None,
            "response": screening_response(0.1),
        }
        self.pilot_input = {
            "config_ref": self.config_ref,
            "cases": [self.case],
            "adjudications": {
                "paper1": {
                    "label": "exclude",
                    "inspected_source": True,
                    "important_failure": False,
                    "rationale": "Checked original source",
                    "corrective_action": None,
                }
            },
        }
        self.pilot = self.put("pilot", self.pilot_input)

    def put(self, kind, payload, *, artifact=None, revision=None, parent=None):
        artifact = artifact or kind
        revision = revision or artifact + "-r1"
        return self.screening.propose(
            kind,
            payload,
            artifact_id=artifact,
            revision_id=revision,
            rights=self.rights(artifact, revision),
            parent_revision_id=parent,
        )

    def approve_ref(self, ref, kind, gate):
        self.approve(ref["artifact_id"], ref["revision_id"], kind, gate)

    def batch_input(self):
        return {
            "config_ref": self.config_ref,
            "pilot_ref": self.pilot,
            "acquisition_ref": self.ref_of(self.manifest),
            "cases": [self.case],
            "seed": "fixed",
        }

    def make_batch(self):
        self.approve_ref(self.pilot, "pilot", "screening.pilot")
        return self.put("batch", self.batch_input())

    def audit_input(self, batch, false=False):
        return {
            "batch_ref": batch,
            "checks": {
                "paper1": {
                    "inspected_source": True,
                    "false_exclusion": false,
                    "rationale": "Read exact original source",
                    "corrective_action": "Revise rules and re-pilot" if false else None,
                }
            },
        }


class FalseExclusionTests(_ScreeningBase):
    def test_false_exclusion_pauses_batch_and_new_batch_audit_corrects(self):
        """Inject a false exclusion (owner: Step 07 screening exclusion audit)."""
        batch = self.make_batch()
        bad = self.put("audit", self.audit_input(batch, True))
        self.assertTrue(self.screening.read(bad, "audit")["paused"])
        self.approve_ref(bad, "exclusion_set", "screening.exclusions")

        # required stop: an observed false exclusion permanently pauses the exact
        # batch; even an approved audit cannot authorize exclusions for it.
        with self.assertRaises(ScreeningError):
            self.screening.require_exclusions(bad)

        # correction: correct the inputs, create a new batch, and re-audit it.
        clean_batch = self.put(
            "batch", self.batch_input(), revision="batch-r2", parent="batch-r1"
        )
        good = self.put(
            "audit",
            self.audit_input(clean_batch),
            revision="audit-r2",
            parent="audit-r1",
        )
        self.approve_ref(good, "exclusion_set", "screening.exclusions")
        self.assertEqual(self.screening.require_exclusions(good), ["paper1"])


class UnsupportedClaimTests(ReviewBase):
    def test_unsupported_claim_blocks_and_a_repair_corrects(self):
        """Inject an unsupported claim (owner: Step 12 review / fact-check)."""
        draft, package = _clean_draft()
        blocked = self.run_review(
            "review-unsupported-stop",
            draft,
            package,
            FakeCritic([_major()]),
            FakeWriter(reject=[("P1", "I1")]),
        )

        # required stop: the claimed support is disputed; a human must resolve it.
        self.assertEqual(blocked["status"], "blocked")
        self.assertIsNotNone(blocked["required_human_action"])
        self.assertIn("disputed", [f["disposition"] for f in blocked["findings"]])

        # correction: the writer revises the claim to a supported statement.
        draft2, package2 = _clean_draft()
        fixed = self.run_review(
            "review-unsupported-fix",
            draft2,
            package2,
            FakeCritic([_major()]),
            FakeWriter({("P1", "I1"): "Compound Z reduced tumor volume in a treated mouse model."}),
        )
        self.assertEqual(fixed["status"], "complete")
        self.assertIn("fixed", [f["disposition"] for f in fixed["findings"]])


class FalseCriticObjectionTests(ReviewBase):
    def test_false_critic_objection_rejected_without_repair_budget(self):
        """Inject a false critic objection (owner: Step 12 review)."""
        draft, package = _clean_draft()
        assertion = _major()
        assertion.pop("basis")
        assertion["objection"] = "I simply do not believe this claim."
        fabricated = _major(objection="The source says something else entirely.")
        fabricated["basis"] = {
            "finding_key": "A10:F1",
            "quote": "text that appears nowhere in the source",
        }
        state = self.run_review(
            "review-false-objection",
            draft,
            package,
            FakeCritic([assertion, fabricated]),
            FakeWriter(),
        )

        # required stop: both objections are rejected and no repair is triggered
        # (the false objection never consumes the bounded repair budget).
        self.assertEqual(state["repair_calls"], 0)
        rejected = [f for f in state["findings"] if f["disposition"] == "rejected"]
        self.assertEqual(len(rejected), 2)
        reasons = " ".join(f["reason"] for f in rejected)
        self.assertIn("assertion", reasons)
        self.assertIn("fabricated", reasons)

        # correction: with the false objections stopped, the review completes.
        self.assertEqual(state["status"], "complete")


class OmittedFindingTests(DeliveryBase):
    def test_omitted_finding_blocks_and_citing_correction_completes(self):
        """Inject an omitted finding (owner: Step 14 delivery global review)."""
        evidence = [
            {
                "evidence_id": "e2",
                "source_key": "k2",
                "direction": "contrary",
                "consequential": True,
            }
        ]

        # required stop: consequential contrary evidence that is not cited is a
        # real open major finding; an unresolving review blocks with a human action.
        did = "delivery-omission-stop"
        self.start(did)
        reviewer, _ = _reviewer(find_if_missing=False)
        writer, _ = _writer()
        state = self.repo.run_global_review(
            did,
            {"revision_id": "m1", "text": "Body [@k1].", "paragraphs": {}},
            evidence,
            reviewer,
            writer,
            rights=self.rights,
        )
        kinds = [f.get("kind") for f in state["global_review"]["findings"]]
        self.assertIn("omitted_contrary", kinds)
        self.assertEqual(state["global_review"]["status"], "blocked")
        self.assertTrue(state["global_review"]["required_human_action"])

        # correction: the writer cites the omitted evidence; the finding clears.
        did2 = "delivery-omission-fix"
        self.start(did2)
        reviewer2, _ = _reviewer(find_if_missing=False)

        def citing_writer(text, findings):
            return text + " [@k2]"

        fixed = self.repo.run_global_review(
            did2,
            {"revision_id": "m1", "text": "Body [@k1].", "paragraphs": {}},
            evidence,
            reviewer2,
            citing_writer,
            rights=self.rights,
        )
        self.assertEqual(fixed["global_review"]["status"], "complete")
        self.assertNotIn(
            "omitted_contrary", [f.get("kind") for f in fixed["global_review"]["findings"]]
        )


class PostApprovalEditTests(DeliveryBase):
    def _approved(self, delivery_id):
        self.start(delivery_id)
        self.review_once(delivery_id)
        self.accept_cutoff(delivery_id)
        return self.repo.final_approval(
            delivery_id,
            artifact_hashes={"art-1": "sha-1"},
            warnings=[],
            accepted_warning_ids=[],
            unsupported_major_claims=[],
            approver="alice",
            rights=self.rights,
        )

    def test_post_approval_edit_invalidates_and_reapproval_corrects(self):
        """Inject a post-approval edit (owner: Step 14 delivery)."""
        did = "delivery-post-approval"
        self._approved(did)
        self.repo.notes(
            did,
            [{"note_id": "n1", "text": "tighten wording", "category": "wording"}],
            rights=self.rights,
        )
        state = self.repo.apply_notes(
            did,
            batch_id="b1",
            approval_id="appr-1",
            edits=[
                {
                    "note_id": "n1",
                    "paragraph_id": "p1",
                    "new_text": "Body [@k1]. adjusted wording.",
                    "kind": "wording",
                }
            ],
            rights=self.rights,
        )

        # required stop: the earlier approval is invalidated and export is refused.
        self.assertIsNone(state["approval"])
        self.assertEqual(len(state["invalidated_approvals"]), 1)
        self.assertEqual(state["status"], "in_progress")
        with self.assertRaises(DeliveryError):
            self.repo.export(
                did,
                manuscript={"revision_id": "m1", "text": "Body [@k1]. adjusted wording."},
                references=[_delivery_ref()],
                corpus=CORPUS,
                current_hashes={"art-1": "sha-1"},
                policy={"companion_accepted": True, "journal_policy_verified": False},
                ai_use=[],
                out_dir=self.tmp / "export-blocked",
                rights=self.rights,
            )

        # correction: re-approve the exact revised revision; export then succeeds.
        reapproved = self.repo.final_approval(
            did,
            artifact_hashes={"art-1": "sha-1"},
            warnings=[],
            accepted_warning_ids=[],
            unsupported_major_claims=[],
            approver="alice",
            rights=self.rights,
        )
        self.assertTrue(reapproved["approval"]["approved"])
        record = self.repo.export(
            did,
            manuscript={"revision_id": "m1", "text": "Body [@k1]. adjusted wording."},
            references=[_delivery_ref()],
            corpus=CORPUS,
            current_hashes={"art-1": "sha-1"},
            policy={"companion_accepted": True, "journal_policy_verified": False},
            ai_use=[],
            out_dir=self.tmp / "export-fixed",
            rights=self.rights,
        )
        self.assertTrue(record["checks"]["consistency"]["ok"])


if __name__ == "__main__":
    unittest.main()
