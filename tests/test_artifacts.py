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
            # invalid dependency (unknown target revision), as a *new* revision
            with self.assertRaises(RecordValidationError):
                repo.import_revision(
                    artifact_id=art.artifact_id,
                    content=b"bad",
                    origin_locator="import:x",
                    rights=_valid_rights(store.project_id, art.artifact_id, "rev-dep"),
                    revision_id="rev-dep",
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

        # Opening migrates v1 -> current schema without rewriting the v1 rows.
        with ProjectStore.open_at(str(app_dir)) as store:
            self.assertEqual(store.schema_version, schema.STORE_SCHEMA_VERSION)
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


def _pre_acquisition_rights(project_id, artifact_id, **overrides):
    """A valid pre-acquisition RightsRecord (subject_artifact_ref, no revision)."""
    ops = {op: "unknown" for op in _OPS}
    record = {
        "rights_id": overrides.pop("rights_id", "RIGHTS-pre-1"),
        "project_id": project_id,
        "record_revision": overrides.pop("record_revision", 1),
        "subject": {
            "kind": "subject_artifact_ref",
            "project_id": project_id,
            "artifact_id": artifact_id,
        },
        "operations": ops,
        "basis": {
            op: {"kind": "unknown", "evidence": {"ref": "", "provenance": ""}}
            for op in _OPS
        },
        "authorization": None,
        "restrictions": [],
        "privacy": {"personal_data": False, "confidentiality": "unknown", "denial": False},
        "recorded_by": "agent:recorder",
        "recorded_at": "2026-10-27T00:00:00Z",
        "derived_status": "valid",
    }
    record.update(overrides)
    return record


class RightsRefImportTest(ArtifactTestBase):
    """Every import path must fully validate rights for THIS exact revision."""

    def _stored_ref(self, repo, artifact_id, revision_id, **changes):
        rights = _valid_rights(repo.project_id, artifact_id, revision_id)
        rights.update(changes)
        return repo.store_rights_record(rights)

    def test_ref_with_unknown_storage_rejected_no_bytes(self) -> None:
        with self.new_store() as store:
            repo = self.repo_for(store)
            art = repo.create_artifact("a", kind="document")
            rights = _valid_rights(store.project_id, "a", "r1")
            rights["operations"]["storage"] = "unknown"
            rights["basis"]["storage"] = {"kind": "unknown", "evidence": {"ref": "", "provenance": ""}}
            ref = repo.store_rights_record(rights)
            with self.assertRaises(RecordValidationError):
                repo.import_revision(
                    artifact_id="a",
                    revision_id="r1",
                    content=b"not-authorized",
                    origin_locator="synthetic",
                    rights=ref,
                )
            self.assertEqual(repo.list_revisions("a"), [])
            self.assertIsNone(repo.get_revision("a", "r1"))

    def test_ref_with_not_allowed_storage_rejected(self) -> None:
        with self.new_store() as store:
            repo = self.repo_for(store)
            repo.create_artifact("a", kind="document")
            rights = _valid_rights(store.project_id, "a", "r1")
            rights["operations"]["storage"] = "not_allowed"
            rights["basis"]["storage"] = {"kind": "unknown", "evidence": {"ref": "", "provenance": ""}}
            ref = repo.store_rights_record(rights)
            with self.assertRaises(RecordValidationError):
                repo.import_revision(
                    artifact_id="a", revision_id="r1", content=b"x",
                    origin_locator="synthetic", rights=ref,
                )
            self.assertEqual(repo.list_revisions("a"), [])

    def test_ref_with_privacy_denial_rejected(self) -> None:
        with self.new_store() as store:
            repo = self.repo_for(store)
            repo.create_artifact("a", kind="document")
            rights = _valid_rights(store.project_id, "a", "r1")
            rights["privacy"]["denial"] = True
            ref = repo.store_rights_record(rights)
            with self.assertRaises(RecordValidationError):
                repo.import_revision(
                    artifact_id="a", revision_id="r1", content=b"x",
                    origin_locator="synthetic", rights=ref,
                )
            self.assertEqual(repo.list_revisions("a"), [])

    def test_ref_for_other_revision_or_artifact_rejected(self) -> None:
        with self.new_store() as store:
            repo = self.repo_for(store)
            repo.create_artifact("a", kind="document")
            repo.create_artifact("b", kind="document")
            ref_r2 = self._stored_ref(repo, "a", "r2")   # bound to r2
            ref_b = self._stored_ref(repo, "b", "r1")    # bound to artifact b
            # Wrong revision id for this ref's subject.
            with self.assertRaises(RecordValidationError):
                repo.import_revision(
                    artifact_id="a", revision_id="r1", content=b"x",
                    origin_locator="synthetic", rights=ref_r2,
                )
            # Ref bound to a different artifact.
            with self.assertRaises(RecordValidationError):
                repo.import_revision(
                    artifact_id="a", revision_id="r1", content=b"x",
                    origin_locator="synthetic", rights=ref_b,
                )
            self.assertEqual(repo.list_revisions("a"), [])

    def test_ref_cross_project_rejected(self) -> None:
        with self.new_store("a") as store_a, self.new_store("b") as store_b:
            repo_a = self.repo_for(store_a)
            repo_b = self.repo_for(store_b)
            repo_a.create_artifact("a", kind="document")
            ref = repo_a.store_rights_record(_valid_rights(store_a.project_id, "a", "r1"))
            with self.assertRaises(RecordValidationError):
                repo_b.import_revision(
                    artifact_id="a", revision_id="r1", content=b"x",
                    origin_locator="synthetic", rights=ref,
                )
            self.assertEqual(repo_b.list_revisions("a"), [])

    def test_ref_bool_record_revision_rejected(self) -> None:
        with self.new_store() as store:
            repo = self.repo_for(store)
            repo.create_artifact("a", kind="document")
            bad = {"project_id": store.project_id, "rights_id": "RIGHTS-1", "record_revision": True}
            with self.assertRaises(RecordValidationError):
                repo.import_revision(
                    artifact_id="a", revision_id="r1", content=b"x",
                    origin_locator="synthetic", rights=bad,
                )
            # A pre-built RightsRef with a bool revision is refused too.
            with self.assertRaises(RecordValidationError):
                repo.get_rights_record(RightsRef(store.project_id, "RIGHTS-1", True))

    def test_ref_omitting_revision_id_derives_bound_id(self) -> None:
        with self.new_store() as store:
            repo = self.repo_for(store)
            repo.create_artifact("a", kind="document")
            ref = repo.store_rights_record(_valid_rights(store.project_id, "a", "r-bound"))
            rev = repo.import_revision(
                artifact_id="a", content=b"bytes", origin_locator="synthetic",
                rights=ref,  # no revision_id: derive from the bound subject
            )
            self.assertEqual(rev.revision_id, "r-bound")
            self.assertEqual(repo.read_content("a", "r-bound"), b"bytes")

    def test_metadata_pre_acquisition_subject_artifact_ref(self) -> None:
        with self.new_store() as store:
            repo = self.repo_for(store)
            art = repo.create_artifact("a", kind="document")
            ref = repo.store_rights_record(_pre_acquisition_rights(store.project_id, "a"))
            stored = repo.get_rights_record(ref)
            self.assertEqual(stored["subject"]["kind"], "subject_artifact_ref")
            self.assertNotIn("revision_id", stored["subject"])
            # Pre-acquisition metadata cannot authorize content bytes.
            with self.assertRaises(RecordValidationError):
                repo.import_revision(
                    artifact_id="a", revision_id="r1", content=b"x",
                    origin_locator="synthetic", rights=ref,
                )
            self.assertEqual(repo.list_revisions("a"), [])


class ParentFreshnessTest(ArtifactTestBase):
    def test_stale_parent_edit_denied_no_rows(self) -> None:
        with self.new_store() as store:
            repo = self.repo_for(store)
            repo.create_artifact("a", kind="document")
            self.import_document(repo, "a", b"r1", rights_revision="r1")
            self.import_document(repo, "a", b"r2", rights_revision="r2", parent_revision_id="r1")
            with self.assertRaises(RecordValidationError):
                self.import_document(
                    repo, "a", b"r3", rights_revision="r3", parent_revision_id="r1"
                )
            self.assertEqual(len(repo.list_revisions("a")), 2)
            self.assertEqual(repo.current_revision("a").revision_id, "r2")

    def test_second_revision_requires_current_parent(self) -> None:
        with self.new_store() as store:
            repo = self.repo_for(store)
            repo.create_artifact("a", kind="document")
            self.import_document(repo, "a", b"r1", rights_revision="r1")
            with self.assertRaises(RecordValidationError):
                self.import_document(repo, "a", b"r2", rights_revision="r2")
            self.assertEqual(len(repo.list_revisions("a")), 1)

    def test_first_revision_must_not_name_parent(self) -> None:
        with self.new_store() as store:
            repo = self.repo_for(store)
            repo.create_artifact("a", kind="document")
            with self.assertRaises(RecordValidationError):
                self.import_document(
                    repo, "a", b"x", rights_revision="r1", parent_revision_id="r0"
                )
            self.assertEqual(repo.list_revisions("a"), [])

    def test_idempotent_replay_before_head_returns_historical(self) -> None:
        with self.new_store() as store:
            repo = self.repo_for(store)
            repo.create_artifact("a", kind="document")
            r1 = self.import_document(
                repo, "a", b"one", rights_revision="r1", request_id="req-1"
            )
            self.import_document(
                repo, "a", b"two", rights_revision="r2", parent_revision_id="r1"
            )
            # Replaying the r1 request after a new head returns the historical r1.
            again = self.import_document(
                repo, "a", b"one", rights_revision="r1", request_id="req-1"
            )
            self.assertEqual(again.revision_id, r1.revision_id)
            self.assertEqual(repo.current_revision("a").revision_id, "r2")

    def test_duplicate_replay_after_later_revision_returns_historical(self) -> None:
        with self.new_store() as store:
            repo = self.repo_for(store)
            repo.create_artifact("a", kind="document")
            r1 = self.import_document(repo, "a", b"one", rights_revision="r1")
            self.import_document(
                repo, "a", b"two", rights_revision="r2", parent_revision_id="r1"
            )
            # Identical import of r1's payload (no request id) reuses historical r1.
            again = self.import_document(repo, "a", b"one", rights_revision="r1")
            self.assertEqual(again.revision_id, r1.revision_id)
            self.assertEqual(len(repo.list_revisions("a")), 2)


class IdempotencyFingerprintTest(ArtifactTestBase):
    def test_rights_changed_on_same_request_conflicts(self) -> None:
        with self.new_store() as store:
            repo = self.repo_for(store)
            repo.create_artifact("a", kind="document")
            first = _valid_rights(store.project_id, "a", "r1")
            repo.import_revision(
                artifact_id="a", revision_id="r1", content=b"x",
                origin_locator="synthetic", rights=first, request_id="req-x",
            )
            changed = _valid_rights(store.project_id, "a", "r1")
            changed["restrictions"] = ["quote_limit"]
            with self.assertRaises(ArtifactConflictError):
                repo.import_revision(
                    artifact_id="a", revision_id="r1", content=b"x",
                    origin_locator="synthetic", rights=changed, request_id="req-x",
                )
            self.assertEqual(len(repo.list_revisions("a")), 1)

    def test_provenance_changed_on_same_request_conflicts(self) -> None:
        with self.new_store() as store:
            repo = self.repo_for(store)
            repo.create_artifact("a", kind="document")
            rights = _valid_rights(store.project_id, "a", "r1")
            repo.import_revision(
                artifact_id="a", revision_id="r1", content=b"x",
                origin_locator="synthetic", rights=rights, request_id="req-p",
                provenance={"method": "import", "actor": "actor-a",
                            "performed_at": "2026-10-27T00:00:00Z", "origin": "synthetic"},
            )
            with self.assertRaises(ArtifactConflictError):
                repo.import_revision(
                    artifact_id="a", revision_id="r1", content=b"x",
                    origin_locator="synthetic", rights=rights, request_id="req-p",
                    provenance={"method": "import", "actor": "actor-b",
                                "performed_at": "2026-10-27T00:00:00Z", "origin": "synthetic"},
                )
            self.assertEqual(len(repo.list_revisions("a")), 1)


class SupersededRightsTest(ArtifactTestBase):
    """A superseded rights record cannot authorize a new import (fail closed)."""

    def _allowed(self, store, record_revision):
        return _valid_rights(
            store.project_id, "a", "r1",
            rights_id="RIGHTS-x", record_revision=record_revision,
        )

    def _storage(self, record, status, basis_kind="unknown"):
        record = dict(record)
        record["operations"] = dict(record["operations"])
        record["operations"]["storage"] = status
        record["basis"] = dict(record["basis"])
        record["basis"]["storage"] = {"kind": basis_kind, "evidence": {"ref": "", "provenance": ""}}
        return record

    def test_ref_to_superseded_allowed_record_rejected(self) -> None:
        with self.new_store() as store:
            repo = self.repo_for(store)
            repo.create_artifact("a", kind="document")
            ref1 = repo.store_rights_record(self._allowed(store, 1))  # formerly allowed
            # A newer record revision for the same rights id revokes storage.
            repo.store_rights_record(
                self._storage(self._allowed(store, 2), "not_allowed")
            )
            with self.assertRaises(ArtifactConflictError):
                repo.import_revision(
                    artifact_id="a", revision_id="r1", content=b"after-revocation",
                    origin_locator="synthetic", rights=ref1,
                )
            self.assertEqual(repo.list_revisions("a"), [])
            self.assertEqual(
                store.connection.execute("SELECT COUNT(*) FROM import_request").fetchone()[0], 0
            )
            # Historical rights data is preserved and readable.
            self.assertEqual(repo.get_rights_record(ref1)["record_revision"], 1)

    def test_raw_old_while_newer_exists_rejected(self) -> None:
        with self.new_store() as store:
            repo = self.repo_for(store)
            repo.create_artifact("a", kind="document")
            repo.store_rights_record(self._allowed(store, 1))
            repo.store_rights_record(
                self._storage(self._allowed(store, 2), "not_allowed")
            )
            old_raw = self._allowed(store, 1)
            with self.assertRaises(ArtifactConflictError):
                repo.import_revision(
                    artifact_id="a", revision_id="r1", content=b"x",
                    origin_locator="synthetic", rights=old_raw,
                )
            self.assertEqual(repo.list_revisions("a"), [])

    def test_newest_allowed_record_imports(self) -> None:
        with self.new_store() as store:
            repo = self.repo_for(store)
            repo.create_artifact("a", kind="document")
            repo.store_rights_record(self._allowed(store, 1))
            ref2 = repo.store_rights_record(self._allowed(store, 2))  # newest, allowed
            rev = repo.import_revision(
                artifact_id="a", revision_id="r1", content=b"authorized",
                origin_locator="synthetic", rights=ref2,
            )
            self.assertEqual(rev.rights_ref.record_revision, 2)
            self.assertEqual(repo.read_content("a", "r1"), b"authorized")

    def test_newest_unknown_record_blocked(self) -> None:
        with self.new_store() as store:
            repo = self.repo_for(store)
            repo.create_artifact("a", kind="document")
            repo.store_rights_record(self._allowed(store, 1))
            ref2 = repo.store_rights_record(
                self._storage(self._allowed(store, 2), "unknown")
            )
            with self.assertRaises(RecordValidationError):
                repo.import_revision(
                    artifact_id="a", revision_id="r1", content=b"x",
                    origin_locator="synthetic", rights=ref2,
                )
            self.assertEqual(repo.list_revisions("a"), [])


class ExplicitProvenanceTest(ArtifactTestBase):
    """Explicit timestamps are replayed exactly; provenance.origin is strict."""

    def test_explicit_acquired_at_exact_retry_ok_changed_conflicts(self) -> None:
        with self.new_store() as store:
            repo = self.repo_for(store)
            repo.create_artifact("a", kind="document")
            first = repo.import_revision(
                artifact_id="a", revision_id="r1", content=b"x",
                origin_locator="synthetic",
                rights=_valid_rights(store.project_id, "a", "r1"),
                request_id="req-t", acquired_at_utc="2026-10-27T05:00:00Z",
            )
            again = repo.import_revision(
                artifact_id="a", revision_id="r1", content=b"x",
                origin_locator="synthetic",
                rights=_valid_rights(store.project_id, "a", "r1"),
                request_id="req-t", acquired_at_utc="2026-10-27T05:00:00Z",
            )
            self.assertEqual(again.revision_id, first.revision_id)
            with self.assertRaises(ArtifactConflictError):
                repo.import_revision(
                    artifact_id="a", revision_id="r1", content=b"x",
                    origin_locator="synthetic",
                    rights=_valid_rights(store.project_id, "a", "r1"),
                    request_id="req-t", acquired_at_utc="2026-10-27T06:00:00Z",
                )
            self.assertEqual(len(repo.list_revisions("a")), 1)

    def test_explicit_performed_at_exact_retry_ok_changed_conflicts(self) -> None:
        with self.new_store() as store:
            repo = self.repo_for(store)
            repo.create_artifact("a", kind="document")

            def prov(at):
                return {"method": "import", "actor": "actor-a", "performed_at": at,
                        "origin": "synthetic"}

            first = repo.import_revision(
                artifact_id="a", revision_id="r1", content=b"x",
                origin_locator="synthetic",
                rights=_valid_rights(store.project_id, "a", "r1"),
                request_id="req-p", provenance=prov("2026-10-27T05:00:00Z"),
            )
            again = repo.import_revision(
                artifact_id="a", revision_id="r1", content=b"x",
                origin_locator="synthetic",
                rights=_valid_rights(store.project_id, "a", "r1"),
                request_id="req-p", provenance=prov("2026-10-27T05:00:00Z"),
            )
            self.assertEqual(again.revision_id, first.revision_id)
            with self.assertRaises(ArtifactConflictError):
                repo.import_revision(
                    artifact_id="a", revision_id="r1", content=b"x",
                    origin_locator="synthetic",
                    rights=_valid_rights(store.project_id, "a", "r1"),
                    request_id="req-p", provenance=prov("2026-10-27T06:00:00Z"),
                )

    def test_explicit_acquired_at_conflicts_on_duplicate_shortcut(self) -> None:
        with self.new_store() as store:
            repo = self.repo_for(store)
            repo.create_artifact("a", kind="document")
            repo.import_revision(
                artifact_id="a", revision_id="r1", content=b"x",
                origin_locator="synthetic",
                rights=_valid_rights(store.project_id, "a", "r1"),
                acquired_at_utc="2026-10-27T05:00:00Z",
            )
            # No request id: the duplicate shortcut must still compare the
            # explicitly supplied acquisition time against the stored row.
            with self.assertRaises(ArtifactConflictError):
                repo.import_revision(
                    artifact_id="a", revision_id="r1", content=b"x",
                    origin_locator="synthetic",
                    rights=_valid_rights(store.project_id, "a", "r1"),
                    acquired_at_utc="2026-10-27T06:00:00Z",
                )

    def test_provenance_origin_must_equal_origin_locator(self) -> None:
        with self.new_store() as store:
            repo = self.repo_for(store)
            repo.create_artifact("a", kind="document")
            with self.assertRaises(RecordValidationError):
                repo.import_revision(
                    artifact_id="a", revision_id="r1", content=b"x",
                    origin_locator="origin-a",
                    rights=_valid_rights(store.project_id, "a", "r1"),
                    provenance={"method": "import", "actor": "a",
                                "performed_at": "2026-10-27T05:00:00Z",
                                "origin": "origin-b"},
                )
            self.assertEqual(repo.list_revisions("a"), [])


class IdempotencyBoundaryTest(ArtifactTestBase):
    """Duplicate/replay shortcuts must compare ALL meaningful inputs."""

    def _counts(self, store):
        c = store.connection
        return (
            c.execute("SELECT COUNT(*) FROM revision").fetchone()[0],
            c.execute("SELECT COUNT(*) FROM rights_record").fetchone()[0],
            c.execute("SELECT COUNT(*) FROM import_request").fetchone()[0],
        )

    def test_changed_origin_same_revision_conflicts(self) -> None:
        with self.new_store() as store:
            repo = self.repo_for(store)
            repo.create_artifact("a", kind="document")
            self.import_document(
                repo, "a", b"synthetic", rights_revision="r1"
            )  # origin default import:synthetic
            with self.assertRaises(ArtifactConflictError):
                repo.import_revision(
                    artifact_id="a", revision_id="r1", content=b"synthetic",
                    origin_locator="changed",
                    rights=_valid_rights(store.project_id, "a", "r1"),
                )
            self.assertEqual(len(repo.list_revisions("a")), 1)
            self.assertEqual(repo.current_revision("a").origin_locator, "import:synthetic")

    def test_changed_rights_same_identity_conflicts(self) -> None:
        with self.new_store() as store:
            repo = self.repo_for(store)
            repo.create_artifact("a", kind="document")
            self.import_document(repo, "a", b"x", rights_revision="r1")
            changed = _valid_rights(store.project_id, "a", "r1")
            changed["restrictions"] = ["new-restriction"]
            with self.assertRaises(ArtifactConflictError):
                repo.import_revision(
                    artifact_id="a", revision_id="r1", content=b"x",
                    origin_locator="import:synthetic", rights=changed,
                )
            self.assertEqual(len(repo.list_revisions("a")), 1)
            stored = repo.get_rights_record(RightsRef(store.project_id, "RIGHTS-r1", 1))
            self.assertEqual(stored["restrictions"], [])

    def test_new_request_key_binds_existing_identical_revision(self) -> None:
        with self.new_store() as store:
            repo = self.repo_for(store)
            repo.create_artifact("a", kind="document")
            r1 = self.import_document(repo, "a", b"x", rights_revision="r1")
            # A NEW request key that resolves to the identical revision is bound.
            again = self.import_document(
                repo, "a", b"x", rights_revision="r1", request_id="new-request"
            )
            self.assertEqual(again.revision_id, r1.revision_id)
            self.assertIsNotNone(repo._import_request_row("new-request"))
            # A later divergent reuse of that key is rejected, not accepted.
            with self.assertRaises(ArtifactConflictError):
                repo.import_revision(
                    artifact_id="a", revision_id="r1", content=b"x",
                    origin_locator="changed",
                    rights=_valid_rights(store.project_id, "a", "r1"),
                    request_id="new-request",
                )
            self.assertEqual(len(repo.list_revisions("a")), 1)

    def test_exact_duplicate_returns_stable_revision_no_new_head(self) -> None:
        with self.new_store() as store:
            repo = self.repo_for(store)
            repo.create_artifact("a", kind="document")
            r1 = self.import_document(repo, "a", b"x", rights_revision="r1")
            again = self.import_document(repo, "a", b"x", rights_revision="r1")
            self.assertEqual(again.revision_id, r1.revision_id)
            self.assertEqual(len(repo.list_revisions("a")), 1)
            self.assertEqual(repo.current_revision("a").revision_id, r1.revision_id)

    def test_conflict_leaves_no_new_rows_or_rights(self) -> None:
        with self.new_store() as store:
            repo = self.repo_for(store)
            repo.create_artifact("a", kind="document")
            self.import_document(repo, "a", b"x", rights_revision="r1")
            before = self._counts(store)
            # Conflicting content under an existing (pinned) revision id.
            with self.assertRaises(ArtifactConflictError):
                repo.import_revision(
                    artifact_id="a", revision_id="r1", content=b"different",
                    origin_locator="import:synthetic",
                    rights=_valid_rights(store.project_id, "a", "r1"),
                    request_id="req-conflict",
                )
            self.assertEqual(self._counts(store), before)
            self.assertIsNone(repo._import_request_row("req-conflict"))

    def test_rights_divergence_conflict_leaves_no_rows(self) -> None:
        with self.new_store() as store:
            repo = self.repo_for(store)
            repo.create_artifact("a", kind="document")
            self.import_document(repo, "a", b"x", rights_revision="r1")
            before = self._counts(store)
            changed = _valid_rights(store.project_id, "a", "r1")
            changed["restrictions"] = ["new-restriction"]
            with self.assertRaises(ArtifactConflictError):
                repo.import_revision(
                    artifact_id="a", revision_id="r1", content=b"x",
                    origin_locator="import:synthetic", rights=changed,
                )
            self.assertEqual(self._counts(store), before)


class ReturnedDependenciesTest(ArtifactTestBase):
    def test_returned_dependencies_populated(self) -> None:
        with self.new_store() as store:
            repo = self.repo_for(store)
            base = repo.create_artifact("base", kind="document")
            base_rev = self.import_document(repo, "base", b"base", rights_revision="rb")
            derived = repo.create_artifact("derived", kind="finding")
            rev = repo.import_revision(
                artifact_id="derived",
                content=b"out",
                origin_locator="synthetic",
                rights=_valid_rights(store.project_id, "derived", "rd"),
                revision_id="rd",
                dependencies=[
                    {"project_id": store.project_id, "artifact_id": "base",
                     "revision_id": base_rev.revision_id}
                ],
            )
            self.assertEqual(len(rev.dependencies), 1)
            dep = rev.dependencies[0]
            self.assertEqual((dep.artifact_id, dep.revision_id, dep.role),
                             ("base", base_rev.revision_id, "derived_from"))
            # Also populated from the read API.
            self.assertEqual(len(repo.get_revision("derived", "rd").dependencies), 1)


class DependencyRoleTest(ArtifactTestBase):
    def _revision(self, repo, artifact_id, kind, content, revision_id, **kwargs):
        repo.create_artifact(artifact_id, kind=kind)
        return repo.import_revision(
            artifact_id=artifact_id,
            content=content,
            origin_locator="synthetic",
            rights=_valid_rights(repo.project_id, artifact_id, revision_id),
            revision_id=revision_id,
            **kwargs,
        )

    def _dep(self, store, artifact_id, revision_id, role):
        return {"project_id": store.project_id, "artifact_id": artifact_id,
                "revision_id": revision_id, "role": role}

    def test_reports_must_target_study(self) -> None:
        with self.new_store() as store:
            repo = self.repo_for(store)
            study = self._revision(repo, "S", "study", b"s", "rs")
            doc = self._revision(repo, "D2", "document", b"d2", "rd")
            # Valid: document reports a study.
            self._revision(repo, "D", "document", b"d", "rd1",
                           dependencies=[self._dep(store, "S", study.revision_id, "reports")])
            # Invalid: reports a document.
            repo.create_artifact("X", kind="document")
            with self.assertRaises(RecordValidationError):
                repo.import_revision(
                    artifact_id="X", content=b"x", origin_locator="synthetic",
                    rights=_valid_rights(store.project_id, "X", "rx"),
                    revision_id="rx",
                    dependencies=[self._dep(store, "D2", doc.revision_id, "reports")],
                )

    def test_uses_dataset_must_target_dataset(self) -> None:
        with self.new_store() as store:
            repo = self.repo_for(store)
            ds = self._revision(repo, "DS", "dataset", b"ds", "rds")
            self._revision(repo, "U", "document", b"u", "ru",
                           dependencies=[self._dep(store, "DS", ds.revision_id, "uses_dataset")])
            doc = self._revision(repo, "DOC", "document", b"doc", "rdoc")
            repo.create_artifact("U2", kind="finding")
            with self.assertRaises(RecordValidationError):
                repo.import_revision(
                    artifact_id="U2", content=b"u2", origin_locator="synthetic",
                    rights=_valid_rights(store.project_id, "U2", "ru2"), revision_id="ru2",
                    dependencies=[self._dep(store, "DOC", doc.revision_id, "uses_dataset")],
                )

    def test_describes_experiment_must_target_experiment(self) -> None:
        with self.new_store() as store:
            repo = self.repo_for(store)
            ex = self._revision(repo, "EX", "experiment", b"ex", "rex")
            self._revision(repo, "D3", "document", b"d3", "rd3",
                           dependencies=[self._dep(store, "EX", ex.revision_id, "describes_experiment")])
            doc = self._revision(repo, "DOC2", "document", b"doc2", "rdoc2")
            repo.create_artifact("D4", kind="document")
            with self.assertRaises(RecordValidationError):
                repo.import_revision(
                    artifact_id="D4", content=b"d4", origin_locator="synthetic",
                    rights=_valid_rights(store.project_id, "D4", "rd4"), revision_id="rd4",
                    dependencies=[self._dep(store, "DOC2", doc.revision_id, "describes_experiment")],
                )

    def test_version_of_must_be_same_artifact(self) -> None:
        with self.new_store() as store:
            repo = self.repo_for(store)
            a1 = self._revision(repo, "A", "document", b"a1", "ra1")
            other = self._revision(repo, "B", "document", b"b", "rb")
            # Same artifact, older revision -> valid (parent is the fresh head).
            self._revision(repo, "A", "document", b"a2", "ra2", parent_revision_id=a1.revision_id,
                           dependencies=[self._dep(store, "A", a1.revision_id, "version_of")])
            # Cross-artifact version_of -> rejected.
            repo.create_artifact("C", kind="document")
            with self.assertRaises(RecordValidationError):
                repo.import_revision(
                    artifact_id="C", content=b"c", origin_locator="synthetic",
                    rights=_valid_rights(store.project_id, "C", "rc"), revision_id="rc",
                    dependencies=[self._dep(store, "B", other.revision_id, "version_of")],
                )

    def test_duplicate_bytes_of_requires_equal_bytes(self) -> None:
        with self.new_store() as store:
            repo = self.repo_for(store)
            r1 = self._revision(repo, "P", "document", b"same-bytes", "rp1")
            # Equal bytes -> valid.
            self._revision(repo, "Q", "document", b"same-bytes", "rq1",
                           dependencies=[self._dep(store, "P", r1.revision_id, "duplicate_bytes_of")])
            # Different bytes -> rejected.
            repo.create_artifact("R", kind="document")
            with self.assertRaises(RecordValidationError):
                repo.import_revision(
                    artifact_id="R", content=b"different", origin_locator="synthetic",
                    rights=_valid_rights(store.project_id, "R", "rr"), revision_id="rr",
                    dependencies=[self._dep(store, "P", r1.revision_id, "duplicate_bytes_of")],
                )

    def test_unsupported_role_rejected(self) -> None:
        with self.new_store() as store:
            repo = self.repo_for(store)
            r1 = self._revision(repo, "P", "document", b"p", "rp")
            repo.create_artifact("Z", kind="finding")
            with self.assertRaises(RecordValidationError):
                repo.import_revision(
                    artifact_id="Z", content=b"z", origin_locator="synthetic",
                    rights=_valid_rights(store.project_id, "Z", "rz"), revision_id="rz",
                    dependencies=[self._dep(store, "P", r1.revision_id, "extracted_from")],
                )


class TypedErrorTest(ArtifactTestBase):
    def test_document_envelope_nonstring_key_typed_error(self) -> None:
        with self.new_store() as store:
            repo = self.repo_for(store)
            repo.create_artifact("a", kind="document")
            with self.assertRaises(RecordValidationError):
                repo.import_revision(
                    artifact_id="a", content={1: "x", "schema_version": 1,
                                              "document_type": "d", "payload": {}},
                    origin_locator="synthetic",
                    rights=RightsRef(store.project_id, "RIGHTS-rev-1", 1),
                )

    def test_provenance_nonstring_key_typed_error(self) -> None:
        with self.new_store() as store:
            repo = self.repo_for(store)
            repo.create_artifact("a", kind="document")
            with self.assertRaises(RecordValidationError):
                repo.import_revision(
                    artifact_id="a", revision_id="r1", content=b"x",
                    origin_locator="synthetic",
                    rights=_valid_rights(store.project_id, "a", "r1"),
                    provenance={1: "x", "method": "import", "actor": "a",
                                "performed_at": "2026-10-27T00:00:00Z", "origin": "s"},
                )

    def test_dependencies_integer_typed_error(self) -> None:
        with self.new_store() as store:
            repo = self.repo_for(store)
            repo.create_artifact("a", kind="document")
            with self.assertRaises(RecordValidationError):
                repo.import_revision(
                    artifact_id="a", revision_id="r1", content=b"x",
                    origin_locator="synthetic",
                    rights=_valid_rights(store.project_id, "a", "r1"),
                    dependencies=5,
                )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
