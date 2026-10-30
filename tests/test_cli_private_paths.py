"""In-process CLI tests for the clearer private-path and forbidden-destination errors.

Both cases drive the real entry point (``cli.main.main`` with in-memory streams)
over a real temporary registry/project store. They pin the two audit fixes:

* a data directory (or app-owned project directory) that is not owner-private
  (``0700``) is refused with a specific, actionable message naming the path, the
  mode it has, and the ``chmod`` remedy (storage, exit ``4``);
* ``backup create`` with a destination inside ``--data-dir`` is refused with a
  specific message pointing to a destination outside ``--data-dir`` (storage,
  exit ``4``).

Synthetic data only: no network, no model call, no approval.

Run::

    PYTHONPATH=src python3 -m unittest discover -s tests -p test_cli_private_paths.py
"""

from __future__ import annotations

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

from review_article_agent.cli import main as cli_main  # noqa: E402


class PrivatePathCliTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="raa-cli-private-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.state = self.tmp / "state"

    # -- driving ------------------------------------------------------------- #
    def _run(self, *args: str):
        out, err = io.StringIO(), io.StringIO()
        code = cli_main.main(
            ["--data-dir", str(self.state), *args], env={}, stdout=out, stderr=err
        )
        return code, out.getvalue(), err.getvalue()

    def cli(self, *args: str, expect: int = 0) -> dict:
        code, out, err = self._run(*args)
        self.assertEqual(code, expect, f"args={args} rc={code}\n{out}\n{err}")
        self.assertNotIn("Traceback", err)
        return json.loads(out if expect == 0 else err)

    def create_project(self) -> str:
        code, out, err = self._run("create", str(self.tmp / "proj"), "--name", "proj")
        self.assertEqual(code, 0, err)
        return json.loads(out)["project"]["project_id"]

    # -- tests ---------------------------------------------------------------- #
    def test_permissive_data_dir_is_refused_with_remedy(self) -> None:
        self.state.mkdir(parents=True, exist_ok=True)
        os.chmod(self.state, 0o755)

        err = self.cli("list", expect=4)
        self.assertEqual(err["error"]["category"], "storage")
        message = err["error"]["message"]
        self.assertIn("must be private to its owner (mode 0700)", message)
        self.assertIn("chmod 700", message)
        self.assertIn(str(self.state), message)

    def test_permissive_app_dir_is_refused_with_remedy(self) -> None:
        pid = self.create_project()
        app_dir = self.tmp / "proj" / ".review_article_agent"
        os.chmod(app_dir, 0o755)

        err = self.cli("status", "--project", pid, expect=4)
        self.assertEqual(err["error"]["category"], "storage")
        message = err["error"]["message"]
        self.assertIn("must be private to its owner (mode 0700)", message)
        self.assertIn("chmod 700", message)
        self.assertIn(str(app_dir), message)

    def test_backup_create_inside_data_dir_is_refused_with_guidance(self) -> None:
        pid = self.create_project()
        inside = self.state / "snapshot-out"

        err = self.cli("backup", "create", str(inside), "--project", pid, expect=4)
        self.assertEqual(err["error"]["category"], "storage")
        message = err["error"]["message"]
        self.assertIn("inside the application data directory", message)
        self.assertIn("outside", message)
        self.assertFalse(inside.exists())

    def test_backup_create_inside_app_dir_is_refused(self) -> None:
        pid = self.create_project()
        inside = self.tmp / "proj" / ".review_article_agent" / "snapshot-out"

        err = self.cli("backup", "create", str(inside), "--project", pid, expect=4)
        self.assertEqual(err["error"]["category"], "storage")
        self.assertIn("inside the application data directory", err["error"]["message"])
        self.assertFalse(inside.exists())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
