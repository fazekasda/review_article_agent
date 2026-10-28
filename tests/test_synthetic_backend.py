"""Tests for the deterministic durable **synthetic** external-admission backend.

Authority: Step 03 acceptance criteria (``docs/implementation/03-durable-worker.md``
"Kill/restart the test worker before dispatch, after external admission, and
before local commit. Recover one accepted local result without claiming
exactly-once external execution"; "Never blindly resend ambiguous external work
with a new identity") and contract C02 §8.1/§8.2/§8.3
(``docs/contracts/02-gates-and-runtime.md``): admission is durable and separate
from completion; a receipt is never a completion; an ambiguous external outcome
is unresolved and must escalate; absence of durable state is not proof.

Everything here is **local and synthetic**: no OpenCode session, network, model,
or service call, and no licensed text. All fixtures are owned and disposable.

Run with::

    PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_synthetic_backend.py'
"""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from review_article_agent.domain.errors import (  # noqa: E402
    InvalidProjectId,
    ProjectPathError,
    StoreError,
    UnsupportedSchemaVersion,
)
from review_article_agent.storage import paths  # noqa: E402
from review_article_agent.worker.synthetic import (  # noqa: E402
    ACTUAL_EXTERNAL_COST_USD_ZERO,
    JOURNAL_FILENAME,
    MAX_DELAY_MS,
    MAX_STEPS,
    RESULT_MARKER,
    RUNTIME_DIR_NAME,
    AdmissionReceipt,
    SyntheticBackend,
    SyntheticConflict,
    SyntheticContinuityError,
    SyntheticInputError,
)


class _Clock:
    """A monotonic fake clock measured in seconds, shared across instances."""

    def __init__(self, start: float = 1_000_000.0) -> None:
        self.now = float(start)

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += float(seconds)


class SyntheticBackendTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="raa-synthetic-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.data_dir = self.tmp / "provider-side"
        self.clock = _Clock()
        self.project_a = paths.new_project_id()
        self.project_b = paths.new_project_id()

    def backend(self, **kwargs: object) -> SyntheticBackend:
        kwargs.setdefault("clock", self.clock)  # type: ignore[arg-type]
        return SyntheticBackend(self.data_dir, **kwargs)  # type: ignore[arg-type]

    def journal_path(self) -> Path:
        return self.data_dir / RUNTIME_DIR_NAME / JOURNAL_FILENAME

    # ------------------------------------------------------------------ #
    # journal location / owner-private safety
    # ------------------------------------------------------------------ #
    def test_journal_is_separate_owner_private_and_not_the_project_db(self) -> None:
        backend = self.backend()
        receipt = backend.admit("d-1", self.project_a, steps=5, delay_ms=0)
        self.assertIsInstance(receipt, AdmissionReceipt)

        journal = self.journal_path()
        self.assertTrue(journal.is_file())
        # Its own journal, not a project store.
        self.assertNotIn("store.sqlite3", str(journal))
        runtime_dir = self.data_dir / RUNTIME_DIR_NAME
        self.assertEqual(runtime_dir.stat().st_mode & 0o077, 0)
        self.assertEqual(journal.stat().st_mode & 0o077, 0)

    # ------------------------------------------------------------------ #
    # persistence / determinism without the original object
    # ------------------------------------------------------------------ #
    def test_persistent_reopen_and_deterministic_result_without_original_object(self) -> None:
        backend = self.backend()
        receipt = backend.admit("d-restart", self.project_a, steps=7, delay_ms=2000)
        self.assertFalse(receipt.has_outcome)
        self.assertFalse(receipt.is_completion)

        # A fresh process (new instance) with time advanced past ready_at.
        self.clock.advance(3.0)
        del backend
        reopened = self.backend()
        result = reopened.lookup("d-restart", self.project_a)
        self.assertEqual(result.status, "completed")
        self.assertIsNotNone(result.result)
        assert result.result is not None
        self.assertIsInstance(result.result.value, int)
        self.assertEqual(result.result.marker, RESULT_MARKER)
        self.assertTrue(result.result.synthetic)
        self.assertTrue(result.result.not_a_scientific_result)
        self.assertEqual(result.result.actual_external_cost_usd, ACTUAL_EXTERNAL_COST_USD_ZERO)
        self.assertTrue(result.actual_external_cost_known_zero)

        # Deterministic across yet another instance.
        again = self.backend().lookup("d-restart", self.project_a)
        assert again.result is not None
        self.assertEqual(again.result.value, result.result.value)
        # A different identity yields a different deterministic value.
        other = self.backend()
        other.admit("d-other", self.project_a, steps=7, delay_ms=0)
        other_result = other.lookup("d-other", self.project_a)
        assert other_result.result is not None
        self.assertNotEqual(other_result.result.value, result.result.value)

    def test_receipt_is_not_completion_and_result_materializes_over_time(self) -> None:
        backend = self.backend()
        receipt = backend.admit("d-later", self.project_a, steps=3, delay_ms=5000)
        self.assertFalse(receipt.has_outcome)
        self.assertFalse(receipt.is_completion)
        self.assertEqual(receipt.mode, "complete")

        early = backend.lookup("d-later", self.project_a)
        self.assertEqual(early.status, "admitted")
        self.assertFalse(early.definitive)
        self.assertIsNone(early.result)
        self.assertIsNotNone(early.ready_at_utc)
        self.assertFalse(early.escalation_required)

        self.clock.advance(5.0)
        late = backend.lookup("d-later", self.project_a)
        self.assertEqual(late.status, "completed")
        self.assertTrue(late.definitive)
        self.assertIsNotNone(late.result)
        # Stable on repeat.
        self.assertEqual(
            backend.lookup("d-later", self.project_a).result.value,  # type: ignore[union-attr]
            late.result.value,  # type: ignore[union-attr]
        )

    # ------------------------------------------------------------------ #
    # idempotency and divergence
    # ------------------------------------------------------------------ #
    def test_identical_repeated_admit_is_idempotent(self) -> None:
        backend = self.backend()
        first = backend.admit("d-dup", self.project_a, steps=4, delay_ms=1000)
        second = backend.admit("d-dup", self.project_a, steps=4, delay_ms=1000)
        self.assertFalse(first.idempotent)
        self.assertTrue(second.idempotent)
        # Same durable record: identical timestamps, one record.
        self.assertEqual(first.admitted_at_utc, second.admitted_at_utc)
        self.assertEqual(first.ready_epoch_s, second.ready_epoch_s)

    def test_divergent_request_conflicts(self) -> None:
        backend = self.backend()
        backend.admit("d-conf", self.project_a, steps=4, delay_ms=1000)
        with self.assertRaises(SyntheticConflict):
            backend.admit("d-conf", self.project_a, steps=5, delay_ms=1000)
        with self.assertRaises(SyntheticConflict):
            backend.admit("d-conf", self.project_a, steps=4, delay_ms=2000)
        with self.assertRaises(SyntheticConflict):
            backend.admit("d-conf", self.project_a, steps=4, delay_ms=1000, outcome="failure")
        # Divergent project is a conflict too.
        with self.assertRaises(SyntheticConflict):
            backend.admit("d-conf", self.project_b, steps=4, delay_ms=1000)

    # ------------------------------------------------------------------ #
    # failed / ambiguous
    # ------------------------------------------------------------------ #
    def test_failure_is_explicit_definitive_and_non_billing(self) -> None:
        backend = self.backend()
        backend.admit("d-fail", self.project_a, steps=9, delay_ms=0, outcome="failure")
        result = backend.lookup("d-fail", self.project_a)
        self.assertEqual(result.status, "failed")
        self.assertTrue(result.definitive)
        self.assertTrue(result.definitive_failure)
        self.assertTrue(result.non_billing)
        self.assertTrue(result.actual_external_cost_known_zero)
        self.assertFalse(result.outcome_unresolved)
        self.assertIsNone(result.result)

    def test_ambiguous_is_unknown_unresolved_and_requires_escalation(self) -> None:
        backend = self.backend()
        backend.admit("d-amb", self.project_a, steps=9, delay_ms=1000, outcome="ambiguous")
        # In flight first, then unresolved.
        self.assertEqual(backend.lookup("d-amb", self.project_a).status, "admitted")
        self.clock.advance(1.0)
        result = backend.lookup("d-amb", self.project_a)
        self.assertEqual(result.status, "unknown")
        self.assertFalse(result.definitive)
        self.assertTrue(result.outcome_unresolved)
        self.assertTrue(result.escalation_required)
        self.assertIsNone(result.result)

    # ------------------------------------------------------------------ #
    # wrong project
    # ------------------------------------------------------------------ #
    def test_wrong_project_lookup_is_absent_and_admit_conflicts(self) -> None:
        backend = self.backend()
        backend.admit("d-proj", self.project_a, steps=2, delay_ms=0)
        other = backend.lookup("d-proj", self.project_b)
        self.assertEqual(other.status, "absent")
        with self.assertRaises(SyntheticConflict):
            backend.admit("d-proj", self.project_b, steps=2, delay_ms=0)

    # ------------------------------------------------------------------ #
    # identity continuity / journal loss
    # ------------------------------------------------------------------ #
    def test_lost_journal_is_not_absent_and_blocks_same_identity(self) -> None:
        backend = self.backend()
        backend.admit("d-lost", self.project_a, steps=1, delay_ms=0)
        continuity_id = backend.continuity_id
        self.assertIsNotNone(continuity_id)
        self.assertEqual(self.backend().lookup("d-lost", self.project_a).status, "completed")

        # Simulate losing the journal after a prior admission.
        self.journal_path().unlink()

        lost = backend.lookup("d-lost", self.project_a)
        self.assertEqual(lost.status, "unknown")
        self.assertFalse(lost.definitive)
        self.assertTrue(lost.outcome_unresolved)
        self.assertTrue(lost.escalation_required)
        self.assertFalse(backend.same_identity_dispatch_allowed("d-lost", self.project_a))
        with self.assertRaises(SyntheticContinuityError):
            backend.admit("d-lost", self.project_a, steps=1, delay_ms=0)

        # A fresh backend that knows the continuity id must also refuse to treat
        # the lost journal as an absence.
        aware = SyntheticBackend(
            self.data_dir, clock=self.clock, expected_continuity_id=continuity_id
        )
        aware_result = aware.lookup("d-lost", self.project_a)
        self.assertEqual(aware_result.status, "unknown")
        self.assertTrue(aware_result.escalation_required)

    def test_definitive_absence_permits_same_identity_dispatch(self) -> None:
        backend = self.backend()
        result = backend.lookup("d-new", self.project_a)
        self.assertEqual(result.status, "absent")
        self.assertTrue(result.definitive)
        self.assertTrue(backend.same_identity_dispatch_allowed("d-new", self.project_a))

    # ------------------------------------------------------------------ #
    # foreign / symlink / malformed journal untouched
    # ------------------------------------------------------------------ #
    def _make_runtime_dir(self) -> Path:
        runtime = self.data_dir / RUNTIME_DIR_NAME
        runtime.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(runtime, 0o700)
        return runtime

    def test_symlinked_journal_refused_and_target_untouched(self) -> None:
        self._make_runtime_dir()
        target = self.tmp / "foreign-target.sqlite3"
        sqlite3.connect(str(target)).close()
        before = target.read_bytes()
        os.symlink(target, self.journal_path())
        with self.assertRaises(ProjectPathError):
            self.backend()
        self.assertEqual(target.read_bytes(), before)

    def test_foreign_sqlite_journal_refused_and_untouched(self) -> None:
        self._make_runtime_dir()
        journal = self.journal_path()
        conn = sqlite3.connect(str(journal))
        try:
            conn.execute("CREATE TABLE someone_else (x INTEGER)")
            conn.commit()
        finally:
            conn.close()
        os.chmod(journal, 0o600)
        before = journal.read_bytes()
        with self.assertRaises(StoreError):
            self.backend()
        self.assertEqual(journal.read_bytes(), before)

    def test_empty_non_journal_file_refused_and_untouched(self) -> None:
        self._make_runtime_dir()
        journal = self.journal_path()
        sqlite3.connect(str(journal)).close()
        os.chmod(journal, 0o600)
        before = journal.read_bytes()
        with self.assertRaises(StoreError):
            self.backend()
        self.assertEqual(journal.read_bytes(), before)

    def test_lookup_does_not_initialize_journal(self) -> None:
        backend = self.backend()
        self.assertFalse(backend.journal_present)
        backend.lookup("d-none", self.project_a)
        self.assertFalse(backend.journal_present)
        self.assertFalse(self.journal_path().exists())

    def test_newer_schema_refused(self) -> None:
        backend = self.backend()
        backend.admit("d-future", self.project_a, steps=1, delay_ms=0)
        conn = sqlite3.connect(str(self.journal_path()))
        try:
            conn.execute("PRAGMA user_version = 999")
            conn.commit()
        finally:
            conn.close()
        with self.assertRaises(UnsupportedSchemaVersion):
            self.backend()

    # ------------------------------------------------------------------ #
    # safe input bounds / no payload surface
    # ------------------------------------------------------------------ #
    def test_safe_numeric_and_identity_bounds(self) -> None:
        backend = self.backend()
        for steps in (0, -1, MAX_STEPS + 1):
            with self.assertRaises(SyntheticInputError):
                backend.admit("d-bounds", self.project_a, steps=steps, delay_ms=0)
        for delay in (-1, MAX_DELAY_MS + 1):
            with self.assertRaises(SyntheticInputError):
                backend.admit("d-bounds", self.project_a, steps=1, delay_ms=delay)
        for bad in (True, 1.5, "3", None):
            with self.assertRaises(SyntheticInputError):
                backend.admit("d-bounds", self.project_a, steps=bad, delay_ms=0)
        with self.assertRaises(SyntheticInputError):
            backend.admit("d-bounds", self.project_a, steps=1, delay_ms=0, outcome="done")
        for bad_id in ("", "a/b", "../escape", "x" * 129, 5, None):
            with self.assertRaises(SyntheticInputError):
                backend.admit(bad_id, self.project_a, steps=1, delay_ms=0)
        with self.assertRaises(InvalidProjectId):
            backend.admit("d-bounds", "not-a-project", steps=1, delay_ms=0)

    def test_admit_accepts_no_document_or_payload_parameter(self) -> None:
        backend = self.backend()
        with self.assertRaises(TypeError):
            backend.admit(  # type: ignore[call-arg]
                "d-payload", self.project_a, steps=1, delay_ms=0, payload="curl example.com"
            )

    def test_boundary_values_accepted(self) -> None:
        backend = self.backend()
        backend.admit("d-min", self.project_a, steps=1, delay_ms=0)
        backend.admit("d-max", self.project_a, steps=MAX_STEPS, delay_ms=MAX_DELAY_MS)


if __name__ == "__main__":
    unittest.main()
