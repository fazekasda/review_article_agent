"""Personal CLI surface for the integrated Step 07 parsing repository (S7-CLI).

This module adds the root ``parsing`` command group. It owns only the **CLI
surface** over the already-integrated
:class:`~review_article_agent.parsing.repository.ParsingRepository`:

* ``parsing parse`` -- turn one exact stored ``document`` revision into a
  **distinct** immutable ``report`` revision, from an explicit source reference,
  an explicit report artifact/revision and an explicit caller ``RightsRecord``.
* ``parsing show`` -- read one exact report revision back (safe summary;
  ``--content`` is the explicit private opt-in for the parsed passages).
* ``parsing locator`` -- build a bound passage locator for one exact report
  passage (safe summary; ``--content`` prints the full locator).
* ``parsing resolve`` -- re-check a saved locator against its exact historical
  report revision (safe counts by default; ``--content`` for raw marker detail).

Nothing here approves, screens, extracts, executes, or reaches the network: no
model, provider, or approval call is made, and a stored report only means that
structural parsing was read -- it is **never** scientific correctness,
completeness, or an exclusion.

Privacy and safety rules mirror :mod:`review_article_agent.cli.acquisition`:
projects are opened through the registry with ``expected_project_id`` and closed
on every path; JSON side-inputs (rights, locator) are read from a bounded
regular file that never follows a final symlink; and failures raise
:class:`ParsingControlError` with a stable ``category``
(``validation``/``conflict``/``storage``) and a static message that never
reflects a path, id, rights value, passage text, or exception text. Default
output carries no private passage/section text, source bytes, captions, or raw
citation-marker strings.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import stat
from typing import Any, Callable

from ..domain.errors import StoreError
from ..domain.records import RecordValidationError
from ..parsing.repository import ParsingRepository, ParsingRepositoryError
from ..storage import ArtifactConflictError, ProjectStore, paths
from .config import STRICT_JSON_REASONS, StrictJsonError, load_strict_json

__all__ = [
    "PARSING_COMMANDS",
    "ParsingControlError",
    "build_parsing_parser",
    "dispatch_parsing",
]

_CATEGORY_VALIDATION = "validation"
_CATEGORY_CONFLICT = "conflict"
_CATEGORY_STORAGE = "storage"

#: Hard cap on any JSON side-input file (rights, locator).
MAX_JSON_INPUT_BYTES = 1 << 20  # 1 MiB

_REJECTED_MESSAGE = (
    "the parsing request was rejected (a malformed or out-of-bounds identifier, "
    "report reference, rights record, or locator). Nothing was written."
)
_NOT_FOUND_MESSAGE = (
    "no stored document or report revision matches that exact reference in this "
    "project; nothing was read or changed."
)
_TAMPERED_MESSAGE = (
    "the stored report or source content does not match its recorded immutable "
    "digest, or disagrees with a deterministic reparse; nothing was trusted or "
    "changed."
)
_CONFLICT_MESSAGE = (
    "the request conflicts with existing immutable state; nothing was overwritten."
)
_RIGHTS_BLOCKED_MESSAGE = (
    "the source's latest rights do not permit derivative processing; nothing was "
    "parsed."
)
_STORAGE_MESSAGE = "a local storage operation failed; nothing was changed."
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
_SHAPE_MESSAGE = (
    "the {what} input has the wrong shape (a JSON object is required). Nothing "
    "was written."
)

#: Static, value-free ``(category, message)`` for each repository error code.
_CODE_ERRORS: dict[str, tuple[str, str]] = {
    "invalid_input": (_CATEGORY_VALIDATION, _REJECTED_MESSAGE),
    "source_not_document": (_CATEGORY_VALIDATION, _NOT_FOUND_MESSAGE),
    "source_not_found": (_CATEGORY_VALIDATION, _NOT_FOUND_MESSAGE),
    "source_content_hash_mismatch": (_CATEGORY_CONFLICT, _TAMPERED_MESSAGE),
    "source_rights_blocked": (_CATEGORY_VALIDATION, _RIGHTS_BLOCKED_MESSAGE),
    "report_overwrites_source": (_CATEGORY_CONFLICT, _CONFLICT_MESSAGE),
    "report_not_found": (_CATEGORY_VALIDATION, _NOT_FOUND_MESSAGE),
    "report_kind_invalid": (_CATEGORY_VALIDATION, _NOT_FOUND_MESSAGE),
    "report_content_hash_mismatch": (_CATEGORY_CONFLICT, _TAMPERED_MESSAGE),
    "report_envelope_invalid": (_CATEGORY_VALIDATION, _REJECTED_MESSAGE),
    "report_not_canonical": (_CATEGORY_VALIDATION, _REJECTED_MESSAGE),
    "report_payload_invalid": (_CATEGORY_VALIDATION, _REJECTED_MESSAGE),
    "report_source_dependency_invalid": (_CATEGORY_VALIDATION, _REJECTED_MESSAGE),
    "report_source_hash_mismatch": (_CATEGORY_CONFLICT, _TAMPERED_MESSAGE),
    "report_payload_mismatch": (_CATEGORY_CONFLICT, _TAMPERED_MESSAGE),
    "passage_not_found": (_CATEGORY_VALIDATION, _NOT_FOUND_MESSAGE),
    "locator_invalid": (_CATEGORY_VALIDATION, _REJECTED_MESSAGE),
    "locator_source_mismatch": (_CATEGORY_VALIDATION, _REJECTED_MESSAGE),
}


class ParsingControlError(Exception):
    """A parsing command failed in a typed way.

    ``category`` is ``"validation"``, ``"conflict"``, or ``"storage"``; the
    message is always a static constant authored in this module.
    """

    def __init__(self, category: str, message: str) -> None:
        super().__init__(message)
        self.category = category


# --------------------------------------------------------------------------- #
# argument tree
# --------------------------------------------------------------------------- #
def build_parsing_parser(sub: "argparse._SubParsersAction") -> None:
    """Register the root ``parsing`` command group on the top-level parser."""
    parsing = sub.add_parser(
        "parsing",
        help=(
            "parse stored documents into immutable report revisions, and read "
            "reports, locators and resolutions back; structural only, local only"
        ),
    )
    pars = parsing.add_subparsers(
        dest="parsing_command", required=True, metavar="PARSING_COMMAND"
    )

    p_parse = pars.add_parser(
        "parse",
        help="parse one exact stored document revision into a new report revision",
    )
    p_parse.add_argument("--project", metavar="ID")
    p_parse.add_argument("--source-artifact-id", required=True, metavar="ID")
    p_parse.add_argument("--source-revision-id", required=True, metavar="ID")
    p_parse.add_argument("--report-artifact-id", required=True, metavar="ID")
    p_parse.add_argument("--report-revision-id", required=True, metavar="ID")
    p_parse.add_argument(
        "--rights-file",
        required=True,
        metavar="PATH",
        help="explicit caller RightsRecord JSON bound to the report revision",
    )
    p_parse.add_argument(
        "--format",
        choices=("xml", "pdf"),
        help="override format inference (default: from media type / magic bytes)",
    )
    p_parse.add_argument(
        "--parent-revision-id",
        metavar="ID",
        help="current report head, when adding a later report revision",
    )

    p_show = pars.add_parser(
        "show", help="read one exact report revision (safe summary; --content to opt in)"
    )
    p_show.add_argument("--project", metavar="ID")
    p_show.add_argument("--report-artifact-id", required=True, metavar="ID")
    p_show.add_argument("--report-revision-id", required=True, metavar="ID")
    p_show.add_argument(
        "--content",
        action="store_true",
        help="explicitly include the private parsed passages, references and captions",
    )

    p_loc = pars.add_parser(
        "locator",
        help="build a bound passage locator (safe summary; --content for the full locator)",
    )
    p_loc.add_argument("--project", metavar="ID")
    p_loc.add_argument("--report-artifact-id", required=True, metavar="ID")
    p_loc.add_argument("--report-revision-id", required=True, metavar="ID")
    p_loc.add_argument("--passage-id", required=True, metavar="ID")
    p_loc.add_argument(
        "--content",
        action="store_true",
        help=(
            "explicitly include the private section-path headings and the full "
            "bound locator JSON to save for 'parsing resolve'"
        ),
    )

    p_res = pars.add_parser(
        "resolve",
        help="re-check a saved locator against its exact historical report revision",
    )
    p_res.add_argument("--project", metavar="ID")
    p_res.add_argument(
        "--locator-file",
        required=True,
        metavar="PATH",
        help="locator JSON input (a bounded regular file; '-' is not accepted)",
    )
    p_res.add_argument(
        "--content",
        action="store_true",
        help="explicitly include the raw per-passage citation markers and link detail",
    )


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
        raise ParsingControlError(
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
        raise ParsingControlError(
            _CATEGORY_VALIDATION, _INPUT_TOO_LARGE_MESSAGE.format(what=what)
        )
    return data


def _read_json_file(path: str, *, what: str) -> object:
    raw = _read_regular_file(path, what=what, limit=MAX_JSON_INPUT_BYTES)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise ParsingControlError(
            _CATEGORY_VALIDATION, _UTF8_MESSAGE.format(what=what)
        ) from None
    try:
        return load_strict_json(text)
    except StrictJsonError as exc:
        reason = STRICT_JSON_REASONS.get(exc.code, "is not usable")
        raise ParsingControlError(
            _CATEGORY_VALIDATION, _JSON_MESSAGE.format(what=what, reason=reason)
        ) from None
    except (ValueError, RecursionError):
        raise ParsingControlError(
            _CATEGORY_VALIDATION,
            _JSON_MESSAGE.format(what=what, reason="is not usable"),
        ) from None


def _read_object(path: str, *, what: str) -> dict:
    value = _read_json_file(path, what=what)
    if not isinstance(value, dict):
        raise ParsingControlError(
            _CATEGORY_VALIDATION, _SHAPE_MESSAGE.format(what=what)
        )
    return value


def _guard(call: Callable[[], Any], *, invalid_message: str) -> Any:
    """Run a repository call, converting every failure to a static typed error."""
    try:
        return call()
    except ParsingControlError:
        raise
    except ParsingRepositoryError as exc:
        category, message = _CODE_ERRORS.get(
            getattr(exc, "code", ""), (_CATEGORY_STORAGE, _STORAGE_MESSAGE)
        )
        raise ParsingControlError(category, message) from None
    except ArtifactConflictError:
        raise ParsingControlError(_CATEGORY_CONFLICT, _CONFLICT_MESSAGE) from None
    except RecordValidationError:
        raise ParsingControlError(_CATEGORY_VALIDATION, invalid_message) from None
    except StoreError:
        raise ParsingControlError(_CATEGORY_STORAGE, _STORAGE_MESSAGE) from None


def _report_ref(project_id: str, artifact_id: str, revision_id: str) -> dict:
    return {
        "project_id": project_id,
        "artifact_id": artifact_id,
        "revision_id": revision_id,
    }


def _section_path_digest(section_path: list) -> str:
    canonical = json.dumps(section_path, ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# handlers
# --------------------------------------------------------------------------- #
def parsing_parse(args: argparse.Namespace, ctx: Any) -> dict[str, Any]:
    """Parse one exact stored document revision into a distinct report revision."""
    rights = _read_object(args.rights_file, what="rights")
    project_id = _resolve_project_id(ctx, args.project)
    source_ref = {
        "project_id": project_id,
        "artifact_id": args.source_artifact_id,
        "revision_id": args.source_revision_id,
    }
    with _verified_store(ctx, project_id) as store:
        repo = ParsingRepository(store)
        result = _guard(
            lambda: repo.parse_stored_document(
                source_ref,
                report_artifact_id=args.report_artifact_id,
                report_revision_id=args.report_revision_id,
                rights=rights,
                format=args.format,
                parent_revision_id=args.parent_revision_id,
            ),
            invalid_message=_REJECTED_MESSAGE,
        )
    return {**result, "authorizes_scientific_processing": False}


def _report_summary(record: dict) -> dict[str, Any]:
    """A safe report projection: no passage/section text, captions, or markers."""
    payload = record["payload"]
    return {
        "report_ref": record["report_ref"],
        "source_ref": record["source_ref"],
        "format": record["format"],
        "status": record["status"],
        "human_action_required": record["human_action_required"],
        "excludes_candidate": record["excludes_candidate"],
        "parser_version": record["parser_version"],
        "normalization_version": record["normalization_version"],
        "parser_version_changed": record["parser_version_changed"],
        "normalization_version_changed": record["normalization_version_changed"],
        "content_sha256": record["content_sha256"],
        "byte_size": record["byte_size"],
        "passage_count": len(payload["passages"]),
        "reference_count": len(payload["references"]),
        "asset_count": len(payload["assets"]),
        "citation_counts": payload["citations"],
        "non_text_assets_require_review": payload["non_text_assets_require_review"],
        "reasons": list(payload["reasons"]),
        "private_content_included": False,
        "authorizes_scientific_processing": False,
    }


def parsing_show(args: argparse.Namespace, ctx: Any) -> dict[str, Any]:
    """Read one exact report revision; safe summary unless ``--content``."""
    project_id = _resolve_project_id(ctx, args.project)
    ref = _report_ref(project_id, args.report_artifact_id, args.report_revision_id)
    with _verified_store(ctx, project_id) as store:
        repo = ParsingRepository(store)
        record = _guard(
            lambda: repo.read_report(ref), invalid_message=_REJECTED_MESSAGE
        )
    summary = _report_summary(record)
    if args.content:
        summary["private_content_included"] = True
        summary["payload"] = record["payload"]
    return summary


def parsing_locator(args: argparse.Namespace, ctx: Any) -> dict[str, Any]:
    """Build a bound passage locator; safe summary unless ``--content``.

    The default output carries the locator's structural identity and hashes
    (needed to identify/save the pointer) but replaces the private
    ``section_path`` headings with a depth and a digest; ``--content`` prints the
    full bound locator -- the exact JSON to save and later feed to
    ``parsing resolve``.
    """
    project_id = _resolve_project_id(ctx, args.project)
    ref = _report_ref(project_id, args.report_artifact_id, args.report_revision_id)
    with _verified_store(ctx, project_id) as store:
        repo = ParsingRepository(store)
        locator = _guard(
            lambda: repo.make_passage_locator(ref, args.passage_id),
            invalid_message=_REJECTED_MESSAGE,
        )
    section_path = list(locator.get("section_path", []))
    summary: dict[str, Any] = {
        "report_ref": locator["report_ref"],
        "source_ref": locator["source_ref"],
        "passage_id": locator["passage_id"],
        "locator_sha256": locator["locator_sha256"],
        "text_sha256": locator["text_sha256"],
        "source_sha256": locator["source_sha256"],
        "parser_version": locator["parser_version"],
        "normalization_version": locator["normalization_version"],
        "section_path_depth": len(section_path),
        "section_path_sha256": _section_path_digest(section_path),
        "private_content_included": False,
        "authorizes_scientific_processing": False,
    }
    if args.content:
        summary["private_content_included"] = True
        summary["section_path"] = section_path
        summary["locator"] = locator
    return summary


def parsing_resolve(args: argparse.Namespace, ctx: Any) -> dict[str, Any]:
    """Re-check a saved locator; safe counts unless ``--content``."""
    locator = _read_object(args.locator_file, what="locator")
    project_id = _resolve_project_id(ctx, args.project)
    with _verified_store(ctx, project_id) as store:
        repo = ParsingRepository(store)
        resolved = _guard(
            lambda: repo.resolve_passage(locator), invalid_message=_REJECTED_MESSAGE
        )
    check = resolved["citation_check"]
    summary: dict[str, Any] = {
        "passage_id": resolved["passage_id"],
        "status": resolved["status"],
        "reasons": list(resolved["reasons"]),
        "locator_ok": resolved["locator_ok"],
        "historical_resolved": resolved["historical_resolved"],
        "report_ref": resolved["report_ref"],
        "source_ref": resolved["source_ref"],
        "current": resolved["current"],
        "citation_check": {
            "passage_id": check["passage_id"],
            "ok": check["ok"],
            "total": check["total"],
            "unresolved_count": len(check["unresolved"]),
            "ambiguous_count": len(check["ambiguous"]),
            "malformed_count": len(check["malformed"]),
        },
        "private_content_included": False,
        "authorizes_scientific_processing": False,
    }
    if args.content:
        summary["private_content_included"] = True
        summary["citation_check"] = check
    return summary


# --------------------------------------------------------------------------- #
# dispatch
# --------------------------------------------------------------------------- #
PARSING_COMMANDS: dict[str, Callable[[argparse.Namespace, Any], Any]] = {
    "parse": parsing_parse,
    "show": parsing_show,
    "locator": parsing_locator,
    "resolve": parsing_resolve,
}


def dispatch_parsing(args: argparse.Namespace, ctx: Any) -> object | None:
    """Route one parsed ``parsing`` invocation; ``None`` if unknown."""
    handler = PARSING_COMMANDS.get(getattr(args, "parsing_command", None))
    return None if handler is None else handler(args, ctx)
