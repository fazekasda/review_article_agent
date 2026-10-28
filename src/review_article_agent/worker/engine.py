"""Standalone durable synthetic worker engine (Step 03).

This module connects the already-integrated pieces — durable job/attempt
persistence (:mod:`review_article_agent.runtime.jobs`), the owner-authenticated
control transport (:mod:`review_article_agent.runtime.ipc`), durable execution
activation (:class:`review_article_agent.storage.registry.Registry`), and the
deterministic synthetic external-admission backend
(:mod:`review_article_agent.worker.synthetic`) — into one explicit, standalone
worker engine.

What it is
----------

A **pure-Python, stdlib-only** engine that:

* owns a single owner-only Unix-domain control socket per ``data_dir`` (the IPC
  layer enforces singleton + owner authentication);
* answers a **strict, versioned, whitelisted** request protocol (see
  :data:`PROTOCOL_VERSION` and :data:`OPERATIONS`);
* executes **at most one synthetic dispatch at a time** (and never more than the
  approved ``max 2`` concurrent external calls) for the single execution-active
  project;
* keeps the single execution activation token-fenced, guarding every claim and
  every accepted write, and never nesting a registry call inside a registry lock;
* never trusts a caller-supplied ``uid``/``actor`` field — the IPC layer supplies
  the kernel-verified peer out of band;
* performs restart reconciliation and a safe, quiesce-before-switch drain.

What it deliberately is **not**
-------------------------------

* **No real model, OpenCode session, network call, source processing, or
  scientific approval.** Every task is a bounded *synthetic numeric* task (or a
  synthetic waiting-human gate). The engine records no scientific result and
  authorizes no real external work.
* It makes **no exactly-once external execution or billing claim** (C02 §8.2).
  Fencing protects accepted local writes; it does not fence external side
  effects that already happened.

Request protocol
----------------

Every request is one newline-delimited strict-JSON object::

    {"version": 1, "op": "<operation>", ...operation keys...}

The version must equal :data:`PROTOCOL_VERSION`; the ``op`` must be one of
:data:`OPERATIONS`; and only the keys declared for that operation are permitted
(any other key — including a caller-supplied ``uid``/``actor`` — is rejected).
Every response is a JSON object. A successful response is
``{"ok": true, ...}``; an error is ``{"ok": false, "code": <static code>,
"message": <static text>}``. A raw exception is **never** surfaced.

Fault-injection seams
---------------------

:meth:`WorkerEngine.add_fault_hook` registers an in-process callable fired at
``before_dispatch`` / ``after_admission`` / ``before_commit``. Separately, the
**test-only** environment variable ``RAA_WORKER_TEST_PAUSE_AT`` (one of those
seam names) makes an owned disposable worker ``os.kill(os.getpid(), SIGSTOP)``
at the seam so a crash matrix can stop it mid-dispatch. It is **never** active
unless that variable is explicitly set, and it never signals another process.
"""

from __future__ import annotations

import os
import signal
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Callable

from ..domain.errors import ReviewAgentError
from ..domain.records import validate_identifier
from ..runtime.jobs import (
    ACTIVE_ATTEMPT_STATES,
    BudgetExceededError,
    Job,
    JobError,
    JobRepository,
    JobStateError,
    StaleFenceError,
)
from ..storage import paths
from ..storage.registry import Registry
from ..storage.store import ProjectStore
from .synthetic import (
    MAX_DELAY_MS,
    MAX_STEPS,
    MIN_DELAY_MS,
    MIN_STEPS,
    SyntheticBackend,
    SyntheticBackendError,
)

__all__ = [
    "APP_NAME",
    "FAULT_SEAMS",
    "OPERATIONS",
    "PROTOCOL_VERSION",
    "SYNTHETIC_NUMERIC_TASK",
    "SYNTHETIC_WAITING_HUMAN_TASK",
    "ErrorCode",
    "WorkerEngine",
]

# --------------------------------------------------------------------------- #
# Protocol constants
# --------------------------------------------------------------------------- #
#: Application name reported by ``status`` (and matched by the launcher).
APP_NAME = "review_article_agent.worker"

#: Control protocol version. The only version this engine accepts.
PROTOCOL_VERSION = 1

#: The only synthetic task kinds an owner may submit. Nothing else — no model,
#: document, path, or scientific task — is accepted.
SYNTHETIC_NUMERIC_TASK = "synthetic_numeric"
SYNTHETIC_WAITING_HUMAN_TASK = "synthetic_waiting_human"

#: Every accepted control operation.
OPERATIONS = (
    "status",
    "submit",
    "job_status",
    "job_list",
    "pause",
    "resume",
    "cancel",
    "reconcile",
    "switch",
    "drain",
    "stop",
)

#: Fault-injection seam names (in-process tests and the test-only SIGSTOP gate).
FAULT_SEAMS = ("before_dispatch", "after_admission", "before_commit")

#: Test-only environment variable naming the seam at which an owned worker stops.
TEST_PAUSE_ENV = "RAA_WORKER_TEST_PAUSE_AT"


class ErrorCode:
    """Static, curated error codes (never derived from request content)."""

    UNSUPPORTED_VERSION = "unsupported_version"
    UNKNOWN_OP = "unknown_op"
    UNKNOWN_KEY = "unknown_key"
    MISSING_KEY = "missing_key"
    BAD_TYPE = "bad_type"
    BAD_VALUE = "bad_value"
    NOT_ACTIVATED = "not_activated"
    WRONG_PROJECT = "wrong_project"
    NO_SUCH_JOB = "no_such_job"
    INVALID_STATE = "invalid_state"
    DENIED = "denied"
    DRAINING = "draining"
    BUDGET_EXCEEDED = "budget_exceeded"
    SWITCH_BLOCKED = "switch_blocked"
    INTERNAL = "internal_error"


#: Static message per code (never reflects the request).
_MESSAGES = {
    ErrorCode.UNSUPPORTED_VERSION: "the control protocol version is not supported",
    ErrorCode.UNKNOWN_OP: "the requested operation is not supported",
    ErrorCode.UNKNOWN_KEY: "the request contained an unknown field",
    ErrorCode.MISSING_KEY: "the request was missing a required field",
    ErrorCode.BAD_TYPE: "a request field had the wrong type",
    ErrorCode.BAD_VALUE: "a request field had an out-of-range or invalid value",
    ErrorCode.NOT_ACTIVATED: "no project holds execution activation",
    ErrorCode.WRONG_PROJECT: "the project is not the execution-active project",
    ErrorCode.NO_SUCH_JOB: "no such job exists in this project",
    ErrorCode.INVALID_STATE: "the job is not in a state that permits this operation",
    ErrorCode.DENIED: "the operation is not permitted right now",
    ErrorCode.DRAINING: "execution is draining; new dispatch is disabled",
    ErrorCode.BUDGET_EXCEEDED: "the cost cap would be exceeded",
    ErrorCode.SWITCH_BLOCKED: "the switch is blocked pending reconciliation",
    ErrorCode.INTERNAL: "the request could not be handled",
}

#: Per-operation permitted keys (strict allowlist). ``version`` and ``op`` are
#: required on every request and are validated separately.
_ALLOWED_KEYS: dict[str, frozenset[str]] = {
    "status": frozenset({"version", "op"}),
    "submit": frozenset(
        {
            "version",
            "op",
            "project_id",
            "request_id",
            "task_kind",
            "steps",
            "delay_ms",
            "outcome",
            "stage",
            "reserve_usd",
        }
    ),
    "job_status": frozenset({"version", "op", "project_id", "job_id"}),
    "job_list": frozenset({"version", "op", "project_id"}),
    "pause": frozenset({"version", "op", "project_id", "job_id"}),
    "resume": frozenset({"version", "op", "project_id", "job_id"}),
    "cancel": frozenset({"version", "op", "project_id", "job_id"}),
    "reconcile": frozenset(
        {"version", "op", "project_id", "job_id", "resolution", "result_kind", "result"}
    ),
    "switch": frozenset({"version", "op", "project_id"}),
    "drain": frozenset({"version", "op", "project_id"}),
    "stop": frozenset({"version", "op"}),
}


class _RequestError(Exception):
    """Internal: a curated request-validation failure."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


# --------------------------------------------------------------------------- #
# Engine
# --------------------------------------------------------------------------- #
@dataclass
class _ActiveExecution:
    project_id: str
    token: str


class WorkerEngine:
    """A standalone synthetic worker engine over one ``data_dir``.

    The engine is single-threaded by design: :meth:`handle` and :meth:`tick`
    are both called from the one control-loop thread, so no job state is shared
    across threads. Tests drive it directly (in-process) or through the UDS
    control transport.
    """

    def __init__(
        self,
        data_dir: str | os.PathLike[str],
        *,
        registry: Registry | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._data_dir = os.path.abspath(os.fspath(data_dir))
        self._registry = registry if registry is not None else Registry(self._data_dir)
        self._backend = SyntheticBackend(self._data_dir, clock=clock)
        self._clock: Callable[[], float] = clock if clock is not None else time.time
        self._activation: _ActiveExecution | None = None
        self._draining = False
        self._stopping = False
        self._paused_projects: set[str] = set()
        self._repos: dict[str, JobRepository] = {}
        self._stores: dict[str, ProjectStore] = {}
        self._wait_reasons: dict[str, str] = {}
        self._hooks: dict[str, list[Callable[..., None]]] = {
            name: [] for name in FAULT_SEAMS
        }
        self._adopt_activation()

    def _adopt_activation(self) -> None:
        """Adopt an activation already held in the registry at startup.

        A detached worker started while a project is execution-active must pick
        up that same activation (no new grant, no new token).
        """
        try:
            activation = self._registry.activation()
        except ReviewAgentError:
            return
        if (
            activation.state in ("active", "draining")
            and activation.active_project_id
            and activation.activation_token
        ):
            self._activation = _ActiveExecution(
                activation.active_project_id, activation.activation_token
            )
            self._draining = activation.state == "draining"

    # -- lifecycle ----------------------------------------------------------- #
    @property
    def data_dir(self) -> str:
        return self._data_dir

    @property
    def stopping(self) -> bool:
        return self._stopping

    def close(self) -> None:
        for store in self._stores.values():
            try:
                store.close()
            except Exception:  # pragma: no cover - defensive
                pass
        self._stores.clear()
        self._repos.clear()

    # -- fault hooks --------------------------------------------------------- #
    def add_fault_hook(self, seam: str, hook: Callable[..., None]) -> None:
        """Register an in-process fault hook at ``seam`` (see :data:`FAULT_SEAMS`)."""
        if seam not in self._hooks:
            raise ValueError(f"unknown fault seam {seam!r}")
        self._hooks[seam].append(hook)

    def _fire(self, seam: str, **info: Any) -> None:
        for hook in self._hooks[seam]:
            hook(**info)
        # Test-only stop gate: never active unless explicitly requested.
        if os.environ.get(TEST_PAUSE_ENV) == seam:
            os.kill(os.getpid(), signal.SIGSTOP)

    # -- request handling ---------------------------------------------------- #
    def handle(self, request: dict[str, Any], peer: Any = None) -> dict[str, Any]:
        """Validate and dispatch one control request; never raises.

        ``peer`` is the kernel-verified peer credentials supplied out of band by
        the IPC layer. The request is untrusted data; the engine never reads an
        authority/actor field from it.
        """
        try:
            # Defensive re-check of the out-of-band owner identity. The IPC layer
            # already enforces this; a mismatch is still never trusted.
            if peer is not None and getattr(peer, "uid", os.getuid()) != os.getuid():
                return self._error(ErrorCode.DENIED)
            op = self._validate_request(request)
            handler = getattr(self, f"_op_{op}")
            return handler(request)
        except _RequestError as exc:
            return self._error(exc.code)
        except Exception:
            return self._error(ErrorCode.INTERNAL)

    def _validate_request(self, request: dict[str, Any]) -> str:
        if not isinstance(request, dict):
            raise _RequestError(ErrorCode.BAD_TYPE)
        version = request.get("version")
        if isinstance(version, bool) or not isinstance(version, int):
            raise _RequestError(ErrorCode.BAD_TYPE)
        if version != PROTOCOL_VERSION:
            raise _RequestError(ErrorCode.UNSUPPORTED_VERSION)
        op = request.get("op")
        if not isinstance(op, str) or op not in _ALLOWED_KEYS:
            raise _RequestError(ErrorCode.UNKNOWN_OP)
        allowed = _ALLOWED_KEYS[op]
        if any(key not in allowed for key in request):
            raise _RequestError(ErrorCode.UNKNOWN_KEY)
        return op

    @staticmethod
    def _error(code: str) -> dict[str, Any]:
        return {"ok": False, "code": code, "message": _MESSAGES.get(code, "")}

    # -- helpers ------------------------------------------------------------- #
    def _require_active(self) -> _ActiveExecution:
        if self._activation is None:
            raise _RequestError(ErrorCode.NOT_ACTIVATED)
        return self._activation

    def _resolve_project(self, request: dict[str, Any]) -> str:
        project_id = request.get("project_id")
        if project_id is None:
            return self._require_active().project_id
        if not isinstance(project_id, str):
            raise _RequestError(ErrorCode.BAD_TYPE)
        return project_id

    def _repo_for(self, project_id: str) -> JobRepository:
        repo = self._repos.get(project_id)
        if repo is not None:
            return repo
        info = self._registry.get(project_id)  # validates the id + registration
        app_dir = paths.app_dir_for(info.root_path)
        store = ProjectStore.open_at(app_dir, expected_project_id=project_id)
        repo = JobRepository(store)
        self._stores[project_id] = store
        self._repos[project_id] = repo
        return repo

    @staticmethod
    def _checkpoint_continuity(attempt: Any) -> str | None:
        checkpoint = attempt.checkpoint
        if isinstance(checkpoint, dict):
            value = checkpoint.get("continuity_id")
            if isinstance(value, str) and value:
                return value
        return None

    def _job_dict(self, repo: JobRepository, job: Job) -> dict[str, Any]:
        accepted = repo.accepted_result(job.job_id)
        data: dict[str, Any] = {
            "project_id": job.project_id,
            "job_id": job.job_id,
            "request_id": job.request_id,
            "task_kind": job.task_kind,
            "state": job.state,
            "outcome_unresolved": job.outcome_unresolved,
            "current_attempt_id": job.current_attempt_id,
            "current_fence": job.current_fence,
            "attempts_total": job.attempts_total,
            "retries_used": job.retries_used,
            "accepted_fence": job.accepted_fence,
            "wait_reason": self._wait_reasons.get(job.job_id),
        }
        if accepted is not None:
            data["accepted_result"] = {
                "result_kind": accepted.result_kind,
                "result": accepted.result,
                "fence": accepted.fence,
            }
        return data

    # -- operations ---------------------------------------------------------- #
    def _op_status(self, request: dict[str, Any]) -> dict[str, Any]:
        activation = self._registry.activation()
        if self._stopping:
            state = "stopping"
        elif activation.state == "none":
            state = "idle"
        elif self._draining or activation.state == "draining":
            state = "draining"
        else:
            state = "active"
        return {
            "ok": True,
            "app": APP_NAME,
            "protocol_version": PROTOCOL_VERSION,
            "pid": os.getpid(),
            "state": state,
            "activation_state": activation.state,
            "active_project_id": activation.active_project_id,
            "paused_projects": sorted(self._paused_projects),
        }

    def _op_submit(self, request: dict[str, Any]) -> dict[str, Any]:
        active = self._require_active()
        project_id = self._resolve_project(request)
        if project_id != active.project_id:
            return self._error(ErrorCode.WRONG_PROJECT)
        if project_id in self._paused_projects:
            return self._error(ErrorCode.DENIED)
        if self._draining:
            return self._error(ErrorCode.DRAINING)

        request_id = request.get("request_id")
        if not isinstance(request_id, str) or not request_id:
            raise _RequestError(ErrorCode.BAD_VALUE)
        task_kind = request.get("task_kind", SYNTHETIC_NUMERIC_TASK)
        if task_kind not in (SYNTHETIC_NUMERIC_TASK, SYNTHETIC_WAITING_HUMAN_TASK):
            raise _RequestError(ErrorCode.BAD_VALUE)

        repo = self._repo_for(project_id)

        if task_kind == SYNTHETIC_WAITING_HUMAN_TASK:
            job = repo.submit(request_id, task_kind=task_kind, parameters={})
            repo.escalate_to_human(job.job_id)
            self._wait_reasons[job.job_id] = "waiting_human_task"
            return {"ok": True, "job": self._job_dict(repo, repo.get(job.job_id))}

        # synthetic numeric dispatch: bounded numeric params only.
        steps = self._bounded_int(request, "steps", MIN_STEPS, MAX_STEPS)
        delay_ms = self._bounded_int(request, "delay_ms", MIN_DELAY_MS, MAX_DELAY_MS)
        outcome = request.get("outcome")
        if outcome not in ("complete", "failure", "ambiguous"):
            raise _RequestError(ErrorCode.BAD_VALUE)
        stage = request.get("stage")
        if not isinstance(stage, str) or not stage:
            raise _RequestError(ErrorCode.BAD_VALUE)
        try:
            validate_identifier(stage, "stage")
        except ReviewAgentError as exc:
            raise _RequestError(ErrorCode.BAD_VALUE) from exc
        reserve_usd = request.get("reserve_usd")
        self._validate_reserve(reserve_usd)

        parameters = {
            "steps": steps,
            "delay_ms": delay_ms,
            "outcome": outcome,
            "stage": stage,
            "reserve_usd": reserve_usd,
        }
        try:
            job = repo.submit(request_id, task_kind=task_kind, parameters=parameters)
        except JobError:
            return self._error(ErrorCode.INVALID_STATE)
        return {"ok": True, "job": self._job_dict(repo, job)}

    def _op_job_status(self, request: dict[str, Any]) -> dict[str, Any]:
        project_id = self._resolve_project(request)
        job_id = request.get("job_id")
        if not isinstance(job_id, str) or not job_id:
            raise _RequestError(ErrorCode.BAD_VALUE)
        try:
            repo = self._repo_for(project_id)
        except ReviewAgentError:
            raise _RequestError(ErrorCode.BAD_VALUE)
        job = repo.get(job_id)
        if job is None:
            return self._error(ErrorCode.NO_SUCH_JOB)
        return {"ok": True, "job": self._job_dict(repo, job)}

    def _op_job_list(self, request: dict[str, Any]) -> dict[str, Any]:
        project_id = self._resolve_project(request)
        try:
            repo = self._repo_for(project_id)
        except ReviewAgentError:
            raise _RequestError(ErrorCode.BAD_VALUE)
        jobs = [self._job_dict(repo, job) for job in repo.list()]
        return {"ok": True, "project_id": project_id, "jobs": jobs}

    def _control_job(self, request: dict[str, Any], op: str) -> dict[str, Any]:
        project_id = self._resolve_project(request)
        job_id = request.get("job_id")
        repo = self._repo_for(project_id)
        if job_id is None:
            # Project-scoped: apply to every non-terminal job of the project.
            changed = []
            for job in repo.list():
                if job.state in ("completed", "failed", "cancelled"):
                    continue
                changed.append(self._apply_control(repo, job.job_id, op))
            if op == "pause":
                self._paused_projects.add(project_id)
            elif op == "resume":
                self._paused_projects.discard(project_id)
            return {"ok": True, "project_id": project_id, "jobs": changed}
        if not isinstance(job_id, str) or not job_id:
            raise _RequestError(ErrorCode.BAD_VALUE)
        job = repo.get(job_id)
        if job is None:
            return self._error(ErrorCode.NO_SUCH_JOB)
        return {"ok": True, "job": self._apply_control(repo, job_id, op)}

    def _apply_control(self, repo: JobRepository, job_id: str, op: str) -> dict[str, Any]:
        try:
            if op == "pause":
                job = repo.pause(job_id)
            elif op == "resume":
                job = repo.resume(job_id)
            else:
                job = repo.cancel(job_id)
        except JobError:
            job = repo.get(job_id)
            if job is None:
                raise _RequestError(ErrorCode.NO_SUCH_JOB)
        return self._job_dict(repo, job)

    def _op_pause(self, request: dict[str, Any]) -> dict[str, Any]:
        return self._control_job(request, "pause")

    def _op_resume(self, request: dict[str, Any]) -> dict[str, Any]:
        return self._control_job(request, "resume")

    def _op_cancel(self, request: dict[str, Any]) -> dict[str, Any]:
        return self._control_job(request, "cancel")

    def _op_reconcile(self, request: dict[str, Any]) -> dict[str, Any]:
        project_id = self._resolve_project(request)
        job_id = request.get("job_id")
        resolution = request.get("resolution")
        if not isinstance(job_id, str) or not job_id:
            raise _RequestError(ErrorCode.BAD_VALUE)
        if resolution not in (
            "accepted_local_result",
            "definitive_failure",
            "human_escalation",
            "keep_unresolved",
        ):
            raise _RequestError(ErrorCode.BAD_VALUE)
        repo = self._repo_for(project_id)
        if repo.get(job_id) is None:
            return self._error(ErrorCode.NO_SUCH_JOB)
        try:
            job = repo.reconcile(
                job_id,
                resolution=resolution,
                result_kind=request.get("result_kind"),
                result=request.get("result"),
            )
        except JobError:
            return self._error(ErrorCode.INVALID_STATE)
        return {"ok": True, "job": self._job_dict(repo, job)}

    def _op_drain(self, request: dict[str, Any]) -> dict[str, Any]:
        active = self._require_active()
        project_id = self._resolve_project(request)
        if project_id != active.project_id:
            return self._error(ErrorCode.WRONG_PROJECT)
        self._draining = True
        self._registry.begin_drain(active.token)
        return {"ok": True, "state": "draining", "project_id": project_id}

    def _op_switch(self, request: dict[str, Any]) -> dict[str, Any]:
        project_id = request.get("project_id")
        if not isinstance(project_id, str) or not project_id:
            raise _RequestError(ErrorCode.BAD_VALUE)
        try:
            info = self._registry.get(project_id)  # validate registration
        except ReviewAgentError:
            raise _RequestError(ErrorCode.BAD_VALUE)

        current = self._registry.activation()
        if current.state == "none":
            activated = self._registry.activate_execution(project_id)
            self._activation = _ActiveExecution(project_id, activated.activation_token)
            return {"ok": True, "switched": True, "active_project_id": project_id}

        if current.active_project_id == project_id:
            self._activation = _ActiveExecution(project_id, current.activation_token)
            return {"ok": True, "switched": True, "active_project_id": project_id}

        # Safe switch: drain the old project, reconcile/quiesce its in-flight
        # work, and only then release + activate the new one.
        old_pid = current.active_project_id
        token = current.activation_token
        self._registry.begin_drain(token)
        self._draining = True
        self._paused_projects.add(old_pid)
        if not self._quiesce_project(old_pid):
            # Unknown / still-executing remote work pins arbitration: the switch
            # is blocked pending reconciliation (C02 §8.5).
            return {
                "ok": False,
                "code": ErrorCode.SWITCH_BLOCKED,
                "message": _MESSAGES[ErrorCode.SWITCH_BLOCKED],
                "active_project_id": old_pid,
            }
        self._registry.release_execution(token)
        activated = self._registry.activate_execution(project_id)
        self._activation = _ActiveExecution(project_id, activated.activation_token)
        self._draining = False
        return {"ok": True, "switched": True, "active_project_id": project_id}

    def _op_stop(self, request: dict[str, Any]) -> dict[str, Any]:
        self._stopping = True
        return {"ok": True, "stopping": True}

    # -- reconciliation / quiescence ----------------------------------------- #
    def _quiesce_project(self, project_id: str) -> bool:
        """Return ``True`` when ``project_id`` has no potential remote work.

        Every **active** attempt state (not only ``outcome_unresolved``) is
        treated as potential remote work, plus an unresolved bit or a
        ``waiting_external`` gate. ``waiting_human``/``paused`` with no active
        attempt hold no remote work and may release execution resources.
        """
        try:
            repo = self._repo_for(project_id)
        except ReviewAgentError:
            # A store we cannot read is treated as not quiescent (fail closed).
            return False
        for job in repo.list():
            if job.state in ("completed", "failed", "cancelled"):
                continue
            if job.outcome_unresolved:
                return False
            if job.state == "waiting_external":
                return False
            for attempt in repo.attempts(job.job_id):
                if attempt.state in ACTIVE_ATTEMPT_STATES:
                    return False
        return True

    def reconcile_startup(self) -> None:
        """Reconcile recorded attempts across **all** registered project stores.

        Includes restored jobs that have no registry activation and no provider
        journal record: an active attempt is always moved to an explicit
        recovered state (never a silent success). A store that cannot be read is
        skipped; it is reconciled again on a later run.
        """
        try:
            projects = self._registry.list_projects()
        except ReviewAgentError:
            return
        for info in projects:
            try:
                repo = self._repo_for(info.project_id)
            except (ReviewAgentError, OSError):
                continue
            for job in repo.list():
                attempt_id = job.current_attempt_id
                if attempt_id is None:
                    continue
                attempt = repo.attempt(attempt_id)
                if attempt is None:
                    continue
                if attempt.state in ACTIVE_ATTEMPT_STATES:
                    try:
                        repo.recover(attempt_id)
                    except JobError:
                        continue

    # -- scheduling ---------------------------------------------------------- #
    def tick(self) -> float | None:
        """Advance one unit of scheduling/reconciliation; return a wait hint.

        Returns ``0.0`` when it made progress (call again promptly), a positive
        number of seconds to wait before the next poll, or ``None`` when there is
        no pending work at all (the caller should block indefinitely). It never
        busy-loops: a waiting-human / paused / reconciliation-required job yields
        ``None``.
        """
        if self._stopping:
            return 0.0
        active = self._activation
        if active is None or self._draining:
            return None
        project_id = active.project_id
        if project_id in self._paused_projects:
            return None
        try:
            repo = self._repo_for(project_id)
        except (ReviewAgentError, OSError):
            return None
        plan = self._plan(repo)
        if plan is None:
            return None
        action = plan[0]
        if action == "claim":
            return self._do_claim(repo, project_id, plan[1])
        if action == "admit":
            return self._do_admit(repo, project_id, plan[1], plan[2])
        if action == "lookup":
            return self._do_lookup(repo, project_id, plan[1], plan[2])
        return self._do_reconcile(repo, project_id, plan[1], plan[2])

    def _plan(self, repo: JobRepository) -> tuple | None:
        for job in repo.list():
            if job.state in (
                "completed",
                "failed",
                "cancelled",
                "paused",
                "waiting_human",
                "waiting_external",
            ):
                continue
            attempt = (
                None
                if job.current_attempt_id is None
                else repo.attempt(job.current_attempt_id)
            )
            if job.state in ("reconciliation_required", "interrupted"):
                if attempt is not None:
                    return ("reconcile", job, attempt)
                continue
            if job.state == "queued":
                if attempt is None:
                    return ("claim", job)
                if attempt.state == "queued":
                    return ("admit", job, attempt)
                if attempt.state in ACTIVE_ATTEMPT_STATES:
                    return ("lookup", job, attempt)
            if job.state in ("admitted", "running") and attempt is not None:
                if attempt.state in ACTIVE_ATTEMPT_STATES:
                    return ("lookup", job, attempt)
        return None

    def _do_claim(
        self, repo: JobRepository, project_id: str, job: Job
    ) -> float | None:
        active = self._activation
        params = job.parameters if isinstance(job.parameters, dict) else {}
        stage = params.get("stage")
        reserve = params.get("reserve_usd")
        if not isinstance(stage, str) or not isinstance(reserve, str):
            repo.escalate_to_human(job.job_id)
            self._wait_reasons[job.job_id] = "invalid_parameters"
            return None
        try:
            # Resolve the activation info first, then take the registry guard,
            # then commit — never nest a registry call inside the guard.
            with self._registry.execution_write_guard(
                active.token, project_id, allow_draining=False
            ):
                repo.begin_attempt(
                    job.job_id,
                    dispatch_kind="external_synthetic",
                    stage=stage,
                    reserve_usd=reserve,
                )
        except BudgetExceededError:
            repo.escalate_to_human(job.job_id)
            self._wait_reasons[job.job_id] = ErrorCode.BUDGET_EXCEEDED
            return None
        except StaleFenceError:
            return 0.0
        except JobError:
            return None
        except ReviewAgentError:
            # Registry guard refused (draining / wrong project): stop dispatch.
            return None
        return 0.0

    def _do_admit(
        self, repo: JobRepository, project_id: str, job: Job, attempt: Any
    ) -> float | None:
        params = job.parameters if isinstance(job.parameters, dict) else {}
        steps = params.get("steps")
        delay_ms = params.get("delay_ms")
        outcome = params.get("outcome")
        if not (
            isinstance(steps, int)
            and isinstance(delay_ms, int)
            and outcome in ("complete", "failure", "ambiguous")
        ):
            repo.escalate_to_human(job.job_id)
            self._wait_reasons[job.job_id] = "invalid_parameters"
            return None
        try:
            # 1) initialize the journal and persist continuity BEFORE any admit.
            continuity = self._backend.initialize()
            repo.record_checkpoint(
                attempt.attempt_id,
                {"continuity_id": continuity, "phase": "predispatch"},
            )
            self._fire(
                "before_dispatch",
                project_id=project_id,
                job_id=job.job_id,
                attempt_id=attempt.attempt_id,
            )
            # 2) admit under the same stable attempt identity (the dispatch id).
            backend = SyntheticBackend(
                self._data_dir, expected_continuity_id=continuity
            )
            backend.admit(
                attempt.attempt_id,
                project_id,
                steps=steps,
                delay_ms=delay_ms,
                outcome=outcome,
            )
            self._fire(
                "after_admission",
                project_id=project_id,
                job_id=job.job_id,
                attempt_id=attempt.attempt_id,
            )
            repo.mark_admitted(
                attempt.attempt_id, admission_id="adm_" + attempt.attempt_id
            )
        except SyntheticBackendError:
            repo.escalate_to_human(job.job_id)
            self._wait_reasons[job.job_id] = "admission_indeterminate"
            return None
        except JobError:
            return None
        return 0.0

    def _do_lookup(
        self, repo: JobRepository, project_id: str, job: Job, attempt: Any
    ) -> float | None:
        continuity = self._checkpoint_continuity(attempt)
        if continuity is None:
            # No continuity checkpoint ⇒ by ordering the dispatch could not have
            # been issued: reopen the same attempt identity (never mint a new one).
            try:
                repo.resume_proven_undispatched(attempt.attempt_id)
            except JobError:
                return None
            return 0.0
        backend = SyntheticBackend(self._data_dir, expected_continuity_id=continuity)
        result = backend.lookup(attempt.attempt_id, project_id)
        if result.status == "completed" and result.definitive and result.result is not None:
            return self._accept_result(
                repo,
                project_id,
                job,
                attempt,
                {"kind": "synthetic_numeric", "value": result.result.value},
            )
        if result.status == "failed" and result.definitive_failure:
            try:
                repo.mark_definitive_failure(attempt.attempt_id)
                repo.settle_cost(attempt.attempt_id, cost_state="not_incurred")
            except JobError:
                return None
            return 0.0
        if result.status == "admitted" and not result.definitive:
            remaining = None
            if result.ready_epoch_s is not None:
                try:
                    remaining = float(result.ready_epoch_s) - self._clock()
                except Exception:  # pragma: no cover - defensive
                    remaining = None
            if remaining is not None and remaining > 0:
                return max(0.05, min(remaining + 0.02, 1.0))
            return 0.25
        # unknown / lost journal / identity conflict / ambiguous: escalate and
        # retain arbitration. Never a new identity, never a silent success.
        try:
            repo.mark_ambiguous(attempt.attempt_id)
            repo.escalate_to_human(job.job_id)
        except JobError:
            return None
        self._wait_reasons[job.job_id] = result.reason or "reconciliation_required"
        return None

    def _do_reconcile(
        self, repo: JobRepository, project_id: str, job: Job, attempt: Any
    ) -> float | None:
        # Recovery already recorded the attempt as interrupted/ambiguous. Decide
        # from durable state, with the ordering argument for a missing checkpoint.
        continuity = self._checkpoint_continuity(attempt)
        if continuity is None:
            try:
                repo.resume_proven_undispatched(attempt.attempt_id)
            except JobError:
                repo.escalate_to_human(job.job_id)
                self._wait_reasons[job.job_id] = "reconciliation_required"
                return None
            return 0.0
        # A recorded continuity id means a dispatch may have been issued: look it
        # up authoritatively; absence of a local receipt is never proof.
        return self._do_lookup(repo, project_id, job, attempt)

    def _accept_result(
        self,
        repo: JobRepository,
        project_id: str,
        job: Job,
        attempt: Any,
        result: Any,
    ) -> float | None:
        active = self._activation
        self._fire(
            "before_commit",
            project_id=project_id,
            job_id=job.job_id,
            attempt_id=attempt.attempt_id,
        )
        try:
            # allow_draining=True is reconciliation-only: finishing a write the
            # engine had already accepted before a drain.
            with self._registry.execution_write_guard(
                active.token, project_id, allow_draining=True
            ):
                repo.accept_result(
                    attempt.attempt_id, result_kind="synthetic_numeric", result=result
                )
            repo.settle_cost(attempt.attempt_id, cost_state="known", amount_usd="0")
        except JobError:
            return None
        except ReviewAgentError:
            return None
        return 0.0

    # -- input validation ---------------------------------------------------- #
    @staticmethod
    def _bounded_int(request: dict[str, Any], key: str, lo: int, hi: int) -> int:
        value = request.get(key)
        if isinstance(value, bool) or not isinstance(value, int):
            raise _RequestError(ErrorCode.BAD_TYPE)
        if not (lo <= value <= hi):
            raise _RequestError(ErrorCode.BAD_VALUE)
        return value

    @staticmethod
    def _validate_reserve(value: Any) -> None:
        if not isinstance(value, str):
            raise _RequestError(ErrorCode.BAD_TYPE)
        if len(value) > 128:
            raise _RequestError(ErrorCode.BAD_VALUE)
        try:
            amount = Decimal(value)
        except (InvalidOperation, ValueError) as exc:
            raise _RequestError(ErrorCode.BAD_VALUE) from exc
        if not amount.is_finite() or amount <= 0:
            raise _RequestError(ErrorCode.BAD_VALUE)
