"""Atomic explicit folder-batch import command (Step 06, C3).

An addon to :mod:`review_article_agent.cli.acquisition` (the ``acquisition``
group). It owns only the **CLI surface** over
:func:`~review_article_agent.acquisition.batches.import_folder_batch`:

``acquisition folder import ROOT --entries-file PATH [--project ID]
[--max-files N] [--max-total-bytes N]``

* ``--entries-file`` is strict, bounded JSON (the C1 helpers): a list of exact
  per-entry mappings. Every source ``artifact_id`` / ``revision_id``,
  occurrence ``request_id`` / ``occurrence_revision_id``, ``origin_locator``,
  inline ``rights`` / ``occurrence_rights``, optional ``parent_revision_id`` and
  ``dependencies`` are caller input. File names never become scientific
  identity, rights, relationships or document membership, and nothing is
  approved: the result is a proposal and always carries
  ``authorizes_scientific_processing=False``.
* The whole batch is read and preflighted, then imported in one all-or-nothing
  library transaction; any failure changes nothing. An exact replay writes
  nothing.
* The default output has counts plus per-entry index, source/occurrence refs,
  static disposition, hash and size. It never echoes the root, relative paths,
  origin locators, rationale, rights content or raw bytes.

Errors reuse :class:`~review_article_agent.cli.acquisition.AcquisitionControlError`
with static text and an integer entry index and fixed cause code only.
"""

from __future__ import annotations

import argparse
from typing import Any, Callable

from ..acquisition import folders as _folders
from ..acquisition.batches import (
    BatchImportConflictError,
    BatchImportError,
    import_folder_batch,
)
from ..acquisition.folders import (
    DEFAULT_MAX_FILES,
    DEFAULT_MAX_TOTAL_BYTES,
    MAX_FILES_CEILING,
    MAX_TOTAL_BYTES_CEILING,
)
from ..domain.errors import StoreError
from .acquisition import (
    AcquisitionControlError,
    _CATEGORY_CONFLICT,
    _CATEGORY_STORAGE,
    _CATEGORY_VALIDATION,
    _IMPORT_ERRORS,
    _read_json_file,
    _resolve_project_id,
    _verified_store,
)

__all__ = ["build_folder_parsers", "dispatch_folder"]

_ENTRIES_SHAPE_MESSAGE = (
    "the entries input must be a JSON list of folder batch entries. Nothing was written."
)
_LIMIT_MESSAGE = (
    "a batch limit is out of range (--max-files and --max-total-bytes must be "
    "positive and within the supported ceilings). Nothing was written."
)
_ROOT_MESSAGE = "the folder root must be a non-empty path. Nothing was written."
_MESSAGES = {
    "invalid_arguments": "the folder batch arguments were rejected. Nothing was written.",
    "invalid_entry": "a batch entry has an invalid shape. Nothing was written.",
    "duplicate_request": "batch entries reuse a request_id. Nothing was written.",
    "version_requires_parent": (
        "a changed version of a document in the batch needs an explicit parent. "
        "Nothing was written."
    ),
    "folder_input": (
        "the folder batch could not be read (unsafe, missing or oversized input). "
        "Nothing was written."
    ),
    "import_failed": "a batch entry was rejected; the whole batch was rolled back.",
    "storage_failure": (
        "the local database operation failed; the whole batch was rolled back."
    ),
}
_CONFLICT_MESSAGE = (
    "a batch entry conflicts with already-recorded immutable state; the whole "
    "batch was rolled back."
)
_STORAGE_MESSAGE = "a local storage operation failed; nothing was changed."

#: The only library cause codes ever copied into a message.
_FOLDER_CODES = frozenset(_folders._MESSAGES)
_IMPORT_CODES = frozenset(_IMPORT_ERRORS)


# --------------------------------------------------------------------------- #
# argument tree
# --------------------------------------------------------------------------- #
def build_folder_parsers(acq_sub: "argparse._SubParsersAction") -> None:
    """Register ``folder`` on the ``acquisition`` subparsers."""
    folder = acq_sub.add_parser(
        "folder",
        help="atomic explicit folder-batch import (proposals only; no approval)",
    )
    fsub = folder.add_subparsers(
        dest="acquisition_folder_command", required=True, metavar="FOLDER_COMMAND"
    )
    p_import = fsub.add_parser(
        "import",
        help=(
            "import exactly the files named in --entries-file under ROOT, all or "
            "nothing; identity and rights are explicit caller input"
        ),
    )
    p_import.add_argument("root", metavar="ROOT", help="folder containing the files")
    p_import.add_argument(
        "--entries-file",
        required=True,
        metavar="PATH",
        help=(
            "strict JSON list; each entry has exactly relative_path, artifact_id, "
            "origin_locator, rights, revision_id, request_id, occurrence_rights, "
            "occurrence_revision_id and optionally parent_revision_id, dependencies"
        ),
    )
    p_import.add_argument("--project", metavar="ID")
    p_import.add_argument(
        "--max-files", type=int, default=DEFAULT_MAX_FILES, metavar="N"
    )
    p_import.add_argument(
        "--max-total-bytes", type=int, default=DEFAULT_MAX_TOTAL_BYTES, metavar="N"
    )


# --------------------------------------------------------------------------- #
# handler
# --------------------------------------------------------------------------- #
def _batch_error(exc: BatchImportError) -> AcquisitionControlError:
    """Static message with only an integer index and a fixed cause code."""
    if isinstance(exc, BatchImportConflictError):
        category, base = _CATEGORY_CONFLICT, _CONFLICT_MESSAGE
    elif exc.code == "storage_failure":
        category, base = _CATEGORY_STORAGE, _MESSAGES["storage_failure"]
    else:
        category = _CATEGORY_VALIDATION
        base = _MESSAGES.get(exc.code, _MESSAGES["invalid_arguments"])
    suffix = ""
    if isinstance(exc.index, int) and not isinstance(exc.index, bool) and exc.index >= 0:
        suffix += f" [entry index {exc.index}]"
    cause = exc.cause_code
    if cause in _FOLDER_CODES or cause in _IMPORT_CODES:
        suffix += f" [code {cause}]"
    return AcquisitionControlError(category, base + suffix)


def _summary(result: dict) -> dict[str, Any]:
    items = [
        {
            "index": item["index"],
            "source_ref": item["source_ref"],
            "occurrence_ref": item["occurrence_ref"],
            "disposition": item["disposition"],
            "replayed": item["replayed"],
            "sha256": item["sha256"],
            "byte_size": item["byte_size"],
        }
        for item in result["items"]
    ]
    return {
        "proposal_only": True,
        "authorizes_scientific_processing": False,
        "counts": dict(result["counts"]),
        "items": items,
        "private_content_included": False,
    }


def folder_import(args: argparse.Namespace, ctx: Any) -> dict[str, Any]:
    """Import one explicit folder batch atomically; never approves anything."""
    if not isinstance(args.root, str) or not args.root:
        raise AcquisitionControlError(_CATEGORY_VALIDATION, _ROOT_MESSAGE)
    if not (1 <= args.max_files <= MAX_FILES_CEILING) or not (
        1 <= args.max_total_bytes <= MAX_TOTAL_BYTES_CEILING
    ):
        raise AcquisitionControlError(_CATEGORY_VALIDATION, _LIMIT_MESSAGE)
    entries = _read_json_file(args.entries_file, what="entries")
    if not isinstance(entries, list):
        raise AcquisitionControlError(_CATEGORY_VALIDATION, _ENTRIES_SHAPE_MESSAGE)
    project_id = _resolve_project_id(ctx, args.project)
    with _verified_store(ctx, project_id) as store:
        try:
            result = import_folder_batch(
                store,
                args.root,
                entries,
                max_files=args.max_files,
                max_total_bytes=args.max_total_bytes,
            )
        except BatchImportError as exc:
            raise _batch_error(exc) from None
        except StoreError:
            raise AcquisitionControlError(_CATEGORY_STORAGE, _STORAGE_MESSAGE) from None
    return _summary(result)


_FOLDER_COMMANDS: dict[str, Callable[[argparse.Namespace, Any], Any]] = {
    "import": folder_import,
}


def dispatch_folder(args: argparse.Namespace, ctx: Any) -> object | None:
    """Route ``acquisition folder ...``; ``None`` if unknown."""
    handler = _FOLDER_COMMANDS.get(getattr(args, "acquisition_folder_command", None))
    if handler is None:
        return None
    return handler(args, ctx)
