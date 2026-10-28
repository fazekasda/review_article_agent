"""Standalone worker engine tests (Step 03, task T4).

Covers ``docs/implementation/03-durable-worker.md`` deliverables/ACs and
``docs/contracts/02-gates-and-runtime.md`` §5.6/§6.3/§8 against the integrated
engine (``review_article_agent.worker.engine``):

* strict request validation (version / op whitelist / known keys / no trusted
  ``uid``/``actor`` field) and curated static error results (never a raw trace);
* the T5a launcher contract: ``status`` and ``stop`` over a real UDS server, and
  the detached ``python -m review_article_agent.worker`` entry point;
* one numeric completion with exactly one accepted result;
* an ambiguous outcome is never resent under a new identity;
* pause / budget cap stop new dispatch (and are visible, not a false success);
* a waiting-human gate does not busy-loop;
* a safe switch quiesces the old project before another project executes.

The comprehensive SIGKILL restart matrix is a separate task by design; this file
uses the in-process fault seams and the test-only SIGSTOP gate only where the
seam is exercised.
"""

from __future__ import annotations

import json
import os
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from review_article_agent.domain.errors import StoreError  # noqa: E402
from review_article_agent.runtime.ipc import WorkerIPCClient, WorkerIPCServer  # noqa: E402
from review_article_agent.runtime.jobs import JobStateError  # noqa: E402
from review_article_agent.storage import Registry, paths  # noqa: E402
from review_article_agent.worker.engine import (  # noqa: E402
    APP_NAME,
    PROTOCOL_VERSION,
    SYNTHETIC_NUMERIC_TASK,
    SYNTHETIC_WAITING_HUMAN_TASK,
    WorkerEngine,
)
from review_article_agent.worker.synthetic import SyntheticBackend  # noqa: E402


def _req(op: str, **kw: object) -> dict:
    return {"version": PROTOCOL_VERSION, "op": op, **kw}


class EngineTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="raa-engine-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.data_dir = self.tmp / "state"
        self.registry = Registry(self.data_dir)
        self.project = self.registry.create_project(self.tmp / "a", name="A")
        self.engine = WorkerEngine(str(self.data_dir), registry=self.registry)
        self.addCleanup(self.engine.close)

    def _activate(self) -> None:
        self.registry.activate_execution(self.project.project_id)
        # Engine re-reads activation lazily; refresh its cached activation.
        self.engine.handle(_req("switch", project_id=self.project.project_id))

    def _drain_engine(self, max_ticks: int = 2000) -> None:
        """Run the scheduler until it reports no pending work (bounded)."""
        for _ in range(max_ticks):
            timeout = self.engine.tick()
            if timeout is None:
                return
            if timeout > 0:
                time.sleep(min(timeout, 0.02))
        self.fail("engine did not settle within the tick budget")

    def _submit_numeric(self, request_id: str, **kw: object) -> dict:
        params = {
            "request_id": request_id,
            "task_kind": SYNTHETIC_NUMERIC_TASK,
            "steps": kw.pop("steps", 10),
            "delay_ms": kw.pop("delay_ms", 0),
            "outcome": kw.pop("outcome", "complete"),
            "stage": kw.pop("stage", "stage-1"),
            "reserve_usd": kw.pop("reserve_usd", "0.10"),
        }
        params.update(kw)
        return self.engine.handle(_req("submit", project_id=self.project.project_id, **params))

    @contextmanager
    def _repo(self, project_id: str | None = None):
        pid = project_id or self.project.project_id
        with self.engine.open_repository(pid) as repo:
            yield repo

    def _new_engine(self) -> WorkerEngine:
        engine = WorkerEngine(str(self.data_dir))
        self.addCleanup(engine.close)
        return engine

    def _drive(self, engine: WorkerEngine, max_ticks: int = 200) -> None:
        for _ in range(max_ticks):
            hint = engine.tick()
            if hint is None:
                return
            if hint > 0:
                time.sleep(min(hint, 0.02))
        self.fail("engine did not settle within the tick budget")

    @staticmethod
    def _params(steps: int = 7, delay_ms: int = 0, outcome: str = "complete",
                stage: str = "s", reserve: str = "0.10") -> dict:
        return {
            "steps": steps,
            "delay_ms": delay_ms,
            "outcome": outcome,
            "stage": stage,
            "reserve_usd": reserve,
        }

    def _simulate_dispatch(self, *, request_id: str, admitted: bool, **params: object) -> tuple[str, str]:
        """Persist a crash-like attempt state directly in the store.

        Returns ``(job_id, attempt_id)``. With ``admitted=True`` the provider
        journal is written (admission happened) but no local receipt is recorded
        — simulating a crash between external admission and the local commit.
        With ``admitted=False`` only the continuity checkpoint is persisted —
        simulating a crash just before dispatch.
        """
        self.registry.activate_execution(self.project.project_id)
        with self._repo() as repo:
            job = repo.submit(
                request_id, task_kind=SYNTHETIC_NUMERIC_TASK,
                parameters=self._params(**params),
            )
            attempt = repo.begin_attempt(
                job.job_id, dispatch_kind="external_synthetic",
                stage="s", reserve_usd=str(params.get("reserve", "0.10")),
            )
            continuity = SyntheticBackend(str(self.data_dir)).initialize()
            repo.record_checkpoint(
                attempt.attempt_id,
                {"continuity_id": continuity, "phase": "predispatch"},
            )
            if admitted:
                SyntheticBackend(
                    str(self.data_dir), expected_continuity_id=continuity
                ).admit(
                    attempt.attempt_id,
                    self.project.project_id,
                    steps=int(params.get("steps", 7)),
                    delay_ms=int(params.get("delay_ms", 0)),
                    outcome=str(params.get("outcome", "complete")),
                )
            return job.job_id, attempt.attempt_id

    def _dispatch_count(self) -> int:
        """Count provider dispatch rows in the synthetic journal (0 if absent)."""
        path = os.path.join(str(self.data_dir), "runtime", "synthetic.sqlite3")
        if not os.path.exists(path):
            return 0
        conn = sqlite3.connect(path)
        try:
            return int(conn.execute("SELECT COUNT(*) FROM dispatch").fetchone()[0])
        finally:
            conn.close()


class ValidationTest(EngineTestBase):
    def test_status_and_unknown_op_and_version(self) -> None:
        self._activate()
        ok = self.engine.handle(_req("status"))
        self.assertTrue(ok["ok"])
        self.assertEqual(ok["app"], APP_NAME)
        self.assertEqual(ok["protocol_version"], PROTOCOL_VERSION)
        self.assertEqual(ok["pid"], os.getpid())
        self.assertTrue(ok["state"])

        bad_version = self.engine.handle({"version": 2, "op": "status"})
        self.assertFalse(bad_version["ok"])
        self.assertEqual(bad_version["code"], "unsupported_version")

        unknown = self.engine.handle({"version": PROTOCOL_VERSION, "op": "rm -rf"})
        self.assertFalse(unknown["ok"])
        self.assertEqual(unknown["code"], "unknown_op")

    def test_unknown_field_is_rejected_never_trusted(self) -> None:
        self._activate()
        # A caller-supplied authority/actor field must never be accepted.
        for field in ("uid", "actor", "role"):
            res = self.engine.handle(
                {"version": PROTOCOL_VERSION, "op": "status", field: "owner"}
            )
            self.assertFalse(res["ok"])
            self.assertEqual(res["code"], "unknown_key")

    def test_errors_are_curated_static_and_never_raise(self) -> None:
        self._activate()
        # missing required submit field returns a static typed code, not a raise.
        res = self.engine.handle(_req("submit", project_id=self.project.project_id))
        self.assertFalse(res["ok"])
        self.assertIn(res["code"], {"bad_value", "missing_key"})
        self.assertTrue(res["message"])

    def test_submit_without_active_project_id_is_denied(self) -> None:
        # With no activation and no project_id, the engine cannot know the
        # project, so it reports not_activated.
        res = self.engine.handle(
            _req(
                "submit",
                request_id="r0",
                task_kind=SYNTHETIC_NUMERIC_TASK,
                steps=1,
                delay_ms=0,
                outcome="complete",
                stage="s",
                reserve_usd="0.1",
            )
        )
        self.assertFalse(res["ok"])
        self.assertEqual(res["code"], "not_activated")

    def test_submit_with_explicit_project_acquires_idle_activation(self) -> None:
        # An explicit owner submit may acquire a free activation (no other
        # project holds it and none has outstanding remote work).
        res = self._submit_numeric("acquire-1")
        self.assertTrue(res["ok"])
        self.assertEqual(
            self.registry.activation().active_project_id, self.project.project_id
        )

    def test_submit_for_other_project_is_rejected(self) -> None:
        other = self.registry.create_project(self.tmp / "b", name="B")
        self._activate()
        res = self.engine.handle(
            _req(
                "submit",
                project_id=other.project_id,
                request_id="r0",
                task_kind=SYNTHETIC_NUMERIC_TASK,
                steps=1,
                delay_ms=0,
                outcome="complete",
                stage="s",
                reserve_usd="0.1",
            )
        )
        self.assertFalse(res["ok"])
        self.assertEqual(res["code"], "wrong_project")

    def test_out_of_range_and_bad_payload_are_rejected(self) -> None:
        self._activate()
        self.assertEqual(self._submit_numeric("r1", steps=0)["code"], "bad_value")
        self.assertEqual(self._submit_numeric("r2", steps=1001)["code"], "bad_value")
        self.assertEqual(self._submit_numeric("r3", delay_ms=60001)["code"], "bad_value")
        self.assertEqual(self._submit_numeric("r4", outcome="explode")["code"], "bad_value")
        self.assertEqual(self._submit_numeric("r5", reserve_usd="-1")["code"], "bad_value")
        self.assertEqual(self._submit_numeric("r6", reserve_usd=0.1)["code"], "bad_type")


class NumericCompletionTest(EngineTestBase):
    def test_one_numeric_completion_yields_one_accepted_result(self) -> None:
        self._activate()
        submitted = self._submit_numeric("req-1", steps=42, delay_ms=0)
        self.assertTrue(submitted["ok"])
        job_id = submitted["job"]["job_id"]

        self._drain_engine()

        res = self.engine.handle(
            _req("job_status", project_id=self.project.project_id, job_id=job_id)
        )
        self.assertTrue(res["ok"])
        job = res["job"]
        self.assertEqual(job["state"], "completed")
        self.assertFalse(job["outcome_unresolved"])
        self.assertEqual(job["attempts_total"], 1)
        self.assertIsNotNone(job["accepted_result"])
        self.assertEqual(job["accepted_result"]["result_kind"], "synthetic_numeric")
        # exactly one accepted result row.
        with self._repo() as repo:
            self.assertEqual(len(repo.attempts(job_id)), 1)

    def test_idempotent_resubmit_does_not_create_second_job(self) -> None:
        self._activate()
        first = self._submit_numeric("dup", steps=5)
        self._drain_engine()
        second = self._submit_numeric("dup", steps=5)
        self.assertEqual(first["job"]["job_id"], second["job"]["job_id"])
        listing = self.engine.handle(
            _req("job_list", project_id=self.project.project_id)
        )
        self.assertEqual(len(listing["jobs"]), 1)


class AmbiguousTest(EngineTestBase):
    def test_ambiguous_outcome_never_resent_and_escalates(self) -> None:
        self._activate()
        submitted = self._submit_numeric("am-1", outcome="ambiguous")
        job_id = submitted["job"]["job_id"]
        self._drain_engine()
        res = self.engine.handle(
            _req("job_status", project_id=self.project.project_id, job_id=job_id)
        )
        job = res["job"]
        self.assertEqual(job["state"], "waiting_human")
        self.assertTrue(job["outcome_unresolved"])
        self.assertEqual(job["attempts_total"], 1)
        # No new identity: still exactly one attempt.
        with self._repo() as repo:
            self.assertEqual(len(repo.attempts(job_id)), 1)

    def test_failed_outcome_is_explicit_and_terminal(self) -> None:
        self._activate()
        submitted = self._submit_numeric("fail-1", outcome="failure")
        job_id = submitted["job"]["job_id"]
        self._drain_engine()
        res = self.engine.handle(
            _req("job_status", project_id=self.project.project_id, job_id=job_id)
        )
        job = res["job"]
        self.assertEqual(job["state"], "failed")
        self.assertFalse(job["outcome_unresolved"])


class PauseAndBudgetTest(EngineTestBase):
    def test_pause_stops_new_dispatch(self) -> None:
        self._activate()
        paused = self.engine.handle(
            _req("pause", project_id=self.project.project_id)
        )
        self.assertTrue(paused["ok"])
        denied = self._submit_numeric("p-1")
        self.assertFalse(denied["ok"])
        self.assertEqual(denied["code"], "denied")
        resumed = self.engine.handle(_req("resume", project_id=self.project.project_id))
        self.assertTrue(resumed["ok"])
        allowed = self._submit_numeric("p-2")
        self.assertTrue(allowed["ok"])

    def test_budget_cap_stops_dispatch_with_visible_reason(self) -> None:
        self._activate()
        with self._repo() as repo:
            repo.set_budget_policy(project_cap_usd="0.05", stage_cap_usd="0.05")
        submitted = self._submit_numeric("b-1", reserve_usd="0.10")
        job_id = submitted["job"]["job_id"]
        self._drain_engine()
        res = self.engine.handle(
            _req("job_status", project_id=self.project.project_id, job_id=job_id)
        )
        job = res["job"]
        # Not a false success: the job is waiting_human with an explicit reason.
        self.assertEqual(job["state"], "waiting_human")
        self.assertEqual(job["wait_reason"], "budget_exceeded")
        self.assertNotEqual(job["state"], "completed")

    def test_pause_after_admission_reconciles_one_result_while_held(self) -> None:
        self._activate()
        submitted = self._submit_numeric("pr-1", delay_ms=150)
        job_id = submitted["job"]["job_id"]
        self.engine.tick()  # claim
        self.engine.tick()  # admit
        with self._repo() as repo:
            self.assertEqual(repo.get(job_id).state, "admitted")
        # Pause while the remote is in flight (arbitration must be retained).
        paused = self.engine.handle(_req("pause", project_id=self.project.project_id))
        self.assertTrue(paused["ok"])
        with self._repo() as repo:
            self.assertEqual(repo.get(job_id).state, "paused")
        self.assertEqual(
            self.registry.activation().active_project_id, self.project.project_id
        )
        # The paused project still reconciles the in-flight remote to one result,
        # preserving the held state.
        self._drain_engine()
        res = self.engine.handle(
            _req("job_status", project_id=self.project.project_id, job_id=job_id)
        )
        job = res["job"]
        self.assertEqual(job["state"], "paused")
        self.assertIsNotNone(job["accepted_result"])
        with self._repo() as repo:
            self.assertEqual(len(repo.attempts(job_id)), 1)

    def test_drain_after_admission_reconciles_one_result_while_held(self) -> None:
        self._activate()
        submitted = self._submit_numeric("dr-1", delay_ms=150)
        job_id = submitted["job"]["job_id"]
        self.engine.tick()  # claim
        self.engine.tick()  # admit
        drained = self.engine.handle(_req("drain", project_id=self.project.project_id))
        self.assertTrue(drained["ok"])
        self.assertEqual(drained["state"], "draining")
        # Under drain, the in-flight remote still finishes to one result.
        self._drain_engine()
        res = self.engine.handle(
            _req("job_status", project_id=self.project.project_id, job_id=job_id)
        )
        job = res["job"]
        self.assertEqual(job["state"], "paused")
        self.assertIsNotNone(job["accepted_result"])
        with self._repo() as repo:
            self.assertEqual(len(repo.attempts(job_id)), 1)


class NoBusyLoopTest(EngineTestBase):
    def test_waiting_human_gate_does_not_busy_loop(self) -> None:
        self._activate()
        submitted = self.engine.handle(
            _req(
                "submit",
                project_id=self.project.project_id,
                request_id="w-1",
                task_kind=SYNTHETIC_WAITING_HUMAN_TASK,
            )
        )
        self.assertTrue(submitted["ok"])
        self.assertEqual(submitted["job"]["state"], "waiting_human")
        # tick reports no pending work: the caller blocks instead of spinning.
        self.assertIsNone(self.engine.tick())
        self.assertIsNone(self.engine.tick())

    def test_idle_engine_reports_no_pending_work(self) -> None:
        self._activate()
        self.assertIsNone(self.engine.tick())

    def test_ambiguous_unknown_returns_none_not_busy_loop(self) -> None:
        self._activate()
        submitted = self._submit_numeric("u-1", outcome="ambiguous", delay_ms=0)
        job_id = submitted["job"]["job_id"]
        for _ in range(6):
            if self.engine.tick() is None:
                break
        res = self.engine.handle(
            _req("job_status", project_id=self.project.project_id, job_id=job_id)
        )
        self.assertEqual(res["job"]["state"], "waiting_human")
        # An unknown/ambiguous outcome must not be polled in a busy loop.
        self.assertIsNone(self.engine.tick())
        self.assertIsNone(self.engine.tick())

    def test_waiting_human_without_external_releases_activation(self) -> None:
        self._activate()
        submitted = self.engine.handle(
            _req(
                "submit",
                project_id=self.project.project_id,
                request_id="wh-rel",
                task_kind=SYNTHETIC_WAITING_HUMAN_TASK,
            )
        )
        self.assertTrue(submitted["ok"])
        self.assertEqual(submitted["job"]["state"], "waiting_human")
        # No remote work is pending, so arbitration is released for another project.
        self.assertEqual(self.registry.activation().state, "none")


class SameAttemptResumeTest(EngineTestBase):
    def test_resume_refuses_after_admission_but_reopens_proven_undispatched(self) -> None:
        self._activate()
        with self._repo() as repo:
            # (a) An attempt that was admitted may never be reopened same-identity.
            job_a = repo.submit(
                "resume-admitted",
                task_kind=SYNTHETIC_NUMERIC_TASK,
                parameters={"stage": "s", "reserve_usd": "0.1"},
            )
            att_a = repo.begin_attempt(
                job_a.job_id,
                dispatch_kind="external_synthetic",
                stage="s",
                reserve_usd="0.1",
            )
            repo.mark_admitted(att_a.attempt_id, admission_id="adm-" + att_a.attempt_id)
            repo.recover(att_a.attempt_id)
            with self.assertRaises(JobStateError):
                repo.resume_proven_undispatched(att_a.attempt_id)

            # (b) A proven-undispatched interrupted attempt reopens SAME identity.
            job_b = repo.submit(
                "resume-undispatched",
                task_kind=SYNTHETIC_NUMERIC_TASK,
                parameters={"stage": "s", "reserve_usd": "0.1"},
            )
            att_b = repo.begin_attempt(
                job_b.job_id,
                dispatch_kind="external_synthetic",
                stage="s",
                reserve_usd="0.1",
            )
            repo.recover(att_b.attempt_id)
            reopened = repo.resume_proven_undispatched(att_b.attempt_id)
            self.assertEqual(reopened.attempt_id, att_b.attempt_id)
            self.assertEqual(reopened.fence, att_b.fence)
            self.assertEqual(reopened.state, "queued")
            self.assertFalse(repo.get(job_b.job_id).outcome_unresolved)

    def test_resume_preserves_pause(self) -> None:
        self._activate()
        with self._repo() as repo:
            job = repo.submit(
                "resume-paused",
                task_kind=SYNTHETIC_NUMERIC_TASK,
                parameters={"stage": "s", "reserve_usd": "0.1"},
            )
            att = repo.begin_attempt(
                job.job_id,
                dispatch_kind="external_synthetic",
                stage="s",
                reserve_usd="0.1",
            )
            repo.recover(att.attempt_id)
            repo.pause(job.job_id)
            reopened = repo.resume_proven_undispatched(att.attempt_id)
            self.assertEqual(reopened.state, "queued")
            # The explicit pause is preserved (never silently undone).
            self.assertEqual(repo.get(job.job_id).state, "paused")


class SwitchTest(EngineTestBase):
    def test_safe_switch_quiesces_old_before_activating_new(self) -> None:
        self._activate()
        other = self.registry.create_project(self.tmp / "b", name="B")
        # An ambiguous (non-quiescent) old job must block the switch.
        self._submit_numeric("sw-1", outcome="ambiguous")
        self._drain_engine()
        blocked = self.engine.handle(_req("switch", project_id=other.project_id))
        self.assertFalse(blocked["ok"])
        self.assertEqual(blocked["code"], "switch_blocked")
        # Arbitration stays with the old project.
        self.assertEqual(
            self.registry.activation().active_project_id, self.project.project_id
        )

    def test_switch_succeeds_after_prior_work_is_resolved(self) -> None:
        self._activate()
        other = self.registry.create_project(self.tmp / "b", name="B")
        submitted = self._submit_numeric("sw-2", outcome="complete")
        job_id = submitted["job"]["job_id"]
        self._drain_engine()
        switched = self.engine.handle(_req("switch", project_id=other.project_id))
        self.assertTrue(switched["ok"])
        self.assertEqual(
            self.registry.activation().active_project_id, other.project_id
        )
        # The old project's result stayed in its own store.
        with self._repo() as old_repo:
            accepted = old_repo.accepted_result(job_id)
        self.assertIsNotNone(accepted)

    def test_switch_persists_pause_across_engine_restart(self) -> None:
        self._activate()
        other = self.registry.create_project(self.tmp / "b", name="B")
        # A job admitted but not yet completed keeps a remote in flight, so the
        # switch is blocked — but it must persist a durable pause first.
        submitted = self._submit_numeric("sw-persist", delay_ms=60000)
        job_id = submitted["job"]["job_id"]
        self.engine.tick()  # claim
        self.engine.tick()  # admit
        blocked = self.engine.handle(_req("switch", project_id=other.project_id))
        self.assertFalse(blocked["ok"])
        self.assertEqual(blocked["code"], "switch_blocked")

        # A brand-new engine over the same store sees the job paused durably.
        engine2 = self._new_engine()
        res = engine2.handle(
            _req("job_status", project_id=self.project.project_id, job_id=job_id)
        )
        self.assertEqual(res["job"]["state"], "paused")
        with self._repo() as repo:
            self.assertEqual(repo.get(job_id).state, "paused")

    def test_owner_reconcile_wrong_active_is_refused(self) -> None:
        self._activate()
        other = self.registry.create_project(self.tmp / "b", name="B")
        # Write directly into B a job to reconcile, then try to reconcile it
        # while A holds execution.
        with self._repo(other.project_id) as repo_b:
            job_b = repo_b.submit(
                "rb-1",
                task_kind=SYNTHETIC_NUMERIC_TASK,
                parameters={"stage": "s", "reserve_usd": "0.1"},
            )
        res = self.engine.handle(
            _req(
                "reconcile",
                project_id=other.project_id,
                job_id=job_b.job_id,
                resolution="human_escalation",
            )
        )
        self.assertFalse(res["ok"])
        self.assertEqual(res["code"], "wrong_project")

    def test_cancel_ambiguity_blocks_other_project(self) -> None:
        self._activate()
        other = self.registry.create_project(self.tmp / "b", name="B")
        submitted = self._submit_numeric("cx-1", outcome="ambiguous")
        job_id = submitted["job"]["job_id"]
        self._drain_engine()
        # Cancel the unresolved job; the ambiguity must survive cancellation and
        # still pin arbitration.
        cancelled = self.engine.handle(
            _req("cancel", project_id=self.project.project_id, job_id=job_id)
        )
        self.assertTrue(cancelled["ok"])
        self.assertTrue(cancelled["job"]["outcome_unresolved"])
        switched = self.engine.handle(_req("switch", project_id=other.project_id))
        self.assertFalse(switched["ok"])
        self.assertEqual(switched["code"], "switch_blocked")


def _submit_to(engine, project_id, request_id, **params):
    return engine.handle(
        _req(
            "submit",
            project_id=project_id,
            request_id=request_id,
            task_kind=SYNTHETIC_NUMERIC_TASK,
            steps=params.get("steps", 1),
            delay_ms=params.get("delay_ms", 0),
            outcome=params.get("outcome", "complete"),
            stage=params.get("stage", "s"),
            reserve_usd=params.get("reserve", "0.10"),
        )
    )


class CrashRecoveryTest(EngineTestBase):
    """Restart recovery from durable state (Step 03 AC2/AC3; C02 §8.3)."""

    def test_restart_before_dispatch_completes_same_attempt(self) -> None:
        job_id, attempt_id = self._simulate_dispatch(
            request_id="crash-predispatch", admitted=False, steps=9
        )
        engine = self._new_engine()
        engine.reconcile_startup()
        self._drive(engine)

        with self._repo() as repo:
            job = repo.get(job_id)
            accepted = repo.accepted_result(job_id)
            attempts = repo.attempts(job_id)
        self.assertEqual(job.state, "completed")
        self.assertEqual(job.current_attempt_id, attempt_id)  # same identity
        self.assertEqual(len(attempts), 1)  # no new attempt minted
        self.assertIsNotNone(accepted)
        # Exactly one provider dispatch row, now terminal.
        result = SyntheticBackend(str(self.data_dir)).lookup(
            attempt_id, self.project.project_id
        )
        self.assertEqual(result.status, "completed")

    def test_restart_after_admission_before_receipt_same_identity(self) -> None:
        job_id, attempt_id = self._simulate_dispatch(
            request_id="crash-postadmit", admitted=True, steps=5
        )
        engine = self._new_engine()
        engine.reconcile_startup()
        self._drive(engine)

        with self._repo() as repo:
            job = repo.get(job_id)
            accepted = repo.accepted_result(job_id)
            attempts = repo.attempts(job_id)
        self.assertEqual(job.state, "completed")
        self.assertEqual(job.current_attempt_id, attempt_id)
        self.assertEqual([a.attempt_id for a in attempts], [attempt_id])
        self.assertIsNotNone(accepted)

    def test_restart_failed_remote_resolves_nonbilling(self) -> None:
        job_id, attempt_id = self._simulate_dispatch(
            request_id="crash-fail", admitted=True, outcome="failure"
        )
        engine = self._new_engine()
        engine.reconcile_startup()
        self._drive(engine)

        with self._repo() as repo:
            job = repo.get(job_id)
            attempt = repo.attempt(attempt_id)
            accepted = repo.accepted_result(job_id)
        self.assertEqual(job.state, "failed")  # explicit, non-billing terminal
        self.assertFalse(job.outcome_unresolved)
        self.assertEqual(attempt.state, "definitive_failure")
        self.assertIsNone(accepted)

    def test_restart_paused_known_terminal_accept_preserves_held(self) -> None:
        job_id, attempt_id = self._simulate_dispatch(
            request_id="crash-paused", admitted=True, steps=3
        )
        with self._repo() as repo:
            repo.mark_admitted(attempt_id, admission_id="adm-" + attempt_id)
            repo.pause(job_id)

        engine = self._new_engine()
        engine.reconcile_startup()
        self._drive(engine)

        with self._repo() as repo:
            job = repo.get(job_id)
            accepted = repo.accepted_result(job_id)
        self.assertEqual(job.state, "paused")  # held state preserved
        self.assertIsNotNone(accepted)

    def test_restart_cancelled_known_terminal_accept_preserves_held(self) -> None:
        job_id, attempt_id = self._simulate_dispatch(
            request_id="crash-cancelled", admitted=True, steps=3
        )
        with self._repo() as repo:
            repo.mark_admitted(attempt_id, admission_id="adm-" + attempt_id)
            repo.cancel(job_id)

        engine = self._new_engine()
        engine.reconcile_startup()
        self._drive(engine)

        with self._repo() as repo:
            job = repo.get(job_id)
            accepted = repo.accepted_result(job_id)
        self.assertEqual(job.state, "cancelled")
        self.assertIsNotNone(accepted)


class RestoredArbitrationTest(EngineTestBase):
    """Restored/unactivated remote state blocks new execution (C02 §8.5)."""

    def _restore_admitted(self, project_id: str, request_id: str) -> str:
        with self.engine.open_repository(project_id) as repo:
            job = repo.submit(
                request_id, task_kind=SYNTHETIC_NUMERIC_TASK,
                parameters=self._params(),
            )
            attempt = repo.begin_attempt(
                job.job_id, dispatch_kind="external_synthetic",
                stage="s", reserve_usd="0.10",
            )
            repo.mark_admitted(attempt.attempt_id, admission_id="adm-" + attempt.attempt_id)
            return job.job_id

    def test_restored_unactivated_unknown_receives_arbitration_and_blocks(self) -> None:
        other = self.registry.create_project(self.tmp / "b", name="B")
        self._restore_admitted(self.project.project_id, "restored-1")
        self.assertEqual(self.registry.activation().state, "none")

        engine = self._new_engine()
        engine.reconcile_startup()
        # The sole pending-remote project receives arbitration so it can reconcile.
        self.assertEqual(
            self.registry.activation().active_project_id, self.project.project_id
        )
        # Submitting to, and switching to, another project is refused.
        self.assertFalse(_submit_to(engine, other.project_id, "b-1")["ok"])
        switched = engine.handle(_req("switch", project_id=other.project_id))
        self.assertFalse(switched["ok"])

    def test_two_restored_unknown_blocked_visible(self) -> None:
        other = self.registry.create_project(self.tmp / "b", name="B")
        self._restore_admitted(self.project.project_id, "restored-a")
        self._restore_admitted(other.project_id, "restored-b")

        engine = self._new_engine()
        engine.reconcile_startup()
        # Two pending projects: no automatic adoption.
        self.assertEqual(self.registry.activation().state, "none")
        status = engine.handle(_req("status"))
        self.assertEqual(
            set(status["recovery_projects"]),
            {self.project.project_id, other.project_id},
        )
        self.assertFalse(
            _submit_to(engine, self.project.project_id, "x-1")["ok"]
        )
        self.assertFalse(
            engine.handle(_req("switch", project_id=other.project_id))["ok"]
        )

    def test_unreadable_store_blocks_new_dispatch(self) -> None:
        other = self.registry.create_project(self.tmp / "b", name="B")
        # Corrupt B's store so it cannot be read (fail closed).
        info = self.registry.get(other.project_id)
        with open(paths.store_path_for(info.root_path), "wb") as handle:
            handle.write(b"this is not a sqlite database")

        engine = self._new_engine()
        engine.reconcile_startup()
        status = engine.handle(_req("status"))
        self.assertIn(other.project_id, status["unreadable_projects"])
        # A new dispatch for the healthy project is still refused.
        self.assertFalse(
            _submit_to(engine, self.project.project_id, "u-1")["ok"]
        )


class SubmitValidationTest(EngineTestBase):
    def test_invalid_reserve_rejected_without_durable_job(self) -> None:
        cases = [
            ("1e999999", "bad_value"),
            ("0", "bad_value"),
            ("-1", "bad_value"),
            (True, "bad_type"),
            (1.0, "bad_type"),
        ]
        for index, (bad, code) in enumerate(cases):
            res = self.engine.handle(
                _req(
                    "submit",
                    project_id=self.project.project_id,
                    request_id=f"bad-reserve-{index}",
                    task_kind=SYNTHETIC_NUMERIC_TASK,
                    steps=1,
                    delay_ms=0,
                    outcome="complete",
                    stage="s",
                    reserve_usd=bad,
                )
            )
            self.assertFalse(res["ok"], f"{bad!r} should be rejected")
            self.assertEqual(res["code"], code, f"{bad!r}")
        # No durable job was created for any invalid request.
        with self._repo() as repo:
            self.assertEqual(repo.list(), [])


class SerialExecutorTest(EngineTestBase):
    """One serial remote slot: a new job is not dispatched while another
    potential remote occupies the slot (task T4-R3 defect 1)."""

    def _settle(self, max_ticks: int = 8) -> None:
        for _ in range(max_ticks):
            if self.engine.tick() is None:
                return

    def test_second_job_not_dispatched_while_first_unresolved(self) -> None:
        self._activate()
        first = self._submit_numeric("amb-a", outcome="ambiguous", delay_ms=0)
        job_a = first["job"]["job_id"]
        self._settle()
        with self._repo() as repo:
            self.assertEqual(repo.get(job_a).state, "waiting_human")
            self.assertTrue(repo.get(job_a).outcome_unresolved)

        second = self._submit_numeric("complete-b", outcome="complete", delay_ms=0)
        job_b = second["job"]["job_id"]
        for _ in range(6):
            self.engine.tick()
        with self._repo() as repo:
            self.assertEqual(len(repo.attempts(job_a)), 1)
            self.assertEqual(len(repo.attempts(job_b)), 0)  # not claimed/admitted
        self.assertEqual(self._dispatch_count(), 1)
        # No busy loop while blocked.
        self.assertIsNone(self.engine.tick())
        # Repeated ambiguous jobs cannot add unbounded remote calls.
        self._submit_numeric("amb-c", outcome="ambiguous", delay_ms=0)
        for _ in range(6):
            self.engine.tick()
        self.assertEqual(self._dispatch_count(), 1)

    def test_second_job_dispatches_after_first_given_disposition(self) -> None:
        self._activate()
        first = self._submit_numeric("amb-d", outcome="ambiguous", delay_ms=0)
        job_a = first["job"]["job_id"]
        self._settle()
        self._submit_numeric("complete-e", outcome="complete", delay_ms=0)
        with self._repo() as repo:
            job_b = [j for j in repo.list() if j.job_id != job_a][0].job_id
        resolved = self.engine.handle(
            _req(
                "reconcile",
                project_id=self.project.project_id,
                job_id=job_a,
                resolution="definitive_failure",
            )
        )
        self.assertTrue(resolved["ok"])
        self._drain_engine()
        with self._repo() as repo:
            self.assertGreaterEqual(len(repo.attempts(job_b)), 1)
        self.assertEqual(self._dispatch_count(), 2)


class ReconcileResultBoundaryTest(EngineTestBase):
    """Owner reconcile accepts only the fixed synthetic result shape
    (task T4-R3 defect 2)."""

    CANARY = "synthetic-secret-canary"

    def _ambiguous(self, request_id: str) -> str:
        self._activate()
        submitted = self._submit_numeric(request_id, outcome="ambiguous", delay_ms=0)
        for _ in range(5):
            if self.engine.tick() is None:
                break
        return submitted["job"]["job_id"]

    def test_arbitrary_result_kind_and_text_are_rejected_and_not_persisted(self) -> None:
        job_id = self._ambiguous("secret-1")
        res = self.engine.handle(
            _req(
                "reconcile",
                project_id=self.project.project_id,
                job_id=job_id,
                resolution="accepted_local_result",
                result_kind=self.CANARY,
                result={"token": self.CANARY},
            )
        )
        self.assertFalse(res["ok"])
        with self._repo() as repo:
            self.assertIsNone(repo.accepted_result(job_id))
        store = paths.store_path_for(
            self.registry.get(self.project.project_id).root_path
        )
        self.assertNotIn(self.CANARY.encode(), Path(store).read_bytes())
        status = self.engine.handle(
            _req("job_status", project_id=self.project.project_id, job_id=job_id)
        )
        self.assertNotIn(self.CANARY, json.dumps(status))

    def test_only_fixed_synthetic_shape_accepted(self) -> None:
        job_id = self._ambiguous("shape-1")
        bad = [
            ("synthetic_numeric", {"kind": "synthetic_numeric", "value": 1, "extra": 2}),
            ("synthetic_numeric", {"kind": "synthetic_numeric", "value": "1"}),
            ("synthetic_numeric", {"kind": "synthetic_numeric", "value": 1_000_000}),
            ("synthetic_numeric", {"kind": "text", "value": 1}),
            ("synthetic_numeric", {"kind": "synthetic_numeric"}),
            ("other", {"kind": "synthetic_numeric", "value": 1}),
        ]
        for kind, payload in bad:
            res = self.engine.handle(
                _req(
                    "reconcile",
                    project_id=self.project.project_id,
                    job_id=job_id,
                    resolution="accepted_local_result",
                    result_kind=kind,
                    result=payload,
                )
            )
            self.assertFalse(res["ok"], f"{payload!r} should be rejected")
        good = self.engine.handle(
            _req(
                "reconcile",
                project_id=self.project.project_id,
                job_id=job_id,
                resolution="accepted_local_result",
                result_kind="synthetic_numeric",
                result={"kind": "synthetic_numeric", "value": 123},
            )
        )
        self.assertTrue(good["ok"])
        with self._repo() as repo:
            accepted = repo.accepted_result(job_id)
        self.assertEqual(accepted.result, {"kind": "synthetic_numeric", "value": 123})

    def test_other_resolutions_reject_result_fields(self) -> None:
        job_id = self._ambiguous("otherres-1")
        res = self.engine.handle(
            _req(
                "reconcile",
                project_id=self.project.project_id,
                job_id=job_id,
                resolution="definitive_failure",
                result={"kind": "synthetic_numeric", "value": 1},
            )
        )
        self.assertFalse(res["ok"])
        self.assertEqual(res["code"], "bad_value")


class PredispatchReleaseTest(EngineTestBase):
    """A proven-undispatched predispatch attempt does not pin arbitration
    (task T4-R3 defect 3)."""

    def test_paused_predispatch_restart_releases_and_resume_reuses_attempt(self) -> None:
        other = self.registry.create_project(self.tmp / "b", name="B")
        job_id, attempt_id = self._simulate_dispatch(
            request_id="pause-predispatch", admitted=False, steps=6
        )
        with self._repo() as repo:
            repo.pause(job_id)

        engine = self._new_engine()
        engine.reconcile_startup()
        self._drive(engine)

        self.assertEqual(self._dispatch_count(), 0)  # zero backend rows
        with self._repo() as repo:
            self.assertEqual(repo.attempt(attempt_id).state, "queued")
            self.assertEqual(repo.get(job_id).state, "paused")
        # The proven-undispatched attempt released arbitration.
        self.assertEqual(self.registry.activation().state, "none")
        # ... so a switch is now allowed.
        self.assertTrue(
            engine.handle(_req("switch", project_id=other.project_id))["ok"]
        )
        # Switching back and owner-resuming reuses the SAME identity.
        self.assertTrue(
            engine.handle(_req("switch", project_id=self.project.project_id))["ok"]
        )
        self.assertTrue(
            engine.handle(_req("resume", project_id=self.project.project_id))["ok"]
        )
        self._drive(engine)
        with self._repo() as repo:
            job = repo.get(job_id)
            attempts = repo.attempts(job_id)
        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0].attempt_id, attempt_id)
        self.assertEqual(job.state, "completed")
        self.assertEqual(self._dispatch_count(), 1)


class MultipleUnknownReconcileTest(EngineTestBase):
    """Owner can reconcile each restored-unknown project in turn
    (task T4-R3 defect 4)."""

    def _restore_admitted(self, project_id: str, request_id: str) -> str:
        with self.engine.open_repository(project_id) as repo:
            job = repo.submit(
                request_id, task_kind=SYNTHETIC_NUMERIC_TASK,
                parameters=self._params(),
            )
            attempt = repo.begin_attempt(
                job.job_id, dispatch_kind="external_synthetic",
                stage="s", reserve_usd="0.10",
            )
            repo.mark_admitted(attempt.attempt_id, admission_id="adm-" + attempt.attempt_id)
            return job.job_id

    def test_human_escalation_retains_reconciliation_activation(self) -> None:
        job_id = self._restore_admitted(self.project.project_id, "escalate")
        with self._repo() as repo:
            repo.mark_ambiguous(repo.get(job_id).current_attempt_id)
        response = self.engine.handle(_req(
            "reconcile", project_id=self.project.project_id,
            job_id=job_id, resolution="human_escalation",
        ))
        self.assertTrue(response["ok"])
        activation = self.registry.activation()
        self.assertEqual(activation.state, "draining")
        self.assertEqual(activation.active_project_id, self.project.project_id)
        with self._repo() as repo:
            self.assertTrue(repo.get(job_id).outcome_unresolved)
        other = self.registry.create_project(self.tmp / "other", name="Other")
        self.assertFalse(self.engine.handle(_req(
            "switch", project_id=other.project_id,
        ))["ok"])

    def test_owner_can_reconcile_each_unknown_project_in_turn(self) -> None:
        other = self.registry.create_project(self.tmp / "b", name="B")
        job_a = self._restore_admitted(self.project.project_id, "mu-a")
        job_b = self._restore_admitted(other.project_id, "mu-b")

        engine = self._new_engine()
        engine.reconcile_startup()
        self.assertEqual(self.registry.activation().state, "none")
        # Both new-dispatch and switch routes are blocked.
        self.assertFalse(_submit_to(engine, self.project.project_id, "blocked-1")["ok"])
        self.assertFalse(
            engine.handle(_req("switch", project_id=other.project_id))["ok"]
        )
        # Owner reconciles A, then B — each acquiring a reconciliation-only
        # activation that is released again.
        first = engine.handle(
            _req(
                "reconcile",
                project_id=self.project.project_id,
                job_id=job_a,
                resolution="definitive_failure",
            )
        )
        self.assertTrue(first["ok"])
        self.assertTrue(first["owner_control"])
        self.assertEqual(self.registry.activation().state, "none")
        second = engine.handle(
            _req(
                "reconcile",
                project_id=other.project_id,
                job_id=job_b,
                resolution="definitive_failure",
            )
        )
        self.assertTrue(second["ok"])
        self.assertEqual(self.registry.activation().state, "none")
        # No new backend calls were made to resolve the unknown projects.
        self.assertEqual(self._dispatch_count(), 0)
        # With both resolved, new dispatch is allowed again.
        self.assertTrue(_submit_to(engine, self.project.project_id, "after-1")["ok"])


class RegistryUnavailableTest(EngineTestBase):
    def test_queued_work_cannot_dispatch_after_registry_becomes_unavailable(self) -> None:
        response = self._submit_numeric("queued-before-failure")
        self.assertTrue(response["ok"])
        job_id = response["job"]["job_id"]
        with mock.patch.object(
            self.registry, "list_projects", side_effect=StoreError("unavailable")
        ):
            self.assertIsNone(self.engine.tick())
            with self._repo() as repo:
                self.assertEqual(repo.get(job_id).attempts_total, 0)
        self._drain_engine()
        with self._repo() as repo:
            self.assertEqual(repo.get(job_id).state, "completed")

    def test_registry_list_failure_blocks_new_dispatch(self) -> None:
        self._activate()
        with mock.patch.object(
            self.engine._registry, "list_projects", side_effect=StoreError("boom")
        ):
            status = self.engine.handle(_req("status"))
            self.assertFalse(status["registry_available"])
            res = self._submit_numeric("reg-1")
            self.assertFalse(res["ok"])
            self.assertEqual(res["code"], "switch_blocked")
            self.assertTrue(res["registry_unavailable"])
        # A readable registry again allows dispatch.
        self.assertTrue(self._submit_numeric("reg-2")["ok"])


class DetachedEntrypointTest(EngineTestBase):
    def test_detached_worker_start_status_stop(self) -> None:
        self.registry.activate_execution(self.project.project_id)
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join(
            [str(SRC), env.get("PYTHONPATH", "")]
        )
        proc = subprocess.Popen(
            [sys.executable, "-m", "review_article_agent.worker", "--data-dir", str(self.data_dir)],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.addCleanup(proc.kill)
        self.addCleanup(self._close_proc, proc)
        client = WorkerIPCClient(str(self.data_dir), io_timeout=5.0)
        # Wait for the socket to come up.
        deadline = time.monotonic() + 10.0
        status = None
        while time.monotonic() < deadline:
            try:
                status = client.request(_req("status"))
                break
            except Exception:  # not up yet
                time.sleep(0.05)
        self.assertIsNotNone(status, "worker did not become ready")
        self.assertEqual(status["app"], APP_NAME)
        self.assertEqual(status["protocol_version"], PROTOCOL_VERSION)
        self.assertEqual(status["pid"], proc.pid)
        self.assertTrue(status["state"])

        # Submit a numeric task through the detached worker and let it complete.
        sub = client.request(
            _req(
                "submit",
                project_id=self.project.project_id,
                request_id="detached-1",
                task_kind=SYNTHETIC_NUMERIC_TASK,
                steps=3,
                delay_ms=0,
                outcome="complete",
                stage="s",
                reserve_usd="0.10",
            )
        )
        self.assertTrue(sub["ok"])
        job_id = sub["job"]["job_id"]
        deadline = time.monotonic() + 10.0
        final = None
        while time.monotonic() < deadline:
            final = client.request(
                _req("job_status", project_id=self.project.project_id, job_id=job_id)
            )
            if final["job"]["state"] in ("completed", "failed"):
                break
            time.sleep(0.05)
        self.assertEqual(final["job"]["state"], "completed")

        # stop replies before the owned worker exits (with launcher identity).
        stop = client.request(_req("stop"))
        self.assertTrue(stop["ok"])
        self.assertTrue(stop["stopping"])
        self.assertEqual(stop["app"], APP_NAME)
        self.assertEqual(stop["protocol_version"], PROTOCOL_VERSION)
        self.assertEqual(stop["pid"], proc.pid)
        proc.wait(timeout=10)
        self.assertEqual(proc.returncode, 0)

    def test_sigkill_then_recover_no_silent_success(self) -> None:
        """A worker killed before commit leaves the attempt non-terminal.

        Uses the test-only SIGSTOP gate at ``before_commit`` to stop an owned
        disposable worker mid-dispatch, then SIGKILLs it. The comprehensive
        matrix lives in the separate restart task; this is a bounded smoke check.
        """
        self.registry.activate_execution(self.project.project_id)
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join([str(SRC), env.get("PYTHONPATH", "")])
        env["RAA_WORKER_TEST_PAUSE_AT"] = "before_commit"
        proc = subprocess.Popen(
            [sys.executable, "-m", "review_article_agent.worker", "--data-dir", str(self.data_dir)],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.addCleanup(proc.kill)
        self.addCleanup(self._close_proc, proc)
        client = WorkerIPCClient(str(self.data_dir), io_timeout=5.0)
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            try:
                client.request(_req("status"))
                break
            except Exception:
                time.sleep(0.05)
        sub = client.request(
            _req(
                "submit",
                project_id=self.project.project_id,
                request_id="kill-1",
                task_kind=SYNTHETIC_NUMERIC_TASK,
                steps=3,
                delay_ms=0,
                outcome="complete",
                stage="s",
                reserve_usd="0.10",
            )
        )
        job_id = sub["job"]["job_id"]
        # Wait until the worker is stopped at the before_commit seam.
        deadline = time.monotonic() + 10.0
        stopped = False
        while time.monotonic() < deadline:
            state = self._proc_state(proc.pid)
            if state == "T":
                stopped = True
                break
            time.sleep(0.05)
        self.assertTrue(stopped, "worker never reached the before_commit gate")
        proc.kill()
        proc.wait(timeout=10)

        # A fresh engine over the same store reconciles: the attempt is explicit,
        # never a silent success.
        engine2 = self._new_engine()
        engine2.reconcile_startup()
        with self._repo() as repo:
            job = repo.get(job_id)
            accepted = repo.accepted_result(job_id)
        self.assertIsNone(accepted)
        self.assertIn(
            job.state,
            {"reconciliation_required", "interrupted", "admitted", "running", "queued"},
        )
        self.assertNotEqual(job.state, "completed")

    def test_duplicate_entrypoint_start_does_not_recover_running_job(self) -> None:
        """A second worker on the same data_dir must fail its lock WITHOUT
        touching the first worker's live attempt (lock before recovery)."""
        self.registry.activate_execution(self.project.project_id)
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join([str(SRC), env.get("PYTHONPATH", "")])
        first = subprocess.Popen(
            [sys.executable, "-m", "review_article_agent.worker", "--data-dir", str(self.data_dir)],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.addCleanup(first.kill)
        self.addCleanup(self._close_proc, first)
        client = WorkerIPCClient(str(self.data_dir), io_timeout=5.0)
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            try:
                client.request(_req("status"))
                break
            except Exception:
                time.sleep(0.05)
        # Admit a long-running numeric job, then confirm it is in-flight.
        sub = client.request(
            _req(
                "submit",
                project_id=self.project.project_id,
                request_id="dup-run-1",
                task_kind=SYNTHETIC_NUMERIC_TASK,
                steps=3,
                delay_ms=60000,
                outcome="complete",
                stage="s",
                reserve_usd="0.10",
            )
        )
        job_id = sub["job"]["job_id"]
        deadline = time.monotonic() + 10.0
        state = None
        while time.monotonic() < deadline:
            res = client.request(
                _req("job_status", project_id=self.project.project_id, job_id=job_id)
            )
            state = res["job"]["state"]
            if state in ("admitted", "running"):
                break
            time.sleep(0.05)
        self.assertIn(state, {"admitted", "running"})

        # A second worker must fail to acquire the singleton lock and exit.
        second = subprocess.run(
            [sys.executable, "-m", "review_article_agent.worker", "--data-dir", str(self.data_dir)],
            env=env,
            capture_output=True,
            timeout=15,
        )
        self.assertNotEqual(second.returncode, 0)
        # It must NOT have recovered the first worker's live attempt.
        with self._repo() as repo:
            self.assertEqual(repo.attempt(self._attempt_id(repo, job_id)).state, "admitted")

        stop = client.request(_req("stop"))
        self.assertTrue(stop["stopping"])
        first.wait(timeout=10)

    def test_before_dispatch_sigkill_completes_same_attempt(self) -> None:
        """AC2: a worker SIGKILLed before dispatch recovers and completes the
        SAME attempt (one accepted result) on a fresh worker."""
        self.registry.activate_execution(self.project.project_id)
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join([str(SRC), env.get("PYTHONPATH", "")])
        env["RAA_WORKER_TEST_PAUSE_AT"] = "before_dispatch"
        first = self._spawn(env)
        client = WorkerIPCClient(str(self.data_dir), io_timeout=5.0)
        self._wait_ready(client)
        sub = client.request(
            _req(
                "submit",
                project_id=self.project.project_id,
                request_id="kill-predispatch",
                task_kind=SYNTHETIC_NUMERIC_TASK,
                steps=4,
                delay_ms=0,
                outcome="complete",
                stage="s",
                reserve_usd="0.10",
            )
        )
        self.assertTrue(sub["ok"])
        job_id = sub["job"]["job_id"]
        # Wait until the worker is stopped at the before_dispatch seam.
        self.assertTrue(
            self._wait_for_state(first.pid, "T"),
            "worker never reached the before_dispatch gate",
        )
        first.kill()
        first.wait(timeout=10)

        # A fresh worker owns the lock and reconciles the same attempt.
        env2 = dict(env)
        env2.pop("RAA_WORKER_TEST_PAUSE_AT", None)
        second = self._spawn(env2)
        client2 = WorkerIPCClient(str(self.data_dir), io_timeout=5.0)
        self._wait_ready(client2)
        deadline = time.monotonic() + 15.0
        final = None
        while time.monotonic() < deadline:
            final = client2.request(
                _req("job_status", project_id=self.project.project_id, job_id=job_id)
            )
            if final["job"]["state"] in ("completed", "failed"):
                break
            time.sleep(0.05)
        self.assertEqual(final["job"]["state"], "completed")
        self.assertEqual(final["job"]["attempts_total"], 1)
        self.assertIsNotNone(final["job"]["accepted_result"])
        stop = client2.request(_req("stop"))
        self.assertTrue(stop["stopping"])
        second.wait(timeout=10)

    def _spawn(self, env: dict[str, str]) -> subprocess.Popen:
        proc = subprocess.Popen(
            [sys.executable, "-m", "review_article_agent.worker", "--data-dir", str(self.data_dir)],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.addCleanup(proc.kill)
        self.addCleanup(self._close_proc, proc)
        return proc

    def _wait_ready(self, client: WorkerIPCClient) -> None:
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            try:
                client.request(_req("status"))
                return
            except Exception:
                time.sleep(0.05)
        self.fail("worker did not become ready")

    def _wait_for_state(self, pid: int, want: str) -> bool:
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            if self._proc_state(pid) == want:
                return True
            time.sleep(0.05)
        return False

    @staticmethod
    def _attempt_id(repo, job_id: str) -> str:
        job = repo.get(job_id)
        assert job is not None and job.current_attempt_id is not None
        return job.current_attempt_id

    @staticmethod
    def _proc_state(pid: int) -> str | None:
        try:
            with open(f"/proc/{pid}/stat", "r", encoding="utf-8") as handle:
                return handle.read().split(") ", 1)[1].split(" ", 1)[0]
        except OSError:
            return None

    @staticmethod
    def _close_proc(proc: subprocess.Popen) -> None:
        for stream in (proc.stdout, proc.stderr):
            if stream is not None:
                try:
                    stream.close()
                except OSError:  # pragma: no cover - defensive
                    pass


class UDSIntegrationTest(EngineTestBase):
    def test_uds_engine_round_trip_and_stop(self) -> None:
        self._activate()
        server = WorkerIPCServer(str(self.data_dir), self.engine.handle)
        server.start()
        self.addCleanup(server.close)
        client = WorkerIPCClient(str(self.data_dir), io_timeout=5.0)

        results: dict[str, object] = {}
        errors: list[BaseException] = []

        def client_work() -> None:
            try:
                results["status"] = client.request(_req("status"))
                # Non-owner field is rejected over the real transport too: the
                # engine returns a curated structured error (outer transport ok).
                rejected = client.request(_req("status", uid=0))
                results["unknown_key_rejected"] = (
                    rejected.get("ok") is False
                    and rejected.get("code") == "unknown_key"
                )
                results["stop"] = client.request(_req("stop"))
            except BaseException as exc:  # pragma: no cover - surfaced below
                errors.append(exc)

        # The engine loop runs in the MAIN thread so every store is opened and
        # closed on the same thread (sqlite connections are not shared across
        # threads); only the client runs in a helper thread.
        worker = threading.Thread(target=client_work, daemon=True)
        worker.start()
        while not self.engine.stopping:
            timeout = self.engine.tick()
            wait = 0.05 if timeout is None else min(timeout, 0.05) if timeout > 0 else 0.0
            server.serve_once(wait)
        worker.join(5)

        self.assertFalse(errors, f"client thread failed: {errors}")
        self.assertEqual(results["status"]["app"], APP_NAME)
        self.assertTrue(results["unknown_key_rejected"])
        self.assertTrue(results["stop"]["stopping"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
