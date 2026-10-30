"""S16-P2b2 tests: the operation-scoped validation memo on the UPPER chain.

The upper validation chain (``synthesis.workflow`` ``require_synthesis`` /
``require_framework`` / ``require_outline`` / ``require_for_blueprints`` /
``_normalize`` and the drafting package/workspace/blueprint reads built on them)
re-validated the whole immutable upstream chain on every read.  Within one
operation the result is deterministic for the database snapshot, so it is now
computed once and reused through
:mod:`review_article_agent.storage.opmemo`.

These tests prove, with the real synthesis/drafting fixtures:

* within one transaction ``require_outline`` (which reaches
  ``require_synthesis`` several times, directly and via ``require_framework``)
  validates the synthesis **once** (criterion 1a);
* a new upstream revision written **inside the same transaction** makes the next
  read fail closed exactly as before -- the operation's own write changes
  ``total_changes`` and misses every earlier memo entry (criterion 1b);
* across separate operations a superseded revision is detected: no memo entry
  survives a transaction boundary (criterion 1c).

Fixtures are the accepted synthetic Step 09 receipt seam inherited from
:mod:`test_synthesis_workflow`; no model, network, or provider call is made.

Run::

    PYTHONPATH=src:tests python3 -m unittest tests.test_validation_memo_upper
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from test_drafting_package import DraftingPackageBase  # noqa: E402
from test_synthesis_workflow import (  # noqa: E402
    SynthesisBase,
)

from review_article_agent.parsing.repository import _NestedStore  # noqa: E402
from review_article_agent.storage import opmemo  # noqa: E402
from review_article_agent.storage.artifacts import ArtifactRepository  # noqa: E402
from review_article_agent.synthesis import workflow as synthesis_workflow  # noqa: E402
from review_article_agent.synthesis.workflow import (  # noqa: E402
    SynthesisWorkflowError,
)


def _counting_validate_synthesis(calls):
    """Return a wrapper around the real ``validate_synthesis`` that counts calls."""
    real = synthesis_workflow.validate_synthesis

    def wrapper(*args, **kwargs):
        calls.append(1)
        return real(*args, **kwargs)

    return wrapper


class UpperChainMemoTests(SynthesisBase):
    def test_require_outline_validates_synthesis_once_per_operation(self):
        # ``require_outline`` reaches ``require_synthesis`` directly, via
        # ``require_framework``, and again inside ``_normalize("outline")``; the
        # memo must validate the synthesis exactly once for the whole operation.
        _syn, _fw, outline_ref = self.approved_chain()
        calls: list[int] = []
        with self.store.transaction():
            opmemo.invalidate(self.store.connection)
            with mock.patch.object(
                synthesis_workflow,
                "validate_synthesis",
                _counting_validate_synthesis(calls),
            ):
                result = self.synth.require_outline(outline_ref)
                self.assertEqual(result["kind"], "outline")
            hits, misses = opmemo.stats(self.store.connection)
        self.assertEqual(len(calls), 1)
        self.assertGreaterEqual(hits, 2)  # repeated require_synthesis/require_framework
        self.assertGreaterEqual(misses, 1)

    def test_repeated_require_outline_in_one_transaction_hits_memo(self):
        _syn, _fw, outline_ref = self.approved_chain()
        with self.store.transaction():
            opmemo.invalidate(self.store.connection)
            first = self.synth.require_outline(outline_ref)
            hits_after_first, _ = opmemo.stats(self.store.connection)
            second = self.synth.require_outline(outline_ref)
            hits_after_second, _ = opmemo.stats(self.store.connection)
        self.assertEqual(first, second)
        self.assertGreater(hits_after_second, hits_after_first)

    def test_new_framework_revision_within_transaction_fails_closed(self):
        # A write the operation itself makes (here: a superseding framework
        # revision imported through a nested savepoint) must invalidate every
        # memo entry, so the next read recomputes and fails closed exactly as an
        # unmemoized read would.
        _syn, framework_ref, outline_ref = self.approved_chain()
        # Sanity: accepted before the write.
        self.assertEqual(self.synth.require_outline(outline_ref)["kind"], "outline")
        nested = ArtifactRepository(_NestedStore(self.store))
        with self.store.transaction():
            opmemo.invalidate(self.store.connection)
            self.assertEqual(self.synth.require_outline(outline_ref)["kind"], "outline")
            nested.import_revision(
                artifact_id=framework_ref["artifact_id"],
                revision_id="fw-r2",
                content=b"superseding framework revision",
                origin_locator="test:supersede-framework",
                rights=self.rights(framework_ref["artifact_id"], "fw-r2"),
                parent_revision_id=framework_ref["revision_id"],
            )
            with self.assertRaises(SynthesisWorkflowError):
                self.synth.require_outline(outline_ref)

    def test_superseded_framework_across_operations_is_detected(self):
        synthesis_ref, _framework_ref, outline_ref = self.approved_chain()
        # Operation 1: accepted and (memoized within its own transaction).
        self.assertEqual(self.synth.require_outline(outline_ref)["kind"], "outline")
        # Operation 2 (a separate transaction): supersede the framework revision,
        # which conservatively stales the dependent outline gate.
        self.put_framework(
            synthesis_ref,
            revision="fw-r2",
            parent="fw-r1",
            c1_definition="A materially revised Compound Z definition.",
        )
        # Operation 3: the outline is now stale; no memo entry survived operation
        # 1, so a fresh read fails closed.
        with self.assertRaises(SynthesisWorkflowError):
            self.synth.require_outline(outline_ref)


class DraftingPackageMemoTests(DraftingPackageBase):
    def test_package_build_validates_synthesis_once_per_operation(self):
        # A single ``build`` re-reads the whole approved chain; the memo must
        # validate the synthesis once for the whole operation.
        _syn, _fw, outline_ref = self.approved_chain()
        calls: list[int] = []
        with mock.patch.object(
            synthesis_workflow,
            "validate_synthesis",
            _counting_validate_synthesis(calls),
        ):
            package = self.pkg.build(outline_ref, "sec1")
        self.assertEqual(package["section"]["section_id"], "sec1")
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
