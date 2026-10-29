"""Step 10 synthesis schema gate: pure evidence map, framework, and outline validation.

This package owns the **minimal S10-EVIDENCE outcome** of
``docs/implementation/10-synthesis.md`` -- a pure, JSON-portable payload schema
with no repository/CLI, no model call, and no statistical pooling.  Public API:

* :func:`~review_article_agent.synthesis.schema.validate_synthesis` -- normalize and
  validate one untrusted synthesis payload over accepted Step 09 cases, or raise
  :class:`~review_article_agent.synthesis.schema.SynthesisError`.
* :func:`~review_article_agent.synthesis.schema.validate_framework` -- validate a
  scientific framework against a validated synthesis.
* :func:`~review_article_agent.synthesis.schema.validate_outline` -- validate an
  ordered outline against a validated framework and synthesis.
* :func:`~review_article_agent.synthesis.schema.synthesis_example_cases`,
  :func:`~review_article_agent.synthesis.schema.synthesis_example_payload`,
  :func:`~review_article_agent.synthesis.schema.synthesis_example`,
  :func:`~review_article_agent.synthesis.schema.framework_example`, and
  :func:`~review_article_agent.synthesis.schema.outline_example` -- synthetic,
  reusable fixtures (never real evidence).

A repository/CLI wrapper adds the exact ``synthesis_ref``/``framework_ref``/
``outline_ref`` and ownership/gate records around these validated payloads.
"""

from __future__ import annotations

from .schema import (
    ACTOR_KINDS,
    CONCEPT_STATUSES,
    CONTEXT_FIELDS,
    FINDING_DISPOSITIONS,
    FRAMEWORK_CRITERIA,
    INDEPENDENCE_CONCLUSIONS,
    NOVELTY_VALUES,
    OUTLINE_NOTICE,
    SCHEMA_VERSION,
    STATEMENT_CLASSIFICATIONS,
    STATEMENT_STATUSES,
    STATED_STATES,
    SYNTHESIS_NOTICE,
    VALIDATION_SCOPE,
    FRAMEWORK_NOTICE,
    SynthesisError,
    framework_example,
    outline_example,
    synthesis_example,
    synthesis_example_cases,
    synthesis_example_payload,
    validate_framework,
    validate_outline,
    validate_synthesis,
)

__all__ = [
    "ACTOR_KINDS",
    "CONCEPT_STATUSES",
    "CONTEXT_FIELDS",
    "FINDING_DISPOSITIONS",
    "FRAMEWORK_CRITERIA",
    "FRAMEWORK_NOTICE",
    "INDEPENDENCE_CONCLUSIONS",
    "NOVELTY_VALUES",
    "OUTLINE_NOTICE",
    "SCHEMA_VERSION",
    "STATEMENT_CLASSIFICATIONS",
    "STATEMENT_STATUSES",
    "STATED_STATES",
    "SYNTHESIS_NOTICE",
    "VALIDATION_SCOPE",
    "SynthesisError",
    "framework_example",
    "outline_example",
    "synthesis_example",
    "synthesis_example_cases",
    "synthesis_example_payload",
    "validate_framework",
    "validate_outline",
    "validate_synthesis",
]
