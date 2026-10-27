"""Acceptance tests for local storage-fixture approvals and exact snapshots.

Synthetic data only (no secrets, no licensed source text). These tests assert
storage/identity lifecycle behavior for a **local, non-authorizing** approval
fixture layer. They claim **no** scientific validity, no human authentication, and
no approval of any real gate.

Run with::

    PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_approvals.py'
"""

from __future__ import annotations

import hashlib
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from review_article_agent.domain.records import RecordValidationError  # noqa: E402
from review_article_agent.storage import (  # noqa: E402
    ApprovalRepository,
    ArtifactRepository,
    ProjectStore,
    RightsRef,
)
from review_article_agent.storage import paths, schema  # noqa: E402
from review_article_agent.storage.approvals import (  # noqa: E402
    FIXTURE_GATE_KINDS,
    ApprovalRepository as _ApprovalRepositoryDirect,
)

_OPS = ("acquisition", "storage", "redistribution", "hosted_transfer")


def _sha(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _valid_rights(project_id, artifact_id, revision_id, **overrides):
    """A synthetic, fully valid RightsRecord v1 bound to one revision."""
    record = {
        "rights_id": overrides.pop("rights_id", f"RIGHTS-{revision_id}"),
        "project_id": project_id,
        "record_revision": overrides.pop("record_revision", 1),
        "subject": {
            "kind": "source_revision",
            "project_id": project_id,
            "artifact_id": artifact_id,
            "revision_id": revision_id,
        },
        "operations": {
            "acquisition": "allowed",
            "storage": "allowed",
            "redistribution": "allowed",
            "hosted_transfer": "allowed",
        },
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
        "recorded_at": "2026-10-27T00:00:00Z",
        "derived_status": "valid",
    }
    record.update(overrides)
    return record


class ApprovalTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="raa-approvals-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def new_store(self, name: str = "p") -> ProjectStore:
        root = self.tmp / name
        return ProjectStore.create_at(paths.app_dir_for(str(root)), paths.new_project_id())

    def import_revision(self, repo, artifact_id, content, *, revision_id, dependencies=None):
        return repo.import_revision(
            artifact_id=artifact_id,
            content=content,
            origin_locator="import:synthetic",
            rights=_valid_rights(repo.project_id, artifact_id, revision_id),
            revision_id=revision_id,
            dependencies=dependencies or [],
        )

    def target(self, store, artifact_id, revision_id):
        return {
            "project_id": store.project_id,
            "artifact_id": artifact_id,
            "revision_id": revision_id,
        }


class SnapshotRecordTest(ApprovalTestBase):
    def test_record_and_reopen_exact_snapshot(self) -> None:
        app_root = str(self.tmp / "p")
        store = ProjectStore.create_at(paths.app_dir_for(app_root), paths.new_project_id())
        pid = store.project_id
        try:
            repo = ArtifactRepository(store)
            art = repo.create_artifact(kind="document")
            rev = self.import_revision(repo, art.artifact_id, b"doc-bytes", revision_id="rev-1")
            approvals = ApprovalRepository(store)
            appr = approvals.record_fixture_approval(
                target=self.target(store, art.artifact_id, rev.revision_id),
                gate_kind="fixture_artifact_revision",
                gate_id="gate-1",
                decision="approved",
                rationale="synthetic local check fixture; not a scientific approval",
            )
            self.assertEqual(appr.scope, "storage_fixture")
            self.assertFalse(appr.authorizes_execution)
            self.assertFalse(appr.actor_authenticated)
            self.assertEqual(appr.storage_schema_version, schema.STORE_SCHEMA_VERSION)
            self.assertEqual(appr.context_kind, "human_authored")
            self.assertEqual(appr.target, self.target(store, art.artifact_id, "rev-1"))
            entries = approvals.snapshot_entries(appr.approval_id)
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0].role, "target")
            self.assertEqual(entries[0].content_sha256, _sha(b"doc-bytes"))
            self.assertEqual(entries[0].rights_id, "RIGHTS-rev-1")
            self.assertTrue(approvals.verify_snapshot(appr.approval_id).ok)
        finally:
            store.close()

        # Reopen: the snapshot is byte-for-byte reproducible and still active.
        with ProjectStore.open_at(paths.app_dir_for(app_root)) as reopened:
            approvals = ApprovalRepository(reopened)
            again = approvals.get_approval(appr.approval_id)
            self.assertIsNotNone(again)
            self.assertEqual(again.snapshot_digest, appr.snapshot_digest)
            self.assertEqual(again.decision, "approved")
            self.assertEqual(
                [e.as_dict() for e in approvals.snapshot_entries(appr.approval_id)],
                [e.as_dict() for e in entries],
            )
            status = approvals.evaluate(appr.approval_id)
            self.assertEqual(status.status, "active")
            self.assertFalse(status.authorizes_execution)
            self.assertFalse(status.authorizes_export)

    def test_empty_dependency_artifact_still_has_exact_target_entry(self) -> None:
        with self.new_store() as store:
            repo = ArtifactRepository(store)
            art = repo.create_artifact(kind="document")
            rev = self.import_revision(repo, art.artifact_id, b"only", revision_id="rev-1")
            approvals = ApprovalRepository(store)
            appr = approvals.record_fixture_approval(
                target=self.target(store, art.artifact_id, rev.revision_id),
                gate_kind="fixture_batch",
                gate_id="g",
                decision="approved",
                rationale="no dependencies but still exact",
            )
            entries = approvals.snapshot_entries(appr.approval_id)
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0].artifact_id, art.artifact_id)

    def test_transitive_dependency_closure_is_recorded(self) -> None:
        with self.new_store() as store:
            repo = ArtifactRepository(store)
            base = repo.create_artifact(kind="document")
            base_rev = self.import_revision(repo, base.artifact_id, b"base", revision_id="rev-base")
            finding = repo.create_artifact(kind="finding")
            find_rev = self.import_revision(
                repo, finding.artifact_id, b"find", revision_id="rev-find",
                dependencies=[self.target(store, base.artifact_id, base_rev.revision_id)],
            )
            report = repo.create_artifact(kind="report")
            rep_rev = self.import_revision(
                repo, report.artifact_id, b"rep", revision_id="rev-rep",
                dependencies=[self.target(store, finding.artifact_id, find_rev.revision_id)],
            )
            approvals = ApprovalRepository(store)
            appr = approvals.record_fixture_approval(
                target=self.target(store, report.artifact_id, rep_rev.revision_id),
                gate_kind="fixture_review",
                gate_id="g",
                decision="approved",
                rationale="transitive closure",
            )
            entries = approvals.snapshot_entries(appr.approval_id)
            got = {(e.artifact_id, e.revision_id, e.role) for e in entries}
            self.assertEqual(
                got,
                {
                    (report.artifact_id, "rev-rep", "target"),
                    (finding.artifact_id, "rev-find", "finding"),
                    (base.artifact_id, "rev-base", "source_document"),
                },
            )
            # Target is entry 0.
            self.assertEqual(entries[0].artifact_id, report.artifact_id)
            self.assertEqual(entries[0].role, "target")


class NoSelfGrantTest(ApprovalTestBase):
    def test_generic_import_payload_never_creates_approval(self) -> None:
        with self.new_store() as store:
            repo = ArtifactRepository(store)
            art = repo.create_artifact(kind="report")
            # An approval-shaped generic envelope imported as an ordinary revision.
            repo.import_revision(
                artifact_id=art.artifact_id,
                content={
                    "schema_version": 1,
                    "document_type": "fixture_approval",
                    "payload": {
                        "scope": "storage_fixture",
                        "authorizes_execution": True,
                        "decision": "approved",
                        "actor": "human",
                        "actor_authenticated": True,
                    },
                },
                origin_locator="import:approval-shaped",
                rights=_valid_rights(store.project_id, art.artifact_id, "rev-x"),
                revision_id="rev-x",
            )
            approvals = ApprovalRepository(store)
            self.assertEqual(approvals.list_approvals(), [])
            count = store.connection.execute(
                "SELECT COUNT(*) FROM fixture_approval"
            ).fetchone()[0]
            self.assertEqual(count, 0)

    def test_in_band_authority_keywords_are_rejected(self) -> None:
        with self.new_store() as store:
            repo = ArtifactRepository(store)
            art = repo.create_artifact(kind="document")
            rev = self.import_revision(repo, art.artifact_id, b"x", revision_id="rev-1")
            approvals = ApprovalRepository(store)
            base_kwargs = dict(
                target=self.target(store, art.artifact_id, rev.revision_id),
                gate_kind="fixture_batch",
                gate_id="g",
                decision="approved",
                rationale="r",
            )
            with self.assertRaises(TypeError):
                approvals.record_fixture_approval(**base_kwargs, actor_kind="human")
            with self.assertRaises(TypeError):
                approvals.record_fixture_approval(**base_kwargs, authority=True)
            with self.assertRaises(TypeError):
                approvals.record_fixture_approval(**base_kwargs, granted_by={"actor_kind": "human"})
            self.assertEqual(approvals.list_approvals(), [])


class RejectionTest(ApprovalTestBase):
    def _store_with_doc(self):
        store = self.new_store()
        self.addCleanup(store.close)
        repo = ArtifactRepository(store)
        art = repo.create_artifact(kind="document")
        rev = self.import_revision(repo, art.artifact_id, b"bytes", revision_id="rev-1")
        return store, art, rev

    def test_cross_project_target_rejected_atomically(self) -> None:
        store, art, rev = self._store_with_doc()
        approvals = ApprovalRepository(store)
        foreign = {
            "project_id": "prj_" + "f" * 32,
            "artifact_id": art.artifact_id,
            "revision_id": rev.revision_id,
        }
        with self.assertRaises(RecordValidationError):
            approvals.record_fixture_approval(
                target=foreign, gate_kind="fixture_batch", gate_id="g",
                decision="approved", rationale="r",
            )
        self.assertEqual(approvals.list_approvals(), [])
        self.assertEqual(
            store.connection.execute("SELECT COUNT(*) FROM fixture_approval_entry").fetchone()[0],
            0,
        )

    def test_missing_target_revision_rejected_atomically(self) -> None:
        store, art, rev = self._store_with_doc()
        approvals = ApprovalRepository(store)
        with self.assertRaises(RecordValidationError):
            approvals.record_fixture_approval(
                target=self.target(store, art.artifact_id, "rev-does-not-exist"),
                gate_kind="fixture_batch", gate_id="g", decision="approved", rationale="r",
            )
        self.assertEqual(approvals.list_approvals(), [])
        self.assertEqual(
            store.connection.execute("SELECT COUNT(*) FROM fixture_approval_entry").fetchone()[0],
            0,
        )

    def test_bad_or_unset_rationale_rejected(self) -> None:
        store, art, rev = self._store_with_doc()
        approvals = ApprovalRepository(store)
        for bad in ("", "   ", None):
            with self.assertRaises(RecordValidationError):
                approvals.record_fixture_approval(
                    target=self.target(store, art.artifact_id, rev.revision_id),
                    gate_kind="fixture_batch", gate_id="g", decision="approved", rationale=bad,
                )
        with self.assertRaises(TypeError):
            approvals.record_fixture_approval(
                target=self.target(store, art.artifact_id, rev.revision_id),
                gate_kind="fixture_batch", gate_id="g", decision="approved",
            )  # missing rationale keyword
        self.assertEqual(approvals.list_approvals(), [])

    def test_bad_decision_and_gate_kind_rejected(self) -> None:
        store, art, rev = self._store_with_doc()
        approvals = ApprovalRepository(store)
        with self.assertRaises(RecordValidationError):
            approvals.record_fixture_approval(
                target=self.target(store, art.artifact_id, rev.revision_id),
                gate_kind="fixture_batch", gate_id="g", decision="maybe", rationale="r",
            )
        with self.assertRaises(RecordValidationError):
            approvals.record_fixture_approval(
                target=self.target(store, art.artifact_id, rev.revision_id),
                gate_kind="not_a_gate", gate_id="g", decision="approved", rationale="r",
            )
        self.assertEqual(approvals.list_approvals(), [])

    def test_real_scientific_gate_kinds_are_not_exposed(self) -> None:
        for real in ("protocol", "pilot", "final_release"):
            self.assertNotIn(real, FIXTURE_GATE_KINDS)
        store, art, rev = self._store_with_doc()
        approvals = ApprovalRepository(store)
        with self.assertRaises(RecordValidationError):
            approvals.record_fixture_approval(
                target=self.target(store, art.artifact_id, rev.revision_id),
                gate_kind="final_release", gate_id="g", decision="approved", rationale="r",
            )


class StalenessTest(ApprovalTestBase):
    def test_target_edit_makes_approval_stale(self) -> None:
        with self.new_store() as store:
            repo = ArtifactRepository(store)
            art = repo.create_artifact(kind="report")
            r1 = self.import_revision(repo, art.artifact_id, b"v1", revision_id="rev-1")
            approvals = ApprovalRepository(store)
            appr = approvals.record_fixture_approval(
                target=self.target(store, art.artifact_id, r1.revision_id),
                gate_kind="fixture_review", gate_id="g", decision="approved", rationale="r",
            )
            self.assertEqual(approvals.evaluate(appr.approval_id).status, "active")
            # Edit the target: a new revision; the old approval becomes stale.
            r2 = self.import_revision(
                repo, art.artifact_id, b"v2", revision_id="rev-2",
                dependencies=None,
            )
            # parent link to express an edit of the same artifact.
            self.assertNotEqual(r1.revision_id, r2.revision_id)
            status = approvals.evaluate(appr.approval_id)
            self.assertEqual(status.status, "stale")
            self.assertFalse(status.authorizes_execution)
            # The new revision is a proposal: no approval covers it.
            current = approvals.current_approval(
                art.artifact_id, gate_kind="fixture_review", gate_id="g"
            )
            self.assertEqual(current.approval_id, appr.approval_id)
            self.assertEqual(approvals.evaluate(current.approval_id).status, "stale")

    def test_dependency_edit_makes_stale_transitively_and_leaves_unrelated_active(self) -> None:
        with self.new_store() as store:
            repo = ArtifactRepository(store)
            base = repo.create_artifact(kind="document")
            base_rev = self.import_revision(repo, base.artifact_id, b"base", revision_id="rev-base")
            finding = repo.create_artifact(kind="finding")
            find_rev = self.import_revision(
                repo, finding.artifact_id, b"find", revision_id="rev-find",
                dependencies=[self.target(store, base.artifact_id, base_rev.revision_id)],
            )
            report = repo.create_artifact(kind="report")
            rep_rev = self.import_revision(
                repo, report.artifact_id, b"rep", revision_id="rev-rep",
                dependencies=[self.target(store, finding.artifact_id, find_rev.revision_id)],
            )
            unrelated = repo.create_artifact(kind="rubric")
            unrel_rev = self.import_revision(
                repo, unrelated.artifact_id, b"rub", revision_id="rev-rub"
            )
            approvals = ApprovalRepository(store)
            t_approval = approvals.record_fixture_approval(
                target=self.target(store, report.artifact_id, rep_rev.revision_id),
                gate_kind="fixture_review", gate_id="g-t", decision="approved", rationale="report",
            )
            m_approval = approvals.record_fixture_approval(
                target=self.target(store, finding.artifact_id, find_rev.revision_id),
                gate_kind="fixture_review", gate_id="g-m", decision="approved", rationale="finding",
            )
            u_approval = approvals.record_fixture_approval(
                target=self.target(store, unrelated.artifact_id, unrel_rev.revision_id),
                gate_kind="fixture_batch", gate_id="g-u", decision="approved", rationale="unrelated",
            )
            self.assertEqual(approvals.evaluate(t_approval.approval_id).status, "active")
            self.assertEqual(approvals.evaluate(m_approval.approval_id).status, "active")
            self.assertEqual(approvals.evaluate(u_approval.approval_id).status, "active")

            # Edit the deepest dependency.
            self.import_revision(
                repo, base.artifact_id, b"base-v2", revision_id="rev-base-2"
            )
            self.assertEqual(approvals.evaluate(t_approval.approval_id).status, "stale")
            self.assertEqual(approvals.evaluate(m_approval.approval_id).status, "stale")
            # Unrelated work is preserved.
            self.assertEqual(approvals.evaluate(u_approval.approval_id).status, "active")

    def test_reapproval_supersedes_old_without_overwriting(self) -> None:
        with self.new_store() as store:
            repo = ArtifactRepository(store)
            art = repo.create_artifact(kind="report")
            rev = self.import_revision(repo, art.artifact_id, b"v1", revision_id="rev-1")
            approvals = ApprovalRepository(store)
            first = approvals.record_fixture_approval(
                target=self.target(store, art.artifact_id, rev.revision_id),
                gate_kind="fixture_review", gate_id="g", decision="approved", rationale="first",
            )
            second = approvals.record_fixture_approval(
                target=self.target(store, art.artifact_id, rev.revision_id),
                gate_kind="fixture_review", gate_id="g", decision="approved", rationale="second",
            )
            self.assertNotEqual(first.approval_id, second.approval_id)
            self.assertEqual(len(approvals.list_approvals()), 2)
            self.assertEqual(approvals.evaluate(second.approval_id).status, "active")
            old = approvals.evaluate(first.approval_id)
            self.assertEqual(old.status, "superseded")
            self.assertEqual(old.superseded_by, second.approval_id)
            # The old row is not overwritten: decision/rationale intact.
            raw = approvals.get_approval(first.approval_id)
            self.assertEqual(raw.rationale, "first")
            self.assertEqual(raw.decision, "approved")

    def test_historical_rows_are_immutable(self) -> None:
        with self.new_store() as store:
            repo = ArtifactRepository(store)
            art = repo.create_artifact(kind="document")
            rev = self.import_revision(repo, art.artifact_id, b"v", revision_id="rev-1")
            approvals = ApprovalRepository(store)
            appr = approvals.record_fixture_approval(
                target=self.target(store, art.artifact_id, rev.revision_id),
                gate_kind="fixture_batch", gate_id="g", decision="approved", rationale="r",
            )
            conn = store.connection
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "UPDATE fixture_approval SET decision = 'rejected' WHERE approval_id = ?",
                    (appr.approval_id,),
                )
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "DELETE FROM fixture_approval WHERE approval_id = ?", (appr.approval_id,)
                )
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "UPDATE fixture_approval_entry SET content_sha256 = 'x' WHERE approval_id = ?",
                    (appr.approval_id,),
                )
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "DELETE FROM fixture_approval_entry WHERE approval_id = ?",
                    (appr.approval_id,),
                )

    def test_stale_history_still_structurally_verifies(self) -> None:
        with self.new_store() as store:
            repo = ArtifactRepository(store)
            art = repo.create_artifact(kind="document")
            rev = self.import_revision(repo, art.artifact_id, b"v1", revision_id="rev-1")
            approvals = ApprovalRepository(store)
            appr = approvals.record_fixture_approval(
                target=self.target(store, art.artifact_id, rev.revision_id),
                gate_kind="fixture_batch", gate_id="g", decision="approved", rationale="r",
            )
            self.import_revision(repo, art.artifact_id, b"v2", revision_id="rev-2")
            self.assertEqual(approvals.evaluate(appr.approval_id).status, "stale")
            # A valid historical (stale) row still verifies structurally.
            verification = approvals.verify_snapshot(appr.approval_id)
            self.assertTrue(verification.ok)
            self.assertEqual(verification.stored_digest, verification.recomputed_digest)
            # And it is still reconstructible for a backup verifier.
            structure = approvals.snapshot_structure(appr.approval_id)
            self.assertEqual(structure["project_id"], store.project_id)
            self.assertEqual(len(structure["entries"]), 1)

    def test_corrupt_stored_snapshot_digest_invalidates(self) -> None:
        with self.new_store() as store:
            repo = ArtifactRepository(store)
            art = repo.create_artifact(kind="document")
            rev = self.import_revision(repo, art.artifact_id, b"v", revision_id="rev-1")
            approvals = ApprovalRepository(store)
            appr = approvals.record_fixture_approval(
                target=self.target(store, art.artifact_id, rev.revision_id),
                gate_kind="fixture_batch", gate_id="g", decision="approved", rationale="r",
            )
            # Simulate out-of-band tamper/corruption of the stored digest.
            store.connection.execute("DROP TRIGGER fixture_approval_no_update")
            store.connection.execute(
                "UPDATE fixture_approval SET snapshot_digest = ? WHERE approval_id = ?",
                ("sha256:" + "0" * 64, appr.approval_id),
            )
            status = approvals.evaluate(appr.approval_id)
            self.assertEqual(status.status, "invalidated")
            self.assertFalse(status.authorizes_execution)
            self.assertFalse(approvals.verify_snapshot(appr.approval_id).ok)

    def test_corrupt_revision_content_invalidates(self) -> None:
        with self.new_store() as store:
            repo = ArtifactRepository(store)
            art = repo.create_artifact(kind="document")
            rev = self.import_revision(repo, art.artifact_id, b"original", revision_id="rev-1")
            approvals = ApprovalRepository(store)
            appr = approvals.record_fixture_approval(
                target=self.target(store, art.artifact_id, rev.revision_id),
                gate_kind="fixture_batch", gate_id="g", decision="approved", rationale="r",
            )
            store.connection.execute("DROP TRIGGER revision_no_update")
            store.connection.execute(
                "UPDATE revision SET content = ? WHERE project_id = ? AND artifact_id = ? "
                "AND revision_id = ?",
                (b"tampered", store.project_id, art.artifact_id, rev.revision_id),
            )
            status = approvals.evaluate(appr.approval_id)
            self.assertEqual(status.status, "invalidated")


class TransactionFaultTest(ApprovalTestBase):
    def test_injected_failure_leaves_no_half_approval(self) -> None:
        with self.new_store() as store:
            repo = ArtifactRepository(store)
            art = repo.create_artifact(kind="document")
            rev = self.import_revision(repo, art.artifact_id, b"v", revision_id="rev-1")
            approvals = ApprovalRepository(store)
            with mock.patch.object(
                ApprovalRepository, "_insert_snapshot_entry",
                side_effect=RuntimeError("injected"),
            ):
                with self.assertRaises(RuntimeError):
                    approvals.record_fixture_approval(
                        target=self.target(store, art.artifact_id, rev.revision_id),
                        gate_kind="fixture_batch", gate_id="g",
                        decision="approved", rationale="r",
                    )
            self.assertEqual(approvals.list_approvals(), [])
            self.assertEqual(
                store.connection.execute(
                    "SELECT COUNT(*) FROM fixture_approval_entry"
                ).fetchone()[0],
                0,
            )
            violations = store.connection.execute("PRAGMA foreign_key_check").fetchall()
            self.assertEqual(violations, [])


class MigrationUpgradeTest(ApprovalTestBase):
    def test_migration2_fixture_upgrade3_preserves_bytes_and_refs(self) -> None:
        pid = paths.new_project_id()
        db_path = str(self.tmp / "m2.sqlite3")
        conn = schema.open_connection(db_path)
        try:
            schema.migrate(
                conn,
                target_version=2,
                migrations={1: schema.STORE_MIGRATIONS[1], 2: schema.STORE_MIGRATIONS[2]},
                label="project store",
            )
            self.assertEqual(schema.read_user_version(conn), 2)
            store = ProjectStore(
                conn, str(self.tmp / "m2"), pid, schema_version=2, app_version="0.0.0"
            )
            with store.transaction():
                conn.execute(
                    "INSERT INTO project "
                    "(project_id, name, created_at_utc, app_version) VALUES (?, ?, ?, ?)",
                    (pid, "M2", "2026-01-01T00:00:00+00:00", "0.0.0"),
                )
            repo = ArtifactRepository(store)
            base = repo.create_artifact(kind="document")
            base_rev = self.import_revision(
                repo, base.artifact_id, b"base-bytes-exact", revision_id="rev-base"
            )
            finding = repo.create_artifact(kind="finding")
            find_rev = self.import_revision(
                repo, finding.artifact_id, b"finding-bytes-exact",
                revision_id="rev-find",
                dependencies=[self.target(store, base.artifact_id, base_rev.revision_id)],
            )
            base_bytes = repo.read_content(base.artifact_id, "rev-base")
            find_bytes = repo.read_content(finding.artifact_id, "rev-find")
            rights_base = repo._rights_row(RightsRef(pid, "RIGHTS-rev-base", 1))["canonical"]
            rights_find = repo._rights_row(RightsRef(pid, "RIGHTS-rev-find", 1))["canonical"]
            deps_before = repo.list_dependencies(finding.artifact_id, "rev-find")
            base_sha_before = repo.get_revision(base.artifact_id, "rev-base").content_sha256

            # Upgrade v2 -> v3.
            schema.migrate(
                conn,
                target_version=3,
                migrations=schema.STORE_MIGRATIONS,
                label="project store",
            )
            self.assertEqual(schema.read_user_version(conn), 3)
            names = {
                r[0]
                for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            }
            self.assertTrue({"fixture_approval", "fixture_approval_entry"} <= names)
            # ALL prior exact bytes and refs preserved.
            self.assertEqual(repo.read_content(base.artifact_id, "rev-base"), base_bytes)
            self.assertEqual(repo.read_content(finding.artifact_id, "rev-find"), find_bytes)
            self.assertEqual(
                repo._rights_row(RightsRef(pid, "RIGHTS-rev-base", 1))["canonical"], rights_base
            )
            self.assertEqual(
                repo._rights_row(RightsRef(pid, "RIGHTS-rev-find", 1))["canonical"], rights_find
            )
            self.assertEqual(
                repo.list_dependencies(finding.artifact_id, "rev-find"), deps_before
            )
            self.assertEqual(
                repo.get_revision(base.artifact_id, "rev-base").content_sha256, base_sha_before
            )
            self.assertEqual(base_sha_before, _sha(b"base-bytes-exact"))
            # The new fixture-approval layer works over the upgraded store.
            approvals = _ApprovalRepositoryDirect(store)
            appr = approvals.record_fixture_approval(
                target=self.target(store, finding.artifact_id, find_rev.revision_id),
                gate_kind="fixture_artifact_revision", gate_id="g1",
                decision="approved", rationale="post-upgrade fixture",
            )
            self.assertEqual(approvals.evaluate(appr.approval_id).status, "active")
        finally:
            conn.close()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
