"""Synthetic scale/operational test harness tests (Step 15, deliverable 2 + AC5).

Covers ``scripts/scale_test.py``: the report shape, a zero-failure small run
(default N=200), and the honesty constraints (zero model/network calls; the
report states that no numeric budget was agreed in Step 01). The real
1,000- and 10,000-candidate runs are gated behind ``RAA_SCALE_TESTS=1`` so the
ordinary suite stays fast; run them explicitly::

    RAA_SCALE_TESTS=1 PYTHONPATH=src python3 -m unittest tests.test_step15_scale -v

Every fixture is synthetic; no test performs a network, provider, or model call.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_SRC = _REPO / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


def _load_scale_module():
    """Load ``scripts/scale_test.py`` by path (scripts is not a package)."""
    path = _REPO / "scripts" / "scale_test.py"
    spec = importlib.util.spec_from_file_location("scale_test_under_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


scale = _load_scale_module()


class ScaleReportShapeTests(unittest.TestCase):
    """Report shape and zero-failure behavior at a small, cheap size."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory(prefix="raa-scale-test-")
        cls.workdir = Path(cls._tmp.name)
        cls.report = scale.run_sizes([200], workdir=cls.workdir)

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def test_top_level_report_shape(self) -> None:
        report = self.report
        self.assertEqual(report["schema_version"], 1)
        self.assertEqual(report["kind"], "step15_scale_report")
        for key in (
            "generated_at_utc",
            "python_version",
            "platform",
            "machine",
            "cpu_count",
            "budget_note",
            "notes",
            "sizes",
        ):
            self.assertIn(key, report)
        self.assertEqual(len(report["sizes"]), 1)

    def test_zero_model_and_network_calls(self) -> None:
        self.assertEqual(self.report["model_calls"], 0)
        self.assertEqual(self.report["network_calls"], 0)

    def test_budget_note_states_no_numeric_budget_agreed_in_01(self) -> None:
        note = self.report["budget_note"].lower()
        self.assertIn("no numeric scale budget", note)
        self.assertIn("step 01", note)

    def test_small_run_has_zero_failures_and_full_counts(self) -> None:
        size = self.report["sizes"][0]
        self.assertEqual(size["size"], 200)
        self.assertEqual(size["failures"], 0)
        self.assertFalse(size["capped"])

        discovery = size["discovery_ledger"]
        self.assertEqual(discovery["records_requested"], 200)
        self.assertEqual(discovery["records_processed"], 200)
        self.assertEqual(discovery["candidates_ingested"], 200)
        self.assertEqual(discovery["failures"], 0)
        self.assertEqual(discovery["errors"], [])
        self.assertTrue(discovery["corpus_snapshot_present"])
        self.assertFalse(discovery["corpus_frozen"])
        self.assertGreaterEqual(discovery["elapsed_seconds"], 0.0)
        self.assertGreater(discovery["store_bytes"], 0)
        self.assertGreaterEqual(discovery["max_rss_kib"], 0)

        queue = size["queue_behaviour"]
        self.assertEqual(queue["jobs_requested"], 200)
        self.assertEqual(queue["jobs_admitted"], 200)
        self.assertEqual(queue["queue_depth_queued"], 200)
        self.assertEqual(queue["failures"], 0)
        self.assertEqual(queue["errors"], [])
        # Duplicate request ids are exercised: half replayed idempotently, and
        # every tenth divergent replay was refused.
        self.assertEqual(queue["duplicates_idempotent"], 100)
        self.assertEqual(queue["duplicates_rejected"], 20)

    def test_stage_metrics_are_present(self) -> None:
        for stage_name in ("discovery_ledger", "queue_behaviour"):
            stage = self.report["sizes"][0][stage_name]
            for key in (
                "elapsed_seconds",
                "peak_python_traced_bytes",
                "max_rss_kib",
                "store_bytes",
                "failures",
                "errors",
                "capped",
            ):
                self.assertIn(key, stage, stage_name)


@unittest.skipUnless(
    os.environ.get("RAA_SCALE_TESTS") == "1",
    "set RAA_SCALE_TESTS=1 to run the 1,000/10,000-candidate scale runs",
)
class GatedScaleTests(unittest.TestCase):
    """The real 1,000- and 10,000-candidate operational runs."""

    def test_1000_and_10000_candidates(self) -> None:
        with tempfile.TemporaryDirectory(prefix="raa-scale-gated-") as tmp:
            report = scale.run_sizes([1000, 10000], workdir=Path(tmp))
        self.assertEqual(len(report["sizes"]), 2)
        for size in report["sizes"]:
            self.assertEqual(size["failures"], 0, size)
            discovery = size["discovery_ledger"]
            self.assertEqual(discovery["failures"], 0, discovery)
            self.assertEqual(
                discovery["candidates_ingested"],
                size["size"],
                discovery,
            )
            self.assertEqual(discovery["records_processed"], size["size"])
            queue = size["queue_behaviour"]
            self.assertEqual(queue["failures"], 0, queue)
            self.assertEqual(queue["jobs_admitted"], size["size"])
            self.assertEqual(queue["queue_depth_queued"], size["size"])
        self.assertEqual(report["model_calls"], 0)
        self.assertEqual(report["network_calls"], 0)


if __name__ == "__main__":
    unittest.main()
