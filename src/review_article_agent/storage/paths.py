"""Filesystem layout, stable project IDs, and path-safety helpers.

Layout (per ``ARCH-STORE``):

* **Per-project store** lives in an app-owned directory ``<project_root>/.review_article_agent/``
  containing ``store.sqlite3`` (the project DB) plus SQLite ``-wal``/``-shm``
  sidecars. The store holds the project's own identity so a mismatch between a
  registry location and the store it points at is detected before any read or
  write.
* **Application registry** lives under an explicit *data directory* (defaulting
  to the XDG data home) as ``registry.sqlite3`` plus a ``registry.lock`` used
  for cross-process arbitration.

Safety rules enforced here:

* Project IDs are generated (``uuid4``) and validated against a fixed pattern;
  they are never derived from a path or Git and are never spliced into a path
  by this layer.
* App-owned directories and database files are created owner-private
  (``0o700`` directory, ``0o600`` file).
* Symlinked app-owned directories or store files are rejected; a project is
  only ever reached through its own app-owned directory, so an attacker cannot
  redirect a lookup to another project's assets.
"""

from __future__ import annotations

import os
import re
import uuid

from ..domain.errors import InvalidProjectId, ProjectPathError

#: App-owned directory name created inside a project root.
APP_DIR_NAME = ".review_article_agent"

#: Staging prefix used while a store is being created before it is registered.
STAGING_PREFIX = ".review_article_agent.staging-"

#: Project database filename inside the app-owned directory.
STORE_FILENAME = "store.sqlite3"

#: Registry database filename inside the data directory.
REGISTRY_FILENAME = "registry.sqlite3"

#: Registry lock filename inside the data directory.
REGISTRY_LOCK_FILENAME = "registry.lock"

#: Application namespace under the XDG data home.
APP_DATA_DIR_NAME = "review_article_agent"

#: Pattern for a valid, application-generated project ID.
PROJECT_ID_PATTERN = re.compile(r"\Aprj_[0-9a-f]{32}\z")

#: Human-readable description used in error messages.
PROJECT_ID_HINT = "'prj_' followed by 32 lowercase hex characters"


def new_project_id() -> str:
    """Return a fresh stable project ID, independent of paths or Git.

    A random UUID4 keeps the identity stable across renames/moves and prevents
    accidental collisions between unrelated projects.
    """
    return "prj_" + uuid.uuid4().hex


def validate_project_id(project_id: object) -> str:
    """Validate ``project_id`` and return it unchanged, else raise.

    Rejects non-strings, empty strings, and anything that does not match the
    generated shape — including path-like or traversal values — before the
    value reaches a query or a path.
    """
    if not isinstance(project_id, str) or not PROJECT_ID_PATTERN.match(project_id):
        raise InvalidProjectId(project_id, expected=PROJECT_ID_HINT)
    return project_id


def resolve_root(path: str | os.PathLike[str]) -> str:
    """Return a canonical absolute path for a project root.

    ``realpath`` collapses ``..`` segments and resolves any symlinks in the
    caller-supplied *root* so the same physical directory always maps to one
    registry location. It does **not** require the directory to exist yet.
    """
    raw = os.fspath(path)
    if not raw:
        raise ProjectPathError("a project root path is required.")
    return os.path.realpath(os.path.abspath(raw))


def app_dir_for(root: str) -> str:
    """Return the app-owned directory path for a resolved project root."""
    return os.path.join(root, APP_DIR_NAME)


def store_path_for(root: str) -> str:
    """Return the project database path for a resolved project root."""
    return os.path.join(app_dir_for(root), STORE_FILENAME)


def default_data_dir(env: dict[str, str] | None = None) -> str:
    """Resolve the default application data directory.

    Honours ``XDG_DATA_HOME`` when set, otherwise ``~/.local/share``; the
    application namespace is appended. Callers may override this entirely by
    passing an explicit ``data_dir`` to :class:`~review_article_agent.storage.registry.Registry`.
    """
    environ = os.environ if env is None else env
    xdg = environ.get("XDG_DATA_HOME")
    if xdg and xdg.strip():
        base = xdg
    else:
        base = os.path.join(os.path.expanduser("~"), ".local", "share")
    return os.path.join(os.path.abspath(base), APP_DATA_DIR_NAME)


def ensure_owner_private_dir(path: str) -> None:
    """Create ``path`` (and parents) if missing, owner-private (``0o700``)."""
    if os.path.islink(path):
        raise ProjectPathError(
            f"refusing to use app-owned directory {path!r}: it is a symlink."
        )
    if os.path.isdir(path):
        return
    if os.path.exists(path):
        raise ProjectPathError(
            f"cannot create app-owned directory {path!r}: a non-directory file "
            f"already exists at that path."
        )
    os.makedirs(path, mode=0o700, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:  # pragma: no cover - best effort on exotic filesystems
        pass


def ensure_owner_private_file(path: str) -> None:
    """Restrict an existing database file to the owner (``0o600``)."""
    try:
        os.chmod(path, 0o600)
    except OSError:  # pragma: no cover - best effort
        pass


def ensure_directory(path: str) -> bool:
    """Create a plain (non-app-owned) directory if missing.

    Returns ``True`` when this call created the directory. Does **not** chmod a
    pre-existing directory and never removes anything; used only for the
    caller-supplied project root.
    """
    created = not os.path.isdir(path)
    os.makedirs(path, mode=0o700, exist_ok=True)
    return created


def assert_app_dir_safe(app_dir: str) -> None:
    """Reject a symlinked app-owned directory or store file.

    ``app_dir`` is expected to be an existing path; a symlink here would let a
    registry entry point at another project's assets, so it is refused.
    """
    if os.path.islink(app_dir):
        raise ProjectPathError(
            f"refusing to use app-owned directory {app_dir!r}: it is a symlink, "
            f"which could redirect to another project's assets."
        )
    store = os.path.join(app_dir, STORE_FILENAME)
    if os.path.islink(store):
        raise ProjectPathError(
            f"refusing to use project store {store!r}: it is a symlink."
        )


def remove_dir_best_effort(path: str) -> None:
    """Remove a directory tree we created, ignoring errors.

    Only ever called on an app-owned staging/final directory this layer created;
    it must never be pointed at a user's arbitrary directory.
    """
    import shutil

    if os.path.islink(path):
        try:
            os.unlink(path)
        except OSError:  # pragma: no cover
            pass
        return
    shutil.rmtree(path, ignore_errors=True)


def remove_dir_if_empty(path: str) -> None:
    """``rmdir`` ``path`` only if it is empty; ignore any failure."""
    try:
        os.rmdir(path)
    except OSError:
        pass
