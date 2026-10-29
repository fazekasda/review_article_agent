"""Minimal local CLI surface for the durable Step 10 synthesis workflow (S10-CLI).

This module adds only the root ``synthesis`` command group over the already
integrated
:class:`~review_article_agent.synthesis.workflow.SynthesisWorkflowRepository`.
It is a **local, read/propose** surface: it never dispatches a model, executes a
runner, approves a gate, exports, or reaches the network. A stored revision only
means a proposal exists -- it is **never** an approval and never authorizes
execution, export, or scientific truth.

* ``synthesis propose`` -- write one *inert* ``synthesis`` / ``framework`` /
  ``outline`` revision from an explicit input envelope, an explicit caller rights
  record, explicit artifact/revision ids, and an optional parent revision.
* ``synthesis show`` -- read one exact current revision back (safe structural
  counts; ``--content`` is the explicit private opt-in for the raw payload). The
  result names the exact owner gate (``gate_kind`` / ``gate_id``) to use with
  ``gate prepare`` / ``gate confirm``.
* ``synthesis accepted`` -- run the blueprint gate for one exact ``outline``
  revision (safe counts and approved refs by default; ``--content`` includes the
  full private accepted package).
* ``synthesis impact`` -- read the stored advisory impact report for one exact
  revision (safe impacted/unaffected section counts by default; ``--content``
  includes the full report).

Privacy/safety rules mirror :mod:`review_article_agent.cli.extraction`: projects
are opened through the registry with ``expected_project_id`` and closed on every
path; side-inputs are read from a bounded regular file that never follows a final
symlink; and every failure raises :class:`SynthesisControlError` with a stable
category (``validation``/``conflict``/``storage``) and a static message that
never reflects a path, id, rights value, instruction, content, or exception text.
By default no memo, statement text, definition, rationale, or accepted package is
emitted.
"""

from __future__ import annotations

import argparse
from typing import Any, Callable

from ..domain.errors import ReviewAgentError, StoreError
from ..storage import ArtifactConflictError
from ..synthesis.workflow import (
    GATE_FRAMEWORK,
    GATE_FRAMEWORK_KIND,
    GATE_OUTLINE,
    GATE_OUTLINE_KIND,
    GATE_SYNTHESIS,
    GATE_SYNTHESIS_KIND,
    SynthesisWorkflowRepository,
)
from .parsing import _read_object, _resolve_project_id, _verified_store

__all__ = [
    "SynthesisControlError",
    "build_synthesis_parser",
    "dispatch_synthesis",
]

_CATEGORY_VALIDATION = "validation"
_CATEGORY_CONFLICT = "conflict"
_CATEGORY_STORAGE = "storage"

_KINDS = ("synthesis", "framework", "outline")

#: Exact named owner gate ``(gate_kind, gate_id)`` per proposal kind.
_GATES = {
    "synthesis": (GATE_SYNTHESIS_KIND, GATE_SYNTHESIS),
    "framework": (GATE_FRAMEWORK_KIND, GATE_FRAMEWORK),
    "outline": (GATE_OUTLINE_KIND, GATE_OUTLINE),
}

_REJECTED_MESSAGE = (
    "the synthesis request was rejected (a malformed or out-of-bounds identifier, "
    "artifact/revision reference, rights record, evidence dependency, or workflow "
    "document). Nothing was written."
)
_NOT_AUTHORIZED_MESSAGE = (
    "the blueprint gate is not satisfied for this outline (a missing named approval, "
    "a blocking or unclassified result, or a stale dependency); nothing was "
    "authorized."
)
_CONFLICT_MESSAGE = (
    "the request conflicts with existing immutable state; nothing was overwritten."
)
_STORAGE_MESSAGE = "a local storage operation failed; nothing was changed."

#: Non-authorizing constants carried on every output.
_FALSE_FLAGS = {
    "approved": False,
    "authorizes_execution": False,
    "authorizes_export": False,
    "scientific_validation": False,
}


class SynthesisControlError(Exception):
    """A synthesis command failed in a typed way.

    ``category`` is ``"validation"``, ``"conflict"``, or ``"storage"``; the
    message is always a static constant authored in this module.
    """

    def __init__(self, category: str, message: str) -> None:
        super().__init__(message)
        self.category = category


# --------------------------------------------------------------------------- #
# argument tree
# --------------------------------------------------------------------------- #
def build_synthesis_parser(sub: "argparse._SubParsersAction") -> None:
    """Register the root ``synthesis`` command group on the top-level parser."""
    synthesis = sub.add_parser(
        "synthesis",
        help=(
            "propose and read immutable synthesis/framework/outline revisions "
            "(local only; no dispatch, no approval, no export)"
        ),
    )
    actions = synthesis.add_subparsers(
        dest="synthesis_command", required=True, metavar="SYNTHESIS_COMMAND"
    )

    p_propose = actions.add_parser(
        "propose", help="write one inert synthesis/framework/outline revision"
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
            "read one exact current revision (safe structural counts; --content to "
            "opt in). The result names the exact owner gate (gate_kind/gate_id) to "
            "use with `gate prepare` / `gate confirm`"
        ),
    )
    p_show.add_argument("--project", metavar="ID")
    p_show.add_argument("--kind", required=True, choices=_KINDS)
    p_show.add_argument("--artifact-id", required=True, metavar="ID")
    p_show.add_argument("--revision-id", required=True, metavar="ID")
    p_show.add_argument(
        "--content",
        action="store_true",
        help="explicitly include the private raw payload (memo, statement text, definitions)",
    )

    p_accepted = actions.add_parser(
        "accepted",
        help="run the blueprint gate for one exact outline revision",
    )
    p_accepted.add_argument("--project", metavar="ID")
    p_accepted.add_argument("--artifact-id", required=True, metavar="ID")
    p_accepted.add_argument("--revision-id", required=True, metavar="ID")
    p_accepted.add_argument(
        "--content",
        action="store_true",
        help="explicitly include the full private accepted synthesis/framework/outline package",
    )

    p_impact = actions.add_parser(
        "impact",
        help="read the stored advisory impact report for one exact revision",
    )
    p_impact.add_argument("--project", metavar="ID")
    p_impact.add_argument("--kind", required=True, choices=("synthesis", "framework", "outline"))
    p_impact.add_argument("--artifact-id", required=True, metavar="ID")
    p_impact.add_argument("--revision-id", required=True, metavar="ID")
    p_impact.add_argument(
        "--content",
        action="store_true",
        help="explicitly include the full private impact report (changed ids, dependent refs)",
    )


# --------------------------------------------------------------------------- #
# shared helpers
# --------------------------------------------------------------------------- #
def _guard(call: Callable[[], Any], *, invalid_message: str) -> Any:
    """Run a repository call, converting every failure to a static typed error."""
    try:
        return call()
    except SynthesisControlError:
        raise
    except ArtifactConflictError:
        raise SynthesisControlError(_CATEGORY_CONFLICT, _CONFLICT_MESSAGE) from None
    except StoreError:
        raise SynthesisControlError(_CATEGORY_STORAGE, _STORAGE_MESSAGE) from None
    except OSError:
        raise SynthesisControlError(_CATEGORY_STORAGE, _STORAGE_MESSAGE) from None
    except (ReviewAgentError, ValueError, TypeError, KeyError, AttributeError, StopIteration):
        raise SynthesisControlError(_CATEGORY_VALIDATION, invalid_message) from None


def _ref(project_id: str, artifact_id: str, revision_id: str) -> dict:
    return {
        "project_id": project_id,
        "artifact_id": artifact_id,
        "revision_id": revision_id,
    }


#: Safe per-kind projections: structural counts only.  No memo, statement text,
#: definition, rationale, refs, or accepted package.
_SHOW_SUMMARIES: dict[str, Callable[[dict], dict]] = {
    "synthesis": lambda result: {
        "case_count": len(result.get("cases", [])),
        "finding_count": len(result.get("findings", [])),
        "correlation_group_count": len(result.get("correlation_groups", [])),
        "statement_count": len(result.get("statements", [])),
        "blocked_statement_count": sum(
            1 for s in result.get("statements", []) if s.get("blocked")
        ),
        "disposition_count": len(result.get("dispositions", [])),
        "independence_count": len(result.get("independence", [])),
        "gap_count": len(result.get("gaps", [])),
    },
    "framework": lambda result: {
        "concept_count": len(result.get("concepts", [])),
        "unclassified_concept_count": len(result.get("unclassified_concepts", [])),
        "relationship_count": len(result.get("relationships", [])),
        "hypothesis_count": len(result.get("hypotheses", [])),
        "open_question_count": len(result.get("open_questions", [])),
    },
    "outline": lambda result: {
        "section_count": len(result.get("sections", [])),
        "concept_dependency_count": sum(
            len(s.get("concept_dependencies", [])) for s in result.get("sections", [])
        ),
        "relationship_dependency_count": sum(
            len(s.get("relationship_dependencies", [])) for s in result.get("sections", [])
        ),
        "statement_dependency_count": sum(
            len(s.get("statement_dependencies", [])) for s in result.get("sections", [])
        ),
    },
}


# --------------------------------------------------------------------------- #
# handlers
# --------------------------------------------------------------------------- #
def synthesis_propose(args: argparse.Namespace, ctx: Any) -> dict[str, Any]:
    """Write one inert synthesis/framework/outline revision; never an approval."""
    payload = _read_object(args.input_file, what="synthesis")
    rights = _read_object(args.rights_file, what="rights")
    project_id = _resolve_project_id(ctx, args.project)
    with _verified_store(ctx, project_id) as store:
        repo = SynthesisWorkflowRepository(store)
        ref = _guard(
            lambda: repo.propose(
                args.kind,
                payload,
                artifact_id=args.artifact_id,
                revision_id=args.revision_id,
                rights=rights,
                parent_revision_id=args.parent_revision_id,
            ),
            invalid_message=_REJECTED_MESSAGE,
        )
    return {
        "ref": ref,
        "kind": args.kind,
        "gate_kind": _GATES[args.kind][0],
        "gate_id": _GATES[args.kind][1],
        "private_content_included": False,
        **_FALSE_FLAGS,
    }


def synthesis_show(args: argparse.Namespace, ctx: Any) -> dict[str, Any]:
    """Read one exact current revision; safe counts unless ``--content``."""
    project_id = _resolve_project_id(ctx, args.project)
    ref = _ref(project_id, args.artifact_id, args.revision_id)
    with _verified_store(ctx, project_id) as store:
        repo = SynthesisWorkflowRepository(store)
        payload = _guard(lambda: repo.read(ref, args.kind), invalid_message=_REJECTED_MESSAGE)
    summary: dict[str, Any] = {
        "ref": ref,
        "kind": args.kind,
        "gate_kind": _GATES[args.kind][0],
        "gate_id": _GATES[args.kind][1],
        "private_content_included": False,
        **_FALSE_FLAGS,
    }
    summary.update(_SHOW_SUMMARIES[args.kind](payload["result"]))
    if args.content:
        summary["private_content_included"] = True
        summary["payload"] = payload
    return summary


def synthesis_accepted(args: argparse.Namespace, ctx: Any) -> dict[str, Any]:
    """Run the blueprint gate for one outline; safe counts unless ``--content``."""
    project_id = _resolve_project_id(ctx, args.project)
    ref = _ref(project_id, args.artifact_id, args.revision_id)
    with _verified_store(ctx, project_id) as store:
        repo = SynthesisWorkflowRepository(store)
        result = _guard(
            lambda: repo.require_for_blueprints(ref),
            invalid_message=_NOT_AUTHORIZED_MESSAGE,
        )
    summary: dict[str, Any] = {
        "accepted": bool(result["accepted"]),
        "status": "accepted" if result["accepted"] else "not_accepted",
        "outline_ref": result["outline_ref"],
        "framework_ref": result["framework_ref"],
        "synthesis_ref": result["synthesis_ref"],
        "audit_ref": result["audit_ref"],
        "gates_satisfied": list(result["gates_satisfied"]),
        "authority": result["authority"],
        "notice": result["notice"],
        "outline_section_count": len(result["outline"]["sections"]),
        "framework_concept_count": len(result["framework"]["concepts"]),
        "synthesis_statement_count": len(result["synthesis"]["statements"]),
        "private_content_included": False,
        **_FALSE_FLAGS,
    }
    if args.content:
        summary["private_content_included"] = True
        summary["outline"] = result["outline"]
        summary["framework"] = result["framework"]
        summary["synthesis"] = result["synthesis"]
    return summary


def synthesis_impact(args: argparse.Namespace, ctx: Any) -> dict[str, Any]:
    """Read the stored advisory impact report; safe counts unless ``--content``."""
    project_id = _resolve_project_id(ctx, args.project)
    ref = _ref(project_id, args.artifact_id, args.revision_id)
    with _verified_store(ctx, project_id) as store:
        repo = SynthesisWorkflowRepository(store)
        impact = _guard(
            lambda: repo.read_impact(ref, args.kind), invalid_message=_REJECTED_MESSAGE
        )
    summary: dict[str, Any] = {
        "ref": ref,
        "kind": args.kind,
        "impact_present": impact is not None,
        "private_content_included": False,
        **_FALSE_FLAGS,
    }
    if impact is not None:
        summary["parent_ref"] = impact.get("parent_ref")
        summary["global_changed"] = bool(impact.get("global_changed"))
        summary["stale_gates"] = list(impact.get("stale_gates", []))
        if args.kind == "outline":
            summary["affected_section_count"] = len(impact.get("affected_sections", {}))
            summary["unaffected_section_count"] = len(impact.get("unaffected_sections", []))
        else:
            dependent = impact.get("dependent_outlines", [])
            summary["dependent_outline_count"] = len(dependent)
            summary["affected_section_count"] = sum(
                len(d.get("affected_sections", {})) for d in dependent
            )
            summary["unaffected_section_count"] = sum(
                len(d.get("unaffected_sections", [])) for d in dependent
            )
        if args.content:
            summary["private_content_included"] = True
            summary["impact"] = impact
    return summary


# --------------------------------------------------------------------------- #
# dispatch
# --------------------------------------------------------------------------- #
SYNTHESIS_COMMANDS: dict[str, Callable[[argparse.Namespace, Any], Any]] = {
    "propose": synthesis_propose,
    "show": synthesis_show,
    "accepted": synthesis_accepted,
    "impact": synthesis_impact,
}


def dispatch_synthesis(args: argparse.Namespace, ctx: Any) -> object | None:
    """Route one parsed ``synthesis`` invocation; ``None`` if unknown."""
    handler = SYNTHESIS_COMMANDS.get(getattr(args, "synthesis_command", None))
    return None if handler is None else handler(args, ctx)
