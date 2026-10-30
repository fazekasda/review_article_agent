"""Execution activation: durable, atomic, separate from UI selection.

Covers Step 03 AC4/AC5 and ``docs/contracts/02-gates-and-runtime.md`` §8.3–8.5:

* execution activation is a durable registry record, distinct from the *selected*
  project (``project selected != executing``);
* at most one project holds execution at a time; a concurrent activation race
  across real processes yields exactly one winner;
* the activation is fenced by a token: a stale token can neither release
  execution nor authorize a local write;
* a switch only proceeds after drain + release; the selected project may change
  without stealing execution;
* an execution-active project cannot be relocated out from under a worker;
* registry migration 1→2 preserves mappings and the selected project, and
  foreign/newer/symlink safety is unchanged.

All fixtures are synthetic temp directories; nothing here is scientific evidence.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from _tiers import slow

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from review_article_agent.domain.errors import (  # noqa: E402
    ProjectConflict,
    ProjectPathError,
    StoreError,
    UnsupportedSchemaVersion,
)
from review_article_agent.storage import Registry, transaction  # noqa: E402
from review_article_agent.storage import paths, schema  # noqa: E402
from review_article_agent.storage.registry import ExecutionActivation  # noqa: E402


def _child_env() -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(SRC), env.get("PYTHONPATH", "")])
    return env


class ActivationTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="raa-activation-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.data_dir = self.tmp / "state"

    def _two_projects(self) -> tuple[Registry, str, str]:
        reg = Registry(self.data_dir)
        a = reg.create_project(self.tmp / "a", name="A")
        b = reg.create_project(self.tmp / "b", name="B")
        return reg, a.project_id, b.project_id

    # -- activation is separate from selection ------------------------------- #
    def test_activation_is_separate_from_selection(self) -> None:
        reg, a, b = self._two_projects()

        # No activation until explicitly granted.
        self.assertEqual(reg.activation().state, "none")
        self.assertIsNone(reg.activation().active_project_id)
        self.assertIsNone(reg.activation().activation_token)

        # Selecting a project is a UI choice; it must not grant execution.
        reg.select(a)
        self.assertEqual(reg.active().project_id, a)
        self.assertEqual(reg.activation().state, "none")

        # Activating execution must not change the selected project.
        act = reg.activate_execution(b)
        self.assertEqual(act.state, "active")
        self.assertEqual(act.active_project_id, b)
        self.assertIsNotNone(act.activation_token)
        self.assertEqual(reg.active().project_id, a)

        status = reg.status()
        self.assertEqual(status["active_project_id"], a)  # selected
        self.assertEqual(status["execution"]["state"], "active")
        self.assertEqual(status["execution"]["active_project_id"], b)
        self.assertNotIn("activation_token", status["execution"])
        self.assertFalse(status["running"])

    def test_selection_does_not_steal_execution(self) -> None:
        reg, a, b = self._two_projects()
        token_b = reg.activate_execution(b).activation_token
        # Selecting another project must never transfer the activation.
        reg.select(a)
        reg.select(b)
        current = reg.activation()
        self.assertEqual(current.active_project_id, b)
        self.assertEqual(current.activation_token, token_b)

    # -- at most one active; idempotent re-activation ------------------------ #
    def test_second_activation_while_active_is_refused(self) -> None:
        reg, a, b = self._two_projects()
        reg.activate_execution(a)
        with self.assertRaises(ProjectConflict):
            reg.activate_execution(b)
        self.assertEqual(reg.activation().active_project_id, a)

    def test_reactivating_same_project_reuses_token(self) -> None:
        reg, a, _ = self._two_projects()
        first = reg.activate_execution(a)
        again = reg.activate_execution(a)
        self.assertEqual(first.activation_token, again.activation_token)

    # -- token fencing: stale token cannot release or authorize writes ------- #
    def test_stale_token_cannot_release_or_authorize(self) -> None:
        reg, a, b = self._two_projects()
        token_a = reg.activate_execution(a).activation_token
        reg.begin_drain(token_a)
        reg.release_execution(token_a)
        self.assertEqual(reg.activation().state, "none")

        token_b = reg.activate_execution(b).activation_token
        # A stale token (project A's) cannot drain/release/authorize B.
        with self.assertRaises(ProjectConflict):
            reg.begin_drain(token_a)
        with self.assertRaises(ProjectConflict):
            reg.release_execution(token_a)
        with self.assertRaises(ProjectConflict):
            with reg.execution_write_guard(token_a, b):
                pass
        self.assertEqual(reg.activation().active_project_id, b)
        self.assertEqual(reg.activation().activation_token, token_b)

    def test_drain_release_then_switch(self) -> None:
        reg, a, b = self._two_projects()
        token_a = reg.activate_execution(a).activation_token

        drained = reg.begin_drain(token_a)
        self.assertEqual(drained.state, "draining")
        # While draining, a new project still cannot activate.
        with self.assertRaises(ProjectConflict):
            reg.activate_execution(b)

        released = reg.release_execution(token_a)
        self.assertEqual(released.state, "none")
        self.assertIsNone(released.active_project_id)
        self.assertIsNone(released.activation_token)
        # The old token is now inert.
        with self.assertRaises(ProjectConflict):
            reg.release_execution(token_a)

        # Only now may B activate, with a fresh token.
        token_b = reg.activate_execution(b).activation_token
        self.assertNotEqual(token_a, token_b)
        self.assertEqual(reg.activation().active_project_id, b)

    def test_release_refused_from_none(self) -> None:
        reg, _, _ = self._two_projects()
        with self.assertRaises(ProjectConflict):
            reg.release_execution("act_" + "0" * 32)
        with self.assertRaises(ProjectConflict):
            reg.begin_drain("act_" + "0" * 32)

    # -- accepted-write guard ------------------------------------------------ #
    def test_write_guard_authorizes_only_active_token(self) -> None:
        reg, a, b = self._two_projects()
        token = reg.activate_execution(a).activation_token

        with reg.execution_write_guard(token, a) as act:
            self.assertEqual(act.active_project_id, a)

        with self.assertRaises(ProjectConflict):
            with reg.execution_write_guard(token, b):  # right token, wrong project
                pass
        with self.assertRaises(ProjectConflict):
            with reg.execution_write_guard("act_" + "f" * 32, a):  # wrong token
                pass

        reg.begin_drain(token)
        # While draining, writes are no longer authorized.
        with self.assertRaises(ProjectConflict):
            with reg.execution_write_guard(token, a):
                pass

    def test_missing_activation_row_is_fail_closed(self) -> None:
        """A missing singleton row is corruption, never a fabricated ``none``."""
        reg, a, _ = self._two_projects()
        conn = schema.open_connection(reg.registry_path)
        try:
            conn.execute("DELETE FROM execution_activation")
        finally:
            conn.close()

        # No read, no token, no release: every path refuses rather than guessing.
        with self.assertRaises(StoreError):
            reg.activation()
        with self.assertRaises(StoreError):
            reg.activate_execution(a)
        with self.assertRaises(StoreError):
            reg.release_execution("act_" + "0" * 32)

        # No fabricated row was written as a side effect.
        conn = schema.open_connection(reg.registry_path)
        try:
            n = conn.execute("SELECT COUNT(*) FROM execution_activation").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(n, 0)

    def test_reconciliation_guard_allows_draining_but_not_new_dispatch(self) -> None:
        reg, a, b = self._two_projects()
        token = reg.activate_execution(a).activation_token
        reg.begin_drain(token)

        # New dispatch (default guard) is refused once draining.
        with self.assertRaises(ProjectConflict):
            with reg.execution_write_guard(token, a):
                pass

        # Reconciliation-only guard permits the matching draining project/token
        # so an already-accepted local write can commit before the switch.
        with reg.execution_write_guard(token, a, allow_draining=True) as act:
            self.assertEqual(act.state, "draining")
            self.assertEqual(act.active_project_id, a)

        # Still token- and project-fenced.
        with self.assertRaises(ProjectConflict):
            with reg.execution_write_guard(token, b, allow_draining=True):
                pass
        with self.assertRaises(ProjectConflict):
            with reg.execution_write_guard("act_" + "f" * 32, a, allow_draining=True):
                pass

        # After release, even reconciliation writes are refused.
        reg.release_execution(token)
        with self.assertRaises(ProjectConflict):
            with reg.execution_write_guard(token, a, allow_draining=True):
                pass

    @slow
    def test_release_cannot_race_accepted_write(self) -> None:
        """A release waits for the guarded local commit; it cannot interleave."""
        reg, a, _ = self._two_projects()
        token = reg.activate_execution(a).activation_token
        enter = self.tmp / "entered"
        exit_ = self.tmp / "exited"
        script = textwrap.dedent(
            """
            import sys, time
            from review_article_agent.storage import Registry
            with Registry(sys.argv[1]).execution_write_guard(sys.argv[2], sys.argv[3]):
                open(sys.argv[4], "w").close()
                time.sleep(float(sys.argv[5]))
                open(sys.argv[6], "w").close()
            """
        )
        proc = subprocess.Popen(
            [
                sys.executable, "-c", script, str(self.data_dir), token, a,
                str(enter), "0.8", str(exit_),
            ],
            env=_child_env(),
        )
        try:
            deadline = time.monotonic() + 10
            while not enter.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(enter.exists(), "child never entered the guard")

            start = time.monotonic()
            reg.release_execution(token)  # must block until the guard exits
            elapsed = time.monotonic() - start
            self.assertTrue(
                exit_.exists(),
                "release completed while the guarded local commit was open",
            )
            self.assertGreaterEqual(elapsed, 0.3)
            self.assertEqual(reg.activation().state, "none")
        finally:
            proc.wait(timeout=10)

    @slow
    def test_release_cannot_race_draining_reconciliation(self) -> None:
        """A release waits for a draining reconciliation commit too."""
        reg, a, _ = self._two_projects()
        token = reg.activate_execution(a).activation_token
        reg.begin_drain(token)
        enter = self.tmp / "d-enter"
        exit_ = self.tmp / "d-exit"
        script = textwrap.dedent(
            """
            import sys, time
            from review_article_agent.storage import Registry
            with Registry(sys.argv[1]).execution_write_guard(
                sys.argv[2], sys.argv[3], allow_draining=True
            ):
                open(sys.argv[4], "w").close()
                time.sleep(float(sys.argv[5]))
                open(sys.argv[6], "w").close()
            """
        )
        proc = subprocess.Popen(
            [
                sys.executable, "-c", script, str(self.data_dir), token, a,
                str(enter), "0.8", str(exit_),
            ],
            env=_child_env(),
        )
        try:
            deadline = time.monotonic() + 10
            while not enter.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(enter.exists(), "child never entered reconciliation guard")

            start = time.monotonic()
            reg.release_execution(token)  # must block until reconciliation exits
            elapsed = time.monotonic() - start
            self.assertTrue(
                exit_.exists(),
                "release completed while a draining reconciliation commit was open",
            )
            self.assertGreaterEqual(elapsed, 0.3)
            self.assertEqual(reg.activation().state, "none")
        finally:
            proc.wait(timeout=10)

    # -- relocation guard ---------------------------------------------------- #
    def test_relocate_refused_while_execution_active(self) -> None:
        reg, a, _ = self._two_projects()
        reg.activate_execution(a)
        new_root = self.tmp / "moved"
        with self.assertRaises(ProjectConflict):
            reg.relocate(a, new_root, move=True)
        # Nothing moved; the store is still at its registered location.
        self.assertTrue(os.path.isdir(paths.app_dir_for(str(self.tmp / "a"))))
        self.assertFalse(os.path.exists(str(new_root)))

        # After release, relocation is allowed again.
        reg.release_execution(reg.activation().activation_token)
        info = reg.relocate(a, new_root, move=True)
        self.assertEqual(info.project_id, a)
        self.assertEqual(info.root_path, paths.resolve_root(new_root))

    # -- cross-process activation race --------------------------------------- #
    def test_concurrent_activation_two_projects_one_wins(self) -> None:
        _, a, b = self._two_projects()
        script = textwrap.dedent(
            """
            import sys
            from review_article_agent.storage import Registry
            try:
                act = Registry(sys.argv[1]).activate_execution(sys.argv[2])
                print("OK", act.active_project_id)
            except Exception as exc:  # noqa: BLE001
                print("FAIL", type(exc).__name__)
            """
        )
        procs = [
            subprocess.Popen(
                [sys.executable, "-c", script, str(self.data_dir), pid],
                env=_child_env(), stdout=subprocess.PIPE, text=True,
            )
            for pid in (a, b)
        ]
        outs = [p.communicate(timeout=30)[0].strip() for p in procs]
        winners = [o for o in outs if o.startswith("OK")]
        self.assertEqual(len(winners), 1, f"expected exactly one winner, got {outs}")
        winner_pid = winners[0].split()[1]
        self.assertEqual(Registry(self.data_dir).activation().active_project_id, winner_pid)

    # -- migration 1 -> 2 ---------------------------------------------------- #
    def test_migration_v1_to_v2_preserves_mappings_and_selection(self) -> None:
        self.assertEqual(schema.REGISTRY_SCHEMA_VERSION, 2)
        paths.ensure_owner_private_dir(str(self.data_dir))
        reg_path = self.data_dir / paths.REGISTRY_FILENAME
        conn = schema.open_connection(str(reg_path))
        try:
            schema.migrate(
                conn,
                target_version=1,
                migrations=schema.REGISTRY_MIGRATIONS,
                label="application registry",
            )
            pa, pb = "prj_" + "a" * 32, "prj_" + "b" * 32
            with transaction(conn):
                conn.execute(
                    "INSERT INTO projects "
                    "(project_id, name, root_path, created_at_utc) VALUES (?,?,?,?)",
                    (pa, "A", str(self.tmp / "a"), "2026-01-01T00:00:00+00:00"),
                )
                conn.execute(
                    "INSERT INTO projects "
                    "(project_id, name, root_path, created_at_utc) VALUES (?,?,?,?)",
                    (pb, "B", str(self.tmp / "b"), "2026-01-02T00:00:00+00:00"),
                )
                conn.execute(
                    "INSERT INTO active_project (singleton, project_id, selected_at_utc) "
                    "VALUES (1, ?, ?)",
                    (pb, "2026-01-02T00:00:00+00:00"),
                )
        finally:
            conn.close()

        # Opening through the registry migrates 1 -> 2 in place.
        reg = Registry(self.data_dir)
        self.assertEqual(
            {p.project_id for p in reg.list_projects()}, {pa, pb}
        )
        self.assertEqual(reg.active().project_id, pb)  # selection preserved
        self.assertEqual(reg.activation().state, "none")  # seeded none, honest
        self.assertIsNone(reg.activation().active_project_id)

    # -- foreign / newer / symlink safety unchanged -------------------------- #
    def test_newer_registry_schema_still_refused(self) -> None:
        reg = Registry(self.data_dir)
        reg.create_project(self.tmp / "p", name="P")
        conn = schema.open_connection(reg.registry_path)
        try:
            conn.execute("PRAGMA user_version = 999")
        finally:
            conn.close()
        with self.assertRaises(UnsupportedSchemaVersion):
            Registry(self.data_dir).activation()

    def test_foreign_database_still_refused(self) -> None:
        paths.ensure_owner_private_dir(str(self.data_dir))
        conn = schema.open_connection(str(self.data_dir / paths.REGISTRY_FILENAME))
        try:
            conn.execute("CREATE TABLE unrelated (x TEXT)")
        finally:
            conn.close()
        with self.assertRaises(StoreError):
            Registry(self.data_dir).activation()

    def test_symlinked_lock_is_refused(self) -> None:
        reg = Registry(self.data_dir)
        reg.create_project(self.tmp / "p", name="P")
        lock = self.data_dir / paths.REGISTRY_LOCK_FILENAME
        if lock.exists():
            lock.unlink()
        target = self.tmp / "elsewhere.lock"
        target.write_text("")
        os.symlink(target, lock)
        with self.assertRaises(ProjectPathError):
            Registry(self.data_dir).activation()

    # -- helper dataclass is importable with the expected shape -------------- #
    def test_execution_activation_shape(self) -> None:
        act = ExecutionActivation("none", None, None, None, None)
        self.assertNotIn("activation_token", act.as_dict())
        self.assertEqual(act.as_dict()["state"], "none")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
