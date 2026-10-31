"""PDF full-text adapter tests (Step 07, S17-P7 / kickoff §7).

Covers ``src/review_article_agent/parsing/pdf_adapter.py`` and
``_pdf_text_child.py`` against ``docs/implementation/07-parsing.md`` AC21
("Failed parsing cannot yield an empty-paper exclusion") and the kickoff §7
requirement: bounded per-page text extraction with page locators, conservative
quality states, and no automatic exclusion.

Three layers:

* **Injected-extractor tests** run in a standard-library-only environment: the
  full-text seam is a deterministic fake, so the whole parent mapping logic is
  exercised without ``pypdf``. None of these is skipped.
* **Child-logic tests** replace ``pypdf`` with a deterministic fake to exercise
  ``_pdf_text_child.extract_pages`` without the dependency. Never skipped.
* ``RealPypdfTests`` uses self-authored minimal PDFs and the real optional
  dependency; it is skipped (with an explicit reason) only when ``pypdf`` is not
  installed. Run it with the ``[pdf]`` extra installed.

No network, provider, or model call; no real licensed source is used.

Run::

    PYTHONPATH=src:tests python3 -m unittest tests.test_parsing_pdf_adapter -v
"""

from __future__ import annotations

import hashlib
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from review_article_agent.parsing import _pdf_text_child, pdf_adapter  # noqa: E402
from review_article_agent.parsing.core import NORMALIZATION_VERSION  # noqa: E402
from review_article_agent.parsing.pdf_adapter import (  # noqa: E402
    PDF_PARSER_VERSION,
    PdfAdapterRequestError,
    parse_pdf,
)
from review_article_agent.acquisition import pdf_inspection  # noqa: E402
from test_acquisition_pdf_inspection import (  # noqa: E402
    BLANK,
    FakePage,
    FakePypdf,
    build_pdf,
    text_stream,
)

REAL_PYPDF = pdf_adapter._pypdf_available()

_LONG = "The quick brown fox jumps over the lazy dog. " * 8  # 360 chars


def extraction(status="ok", *, failure_code=None, page_count=None, pages=None,
               parser_version="fake"):
    return {
        "status": status,
        "failure_code": failure_code,
        "page_count": page_count,
        "pages": pages or [],
        "parser_version": parser_version,
    }


class InjectedExtractorTests(unittest.TestCase):
    """Parent mapping logic with a deterministic extractor; never skipped."""

    PDF = b"%PDF-1.4 synthetic"

    def test_good_text_yields_parsed_pages_and_locators(self):
        good = extraction(page_count=2, pages=[
            {"page": 1, "text": "Page one. " + _LONG},
            {"page": 2, "text": "Page two. " + _LONG},
        ])
        result = parse_pdf(self.PDF, extractor=lambda data: good)
        self.assertEqual(result["format"], "pdf")
        self.assertEqual(result["status"], "parsed")
        self.assertEqual(result["parser_version"], PDF_PARSER_VERSION)
        self.assertGreaterEqual(len(result["passages"]), 2)
        pages = {p["page"] for p in result["passages"]}
        self.assertEqual(pages, {1, 2})
        for passage in result["passages"]:
            self.assertTrue(passage["passage_id"].startswith("p"))
            self.assertEqual(passage["kind"], "paragraph")
            self.assertEqual(passage["section_path"], [])
            self.assertEqual(passage["normalization_version"], NORMALIZATION_VERSION)
            self.assertEqual(
                passage["text_sha256"],
                hashlib.sha256(passage["text"].encode("utf-8")).hexdigest(),
            )
        self.assertIn("pdf_column_order_uncertain", result["reasons"])
        self.assertIn("pdf_non_text_assets_unverified", result["reasons"])
        self.assertNotIn("pdf_full_extraction_unsupported", result["reasons"])
        self.assertTrue(result["non_text_assets_require_review"])
        # Usable text is original text: no per-passage human action, but the
        # recorded parser version is present for reproducible read-back.
        self.assertFalse(result["human_action_required"])
        self.assertEqual(result["pdf_parser_version"], "fake")
        self.assertFalse(result["excludes_candidate"])
        self.assertFalse(result["authorizes_scientific_processing"])
        # diagnostic envelope keeps the page-present shape and never a preview
        self.assertEqual(result["pdf_inspection"]["status"], "inspected")
        self.assertEqual(result["pdf_inspection"]["page_text_present"], [True, True])
        self.assertNotIn("text_preview", result["pdf_inspection"])
        json.dumps(result)  # JSON-safe

    def test_passage_ids_are_stable_and_sequential(self):
        good = extraction(page_count=1, pages=[{"page": 1, "text": _LONG}])
        first = parse_pdf(self.PDF, extractor=lambda data: good)
        second = parse_pdf(self.PDF, extractor=lambda data: good)
        self.assertEqual(
            [p["passage_id"] for p in first["passages"]],
            [p["passage_id"] for p in second["passages"]],
        )
        self.assertEqual(first["passages"][0]["passage_id"], "p0001")

    def test_degraded_extraction_needs_review(self):
        # Each former standalone needs-review test is preserved verbatim as a
        # labelled subTest case.
        def case_test_blank_pages_need_review():
            blank = extraction(page_count=2, pages=[
                {"page": 1, "text": ""}, {"page": 2, "text": "   "},
            ])
            result = parse_pdf(self.PDF, extractor=lambda data: blank)
            self.assertEqual(result["status"], "needs_review")
            self.assertEqual(result["passages"], [])
            self.assertIn("pdf_scan_or_blank_page", result["reasons"])
            self.assertTrue(result["human_action_required"])
            self.assertFalse(result["excludes_candidate"])

        def case_test_very_short_text_needs_review():
            short = extraction(page_count=1, pages=[{"page": 1, "text": "tiny"}])
            result = parse_pdf(self.PDF, extractor=lambda data: short)
            self.assertEqual(result["status"], "needs_review")
            self.assertIn("pdf_text_too_short", result["reasons"])
            self.assertFalse(result["excludes_candidate"])

        def case_test_garbled_text_needs_review():
            garbled = extraction(page_count=1, pages=[{"page": 1, "text": "\ufffd" * 400}])
            result = parse_pdf(self.PDF, extractor=lambda data: garbled)
            self.assertEqual(result["status"], "needs_review")
            self.assertIn("pdf_text_garbled", result["reasons"])

        def case_test_limited_extraction_needs_review_but_keeps_text():
            limited = extraction("limited", failure_code="page_limit", page_count=100,
                                 pages=[{"page": 1, "text": _LONG}])
            result = parse_pdf(self.PDF, extractor=lambda data: limited)
            self.assertEqual(result["status"], "needs_review")
            self.assertIn("pdf_limited", result["reasons"])
            self.assertIn("pdf_column_order_uncertain", result["reasons"])
            self.assertGreaterEqual(len(result["passages"]), 1)

        def case_test_unavailable_maps_to_human_action():
            unavailable = extraction("unavailable", failure_code="dependency_missing")
            result = parse_pdf(self.PDF, extractor=lambda data: unavailable)
            self.assertEqual(result["status"], "needs_review")
            self.assertEqual(result["passages"], [])
            self.assertIn("ocr_or_parser_unavailable", result["reasons"])
            self.assertIn("pdf_full_extraction_unsupported", result["reasons"])
            self.assertTrue(result["human_action_required"])
            self.assertFalse(result["excludes_candidate"])

        def case_test_failed_and_encrypted_map_to_review():
            malformed = extraction("failed", failure_code="malformed_pdf")
            result = parse_pdf(self.PDF, extractor=lambda data: malformed)
            self.assertEqual(result["status"], "needs_review")
            self.assertIn("pdf_malformed", result["reasons"])

            encrypted = extraction("failed", failure_code="encrypted")
            result = parse_pdf(self.PDF, extractor=lambda data: encrypted)
            self.assertIn("pdf_encrypted", result["reasons"])
            self.assertFalse(result["excludes_candidate"])

        for label, case in (
            ("test_blank_pages_need_review", case_test_blank_pages_need_review),
            ("test_very_short_text_needs_review", case_test_very_short_text_needs_review),
            ("test_garbled_text_needs_review", case_test_garbled_text_needs_review),
            ("test_limited_extraction_needs_review_but_keeps_text", case_test_limited_extraction_needs_review_but_keeps_text),
            ("test_unavailable_maps_to_human_action", case_test_unavailable_maps_to_human_action),
            ("test_failed_and_encrypted_map_to_review", case_test_failed_and_encrypted_map_to_review),
        ):
            with self.subTest(case=label):
                case()

    def test_extractor_exception_never_crashes(self):
        def boom(data):  # pragma: no cover - must be caught
            raise RuntimeError("hostile")

        result = parse_pdf(self.PDF, extractor=boom)
        self.assertEqual(result["status"], "needs_review")
        self.assertIn("pdf_malformed", result["reasons"])
        self.assertFalse(result["excludes_candidate"])

    def test_non_mapping_extractor_result_fails_closed(self):
        result = parse_pdf(self.PDF, extractor=lambda data: ["nope"])
        self.assertEqual(result["status"], "needs_review")
        self.assertIn("pdf_malformed", result["reasons"])

    def test_empty_input_does_not_invoke_extractor(self):
        def boom(data):  # pragma: no cover - must never be called
            raise AssertionError("extractor must not run on empty input")

        result = parse_pdf(b"", extractor=boom)
        self.assertIn("pdf_empty_input", result["reasons"])
        self.assertIn("pdf_full_extraction_unsupported", result["reasons"])
        self.assertEqual(result["passages"], [])
        self.assertFalse(result["excludes_candidate"])

    def test_request_error_on_non_bytes(self):
        with self.assertRaises(PdfAdapterRequestError):
            parse_pdf("not-bytes")  # type: ignore[arg-type]

    def test_default_falls_back_to_diagnostics_without_pypdf(self):
        # No extractor and no inspector: with pypdf absent the diagnostics path
        # runs and reports the dependency missing (never an exception).
        with mock.patch.object(pdf_adapter, "_pypdf_available", return_value=False), \
                mock.patch.object(pdf_inspection, "_pypdf_available", return_value=False):
            result = parse_pdf(self.PDF)
        self.assertEqual(result["status"], "needs_review")
        self.assertIn("ocr_or_parser_unavailable", result["reasons"])
        self.assertEqual(result["passages"], [])
        self.assertFalse(result["excludes_candidate"])


class ChildLogicTests(unittest.TestCase):
    """``_pdf_text_child.extract_pages`` with a fake pypdf; never skipped."""

    def run_child(self, fake, *, max_pages=500, max_text_chars=1_000_000):
        return _pdf_text_child.extract_pages(
            b"%PDF-fake", max_pages, max_text_chars, pypdf=fake
        )

    def test_child_extraction_matrix(self):
        # Each former standalone child-logic test is preserved verbatim as a
        # labelled subTest case. (test_corrupt_is_static_failure stays standalone.)
        def case_test_two_pages():
            r = self.run_child(FakePypdf([FakePage("alpha"), FakePage("beta")]))
            self.assertEqual(r["status"], "ok")
            self.assertIsNone(r["failure_code"])
            self.assertEqual(r["page_count"], 2)
            self.assertEqual(r["pages"], [{"page": 1, "text": "alpha"}, {"page": 2, "text": "beta"}])

        def case_test_blank_pages_are_empty_strings():
            r = self.run_child(FakePypdf([FakePage(""), FakePage("  \n ")]))
            self.assertEqual(r["pages"], [{"page": 1, "text": ""}, {"page": 2, "text": "  \n "}])

        def case_test_encrypted():
            r = self.run_child(FakePypdf([FakePage("x")], encrypted=True))
            self.assertEqual((r["status"], r["failure_code"]), ("failed", "encrypted"))

        def case_test_no_pages():
            r = self.run_child(FakePypdf([]))
            self.assertEqual((r["status"], r["failure_code"]), ("failed", "no_pages"))

        def case_test_page_cap_limits():
            fake = FakePypdf([FakePage("a"), FakePage("b"), FakePage("c")])
            r = self.run_child(fake, max_pages=2)
            self.assertEqual((r["status"], r["failure_code"]), ("limited", "page_limit"))
            self.assertEqual([p["page"] for p in r["pages"]], [1, 2])

        def case_test_text_cap_limits():
            r = self.run_child(FakePypdf([FakePage("abcdef")]), max_text_chars=3)
            self.assertEqual((r["status"], r["failure_code"]), ("limited", "text_limit"))
            self.assertEqual(r["pages"][0]["text"], "abc")

        def case_test_per_page_extract_error_is_limited():
            r = self.run_child(FakePypdf([FakePage(RuntimeError("x")), FakePage("ok")]))
            self.assertEqual((r["status"], r["failure_code"]), ("limited", "page_extract_error"))
            self.assertEqual(r["pages"][0]["text"], "")

        def case_test_control_characters_stripped():
            r = self.run_child(FakePypdf([FakePage("a\x00\x07b")]))
            self.assertNotIn("\x00", r["pages"][0]["text"])

        def case_test_missing_dependency_in_child():
            with mock.patch.dict(sys.modules, {"pypdf": None}):
                r = _pdf_text_child.extract_pages(b"%PDF-", 5, 5)
            self.assertEqual((r["status"], r["failure_code"]), ("unavailable", "dependency_missing"))

        for label, case in (
            ("test_two_pages", case_test_two_pages),
            ("test_blank_pages_are_empty_strings", case_test_blank_pages_are_empty_strings),
            ("test_encrypted", case_test_encrypted),
            ("test_no_pages", case_test_no_pages),
            ("test_page_cap_limits", case_test_page_cap_limits),
            ("test_text_cap_limits", case_test_text_cap_limits),
            ("test_per_page_extract_error_is_limited", case_test_per_page_extract_error_is_limited),
            ("test_control_characters_stripped", case_test_control_characters_stripped),
            ("test_missing_dependency_in_child", case_test_missing_dependency_in_child),
        ):
            with self.subTest(case=label):
                case()

    def test_corrupt_is_static_failure(self):
        r = self.run_child(FakePypdf(open_error=ValueError("boom")))
        self.assertEqual((r["status"], r["failure_code"]), ("failed", "malformed_pdf"))
        self.assertNotIn("boom", json.dumps(r))


@unittest.skipUnless(REAL_PYPDF, "optional dependency pypdf not installed ([pdf] extra)")
class RealPypdfTests(unittest.TestCase):
    """End-to-end extraction with the real optional dependency."""

    def test_two_page_text_pdf_parses_with_page_locators(self):
        data = build_pdf([text_stream("Abstract. " + _LONG),
                          text_stream("Results. " + _LONG)])
        result = parse_pdf(data)
        self.assertEqual(result["status"], "parsed")
        self.assertEqual(result["parser_version"], PDF_PARSER_VERSION)
        self.assertGreaterEqual(len(result["passages"]), 2)
        self.assertEqual({p["page"] for p in result["passages"]}, {1, 2})
        self.assertIn("pdf_column_order_uncertain", result["reasons"])
        self.assertTrue(result["non_text_assets_require_review"])
        self.assertFalse(result["human_action_required"])
        self.assertEqual(result["pdf_parser_version"], pdf_adapter.pypdf_installed_version())
        self.assertFalse(result["excludes_candidate"])

    def test_blank_page_pdf_needs_review(self):
        result = parse_pdf(build_pdf([BLANK]))
        self.assertEqual(result["status"], "needs_review")
        self.assertEqual(result["passages"], [])
        self.assertIn("pdf_scan_or_blank_page", result["reasons"])
        self.assertFalse(result["excludes_candidate"])

    def test_malformed_pdf_needs_review_without_exception(self):
        result = parse_pdf(b"%PDF-1.4 this is not really a pdf body")
        self.assertEqual(result["status"], "needs_review")
        self.assertEqual(result["passages"], [])
        self.assertIn("pdf_malformed", result["reasons"])
        self.assertFalse(result["excludes_candidate"])


if __name__ == "__main__":
    unittest.main()
