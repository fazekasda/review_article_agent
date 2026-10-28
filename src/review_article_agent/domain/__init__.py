"""Bounded domain layer.

Step 02 only needs the error vocabulary for the storage/registry foundation;
scientific domain objects (sources, artifacts, approvals) are added by later
workers behind their own steps.
"""

from __future__ import annotations

from .errors import (
    InvalidProjectId,
    LockError,
    MigrationError,
    NoActiveProject,
    ProjectConflict,
    ProjectError,
    ProjectNotFound,
    ProjectPathError,
    RegistryError,
    ReviewAgentError,
    SchemaVersionError,
    StoreError,
    UnsupportedSchemaVersion,
)

__all__ = [
    "ReviewAgentError",
    "StoreError",
    "SchemaVersionError",
    "UnsupportedSchemaVersion",
    "MigrationError",
    "RegistryError",
    "LockError",
    "ProjectError",
    "ProjectNotFound",
    "InvalidProjectId",
    "ProjectConflict",
    "ProjectPathError",
    "NoActiveProject",
]
