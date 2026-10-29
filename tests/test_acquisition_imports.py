"""Acceptance tests for durable source import + exact-byte duplicate occurrences.

Synthetic data only (no secrets, no licensed source text). These tests assert
storage/identity/rights behavior over a real SQLite store; they make no
scientific, legal, or approval claim.

Run with::

    PYTHONPATH=src python3 -m unittest discover -s tests -p test_acquisition_imports.py
"""

from __future__ import annotations

import copy
import sys
import tempfile
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from review_article_agent.acquisition.imports import (  # noqa: E402
    MAX_SOURCE_BYTES,
    AssetImportConflictError,
    AssetImportError,
    AssetImportRepository,
)
from review_article_agent.storage import (  # noqa: E402
    ArtifactRepository,
    ProjectStore,
    Registry,
    paths,
)
from review_article_agent.storage.backup import create_backup, restore_backup  # noqa: E402

_OPS = ("acquisition", "storage", "redistribution", "hosted_transfer")


def rights(project_id, artifact_id, revision_id, rights_id=None, **overrides):
    """A synthetic valid RightsRecord v1 bound to one source/report revision."""
    ops = overrides.pop("ops", {})
    record = {
        "rights_id": rights_id or f"RIGHTS-{revision_id}",
        "project_id": project_id,
        "record_revision": overrides.pop("record_revision", 1),
        "subject": {
            "kind": "source_revision",
            "project_id": project_id,
            "artifact_id": artifact_id,
            "revision_id": revision_id,
        },
        "operations": {op: "allowed" for op in _OPS} | ops,
        "basis": {},
        "authorization": None,
        "restrictions": overrides.pop("restrictions", []),
        "privacy": overrides.pop(
            "privacy", {"personal_data": False, "confidentiality": "public", "denial": False}
        ),
        "recorded_by": "user",
        "recorded_at": "2026-10-27T00:00:00Z",
        "derived_status": "valid",
    }
    for op in _OPS:
        status = record["operations"][op]
        if status == "allowed":
            record["basis"][op] = {
                "kind": "public_domain",
                "evidence": {"ref": "lic:pd-syn", "provenance": "publisher:pd-list"},
            }
        else:
            record["basis"][op] = {"kind": "unknown", "evidence": {"ref": "", "provenance": ""}}
    record.update(overrides)
    return record


def missing_rights(project_id, artifact_id, rights_id="RIGHTS-missing"):
    record = rights(project_id, artifact_id, "x", rights_id=rights_id,
                    ops={op: "unknown" for op in _OPS})
    record["subject"] = {
        "kind": "subject_artifact_ref", "project_id": project_id, "artifact_id": artifact_id,
    }
    return record


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="raa-imports-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.root = self.tmp / "p"
        self.store = ProjectStore.create_at(
            paths.app_dir_for(str(self.root)), paths.new_project_id()
        )
        self.addCleanup(self.store.close)
        self.pid = self.store.project_id
        self.artifacts = ArtifactRepository(self.store)
        self.imports = AssetImportRepository(self.artifacts)
        self.n = 0

    def reopen(self) -> None:
        self.store.close()
        self.store = ProjectStore.open_at(
            paths.app_dir_for(str(self.root)), expected_project_id=self.pid
        )
        self.addCleanup(self.store.close)
        self.artifacts = ArtifactRepository(self.store)
        self.imports = AssetImportRepository(self.artifacts)

    def occ_args(self, request_id):
        self.n += 1
        report = self.imports.occurrence_artifact_id(request_id)
        rev = f"occ-rev-{self.n}"
        return {
            "request_id": request_id,
            "occurrence_revision_id": rev,
            "occurrence_rights": rights(self.pid, report, rev, rights_id=f"OCC-{request_id}"),
        }

    def do_import(self, request_id, content=b"%PDF synthetic one", *, artifact="doc-a",
                  revision="src-1", origin="file:///a/one.pdf", src_rights=None, **kw):
        return self.imports.import_asset(
            artifact_id=artifact,
            content=content,
            origin_locator=origin,
            rights=src_rights or rights(self.pid, artifact, revision, rights_id=f"R-{revision}"),
            revision_id=revision,
            **self.occ_args(request_id),
            **kw,
        )

    def count(self, table):
        return self.store.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


class NewAndDuplicateTest(Base):
    def test_new_source_and_exact_duplicate_at_other_path(self) -> None:
        first = self.do_import("req-1")
        self.assertEqual(first["disposition"], "created_new_revision")
        self.assertEqual(first["byte_size"], len(b"%PDF synthetic one"))
        self.assertTrue(first["sha256"].startswith("sha256:"))
        self.assertEqual(first["source_ref"]["revision_id"], "src-1")

        dup = self.do_import(
            "req-2", origin="file:///elsewhere/copy.pdf", revision="src-2",
            src_rights=rights(self.pid, "doc-a", "src-2", rights_id="R-src-2"),
        )
        self.assertEqual(dup["disposition"], "duplicate_bytes_of_existing_revision")
        self.assertEqual(dup["source_ref"], first["source_ref"])
        self.assertEqual(len(self.artifacts.list_revisions("doc-a")), 1)

        occ = self.imports.get_occurrence(dup["occurrence_ref"])
        self.assertEqual(occ.origin_locator, "file:///elsewhere/copy.pdf")
        # identical terms bound to the never-created revision are not persisted;
        # the occurrence pins the existing source's rights record instead.
        self.assertEqual(occ.source_rights_ref.rights_id, "R-src-1")
        self.assertEqual(self.count("rights_record"), 3)  # src-1, 2 occurrence records
        # rights bound to the exact existing source are stored and pinned
        bound = rights(self.pid, "doc-a", "src-1", rights_id="R-src-1", record_revision=2)
        third = self.do_import("req-3", revision="src-3", src_rights=bound)
        occ3 = self.imports.get_occurrence(third["occurrence_ref"])
        self.assertEqual(occ3.source_rights_ref.record_revision, 2)
        self.assertEqual(third["source_ref"], first["source_ref"])
        self.assertTrue(occ.proposal_only)
        self.assertFalse(occ.authorizes_use)
        original = self.imports.get_occurrence(first["occurrence_ref"])
        self.assertEqual(original.origin_locator, "file:///a/one.pdf")
        self.assertNotEqual(original.occurrence_ref, occ.occurrence_ref)

    def test_changed_rights_cannot_authorize_reuse_and_roll_back(self) -> None:
        self.do_import("req-1")
        revs, arts, rrs = (self.count(t) for t in ("revision", "artifact", "rights_record"))
        changed = rights(self.pid, "doc-a", "src-2", rights_id="R-src-2",
                         restrictions=["new-term"])
        with self.assertRaises(AssetImportConflictError) as caught:
            self.do_import("req-2", revision="src-2", src_rights=changed)
        self.assertEqual(caught.exception.code, "rights_conflict")
        self.assertEqual((self.count("revision"), self.count("artifact"), self.count("rights_record")),
                         (revs, arts, rrs))
        self.assertIsNone(self.imports.find_request("req-2"))

    def test_duplicate_blocked_when_latest_existing_rights_deny(self) -> None:
        self.do_import("req-1")
        denied = rights(self.pid, "doc-a", "src-1", rights_id="R-src-1", record_revision=2,
                        privacy={"personal_data": False, "confidentiality": "public", "denial": True})
        self.artifacts.store_rights_record(denied)
        with self.assertRaises(AssetImportConflictError):
            self.do_import("req-2", revision="src-2",
                           src_rights=rights(self.pid, "doc-a", "src-2", rights_id="R-src-2"))
        self.assertEqual(len(self.artifacts.list_revisions("doc-a")), 1)

    def test_same_bytes_in_different_artifacts_not_merged(self) -> None:
        a = self.do_import("req-1", artifact="doc-a", revision="a-1")
        b = self.do_import("req-2", artifact="doc-b", revision="b-1")
        self.assertEqual(a["disposition"], "created_new_revision")
        self.assertEqual(b["disposition"], "created_new_revision")
        self.assertEqual(a["sha256"], b["sha256"])
        self.assertNotEqual(a["source_ref"], b["source_ref"])

    def test_alternate_bytes_are_versions_with_parent(self) -> None:
        self.do_import("req-1")
        v2 = self.do_import(
            "req-2", content=b"%PDF synthetic two", revision="src-2",
            src_rights=rights(self.pid, "doc-a", "src-2", rights_id="R-src-2"),
            parent_revision_id="src-1",
        )
        self.assertEqual(v2["disposition"], "created_new_revision")
        revision = self.artifacts.get_revision("doc-a", "src-2")
        self.assertEqual(revision.parent_revision_id, "src-1")
        self.assertTrue(any(d.role == "version_of" and d.revision_id == "src-1"
                            for d in revision.dependencies))
        with self.assertRaises(AssetImportError):  # stale parent
            self.do_import(
                "req-3", content=b"%PDF synthetic three", revision="src-3",
                src_rights=rights(self.pid, "doc-a", "src-3", rights_id="R-src-3"),
                parent_revision_id="src-1",
            )
        # old bytes again -> historical revision reused despite newer head
        again = self.do_import(
            "req-4", revision="src-4",
            src_rights=rights(self.pid, "doc-a", "src-4", rights_id="R-src-4"),
        )
        self.assertEqual(again["disposition"], "duplicate_bytes_of_existing_revision")
        self.assertEqual(again["source_ref"]["revision_id"], "src-1")

    def test_explicit_study_link_is_retained_and_not_silently_dropped(self) -> None:
        self.artifacts.create_artifact("study-1", kind="study")
        study_rights = rights(self.pid, "study-1", "st-1", rights_id="R-st")
        self.artifacts.import_revision(
            artifact_id="study-1", content=b"study metadata", origin_locator="note:study",
            rights=study_rights, revision_id="st-1",
        )
        link = [{"project_id": self.pid, "artifact_id": "study-1",
                 "revision_id": "st-1", "role": "reports"}]
        first = self.do_import("req-1", dependencies=link)
        deps = self.artifacts.list_dependencies("doc-a", "src-1")
        self.assertEqual([(d.artifact_id, d.role) for d in deps], [("study-1", "reports")])
        occ = self.imports.get_occurrence(first["occurrence_ref"])
        self.assertEqual([(d.artifact_id, d.role) for d in occ.dependencies],
                         [("study-1", "reports")])
        # duplicate declaring no link must not silently drop the existing relationship
        with self.assertRaises(AssetImportConflictError) as caught:
            self.do_import("req-2", revision="src-2",
                           src_rights=rights(self.pid, "doc-a", "src-2", rights_id="R-src-2"))
        self.assertEqual(caught.exception.code, "dependency_conflict")
        # an unrelated title/artifact is never auto-linked
        other = self.do_import("req-3", content=b"other doc", artifact="doc-z", revision="z-1")
        self.assertEqual(self.artifacts.list_dependencies("doc-z", "z-1"), [])
        self.assertNotEqual(other["source_ref"], first["source_ref"])


class IdempotencyTest(Base):
    def test_exact_replay_returns_history_even_after_head_change_and_restart(self) -> None:
        first = self.do_import("req-1")
        # head changes
        self.do_import(
            "req-2", content=b"%PDF synthetic two", revision="src-2",
            src_rights=rights(self.pid, "doc-a", "src-2", rights_id="R-src-2"),
            parent_revision_id="src-1",
        )
        n = self.count("revision")
        args = dict(
            artifact_id="doc-a", content=b"%PDF synthetic one", origin_locator="file:///a/one.pdf",
            rights=rights(self.pid, "doc-a", "src-1", rights_id="R-src-1"), revision_id="src-1",
            request_id="req-1",
            occurrence_revision_id=self.imports.get_occurrence(first["occurrence_ref"])
            .occurrence_ref.revision_id,
            occurrence_rights=self.artifacts.get_rights_record(
                self.imports.get_occurrence(first["occurrence_ref"]).occurrence_rights_ref
            ),
        )
        replay = self.imports.import_asset(**args)
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["source_ref"], first["source_ref"])
        self.assertEqual(replay["occurrence_ref"], first["occurrence_ref"])
        self.assertEqual(self.count("revision"), n)
        self.reopen()
        replay2 = self.imports.import_asset(**args)
        self.assertEqual(replay2["occurrence_ref"], first["occurrence_ref"])
        self.assertEqual(self.imports.find_request("req-1").occurrence_ref.as_dict(),
                         first["occurrence_ref"])

    def test_divergent_replay_conflicts(self) -> None:
        first = self.do_import("req-1")
        occ = self.imports.get_occurrence(first["occurrence_ref"])
        base = dict(
            artifact_id="doc-a", content=b"%PDF synthetic one", origin_locator="file:///a/one.pdf",
            rights=rights(self.pid, "doc-a", "src-1", rights_id="R-src-1"), revision_id="src-1",
            request_id="req-1", occurrence_revision_id=occ.occurrence_ref.revision_id,
            occurrence_rights=self.artifacts.get_rights_record(occ.occurrence_rights_ref),
        )
        for change in (
            {"content": b"%PDF different"},
            {"origin_locator": "file:///a/other.pdf"},
            {"artifact_id": "doc-b"},
        ):
            with self.assertRaises(AssetImportConflictError) as caught:
                self.imports.import_asset(**(base | change))
            self.assertEqual(caught.exception.code, "request_conflict")
        with self.assertRaises(AssetImportConflictError):
            self.imports.record_missing(
                artifact_id="doc-a", origin_locator="file:///a/one.pdf",
                rights=missing_rights(self.pid, "doc-a"), request_id="req-1",
                occurrence_revision_id=occ.occurrence_ref.revision_id,
                occurrence_rights=base["occurrence_rights"], reason="gone",
            )


class RightsGateTest(Base):
    def assert_nothing_retained(self):
        for table in ("artifact", "revision", "rights_record"):
            self.assertEqual(self.count(table), 0, table)

    def test_unknown_storage_retains_no_bytes(self) -> None:
        bad = rights(self.pid, "doc-a", "src-1", ops={"storage": "unknown"})
        with self.assertRaises(AssetImportError) as caught:
            self.do_import("req-1", src_rights=bad)
        self.assertEqual(caught.exception.code, "storage_not_allowed")
        self.assert_nothing_retained()

    def test_acquisition_and_storage_are_separate_gates(self) -> None:
        bad = rights(self.pid, "doc-a", "src-1", ops={"acquisition": "not_allowed"})
        with self.assertRaises(AssetImportError) as caught:
            self.do_import("req-1", src_rights=bad)
        self.assertEqual(caught.exception.code, "acquisition_not_allowed")
        self.assert_nothing_retained()

    def test_privacy_denial_blocks(self) -> None:
        bad = rights(self.pid, "doc-a", "src-1",
                     privacy={"personal_data": True, "confidentiality": "confidential",
                              "denial": True})
        with self.assertRaises(AssetImportError) as caught:
            self.do_import("req-1", src_rights=bad)
        self.assertEqual(caught.exception.code, "privacy_denied")
        self.assert_nothing_retained()

    def test_superseded_source_rights_rejected(self) -> None:
        self.artifacts.create_artifact("doc-a", kind="document")
        newer = rights(self.pid, "doc-a", "src-1", rights_id="R-src-1", record_revision=2)
        self.artifacts.store_rights_record(newer)
        with self.assertRaises(AssetImportError):
            self.do_import("req-1", src_rights=rights(self.pid, "doc-a", "src-1",
                                                       rights_id="R-src-1"))
        self.assertEqual(self.count("revision"), 0)

    def test_failed_occurrence_rights_roll_back_everything(self) -> None:
        report = self.imports.occurrence_artifact_id("req-1")
        newer = rights(self.pid, report, "occ-rev-x", rights_id="OCC-req-1", record_revision=2)
        self.artifacts.store_rights_record(newer)
        before = {t: self.count(t) for t in ("artifact", "revision", "rights_record", "dependency")}
        with self.assertRaises(AssetImportError) as caught:
            self.imports.import_asset(
                artifact_id="doc-a", content=b"bytes", origin_locator="file:///x",
                rights=rights(self.pid, "doc-a", "src-1", rights_id="R-src-1"),
                revision_id="src-1", request_id="req-1",
                occurrence_revision_id="occ-rev-x",
                occurrence_rights=rights(self.pid, report, "occ-rev-x", rights_id="OCC-req-1"),
            )
        self.assertEqual(caught.exception.code, "occurrence_rejected")
        after = {t: self.count(t) for t in before}
        self.assertEqual(before, after)
        self.assertIsNone(self.artifacts.get_artifact("doc-a"))
        self.assertIsNone(self.artifacts.get_artifact(report))
        self.assertIsNone(self.imports.find_request("req-1"))

    def test_errors_carry_no_peer_text(self) -> None:
        secret = "peer-secret-artifact"
        bad = rights(self.pid, secret, "src-1", ops={"storage": "unknown"})
        with self.assertRaises(AssetImportError) as caught:
            self.do_import("req-1", artifact=secret, src_rights=bad)
        self.assertNotIn(secret, str(caught.exception))
        with self.assertRaises(AssetImportError) as caught:
            self.imports.import_asset(
                artifact_id="bad/id", content=b"x", origin_locator="o", rights={},
                revision_id="r", request_id="q", occurrence_rights={},
                occurrence_revision_id="o",
            )
        self.assertNotIn("bad/id", str(caught.exception))


class BoundsTest(Base):
    def test_input_bounds(self) -> None:
        with self.assertRaises(AssetImportError) as caught:
            self.do_import("req-1", content=b"x" * (MAX_SOURCE_BYTES + 1))
        self.assertEqual(caught.exception.code, "content_too_large")
        for kwargs in (
            {"content": b""},
            {"content": "text"},
            {"origin": "x" * 5000},
            {"origin": " padded "},
        ):
            with self.assertRaises(AssetImportError) as caught:
                self.do_import("req-b", **kwargs)
            self.assertEqual(caught.exception.code, "invalid_input")
        self.assertEqual(self.count("artifact"), 0)

    def test_max_size_boundary_accepts(self) -> None:
        result = self.do_import("req-1", content=b"y" * MAX_SOURCE_BYTES)
        self.assertEqual(result["byte_size"], MAX_SOURCE_BYTES)


class MissingTest(Base):
    def test_missing_event_creates_no_source_revision(self) -> None:
        result = self.imports.record_missing(
            artifact_id="doc-m", origin_locator="https://example.invalid/paper",
            rights=missing_rights(self.pid, "doc-m"), reason="no authorized full text",
            **self.occ_args("req-m"),
        )
        self.assertEqual(result["disposition"], "no_full_text_acquired")
        self.assertIsNone(result["source_ref"])
        self.assertIsNone(result["sha256"])
        self.assertIsNone(result["byte_size"])
        self.assertEqual(self.artifacts.list_revisions("doc-m"), [])
        occ = self.imports.get_occurrence(result["occurrence_ref"])
        self.assertIsNone(occ.content_hash)
        self.assertEqual(occ.reason, "no authorized full text")
        stored = self.artifacts.get_rights_record(occ.source_rights_ref)
        self.assertEqual(stored["subject"]["kind"], "subject_artifact_ref")
        self.assertEqual(stored["operations"]["storage"], "unknown")
        # a later authorized import of that artifact still works, no pretend bytes before
        later = self.do_import("req-l", artifact="doc-m", revision="m-1")
        self.assertEqual(later["disposition"], "created_new_revision")

    def test_missing_rejects_wrong_subject_and_is_idempotent(self) -> None:
        args = dict(
            artifact_id="doc-m", origin_locator="https://example.invalid/p",
            reason="r", **self.occ_args("req-m"),
        )
        with self.assertRaises(AssetImportError):
            self.imports.record_missing(
                rights=rights(self.pid, "doc-m", "x"), **args
            )
        with self.assertRaises(AssetImportError):
            self.imports.record_missing(rights=missing_rights(self.pid, "other"), **args)
        first = self.imports.record_missing(rights=missing_rights(self.pid, "doc-m"), **args)
        again = self.imports.record_missing(rights=missing_rights(self.pid, "doc-m"), **args)
        self.assertEqual(first["occurrence_ref"], again["occurrence_ref"])
        self.assertTrue(again["replayed"])


class ReadValidationTest(Base):
    def test_get_occurrence_validates_ref(self) -> None:
        first = self.do_import("req-1")
        ref = first["occurrence_ref"]
        self.assertEqual(self.imports.get_occurrence(ref).source_ref.as_dict(), first["source_ref"])
        with self.assertRaises(AssetImportError):
            self.imports.get_occurrence(ref | {"revision_id": "nope"})
        with self.assertRaises(AssetImportError):
            self.imports.get_occurrence(ref | {"project_id": "other-project"})
        with self.assertRaises(AssetImportError):  # a source revision is not an occurrence
            self.imports.get_occurrence(first["source_ref"])
        self.assertIsNone(self.imports.find_request("never-used"))

    def test_occurrence_detects_source_tampering(self) -> None:
        first = self.do_import("req-1")
        conn = self.store.connection
        for trigger in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger'"
        ).fetchall():
            conn.execute(f"DROP TRIGGER {trigger['name']}")
        conn.execute("UPDATE revision SET content = ? WHERE artifact_id = 'doc-a'", (b"tampered",))
        with self.assertRaises(AssetImportError) as caught:
            self.imports.get_occurrence(first["occurrence_ref"])
        self.assertEqual(caught.exception.code, "occurrence_invalid")


class LatestRightsBindingTest(Base):
    def snapshot(self):
        return {t: self.count(t) for t in ("artifact", "revision", "rights_record", "dependency")}

    def dup(self, request_id="req-2"):
        return self.do_import(
            request_id, revision="src-2",
            src_rights=rights(self.pid, "doc-a", "src-2", rights_id="fresh"),
        )

    def test_latest_record_binding_another_source_cannot_authorize_reuse(self) -> None:
        self.do_import("req-1")
        self.do_import("other", artifact="doc-b", revision="b1", content=b"other")
        # same rights_id chain, newer revision, but it now binds a DIFFERENT source
        self.artifacts.store_rights_record(
            rights(self.pid, "doc-b", "b1", rights_id="R-src-1", record_revision=2)
        )
        before = self.snapshot()
        with self.assertRaises(AssetImportConflictError) as caught:
            self.dup()
        self.assertEqual(caught.exception.code, "rights_conflict")
        self.assertEqual(self.snapshot(), before)
        self.assertIsNone(self.imports.find_request("req-2"))

    def test_latest_same_source_allowed_revision_is_pinned_not_stale_original(self) -> None:
        first = self.do_import("req-1")
        self.artifacts.store_rights_record(
            rights(self.pid, "doc-a", "src-1", rights_id="R-src-1", record_revision=2)
        )
        result = self.dup()
        self.assertEqual(result["disposition"], "duplicate_bytes_of_existing_revision")
        self.assertEqual(result["source_ref"], first["source_ref"])
        occ = self.imports.get_occurrence(result["occurrence_ref"])
        self.assertEqual(occ.source_rights_ref.record_revision, 2)

    def test_latest_same_source_revision_that_denies_or_unknowns_fails_closed(self) -> None:
        self.do_import("req-1")
        cases = (
            {"privacy": {"personal_data": False, "confidentiality": "public", "denial": True}},
            {"ops": {"storage": "unknown"}},
            {"ops": {"acquisition": "not_allowed"}},
        )
        for offset, change in enumerate(cases, start=2):
            self.artifacts.store_rights_record(
                rights(self.pid, "doc-a", "src-1", rights_id="R-src-1",
                       record_revision=offset, **change)
            )
            before = self.snapshot()
            with self.assertRaises(AssetImportConflictError) as caught:
                self.dup(f"req-{offset}")
            self.assertEqual(caught.exception.code, "rights_conflict")
            self.assertEqual(self.snapshot(), before)
        self.assertEqual(len(self.artifacts.list_revisions("doc-a")), 1)

    def test_replay_stays_read_only_after_latest_rights_change(self) -> None:
        self.do_import("req-1")
        dup = self.dup()
        self.artifacts.store_rights_record(
            rights(self.pid, "doc-a", "src-1", rights_id="R-src-1", record_revision=2,
                   privacy={"personal_data": False, "confidentiality": "public",
                            "denial": True})
        )
        occ = self.imports.get_occurrence(dup["occurrence_ref"])
        before = self.snapshot()
        replay = self.imports.import_asset(
            artifact_id="doc-a", content=b"%PDF synthetic one",
            origin_locator="file:///a/one.pdf",
            rights=rights(self.pid, "doc-a", "src-2", rights_id="fresh"),
            revision_id="src-2", request_id="req-2",
            occurrence_revision_id=occ.occurrence_ref.revision_id,
            occurrence_rights=self.artifacts.get_rights_record(occ.occurrence_rights_ref),
        )
        self.assertTrue(replay["replayed"])
        self.assertEqual(self.snapshot(), before)


class ForgedRecordTest(Base):
    def forge(self, occurrence_ref, transform):
        """Rewrite stored occurrence content (hash/size kept consistent)."""
        import json

        from review_article_agent.domain.records import canonical_json
        from review_article_agent.storage.artifacts import sha256_digest

        conn = self.store.connection
        for trigger in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger'").fetchall():
            conn.execute(f"DROP TRIGGER {trigger['name']}")
        artifact, revision = occurrence_ref["artifact_id"], occurrence_ref["revision_id"]
        row = conn.execute(
            "SELECT content FROM revision WHERE artifact_id = ? AND revision_id = ?",
            (artifact, revision),
        ).fetchone()
        raw = transform(bytes(row["content"]).decode("utf-8"), json, canonical_json)
        data = raw.encode("utf-8")
        conn.execute(
            "UPDATE revision SET content = ?, content_sha256 = ?, byte_size = ? "
            "WHERE artifact_id = ? AND revision_id = ?",
            (data, sha256_digest(data), len(data), artifact, revision),
        )

    def assert_invalid(self, ref):
        with self.assertRaises(AssetImportError) as caught:
            self.imports.get_occurrence(ref)
        self.assertEqual(caught.exception.code, "occurrence_invalid")

    def test_bool_versions_rejected(self) -> None:
        first = self.do_import("req-1")
        self.forge(first["occurrence_ref"],
                   lambda t, j, c: t.replace('"schema_version":1', '"schema_version":true'))
        self.assert_invalid(first["occurrence_ref"])

    def test_bool_record_version_rejected(self) -> None:
        first = self.do_import("req-1")
        self.forge(first["occurrence_ref"],
                   lambda t, j, c: t.replace('"record_version":1', '"record_version":true'))
        self.assert_invalid(first["occurrence_ref"])

    def test_noncanonical_and_duplicate_key_content_rejected(self) -> None:
        first = self.do_import("req-1")
        second = self.do_import("req-2", artifact="doc-b", revision="b1", content=b"other")
        self.forge(first["occurrence_ref"],
                   lambda t, j, c: j.dumps(j.loads(t), indent=2))  # same data, noncanonical
        self.assert_invalid(first["occurrence_ref"])
        self.forge(second["occurrence_ref"],
                   lambda t, j, c: t[:-1] + ',"payload":{}}')  # duplicate key
        self.assert_invalid(second["occurrence_ref"])

    def test_pinned_rights_naming_another_source_rejected_on_read(self) -> None:
        self.do_import("req-1")
        self.do_import("other", artifact="doc-b", revision="b1", content=b"other")
        dup = self.do_import(
            "req-2", revision="src-2",
            src_rights=rights(self.pid, "doc-a", "src-2", rights_id="fresh"),
        )
        self.assertEqual(self.imports.get_occurrence(dup["occurrence_ref"]).disposition,
                         "duplicate_bytes_of_existing_revision")

        def point_at_other(text, j, c):
            doc = j.loads(text)
            doc["payload"]["source_rights_ref"] = {
                "project_id": self.pid, "rights_id": "R-b1", "record_revision": 1,
            }
            return c(doc).decode("utf-8")

        self.forge(dup["occurrence_ref"], point_at_other)
        self.assert_invalid(dup["occurrence_ref"])


class BackupRestartTest(Base):
    def test_backup_and_restart_preserve_occurrences(self) -> None:
        first = self.do_import("req-1")
        dup = self.do_import(
            "req-2", revision="src-2", origin="file:///b.pdf",
            src_rights=rights(self.pid, "doc-a", "src-2", rights_id="R-src-2"),
        )
        missing = self.imports.record_missing(
            artifact_id="doc-m", origin_locator="https://example.invalid/m",
            rights=missing_rights(self.pid, "doc-m"), reason="unavailable",
            **self.occ_args("req-m"),
        )
        dest = self.tmp / "backup"
        create_backup(self.store, dest)
        registry = Registry(self.tmp / "state")
        target = self.tmp / "restored"
        target.mkdir()
        info = restore_backup(dest, target, registry)
        restored = ProjectStore.open_at(
            paths.app_dir_for(str(target)), expected_project_id=info.project_id
        )
        self.addCleanup(restored.close)
        repo = AssetImportRepository(restored)
        for original in (first, dup, missing):
            occ = repo.get_occurrence(original["occurrence_ref"])
            self.assertEqual(occ.disposition, original["disposition"])
            self.assertEqual(occ.content_hash, original["sha256"])
            self.assertEqual(
                None if occ.source_ref is None else occ.source_ref.as_dict(),
                original["source_ref"],
            )
        self.assertEqual(len(ArtifactRepository(restored).list_revisions("doc-a")), 1)
        self.reopen()
        self.assertEqual(
            self.imports.find_request("req-2").disposition,
            "duplicate_bytes_of_existing_revision",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
