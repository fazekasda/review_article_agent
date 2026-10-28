"""Acceptance tests for the atomic local direct-Jev result **commit**.

This covers the Step 04 result-commit bridge
(``DecisionResultRepository.commit_result``) over a **real** temporary project
store, a real recorded request intent, and the real Jev adapter driven by
injected transports (no network, no credential, no paid call):

* one store transaction records the complete 1+N observation, settles the exact
  cost monotonically, and applies the transition the **validated** result envelope
  derives — never a caller terminal/success/authenticated boolean;
* an HTTP 200 result (**including** per-question failures) is accepted exactly once
  as ``result_kind=jev_decision_set`` / ``result_ref=result_id`` /
  ``result_digest=content_digest``; a per-question failure is retained and is never
  a scientific exclusion;
* a documented definitive pre-billing rejection (400/401/402/403/404/413) settles
  ``not_incurred`` and marks the dispatch ``failed`` — never a scientific negative;
* every transport/protocol failure or ambiguous API status (incl. 429/5xx) is
  reconciled to ``unknown`` with the reserve retained — a known reported cost does
  **not** resolve the operation and no result is accepted;
* idempotent replay (no fence change, no duplicate cost, an already-unknown replay
  stays unknown), divergent-replay and stale-fence refusal with zero mutation, and
  contradiction refusal before any write;
* an existing **inert** observation (`record_result` then `commit_result`) never
  implies the dispatch transition/cost were already committed: a pending
  submitted/unknown dispatch is transitioned by the commit without re-inserting
  rows, while a genuinely already-applied commit is a byte-exact no-op;
* injected fault seams after the set insert / after the cost / before the accept
  roll back the rows, the state, and the cost together;
* the commit path never nests a transaction (``ProjectStore.transaction`` cannot
  nest): it drives the transaction-owned primitives inside its single BEGIN.

Run::

    PYTHONPATH=src python3 -m unittest tests.test_decision_commit
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import sys
import tempfile
import unittest
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from review_article_agent.domain.records import canonical_json  # noqa: E402
from review_article_agent.runtime import jev  # noqa: E402
from review_article_agent.runtime.dispatch import (  # noqa: E402
    DispatchStaleFenceError,
    DispatchStateError,
    RuntimeDispatchRepository,
    runtime_reserve_exposure,
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
from review_article_agent.storage import store as store_module  # noqa: E402
from review_article_agent.storage.agent_runtime import (  # noqa: E402
    RuntimeRepository,
    validate_version_set,
)
from review_article_agent.storage.decision_intents import (  # noqa: E402
    DecisionIntentRepository,
)
from review_article_agent.storage.decision_results import (  # noqa: E402
    COMMIT_FAULT_SEAMS,
    RESULT_KIND,
    DecisionCommitOutcome,
    DecisionResultConflictError,
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


class CommitTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="raa-commit-")
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

    def commit(self, result, *, fence=1):
        return self.results.commit_result(
            self.dispatch_record.dispatch_id, result, fence=fence
        )

    def dispatch_result(self, transport):
        return jev.dispatch_plan(self.plan, api_key=API_KEY, transport=transport)

    def valid_result(self, **overrides):
        answers = dict(_wire_answers())
        answers.update(overrides.pop("answers", {}))
        transport = _CannedTransport(200, _wire_body(answers, **overrides))
        return self.dispatch_result(transport)

    def definitive_result(self, status=400):
        return self.dispatch_result(_CannedTransport(status, ""))

    def transport_error_result(self):
        return self.dispatch_result(_RaisingTransport(TimeoutError("simulated")))

    def protocol_error_result(self):
        return self.dispatch_result(_CannedTransport(200, "{not json"))

    def ambiguous_known_cost_result(self, status=429, cost=0.001):
        body = json.dumps({"usage": {"cost": cost}})
        return self.dispatch_result(_CannedTransport(status, body))

    def counts(self):
        conn = self.store.connection
        sets = conn.execute("SELECT COUNT(*) FROM decision_result_set").fetchone()[0]
        rows = conn.execute("SELECT COUNT(*) FROM decision_result_question").fetchone()[0]
        return sets, rows

    def reload_dispatch(self):
        return self.dispatch.get_dispatch(self.dispatch_record.dispatch_id)

    def snapshot_rows(self):
        """Raw byte-for-byte snapshots of the observation + dispatch rows."""
        conn = self.store.connection
        dispatch_id = self.dispatch_record.dispatch_id

        def one(sql, params):
            row = conn.execute(sql, params).fetchone()
            return None if row is None else tuple(row)

        def many(sql, params):
            return tuple(tuple(row) for row in conn.execute(sql, params).fetchall())

        return {
            "set": one(
                "SELECT * FROM decision_result_set WHERE project_id = ? AND "
                "dispatch_id = ?",
                (self.pid, dispatch_id),
            ),
            "questions": many(
                "SELECT * FROM decision_result_question WHERE project_id = ? AND "
                "dispatch_id = ? ORDER BY ordinal",
                (self.pid, dispatch_id),
            ),
            "dispatch": one(
                "SELECT * FROM runtime_dispatch WHERE project_id = ? AND "
                "dispatch_id = ?",
                (self.pid, dispatch_id),
            ),
            "state": one(
                "SELECT * FROM runtime_dispatch_state WHERE project_id = ? AND "
                "dispatch_id = ?",
                (self.pid, dispatch_id),
            ),
        }

    def assert_pristine(self, *, state="submitted", cost_state="unknown"):
        self.assertEqual(self.counts(), (0, 0))
        record = self.reload_dispatch()
        self.assertEqual(record.state, state)
        self.assertEqual(record.cost_state, cost_state)
        self.assertIsNone(record.cost_amount_usd)
        self.assertIsNone(record.result_digest)
        self.assertFalse(self.store.connection.in_transaction)


class CompletedCommitTest(CommitTestBase):
    def test_completed_http_200_accepted_once_and_cost_settled(self) -> None:
        self.submit()
        committed = self.commit(self.valid_result())

        self.assertIsInstance(committed, DecisionCommitOutcome)
        self.assertEqual(committed.outcome, "completed")
        self.assertFalse(committed.replayed)
        observation = committed.observation
        record = committed.dispatch

        # The dispatch is completed exactly once with the observation as its
        # accepted local result (kind/ref/digest from the stored observation).
        self.assertEqual(record.state, "completed")
        self.assertEqual(record.result_kind, RESULT_KIND)
        self.assertEqual(record.result_ref, observation.result_id)
        self.assertEqual(record.result_digest, observation.digest)
        self.assertEqual(record.current_fence, 1)
        # The exact provider-reported cost is settled (not the reserve).
        self.assertEqual(record.cost_state, "known")
        self.assertEqual(record.cost_amount_usd, "0.00002037")

        # Exactly one immutable set + one canonical row per question.
        self.assertEqual(self.counts(), (1, len(_SPECS)))
        self.assertEqual(observation.question_count, len(_SPECS))
        self.assertEqual(list(self.results.list_results())[0].canonical, observation.canonical)

        # Conservative exposure is max(reserve, actual) and is never lowered.
        self.assertEqual(
            runtime_reserve_exposure(self.store.connection, self.pid), Decimal("0.25")
        )
        self.assertEqual(self.results.verify_all_integrity(), [])
        self.assertEqual(self.dispatch.verify_all_integrity(), [])
        self.assertEqual(
            decision_result_integrity_problems(self.store.connection, self.pid), []
        )
        self.assertFalse(self.store.connection.in_transaction)

    def test_per_question_failures_retained_and_never_exclusion(self) -> None:
        self.submit()
        committed = self.commit(self.valid_result())
        self.assertEqual(committed.outcome, "completed")
        by_id = {
            r.question_id: r.record
            for r in self.results.list_question_records(self.dispatch_record.dispatch_id)
        }
        self.assertEqual(by_id["is_original_report"]["outcome"], "answered")
        self.assertEqual(by_id["missing_q"]["outcome"], "omitted")
        self.assertEqual(by_id["provider_err_q"]["outcome"], "omitted")
        self.assertEqual(by_id["refusal_q"]["outcome"], "refusal")
        self.assertEqual(by_id["confidence_level"]["outcome"], "malformed")
        self.assertEqual(by_id["free_text"]["outcome"], "unsupported")
        for record in by_id.values():
            self.assertFalse(record["is_exclusion"])
            self.assertTrue(record["retains_candidate"])
            self.assertFalse(record["grants_eligibility"])

    def test_completed_with_unknown_cost_still_completes_and_retains_reserve(self) -> None:
        self.submit()
        # A completed 200 envelope whose provider reported no cost: the operation
        # completed, yet the cost stays unknown and the reserve is retained.
        body = json.dumps(
            {
                "id": "gen-dec-2",
                "model": MODEL_SNAPSHOT,
                "provider": "TypeSafe",
                "answers": _wire_answers(),
                "usage": {"input_tokens": 485, "output_tokens": 76},
            }
        )
        result = self.dispatch_result(_CannedTransport(200, body))
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.cost.state, "unknown")

        committed = self.commit(result)
        self.assertEqual(committed.outcome, "completed")
        record = committed.dispatch
        self.assertEqual(record.state, "completed")
        self.assertEqual(record.result_kind, RESULT_KIND)
        self.assertEqual(record.cost_state, "unknown")
        self.assertIsNone(record.cost_amount_usd)
        self.assertEqual(
            runtime_reserve_exposure(self.store.connection, self.pid), Decimal("0.25")
        )


class DefinitiveFailureCommitTest(CommitTestBase):
    def test_definitive_400_fails_not_incurred_and_is_never_a_negative(self) -> None:
        self.submit()
        result = self.definitive_result(400)
        self.assertEqual(result.status, "api_error")
        self.assertEqual(result.cost.state, "not_incurred")

        committed = self.commit(result)
        self.assertEqual(committed.outcome, "failed")
        record = committed.dispatch
        self.assertEqual(record.state, "failed")
        self.assertIsNone(record.result_kind)
        self.assertIsNone(record.result_ref)
        self.assertIsNone(record.result_digest)
        self.assertEqual(record.cost_state, "not_incurred")
        self.assertIsNone(record.cost_amount_usd)

        # The failure diagnostic is persisted; no question became an exclusion.
        rows = self.results.list_question_records(self.dispatch_record.dispatch_id)
        self.assertEqual(len(rows), len(_SPECS))
        for row in rows:
            self.assertFalse(row.record["is_exclusion"])
            self.assertTrue(row.record["retains_candidate"])
            if row.question_kind == "box":
                self.assertEqual(row.outcome, "unsupported")
            else:
                self.assertEqual(row.outcome, "omitted")
                self.assertEqual(row.record["call_status"], "api_error")

        # The reserve is still counted (never released as zero exposure).
        self.assertEqual(
            runtime_reserve_exposure(self.store.connection, self.pid), Decimal("0.25")
        )
        self.assertEqual(self.results.verify_all_integrity(), [])

    def test_every_definitive_status_fails(self) -> None:
        for status in (400, 401, 402, 403, 404, 413):
            with self.subTest(status=status):
                self.setUp()
                self.submit()
                committed = self.commit(self.definitive_result(status))
                self.assertEqual(committed.outcome, "failed")
                self.assertEqual(committed.dispatch.state, "failed")
                self.assertEqual(committed.dispatch.cost_state, "not_incurred")


class AmbiguousOutcomeCommitTest(CommitTestBase):
    def _assert_unknown(self, result):
        self.submit()
        committed = self.commit(result)
        self.assertEqual(committed.outcome, "unknown")
        record = committed.dispatch
        self.assertEqual(record.state, "unknown")
        self.assertIsNone(record.result_digest)
        self.assertIsNone(record.result_kind)
        # The reserve is retained; the remote outcome is unresolved.
        self.assertEqual(
            runtime_reserve_exposure(self.store.connection, self.pid), Decimal("0.25")
        )
        # No new identity, no retry, no second submission.
        self.assertEqual(self.dispatch.list_dispatches()[0].dispatch_id,
                         self.dispatch_record.dispatch_id)
        with self.assertRaises(DispatchStateError):
            self.dispatch.mark_submitted(self.dispatch_record.dispatch_id)
        return committed

    def test_transport_error_recovers_unknown(self) -> None:
        committed = self._assert_unknown(self.transport_error_result())
        self.assertEqual(committed.dispatch.cost_state, "unknown")
        self.assertIsNone(committed.dispatch.cost_amount_usd)
        self.assertEqual(committed.observation.status, "transport_error")

    def test_ambiguous_429_recovers_unknown(self) -> None:
        committed = self._assert_unknown(self.dispatch_result(_CannedTransport(429, "")))
        self.assertEqual(committed.observation.status, "api_error")

    def test_ambiguous_500_recovers_unknown(self) -> None:
        self._assert_unknown(self.dispatch_result(_CannedTransport(500, "")))

    def test_protocol_error_recovers_unknown(self) -> None:
        committed = self._assert_unknown(self.protocol_error_result())
        self.assertEqual(committed.observation.status, "protocol_error")

    def test_known_reported_cost_does_not_resolve_the_operation(self) -> None:
        # A salvageable provider-reported cost on an ambiguous status is settled,
        # but the operation itself stays unresolved (unknown).
        result = self.ambiguous_known_cost_result(status=429, cost=0.001)
        self.assertEqual(result.status, "api_error")
        self.assertEqual(result.cost.state, "known")
        committed = self._assert_unknown(result)
        self.assertEqual(committed.dispatch.cost_state, "known")
        self.assertEqual(committed.dispatch.cost_amount_usd, "0.001")
        self.assertEqual(committed.dispatch.state, "unknown")
        # The exact-decimal amount is preserved verbatim (no float round-trip).
        self.assertEqual(committed.observation.cost["amount_usd"], "0.001")


class IdempotenceAndRefusalTest(CommitTestBase):
    def test_completed_replay_is_idempotent(self) -> None:
        self.submit()
        result = self.valid_result()
        first = self.commit(result)
        second = self.commit(result)

        self.assertTrue(second.replayed)
        self.assertEqual(second.observation.canonical, first.observation.canonical)
        self.assertEqual(second.dispatch.state, "completed")
        self.assertEqual(second.dispatch.cost_amount_usd, "0.00002037")
        self.assertEqual(self.counts(), (1, len(_SPECS)))
        self.assertEqual(second.dispatch.current_fence, 1)
        # A replay writes nothing at all: the accepted-result triple and the
        # settled cost are byte-identical, including the update timestamp.
        self.assertEqual(
            (
                second.dispatch.result_kind,
                second.dispatch.result_ref,
                second.dispatch.result_digest,
            ),
            (
                first.dispatch.result_kind,
                first.dispatch.result_ref,
                first.dispatch.result_digest,
            ),
        )
        self.assertEqual(second.dispatch.updated_at_utc, first.dispatch.updated_at_utc)

    def test_unknown_replay_stays_unknown(self) -> None:
        self.submit()
        result = self.transport_error_result()
        first = self.commit(result)
        second = self.commit(result)

        self.assertEqual(first.outcome, "unknown")
        self.assertTrue(second.replayed)
        self.assertEqual(second.outcome, "unknown")
        self.assertEqual(second.dispatch.state, "unknown")
        self.assertIsNone(second.dispatch.result_digest)
        self.assertEqual(self.counts(), (1, len(_SPECS)))

    def test_failed_replay_stays_failed(self) -> None:
        self.submit()
        result = self.definitive_result(403)
        self.commit(result)
        second = self.commit(result)
        self.assertTrue(second.replayed)
        self.assertEqual(second.outcome, "failed")
        self.assertEqual(second.dispatch.state, "failed")
        self.assertEqual(second.dispatch.cost_state, "not_incurred")
        self.assertEqual(self.counts(), (1, len(_SPECS)))

    def test_restart_unknown_replay_mints_no_new_identity_or_submit(self) -> None:
        self.submit()
        result = self.transport_error_result()
        self.commit(result)
        # A new process opens the same store: fresh repositories over one store.
        fresh_dispatch = RuntimeDispatchRepository(self.store)
        fresh_results = DecisionResultRepository(self.store)
        reloaded = fresh_dispatch.get_dispatch(self.dispatch_record.dispatch_id)
        self.assertEqual(reloaded.state, "unknown")
        self.assertEqual(len(fresh_dispatch.list_dispatches()), 1)

        replay = fresh_results.commit_result(
            self.dispatch_record.dispatch_id, result, fence=1
        )
        self.assertTrue(replay.replayed)
        self.assertEqual(replay.dispatch.state, "unknown")
        self.assertEqual(len(fresh_dispatch.list_dispatches()), 1)
        with self.assertRaises(DispatchStateError):
            fresh_dispatch.mark_submitted(self.dispatch_record.dispatch_id)

    def test_divergent_replay_refused_with_zero_mutation(self) -> None:
        self.submit()
        self.commit(self.valid_result())
        divergent = self.dispatch_result(
            _CannedTransport(
                200, _wire_body({"is_original_report": {"type": "noul", "noul": 0.01}})
            )
        )
        with self.assertRaises(DecisionResultConflictError):
            self.commit(divergent)
        self.assertEqual(self.counts(), (1, len(_SPECS)))
        self.assertEqual(self.reload_dispatch().state, "completed")

    def test_stale_fence_refused_with_zero_mutation(self) -> None:
        self.submit()
        with self.assertRaises(DecisionResultStaleFenceError):
            self.commit(self.valid_result(), fence=2)
        self.assert_pristine()

    def test_contradictory_envelope_refused_before_any_write(self) -> None:
        self.submit()
        result = self.valid_result()
        # A completed 200 envelope may never carry a not_incurred cost.
        forged = dataclasses.replace(
            result, cost=jev.CostRecord(state="not_incurred", reserve_usd="0.25")
        )
        with self.assertRaises(DecisionResultValidationError):
            self.commit(forged)
        self.assert_pristine()

    def test_reserved_dispatch_refused(self) -> None:
        # No submission was recorded, so no request could have been issued.
        with self.assertRaises(DecisionResultStateError):
            self.commit(self.valid_result())
        self.assert_pristine(state="reserved", cost_state="not_incurred")

    def test_non_result_object_refused(self) -> None:
        self.submit()
        with self.assertRaises(DecisionResultValidationError):
            self.results.commit_result(
                self.dispatch_record.dispatch_id, object(), fence=1
            )
        self.assert_pristine()


class PendingInertObservationTest(CommitTestBase):
    """An inert ``record_result`` observation must never masquerade as committed."""

    def inert(self, result):
        return self.results.record_result(
            self.dispatch_record.dispatch_id, result, fence=1
        )

    def test_submitted_success_inert_observation_is_applied_atomically(self) -> None:
        self.submit()
        result = self.valid_result()
        observation = self.inert(result)
        self.assertEqual(self.reload_dispatch().state, "submitted")

        committed = self.commit(result)
        self.assertEqual(committed.outcome, "completed")
        self.assertTrue(committed.observation_reused)
        self.assertFalse(committed.replayed)
        self.assertEqual(committed.dispatch.state, "completed")
        self.assertEqual(committed.dispatch.result_kind, RESULT_KIND)
        self.assertEqual(committed.dispatch.result_ref, observation.result_id)
        self.assertEqual(committed.dispatch.result_digest, observation.digest)
        self.assertEqual(committed.dispatch.cost_state, "known")
        self.assertEqual(committed.dispatch.cost_amount_usd, "0.00002037")
        # No duplicate observation rows and no new identity.
        self.assertEqual(self.counts(), (1, len(_SPECS)))
        self.assertEqual(len(self.dispatch.list_dispatches()), 1)

    def test_submitted_definitive_error_inert_observation_is_applied(self) -> None:
        self.submit()
        result = self.definitive_result(400)
        self.inert(result)

        committed = self.commit(result)
        self.assertEqual(committed.outcome, "failed")
        self.assertTrue(committed.observation_reused)
        self.assertFalse(committed.replayed)
        self.assertEqual(committed.dispatch.state, "failed")
        self.assertIsNone(committed.dispatch.result_digest)
        self.assertEqual(committed.dispatch.cost_state, "not_incurred")
        self.assertIsNone(committed.dispatch.cost_amount_usd)
        self.assertEqual(self.counts(), (1, len(_SPECS)))

    def test_submitted_ambiguous_known_cost_inert_observation_is_applied(self) -> None:
        self.submit()
        result = self.ambiguous_known_cost_result(status=429, cost=0.001)
        self.inert(result)

        committed = self.commit(result)
        self.assertEqual(committed.outcome, "unknown")
        self.assertTrue(committed.observation_reused)
        self.assertFalse(committed.replayed)
        self.assertEqual(committed.dispatch.state, "unknown")
        # The known reported cost is settled, but the operation stays unresolved.
        self.assertEqual(committed.dispatch.cost_state, "known")
        self.assertEqual(committed.dispatch.cost_amount_usd, "0.001")
        self.assertIsNone(committed.dispatch.result_digest)
        self.assertEqual(
            runtime_reserve_exposure(self.store.connection, self.pid), Decimal("0.25")
        )

    def test_submitted_timeout_inert_observation_is_applied(self) -> None:
        self.submit()
        result = self.transport_error_result()
        self.inert(result)

        committed = self.commit(result)
        self.assertEqual(committed.outcome, "unknown")
        self.assertTrue(committed.observation_reused)
        self.assertFalse(committed.replayed)
        self.assertEqual(committed.dispatch.state, "unknown")
        self.assertEqual(committed.dispatch.cost_state, "unknown")
        self.assertIsNone(committed.dispatch.result_digest)

    def test_recovered_unknown_success_inert_observation_completes(self) -> None:
        self.submit()
        self.dispatch.recover(self.dispatch_record.dispatch_id)
        result = self.valid_result()
        observation = self.inert(result)
        self.assertEqual(self.reload_dispatch().state, "unknown")

        committed = self.commit(result)
        self.assertEqual(committed.outcome, "completed")
        self.assertTrue(committed.observation_reused)
        self.assertFalse(committed.replayed)
        self.assertEqual(committed.dispatch.state, "completed")
        self.assertEqual(committed.dispatch.result_ref, observation.result_id)
        self.assertEqual(committed.dispatch.result_digest, observation.digest)

    def test_recovered_unknown_definitive_inert_observation_fails(self) -> None:
        self.submit()
        self.dispatch.recover(self.dispatch_record.dispatch_id)
        result = self.definitive_result(404)
        self.inert(result)

        committed = self.commit(result)
        self.assertEqual(committed.outcome, "failed")
        self.assertTrue(committed.observation_reused)
        self.assertFalse(committed.replayed)
        self.assertEqual(committed.dispatch.state, "failed")
        self.assertEqual(committed.dispatch.cost_state, "not_incurred")
        self.assertIsNone(committed.dispatch.result_digest)

    def test_recovered_unknown_ambiguous_inert_observation_stays_unknown(self) -> None:
        self.submit()
        self.dispatch.recover(self.dispatch_record.dispatch_id)
        result = self.transport_error_result()
        self.inert(result)

        committed = self.commit(result)
        self.assertEqual(committed.outcome, "unknown")
        self.assertTrue(committed.observation_reused)
        # Already-unknown state and already-unknown cost: the entire commit is a
        # no-op (nothing was written), so this one legitimately reports a replay.
        self.assertTrue(committed.replayed)
        self.assertEqual(committed.dispatch.state, "unknown")
        self.assertIsNone(committed.dispatch.result_digest)
        self.assertEqual(self.counts(), (1, len(_SPECS)))

    def test_restart_then_inert_observation_commit_completes_once(self) -> None:
        self.submit()
        result = self.valid_result()
        self.inert(result)
        # A fresh process opens the same store: fresh repositories over one store.
        fresh_dispatch = RuntimeDispatchRepository(self.store)
        fresh_results = DecisionResultRepository(self.store)
        self.assertEqual(
            fresh_dispatch.get_dispatch(self.dispatch_record.dispatch_id).state,
            "submitted",
        )
        committed = fresh_results.commit_result(
            self.dispatch_record.dispatch_id, result, fence=1
        )
        self.assertEqual(committed.outcome, "completed")
        self.assertTrue(committed.observation_reused)
        self.assertFalse(committed.replayed)
        self.assertEqual(committed.dispatch.state, "completed")
        self.assertEqual(len(fresh_dispatch.list_dispatches()), 1)
        with self.assertRaises(DispatchStateError):
            fresh_dispatch.mark_submitted(self.dispatch_record.dispatch_id)

    def test_conflicting_accepted_triple_rejected_with_zero_mutation(self) -> None:
        self.submit()
        result = self.valid_result()
        self.results.record_result(self.dispatch_record.dispatch_id, result, fence=1)
        # A different accepted result already claimed the dispatch.
        self.dispatch.accept_result(
            self.dispatch_record.dispatch_id,
            fence=1,
            result_kind="other_kind",
            result_ref="res_other",
            result_digest=_DIG_A,
        )
        before = self.snapshot_rows()
        with self.assertRaises(DecisionResultConflictError):
            self.commit(result)
        self.assertEqual(self.snapshot_rows(), before)
        self.assertEqual(self.reload_dispatch().result_kind, "other_kind")
        self.assertFalse(self.store.connection.in_transaction)

    def test_completed_dispatch_with_ambiguous_inert_observation_refused(self) -> None:
        self.submit()
        result = self.transport_error_result()
        observation = self.results.record_result(
            self.dispatch_record.dispatch_id, result, fence=1
        )
        self.dispatch.accept_result(
            self.dispatch_record.dispatch_id,
            fence=1,
            result_kind=RESULT_KIND,
            result_ref=observation.result_id,
            result_digest=observation.digest,
        )
        before = self.snapshot_rows()
        with self.assertRaises(DecisionResultConflictError):
            self.commit(result)
        self.assertEqual(self.snapshot_rows(), before)

    def test_fully_applied_replay_is_a_byte_exact_noop(self) -> None:
        self.submit()
        result = self.valid_result()
        self.commit(result)
        before = self.snapshot_rows()

        replay = self.commit(result)
        self.assertTrue(replay.replayed)
        self.assertTrue(replay.observation_reused)
        self.assertEqual(replay.dispatch.state, "completed")
        # Bytes, timestamps, fence and cost are all untouched.
        self.assertEqual(self.snapshot_rows(), before)
        self.assertFalse(self.store.connection.in_transaction)


    def test_fully_applied_replay_preserves_a_stronger_external_settlement(self) -> None:
        self.submit()
        result = self.valid_result()
        first = self.commit(result)
        # An external caller settles a stronger actual cost on the same dispatch.
        stronger = self.dispatch.settle_cost(
            self.dispatch_record.dispatch_id, cost_state="known", amount_usd="2.00"
        )
        self.assertEqual(stronger.cost_amount_usd, "2.00")

        replay = self.commit(result)
        self.assertTrue(replay.replayed)
        self.assertEqual(replay.dispatch.cost_state, "known")
        # The stronger settlement is preserved: never lowered back to the observed
        # amount, and never reported as absent.
        self.assertEqual(replay.dispatch.cost_amount_usd, "2.00")
        self.assertEqual(
            runtime_reserve_exposure(self.store.connection, self.pid), Decimal("2.00")
        )
        self.assertEqual(replay.observation.cost["amount_usd"], "0.00002037")

    def test_ambiguous_observation_does_not_downgrade_a_stronger_known_cost(self) -> None:
        self.submit()
        result = self.ambiguous_known_cost_result(status=429, cost=0.001)
        self.results.record_result(self.dispatch_record.dispatch_id, result, fence=1)
        # A stronger actual cost is already established before the commit.
        self.dispatch.settle_cost(
            self.dispatch_record.dispatch_id, cost_state="known", amount_usd="2.00"
        )
        committed = self.commit(result)
        self.assertEqual(committed.outcome, "unknown")
        self.assertEqual(committed.dispatch.state, "unknown")
        self.assertEqual(committed.dispatch.cost_state, "known")
        self.assertEqual(committed.dispatch.cost_amount_usd, "2.00")


class FaultSeamTest(CommitTestBase):
    def _install_raising_hook(self, seam):
        def hook(**_info):
            raise RuntimeError(f"forced fault at {seam}")

        self.results.add_commit_fault_hook(seam, hook)
        self.addCleanup(lambda: self.results._commit_hooks[seam].clear())

    def _assert_seam_rolls_back_everything(self, seam):
        self.submit()
        self._install_raising_hook(seam)
        with self.assertRaises(RuntimeError):
            self.commit(self.valid_result())
        # The 1+N observation, the cost settlement, and the dispatch state all
        # rolled back together; the dispatch is exactly as the submission left it.
        self.assert_pristine()
        # A subsequent clean commit still lands atomically.
        self.results._commit_hooks[seam].clear()
        committed = self.commit(self.valid_result())
        self.assertEqual(committed.outcome, "completed")
        self.assertEqual(self.counts(), (1, len(_SPECS)))
        self.assertEqual(committed.dispatch.cost_state, "known")

    def test_all_fault_seams_roll_back_all_rows_state_and_cost(self) -> None:
        for seam in COMMIT_FAULT_SEAMS:
            with self.subTest(seam=seam):
                self.setUp()
                self._assert_seam_rolls_back_everything(seam)

    def test_unknown_seam_name_refused(self) -> None:
        with self.assertRaises(DecisionResultValidationError):
            self.results.add_commit_fault_hook("not_a_seam", lambda **_: None)

    def test_after_cost_fault_preserves_preexisting_rows_and_rolls_back_apply(self) -> None:
        self.submit()
        result = self.valid_result()
        self.results.record_result(self.dispatch_record.dispatch_id, result, fence=1)
        before = self.snapshot_rows()

        self._install_raising_hook("after_cost")
        with self.assertRaises(RuntimeError):
            self.commit(result)
        # The prior inert 1+N rows are preserved byte-exact; only the newly applied
        # dispatch state and cost rolled back.
        after = self.snapshot_rows()
        self.assertIsNotNone(before["set"])
        self.assertEqual(after["set"], before["set"])
        self.assertEqual(after["questions"], before["questions"])
        self.assertEqual(after["dispatch"], before["dispatch"])
        self.assertEqual(after["state"], before["state"])
        self.assertEqual(self.reload_dispatch().state, "submitted")
        self.assertEqual(self.reload_dispatch().cost_state, "unknown")
        self.assertFalse(self.store.connection.in_transaction)

        # A clean commit then applies the pending transition exactly once.
        self.results._commit_hooks["after_cost"].clear()
        committed = self.commit(result)
        self.assertEqual(committed.outcome, "completed")
        self.assertTrue(committed.observation_reused)
        self.assertFalse(committed.replayed)
        self.assertEqual(committed.dispatch.state, "completed")
        self.assertEqual(self.counts(), (1, len(_SPECS)))


class SingleTransactionTest(CommitTestBase):
    def test_commit_opens_exactly_one_store_transaction(self) -> None:
        self.submit()
        original = store_module.transaction
        calls = {"n": 0}

        @contextmanager
        def counting(conn):
            calls["n"] += 1
            with original(conn) as live:
                yield live

        store_module.transaction = counting
        try:
            committed = self.commit(self.valid_result())
        finally:
            store_module.transaction = original
        self.assertEqual(committed.outcome, "completed")
        self.assertEqual(calls["n"], 1)
        self.assertFalse(self.store.connection.in_transaction)

    def test_transaction_owned_primitives_refuse_no_transaction(self) -> None:
        # The transaction-owned primitives fail closed when reached without a
        # caller-owned transaction (this is what keeps the commit path from ever
        # nesting a BEGIN).
        self.submit()
        result = self.valid_result()
        with self.assertRaises(DecisionResultStateError):
            self.results._record_result_in_transaction(
                self.dispatch_record.dispatch_id, result, fence=1
            )
        with self.assertRaises(DispatchStateError):
            self.dispatch._accept_result_in_transaction(
                self.dispatch_record.dispatch_id,
                fence=1,
                result_kind=RESULT_KIND,
                result_ref="dres_x",
                result_digest=_DIG_A,
            )
        with self.assertRaises(DispatchStateError):
            self.dispatch._settle_cost_in_transaction(
                self.dispatch_record.dispatch_id, cost_state="unknown"
            )
        with self.assertRaises(DispatchStateError):
            self.dispatch._recover_in_transaction(self.dispatch_record.dispatch_id)
        with self.assertRaises(DispatchStateError):
            self.dispatch._mark_failed_in_transaction(
                self.dispatch_record.dispatch_id
            )
        self.assert_pristine()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
