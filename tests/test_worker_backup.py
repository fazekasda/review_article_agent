"""Backup/verify/restore compatibility with the Step 03 durable worker (T6).

These tests extend the private backup contract (``storage/backup.py``,
``docs/storage.md``) to the durable per-project job/attempt/budget tables added
by store schema 4 (``runtime/jobs.py``):

* a **schema-3** snapshot (the pre-Step-03 schema) still verifies against its own
  exact canonical schema and restores, upgraded to schema 4 by the ordinary
  ``ProjectStore`` migration, preserving exact artifact bytes and fixture
  approvals;
* a **schema-4** snapshot roundtrips pending/completed/paused/cancelled jobs, an
  accepted result, an unresolved attempt, and a retained (unknown-cost) reserve
  exactly;
* the schema-4 job/attempt/result/budget rows are inner-validated **read-only**,
  so a corrupt archive whose outer database hash was rewritten still fails closed
  (wrong-project job, altered result digest, orphan/malformed reserve, tampered
  counters/fence, malformed checkpoint, a missing/altered schema object), and a
  corrupt backup is rejected before any restore.

Run with::

    PYTHONPATH=src python3 -m unittest discover -s tests -p 'test*backup.py'
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from review_article_agent.runtime.jobs import JobRepository  # noqa: E402
from review_article_agent.storage import (  # noqa: E402
    ApprovalRepository,
    ArtifactRepository,
    ProjectStore,
    Registry,
)
from review_article_agent.storage import paths, schema  # noqa: E402
from review_article_agent.storage.backup import (  # noqa: E402
    MANIFEST_FILENAME,
    SNAPSHOT_FILENAME,
    BackupError,
    create_backup,
    restore_backup,
    verify_backup,
)

_OPS = ("acquisition", "storage", "redistribution", "hosted_transfer")


def _sha(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _rights(project_id: str, artifact_id: str, revision_id: str) -> dict:
    """A synthetic, fully valid RightsRecord v1 bound to one revision."""
    return {
        "rights_id": f"RIGHTS-{revision_id}",
        "project_id": project_id,
        "record_revision": 1,
        "subject": {
            "kind": "source_revision",
            "project_id": project_id,
            "artifact_id": artifact_id,
            "revision_id": revision_id,
        },
        "operations": {op: "allowed" for op in _OPS},
        "basis": {
            op: {
                "kind": "public_domain",
                "evidence": {"ref": "lic:pd-syn", "provenance": "publisher:pd-list"},
            }
            for op in _OPS
        },
        "authorization": None,
        "restrictions": [],
        "privacy": {
            "personal_data": False,
            "confidentiality": "public",
            "denial": False,
        },
        "recorded_by": "user",
        "recorded_at": "2026-10-27T00:00:00Z",
        "derived_status": "valid",
    }


class WorkerBackupBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="raa-worker-backup-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    # -- fixtures ------------------------------------------------------------ #
    def new_store(self, name: str = "p") -> ProjectStore:
        return ProjectStore.create_at(
            paths.app_dir_for(str(self.tmp / name)), paths.new_project_id()
        )

    def new_schema3_store(self, name: str = "p3") -> ProjectStore:
        """Create a store pinned at store schema **3** (pre-Step-03)."""
        app_dir = str(self.tmp / name)
        paths.ensure_owner_private_dir(app_dir)
        db_path = os.path.join(app_dir, paths.STORE_FILENAME)
        conn = schema.open_connection(db_path)
        try:
            schema.migrate(
                conn,
                target_version=3,
                migrations=schema.STORE_MIGRATIONS,
                label="project store",
            )
            project_id = paths.new_project_id()
            conn.execute(
                "INSERT INTO project (project_id, name, created_at_utc, app_version) "
                "VALUES (?, 'schema3', '2026-01-01T00:00:00+00:00', '0.0.0')",
                (project_id,),
            )
            conn.execute(
                "INSERT INTO meta (key, value) VALUES ('created_at_utc', ?)",
                ("2026-01-01T00:00:00+00:00",),
            )
        except BaseException:
            conn.close()
            raise
        paths.ensure_owner_private_file(db_path)
        return ProjectStore(
            conn, app_dir, project_id, schema_version=3, app_version="0.0.0"
        )

    def import_revision(self, repo, artifact_id, content, *, revision_id, parent=None):
        return repo.import_revision(
            artifact_id=artifact_id,
            content=content,
            origin_locator="import:synthetic",
            rights=_rights(repo.project_id, artifact_id, revision_id),
            revision_id=revision_id,
            parent_revision_id=parent,
            dependencies=[],
            request_id=None,
        )

    def seed_artifacts(self, store: ProjectStore) -> dict:
        repo = ArtifactRepository(store)
        doc = repo.create_artifact(kind="document")
        d1 = self.import_revision(
            repo, doc.artifact_id, b"doc-v1-exact-bytes", revision_id="rev-d1"
        )
        approvals = ApprovalRepository(store)
        approval = approvals.record_fixture_approval(
            target={
                "project_id": store.project_id,
                "artifact_id": doc.artifact_id,
                "revision_id": d1.revision_id,
            },
            gate_kind="fixture_artifact_revision",
            gate_id="gate-doc",
            decision="approved",
            rationale="synthetic local fixture; not a scientific approval",
        )
        return {"doc": doc, "revision": d1, "approval": approval}

    def seed_jobs(self, store: ProjectStore) -> dict:
        """Create one job per interesting state plus an accepted result/reserve."""
        jobs = JobRepository(store)

        # completed with an accepted result and a settled reserve.
        done = jobs.submit("req-done", task_kind="probe", parameters={"n": 1})
        done_att = jobs.begin_attempt(
            done.job_id,
            dispatch_kind="external_synthetic",
            stage="s1",
            reserve_usd="0.10",
        )
        jobs.mark_admitted(done_att.attempt_id, admission_id="adm-done")
        jobs.mark_running(done_att.attempt_id)
        jobs.accept_result(
            done_att.attempt_id, result_kind="summary", result={"text": "done"}
        )

        # pending (queued, local, no reserve) with a durable checkpoint.
        pending = jobs.submit("req-pending", task_kind="probe")
        pending_att = jobs.begin_attempt(pending.job_id, dispatch_kind="local")
        jobs.record_checkpoint(pending_att.attempt_id, {"step": 2})

        # paused with durable state.
        paused = jobs.submit("req-paused", task_kind="probe")
        jobs.pause(paused.job_id)

        # cancelled.
        cancelled = jobs.submit("req-cancelled", task_kind="probe")
        jobs.cancel(cancelled.job_id)

        # ambiguous external attempt with an unknown cost: reserve retained.
        unresolved = jobs.submit("req-unresolved", task_kind="probe")
        unresolved_att = jobs.begin_attempt(
            unresolved.job_id,
            dispatch_kind="external_synthetic",
            stage="s2",
            reserve_usd="0.25",
        )
        jobs.mark_ambiguous(unresolved_att.attempt_id)
        jobs.settle_cost(unresolved_att.attempt_id, cost_state="unknown")

        # a bounded safe predispatch retry: definitive failure then a local retry.
        retried = jobs.submit("req-retried", task_kind="probe")
        retried_first = jobs.begin_attempt(retried.job_id, dispatch_kind="local")
        jobs.mark_definitive_failure(retried_first.attempt_id)
        retried_second = jobs.begin_attempt(
            retried.job_id, dispatch_kind="local", retry_authorized_by="owner"
        )

        return {
            "done": done,
            "done_attempt": done_att,
            "pending": pending,
            "pending_attempt": pending_att,
            "paused": paused,
            "cancelled": cancelled,
            "unresolved": unresolved,
            "unresolved_attempt": unresolved_att,
            "retried": retried,
            "retried_first": retried_first,
            "retried_second": retried_second,
        }

    # -- tamper helpers ------------------------------------------------------ #
    def rewrite_outer_db_hash(self, backup_dir: Path) -> None:
        manifest_path = backup_dir / MANIFEST_FILENAME
        manifest = json.loads(manifest_path.read_text())
        data = (backup_dir / SNAPSHOT_FILENAME).read_bytes()
        manifest["database"]["sha256"] = _sha(data)
        manifest["database"]["byte_size"] = len(data)
        manifest_path.write_text(json.dumps(manifest))

    def tamper_db(
        self, backup_dir: Path, *statements: str, preserve_schema: bool = False
    ) -> None:
        db = str(backup_dir / SNAPSHOT_FILENAME)
        conn = sqlite3.connect(db)
        try:
            dropped: dict[str, str] = {}
            for statement in statements:
                match = re.match(
                    r"\s*DROP\s+TRIGGER\s+(?:IF\s+EXISTS\s+)?(\S+)\s*;?\s*$",
                    statement,
                    re.IGNORECASE,
                )
                if match:
                    row = conn.execute(
                        "SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?",
                        (match.group(1),),
                    ).fetchone()
                    if row and row[0]:
                        dropped[match.group(1)] = row[0]
                conn.execute(statement)
            if preserve_schema:
                for sql in dropped.values():
                    conn.execute(sql)
            conn.commit()
        finally:
            conn.close()


class Schema3CompatibilityTest(WorkerBackupBase):
    def _schema3_backup(self):
        store = self.new_schema3_store()
        self.addCleanup(store.close)
        seeded = self.seed_artifacts(store)
        dest = self.tmp / "backup3"
        create_backup(store, dest)
        return store, seeded, dest

    def test_schema3_backup_verifies_against_its_own_schema(self) -> None:
        store, seeded, dest = self._schema3_backup()
        report = verify_backup(dest)
        self.assertTrue(report["ok"])
        self.assertEqual(report["store_schema_version"], 3)
        manifest = json.loads((dest / MANIFEST_FILENAME).read_text())
        self.assertEqual(manifest["store_schema_version"], 3)
        self.assertEqual(manifest["format_version"], 1)

    def test_schema3_restore_upgrades_to_schema4_and_preserves_bytes(self) -> None:
        store, seeded, dest = self._schema3_backup()
        project_id = store.project_id
        registry = Registry(self.tmp / "state")
        info = restore_backup(dest, self.tmp / "restored3", registry)
        self.assertEqual(info.project_id, project_id)

        restored = ProjectStore.open_at(
            paths.app_dir_for(info.root_path), expected_project_id=project_id
        )
        try:
            # The ordinary ProjectStore open migrated 3 -> 4.
            self.assertEqual(restored.schema_version, schema.STORE_SCHEMA_VERSION)
            names = {
                row[0]
                for row in restored.connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            self.assertTrue(
                {"job", "job_attempt", "job_accepted_result",
                 "job_budget_policy", "job_budget_reserve"} <= names
            )
            # No jobs existed pre-upgrade.
            self.assertEqual(JobRepository(restored).list(), [])
            # Exact artifact bytes preserved.
            repo = ArtifactRepository(restored)
            self.assertEqual(
                repo.read_content(seeded["doc"].artifact_id, "rev-d1"),
                b"doc-v1-exact-bytes",
            )
            revision = repo.get_revision(seeded["doc"].artifact_id, "rev-d1")
            self.assertEqual(_sha(b"doc-v1-exact-bytes"), revision.content_sha256)
            # Fixture approval snapshot preserved and still verifies.
            approvals = ApprovalRepository(restored)
            stored = approvals.get_approval(seeded["approval"].approval_id)
            self.assertEqual(
                stored.snapshot_digest, seeded["approval"].snapshot_digest
            )
            self.assertEqual(stored.storage_schema_version, 3)
            self.assertTrue(
                approvals.verify_snapshot(seeded["approval"].approval_id).ok
            )
        finally:
            restored.close()


class Schema4RoundTripTest(WorkerBackupBase):
    def test_roundtrip_preserves_all_job_states_results_and_reserves(self) -> None:
        store = self.new_store()
        self.addCleanup(store.close)
        self.seed_artifacts(store)
        seeded = self.seed_jobs(store)
        before_jobs = JobRepository(store)
        before_states = {j.job_id: j.state for j in before_jobs.list()}
        before_accepted = before_jobs.accepted_result(seeded["done"].job_id)
        before_reserves = {
            row["attempt_id"]: (row["amount_usd"], row["state"])
            for row in store.connection.execute(
                "SELECT attempt_id, amount_usd, state FROM job_budget_reserve"
            )
        }
        dest = self.tmp / "backup4"
        create_backup(store, dest)
        report = verify_backup(dest)
        self.assertEqual(report["store_schema_version"], schema.STORE_SCHEMA_VERSION)

        registry = Registry(self.tmp / "state")
        info = restore_backup(dest, self.tmp / "restored4", registry)
        restored = ProjectStore.open_at(
            paths.app_dir_for(info.root_path), expected_project_id=info.project_id
        )
        try:
            jobs = JobRepository(restored)
            states = {j.job_id: j.state for j in jobs.list()}
            self.assertEqual(states[seeded["done"].job_id], "completed")
            self.assertEqual(states[seeded["pending"].job_id], "queued")
            self.assertEqual(states[seeded["paused"].job_id], "paused")
            self.assertEqual(states[seeded["cancelled"].job_id], "cancelled")
            self.assertEqual(
                states[seeded["unresolved"].job_id], "reconciliation_required"
            )
            # Every job state survives exactly as recorded before the backup.
            self.assertEqual(states, before_states)

            # The accepted result survives with the exact digest and payload.
            accepted = jobs.accepted_result(seeded["done"].job_id)
            self.assertEqual(accepted.result, {"text": "done"})
            self.assertEqual(accepted.fence, seeded["done_attempt"].fence)
            self.assertEqual(accepted.result_digest, before_accepted.result_digest)

            # The unresolved attempt is preserved with its unresolved bit.
            attempt = jobs.attempt(seeded["unresolved_attempt"].attempt_id)
            self.assertEqual(attempt.state, "ambiguous")
            self.assertTrue(attempt.outcome_unresolved)
            self.assertEqual(jobs.get(seeded["unresolved"].job_id).outcome_unresolved, True)

            # The unknown-cost reserve is preserved exactly (retained, not zeroed).
            reserves = {
                row["attempt_id"]: (row["amount_usd"], row["state"])
                for row in restored.connection.execute(
                    "SELECT attempt_id, amount_usd, state FROM job_budget_reserve"
                )
            }
            self.assertEqual(reserves, before_reserves)
            self.assertEqual(
                reserves[seeded["unresolved_attempt"].attempt_id], ("0.25", "retained")
            )
            # The durable checkpoint survives exactly.
            self.assertEqual(
                jobs.attempt(seeded["pending_attempt"].attempt_id).checkpoint,
                {"step": 2},
            )
        finally:
            restored.close()


class FailClosedJobVerificationTest(WorkerBackupBase):
    def _schema4_backup(self):
        store = self.new_store()
        self.addCleanup(store.close)
        self.seed_artifacts(store)
        seeded = self.seed_jobs(store)
        dest = self.tmp / "backup4"
        create_backup(store, dest)
        # Sanity: the untampered backup verifies.
        verify_backup(dest)
        return store, seeded, dest

    def test_wrong_project_job_rejected(self) -> None:
        _, _, dest = self._schema4_backup()
        self.tamper_db(
            dest,
            "INSERT INTO job (project_id, job_id, request_id, fingerprint, "
            " task_kind, parameters_json, state, outcome_unresolved, resume_state, "
            " current_attempt_id, current_fence, attempts_total, retries_used, "
            " accepted_fence, created_at_utc, updated_at_utc) VALUES "
            "('prj_00000000000000000000000000000000', 'job_forgedreq', 'req-forged', "
            " 'deadbeef', 'probe', '{}', 'queued', 0, NULL, NULL, 0, 0, 0, NULL, "
            " '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')",
        )
        self.rewrite_outer_db_hash(dest)
        with self.assertRaises(BackupError):
            verify_backup(dest)

    def test_parameter_fingerprint_altered_rejected(self) -> None:
        _, seeded, dest = self._schema4_backup()
        self.tamper_db(
            dest,
            "UPDATE job SET parameters_json = '{\"n\":2}' "
            f"WHERE job_id = '{seeded['done'].job_id}'",
        )
        self.rewrite_outer_db_hash(dest)
        with self.assertRaises(BackupError):
            verify_backup(dest)

    def test_result_digest_altered_rejected(self) -> None:
        _, seeded, dest = self._schema4_backup()
        self.tamper_db(
            dest,
            "DROP TRIGGER job_accepted_result_no_update",
            "UPDATE job_accepted_result SET result_digest = "
            "'" + "0" * 64 + "'",
            preserve_schema=True,
        )
        self.rewrite_outer_db_hash(dest)
        with self.assertRaises(BackupError):
            verify_backup(dest)

    def test_result_payload_altered_rejected(self) -> None:
        _, seeded, dest = self._schema4_backup()
        self.tamper_db(
            dest,
            "DROP TRIGGER job_accepted_result_no_update",
            "UPDATE job_accepted_result SET result_json = '{\"text\":\"forged\"}'",
            preserve_schema=True,
        )
        self.rewrite_outer_db_hash(dest)
        with self.assertRaises(BackupError):
            verify_backup(dest)

    def test_job_counters_tampered_rejected(self) -> None:
        _, seeded, dest = self._schema4_backup()
        self.tamper_db(
            dest,
            "UPDATE job SET attempts_total = attempts_total + 5 "
            f"WHERE job_id = '{seeded['done'].job_id}'",
        )
        self.rewrite_outer_db_hash(dest)
        with self.assertRaises(BackupError):
            verify_backup(dest)

    def test_current_fence_tampered_rejected(self) -> None:
        _, seeded, dest = self._schema4_backup()
        self.tamper_db(
            dest,
            "UPDATE job SET current_fence = current_fence + 1 "
            f"WHERE job_id = '{seeded['done'].job_id}'",
        )
        self.rewrite_outer_db_hash(dest)
        with self.assertRaises(BackupError):
            verify_backup(dest)

    def test_reserve_orphan_rejected(self) -> None:
        _, _, dest = self._schema4_backup()
        self.tamper_db(
            dest,
            "INSERT INTO job_budget_reserve (project_id, reserve_id, job_id, "
            " attempt_id, stage, amount_usd, state, created_at_utc) SELECT "
            " project_id, 'rsv_orphan', 'job_doesnotexist', NULL, 's1', '1.00', "
            " 'held', '2026-01-01T00:00:00+00:00' FROM project LIMIT 1",
        )
        self.rewrite_outer_db_hash(dest)
        with self.assertRaises(BackupError):
            verify_backup(dest)

    def test_reserve_malformed_decimal_rejected(self) -> None:
        _, seeded, dest = self._schema4_backup()
        self.tamper_db(
            dest,
            "DROP TRIGGER job_budget_reserve_amount_immutable",
            "UPDATE job_budget_reserve SET amount_usd = 'not-a-decimal' "
            f"WHERE attempt_id = '{seeded['unresolved_attempt'].attempt_id}'",
            preserve_schema=True,
        )
        self.rewrite_outer_db_hash(dest)
        with self.assertRaises(BackupError):
            verify_backup(dest)

    def test_reserve_amount_mismatch_rejected(self) -> None:
        _, seeded, dest = self._schema4_backup()
        self.tamper_db(
            dest,
            "DROP TRIGGER job_budget_reserve_amount_immutable",
            "UPDATE job_budget_reserve SET amount_usd = '9.99' "
            f"WHERE attempt_id = '{seeded['unresolved_attempt'].attempt_id}'",
            preserve_schema=True,
        )
        self.rewrite_outer_db_hash(dest)
        with self.assertRaises(BackupError):
            verify_backup(dest)

    def test_duplicate_reserve_rejected(self) -> None:
        _, seeded, dest = self._schema4_backup()
        self.tamper_db(
            dest,
            "INSERT INTO job_budget_reserve (project_id, reserve_id, job_id, "
            " attempt_id, stage, amount_usd, state, created_at_utc) SELECT "
            " project_id, 'rsv_dup', job_id, attempt_id, stage, amount_usd, state, "
            " created_at_utc FROM job_budget_reserve "
            f"WHERE attempt_id = '{seeded['unresolved_attempt'].attempt_id}'",
        )
        self.rewrite_outer_db_hash(dest)
        with self.assertRaises(BackupError):
            verify_backup(dest)

    def test_malformed_checkpoint_rejected(self) -> None:
        _, seeded, dest = self._schema4_backup()
        self.tamper_db(
            dest,
            "UPDATE job_attempt SET checkpoint_json = '{not json' "
            f"WHERE attempt_id = '{seeded['unresolved_attempt'].attempt_id}'",
        )
        self.rewrite_outer_db_hash(dest)
        with self.assertRaises(BackupError):
            verify_backup(dest)

    def test_dropped_job_trigger_rejected(self) -> None:
        _, _, dest = self._schema4_backup()
        self.tamper_db(dest, "DROP TRIGGER job_accepted_result_no_delete")
        self.rewrite_outer_db_hash(dest)
        with self.assertRaises(BackupError) as ctx:
            verify_backup(dest)
        self.assertIn("schema", str(ctx.exception))

    def test_added_hidden_job_column_rejected(self) -> None:
        _, _, dest = self._schema4_backup()
        self.tamper_db(dest, "ALTER TABLE job ADD COLUMN secret TEXT")
        self.rewrite_outer_db_hash(dest)
        with self.assertRaises(BackupError) as ctx:
            verify_backup(dest)
        self.assertIn("schema", str(ctx.exception))

    def test_reserve_attempt_other_job_rejected(self) -> None:
        _, seeded, dest = self._schema4_backup()
        # A reserve whose attempt belongs to a *different* job (scope violation).
        self.tamper_db(
            dest,
            "INSERT INTO job_budget_reserve (project_id, reserve_id, job_id, "
            " attempt_id, stage, amount_usd, state, created_at_utc) SELECT "
            f"project_id, 'rsv_scope', '{seeded['unresolved'].job_id}', "
            f"'{seeded['done_attempt'].attempt_id}', 's1', '0.10', 'held', "
            "'2026-01-01T00:00:00+00:00' FROM project LIMIT 1",
        )
        self.rewrite_outer_db_hash(dest)
        with self.assertRaises(BackupError):
            verify_backup(dest)

    def test_dangling_accepted_result_attempt_rejected(self) -> None:
        _, seeded, dest = self._schema4_backup()
        self.tamper_db(
            dest,
            "DROP TRIGGER job_accepted_result_no_update",
            "UPDATE job_accepted_result SET attempt_id = 'att_doesnotexist'",
            preserve_schema=True,
        )
        self.rewrite_outer_db_hash(dest)
        with self.assertRaises(BackupError):
            verify_backup(dest)

    def test_corrupt_archive_rejected_before_restore(self) -> None:
        _, seeded, dest = self._schema4_backup()
        self.tamper_db(
            dest,
            "DROP TRIGGER job_accepted_result_no_update",
            "UPDATE job_accepted_result SET result_digest = '" + "0" * 64 + "'",
            preserve_schema=True,
        )
        self.rewrite_outer_db_hash(dest)
        registry = Registry(self.tmp / "state")
        with self.assertRaises(BackupError):
            restore_backup(dest, self.tmp / "never", registry)
        self.assertFalse(os.path.exists(paths.app_dir_for(str(self.tmp / "never"))))
        self.assertEqual(registry.list_projects(), [])


class ZeroCostBudgetTest(WorkerBackupBase):
    """A reported zero actual cost is valid and must not be rejected."""

    def _store_with_settled(self, cost_state: str):
        store = self.new_store()
        self.addCleanup(store.close)
        jobs = JobRepository(store)
        job = jobs.submit(f"req-{cost_state}", task_kind="probe")
        attempt = jobs.begin_attempt(
            job.job_id,
            dispatch_kind="external_synthetic",
            stage="s1",
            reserve_usd="0.10",
        )
        jobs.settle_cost(attempt.attempt_id, cost_state=cost_state, amount_usd="0")
        return store, jobs, job, attempt

    def test_known_zero_actual_cost_roundtrips(self) -> None:
        store, jobs, job, attempt = self._store_with_settled("known")
        dest = self.tmp / "backup-known0"
        create_backup(store, dest)
        report = verify_backup(dest)
        self.assertTrue(report["ok"])
        registry = Registry(self.tmp / "state")
        info = restore_backup(dest, self.tmp / "restored-known0", registry)
        restored = ProjectStore.open_at(
            paths.app_dir_for(info.root_path), expected_project_id=info.project_id
        )
        try:
            rjobs = JobRepository(restored)
            restored_attempt = rjobs.attempt(attempt.attempt_id)
            self.assertEqual(restored_attempt.cost_state, "known")
            self.assertEqual(restored_attempt.cost_amount_usd, "0")
            # The immutable reserve upper bound is retained.
            self.assertEqual(restored_attempt.reserve_usd, "0.10")
            self.assertEqual(rjobs.get(job.job_id).state, "queued")
        finally:
            restored.close()

    def test_estimated_zero_retains_reserve_roundtrip(self) -> None:
        store, jobs, job, attempt = self._store_with_settled("estimated")
        # An estimated amount settles the reserve to 'retained', never zeroed.
        reserve_before = store.connection.execute(
            "SELECT amount_usd, state FROM job_budget_reserve WHERE attempt_id = ?",
            (attempt.attempt_id,),
        ).fetchone()
        self.assertEqual((reserve_before["amount_usd"], reserve_before["state"]), ("0.10", "retained"))
        dest = self.tmp / "backup-est0"
        create_backup(store, dest)
        verify_backup(dest)
        registry = Registry(self.tmp / "state")
        info = restore_backup(dest, self.tmp / "restored-est0", registry)
        restored = ProjectStore.open_at(
            paths.app_dir_for(info.root_path), expected_project_id=info.project_id
        )
        try:
            row = restored.connection.execute(
                "SELECT amount_usd, state FROM job_budget_reserve WHERE attempt_id = ?",
                (attempt.attempt_id,),
            ).fetchone()
            self.assertEqual((row["amount_usd"], row["state"]), ("0.10", "retained"))
            restored_attempt = JobRepository(restored).attempt(attempt.attempt_id)
            self.assertEqual(restored_attempt.cost_state, "estimated")
            self.assertEqual(restored_attempt.cost_amount_usd, "0")
        finally:
            restored.close()


class HeldAcceptedResultTest(WorkerBackupBase):
    """A paused/cancelled job may legitimately hold an accepted result."""

    def _held_accepted_backup(self, *, hold: str):
        store = self.new_store()
        self.addCleanup(store.close)
        jobs = JobRepository(store)
        job = jobs.submit(f"req-{hold}", task_kind="probe")
        attempt = jobs.begin_attempt(
            job.job_id,
            dispatch_kind="external_synthetic",
            stage="s1",
            reserve_usd="0.10",
        )
        jobs.mark_admitted(attempt.attempt_id, admission_id="adm-held")
        jobs.mark_running(attempt.attempt_id)
        if hold == "pause":
            jobs.pause(job.job_id)
        else:
            jobs.cancel(job.job_id)
        # Accepting while held retains the result and preserves the held state.
        accepted = jobs.accept_result(
            attempt.attempt_id, result_kind="summary", result={"ok": True}
        )
        expected_state = "paused" if hold == "pause" else "cancelled"
        self.assertEqual(jobs.get(job.job_id).state, expected_state)
        dest = self.tmp / f"backup-{hold}"
        create_backup(store, dest)
        verify_backup(dest)
        return store, jobs, job, attempt, accepted, dest

    def test_paused_job_accepted_result_roundtrips(self) -> None:
        store, jobs, job, attempt, accepted, dest = self._held_accepted_backup(hold="pause")
        registry = Registry(self.tmp / "state")
        info = restore_backup(dest, self.tmp / "restored-pause-acc", registry)
        restored = ProjectStore.open_at(
            paths.app_dir_for(info.root_path), expected_project_id=info.project_id
        )
        try:
            rjobs = JobRepository(restored)
            self.assertEqual(rjobs.get(job.job_id).state, "paused")
            self.assertEqual(rjobs.get(job.job_id).resume_state, "running")
            stored = rjobs.accepted_result(job.job_id)
            self.assertEqual(stored.result, {"ok": True})
            self.assertEqual(stored.result_digest, accepted.result_digest)
            self.assertFalse(rjobs.get(job.job_id).outcome_unresolved)
        finally:
            restored.close()

    def test_cancelled_job_accepted_result_roundtrips(self) -> None:
        store, jobs, job, attempt, accepted, dest = self._held_accepted_backup(hold="cancel")
        registry = Registry(self.tmp / "state")
        info = restore_backup(dest, self.tmp / "restored-cancel-acc", registry)
        restored = ProjectStore.open_at(
            paths.app_dir_for(info.root_path), expected_project_id=info.project_id
        )
        try:
            rjobs = JobRepository(restored)
            self.assertEqual(rjobs.get(job.job_id).state, "cancelled")
            stored = rjobs.accepted_result(job.job_id)
            self.assertEqual(stored.result, {"ok": True})
            self.assertFalse(rjobs.get(job.job_id).outcome_unresolved)
        finally:
            restored.close()


class LifecycleFailClosedTest(WorkerBackupBase):
    """Reject forged lifecycle/flag/retry states before any restore."""

    def _schema4_backup(self):
        store = self.new_store()
        self.addCleanup(store.close)
        self.seed_artifacts(store)
        seeded = self.seed_jobs(store)
        dest = self.tmp / "backup4"
        create_backup(store, dest)
        verify_backup(dest)
        return store, seeded, dest

    def _reject(self, dest: Path) -> None:
        self.rewrite_outer_db_hash(dest)
        registry = Registry(self.tmp / "state")
        with self.assertRaises(BackupError):
            verify_backup(dest)
        # And a corrupt archive is rejected before any restore target is created.
        with self.assertRaises(BackupError):
            restore_backup(dest, self.tmp / "never", registry)
        self.assertFalse(os.path.exists(paths.app_dir_for(str(self.tmp / "never"))))
        self.assertEqual(registry.list_projects(), [])

    def test_completed_job_without_accepted_result_rejected(self) -> None:
        _, seeded, dest = self._schema4_backup()
        self.tamper_db(
            dest,
            "DROP TRIGGER job_accepted_result_no_delete",
            "DELETE FROM job_accepted_result",
            f"UPDATE job SET accepted_fence = NULL WHERE job_id = "
            f"'{seeded['done'].job_id}'",
            preserve_schema=True,
        )
        self._reject(dest)

    def test_succeeded_attempt_without_accepted_result_rejected(self) -> None:
        _, seeded, dest = self._schema4_backup()
        self.tamper_db(
            dest,
            "UPDATE job_attempt SET state = 'succeeded', ended_at_utc = "
            "'2026-01-01T00:00:00+00:00' "
            f"WHERE attempt_id = '{seeded['pending_attempt'].attempt_id}'",
        )
        self._reject(dest)

    def test_accepted_result_on_nonterminal_job_rejected(self) -> None:
        _, seeded, dest = self._schema4_backup()
        self.tamper_db(
            dest,
            f"UPDATE job SET state = 'running' WHERE job_id = "
            f"'{seeded['done'].job_id}'",
        )
        self._reject(dest)

    def test_job_unresolved_flag_mismatch_rejected(self) -> None:
        _, seeded, dest = self._schema4_backup()
        self.tamper_db(
            dest,
            f"UPDATE job SET outcome_unresolved = 1 WHERE job_id = "
            f"'{seeded['pending'].job_id}'",
        )
        self._reject(dest)

    def test_job_without_attempt_claiming_unresolved_rejected(self) -> None:
        _, seeded, dest = self._schema4_backup()
        self.tamper_db(
            dest,
            f"UPDATE job SET outcome_unresolved = 1 WHERE job_id = "
            f"'{seeded['paused'].job_id}'",
        )
        self._reject(dest)

    def test_retry_after_ambiguous_prior_rejected(self) -> None:
        _, seeded, dest = self._schema4_backup()
        self.tamper_db(
            dest,
            "UPDATE job_attempt SET state = 'ambiguous', outcome_unresolved = 1 "
            f"WHERE attempt_id = '{seeded['retried_first'].attempt_id}'",
        )
        self._reject(dest)

    def test_retry_after_admitted_prior_rejected(self) -> None:
        _, seeded, dest = self._schema4_backup()
        self.tamper_db(
            dest,
            "UPDATE job_attempt SET admission_id = 'adm-prior' "
            f"WHERE attempt_id = '{seeded['retried_first'].attempt_id}'",
        )
        self._reject(dest)

    def test_retry_after_succeeded_prior_rejected(self) -> None:
        _, seeded, dest = self._schema4_backup()
        self.tamper_db(
            dest,
            "UPDATE job_attempt SET state = 'succeeded' "
            f"WHERE attempt_id = '{seeded['retried_first'].attempt_id}'",
        )
        self._reject(dest)

    def test_retries_used_over_bound_rejected(self) -> None:
        _, seeded, dest = self._schema4_backup()
        job_id = seeded["retried"].job_id
        # A forged 4-attempt chain: structurally chained but over the retry bound.
        self.tamper_db(
            dest,
            "UPDATE job_attempt SET state = 'definitive_failure' "
            f"WHERE attempt_id = '{seeded['retried_second'].attempt_id}'",
            "INSERT INTO job_attempt (project_id, attempt_id, job_id, fence, "
            " attempt_no, dispatch_kind, state, prior_outcome, retry_authorized_by, "
            " outcome_unresolved, admission_id, external_ref, checkpoint_json, "
            " reserve_usd, cost_state, cost_amount_usd, started_at_utc, ended_at_utc) "
            "SELECT project_id, 'att_forged_3', job_id, 3, 3, 'local', "
            "'definitive_failure', 'definitive_failure', 'owner', 0, NULL, NULL, NULL, "
            "NULL, 'not_incurred', NULL, '2026-01-01T00:00:00+00:00', NULL "
            f"FROM job WHERE job_id = '{job_id}'",
            "INSERT INTO job_attempt (project_id, attempt_id, job_id, fence, "
            " attempt_no, dispatch_kind, state, prior_outcome, retry_authorized_by, "
            " outcome_unresolved, admission_id, external_ref, checkpoint_json, "
            " reserve_usd, cost_state, cost_amount_usd, started_at_utc, ended_at_utc) "
            "SELECT project_id, 'att_forged_4', job_id, 4, 4, 'local', 'queued', "
            "'definitive_failure', 'owner', 0, NULL, NULL, NULL, NULL, "
            "'not_incurred', NULL, '2026-01-01T00:00:00+00:00', NULL "
            f"FROM job WHERE job_id = '{job_id}'",
            f"UPDATE job SET attempts_total = 4, retries_used = 3, current_fence = 4, "
            f"current_attempt_id = 'att_forged_4', state = 'queued', "
            f"outcome_unresolved = 0 WHERE job_id = '{job_id}'",
        )
        self._reject(dest)

    def test_deep_json_checkpoint_rejected_cleanly(self) -> None:
        _, seeded, dest = self._schema4_backup()
        # Deep enough that ``json.loads`` itself raises RecursionError; deep and
        # long enough that the diagnostic must never echo the raw payload.
        deep = "[" * 150000 + "]" * 150000
        self.tamper_db(
            dest,
            "UPDATE job_attempt SET checkpoint_json = '" + deep + "' "
            f"WHERE attempt_id = '{seeded['pending_attempt'].attempt_id}'",
        )
        self.rewrite_outer_db_hash(dest)
        with self.assertRaises(BackupError) as ctx:
            verify_backup(dest)
        # The diagnostic never leaks the raw (deep) payload.
        self.assertNotIn(deep, str(ctx.exception))
        self.assertNotIn("[", str(ctx.exception))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
