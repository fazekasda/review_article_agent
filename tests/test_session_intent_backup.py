"""Backup/verify/restore coverage for the schema-10 session-turn intent.

These tests extend the private backup contract in
``src/review_article_agent/storage/backup.py`` to the schema-10 immutable
pre-transport session-turn correlation (``storage/session_intents.py``). They
reuse the small persisted fixture in ``tests/test_session_intents.py`` (the same
real project/task/session-mapping/dispatch scenario) rather than duplicating a
large local fixture:

* a reserved, submitted, and unknown dispatch round-trips its intent exactly:
  the canonical plan/message bytes, spec/message digests, session/message/
  admission ids, mapping/task/dispatch refs, and created time are unchanged;
* a moved target head and a read-only raw snapshot connection verify without a
  live service or a live application path;
* a tampered canonical, a tampered projected column, and a tampered message are
  refused by the schema-10 inner checker even after the outer archive database
  hash is recomputed; the dropped intent triggers are restored exactly and the
  canonical schema is asserted before the inner check;
* an orphan (missing dispatch) row and a foreign-project row are detected;
* a schema-9 snapshot without the ``session_turn_intent`` table still verifies,
  so the schema-10 hook is threshold-gated;
* the hook table stays ordered and keyed by minimum version, with the existing
  schema-4..9 validators unchanged.

The intent canonical/message bytes are private application text and may contain
caller-submitted source text. This module is not a secret scanner and makes no
"never contains source" promise; the public manifest and static verification
errors carry none of that text. Synthetic data only: no network, no credential,
no model call, no paid call.

Run::

    PYTHONPATH=src:tests python3 -m unittest test_session_intent_backup
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
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
TESTS = Path(__file__).resolve().parent
if str(TESTS) not in sys.path:
    sys.path.insert(0, str(TESTS))

from review_article_agent.runtime.dispatch import (  # noqa: E402
    RuntimeDispatchRepository,
)
from review_article_agent.storage import (  # noqa: E402
    ArtifactRepository,
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
from review_article_agent.storage.session_intents import (  # noqa: E402
    SessionIntentRepository,
    session_intent_integrity_problems,
)
from test_session_intents import SessionIntentTestBase  # noqa: E402
from test_session_turns import _rights  # noqa: E402

_INTENT_TRIGGERS = (
    "session_turn_intent_no_update",
    "session_turn_intent_no_delete",
)


def _sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


class SessionIntentBackupBase(SessionIntentTestBase):
    """The shared persisted intent fixture plus backup/restore helpers."""

    def _backup(self, scenario, name: str = "backup") -> Path:
        dest = self.tmp / name
        create_backup(scenario.store, dest)
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
        """Recompute the manifest's database hash (a self-consistent outer archive)."""
        manifest_path = dest / MANIFEST_FILENAME
        manifest = json.loads(manifest_path.read_text())
        data = (dest / SNAPSHOT_FILENAME).read_bytes()
        manifest["database"]["sha256"] = _sha256(data)
        manifest["database"]["byte_size"] = len(data)
        manifest_path.write_text(json.dumps(manifest))

    def _mutate(self, dest: Path, fn) -> None:
        """Run ``fn(conn)`` with the intent triggers dropped and then restored.

        The trigger SQL is saved from ``sqlite_master`` before dropping and replayed
        exactly afterwards, so the canonical schema comparison still passes and a
        refusal can only come from the inner checker, not from a missing trigger.
        """
        conn = sqlite3.connect(str(dest / SNAPSHOT_FILENAME))
        conn.row_factory = sqlite3.Row
        try:
            saved: dict[str, str] = {}
            for name in _INTENT_TRIGGERS:
                row = conn.execute(
                    "SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?",
                    (name,),
                ).fetchone()
                if row and row[0]:
                    saved[name] = row[0]
                conn.execute(f"DROP TRIGGER IF EXISTS {name}")
            fn(conn)
            for name, sql in saved.items():
                conn.execute(sql)
            conn.commit()
        finally:
            conn.close()

    def _assert_inner_hook_accepts(self, dest: Path, project_id: str) -> None:
        conn = self._snapshot_conn(dest)
        try:
            actual_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
            backup_mod._assert_known_schema(conn, actual_version)
            self.assertEqual(
                session_intent_integrity_problems(conn, project_id), []
            )
            backup_mod._verify_session_intent_relations(conn, project_id)
        finally:
            conn.close()

    def _assert_hook_rejects(self, dest: Path, *, expect_static_message: bool = True):
        conn = self._snapshot_conn(dest)
        try:
            # The tamper dropped and exactly restored the intent triggers, so the
            # canonical schema comparison must still pass. The expected current
            # version comes from the snapshot's own PRAGMA, not a hardcoded value.
            actual_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
            self.assertEqual(actual_version, schema.STORE_SCHEMA_VERSION)
            backup_mod._assert_known_schema(conn, actual_version)
            with self.assertRaises(BackupError) as caught:
                backup_mod._verify_session_intent_relations(conn, self.pid_for(conn))
            self.assertIn("session-turn intents do not revalidate", str(caught.exception))
            self.assertNotIn("Draft the pinned", str(caught.exception))
        finally:
            conn.close()
        self._rewrite_outer_db_hash(dest)
        with self.assertRaises(BackupError) as caught:
            verify_backup(dest)
        if expect_static_message:
            self.assertIn("session-turn intents do not revalidate", str(caught.exception))
        else:
            # A foreign/orphan row also trips the generic foreign-key check before
            # the inner hook; the direct call above already proved the inner checker.
            self.assertIn("foreign_key", str(caught.exception).lower())
        return caught.exception

    @staticmethod
    def pid_for(conn: sqlite3.Connection) -> str:
        return str(conn.execute("SELECT project_id FROM project").fetchone()[0])


class RoundTripStatesTest(SessionIntentBackupBase):
    def _assert_exact_intent(self, restored: ProjectStore, spec, record) -> None:
        reloaded = SessionIntentRepository(restored).get_intent(spec.dispatch_id)
        self.assertIsNotNone(reloaded)
        self.assertEqual(reloaded.canonical, spec.canonical)
        self.assertEqual(reloaded.message, spec.message)
        self.assertEqual(reloaded.spec_digest, spec.digest)
        self.assertEqual(reloaded.message_digest, spec.message_digest)
        self.assertEqual(reloaded.message_bytes, len(spec.message))
        self.assertEqual(reloaded.admission_id, spec.admission_id)
        self.assertEqual(reloaded.session_id, spec.session_id)
        self.assertEqual(reloaded.mapping_id, spec.mapping_id)
        self.assertEqual(reloaded.task_id, spec.task_id)
        self.assertEqual(reloaded.dispatch_id, spec.dispatch_id)
        self.assertEqual(reloaded.fence, record.fence)
        self.assertEqual(reloaded.schema_version, record.schema_version)
        self.assertEqual(reloaded.created_at_utc, record.created_at_utc)
        rebuilt = reloaded.spec()
        self.assertEqual(rebuilt.canonical, spec.canonical)
        self.assertEqual(rebuilt.message, spec.message)
        self.assertEqual(rebuilt.admission_id, spec.admission_id)
        self.assertEqual(
            session_intent_integrity_problems(restored.connection, restored.project_id),
            [],
        )

    def test_reserved_submitted_unknown_roundtrip_exactly(self) -> None:
        for state in ("reserved", "submitted", "unknown"):
            with self.subTest(state=state):
                scenario = self._scenario()
                spec = self._prepare(scenario)
                record = SessionIntentRepository(scenario.store).record_intent(
                    spec, fence=1
                )
                if state == "submitted":
                    scenario.dispatch_repo.mark_submitted(spec.dispatch_id)
                elif state == "unknown":
                    scenario.dispatch_repo.mark_submitted(spec.dispatch_id)
                    scenario.dispatch_repo.recover(spec.dispatch_id)
                self.assertEqual(
                    scenario.dispatch_repo.get_dispatch(spec.dispatch_id).state, state
                )

                dest = self._backup(scenario, f"backup_{state}")
                report = verify_backup(dest)
                self.assertTrue(report["ok"])
                self.assertEqual(report["project_id"], scenario.store.project_id)
                conn = self._snapshot_conn(dest)
                try:
                    actual_version = int(
                        conn.execute("PRAGMA user_version").fetchone()[0]
                    )
                finally:
                    conn.close()
                # The summary reports the snapshot's actual recorded schema.
                self.assertEqual(report["store_schema_version"], actual_version)
                self._assert_inner_hook_accepts(dest, scenario.store.project_id)

                restored = self._restore(dest, f"restored_{state}")
                self.assertEqual(restored.project_id, scenario.store.project_id)
                self.assertEqual(
                    restored.connection.execute(
                        "PRAGMA user_version"
                    ).fetchone()[0],
                    schema.STORE_SCHEMA_VERSION,
                )
                self._assert_exact_intent(restored, spec, record)
                self.assertEqual(
                    RuntimeDispatchRepository(restored)
                    .get_dispatch(spec.dispatch_id)
                    .state,
                    state,
                )
                self.assertEqual(
                    restored.connection.execute(
                        "SELECT COUNT(*) FROM session_turn_intent"
                    ).fetchone()[0],
                    1,
                )


class HistoryAndReadOnlyTest(SessionIntentBackupBase):
    def test_moved_head_and_raw_snapshot_need_no_live_service_or_path(self) -> None:
        scenario = self._scenario()
        spec = self._prepare(scenario)
        record = SessionIntentRepository(scenario.store).record_intent(spec, fence=1)

        # Move the target artifact's head after the intent was recorded. The intent
        # remains honest history and is not rewritten to the new head.
        ArtifactRepository(scenario.store).import_revision(
            artifact_id=scenario.target_artifact.artifact_id,
            content=b"a-newer-target-head",
            origin_locator="import:synthetic",
            rights=_rights(
                scenario.store.project_id,
                scenario.target_artifact.artifact_id,
                "rev_2",
            ),
            revision_id="rev_2",
            parent_revision_id="rev_1",
        )
        self.assertEqual(
            ArtifactRepository(scenario.store)
            .current_revision(scenario.target_artifact.artifact_id)
            .revision_id,
            "rev_2",
        )

        dest = self._backup(scenario, "backup_moved_head")
        # The raw read-only connection is the only "store": no manager, service,
        # application path, or ProjectStore is required for the inner check.
        self._assert_inner_hook_accepts(dest, scenario.store.project_id)
        self.assertTrue(verify_backup(dest)["ok"])

        restored = self._restore(dest, "restored_moved_head")
        reloaded = SessionIntentRepository(restored).get_intent(spec.dispatch_id)
        self.assertIsNotNone(reloaded)
        self.assertEqual(reloaded.canonical, spec.canonical)
        self.assertEqual(reloaded.message, spec.message)
        self.assertEqual(reloaded.spec().to_mapping(), spec.to_mapping())
        self.assertEqual(reloaded.created_at_utc, record.created_at_utc)


class TamperRefusalTest(SessionIntentBackupBase):
    def _backup_fixture(self):
        scenario = self._scenario()
        spec = self._prepare(scenario)
        record = SessionIntentRepository(scenario.store).record_intent(spec, fence=1)
        dest = self._backup(scenario, "backup_tamper")
        self.assertTrue(verify_backup(dest)["ok"])
        return scenario, spec, record, dest

    def test_tampered_canonical_is_refused(self) -> None:
        scenario, spec, _record, dest = self._backup_fixture()

        def tamper(conn):
            row = conn.execute(
                "SELECT canonical FROM session_turn_intent"
            ).fetchone()
            changed = bytes(row["canonical"]) + b" "
            conn.execute(
                "UPDATE session_turn_intent SET canonical = ?, spec_digest = ?",
                (changed, _sha256(changed)),
            )

        self._mutate(dest, tamper)
        caught = self._assert_hook_rejects(dest)
        self.assertNotIn(spec.canonical.decode("utf-8"), str(caught))

    def test_tampered_column_is_refused(self) -> None:
        _scenario, _spec, _record, dest = self._backup_fixture()

        def tamper(conn):
            conn.execute(
                "UPDATE session_turn_intent SET session_id = 'ses_tampered'"
            )

        self._mutate(dest, tamper)
        self._assert_hook_rejects(dest)

    def test_tampered_message_is_refused(self) -> None:
        _scenario, _spec, _record, dest = self._backup_fixture()

        def tamper(conn):
            row = conn.execute(
                "SELECT message FROM session_turn_intent"
            ).fetchone()
            changed = bytes(row["message"]) + b" "
            conn.execute(
                "UPDATE session_turn_intent SET message = ?, message_digest = ?, "
                "message_bytes = ?",
                (changed, _sha256(changed), len(changed)),
            )

        self._mutate(dest, tamper)
        self._assert_hook_rejects(dest)


class OrphanAndForeignTest(SessionIntentBackupBase):
    def _backup_fixture(self):
        scenario = self._scenario()
        spec = self._prepare(scenario)
        SessionIntentRepository(scenario.store).record_intent(spec, fence=1)
        dest = self._backup(scenario, "backup_orphan_foreign")
        self.assertTrue(verify_backup(dest)["ok"])
        return scenario, spec, dest

    def test_orphan_missing_dispatch_is_refused(self) -> None:
        scenario, _spec, dest = self._backup_fixture()

        def tamper(conn):
            conn.execute(
                "INSERT INTO session_turn_intent "
                "SELECT ?, ?, ?, schema_version, fence, ?, session_id, mapping_id, "
                "service_instance_id, task_id, task_fingerprint, role, model, "
                "prompt_version, input_digest, version_digest, config_snapshot_id, "
                "config_digest, spec_digest, message_digest, message_bytes, "
                "canonical, message, created_at_utc FROM session_turn_intent",
                (
                    scenario.store.project_id,
                    "disp_missing_orphan",
                    "sint_orphan_placeholder",
                    "msg_orphan_admission_placeholder",
                ),
            )

        self._mutate(dest, tamper)
        self._assert_hook_rejects(dest, expect_static_message=False)

    def test_foreign_project_row_is_refused(self) -> None:
        _scenario, _spec, dest = self._backup_fixture()

        def tamper(conn):
            conn.execute(
                "INSERT INTO session_turn_intent "
                "SELECT ?, dispatch_id, ?, schema_version, fence, ?, session_id, "
                "mapping_id, service_instance_id, task_id, task_fingerprint, role, "
                "model, prompt_version, input_digest, version_digest, "
                "config_snapshot_id, config_digest, spec_digest, message_digest, "
                "message_bytes, canonical, message, created_at_utc "
                "FROM session_turn_intent",
                (
                    "prj_foreign_placeholder",
                    "sint_foreign_placeholder",
                    "msg_foreign_admission_placeholder",
                ),
            )

        self._mutate(dest, tamper)
        self._assert_hook_rejects(dest, expect_static_message=False)


class SchemaCompatibilityTest(SessionIntentBackupBase):
    def _schema_store(self, version: int, name: str) -> ProjectStore:
        """Create a store pinned at an older schema (no later tables)."""
        app_dir = str(self.tmp / name)
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
            project_id = paths.new_project_id()
            conn.execute(
                "INSERT INTO project (project_id, name, created_at_utc, app_version) "
                "VALUES (?, ?, '2026-01-01T00:00:00+00:00', '0.0.0')",
                (project_id, f"schema{version}"),
            )
            conn.execute(
                "INSERT INTO meta (key, value) VALUES ('created_at_utc', ?)",
                ("2026-01-01T00:00:00+00:00",),
            )
        except BaseException:
            conn.close()
            raise
        paths.ensure_owner_private_file(db_path)
        store = ProjectStore(
            conn, app_dir, project_id, schema_version=version, app_version="0.0.0"
        )
        self.addCleanup(store.close)
        return store

    def test_schema9_snapshot_verifies_without_schema10_hook(self) -> None:
        store = self._schema_store(9, "p9")
        tables = {
            row["name"]
            for row in store.connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        # If the schema-10 hook ran at 9 it would fail on this missing table, so a
        # successful verify proves the hook is threshold-gated.
        self.assertNotIn("session_turn_intent", tables)

        dest = self.tmp / "backup9"
        create_backup(store, dest)
        report = verify_backup(dest)
        self.assertTrue(report["ok"])
        self.assertEqual(report["store_schema_version"], 9)

        restored = self._restore(dest, "restored9")
        self.assertEqual(restored.schema_version, schema.STORE_SCHEMA_VERSION)


class DynamicHookTest(SessionIntentBackupBase):
    def test_hook_table_is_ordered_and_existing_hooks_are_unchanged(self) -> None:
        minimums = [minimum for minimum, _ in backup_mod._SCHEMA_VALIDATORS]
        self.assertEqual(minimums, sorted(minimums))
        self.assertEqual(len(minimums), len(set(minimums)))
        self.assertEqual(
            minimums[:6], [4, 5, 6, 7, 8, backup_mod._DECISION_RESULT_SCHEMA_VERSION]
        )
        self.assertEqual(minimums[-1], schema.STORE_SCHEMA_VERSION)
        self.assertEqual(backup_mod._SESSION_INTENT_SCHEMA_VERSION, 10)
        self.assertIn(backup_mod._SESSION_INTENT_SCHEMA_VERSION, minimums)
        validators = [validator for _, validator in backup_mod._SCHEMA_VALIDATORS]
        self.assertTrue(all(callable(validator) for validator in validators))
        self.assertEqual(len(validators), len(set(validators)))
        self.assertIs(
            validators[minimums.index(backup_mod._SESSION_INTENT_SCHEMA_VERSION)],
            backup_mod._verify_session_intent_relations,
        )
        # A later additive checkpoint appends its hook without moving schema-10.
        self.assertIs(validators[-1], backup_mod._verify_discovery_relations)
        self.assertGreater(minimums[-1], backup_mod._SESSION_INTENT_SCHEMA_VERSION)
        # The pre-existing schema-4..9 entries are untouched.
        self.assertEqual(
            validators[:6],
            [
                backup_mod._verify_job_relations,
                backup_mod._verify_runtime_relations,
                backup_mod._verify_dispatch_relations,
                backup_mod._verify_human_approval_relations,
                backup_mod._verify_intent_relations,
                backup_mod._verify_decision_result_relations,
            ],
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
