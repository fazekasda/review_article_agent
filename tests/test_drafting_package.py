"""Focused S11-PACKAGE tests: approved, reconstructible evidence/citation package.

Covers ``src/review_article_agent/drafting/package.py`` against
``docs/implementation/11-drafting.md`` AC2/AC3/AC5: a drafting package assembled
from the exact current accepted synthesis/framework/outline chain, the accepted
extraction batch cases, and verified frozen corpus metadata; it rejects a missing,
foreign, unresolved, ambiguous, or tampered metadata linkage rather than inventing
one; and its stable citation key derives from the project + candidate identity.

The accepted Step 09 receipt seam (``extraction.workflow._require_run``) is
inherited from :mod:`test_extraction_workflow` via :class:`SynthesisBase` (which
patches it with synthetic *verified receipts*); every human approval uses the real
owner-capability flow (the controlling-tty seam is mocked as the accepted freeze
tests do).  No model, network, or provider call is made.

Run::

    PYTHONPATH=src:tests python3 -m unittest test_drafting_package -v
"""

from __future__ import annotations

from test_synthesis_workflow import SynthesisBase  # noqa: E402

from review_article_agent.drafting import (  # noqa: E402
    DraftingPackageError,
    DraftingPackageRepository,
    citation_key,
)
from review_article_agent.domain.records import canonical_json  # noqa: E402

_CASE = "c1"
_FINDING = _CASE + ":F1"


class DraftingPackageBase(SynthesisBase):
    def setUp(self):
        super().setUp()
        self.pkg = DraftingPackageRepository(self.store)

    def _reopen(self):
        super()._reopen()
        self.pkg = DraftingPackageRepository(self.store)


class HappyPathTests(DraftingPackageBase):
    def test_package_is_reconstructible_and_reopens_identically(self):
        _syn, _fw, outline_ref = self.approved_chain()
        package = self.pkg.build(outline_ref, "sec1")

        # Contract shape: exact global refs + section + approved global chain.
        self.assertEqual(package["document_type"], "drafting_package")
        self.assertEqual(package["section"]["section_id"], "sec1")
        self.assertEqual(package["framework"]["kind"], "framework")
        self.assertEqual(package["synthesis"]["kind"], "synthesis")
        self.assertEqual(package["outline_ref"], outline_ref)
        self.assertEqual(package["authority"], "named_gate_only")
        self.assertFalse(package["authorizes_export"])
        self.assertFalse(package["scientific_validation"])
        # Exact global refs to the manifest/freeze/corpus metadata chain.
        self.assertEqual(package["acquisition_ref"]["project_id"], self.project_id)
        self.assertEqual(package["freeze_ref"]["project_id"], self.project_id)
        self.assertEqual(package["corpus_ref"]["project_id"], self.project_id)

        # Evidence: the retained accepted finding, with summary + original data.
        self.assertEqual(len(package["evidence"]), 1)
        item = package["evidence"][0]
        self.assertEqual(item["finding_key"], _FINDING)
        self.assertEqual(item["finding"]["finding_id"], "F1")
        self.assertEqual(item["finding"]["statement"], "Compound Z did not reduce tumor volume versus vehicle.")
        self.assertEqual(item["finding"]["snippet"], "did not reduce tumor volume")
        self.assertEqual(item["summary"], "Synthetic summary.")
        self.assertEqual(item["extraction_ref"], package["synthesis"]["findings"][0]["extraction_ref"])
        self.assertEqual(item["source_refs"], package["synthesis"]["cases"][0]["source_refs"])

        # Citations: exactly one verified frozen candidate record.
        expected_key = citation_key(self.project_id, self.cands["111"])
        self.assertEqual(list(package["citations"]), [expected_key])
        self.assertEqual(item["citation_keys"], [expected_key])
        record = package["citations"][expected_key]
        self.assertEqual(record["candidate_id"], self.cands["111"])
        self.assertEqual(record["bibliographic_identifiers"], {"pmid": "111"})
        self.assertTrue(record["resolved"])
        self.assertEqual(record["corpus_ref"], package["corpus_ref"])
        self.assertEqual(record["freeze_ref"], package["freeze_ref"])

        # Reconstruct after worker restart: identical package from durable artifacts.
        self._reopen()
        rebuilt = self.pkg.build(outline_ref, "sec1")
        self.assertEqual(rebuilt, package)

    def test_unknown_section_is_rejected(self):
        _syn, _fw, outline_ref = self.approved_chain()
        with self.assertRaises(DraftingPackageError):
            self.pkg.build(outline_ref, "does-not-exist")

    def test_unapproved_outline_is_rejected(self):
        synthesis_ref, framework_ref, outline_ref = self.approved_chain()
        # A superseding outline revision without its own gate is not consumable.
        self.synth.propose(
            "outline",
            {"synthesis_ref": synthesis_ref, "framework_ref": framework_ref,
             "content": self.outline_content(title="edited")},
            artifact_id="out", revision_id="out-r2",
            rights=self.rights("out", "out-r2"), parent_revision_id="out-r1",
        )
        with self.assertRaises(DraftingPackageError):
            self.pkg.build(outline_ref, "sec1")


class MetadataRejectionTests(DraftingPackageBase):
    def setUp(self):
        super().setUp()
        self._syn, self._fw, self._outline_ref = self.approved_chain()
        self._package = self.pkg.build(self._outline_ref, "sec1")

    def _case(self):
        return {
            "id": _CASE,
            "source_refs": [{"project_id": self.project_id, "artifact_id": "src-0", "revision_id": "rev-0"}],
        }

    def _candidate(self, tokens, *, resolved=True):
        return {
            "candidate_id": "cand_x",
            "tokens": tokens,
            "resolved": resolved,
        }

    def test_missing_source_linkage_is_rejected(self):
        with self.assertRaises(DraftingPackageError):
            self.pkg._case_candidate_ids(self._case(), {}, {})

    def test_foreign_candidate_is_rejected(self):
        ref = self._case()["source_refs"][0]
        index = {canonical_json(ref): "cand_x"}
        with self.assertRaises(DraftingPackageError):
            self.pkg._case_candidate_ids(self._case(), index, {})

    def test_unresolved_candidate_is_rejected(self):
        ref = self._case()["source_refs"][0]
        index = {canonical_json(ref): "cand_x"}
        candidates = {"cand_x": self._candidate([["pmid", "1"]], resolved=False)}
        with self.assertRaises(DraftingPackageError):
            self.pkg._case_candidate_ids(self._case(), index, candidates)

    def test_ambiguous_identifiers_are_rejected(self):
        ref = self._case()["source_refs"][0]
        index = {canonical_json(ref): "cand_x"}
        candidates = {"cand_x": self._candidate([["pmid", "1"], ["pmid", "2"]])}
        with self.assertRaises(DraftingPackageError):
            self.pkg._case_candidate_ids(self._case(), index, candidates)

    def test_tampered_corpus_metadata_is_rejected_on_build(self):
        current = self.corpus.current()
        # Simulate on-disk tampering of the frozen candidate metadata bytes; the
        # immutability trigger is dropped only inside this test store.
        self.store.connection.execute("DROP TRIGGER revision_no_update")
        self.store.connection.execute(
            "UPDATE revision SET content=? WHERE artifact_id=? AND revision_id=?",
            (b"{}", current["artifact_id"], current["revision_id"]),
        )
        self.store.connection.commit()
        with self.assertRaises(DraftingPackageError):
            self.pkg.build(self._outline_ref, "sec1")


class CitationKeyTests(DraftingPackageBase):
    def test_key_is_deterministic_and_project_scoped(self):
        candidate_id = "cand_abc"
        first = citation_key(self.project_id, candidate_id)
        self.assertEqual(first, citation_key(self.project_id, candidate_id))
        self.assertTrue(first.startswith("cit_"))
        self.assertNotEqual(first, citation_key("OTHER-PROJECT", candidate_id))

    def test_key_is_stable_across_reopen(self):
        _syn, _fw, outline_ref = self.approved_chain()
        before = self.pkg.build(outline_ref, "sec1")["citations"]
        self._reopen()
        after = self.pkg.build(outline_ref, "sec1")["citations"]
        self.assertEqual(list(before), list(after))
        self.assertEqual(
            list(before), [citation_key(self.project_id, self.cands["111"])]
        )
