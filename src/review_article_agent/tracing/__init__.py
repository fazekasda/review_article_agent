"""Step 13 configurable citation-origin tracing (pure rules).

Public API lives in :mod:`review_article_agent.tracing.schema`.
"""

from .schema import (
    ATTRIBUTIONS,
    POLICY_FIELDS,
    TracingError,
    assess_support,
    classify_attribution,
    trace_atom,
    trace_claim,
    trace_fingerprint,
    validate_policy,
)
from .workflow import (
    ARTIFACT_KIND,
    DOCUMENT_TYPE,
    SCHEMA_VERSION,
    OriginTraceError,
    OriginTraceRepository,
    origin_coverage,
)

__all__ = [
    "ARTIFACT_KIND",
    "ATTRIBUTIONS",
    "DOCUMENT_TYPE",
    "POLICY_FIELDS",
    "SCHEMA_VERSION",
    "OriginTraceError",
    "OriginTraceRepository",
    "TracingError",
    "assess_support",
    "classify_attribution",
    "origin_coverage",
    "trace_atom",
    "trace_claim",
    "trace_fingerprint",
    "validate_policy",
]
