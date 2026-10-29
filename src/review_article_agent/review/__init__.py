"""Step 12 bounded critique and original-source fact-checking (pure rules).

Public API lives in :mod:`review_article_agent.review.schema`.
"""

from .schema import (
    BLOCKING_SEVERITIES,
    FACTUAL_KINDS,
    NON_FACTUAL_KINDS,
    ROUND_LIMITS,
    SEVERITIES,
    ReviewError,
    check_repair,
    evaluate,
    fact_check,
    normalize_finding,
    stale_checks,
    writer_response,
)
from .workflow import (
    ARTIFACT_KIND,
    DOCUMENT_TYPE,
    INDEPENDENT_VERIFICATION_NOTICE,
    SCHEMA_VERSION,
    STAGES,
    SectionReviewError,
    SectionReviewRepository,
)

__all__ = [
    "ARTIFACT_KIND",
    "DOCUMENT_TYPE",
    "INDEPENDENT_VERIFICATION_NOTICE",
    "SCHEMA_VERSION",
    "STAGES",
    "SectionReviewError",
    "SectionReviewRepository",
    "BLOCKING_SEVERITIES",
    "FACTUAL_KINDS",
    "NON_FACTUAL_KINDS",
    "ROUND_LIMITS",
    "SEVERITIES",
    "ReviewError",
    "check_repair",
    "evaluate",
    "fact_check",
    "normalize_finding",
    "stale_checks",
    "writer_response",
]
