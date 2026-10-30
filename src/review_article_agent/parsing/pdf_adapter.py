"""PDF adapter: bounded full-text extraction with page locators (Step 07, S17-P7).

Authority: ``docs/implementation/07-parsing.md`` (structured ingestion with
explicit parser/normalization versions and quality states; AC21 "Failed parsing
cannot yield an empty-paper exclusion. Required unavailable evidence produces a
human action"), ``docs/implementation/kickoffs/16-mvp-hardening.md`` §7, and the
scientific invariants in ``docs/specification.md`` (documents are untrusted; no
automatic exclusion).

Behaviour:

* When the optional ``pypdf`` extra is importable the PDF is parsed by a
  **bounded, isolated child process** (``python -I``, no shell, empty
  environment, new session, POSIX CPU/address-space/file-size limits, wall-clock
  timeout, page/byte/text caps) that returns per-page text. The parent never
  imports ``pypdf`` and never touches PDF content in-process.
* Extracted pages are split into paragraph passages (stable ``p0001`` ids,
  1-based ``page`` locators) using the shared conservative normalization
  (Unicode NFC, ligature folding, soft-hyphen removal, whitespace collapsing).
* Quality states are conservative. Usable text yields the XML success status
  ``parsed``; empty/blank, very short, garbled, capped (``limited``), malformed,
  encrypted, or ``pypdf``-unavailable inputs stay ``needs_review`` with
  ``human_action_required`` true. ``pdf_column_order_uncertain`` is always
  reported for text: PDF text is never reliable column-ordered full text, and
  figures/tables/supplements are not interpreted here, so
  ``non_text_assets_require_review`` stays true.
* ``excludes_candidate`` is always false: a failed PDF can never become a
  silent empty-paper exclusion. ``authorizes_scientific_processing`` is false.

A caller may inject ``extractor`` (a ``bytes -> extraction mapping`` seam) for
deterministic tests, or ``inspector`` (the Step 06 integrity diagnostics seam).
When neither is given and ``pypdf`` is absent the diagnostic path runs and
reports ``ocr_or_parser_unavailable`` / ``pdf_full_extraction_unsupported``.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
from pathlib import Path

from ..domain.errors import ReviewAgentError
from .core import (
    DOCUMENT_TYPE,
    LIMITATIONS,
    NORMALIZATION_VERSION,
    SCHEMA_VERSION,
    normalize_text,
)

PDF_PARSER_VERSION = "step07-pdf-manual/2"

#: Bounds for the isolated full-text child (mirrors the Step 06 inspector).
MAX_TEXT_PAGES = 500
MAX_TEXT_CHARS = 1_000_000
MAX_TEXT_OUTPUT_BYTES = 8 * 1024 * 1024
TEXT_TIMEOUT_SECONDS = 30
MAX_PAGES_CEILING = 10_000
#: Below this many normalized characters the text is "very short" -> review.
MIN_TEXT_CHARS = 200
#: A passage block is split further past this many characters.
MAX_PASSAGE_CHARS = 4_000

#: Extraction child statuses and static failure codes (never messages).
TEXT_STATUSES = ("ok", "limited", "unavailable", "failed")
TEXT_LIMITED_CODES = frozenset({"page_limit", "text_limit", "page_extract_error"})
TEXT_UNAVAILABLE_CODES = frozenset({"dependency_missing", "platform_unsupported"})
TEXT_FAILED_CODES = frozenset(
    {
        "invalid_parameters",
        "invalid_input",
        "empty_input",
        "not_pdf",
        "malformed_pdf",
        "encrypted",
        "no_pages",
        "timeout",
        "resource_limit",
        "child_crashed",
        "child_output_invalid",
        "internal_error",
        "spawn_failed",
    }
)
_TEXT_FIELDS = frozenset({"status", "failure_code", "page_count", "pages", "parser_version"})
_TEXT_ALL_CODES = TEXT_LIMITED_CODES | TEXT_UNAVAILABLE_CODES | TEXT_FAILED_CODES

#: Sanitized diagnostic fields kept from the Step 06 inspector.  ``text_preview``
#: is deliberately excluded: it is private source text, never full text.
_INSPECTION_FIELDS = (
    "status",
    "encrypted",
    "page_count",
    "page_text_present",
    "identifiers",
    "failure_code",
    "parser_version",
)

_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
_BLANK_LINE_RE = re.compile(r"\n\s*\n")
#: Characters that are plausible in extracted prose (letters/digits and common
#: punctuation).  Used only to flag likely-garbled text for human review.
_GARBLED_OK = set(".,;:!?()[]{}'\"-/%&@#+*=<>_|~^$`\\")


class PdfAdapterRequestError(ReviewAgentError, ValueError):
    """The PDF adapter request was malformed (bad argument types)."""


def _pypdf_available() -> bool:
    """Seam: whether the optional parser can be imported (tests patch this)."""
    try:
        return importlib.util.find_spec("pypdf") is not None
    except (ImportError, ValueError):
        return False


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _dedupe(reasons) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for reason in reasons:
        if reason not in seen:
            seen.add(reason)
            ordered.append(reason)
    return ordered


def _clean_text(text: str) -> str:
    text = text.encode("utf-8", "replace").decode("utf-8")
    return _CONTROL_RE.sub(" ", text)


# --------------------------------------------------------------------------- #
# Strict validation of the isolated child's reply
# --------------------------------------------------------------------------- #
def _no_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    obj: dict[str, object] = {}
    for key, value in pairs:
        if key in obj:
            raise ValueError("duplicate key")
        obj[key] = value
    return obj


def _reject_constant(_name: str) -> object:
    raise ValueError("non-finite number")


def _validate_text_child(raw: bytes) -> dict | None:
    """Strictly validate the child's JSON; ``None`` means unusable. Never raises."""
    try:
        return _validate_text_child_unsafe(raw)
    except Exception:  # noqa: BLE001 - defence in depth; hostile input only
        return None


def _validate_text_child_unsafe(raw: bytes) -> dict | None:
    obj = json.loads(
        raw.decode("utf-8"),
        object_pairs_hook=_no_duplicates,
        parse_constant=_reject_constant,
    )
    if not isinstance(obj, dict) or set(obj) != _TEXT_FIELDS:
        return None
    status = obj["status"]
    if not isinstance(status, str) or status not in TEXT_STATUSES:
        return None
    code = obj["failure_code"]
    if code is not None and (not isinstance(code, str) or code not in _TEXT_ALL_CODES):
        return None
    page_count = obj["page_count"]
    if page_count is not None and (
        not _is_int(page_count) or not 0 <= page_count <= 10_000_000
    ):
        return None
    pages = obj["pages"]
    if not isinstance(pages, list) or len(pages) > MAX_PAGES_CEILING:
        return None
    clean_pages: list[dict] = []
    for entry in pages:
        if not isinstance(entry, dict) or set(entry) != {"page", "text"}:
            return None
        page_no = entry["page"]
        text = entry["text"]
        if not _is_int(page_no) or not 1 <= page_no <= 10_000_000:
            return None
        if not isinstance(text, str):
            return None
        clean_pages.append({"page": page_no, "text": _clean_text(text)})
    version = obj["parser_version"]
    if version is not None and (not isinstance(version, str) or len(version) > 64):
        return None

    if status in ("ok", "limited"):
        if page_count is None or page_count < 1:
            return None
        if not 1 <= len(clean_pages) <= page_count:
            return None
        if status == "ok" and code is not None:
            return None
        if status == "limited" and code not in TEXT_LIMITED_CODES:
            return None
    else:
        if clean_pages:
            return None
        if status == "unavailable" and (code not in TEXT_UNAVAILABLE_CODES or page_count is not None):
            return None
        if status == "failed" and code not in TEXT_FAILED_CODES:
            return None
    return {
        "status": status,
        "failure_code": code,
        "page_count": page_count,
        "pages": clean_pages,
        "parser_version": version,
    }


# --------------------------------------------------------------------------- #
# Default extractor: bounded, isolated child process
# --------------------------------------------------------------------------- #
def _extraction(
    status: str, failure_code: str | None = None
) -> dict:
    return {
        "status": status,
        "failure_code": failure_code,
        "page_count": None,
        "pages": [],
        "parser_version": None,
    }


def _extract_pdf_text(
    data: bytes,
    *,
    timeout_seconds: float = TEXT_TIMEOUT_SECONDS,
    max_pages: int = MAX_TEXT_PAGES,
    max_text_chars: int = MAX_TEXT_CHARS,
) -> dict:
    """Run the isolated full-text child; map its bounded outcome to a mapping.

    Never raises: every abnormal end maps to a static ``failed`` code so hostile
    input cannot crash parsing.
    """
    from ..acquisition.pdf_inspection import _run_child

    script = Path(__file__).with_name("_pdf_text_child.py")
    outcome, raw = _run_child(
        data,
        max_pages,
        max_text_chars,
        float(timeout_seconds),
        script=script,
        max_output_bytes=MAX_TEXT_OUTPUT_BYTES,
    )
    if outcome == "timeout":
        return _extraction("failed", "timeout")
    if outcome == "output_limit":
        return _extraction("failed", "child_output_invalid")
    if outcome == "spawn_failed":
        return _extraction("failed", "spawn_failed")
    if outcome != "ok":
        return _extraction("failed", "child_crashed")
    validated = _validate_text_child(raw)
    if validated is None:
        return _extraction("failed", "child_output_invalid")
    return validated


def _run_extractor(extractor, data: bytes) -> dict:
    try:
        result = extractor(data)
    except Exception:  # noqa: BLE001 - extraction must never crash parsing
        return _extraction("failed", "internal_error")
    if not isinstance(result, dict):
        return _extraction("failed", "internal_error")
    return result


# --------------------------------------------------------------------------- #
# Passages
# --------------------------------------------------------------------------- #
def _split_blocks(text: str) -> list[str]:
    """Split one page's text into paragraph blocks, bounded in length."""
    blocks: list[str] = []
    for raw_block in _BLANK_LINE_RE.split(text):
        block = raw_block.strip()
        if not block:
            continue
        while len(block) > MAX_PASSAGE_CHARS:
            cut = block.rfind(" ", 0, MAX_PASSAGE_CHARS)
            if cut <= 0:
                cut = MAX_PASSAGE_CHARS
            piece = block[:cut].strip()
            if piece:
                blocks.append(piece)
            block = block[cut:].strip()
        if block:
            blocks.append(block)
    return blocks


def _pdf_passages(pages: list[tuple[object, str]]) -> list[dict]:
    """Build normalized paragraph passages with 1-based page locators."""
    passages: list[dict] = []
    for page_no, text in pages:
        for block in _split_blocks(text):
            normalized, transformations = normalize_text(block)
            if not normalized:
                continue
            passages.append(
                {
                    "passage_id": f"p{len(passages) + 1:04d}",
                    "kind": "paragraph",
                    "page": page_no if _is_int(page_no) else None,
                    "section_path": [],
                    "raw_text": block,
                    "text": normalized,
                    "text_sha256": hashlib.sha256(normalized.encode("utf-8")).hexdigest(),
                    "normalization_version": NORMALIZATION_VERSION,
                    "transformations": transformations,
                }
            )
    return passages


def _looks_garbled(text: str) -> bool:
    """A conservative heuristic: mostly non-prose characters -> likely garbled."""
    sample = text[:2000]
    if not sample:
        return False
    good = sum(
        1 for ch in sample if ch.isalnum() or ch.isspace() or ch in _GARBLED_OK
    )
    return good / len(sample) < 0.5


# --------------------------------------------------------------------------- #
# Result assembly
# --------------------------------------------------------------------------- #
def _sanitize_inspection(inspection: dict | None) -> dict | None:
    if not isinstance(inspection, dict):
        return None
    return {name: inspection.get(name) for name in _INSPECTION_FIELDS}


def _inspection_reasons(inspection: dict) -> list[str]:
    status = inspection.get("status")
    reasons: list[str] = []
    if status == "unavailable":
        reasons.append("ocr_or_parser_unavailable")
    elif status == "failed":
        reasons.append("pdf_malformed")
    elif status == "limited":
        reasons.append("pdf_limited")

    present = inspection.get("page_text_present")
    if isinstance(present, list) and present and not all(present):
        reasons.append("pdf_scan_or_blank_page")
    if status == "inspected":
        # A preview is never reliable column-ordered full text.
        reasons.append("pdf_column_order_uncertain")
    return reasons


def _inspection_from_extraction(extraction: dict) -> dict | None:
    mapped = {
        "ok": "inspected",
        "limited": "limited",
        "unavailable": "unavailable",
        "failed": "failed",
    }.get(extraction.get("status"))
    if mapped is None:
        return None
    pages = extraction.get("pages")
    present = []
    if isinstance(pages, list):
        for entry in pages:
            if isinstance(entry, dict) and isinstance(entry.get("text"), str):
                present.append(bool(entry["text"].strip()))
    return {
        "status": mapped,
        "encrypted": extraction.get("encrypted"),
        "page_count": extraction.get("page_count"),
        "page_text_present": present,
        "identifiers": None,
        "failure_code": extraction.get("failure_code"),
        "parser_version": extraction.get("parser_version"),
    }


def _extraction_reasons(extraction: dict) -> list[str]:
    status = extraction.get("status")
    if status == "unavailable":
        return ["ocr_or_parser_unavailable", "pdf_full_extraction_unsupported"]
    if status == "failed":
        if extraction.get("failure_code") == "encrypted":
            return ["pdf_encrypted"]
        return ["pdf_malformed"]
    if status == "limited":
        return ["pdf_limited"]
    return []


def _assemble(
    digest: str,
    byte_size: int,
    *,
    status: str,
    passages: list[dict],
    reasons: list[str],
    inspection: dict | None,
) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "document_type": DOCUMENT_TYPE,
        "parser_version": PDF_PARSER_VERSION,
        "normalization_version": NORMALIZATION_VERSION,
        "format": "pdf",
        "status": status,
        "source": {"sha256": digest, "byte_size": byte_size},
        "passages": passages,
        "references": [],
        "citation_links": [],
        "citations": {"resolved": 0, "unresolved": 0, "ambiguous": 0, "malformed": 0},
        "assets": [],
        "non_text_assets_require_review": True,
        "human_action_required": True,
        "excludes_candidate": False,
        "reasons": _dedupe(reasons),
        "pdf_inspection": _sanitize_inspection(inspection),
        "authorizes_scientific_processing": False,
        "limitations": LIMITATIONS,
    }


def _from_extraction(digest: str, byte_size: int, extraction: dict) -> dict:
    if extraction.get("status") not in TEXT_STATUSES:
        extraction = _extraction("failed", "internal_error")
    status = extraction["status"]
    inspection = _inspection_from_extraction(extraction)
    reasons = _extraction_reasons(extraction)

    if status in ("unavailable", "failed"):
        return _assemble(
            digest, byte_size, status="needs_review", passages=[], reasons=reasons,
            inspection=inspection,
        )

    raw_pages = extraction.get("pages")
    if not isinstance(raw_pages, list):
        raw_pages = []
    pages = [
        (entry.get("page"), entry["text"])
        for entry in raw_pages
        if isinstance(entry, dict) and isinstance(entry.get("text"), str)
    ]
    passages = _pdf_passages(pages)
    substantive = any(text.strip() for _page, text in pages)

    if not substantive:
        reasons += ["pdf_scan_or_blank_page", "pdf_no_extractable_text"]
        final_status = "needs_review"
    elif status == "limited":
        reasons += ["pdf_column_order_uncertain"]
        final_status = "needs_review"
    else:
        total = sum(len(normalize_text(text)[0]) for _page, text in pages)
        if total < MIN_TEXT_CHARS:
            reasons += ["pdf_text_too_short", "pdf_column_order_uncertain"]
            final_status = "needs_review"
        elif _looks_garbled(" ".join(text for _page, text in pages)):
            reasons += ["pdf_text_garbled", "pdf_column_order_uncertain"]
            final_status = "needs_review"
        else:
            reasons += ["pdf_column_order_uncertain", "pdf_non_text_assets_unverified"]
            final_status = "parsed"

    return _assemble(
        digest, byte_size, status=final_status, passages=passages, reasons=reasons,
        inspection=inspection,
    )


def parse_pdf(data: bytes, *, inspector=None, extractor=None) -> dict:
    """Return the bounded extraction result for PDF bytes.

    ``extractor`` is the full-text seam (``bytes -> extraction mapping``); when
    omitted and ``pypdf`` is importable the isolated child extractor runs.
    ``inspector`` is the Step 06 integrity diagnostics seam; when it is given
    and no ``extractor`` is, the conservative diagnostics-only path runs (no
    passages). With neither, the default is extraction when ``pypdf`` is
    available, otherwise the diagnostics path reporting the dependency missing.

    The returned mapping is JSON-safe, always keeps ``excludes_candidate`` false
    and ``human_action_required`` true, and carries page-locatored passages only
    when usable text was extracted.
    """
    if isinstance(data, (bytearray, memoryview)):
        data = bytes(data)
    if not isinstance(data, bytes):
        raise PdfAdapterRequestError("data must be bytes")

    digest = hashlib.sha256(data).hexdigest()
    byte_size = len(data)

    if byte_size == 0:
        return _assemble(
            digest, byte_size, status="needs_review", passages=[],
            reasons=["pdf_full_extraction_unsupported", "pdf_empty_input"],
            inspection=None,
        )

    if extractor is None and inspector is None and _pypdf_available():
        extractor = _extract_pdf_text

    if extractor is not None:
        return _from_extraction(digest, byte_size, _run_extractor(extractor, data))

    # Diagnostics-only path: no text extractor was provided.
    diagnostics = inspector
    if diagnostics is None:
        from ..acquisition.pdf_inspection import inspect_pdf

        diagnostics = inspect_pdf

    inspection = None
    try:
        inspection = diagnostics(data)
    except Exception:  # noqa: BLE001 - diagnostics must never crash parsing
        inspection = {"status": "failed", "failure_code": "internal_error"}

    reasons = ["pdf_full_extraction_unsupported"]
    if isinstance(inspection, dict):
        reasons.extend(_inspection_reasons(inspection))

    return _assemble(
        digest, byte_size, status="needs_review", passages=[], reasons=reasons,
        inspection=inspection,
    )
