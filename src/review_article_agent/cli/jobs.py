"""Job-control CLI commands over the integrated synthetic worker engine.

This module adds the ``job`` command group to the CLI: ``job
run|status|list|pause|resume|cancel|switch|drain|reconcile|attach``. It owns
only the *CLI surface* over the already-integrated worker engine
(:mod:`review_article_agent.worker.engine`) reached through the owner-authenticated
control transport (:mod:`review_article_agent.runtime.ipc`) and ensured through
the detached-process launcher (:mod:`review_article_agent.worker.process`).

Design rules (``docs/implementation/03-durable-worker.md`` deliverable 1 and the
acceptance criteria "Start a task, close the CLI, and confirm
completion/checkpoint from a new CLI" / "Authentication rejects non-owner
control"; ``docs/contracts/02-gates-and-runtime.md`` §8.4 "Pause, cancel, and
detached CLI"; ``docs/cli.md`` output/exit/secret conventions):

* **Synthetic only.** The only submittable task kinds are the engine's
  ``synthetic_numeric`` and ``synthetic_waiting_human``. There is **no** model,
  provider, credential, budget-setting, document, path, or scientific task, and
  no such option is read from the config file. The default reserve is a small,
  clearly **virtual** synthetic value: it is not real money and no provider is
  contacted.
* **``run`` returns admission, never completion.** ``job run`` explicitly ensures
  a worker is running and then submits one job over the control socket. It
  returns the durable job acknowledgement: ``job_completion=false``. Completion
  is observed later from a *new* CLI via ``job status``/``job attach``.
* **Authenticated control.** Every command first probes the worker with
  :func:`review_article_agent.worker.process.worker_status` (which verifies our
  application marker and the kernel-reported PID), then sends its control request
  over :meth:`review_article_agent.runtime.ipc.WorkerIPCClient.request` with
  ``expected_peer_pid`` bound to that verified PID, so a replaced or foreign
  listener at the control path cannot receive a payload. Only ``job run`` ever
  starts a worker; queries and controls never start one implicitly.
* **Queries and controls never mutate on exit.** ``job attach`` reads fresh job
  status snapshots and never cancels a job on CLI exit (Ctrl-C, a broken pipe, or
  a normal exit). It is bounded by a poll interval and an optional timeout, and
  never busy-loops.
* **Typed, curated errors.** A failure is raised as :class:`JobControlError`
  carrying a stable ``category`` (``validation`` / ``conflict`` / ``storage``)
  and a static, actionable message authored here. No raw engine text, no
  structured error payload, and no caller-supplied value is ever echoed — a
  caller may have pasted a credential by mistake.
* **Numeric arguments are parsed as strings.** ``argparse``'s
  ``type=int``/``type=float`` is deliberately avoided because its "invalid value"
  message would echo the raw (possibly secret) token.
* **Scientific approvals are not exposed.** No ``approve``/``reject`` or
  arbitrary ``result`` JSON option exists here; ``job reconcile`` accepts only the
  engine's fixed owner dispositions and never an arbitrary result payload.
"""

from __future__ import annotations

import argparse
import math
import re
import time
from typing import Any, Iterator

from ..runtime import ipc
from ..runtime.jobs import JobValidationError, TERMINAL_JOB_STATES, validate_reserve_usd
from ..worker import process
from ..worker.engine import PROTOCOL_VERSION, SYNTHETIC_NUMERIC_TASK, SYNTHETIC_WAITING_HUMAN_TASK
from ..worker.synthetic import MAX_DELAY_MS, MAX_STEPS, MIN_DELAY_MS, MIN_STEPS
from .worker import WorkerControlError, _resolve_timeout

__all__ = [
    "JOB_COMMANDS",
    "JobControlError",
    "StreamNDJSON",
    "build_jobs_parser",
    "job_attach",
    "job_cancel",
    "job_drain",
    "job_list",
    "job_pause",
    "job_reconcile",
    "job_resume",
    "job_run",
    "job_status",
    "job_switch",
]

# --------------------------------------------------------------------------- #
# constants
# --------------------------------------------------------------------------- #
_CATEGORY_VALIDATION = "validation"
_CATEGORY_CONFLICT = "conflict"
_CATEGORY_STORAGE = "storage"

#: The two synthetic task kinds the CLI will submit (mirrored from the engine).
_TASK_KINDS = (SYNTHETIC_NUMERIC_TASK, SYNTHETIC_WAITING_HUMAN_TASK)

#: The engine's fixed synthetic outcome modes for a numeric task.
_OUTCOMES = ("complete", "failure", "ambiguous")

#: The engine's owner dispositions for ``reconcile`` (no arbitrary result JSON).
_RESOLUTIONS = (
    "accepted_local_result",
    "definitive_failure",
    "human_escalation",
    "keep_unresolved",
)

#: Dispositions that assert a terminal local outcome and therefore require the
#: explicit ``--confirm-quiesced`` acknowledgement.
_TERMINAL_RESOLUTIONS = frozenset({"accepted_local_result", "definitive_failure"})

#: Bounded value range for a fixed synthetic accepted result.
_RESULT_VALUE_MIN = 0
_RESULT_VALUE_MAX = 999_999

#: Default and maximum attach poll interval (seconds).
_DEFAULT_POLL_INTERVAL = 0.5
_MAX_POLL_INTERVAL = 60.0

#: Bounded default overall watch budget when ``--timeout`` is omitted.
_DEFAULT_WATCH_TIMEOUT = 60.0

#: Default virtual synthetic reserve (an exact decimal; never real money).
_DEFAULT_RESERVE = "0.01"

#: Default synthetic stage identifier.
_DEFAULT_STAGE = "cli"

# --------------------------------------------------------------------------- #
# curated, value-free messages (never built from a caller value or an exception)
# --------------------------------------------------------------------------- #
_TIMEOUT_MESSAGE = (
    "invalid value for --timeout; it must be a finite number of seconds greater "
    "than 0 and at most 3600. The supplied value is not shown. Run --help for usage."
)
_POLL_MESSAGE = (
    "invalid value for --poll-interval; it must be a finite number of seconds "
    "greater than 0 and at most 60. The supplied value is not shown. Run --help "
    "for usage."
)
_STEPS_MESSAGE = (
    "invalid value for --steps; it must be an integer from 1 to 1000. The supplied "
    "value is not shown. Run --help for usage."
)
_DELAY_MESSAGE = (
    "invalid value for --delay-ms; it must be an integer from 0 to 60000. The "
    "supplied value is not shown. Run --help for usage."
)
_RESERVE_MESSAGE = (
    "invalid value for --reserve; it must be a small, exact, strictly positive "
    "decimal (for example 0.01). The supplied value is not shown. Run --help for "
    "usage."
)
_RESULT_VALUE_MESSAGE = (
    "invalid value for --result-value; it must be an integer from 0 to 999999. The "
    "supplied value is not shown. Run --help for usage."
)
_CONFIRM_MESSAGE = (
    "this disposition asserts a terminal local outcome; pass --confirm-quiesced to "
    "acknowledge that the remote work is known to be quiesced. Nothing was changed."
)
_CONFLICT_MESSAGE = (
    "the request conflicts with current execution state (the project is not the "
    "execution-active project, the job is in a state that does not permit the "
    "operation, or unresolved remote work blocks it); nothing was changed."
)
_NO_SUCH_JOB_MESSAGE = (
    "no job with that id exists in the project; nothing was changed."
)
_BAD_INPUT_MESSAGE = (
    "the worker rejected the request as invalid (an identifier, a bounded number, "
    "or a task parameter did not match the expected form); nothing was changed."
)
_ENGINE_MESSAGE = (
    "the worker could not complete the request; nothing was changed."
)
_ATTACH_MODE_MESSAGE = (
    "choose at most one of --once and --watch; the supplied values are not shown. "
    "Run --help for usage."
)
_WORKER_UNAVAILABLE_MESSAGE = (
    "the worker control endpoint is unavailable; no request was sent. Check the "
    "application data directory, or start the worker first, then retry."
)
_WORKER_NOT_RUNNING_MESSAGE = (
    "no worker is running for the data directory; job control requires a running "
    "worker. Run `worker start` or `job run` first, then retry."
)
_FOREIGN_MESSAGE = (
    "another process occupies the worker control endpoint and did not identify as "
    "this application's worker; no control was sent and nothing was changed."
)
_TRANSPORT_MESSAGE = (
    "the worker control request failed at the transport level; no control was "
    "applied and no job state was changed. Check the application data directory, "
    "then retry."
)
_START_MESSAGE = (
    "the worker did not become ready within the timeout; no job was submitted. "
    "Check the application data directory and its permissions, then retry, or "
    "increase --timeout."
)

#: Engine error code -> ``(category, static message)``. The engine's own message
#: is never surfaced (it is curated there too, but this layer keeps its own).
_CODE_ERRORS: dict[str, tuple[str, str]] = {
    "unsupported_version": (_CATEGORY_VALIDATION, _BAD_INPUT_MESSAGE),
    "unknown_op": (_CATEGORY_VALIDATION, _BAD_INPUT_MESSAGE),
    "unknown_key": (_CATEGORY_VALIDATION, _BAD_INPUT_MESSAGE),
    "missing_key": (_CATEGORY_VALIDATION, _BAD_INPUT_MESSAGE),
    "bad_type": (_CATEGORY_VALIDATION, _BAD_INPUT_MESSAGE),
    "bad_value": (_CATEGORY_VALIDATION, _BAD_INPUT_MESSAGE),
    "no_such_job": (_CATEGORY_VALIDATION, _NO_SUCH_JOB_MESSAGE),
    "not_activated": (_CATEGORY_CONFLICT, _CONFLICT_MESSAGE),
    "invalid_state": (_CATEGORY_CONFLICT, _CONFLICT_MESSAGE),
    "denied": (_CATEGORY_CONFLICT, _CONFLICT_MESSAGE),
    "wrong_project": (_CATEGORY_CONFLICT, _CONFLICT_MESSAGE),
    "switch_blocked": (_CATEGORY_CONFLICT, _CONFLICT_MESSAGE),
    "draining": (_CATEGORY_CONFLICT, _CONFLICT_MESSAGE),
    "budget_exceeded": (_CATEGORY_CONFLICT, _CONFLICT_MESSAGE),
    "internal_error": (_CATEGORY_STORAGE, _ENGINE_MESSAGE),
}

#: Simple, non-negative decimal-integer shape (no underscores/signs/exponents).
_POSITIVE_INT_RE = re.compile(r"^[0-9]+$")


# --------------------------------------------------------------------------- #
# errors + streaming
# --------------------------------------------------------------------------- #
class JobControlError(WorkerControlError):
    """A job-control command failed in a typed, curated way.

    ``category`` is one of ``"validation"``, ``"conflict"``, or ``"storage"`` and
    the message is always authored in this module (or copied from a static
    curated constant), so it never reflects a raw exception, an engine payload,
    an environment value, or a caller-supplied value. It subclasses
    :class:`review_article_agent.cli.worker.WorkerControlError` so the existing
    exit taxonomy in :mod:`review_article_agent.cli.main` applies unchanged.
    """


class StreamNDJSON:
    """Ordered documents to emit as newline-delimited JSON (the ``--watch`` form).

    This is the *one* documented exception to the single-JSON-document rule,
    limited to ``job attach --watch``. Each item is a self-contained JSON object
    on its own line.
    """

    def __init__(self, documents: Iterator[object]) -> None:
        self.documents = documents


# --------------------------------------------------------------------------- #
# argument validation
# --------------------------------------------------------------------------- #
def _parse_bounded_int(raw: object, *, lo: int, hi: int, message: str) -> int:
    """Parse a bounded non-negative integer from a raw string (never echoed)."""
    if not isinstance(raw, str):
        raise JobControlError(_CATEGORY_VALIDATION, message)
    text = raw.strip()
    if not text or _POSITIVE_INT_RE.fullmatch(text) is None:
        raise JobControlError(_CATEGORY_VALIDATION, message)
    value = int(text)
    if not (lo <= value <= hi):
        raise JobControlError(_CATEGORY_VALIDATION, message)
    return value


def _parse_seconds(raw: object, *, hi: float, message: str) -> float:
    """Parse a bounded positive number of seconds from a raw string."""
    if not isinstance(raw, str):
        raise JobControlError(_CATEGORY_VALIDATION, message)
    text = raw.strip()
    if not text:
        raise JobControlError(_CATEGORY_VALIDATION, message)
    try:
        number = float(text)
    except ValueError:
        raise JobControlError(_CATEGORY_VALIDATION, message) from None
    if not math.isfinite(number) or number <= 0 or number > hi:
        raise JobControlError(_CATEGORY_VALIDATION, message)
    return number


def _parse_reserve(raw: object) -> str:
    """Validate and canonicalise the synthetic reserve (never echoes the value)."""
    if raw is None:
        raw = _DEFAULT_RESERVE
    if not isinstance(raw, str):
        raise JobControlError(_CATEGORY_VALIDATION, _RESERVE_MESSAGE)
    try:
        return validate_reserve_usd(raw)
    except JobValidationError:
        raise JobControlError(_CATEGORY_VALIDATION, _RESERVE_MESSAGE) from None


def _resolve_poll_interval(args: argparse.Namespace) -> float:
    raw = getattr(args, "poll_interval", None)
    if raw is None:
        return _DEFAULT_POLL_INTERVAL
    return _parse_seconds(raw, hi=_MAX_POLL_INTERVAL, message=_POLL_MESSAGE)


# --------------------------------------------------------------------------- #
# argument tree
# --------------------------------------------------------------------------- #
def build_jobs_parser(sub: "argparse._SubParsersAction") -> None:
    """Register the ``job`` command group on the top-level ``sub`` parser."""
    job = sub.add_parser(
        "job",
        help=(
            "synthetic durable job control (run/status/list/pause/resume/cancel/"
            "switch/drain/reconcile/attach) over owner-only local control"
        ),
    )
    job_sub = job.add_subparsers(
        dest="job_command", required=True, metavar="JOB_COMMAND"
    )

    p_run = job_sub.add_parser(
        "run",
        help=(
            "ensure a worker is running and submit one synthetic job; returns "
            "admission, never completion"
        ),
    )
    p_run.add_argument(
        "--request-id",
        required=True,
        metavar="ID",
        help="stable caller idempotency key for this submission.",
    )
    p_run.add_argument(
        "--task-kind",
        choices=list(_TASK_KINDS),
        default=SYNTHETIC_NUMERIC_TASK,
        help="synthetic task kind (default: synthetic_numeric).",
    )
    p_run.add_argument("--steps", metavar="N", help="bounded steps (1..1000).")
    p_run.add_argument("--delay-ms", metavar="MS", help="bounded synthetic delay (0..60000).")
    p_run.add_argument(
        "--outcome",
        choices=list(_OUTCOMES),
        default="complete",
        help="synthetic outcome mode (default: complete).",
    )
    p_run.add_argument("--stage", metavar="STAGE", help="synthetic stage identifier.")
    p_run.add_argument(
        "--reserve",
        metavar="USD",
        help="small, exact, virtual synthetic reserve (default: 0.01; not real money).",
    )
    p_run.add_argument("--project", metavar="ID", help="target project (default: active).")
    p_run.add_argument("--timeout", metavar="SECONDS", help="bounded worker-start wait (positive, at most 3600).")

    p_status = job_sub.add_parser("status", help="show one job's durable status")
    p_status.add_argument("job_id", metavar="JOB_ID")
    p_status.add_argument("--project", metavar="ID")
    p_status.add_argument("--timeout", metavar="SECONDS")

    p_list = job_sub.add_parser("list", help="list a project's jobs")
    p_list.add_argument("--project", metavar="ID")
    p_list.add_argument("--timeout", metavar="SECONDS")

    for name, help_text in (
        ("pause", "pause scheduling for a job, or for the whole project"),
        ("resume", "resume scheduling for a job, or for the whole project"),
        ("cancel", "cancel a job, or all non-terminal jobs in the project"),
    ):
        p_ctrl = job_sub.add_parser(name, help=help_text)
        p_ctrl.add_argument("job_id", metavar="JOB_ID", nargs="?")
        p_ctrl.add_argument("--project", metavar="ID")
        p_ctrl.add_argument("--timeout", metavar="SECONDS")

    p_switch = job_sub.add_parser(
        "switch",
        help="safely make another project the execution-active project (drains the old one)",
    )
    p_switch.add_argument("--project", metavar="ID", required=True)
    p_switch.add_argument("--timeout", metavar="SECONDS")

    p_drain = job_sub.add_parser(
        "drain",
        help="persist a durable pause and begin draining the execution-active project",
    )
    p_drain.add_argument("--project", metavar="ID")
    p_drain.add_argument("--timeout", metavar="SECONDS")

    p_rec = job_sub.add_parser(
        "reconcile",
        help="record an explicit owner disposition for an unresolved job",
    )
    p_rec.add_argument("job_id", metavar="JOB_ID")
    p_rec.add_argument("--resolution", choices=list(_RESOLUTIONS), required=True)
    p_rec.add_argument(
        "--result-value",
        metavar="N",
        help="fixed synthetic accepted-result value (0..999999) for accepted_local_result.",
    )
    p_rec.add_argument(
        "--confirm-quiesced",
        action="store_true",
        help="acknowledge the remote work is quiesced (required for a terminal disposition).",
    )
    p_rec.add_argument("--project", metavar="ID")
    p_rec.add_argument("--timeout", metavar="SECONDS")

    p_attach = job_sub.add_parser(
        "attach",
        help="read-only job status snapshots; --watch streams newline-delimited JSON",
    )
    p_attach.add_argument("job_id", metavar="JOB_ID")
    p_attach.add_argument("--once", action="store_true", help="read one snapshot and exit (default).")
    p_attach.add_argument("--watch", action="store_true", help="stream snapshots until terminal/timeout.")
    p_attach.add_argument("--poll-interval", metavar="SECONDS", help="bounded poll interval (default: 0.5).")
    p_attach.add_argument("--timeout", metavar="SECONDS", help="bounded overall watch budget (default: 60).")
    p_attach.add_argument("--project", metavar="ID")


# --------------------------------------------------------------------------- #
# shared helpers
# --------------------------------------------------------------------------- #
def _resolve_project_id(ctx: Any, explicit: str | None) -> str:
    """Return the explicit project ID or the active project's ID."""
    if explicit is not None:
        return explicit
    return ctx.registry.active_project().project_id


def _verified_worker_pid(ctx: Any, timeout: float | None) -> int:
    """Return the verified running worker PID, or raise a curated error.

    The probe verifies our application marker and the kernel-reported peer PID
    (``worker.process.worker_status``). A non-running or unverified worker never
    yields a PID; a query or control command therefore never starts one.
    """
    try:
        status = process.worker_status(ctx.data_dir, timeout=timeout)
    except process.WorkerProcessError as exc:
        raise JobControlError(_CATEGORY_STORAGE, _WORKER_UNAVAILABLE_MESSAGE) from exc
    if not status.get("running") or not status.get("verified"):
        reason = status.get("reason")
        if reason == "foreign_endpoint":
            raise JobControlError(_CATEGORY_CONFLICT, _FOREIGN_MESSAGE)
        raise JobControlError(_CATEGORY_STORAGE, _WORKER_NOT_RUNNING_MESSAGE)
    pid = status.get("pid")
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        raise JobControlError(_CATEGORY_STORAGE, _WORKER_NOT_RUNNING_MESSAGE)
    return pid


def _start_worker(ctx: Any, timeout: float | None) -> int:
    """Ensure a worker is running and return its verified PID.

    Only ``job run`` calls this. A launch/settle failure is a curated
    ``storage`` error; no job is submitted.
    """
    try:
        if timeout is None:
            result = process.start_worker(ctx.data_dir)
        else:
            result = process.start_worker(ctx.data_dir, timeout=timeout)
    except process.WorkerProcessError as exc:
        raise JobControlError(_CATEGORY_STORAGE, _START_MESSAGE) from exc
    pid = result.get("pid") if isinstance(result, dict) else None
    if not result.get("running") or isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        raise JobControlError(_CATEGORY_STORAGE, _START_MESSAGE)
    return pid


def _rpc(ctx: Any, pid: int, request: dict[str, Any], timeout: float | None) -> Any:
    """Send one control request bound to the verified peer PID.

    Transport failures are mapped to curated ``conflict`` (a foreign/replaced
    endpoint) or ``storage`` (unavailable) errors; no engine or peer text is ever
    surfaced.
    """
    client = ipc.WorkerIPCClient(ctx.data_dir)
    try:
        return client.request(request, timeout=timeout, expected_peer_pid=pid)
    except (ipc.PeerNotAuthorized, ipc.PeerIdentityMismatch) as exc:
        raise JobControlError(_CATEGORY_CONFLICT, _FOREIGN_MESSAGE) from exc
    except ipc.WorkerNotRunning as exc:
        raise JobControlError(_CATEGORY_STORAGE, _WORKER_UNAVAILABLE_MESSAGE) from exc
    except ipc.IPCRequestError as exc:
        raise JobControlError(_CATEGORY_STORAGE, _TRANSPORT_MESSAGE) from exc
    except ipc.IPCError as exc:
        raise JobControlError(_CATEGORY_STORAGE, _TRANSPORT_MESSAGE) from exc
    except OSError as exc:
        raise JobControlError(_CATEGORY_STORAGE, _WORKER_UNAVAILABLE_MESSAGE) from exc


def _expect_ok(result: Any) -> dict[str, Any]:
    """Return a successful engine result, or raise a curated mapped error."""
    if not isinstance(result, dict) or result.get("ok") is not True:
        code = result.get("code") if isinstance(result, dict) else None
        category, message = _CODE_ERRORS.get(
            code if isinstance(code, str) else "", (_CATEGORY_STORAGE, _ENGINE_MESSAGE)
        )
        raise JobControlError(category, message)
    return result


#: Metadata fields projected from an engine job document. No arbitrary or
#: source-derived field is copied through.
_JOB_FIELDS = (
    "project_id",
    "job_id",
    "request_id",
    "task_kind",
    "state",
    "outcome_unresolved",
    "current_attempt_id",
    "current_fence",
    "attempts_total",
    "retries_used",
    "accepted_fence",
    "wait_reason",
)


def _job_projection(job: Any) -> dict[str, Any]:
    """Project one engine job document to safe, readonly metadata.

    The accepted result is narrowed to the engine's fixed synthetic shape
    (``{"kind": "synthetic_numeric", "value": <int>}``); any other shape is
    dropped rather than echoed.
    """
    source = job if isinstance(job, dict) else {}
    data: dict[str, Any] = {name: source.get(name) for name in _JOB_FIELDS}
    accepted = source.get("accepted_result")
    if isinstance(accepted, dict):
        projected: dict[str, Any] = {
            "result_kind": accepted.get("result_kind"),
            "fence": accepted.get("fence"),
        }
        result = accepted.get("result")
        if (
            isinstance(result, dict)
            and set(result) == {"kind", "value"}
            and result.get("kind") == "synthetic_numeric"
            and isinstance(result.get("value"), int)
            and not isinstance(result.get("value"), bool)
        ):
            projected["result"] = {"kind": "synthetic_numeric", "value": result["value"]}
        data["accepted_result"] = projected
    return data


def _control_request(
    ctx: Any, pid: int, op: str, project_id: str, job_id: str | None, timeout: float | None
) -> dict[str, Any]:
    request: dict[str, Any] = {
        "version": PROTOCOL_VERSION,
        "op": op,
        "project_id": project_id,
    }
    if job_id is not None:
        request["job_id"] = job_id
    return _expect_ok(_rpc(ctx, pid, request, timeout))


# --------------------------------------------------------------------------- #
# handlers
# --------------------------------------------------------------------------- #
def _numeric_parameters(args: argparse.Namespace) -> dict[str, Any]:
    """Validate the bounded synthetic numeric parameters (never echoing a value)."""
    steps = _parse_bounded_int(
        args.steps if args.steps is not None else str(MIN_STEPS),
        lo=MIN_STEPS,
        hi=MAX_STEPS,
        message=_STEPS_MESSAGE,
    )
    delay_ms = _parse_bounded_int(
        args.delay_ms if args.delay_ms is not None else str(MIN_DELAY_MS),
        lo=MIN_DELAY_MS,
        hi=MAX_DELAY_MS,
        message=_DELAY_MESSAGE,
    )
    stage = args.stage if args.stage is not None else _DEFAULT_STAGE
    if not isinstance(stage, str) or not stage:
        raise JobControlError(_CATEGORY_VALIDATION, _BAD_INPUT_MESSAGE)
    return {
        "steps": steps,
        "delay_ms": delay_ms,
        "outcome": args.outcome,
        "stage": stage,
        "reserve_usd": _parse_reserve(args.reserve),
    }


def job_run(args: argparse.Namespace, ctx: Any) -> dict[str, object]:
    """``job run`` — ensure a worker and submit one synthetic job (admission only)."""
    timeout = _resolve_timeout(args)
    project_id = _resolve_project_id(ctx, args.project)

    task_kind = getattr(args, "task_kind", SYNTHETIC_NUMERIC_TASK)
    if task_kind not in _TASK_KINDS:
        raise JobControlError(_CATEGORY_VALIDATION, _BAD_INPUT_MESSAGE)

    request: dict[str, Any] = {
        "version": PROTOCOL_VERSION,
        "op": "submit",
        "project_id": project_id,
        "request_id": args.request_id,
        "task_kind": task_kind,
    }
    if task_kind == SYNTHETIC_NUMERIC_TASK:
        request.update(_numeric_parameters(args))

    pid = _start_worker(ctx, timeout)
    result = _expect_ok(_rpc(ctx, pid, request, timeout))
    job = _job_projection(result.get("job"))
    return {
        "job": job,
        "submitted": True,
        # A submit returns durable admission/acknowledgement, never completion.
        "job_completion": False,
        # The reserve is a small virtual synthetic value; no real cost/provider.
        "virtual_reserve": True,
        "scientific_validation": False,
    }


def job_status(args: argparse.Namespace, ctx: Any) -> dict[str, object]:
    """``job status`` — one job's durable status, read-only."""
    timeout = _resolve_timeout(args)
    project_id = _resolve_project_id(ctx, args.project)
    pid = _verified_worker_pid(ctx, timeout)
    result = _control_request(ctx, pid, "job_status", project_id, args.job_id, timeout)
    return {"job": _job_projection(result.get("job"))}


def job_list(args: argparse.Namespace, ctx: Any) -> dict[str, object]:
    """``job list`` — a project's jobs, read-only."""
    timeout = _resolve_timeout(args)
    project_id = _resolve_project_id(ctx, args.project)
    pid = _verified_worker_pid(ctx, timeout)
    result = _control_request(ctx, pid, "job_list", project_id, None, timeout)
    return {"jobs": [_job_projection(job) for job in result.get("jobs", [])]}


def _control_command(args: argparse.Namespace, ctx: Any, op: str) -> dict[str, object]:
    timeout = _resolve_timeout(args)
    project_id = _resolve_project_id(ctx, args.project)
    job_id = getattr(args, "job_id", None)
    pid = _verified_worker_pid(ctx, timeout)
    result = _control_request(ctx, pid, op, project_id, job_id, timeout)
    if "job" in result:
        return {"job": _job_projection(result.get("job"))}
    return {"jobs": [_job_projection(job) for job in result.get("jobs", [])]}


def job_pause(args: argparse.Namespace, ctx: Any) -> dict[str, object]:
    """``job pause`` — stop scheduling new work; durable state is preserved."""
    return _control_command(args, ctx, "pause")


def job_resume(args: argparse.Namespace, ctx: Any) -> dict[str, object]:
    """``job resume`` — re-acquire execution and resume scheduling.

    Resuming never clears an unknown/unresolved outcome, never raises a budget
    cap, and never bypasses a waiting gate; the visible job state is shown
    unchanged where the engine refuses.
    """
    return _control_command(args, ctx, "resume")


def job_cancel(args: argparse.Namespace, ctx: Any) -> dict[str, Any]:
    """``job cancel`` — stop scheduling; terminal for scheduling only.

    Cancellation never clears an ambiguous remote outcome and never retracts an
    external side effect that already executed.
    """
    return _control_command(args, ctx, "cancel")


def job_switch(args: argparse.Namespace, ctx: Any) -> dict[str, object]:
    """``job switch`` — safely make another project execution-active."""
    timeout = _resolve_timeout(args)
    project_id = args.project
    pid = _verified_worker_pid(ctx, timeout)
    result = _control_request(ctx, pid, "switch", project_id, None, timeout)
    return {
        "switch": {
            "switched": bool(result.get("switched")),
            "active_project_id": result.get("active_project_id"),
        }
    }


def job_drain(args: argparse.Namespace, ctx: Any) -> dict[str, object]:
    """``job drain`` — persist a durable pause and begin draining the active project."""
    timeout = _resolve_timeout(args)
    project_id = _resolve_project_id(ctx, args.project)
    pid = _verified_worker_pid(ctx, timeout)
    result = _control_request(ctx, pid, "drain", project_id, None, timeout)
    return {
        "drain": {
            "state": result.get("state"),
            "project_id": result.get("project_id"),
        }
    }


def job_reconcile(args: argparse.Namespace, ctx: Any) -> dict[str, object]:
    """``job reconcile`` — record an explicit owner disposition.

    No arbitrary result JSON is accepted. A terminal disposition
    (``accepted_local_result`` or ``definitive_failure``) requires the explicit
    ``--confirm-quiesced`` acknowledgement. ``human_escalation`` and
    ``keep_unresolved`` are the safe options.
    """
    timeout = _resolve_timeout(args)
    resolution = args.resolution
    if resolution not in _RESOLUTIONS:
        raise JobControlError(_CATEGORY_VALIDATION, _BAD_INPUT_MESSAGE)

    request: dict[str, Any] = {"resolution": resolution}
    if resolution == "accepted_local_result":
        value = _parse_bounded_int(
            args.result_value,
            lo=_RESULT_VALUE_MIN,
            hi=_RESULT_VALUE_MAX,
            message=_RESULT_VALUE_MESSAGE,
        )
        request["result_kind"] = "synthetic_numeric"
        request["result"] = {"kind": "synthetic_numeric", "value": value}
    else:
        if args.result_value is not None:
            raise JobControlError(_CATEGORY_VALIDATION, _RESULT_VALUE_MESSAGE)

    if resolution in _TERMINAL_RESOLUTIONS and not args.confirm_quiesced:
        raise JobControlError(_CATEGORY_VALIDATION, _CONFIRM_MESSAGE)

    project_id = _resolve_project_id(ctx, args.project)
    pid = _verified_worker_pid(ctx, timeout)
    engine_request = {
        "version": PROTOCOL_VERSION,
        "op": "reconcile",
        "project_id": project_id,
        "job_id": args.job_id,
        **request,
    }
    result = _expect_ok(_rpc(ctx, pid, engine_request, timeout))
    return {
        "job": _job_projection(result.get("job")),
        "owner_control": True,
        "scientific_validation": False,
    }


def job_attach(args: argparse.Namespace, ctx: Any) -> dict[str, object] | StreamNDJSON:
    """``job attach`` — read-only status snapshots; ``--watch`` streams NDJSON.

    The default reads one snapshot and exits. ``--watch`` polls fresh
    ``job_status`` queries with a bounded interval and an optional overall
    timeout, emitting one newline-delimited JSON document per poll and stopping
    at a terminal scheduling state (or the timeout). It never cancels a job on
    CLI exit (a normal exit, Ctrl-C, or a broken pipe).
    """
    if args.watch and args.once:
        raise JobControlError(_CATEGORY_VALIDATION, _ATTACH_MODE_MESSAGE)

    timeout = _resolve_timeout(args)
    poll = _resolve_poll_interval(args)
    project_id = _resolve_project_id(ctx, args.project)
    pid = _verified_worker_pid(ctx, timeout)

    if not args.watch:
        result = _control_request(ctx, pid, "job_status", project_id, args.job_id, timeout)
        return {"job": _job_projection(result.get("job")), "streaming": False}

    budget = timeout if timeout is not None else _DEFAULT_WATCH_TIMEOUT
    deadline = time.monotonic() + budget

    def _documents() -> Iterator[object]:
        while True:
            result = _control_request(ctx, pid, "job_status", project_id, args.job_id, timeout)
            job = _job_projection(result.get("job"))
            yield {"job": job}
            if job.get("state") in TERMINAL_JOB_STATES:
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(poll, remaining))

    return StreamNDJSON(_documents())


# --------------------------------------------------------------------------- #
# dispatch table
# --------------------------------------------------------------------------- #
JOB_COMMANDS = {
    "run": job_run,
    "status": job_status,
    "list": job_list,
    "pause": job_pause,
    "resume": job_resume,
    "cancel": job_cancel,
    "switch": job_switch,
    "drain": job_drain,
    "reconcile": job_reconcile,
    "attach": job_attach,
}
