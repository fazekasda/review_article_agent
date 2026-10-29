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
  (with an optional parent revision). ``--kind workspace`` uses the workspace
  repository; ``--kind blueprint``/``draft``/``assembly`` pass the kind through to
  the workflow repository. An assembly is simply ``--kind assembly`` with an
  ``{outline_ref, draft_refs}`` input envelope -- there is no separate ``assemble``
  command.
* ``drafting show`` -- read one exact revision back (safe structural counts and
  opaque refs; ``--content`` is the explicit private opt-in for the raw payload,
  including draft/assembly text, Markdown, citations, and evidence).
  ``--historical`` inspects a non-current revision without live-gate authority. A
  blueprint result names its exact owner gate (``gate_kind`` / ``gate_id``); no
  draft/assembly gate is invented.
* ``drafting package`` -- assemble one approved outline section's reconstructible
  drafting package (safe counts by default; ``--content`` opts into the full
  private package).
* ``drafting accepted`` -- require **this blueprint's own** exact named owner gate
  (``artifact_revision`` / ``drafting.blueprint``) and read back the approved
  blueprint. Blueprint only (there is no draft/assembly approval); it is a **read**
  of an already-recorded approval and never creates one.
* ``drafting status`` -- read one current revision's structured status
  (``stale`` / ``reasons`` / ``pending`` and the non-export flags) for ``blueprint``/
  ``draft``/``assembly``; no raw content is emitted.

The CLI never fabricates a runtime session mapping: the planner/writer mappings
must already exist as app-owned rows (the app owns their creation); this module
adds no mapping command.

Privacy/safety rules mirror :mod:`review_article_agent.cli.synthesis`: projects
are opened through the registry with ``expected_project_id`` and closed on every
path; side-inputs are read from a bounded regular file that never follows a final
symlink; and every failure raises :class:`DraftingControlError` with a stable
category (``validation``/``conflict``/``storage``) and a static message that
never reflects a path, id, rights value, session/model detail, content, or
exception text. By default no source excerpt, prompt, session id, model detail,
draft/report text, Markdown, citation, or evidence is emitted.
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

#: The proposal/read kinds: ``workspace`` uses the workspace repository; the
#: rest pass through to the integrated workflow repository.
_KINDS = ("workspace", "blueprint", "draft", "assembly")

#: The workflow-backed kinds that expose a ``status`` read.
_STATUS_KINDS = ("blueprint", "draft", "assembly")

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
_DRAFTING_REJECTED_MESSAGE = (
    "the drafting request was rejected (a malformed or out-of-bounds identifier, "
    "reference, rights record, or workflow document; or the exact current approved "
    "chain, workspace, package, blueprint, or draft set is not consumable). Nothing "
    "was written."
)
_DRAFTING_READ_MESSAGE = (
    "the drafting revision could not be read (a missing revision, a non-current "
    "revision, a failed content-integrity check, or a stored snapshot that no "
    "longer re-derives from durable rows)."
)
_DRAFTING_STATUS_MESSAGE = (
    "the drafting status could not be read (a missing revision, a failed "
    "content-integrity check, or a stored snapshot that no longer re-derives from "
    "durable rows)."
)
_BLUEPRINT_NOT_APPROVED_MESSAGE = (
    "the blueprint's own named owner gate is not active and approved for this "
    "exact revision; nothing was authorized. Record the human approval with "
    "`gate prepare` / `gate confirm` first."
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
            "propose/read workspaces, blueprints, drafts, and assemblies; assemble "
            "one section package; read a blueprint's own owner gate (local only)"
        ),
    )
    actions = drafting.add_subparsers(
        dest="drafting_command", required=True, metavar="DRAFTING_COMMAND"
    )

    p_propose = actions.add_parser(
        "propose", help="write one inert workspace/blueprint/draft/assembly revision"
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
            "read one exact revision (safe counts; --content to opt in; "
            "--historical for a non-current revision). A blueprint names its exact "
            "owner gate (gate_kind/gate_id)"
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
        help="explicitly include the private raw payload (text, Markdown, citations, evidence)",
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
            "(blueprint only; read-only; never approves)"
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
        "status", help="read one current revision's gate/staleness status (no content)"
    )
    p_status.add_argument("--project", metavar="ID")
    p_status.add_argument("--kind", required=True, choices=_STATUS_KINDS)
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


def _draft_counts(draft: dict) -> dict[str, int]:
    """Safe structural counts for a validated draft result (no text/citations)."""
    paragraphs = draft.get("paragraphs") or []
    sentences = [s for p in paragraphs for s in (p.get("sentences") or [])]
    return {
        "paragraph_count": len(paragraphs),
        "sentence_count": len(sentences),
        "citation_count": len(draft.get("citations") or {}),
    }


def _assembly_counts(assembly: dict) -> dict[str, int]:
    """Safe structural counts for an assembly result (no text/Markdown/citations)."""
    sections = assembly.get("sections") or []
    paragraphs = [p for s in sections for p in (s.get("paragraphs") or [])]
    sentences = [s for p in paragraphs for s in (p.get("sentences") or [])]
    return {
        "section_count": len(sections),
        "paragraph_count": len(paragraphs),
        "sentence_count": len(sentences),
        "citation_count": len(assembly.get("citations") or {}),
    }


def _show_payload(kind: str, result: dict) -> dict[str, Any]:
    """Safe, per-kind structural projection of one result (never raw content)."""
    if kind == "blueprint":
        return {"section_id": result["section_id"], **_blueprint_counts(result)}
    if kind == "draft":
        return {"section_id": result["section_id"], **_draft_counts(result)}
    return _assembly_counts(result)


# --------------------------------------------------------------------------- #
# handlers
# --------------------------------------------------------------------------- #
def drafting_propose(args: argparse.Namespace, ctx: Any) -> dict[str, Any]:
    """Write one inert workspace/blueprint/draft/assembly revision; never an approval."""
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
                args.kind,
                payload,
                artifact_id=args.artifact_id,
                revision_id=args.revision_id,
                rights=rights,
                parent_revision_id=args.parent_revision_id,
            ),
            invalid_message=_DRAFTING_REJECTED_MESSAGE,
        )
    summary: dict[str, Any] = {
        "ref": ref,
        "kind": args.kind,
        "status": _STATUS,
        "private_content_included": False,
        **_FALSE_FLAGS,
    }
    if args.kind == "blueprint":
        # Only a blueprint has an exact named owner gate; no draft/assembly gate.
        summary["gate_kind"] = GATE_KIND
        summary["gate_id"] = GATE_ID
    return summary


def drafting_show(args: argparse.Namespace, ctx: Any) -> dict[str, Any]:
    """Read one exact revision; safe counts unless ``--content``."""
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
            lambda: repo.read(ref, args.kind, current=current),
            invalid_message=_DRAFTING_READ_MESSAGE,
        )
    result = payload["result"]
    summary = {
        "ref": ref,
        "kind": args.kind,
        "status": _STATUS,
        "current": current,
        **_show_payload(args.kind, result),
        "pending": list(result.get("pending") or []),
        "authorizes_export": False,
        "private_content_included": False,
        **_FALSE_FLAGS,
    }
    if args.kind == "blueprint":
        summary["gate_kind"] = GATE_KIND
        summary["gate_id"] = GATE_ID
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
    """Read one current revision's gate/staleness status; never raw content."""
    project_id = _resolve_project_id(ctx, args.project)
    ref = _ref(project_id, args.artifact_id, args.revision_id)
    with _verified_store(ctx, project_id) as store:
        repo = DraftingWorkflowRepository(store)
        report = _guard(
            lambda: repo.status(ref, args.kind),
            invalid_message=_DRAFTING_STATUS_MESSAGE,
        )
    summary: dict[str, Any] = {
        "kind": args.kind,
        "status": _STATUS,
        "stale": bool(report["stale"]),
        "reasons": list(report["reasons"]),
        "pending": list(report["pending"]),
        "provisional": bool(report["provisional"]),
        "authorizes_export": False,
        **_FALSE_FLAGS,
    }
    # Forward the per-kind safe refs/flags without assuming one kind's key set.
    for key in (
        "blueprint_ref",
        "draft_ref",
        "assembly_ref",
        "outline_ref",
        "gate_kind",
        "gate_id",
        "final",
    ):
        if key in report:
            summary[key] = report[key]
    return summary


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
