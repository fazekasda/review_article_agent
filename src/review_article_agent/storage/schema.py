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
from datetime import datetime, timezone
from typing import Callable, Mapping
from urllib.parse import quote

from ..domain.errors import MigrationError, StoreError, UnsupportedSchemaVersion


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


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
STORE_SCHEMA_VERSION = 3

#: Registry maps project IDs to names and locations, records the single
#: *selected* project, and records durable **execution activation** (which
#: project, if any, is authorized to execute). It is *not* a cache of project
#: data.
REGISTRY_SCHEMA_VERSION = 2


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


#: Statuses an :class:`ArtifactRecord` identity row may hold (C01 §5.1). Lifecycle
#: only; it never alters revision immutability.
ARTIFACT_STATUSES = ("active", "superseded", "withdrawn")


def _store_v2(conn: sqlite3.Connection) -> None:
    """Add the immutable artifact/revision/rights/dependency/import tables.

    This migration only *adds* tables and triggers; it never rewrites v1 rows, so
    a store created at version 1 keeps its ``project``/``meta`` identity metadata
    (``ARCH-ASSETS``; step doc line 21).

    Immutability is enforced **at the database**, not only in the Python API: a
    revision's content, hash, provenance, dependency set, and pinned rights key
    can never be rewritten by an ``UPDATE``, and a written row can never be
    ``DELETE``d. A change is therefore always a **new** revision row. Composite
    foreign keys keep every dependency pinned to existing same-project revisions
    and every revision pinned to an existing rights record, so a failed
    transaction can leave no dangling reference (all rows are written together).
    """
    conn.execute(
        """
        CREATE TABLE artifact (
            project_id     TEXT NOT NULL,
            artifact_id    TEXT NOT NULL,
            kind           TEXT NOT NULL,
            created_at_utc TEXT NOT NULL,
            created_by     TEXT NOT NULL,
            label          TEXT,
            status         TEXT NOT NULL DEFAULT 'active',
            PRIMARY KEY (project_id, artifact_id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE rights_record (
            project_id          TEXT NOT NULL,
            rights_id           TEXT NOT NULL,
            record_revision     INTEGER NOT NULL,
            subject_kind        TEXT NOT NULL,
            subject_project_id  TEXT NOT NULL,
            subject_artifact_id TEXT NOT NULL,
            subject_revision_id TEXT,
            canonical           BLOB NOT NULL,
            content_sha256      TEXT NOT NULL,
            recorded_at_utc     TEXT NOT NULL,
            PRIMARY KEY (project_id, rights_id, record_revision)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE revision (
            project_id                   TEXT NOT NULL,
            artifact_id                  TEXT NOT NULL,
            revision_id                  TEXT NOT NULL,
            parent_revision_id           TEXT,
            content                      BLOB NOT NULL,
            content_sha256               TEXT NOT NULL,
            byte_size                    INTEGER NOT NULL,
            media_type                   TEXT,
            origin_locator               TEXT NOT NULL,
            acquired_at_utc              TEXT NOT NULL,
            acquired_by                  TEXT NOT NULL,
            provenance_method            TEXT NOT NULL,
            provenance_actor             TEXT NOT NULL,
            provenance_performed_at_utc  TEXT NOT NULL,
            rights_project_id            TEXT NOT NULL,
            rights_id                    TEXT NOT NULL,
            rights_record_revision       INTEGER NOT NULL,
            immutable                    INTEGER NOT NULL DEFAULT 1
                                         CHECK (immutable = 1),
            PRIMARY KEY (project_id, artifact_id, revision_id),
            FOREIGN KEY (project_id, artifact_id)
                REFERENCES artifact (project_id, artifact_id),
            FOREIGN KEY (project_id, artifact_id, parent_revision_id)
                REFERENCES revision (project_id, artifact_id, revision_id),
            FOREIGN KEY (rights_project_id, rights_id, rights_record_revision)
                REFERENCES rights_record (project_id, rights_id, record_revision)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE dependency (
            project_id        TEXT NOT NULL,
            from_artifact_id  TEXT NOT NULL,
            from_revision_id  TEXT NOT NULL,
            dependency_id     TEXT NOT NULL,
            role              TEXT NOT NULL,
            to_project_id     TEXT NOT NULL,
            to_artifact_id    TEXT NOT NULL,
            to_revision_id    TEXT NOT NULL,
            created_at_utc    TEXT NOT NULL,
            PRIMARY KEY (project_id, from_artifact_id, from_revision_id, dependency_id),
            FOREIGN KEY (project_id, from_artifact_id, from_revision_id)
                REFERENCES revision (project_id, artifact_id, revision_id),
            FOREIGN KEY (to_project_id, to_artifact_id, to_revision_id)
                REFERENCES revision (project_id, artifact_id, revision_id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE import_request (
            project_id     TEXT NOT NULL,
            request_id     TEXT NOT NULL,
            fingerprint    TEXT NOT NULL,
            artifact_id    TEXT NOT NULL,
            revision_id    TEXT NOT NULL,
            created_at_utc TEXT NOT NULL,
            PRIMARY KEY (project_id, request_id)
        )
        """
    )

    # Identity columns of a logical artifact are fixed at creation; only the
    # lifecycle ``status``/``label`` may change, and the row is never deleted.
    conn.execute(
        """
        CREATE TRIGGER artifact_identity_immutable
        BEFORE UPDATE ON artifact
        WHEN NEW.project_id <> OLD.project_id
          OR NEW.artifact_id <> OLD.artifact_id
          OR NEW.kind <> OLD.kind
          OR NEW.created_at_utc <> OLD.created_at_utc
          OR NEW.created_by <> OLD.created_by
        BEGIN
            SELECT RAISE(ABORT, 'artifact identity rows are immutable');
        END
        """
    )
    conn.execute(
        """
        CREATE TRIGGER artifact_no_delete
        BEFORE DELETE ON artifact
        BEGIN
            SELECT RAISE(ABORT, 'artifact identity rows are immutable');
        END
        """
    )
    for table in ("revision", "rights_record", "dependency", "import_request"):
        conn.execute(
            f"""
            CREATE TRIGGER {table}_no_update
            BEFORE UPDATE ON {table}
            BEGIN
                SELECT RAISE(ABORT, '{table} rows are immutable');
            END
            """
        )
        conn.execute(
            f"""
            CREATE TRIGGER {table}_no_delete
            BEFORE DELETE ON {table}
            BEGIN
                SELECT RAISE(ABORT, '{table} rows are immutable');
            END
            """
        )


def _store_v3(conn: sqlite3.Connection) -> None:
    """Add the immutable *local storage-fixture approval* tables.

    These tables back ``storage/approvals.py``: a deliberately local, synthetic
    approval/check layer that records an exact target revision plus its full
    transitive dependency closure. It is **not** a scientific approval and can
    never authorize execution or export (``authorizes_execution`` is pinned to
    ``0`` and ``scope`` to ``'storage_fixture'`` by ``CHECK`` constraints).

    Only tables and triggers are added; migration 1/2 rows (project identity,
    artifacts, revisions, rights, dependencies, import requests) are never
    rewritten, so their exact bytes/refs are preserved across the upgrade
    (``ARCH-ASSETS``; step doc line 21). Immutability is enforced at the
    database: an approval row and its snapshot entries can never be ``UPDATE``d
    or ``DELETE``d, so a change is always a **new** approval row. Composite
    foreign keys pin every approval and every snapshot entry to an existing
    same-project revision, so a failed transaction can leave no dangling
    approval.
    """
    conn.execute(
        """
        CREATE TABLE fixture_approval (
            project_id             TEXT NOT NULL,
            approval_id            TEXT NOT NULL,
            target_artifact_id     TEXT NOT NULL,
            target_revision_id     TEXT NOT NULL,
            gate_kind              TEXT NOT NULL,
            gate_id                TEXT NOT NULL,
            decision               TEXT NOT NULL,
            scope                  TEXT NOT NULL DEFAULT 'storage_fixture'
                                   CHECK (scope = 'storage_fixture'),
            authorizes_execution   INTEGER NOT NULL DEFAULT 0
                                   CHECK (authorizes_execution = 0),
            actor_label            TEXT NOT NULL,
            actor_authenticated    INTEGER NOT NULL DEFAULT 0
                                   CHECK (actor_authenticated = 0),
            rationale              TEXT NOT NULL,
            granted_at_utc         TEXT NOT NULL,
            snapshot_id            TEXT NOT NULL,
            snapshot_digest        TEXT NOT NULL,
            storage_schema_version INTEGER NOT NULL,
            context_kind           TEXT NOT NULL,
            context_note           TEXT,
            created_at_utc         TEXT NOT NULL,
            PRIMARY KEY (project_id, approval_id),
            FOREIGN KEY (project_id, target_artifact_id, target_revision_id)
                REFERENCES revision (project_id, artifact_id, revision_id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE fixture_approval_entry (
            project_id             TEXT NOT NULL,
            approval_id            TEXT NOT NULL,
            entry_index            INTEGER NOT NULL,
            artifact_id            TEXT NOT NULL,
            revision_id            TEXT NOT NULL,
            content_sha256         TEXT NOT NULL,
            role                   TEXT NOT NULL,
            rights_id              TEXT NOT NULL,
            rights_record_revision INTEGER NOT NULL,
            byte_size              INTEGER NOT NULL,
            PRIMARY KEY (project_id, approval_id, entry_index),
            FOREIGN KEY (project_id, approval_id)
                REFERENCES fixture_approval (project_id, approval_id),
            FOREIGN KEY (project_id, artifact_id, revision_id)
                REFERENCES revision (project_id, artifact_id, revision_id)
        )
        """
    )
    for table in ("fixture_approval", "fixture_approval_entry"):
        conn.execute(
            f"""
            CREATE TRIGGER {table}_no_update
            BEFORE UPDATE ON {table}
            BEGIN
                SELECT RAISE(ABORT, '{table} rows are immutable');
            END
            """
        )
        conn.execute(
            f"""
            CREATE TRIGGER {table}_no_delete
            BEFORE DELETE ON {table}
            BEGIN
                SELECT RAISE(ABORT, '{table} rows are immutable');
            END
            """
        )


STORE_MIGRATIONS: Mapping[int, Migration] = {
    1: _store_v1,
    2: _store_v2,
    3: _store_v3,
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


def _registry_v2(conn: sqlite3.Connection) -> None:
    """Add the durable **execution-activation** singleton (C02 §8.5).

    Selection (``active_project``) is a UI/bookkeeping choice and never proof
    that a project is executing. Execution activation is a *separate* durable
    record: at most one project may hold it, and it is fenced by an opaque
    ``activation_token`` so a stale engine can never release it or authorize a
    local write after a switch (``ARCH-ACTIVE-ENF``; step 03 AC4/AC5).

    ``state`` is one of ``none``/``active``/``draining``.  ``active_project_id``
    and ``activation_token`` are ``NULL`` **iff** ``state = 'none'`` — enforced
    at the database, so a half-cleared activation can never persist (C02 §8.5,
    §11.6).  This migration only *adds* a table and seeds the single ``none``
    row; existing ``projects``/``active_project`` rows are never rewritten, so
    mappings and the selected project are preserved across the upgrade.
    """
    conn.execute(
        """
        CREATE TABLE execution_activation (
            singleton         INTEGER PRIMARY KEY CHECK (singleton = 1),
            state             TEXT NOT NULL
                              CHECK (state IN ('none', 'active', 'draining')),
            active_project_id TEXT REFERENCES projects(project_id),
            activation_token  TEXT,
            activated_at_utc  TEXT,
            updated_at_utc    TEXT NOT NULL,
            CHECK ((state = 'none') = (active_project_id IS NULL)),
            CHECK ((state = 'none') = (activation_token IS NULL))
        )
        """
    )
    conn.execute(
        "INSERT INTO execution_activation "
        "(singleton, state, active_project_id, activation_token, "
        " activated_at_utc, updated_at_utc) "
        "VALUES (1, 'none', NULL, NULL, NULL, ?)",
        (_utc_now(),),
    )


REGISTRY_MIGRATIONS: Mapping[int, Migration] = {
    1: _registry_v1,
    2: _registry_v2,
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

    Concurrency: the version is re-read **under the ``BEGIN IMMEDIATE`` write
    lock** before each step. Two connections that both observed an old version
    therefore do not both run the same step: the loser, once it holds the lock,
    sees the step already applied and skips it (and refuses a version newer than
    ``target_version`` that appeared while waiting). This keeps a single
    transactional migration path without a duplicate-``CREATE TABLE`` failure.
    """
    while True:
        current = read_user_version(conn)
        if current > target_version:
            raise UnsupportedSchemaVersion(
                label=label, found=current, supported=target_version
            )
        if current == target_version:
            return current

        version = current + 1
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
            # Re-read under the write lock: a concurrent migrator may have
            # applied this step (or advanced past it) while we waited to begin.
            locked = read_user_version(conn)
            if locked > target_version:
                conn.execute("ROLLBACK")
                raise UnsupportedSchemaVersion(
                    label=label, found=locked, supported=target_version
                )
            if locked < version:
                step(conn)
                # ``PRAGMA`` does not accept bound parameters; ``version`` is an
                # int from our own closed range, never caller text.
                conn.execute(f"PRAGMA user_version = {int(version)}")
            conn.execute("COMMIT")
        except UnsupportedSchemaVersion:
            raise
        except Exception as exc:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:  # pragma: no cover - rollback best effort
                pass
            raise MigrationError(
                label=label, target=version, reason=_summarise(exc)
            ) from exc
        # Loop re-reads the committed version and either applies the next step
        # or returns.


def _summarise(exc: BaseException) -> str:
    """Summarise an exception for an actionable, secret-free message."""
    if isinstance(exc, sqlite3.Error):
        # sqlite messages describe the failure (e.g. "table already exists")
        # and never contain application data or credentials.
        return f"sqlite: {exc}"
    return exc.__class__.__name__


def read_only_probe(db_path: str) -> tuple[int, set[str]]:
    """Return ``(user_version, user_table_names)`` **read-only**.

    Used to classify an existing file (registry or store) as empty, ours, or
    foreign *before* any pragma/migration is applied. Opens with ``mode=ro``
    **only** — there is no writable fallback; a read-only open failure raises a
    clean :class:`StoreError` and an absent path stays absent.

    As with any WAL read, SQLite may create read-only ``-shm``/``-wal``
    shared-memory sidecars in the directory; the database file itself is not
    modified and no ``journal_mode`` change or migration occurs.
    """
    try:
        conn: sqlite3.Connection = sqlite3.connect(
            "file:" + quote(db_path) + "?mode=ro", uri=True, isolation_level=None
        )
    except sqlite3.Error as exc:
        raise StoreError(
            f"could not open the local database at {db_path!r} read-only "
            f"({exc.__class__.__name__}); refusing to fall back to a writable "
            f"open."
        ) from exc
    try:
        version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    except sqlite3.DatabaseError as exc:
        raise StoreError(
            f"the file at {db_path!r} is not a readable SQLite database."
        ) from exc
    finally:
        conn.close()
    return version, tables


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
