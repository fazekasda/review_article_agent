"""Minimal local CLI surface for the durable Step 11 drafting workflow (S11-CLI-BASE).

This module adds only the root ``drafting`` command group over the already
integrated
:class:`~review_article_agent.drafting.workspace.SectionWorkspaceRepository` and
:class:`~review_article_agent.drafting.package.DraftingPackageRepository`. It is
a **local, private-by-default read/propose** surface: it never dispatches a
model, creates or executes a session, approves a gate, exports, or reaches the
network. A stored workspace revision only means a binding exists -- it is
**never** an approval and never authorizes execution, export, or scientific
truth.

* ``drafting propose`` -- write one *inert* per-section planner/writer workspace
  revision from an explicit input envelope, an explicit caller rights record,
  and explicit artifact/revision ids (with an optional parent revision).
* ``drafting show`` -- read one exact workspace revision back (safe structural
  counts and opaque refs; ``--content`` is the explicit private opt-in for the
  raw payload, which carries the runtime session/model pins). ``--historical``
  inspects a non-current revision without live-gate authority.
* ``drafting package`` -- assemble one approved outline section's reconstructible
  drafting package (safe counts by default; ``--content`` is the explicit private
  opt-in for the full package, which carries source excerpts, citations, and the
  approved global chain).

Only the ``workspace`` kind is registered today; a later task extends the kind
choices to ``blueprint``/``draft`` and adds the ``accepted``/``status``/
``assemble`` commands once those APIs are integrated. The CLI never fabricates a
runtime session mapping: the planner/writer mappings must already exist as
app-owned rows (the app owns their creation); this module adds no mapping command
and never guesses an unimplemented workflow import.

Privacy/safety rules mirror :mod:`review_article_agent.cli.synthesis`: projects
are opened through the registry with ``expected_project_id`` and closed on every
path; side-inputs are read from a bounded regular file that never follows a final
symlink; and every failure raises :class:`DraftingControlError` with a stable
category (``validation``/``conflict``/``storage``) and a static message that
never reflects a path, id, rights value, session/model detail, content, or
exception text. By default no source excerpt, prompt, session id, or model detail
is emitted.
"""

from __future__ import annotations

import argparse
from typing import Any, Callable

from ..domain.errors import ReviewAgentError, StoreError
from ..domain.records import RecordValidationError
from ..drafting.package import DraftingPackageRepository
from ..drafting.workspace import SectionWorkspaceRepository
from ..storage import ArtifactConflictError
from .parsing import _read_object, _resolve_project_id, _verified_store

__all__ = [
    "DraftingControlError",
    "build_drafting_parser",
    "dispatch_drafting",
]

_CATEGORY_VALIDATION = "validation"
_CATEGORY_CONFLICT = "conflict"
_CATEGORY_STORAGE = "storage"

#: The only proposal kind registered today. A later task extends this to
#: ``blueprint``/``draft`` once those APIs are integrated.
_KINDS = ("workspace",)

_WORKSPACE_REJECTED_MESSAGE = (
    "the drafting workspace request was rejected (a malformed or out-of-bounds "
    "identifier, outline/section reference, runtime session mapping pin, rights "
    "record, or workspace document). Nothing was written."
)
_WORKSPACE_READ_MESSAGE = (
    "the drafting workspace revision could not be read (a missing revision, a "
    "non-current revision, a failed content-integrity check, or a mapping that no "
    "longer re-derives from durable rows)."
)
_PACKAGE_REJECTED_MESSAGE = (
    "the drafting package could not be assembled (the exact current approved "
    "chain, the accepted extraction evidence, or the verified frozen corpus "
    "metadata is not consumable, or the section is not part of the approved "
    "outline)."
)
_CONFLICT_MESSAGE = (
    "the request conflicts with existing immutable state; nothing was overwritten."
)
_STORAGE_MESSAGE = "a local storage operation failed; nothing was changed."

#: Non-authorizing constants carried on every output.
_FALSE_FLAGS = {
    "authorizes_execution": False,
    "authorizes_export": False,
    "scientific_validation": False,
}

#: Every drafting output is a provisional local proposal, never a validation.
_STATUS = "provisional"


class DraftingControlError(Exception):
    """A drafting command failed in a typed way.

    ``category`` is ``"validation"``, ``"conflict"``, or ``"storage"``; the
    message is always a static constant authored in this module.
    """

    def __init__(self, category: str, message: str) -> None:
        super().__init__(message)
        self.category = category


# --------------------------------------------------------------------------- #
# argument tree
# --------------------------------------------------------------------------- #
def build_drafting_parser(sub: "argparse._SubParsersAction") -> None:
    """Register the root ``drafting`` command group on the top-level parser."""
    drafting = sub.add_parser(
        "drafting",
        help=(
            "propose and read per-section planner/writer workspaces and assemble "
            "one section's approved drafting package (local only; no dispatch, no "
            "approval, no export)"
        ),
    )
    actions = drafting.add_subparsers(
        dest="drafting_command", required=True, metavar="DRAFTING_COMMAND"
    )

    p_propose = actions.add_parser(
        "propose", help="write one inert per-section planner/writer workspace revision"
    )
    p_propose.add_argument("--project", metavar="ID")
    p_propose.add_argument("--kind", required=True, choices=_KINDS)
    p_propose.add_argument("--input-file", required=True, metavar="PATH")
    p_propose.add_argument("--rights-file", required=True, metavar="PATH")
    p_propose.add_argument("--artifact-id", required=True, metavar="ID")
    p_propose.add_argument("--revision-id", required=True, metavar="ID")
    p_propose.add_argument("--parent-revision-id", metavar="ID")

    p_show = actions.add_parser(
        "show",
        help=(
            "read one exact workspace revision (safe structural counts; --content "
            "to opt in; --historical to inspect a non-current revision)"
        ),
    )
    p_show.add_argument("--project", metavar="ID")
    p_show.add_argument("--kind", required=True, choices=_KINDS)
    p_show.add_argument("--artifact-id", required=True, metavar="ID")
    p_show.add_argument("--revision-id", required=True, metavar="ID")
    p_show.add_argument(
        "--historical",
        action="store_true",
        help="read a historical (non-current) revision without live-gate authority",
    )
    p_show.add_argument(
        "--content",
        action="store_true",
        help="explicitly include the private raw payload (runtime session/model pins)",
    )

    p_package = actions.add_parser(
        "package", help="assemble one approved outline section's drafting package"
    )
    p_package.add_argument("--project", metavar="ID")
    p_package.add_argument("--outline-artifact-id", required=True, metavar="ID")
    p_package.add_argument("--outline-revision-id", required=True, metavar="ID")
    p_package.add_argument("--section-id", required=True, metavar="ID")
    p_package.add_argument(
        "--content",
        action="store_true",
        help="explicitly include the full private package (evidence, excerpts, citations)",
    )


# --------------------------------------------------------------------------- #
# shared helpers
# --------------------------------------------------------------------------- #
def _guard(call: Callable[[], Any], *, invalid_message: str) -> Any:
    """Run a repository call, converting every failure to a static typed error."""
    try:
        return call()
    except DraftingControlError:
        raise
    except ArtifactConflictError:
        raise DraftingControlError(_CATEGORY_CONFLICT, _CONFLICT_MESSAGE) from None
    except StoreError:
        raise DraftingControlError(_CATEGORY_STORAGE, _STORAGE_MESSAGE) from None
    except OSError:
        raise DraftingControlError(_CATEGORY_STORAGE, _STORAGE_MESSAGE) from None
    except (
        ReviewAgentError,
        RecordValidationError,
        ValueError,
        TypeError,
        KeyError,
        AttributeError,
        StopIteration,
    ):
        raise DraftingControlError(_CATEGORY_VALIDATION, invalid_message) from None


def _ref(project_id: str, artifact_id: str, revision_id: str) -> dict:
    return {
        "project_id": project_id,
        "artifact_id": artifact_id,
        "revision_id": revision_id,
    }


# --------------------------------------------------------------------------- #
# handlers
# --------------------------------------------------------------------------- #
def drafting_propose(args: argparse.Namespace, ctx: Any) -> dict[str, Any]:
    """Write one inert per-section workspace revision; never an approval."""
    payload = _read_object(args.input_file, what="drafting workspace")
    rights = _read_object(args.rights_file, what="rights")
    project_id = _resolve_project_id(ctx, args.project)
    with _verified_store(ctx, project_id) as store:
        repo = SectionWorkspaceRepository(store)
        ref = _guard(
            lambda: repo.propose(
                payload,
                artifact_id=args.artifact_id,
                revision_id=args.revision_id,
                rights=rights,
                parent_revision_id=args.parent_revision_id,
            ),
            invalid_message=_WORKSPACE_REJECTED_MESSAGE,
        )
    return {
        "ref": ref,
        "kind": args.kind,
        "status": _STATUS,
        "private_content_included": False,
        **_FALSE_FLAGS,
    }


def drafting_show(args: argparse.Namespace, ctx: Any) -> dict[str, Any]:
    """Read one exact workspace revision; safe counts unless ``--content``."""
    project_id = _resolve_project_id(ctx, args.project)
    ref = _ref(project_id, args.artifact_id, args.revision_id)
    current = not args.historical
    with _verified_store(ctx, project_id) as store:
        repo = SectionWorkspaceRepository(store)
        payload = _guard(
            lambda: repo.read(ref, current=current),
            invalid_message=_WORKSPACE_READ_MESSAGE,
        )
    result = payload["result"]
    summary: dict[str, Any] = {
        "ref": ref,
        "kind": args.kind,
        "status": _STATUS,
        "current": current,
        "section_id": result["section_id"],
        "outline_ref": result["outline_ref"],
        "planner_role": result["planner"]["role"],
        "writer_role": result["writer"]["role"],
        "distinct_sessions": bool(result.get("distinct_sessions")),
        "grants_execution": bool(result.get("grants_execution")),
        "private_content_included": False,
        **_FALSE_FLAGS,
    }
    if args.content:
        summary["private_content_included"] = True
        summary["payload"] = payload
    return summary


def drafting_package(args: argparse.Namespace, ctx: Any) -> dict[str, Any]:
    """Assemble one section's drafting package; safe counts unless ``--content``."""
    project_id = _resolve_project_id(ctx, args.project)
    outline_ref = _ref(
        project_id, args.outline_artifact_id, args.outline_revision_id
    )
    with _verified_store(ctx, project_id) as store:
        repo = DraftingPackageRepository(store)
        package = _guard(
            lambda: repo.build(outline_ref, args.section_id),
            invalid_message=_PACKAGE_REJECTED_MESSAGE,
        )
    summary: dict[str, Any] = {
        "outline_ref": package["outline_ref"],
        "section_id": package["section_id"],
        "document_type": package["document_type"],
        "status": _STATUS,
        "evidence_count": len(package["evidence"]),
        "citation_count": len(package["citations"]),
        "original_source_count": len(package["methods"]["original_sources"]),
        "limitation_count": len(package["limitations"]),
        "authority": package["authority"],
        "private_content_included": False,
        **_FALSE_FLAGS,
    }
    if args.content:
        summary["private_content_included"] = True
        summary["package"] = package
    return summary


# --------------------------------------------------------------------------- #
# dispatch
# --------------------------------------------------------------------------- #
DRAFTING_COMMANDS: dict[str, Callable[[argparse.Namespace, Any], Any]] = {
    "propose": drafting_propose,
    "show": drafting_show,
    "package": drafting_package,
}


def dispatch_drafting(args: argparse.Namespace, ctx: Any) -> object | None:
    """Route one parsed ``drafting`` invocation; ``None`` if unknown."""
    handler = DRAFTING_COMMANDS.get(getattr(args, "drafting_command", None))
    return None if handler is None else handler(args, ctx)
