"""Minimal local CLI surface for the immutable extraction workflow (S9-CLI).

This module adds only the root ``extraction`` command group over the already
integrated :class:`~review_article_agent.extraction.workflow.ExtractionWorkflowRepository`.
It is a **local, read/propose** surface: it never dispatches a model, executes a
runner, approves a gate, or reaches the network. A stored revision only means a
proposal exists -- it is **never** an approval, and it never authorizes
extraction or synthesis.

* ``extraction propose`` -- write one *inert* ``config`` / ``pilot`` / ``batch`` /
  ``audit`` revision from an explicit side-input, an explicit caller rights
  record, an explicit artifact/revision id, and an optional parent revision.
* ``extraction show`` -- read one exact current extraction revision back (safe
  summary; ``--content`` is the explicit private opt-in for the raw payload).
* ``extraction accepted`` -- run the synthesis gate for one exact ``audit``
  revision (safe summary of ids/status/counts/guarantee; ``--content`` includes
  the accepted structured evidence detail).

Privacy/safety rules mirror :mod:`review_article_agent.cli.parsing`: projects are
opened through the registry with ``expected_project_id`` and closed on every
path; side-inputs are read from a bounded regular file that never follows a final
symlink; and every failure raises :class:`ExtractionControlError` with a stable
category (``validation``/``conflict``/``storage``) and a static message that
never reflects a path, id, rights value, instruction, model output, dispatch key,
or exception text. By default no prompt instructions, model configuration, or
accepted evidence bodies are emitted.
"""

from __future__ import annotations

import argparse
from typing import Any, Callable

from ..domain.errors import ReviewAgentError, StoreError
from ..extraction.workflow import ExtractionWorkflowRepository
from ..storage import ArtifactConflictError
from .parsing import _read_object, _resolve_project_id, _verified_store

__all__ = [
    "ExtractionControlError",
    "build_extraction_parser",
    "dispatch_extraction",
]

_CATEGORY_VALIDATION = "validation"
_CATEGORY_CONFLICT = "conflict"
_CATEGORY_STORAGE = "storage"

_KINDS = ("config", "pilot", "batch", "audit")

_REJECTED_MESSAGE = (
    "the extraction request was rejected (a malformed or out-of-bounds identifier, "
    "artifact/revision reference, rights record, or workflow document). Nothing was "
    "written."
)
_NOT_AUTHORIZED_MESSAGE = (
    "the extraction synthesis gate is not satisfied for this audit (a missing named "
    "approval, a blocking or paused workflow, or a stale dependency); nothing was "
    "authorized."
)
_CONFLICT_MESSAGE = (
    "the request conflicts with existing immutable state; nothing was overwritten."
)
_STORAGE_MESSAGE = "a local storage operation failed; nothing was changed."


class ExtractionControlError(Exception):
    """An extraction command failed in a typed way.

    ``category`` is ``"validation"``, ``"conflict"``, or ``"storage"``; the
    message is always a static constant authored in this module.
    """

    def __init__(self, category: str, message: str) -> None:
        super().__init__(message)
        self.category = category


# --------------------------------------------------------------------------- #
# argument tree
# --------------------------------------------------------------------------- #
def build_extraction_parser(sub: "argparse._SubParsersAction") -> None:
    """Register the root ``extraction`` command group on the top-level parser."""
    extraction = sub.add_parser(
        "extraction",
        help=(
            "propose and read immutable extraction config/pilot/batch/audit "
            "revisions, and inspect the accepted synthesis evidence; local only, "
            "no dispatch and no approval"
        ),
    )
    actions = extraction.add_subparsers(
        dest="extraction_command", required=True, metavar="EXTRACTION_COMMAND"
    )

    p_propose = actions.add_parser(
        "propose", help="write one inert extraction workflow revision"
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
        help="read one exact current extraction revision (safe summary; --content to opt in)",
    )
    p_show.add_argument("--project", metavar="ID")
    p_show.add_argument("--kind", required=True, choices=_KINDS)
    p_show.add_argument("--artifact-id", required=True, metavar="ID")
    p_show.add_argument("--revision-id", required=True, metavar="ID")
    p_show.add_argument(
        "--content",
        action="store_true",
        help="explicitly include the private raw payload (instructions/model config)",
    )

    p_accepted = actions.add_parser(
        "accepted",
        help="run the synthesis gate for one exact audit revision",
    )
    p_accepted.add_argument("--project", metavar="ID")
    p_accepted.add_argument("--artifact-id", required=True, metavar="ID")
    p_accepted.add_argument("--revision-id", required=True, metavar="ID")
    p_accepted.add_argument(
        "--content",
        action="store_true",
        help="explicitly include the accepted evidence detail (ids, limitations, escalations)",
    )


# --------------------------------------------------------------------------- #
# shared helpers
# --------------------------------------------------------------------------- #
def _guard(call: Callable[[], Any], *, invalid_message: str) -> Any:
    """Run a repository call, converting every failure to a static typed error."""
    try:
        return call()
    except ExtractionControlError:
        raise
    except ArtifactConflictError:
        raise ExtractionControlError(_CATEGORY_CONFLICT, _CONFLICT_MESSAGE) from None
    except StoreError:
        raise ExtractionControlError(_CATEGORY_STORAGE, _STORAGE_MESSAGE) from None
    except OSError:
        raise ExtractionControlError(_CATEGORY_STORAGE, _STORAGE_MESSAGE) from None
    except (ReviewAgentError, ValueError, TypeError, KeyError, AttributeError, StopIteration):
        raise ExtractionControlError(_CATEGORY_VALIDATION, invalid_message) from None


def _ref(project_id: str, artifact_id: str, revision_id: str) -> dict:
    return {
        "project_id": project_id,
        "artifact_id": artifact_id,
        "revision_id": revision_id,
    }


#: Safe per-kind projections: counts + safe structural identity only. No prompt
#: instructions, model configuration, or accepted evidence bodies.
_SHOW_SUMMARIES: dict[str, Callable[[dict], dict]] = {
    "config": lambda payload: {
        "config_schema_version": payload["config_schema_version"],
        "dimension_count": len(payload["appraisal_dimensions"]),
        "dimension_ids": [d["dimension_id"] for d in payload["appraisal_dimensions"]],
        "pilot_size": payload["pilot_size"],
        "random_size": payload["random_size"],
        "risk_size": payload["risk_size"],
        "parser": payload["parser"],
        "normalization": payload["normalization"],
    },
    "pilot": lambda payload: {
        "case_count": len(payload.get("cases", [])),
        "blocking_failure_count": len(payload.get("blocking_failures", [])),
        "mandatory_labels": list(payload.get("mandatory_labels", [])),
    },
    "batch": lambda payload: {
        "case_count": len(payload.get("cases", [])),
        "random_count": len(payload.get("random_ids", [])),
        "risk_count": len(payload.get("risk_ids", [])),
    },
    "audit": lambda payload: {
        "paused": bool(payload.get("paused", False)),
        "important_error_count": len(payload.get("important_errors", [])),
        "guarantee": payload.get("guarantee"),
    },
}


# --------------------------------------------------------------------------- #
# handlers
# --------------------------------------------------------------------------- #
def extraction_propose(args: argparse.Namespace, ctx: Any) -> dict[str, Any]:
    """Write one inert extraction revision; never an approval."""
    payload = _read_object(args.input_file, what="extraction")
    rights = _read_object(args.rights_file, what="rights")
    project_id = _resolve_project_id(ctx, args.project)
    with _verified_store(ctx, project_id) as store:
        repo = ExtractionWorkflowRepository(store)
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
        "approved": False,
        "private_content_included": False,
        "authorizes_extraction": False,
        "authorizes_synthesis": False,
    }


def extraction_show(args: argparse.Namespace, ctx: Any) -> dict[str, Any]:
    """Read one exact current extraction revision; safe summary unless ``--content``."""
    project_id = _resolve_project_id(ctx, args.project)
    ref = _ref(project_id, args.artifact_id, args.revision_id)
    with _verified_store(ctx, project_id) as store:
        repo = ExtractionWorkflowRepository(store)
        payload = _guard(
            lambda: repo.read(ref, args.kind), invalid_message=_REJECTED_MESSAGE
        )
    summary: dict[str, Any] = {
        "ref": ref,
        "kind": args.kind,
        "private_content_included": False,
        "authorizes_extraction": False,
        "authorizes_synthesis": False,
    }
    summary.update(_SHOW_SUMMARIES[args.kind](payload))
    if args.content:
        summary["private_content_included"] = True
        summary["payload"] = payload
    return summary


def extraction_accepted(args: argparse.Namespace, ctx: Any) -> dict[str, Any]:
    """Run the synthesis gate for one audit; safe summary unless ``--content``."""
    project_id = _resolve_project_id(ctx, args.project)
    ref = _ref(project_id, args.artifact_id, args.revision_id)
    with _verified_store(ctx, project_id) as store:
        repo = ExtractionWorkflowRepository(store)
        result = _guard(
            lambda: repo.require_for_synthesis(ref),
            invalid_message=_NOT_AUTHORIZED_MESSAGE,
        )
    summary: dict[str, Any] = {
        "audit_ref": result["audit_ref"],
        "batch_ref": result["batch_ref"],
        "config_ref": result["config_ref"],
        "pilot_ref": result["pilot_ref"],
        "acquisition_ref": result["acquisition_ref"],
        "accepted": bool(result["accepted"]),
        "status": "accepted" if result["accepted"] else "not_accepted",
        "case_count": len(result["case_ids"]),
        "audited_count": len(result["audited_ids"]),
        "gates_satisfied": list(result["gates_satisfied"]),
        "guarantee": result["guarantee"],
        "authority": result["authority"],
        "notice": result["notice"],
        "private_content_included": False,
        "authorizes_synthesis": True,
    }
    if args.content:
        summary["private_content_included"] = True
        summary["case_ids"] = list(result["case_ids"])
        summary["audited_ids"] = list(result["audited_ids"])
        summary["limitations"] = list(result["limitations"])
        summary["escalations"] = list(result["escalations"])
    return summary


# --------------------------------------------------------------------------- #
# dispatch
# --------------------------------------------------------------------------- #
EXTRACTION_COMMANDS: dict[str, Callable[[argparse.Namespace, Any], Any]] = {
    "propose": extraction_propose,
    "show": extraction_show,
    "accepted": extraction_accepted,
}


def dispatch_extraction(args: argparse.Namespace, ctx: Any) -> object | None:
    """Route one parsed ``extraction`` invocation; ``None`` if unknown."""
    handler = EXTRACTION_COMMANDS.get(getattr(args, "extraction_command", None))
    return None if handler is None else handler(args, ctx)
