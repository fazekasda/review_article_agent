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
* executes **at most one synthetic dispatch at a time** for the single
  execution-active project;
* keeps the single execution activation token-fenced, guarding every claim and
  every accepted write, and **never nests a registry call inside a registry
  lock**;
* never trusts a caller-supplied ``uid``/``actor`` field — the IPC layer supplies
  the kernel-verified peer out of band;
* reconciles restart state and performs a safe, quiesce-before-switch drain.

Scheduling vs reconciliation
-----------------------------

Scheduling (starting **new** external work: a claim or a dispatch admission) and
reconciliation (finishing **existing** work: a lookup, an accepted local result,
or a definitive failure) are deliberately separate:

* new dispatch requires an *execution-eligible* project: activation held, not
  draining, not owner-paused, and not budget-blocked;
* reconciliation of an already-admitted attempt continues under an
  ``allow_draining`` write guard while the project is draining or paused, and
  preserves the held (paused/cancelled) scheduling state.

An ambiguous/unknown outcome escalates to ``waiting_human`` and is **never** polled
in a busy loop. An execution-active project with no remote work and no schedulable
work **releases** its activation so another project can execute.

Store lifetime
--------------

Inactive project stores are **never cached**: a repository handle is opened,
used, and closed per request and per startup scan. The location is re-resolved
from the registry on every access, so a user relocate of an inactive project is
always picked up (and an open handle never outlives the activation it was opened
for).

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
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Iterator

from ..domain.errors import ProjectConflict, ReviewAgentError
from ..domain.records import validate_identifier
from ..runtime.jobs import (
    ACTIVE_ATTEMPT_STATES,
    BudgetExceededError,
    Job,
    JobError,
    JobRepository,
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
#: Application name reported by ``status`` and ``stop`` (and matched by the launcher).
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
    across threads. Inactive project stores are never cached: each request and
    each startup scan opens, uses, and closes its own repository handle.
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
        self._wait_reasons: dict[str, str] = {}
        self._hooks: dict[str, list[Callable[..., None]]] = {
            name: [] for name in FAULT_SEAMS
        }
        self._adopt_activation()

    def _adopt_activation(self) -> None:
        """Adopt an activation already held in the registry at startup.

        A detached worker started while a project is execution-active must pick
        up that same activation (no new grant, no new token). This is a
        read-only registry observation; it never mutates scheduling state.
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
        """Release any engine-owned resources.

        No project store is held between calls, so this is a no-op beyond
        dropping cached wait-reason text; it exists for a stable lifecycle API.
        """
        self._wait_reasons.clear()

    # -- store access -------------------------------------------------------- #
    @contextmanager
    def open_repository(self, project_id: str) -> Iterator[JobRepository]:
        """Yield a repository for ``project_id``, closing it on exit.

        The store location is re-resolved from the registry on every open, so a
        relocated inactive project is always read at its current location and no
        connection outlives the call. This is the only path by which the engine
        touches a project store.
        """
        info = self._registry.get(project_id)  # validates id + registration
        app_dir = paths.app_dir_for(info.root_path)
        store = ProjectStore.open_at(app_dir, expected_project_id=project_id)
        try:
            yield JobRepository(store)
        finally:
            store.close()

    @contextmanager
    def _write_guard(self, project_id: str, *, allow_draining: bool) -> Iterator[None]:
        """Authorize a local write under the registry's execution token.

        Resolves the activation from the registry (never a nested registry call),
        refuses a project that is not the execution-active one, and holds the
        registry lock only across the local commit.
        """
        current = self._registry.activation()
        if current.state == "none" or current.active_project_id != project_id:
            raise ProjectConflict(
                f"project {project_id!r} is not the execution-active project; "
                f"refusing to authorize a write."
            )
        with self._registry.execution_write_guard(
            current.activation_token, project_id, allow_draining=allow_draining
        ):
            yield

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
            if self._activation is None:
                raise _RequestError(ErrorCode.NOT_ACTIVATED)
            return self._activation.project_id
        if not isinstance(project_id, str) or not project_id:
            raise _RequestError(ErrorCode.BAD_VALUE)
        return project_id

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

    # -- activation acquisition / release ------------------------------------ #
    def _ensure_execution(self, project_id: str) -> _ActiveExecution:
        """Return the execution activation for ``project_id``, acquiring if free.

        Acquiring is allowed only when no activation is held and no *other*
        project still has remote work outstanding (a recovery blocker). If a
        different project holds execution, this is refused — the engine never
        implicitly drains another project.
        """
        try:
            self._registry.get(project_id)
        except ReviewAgentError as exc:
            raise _RequestError(ErrorCode.BAD_VALUE) from exc
        current = self._registry.activation()
        if current.state != "none":
            if current.active_project_id != project_id:
                raise _RequestError(ErrorCode.WRONG_PROJECT)
            self._activation = _ActiveExecution(
                project_id, current.activation_token
            )
            self._draining = current.state == "draining"
            return self._activation
        if current.active_project_id is not None:
            # ``none`` state must carry no project; anything else is corruption.
            raise _RequestError(ErrorCode.INTERNAL)
        if self._has_remote_elsewhere(project_id):
            raise _RequestError(ErrorCode.SWITCH_BLOCKED)
        activated = self._registry.activate_execution(project_id)
        self._activation = _ActiveExecution(project_id, activated.activation_token)
        self._draining = False
        return self._activation

    def _release(self, project_id: str) -> None:
        active = self._activation
        if active is None or active.project_id != project_id:
            return
        try:
            self._registry.release_execution(active.token)
        except ReviewAgentError:
            # Confirm whether the registry already recorded a release.
            try:
                if self._registry.activation().state == "none":
                    self._activation = None
                    self._draining = False
            except ReviewAgentError:
                pass
            return
        self._activation = None
        self._draining = False

    def _maybe_release_active(self) -> None:
        """Release execution when the active project has nothing left to do.

        A waiting-human/paused project with no potential remote work (and no
        schedulable job) releases its arbitration so another project may execute.
        An unknown/unresolved remote outcome is **never** auto-released.
        """
        active = self._activation
        if active is None:
            return
        project_id = active.project_id
        if self._has_remote(project_id) or self._has_schedulable(project_id):
            return
        self._release(project_id)

    # -- project-work predicates --------------------------------------------- #
    def _has_remote(self, project_id: str) -> bool:
        """True if any job carries potential remote work (pins arbitration)."""
        try:
            with self.open_repository(project_id) as repo:
                for job in repo.list():
                    if job.outcome_unresolved:
                        return True
                    if job.state == "waiting_external":
                        return True
                    for attempt in repo.attempts(job.job_id):
                        if attempt.state in ACTIVE_ATTEMPT_STATES:
                            return True
        except (ReviewAgentError, OSError):
            # Unreadable store: fail closed (treat as still doing work).
            return True
        return False

    def _has_schedulable(self, project_id: str) -> bool:
        """True if any job still needs scheduling (so we do not release early)."""
        try:
            with self.open_repository(project_id) as repo:
                for job in repo.list():
                    if job.state in ("queued", "admitted", "running", "interrupted"):
                        return True
        except (ReviewAgentError, OSError):
            return True
        return False

    def _has_remote_elsewhere(self, project_id: str) -> bool:
        try:
            projects = self._registry.list_projects()
        except ReviewAgentError:
            return True
        for info in projects:
            if info.project_id == project_id:
                continue
            if self._has_remote(info.project_id):
                return True
        return False

    def _quiesce_project(self, project_id: str) -> bool:
        """Return ``True`` when ``project_id`` has no potential remote work.

        Every job is checked, including cancelled/failed ones: an unresolved bit,
        a ``waiting_external`` gate, or **any** attempt in an active state means
        remote work may still be executing and pins arbitration (C02 §8.5).
        """
        return not self._has_remote(project_id)

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
        project_id = self._resolve_project(request)

        request_id = request.get("request_id")
        if not isinstance(request_id, str) or not request_id:
            raise _RequestError(ErrorCode.BAD_VALUE)
        task_kind = request.get("task_kind", SYNTHETIC_NUMERIC_TASK)
        if task_kind not in (SYNTHETIC_NUMERIC_TASK, SYNTHETIC_WAITING_HUMAN_TASK):
            raise _RequestError(ErrorCode.BAD_VALUE)

        # Validate the dispatch inputs before any durable or activation change.
        parameters: dict[str, Any] = {}
        if task_kind == SYNTHETIC_NUMERIC_TASK:
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

        # Another project must not be silently drained for this submit.
        current = self._registry.activation()
        if current.state != "none" and current.active_project_id != project_id:
            return self._error(ErrorCode.WRONG_PROJECT)
        # Do not create new durable work while this project is held.
        if project_id in self._paused_projects:
            return self._error(ErrorCode.DENIED)
        if (
            self._draining
            and self._activation is not None
            and self._activation.project_id == project_id
        ):
            return self._error(ErrorCode.DRAINING)

        with self.open_repository(project_id) as repo:
            try:
                job = repo.submit(
                    request_id, task_kind=task_kind, parameters=parameters
                )
            except JobError:
                return self._error(ErrorCode.INVALID_STATE)

            terminal = job.state in ("completed", "failed")
            if task_kind == SYNTHETIC_WAITING_HUMAN_TASK and not terminal:
                # A waiting-human gate is a durable, non-authorizing stop; never
                # reopen a completed/cancelled job (idempotent replay).
                if job.state not in ("waiting_human", "paused"):
                    try:
                        repo.escalate_to_human(job.job_id)
                    except JobError:
                        pass
                self._wait_reasons[job.job_id] = "waiting_human_task"
            job = repo.get(job.job_id)

        if not terminal:
            try:
                self._ensure_execution(project_id)
            except _RequestError as exc:
                return self._error(exc.code)
            if project_id in self._paused_projects:
                return self._error(ErrorCode.DENIED)
            if self._draining:
                return self._error(ErrorCode.DRAINING)
        with self.open_repository(project_id) as repo:
            result = {"ok": True, "job": self._job_dict(repo, repo.get(job.job_id))}
        self._maybe_release_active()
        return result

    def _op_job_status(self, request: dict[str, Any]) -> dict[str, Any]:
        project_id = self._resolve_project(request)
        job_id = request.get("job_id")
        if not isinstance(job_id, str) or not job_id:
            raise _RequestError(ErrorCode.BAD_VALUE)
        try:
            with self.open_repository(project_id) as repo:
                job = repo.get(job_id)
                if job is None:
                    return self._error(ErrorCode.NO_SUCH_JOB)
                return {"ok": True, "job": self._job_dict(repo, job)}
        except ReviewAgentError as exc:
            raise _RequestError(ErrorCode.BAD_VALUE) from exc

    def _op_job_list(self, request: dict[str, Any]) -> dict[str, Any]:
        project_id = self._resolve_project(request)
        try:
            with self.open_repository(project_id) as repo:
                jobs = [self._job_dict(repo, job) for job in repo.list()]
        except ReviewAgentError as exc:
            raise _RequestError(ErrorCode.BAD_VALUE) from exc
        return {"ok": True, "project_id": project_id, "jobs": jobs}

    def _op_pause(self, request: dict[str, Any]) -> dict[str, Any]:
        return self._control(request, "pause")

    def _op_resume(self, request: dict[str, Any]) -> dict[str, Any]:
        return self._control(request, "resume")

    def _op_cancel(self, request: dict[str, Any]) -> dict[str, Any]:
        return self._control(request, "cancel")

    def _control(self, request: dict[str, Any], op: str) -> dict[str, Any]:
        project_id = self._resolve_project(request)
        job_id = request.get("job_id")
        if op == "resume":
            # Resume explicitly (re-)acquires execution, but never steals it from
            # another project.
            try:
                self._ensure_execution(project_id)
            except _RequestError as exc:
                return self._error(exc.code)
        try:
            with self.open_repository(project_id) as repo:
                if job_id is None:
                    if op == "pause":
                        self._pause_project_jobs(repo)
                        self._paused_projects.add(project_id)
                    elif op == "resume":
                        self._resume_project_jobs(repo)
                        self._paused_projects.discard(project_id)
                    else:
                        for job in repo.list():
                            if job.state in ("completed", "failed", "cancelled"):
                                continue
                            try:
                                repo.cancel(job.job_id)
                            except JobError:
                                pass
                    changed = [self._job_dict(repo, job) for job in repo.list()]
                    result: dict[str, Any] = {
                        "ok": True,
                        "project_id": project_id,
                        "jobs": changed,
                    }
                else:
                    if not isinstance(job_id, str) or not job_id:
                        raise _RequestError(ErrorCode.BAD_VALUE)
                    if repo.get(job_id) is None:
                        return self._error(ErrorCode.NO_SUCH_JOB)
                    try:
                        if op == "pause":
                            repo.pause(job_id)
                        elif op == "resume":
                            repo.resume(job_id)
                        else:
                            repo.cancel(job_id)
                    except JobError:
                        return self._error(ErrorCode.INVALID_STATE)
                    result = {"ok": True, "job": self._job_dict(repo, repo.get(job_id))}
        except ReviewAgentError as exc:
            raise _RequestError(ErrorCode.BAD_VALUE) from exc
        self._maybe_release_active()
        return result

    @staticmethod
    def _pause_project_jobs(repo: JobRepository) -> None:
        for job in repo.list():
            if job.state in ("completed", "failed", "cancelled"):
                continue
            try:
                repo.pause(job.job_id)
            except JobError:
                pass

    @staticmethod
    def _resume_project_jobs(repo: JobRepository) -> None:
        for job in repo.list():
            if job.state == "paused":
                try:
                    repo.resume(job.job_id)
                except JobError:
                    pass

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

        # An explicit owner reconcile is an authorized write for the active
        # project only: another executing project must never be crossed.
        current = self._registry.activation()
        if current.state == "none":
            return self._error(ErrorCode.NOT_ACTIVATED)
        if current.active_project_id != project_id:
            return self._error(ErrorCode.WRONG_PROJECT)

        try:
            with self.open_repository(project_id) as repo:
                if repo.get(job_id) is None:
                    return self._error(ErrorCode.NO_SUCH_JOB)
                try:
                    with self._write_guard(project_id, allow_draining=True):
                        job = repo.reconcile(
                            job_id,
                            resolution=resolution,
                            result_kind=request.get("result_kind"),
                            result=request.get("result"),
                        )
                except JobError:
                    return self._error(ErrorCode.INVALID_STATE)
                except ReviewAgentError:
                    return self._error(ErrorCode.WRONG_PROJECT)
                result = {"ok": True, "job": self._job_dict(repo, job)}
        except ReviewAgentError as exc:
            raise _RequestError(ErrorCode.BAD_VALUE) from exc
        self._maybe_release_active()
        return result

    def _op_drain(self, request: dict[str, Any]) -> dict[str, Any]:
        project_id = self._resolve_project(request)
        current = self._registry.activation()
        if current.state == "none":
            return self._error(ErrorCode.NOT_ACTIVATED)
        if current.active_project_id != project_id:
            return self._error(ErrorCode.WRONG_PROJECT)
        # Persist a durable pause on the old project's non-terminal jobs so a
        # restart/switch cannot silently unpause them.
        with self.open_repository(project_id) as repo:
            self._pause_project_jobs(repo)
        self._registry.begin_drain(current.activation_token)
        self._activation = _ActiveExecution(project_id, current.activation_token)
        self._draining = True
        self._paused_projects.add(project_id)
        return {"ok": True, "state": "draining", "project_id": project_id}

    def _op_switch(self, request: dict[str, Any]) -> dict[str, Any]:
        project_id = request.get("project_id")
        if not isinstance(project_id, str) or not project_id:
            raise _RequestError(ErrorCode.BAD_VALUE)
        try:
            self._registry.get(project_id)  # validate registration
        except ReviewAgentError as exc:
            raise _RequestError(ErrorCode.BAD_VALUE) from exc

        current = self._registry.activation()
        if current.state == "none":
            activated = self._registry.activate_execution(project_id)
            self._activation = _ActiveExecution(project_id, activated.activation_token)
            self._draining = False
            return {"ok": True, "switched": True, "active_project_id": project_id}

        if current.active_project_id == project_id:
            self._activation = _ActiveExecution(project_id, current.activation_token)
            self._draining = current.state == "draining"
            return {"ok": True, "switched": True, "active_project_id": project_id}

        # Safe switch: persist a durable pause, drain, reconcile/quiesce the old
        # project's remote work, then release + activate the new one.
        old_pid = current.active_project_id
        token = current.activation_token
        with self.open_repository(old_pid) as repo:
            self._pause_project_jobs(repo)
        self._registry.begin_drain(token)
        self._activation = _ActiveExecution(old_pid, token)
        self._draining = True
        self._paused_projects.add(old_pid)

        for _ in range(32):
            if self._quiesce_project(old_pid):
                break
            hint = self._tick_project(old_pid)
            if hint is None or hint > 0:
                break

        if not self._quiesce_project(old_pid):
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
        return {
            "ok": True,
            "app": APP_NAME,
            "protocol_version": PROTOCOL_VERSION,
            "pid": os.getpid(),
            "stopping": True,
        }

    # -- reconciliation / startup -------------------------------------------- #
    def reconcile_startup(self) -> None:
        """Reconcile recorded attempts across **all** registered project stores.

        Must run only after the worker owns its singleton lock (see
        ``worker/__main__``): it opens each project store, recovers any active
        attempt to an explicit state (never a silent success), and closes the
        handle so no inactive connection is cached. A store that cannot be read
        is skipped; it is reconciled again on a later run.
        """
        try:
            projects = self._registry.list_projects()
        except ReviewAgentError:
            return
        for info in projects:
            try:
                with self.open_repository(info.project_id) as repo:
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
            except (ReviewAgentError, OSError):
                continue

    # -- scheduling ---------------------------------------------------------- #
    def tick(self) -> float | None:
        """Advance one unit of scheduling/reconciliation; return a wait hint.

        Returns ``0.0`` when it made progress (call again promptly), a positive
        number of seconds to wait before the next poll, or ``None`` when there is
        no pending work (the caller should block indefinitely). It never
        busy-loops: an ambiguous/unknown outcome becomes ``waiting_human`` and
        yields ``None`` until an owner request or a known backend deadline.
        """
        if self._stopping:
            return 0.0
        active = self._activation
        if active is None:
            return None
        hint = self._tick_project(active.project_id)
        # Release arbitration once the active project has nothing left to do.
        self._maybe_release_active()
        return hint

    def _scheduling_allowed(self, project_id: str) -> bool:
        active = self._activation
        if active is None or active.project_id != project_id:
            return False
        if self._draining:
            return False
        if project_id in self._paused_projects:
            return False
        return True

    def _tick_project(self, project_id: str) -> float | None:
        try:
            with self.open_repository(project_id) as repo:
                plan = self._plan(repo, project_id)
                if plan is None:
                    return None
                action, job, attempt = plan
                if action == "claim":
                    return self._do_claim(repo, project_id, job)
                if action == "admit":
                    return self._do_admit(repo, project_id, job, attempt)
                if action == "lookup":
                    return self._do_lookup(repo, project_id, job, attempt)
                return self._do_reconcile(repo, project_id, job, attempt)
        except (ReviewAgentError, OSError):
            return None

    def _plan(self, repo: JobRepository, project_id: str) -> tuple | None:
        """Pick the next action, separating scheduling from reconciliation.

        New dispatch (``claim``/``admit``) is returned only when scheduling is
        allowed. Reconciliation (``lookup``/``reconcile``) of an already-admitted
        attempt is returned even while draining/paused/cancelled, so remote work
        still finishes under an ``allow_draining`` guard.
        """
        scheduling = self._scheduling_allowed(project_id)
        for job in repo.list():
            if job.state in ("waiting_human", "waiting_external"):
                # A waiting gate is never polled (no busy loop).
                continue
            attempt = (
                None
                if job.current_attempt_id is None
                else repo.attempt(job.current_attempt_id)
            )
            if job.state in ("reconciliation_required", "interrupted") and attempt is not None:
                return ("reconcile", job, attempt)
            if attempt is not None and attempt.state in ACTIVE_ATTEMPT_STATES:
                if attempt.state == "queued" and job.state == "queued" and scheduling:
                    return ("admit", job, attempt)
                if attempt.state in ("admitted", "running"):
                    return ("lookup", job, attempt)
                # A queued attempt that may not be admitted now: no action.
                continue
            if job.state == "queued" and attempt is None and scheduling:
                return ("claim", job, None)
        return None

    def _do_claim(
        self, repo: JobRepository, project_id: str, job: Job
    ) -> float | None:
        params = job.parameters if isinstance(job.parameters, dict) else {}
        stage = params.get("stage")
        reserve = params.get("reserve_usd")
        if not isinstance(stage, str) or not isinstance(reserve, str):
            repo.escalate_to_human(job.job_id)
            self._wait_reasons[job.job_id] = "invalid_parameters"
            return None
        try:
            # New dispatch: active-only guard (no draining).
            with self._write_guard(project_id, allow_draining=False):
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
            # 2) admit under the same stable attempt identity (the dispatch id);
            #    external work is done outside the registry lock.
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
            # 3) record the durable admitted state under the active-only guard.
            with self._write_guard(project_id, allow_draining=False):
                repo.mark_admitted(
                    attempt.attempt_id, admission_id="adm_" + attempt.attempt_id
                )
        except SyntheticBackendError:
            repo.escalate_to_human(job.job_id)
            self._wait_reasons[job.job_id] = "admission_indeterminate"
            return None
        except JobError:
            return None
        except ReviewAgentError:
            return None
        return 0.0

    def _do_lookup(
        self, repo: JobRepository, project_id: str, job: Job, attempt: Any
    ) -> float | None:
        continuity = self._checkpoint_continuity(attempt)
        if continuity is None:
            # No continuity checkpoint ⇒ by ordering the dispatch could not have
            # been issued: reopen the same attempt identity (never mint a new one).
            return self._resume_same_attempt(repo, project_id, job, attempt)
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
        if result.status == "absent" and result.definitive:
            # A definitively-absent identity was never admitted: same identity.
            return self._resume_same_attempt(repo, project_id, job, attempt)
        if result.status == "failed" and result.definitive_failure:
            try:
                # Reconciliation of known work: allow_draining finish.
                with self._write_guard(project_id, allow_draining=True):
                    repo.mark_definitive_failure(attempt.attempt_id)
                    repo.settle_cost(attempt.attempt_id, cost_state="not_incurred")
            except (JobError, ReviewAgentError):
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
        # retain arbitration. Never a new identity, never a silent success, and
        # never polled in a busy loop (the job becomes waiting_human).
        try:
            with self._write_guard(project_id, allow_draining=True):
                repo.mark_ambiguous(attempt.attempt_id)
                repo.escalate_to_human(job.job_id)
        except (JobError, ReviewAgentError):
            return None
        self._wait_reasons[job.job_id] = result.reason or "reconciliation_required"
        return None

    def _resume_same_attempt(
        self, repo: JobRepository, project_id: str, job: Job, attempt: Any
    ) -> float | None:
        try:
            with self._write_guard(project_id, allow_draining=True):
                repo.resume_proven_undispatched(attempt.attempt_id)
        except (JobError, ReviewAgentError):
            return None
        return 0.0

    def _do_reconcile(
        self, repo: JobRepository, project_id: str, job: Job, attempt: Any
    ) -> float | None:
        # Recovery already recorded the attempt as interrupted/ambiguous. Decide
        # from durable state, with the ordering argument for a missing checkpoint.
        return self._do_lookup(repo, project_id, job, attempt)

    def _accept_result(
        self,
        repo: JobRepository,
        project_id: str,
        job: Job,
        attempt: Any,
        result: Any,
    ) -> float | None:
        self._fire(
            "before_commit",
            project_id=project_id,
            job_id=job.job_id,
            attempt_id=attempt.attempt_id,
        )
        try:
            # allow_draining=True is reconciliation-only: finishing a write the
            # engine had already accepted before a drain/pause. Fencing protects
            # accepted local writes; a paused/cancelled job keeps its state.
            with self._write_guard(project_id, allow_draining=True):
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
