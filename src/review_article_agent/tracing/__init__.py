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

__all__ = [
    "ATTRIBUTIONS",
    "POLICY_FIELDS",
    "TracingError",
    "assess_support",
    "classify_attribution",
    "trace_atom",
    "trace_claim",
    "trace_fingerprint",
    "validate_policy",
]
