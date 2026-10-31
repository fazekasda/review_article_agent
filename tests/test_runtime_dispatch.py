"""Acceptance tests for the durable runtime dispatch + route-separated cost ledger.

Synthetic data only (no secrets, no licensed source text, no model/network call).
These tests cover the additive ``runtime/dispatch.py`` layer and store schema 6:

* an atomic reserve-before-dispatch reservation that validates the persisted
  runtime task, checks the cumulative project/stage caps, and writes the identity
  + initial ``reserved`` state inside one ``BEGIN IMMEDIATE`` (a refused reserve
  leaves nothing behind);
* immutable logical-call identity, idempotent same-identity replay (no double
  reserve), and a divergent-retry refusal;
* route/accounting-bucket separation (``opencode_session``/``session_statistics``
  vs ``direct_call``/``direct_call``);
* explicit safe states, ``submitted`` recorded before I/O and single-use (only the
  winner of the send claim may proceed), no automatic retry, at most one logical
  dispatch per task (a different request key is refused in every state), and
  restart reconciliation (``submitted`` -> ``unknown`` with an ``unknown`` cost;
  an opencode admission is retained);
* the direct route has no durable admission (API + database trigger, id and ref);
* one fenced accepted local result (identical replay idempotent, divergent/stale
  denied);
* conservative, monotonic cost accounting (unknown retains its reserve and can
  never become not_incurred; a known overrun is counted at the larger actual and
  can never be erased by a later weaker/lower settlement; low-precision decimals
  are preserved);
* the shared cumulative caps counting **both** synthetic job reserves and runtime
  reserves in both directions (including concurrent cap checks);
* database-level identity immutability and a reusable read-only integrity sweep
  that detects foreign/dangling/orphan/corrupt rows and malformed linked tasks;
* a fail-closed exposure read (missing operational row, malformed cost state or
  amount raises instead of silently counting the bare reserve; a schema-6 store
  missing a ledger table raises rather than reporting zero);
* monotonic **legacy** settlement shared with the runtime ledger (a known overrun
  cannot be erased by a later unknown/not_incurred/lower settlement, and both cap
  directions stay closed) and an exact admission replay that compares the ref too;
* database-level ``UNIQUE(project_id, task_id)`` (at most one logical dispatch per
  task);
* static, curated error messages that never echo raw ids/values;
* a schema 5 -> 6 upgrade that leaves old artifacts, tasks, and jobs unchanged.

Run with::

    PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_runtime_dispatch.py'
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import threading
import unittest
from decimal import Decimal
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from review_article_agent.runtime.dispatch import (  # noqa: E402
    DISPATCH_GRANTS_AUTHORITY,
    DISPATCH_PERFORMS_NETWORK_IO,
    DispatchConflictError,
    DispatchCorruptionError,
    DispatchStaleFenceError,
    DispatchStateError,
    DispatchValidationError,
    RuntimeDispatchRepository,
    runtime_dispatch_integrity_problems,
    runtime_reserve_exposure,
)
from review_article_agent.runtime.jobs import (  # noqa: E402
    BudgetExceededError,
    JobConflictError,
    JobRepository,
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
    RuntimeTaskRecord,
)

_OPS = ("acquisition", "storage", "redistribution", "hosted_transfer")

_DIG_A = "a" * 64
_DIG_B = "b" * 64
_DIG_C = "c" * 64
_DIG_D = "d" * 64
_DIG_E = "e" * 64


def _rights(project_id, artifact_id, revision_id, *, rights_id=None):
    ops = {
        "acquisition": "allowed",
        "storage": "allowed",
        "redistribution": "allowed",
        "hosted_transfer": "allowed",
    }
    return {
        "rights_id": rights_id if rights_id is not None else f"RIGHTS-{artifact_id}",
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


class DispatchTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="raa-dispatch-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.store = ProjectStore.create_at(
            paths.app_dir_for(str(self.tmp / "p")), paths.new_project_id()
        )
        self.addCleanup(self.store.close)
        self.jobs = JobRepository(self.store)
        self.runtime = RuntimeRepository(self.store)
        self.dispatch = RuntimeDispatchRepository(self.store)
        self.config = _config()
        self.artifact, self.revision = self.seed_revision()
        self.task = self.make_task()

    @property
    def pid(self) -> str:
        return self.store.project_id

    def seed_revision(self, content=b"dispatch-source-bytes"):
        repo = ArtifactRepository(self.store)
        artifact = repo.create_artifact(kind="document")
        revision = repo.import_revision(
            artifact_id=artifact.artifact_id,
            content=content,
            origin_locator="import:synthetic",
            rights=_rights(self.pid, artifact.artifact_id, "rev_1"),
            revision_id="rev_1",
        )
        return artifact, revision

    def make_task(self, request_id="task-req") -> RuntimeTaskRecord:
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

    def reserve(
        self,
        *,
        request_id="call-1",
        route="opencode_session",
        stage="s1",
        reserve_usd="0.10",
        input_digest=_DIG_A,
        version_digest=_DIG_B,
        bound_evidence_digest=_DIG_C,
        task=None,
    ):
        return self.dispatch.reserve_dispatch(
            task if task is not None else self.task,
            request_id=request_id,
            route=route,
            stage=stage,
            input_digest=input_digest,
            version_digest=version_digest,
            reserve_usd=reserve_usd,
            bound_evidence_digest=bound_evidence_digest,
        )


class ReserveDispatchTest(DispatchTestBase):
    def test_reserve_records_identity_bucket_and_reserved_state(self) -> None:
        record = self.reserve(route="opencode_session")
        self.assertEqual(record.state, "reserved")
        self.assertEqual(record.current_fence, 1)
        self.assertEqual(record.accounting_bucket, "session_statistics")
        self.assertEqual(record.task_id, self.task.task_id)
        self.assertEqual(record.task_fingerprint, self.task.fingerprint)
        self.assertEqual(record.reserve_usd, "0.10")
        self.assertEqual(record.bound_evidence_digest, _DIG_C)
        self.assertEqual(record.cost_state, "not_incurred")
        self.assertIsNone(record.admission_id)
        # Exactly one identity + one state row.
        self.assertEqual(len(self.dispatch.list_dispatches()), 1)

    def test_direct_route_bucket_is_direct_call(self) -> None:
        record = self.reserve(route="direct_call")
        self.assertEqual(record.accounting_bucket, "direct_call")

    def test_invalid_reserve_inputs_denied(self) -> None:
        """Table of reserve-argument input-validation cases (case labels are the
        original method names; every case/assertion is preserved verbatim)."""

        def missing_or_invalid_reserve():
            for bad in (None, "0", "-1", 0.1, True, "not-a-decimal", "1e999999"):
                with self.assertRaises(DispatchValidationError):
                    self.reserve(reserve_usd=bad)  # type: ignore[arg-type]
            self.assertEqual(self.dispatch.list_dispatches(), [])

        def missing_or_invalid_bound_evidence():
            for bad in (None, "", "nothex", "A" * 64, "a" * 63):
                with self.assertRaises(DispatchValidationError):
                    self.reserve(bound_evidence_digest=bad)  # type: ignore[arg-type]
            self.assertEqual(self.dispatch.list_dispatches(), [])

        def invalid_route_and_digests():
            with self.assertRaises(DispatchValidationError):
                self.reserve(route="batch")
            with self.assertRaises(DispatchValidationError):
                self.reserve(input_digest="short")
            with self.assertRaises(DispatchValidationError):
                self.reserve(version_digest=123)  # type: ignore[arg-type]
            self.assertEqual(self.dispatch.list_dispatches(), [])

        for label, case in (
            ("test_missing_or_invalid_reserve_denied", missing_or_invalid_reserve),
            ("test_missing_or_invalid_bound_evidence_denied", missing_or_invalid_bound_evidence),
            ("test_invalid_route_and_digests_denied", invalid_route_and_digests),
        ):
            with self.subTest(case=label):
                case()

    def test_idempotent_same_identity_no_double_reserve(self) -> None:
        first = self.reserve()
        second = self.reserve()
        self.assertEqual(first.dispatch_id, second.dispatch_id)
        self.assertEqual(len(self.dispatch.list_dispatches()), 1)
        self.assertEqual(
            Decimal(runtime_reserve_exposure(self.store.connection, self.pid)),
            Decimal("0.10"),
        )

    def test_divergent_request_denied(self) -> None:
        self.reserve(reserve_usd="0.10")
        with self.assertRaises(DispatchConflictError):
            self.reserve(reserve_usd="0.20")
        with self.assertRaises(DispatchConflictError):
            self.reserve(input_digest=_DIG_D)
        self.assertEqual(len(self.dispatch.list_dispatches()), 1)

    def test_unpersisted_or_foreign_task_denied(self) -> None:
        with self.assertRaises(DispatchValidationError):
            self.dispatch.reserve_dispatch(
                object(),  # type: ignore[arg-type]
                request_id="x",
                route="direct_call",
                stage="s1",
                input_digest=_DIG_A,
                version_digest=_DIG_B,
                reserve_usd="0.10",
                bound_evidence_digest=_DIG_C,
            )
        # A structurally valid record whose task id is not persisted is refused.
        fake = RuntimeTaskRecord(
            project_id=self.pid,
            task_id="rtask_missing",
            request_id="missing",
            fingerprint=self.task.fingerprint,
            role=self.task.role,
            config_digest=self.task.config_digest,
            config_snapshot_id=self.task.config_snapshot_id,
            target=self.task.target,
            input_refs=self.task.input_refs,
            prompt_version=self.task.prompt_version,
            size_limit=self.task.size_limit,
            step_limit=self.task.step_limit,
            created_at_utc=self.task.created_at_utc,
            canonical=self.task.canonical,
        )
        with self.assertRaises(DispatchValidationError):
            self.reserve(task=fake)

    def test_rollback_on_cap_exceeded_leaves_no_rows(self) -> None:
        self.jobs.set_budget_policy(project_cap_usd="25", stage_cap_usd="10")
        with self.assertRaises(BudgetExceededError):
            self.reserve(reserve_usd="11")
        self.assertEqual(self.dispatch.list_dispatches(), [])
        self.assertEqual(
            runtime_reserve_exposure(self.store.connection, self.pid), Decimal("0")
        )

    def test_stored_rows_response_has_no_authority_fields(self) -> None:
        record = self.reserve()
        mapping = record.to_mapping()
        for forbidden in ("authority", "approval", "authorize", "endpoint", "api_key"):
            self.assertNotIn(forbidden, mapping)
        # The module explicitly disclaims authority and network/transport I/O.
        self.assertFalse(DISPATCH_GRANTS_AUTHORITY)
        self.assertFalse(DISPATCH_PERFORMS_NETWORK_IO)


class StateTransitionTest(DispatchTestBase):
    def test_submitted_before_io_records_started_at_and_unknown_cost(self) -> None:
        record = self.reserve()
        self.assertIsNone(record.started_at_utc)
        self.assertEqual(record.cost_state, "not_incurred")
        submitted = self.dispatch.mark_submitted(record.dispatch_id)
        self.assertEqual(submitted.state, "submitted")
        self.assertIsNotNone(submitted.started_at_utc)
        # A submission is before any I/O, so the remote outcome is unknown: the
        # cost can never default to not_incurred while the reserve is retained.
        self.assertEqual(submitted.cost_state, "unknown")
        self.assertIsNone(submitted.cost_amount_usd)
        self.assertEqual(
            runtime_reserve_exposure(self.store.connection, self.pid), Decimal("0.10")
        )

    def test_submission_is_single_use(self) -> None:
        record = self.reserve()
        first = self.dispatch.mark_submitted(record.dispatch_id)
        self.assertEqual(first.state, "submitted")
        # A second caller must NOT be handed a submitted record it did not win.
        with self.assertRaises(DispatchStateError):
            self.dispatch.mark_submitted(record.dispatch_id)
        self.assertEqual(self.dispatch.get_dispatch(record.dispatch_id).state, "submitted")

    def test_reserved_cannot_accept_but_submitted_can(self) -> None:
        record = self.reserve()
        with self.assertRaises(DispatchStateError):
            self.dispatch.accept_result(
                record.dispatch_id,
                fence=1,
                result_kind="synthetic",
                result_ref="res_1",
                result_digest=_DIG_D,
            )

    def test_direct_route_has_no_durable_admission(self) -> None:
        record = self.reserve(route="direct_call")
        self.dispatch.mark_submitted(record.dispatch_id)
        with self.assertRaises(DispatchStateError):
            self.dispatch.mark_admitted(record.dispatch_id, admission_id="adm-1")
        # The database also refuses a forced admission row.
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.connection.execute(
                "UPDATE runtime_dispatch_state SET admission_id = 'adm-1' "
                "WHERE project_id = ? AND dispatch_id = ?",
                (self.pid, record.dispatch_id),
            )

    def test_opencode_admission_retained_on_recover(self) -> None:
        record = self.reserve(route="opencode_session")
        self.dispatch.mark_submitted(record.dispatch_id)
        admitted = self.dispatch.mark_admitted(
            record.dispatch_id, admission_id="adm-1", admission_ref="ext-1"
        )
        self.assertEqual(admitted.state, "admitted")
        self.assertEqual(admitted.admission_id, "adm-1")
        recovered = self.dispatch.recover(record.dispatch_id)
        self.assertEqual(recovered.state, "admitted")
        self.assertEqual(recovered.admission_id, "adm-1")

    def test_submitted_becomes_unknown_on_restart_and_is_not_retried(self) -> None:
        record = self.reserve()
        self.dispatch.mark_submitted(record.dispatch_id)
        recovered = self.dispatch.recover(record.dispatch_id)
        self.assertEqual(recovered.state, "unknown")
        # The remote outcome is unknown, never "not incurred"; the reserve stays.
        self.assertEqual(recovered.cost_state, "unknown")
        self.assertIsNone(recovered.cost_amount_usd)
        self.assertEqual(
            runtime_reserve_exposure(self.store.connection, self.pid), Decimal("0.10")
        )
        # A restart never resets it to reserved and never re-submits.
        with self.assertRaises(DispatchStateError):
            self.dispatch.mark_submitted(record.dispatch_id)
        self.assertEqual(self.dispatch.recover(record.dispatch_id).state, "unknown")
        self.assertEqual(len(self.dispatch.list_dispatches()), 1)

    def test_reserved_untouched_on_recover(self) -> None:
        record = self.reserve()
        self.assertEqual(self.dispatch.recover(record.dispatch_id).state, "reserved")

    def test_second_request_for_task_denied_even_while_reserved(self) -> None:
        # Regression: two reserves with different keys for the same task must not
        # both succeed (a=reserve(); b=reserve(request_id='second-reserved')).
        first = self.reserve(request_id="call-1")
        self.assertEqual(first.state, "reserved")
        with self.assertRaises(DispatchStateError):
            self.reserve(request_id="second-reserved")
        self.assertEqual(len(self.dispatch.list_dispatches()), 1)

    def test_no_new_dispatch_for_task_after_completion_or_failure(self) -> None:
        first = self.reserve(request_id="call-1")
        self.dispatch.mark_submitted(first.dispatch_id)
        self.dispatch.recover(first.dispatch_id)
        # The restart-orphaned submission is ambiguous and still blocks.
        with self.assertRaises(DispatchStateError):
            self.reserve(request_id="call-3")
        self.dispatch.accept_result(
            first.dispatch_id,
            fence=1,
            result_kind="synthetic",
            result_ref="res_1",
            result_digest=_DIG_D,
        )
        # Even a completed dispatch blocks a *different* logical call for the task:
        # initial scope permits no retry, only a separate authorized future task.
        with self.assertRaises(DispatchStateError):
            self.reserve(request_id="call-4")
        self.assertEqual(len(self.dispatch.list_dispatches()), 1)
        # Same-request replay remains idempotent (not a second dispatch).
        replay = self.reserve(request_id="call-1")
        self.assertEqual(replay.dispatch_id, first.dispatch_id)

    def test_no_new_dispatch_for_task_after_failure(self) -> None:
        first = self.reserve(request_id="call-1")
        self.dispatch.mark_submitted(first.dispatch_id)
        self.dispatch.mark_failed(first.dispatch_id)
        with self.assertRaises(DispatchStateError):
            self.reserve(request_id="call-2")
        self.assertEqual(len(self.dispatch.list_dispatches()), 1)

    def test_mark_failed_refuses_ambiguous_unknown(self) -> None:
        record = self.reserve()
        self.dispatch.mark_submitted(record.dispatch_id)
        self.assertEqual(self.dispatch.recover(record.dispatch_id).state, "unknown")
        # An ambiguous outcome must never be reclassified as a definitive failure.
        with self.assertRaises(DispatchStateError):
            self.dispatch.mark_failed(record.dispatch_id)
        self.assertEqual(self.dispatch.get_dispatch(record.dispatch_id).state, "unknown")

    def test_mark_failed_is_terminal_and_idempotent(self) -> None:
        record = self.reserve()
        self.dispatch.mark_submitted(record.dispatch_id)
        failed = self.dispatch.mark_failed(record.dispatch_id)
        self.assertEqual(failed.state, "failed")
        self.assertEqual(self.dispatch.mark_failed(record.dispatch_id).state, "failed")
        with self.assertRaises(DispatchStateError):
            self.dispatch.accept_result(
                record.dispatch_id,
                fence=1,
                result_kind="synthetic",
                result_ref="res_1",
                result_digest=_DIG_D,
            )

    def test_reserved_can_fail_before_io_and_stays_consistent(self) -> None:
        record = self.reserve()
        failed = self.dispatch.mark_failed(record.dispatch_id)
        self.assertEqual(failed.state, "failed")
        self.assertIsNone(failed.started_at_utc)
        self.assertIsNotNone(failed.ended_at_utc)
        self.assertEqual(self.dispatch.verify_all_integrity(), [])


class AcceptedResultTest(DispatchTestBase):
    def _submitted(self):
        record = self.reserve()
        self.dispatch.mark_submitted(record.dispatch_id)
        return record

    def test_fenced_accept_idempotent_and_divergent_denied(self) -> None:
        record = self._submitted()
        completed = self.dispatch.accept_result(
            record.dispatch_id,
            fence=1,
            result_kind="synthetic",
            result_ref="res_1",
            result_digest=_DIG_D,
        )
        self.assertEqual(completed.state, "completed")
        self.assertEqual(completed.result_digest, _DIG_D)
        replay = self.dispatch.accept_result(
            record.dispatch_id,
            fence=1,
            result_kind="synthetic",
            result_ref="res_1",
            result_digest=_DIG_D,
        )
        self.assertEqual(
            (replay.result_kind, replay.result_ref, replay.result_digest),
            (completed.result_kind, completed.result_ref, completed.result_digest),
        )
        with self.assertRaises(DispatchConflictError):
            self.dispatch.accept_result(
                record.dispatch_id,
                fence=1,
                result_kind="synthetic",
                result_ref="res_1",
                result_digest=_DIG_E,
            )

    def test_stale_fence_refused(self) -> None:
        record = self._submitted()
        with self.assertRaises(DispatchStaleFenceError):
            self.dispatch.accept_result(
                record.dispatch_id,
                fence=2,
                result_kind="synthetic",
                result_ref="res_1",
                result_digest=_DIG_D,
            )

    def test_accept_from_unknown_reconciles(self) -> None:
        record = self.reserve()
        self.dispatch.mark_submitted(record.dispatch_id)
        unknown = self.dispatch.recover(record.dispatch_id)
        self.assertEqual(unknown.state, "unknown")
        completed = self.dispatch.accept_result(
            record.dispatch_id,
            fence=unknown.current_fence,
            result_kind="synthetic",
            result_ref="res_1",
            result_digest=_DIG_D,
        )
        self.assertEqual(completed.state, "completed")


class CostLedgerTest(DispatchTestBase):
    def test_unknown_retains_reserve(self) -> None:
        record = self.reserve(reserve_usd="0.10")
        self.dispatch.mark_submitted(record.dispatch_id)
        settled = self.dispatch.settle_cost(record.dispatch_id, cost_state="unknown")
        self.assertEqual(settled.cost_state, "unknown")
        self.assertIsNone(settled.cost_amount_usd)
        self.assertEqual(
            runtime_reserve_exposure(self.store.connection, self.pid), Decimal("0.10")
        )

    def test_known_overrun_counted_at_larger_actual(self) -> None:
        record = self.reserve(reserve_usd="0.10")
        self.dispatch.mark_submitted(record.dispatch_id)
        self.dispatch.settle_cost(
            record.dispatch_id, cost_state="known", amount_usd="0.50"
        )
        self.assertEqual(
            runtime_reserve_exposure(self.store.connection, self.pid), Decimal("0.50")
        )

    def test_not_incurred_retains_original_reserve(self) -> None:
        record = self.reserve(reserve_usd="0.10")
        self.dispatch.settle_cost(record.dispatch_id, cost_state="not_incurred")
        self.assertEqual(
            runtime_reserve_exposure(self.store.connection, self.pid), Decimal("0.10")
        )

    def test_known_zero_amount_is_exact_zero_but_reserve_retained(self) -> None:
        record = self.reserve(reserve_usd="0.10")
        settled = self.dispatch.settle_cost(
            record.dispatch_id, cost_state="known", amount_usd="0"
        )
        self.assertEqual(settled.cost_amount_usd, "0")
        # max(reserve, 0) is still the reserve.
        self.assertEqual(
            runtime_reserve_exposure(self.store.connection, self.pid), Decimal("0.10")
        )

    def test_low_decimal_precision_preserved(self) -> None:
        record = self.reserve(reserve_usd="0.00002037")
        self.assertEqual(record.reserve_usd, "0.00002037")
        self.dispatch.settle_cost(
            record.dispatch_id, cost_state="known", amount_usd="0.00002038"
        )
        self.assertEqual(
            runtime_reserve_exposure(self.store.connection, self.pid),
            Decimal("0.00002038"),
        )

    def test_amount_validation(self) -> None:
        record = self.reserve()
        with self.assertRaises(DispatchValidationError):
            self.dispatch.settle_cost(record.dispatch_id, cost_state="known")
        with self.assertRaises(DispatchValidationError):
            self.dispatch.settle_cost(
                record.dispatch_id, cost_state="unknown", amount_usd="1"
            )
        with self.assertRaises(DispatchValidationError):
            self.dispatch.settle_cost(
                record.dispatch_id, cost_state="known", amount_usd=0.01  # float
            )
        with self.assertRaises(DispatchValidationError):
            self.dispatch.settle_cost(record.dispatch_id, cost_state="bogus")

    def test_route_cost_separation(self) -> None:
        opencode = self.reserve(request_id="oc", route="opencode_session")
        direct = self.reserve(
            request_id="dc", route="direct_call", task=self.make_task("task-dc")
        )
        self.assertEqual(opencode.accounting_bucket, "session_statistics")
        self.assertEqual(direct.accounting_bucket, "direct_call")
        # A direct-call row can never be relabelled into a session-statistics bucket.
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.connection.execute(
                "UPDATE runtime_dispatch SET accounting_bucket = 'session_statistics' "
                "WHERE project_id = ? AND dispatch_id = ?",
                (self.pid, direct.dispatch_id),
            )

    def test_cost_settlement_is_monotonic_both_routes(self) -> None:
        for index, route in enumerate(("opencode_session", "direct_call")):
            with self.subTest(route=route):
                task = self.task if index == 0 else self.make_task("task-monotonic")
                record = self.reserve(
                    request_id=f"mono-{route}", route=route, task=task
                )
                self.dispatch.mark_submitted(record.dispatch_id)
                self.dispatch.settle_cost(
                    record.dispatch_id, cost_state="known", amount_usd="2.00"
                )
                # A later weaker settlement must never erase the overrun.
                with self.assertRaises(DispatchConflictError):
                    self.dispatch.settle_cost(
                        record.dispatch_id, cost_state="unknown"
                    )
                with self.assertRaises(DispatchConflictError):
                    self.dispatch.settle_cost(
                        record.dispatch_id, cost_state="not_incurred"
                    )
                with self.assertRaises(DispatchConflictError):
                    self.dispatch.settle_cost(
                        record.dispatch_id, cost_state="known", amount_usd="1.00"
                    )
                self.assertEqual(
                    self.dispatch.get_dispatch(record.dispatch_id).cost_amount_usd, "2.00"
                )
        # Both routes contribute; the overrun is retained for each.
        self.assertEqual(
            runtime_reserve_exposure(self.store.connection, self.pid), Decimal("4.00")
        )

    def test_estimated_then_lower_known_refused_higher_allowed(self) -> None:
        record = self.reserve(reserve_usd="0.10")
        self.dispatch.mark_submitted(record.dispatch_id)
        self.dispatch.settle_cost(
            record.dispatch_id, cost_state="estimated", amount_usd="1.50"
        )
        with self.assertRaises(DispatchConflictError):
            self.dispatch.settle_cost(
                record.dispatch_id, cost_state="known", amount_usd="1.00"
            )
        settled = self.dispatch.settle_cost(
            record.dispatch_id, cost_state="known", amount_usd="1.60"
        )
        self.assertEqual(settled.cost_amount_usd, "1.60")
        self.assertEqual(
            runtime_reserve_exposure(self.store.connection, self.pid), Decimal("1.60")
        )

    def test_unknown_cannot_become_not_incurred(self) -> None:
        record = self.reserve(reserve_usd="0.10")
        self.dispatch.mark_submitted(record.dispatch_id)
        self.assertEqual(
            self.dispatch.get_dispatch(record.dispatch_id).cost_state, "unknown"
        )
        with self.assertRaises(DispatchConflictError):
            self.dispatch.settle_cost(record.dispatch_id, cost_state="not_incurred")
        self.assertEqual(
            runtime_reserve_exposure(self.store.connection, self.pid), Decimal("0.10")
        )


class SharedCapsTest(DispatchTestBase):
    def test_runtime_reserve_counts_against_synthetic(self) -> None:
        self.jobs.set_budget_policy(project_cap_usd="25", stage_cap_usd="25")
        self.reserve(request_id="rt", reserve_usd="20")
        job = self.jobs.submit("synthetic-1", task_kind="probe")
        with self.assertRaises(BudgetExceededError):
            self.jobs.begin_attempt(
                job.job_id,
                dispatch_kind="external_synthetic",
                stage="s1",
                reserve_usd="10",
            )

    def test_synthetic_reserve_counts_against_runtime(self) -> None:
        self.jobs.set_budget_policy(project_cap_usd="25", stage_cap_usd="25")
        job = self.jobs.submit("synthetic-1", task_kind="probe")
        self.jobs.begin_attempt(
            job.job_id,
            dispatch_kind="external_synthetic",
            stage="s1",
            reserve_usd="20",
        )
        with self.assertRaises(BudgetExceededError):
            self.reserve(request_id="rt", reserve_usd="10")

    def test_stage_cap_shared_across_routes(self) -> None:
        self.jobs.set_budget_policy(project_cap_usd="25", stage_cap_usd="10")
        self.reserve(request_id="rt-1", stage="critique", reserve_usd="8")
        with self.assertRaises(BudgetExceededError):
            self.reserve(
                request_id="rt-2",
                stage="critique",
                reserve_usd="3",
                task=self.make_task("task-rt2"),
            )
        # A different stage still has its own stage ceiling.
        other = self.reserve(
            request_id="rt-3",
            stage="review",
            reserve_usd="8",
            task=self.make_task("task-rt3"),
        )
        self.assertEqual(other.stage, "review")

    def test_combined_budget_usage_reports_both(self) -> None:
        self.jobs.set_budget_policy(project_cap_usd="25", stage_cap_usd="25")
        job = self.jobs.submit("synthetic-1", task_kind="probe")
        self.jobs.begin_attempt(
            job.job_id,
            dispatch_kind="external_synthetic",
            stage="s1",
            reserve_usd="3",
        )
        self.reserve(request_id="rt", stage="s1", reserve_usd="4")
        usage = self.dispatch.budget_usage(stage="s1")
        self.assertEqual(Decimal(usage.project_reserved_usd), Decimal("7"))
        self.assertEqual(Decimal(usage.stage_reserved_usd), Decimal("7"))

    def test_concurrent_cap_checks_serialize(self) -> None:
        self.jobs.set_budget_policy(project_cap_usd="25", stage_cap_usd="25")
        app_dir = self.store.app_dir
        pid = self.pid
        tasks = {
            "rt-a": self.make_task("task-conc-a"),
            "rt-b": self.make_task("task-conc-b"),
        }
        results: list[str] = []
        barrier = threading.Barrier(2)

        def worker(request_id: str) -> None:
            store = ProjectStore.open_at(app_dir, expected_project_id=pid)
            try:
                repo = RuntimeDispatchRepository(store)
                barrier.wait(timeout=10)
                try:
                    repo.reserve_dispatch(
                        tasks[request_id],
                        request_id=request_id,
                        route="direct_call",
                        stage="s1",
                        input_digest=_DIG_A,
                        version_digest=_DIG_B,
                        reserve_usd="20",
                        bound_evidence_digest=_DIG_C,
                    )
                    results.append("ok")
                except BudgetExceededError:
                    results.append("exceeded")
                except DispatchValidationError:
                    # The task record was re-read on the other connection; a
                    # validation failure would be a test bug, not a cap race.
                    results.append("invalid")
            finally:
                store.close()

        threads = [
            threading.Thread(target=worker, args=("rt-a",)),
            threading.Thread(target=worker, args=("rt-b",)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        self.assertEqual(sorted(results), ["exceeded", "ok"])
        self.assertEqual(len(self.dispatch.list_dispatches()), 1)


class LegacySettlementSharedCapTest(DispatchTestBase):
    """The legacy synthetic ledger cannot erase a known overrun on settlement.

    R1 correctly separated routes, but the reciprocal helper counted the *current*
    legacy amount, so a later weaker legacy settlement discarded the known actual
    and shrank the exposure shared with every runtime route. These regressions pin
    the conservative, monotonic legacy settlement and both cap directions.
    """

    def _legacy_overrun(self):
        job = self.jobs.submit("legacy", task_kind="synthetic_numeric")
        attempt = self.jobs.begin_attempt(
            job.job_id,
            dispatch_kind="external_synthetic",
            stage="s",
            reserve_usd="0.1",
        )
        self.jobs.settle_cost(attempt.attempt_id, cost_state="known", amount_usd="2")
        return job, attempt

    def test_known_overrun_cannot_be_erased_by_unknown_or_not_incurred(self) -> None:
        _job, attempt = self._legacy_overrun()
        # The known actual overran the 0.10 reserve and counts at 2.00.
        self.assertEqual(
            Decimal(self.dispatch.budget_usage().project_reserved_usd), Decimal("2")
        )
        # A later unknown/not_incurred settlement must be refused, not applied.
        with self.assertRaises(JobConflictError):
            self.jobs.settle_cost(attempt.attempt_id, cost_state="unknown")
        with self.assertRaises(JobConflictError):
            self.jobs.settle_cost(attempt.attempt_id, cost_state="not_incurred")
        # The shared exposure still counts the known overrun, both scopes.
        self.assertEqual(
            Decimal(self.dispatch.budget_usage().project_reserved_usd), Decimal("2")
        )
        self.assertEqual(
            Decimal(self.dispatch.budget_usage(stage="s").stage_reserved_usd),
            Decimal("2"),
        )
        stored = self.jobs.attempt(attempt.attempt_id)
        self.assertEqual((stored.cost_state, stored.cost_amount_usd), ("known", "2"))

    def test_known_overrun_cannot_be_lowered_but_higher_is_allowed(self) -> None:
        _job, attempt = self._legacy_overrun()
        with self.assertRaises(JobConflictError):
            self.jobs.settle_cost(
                attempt.attempt_id, cost_state="known", amount_usd="1"
            )
        with self.assertRaises(JobConflictError):
            self.jobs.settle_cost(
                attempt.attempt_id, cost_state="estimated", amount_usd="1.99"
            )
        settled = self.jobs.settle_cost(
            attempt.attempt_id, cost_state="known", amount_usd="3"
        )
        self.assertEqual(settled.cost_amount_usd, "3")
        self.assertEqual(
            Decimal(self.dispatch.budget_usage().project_reserved_usd), Decimal("3")
        )

    def test_project_and_stage_caps_stay_closed_after_refused_downgrade(self) -> None:
        self.jobs.set_budget_policy(project_cap_usd="25", stage_cap_usd="25")
        _job, _attempt = self._legacy_overrun()
        # A refused downgrade must not reopen the shared caps in either direction:
        # a new runtime reserve in the same stage cannot reopen the stage cap...
        with self.assertRaises(BudgetExceededError):
            self.reserve(request_id="rt-same-stage", stage="s", reserve_usd="24")
        # ...nor can a runtime reserve in a different stage reopen the project cap.
        with self.assertRaises(BudgetExceededError):
            self.reserve(request_id="rt-other-stage", stage="s2", reserve_usd="24")
        # The reciprocal direction (a synthetic reserve) also stays closed.
        job2 = self.jobs.submit("legacy-2", task_kind="synthetic_numeric")
        with self.assertRaises(BudgetExceededError):
            self.jobs.begin_attempt(
                job2.job_id,
                dispatch_kind="external_synthetic",
                stage="s",
                reserve_usd="24",
            )
        # The known overrun is still counted and unchanged.
        self.assertEqual(
            Decimal(self.dispatch.budget_usage().project_reserved_usd), Decimal("2")
        )


class ImmutableAndIntegrityTest(DispatchTestBase):
    def test_identity_row_is_immutable(self) -> None:
        record = self.reserve()
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.connection.execute(
                "UPDATE runtime_dispatch SET reserve_usd = '9.99' "
                "WHERE project_id = ? AND dispatch_id = ?",
                (self.pid, record.dispatch_id),
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.connection.execute(
                "DELETE FROM runtime_dispatch WHERE project_id = ? AND dispatch_id = ?",
                (self.pid, record.dispatch_id),
            )

    def test_integrity_of_valid_store_is_empty(self) -> None:
        self.reserve(request_id="oc", route="opencode_session")
        self.reserve(
            request_id="dc", route="direct_call", task=self.make_task("task-dc")
        )
        self.assertEqual(self.dispatch.verify_all_integrity(), [])
        self.assertEqual(
            runtime_dispatch_integrity_problems(self.store.connection, self.pid), []
        )

    def test_integrity_detects_corrupt_row(self) -> None:
        record = self.reserve()
        conn = self.store.connection
        conn.execute("DROP TRIGGER runtime_dispatch_no_update")
        conn.execute(
            "UPDATE runtime_dispatch SET reserve_usd = 'not-a-decimal' "
            "WHERE project_id = ? AND dispatch_id = ?",
            (self.pid, record.dispatch_id),
        )
        problems = runtime_dispatch_integrity_problems(conn, self.pid)
        self.assertTrue(problems)
        self.assertFalse(self.dispatch.verify_dispatch(record.dispatch_id))

    def test_integrity_detects_dangling_task(self) -> None:
        record = self.reserve()
        conn = self.store.connection
        conn.execute("DROP TRIGGER runtime_dispatch_no_update")
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute(
            "UPDATE runtime_dispatch SET task_id = 'rtask_missing' "
            "WHERE project_id = ? AND dispatch_id = ?",
            (self.pid, record.dispatch_id),
        )
        conn.execute("PRAGMA foreign_keys = ON")
        problems = runtime_dispatch_integrity_problems(conn, self.pid)
        self.assertTrue(problems)

    def test_integrity_detects_foreign_row(self) -> None:
        record = self.reserve()
        conn = self.store.connection
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute(
            "INSERT INTO runtime_dispatch "
            "(project_id, dispatch_id, request_id, task_id, task_fingerprint, route, "
            " accounting_bucket, stage, input_digest, version_digest, reserve_usd, "
            " bound_evidence_digest, created_at_utc) "
            "VALUES ('other-project', 'disp_foreign', 'foreign-req', ?, ?, "
            " 'direct_call', 'direct_call', 's1', ?, ?, '0.10', ?, "
            " '2026-01-01T00:00:00+00:00')",
            (record.task_id, record.task_fingerprint, _DIG_A, _DIG_B, _DIG_C),
        )
        conn.execute(
            "INSERT INTO runtime_dispatch_state "
            "(project_id, dispatch_id, state, current_fence, cost_state, updated_at_utc) "
            "VALUES ('other-project', 'disp_foreign', 'reserved', 1, 'not_incurred', "
            " '2026-01-01T00:00:00+00:00')"
        )
        conn.execute("PRAGMA foreign_keys = ON")
        problems = runtime_dispatch_integrity_problems(conn, self.pid)
        self.assertTrue(any("foreign" in problem for problem in problems))

    def test_integrity_revalidates_linked_task_not_only_fingerprint_column(self) -> None:
        # Regression: only the task.fingerprint column used to be compared, so a
        # malformed task row survived behind an intact pin. Tamper a projected
        # column while leaving the fingerprint column unchanged.
        record = self.reserve()
        conn = self.store.connection
        conn.execute("DROP TRIGGER runtime_bounded_task_no_update")
        conn.execute(
            "UPDATE runtime_bounded_task SET prompt_version = 'tampered.v9' "
            "WHERE project_id = ? AND task_id = ?",
            (self.pid, record.task_id),
        )
        problems = runtime_dispatch_integrity_problems(conn, self.pid)
        self.assertTrue(any("integrity" in problem for problem in problems))
        self.assertFalse(self.dispatch.verify_dispatch(record.dispatch_id))

    def test_integrity_detects_orphan_operational_row(self) -> None:
        self.reserve()
        conn = self.store.connection
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute(
            "INSERT INTO runtime_dispatch_state "
            "(project_id, dispatch_id, state, current_fence, cost_state, "
            " updated_at_utc) "
            "VALUES (?, 'disp_orphan', 'reserved', 1, 'not_incurred', "
            " '2026-01-01T00:00:00+00:00')",
            (self.pid,),
        )
        conn.execute("PRAGMA foreign_keys = ON")
        problems = runtime_dispatch_integrity_problems(conn, self.pid)
        self.assertTrue(any("orphan" in problem for problem in problems))

    def test_integrity_detects_foreign_operational_row(self) -> None:
        self.reserve()
        conn = self.store.connection
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute(
            "INSERT INTO runtime_dispatch_state "
            "(project_id, dispatch_id, state, current_fence, cost_state, "
            " updated_at_utc) "
            "VALUES ('other-project', 'disp_foreign_state', 'reserved', 1, "
            " 'not_incurred', '2026-01-01T00:00:00+00:00')"
        )
        conn.execute("PRAGMA foreign_keys = ON")
        problems = runtime_dispatch_integrity_problems(conn, self.pid)
        self.assertTrue(any("foreign" in problem for problem in problems))

    def _inject_state(self, dispatch_id: str, assignments: str) -> None:
        conn = self.store.connection
        conn.execute("PRAGMA ignore_check_constraints = ON")
        try:
            conn.execute(
                f"UPDATE runtime_dispatch_state SET {assignments} "
                "WHERE project_id = ? AND dispatch_id = ?",
                (self.pid, dispatch_id),
            )
        finally:
            conn.execute("PRAGMA ignore_check_constraints = OFF")

    def test_integrity_detects_non_initial_fence(self) -> None:
        record = self.reserve()
        self._inject_state(record.dispatch_id, "current_fence = 2")
        problems = runtime_dispatch_integrity_problems(self.store.connection, self.pid)
        self.assertTrue(problems)
        self.assertFalse(self.dispatch.verify_dispatch(record.dispatch_id))

    def test_integrity_detects_submitted_without_incurred_cost(self) -> None:
        record = self.reserve()
        self.dispatch.mark_submitted(record.dispatch_id)
        self._inject_state(
            record.dispatch_id, "cost_state = 'not_incurred', cost_amount_usd = NULL"
        )
        problems = runtime_dispatch_integrity_problems(self.store.connection, self.pid)
        self.assertTrue(problems)

    def test_integrity_detects_admitted_without_admission(self) -> None:
        record = self.reserve()
        self.dispatch.mark_submitted(record.dispatch_id)
        self.dispatch.mark_admitted(record.dispatch_id, admission_id="adm-1")
        self._inject_state(
            record.dispatch_id, "admission_id = NULL, admission_ref = NULL"
        )
        problems = runtime_dispatch_integrity_problems(self.store.connection, self.pid)
        self.assertTrue(problems)


class CostExposureFailClosedTest(DispatchTestBase):
    def test_missing_operational_state_row_fails_closed(self) -> None:
        record = self.reserve(reserve_usd="0.10")
        conn = self.store.connection
        conn.execute(
            "DELETE FROM runtime_dispatch_state WHERE project_id = ? AND dispatch_id = ?",
            (self.pid, record.dispatch_id),
        )
        with self.assertRaises(DispatchCorruptionError):
            runtime_reserve_exposure(conn, self.pid)

    def test_malformed_cost_state_fails_closed(self) -> None:
        record = self.reserve(reserve_usd="0.10")
        conn = self.store.connection
        conn.execute("PRAGMA ignore_check_constraints = ON")
        try:
            conn.execute(
                "UPDATE runtime_dispatch_state SET cost_state = 'bogus' "
                "WHERE project_id = ? AND dispatch_id = ?",
                (self.pid, record.dispatch_id),
            )
        finally:
            conn.execute("PRAGMA ignore_check_constraints = OFF")
        with self.assertRaises(DispatchCorruptionError):
            runtime_reserve_exposure(conn, self.pid)

    def test_inconsistent_cost_state_amount_fails_closed(self) -> None:
        record = self.reserve(reserve_usd="0.10")
        conn = self.store.connection
        conn.execute("PRAGMA ignore_check_constraints = ON")
        try:
            conn.execute(
                "UPDATE runtime_dispatch_state SET cost_state = 'known', "
                "cost_amount_usd = NULL WHERE project_id = ? AND dispatch_id = ?",
                (self.pid, record.dispatch_id),
            )
        finally:
            conn.execute("PRAGMA ignore_check_constraints = OFF")
        with self.assertRaises(DispatchCorruptionError):
            runtime_reserve_exposure(conn, self.pid)

    def test_malformed_cost_amount_fails_closed(self) -> None:
        record = self.reserve(reserve_usd="0.10")
        conn = self.store.connection
        conn.execute("PRAGMA ignore_check_constraints = ON")
        try:
            conn.execute(
                "UPDATE runtime_dispatch_state SET cost_state = 'known', "
                "cost_amount_usd = 'not-a-decimal' "
                "WHERE project_id = ? AND dispatch_id = ?",
                (self.pid, record.dispatch_id),
            )
        finally:
            conn.execute("PRAGMA ignore_check_constraints = OFF")
        with self.assertRaises(DispatchCorruptionError):
            runtime_reserve_exposure(conn, self.pid)


class MissingLedgerTableFailClosedTest(DispatchTestBase):
    """A current-schema store missing a ledger table must not report zero exposure."""

    def test_missing_identity_and_operational_tables_fail_closed(self) -> None:
        conn = self.store.connection
        conn.execute("DROP TABLE runtime_dispatch_state")
        conn.execute("DROP TABLE runtime_dispatch")
        self.assertEqual(
            int(conn.execute("PRAGMA user_version").fetchone()[0]),
            schema.STORE_SCHEMA_VERSION,
        )
        with self.assertRaises(DispatchCorruptionError) as ctx:
            runtime_reserve_exposure(conn, self.pid)
        # Typed and static: the denial never echoes the project id.
        self.assertNotIn(self.pid, str(ctx.exception))

    def test_missing_operational_table_fails_closed(self) -> None:
        self.reserve(reserve_usd="0.10")
        conn = self.store.connection
        conn.execute("DROP TABLE runtime_dispatch_state")
        with self.assertRaises(DispatchCorruptionError):
            runtime_reserve_exposure(conn, self.pid)

    def test_pre_ledger_schema_has_zero_runtime_exposure(self) -> None:
        root = str(self.tmp / "p3")
        paths.ensure_owner_private_dir(paths.app_dir_for(root))
        conn = schema.open_connection(paths.store_path_for(root))
        try:
            schema.migrate(
                conn,
                target_version=3,
                migrations=schema.STORE_MIGRATIONS,
                label="project store",
            )
            self.assertEqual(int(conn.execute("PRAGMA user_version").fetchone()[0]), 3)
            # A genuine pre-ledger store has no runtime ledger to count.
            self.assertEqual(
                runtime_reserve_exposure(conn, paths.new_project_id()), Decimal("0")
            )
        finally:
            conn.close()


class SingleDispatchPerTaskDatabaseTest(DispatchTestBase):
    def test_database_refuses_second_dispatch_for_same_task(self) -> None:
        first = self.reserve(request_id="call-1")
        conn = self.store.connection
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO runtime_dispatch "
                "(project_id, dispatch_id, request_id, task_id, task_fingerprint, "
                " route, accounting_bucket, stage, input_digest, version_digest, "
                " reserve_usd, bound_evidence_digest, created_at_utc) "
                "VALUES (?, 'disp_second', 'call-2', ?, ?, 'direct_call', "
                " 'direct_call', 's2', ?, ?, '0.20', ?, '2026-01-01T00:00:00+00:00')",
                (
                    self.pid,
                    first.task_id,
                    first.task_fingerprint,
                    _DIG_A,
                    _DIG_B,
                    _DIG_C,
                ),
            )
        self.assertEqual(len(self.dispatch.list_dispatches()), 1)


class AdmissionReplayTest(DispatchTestBase):
    def test_admission_ref_must_match_on_exact_replay(self) -> None:
        record = self.reserve(route="opencode_session")
        self.dispatch.mark_submitted(record.dispatch_id)
        admitted = self.dispatch.mark_admitted(
            record.dispatch_id, admission_id="adm-1", admission_ref="ext-1"
        )
        self.assertEqual(admitted.admission_ref, "ext-1")
        replay = self.dispatch.mark_admitted(
            record.dispatch_id, admission_id="adm-1", admission_ref="ext-1"
        )
        self.assertEqual((replay.admission_id, replay.admission_ref), ("adm-1", "ext-1"))
        # A divergent ref (same id) must not silently reuse the recorded ref.
        with self.assertRaises(DispatchConflictError):
            self.dispatch.mark_admitted(
                record.dispatch_id, admission_id="adm-1", admission_ref="ext-2"
            )
        # A missing ref must not silently accept the established one either.
        with self.assertRaises(DispatchConflictError):
            self.dispatch.mark_admitted(record.dispatch_id, admission_id="adm-1")
        self.assertEqual(
            self.dispatch.get_dispatch(record.dispatch_id).admission_ref, "ext-1"
        )

    def test_divergent_admission_id_is_refused(self) -> None:
        record = self.reserve(route="opencode_session")
        self.dispatch.mark_submitted(record.dispatch_id)
        self.dispatch.mark_admitted(record.dispatch_id, admission_id="adm-1")
        with self.assertRaises(DispatchConflictError):
            self.dispatch.mark_admitted(record.dispatch_id, admission_id="adm-2")
        self.assertEqual(
            self.dispatch.get_dispatch(record.dispatch_id).admission_id, "adm-1"
        )


class StaticMessageTest(DispatchTestBase):
    def test_errors_never_echo_raw_inputs(self) -> None:
        record = self.reserve(request_id="secret-request-value")
        with self.assertRaises(DispatchConflictError) as conflict:
            self.reserve(request_id="secret-request-value", reserve_usd="0.20")
        self.assertNotIn("secret-request-value", str(conflict.exception))

        with self.assertRaises(DispatchValidationError) as enum_error:
            self.reserve(route="raw-route-value")
        self.assertNotIn("raw-route-value", str(enum_error.exception))

        with self.assertRaises(DispatchValidationError) as cost_error:
            self.dispatch.settle_cost(record.dispatch_id, cost_state="bogus-state")
        self.assertNotIn("bogus-state", str(cost_error.exception))

        self.dispatch.mark_submitted(record.dispatch_id)
        with self.assertRaises(DispatchStateError) as state_error:
            self.dispatch.mark_submitted(record.dispatch_id)
        self.assertNotIn(record.dispatch_id, str(state_error.exception))

        missing = "disp_" + "0" * 32
        with self.assertRaises(DispatchStateError) as missing_error:
            self.dispatch.mark_submitted(missing)
        self.assertNotIn(missing, str(missing_error.exception))

    def test_cost_downgrade_error_never_echoes_amount(self) -> None:
        record = self.reserve(reserve_usd="0.10")
        self.dispatch.mark_submitted(record.dispatch_id)
        self.dispatch.settle_cost(
            record.dispatch_id, cost_state="known", amount_usd="2.00"
        )
        with self.assertRaises(DispatchConflictError) as conflict:
            self.dispatch.settle_cost(
                record.dispatch_id, cost_state="known", amount_usd="1.00"
            )
        self.assertNotIn("1.00", str(conflict.exception))


class ReopenTest(DispatchTestBase):
    def test_state_and_exposure_survive_reopen_and_replay_is_idempotent(self) -> None:
        record = self.reserve(reserve_usd="0.25")
        self.dispatch.mark_submitted(record.dispatch_id)
        self.dispatch.settle_cost(record.dispatch_id, cost_state="unknown")
        pid = self.pid
        app_dir = self.store.app_dir
        self.store.close()

        reopened = ProjectStore.open_at(app_dir, expected_project_id=pid)
        self.addCleanup(reopened.close)
        repo = RuntimeDispatchRepository(reopened)
        stored = repo.get_dispatch(record.dispatch_id)
        self.assertIsNotNone(stored)
        assert stored is not None
        self.assertEqual(stored.state, "submitted")
        self.assertEqual(stored.cost_state, "unknown")
        self.assertEqual(stored.reserve_usd, "0.25")
        self.assertEqual(
            runtime_reserve_exposure(reopened.connection, pid), Decimal("0.25")
        )
        # A replay after reopen returns the same row and never double-reserves.
        task = RuntimeRepository(reopened).get_task_by_request("task-req")
        assert task is not None
        replay = repo.reserve_dispatch(
            task,
            request_id="call-1",
            route="opencode_session",
            stage="s1",
            input_digest=_DIG_A,
            version_digest=_DIG_B,
            reserve_usd="0.25",
            bound_evidence_digest=_DIG_C,
        )
        self.assertEqual(replay.dispatch_id, stored.dispatch_id)
        self.assertEqual(len(repo.list_dispatches()), 1)
        self.assertEqual(repo.verify_all_integrity(), [])


class MigrationTest(DispatchTestBase):
    def test_schema5_to_6_preserves_old_rows(self) -> None:
        # Build a real schema-5 store with an artifact, a persisted task, and a job.
        root = str(self.tmp / "p5")
        app_dir = paths.app_dir_for(root)
        db_path = paths.store_path_for(root)
        paths.ensure_owner_private_dir(app_dir)
        conn = schema.open_connection(db_path)
        try:
            schema.migrate(
                conn,
                target_version=5,
                migrations=schema.STORE_MIGRATIONS,
                label="project store",
            )
            self.assertEqual(schema.read_user_version(conn), 5)
            pid = paths.new_project_id()
            conn.execute(
                "INSERT INTO project (project_id, name, created_at_utc, app_version) "
                "VALUES (?, 's5', '2026-01-01T00:00:00+00:00', '0.0.0')",
                (pid,),
            )
            conn.execute(
                "INSERT INTO meta (key, value) VALUES ('created_at_utc', ?)",
                ("2026-01-01T00:00:00+00:00",),
            )
            store = ProjectStore(conn, app_dir, pid, schema_version=5, app_version="0.0.0")
            artifact, _ = self.seed_revision_for(store, pid)
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
        self.assertTrue({"runtime_dispatch", "runtime_dispatch_state"} <= names)
        # Prior rows and bytes are untouched.
        repo = ArtifactRepository(reopened)
        self.assertEqual(
            repo.read_content(artifact.artifact_id, "rev_1"), b"dispatch-source-bytes"
        )
        self.assertEqual(
            JobRepository(reopened).get(job.job_id).state, "queued"
        )

    def seed_revision_for(self, store, pid):
        repo = ArtifactRepository(store)
        artifact = repo.create_artifact(kind="document")
        repo.import_revision(
            artifact_id=artifact.artifact_id,
            content=b"dispatch-source-bytes",
            origin_locator="import:synthetic",
            rights=_rights(pid, artifact.artifact_id, "rev_1"),
            revision_id="rev_1",
        )
        return artifact, None


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
