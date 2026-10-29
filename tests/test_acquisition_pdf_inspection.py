"""PDF input-integrity inspector tests (Step 06, S6-P1).

Covers ``src/review_article_agent/acquisition/pdf_inspection.py`` and
``_pdf_child.py`` against ``docs/implementation/06-acquisition.md`` acceptance
criterion 2: scan, wrong-paper, corruption and incomplete-text conditions stay
explicit review conditions; a successful inspection only means "container
parsed and text observed", never scientific acceptance (Step 07 owns that).

Two layers:

* **Core classes** run in a standard-library-only environment. ``pypdf`` is
  replaced by a deterministic fake for the child logic, and the real
  subprocess machinery (timeout, output bound, crash, garbage) is exercised
  with stdlib stand-in child scripts. None of these is ever skipped.
* ``RealPypdfTests`` uses self-authored minimal PDFs and the real optional
  dependency; it is skipped (with an explicit reason) only when ``pypdf`` is
  not installed. Run it with the ``[pdf]`` extra installed.

No network, provider, or model call; no real licensed source is used.

Run::

    PYTHONPATH=src python3 -m unittest tests.test_acquisition_pdf_inspection -v
"""

from __future__ import annotations

import json
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

from review_article_agent.acquisition import _pdf_child, pdf_inspection

REAL_PYPDF = pdf_inspection._pypdf_available()


# --------------------------------------------------------------------------- fixtures
def build_pdf(page_streams: list[bytes], *, info: dict[str, str] | None = None,
              encrypt: bool = False) -> bytes:
    """Build a minimal valid PDF with one page per content stream."""
    objects: list[bytes] = []

    def add(body: bytes) -> int:
        objects.append(body)
        return len(objects)

    n = len(page_streams)
    add(b"")  # 1: catalog (filled below)
    add(b"")  # 2: pages (filled below)
    font = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    page_ids = []
    for stream in page_streams:
        content = add(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
        page_ids.append(add(
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 %d 0 R >> >> /Contents %d 0 R >>" % (font, content)))
    objects[0] = b"<< /Type /Catalog /Pages 2 0 R >>"
    kids = b" ".join(b"%d 0 R" % p for p in page_ids)
    objects[1] = b"<< /Type /Pages /Kids [" + kids + b"] /Count %d >>" % n
    trailer_extra = b""
    if info is not None:
        entries = b" ".join(b"/%s (%s)" % (k.encode(), v.encode()) for k, v in info.items())
        trailer_extra += b" /Info %d 0 R" % add(b"<< " + entries + b" >>")
    if encrypt:
        enc = add(b"<< /Filter /Standard /V 1 /R 2 /O (" + b"0" * 32 + b") /U (" + b"0" * 32
                  + b") /P -4 >>")
        trailer_extra += b" /Encrypt %d 0 R /ID [<00112233445566778899aabbccddeeff> "\
                         b"<00112233445566778899aabbccddeeff>]" % enc
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += b"trailer\n<< /Size %d /Root 1 0 R%s >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objects) + 1, trailer_extra, xref)
    return bytes(out)


def text_stream(text: str) -> bytes:
    return b"BT /F1 12 Tf 72 720 Td (" + text.encode("latin-1") + b") Tj ET"


BLANK = b""


class FakePage:
    def __init__(self, text: str | Exception) -> None:
        self._text = text

    def extract_text(self) -> str:
        if isinstance(self._text, Exception):
            raise self._text
        return self._text


class FakeReader:
    def __init__(self, pages: list[object], metadata: dict[str, str] | None,
                 encrypted: bool) -> None:
        self.pages = pages
        self.metadata = metadata
        self.is_encrypted = encrypted


class FakePypdf:
    __version__ = "9.9.9-fake"

    def __init__(self, pages: list[object] | None = None, metadata: dict[str, str] | None = None,
                 encrypted: bool = False, open_error: Exception | None = None) -> None:
        self.pages = pages or []
        self.metadata = metadata
        self.encrypted = encrypted
        self.open_error = open_error

    def PdfReader(self, stream: object, strict: bool = True) -> FakeReader:  # noqa: N802
        if self.open_error is not None:
            raise self.open_error
        return FakeReader(self.pages, self.metadata, self.encrypted)


def run_child(fake: FakePypdf, *, max_pages: int = 500, max_text_chars: int = 2_000_000) -> dict:
    return _pdf_child.inspect_bytes(b"%PDF-fake", max_pages, max_text_chars, pypdf=fake)


# ------------------------------------------------------------------ child logic (core)
class ChildLogicTests(unittest.TestCase):
    def test_born_digital_first_page_doi(self) -> None:
        fake = FakePypdf([FakePage("A Study\nDOI: 10.1234/Abc.Def-5.\nAbstract text"),
                          FakePage("Methods text")])
        r = run_child(fake)
        self.assertEqual(r["status"], "inspected")
        self.assertIsNone(r["failure_code"])
        self.assertIs(r["encrypted"], False)
        self.assertEqual(r["page_count"], 2)
        self.assertEqual(r["page_text_present"], [True, True])
        self.assertEqual(r["identifiers"]["doi"], "10.1234/abc.def-5")
        self.assertIsNone(r["identifiers"]["pmid"])
        self.assertEqual(r["parser_version"], "9.9.9-fake")
        self.assertIn("Methods text", r["text_preview"])

    def test_scan_blank_pages_are_explicit(self) -> None:
        r = run_child(FakePypdf([FakePage(""), FakePage("  \n ")]))
        self.assertEqual(r["status"], "inspected")
        self.assertEqual(r["page_text_present"], [False, False])
        self.assertEqual(r["text_preview"], "")

    def test_mixed_blank_and_text(self) -> None:
        r = run_child(FakePypdf([FakePage("body"), FakePage(""), FakePage("more")]))
        self.assertEqual(r["page_text_present"], [True, False, True])

    def test_corrupt_is_static_failure_without_message(self) -> None:
        r = run_child(FakePypdf(open_error=ValueError("SECRET-LEAK /private/path")))
        self.assertEqual((r["status"], r["failure_code"]), ("failed", "malformed_pdf"))
        self.assertNotIn("SECRET", json.dumps(r))

    def test_memory_error_maps_to_resource_limit(self) -> None:
        r = run_child(FakePypdf(open_error=MemoryError()))
        self.assertEqual(r["failure_code"], "resource_limit")

    def test_encrypted_not_decrypted(self) -> None:
        r = run_child(FakePypdf([FakePage("hidden")], encrypted=True))
        self.assertEqual((r["status"], r["failure_code"], r["encrypted"]),
                         ("failed", "encrypted", True))
        self.assertIsNone(r["page_count"])
        self.assertEqual(r["text_preview"], "")

    def test_no_pages(self) -> None:
        r = run_child(FakePypdf([]))
        self.assertEqual((r["status"], r["failure_code"]), ("failed", "no_pages"))

    def test_ambiguous_identifiers_are_null(self) -> None:
        text = "doi 10.1000/aaa111 and cites 10.2000/bbb222 PMID: 111 PMID 222 PMC12345 PMC67890"
        r = run_child(FakePypdf([FakePage(text)]))
        self.assertEqual(r["identifiers"], {"doi": None, "pmid": None, "pmcid": None})

    def test_unambiguous_identifiers_and_metadata_agree(self) -> None:
        fake = FakePypdf([FakePage("https://doi.org/10.1000/ABC123 PMID: 424242 PMC1234567")],
                         metadata={"/Subject": "doi:10.1000/abc123"})
        ids = run_child(fake)["identifiers"]
        self.assertEqual(ids, {"doi": "10.1000/abc123", "pmid": "424242", "pmcid": "PMC1234567"})

    def test_metadata_disagreeing_with_text_is_ambiguous(self) -> None:
        fake = FakePypdf([FakePage("10.1000/abc123")], metadata={"/Subject": "10.9999/other9"})
        self.assertIsNone(run_child(fake)["identifiers"]["doi"])

    def test_wrong_paper_ids_are_reported_not_judged(self) -> None:
        r = run_child(FakePypdf([FakePage("Different paper 10.5555/zzz999")]))
        self.assertEqual(r["identifiers"]["doi"], "10.5555/zzz999")
        self.assertEqual(r["status"], "inspected")  # caller compares; no acceptance claim

    def test_page_cap_is_limited(self) -> None:
        r = run_child(FakePypdf([FakePage("a"), FakePage("b"), FakePage("c")]), max_pages=2)
        self.assertEqual((r["status"], r["failure_code"]), ("limited", "page_limit"))
        self.assertEqual(r["page_count"], 3)
        self.assertEqual(r["page_text_present"], [True, True])

    def test_text_cap_is_limited_and_stops(self) -> None:
        r = run_child(FakePypdf([FakePage("x" * 30), FakePage("y" * 30), FakePage("z")]),
                      max_text_chars=40)
        self.assertEqual((r["status"], r["failure_code"]), ("limited", "text_limit"))
        self.assertEqual(r["page_text_present"], [True, True])
        self.assertLessEqual(len(r["text_preview"]), 8192)

    def test_page_extract_error_is_limited_not_silent(self) -> None:
        r = run_child(FakePypdf([FakePage("ok"), FakePage(RuntimeError("boom")), FakePage("ok2")]))
        self.assertEqual((r["status"], r["failure_code"]), ("limited", "page_extract_error"))
        self.assertEqual(r["page_text_present"], [True, False, True])
        self.assertNotIn("boom", json.dumps(r))

    def test_preview_bounded_and_sanitised(self) -> None:
        r = run_child(FakePypdf([FakePage("w " * 20000 + "\x00\x07 \ud800")]))
        self.assertLessEqual(len(r["text_preview"]), 8192)
        self.assertNotIn("\x00", r["text_preview"])
        json.dumps(r).encode("ascii")

    def test_missing_dependency_in_child(self) -> None:
        with mock.patch.dict(sys.modules, {"pypdf": None}):
            r = _pdf_child.inspect_bytes(b"%PDF-", 5, 5)
        self.assertEqual((r["status"], r["failure_code"]), ("unavailable", "dependency_missing"))


# --------------------------------------------------------- parent behaviour (core)
GOOD_CHILD = {
    "status": "inspected", "encrypted": False, "page_count": 2,
    "page_text_present": [True, False], "text_preview": "hello",
    "identifiers": {"doi": "10.1000/abc", "pmid": None, "pmcid": None},
    "failure_code": None, "parser_version": "1.0",
}


class ParentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        p = mock.patch.object(pdf_inspection, "_pypdf_available", return_value=True)
        p.start()
        self.addCleanup(p.stop)

    def child(self, body: str) -> None:
        path = Path(self.tmp.name) / "fake_child.py"
        path.write_text(textwrap.dedent(body))
        p = mock.patch.object(pdf_inspection, "_CHILD_SCRIPT", path)
        p.start()
        self.addCleanup(p.stop)

    def emit(self, obj: object) -> None:
        self.child(f"import sys, json\nsys.stdin.buffer.read()\n"
                   f"sys.stdout.write({json.dumps(json.dumps(obj))})\n")

    PDF = b"%PDF-1.4 minimal"

    def test_unavailable_fails_closed_without_spawn(self) -> None:
        with mock.patch.object(pdf_inspection, "_pypdf_available", return_value=False), \
                mock.patch.object(pdf_inspection, "_run_child") as run:
            r = pdf_inspection.inspect_pdf(self.PDF)
        run.assert_not_called()
        self.assertEqual((r["status"], r["failure_code"]), ("unavailable", "dependency_missing"))
        self.assertEqual(r["identifiers"], {"doi": None, "pmid": None, "pmcid": None})

    def test_fields_exact(self) -> None:
        r = pdf_inspection.inspect_pdf(b"")
        self.assertEqual(set(r), {"status", "encrypted", "page_count", "page_text_present",
                                  "text_preview", "identifiers", "failure_code", "parser_version"})

    def test_input_checks(self) -> None:
        f = pdf_inspection.inspect_pdf
        self.assertEqual(f(b"")["failure_code"], "empty_input")
        self.assertEqual(f("text")["failure_code"], "invalid_input")  # type: ignore[arg-type]
        self.assertEqual(f(b"not a pdf at all")["failure_code"], "not_pdf")
        big = b"%PDF-" + b"0" * pdf_inspection.MAX_SOURCE_BYTES
        self.assertEqual(f(big)["failure_code"], "input_too_large")
        for kw in ({"timeout_seconds": 0}, {"timeout_seconds": True}, {"max_pages": 0},
                   {"max_pages": 10**9}, {"max_text_chars": 0}, {"max_pages": 1.5}):
            self.assertEqual(f(self.PDF, **kw)["failure_code"], "invalid_parameters", kw)

    def test_valid_child_reply_roundtrip(self) -> None:
        self.emit(GOOD_CHILD)
        r = pdf_inspection.inspect_pdf(self.PDF)
        self.assertEqual(r, GOOD_CHILD)

    def test_oversize_preview_truncated_and_controls_stripped(self) -> None:
        self.emit({**GOOD_CHILD, "text_preview": "a\x00" * 20000})
        r = pdf_inspection.inspect_pdf(self.PDF)
        self.assertLessEqual(len(r["text_preview"]), 8192)
        self.assertNotIn("\x00", r["text_preview"])

    def test_timeout_kills_hostile_child(self) -> None:
        self.child("import time\ntime.sleep(60)\n")
        r = pdf_inspection.inspect_pdf(self.PDF, timeout_seconds=0.5)
        self.assertEqual((r["status"], r["failure_code"]), ("failed", "timeout"))

    def test_output_flood_is_bounded(self) -> None:
        self.child("import sys\nsys.stdin.buffer.read()\n"
                   "while True:\n    sys.stdout.write('x' * 65536)\n    sys.stdout.flush()\n")
        r = pdf_inspection.inspect_pdf(self.PDF, timeout_seconds=10)
        self.assertEqual((r["status"], r["failure_code"]), ("failed", "child_output_invalid"))

    def test_crash_and_garbage(self) -> None:
        self.child("import sys\nsys.exit(3)\n")
        self.assertEqual(pdf_inspection.inspect_pdf(self.PDF)["failure_code"], "child_crashed")
        self.child("print('not json')\n")
        self.assertEqual(pdf_inspection.inspect_pdf(self.PDF)["failure_code"], "child_output_invalid")

    def test_hostile_child_replies_rejected(self) -> None:
        bad = [
            {**GOOD_CHILD, "status": "great"},
            {**GOOD_CHILD, "failure_code": "free text from pdf"},
            {**GOOD_CHILD, "status": "failed", "failure_code": None},
            {**GOOD_CHILD, "page_text_present": [True]},  # inspected must cover all pages
            {**GOOD_CHILD, "page_text_present": ["yes", "no"]},
            {**GOOD_CHILD, "identifiers": {"doi": "not a doi", "pmid": None, "pmcid": None}},
            {**GOOD_CHILD, "identifiers": {"doi": None, "pmid": "12x", "pmcid": None}},
            {**GOOD_CHILD, "page_count": True},
            {**GOOD_CHILD, "encrypted": "no"},
            [1, 2],
        ]
        for obj in bad:
            self.emit(obj)
            r = pdf_inspection.inspect_pdf(self.PDF)
            self.assertEqual(r["failure_code"], "child_output_invalid", obj)

    def emit_raw(self, text: str) -> None:
        self.child(f"import sys\nsys.stdin.buffer.read()\nsys.stdout.write({text!r})\n")

    def assert_invalid_via_public_path(self, label: object) -> None:
        r = pdf_inspection.inspect_pdf(self.PDF)  # must not raise
        self.assertEqual((r["status"], r["failure_code"]), ("failed", "child_output_invalid"), label)
        self.assertEqual(r["page_text_present"], [])
        self.assertEqual(r["text_preview"], "")

    FAILED = {**GOOD_CHILD, "status": "failed", "failure_code": "malformed_pdf",
              "encrypted": None, "page_count": None, "page_text_present": [], "text_preview": "",
              "identifiers": {"doi": None, "pmid": None, "pmcid": None}}

    def test_hostile_scalar_types_never_raise(self) -> None:
        hostile_values = [[], {}, [1], {"a": 1}, None, True, False, 1.5, 7, "x", ""]
        for field in GOOD_CHILD:
            for value in hostile_values:
                for base in (GOOD_CHILD, self.FAILED):
                    obj = {**base, field: value}
                    if obj == base:
                        continue
                    with self.subTest(field=field, value=value, base=base["status"]):
                        # Direct boundary must return None or a valid dict, never raise.
                        out = pdf_inspection._validate_child(json.dumps(obj).encode())
                        self.assertTrue(out is None or out["status"] in pdf_inspection.STATUSES)
                        if out is None:
                            self.emit(obj)
                            self.assert_invalid_via_public_path(obj)

    def test_unhashable_status_and_code_regression(self) -> None:
        for obj in ({"status": "failed", "failure_code": []},
                    {**self.FAILED, "failure_code": []},
                    {**self.FAILED, "failure_code": {}},
                    {**self.FAILED, "status": []},
                    {**self.FAILED, "status": {}}):
            self.assertIsNone(pdf_inspection._validate_child(json.dumps(obj).encode()))
            self.emit(obj)
            self.assert_invalid_via_public_path(obj)

    def test_duplicate_keys_nonfinite_and_structure(self) -> None:
        dup = json.dumps(GOOD_CHILD).replace(
            '"status": "inspected"', '"status": "failed", "status": "inspected"')
        raws = [
            dup,
            json.dumps(GOOD_CHILD).replace('"page_count": 2', '"page_count": NaN'),
            json.dumps(GOOD_CHILD).replace('"page_count": 2', '"page_count": Infinity'),
            json.dumps(GOOD_CHILD).replace('"page_count": 2', '"page_count": 2.0'),
            json.dumps({**GOOD_CHILD, "extra": 1}),
            json.dumps({k: v for k, v in GOOD_CHILD.items() if k != "encrypted"}),
            json.dumps({**GOOD_CHILD, "identifiers": {"doi": None, "pmid": None}}),
            json.dumps({**GOOD_CHILD, "identifiers": {"doi": None, "pmid": None,
                                                       "pmcid": None, "x": None}}),
            '{"status":"inspected"',
            "null", "[]", "1", '"s"', "",
            "\xff",
        ]
        for raw in raws:
            with self.subTest(raw=raw[:60]):
                self.emit_raw(raw)
                self.assert_invalid_via_public_path(raw)

    def test_status_code_encryption_consistency(self) -> None:
        bad = [
            {**GOOD_CHILD, "encrypted": True},                          # inspected + encrypted
            {**GOOD_CHILD, "encrypted": None},
            {**GOOD_CHILD, "page_count": None},
            {**GOOD_CHILD, "page_count": 0, "page_text_present": []},
            {**GOOD_CHILD, "status": "limited", "failure_code": "encrypted"},
            {**GOOD_CHILD, "status": "limited", "failure_code": None},
            {**GOOD_CHILD, "status": "limited", "failure_code": "timeout"},
            {**GOOD_CHILD, "status": "limited", "failure_code": "page_limit",
             "page_text_present": []},
            {**GOOD_CHILD, "failure_code": "page_limit"},               # inspected with code
            {**self.FAILED, "failure_code": "encrypted", "encrypted": False},
            {**self.FAILED, "failure_code": "malformed_pdf", "encrypted": True},
            {**self.FAILED, "failure_code": "page_limit"},               # limited code on failed
            {**self.FAILED, "failure_code": None},
            {**self.FAILED, "text_preview": "leaked text"},
            {**self.FAILED, "page_text_present": [True]},
            {**self.FAILED, "identifiers": {"doi": "10.1000/abc", "pmid": None, "pmcid": None}},
            {**self.FAILED, "status": "unavailable", "failure_code": "dependency_missing",
             "page_count": 3},
            {**self.FAILED, "status": "unavailable", "failure_code": "malformed_pdf"},
        ]
        for obj in bad:
            with self.subTest(obj=obj):
                self.emit(obj)
                self.assert_invalid_via_public_path(obj)

    def test_valid_limited_and_unavailable_pass(self) -> None:
        limited = {**GOOD_CHILD, "status": "limited", "failure_code": "page_limit",
                   "page_count": 5}
        self.emit(limited)
        self.assertEqual(pdf_inspection.inspect_pdf(self.PDF), limited)
        unavailable = {**self.FAILED, "status": "unavailable", "encrypted": None,
                       "failure_code": "dependency_missing"}
        self.emit(unavailable)
        self.assertEqual(pdf_inspection.inspect_pdf(self.PDF), unavailable)

    def test_child_status_failed_with_static_code_passes_through(self) -> None:
        self.emit({**GOOD_CHILD, "status": "failed", "failure_code": "encrypted",
                   "encrypted": True, "page_count": None, "page_text_present": [],
                   "text_preview": "",
                   "identifiers": {"doi": None, "pmid": None, "pmcid": None}})
        r = pdf_inspection.inspect_pdf(self.PDF)
        self.assertEqual((r["status"], r["failure_code"], r["encrypted"]),
                         ("failed", "encrypted", True))

    def test_spawn_failure(self) -> None:
        with mock.patch.object(pdf_inspection.subprocess, "Popen", side_effect=OSError("x")):
            self.assertEqual(pdf_inspection.inspect_pdf(self.PDF)["failure_code"], "spawn_failed")

    def test_child_gets_isolated_argv_and_no_shell(self) -> None:
        self.emit(GOOD_CHILD)
        real = pdf_inspection.subprocess.Popen
        with mock.patch.object(pdf_inspection.subprocess, "Popen", wraps=real) as popen:
            pdf_inspection.inspect_pdf(self.PDF, timeout_seconds=7, max_pages=3, max_text_chars=99)
        args, kwargs = popen.call_args
        self.assertIsInstance(args[0], list)
        self.assertIn("-I", args[0])
        self.assertEqual(args[0][-3:], ["3", "99", "9"])
        self.assertFalse(kwargs.get("shell", False))
        self.assertTrue(kwargs["start_new_session"])
        self.assertEqual(kwargs["env"], {"PYTHONIOENCODING": "utf-8"})


# ---------------------------------------------------------------- real pypdf (optional)
@unittest.skipUnless(REAL_PYPDF, "optional dependency pypdf not installed ([pdf] extra)")
class RealPypdfTests(unittest.TestCase):
    def test_born_digital_with_doi(self) -> None:
        data = build_pdf([text_stream("Title DOI 10.1234/real.paper.1"), text_stream("Body")],
                         info={"Title": "T"})
        r = pdf_inspection.inspect_pdf(data)
        self.assertEqual((r["status"], r["failure_code"]), ("inspected", None))
        self.assertEqual(r["page_count"], 2)
        self.assertEqual(r["page_text_present"], [True, True])
        self.assertEqual(r["identifiers"]["doi"], "10.1234/real.paper.1")
        self.assertIs(r["encrypted"], False)
        self.assertIsInstance(r["parser_version"], str)

    def test_scan_blank_page(self) -> None:
        r = pdf_inspection.inspect_pdf(build_pdf([BLANK]))
        self.assertEqual(r["status"], "inspected")
        self.assertEqual(r["page_text_present"], [False])
        self.assertEqual(r["text_preview"], "")

    def test_mixed(self) -> None:
        r = pdf_inspection.inspect_pdf(build_pdf([text_stream("one"), BLANK, text_stream("three")]))
        self.assertEqual(r["page_text_present"], [True, False, True])

    def test_metadata_doi_and_conflicting_text(self) -> None:
        agree = build_pdf([text_stream("10.1111/same.1")], info={"Subject": "doi:10.1111/same.1"})
        self.assertEqual(pdf_inspection.inspect_pdf(agree)["identifiers"]["doi"], "10.1111/same.1")
        clash = build_pdf([text_stream("10.1111/same.1")], info={"Subject": "10.2222/other.2"})
        self.assertIsNone(pdf_inspection.inspect_pdf(clash)["identifiers"]["doi"])

    def test_corrupt(self) -> None:
        good = build_pdf([text_stream("x")])
        for bad in (good[: len(good) // 2], b"%PDF-1.4\ngarbage\x00\xff" * 5):
            r = pdf_inspection.inspect_pdf(bad)
            self.assertEqual(r["status"], "failed", r)
            self.assertIn(r["failure_code"], {"malformed_pdf", "no_pages"})

    def test_encrypted(self) -> None:
        r = pdf_inspection.inspect_pdf(build_pdf([text_stream("secret")], encrypt=True))
        self.assertEqual((r["status"], r["failure_code"], r["encrypted"]),
                         ("failed", "encrypted", True))
        self.assertNotIn("secret", r["text_preview"])

    def test_caps(self) -> None:
        data = build_pdf([text_stream("aaaa")] * 3)
        r = pdf_inspection.inspect_pdf(data, max_pages=2)
        self.assertEqual((r["status"], r["failure_code"], r["page_count"]), ("limited", "page_limit", 3))
        self.assertEqual(len(r["page_text_present"]), 2)
        r = pdf_inspection.inspect_pdf(data, max_text_chars=5)
        self.assertEqual((r["status"], r["failure_code"]), ("limited", "text_limit"))


if __name__ == "__main__":
    unittest.main()
