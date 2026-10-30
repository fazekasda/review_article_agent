"""Root ``pipeline`` command group: read-only stage status (kickoff 16 §6).

This module adds only the ``pipeline`` command group over the already-integrated
:mod:`review_article_agent.pipeline.status` resolver. It is a **read-only**,
local surface:

* ``pipeline status [--project ID]`` -- report the active (or named) project's
  current pipeline stage, the stages already complete, what the project is
  blocked on (a human gate, a missing input, or a failure), and the exact next
  action / CLI command.

It never writes a row, file, or revision, never dispatches a model, session, or
worker job, never records an approval, and never reaches the network. A stage is
complete only when the stage's own existing ``require_*``/read check passes, so a
reported "complete" is exactly the same authority the stage itself grants.

Privacy/safety rules mirror :mod:`review_article_agent.cli.drafting`: the project
is opened through the registry with ``expected_project_id`` and closed on every
path; a failure is a typed :class:`PipelineControlError` with a stable category
(``validation``/``conflict``/``storage``) and a static, value-free message.
"""

from __future__ import annotations

import argparse
from typing import Any

from ..domain.errors import StoreError
from ..domain.records import RecordValidationError
from ..pipeline.status import resolve
from ..storage import ArtifactConflictError
from .parsing import _resolve_project_id, _verified_store

__all__ = [
    "PipelineControlError",
    "build_pipeline_parser",
    "dispatch_pipeline",
    "pipeline_status",
]

_CATEGORY_VALIDATION = "validation"
_CATEGORY_CONFLICT = "conflict"
_CATEGORY_STORAGE = "storage"

_STATUS_MESSAGE = (
    "the pipeline status could not be read (a malformed project, a corrupt or "
    "missing stored revision, or a stage input that no longer validates); nothing "
    "was written."
)
_CONFLICT_MESSAGE = (
    "the request conflicts with existing immutable state; nothing was overwritten."
)
_STORAGE_MESSAGE = "a local storage operation failed; nothing was changed."


class PipelineControlError(Exception):
    """A ``pipeline`` command failed in a typed way.

    ``category`` is ``"validation"``, ``"conflict"``, or ``"storage"``; the
    message is always a static constant authored in this module.
    """

    def __init__(self, category: str, message: str) -> None:
        super().__init__(message)
        self.category = category


# --------------------------------------------------------------------------- #
# argument tree
# --------------------------------------------------------------------------- #
def build_pipeline_parser(sub: "argparse._SubParsersAction") -> None:
    """Register the root ``pipeline`` command group on the top-level parser."""
    pipeline = sub.add_parser(
        "pipeline",
        help=(
            "read-only pipeline stage status: the current stage and the exact "
            "next action for the active project (local only; never writes)"
        ),
    )
    actions = pipeline.add_subparsers(
        dest="pipeline_command", required=True, metavar="PIPELINE_COMMAND"
    )

    p_status = actions.add_parser(
        "status",
        help=(
            "report the current stage, the completed stages, what the project is "
            "blocked on, and the exact next action / CLI command"
        ),
    )
    p_status.add_argument("--project", metavar="ID")


# --------------------------------------------------------------------------- #
# handlers
# --------------------------------------------------------------------------- #
def pipeline_status(args: argparse.Namespace, ctx: Any) -> dict[str, Any]:
    """``pipeline status`` -- read-only stage resolution for one project."""
    project_id = _resolve_project_id(ctx, args.project)
    try:
        with _verified_store(ctx, project_id) as store:
            result = resolve(store)
    except PipelineControlError:
        raise
    except ArtifactConflictError:
        raise PipelineControlError(_CATEGORY_CONFLICT, _CONFLICT_MESSAGE) from None
    except StoreError:
        raise PipelineControlError(_CATEGORY_STORAGE, _STORAGE_MESSAGE) from None
    except OSError:
        raise PipelineControlError(_CATEGORY_STORAGE, _STORAGE_MESSAGE) from None
    except (RecordValidationError, ValueError, TypeError, KeyError, AttributeError):
        raise PipelineControlError(_CATEGORY_VALIDATION, _STATUS_MESSAGE) from None
    result["command"] = "pipeline status"
    return result


PIPELINE_COMMANDS = {
    "status": pipeline_status,
}


def dispatch_pipeline(args: argparse.Namespace, ctx: Any) -> object | None:
    """Route one parsed ``pipeline`` invocation; ``None`` if unknown."""
    handler = PIPELINE_COMMANDS.get(getattr(args, "pipeline_command", None))
    return None if handler is None else handler(args, ctx)
