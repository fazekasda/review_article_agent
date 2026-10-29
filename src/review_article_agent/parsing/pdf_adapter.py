"""Conservative PDF adapter: always a human action, never full text (Step 07).

Authority: ``docs/implementation/07-parsing.md`` AC21 ("Failed parsing cannot
yield an empty-paper exclusion. Required unavailable evidence produces a human
action"), the requirement that "PDF full extraction may stay explicitly
unsupported MVP", and ``docs/implementation/handoffs/06-acquisition.md``:
"PDFs are always exact-source human review. The optional ``pypdf`` inspector is
a **diagnostic** (input integrity only), **not** a parser."

This adapter therefore **never** produces passages.  It optionally calls the
bounded Step 06 diagnostics inspector
(:func:`review_article_agent.acquisition.pdf_inspection.inspect_pdf`) and maps
its integrity result to explicit reasons (scan / blank page, unavailable
parser/OCR, malformed, limited, and always "columns unreliable").  The private
``text_preview`` is *not* promoted to full text and is *not* copied into the
result.  Every outcome is ``needs_review`` with ``human_action_required`` true
and ``excludes_candidate`` false, so a failed PDF can never become a silent
empty-paper exclusion.
"""

from __future__ import annotations

import hashlib

from ..domain.errors import ReviewAgentError
from .core import (
    DOCUMENT_TYPE,
    LIMITATIONS,
    NORMALIZATION_VERSION,
    SCHEMA_VERSION,
)

PDF_PARSER_VERSION = "step07-pdf-manual/1"

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


class PdfAdapterRequestError(ReviewAgentError, ValueError):
    """The PDF adapter request was malformed (bad argument types)."""


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


def parse_pdf(data: bytes, *, inspector=None) -> dict:
    """Return the conservative, human-review result for PDF bytes.

    ``inspector`` defaults to the Step 06
    :func:`~review_article_agent.acquisition.pdf_inspection.inspect_pdf`; tests
    inject a deterministic stand-in.  The returned mapping is JSON-safe and
    always has ``passages == []``.
    """
    if isinstance(data, (bytearray, memoryview)):
        data = bytes(data)
    if not isinstance(data, bytes):
        raise PdfAdapterRequestError("data must be bytes")

    digest = hashlib.sha256(data).hexdigest()
    byte_size = len(data)

    reasons = ["pdf_full_extraction_unsupported"]
    if byte_size == 0:
        reasons.append("pdf_empty_input")

    inspection = None
    if inspector is None and byte_size > 0:
        from ..acquisition.pdf_inspection import inspect_pdf

        inspector = inspect_pdf

    if inspector is not None and byte_size > 0:
        try:
            inspection = inspector(data)
        except Exception:  # noqa: BLE001 - diagnostics must never crash parsing
            inspection = {"status": "failed", "failure_code": "internal_error"}
        if isinstance(inspection, dict):
            reasons.extend(_inspection_reasons(inspection))

    seen: set[str] = set()
    ordered: list[str] = []
    for reason in reasons:
        if reason not in seen:
            seen.add(reason)
            ordered.append(reason)

    return {
        "schema_version": SCHEMA_VERSION,
        "document_type": DOCUMENT_TYPE,
        "parser_version": PDF_PARSER_VERSION,
        "normalization_version": NORMALIZATION_VERSION,
        "format": "pdf",
        "status": "needs_review",
        "source": {"sha256": digest, "byte_size": byte_size},
        "passages": [],
        "references": [],
        "citation_links": [],
        "citations": {"resolved": 0, "unresolved": 0, "ambiguous": 0, "malformed": 0},
        "assets": [],
        "non_text_assets_require_review": True,
        "human_action_required": True,
        "excludes_candidate": False,
        "reasons": ordered,
        "pdf_inspection": _sanitize_inspection(inspection),
        "authorizes_scientific_processing": False,
        "limitations": LIMITATIONS,
    }
