"""Backup/verify/restore coverage for schema-6 dispatch and schema-7 approvals (T8b).

These tests extend the private backup contract to the two later store schemas
(``storage/backup.py``):

* a schema-7 backup roundtrips a durable runtime dispatch on **both** routes
  (``opencode_session`` and ``direct_call``) plus real out-of-band human
  approvals recorded through the **actual low-level owner-auth TTY seams** (never
  a fabricated capability), leaving the artifact/fixture-approval rows unchanged;
* an ``unknown`` (ambiguous) direct dispatch keeps its ``unknown`` cost with **no**
  amount (never ``0``) and restore does not imply a fresh retry;
* a legitimate historical ``stale``/``superseded`` approval remains structurally
  valid and is preserved across backup and restore;
* the schema-6 dispatch ledger and schema-7 approval rows are inner-validated
  **read-only**, so a corrupt archive whose outer database hash was rewritten still
  fails closed — a tampered dispatch state, owner-auth evidence, snapshot
  timestamp, action digest, and orphan rows;
* a corrupt backup is rejected before any restore registration and leaves no
  partial restore;
* a schema-6-only fixture still verifies (the schema-7 approval validator does not
  run below its threshold);
* read-only verification never changes the snapshot;
* no capability/challenge is ever serialized; only the safe evidence projection.

Synthetic data only (no secrets, no licensed source text, no credentials, no
network, no model calls, no paid calls).

Run with::

    PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_human_runtime_backup.py'
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
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from review_article_agent.runtime import owner_auth  # noqa: E402
from review_article_agent.runtime.dispatch import (  # noqa: E402
    RuntimeDispatchRepository,
    runtime_dispatch_integrity_problems,
)
from review_article_agent.runtime.gates import prepare_gate_snapshot  # noqa: E402
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
from review_article_agent.storage.agent_runtime import RuntimeRepository  # noqa: E402
from review_article_agent.storage.backup import (  # noqa: E402
    MANIFEST_FILENAME,
    SNAPSHOT_FILENAME,
    BackupError,
    create_backup,
    restore_backup,
    verify_backup,
)
from review_article_agent.storage.human_approvals import (  # noqa: E402
    HumanApprovalRepository,
    integrity_problems as human_integrity_problems,
)

_OPS = ("acquisition", "storage", "redistribution", "hosted_transfer")
_CANARY = b"SECRET-CANARY-9931-source-bytes"
_BASE_TIME = datetime(2026, 10, 28, 12, 0, 0, tzinfo=timezone.utc)
_FIXED_CHALLENGE = "TEST-CHAL-0001"
_OWNER_UID = 1000


def _sha(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _bare(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _hexlit(data: bytes) -> str:
    """A SQLite ``X'..'`` blob literal."""
    return "X'" + data.hex() + "'"


def _rights(project_id, artifact_id, revision_id):
    """A synthetic, fully valid RightsRecord v1 bound to one exact revision."""
    return {
        "rights_id": f"RIGHTS-{revision_id}",
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
        "privacy": {"personal_data": False, "confidentiality": "public", "denial": False},
        "recorded_by": "user",
        "recorded_at": "2026-10-28T00:00:00Z",
        "derived_status": "valid",
    }


def _config() -> RoleConfiguration:
    return RoleConfiguration.from_mapping(
        {
            "schema_version": 1,
            "roles": {
                "writer": {
                    "provider_id": "openrouter",
                    "model_id": "anthropic/claude-sonnet-5-5",
                    "variant": "high",
                    "steps": 8,
                },
            },
        }
    )


class _FakeTty:
    """A low-level terminal double: only the terminal operations are mocked."""

    def __init__(self, *, replies=(), is_tty=True):
        self._replies = list(replies)
        self._is_tty = is_tty
        self.written: list[str] = []
        self.closed = False

    def is_tty(self):
        return self._is_tty

    def write(self, text, *, deadline=None):
        self.written.append(text)

    def read_line(self, *, max_chars, deadline):
        return self._replies.pop(0) if self._replies else None

    def close(self):
        self.closed = True


class HumanRuntimeBackupBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="raa-hr-backup-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.clock = [_BASE_TIME]
        self.mono = [1000.0]

    # -- stores -------------------------------------------------------------- #
    def new_store(self, name: str = "p") -> ProjectStore:
        store = ProjectStore.create_at(
            paths.app_dir_for(str(self.tmp / name)), paths.new_project_id()
        )
        self.addCleanup(store.close)
        return store

    def new_schema_store(self, version: int, name: str) -> ProjectStore:
        """Create a store pinned at an older store schema (no later tables)."""
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

    # -- human-gate chain ---------------------------------------------------- #
    def _target(self, store, artifact_id, revision_id):
        return {
            "project_id": store.project_id,
            "artifact_id": artifact_id,
            "revision_id": revision_id,
        }

    def _import(self, repo, artifact_id, content, revision_id, *, dependencies=(), parent_revision_id=None):
        return repo.import_revision(
            artifact_id=artifact_id,
            content=content,
            origin_locator="import:synthetic",
            rights=_rights(repo.project_id, artifact_id, revision_id),
            revision_id=revision_id,
            dependencies=list(dependencies),
            parent_revision_id=parent_revision_id,
        )

    def seed_chain(self, store):
        """base document (canary bytes) <- finding <- report (a live closure)."""
        repo = ArtifactRepository(store)
        repo.create_artifact("a-base", kind="document")
        self._import(repo, "a-base", _CANARY, "rev-base")
        repo.create_artifact("a-find", kind="finding")
        self._import(
            repo,
            "a-find",
            b"find-bytes",
            "rev-find",
            dependencies=[self._target(store, "a-base", "rev-base")],
        )
        repo.create_artifact("a-rep", kind="report")
        self._import(
            repo,
            "a-rep",
            b"rep-bytes",
            "rev-rep",
            dependencies=[self._target(store, "a-find", "rev-find")],
        )
        return repo

    def prepare(self, store, *, gate_kind="pilot", gate_id="gate_pilot_1", decision="approved"):
        return prepare_gate_snapshot(
            store,
            self._target(store, "a-rep", "rev-rep"),
            gate_kind=gate_kind,
            gate_id=gate_id,
            decision=decision,
            rationale="human review of the exact report revision",
        )

    # -- owner-auth low-level seams (never a capability bypass) -------------- #
    def install(self, *, challenge=_FIXED_CHALLENGE, owner_uid=_OWNER_UID, real_uid=_OWNER_UID):
        """Patch only the module's private low-level terminal/identity seams."""
        patches = [
            mock.patch.object(owner_auth, "_new_challenge", lambda: challenge),
            mock.patch.object(owner_auth, "_current_owner_uid", lambda: owner_uid),
            mock.patch.object(owner_auth, "_real_uid", lambda: real_uid),
            mock.patch.object(owner_auth, "_utcnow", lambda: self.clock[0]),
            mock.patch.object(owner_auth, "_monotonic", lambda: self.mono[0]),
            mock.patch.object(
                owner_auth,
                "_open_controlling_tty",
                lambda: _FakeTty(replies=[challenge]),
            ),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def mint(self, prepared):
        return owner_auth.request_owner_confirmation(prepared.owner_action())

    def record_approval(self, store, prepared, approvals=None):
        approvals = approvals if approvals is not None else HumanApprovalRepository(store)
        return approvals.record_decision(prepared, self.mint(prepared))

    # -- runtime dispatch ---------------------------------------------------- #
    def _make_task(self, store, request_id):
        cfg = _config()
        bounded = BoundedTask.from_mapping(
            {
                "project_id": store.project_id,
                "role": "writer",
                "target": self._target(store, "a-rep", "rev-rep"),
                "input_refs": [],
                "scope": ["section:introduction"],
                "prompt_version": "writer.v1",
                "size_limit": 4096,
                "step_limit": 4,
            },
            configuration=cfg,
        )
        return RuntimeRepository(store).record_task(
            bounded, request_id=request_id, configuration=cfg
        )

    def seed_dispatches(self, store) -> dict:
        """One completed opencode_session dispatch and one unknown direct dispatch."""
        runtime = RuntimeDispatchRepository(store)
        session_task = self._make_task(store, "task-session")
        direct_task = self._make_task(store, "task-direct")

        session = runtime.reserve_dispatch(
            session_task,
            request_id="call-session",
            route="opencode_session",
            stage="s1",
            input_digest=_bare(b"in-session"),
            version_digest=_bare(b"ver-session"),
            reserve_usd="0.10",
            bound_evidence_digest=_bare(b"bound-session"),
        )
        session = runtime.mark_submitted(session.dispatch_id)
        session = runtime.mark_admitted(session.dispatch_id, admission_id="adm_1")
        session = runtime.accept_result(
            session.dispatch_id,
            fence=session.current_fence,
            result_kind="synthetic",
            result_ref="res_1",
            result_digest=_bare(b"result-1"),
        )
        session = runtime.settle_cost(
            session.dispatch_id, cost_state="known", amount_usd="0.05"
        )

        direct = runtime.reserve_dispatch(
            direct_task,
            request_id="call-direct",
            route="direct_call",
            stage="s2",
            input_digest=_bare(b"in-direct"),
            version_digest=_bare(b"ver-direct"),
            reserve_usd="0.20",
            bound_evidence_digest=_bare(b"bound-direct"),
        )
        direct = runtime.mark_submitted(direct.dispatch_id)
        # A restart-orphaned submission: unknown (never reset, never retried).
        direct = runtime.recover(direct.dispatch_id)
        return {"session": session, "direct": direct}

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
        """Execute raw SQL against the snapshot, recreating any dropped trigger."""
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


class RoundTripTest(HumanRuntimeBackupBase):
    def test_roundtrip_preserves_dispatch_and_human_approvals(self) -> None:
        store = self.new_store()
        self.seed_chain(store)
        dispatched = self.seed_dispatches(store)

        fixture = ApprovalRepository(store).record_fixture_approval(
            target=self._target(store, "a-rep", "rev-rep"),
            gate_kind="fixture_artifact_revision",
            gate_id="gate-doc",
            decision="approved",
            rationale="synthetic local fixture; not a scientific approval",
        )

        approvals = HumanApprovalRepository(store)
        prepared = self.prepare(store)
        self.install()
        first = self.record_approval(store, prepared, approvals)
        second = self.record_approval(store, prepared, approvals)
        self.assertNotEqual(first.approval_id, second.approval_id)

        dest = self.tmp / "backup"
        create_backup(store, dest)
        report = verify_backup(dest)
        self.assertTrue(report["ok"])
        self.assertEqual(report["store_schema_version"], schema.STORE_SCHEMA_VERSION)

        registry = Registry(self.tmp / "state")
        target = self.tmp / "restored"
        target.mkdir()
        info = restore_backup(dest, target, registry)
        self.assertEqual(info.project_id, store.project_id)

        restored = ProjectStore.open_at(
            paths.app_dir_for(str(target)), expected_project_id=info.project_id
        )
        self.addCleanup(restored.close)

        # Existing artifact + fixture approvals are unchanged.
        restored_fixtures = ApprovalRepository(restored)
        self.assertEqual(
            {a.approval_id for a in restored_fixtures.list_approvals()},
            {fixture.approval_id},
        )
        self.assertTrue(restored_fixtures.verify_snapshot(fixture.approval_id).ok)

        # Both dispatch identities/reserves and their exact states survive.
        runtime = RuntimeDispatchRepository(restored)
        got_session = runtime.get_dispatch(dispatched["session"].dispatch_id)
        self.assertEqual(
            got_session.to_mapping(), dispatched["session"].to_mapping()
        )
        self.assertEqual(got_session.route, "opencode_session")
        self.assertEqual(got_session.accounting_bucket, "session_statistics")
        self.assertEqual(got_session.reserve_usd, "0.10")
        self.assertEqual(got_session.admission_id, "adm_1")
        self.assertEqual(got_session.state, "completed")
        self.assertEqual(got_session.cost_state, "known")
        self.assertEqual(got_session.cost_amount_usd, "0.05")

        got_direct = runtime.get_dispatch(dispatched["direct"].dispatch_id)
        self.assertEqual(got_direct.to_mapping(), dispatched["direct"].to_mapping())
        self.assertEqual(got_direct.route, "direct_call")
        self.assertEqual(got_direct.accounting_bucket, "direct_call")
        self.assertEqual(got_direct.reserve_usd, "0.20")
        self.assertIsNone(got_direct.admission_id)
        self.assertEqual(got_direct.state, "unknown")
        self.assertEqual(got_direct.cost_state, "unknown")
        # An ambiguous cost records no amount — never 0.
        self.assertIsNone(got_direct.cost_amount_usd)
        self.assertIsNone(got_direct.result_digest)
        self.assertEqual(
            runtime_dispatch_integrity_problems(restored.connection, info.project_id),
            [],
        )

        # Both approvals and their owner evidence survive; the earlier one is
        # superseded, the later one active.
        restored_approvals = HumanApprovalRepository(restored)
        self.assertEqual(
            restored_approvals.evaluate(first.approval_id).status, "superseded"
        )
        self.assertEqual(
            restored_approvals.evaluate(second.approval_id).status, "active"
        )
        self.assertTrue(restored_approvals.verify_snapshot(first.approval_id).ok)
        self.assertTrue(restored_approvals.verify_snapshot(second.approval_id).ok)
        self.assertEqual(
            human_integrity_problems(restored.connection, info.project_id), []
        )
        counts = {
            table: restored.connection.execute(
                f"SELECT COUNT(*) FROM {table}"
            ).fetchone()[0]
            for table in backup_mod._HUMAN_APPROVAL_TABLES
        }
        self.assertEqual(counts["human_approval"], 2)
        self.assertEqual(counts["owner_auth_evidence"], 2)

    def test_no_capability_or_challenge_is_serialized(self) -> None:
        store = self.new_store()
        self.seed_chain(store)
        self.install()
        approvals = HumanApprovalRepository(store)
        record = self.record_approval(store, self.prepare(store), approvals)
        dest = self.tmp / "backup"
        create_backup(store, dest)

        conn = self._snapshot_conn(dest)
        try:
            blobs: list[bytes] = []
            for table in backup_mod._HUMAN_APPROVAL_TABLES:
                for row in conn.execute(f"SELECT * FROM {table}").fetchall():
                    for value in tuple(row):
                        if isinstance(value, bytes):
                            blobs.append(value)
                        elif isinstance(value, str):
                            blobs.append(value.encode("utf-8"))
            haystack = b"\x00".join(blobs)
        finally:
            conn.close()
        # Neither the test challenge (a capability seam input) nor source bytes are
        # ever written; only the safe evidence projection is stored.
        self.assertNotIn(_FIXED_CHALLENGE.encode(), haystack)
        self.assertNotIn(_CANARY, haystack)
        self.assertTrue(record.owner_auth_id.startswith("auth_"))


class LifecycleTest(HumanRuntimeBackupBase):
    def test_stale_and_superseded_history_remains_valid_after_restore(self) -> None:
        store = self.new_store()
        repo = self.seed_chain(store)
        approvals = HumanApprovalRepository(store)
        prepared = self.prepare(store)
        self.install()
        first = self.record_approval(store, prepared, approvals)
        second = self.record_approval(store, prepared, approvals)
        # A later revision makes the latest approval stale; the earlier stays
        # superseded. Both are honest history.
        self._import(
            repo, "a-rep", b"rep-v2", "rev-rep-2", parent_revision_id="rev-rep"
        )
        self.assertEqual(approvals.evaluate(first.approval_id).status, "superseded")
        self.assertEqual(approvals.evaluate(second.approval_id).status, "stale")

        dest = self.tmp / "backup"
        create_backup(store, dest)
        report = verify_backup(dest)
        self.assertTrue(report["ok"])

        registry = Registry(self.tmp / "state")
        target = self.tmp / "restored"
        target.mkdir()
        info = restore_backup(dest, target, registry)
        restored = ProjectStore.open_at(
            paths.app_dir_for(str(target)), expected_project_id=info.project_id
        )
        self.addCleanup(restored.close)
        restored_approvals = HumanApprovalRepository(restored)
        self.assertEqual(
            restored_approvals.evaluate(first.approval_id).status, "superseded"
        )
        self.assertEqual(
            restored_approvals.evaluate(second.approval_id).status, "stale"
        )
        self.assertTrue(restored_approvals.verify_snapshot(first.approval_id).ok)
        self.assertTrue(restored_approvals.verify_snapshot(second.approval_id).ok)
        self.assertEqual(
            human_integrity_problems(restored.connection, info.project_id), []
        )

    def test_readonly_verify_does_not_change_snapshot(self) -> None:
        store = self.new_store()
        self.seed_chain(store)
        self.seed_dispatches(store)
        self.install()
        self.record_approval(store, self.prepare(store))
        dest = self.tmp / "backup"
        create_backup(store, dest)

        snapshot = dest / SNAPSHOT_FILENAME
        manifest = dest / MANIFEST_FILENAME
        before = (snapshot.read_bytes(), manifest.read_bytes())
        for _ in range(3):
            self.assertTrue(verify_backup(dest)["ok"])
        after = (snapshot.read_bytes(), manifest.read_bytes())
        self.assertEqual(before, after)


class InnerValidationTest(HumanRuntimeBackupBase):
    def _backup(self):
        store = self.new_store()
        self.seed_chain(store)
        dispatched = self.seed_dispatches(store)
        self.install()
        approvals = HumanApprovalRepository(store)
        prepared = self.prepare(store)
        record = self.record_approval(store, prepared, approvals)
        dest = self.tmp / "backup"
        create_backup(store, dest)
        return store, dispatched, record, dest

    def test_schema_validator_hook_appends_six_and_seven(self) -> None:
        minimums = [minimum for minimum, _ in backup_mod._SCHEMA_VALIDATORS]
        self.assertEqual(minimums, sorted(minimums))
        self.assertIn(4, minimums)
        self.assertIn(5, minimums)
        self.assertIn(backup_mod._DISPATCH_SCHEMA_VERSION, minimums)
        self.assertIn(backup_mod._HUMAN_APPROVAL_SCHEMA_VERSION, minimums)

    def test_dispatch_state_tamper_rejected(self) -> None:
        _, _, _, dest = self._backup()
        self.tamper_db(
            dest,
            "UPDATE runtime_dispatch_state SET updated_at_utc = 'not-a-time' "
            "WHERE state = 'completed'",
        )
        self._assert_rewritten_hash_rejected(dest)

    def test_dispatch_identity_tamper_rejected(self) -> None:
        _, _, _, dest = self._backup()
        self.tamper_db(
            dest,
            "DROP TRIGGER runtime_dispatch_no_update",
            "UPDATE runtime_dispatch SET task_fingerprint = '" + "b" * 64 + "'",
        )
        self._assert_rewritten_hash_rejected(dest)

    def test_owner_evidence_tamper_rejected(self) -> None:
        _, _, _, dest = self._backup()
        self.tamper_db(
            dest,
            "DROP TRIGGER owner_auth_evidence_no_update",
            "UPDATE owner_auth_evidence SET content_sha256 = '" + "sha256:" + "0" * 64 + "'",
        )
        self._assert_rewritten_hash_rejected(dest)

    def test_snapshot_timestamp_tamper_rejected(self) -> None:
        _, _, _, dest = self._backup()
        self.tamper_db(
            dest,
            "DROP TRIGGER human_approval_no_update",
            "UPDATE human_approval SET granted_at_utc = 'not-a-time'",
        )
        self._assert_rewritten_hash_rejected(dest)

    def test_action_digest_tamper_rejected(self) -> None:
        _, _, _, dest = self._backup()
        self.tamper_db(
            dest,
            "DROP TRIGGER human_approval_no_update",
            "UPDATE human_approval SET owner_auth_action_digest = '" + "a" * 64 + "'",
        )
        self._assert_rewritten_hash_rejected(dest)

    def test_orphan_entry_row_rejected(self) -> None:
        store, _, _, dest = self._backup()
        self.tamper_db(
            dest,
            "INSERT INTO human_approval_entry "
            "(project_id, approval_id, entry_index, artifact_id, revision_id, role, "
            " content_sha256, byte_size, rights_id, rights_record_revision, "
            " rights_digest) VALUES "
            f"('{store.project_id}', 'hapv_orphan', 0, 'a-rep', 'rev-rep', 'target', "
            f"'{_sha(b'x')}', 1, 'RIGHTS-x', 1, '{_sha(b'y')}')",
        )
        # The inner approval sweep reports the orphan even before the generic FK
        # check; both the direct validator and the full verify refuse it.
        snapshot = self._snapshot_conn(dest)
        try:
            with self.assertRaises(BackupError):
                backup_mod._verify_human_approval_relations(
                    snapshot, store.project_id
                )
        finally:
            snapshot.close()
        self._assert_rewritten_hash_rejected(dest)

    def test_orphan_dispatch_state_row_rejected(self) -> None:
        store, _, _, dest = self._backup()
        self.tamper_db(
            dest,
            "INSERT INTO runtime_dispatch_state "
            "(project_id, dispatch_id, state, current_fence, cost_state, "
            " updated_at_utc) VALUES "
            f"('{store.project_id}', 'disp_orphan', 'reserved', 1, 'not_incurred', "
            "'2026-10-28T00:00:00+00:00')",
        )
        snapshot = self._snapshot_conn(dest)
        try:
            with self.assertRaises(BackupError):
                backup_mod._verify_dispatch_relations(snapshot, store.project_id)
        finally:
            snapshot.close()
        self._assert_rewritten_hash_rejected(dest)

    def test_foreign_project_dispatch_row_rejected(self) -> None:
        store, _, _, dest = self._backup()
        self.tamper_db(
            dest,
            "INSERT INTO runtime_dispatch_state "
            "(project_id, dispatch_id, state, current_fence, cost_state, "
            " updated_at_utc) VALUES "
            "('prj_foreign', 'disp_foreign', 'reserved', 1, 'not_incurred', "
            "'2026-10-28T00:00:00+00:00')",
        )
        snapshot = self._snapshot_conn(dest)
        try:
            with self.assertRaises(BackupError):
                backup_mod._verify_dispatch_relations(snapshot, store.project_id)
        finally:
            snapshot.close()

    def test_foreign_project_approval_row_rejected(self) -> None:
        store, _, _, dest = self._backup()
        self.tamper_db(
            dest,
            "INSERT INTO owner_auth_evidence "
            "(project_id, auth_id, method, owner_uid, action_digest, "
            " verified_at_utc, canonical, content_sha256, created_at_utc) VALUES "
            "('prj_foreign', 'auth_foreign', 'controlling_tty_challenge', 1000, "
            f"'{'a' * 64}', '2026-10-28T00:00:00+00:00', {_hexlit(b'{}')}, "
            f"'{_sha(b'{}')}', '2026-10-28T00:00:00+00:00')",
        )
        snapshot = self._snapshot_conn(dest)
        try:
            with self.assertRaises(BackupError):
                backup_mod._verify_human_approval_relations(
                    snapshot, store.project_id
                )
        finally:
            snapshot.close()

    def test_dangling_dispatch_identity_rejected(self) -> None:
        store = self.new_store()
        self.seed_chain(store)
        self.seed_dispatches(store)
        # A third persisted task with no operational state row.
        task = self._make_task(store, "task-dangling")
        dest = self.tmp / "backup"
        create_backup(store, dest)
        self.tamper_db(
            dest,
            "INSERT INTO runtime_dispatch "
            "(project_id, dispatch_id, request_id, task_id, task_fingerprint, "
            " route, accounting_bucket, stage, input_digest, version_digest, "
            " reserve_usd, bound_evidence_digest, created_at_utc) VALUES "
            f"('{store.project_id}', 'disp_dangling', 'call-dangling', "
            f"'{task.task_id}', '{task.fingerprint}', 'opencode_session', "
            f"'session_statistics', 's3', '{_bare(b'i')}', '{_bare(b'v')}', "
            f"'0.05', '{_bare(b'b')}', '2026-10-28T00:00:00+00:00')",
        )
        # The generic FK check passes (the linked task exists); the inner sweep
        # catches the missing operational row.
        self._assert_rewritten_hash_rejected(dest)

    def test_corrupt_approval_backup_rejected_before_registration(self) -> None:
        store, _, _, dest = self._backup()
        self.tamper_db(
            dest,
            "DROP TRIGGER human_approval_no_update",
            f"UPDATE human_approval SET snapshot_canonical = {_hexlit(b'{}')}",
        )
        self.rewrite_outer_db_hash(dest)

        registry = Registry(self.tmp / "state")
        target = self.tmp / "restored"
        target.mkdir()
        (target / "unrelated.txt").write_text("keep me")
        with self.assertRaises(BackupError):
            restore_backup(dest, target, registry)
        self.assertEqual(registry.list_projects(), [])
        self.assertFalse(os.path.lexists(paths.app_dir_for(str(target))))
        self.assertEqual((target / "unrelated.txt").read_text(), "keep me")


class Schema6FixtureTest(HumanRuntimeBackupBase):
    def test_schema6_only_fixture_verifies_and_inner_checks(self) -> None:
        store = self.new_schema_store(6, "p6")
        self.addCleanup(store.close)
        self.seed_chain(store)
        dispatched = self.seed_dispatches(store)

        dest = self.tmp / "backup6"
        create_backup(store, dest)
        report = verify_backup(dest)
        self.assertTrue(report["ok"])
        self.assertEqual(report["store_schema_version"], 6)

        # The schema-7 approval validator does not run at schema 6; the schema-6
        # dispatch validator does. A tampered dispatch state still fails closed.
        self.tamper_db(
            dest,
            "UPDATE runtime_dispatch_state SET updated_at_utc = 'not-a-time' "
            "WHERE state = 'completed'",
        )
        self._assert_rewritten_hash_rejected(dest)

        # A fresh, untampered schema-6 snapshot restores to the current schema.
        clean = self.tmp / "backup6-clean"
        create_backup(store, clean)
        registry = Registry(self.tmp / "state")
        target = self.tmp / "restored6"
        target.mkdir()
        info = restore_backup(clean, target, registry)
        restored = ProjectStore.open_at(
            paths.app_dir_for(str(target)), expected_project_id=info.project_id
        )
        self.addCleanup(restored.close)
        self.assertEqual(restored.schema_version, schema.STORE_SCHEMA_VERSION)
        got = RuntimeDispatchRepository(restored).get_dispatch(
            dispatched["direct"].dispatch_id
        )
        self.assertEqual(got.state, "unknown")
        self.assertEqual(got.cost_state, "unknown")
        self.assertIsNone(got.cost_amount_usd)


if __name__ == "__main__":
    unittest.main()
