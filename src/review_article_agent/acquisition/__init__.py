"""Fail-closed acquisition asset validation (Step 06 slice).

This package owns the **first bounded slice** of
``docs/implementation/06-acquisition.md``: pure, standard-library byte
validation of a would-be acquired source document's *container* and *exact*
declared identity.  See :mod:`review_article_agent.acquisition.validation`.

What this package **is**:

* :func:`~review_article_agent.acquisition.validation.validate_asset` -- a pure
  function from bytes plus expected identifiers to a JSON-safe observation.  A
  file extension or ``Content-Type`` never establishes success; HTML/login
  responses, grossly corrupt PDFs, and malicious XML are rejected; a plausible
  but uninspected PDF is ``needs_review``; a JATS full article is parsed with
  bounded node/depth/text limits and an exact ``front/article-meta`` identity
  check.

What this package is **not**:

* no PDF/full-text parser, no scientific reading, no storage, no CLI, no
  network, no approval, and no authorization.  A ``ready`` result never
  authorises scientific digestion, screening, or eligibility -- those remain
  later tasks and explicit human dispositions.
"""

from __future__ import annotations

from .validation import (
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

__all__ = [
    "SCHEMA_VERSION",
    "DOCUMENT_TYPE",
    "VALIDATOR_VERSION",
    "MAX_BYTE_SIZE",
    "MEDIA_KINDS",
    "STATUSES",
    "CHECK_NAMES",
    "CHECK_STATES",
    "IDENTIFIER_NAMESPACES",
    "REASONS",
    "AcquisitionValidationError",
    "validate_asset",
]
