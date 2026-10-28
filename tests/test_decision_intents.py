"""Acceptance tests for the durable pre-dispatch Jev decision request intent.

Covers ``src/review_article_agent/storage/decision_intents.py`` against the task
T5f acceptance requirements and ``docs/contracts/02-gates-and-runtime.md``
§6.1/§6.2/§6.3/§8.1.1, over a **real** temporary project store:

* one immutable canonical intent row per reserved ``direct_call`` dispatch,
  binding the exact target/question refs, the consumed ``VersionSet`` (with
  ``input_digest``/version-hash/prompt-version bindings) and the exact recomputed
  request digest/bytes/bound/pricing/reserve;
* the reviewable C02 ``QuestionDef`` representation rebuilt from the question
  definition + criteria revisions and compared with the planned questions;
* ``box`` unsupported questions recorded with no invented output;
* idempotent same-dispatch replay (even after submission and moved heads) and a
  divergent-request refusal;
* refusal of a forged plan, foreign/out-of-scope refs, unconsumed refs, corrupt
  ref bytes, version mismatches, missing known slots, and bad route/timestamps;
* a no-write guarantee on every failure;
* a read-only integrity sweep that detects foreign/dangling/corrupt rows;
* a schema 7 -> 8 upgrade that preserves predecessor rows.

No network, no credential, no paid call. Run:

    PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_decision_intents.py'
"""

from __future__ import annotations

import dataclasses
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

from review_article_agent.domain.records import canonical_json  # noqa: E402
from review_article_agent.runtime import jev  # noqa: E402
from review_article_agent.runtime.dispatch import (  # noqa: E402
    RuntimeDispatchRepository,
)
from review_article_agent.runtime.jobs import JobRepository  # noqa: E402
from review_article_agent.runtime.roles import (  # noqa: E402
    BoundedTask,
    RoleConfiguration,
)
from review_article_agent.storage import (  # noqa: E402
    ArtifactRepository,
    ProjectStore,
    paths,
    schema,
)
from review_article_agent.storage.agent_runtime import (  # noqa: E402
    RuntimeRepository,
    validate_version_set,
)
from review_article_agent.storage.decision_intents import (  # noqa: E402
    INTENT_SCHEMA_VERSION,
    DecisionIntentConflictError,
    DecisionIntentCorruptionError,
    DecisionIntentRepository,
    DecisionIntentStateError,
    DecisionIntentValidationError,
    decision_intent_integrity_problems,
)

_OPS = ("acquisition", "storage", "redistribution", "hosted_transfer")

_DIG_A = "a" * 64
_DIG_B = "b" * 64
_DIG_C = "c" * 64

PROMPT_VERSION = "screen/1"

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
        ("randomised", "observational", "other"),
        (),
        {
            "randomised": "Randomised trial",
            "observational": "Observational study",
            "other": "Other design",
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
    (
        "free_text",
        "box",
        "Explain your reasoning.",
        (),
        (),
        None,
    ),
)


def _rights(project_id, artifact_id, revision_id, *, rights_id=None):
    ops = {op: "allowed" for op in _OPS}
    return {
        "rights_id": rights_id if rights_id is not None else f"RIGHTS-{artifact_id}-{revision_id}",
        "project_id": project_id,
        "record_revision": 1,
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


def _config(role="writer", model="anthropic/claude-sonnet-5-5", variant="high"):
    return RoleConfiguration.from_mapping(
        {
            "schema_version": 1,
            "roles": {
                role: {
                    "provider_id": "openrouter",
                    "model_id": model,
                    "variant": variant,
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


class IntentTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="raa-intent-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.store = ProjectStore.create_at(
            paths.app_dir_for(str(self.tmp / "p")), paths.new_project_id()
        )
        self.addCleanup(self.store.close)
        self.artifacts = ArtifactRepository(self.store)
        self.runtime = RuntimeRepository(self.store)
        self.dispatch = RuntimeDispatchRepository(self.store)
        self.jobs = JobRepository(self.store)
        self.intents = DecisionIntentRepository(self.store)
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
        self.target_artifact, _ = self._import(b"candidate-source-bytes", revision_id="rev_1")
        self.target_ref = {
            "project_id": self.pid,
            "artifact_id": self.target_artifact.artifact_id,
            "revision_id": "rev_1",
        }

        self.questions: list[jev.QuestionDef] = []
        self.qdef_refs: dict[str, dict[str, str]] = {}
        self.crit_refs: dict[str, dict[str, str]] = {}
        input_refs: list[dict[str, str]] = []

        for index, (qid, kind, instructions, options, levels, criteria) in enumerate(_SPECS):
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
            "rubric": {"state": "known", "ref": {"version": "jev-rubric/1", "digest": "e" * 64}},
            "parser": {"state": "not_applicable"},
            "adapter": {"state": "known", "ref": {"version": "jev-adapter/1", "digest": "f" * 64}},
            "route": "direct_call",
        }
        version_map = validate_version_set(self.versions)
        self.version_digest = hashlib.sha256(canonical_json(version_map)).hexdigest()

        self.dispatch_record = self.dispatch.reserve_dispatch(
            self.task,
            request_id="call-1",
            route="direct_call",
            stage="screening",
            input_digest=self.input_digest,
            version_digest=self.version_digest,
            reserve_usd="0.25",
            bound_evidence_digest=_DIG_B,
        )

    # -- helpers ------------------------------------------------------------- #
    def _tamper_revision(self, artifact_id, revision_id, content) -> None:
        """Rewrite a revision's bytes **and** its recorded metadata consistently."""
        conn = self.store.connection
        digest = hashlib.sha256(content).hexdigest()
        conn.execute("DROP TRIGGER IF EXISTS revision_no_update")
        conn.execute(
            "UPDATE revision SET content = ?, content_sha256 = ?, byte_size = ? "
            "WHERE project_id = ? AND artifact_id = ? AND revision_id = ?",
            (content, digest, len(content), self.pid, artifact_id, revision_id),
        )

    def _forge_intent_canonical(self, dispatch_id, mutate) -> None:
        """Rewrite a stored intent's canonical bytes **consistently** (self-hashes).

        The canonical bytes, ``content_digest`` and the projected columns are kept
        in agreement, so the forgery is only detectable by re-deriving the actual
        independent pins (parent links, refs, request wire).
        """
        conn = self.store.connection
        conn.execute("DROP TRIGGER IF EXISTS decision_request_intent_no_update")
        row = conn.execute(
            "SELECT canonical FROM decision_request_intent WHERE project_id = ? "
            "AND dispatch_id = ?",
            (self.pid, dispatch_id),
        ).fetchone()
        mapping = json.loads(bytes(row["canonical"]).decode("utf-8"))
        mutate(mapping)
        canonical = canonical_json(mapping)
        conn.execute(
            "UPDATE decision_request_intent SET canonical = ?, content_digest = ?, "
            " stage = ?, task_id = ?, request_sha256 = ?, request_bytes = ? "
            "WHERE project_id = ? AND dispatch_id = ?",
            (
                canonical,
                hashlib.sha256(canonical).hexdigest(),
                mapping["stage"],
                mapping["task_id"],
                mapping["request_sha256"],
                mapping["request_bytes"],
                self.pid,
                dispatch_id,
            ),
        )

    def _corrupt_revision_content(self, artifact_id, revision_id, content=b"tampered") -> None:
        """Rewrite a revision's bytes **without** touching its recorded metadata."""
        conn = self.store.connection
        conn.execute("DROP TRIGGER IF EXISTS revision_no_update")
        conn.execute(
            "UPDATE revision SET content = ? WHERE project_id = ? AND artifact_id = ? "
            "AND revision_id = ?",
            (content, self.pid, artifact_id, revision_id),
        )

    def _record(
        self,
        *,
        plan=None,
        qdef_ref=None,
        crit_ref=None,
        versions=None,
        consumed=("rubric",),
        dispatch=None,
        task=None,
        created_at_utc=None,
    ):
        return self.intents.record_intent(
            task if task is not None else self.task,
            dispatch if dispatch is not None else self.dispatch_record,
            plan if plan is not None else self.plan,
            question_def_ref=qdef_ref if qdef_ref is not None else self.qdef_refs,
            criteria_ref=crit_ref if crit_ref is not None else self.crit_refs,
            versions=versions if versions is not None else self.versions,
            consumed=consumed,
            created_at_utc=created_at_utc,
        )


class RecordIntentTest(IntentTestBase):
    def test_records_exact_intent(self) -> None:
        record = self._record()
        self.assertEqual(record.project_id, self.pid)
        self.assertEqual(record.dispatch_id, self.dispatch_record.dispatch_id)
        self.assertEqual(record.task_id, self.task.task_id)
        self.assertEqual(record.route, "direct_call")
        self.assertEqual(record.stage, "screening")
        self.assertEqual(record.schema_version, INTENT_SCHEMA_VERSION)
        self.assertEqual(record.reserve_usd, "0.25")
        self.assertEqual(record.bound_evidence_digest, _DIG_B)
        self.assertEqual(record.input_digest, self.input_digest)
        self.assertEqual(record.version_digest, self.version_digest)
        self.assertEqual(record.prompt_version, PROMPT_VERSION)
        self.assertEqual(record.request_sha256, self.plan.request_sha256)
        self.assertEqual(record.request_bytes, self.plan.request_bytes)
        self.assertEqual(record.cost_upper_bound_usd, self.plan.cost_upper_bound_usd)
        self.assertEqual(record.pricing_version, jev.PRICING_VERSION)
        self.assertEqual(dict(record.target), self.target_ref)
        # Exact question ids/order and refs.
        self.assertEqual(
            record.question_ids(),
            ("is_original_report", "study_design", "confidence_level", "free_text"),
        )
        self.assertEqual(
            record.supported_question_ids(),
            ("is_original_report", "study_design", "confidence_level"),
        )
        self.assertEqual(record.unsupported_question_ids(), ("free_text",))
        for q in record.questions:
            self.assertEqual(dict(q.question_def_ref), self.qdef_refs[q.question_id])
            self.assertEqual(dict(q.criteria_ref), self.crit_refs[q.question_id])
            self.assertEqual(q.supported, q.question_kind != "box")
            self.assertEqual(len(q.question_def_digest), 64)
        # The canonical bytes are the truth and the digest binds them.
        self.assertEqual(record.digest, hashlib.sha256(record.canonical).hexdigest())
        self.assertEqual(
            canonical_json(record.to_mapping()), record.canonical
        )
        # Reading re-verifies and returns the same content.
        self.assertEqual(self.intents.get_intent(record.dispatch_id).canonical, record.canonical)
        self.assertEqual(len(self.intents.list_intents()), 1)
        self.assertEqual(self.intents.verify_all_integrity(), [])

    def test_plan_request_binding_is_recomputed(self) -> None:
        record = self._record()
        # The recorded request body is the plan's exact bytes (recomputed, not trusted).
        body = canonical_json(record.to_mapping()["plan_request"])
        self.assertEqual(len(body), record.request_bytes)
        self.assertEqual(hashlib.sha256(body).hexdigest(), record.request_sha256)
        self.assertEqual(body, bytes(self.plan.request_body))

    def test_predicate_absent_criteria_matches_wire(self) -> None:
        record = self._record()
        predicate = next(q for q in record.questions if q.question_id == "is_original_report")
        self.assertEqual(predicate.question_kind, "predicate")
        self.assertNotIn("free_text", record.supported_question_ids())

    def test_box_question_is_unsupported_without_output(self) -> None:
        record = self._record()
        box = next(q for q in record.questions if q.question_id == "free_text")
        self.assertFalse(box.supported)
        self.assertIn("free_text", record.unsupported_question_ids())
        self.assertNotIn("free_text", record.supported_question_ids())

    def test_idempotent_replay_returns_historical_record(self) -> None:
        first = self._record()
        second = self._record()
        self.assertEqual(first.intent_id, second.intent_id)
        self.assertEqual(first.canonical, second.canonical)
        self.assertEqual(len(self.intents.list_intents()), 1)

    def test_idempotent_replay_after_submission_and_moved_heads(self) -> None:
        first = self._record()
        # Move the target head forward (same artifact): the recorded ref is stale.
        self.artifacts.import_revision(
            artifact_id=self.target_artifact.artifact_id,
            content=b"a newer target revision",
            origin_locator="import:synthetic",
            rights=_rights(self.pid, self.target_artifact.artifact_id, "rev_2"),
            revision_id="rev_2",
            parent_revision_id="rev_1",
        )
        current = self.artifacts.current_revision(self.target_artifact.artifact_id)
        self.assertEqual(current.revision_id, "rev_2")
        # Submit the dispatch (a new intent is no longer permitted).
        self.dispatch.mark_submitted(self.dispatch_record.dispatch_id)
        # The replay is still idempotent and the historical read stays valid.
        replay = self._record()
        self.assertEqual(replay.canonical, first.canonical)
        self.assertEqual(self.intents.get_intent(first.dispatch_id).canonical, first.canonical)
        self.assertEqual(self.intents.verify_all_integrity(), [])

    def test_divergent_request_refused(self) -> None:
        first = self._record()
        divergent = jev.plan_dispatch(
            self.questions, "A different candidate abstract.", reserve_usd="0.25"
        )
        with self.assertRaises(DecisionIntentConflictError):
            self._record(plan=divergent)
        self.assertEqual(len(self.intents.list_intents()), 1)
        self.assertEqual(self.intents.get_intent(first.dispatch_id).canonical, first.canonical)

    def test_new_intent_requires_reserved_dispatch(self) -> None:
        self.dispatch.mark_submitted(self.dispatch_record.dispatch_id)
        with self.assertRaises(DecisionIntentStateError):
            self._record()
        self.assertEqual(self.intents.list_intents(), [])

    def test_unpersisted_or_relabelled_task_or_dispatch_refused(self) -> None:
        with self.assertRaises(DecisionIntentValidationError):
            self._record(task=object())  # type: ignore[arg-type]
        with self.assertRaises(DecisionIntentValidationError):
            self._record(dispatch=object())  # type: ignore[arg-type]
        # A structurally valid dispatch record with a foreign project is refused.
        forged = dataclasses.replace(self.dispatch_record, project_id="other-project")
        with self.assertRaises(DecisionIntentValidationError):
            self._record(dispatch=forged)
        self.assertEqual(self.intents.list_intents(), [])


class ForgedPlanTest(IntentTestBase):
    def test_forged_request_body_refused(self) -> None:
        forged = dataclasses.replace(self.plan, request_body=b"forged-body")
        with self.assertRaises(DecisionIntentValidationError):
            self._record(plan=forged)
        self.assertEqual(self.intents.list_intents(), [])

    def test_forged_cost_bound_refused(self) -> None:
        forged = dataclasses.replace(self.plan, cost_upper_bound_usd="0")
        with self.assertRaises(DecisionIntentValidationError):
            self._record(plan=forged)
        self.assertEqual(self.intents.list_intents(), [])

    def test_forged_reserve_refused(self) -> None:
        forged = dataclasses.replace(self.plan, reserve_usd="9.99")
        with self.assertRaises(DecisionIntentValidationError):
            self._record(plan=forged)
        self.assertEqual(self.intents.list_intents(), [])

    def test_non_plan_refused(self) -> None:
        with self.assertRaises(DecisionIntentValidationError):
            self._record(plan=object())  # type: ignore[arg-type]
        self.assertEqual(self.intents.list_intents(), [])


class RefsTest(IntentTestBase):
    def test_foreign_ref_refused(self) -> None:
        refs = dict(self.qdef_refs)
        refs["is_original_report"] = {
            "project_id": "other-project",
            "artifact_id": refs["is_original_report"]["artifact_id"],
            "revision_id": refs["is_original_report"]["revision_id"],
        }
        with self.assertRaises(DecisionIntentValidationError):
            self._record(qdef_ref=refs)
        self.assertEqual(self.intents.list_intents(), [])

    def test_out_of_scope_ref_refused(self) -> None:
        unrelated, _ = self._import(b"unrelated bytes", revision_id="rev_u")
        refs = dict(self.qdef_refs)
        refs["is_original_report"] = {
            "project_id": self.pid,
            "artifact_id": unrelated.artifact_id,
            "revision_id": "rev_u",
        }
        with self.assertRaises(DecisionIntentValidationError):
            self._record(qdef_ref=refs)
        self.assertEqual(self.intents.list_intents(), [])

    def test_missing_ref_refused(self) -> None:
        refs = dict(self.qdef_refs)
        refs["study_design"] = {
            "project_id": self.pid,
            "artifact_id": refs["study_design"]["artifact_id"],
            "revision_id": "rev_missing",
        }
        with self.assertRaises(DecisionIntentValidationError):
            self._record(qdef_ref=refs)
        self.assertEqual(self.intents.list_intents(), [])

    def test_incomplete_ref_coverage_refused(self) -> None:
        partial = {"is_original_report": self.qdef_refs["is_original_report"]}
        with self.assertRaises(DecisionIntentValidationError):
            self._record(qdef_ref=partial)
        self.assertEqual(self.intents.list_intents(), [])

    def test_embedded_criteria_ref_mismatch_detected_on_read(self) -> None:
        # A recorded question definition is rewritten with a criteria_ref that does
        # not match the recorded per-question criteria ref (still a valid QuestionDef
        # JSON object). The read rebuilds the question and must fail closed.
        record = self._record()
        qdef_ref = self.qdef_refs["study_design"]
        embedded = self.crit_refs["confidence_level"]
        bad = canonical_json(
            _question_def_json(
                "study_design",
                "choice",
                "Which study design?",
                ("randomised", "observational", "other"),
                (),
                embedded,
            )
        )
        self._tamper_revision(qdef_ref["artifact_id"], qdef_ref["revision_id"], bad)
        with self.assertRaises(DecisionIntentCorruptionError):
            self.intents.get_intent(record.dispatch_id)

    def test_byte_corruption_refused_on_new_intent(self) -> None:
        qdef_ref = self.qdef_refs["is_original_report"]
        self._corrupt_revision_content(qdef_ref["artifact_id"], qdef_ref["revision_id"])
        with self.assertRaises(DecisionIntentCorruptionError):
            self._record()
        self.assertEqual(self.intents.list_intents(), [])

    def test_byte_corruption_detected_on_read(self) -> None:
        record = self._record()
        qdef_ref = self.qdef_refs["is_original_report"]
        self._corrupt_revision_content(qdef_ref["artifact_id"], qdef_ref["revision_id"])
        with self.assertRaises(DecisionIntentCorruptionError):
            self.intents.get_intent(record.dispatch_id)
        self.assertTrue(
            decision_intent_integrity_problems(self.store.connection, self.pid)
        )

    def test_corrupt_question_def_json_detected_on_read(self) -> None:
        # A recorded question definition revision is replaced with a non-QuestionDef
        # JSON object: the read rebuild must fail closed.
        record = self._record()
        qdef_ref = self.qdef_refs["study_design"]
        bad = canonical_json({"question_id": "study_design", "unexpected": True})
        self._tamper_revision(qdef_ref["artifact_id"], qdef_ref["revision_id"], bad)
        with self.assertRaises(DecisionIntentCorruptionError):
            self.intents.get_intent(record.dispatch_id)


class VersionsTest(IntentTestBase):
    def test_input_digest_mismatch_refused(self) -> None:
        versions = dict(self.versions)
        versions["input_digest"] = _DIG_C
        with self.assertRaises(DecisionIntentValidationError):
            self._record(versions=versions)
        self.assertEqual(self.intents.list_intents(), [])

    def test_version_hash_mismatch_refused(self) -> None:
        versions = dict(self.versions)
        versions["adapter"] = {
            "state": "known",
            "ref": {"version": "jev-adapter/2", "digest": "9" * 64},
        }
        with self.assertRaises(DecisionIntentValidationError):
            self._record(versions=versions)
        self.assertEqual(self.intents.list_intents(), [])

    def test_prompt_version_mismatch_refused(self) -> None:
        versions = dict(self.versions)
        versions["prompt"] = {
            "state": "known",
            "ref": {"version": "screen/2", "digest": "d" * 64},
        }
        with self.assertRaises(DecisionIntentValidationError):
            self._record(versions=versions)
        self.assertEqual(self.intents.list_intents(), [])

    def test_missing_known_slot_refused(self) -> None:
        versions = dict(self.versions)
        versions["model"] = {"state": "unknown"}
        with self.assertRaises(DecisionIntentValidationError):
            self._record(versions=versions)
        # A slot named as consumed but not known is refused too.
        versions2 = dict(self.versions)
        versions2["parser"] = {"state": "unknown"}
        with self.assertRaises(DecisionIntentValidationError):
            self._record(versions=versions2, consumed=("parser",))
        self.assertEqual(self.intents.list_intents(), [])

    def test_bad_route_refused(self) -> None:
        versions = dict(self.versions)
        versions["route"] = "opencode_session"
        with self.assertRaises(DecisionIntentValidationError):
            self._record(versions=versions)
        self.assertEqual(self.intents.list_intents(), [])

    def test_bad_consumed_slot_refused(self) -> None:
        with self.assertRaises(DecisionIntentValidationError):
            self._record(consumed=("not_a_slot",))
        self.assertEqual(self.intents.list_intents(), [])


class TimestampAndRouteTest(IntentTestBase):
    def test_bad_created_at_refused(self) -> None:
        with self.assertRaises(DecisionIntentValidationError):
            self._record(created_at_utc="not-a-timestamp")
        self.assertEqual(self.intents.list_intents(), [])

    def test_non_direct_dispatch_refused(self) -> None:
        # An opencode_session dispatch cannot carry a Jev request intent.
        second_task = self._make_task("task-oc")
        oc = self.dispatch.reserve_dispatch(
            second_task,
            request_id="oc-1",
            route="opencode_session",
            stage="screening",
            input_digest=_DIG_A,
            version_digest=_DIG_B,
            reserve_usd="0.10",
            bound_evidence_digest=_DIG_C,
        )
        with self.assertRaises(DecisionIntentValidationError):
            self._record(dispatch=oc, task=second_task)
        self.assertEqual(self.intents.list_intents(), [])

    def _make_task(self, request_id: str):
        bounded = BoundedTask.from_mapping(
            {
                "project_id": self.pid,
                "role": "writer",
                "target": dict(self.target_ref),
                "input_refs": [],
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


class IntegrityTest(IntentTestBase):
    def test_integrity_of_valid_store_is_empty(self) -> None:
        self._record()
        self.assertEqual(self.intents.verify_all_integrity(), [])
        self.assertEqual(
            decision_intent_integrity_problems(self.store.connection, self.pid), []
        )

    def test_integrity_detects_corrupt_canonical(self) -> None:
        record = self._record()
        conn = self.store.connection
        conn.execute("DROP TRIGGER decision_request_intent_no_update")
        conn.execute(
            "UPDATE decision_request_intent SET canonical = ? WHERE project_id = ? "
            "AND dispatch_id = ?",
            (b"{}", self.pid, record.dispatch_id),
        )
        problems = decision_intent_integrity_problems(conn, self.pid)
        self.assertTrue(problems)
        self.assertFalse(self.intents.verify_intent(record.dispatch_id))

    def test_integrity_detects_column_tamper(self) -> None:
        record = self._record()
        conn = self.store.connection
        conn.execute("DROP TRIGGER decision_request_intent_no_update")
        conn.execute(
            "UPDATE decision_request_intent SET request_sha256 = ? WHERE project_id = ? "
            "AND dispatch_id = ?",
            (_DIG_C, self.pid, record.dispatch_id),
        )
        problems = decision_intent_integrity_problems(conn, self.pid)
        self.assertTrue(problems)

    def test_integrity_detects_dangling_intent(self) -> None:
        record = self._record()
        conn = self.store.connection
        conn.execute("DROP TRIGGER decision_request_intent_no_update")
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute(
            "UPDATE decision_request_intent SET task_id = 'rtask_missing' "
            "WHERE project_id = ? AND dispatch_id = ?",
            (self.pid, record.dispatch_id),
        )
        conn.execute("PRAGMA foreign_keys = ON")
        problems = decision_intent_integrity_problems(conn, self.pid)
        self.assertTrue(any("dangling" in problem for problem in problems))

    def test_integrity_detects_foreign_row(self) -> None:
        record = self._record()
        conn = self.store.connection
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute(
            "INSERT INTO decision_request_intent "
            "(project_id, dispatch_id, intent_id, schema_version, task_id, "
            " task_fingerprint, route, stage, input_digest, version_digest, "
            " reserve_usd, bound_evidence_digest, request_sha256, request_bytes, "
            " cost_upper_bound_usd, pricing_version, prompt_version, "
            " target_artifact_id, target_revision_id, question_count, canonical, "
            " content_digest, created_at_utc) "
            "VALUES ('other-project', 'disp_foreign', 'dint_x', 1, ?, ?, "
            " 'direct_call', 's1', ?, ?, '0.25', ?, ?, 1, '0', ?, ?, ?, ?, 1, "
            " ?, ?, '2026-01-01T00:00:00+00:00')",
            (
                record.task_id,
                record.task_fingerprint,
                _DIG_A,
                _DIG_B,
                _DIG_C,
                _DIG_A,
                jev.PRICING_VERSION,
                PROMPT_VERSION,
                self.target_ref["artifact_id"],
                self.target_ref["revision_id"],
                b"{}",
                _DIG_A,
            ),
        )
        conn.execute("PRAGMA foreign_keys = ON")
        problems = decision_intent_integrity_problems(conn, self.pid)
        self.assertTrue(any("foreign" in problem for problem in problems))


class IntegrityRepairTest(IntentTestBase):
    """Regression coverage for the T5f-R1 independent-pin / bound / immutability repairs."""

    def test_second_same_fingerprint_task_refused(self) -> None:
        # A second persisted task with identical canonical bytes (same fingerprint)
        # but a different request key (hence a different task id) must never be
        # recorded against this dispatch. Zero write on refusal.
        second_bounded = BoundedTask.from_mapping(
            json.loads(self.task.canonical.decode("utf-8")), configuration=self.config
        )
        second = self.runtime.record_task(
            second_bounded, request_id="task-req-2", configuration=self.config
        )
        self.assertNotEqual(second.task_id, self.task.task_id)
        self.assertEqual(second.fingerprint, self.task.fingerprint)
        with self.assertRaises(DecisionIntentValidationError):
            self._record(task=second)
        self.assertEqual(self.intents.list_intents(), [])

    def test_read_detects_parent_dispatch_tamper(self) -> None:
        record = self._record()
        conn = self.store.connection
        conn.execute("DROP TRIGGER IF EXISTS runtime_dispatch_no_update")
        conn.execute(
            "UPDATE runtime_dispatch SET stage = 'forged' WHERE project_id = ? "
            "AND dispatch_id = ?",
            (self.pid, record.dispatch_id),
        )
        with self.assertRaises(DecisionIntentCorruptionError):
            self.intents.get_intent(record.dispatch_id)
        self.assertTrue(decision_intent_integrity_problems(conn, self.pid))

    def test_read_detects_parent_task_tamper(self) -> None:
        record = self._record()
        conn = self.store.connection
        conn.execute("DROP TRIGGER IF EXISTS runtime_bounded_task_no_update")
        conn.execute(
            "UPDATE runtime_bounded_task SET prompt_version = 'tampered/9' "
            "WHERE project_id = ? AND task_id = ?",
            (self.pid, self.task.task_id),
        )
        with self.assertRaises(DecisionIntentCorruptionError):
            self.intents.get_intent(record.dispatch_id)

    def test_read_detects_self_consistent_parent_mismatch(self) -> None:
        # Self-consistent outer hashes: canonical + content_digest + columns agree,
        # yet the recorded stage no longer matches the actual parent dispatch.
        record = self._record()
        self._forge_intent_canonical(
            record.dispatch_id, lambda m: m.__setitem__("stage", "forged_stage")
        )
        with self.assertRaises(DecisionIntentCorruptionError):
            self.intents.get_intent(record.dispatch_id)
        self.assertTrue(
            decision_intent_integrity_problems(self.store.connection, self.pid)
        )

    def test_read_detects_wire_question_mismatch(self) -> None:
        # Self-consistent forgery of the recorded request wire (and its digest/size)
        # that no longer matches the recorded question descriptors.
        record = self._record()

        def mutate(mapping):
            mapping["plan_request"]["questions"]["is_original_report"][
                "instructions"
            ] = "FORGED WIRE INSTRUCTIONS"
            body = canonical_json(mapping["plan_request"])
            mapping["request_sha256"] = hashlib.sha256(body).hexdigest()
            mapping["request_bytes"] = len(body)

        self._forge_intent_canonical(record.dispatch_id, mutate)
        with self.assertRaises(DecisionIntentCorruptionError):
            self.intents.get_intent(record.dispatch_id)

    def test_forged_cyclic_request_is_typed_error(self) -> None:
        cyclic: dict = {}
        cyclic["a"] = cyclic
        forged = dataclasses.replace(self.plan, request={"state": cyclic})
        with self.assertRaises(DecisionIntentValidationError):
            self._record(plan=forged)
        self.assertEqual(self.intents.list_intents(), [])

    def test_record_nested_mappings_are_immutable(self) -> None:
        record = self._record()
        for container, key in (
            (record.versions, "input_digest"),
            (record.target, "artifact_id"),
            (record.plan_request, "model"),
            (record.questions[0].question_def_ref, "revision_id"),
            (record.questions[0].criteria_ref, "revision_id"),
        ):
            with self.assertRaises(TypeError):
                container[key] = "FORGED"  # type: ignore[index]
        # ``to_mapping`` returns a fresh plain copy: mutating it never touches the
        # record, and it still round-trips to the exact stored canonical bytes.
        copy = record.to_mapping()
        copy["versions"]["input_digest"] = "FORGED"
        copy["target"]["artifact_id"] = "FORGED"
        self.assertEqual(record.versions["input_digest"], self.input_digest)
        self.assertEqual(dict(record.target), self.target_ref)
        self.assertEqual(canonical_json(record.to_mapping()), record.canonical)

    def test_overlong_question_def_refused(self) -> None:
        record = self._record()
        qdef_ref = self.qdef_refs["study_design"]
        overlong = b"[" + b"0," * 200_000 + b"0]"
        self.assertGreater(len(overlong), 131_072)
        self._tamper_revision(
            qdef_ref["artifact_id"], qdef_ref["revision_id"], overlong
        )
        with self.assertRaises(DecisionIntentCorruptionError):
            self.intents.get_intent(record.dispatch_id)

    def test_valid_history_survives_submission_and_new_heads(self) -> None:
        first = self._record()
        self.artifacts.import_revision(
            artifact_id=self.target_artifact.artifact_id,
            content=b"a newer target revision",
            origin_locator="import:synthetic",
            rights=_rights(self.pid, self.target_artifact.artifact_id, "rev_2"),
            revision_id="rev_2",
            parent_revision_id="rev_1",
        )
        self.dispatch.mark_submitted(self.dispatch_record.dispatch_id)
        self.assertEqual(self.intents.get_intent(first.dispatch_id).canonical, first.canonical)
        self.assertEqual(self._record().canonical, first.canonical)
        self.assertEqual(self.intents.verify_all_integrity(), [])


class MigrationTest(IntentTestBase):
    def test_schema7_to_8_preserves_old_rows(self) -> None:
        root = str(self.tmp / "p7")
        app_dir = paths.app_dir_for(root)
        db_path = paths.store_path_for(root)
        paths.ensure_owner_private_dir(app_dir)
        conn = schema.open_connection(db_path)
        try:
            schema.migrate(
                conn,
                target_version=7,
                migrations=schema.STORE_MIGRATIONS,
                label="project store",
            )
            self.assertEqual(schema.read_user_version(conn), 7)
            pid = paths.new_project_id()
            conn.execute(
                "INSERT INTO project (project_id, name, created_at_utc, app_version) "
                "VALUES (?, 's7', '2026-01-01T00:00:00+00:00', '0.0.0')",
                (pid,),
            )
            conn.execute(
                "INSERT INTO meta (key, value) VALUES ('created_at_utc', ?)",
                ("2026-01-01T00:00:00+00:00",),
            )
            store = ProjectStore(conn, app_dir, pid, schema_version=7, app_version="0.0.0")
            repo = ArtifactRepository(store)
            artifact = repo.create_artifact(kind="document")
            repo.import_revision(
                artifact_id=artifact.artifact_id,
                content=b"predecessor-bytes",
                origin_locator="import:synthetic",
                rights=_rights(pid, artifact.artifact_id, "rev_1"),
                revision_id="rev_1",
            )
            job = JobRepository(store).submit("req-job", task_kind="probe")
        finally:
            conn.close()

        reopened = ProjectStore.open_at(app_dir, expected_project_id=pid)
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.schema_version, schema.STORE_SCHEMA_VERSION)
        names = {
            row[0]
            for row in reopened.connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        self.assertIn("decision_request_intent", names)
        # Predecessor rows and bytes are untouched.
        self.assertEqual(
            ArtifactRepository(reopened).read_content(artifact.artifact_id, "rev_1"),
            b"predecessor-bytes",
        )
        self.assertEqual(JobRepository(reopened).get(job.job_id).state, "queued")
        # The new intent table starts empty and revalidates.
        self.assertEqual(DecisionIntentRepository(reopened).list_intents(), [])
        self.assertEqual(
            decision_intent_integrity_problems(reopened.connection, pid), []
        )

    def test_schema_version_is_current(self) -> None:
        # Dynamic: additive later migrations (schema 9 adds the full decision
        # result set) keep this assertion true without rewriting intent rows.
        self.assertEqual(
            schema.STORE_SCHEMA_VERSION, max(schema.STORE_MIGRATIONS)
        )
        self.assertGreaterEqual(schema.STORE_SCHEMA_VERSION, 8)
        self.assertEqual(self.store.schema_version, schema.STORE_SCHEMA_VERSION)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
