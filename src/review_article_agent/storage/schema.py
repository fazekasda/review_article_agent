"""Versioned, transactional SQLite schema migrations.

Migration state is tracked with SQLite's built-in ``PRAGMA user_version`` — the
simplest durable version marker that needs no bootstrap table. Each step runs
inside its own ``BEGIN IMMEDIATE`` transaction and advances ``user_version`` in
the same transaction, so a failed migration rolls back completely and leaves
the previous version intact (``ARCH-MIGRATE``; step doc line 21).

A database written by a **newer** schema version is refused with
:class:`~review_article_agent.domain.errors.UnsupportedSchemaVersion` rather
than read or downgraded.

Both the per-project store and the application registry keep independent,
independently-versioned schemas. Later workers (artifacts, approvals, backup)
add new numbered migrations without rewriting an existing one, so prior data —
including future artifact rows — is preserved across upgrades.
"""

from __future__ import annotations

import sqlite3
from typing import Callable, Mapping

from ..domain.errors import MigrationError, StoreError, UnsupportedSchemaVersion

#: SQL statements that must hold for every connection.
COMMON_PRAGMAS = (
    "PRAGMA foreign_keys = ON",
    "PRAGMA journal_mode = WAL",
    "PRAGMA synchronous = FULL",
)

Migration = Callable[[sqlite3.Connection], None]

# --------------------------------------------------------------------------- #
# Per-project store schema
# --------------------------------------------------------------------------- #
STORE_SCHEMA_VERSION = 1

#: Registry maps project IDs to names and locations and records the single
#: selected project. It is *not* a cache of project data.
REGISTRY_SCHEMA_VERSION = 1


def _store_v1(conn: sqlite3.Connection) -> None:
    """Create the store's identity/metadata tables.

    Only project identity and application metadata live here in Step 02. Later
    steps add artifact/approval tables via new migrations; the ``meta`` table
    stays stable so migrations can record their own bookkeeping.
    """
    conn.execute(
        """
        CREATE TABLE meta (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE project (
            project_id    TEXT PRIMARY KEY,
            name          TEXT,
            created_at_utc TEXT NOT NULL,
            app_version   TEXT NOT NULL
        )
        """
    )


STORE_MIGRATIONS: Mapping[int, Migration] = {
    1: _store_v1,
}


def _registry_v1(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE projects (
            project_id     TEXT PRIMARY KEY,
            name           TEXT NOT NULL,
            root_path      TEXT NOT NULL UNIQUE,
            created_at_utc TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE active_project (
            singleton      INTEGER PRIMARY KEY CHECK (singleton = 1),
            project_id     TEXT NOT NULL REFERENCES projects(project_id),
            selected_at_utc TEXT NOT NULL
        )
        """
    )


REGISTRY_MIGRATIONS: Mapping[int, Migration] = {
    1: _registry_v1,
}


# --------------------------------------------------------------------------- #
# Engine
# --------------------------------------------------------------------------- #
def read_user_version(conn: sqlite3.Connection) -> int:
    """Return the current ``PRAGMA user_version`` as an int."""
    row = conn.execute("PRAGMA user_version").fetchone()
    return int(row[0]) if row is not None else 0


def migrate(
    conn: sqlite3.Connection,
    *,
    target_version: int,
    migrations: Mapping[int, Migration],
    label: str,
) -> int:
    """Bring ``conn`` up to ``target_version`` transactionally.

    Returns the resulting version. Raises :class:`UnsupportedSchemaVersion` if
    the database is newer than ``target_version`` and :class:`MigrationError`
    if any step fails (that step is rolled back; earlier applied steps remain).
    """
    current = read_user_version(conn)
    if current > target_version:
        raise UnsupportedSchemaVersion(
            label=label, found=current, supported=target_version
        )
    if current == target_version:
        return current

    for version in range(current + 1, target_version + 1):
        step = migrations.get(version)
        if step is None:
            raise MigrationError(
                label=label,
                target=version,
                reason="no migration registered for this version",
            )
        try:
            conn.execute("BEGIN IMMEDIATE")
        except sqlite3.Error as exc:  # pragma: no cover - environment dependent
            raise MigrationError(
                label=label, target=version, reason="could not begin transaction"
            ) from exc
        try:
            step(conn)
            # ``PRAGMA`` does not accept bound parameters; ``version`` is an
            # int from our own closed range, never caller text.
            conn.execute(f"PRAGMA user_version = {int(version)}")
            conn.execute("COMMIT")
        except Exception as exc:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:  # pragma: no cover - rollback best effort
                pass
            raise MigrationError(
                label=label, target=version, reason=_summarise(exc)
            ) from exc
    return target_version


def _summarise(exc: BaseException) -> str:
    """Summarise an exception for an actionable, secret-free message."""
    if isinstance(exc, sqlite3.Error):
        # sqlite messages describe the failure (e.g. "table already exists")
        # and never contain application data or credentials.
        return f"sqlite: {exc}"
    return exc.__class__.__name__


def open_connection(db_path: str) -> sqlite3.Connection:
    """Open a store/registry connection configured for this package.

    ``isolation_level=None`` puts the connection in autocommit mode so the
    migration and transaction helpers control ``BEGIN``/``COMMIT`` explicitly.
    """
    try:
        conn = sqlite3.connect(db_path, isolation_level=None, timeout=30.0)
    except sqlite3.Error as exc:
        raise StoreError(
            f"could not open the local database at {db_path!r}."
        ) from exc
    conn.row_factory = sqlite3.Row
    for pragma in COMMON_PRAGMAS:
        try:
            conn.execute(pragma)
        except sqlite3.Error as exc:
            conn.close()
            raise StoreError(
                f"could not configure the local database at {db_path!r}."
            ) from exc
    return conn
