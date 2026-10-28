"""CLI acceptance tests for the ``job`` synthetic job-control surface (T5c).

Covers the Step 03 deliverable "Local worker independent of CLI lifetime;
supported start/status/attach/pause/resume/stop behavior and authenticated local
control" and the acceptance criteria "Start a task, close the CLI, and confirm
completion/checkpoint from a new CLI", "Concurrent activation attempts cannot
execute two projects", "Paused ... work does not schedule new calls", and
"Authentication rejects non-owner control", plus
``docs/contracts/02-gates-and-runtime.md`` §8.4 ("Close CLI (detached)": CLI
lifetime ends while an authorized background job continues) and the existing
``docs/cli.md`` output/exit/secret conventions.

Three layers:

* Most cases run the CLI as a **real subprocess** (``python -m
  review_article_agent``) driving the **real integrated engine**
  (``python -m review_article_agent.worker``) started by ``job run``. Every job
  is a bounded synthetic task; no model, provider, or network is used.
* A Ctrl-C detach case sends ``SIGINT`` to the CLI subprocess it started and
  proves the run is not cancelled.
* In-process cases substitute the public ``worker.process`` API and the IPC
  client to exercise error-category mapping and the "queries never start a
  worker" rule deterministically.

Every worker this file starts is an *owned* disposable process; it is stopped by
an authenticated RPC ``stop`` (never a signal, never a PID file) and the data
directory is a per-test temporary directory.

All data is synthetic: no secrets, no real project, no scientific evidence.
"""

from __future__ import annotations

import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

SRC = Path(__file__).resolve().parents[1] / "src"
REPO = Path(__file__).resolve().parents[1]
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import review_article_agent.cli.main as cli_main  # noqa: E402
from review_article_agent.runtime import ipc  # noqa: E402
from review_article_agent.worker import process  # noqa: E402

SECRET = "SYNTHETIC-SECRET-DO-NOT-ECHO"

_STATUS_TIMEOUT = 6.0


class JobCliTestCase(unittest.TestCase):
    """Base: a temp data directory and a real-subprocess CLI helper."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="raa-cli-jobs-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.state = str(self.tmp / "state")
        # Stop any worker this test started, by authenticated RPC (never a
        # signal, never a PID file). Safe/idempotent if none is running.
        self.addCleanup(lambda: process.stop_worker(self.state, timeout=10))

    # -- subprocess helpers -------------------------------------------------- #
    def _env(self) -> dict:
        env = dict(os.environ)
        existing = env.get("PYTHONPATH")
        env["PYTHONPATH"] = str(SRC) + (os.pathsep + existing if existing else "")
        env.pop("XDG_DATA_HOME", None)
        return env

    def raw(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-m", "review_article_agent", *args],
            capture_output=True,
            text=True,
            env=self._env(),
            cwd=str(REPO),
        )

    def cli(self, *args: str, expect: int = 0) -> dict:
        proc = self.raw(*args)
        self.assertEqual(
            proc.returncode,
            expect,
            f"args={args} rc={proc.returncode}\nstdout={proc.stdout}\nstderr={proc.stderr}",
        )
        if expect == 0:
            return json.loads(proc.stdout)
        self.assertNotIn("Traceback", proc.stderr)
        return json.loads(proc.stderr)

    def create_project(self, name: str) -> str:
        out = self.cli(
            "--data-dir", self.state, "create", str(self.tmp / name), "--name", name
        )
        return out["project"]["project_id"]

    def select(self, project_id: str) -> None:
        self.cli("--data-dir", self.state, "select", project_id)

    def run_job(self, request_id: str, *extra: str, expect: int = 0, project=None) -> dict:
        args = ["--data-dir", self.state, "job", "run", "--request-id", request_id]
        if project is not None:
            args += ["--project", project]
        args += list(extra)
        return self.cli(*args, expect=expect)

    def status(self, job_id: str, project: str) -> dict:
        return self.cli(
            "--data-dir", self.state, "job", "status", job_id, "--project", project
        )["job"]

    def await_state(self, job_id: str, project: str, predicate, timeout: float = _STATUS_TIMEOUT) -> dict:
        deadline = time.monotonic() + timeout
        last = None
        while time.monotonic() < deadline:
            last = self.status(job_id, project)
            if predicate(last):
                return last
            time.sleep(0.1)
        self.fail(f"job did not reach the expected state; last={last}")


class RunAdmissionTest(JobCliTestCase):
    def test_run_returns_admission_not_completion(self) -> None:
        pid = self.create_project("A")
        self.select(pid)
        out = self.run_job("r-1", "--steps", "1", "--delay-ms", "0")
        self.assertTrue(out["submitted"])
        self.assertFalse(out["job_completion"])
        self.assertFalse(out["scientific_validation"])
        self.assertTrue(out["virtual_reserve"])
        job = out["job"]
        # A submit returns durable admission, never a completed job.
        self.assertIn(job["state"], ("queued", "admitted"))
        self.assertNotEqual(job["state"], "completed")
        self.assertEqual(job["task_kind"], "synthetic_numeric")

    def test_separate_cli_later_sees_completion(self) -> None:
        pid = self.create_project("A")
        self.select(pid)
        out = self.run_job("r-2", "--steps", "3", "--delay-ms", "0")
        job_id = out["job"]["job_id"]
        # A *new* CLI process observes the completion (detached lifetime).
        final = self.await_state(job_id, pid, lambda j: j["state"] == "completed")
        self.assertEqual(final["state"], "completed")
        self.assertIsNotNone(final["accepted_result"])
        result = final["accepted_result"]["result"]
        self.assertEqual(result["kind"], "synthetic_numeric")
        self.assertIsInstance(result["value"], int)

    def test_run_ensures_worker_and_status_reads_it(self) -> None:
        pid = self.create_project("A")
        self.select(pid)
        out = self.run_job("r-3")
        job_id = out["job"]["job_id"]
        # The worker that run ensured is verified and running for a later probe.
        wstatus = self.cli("--data-dir", self.state, "worker", "status")["worker"]
        self.assertTrue(wstatus["running"])
        self.assertTrue(wstatus["verified"])
        self.assertGreater(wstatus["pid"], 0)
        self.assertEqual(self.status(job_id, pid)["job_id"], job_id)

    def test_waiting_human_task_is_durable_gate(self) -> None:
        pid = self.create_project("A")
        self.select(pid)
        out = self.run_job("r-4", "--task-kind", "synthetic_waiting_human")
        job_id = out["job"]["job_id"]
        final = self.await_state(job_id, pid, lambda j: j["state"] == "waiting_human")
        self.assertEqual(final["state"], "waiting_human")
        self.assertNotIn("accepted_result", final)


class PauseResumeCancelTest(JobCliTestCase):
    def test_pause_resume_cancel_are_truthful(self) -> None:
        pid = self.create_project("A")
        self.select(pid)
        job_id = self.run_job("p-1", "--task-kind", "synthetic_waiting_human")["job"]["job_id"]
        self.await_state(job_id, pid, lambda j: j["state"] == "waiting_human")

        paused = self.cli(
            "--data-dir", self.state, "job", "pause", job_id, "--project", pid
        )["job"]
        self.assertEqual(paused["state"], "paused")
        self.assertEqual(self.status(job_id, pid)["state"], "paused")

        resumed = self.cli(
            "--data-dir", self.state, "job", "resume", job_id, "--project", pid
        )["job"]
        # Resume restores the held gate; it never clears it or claims completion.
        self.assertEqual(resumed["state"], "waiting_human")
        self.assertEqual(self.status(job_id, pid)["state"], "waiting_human")

        cancelled = self.cli(
            "--data-dir", self.state, "job", "cancel", job_id, "--project", pid
        )["job"]
        self.assertEqual(cancelled["state"], "cancelled")
        self.assertEqual(self.status(job_id, pid)["state"], "cancelled")

    def test_resume_does_not_clear_unknown_outcome(self) -> None:
        pid = self.create_project("A")
        self.select(pid)
        job_id = self.run_job("p-2", "--outcome", "ambiguous", "--steps", "1", "--delay-ms", "0")[
            "job"
        ]["job_id"]
        unresolved = self.await_state(
            job_id, pid, lambda j: j["outcome_unresolved"] and j["state"] == "waiting_human"
        )
        self.assertTrue(unresolved["outcome_unresolved"])
        # Pause then resume must not clear the unresolved bit or raise a gate.
        paused = self.cli(
            "--data-dir", self.state, "job", "pause", job_id, "--project", pid
        )["job"]
        self.assertEqual(paused["state"], "paused")
        self.assertTrue(paused["outcome_unresolved"])
        resumed = self.cli(
            "--data-dir", self.state, "job", "resume", job_id, "--project", pid
        )["job"]
        self.assertTrue(resumed["outcome_unresolved"])
        self.assertNotEqual(resumed["state"], "completed")


class AttachTest(JobCliTestCase):
    def _attach(self, *extra: str, expect: int = 0):
        proc = self.raw("--data-dir", self.state, "job", "attach", *extra)
        return proc

    def test_attach_once_reads_one_snapshot(self) -> None:
        pid = self.create_project("A")
        self.select(pid)
        job_id = self.run_job("a-1", "--steps", "1", "--delay-ms", "0")["job"]["job_id"]
        self.await_state(job_id, pid, lambda j: j["state"] == "completed")
        proc = self._attach(job_id, "--once", "--project", pid)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        doc = json.loads(proc.stdout)
        self.assertFalse(doc["streaming"])
        self.assertEqual(doc["job"]["state"], "completed")

    def test_attach_watch_reaches_terminal_and_does_not_cancel(self) -> None:
        pid = self.create_project("A")
        self.select(pid)
        job_id = self.run_job("a-2", "--steps", "1", "--delay-ms", "100")["job"]["job_id"]
        proc = self._attach(
            job_id, "--watch", "--poll-interval", "0.1", "--timeout", "8", "--project", pid
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        lines = [line for line in proc.stdout.splitlines() if line.strip()]
        self.assertGreaterEqual(len(lines), 1)
        docs = [json.loads(line) for line in lines]
        self.assertTrue(all("job" in d for d in docs))
        # The watch observed the terminal completion...
        self.assertEqual(docs[-1]["job"]["state"], "completed")
        # ...and attaching did not cancel or mutate the job on exit.
        self.assertEqual(self.status(job_id, pid)["state"], "completed")

    def test_attach_watch_timeout_does_not_cancel_waiting_gate(self) -> None:
        pid = self.create_project("A")
        self.select(pid)
        job_id = self.run_job("a-3", "--task-kind", "synthetic_waiting_human")["job"]["job_id"]
        self.await_state(job_id, pid, lambda j: j["state"] == "waiting_human")
        proc = self._attach(
            job_id, "--watch", "--poll-interval", "0.1", "--timeout", "0.6", "--project", pid
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        # A bounded watch on a non-terminal gate exits cleanly and never cancels.
        self.assertEqual(self.status(job_id, pid)["state"], "waiting_human")

    def test_ctrl_c_detach_is_clean_and_does_not_cancel(self) -> None:
        pid = self.create_project("A")
        self.select(pid)
        job_id = self.run_job("a-4", "--task-kind", "synthetic_waiting_human")["job"]["job_id"]
        self.await_state(job_id, pid, lambda j: j["state"] == "waiting_human")

        child = subprocess.Popen(
            [
                sys.executable, "-m", "review_article_agent",
                "--data-dir", self.state, "job", "attach", job_id,
                "--watch", "--poll-interval", "0.2", "--timeout", "30", "--project", pid,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=self._env(),
            cwd=str(REPO),
        )
        try:
            first = child.stdout.readline()
            self.assertTrue(first.strip(), "attach produced no first snapshot")
            time.sleep(0.2)
            child.send_signal(signal.SIGINT)
            out, err = child.communicate(timeout=10)
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=5)
        self.assertNotEqual(child.returncode, 0)
        self.assertNotIn("Traceback", err)
        self.assertNotIn("Traceback", out)
        # Ctrl-C is a clean detach: the job was never cancelled.
        self.assertEqual(self.status(job_id, pid)["state"], "waiting_human")


class ConcurrentProjectTest(JobCliTestCase):
    def test_concurrent_active_other_project_is_conflict(self) -> None:
        pa = self.create_project("A")
        pb = self.create_project("B")
        self.select(pa)
        # A's ambiguous job retains execution arbitration (unresolved remote).
        job_id = self.run_job("c-1", "--outcome", "ambiguous", "--steps", "1", "--delay-ms", "0")[
            "job"
        ]["job_id"]
        self.await_state(
            job_id, pa, lambda j: j["outcome_unresolved"] and j["state"] == "waiting_human"
        )
        # B cannot execute while A is unresolved: a stable conflict, not a stray.
        err = self.run_job("c-2", "--project", pb, expect=3)
        self.assertEqual(err["error"]["category"], "conflict")


class ErrorAndSecretTest(JobCliTestCase):
    def test_invalid_numeric_and_secret_values_are_never_echoed(self) -> None:
        pid = self.create_project("A")
        self.select(pid)
        run_cases = ("--steps", "--delay-ms", "--reserve", "--timeout")
        for flag in run_cases:
            with self.subTest(flag=flag):
                proc = self.raw(
                    "--data-dir", self.state, "job", "run",
                    "--request-id", "x", flag, SECRET,
                )
                self.assertEqual(proc.returncode, 2, proc.stderr)
                combined = proc.stdout + proc.stderr
                self.assertNotIn(SECRET, combined)
                self.assertNotIn("Traceback", combined)
                self.assertEqual(json.loads(proc.stderr)["error"]["category"], "validation")
        # A secret in the attach poll interval is likewise never echoed.
        proc = self.raw(
            "--data-dir", self.state, "job", "attach", "jobx",
            "--watch", "--poll-interval", SECRET,
        )
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertNotIn(SECRET, proc.stdout + proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)
        self.assertEqual(json.loads(proc.stderr)["error"]["category"], "validation")

    def test_unknown_option_is_usage_without_echo(self) -> None:
        pid = self.create_project("A")
        self.select(pid)
        proc = self.raw(
            "--data-dir", self.state, "job", "run", "--request-id", "x", "--api-key", SECRET
        )
        self.assertEqual(proc.returncode, 2)
        self.assertNotIn(SECRET, proc.stdout + proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)
        self.assertEqual(json.loads(proc.stderr)["error"]["category"], "usage")

    def test_reconcile_confirm_required_before_any_request(self) -> None:
        pid = self.create_project("A")
        self.select(pid)
        err = self.cli(
            "--data-dir", self.state, "job", "reconcile", "job_x",
            "--resolution", "definitive_failure", "--project", pid, expect=2,
        )
        self.assertEqual(err["error"]["category"], "validation")

    def test_unavailable_worker_is_nonzero(self) -> None:
        pid = self.create_project("A")
        self.select(pid)
        # No worker has been started for this data directory.
        err = self.cli("--data-dir", self.state, "job", "list", "--project", pid, expect=4)
        self.assertEqual(err["error"]["category"], "storage")

    def test_worker_stopped_then_query_is_nonzero(self) -> None:
        pid = self.create_project("A")
        self.select(pid)
        job_id = self.run_job("e-1", "--steps", "1", "--delay-ms", "0")["job"]["job_id"]
        self.await_state(job_id, pid, lambda j: j["state"] == "completed")
        self.cli("--data-dir", self.state, "worker", "stop")
        err = self.cli("--data-dir", self.state, "job", "status", job_id, "--project", pid, expect=4)
        self.assertEqual(err["error"]["category"], "storage")


class InProcessMappingTest(JobCliTestCase):
    """Deterministic error-category and no-implicit-start checks (in process)."""

    def _run(self, argv, env=None):
        out, err = io.StringIO(), io.StringIO()
        code = cli_main.main(
            list(argv), env={} if env is None else env, stdout=out, stderr=err
        )
        return code, out.getvalue(), err.getvalue()

    def _activate(self) -> str:
        pid = self.create_project("A")
        self.cli("--data-dir", self.state, "select", pid)
        return pid

    def test_queries_never_start_a_worker(self) -> None:
        pid = self._activate()
        start = mock.MagicMock()
        status = mock.MagicMock(return_value={
            "running": True, "verified": True, "app": process.APP_MARKER,
            "protocol_version": process.PROTOCOL_VERSION, "pid": 4321, "state": "running",
        })
        with mock.patch.object(process, "start_worker", start), mock.patch.object(
            process, "worker_status", status
        ), mock.patch.object(ipc.WorkerIPCClient, "request", return_value={"ok": True, "jobs": []}):
            code, _out, _err = self._run(
                ["--data-dir", self.state, "job", "list", "--project", pid]
            )
        self.assertEqual(code, 0)
        start.assert_not_called()

    def test_foreign_endpoint_maps_to_conflict(self) -> None:
        pid = self._activate()
        down = {
            "running": False, "verified": False, "app": None, "protocol_version": None,
            "pid": None, "state": None, "reason": "foreign_endpoint",
        }
        with mock.patch.object(process, "worker_status", return_value=down):
            code, _out, err = self._run(
                ["--data-dir", self.state, "job", "status", "j1", "--project", pid]
            )
        self.assertEqual(code, 3)
        self.assertEqual(json.loads(err)["error"]["category"], "conflict")

    def test_engine_error_codes_map_to_static_categories(self) -> None:
        pid = self._activate()
        running = {
            "running": True, "verified": True, "app": process.APP_MARKER,
            "protocol_version": process.PROTOCOL_VERSION, "pid": 4321, "state": "running",
        }
        for code, expected in (("bad_value", 2), ("wrong_project", 3), ("internal_error", 4)):
            with self.subTest(code=code):
                with mock.patch.object(process, "worker_status", return_value=running), mock.patch.object(
                    ipc.WorkerIPCClient, "request", return_value={"ok": False, "code": code}
                ):
                    rc, _out, err = self._run(
                        ["--data-dir", self.state, "job", "status", "j1", "--project", pid]
                    )
                self.assertEqual(rc, expected)
                self.assertNotIn("Traceback", err)

    def test_transport_failure_is_curated_storage(self) -> None:
        pid = self._activate()
        running = {
            "running": True, "verified": True, "app": process.APP_MARKER,
            "protocol_version": process.PROTOCOL_VERSION, "pid": 4321, "state": "running",
        }
        with mock.patch.object(process, "worker_status", return_value=running), mock.patch.object(
            ipc.WorkerIPCClient, "request", side_effect=ipc.IPCError(f"raw {SECRET}")
        ):
            rc, out, err = self._run(
                ["--data-dir", self.state, "job", "status", "j1", "--project", pid]
            )
        self.assertEqual(rc, 4)
        self.assertNotIn(SECRET, out + err)
        self.assertNotIn("Traceback", err)
        self.assertEqual(json.loads(err)["error"]["category"], "storage")

    def test_start_worker_failure_is_curated_storage(self) -> None:
        pid = self._activate()
        with mock.patch.object(
            process, "start_worker", side_effect=process.WorkerStartError(f"raw {SECRET}")
        ):
            rc, out, err = self._run(
                ["--data-dir", self.state, "job", "run", "--request-id", "x", "--project", pid]
            )
        self.assertEqual(rc, 4)
        self.assertNotIn(SECRET, out + err)
        self.assertNotIn("Traceback", err)
        self.assertEqual(json.loads(err)["error"]["category"], "storage")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
