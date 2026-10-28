"""Storage layer: per-project SQLite stores plus the atomic app registry.

Public surface for later workers (artifact repository, approvals, backup, CLI):

* :class:`~review_article_agent.storage.store.ProjectStore` — open/create a
  project store, reuse its connection/transaction, read its identity.
* :class:`~review_article_agent.storage.registry.Registry` — create/list/select/
  status/relocate/register_restored over the app-level registry.
* :class:`~review_article_agent.storage.registry.ProjectInfo` — a project
  projection.
* :mod:`review_article_agent.storage.paths` — layout, stable IDs, path safety.
* :mod:`review_article_agent.storage.schema` — versioned migrations.
"""

from __future__ import annotations

from .approvals import (
    ApprovalError,
    ApprovalRepository,
    FixtureApproval,
    FixtureApprovalStatus,
    FixtureSnapshotEntry,
    SnapshotVerification,
)
from .artifacts import (
    Artifact,
    ArtifactConflictError,
    ArtifactRepository,
    ArtifactRepositoryError,
    DependencyRef,
    Revision,
    RightsRef,
)
from .registry import ProjectInfo, Registry
from .store import ProjectStore, transaction

__all__ = [
    "ApprovalError",
    "ApprovalRepository",
    "Artifact",
    "ArtifactConflictError",
    "ArtifactRepository",
    "ArtifactRepositoryError",
    "DependencyRef",
    "FixtureApproval",
    "FixtureApprovalStatus",
    "FixtureSnapshotEntry",
    "Registry",
    "ProjectInfo",
    "ProjectStore",
    "Revision",
    "RightsRef",
    "SnapshotVerification",
    "transaction",
]
