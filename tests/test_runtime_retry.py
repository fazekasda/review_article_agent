"""Acceptance tests for the bounded direct-call retry policy (ADR 006).

Synthetic data only (no secrets, no licensed source text, no model/network call).
Covers ``runtime/dispatch.py`` retry primitives and ``runtime/retry.py``:

* ``supersede_unknown_for_retry`` is allowed only from an ``unknown``
  ``direct_call`` dispatch with the deterministic successor id; refused otherwise;
  the predecessor becomes terminal ``failed`` with ``estimated`` = its full
  reserve, the remote slot is freed, and its cost counts in the budget;
* ``abandon_unknown_after_retries`` resolves an exhausted ambiguous dispatch the
  same way;
* ``next_attempt`` returns ``dispatch(base)`` for a fresh base, supersedes an
  ``unknown`` base and dispatches ``base-r1``, stops on ``completed`` / a
  definitive ``failed``, resolves 3 ambiguities to ``exhausted``, and resolves an
  overhead-cap refusal to ``refused_overhead`` (never leaving ``unknown``);
* deterministic ids are validated; backoff is injectable; a crash-recovered
  ``submitted`` -> ``unknown`` dispatch is retryable.

Run with::

    PYTHONPATH=src python3 -m unittest tests.test_runtime_retry
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from review_article_agent.runtime.dispatch import (  # noqa: E402
    DispatchStateError,
    DispatchValidationError,
    RuntimeDispatchRepository,
    is_retry_resolved,
    retry_request_ids,
    retry_successor_request_id,
    runtime_dispatch_remote_state,
    runtime_reserve_exposure,
)
from review_article_agent.runtime.retry import (  # noqa: E402
    RETRY_BACKOFF_SECONDS,
    RETRY_MAX_ATTEMPTS,
    backoff_seconds,
    next_attempt,
)
from review_article_agent.runtime.roles import (  # noqa: E402
    BoundedTask,
    RoleConfiguration,
)
from review_article_agent.storage import (  # noqa: E402
    ArtifactRepository,
    ProjectStore,
    paths,
)
from review_article_agent.storage.agent_runtime import RuntimeRepository  # noqa: E402

_OPS = ("acquisition", "storage", "redistribution", "hosted_transfer")

_DIG_A = "a" * 64
_DIG_B = "b" * 64
_DIG_C = "c" * 64


def _rights(project_id, artifact_id, revision_id):
    ops = {op: "allowed" for op in _OPS}
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


def _config():
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


class RetryTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="raa-retry-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.store = ProjectStore.create_at(
            paths.app_dir_for(str(self.tmp / "p")), paths.new_project_id()
        )
        self.addCleanup(self.store.close)
        self.runtime = RuntimeRepository(self.store)
        self.dispatch = RuntimeDispatchRepository(self.store)
        self.config = _config()
        self.artifact, self.revision = self._seed_revision()

    @property
    def pid(self) -> str:
        return self.store.project_id

    def _seed_revision(self):
        repo = ArtifactRepository(self.store)
        artifact = repo.create_artifact(kind="document")
        revision = repo.import_revision(
            artifact_id=artifact.artifact_id,
            content=b"retry-source-bytes",
            origin_locator="import:synthetic",
            rights=_rights(self.pid, artifact.artifact_id, "rev_1"),
            revision_id="rev_1",
        )
        return artifact, revision

    def make_task(self, request_id):
        mapping = {
            "project_id": self.pid,
            "role": "writer",
            "target": {
                "project_id": self.pid,
                "artifact_id": self.artifact.artifact_id,
                "revision_id": "rev_1",
            },
            "input_refs": [],
            "scope": ["section:introduction"],
            "prompt_version": "writer.v1",
            "size_limit": 4096,
            "step_limit": 4,
        }
        bounded = BoundedTask.from_mapping(mapping, configuration=self.config)
        return self.runtime.record_task(
            bounded, request_id=request_id, configuration=self.config
        )

    def reserve(self, request_id, *, stage="s1", reserve_usd="0.10", route="direct_call"):
        return self.dispatch.reserve_dispatch(
            self.make_task(request_id),
            request_id=request_id,
            route=route,
            stage=stage,
            input_digest=_DIG_A,
            version_digest=_DIG_B,
            reserve_usd=reserve_usd,
            bound_evidence_digest=_DIG_C,
        )

    def make_unknown(self, request_id, *, stage="s1", reserve_usd="0.10"):
        """A ``direct_call`` dispatch left ambiguous (unknown) as the adapters leave it."""
        record = self.reserve(
            request_id, stage=stage, reserve_usd=reserve_usd
        )
        self.dispatch.mark_submitted(record.dispatch_id)
        return self.dispatch.recover(record.dispatch_id)

    def make_submitted(self, request_id, *, stage="s1", reserve_usd="0.10"):
        record = self.reserve(
            request_id, stage=stage, reserve_usd=reserve_usd
        )
        return self.dispatch.mark_submitted(record.dispatch_id)


class RetryIdentityTest(unittest.TestCase):
    def test_chain_is_deterministic(self) -> None:
        self.assertEqual(RETRY_MAX_ATTEMPTS, 3)
        self.assertEqual(
            retry_request_ids("call-1"), ["call-1", "call-1-r1", "call-1-r2"]
        )
        self.assertEqual(retry_successor_request_id("call-1"), "call-1-r1")
        self.assertEqual(retry_successor_request_id("call-1-r1"), "call-1-r2")
        self.assertIsNone(retry_successor_request_id("call-1-r2"))

    def test_base_must_not_already_carry_a_retry_suffix(self) -> None:
        for bad in ("call-1-r1", "call-1-r2", "x-r9"):
            with self.assertRaises(DispatchValidationError):
                retry_request_ids(bad)
        # A structurally invalid identifier fails closed (ValueError).
        with self.assertRaises(ValueError):
            retry_request_ids("")
        with self.assertRaises(ValueError):
            retry_request_ids("has space")

    def test_backoff_schedule(self) -> None:
        self.assertEqual(RETRY_BACKOFF_SECONDS, (2.0, 8.0))
        self.assertEqual(backoff_seconds(0), 0.0)
        self.assertEqual(backoff_seconds(1), 2.0)
        self.assertEqual(backoff_seconds(2), 8.0)


class SupersedePrimitiveTest(RetryTestBase):
    def test_supersede_from_unknown_direct_call(self) -> None:
        record = self.make_unknown("call-1")
        self.assertEqual(record.state, "unknown")
        successor = self.dispatch.supersede_unknown_for_retry(
            record.dispatch_id, successor_request_id="call-1-r1"
        )
        self.assertEqual(successor.state, "failed")
        self.assertEqual(successor.cost_state, "estimated")
        self.assertEqual(successor.cost_amount_usd, "0.10")
        self.assertEqual(successor.reserve_usd, "0.10")
        self.assertTrue(is_retry_resolved(successor))
        self.assertEqual(successor.ended_at_utc is not None, True)
        # The remote slot is freed and the cost still counts.
        self.assertEqual(
            runtime_dispatch_remote_state(self.store.connection, self.pid), "none"
        )
        self.assertEqual(
            Decimal(runtime_reserve_exposure(self.store.connection, self.pid)),
            Decimal("0.10"),
        )
        self.assertEqual(self.dispatch.verify_all_integrity(), [])

    def test_supersede_refuses_wrong_successor(self) -> None:
        record = self.make_unknown("call-1")
        for bad in ("call-1-r2", "call-1", "other-r1", "call-1-r1-r1"):
            with self.assertRaises(DispatchValidationError):
                self.dispatch.supersede_unknown_for_retry(
                    record.dispatch_id, successor_request_id=bad
                )
        # Nothing changed.
        self.assertEqual(
            self.dispatch.get_dispatch(record.dispatch_id).state, "unknown"
        )

    def test_supersede_refuses_non_unknown_and_non_direct(self) -> None:
        reserved = self.reserve("call-1")
        with self.assertRaises(DispatchStateError):
            self.dispatch.supersede_unknown_for_retry(
                reserved.dispatch_id, successor_request_id="call-1-r1"
            )
        # A non-direct route is refused even when ambiguous.
        session = self.reserve("call-2", route="opencode_session")
        self.dispatch.mark_submitted(session.dispatch_id)
        self.dispatch.recover(session.dispatch_id)
        with self.assertRaises(DispatchStateError):
            self.dispatch.supersede_unknown_for_retry(
                session.dispatch_id, successor_request_id="call-2-r1"
            )
        # A definitive failure is never superseded.
        failed = self.reserve("call-3")
        self.dispatch.mark_failed(failed.dispatch_id)
        with self.assertRaises(DispatchStateError):
            self.dispatch.supersede_unknown_for_retry(
                failed.dispatch_id, successor_request_id="call-3-r1"
            )

    def test_supersede_second_hop(self) -> None:
        first = self.make_unknown("call-1")
        self.dispatch.supersede_unknown_for_retry(
            first.dispatch_id, successor_request_id="call-1-r1"
        )
        second = self.make_unknown("call-1-r1")
        resolved = self.dispatch.supersede_unknown_for_retry(
            second.dispatch_id, successor_request_id="call-1-r2"
        )
        self.assertEqual(resolved.state, "failed")
        self.assertTrue(is_retry_resolved(resolved))

    def test_abandon_from_unknown_direct_call(self) -> None:
        record = self.make_unknown("call-1")
        resolved = self.dispatch.abandon_unknown_after_retries(record.dispatch_id)
        self.assertEqual(resolved.state, "failed")
        self.assertTrue(is_retry_resolved(resolved))
        self.assertEqual(
            runtime_dispatch_remote_state(self.store.connection, self.pid), "none"
        )
        self.assertEqual(self.dispatch.verify_all_integrity(), [])
        with self.assertRaises(DispatchStateError):
            self.dispatch.abandon_unknown_after_retries(resolved.dispatch_id)
    def test_abandon_refuses_non_direct(self) -> None:
        session = self.reserve("call-2", route="opencode_session")
        self.dispatch.mark_submitted(session.dispatch_id)
        self.dispatch.recover(session.dispatch_id)
        with self.assertRaises(DispatchStateError):
            self.dispatch.abandon_unknown_after_retries(session.dispatch_id)


class NextAttemptTest(RetryTestBase):
    def setUp(self) -> None:
        super().setUp()
        self.slept: list[float] = []

    def _sleep(self, delay: float) -> None:
        self.slept.append(delay)

    def _next(self, base, **kwargs):
        return next_attempt(
            self.store, base, sleep=self._sleep, repo=self.dispatch, **kwargs
        )

    def test_fresh_base_dispatches_base(self) -> None:
        decision = self._next("call-1")
        self.assertEqual(decision.outcome, "dispatch")
        self.assertEqual(decision.request_id, "call-1")
        self.assertEqual(decision.attempt, 0)
        self.assertEqual(decision.delay_seconds, 0.0)
        self.assertEqual(self.slept, [])

    def test_reserved_row_is_reclaimed(self) -> None:
        self.reserve("call-1")
        decision = self._next("call-1")
        self.assertEqual(decision.outcome, "dispatch")
        self.assertEqual(decision.request_id, "call-1")

    def test_unknown_base_supersedes_and_dispatches_r1(self) -> None:
        base = self.make_unknown("call-1")
        decision = self._next("call-1")
        self.assertEqual(decision.outcome, "dispatch")
        self.assertEqual(decision.request_id, "call-1-r1")
        self.assertEqual(decision.attempt, 1)
        self.assertEqual(decision.delay_seconds, 2.0)
        self.assertEqual(self.slept, [2.0])
        self.assertTrue(
            is_retry_resolved(self.dispatch.get_dispatch(base.dispatch_id))
        )

    def test_completed_attempt_stops(self) -> None:
        base = self.make_unknown("call-1")
        self.dispatch.supersede_unknown_for_retry(
            base.dispatch_id, successor_request_id="call-1-r1"
        )
        attempt = self.make_submitted("call-1-r1")
        self.dispatch.accept_result(
            attempt.dispatch_id,
            fence=attempt.current_fence,
            result_kind="synthetic",
            result_ref="res_1",
            result_digest=_DIG_C,
        )
        decision = self._next("call-1")
        self.assertEqual(decision.outcome, "completed")
        self.assertEqual(decision.request_id, "call-1-r1")
        self.assertIsNotNone(decision.dispatch)

    def test_definitive_failure_is_not_retried(self) -> None:
        record = self.reserve("call-1")
        self.dispatch.mark_failed(record.dispatch_id)
        decision = self._next("call-1")
        self.assertEqual(decision.outcome, "definitive")
        self.assertEqual(decision.request_id, "call-1")

    def test_three_unknowns_exhaust(self) -> None:
        # A large non-chain spend in the stage lets the retries clear the overhead cap.
        self.reserve("other", reserve_usd="1.00")
        base = self.make_unknown("call-1")
        first = self._next("call-1")
        self.assertEqual(first.request_id, "call-1-r1")
        self.make_unknown("call-1-r1")
        second = self._next("call-1")
        self.assertEqual(second.request_id, "call-1-r2")
        self.make_unknown("call-1-r2")
        decision = self._next("call-1")
        self.assertEqual(decision.outcome, "exhausted")
        self.assertEqual(decision.request_id, "call-1-r2")
        last = self.dispatch.get_dispatch_by_request("call-1-r2")
        self.assertTrue(is_retry_resolved(last))
        self.assertEqual(
            runtime_dispatch_remote_state(self.store.connection, self.pid), "none"
        )
        self.assertTrue(is_retry_resolved(self.dispatch.get_dispatch(base.dispatch_id)))

    def test_overhead_cap_refuses_and_resolves(self) -> None:
        # No non-chain spend: the first retry is floored, the second is refused.
        self.make_unknown("call-1")
        first = self._next("call-1")
        self.assertEqual(first.request_id, "call-1-r1")
        self.make_unknown("call-1-r1")
        decision = self._next("call-1")
        self.assertEqual(decision.outcome, "refused_overhead")
        self.assertEqual(decision.request_id, "call-1-r1")
        resolved = self.dispatch.get_dispatch_by_request("call-1-r1")
        self.assertTrue(is_retry_resolved(resolved))
        self.assertEqual(
            runtime_dispatch_remote_state(self.store.connection, self.pid), "none"
        )

    def test_crash_recovered_submitted_is_retryable(self) -> None:
        submitted = self.make_submitted("call-1")
        self.assertEqual(submitted.state, "submitted")
        # A retry must not race an unreconciled send claim.
        with self.assertRaises(DispatchStateError):
            self._next("call-1")
        self.dispatch.recover(submitted.dispatch_id)
        decision = self._next("call-1")
        self.assertEqual(decision.outcome, "dispatch")
        self.assertEqual(decision.request_id, "call-1-r1")

    def test_no_sleep_for_first_attempt(self) -> None:
        decision = next_attempt(
            self.store, "call-1", sleep=None, repo=self.dispatch
        )
        self.assertEqual(decision.outcome, "dispatch")
        self.assertEqual(decision.delay_seconds, 0.0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
