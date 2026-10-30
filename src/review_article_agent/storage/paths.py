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

* Project IDs are generated (``uuid4``) and validated against a fixed pattern
  (``fullmatch``; the pattern contains no version-specific escapes) before the
  value reaches a query or a path.
* App-owned directories and database files are created **and validated**
  owner-private (``0o700`` directory, ``0o600`` file). A pre-existing directory
  or file that is group/world accessible is refused rather than silently used.
* Symlinked app-owned directories, database files, their ``-wal``/``-shm``
  sidecars, and the registry lock are rejected, so a lookup can never be
  redirected to another project's (or a foreign) SQLite file.
"""

from __future__ import annotations

import os
import re
import uuid

from ..domain.errors import InvalidProjectId, OwnerPrivateError, ProjectPathError

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

#: Pattern for a valid, application-generated project ID. Matched with
#: ``fullmatch`` (no ``\\z``/``\\A`` escapes, which differ across Python
#: versions); this keeps the declared ``>=3.11`` requirement honest.
PROJECT_ID_PATTERN = re.compile(r"prj_[0-9a-f]{32}")

#: Human-readable description used in error messages.
PROJECT_ID_HINT = "'prj_' followed by 32 lowercase hex characters"

#: Owner-private permission mask: no group/other access.
_GROUP_OTHER_BITS = 0o077


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
    if not isinstance(project_id, str) or PROJECT_ID_PATTERN.fullmatch(project_id) is None:
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


def sidecar_paths(db_path: str) -> tuple[str, str, str]:
    """Return ``(db, db-wal, db-shm)`` for a SQLite database path."""
    return (db_path, db_path + "-wal", db_path + "-shm")


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


# --------------------------------------------------------------------------- #
# owner-private policy
# --------------------------------------------------------------------------- #
def is_owner_private(mode: int) -> bool:
    """True when a file mode grants no group/other access."""
    return (mode & _GROUP_OTHER_BITS) == 0


def assert_owner_private_dir(path: str, *, what: str = "app-owned directory") -> None:
    """Refuse a directory that is not owner-private (``0o700``-equivalent).

    The refusal is a :class:`OwnerPrivateError` whose message names the path, the
    mode it currently has, and the exact ``chmod`` remedy, so a user can fix it
    without guessing which directory is at fault.
    """
    try:
        st = os.stat(path)
    except OSError as exc:
        raise ProjectPathError(f"{what} {path!r} cannot be inspected.") from exc
    if not is_owner_private(st.st_mode):
        raise OwnerPrivateError(
            f"the {what} {path!r} must be private to its owner (mode 0700); it "
            f"is currently {st.st_mode & 0o777:04o}, so other users can read or "
            f"write it. Refusing to use a shared directory. "
            f"Run: chmod 700 {path}"
        )


def assert_owner_private_file(path: str, *, what: str = "database file") -> None:
    """Refuse a symlinked or non-owner-private database file."""
    if os.path.islink(path):
        raise ProjectPathError(f"{what} {path!r} is a symlink; refusing to use it.")
    try:
        st = os.stat(path)
    except OSError as exc:
        raise ProjectPathError(f"{what} {path!r} cannot be inspected.") from exc
    if not is_owner_private(st.st_mode):
        raise OwnerPrivateError(
            f"the {what} {path!r} must be private to its owner (mode 0600); it "
            f"is currently {st.st_mode & 0o777:04o}, so other users can read or "
            f"write it. Refusing to use it. Run: chmod 600 {path}"
        )


def assert_not_symlink(path: str, *, what: str) -> None:
    """Refuse a path that is a symlink (checked with ``lexists`` semantics)."""
    if os.path.islink(path):
        raise ProjectPathError(
            f"{what} {path!r} is a symlink, which could redirect to a foreign "
            f"file or another project's data; refusing to use it."
        )


def ensure_owner_private_dir(path: str, *, what: str = "app-owned directory") -> None:
    """Create ``path`` (and parents) if missing, owner-private (``0o700``).

    A pre-existing directory is **validated**, not silently accepted: if it is
    group/world accessible, or a symlink, this refuses to use it. This is only
    ever applied to app-owned directories (the store directory and the data
    directory), never to a user's project root (see :func:`ensure_directory`).

    ``what`` labels the directory in the refusal message (e.g. ``"data
    directory"``) so the user knows which directory to fix.
    """
    if os.path.islink(path):
        raise ProjectPathError(
            f"refusing to use app-owned directory {path!r}: it is a symlink."
        )
    if os.path.isdir(path):
        assert_owner_private_dir(path, what=what)
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
    """Restrict an existing database file we own to the owner (``0o600``).

    Refuses a symlink (so the registry/store never follows a symlink to another
    file) and only then narrows permissions.
    """
    if os.path.islink(path):
        raise ProjectPathError(
            f"refusing to use database file {path!r}: it is a symlink."
        )
    try:
        os.chmod(path, 0o600)
    except OSError:  # pragma: no cover - best effort
        pass


def ensure_directory(path: str) -> bool:
    """Create a plain (non-app-owned) directory if missing.

    Returns ``True`` when this call created the directory. This is used only for
    the caller-supplied project root / relocation destination, so it does **not**
    chmod or validate a pre-existing directory and never removes anything.
    """
    created = not os.path.isdir(path)
    os.makedirs(path, mode=0o700, exist_ok=True)
    return created


def assert_app_dir_safe(app_dir: str) -> None:
    """Reject an unsafe app-owned directory, store file, or sidecar.

    Refuses a symlinked app-owned directory (which would let a registry entry
    point at another project's assets), an app-owned directory that is not
    owner-private, and any symlinked ``store.sqlite3``/``-wal``/``-shm``.
    """
    assert_not_symlink(app_dir, what="app-owned directory")
    if os.path.isdir(app_dir):
        assert_owner_private_dir(app_dir)
    for side in sidecar_paths(os.path.join(app_dir, STORE_FILENAME)):
        assert_not_symlink(side, what="project store file")


def assert_registry_paths_safe(data_dir: str, registry_path: str, lock_path: str) -> None:
    """Reject symlinked registry database, sidecars, or lock file."""
    for side in sidecar_paths(registry_path):
        assert_not_symlink(side, what="registry database file")
    assert_not_symlink(lock_path, what="registry lock file")


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
