"""Per-role **hosted-use authorization** for the generative model roles.

This package owns the *authorization document* and the *named human gate* for the
generative roles built on the direct OpenRouter route
(:mod:`review_article_agent.runtime.generation`). It deliberately reuses the
existing gates — human hosted-use approval, rights, and budget reserve via
``gate prepare`` / ``gate confirm`` — rather than inventing a parallel
authorization path.

Authority
---------

* ``docs/implementation/kickoffs/16-mvp-hardening.md`` §5 ("One real model port";
  "Reuse those gates; do not invent a parallel authorization path"; "Model per
  role is configurable").
* The tested precedents it mirrors: :mod:`review_article_agent.discovery.lead_runner`
  and :mod:`review_article_agent.extraction.runner`.

This package is **not** an authorizer: the hosted-use document is a *description*
of an authorization, and the out-of-band, owner-authenticated ``batch`` gate
(``generation.hosted_use.<role>``) is the authority. No field of the document —
and no model output — can create, widen, or substitute for that gate.
"""

from __future__ import annotations

from .hosted_use import (
    HOSTED_USE_DOCUMENT_TYPE,
    HOSTED_USE_GATE_KIND,
    HOSTED_USE_GATE_PREFIX,
    SCHEMA_VERSION,
    GenerationHostedUseAuthorizationError,
    GenerationHostedUseDocumentError,
    GenerationHostedUseError,
    GenerationRoleModelError,
    hosted_use_gate_id,
    propose_hosted_use,
    require_hosted_use,
    role_models_distinct,
    validate_hosted_use_document,
)

__all__ = [
    "SCHEMA_VERSION",
    "HOSTED_USE_DOCUMENT_TYPE",
    "HOSTED_USE_GATE_KIND",
    "HOSTED_USE_GATE_PREFIX",
    "GenerationHostedUseError",
    "GenerationHostedUseDocumentError",
    "GenerationHostedUseAuthorizationError",
    "GenerationRoleModelError",
    "validate_hosted_use_document",
    "hosted_use_gate_id",
    "propose_hosted_use",
    "require_hosted_use",
    "role_models_distinct",
]
