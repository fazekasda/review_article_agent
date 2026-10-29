"""Personal CLI surface for local acquisition imports and readbacks (Step 06, C1).

This module adds the root ``acquisition`` command group. It owns only the **CLI
surface** over the already-integrated Step 06 value layers:

* :class:`~review_article_agent.acquisition.imports.AssetImportRepository` --
  durable authorized source import, the metadata-only "missing" event, and the
  typed occurrence readback.
* :func:`~review_article_agent.acquisition.validation.validate_asset` and
  :func:`~review_article_agent.acquisition.pdf_inspection.inspect_pdf` -- pure
  byte validation / bounded optional PDF inspection of one local file.
* :func:`~review_article_agent.acquisition.impact.source_impact` -- the read-only
  dependency-impact report.

Commands (all local; none performs a network, provider/model, or approval call):

* ``acquisition import FILE`` -- store one local file as a proposal source
  revision. Every identifier, the source ``RightsRecord`` and the occurrence
  ``RightsRecord`` are explicit caller input (nothing is defaulted or fabricated).
* ``acquisition missing`` -- record a metadata-only ``no_full_text_acquired``
  event under explicit pre-acquisition subject rights; no bytes, no revision.
* ``acquisition occurrence id`` -- print the deterministic occurrence artifact id
  for a request id so the caller can prepare the exact occurrence rights.
* ``acquisition occurrence show`` -- read one stored occurrence (by request id,
  or by artifact id with the current/an exact revision). The default output is a
  safe summary; ``--content`` is the explicit private opt-in.
* ``acquisition inspect FILE`` -- validate one local file against expected
  identifiers (and optionally run the PDF inspector). Safe status/checks/reasons
  by default; ``--content`` opts in to the private details.
* ``acquisition impact`` -- the read-only ``source_impact`` report.

Imports record a **proposal only**: no approval, screening, or seed eligibility
is created or implied.

Design rules mirror :mod:`review_article_agent.cli.discovery`: projects are opened
through the registry with ``expected_project_id`` and closed on every path;
JSON input is strict and bounded; file reads are bounded, regular-file-only and
do not follow a final symlink; failures raise :class:`AcquisitionControlError`
with a stable ``category`` (``validation`` / ``conflict`` / ``storage``) and a
static message that never reflects a path, id, title, DOI, source byte or
exception text.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import stat
from typing import Any, Callable

from ..acquisition.imports import (
    MAX_SOURCE_BYTES,
    AssetImportConflictError,
    AssetImportError,
    AssetImportRepository,
    ImportOccurrence,
)
from ..acquisition.impact import source_impact
from ..acquisition.pdf_inspection import inspect_pdf
from ..acquisition.validation import AcquisitionValidationError, validate_asset
from ..domain.errors import ReviewAgentError, StoreError
from ..domain.records import RecordValidationError
from ..storage import ArtifactConflictError, ArtifactRepository, ProjectStore, paths
from .config import STRICT_JSON_REASONS, StrictJsonError, load_strict_json

__all__ = [
    "ACQUISITION_COMMANDS",
    "AcquisitionControlError",
    "MAX_JSON_INPUT_BYTES",
    "build_acquisition_parser",
    "dispatch_acquisition",
]

_CATEGORY_VALIDATION = "validation"
_CATEGORY_CONFLICT = "conflict"
_CATEGORY_STORAGE = "storage"

#: Hard cap on any JSON side-input file (rights, identifiers, dependencies).
MAX_JSON_INPUT_BYTES = 1 << 20  # 1 MiB

_NOT_FOUND_MESSAGE = (
    "no stored acquisition occurrence matches that request in this project; "
    "nothing was read or changed."
)
_INPUT_UNREADABLE_MESSAGE = (
    "the {what} input could not be read (it must be an existing regular file; "
    "symlinks, pipes and devices are refused). Nothing was written."
)
_INPUT_TOO_LARGE_MESSAGE = (
    "the {what} input is larger than the permitted limit; refusing to read it. "
    "Nothing was written."
)
_JSON_MESSAGE = "the {what} input {reason}. Nothing was written."
_UTF8_MESSAGE = "the {what} input is not valid UTF-8 text. Nothing was written."
_SHAPE_MESSAGE = "the {what} input has the wrong shape. Nothing was written."
_REQUEST_INVALID_MESSAGE = (
    "the request was rejected (a malformed or out-of-bounds identifier, locator or "
    "reason, or a malformed dependency). Nothing was written."
)
_VALIDATION_REQUEST_MESSAGE = (
    "the validation request was rejected (a malformed identifier mapping or media "
    "type). Nothing was done."
)
_IMPACT_INVALID_MESSAGE = (
    "the impact request was rejected (a malformed identifier or a revision that "
    "does not exist in this project). Nothing was changed."
)

#: Static, value-free message and category for each ``AssetImportError`` code.
_IMPORT_ERRORS: dict[str, tuple[str, str]] = {
    "invalid_input": (_CATEGORY_VALIDATION, _REQUEST_INVALID_MESSAGE),
    "content_too_large": (
        _CATEGORY_VALIDATION,
        "the source file exceeds the import size bound. Nothing was written.",
    ),
    "rights_invalid": (
        _CATEGORY_VALIDATION,
        "a supplied rights record is invalid for its exact subject. Nothing was "
        "written.",
    ),
    "acquisition_not_allowed": (
        _CATEGORY_VALIDATION,
        "the source rights do not allow acquisition. Nothing was written.",
    ),
    "storage_not_allowed": (
        _CATEGORY_VALIDATION,
        "the source rights do not allow storage; no bytes were retained.",
    ),
    "privacy_denied": (
        _CATEGORY_VALIDATION,
        "the source rights record a privacy denial; the import is blocked. "
        "Nothing was written.",
    ),
    "rights_superseded": (
        _CATEGORY_CONFLICT,
        "a supplied rights record is superseded by a newer revision. Nothing was "
        "written.",
    ),
    "rights_conflict": (
        _CATEGORY_CONFLICT,
        "the supplied rights cannot authorize reuse of the exact existing source. "
        "Nothing was written.",
    ),
    "request_conflict": (
        _CATEGORY_CONFLICT,
        "this request id was already used with different arguments. Nothing was "
        "overwritten.",
    ),
    "dependency_conflict": (
        _CATEGORY_CONFLICT,
        "the declared dependencies differ from the existing source. Nothing was "
        "overwritten.",
    ),
    "kind_conflict": (
        _CATEGORY_CONFLICT,
        "the artifact id is already used for a different kind. Nothing was "
        "overwritten.",
    ),
    "source_rejected": (
        _CATEGORY_CONFLICT,
        "the source revision was rejected by the artifact store. Nothing was "
        "written.",
    ),
    "occurrence_rejected": (
        _CATEGORY_CONFLICT,
        "the occurrence record was rejected by the artifact store. Nothing was "
        "written.",
    ),
    "occurrence_invalid": (
        _CATEGORY_STORAGE,
        "the stored occurrence failed validation; nothing was changed.",
    ),
    "storage_failure": (
        _CATEGORY_STORAGE,
        "the local database operation failed; nothing was changed.",
    ),
}
_STORAGE_MESSAGE = "a local storage operation failed; nothing was changed."
_CONFLICT_MESSAGE = "a conflicting record already exists; nothing was overwritten."


class AcquisitionControlError(ReviewAgentError):
    """An acquisition command failed in a typed way.

    ``category`` is ``"validation"``, ``"conflict"``, or ``"storage"``; the
    message is always a static constant authored in this module.
    """

    def __init__(self, category: str, message: str) -> None:
        super().__init__(message)
        self.category = category


# --------------------------------------------------------------------------- #
# argument tree
# --------------------------------------------------------------------------- #
def _add_import_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--project", metavar="ID")
    parser.add_argument("--artifact-id", required=True, metavar="ID")
    parser.add_argument("--request-id", required=True, metavar="ID")
    parser.add_argument(
        "--rights-file",
        required=True,
        metavar="PATH",
        help="explicit caller RightsRecord JSON for the source / subject",
    )
    parser.add_argument(
        "--occurrence-rights-file",
        required=True,
        metavar="PATH",
        help=(
            "explicit caller RightsRecord JSON for the occurrence record "
            "(see 'occurrence id' for the exact subject artifact id)"
        ),
    )
    parser.add_argument("--occurrence-revision-id", required=True, metavar="ID")
    parser.add_argument("--origin-locator", required=True, metavar="TEXT")


def build_acquisition_parser(sub: "argparse._SubParsersAction") -> None:
    """Register the root ``acquisition`` command group on the top-level parser."""
    acq = sub.add_parser(
        "acquisition",
        help=(
            "local source import, missing-source events, occurrence readback, "
            "file inspection and impact report; stores proposals only"
        ),
    )
    acq_sub = acq.add_subparsers(
        dest="acquisition_command", required=True, metavar="ACQUISITION_COMMAND"
    )

    p_import = acq_sub.add_parser(
        "import",
        help="store one local file as an authorized source revision (proposal only)",
    )
    p_import.add_argument("file", metavar="FILE", help="local regular file to import")
    _add_import_common(p_import)
    p_import.add_argument("--revision-id", required=True, metavar="ID")
    p_import.add_argument("--parent-revision-id", metavar="ID")
    p_import.add_argument(
        "--dependencies-file",
        metavar="PATH",
        help="JSON list of {project_id, artifact_id, revision_id, role} objects",
    )

    p_missing = acq_sub.add_parser(
        "missing",
        help=(
            "record a metadata-only 'no full text acquired' event; stores no "
            "bytes and no source revision"
        ),
    )
    _add_import_common(p_missing)
    p_missing.add_argument("--reason", required=True, metavar="TEXT")

    occ = acq_sub.add_parser("occurrence", help="import-occurrence id and readback")
    occ_sub = occ.add_subparsers(
        dest="acquisition_occurrence_command",
        required=True,
        metavar="OCCURRENCE_COMMAND",
    )
    p_occ_id = occ_sub.add_parser(
        "id", help="print the deterministic occurrence artifact id for a request id"
    )
    p_occ_id.add_argument("--request-id", required=True, metavar="ID")
    p_occ_id.add_argument("--project", metavar="ID")

    p_occ_show = occ_sub.add_parser(
        "show", help="read one stored occurrence (safe summary; --content to opt in)"
    )
    p_occ_show.add_argument("--request-id", metavar="ID")
    p_occ_show.add_argument("--artifact-id", metavar="ID")
    p_occ_show.add_argument(
        "--revision-id", metavar="ID", help="exact revision (default: current)"
    )
    p_occ_show.add_argument(
        "--content",
        action="store_true",
        help="explicitly include the private origin locator and reason",
    )
    p_occ_show.add_argument("--project", metavar="ID")

    p_inspect = acq_sub.add_parser(
        "inspect",
        help=(
            "validate one local file against expected identifiers "
            "(safe summary; --content to opt in); stores nothing"
        ),
    )
    p_inspect.add_argument("file", metavar="FILE")
    p_inspect.add_argument(
        "--identifiers-file",
        required=True,
        metavar="PATH",
        help="JSON mapping of expected identifiers, e.g. {\"pmid\": \"123\"}",
    )
    p_inspect.add_argument("--declared-media-type", metavar="TYPE")
    p_inspect.add_argument(
        "--pdf-inspect",
        action="store_true",
        help="also run the bounded optional PDF inspector on a PDF container",
    )
    p_inspect.add_argument(
        "--content",
        action="store_true",
        help="explicitly include private details (identifiers, text preview)",
    )

    p_impact = acq_sub.add_parser(
        "impact", help="read-only dependency-impact report for a source revision"
    )
    p_impact.add_argument("--project", metavar="ID")
    p_impact.add_argument("--artifact-id", required=True, metavar="ID")
    p_impact.add_argument("--revision-id", required=True, metavar="ID")

    # Step 06 C2 addon (manifest + queue); imported lazily because the addon
    # module imports this one for its shared helpers.
    from .acquisition_manifest import build_manifest_parsers

    build_manifest_parsers(acq_sub)

    # Step 06 C3 addon (atomic explicit folder import); same lazy-import reason.
    from .acquisition_folders import build_folder_parsers

    build_folder_parsers(acq_sub)


# --------------------------------------------------------------------------- #
# shared helpers
# --------------------------------------------------------------------------- #
def _resolve_project_id(ctx: Any, explicit: str | None) -> str:
    if explicit is not None:
        return explicit
    return ctx.registry.active_project().project_id


@contextlib.contextmanager
def _verified_store(ctx: Any, project_id: str):
    """Open a project's store with its expected id; always close it."""
    info = ctx.registry.get(project_id)
    store = ProjectStore.open_at(
        paths.app_dir_for(info.root_path), expected_project_id=info.project_id
    )
    try:
        yield store
    finally:
        store.close()


def _read_regular_file(path: str, *, what: str, limit: int) -> bytes:
    """Read a bounded regular file without following a final symlink."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    fd = -1
    try:
        fd = os.open(path, flags)
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError
        chunks: list[bytes] = []
        remaining = limit + 1
        while remaining > 0:
            chunk = os.read(fd, min(1 << 20, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
    except (OSError, ValueError, TypeError):
        raise AcquisitionControlError(
            _CATEGORY_VALIDATION, _INPUT_UNREADABLE_MESSAGE.format(what=what)
        ) from None
    finally:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass
    data = b"".join(chunks)
    if len(data) > limit:
        raise AcquisitionControlError(
            _CATEGORY_VALIDATION, _INPUT_TOO_LARGE_MESSAGE.format(what=what)
        )
    return data


def _read_json_file(path: str, *, what: str) -> object:
    raw = _read_regular_file(path, what=what, limit=MAX_JSON_INPUT_BYTES)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise AcquisitionControlError(
            _CATEGORY_VALIDATION, _UTF8_MESSAGE.format(what=what)
        ) from None
    try:
        return load_strict_json(text)
    except StrictJsonError as exc:
        reason = STRICT_JSON_REASONS.get(exc.code, "is not usable")
        raise AcquisitionControlError(
            _CATEGORY_VALIDATION, _JSON_MESSAGE.format(what=what, reason=reason)
        ) from None
    except (ValueError, RecursionError):
        raise AcquisitionControlError(
            _CATEGORY_VALIDATION,
            _JSON_MESSAGE.format(what=what, reason="is not usable"),
        ) from None


def _read_rights(path: str, *, what: str) -> dict:
    value = _read_json_file(path, what=what)
    if not isinstance(value, dict):
        raise AcquisitionControlError(
            _CATEGORY_VALIDATION, _SHAPE_MESSAGE.format(what=what)
        )
    return value


def _guard_store_call(call: Callable[[], Any], *, invalid_message: str) -> Any:
    """Run a store-layer call, converting every failure to a static typed error."""
    try:
        return call()
    except AcquisitionControlError:
        raise
    except AssetImportError as exc:
        category, message = _IMPORT_ERRORS.get(
            getattr(exc, "code", ""), (_CATEGORY_STORAGE, _STORAGE_MESSAGE)
        )
        if isinstance(exc, AssetImportConflictError) and category != _CATEGORY_CONFLICT:
            category, message = _CATEGORY_CONFLICT, _CONFLICT_MESSAGE
        raise AcquisitionControlError(category, message) from None
    except ArtifactConflictError:
        raise AcquisitionControlError(_CATEGORY_CONFLICT, _CONFLICT_MESSAGE) from None
    except RecordValidationError:
        raise AcquisitionControlError(_CATEGORY_VALIDATION, invalid_message) from None
    except StoreError:
        raise AcquisitionControlError(_CATEGORY_STORAGE, _STORAGE_MESSAGE) from None


def _ref_dict(ref: Any) -> dict | None:
    return None if ref is None else ref.as_dict()


def _occurrence_summary(occ: ImportOccurrence) -> dict[str, Any]:
    """Opaque ids, hashes and flags only: no origin locator and no reason text."""
    return {
        "occurrence_ref": occ.occurrence_ref.as_dict(),
        "operation": occ.operation,
        "artifact_id": occ.artifact_id,
        "disposition": occ.disposition,
        "sha256": occ.content_hash,
        "byte_size": occ.byte_size,
        "acquired_at": occ.acquired_at,
        "source_ref": _ref_dict(occ.source_ref),
        "source_rights_ref": occ.source_rights_ref.as_dict(),
        "source_revision_rights_ref": _ref_dict(occ.source_revision_rights_ref),
        "occurrence_rights_ref": occ.occurrence_rights_ref.as_dict(),
        "parent_revision_id": occ.parent_revision_id,
        "dependencies": [d.as_dict() for d in occ.dependencies],
        "origin_locator_present": bool(occ.origin_locator),
        "reason_present": occ.reason is not None,
        "proposal_only": occ.proposal_only,
        "authorizes_use": occ.authorizes_use,
        "private_content_included": False,
    }


# --------------------------------------------------------------------------- #
# handlers
# --------------------------------------------------------------------------- #
def acquisition_import(args: argparse.Namespace, ctx: Any) -> dict[str, Any]:
    """Import one local file; every identifier and rights record is explicit."""
    rights = _read_rights(args.rights_file, what="rights")
    occ_rights = _read_rights(args.occurrence_rights_file, what="occurrence rights")
    deps: object = []
    if args.dependencies_file is not None:
        deps = _read_json_file(args.dependencies_file, what="dependencies")
        if not isinstance(deps, list):
            raise AcquisitionControlError(
                _CATEGORY_VALIDATION, _SHAPE_MESSAGE.format(what="dependencies")
            )
    content = _read_regular_file(args.file, what="source file", limit=MAX_SOURCE_BYTES)
    project_id = _resolve_project_id(ctx, args.project)
    with _verified_store(ctx, project_id) as store:
        repo = AssetImportRepository(store)
        result = _guard_store_call(
            lambda: repo.import_asset(
                artifact_id=args.artifact_id,
                content=content,
                origin_locator=args.origin_locator,
                rights=rights,
                revision_id=args.revision_id,
                request_id=args.request_id,
                occurrence_rights=occ_rights,
                occurrence_revision_id=args.occurrence_revision_id,
                parent_revision_id=args.parent_revision_id,
                dependencies=deps,
            ),
            invalid_message=_REQUEST_INVALID_MESSAGE,
        )
    return {**result, "proposal_only": True, "authorizes_use": False}


def acquisition_missing(args: argparse.Namespace, ctx: Any) -> dict[str, Any]:
    """Record a metadata-only 'no full text acquired' event (no bytes)."""
    rights = _read_rights(args.rights_file, what="rights")
    occ_rights = _read_rights(args.occurrence_rights_file, what="occurrence rights")
    project_id = _resolve_project_id(ctx, args.project)
    with _verified_store(ctx, project_id) as store:
        repo = AssetImportRepository(store)
        result = _guard_store_call(
            lambda: repo.record_missing(
                artifact_id=args.artifact_id,
                origin_locator=args.origin_locator,
                rights=rights,
                request_id=args.request_id,
                occurrence_rights=occ_rights,
                occurrence_revision_id=args.occurrence_revision_id,
                reason=args.reason,
            ),
            invalid_message=_REQUEST_INVALID_MESSAGE,
        )
    return {**result, "proposal_only": True, "authorizes_use": False}


def acquisition_occurrence_id(args: argparse.Namespace, ctx: Any) -> dict[str, Any]:
    """Print the deterministic occurrence artifact id (read-only, no write)."""
    project_id = _resolve_project_id(ctx, args.project)
    with _verified_store(ctx, project_id) as store:
        repo = AssetImportRepository(store)
        artifact_id = _guard_store_call(
            lambda: repo.occurrence_artifact_id(args.request_id),
            invalid_message=_REQUEST_INVALID_MESSAGE,
        )
    return {
        "project_id": project_id,
        "request_id": args.request_id,
        "occurrence_artifact_id": artifact_id,
    }


def acquisition_occurrence_show(args: argparse.Namespace, ctx: Any) -> dict[str, Any]:
    """Read one stored occurrence; safe summary unless ``--content``."""
    by_request = args.request_id is not None
    if by_request == (args.artifact_id is not None) or (
        by_request and args.revision_id is not None
    ):
        raise AcquisitionControlError(
            _CATEGORY_VALIDATION,
            "provide either --request-id, or --artifact-id with an optional "
            "--revision-id.",
        )
    project_id = _resolve_project_id(ctx, args.project)
    with _verified_store(ctx, project_id) as store:
        repo = AssetImportRepository(store)

        def read() -> ImportOccurrence:
            if by_request:
                found = repo.find_request(args.request_id)
                if found is None:
                    raise AcquisitionControlError(
                        _CATEGORY_VALIDATION, _NOT_FOUND_MESSAGE
                    )
                return found
            revision_id = args.revision_id
            if revision_id is None:
                head = ArtifactRepository(store).current_revision(args.artifact_id)
                if head is None:
                    raise AcquisitionControlError(
                        _CATEGORY_VALIDATION, _NOT_FOUND_MESSAGE
                    )
                revision_id = head.revision_id
            return repo.get_occurrence(
                {
                    "project_id": project_id,
                    "artifact_id": args.artifact_id,
                    "revision_id": revision_id,
                }
            )

        occ = _guard_store_call(read, invalid_message=_REQUEST_INVALID_MESSAGE)
    result = _occurrence_summary(occ)
    if args.content:
        result["private_content_included"] = True
        result["origin_locator"] = occ.origin_locator
        result["reason"] = occ.reason
    return result


def acquisition_inspect(args: argparse.Namespace, ctx: Any) -> dict[str, Any]:
    """Validate (and optionally PDF-inspect) one local file; stores nothing."""
    identifiers = _read_json_file(args.identifiers_file, what="identifiers")
    if not isinstance(identifiers, dict):
        raise AcquisitionControlError(
            _CATEGORY_VALIDATION, _SHAPE_MESSAGE.format(what="identifiers")
        )
    data = _read_regular_file(args.file, what="source file", limit=MAX_SOURCE_BYTES)
    try:
        validation = validate_asset(
            data,
            expected_identifiers=identifiers,
            declared_media_type=args.declared_media_type,
        )
    except AcquisitionValidationError:
        raise AcquisitionControlError(
            _CATEGORY_VALIDATION, _VALIDATION_REQUEST_MESSAGE
        ) from None
    result: dict[str, Any] = {
        "report": "acquisition_inspection",
        "status": validation["status"],
        "media_kind": validation["media_kind"],
        "sha256": validation["sha256"],
        "byte_size": validation["byte_size"],
        "checks": validation["checks"],
        "reasons": validation["reasons"],
        "authorizes_scientific_processing": False,
        "private_content_included": bool(args.content),
    }
    inspection: dict[str, Any] | None = None
    if args.pdf_inspect:
        if validation["media_kind"] == "pdf":
            inspection = inspect_pdf(data)
            result["pdf_inspection"] = {
                "status": inspection["status"],
                "failure_code": inspection["failure_code"],
                "encrypted": inspection["encrypted"],
                "page_count": inspection["page_count"],
                "pages_with_text": sum(1 for p in inspection["page_text_present"] if p),
                "parser_version": inspection["parser_version"],
            }
        else:
            result["pdf_inspection"] = {
                "status": "skipped",
                "failure_code": "not_pdf_container",
            }
    if args.content:
        result["observed_identifiers"] = validation["observed_identifiers"]
        if inspection is not None:
            result["pdf_inspection_content"] = {
                "identifiers": inspection["identifiers"],
                "page_text_present": inspection["page_text_present"],
                "text_preview": inspection["text_preview"],
            }
    return result


def acquisition_impact(args: argparse.Namespace, ctx: Any) -> dict[str, Any]:
    """The read-only ``source_impact`` report (opaque ids and statuses only)."""
    project_id = _resolve_project_id(ctx, args.project)
    with _verified_store(ctx, project_id) as store:
        return _guard_store_call(
            lambda: source_impact(
                store,
                {
                    "project_id": project_id,
                    "artifact_id": args.artifact_id,
                    "revision_id": args.revision_id,
                },
            ),
            invalid_message=_IMPACT_INVALID_MESSAGE,
        )


# --------------------------------------------------------------------------- #
# dispatch
# --------------------------------------------------------------------------- #
ACQUISITION_COMMANDS: dict[str, Callable[[argparse.Namespace, Any], Any]] = {
    "import": acquisition_import,
    "missing": acquisition_missing,
    "inspect": acquisition_inspect,
    "impact": acquisition_impact,
}
_OCCURRENCE_COMMANDS: dict[str, Callable[[argparse.Namespace, Any], Any]] = {
    "id": acquisition_occurrence_id,
    "show": acquisition_occurrence_show,
}


def dispatch_acquisition(args: argparse.Namespace, ctx: Any) -> object | None:
    """Route one parsed ``acquisition`` invocation; ``None`` if unknown."""
    command = args.acquisition_command
    if command in ("manifest", "queue"):
        from .acquisition_manifest import dispatch_manifest

        return dispatch_manifest(args, ctx)
    if command == "folder":
        from .acquisition_folders import dispatch_folder

        return dispatch_folder(args, ctx)
    if command == "occurrence":
        handler = _OCCURRENCE_COMMANDS.get(
            getattr(args, "acquisition_occurrence_command", None)
        )
    else:
        handler = ACQUISITION_COMMANDS.get(command)
    if handler is None:
        return None
    return handler(args, ctx)
