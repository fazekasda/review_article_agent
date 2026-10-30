"""Operation-scoped validation memo over the LOWER validation chain (S16-P2b1).

The drafting/synthesis chain re-validates the whole immutable upstream chain on
every read.  S16-P2a added the mechanism (``storage/opmemo.py``); this task
applies it to the lower half: human approvals (``storage/human_approvals.py``),
corpus freeze (``discovery/freeze.py``), acquisition manifests
(``acquisition/manifest.py``), and the extraction workflow
(``extraction/workflow.py``).

The tests here assert the three safety properties the mechanism must preserve:

1. within one outer transaction a repeated validation is served from the memo
   (``opmemo.stats`` hits > 0, or the inner ``_validate_payload`` runs once);
2. a write the operation itself makes inside that transaction -- a superseding
   or revoking approval, a new freeze revision -- invalidates the memo and the
   next read fails closed with exactly the same exception type;
3. no entry survives the operation: a later, separate transaction never reuses
   the earlier result (no process-global cache), and outside a transaction the
   call count is unchanged.

Every fixture is a real disposable ``ProjectStore`` built through the existing
test helpers (``test_extraction_workflow.WorkflowBase`` -> ``ManifestBase`` ->
``FreezeTestBase``) with the real owner-capability flow mocked at the
controlling-terminal seam.  No network, provider, or model call is made.

Run::

    PYTHONPATH=src:tests python3 -m unittest tests.test_validation_memo_lower -v
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
TESTS = Path(__file__).resolve().parent
if str(TESTS) not in sys.path:
    sys.path.insert(0, str(TESTS))

from test_extraction_workflow import WorkflowBase  # noqa: E402

from review_article_agent.acquisition import manifest as manifest_mod  # noqa: E402
from review_article_agent.discovery import freeze as freeze_mod  # noqa: E402
from review_article_agent.extraction import workflow as workflow_mod  # noqa: E402
from review_article_agent.extraction.workflow import (  # noqa: E402
    GATE_BATCH,
    GATE_KIND,
    GATE_PILOT,
    MANDATORY_FAILURE_LABELS,
)
from review_article_agent.parsing.repository import _NestedStore  # noqa: E402
from review_article_agent.runtime import owner_auth  # noqa: E402
from review_article_agent.runtime.gates import prepare_gate_snapshot  # noqa: E402
from review_article_agent.storage import opmemo  # noqa: E402
from review_article_agent.storage.human_approvals import (  # noqa: E402
    HumanApprovalRepository,
)

_FREEZE_KIND = freeze_mod.FREEZE_GATE_KIND
_FREEZE_ID = freeze_mod.FREEZE_GATE_ID


class _TxStore(_NestedStore):
    """A nested store that also exposes the outer store's ``schema_version``.

    ``prepare_gate_snapshot`` reads ``store.schema_version``; the plain
    ``_NestedStore`` adapter exposes only ``connection``/``project_id``.
    """

    @property
    def schema_version(self):
        return self._store.schema_version


class MemoLowerBase(WorkflowBase):
    """Shared fixture helpers on top of the real freeze/manifest/extraction stack."""

    # -- authentic freeze ---------------------------------------------------- #
    def authentic_freeze(self):
        """Propose, approve, and return an authentic freeze revision."""
        corpus_ref, coverage_ref, _plan, snapshot = self.freeze_setup()
        revision = self.propose(corpus_ref, coverage_ref, snapshot, accept="accepted")
        self.approve_freeze(revision)
        self.frozen_corpus_ref = corpus_ref
        self.frozen_coverage_ref = coverage_ref
        self.snapshot = snapshot
        return revision

    def propose_on(self, store, corpus_ref, coverage_ref, snapshot, **kwargs):
        """Propose a freeze decision through ``store`` (may be a nested store)."""
        rid = freeze_mod.FreezeRepository.new_revision_id()
        repo = freeze_mod.FreezeRepository(store)
        return repo.propose(
            corpus_ref,
            coverage_ref,
            rights=self.rights(freeze_mod.DEFAULT_FREEZE_ARTIFACT_ID, rid),
            revision_id=rid,
            **kwargs,
        )

    def approve_on(self, store, artifact_id, revision_id, gate_kind, gate_id, *, decision="approved"):
        """Record an approval through ``store`` (may be a nested store)."""
        prepared = prepare_gate_snapshot(
            store,
            self.ref(artifact_id, revision_id),
            gate_kind=gate_kind,
            gate_id=gate_id,
            decision=decision,
            rationale="synthetic human gate",
        )
        capability = owner_auth.request_owner_confirmation(prepared.owner_action())
        return HumanApprovalRepository(store).record_decision(prepared, capability)

    # -- gated extraction pipeline ------------------------------------------ #
    def gated_batch(self):
        """Return an approved ``(manifest, batch_ref)`` for the standard corpus."""
        _a, _b, source, entries = self.standard()
        manifest = self.make(entries)
        self.gate(manifest)
        case = self.case("c1", source_refs=[source])
        pilot_ref = self.put(
            "pilot",
            {
                "config_ref": self.config_ref,
                "cases": [case],
                "adjudications": {case["id"]: self.adjudication(MANDATORY_FAILURE_LABELS)},
            },
        )
        self.approve_ref(pilot_ref, GATE_KIND, GATE_PILOT)
        batch_ref = self.put(
            "batch",
            {
                "config_ref": self.config_ref,
                "pilot_ref": pilot_ref,
                "acquisition_ref": self.ref_of(manifest),
                "cases": [case],
                "seed": "s-1",
            },
        )
        self.approve_ref(batch_ref, GATE_KIND, GATE_BATCH)
        return manifest, batch_ref


class WithinTransactionTests(MemoLowerBase):
    """Criterion 1a: a repeated validation inside one transaction is memoized."""

    def test_require_frozen_validates_payload_once(self):
        revision = self.authentic_freeze()
        calls: list[int] = []
        real = freeze_mod._validate_payload

        def counting(*args, **kwargs):
            calls.append(1)
            return real(*args, **kwargs)

        conn = self.store.connection
        with mock.patch.object(freeze_mod, "_validate_payload", counting):
            with self.store.transaction():
                first = self.freeze.require_frozen()
                second = self.freeze.require_frozen()
                hits, _misses = opmemo.stats(conn)
        self.assertEqual(first, second)
        self.assertTrue(first["frozen"])
        self.assertGreater(hits, 0)
        self.assertEqual(len(calls), 1)

    def test_require_frozen_memo_hits_within_transaction(self):
        self.authentic_freeze()
        conn = self.store.connection
        with self.store.transaction():
            self.freeze.require_frozen()
            self.freeze.require_frozen()
            hits, _misses = opmemo.stats(conn)
        self.assertGreater(hits, 0)

    def test_require_acquired_memoized_within_transaction(self):
        _a, _b, _src, entries = self.standard()
        manifest = self.make(entries)
        self.gate(manifest)
        conn = self.store.connection
        with self.store.transaction():
            first = self.acq.require_acquired()
            second = self.acq.require_acquired()
            hits, _misses = opmemo.stats(conn)
        self.assertEqual(first, second)
        self.assertTrue(first["acquired"])
        self.assertGreater(hits, 0)

    def test_require_batch_memoized_within_transaction(self):
        _manifest, batch_ref = self.gated_batch()
        conn = self.store.connection
        with self.store.transaction():
            first = self.workflow.require_batch(batch_ref)
            second = self.workflow.require_batch(batch_ref)
            hits, _misses = opmemo.stats(conn)
        self.assertEqual(first, second)
        self.assertGreater(hits, 0)

    def test_named_gate_satisfied_memoized_within_transaction(self):
        revision = self.authentic_freeze()
        approvals = HumanApprovalRepository(self.store)
        conn = self.store.connection
        with self.store.transaction():
            first = approvals.named_gate_satisfied(
                revision.artifact_id, gate_kind=_FREEZE_KIND, gate_id=_FREEZE_ID
            )
            second = approvals.named_gate_satisfied(
                revision.artifact_id, gate_kind=_FREEZE_KIND, gate_id=_FREEZE_ID
            )
            hits, _misses = opmemo.stats(conn)
        self.assertTrue(first)
        self.assertTrue(second)
        self.assertGreater(hits, 0)


class SameTransactionFailClosedTests(MemoLowerBase):
    """Criterion 1b: an own write invalidates the memo and the read fails closed."""

    def test_require_frozen_fails_closed_after_new_revision_in_transaction(self):
        self.authentic_freeze()
        nested = _NestedStore(self.store)
        with self.store.transaction():
            self.assertTrue(self.freeze.require_frozen()["frozen"])
            # Write a superseding search-again revision in the same transaction.
            self.propose_on(
                nested,
                self.frozen_corpus_ref,
                self.frozen_coverage_ref,
                self.snapshot,
                request_id="freeze-2",
                decision="search_again",
                entry_reviews=self.reviews_for(self.snapshot),
                rationale="search again",
            )
            with self.assertRaises(freeze_mod.FreezeNotAuthorizedError):
                self.freeze.require_frozen()

    def test_require_acquired_fails_closed_after_superseding_freeze_in_transaction(self):
        _a, _b, _src, entries = self.standard()
        manifest = self.make(entries)
        self.gate(manifest)
        frozen = self.freeze.get(self.freeze_ref)
        corpus_ref = frozen["payload"]["corpus"]["ref"]
        coverage_ref = frozen["payload"]["coverage"]["ref"]
        nested = _NestedStore(self.store)
        with self.store.transaction():
            self.assertTrue(self.acq.require_acquired()["acquired"])
            self.propose_on(
                nested,
                corpus_ref,
                coverage_ref,
                self.snapshot,
                request_id="freeze-2",
                decision="search_again",
                entry_reviews=self.reviews_for(self.snapshot),
                rationale="search again",
            )
            with self.assertRaises(manifest_mod.ManifestNotAcquiredError):
                self.acq.require_acquired()

    def test_named_gate_satisfied_fails_closed_after_rejection_in_transaction(self):
        revision = self.authentic_freeze()
        approvals = HumanApprovalRepository(self.store)
        nested = _TxStore(self.store)
        with self.store.transaction():
            self.assertTrue(
                approvals.named_gate_satisfied(
                    revision.artifact_id, gate_kind=_FREEZE_KIND, gate_id=_FREEZE_ID
                )
            )
            # A newer rejection for the same gate instance supersedes the approval.
            self.approve_on(
                nested,
                revision.artifact_id,
                revision.revision_id,
                _FREEZE_KIND,
                _FREEZE_ID,
                decision="rejected",
            )
            self.assertFalse(
                approvals.named_gate_satisfied(
                    revision.artifact_id, gate_kind=_FREEZE_KIND, gate_id=_FREEZE_ID
                )
            )


class CrossOperationTests(MemoLowerBase):
    """Criterion 1c: no memo entry survives the operation it was computed in."""

    def test_no_cross_operation_reuse_after_revocation(self):
        revision = self.authentic_freeze()
        approvals = HumanApprovalRepository(self.store)
        conn = self.store.connection

        with self.store.transaction():
            self.assertTrue(
                approvals.named_gate_satisfied(
                    revision.artifact_id, gate_kind=_FREEZE_KIND, gate_id=_FREEZE_ID
                )
            )
            self.assertGreater(opmemo.stats(conn)[0], 0)

        # Separate operation: revoke the approval for that exact gate instance.
        self.approve_on(
            self.store,
            revision.artifact_id,
            revision.revision_id,
            _FREEZE_KIND,
            _FREEZE_ID,
            decision="rejected",
        )

        with self.store.transaction():
            # A fresh operation starts with an empty memo: no reuse across it.
            self.assertEqual(opmemo.stats(conn), (0, 0))
            self.assertFalse(
                approvals.named_gate_satisfied(
                    revision.artifact_id, gate_kind=_FREEZE_KIND, gate_id=_FREEZE_ID
                )
            )

    def test_no_cross_operation_reuse_after_new_freeze_revision(self):
        self.authentic_freeze()
        conn = self.store.connection

        with self.store.transaction():
            self.freeze.require_frozen()
            self.assertGreater(opmemo.stats(conn)[0], 0)

        # Separate operation: supersede the freeze with a search-again revision.
        self.propose_on(
            self.store,
            self.frozen_corpus_ref,
            self.frozen_coverage_ref,
            self.snapshot,
            request_id="freeze-2",
            decision="search_again",
            entry_reviews=self.reviews_for(self.snapshot),
            rationale="search again",
        )

        with self.store.transaction():
            self.assertEqual(opmemo.stats(conn), (0, 0))
            with self.assertRaises(freeze_mod.FreezeNotAuthorizedError):
                self.freeze.require_frozen()


class OutsideTransactionTests(MemoLowerBase):
    """Criterion 1d: outside a transaction nothing is cached or counted."""

    def test_outside_transaction_revalidates_every_call(self):
        self.authentic_freeze()
        conn = self.store.connection
        opmemo.invalidate(conn)
        calls: list[int] = []
        real = freeze_mod._validate_payload

        def counting(*args, **kwargs):
            calls.append(1)
            return real(*args, **kwargs)

        with mock.patch.object(freeze_mod, "_validate_payload", counting):
            self.freeze.require_frozen()
            self.freeze.require_frozen()

        # No transaction -> no memo -> both calls re-derive the payload.
        self.assertEqual(len(calls), 2)
        self.assertEqual(opmemo.stats(conn), (0, 0))

    def test_named_gate_satisfied_outside_transaction_unchanged(self):
        revision = self.authentic_freeze()
        approvals = HumanApprovalRepository(self.store)
        conn = self.store.connection
        opmemo.invalidate(conn)
        self.assertTrue(
            approvals.named_gate_satisfied(
                revision.artifact_id, gate_kind=_FREEZE_KIND, gate_id=_FREEZE_ID
            )
        )
        self.assertTrue(
            approvals.named_gate_satisfied(
                revision.artifact_id, gate_kind=_FREEZE_KIND, gate_id=_FREEZE_ID
            )
        )
        self.assertEqual(opmemo.stats(conn), (0, 0))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
