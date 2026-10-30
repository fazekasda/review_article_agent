"""Step 15 release AC3/AC4 fault/recovery suite on **disposable** instances.

``docs/implementation/15-release.md``:

* **AC3** — "Close CLI, interrupt worker/harness independently, lose events,
  duplicate admissions, and restore backups on disposable instances. No lost
  scientific approval or duplicate accepted result; ambiguous external spend
  remains documented."
* **AC4** — "Two stored projects remain isolated and cannot execute concurrently.
  Safe activation/pause/switch behavior is demonstrated."

Every instance is a private ``tempfile`` root and every worker/harness is an
*owned, disposable* process or an in-memory mock. The user's real OpenCode
service, its data, and its registrations are never touched. This module is a
thin re-composition of the existing acceptance helpers, which are imported rather
than copied:

* ``tests/test_cli_jobs.py`` — real-subprocess CLI admission/detach (scenario 1);
* ``tests/test_step03_acceptance.py`` — owned engine process, dispatch seams,
  SIGKILL/restart, activation/switch (scenarios 2, 4, 5, 7);
* ``tests/test_session_runner.py`` + ``tests/_opencode_mock.py`` — the Step 04
  session submit/reconcile path over the deterministic mock server
  (scenario 3);
* ``tests/test_human_runtime_backup.py`` — a durable human approval recorded
  through the real owner-auth seams, backed up/verified/restored (scenario 6).

Run with::

    PYTHONPATH=src python3 -m unittest tests.test_step15_recovery -v
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
TESTS = Path(__file__).resolve().parent
for _path in (str(SRC), str(TESTS)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

# Existing acceptance helpers (imported, never modified).
import _opencode_mock  # noqa: E402
import test_cli_jobs as cli_jobs  # noqa: E402
import test_human_runtime_backup as hrb  # noqa: E402
import test_session_runner as sr  # noqa: E402
import test_step03_acceptance as step03  # noqa: E402

from review_article_agent.runtime.dispatch import RuntimeDispatchRepository  # noqa: E402
from review_article_agent.runtime.jobs import JobRepository  # noqa: E402
from review_article_agent.runtime.opencode import OpenCodeV2Client  # noqa: E402
from review_article_agent.storage import Registry, paths, schema  # noqa: E402
from review_article_agent.storage import ProjectStore  # noqa: E402
from review_article_agent.storage.agent_runtime import RuntimeRepository  # noqa: E402
from review_article_agent.storage.backup import (  # noqa: E402
    create_backup,
    restore_backup,
    verify_backup,
)
from review_article_agent.storage.human_approvals import (  # noqa: E402
    HumanApprovalRepository,
    integrity_problems as human_integrity_problems,
)


class Step15CliExitRecoveryTest(cli_jobs.JobCliTestCase):
    """AC3 (1): a CLI that exits after admission is observed by a new CLI."""

    def test_cli_exits_after_admission_and_a_new_cli_sees_completion(self) -> None:
        pid = self.create_project("A")
        self.select(pid)

        # The `job run` subprocess returns a durable *admission*, never a
        # completion, and then exits (each `self.cli` is its own process).
        out = self.run_job("recover-1", "--steps", "3", "--delay-ms", "0")
        self.assertTrue(out["submitted"])
        self.assertFalse(out["job_completion"])
        job_id = out["job"]["job_id"]
        self.assertIn(out["job"]["state"], ("queued", "admitted"))

        # A *separate* new CLI observes the detached run reach completion. A
        # receipt (admission) is not a completion.
        final = self.await_state(job_id, pid, lambda j: j["state"] == "completed")
        self.assertEqual(final["state"], "completed")
        self.assertIsNotNone(final["accepted_result"])
        # The run was never cancelled and the worker it started is still alive.
        self.assertNotEqual(final["state"], "cancelled")
        self.assertTrue(self.cli("--data-dir", self.state, "worker", "status")["worker"]["running"])


class Step15WorkerKillAndDedupTest(step03._AcceptanceBase):
    """AC3 (2,4,5) + AC4 (7): owned engine crash/duplicate/isolation matrix."""

    def test_sigkill_mid_job_then_restart_has_no_duplicate_result(self) -> None:
        # Kill the detached worker mid-job at the after-admission seam; a fresh
        # worker reconciles one accepted result under the *same* identity.
        observed = step03.SigkillRestartMatrixTest._run_case(
            self, "after_admission", "step15-mid"
        )
        self.assertEqual(observed["backend_after_recovery"], 1, observed)
        self.assertEqual(observed["attempts_total_after"], 1, observed)
        self.assertEqual(observed["accepted_fence"], 1, observed)

    def test_lost_events_reconcile_from_persisted_state(self) -> None:
        data_dir, _registry, info = self._fixture(
            name="state-lost", project="proj-lost"
        )
        first = self._start_worker(data_dir, pause_at="before_commit")
        first.wait_ready()
        submitted = self._submit(first, info.project_id, "lost-1")
        self.assertTrue(submitted["ok"], submitted)
        job_id = submitted["job"]["job_id"]
        self.assertTrue(first.wait_state("T", timeout=25.0), "never reached seam")

        before = self._disk_view(info, job_id)
        self.assertIsNone(before["accepted"], "no result may be committed at the seam")

        # Hard kill: no graceful stop, so any lifecycle/event log is lost.
        first.hard_kill()
        self.assertIsNotNone(first.proc.returncode)

        # A fresh, independent handle still reads the same persisted state.
        stale = self._disk_view(info, job_id)
        self.assertEqual(stale["job"].state, before["job"].state)
        self.assertIsNone(stale["accepted"])

        second = self._start_worker(data_dir)
        second.wait_ready()

        def _completed() -> bool:
            res = self._job_status(second, info.project_id, job_id)
            return res.get("ok") and res["job"]["state"] == "completed"

        self.assertTrue(self._wait_for(_completed, timeout=25.0), "no recovery")
        final = self._disk_view(info, job_id)
        self.assertIsNotNone(final["accepted"])
        self.assertEqual(final["attempt_count"], 1)
        self.assertEqual(step03._backend_dispatch_count(data_dir), 1)

        # Reading the recovered result again cannot mint a duplicate.
        again = self._disk_view(info, job_id)
        self.assertEqual(again["accepted"].fence, final["accepted"].fence)
        self.assertEqual(again["attempt_count"], 1)
        self.assertEqual(again["reserve_count"], 1)
        second.stop()

    def test_duplicate_admission_same_request_id_returns_same_job(self) -> None:
        data_dir, _registry, info = self._fixture(
            name="state-dedup", project="proj-dedup"
        )
        worker = self._start_worker(data_dir)
        worker.wait_ready()

        first = self._submit(worker, info.project_id, "dup-1", delay_ms=1500)
        second = self._submit(worker, info.project_id, "dup-1", delay_ms=1500)
        self.assertTrue(first["ok"] and second["ok"], (first, second))
        # A repeated admission with the same request id returns the same job.
        self.assertEqual(first["job"]["job_id"], second["job"]["job_id"])

        store = self._store_for(info)
        try:
            jobs = [job for job in JobRepository(store).list() if job.request_id == "dup-1"]
        finally:
            store.close()
        self.assertEqual(len(jobs), 1, "duplicate admission created a second job")
        worker.stop()

    def test_two_projects_isolated_no_concurrent_execution_safe_switch(self) -> None:
        data_dir = self.tmp / "state-iso"
        registry = Registry(data_dir)
        a = registry.create_project(self.tmp / "a", name="A")
        b = registry.create_project(self.tmp / "b", name="B")
        worker = self._start_worker(data_dir)
        worker.wait_ready()

        submitted = self._submit(worker, a.project_id, "iso-a", delay_ms=1500)
        self.assertTrue(submitted["ok"], submitted)
        job_a = submitted["job"]["job_id"]

        def _inflight() -> bool:
            res = self._job_status(worker, a.project_id, job_a)
            return res.get("ok") and res["job"]["state"] in ("admitted", "running")

        self.assertTrue(self._wait_for(_inflight, timeout=20.0), "A never in flight")

        # While A holds execution, B cannot become execution-active and cannot
        # run its own work; the two stores are isolated.
        blocked = self._submit(worker, b.project_id, "iso-b")
        self.assertFalse(blocked["ok"], blocked)
        self.assertIn(blocked["code"], {"wrong_project", "switch_blocked", "denied"})
        self.assertEqual(registry.activation().active_project_id, a.project_id)
        self.assertIsNone(self._disk_view(b, job_a)["job"], "B sees A's job")

        # The old project's in-flight remote commits exactly one accepted result,
        # in the OLD store only.
        def _accepted_a() -> bool:
            return self._disk_view(a, job_a)["accepted"] is not None

        self.assertTrue(self._wait_for(_accepted_a, timeout=25.0), "A never landed")
        before_switch = self._disk_view(a, job_a)
        self.assertEqual(before_switch["attempt_count"], 1)

        # Pause + drain + switch: the engine persists a pause, drains A, then
        # activates B.
        switched = worker.request(step03._req("switch", project_id=b.project_id))
        self.assertTrue(switched["ok"], switched)
        self.assertEqual(registry.activation().active_project_id, b.project_id)
        self.assertIsNone(self._disk_view(b, job_a)["accepted"], "A's result leaked into B")

        # B can now complete its own work; A's accepted result and attempt count
        # are unchanged (no job of the first commits after the switch).
        submitted_b = self._submit(worker, b.project_id, "iso-b2")
        self.assertTrue(submitted_b["ok"], submitted_b)
        job_b = submitted_b["job"]["job_id"]

        def _completed_b() -> bool:
            res = self._job_status(worker, b.project_id, job_b)
            return res.get("ok") and res["job"]["state"] == "completed"

        self.assertTrue(self._wait_for(_completed_b, timeout=25.0), "B never completed")
        self.assertIsNotNone(self._disk_view(b, job_b)["accepted"])

        after_switch = self._disk_view(a, job_a)
        self.assertEqual(after_switch["attempt_count"], before_switch["attempt_count"])
        self.assertEqual(after_switch["accepted"].fence, before_switch["accepted"].fence)
        worker.stop()


class Step15SessionReconcileRecoveryTest(sr.SessionRunnerTestBase):
    """AC3 (3): harness/model-session interruption over the Step 04 mock.

    The reconcile path runs against a **real** :class:`OpenCodeV2Client` talking
    to the deterministic loopback ``tests/_opencode_mock.py`` server (no live
    service, no paid call). An interrupted turn reconciles without a duplicate
    accepted result; an ambiguous (errored, no authoritative idle) turn stays
    ``unknown`` with its reserve held.
    """

    def setUp(self) -> None:
        super().setUp()
        self.mock = _opencode_mock.MockServer(token="test-token")
        self.mock.__enter__()
        self.addCleanup(self.mock.__exit__, None, None, None)
        self.real_client = OpenCodeV2Client(
            self.mock.base_url, token="test-token", directory=self.location.directory
        )

    def _accepted_proposals(self):
        return RuntimeRepository(self.store).list_proposals()

    def test_interrupted_turn_reconciles_without_duplicate_result(self) -> None:
        scenario = self._scenario(request_id="rec-int", call_id="call-int")
        receipt = self._submit(scenario, client=self.real_client)
        self.assertEqual(receipt.state, "admitted")

        # Harness interruption: the owned turn is torn down with an authoritative
        # interrupted idle and no accepted model result.
        self.mock.state.append_idle(receipt.session_id, "interrupted")
        outcome = self._reconcile(scenario, client=self.real_client)
        self.assertEqual(outcome.state, "interrupted")
        self.assertIsNone(outcome.proposal_record_id)
        self.assertEqual(self._accepted_proposals(), [], "an interruption became a result")

        dispatch = RuntimeDispatchRepository(self.store).get_dispatch(
            scenario["dispatch"].dispatch_id
        )
        self.assertEqual(dispatch.state, "failed")
        self.assertIsNone(dispatch.result_ref)

        # A repeated reconcile is a no-op and still accepts nothing.
        again = self._reconcile(scenario, client=self.real_client)
        self.assertTrue(again.duplicate)
        self.assertEqual(self._accepted_proposals(), [])

    def test_ambiguous_dispatch_stays_unknown_with_spend_documented(self) -> None:
        scenario = self._scenario(request_id="rec-amb", call_id="call-amb")
        receipt = self._submit(scenario, client=self.real_client)
        self.assertEqual(receipt.state, "admitted")

        # Observed restart shape: a partial assistant with finish="error" and no
        # authoritative idle is an explicit unknown, never a success.
        self.mock.state.append_assistant(receipt.session_id, "", finish="error")
        outcome = self._reconcile(scenario, client=self.real_client)
        self.assertEqual(outcome.state, "unknown")
        self.assertTrue(outcome.reconciliation_required)
        self.assertIsNone(outcome.proposal_record_id)
        self.assertEqual(self._accepted_proposals(), [])

        # The ambiguous external spend stays documented: reserve held, cost not
        # known, no accepted result, no new dispatch identity.
        dispatch = RuntimeDispatchRepository(self.store).get_dispatch(
            scenario["dispatch"].dispatch_id
        )
        self.assertNotEqual(dispatch.state, "completed")
        self.assertIsNone(dispatch.result_ref)
        self.assertEqual(dispatch.reserve_usd, "0.10")
        self.assertNotEqual(dispatch.cost_state, "known")


class Step15ApprovalBackupRecoveryTest(hrb.HumanRuntimeBackupBase):
    """AC3 (6): a recorded human approval survives backup -> verify -> restore."""

    def test_human_approval_survives_backup_verify_restore(self) -> None:
        store = self.new_store()
        self.seed_chain(store)
        self.install()  # patches only the owner-auth low-level terminal seams
        prepared = self.prepare(store)
        recorded = self.record_approval(store, prepared)
        self.assertEqual(
            HumanApprovalRepository(store).evaluate(recorded.approval_id).status,
            "active",
        )

        backup_dir = self.tmp / "backup"
        create_backup(store, backup_dir)
        report = verify_backup(backup_dir)
        self.assertTrue(report["ok"], report)
        self.assertEqual(report["store_schema_version"], schema.STORE_SCHEMA_VERSION)

        # Restore into a fresh registry root (no reuse of the source root).
        registry = Registry(self.tmp / "state2")
        target = self.tmp / "restored"
        target.mkdir()
        info = restore_backup(backup_dir, target, registry)
        self.assertEqual(info.project_id, store.project_id)

        restored = ProjectStore.open_at(
            paths.app_dir_for(str(target)), expected_project_id=info.project_id
        )
        self.addCleanup(restored.close)
        restored_approvals = HumanApprovalRepository(restored)
        # The scientific approval is not lost, and its owner evidence revalidates.
        self.assertEqual(
            restored_approvals.evaluate(recorded.approval_id).status, "active"
        )
        self.assertTrue(restored_approvals.verify_snapshot(recorded.approval_id).ok)
        self.assertEqual(
            human_integrity_problems(restored.connection, info.project_id), []
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
