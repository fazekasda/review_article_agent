"""Acceptance tests for immutable artifact revisions and idempotent imports.

Synthetic data only (no secrets, no licensed source text). These tests assert
storage/identity behavior; they make no scientific, legal, or approval claim.

Run with::

    PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_artifacts.py'
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from review_article_agent.domain.errors import UnsupportedSchemaVersion  # noqa: E402
from review_article_agent.domain.records import RecordValidationError  # noqa: E402
from review_article_agent.storage import (  # noqa: E402
    ArtifactConflictError,
    ArtifactRepository,
    ProjectStore,
    Registry,
    RightsRef,
)
from review_article_agent.storage import paths, schema  # noqa: E402

_OPS = ("acquisition", "storage", "redistribution", "hosted_transfer")


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


class ArtifactTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="raa-artifacts-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def new_store(self, name: str = "p") -> ProjectStore:
        root = self.tmp / name
        return ProjectStore.create_at(paths.app_dir_for(str(root)), paths.new_project_id())

    def repo_for(self, store: ProjectStore) -> ArtifactRepository:
        return ArtifactRepository(store)

    def import_document(self, repo, artifact_id, content, *, rights_revision, **kwargs):
        return repo.import_revision(
            artifact_id=artifact_id,
            content=content,
            origin_locator="import:synthetic",
            rights=_valid_rights(repo.project_id, artifact_id, rights_revision),
            revision_id=rights_revision,
            **kwargs,
        )


class PersistenceTest(ArtifactTestBase):
    def test_two_projects_persist_independently(self) -> None:
        reg = Registry(self.tmp / "state")
        a = reg.create_project(self.tmp / "proj-a", name="Alpha")
        b = reg.create_project(self.tmp / "proj-b", name="Beta")

        with ProjectStore.open_at(paths.app_dir_for(a.root_path)) as store_a:
            repo_a = self.repo_for(store_a)
            art = repo_a.create_artifact(kind="document", label="syn-a")
            rev = self.import_document(
                repo_a, art.artifact_id, b"alpha bytes", rights_revision="rev-a"
            )

        with ProjectStore.open_at(paths.app_dir_for(b.root_path)) as store_b:
            repo_b = self.repo_for(store_b)
            # B is an independent project: it cannot see A's artifact.
            self.assertIsNone(repo_b.get_artifact(art.artifact_id))
            self.assertEqual(repo_b.list_artifacts(), [])

        # Reopen through fresh stores: A's revision is still there, byte-identical.
        with ProjectStore.open_at(paths.app_dir_for(a.root_path)) as store_a:
            repo_a = self.repo_for(store_a)
            again = repo_a.get_revision(art.artifact_id, rev.revision_id)
            self.assertIsNotNone(again)
            self.assertEqual(again.content_sha256, rev.content_sha256)
            self.assertEqual(repo_a.read_content(art.artifact_id, rev.revision_id), b"alpha bytes")
            self.assertEqual(repo_a.current_revision(art.artifact_id).revision_id, rev.revision_id)


class ArtifactIdentityTest(ArtifactTestBase):
    def test_invalid_kind_rejected(self) -> None:
        with self.new_store() as store:
            repo = self.repo_for(store)
            with self.assertRaises(RecordValidationError):
                repo.create_artifact(kind="not_a_kind")

    def test_create_is_idempotent_but_kind_conflict_is_refused(self) -> None:
        with self.new_store() as store:
            repo = self.repo_for(store)
            first = repo.create_artifact("art-fixed", kind="document")
            again = repo.create_artifact("art-fixed", kind="document")
            self.assertEqual(first, again)
            with self.assertRaises(ArtifactConflictError):
                repo.create_artifact("art-fixed", kind="study")


class ImportIdempotencyTest(ArtifactTestBase):
    def test_same_request_id_returns_same_revision(self) -> None:
        with self.new_store() as store:
            repo = self.repo_for(store)
            art = repo.create_artifact(kind="document")
            rev = self.import_document(
                repo, art.artifact_id, b"same", rights_revision="rev-1", request_id="req-1"
            )
            again = self.import_document(
                repo, art.artifact_id, b"same", rights_revision="rev-1", request_id="req-1"
            )
            self.assertEqual(again.revision_id, rev.revision_id)
            self.assertEqual(len(repo.list_revisions(art.artifact_id)), 1)

    def test_divergent_request_id_is_conflict(self) -> None:
        with self.new_store() as store:
            repo = self.repo_for(store)
            art = repo.create_artifact(kind="document")
            self.import_document(
                repo, art.artifact_id, b"one", rights_revision="rev-1", request_id="req-1"
            )
            with self.assertRaises(ArtifactConflictError):
                self.import_document(
                    repo, art.artifact_id, b"two", rights_revision="rev-1", request_id="req-1"
                )
            self.assertEqual(len(repo.list_revisions(art.artifact_id)), 1)

    def test_repeated_identical_import_reuses_revision(self) -> None:
        with self.new_store() as store:
            repo = self.repo_for(store)
            art = repo.create_artifact(kind="document")
            first = self.import_document(repo, art.artifact_id, b"dup", rights_revision="rev-1")
            # Same artifact + parent + deps + rights: safe reuse, no new revision.
            reused = repo.import_revision(
                artifact_id=art.artifact_id,
                content=b"dup",
                origin_locator="import:synthetic",
                rights=RightsRef(store.project_id, "RIGHTS-rev-1", 1),
            )
            self.assertEqual(reused.revision_id, first.revision_id)
            self.assertEqual(len(repo.list_revisions(art.artifact_id)), 1)

    def test_equal_bytes_distinct_artifacts_keep_identity(self) -> None:
        with self.new_store() as store:
            repo = self.repo_for(store)
            a1 = repo.create_artifact(kind="document")
            a2 = repo.create_artifact(kind="document")
            r1 = self.import_document(repo, a1.artifact_id, b"identical", rights_revision="rev-1")
            r2 = self.import_document(repo, a2.artifact_id, b"identical", rights_revision="rev-2")
            self.assertNotEqual(r1.revision_id, r2.revision_id)
            self.assertEqual(r1.content_sha256, r2.content_sha256)
            self.assertEqual(len(repo.list_revisions(a1.artifact_id)), 1)
            self.assertEqual(len(repo.list_revisions(a2.artifact_id)), 1)


class RevisionEditTest(ArtifactTestBase):
    def test_edit_creates_new_revision_with_parent(self) -> None:
        with self.new_store() as store:
            repo = self.repo_for(store)
            art = repo.create_artifact(kind="document")
            v1 = self.import_document(repo, art.artifact_id, b"v1", rights_revision="rev-1")
            v2 = self.import_document(
                repo, art.artifact_id, b"v2", rights_revision="rev-2",
                parent_revision_id=v1.revision_id,
            )
            self.assertEqual(v2.parent_revision_id, v1.revision_id)
            self.assertEqual(len(repo.list_revisions(art.artifact_id)), 2)
            # The earlier revision's bytes are untouched by the edit.
            self.assertEqual(repo.read_content(art.artifact_id, v1.revision_id), b"v1")

    def test_wrong_parent_is_rejected(self) -> None:
        with self.new_store() as store:
            repo = self.repo_for(store)
            art = repo.create_artifact(kind="document")
            with self.assertRaises(RecordValidationError):
                self.import_document(
                    repo, art.artifact_id, b"x", rights_revision="rev-1",
                    parent_revision_id="rev-does-not-exist",
                )
            self.assertEqual(repo.list_revisions(art.artifact_id), [])

    def test_invalid_edits_do_not_mutate_earlier_revision(self) -> None:
        with self.new_store() as store:
            repo = self.repo_for(store)
            art = repo.create_artifact(kind="document")
            good = self.import_document(repo, art.artifact_id, b"stable", rights_revision="rev-1")

            # invalid content envelope
            with self.assertRaises(RecordValidationError):
                repo.import_revision(
                    artifact_id=art.artifact_id,
                    content={"schema_version": 1},  # missing document_type/payload
                    origin_locator="import:x",
                    rights=RightsRef(store.project_id, "RIGHTS-rev-1", 1),
                )
            # invalid dependency (unknown target revision)
            with self.assertRaises(RecordValidationError):
                repo.import_revision(
                    artifact_id=art.artifact_id,
                    content=b"bad",
                    origin_locator="import:x",
                    rights=RightsRef(store.project_id, "RIGHTS-rev-1", 1),
                    dependencies=[
                        {"project_id": store.project_id, "artifact_id": art.artifact_id,
                         "revision_id": "rev-missing"}
                    ],
                )
            # invalid rights (storage not allowed)
            bad_rights = _valid_rights(store.project_id, art.artifact_id, "rev-2")
            bad_rights["operations"]["storage"] = "unknown"
            bad_rights["basis"]["storage"] = {"kind": "unknown", "evidence": {"ref": "", "provenance": ""}}
            with self.assertRaises(RecordValidationError):
                repo.import_revision(
                    artifact_id=art.artifact_id,
                    content=b"bad",
                    origin_locator="import:x",
                    rights=bad_rights,
                    revision_id="rev-2",
                )

            self.assertEqual(len(repo.list_revisions(art.artifact_id)), 1)
            current = repo.current_revision(art.artifact_id)
            self.assertEqual(current.revision_id, good.revision_id)
            self.assertEqual(repo.read_content(art.artifact_id, good.revision_id), b"stable")

    def test_provenance_is_retained(self) -> None:
        with self.new_store() as store:
            repo = self.repo_for(store)
            art = repo.create_artifact(kind="document")
            rev = repo.import_revision(
                artifact_id=art.artifact_id,
                content=b"x",
                origin_locator="import:batch-7/a.pdf",
                rights=_valid_rights(store.project_id, art.artifact_id, "rev-1"),
                revision_id="rev-1",
                provenance={
                    "method": "import",
                    "actor": "import-tool",
                    "performed_at": "2026-10-27T10:00:00Z",
                    "origin": "import:batch-7/a.pdf",
                },
            )
            self.assertEqual(rev.provenance_method, "import")
            self.assertEqual(rev.provenance_actor, "import-tool")
            self.assertEqual(rev.provenance_performed_at_utc, "2026-10-27T10:00:00Z")
            self.assertEqual(rev.origin_locator, "import:batch-7/a.pdf")


class DocumentEnvelopeTest(ArtifactTestBase):
    def test_valid_envelope_roundtrips_bytes(self) -> None:
        with self.new_store() as store:
            repo = self.repo_for(store)
            art = repo.create_artifact(kind="finding")
            envelope = {
                "schema_version": 1,
                "document_type": "finding.draft",
                "payload": {"summary": "synthetic", "n": 3},
            }
            rev = repo.import_revision(
                artifact_id=art.artifact_id,
                content=envelope,
                origin_locator="store:finding/f1",
                rights=_valid_rights(store.project_id, art.artifact_id, "rev-1"),
                revision_id="rev-1",
            )
            self.assertEqual(rev.media_type, "application/json")
            self.assertEqual(
                repo.read_content(art.artifact_id, "rev-1"),
                b'{"document_type":"finding.draft","payload":{"n":3,"summary":"synthetic"},"schema_version":1}',
            )

    def test_newer_document_schema_refused(self) -> None:
        with self.new_store() as store:
            repo = self.repo_for(store)
            art = repo.create_artifact(kind="finding")
            with self.assertRaises(RecordValidationError):
                repo.import_revision(
                    artifact_id=art.artifact_id,
                    content={"schema_version": 2, "document_type": "future", "payload": {}},
                    origin_locator="import:x",
                    rights=RightsRef(store.project_id, "RIGHTS-rev-1", 1),
                )
            self.assertEqual(repo.list_revisions(art.artifact_id), [])


class CrossProjectTest(ArtifactTestBase):
    def test_cross_project_dependency_is_rejected(self) -> None:
        with self.new_store("a") as store_a, self.new_store("b") as store_b:
            repo_a = self.repo_for(store_a)
            repo_b = self.repo_for(store_b)
            art_a = repo_a.create_artifact(kind="document")
            rev_a = self.import_document(repo_a, art_a.artifact_id, b"a", rights_revision="rev-a")

            b_art = repo_b.create_artifact(kind="document")
            with self.assertRaises(RecordValidationError):
                repo_b.import_revision(
                    artifact_id=b_art.artifact_id,
                    content=b"b",
                    origin_locator="import:x",
                    rights=RightsRef(store_b.project_id, "RIGHTS-rev-1", 1),
                    dependencies=[
                        {"project_id": store_a.project_id, "artifact_id": art_a.artifact_id,
                         "revision_id": rev_a.revision_id}
                    ],
                )

    def test_traversal_identifiers_are_rejected(self) -> None:
        with self.new_store() as store:
            repo = self.repo_for(store)
            for bad in ("../etc", "a/b", "..", ".hidden", "a b"):
                with self.assertRaises(RecordValidationError):
                    repo.create_artifact(bad, kind="document")

    def test_cross_project_rights_ref_is_rejected(self) -> None:
        with self.new_store() as store:
            repo = self.repo_for(store)
            art = repo.create_artifact(kind="document")
            # A rights record whose project_id differs from this store is refused.
            foreign = _valid_rights("prj_" + "b" * 32, art.artifact_id, "rev-1")
            with self.assertRaises(RecordValidationError):
                repo.import_revision(
                    artifact_id=art.artifact_id,
                    content=b"x",
                    origin_locator="import:x",
                    rights=foreign,
                    revision_id="rev-1",
                )


class RightsPinningTest(ArtifactTestBase):
    def test_same_identity_changed_bytes_rejected(self) -> None:
        with self.new_store() as store:
            repo = self.repo_for(store)
            art = repo.create_artifact(kind="document")
            first = _valid_rights(store.project_id, art.artifact_id, "rev-1")
            repo.store_rights_record(first)
            # Same {project_id, rights_id, record_revision} with a changed field.
            changed = _valid_rights(store.project_id, art.artifact_id, "rev-1")
            changed["recorded_by"] = "someone-else"
            with self.assertRaises(ArtifactConflictError):
                repo.store_rights_record(changed)

    def test_raw_storage_right_not_persisted(self) -> None:
        with self.new_store() as store:
            repo = self.repo_for(store)
            art = repo.create_artifact(kind="document")
            rights = _valid_rights(store.project_id, art.artifact_id, "rev-1")
            rights["operations"]["storage"] = "unknown"
            rights["basis"]["storage"] = {"kind": "unknown", "evidence": {"ref": "", "provenance": ""}}
            with self.assertRaises(RecordValidationError):
                repo.import_revision(
                    artifact_id=art.artifact_id,
                    content=b"secret bytes",
                    origin_locator="import:x",
                    rights=rights,
                    revision_id="rev-1",
                )
            # Nothing persisted: no revision, no rights row, no bytes.
            self.assertEqual(repo.list_revisions(art.artifact_id), [])
            self.assertIsNone(repo.get_rights_record(RightsRef(store.project_id, "RIGHTS-rev-1", 1)))

    def test_metadata_rights_record_may_persist_when_storage_unknown(self) -> None:
        with self.new_store() as store:
            repo = self.repo_for(store)
            art = repo.create_artifact(kind="document")
            rights = _valid_rights(store.project_id, art.artifact_id, "rev-1")
            rights["operations"]["storage"] = "unknown"
            rights["basis"]["storage"] = {"kind": "unknown", "evidence": {"ref": "", "provenance": ""}}
            # Metadata-only quarantine path: the validated record is stored, no bytes.
            ref = repo.store_rights_record(rights)
            self.assertEqual(ref.record_revision, 1)
            stored = repo.get_rights_record(ref)
            self.assertEqual(stored["operations"]["storage"], "unknown")
            self.assertEqual(repo.list_revisions(art.artifact_id), [])


class ImmutabilityTest(ArtifactTestBase):
    def test_raw_revision_update_and_delete_denied(self) -> None:
        with self.new_store() as store:
            repo = self.repo_for(store)
            art = repo.create_artifact(kind="document")
            rev = self.import_document(repo, art.artifact_id, b"frozen", rights_revision="rev-1")
            conn = store.connection
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("UPDATE revision SET content = ? WHERE revision_id = ?", (b"tampered", rev.revision_id))
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("DELETE FROM revision WHERE revision_id = ?", (rev.revision_id,))
            self.assertEqual(repo.read_content(art.artifact_id, rev.revision_id), b"frozen")

    def test_raw_dependency_and_rights_update_denied(self) -> None:
        with self.new_store() as store:
            repo = self.repo_for(store)
            art = repo.create_artifact(kind="document")
            rev = self.import_document(repo, art.artifact_id, b"x", rights_revision="rev-1")
            conn = store.connection
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("UPDATE rights_record SET canonical = ? WHERE rights_id = ?", (b"{}", "RIGHTS-rev-1"))
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("DELETE FROM rights_record WHERE rights_id = ?", ("RIGHTS-rev-1",))
            # artifact identity cannot be changed to merge/reuse an id
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("UPDATE artifact SET kind = 'study' WHERE artifact_id = ?", (art.artifact_id,))


class TransactionFaultTest(ArtifactTestBase):
    def test_injected_failure_rolls_back_all_rows(self) -> None:
        with self.new_store() as store:
            repo = self.repo_for(store)
            base = repo.create_artifact(kind="document")
            base_rev = self.import_document(repo, base.artifact_id, b"base", rights_revision="rev-base")

            target = repo.create_artifact(kind="finding")
            with mock.patch.object(
                ArtifactRepository, "_insert_dependency", side_effect=RuntimeError("injected")
            ):
                with self.assertRaises(RuntimeError):
                    self.import_document(
                        repo, target.artifact_id, b"derived", rights_revision="rev-new",
                        request_id="req-fault",
                        dependencies=[
                            {"project_id": store.project_id, "artifact_id": base.artifact_id,
                             "revision_id": base_rev.revision_id}
                        ],
                    )

            # No half rows: no revision, no rights row, no dependency, no request.
            self.assertEqual(repo.list_revisions(target.artifact_id), [])
            self.assertIsNone(repo.get_rights_record(RightsRef(store.project_id, "RIGHTS-rev-new", 1)))
            self.assertIsNone(repo._import_request_row("req-fault"))
            count = store.connection.execute("SELECT COUNT(*) FROM dependency").fetchone()[0]
            self.assertEqual(count, 0)
            # The unrelated base revision is intact.
            self.assertEqual(repo.read_content(base.artifact_id, base_rev.revision_id), b"base")

    def test_failure_leaves_no_dangling_foreign_keys(self) -> None:
        with self.new_store() as store:
            repo = self.repo_for(store)
            base = repo.create_artifact(kind="document")
            base_rev = self.import_document(repo, base.artifact_id, b"base", rights_revision="rev-base")
            target = repo.create_artifact(kind="finding")
            with mock.patch.object(
                ArtifactRepository, "_insert_dependency", side_effect=RuntimeError("injected")
            ):
                with self.assertRaises(RuntimeError):
                    self.import_document(
                        repo, target.artifact_id, b"x", rights_revision="rev-new",
                        request_id="req-fk",
                        dependencies=[
                            {"project_id": store.project_id, "artifact_id": base.artifact_id,
                             "revision_id": base_rev.revision_id}
                        ],
                    )
            # No dangling reference and no leftover request row.
            violations = store.connection.execute("PRAGMA foreign_key_check").fetchall()
            self.assertEqual(violations, [])
            self.assertIsNone(repo._import_request_row("req-fk"))


class MigrationTest(ArtifactTestBase):
    def test_migration1_metadata_preserved_across_v2(self) -> None:
        app_dir = self.tmp / "v1proj" / paths.APP_DIR_NAME
        paths.ensure_owner_private_dir(str(app_dir))
        conn = schema.open_connection(paths.store_path_for(str(self.tmp / "v1proj")))
        try:
            schema.migrate(
                conn, target_version=1, migrations=schema.STORE_MIGRATIONS,
                label="project store",
            )
            conn.execute(
                "INSERT INTO project (project_id, name, created_at_utc, app_version) "
                "VALUES (?, ?, ?, ?)",
                ("prj_" + "c" * 32, "V1", "2026-01-01T00:00:00+00:00", "0.0.0"),
            )
            conn.execute("INSERT INTO meta (key, value) VALUES ('created_at_utc', '2026-01-01T00:00:00+00:00')")
        finally:
            conn.close()

        # Opening migrates v1 -> v2 without rewriting the v1 rows.
        with ProjectStore.open_at(str(app_dir)) as store:
            self.assertEqual(store.schema_version, 2)
            meta = store.metadata()
            self.assertEqual(meta["created_at_utc"], "2026-01-01T00:00:00+00:00")
            self.assertEqual(meta["project_id"], "prj_" + "c" * 32)
            # artifact/revision tables exist after the migration.
            names = {
                r[0]
                for r in store.connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            self.assertTrue({"artifact", "revision", "rights_record", "dependency", "import_request"} <= names)

    def test_newer_store_schema_refused(self) -> None:
        store = self.new_store()
        db_path = store.db_path
        store.close()
        conn = schema.open_connection(db_path)
        try:
            conn.execute("PRAGMA user_version = 999")
        finally:
            conn.close()
        with self.assertRaises(UnsupportedSchemaVersion):
            ProjectStore.open_at(paths.app_dir_for(str(self.tmp / "p")))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
