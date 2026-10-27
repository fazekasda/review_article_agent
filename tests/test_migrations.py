"""Versioned, transactional schema-migration tests.

Covers: version stamping, refusal of a newer (unsupported) schema, full
transactional rollback of a failed migration step, and preservation of existing
rows across an upgrade (the property future artifact migrations rely on).
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from review_article_agent.domain.errors import (  # noqa: E402
    MigrationError,
    UnsupportedSchemaVersion,
)
from review_article_agent.storage import ProjectStore, Registry  # noqa: E402
from review_article_agent.storage import paths, schema  # noqa: E402


def _table_names(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'"
    ).fetchall()
    return {row[0] for row in rows}


class MigrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="raa-migrations-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def _fresh_conn(self, name: str = "x.sqlite3") -> sqlite3.Connection:
        return schema.open_connection(str(self.tmp / name))

    def test_migrate_from_empty_sets_version(self) -> None:
        conn = self._fresh_conn()
        self.addCleanup(conn.close)
        self.assertEqual(schema.read_user_version(conn), 0)
        version = schema.migrate(
            conn,
            target_version=1,
            migrations=schema.STORE_MIGRATIONS,
            label="project store",
        )
        self.assertEqual(version, 1)
        self.assertEqual(schema.read_user_version(conn), 1)
        self.assertIn("project", _table_names(conn))

    def test_newer_schema_is_refused(self) -> None:
        conn = self._fresh_conn()
        self.addCleanup(conn.close)
        schema.migrate(
            conn,
            target_version=1,
            migrations=schema.STORE_MIGRATIONS,
            label="project store",
        )
        conn.execute("PRAGMA user_version = 42")
        with self.assertRaises(UnsupportedSchemaVersion) as ctx:
            schema.migrate(
                conn,
                target_version=1,
                migrations=schema.STORE_MIGRATIONS,
                label="project store",
            )
        self.assertEqual(ctx.exception.found, 42)

    def test_failed_migration_rolls_back_transactionally(self) -> None:
        conn = self._fresh_conn()
        self.addCleanup(conn.close)
        schema.migrate(
            conn,
            target_version=1,
            migrations=schema.STORE_MIGRATIONS,
            label="project store",
        )

        def _bad(conn: sqlite3.Connection) -> None:
            conn.execute("CREATE TABLE partial (x TEXT)")
            raise RuntimeError("boom")

        with self.assertRaises(MigrationError):
            schema.migrate(
                conn,
                target_version=2,
                migrations={1: schema.STORE_MIGRATIONS[1], 2: _bad},
                label="project store",
            )
        # Version unchanged and the partial table rolled back.
        self.assertEqual(schema.read_user_version(conn), 1)
        self.assertNotIn("partial", _table_names(conn))
        self.assertIn("project", _table_names(conn))

    def test_migration_preserves_existing_rows(self) -> None:
        conn = self._fresh_conn()
        self.addCleanup(conn.close)
        schema.migrate(
            conn,
            target_version=1,
            migrations=schema.STORE_MIGRATIONS,
            label="project store",
        )
        conn.execute(
            "INSERT INTO project (project_id, name, created_at_utc, app_version) "
            "VALUES ('prj_x', 'X', '2026-01-01T00:00:00+00:00', '0.0.0')"
        )

        def _v2(conn: sqlite3.Connection) -> None:
            # Stand-in for a future artifact table.
            conn.execute("CREATE TABLE artifact (artifact_id TEXT PRIMARY KEY)")

        schema.migrate(
            conn,
            target_version=2,
            migrations={1: schema.STORE_MIGRATIONS[1], 2: _v2},
            label="project store",
        )
        self.assertEqual(schema.read_user_version(conn), 2)
        self.assertIn("artifact", _table_names(conn))
        names = [r[0] for r in conn.execute("SELECT project_id FROM project")]
        self.assertEqual(names, ["prj_x"])

    def test_project_store_open_refuses_newer(self) -> None:
        app_dir = self.tmp / paths.APP_DIR_NAME
        store = ProjectStore.create_at(str(app_dir), "prj_" + "a" * 32)
        store.close()
        conn = schema.open_connection(paths.store_path_for(str(self.tmp)))
        try:
            conn.execute("PRAGMA user_version = 999")
        finally:
            conn.close()
        with self.assertRaises(UnsupportedSchemaVersion):
            ProjectStore.open_at(str(app_dir))

    def test_concurrent_applied_migration_is_not_reapplied(self) -> None:
        """A second connection must not re-run an already-applied step.

        Deterministic two-connection version: conn1 applies v1 and commits
        (simulating a concurrent migrator that finished); conn2 must observe the
        advanced version and skip the step rather than hitting a duplicate
        ``CREATE TABLE``.
        """
        db = self.tmp / "concurrent.sqlite3"
        calls: list[int] = []

        def step(conn: sqlite3.Connection) -> None:
            calls.append(1)
            conn.execute("CREATE TABLE fixture (id INTEGER PRIMARY KEY, v TEXT)")
            conn.execute("INSERT INTO fixture (v) VALUES ('x')")

        conn1 = schema.open_connection(str(db))
        conn2 = schema.open_connection(str(db))
        try:
            self.assertEqual(
                schema.migrate(
                    conn1, target_version=1, migrations={1: step}, label="fixture"
                ),
                1,
            )
            # conn2 starts fresh against the same now-v1 database.
            self.assertEqual(
                schema.migrate(
                    conn2, target_version=1, migrations={1: step}, label="fixture"
                ),
                1,
            )
            self.assertEqual(len(calls), 1)  # no stale-version attempt
            self.assertEqual(schema.read_user_version(conn2), 1)
            rows = conn2.execute("SELECT v FROM fixture").fetchall()
            self.assertEqual([r[0] for r in rows], ["x"])
        finally:
            conn1.close()
            conn2.close()

    def test_threaded_dual_migrate_applies_exactly_once(self) -> None:
        """Two threads racing at a barrier both succeed; step runs once.

        Both connections open the same fresh fixture database, synchronise on a
        barrier *before* acquiring the migration write lock, and migrate. The
        loser must wait, then re-read the version under the lock and skip the
        step — no duplicate ``CREATE TABLE``, one data row, same final version.
        """
        db = self.tmp / "threaded.sqlite3"
        schema.open_connection(str(db)).close()  # pre-create the file (WAL)

        barrier = threading.Barrier(2)
        counter = {"n": 0}
        counter_lock = threading.Lock()

        def step(conn: sqlite3.Connection) -> None:
            with counter_lock:
                counter["n"] += 1
            time.sleep(0.25)  # hold the write lock to force the loser to wait
            conn.execute("CREATE TABLE fixture (id INTEGER PRIMARY KEY, v TEXT)")
            conn.execute("INSERT INTO fixture (v) VALUES ('x')")

        errors: list[BaseException] = []
        results: list[int] = []

        def worker() -> None:
            conn = schema.open_connection(str(db))
            try:
                barrier.wait(timeout=10)
                results.append(
                    schema.migrate(
                        conn, target_version=1, migrations={1: step}, label="fixture"
                    )
                )
            except BaseException as exc:  # noqa: BLE001 - recorded for assertion
                errors.append(exc)
            finally:
                conn.close()

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        self.assertFalse(any(t.is_alive() for t in threads), "migration deadlocked")

        self.assertEqual(errors, [])
        self.assertEqual(sorted(results), [1, 1])
        self.assertEqual(counter["n"], 1)  # exactly one stale-free attempt
        conn = schema.open_connection(str(db))
        try:
            self.assertEqual(schema.read_user_version(conn), 1)
            rows = conn.execute("SELECT v FROM fixture").fetchall()
            self.assertEqual([r[0] for r in rows], ["x"])
        finally:
            conn.close()

    def test_registry_refuses_newer_schema(self) -> None:
        data_dir = self.tmp / "state"
        reg = Registry(data_dir)
        reg.create_project(self.tmp / "p", name="P")
        conn = schema.open_connection(reg.registry_path)
        try:
            conn.execute("PRAGMA user_version = 999")
        finally:
            conn.close()
        with self.assertRaises(UnsupportedSchemaVersion):
            Registry(data_dir).list_projects()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
