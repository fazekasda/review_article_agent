"""Acceptance tests: durable runtime dispatches in worker arbitration/recovery.

Synthetic data only (no secrets, no licensed source text, no model/network call).

These tests cover the Step 04 (task T7c) integration of the durable **runtime
dispatch ledger** (``runtime/dispatch.py``) into the existing synthetic worker
engine's global arbitration, startup recovery, and single serial remote slot
(C02 §8.3/§8.5; spec "one active project at a time"):

* an outstanding runtime dispatch (``submitted``/``admitted``/``unknown``) — on
  **either** the ``opencode_session`` or the ``direct_call`` route — counts as
  potential remote work: it occupies the serial slot, blocks a *new* synthetic
  claim/admit in the same project, and pins the project's arbitration;
* a runtime dispatch in another project blocks new work and blocks a switch, and
  a sole restored runtime-remote project is adopted so it can be reconciled;
* a restart recovers ``submitted`` -> ``unknown`` exactly once (same identity,
  fence, and retained reserve) and retains an ``opencode_session`` admission
  unchanged; an ``unknown`` dispatch is never released (its reserve stays held);
* ``reserved`` (recorded before any I/O) and terminal (``completed``/``failed``)
  dispatches are not remote and do not block;
* an already-remote synthetic attempt is still reconciled while a runtime
  dispatch occupies the slot;
* a corrupt, dangling, or missing runtime ledger fails closed as ``unreadable``
  and blocks new dispatch (no partial recovery).

Run with::

    PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_runtime_worker_arbitration.py'
"""

from __future__ import annotations

import sys
import tempfile
import time
import unittest
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from review_article_agent.runtime.dispatch import (  # noqa: E402
    RUNTIME_REMOTE_STATES,
    RuntimeDispatchRepository,
    runtime_dispatch_remote_state,
    runtime_reserve_exposure,
)
from review_article_agent.runtime.jobs import JobRepository  # noqa: E402
from review_article_agent.runtime.roles import (  # noqa: E402
    BoundedTask,
    RoleConfiguration,
)
from review_article_agent.storage import (  # noqa: E402
    ArtifactRepository,
    ProjectStore,
    Registry,
    paths,
)
from review_article_agent.storage.agent_runtime import RuntimeRepository  # noqa: E402
from review_article_agent.worker.engine import (  # noqa: E402
    PROTOCOL_VERSION,
    SYNTHETIC_NUMERIC_TASK,
    WorkerEngine,
)
from review_article_agent.worker.synthetic import SyntheticBackend  # noqa: E402

_OPS = ("acquisition", "storage", "redistribution", "hosted_transfer")

_DIG_A = "a" * 64
_DIG_B = "b" * 64
_DIG_C = "c" * 64
_DIG_D = "d" * 64


def _req(op: str, **kw: object) -> dict:
    return {"version": PROTOCOL_VERSION, "op": op, **kw}


def _rights(project_id, artifact_id, revision_id, *, rights_id=None):
    ops = {op: "allowed" for op in _OPS}
    return {
        "rights_id": rights_id if rights_id is not None else f"RIGHTS-{artifact_id}",
        "project_id": project_id,
        "record_revision": 1,
        "subject": {
            "kind": "source_revision",
            "project_id": project_id,
            "artifact_id": artifact_id,
            "revision_id": revision_id,
        },
        "operations": ops,
        "basis": {
            op: {
                "kind": "public_domain",
                "evidence": {"ref": "lic:pd-syn", "provenance": "publisher:pd-list"},
            }
            for op in _OPS
        },
        "authorization": None,
        "restrictions": [],
        "privacy": {"personal_data": False, "confidentiality": "public", "denial": False},
        "recorded_by": "user",
        "recorded_at": "2026-10-28T00:00:00Z",
        "derived_status": "valid",
    }


def _config(role="writer", model="anthropic/claude-sonnet-5-5", variant="high"):
    return RoleConfiguration.from_mapping(
        {
            "schema_version": 1,
            "roles": {
                role: {
                    "provider_id": "openrouter",
                    "model_id": model,
                    "variant": variant,
                    "steps": 8,
                }
            },
        }
    )


class RuntimeArbBase(unittest.TestCase):
    """A real temporary registry + projects + engine, with a runtime seed helper."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="raa-rt-arb-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.data_dir = self.tmp / "state"
        self.registry = Registry(self.data_dir)
        self.a = self.registry.create_project(self.tmp / "a", name="A")
        self.engine = WorkerEngine(str(self.data_dir), registry=self.registry)
        self.addCleanup(self.engine.close)
        self._seq = 0

    # -- fixtures ------------------------------------------------------------ #
    def _new_project(self, name: str):
        return self.registry.create_project(self.tmp / name.lower(), name=name)

    def _new_engine(self) -> WorkerEngine:
        engine = WorkerEngine(str(self.data_dir))
        self.addCleanup(engine.close)
        return engine

    @contextmanager
    def _store(self, project_id):
        info = self.registry.get(project_id)
        store = ProjectStore.open_at(
            paths.app_dir_for(info.root_path), expected_project_id=project_id
        )
        try:
            yield store
        finally:
            store.close()

    def _make_task(self, store, project_id, *, request_id):
        artifacts = ArtifactRepository(store)
        artifact = artifacts.create_artifact(kind="document")
        revision_id = "rev_" + request_id.replace("-", "_")
        artifacts.import_revision(
            artifact_id=artifact.artifact_id,
            content=b"runtime-arbitration-source",
            origin_locator="import:synthetic",
            rights=_rights(project_id, artifact.artifact_id, revision_id),
            revision_id=revision_id,
        )
        config = _config()
        mapping = {
            "project_id": project_id,
            "role": "writer",
            "target": {
                "project_id": project_id,
                "artifact_id": artifact.artifact_id,
                "revision_id": revision_id,
            },
            "input_refs": [],
            "scope": ["section:introduction"],
            "prompt_version": "writer.v1",
            "size_limit": 4096,
            "step_limit": 4,
        }
        bounded = BoundedTask.from_mapping(mapping, configuration=config)
        return RuntimeRepository(store).record_task(
            bounded, request_id=request_id, configuration=config
        )

    @staticmethod
    def _advance(repo, record, target):
        if target == "reserved":
            return record
        if target == "submitted":
            return repo.mark_submitted(record.dispatch_id)
        if target == "admitted":
            record = repo.mark_submitted(record.dispatch_id)
            return repo.mark_admitted(
                record.dispatch_id, admission_id="adm-" + record.dispatch_id
            )
        if target == "unknown":
            record = repo.mark_submitted(record.dispatch_id)
            return repo.recover(record.dispatch_id)
        if target == "completed":
            record = repo.mark_submitted(record.dispatch_id)
            return repo.accept_result(
                record.dispatch_id,
                fence=record.current_fence,
                result_kind="synthetic",
                result_ref="res_1",
                result_digest=_DIG_D,
            )
        if target == "failed":
            return repo.mark_failed(record.dispatch_id)
        raise AssertionError(f"unknown target {target!r}")

    def _seed(
        self,
        project_id,
        *,
        route="opencode_session",
        target="unknown",
        request_id=None,
        reserve="0.10",
        stage="s1",
    ):
        """Seed one real runtime task + dispatch into a project store."""
        self._seq += 1
        request_id = request_id or f"rt-call-{self._seq}"
        with self._store(project_id) as store:
            task = self._make_task(store, project_id, request_id=request_id + "-task")
            repo = RuntimeDispatchRepository(store)
            record = repo.reserve_dispatch(
                task,
                request_id=request_id,
                route=route,
                stage=stage,
                input_digest=_DIG_A,
                version_digest=_DIG_B,
                reserve_usd=reserve,
                bound_evidence_digest=_DIG_C,
            )
            return self._advance(repo, record, target)

    def _seed_synthetic_admitted(self, project_id, request_id):
        """Persist a crash-like admitted synthetic attempt (no local receipt)."""
        with self._store(project_id) as store:
            repo = JobRepository(store)
            job = repo.submit(
                request_id,
                task_kind=SYNTHETIC_NUMERIC_TASK,
                parameters={
                    "steps": 2,
                    "delay_ms": 0,
                    "outcome": "complete",
                    "stage": "s1",
                    "reserve_usd": "0.02",
                },
            )
            attempt = repo.begin_attempt(
                job.job_id,
                dispatch_kind="external_synthetic",
                stage="s1",
                reserve_usd="0.02",
            )
            continuity = SyntheticBackend(str(self.data_dir)).initialize()
            repo.record_checkpoint(
                attempt.attempt_id,
                {"continuity_id": continuity, "phase": "predispatch"},
            )
            SyntheticBackend(
                str(self.data_dir), expected_continuity_id=continuity
            ).admit(
                attempt.attempt_id,
                project_id,
                steps=2,
                delay_ms=0,
                outcome="complete",
            )
            return job.job_id

    # -- reads --------------------------------------------------------------- #
    def _dispatch(self, project_id, dispatch_id):
        with self._store(project_id) as store:
            return RuntimeDispatchRepository(store).get_dispatch(dispatch_id)

    def _exposure(self, project_id):
        with self._store(project_id) as store:
            return runtime_reserve_exposure(store.connection, project_id)

    def _raw_state(self, project_id, dispatch_id):
        with self._store(project_id) as store:
            row = store.connection.execute(
                "SELECT state FROM runtime_dispatch_state WHERE project_id = ? "
                "AND dispatch_id = ?",
                (project_id, dispatch_id),
            ).fetchone()
            return None if row is None else row["state"]

    def _submit_numeric(self, engine, project_id, request_id, **kw):
        params = {
            "project_id": project_id,
            "request_id": request_id,
            "task_kind": SYNTHETIC_NUMERIC_TASK,
            "steps": kw.pop("steps", 3),
            "delay_ms": kw.pop("delay_ms", 0),
            "outcome": kw.pop("outcome", "complete"),
            "stage": kw.pop("stage", "s1"),
            "reserve_usd": kw.pop("reserve_usd", "0.05"),
        }
        params.update(kw)
        return engine.handle(_req("submit", **params))

    def _drain(self, engine, max_ticks=500):
        for _ in range(max_ticks):
            hint = engine.tick()
            if hint is None:
                return
            if hint > 0:
                time.sleep(min(hint, 0.02))
        self.fail("engine did not settle within the tick budget")

    def _job_state(self, project_id, job_id):
        with self._store(project_id) as store:
            return JobRepository(store).get(job_id).state

    def _attempt_count(self, project_id, job_id):
        with self._store(project_id) as store:
            return len(JobRepository(store).attempts(job_id))


class HelperClassificationTest(RuntimeArbBase):
    """The read-only classifier reuses the ledger and fails closed."""

    def test_remote_states_vocabulary(self) -> None:
        self.assertEqual(set(RUNTIME_REMOTE_STATES), {"submitted", "admitted", "unknown"})

    def test_classifies_each_route_and_state(self) -> None:
        with self._store(self.a.project_id) as store:
            conn, pid = store.connection, self.a.project_id
            self.assertEqual(runtime_dispatch_remote_state(conn, pid), "none")
        for route in ("opencode_session", "direct_call"):
            for target, expected in (
                ("reserved", "none"),
                ("submitted", "remote"),
                ("unknown", "remote"),
                ("completed", "none"),
                ("failed", "none"),
            ):
                pid = self._new_project(f"P{route[:4]}{target[:3]}").project_id
                self._seed(pid, route=route, target=target)
                with self._store(pid) as store:
                    self.assertEqual(
                        runtime_dispatch_remote_state(store.connection, pid),
                        expected,
                        f"{route}/{target}",
                    )
            # opencode_session admission is remote; direct_call has no admission.
            if route == "opencode_session":
                pid = self._new_project("PAdm").project_id
                self._seed(pid, route=route, target="admitted")
                with self._store(pid) as store:
                    self.assertEqual(
                        runtime_dispatch_remote_state(store.connection, pid), "remote"
                    )


class SameProjectSerialTest(RuntimeArbBase):
    """An outstanding runtime dispatch occupies the single serial remote slot."""

    def _assert_blocks_new_claim(self, route: str) -> None:
        record = self._seed(self.a.project_id, route=route, target="unknown")
        engine = self._new_engine()
        engine.reconcile_startup()
        # The sole runtime-remote project is adopted so it can be reconciled.
        self.assertEqual(self.registry.activation().active_project_id, self.a.project_id)

        submitted = self._submit_numeric(engine, self.a.project_id, "syn-blocked")
        self.assertTrue(submitted["ok"])
        job_id = submitted["job"]["job_id"]

        # No new synthetic claim/admit while the runtime remote occupies the slot.
        self.assertIsNone(engine.tick())
        self.assertEqual(self._attempt_count(self.a.project_id, job_id), 0)
        self.assertEqual(self._job_state(self.a.project_id, job_id), "queued")

        # The runtime dispatch is unchanged and its reserve is still held.
        after = self._dispatch(self.a.project_id, record.dispatch_id)
        self.assertEqual(after.state, "unknown")
        self.assertEqual(after.reserve_usd, record.reserve_usd)
        self.assertEqual(self._exposure(self.a.project_id), Decimal("0.10"))

    def test_opencode_session_unknown_blocks_new_synthetic_claim(self) -> None:
        self._assert_blocks_new_claim("opencode_session")

    def test_direct_call_unknown_blocks_new_synthetic_claim(self) -> None:
        self._assert_blocks_new_claim("direct_call")

    def test_reserved_and_terminal_runtime_are_not_blocking(self) -> None:
        for target in ("reserved", "completed", "failed"):
            pid = self._new_project(f"NonBlock{target[:4]}").project_id
            self._seed(pid, target=target)
            engine = self._new_engine()
            engine.reconcile_startup()
            submitted = self._submit_numeric(engine, pid, f"syn-{target}")
            self.assertTrue(submitted["ok"], target)
            self._drain(engine)
            self.assertEqual(self._job_state(pid, submitted["job"]["job_id"]), "completed")

    def test_runtime_occupancy_still_allows_synthetic_reconciliation(self) -> None:
        # A runtime remote in the same project must not strand an already-admitted
        # synthetic attempt: finishing work the engine had already accepted stays
        # allowed.
        self._seed(self.a.project_id, target="unknown")
        job_id = self._seed_synthetic_admitted(self.a.project_id, "syn-adm-1")
        engine = self._new_engine()
        engine.reconcile_startup()
        self.assertEqual(self.registry.activation().active_project_id, self.a.project_id)
        for _ in range(12):
            if engine.tick() is None:
                break
        self.assertEqual(self._job_state(self.a.project_id, job_id), "completed")


class CrossProjectTest(RuntimeArbBase):
    """A runtime remote in another project blocks new work and a switch."""

    def test_crossproject_runtime_blocks_new_work_and_switch(self) -> None:
        b = self._new_project("B")
        self._seed(b.project_id, target="unknown")
        engine = self._new_engine()
        engine.reconcile_startup()
        # The sole runtime-remote project (B) receives arbitration.
        self.assertEqual(self.registry.activation().active_project_id, b.project_id)

        blocked = self._submit_numeric(engine, self.a.project_id, "syn-a")
        self.assertFalse(blocked["ok"])
        self.assertEqual(blocked["code"], "switch_blocked")

        switched = engine.handle(_req("switch", project_id=self.a.project_id))
        self.assertFalse(switched["ok"])
        self.assertEqual(switched["code"], "switch_blocked")
        # B still holds arbitration (never silently released).
        self.assertEqual(self.registry.activation().active_project_id, b.project_id)

    def test_two_runtime_remote_projects_block_and_are_visible(self) -> None:
        b = self._new_project("B")
        c = self._new_project("C")
        self._seed(b.project_id, target="unknown")
        self._seed(c.project_id, target="unknown")
        engine = self._new_engine()
        engine.reconcile_startup()
        # Two pending projects: no automatic adoption.
        self.assertEqual(self.registry.activation().state, "none")
        status = engine.handle(_req("status"))
        self.assertEqual(
            set(status["recovery_projects"]), {b.project_id, c.project_id}
        )
        self.assertFalse(self._submit_numeric(engine, self.a.project_id, "syn-a")["ok"])
        self.assertFalse(
            engine.handle(_req("switch", project_id=self.a.project_id))["ok"]
        )


class StartupRecoveryTest(RuntimeArbBase):
    """Restart reconciliation of the runtime ledger (no new identity, no release)."""

    def test_restart_recovers_submitted_once_and_retains_admitted(self) -> None:
        submitted = self._seed(
            self.a.project_id, target="submitted", request_id="rt-sub", reserve="0.10"
        )
        admitted = self._seed(
            self.a.project_id, target="admitted", request_id="rt-adm", reserve="0.20"
        )
        exposure_before = self._exposure(self.a.project_id)
        self.assertEqual(exposure_before, Decimal("0.30"))

        engine = self._new_engine()
        engine.reconcile_startup()

        recovered = self._dispatch(self.a.project_id, submitted.dispatch_id)
        self.assertEqual(recovered.state, "unknown")
        # Same identity, fence, budget, and creation time; the reserve is retained.
        self.assertEqual(recovered.dispatch_id, submitted.dispatch_id)
        self.assertEqual(recovered.request_id, submitted.request_id)
        self.assertEqual(recovered.current_fence, submitted.current_fence)
        self.assertEqual(recovered.reserve_usd, "0.10")
        self.assertEqual(recovered.created_at_utc, submitted.created_at_utc)
        self.assertEqual(recovered.cost_state, "unknown")

        retained = self._dispatch(self.a.project_id, admitted.dispatch_id)
        self.assertEqual(retained.state, "admitted")
        self.assertEqual(retained.admission_id, admitted.admission_id)
        self.assertEqual(retained.current_fence, 1)
        self.assertEqual(retained.reserve_usd, "0.20")

        # No reserve was released: exposure is unchanged.
        self.assertEqual(self._exposure(self.a.project_id), exposure_before)

        # Recovery is idempotent: a second scan mints nothing and changes nothing.
        engine.reconcile_startup()
        self.assertEqual(
            self._dispatch(self.a.project_id, submitted.dispatch_id).state, "unknown"
        )
        self.assertEqual(self._exposure(self.a.project_id), exposure_before)

    def test_unknown_remains_held_across_pause_drain_and_restart(self) -> None:
        record = self._seed(self.a.project_id, target="unknown")
        engine = self._new_engine()
        engine.reconcile_startup()
        self.assertEqual(self.registry.activation().active_project_id, self.a.project_id)

        # Pause does not release the project's arbitration.
        self.assertTrue(engine.handle(_req("pause", project_id=self.a.project_id))["ok"])
        self.assertEqual(self.registry.activation().active_project_id, self.a.project_id)
        self.assertIsNone(engine.tick())
        self.assertEqual(self.registry.activation().active_project_id, self.a.project_id)
        self.assertTrue(engine.handle(_req("resume", project_id=self.a.project_id))["ok"])

        # Drain does not release it either.
        self.assertTrue(engine.handle(_req("drain", project_id=self.a.project_id))["ok"])
        self.assertEqual(self.registry.activation().state, "draining")
        self.assertIsNone(engine.tick())
        self.assertEqual(self.registry.activation().active_project_id, self.a.project_id)

        # A restart retains the unknown dispatch and the arbitration.
        engine2 = self._new_engine()
        engine2.reconcile_startup()
        self.assertEqual(
            self._dispatch(self.a.project_id, record.dispatch_id).state, "unknown"
        )
        self.assertEqual(self.registry.activation().active_project_id, self.a.project_id)
        self.assertEqual(self._exposure(self.a.project_id), Decimal("0.10"))


class CorruptionTest(RuntimeArbBase):
    """A runtime ledger that cannot be proven intact fails closed as unreadable."""

    def _assert_unreadable_and_blocked(self, project_id, dispatch_id, expected_state):
        engine = self._new_engine()
        engine.reconcile_startup()
        status = engine.handle(_req("status"))
        self.assertIn(project_id, status["unreadable_projects"])
        self.assertNotIn(project_id, status["recovery_projects"])
        self.assertFalse(self._submit_numeric(engine, project_id, "syn-x")["ok"])
        self.assertFalse(engine.handle(_req("switch", project_id=project_id))["ok"])
        # No partial recovery of the outstanding submission.
        self.assertEqual(self._raw_state(project_id, dispatch_id), expected_state)

    def test_corrupt_state_row_is_unreadable(self) -> None:
        record = self._seed(self.a.project_id, target="submitted")
        with self._store(self.a.project_id) as store:
            with store.transaction():
                store.connection.execute(
                    "UPDATE runtime_dispatch_state SET updated_at_utc = ? "
                    "WHERE project_id = ? AND dispatch_id = ?",
                    ("not-a-timestamp", self.a.project_id, record.dispatch_id),
                )
        self._assert_unreadable_and_blocked(
            self.a.project_id, record.dispatch_id, "submitted"
        )

    def test_missing_state_table_is_unreadable(self) -> None:
        record = self._seed(self.a.project_id, target="submitted")
        with self._store(self.a.project_id) as store:
            with store.transaction():
                store.connection.execute("DROP TABLE runtime_dispatch_state")
        engine = self._new_engine()
        engine.reconcile_startup()
        status = engine.handle(_req("status"))
        self.assertIn(self.a.project_id, status["unreadable_projects"])
        self.assertFalse(self._submit_numeric(engine, self.a.project_id, "syn-x")["ok"])

    def test_dangling_state_row_is_unreadable(self) -> None:
        record = self._seed(self.a.project_id, target="submitted")
        with self._store(self.a.project_id) as store:
            conn = store.connection
            conn.execute("PRAGMA foreign_keys = OFF")
            with store.transaction():
                conn.execute(
                    "INSERT INTO runtime_dispatch_state "
                    "(project_id, dispatch_id, state, current_fence, cost_state, "
                    " updated_at_utc) VALUES (?, ?, 'unknown', 1, 'unknown', ?)",
                    (self.a.project_id, "disp_orphan_row", "2026-10-28T00:00:00Z"),
                )
        self._assert_unreadable_and_blocked(
            self.a.project_id, record.dispatch_id, "submitted"
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
