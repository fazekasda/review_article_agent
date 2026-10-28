"""Acceptance tests for the durable full per-question Jev decision result set.

Covers ``src/review_article_agent/storage/decision_results.py`` against the task
T5g acceptance requirements and ``docs/contracts/02-gates-and-runtime.md``
§6.1/§6.2/§6.3/§8.1.1, over a **real** temporary project store:

* one immutable, complete per-dispatch observation binding the exact request /
  intent digest / version digest (never a caller-supplied ref/version) plus one
  canonical C02 ``DecisionRecord`` row per question, queryable by
  ``(dispatch_id, question_id)``;
* the trusted ``DispatchResult`` is compared field-by-field with the persisted
  intent (model/endpoint/request digest+size/reserve/cost bound and the single
  first-attempt envelope), and every question is rebuilt from the recorded refs;
* answered + missing-answer + provider-error + refusal + malformed + box outcomes
  are all retained (missing/provider -> canonical ``omitted`` with the EXISTING
  diagnostic; no fabricated answer), and a whole-call transport/API/protocol
  failure is retained as ``omitted`` with the existing global diagnostic;
* an ``unknown`` cost is never recorded as ``0`` and the dispatch state is never
  advanced (the observation is inert: no accepted result, no completion claim);
* idempotent same-result replay, divergent replay refusal, stale-fence refusal;
* the set + rows are written in one transaction (a forced row failure writes
  nothing), and the complete per-question set is verifiable row-count-wise;
* a read-only integrity sweep that detects foreign/orphan/partial/corrupt rows;
* ref-byte corruption and week-later head moves are detected on read while valid
  history survives.

No network, no credential, no paid call. Run:

    PYTHONPATH=src python3 -m unittest tests.test_decision_results
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
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
    DecisionIntentRepository,
)
from review_article_agent.storage.decision_results import (  # noqa: E402
    RESULT_SCHEMA_VERSION,
    DecisionResultConflictError,
    DecisionResultCorruptionError,
    DecisionResultRepository,
    DecisionResultStaleFenceError,
    DecisionResultStateError,
    DecisionResultValidationError,
    decision_result_integrity_problems,
)

_OPS = ("acquisition", "storage", "redistribution", "hosted_transfer")

_DIG_A = "a" * 64
_DIG_B = "b" * 64
_DIG_C = "c" * 64

PROMPT_VERSION = "screen/1"
API_KEY = "synthetic-not-a-real-key"

#: (question_id, kind, instructions, options, levels, criteria)
_SPECS = (
    ("is_original_report", "predicate", "Is it an original report?", (), (), None),
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
    ("provider_err_q", "predicate", "Provider error probe?", (), (), None),
    ("missing_q", "predicate", "Missing answer probe?", (), (), None),
    ("refusal_q", "predicate", "Refusal probe?", (), (), None),
    ("free_text", "box", "Explain your reasoning.", (), (), None),
)

MODEL_SNAPSHOT = "typesafe/jev-1.14-20261014"


def _wire_answers():
    """Wire answers covering answered/missing/provider/refusal/malformed/box."""
    return {
        "is_original_report": {"type": "noul", "noul": 0.97},
        "study_design": {
            "type": "choice",
            "choice": "randomised",
            "confidence": 0.81,
            "probabilities": {"randomised": 0.9, "observational": 0.1, "other": 0.0},
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


class ResultTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="raa-result-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.store = ProjectStore.create_at(
            paths.app_dir_for(str(self.tmp / "p")), paths.new_project_id()
        )
        self.addCleanup(self.store.close)
        self.artifacts = ArtifactRepository(self.store)
        self.runtime = RuntimeRepository(self.store)
        self.dispatch = RuntimeDispatchRepository(self.store)
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
            qdef_json = _question_def_json(qid, kind, instructions, options, levels, crit_ref)
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
        self.intent = self.intents.record_intent(
            self.task,
            self.dispatch_record,
            self.plan,
            question_def_ref=self.qdef_refs,
            criteria_ref=self.crit_refs,
            versions=self.versions,
            consumed=("rubric",),
        )

    # -- helpers ------------------------------------------------------------- #
    def submit(self):
        return self.dispatch.mark_submitted(self.dispatch_record.dispatch_id)

    def record(self, result, *, fence=1):
        return self.results.record_result(
            self.dispatch_record.dispatch_id, result, fence=fence
        )

    def dispatch_result(self, transport):
        return jev.dispatch_plan(self.plan, api_key=API_KEY, transport=transport)

    def valid_result(self, **overrides):
        answers = dict(_wire_answers())
        answers.update(overrides.pop("answers", {}))
        transport = _CannedTransport(200, _wire_body(answers, **overrides))
        return self.dispatch_result(transport)

    # -- tampering ----------------------------------------------------------- #
    def _corrupt_revision_content(self, artifact_id, revision_id, content=b"tampered"):
        conn = self.store.connection
        conn.execute("DROP TRIGGER IF EXISTS revision_no_update")
        conn.execute(
            "UPDATE revision SET content = ? WHERE project_id = ? AND artifact_id = ? "
            "AND revision_id = ?",
            (content, self.pid, artifact_id, revision_id),
        )

    def _drop_set_trigger(self):
        self.store.connection.execute(
            "DROP TRIGGER IF EXISTS decision_result_set_no_update"
        )


class RecordResultTest(ResultTestBase):
    def test_records_complete_set_and_rows(self) -> None:
        self.submit()
        result = self.valid_result()
        observation = self.record(result)

        self.assertEqual(observation.project_id, self.pid)
        self.assertEqual(observation.dispatch_id, self.dispatch_record.dispatch_id)
        self.assertEqual(observation.task_id, self.task.task_id)
        self.assertEqual(observation.intent_id, self.intent.intent_id)
        self.assertEqual(observation.intent_digest, self.intent.content_digest)
        self.assertEqual(observation.schema_version, RESULT_SCHEMA_VERSION)
        self.assertEqual(observation.status, "completed")
        self.assertEqual(observation.attempt_no, 1)
        self.assertEqual(observation.calls_attempted, 1)
        self.assertEqual(observation.prior_outcome, "none")
        self.assertFalse(observation.idempotent_admission_proven)
        self.assertTrue(observation.no_automatic_retry)
        self.assertEqual(observation.actual_model, MODEL_SNAPSHOT)
        self.assertEqual(observation.request_sha256, self.plan.request_sha256)
        self.assertEqual(observation.request_bytes, self.plan.request_bytes)
        self.assertEqual(observation.reserve_usd, "0.25")
        self.assertEqual(observation.cost_upper_bound_usd, self.plan.cost_upper_bound_usd)

        # Complete ordered question set (all seven questions, box retained).
        self.assertEqual(observation.question_count, len(_SPECS))
        self.assertEqual(list(observation.question_ids), [s[0] for s in _SPECS])
        self.assertEqual(len(observation.questions), len(_SPECS))
        for entry in observation.questions:
            self.assertTrue(entry.record_id.startswith("dec_"))
            self.assertEqual(len(entry.record_digest), 64)

        # The canonical bytes are the truth and the digest binds them.
        self.assertEqual(observation.digest, hashlib.sha256(observation.canonical).hexdigest())
        self.assertEqual(canonical_json(observation.to_mapping()), observation.canonical)

        # One canonical row per question, queryable by (dispatch, question_id).
        rows = self.results.list_question_records(self.dispatch_record.dispatch_id)
        self.assertEqual(len(rows), len(_SPECS))
        self.assertEqual([r.question_id for r in rows], [s[0] for s in _SPECS])
        row_q = self.results.get_question_record(
            self.dispatch_record.dispatch_id, "study_design"
        )
        self.assertEqual(row_q.record["answer"]["choice"], "randomised")
        self.assertEqual(row_q.content_digest, hashlib.sha256(row_q.canonical).hexdigest())

        self.assertEqual(self.results.verify_all_integrity(), [])
        self.assertEqual(
            decision_result_integrity_problems(self.store.connection, self.pid), []
        )

    def test_all_failure_outcomes_retained_without_fabrication(self) -> None:
        self.submit()
        observation = self.record(self.valid_result())
        by_id = {
            r.question_id: r.record
            for r in self.results.list_question_records(self.dispatch_record.dispatch_id)
        }
        self.assertEqual(by_id["is_original_report"]["outcome"], "answered")
        self.assertEqual(by_id["is_original_report"]["answer"]["probability"], "0.97")
        # missing answer -> canonical omitted via the adapter-outcome audit.
        self.assertEqual(by_id["missing_q"]["outcome"], "omitted")
        self.assertEqual(by_id["missing_q"]["adapter_outcome"], "partial_error")
        self.assertEqual(by_id["missing_q"]["per_question_error"]["reason"], "missing_answer")
        self.assertNotIn("answer", by_id["missing_q"])
        # provider error -> canonical omitted.
        self.assertEqual(by_id["provider_err_q"]["outcome"], "omitted")
        self.assertEqual(
            by_id["provider_err_q"]["per_question_error"]["reason"], "provider_error"
        )
        # refusal stays refusal; malformed stays malformed; box stays unsupported.
        self.assertEqual(by_id["refusal_q"]["outcome"], "refusal")
        self.assertEqual(by_id["confidence_level"]["outcome"], "malformed")
        self.assertEqual(by_id["free_text"]["outcome"], "unsupported")
        self.assertNotIn("answer", by_id["free_text"])
        for record in by_id.values():
            self.assertFalse(record["is_exclusion"])
            self.assertTrue(record["retains_candidate"])
            self.assertFalse(record["grants_eligibility"])

    def test_single_direct_charge_identical_across_rows(self) -> None:
        self.submit()
        observation = self.record(self.valid_result())
        rows = self.results.list_question_records(self.dispatch_record.dispatch_id)
        for row in rows:
            self.assertEqual(row.record["cost"], observation.to_mapping()["cost"])
            self.assertEqual(row.record["cost"]["dispatch_ref"], self.dispatch_record.dispatch_id)
        self.assertEqual(observation.cost["state"], "known")
        self.assertEqual(observation.cost["amount_usd"], "0.00002037")

    def test_whole_call_transport_error_retained_and_dispatch_not_completed(self) -> None:
        self.submit()
        result = self.dispatch_result(_RaisingTransport(TimeoutError("simulated")))
        self.assertEqual(result.status, "transport_error")
        self.assertTrue(result.cost.reserve_retained)
        self.assertIsNone(result.cost.amount_usd)

        observation = self.record(result)
        # The unknown cost is never recorded as a silent 0.
        self.assertEqual(observation.cost["state"], "unknown")
        self.assertNotIn("amount_usd", observation.cost)
        self.assertEqual(observation.status, "transport_error")
        self.assertIsNotNone(observation.diagnostic)

        # Inert: the dispatch state machine is untouched.
        reloaded = self.dispatch.get_dispatch(self.dispatch_record.dispatch_id)
        self.assertIn(reloaded.state, ("submitted", "unknown"))
        self.assertNotEqual(reloaded.state, "completed")

        rows = self.results.list_question_records(self.dispatch_record.dispatch_id)
        for row in rows:
            if row.question_kind == "box":
                self.assertEqual(row.outcome, "unsupported")
                self.assertNotIn("call_status", row.record)
            else:
                self.assertEqual(row.outcome, "omitted")
                self.assertEqual(row.record["call_status"], "transport_error")
                self.assertNotIn("answer", row.record)

    def _assert_envelope_failure(self, transport, status, cost_state):
        self.submit()
        result = self.dispatch_result(transport)
        self.assertEqual(result.status, status)
        observation = self.record(result)
        self.assertEqual(observation.status, status)
        self.assertEqual(observation.cost["state"], cost_state)
        # An unknown/not-incurred cost is never recorded as a silent 0.
        self.assertNotIn("amount_usd", observation.cost)
        # Every supported question is retained as omitted; the box is unsupported.
        for row in self.results.list_question_records(self.dispatch_record.dispatch_id):
            if row.question_kind == "box":
                self.assertEqual(row.outcome, "unsupported")
            else:
                self.assertEqual(row.outcome, "omitted")
                self.assertEqual(row.record["call_status"], status)
        return observation

    def test_429_ambiguous_api_error_unknown(self) -> None:
        self._assert_envelope_failure(_CannedTransport(429, ""), "api_error", "unknown")

    def test_500_ambiguous_api_error_unknown(self) -> None:
        self._assert_envelope_failure(_CannedTransport(500, ""), "api_error", "unknown")

    def test_malformed_json_protocol_error_unknown(self) -> None:
        self._assert_envelope_failure(
            _CannedTransport(200, "{not json"), "protocol_error", "unknown"
        )

    def test_401_definitive_rejection_not_incurred_without_zero(self) -> None:
        self._assert_envelope_failure(
            _CannedTransport(401, ""), "api_error", "not_incurred"
        )

    def test_idempotent_replay(self) -> None:
        self.submit()
        result = self.valid_result()
        first = self.record(result)
        second = self.record(result)
        self.assertEqual(first.result_id, second.result_id)
        self.assertEqual(first.canonical, second.canonical)
        self.assertEqual(len(self.results.list_results()), 1)
        self.assertEqual(len(self.results.list_question_records(self.dispatch_record.dispatch_id)), len(_SPECS))

    def test_divergent_replay_rejected(self) -> None:
        self.submit()
        self.record(self.valid_result())
        divergent = self.dispatch_result(
            _CannedTransport(200, _wire_body({"is_original_report": {"type": "noul", "noul": 0.01}}))
        )
        with self.assertRaises(DecisionResultConflictError):
            self.record(divergent)
        self.assertEqual(len(self.results.list_results()), 1)

    def test_stale_fence_rejected(self) -> None:
        self.submit()
        result = self.valid_result()
        with self.assertRaises(DecisionResultStaleFenceError):
            self.record(result, fence=2)
        self.assertEqual(self.results.list_results(), [])

    def test_reserved_dispatch_refused(self) -> None:
        result = self.valid_result()
        with self.assertRaises(DecisionResultStateError):
            self.record(result)
        self.assertEqual(self.results.list_results(), [])

    def test_blocked_unsubmitted_result_refused(self) -> None:
        self.submit()
        blocked = jev.execute(
            self.questions,
            "state",
            api_key=None,
            reserve_usd="0.25",
            authorized=False,
        )
        self.assertEqual(blocked.status, "blocked")
        with self.assertRaises(DecisionResultValidationError):
            self.record(blocked)
        self.assertEqual(self.results.list_results(), [])

    def test_forged_request_binding_refused(self) -> None:
        self.submit()
        result = self.valid_result()
        forged = dataclasses.replace(result, request_sha256=_DIG_C)
        with self.assertRaises(DecisionResultValidationError):
            self.record(forged)
        forged2 = dataclasses.replace(result, request_bytes=result.request_bytes + 1)
        with self.assertRaises(DecisionResultValidationError):
            self.record(forged2)
        forged3 = dataclasses.replace(result, reserve_usd="9.99")
        with self.assertRaises(DecisionResultValidationError):
            self.record(forged3)
        forged4 = dataclasses.replace(result, attempt_no=2)
        with self.assertRaises(DecisionResultValidationError):
            self.record(forged4)
        forged5 = dataclasses.replace(result, idempotent_admission_proven=True)
        with self.assertRaises(DecisionResultValidationError):
            self.record(forged5)
        self.assertEqual(self.results.list_results(), [])

    def test_non_result_and_foreign_project_refused(self) -> None:
        self.submit()
        with self.assertRaises(DecisionResultValidationError):
            self.results.record_result(
                self.dispatch_record.dispatch_id, object(), fence=1
            )
        self.assertEqual(self.results.list_results(), [])

    def test_cross_project_dispatch_invisible_and_refused(self) -> None:
        self.submit()
        other = ProjectStore.create_at(
            paths.app_dir_for(str(self.tmp / "p_other")), paths.new_project_id()
        )
        self.addCleanup(other.close)
        other_results = DecisionResultRepository(other)
        self.assertIsNone(other_results.get_result(self.dispatch_record.dispatch_id))
        with self.assertRaises(DecisionResultValidationError):
            other_results.record_result(
                self.dispatch_record.dispatch_id, self.valid_result(), fence=1
            )
        self.assertEqual(self.results.list_results(), [])

    def test_missing_intent_refused(self) -> None:
        second = self.runtime.record_task(
            BoundedTask.from_mapping(
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
            ),
            request_id="task-no-intent",
            configuration=self.config,
        )
        record = self.dispatch.reserve_dispatch(
            second,
            request_id="no-intent-call",
            route="direct_call",
            stage="screening",
            input_digest=_DIG_A,
            version_digest=_DIG_B,
            reserve_usd="0.25",
            bound_evidence_digest=_DIG_C,
        )
        self.dispatch.mark_submitted(record.dispatch_id)
        result = self.dispatch_result(_CannedTransport(200, _wire_body(_wire_answers())))
        with self.assertRaises(DecisionResultValidationError):
            self.results.record_result(record.dispatch_id, result, fence=1)

    def test_completed_exact_replay_ok_divergent_refused(self) -> None:
        self.submit()
        result = self.valid_result()
        first = self.record(result)
        # Accept the result through the dispatch state machine (a later task's job),
        # then require the exact recorded observation to replay idempotently.
        self.dispatch.accept_result(
            self.dispatch_record.dispatch_id,
            fence=1,
            result_kind="jev_decision",
            result_ref=first.result_id,
            result_digest=first.content_digest,
        )
        replay = self.record(result)
        self.assertEqual(replay.canonical, first.canonical)
        divergent = self.dispatch_result(
            _CannedTransport(200, _wire_body({"is_original_report": {"type": "noul", "noul": 0.5}}))
        )
        with self.assertRaises(DecisionResultConflictError):
            self.record(divergent)

    def test_forced_second_row_failure_writes_nothing(self) -> None:
        self.submit()
        result = self.valid_result()
        original = DecisionResultRepository._insert_question_row
        calls = {"n": 0}

        def failing(self, dispatch_id, ordinal, created, record):
            calls["n"] += 1
            if calls["n"] >= 2:
                raise RuntimeError("forced second-row failure")
            return original(self, dispatch_id, ordinal, created, record)

        DecisionResultRepository._insert_question_row = failing
        try:
            with self.assertRaises(RuntimeError):
                self.record(result)
        finally:
            DecisionResultRepository._insert_question_row = original
        self.assertEqual(self.results.list_results(), [])
        count = self.store.connection.execute(
            "SELECT COUNT(*) FROM decision_result_question"
        ).fetchone()[0]
        self.assertEqual(count, 0)

    def test_records_are_deep_frozen_with_no_aliases(self) -> None:
        self.submit()
        observation = self.record(self.valid_result())
        for container, key in (
            (observation.cost, "state"),
            (observation.usage, "input_tokens"),
            (observation.cost, "dispatch_ref"),
        ):
            with self.assertRaises(TypeError):
                container[key] = "FORGED"  # type: ignore[index]
        self.assertTrue(observation.questions)
        with self.assertRaises(AttributeError):
            observation.questions[0].outcome = "FORGED"  # type: ignore[misc]
        copy = observation.to_mapping()
        copy["cost"]["state"] = "FORGED"
        self.assertEqual(observation.cost["state"], "known")
        self.assertEqual(canonical_json(observation.to_mapping()), observation.canonical)
        row = self.results.get_question_record(self.dispatch_record.dispatch_id, "study_design")
        with self.assertRaises(TypeError):
            row.record["cost"]["state"] = "FORGED"  # type: ignore[index]


class IntegrityAndHistoryTest(ResultTestBase):
    def test_ref_byte_corruption_detected_on_read(self) -> None:
        self.submit()
        self.record(self.valid_result())
        qdef = self.qdef_refs["study_design"]
        self._corrupt_revision_content(qdef["artifact_id"], qdef["revision_id"])
        with self.assertRaises(DecisionResultCorruptionError):
            self.results.get_result(self.dispatch_record.dispatch_id)
        self.assertTrue(
            decision_result_integrity_problems(self.store.connection, self.pid)
        )

    def test_valid_history_survives_head_move_and_submission(self) -> None:
        self.submit()
        first = self.record(self.valid_result())
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
        self.assertEqual(
            self.results.get_result(first.dispatch_id).canonical, first.canonical
        )
        self.assertEqual(self.results.verify_all_integrity(), [])

    def test_integrity_detects_corrupt_set(self) -> None:
        self.submit()
        observation = self.record(self.valid_result())
        self._drop_set_trigger()
        self.store.connection.execute(
            "UPDATE decision_result_set SET canonical = ? WHERE project_id = ? "
            "AND dispatch_id = ?",
            (b"{}", self.pid, observation.dispatch_id),
        )
        self.assertTrue(
            decision_result_integrity_problems(self.store.connection, self.pid)
        )
        self.assertFalse(self.results.verify_result(observation.dispatch_id))

    def test_integrity_detects_foreign_row(self) -> None:
        self.submit()
        observation = self.record(self.valid_result())
        conn = self.store.connection
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute(
            "INSERT INTO decision_result_set "
            "(project_id, dispatch_id, result_id, schema_version, task_id, "
            " intent_id, intent_digest, input_digest, version_digest, "
            " request_sha256, request_bytes, cost_upper_bound_usd, reserve_usd, "
            " status, attempt_no, prior_outcome, cost_state, cost_amount_usd, "
            " question_count, canonical, content_digest, created_at_utc) "
            "VALUES ('other-project', 'disp_foreign', 'dres_x', 1, ?, 'dint_x', "
            " ?, ?, ?, ?, 1, '0', '0.25', 'completed', 1, 'none', 'unknown', NULL, "
            " 1, ?, ?, '2026-01-01T00:00:00+00:00')",
            (
                observation.task_id,
                _DIG_A,
                _DIG_A,
                _DIG_A,
                _DIG_A,
                b"{}",
                _DIG_A,
            ),
        )
        conn.execute("PRAGMA foreign_keys = ON")
        problems = decision_result_integrity_problems(conn, self.pid)
        self.assertTrue(any("foreign" in problem for problem in problems))

    def test_integrity_detects_partial_set(self) -> None:
        self.submit()
        observation = self.record(self.valid_result())
        conn = self.store.connection
        conn.execute("DROP TRIGGER IF EXISTS decision_result_question_no_delete")
        conn.execute(
            "DELETE FROM decision_result_question WHERE project_id = ? AND "
            "dispatch_id = ? AND question_id = ?",
            (self.pid, observation.dispatch_id, "missing_q"),
        )
        problems = decision_result_integrity_problems(conn, self.pid)
        self.assertTrue(problems)

    def test_orphan_question_row_detected(self) -> None:
        self.submit()
        self.record(self.valid_result())
        conn = self.store.connection
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
        conn.execute("PRAGMA foreign_keys = ON")
        problems = decision_result_integrity_problems(conn, self.pid)
        self.assertTrue(any("orphan" in problem for problem in problems))


class MigrationTest(ResultTestBase):
    def test_schema8_to_9_preserves_intent_and_rows(self) -> None:
        # A schema-8 store with a recorded intent upgrades to 9 additively.
        root = str(self.tmp / "p89")
        app_dir = paths.app_dir_for(root)
        db_path = paths.store_path_for(root)
        paths.ensure_owner_private_dir(app_dir)
        conn = schema.open_connection(db_path)
        try:
            schema.migrate(
                conn,
                target_version=8,
                migrations=schema.STORE_MIGRATIONS,
                label="project store",
            )
            self.assertEqual(schema.read_user_version(conn), 8)
            pid = paths.new_project_id()
            conn.execute(
                "INSERT INTO project (project_id, name, created_at_utc, app_version) "
                "VALUES (?, 's8', '2026-01-01T00:00:00+00:00', '0.0.0')",
                (pid,),
            )
            conn.execute(
                "INSERT INTO meta (key, value) VALUES ('created_at_utc', ?)",
                ("2026-01-01T00:00:00+00:00",),
            )
            store = ProjectStore(conn, app_dir, pid, schema_version=8, app_version="0.0.0")
            repo = ArtifactRepository(store)
            artifact = repo.create_artifact(kind="document")
            repo.import_revision(
                artifact_id=artifact.artifact_id,
                content=b"predecessor-bytes",
                origin_locator="import:synthetic",
                rights=_rights(pid, artifact.artifact_id, "rev_1"),
                revision_id="rev_1",
            )
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
        self.assertIn("decision_result_set", names)
        self.assertIn("decision_result_question", names)
        # Predecessor rows/bytes are untouched and the new tables revalidate empty.
        self.assertEqual(
            ArtifactRepository(reopened).read_content(artifact.artifact_id, "rev_1"),
            b"predecessor-bytes",
        )
        self.assertEqual(DecisionResultRepository(reopened).list_results(), [])
        self.assertEqual(
            decision_result_integrity_problems(reopened.connection, pid), []
        )

    def test_schema_version_is_at_least_nine(self) -> None:
        # Dynamic: this fix adds schema 9; older schema assertions stay paintable.
        self.assertGreaterEqual(schema.STORE_SCHEMA_VERSION, 9)
        self.assertEqual(self.store.schema_version, schema.STORE_SCHEMA_VERSION)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
