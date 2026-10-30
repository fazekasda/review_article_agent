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
import os
from typing import Any, Callable

from ..domain.errors import StoreError
from ..domain.records import RecordValidationError
from ..pipeline import run as pipeline_run
from ..pipeline.run import PipelineConfigError, PipelineRunError, load_config
from ..pipeline.status import resolve
from ..storage import ArtifactConflictError
from .jobs import (
    JobControlError,
    _control_request,
    _expect_ok,
    _job_projection,
    _parse_bounded_int,
    _rpc,
    _start_worker,
)
from .parsing import _resolve_project_id, _verified_store
from .worker import _resolve_timeout

__all__ = [
    "PipelineControlError",
    "build_pipeline_parser",
    "dispatch_pipeline",
    "pipeline_run_command",
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
_RUN_CONFIG_MESSAGE = (
    "the --config pipeline configuration file is missing, unreadable, or malformed "
    "(it must be a strict JSON object naming credential_env, sections, roles, the "
    "workspace id templates, and one rights template). Nothing was submitted. The "
    "supplied path and contents are not shown. Run --help for the format."
)
_RUN_REJECTED_MESSAGE = (
    "the pipeline run request was rejected before or during submission (a missing "
    "configured credential, a derivation that failed strict validation, or a "
    "workspace that could not be written); no further job was submitted."
)
_RUN_STEPS_MESSAGE = (
    "invalid value for --max-steps; it must be an integer from 1 to 1000. The "
    "supplied value is not shown. Run --help for usage."
)
_RUN_CREDENTIAL_MESSAGE = (
    "the configured generation credential environment variable is not present in "
    "this process's environment; no job was submitted. Export the named variable "
    "(its value is never accepted on the command line) and retry. The variable "
    "name and value are not shown."
)


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

    p_run = actions.add_parser(
        "run",
        help=(
            "submit the current stage's next machine job(s) to the worker and stop "
            "at the next human gate, budget limit, pause, or failure"
        ),
    )
    p_run.add_argument("--project", metavar="ID")
    p_run.add_argument(
        "--config",
        dest="pipeline_config",
        required=True,
        metavar="PATH",
        help=(
            "strict JSON pipeline configuration (credential_env, sections, per-role "
            "hosted-use artifact ids, target id templates, and one rights template)"
        ),
    )
    p_run.add_argument(
        "--wait",
        action="store_true",
        help="poll the submitted jobs to a terminal state before re-resolving",
    )
    p_run.add_argument(
        "--timeout",
        metavar="SECONDS",
        help="bounded overall wait budget (positive, at most 3600)",
    )
    p_run.add_argument(
        "--max-steps",
        metavar="N",
        help="bounded number of orchestration steps (1..1000, default 10)",
    )


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


def _resolve_max_steps(args: argparse.Namespace) -> int:
    raw = getattr(args, "max_steps", None)
    if raw is None:
        return pipeline_run.DEFAULT_MAX_STEPS
    return _parse_bounded_int(raw, lo=1, hi=1000, message=_RUN_STEPS_MESSAGE)


def pipeline_run_command(args: argparse.Namespace, ctx: Any) -> dict[str, Any]:
    """``pipeline run`` -- submit the current stage's next machine job(s).

    The configuration is validated and the credential **environment-variable
    name** is checked before any job is submitted; only the name is ever read
    (never a value). ``--wait`` polls the submitted jobs through the existing
    ``job status`` control call with a bounded overall budget. Idempotent: an
    already-recorded request id is never re-submitted.
    """
    try:
        config = load_config(args.pipeline_config)
    except PipelineConfigError:
        raise PipelineControlError(_CATEGORY_VALIDATION, _RUN_CONFIG_MESSAGE) from None

    timeout = _resolve_timeout(args)
    max_steps = _resolve_max_steps(args)
    project_id = _resolve_project_id(ctx, args.project)

    state: dict[str, Any] = {"pid": None, "job_ids": {}}

    def _worker_pid() -> int:
        if state["pid"] is None:
            state["pid"] = _start_worker(ctx, timeout)
        return state["pid"]

    def submit(request: dict) -> Any:
        result = _expect_ok(_rpc(ctx, _worker_pid(), request, timeout))
        job = _job_projection(result.get("job"))
        state["job_ids"][request["request_id"]] = job.get("job_id")
        return result

    def poll(request_id: str) -> str | None:
        job_id = state["job_ids"].get(request_id)
        if job_id is None:
            return None
        result = _control_request(ctx, _worker_pid(), "job_status", project_id, job_id, timeout)
        return _job_projection(result.get("job")).get("state")

    try:
        with _verified_store(ctx, project_id) as store:
            result = pipeline_run.run(
                store,
                None,
                config=config,
                submit=submit,
                wait=bool(args.wait),
                timeout=timeout,
                max_steps=max_steps,
                environ=os.environ,
                poll=poll if args.wait else None,
            )
    except PipelineRunError as exc:
        message = (
            _RUN_CREDENTIAL_MESSAGE
            if getattr(exc, "code", None) == "missing_credential"
            else _RUN_REJECTED_MESSAGE
        )
        raise PipelineControlError(exc.category, message) from None
    except JobControlError:
        raise
    except PipelineControlError:
        raise
    except ArtifactConflictError:
        raise PipelineControlError(_CATEGORY_CONFLICT, _CONFLICT_MESSAGE) from None
    except StoreError:
        raise PipelineControlError(_CATEGORY_STORAGE, _STORAGE_MESSAGE) from None
    except OSError:
        raise PipelineControlError(_CATEGORY_STORAGE, _STORAGE_MESSAGE) from None
    except (RecordValidationError, ValueError, TypeError, KeyError, AttributeError):
        raise PipelineControlError(_CATEGORY_VALIDATION, _RUN_REJECTED_MESSAGE) from None
    result["command"] = "pipeline run"
    result["project_id"] = project_id
    result["key_value_accepted"] = False
    return result


PIPELINE_COMMANDS = {
    "status": pipeline_status,
    "run": pipeline_run_command,
}


def dispatch_pipeline(args: argparse.Namespace, ctx: Any) -> object | None:
    """Route one parsed ``pipeline`` invocation; ``None`` if unknown."""
    handler = PIPELINE_COMMANDS.get(getattr(args, "pipeline_command", None))
    return None if handler is None else handler(args, ctx)
