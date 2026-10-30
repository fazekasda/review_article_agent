"""Acceptance tests for the detached worker process launcher (T5a).

Covers ``docs/implementation/03-durable-worker.md`` acceptance criterion
"Start a task, close the CLI, and confirm completion/checkpoint from a new CLI"
(detached lifetime) and the exclusions (no system services / lingering / reboot
startup), plus ``docs/contracts/02-gates-and-runtime.md`` §8.4 ("Close CLI
(detached)": CLI lifetime ends while an authorized background job continues) and
§8.4 / step 03 ("Authentication rejects non-owner control; no root or public
unauthenticated listener is required").

The engine implementation follows in a later task; these tests run against an
*owned, disposable* Python fixture worker that imports only
``review_article_agent.runtime.ipc`` and speaks the versioned control protocol.
No engine, CLI, jobs, schema, or documentation module is imported.
"""

from __future__ import annotations

import concurrent.futures
import gc
import json
import os
import stat
import subprocess
import sys
import tempfile
import time
import unittest
import warnings
from pathlib import Path
from unittest import mock
from _tiers import slow

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from review_article_agent.runtime import ipc  # noqa: E402
from review_article_agent.worker import process  # noqa: E402

# --------------------------------------------------------------------------- #
# disposable fixture programs (written to a temp dir; never added to the package)
# --------------------------------------------------------------------------- #
FIXTURE_WORKER = r'''
import os
import sys
import threading

from review_article_agent.runtime import ipc

data_dir = sys.argv[1]
_stop = threading.Event()


def _handler(request, peer):
    if request.get("version") != 1:
        raise ValueError("unsupported protocol version")
    op = request.get("op")
    if op == "status":
        return {
            "app": "review_article_agent.worker",
            "protocol_version": 1,
            "pid": os.getpid(),
            "state": "running",
        }
    if op == "stop":
        _stop.set()
        return {
            "app": "review_article_agent.worker",
            "protocol_version": 1,
            "stopping": True,
        }
    raise ValueError("unknown operation")


server = ipc.WorkerIPCServer(data_dir, _handler, io_timeout=2.0)
server.start()
try:
    while not _stop.is_set():
        server.serve_once(0.1)
finally:
    server.close()
'''

# Binds the control socket itself and answers with a foreign/malformed status; it
# records (via a marker file) whether a stop request arrived, and never exits on
# its own so the test can prove it was neither signalled nor stopped.
FOREIGN_SERVER = r'''
import json
import os
import socket
import sys
import threading
import time

data_dir = sys.argv[1]
marker = sys.argv[2]
mode = sys.argv[3] if len(sys.argv) > 3 else "foreign"

runtime = os.path.join(data_dir, "runtime")
os.makedirs(runtime, mode=0o700, exist_ok=True)
os.chmod(runtime, 0o700)
sock_path = os.path.join(runtime, "worker.sock")
if os.path.lexists(sock_path):
    os.unlink(sock_path)

srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
srv.bind(sock_path)
os.chmod(sock_path, 0o600)
srv.listen(8)


def _status_body():
    if mode == "malformed":
        return b"not json"
    app = "someone.else" if mode == "foreign" else "review_article_agent.worker"
    proto = 2 if mode == "wrongproto" else 1
    body = {"app": app, "protocol_version": proto, "pid": os.getpid(),
            "state": "running"}
    if mode == "nopid":
        body.pop("pid")
    if mode == "pidmismatch":
        # Correct marker/protocol but an in-band PID that is not the kernel peer.
        body["pid"] = os.getpid() + 424242
    return json.dumps({"ok": True, "result": body}).encode()


def _serve():
    while True:
        try:
            conn, _ = srv.accept()
        except OSError:
            return
        try:
            buf = b""
            while b"\n" not in buf:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                buf += chunk
            line = buf.split(b"\n", 1)[0] if buf else b""
            try:
                req = json.loads(line.decode("utf-8"))
            except Exception:
                req = {}
            if isinstance(req, dict) and req.get("op") == "stop":
                with open(marker, "w", encoding="utf-8") as fh:
                    fh.write("stop")
                conn.sendall(b'{"ok":true,"result":{"stopping":true}}\n')
            else:
                conn.sendall(_status_body() + b"\n")
        except OSError:
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass


threading.Thread(target=_serve, daemon=True).start()
while True:
    time.sleep(0.2)
'''

# A worker whose handler delays every response, to prove the launcher honours a
# caller's small timeout end to end instead of a fixed five-second probe.
SLOW_WORKER = r'''
import os
import sys
import time

from review_article_agent.runtime import ipc

data_dir = sys.argv[1]
delay = float(sys.argv[2])


def _handler(request, peer):
    time.sleep(delay)
    op = request.get("op")
    if op == "status":
        return {"app": "review_article_agent.worker", "protocol_version": 1,
                "pid": os.getpid(), "state": "running"}
    if op == "stop":
        return {"app": "review_article_agent.worker", "protocol_version": 1,
                "stopping": True}
    raise ValueError("op")


server = ipc.WorkerIPCServer(data_dir, _handler, io_timeout=30.0)
server.start()
try:
    while True:
        server.serve_once(0.1)
finally:
    server.close()
'''

# A worker that acknowledges a stop in ways that must not be credited: a
# malformed marker, or by going unresponsive while keeping the socket open.
SUBTLE_WORKER = r'''
import os
import sys
import threading
import time

from review_article_agent.runtime import ipc

data_dir = sys.argv[1]
mode = sys.argv[2]
marker = sys.argv[3]
_hang = threading.Event()


def _handler(request, peer):
    op = request.get("op")
    if op == "status":
        if _hang.is_set():
            time.sleep(30.0)
        return {"app": "review_article_agent.worker", "protocol_version": 1,
                "pid": os.getpid(), "state": "running"}
    if op == "stop":
        with open(marker, "w", encoding="utf-8") as fh:
            fh.write("stop")
        if mode == "hangafterstop":
            _hang.set()
        if mode == "badstopmarker":
            return {"stopping": True}
        return {"app": "review_article_agent.worker", "protocol_version": 1,
                "stopping": True}
    raise ValueError("op")


server = ipc.WorkerIPCServer(data_dir, _handler, io_timeout=30.0)
server.start()
try:
    while True:
        server.serve_once(0.1)
finally:
    server.close()
'''

# A worker that announces itself, then waits for an external gate file before it
# ever attempts to claim the singleton lock. It lets a test deterministically
# hold a *spawned* child in the pre-bind window so it can prove the launcher does
# not leave a losing child alive to become a second singleton later.
GATED_WORKER = r'''
import os
import sys
import threading
import time

from review_article_agent.runtime import ipc

data_dir = sys.argv[1]
pidfile = sys.argv[2]
gate = sys.argv[3]

# Announce this child exists *before* it ever tries to bind the socket.
with open(pidfile, "w", encoding="utf-8") as fh:
    fh.write(str(os.getpid()))

# Hold off claiming the singleton until the test opens the gate (bounded so a
# bug cannot leak this process past the test's lifetime).
deadline = time.monotonic() + 30.0
while not os.path.exists(gate) and time.monotonic() < deadline:
    time.sleep(0.01)

_stop = threading.Event()


def _handler(request, peer):
    if request.get("version") != 1:
        raise ValueError("unsupported protocol version")
    op = request.get("op")
    if op == "status":
        return {"app": "review_article_agent.worker", "protocol_version": 1,
                "pid": os.getpid(), "state": "running"}
    if op == "stop":
        _stop.set()
        return {"app": "review_article_agent.worker", "protocol_version": 1,
                "stopping": True}
    raise ValueError("unknown operation")


server = ipc.WorkerIPCServer(data_dir, _handler, io_timeout=2.0)
server.start()
try:
    while not _stop.is_set():
        server.serve_once(0.1)
finally:
    server.close()
'''

# A launcher that runs this checkout's launcher in a separate process and exits;
# used to prove the daemon outlives the process that started it.
LAUNCHER = r'''
import json
import sys

sys.path.insert(0, {src!r})

import review_article_agent.worker.process as process

process._build_command = lambda data_dir: [sys.executable, {fixture!r}, data_dir]
result = process.start_worker(sys.argv[1], timeout=int(sys.argv[2]))
print(json.dumps(result))
'''


@slow
class WorkerProcessTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="raa-wp-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.data_dir = str(self.tmp / "state")
        self.foreign_marker = str(self.tmp / "stop-marker")
        self.worker_script = self._write("fixture_worker.py", FIXTURE_WORKER)
        self.foreign_script = self._write("foreign_server.py", FOREIGN_SERVER)
        self.slow_script = self._write("slow_worker.py", SLOW_WORKER)
        self.subtle_script = self._write("subtle_worker.py", SUBTLE_WORKER)
        self.gated_script = self._write("gated_worker.py", GATED_WORKER)
        self.launcher_script = self._write(
            "launcher.py", LAUNCHER.format(src=str(SRC), fixture=self.worker_script)
        )
        self._children: list[subprocess.Popen] = []
        # Stop any worker this test started before the temp dir is removed.
        self.addCleanup(lambda: process.stop_worker(self.data_dir, timeout=5))

    def tearDown(self) -> None:
        for child in self._children:
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=3)
                except subprocess.TimeoutExpired:  # pragma: no cover - defensive
                    child.kill()
                    child.wait(timeout=3)

    # -- helpers ------------------------------------------------------------- #
    def _write(self, name: str, body: str) -> str:
        path = self.tmp / name
        path.write_text(body, encoding="utf-8")
        return str(path)

    def _worker_command(self):
        script, python = self.worker_script, sys.executable
        return lambda data_dir: [python, script, data_dir]

    def _start_foreign(self, mode: str = "foreign") -> subprocess.Popen:
        child = subprocess.Popen(
            [sys.executable, self.foreign_script, self.data_dir,
             self.foreign_marker, mode],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self._children.append(child)
        self._wait_for_socket()
        return child

    def _start_script(self, script: str, *args: str) -> subprocess.Popen:
        """Start a fixture worker directly (holding its Popen) and await its socket."""
        child = subprocess.Popen(
            [sys.executable, script, *args],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self._children.append(child)
        self._wait_for_socket()
        return child

    def _wait_for_socket(self, timeout: float = 10.0) -> None:
        sock_path = os.path.join(ipc.runtime_dir_for(self.data_dir), "worker.sock")
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if os.path.exists(sock_path):
                return
            time.sleep(0.02)
        self.fail("foreign server socket did not appear")

    # -- happy path ---------------------------------------------------------- #
    def test_start_status_stop_round_trip(self) -> None:
        with mock.patch.object(process, "_build_command", self._worker_command()):
            started = process.start_worker(self.data_dir, timeout=10.0)
        self.assertTrue(started["running"])
        self.assertTrue(started["started"])
        self.assertTrue(started["verified"])
        self.assertEqual(started["app"], process.APP_MARKER)
        self.assertEqual(started["protocol_version"], process.PROTOCOL_VERSION)
        self.assertGreater(started["pid"], 0)
        self.assertIsInstance(started["state"], str)

        status = process.worker_status(self.data_dir)
        self.assertTrue(status["running"])
        self.assertEqual(status["pid"], started["pid"])

        runtime = ipc.runtime_dir_for(self.data_dir)
        self.assertEqual(stat.S_IMODE(os.stat(runtime).st_mode), 0o700)
        sock = os.path.join(runtime, "worker.sock")
        self.assertEqual(stat.S_IMODE(os.stat(sock).st_mode), 0o600)

        stopped = process.stop_worker(self.data_dir, timeout=10.0)
        self.assertTrue(stopped["stopped"])
        self.assertTrue(stopped["was_running"])
        self.assertFalse(process.worker_status(self.data_dir)["running"])

    def test_start_is_idempotent(self) -> None:
        with mock.patch.object(process, "_build_command", self._worker_command()):
            first = process.start_worker(self.data_dir, timeout=10.0)
            second = process.start_worker(self.data_dir, timeout=10.0)
        self.assertTrue(first["started"])
        self.assertFalse(second["started"])
        self.assertEqual(first["pid"], second["pid"])
        self.assertTrue(second["running"])

    def test_concurrent_starts_race_to_single_worker(self) -> None:
        def call(_: int) -> dict:
            return process.start_worker(self.data_dir, timeout=15.0)

        with mock.patch.object(process, "_build_command", self._worker_command()):
            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
                results = list(pool.map(call, range(4)))
        pids = {r["pid"] for r in results}
        self.assertEqual(len(pids), 1)
        self.assertTrue(all(r["running"] for r in results))
        # The single surviving worker is still ours and stoppable.
        self.assertTrue(process.worker_status(self.data_dir)["running"])
        self.assertTrue(process.stop_worker(self.data_dir, timeout=10.0)["stopped"])

    def test_losing_concurrent_child_is_not_left_to_become_worker(self) -> None:
        """A losing starter must not leave its own spawned child alive.

        Regression for the flake in ``test_concurrent_starts_race_to_single_worker``:
        ``start_worker`` returned as soon as *any* verified worker answered,
        leaving its own spawned-but-not-yet-bound child alive. That child later
        acquired the freed singleton lock and bound a second worker as soon as
        the winner was stopped, so the subsequent ``stop_worker`` never observed
        the socket gone (``stop_unconfirmed``).

        This holds one child deterministically in its pre-bind window (gated on
        a file the test never creates) while a second, well-behaved starter wins
        the singleton race. The losing child must be gone by the time its
        ``start_worker`` call returns.
        """
        pidfile = self.tmp / "gated.pid"
        gate = self.tmp / "gate"

        def gated_command(data_dir: str) -> list[str]:
            return [
                sys.executable, self.gated_script, data_dir,
                str(pidfile), str(gate),
            ]

        normal = self._worker_command()
        current = {"command": gated_command}

        def dispatch(data_dir: str) -> list[str]:
            return current["command"](data_dir)

        gated_pid: int | None = None

        def cleanup() -> None:
            # Release any lingering gated child, then make sure nothing runs.
            try:
                gate.write_text("go", encoding="utf-8")
            except OSError:  # pragma: no cover - defensive
                pass
            if gated_pid is not None:
                try:
                    os.kill(gated_pid, 15)
                except ProcessLookupError:
                    pass
            process.stop_worker(self.data_dir, timeout=5.0)

        self.addCleanup(cleanup)

        with mock.patch.object(process, "_build_command", dispatch):
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                losing = pool.submit(
                    process.start_worker, self.data_dir, timeout=15.0
                )
                # Wait until the gated child exists but has not bound the socket.
                deadline = time.monotonic() + 10.0
                while not pidfile.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(pidfile.exists(), "gated child never started")
                gated_pid = int(pidfile.read_text(encoding="utf-8"))
                # A different, well-behaved worker now wins the singleton race.
                current["command"] = normal
                winning = pool.submit(
                    process.start_worker, self.data_dir, timeout=15.0
                )
                loser_result = losing.result(timeout=30.0)
                winner_result = winning.result(timeout=30.0)

        self.assertTrue(winner_result["running"])
        self.assertEqual(loser_result["pid"], winner_result["pid"])
        # The losing child must be gone, so it can never become a second worker.
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            try:
                os.kill(gated_pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.02)
        else:
            self.fail("losing child was left alive by start_worker")
        # The single surviving worker is still ours and stoppable.
        self.assertTrue(process.stop_worker(self.data_dir, timeout=10.0)["stopped"])

    # -- absent / malformed / foreign ---------------------------------------- #
    def test_status_when_absent(self) -> None:
        status = process.worker_status(self.data_dir)
        self.assertFalse(status["running"])
        self.assertFalse(status["verified"])
        self.assertEqual(status["reason"], "not_running")
        self.assertIsNone(status["pid"])
        stopped = process.stop_worker(self.data_dir, timeout=1.0)
        self.assertFalse(stopped["stopped"])
        self.assertFalse(stopped["was_running"])

    def test_is_our_status_shape(self) -> None:
        good = {
            "app": process.APP_MARKER,
            "protocol_version": process.PROTOCOL_VERSION,
            "pid": 4321,
            "state": "running",
        }
        self.assertTrue(process._is_our_status(good))
        bad = [
            {},
            {"app": "someone.else", "protocol_version": 1, "pid": 5, "state": "s"},
            {"app": process.APP_MARKER, "protocol_version": 2, "pid": 5, "state": "s"},
            {"app": process.APP_MARKER, "protocol_version": 1, "pid": True, "state": "s"},
            {"app": process.APP_MARKER, "protocol_version": 1, "pid": 0, "state": "s"},
            {"app": process.APP_MARKER, "protocol_version": 1, "pid": -1, "state": "s"},
            {"app": process.APP_MARKER, "protocol_version": 1, "pid": 5, "state": ""},
            {"app": process.APP_MARKER, "protocol_version": 1, "pid": 5},
            {"app": process.APP_MARKER, "protocol_version": 1, "state": "s"},
            [],
            "text",
            None,
        ]
        for shape in bad:
            with self.subTest(shape=shape):
                self.assertFalse(process._is_our_status(shape))

    def test_foreign_foreign_shaped_status_not_trusted(self) -> None:
        for mode in ("foreign", "wrongproto", "nopid", "malformed"):
            with self.subTest(mode=mode):
                child = self._start_foreign(mode)
                try:
                    status = process.worker_status(self.data_dir)
                    self.assertFalse(status["running"], msg=mode)
                    self.assertFalse(status["verified"], msg=mode)
                    # No stop request is ever sent to an unverified endpoint.
                    stopped = process.stop_worker(self.data_dir, timeout=1.0)
                    self.assertFalse(stopped["stopped"])
                    self.assertFalse(stopped["was_running"])
                    self.assertIsNone(child.poll())
                    self.assertFalse(os.path.exists(self.foreign_marker))
                finally:
                    child.terminate()
                    child.wait(timeout=3)
                    if os.path.exists(self.foreign_marker):
                        os.unlink(self.foreign_marker)
                    sock_path = os.path.join(
                        ipc.runtime_dir_for(self.data_dir), "worker.sock"
                    )
                    if os.path.lexists(sock_path):
                        os.unlink(sock_path)

    def test_start_refuses_live_foreign_socket(self) -> None:
        child = self._start_foreign("foreign")
        try:
            with mock.patch.object(process, "_build_command", self._worker_command()):
                with self.assertRaises(process.WorkerStartError):
                    process.start_worker(self.data_dir, timeout=1.0)
            # The foreign service is untouched: still alive, never stopped.
            self.assertIsNone(child.poll())
            self.assertFalse(os.path.exists(self.foreign_marker))
        finally:
            child.terminate()
            child.wait(timeout=3)

    # -- failure and timeout ------------------------------------------------- #
    def test_startup_failure_is_curated(self) -> None:
        failing = self._write("failing_worker.py", "import sys\nsys.exit(3)\n")
        with mock.patch.object(
            process, "_build_command", lambda d: [sys.executable, failing]
        ):
            with self.assertRaises(process.WorkerStartError) as cm:
                process.start_worker(self.data_dir, timeout=1.0)
        self.assertEqual(cm.exception.returncode, 3)
        self.assertGreater(cm.exception.child_pid, 0)
        # The message reflects no program output or secret.
        self.assertNotIn("Traceback", str(cm.exception))
        self.assertNotIn(str(self.tmp), str(cm.exception))
        # A child that already exited is not left running.
        with self.assertRaises(ProcessLookupError):
            os.kill(cm.exception.child_pid, 0)

    def test_start_timeout_terminates_own_child(self) -> None:
        sleeper = self._write("sleeper.py", "import time\ntime.sleep(60)\n")
        with mock.patch.object(
            process, "_build_command", lambda d: [sys.executable, sleeper]
        ):
            with self.assertRaises(process.WorkerStartError) as cm:
                process.start_worker(self.data_dir, timeout=0.5)
        pid = cm.exception.child_pid
        self.assertGreater(pid, 0)
        # The hung child was alive when the deadline hit...
        self.assertIsNone(cm.exception.returncode)
        # ...but only our own child was terminated (never a stored/foreign PID).
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.05)
        else:
            self.fail("own child was not terminated")

    # -- detached lifetime + cwd independence -------------------------------- #
    def test_launcher_exits_while_daemon_survives_any_cwd(self) -> None:
        env = dict(os.environ)
        env.pop("PYTHONPATH", None)  # prove the child's import does not rely on it
        other_cwd = self.tmp / "unrelated-cwd"
        other_cwd.mkdir()
        completed = subprocess.run(
            [sys.executable, self.launcher_script, self.data_dir, "10"],
            cwd=str(other_cwd),
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = json.loads(completed.stdout)
        self.assertTrue(result["running"])
        self.assertEqual(result["app"], process.APP_MARKER)
        # The launcher process has fully exited; the daemon must still answer.
        daemon_pid = result["pid"]
        os.kill(daemon_pid, 0)  # raises if the daemon did not survive
        status = process.worker_status(self.data_dir)
        self.assertTrue(status["running"])
        self.assertEqual(status["pid"], daemon_pid)
        self.assertTrue(process.stop_worker(self.data_dir, timeout=10.0)["stopped"])

    # -- R1: bounded timeout, stop confirmation, peer binding ---------------- #
    def test_status_honors_small_timeout_with_slow_endpoint(self) -> None:
        self._start_script(self.slow_script, self.data_dir, "2.0")
        started = time.monotonic()
        status = process.worker_status(self.data_dir, timeout=0.3)
        elapsed = time.monotonic() - started
        self.assertFalse(status["running"])
        self.assertEqual(status["reason"], "unavailable")
        self.assertLess(elapsed, 1.5)  # not a fixed five-second probe

    def test_start_worker_honors_small_timeout_with_slow_endpoint(self) -> None:
        with mock.patch.object(
            process,
            "_build_command",
            lambda d: [sys.executable, self.slow_script, d, "2.0"],
        ):
            started = time.monotonic()
            with self.assertRaises(process.WorkerStartError) as cm:
                process.start_worker(self.data_dir, timeout=0.5)
            elapsed = time.monotonic() - started
        self.assertLess(elapsed, 2.5)
        pid = cm.exception.child_pid
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.05)
        else:
            self.fail("slow child was not terminated")

    def test_stop_not_credited_when_endpoint_becomes_unavailable(self) -> None:
        child = self._start_script(
            self.subtle_script, self.data_dir, "hangafterstop", self.foreign_marker
        )
        result = process.stop_worker(self.data_dir, timeout=1.0)
        self.assertFalse(result["stopped"])
        self.assertTrue(result["unresolved"])
        self.assertIn(result["reason"], ("stop_unconfirmed", "stop_unavailable"))
        # The stop *was* delivered (our worker acknowledged) ...
        self.assertTrue(os.path.exists(self.foreign_marker))
        # ... but the endpoint never confirmed gone, and was never signalled.
        self.assertIsNone(child.poll())

    def test_stop_malformed_marker_not_credited(self) -> None:
        child = self._start_script(
            self.subtle_script, self.data_dir, "badstopmarker", self.foreign_marker
        )
        result = process.stop_worker(self.data_dir, timeout=1.0)
        self.assertFalse(result["stopped"])
        self.assertTrue(result["unresolved"])
        self.assertEqual(result["reason"], "foreign_endpoint")
        self.assertTrue(os.path.exists(self.foreign_marker))
        self.assertIsNone(child.poll())

    def test_status_pid_must_match_kernel_peer(self) -> None:
        child = self._start_foreign("pidmismatch")
        try:
            status = process.worker_status(self.data_dir)
            self.assertFalse(status["running"])
            self.assertEqual(status["reason"], "foreign_endpoint")
            # A mismatched in-band PID means no control is ever delivered.
            result = process.stop_worker(self.data_dir, timeout=1.0)
            self.assertFalse(result["stopped"])
            self.assertFalse(result["was_running"])
            self.assertIsNone(child.poll())
            self.assertFalse(os.path.exists(self.foreign_marker))
        finally:
            child.terminate()
            child.wait(timeout=3)

    # -- R1: daemon reaping (no zombie / ResourceWarning) -------------------- #
    def test_reap_in_background_reaps_child(self) -> None:
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(0.2)"])
        thread = process._reap_in_background(child)
        self.assertTrue(thread.daemon)
        thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertIsNotNone(child.returncode)

    def test_successful_start_leaves_no_warning_or_zombie(self) -> None:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            with mock.patch.object(process, "_build_command", self._worker_command()):
                started = process.start_worker(self.data_dir, timeout=10.0)
            gc.collect()
            stopped = process.stop_worker(self.data_dir, timeout=10.0)
            # Let the background reaper observe the exit, then force collection.
            time.sleep(0.5)
            gc.collect()
        self.assertTrue(started["running"])
        self.assertTrue(stopped["stopped"])
        resource = [
            w for w in caught
            if issubclass(w.category, ResourceWarning)
            and "still running" in str(w.message)
        ]
        self.assertEqual(resource, [], [str(w.message) for w in resource])

    # -- argument validation ------------------------------------------------- #
    def test_argument_validation(self) -> None:
        for bad in (0, -1, float("nan"), float("inf"), True, "x"):
            with self.subTest(timeout=bad):
                with self.assertRaises(process.WorkerProcessError):
                    process.worker_status(self.data_dir, timeout=bad)
        with self.assertRaises(process.WorkerProcessError):
            process.start_worker(None)
        with self.assertRaises(process.WorkerProcessError):
            process.stop_worker("", timeout=1.0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
