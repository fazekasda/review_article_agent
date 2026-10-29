"""In-process CLI tests for ``acquisition folder import`` (Step 06, C3).

Drives the real entry point (``cli.main.main``) over registry-created project
stores and the integrated atomic batch importer. Synthetic data only.

Run::

    PYTHONPATH=src python3 -m unittest tests.test_cli_acquisition_folders -v
"""

from __future__ import annotations

import copy
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
TESTS = Path(__file__).resolve().parent
if str(TESTS) not in sys.path:
    sys.path.insert(0, str(TESTS))

from test_acquisition_imports import rights  # noqa: E402

from review_article_agent.acquisition.imports import AssetImportRepository  # noqa: E402
from review_article_agent.cli import main as cli_main  # noqa: E402
from review_article_agent.storage import ProjectStore, Registry, paths  # noqa: E402

_OK = cli_main.EXIT_SUCCESS
_VALID = cli_main.EXIT_VALIDATION
_CONFLICT = cli_main.EXIT_CONFLICT
_TABLES = ("artifact", "revision", "rights_record", "human_approval")


class FolderCliBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="raa-cli-folder-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(os.path.realpath(self._tmp.name))
        self.state = self.tmp / "state"
        self.project_id, self.proot = self._create("proj")
        self.folder = self.tmp / "folder"
        self.folder.mkdir()

    def _run(self, *args: str):
        out, err = io.StringIO(), io.StringIO()
        code = cli_main.main(
            ["--data-dir", str(self.state), *args], env={}, stdout=out, stderr=err
        )
        return code, out.getvalue(), err.getvalue()

    def _create(self, name: str):
        code, out, err = self._run("create", str(self.tmp / name), "--name", name)
        self.assertEqual(code, 0, err)
        pid = json.loads(out)["project"]["project_id"]
        return pid, Registry(str(self.state)).get(pid).root_path

    def _store(self, pid=None, root=None):
        return ProjectStore.open_at(
            paths.app_dir_for(root or self.proot),
            expected_project_id=pid or self.project_id,
        )

    def count(self, table, pid=None, root=None) -> int:
        store = self._store(pid, root)
        try:
            return store.connection.execute(
                f"SELECT COUNT(*) FROM {table}"
            ).fetchone()[0]
        finally:
            store.close()

    def snapshot(self, pid=None, root=None):
        tables = []
        store = self._store(pid, root)
        try:
            for (name,) in store.connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            ).fetchall():
                if name.startswith("sqlite_"):
                    continue
                tables.append(
                    (name, store.connection.execute(
                        f"SELECT COUNT(*) FROM {name}").fetchone()[0])
                )
        finally:
            store.close()
        return tuple(tables)

    def write(self, name: str, data: bytes) -> None:
        path = self.folder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def entry(self, name, req, artifact="doc-a", **extra):
        revision = f"src-{req}"
        occ_rev = f"occ-{req}"
        store = self._store()
        try:
            report = AssetImportRepository(store).occurrence_artifact_id(req)
        finally:
            store.close()
        item = {
            "relative_path": name,
            "artifact_id": artifact,
            "origin_locator": f"file:{name}",
            "rights": rights(
                self.project_id, artifact, revision, rights_id=f"R-{revision}"
            ),
            "revision_id": revision,
            "request_id": req,
            "occurrence_rights": rights(
                self.project_id, report, occ_rev, rights_id=f"OCC-{req}"
            ),
            "occurrence_revision_id": occ_rev,
        }
        item.update(extra)
        return item

    def entries_file(self, entries, name="entries.json") -> str:
        path = self.tmp / name
        path.write_text(json.dumps(entries), encoding="utf-8")
        return str(path)

    def run_import(self, entries, *extra, root=None, project=None):
        return self._run(
            "acquisition", "folder", "import", str(root or self.folder),
            "--entries-file", self.entries_file(entries),
            "--project", project or self.project_id, *extra,
        )

    def ok(self, entries, *extra, **kw) -> dict:
        code, out, err = self.run_import(entries, *extra, **kw)
        self.assertEqual(code, _OK, f"{out}\n{err}")
        return json.loads(out)

    def fail_cli(self, entries, expect, *extra, **kw) -> str:
        code, out, err = self.run_import(entries, *extra, **kw)
        self.assertEqual(code, expect, f"{out}\n{err}")
        self.assertNotIn("Traceback", err)
        self.assertEqual(out, "")
        return err


class FolderImportTest(FolderCliBase):
    def test_success_restart_replay_and_privacy(self) -> None:
        self.write("a.pdf", b"%PDF-secret-raw-bytes")
        self.write("sub/b.txt", b"second doc")
        entries = [
            self.entry("a.pdf", "r1", artifact="doc-a"),
            self.entry("sub/b.txt", "r2", artifact="doc-b"),
        ]
        first = self.ok(entries)
        self.assertTrue(first["proposal_only"])
        self.assertIs(first["authorizes_scientific_processing"], False)
        self.assertEqual(first["counts"]["entries"], 2)
        self.assertEqual(first["counts"]["replayed"], 0)
        self.assertEqual(first["counts"]["dispositions"], {"created_new_revision": 2})
        self.assertEqual(
            [i["source_ref"]["artifact_id"] for i in first["items"]],
            ["doc-a", "doc-b"],
        )
        before = self.snapshot()
        code, out, err = self.run_import(copy.deepcopy(entries))
        self.assertEqual(code, _OK, err)
        second = json.loads(out)
        self.assertEqual(second["counts"]["replayed"], 2)
        self.assertEqual(self.snapshot(), before)
        for a, b in zip(first["items"], second["items"]):
            self.assertEqual(a["source_ref"], b["source_ref"])
            self.assertEqual(a["occurrence_ref"], b["occurrence_ref"])
        # privacy: no root, paths, locators, rights ids, raw bytes
        for needle in (str(self.folder), str(self.tmp), "a.pdf", "b.txt", "file:",
                       "R-src-r1", "OCC-r1", "secret-raw-bytes", "relative_path",
                       "origin_locator", "rights"):
            self.assertNotIn(needle, json.dumps(first), needle)
        self.assertEqual(self.count("human_approval"), 0)

    def test_different_path_identical_bytes_reuses_source(self) -> None:
        self.write("one.pdf", b"same bytes")
        self.write("two.pdf", b"same bytes")
        out = self.ok([
            self.entry("one.pdf", "r1"),
            self.entry("two.pdf", "r2"),
        ])
        refs = [i["source_ref"] for i in out["items"]]
        self.assertEqual(refs[0], refs[1])
        self.assertNotEqual(
            out["items"][0]["occurrence_ref"], out["items"][1]["occurrence_ref"]
        )
        self.assertEqual(out["items"][1]["disposition"], "duplicate_bytes_of_existing_revision")

    def test_second_entry_rights_failure_rolls_back_everything(self) -> None:
        self.write("a.pdf", b"one")
        self.write("b.pdf", b"two")
        good = self.entry("a.pdf", "r1", artifact="doc-a")
        bad = self.entry("b.pdf", "r2", artifact="doc-b")
        bad["occurrence_rights"]["subject"]["revision_id"] = "wrong"
        before = self.snapshot()
        err = self.fail_cli([good, bad], _VALID)
        self.assertIn("entry index 1", err)
        self.assertEqual(self.snapshot(), before)
        self.assertNotIn("wrong", err)

    def test_symlink_missing_traversal_before_any_write(self) -> None:
        self.write("a.pdf", b"one")
        (self.folder / "link.pdf").symlink_to(self.folder / "a.pdf")
        before = self.snapshot()
        good = self.entry("a.pdf", "r1")
        for bad_path in ("link.pdf", "missing.pdf", "../outside.pdf", "/abs.pdf"):
            bad = self.entry(bad_path, "r2", artifact="doc-b")
            err = self.fail_cli([good, bad], _VALID)
            self.assertNotIn(bad_path, err)
            self.assertNotIn(str(self.folder), err)
            self.assertEqual(self.snapshot(), before)

    def test_changed_source_same_request_key_conflicts(self) -> None:
        self.write("a.pdf", b"original")
        entries = [self.entry("a.pdf", "r1")]
        self.ok(entries)
        before = self.snapshot()
        self.write("a.pdf", b"CHANGED")
        err = self.fail_cli(entries, _CONFLICT)
        self.assertIn("rolled back", err)
        self.assertEqual(self.snapshot(), before)

    def test_inline_malformed_extra_and_authority_keys_rejected(self) -> None:
        self.write("a.pdf", b"one")
        before = self.snapshot()
        base = self.entry("a.pdf", "r1")
        variants = []
        for extra in ("approved", "authorizes_use", "actor", "disposition"):
            v = copy.deepcopy(base)
            v[extra] = True
            variants.append(v)
        for missing in ("rights", "occurrence_rights", "request_id", "artifact_id"):
            v = copy.deepcopy(base)
            del v[missing]
            variants.append(v)
        v = copy.deepcopy(base)
        v["rights"] = "not-a-mapping"
        variants.append(v)
        for v in variants:
            self.fail_cli([v], _VALID)
        self.assertEqual(self.snapshot(), before)
        # not a list / duplicate keys / non-JSON
        for payload in ('{"a": 1}', '[{"a": 1, "a": 2}]', "not json", "[NaN]"):
            path = self.tmp / "bad.json"
            path.write_text(payload, encoding="utf-8")
            code, out, err = self._run(
                "acquisition", "folder", "import", str(self.folder),
                "--entries-file", str(path), "--project", self.project_id,
            )
            self.assertEqual(code, _VALID, err)
            self.assertEqual(out, "")
        self.assertEqual(self.snapshot(), before)

    def test_limits_are_not_silently_truncated(self) -> None:
        self.write("a.pdf", b"one")
        self.write("b.pdf", b"two")
        entries = [self.entry("a.pdf", "r1"), self.entry("b.pdf", "r2", artifact="d2")]
        before = self.snapshot()
        err = self.fail_cli(entries, _VALID, "--max-files", "1")
        self.assertIn("too_many_files", err)
        err = self.fail_cli(entries, _VALID, "--max-total-bytes", "5")
        self.assertIn("total_too_large", err)
        self.fail_cli(entries, _VALID, "--max-files", "0")
        self.fail_cli(entries, _VALID, "--max-total-bytes", "-1")
        self.fail_cli(entries, _VALID, "--max-files", "999999999")
        self.assertEqual(self.snapshot(), before)
        out = self.ok(entries, "--max-files", "2", "--max-total-bytes", "6")
        self.assertEqual(out["counts"]["entries"], 2)

    def test_project_isolation_and_no_approval_writes(self) -> None:
        other_id, other_root = self._create("other")
        self.write("a.pdf", b"one")
        other_before = self.snapshot(other_id, other_root)
        self.ok([self.entry("a.pdf", "r1")])
        self.assertEqual(self.snapshot(other_id, other_root), other_before)
        self.assertEqual(self.count("human_approval"), 0)
        # rights bound to the wrong project are refused and write nothing
        before = self.snapshot(other_id, other_root)
        self.fail_cli([self.entry("a.pdf", "r1")], _VALID, project=other_id)
        self.assertEqual(self.snapshot(other_id, other_root), before)

    def test_root_unavailable_and_privacy_of_error(self) -> None:
        err = self.fail_cli(
            [self.entry("a.pdf", "r1")], _VALID, root=self.tmp / "no-such-root"
        )
        self.assertNotIn("no-such-root", err)
        self.assertIn("root_unavailable", err)


if __name__ == "__main__":
    unittest.main()
