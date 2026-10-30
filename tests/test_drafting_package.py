"""Focused S11-PACKAGE tests: approved, reconstructible evidence/citation package.

Covers ``src/review_article_agent/drafting/package.py`` against
``docs/implementation/11-drafting.md`` AC2/AC3/AC5: a drafting package assembled
from the exact current accepted synthesis/framework/outline chain, the accepted
extraction batch cases, and verified frozen corpus metadata.  Each finding is
bound to its **own** verified receipt passage (id + source key) and to that
passage's exact source, so a two-source case yields each finding's own citation;
original passages/locators/methods-context are carried for interpretation; and a
missing, foreign, unresolved, ambiguous, or tampered linkage is refused rather
than invented.

The accepted Step 09 receipt seam (``extraction.workflow._require_run``) is
inherited from :mod:`test_extraction_workflow` via :class:`SynthesisBase` (which
patches it with synthetic *verified receipts*); one integration case instead
extends the real runner / fake-transport end-to-end fixture in
:mod:`test_extraction_runner`, so the real verified receipts (with per-passage
source refs and locators) are exercised.  Every human approval uses the real
owner-capability flow (the controlling-tty seam is mocked as the accepted freeze
tests do).  No model, network, or provider call is made.

Run::

    PYTHONPATH=src:tests python3 -m unittest test_drafting_package -v
"""

from __future__ import annotations

import unittest  # noqa: E402

import test_extraction_runner as extraction_runner  # noqa: E402
from test_acquisition_manifest import _jats  # noqa: E402
from test_extraction_workflow import (  # noqa: E402
    EXTRACTION_MODEL,
    MANDATORY_FAILURE_LABELS,
    QUALITY_MODEL,
    _quality_output,
)
from test_synthesis_workflow import (  # noqa: E402
    GATE_FRAMEWORK,
    GATE_FRAMEWORK_KIND,
    GATE_OUTLINE,
    GATE_OUTLINE_KIND,
    GATE_SYNTHESIS,
    GATE_SYNTHESIS_KIND,
    SynthesisBase,
)

from review_article_agent.drafting import (  # noqa: E402
    DraftingPackageError,
    DraftingPackageRepository,
    citation_key,
)
from review_article_agent.drafting.package import (  # noqa: E402
    _bibliographic_record,
    _observed_bibliographic_fields,
)
from review_article_agent.domain.records import canonical_json  # noqa: E402
from review_article_agent.extraction import workflow as extraction_workflow  # noqa: E402
from review_article_agent.synthesis.workflow import SynthesisWorkflowRepository  # noqa: E402
from _tiers import slow

_CASE = "c1"
_FINDING = _CASE + ":F1"


def _known(value):
    return {"state": "known", "value": value}


def _known_context():
    return {
        "species": _known("mouse"),
        "population": _known("adult mice"),
        "assay": _known("tumor volume"),
        "outcome": _known("tumor volume"),
        "conditions": _known("vehicle"),
    }


def _finding(finding_id, source_key, passage_id, snippet):
    return {
        "finding_id": finding_id,
        "source_key": source_key,
        "passage_id": passage_id,
        "snippet": snippet,
        "statement": f"Reported: {snippet}",
        "attribution": "direct_observation",
        "citation_flag": "own_result",
        "context": _known_context(),
        "secondary_warning": {"state": "not_applicable", "value": None},
        "limitations": _known("single model"),
        "numeric_details": [],
    }


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
        self.assertEqual(package["acquisition_ref"]["project_id"], self.project_id)
        self.assertEqual(package["freeze_ref"]["project_id"], self.project_id)
        self.assertEqual(package["corpus_ref"]["project_id"], self.project_id)

        # Evidence: the retained accepted finding, with summary + original data.
        self.assertEqual(len(package["evidence"]), 1)
        item = package["evidence"][0]
        self.assertEqual(item["finding_key"], _FINDING)
        self.assertEqual(item["finding"]["finding_id"], "F1")
        self.assertEqual(item["summary"], "Synthetic summary.")
        self.assertEqual(item["extraction_ref"], package["synthesis"]["findings"][0]["extraction_ref"])
        self.assertEqual(item["source_refs"], package["synthesis"]["cases"][0]["source_refs"])
        # The original verified passage text survives in the evidence item.
        self.assertEqual(item["original_passage"]["passage_id"], "p1")
        self.assertEqual(
            item["original_passage"]["text"],
            "In mice, compound Z did not reduce tumor volume (12.5 mm3) versus vehicle.",
        )

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

        # Original context: verified passages per used source.
        self.assertEqual(len(package["methods"]["original_sources"]), 1)
        original = package["methods"]["original_sources"][0]
        self.assertEqual(original["citation_key"], expected_key)
        self.assertEqual(
            [p["passage_id"] for p in original["passages"]], ["p1"]
        )
        # The legacy synthetic fixture's receipt passages lack a per-passage
        # source ref, so the unique single case source fallback is used and
        # disclosed.
        self.assertTrue(original["source_ref_fallback"])
        self.assertTrue(package["limitations"])

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
        self.synth.propose(
            "outline",
            {"synthesis_ref": synthesis_ref, "framework_ref": framework_ref,
             "content": self.outline_content(title="edited")},
            artifact_id="out", revision_id="out-r2",
            rights=self.rights("out", "out-r2"), parent_revision_id="out-r1",
        )
        with self.assertRaises(DraftingPackageError):
            self.pkg.build(outline_ref, "sec1")


class TwoSourcePerFindingTests(DraftingPackageBase):
    """Two distinct sources in one case: each finding gets only its own citation."""

    def _two_source_chain(self):
        self.setup_frozen()
        a, b = self.cands["111"], self.cands["222"]
        src1 = self.put_source(_jats("111"))
        src2 = self.put_source(_jats("222"))
        entries = [self.entry(a, source=src1), self.entry(b, source=src2)]
        manifest = self.make(entries)
        self.gate(manifest)

        passages = [
            {"passage_id": "p1", "source_key": "docA",
             "text": "In mice compound Z did not reduce tumor volume.", "source_ref": src1},
            {"passage_id": "p2", "source_key": "docB",
             "text": "Compound Z reduced tumor volume in rats.", "source_ref": src2},
        ]
        raw = {
            "schema_version": 1,
            "summary": "Two-source synthetic summary.",
            "source_records": [
                {"source_key": "docA", "source_kind": "document"},
                {"source_key": "docB", "source_kind": "document"},
            ],
            "findings": [
                _finding("F1", "docA", "p1", "did not reduce tumor volume"),
                _finding("F2", "docB", "p2", "reduced tumor volume"),
            ],
            "coverage": [
                {"target_kind": "passage", "target_id": "p1", "disposition": "finding", "finding_id": "F1"},
                {"target_kind": "passage", "target_id": "p2", "disposition": "finding", "finding_id": "F2"},
            ],
            "appraisal": [
                {"dimension_id": "risk_of_bias", "state": "assessed", "judgement": "low", "rationale": "randomised"}
            ],
        }
        extraction = self.dispatch(
            "c1-ext", config_ref=self.config_ref,
            observed_model=EXTRACTION_MODEL["model_id"], raw_output=raw,
            source_refs=[src1, src2], passages=passages,
        )
        quality_ref = self.dispatch(
            "c1-qual", config_ref=self.config_ref, observed_model=QUALITY_MODEL["model_id"],
            purpose="quality", extraction_ref=extraction, raw_output=_quality_output(),
        )
        case = {"id": _CASE, "extraction_ref": extraction, "quality_ref": quality_ref}
        pilot_ref = self.build_pilot([case], {_CASE: self.adjudication(MANDATORY_FAILURE_LABELS)})
        self.approve_ref(pilot_ref, extraction_workflow.GATE_KIND, extraction_workflow.GATE_PILOT)
        batch_ref = self.put(
            "batch",
            {"config_ref": self.config_ref, "pilot_ref": pilot_ref,
             "acquisition_ref": self.ref_of(manifest), "cases": [case], "seed": "s1"},
        )
        self.approve_ref(batch_ref, extraction_workflow.GATE_KIND, extraction_workflow.GATE_BATCH)
        audit_ref = self.put("audit", {"batch_ref": batch_ref, "checks": {_CASE: self.audit_check()}})
        self.approve_ref(audit_ref, extraction_workflow.GATE_KIND, extraction_workflow.GATE_OUTPUTS)

        synthesis_ref = self.synth.propose(
            "synthesis",
            {"audit_ref": audit_ref, "content": {
                "schema_version": 1, "memo": "two-source memo", "statements": [],
                "dispositions": [
                    {"finding": "c1:F1", "disposition": "genuine_disagreement", "rationale": "contrast"},
                    {"finding": "c1:F2", "disposition": "genuine_disagreement", "rationale": "contrast"},
                ],
            }},
            artifact_id="syn", revision_id="syn-r1", rights=self.rights("syn", "syn-r1"),
        )
        self.approve_ref(synthesis_ref, GATE_SYNTHESIS_KIND, GATE_SYNTHESIS)
        framework_ref = self.put_framework(synthesis_ref)
        self.approve_ref(framework_ref, GATE_FRAMEWORK_KIND, GATE_FRAMEWORK)
        outline_ref = self.synth.propose(
            "outline",
            {"synthesis_ref": synthesis_ref, "framework_ref": framework_ref, "content": {
                "schema_version": 1, "title": "Two-source outline",
                "sections": [{"section_id": "sec1", "order": 1, "title": "Evidence",
                              "concept_dependencies": ["C1"]}],
            }},
            artifact_id="out", revision_id="out-r1", rights=self.rights("out", "out-r1"),
        )
        self.approve_ref(outline_ref, GATE_OUTLINE_KIND, GATE_OUTLINE)
        return outline_ref

    @slow
    def test_each_finding_gets_its_own_source_and_citation(self):
        outline_ref = self._two_source_chain()
        package = self.pkg.build(outline_ref, "sec1")
        evidence = {item["finding_key"]: item for item in package["evidence"]}
        self.assertEqual(set(evidence), {"c1:F1", "c1:F2"})

        key1 = citation_key(self.project_id, self.cands["111"])
        key2 = citation_key(self.project_id, self.cands["222"])
        self.assertNotEqual(key1, key2)

        # Finding 1 -> source 1 only; finding 2 -> source 2 only.
        self.assertEqual(evidence["c1:F1"]["citation_keys"], [key1])
        self.assertEqual(evidence["c1:F2"]["citation_keys"], [key2])
        self.assertEqual(
            package["citations"][key1]["bibliographic_identifiers"], {"pmid": "111"}
        )
        self.assertEqual(
            package["citations"][key2]["bibliographic_identifiers"], {"pmid": "222"}
        )
        # Neither finding carries the other's source.
        self.assertNotEqual(evidence["c1:F1"]["source_refs"], evidence["c1:F2"]["source_refs"])
        self.assertEqual(evidence["c1:F1"]["original_passage"]["passage_id"], "p1")
        self.assertEqual(evidence["c1:F2"]["original_passage"]["passage_id"], "p2")
        self.assertFalse(evidence["c1:F1"]["original_passage"]["source_ref_fallback"])
        # Both used sources appear in the original context, each with its passages.
        self.assertEqual(
            {o["citation_key"] for o in package["methods"]["original_sources"]},
            {key1, key2},
        )


@slow
class MetadataRejectionTests(DraftingPackageBase):
    def setUp(self):
        super().setUp()
        self._syn, self._fw, self._outline_ref = self.approved_chain()
        self._package = self.pkg.build(self._outline_ref, "sec1")
        self._ref = {"project_id": self.project_id, "artifact_id": "src-0", "revision_id": "rev-0"}

    def _candidate(self, tokens, *, resolved=True):
        return {"candidate_id": "cand_x", "tokens": tokens, "resolved": resolved}

    def test_missing_source_linkage_is_rejected(self):
        with self.assertRaises(DraftingPackageError):
            self.pkg._candidate_for_source(self._ref, {}, {})

    def test_foreign_candidate_is_rejected(self):
        index = {canonical_json(self._ref): "cand_x"}
        with self.assertRaises(DraftingPackageError):
            self.pkg._candidate_for_source(self._ref, index, {})

    def test_unresolved_candidate_is_rejected(self):
        index = {canonical_json(self._ref): "cand_x"}
        candidates = {"cand_x": self._candidate([["pmid", "1"]], resolved=False)}
        with self.assertRaises(DraftingPackageError):
            self.pkg._candidate_for_source(self._ref, index, candidates)

    def test_ambiguous_identifiers_are_rejected(self):
        index = {canonical_json(self._ref): "cand_x"}
        candidates = {"cand_x": self._candidate([["pmid", "1"], ["pmid", "2"]])}
        with self.assertRaises(DraftingPackageError):
            self.pkg._candidate_for_source(self._ref, index, candidates)

    def test_ambiguous_source_to_candidate_is_rejected(self):
        entries = [
            {"candidate_id": "cand_a", "source_ref": self._ref, "required_assets": []},
            {"candidate_id": "cand_b", "source_ref": self._ref, "required_assets": []},
        ]
        with self.assertRaises(DraftingPackageError):
            self.pkg._source_index(entries)

    def test_unbound_finding_is_rejected(self):
        with self.assertRaises(DraftingPackageError):
            self.pkg._finding_passage({"passage_id": "pX", "source_key": "docA"}, [{"passage_id": "p1", "source_key": "docA"}])

    def test_passage_without_source_ref_needs_single_source_case(self):
        other = {"project_id": self.project_id, "artifact_id": "src-1", "revision_id": "rev-1"}
        with self.assertRaises(DraftingPackageError):
            self.pkg._passage_source_ref({"passage_id": "p1"}, [self._ref, other])
        ref, fallback = self.pkg._passage_source_ref({"passage_id": "p1"}, [self._ref])
        self.assertEqual(ref, self._ref)
        self.assertTrue(fallback)

    def test_tampered_corpus_metadata_is_rejected_on_build(self):
        current = self.corpus.current()
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

    @slow
    def test_key_is_stable_across_reopen(self):
        _syn, _fw, outline_ref = self.approved_chain()
        before = self.pkg.build(outline_ref, "sec1")["citations"]
        self._reopen()
        after = self.pkg.build(outline_ref, "sec1")["citations"]
        self.assertEqual(list(before), list(after))
        self.assertEqual(list(before), [citation_key(self.project_id, self.cands["111"])])


@slow
class DraftingOverRealRunnerTest(extraction_runner.FullFlowIntegrationTest):
    """Prove the real verified receipts (per-passage source refs) feed a package.

    This class *extends* ``FullFlowIntegrationTest`` (it does not replicate the
    frozen-corpus/acquisition fixture) and uses the real runner / fake transport.
    Its inherited full-flow cases are deselected here so the giant fixture runs
    once, in its own module.
    """

    test_positive_full_flow_to_synthesis = None
    test_important_audit_pauses_exact_batch_until_new_batch = None
    test_presend_revalidates_revoked_pilot = None
    test_bulk_source_not_in_manifest_membership_blocked = None

    def setUp(self):
        super().setUp()
        self.synth = SynthesisWorkflowRepository(self.store)
        self.pkg = DraftingPackageRepository(self.store)

    def test_package_from_real_verified_receipts(self):
        src, acquisition_ref = self._acquire()
        self._config_and_report(src)
        pilot_ref = self._pilot()
        batch_ref, _cases = self._bulk_case(pilot_ref, acquisition_ref)
        audit_ref = self.workflow.propose(
            "audit", {"batch_ref": batch_ref, "checks": {"case-1": self._check(important_error=False)}},
            artifact_id="art-audit", revision_id="rev-audit", rights=self.rights("art-audit", "rev-audit"),
        )
        self.approve("art-audit", "rev-audit", extraction_workflow.GATE_KIND, extraction_workflow.GATE_OUTPUTS)

        accepted = self.workflow.require_batch(batch_ref)["cases"]
        finding_keys = [
            f"{case['id']}:{finding['finding_id']}"
            for case in accepted
            for finding in case["extraction"]["findings"]
        ]
        self.assertTrue(finding_keys)
        dispositions = [
            {"finding": key, "disposition": "null_evidence", "rationale": "integration context"}
            for key in finding_keys
        ]
        synthesis_ref = self.synth.propose(
            "synthesis",
            {"audit_ref": audit_ref, "content": {
                "schema_version": 1, "memo": "integration memo", "statements": [],
                "dispositions": dispositions,
            }},
            artifact_id="art-syn", revision_id="rev-syn", rights=self.rights("art-syn", "rev-syn"),
        )
        self.approve("art-syn", "rev-syn", GATE_SYNTHESIS_KIND, GATE_SYNTHESIS)

        framework = {
            "schema_version": 1,
            "title": "Integration framework",
            "contribution": "organizes the accepted evidence",
            "criteria": {
                "contribution": "organizing relationship",
                "admission": "definition, role, support, counterevidence, limits",
                "support": "accepted finding keys",
                "revision": "changed definitions reopen dependent sections",
            },
            "concepts": [{
                "concept_id": "C1", "definition": "Integration concept.",
                "role": "central outcome", "original_support": finding_keys,
                "counterevidence_note": "no counterevidence identified",
                "applicability_limits": "synthetic corpus only",
                "testable_implication": "a repeat run would reproduce the effect",
                "status": "proposed", "novelty": "existing",
            }],
        }
        framework_ref = self.synth.propose(
            "framework", {"synthesis_ref": synthesis_ref, "content": framework},
            artifact_id="art-fw", revision_id="rev-fw", rights=self.rights("art-fw", "rev-fw"),
        )
        self.approve("art-fw", "rev-fw", GATE_FRAMEWORK_KIND, GATE_FRAMEWORK)
        outline = {
            "schema_version": 1, "title": "Integration outline",
            "sections": [{"section_id": "sec1", "order": 1, "title": "Evidence",
                          "concept_dependencies": ["C1"]}],
        }
        outline_ref = self.synth.propose(
            "outline", {"synthesis_ref": synthesis_ref, "framework_ref": framework_ref,
                        "content": outline},
            artifact_id="art-out", revision_id="rev-out", rights=self.rights("art-out", "rev-out"),
        )
        self.approve("art-out", "rev-out", GATE_OUTLINE_KIND, GATE_OUTLINE)

        package = self.pkg.build(outline_ref, "sec1")
        self.assertEqual(len(package["evidence"]), len(finding_keys))
        for item in package["evidence"]:
            passage = item["original_passage"]
            # The real runner path carries an exact per-passage source ref and a
            # locator (no legacy fallback).
            self.assertFalse(passage["source_ref_fallback"])
            self.assertEqual(passage["source_ref"], self.source_ref)
            self.assertIsInstance(passage["locator"], dict)
            self.assertEqual(passage["locator"]["source_ref"], self.source_ref)
            self.assertTrue(passage["text"])
            self.assertEqual(item["citation_keys"], [citation_key(self.project_id, self.cands["111"])])
        original = package["methods"]["original_sources"][0]
        self.assertFalse(original["source_ref_fallback"])
        self.assertTrue(original["passages"])
        self.assertEqual(package["limitations"], [])


# --------------------------------------------------------------------------- #
# Bibliographic metadata carry-over (pure; no store)
# --------------------------------------------------------------------------- #
class _Rec:
    def __init__(self, title=None, year=None, authors=(), journal=None):
        self.title = title
        self.publication_year = year
        self.authors = authors
        self.journal = journal


class _Result:
    def __init__(self, records):
        self.records = tuple(records)


class _Observation:
    def __init__(self, records):
        self.result = _Result(records)


class _FakeDiscovery:
    def __init__(self, by_run):
        self._by_run = by_run

    def get_search(self, run_id):
        return self._by_run.get(run_id)


class BibliographicMetadataTests(unittest.TestCase):
    """The citation record carries only verified frozen-corpus metadata."""

    _CORPUS_REF = {"project_id": "P", "artifact_id": "discovery.corpus", "revision_id": "rev-c"}
    _FREEZE_REF = {"project_id": "P", "artifact_id": "discovery.freeze", "revision_id": "rev-f"}

    @staticmethod
    def _candidate(**overrides):
        doc = {
            "candidate_id": "cand_x",
            "resolved": True,
            "tokens": [["native:pubmed", "a"], ["pmid", "111"], ["doi", "10.1/x"]],
            "occurrences": [["run1", 0]],
        }
        doc.update(overrides)
        return doc

    def test_present_fields_are_carried_from_the_observation(self):
        discovery = _FakeDiscovery({"run1": _Observation([_Rec("Real title", 2020)])})
        observed = _observed_bibliographic_fields(self._candidate(), discovery)
        self.assertEqual(observed, {"title": "Real title", "year": 2020})
        record = _bibliographic_record(
            "P", self._candidate(), self._CORPUS_REF, self._FREEZE_REF, [], observed
        )
        self.assertEqual(record["title"], "Real title")
        self.assertEqual(record["year"], 2020)
        self.assertEqual(record["identity"], {"doi": "10.1/x", "pmid": "111"})
        self.assertEqual(record["version"], "rev-c")
        self.assertEqual(record["candidate_id"], "cand_x")
        # The frozen corpus carries no authors/journal; they are omitted.
        self.assertNotIn("authors", record)
        self.assertNotIn("journal", record)

    def test_authors_and_journal_are_carried_from_the_observation(self):
        discovery = _FakeDiscovery(
            {
                "run1": _Observation(
                    [
                        _Rec(
                            "Real title",
                            2020,
                            authors=("Ada Lovelace", "Alan Turing"),
                            journal="Journal of Synthetic Studies",
                        )
                    ]
                )
            }
        )
        observed = _observed_bibliographic_fields(self._candidate(), discovery)
        self.assertEqual(
            observed,
            {
                "title": "Real title",
                "year": 2020,
                "authors": ["Ada Lovelace", "Alan Turing"],
                "journal": "Journal of Synthetic Studies",
            },
        )
        record = _bibliographic_record(
            "P", self._candidate(), self._CORPUS_REF, self._FREEZE_REF, [], observed
        )
        self.assertEqual(record["authors"], ["Ada Lovelace", "Alan Turing"])
        self.assertEqual(record["journal"], "Journal of Synthetic Studies")

    def test_disagreeing_authors_or_journal_omit_the_field(self):
        discovery = _FakeDiscovery(
            {
                "run1": _Observation([_Rec("T", 2020, authors=("A",), journal="J1")]),
                "run2": _Observation([_Rec("T", 2020, authors=("B",), journal="J2")]),
            }
        )
        candidate = self._candidate(occurrences=[["run1", 0], ["run2", 0]])
        observed = _observed_bibliographic_fields(candidate, discovery)
        self.assertNotIn("authors", observed)
        self.assertNotIn("journal", observed)
        # The title agrees, so it is still carried.
        self.assertEqual(observed["title"], "T")

    def test_absent_fields_are_omitted_never_invented(self):
        discovery = _FakeDiscovery({"run1": _Observation([_Rec(None, None)])})
        observed = _observed_bibliographic_fields(self._candidate(), discovery)
        self.assertEqual(observed, {})
        record = _bibliographic_record(
            "P", self._candidate(), self._CORPUS_REF, self._FREEZE_REF, [], observed
        )
        self.assertNotIn("title", record)
        self.assertNotIn("year", record)
        self.assertNotIn("authors", record)
        self.assertNotIn("journal", record)

    def test_disagreeing_occurrences_omit_the_field(self):
        discovery = _FakeDiscovery(
            {
                "run1": _Observation([_Rec("Title A", 2020)]),
                "run2": _Observation([_Rec("Title B", 2021)]),
            }
        )
        candidate = self._candidate(occurrences=[["run1", 0], ["run2", 0]])
        observed = _observed_bibliographic_fields(candidate, discovery)
        self.assertNotIn("title", observed)
        self.assertNotIn("year", observed)

    def test_identity_only_carries_present_namespaces(self):
        candidate = self._candidate(tokens=[["native:pubmed", "a"], ["pmid", "111"]])
        record = _bibliographic_record(
            "P", candidate, self._CORPUS_REF, self._FREEZE_REF, [], {}
        )
        self.assertEqual(record["identity"], {"pmid": "111"})
        self.assertEqual(record["bibliographic_identifiers"], {"pmid": "111"})


if __name__ == "__main__":
    unittest.main()
