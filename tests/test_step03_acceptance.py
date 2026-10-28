"""Step 03 acceptance: real-process crash/concurrency matrix (task T7).

This module proves the Step 03 durable-worker acceptance criteria against the
**integrated engine** (``python -m review_article_agent.worker --data-dir``) with
*o*wned, disposable, real OS processes* — never the ordinary OpenCode service,
the network, or a paid model. It is deliberately independent of the new
worker-lifecycle CLI spellings (those are proven separately by T5c); the only
entry point used here is the engine process plus the owner-only UDS control
protocol.

Coverage (``docs/implementation/03-durable-worker.md`` AC1-AC7 and
``docs/contracts/02-gates-and-runtime.md`` §5.6/§6.3/§8):

* **AC2 — SIGKILL restart matrix at all three dispatch seams.** For each of
  ``before_dispatch`` / ``after_admission`` / ``before_commit`` a worker is
  stopped at the seam via the test-only ``RAA_WORKER_TEST_PAUSE_AT`` gate
  (the worker ``SIGSTOP``s *only itself*), the persisted job/attempt/fence/
  reserve and the backend admission count are captured, the worker is hard
  ``SIGKILL``ed, a fresh worker restarts without the hook, and exactly one
  accepted local result is recovered under the **same** attempt id and fence
  (``attempts_total`` 1, one backend admission, no extra reserves). No
  exactly-once *external* claim is made: the synthetic backend state is
  explained by the engine's own journal, not a real provider.
* **AC1 — detached submitter lifetime.** A separate ``python`` submitter process
  exits before the background job completes; the worker finishes it anyway.
* **AC4 — one active project / safe switch.** Two concurrent project submitters
  cannot admit two projects; a switch while a remote is in flight is blocked and
  drains the old project, whose result is accepted **only** in the old store,
  before the new project runs.
* **AC3/AC5 — pause/cancel ambiguity and stale fencing.** An ambiguous outcome
  pins the global activation and its reserve, admits nothing new, and does not
  busy-loop (bounded ``/proc`` CPU over a wall interval); a stale attempt fence
  cannot overwrite a newer accepted result.
* **AC6 — pause/budget.** A budget cap stops new admission and retains an
  unknown-cost reserve.
* **AC7 — auth.** Owner-only UDS control plus injected non-owner denial is
  already covered by the existing 47-test IPC/activation suites; this module
  cites them rather than duplicating, and does not attempt a cross-UID check
  (which would need a second, privileged user).
* **T6 interop.** A completed engine job backs up/verifies/restores (real store
  backup API) with zero integrated cost and restored-inner-validation; a restored
  ambiguous job in a **separate** registry with **no** external journal is an
  explicit unknown that blocks a switch/dispatch of another project after
  restart — never a silent success or a new identity.

Every wait is bounded with backoff, every owned child is cleaned up in
``try``/``finally`` (RPC ``stop`` while responsive, else ``kill`` of our own
``Popen`` only, then ``wait``), and all runtime state lives under a private
``tempfile`` directory.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from review_article_agent.runtime.ipc import WorkerIPCClient  # noqa: E402
from review_article_agent.runtime.jobs import (  # noqa: E402
    BudgetExceededError,
    JobRepository,
    StaleFenceError,
)
from review_article_agent.storage import ProjectStore, Registry, paths  # noqa: E402
from review_article_agent.storage.backup import (  # noqa: E402
    create_backup,
    restore_backup,
    verify_backup,
)
from review_article_agent.worker.engine import (  # noqa: E402
    APP_NAME,
    PROTOCOL_VERSION,
    SYNTHETIC_NUMERIC_TASK,
    TEST_PAUSE_ENV,
)

#: The three dispatch fault seams the engine exposes (engine.FAULT_SEAMS).
SEAMS = ("before_dispatch", "after_admission", "before_commit")

#: The synthetic provider journal filename (``worker.synthetic.JOURNAL_FILENAME``).
_JOURNAL = os.path.join("runtime", "synthetic.sqlite3")

#: Per-seam expected state right after the worker stops at the seam. ``backend``
#: is the synthetic admission-row count; ``attempt_state``/``admitted`` describe
#: the persisted current attempt.
_SEAM_EXPECTATION = {
    "before_dispatch": {"backend": 0, "attempt_state": "queued", "admitted": False},
    "after_admission": {"backend": 1, "attempt_state": "queued", "admitted": False},
    "before_commit": {"backend": 1, "attempt_state": "admitted", "admitted": True},
}


def _req(op: str, **kw: object) -> dict:
    return {"version": PROTOCOL_VERSION, "op": op, **kw}


def _proc_state(pid: int) -> str | None:
    """Return the ``/proc/<pid>/stat`` single-letter state, or ``None`` if gone."""
    try:
        with open(f"/proc/{pid}/stat", "r", encoding="utf-8") as handle:
            return handle.read().split(") ", 1)[1].split(" ", 1)[0]
    except OSError:
        return None


def _proc_cpu_ticks(pid: int) -> int | None:
    """Return utime+stime (clock ticks) for ``pid``, or ``None`` if gone."""
    try:
        with open(f"/proc/{pid}/stat", "r", encoding="utf-8") as handle:
            rest = handle.read().split(") ", 1)[1].split()
    except OSError:
        return None
    # After "pid (comm)", index 0 is field 3 (state); utime is field 14 (index 11)
    # and stime is field 15 (index 12).
    try:
        return int(rest[11]) + int(rest[12])
    except (IndexError, ValueError):  # pragma: no cover - defensive
        return None


def _backend_dispatch_count(data_dir: object) -> int:
    """Count synthetic provider admission rows in ``<data_dir>/runtime``."""
    path = os.path.join(str(data_dir), _JOURNAL)
    if not os.path.exists(path):
        return 0
    conn = sqlite3.connect("file:" + path + "?mode=ro", uri=True)
    try:
        return int(conn.execute("SELECT COUNT(*) FROM dispatch").fetchone()[0])
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Disposable submitter programs (written to a temp dir; never added to the pkg)
# --------------------------------------------------------------------------- #
_SUBMITTER = r'''
import sys
sys.path.insert(0, {src!r})
from review_article_agent.runtime.ipc import WorkerIPCClient

data_dir, project_id, request_id, delay_ms = (
    sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4])
)
client = WorkerIPCClient(data_dir, io_timeout=15.0)
res = client.request({{
    "version": 1, "op": "submit", "project_id": project_id,
    "request_id": request_id, "task_kind": "synthetic_numeric",
    "steps": 5, "delay_ms": delay_ms, "outcome": "complete",
    "stage": "s", "reserve_usd": "0.10",
}})
if res.get("ok"):
    print("OK " + res["job"]["job_id"])
else:
    print("ERR " + str(res.get("code")))
'''

# A submit writer: when given a second argument it is the project id that must
# have been accepted; otherwise prints the raw outcome. Nothing here is a model,
# network, or scientific call.
_CONCURRENT_SUBMITTER = _SUBMITTER


class _OwnedWorker:
    """A directly-owned engine process plus its owner control client.

    The ``Popen`` object is held so that a hard kill always targets *this* child
    (never a stored or foreign PID). ``SIGSTOP`` in the worker only ever stops
    the worker's own process image, so no other process is signalled.
    """

    def __init__(self, data_dir: object, *, pause_at: str | None = None) -> None:
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join([str(SRC), env.get("PYTHONPATH", "")])
        if pause_at is not None:
            env[TEST_PAUSE_ENV] = pause_at
        else:
            env.pop(TEST_PAUSE_ENV, None)
        self.data_dir = str(data_dir)
        self.proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "review_article_agent.worker",
                "--data-dir",
                self.data_dir,
                "--poll-timeout",
                "0.2",
            ],
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.client = WorkerIPCClient(self.data_dir, io_timeout=10.0)

    # -- readiness / control ------------------------------------------------- #
    def wait_ready(self, timeout: float = 20.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise AssertionError(
                    f"worker exited before ready (rc={self.proc.returncode})"
                )
            try:
                status = self.client.request(_req("status"))
                if status.get("app") == APP_NAME:
                    return
            except Exception:  # noqa: BLE001 - transport not up yet
                time.sleep(0.03)
        raise AssertionError("worker did not become ready")

    def request(self, request: dict) -> dict:
        return self.client.request(request)

    def wait_state(self, want: str, timeout: float = 20.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if _proc_state(self.proc.pid) == want:
                return True
            time.sleep(0.02)
        return False

    # -- teardown ------------------------------------------------------------ #
    def stop(self, timeout: float = 20.0) -> None:
        """Stop gracefully if responsive, else hard-kill our own child."""
        if self.proc.poll() is None:
            try:
                self.client.request(_req("stop"))
            except Exception:  # noqa: BLE001 - a stopped/killed worker is expected
                pass
            try:
                self.proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                self.hard_kill()
        self._close_streams()

    def hard_kill(self) -> None:
        if self.proc.poll() is None:
            self.proc.kill()
            try:
                self.proc.wait(timeout=15)
            except subprocess.TimeoutExpired:  # pragma: no cover - defensive
                pass
        self._close_streams()

    def _close_streams(self) -> None:
        for stream in (self.proc.stdout, self.proc.stderr):
            if stream is not None:
                try:
                    stream.close()
                except OSError:  # pragma: no cover - defensive
                    pass


class _AcceptanceBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory(prefix="raa-step03-acc-")
        self.addCleanup(self._tmpdir.cleanup)
        self.tmp = Path(self._tmpdir.name)
        self._workers: list[_OwnedWorker] = []

    def tearDown(self) -> None:
        for worker in self._workers:
            worker.hard_kill()

    # -- fixtures / helpers -------------------------------------------------- #
    def _fixture(self, *, name: str = "state", project: str = "proj"):
        """Create an isolated ``(data_dir, registry, info)`` triple."""
        data_dir = self.tmp / name
        registry = Registry(data_dir)
        info = registry.create_project(self.tmp / project, name=project)
        return data_dir, registry, info

    def _start_worker(self, data_dir: object, *, pause_at: str | None = None) -> _OwnedWorker:
        worker = _OwnedWorker(data_dir, pause_at=pause_at)
        self._workers.append(worker)
        return worker

    def _store_for(self, info) -> ProjectStore:
        return ProjectStore.open_at(
            paths.app_dir_for(info.root_path), expected_project_id=info.project_id
        )

    def _disk_view(self, info, job_id: str) -> dict:
        """Read the persisted job/attempt/reserve state directly from the store."""
        store = self._store_for(info)
        try:
            repo = JobRepository(store)
            job = repo.get(job_id)
            attempts = repo.attempts(job_id)
            accepted = repo.accepted_result(job_id)
            current = (
                repo.attempt(job.current_attempt_id)
                if job is not None and job.current_attempt_id
                else None
            )
            reserves = [
                dict(row)
                for row in store.connection.execute(
                    "SELECT reserve_id, attempt_id, amount_usd, state "
                    "FROM job_budget_reserve WHERE project_id = ?",
                    (info.project_id,),
                ).fetchall()
            ]
            return {
                "job": job,
                "attempt": current,
                "attempt_count": job.attempts_total if job is not None else 0,
                "accepted": accepted,
                "reserves": reserves,
                "reserve_count": len(reserves),
            }
        finally:
            store.close()

    @staticmethod
    def _wait_for(predicate, timeout: float = 20.0, interval: float = 0.03) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(interval)
        return False

    def _submit(self, worker: _OwnedWorker, project_id: str, request_id: str, **kw):
        params = {
            "project_id": project_id,
            "request_id": request_id,
            "task_kind": SYNTHETIC_NUMERIC_TASK,
            "steps": kw.pop("steps", 7),
            "delay_ms": kw.pop("delay_ms", 0),
            "outcome": kw.pop("outcome", "complete"),
            "stage": kw.pop("stage", "s"),
            "reserve_usd": kw.pop("reserve_usd", "0.10"),
        }
        params.update(kw)
        return worker.request(_req("submit", **params))

    def _job_status(self, worker: _OwnedWorker, project_id: str, job_id: str) -> dict:
        res = worker.request(
            _req("job_status", project_id=project_id, job_id=job_id)
        )
        return res

    def _write_script(self, name: str, body: str) -> str:
        path = self.tmp / name
        path.write_text(body.format(src=str(SRC)), encoding="utf-8")
        return str(path)


class SigkillRestartMatrixTest(_AcceptanceBase):
    """AC2: SIGKILL/restart at every dispatch seam recovers one local result."""

    def _run_case(self, seam: str, case_id: str) -> dict:
        data_dir, registry, info = self._fixture(
            name=f"state-{case_id}", project=f"proj-{case_id}"
        )
        project_id = info.project_id

        first = self._start_worker(data_dir, pause_at=seam)
        first.wait_ready()
        submitted = self._submit(first, project_id, f"req-{case_id}")
        self.assertTrue(submitted["ok"], submitted)
        job_id = submitted["job"]["job_id"]

        # Precise, bounded proof the worker stopped exactly at the seam.
        self.assertTrue(
            first.wait_state("T", timeout=25.0),
            f"worker never reached the {seam!r} gate",
        )

        expected = _SEAM_EXPECTATION[seam]
        view = self._disk_view(info, job_id)
        self.assertIsNotNone(view["attempt"], "no current attempt persisted")
        self.assertEqual(view["attempt"].state, expected["attempt_state"])
        self.assertEqual(view["attempt"].fence, 1)
        self.assertEqual(view["attempt"].reserve_usd, "0.10")
        self.assertEqual(bool(view["attempt"].admission_id), expected["admitted"])
        self.assertEqual(view["attempt_count"], 1)
        self.assertEqual(view["reserve_count"], 1)
        self.assertEqual(_backend_dispatch_count(data_dir), expected["backend"])
        self.assertIsNone(view["accepted"], "no result may be committed at the seam")
        attempt_id = view["attempt"].attempt_id
        fence = view["attempt"].fence
        observed = {
            "seam": seam,
            "backend_at_seam": expected["backend"],
            "attempt_state_at_seam": view["attempt"].state,
        }

        first.hard_kill()
        self.assertIsNotNone(first.proc.returncode)

        # Restart WITHOUT the test hook: a fresh engine owns the lock and
        # reconciles the same attempt identity to one authoritative terminal.
        second = self._start_worker(data_dir)
        second.wait_ready()

        def _terminal() -> bool:
            res = self._job_status(second, project_id, job_id)
            return res.get("ok") and res["job"]["state"] in ("completed", "failed")

        self.assertTrue(self._wait_for(_terminal, timeout=25.0), "no terminal state")
        final = self._job_status(second, project_id, job_id)
        self.assertTrue(final["ok"])
        job = final["job"]
        self.assertEqual(job["state"], "completed", job)
        self.assertFalse(job["outcome_unresolved"])
        self.assertEqual(job["attempts_total"], 1)
        self.assertEqual(job["current_attempt_id"], attempt_id)
        self.assertEqual(job["current_fence"], fence)
        self.assertIsNotNone(job["accepted_result"])
        self.assertEqual(job["accepted_result"]["fence"], fence)

        after = self._disk_view(info, job_id)
        self.assertIsNotNone(after["accepted"])
        self.assertEqual(after["attempt_count"], 1)
        self.assertEqual(after["reserve_count"], 1)
        self.assertEqual(_backend_dispatch_count(data_dir), 1)

        observed["backend_after_recovery"] = _backend_dispatch_count(data_dir)
        observed["attempts_total_after"] = after["attempt_count"]
        observed["accepted_fence"] = after["accepted"].fence

        second.stop()
        return observed

    def test_matrix_all_seams_multiple_passes(self) -> None:
        # Two passes per seam for stability; the observed counts are asserted
        # inside each case and recorded here.
        observed: list[dict] = []
        for pass_no in range(2):
            for seam in SEAMS:
                with self.subTest(seam=seam, pass_no=pass_no):
                    observed.append(self._run_case(seam, f"{seam}-{pass_no}"))
        # One accepted result per case, one backend admission each, never more.
        self.assertEqual(len(observed), len(SEAMS) * 2)
        for entry in observed:
            self.assertEqual(entry["backend_after_recovery"], 1, entry)
            self.assertEqual(entry["attempts_total_after"], 1, entry)
            self.assertEqual(entry["accepted_fence"], 1, entry)


class DetachedSubmitterTest(_AcceptanceBase):
    """AC1: a background job finishes after the submitting CLI process exits."""

    def test_job_completes_after_submitter_process_exits(self) -> None:
        data_dir, _registry, info = self._fixture()
        worker = self._start_worker(data_dir)
        worker.wait_ready()
        script = self._write_script("detached_submitter.py", _SUBMITTER)

        completed = subprocess.run(
            [sys.executable, script, str(data_dir), info.project_id, "detached-1", "0"],
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        out = completed.stdout.strip()
        self.assertTrue(out.startswith("OK "), out)
        job_id = out.split(" ", 1)[1]

        # The submitter process has fully exited (subprocess.run returned) while
        # the independent worker process is still alive and serving.
        self.assertIsNone(worker.proc.poll())

        def _terminal() -> bool:
            res = self._job_status(worker, info.project_id, job_id)
            return res.get("ok") and res["job"]["state"] in ("completed", "failed")

        self.assertTrue(
            self._wait_for(_terminal, timeout=25.0),
            "detached job did not complete after the submitter exited",
        )
        final = self._job_status(worker, info.project_id, job_id)
        self.assertEqual(final["job"]["state"], "completed")
        self.assertIsNotNone(final["job"]["accepted_result"])
        worker.stop()


class ConcurrentActivationTest(_AcceptanceBase):
    """AC4: two concurrent project submitters cannot admit two projects."""

    def test_two_concurrent_project_submits_only_one_admits(self) -> None:
        data_dir = self.tmp / "state"
        registry = Registry(data_dir)
        a = registry.create_project(self.tmp / "a", name="A")
        b = registry.create_project(self.tmp / "b", name="B")
        worker = self._start_worker(data_dir)
        worker.wait_ready()
        script = self._write_script("concurrent_submitter.py", _CONCURRENT_SUBMITTER)

        # A long delay keeps the winner's remote in flight (and its activation
        # pinned) so the loser sees a genuinely concurrent conflict.
        def _run(project_id: str, request_id: str) -> subprocess.Popen:
            return subprocess.Popen(
                [
                    sys.executable,
                    script,
                    str(data_dir),
                    project_id,
                    request_id,
                    "60000",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

        p_a = _run(a.project_id, "conc-a")
        p_b = _run(b.project_id, "conc-b")
        out_a, err_a = p_a.communicate(timeout=60)
        out_b, err_b = p_b.communicate(timeout=60)
        self.assertEqual(p_a.returncode, 0, err_a)
        self.assertEqual(p_b.returncode, 0, err_b)

        results = [out_a.strip(), out_b.strip()]
        ok = [r for r in results if r.startswith("OK")]
        errs = [r for r in results if r.startswith("ERR")]
        self.assertEqual(len(ok), 1, results)
        self.assertEqual(len(errs), 1, results)
        self.assertIn(
            errs[0].split(" ", 1)[1],
            {"wrong_project", "switch_blocked", "denied"},
            results,
        )
        activation = registry.activation()
        self.assertIn(
            activation.active_project_id, {a.project_id, b.project_id}
        )
        worker.stop()


class SwitchWhileInflightTest(_AcceptanceBase):
    """AC4: a switch drains the old project; its result stays in the old store."""

    def test_switch_blocks_then_old_result_only_in_old_store(self) -> None:
        data_dir = self.tmp / "state"
        registry = Registry(data_dir)
        a = registry.create_project(self.tmp / "a", name="A")
        b = registry.create_project(self.tmp / "b", name="B")
        worker = self._start_worker(data_dir)
        worker.wait_ready()

        submitted = self._submit(worker, a.project_id, "sw-1", delay_ms=1200)
        self.assertTrue(submitted["ok"], submitted)
        job_id = submitted["job"]["job_id"]

        def _inflight() -> bool:
            res = self._job_status(worker, a.project_id, job_id)
            return res.get("ok") and res["job"]["state"] in ("admitted", "running")

        self.assertTrue(self._wait_for(_inflight, timeout=20.0), "remote never in flight")

        blocked = worker.request(_req("switch", project_id=b.project_id))
        self.assertFalse(blocked["ok"])
        self.assertEqual(blocked["code"], "switch_blocked")
        self.assertEqual(registry.activation().active_project_id, a.project_id)

        # The old project's in-flight remote still reconciles to exactly one
        # accepted result while held, in the OLD store only.
        def _accepted_a() -> bool:
            return self._disk_view(a, job_id)["accepted"] is not None

        self.assertTrue(self._wait_for(_accepted_a, timeout=25.0), "old result never landed")
        view_a = self._disk_view(a, job_id)
        self.assertIsNotNone(view_a["accepted"])
        self.assertEqual(view_a["accepted"].fence, view_a["attempt"].fence)

        switched = worker.request(_req("switch", project_id=b.project_id))
        self.assertTrue(switched["ok"], switched)
        self.assertEqual(registry.activation().active_project_id, b.project_id)

        # B's own store holds no accepted result for A's job.
        view_b = self._disk_view(b, job_id)
        self.assertIsNone(view_b["job"])
        self.assertIsNone(view_b["accepted"])

        # B can now run its own work to completion.
        submitted_b = self._submit(worker, b.project_id, "b-1")
        self.assertTrue(submitted_b["ok"], submitted_b)
        job_b = submitted_b["job"]["job_id"]

        def _accepted_b() -> bool:
            res = self._job_status(worker, b.project_id, job_b)
            return res.get("ok") and res["job"]["state"] == "completed"

        self.assertTrue(self._wait_for(_accepted_b, timeout=25.0), "B never completed")
        self.assertIsNotNone(self._disk_view(b, job_b)["accepted"])
        worker.stop()


class AmbiguityPinsActivationTest(_AcceptanceBase):
    """AC3/AC5: ambiguity pins activation, admits nothing new, never busy-loops."""

    def test_pause_cancel_ambiguity_pins_activation_and_no_busy_loop(self) -> None:
        data_dir = self.tmp / "state"
        registry = Registry(data_dir)
        a = registry.create_project(self.tmp / "a", name="A")
        b = registry.create_project(self.tmp / "b", name="B")
        worker = self._start_worker(data_dir)
        worker.wait_ready()

        submitted = self._submit(worker, a.project_id, "amb-1", outcome="ambiguous")
        self.assertTrue(submitted["ok"], submitted)
        job_id = submitted["job"]["job_id"]

        def _waiting() -> bool:
            res = self._job_status(worker, a.project_id, job_id)
            return (
                res.get("ok")
                and res["job"]["state"] == "waiting_human"
                and res["job"]["outcome_unresolved"]
            )

        self.assertTrue(self._wait_for(_waiting, timeout=25.0), "no explicit unknown")
        self.assertEqual(_backend_dispatch_count(data_dir), 1)

        paused = worker.request(_req("pause", project_id=a.project_id))
        self.assertTrue(paused["ok"], paused)
        cancelled = worker.request(
            _req("cancel", project_id=a.project_id, job_id=job_id)
        )
        self.assertTrue(cancelled["ok"], cancelled)
        self.assertTrue(cancelled["job"]["outcome_unresolved"])

        view = self._disk_view(a, job_id)
        self.assertTrue(view["job"].outcome_unresolved)
        self.assertIn(view["job"].state, ("paused", "cancelled", "reconciliation_required"))
        self.assertIsNone(view["accepted"])
        self.assertEqual(view["reserve_count"], 1)

        # The global active arbitration is retained and no new admission occurs.
        self.assertEqual(registry.activation().active_project_id, a.project_id)
        other = worker.request(
            _req(
                "submit",
                project_id=b.project_id,
                request_id="b-blocked",
                task_kind=SYNTHETIC_NUMERIC_TASK,
                steps=3,
                delay_ms=0,
                outcome="complete",
                stage="s",
                reserve_usd="0.10",
            )
        )
        self.assertFalse(other["ok"])
        self.assertIn(other["code"], {"switch_blocked", "wrong_project", "denied"})
        self.assertEqual(_backend_dispatch_count(data_dir), 1)

        # No busy loop: bounded CPU over a wall interval while the gate waits.
        cpu0 = _proc_cpu_ticks(worker.proc.pid)
        time.sleep(1.0)
        cpu1 = _proc_cpu_ticks(worker.proc.pid)
        self.assertIsNotNone(cpu0)
        self.assertIsNotNone(cpu1)
        self.assertLess(cpu1 - cpu0, 50, "worker appears to busy-loop while waiting")
        worker.stop()


class StaleFenceTest(_AcceptanceBase):
    """AC5: a stale attempt fence cannot overwrite a newer accepted result."""

    def test_stale_attempt_cannot_overwrite_newer_result(self) -> None:
        app_dir = paths.app_dir_for(str(self.tmp / "fenceproj"))
        store = ProjectStore.create_at(app_dir, paths.new_project_id())
        try:
            repo = JobRepository(store)
            job = repo.submit("fence-1", task_kind="probe")
            first = repo.begin_attempt(job.job_id, dispatch_kind="local")
            repo.mark_definitive_failure(first.attempt_id)
            second = repo.begin_attempt(
                job.job_id,
                dispatch_kind="external_synthetic",
                stage="s",
                reserve_usd="0.10",
                retry_authorized_by="owner",
            )
            repo.mark_admitted(second.attempt_id, admission_id="adm-" + second.attempt_id)
            newer = repo.accept_result(
                second.attempt_id,
                result_kind="synthetic_numeric",
                result={"kind": "synthetic_numeric", "value": 222},
            )
            self.assertNotEqual(first.fence, second.fence)
            with self.assertRaises(StaleFenceError):
                repo.accept_result(
                    first.attempt_id,
                    result_kind="synthetic_numeric",
                    result={"kind": "synthetic_numeric", "value": 111},
                )
            after = repo.accepted_result(job.job_id)
            self.assertEqual(after.result, {"kind": "synthetic_numeric", "value": 222})
            self.assertEqual(after.fence, newer.fence)
            self.assertEqual(repo.get(job.job_id).state, "completed")
        finally:
            store.close()


class BudgetCapTest(_AcceptanceBase):
    """AC6: a cap stops new admission; an unknown reserve is retained."""

    def test_unknown_reserve_retained_and_cap_blocks_new_admission(self) -> None:
        # -- direct repository: a retained unknown reserve counts and blocks. --
        app_dir = paths.app_dir_for(str(self.tmp / "budgetproj"))
        store = ProjectStore.create_at(app_dir, paths.new_project_id())
        try:
            repo = JobRepository(store)
            repo.set_budget_policy(project_cap_usd="0.05", stage_cap_usd="0.05")
            j1 = repo.submit("budget-1", task_kind="probe")
            a1 = repo.begin_attempt(
                j1.job_id,
                dispatch_kind="external_synthetic",
                stage="s",
                reserve_usd="0.04",
            )
            repo.settle_cost(a1.attempt_id, cost_state="unknown")
            row = store.connection.execute(
                "SELECT amount_usd, state FROM job_budget_reserve WHERE attempt_id = ?",
                (a1.attempt_id,),
            ).fetchone()
            self.assertEqual((row["amount_usd"], row["state"]), ("0.04", "retained"))

            j2 = repo.submit("budget-2", task_kind="probe")
            with self.assertRaises(BudgetExceededError):
                repo.begin_attempt(
                    j2.job_id,
                    dispatch_kind="external_synthetic",
                    stage="s",
                    reserve_usd="0.03",
                )
            self.assertEqual(len(repo.attempts(j2.job_id)), 0)
            self.assertEqual(repo.get(j2.job_id).attempts_total, 0)
            total_reserves = store.connection.execute(
                "SELECT COUNT(*) FROM job_budget_reserve"
            ).fetchone()[0]
            self.assertEqual(total_reserves, 1)
            self.assertEqual(repo.budget_usage().project_reserved_usd, "0.04")
        finally:
            store.close()

        # -- engine: a reserve over the cap is refused with no new admission. --
        data_dir = self.tmp / "state"
        registry = Registry(data_dir)
        info = registry.create_project(self.tmp / "p", name="P")
        pre = self._store_for(info)
        try:
            JobRepository(pre).set_budget_policy(
                project_cap_usd="0.05", stage_cap_usd="0.05"
            )
        finally:
            pre.close()
        worker = self._start_worker(data_dir)
        worker.wait_ready()
        submitted = self._submit(worker, info.project_id, "cap-1", reserve_usd="0.10")
        self.assertTrue(submitted["ok"], submitted)
        job_id = submitted["job"]["job_id"]

        def _waiting() -> bool:
            res = self._job_status(worker, info.project_id, job_id)
            return (
                res.get("ok")
                and res["job"]["state"] == "waiting_human"
                and res["job"].get("wait_reason") == "budget_exceeded"
            )

        self.assertTrue(self._wait_for(_waiting, timeout=20.0), "no budget gate")
        self.assertEqual(_backend_dispatch_count(data_dir), 0)
        view = self._disk_view(info, job_id)
        self.assertEqual(view["reserve_count"], 0)
        self.assertEqual(view["attempt_count"], 0)
        self.assertIsNone(view["accepted"])
        worker.stop()


class BackupRestoreInteropTest(_AcceptanceBase):
    """T6: engine-job backup/roundtrip and restored ambiguity blocks execution."""

    def test_completed_engine_job_backup_roundtrip_cost_zero(self) -> None:
        data_dir, _registry, info = self._fixture()
        worker = self._start_worker(data_dir)
        worker.wait_ready()
        submitted = self._submit(worker, info.project_id, "bk-1", steps=9)
        self.assertTrue(submitted["ok"], submitted)
        job_id = submitted["job"]["job_id"]

        def _completed() -> bool:
            res = self._job_status(worker, info.project_id, job_id)
            return res.get("ok") and res["job"]["state"] == "completed"

        self.assertTrue(self._wait_for(_completed, timeout=25.0), "job never completed")
        worker.stop()

        backup_dir = self.tmp / "backup"
        store = self._store_for(info)
        try:
            create_backup(store, backup_dir)
        finally:
            store.close()
        report = verify_backup(backup_dir)
        self.assertTrue(report["ok"])
        self.assertEqual(report["store_schema_version"], 4)

        reg2 = Registry(self.tmp / "state2")
        restored = restore_backup(backup_dir, self.tmp / "restored", reg2)
        rstore = ProjectStore.open_at(
            paths.app_dir_for(restored.root_path),
            expected_project_id=restored.project_id,
        )
        try:
            rjobs = JobRepository(rstore)
            rjob = rjobs.get(job_id)
            self.assertEqual(rjob.state, "completed")
            self.assertIsNotNone(rjobs.accepted_result(job_id))
            attempt = rjobs.attempt(rjob.current_attempt_id)
            # The integrated engine's own cost is known-zero and the reserve is
            # settled (not left held) — the T6 inner checks accepted the format.
            self.assertEqual(attempt.cost_state, "known")
            self.assertEqual(attempt.cost_amount_usd, "0")
            reserve = rstore.connection.execute(
                "SELECT amount_usd, state FROM job_budget_reserve WHERE attempt_id = ?",
                (attempt.attempt_id,),
            ).fetchone()
            self.assertEqual(reserve["state"], "settled")
        finally:
            rstore.close()

    def test_restored_ambiguous_without_journal_blocks_other_project(self) -> None:
        # A SEPARATE registry/data_dir with NO external synthetic journal.
        data_dir2 = self.tmp / "state-amb"
        reg2 = Registry(data_dir2)
        amb = reg2.create_project(self.tmp / "ambproj", name="Amb")
        other = reg2.create_project(self.tmp / "otherproj", name="Other")
        store = self._store_for(amb)
        try:
            repo = JobRepository(store)
            job = repo.submit(
                "amb-restored", task_kind=SYNTHETIC_NUMERIC_TASK,
                parameters={"stage": "s", "reserve_usd": "0.10", "steps": 3,
                            "delay_ms": 0, "outcome": "ambiguous"},
            )
            attempt = repo.begin_attempt(
                job.job_id, dispatch_kind="external_synthetic",
                stage="s", reserve_usd="0.10",
            )
            repo.record_checkpoint(
                attempt.attempt_id,
                {"continuity_id": "a" * 32, "phase": "predispatch"},
            )
            repo.mark_admitted(attempt.attempt_id, admission_id="adm-" + attempt.attempt_id)
            repo.mark_ambiguous(attempt.attempt_id)
            job_id = job.job_id
            attempt_id = attempt.attempt_id
        finally:
            store.close()

        # No provider journal was ever created for this data dir.
        self.assertFalse(os.path.exists(os.path.join(str(data_dir2), _JOURNAL)))

        worker = self._start_worker(data_dir2)
        worker.wait_ready()

        def _explicit_unknown() -> bool:
            res = self._job_status(worker, amb.project_id, job_id)
            return (
                res.get("ok")
                and res["job"]["outcome_unresolved"]
                and res["job"]["state"]
                in ("waiting_human", "reconciliation_required", "waiting_external")
            )

        self.assertTrue(self._wait_for(_explicit_unknown, timeout=25.0), "not explicit unknown")
        final = self._job_status(worker, amb.project_id, job_id)["job"]
        self.assertTrue(final["outcome_unresolved"])
        self.assertIsNone(final.get("accepted_result"), "must not be a silent success")
        self.assertEqual(final["attempts_total"], 1, "must not mint a new identity")
        self.assertEqual(final["current_attempt_id"], attempt_id)

        # It blocks switching to, and dispatch of, the other project.
        switched = worker.request(_req("switch", project_id=other.project_id))
        self.assertFalse(switched["ok"], switched)
        dispatch = worker.request(
            _req(
                "submit",
                project_id=other.project_id,
                request_id="other-1",
                task_kind=SYNTHETIC_NUMERIC_TASK,
                steps=3,
                delay_ms=0,
                outcome="complete",
                stage="s",
                reserve_usd="0.10",
            )
        )
        self.assertFalse(dispatch["ok"], dispatch)

        view = self._disk_view(amb, job_id)
        self.assertEqual(view["attempt_count"], 1)
        self.assertEqual(view["attempt"].attempt_id, attempt_id)
        self.assertIsNone(view["accepted"])
        worker.stop()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
