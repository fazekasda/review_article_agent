"""Step 11 drafting package: approved, reconstructible evidence/citation packages.

This package owns the **minimal S11-PACKAGE outcome** of
``docs/implementation/11-drafting.md`` AC2/AC3/AC5 -- assemble a deterministic
drafting package for one approved outline section from the exact current accepted
synthesis/framework/outline chain, the accepted extraction batch cases, and
verified frozen corpus metadata.  It reconstructs entirely from durable store
artifacts (no raw session history), performs no live execution and no model call,
and adds no storage migration.

Public API:

* :class:`~review_article_agent.drafting.package.DraftingPackageRepository` --
  :meth:`~review_article_agent.drafting.package.DraftingPackageRepository.build`
  returns the JSON package for one section.
* :func:`~review_article_agent.drafting.package.citation_key` -- the stable,
  deterministic project + candidate citation key.

Workflow/gates, role sessions, CLI, and the schema validator are owned by other
tasks and are deliberately out of scope here.
"""

from __future__ import annotations

from .package import (
    DOCUMENT_TYPE,
    NOTICE,
    RETAINED_DISPOSITIONS,
    SCHEMA_VERSION,
    DraftingPackageError,
    DraftingPackageRepository,
    DraftingRepository,
    citation_key,
)

__all__ = [
    "DOCUMENT_TYPE",
    "NOTICE",
    "RETAINED_DISPOSITIONS",
    "SCHEMA_VERSION",
    "DraftingPackageError",
    "DraftingPackageRepository",
    "DraftingRepository",
    "citation_key",
]
