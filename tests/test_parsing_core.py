"""Step 07 parsing-core tests (S7-CORE).

Covers ``src/review_article_agent/parsing/`` against
``docs/implementation/07-parsing.md`` acceptance criteria 16-22: structural
passages with section paths, captions/assets, local citation-marker links with
explicit states, conservative normalization, stable locators with staleness
detection, XML-threat rejection, and a conservative PDF human-review result
that never promotes a preview to full text and never yields an exclusion.

All fixtures are self-authored, synthetic, and authorized; no real licensed or
network source is used, and no scientific correctness/completeness claim is
made or tested.

Run::

    PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_parsing_core.py'
"""

from __future__ import annotations

import hashlib
import json
import unittest

from review_article_agent.parsing import (
    ParsingRequestError,
    make_locator,
    normalize_text,
    parse_document,
    parse_pdf,
    passage_citation_check,
    resolve_locator,
)

# A minimal, synthetic JATS article: external publisher DOCTYPE (no internal
# subset), an abstract paragraph, two Results paragraphs (resolved, then
# unresolved/ambiguous/malformed markers), a Discussion paragraph exercising
# ligature/hyphen/negation/units, a figure, a table, a supplement, and a
# bibliography with a duplicate id and an unrelated malformed (id-less) entry.
JATS = (
    '<?xml version="1.0" encoding="utf-8"?>\n'
    '<!DOCTYPE article PUBLIC "-//NLM//DTD JATS (Z39.96) Journal Archiving '
    'and Interchange DTD v1.3 20210610//EN" "JATS-archivearticle1-3.dtd">\n'
    '<article xmlns:xlink="http://www.w3.org/1999/xlink">\n'
    "  <front>\n"
    "    <article-meta>\n"
    "      <abstract>\n"
    '        <p>Clearance was 5 mg not 10 mg'
    '<xref ref-type="bibr" rid="ref1">1</xref>.</p>\n'
    "      </abstract>\n"
    "    </article-meta>\n"
    "  </front>\n"
    "  <body>\n"
    "    <sec>\n"
    "      <title>Results</title>\n"
    '      <p>Treatment improved<xref ref-type="bibr" rid="ref1">1</xref>, '
    '<xref ref-type="bibr" rid="ref2">2</xref>.</p>\n'
    '      <p>Unresolved<xref ref-type="bibr" rid="ref9">9</xref> '
    'ambiguous<xref ref-type="bibr" rid="ref3">3</xref> '
    'untagged<xref ref-type="bibr">4</xref>.</p>\n'
    '      <fig id="fig1"><label>Figure 1</label>'
    "<caption><p>A timeline.</p></caption>"
    '<graphic xlink:href="fig1.png"/></fig>\n'
    '      <table-wrap id="tab1"><label>Table 1</label>'
    "<caption><p>Baseline values.</p></caption><table/></table-wrap>\n"
    "    </sec>\n"
    "    <sec>\n"
    "      <title>Discussion</title>\n"
    "      <p>A well-known e\ufb00ect (inter-\nstitial) was not reproduced "
    "in 5\u201310 mg.</p>\n"
    "    </sec>\n"
    "  </body>\n"
    "  <back>\n"
    "    <ref-list>\n"
    '      <ref id="ref1"><label>1</label>'
    "<mixed-citation>Alpha et al.</mixed-citation></ref>\n"
    '      <ref id="ref2"><label>2</label>'
    "<mixed-citation>Beta et al.</mixed-citation></ref>\n"
    '      <ref id="ref3"><label>3</label>'
    "<mixed-citation>Gamma et al.</mixed-citation></ref>\n"
    '      <ref id="ref3"><label>3</label>'
    "<mixed-citation>Gamma duplicate.</mixed-citation></ref>\n"
    "      <ref><mixed-citation>Anonymous malformed.</mixed-citation></ref>\n"
    "    </ref-list>\n"
    '    <supplementary-material id="sup1"><label>Supplement 1</label>'
    "<caption><p>Raw data.</p></caption></supplementary-material>\n"
    "  </back>\n"
    "</article>\n"
)

TEI = (
    '<?xml version="1.0" encoding="utf-8"?>\n'
    '<TEI xmlns="http://www.tei-c.org/ns/1.0">\n'
    "  <text>\n"
    "    <body>\n"
    "      <div>\n"
    "        <head>Introduction</head>\n"
    "        <p>See prior work"
    '<ref type="bibr" target="#b1">[1]</ref> and more.</p>\n'
    "      </div>\n"
    "      <div>\n"
    "        <head>Methods</head>\n"
    "        <p>We used no sham controls.</p>\n"
    "      </div>\n"
    "    </body>\n"
    "    <back>\n"
    "      <div>\n"
    "        <listBibl>\n"
    '          <bibl xml:id="b1">Author A. 2020.</bibl>\n'
    "        </listBibl>\n"
    "      </div>\n"
    "    </back>\n"
    "  </text>\n"
    "</TEI>\n"
)


def _passage(parsed, passage_id):
    for passage in parsed["passages"]:
        if passage["passage_id"] == passage_id:
            return passage
    raise AssertionError(f"passage {passage_id} missing")


def _link_states(parsed, passage_id):
    return [
        link["state"]
        for link in parsed["citation_links"]
        if link["passage_id"] == passage_id
    ]


class JatsParsingTests(unittest.TestCase):
    def setUp(self):
        self.parsed = parse_document(JATS.encode("utf-8"))

    def test_structure_and_section_paths(self):
        self.assertEqual(self.parsed["format"], "jats")
        self.assertEqual(self.parsed["status"], "parsed")
        self.assertEqual(self.parsed["parser_version"], "step07-jats-tei/1")
        self.assertEqual(self.parsed["normalization_version"], "nfc-ligature-ws/1")
        self.assertEqual(self.parsed["source"]["byte_size"], len(JATS.encode("utf-8")))
        self.assertEqual(
            self.parsed["source"]["sha256"],
            hashlib.sha256(JATS.encode("utf-8")).hexdigest(),
        )
        # stable sequential ids
        self.assertEqual(
            [p["passage_id"] for p in self.parsed["passages"]],
            ["p0001", "p0002", "p0003", "p0004"],
        )
        self.assertEqual(_passage(self.parsed, "p0001")["section_path"], ["Abstract"])
        self.assertEqual(_passage(self.parsed, "p0002")["section_path"], ["Results"])
        self.assertEqual(_passage(self.parsed, "p0003")["section_path"], ["Results"])
        self.assertEqual(
            _passage(self.parsed, "p0004")["section_path"], ["Discussion"]
        )
        self.assertIn("Treatment improved", _passage(self.parsed, "p0002")["text"])
        # digest is of the normalized text
        p2 = _passage(self.parsed, "p0002")
        self.assertEqual(
            p2["text_sha256"],
            hashlib.sha256(p2["text"].encode("utf-8")).hexdigest(),
        )
        # never a semantic claim
        self.assertFalse(self.parsed["authorizes_scientific_processing"])

    def test_citation_link_states(self):
        self.assertEqual(_link_states(self.parsed, "p0002"), ["resolved", "resolved"])
        self.assertEqual(
            sorted(_link_states(self.parsed, "p0003")),
            ["ambiguous", "malformed", "unresolved"],
        )
        self.assertEqual(
            self.parsed["citations"],
            {"resolved": 3, "unresolved": 1, "ambiguous": 1, "malformed": 1},
        )

    def test_captions_and_non_text_assets(self):
        kinds = {asset["kind"]: asset for asset in self.parsed["assets"]}
        self.assertEqual(set(kinds), {"figure", "table", "supplement"})
        self.assertEqual(kinds["figure"]["caption"], "A timeline.")
        self.assertEqual(kinds["figure"]["label"], "Figure 1")
        self.assertEqual(kinds["figure"]["href"], "fig1.png")
        self.assertEqual(kinds["table"]["caption"], "Baseline values.")
        self.assertEqual(kinds["supplement"]["caption"], "Raw data.")
        self.assertTrue(self.parsed["non_text_assets_require_review"])
        self.assertTrue(self.parsed["human_action_required"])
        self.assertIn("non_text_assets_present", self.parsed["reasons"])
        self.assertIn("jats_doctype_removed", self.parsed["reasons"])

    def test_result_is_json_serializable(self):
        json.dumps(self.parsed)


class NormalizationTests(unittest.TestCase):
    def test_ligature_hyphen_negation_units_preserved(self):
        parsed = parse_document(JATS.encode("utf-8"))
        passage = _passage(parsed, "p0004")
        text = passage["text"]
        self.assertIn("effect", text)  # \ufb00 folded to ff
        self.assertIn("inter-", text)  # ambiguous hyphen preserved
        self.assertNotIn("interstitial", text)  # never aggressively joined
        self.assertIn("not reproduced", text)  # negation preserved
        self.assertIn("5\u201310 mg", text)  # en-dash and unit untouched
        self.assertIn("ligature_fold", passage["transformations"])
        self.assertIn("whitespace_collapsed", passage["transformations"])
        # raw and normalized are distinct revisions
        self.assertNotEqual(passage["raw_text"], passage["text"])

    def test_normalize_text_direct(self):
        text, transforms = normalize_text("\ufb01ne  co\u00adoperate")
        self.assertEqual(text, "fine cooperate")
        self.assertIn("ligature_fold", transforms)
        self.assertIn("soft_hyphen_removed", transforms)
        self.assertIn("whitespace_collapsed", transforms)


class TeiParsingTests(unittest.TestCase):
    def test_div_passages_and_links(self):
        parsed = parse_document(TEI.encode("utf-8"))
        self.assertEqual(parsed["format"], "tei")
        self.assertEqual(parsed["status"], "parsed")
        self.assertEqual(
            [p["passage_id"] for p in parsed["passages"]], ["p0001", "p0002"]
        )
        self.assertEqual(_passage(parsed, "p0001")["section_path"], ["Introduction"])
        self.assertEqual(_passage(parsed, "p0002")["section_path"], ["Methods"])
        self.assertEqual(_link_states(parsed, "p0001"), ["resolved"])
        self.assertEqual(parsed["references"][0]["ref_id"], "b1")

    def test_biblstruct_reference_resolves(self):
        data = (
            '<TEI xmlns="http://www.tei-c.org/ns/1.0"><text><body>'
            "<div><head>Intro</head>"
            '<p>See<ref type="bibr" target="#b1">[1]</ref>.</p></div>'
            "</body><back><div><listBibl>"
            '<biblStruct xml:id="b1"><monogr><title>Study</title></monogr>'
            "</biblStruct></listBibl></div></back></text></TEI>"
        )
        parsed = parse_document(data.encode("utf-8"))
        self.assertEqual(parsed["format"], "tei")
        self.assertEqual(_link_states(parsed, "p0001"), ["resolved"])
        self.assertTrue(parsed["references"][0]["usable"])



class CitationIsolationTests(unittest.TestCase):
    def test_unrelated_malformed_bibliography_does_not_invalidate_all(self):
        parsed = parse_document(JATS.encode("utf-8"))
        good = passage_citation_check(parsed, "p0002")
        self.assertTrue(good["ok"])
        bad = passage_citation_check(parsed, "p0003")
        self.assertFalse(bad["ok"])
        self.assertEqual(len(bad["unresolved"]), 1)
        self.assertEqual(len(bad["ambiguous"]), 1)
        self.assertEqual(len(bad["malformed"]), 1)
        # the id-less ("malformed") bibliography entry is recorded but never
        # breaks a link that points at a valid id
        self.assertTrue(any(ref["malformed"] for ref in parsed["references"]))

    def test_unusable_id_entry_is_local_malformed(self):
        data = (
            "<article><body><sec><title>S</title>"
            '<p>Good<xref ref-type="bibr" rid="ok">1</xref>.</p>'
            '<p>Bad<xref ref-type="bibr" rid="empty">2</xref>.</p>'
            "</sec></body><back><ref-list>"
            '<ref id="ok"><mixed-citation>Real entry.</mixed-citation></ref>'
            '<ref id="empty"><label>2</label></ref>'
            "</ref-list></back></article>"
        )
        parsed = parse_document(data.encode("utf-8"))
        by_id = {r["ref_id"]: r for r in parsed["references"]}
        self.assertTrue(by_id["ok"]["usable"])
        self.assertFalse(by_id["empty"]["usable"])
        # the label-only target is malformed *locally*, the good link survives
        self.assertEqual(_link_states(parsed, "p0002"), ["malformed"])
        self.assertTrue(passage_citation_check(parsed, "p0001")["ok"])
        self.assertFalse(passage_citation_check(parsed, "p0002")["ok"])



class LocatorTests(unittest.TestCase):
    def setUp(self):
        self.parsed = parse_document(JATS.encode("utf-8"))
        self.locator = make_locator(self.parsed, "p0002")

    def test_locator_round_trip_and_fields(self):
        self.assertEqual(self.locator["passage_id"], "p0002")
        self.assertEqual(self.locator["section_path"], ["Results"])
        self.assertEqual(
            self.locator["source_sha256"], self.parsed["source"]["sha256"]
        )
        self.assertIn("locator_sha256", self.locator)
        resolved = resolve_locator(self.locator, self.parsed)
        self.assertEqual(resolved["status"], "resolved")
        self.assertTrue(resolved["locator_ok"])
        self.assertEqual(resolved["reasons"], [])
        self.assertTrue(resolved["citation_check"]["ok"])

    def test_changed_source_digest_is_stale(self):
        changed = parse_document(JATS.replace("Alpha", "Gamma").encode("utf-8"))
        result = resolve_locator(self.locator, changed)
        self.assertEqual(result["status"], "stale")
        self.assertIn("source_digest_changed", result["reasons"])

    def test_changed_parser_and_normalization_version_are_stale(self):
        bumped_parser = json.loads(json.dumps(self.parsed))
        bumped_parser["parser_version"] = "future"
        self.assertIn(
            "parser_version_changed",
            resolve_locator(self.locator, bumped_parser)["reasons"],
        )
        bumped_norm = json.loads(json.dumps(self.parsed))
        bumped_norm["normalization_version"] = "future"
        self.assertIn(
            "normalization_version_changed",
            resolve_locator(self.locator, bumped_norm)["reasons"],
        )

    def test_changed_passage_text_is_stale(self):
        mutated = json.loads(json.dumps(self.parsed))
        _passage(mutated, "p0002")["text"] += " CHANGED"
        result = resolve_locator(self.locator, mutated)
        self.assertEqual(result["status"], "stale")
        self.assertIn("passage_text_changed", result["reasons"])

    def test_missing_passage(self):
        empty = json.loads(json.dumps(self.parsed))
        empty["passages"] = []
        result = resolve_locator(self.locator, empty)
        self.assertEqual(result["status"], "missing")
        self.assertIn("passage_not_found", result["reasons"])

    def test_locator_validates_digest_section_path_and_actual_text(self):
        # text-only mutation (internal text_sha256 untouched) must not resolve
        mutated = json.loads(json.dumps(self.parsed))
        _passage(mutated, "p0002")["text"] += " CHANGED"
        result = resolve_locator(self.locator, mutated)
        self.assertEqual(result["status"], "stale")
        self.assertIn("passage_text_changed", result["reasons"])

        moved = json.loads(json.dumps(self.parsed))
        _passage(moved, "p0002")["section_path"] = ["Elsewhere"]
        self.assertIn("section_path_changed", resolve_locator(self.locator, moved)["reasons"])

        inconsistent = json.loads(json.dumps(self.parsed))
        _passage(inconsistent, "p0002")["text_sha256"] = "0" * 64
        self.assertIn(
            "passage_text_hash_inconsistent",
            resolve_locator(self.locator, inconsistent)["reasons"],
        )

        tampered = dict(self.locator)
        tampered["locator_sha256"] = "0" * 64
        result = resolve_locator(tampered, self.parsed)
        self.assertEqual(result["status"], "stale")
        self.assertIn("locator_digest_invalid", result["reasons"])



class XmlThreatTests(unittest.TestCase):
    def test_entity_declaration_rejected(self):
        data = (
            '<!DOCTYPE article [ <!ENTITY x "y"> ]>'
            '<article><body><p>&x;</p></body></article>'
        )
        parsed = parse_document(data.encode("utf-8"))
        self.assertEqual(parsed["status"], "rejected")
        self.assertIn("xml_entity_declaration", parsed["reasons"])

    def test_internal_subset_rejected(self):
        data = '<!DOCTYPE article [ <!ELEMENT article ANY> ]><article/>'
        parsed = parse_document(data.encode("utf-8"))
        self.assertEqual(parsed["status"], "rejected")
        self.assertIn("xml_internal_subset", parsed["reasons"])

    def test_undefined_entity_rejected(self):
        parsed = parse_document(b"<article><p>&foo;</p></article>")
        self.assertEqual(parsed["status"], "rejected")
        self.assertIn("xml_undefined_entity", parsed["reasons"])

    def test_unsupported_root_is_human_review_not_exclusion(self):
        parsed = parse_document(b"<foo><p>text</p></foo>")
        self.assertEqual(parsed["format"], "unsupported")
        self.assertEqual(parsed["status"], "needs_review")
        self.assertTrue(parsed["human_action_required"])
        self.assertFalse(parsed["excludes_candidate"])
        self.assertEqual(parsed["passages"], [])

    def test_empty_input_is_human_review_not_exclusion(self):
        parsed = parse_document(b"")
        self.assertEqual(parsed["status"], "rejected")
        self.assertIn("empty_input", parsed["reasons"])
        self.assertTrue(parsed["human_action_required"])
        self.assertFalse(parsed["excludes_candidate"])
        self.assertEqual(parsed["passages"], [])

    def test_empty_body_is_human_review_not_exclusion(self):
        parsed = parse_document(b"<article><body/></article>")
        self.assertEqual(parsed["status"], "needs_review")
        self.assertIn("no_substantive_text", parsed["reasons"])
        self.assertTrue(parsed["human_action_required"])
        self.assertFalse(parsed["excludes_candidate"])

    def test_source_digest_must_match_bytes(self):
        data = JATS.encode("utf-8")
        good = hashlib.sha256(data).hexdigest()
        self.assertEqual(
            parse_document(data, source_digest=good)["source"]["sha256"], good
        )
        with self.assertRaises(ParsingRequestError):
            parse_document(data, source_digest="0" * 64)



class PdfAdapterTests(unittest.TestCase):
    def test_inspected_scan_and_column_uncertainty(self):
        def fake(data):
            return {
                "status": "inspected",
                "encrypted": False,
                "page_count": 2,
                "page_text_present": [True, False],
                "text_preview": "PRIVATE-SECRET",
                "identifiers": {"doi": None, "pmid": None, "pmcid": None},
                "failure_code": None,
                "parser_version": "fake",
            }

        result = parse_pdf(b"%PDF-1.4 synthetic", inspector=fake)
        self.assertEqual(result["format"], "pdf")
        self.assertEqual(result["status"], "needs_review")
        self.assertEqual(result["passages"], [])
        self.assertTrue(result["human_action_required"])
        self.assertFalse(result["excludes_candidate"])
        self.assertIn("pdf_full_extraction_unsupported", result["reasons"])
        self.assertIn("pdf_scan_or_blank_page", result["reasons"])
        self.assertIn("pdf_column_order_uncertain", result["reasons"])
        # preview is never promoted or even retained
        self.assertNotIn("text_preview", result["pdf_inspection"])
        self.assertNotIn("PRIVATE-SECRET", json.dumps(result))

    def test_unavailable_and_failed_map_to_human_action(self):
        unavailable = parse_pdf(
            b"%PDF-1.4 synthetic",
            inspector=lambda data: {"status": "unavailable", "failure_code": "dependency_missing"},
        )
        self.assertIn("ocr_or_parser_unavailable", unavailable["reasons"])
        self.assertTrue(unavailable["human_action_required"])

        failed = parse_pdf(
            b"%PDF-1.4 synthetic",
            inspector=lambda data: {"status": "failed", "failure_code": "malformed_pdf"},
        )
        self.assertIn("pdf_malformed", failed["reasons"])
        self.assertFalse(failed["excludes_candidate"])

    def test_empty_input_does_not_invoke_inspector(self):
        def boom(data):  # pragma: no cover - must never be called
            raise AssertionError("inspector must not run on empty input")

        result = parse_pdf(b"", inspector=boom)
        self.assertIn("pdf_empty_input", result["reasons"])
        self.assertEqual(result["passages"], [])
        self.assertFalse(result["excludes_candidate"])


class RequestErrorTests(unittest.TestCase):
    def test_bad_arguments_raise_request_error(self):
        with self.assertRaises(ParsingRequestError):
            parse_document("not-bytes")
        with self.assertRaises(ParsingRequestError):
            make_locator(parse_document(JATS.encode("utf-8")), "p9999")


if __name__ == "__main__":
    unittest.main()
