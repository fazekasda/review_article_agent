"""Tests for atomic explicit folder-batch import (real filesystem + SQLite).

Synthetic data only. Run with::

    PYTHONPATH=src python3 -m unittest discover -s tests -p test_acquisition_batches.py
"""

from __future__ import annotations

import copy
import os
import sys
import tempfile
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_acquisition_imports import rights  # noqa: E402

from review_article_agent.acquisition.batches import (  # noqa: E402
    BatchImportConflictError,
    BatchImportError,
    import_folder_batch,
)
from review_article_agent.acquisition.imports import AssetImportRepository  # noqa: E402
from review_article_agent.storage import ArtifactRepository, ProjectStore, paths  # noqa: E402


class BatchBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="raa-batches-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(os.path.realpath(self._tmp.name))
        self.proot = self.tmp / "p"
        self.store = ProjectStore.create_at(
            paths.app_dir_for(str(self.proot)), paths.new_project_id()
        )
        self.addCleanup(self._close)
        self.pid = self.store.project_id
        self.folder = self.tmp / "folder"
        self.folder.mkdir()
        self.importer = AssetImportRepository(self.store)

    def _close(self) -> None:
        try:
            self.store.close()
        except Exception:
            pass

    def reopen(self) -> None:
        self.store.close()
        self.store = ProjectStore.open_at(
            paths.app_dir_for(str(self.proot)), expected_project_id=self.pid
        )
        self.importer = AssetImportRepository(self.store)

    def write(self, name: str, data: bytes) -> None:
        path = self.folder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def entry(self, name, req, artifact="doc-a", revision=None, **extra):
        revision = revision or f"src-{req}"
        occ_rev = f"occ-{req}"
        report = self.importer.occurrence_artifact_id(req)
        item = {
            "relative_path": name,
            "artifact_id": artifact,
            "origin_locator": f"file:{name}",
            "rights": rights(self.pid, artifact, revision, rights_id=f"R-{revision}"),
            "revision_id": revision,
            "request_id": req,
            "occurrence_rights": rights(self.pid, report, occ_rev, rights_id=f"OCC-{req}"),
            "occurrence_revision_id": occ_rev,
        }
        item.update(extra)
        return item

    def count(self, table: str) -> int:
        return self.store.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    def snapshot(self):
        return tuple(self.count(t) for t in ("artifact", "revision", "rights_record"))

    def run_batch(self, entries, **kw):
        return import_folder_batch(self.store, self.folder, entries, **kw)


class BatchImportTest(BatchBase):
    def test_two_items_succeed_and_replay_after_restart(self) -> None:
        self.write("a.pdf", b"%PDF one")
        self.write("sub/b.txt", b"second doc")
        entries = [
            self.entry("a.pdf", "r1", artifact="doc-a"),
            self.entry("sub/b.txt", "r2", artifact="doc-b"),
        ]
        first = self.run_batch(entries)
        self.assertFalse(first["authorizes_scientific_processing"])
        self.assertTrue(first["proposal_only"])
        self.assertEqual(first["counts"]["entries"], 2)
        self.assertEqual(first["counts"]["replayed"], 0)
        self.assertEqual(first["counts"]["dispositions"], {"created_new_revision": 2})
        self.assertNotIn("a.pdf", repr(first))
        before = self.snapshot()

        self.reopen()
        second = self.run_batch(copy.deepcopy(entries))
        self.assertEqual(second["counts"]["replayed"], 2)
        self.assertEqual(self.snapshot(), before)
        for a, b in zip(first["items"], second["items"]):
            self.assertEqual(a["source_ref"], b["source_ref"])
            self.assertEqual(a["occurrence_ref"], b["occurrence_ref"])
            self.assertEqual(a["disposition"], b["disposition"])

    def test_different_path_identical_bytes_reuses_source(self) -> None:
        self.write("one.pdf", b"same bytes")
        self.write("copy/two.pdf", b"same bytes")
        result = self.run_batch(
            [self.entry("one.pdf", "r1"), self.entry("copy/two.pdf", "r2")]
        )
        a, b = result["items"]
        self.assertEqual(a["source_ref"], b["source_ref"])
        self.assertNotEqual(a["occurrence_ref"], b["occurrence_ref"])
        self.assertEqual(b["disposition"], "duplicate_bytes_of_existing_revision")
        repo = ArtifactRepository(self.store)
        self.assertEqual(len(repo.list_revisions("doc-a")), 1)

    def test_distinct_versions_need_explicit_parent(self) -> None:
        self.write("v1.pdf", b"version one")
        self.write("v2.pdf", b"version two")
        with self.assertRaises(BatchImportError) as ctx:
            self.run_batch([self.entry("v1.pdf", "r1"), self.entry("v2.pdf", "r2")])
        self.assertEqual(ctx.exception.code, "version_requires_parent")
        self.assertEqual(self.count("artifact"), 0)

        result = self.run_batch(
            [
                self.entry("v1.pdf", "r1", revision="src-1"),
                self.entry("v2.pdf", "r2", revision="src-2", parent_revision_id="src-1"),
            ]
        )
        self.assertEqual(
            [i["source_ref"]["revision_id"] for i in result["items"]], ["src-1", "src-2"]
        )
        revs = ArtifactRepository(self.store).list_revisions("doc-a")
        self.assertEqual(len(revs), 2)
        self.assertEqual(revs[1].parent_revision_id, "src-1")

    def test_second_item_rights_failure_rolls_back_first(self) -> None:
        self.write("a.pdf", b"first")
        self.write("b.pdf", b"second")
        bad = self.entry("b.pdf", "r2", artifact="doc-b")
        bad["rights"] = rights(self.pid, "doc-b", "src-r2", rights_id="R-bad",
                               ops={"storage": "unknown"})
        with self.assertRaises(BatchImportError) as ctx:
            self.run_batch([self.entry("a.pdf", "r1"), bad])
        self.assertEqual(ctx.exception.code, "import_failed")
        self.assertEqual(ctx.exception.index, 1)
        self.assertEqual(ctx.exception.cause_code, "storage_not_allowed")
        self.assertEqual(self.snapshot(), (0, 0, 0))

    def test_bad_occurrence_rights_rolls_back_all(self) -> None:
        self.write("a.pdf", b"first")
        self.write("b.pdf", b"second")
        bad = self.entry("b.pdf", "r2", artifact="doc-b")
        bad["occurrence_rights"]["subject"]["revision_id"] = "wrong"
        with self.assertRaises(BatchImportError):
            self.run_batch([self.entry("a.pdf", "r1"), bad])
        self.assertEqual(self.snapshot(), (0, 0, 0))

    def test_missing_file_and_symlink_fail_before_any_write(self) -> None:
        self.write("a.pdf", b"first")
        with self.assertRaises(BatchImportError) as ctx:
            self.run_batch([self.entry("a.pdf", "r1"), self.entry("nope.pdf", "r2", artifact="doc-b")])
        self.assertEqual(ctx.exception.code, "folder_input")
        self.assertEqual(ctx.exception.cause_code, "not_found")
        self.assertNotIn("nope", str(ctx.exception))
        self.assertEqual(self.snapshot(), (0, 0, 0))

        outside = self.tmp / "outside.pdf"
        outside.write_bytes(b"outside")
        os.symlink(outside, self.folder / "link.pdf")
        with self.assertRaises(BatchImportError) as ctx:
            self.run_batch([self.entry("a.pdf", "r1"), self.entry("link.pdf", "r2", artifact="doc-b")])
        self.assertEqual(ctx.exception.code, "folder_input")
        self.assertEqual(self.snapshot(), (0, 0, 0))

    def test_changed_source_under_same_request_conflicts_atomically(self) -> None:
        self.write("a.pdf", b"original")
        self.write("b.pdf", b"other doc")
        entries = [
            self.entry("a.pdf", "r1"),
            self.entry("b.pdf", "r2", artifact="doc-b"),
        ]
        self.run_batch(entries)
        before = self.snapshot()
        self.write("b.pdf", b"changed bytes")
        with self.assertRaises(BatchImportConflictError) as ctx:
            self.run_batch(copy.deepcopy(entries))
        self.assertEqual(ctx.exception.cause_code, "request_conflict")
        self.assertEqual(ctx.exception.index, 1)
        self.assertEqual(self.snapshot(), before)

    def test_stale_parent_rolls_back_batch(self) -> None:
        self.write("a.pdf", b"first")
        self.write("b.pdf", b"second")
        with self.assertRaises(BatchImportError):
            self.run_batch(
                [
                    self.entry("a.pdf", "r1", artifact="doc-a"),
                    self.entry("b.pdf", "r2", artifact="doc-b", parent_revision_id="missing-rev"),
                ]
            )
        self.assertEqual(self.snapshot(), (0, 0, 0))

    def test_study_relationship_retained_without_title_merge_or_approval(self) -> None:
        repo = ArtifactRepository(self.store)
        repo.create_artifact("study-1", kind="study")
        self.write("paper-final.pdf", b"paper text")
        self.write("paper-final-v2.pdf", b"other paper text")
        link = [{"project_id": self.pid, "artifact_id": "study-1", "revision_id": None,
                 "role": "reports"}]
        # revision_id must be explicit; a study without a revision cannot be linked.
        with self.assertRaises(BatchImportError):
            self.run_batch([self.entry("paper-final.pdf", "r1", dependencies=link)])
        self.assertEqual(self.count("revision"), 0)

        study_rights = rights(self.pid, "study-1", "st-1", rights_id="R-st")
        repo.import_revision(
            artifact_id="study-1", content=b"study meta", origin_locator="note:study",
            rights=study_rights, revision_id="st-1",
        )
        link[0]["revision_id"] = "st-1"
        result = self.run_batch(
            [
                self.entry("paper-final.pdf", "r1", artifact="doc-a", dependencies=link),
                self.entry("paper-final-v2.pdf", "r2", artifact="doc-b"),
            ]
        )
        deps = repo.get_revision("doc-a", "src-r1").dependencies
        self.assertEqual([(d.artifact_id, d.role) for d in deps], [("study-1", "reports")])
        self.assertEqual(repo.get_revision("doc-b", "src-r2").dependencies, ())
        self.assertEqual(len(repo.list_revisions("doc-a")), 1)
        self.assertEqual(len(repo.list_revisions("doc-b")), 1)
        self.assertFalse(result["authorizes_scientific_processing"])
        tables = {
            r[0] for r in self.store.connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        for name in tables:
            self.assertFalse("approval" in name and self.count(name), name)

    def test_shape_validation_precedes_reads(self) -> None:
        self.write("a.pdf", b"first")
        good = self.entry("a.pdf", "r1")
        cases = [
            "not-a-list",
            [dict(good, extra=1)],
            [{k: v for k, v in good.items() if k != "rights"}],
            [dict(good, content=b"x")],
            [dict(good, rights="text")],
            [good, dict(self.entry("a.pdf", "r1"), relative_path="b.pdf")],
            [dict(good, dependencies="x")],
        ]
        for entries in cases:
            with self.assertRaises(BatchImportError):
                self.run_batch(entries)
        self.assertEqual(self.snapshot(), (0, 0, 0))

    def test_limits_enforced(self) -> None:
        self.write("a.pdf", b"12345678")
        with self.assertRaises(BatchImportError) as ctx:
            self.run_batch([self.entry("a.pdf", "r1")], max_total_bytes=4)
        self.assertEqual(ctx.exception.cause_code, "total_too_large")
        self.assertEqual(self.snapshot(), (0, 0, 0))


if __name__ == "__main__":
    unittest.main()
