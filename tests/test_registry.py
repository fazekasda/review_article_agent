"""Registry-specific tests: arbitration, explicit adoption, defaults.

Complements ``test_projects.py`` by exercising the minimal atomic registry in
isolation: one selected project, explicit ``register_restored`` (never implicit
attachment), XDG data-dir defaulting, and cross-process persistence.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from review_article_agent.domain.errors import (  # noqa: E402
    InvalidProjectId,
    ProjectConflict,
    ProjectNotFound,
    ProjectPathError,
)
from review_article_agent.storage import Registry  # noqa: E402
from review_article_agent.storage import paths  # noqa: E402


class RegistryTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="raa-registry-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.data_dir = self.tmp / "state"

    def test_default_data_dir_honours_xdg(self) -> None:
        env = {"XDG_DATA_HOME": str(self.tmp / "xdg")}
        self.assertEqual(
            paths.default_data_dir(env),
            str((self.tmp / "xdg" / paths.APP_DATA_DIR_NAME).resolve()),
        )
        explicit = Registry(self.data_dir)
        self.assertEqual(explicit.data_dir, paths.resolve_root(self.data_dir))

    def test_explicit_register_adopts_moved_store_with_same_id(self) -> None:
        reg = Registry(self.data_dir)
        a = reg.create_project(self.tmp / "orig", name="Alpha")

        # Simulate a user copying the store to a new root and losing the registry.
        dest = self.tmp / "restored"
        dest.mkdir()
        shutil.copytree(paths.app_dir_for(a.root_path), paths.app_dir_for(str(dest)))

        fresh = Registry(self.tmp / "fresh-state")
        with self.assertRaises(ProjectPathError):
            # Wrong (mismatched) explicit id is refused.
            fresh.register_restored(dest, project_id="prj_" + "b" * 32)
        info = fresh.register_restored(dest)
        self.assertEqual(info.project_id, a.project_id)
        self.assertEqual(info.root_path, paths.resolve_root(dest))

    def test_register_restored_never_implicit(self) -> None:
        reg = Registry(self.data_dir)
        # A plain directory with no app-owned store cannot be registered.
        empty = self.tmp / "empty"
        empty.mkdir()
        with self.assertRaises(ProjectPathError):
            reg.register_restored(empty)

    def test_duplicate_registration_across_locations_conflicts(self) -> None:
        reg = Registry(self.data_dir)
        a = reg.create_project(self.tmp / "orig", name="Alpha")
        dest = self.tmp / "copy"
        dest.mkdir()
        shutil.copytree(paths.app_dir_for(a.root_path), paths.app_dir_for(str(dest)))
        with self.assertRaises(ProjectConflict):
            reg.register_restored(dest)  # id already registered at orig

    def test_single_active_arbitration_and_status(self) -> None:
        reg = Registry(self.data_dir)
        a = reg.create_project(self.tmp / "a", name="A")
        b = reg.create_project(self.tmp / "b", name="B")
        c = reg.create_project(self.tmp / "c", name="C")

        reg.select(a.project_id)
        reg.select(c.project_id)
        status = reg.status()
        self.assertEqual(status["active_project_id"], c.project_id)
        self.assertFalse(status["running"])
        self.assertEqual(
            {p["project_id"] for p in status["projects"]},
            {a.project_id, b.project_id, c.project_id},
        )
        # Exactly one active row remains after repeated selection.
        self.assertEqual(self.registry_row_count(), 1)

    def registry_row_count(self) -> int:
        reg = Registry(self.data_dir)
        conn = reg._connect()
        try:
            return int(conn.execute("SELECT COUNT(*) FROM active_project").fetchone()[0])
        finally:
            conn.close()

    def test_get_and_list_are_stable(self) -> None:
        reg = Registry(self.data_dir)
        a = reg.create_project(self.tmp / "a", name="A")
        b = reg.create_project(self.tmp / "b", name="B")
        self.assertEqual(reg.get(a.project_id).name, "A")
        listed = reg.list_projects()
        self.assertEqual({p.project_id for p in listed}, {a.project_id, b.project_id})
        with self.assertRaises(ProjectNotFound):
            reg.get("prj_" + "9" * 32)
        with self.assertRaises(InvalidProjectId):
            reg.get("../escape")

    def test_cross_process_registration_is_visible(self) -> None:
        """A store created in a separate process is visible to this process."""
        root = self.tmp / "subproc"
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join([str(SRC), env.get("PYTHONPATH", "")])
        script = textwrap.dedent(
            """
            import sys
            from review_article_agent.storage import Registry
            info = Registry(sys.argv[1]).create_project(sys.argv[2], name="Sub")
            print(info.project_id)
            """
        )
        out = subprocess.run(
            [sys.executable, "-c", script, str(self.data_dir), str(root)],
            check=True,
            capture_output=True,
            text=True,
            env=env,
        )
        pid = out.stdout.strip()
        self.assertTrue(pid.startswith("prj_"))
        reg = Registry(self.data_dir)
        self.assertEqual([p.project_id for p in reg.list_projects()], [pid])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
