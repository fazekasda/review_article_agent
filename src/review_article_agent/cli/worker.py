"""Worker **lifecycle** CLI commands over the integrated ``worker.process`` API.

This module adds the ``worker start|status|stop`` command group to the CLI. It
owns only the *CLI surface* over
:mod:`review_article_agent.worker.process`: it resolves the caller's timeout,
calls the process launcher's public functions (``start_worker``,
``worker_status``, ``stop_worker``), and projects their result into a stable,
JSON-ready document or a typed, curated error.

Design rules (``docs/implementation/03-durable-worker.md`` deliverable 1 and
acceptance criterion "Start a task, close the CLI, and confirm
completion/checkpoint from a new CLI" / "Authentication rejects non-owner
control; no root or public unauthenticated listener is required";
``docs/contracts/02-gates-and-runtime.md`` §8.4 "Pause, cancel, and detached
CLI"; ``docs/cli.md`` output/exit/secret conventions):

* **This is lifecycle control only.** It starts, probes, and *stops-by-request*
  the detached worker. It launches no job, shows no job result, and never
  claims a job completed: ``worker start`` reports **readiness**, not job
  completion. Job/``run``/``attach`` control is a separate, later surface.
* **Selection is not execution.** No ordinary command (``create``/``list``/
  ``select``/``status``) starts or stops a worker; only this command group
  acts on the worker lifecycle.
* **Stops go through authenticated control only.** ``worker stop`` routes the
  request over ``worker.process.stop_worker`` (owner-authenticated local IPC).
  It never signals a PID, never reads a PID file, and never deletes an ordinary
  service. A stop that is not *confirmed* is not reported as success.
* **Typed, curated errors.** A failure is raised as :class:`WorkerControlError`
  carrying a stable ``category`` (``validation`` / ``conflict`` / ``storage``)
  and a static, actionable message authored here. No raw exception text, no
  environment value, and no caller-supplied value is ever echoed — a caller may
  have pasted a credential into ``--timeout`` by mistake.
* **Numeric arguments are parsed as strings.** ``--timeout`` is taken as a raw
  string and validated here; ``argparse``'s ``type=float``/``type=int`` is
  deliberately avoided because its "invalid value" message would echo the raw
  (possibly secret) token back to the user.
* **No model/credential settings.** No provider, model, key, token, or budget
  option exists here, and none is read from the config file.
"""

from __future__ import annotations

import argparse
import math

from ..domain.errors import ReviewAgentError
from ..worker import process

__all__ = [
    "WorkerControlError",
    "build_worker_parser",
    "worker_start",
    "worker_status",
    "worker_stop",
    "WORKER_COMMANDS",
]

#: Upper bound on ``--timeout`` seconds, mirrored from the process launcher and
#: the IPC transport so the CLI never forwards an unbounded wait.
_MAX_TIMEOUT_SECONDS = 3600.0

#: The two/three stable error categories this command group can raise. They map
#: onto the CLI's existing exit taxonomy in :mod:`review_article_agent.cli.main`.
_CATEGORY_VALIDATION = "validation"
_CATEGORY_CONFLICT = "conflict"
_CATEGORY_STORAGE = "storage"

# --------------------------------------------------------------------------- #
# curated, value-free messages (never built from a caller value or an exception)
# --------------------------------------------------------------------------- #
_TIMEOUT_MESSAGE = (
    "invalid value for --timeout; it must be a finite number of seconds greater "
    "than 0 and at most 3600. The supplied value is not shown. Run --help for "
    "usage."
)
_START_MESSAGE = (
    "the worker did not become ready within the timeout; no worker is confirmed "
    "running. Check the application data directory and its permissions, then "
    "retry, or increase --timeout."
)
_STATUS_MESSAGE = (
    "the worker control endpoint could not be probed; the worker's state is "
    "unknown. Check the application data directory and its permissions, then "
    "retry."
)
_STOP_FOREIGN_MESSAGE = (
    "another process occupies the worker control endpoint and did not identify "
    "as this application's worker; no stop request was sent and nothing was "
    "signalled. Investigate the runtime endpoint in the application data "
    "directory."
)
_STOP_UNAVAILABLE_MESSAGE = (
    "the worker control endpoint is unavailable, so the stop request could not "
    "be delivered or confirmed; nothing was signalled. Check the application "
    "data directory, then retry (the worker may already be gone)."
)
_STOP_PENDING_MESSAGE = (
    "the stop request was delivered but the worker did not confirm shutdown "
    "within the timeout; the worker may still be running. Retry, or increase "
    "--timeout."
)


class WorkerControlError(ReviewAgentError):
    """A worker lifecycle command failed in a typed, curated way.

    ``category`` is one of ``"validation"``, ``"conflict"``, or ``"storage"``;
    the message is always authored in this module (or copied from a static
    curated constant), so it never reflects a raw exception, an environment
    value, or a caller-supplied value.
    """

    def __init__(self, category: str, message: str) -> None:
        super().__init__(message)
        self.category = category


# --------------------------------------------------------------------------- #
# argument validation
# --------------------------------------------------------------------------- #
def _resolve_timeout(args: argparse.Namespace) -> float | None:
    """Return the validated ``--timeout`` value, or ``None`` to use defaults.

    The raw token is parsed here (never by ``argparse``'s ``type=float``), so an
    invalid or secret-looking value is rejected with a static message that does
    not echo the value.
    """
    raw = getattr(args, "timeout", None)
    if raw is None:
        return None
    if not isinstance(raw, str):
        # argparse only ever hands us a string here; defend anyway.
        raise WorkerControlError(_CATEGORY_VALIDATION, _TIMEOUT_MESSAGE)
    text = raw.strip()
    if not text:
        raise WorkerControlError(_CATEGORY_VALIDATION, _TIMEOUT_MESSAGE)
    try:
        number = float(text)
    except ValueError:
        raise WorkerControlError(_CATEGORY_VALIDATION, _TIMEOUT_MESSAGE) from None
    if not math.isfinite(number) or number <= 0 or number > _MAX_TIMEOUT_SECONDS:
        raise WorkerControlError(_CATEGORY_VALIDATION, _TIMEOUT_MESSAGE)
    return number


def _call(func, data_dir: str, timeout: float | None):
    """Call a process launcher function with or without an explicit timeout."""
    if timeout is None:
        return func(data_dir)
    return func(data_dir, timeout=timeout)


# --------------------------------------------------------------------------- #
# argument tree
# --------------------------------------------------------------------------- #
def build_worker_parser(sub: "argparse._SubParsersAction") -> None:
    """Register the ``worker`` command group on the top-level ``sub`` parser."""
    worker = sub.add_parser(
        "worker",
        help=(
            "background-worker lifecycle (start/status/stop) over owner-only "
            "local control; lifecycle control only, no job execution"
        ),
    )
    worker_sub = worker.add_subparsers(
        dest="worker_command", required=True, metavar="WORKER_COMMAND"
    )

    p_start = worker_sub.add_parser(
        "start",
        help=(
            "ensure a detached worker is running for the data directory "
            "(reports readiness, not job completion)"
        ),
    )
    p_start.add_argument(
        "--timeout",
        metavar="SECONDS",
        help="bounded readiness wait in seconds (positive, at most 3600).",
    )

    p_status = worker_sub.add_parser(
        "status", help="show the verified worker status for the data directory"
    )
    p_status.add_argument(
        "--timeout",
        metavar="SECONDS",
        help="bounded probe timeout in seconds (positive, at most 3600).",
    )

    p_stop = worker_sub.add_parser(
        "stop",
        help="stop the worker by authenticated control request (never a signal)",
    )
    p_stop.add_argument(
        "--timeout",
        metavar="SECONDS",
        help="bounded stop wait in seconds (positive, at most 3600).",
    )


# --------------------------------------------------------------------------- #
# result projections
# --------------------------------------------------------------------------- #
def _start_payload(result: dict) -> dict:
    return {
        "command": "start",
        "readiness": "ready",
        "started": bool(result.get("started")),
        "running": bool(result.get("running")),
        "verified": bool(result.get("verified")),
        "app": result.get("app"),
        "protocol_version": result.get("protocol_version"),
        "pid": result.get("pid"),
        "state": result.get("state"),
        # ``start`` confirms the worker is up, never that any job finished.
        "job_completion": False,
        "scientific_validation": False,
    }


def _status_payload(result: dict) -> dict:
    running = bool(result.get("running"))
    return {
        "command": "status",
        "running": running,
        "verified": bool(result.get("verified")),
        "app": result.get("app"),
        "protocol_version": result.get("protocol_version"),
        "pid": result.get("pid"),
        "state": result.get("state"),
        # An explicit, curated reason for a non-running result: ``running``
        # ``False`` alone is not proof that all is well (it may be unavailable
        # or a foreign endpoint). ``None`` only for a verified running worker.
        "reason": None if running else result.get("reason"),
        "scientific_validation": False,
    }


def _stopped_payload(*, already_stopped: bool, was_running: bool, pid, state) -> dict:
    return {
        "command": "stop",
        "stopped": True,
        "already_stopped": already_stopped,
        "was_running": was_running,
        "pid": pid,
        "state": state,
        "scientific_validation": False,
    }


# --------------------------------------------------------------------------- #
# handlers
# --------------------------------------------------------------------------- #
def worker_start(args: argparse.Namespace, ctx) -> dict[str, object]:
    """``worker start`` — ensure a detached worker is running (readiness)."""
    timeout = _resolve_timeout(args)
    try:
        result = _call(process.start_worker, ctx.data_dir, timeout)
    except WorkerControlError:
        raise
    except process.WorkerStartError as exc:
        raise WorkerControlError(_CATEGORY_STORAGE, _START_MESSAGE) from exc
    except process.WorkerProcessError as exc:
        raise WorkerControlError(_CATEGORY_STORAGE, _START_MESSAGE) from exc
    return {"worker": _start_payload(result)}


def worker_status(args: argparse.Namespace, ctx) -> dict[str, object]:
    """``worker status`` — verified worker status (explicit reason when down)."""
    timeout = _resolve_timeout(args)
    try:
        result = _call(process.worker_status, ctx.data_dir, timeout)
    except WorkerControlError:
        raise
    except process.WorkerProcessError as exc:
        raise WorkerControlError(_CATEGORY_STORAGE, _STATUS_MESSAGE) from exc
    return {"worker": _status_payload(result)}


def worker_stop(args: argparse.Namespace, ctx) -> dict[str, object]:
    """``worker stop`` — stop by authenticated control request.

    A confirmed stop is success. An operator who stops an already-stopped worker
    gets an idempotent success carrying ``already_stopped`` (the worker is
    confirmed absent). A foreign endpoint, an unavailable endpoint, or a stop the
    worker never confirmed is **not** success: it raises a typed
    :class:`WorkerControlError`.
    """
    timeout = _resolve_timeout(args)
    try:
        result = _call(process.stop_worker, ctx.data_dir, timeout)
    except WorkerControlError:
        raise
    except process.WorkerProcessError as exc:
        raise WorkerControlError(_CATEGORY_STORAGE, _STOP_UNAVAILABLE_MESSAGE) from exc

    if result.get("stopped"):
        return {
            "worker": _stopped_payload(
                already_stopped=False,
                was_running=bool(result.get("was_running")),
                pid=result.get("pid"),
                state=result.get("state"),
            )
        }

    reason = result.get("reason")
    was_running = bool(result.get("was_running"))
    if reason == "not_running" and not was_running:
        # Confirmed absent: idempotent success, clearly flagged.
        return {
            "worker": _stopped_payload(
                already_stopped=True, was_running=False, pid=None, state=None
            )
        }
    if reason == "foreign_endpoint":
        raise WorkerControlError(_CATEGORY_CONFLICT, _STOP_FOREIGN_MESSAGE)
    if reason == "stop_unconfirmed":
        raise WorkerControlError(_CATEGORY_STORAGE, _STOP_PENDING_MESSAGE)
    # ``stop_unavailable`` and any other unconfirmed outcome.
    raise WorkerControlError(_CATEGORY_STORAGE, _STOP_UNAVAILABLE_MESSAGE)


WORKER_COMMANDS = {
    "start": worker_start,
    "status": worker_status,
    "stop": worker_stop,
}
