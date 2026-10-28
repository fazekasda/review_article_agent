"""Backup/verify/restore coverage for schema-11 discovery search observations.

These tests extend the private backup contract in
``src/review_article_agent/storage/backup.py`` to the inert, immutable
project-local discovery search observation (``storage/discovery.py``). They
prove:

* a fresh backup verifies and restores the exact canonical result bytes,
  PubMed ``query_translation``, recorded time, binding, and projections without
  any network/current service/provider access;
* a forged snapshot is refused by the schema-11 inner sweep even after the outer
  manifest database hash is recomputed, for a tampered payload, digest,
  projection, and timestamp;
* the shared sweep reports a foreign-project row through the same backup hook;
* a legacy schema-10 snapshot (no discovery table) remains supported and is
  upgraded by the ordinary store open.

Synthetic data only: no network, no credential, no model call, no paid call.

Run::

    PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_discovery_*.py'
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
import unittest
from pathlib import Path
from urllib.parse import quote

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
TESTS = Path(__file__).resolve().parent
if str(TESTS) not in sys.path:
    sys.path.insert(0, str(TESTS))

from review_article_agent.storage import (  # noqa: E402
    ProjectStore,
    Registry,
    paths,
    schema,
)
from review_article_agent.storage import backup as backup_mod  # noqa: E402
from review_article_agent.storage.backup import (  # noqa: E402
    MANIFEST_FILENAME,
    SNAPSHOT_FILENAME,
    BackupError,
    create_backup,
    restore_backup,
    verify_backup,
)
from review_article_agent.storage.discovery import (  # noqa: E402
    DiscoveryRepository,
    discovery_integrity_problems,
)
from test_discovery_store import (  # noqa: E402
    _TRIGGERS,
    DiscoveryStoreBase,
    _sha,
)


class DiscoveryBackupBase(DiscoveryStoreBase):
    def _backup(self, name: str = "backup") -> Path:
        dest = self.tmp / name
        create_backup(self.store, dest)
        return dest

    def _restore(self, dest: Path, name: str = "restored") -> ProjectStore:
        registry = Registry(self.tmp / "state")
        target = self.tmp / name
        target.mkdir()
        info = restore_backup(dest, target, registry)
        restored = ProjectStore.open_at(
            paths.app_dir_for(str(target)), expected_project_id=info.project_id
        )
        self.addCleanup(restored.close)
        return restored

    @staticmethod
    def _snapshot_conn(dest: Path) -> sqlite3.Connection:
        uri = "file:" + quote(str(dest / SNAPSHOT_FILENAME)) + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True, isolation_level=None)
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _rewrite_outer_db_hash(dest: Path) -> None:
        manifest_path = dest / MANIFEST_FILENAME
        manifest = json.loads(manifest_path.read_text())
        data = (dest / SNAPSHOT_FILENAME).read_bytes()
        manifest["database"]["sha256"] = "sha256:" + hashlib.sha256(data).hexdigest()
        manifest["database"]["byte_size"] = len(data)
        manifest_path.write_text(json.dumps(manifest))

    def _mutate(self, dest: Path, fn) -> None:
        conn = sqlite3.connect(str(dest / SNAPSHOT_FILENAME))
        conn.row_factory = sqlite3.Row
        try:
            saved: dict[str, str] = {}
            for name in _TRIGGERS:
                row = conn.execute(
                    "SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?",
                    (name,),
                ).fetchone()
                if row and row[0]:
                    saved[name] = row[0]
                conn.execute(f"DROP TRIGGER IF EXISTS {name}")
            fn(conn)
            for sql in saved.values():
                conn.execute(sql)
            conn.commit()
        finally:
            conn.close()

    def _legacy_schema_store(self, version: int) -> ProjectStore:
        project_id = paths.new_project_id()
        root = self.tmp / f"legacy-{version}"
        app_dir = paths.app_dir_for(str(root))
        paths.ensure_owner_private_dir(app_dir)
        db_path = os.path.join(app_dir, paths.STORE_FILENAME)
        conn = schema.open_connection(db_path)
        try:
            schema.migrate(
                conn,
                target_version=version,
                migrations=schema.STORE_MIGRATIONS,
                label="project store",
            )
            conn.execute(
                "INSERT INTO project (project_id, name, created_at_utc, app_version) "
                "VALUES (?, 'legacy', '2026-01-01T00:00:00+00:00', '0.0.0')",
                (project_id,),
            )
            conn.execute(
                "INSERT INTO meta (key, value) VALUES ('created_at_utc', ?)",
                ("2026-01-01T00:00:00+00:00",),
            )
        finally:
            conn.close()
        paths.ensure_owner_private_file(db_path)
        conn = schema.open_connection(db_path)
        store = ProjectStore(
            conn,
            app_dir,
            project_id,
            schema_version=version,
            app_version="0.0.0",
        )
        self.addCleanup(store.close)
        return store


class RoundTripTests(DiscoveryBackupBase):
    def test_fresh_backup_restores_exact_pubmed_and_epmc_observations(self) -> None:
        pubmed = self.pubmed_completed()
        epmc = self.epmc_completed()
        first = self.repo.record_search(request_id="req_pubmed", result=pubmed)
        second = self.repo.record_search(request_id="req_epmc", result=epmc)

        dest = self._backup()
        report = verify_backup(dest)
        self.assertTrue(report["ok"])
        self.assertEqual(report["store_schema_version"], schema.STORE_SCHEMA_VERSION)

        restored = self._restore(dest)
        self.assertEqual(restored.schema_version, schema.STORE_SCHEMA_VERSION)
        repo = DiscoveryRepository(restored)
        rows = {row.request_id: row for row in repo.list_searches()}
        self.assertEqual(set(rows), {"req_pubmed", "req_epmc"})

        loaded_pubmed = rows["req_pubmed"]
        self.assertEqual(loaded_pubmed.run_id, first.run_id)
        self.assertEqual(loaded_pubmed.recorded_at_utc, first.recorded_at_utc)
        self.assertEqual(loaded_pubmed.canonical, first.canonical)
        self.assertEqual(loaded_pubmed.binding, first.binding)
        self.assertEqual(loaded_pubmed.result, pubmed)
        self.assertEqual(
            loaded_pubmed.result.query_translation, pubmed.query_translation
        )

        loaded_epmc = rows["req_epmc"]
        self.assertEqual(loaded_epmc.run_id, second.run_id)
        self.assertEqual(loaded_epmc.recorded_at_utc, second.recorded_at_utc)
        self.assertEqual(loaded_epmc.canonical, second.canonical)
        self.assertEqual(loaded_epmc.result, epmc)
        self.assertEqual(loaded_epmc.source, "europepmc")

        conn = self._snapshot_conn(dest)
        try:
            self.assertEqual(
                discovery_integrity_problems(conn, restored.project_id), []
            )
        finally:
            conn.close()


class ForgedSnapshotTests(DiscoveryBackupBase):
    def test_tampered_payload_digest_projection_or_time_is_refused(self) -> None:
        record = self.repo.record_search(
            request_id="req_forged", result=self.pubmed_completed()
        )
        forged_payload = b'{"format":"unsupported"}'
        scenarios = {
            "payload": (
                "UPDATE discovery_search_run SET canonical = ?, result_sha256 = ?, "
                "result_bytes = ? WHERE project_id = ? AND run_id = ?",
                (
                    forged_payload,
                    _sha(forged_payload),
                    len(forged_payload),
                    self.project_id,
                    record.run_id,
                ),
            ),
            "digest": (
                "UPDATE discovery_search_run SET result_sha256 = ? "
                "WHERE project_id = ? AND run_id = ?",
                ("0" * 64, self.project_id, record.run_id),
            ),
            "projection": (
                "UPDATE discovery_search_run SET source = 'forged' "
                "WHERE project_id = ? AND run_id = ?",
                (self.project_id, record.run_id),
            ),
            "timestamp": (
                "UPDATE discovery_search_run SET recorded_at_utc = 'not-a-time' "
                "WHERE project_id = ? AND run_id = ?",
                (self.project_id, record.run_id),
            ),
        }
        for name, (sql, params) in scenarios.items():
            with self.subTest(scenario=name):
                dest = self.tmp / f"forged-{name}"
                create_backup(self.store, dest)
                self._mutate(dest, lambda conn: conn.execute(sql, params))
                self._rewrite_outer_db_hash(dest)

                with self.assertRaises(BackupError) as caught:
                    verify_backup(dest)
                self.assertIn(
                    "discovery search observations do not revalidate",
                    str(caught.exception),
                )
                self.assertNotIn("cancer", str(caught.exception))
                self.assertNotIn("unsupported", str(caught.exception))

    def test_foreign_row_is_refused_by_the_shared_backup_hook(self) -> None:
        self.repo.record_search(request_id="req_foreign", result=self.pubmed_completed())
        dest = self._backup("foreign-backup")
        conn = self._snapshot_conn(dest)
        try:
            with self.assertRaises(BackupError) as caught:
                backup_mod._verify_discovery_relations(conn, "prj_other")
        finally:
            conn.close()
        self.assertIn("foreign project row", str(caught.exception))
        self.assertNotIn("cancer", str(caught.exception))


class CompatibilityTests(DiscoveryBackupBase):
    def test_schema10_snapshot_without_discovery_table_remains_supported(self) -> None:
        legacy = self._legacy_schema_store(10)
        tables = {
            row["name"]
            for row in legacy.connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        self.assertNotIn("discovery_search_run", tables)

        dest = self.tmp / "legacy-backup"
        report = create_backup(legacy, dest)
        self.assertEqual(report["store_schema_version"], 10)
        verified = verify_backup(dest)
        self.assertTrue(verified["ok"])
        self.assertEqual(verified["store_schema_version"], 10)

        restored = self._restore(dest, "restored-legacy")
        self.assertEqual(restored.schema_version, schema.STORE_SCHEMA_VERSION)
        self.assertEqual(DiscoveryRepository(restored).list_searches(), [])
        self.assertEqual(
            discovery_integrity_problems(restored.connection, restored.project_id), []
        )

    def test_hook_table_is_ordered_and_schema11_is_current(self) -> None:
        minimums = [minimum for minimum, _ in backup_mod._SCHEMA_VALIDATORS]
        self.assertEqual(minimums, sorted(minimums))
        self.assertEqual(len(minimums), len(set(minimums)))
        self.assertEqual(schema.STORE_SCHEMA_VERSION, 11)
        self.assertEqual(backup_mod._DISCOVERY_SCHEMA_VERSION, 11)
        self.assertEqual(minimums[-1], backup_mod._DISCOVERY_SCHEMA_VERSION)
        self.assertEqual(
            backup_mod.SUPPORTED_STORE_SCHEMA_VERSION, schema.STORE_SCHEMA_VERSION
        )
        self.assertEqual(
            backup_mod.SUPPORTED_SNAPSHOT_SCHEMA_VERSIONS,
            frozenset(range(3, schema.STORE_SCHEMA_VERSION + 1)),
        )
        validators = [validator for _, validator in backup_mod._SCHEMA_VALIDATORS]
        self.assertIs(validators[-1], backup_mod._verify_discovery_relations)
        self.assertIn(10, minimums)
        self.assertIs(
            validators[minimums.index(10)],
            backup_mod._verify_session_intent_relations,
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()