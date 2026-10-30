"""Thin local CLI surface for the durable Step 13 citation-origin tracing workflow.

This module adds the root ``tracing`` command group over the already integrated
:class:`~review_article_agent.tracing.workflow.OriginTraceRepository` and the
Step 11/12 state it consumes. It is a **deterministic, local, read/record**
surface: it never dispatches a model, creates a session, approves a gate,
exports, or reaches the network.

* ``tracing run --trace-id ...`` -- deterministically derive the claim/source
  graph from the **real** draft, its approved drafting package, and the recorded
  section-review checks (via
  :func:`~review_article_agent.tracing.inputs.claims_and_sources`), resolve the
  acquisition port over the project's already acquired sources (via
  :func:`~review_article_agent.tracing.acquisition_port.make_acquire_port`; no
  fetch, no network), and record one resumable trace revision. ``--policy-file``
  supplies the required per-project limits; without it the trace is
  ``configuration_required`` (no acquisition).
* ``tracing show --trace-id ...`` -- read the latest persisted trace state back as
  a safe structural summary; ``--content`` is the explicit private opt-in.
* ``tracing coverage --trace-id ...`` -- read the read-only
  :func:`~review_article_agent.tracing.workflow.origin_coverage` audit.

Privacy/safety rules mirror :mod:`review_article_agent.cli.drafting`: projects are
opened through the registry with ``expected_project_id`` and closed on every path;
side-inputs are read from a bounded regular file; and every failure raises
:class:`TracingControlError` with a stable category
(``validation``/``conflict``/``storage``) and a static message that never reflects
a path, id, source text, or exception text.
"""

from __future__ import annotations

import argparse
from typing import Any, Callable

from ..domain.errors import ReviewAgentError, StoreError
from ..domain.records import RecordValidationError
from ..drafting.workflow import DraftingWorkflowRepository
from ..review.workflow import SectionReviewRepository
from ..storage import ArtifactConflictError
from ..tracing.acquisition_port import make_acquire_port
from ..tracing.inputs import claims_and_sources
from ..tracing.workflow import OriginTraceRepository, origin_coverage
from .parsing import _read_object, _resolve_project_id, _verified_store

__all__ = [
    "TracingControlError",
    "build_tracing_parser",
    "dispatch_tracing",
]

_CATEGORY_VALIDATION = "validation"
_CATEGORY_CONFLICT = "conflict"
_CATEGORY_STORAGE = "storage"

_RUN_REJECTED_MESSAGE = (
    "the tracing request was rejected (a malformed or out-of-bounds identifier, a "
    "draft/package/review reference that is not the current approved state, a "
    "malformed rights record or policy, or a stored document that no longer "
    "re-derives from durable rows). Nothing was written."
)
_NOT_FOUND_MESSAGE = (
    "no origin trace with that id exists in this project; nothing was read or "
    "changed."
)
_READ_MESSAGE = (
    "the origin trace could not be read (a missing revision, a failed "
    "content-integrity check, or a stored document that no longer re-derives from "
    "durable rows)."
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


class TracingControlError(Exception):
    """A tracing command failed in a typed way.

    ``category`` is ``"validation"``, ``"conflict"``, or ``"storage"``; the
    message is always a static constant authored in this module.
    """

    def __init__(self, category: str, message: str) -> None:
        super().__init__(message)
        self.category = category


# --------------------------------------------------------------------------- #
# argument tree
# --------------------------------------------------------------------------- #
def build_tracing_parser(sub: "argparse._SubParsersAction") -> None:
    """Register the root ``tracing`` command group on the top-level parser."""
    tracing = sub.add_parser(
        "tracing",
        help=(
            "derive and record citation-origin traces from the real draft/package/"
            "review state; read a trace and its coverage (local only; no network)"
        ),
    )
    actions = tracing.add_subparsers(
        dest="tracing_command", required=True, metavar="TRACING_COMMAND"
    )

    p_run = actions.add_parser(
        "run",
        help=(
            "deterministically derive and record one origin trace from the real "
            "draft/package/review state (no network)"
        ),
    )
    p_run.add_argument("--project", metavar="ID")
    p_run.add_argument("--trace-id", required=True, metavar="ID")
    p_run.add_argument("--draft-artifact-id", required=True, metavar="ID")
    p_run.add_argument("--draft-revision-id", required=True, metavar="ID")
    p_run.add_argument(
        "--review-id",
        metavar="ID",
        help="the recorded section review whose checks annotate the trace",
    )
    p_run.add_argument(
        "--policy-file",
        metavar="PATH",
        help=(
            "strict JSON object with the four required tracing limits; without it "
            "the trace is configuration_required and nothing is acquired"
        ),
    )
    p_run.add_argument(
        "--rights-file",
        required=True,
        metavar="PATH",
        help="explicit caller RightsRecord JSON bound to each recorded revision",
    )

    p_show = actions.add_parser(
        "show", help="read one origin trace's safe state summary (--content to opt in)"
    )
    p_show.add_argument("--project", metavar="ID")
    p_show.add_argument("--trace-id", required=True, metavar="ID")
    p_show.add_argument(
        "--content",
        action="store_true",
        help="explicitly include the private raw trace state (claim graphs, warnings)",
    )

    p_cov = actions.add_parser(
        "coverage",
        help="read one origin trace's read-only coverage audit (never edits)",
    )
    p_cov.add_argument("--project", metavar="ID")
    p_cov.add_argument("--trace-id", required=True, metavar="ID")
    p_cov.add_argument(
        "--content",
        action="store_true",
        help="explicitly include the private raw trace state alongside the audit",
    )


# --------------------------------------------------------------------------- #
# shared helpers
# --------------------------------------------------------------------------- #
def _guard(call: Callable[[], Any], *, invalid_message: str) -> Any:
    """Run a repository call, converting every failure to a static typed error."""
    try:
        return call()
    except TracingControlError:
        raise
    except ArtifactConflictError:
        raise TracingControlError(_CATEGORY_CONFLICT, _CONFLICT_MESSAGE) from None
    except StoreError:
        raise TracingControlError(_CATEGORY_STORAGE, _STORAGE_MESSAGE) from None
    except OSError:
        raise TracingControlError(_CATEGORY_STORAGE, _STORAGE_MESSAGE) from None
    except (
        ReviewAgentError,
        RecordValidationError,
        ValueError,
        TypeError,
        KeyError,
        AttributeError,
        StopIteration,
    ):
        raise TracingControlError(_CATEGORY_VALIDATION, invalid_message) from None


def _ref(project_id: str, artifact_id: str, revision_id: str) -> dict:
    return {
        "project_id": project_id,
        "artifact_id": artifact_id,
        "revision_id": revision_id,
    }


def _rights_factory(template: dict, project_id: str) -> Callable[[str, str], dict]:
    """Rebind a rights-record template to each exact revision a workflow records.

    The tracing workflow assigns its own opaque revision ids, so a static
    ``--rights-file`` cannot pre-bind the subject. The template supplies the
    operations/basis/restrictions/privacy; the subject and ``rights_id`` are
    rebound to the exact ``(artifact_id, revision_id)`` the repository persists.
    """

    def factory(artifact_id: str, revision_id: str) -> dict:
        record = dict(template)
        record["project_id"] = project_id
        record["rights_id"] = f"RIGHTS-{revision_id}"
        record["record_revision"] = 1
        record["subject"] = {
            "kind": "source_revision",
            "project_id": project_id,
            "artifact_id": artifact_id,
            "revision_id": revision_id,
        }
        return record

    return factory


def _trace_summary(state: dict, *, content: bool) -> dict[str, Any]:
    """A safe structural projection of one trace state (never raw content)."""
    coverage = origin_coverage(state)
    summary: dict[str, Any] = {
        "trace_id": state.get("trace_id"),
        "status": state.get("status"),
        "claim_count": len(state.get("claims") or []),
        "counts": dict(coverage["counts"]),
        "acquire_calls": state.get("acquire_calls", 0),
        "project_sources_used": state.get("project_sources_used", 0),
        "invalidated_count": len(state.get("invalidated") or []),
        "complete": coverage["complete"],
        "export_ready": coverage["export_ready"],
        "edits_applied": coverage["edits_applied"],
        "requires_human_approval_count": len(coverage["requires_human_approval"]),
        "blocker_count": len(coverage["blockers"]),
        "private_content_included": False,
        **_FALSE_FLAGS,
    }
    if content:
        summary["private_content_included"] = True
        summary["payload"] = state
    return summary


def _read_trace(ctx: Any, project_id: str, trace_id: str) -> dict:
    with _verified_store(ctx, project_id) as store:
        repo = OriginTraceRepository(store)
        state = _guard(lambda: repo.read(trace_id), invalid_message=_READ_MESSAGE)
    if state is None:
        raise TracingControlError(_CATEGORY_VALIDATION, _NOT_FOUND_MESSAGE)
    return state


# --------------------------------------------------------------------------- #
# handlers
# --------------------------------------------------------------------------- #
def tracing_run(args: argparse.Namespace, ctx: Any) -> dict[str, Any]:
    """Derive and record one origin trace from real draft/package/review state."""
    project_id = _resolve_project_id(ctx, args.project)
    rights = _rights_factory(
        _read_object(args.rights_file, what="rights"), project_id
    )
    policy = _read_object(args.policy_file, what="policy") if args.policy_file else None
    draft_ref = _ref(project_id, args.draft_artifact_id, args.draft_revision_id)
    with _verified_store(ctx, project_id) as store:
        workflow = DraftingWorkflowRepository(store)
        draft_env = _guard(
            lambda: workflow.read(draft_ref, "draft"),
            invalid_message=_RUN_REJECTED_MESSAGE,
        )
        draft = draft_env["result"]
        package = draft_env["package"]
        review_checks: list = []
        if args.review_id:
            review_state = _guard(
                lambda: SectionReviewRepository(store).read(args.review_id),
                invalid_message=_RUN_REJECTED_MESSAGE,
            )
            review_checks = (review_state or {}).get("checks") or []
        claims, sources, source_digests = claims_and_sources(
            store, draft=draft, package=package
        )
        acquire = make_acquire_port(store)
        trace = OriginTraceRepository(store)
        state = _guard(
            lambda: trace.run(
                args.trace_id,
                claims=claims,
                sources=sources,
                policy=policy,
                review_checks=review_checks,
                acquire=acquire,
                rights=rights,
                source_digests=source_digests,
            ),
            invalid_message=_RUN_REJECTED_MESSAGE,
        )
    summary = _trace_summary(state, content=False)
    summary["trace_id"] = args.trace_id
    return summary


def tracing_show(args: argparse.Namespace, ctx: Any) -> dict[str, Any]:
    """Read one origin trace's safe state summary; raw state only with --content."""
    project_id = _resolve_project_id(ctx, args.project)
    state = _read_trace(ctx, project_id, args.trace_id)
    return _trace_summary(state, content=args.content)


def tracing_coverage(args: argparse.Namespace, ctx: Any) -> dict[str, Any]:
    """Read one origin trace's read-only coverage audit (never edits)."""
    project_id = _resolve_project_id(ctx, args.project)
    state = _read_trace(ctx, project_id, args.trace_id)
    coverage = origin_coverage(state)
    summary: dict[str, Any] = {
        "trace_id": coverage["trace_id"],
        "counts": dict(coverage["counts"]),
        "requires_human_approval": list(coverage["requires_human_approval"]),
        "blockers": list(coverage["blockers"]),
        "complete": coverage["complete"],
        "export_ready": coverage["export_ready"],
        "edits_applied": coverage["edits_applied"],
        "private_content_included": False,
        **_FALSE_FLAGS,
    }
    if args.content:
        summary["private_content_included"] = True
        summary["payload"] = state
    return summary


# --------------------------------------------------------------------------- #
# dispatch
# --------------------------------------------------------------------------- #
TRACING_COMMANDS: dict[str, Callable[[argparse.Namespace, Any], Any]] = {
    "run": tracing_run,
    "show": tracing_show,
    "coverage": tracing_coverage,
}


def dispatch_tracing(args: argparse.Namespace, ctx: Any) -> object | None:
    """Route one parsed ``tracing`` invocation; ``None`` if unknown."""
    handler = TRACING_COMMANDS.get(getattr(args, "tracing_command", None))
    return None if handler is None else handler(args, ctx)
