"""Regression tests for T1-R1: identity/path safety and Python compatibility.

Each test reproduces a specific integrated-base failure and proves the fix:

1. A foreign/mismatched store must be refused **before any write** — its
   ``journal_mode`` and bytes stay unchanged.
2. A symlinked registry database (or lock) must be refused without touching the
   foreign target file.
3. Owner-private directory policy is enforced, not silently ignored.
4. ``relocate(move=True)`` rolls the directory move back if the registry update
   fails, and refuses a cross-device move outright.
5. Idempotent short-circuits re-validate the store instead of returning blindly.
6. The project-ID pattern uses no version-specific regex escape.
"""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from review_article_agent.domain.errors import (  # noqa: E402
    ProjectPathError,
    StoreError,
)
from review_article_agent.storage import ProjectStore, Registry  # noqa: E402
from review_article_agent.storage import paths, schema  # noqa: E402


def _journal_mode(db_path: str) -> str:
    conn = sqlite3.connect(db_path)
    try:
        return str(conn.execute("PRAGMA journal_mode").fetchone()[0])
    finally:
        conn.close()


class IdentityPathSafetyTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="raa-safety-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.data_dir = self.tmp / "state"

    def test_foreign_identity_refused_without_mutating_store_bytes_or_journal(self) -> None:
        reg = Registry(self.data_dir)
        a = reg.create_project(self.tmp / "a", name="A")
        b = reg.create_project(self.tmp / "b", name="B")

        # Make B a non-WAL database so a journal_mode change would be visible.
        conn = sqlite3.connect(b.store_path)
        try:
            mode = conn.execute("PRAGMA journal_mode = DELETE").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(mode, "delete")

        before_bytes = Path(b.store_path).read_bytes()
        before_probe = schema.read_only_probe(b.store_path)

        # Ask for B's store while claiming A's identity.
        with self.assertRaises(ProjectPathError):
            ProjectStore.open_at(
                paths.app_dir_for(b.root_path), expected_project_id=a.project_id
            )
        with self.assertRaises(ProjectPathError):
            reg._verify_store_matches(b.root_path, a.project_id)

        # The foreign store is byte-identical and still in DELETE mode.
        self.assertEqual(Path(b.store_path).read_bytes(), before_bytes)
        self.assertEqual(_journal_mode(b.store_path), "delete")
        self.assertEqual(schema.read_only_probe(b.store_path), before_probe)

    def test_foreign_identity_refused_for_newer_schema_before_write(self) -> None:
        reg = Registry(self.data_dir)
        b = reg.create_project(self.tmp / "b", name="B")
        conn = sqlite3.connect(b.store_path)
        try:
            conn.execute("PRAGMA user_version = 999")
        finally:
            conn.close()
        before_bytes = Path(b.store_path).read_bytes()
        from review_article_agent.domain.errors import UnsupportedSchemaVersion

        with self.assertRaises(UnsupportedSchemaVersion):
            ProjectStore.open_at(paths.app_dir_for(b.root_path))
        self.assertEqual(Path(b.store_path).read_bytes(), before_bytes)

    def test_registry_db_symlink_refused_and_foreign_file_untouched(self) -> None:
        self.data_dir.mkdir(mode=0o700)
        foreign = self.tmp / "foreign.sqlite3"
        # An unrelated, empty SQLite file.
        sqlite3.connect(str(foreign)).close()
        before = foreign.read_bytes()
        os.symlink(foreign, self.data_dir / paths.REGISTRY_FILENAME)

        with self.assertRaises(ProjectPathError):
            Registry(self.data_dir).list_projects()

        self.assertEqual(foreign.read_bytes(), before)
        version, tables = schema.read_only_probe(str(foreign))
        self.assertEqual((version, tables), (0, set()))

    def test_registry_lock_symlink_refused(self) -> None:
        self.data_dir.mkdir(mode=0o700)
        target = self.tmp / "lock-target"
        target.write_text("do not touch", encoding="utf-8")
        os.symlink(target, self.data_dir / paths.REGISTRY_LOCK_FILENAME)

        with self.assertRaises(ProjectPathError):
            Registry(self.data_dir).list_projects()
        self.assertEqual(target.read_text(encoding="utf-8"), "do not touch")

    def test_owner_private_dir_policy_is_enforced(self) -> None:
        # A permissive existing data directory is refused, not silently used.
        permissive = self.tmp / "shared-state"
        permissive.mkdir()
        os.chmod(permissive, 0o755)
        with self.assertRaises(ProjectPathError):
            Registry(permissive).list_projects()

        # A permissive existing app-owned store directory is refused on open.
        reg = Registry(self.data_dir)
        a = reg.create_project(self.tmp / "a", name="A")
        app_dir = paths.app_dir_for(a.root_path)
        os.chmod(app_dir, 0o755)
        with self.assertRaises(ProjectPathError):
            ProjectStore.open_at(app_dir)

    def test_relocate_move_update_failure_rolls_back(self) -> None:
        reg = Registry(self.data_dir)
        a = reg.create_project(self.tmp / "a", name="A")
        dest = self.tmp / "dest"
        old_app = paths.app_dir_for(a.root_path)
        new_app = paths.app_dir_for(str(dest))

        with mock.patch.object(
            reg, "_update_project_root", side_effect=RuntimeError("injected")
        ):
            with self.assertRaises(RuntimeError):
                reg.relocate(a.project_id, dest, move=True)

        # Directory move undone; registry still points at the old, present store.
        self.assertTrue(os.path.isfile(paths.store_path_for(a.root_path)))
        self.assertFalse(os.path.exists(new_app))
        self.assertTrue(os.path.isdir(old_app))
        self.assertEqual(reg.get(a.project_id).root_path, a.root_path)
        reg.select(a.project_id)  # still fully usable

    def test_relocate_cross_device_refused_without_moving(self) -> None:
        reg = Registry(self.data_dir)
        a = reg.create_project(self.tmp / "a", name="A")
        dest = self.tmp / "dest"

        with mock.patch.object(reg, "_same_device", return_value=False):
            with self.assertRaises(ProjectPathError):
                reg.relocate(a.project_id, dest, move=True)

        self.assertTrue(os.path.isfile(paths.store_path_for(a.root_path)))
        self.assertFalse(os.path.exists(paths.app_dir_for(str(dest))))
        self.assertEqual(reg.get(a.project_id).root_path, a.root_path)

    def test_idempotent_shortcuts_revalidate_store(self) -> None:
        reg = Registry(self.data_dir)
        a = reg.create_project(self.tmp / "a", name="A")
        # Drift the store's recorded identity.
        conn = sqlite3.connect(a.store_path)
        try:
            conn.execute("UPDATE project SET project_id = ?", ("prj_" + "0" * 32,))
            conn.commit()
        finally:
            conn.close()

        with self.assertRaises(ProjectPathError):
            reg.create_project(self.tmp / "a", name="A")  # idempotent path
        with self.assertRaises(ProjectPathError):
            reg.register_restored(self.tmp / "a")

    def test_read_only_probe_absent_file_stays_absent(self) -> None:
        absent = self.tmp / "missing.sqlite3"
        with self.assertRaises(StoreError):
            schema.read_only_probe(str(absent))
        self.assertFalse(absent.exists())
        # The read-only identity path likewise refuses rather than creating.
        app_dir = self.tmp / "empty-app"
        app_dir.mkdir(mode=0o700)
        with self.assertRaises(ProjectPathError):
            ProjectStore.open_at(str(app_dir))  # no store file at all
        self.assertFalse((app_dir / paths.STORE_FILENAME).exists())

    def test_no_writable_fallback_on_readonly_open_failure(self) -> None:
        reg = Registry(self.data_dir)
        a = reg.create_project(self.tmp / "a", name="A")
        before = Path(a.store_path).read_bytes()

        # Inject a read-only open failure; a writable fallback would call
        # sqlite3.connect a second time. It must not.
        with mock.patch(
            "sqlite3.connect", side_effect=sqlite3.OperationalError("injected")
        ) as connect_mock:
            with self.assertRaises(StoreError):
                ProjectStore.open_at(paths.app_dir_for(a.root_path))

        self.assertEqual(connect_mock.call_count, 1)
        self.assertIs(connect_mock.call_args.kwargs.get("uri"), True)
        self.assertIn("mode=ro", connect_mock.call_args.args[0])
        # Nothing was created or modified.
        self.assertEqual(Path(a.store_path).read_bytes(), before)

    def test_project_id_pattern_has_no_version_specific_escape(self) -> None:
        # ``\z`` (3.14-only) must not appear; ``fullmatch`` is used instead.
        pattern = paths.PROJECT_ID_PATTERN.pattern
        self.assertNotIn(r"\z", pattern)
        self.assertNotIn(r"\A", pattern)
        self.assertEqual(
            paths.validate_project_id("prj_" + "a" * 32), "prj_" + "a" * 32
        )
        from review_article_agent.domain.errors import InvalidProjectId

        for bad in ("prj_" + "A" * 32, "prj_" + "a" * 31, "prj_", "prj_" + "a" * 32 + "x"):
            with self.assertRaises(InvalidProjectId):
                paths.validate_project_id(bad)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
