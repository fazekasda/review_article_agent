"""Focused S14-T2 regression suite; pure export rendering/checks, no model calls.

These assert only the mechanical Step 14 export obligations over synthetic
fixtures built in-file: ordered citation keys, consistency checks (invented key,
duplicate DOI, version mismatch, missing reference), deterministic BibTeX/RIS
that preserve keys/identities, fail-closed disclosures, stale-approval and
revision-mismatch rejection, and that generated text never asserts independent
validation or submission compliance.  They cannot establish scientific truth.
"""

import json
import unittest

from review_article_agent.delivery.export import (
    ExportError,
    build_disclosures,
    build_export_package,
    check_consistency,
    cited_keys,
    render_bibtex,
    render_ris,
)


def _ref(key, *, doi=None, pmid=None, version="1", title="T", authors=None, year=2020, journal=None):
    identity = {}
    if doi:
        identity["doi"] = doi
    if pmid:
        identity["pmid"] = pmid
    ref = {
        "key": key,
        "identity": identity,
        "version": version,
        "title": title,
        "authors": list(authors or ["A. Author"]),
        "year": year,
    }
    if journal:
        ref["journal"] = journal
    return ref


CORPUS = {
    "smith2020": {"identity": {"doi": "10.1/x"}, "version": "1"},
    "jones2021": {"identity": {"pmid": "999"}, "version": "2"},
}


class CitedKeysTests(unittest.TestCase):
    def test_ordered_unique_keys(self):
        text = "See [@b; @a] and again [@b, p. 4] and [@c]."
        self.assertEqual(cited_keys(text), ["b", "a", "c"])

    def test_no_citations(self):
        self.assertEqual(cited_keys("plain text"), [])


class CheckConsistencyTests(unittest.TestCase):
    def test_clean(self):
        refs = [_ref("smith2020", doi="10.1/x"), _ref("jones2021", pmid="999", version="2")]
        result = check_consistency("A [@smith2020] and B [@jones2021].", refs, CORPUS)
        self.assertTrue(result["ok"])
        self.assertEqual(result["errors"], [])

    def test_invented_and_missing(self):
        refs = [_ref("ghost", doi="10.9/z")]
        result = check_consistency("A [@smith2020].", refs, CORPUS)
        codes = {e["code"] for e in result["errors"]}
        self.assertFalse(result["ok"])
        self.assertIn("missing_reference", codes)
        self.assertIn("invented_reference", codes)

    def test_duplicate_doi_and_version_mismatch(self):
        refs = [
            _ref("smith2020", doi="10.1/x", version="2"),
            _ref("other", doi="10.1/x"),
        ]
        corpus = dict(CORPUS)
        corpus["other"] = {"identity": {"doi": "10.1/x"}, "version": "1"}
        result = check_consistency("A [@smith2020] and B [@other].", refs, corpus)
        codes = [e["code"] for e in result["errors"]]
        self.assertIn("version_mismatch", codes)
        self.assertIn("duplicate_identity", codes)


class RenderTests(unittest.TestCase):
    def test_bibtex_preserves_key_title_doi(self):
        text = render_bibtex([_ref("smith2020", doi="10.1/x", authors=["A. Author", "B. Writer"])])
        self.assertIn("@article{smith2020", text)
        self.assertIn("title = {T}", text)
        self.assertIn("author = {A. Author and B. Writer}", text)
        self.assertIn("doi = {10.1/x}", text)

    def test_ris_keys_and_tags(self):
        text = render_ris([_ref("jones2021", pmid="999", version="2")])
        self.assertIn("TY  - JOUR", text)
        self.assertIn("ID  - jones2021", text)
        self.assertIn("AN  - 999", text)
        self.assertIn("ER  - ", text)

    def test_render_keys_match_manuscript(self):
        refs = [_ref("smith2020", doi="10.1/x")]
        bib = render_bibtex(refs)
        ris = render_ris(refs)
        for key in cited_keys("A [@smith2020]."):
            self.assertIn("{" + key + ",", bib)
            self.assertIn("ID  - " + key, ris)


class DisclosureTests(unittest.TestCase):
    def test_unaccepted_companion_is_unmet_and_no_compliance_claim(self):
        out = build_disclosures(
            {"companion_accepted": False, "journal_policy_verified": False},
            [{"role": "drafting", "model": "m", "version": "v1"}],
        )
        self.assertEqual(out["reporting_requirement"], "unmet")
        self.assertIn("REPORTING REQUIREMENT UNMET", out["companion_md"])
        self.assertEqual(out["venue_compliance"], "not_asserted")
        for text in (out["companion_md"], out["summary_md"], out["ai_declaration_md"]):
            self.assertNotIn("independently validated", text.lower())
            self.assertNotIn("submission compliance", text.lower())

    def test_accepted_companion_and_venue_verification(self):
        out = build_disclosures(
            {"companion_accepted": True, "journal_policy_verified": True},
            [{"role": "drafting", "model": "m", "version": "v1"}],
            venue="Journal X",
            warnings=[{"claim_id": "c1", "cite_source_key": "smith2020", "text": "secondary account"}],
        )
        self.assertEqual(out["reporting_requirement"], "met")
        self.assertEqual(out["venue_compliance"], "verified_by_human")
        self.assertIn("Secondary-origin warnings", out["companion_md"])
        self.assertIn("c1", out["companion_md"])
        self.assertIn("drafting", out["ai_declaration_md"])


class ExportPackageTests(unittest.TestCase):
    def _fixture(self):
        manuscript = {"revision_id": "rev-1", "text": "A [@smith2020]."}
        refs = [_ref("smith2020", doi="10.1/x")]
        current_hashes = {"manuscript": "abc", "outline": "def"}
        approval = {
            "approved": True,
            "manuscript_revision": "rev-1",
            "artifact_hashes": dict(current_hashes),
        }
        disclosures = build_disclosures(
            {"companion_accepted": True, "journal_policy_verified": False},
            [{"role": "drafting", "model": "m", "version": "v1"}],
        )
        return manuscript, refs, current_hashes, approval, disclosures

    def test_success_manifest_and_files(self):
        manuscript, refs, current_hashes, approval, disclosures = self._fixture()
        out = build_export_package(manuscript, refs, CORPUS, approval, current_hashes, disclosures)
        names = set(out["files"])
        self.assertEqual(
            names,
            {
                "manuscript.md",
                "references.bib",
                "references.ris",
                "companion.md",
                "summary.md",
                "ai_declaration.md",
                "export_manifest.json",
            },
        )
        manifest = json.loads(out["files"]["export_manifest.json"])
        self.assertEqual(manifest["manuscript_revision"], "rev-1")
        self.assertEqual(manifest["approved_hashes"], current_hashes)
        self.assertNotIn("export_manifest.json", manifest["file_hashes"])
        self.assertTrue(out["checks"]["consistency"]["ok"])

    def test_stale_hash_raises(self):
        manuscript, refs, current_hashes, approval, disclosures = self._fixture()
        approval["artifact_hashes"] = {"manuscript": "OLD"}
        with self.assertRaises(ExportError) as ctx:
            build_export_package(manuscript, refs, CORPUS, approval, current_hashes, disclosures)
        self.assertIn("stale approval", str(ctx.exception))

    def test_revision_mismatch_raises(self):
        manuscript, refs, current_hashes, approval, disclosures = self._fixture()
        approval["manuscript_revision"] = "rev-2"
        with self.assertRaises(ExportError):
            build_export_package(manuscript, refs, CORPUS, approval, current_hashes, disclosures)

    def test_consistency_error_aborts(self):
        manuscript, refs, current_hashes, approval, disclosures = self._fixture()
        manuscript = {"revision_id": "rev-1", "text": "A [@smith2020; @ghost]."}
        with self.assertRaises(ExportError):
            build_export_package(manuscript, refs, CORPUS, approval, current_hashes, disclosures)


if __name__ == "__main__":
    unittest.main()
