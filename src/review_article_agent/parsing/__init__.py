"""Step 07 parsing core: bounded XML-first structure, locators, PDF review.

This package owns the **minimal Step 07 core (S7-CORE)** of
``docs/implementation/07-parsing.md`` -- see the module docstrings for the
explicit non-claims.  Public API:

* :func:`~review_article_agent.parsing.core.parse_document` -- JATS/TEI bytes
  to a JSON-safe structural map (passages, section paths, assets, citation
  links, versions, digests).
* :func:`~review_article_agent.parsing.core.normalize_text` -- the documented,
  conservative normalization (NFC, ligatures, soft hyphen, whitespace).
* :func:`~review_article_agent.parsing.locator.make_locator`,
  :func:`~review_article_agent.parsing.locator.resolve_locator`,
  :func:`~review_article_agent.parsing.locator.passage_citation_check` --
  passage locators, staleness resolution, and local citation-link checks.
* :func:`~review_article_agent.parsing.pdf_adapter.parse_pdf` -- the
  conservative PDF human-review result (never full text).
* :class:`~review_article_agent.parsing.repository.ParsingRepository` -- persist
  a parsed result as a distinct immutable ``report`` revision and read it back
  (S7-STORE); no schema change, no CLI.

The parsing package itself does no storage, CLI, network, model, or approval;
nothing here authorises scientific digestion, screening, extraction, or
exclusion.
"""

from __future__ import annotations

from .core import (
    ASSET_KINDS,
    DOCUMENT_TYPE,
    FORMATS,
    LIMITATIONS,
    MARKER_STATES,
    MAX_BYTE_SIZE,
    NORMALIZATION_VERSION,
    PARSER_VERSION,
    REASONS,
    SCHEMA_VERSION,
    STATUSES,
    ParsingRequestError,
    normalize_text,
    parse_document,
)
from .locator import (
    LOCATOR_STATUSES,
    make_locator,
    passage_citation_check,
    resolve_locator,
)
from .pdf_adapter import PdfAdapterRequestError, parse_pdf
from .repository import ParsingRepository, ParsingRepositoryError

__all__ = [
    "SCHEMA_VERSION",
    "DOCUMENT_TYPE",
    "PARSER_VERSION",
    "NORMALIZATION_VERSION",
    "MAX_BYTE_SIZE",
    "FORMATS",
    "STATUSES",
    "MARKER_STATES",
    "ASSET_KINDS",
    "REASONS",
    "LIMITATIONS",
    "LOCATOR_STATUSES",
    "ParsingRequestError",
    "PdfAdapterRequestError",
    "parse_document",
    "normalize_text",
    "make_locator",
    "resolve_locator",
    "passage_citation_check",
    "parse_pdf",
    "ParsingRepository",
    "ParsingRepositoryError",
]
