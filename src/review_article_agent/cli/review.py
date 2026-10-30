"""Thin local CLI surface for the durable Step 12 section-review workflow.

This module adds the root ``review`` command group over the already integrated
:class:`~review_article_agent.review.workflow.SectionReviewRepository`. It is a
**read-only, private-by-default** surface: it never dispatches a model, creates a
session, approves a gate, exports, or reaches the network. A stored review
revision only means a critic/fact-check run recorded a result -- it is **never**
an approval and never authorizes execution, export, or scientific truth.

* ``review show --review-id ...`` -- read the latest persisted review state back
  as a safe structural summary (status, phase, round counters, finding/check
  counts, the required human action); ``--content`` is the explicit private
  opt-in for the raw state (draft text, findings, checks).
* ``review status --review-id ...`` -- read the same state's focused status
  summary (rounds used, status, required human action); ``--content`` opts in to
  the raw state.

Model-backed review operations (critic/repair) are submitted as worker jobs
through a separate ``generation run submit`` command; this module implements only
the deterministic read/status view.

Privacy/safety rules mirror :mod:`review_article_agent.cli.drafting`: projects
are opened through the registry with ``expected_project_id`` and closed on every
path; and every failure raises :class:`ReviewControlError` with a stable category
(``validation``/``conflict``/``storage``) and a static message that never reflects
a path, id, or exception text.
"""

from __future__ import annotations

import argparse
from typing import Any, Callable

from ..domain.errors import ReviewAgentError, StoreError
from ..domain.records import RecordValidationError
from ..review.workflow import SectionReviewRepository
from ..storage import ArtifactConflictError
from .parsing import _resolve_project_id, _verified_store

__all__ = [
    "ReviewControlError",
    "build_review_parser",
    "dispatch_review",
]

_CATEGORY_VALIDATION = "validation"
_CATEGORY_CONFLICT = "conflict"
_CATEGORY_STORAGE = "storage"

_NOT_FOUND_MESSAGE = (
    "no section review with that id exists in this project; nothing was read or "
    "changed."
)
_READ_MESSAGE = (
    "the section review could not be read (a missing revision, a failed "
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


class ReviewControlError(Exception):
    """A review command failed in a typed way.

    ``category`` is ``"validation"``, ``"conflict"``, or ``"storage"``; the
    message is always a static constant authored in this module.
    """

    def __init__(self, category: str, message: str) -> None:
        super().__init__(message)
        self.category = category


# --------------------------------------------------------------------------- #
# argument tree
# --------------------------------------------------------------------------- #
def build_review_parser(sub: "argparse._SubParsersAction") -> None:
    """Register the root ``review`` command group on the top-level parser."""
    review = sub.add_parser(
        "review",
        help=(
            "read a durable section review's status and state summary (local "
            "only; no dispatch, no approval, no export)"
        ),
    )
    actions = review.add_subparsers(
        dest="review_command", required=True, metavar="REVIEW_COMMAND"
    )

    p_show = actions.add_parser(
        "show",
        help=(
            "read one section review's safe state summary (counts; --content for "
            "the raw state)"
        ),
    )
    p_show.add_argument("--project", metavar="ID")
    p_show.add_argument("--review-id", required=True, metavar="ID")
    p_show.add_argument(
        "--content",
        action="store_true",
        help="explicitly include the private raw state (draft text, findings, checks)",
    )

    p_status = actions.add_parser(
        "status",
        help=(
            "read one section review's status (rounds used, status, required "
            "human action)"
        ),
    )
    p_status.add_argument("--project", metavar="ID")
    p_status.add_argument("--review-id", required=True, metavar="ID")
    p_status.add_argument(
        "--content",
        action="store_true",
        help="explicitly include the private raw state (draft text, findings, checks)",
    )


# --------------------------------------------------------------------------- #
# shared helpers
# --------------------------------------------------------------------------- #
def _guard(call: Callable[[], Any], *, invalid_message: str) -> Any:
    """Run a repository call, converting every failure to a static typed error."""
    try:
        return call()
    except ReviewControlError:
        raise
    except ArtifactConflictError:
        raise ReviewControlError(_CATEGORY_CONFLICT, _CONFLICT_MESSAGE) from None
    except StoreError:
        raise ReviewControlError(_CATEGORY_STORAGE, _STORAGE_MESSAGE) from None
    except OSError:
        raise ReviewControlError(_CATEGORY_STORAGE, _STORAGE_MESSAGE) from None
    except (
        ReviewAgentError,
        RecordValidationError,
        ValueError,
        TypeError,
        KeyError,
        AttributeError,
        StopIteration,
    ):
        raise ReviewControlError(_CATEGORY_VALIDATION, invalid_message) from None


def _state_summary(state: dict, *, content: bool) -> dict[str, Any]:
    """A safe structural projection of one review state (never raw content)."""
    rounds = state.get("rounds_used") or {}
    validation = state.get("validation") or {}
    engine = state.get("engine") or {}
    summary: dict[str, Any] = {
        "review_id": state.get("review_id"),
        "status": state.get("status"),
        "phase": state.get("phase"),
        "rounds_used": {
            "critique": rounds.get("critique", 0),
            "fact_check": rounds.get("fact_check", 0),
        },
        "critic_calls": state.get("critic_calls", 0),
        "repair_calls": state.get("repair_calls", 0),
        "finding_count": len(state.get("findings") or []),
        "check_count": len(state.get("checks") or []),
        "violation_count": len(state.get("violations") or []),
        "required_human_action": state.get("required_human_action"),
        "validation_status": validation.get("status"),
        "validation_read_only": bool(validation.get("read_only")),
        "different_model": bool(engine.get("different_model")),
        "independent_human_verification": bool(
            engine.get("independent_human_verification")
        ),
        "private_content_included": False,
        **_FALSE_FLAGS,
    }
    if content:
        summary["private_content_included"] = True
        summary["payload"] = state
    return summary


# --------------------------------------------------------------------------- #
# handlers
# --------------------------------------------------------------------------- #
def review_show(args: argparse.Namespace, ctx: Any) -> dict[str, Any]:
    """Read one section review's safe state summary; raw state only with --content."""
    project_id = _resolve_project_id(ctx, args.project)
    with _verified_store(ctx, project_id) as store:
        repo = SectionReviewRepository(store)
        state = _guard(lambda: repo.read(args.review_id), invalid_message=_READ_MESSAGE)
    if state is None:
        raise ReviewControlError(_CATEGORY_VALIDATION, _NOT_FOUND_MESSAGE)
    return _state_summary(state, content=args.content)


def review_status(args: argparse.Namespace, ctx: Any) -> dict[str, Any]:
    """Read one section review's status summary; raw state only with --content."""
    project_id = _resolve_project_id(ctx, args.project)
    with _verified_store(ctx, project_id) as store:
        repo = SectionReviewRepository(store)
        state = _guard(lambda: repo.read(args.review_id), invalid_message=_READ_MESSAGE)
    if state is None:
        raise ReviewControlError(_CATEGORY_VALIDATION, _NOT_FOUND_MESSAGE)
    summary = _state_summary(state, content=args.content)
    return {
        "review_id": summary["review_id"],
        "status": summary["status"],
        "rounds_used": summary["rounds_used"],
        "required_human_action": summary["required_human_action"],
        "validation_status": summary["validation_status"],
        "private_content_included": summary["private_content_included"],
        **({} if not args.content else {"payload": summary.get("payload")}),
        **_FALSE_FLAGS,
    }


# --------------------------------------------------------------------------- #
# dispatch
# --------------------------------------------------------------------------- #
REVIEW_COMMANDS: dict[str, Callable[[argparse.Namespace, Any], Any]] = {
    "show": review_show,
    "status": review_status,
}


def dispatch_review(args: argparse.Namespace, ctx: Any) -> object | None:
    """Route one parsed ``review`` invocation; ``None`` if unknown."""
    handler = REVIEW_COMMANDS.get(getattr(args, "review_command", None))
    return None if handler is None else handler(args, ctx)
