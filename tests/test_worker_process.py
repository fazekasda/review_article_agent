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
import json
import os
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

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


class WorkerProcessTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="raa-wp-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.data_dir = str(self.tmp / "state")
        self.foreign_marker = str(self.tmp / "stop-marker")
        self.worker_script = self._write("fixture_worker.py", FIXTURE_WORKER)
        self.foreign_script = self._write("foreign_server.py", FOREIGN_SERVER)
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
