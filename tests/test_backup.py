"""Acceptance tests for consistent private backup/verify/restore.

Synthetic data only (no secrets, no licensed source text). They assert the
Step 02 backup/restore behavior: an online (WAL-consistent) snapshot, a manifest
derived from the closed snapshot, fail-closed verification, and a verified atomic
restore that preserves exact IDs and immutable historical approval snapshots.

Run with::

    PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_backup.py'
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from review_article_agent.domain.errors import (  # noqa: E402
    StoreError,
    UnsupportedSchemaVersion,
)
from review_article_agent.domain.records import canonical_json  # noqa: E402
from review_article_agent.storage import (  # noqa: E402
    ApprovalRepository,
    ArtifactRepository,
    ProjectStore,
    Registry,
)
from review_article_agent.storage import paths  # noqa: E402
from review_article_agent.storage import backup as backup_mod  # noqa: E402
from review_article_agent.storage.backup import (  # noqa: E402
    MANIFEST_FILENAME,
    SNAPSHOT_FILENAME,
    BackupConflictError,
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
        "privacy": {"personal_data": False, "confidentiality": "public", "denial": False},
        "recorded_by": "user",
        "recorded_at": "2026-10-27T00:00:00Z",
        "derived_status": "valid",
    }


class BackupTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="raa-backup-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    # -- fixtures ------------------------------------------------------------ #
    def new_store(self, name: str = "p") -> ProjectStore:
        return ProjectStore.create_at(
            paths.app_dir_for(str(self.tmp / name)), paths.new_project_id()
        )

    def import_revision(
        self,
        repo: ArtifactRepository,
        artifact_id: str,
        content: bytes,
        *,
        revision_id: str,
        parent: str | None = None,
        dependencies=None,
        request_id: str | None = None,
    ):
        return repo.import_revision(
            artifact_id=artifact_id,
            content=content,
            origin_locator="import:synthetic",
            rights=_rights(repo.project_id, artifact_id, revision_id),
            revision_id=revision_id,
            parent_revision_id=parent,
            dependencies=dependencies or [],
            request_id=request_id,
        )

    def seed(self, store: ProjectStore) -> dict:
        """Create two artifacts, a dependency, stale history, and an approval."""
        repo = ArtifactRepository(store)
        doc = repo.create_artifact(kind="document")
        find = repo.create_artifact(kind="finding")
        d1 = self.import_revision(repo, doc.artifact_id, b"doc-v1", revision_id="rev-d1")
        f1 = self.import_revision(
            repo,
            find.artifact_id,
            b"finding-v1",
            revision_id="rev-f1",
            dependencies=[
                {"project_id": store.project_id, "artifact_id": doc.artifact_id, "revision_id": "rev-d1"}
            ],
            request_id="req-1",
        )
        approvals = ApprovalRepository(store)
        stale_candidate = approvals.record_fixture_approval(
            target={"project_id": store.project_id, "artifact_id": doc.artifact_id, "revision_id": "rev-d1"},
            gate_kind="fixture_artifact_revision",
            gate_id="gate-doc",
            decision="approved",
            rationale="synthetic local fixture; not a scientific approval",
        )
        current = approvals.record_fixture_approval(
            target={"project_id": store.project_id, "artifact_id": find.artifact_id, "revision_id": "rev-f1"},
            gate_kind="fixture_batch",
            gate_id="gate-find",
            decision="approved",
            rationale="synthetic local fixture; not a scientific approval",
        )
        # A later revision makes the doc approval historical (stale) history.
        self.import_revision(
            repo, doc.artifact_id, b"doc-v2", revision_id="rev-d2", parent="rev-d1"
        )
        return {
            "doc": doc,
            "find": find,
            "stale_approval": stale_candidate,
            "current_approval": current,
        }

    # -- tamper helpers ------------------------------------------------------ #
    def rewrite_outer_db_hash(self, backup_dir: Path) -> None:
        """Recompute the manifest's database hash (simulates a rewritten hash)."""
        manifest_path = backup_dir / MANIFEST_FILENAME
        manifest = json.loads(manifest_path.read_text())
        data = (backup_dir / SNAPSHOT_FILENAME).read_bytes()
        manifest["database"]["sha256"] = _sha(data)
        manifest["database"]["byte_size"] = len(data)
        manifest_path.write_text(json.dumps(manifest))

    def tamper_db(
        self, backup_dir: Path, *statements: str, preserve_schema: bool = False
    ) -> None:
        """Execute raw SQL against the snapshot.

        With ``preserve_schema=True`` any *trigger* the statements drop is
        recreated with its original definition afterwards, so a test that mutates
        data still exercises inner digest validation rather than merely a missing
        schema object.
        """
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


class CreateAndVerifyTest(BackupTestBase):
    def test_create_produces_only_allowlisted_members(self) -> None:
        store = self.new_store()
        self.addCleanup(store.close)
        self.seed(store)
        dest = self.tmp / "backup"
        summary = create_backup(store, dest)
        self.assertEqual(sorted(os.listdir(dest)), [MANIFEST_FILENAME, SNAPSHOT_FILENAME])
        self.assertEqual(summary["project_id"], store.project_id)
        self.assertEqual(summary["counts"]["approvals"], 2)
        report = verify_backup(dest)
        self.assertTrue(report["ok"])
        self.assertEqual(report["project_id"], store.project_id)
        self.assertEqual(report["counts"]["revisions"], 3)

    def test_created_members_are_owner_private(self) -> None:
        store = self.new_store()
        self.addCleanup(store.close)
        self.seed(store)
        dest = self.tmp / "backup"
        create_backup(store, dest)
        self.assertEqual(os.stat(dest).st_mode & 0o077, 0)
        for name in (MANIFEST_FILENAME, SNAPSHOT_FILENAME):
            self.assertEqual(os.stat(dest / name).st_mode & 0o077, 0)

    def test_create_refuses_existing_destination(self) -> None:
        store = self.new_store()
        self.addCleanup(store.close)
        dest = self.tmp / "backup"
        dest.mkdir()
        with self.assertRaises(BackupError):
            create_backup(store, dest)

    def test_verify_absent_backup_rejected(self) -> None:
        with self.assertRaises(BackupError):
            verify_backup(self.tmp / "nope")


class SecretExclusionTest(BackupTestBase):
    def test_env_and_loose_credentials_never_in_snapshot_or_manifest(self) -> None:
        secret = "sk-fake-provider-key-0123456789abcdef"
        os.environ["OPENROUTER_API_KEY"] = secret
        self.addCleanup(os.environ.pop, "OPENROUTER_API_KEY", None)

        root = self.tmp / "project"
        root.mkdir()
        (root / ".env").write_text(f"OPENROUTER_API_KEY={secret}\n")
        (root / "credentials.json").write_text(json.dumps({"provider_key": secret}))
        (root / "notes.txt").write_text("a loose user file that must not be copied")

        store = ProjectStore.create_at(
            paths.app_dir_for(str(root)), paths.new_project_id()
        )
        self.addCleanup(store.close)
        repo = ArtifactRepository(store)
        art = repo.create_artifact(kind="document")
        self.import_revision(repo, art.artifact_id, b"clean content", revision_id="rev-1")

        dest = self.tmp / "backup"
        summary = create_backup(store, dest)
        raw_snapshot = (dest / SNAPSHOT_FILENAME).read_bytes()
        raw_manifest = (dest / MANIFEST_FILENAME).read_bytes()
        self.assertNotIn(secret.encode(), raw_snapshot)
        self.assertNotIn(secret.encode(), raw_manifest)
        self.assertNotIn(secret, json.dumps(summary))
        self.assertNotIn("notes.txt", raw_manifest.decode("utf-8", "replace"))
        report = verify_backup(dest)
        self.assertNotIn(secret, json.dumps(report))
        self.assertEqual(sorted(os.listdir(dest)), [MANIFEST_FILENAME, SNAPSHOT_FILENAME])


class RestoreTest(BackupTestBase):
    def test_roundtrip_preserves_ids_content_and_approvals(self) -> None:
        store = self.new_store()
        self.addCleanup(store.close)
        seeded = self.seed(store)
        dest = self.tmp / "backup"
        create_backup(store, dest)

        target_root = self.tmp / "restored"
        target_root.mkdir()
        (target_root / "unrelated.txt").write_text("keep me")
        registry = Registry(self.tmp / "state")
        info = restore_backup(dest, target_root, registry)

        self.assertEqual(info.project_id, store.project_id)
        self.assertEqual((target_root / "unrelated.txt").read_text(), "keep me")

        restored = ProjectStore.open_at(
            paths.app_dir_for(info.root_path), expected_project_id=info.project_id
        )
        try:
            repo = ArtifactRepository(restored)
            self.assertEqual(repo.read_content(seeded["doc"].artifact_id, "rev-d1"), b"doc-v1")
            self.assertEqual(repo.read_content(seeded["doc"].artifact_id, "rev-d2"), b"doc-v2")
            self.assertEqual(repo.read_content(seeded["find"].artifact_id, "rev-f1"), b"finding-v1")
            approvals = ApprovalRepository(restored)
            ids = {a.approval_id for a in approvals.list_approvals()}
            self.assertEqual(
                ids,
                {seeded["stale_approval"].approval_id, seeded["current_approval"].approval_id},
            )
            # Historical (stale) approval snapshots survive and verify structurally.
            for approval_id in ids:
                self.assertTrue(approvals.verify_snapshot(approval_id).ok)
            stored = approvals.get_approval(seeded["stale_approval"].approval_id)
            self.assertEqual(
                stored.snapshot_digest, seeded["stale_approval"].snapshot_digest
            )
        finally:
            restored.close()

    def test_duplicate_restore_same_path_is_safe_noop(self) -> None:
        store = self.new_store()
        self.addCleanup(store.close)
        self.seed(store)
        dest = self.tmp / "backup"
        create_backup(store, dest)
        registry = Registry(self.tmp / "state")
        root = self.tmp / "restored"
        first = restore_backup(dest, root, registry)
        second = restore_backup(dest, root, registry)
        self.assertEqual(first.project_id, second.project_id)
        self.assertEqual(len(registry.list_projects()), 1)

    def test_restore_same_project_other_root_refused(self) -> None:
        registry = Registry(self.tmp / "state")
        origin = registry.create_project(self.tmp / "origin", name="Origin")
        store = ProjectStore.open_at(
            paths.app_dir_for(origin.root_path), expected_project_id=origin.project_id
        )
        try:
            self.seed(store)
            dest = self.tmp / "backup"
            create_backup(store, dest)
        finally:
            store.close()
        with self.assertRaises(BackupConflictError):
            restore_backup(dest, self.tmp / "clone", registry)
        self.assertEqual(len(registry.list_projects()), 1)

    def test_restore_different_project_same_root_refused(self) -> None:
        registry = Registry(self.tmp / "state")
        other = registry.create_project(self.tmp / "occupied", name="Occupied")
        store = self.new_store("backup-src")
        self.addCleanup(store.close)
        self.seed(store)
        dest = self.tmp / "backup"
        create_backup(store, dest)
        with self.assertRaises(BackupConflictError):
            restore_backup(dest, self.tmp / "occupied", registry)
        # The pre-existing project is untouched.
        self.assertEqual(registry.get(other.project_id).root_path, other.root_path)

    def test_registration_failure_cleans_own_dir_and_preserves_user_files(self) -> None:
        store = self.new_store()
        self.addCleanup(store.close)
        self.seed(store)
        dest = self.tmp / "backup"
        create_backup(store, dest)

        root = self.tmp / "target"
        root.mkdir()
        (root / "keep.txt").write_text("user data")
        registry = Registry(self.tmp / "state")

        def boom(*args, **kwargs):
            raise RuntimeError("injected registration failure")

        registry._insert_project_row = boom  # type: ignore[assignment]
        with self.assertRaises(RuntimeError):
            restore_backup(dest, root, registry)

        self.assertFalse(os.path.exists(paths.app_dir_for(str(root))))
        self.assertEqual((root / "keep.txt").read_text(), "user data")
        self.assertEqual(registry.list_projects(), [])


class ConcurrentWriteTest(BackupTestBase):
    def test_committed_wal_content_is_included(self) -> None:
        """A write committed only to WAL is captured by the snapshot."""
        root = self.tmp / "p"
        store = ProjectStore.create_at(paths.app_dir_for(str(root)), paths.new_project_id())
        self.addCleanup(store.close)
        repo = ArtifactRepository(store)
        art = repo.create_artifact(kind="document")

        # A second, independent connection commits a revision (WAL, not yet
        # checkpointed into the main database file).
        writer = ProjectStore.open_at(store.app_dir, expected_project_id=store.project_id)
        try:
            wrepo = ArtifactRepository(writer)
            self.import_revision(wrepo, art.artifact_id, b"wal-only-content", revision_id="rev-wal")
        finally:
            writer.close()

        dest = self.tmp / "backup"
        create_backup(store, dest)
        verify_backup(dest)
        registry = Registry(self.tmp / "state")
        info = restore_backup(dest, self.tmp / "restored", registry)
        restored = ProjectStore.open_at(
            paths.app_dir_for(info.root_path), expected_project_id=info.project_id
        )
        try:
            self.assertEqual(
                ArtifactRepository(restored).read_content(art.artifact_id, "rev-wal"),
                b"wal-only-content",
            )
        finally:
            restored.close()

    def test_concurrent_writes_overlap_backup_and_stay_consistent(self) -> None:
        root = self.tmp / "p"
        store = ProjectStore.create_at(paths.app_dir_for(str(root)), paths.new_project_id())
        self.addCleanup(store.close)
        project_id = store.project_id
        app_dir = store.app_dir
        repo = ArtifactRepository(store)
        art = repo.create_artifact(kind="document")
        # A large first revision makes the online backup take measurably long.
        self.import_revision(repo, art.artifact_id, os.urandom(2_000_000), revision_id="rev-0")

        stop = threading.Event()
        state = {"writes": 0, "last_commit": 0.0, "head": "rev-0"}
        errors: list[BaseException] = []

        def writer() -> None:
            wstore = ProjectStore.open_at(app_dir, expected_project_id=project_id)
            try:
                wrepo = ArtifactRepository(wstore)
                i = 0
                while not stop.is_set():
                    revision_id = f"rev-w{i}"
                    self.import_revision(
                        wrepo,
                        art.artifact_id,
                        os.urandom(256_000),
                        revision_id=revision_id,
                        parent=state["head"],
                        request_id=f"req-w{i}",
                    )
                    state["head"] = revision_id
                    state["writes"] = i + 1
                    state["last_commit"] = time.monotonic()
                    i += 1
            except BaseException as exc:  # pragma: no cover - surfaced below
                errors.append(exc)
            finally:
                wstore.close()

        thread = threading.Thread(target=writer)
        thread.start()
        try:
            deadline = time.monotonic() + 10
            while state["writes"] < 1 and time.monotonic() < deadline:
                time.sleep(0.001)

            backup_started = time.monotonic()
            writes_before_backup = state["writes"]
            dest = self.tmp / "backup"
            create_backup(store, dest)
        finally:
            stop.set()
            thread.join(30)

        self.assertEqual(errors, [])
        # At least one write committed after the backup began (real overlap).
        self.assertGreater(state["last_commit"], backup_started)
        self.assertGreater(state["writes"], writes_before_backup)

        report = verify_backup(dest)
        self.assertTrue(report["ok"])
        # The snapshot is a consistent point in a strictly growing chain.
        self.assertGreaterEqual(report["counts"]["revisions"], 2)
        registry = Registry(self.tmp / "state")
        info = restore_backup(dest, self.tmp / "restored", registry)
        restored = ProjectStore.open_at(
            paths.app_dir_for(info.root_path), expected_project_id=info.project_id
        )
        try:
            revisions = ArtifactRepository(restored).list_revisions(art.artifact_id)
            self.assertEqual(len(revisions), report["counts"]["revisions"])
            for revision in revisions:
                content = ArtifactRepository(restored).read_content(
                    art.artifact_id, revision.revision_id
                )
                self.assertEqual(_sha(content), revision.content_sha256)
                self.assertEqual(len(content), revision.byte_size)
        finally:
            restored.close()


class FailClosedVerificationTest(BackupTestBase):
    def _fresh_backup(self) -> tuple[ProjectStore, Path]:
        store = self.new_store()
        self.addCleanup(store.close)
        self.seed(store)
        dest = self.tmp / "backup"
        create_backup(store, dest)
        return store, dest

    def test_tampered_content_rejected(self) -> None:
        _, dest = self._fresh_backup()
        self.tamper_db(
            dest,
            "DROP TRIGGER revision_no_update",
            "UPDATE revision SET content = X'00' WHERE revision_id = 'rev-d1'",
            preserve_schema=True,
        )
        self.rewrite_outer_db_hash(dest)
        with self.assertRaises(BackupError):
            verify_backup(dest)

    def test_tampered_rights_rejected(self) -> None:
        _, dest = self._fresh_backup()
        self.tamper_db(
            dest,
            "DROP TRIGGER rights_record_no_update",
            "UPDATE rights_record SET canonical = X'7b7d'",
            preserve_schema=True,
        )
        self.rewrite_outer_db_hash(dest)
        with self.assertRaises(BackupError):
            verify_backup(dest)

    def test_tampered_approval_rejected(self) -> None:
        _, dest = self._fresh_backup()
        self.tamper_db(
            dest,
            "DROP TRIGGER fixture_approval_no_update",
            "UPDATE fixture_approval SET snapshot_digest = "
            "'sha256:" + "0" * 64 + "'",
            preserve_schema=True,
        )
        self.rewrite_outer_db_hash(dest)
        with self.assertRaises(BackupError):
            verify_backup(dest)

    def test_manifest_database_hash_mismatch_rejected(self) -> None:
        _, dest = self._fresh_backup()
        manifest = json.loads((dest / MANIFEST_FILENAME).read_text())
        manifest["database"]["sha256"] = "sha256:" + "1" * 64
        (dest / MANIFEST_FILENAME).write_text(json.dumps(manifest))
        with self.assertRaises(BackupError):
            verify_backup(dest)

    def test_rewritten_outer_hash_does_not_bypass_inner_validation(self) -> None:
        _, dest = self._fresh_backup()
        self.tamper_db(
            dest,
            "DROP TRIGGER revision_no_update",
            "UPDATE revision SET byte_size = byte_size + 1 WHERE revision_id = 'rev-d1'",
            preserve_schema=True,
        )
        # The outer database hash is made to match the tampered file.
        self.rewrite_outer_db_hash(dest)
        with self.assertRaises(BackupError):
            verify_backup(dest)

    def test_manifest_digest_mismatch_rejected(self) -> None:
        _, dest = self._fresh_backup()
        manifest = json.loads((dest / MANIFEST_FILENAME).read_text())
        manifest["artifact_digest"] = "sha256:" + "2" * 64
        (dest / MANIFEST_FILENAME).write_text(json.dumps(manifest))
        with self.assertRaises(BackupError):
            verify_backup(dest)

    def test_newer_manifest_version_rejected(self) -> None:
        _, dest = self._fresh_backup()
        manifest = json.loads((dest / MANIFEST_FILENAME).read_text())
        manifest["format_version"] = 2
        (dest / MANIFEST_FILENAME).write_text(json.dumps(manifest))
        with self.assertRaises(UnsupportedSchemaVersion):
            verify_backup(dest)

    def test_newer_store_schema_in_manifest_rejected(self) -> None:
        _, dest = self._fresh_backup()
        manifest = json.loads((dest / MANIFEST_FILENAME).read_text())
        manifest["store_schema_version"] = (
            backup_mod.SUPPORTED_STORE_SCHEMA_VERSION + 1
        )
        (dest / MANIFEST_FILENAME).write_text(json.dumps(manifest))
        with self.assertRaises(UnsupportedSchemaVersion):
            verify_backup(dest)

    def test_newer_snapshot_store_schema_rejected(self) -> None:
        _, dest = self._fresh_backup()
        self.tamper_db(
            dest,
            f"PRAGMA user_version = {backup_mod.SUPPORTED_STORE_SCHEMA_VERSION + 1}",
        )
        self.rewrite_outer_db_hash(dest)
        with self.assertRaises(UnsupportedSchemaVersion):
            verify_backup(dest)

    def test_missing_manifest_field_rejected(self) -> None:
        _, dest = self._fresh_backup()
        manifest = json.loads((dest / MANIFEST_FILENAME).read_text())
        del manifest["counts"]
        (dest / MANIFEST_FILENAME).write_text(json.dumps(manifest))
        with self.assertRaises(BackupError):
            verify_backup(dest)

    def test_traversing_database_filename_rejected(self) -> None:
        _, dest = self._fresh_backup()
        manifest = json.loads((dest / MANIFEST_FILENAME).read_text())
        manifest["database"]["filename"] = "../evil.sqlite3"
        (dest / MANIFEST_FILENAME).write_text(json.dumps(manifest))
        with self.assertRaises(BackupError):
            verify_backup(dest)

    def test_unexpected_member_rejected(self) -> None:
        _, dest = self._fresh_backup()
        (dest / "extra.txt").write_text("not allowed")
        with self.assertRaises(BackupError):
            verify_backup(dest)

    def test_wal_sidecar_member_rejected(self) -> None:
        _, dest = self._fresh_backup()
        (dest / (SNAPSHOT_FILENAME + "-wal")).write_bytes(b"")
        with self.assertRaises(BackupError):
            verify_backup(dest)

    def test_external_table_rejected(self) -> None:
        _, dest = self._fresh_backup()
        self.tamper_db(dest, "CREATE TABLE evil (payload TEXT)")
        self.rewrite_outer_db_hash(dest)
        with self.assertRaises(BackupError):
            verify_backup(dest)

    def test_missing_required_table_rejected(self) -> None:
        _, dest = self._fresh_backup()
        self.tamper_db(dest, "DROP TABLE import_request")
        self.rewrite_outer_db_hash(dest)
        with self.assertRaises(BackupError):
            verify_backup(dest)

    def test_symlink_member_rejected(self) -> None:
        _, dest = self._fresh_backup()
        real = dest / SNAPSHOT_FILENAME
        other = self.tmp / "real.sqlite3"
        other.write_bytes(real.read_bytes())
        real.unlink()
        os.symlink(other, real)
        with self.assertRaises(BackupError):
            verify_backup(dest)

    def test_symlink_directory_rejected(self) -> None:
        _, dest = self._fresh_backup()
        link = self.tmp / "link-backup"
        os.symlink(dest, link)
        with self.assertRaises(BackupError):
            verify_backup(link)

    def test_verify_does_not_mutate_supplied_backup(self) -> None:
        _, dest = self._fresh_backup()
        before = {
            name: (dest / name).read_bytes() for name in (MANIFEST_FILENAME, SNAPSHOT_FILENAME)
        }
        verify_backup(dest)
        after = {
            name: (dest / name).read_bytes() for name in (MANIFEST_FILENAME, SNAPSHOT_FILENAME)
        }
        self.assertEqual(before, after)
        self.assertEqual(sorted(os.listdir(dest)), [MANIFEST_FILENAME, SNAPSHOT_FILENAME])


class RepairTrustBoundaryTest(BackupTestBase):
    """T5-R1 focused repairs: close backup/restore trust-boundary gaps."""

    def _seeded_backup(self):
        store = self.new_store()
        self.addCleanup(store.close)
        seeded = self.seed(store)
        dest = self.tmp / "backup"
        create_backup(store, dest)
        return store, seeded, dest

    # (1) exact rights/revision structural bindings ------------------------- #
    def test_revision_pinning_other_artifacts_rights_rejected(self) -> None:
        store, seeded, dest = self._seeded_backup()
        project_id = store.project_id
        content = b"forged"
        sha = _sha(content)
        # A forged revision under the *finding* artifact that pins the *document*
        # revision's rights record (a valid record for a different revision).
        self.tamper_db(
            dest,
            "INSERT INTO revision (project_id, artifact_id, revision_id, "
            "parent_revision_id, content, content_sha256, byte_size, media_type, "
            "origin_locator, acquired_at_utc, acquired_by, provenance_method, "
            "provenance_actor, provenance_performed_at_utc, rights_project_id, "
            "rights_id, rights_record_revision, immutable) VALUES "
            f"('{project_id}', '{seeded['find'].artifact_id}', 'rev-forged', NULL, "
            f"X'{content.hex()}', '{sha}', {len(content)}, NULL, 'import:forged', "
            f"'2026-10-27T00:00:00Z', 'import', 'import', 'forged', "
            f"'2026-10-27T00:00:00Z', '{project_id}', 'RIGHTS-rev-d1', 1, 1)",
        )
        self.rewrite_outer_db_hash(dest)
        with self.assertRaises(BackupError) as ctx:
            verify_backup(dest)
        self.assertIn("bind", str(ctx.exception))

    def test_revision_with_non_storage_rights_rejected(self) -> None:
        store, seeded, dest = self._seeded_backup()
        project_id = store.project_id
        conn = sqlite3.connect(str(dest / SNAPSHOT_FILENAME))
        try:
            canonical = conn.execute(
                "SELECT canonical FROM rights_record WHERE rights_id = "
                "'RIGHTS-rev-d1' AND record_revision = 1"
            ).fetchone()[0]
        finally:
            conn.close()
        record = json.loads(canonical.decode("utf-8"))
        record["operations"]["storage"] = "not_allowed"
        new_canonical = canonical_json(record)
        self.tamper_db(
            dest,
            "DROP TRIGGER rights_record_no_update",
            "UPDATE rights_record SET canonical = X'" + new_canonical.hex() + "', "
            "content_sha256 = '" + _sha(new_canonical) + "' WHERE rights_id = "
            "'RIGHTS-rev-d1' AND record_revision = 1",
            preserve_schema=True,
        )
        self.rewrite_outer_db_hash(dest)
        with self.assertRaises(BackupError) as ctx:
            verify_backup(dest)
        self.assertIn("storage", str(ctx.exception))

    # (2) exact schema-object fingerprint ----------------------------------- #
    def test_missing_immutability_trigger_rejected(self) -> None:
        _, _, dest = self._seeded_backup()
        self.tamper_db(dest, "DROP TRIGGER artifact_no_delete")
        self.rewrite_outer_db_hash(dest)
        with self.assertRaises(BackupError) as ctx:
            verify_backup(dest)
        self.assertIn("schema", str(ctx.exception))

    def test_added_hidden_column_rejected(self) -> None:
        _, _, dest = self._seeded_backup()
        self.tamper_db(dest, "ALTER TABLE revision ADD COLUMN secret TEXT")
        self.rewrite_outer_db_hash(dest)
        with self.assertRaises(BackupError) as ctx:
            verify_backup(dest)
        self.assertIn("schema", str(ctx.exception))

    # (3) active write transaction is rejected fast ------------------------- #
    def test_active_transaction_rejected(self) -> None:
        store = self.new_store()
        self.addCleanup(store.close)
        self.seed(store)
        dest = self.tmp / "backup"
        with store.transaction():
            with self.assertRaises(BackupError):
                create_backup(store, dest)
        self.assertFalse(os.path.lexists(dest))
        self.assertEqual(
            [p.name for p in self.tmp.iterdir() if "staging" in p.name], []
        )

    # (4) corrupt staging is never published -------------------------------- #
    def test_corrupt_staging_not_published(self) -> None:
        store = self.new_store()
        self.addCleanup(store.close)
        self.seed(store)
        dest = self.tmp / "backup"
        original = backup_mod._snapshot_database

        def corrupt(source, dest_path):
            original(source, dest_path)
            conn = sqlite3.connect(dest_path)
            try:
                conn.execute("DROP TRIGGER artifact_no_delete")
                conn.commit()
            finally:
                conn.close()

        with mock.patch.object(backup_mod, "_snapshot_database", corrupt):
            with self.assertRaises(BackupError):
                create_backup(store, dest)
        self.assertFalse(os.path.lexists(dest))
        self.assertEqual(
            [p.name for p in self.tmp.iterdir() if "staging" in p.name], []
        )

    # (5) TOCTOU: source change after verify is refused -------------------- #
    def test_restore_detects_source_change_after_verify(self) -> None:
        store = self.new_store()
        self.addCleanup(store.close)
        self.seed(store)
        dest = self.tmp / "backup"
        create_backup(store, dest)
        root = self.tmp / "target"
        registry = Registry(self.tmp / "state")
        original_copy = backup_mod.shutil.copyfile

        def evil_copy(src, dst, *args, **kwargs):
            conn = sqlite3.connect(src)
            try:
                conn.execute("DROP TRIGGER revision_no_update")
                conn.execute(
                    "UPDATE revision SET content = X'00' WHERE revision_id = 'rev-d1'"
                )
                conn.commit()
            finally:
                conn.close()
            return original_copy(src, dst, *args, **kwargs)

        with mock.patch.object(backup_mod.shutil, "copyfile", evil_copy):
            with self.assertRaises(BackupError):
                restore_backup(dest, root, registry)
        self.assertFalse(os.path.exists(paths.app_dir_for(str(root))))
        self.assertEqual(registry.list_projects(), [])

    # (6) malformed manifest JSON is rejected as BackupError --------------- #
    def test_manifest_duplicate_key_rejected(self) -> None:
        _, _, dest = self._seeded_backup()
        path = dest / MANIFEST_FILENAME
        text = path.read_text()
        path.write_text(text.replace('"notice":', '"format_version": 1, "notice":', 1))
        with self.assertRaises(BackupError):
            verify_backup(dest)

    def test_manifest_nonfinite_constant_rejected(self) -> None:
        _, _, dest = self._seeded_backup()
        path = dest / MANIFEST_FILENAME
        manifest = json.loads(path.read_text())
        path.write_text(
            json.dumps(manifest).replace(
                f'"artifacts": {manifest["counts"]["artifacts"]}',
                '"artifacts": NaN',
                1,
            )
        )
        with self.assertRaises(BackupError):
            verify_backup(dest)

    def test_manifest_deep_nesting_rejected(self) -> None:
        _, _, dest = self._seeded_backup()
        (dest / MANIFEST_FILENAME).write_text('{"x": ' + "[" * 4000 + "]" * 4000 + "}")
        with self.assertRaises(BackupError):
            verify_backup(dest)


class CorruptRejectedBeforeRestoreTest(BackupTestBase):
    def test_corrupt_backup_rejected_before_target_created(self) -> None:
        store = self.new_store()
        self.addCleanup(store.close)
        self.seed(store)
        dest = self.tmp / "backup"
        create_backup(store, dest)
        self.tamper_db(
            dest,
            "DROP TRIGGER revision_no_update",
            "UPDATE revision SET content = X'ff' WHERE revision_id = 'rev-d1'",
            preserve_schema=True,
        )
        self.rewrite_outer_db_hash(dest)

        root = self.tmp / "never"
        registry = Registry(self.tmp / "state")
        with self.assertRaises(BackupError):
            restore_backup(dest, root, registry)
        self.assertFalse(os.path.exists(paths.app_dir_for(str(root))))
        self.assertEqual(registry.list_projects(), [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
