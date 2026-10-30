"""Step 15 end-to-end manuscript demonstration (back half of the pipeline).

Proves, in **one** disposable project store (a tempdir), the back half of
``docs/implementation/15-release.md`` AC1::

    synthesis -> framework/outline -> section blueprints -> draft
    -> section review (critic/fact-check) -> origin tracing
    -> author notes/freshness -> final approval -> exports

The stages are chained through the real repositories, feeding each stage's
actual output into the next wherever the APIs allow:

* ``SynthesisWorkflowRepository`` (kind ``synthesis``/``framework``/``outline``)
  -> ``DraftingWorkflowRepository`` (``blueprint`` -> ``draft``);
* the drafting workflow's draft **result** and stored **package** are consumed
  directly by ``SectionReviewRepository.run`` (the draft sentence shape already
  carries ``intent_id``/``text``/``citation_keys``/``secondary``/
  ``proposed_synthesis`` that ``review.schema.fact_check`` reads);
* the review state's ``checks`` are consumed directly by
  ``OriginTraceRepository.run`` (``review_checks``) and ``origin_coverage``;
* the review ``status`` and the trace coverage feed
  ``DeliveryRepository.start_global_review``; the origin state feeds
  ``origin_coverage`` for the export disclosures;
* ``run_global_review`` -> ``freshness`` -> ``notes``/``apply_notes`` ->
  ``final_approval`` -> ``export``.

Where a stage's actual output cannot be consumed by the next API, the existing
synthetic fixture is used and the call site is marked ``# BRIDGE: <why>``; every
bridge is listed in :data:`BRIDGES` below.

**Fixture disclosure:** the upstream extraction/audit inputs to synthesis, the
origin-tracing claim/source graph, and the export bibliography/corpus are
existing *synthetic* fixtures; the critic/writer/reviewer/acquire ports are local
fakes that count calls.  No live model, network, provider, or OpenCode session
call is made, and no source is copyrighted.  All content is synthetic.

Run::

    PYTHONPATH=src python3 -m unittest tests.test_step15_e2e_manuscript -v
"""

from __future__ import annotations

import hashlib
import os
import sys
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
for _path in (str(_HERE.parent / "src"), str(_HERE)):  # src package + sibling helpers
    if _path not in sys.path:
        sys.path.insert(0, _path)

# Reuse the accepted, integrated fixtures/helpers instead of copying them.
from test_drafting_drafts import DraftBase  # noqa: E402
from test_review_workflow import (  # noqa: E402
    _CRITIC,
    _WRITER,
    FakeCritic,
    FakeWriter,
)
from test_tracing_workflow import (  # noqa: E402
    POLICY,
    FakeAcquire,
    _claim,
    _primary,
    _secondary_review,
)

from review_article_agent.delivery.export import _FORBIDDEN_CLAIM_SUBSTRINGS  # noqa: E402
from review_article_agent.delivery.workflow import DeliveryRepository  # noqa: E402
from review_article_agent.review.workflow import SectionReviewRepository  # noqa: E402
from review_article_agent.tracing.workflow import (  # noqa: E402
    OriginTraceRepository,
    origin_coverage,
)

#: Every place a real stage output could not be consumed by the next API and the
#: existing synthetic fixture is used instead.  Each is marked ``# BRIDGE:`` at
#: its call site.
BRIDGES = [
    (
        "synthesis_inputs",
        "The accepted Step 09 extraction/audit inputs to synthesis come from the "
        "existing synthetic receipt fixture (test_synthesis_workflow."
        "approved_chain -> accepted_audit); no live extraction run is chained.",
    ),
    (
        "tracing_claims_sources",
        "Origin tracing's claim/source graph inputs come from the Step 13 "
        "synthetic fixture (test_tracing_workflow._claim / _secondary_review / "
        "_primary); only review_checks are chained from the real review state.",
    ),
    (
        "export_bibliography",
        "Export references/corpus come from the delivery export fixture's "
        "synthetic approved-corpus records, not from the drafting package's "
        "opaque cit_* records (which carry no title/author/year).",
    ),
    (
        "manuscript_text",
        "The global-review/export manuscript text is a short synthetic bridge "
        "text: the provisional assembly Markdown carries cit_* markers, which the "
        "export Pandoc-citation reader does not recognise.",
    ),
]

#: The synthetic approved-corpus record / corpus used by the export stage.
_BRIDGE_REF = {
    "key": "k1",
    "identity": {"doi": "10.1/x"},
    "version": "v1",
    "title": "A synthetic study",
    "authors": ["A. Author", "B. Author"],
    "year": 2020,
    "journal": "Journal of Synthetic Results",
}
_BRIDGE_CORPUS = {"k1": {"identity": {"doi": "10.1/x"}, "version": "v1"}}
#: A short bridge text citing the single synthetic approved-corpus record.
_BRIDGE_TEXT = "The reported effect is summarised here [@k1]."


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class EndToEndManuscriptTests(DraftBase):
    """One disposable store: synthesis -> ... -> final approved exports."""

    def test_synthesis_to_final_exports(self):
        # ---------------------------------------------------------------- #
        # 1. synthesis -> framework -> outline (approved chain)
        # ---------------------------------------------------------------- #
        # BRIDGE: synthesis_inputs -- the extraction/audit provenance is the
        # existing synthetic Step 09 receipt fixture, not a live extraction run.
        synthesis_ref, framework_ref, outline_ref = self.approved_chain()
        self.assertEqual(self.synth.require_synthesis(synthesis_ref)["kind"], "synthesis")
        self.assertEqual(self.synth.require_framework(framework_ref)["kind"], "framework")
        self.assertEqual(self.synth.require_outline(outline_ref)["kind"], "outline")
        # Gate/approval state reached: the approved chain is consumable through
        # require_for_blueprints only because each named gate is satisfied.
        blueprints_ctx = self.synth.require_for_blueprints(outline_ref)
        self.assertTrue(blueprints_ctx["accepted"])
        self.assertEqual(blueprints_ctx["authority"], "named_gate_only")

        # ---------------------------------------------------------------- #
        # 2. section blueprint (per-section workspace + approved package)
        # ---------------------------------------------------------------- #
        ws_ref = self.make_workspace(outline_ref)
        bp_ref = self.propose_bp(self.blueprint_payload(ws_ref))
        self.approve_bp(bp_ref)
        approved_bp = self.wf.require_blueprint(bp_ref)
        self.assertTrue(approved_bp["approved"])
        self.assertFalse(approved_bp["authorizes_export"])
        self.assertEqual(approved_bp["result"]["status"], "provisional")

        # ---------------------------------------------------------------- #
        # 3. draft -- atomically consumes the approved blueprint
        # ---------------------------------------------------------------- #
        draft_ref = self.propose_draft(self.draft_payload(bp_ref))
        draft_env = self.wf.read(draft_ref, "draft")
        draft = draft_env["result"]
        package = draft_env["package"]
        self.assertEqual(draft["kind"], "section_draft")
        self.assertEqual(draft["status"], "provisional")
        self.assertFalse(draft["authorizes_export"])
        self.assertTrue(package["evidence"])
        # The draft sentence carries the real verified-corpus citation key.
        first_citation = draft["paragraphs"][0]["sentences"][0]["citation_keys"][0]
        self.assertIn(first_citation, package["citations"])

        # ---------------------------------------------------------------- #
        # 4. section review -- chained real draft + real package
        # ---------------------------------------------------------------- #
        review = SectionReviewRepository(self.store)
        review_state = review.run(
            "review-sec1",
            draft=draft,
            package=package,
            writer_model=_WRITER,
            critic_model=_CRITIC,
            critic=FakeCritic([]),
            writer=FakeWriter(),
            rights=self.rights,
        )
        # Read-only validation, never an automatic pass, never independent
        # human verification.
        self.assertEqual(review_state["status"], "complete")
        self.assertTrue(review_state["validation"]["read_only"])
        self.assertFalse(review_state["engine"]["independent_human_verification"])
        self.assertEqual(review_state["critic_calls"], 1)
        self.assertEqual(review_state["repair_calls"], 0)
        checks = review_state["checks"]
        self.assertTrue(checks)
        self.assertTrue(all(c["verdict"] == "supported" for c in checks))

        # ---------------------------------------------------------------- #
        # 5. origin tracing -- chained real review checks
        # ---------------------------------------------------------------- #
        # BRIDGE: tracing_claims_sources -- the claim/source graph is the Step 13
        # synthetic fixture; the review checks are chained from the real state.
        acquire = FakeAcquire({"r1": {"status": "acquired", "source": _primary()}})
        trace = OriginTraceRepository(self.store)
        trace_state = trace.run(
            "trace-e2e",
            claims=[_claim()],
            sources={"review": _secondary_review()},
            policy=POLICY,
            review_checks=checks,
            acquire=acquire,
            rights=self.rights,
        )
        self.assertEqual(trace_state["claims"][0]["outcome"], "origin_traced")
        # The chained review check is copied and annotated with origin fields only.
        self.assertEqual(trace_state["section_checks"][0]["verdict"], "supported")
        self.assertEqual(trace_state["section_checks"][0]["origin_outcome"], "origin_traced")
        # primary_verified is never emitted.
        self.assertNotIn("primary_verified", str(trace_state))

        coverage = origin_coverage(trace_state)
        self.assertTrue(coverage["complete"])
        self.assertFalse(coverage["edits_applied"])
        self.assertEqual(coverage["requires_human_approval"], [])

        # ---------------------------------------------------------------- #
        # 6. delivery -- global review gate fed by real review + coverage
        # ---------------------------------------------------------------- #
        delivery = DeliveryRepository(self.store)
        sections = [
            {
                "section_id": "sec1",
                "review_status": review_state["status"],  # "complete"
                "origin_complete": coverage["complete"],
            }
        ]
        state = delivery.start_global_review(
            "delivery-e2e", sections, trace_state, rights=self.rights
        )
        self.assertEqual(state["status"], "in_progress")

        # Global review with a clean reviewer: no findings -> complete.
        reviewer, _ = self._clean_reviewer()
        writer, _ = self._clean_writer()
        state = delivery.run_global_review(
            "delivery-e2e",
            {
                "revision_id": "m1",
                "text": _BRIDGE_TEXT,
                "paragraphs": {"P1": {"text": _BRIDGE_TEXT, "checks": "valid"}},
            },
            [],
            reviewer,
            writer,
            rights=self.rights,
        )
        self.assertEqual(state["global_review"]["status"], "complete")
        self.assertEqual(state["global_review"]["reviewer_calls"], 1)

        # ---------------------------------------------------------------- #
        # 7. freshness -- explicit cutoff decision, never silent additions
        # ---------------------------------------------------------------- #
        state = delivery.freshness(
            "delivery-e2e", "2026-01-01", {"action": "accept_cutoff"}, rights=self.rights
        )
        self.assertEqual(state["freshness"]["status"], "cutoff_accepted")

        # ---------------------------------------------------------------- #
        # 8. author notes -- plan + one approved, non-staling wording batch
        # ---------------------------------------------------------------- #
        delivery.notes(
            "delivery-e2e",
            [{"note_id": "n1", "text": "tighten wording", "category": "wording"}],
            rights=self.rights,
        )
        state = delivery.apply_notes(
            "delivery-e2e",
            batch_id="b1",
            approval_id="appr-1",
            edits=[
                {
                    "note_id": "n1",
                    "paragraph_id": "P1",
                    "new_text": "The reported effect is summarised here [@k1].",
                    "kind": "wording",
                }
            ],
            rights=self.rights,
        )
        self.assertEqual(state["applied_batches"], ["b1"])
        self.assertEqual(state["revalidation_required"], [])

        # ---------------------------------------------------------------- #
        # 9. final approval -- exact-revision manifest
        # ---------------------------------------------------------------- #
        state = delivery.final_approval(
            "delivery-e2e",
            artifact_hashes={"art-1": "sha-1"},
            warnings=[],
            accepted_warning_ids=[],
            unsupported_major_claims=[],
            approver="alice",
            rights=self.rights,
        )
        manifest = state["approval"]
        self.assertTrue(manifest["approved"])
        self.assertEqual(manifest["manuscript_revision"], "m1")
        self.assertEqual(manifest["accepted_warnings"], [])
        self.assertNotIn("primary_verified", str(manifest))

        # ---------------------------------------------------------------- #
        # 10. export -- manuscript.md, references.*, companion, disclosures
        # ---------------------------------------------------------------- #
        out_dir = self.tmp / "export-e2e"
        # BRIDGE: manuscript_text / export_bibliography -- a short synthetic
        # manuscript text plus the delivery fixture's approved-corpus records.
        record = delivery.export(
            "delivery-e2e",
            manuscript={"revision_id": "m1", "text": _BRIDGE_TEXT},
            references=[dict(_BRIDGE_REF)],
            corpus=dict(_BRIDGE_CORPUS),
            current_hashes={"art-1": "sha-1"},
            policy={"companion_accepted": True, "journal_policy_verified": False},
            ai_use=[{"role": "writer", "model": "fake", "version": "1"}],
            out_dir=out_dir,
            rights=self.rights,
        )

        expected = {
            "manuscript.md",
            "references.bib",
            "references.ris",
            "companion.md",
            "summary.md",
            "ai_declaration.md",
            "export_manifest.json",
        }
        self.assertEqual(set(os.listdir(out_dir)), expected)
        self.assertEqual(set(record["file_hashes"]), expected)
        self.assertTrue(record["checks"]["consistency"]["ok"])

        # Every recorded file hash matches the bytes actually written.
        for name, digest in record["file_hashes"].items():
            content = (out_dir / name).read_text(encoding="utf-8")
            self.assertEqual(
                _sha256(content), digest, f"recorded hash mismatch for {name}"
            )

        # The bibliography/RIS agree with the manuscript's cited key.
        self.assertIn("@article{k1,", (out_dir / "references.bib").read_text())
        self.assertIn("ID  - k1", (out_dir / "references.ris").read_text())

        # Disclosures / AI declaration never assert independent validation or
        # submission compliance, and never emit primary_verified.
        for name in ("ai_declaration.md", "companion.md", "summary.md"):
            lowered = (out_dir / name).read_text(encoding="utf-8").lower()
            for phrase in _FORBIDDEN_CLAIM_SUBSTRINGS:
                self.assertNotIn(phrase, lowered)
            self.assertNotIn("primary_verified", lowered)
        self.assertIn(
            "does not assert validation beyond the recorded ai use",
            (out_dir / "ai_declaration.md").read_text(encoding="utf-8").lower(),
        )

    # -- local fakes (count calls; no live model) ---------------------------- #
    @staticmethod
    def _clean_reviewer():
        calls = []

        def reviewer(text):
            calls.append(text)
            return []

        return reviewer, calls

    @staticmethod
    def _clean_writer():
        calls = []

        def writer(text, findings):
            calls.append(findings)
            return text

        return writer, calls


if __name__ == "__main__":
    unittest.main()
