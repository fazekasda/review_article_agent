"""Minimal local CLI surface for the durable Step 11 drafting workflow (S11-CLI).

This module adds the root ``drafting`` command group over the already integrated
:class:`~review_article_agent.drafting.workspace.SectionWorkspaceRepository`,
:class:`~review_article_agent.drafting.package.DraftingPackageRepository`, and
:class:`~review_article_agent.drafting.workflow.DraftingWorkflowRepository`. It is
a **local, private-by-default read/propose** surface: it never dispatches a
model, creates or executes a session, approves a gate, exports, or reaches the
network. A stored revision only means a proposal exists -- it is **never** an
approval and never authorizes execution, export, or scientific truth.

* ``drafting propose`` -- write one *inert* revision from an explicit input
  envelope, an explicit caller rights record, and explicit artifact/revision ids
  (with an optional parent revision). ``--kind workspace`` writes a per-section
  planner/writer workspace identity; ``--kind blueprint`` writes a section
  blueprint (over the exact current approved chain, the current workspace, and
  the reconstructible approved package).
* ``drafting show`` -- read one exact revision back (safe structural counts and
  opaque refs; ``--content`` is the explicit private opt-in for the raw payload).
  ``--historical`` inspects a non-current revision without live-gate authority.
  A blueprint result names the exact owner gate (``gate_kind`` / ``gate_id``) to
  use with ``gate prepare`` / ``gate confirm``; this command **never** approves.
* ``drafting package`` -- assemble one approved outline section's reconstructible
  drafting package (safe counts by default; ``--content`` opts into the full
  private package).
* ``drafting accepted`` -- require **this blueprint's own** exact named owner gate
  (``artifact_revision`` / ``drafting.blueprint``) and read back the approved
  blueprint (safe counts by default; ``--content`` opts into the full envelope).
  It is a **read** of an already-recorded approval: it never creates one, and a
  ``blueprint_approved`` result still authorizes neither execution nor export.
* ``drafting status`` -- read one current blueprint's structured status
  (``stale`` / ``reasons`` / ``pending`` and the non-export flags); no raw
  content is emitted.

Only the ``workspace`` and ``blueprint`` kinds are registered today; ``draft``/
``assembly`` kinds and the assembly command are deliberately left to the next
bounded task once those APIs are integrated. The CLI never fabricates a runtime
session mapping: the planner/writer mappings must already exist as app-owned rows
(the app owns their creation); this module adds no mapping command and never
guesses an unimplemented workflow import.

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
from ..drafting.workflow import (
    GATE_ID,
    GATE_KIND,
    DraftingWorkflowRepository,
)
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

#: The proposal/read kinds registered today. ``draft``/``assembly`` are
#: deliberately left to the next bounded task once those APIs are integrated.
_KINDS = ("workspace", "blueprint")

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
_BLUEPRINT_REJECTED_MESSAGE = (
    "the drafting blueprint request was rejected (a malformed or out-of-bounds "
    "identifier, workspace/section reference, rights record, or blueprint "
    "document; or the exact current approved chain, the current workspace, and "
    "the reconstructible approved package are not consumable). Nothing was "
    "written."
)
_BLUEPRINT_READ_MESSAGE = (
    "the drafting blueprint revision could not be read (a missing revision, a "
    "non-current revision, a failed content-integrity check, or a stored "
    "workspace/package that no longer re-derives from durable rows)."
)
_BLUEPRINT_NOT_APPROVED_MESSAGE = (
    "the blueprint's own named owner gate is not active and approved for this "
    "exact revision; nothing was authorized. Record the human approval with "
    "`gate prepare` / `gate confirm` first."
)
_BLUEPRINT_STATUS_MESSAGE = (
    "the drafting blueprint status could not be read (a missing revision, a "
    "failed content-integrity check, or a stored workspace/package that no longer "
    "re-derives from durable rows)."
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
            "propose and read per-section planner/writer workspaces and section "
            "blueprints, assemble one section's approved drafting package, and read "
            "one blueprint's own owner-gate approval/status (local only; no "
            "dispatch, no approval, no export)"
        ),
    )
    actions = drafting.add_subparsers(
        dest="drafting_command", required=True, metavar="DRAFTING_COMMAND"
    )

    p_propose = actions.add_parser(
        "propose", help="write one inert workspace/blueprint revision"
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
            "read one exact workspace/blueprint revision (safe structural counts; "
            "--content to opt in; --historical to inspect a non-current revision). A "
            "blueprint result names its exact owner gate (gate_kind/gate_id)"
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
        help="explicitly include the private raw payload (session/model pins; blueprint evidence)",
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

    p_accepted = actions.add_parser(
        "accepted",
        help=(
            "require this blueprint's own named owner gate and read it back "
            "(read-only; never approves; safe counts unless --content)"
        ),
    )
    p_accepted.add_argument("--project", metavar="ID")
    p_accepted.add_argument("--artifact-id", required=True, metavar="ID")
    p_accepted.add_argument("--revision-id", required=True, metavar="ID")
    p_accepted.add_argument(
        "--content",
        action="store_true",
        help="explicitly include the full private blueprint envelope (evidence, package)",
    )

    p_status = actions.add_parser(
        "status", help="read one current blueprint's gate/staleness status (no content)"
    )
    p_status.add_argument("--project", metavar="ID")
    p_status.add_argument("--kind", required=True, choices=("blueprint",))
    p_status.add_argument("--artifact-id", required=True, metavar="ID")
    p_status.add_argument("--revision-id", required=True, metavar="ID")


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


def _blueprint_counts(blueprint: dict) -> dict[str, int]:
    """Safe structural counts for a validated blueprint result (no raw content)."""
    paragraphs = blueprint.get("paragraphs") or []
    intents = [i for p in paragraphs for i in (p.get("intents") or [])]
    claims = [c for i in intents for c in (i.get("claims") or [])]
    return {
        "paragraph_count": len(paragraphs),
        "intent_count": len(intents),
        "claim_count": len(claims),
    }


# --------------------------------------------------------------------------- #
# handlers
# --------------------------------------------------------------------------- #
def drafting_propose(args: argparse.Namespace, ctx: Any) -> dict[str, Any]:
    """Write one inert workspace/blueprint revision; never an approval."""
    payload = _read_object(args.input_file, what="drafting input")
    rights = _read_object(args.rights_file, what="rights")
    project_id = _resolve_project_id(ctx, args.project)
    if args.kind == "workspace":
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
            "kind": "workspace",
            "status": _STATUS,
            "private_content_included": False,
            **_FALSE_FLAGS,
        }
    with _verified_store(ctx, project_id) as store:
        repo = DraftingWorkflowRepository(store)
        ref = _guard(
            lambda: repo.propose(
                "blueprint",
                payload,
                artifact_id=args.artifact_id,
                revision_id=args.revision_id,
                rights=rights,
                parent_revision_id=args.parent_revision_id,
            ),
            invalid_message=_BLUEPRINT_REJECTED_MESSAGE,
        )
    return {
        "ref": ref,
        "kind": "blueprint",
        "status": _STATUS,
        "gate_kind": GATE_KIND,
        "gate_id": GATE_ID,
        "private_content_included": False,
        **_FALSE_FLAGS,
    }


def drafting_show(args: argparse.Namespace, ctx: Any) -> dict[str, Any]:
    """Read one exact workspace/blueprint revision; safe counts unless ``--content``."""
    project_id = _resolve_project_id(ctx, args.project)
    ref = _ref(project_id, args.artifact_id, args.revision_id)
    current = not args.historical
    if args.kind == "workspace":
        with _verified_store(ctx, project_id) as store:
            repo = SectionWorkspaceRepository(store)
            payload = _guard(
                lambda: repo.read(ref, current=current),
                invalid_message=_WORKSPACE_READ_MESSAGE,
            )
        result = payload["result"]
        summary: dict[str, Any] = {
            "ref": ref,
            "kind": "workspace",
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
    with _verified_store(ctx, project_id) as store:
        repo = DraftingWorkflowRepository(store)
        payload = _guard(
            lambda: repo.read(ref, "blueprint", current=current),
            invalid_message=_BLUEPRINT_READ_MESSAGE,
        )
    result = payload["result"]
    summary = {
        "ref": ref,
        "kind": "blueprint",
        "status": _STATUS,
        "current": current,
        "gate_kind": GATE_KIND,
        "gate_id": GATE_ID,
        "section_id": result["section_id"],
        **_blueprint_counts(result),
        "pending": list(result.get("pending") or []),
        "authorizes_export": False,
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


def drafting_accepted(args: argparse.Namespace, ctx: Any) -> dict[str, Any]:
    """Require this blueprint's own named owner gate; read-only, never approves."""
    project_id = _resolve_project_id(ctx, args.project)
    ref = _ref(project_id, args.artifact_id, args.revision_id)
    with _verified_store(ctx, project_id) as store:
        repo = DraftingWorkflowRepository(store)
        result = _guard(
            lambda: repo.require_blueprint(ref),
            invalid_message=_BLUEPRINT_NOT_APPROVED_MESSAGE,
        )
    blueprint = result["result"]
    summary: dict[str, Any] = {
        "blueprint_ref": result["blueprint_ref"],
        "kind": "blueprint",
        "status": _STATUS,
        "gate_kind": result["gate_kind"],
        "gate_id": result["gate_id"],
        "blueprint_approved": bool(result["approved"]),
        "section_id": blueprint["section_id"],
        **_blueprint_counts(blueprint),
        "pending": list(blueprint.get("pending") or []),
        "authorizes_export": False,
        "private_content_included": False,
        **_FALSE_FLAGS,
    }
    if args.content:
        summary["private_content_included"] = True
        summary["blueprint"] = result
    return summary


def drafting_status(args: argparse.Namespace, ctx: Any) -> dict[str, Any]:
    """Read one current blueprint's gate/staleness status; never raw content."""
    project_id = _resolve_project_id(ctx, args.project)
    ref = _ref(project_id, args.artifact_id, args.revision_id)
    with _verified_store(ctx, project_id) as store:
        repo = DraftingWorkflowRepository(store)
        report = _guard(
            lambda: repo.status(ref, args.kind),
            invalid_message=_BLUEPRINT_STATUS_MESSAGE,
        )
    return {
        "blueprint_ref": report["blueprint_ref"],
        "kind": args.kind,
        "status": _STATUS,
        "gate_kind": report["gate_kind"],
        "gate_id": report["gate_id"],
        "stale": bool(report["stale"]),
        "reasons": list(report["reasons"]),
        "pending": list(report["pending"]),
        "provisional": bool(report["provisional"]),
        "authorizes_export": False,
        **_FALSE_FLAGS,
    }


# --------------------------------------------------------------------------- #
# dispatch
# --------------------------------------------------------------------------- #
DRAFTING_COMMANDS: dict[str, Callable[[argparse.Namespace, Any], Any]] = {
    "propose": drafting_propose,
    "show": drafting_show,
    "package": drafting_package,
    "accepted": drafting_accepted,
    "status": drafting_status,
}


def dispatch_drafting(args: argparse.Namespace, ctx: Any) -> object | None:
    """Route one parsed ``drafting`` invocation; ``None`` if unknown."""
    handler = DRAFTING_COMMANDS.get(getattr(args, "drafting_command", None))
    return None if handler is None else handler(args, ctx)
