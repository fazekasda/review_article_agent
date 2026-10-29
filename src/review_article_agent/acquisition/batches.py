"""Atomic, restart-safe explicit folder-batch import (Step 06 slice F2).

Authority: ``docs/implementation/06-acquisition.md`` D2 (folder import) AC3/AC4/
AC7 and the workflow-10 manual import path. Combines
:func:`~review_article_agent.acquisition.folders.read_folder_batch` (bounded,
no-follow, explicit-manifest reads) with
:class:`~review_article_agent.acquisition.imports.AssetImportRepository`.

Entry schema (JSON-safe mapping; exact keys, nothing inferred)
---------------------------------------------------------------

Required: ``relative_path``, ``artifact_id``, ``origin_locator``, ``rights``,
``revision_id``, ``request_id``, ``occurrence_rights``,
``occurrence_revision_id``. Optional: ``parent_revision_id`` (string or null),
``dependencies`` (list of explicit relationship mappings). ``rights`` and
``occurrence_rights`` are inline, exact canonical RightsRecord mappings. File
names are never turned into IDs, titles, rights or relationships, and files are
never merged into a document by name.

Guarantees
----------

* Shape, key set, JSON-safety and unique ``request_id`` values are validated
  before any file is opened; every file is then read and bounded
  (``read_folder_batch``) before **any** database write.
* All imports run inside one outer ``BEGIN IMMEDIATE`` transaction; the
  importer's own transactions nest as savepoints (``_NestedStore``, an accepted
  package-internal coupling). Any failure (bad source or occurrence rights,
  conflict, stale parent) rolls back the whole batch.
* An exact replay of the full batch returns the historical results and writes
  nothing; changed bytes under an existing ``request_id`` is a conflict and
  rolls the batch back. Equal bytes at a different path in the same document
  reuse the source revision with a separate occurrence.
* Two different byte contents for one document inside a batch need an explicit
  ``parent_revision_id`` on the later entry: no silent version merge.
* The result is a proposal summary; it never bypasses seed, screening, pilot or
  acquisition gates and always sets ``authorizes_scientific_processing=False``.

Errors carry static text and an integer ``index`` only; never paths, rights
content or caller metadata.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Mapping

from .folders import (
    DEFAULT_MAX_FILES,
    DEFAULT_MAX_TOTAL_BYTES,
    FolderInputError,
    read_folder_batch,
)
from .imports import (
    AssetImportConflictError,
    AssetImportError,
    AssetImportRepository,
    _NestedStore,
)

REQUIRED_KEYS = frozenset(
    {
        "relative_path", "artifact_id", "origin_locator", "rights", "revision_id",
        "request_id", "occurrence_rights", "occurrence_revision_id",
    }
)
OPTIONAL_KEYS = frozenset({"parent_revision_id", "dependencies"})
_MAX_DEPTH = 32

_MESSAGES = {
    "invalid_arguments": "invalid folder batch import arguments.",
    "invalid_entry": "a batch entry has an invalid shape.",
    "duplicate_request": "batch entries reuse a request_id.",
    "version_requires_parent": "a changed version of a document in the batch needs an explicit parent.",
    "folder_input": "the folder batch could not be read.",
    "import_failed": "a batch entry was rejected; the whole batch was rolled back.",
    "storage_failure": "the local database operation failed; the batch was rolled back.",
}


class BatchImportError(ValueError):
    """Static-text failure; ``code``/``index``/``cause_code`` are safe fixed values."""

    def __init__(self, code: str, *, index: int | None = None, cause_code: str | None = None):
        self.code = code
        self.index = index
        self.cause_code = cause_code
        super().__init__(_MESSAGES[code])


class BatchImportConflictError(BatchImportError):
    """The batch conflicts with already-recorded immutable state."""


def _json_safe(value: Any, depth: int = 0) -> bool:
    if depth > _MAX_DEPTH:
        return False
    if value is None or isinstance(value, (str, bool, int)):
        return True
    if isinstance(value, float):
        return value == value and value not in (float("inf"), float("-inf"))
    if isinstance(value, (list, tuple)):
        return all(_json_safe(v, depth + 1) for v in value)
    if isinstance(value, dict):
        return all(isinstance(k, str) and _json_safe(v, depth + 1) for k, v in value.items())
    return False


def _validate_entries(entries: Any) -> None:
    if not isinstance(entries, (list, tuple)):
        raise BatchImportError("invalid_entry")
    seen: set[str] = set()
    for index, entry in enumerate(entries):
        if not isinstance(entry, Mapping) or not all(isinstance(k, str) for k in entry):
            raise BatchImportError("invalid_entry", index=index)
        keys = set(entry)
        if not REQUIRED_KEYS <= keys or keys - REQUIRED_KEYS - OPTIONAL_KEYS:
            raise BatchImportError("invalid_entry", index=index)
        for key in ("relative_path", "artifact_id", "origin_locator", "revision_id",
                    "request_id", "occurrence_revision_id"):
            if not isinstance(entry[key], str) or not entry[key]:
                raise BatchImportError("invalid_entry", index=index)
        for key in ("rights", "occurrence_rights"):
            if not isinstance(entry[key], dict):
                raise BatchImportError("invalid_entry", index=index)
        parent = entry.get("parent_revision_id")
        if parent is not None and (not isinstance(parent, str) or not parent):
            raise BatchImportError("invalid_entry", index=index)
        if not isinstance(entry.get("dependencies", []), (list, tuple)):
            raise BatchImportError("invalid_entry", index=index)
        if not _json_safe(dict(entry)):
            raise BatchImportError("invalid_entry", index=index)
        if entry["request_id"] in seen:
            raise BatchImportError("duplicate_request", index=index)
        seen.add(entry["request_id"])


def _check_versions(items: list[dict]) -> None:
    """Different bytes for one document in a batch need an explicit parent."""
    import hashlib

    seen: dict[str, set[str]] = {}
    for index, item in enumerate(items):
        digest = hashlib.sha256(item["content"]).hexdigest()
        known = seen.setdefault(item["artifact_id"], set())
        if known and digest not in known and item.get("parent_revision_id") is None:
            raise BatchImportError("version_requires_parent", index=index)
        known.add(digest)


def import_folder_batch(
    store,
    root,
    entries,
    *,
    max_files: int = DEFAULT_MAX_FILES,
    max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES,
) -> dict:
    """Import every explicit entry under ``root`` atomically or change nothing."""

    if not hasattr(store, "transaction") or not hasattr(store, "connection"):
        raise BatchImportError("invalid_arguments")
    _validate_entries(entries)
    try:
        items = read_folder_batch(
            root, entries, max_files=max_files, max_total_bytes=max_total_bytes
        )
    except FolderInputError as exc:
        raise BatchImportError("folder_input", cause_code=exc.code) from None
    _check_versions(items)
    if store.connection.in_transaction:
        raise BatchImportError("invalid_arguments")

    importer = AssetImportRepository(_NestedStore(store))
    results: list[dict] = []
    index = -1
    try:
        with store.transaction():
            for index, item in enumerate(items):
                kwargs = {k: v for k, v in item.items() if k != "relative_path"}
                result = importer.import_asset(**kwargs)
                results.append(
                    {
                        "index": index,
                        "request_id": item["request_id"],
                        "artifact_id": item["artifact_id"],
                        **result,
                    }
                )
    except AssetImportConflictError as exc:
        raise BatchImportConflictError(
            "import_failed", index=index, cause_code=exc.code
        ) from None
    except AssetImportError as exc:
        raise BatchImportError("import_failed", index=index, cause_code=exc.code) from None
    except sqlite3.Error:
        raise BatchImportError("storage_failure", index=index) from None

    dispositions: dict[str, int] = {}
    for r in results:
        dispositions[r["disposition"]] = dispositions.get(r["disposition"], 0) + 1
    return {
        "proposal_only": True,
        "authorizes_scientific_processing": False,
        "counts": {
            "entries": len(results),
            "replayed": sum(1 for r in results if r["replayed"]),
            "total_bytes": sum(r["byte_size"] or 0 for r in results),
            "dispositions": dispositions,
        },
        "items": results,
    }


__all__ = [
    "BatchImportConflictError",
    "BatchImportError",
    "import_folder_batch",
]
