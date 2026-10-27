"""Minimal atomic application-level registry over per-project stores.

The registry is the single source of truth for **which projects exist**, **where
their app-owned stores live**, and **which one is selected**. It is a small
SQLite database (``registry.sqlite3``) under an explicit data directory,
guarded by an OS file lock (``flock`` on ``registry.lock``) so concurrent
create/select/relocate attempts serialise — the accepted "minimal atomic global
registry/arbitration" (``ARCH-STORE``/``ARCH-ACTIVE-ENF``).

Guarantees upheld here:

* **Stable identity.** Project IDs are generated, never derived from paths or
  Git. A path rename is an *explicit* :meth:`Registry.relocate` that keeps the
  ID (or explicitly :meth:`Registry.register_restored` for an already-moved
  store) — never an implicit new project.
* **Verified location.** Before a selection or relocation reads/writes a store,
  the store's *internal* ``project_id`` is checked against the registry ID and
  the app-owned directory is checked for symlinks. A mismatch or a symlinked
  store is refused (step doc line 18).
* **Idempotent create.** Re-issuing the same create (same canonical location,
  same identity/name) returns the existing project; a divergent request raises
  :class:`~review_article_agent.domain.errors.ProjectConflict`.
* **No half-stores.** A store is built in a staging directory and renamed into
  place, and is *registered last*. Any failure removes the staged/renamed store
  and leaves no registered row — an unregistered orphan store is never
  implicitly attached.
* **One selection, not execution.** At most one active project is recorded.
  Selection is a bookkeeping choice: Step 02 runs nothing, so
  :meth:`Registry.status` always reports ``running = False``.
"""

from __future__ import annotations

import fcntl
import os
import shutil
import sqlite3
import secrets
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterator

from .. import __version__
from ..domain.errors import (
    ProjectConflict,
    ProjectNotFound,
    ProjectPathError,
)
from . import paths, schema
from .store import ProjectStore, transaction


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class ProjectInfo:
    """Public projection of a registered project."""

    project_id: str
    name: str
    root_path: str
    created_at_utc: str
    store_path: str

    def as_dict(self) -> dict[str, str]:
        return {
            "project_id": self.project_id,
            "name": self.name,
            "root_path": self.root_path,
            "created_at_utc": self.created_at_utc,
            "store_path": self.store_path,
        }


class Registry:
    """Application-level registry of projects and the selected project."""

    def __init__(
        self, data_dir: str | os.PathLike[str] | None = None, *, app_version: str = __version__
    ) -> None:
        self._data_dir = (
            paths.default_data_dir() if data_dir is None else paths.resolve_root(data_dir)
        )
        self._app_version = app_version

    # -- locations ----------------------------------------------------------- #
    @property
    def data_dir(self) -> str:
        return self._data_dir

    @property
    def registry_path(self) -> str:
        return os.path.join(self._data_dir, paths.REGISTRY_FILENAME)

    @property
    def lock_path(self) -> str:
        return os.path.join(self._data_dir, paths.REGISTRY_LOCK_FILENAME)

    # -- low-level session --------------------------------------------------- #
    def _ensure_data_dir(self) -> None:
        paths.ensure_owner_private_dir(self._data_dir)

    def _connect(self) -> sqlite3.Connection:
        conn = schema.open_connection(self.registry_path)
        try:
            schema.migrate(
                conn,
                target_version=schema.REGISTRY_SCHEMA_VERSION,
                migrations=schema.REGISTRY_MIGRATIONS,
                label="application registry",
            )
        except BaseException:
            conn.close()
            raise
        paths.ensure_owner_private_file(self.registry_path)
        return conn

    @contextmanager
    def _session(self) -> Iterator[sqlite3.Connection]:
        """Hold the exclusive registry lock and open a fresh connection.

        The lock serialises concurrent create/select/relocate across processes
        (this is what prevents two projects from being activated at once); the
        connection drives the atomic SQLite row updates.
        """
        self._ensure_data_dir()
        fd = os.open(self.lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            conn = self._connect()
            try:
                yield conn
            finally:
                conn.close()
        finally:
            os.close(fd)

    # -- queries ------------------------------------------------------------- #
    @staticmethod
    def _row_to_info(row: sqlite3.Row) -> ProjectInfo:
        return ProjectInfo(
            project_id=row["project_id"],
            name=row["name"],
            root_path=row["root_path"],
            created_at_utc=row["created_at_utc"],
            store_path=paths.store_path_for(row["root_path"]),
        )

    @staticmethod
    def _select_by_id(conn: sqlite3.Connection, project_id: str) -> sqlite3.Row | None:
        return conn.execute(
            "SELECT * FROM projects WHERE project_id = ?", (project_id,)
        ).fetchone()

    @staticmethod
    def _select_by_root(conn: sqlite3.Connection, root_path: str) -> sqlite3.Row | None:
        return conn.execute(
            "SELECT * FROM projects WHERE root_path = ?", (root_path,)
        ).fetchone()

    # -- create -------------------------------------------------------------- #
    def create_project(
        self,
        root: str | os.PathLike[str],
        *,
        name: str | None = None,
        project_id: str | None = None,
    ) -> ProjectInfo:
        """Create and register a new project at ``root``.

        Idempotent for the same canonical location and identity/name; a
        divergent request raises :class:`ProjectConflict`. On any failure no
        registered row and no leftover app-owned store remain.
        """
        root_path = paths.resolve_root(root)
        if project_id is not None:
            paths.validate_project_id(project_id)
        with self._session() as conn:
            existing = self._select_by_root(conn, root_path)
            if existing is not None:
                return self._resolve_idempotent(existing, name, project_id)

            if project_id is not None and self._select_by_id(conn, project_id) is not None:
                raise ProjectConflict(
                    f"project id {project_id!r} is already registered at a "
                    f"different location; choose a new id or relocate the "
                    f"existing project."
                )

            app_dir = paths.app_dir_for(root_path)
            if os.path.lexists(app_dir):
                raise ProjectPathError(
                    f"an app-owned directory already exists at {app_dir!r} but "
                    f"is not registered; run register_restored to adopt it "
                    f"explicitly, or remove it if it is stale."
                )

            pid = project_id or paths.new_project_id()
            resolved_name = name or os.path.basename(root_path.rstrip(os.sep)) or pid
            created_root = paths.ensure_directory(root_path)
            staging = os.path.join(
                root_path, paths.STAGING_PREFIX + secrets.token_hex(6)
            )
            renamed = False
            try:
                store = ProjectStore.create_at(
                    staging, pid, name=name, app_version=self._app_version
                )
                store.close()
                os.rename(staging, app_dir)
                renamed = True
                created_at = _utc_now()
                with transaction(conn):
                    self._insert_project_row(
                        conn, pid, resolved_name, root_path, created_at
                    )
            except BaseException:
                paths.remove_dir_best_effort(staging)
                if renamed:
                    paths.remove_dir_best_effort(app_dir)
                if created_root:
                    paths.remove_dir_if_empty(root_path)
                raise
            return ProjectInfo(
                pid, resolved_name, root_path, created_at, paths.store_path_for(root_path)
            )

    def _resolve_idempotent(
        self, existing: sqlite3.Row, name: str | None, project_id: str | None
    ) -> ProjectInfo:
        """Return an existing project for a repeated/duplicate create request."""
        if project_id is not None and project_id != existing["project_id"]:
            raise ProjectConflict(
                f"location {existing['root_path']!r} is already registered as "
                f"project {existing['project_id']!r}, not {project_id!r}."
            )
        if name is not None and name != existing["name"]:
            raise ProjectConflict(
                f"location {existing['root_path']!r} is already registered as "
                f"project {existing['project_id']!r} named {existing['name']!r}; "
                f"the requested name {name!r} conflicts."
            )
        return self._row_to_info(existing)

    def _insert_project_row(
        self,
        conn: sqlite3.Connection,
        project_id: str,
        name: str,
        root_path: str,
        created_at_utc: str,
    ) -> None:
        """Insert a registry row (separate method so failures are injectable)."""
        conn.execute(
            "INSERT INTO projects "
            "(project_id, name, root_path, created_at_utc) VALUES (?, ?, ?, ?)",
            (project_id, name, root_path, created_at_utc),
        )

    # -- register an already-moved store ------------------------------------- #
    def register_restored(
        self,
        root: str | os.PathLike[str],
        *,
        project_id: str | None = None,
        name: str | None = None,
    ) -> ProjectInfo:
        """Explicitly register an existing app-owned store at ``root``.

        Used when a store was moved/copied by the user or recovered from a
        backup. The store's internal ``project_id`` is authoritative; a
        caller-supplied ``project_id`` must match it. Never attaches a store
        implicitly.
        """
        root_path = paths.resolve_root(root)
        if project_id is not None:
            paths.validate_project_id(project_id)
        with self._session() as conn:
            existing = self._select_by_root(conn, root_path)
            if existing is not None:
                return self._resolve_idempotent(existing, name, project_id)

            app_dir = paths.app_dir_for(root_path)
            paths.assert_app_dir_safe(app_dir)
            if not os.path.isdir(app_dir):
                raise ProjectPathError(
                    f"cannot register {root_path!r}: no app-owned directory at "
                    f"{app_dir!r} to adopt."
                )
            store = ProjectStore.open_at(app_dir)
            try:
                store_id = store.project_id
                created_at = store.metadata().get("created_at_utc") or _utc_now()
            finally:
                store.close()
            if project_id is not None and project_id != store_id:
                raise ProjectPathError(
                    f"store at {app_dir!r} belongs to project {store_id!r}, not "
                    f"{project_id!r}; refusing to register a mismatched store."
                )
            if self._select_by_id(conn, store_id) is not None:
                raise ProjectConflict(
                    f"project {store_id!r} is already registered at another "
                    f"location; relocating the existing entry is required."
                )
            resolved_name = name or os.path.basename(root_path.rstrip(os.sep)) or store_id
            with transaction(conn):
                self._insert_project_row(
                    conn, store_id, resolved_name, root_path, created_at
                )
            return ProjectInfo(
                store_id,
                resolved_name,
                root_path,
                created_at,
                paths.store_path_for(root_path),
            )

    # -- relocate (explicit) ------------------------------------------------- #
    def relocate(
        self,
        project_id: str,
        new_root: str | os.PathLike[str],
        *,
        move: bool = False,
    ) -> ProjectInfo:
        """Explicitly re-register ``project_id`` at ``new_root``, keeping the ID.

        With ``move=False`` (default) the store is expected to have been moved
        already: the new location is verified to hold *this* project's store and
        a stale store left at the old location is refused. With ``move=True``
        only the app-owned ``.review_article_agent`` directory is moved (never
        any other file in either root).
        """
        paths.validate_project_id(project_id)
        new_root_path = paths.resolve_root(new_root)
        with self._session() as conn:
            row = self._select_by_id(conn, project_id)
            if row is None:
                raise ProjectNotFound(project_id)
            old_root = row["root_path"]
            if old_root == new_root_path:
                return self._row_to_info(row)

            if move:
                # Verify the source store's identity before touching it.
                self._verify_store_matches(old_root, project_id)
                self._move_app_dir(old_root, new_root_path)
            else:
                old_app = paths.app_dir_for(old_root)
                if os.path.isdir(old_app) or os.path.islink(old_app):
                    raise ProjectPathError(
                        f"old location {old_app!r} still holds an app-owned "
                        f"directory; refusing to relocate and leave a duplicate "
                        f"store. Remove it explicitly, or use move=True."
                    )
                self._verify_store_matches(new_root_path, project_id)

            with transaction(conn):
                conn.execute(
                    "UPDATE projects SET root_path = ? WHERE project_id = ?",
                    (new_root_path, project_id),
                )
            return ProjectInfo(
                project_id,
                row["name"],
                new_root_path,
                row["created_at_utc"],
                paths.store_path_for(new_root_path),
            )

    def _move_app_dir(self, old_root: str, new_root: str) -> None:
        old_app = paths.app_dir_for(old_root)
        new_app = paths.app_dir_for(new_root)
        if os.path.lexists(new_app):
            raise ProjectPathError(
                f"destination {new_app!r} already exists; refusing to overwrite."
            )
        paths.assert_app_dir_safe(old_app)
        if not os.path.isdir(old_app):
            raise ProjectPathError(
                f"source app-owned directory {old_app!r} does not exist."
            )
        created_root = paths.ensure_directory(new_root)
        try:
            shutil.move(old_app, new_app)
        except BaseException:
            if created_root:
                paths.remove_dir_if_empty(new_root)
            raise

    # -- selection ----------------------------------------------------------- #
    def select(self, project_id: str) -> ProjectInfo:
        """Select the one active project, after verifying its store."""
        paths.validate_project_id(project_id)
        with self._session() as conn:
            row = self._select_by_id(conn, project_id)
            if row is None:
                raise ProjectNotFound(project_id)
            self._verify_store_matches(row["root_path"], project_id)
            with transaction(conn):
                conn.execute(
                    "INSERT INTO active_project (singleton, project_id, selected_at_utc) "
                    "VALUES (1, ?, ?) "
                    "ON CONFLICT(singleton) DO UPDATE SET "
                    "project_id = excluded.project_id, "
                    "selected_at_utc = excluded.selected_at_utc",
                    (project_id, _utc_now()),
                )
            return self._row_to_info(row)

    def active(self) -> ProjectInfo | None:
        """Return the selected project, or ``None`` if none is selected."""
        with self._session() as conn:
            row = conn.execute(
                "SELECT p.* FROM active_project a "
                "JOIN projects p ON p.project_id = a.project_id "
                "WHERE a.singleton = 1"
            ).fetchone()
            return None if row is None else self._row_to_info(row)

    def active_project(self) -> ProjectInfo:
        """Return the selected project or raise :class:`NoActiveProject`."""
        from ..domain.errors import NoActiveProject

        info = self.active()
        if info is None:
            raise NoActiveProject()
        return info

    # -- listing / status ---------------------------------------------------- #
    def get(self, project_id: str) -> ProjectInfo:
        paths.validate_project_id(project_id)
        with self._session() as conn:
            row = self._select_by_id(conn, project_id)
            if row is None:
                raise ProjectNotFound(project_id)
            return self._row_to_info(row)

    def list_projects(self) -> list[ProjectInfo]:
        with self._session() as conn:
            rows = conn.execute(
                "SELECT * FROM projects ORDER BY created_at_utc, project_id"
            ).fetchall()
            return [self._row_to_info(row) for row in rows]

    def status(self) -> dict[str, object]:
        """Return a small, secret-free status view.

        ``running`` is always ``False`` in Step 02: a project being *selected*
        is distinct from it *executing*, and no worker runs in this step.
        """
        active = self.active()
        return {
            "active_project_id": None if active is None else active.project_id,
            "active_project": None if active is None else active.as_dict(),
            "running": False,
            "projects": [info.as_dict() for info in self.list_projects()],
        }

    # -- verification -------------------------------------------------------- #
    def _verify_store_matches(self, root_path: str, project_id: str) -> None:
        """Open the store at ``root_path`` and confirm its identity.

        Checks are performed *before* any selection/relocation write, and the
        connection is then closed. Raises :class:`ProjectPathError` on a
        missing store, a symlinked store, or an ID/location mismatch.
        """
        app_dir = paths.app_dir_for(root_path)
        paths.assert_app_dir_safe(app_dir)
        if not os.path.isdir(app_dir):
            raise ProjectPathError(
                f"project {project_id!r} is registered at {root_path!r} but its "
                f"app-owned directory {app_dir!r} is missing."
            )
        store = ProjectStore.open_at(app_dir)
        try:
            found = store.project_id
        finally:
            store.close()
        if found != project_id:
            raise ProjectPathError(
                f"store at {app_dir!r} belongs to project {found!r}, but the "
                f"registry maps {project_id!r} to that location; refusing to "
                f"read or write a mismatched store."
            )
