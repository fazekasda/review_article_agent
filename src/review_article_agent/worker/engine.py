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
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Callable, Iterator

from ..acquisition.transport import http_get as acquisition_http_get
from ..discovery.execution import (
    DiscoveryExecutionBlockedError,
    DiscoveryExecutionError,
)
from ..discovery.lead_runner import HostedLeadError
from ..discovery.transport import DiscoveryRequestInterrupted, http_get
from ..domain.errors import ProjectConflict, ReviewAgentError
from ..domain.records import validate_identifier
from ..runtime.dispatch import (
    RuntimeDispatchRepository,
    runtime_dispatch_remote_state,
)
from ..runtime.jobs import (
    ACTIVE_ATTEMPT_STATES,
    BudgetExceededError,
    Job,
    JobConflictError,
    JobError,
    JobRepository,
    JobValidationError,
    StaleFenceError,
    validate_reserve_usd,
)
from ..storage import paths
from ..storage.registry import Registry
from ..storage.store import ProjectStore
from . import acquisition as acquisition_jobs
from . import discovery as discovery_jobs
from .discovery import (
    AttemptCounter,
    DiscoveryJobStopped,
    DiscoveryJobValidationError,
)
from .synthetic import (
    MAX_DELAY_MS,
    MAX_STEPS,
    MIN_DELAY_MS,
    MIN_STEPS,
    SyntheticBackend,
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
    "discovery_submit",
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
    CONFLICT = "conflict"
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
    ErrorCode.CONFLICT: "the request diverges from an already-recorded job",
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
    "discovery_submit": frozenset(
        {
            "version",
            "op",
            "project_id",
            "request_id",
            "mode",
            "parameters",
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
        discovery_transport: Callable[..., Any] | None = None,
        lead_transport: Callable[..., Any] | None = None,
        lead_api_key: str | None = None,
        acquisition_transport: Callable[..., Any] | None = None,
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
        self._owner_audit: list[dict[str, Any]] = []
        self._hooks: dict[str, list[Callable[..., None]]] = {
            name: [] for name in FAULT_SEAMS
        }
        # Discovery execution: the default low-level transport is the S1 real
        # HTTP GET, but it is only ever invoked for an explicit discovery job
        # (never automatically on boot). Tests inject a deterministic fake here.
        lowlevel = http_get if discovery_transport is None else discovery_transport
        if not callable(lowlevel):
            raise ValueError("discovery_transport must be a callable low-level transport")
        self._discovery_lowlevel = lowlevel
        # Official full-text retrieval (Step 06): a *separate* low-level seam. The
        # discovery ``http_get`` refuses full-text paths, so the default is the
        # acquisition transport's own fixed-endpoint GET; trusted Python code (a
        # test fixture) may inject a no-egress callable. Never reachable from JSON.
        acq_lowlevel = acquisition_http_get if acquisition_transport is None else acquisition_transport
        if not callable(acq_lowlevel):
            raise ValueError("acquisition_transport must be a callable low-level transport")
        self._acquisition_lowlevel = acq_lowlevel
        # Hosted lead: the default transport is the real direct-lead adapter (a
        # single POST to the one documented endpoint); it is only ever used for an
        # explicit, fully-guarded lead job. A test may inject a deterministic fake
        # transport and/or an explicit key. The credential is read from the
        # environment only at dispatch; it is never persisted.
        self._lead_transport = lead_transport
        self._lead_api_key = lead_api_key
        self._discovery_pool: ThreadPoolExecutor | None = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="raa-discovery"
        )
        self._discovery_future: Future[Any] | None = None
        self._discovery_job: tuple[str, str, str] | None = None
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

        No project store is held between calls. The discovery executor is shut
        down **without cancelling an authorized in-flight job**: a running task is
        awaited so a worker stop *drains* it rather than aborting it (queued jobs
        are never left in the pool — the engine keeps at most one active future,
        started only by :meth:`tick`). Closing the CLI never reaches here; only a
        real worker stop does.
        """
        self._wait_reasons.clear()
        pool, self._discovery_pool = self._discovery_pool, None
        if pool is not None:
            try:
                pool.shutdown(wait=True, cancel_futures=False)
            except Exception:  # noqa: BLE001 - shutdown must never raise
                pass
        self._discovery_future = None
        self._discovery_job = None

    # -- store access -------------------------------------------------------- #
    @contextmanager
    def open_store(self, project_id: str) -> Iterator[ProjectStore]:
        """Yield a :class:`ProjectStore` for ``project_id``, closing it on exit.

        The store location is re-resolved from the registry on every open, so a
        relocated inactive project is always read at its current location and no
        connection outlives the call. This is the only path by which the engine
        touches a project store; both the synthetic job repository and the
        runtime dispatch ledger are built over the same handle. A store is always
        resolved **before** any registry write guard (registry locks are not
        reentrant).
        """
        info = self._registry.get(project_id)  # validates id + registration
        app_dir = paths.app_dir_for(info.root_path)
        store = ProjectStore.open_at(app_dir, expected_project_id=project_id)
        try:
            yield store
        finally:
            store.close()

    @contextmanager
    def open_repository(self, project_id: str) -> Iterator[JobRepository]:
        """Yield a synthetic :class:`JobRepository` for ``project_id``.

        Thin wrapper over :meth:`open_store` for callers that only need the
        synthetic job/attempt layer.
        """
        with self.open_store(project_id) as store:
            yield JobRepository(store)

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
                "result": discovery_jobs.status_projection(
                    accepted.result_kind, accepted.result
                ),
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
    def _job_has_potential_remote(self, repo: JobRepository, job: Job) -> bool:
        """True if ``job`` currently occupies the serial remote slot.

        Counts **attempt-level** unresolved flags (not only the job flag): any
        unresolved job or attempt, a ``waiting_external`` gate, an
        admitted/running attempt, or a queued attempt that still carries a
        continuity checkpoint. A queued attempt marked ``proven_undispatched``
        (no continuity) is predispatch: by ordering it cannot have been issued.
        """
        if job.outcome_unresolved or job.state == "waiting_external":
            return True
        for attempt in repo.attempts(job.job_id):
            if attempt.outcome_unresolved:
                return True
            if attempt.state in ("admitted", "running"):
                return True
            if attempt.state == "queued" and (
                self._checkpoint_continuity(attempt) is not None
                or discovery_jobs.is_predispatch_checkpoint(attempt.checkpoint)
            ):
                return True
        return False

    def _remote_state(self, project_id: str) -> str:
        """Classify a project's potential remote work: ``remote``/``none``/``unreadable``.

        Both layers count: the synthetic job/attempt layer **and** the durable
        runtime dispatch ledger (C02 §8.3/§8.5). A runtime dispatch that is
        ``submitted``/``admitted``/``unknown`` still occupies the single serial
        remote slot and pins the project's arbitration even when its cost is
        settled or the project holds no schedulable synthetic job.

        Fail closed: an unreadable store — or a runtime ledger that cannot be
        proven intact — is ``unreadable`` and is treated as remote by callers
        rather than silently as safe. The runtime ledger is classified first, so
        corruption is never masked by an otherwise-clean synthetic scan.
        """
        try:
            with self.open_store(project_id) as store:
                runtime_state = runtime_dispatch_remote_state(
                    store.connection, project_id
                )
                if runtime_state != "none":
                    return runtime_state
                repo = JobRepository(store)
                for job in repo.list():
                    if self._job_has_potential_remote(repo, job):
                        return "remote"
            return "none"
        except (ReviewAgentError, OSError):
            return "unreadable"

    def _has_remote(self, project_id: str) -> bool:
        """True if any job carries potential remote work (pins arbitration).

        Fail closed: an unreadable store counts as potential remote work.
        """
        return self._remote_state(project_id) != "none"

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

    def _registry_projects(self) -> list[Any] | None:
        """Return the registered projects, or ``None`` if the registry is unavailable."""
        try:
            return self._registry.list_projects()
        except ReviewAgentError:
            return None

    def _recovery_report(self) -> tuple[list[str], list[str], bool]:
        """Return ``(remote_projects, unreadable_projects, registry_available)``.

        ``registry_available`` is ``False`` when the project list cannot be read;
        callers must then fail closed (block new dispatch) rather than report a
        clean bill of health.
        """
        projects = self._registry_projects()
        if projects is None:
            return [], [], False
        remote: list[str] = []
        unreadable: list[str] = []
        for info in projects:
            state = self._remote_state(info.project_id)
            if state == "unreadable":
                unreadable.append(info.project_id)
            elif state == "remote":
                remote.append(info.project_id)
        return remote, unreadable, True

    def _new_dispatch_blockers(self, project_id: str) -> dict[str, Any] | None:
        """Return recovery blockers that forbid NEW dispatch for ``project_id``.

        A different store with unresolved remote work, any unreadable store
        (which cannot be proven safe), or an unreadable registry forbids new
        dispatch. The project's own remote work is not a blocker.
        """
        remote, unreadable, available = self._recovery_report()
        if not available:
            return {
                "recovery_projects": [],
                "unreadable_projects": [],
                "registry_unavailable": True,
            }
        other_remote = [pid for pid in remote if pid != project_id]
        if other_remote or unreadable:
            return {
                "recovery_projects": other_remote,
                "unreadable_projects": unreadable,
            }
        return None

    def _has_remote_elsewhere(self, project_id: str) -> bool:
        return self._new_dispatch_blockers(project_id) is not None

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
        remote, unreadable, available = self._recovery_report()
        return {
            "ok": True,
            "app": APP_NAME,
            "protocol_version": PROTOCOL_VERSION,
            "pid": os.getpid(),
            "state": state,
            "activation_state": activation.state,
            "active_project_id": activation.active_project_id,
            "paused_projects": sorted(self._paused_projects),
            "recovery_projects": remote,
            "unreadable_projects": unreadable,
            "registry_available": available,
            "owner_reconciliations": len(self._owner_audit),
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
            # Bounded exact-decimal validation BEFORE persistence, so a huge
            # exponent / zero / boolean / negative is rejected outright rather
            # than leaving a job stuck queued.
            reserve_usd = self._validate_reserve(request.get("reserve_usd"))
            parameters = {
                "steps": steps,
                "delay_ms": delay_ms,
                "outcome": outcome,
                "stage": stage,
                "reserve_usd": reserve_usd,
            }

        # 1) Recovery blockers are checked BEFORE any durable queuing: a different
        #    store with unresolved remote work (or any unreadable store) forbids
        #    new dispatch, and no job row is created.
        blockers = self._new_dispatch_blockers(project_id)
        if blockers is not None:
            return self._blocked(blockers)

        # 2) Another project must not be silently drained for this submit.
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

        # 3) Acquire/verify execution BEFORE persisting the job.
        try:
            self._ensure_execution(project_id)
        except _RequestError as exc:
            return self._error(exc.code)

        # 4) Persist the (idempotent) job row.
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
            result = {"ok": True, "job": self._job_dict(repo, job)}
        self._maybe_release_active()
        return result

    def _op_discovery_submit(self, request: dict[str, Any]) -> dict[str, Any]:
        """Admit one owner-controlled discovery job (queued, not completed).

        Strictly validates the mode/parameters *before* any durable write or
        activation change, persists one immutable ``JobRepository`` job under the
        caller request id (a divergent replay is a conflict), and acquires the
        same global registry execution token new dispatch uses. The provider work
        is scheduled asynchronously by :meth:`tick`; this operation returns the
        queued admission only.
        """
        project_id = self._resolve_project(request)

        request_id = request.get("request_id")
        if not isinstance(request_id, str) or not request_id:
            raise _RequestError(ErrorCode.BAD_TYPE)
        try:
            validate_identifier(request_id, "request_id")
        except ReviewAgentError as exc:
            raise _RequestError(ErrorCode.BAD_VALUE) from exc

        mode = request.get("mode")
        if mode not in discovery_jobs.DISCOVERY_MODES:
            raise _RequestError(ErrorCode.BAD_VALUE)
        try:
            parameters = discovery_jobs.validate_submission(
                mode, request.get("parameters"), project_id, request_id
            )
        except DiscoveryJobValidationError as exc:
            raise _RequestError(exc.code) from exc

        # 1) Recovery blockers forbid new durable queuing (no job row created).
        blockers = self._new_dispatch_blockers(project_id)
        if blockers is not None:
            return self._blocked(blockers)

        # 2) Another project must not be silently drained for this submission.
        current = self._registry.activation()
        if current.state != "none" and current.active_project_id != project_id:
            return self._error(ErrorCode.WRONG_PROJECT)
        if project_id in self._paused_projects:
            return self._error(ErrorCode.DENIED)
        if (
            self._draining
            and self._activation is not None
            and self._activation.project_id == project_id
        ):
            return self._error(ErrorCode.DRAINING)

        # 3) Acquire/verify execution BEFORE persisting the job.
        try:
            self._ensure_execution(project_id)
        except _RequestError as exc:
            return self._error(exc.code)

        # 4) Persist the (idempotent) discovery job; a divergent replay conflicts.
        task_kind = discovery_jobs.task_kind_for_mode(mode)
        with self.open_repository(project_id) as repo:
            try:
                job = repo.submit(
                    request_id, task_kind=task_kind, parameters=parameters
                )
            except JobConflictError:
                return self._error(ErrorCode.CONFLICT)
            except JobError:
                return self._error(ErrorCode.INVALID_STATE)
            result = {"ok": True, "queued": True, "job": self._job_dict(repo, job)}
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

        # Fixed synthetic protocol: an arbitrary/untrusted result payload is
        # rejected before any write, and no resolution other than
        # ``accepted_local_result`` may carry result fields at all.
        result_kind = request.get("result_kind")
        result = request.get("result")
        if resolution == "accepted_local_result":
            result = self._validate_reconcile_result(result_kind, result)
            result_kind = "synthetic_numeric"
        else:
            if result_kind is not None or result is not None:
                raise _RequestError(ErrorCode.BAD_VALUE)

        # An explicit owner reconcile is a **known-work disposition**. When no
        # activation is held (e.g. several restored unknown projects), it may
        # acquire a RECONCILIATION-ONLY activation (immediately draining, so no
        # new dispatch) for one project while other blockers remain, then release
        # it. It never crosses or steals another executing project.
        current = self._registry.activation()
        acquired_token: str | None = None
        if current.state == "none":
            try:
                activated = self._registry.activate_execution(project_id)
            except ReviewAgentError:
                return self._error(ErrorCode.WRONG_PROJECT)
            acquired_token = activated.activation_token
            try:
                self._registry.begin_drain(acquired_token)
            except ReviewAgentError:
                self._release_token_quiet(acquired_token)
                return self._error(ErrorCode.WRONG_PROJECT)
            self._activation = _ActiveExecution(project_id, acquired_token)
            self._draining = True
        elif current.active_project_id != project_id:
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
                            result_kind=result_kind,
                            result=result,
                        )
                except JobError:
                    return self._error(ErrorCode.INVALID_STATE)
                except ReviewAgentError:
                    return self._error(ErrorCode.WRONG_PROJECT)
                payload: dict[str, Any] = {
                    "ok": True,
                    "job": self._job_dict(repo, job),
                    "owner_control": True,
                }
        except ReviewAgentError as exc:
            raise _RequestError(ErrorCode.BAD_VALUE) from exc
        finally:
            if acquired_token is not None and self._quiesce_project(project_id):
                self._release_token_quiet(acquired_token)
                self._activation = None
                self._draining = False
        # Explicit owner control is recorded (in-memory audit; not a scientific
        # approval and not a durable store in this step).
        self._owner_audit.append(
            {
                "project_id": project_id,
                "job_id": job_id,
                "resolution": resolution,
                "acquired": acquired_token is not None,
            }
        )
        del self._owner_audit[:-100]
        self._maybe_release_active()
        return payload

    def _release_token_quiet(self, token: str) -> None:
        try:
            self._registry.release_execution(token)
        except ReviewAgentError:
            return

    @staticmethod
    def _validate_reconcile_result(result_kind: Any, result: Any) -> dict[str, Any]:
        """Constrain an owner-supplied accepted result to the fixed synthetic shape.

        Exactly ``result_kind == "synthetic_numeric"`` and
        ``result == {"kind": "synthetic_numeric", "value": <int 0..999999>}``.
        Any other kind, extra/unknown field, string payload, boolean, or
        out-of-range value is rejected before any write.
        """
        if result_kind != "synthetic_numeric":
            raise _RequestError(ErrorCode.BAD_VALUE)
        if not isinstance(result, dict) or set(result) != {"kind", "value"}:
            raise _RequestError(ErrorCode.BAD_VALUE)
        if result.get("kind") != "synthetic_numeric":
            raise _RequestError(ErrorCode.BAD_VALUE)
        value = result.get("value")
        if isinstance(value, bool) or not isinstance(value, int) or not (0 <= value <= 999999):
            raise _RequestError(ErrorCode.BAD_VALUE)
        return {"kind": "synthetic_numeric", "value": value}

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
            remote, unreadable, available = self._recovery_report()
            if not available:
                return self._blocked(
                    {
                        "recovery_projects": [],
                        "unreadable_projects": [],
                        "registry_unavailable": True,
                    }
                )
            if unreadable:
                return self._blocked(
                    {"recovery_projects": remote, "unreadable_projects": unreadable}
                )
            other_remote = [pid for pid in remote if pid != project_id]
            if other_remote:
                # A restored project still has unresolved remote work: it keeps
                # arbitration and the new project must not execute yet.
                return self._blocked(
                    {"recovery_projects": other_remote, "unreadable_projects": []}
                )
            # If the target itself carries unresolved remote work, adopting it is
            # what lets the engine reconcile it; either way it becomes active.
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
            return self._blocked(
                {"recovery_projects": [old_pid], "unreadable_projects": []}
            )
        # No OTHER store may still hold unresolved remote work either.
        remote, unreadable, available = self._recovery_report()
        if not available:
            return self._blocked(
                {
                    "recovery_projects": [old_pid],
                    "unreadable_projects": [],
                    "registry_unavailable": True,
                }
            )
        other_remote = [pid for pid in remote if pid not in (old_pid, project_id)]
        if other_remote or unreadable:
            return self._blocked(
                {
                    "recovery_projects": [old_pid, *other_remote],
                    "unreadable_projects": unreadable,
                }
            )
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
        """Reconcile recorded attempts and runtime dispatches across **all** stores.

        Must run only after the worker owns its singleton lock (see
        ``worker/__main__``): it opens each project store, recovers any active
        synthetic attempt to an explicit state (never a silent success), recovers
        any outstanding runtime dispatch (``submitted`` -> ``unknown``, once),
        and closes the handle so no inactive connection is cached. A store that
        cannot be read is **never** silently skipped as safe: it is reported as an
        unreadable recovery blocker (visible via ``status``) that forbids new
        dispatch.

        After recovery, if no execution activation is held and exactly one
        project still has unresolved remote work (synthetic or runtime), that
        project is adopted (made the execution-active project) so the engine can
        reconcile it. Multiple pending projects, or any unreadable store, leave
        execution unacquired and all new dispatch/switch blocked pending an
        explicit owner decision.
        """
        try:
            projects = self._registry.list_projects()
        except ReviewAgentError:
            return
        for info in projects:
            try:
                with self.open_store(info.project_id) as store:
                    repo = JobRepository(store)
                    for job in repo.list():
                        if discovery_jobs.is_discovery_task_kind(job.task_kind):
                            # Discovery attempts are reconciled explicitly by the
                            # scheduling loop (a local-kind recover would wrongly
                            # treat a possibly-dispatched attempt as retry-safe).
                            continue
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
                    self._recover_runtime_dispatches(store)
            except (ReviewAgentError, OSError):
                continue
        self._auto_adopt_recovery()

    @staticmethod
    def _recover_runtime_dispatches(store: ProjectStore) -> None:
        """Recover outstanding runtime dispatches after a restart, exactly once.

        Mirrors the synthetic attempt recovery: this is a durable restart
        reconciliation, so it mints **no** new identity, issues **no** retry, and
        releases **no** reserve. Only an outstanding ``submitted`` dispatch
        transitions (to ``unknown``) via the ledger's own
        :meth:`RuntimeDispatchRepository.recover`; an ``opencode_session``
        ``admitted`` dispatch is retained unchanged for same-identity
        reconciliation, and ``reserved``/``unknown``/terminal states are left
        untouched. A corrupt ledger fails closed (no write) rather than being
        partially recovered.

        The store handle is already resolved by the caller and no registry guard
        is taken here (the startup scan runs under the worker's singleton lock,
        matching the synthetic recovery path); no registry call is made.
        """
        try:
            if (
                runtime_dispatch_remote_state(store.connection, store.project_id)
                == "unreadable"
            ):
                # A ledger that cannot be proven intact is never partially
                # recovered: fail closed and leave it for the owner/backup path.
                return
            runtime = RuntimeDispatchRepository(store)
            for record in runtime.list_dispatches():
                if record.state == "submitted":
                    runtime.recover(record.dispatch_id)
        except ReviewAgentError:
            return

    def _auto_adopt_recovery(self) -> None:
        """Adopt the sole pending-remote project so it can be reconciled."""
        current = self._registry.activation()
        if current.state != "none":
            return
        remote, unreadable, available = self._recovery_report()
        if not available or unreadable or len(remote) != 1:
            return
        project_id = remote[0]
        try:
            activated = self._registry.activate_execution(project_id)
        except ReviewAgentError:
            return
        self._activation = _ActiveExecution(project_id, activated.activation_token)
        self._draining = False

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
        # A running discovery job occupies the single serial executor: poll it
        # without blocking the control loop, and start nothing else meanwhile.
        if self._discovery_future is not None:
            if self._discovery_future.done():
                self._poll_discovery_future()
                return 0.0
            return 0.05
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
        # A queued authorization is not permission to ignore a later recovery
        # blocker. Re-check before every new claim/admission, not just submit.
        if self._new_dispatch_blockers(project_id) is not None:
            return False
        return True

    # -- discovery jobs ------------------------------------------------------ #
    def _active_token(self, project_id: str) -> str | None:
        """Return the current execution token when it authorizes ``project_id``.

        Used to build the S1 executor; the S1 library revalidates the activation
        (``active`` only) before every attempt, so a stale/absent token simply
        blocks the job rather than dispatching under stale authority.
        """
        try:
            current = self._registry.activation()
        except ReviewAgentError:
            return None
        if current.state == "active" and current.active_project_id == project_id:
            return current.activation_token
        return None

    def _poll_discovery_future(self) -> None:
        """Clear a finished discovery future and surface (swallow) its outcome.

        The task records every durable outcome itself; the future is only a
        liveness handle, so any exception is retrieved and dropped (never a raw
        trace, never a crash in the control thread).
        """
        future = self._discovery_future
        self._discovery_future = None
        self._discovery_job = None
        if future is None:
            return
        try:
            future.result()
        except Exception:  # noqa: BLE001 - the task owns its error handling
            pass

    def _maybe_start_discovery(
        self,
        repo: JobRepository,
        project_id: str,
        jobs: list[Job],
        *,
        scheduling: bool,
        occupied: bool,
    ) -> bool:
        """Claim + admit one queued discovery job and start it asynchronously.

        Only starts when the serial slot is free and scheduling is allowed; the
        durable claim/admit writes happen here (fast, local) while the provider
        work runs on the executor's own thread with its own store connection.
        """
        if self._discovery_pool is None or self._discovery_future is not None:
            return False
        if not scheduling or occupied:
            return False
        if self._active_token(project_id) is None:
            return False
        for job in jobs:
            if not discovery_jobs.is_discovery_task_kind(job.task_kind):
                continue
            if job.state != "queued":
                continue
            attempt = (
                None
                if job.current_attempt_id is None
                else repo.attempt(job.current_attempt_id)
            )
            if attempt is not None:
                if attempt.state != "queued":
                    continue
                if discovery_jobs.is_predispatch_checkpoint(attempt.checkpoint):
                    continue  # a possibly-dispatched attempt is reconciled, never restarted
            try:
                if attempt is None:
                    with self._write_guard(project_id, allow_draining=False):
                        attempt = repo.begin_attempt(job.job_id, dispatch_kind="local")
                with self._write_guard(project_id, allow_draining=False):
                    repo.record_checkpoint(
                        attempt.attempt_id,
                        discovery_jobs.predispatch_checkpoint(
                            discovery_jobs.mode_for_task_kind(job.task_kind)
                        ),
                    )
            except (JobError, ReviewAgentError):
                return False
            try:
                future = self._discovery_pool.submit(
                    self._run_discovery_job,
                    project_id,
                    job.job_id,
                    attempt.attempt_id,
                )
            except RuntimeError:  # pool already shut down
                return False
            self._discovery_future = future
            self._discovery_job = (project_id, job.job_id, attempt.attempt_id)
            return True
        return False

    def _tick_discovery(
        self, store: Any, repo: JobRepository, project_id: str, jobs: list[Job]
    ) -> bool:
        """Reconcile one possibly-dispatched discovery attempt; return progress.

        A standing local observation completes the same fence; an attempt that
        *may* have dispatched but has no observation is escalated (never
        blind-retried); a provably undispatched attempt is reopened. A fresh
        queued attempt (never dispatched) is left to :meth:`_maybe_start_discovery`.
        """
        active_job = self._discovery_job[1] if self._discovery_job else None
        for job in jobs:
            if not discovery_jobs.is_discovery_task_kind(job.task_kind):
                continue
            if job.job_id == active_job:
                continue
            attempt_id = job.current_attempt_id
            if attempt_id is None:
                continue
            attempt = repo.attempt(attempt_id)
            if attempt is None or attempt.state in ("succeeded", "definitive_failure"):
                continue
            if attempt.state not in (
                "queued",
                "interrupted",
                "ambiguous",
                "admitted",
                "running",
            ):
                continue
            if attempt.state == "queued" and not discovery_jobs.is_predispatch_checkpoint(
                attempt.checkpoint
            ):
                # Proven never-dispatched: wait for a schedulable start.
                continue
            return self._reconcile_discovery_attempt(
                store, repo, project_id, job, attempt
            )
        return False

    def _reconcile_discovery_attempt(
        self, store: Any, repo: JobRepository, project_id: str, job: Job, attempt: Any
    ) -> bool:
        token = self._active_token(project_id)
        outcome = None
        if token is not None:
            outcome = discovery_jobs.replay_only(store, self._registry, token, job)
        if outcome is not None:
            if outcome.complete:
                try:
                    with self._write_guard(project_id, allow_draining=True):
                        repo.accept_result(
                            attempt.attempt_id,
                            result_kind=outcome.result_kind,
                            result=outcome.result,
                        )
                except (JobError, ReviewAgentError):
                    return False
                return True
            # An explicit **missing remainder**: the traversal needs a provider
            # request that was never persisted, so this is never a silent
            # completion (and no new request is ever sent here). Escalate once;
            # an already-escalated attempt is not re-escalated (no busy loop).
            if attempt.state == "ambiguous":
                return False
            try:
                with self._write_guard(project_id, allow_draining=True):
                    repo.mark_ambiguous(attempt.attempt_id)
                    repo.escalate_to_human(job.job_id)
            except (JobError, ReviewAgentError):
                return False
            self._wait_reasons[job.job_id] = "discovery_incomplete"
            return True

        predispatch = discovery_jobs.is_predispatch_checkpoint(attempt.checkpoint)
        may_have_dispatched = predispatch or attempt.state in (
            "admitted",
            "running",
            "ambiguous",
        )
        try:
            if may_have_dispatched:
                if attempt.state == "ambiguous":
                    # Already recorded as an explicit unknown outcome: never
                    # re-escalate (or poll) in a loop.
                    return False
                with self._write_guard(project_id, allow_draining=True):
                    repo.mark_ambiguous(attempt.attempt_id)
                    repo.escalate_to_human(job.job_id)
                self._wait_reasons[job.job_id] = "discovery_reconciliation_required"
            else:
                with self._write_guard(project_id, allow_draining=True):
                    repo.resume_proven_undispatched(attempt.attempt_id)
        except (JobError, ReviewAgentError):
            return False
        return True

    def _run_discovery_job(
        self, project_id: str, job_id: str, attempt_id: str
    ) -> None:
        """Execute one discovery job on a worker thread with its own store.

        The store location is re-resolved from the registry here (never carried
        across the thread lifetime), and the S1 library performs the provider I/O
        and the fenced local commit on this thread's connection.
        """
        try:
            with self.open_store(project_id) as store:
                self._run_discovery_job_inner(store, project_id, job_id, attempt_id)
        except (ReviewAgentError, OSError):
            # Cannot even resolve/open the store: leave the durable attempt for
            # restart reconciliation (never a silent success).
            return

    def _run_discovery_job_inner(
        self, store: Any, project_id: str, job_id: str, attempt_id: str
    ) -> None:
        repo = JobRepository(store)
        job = repo.get(job_id)
        if job is None or job.current_attempt_id != attempt_id:
            return
        attempt = repo.attempt(attempt_id)
        if attempt is None or attempt.state != "queued":
            return

        if job.state in ("paused", "cancelled"):
            self._discovery_fail(
                repo, project_id, job, attempt, attempts=0, reason="stopped"
            )
            return

        token = self._active_token(project_id)
        if token is None:
            self._discovery_fail(
                repo, project_id, job, attempt, attempts=0, reason="not_active"
            )
            return

        mode = discovery_jobs.mode_for_task_kind(job.task_kind)
        if mode == discovery_jobs.MODE_LEAD:
            self._run_lead_dispatch(store, repo, project_id, job, attempt, token)
            return

        if mode == discovery_jobs.MODE_ACQUIRE:
            self._run_acquire_dispatch(store, repo, project_id, job, attempt, token)
            return

        counter = AttemptCounter()
        transport = discovery_jobs.guard_transport(
            self._discovery_lowlevel,
            stop_requested=lambda: self._discovery_stop_requested(
                repo, project_id, job_id
            ),
            counter=counter,
        )
        try:
            record = discovery_jobs.dispatch(
                store,
                self._registry,
                token,
                job,
                transport,
                job_held=lambda: self._discovery_stop_requested(
                    repo, project_id, job_id
                ),
            )
        except DiscoveryJobStopped:
            self._discovery_fail(
                repo, project_id, job, attempt, attempts=counter.value, reason="stopped"
            )
            return
        except DiscoveryRequestInterrupted:
            # The opt-in interrupt is normally swallowed by the shared transport
            # (a failed page with no fabricated attempt); if it ever reaches here
            # there is no partial result to commit, so record an honest outcome.
            self._discovery_fail(
                repo,
                project_id,
                job,
                attempt,
                attempts=counter.value,
                reason="interrupted",
            )
            return
        except DiscoveryExecutionBlockedError:
            held = self._discovery_is_held(repo, project_id, job_id)
            self._discovery_fail(
                repo,
                project_id,
                job,
                attempt,
                attempts=counter.value,
                reason="blocked",
                escalate=(counter.value > 0 or not held),
            )
            return
        except DiscoveryExecutionError:
            self._discovery_fail(
                repo, project_id, job, attempt, attempts=counter.value, reason="refused"
            )
            return
        except BaseException:  # noqa: BLE001 - fail closed, never a silent success
            self._discovery_fail(
                repo,
                project_id,
                job,
                attempt,
                attempts=counter.value,
                reason="indeterminate",
            )
            return

        result_kind, payload = discovery_jobs.accepted_result(job, record)
        try:
            with self._write_guard(project_id, allow_draining=True):
                repo.accept_result(
                    attempt_id,
                    result_kind=result_kind,
                    result=payload,
                )
        except (JobError, ReviewAgentError):
            # The underlying observation/report is durably recorded; a restart
            # proves it via replay. Do not fabricate a local receipt here.
            return
        # Label an interrupted partial explicitly: the job was stopped before its
        # next request, so the recorded result is incomplete. The accepted result
        # preserves any held (paused/cancelled) scheduling state.
        if counter.interrupted or self._discovery_is_held(repo, project_id, job_id):
            self._wait_reasons[job_id] = "discovery_interrupted"

    def _run_acquire_dispatch(
        self,
        store: Any,
        repo: JobRepository,
        project_id: str,
        job: Job,
        attempt: Any,
        token: str,
    ) -> None:
        """Run one official full-text retrieval job (E1) with a durable GET journal.

        A may-have-dispatched checkpoint is persisted under the registry write
        guard before every GET and its safe metadata after it. A zero-I/O refusal
        reopens the proven-undispatched attempt (escalating a persistent
        obstruction); anything that may have been sent and has no proven report is
        an unknown outcome (``waiting_human``) and is never re-sent.
        """
        job_id = job.job_id
        run = acquisition_jobs.dispatch_acquire(
            store,
            self._registry,
            token,
            job,
            repo=repo,
            attempt_id=attempt.attempt_id,
            lowlevel=self._acquisition_lowlevel,
            stop_requested=lambda: self._discovery_stop_requested(repo, project_id, job_id),
            guard=lambda allow_draining: self._write_guard(
                project_id, allow_draining=allow_draining
            ),
        )
        if run.summary is not None and not run.journal_failed:
            try:
                with self._write_guard(project_id, allow_draining=True):
                    repo.accept_result(
                        attempt.attempt_id,
                        result_kind=discovery_jobs.RESULT_KIND_ACQUISITION,
                        result=acquisition_jobs.accepted_result(run.summary),
                    )
            except (JobError, ReviewAgentError):
                # The report is durable; a restart proves it by replay.
                return
            if run.summary["status"] == "interrupted" or self._discovery_is_held(
                repo, project_id, job_id
            ):
                self._wait_reasons[job_id] = "discovery_interrupted"
            return

        if run.summary is not None or run.sent:
            # A GET was sent but the journal/report cannot prove a clean outcome.
            self._discovery_fail(
                repo, project_id, job, attempt, attempts=1, reason="indeterminate"
            )
            return

        held = self._discovery_is_held(repo, project_id, job_id)
        if held and run.error_code in ("interrupted", "not_active"):
            self._discovery_fail(
                repo, project_id, job, attempt, attempts=0, reason="stopped", escalate=False
            )
            return
        self._discovery_fail(
            repo,
            project_id,
            job,
            attempt,
            attempts=0,
            reason="blocked" if run.blocked else "refused",
            escalate=True,
        )

    def _run_lead_dispatch(
        self,
        store: Any,
        repo: JobRepository,
        project_id: str,
        job: Job,
        attempt: Any,
        token: str,
    ) -> None:
        """Run one scoped hosted lead job (at most one POST) and record its lifecycle.

        The credential is read from the **one** named environment variable at
        dispatch (never scanned, never persisted). A missing credential, an
        unapproved/stale batch, a denied rights record, or a cap block performs
        **zero** POST. A completed/refused/protocol/oversized/failed outcome is
        persisted as the job's safe accepted result; an ``unknown`` external
        outcome keeps the reserve and the runtime occupancy and moves the job to
        ``waiting_human`` — it is **never** re-POSTed on a retry or restart.
        """
        job_id = job.job_id
        if self._discovery_stop_requested(repo, project_id, job_id):
            self._discovery_fail(
                repo, project_id, job, attempt, attempts=0, reason="stopped", escalate=False
            )
            return

        api_key = self._lead_api_key
        if api_key is None:
            api_key = os.environ.get(discovery_jobs.LEAD_CREDENTIAL_ENV)
        if not isinstance(api_key, str) or api_key == "":
            self._discovery_fail(
                repo,
                project_id,
                job,
                attempt,
                attempts=0,
                reason="credential_missing",
                escalate=True,
            )
            return

        try:
            mapping = discovery_jobs.dispatch_lead(
                store,
                self._registry,
                token,
                job,
                api_key=api_key,
                lead_transport=self._lead_transport,
            )
        except (DiscoveryExecutionError, HostedLeadError, BudgetExceededError):
            # A refusal raised strictly *before* any reserve/POST.
            self._discovery_fail(
                repo,
                project_id,
                job,
                attempt,
                attempts=0,
                reason="blocked",
                escalate=True,
            )
            return
        except BaseException:  # noqa: BLE001 - the POST may have happened: unknown
            self._discovery_fail(
                repo,
                project_id,
                job,
                attempt,
                attempts=1,
                reason="indeterminate",
            )
            return

        if discovery_jobs.lead_outcome_accepted(mapping):
            try:
                with self._write_guard(project_id, allow_draining=True):
                    repo.accept_result(
                        attempt.attempt_id,
                        result_kind=discovery_jobs.RESULT_KIND_LEAD,
                        result=mapping,
                    )
            except (JobError, ReviewAgentError):
                return
            if self._discovery_is_held(repo, project_id, job_id):
                self._wait_reasons[job_id] = "discovery_interrupted"
            return

        # An unknown/submitted external outcome: retain the reserve and the
        # runtime occupancy, escalate to a human, and never re-POST.
        try:
            with self._write_guard(project_id, allow_draining=True):
                repo.mark_ambiguous(attempt.attempt_id)
                repo.escalate_to_human(job_id)
        except (JobError, ReviewAgentError):
            return
        self._wait_reasons[job_id] = "discovery_lead_reconciliation_required"

    def _discovery_fail(
        self,
        repo: JobRepository,
        project_id: str,
        job: Job,
        attempt: Any,
        *,
        attempts: int,
        reason: str,
        escalate: bool | None = None,
    ) -> None:
        """Record an honest, non-silent outcome for a failed discovery dispatch.

        Zero real provider attempts means nothing could have been issued: the
        attempt is reopened (its dispatch marker cleared) so it can run once the
        obstruction is lifted. One or more attempts (or an explicit escalation)
        is an unknown outcome: the attempt is marked ambiguous and the job is
        escalated, never silently retried.
        """
        if escalate is None:
            escalate = True
        try:
            if attempts == 0:
                # Nothing could have been issued: reopen the same identity and
                # clear the dispatch marker. Only an explicit, non-transient
                # refusal escalates (a persistent obstruction must not loop).
                with self._write_guard(project_id, allow_draining=True):
                    repo.resume_proven_undispatched(attempt.attempt_id)
                    if escalate:
                        repo.escalate_to_human(job.job_id)
            else:
                with self._write_guard(project_id, allow_draining=True):
                    repo.mark_ambiguous(attempt.attempt_id)
                    repo.escalate_to_human(job.job_id)
        except (JobError, ReviewAgentError):
            pass
        self._wait_reasons[job.job_id] = "discovery_" + reason

    def _discovery_stop_requested(
        self, repo: JobRepository, project_id: str, job_id: str
    ) -> bool:
        """True when a running discovery job must stop issuing attempts."""
        if self._draining or project_id in self._paused_projects:
            return True
        try:
            job = repo.get(job_id)
        except ReviewAgentError:
            return True
        return job is None or job.state in ("paused", "cancelled")

    def _discovery_is_held(
        self, repo: JobRepository, project_id: str, job_id: str
    ) -> bool:
        if self._draining or project_id in self._paused_projects:
            return True
        try:
            job = repo.get(job_id)
        except ReviewAgentError:
            return False
        return job is None or job.state in ("paused", "cancelled")

    def _runtime_occupied(self, conn: Any, project_id: str) -> bool:
        """True when a durable runtime dispatch occupies the serial remote slot.

        A ``remote`` ledger (an outstanding ``submitted``/``admitted``/``unknown``
        dispatch) or an ``unreadable`` ledger both count — fail closed: new
        synthetic dispatch must not start while the remote outcome is unknown or
        the ledger cannot be proven safe. A ``reserved``/terminal ledger is not
        occupying (recorded before any I/O, or already finished).
        """
        return runtime_dispatch_remote_state(conn, project_id) != "none"

    def _tick_project(self, project_id: str) -> float | None:
        try:
            with self.open_store(project_id) as store:
                repo = JobRepository(store)
                runtime_occupied = self._runtime_occupied(store.connection, project_id)
                jobs = repo.list()
                occupied = runtime_occupied or any(
                    self._job_has_potential_remote(repo, job) for job in jobs
                )
                scheduling = self._scheduling_allowed(project_id)
                # Discovery reconciliation (a standing observation completes the
                # same fence; an unknown outcome escalates) is independent of the
                # synthetic plan and of a held/occupied slot.
                if self._tick_discovery(store, repo, project_id, jobs):
                    return 0.0
                plan = self._plan(
                    repo,
                    project_id,
                    runtime_occupied=runtime_occupied,
                    jobs=jobs,
                    occupied=occupied,
                )
                if plan is None:
                    if self._maybe_start_discovery(
                        repo,
                        project_id,
                        jobs,
                        scheduling=scheduling,
                        occupied=occupied,
                    ):
                        return 0.0
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

    def _plan(
        self,
        repo: JobRepository,
        project_id: str,
        *,
        runtime_occupied: bool = False,
        jobs: list[Job] | None = None,
        occupied: bool | None = None,
    ) -> tuple | None:
        """Pick the next action for the single serial remote executor.

        One project executes at most one dispatch at a time — synthetic **or**
        runtime. If ANY job currently occupies the serial slot (attempt-level
        unresolved, an admitted/running attempt, a ``waiting_external`` gate, or a
        queued attempt that still carries a continuity checkpoint) **or** a
        durable runtime dispatch is outstanding (``submitted``/``admitted``/
        ``unknown``, or an unreadable ledger), then **no** new ``claim``/``admit``
        is returned for any other job: authorized new jobs may be queued but are
        not dispatched until the occupying remote is quiesced or given an explicit
        human disposition.

        Reconciliation (``lookup``/``reconcile``) of an already-admitted or
        interrupted attempt is returned independently of the job's held state
        (paused/cancelled included) **and** independently of a runtime occupancy:
        finishing work the engine had already accepted is always allowed.
        ``waiting_human``/``waiting_external`` are never polled (no busy loop).
        """
        scheduling = self._scheduling_allowed(project_id)
        if jobs is None:
            jobs = repo.list()
        if occupied is None:
            occupied = runtime_occupied or any(
                self._job_has_potential_remote(repo, job) for job in jobs
            )
        for job in jobs:
            if discovery_jobs.is_discovery_task_kind(job.task_kind):
                # Discovery jobs have their own claim/reconcile path; never route
                # them through the synthetic numeric backend.
                continue
            if job.state in ("waiting_human", "waiting_external"):
                # A waiting gate is never polled, but it still occupies the slot
                # (tracked via ``occupied``) and blocks other dispatch.
                continue
            attempt = (
                None
                if job.current_attempt_id is None
                else repo.attempt(job.current_attempt_id)
            )
            if attempt is not None and attempt.state in ("interrupted", "ambiguous"):
                # Attempt-level recovery is independent of the job's held state.
                return ("reconcile", job, attempt)
            if attempt is not None and attempt.state in ACTIVE_ATTEMPT_STATES:
                if attempt.state == "queued":
                    if (
                        job.state == "queued"
                        and scheduling
                        and not occupied
                    ):
                        return ("admit", job, attempt)
                    continue
                if attempt.state in ("admitted", "running"):
                    return ("lookup", job, attempt)
            if job.state == "reconciliation_required" and attempt is not None:
                return ("reconcile", job, attempt)
            if job.state == "queued" and attempt is None and scheduling and not occupied:
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
            #    external work is done outside the registry lock. Every backend
            #    instance shares the engine clock (real default, injected tests).
            backend = SyntheticBackend(
                self._data_dir, expected_continuity_id=continuity, clock=self._clock
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
        except JobError:
            # A guard refusal here is significant: the external admit may already
            # have happened, so an unknown local state must not be swallowed.
            return self._mark_indeterminate(
                repo, project_id, job, attempt, "admission_indeterminate"
            )
        except ReviewAgentError:
            # Backend malformed/lost or guard refused: persist an explicit gate.
            return self._mark_indeterminate(
                repo, project_id, job, attempt, "admission_indeterminate"
            )
        return 0.0

    def _do_lookup(
        self, repo: JobRepository, project_id: str, job: Job, attempt: Any
    ) -> float | None:
        continuity = self._checkpoint_continuity(attempt)
        if continuity is None:
            # No continuity checkpoint ⇒ by ordering the dispatch could not have
            # been issued: reopen the same attempt identity (never mint a new one).
            return self._resume_same_attempt(repo, project_id, job, attempt)
        try:
            backend = SyntheticBackend(
                self._data_dir, expected_continuity_id=continuity, clock=self._clock
            )
            result = backend.lookup(attempt.attempt_id, project_id)
        except ReviewAgentError:
            # A malformed/lost provider journal is uncertain, not absent.
            return self._mark_indeterminate(
                repo, project_id, job, attempt, "backend_indeterminate"
            )
        if result.status == "completed" and result.definitive and result.result is not None:
            return self._accept_result(
                repo,
                project_id,
                job,
                attempt,
                {"kind": "synthetic_numeric", "value": result.result.value},
            )
        if result.status == "absent" and result.definitive:
            if attempt.admission_id is not None:
                # Admission is recorded locally but the provider says absent: a
                # contradiction that must never be resumed as if undispatched.
                return self._mark_indeterminate(
                    repo, project_id, job, attempt, "admission_contradiction"
                )
            # A definitively-absent identity (same persisted generation, no
            # admission) was never issued: reopen the SAME attempt identity.
            return self._resume_same_attempt(repo, project_id, job, attempt)
        if result.status == "failed" and result.definitive_failure:
            try:
                # Trusted reconcile path: a definitive failure may resolve an
                # interrupted/ambiguous attempt (active-only mark would refuse it
                # after a restart). Reconciliation of known work: allow_draining.
                with self._write_guard(project_id, allow_draining=True):
                    repo.reconcile(job.job_id, resolution="definitive_failure")
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
        return self._mark_indeterminate(
            repo, project_id, job, attempt, result.reason or "reconciliation_required"
        )

    def _mark_indeterminate(
        self, repo: JobRepository, project_id: str, job: Job, attempt: Any, reason: str
    ) -> float | None:
        """Persist an explicit reconciliation gate for uncertain remote state.

        Marks the attempt ambiguous (retaining its reserve and setting the
        unresolved bit) and moves the job to ``waiting_human`` so it is visible
        and is never silently retried or polled. No raw source value is surfaced.
        """
        try:
            with self._write_guard(project_id, allow_draining=True):
                repo.mark_ambiguous(attempt.attempt_id)
                repo.escalate_to_human(job.job_id)
        except (JobError, ReviewAgentError):
            return None
        self._wait_reasons[job.job_id] = reason
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
    def _blocked(blockers: dict[str, Any]) -> dict[str, Any]:
        return {
            "ok": False,
            "code": ErrorCode.SWITCH_BLOCKED,
            "message": _MESSAGES[ErrorCode.SWITCH_BLOCKED],
            **blockers,
        }

    @staticmethod
    def _validate_reserve(value: Any) -> str:
        """Validate and canonicalise a reserve with the shared bounded rules."""
        if not isinstance(value, str):
            raise _RequestError(ErrorCode.BAD_TYPE)
        try:
            return validate_reserve_usd(value)
        except JobValidationError as exc:
            raise _RequestError(ErrorCode.BAD_VALUE) from exc
