"""Backup/verify/restore coverage for the schema-5 durable runtime mappings (T8a).

These tests extend the private backup contract to the durable runtime
configuration/session/task/proposal tables added by store schema 5
(``storage/agent_runtime.py``):

* a schema-5 backup roundtrips configuration snapshots, two independent session
  mappings, a bounded task, and its non-authorizing proposal exactly, while
  leaving the existing artifact/fixture-approval rows unchanged;
* two saved projects stay isolated: a backup of one project contains no runtime
  row for the other;
* the schema-5 runtime rows are inner-validated **read-only**, so a corrupt
  archive whose outer database hash was rewritten still fails closed — a foreign
  project key in **every** runtime table, a corrupted canonical/digest/projected
  column, a broken config/task/input/proposal association, and a proposal
  canonical that claims in-band authority;
* a corrupt backup is rejected before any restore registration and leaves no
  partial restore;
* schema-3 and schema-4 snapshots remain verifiable (format-1 compatibility), and
  the schema-validator hook is additive for a future schema beyond the current
  one.

Synthetic data only (no secrets, no licensed source text, no model call).

Run with::

    PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_runtime_backup.py'
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from review_article_agent.domain.records import canonical_json  # noqa: E402
from review_article_agent.runtime.roles import (  # noqa: E402
    BoundedTask,
    RoleConfiguration,
)
from review_article_agent.storage import (  # noqa: E402
    ApprovalRepository,
    ArtifactRepository,
    ProjectStore,
    Registry,
)
from review_article_agent.storage import paths, schema  # noqa: E402
from review_article_agent.storage import backup as backup_mod  # noqa: E402
from review_article_agent.storage.agent_runtime import (  # noqa: E402
    RuntimeRepository,
)
from review_article_agent.storage.backup import (  # noqa: E402
    MANIFEST_FILENAME,
    SNAPSHOT_FILENAME,
    BackupError,
    create_backup,
    restore_backup,
    verify_backup,
)

_OPS = ("acquisition", "storage", "redistribution", "hosted_transfer")
_MODEL_REF = "openrouter/anthropic/claude-sonnet-5-5#high"


def _sha(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _bare(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _hexlit(data: bytes) -> str:
    """A SQLite ``X'..'`` blob literal."""
    return "X'" + data.hex() + "'"


def _rights(project_id: str, artifact_id: str, revision_id: str) -> dict:
    """A synthetic, fully valid RightsRecord v1 bound to one revision."""
    return {
        "rights_id": f"RIGHTS-{artifact_id}",
        "project_id": project_id,
        "record_revision": 1,
        "subject": {
            "kind": "source_revision",
            "project_id": project_id,
            "artifact_id": artifact_id,
            "revision_id": revision_id,
        },
        "operations": {op: "allowed" for op in _OPS},
        "basis": {
            op: {
                "kind": "public_domain",
                "evidence": {"ref": "lic:pd-syn", "provenance": "publisher:pd-list"},
            }
            for op in _OPS
        },
        "authorization": None,
        "restrictions": [],
        "privacy": {
            "personal_data": False,
            "confidentiality": "public",
            "denial": False,
        },
        "recorded_by": "user",
        "recorded_at": "2026-10-27T00:00:00Z",
        "derived_status": "valid",
    }


def _role_model(
    model: str = "anthropic/claude-sonnet-5-5", variant: str | None = "high"
) -> dict:
    spec = {"provider_id": "openrouter", "model_id": model, "steps": 8}
    if variant is not None:
        spec["variant"] = variant
    return spec


def _config() -> RoleConfiguration:
    return RoleConfiguration.from_mapping(
        {
            "schema_version": 1,
            "roles": {
                "writer": _role_model(),
                "section_critic": _role_model(),
            },
        }
    )


class RuntimeBackupBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="raa-runtime-backup-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    # -- fixtures ------------------------------------------------------------ #
    def new_store(self, name: str = "p") -> ProjectStore:
        return ProjectStore.create_at(
            paths.app_dir_for(str(self.tmp / name)), paths.new_project_id()
        )

    def new_schema_store(self, version: int, name: str) -> ProjectStore:
        """Create a store pinned at an older store schema (no runtime tables)."""
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
        return ProjectStore(
            conn, app_dir, project_id, schema_version=version, app_version="0.0.0"
        )

    def seed_revision(self, store: ProjectStore, content: bytes = b"runtime-source-bytes"):
        repo = ArtifactRepository(store)
        artifact = repo.create_artifact(kind="document")
        repo.import_revision(
            artifact_id=artifact.artifact_id,
            content=content,
            origin_locator="import:synthetic",
            rights=_rights(store.project_id, artifact.artifact_id, "rev_1"),
            revision_id="rev_1",
        )
        approval = ApprovalRepository(store).record_fixture_approval(
            target={
                "project_id": store.project_id,
                "artifact_id": artifact.artifact_id,
                "revision_id": "rev_1",
            },
            gate_kind="fixture_artifact_revision",
            gate_id="gate-doc",
            decision="approved",
            rationale="synthetic local fixture; not a scientific approval",
        )
        return artifact, approval

    def make_task(self, store: ProjectStore, artifact):
        cfg = _config()
        mapping = {
            "project_id": store.project_id,
            "role": "writer",
            "target": {
                "project_id": store.project_id,
                "artifact_id": artifact.artifact_id,
                "revision_id": "rev_1",
            },
            "input_refs": [
                {
                    "project_id": store.project_id,
                    "artifact_id": artifact.artifact_id,
                    "revision_id": "rev_1",
                    "digest": _bare(b"runtime-source-bytes"),
                }
            ],
            "scope": ["section:introduction"],
            "prompt_version": "writer.v1",
            "size_limit": 4096,
            "step_limit": 4,
        }
        return BoundedTask.from_mapping(mapping, configuration=cfg), cfg

    def seed_runtime(self, store: ProjectStore) -> dict:
        """Persist a config, two session mappings, a task, and its proposal."""
        artifact, approval = self.seed_revision(store)
        cfg = _config()
        runtime = RuntimeRepository(store)
        snapshot = runtime.record_configuration(cfg)
        writer = runtime.record_session_mapping(
            service_instance_id="svc_1",
            opencode_project_id="oc_1",
            clean_location=str(self.tmp / "clean"),
            session_id="ses_writer_1",
            role="writer",
            model=_MODEL_REF,
            configuration=cfg,
        )
        critic = runtime.record_session_mapping(
            service_instance_id="svc_1",
            opencode_project_id="oc_1",
            clean_location=str(self.tmp / "clean"),
            session_id="ses_critic_1",
            role="section_critic",
            model=_MODEL_REF,
            configuration=cfg,
        )
        task, task_cfg = self.make_task(store, artifact)
        record = runtime.record_task(task, request_id="req-1", configuration=task_cfg)
        proposal = runtime.record_proposal(
            record, {"content": {"finding": "synthetic"}}, configuration=task_cfg
        )
        return {
            "artifact": artifact,
            "approval": approval,
            "config": snapshot,
            "writer": writer,
            "critic": critic,
            "task": record,
            "proposal": proposal,
        }

    # -- tamper helpers ------------------------------------------------------ #
    def rewrite_outer_db_hash(self, backup_dir: Path) -> None:
        """Recompute the manifest's database hash (simulates a rewritten hash)."""
        manifest_path = backup_dir / MANIFEST_FILENAME
        manifest = json.loads(manifest_path.read_text())
        data = (backup_dir / SNAPSHOT_FILENAME).read_bytes()
        manifest["database"]["sha256"] = _sha(data)
        manifest["database"]["byte_size"] = len(data)
        manifest_path.write_text(json.dumps(manifest))

    def tamper_db(self, backup_dir: Path, *statements: str) -> None:
        """Execute raw SQL against the snapshot, recreating any dropped trigger.

        Recreating a trigger the statements drop keeps the snapshot's schema
        identical, so the tamper exercises inner digest/link validation rather
        than merely a missing schema object.
        """
        db = str(backup_dir / SNAPSHOT_FILENAME)
        conn = sqlite3.connect(db)
        try:
            dropped: dict[str, str] = {}
            for statement in statements:
                match = re.match(
                    r"\s*DROP\s+TRIGGER\s+(?:IF\s+EXISTS\s+)?(\S+)\s*;?\s*$",
                    statement,
                    re.IGNORECASE,
                )
                if match:
                    row = conn.execute(
                        "SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?",
                        (match.group(1),),
                    ).fetchone()
                    if row and row[0]:
                        dropped[match.group(1)] = row[0]
                conn.execute(statement)
            for sql in dropped.values():
                conn.execute(sql)
            conn.commit()
        finally:
            conn.close()

    def _snapshot_conn(self, backup_dir: Path) -> sqlite3.Connection:
        conn = sqlite3.connect(str(backup_dir / SNAPSHOT_FILENAME))
        conn.row_factory = sqlite3.Row
        return conn

    def _assert_rewritten_hash_rejected(self, dest: Path) -> None:
        self.rewrite_outer_db_hash(dest)
        with self.assertRaises(BackupError):
            verify_backup(dest)


class RuntimeRoundTripTest(RuntimeBackupBase):
    def test_roundtrip_preserves_runtime_mappings_and_approvals(self) -> None:
        store = self.new_store()
        self.addCleanup(store.close)
        seeded = self.seed_runtime(store)
        dest = self.tmp / "backup"
        create_backup(store, dest)
        report = verify_backup(dest)
        self.assertTrue(report["ok"])
        self.assertEqual(report["project_id"], store.project_id)

        registry = Registry(self.tmp / "state")
        target = self.tmp / "restored"
        target.mkdir()
        info = restore_backup(dest, target, registry)
        self.assertEqual(info.project_id, store.project_id)

        restored = ProjectStore.open_at(
            paths.app_dir_for(str(target)), expected_project_id=info.project_id
        )
        self.addCleanup(restored.close)

        # Existing artifact + fixture approval are unchanged by the schema-5 path.
        approvals = ApprovalRepository(restored)
        self.assertEqual(
            {a.approval_id for a in approvals.list_approvals()},
            {seeded["approval"].approval_id},
        )
        self.assertTrue(approvals.verify_snapshot(seeded["approval"].approval_id).ok)

        runtime = RuntimeRepository(restored)
        self.assertEqual(
            [c.config_digest for c in runtime.list_configurations()],
            [seeded["config"].config_digest],
        )
        mappings = {
            m.session_id: (m.role, m.model_ref) for m in runtime.list_session_mappings()
        }
        self.assertEqual(
            mappings,
            {
                "ses_writer_1": ("writer", _MODEL_REF),
                "ses_critic_1": ("section_critic", _MODEL_REF),
            },
        )
        self.assertEqual(
            runtime.get_task_by_request("req-1").to_mapping(),
            seeded["task"].to_mapping(),
        )
        self.assertEqual(
            [p.to_mapping() for p in runtime.list_proposals()],
            [seeded["proposal"].to_mapping()],
        )

    def test_two_project_mappings_stay_isolated(self) -> None:
        first = self.new_store("p1")
        self.addCleanup(first.close)
        second = self.new_store("p2")
        self.addCleanup(second.close)
        seed_first = self.seed_runtime(first)
        seed_second = self.seed_runtime(second)

        dest1 = self.tmp / "backup1"
        dest2 = self.tmp / "backup2"
        create_backup(first, dest1)
        create_backup(second, dest2)
        self.assertTrue(verify_backup(dest1)["ok"])
        self.assertTrue(verify_backup(dest2)["ok"])

        # Each snapshot's runtime tables hold only its own project's rows.
        for dest, own, other in (
            (dest1, seed_first, seed_second),
            (dest2, seed_second, seed_first),
        ):
            conn = self._snapshot_conn(dest)
            try:
                for table in backup_mod._RUNTIME_TABLES:
                    foreign = conn.execute(
                        f"SELECT COUNT(*) AS n FROM {table} WHERE project_id = ?",
                        (other["config"].project_id,),
                    ).fetchone()["n"]
                    self.assertEqual(foreign, 0, table)
                    present = conn.execute(
                        f"SELECT COUNT(*) AS n FROM {table} WHERE project_id = ?",
                        (own["config"].project_id,),
                    ).fetchone()["n"]
                    self.assertGreater(present, 0, table)
            finally:
                conn.close()

        # Restoring one project never imports the other's mapping.
        registry = Registry(self.tmp / "state")
        target = self.tmp / "restored1"
        target.mkdir()
        info = restore_backup(dest1, target, registry)
        restored = ProjectStore.open_at(
            paths.app_dir_for(str(target)), expected_project_id=info.project_id
        )
        self.addCleanup(restored.close)
        runtime = RuntimeRepository(restored)
        self.assertEqual(len(runtime.list_session_mappings()), 2)
        self.assertEqual(
            {m.session_id for m in runtime.list_session_mappings()},
            {"ses_writer_1", "ses_critic_1"},
        )

    def test_foreign_project_runtime_row_rejected_in_every_table(self) -> None:
        """The explicit scan refuses a foreign key in all five runtime tables.

        The scan is exercised directly on a read-only snapshot connection so the
        assertion isolates the explicit project check from the generic
        ``foreign_key_check`` (which could otherwise mask a dangling foreign row).
        """
        store = self.new_store()
        self.addCleanup(store.close)
        self.seed_runtime(store)

        digest = _bare(b"{}")
        inserts = {
            "runtime_config_snapshot": (
                "INSERT INTO runtime_config_snapshot (project_id, snapshot_id, "
                "config_version, config_digest, canonical, created_at_utc) "
                f"VALUES ('proj_foreign', 'rcfg_x', 1, '{digest}', "
                f"{_hexlit(b'{}')}, '2026-01-01T00:00:00+00:00')"
            ),
            "runtime_session_mapping": (
                "INSERT INTO runtime_session_mapping (project_id, mapping_id, "
                "service_instance_id, opencode_project_id, clean_location, session_id, "
                "role, provider_id, model_id, variant, config_digest, "
                "config_snapshot_id, created_at_utc) "
                "VALUES ('proj_foreign', 'rmap_x', 'svc', NULL, '/tmp/clean', "
                f"'ses_foreign', 'writer', 'openrouter', 'm', NULL, '{digest}', "
                "'rcfg_x', '2026-01-01T00:00:00+00:00')"
            ),
            "runtime_bounded_task": (
                "INSERT INTO runtime_bounded_task (project_id, task_id, request_id, "
                "fingerprint, role, config_digest, config_snapshot_id, "
                "target_artifact_id, target_revision_id, prompt_version, size_limit, "
                "step_limit, canonical, created_at_utc) "
                f"VALUES ('proj_foreign', 'rtask_x', 'req-foreign', '{digest}', "
                f"'writer', '{digest}', 'rcfg_x', 'art_x', 'rev_x', 'writer.v1', 1, 1, "
                f"{_hexlit(b'{}')}, '2026-01-01T00:00:00+00:00')"
            ),
            "runtime_task_input": (
                "INSERT INTO runtime_task_input (project_id, task_id, ordinal, "
                "artifact_id, revision_id, digest) "
                f"VALUES ('proj_foreign', 'rtask_x', 0, 'art_x', 'rev_x', '{digest}')"
            ),
            "runtime_proposal": (
                "INSERT INTO runtime_proposal (project_id, record_id, proposal_id, "
                "task_id, role, provider_id, model_id, variant, target_artifact_id, "
                "target_revision_id, task_digest, input_digest, payload_digest, "
                "stored_digest, canonical, created_at_utc) "
                f"VALUES ('proj_foreign', 'prop_x', '{'a' * 64}', 'rtask_x', 'writer', "
                f"'openrouter', 'm', NULL, 'art_x', 'rev_x', '{digest}', '{digest}', "
                f"'{digest}', '{digest}', {_hexlit(b'{}')}, "
                "'2026-01-01T00:00:00+00:00')"
            ),
        }
        self.assertEqual(set(inserts), set(backup_mod._RUNTIME_TABLES))

        for table, statement in inserts.items():
            with self.subTest(table=table):
                isolated = self.tmp / f"backup-{table}"
                create_backup(store, isolated)
                self.tamper_db(isolated, statement)
                snapshot = self._snapshot_conn(isolated)
                try:
                    with self.assertRaises(BackupError):
                        backup_mod._verify_runtime_relations(snapshot, store.project_id)
                finally:
                    snapshot.close()


class RuntimeFailClosedTest(RuntimeBackupBase):
    def _backup_with_runtime(self):
        store = self.new_store()
        self.addCleanup(store.close)
        seeded = self.seed_runtime(store)
        dest = self.tmp / "backup"
        create_backup(store, dest)
        return store, seeded, dest

    def test_config_canonical_corruption_rejected(self) -> None:
        _, _, dest = self._backup_with_runtime()
        self.tamper_db(
            dest,
            "DROP TRIGGER runtime_config_snapshot_no_update",
            f"UPDATE runtime_config_snapshot SET canonical = {_hexlit(b'tampered')}",
        )
        self._assert_rewritten_hash_rejected(dest)

    def test_session_model_projection_corruption_rejected(self) -> None:
        _, _, dest = self._backup_with_runtime()
        self.tamper_db(
            dest,
            "DROP TRIGGER runtime_session_mapping_no_update",
            "UPDATE runtime_session_mapping SET model_id = 'gpt-6.1-sol'",
        )
        self._assert_rewritten_hash_rejected(dest)

    def test_task_fingerprint_corruption_rejected(self) -> None:
        _, _, dest = self._backup_with_runtime()
        self.tamper_db(
            dest,
            "DROP TRIGGER runtime_bounded_task_no_update",
            "UPDATE runtime_bounded_task SET fingerprint = '" + "b" * 64 + "'",
        )
        self._assert_rewritten_hash_rejected(dest)

    def test_task_config_link_corruption_rejected(self) -> None:
        _, _, dest = self._backup_with_runtime()
        # Keep the FK target valid but break the digest link to the snapshot.
        self.tamper_db(
            dest,
            "DROP TRIGGER runtime_bounded_task_no_update",
            "UPDATE runtime_bounded_task SET config_digest = '" + "c" * 64 + "'",
        )
        self._assert_rewritten_hash_rejected(dest)

    def test_task_input_corruption_rejected(self) -> None:
        _, _, dest = self._backup_with_runtime()
        self.tamper_db(
            dest,
            "DROP TRIGGER runtime_task_input_no_update",
            "UPDATE runtime_task_input SET digest = '" + "d" * 64 + "'",
        )
        self._assert_rewritten_hash_rejected(dest)

    def test_proposal_canonical_authority_rejected(self) -> None:
        _, _, dest = self._backup_with_runtime()
        conn = self._snapshot_conn(dest)
        try:
            row = conn.execute(
                "SELECT record_id, canonical FROM runtime_proposal"
            ).fetchone()
            parsed = json.loads(bytes(row["canonical"]).decode("utf-8"))
        finally:
            conn.close()
        # A proposal canonical that claims in-band authority, with a matching
        # recomputed stored digest, must still fail re-derivation.
        parsed["actor"] = "owner"
        forged = canonical_json(parsed)
        self.tamper_db(
            dest,
            "DROP TRIGGER runtime_proposal_no_update",
            "UPDATE runtime_proposal SET canonical = "
            f"{_hexlit(forged)}, stored_digest = "
            f"'{hashlib.sha256(forged).hexdigest()}' "
            f"WHERE record_id = '{row['record_id']}'",
        )
        self._assert_rewritten_hash_rejected(dest)

    def test_corrupt_runtime_backup_rejected_before_registration(self) -> None:
        store, _, dest = self._backup_with_runtime()
        self.tamper_db(
            dest,
            "DROP TRIGGER runtime_config_snapshot_no_update",
            f"UPDATE runtime_config_snapshot SET canonical = {_hexlit(b'not-json')}",
        )
        self.rewrite_outer_db_hash(dest)

        registry = Registry(self.tmp / "state")
        target = self.tmp / "restored"
        target.mkdir()
        (target / "unrelated.txt").write_text("keep me")
        with self.assertRaises(BackupError):
            restore_backup(dest, target, registry)
        # No registration, no app-owned directory, user file preserved.
        self.assertEqual(registry.list_projects(), [])
        self.assertFalse(os.path.lexists(paths.app_dir_for(str(target))))
        self.assertEqual((target / "unrelated.txt").read_text(), "keep me")

    def test_schema_validator_hook_is_additive(self) -> None:
        store, _, dest = self._backup_with_runtime()
        # The hook's minimum versions are ordered and include the job/runtime paths.
        minimums = [minimum for minimum, _ in backup_mod._SCHEMA_VALIDATORS]
        self.assertEqual(minimums, sorted(minimums))
        self.assertIn(4, minimums)
        self.assertIn(5, minimums)

        # Corrupt a runtime row only; the schema-4 job path is unaffected.
        self.tamper_db(
            dest,
            "DROP TRIGGER runtime_config_snapshot_no_update",
            f"UPDATE runtime_config_snapshot SET canonical = {_hexlit(b'bad')}",
        )
        snapshot = self._snapshot_conn(dest)
        try:
            # A version-4 path does not run the runtime validator.
            backup_mod._verify_schema_specific(snapshot, store.project_id, 4)
            with self.assertRaises(BackupError):
                backup_mod._verify_schema_specific(snapshot, store.project_id, 5)
        finally:
            snapshot.close()

        # A validator appended *above* the live schema applies only at its own
        # threshold and beyond, never to the historical paths. The threshold is
        # derived from the current schema so the hook stays future-only as the
        # schema advances (``create_backup`` internally re-verifies at the
        # store's current version, so a threshold at that version would fire).
        future_version = schema.STORE_SCHEMA_VERSION + 1
        calls: list[int] = []
        original = backup_mod._SCHEMA_VALIDATORS
        backup_mod._SCHEMA_VALIDATORS = original + (
            (future_version, lambda _conn, _pid: calls.append(future_version)),
        )
        try:
            clean = self.tmp / "clean-backup"
            create_backup(store, clean)
            conn = self._snapshot_conn(clean)
            try:
                # Below its threshold the appended validator must not run.
                backup_mod._verify_schema_specific(
                    conn, store.project_id, future_version - 1
                )
                self.assertEqual(calls, [])
                # At its own threshold it applies.
                backup_mod._verify_schema_specific(
                    conn, store.project_id, future_version
                )
                self.assertEqual(calls, [future_version])
            finally:
                conn.close()
        finally:
            backup_mod._SCHEMA_VALIDATORS = original


class HistoricalSchemaCompatibilityTest(RuntimeBackupBase):
    def test_schema3_snapshot_still_verifies(self) -> None:
        store = self.new_schema_store(3, "p3")
        self.addCleanup(store.close)
        self.seed_revision(store)
        dest = self.tmp / "backup3"
        create_backup(store, dest)
        report = verify_backup(dest)
        self.assertTrue(report["ok"])
        self.assertEqual(report["store_schema_version"], 3)

    def test_schema4_snapshot_still_verifies(self) -> None:
        store = self.new_schema_store(4, "p4")
        self.addCleanup(store.close)
        self.seed_revision(store)
        dest = self.tmp / "backup4"
        create_backup(store, dest)
        report = verify_backup(dest)
        self.assertTrue(report["ok"])
        self.assertEqual(report["store_schema_version"], 4)


if __name__ == "__main__":
    unittest.main()
