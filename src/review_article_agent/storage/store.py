"""Per-project SQLite store with reusable connection/transaction/identity.

A :class:`ProjectStore` owns exactly one project's app-owned directory
(``<root>/.review_article_agent/``) and its ``store.sqlite3`` file. It exposes:

* :meth:`ProjectStore.connection` — the live :class:`sqlite3.Connection` for
  later storage workers (artifact repository, approvals, backup) to build on,
  without re-implementing path/identity/migration handling;
* :meth:`ProjectStore.transaction` — an explicit ``BEGIN IMMEDIATE`` transaction
  context manager;
* :attr:`ProjectStore.project_id` — the stable identity recorded **inside** the
  store, so a registry location can be checked against the store it points at;
* :meth:`ProjectStore.metadata` — the recorded application metadata.

Opening a store **verifies the existing database read-only first** — schema
version and recorded project identity — and only after that passes does it open
a writable connection, apply pragmas/migrations, and narrow permissions. A
foreign SQLite file, an invalid identity, a newer schema, or an identity that
does not match ``expected_project_id`` are all rejected *before any write*, so a
rejected open never changes the target's ``journal_mode`` or creates tables in
it.

The store directory holds no user assets in Step 02, so it is safe for a future
consistent backup to snapshot the SQLite database (with WAL sidecars) rather
than racing an external file tree; immutable content will be stored as SQLite
BLOBs by the artifact worker.
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator
from urllib.parse import quote

from .. import __version__
from ..domain.errors import (
    InvalidProjectId,
    ProjectPathError,
    StoreError,
    UnsupportedSchemaVersion,
)
from . import paths, schema


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Run a block in a single ``BEGIN IMMEDIATE`` transaction.

    Commits on clean exit and rolls back on any exception. Nested use is not
    supported (SQLite has no nested transactions); the caller owns the unit of
    work.
    """
    try:
        conn.execute("BEGIN IMMEDIATE")
    except sqlite3.Error as exc:  # pragma: no cover - environment dependent
        raise StoreError("could not begin a database transaction.") from exc
    try:
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:  # pragma: no cover - best effort
            pass
        raise


class _ReadonlyIdentity:
    __slots__ = ("schema_version", "project_id", "app_version")

    def __init__(self, schema_version: int, project_id: str, app_version: str) -> None:
        self.schema_version = schema_version
        self.project_id = project_id
        self.app_version = app_version


def _read_identity_readonly(
    db_path: str, *, expected_project_id: str | None
) -> _ReadonlyIdentity:
    """Read schema version and project identity **read-only**.

    Uses a read-only URI (``mode=ro``) **exclusively** — there is deliberately
    no writable fallback. If the read-only open fails, this raises a clean
    :class:`StoreError` rather than silently reopening writable and risking
    creating or modifying a foreign file; a path that is absent stays absent.

    Honest scope of "read-only": SQLite may create read-only **shared-memory
    sidecars** (``-shm``/``-wal``) in the database's directory as part of WAL
    read access. These are not library-driven writes. This call never changes
    ``journal_mode``, never runs a migration, never ``CREATE``s a table, and
    never chmods the file — so a rejected target's database file is left
    byte-identical and its journal mode unchanged.

    Raises :class:`UnsupportedSchemaVersion` for a newer schema,
    :class:`ProjectPathError` for an ID mismatch against ``expected_project_id``,
    and :class:`StoreError` for a foreign, corrupt, or unreadable file.
    """
    read_only_uri = "file:" + quote(db_path) + "?mode=ro"
    try:
        conn = sqlite3.connect(read_only_uri, uri=True, isolation_level=None)
    except sqlite3.Error as exc:
        raise StoreError(
            f"could not open the local database at {db_path!r} read-only "
            f"({exc.__class__.__name__}); refusing to fall back to a writable "
            f"open."
        ) from exc
    conn.row_factory = sqlite3.Row
    try:
        version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        if version > schema.STORE_SCHEMA_VERSION:
            raise UnsupportedSchemaVersion(
                label="project store",
                found=version,
                supported=schema.STORE_SCHEMA_VERSION,
            )
        if version < 1:
            raise StoreError(
                f"the file at {db_path!r} is not a review_article_agent project "
                f"store (no schema version recorded)."
            )
        table = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'project'"
        ).fetchone()
        if table is None:
            raise StoreError(
                f"the file at {db_path!r} is not a review_article_agent project "
                f"store (no project identity table)."
            )
        rows = conn.execute(
            "SELECT project_id, app_version FROM project"
        ).fetchall()
        if len(rows) != 1:
            raise StoreError(
                f"the store at {db_path!r} does not contain exactly one project "
                f"identity row."
            )
        project_id = rows[0]["project_id"]
        try:
            paths.validate_project_id(project_id)
        except InvalidProjectId as exc:
            raise StoreError(
                f"the store at {db_path!r} records an invalid project identity; "
                f"refusing to use it."
            ) from exc
        if expected_project_id is not None and project_id != expected_project_id:
            raise ProjectPathError(
                f"the store at {db_path!r} belongs to project {project_id!r}, "
                f"not {expected_project_id!r}; refusing to read or write a "
                f"mismatched store."
            )
        return _ReadonlyIdentity(
            schema_version=version,
            project_id=project_id,
            app_version=rows[0]["app_version"] or __version__,
        )
    except sqlite3.DatabaseError as exc:
        raise StoreError(
            f"the file at {db_path!r} is not a readable review_article_agent "
            f"store."
        ) from exc
    finally:
        conn.close()


class ProjectStore:
    """A single project's SQLite store, bound to its app-owned directory."""

    STORE_FILENAME = paths.STORE_FILENAME

    def __init__(
        self,
        conn: sqlite3.Connection,
        app_dir: str,
        project_id: str,
        *,
        schema_version: int,
        app_version: str,
    ) -> None:
        self._conn = conn
        self._app_dir = app_dir
        self._project_id = project_id
        self._schema_version = schema_version
        self._app_version = app_version
        self._closed = False

    # -- identity / metadata ------------------------------------------------- #
    @property
    def project_id(self) -> str:
        """The stable project ID recorded inside this store."""
        return self._project_id

    @property
    def app_dir(self) -> str:
        """The app-owned directory holding this store."""
        return self._app_dir

    @property
    def db_path(self) -> str:
        return os.path.join(self._app_dir, self.STORE_FILENAME)

    @property
    def schema_version(self) -> int:
        return self._schema_version

    @property
    def app_version(self) -> str:
        return self._app_version

    @property
    def connection(self) -> sqlite3.Connection:
        """The live connection, for later storage workers."""
        self._assert_open()
        return self._conn

    def transaction(self) -> "contextmanager[Iterator[sqlite3.Connection]]":
        """Return a transaction context manager over this store's connection."""
        self._assert_open()
        return transaction(self._conn)

    def metadata(self) -> dict[str, str]:
        """Return recorded store metadata (includes ``project_id``)."""
        self._assert_open()
        rows = self._conn.execute("SELECT key, value FROM meta").fetchall()
        meta = {row["key"]: row["value"] for row in rows}
        meta["project_id"] = self._project_id
        meta["schema_version"] = str(self._schema_version)
        meta["app_version"] = self._app_version
        return meta

    # -- lifecycle ----------------------------------------------------------- #
    @classmethod
    def create_at(
        cls,
        app_dir: str,
        project_id: str,
        *,
        name: str | None = None,
        app_version: str = __version__,
        created_at_utc: str | None = None,
    ) -> "ProjectStore":
        """Create a brand-new store in a fresh app-owned directory.

        ``app_dir`` must not already exist; the caller decides the location
        (the registry creates it in a staging directory and renames into place
        only after success). Fails if a directory already exists so an
        unrelated directory or an orphaned store is never overwritten.
        """
        paths.validate_project_id(project_id)
        if os.path.lexists(app_dir):
            raise ProjectPathError(
                f"cannot create a store at {app_dir!r}: the path already exists."
            )
        paths.ensure_owner_private_dir(app_dir)
        db_path = os.path.join(app_dir, cls.STORE_FILENAME)
        created = _utc_now() if created_at_utc is None else created_at_utc
        conn = schema.open_connection(db_path)
        try:
            schema.migrate(
                conn,
                target_version=schema.STORE_SCHEMA_VERSION,
                migrations=schema.STORE_MIGRATIONS,
                label="project store",
            )
            with transaction(conn):
                conn.execute(
                    "INSERT INTO project "
                    "(project_id, name, created_at_utc, app_version) "
                    "VALUES (?, ?, ?, ?)",
                    (project_id, name, created, app_version),
                )
                conn.execute(
                    "INSERT INTO meta (key, value) VALUES (?, ?)",
                    ("created_at_utc", created),
                )
            paths.ensure_owner_private_file(db_path)
        except BaseException:
            conn.close()
            paths.remove_dir_best_effort(app_dir)
            raise
        return cls(
            conn,
            app_dir,
            project_id,
            schema_version=schema.STORE_SCHEMA_VERSION,
            app_version=app_version,
        )

    @classmethod
    def open_at(
        cls, app_dir: str, *, expected_project_id: str | None = None
    ) -> "ProjectStore":
        """Open an existing store, migrating it transactionally.

        Rejects a symlinked app-owned directory or store file (and sidecars) and
        a store whose recorded identity is missing or invalid. When
        ``expected_project_id`` is given, the recorded identity must match it.

        Identity and schema version are verified **read-only before any write**;
        only then is a writable connection opened, pragmas applied, and the
        migration run. A rejected open therefore leaves the target database
        untouched (its ``journal_mode`` and tables are unchanged).
        """
        if expected_project_id is not None:
            paths.validate_project_id(expected_project_id)
        paths.assert_app_dir_safe(app_dir)
        db_path = os.path.join(app_dir, cls.STORE_FILENAME)
        if not os.path.isfile(db_path):
            raise ProjectPathError(f"no project store found at {db_path!r}.")

        # Phase 1: read-only verification (no pragmas, no migration, no chmod).
        identity = _read_identity_readonly(
            db_path, expected_project_id=expected_project_id
        )

        # Phase 2: only now may we write.
        conn = schema.open_connection(db_path)
        try:
            version = schema.migrate(
                conn,
                target_version=schema.STORE_SCHEMA_VERSION,
                migrations=schema.STORE_MIGRATIONS,
                label="project store",
            )
            paths.ensure_owner_private_file(db_path)
        except BaseException:
            conn.close()
            raise
        return cls(
            conn,
            app_dir,
            identity.project_id,
            schema_version=version,
            app_version=identity.app_version,
        )

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            try:
                self._conn.close()
            except sqlite3.Error:  # pragma: no cover - best effort
                pass

    def _assert_open(self) -> None:
        if self._closed:
            raise StoreError("this project store is closed.")

    def __enter__(self) -> "ProjectStore":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
