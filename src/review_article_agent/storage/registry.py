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
* **Selection is not execution.** ``active_project`` records at most one
  *selected* project — a UI/bookkeeping choice. Execution **activation** is a
  separate durable record (:class:`ExecutionActivation`, C02 §8.5): at most one
  project may be authorized to execute, identified by an ``activation_token``.
  :meth:`Registry.select` never transfers or grants execution, and
  :meth:`Registry.status` reports them as distinct fields. Persistent activation
  is not proof that a worker is *actually* running (that is not knowable from
  the registry alone), so ``running`` stays honest at ``False``.
* **Fenced execution.** :meth:`Registry.activate_execution` grants the token;
  :meth:`Registry.begin_drain` marks a quiesce; :meth:`Registry.release_execution`
  is the trusted engine operation, valid only after it has proven quiescence.
  A stale token can neither release activation nor authorize a local write
  (:meth:`Registry.execution_write_guard`), and an execution-active project
  cannot be relocated out from under an open worker.
"""

from __future__ import annotations

import fcntl
import os
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
    StoreError,
    UnsupportedSchemaVersion,
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


#: Valid execution-activation states (C02 §8.5). ``switching`` is intentionally
#: not persisted: a switch is expressed as ``draining`` the old project and then
#: releasing before ``active`` is set for the next one.
ACTIVATION_STATES = ("none", "active", "draining")


@dataclass(frozen=True)
class ExecutionActivation:
    """Durable execution activation (separate from the selected project).

    ``active_project_id`` and ``activation_token`` are non-``None`` iff
    ``state != 'none'`` (C02 §8.5).  ``as_dict`` deliberately omits the token:
    it is an engine capability, not status-surface data.
    """

    state: str
    active_project_id: str | None
    activation_token: str | None
    activated_at_utc: str | None
    updated_at_utc: str | None

    def as_dict(self) -> dict[str, str | None]:
        return {
            "state": self.state,
            "active_project_id": self.active_project_id,
            "activated_at_utc": self.activated_at_utc,
            "updated_at_utc": self.updated_at_utc,
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

    def _assert_registry_safe(self) -> None:
        paths.assert_registry_paths_safe(
            self._data_dir, self.registry_path, self.lock_path
        )

    def _connect(self) -> sqlite3.Connection:
        self._assert_registry_safe()
        # Classify an existing registry file read-only before writing to it, so
        # a foreign or newer file is never mutated with our pragmas/migrations.
        if os.path.exists(self.registry_path):
            version, tables = schema.read_only_probe(self.registry_path)
            if version > schema.REGISTRY_SCHEMA_VERSION:
                raise UnsupportedSchemaVersion(
                    label="application registry",
                    found=version,
                    supported=schema.REGISTRY_SCHEMA_VERSION,
                )
            if version >= 1 and "projects" not in tables:
                raise StoreError(
                    f"the file at {self.registry_path!r} is not a "
                    f"review_article_agent registry; refusing to use it."
                )
            if version == 0 and tables:
                raise StoreError(
                    f"the file at {self.registry_path!r} is a foreign database; "
                    f"refusing to initialise a registry in it."
                )
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
        self._assert_registry_safe()
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(self.lock_path, os.O_CREAT | os.O_RDWR | nofollow, 0o600)
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
        # An idempotent short-circuit must not return a row whose store is
        # missing, symlinked, or has drifted to another identity.
        self._verify_store_matches(existing["root_path"], existing["project_id"])
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
            # An execution-active project must not be moved out from under an
            # open worker: a worker holding the activation could keep writing to
            # the old (now wrong) path. Release execution first.
            current = self._read_activation(conn)
            if current.state != "none" and current.active_project_id == project_id:
                raise ProjectConflict(
                    f"project {project_id!r} is execution-{current.state}; "
                    f"refusing to relocate it while its execution activation is "
                    f"held. Drain and release execution before relocating."
                )
            old_root = row["root_path"]
            if old_root == new_root_path:
                # A no-op relocation still re-validates the registration.
                self._verify_store_matches(old_root, project_id)
                return self._row_to_info(row)

            moved = False
            if move:
                # Verify the source store's identity read-only before touching it.
                self._verify_store_matches(old_root, project_id)
                self._move_app_dir(old_root, new_root_path)
                moved = True
            else:
                old_app = paths.app_dir_for(old_root)
                if os.path.isdir(old_app) or os.path.islink(old_app):
                    raise ProjectPathError(
                        f"old location {old_app!r} still holds an app-owned "
                        f"directory; refusing to relocate and leave a duplicate "
                        f"store. Remove it explicitly, or use move=True."
                    )
                self._verify_store_matches(new_root_path, project_id)

            try:
                with transaction(conn):
                    self._update_project_root(conn, project_id, new_root_path)
            except BaseException:
                if moved:
                    # The registry still points at old_root: undo the move so the
                    # recorded location is never a missing store.
                    self._undo_move(new_root_path, old_root)
                raise
            return ProjectInfo(
                project_id,
                row["name"],
                new_root_path,
                row["created_at_utc"],
                paths.store_path_for(new_root_path),
            )

    def _update_project_root(
        self, conn: sqlite3.Connection, project_id: str, new_root_path: str
    ) -> None:
        """Update a project's registered location (injectable for tests)."""
        conn.execute(
            "UPDATE projects SET root_path = ? WHERE project_id = ?",
            (new_root_path, project_id),
        )

    @staticmethod
    def _same_device(a: str, b: str) -> bool:
        """True when two existing paths live on the same filesystem."""
        return os.stat(a).st_dev == os.stat(b).st_dev

    def _move_app_dir(self, old_root: str, new_root: str) -> None:
        """Move only the app-owned directory, atomically, same filesystem.

        A cross-device move is refused: ``shutil.move`` would fall back to a
        copy-and-delete of a live WAL database, which is unsafe for a consistent
        store. The caller must relocate to a path on the same filesystem (or use
        a future backup/restore path) instead.
        """
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
            if not self._same_device(old_app, new_root):
                raise ProjectPathError(
                    f"cannot move {old_app!r} to {new_root!r}: the destination is "
                    f"on a different filesystem. A live store is not copied "
                    f"across devices (that would copy a WAL database unsafely); "
                    f"relocate to the same filesystem or restore from a backup."
                )
            os.rename(old_app, new_app)
        except BaseException:
            if created_root:
                paths.remove_dir_if_empty(new_root)
            raise

    def _undo_move(self, from_root: str, to_root: str) -> None:
        """Best-effort reverse of :meth:`_move_app_dir` after a failed update."""
        from_app = paths.app_dir_for(from_root)
        to_app = paths.app_dir_for(to_root)
        try:
            if os.path.lexists(from_app) and not os.path.lexists(to_app):
                os.rename(from_app, to_app)
        except OSError as exc:
            raise StoreError(
                f"the registry update failed and the store at {from_app!r} could "
                f"not be moved back to {to_app!r}; it remains at {from_app!r} while "
                f"the registry still records {to_root!r}. Recover by moving it "
                f"back or re-registering explicitly."
            ) from exc

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

    # -- execution activation (separate from selection) ---------------------- #
    @staticmethod
    def _read_activation(conn: sqlite3.Connection) -> ExecutionActivation:
        row = conn.execute(
            "SELECT * FROM execution_activation WHERE singleton = 1"
        ).fetchone()
        if row is None:
            # Migration 2 seeds exactly this row. Its absence is corruption, and
            # reporting a fabricated ``none`` would be fail-open: it would let a
            # new dispatch activate (overwriting the durable record) or a stale
            # engine treat the boundary as free. Refuse and surface a curated
            # error instead (C02 §8.5; step 03 AC4).
            raise StoreError(
                "the application registry is missing its execution-activation "
                "record (the 'execution_activation' singleton row); refusing to "
                "guess an activation state. Repair the registry (or restore it "
                "from backup) before running any worker."
            )
        return ExecutionActivation(
            state=row["state"],
            active_project_id=row["active_project_id"],
            activation_token=row["activation_token"],
            activated_at_utc=row["activated_at_utc"],
            updated_at_utc=row["updated_at_utc"],
        )

    def activation(self) -> ExecutionActivation:
        """Return the durable execution activation (independent of selection)."""
        with self._session() as conn:
            return self._read_activation(conn)

    def activate_execution(self, project_id: str) -> ExecutionActivation:
        """Grant execution to ``project_id`` and return its activation/token.

        At most one project may hold execution (C02 §8.5).  Re-activating the
        *same* project while it is already ``active`` is idempotent and returns
        the existing token, so engine recovery reuses one activation until a
        safe drain (no token is wiped for age/PID).  Activating any project
        while the record is ``active``/``draining`` for another project is
        refused: the current activation must be drained and released first.
        """
        paths.validate_project_id(project_id)
        with self._session() as conn:
            row = self._select_by_id(conn, project_id)
            if row is None:
                raise ProjectNotFound(project_id)
            current = self._read_activation(conn)
            if current.state == "active" and current.active_project_id == project_id:
                return current
            if current.state != "none":
                raise ProjectConflict(
                    f"cannot activate execution for {project_id!r}: the registry "
                    f"is execution-{current.state} for project "
                    f"{current.active_project_id!r}. Drain and release the "
                    f"current activation before activating another."
                )
            self._verify_store_matches(row["root_path"], project_id)
            token = "act_" + secrets.token_hex(16)
            now = _utc_now()
            with transaction(conn):
                cursor = conn.execute(
                    "UPDATE execution_activation SET state = 'active', "
                    "active_project_id = ?, activation_token = ?, "
                    "activated_at_utc = ?, updated_at_utc = ? WHERE singleton = 1",
                    (project_id, token, now, now),
                )
                # The write must have durably landed before a token is returned:
                # a zero-row update (row missing/removed) must never yield a
                # usable token that could authorize remote dispatch without a
                # persisted activation.
                self._assert_activation_written(cursor, "activate")
            return ExecutionActivation("active", project_id, token, now, now)

    def begin_drain(self, token: str) -> ExecutionActivation:
        """Mark the current activation ``draining`` (token-fenced, idempotent).

        The engine calls this to record intent to quiesce in-flight work; the
        activation is *not* released here.  A stale/unknown token is refused.
        """
        with self._session() as conn:
            current = self._read_activation(conn)
            if current.state == "none":
                raise ProjectConflict("there is no execution activation to drain.")
            if not secrets.compare_digest(current.activation_token or "", token or ""):
                raise ProjectConflict(
                    f"stale or unknown activation token; refusing to drain "
                    f"execution for project {current.active_project_id!r}."
                )
            if current.state == "draining":
                return current
            now = _utc_now()
            with transaction(conn):
                cursor = conn.execute(
                    "UPDATE execution_activation SET state = 'draining', "
                    "updated_at_utc = ? WHERE singleton = 1",
                    (now,),
                )
                self._assert_activation_written(cursor, "drain")
            return ExecutionActivation(
                "draining",
                current.active_project_id,
                current.activation_token,
                current.activated_at_utc,
                now,
            )

    def release_execution(self, token: str) -> ExecutionActivation:
        """Release execution to ``none`` — trusted, token-fenced engine op.

        Must only be called once the engine has **proven** the old project's
        in-flight admitted work is reconciled/quiesced (C02 §8.5): the registry
        cannot observe remote side effects.  A stale/unknown token is refused,
        so a previous activation cannot clear or hijack the current one.
        """
        with self._session() as conn:
            current = self._read_activation(conn)
            if current.state == "none":
                raise ProjectConflict("there is no execution activation to release.")
            if not secrets.compare_digest(current.activation_token or "", token or ""):
                raise ProjectConflict(
                    f"stale or unknown activation token; refusing to release "
                    f"execution for project {current.active_project_id!r}."
                )
            now = _utc_now()
            with transaction(conn):
                cursor = conn.execute(
                    "UPDATE execution_activation SET state = 'none', "
                    "active_project_id = NULL, activation_token = NULL, "
                    "activated_at_utc = NULL, updated_at_utc = ? WHERE singleton = 1",
                    (now,),
                )
                self._assert_activation_written(cursor, "release")
            return ExecutionActivation("none", None, None, None, now)

    @staticmethod
    def _assert_activation_written(cursor: sqlite3.Cursor, action: str) -> None:
        """Fail the transaction if an activation UPDATE changed no row.

        A missing singleton row must never silently succeed: a fabricated
        token/state would be fail-open. Raising inside the ``transaction`` block
        rolls the whole update back.
        """
        if cursor.rowcount != 1:
            raise StoreError(
                f"could not {action} execution activation: the registry's "
                f"execution-activation record is missing or could not be "
                f"updated; no durable activation was written. Repair the "
                f"registry before running any worker."
            )

    @contextmanager
    def execution_write_guard(
        self, token: str, project_id: str, *, allow_draining: bool = False
    ) -> Iterator[ExecutionActivation]:
        """Authorize one accepted per-project write under the registry lock.

        Holds the exclusive registry lock for the duration of the block while
        confirming that execution belongs to ``project_id`` **and** the token
        matches.  Because ``begin_drain``/``release_execution``/``select`` take
        the same lock, a release or switch cannot interleave a local commit:
        either the write is fenced-in first or the release wins and the write is
        refused.  Lock order is registry before project store (``_session``
        first, then the caller opens the store).  The engine must *not* hold this
        guard across external (remote) work — only across the local commit.

        ``allow_draining`` defaults to ``False`` (active only), which is what a
        **new dispatch** must use: once execution is ``draining`` no new work may
        be dispatched.  ``allow_draining=True`` is **reconciliation-only**: it
        additionally permits the *matching* still-draining project/token to
        finish committing writes it had already accepted *before* the drain, so a
        safe switch can complete without stranding an accepted local result.
        Never use ``allow_draining=True`` to dispatch new work (C02 §8.3/§8.5;
        step 03 AC4/AC5).
        """
        paths.validate_project_id(project_id)
        with self._session() as conn:
            current = self._read_activation(conn)
            permitted = ("active", "draining") if allow_draining else ("active",)
            if current.state not in permitted or current.active_project_id != project_id:
                expected = "execution-active" if not allow_draining else "active/draining"
                raise ProjectConflict(
                    f"refusing to authorize a write for {project_id!r}: it is "
                    f"not the {expected} project (state {current.state!r}, "
                    f"active {current.active_project_id!r})."
                )
            if not secrets.compare_digest(current.activation_token or "", token or ""):
                raise ProjectConflict(
                    f"stale or unknown activation token; refusing to authorize "
                    f"a write for project {project_id!r}."
                )
            yield current

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

        ``active_project_id`` is the *selected* project (a UI/bookkeeping
        choice).  ``execution`` is the separate durable execution activation
        (its token is never exposed here).  ``running`` stays ``False``: whether
        a worker is *actually* running cannot be known from persistent
        activation alone, so this view does not claim it.
        """
        active = self.active()
        execution = self.activation()
        return {
            "active_project_id": None if active is None else active.project_id,
            "active_project": None if active is None else active.as_dict(),
            "execution": execution.as_dict(),
            "running": False,
            "projects": [info.as_dict() for info in self.list_projects()],
        }

    # -- verification -------------------------------------------------------- #
    def _verify_store_matches(self, root_path: str, project_id: str) -> None:
        """Confirm the store at ``root_path`` holds ``project_id``.

        The identity is verified **read-only inside** :meth:`ProjectStore.open_at`
        before any pragma/migration/chmod, and the store is closed immediately.
        Raises :class:`ProjectPathError` on a missing store, an unsafe store, or
        an ID/location mismatch — never after a foreign write.
        """
        app_dir = paths.app_dir_for(root_path)
        paths.assert_app_dir_safe(app_dir)
        if not os.path.isdir(app_dir):
            raise ProjectPathError(
                f"project {project_id!r} is registered at {root_path!r} but its "
                f"app-owned directory {app_dir!r} is missing."
            )
        store = ProjectStore.open_at(app_dir, expected_project_id=project_id)
        store.close()
