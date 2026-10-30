"""Focused tests: Step 13 tracing inputs built from real draft/package state.

Covers ``src/review_article_agent/tracing/inputs.py`` against the kickoff 16 §8
wiring: ``claims_and_sources(store, draft=..., package=..., review_state=...)``
derives one claim per drafted sentence that cites evidence (atoms from the
sentence's cited evidence), one tracing source per cited citation key (in the
exact shape :mod:`review_article_agent.tracing.schema` reads), and a
``source_digests`` map from the acquired asset sha256s.  It is read-only.

The draft and its approved package come from the real Step 11 modules over a real
disposable ``ProjectStore`` (fixtures inherited from
:mod:`test_drafting_drafts`).  No model, network, provider, or OpenCode session
call is made.

Run::

    PYTHONPATH=src:tests python3 -m unittest tests.test_tracing_inputs -v
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

import test_drafting_drafts as dd  # noqa: E402

from review_article_agent.tracing.inputs import claims_and_sources  # noqa: E402


class InputsBase(dd.DraftBase):
    def _draft_and_package(self):
        _syn, _fw, outline_ref = self.approved_chain()
        ws_ref = self.make_workspace(outline_ref)
        bp_ref = self.propose_bp(self.blueprint_payload(ws_ref))
        self.approve_bp(bp_ref)
        d_ref = self.propose_draft(self.draft_payload(bp_ref))
        envelope = self.wf.read(d_ref, "draft")
        return envelope["result"], envelope["package"]


class ClaimsAndSourcesTests(InputsBase):
    def test_one_claim_per_citing_sentence_with_evidence_atoms(self):
        draft, package = self._draft_and_package()
        claims, sources, source_digests = claims_and_sources(
            self.store, draft=draft, package=package, review_state=None
        )

        self.assertEqual(len(claims), 1)
        claim = claims[0]
        self.assertEqual(claim["paragraph_id"], "P1")
        self.assertEqual(claim["intent_id"], "I1")
        self.assertIn(claim["root_source_key"], sources)
        self.assertEqual(set(sources), set(package["citations"]))

        # One atom per cited evidence item, carrying the verified passage id.
        self.assertTrue(claim["atoms"])
        evidence = package["evidence"][0]
        self.assertEqual(
            claim["atoms"][0]["passage_ids"], [evidence["original_passage"]["passage_id"]]
        )
        self.assertEqual(claim["atoms"][0]["text"], draft["paragraphs"][0]["sentences"][0]["text"])

    def test_source_shape_and_digests(self):
        draft, package = self._draft_and_package()
        claims, sources, source_digests = claims_and_sources(
            self.store, draft=draft, package=package, review_state=None
        )
        key = claims[0]["root_source_key"]
        source = sources[key]

        # Exact tracing-source shape.
        self.assertEqual(
            set(source), {"source_key", "source", "passages", "references", "citation_links"}
        )
        self.assertEqual(source["source_key"], key)
        # The passage carries the package's verified original passage text.
        evidence = package["evidence"][0]
        self.assertEqual(
            source["passages"],
            [
                {
                    "passage_id": evidence["original_passage"]["passage_id"],
                    "text": evidence["original_passage"]["text"],
                }
            ],
        )
        # The digest is the acquired asset sha256 and is mirrored in source_digests.
        self.assertTrue(source["source"]["sha256"])
        self.assertEqual(source_digests[key], source["source"]["sha256"])

    def test_is_read_only_and_deterministic(self):
        draft, package = self._draft_and_package()
        first = claims_and_sources(self.store, draft=draft, package=package)
        second = claims_and_sources(self.store, draft=draft, package=package)
        self.assertEqual(first, second)

    def test_sentence_without_citations_yields_no_claim(self):
        draft, package = self._draft_and_package()
        # Strip the sentence's citations: no claim should be derived for it.
        bare = {
            "paragraphs": [
                {
                    "paragraph_id": "P1",
                    "sentences": [
                        {"intent_id": "I1", "claim_ids": ["K1"], "text": "x", "citation_keys": []}
                    ],
                }
            ]
        }
        claims, _sources, _digests = claims_and_sources(
            self.store, draft=bare, package=package
        )
        self.assertEqual(claims, [])


if __name__ == "__main__":
    unittest.main()
