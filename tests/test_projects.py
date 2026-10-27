"""Integration tests for the durable local project foundation.

Covers the Step 02 acceptance slice owned by this task: two independent
non-Git projects created and recovered across separate registry instances,
single selection, explicit relocation preserving identity, denial of stale/
mismatched/symlinked stores, idempotent duplicate create, injected-failure
rollback, and owner-private app-owned artifacts.

All fixtures are synthetic temp directories; nothing here is scientific
evidence or a real user project.
"""

from __future__ import annotations

import os
import shutil
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from review_article_agent.domain.errors import (  # noqa: E402
    InvalidProjectId,
    ProjectConflict,
    ProjectNotFound,
    ProjectPathError,
    UnsupportedSchemaVersion,
)
from review_article_agent.storage import ProjectStore, Registry  # noqa: E402
from review_article_agent.storage import paths, schema  # noqa: E402


class ProjectFoundationTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="raa-projects-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.data_dir = self.tmp / "state"
        self.root_a = self.tmp / "proj-a"
        self.root_b = self.tmp / "proj-b"

    def registry(self) -> Registry:
        return Registry(self.data_dir)

    # -- AC: two independent, recoverable, non-Git projects ------------------ #
    def test_two_projects_persist_and_recover_independently(self) -> None:
        reg = self.registry()
        a = reg.create_project(self.root_a, name="Alpha")
        b = reg.create_project(self.root_b, name="Beta")

        self.assertNotEqual(a.project_id, b.project_id)
        self.assertTrue(a.project_id.startswith("prj_"))
        for info in (a, b):
            self.assertTrue(os.path.isdir(info.root_path))
            self.assertFalse((Path(info.root_path) / ".git").exists())

        # Write independent synthetic state into each store.
        with ProjectStore.open_at(paths.app_dir_for(a.root_path)) as store:
            with store.transaction():
                store.connection.execute(
                    "INSERT OR REPLACE INTO meta (key, value) VALUES ('marker', 'A')"
                )
        # B is an independent store: it must not see A's marker.
        with ProjectStore.open_at(paths.app_dir_for(b.root_path)) as store_b:
            self.assertNotIn("marker", store_b.metadata())
            with store_b.transaction():
                store_b.connection.execute(
                    "INSERT OR REPLACE INTO meta (key, value) VALUES ('marker', 'B')"
                )

        # Recover through a brand-new registry instance (persistence).
        reg2 = self.registry()
        ids = {p.project_id for p in reg2.list_projects()}
        self.assertEqual(ids, {a.project_id, b.project_id})

        with ProjectStore.open_at(paths.app_dir_for(a.root_path)) as store:
            self.assertEqual(store.metadata()["marker"], "A")
        with ProjectStore.open_at(paths.app_dir_for(b.root_path)) as store:
            self.assertEqual(store.metadata()["marker"], "B")

    def test_only_one_selected_project(self) -> None:
        reg = self.registry()
        a = reg.create_project(self.root_a, name="Alpha")
        b = reg.create_project(self.root_b, name="Beta")

        self.assertIsNone(reg.active())
        reg.select(a.project_id)
        self.assertEqual(reg.active().project_id, a.project_id)
        reg.select(b.project_id)
        self.assertEqual(reg.active().project_id, b.project_id)
        # Selection (identity) persisted for a fresh instance.
        self.assertEqual(self.registry().active().project_id, b.project_id)
        # Selection is not execution.
        self.assertFalse(reg.status()["running"])

    # -- AC: explicit relocation preserves identity -------------------------- #
    def test_explicit_move_preserves_id(self) -> None:
        reg = self.registry()
        a = reg.create_project(self.root_a, name="Alpha")
        dest = self.tmp / "moved-a"

        moved = reg.relocate(a.project_id, dest, move=True)
        self.assertEqual(moved.project_id, a.project_id)
        self.assertEqual(moved.root_path, paths.resolve_root(dest))
        self.assertFalse(os.path.exists(paths.app_dir_for(a.root_path)))
        self.assertTrue(os.path.isfile(paths.store_path_for(dest)))

        # The moved project is still selectable and readable under its ID.
        reg.select(a.project_id)
        with ProjectStore.open_at(paths.app_dir_for(moved.root_path)) as store:
            self.assertEqual(store.project_id, a.project_id)

    def test_copy_then_explicit_relocate_refuses_stale_duplicate(self) -> None:
        reg = self.registry()
        a = reg.create_project(self.root_a, name="Alpha")
        dest = self.tmp / "copy-a"
        dest.mkdir()
        # User copies the app-owned directory to a new root by hand.
        shutil.copytree(paths.app_dir_for(a.root_path), paths.app_dir_for(str(dest)))

        with self.assertRaises(ProjectPathError):
            reg.relocate(a.project_id, dest, move=False)

        # Removing the stale copy at the old location permits the relocation.
        shutil.rmtree(paths.app_dir_for(a.root_path))
        relocated = reg.relocate(a.project_id, dest, move=False)
        self.assertEqual(relocated.project_id, a.project_id)
        self.assertEqual(relocated.root_path, paths.resolve_root(dest))

    # -- AC: mismatch / symlink / stale denial ------------------------------- #
    def test_store_id_mismatch_is_denied(self) -> None:
        reg = self.registry()
        a = reg.create_project(self.root_a, name="Alpha")
        app_dir = paths.app_dir_for(a.root_path)
        with ProjectStore.open_at(app_dir) as store:
            with store.transaction():
                store.connection.execute(
                    "UPDATE project SET project_id = ?",
                    ("prj_" + "0" * 32,),
                )
        with self.assertRaises(ProjectPathError):
            reg.select(a.project_id)
        with self.assertRaises(ProjectPathError):
            reg.relocate(a.project_id, self.root_b, move=True)

    def test_symlinked_store_is_denied(self) -> None:
        reg = self.registry()
        a = reg.create_project(self.root_a, name="Alpha")
        b = reg.create_project(self.root_b, name="Beta")
        app_a = paths.app_dir_for(a.root_path)
        # Replace A's app-owned directory with a symlink to B's store.
        shutil.rmtree(app_a)
        os.symlink(paths.app_dir_for(b.root_path), app_a)

        with self.assertRaises(ProjectPathError):
            ProjectStore.open_at(app_a)
        with self.assertRaises(ProjectPathError):
            reg.select(a.project_id)

    def test_unregistered_orphan_is_not_implicitly_attached(self) -> None:
        reg = self.registry()
        a = reg.create_project(self.root_a, name="Alpha")
        # Simulate the registry losing the row while the store remains.
        reg2 = Registry(self.tmp / "fresh-state")
        with self.assertRaises(ProjectPathError):
            reg2.create_project(self.root_a, name="Alpha")
        # The orphan store is untouched.
        self.assertTrue(os.path.isfile(paths.store_path_for(a.root_path)))

    # -- AC: idempotent create / divergent conflict -------------------------- #
    def test_repeated_create_is_idempotent_and_divergence_conflicts(self) -> None:
        reg = self.registry()
        first = reg.create_project(self.root_a, name="Alpha")
        again = reg.create_project(self.root_a, name="Alpha")
        self.assertEqual(first.project_id, again.project_id)
        self.assertEqual(len(reg.list_projects()), 1)

        with self.assertRaises(ProjectConflict):
            reg.create_project(self.root_a, name="Different")
        with self.assertRaises(ProjectConflict):
            reg.create_project(self.root_a, name="Alpha", project_id="prj_" + "a" * 32)

        # A fresh instance sees the same single registration.
        self.assertEqual(len(self.registry().list_projects()), 1)

    def test_unsafe_project_ids_are_rejected(self) -> None:
        reg = self.registry()
        with self.assertRaises(InvalidProjectId):
            reg.create_project(self.root_a, project_id="../../etc/passwd")
        with self.assertRaises(InvalidProjectId):
            reg.select("../../etc/passwd")
        with self.assertRaises(InvalidProjectId):
            reg.get("not-an-id")
        with self.assertRaises(ProjectNotFound):
            reg.get("prj_" + "f" * 32)

    # -- AC: injected failure rolls back, unrelated content preserved -------- #
    def test_injected_registration_failure_leaves_no_half_store(self) -> None:
        reg = self.registry()
        root = self.tmp / "fresh"
        root.mkdir()
        keep = root / "keep.txt"
        keep.write_text("unrelated user content\n", encoding="utf-8")

        with mock.patch.object(
            reg, "_insert_project_row", side_effect=RuntimeError("injected")
        ):
            with self.assertRaises(RuntimeError):
                reg.create_project(root, name="Broken")

        self.assertEqual(reg.list_projects(), [])
        self.assertFalse(os.path.exists(paths.app_dir_for(str(root))))
        self.assertFalse(
            any(name.startswith(paths.STAGING_PREFIX) for name in os.listdir(root))
        )
        # Unrelated user content is never deleted.
        self.assertEqual(keep.read_text(encoding="utf-8"), "unrelated user content\n")

    def test_created_root_removed_when_empty_after_failure(self) -> None:
        reg = self.registry()
        root = self.tmp / "will-not-exist" / "deep"
        with mock.patch.object(
            reg, "_insert_project_row", side_effect=RuntimeError("injected")
        ):
            with self.assertRaises(RuntimeError):
                reg.create_project(root)
        self.assertFalse(os.path.exists(root))

    # -- AC: app-owned files are owner-private ------------------------------- #
    def test_app_owned_artifacts_are_owner_private(self) -> None:
        reg = self.registry()
        a = reg.create_project(self.root_a, name="Alpha")
        app_dir = paths.app_dir_for(a.root_path)
        store_path = paths.store_path_for(a.root_path)

        self.assertEqual(stat.S_IMODE(os.stat(app_dir).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.stat(store_path).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(self.data_dir).st_mode), 0o700)
        self.assertEqual(
            stat.S_IMODE(os.stat(reg.registry_path).st_mode), 0o600
        )

    # -- AC: newer schema refused ------------------------------------------- #
    def test_newer_store_schema_is_refused(self) -> None:
        reg = self.registry()
        a = reg.create_project(self.root_a, name="Alpha")
        app_dir = paths.app_dir_for(a.root_path)
        conn = schema.open_connection(paths.store_path_for(a.root_path))
        try:
            conn.execute("PRAGMA user_version = 999")
        finally:
            conn.close()
        with self.assertRaises(UnsupportedSchemaVersion):
            ProjectStore.open_at(app_dir)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
