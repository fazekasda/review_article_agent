"""Round-trip tests for **atomic committed** decision state across backup/restore.

The observed-only round-trip (``record_result``) is already covered by
``tests/test_decision_result_backup.py``. This file covers the *committed* state the
integrated local bridge writes (``DecisionResultRepository.commit_result``) and
proves a private snapshot/restore preserves it **exactly** without itself executing,
accepting, retrying, or promoting anything:

* a committed HTTP 200 result (known **and** unknown cost) keeps its accepted
  ``result_kind``/``result_ref``/``result_digest`` triple, its ``completed`` state,
  its settled cost, and its retained reserve bound across a round-trip;
* a committed definitive pre-billing rejection keeps ``failed``/``not_incurred``
  with no accepted result — never a scientific negative;
* a committed ambiguous outcome keeps ``unknown`` (with a known/estimated reported
  amount preserved, or genuinely unknown with **no** amount, never ``0``), retains
  its reserve, and is never retried or resubmitted — before or after a restore;
* an inert ``record_result`` row restores as an inert ``submitted`` observation and
  restore **never** promotes it; only an explicit post-restore ``commit_result``
  applies the transition (reusing the stored rows, no duplicate);
* a completed replay after restore is a byte-exact no-op (no timestamp change) and a
  stale fence / divergent replay is refused with zero mutation;
* ``verify_backup`` is read-only and never changes the snapshot, the manifest, or
  the live store, and the public manifest never carries the synthetic provider key.

Everything runs over a real temporary project store with the injected Jev
transports from ``tests.test_decision_commit`` (no network, no credential, no paid
call). The authenticated fixture base is reused, not duplicated.

Run::

    PYTHONPATH=src python3 -m unittest tests.test_decision_commit_backup
"""

from __future__ import annotations

import hashlib
import json
import sys
import unittest
from decimal import Decimal
from pathlib import Path
from _tiers import slow

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from review_article_agent.runtime.dispatch import (  # noqa: E402
    DispatchStateError,
    RuntimeDispatchRepository,
    runtime_reserve_exposure,
)
from review_article_agent.storage import (  # noqa: E402
    ProjectStore,
    Registry,
    paths,
    schema,
)
from review_article_agent.storage.backup import (  # noqa: E402
    MANIFEST_FILENAME,
    SNAPSHOT_FILENAME,
    create_backup,
    restore_backup,
    verify_backup,
)
from review_article_agent.storage.decision_results import (  # noqa: E402
    RESULT_KIND,
    DecisionResultConflictError,
    DecisionResultRepository,
    DecisionResultStaleFenceError,
    decision_result_integrity_problems,
)
from tests.test_decision_commit import (  # noqa: E402
    API_KEY,
    _SPECS,
    CommitTestBase,
    _CannedTransport,
    _wire_body,
)


def _fingerprint(store, dispatch_id: str) -> dict:
    """Byte-exact, repository-independent snapshot of one committed dispatch.

    Includes the immutable set/question ``canonical`` BLOBs (the private
    observation), the raw ``runtime_dispatch``/``runtime_dispatch_state`` rows
    (so an accidental timestamp rewrite is visible), the dispatch's public
    projection, and the conservative reserve exposure (budget usage).
    """
    conn = store.connection
    pid = store.project_id
    set_row = conn.execute(
        "SELECT canonical, content_digest, status, cost_state, cost_amount_usd, "
        "question_count, result_id FROM decision_result_set "
        "WHERE project_id = ? AND dispatch_id = ?",
        (pid, dispatch_id),
    ).fetchone()
    questions = conn.execute(
        "SELECT ordinal, question_id, outcome, canonical, content_digest "
        "FROM decision_result_question WHERE project_id = ? AND dispatch_id = ? "
        "ORDER BY ordinal",
        (pid, dispatch_id),
    ).fetchall()
    record = RuntimeDispatchRepository(store).get_dispatch(dispatch_id)
    raw_dispatch = conn.execute(
        "SELECT * FROM runtime_dispatch WHERE project_id = ? AND dispatch_id = ?",
        (pid, dispatch_id),
    ).fetchone()
    raw_state = conn.execute(
        "SELECT * FROM runtime_dispatch_state WHERE project_id = ? AND dispatch_id = ?",
        (pid, dispatch_id),
    ).fetchone()
    set_count = conn.execute("SELECT COUNT(*) FROM decision_result_set").fetchone()[0]
    rows_count = conn.execute(
        "SELECT COUNT(*) FROM decision_result_question"
    ).fetchone()[0]
    return {
        "project_id": pid,
        "set": None
        if set_row is None
        else (
            bytes(set_row["canonical"]),
            set_row["content_digest"],
            set_row["status"],
            set_row["cost_state"],
            set_row["cost_amount_usd"],
            set_row["question_count"],
            set_row["result_id"],
        ),
        "questions": tuple(
            (
                row["ordinal"],
                row["question_id"],
                row["outcome"],
                bytes(row["canonical"]),
                row["content_digest"],
            )
            for row in questions
        ),
        "dispatch": (
            record.state,
            record.result_kind,
            record.result_ref,
            record.result_digest,
            record.current_fence,
            record.cost_state,
            record.cost_amount_usd,
        ),
        "raw_dispatch": None if raw_dispatch is None else tuple(raw_dispatch),
        "raw_state": None if raw_state is None else tuple(raw_state),
        "reserve": runtime_reserve_exposure(conn, pid),
        "counts": (set_count, rows_count),
    }


class CommittedStateBackupTest(CommitTestBase):
    """The authenticated commit fixture plus sensibly reused backup helpers."""

    def backup(self, name: str = "backup") -> Path:
        dest = self.tmp / name
        create_backup(self.store, dest)
        return dest

    def verify(self, dest: Path) -> dict:
        report = verify_backup(dest)
        self.assertTrue(report["ok"])
        self.assertEqual(report["store_schema_version"], schema.STORE_SCHEMA_VERSION)
        self.assertEqual(report["project_id"], self.pid)
        return report

    def restore(self, dest: Path, name: str = "restored") -> ProjectStore:
        registry = Registry(self.tmp / "state")
        target = self.tmp / name
        target.mkdir()
        info = restore_backup(dest, target, registry)
        restored = ProjectStore.open_at(
            paths.app_dir_for(str(target)), expected_project_id=info.project_id
        )
        self.addCleanup(restored.close)
        return restored

    def roundtrip(self) -> tuple[Path, ProjectStore]:
        dest = self.backup()
        self.verify(dest)
        return dest, self.restore(dest)

    def fingerprint(self, store: ProjectStore | None = None) -> dict:
        return _fingerprint(
            store or self.store, self.dispatch_record.dispatch_id
        )

    def _restored_dispatch(self, restored: ProjectStore):
        return RuntimeDispatchRepository(restored).get_dispatch(
            self.dispatch_record.dispatch_id
        )

    def assert_roundtrips_exactly(self) -> ProjectStore:
        """Backup, verify, restore, and assert the committed rows are unchanged."""
        before = self.fingerprint()
        dest, restored = self.roundtrip()
        after = self.fingerprint(restored)
        self.assertEqual(after, before, "committed state changed across restore")
        self.assertEqual(restored.project_id, self.pid)
        self.assertEqual(
            decision_result_integrity_problems(restored.connection, self.pid), []
        )
        self.assertFalse(restored.connection.in_transaction)
        return restored

    def no_retry_or_resubmit(self, restored: ProjectStore, before: dict) -> None:
        """A restored unresolved dispatch is never retried and mints no identity."""
        dispatch = RuntimeDispatchRepository(restored)
        with self.assertRaises(DispatchStateError):
            dispatch.mark_submitted(self.dispatch_record.dispatch_id)
        self.assertEqual(len(dispatch.list_dispatches()), 1)
        self.assertEqual(self.fingerprint(restored), before)


@slow
class CompletedKnownCostRoundTripTest(CommittedStateBackupTest):
    def test_completed_accepted_triple_state_cost_and_budget_survive(self) -> None:
        self.submit()
        committed = self.commit(self.valid_result())
        self.assertEqual(committed.outcome, "completed")
        record = committed.dispatch
        self.assertEqual(record.state, "completed")
        self.assertEqual(record.result_kind, RESULT_KIND)
        self.assertEqual(record.cost_state, "known")
        self.assertEqual(record.cost_amount_usd, "0.00002037")
        triple = (RESULT_KIND, committed.observation.result_id, committed.observation.digest)
        self.assertEqual(
            (record.result_kind, record.result_ref, record.result_digest), triple
        )

        before = self.fingerprint()
        self.assertEqual(before["counts"], (1, len(_SPECS)))
        self.assertEqual(before["reserve"], Decimal("0.25"))
        self.assertEqual(before["set"][2], "completed")
        self.assertEqual(before["set"][3], "known")
        self.assertEqual(before["set"][4], "0.00002037")
        self.assertEqual(before["set"][5], len(_SPECS))
        self.assertEqual(before["set"][6], committed.observation.result_id)

        restored = self.assert_roundtrips_exactly()

        # The original immutable set and every question canonical survive exactly.
        self.assertEqual(self.fingerprint(restored)["set"], before["set"])
        self.assertEqual(self.fingerprint(restored)["questions"], before["questions"])

        # The exact accepted triple, completed state, settled cost, and retained
        # reserve bound all survive.
        reloaded = self._restored_dispatch(restored)
        self.assertEqual(
            (reloaded.result_kind, reloaded.result_ref, reloaded.result_digest), triple
        )
        self.assertEqual(reloaded.state, "completed")
        self.assertEqual(reloaded.cost_state, "known")
        self.assertEqual(reloaded.cost_amount_usd, "0.00002037")
        self.assertEqual(
            runtime_reserve_exposure(restored.connection, self.pid), Decimal("0.25")
        )


@slow
class CompletedUnknownCostRoundTripTest(CommittedStateBackupTest):
    def test_completed_with_unknown_cost_survives_and_keeps_reserve(self) -> None:
        self.submit()
        body = json.dumps(
            {
                "id": "gen-dec-1",
                "model": "typesafe/jev-1.14-20261014",
                "provider": "TypeSafe",
                "answers": {
                    "is_original_report": {"type": "noul", "noul": 0.97},
                },
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

        before = self.fingerprint()
        self.assertEqual(before["set"][3], "unknown")
        self.assertIsNone(before["set"][4])
        self.assertEqual(before["reserve"], Decimal("0.25"))

        restored = self.assert_roundtrips_exactly()

        reloaded = self._restored_dispatch(restored)
        self.assertEqual(reloaded.state, "completed")
        self.assertEqual(reloaded.result_kind, RESULT_KIND)
        self.assertEqual(reloaded.cost_state, "unknown")
        self.assertIsNone(reloaded.cost_amount_usd)
        # A completed operation with an unknown cost is never recorded as ``0``.
        self.assertEqual(
            runtime_reserve_exposure(restored.connection, self.pid), Decimal("0.25")
        )


@slow
class DefinitiveFailureRoundTripTest(CommittedStateBackupTest):
    def test_failed_not_incurred_survives_and_is_never_an_accepted_result(self) -> None:
        self.submit()
        result = self.definitive_result(403)
        self.assertEqual(result.cost.state, "not_incurred")
        committed = self.commit(result)
        self.assertEqual(committed.outcome, "failed")
        record = committed.dispatch
        self.assertEqual(record.state, "failed")
        self.assertEqual(record.cost_state, "not_incurred")
        self.assertIsNone(record.result_kind)
        self.assertIsNone(record.result_ref)
        self.assertIsNone(record.result_digest)
        self.assertIsNone(record.cost_amount_usd)

        before = self.fingerprint()
        self.assertEqual(before["dispatch"][0], "failed")
        self.assertEqual(before["dispatch"][5], "not_incurred")
        self.assertEqual(before["reserve"], Decimal("0.25"))

        restored = self.assert_roundtrips_exactly()

        reloaded = self._restored_dispatch(restored)
        self.assertEqual(reloaded.state, "failed")
        self.assertEqual(reloaded.cost_state, "not_incurred")
        self.assertIsNone(reloaded.result_kind)
        self.assertIsNone(reloaded.result_ref)
        self.assertIsNone(reloaded.result_digest)

        # A definitive failure is never a scientific negative / accepted result.
        for row in DecisionResultRepository(restored).list_question_records(
            self.dispatch_record.dispatch_id
        ):
            self.assertFalse(row.record["is_exclusion"])
            self.assertTrue(row.record["retains_candidate"])

        # Replaying the same definitive rejection after restore stays failed.
        replay = DecisionResultRepository(restored).commit_result(
            self.dispatch_record.dispatch_id, result, fence=1
        )
        self.assertTrue(replay.replayed)
        self.assertEqual(replay.outcome, "failed")
        self.assertEqual(replay.dispatch.state, "failed")
        self.assertEqual(self.fingerprint(restored), before)


@slow
class AmbiguousOutcomeRoundTripTest(CommittedStateBackupTest):
    def test_ambiguous_known_amount_survives_unknown_and_is_never_resubmitted(
        self,
    ) -> None:
        self.submit()
        result = self.ambiguous_known_cost_result(status=429, cost=0.001)
        committed = self.commit(result)
        self.assertEqual(committed.outcome, "unknown")
        record = committed.dispatch
        self.assertEqual(record.state, "unknown")
        self.assertEqual(record.cost_state, "known")
        self.assertEqual(record.cost_amount_usd, "0.001")
        self.assertIsNone(record.result_kind)
        self.assertIsNone(record.result_digest)

        before = self.fingerprint()
        self.assertEqual(before["dispatch"][5], "known")
        self.assertEqual(before["dispatch"][6], "0.001")
        self.assertEqual(before["reserve"], Decimal("0.25"))

        restored = self.assert_roundtrips_exactly()

        reloaded = self._restored_dispatch(restored)
        self.assertEqual(reloaded.state, "unknown")
        self.assertEqual(reloaded.cost_state, "known")
        self.assertEqual(reloaded.cost_amount_usd, "0.001")
        self.assertIsNone(reloaded.result_kind)

        # The known reported amount is bookkeeping: the operation stays unresolved,
        # the reserve is retained, and no retry/resubmit/new identity is possible.
        self.assertEqual(
            runtime_reserve_exposure(restored.connection, self.pid), Decimal("0.25")
        )
        self.no_retry_or_resubmit(restored, before)

        replay = DecisionResultRepository(restored).commit_result(
            self.dispatch_record.dispatch_id, result, fence=1
        )
        self.assertTrue(replay.replayed)
        self.assertEqual(replay.outcome, "unknown")
        self.assertEqual(replay.dispatch.state, "unknown")
        self.assertEqual(self.fingerprint(restored), before)

    def test_timeout_unknown_with_no_amount_survives_and_is_never_resubmitted(
        self,
    ) -> None:
        self.submit()
        result = self.transport_error_result()
        committed = self.commit(result)
        self.assertEqual(committed.outcome, "unknown")
        record = committed.dispatch
        self.assertEqual(record.state, "unknown")
        self.assertEqual(record.cost_state, "unknown")
        self.assertIsNone(record.cost_amount_usd)
        self.assertIsNone(record.result_digest)
        self.assertEqual(committed.observation.status, "transport_error")

        before = self.fingerprint()
        self.assertEqual(before["dispatch"][5], "unknown")
        self.assertIsNone(before["dispatch"][6])
        self.assertEqual(before["reserve"], Decimal("0.25"))

        restored = self.assert_roundtrips_exactly()

        reloaded = self._restored_dispatch(restored)
        self.assertEqual(reloaded.state, "unknown")
        self.assertEqual(reloaded.cost_state, "unknown")
        self.assertIsNone(reloaded.cost_amount_usd)
        # A genuinely unknown cost stays unknown with no amount, never ``0``.
        self.assertEqual(
            runtime_reserve_exposure(restored.connection, self.pid), Decimal("0.25")
        )
        self.no_retry_or_resubmit(restored, before)

        replay = DecisionResultRepository(restored).commit_result(
            self.dispatch_record.dispatch_id, result, fence=1
        )
        self.assertTrue(replay.replayed)
        self.assertEqual(replay.outcome, "unknown")
        self.assertEqual(self.fingerprint(restored), before)


@slow
class CompletedReplayAfterRestoreTest(CommittedStateBackupTest):
    def test_replay_is_a_byte_exact_noop_and_refusals_write_nothing(self) -> None:
        self.submit()
        result = self.valid_result()
        self.commit(result)

        before = self.fingerprint()
        restored = self.assert_roundtrips_exactly()
        results = DecisionResultRepository(restored)

        # A fully applied completed replay after restore is a byte-exact no-op: the
        # accepted triple, the set/question canonicals, and every raw timestamp row
        # are unchanged (``raw_state`` carries ``updated_at_utc``).
        replay = results.commit_result(
            self.dispatch_record.dispatch_id, result, fence=1
        )
        self.assertTrue(replay.replayed)
        self.assertEqual(self.fingerprint(restored), before)
        self.assertEqual(
            replay.dispatch.updated_at_utc,
            self._restored_dispatch(restored).updated_at_utc,
        )

        # A stale fence is refused with zero mutation.
        with self.assertRaises(DecisionResultStaleFenceError):
            results.commit_result(self.dispatch_record.dispatch_id, result, fence=2)
        self.assertEqual(self.fingerprint(restored), before)

        # A divergent replay is refused with zero mutation.
        divergent = self.dispatch_result(
            _CannedTransport(
                200,
                _wire_body({"is_original_report": {"type": "noul", "noul": 0.01}}),
            )
        )
        self.assertEqual(divergent.status, "completed")
        with self.assertRaises(DecisionResultConflictError):
            results.commit_result(
                self.dispatch_record.dispatch_id, divergent, fence=1
            )
        self.assertEqual(self.fingerprint(restored), before)
        self.assertFalse(restored.connection.in_transaction)


@slow
class InertObservationRestoreTest(CommittedStateBackupTest):
    def test_inert_observation_restores_submitted_and_is_never_promoted(self) -> None:
        self.submit()
        result = self.valid_result()
        observation = self.results.record_result(
            self.dispatch_record.dispatch_id, result, fence=1
        )
        self.assertEqual(observation.status, "completed")

        # The inert observation alone leaves the dispatch pending: submitted, cost
        # unknown, and no accepted-result triple.
        pending = self.reload_dispatch()
        self.assertEqual(pending.state, "submitted")
        self.assertEqual(pending.cost_state, "unknown")
        self.assertIsNone(pending.result_kind)
        self.assertIsNone(pending.result_digest)
        before = self.fingerprint()
        self.assertEqual(before["counts"], (1, len(_SPECS)))
        self.assertEqual(before["dispatch"][0], "submitted")
        self.assertIsNone(before["dispatch"][1])

        restored = self.assert_roundtrips_exactly()

        # Restore preserves the inert submitted observation exactly and never
        # promotes it: no completion claim, no accepted triple, no cost settlement.
        after = self.fingerprint(restored)
        self.assertEqual(after, before)
        self.assertEqual(after["counts"], (1, len(_SPECS)))
        self.assertEqual(after["dispatch"][0], "submitted")
        self.assertIsNone(after["dispatch"][1])
        reloaded = self._restored_dispatch(restored)
        self.assertEqual(reloaded.state, "submitted")
        self.assertIsNone(reloaded.result_kind)
        self.assertEqual(reloaded.cost_state, "unknown")

        # Only an explicit commit on the restored store applies the transition, and
        # it reuses the stored observation rows instead of duplicating them.
        committed = DecisionResultRepository(restored).commit_result(
            self.dispatch_record.dispatch_id, result, fence=1
        )
        self.assertEqual(committed.outcome, "completed")
        self.assertTrue(committed.observation_reused)
        self.assertFalse(committed.replayed)
        self.assertEqual(self.fingerprint(restored)["counts"], (1, len(_SPECS)))
        applied = self._restored_dispatch(restored)
        self.assertEqual(applied.state, "completed")
        self.assertEqual(applied.result_kind, RESULT_KIND)


@slow
class ReadonlyVerifyTest(CommittedStateBackupTest):
    def test_verify_backup_never_changes_snapshot_manifest_or_live_store(self) -> None:
        self.submit()
        self.commit(self.valid_result())
        dest = self.backup()
        snapshot = dest / SNAPSHOT_FILENAME
        manifest = dest / MANIFEST_FILENAME

        live_before = self.fingerprint()
        snapshot_before = hashlib.sha256(snapshot.read_bytes()).hexdigest()
        manifest_before = hashlib.sha256(manifest.read_bytes()).hexdigest()
        listing_before = tuple(sorted(entry.name for entry in dest.iterdir()))
        self.assertEqual(listing_before, (MANIFEST_FILENAME, SNAPSHOT_FILENAME))

        self.assertTrue(verify_backup(dest)["ok"])

        self.assertEqual(
            hashlib.sha256(snapshot.read_bytes()).hexdigest(), snapshot_before
        )
        self.assertEqual(
            hashlib.sha256(manifest.read_bytes()).hexdigest(), manifest_before
        )
        self.assertEqual(
            tuple(sorted(entry.name for entry in dest.iterdir())), listing_before
        )
        # The read-only verifier never touches the live store either.
        self.assertEqual(self.fingerprint(), live_before)

        # A second independent verification is equally side-effect free.
        self.assertTrue(verify_backup(dest)["ok"])
        self.assertEqual(
            hashlib.sha256(snapshot.read_bytes()).hexdigest(), snapshot_before
        )

        # The synthetic provider key is never serialized into the public manifest
        # (the guarantee is withdrawal-by-design, not a secret-scanner promise).
        self.assertNotIn(API_KEY.encode("utf-8"), manifest.read_bytes())


if __name__ == "__main__":
    unittest.main()
