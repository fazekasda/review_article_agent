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

Opening a store applies pending migrations transactionally and refuses a newer
schema. The store directory holds no user assets in Step 02, so it is safe for a
future consistent backup to snapshot the SQLite database (with WAL sidecars)
rather than racing an external file tree; immutable content will be stored as
SQLite BLOBs by the artifact worker.
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator

from .. import __version__
from ..domain.errors import ProjectPathError, StoreError
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
    def open_at(cls, app_dir: str) -> "ProjectStore":
        """Open an existing store, migrating it transactionally.

        Rejects a symlinked app-owned directory or store file and a store whose
        recorded identity is missing.
        """
        paths.assert_app_dir_safe(app_dir)
        db_path = os.path.join(app_dir, cls.STORE_FILENAME)
        if not os.path.isfile(db_path):
            raise ProjectPathError(
                f"no project store found at {db_path!r}."
            )
        conn = schema.open_connection(db_path)
        try:
            version = schema.migrate(
                conn,
                target_version=schema.STORE_SCHEMA_VERSION,
                migrations=schema.STORE_MIGRATIONS,
                label="project store",
            )
            row = conn.execute(
                "SELECT project_id, app_version FROM project LIMIT 2"
            ).fetchall()
            if len(row) != 1:
                raise StoreError(
                    f"store at {db_path!r} does not contain exactly one project "
                    f"identity row."
                )
            project_id = row[0]["project_id"]
            app_version = row[0]["app_version"] or __version__
            paths.ensure_owner_private_file(db_path)
        except BaseException:
            conn.close()
            raise
        return cls(
            conn,
            app_dir,
            project_id,
            schema_version=version,
            app_version=app_version,
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
