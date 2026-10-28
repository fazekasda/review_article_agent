"""Detached background-worker process lifecycle launcher.

This module owns only the **process** lifecycle of the durable worker: launch it
detached from the CLI's lifetime, probe it over the owner-only control socket,
and stop it *by authenticated control request* (never by a signal). It is
deliberately independent of the engine implementation that will live behind
``review_article_agent.worker`` (``python -m review_article_agent.worker``):
that engine supplies the entry point and the IPC handler, and this module
spawns, probes, and stops the resulting process.

Contract targeted (``docs/implementation/03-durable-worker.md`` acceptance
criteria "Start a task, close the CLI, and confirm completion/checkpoint from a
new CLI" / "Authentication rejects non-owner control; no root or public
unauthenticated listener is required"; ``docs/contracts/02-gates-and-runtime.md``
§8.4 "Pause, cancel, and detached CLI"):

* The worker is launched as
  ``sys.executable -m review_article_agent.worker --data-dir <absolute>`` with
  **no shell**, in a **new session** (``start_new_session=True``), and with
  ``stdin``/``stdout``/``stderr`` bound to ``os.devnull``. Closing the CLI does
  therefore not cancel the worker, and nothing the worker writes reaches a log
  or a secret-bearing stream.
* The absolute module search path is derived from this module's own location and
  exported to the child as ``PYTHONPATH``, so the spawned worker keeps importing
  *this* package even if the launcher's working directory changes or is removed.
  An installed (wheel/site-packages) layout needs nothing extra and is harmless.
* Readiness is confirmed only from a **verified** control response: the worker
  must answer ``{"version": 1, "op": "status"}`` with a dict whose ``app`` is
  :data:`APP_MARKER`, whose ``protocol_version`` is :data:`PROTOCOL_VERSION`,
  and which carries an integer ``pid`` and a string ``state``. A foreign,
  same-UID endpoint at the control path is never trusted, and its status shape
  is never reported as a running worker.
* Stops are routed as ``{"version": 1, "op": "stop"}``; the worker answers
  *before* exiting. This module never signals a stored or foreign PID, and it
  never reads or writes a PID file. On a launch failure it may terminate only
  its **own** still-live child process object.
* Concurrent starts may race: the server's singleton ``flock`` selects exactly
  one worker, and the losers reconnect to the already-running, same-application
  worker idempotently.
* Only owner-only local control is used: no TCP listener, no root, no public
  surface. Durable state and activation semantics remain the engine's concern;
  ``stop`` only routes control.

The engine contract this module expects is documented on the public functions
below so the engine package can implement it without importing this module.
"""

from __future__ import annotations

import math
import os
import subprocess
import sys
import time
from typing import Any

from ..domain.errors import ReviewAgentError
from ..runtime import ipc

__all__ = [
    "WorkerProcessError",
    "WorkerStartError",
    "APP_MARKER",
    "PROTOCOL_VERSION",
    "REQUEST_VERSION",
    "STATUS_OP",
    "STOP_OP",
    "start_worker",
    "worker_status",
    "stop_worker",
]

#: Marker every status response must carry to be trusted as *our* worker.
APP_MARKER = "review_article_agent.worker"

#: Wire protocol version for both requests and status responses.
PROTOCOL_VERSION = 1

#: Version field value carried in every control request.
REQUEST_VERSION = 1

#: Operation name for a status probe.
STATUS_OP = "status"

#: Operation name for an owner stop (answered before the process exits).
STOP_OP = "stop"

# Status-document keys (the shape the engine returns for ``op=status``).
_APP_KEY = "app"
_PROTOCOL_KEY = "protocol_version"
_PID_KEY = "pid"
_STATE_KEY = "state"

# Readiness polling knobs.
_INITIAL_BACKOFF = 0.02
_MAX_BACKOFF = 0.25

#: How long to keep waiting for *another* concurrent starter's worker to become
#: ready after our own child has exited (the loser of the singleton race).
_EARLY_EXIT_GRACE = 2.0

#: Upper bound for a caller-supplied timeout, matching the IPC transport's cap.
_MAX_TIMEOUT_SECONDS = 3600.0

#: Default single-request I/O timeout used when probing status.
_STATUS_IO_TIMEOUT = ipc.DEFAULT_IO_TIMEOUT


# --------------------------------------------------------------------------- #
# errors
# --------------------------------------------------------------------------- #
class WorkerProcessError(ReviewAgentError):
    """Base class for detached-worker process lifecycle failures."""


class WorkerStartError(WorkerProcessError):
    """The worker did not become ready within the bound.

    Carries the launched child's ``child_pid`` and, when the child exited on its
    own, its ``returncode``. It never carries program output, environment
    contents, or any other potentially sensitive detail.
    """

    def __init__(
        self,
        message: str,
        *,
        child_pid: int | None = None,
        returncode: int | None = None,
    ) -> None:
        super().__init__(message)
        self.child_pid = child_pid
        self.returncode = returncode


# --------------------------------------------------------------------------- #
# argument validation
# --------------------------------------------------------------------------- #
def _validate_timeout(value: object, *, what: str = "timeout") -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise WorkerProcessError(f"{what} must be a number of seconds")
    number = float(value)
    if not math.isfinite(number) or number <= 0 or number > _MAX_TIMEOUT_SECONDS:
        raise WorkerProcessError(
            f"{what} must be a finite value in (0, {_MAX_TIMEOUT_SECONDS}]"
        )
    return number


def _normalize_data_dir(data_dir: object) -> str:
    try:
        path = os.fspath(data_dir)  # type: ignore[arg-type]
    except TypeError as exc:
        raise WorkerProcessError("data_dir must be a filesystem path") from exc
    if isinstance(path, bytes):
        path = os.fsdecode(path)
    if not path:
        raise WorkerProcessError("data_dir must not be empty")
    return os.path.abspath(path)


# --------------------------------------------------------------------------- #
# command + environment (private seams; substituted by tests)
# --------------------------------------------------------------------------- #
def _module_search_path() -> str:
    """Return the absolute directory that holds the package to spawn.

    Derived from this module's own location so a spawned worker imports *this*
    checkout's ``review_article_agent`` package even if the launcher's working
    directory changes or is removed. Under an installed layout this is the
    (already-importable) install directory, so exporting it is harmless.
    """
    here = os.path.abspath(__file__)
    # .../review_article_agent/worker/process.py -> .../<search path>
    return os.path.dirname(os.path.dirname(os.path.dirname(here)))


def _build_command(data_dir: str) -> list[str]:
    """Return the argv for the detached worker.

    A private seam: tests substitute this to run a disposable fixture worker
    without an engine implementation. The default targets the engine contract
    ``python -m review_article_agent.worker --data-dir <absolute>``.
    """
    return [sys.executable, "-m", APP_MARKER, "--data-dir", data_dir]


def _build_env() -> dict[str, str]:
    """Return the child environment.

    Inherits the parent environment (the interpreter needs ``PATH`` etc.) and
    prepends the derived absolute module search path to ``PYTHONPATH`` so imports
    are independent of the child's working directory.
    """
    env = dict(os.environ)
    search_path = _module_search_path()
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = search_path + (os.pathsep + existing if existing else "")
    return env


def _spawn(command: list[str], env: dict[str, str]) -> subprocess.Popen:
    """Launch the worker detached from this process."""
    try:
        return subprocess.Popen(  # noqa: S603 - argv list, shell=False
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            start_new_session=True,
            env=env,
            shell=False,
        )
    except OSError as exc:
        raise WorkerStartError(
            f"could not launch the worker process "
            f"({exc.__class__.__name__})"
        ) from exc


def _terminate_own_child(child: subprocess.Popen) -> None:
    """Terminate only our own still-live child, best effort.

    Never touches a stored PID or any process we did not start here.
    """
    if child.poll() is not None:
        return
    try:
        child.terminate()
    except OSError:
        return
    try:
        child.wait(timeout=2.0)
    except subprocess.TimeoutExpired:
        try:
            child.kill()
        except OSError:
            return
        try:
            child.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            pass


# --------------------------------------------------------------------------- #
# verified status
# --------------------------------------------------------------------------- #
def _is_our_status(result: Any) -> bool:
    """Return whether ``result`` is a trustworthy status from our worker.

    Requires the exact application marker, the expected protocol version, a
    positive integer PID, and a non-empty string state. Anything else — a
    foreign endpoint's shape, a missing field, a non-integer PID — is refused.
    """
    if not isinstance(result, dict):
        return False
    if result.get(_APP_KEY) != APP_MARKER:
        return False
    if result.get(_PROTOCOL_KEY) != PROTOCOL_VERSION:
        return False
    pid = result.get(_PID_KEY)
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return False
    state = result.get(_STATE_KEY)
    if not isinstance(state, str) or not state:
        return False
    return True


def _running_status(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "running": True,
        "verified": True,
        "app": result[_APP_KEY],
        "protocol_version": result[_PROTOCOL_KEY],
        "pid": result[_PID_KEY],
        "state": result[_STATE_KEY],
    }


def _not_running_status(reason: str) -> dict[str, Any]:
    return {
        "running": False,
        "verified": False,
        "app": None,
        "protocol_version": None,
        "pid": None,
        "state": None,
        "reason": reason,
    }


def _probe_status(data_dir: str, timeout: float) -> dict[str, Any]:
    """Probe the control socket once and return a normalized status mapping."""
    client = ipc.WorkerIPCClient(data_dir, io_timeout=timeout)
    try:
        result = client.request(
            {"version": REQUEST_VERSION, "op": STATUS_OP}, timeout=timeout
        )
    except ipc.WorkerNotRunning:
        return _not_running_status("not_running")
    except ipc.PeerNotAuthorized:
        return _not_running_status("foreign_endpoint")
    except ipc.IPCRequestError:
        return _not_running_status("foreign_endpoint")
    except ipc.IPCError:
        return _not_running_status("unavailable")
    if not _is_our_status(result):
        return _not_running_status("foreign_endpoint")
    return _running_status(result)


# --------------------------------------------------------------------------- #
# public API
# --------------------------------------------------------------------------- #
def worker_status(
    data_dir: str | os.PathLike[str], *, timeout: float | None = None
) -> dict[str, Any]:
    """Return the verified status of the worker for ``data_dir``.

    The returned mapping always has ``running`` and ``verified`` keys. A running
    worker also yields ``app``, ``protocol_version``, ``pid``, and ``state``; a
    non-running one yields ``None`` for those and a curated ``reason`` (e.g.
    ``"not_running"``, ``"foreign_endpoint"``, ``"unavailable"``).

    ``running`` is ``True`` only for a response carrying :data:`APP_MARKER`,
    :data:`PROTOCOL_VERSION`, an integer ``pid``, and a string ``state``; a
    foreign same-UID endpoint's status is never reported as a running worker.
    """
    normalized = _normalize_data_dir(data_dir)
    effective = _STATUS_IO_TIMEOUT if timeout is None else _validate_timeout(timeout)
    return _probe_status(normalized, effective)


def start_worker(
    data_dir: str | os.PathLike[str], *, timeout: float = 10.0
) -> dict[str, Any]:
    """Ensure a detached worker is running for ``data_dir``.

    If a verified worker already answers, this is idempotent: no process is
    spawned and ``started`` is ``False``. Otherwise the worker is launched
    detached (new session, ``DEVNULL`` stdio, no shell, no PID file) and this
    call waits, with bounded backoff, up to ``timeout`` seconds for it to become
    ready.

    Returns the verified status mapping (as :func:`worker_status`) plus a
    ``started`` key indicating whether this call launched the process.

    Raises :class:`WorkerStartError` when no verified worker becomes ready
    within the bound. If our own child exited on its own, the error carries its
    ``returncode`` and ``child_pid``. A child we started and that is still alive
    is terminated before raising; no other process is ever signalled.
    """
    normalized = _normalize_data_dir(data_dir)
    effective = _validate_timeout(timeout)

    existing = _probe_status(normalized, _STATUS_IO_TIMEOUT)
    if existing["running"]:
        return {**existing, "started": False}

    child = _spawn(_build_command(normalized), _build_env())
    deadline = time.monotonic() + effective
    exit_grace_deadline: float | None = None
    delay = _INITIAL_BACKOFF
    while True:
        status = _probe_status(normalized, _STATUS_IO_TIMEOUT)
        if status["running"]:
            return {**status, "started": True}
        now = time.monotonic()
        if now >= deadline:
            break
        if exit_grace_deadline is None and child.poll() is not None:
            # Our child exited: it may be the loser of a concurrent-start race,
            # so keep waiting briefly for another starter's worker to be ready.
            exit_grace_deadline = min(deadline, now + _EARLY_EXIT_GRACE)
        if exit_grace_deadline is not None and now >= exit_grace_deadline:
            break
        time.sleep(min(delay, max(0.0, deadline - now)))
        delay = min(delay * 2, _MAX_BACKOFF)

    returncode = child.poll()
    _terminate_own_child(child)
    raise WorkerStartError(
        "the worker did not become ready within the timeout",
        child_pid=child.pid,
        returncode=returncode,
    )


def stop_worker(
    data_dir: str | os.PathLike[str], *, timeout: float = 10.0
) -> dict[str, Any]:
    """Stop the worker for ``data_dir`` by authenticated control request.

    Sends ``{"version": 1, "op": "stop"}`` to a **verified** worker and waits,
    with bounded backoff, up to ``timeout`` seconds for it to stop answering.
    The worker answers before exiting; the control request is the only stop
    mechanism used. This function never sends a signal and never reads a PID
    file, so it cannot stop a foreign service or the wrong process.

    Returns a mapping with ``stopped`` (the worker stopped answering), and
    ``was_running``, ``pid``, ``state``, and a curated ``reason``. If no
    verified worker is running, nothing is sent and ``stopped`` is ``False``.
    """
    normalized = _normalize_data_dir(data_dir)
    effective = _validate_timeout(timeout)

    status = _probe_status(normalized, _STATUS_IO_TIMEOUT)
    if not status["running"]:
        return {
            "stopped": False,
            "was_running": False,
            "pid": None,
            "state": None,
            "reason": status.get("reason", "not_running"),
        }

    pid = status["pid"]
    state = status["state"]

    client = ipc.WorkerIPCClient(normalized, io_timeout=effective)
    try:
        response = client.request(
            {"version": REQUEST_VERSION, "op": STOP_OP}, timeout=effective
        )
    except ipc.IPCError:
        return {
            "stopped": False,
            "was_running": True,
            "pid": pid,
            "state": state,
            "reason": "stop_unavailable",
        }
    # A response that declares an identity must be ours; a foreign endpoint is
    # never credited with a stop.
    if isinstance(response, dict) and (
        _APP_KEY in response or _PROTOCOL_KEY in response
    ):
        if (
            response.get(_APP_KEY) != APP_MARKER
            or response.get(_PROTOCOL_KEY) != PROTOCOL_VERSION
        ):
            return {
                "stopped": False,
                "was_running": True,
                "pid": pid,
                "state": state,
                "reason": "foreign_endpoint",
            }

    deadline = time.monotonic() + effective
    delay = _INITIAL_BACKOFF
    while True:
        if not _probe_status(normalized, _STATUS_IO_TIMEOUT)["running"]:
            return {
                "stopped": True,
                "was_running": True,
                "pid": pid,
                "state": state,
                "reason": None,
            }
        now = time.monotonic()
        if now >= deadline:
            break
        time.sleep(min(delay, max(0.0, deadline - now)))
        delay = min(delay * 2, _MAX_BACKOFF)
    return {
        "stopped": False,
        "was_running": True,
        "pid": pid,
        "state": state,
        "reason": "stop_timeout",
    }
