"""Durable synthetic job/attempt persistence tests (Step 03 core).

Covers the storage-side acceptance for ``runtime/jobs.py``:

* idempotent submit and divergent-request conflict;
* closed/reopened recovery and one accepted local result;
* a stale fence can never accept a newer result;
* paused/cancelled jobs retain an unresolved (ambiguous) remote outcome;
* bounded safe retries and no automatic retry after an ambiguous outcome;
* exact-decimal budget caps, reserve retention, and invalid values;
* migration 3 -> 4 preserving prior artifacts and refusing a newer schema.
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from review_article_agent.domain.errors import UnsupportedSchemaVersion  # noqa: E402
from review_article_agent.runtime.jobs import (  # noqa: E402
    MAX_SAFE_RETRIES,
    AmbiguousOutcomeError,
    BudgetExceededError,
    JobConflictError,
    JobRepository,
    JobStateError,
    JobValidationError,
    RetryLimitExceeded,
    StaleFenceError,
)
from review_article_agent.storage import ProjectStore, paths, schema  # noqa: E402


class JobTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="raa-jobs-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.app_dir = self.tmp / ".review_article_agent"
        self.store = ProjectStore.create_at(str(self.app_dir), self.project_id())
        self.addCleanup(self.store.close)
        self.jobs = JobRepository(self.store)

    @staticmethod
    def project_id() -> str:
        return paths.new_project_id()

    def reopen(self) -> None:
        pid = self.store.project_id
        self.store.close()
        self.store = ProjectStore.open_at(
            str(self.app_dir), expected_project_id=pid
        )
        self.jobs = JobRepository(self.store)


class SubmitTest(JobTestBase):
    def test_submit_is_idempotent_for_same_request(self) -> None:
        first = self.jobs.submit("req-1", task_kind="probe", parameters={"n": 1})
        second = self.jobs.submit("req-1", task_kind="probe", parameters={"n": 1})
        self.assertEqual(first.job_id, second.job_id)
        self.assertEqual(len(self.jobs.list()), 1)
        self.assertEqual(first.state, "queued")
        self.assertFalse(first.outcome_unresolved)

    def test_submit_divergent_payload_conflicts(self) -> None:
        self.jobs.submit("req-1", task_kind="probe", parameters={"n": 1})
        with self.assertRaises(JobConflictError):
            self.jobs.submit("req-1", task_kind="probe", parameters={"n": 2})

    def test_submit_distinct_requests_make_distinct_jobs(self) -> None:
        a = self.jobs.submit("req-a", task_kind="probe")
        b = self.jobs.submit("req-b", task_kind="probe")
        self.assertNotEqual(a.job_id, b.job_id)
        self.assertEqual(len(self.jobs.list()), 2)


class RecoveryTest(JobTestBase):
    def test_reopen_preserves_job_and_attempt_and_accepts_one_result(self) -> None:
        job = self.jobs.submit("req-1", task_kind="probe", parameters={"n": 7})
        attempt = self.jobs.begin_attempt(
            job.job_id, dispatch_kind="external_synthetic", stage="s1",
            reserve_usd="0.10",
        )
        self.jobs.mark_admitted(attempt.attempt_id, admission_id="adm-1")
        self.jobs.record_checkpoint(
            attempt.attempt_id, {"local_result": {"answer": 42}}
        )
        self.reopen()

        recovered = self.jobs.get(job.job_id)
        assert recovered is not None
        self.assertEqual(recovered.state, "admitted")
        self.assertEqual(recovered.current_attempt_id, attempt.attempt_id)

        outcome = self.jobs.recover(attempt.attempt_id)
        self.assertEqual(outcome.disposition, "accept_pending_local_result")
        self.assertTrue(self.jobs.get(job.job_id).outcome_unresolved)

        resolved = self.jobs.reconcile(
            job.job_id,
            resolution="accepted_local_result",
            result_kind="synthetic",
            result={"answer": 42},
        )
        self.assertEqual(resolved.state, "completed")
        self.assertFalse(resolved.outcome_unresolved)

        accepted = self.jobs.accepted_result(job.job_id)
        assert accepted is not None
        self.assertEqual(accepted.result, {"answer": 42})
        self.assertEqual(len(self.jobs.attempts(job.job_id)), 1)

    def test_stale_fence_cannot_accept(self) -> None:
        job = self.jobs.submit("req-1", task_kind="probe")
        first = self.jobs.begin_attempt(job.job_id, dispatch_kind="local")
        self.jobs.mark_definitive_failure(first.attempt_id)
        second = self.jobs.begin_attempt(
            job.job_id, dispatch_kind="local", retry_authorized_by="owner"
        )
        self.assertEqual(second.fence, first.fence + 1)
        with self.assertRaises(StaleFenceError):
            self.jobs.accept_result(
                first.attempt_id, result_kind="synthetic", result={"stale": True}
            )
        accepted = self.jobs.accept_result(
            second.attempt_id, result_kind="synthetic", result={"fresh": True}
        )
        self.assertEqual(accepted.fence, second.fence)
        self.assertEqual(self.jobs.get(job.job_id).state, "completed")

    def test_exactly_one_local_accepted_result(self) -> None:
        job = self.jobs.submit("req-1", task_kind="probe")
        attempt = self.jobs.begin_attempt(job.job_id, dispatch_kind="local")
        first = self.jobs.accept_result(
            attempt.attempt_id, result_kind="synthetic", result={"ok": True}
        )
        again = self.jobs.accept_result(
            attempt.attempt_id, result_kind="synthetic", result={"ok": True}
        )
        self.assertEqual(first.result_digest, again.result_digest)
        rows = self.store.connection.execute(
            "SELECT COUNT(*) FROM job_accepted_result WHERE project_id = ?",
            (self.store.project_id,),
        ).fetchone()
        self.assertEqual(rows[0], 1)
        with self.assertRaises(JobConflictError):
            self.jobs.accept_result(
                attempt.attempt_id, result_kind="synthetic", result={"ok": False}
            )


class AmbiguityTest(JobTestBase):
    def _ambiguous_job(self):
        job = self.jobs.submit("req-1", task_kind="probe")
        attempt = self.jobs.begin_attempt(
            job.job_id, dispatch_kind="external_synthetic", stage="s1",
            reserve_usd="0.50",
        )
        self.jobs.mark_admitted(attempt.attempt_id, admission_id="adm-1")
        self.jobs.mark_ambiguous(attempt.attempt_id)
        return job, attempt

    def test_ambiguous_outcome_blocks_automatic_retry(self) -> None:
        job, attempt = self._ambiguous_job()
        self.assertTrue(self.jobs.get(job.job_id).outcome_unresolved)
        self.assertEqual(
            self.jobs.get(job.job_id).state, "reconciliation_required"
        )
        with self.assertRaises(AmbiguousOutcomeError):
            self.jobs.begin_attempt(
                job.job_id, dispatch_kind="local", retry_authorized_by="owner"
            )

    def test_pause_and_cancel_retain_unresolved(self) -> None:
        job, _ = self._ambiguous_job()

        paused = self.jobs.pause(job.job_id)
        self.assertEqual(paused.state, "paused")
        self.assertTrue(paused.outcome_unresolved)
        # A paused job does not schedule new work.
        with self.assertRaises(JobStateError):
            self.jobs.begin_attempt(
                job.job_id, dispatch_kind="local", retry_authorized_by="owner"
            )

        resumed = self.jobs.resume(job.job_id)
        self.assertEqual(resumed.state, "reconciliation_required")
        self.assertTrue(resumed.outcome_unresolved)

        cancelled = self.jobs.cancel(job.job_id)
        self.assertEqual(cancelled.state, "cancelled")
        self.assertTrue(cancelled.outcome_unresolved)

    def test_human_escalation_is_available_instead_of_retry(self) -> None:
        job, _ = self._ambiguous_job()
        escalated = self.jobs.reconcile(job.job_id, resolution="human_escalation")
        self.assertEqual(escalated.state, "waiting_human")
        self.assertTrue(escalated.outcome_unresolved)


class RetryTest(JobTestBase):
    def test_retry_bound_and_authorization(self) -> None:
        job = self.jobs.submit("req-1", task_kind="probe")
        attempt = self.jobs.begin_attempt(job.job_id, dispatch_kind="local")
        self.jobs.mark_definitive_failure(attempt.attempt_id)
        with self.assertRaises(JobStateError):
            self.jobs.begin_attempt(job.job_id, dispatch_kind="local")

        fences = []
        for _ in range(MAX_SAFE_RETRIES):
            retry = self.jobs.begin_attempt(
                job.job_id, dispatch_kind="local", retry_authorized_by="owner"
            )
            fences.append(retry.fence)
            self.jobs.mark_definitive_failure(retry.attempt_id)
        self.assertEqual(fences, [2, 3])
        with self.assertRaises(RetryLimitExceeded):
            self.jobs.begin_attempt(
                job.job_id, dispatch_kind="local", retry_authorized_by="owner"
            )

    def test_admitted_interrupted_requires_reconciliation_not_retry(self) -> None:
        job = self.jobs.submit("req-1", task_kind="probe")
        attempt = self.jobs.begin_attempt(
            job.job_id, dispatch_kind="external_synthetic", stage="s1",
            reserve_usd="0.10",
        )
        self.jobs.mark_admitted(attempt.attempt_id, admission_id="adm-1")
        self.jobs.mark_running(attempt.attempt_id)
        outcome = self.jobs.recover(attempt.attempt_id)
        self.assertEqual(outcome.disposition, "reconciliation_required")
        self.assertTrue(self.jobs.get(job.job_id).outcome_unresolved)
        with self.assertRaises(AmbiguousOutcomeError):
            self.jobs.begin_attempt(
                job.job_id, dispatch_kind="local", retry_authorized_by="owner"
            )

    def test_predispatch_recovery_is_safe(self) -> None:
        job = self.jobs.submit("req-1", task_kind="probe")
        attempt = self.jobs.begin_attempt(job.job_id, dispatch_kind="local")
        outcome = self.jobs.recover(attempt.attempt_id)
        self.assertEqual(outcome.disposition, "retry_safe")
        self.assertEqual(self.jobs.get(job.job_id).state, "interrupted")
        self.assertFalse(self.jobs.get(job.job_id).outcome_unresolved)
        retry = self.jobs.begin_attempt(
            job.job_id, dispatch_kind="local", retry_authorized_by="owner"
        )
        self.assertEqual(retry.fence, 2)


class BudgetTest(JobTestBase):
    def _external(self, job, *, reserve, stage="s1"):
        return self.jobs.begin_attempt(
            job.job_id,
            dispatch_kind="external_synthetic",
            stage=stage,
            reserve_usd=reserve,
        )

    def test_default_caps_and_reserve_retention(self) -> None:
        self.assertEqual(self.jobs.budget_policy(), ("25", "10"))
        job = self.jobs.submit("req-1", task_kind="probe")
        attempt = self._external(job, reserve="10")
        self.assertEqual(
            self.jobs.budget_usage(stage="s1").stage_reserved_usd, "10"
        )

        # An unknown cost retains its reserve: a further stage reservation is
        # still refused by the stage cap.
        self.jobs.settle_cost(attempt.attempt_id, cost_state="unknown")
        self.assertEqual(
            self.jobs.budget_usage(stage="s1").stage_reserved_usd, "10"
        )
        job2 = self.jobs.submit("req-2", task_kind="probe")
        with self.assertRaises(BudgetExceededError):
            self._external(job2, reserve="0.01")

    def test_estimated_settlement_keeps_full_reserve(self) -> None:
        job = self.jobs.submit("req-1", task_kind="probe")
        attempt = self._external(job, reserve="1")
        settled = self.jobs.settle_cost(
            attempt.attempt_id, cost_state="estimated", amount_usd="0.01"
        )
        # The estimate is recorded on the attempt, but the held reserve bound is
        # retained (counted conservatively) and never shrunk to the estimate.
        self.assertEqual(settled.cost_amount_usd, "0.01")
        self.assertEqual(settled.cost_state, "estimated")
        self.assertEqual(
            self.jobs.budget_usage(stage="s1").stage_reserved_usd, "1"
        )
        row = self.store.connection.execute(
            "SELECT amount_usd, state FROM job_budget_reserve WHERE project_id = ?",
            (self.store.project_id,),
        ).fetchone()
        self.assertEqual(row["amount_usd"], "1")
        self.assertEqual(row["state"], "retained")

        # A later known settlement still must not silently shrink the held bound.
        self.jobs.settle_cost(
            attempt.attempt_id, cost_state="known", amount_usd="0.5"
        )
        self.assertEqual(
            self.jobs.budget_usage(stage="s1").stage_reserved_usd, "1"
        )

    def test_unknown_cost_records_no_amount(self) -> None:
        job = self.jobs.submit("req-1", task_kind="probe")
        attempt = self._external(job, reserve="2")
        settled = self.jobs.settle_cost(attempt.attempt_id, cost_state="unknown")
        self.assertIsNone(settled.cost_amount_usd)
        self.assertEqual(settled.cost_state, "unknown")
        self.assertEqual(
            self.jobs.budget_usage(stage="s1").stage_reserved_usd, "2"
        )

    def test_project_cap_refuses_further_dispatch(self) -> None:
        for index, (stage, reserve) in enumerate(
            [("s1", "10"), ("s2", "10"), ("s3", "5")]
        ):
            job = self.jobs.submit(f"req-{index}", task_kind="probe")
            self._external(job, reserve=reserve, stage=stage)
        self.assertEqual(self.jobs.budget_usage().project_reserved_usd, "25")
        self.assertEqual(self.jobs.budget_usage().project_remaining_usd, "0")
        job = self.jobs.submit("req-x", task_kind="probe")
        with self.assertRaises(BudgetExceededError):
            self._external(job, reserve="3", stage="s4")

    def test_invalid_budget_values_are_rejected(self) -> None:
        job = self.jobs.submit("req-1", task_kind="probe")
        for bad in ("-1", "abc", 1.0, 0, "1e999999999", "1e-999999999"):
            with self.assertRaises(JobValidationError):
                self._external(job, reserve=bad)  # type: ignore[arg-type]
        with self.assertRaises(JobValidationError):
            self.jobs.set_budget_policy(project_cap_usd="0", stage_cap_usd="10")
        with self.assertRaises(JobValidationError):
            self.jobs.set_budget_policy(project_cap_usd="-5", stage_cap_usd="10")
        with self.assertRaises(JobValidationError):
            self.jobs.set_budget_policy(project_cap_usd="x", stage_cap_usd="10")

        attempt = self._external(job, reserve="1")
        with self.assertRaises(JobValidationError):
            self.jobs.settle_cost(attempt.attempt_id, cost_state="known")
        with self.assertRaises(JobValidationError):
            self.jobs.settle_cost(
                attempt.attempt_id, cost_state="unknown", amount_usd="1"
            )

    def test_external_dispatch_requires_reserve_and_stage(self) -> None:
        job = self.jobs.submit("req-1", task_kind="probe")
        with self.assertRaises(JobValidationError):
            self.jobs.begin_attempt(job.job_id, dispatch_kind="external_synthetic")
        with self.assertRaises(JobValidationError):
            self.jobs.begin_attempt(
                job.job_id, dispatch_kind="external_synthetic", stage="s1"
            )
        with self.assertRaises(JobValidationError):
            self.jobs.begin_attempt(
                job.job_id, dispatch_kind="local", reserve_usd="1"
            )


class RepairRegressionTest(JobTestBase):
    def test_external_missing_receipt_recovery_is_unresolved(self) -> None:
        # An external attempt with no local admission receipt must NOT be treated
        # as retry_safe: the request may have reached the backend already.
        job = self.jobs.submit("req-1", task_kind="probe")
        attempt = self.jobs.begin_attempt(
            job.job_id,
            dispatch_kind="external_synthetic",
            stage="s1",
            reserve_usd="0.10",
        )
        outcome = self.jobs.recover(attempt.attempt_id)
        self.assertEqual(outcome.disposition, "reconciliation_required")
        final = self.jobs.get(job.job_id)
        self.assertTrue(final.outcome_unresolved)
        self.assertEqual(final.state, "reconciliation_required")
        # The reserve is retained.
        self.assertEqual(
            self.jobs.budget_usage(stage="s1").stage_reserved_usd, "0.10"
        )
        with self.assertRaises(AmbiguousOutcomeError):
            self.jobs.begin_attempt(
                job.job_id, dispatch_kind="local", retry_authorized_by="owner"
            )

    def test_local_missing_result_recovery_is_safe(self) -> None:
        job = self.jobs.submit("req-1", task_kind="probe")
        attempt = self.jobs.begin_attempt(job.job_id, dispatch_kind="local")
        outcome = self.jobs.recover(attempt.attempt_id)
        self.assertEqual(outcome.disposition, "retry_safe")
        self.assertFalse(self.jobs.get(job.job_id).outcome_unresolved)

    def test_cancelled_never_completed_by_acceptance(self) -> None:
        job = self.jobs.submit("req-1", task_kind="probe")
        attempt = self.jobs.begin_attempt(job.job_id, dispatch_kind="local")
        self.jobs.cancel(job.job_id)
        accepted = self.jobs.accept_result(
            attempt.attempt_id, result_kind="synthetic", result={"late": True}
        )
        self.assertEqual(accepted.result, {"late": True})
        final = self.jobs.get(job.job_id)
        self.assertEqual(final.state, "cancelled")
        self.assertNotEqual(final.state, "completed")
        # The remote unresolved bit may be resolved while scheduling stays cancelled.
        self.assertFalse(final.outcome_unresolved)

    def test_pause_survives_late_admission_and_recovery(self) -> None:
        job = self.jobs.submit("req-1", task_kind="probe")
        attempt = self.jobs.begin_attempt(job.job_id, dispatch_kind="local")
        self.jobs.pause(job.job_id)
        self.jobs.mark_admitted(attempt.attempt_id, admission_id="adm-1")
        self.assertEqual(self.jobs.get(job.job_id).state, "paused")
        self.jobs.mark_running(attempt.attempt_id)
        self.assertEqual(self.jobs.get(job.job_id).state, "paused")
        self.jobs.recover(attempt.attempt_id)
        self.assertEqual(self.jobs.get(job.job_id).state, "paused")

    def test_stale_state_mutators_refuse(self) -> None:
        job = self.jobs.submit("req-1", task_kind="probe")
        first = self.jobs.begin_attempt(job.job_id, dispatch_kind="local")
        self.jobs.mark_definitive_failure(first.attempt_id)
        second = self.jobs.begin_attempt(
            job.job_id, dispatch_kind="local", retry_authorized_by="owner"
        )
        stale = first.attempt_id
        with self.assertRaises(StaleFenceError):
            self.jobs.mark_admitted(stale, admission_id="adm-x")
        with self.assertRaises(StaleFenceError):
            self.jobs.mark_running(stale)
        with self.assertRaises(StaleFenceError):
            self.jobs.record_checkpoint(stale, {"x": 1})
        with self.assertRaises(StaleFenceError):
            self.jobs.mark_ambiguous(stale)
        with self.assertRaises(StaleFenceError):
            self.jobs.mark_definitive_failure(stale)
        with self.assertRaises(StaleFenceError):
            self.jobs.recover(stale)
        with self.assertRaises(StaleFenceError):
            self.jobs.accept_result(stale, result_kind="synthetic", result={})
        # The current attempt is untouched and still usable.
        self.assertEqual(self.jobs.attempt(second.attempt_id).state, "queued")

    def test_mark_ambiguous_cannot_regress_completed(self) -> None:
        job = self.jobs.submit("req-1", task_kind="probe")
        attempt = self.jobs.begin_attempt(job.job_id, dispatch_kind="local")
        self.jobs.accept_result(
            attempt.attempt_id, result_kind="synthetic", result={"ok": True}
        )
        self.assertEqual(self.jobs.get(job.job_id).state, "completed")
        with self.assertRaises(JobStateError):
            self.jobs.mark_ambiguous(attempt.attempt_id)
        self.assertEqual(self.jobs.get(job.job_id).state, "completed")

    def test_reconcile_definitive_failure_from_ambiguous(self) -> None:
        job = self.jobs.submit("req-1", task_kind="probe")
        attempt = self.jobs.begin_attempt(
            job.job_id,
            dispatch_kind="external_synthetic",
            stage="s1",
            reserve_usd="0.10",
        )
        self.jobs.mark_ambiguous(attempt.attempt_id)
        self.assertTrue(self.jobs.get(job.job_id).outcome_unresolved)
        resolved = self.jobs.reconcile(job.job_id, resolution="definitive_failure")
        self.assertEqual(resolved.state, "failed")
        self.assertFalse(resolved.outcome_unresolved)
        self.assertEqual(
            self.jobs.attempt(attempt.attempt_id).state, "definitive_failure"
        )


class ConcurrencyTest(JobTestBase):
    def _race(self, operation):
        import threading

        pid = self.store.project_id
        app_dir = str(self.app_dir)
        barrier = threading.Barrier(2)
        outcomes: list[tuple[str, object]] = []
        lock = threading.Lock()

        def worker() -> None:
            store = ProjectStore.open_at(app_dir, expected_project_id=pid)
            try:
                barrier.wait(timeout=10)
                value = operation(JobRepository(store), store)
                with lock:
                    outcomes.append(("ok", value))
            except BaseException as exc:  # noqa: BLE001 - recorded for assertion
                with lock:
                    outcomes.append(("err", exc))
            finally:
                store.close()

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        self.assertFalse(any(t.is_alive() for t in threads), "race deadlocked")
        return outcomes

    def test_two_connections_racing_submit_yield_one_job(self) -> None:
        outcomes = self._race(
            lambda repo, store: repo.submit("req-race", task_kind="probe", parameters={"n": 1})
        )
        errors = [v for status, v in outcomes if status == "err"]
        self.assertEqual(errors, [])
        job_ids = {v.job_id for status, v in outcomes if status == "ok"}
        self.assertEqual(len(job_ids), 1)
        count = self.store.connection.execute(
            "SELECT COUNT(*) FROM job WHERE project_id = ?",
            (self.store.project_id,),
        ).fetchone()[0]
        self.assertEqual(count, 1)

    def test_two_connections_racing_begin_yield_one_attempt(self) -> None:
        job = self.jobs.submit("req-1", task_kind="probe")
        outcomes = self._race(
            lambda repo, store: repo.begin_attempt(
                job.job_id,
                dispatch_kind="external_synthetic",
                stage="s1",
                reserve_usd="0.10",
            )
        )
        started = [v for status, v in outcomes if status == "ok"]
        failed = [v for status, v in outcomes if status == "err"]
        self.assertEqual(len(started), 1)
        self.assertEqual(len(failed), 1)
        self.assertIsInstance(failed[0], JobStateError)
        count = self.store.connection.execute(
            "SELECT COUNT(*) FROM job_attempt WHERE project_id = ?",
            (self.store.project_id,),
        ).fetchone()[0]
        self.assertEqual(count, 1)
        # No double reserve: exactly one reserve row, counted once.
        reserve_count = self.store.connection.execute(
            "SELECT COUNT(*) FROM job_budget_reserve WHERE project_id = ?",
            (self.store.project_id,),
        ).fetchone()[0]
        self.assertEqual(reserve_count, 1)
        self.assertEqual(
            self.jobs.budget_usage(stage="s1").stage_reserved_usd, "0.10"
        )


class MigrationTest(JobTestBase):
    def test_migration3_to_4_preserves_artifacts_and_refuses_newer(self) -> None:
        pid = self.project_id()
        db_path = str(self.tmp / "m3.sqlite3")
        conn: sqlite3.Connection = schema.open_connection(db_path)
        try:
            schema.migrate(
                conn,
                target_version=3,
                migrations=schema.STORE_MIGRATIONS,
                label="project store",
            )
            self.assertEqual(schema.read_user_version(conn), 3)
            conn.execute(
                "INSERT INTO project (project_id, name, created_at_utc, app_version) "
                "VALUES (?, 'M3', '2026-01-01T00:00:00+00:00', '0.0.0')",
                (pid,),
            )
            conn.execute(
                "INSERT INTO artifact (project_id, artifact_id, kind, "
                " created_at_utc, created_by, label, status) "
                "VALUES (?, 'art-1', 'document', '2026-01-01T00:00:00+00:00', "
                " 'tester', NULL, 'active')",
                (pid,),
            )

            schema.migrate(
                conn,
                target_version=4,
                migrations=schema.STORE_MIGRATIONS,
                label="project store",
            )
            self.assertEqual(schema.read_user_version(conn), 4)
            names = {
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            self.assertTrue(
                {"job", "job_attempt", "job_accepted_result",
                 "job_budget_policy", "job_budget_reserve"} <= names
            )
            # Prior rows survive the upgrade untouched.
            rows = conn.execute(
                "SELECT artifact_id FROM artifact WHERE project_id = ?", (pid,)
            ).fetchall()
            self.assertEqual([r[0] for r in rows], ["art-1"])

            conn.execute("PRAGMA user_version = 5")
            with self.assertRaises(UnsupportedSchemaVersion):
                schema.migrate(
                    conn,
                    target_version=4,
                    migrations=schema.STORE_MIGRATIONS,
                    label="project store",
                )
        finally:
            conn.close()

    def test_open_refuses_newer_schema(self) -> None:
        pid = self.store.project_id
        self.store.close()
        conn = schema.open_connection(paths.store_path_for(str(self.tmp)))
        try:
            conn.execute("PRAGMA user_version = 5")
        finally:
            conn.close()
        with self.assertRaises(UnsupportedSchemaVersion):
            ProjectStore.open_at(str(self.app_dir), expected_project_id=pid)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
