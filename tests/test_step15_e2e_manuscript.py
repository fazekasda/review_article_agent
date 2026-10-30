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

**Fixture disclosure:** the upstream extraction/audit inputs to synthesis and the
manuscript-text bridge are existing *synthetic* fixtures; the critic/writer/
reviewer ports are local fakes that count calls.  The origin-tracing claim/source
graph is now derived from the **real** draft and its approved package, and the
acquisition port is the **real** Step 06/07-backed port
(``tracing.acquisition_port.make_acquire_port``); no source is fetched.  The
export bibliography is built from the draft package's real verified frozen-corpus
citation records (``bibliography_from_packages``).  No live model, network,
provider, or OpenCode session call is made, and no source is copyrighted.  All
content is synthetic.

Run::

    PYTHONPATH=src python3 -m unittest tests.test_step15_e2e_manuscript -v
"""

from __future__ import annotations

import hashlib
import os
import sys
import unittest
from pathlib import Path
from _tiers import slow

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
from test_tracing_workflow import POLICY  # noqa: E402

from review_article_agent.delivery.export import (  # noqa: E402
    _FORBIDDEN_CLAIM_SUBSTRINGS,
    bibliography_from_packages,
)
from review_article_agent.delivery.workflow import DeliveryRepository  # noqa: E402
from review_article_agent.review.workflow import SectionReviewRepository  # noqa: E402
from review_article_agent.tracing.acquisition_port import (  # noqa: E402
    make_acquire_port,
)
from review_article_agent.tracing.inputs import claims_and_sources  # noqa: E402
from review_article_agent.tracing.workflow import (  # noqa: E402
    OriginTraceRepository,
    origin_coverage,
)

#: Every place a real stage output could not be consumed by the next API and the
#: existing synthetic fixture is used instead.  Each is marked ``# BRIDGE:`` at
#: its call site.
#:
#: **The MVP path is now covered without these bridges.**  This hand-glued chain
#: is retained as a focused back-half demonstration, but the end-to-end MVP path
#: (real Step 09 receipts -> ``pipeline run`` -> scripted gates -> delivery) lives
#: in :mod:`tests.test_mvp_pipeline_e2e`; the ``synthesis_inputs`` bridge below is
#: exactly the Step 08 "extraction -> synthesis" seam that test closes.
BRIDGES = [
    (
        "synthesis_inputs",
        "The accepted Step 09 extraction/audit inputs to synthesis come from the "
        "existing synthetic receipt fixture (test_synthesis_workflow."
        "approved_chain -> accepted_audit); no live extraction run is chained.",
    ),
    (
        "manuscript_text",
        "The global-review/export manuscript text is a short synthetic bridge "
        "text: the provisional assembly Markdown carries cit_* markers, which the "
        "export Pandoc-citation reader does not recognise. The bridge text cites "
        "the draft package's real verified-corpus citation key so the export "
        "bibliography (built from the package via bibliography_from_packages) "
        "passes the consistency check.",
    ),
]


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@slow
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

        # The export bibliography is built from the real draft package: its
        # references/corpus come from the package's verified frozen-corpus
        # citation records (no synthetic _BRIDGE_REF fixture).
        references, corpus = bibliography_from_packages([package])
        self.assertEqual([ref["key"] for ref in references], [first_citation])
        self.assertEqual(set(corpus), {first_citation})
        # The real frozen-corpus metadata reached the citation record.
        citation_record = package["citations"][first_citation]
        self.assertEqual(citation_record["title"], "A study of things")
        self.assertEqual(citation_record["year"], 2020)
        self.assertEqual(citation_record["identity"], {"pmid": "111"})
        # The recorded provider authors/journal also reach the citation record
        # (the fixture's observation for this candidate carries them).
        self.assertEqual(citation_record["authors"], ["Ada Lovelace", "Alan Turing"])
        self.assertEqual(
            citation_record["journal"], "Journal of Synthetic Studies"
        )

        # The manuscript-text bridge cites the package's real citation key so the
        # export consistency check can pass (the assembly Markdown's cit_* markers
        # are still unrecognised by the Pandoc reader -- a separate bridge).
        bridge_text = f"The reported effect is summarised here [@{first_citation}]."

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
        # 5. origin tracing -- real claims/sources from the draft + package
        # ---------------------------------------------------------------- #
        # The claim/source graph is derived from the real draft and its approved
        # package (one claim per drafted sentence, one source per cited key); the
        # acquisition port is the real Step 06/07-backed port.  The acquired
        # primary is the frozen-corpus source itself: at depth 0 its tagged
        # passage does not attribute the claim (no own/secondary cue), so the
        # trace ends at depth 0 with the explicit secondary-attribution warning
        # (the spec-acceptable MVP outcome; never primary_verified).
        claims, sources, source_digests = claims_and_sources(
            self.store, draft=draft, package=package, review_state=review_state
        )
        self.assertTrue(claims)
        self.assertEqual(claims[0]["paragraph_id"], "P1")
        self.assertEqual(claims[0]["intent_id"], "I1")
        self.assertIn(claims[0]["root_source_key"], sources)
        self.assertEqual(set(sources), {first_citation})
        acquire = make_acquire_port(self.store)
        trace = OriginTraceRepository(self.store)
        trace_state = trace.run(
            "trace-e2e",
            claims=claims,
            sources=sources,
            policy=POLICY,
            review_checks=checks,
            acquire=acquire,
            rights=self.rights,
            source_digests=source_digests,
        )
        self.assertEqual(trace_state["claims"][0]["outcome"], "secondary_warning")
        self.assertTrue(trace_state["claims"][0]["requires_final_human_approval"])
        self.assertTrue(trace_state["claims"][0]["origin_warning"])
        # The chained review check is copied and annotated with origin fields only.
        self.assertEqual(trace_state["section_checks"][0]["verdict"], "supported")
        self.assertEqual(
            trace_state["section_checks"][0]["origin_outcome"], "secondary_warning"
        )
        self.assertTrue(
            trace_state["section_checks"][0]["requires_final_human_approval"]
        )
        # primary_verified is never emitted.
        self.assertNotIn("primary_verified", str(trace_state))

        coverage = origin_coverage(trace_state)
        self.assertTrue(coverage["complete"])
        self.assertFalse(coverage["edits_applied"])
        # The warned secondary account is listed for final human approval and the
        # export is not yet ready.
        self.assertFalse(coverage["export_ready"])
        self.assertEqual(len(coverage["requires_human_approval"]), 1)
        self.assertEqual(
            coverage["requires_human_approval"][0]["cite_source_key"], first_citation
        )

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
                "text": bridge_text,
                "paragraphs": {"P1": {"text": bridge_text, "checks": "valid"}},
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
                    "new_text": bridge_text,
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
        # BRIDGE: manuscript_text -- the synthetic bridge text now cites the
        # draft package's real verified-corpus key; the bibliography comes from
        # the package's real citation records (no synthetic _BRIDGE_REF).
        record = delivery.export(
            "delivery-e2e",
            manuscript={"revision_id": "m1", "text": bridge_text},
            references=references,
            corpus=corpus,
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

        # The bibliography/RIS agree with the manuscript's cited key and carry the
        # real frozen-corpus metadata (title/year) from the draft package.
        bib = (out_dir / "references.bib").read_text()
        ris = (out_dir / "references.ris").read_text()
        self.assertIn(f"@article{{{first_citation},", bib)
        self.assertIn("ID  - " + first_citation, ris)
        self.assertIn("title = {A study of things}", bib)
        self.assertIn("author = {Ada Lovelace and Alan Turing}", bib)
        self.assertIn("journal = {Journal of Synthetic Studies}", bib)
        self.assertIn("year = {2020}", bib)
        self.assertIn("TI  - A study of things", ris)
        self.assertIn("AU  - Ada Lovelace", ris)
        self.assertIn("AU  - Alan Turing", ris)
        self.assertIn("JO  - Journal of Synthetic Studies", ris)
        self.assertIn("AN  - 111", ris)

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
