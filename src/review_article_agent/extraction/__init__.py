"""Step 09 extraction schema gate: strict, grounded, JSON-portable evidence.

This package owns the **minimal S9-SCHEMA outcome** of
``docs/implementation/09-extraction.md`` -- schema plus original-evidence
validation only.  Public API:

* :data:`~review_article_agent.extraction.schema.EXTRACTION_INSTRUCTIONS` -- the
  trusted instruction string the runtime runner includes with original passages.
* :func:`~review_article_agent.extraction.schema.validate_extraction` -- normalize
  and validate one untrusted extraction document, or raise
  :class:`~review_article_agent.extraction.schema.ExtractionError`.
* :func:`~review_article_agent.extraction.schema.extraction_example` -- a
  synthetic reusable fixture for downstream workflow tests.

Nothing here calls a model, reads storage, resolves a workflow, appraises a study
scientifically, or authorizes digestion/synthesis; passing validation is a
structural, grounding, and coverage check only.
"""

from __future__ import annotations

from .schema import (
    APPRAISAL_STATES,
    ATTRIBUTIONS,
    CITATION_FLAGS,
    CONTEXT_FIELDS,
    COVERAGE_DISPOSITIONS,
    DOCUMENT_TYPE,
    EXTRACTION_INSTRUCTIONS,
    LINKAGE_NOTICE,
    LINK_RELATIONS,
    LINK_STATUSES,
    SCHEMA_VERSION,
    STATED_STATES,
    VALIDATION_SCOPE,
    ExtractionError,
    extraction_example,
    validate_extraction,
)

__all__ = [
    "APPRAISAL_STATES",
    "ATTRIBUTIONS",
    "CITATION_FLAGS",
    "CONTEXT_FIELDS",
    "COVERAGE_DISPOSITIONS",
    "DOCUMENT_TYPE",
    "LINKAGE_NOTICE",
    "EXTRACTION_INSTRUCTIONS",
    "LINK_RELATIONS",
    "LINK_STATUSES",
    "SCHEMA_VERSION",
    "STATED_STATES",
    "VALIDATION_SCOPE",
    "ExtractionError",
    "extraction_example",
    "validate_extraction",
]
