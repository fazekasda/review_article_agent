"""Deterministic, fail-closed tests for acquisition asset validation (Step 06).

Covers ``src/review_article_agent/acquisition/validation.py`` against
``docs/implementation/06-acquisition.md`` AC2/AC6/AC7 and specification
workflow step 6:

* a file extension or declared ``Content-Type`` never establishes success: an
  HTML/login page is rejected even when named/declared ``.pdf``;
* a grossly corrupt PDF is rejected; a structurally plausible PDF stays
  ``needs_review`` with ``unknown`` scan/text/identity checks (a scan or a raw
  byte DOI is never promoted to success);
* malformed XML, an internal subset, entity declarations (billion laughs / XXE),
  a second DOCTYPE, and non-UTF-8 XML are rejected without any external access;
* a benign publisher JATS ``DOCTYPE`` (no internal subset) is safely removed and
  a well-formed JATS full article parses;
* JATS identity comes only from ``front/article-meta`` (never bibliography ids)
  and must be exactly consistent; a conflict is rejected, a missing/unknown one
  is ``needs_review``;
* bounded node count / depth / text and the 32 MiB byte cap are explicit
  rejections, never truncation;
* the result is JSON-safe, deterministic, carries the required schema, and
  never authorises scientific processing;
* malformed *requests* raise ``AcquisitionValidationError``.

No network, no credentials, no persistence.  Run:

    PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_acquisition_validation.py'
"""

from __future__ import annotations

import hashlib
import json
import sys
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from review_article_agent.acquisition import (  # noqa: E402
    CHECK_NAMES,
    CHECK_STATES,
    DOCUMENT_TYPE,
    IDENTIFIER_NAMESPACES,
    MAX_BYTE_SIZE,
    MEDIA_KINDS,
    REASONS,
    SCHEMA_VERSION,
    STATUSES,
    VALIDATOR_VERSION,
    AcquisitionValidationError,
    validate_asset,
)
from review_article_agent.acquisition import validation as validation_mod  # noqa: E402
from review_article_agent.domain.errors import ReviewAgentError  # noqa: E402


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
JATS_DOCTYPE = (
    b'<!DOCTYPE article PUBLIC "-//NLM//DTD JATS (Z39.96) Journal Archiving '
    b'and Interchange DTD v1.2 20190208//EN" "JATS-archivearticle1-2.dtd">'
)


def jats(
    *,
    pmid: bytes | None = b"12345678",
    pmcid: bytes | None = b"PMC1234567",
    doi: bytes | None = b"10.1234/AbC.dEf",
    body: bytes | None = b"<sec><p>Hello scientific world.</p></sec>",
    doctype: bytes | None = None,
    declaration: bytes = b'<?xml version="1.0" encoding="UTF-8"?>\n',
    back: bytes = b"",
) -> bytes:
    ids = b""
    if pmid is not None:
        ids += b'<article-id pub-id-type="pmid">' + pmid + b"</article-id>"
    if pmcid is not None:
        ids += b'<article-id pub-id-type="pmcid">' + pmcid + b"</article-id>"
    if doi is not None:
        ids += b'<article-id pub-id-type="doi">' + doi + b"</article-id>"
    front = b"<front><journal-meta/><article-meta>" + ids + b"</article-meta></front>"
    body_xml = b"<body>" + body + b"</body>" if body is not None else b""
    doctype_xml = doctype + b"\n" if doctype is not None else b""
    return declaration + doctype_xml + b"<article>" + front + body_xml + back + b"</article>"


GOOD_PDF = (
    b"%PDF-1.4\n"
    b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n"
    b"2 0 obj\n<< /Type /Pages /Kids [] /Count 0 >>\nendobj\n"
    b"xref\n0 3\n0000000000 65535 f \n"
    b"trailer\n<< /Size 3 /Root 1 0 R >>\n"
    b"startxref\n0\n%%EOF\n"
)

LOGIN_HTML = (
    b"<!DOCTYPE html>\n<html><head><title>Sign in</title></head>"
    b"<body><form><input type=\"password\" name=\"password\">"
    b"<button>Sign in</button></form></body></html>"
)

PLAIN_HTML = b"<!DOCTYPE html><html><body><h1>Not a paper</h1></body></html>"

#: Step 06 known HTML/login sources that must never be sniffed as a PDF/unknown
#: even when named ``.pdf`` / declared ``application/pdf`` (AC2).
BOM_LOGIN_HTML = (
    b"\xef\xbb\xbf<html><body><form><input type=\"password\"/>"
    b"Log in</form></body></html>"
)
LEADING_HTML_FAKE_PDF = (
    b"<html><body>login %PDF-1.7 1 0 obj %%EOF</body></html>"
)


class _Base(unittest.TestCase):
    def call(self, data, expected=None, declared=None):
        return validate_asset(
            data,
            expected_identifiers=expected or {},
            declared_media_type=declared,
        )


# --------------------------------------------------------------------------- #
# Schema, determinism, JSON-safety
# --------------------------------------------------------------------------- #
class TestResultShape(_Base):
    def test_required_keys_and_types(self):
        result = self.call(jats(), {"pmid": "12345678"})
        self.assertEqual(result["schema_version"], SCHEMA_VERSION)
        self.assertEqual(result["document_type"], DOCUMENT_TYPE)
        self.assertEqual(result["validator_version"], VALIDATOR_VERSION)
        self.assertRegex(result["sha256"], r"^[0-9a-f]{64}$")
        self.assertIsInstance(result["byte_size"], int)
        self.assertIn(result["media_kind"], MEDIA_KINDS)
        self.assertIn(result["status"], STATUSES)
        self.assertEqual(tuple(result["checks"].keys()), CHECK_NAMES)
        for state in result["checks"].values():
            self.assertIn(state, CHECK_STATES)
        self.assertIsInstance(result["observed_identifiers"], dict)
        self.assertIsInstance(result["reasons"], list)
        for reason in result["reasons"]:
            self.assertIn(reason, REASONS)
        self.assertIs(result["authorizes_scientific_processing"], False)

    def test_json_safe_and_round_trips(self):
        result = self.call(jats(), {"pmid": "12345678"})
        text = json.dumps(result, sort_keys=True)
        self.assertEqual(json.loads(text), result)

    def test_sha256_and_byte_size_are_exact(self):
        data = jats()
        result = self.call(data)
        self.assertEqual(result["sha256"], hashlib.sha256(data).hexdigest())
        self.assertEqual(result["byte_size"], len(data))

    def test_deterministic(self):
        data = jats()
        self.assertEqual(self.call(data), self.call(data))

    def test_ready_never_authorizes_digestion(self):
        result = self.call(jats(), {"pmid": "12345678", "doi": "10.1234/abc.def"})
        self.assertEqual(result["status"], "ready")
        self.assertIs(result["authorizes_scientific_processing"], False)
        self.assertIn("never authorises", result["limitations"])


# --------------------------------------------------------------------------- #
# AC2: extension / content-type never establishes success (HTML/login)
# --------------------------------------------------------------------------- #
class TestHtmlNeverSucceeds(_Base):
    def test_login_page_rejected_even_declared_pdf(self):
        result = self.call(LOGIN_HTML, declared="application/pdf")
        self.assertEqual(result["media_kind"], "html")
        self.assertEqual(result["status"], "rejected")
        self.assertIn("html_login_page", result["reasons"])
        self.assertIn("html_not_an_article", result["reasons"])
        self.assertIn("content_type_mismatch", result["reasons"])

    def test_plain_html_rejected(self):
        result = self.call(PLAIN_HTML, declared="text/html")
        self.assertEqual(result["media_kind"], "html")
        self.assertEqual(result["status"], "rejected")
        self.assertIn("html_not_an_article", result["reasons"])
        self.assertNotIn("content_type_mismatch", result["reasons"])

    def test_html_without_extension_hint_is_still_html(self):
        # No declared media type at all; content decides.
        result = self.call(LOGIN_HTML)
        self.assertEqual(result["media_kind"], "html")
        self.assertEqual(result["status"], "rejected")

    def test_html_never_ready(self):
        for declared in (None, "application/pdf", "text/html", "application/xml"):
            self.assertNotEqual(self.call(LOGIN_HTML, declared=declared)["status"], "ready")

    def test_bom_prefixed_login_html_is_rejected_not_unknown(self):
        # A leading UTF-8 BOM must not hide the HTML root and demote the asset to
        # an inert "unknown" that a later human review might try to waive.
        result = self.call(
            BOM_LOGIN_HTML, {"pmid": "123"}, declared="application/pdf"
        )
        self.assertEqual(result["media_kind"], "html")
        self.assertEqual(result["status"], "rejected")
        self.assertIn("html_not_an_article", result["reasons"])
        self.assertIn("html_login_page", result["reasons"])
        self.assertIn("content_type_mismatch", result["reasons"])
        self.assertNotIn("unrecognized_container", result["reasons"])

    def test_leading_html_with_incidental_pdf_magic_is_hard_rejected(self):
        # A login/HTML body that merely *contains* PDF magic must not be sniffed
        # as a PDF ("pdf_not_inspected" would be a waivable needs_review).
        result = self.call(
            LEADING_HTML_FAKE_PDF, {"pmid": "123"}, declared="application/pdf"
        )
        self.assertEqual(result["media_kind"], "html")
        self.assertEqual(result["status"], "rejected")
        self.assertIn("html_not_an_article", result["reasons"])
        self.assertNotIn("pdf_not_inspected", result["reasons"])


# --------------------------------------------------------------------------- #
# AC2: PDF -- corrupt rejected, plausible needs_review (never ready)
# --------------------------------------------------------------------------- #
class TestPdfFailClosed(_Base):
    def test_good_pdf_is_needs_review_not_ready(self):
        result = self.call(GOOD_PDF, declared="application/pdf")
        self.assertEqual(result["media_kind"], "pdf")
        self.assertEqual(result["status"], "needs_review")
        self.assertEqual(result["checks"]["container"], "pass")
        self.assertEqual(result["checks"]["full_text"], "unknown")
        self.assertEqual(result["checks"]["identity"], "unknown")
        self.assertEqual(result["checks"]["text_quality"], "unknown")
        self.assertIn("pdf_not_inspected", result["reasons"])

    def test_scan_pdf_is_needs_review(self):
        # A plausible image-only (scanned) PDF is not distinguishable without a
        # real inspector: it must not be claimed as having full text.
        scan = GOOD_PDF.replace(
            b"<< /Type /Pages /Kids [] /Count 0 >>",
            b"<< /Type /Pages /Kids [] /Count 0 /XObject <</Im1 3 0 R>> /Subtype /Image >>",
        )
        result = self.call(scan, declared="application/pdf")
        self.assertEqual(result["status"], "needs_review")
        self.assertEqual(result["checks"]["full_text"], "unknown")

    def test_pdf_without_eof_is_corrupt(self):
        result = self.call(b"%PDF-1.7\n1 0 obj\n<< >>\nendobj\n")
        self.assertEqual(result["status"], "rejected")
        self.assertIn("pdf_gross_corruption", result["reasons"])
        self.assertEqual(result["checks"]["container"], "fail")

    def test_pdf_without_version_digit_is_corrupt(self):
        result = self.call(b"%PDF-\n1 0 obj\n%%EOF\n")
        self.assertEqual(result["status"], "rejected")
        self.assertIn("pdf_gross_corruption", result["reasons"])

    def test_pdf_without_objects_is_corrupt(self):
        result = self.call(b"%PDF-1.4\n%%EOF\n")
        self.assertEqual(result["status"], "rejected")
        self.assertIn("pdf_gross_corruption", result["reasons"])

    def test_raw_byte_doi_never_promoted_to_identity(self):
        payload = GOOD_PDF + b"\n10.9999/not-a-real-doi-in-bytes\n"
        result = self.call(payload, {"doi": "10.9999/not-a-real-doi-in-bytes"})
        self.assertEqual(result["status"], "needs_review")
        self.assertEqual(result["observed_identifiers"], {})
        self.assertEqual(result["checks"]["identity"], "unknown")

    def test_pdf_declared_as_html_flags_mismatch(self):
        result = self.call(GOOD_PDF, declared="text/html")
        self.assertIn("content_type_mismatch", result["reasons"])
        self.assertEqual(result["status"], "needs_review")

    def test_genuine_pdf_header_with_later_html_string_stays_pdf(self):
        # An incidental "<html" *after* a real leading PDF header must not
        # hijack the sniff: only a leading HTML root takes precedence.
        data = GOOD_PDF + b"\n<html><body>appended note</body></html>\n"
        result = self.call(data, declared="application/pdf")
        self.assertEqual(result["media_kind"], "pdf")
        self.assertEqual(result["status"], "needs_review")
        self.assertIn("pdf_not_inspected", result["reasons"])
        self.assertNotIn("html_not_an_article", result["reasons"])


# --------------------------------------------------------------------------- #
# AC2/AC6: JATS parsing, DOCTYPE handling, identity
# --------------------------------------------------------------------------- #
class TestJatsHappyPath(_Base):
    def test_full_article_ready_with_exact_identity(self):
        data = jats(doctype=JATS_DOCTYPE)
        result = self.call(
            data,
            {"pmid": "12345678", "pmcid": "PMC1234567", "doi": "10.1234/abc.def"},
            declared="application/xml",
        )
        self.assertEqual(result["media_kind"], "jats_xml")
        self.assertEqual(result["status"], "ready")
        self.assertTrue(all(v == "pass" for v in result["checks"].values()))
        self.assertEqual(
            result["observed_identifiers"],
            {
                "pmid": "12345678",
                "pmcid": "PMC1234567",
                "doi": "10.1234/abc.def",
            },
        )
        self.assertIn("jats_doctype_removed", result["reasons"])

    def test_benign_doctype_without_external_id_removed(self):
        data = jats(doctype=b"<!DOCTYPE article>")
        result = self.call(data, {"pmid": "12345678"})
        self.assertEqual(result["status"], "ready")
        self.assertIn("jats_doctype_removed", result["reasons"])

    def test_identifier_normalization(self):
        data = jats(
            pmid=b" 12345678 ",
            pmcid=b"pmc1234567",
            doi=b"https://doi.org/10.1234/ABC.DEF",
        )
        result = self.call(
            data,
            {"pmid": "12345678", "pmcid": "PMC1234567", "doi": "10.1234/abc.def"},
        )
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["observed_identifiers"]["pmcid"], "PMC1234567")
        self.assertEqual(result["observed_identifiers"]["doi"], "10.1234/abc.def")

    def test_bom_prefixed_full_article_ready_with_exact_identity(self):
        # A UTF-8 BOM before the XML declaration must not defeat JATS parsing.
        data = b"\xef\xbb\xbf" + jats(doctype=JATS_DOCTYPE)
        result = self.call(
            data,
            {"pmid": "12345678", "pmcid": "PMC1234567", "doi": "10.1234/abc.def"},
            declared="application/xml",
        )
        self.assertEqual(result["media_kind"], "jats_xml")
        self.assertEqual(result["status"], "ready")
        self.assertEqual(
            result["observed_identifiers"],
            {
                "pmid": "12345678",
                "pmcid": "PMC1234567",
                "doi": "10.1234/abc.def",
            },
        )

    def test_extra_observed_namespace_does_not_break_match(self):
        data = jats(pmid=b"12345678", pmcid=b"PMC1", doi=b"10.1234/x")
        result = self.call(data, {"pmid": "12345678"})
        self.assertEqual(result["checks"]["identity"], "pass")
        self.assertEqual(result["status"], "ready")


class TestJatsIdentityFailClosed(_Base):
    def test_mismatched_pmid_rejected(self):
        result = self.call(jats(pmid=b"11111111"), {"pmid": "22222222"})
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["checks"]["identity"], "fail")
        self.assertIn("identity_mismatch", result["reasons"])

    def test_mismatched_doi_rejected(self):
        result = self.call(jats(doi=b"10.1234/wrong"), {"doi": "10.1234/right"})
        self.assertEqual(result["status"], "rejected")
        self.assertIn("identity_mismatch", result["reasons"])

    def test_mismatched_pmcid_rejected(self):
        result = self.call(jats(pmcid=b"PMC1"), {"pmcid": "PMC2"})
        self.assertEqual(result["status"], "rejected")
        self.assertIn("identity_mismatch", result["reasons"])

    def test_expected_namespace_absent_needs_review(self):
        result = self.call(jats(doi=None), {"doi": "10.1234/abc"})
        self.assertEqual(result["status"], "needs_review")
        self.assertEqual(result["checks"]["identity"], "unknown")
        self.assertIn("identity_unconfirmed", result["reasons"])

    def test_no_expected_identifiers_needs_review(self):
        result = self.call(jats())
        self.assertEqual(result["status"], "needs_review")
        self.assertEqual(result["checks"]["identity"], "unknown")
        self.assertIn("identity_unconfirmed", result["reasons"])

    def test_missing_front_identity_needs_review(self):
        data = jats(pmid=None, pmcid=None, doi=None)
        result = self.call(data, {"pmid": "12345678"})
        self.assertEqual(result["status"], "needs_review")
        self.assertIn("identity_unconfirmed", result["reasons"])

    def test_bibliography_ids_are_not_identity(self):
        back = (
            b"<back><ref-list><ref>"
            b'<article-id pub-id-type="pmid">77777777</article-id>'
            b"</ref></ref-list></back>"
        )
        data = jats(pmid=None, pmcid=None, doi=None, back=back)
        result = self.call(data, {"pmid": "77777777"})
        self.assertEqual(result["observed_identifiers"], {})
        self.assertEqual(result["status"], "needs_review")
        self.assertIn("identity_unconfirmed", result["reasons"])

    def test_malformed_front_identifier_needs_review(self):
        data = jats(pmid=None, pmcid=None, doi=b"not-a-doi")
        result = self.call(data)
        self.assertEqual(result["status"], "needs_review")
        self.assertIn("identifier_malformed", result["reasons"])


class TestJatsStructureAnchoring(_Base):
    @staticmethod
    def _ids(*pairs, body=b"<p>Test text</p>"):
        ids = b"".join(
            b'<article-id pub-id-type="' + t + b'">' + v + b"</article-id>"
            for t, v in pairs
        )
        return (
            b"<article><front><article-meta>" + ids
            + b"</article-meta></front><body>" + body + b"</body></article>"
        )

    def test_conflicting_repeated_ids_rejected_in_both_orders(self):
        for first, second in ((b"999", b"123"), (b"123", b"999")):
            result = self.call(
                self._ids((b"pmid", first), (b"pmid", second)), {"pmid": "123"}
            )
            self.assertEqual(result["status"], "rejected", (first, second))
            self.assertEqual(result["checks"]["identity"], "fail")
            self.assertIn("identifier_conflict", result["reasons"])
            self.assertNotIn("pmid", result["observed_identifiers"])

    def test_conflicting_ids_rejected_even_without_expected(self):
        result = self.call(self._ids((b"pmid", b"1"), (b"pmid", b"2")), {})
        self.assertEqual(result["status"], "rejected")

    def test_identical_repeated_ids_accepted(self):
        result = self.call(
            self._ids((b"pmid", b"123"), (b"pmid", b"123")), {"pmid": "123"}
        )
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["observed_identifiers"], {"pmid": "123"})

    def test_malformed_duplicate_never_ready_in_either_order(self):
        for pair in (
            ((b"pmid", b"abc"), (b"pmid", b"123")),
            ((b"pmid", b"123"), (b"pmid", b"abc")),
        ):
            result = self.call(self._ids(*pair), {"pmid": "123"})
            self.assertNotEqual(result["status"], "ready", pair)

    def test_sub_article_cannot_establish_outer_identity_or_body(self):
        data = (
            b"<article><sub-article><front><article-meta>"
            b'<article-id pub-id-type="pmid">123</article-id>'
            b"</article-meta></front><body><p>Child text</p></body>"
            b"</sub-article></article>"
        )
        result = self.call(data, {"pmid": "123"})
        self.assertEqual(result["status"], "needs_review")
        self.assertNotEqual(result["checks"]["identity"], "pass")
        self.assertNotEqual(result["checks"]["full_text"], "pass")
        self.assertEqual(result["observed_identifiers"], {})

    def test_sub_article_body_does_not_satisfy_outer_body(self):
        data = (
            b"<article><front><article-meta>"
            b'<article-id pub-id-type="pmid">123</article-id>'
            b"</article-meta></front><sub-article><body><p>Child</p></body>"
            b"</sub-article></article>"
        )
        result = self.call(data, {"pmid": "123"})
        self.assertEqual(result["status"], "needs_review")
        self.assertIn("jats_body_missing", result["reasons"])

    def test_nested_ids_in_non_direct_positions_ignored(self):
        data = (
            b"<article><front><article-meta><contrib-group>"
            b'<article-id pub-id-type="pmid">123</article-id></contrib-group>'
            b"</article-meta></front><body><p>x</p></body></article>"
        )
        result = self.call(data, {"pmid": "123"})
        self.assertEqual(result["status"], "needs_review")

    def test_duplicate_front_or_body_is_ambiguous(self):
        base = self._ids((b"pmid", b"123"))
        dup_body = base.replace(b"</article>", b"<body><p>y</p></body></article>")
        self.assertNotEqual(self.call(dup_body, {"pmid": "123"})["status"], "ready")
        dup_front = base.replace(
            b"<body>",
            b'<front><article-meta><article-id pub-id-type="pmid">123</article-id>'
            b"</article-meta></front><body>",
        )
        self.assertNotEqual(self.call(dup_front, {"pmid": "123"})["status"], "ready")


class TestJatsBodyTailAndEmptiness(_Base):
    HEAD = (
        b"<article><front><article-meta>"
        b'<article-id pub-id-type="pmid">123</article-id>'
        b"</article-meta></front>"
    )

    def _check(self, tail_xml):
        return self.call(self.HEAD + tail_xml + b"</article>", {"pmid": "123"})

    def test_selfclosing_body_with_external_tail_not_ready(self):
        result = self._check(b"<body/>Not article body")
        self.assertEqual(result["status"], "needs_review")
        self.assertEqual(result["checks"]["full_text"], "fail")
        self.assertIn("jats_body_empty", result["reasons"])

    def test_empty_body_with_external_tail_not_ready(self):
        result = self._check(b"<body></body>Not article body")
        self.assertEqual(result["status"], "needs_review")
        self.assertEqual(result["checks"]["full_text"], "fail")

    def test_nested_empty_structures_not_ready(self):
        result = self._check(b"<body><sec><title/><p/><sec/></sec></body> tail")
        self.assertEqual(result["status"], "needs_review")
        self.assertEqual(result["checks"]["full_text"], "fail")

    def test_invisible_only_body_not_ready(self):
        result = self._check("<body><p>\u200b\u00a0 \ufeff</p></body>".encode())
        self.assertEqual(result["status"], "needs_review")

    def test_child_tail_inside_body_counts(self):
        result = self._check(b"<body><p/>Inside text</body>")
        self.assertEqual(result["status"], "ready")

    def test_body_text_and_nested_child_text_count(self):
        self.assertEqual(self._check(b"<body>Direct</body>")["status"], "ready")
        self.assertEqual(
            self._check(b"<body><sec><p>Deep</p></sec></body>")["status"], "ready"
        )

    def test_article_tail_text_not_body(self):
        result = self._check(b"<body/>")
        self.assertEqual(result["status"], "needs_review")


class TestJatsBodyFailClosed(_Base):
    def test_missing_body_needs_review(self):
        result = self.call(jats(body=None), {"pmid": "12345678"})
        self.assertEqual(result["status"], "needs_review")
        self.assertEqual(result["checks"]["full_text"], "fail")
        self.assertIn("jats_body_missing", result["reasons"])

    def test_empty_body_needs_review(self):
        result = self.call(jats(body=b""), {"pmid": "12345678"})
        self.assertEqual(result["status"], "needs_review")
        self.assertIn("jats_body_empty", result["reasons"])

    def test_whitespace_only_body_needs_review(self):
        result = self.call(jats(body=b"   \n\t  "), {"pmid": "12345678"})
        self.assertEqual(result["status"], "needs_review")
        self.assertIn("jats_body_empty", result["reasons"])

    def test_missing_article_meta_needs_review(self):
        data = (
            b'<?xml version="1.0"?><article><front/><body><p>x</p></body></article>'
        )
        result = self.call(data)
        self.assertEqual(result["status"], "needs_review")
        self.assertIn("jats_structure_incomplete", result["reasons"])


# --------------------------------------------------------------------------- #
# AC2/AC6: malicious XML defences
# --------------------------------------------------------------------------- #
class TestXmlDefences(_Base):
    def test_malformed_xml_rejected(self):
        result = self.call(b"<?xml version='1.0'?>\n<article><front><article-meta>")
        self.assertEqual(result["status"], "rejected")
        self.assertIn("xml_malformed", result["reasons"])
        self.assertEqual(result["checks"]["container"], "fail")

    def test_billion_laughs_rejected(self):
        payload = (
            b'<?xml version="1.0"?>'
            b'<!DOCTYPE lolz [<!ENTITY lol "lol">'
            b'<!ENTITY lol2 "&lol;&lol;&lol;">]>'
            b"<article>&lol2;</article>"
        )
        result = self.call(payload)
        self.assertEqual(result["status"], "rejected")
        self.assertIn("xml_entity_declaration", result["reasons"])

    def test_external_entity_xxe_rejected_without_access(self):
        payload = (
            b'<?xml version="1.0"?>'
            b'<!DOCTYPE foo [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>'
            b"<article>&xxe;</article>"
        )
        result = self.call(payload)
        self.assertEqual(result["status"], "rejected")
        self.assertIn("xml_entity_declaration", result["reasons"])

    def test_internal_subset_without_entity_rejected(self):
        payload = (
            b'<?xml version="1.0"?>'
            b"<!DOCTYPE article [ <!ELEMENT article ANY> ]>"
            b"<article><body><p>x</p></body></article>"
        )
        result = self.call(payload)
        self.assertEqual(result["status"], "rejected")
        self.assertIn("xml_internal_subset", result["reasons"])

    def test_second_doctype_rejected(self):
        payload = (
            b'<?xml version="1.0"?>'
            b"<!DOCTYPE article>"
            b"<!DOCTYPE article>"
            b"<article><body><p>x</p></body></article>"
        )
        result = self.call(payload)
        self.assertEqual(result["status"], "rejected")
        self.assertIn("xml_doctype_rejected", result["reasons"])

    def test_undefined_entity_rejected(self):
        payload = (
            b'<?xml version="1.0"?>'
            b"<article><body><p>a&nbsp;b</p></body></article>"
        )
        result = self.call(payload)
        self.assertEqual(result["status"], "rejected")
        self.assertIn("xml_undefined_entity", result["reasons"])

    def test_non_utf8_xml_rejected(self):
        payload = b'<?xml version="1.0" encoding="UTF-16"?>\xff\xfe<\x00a\x00'
        result = self.call(payload)
        self.assertEqual(result["status"], "rejected")
        self.assertIn("xml_not_utf8", result["reasons"])

    def test_non_jats_wellformed_xml_needs_review(self):
        result = self.call(b'<?xml version="1.0"?><root><a/></root>')
        self.assertEqual(result["media_kind"], "unknown")
        self.assertEqual(result["status"], "needs_review")
        self.assertIn("xml_not_jats_article", result["reasons"])

    def test_malformed_xml_does_not_raise(self):
        for payload in (
            b"<?xml version='1.0'?><article><",
            b"<?xml version='1.0'?><article></wrong>",
            b"<?xml version='1.0'?><article attr=unquoted></article>",
        ):
            result = self.call(payload)
            self.assertEqual(result["status"], "rejected")


# --------------------------------------------------------------------------- #
# Bounds: byte cap, node count, depth, text
# --------------------------------------------------------------------------- #
class TestBounds(_Base):
    def test_empty_input_rejected(self):
        result = self.call(b"")
        self.assertEqual(result["status"], "rejected")
        self.assertIn("empty_input", result["reasons"])

    def test_oversize_rejected_without_truncation(self):
        data = b"%PDF-1.7\n" + b"0" * (MAX_BYTE_SIZE + 1024)
        result = self.call(data)
        self.assertEqual(result["status"], "rejected")
        self.assertIn("byte_cap_exceeded", result["reasons"])
        self.assertEqual(result["byte_size"], len(data))
        self.assertEqual(result["sha256"], hashlib.sha256(data).hexdigest())

    def test_exactly_at_cap_is_not_size_rejected(self):
        data = b"%PDF-1.4\n1 0 obj\n%%EOF\n"
        padded = data + b" " * (MAX_BYTE_SIZE - len(data))
        result = self.call(padded)
        self.assertNotIn("byte_cap_exceeded", result["reasons"])

    def test_node_limit_exceeded_rejected(self):
        body = b"<p>x</p>" * (validation_mod.MAX_XML_NODES + 100)
        data = jats(body=body)
        result = self.call(data)
        self.assertEqual(result["status"], "rejected")
        self.assertIn("xml_node_limit_exceeded", result["reasons"])

    def test_depth_limit_exceeded_rejected(self):
        depth = validation_mod.MAX_XML_DEPTH + 100
        body = b"<sec>" * depth + b"<p>x</p>" + b"</sec>" * depth
        data = jats(body=body)
        result = self.call(data)
        self.assertEqual(result["status"], "rejected")
        self.assertIn("xml_depth_limit_exceeded", result["reasons"])

    def test_text_limit_exceeded_rejected(self):
        body = b"<p>" + b"a" * (validation_mod.MAX_XML_TEXT_CHARS + 1024) + b"</p>"
        data = jats(body=body)
        result = self.call(data)
        self.assertEqual(result["status"], "rejected")
        self.assertIn("xml_text_limit_exceeded", result["reasons"])


# --------------------------------------------------------------------------- #
# Unknown containers
# --------------------------------------------------------------------------- #
class TestUnknown(_Base):
    def test_unrecognized_bytes_needs_review(self):
        result = self.call(b"just some random bytes, not a document at all")
        self.assertEqual(result["media_kind"], "unknown")
        self.assertEqual(result["status"], "needs_review")
        self.assertIn("unrecognized_container", result["reasons"])


# --------------------------------------------------------------------------- #
# Request validation (caller errors raise; content never does)
# --------------------------------------------------------------------------- #
class TestRequestValidation(unittest.TestCase):
    def test_error_hierarchy(self):
        self.assertTrue(issubclass(AcquisitionValidationError, ReviewAgentError))
        self.assertTrue(issubclass(AcquisitionValidationError, ValueError))

    def test_data_must_be_bytes(self):
        for bad in (None, "x", 5, [1, 2]):
            with self.assertRaises(AcquisitionValidationError):
                validate_asset(bad, expected_identifiers={})

    def test_bytes_like_is_accepted(self):
        result = validate_asset(bytearray(GOOD_PDF), expected_identifiers={})
        self.assertEqual(result["media_kind"], "pdf")

    def test_expected_must_be_mapping(self):
        with self.assertRaises(AcquisitionValidationError):
            validate_asset(b"x", expected_identifiers=["pmid"])

    def test_expected_key_must_be_known_namespace(self):
        with self.assertRaises(AcquisitionValidationError):
            validate_asset(b"x", expected_identifiers={"arxiv": "1234.5678"})

    def test_expected_value_must_be_str(self):
        with self.assertRaises(AcquisitionValidationError):
            validate_asset(b"x", expected_identifiers={"pmid": 123})

    def test_expected_shapes_enforced(self):
        for namespace, value in (
            ("pmid", "abc"),
            ("pmcid", "12x"),
            ("doi", "not-a-doi"),
            ("pmid", ""),
            ("doi", "10.1234/"),
        ):
            with self.assertRaises(AcquisitionValidationError):
                validate_asset(b"x", expected_identifiers={namespace: value})

    def test_declared_media_type_must_be_str_or_none(self):
        with self.assertRaises(AcquisitionValidationError):
            validate_asset(b"x", expected_identifiers={}, declared_media_type=5)

    def test_identifier_namespaces_constant(self):
        self.assertEqual(IDENTIFIER_NAMESPACES, ("pmid", "pmcid", "doi"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
