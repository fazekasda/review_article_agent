"""Acceptance tests for durable runtime task/session/proposal mappings (task T5a).

Synthetic data only (no secrets, no licensed source text, no model call). These
tests cover the additive ``storage/agent_runtime.py`` layer and store schema 5:

* an immutable per-project configuration snapshot whose digest is recomputed
  from the exact canonical bytes;
* app-owned service/project/session mappings that refuse a foreign
  role/model/variant substitution and non-URL-safe ids;
* bounded tasks keyed by ``request_id`` with same-key replay/divergent conflict,
  exact target/input digest + current-rights + current-target verification, and
  historical auditability after the target moves on;
* non-authorizing proposals derived anew through ``roles.submit_proposal`` (an
  in-band actor/id/digest is never trusted, and no artifact/approval state is
  written);
* database-level immutability (``UPDATE``/``DELETE`` refused) for every runtime
  table;
* a schema 4 -> 5 upgrade that leaves old artifacts, fixture-approval pins, and
  durable jobs unchanged;
* the pure ``VersionSet`` (C02 §6.1) validation helper.

Run with::

    PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_agent_runtime_store.py'
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from review_article_agent.domain.errors import UnsupportedSchemaVersion  # noqa: E402
from review_article_agent.domain.records import (  # noqa: E402
    RecordValidationError,
    validate_rights_record,
)
from review_article_agent.runtime.jobs import JobRepository  # noqa: E402
from review_article_agent.runtime.roles import (  # noqa: E402
    BoundedTask,
    ProposalError,
    RoleConfigError,
    RoleConfiguration,
    submit_proposal,
)
from review_article_agent.storage import (  # noqa: E402
    ApprovalRepository,
    ArtifactRepository,
    ProjectStore,
)
from review_article_agent.storage import paths, schema  # noqa: E402
from review_article_agent.storage.agent_runtime import (  # noqa: E402
    CONTEXT_KINDS,
    VERSION_SLOT_STATES,
    PureJsonVersionSet,
    RuntimeConflictError,
    RuntimeCorruptionError,
    RuntimeRepository,
    RuntimeValidationError,
    store_integrity_problems,
    validate_version_set,
    verify_store_integrity,
)

_OPS = ("acquisition", "storage", "redistribution", "hosted_transfer")


def _sha(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _bare(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _rights(
    project_id, artifact_id, revision_id, *, record_revision=1, hosted_ok=True,
    rights_id=None,
):
    ops = {
        "acquisition": "allowed",
        "storage": "allowed",
        "redistribution": "allowed",
        "hosted_transfer": "allowed" if hosted_ok else "unknown",
    }
    return {
        "rights_id": rights_id if rights_id is not None else f"RIGHTS-{artifact_id}",
        "project_id": project_id,
        "record_revision": record_revision,
        "subject": {
            "kind": "source_revision",
            "project_id": project_id,
            "artifact_id": artifact_id,
            "revision_id": revision_id,
        },
        "operations": ops,
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
        "recorded_at": "2026-10-27T00:00:00Z",
        "derived_status": "valid",
    }


def _role_model(model="anthropic/claude-sonnet-5-5", variant="high"):
    spec = {"provider_id": "openrouter", "model_id": model, "steps": 8}
    if variant is not None:
        spec["variant"] = variant
    return spec


def _config(role="writer", model="anthropic/claude-sonnet-5-5", variant="high"):
    return RoleConfiguration.from_mapping(
        {"schema_version": 1, "roles": {role: _role_model(model, variant)}}
    )


class RuntimeTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="raa-runtime-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def new_store(self, name="p") -> ProjectStore:
        return ProjectStore.create_at(
            paths.app_dir_for(str(self.tmp / name)), paths.new_project_id()
        )

    def seed_revision(
        self, store, content=b"runtime-source-bytes", *, artifact_id=None,
        revision_id="rev_1", hosted_ok=True,
    ):
        repo = ArtifactRepository(store)
        artifact = repo.create_artifact(kind="document", artifact_id=artifact_id)
        revision = repo.import_revision(
            artifact_id=artifact.artifact_id,
            content=content,
            origin_locator="import:synthetic",
            rights=_rights(
                store.project_id, artifact.artifact_id, revision_id, hosted_ok=hosted_ok
            ),
            revision_id=revision_id,
        )
        return artifact, revision

    def make_task(self, store, artifact, *, role="writer", configuration=None,
                  input_refs=(), step_limit=4, target_revision="rev_1"):
        cfg = configuration if configuration is not None else _config(role=role)
        mapping = {
            "project_id": store.project_id,
            "role": role,
            "target": {
                "project_id": store.project_id,
                "artifact_id": artifact.artifact_id,
                "revision_id": target_revision,
            },
            "input_refs": list(input_refs),
            "scope": ["section:introduction"],
            "prompt_version": f"{role}.v1",
            "size_limit": 4096,
            "step_limit": step_limit,
        }
        return BoundedTask.from_mapping(mapping, configuration=cfg), cfg


class ConfigSnapshotTest(RuntimeTestBase):
    def test_record_reopen_and_verify(self) -> None:
        store = self.new_store()
        self.addCleanup(store.close)
        cfg = _config()
        runtime = RuntimeRepository(store)
        snap = runtime.record_configuration(cfg)
        self.assertEqual(snap.config_version, 1)
        self.assertEqual(
            snap.config_digest,
            hashlib.sha256(
                __import__("json").dumps(
                    cfg.as_mapping(), sort_keys=True, separators=(",", ":"),
                    ensure_ascii=False,
                ).encode("utf-8")
            ).hexdigest(),
        )
        self.assertTrue(runtime.verify_configuration(snap.snapshot_id))

        # Identical bytes are idempotent.
        again = runtime.record_configuration(cfg)
        self.assertEqual(again.snapshot_id, snap.snapshot_id)
        self.assertEqual(len(runtime.list_configurations()), 1)

        # A different configuration gets the next monotonic version.
        other = runtime.record_configuration(
            _config(role="writer", model="openai/gpt-6.1-sol", variant="high")
        )
        self.assertEqual(other.config_version, 2)
        self.assertEqual(len(runtime.list_configurations()), 2)

    def test_reopen_preserves_snapshot(self) -> None:
        root = str(self.tmp / "p")
        store = ProjectStore.create_at(paths.app_dir_for(root), paths.new_project_id())
        pid = store.project_id
        snap = RuntimeRepository(store).record_configuration(_config())
        store.close()
        reopened = ProjectStore.open_at(paths.app_dir_for(root), expected_project_id=pid)
        self.addCleanup(reopened.close)
        runtime = RuntimeRepository(reopened)
        got = runtime.get_configuration(snap.snapshot_id)
        self.assertIsNotNone(got)
        self.assertEqual(got.config_digest, snap.config_digest)
        self.assertEqual(got.role_configuration().as_mapping(), _config().as_mapping())


class SessionMappingTest(RuntimeTestBase):
    def test_record_reopen_and_idempotent(self) -> None:
        root = str(self.tmp / "p")
        store = ProjectStore.create_at(paths.app_dir_for(root), paths.new_project_id())
        pid = store.project_id
        runtime = RuntimeRepository(store)
        mapping = runtime.record_session_mapping(
            service_instance_id="svc_1",
            opencode_project_id="oc_proj_9",
            clean_location=str(self.tmp / "clean"),
            session_id="ses_writer_1",
            role="writer",
            model="openrouter/anthropic/claude-sonnet-5-5#high",
            configuration=_config(),
        )
        self.assertEqual(mapping.model_ref, "openrouter/anthropic/claude-sonnet-5-5#high")
        self.assertEqual(mapping.opencode_project_id, "oc_proj_9")
        # Identical replay is idempotent.
        same = runtime.record_session_mapping(
            service_instance_id="svc_1",
            opencode_project_id="oc_proj_9",
            clean_location=str(self.tmp / "clean"),
            session_id="ses_writer_1",
            role="writer",
            model="openrouter/anthropic/claude-sonnet-5-5#high",
            configuration=_config(),
        )
        self.assertEqual(same.mapping_id, mapping.mapping_id)
        store.close()

        reopened = ProjectStore.open_at(paths.app_dir_for(root), expected_project_id=pid)
        self.addCleanup(reopened.close)
        got = RuntimeRepository(reopened).get_session_mapping_for("ses_writer_1")
        self.assertIsNotNone(got)
        self.assertEqual(got.mapping_id, mapping.mapping_id)
        self.assertEqual(got.config_digest, mapping.config_digest)

    def test_opencode_project_id_is_optional(self) -> None:
        store = self.new_store()
        self.addCleanup(store.close)
        mapping = RuntimeRepository(store).record_session_mapping(
            service_instance_id="svc_1",
            clean_location=str(self.tmp / "clean"),
            session_id="ses_1",
            role="writer",
            model="openrouter/anthropic/claude-sonnet-5-5#high",
            configuration=_config(),
        )
        self.assertIsNone(mapping.opencode_project_id)

    def test_divergent_session_conflicts(self) -> None:
        store = self.new_store()
        self.addCleanup(store.close)
        runtime = RuntimeRepository(store)
        runtime.record_session_mapping(
            service_instance_id="svc_1",
            clean_location=str(self.tmp / "clean"),
            session_id="ses_1",
            role="writer",
            model="openrouter/anthropic/claude-sonnet-5-5#high",
            configuration=_config(),
        )
        with self.assertRaises(RuntimeConflictError):
            runtime.record_session_mapping(
                service_instance_id="svc_2",  # divergent
                clean_location=str(self.tmp / "clean"),
                session_id="ses_1",
                role="writer",
                model="openrouter/anthropic/claude-sonnet-5-5#high",
                configuration=_config(),
            )

    def test_foreign_role_and_model_substitutions_refused(self) -> None:
        store = self.new_store()
        self.addCleanup(store.close)
        runtime = RuntimeRepository(store)
        base = dict(
            service_instance_id="svc_1",
            clean_location=str(self.tmp / "clean"),
            session_id="ses_1",
            configuration=_config(),
        )
        with self.assertRaises(RoleConfigError):
            # 'lead' is a known scientific role but not configured.
            runtime.record_session_mapping(role="lead", model="openrouter/x", **base)
        with self.assertRaises(RuntimeValidationError):
            # A different concrete model than configured.
            runtime.record_session_mapping(
                role="writer", model="openrouter/openai/gpt-6.1-sol#high", **base
            )
        with self.assertRaises(RuntimeValidationError):
            # Variant-only substitution is still a different concrete reference.
            runtime.record_session_mapping(
                role="writer", model="openrouter/anthropic/claude-sonnet-5-5", **base
            )

    def test_non_urlsafe_ids_refused(self) -> None:
        store = self.new_store()
        self.addCleanup(store.close)
        runtime = RuntimeRepository(store)
        common = dict(
            role="writer",
            model="openrouter/anthropic/claude-sonnet-5-5#high",
            configuration=_config(),
        )
        with self.assertRaises(RuntimeValidationError):
            runtime.record_session_mapping(
                service_instance_id="svc 1",
                clean_location=str(self.tmp / "clean"),
                session_id="ses_1",
                **common,
            )
        with self.assertRaises(RuntimeValidationError):
            runtime.record_session_mapping(
                service_instance_id="svc_1",
                clean_location=str(self.tmp / "clean"),
                session_id="ses/../../evil",
                **common,
            )
        with self.assertRaises(RuntimeValidationError):
            runtime.record_session_mapping(
                service_instance_id="svc_1",
                opencode_project_id="oc 1",
                clean_location=str(self.tmp / "clean"),
                session_id="ses_1",
                **common,
            )


class TaskRecordTest(RuntimeTestBase):
    def test_record_reopen_and_replay(self) -> None:
        root = str(self.tmp / "p")
        store = ProjectStore.create_at(paths.app_dir_for(root), paths.new_project_id())
        pid = store.project_id
        artifact, revision = self.seed_revision(store)
        input_artifact, input_revision = self.seed_revision(
            store, b"input-bytes", artifact_id="art_in", revision_id="rev_in"
        )
        task, cfg = self.make_task(
            store,
            artifact,
            input_refs=[
                {
                    "project_id": pid,
                    "artifact_id": input_artifact.artifact_id,
                    "revision_id": "rev_in",
                    "digest": _bare(b"input-bytes"),
                }
            ],
        )
        runtime = RuntimeRepository(store)
        record = runtime.record_task(task, request_id="req-1", configuration=cfg)
        self.assertEqual(record.fingerprint, task.fingerprint())
        self.assertEqual(record.target["revision_id"], "rev_1")
        self.assertEqual(len(record.input_refs), 1)
        self.assertEqual(record.input_refs[0]["digest"], _bare(b"input-bytes"))

        # Same request key + same task is idempotent.
        replay = runtime.record_task(task, request_id="req-1", configuration=cfg)
        self.assertEqual(replay.task_id, record.task_id)
        store.close()

        reopened = ProjectStore.open_at(paths.app_dir_for(root), expected_project_id=pid)
        self.addCleanup(reopened.close)
        got = RuntimeRepository(reopened).get_task_by_request("req-1")
        self.assertIsNotNone(got)
        self.assertEqual(got.task_id, record.task_id)
        self.assertEqual(got.to_mapping(), record.to_mapping())

    def test_divergent_request_conflicts(self) -> None:
        store = self.new_store()
        self.addCleanup(store.close)
        artifact, _ = self.seed_revision(store)
        runtime = RuntimeRepository(store)
        task, cfg = self.make_task(store, artifact)
        runtime.record_task(task, request_id="req-1", configuration=cfg)
        other, _ = self.make_task(store, artifact, step_limit=3)
        with self.assertRaises(RuntimeConflictError):
            runtime.record_task(other, request_id="req-1", configuration=cfg)

    def test_missing_target_and_input_fail_closed(self) -> None:
        store = self.new_store()
        self.addCleanup(store.close)
        artifact, _ = self.seed_revision(store)
        runtime = RuntimeRepository(store)
        bad_target, cfg = self.make_task(store, artifact, target_revision="rev_missing")
        with self.assertRaises(RuntimeValidationError):
            runtime.record_task(bad_target, request_id="r-t", configuration=cfg)

        bad_input, cfg = self.make_task(
            store,
            artifact,
            input_refs=[
                {
                    "project_id": store.project_id,
                    "artifact_id": artifact.artifact_id,
                    "revision_id": "rev_missing",
                    "digest": "a" * 64,
                }
            ],
        )
        with self.assertRaises(RuntimeValidationError):
            runtime.record_task(bad_input, request_id="r-i", configuration=cfg)

    def test_digest_mismatch_fails_closed(self) -> None:
        store = self.new_store()
        self.addCleanup(store.close)
        artifact, _ = self.seed_revision(store)
        runtime = RuntimeRepository(store)
        task, cfg = self.make_task(
            store,
            artifact,
            input_refs=[
                {
                    "project_id": store.project_id,
                    "artifact_id": artifact.artifact_id,
                    "revision_id": "rev_1",
                    "digest": "b" * 64,  # not the stored digest
                }
            ],
        )
        with self.assertRaises(RuntimeValidationError):
            runtime.record_task(task, request_id="r-d", configuration=cfg)

    def test_stale_superseded_target_fails_closed(self) -> None:
        store = self.new_store()
        self.addCleanup(store.close)
        artifact, _ = self.seed_revision(store)
        repo = ArtifactRepository(store)
        # A newer revision of the same artifact supersedes rev_1 as the head.
        repo.import_revision(
            artifact_id=artifact.artifact_id,
            content=b"second-generation",
            origin_locator="import:synthetic",
            rights=_rights(
                store.project_id,
                artifact.artifact_id,
                "rev_2",
                rights_id=f"RIGHTS-{artifact.artifact_id}-r2",
            ),
            revision_id="rev_2",
            parent_revision_id="rev_1",
        )
        runtime = RuntimeRepository(store)
        stale, cfg = self.make_task(store, artifact, target_revision="rev_1")
        with self.assertRaises(RuntimeValidationError):
            runtime.record_task(stale, request_id="r-s", configuration=cfg)

    def test_superseded_rights_fails_closed(self) -> None:
        store = self.new_store()
        self.addCleanup(store.close)
        artifact, _ = self.seed_revision(store)
        repo = ArtifactRepository(store)
        # A newer rights record_revision for the same rights_id supersedes the pin.
        repo.store_rights_record(
            _rights(store.project_id, artifact.artifact_id, "rev_1", record_revision=2)
        )
        runtime = RuntimeRepository(store)
        task, cfg = self.make_task(store, artifact)
        with self.assertRaises(RuntimeValidationError):
            runtime.record_task(task, request_id="r-r", configuration=cfg)

    def test_storage_rights_suffice_without_hosted_grant(self) -> None:
        store = self.new_store()
        self.addCleanup(store.close)
        artifact, _ = self.seed_revision(store, hosted_ok=False)
        runtime = RuntimeRepository(store)
        task, cfg = self.make_task(store, artifact)
        record = runtime.record_task(task, request_id="r-h", configuration=cfg)
        self.assertEqual(record.role, "writer")

    def test_foreign_project_refused(self) -> None:
        store = self.new_store()
        self.addCleanup(store.close)
        artifact, _ = self.seed_revision(store)
        other = _config()
        # Build a task whose project_id is a different, valid project id.
        mapping = {
            "project_id": "prj_other",
            "role": "writer",
            "target": {
                "project_id": "prj_other",
                "artifact_id": artifact.artifact_id,
                "revision_id": "rev_1",
            },
            "input_refs": [],
            "scope": ["s"],
            "prompt_version": "writer.v1",
            "size_limit": 4096,
            "step_limit": 4,
        }
        foreign = BoundedTask.from_mapping(mapping, configuration=other)
        with self.assertRaises(RuntimeValidationError):
            RuntimeRepository(store).record_task(
                foreign, request_id="r-f", configuration=other
            )


class ProposalRecordTest(RuntimeTestBase):
    def _seeded(self):
        store = self.new_store()
        self.addCleanup(store.close)
        artifact, _ = self.seed_revision(store)
        runtime = RuntimeRepository(store)
        task, cfg = self.make_task(store, artifact)
        record = runtime.record_task(task, request_id="req-1", configuration=cfg)
        return store, runtime, task, cfg, record

    def test_record_reopen_and_derive_anew(self) -> None:
        store, runtime, task, cfg, record = self._seeded()
        proposal = runtime.record_proposal(
            record, {"content": {"finding": "synthetic"}}, configuration=cfg
        )
        expected = submit_proposal(task, {"content": {"finding": "synthetic"}},
                                   configuration=cfg)
        self.assertEqual(proposal.proposal_id, expected.proposal_id)
        self.assertEqual(proposal.payload_digest, expected.payload_digest)
        self.assertEqual(proposal.role, "writer")
        self.assertTrue(runtime.verify_proposal(proposal.record_id))

        # Same task+payload is idempotent (derived id is stable).
        replay = runtime.record_proposal(
            record, {"content": {"finding": "synthetic"}}, configuration=cfg
        )
        self.assertEqual(replay.record_id, proposal.record_id)
        self.assertEqual(
            RuntimeRepository(store).get_proposal(proposal.record_id).stored_digest,
            proposal.stored_digest,
        )

    def test_inband_authority_and_identifier_never_trusted(self) -> None:
        _, runtime, task, cfg, record = self._seeded()
        # A payload that tries to carry authority/actor/eid is rejected outright.
        with self.assertRaises(ProposalError):
            runtime.record_proposal(record, {"content": {"actor": "owner"}}, configuration=cfg)
        with self.assertRaises(ProposalError):
            runtime.record_proposal(
                record, {"actor": "owner", "content": {}}, configuration=cfg
            )
        # An in-band proposal_id in the content is carried as data, never trusted:
        # the stored id is the derived one.
        proposal = runtime.record_proposal(
            record, {"content": {"proposal_id": "0" * 64}}, configuration=cfg
        )
        self.assertNotEqual(proposal.proposal_id, "0" * 64)
        self.assertEqual(
            proposal.proposal_id,
            submit_proposal(
                task, {"content": {"proposal_id": "0" * 64}}, configuration=cfg
            ).proposal_id,
        )

    def test_proposal_is_not_an_artifact_or_approval(self) -> None:
        store, runtime, task, cfg, record = self._seeded()
        before_artifacts = store.connection.execute(
            "SELECT COUNT(*) AS n FROM artifact"
        ).fetchone()["n"]
        before_approvals = store.connection.execute(
            "SELECT COUNT(*) AS n FROM fixture_approval"
        ).fetchone()["n"]
        runtime.record_proposal(record, {"content": {"finding": "x"}}, configuration=cfg)
        self.assertEqual(
            store.connection.execute("SELECT COUNT(*) AS n FROM artifact").fetchone()["n"],
            before_artifacts,
        )
        self.assertEqual(
            store.connection.execute(
                "SELECT COUNT(*) AS n FROM fixture_approval"
            ).fetchone()["n"],
            before_approvals,
        )

    def test_unpersisted_task_refused(self) -> None:
        store = self.new_store()
        self.addCleanup(store.close)
        artifact, _ = self.seed_revision(store)
        task, cfg = self.make_task(store, artifact)
        with self.assertRaises(RuntimeValidationError):
            RuntimeRepository(store).record_proposal(
                task, {"content": {}}, configuration=cfg
            )

    def test_drifted_config_refused(self) -> None:
        _, runtime, task, cfg, record = self._seeded()
        other_cfg = _config(model="openai/gpt-6.1-sol")
        with self.assertRaises(RuntimeValidationError):
            runtime.record_proposal(
                record, {"content": {"finding": "x"}}, configuration=other_cfg
            )


class ImmutabilityTest(RuntimeTestBase):
    def _populated(self):
        store = self.new_store()
        self.addCleanup(store.close)
        artifact, _ = self.seed_revision(store)
        input_artifact, _ = self.seed_revision(
            store, b"input-bytes", artifact_id="art_in", revision_id="rev_in"
        )
        runtime = RuntimeRepository(store)
        snap = runtime.record_configuration(_config())
        runtime.record_session_mapping(
            service_instance_id="svc_1",
            clean_location=str(self.tmp / "clean"),
            session_id="ses_1",
            role="writer",
            model="openrouter/anthropic/claude-sonnet-5-5#high",
            configuration=_config(),
        )
        task, cfg = self.make_task(
            store,
            artifact,
            input_refs=[
                {
                    "project_id": store.project_id,
                    "artifact_id": input_artifact.artifact_id,
                    "revision_id": "rev_in",
                    "digest": _bare(b"input-bytes"),
                }
            ],
        )
        record = runtime.record_task(task, request_id="req-1", configuration=cfg)
        runtime.record_proposal(record, {"content": {"finding": "x"}}, configuration=cfg)
        return store, runtime, snap

    def test_update_and_delete_are_refused(self) -> None:
        store, _, _ = self._populated()
        conn = store.connection
        cases = [
            ("runtime_config_snapshot", "snapshot_id"),
            ("runtime_session_mapping", "mapping_id"),
            ("runtime_bounded_task", "task_id"),
            ("runtime_proposal", "proposal_id"),
        ]
        for table, key in cases:
            for statement in (
                f"UPDATE {table} SET created_at_utc = 'x'",
                f"DELETE FROM {table}",
            ):
                with self.assertRaises(sqlite3.IntegrityError):
                    conn.execute(statement)
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute("UPDATE runtime_task_input SET digest = 'x'")
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute("DELETE FROM runtime_task_input")

    def test_tampered_config_canonical_is_denied_on_read(self) -> None:
        # R1 repro: dropping the immutability trigger and rewriting the canonical
        # bytes must NOT yield corrupt bytes from a read.
        store, runtime, snap = self._populated()
        conn = store.connection
        conn.execute("DROP TRIGGER runtime_config_snapshot_no_update")
        conn.execute(
            "UPDATE runtime_config_snapshot SET canonical = ? WHERE project_id = ? "
            "AND snapshot_id = ?",
            (b'{"schema_version":1,"roles":{}}', snap.project_id, snap.snapshot_id),
        )
        with self.assertRaises(RuntimeCorruptionError):
            runtime.get_configuration(snap.snapshot_id)
        with self.assertRaises(RuntimeCorruptionError):
            runtime.list_configurations()
        self.assertFalse(runtime.verify_configuration(snap.snapshot_id))
        self.assertTrue(runtime.verify_all_integrity())


class SchemaUpgradeTest(RuntimeTestBase):
    def _schema4_store_with_data(self):
        """Create a real schema-4 store and seed an artifact, approval, and job."""
        root = str(self.tmp / "p4")
        app_dir = paths.app_dir_for(root)
        db_path = paths.store_path_for(root)
        paths.ensure_owner_private_dir(app_dir)
        conn = schema.open_connection(db_path)
        schema.migrate(
            conn,
            target_version=4,
            migrations=schema.STORE_MIGRATIONS,
            label="project store",
        )
        pid = paths.new_project_id()
        conn.execute(
            "INSERT INTO project (project_id, name, created_at_utc, app_version) "
            "VALUES (?, 's4', '2026-01-01T00:00:00+00:00', '0.0.0')",
            (pid,),
        )
        conn.execute(
            "INSERT INTO meta (key, value) VALUES ('created_at_utc', ?)",
            ("2026-01-01T00:00:00+00:00",),
        )
        store = ProjectStore(conn, app_dir, pid, schema_version=4, app_version="0.0.0")
        artifact, revision = self.seed_revision(store, b"schema4-bytes")
        approval = ApprovalRepository(store).record_fixture_approval(
            target={
                "project_id": pid,
                "artifact_id": artifact.artifact_id,
                "revision_id": revision.revision_id,
            },
            gate_kind="fixture_artifact_revision",
            gate_id="gate-1",
            decision="approved",
            rationale="synthetic local fixture; not a scientific approval",
        )
        job = JobRepository(store).submit("req-job", task_kind="probe")
        return store, artifact, approval, job

    def test_schema4_to_5_preserves_old_rows(self) -> None:
        store, artifact, approval, job = self._schema4_store_with_data()
        pid = store.project_id
        app_dir = store.app_dir
        store.close()

        reopened = ProjectStore.open_at(app_dir, expected_project_id=pid)
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.schema_version, 5)
        names = {
            row[0]
            for row in reopened.connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        self.assertTrue(
            {
                "runtime_config_snapshot",
                "runtime_session_mapping",
                "runtime_bounded_task",
                "runtime_task_input",
                "runtime_proposal",
            }
            <= names
        )
        # Exact artifact bytes preserved.
        repo = ArtifactRepository(reopened)
        self.assertEqual(repo.read_content(artifact.artifact_id, "rev_1"), b"schema4-bytes")
        # Fixture approval pin preserved and still verifies at its recorded schema.
        approvals = ApprovalRepository(reopened)
        stored = approvals.get_approval(approval.approval_id)
        self.assertEqual(stored.snapshot_digest, approval.snapshot_digest)
        self.assertEqual(stored.storage_schema_version, 4)
        self.assertTrue(approvals.verify_snapshot(approval.approval_id).ok)
        # Durable job unchanged.
        self.assertEqual(JobRepository(reopened).get(job.job_id).state, "queued")

    def test_newer_schema_refused(self) -> None:
        store = self.new_store()
        pid = store.project_id
        app_dir = store.app_dir
        db_path = store.db_path
        store.close()
        conn = schema.open_connection(db_path)
        try:
            conn.execute("PRAGMA user_version = 6")
        finally:
            conn.close()
        with self.assertRaises(UnsupportedSchemaVersion):
            ProjectStore.open_at(app_dir, expected_project_id=pid)


class TwoProjectIsolationTest(RuntimeTestBase):
    def test_records_are_per_project(self) -> None:
        store1 = self.new_store("p1")
        self.addCleanup(store1.close)
        store2 = self.new_store("p2")
        self.addCleanup(store2.close)
        artifact1, _ = self.seed_revision(store1)
        artifact2, _ = self.seed_revision(store2)
        runtime1 = RuntimeRepository(store1)
        runtime2 = RuntimeRepository(store2)
        task1, cfg = self.make_task(store1, artifact1)
        record1 = runtime1.record_task(task1, request_id="req-1", configuration=cfg)
        runtime1.record_session_mapping(
            service_instance_id="svc_1",
            clean_location=str(self.tmp / "clean"),
            session_id="ses_1",
            role="writer",
            model="openrouter/anthropic/claude-sonnet-5-5#high",
            configuration=cfg,
        )
        self.assertIsNone(runtime2.get_task_by_request("req-1"))
        self.assertEqual(runtime2.list_tasks(), [])
        self.assertEqual(runtime2.list_session_mappings(), [])
        self.assertIsNone(runtime2.get_session_mapping_for("ses_1"))

        # The same request id in another project yields a different, isolated id.
        task2, cfg = self.make_task(store2, artifact2)
        record2 = runtime2.record_task(task2, request_id="req-1", configuration=cfg)
        self.assertNotEqual(record1.task_id, record2.task_id)
        self.assertNotEqual(record1.project_id, record2.project_id)


class VersionSetTest(unittest.TestCase):
    def _known_ref(self):
        return {"version": "1.2.0", "digest": "a" * 64}

    def _valid(self):
        return {
            "context_kind": "model_derived",
            "input_digest": "b" * 64,
            "model": {"state": "known", "ref": self._known_ref()},
            "prompt": {"state": "known", "ref": self._known_ref()},
            "rubric": {"state": "unknown"},
            "parser": {"state": "not_applicable"},
            "adapter": {"state": "known", "ref": self._known_ref()},
            "route": "opencode_session",
        }

    def test_valid_model_derived_consumed(self) -> None:
        vs = self._valid()
        normalized = validate_version_set(vs, consumed=("model", "prompt", "adapter"))
        self.assertEqual(normalized["context_kind"], "model_derived")
        # The canonical NESTED shape is preserved.
        self.assertEqual(normalized["model"]["state"], "known")
        self.assertEqual(normalized["model"]["ref"]["version"], "1.2.0")
        self.assertEqual(normalized["rubric"], {"state": "unknown"})
        wrapper = PureJsonVersionSet.from_mapping(vs, consumed=("model",))
        self.assertEqual(wrapper.route, "opencode_session")
        self.assertEqual(wrapper.slot("model")["ref"]["digest"], "a" * 64)

    def test_validator_is_idempotent(self) -> None:
        once = validate_version_set(self._valid())
        twice = validate_version_set(once)
        self.assertEqual(once, twice)

    def test_wrapper_roundtrips(self) -> None:
        wrapper = PureJsonVersionSet.from_mapping(self._valid())
        self.assertEqual(validate_version_set(wrapper.as_mapping()), self._valid())

    def test_direct_constructor_validates(self) -> None:
        with self.assertRaises(RecordValidationError):
            PureJsonVersionSet({"bad": True})
        # A valid mapping is accepted through the direct constructor.
        direct = PureJsonVersionSet(self._valid())
        self.assertEqual(direct.context_kind, "model_derived")

    def test_wrapper_is_deeply_immutable(self) -> None:
        wrapper = PureJsonVersionSet.from_mapping(self._valid())
        # Mutating a returned copy never changes the stored value.
        out = wrapper.as_mapping()
        out["model"]["ref"]["digest"] = "0" * 64
        self.assertEqual(wrapper.slot("model")["ref"]["digest"], "a" * 64)
        with self.assertRaises(TypeError):
            wrapper._mapping["model"]["ref"]["digest"] = "0" * 64  # type: ignore[index]

    def test_errors_are_static_and_do_not_echo_input(self) -> None:
        vs = self._valid()
        vs["model"] = {"state": "SYNTHETIC_CANARY_STATE"}
        try:
            validate_version_set(vs)
        except RecordValidationError as exc:
            self.assertNotIn("SYNTHETIC_CANARY_STATE", str(exc))
        else:  # pragma: no cover - the state is invalid
            self.fail("invalid state accepted")

    def test_human_authored_not_applicable(self) -> None:
        vs = {
            "context_kind": "human_authored",
            "input_digest": "c" * 64,
            "model": {"state": "not_applicable"},
            "prompt": {"state": "not_applicable"},
            "rubric": {"state": "not_applicable"},
            "parser": {"state": "not_applicable"},
            "adapter": {"state": "not_applicable"},
            "route": "not_applicable",
        }
        self.assertEqual(validate_version_set(vs)["context_kind"], "human_authored")

    def test_human_authored_must_not_invent_a_model(self) -> None:
        vs = {
            "context_kind": "human_authored",
            "input_digest": "c" * 64,
            "model": {"state": "known", "ref": self._known_ref()},
            "prompt": {"state": "not_applicable"},
            "rubric": {"state": "not_applicable"},
            "parser": {"state": "not_applicable"},
            "adapter": {"state": "not_applicable"},
            "route": "not_applicable",
        }
        with self.assertRaises(RecordValidationError):
            validate_version_set(vs)

    def test_missing_field_fails(self) -> None:
        with self.assertRaises(RecordValidationError):
            validate_version_set({"context_kind": "mixed"})

    def test_unknown_state_fails(self) -> None:
        vs = {
            "context_kind": "model_derived",
            "input_digest": "b" * 64,
            "model": {"state": "guessed"},
            "prompt": {"state": "known", "ref": self._known_ref()},
            "rubric": {"state": "unknown"},
            "parser": {"state": "unknown"},
            "adapter": {"state": "unknown"},
            "route": "direct_call",
        }
        with self.assertRaises(RecordValidationError):
            validate_version_set(vs)
        self.assertIn("known", VERSION_SLOT_STATES)

    def test_known_slot_requires_real_ref(self) -> None:
        vs = {
            "context_kind": "model_derived",
            "input_digest": "b" * 64,
            "model": {"state": "known", "ref": {"version": "1", "digest": "not-hex"}},
            "prompt": {"state": "known", "ref": self._known_ref()},
            "rubric": {"state": "unknown"},
            "parser": {"state": "unknown"},
            "adapter": {"state": "unknown"},
            "route": "direct_call",
        }
        with self.assertRaises(RecordValidationError):
            validate_version_set(vs)

    def test_consumed_slot_must_be_known(self) -> None:
        vs = {
            "context_kind": "model_derived",
            "input_digest": "b" * 64,
            "model": {"state": "known", "ref": self._known_ref()},
            "prompt": {"state": "known", "ref": self._known_ref()},
            "rubric": {"state": "unknown"},
            "parser": {"state": "unknown"},
            "adapter": {"state": "unknown"},
            "route": "opencode_session",
        }
        with self.assertRaises(RecordValidationError):
            validate_version_set(vs, consumed=("adapter",))
        self.assertIn("model_derived", CONTEXT_KINDS)


class TaskProposalAssociationTest(RuntimeTestBase):
    """Repair R1: proposals are scoped to the logical persisted task id."""

    def _two_identical_tasks(self):
        store = self.new_store()
        self.addCleanup(store.close)
        artifact, _ = self.seed_revision(store)
        runtime = RuntimeRepository(store)
        task, cfg = self.make_task(store, artifact)
        r1 = runtime.record_task(task, request_id="one", configuration=cfg)
        r2 = runtime.record_task(task, request_id="two", configuration=cfg)
        return store, runtime, cfg, r1, r2

    def test_two_identical_tasks_keep_independent_proposals(self) -> None:
        _store, runtime, cfg, r1, r2 = self._two_identical_tasks()
        self.assertTrue(r1.task_id != r2.task_id)
        self.assertEqual(r1.fingerprint, r2.fingerprint)  # same logical task content
        p1 = runtime.record_proposal(r1, {"content": {"n": 1}}, configuration=cfg)
        p2 = runtime.record_proposal(r2, {"content": {"n": 1}}, configuration=cfg)
        # Same machine proposal id, but a distinct task-scoped record id, and the
        # association is to each task — never lost or relabelled.
        self.assertEqual(p1.proposal_id, p2.proposal_id)
        self.assertNotEqual(p1.record_id, p2.record_id)
        self.assertEqual(p1.task_id, r1.task_id)
        self.assertEqual(p2.task_id, r2.task_id)
        self.assertEqual(
            [p.record_id for p in runtime.proposals_for_task(r2.task_id)],
            [p2.record_id],
        )
        self.assertEqual(
            [p.record_id for p in runtime.proposals_for_task(r1.task_id)],
            [p1.record_id],
        )
        self.assertEqual(len(runtime.list_proposals()), 2)

    def test_same_task_replay_is_idempotent(self) -> None:
        _store, runtime, cfg, r1, _r2 = self._two_identical_tasks()
        first = runtime.record_proposal(r1, {"content": {"n": 1}}, configuration=cfg)
        again = runtime.record_proposal(r1, {"content": {"n": 1}}, configuration=cfg)
        self.assertEqual(first.record_id, again.record_id)
        self.assertEqual(len(runtime.proposals_for_task(r1.task_id)), 1)

    def test_bare_task_with_two_matches_is_refused(self) -> None:
        store, runtime, cfg, _r1, _r2 = self._two_identical_tasks()
        artifact = ArtifactRepository(store).list_artifacts()[0]
        task, _cfg = self.make_task(store, artifact)
        with self.assertRaises(RuntimeValidationError):
            runtime.record_proposal(task, {"content": {"n": 1}}, configuration=cfg)


class ReadIntegrityDenialTest(RuntimeTestBase):
    """Repair R1: every read/replay path structurally revalidates and denies."""

    def _populated(self):
        store = self.new_store()
        self.addCleanup(store.close)
        artifact, _ = self.seed_revision(store)
        input_artifact, _ = self.seed_revision(
            store, b"input-bytes", artifact_id="art_in", revision_id="rev_in"
        )
        runtime = RuntimeRepository(store)
        snap = runtime.record_configuration(_config())
        runtime.record_session_mapping(
            service_instance_id="svc_1",
            clean_location=str(self.tmp / "clean"),
            session_id="ses_1",
            role="writer",
            model="openrouter/anthropic/claude-sonnet-5-5#high",
            configuration=_config(),
        )
        task, cfg = self.make_task(
            store,
            artifact,
            input_refs=[
                {
                    "project_id": store.project_id,
                    "artifact_id": input_artifact.artifact_id,
                    "revision_id": "rev_in",
                    "digest": _bare(b"input-bytes"),
                }
            ],
        )
        record = runtime.record_task(task, request_id="req-1", configuration=cfg)
        proposal = runtime.record_proposal(
            record, {"content": {"finding": "x"}}, configuration=cfg
        )
        return store, runtime, snap, record, proposal

    def test_integrity_ok_and_exported_validator(self) -> None:
        store, runtime, _snap, _record, _proposal = self._populated()
        self.assertEqual(runtime.verify_all_integrity(), [])
        self.assertEqual(store_integrity_problems(store.connection, store.project_id), [])
        self.assertTrue(verify_store_integrity(store.connection, store.project_id))

    def test_tampered_config_link_in_session_is_denied(self) -> None:
        store, runtime, _snap, _record, _proposal = self._populated()
        conn = store.connection
        conn.execute("DROP TRIGGER runtime_session_mapping_no_update")
        conn.execute(
            "UPDATE runtime_session_mapping SET config_digest = ? WHERE project_id = ?",
            ("0" * 64, store.project_id),
        )
        with self.assertRaises(RuntimeCorruptionError):
            runtime.get_session_mapping_for("ses_1")
        with self.assertRaises(RuntimeCorruptionError):
            runtime.list_session_mappings()
        self.assertTrue(runtime.verify_all_integrity())

    def test_tampered_task_projection_is_denied(self) -> None:
        store, runtime, _snap, record, _proposal = self._populated()
        conn = store.connection
        conn.execute("DROP TRIGGER runtime_bounded_task_no_update")
        conn.execute(
            "UPDATE runtime_bounded_task SET prompt_version = 'tampered' "
            "WHERE project_id = ? AND task_id = ?",
            (store.project_id, record.task_id),
        )
        with self.assertRaises(RuntimeCorruptionError):
            runtime.get_task(record.task_id)
        with self.assertRaises(RuntimeCorruptionError):
            runtime.get_task_by_request("req-1")
        self.assertTrue(runtime.verify_all_integrity())

    def test_tampered_child_input_row_is_denied(self) -> None:
        store, runtime, _snap, record, _proposal = self._populated()
        conn = store.connection
        conn.execute("DROP TRIGGER runtime_task_input_no_update")
        conn.execute(
            "UPDATE runtime_task_input SET digest = ? WHERE project_id = ? "
            "AND task_id = ?",
            ("0" * 64, store.project_id, record.task_id),
        )
        with self.assertRaises(RuntimeCorruptionError):
            runtime.get_task(record.task_id)
        self.assertTrue(runtime.verify_all_integrity())

    def test_tampered_proposal_provenance_is_denied(self) -> None:
        store, runtime, _snap, _record, proposal = self._populated()
        conn = store.connection
        conn.execute("DROP TRIGGER runtime_proposal_no_update")
        conn.execute(
            "UPDATE runtime_proposal SET payload_digest = ? WHERE project_id = ? "
            "AND record_id = ?",
            ("0" * 64, store.project_id, proposal.record_id),
        )
        with self.assertRaises(RuntimeCorruptionError):
            runtime.get_proposal(proposal.record_id)
        with self.assertRaises(RuntimeCorruptionError):
            runtime.proposals_for_task(proposal.task_id)
        self.assertTrue(runtime.verify_all_integrity())

    def test_tampered_config_reuse_is_denied(self) -> None:
        # A corrupt config row must not be silently reused as an idempotent write.
        store, runtime, snap, _record, _proposal = self._populated()
        conn = store.connection
        conn.execute("DROP TRIGGER runtime_config_snapshot_no_update")
        # Corrupt the canonical bytes while keeping the (now invalid) digest, so a
        # same-bytes replay would find and try to reuse the corrupt row.
        conn.execute(
            "UPDATE runtime_config_snapshot SET canonical = ? WHERE project_id = ? "
            "AND snapshot_id = ?",
            (b'{"schema_version":1,"roles":{}}', store.project_id, snap.snapshot_id),
        )
        with self.assertRaises(RuntimeCorruptionError):
            runtime.record_configuration(_config())

    def test_historical_task_read_survives_head_change(self) -> None:
        # Audit reads must keep working after a legitimate current-revision change.
        store, runtime, _snap, record, proposal = self._populated()
        artifact_id = record.target["artifact_id"]
        repo = ArtifactRepository(store)
        repo.import_revision(
            artifact_id=artifact_id,
            content=b"second-generation",
            origin_locator="import:synthetic",
            rights=_rights(
                store.project_id, artifact_id, "rev_2",
                rights_id=f"RIGHTS-{artifact_id}-r2",
            ),
            revision_id="rev_2",
            parent_revision_id="rev_1",
        )
        # The historical task and its proposal remain readable/verifiable.
        reread = runtime.get_task(record.task_id)
        self.assertEqual(reread.target["revision_id"], "rev_1")
        self.assertEqual(runtime.verify_all_integrity(), [])
        self.assertTrue(runtime.verify_proposal(proposal.record_id))
        # A NEW task pinned to the now-stale rev_1 is refused.
        stale, cfg = self.make_task(store, repo.get_artifact(artifact_id), target_revision="rev_1")
        with self.assertRaises(RuntimeValidationError):
            runtime.record_task(stale, request_id="req-2", configuration=cfg)

    def test_outdated_input_fails_closed(self) -> None:
        store = self.new_store()
        self.addCleanup(store.close)
        artifact, _ = self.seed_revision(store)
        input_artifact, _ = self.seed_revision(
            store, b"input-bytes", artifact_id="art_in", revision_id="rev_in"
        )
        repo = ArtifactRepository(store)
        # A newer revision of the input artifact supersedes rev_in.
        repo.import_revision(
            artifact_id=input_artifact.artifact_id,
            content=b"newer-input",
            origin_locator="import:synthetic",
            rights=_rights(
                store.project_id, input_artifact.artifact_id, "rev_in2",
                rights_id=f"RIGHTS-{input_artifact.artifact_id}-2",
            ),
            revision_id="rev_in2",
            parent_revision_id="rev_in",
        )
        task, cfg = self.make_task(
            store,
            artifact,
            input_refs=[
                {
                    "project_id": store.project_id,
                    "artifact_id": input_artifact.artifact_id,
                    "revision_id": "rev_in",
                    "digest": _bare(b"input-bytes"),
                }
            ],
        )
        with self.assertRaises(RuntimeValidationError):
            RuntimeRepository(store).record_task(task, request_id="r", configuration=cfg)


class IntegrityHardeningTest(RuntimeTestBase):
    """Repair R2: fail-closed integrity for existing durable runtime mappings.

    The R1 read/replay validators re-derived a proposal's *fields* but never
    compared the stored canonical bytes with the re-derived mapping, so a
    tampered canonical (an added ``actor``/``authority`` field, or a changed
    canonical role/model/target) whose ``stored_digest`` was recomputed survived.
    These tests pin the exact canonical/provenance equality, the re-derived
    ``task_id``/``mapping_id``, malformed-row normalization, and static (never
    id/content-echoing) integrity problems.
    """

    def _populated(self):
        store = self.new_store()
        self.addCleanup(store.close)
        artifact, _ = self.seed_revision(store)
        runtime = RuntimeRepository(store)
        mapping = runtime.record_session_mapping(
            service_instance_id="svc_1",
            clean_location=str(self.tmp / "clean"),
            session_id="ses_1",
            role="writer",
            model="openrouter/anthropic/claude-sonnet-5-5#high",
            configuration=_config(),
        )
        task, cfg = self.make_task(store, artifact)
        record = runtime.record_task(task, request_id="req-1", configuration=cfg)
        proposal = runtime.record_proposal(
            record, {"content": {"finding": "x"}}, configuration=cfg
        )
        return store, runtime, mapping, record, proposal

    def _rewrite_proposal_canonical(self, store, record_id, mutate):
        """Drop immutability, mutate the canonical JSON, and re-pin the digest.

        Simulates an attacker who can write the row directly and fixes the
        stored digest to match the tampered bytes.
        """
        conn = store.connection
        conn.execute("DROP TRIGGER runtime_proposal_no_update")
        row = conn.execute(
            "SELECT canonical FROM runtime_proposal WHERE record_id = ?",
            (record_id,),
        ).fetchone()
        data = json.loads(bytes(row["canonical"]).decode("utf-8"))
        mutate(data)
        canonical = json.dumps(
            data, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
        conn.execute(
            "UPDATE runtime_proposal SET canonical = ?, stored_digest = ? "
            "WHERE record_id = ?",
            (canonical, hashlib.sha256(canonical).hexdigest(), record_id),
        )

    def test_tampered_proposal_canonical_authority_is_denied(self) -> None:
        store, runtime, _mapping, _record, proposal = self._populated()

        def add_authority(data):
            # The machine proposal tries to construct authority it cannot have.
            data["actor"] = {"actor_kind": "human", "actor_id": "owner"}
            data["authority"] = "owner_authenticated"
            data["owner_auth_ref"] = "auth_synthetic"

        self._rewrite_proposal_canonical(store, proposal.record_id, add_authority)
        with self.assertRaises(RuntimeCorruptionError):
            runtime.get_proposal(proposal.record_id)
        with self.assertRaises(RuntimeCorruptionError):
            runtime.proposals_for_task(proposal.task_id)
        with self.assertRaises(RuntimeCorruptionError):
            runtime.list_proposals()
        self.assertTrue(runtime.verify_all_integrity())

    def test_tampered_proposal_canonical_role_model_target_is_denied(self) -> None:
        store, runtime, _mapping, _record, proposal = self._populated()

        def relabel(data):
            data["role"] = "critic"
            data["model"] = {
                "provider_id": "openrouter",
                "model_id": "evil/other",
                "variant": "high",
            }
            data["target"] = {
                "project_id": data["target"]["project_id"],
                "artifact_id": "art_forged",
                "revision_id": "rev_forged",
            }

        self._rewrite_proposal_canonical(store, proposal.record_id, relabel)
        with self.assertRaises(RuntimeCorruptionError):
            runtime.get_proposal(proposal.record_id)
        self.assertTrue(runtime.verify_all_integrity())

    def test_tampered_task_id_is_denied(self) -> None:
        store = self.new_store()
        self.addCleanup(store.close)
        artifact, _ = self.seed_revision(store)
        runtime = RuntimeRepository(store)
        task, cfg = self.make_task(store, artifact)
        record = runtime.record_task(task, request_id="req-1", configuration=cfg)
        conn = store.connection
        conn.execute("DROP TRIGGER runtime_bounded_task_no_update")
        conn.execute(
            "UPDATE runtime_bounded_task SET task_id = ? WHERE task_id = ?",
            ("rtask_" + "0" * 32, record.task_id),
        )
        with self.assertRaises(RuntimeCorruptionError):
            runtime.get_task_by_request("req-1")
        with self.assertRaises(RuntimeCorruptionError):
            runtime.list_tasks()
        self.assertTrue(runtime.verify_all_integrity())

    def test_tampered_session_mapping_id_is_denied(self) -> None:
        store, runtime, mapping, _record, _proposal = self._populated()
        conn = store.connection
        conn.execute("DROP TRIGGER runtime_session_mapping_no_update")
        conn.execute(
            "UPDATE runtime_session_mapping SET mapping_id = ? WHERE mapping_id = ?",
            ("rmap_" + "0" * 32, mapping.mapping_id),
        )
        with self.assertRaises(RuntimeCorruptionError):
            runtime.get_session_mapping_for("ses_1")
        with self.assertRaises(RuntimeCorruptionError):
            runtime.list_session_mappings()
        self.assertTrue(runtime.verify_all_integrity())

    def test_malformed_rows_normalize_to_corruption_error(self) -> None:
        # A malformed stored row must fail closed as RuntimeCorruptionError on the
        # read path, never leak RuntimeValidationError or a raw exception.
        store, runtime, _mapping, record, _proposal = self._populated()
        conn = store.connection

        conn.execute("DROP TRIGGER runtime_session_mapping_no_update")
        conn.execute(
            "UPDATE runtime_session_mapping SET created_at_utc = ? WHERE session_id = ?",
            ("not-a-timestamp", "ses_1"),
        )
        with self.assertRaises(RuntimeCorruptionError):
            runtime.get_session_mapping_for("ses_1")

        conn.execute("DROP TRIGGER runtime_bounded_task_no_update")
        conn.execute(
            "UPDATE runtime_bounded_task SET fingerprint = ? WHERE task_id = ?",
            ("not-hex", record.task_id),
        )
        with self.assertRaises(RuntimeCorruptionError):
            runtime.get_task(record.task_id)

    def test_integrity_problems_are_static_and_do_not_echo_ids(self) -> None:
        store, runtime, _mapping, record, _proposal = self._populated()
        conn = store.connection
        conn.execute("DROP TRIGGER runtime_bounded_task_no_update")
        conn.execute(
            "UPDATE runtime_bounded_task SET request_id = ? WHERE task_id = ?",
            ("SYNTHETIC_CANARY_REQUEST", record.task_id),
        )
        problems = store_integrity_problems(conn, store.project_id)
        self.assertTrue(problems)
        for problem in problems:
            self.assertNotIn("SYNTHETIC_CANARY_REQUEST", problem)
            self.assertNotIn(record.task_id, problem)
            self.assertNotIn(store.project_id, problem)

    def test_replay_conflict_messages_are_static(self) -> None:
        store = self.new_store()
        self.addCleanup(store.close)
        artifact, _ = self.seed_revision(store)
        runtime = RuntimeRepository(store)
        task, cfg = self.make_task(store, artifact)
        runtime.record_task(task, request_id="req-canary", configuration=cfg)
        other, _ = self.make_task(store, artifact, step_limit=3)
        try:
            runtime.record_task(other, request_id="req-canary", configuration=cfg)
        except RuntimeConflictError as exc:
            self.assertNotIn("req-canary", str(exc))
        else:  # pragma: no cover - the divergent request must conflict
            self.fail("divergent request accepted")

        runtime.record_session_mapping(
            service_instance_id="svc_1",
            clean_location=str(self.tmp / "clean"),
            session_id="ses_canary",
            role="writer",
            model="openrouter/anthropic/claude-sonnet-5-5#high",
            configuration=cfg,
        )
        try:
            runtime.record_session_mapping(
                service_instance_id="svc_2",
                clean_location=str(self.tmp / "clean"),
                session_id="ses_canary",
                role="writer",
                model="openrouter/anthropic/claude-sonnet-5-5#high",
                configuration=cfg,
            )
        except RuntimeConflictError as exc:
            self.assertNotIn("ses_canary", str(exc))
        else:  # pragma: no cover - the divergent session must conflict
            self.fail("divergent session accepted")


class AtomicityAndTimestampTest(RuntimeTestBase):
    def test_no_writes_on_proposal_failure(self) -> None:
        store = self.new_store()
        self.addCleanup(store.close)
        artifact, _ = self.seed_revision(store)
        runtime = RuntimeRepository(store)
        task, cfg = self.make_task(store, artifact)
        record = runtime.record_task(task, request_id="req-1", configuration=cfg)
        before = store.connection.execute(
            "SELECT COUNT(*) AS n FROM runtime_proposal"
        ).fetchone()["n"]
        with self.assertRaises(ProposalError):
            runtime.record_proposal(record, {"content": {"actor": "owner"}}, configuration=cfg)
        self.assertEqual(
            store.connection.execute(
                "SELECT COUNT(*) AS n FROM runtime_proposal"
            ).fetchone()["n"],
            before,
        )
        self.assertEqual(runtime.verify_all_integrity(), [])

    def test_no_task_or_config_rows_on_failed_task_write(self) -> None:
        store = self.new_store()
        self.addCleanup(store.close)
        artifact, _ = self.seed_revision(store)
        runtime = RuntimeRepository(store)
        bad, cfg = self.make_task(store, artifact, target_revision="rev_missing")
        with self.assertRaises(RuntimeValidationError):
            runtime.record_task(bad, request_id="r-bad", configuration=cfg)
        # Neither the task row nor its just-inserted config snapshot survive the
        # rolled-back transaction.
        self.assertEqual(runtime.list_tasks(), [])
        self.assertEqual(
            store.connection.execute(
                "SELECT COUNT(*) AS n FROM runtime_bounded_task"
            ).fetchone()["n"],
            0,
        )
        self.assertEqual(
            store.connection.execute(
                "SELECT COUNT(*) AS n FROM runtime_config_snapshot"
            ).fetchone()["n"],
            0,
        )

    def test_non_timestamp_created_at_is_rejected(self) -> None:
        store = self.new_store()
        self.addCleanup(store.close)
        runtime = RuntimeRepository(store)
        with self.assertRaises(RuntimeValidationError):
            runtime.record_configuration(_config(), created_at_utc="not-a-timestamp")
        with self.assertRaises(RuntimeValidationError):
            runtime.record_configuration(
                _config(), created_at_utc="2026-10-27T00:00:00"  # no offset
            )
        # A valid UTC timestamp is accepted.
        snap = runtime.record_configuration(
            _config(), created_at_utc="2026-10-27T00:00:00Z"
        )
        self.assertEqual(snap.created_at_utc, "2026-10-27T00:00:00Z")

    def test_non_rfc3339_timestamp_shapes_are_rejected(self) -> None:
        # datetime.fromisoformat tolerates these shapes; the intended stored
        # format (date T time seconds + explicit UTC) does not.
        store = self.new_store()
        self.addCleanup(store.close)
        runtime = RuntimeRepository(store)
        rejected = (
            "2026-10-27 00:00:00+00:00",   # space separator
            "20261027T000000+00:00",       # basic layout, no separators
            "2026-10-27T00:00Z",           # missing seconds
            "2026-10-27T00:00:00-00:00",   # negative zero offset
            "2026-10-27T00:00:00",         # no offset
            "2026-10-27",                  # date only
        )
        for value in rejected:
            with self.assertRaises(RuntimeValidationError):
                runtime.record_configuration(_config(), created_at_utc=value)
        accepted = (
            "2026-10-27T00:00:00Z",
            "2026-10-27T00:00:00+00:00",
            "2026-10-27T00:00:00.500000+00:00",
        )
        for value in accepted:
            fresh = self.new_store("ts_" + value.replace(":", "").replace("+", "p"))
            self.addCleanup(fresh.close)
            snap = RuntimeRepository(fresh).record_configuration(
                _config(), created_at_utc=value
            )
            self.assertEqual(snap.created_at_utc, value)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
