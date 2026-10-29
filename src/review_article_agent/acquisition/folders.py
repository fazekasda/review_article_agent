"""Bounded local folder-batch reader (Step 06 slice F1).

Authority: ``docs/implementation/06-acquisition.md`` D2 (folder import), AC4
(imports never bypass screening or seed handling) and AC6 (manual, unofficial
import only).

What it does
------------

:func:`read_folder_batch` reads an **explicit manifest** of regular files below
one root directory and returns copies of the caller's entry mappings, each with
an added ``content`` (``bytes``) key.  The whole manifest is validated before
any file is opened and **no result is returned unless every read succeeded**, so
a caller can preflight a batch before any storage write.

What it is not
--------------

* No directory listing or globbing: only the paths the caller names are read.
* No identity inference: file names are never turned into titles, DOIs, study
  identities or merges.  Every other key of an entry is opaque caller import
  metadata, deep-copied and never interpreted.
* No screening, seed or scientific decision; no storage, network or filesystem
  write.

Safety model
------------

* Paths are relative, ``/``-separated, with no empty, ``.`` or ``..``
  components, no control characters, surrogates or backslashes, and no
  ``.git`` component.  Duplicate paths are rejected.
* The root is opened by walking its own components (from ``/`` for absolute
  roots, from the current directory for relative ones) and every entry path
  component is opened relative to an already-open directory descriptor, all
  with ``O_NOFOLLOW`` (and ``O_DIRECTORY`` for directories).  Symlinks anywhere
  in the root chain, in any component or at the final name are refused, as is
  ``..`` in the root.  Consequence: a root under a symlinked ancestor (for
  example a symlinked ``/tmp``) is refused; pass a real path.  The current
  directory itself is trusted for relative roots.  The final file is opened ``O_NONBLOCK`` (a FIFO cannot hang the
  open) and must then be a regular file by ``fstat``.
* Per-file size is capped (32 MiB) and the batch total is capped (default
  64 MiB, hard ceiling 256 MiB); over-limit input is refused, never truncated.
* ``fstat`` before and after reading must agree (size, mtime, inode, device)
  and the byte count must match, otherwise the file is reported as changed.
* Accessible regular files are accepted regardless of link count or owner:
  hard links are indistinguishable from the file itself and no ownership
  check is made.

Limitations: this is not a sandbox.  Descriptor anchoring only prevents
symlink/path swaps from redirecting a read outside the root; a process running
as the same user can still modify the tree concurrently, and the changed-file
check is best effort, not a guarantee.

Errors are :class:`FolderInputError` with a static ``code`` and message; caller
paths and metadata never appear in error text.
"""

from __future__ import annotations

import copy
import errno
import os
import stat
from typing import Any, Mapping, Sequence

MAX_FILE_BYTES = 32 * 1024 * 1024
DEFAULT_MAX_FILES = 1000
DEFAULT_MAX_TOTAL_BYTES = 64 * 1024 * 1024
MAX_TOTAL_BYTES_CEILING = 256 * 1024 * 1024
MAX_FILES_CEILING = 100000
MAX_PATH_BYTES = 4096
MAX_COMPONENT_BYTES = 255
_CHUNK = 1024 * 1024

_MESSAGES = {
    "invalid_arguments": "invalid folder batch arguments",
    "invalid_manifest": "invalid folder batch manifest",
    "invalid_path": "invalid relative path in manifest",
    "duplicate_path": "duplicate relative path in manifest",
    "too_many_files": "folder batch has too many files",
    "root_unavailable": "folder batch root is unavailable or unsafe",
    "not_found": "a manifest file was not found",
    "unsafe_path": "a manifest path is unsafe or not a directory chain",
    "not_regular": "a manifest path is not a regular file",
    "file_too_large": "a manifest file exceeds the per-file limit",
    "total_too_large": "folder batch exceeds the total size limit",
    "changed_during_read": "a manifest file changed while being read",
    "io_error": "a manifest file could not be read",
}


class FolderInputError(ValueError):
    """Typed, static-text failure; ``code`` is one of a fixed set."""

    def __init__(self, code: str) -> None:
        self.code = code if code in _MESSAGES else "io_error"
        super().__init__(_MESSAGES[self.code])


def _validate_path(value: Any) -> tuple[str, ...]:
    if not isinstance(value, str) or not value:
        raise FolderInputError("invalid_path")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        raise FolderInputError("invalid_path") from None
    if len(encoded) > MAX_PATH_BYTES:
        raise FolderInputError("invalid_path")
    for ch in value:
        if ord(ch) < 0x20 or ord(ch) == 0x7F or ch == "\\":
            raise FolderInputError("invalid_path")
        if 0x80 <= ord(ch) < 0xA0:
            raise FolderInputError("invalid_path")
    if value.startswith("/"):
        raise FolderInputError("invalid_path")
    parts = tuple(value.split("/"))
    for part in parts:
        if part in ("", ".", "..") or part.casefold() == ".git":
            raise FolderInputError("invalid_path")
        if len(part.encode("utf-8")) > MAX_COMPONENT_BYTES:
            raise FolderInputError("invalid_path")
    return parts


def _validate_manifest(
    entries: Any, max_files: Any, max_total_bytes: Any
) -> list[tuple[Mapping[str, Any], tuple[str, ...]]]:
    for limit in (max_files, max_total_bytes):
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise FolderInputError("invalid_arguments")
    if max_files > MAX_FILES_CEILING or max_total_bytes > MAX_TOTAL_BYTES_CEILING:
        raise FolderInputError("invalid_arguments")
    if isinstance(entries, (str, bytes, Mapping)) or not isinstance(entries, Sequence):
        raise FolderInputError("invalid_manifest")
    if len(entries) > max_files:
        raise FolderInputError("too_many_files")
    seen: set[tuple[str, ...]] = set()
    parsed: list[tuple[Mapping[str, Any], tuple[str, ...]]] = []
    for entry in entries:
        if not isinstance(entry, Mapping) or "relative_path" not in entry:
            raise FolderInputError("invalid_manifest")
        if "content" in entry:
            raise FolderInputError("invalid_manifest")
        if not all(isinstance(key, str) for key in entry):
            raise FolderInputError("invalid_manifest")
        parts = _validate_path(entry["relative_path"])
        if parts in seen:
            raise FolderInputError("duplicate_path")
        seen.add(parts)
        parsed.append((entry, parts))
    return parsed


_DIR_FLAGS = (
    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
)
_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK


def _map_open_error(exc: OSError) -> FolderInputError:
    if exc.errno == errno.ENOENT:
        return FolderInputError("not_found")
    if exc.errno in (errno.ELOOP, errno.ENOTDIR, errno.EACCES, errno.EPERM):
        return FolderInputError("unsafe_path")
    return FolderInputError("io_error")


def _signature(st: os.stat_result) -> tuple[int, int, int, int]:
    return (st.st_size, st.st_mtime_ns, st.st_ino, st.st_dev)


def _open_root(root_path: str) -> int:
    """Open ``root_path`` component by component without following symlinks.

    Absolute roots are walked from ``/``, relative roots from the current
    directory (which is itself trusted as-is).  ``..`` is refused.  Every
    intermediate descriptor is closed; the returned descriptor is the caller's.
    """

    if not isinstance(root_path, str) or not root_path or "\x00" in root_path:
        raise FolderInputError("invalid_arguments")
    try:
        root_path.encode("utf-8")
    except UnicodeEncodeError:
        raise FolderInputError("invalid_arguments") from None
    parts = [p for p in root_path.split("/") if p not in ("", ".")]
    if ".." in parts:
        raise FolderInputError("root_unavailable")
    try:
        current = os.open("/" if root_path.startswith("/") else ".", _DIR_FLAGS)
    except OSError:
        raise FolderInputError("root_unavailable") from None
    try:
        for part in parts:
            try:
                nxt = os.open(part, _DIR_FLAGS, dir_fd=current)
            except OSError:
                raise FolderInputError("root_unavailable") from None
            os.close(current)
            current = nxt
    except BaseException:
        os.close(current)
        raise
    return current


def _read_one(root_fd: int, parts: tuple[str, ...], budget: int) -> bytes:
    opened: list[int] = []
    try:
        current = root_fd
        for part in parts[:-1]:
            try:
                current = os.open(part, _DIR_FLAGS, dir_fd=current)
            except OSError as exc:
                raise _map_open_error(exc) from None
            opened.append(current)
        try:
            fd = os.open(parts[-1], _FILE_FLAGS, dir_fd=current)
        except OSError as exc:
            raise _map_open_error(exc) from None
        opened.append(fd)
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise FolderInputError("not_regular")
        if before.st_size > MAX_FILE_BYTES:
            raise FolderInputError("file_too_large")
        if before.st_size > budget:
            raise FolderInputError("total_too_large")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(fd, min(_CHUNK, before.st_size + 1 - total))
            if not chunk:
                break
            total += len(chunk)
            if total > before.st_size:
                raise FolderInputError("changed_during_read")
            chunks.append(chunk)
        after = os.fstat(fd)
        if total != before.st_size or _signature(before) != _signature(after):
            raise FolderInputError("changed_during_read")
        return b"".join(chunks)
    except FolderInputError:
        raise
    except OSError:
        raise FolderInputError("io_error") from None
    finally:
        for fd_ in reversed(opened):
            try:
                os.close(fd_)
            except OSError:
                pass


def read_folder_batch(
    root: Any,
    entries: Any,
    *,
    max_files: int = DEFAULT_MAX_FILES,
    max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES,
) -> list[dict[str, Any]]:
    """Read every manifest file under ``root`` or raise :class:`FolderInputError`.

    Returns one new dict per entry, in manifest order: a deep copy of the entry
    mapping plus ``content`` (``bytes``).  Nothing is returned on any failure.
    """

    parsed = _validate_manifest(entries, max_files, max_total_bytes)
    try:
        root_path = os.fspath(root)
    except TypeError:
        raise FolderInputError("invalid_arguments") from None
    if isinstance(root_path, bytes):
        root_path = os.fsdecode(root_path)
    root_fd = _open_root(root_path)
    results: list[dict[str, Any]] = []
    try:
        remaining = max_total_bytes
        for entry, parts in parsed:
            data = _read_one(root_fd, parts, remaining)
            remaining -= len(data)
            item = copy.deepcopy(dict(entry))
            item["content"] = data
            results.append(item)
    finally:
        os.close(root_fd)
    return results
