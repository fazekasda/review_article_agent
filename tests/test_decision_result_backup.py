"""Backup/verify/restore coverage for the schema-9 decision-result observation (T8d).

These tests extend the private backup contract in
``src/review_article_agent/storage/backup.py`` to the schema-9 immutable,
**complete per-question** Jev decision-result observation
(``storage/decision_results.py``):

* a backup roundtrips a complete answered/mixed observation and a whole-call
  failure observation with their exact per-question refs, versions, cost, and
  provider envelope;
* an ``unknown`` cost stays ``unknown`` with no amount (never a silent ``0``), and
  restore neither accepts, executes, nor retries a recorded observation: the
  dispatch keeps its submitted/unknown state and no accepted result is invented;
* the schema-9 inner hook re-validates every observation read-only, so an archive
  whose outer database hash was rewritten still fails closed on a corrupt
  canonical, a column disagreement, a self-consistent partial set, a per-record
  cost or ref change, a parent-link change, or a foreign/orphan row;
* the tamper drops and exactly restores the result triggers, so the exact
  schema-object comparison still passes and the refusal is proven to come from the
  inner checker rather than the schema-shape check;
* read-only verification never changes the snapshot bytes;
* a corrupt observation archive is refused **before** any restore registration;
* a schema-8 predecessor snapshot without the result tables still verifies (the
  schema-9 hook does not run below its threshold);
* the hook table is threshold-driven and ordered, so a later schema can append its
  own validator without editing the schema-9 code.

Redaction honesty
-----------------

The private observation canonical legitimately records **source-derived**
per-question values: the provider envelope, a score ``legend``, and the declared
``options``/``levels`` copied from the question definitions. The referenced
question ``instructions`` and the target source bytes live in the private revision
BLOBs of the same snapshot. This module is **not** a secret scanner and makes **no**
"never contains source" promise: a caller who submits source text into a request
state or artifact content places it in the private archive. The **public** manifest
and static verification errors carry none of that text. The archive is
owner-private (``0o700``/``0o600``) and is never published or redistributed.

Synthetic data only (no real secrets, no licensed source text, no credentials, no
network, no model calls, no paid calls). The canaries below are deliberate test
fixtures, not real secrets.

Run with::

    PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_decision_result_backup.py'
"""

from __future__ import annotations

import hashlib
import json
import os
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
)
from review_article_agent.storage.decision_results import (  # noqa: E402
    DecisionResultRepository,
    decision_result_integrity_problems,
)

_OPS = ("acquisition", "storage", "redistribution", "hosted_transfer")

#: A canary in the target artifact's private BLOB content. It stays in the private
#: revision BLOB and never appears in the public manifest or static errors.
_SOURCE_CANARY = b"PRIVATE-SOURCE-CANARY-8842-target-bytes"

#: A distinct synthetic canary used as the Jev request *state*. The bounded
#: request state IS recorded verbatim in the private intent canonical; the public
#: manifest and static errors must not carry it. A real caller secret placed here
#: would likewise remain private and must never be published.
_STATE_CANARY = "SUBMITTED-STATE-CANARY-7731-candidate-abstract"

#: A canary placed in a choice question's declared options. Options are copied
#: into each per-question observation canonical, so this proves per-question
#: source-derived values are recorded privately.
_OPTIONS_CANARY = "CANARY-OPTION-9153-source-choice"

#: A canary placed in a score answer's legend. The legend is copied into the
#: per-question observation canonical.
_LEGEND_CANARY = "CANARY-LEGEND-6621-source-level"

#: A canary placed in a question's instructions (the referenced question-def
#: revision BLOB). It stays in the private revision BLOB and never appears in the
#: public manifest or static errors.
_INSTRUCTIONS_CANARY = "CANARY-INSTRUCTIONS-4478-source-text"

PROMPT_VERSION = "screen/1"
API_KEY = "synthetic-not-a-real-key"

_DIG_A = "a" * 64
_DIG_B = "b" * 64
_DIG_C = "c" * 64

MODEL_SNAPSHOT = "typesafe/jev-1.14-20261014"

#: (question_id, kind, instructions, options, levels, criteria)
_SPECS = (
    ("is_original_report", "predicate", "Is it an original report?", (), (), None),
    (
        "study_design",
        "choice",
        "Which study design?",
        ("randomised", "observational", "other", _OPTIONS_CANARY),
        (),
        {
            "randomised": "Randomised trial",
            "observational": "Observational study",
            "other": "Other design",
            _OPTIONS_CANARY: "Source-derived option text",
        },
    ),
    (
        "confidence_level",
        "score",
        "How confident are you?",
        (),
        ("low", "moderate", "high"),
        ["low", "moderate", "high"],
    ),
    ("provider_err_q", "predicate", "Provider error probe?", (), (), None),
    ("missing_q", "predicate", "Missing answer probe?", (), (), None),
    ("refusal_q", "predicate", "Refusal probe?", (), (), None),
    (
        "free_text",
        "box",
        "Explain your reasoning. " + _INSTRUCTIONS_CANARY,
        (),
        (),
        None,
    ),
)


def _wire_answers():
    """Wire answers covering answered/missing/provider/refusal/malformed/box."""
    return {
        "is_original_report": {"type": "noul", "noul": 0.97},
        "study_design": {
            "type": "choice",
            "choice": "randomised",
            "confidence": 0.81,
            "probabilities": {
                "randomised": 0.9,
                "observational": 0.1,
                "other": 0.0,
            },
        },
        # A choice answer to a score question -> malformed (no fabricated score).
        "confidence_level": {"type": "choice", "choice": "high"},
        "provider_err_q": {"type": "error", "reason": "boom"},
        "refusal_q": {"type": "error", "reason": "refused"},
        # ``missing_q`` is omitted entirely.
    }


def _wire_body(answers, model=MODEL_SNAPSHOT, cost=0.00002037):
    body = {
        "id": "gen-dec-1",
        "model": model,
        "provider": "TypeSafe",
        "answers": answers,
        "usage": {"input_tokens": 485, "output_tokens": 76, "cost": cost},
    }
    return json.dumps(body)


class _CannedTransport:
    def __init__(self, status, body_text=""):
        self.response = jev.TransportResponse(status, body_text)
        self.calls = 0

    def __call__(self, body, api_key, *, endpoint, timeout):
        self.calls += 1
        return self.response


class _RaisingTransport:
    def __init__(self, exc):
        self.exc = exc
        self.calls = 0

    def __call__(self, body, api_key, *, endpoint, timeout):
        self.calls += 1
        raise self.exc


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


def _question_def_json(qid, kind, instructions, options, levels, criteria_ref):
    raw = {
        "question_id": qid,
        "question_kind": kind,
        "instructions": instructions,
        "criteria_ref": criteria_ref,
    }
    if kind == "choice":
        raw["options"] = list(options)
    if kind == "score":
        raw["levels"] = list(levels)
    return raw


class ResultBackupBase(unittest.TestCase):
    """A standalone minimal decision-result fixture (no network/model/paid call)."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="raa-result-backup-")
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
        self.results = DecisionResultRepository(self.store)
        self.config = _config()
        self._build_fixture()

    @property
    def pid(self) -> str:
        return self.store.project_id

    # -- fixture ------------------------------------------------------------- #
    def _import(self, content: bytes, *, revision_id: str):
        artifact = self.artifacts.create_artifact(kind="document")
        revision = self.artifacts.import_revision(
            artifact_id=artifact.artifact_id,
            content=content,
            origin_locator="import:synthetic",
            rights=_rights(self.pid, artifact.artifact_id, revision_id),
            revision_id=revision_id,
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
            crit_artifact, _ = self._import(crit_bytes, revision_id=f"crit_rev_{index}")
            crit_ref = {
                "project_id": self.pid,
                "artifact_id": crit_artifact.artifact_id,
                "revision_id": f"crit_rev_{index}",
            }
            qdef_json = _question_def_json(
                qid, kind, instructions, options, levels, crit_ref
            )
            qdef_bytes = canonical_json(qdef_json)
            qdef_artifact, _ = self._import(qdef_bytes, revision_id=f"qd_rev_{index}")
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

        self.input_refs = input_refs
        self.task = self._record_task("task-req")

        self.plan = jev.plan_dispatch(
            self.questions, _STATE_CANARY, reserve_usd="0.25"
        )

        self.input_digest = _DIG_A
        self.versions = {
            "context_kind": "model_derived",
            "input_digest": self.input_digest,
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

        self.dispatch_record = self._reserve("call-1")
        self.intent = self._record_intent(self.dispatch_record)

    def _record_task(self, request_id: str):
        bounded = BoundedTask.from_mapping(
            {
                "project_id": self.pid,
                "role": "writer",
                "target": dict(self.target_ref),
                "input_refs": list(self.input_refs),
                "scope": ["section:screening"],
                "prompt_version": PROMPT_VERSION,
                "size_limit": 4096,
                "step_limit": 4,
            },
            configuration=self.config,
        )
        return self.runtime.record_task(
            bounded, request_id=request_id, configuration=self.config
        )

    def _reserve(self, request_id: str, *, task=None):
        return self.dispatches.reserve_dispatch(
            task or self.task,
            request_id=request_id,
            route="direct_call",
            stage="screening",
            input_digest=self.input_digest,
            version_digest=self.version_digest,
            reserve_usd="0.25",
            bound_evidence_digest=_DIG_B,
        )

    def _record_intent(self, dispatch_record, *, task=None):
        return self.intents.record_intent(
            task or self.task,
            dispatch_record,
            self.plan,
            question_def_ref=self.qdef_refs,
            criteria_ref=self.crit_refs,
            versions=self.versions,
            consumed=("rubric",),
        )

    def add_failure_dispatch(self):
        """Reserve + record a second direct dispatch on its own task.

        The initial scope permits at most one logical dispatch per task, so a
        whole-call failure observation needs a distinct task (never a retry).
        """
        task = self._record_task("task-req-failure")
        record = self._reserve("call-2", task=task)
        intent = self._record_intent(record, task=task)
        return record, intent

    # -- recording ----------------------------------------------------------- #
    def submit(self, dispatch_record=None):
        record = dispatch_record or self.dispatch_record
        return self.dispatches.mark_submitted(record.dispatch_id)

    def record(self, result, *, dispatch_record=None, fence=1):
        record = dispatch_record or self.dispatch_record
        return self.results.record_result(record.dispatch_id, result, fence=fence)

    def dispatch_result(self, transport):
        return jev.dispatch_plan(self.plan, api_key=API_KEY, transport=transport)

    def valid_result(self, **overrides):
        answers = dict(_wire_answers())
        answers.update(overrides.pop("answers", {}))
        transport = _CannedTransport(200, _wire_body(answers, **overrides))
        return self.dispatch_result(transport)

    def failure_result(self):
        return self.dispatch_result(
            _RaisingTransport(TimeoutError("simulated transport failure"))
        )

    # -- backup/restore helpers ---------------------------------------------- #
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

    def _assert_rewritten_hash_rejected(self, dest: Path) -> None:
        self.rewrite_outer_db_hash(dest)
        with self.assertRaises(BackupError):
            verify_backup(dest)

    _RESULT_TRIGGERS = (
        "decision_result_set_no_update",
        "decision_result_set_no_delete",
        "decision_result_question_no_update",
        "decision_result_question_no_delete",
    )

    def _mutate(self, dest: Path, fn, *, triggers=None) -> None:
        """Execute ``fn(conn)`` against the snapshot, recreating dropped triggers.

        The triggers are read from ``sqlite_master`` before being dropped and are
        recreated with their **exact** stored SQL, so the canonical schema-object
        comparison still passes and the tamper is only detectable by an inner
        validator.
        """
        names = self._RESULT_TRIGGERS if triggers is None else triggers
        conn = sqlite3.connect(str(dest / SNAPSHOT_FILENAME))
        conn.row_factory = sqlite3.Row
        try:
            saved: dict[str, str] = {}
            for name in names:
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

    @staticmethod
    def _first_set(conn: sqlite3.Connection):
        # ``rowid`` is not part of ``SELECT *`` for a composite primary key table.
        return conn.execute(
            "SELECT rowid, * FROM decision_result_set ORDER BY rowid LIMIT 1"
        ).fetchone()

    @staticmethod
    def _set_mapping(row) -> dict:
        return json.loads(bytes(row["canonical"]).decode("utf-8"))

    @staticmethod
    def _update_set(conn: sqlite3.Connection, row, mapping: dict) -> None:
        canonical = canonical_json(mapping)
        conn.execute(
            "UPDATE decision_result_set SET canonical = ?, content_digest = ?, "
            "status = ?, question_count = ?, cost_state = ?, cost_amount_usd = ?, "
            "task_id = ?, intent_id = ? WHERE rowid = ?",
            (
                canonical,
                hashlib.sha256(canonical).hexdigest(),
                mapping["status"],
                mapping["question_count"],
                mapping["cost"]["state"],
                mapping["cost"].get("amount_usd"),
                mapping["task_id"],
                mapping["intent_id"],
                row["rowid"],
            ),
        )

    def _first_question(self, conn: sqlite3.Connection, question_id: str):
        return conn.execute(
            "SELECT rowid, * FROM decision_result_question WHERE question_id = ? "
            "ORDER BY rowid LIMIT 1",
            (question_id,),
        ).fetchone()

    @staticmethod
    def _rewrite_question(conn, row, record: dict) -> str:
        canonical = canonical_json(record)
        digest = hashlib.sha256(canonical).hexdigest()
        conn.execute(
            "UPDATE decision_result_question SET canonical = ?, content_digest = ? "
            "WHERE rowid = ?",
            (canonical, digest, row["rowid"]),
        )
        return digest

    # -- specific tampers ---------------------------------------------------- #
    def _corrupt_set_canonical(self, dest: Path) -> None:
        def fn(conn):
            conn.execute("UPDATE decision_result_set SET canonical = ?", (b"{}",))

        self._mutate(dest, fn, triggers=("decision_result_set_no_update",))

    def _disagree_set_column(self, dest: Path) -> None:
        def fn(conn):
            # The canonical still records ``completed``; the projected column lies.
            conn.execute("UPDATE decision_result_set SET status = 'failed'")

        self._mutate(dest, fn, triggers=("decision_result_set_no_update",))

    def _self_consistent_subset(self, dest: Path) -> None:
        """Drop one question row and trim the set's own array to match.

        The set canonical, its digest, the projected ``question_count`` column, and
        the surviving question rows all agree, so only the *independent* intent
        comparison can detect the partial set.
        """

        def fn(conn):
            row = self._first_set(conn)
            mapping = self._set_mapping(row)
            removed = mapping["questions"].pop()
            mapping["question_ids"].remove(removed["question_id"])
            mapping["question_count"] = len(mapping["questions"])
            self._update_set(conn, row, mapping)
            conn.execute(
                "DELETE FROM decision_result_question WHERE question_id = ?",
                (removed["question_id"],),
            )

        self._mutate(dest, fn)

    def _change_record_cost(self, dest: Path, question_id: str = "study_design") -> None:
        """Change one record's cost amount self-consistently.

        The record canonical/digest and the set's per-record digest agree; only the
        independent set-cost binding catches the change.
        """

        def fn(conn):
            row = self._first_question(conn, question_id)
            record = json.loads(bytes(row["canonical"]).decode("utf-8"))
            record["cost"]["amount_usd"] = "9.99000000"
            digest = self._rewrite_question(conn, row, record)
            set_row = self._first_set(conn)
            mapping = self._set_mapping(set_row)
            for entry in mapping["questions"]:
                if entry["question_id"] == question_id:
                    entry["record_digest"] = digest
            self._update_set(conn, set_row, mapping)

        self._mutate(dest, fn)

    def _change_record_ref(self, dest: Path, question_id: str = "study_design") -> None:
        """Point one record's question ref at a different existing revision."""

        def fn(conn):
            row = self._first_question(conn, question_id)
            record = json.loads(bytes(row["canonical"]).decode("utf-8"))
            record["question_def_ref"] = dict(record["criteria_ref"])
            digest = self._rewrite_question(conn, row, record)
            set_row = self._first_set(conn)
            mapping = self._set_mapping(set_row)
            for entry in mapping["questions"]:
                if entry["question_id"] == question_id:
                    entry["record_digest"] = digest
            self._update_set(conn, set_row, mapping)

        self._mutate(dest, fn)

    def _change_parent_link(self, dest: Path) -> None:
        """Change the set's recorded intent id (no FK violation; a parent mismatch)."""

        def fn(conn):
            row = self._first_set(conn)
            mapping = self._set_mapping(row)
            mapping["intent_id"] = "dint_missing"
            self._update_set(conn, row, mapping)

        self._mutate(dest, fn, triggers=("decision_result_set_no_update",))

    def _insert_orphan_question(self, dest: Path) -> None:
        def fn(conn):
            conn.execute("PRAGMA foreign_keys = OFF")
            conn.execute(
                "INSERT INTO decision_result_question "
                "(project_id, dispatch_id, result_id, record_id, question_id, "
                " question_kind, outcome, ordinal, request_sha256, canonical, "
                " content_digest, created_at_utc) "
                "VALUES (?, 'disp_orphan', 'dres_o', 'dec_o', 'q_o', 'predicate', "
                " 'answered', 0, ?, ?, ?, '2026-01-01T00:00:00+00:00')",
                (self.pid, _DIG_A, b"{}", _DIG_A),
            )

        self._mutate(dest, fn)

    def _insert_foreign_set(self, dest: Path) -> None:
        def fn(conn):
            conn.execute("PRAGMA foreign_keys = OFF")
            conn.execute(
                "INSERT INTO decision_result_set "
                "(project_id, dispatch_id, result_id, schema_version, task_id, "
                " intent_id, intent_digest, input_digest, version_digest, "
                " request_sha256, request_bytes, cost_upper_bound_usd, reserve_usd, "
                " status, attempt_no, prior_outcome, cost_state, cost_amount_usd, "
                " question_count, canonical, content_digest, created_at_utc) "
                "VALUES ('prj_foreign', 'disp_foreign', 'dres_x', 1, ?, 'dint_x', "
                " ?, ?, ?, ?, 1, '0', '0.25', 'completed', 1, 'none', 'unknown', NULL, "
                " 1, ?, ?, '2026-01-01T00:00:00+00:00')",
                (
                    self.task.task_id,
                    _DIG_A,
                    _DIG_A,
                    _DIG_A,
                    _DIG_A,
                    b"{}",
                    _DIG_A,
                ),
            )

        self._mutate(dest, fn)

    def _assert_hook_rejects(self, dest: Path, *, expect_static_message: bool = True) -> None:
        conn = self._snapshot_conn(dest)
        try:
            # The tamper dropped and exactly restored the result triggers, so the
            # canonical schema comparison still passes: the refusal below can only
            # come from the inner checker, not the schema-shape check. Compare
            # against the snapshot's *actual* recorded schema version; the expected
            # current version is asserted separately.
            actual_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
            self.assertEqual(actual_version, schema.STORE_SCHEMA_VERSION)
            backup_mod._assert_known_schema(conn, actual_version)
            with self.assertRaises(BackupError):
                backup_mod._verify_decision_result_relations(conn, self.pid)
        finally:
            conn.close()
        if expect_static_message:
            self.rewrite_outer_db_hash(dest)
            with self.assertRaises(BackupError) as caught:
                verify_backup(dest)
            self.assertIn("do not revalidate", str(caught.exception))
        else:
            # A foreign/orphan row also trips the generic FK check before the inner
            # hook; the direct call above already proved the inner checker catches
            # it. The rewritten archive is still refused before registration.
            self._assert_rewritten_hash_rejected(dest)

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


class RoundTripTest(ResultBackupBase):
    def _record_answered(self):
        self.submit(self.dispatch_record)
        return self.record(self.valid_result())

    def test_roundtrip_answered_and_failure_observations_preserve_exact_rows(self) -> None:
        answered = self._record_answered()
        failure_dispatch, failure_intent = self.add_failure_dispatch()
        self.submit(failure_dispatch)
        failure = self.record(self.failure_result(), dispatch_record=failure_dispatch)

        self.assertEqual(answered.status, "completed")
        self.assertEqual(failure.status, "transport_error")

        dest = self.backup()
        report = verify_backup(dest)
        self.assertTrue(report["ok"])
        self.assertEqual(report["store_schema_version"], schema.STORE_SCHEMA_VERSION)

        restored, info = self._restore(dest, "restored")
        self.assertEqual(info.project_id, self.pid)
        results = DecisionResultRepository(restored)

        got = results.get_result(answered.dispatch_id)
        self.assertEqual(got.canonical, answered.canonical)
        self.assertEqual(got.digest, answered.digest)
        # Exact refs/versions/cost/provider rows survive.
        self.assertEqual(got.intent_id, self.intent.intent_id)
        self.assertEqual(got.intent_digest, self.intent.content_digest)
        self.assertEqual(got.input_digest, self.input_digest)
        self.assertEqual(got.version_digest, self.version_digest)
        self.assertEqual(got.request_sha256, self.plan.request_sha256)
        self.assertEqual(got.request_bytes, self.plan.request_bytes)
        self.assertEqual(got.cost_upper_bound_usd, self.plan.cost_upper_bound_usd)
        self.assertEqual(got.reserve_usd, "0.25")
        self.assertEqual(got.actual_model, MODEL_SNAPSHOT)
        self.assertEqual(got.provider, "TypeSafe")
        self.assertEqual(got.response_id, "gen-dec-1")
        self.assertEqual(dict(got.usage)["input_tokens"], 485)
        self.assertEqual(got.cost["state"], "known")
        self.assertEqual(got.cost["amount_usd"], "0.00002037")
        self.assertEqual(got.question_count, len(_SPECS))
        self.assertEqual(tuple(got.question_ids), tuple(s[0] for s in _SPECS))
        self.assertEqual(len(got.questions), len(_SPECS))

        original_rows = self.results.list_question_records(answered.dispatch_id)
        restored_rows = results.list_question_records(answered.dispatch_id)
        self.assertEqual(len(restored_rows), len(_SPECS))
        for original, restored_row in zip(original_rows, restored_rows):
            self.assertEqual(restored_row.canonical, original.canonical)
            self.assertEqual(restored_row.content_digest, original.content_digest)
            self.assertEqual(dict(restored_row.record), dict(original.record))
            self.assertEqual(restored_row.record["cost"], original.record["cost"])
            self.assertEqual(restored_row.record["provider"], original.record["provider"])
            self.assertEqual(restored_row.record["versions"], original.record["versions"])

        # The whole-call failure observation survives too.
        got_failure = results.get_result(failure.dispatch_id)
        self.assertEqual(got_failure.canonical, failure.canonical)
        self.assertEqual(got_failure.status, "transport_error")
        self.assertEqual(got_failure.intent_id, failure_intent.intent_id)
        self.assertEqual(got_failure.cost["state"], "unknown")
        self.assertNotIn("amount_usd", got_failure.cost)

        self.assertEqual(
            decision_result_integrity_problems(restored.connection, self.pid), []
        )
        self.assertEqual(
            runtime_dispatch_integrity_problems(restored.connection, self.pid), []
        )

        # Restore neither accepts, executes, nor retries a recorded observation.
        for dispatch_id in (answered.dispatch_id, failure.dispatch_id):
            got_dispatch = RuntimeDispatchRepository(restored).get_dispatch(dispatch_id)
            self.assertIn(got_dispatch.state, ("submitted", "unknown"))
            self.assertNotEqual(got_dispatch.state, "completed")
            self.assertIsNone(got_dispatch.result_ref)
            self.assertIsNone(got_dispatch.result_digest)

    def test_advanced_target_head_history_verifies_and_preserves_observation(
        self,
    ) -> None:
        answered = self._record_answered()
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
        results = DecisionResultRepository(restored)
        got = results.get_result(answered.dispatch_id)
        # The observation still pins rev_1, not the moved head; history is preserved.
        self.assertEqual(got.canonical, answered.canonical)
        row = results.get_question_record(answered.dispatch_id, "study_design")
        self.assertEqual(row.record["target"]["revision_id"], "rev_1")
        self.assertEqual(
            decision_result_integrity_problems(restored.connection, self.pid), []
        )

    def test_unknown_cost_stays_unknown_and_is_not_accepted_or_retried(self) -> None:
        failure_dispatch, _ = self.add_failure_dispatch()
        self.submit(failure_dispatch)
        failure = self.record(self.failure_result(), dispatch_record=failure_dispatch)

        self.assertEqual(failure.cost["state"], "unknown")
        self.assertNotIn("amount_usd", failure.cost)
        self.assertEqual(failure.status, "transport_error")

        dest = self.backup()
        self.assertTrue(verify_backup(dest)["ok"])
        restored, _ = self._restore(dest, "restored-unknown")

        got = DecisionResultRepository(restored).get_result(failure.dispatch_id)
        self.assertEqual(got.cost["state"], "unknown")
        self.assertNotIn("amount_usd", got.cost)
        # An unknown cost is never a silent zero, and no accepted result is invented.
        self.assertNotEqual(got.cost.get("amount_usd"), "0")
        self.assertEqual(
            decision_result_integrity_problems(restored.connection, self.pid), []
        )
        got_dispatch = RuntimeDispatchRepository(restored).get_dispatch(
            failure.dispatch_id
        )
        self.assertIn(got_dispatch.state, ("submitted", "unknown"))
        self.assertIsNone(got_dispatch.result_ref)
        self.assertIsNone(got_dispatch.result_digest)

    def test_readonly_verify_does_not_change_snapshot(self) -> None:
        self._record_answered()
        dest = self.backup()
        snapshot = dest / SNAPSHOT_FILENAME
        manifest = dest / MANIFEST_FILENAME
        before = (snapshot.read_bytes(), manifest.read_bytes())
        for _ in range(3):
            self.assertTrue(verify_backup(dest)["ok"])
        after = (snapshot.read_bytes(), manifest.read_bytes())
        self.assertEqual(before, after)

    def test_private_canary_redaction_is_accurate_and_archive_is_owner_private(
        self,
    ) -> None:
        self.submit(self.dispatch_record)
        observation = self.record(
            self.valid_result(
                answers={
                    "confidence_level": {
                        "type": "score",
                        "score": 1.0,
                        "legend": {
                            "0": "low",
                            "1": _LEGEND_CANARY,
                            "2": "high",
                        },
                    }
                }
            )
        )

        # The PRIVATE per-question canonical records source-derived values verbatim:
        # the declared options and the score legend. It is NOT a secret scanner and
        # makes NO "never contains source" promise.
        options_row = self.results.get_question_record(
            observation.dispatch_id, "study_design"
        )
        self.assertIn(_OPTIONS_CANARY.encode(), options_row.canonical)
        self.assertIn(_OPTIONS_CANARY, options_row.record["options"])
        score_row = self.results.get_question_record(
            observation.dispatch_id, "confidence_level"
        )
        self.assertIn(_LEGEND_CANARY.encode(), score_row.canonical)
        self.assertIn(_LEGEND_CANARY, score_row.record["answer"]["legend"].values())
        # The provider envelope is recorded on the set row.
        self.assertEqual(observation.provider, "TypeSafe")
        self.assertEqual(dict(observation.usage)["input_tokens"], 485)

        # The referenced question instructions and the target source bytes are in
        # private revision BLOBs in the same snapshot, not inlined into the set.
        self.assertIn(
            _INSTRUCTIONS_CANARY.encode(),
            self.artifacts.read_content(
                self.qdef_refs["free_text"]["artifact_id"],
                self.qdef_refs["free_text"]["revision_id"],
            ),
        )
        self.assertEqual(
            self.artifacts.read_content(
                self.target_ref["artifact_id"], self.target_ref["revision_id"]
            ),
            _SOURCE_CANARY,
        )
        self.assertNotIn(_SOURCE_CANARY, observation.canonical)
        self.assertNotIn(_INSTRUCTIONS_CANARY.encode(), observation.canonical)

        # The caller-submitted request state may itself contain a secret the caller
        # placed there; it is recorded in the private intent canonical only.
        intent = self.intents.get_intent(observation.dispatch_id)
        self.assertIn(_STATE_CANARY.encode(), intent.canonical)

        dest = self.backup()
        self.assertTrue(verify_backup(dest)["ok"])

        # Owner-private archive.
        self.assertEqual(stat.S_IMODE(os.stat(dest).st_mode), 0o700)
        for name in (MANIFEST_FILENAME, SNAPSHOT_FILENAME):
            self.assertEqual(
                stat.S_IMODE(os.stat(dest / name).st_mode), 0o600, name
            )

        # The result tables have no credential/capability/authority projection
        # column; no such value is automatically serialized.
        conn = self._snapshot_conn(dest)
        try:
            columns: set[str] = set()
            for table in (
                "decision_result_set",
                "decision_result_question",
                "decision_request_intent",
            ):
                columns |= {
                    row["name"]
                    for row in conn.execute(
                        f"PRAGMA table_info({table})"
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

        # The PUBLIC manifest carries no source/state text and remains a private
        # local snapshot (never an export).
        manifest = (dest / MANIFEST_FILENAME).read_bytes()
        for canary in (
            _SOURCE_CANARY,
            _STATE_CANARY.encode(),
            _OPTIONS_CANARY.encode(),
            _LEGEND_CANARY.encode(),
            _INSTRUCTIONS_CANARY.encode(),
        ):
            self.assertNotIn(canary, manifest)
        self.assertIn(b"Private local storage snapshot", manifest)

        # A static verification error is value-free. Corrupt the set canonical and
        # rehash the outer database so verification reaches the inner checker; the
        # raised message must not echo any canary.
        self._corrupt_set_canonical(dest)
        self.rewrite_outer_db_hash(dest)
        with self.assertRaises(BackupError) as caught:
            verify_backup(dest)
        message = str(caught.exception)
        for text in (
            _SOURCE_CANARY.decode(),
            _STATE_CANARY,
            _OPTIONS_CANARY,
            _LEGEND_CANARY,
            _INSTRUCTIONS_CANARY,
        ):
            self.assertNotIn(text, message)
        self.assertIn("do not revalidate", message)


class InnerValidationTest(ResultBackupBase):
    def _backup_with_observation(self):
        self.submit(self.dispatch_record)
        observation = self.record(self.valid_result())
        dest = self.backup()
        return observation, dest

    def test_corrupt_set_canonical_rejected_by_inner_checker(self) -> None:
        _, dest = self._backup_with_observation()
        self._corrupt_set_canonical(dest)
        self._assert_hook_rejects(dest)

    def test_column_disagreement_rejected_by_inner_checker(self) -> None:
        _, dest = self._backup_with_observation()
        self._disagree_set_column(dest)
        self._assert_hook_rejects(dest)

    def test_self_consistent_subset_rejected_by_inner_checker(self) -> None:
        _, dest = self._backup_with_observation()
        self._self_consistent_subset(dest)
        self._assert_hook_rejects(dest)

    def test_per_record_cost_change_rejected_by_inner_checker(self) -> None:
        _, dest = self._backup_with_observation()
        self._change_record_cost(dest)
        self._assert_hook_rejects(dest)

    def test_per_record_ref_change_rejected_by_inner_checker(self) -> None:
        _, dest = self._backup_with_observation()
        self._change_record_ref(dest)
        self._assert_hook_rejects(dest)

    def test_parent_link_change_rejected_by_inner_checker(self) -> None:
        _, dest = self._backup_with_observation()
        self._change_parent_link(dest)
        self._assert_hook_rejects(dest)

    def test_orphan_question_row_rejected_by_inner_checker(self) -> None:
        _, dest = self._backup_with_observation()
        self._insert_orphan_question(dest)
        self._assert_hook_rejects(dest, expect_static_message=False)

    def test_foreign_project_set_row_rejected_by_inner_checker(self) -> None:
        _, dest = self._backup_with_observation()
        self._insert_foreign_set(dest)
        self._assert_hook_rejects(dest, expect_static_message=False)

    def test_corrupt_observation_archive_rejected_before_registration(self) -> None:
        _, dest = self._backup_with_observation()
        # A self-consistent partial set survives the schema comparison, the generic
        # FK check, and the outer hash rewrite; only the inner hook refuses it.
        self._self_consistent_subset(dest)
        self.rewrite_outer_db_hash(dest)

        registry = self.new_registry()
        target = self.tmp / "restored-corrupt"
        target.mkdir()
        (target / "unrelated.txt").write_text("keep me")
        with self.assertRaises(BackupError):
            restore_backup(dest, target, registry)
        self.assertEqual(registry.list_projects(), [])
        self.assertFalse(os.path.lexists(paths.app_dir_for(str(target))))
        self.assertEqual((target / "unrelated.txt").read_text(), "keep me")


class SchemaCompatibilityTest(ResultBackupBase):
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

    def test_schema8_snapshot_without_result_tables_verifies(self) -> None:
        store = self.new_schema_store(8, "p8")
        tables = {
            row["name"]
            for row in store.connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        # The schema-9 result tables do not exist below schema 9. If the schema-9
        # hook ran at 8 it would fail on the missing tables, so a successful verify
        # proves the hook is threshold-gated.
        self.assertNotIn("decision_result_set", tables)
        self.assertNotIn("decision_result_question", tables)

        dest = self.tmp / "backup8"
        create_backup(store, dest)
        report = verify_backup(dest)
        self.assertTrue(report["ok"])
        self.assertEqual(report["store_schema_version"], 8)

        registry = Registry(self.tmp / "state")
        target = self.tmp / "restored8"
        target.mkdir()
        info = restore_backup(dest, target, registry)
        restored = ProjectStore.open_at(
            paths.app_dir_for(str(target)), expected_project_id=info.project_id
        )
        self.addCleanup(restored.close)
        self.assertEqual(restored.schema_version, schema.STORE_SCHEMA_VERSION)


class DynamicHookTest(ResultBackupBase):
    def test_hook_table_is_ordered_and_threshold_keyed(self) -> None:
        minimums = [minimum for minimum, _ in backup_mod._SCHEMA_VALIDATORS]
        self.assertEqual(minimums, sorted(minimums))
        self.assertEqual(len(minimums), len(set(minimums)))
        # The schema-9 decision-result hook is the newest entry, but the table is a
        # plain ordered threshold table, not a hardcoded "must be exactly current"
        # check. The earlier schema-8 intent hook stays pinned at its own minimum.
        self.assertEqual(minimums[-1], schema.STORE_SCHEMA_VERSION)
        self.assertIn(backup_mod._DECISION_RESULT_SCHEMA_VERSION, minimums)
        self.assertIn(8, minimums)
        self.assertIn(backup_mod._INTENT_SCHEMA_VERSION, minimums)
        self.assertEqual(backup_mod._INTENT_SCHEMA_VERSION, 8)
        self.assertEqual(
            backup_mod.SUPPORTED_STORE_SCHEMA_VERSION, schema.STORE_SCHEMA_VERSION
        )
        self.assertEqual(
            backup_mod.SUPPORTED_SNAPSHOT_SCHEMA_VERSIONS,
            frozenset(range(3, schema.STORE_SCHEMA_VERSION + 1)),
        )
        validators = [validator for _, validator in backup_mod._SCHEMA_VALIDATORS]
        self.assertTrue(all(callable(validator) for validator in validators))
        self.assertEqual(len(validators), len(set(validators)))

    def test_a_later_hook_can_append_without_editing_schema9_code(self) -> None:
        called: list[str] = []

        def future_validator(conn, project_id):
            called.append(project_id)

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

    def test_schema9_hook_runs_at_9_and_not_at_8(self) -> None:
        # A stand-in proves the dispatch table's semantics without needing real
        # tables: a hook whose minimum is 9 runs for 9 and is skipped for 8.
        called: list[int] = []

        def result_stand_in(conn, project_id):
            called.append(1)

        extended = ((backup_mod._DECISION_RESULT_SCHEMA_VERSION, result_stand_in),)
        with mock.patch.object(backup_mod, "_SCHEMA_VALIDATORS", extended):
            conn = self.store.connection
            backup_mod._verify_schema_specific(conn, self.pid, 8)
            self.assertEqual(called, [])
            backup_mod._verify_schema_specific(
                conn, self.pid, backup_mod._DECISION_RESULT_SCHEMA_VERSION
            )
            self.assertEqual(called, [1])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
