"""Backup/verify/restore coverage for the schema-8 decision request intent (T8c).

These tests extend the private backup contract in
``src/review_article_agent/storage/backup.py`` to the schema-8 immutable
pre-dispatch Jev decision request intent (``storage/decision_intents.py``):

* a backup roundtrips a **reserved**, **submitted**, and **unknown** direct
  dispatch's intent with the exact target/question refs, consumed versions,
  request digest/bytes/bound/pricing/reserve, and canonical bytes;
* a legitimate intent whose target head has since advanced still verifies
  (history is preserved, never "repaired");
* restore neither executes, resumes, nor retries a recorded intent;
* the schema-8 inner hook re-validates the intent read-only, so an archive whose
  outer database hash was rewritten still fails closed on a tampered column,
  canonical digest, canonical request wire, parent link, question-definition ref,
  or incomplete question set;
* the tamper drops and exactly restores the intent triggers, proving the refusal
  comes from the inner checker rather than the schema comparison;
* read-only verification never changes the snapshot bytes;
* a dangling or foreign-project intent row is refused by the inner checker and a
  corrupt intent archive is refused **before** any restore registration;
* a schema-7 predecessor snapshot still verifies (the schema-8 hook does not run
  below its threshold), and an upgraded schema-7 store preserves its prior rows;
* the hook table is threshold-driven and ordered, so a later schema can append its
  own validator without editing the schema-8 code (not hardcoded to "current 8");
* the private intent canonical includes the bounded submitted request state (which
  may be source text), the actual backup/restore preserves it, and the **public**
  manifest and static verification errors do not carry that state; no credential,
  capability, or authority is automatically serialized. The archive is owner-private
  (0o700/0o600) and is never published or redistributed.

Synthetic data only (no secrets, no licensed source text, no credentials, no
network, no model calls, no paid calls). The synthetic "source state" canary below
is a deliberate test fixture that models caller-submitted request state; it is not
a real secret and no real licensed source text is uploaded.

Run with::

    PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_decision_intent_backup.py'
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from review_article_agent.domain.records import canonical_json  # noqa: E402
from review_article_agent.runtime import jev  # noqa: E402
from review_article_agent.runtime.dispatch import (  # noqa: E402
    RuntimeDispatchRepository,
    runtime_dispatch_integrity_problems,
)
from review_article_agent.runtime.jobs import JobRepository  # noqa: E402
from review_article_agent.runtime.roles import (  # noqa: E402
    BoundedTask,
    RoleConfiguration,
)
from review_article_agent.storage import (  # noqa: E402
    ArtifactRepository,
    ProjectStore,
    Registry,
    paths,
    schema,
)
from review_article_agent.storage import backup as backup_mod  # noqa: E402
from review_article_agent.storage.agent_runtime import (  # noqa: E402
    RuntimeRepository,
    validate_version_set,
)
from review_article_agent.storage.backup import (  # noqa: E402
    MANIFEST_FILENAME,
    SNAPSHOT_FILENAME,
    BackupError,
    create_backup,
    restore_backup,
    verify_backup,
)
from review_article_agent.storage.decision_intents import (  # noqa: E402
    DecisionIntentRepository,
    decision_intent_integrity_problems,
)

_OPS = ("acquisition", "storage", "redistribution", "hosted_transfer")

#: A canary in the target artifact's private BLOB content. The private intent
#: canonical records only the submitted *state*, not arbitrary artifact bytes, so
#: this artifact canary must not appear in the intent canonical or the manifest.
_SOURCE_CANARY = b"PRIVATE-SOURCE-CANARY-8842-target-bytes"

#: A distinct synthetic canary used as the Jev request *state*. Unlike arbitrary
#: artifact bytes, the bounded request state IS recorded verbatim in the private
#: intent canonical; the public manifest and static errors must not carry it.
_STATE_CANARY = b"SUBMITTED-STATE-CANARY-7731-candidate-abstract"

PROMPT_VERSION = "screen/1"

_DIG_A = "a" * 64
_DIG_B = "b" * 64
_DIG_C = "c" * 64

#: (question_id, kind, instructions, options, levels, criteria)
_SPECS = (
    (
        "is_original_report",
        "predicate",
        "Is it an original report?",
        (),
        (),
        None,
    ),
    (
        "study_design",
        "choice",
        "Which study design?",
        ("randomised", "observational"),
        (),
        {
            "randomised": "Randomised trial",
            "observational": "Observational study",
        },
    ),
    (
        "free_text",
        "box",
        "Explain your reasoning.",
        (),
        (),
        None,
    ),
)


def _rights(project_id, artifact_id, revision_id):
    """A synthetic, fully valid RightsRecord v1 bound to one exact revision."""
    return {
        "rights_id": f"RIGHTS-{artifact_id}-{revision_id}",
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
                }
            },
        }
    )


def _hexlit(data: bytes) -> str:
    """A SQLite ``X'..'`` blob literal."""
    return "X'" + data.hex() + "'"


class IntentBackupBase(unittest.TestCase):
    """A standalone minimal intent fixture (no network/model/paid call)."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="raa-intent-backup-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.store = ProjectStore.create_at(
            paths.app_dir_for(str(self.tmp / "p")), paths.new_project_id()
        )
        self.addCleanup(self.store.close)
        self.artifacts = ArtifactRepository(self.store)
        self.runtime = RuntimeRepository(self.store)
        self.dispatches = RuntimeDispatchRepository(self.store)
        self.intents = DecisionIntentRepository(self.store)
        self.config = _config()
        self._build_fixture()

    @property
    def pid(self) -> str:
        return self.store.project_id

    # -- fixture ------------------------------------------------------------- #
    def _import(self, content: bytes, *, revision_id: str, parent_revision_id=None):
        artifact = self.artifacts.create_artifact(kind="document")
        revision = self.artifacts.import_revision(
            artifact_id=artifact.artifact_id,
            content=content,
            origin_locator="import:synthetic",
            rights=_rights(self.pid, artifact.artifact_id, revision_id),
            revision_id=revision_id,
            parent_revision_id=parent_revision_id,
        )
        return artifact, revision

    def _build_fixture(self) -> None:
        self.target_artifact, _ = self._import(_SOURCE_CANARY, revision_id="rev_1")
        self.target_ref = {
            "project_id": self.pid,
            "artifact_id": self.target_artifact.artifact_id,
            "revision_id": "rev_1",
        }

        self.questions: list[jev.QuestionDef] = []
        self.qdef_refs: dict[str, dict[str, str]] = {}
        self.crit_refs: dict[str, dict[str, str]] = {}
        input_refs: list[dict[str, str]] = []

        for index, (qid, kind, instructions, options, levels, criteria) in enumerate(
            _SPECS
        ):
            crit_bytes = canonical_json(criteria)
            crit_artifact, _ = self._import(
                crit_bytes, revision_id=f"crit_rev_{index}"
            )
            crit_ref = {
                "project_id": self.pid,
                "artifact_id": crit_artifact.artifact_id,
                "revision_id": f"crit_rev_{index}",
            }
            qdef_json = {
                "question_id": qid,
                "question_kind": kind,
                "instructions": instructions,
                "criteria_ref": crit_ref,
            }
            if kind == "choice":
                qdef_json["options"] = list(options)
            qdef_bytes = canonical_json(qdef_json)
            qdef_artifact, _ = self._import(
                qdef_bytes, revision_id=f"qd_rev_{index}"
            )
            qdef_ref = {
                "project_id": self.pid,
                "artifact_id": qdef_artifact.artifact_id,
                "revision_id": f"qd_rev_{index}",
            }
            self.qdef_refs[qid] = qdef_ref
            self.crit_refs[qid] = crit_ref
            input_refs.append(
                {
                    "project_id": self.pid,
                    "artifact_id": qdef_ref["artifact_id"],
                    "revision_id": qdef_ref["revision_id"],
                    "digest": hashlib.sha256(qdef_bytes).hexdigest(),
                }
            )
            input_refs.append(
                {
                    "project_id": self.pid,
                    "artifact_id": crit_ref["artifact_id"],
                    "revision_id": crit_ref["revision_id"],
                    "digest": hashlib.sha256(crit_bytes).hexdigest(),
                }
            )
            self.questions.append(
                jev.QuestionDef(
                    question_id=qid,
                    question_kind=kind,
                    instructions=instructions,
                    options=options,
                    levels=levels,
                    criteria=criteria,
                )
            )

        bounded = BoundedTask.from_mapping(
            {
                "project_id": self.pid,
                "role": "writer",
                "target": dict(self.target_ref),
                "input_refs": input_refs,
                "scope": ["section:screening"],
                "prompt_version": PROMPT_VERSION,
                "size_limit": 4096,
                "step_limit": 4,
            },
            configuration=self.config,
        )
        self.task = self.runtime.record_task(
            bounded, request_id="task-req", configuration=self.config
        )

        self.plan = jev.plan_dispatch(
            self.questions, "Synthetic candidate abstract text.", reserve_usd="0.25"
        )

        self.versions = {
            "context_kind": "model_derived",
            "input_digest": _DIG_A,
            "model": {
                "state": "known",
                "ref": {"version": "typesafe/jev-1.14-20261014", "digest": _DIG_C},
            },
            "prompt": {
                "state": "known",
                "ref": {"version": PROMPT_VERSION, "digest": "d" * 64},
            },
            "rubric": {
                "state": "known",
                "ref": {"version": "jev-rubric/1", "digest": "e" * 64},
            },
            "parser": {"state": "not_applicable"},
            "adapter": {
                "state": "known",
                "ref": {"version": "jev-adapter/1", "digest": "f" * 64},
            },
            "route": "direct_call",
        }
        version_map = validate_version_set(self.versions)
        self.version_digest = hashlib.sha256(canonical_json(version_map)).hexdigest()

        self.dispatch_record = self.dispatches.reserve_dispatch(
            self.task,
            request_id="call-1",
            route="direct_call",
            stage="screening",
            input_digest=_DIG_A,
            version_digest=self.version_digest,
            reserve_usd="0.25",
            bound_evidence_digest=_DIG_B,
        )

    # -- intent -------------------------------------------------------------- #
    def record_intent(self):
        return self.intents.record_intent(
            self.task,
            self.dispatch_record,
            self.plan,
            question_def_ref=self.qdef_refs,
            criteria_ref=self.crit_refs,
            versions=self.versions,
            consumed=("rubric",),
        )

    # -- backup helpers ------------------------------------------------------ #
    def backup(self, name: str = "backup") -> Path:
        dest = self.tmp / name
        create_backup(self.store, dest)
        return dest

    def _snapshot_conn(self, dest: Path) -> sqlite3.Connection:
        conn = sqlite3.connect(str(dest / SNAPSHOT_FILENAME))
        conn.row_factory = sqlite3.Row
        return conn

    def rewrite_outer_db_hash(self, dest: Path) -> None:
        """Recompute the manifest's database hash (simulates a rewritten hash)."""
        manifest_path = dest / MANIFEST_FILENAME
        manifest = json.loads(manifest_path.read_text())
        data = (dest / SNAPSHOT_FILENAME).read_bytes()
        manifest["database"]["sha256"] = "sha256:" + hashlib.sha256(data).hexdigest()
        manifest["database"]["byte_size"] = len(data)
        manifest_path.write_text(json.dumps(manifest))

    def tamper_db(self, dest: Path, *statements: str) -> None:
        """Execute raw SQL against the snapshot, recreating any dropped trigger."""
        conn = sqlite3.connect(str(dest / SNAPSHOT_FILENAME))
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

    def forge_intent_canonical(self, dest: Path, mutate) -> None:
        """Rewrite a stored intent's canonical bytes **consistently**.

        The canonical bytes, ``content_digest`` and the affected projected columns
        are kept in agreement, so the forgery is only detectable by re-deriving the
        actual independent pins (parent links, refs, request wire, question set).
        """
        conn = sqlite3.connect(str(dest / SNAPSHOT_FILENAME))
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?",
                ("decision_request_intent_no_update",),
            ).fetchone()
            trigger_sql = row["sql"] if row else None
            conn.execute("DROP TRIGGER decision_request_intent_no_update")
            stored = conn.execute(
                "SELECT canonical FROM decision_request_intent"
            ).fetchone()
            mapping = json.loads(bytes(stored["canonical"]).decode("utf-8"))
            mutate(mapping)
            canonical = canonical_json(mapping)
            conn.execute(
                "UPDATE decision_request_intent SET canonical = ?, content_digest = ?, "
                "stage = ?, task_id = ?, request_sha256 = ?, request_bytes = ?, "
                "question_count = ?",
                (
                    canonical,
                    hashlib.sha256(canonical).hexdigest(),
                    mapping["stage"],
                    mapping["task_id"],
                    mapping["request_sha256"],
                    mapping["request_bytes"],
                    len(mapping["questions"]),
                ),
            )
            if trigger_sql:
                conn.execute(trigger_sql)
            conn.commit()
        finally:
            conn.close()

    def tamper_qdef_revision(self, dest: Path, qid: str, content: bytes) -> None:
        """Rewrite a question-definition revision's bytes *and* metadata consistently."""
        ref = self.qdef_refs[qid]
        digest = hashlib.sha256(content).hexdigest()
        self.tamper_db(
            dest,
            "DROP TRIGGER revision_no_update",
            "UPDATE revision SET content = " + _hexlit(content) + ", content_sha256 = '"
            + digest + "', byte_size = " + str(len(content))
            + " WHERE project_id = '" + self.pid + "' AND artifact_id = '"
            + ref["artifact_id"] + "' AND revision_id = '" + ref["revision_id"] + "'",
        )

    def insert_intent_copy(self, dest: Path, **replacements: object) -> None:
        """Insert a copy of the stored intent row with selected columns replaced."""
        conn = sqlite3.connect(str(dest / SNAPSHOT_FILENAME))
        conn.row_factory = sqlite3.Row
        try:
            columns = [
                row["name"]
                for row in conn.execute(
                    "PRAGMA table_info(decision_request_intent)"
                ).fetchall()
            ]
            source = conn.execute(
                "SELECT * FROM decision_request_intent LIMIT 1"
            ).fetchone()
            values = [replacements.get(column, source[column]) for column in columns]
            conn.execute(
                "INSERT INTO decision_request_intent ("
                + ", ".join(columns)
                + ") VALUES ("
                + ", ".join("?" for _ in columns)
                + ")",
                values,
            )
            conn.commit()
        finally:
            conn.close()

    # -- restore helpers ----------------------------------------------------- #
    def new_registry(self) -> Registry:
        return Registry(self.tmp / "state")

    def _restore(self, dest: Path, name: str):
        registry = self.new_registry()
        target = self.tmp / name
        target.mkdir()
        info = restore_backup(dest, target, registry)
        restored = ProjectStore.open_at(
            paths.app_dir_for(str(target)), expected_project_id=info.project_id
        )
        self.addCleanup(restored.close)
        return restored, info

    def _assert_rewritten_hash_rejected(self, dest: Path) -> None:
        self.rewrite_outer_db_hash(dest)
        with self.assertRaises(BackupError):
            verify_backup(dest)


class RoundTripTest(IntentBackupBase):
    def test_roundtrip_reserved_intent_preserves_exact_request(self) -> None:
        record = self.record_intent()
        self.assertEqual(self.dispatches.get_dispatch(record.dispatch_id).state, "reserved")

        dest = self.backup()
        report = verify_backup(dest)
        self.assertTrue(report["ok"])
        self.assertEqual(report["store_schema_version"], schema.STORE_SCHEMA_VERSION)
        # The recorded schema is at least the schema-8 intent threshold, so the
        # version-specific intent hook remains active for this snapshot (dynamic,
        # not a literal "8" masquerading as the current store schema).
        self.assertGreaterEqual(
            report["store_schema_version"], backup_mod._INTENT_SCHEMA_VERSION
        )

        restored, info = self._restore(dest, "restored-reserved")
        self.assertEqual(info.project_id, self.pid)

        restored_intents = DecisionIntentRepository(restored)
        got = restored_intents.get_intent(record.dispatch_id)
        self.assertIsNotNone(got)
        # Exact canonical bytes and every pinned request/question field survive.
        self.assertEqual(got.canonical, record.canonical)
        self.assertEqual(got.digest, record.digest)
        self.assertEqual(got.to_mapping(), record.to_mapping())
        self.assertEqual(tuple(got.target.items()), tuple(record.target.items()))
        self.assertEqual(got.question_ids(), record.question_ids())
        self.assertEqual(
            got.supported_question_ids(), record.supported_question_ids()
        )
        self.assertEqual(
            got.unsupported_question_ids(), record.unsupported_question_ids()
        )
        for restored_question, original in zip(got.questions, record.questions):
            self.assertEqual(
                dict(restored_question.question_def_ref),
                dict(original.question_def_ref),
            )
            self.assertEqual(
                dict(restored_question.criteria_ref), dict(original.criteria_ref)
            )
            self.assertEqual(
                restored_question.question_def_digest,
                original.question_def_digest,
            )
        self.assertEqual(dict(got.versions), dict(record.versions))
        self.assertEqual(got.consumed_versions, record.consumed_versions)
        self.assertEqual(dict(got.plan_request), dict(record.plan_request))
        self.assertEqual(got.reserve_usd, "0.25")
        self.assertEqual(got.cost_upper_bound_usd, record.cost_upper_bound_usd)
        self.assertEqual(got.pricing_version, jev.PRICING_VERSION)
        self.assertEqual(got.request_sha256, self.plan.request_sha256)
        self.assertEqual(got.request_bytes, self.plan.request_bytes)
        self.assertEqual(
            decision_intent_integrity_problems(restored.connection, info.project_id), []
        )
        # The dispatch is still only reserved: restore never submits or executes.
        got_dispatch = RuntimeDispatchRepository(restored).get_dispatch(
            record.dispatch_id
        )
        self.assertEqual(got_dispatch.state, "reserved")
        self.assertIsNone(got_dispatch.result_digest)
        self.assertEqual(
            runtime_dispatch_integrity_problems(restored.connection, info.project_id),
            [],
        )

    def test_roundtrip_submitted_intent_preserves_request_without_execution(self) -> None:
        record = self.record_intent()
        submitted = self.dispatches.mark_submitted(record.dispatch_id)
        self.assertEqual(submitted.state, "submitted")
        self.assertIsNone(submitted.result_digest)

        dest = self.backup()
        self.assertTrue(verify_backup(dest)["ok"])
        restored, info = self._restore(dest, "restored-submitted")

        got = DecisionIntentRepository(restored).get_intent(record.dispatch_id)
        self.assertEqual(got.canonical, record.canonical)
        self.assertEqual(got.question_ids(), record.question_ids())
        self.assertEqual(dict(got.plan_request), dict(record.plan_request))
        self.assertEqual(got.reserve_usd, "0.25")

        got_dispatch = RuntimeDispatchRepository(restored).get_dispatch(
            record.dispatch_id
        )
        self.assertEqual(got_dispatch.state, "submitted")
        self.assertIsNone(got_dispatch.result_digest)
        self.assertIsNone(got_dispatch.result_kind)
        self.assertIsNone(got_dispatch.cost_amount_usd)
        # No result/observation row was invented by the restore.
        count = restored.connection.execute(
            "SELECT COUNT(*) AS n FROM runtime_dispatch_state "
            "WHERE dispatch_id = ? AND state IN ('admitted', 'completed', 'failed')",
            (record.dispatch_id,),
        ).fetchone()["n"]
        self.assertEqual(count, 0)

    def test_roundtrip_unknown_intent_keeps_unknown_and_is_not_retried(self) -> None:
        record = self.record_intent()
        self.dispatches.mark_submitted(record.dispatch_id)
        recovered = self.dispatches.recover(record.dispatch_id)
        self.assertEqual(recovered.state, "unknown")
        self.assertEqual(recovered.cost_state, "unknown")
        self.assertIsNone(recovered.cost_amount_usd)

        dest = self.backup()
        self.assertTrue(verify_backup(dest)["ok"])
        restored, _ = self._restore(dest, "restored-unknown")

        got = DecisionIntentRepository(restored).get_intent(record.dispatch_id)
        self.assertEqual(got.canonical, record.canonical)
        self.assertEqual(got.reserve_usd, "0.25")

        got_dispatch = RuntimeDispatchRepository(restored).get_dispatch(
            record.dispatch_id
        )
        self.assertEqual(got_dispatch.state, "unknown")
        self.assertEqual(got_dispatch.cost_state, "unknown")
        self.assertIsNone(got_dispatch.cost_amount_usd)
        self.assertEqual(got_dispatch.current_fence, recovered.current_fence)
        self.assertIsNone(got_dispatch.result_digest)
        self.assertEqual(
            runtime_dispatch_integrity_problems(restored.connection, self.pid),
            [],
        )

    def test_current_head_changed_history_verifies_and_preserves_intent(self) -> None:
        record = self.record_intent()
        # A later revision moves the target artifact's head past the pinned rev_1.
        self.artifacts.import_revision(
            artifact_id=self.target_ref["artifact_id"],
            content=b"later-target-head-bytes",
            origin_locator="import:synthetic",
            rights=_rights(self.pid, self.target_ref["artifact_id"], "rev_2"),
            revision_id="rev_2",
            parent_revision_id="rev_1",
        )
        heads = self.artifacts.list_revisions(self.target_ref["artifact_id"])
        self.assertIn("rev_2", {revision.revision_id for revision in heads})

        dest = self.backup()
        report = verify_backup(dest)
        self.assertTrue(report["ok"])

        restored, _ = self._restore(dest, "restored-moved-head")
        got = DecisionIntentRepository(restored).get_intent(record.dispatch_id)
        # The intent still pins rev_1, not the moved head; history is preserved.
        self.assertEqual(dict(got.target)["revision_id"], "rev_1")
        self.assertEqual(got.canonical, record.canonical)
        self.assertEqual(
            decision_intent_integrity_problems(restored.connection, self.pid), []
        )

    def test_readonly_verify_does_not_change_snapshot(self) -> None:
        self.record_intent()
        dest = self.backup()
        snapshot = dest / SNAPSHOT_FILENAME
        manifest = dest / MANIFEST_FILENAME
        before = (snapshot.read_bytes(), manifest.read_bytes())
        for _ in range(3):
            self.assertTrue(verify_backup(dest)["ok"])
        after = (snapshot.read_bytes(), manifest.read_bytes())
        self.assertEqual(before, after)

    def test_intent_canonical_includes_bounded_request_state_and_is_owner_private(
        self,
    ) -> None:
        # Model real behavior: the Jev request ``state`` IS the submitted source/
        # state text. Record the intent with a distinct state canary so the evidence
        # is not vacuous.
        state_text = _STATE_CANARY.decode("utf-8")
        self.plan = jev.plan_dispatch(self.questions, state_text, reserve_usd="0.25")
        record = self.record_intent()

        # The PRIVATE intent canonical records the bounded request state verbatim;
        # it may therefore contain source text. It does NOT copy arbitrary target
        # artifact BLOBs (only the bounded state the caller submitted).
        self.assertIn(_STATE_CANARY, record.canonical)
        self.assertNotIn(_SOURCE_CANARY, record.canonical)
        # Boundedness is retained: the request bytes stay within the Jev safe bound,
        # and an over-bound state is refused by the planner rather than truncated.
        self.assertEqual(record.request_bytes, len(self.plan.request_body))
        self.assertLessEqual(record.request_bytes, jev.SAFE_MAX_REQUEST_BYTES)
        with self.assertRaises(jev.JevValidationError):
            jev.plan_dispatch(
                self.questions,
                "x" * (jev.SAFE_MAX_REQUEST_BYTES + 1),
                reserve_usd="0.25",
            )

        dest = self.backup()

        # The actual backup stores the state-bearing intent, and restore preserves it.
        self.assertTrue(verify_backup(dest)["ok"])
        conn = self._snapshot_conn(dest)
        try:
            stored = conn.execute(
                "SELECT canonical FROM decision_request_intent"
            ).fetchone()
            self.assertIn(_STATE_CANARY, bytes(stored["canonical"]))
            # The intent table has no credential/authority projection column at all;
            # no credential or capability is automatically serialized.
            columns = {
                row["name"]
                for row in conn.execute(
                    "PRAGMA table_info(decision_request_intent)"
                ).fetchall()
            }
        finally:
            conn.close()
        for forbidden in (
            "credential",
            "capability",
            "challenge",
            "authority",
            "token",
            "secret",
            "password",
        ):
            self.assertFalse(
                any(forbidden in column.lower() for column in columns), forbidden
            )

        restored, _ = self._restore(dest, "restored-state")
        got = DecisionIntentRepository(restored).get_intent(record.dispatch_id)
        self.assertEqual(got.canonical, record.canonical)
        self.assertIn(_STATE_CANARY, got.canonical)

        # The PUBLIC manifest carries only ids/digests/aggregates: neither the state
        # text nor arbitrary artifact bytes appear in it.
        manifest = (dest / MANIFEST_FILENAME).read_bytes()
        self.assertNotIn(_STATE_CANARY, manifest)
        self.assertNotIn(_SOURCE_CANARY, manifest)

        # A static verification error is value-free: corrupt the intent digest, then
        # rehash the outer database so verification reaches the inner checker. The
        # raised message must not echo the submitted state or the artifact bytes.
        self.tamper_db(
            dest,
            "DROP TRIGGER decision_request_intent_no_update",
            "UPDATE decision_request_intent SET content_digest = '" + "0" * 64 + "'",
        )
        self.rewrite_outer_db_hash(dest)
        with self.assertRaises(BackupError) as caught:
            verify_backup(dest)
        message = str(caught.exception)
        self.assertNotIn(_STATE_CANARY.decode("utf-8"), message)
        self.assertNotIn("SUBMITTED-STATE-CANARY", message)
        self.assertNotIn(_SOURCE_CANARY.decode("utf-8"), message)
        # The error stays a curated static description.
        self.assertIn("do not revalidate", message)

        # The backup directory and its members are owner-private (0o700/0o600) and
        # the archive is never published/redistributed by this module.
        self.assertEqual(stat.S_IMODE(os.stat(dest).st_mode), 0o700)
        for name in (MANIFEST_FILENAME, SNAPSHOT_FILENAME):
            self.assertEqual(
                stat.S_IMODE(os.stat(dest / name).st_mode), 0o600, name
            )


class InnerValidationTest(IntentBackupBase):
    def _backup_with_intent(self):
        record = self.record_intent()
        dest = self.backup()
        return record, dest

    def _assert_hook_rejects(self, dest: Path) -> None:
        conn = self._snapshot_conn(dest)
        try:
            # The tamper dropped and exactly restored the intent triggers, so the
            # canonical schema comparison still passes: the refusal below can only
            # come from the inner checker, not the schema-shape check. Compare
            # against the snapshot's *actual* recorded schema version so the gate
            # is not hardcoded to "8"; the expected current version is asserted
            # separately.
            actual_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
            self.assertEqual(actual_version, schema.STORE_SCHEMA_VERSION)
            backup_mod._assert_known_schema(conn, actual_version)
            with self.assertRaises(BackupError):
                backup_mod._verify_intent_relations(conn, self.pid)
        finally:
            conn.close()
        self._assert_rewritten_hash_rejected(dest)

    def test_column_hash_tamper_rejected_by_inner_checker(self) -> None:
        _, dest = self._backup_with_intent()
        self.tamper_db(
            dest,
            "DROP TRIGGER decision_request_intent_no_update",
            "UPDATE decision_request_intent SET request_sha256 = '" + "0" * 64 + "'",
        )
        # The triggers were restored exactly; only the column disagrees with the
        # canonical bytes. The schema comparison therefore cannot be the reason.
        self._assert_hook_rejects(dest)

    def test_canonical_digest_tamper_rejected_by_inner_checker(self) -> None:
        _, dest = self._backup_with_intent()
        self.tamper_db(
            dest,
            "DROP TRIGGER decision_request_intent_no_update",
            "UPDATE decision_request_intent SET canonical = " + _hexlit(b"{}"),
        )
        self._assert_hook_rejects(dest)

    def test_canonical_wire_tamper_rejected_by_inner_checker(self) -> None:
        _, dest = self._backup_with_intent()

        def mutate(mapping):
            mapping["plan_request"]["questions"]["is_original_report"][
                "instructions"
            ] = "FORGED WIRE INSTRUCTIONS"
            body = canonical_json(mapping["plan_request"])
            mapping["request_sha256"] = hashlib.sha256(body).hexdigest()
            mapping["request_bytes"] = len(body)

        # Self-consistent: canonical, content digest, request digest/bytes and
        # columns all agree; only the rebuilt request wire disagrees.
        self.forge_intent_canonical(dest, mutate)
        self._assert_hook_rejects(dest)

    def test_parent_link_tamper_rejected_by_inner_checker(self) -> None:
        _, dest = self._backup_with_intent()
        # Self-consistent forged stage that no longer matches the parent dispatch.
        self.forge_intent_canonical(
            dest, lambda mapping: mapping.__setitem__("stage", "forged_stage")
        )
        self._assert_hook_rejects(dest)

    def test_question_def_ref_tamper_rejected_by_inner_checker(self) -> None:
        _, dest = self._backup_with_intent()
        # Consistently rewrite the referenced question-definition bytes so the base
        # revision/digest checks still pass; only the intent's pinned ref digest no
        # longer matches.
        self.tamper_qdef_revision(dest, "is_original_report", b"forged-question-def")
        self._assert_hook_rejects(dest)

    def test_incomplete_question_set_tamper_rejected_by_inner_checker(self) -> None:
        _, dest = self._backup_with_intent()

        def drop_supported_question(mapping):
            # Drop a *supported* question so the rebuilt request wire changes while
            # the (unsupported) box question would not.
            dropped = next(
                question
                for question in mapping["questions"]
                if question["question_id"] == "study_design"
            )
            mapping["questions"].remove(dropped)
            mapping["question_ids"].remove(dropped["question_id"])
            mapping["supported_question_ids"].remove(dropped["question_id"])

        self.forge_intent_canonical(dest, drop_supported_question)
        self._assert_hook_rejects(dest)

    def test_dangling_intent_row_rejected_by_inner_checker(self) -> None:
        record, dest = self._backup_with_intent()
        self.insert_intent_copy(
            dest,
            dispatch_id="disp_missing",
            intent_id="intent_missing",
        )
        conn = self._snapshot_conn(dest)
        try:
            with self.assertRaises(BackupError):
                backup_mod._verify_intent_relations(conn, self.pid)
        finally:
            conn.close()
        self.assertTrue(record.dispatch_id)

    def test_foreign_project_intent_row_rejected_by_inner_checker(self) -> None:
        _, dest = self._backup_with_intent()
        self.insert_intent_copy(
            dest,
            project_id="prj_foreign",
            dispatch_id="disp_foreign",
            intent_id="intent_foreign",
        )
        conn = self._snapshot_conn(dest)
        try:
            with self.assertRaises(BackupError):
                backup_mod._verify_intent_relations(conn, self.pid)
        finally:
            conn.close()

    def test_corrupt_intent_archive_rejected_before_registration(self) -> None:
        _, dest = self._backup_with_intent()
        # A self-consistent parent-link forgery survives the schema comparison, the
        # generic FK check, and the outer hash rewrite; only the inner hook refuses.
        self.forge_intent_canonical(
            dest, lambda mapping: mapping.__setitem__("stage", "forged_stage")
        )
        self.rewrite_outer_db_hash(dest)

        registry = self.new_registry()
        target = self.tmp / "restored"
        target.mkdir()
        (target / "unrelated.txt").write_text("keep me")
        with self.assertRaises(BackupError):
            restore_backup(dest, target, registry)
        self.assertEqual(registry.list_projects(), [])
        self.assertFalse(os.path.lexists(paths.app_dir_for(str(target))))
        self.assertEqual((target / "unrelated.txt").read_text(), "keep me")


class SchemaCompatibilityTest(IntentBackupBase):
    def new_schema_store(self, version: int, name: str) -> ProjectStore:
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

    def _seed_predecessor(self, store: ProjectStore):
        repo = ArtifactRepository(store)
        artifact = repo.create_artifact(kind="document")
        repo.import_revision(
            artifact_id=artifact.artifact_id,
            content=b"predecessor-bytes",
            origin_locator="import:synthetic",
            rights=_rights(store.project_id, artifact.artifact_id, "rev_1"),
            revision_id="rev_1",
        )
        job = JobRepository(store).submit("req-job", task_kind="probe")
        return artifact, job

    def test_schema7_snapshot_verifies_and_skips_schema8_hook(self) -> None:
        store = self.new_schema_store(7, "p7")
        artifact, job = self._seed_predecessor(store)
        # The schema-8 intent table does not exist below schema 8. If the intent
        # hook ran at 7 it would fail on the missing table, so a successful verify
        # proves the hook is threshold-gated.
        tables = {
            row["name"]
            for row in store.connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        self.assertNotIn("decision_request_intent", tables)

        dest = self.tmp / "backup7"
        create_backup(store, dest)
        report = verify_backup(dest)
        self.assertTrue(report["ok"])
        self.assertEqual(report["store_schema_version"], 7)
        self.assertTrue(job.job_id)

        # A tampered schema-8-only field is irrelevant at 7; the schema-7 contract
        # still fails closed on its own rows. Here we only prove a clean verify and
        # the preserved artifact.
        registry = Registry(self.tmp / "state")
        target = self.tmp / "restored7"
        target.mkdir()
        info = restore_backup(dest, target, registry)
        restored = ProjectStore.open_at(
            paths.app_dir_for(str(target)), expected_project_id=info.project_id
        )
        self.addCleanup(restored.close)
        self.assertEqual(restored.schema_version, schema.STORE_SCHEMA_VERSION)
        got = ArtifactRepository(restored).read_content(artifact.artifact_id, "rev_1")
        self.assertEqual(got, b"predecessor-bytes")

    def test_schema8_upgrade_from_7_preserves_predecessor_rows(self) -> None:
        store = self.new_schema_store(7, "p7up")
        artifact, job = self._seed_predecessor(store)
        app_dir = store.app_dir
        pid = store.project_id
        store.close()

        upgraded = ProjectStore.open_at(app_dir, expected_project_id=pid)
        self.addCleanup(upgraded.close)
        self.assertEqual(upgraded.schema_version, schema.STORE_SCHEMA_VERSION)

        # Predecessor rows and bytes are preserved exactly; only the new empty
        # tables are added on first open.
        got = ArtifactRepository(upgraded).read_content(artifact.artifact_id, "rev_1")
        self.assertEqual(got, b"predecessor-bytes")
        self.assertEqual(
            JobRepository(upgraded).get(job.job_id).state, "queued"
        )
        intent_count = upgraded.connection.execute(
            "SELECT COUNT(*) AS n FROM decision_request_intent"
        ).fetchone()["n"]
        self.assertEqual(intent_count, 0)
        self.assertEqual(
            decision_intent_integrity_problems(upgraded.connection, pid), []
        )

        dest = self.tmp / "backup-upgraded"
        create_backup(upgraded, dest)
        report = verify_backup(dest)
        self.assertTrue(report["ok"])
        self.assertEqual(report["store_schema_version"], schema.STORE_SCHEMA_VERSION)


class DynamicHookTest(IntentBackupBase):
    def test_hook_table_is_ordered_and_threshold_keyed(self) -> None:
        minimums = [minimum for minimum, _ in backup_mod._SCHEMA_VALIDATORS]
        self.assertEqual(minimums, sorted(minimums))
        self.assertEqual(len(minimums), len(set(minimums)))
        # The newest hook sits at the current supported schema, but the table is a
        # plain ordered threshold table, not a hardcoded "must be exactly current"
        # check. The schema-8 intent hook stays pinned at its own version-specific
        # minimum even after a later checkpoint appends its own validator.
        self.assertEqual(minimums[-1], schema.STORE_SCHEMA_VERSION)
        self.assertIn(8, minimums)
        self.assertEqual(backup_mod._INTENT_SCHEMA_VERSION, 8)
        self.assertIn(backup_mod._INTENT_SCHEMA_VERSION, minimums)
        self.assertEqual(
            backup_mod.SUPPORTED_STORE_SCHEMA_VERSION, schema.STORE_SCHEMA_VERSION
        )
        self.assertEqual(
            backup_mod.SUPPORTED_SNAPSHOT_SCHEMA_VERSIONS,
            frozenset(range(3, schema.STORE_SCHEMA_VERSION + 1)),
        )
        # Every validator is callable and distinct (no accidental duplicate).
        validators = [validator for _, validator in backup_mod._SCHEMA_VALIDATORS]
        self.assertTrue(all(callable(validator) for validator in validators))
        self.assertEqual(len(validators), len(set(validators)))

    def test_a_later_hook_can_append_without_editing_schema8_code(self) -> None:
        called: list[str] = []

        def future_validator(conn, project_id):
            called.append(project_id)

        # A genuinely later checkpoint is CURRENT+1, never a literal "9" that is
        # now itself a real registered hook.
        later = schema.STORE_SCHEMA_VERSION + 1
        extended = backup_mod._SCHEMA_VALIDATORS + ((later, future_validator),)
        with mock.patch.object(backup_mod, "_SCHEMA_VALIDATORS", extended):
            conn = self.store.connection
            # At the current recorded schema the appended later hook does not run...
            backup_mod._verify_schema_specific(
                conn, self.pid, schema.STORE_SCHEMA_VERSION
            )
            self.assertEqual(called, [])
            # ...and at a hypothetical later recorded schema it runs by threshold alone.
            backup_mod._verify_schema_specific(conn, self.pid, later)
            self.assertEqual(called, [self.pid])

    def test_schema8_hook_runs_at_8_and_not_at_7(self) -> None:
        # An empty-schema stand-in proves the dispatch table's semantics without
        # needing real tables: a hook whose minimum is 8 runs for 8 and is skipped
        # for 7.
        called: list[int] = []

        def intent_stand_in(conn, project_id):
            called.append(1)

        extended = ((8, intent_stand_in),)
        with mock.patch.object(backup_mod, "_SCHEMA_VALIDATORS", extended):
            conn = self.store.connection
            backup_mod._verify_schema_specific(conn, self.pid, 7)
            self.assertEqual(called, [])
            backup_mod._verify_schema_specific(conn, self.pid, 8)
            self.assertEqual(called, [1])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
