"""Focused S13-T2 tests: durable origin-tracing workflow, cache, review integration.

Covers ``src/review_article_agent/tracing/workflow.py`` against
``docs/implementation/13-origin-tracing.md``: persisted trace graphs, dependency-valid
caching (retrace on changed claim/source-sha/policy/verdict), an unconfigured-policy
gate, per-project budget surfacing, read-only coverage audit, correction routing, and
section checks that never carry ``primary_verified``.

**Fixture disclosure:** every source is a synthetic in-file document and the
acquisition port is a local fake that counts calls.  No real model, network,
provider, or OpenCode session call is made.

Run::

    PYTHONPATH=src python3 -m unittest tests.test_tracing_workflow -v
"""

from __future__ import annotations

import copy
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from review_article_agent.storage import ProjectStore, paths  # noqa: E402
from review_article_agent.tracing.workflow import (  # noqa: E402
    OriginTraceRepository,
    origin_coverage,
)

POLICY = {
    "max_hops": 4,
    "max_sources_per_claim": 5,
    "max_sources_per_project": 20,
    "max_resolution_retries": 1,
}

_OPS = ("acquisition", "storage", "redistribution", "hosted_transfer")


def _rights(project_id, artifact_id, revision_id):
    """A synthetic, fully valid RightsRecord v1 bound to one exact revision."""
    return {
        "rights_id": f"RIGHTS-{revision_id}",
        "project_id": project_id,
        "record_revision": 1,
        "subject": {
            "kind": "source_revision",
            "project_id": project_id,
            "artifact_id": artifact_id,
            "revision_id": revision_id,
        },
        "operations": {op: "allowed" for op in _OPS},
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
        "recorded_at": "2026-10-28T00:00:00Z",
        "derived_status": "valid",
    }


def _ref(ref_id, *, doi=None, text="ref text"):
    identifiers = {}
    if doi:
        identifiers["doi"] = doi
    return {"ref_id": ref_id, "text": text, "identifiers": identifiers}


def _link(passage_id, marker, state, ref_ids):
    return {
        "passage_id": passage_id,
        "marker": marker,
        "state": state,
        "resolved_ref_ids": list(ref_ids),
    }


def _src(key, sha, passages, references=(), links=()):
    return {
        "source_key": key,
        "source": {"sha256": sha},
        "passages": [{"passage_id": pid, "text": text} for pid, text in passages],
        "references": list(references),
        "citation_links": list(links),
    }


def _atom(atom_id, text, passage_ids):
    return {"atom_id": atom_id, "text": text, "passage_ids": list(passage_ids)}


def _secondary_review(sha="shaR"):
    """A review whose passage secondarily reports a primary result."""
    return _src(
        "review", sha,
        [("p1", "According to the literature [1], compound X inhibits Y.")],
        [_ref("r1", doi="10.1/x")],
        [_link("p1", "[1]", "resolved", ["r1"])],
    )


def _primary():
    return _src("primary", "shaP", [("p1", "We found that compound X inhibits Y in our assay.")])


def _claim(claim_id="c1", paragraph_id="P1", intent_id="I1", root="review",
           text="Compound X inhibits Y."):
    return {
        "claim_id": claim_id,
        "paragraph_id": paragraph_id,
        "intent_id": intent_id,
        "root_source_key": root,
        "atoms": [_atom("a1", text, ["p1"])],
    }


def _checks(verdict="supported"):
    return [
        {
            "check_id": "fc:P1:I1",
            "paragraph_id": "P1",
            "intent_id": "I1",
            "citation_keys": ["CIT-X"],
            "verdict": verdict,
            "support_label": "secondary_support",
        }
    ]


class FakeAcquire:
    """Recording fake acquisition port."""

    def __init__(self, mapping=None, default=None):
        self.mapping = mapping or {}
        self.default = default or {"status": "unavailable"}
        self.calls = []

    def __call__(self, request):
        self.calls.append(request)
        entry = self.mapping.get(request["ref_id"])
        if callable(entry):
            return entry(request)
        if entry is not None:
            return entry
        return self.default


class TraceBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="raa-trace-")
        self.addCleanup(self._tmp.cleanup)
        self.proot = Path(os.path.realpath(self._tmp.name)) / "p"
        self.store = ProjectStore.create_at(
            paths.app_dir_for(str(self.proot)), paths.new_project_id()
        )
        self.addCleanup(self._close)
        self.pid = self.store.project_id
        self.repo = OriginTraceRepository(self.store)

    def _close(self):
        try:
            self.store.close()
        except Exception:
            pass

    def rights(self, artifact_id, revision_id):
        return _rights(self.pid, artifact_id, revision_id)

    def run_trace(self, trace_id, *, claims, sources, policy, review_checks, acquire):
        return self.repo.run(
            trace_id,
            claims=claims,
            sources=sources,
            policy=policy,
            review_checks=review_checks,
            acquire=acquire,
            rights=self.rights,
        )


class PersistedTraceTests(TraceBase):
    def test_persisted_trace_graph_is_readable(self):
        acquire = FakeAcquire({"r1": {"status": "acquired", "source": _primary()}})
        state = self.run_trace(
            "trace-read",
            claims=[_claim()],
            sources={"review": _secondary_review()},
            policy=POLICY,
            review_checks=_checks(),
            acquire=acquire,
        )
        self.assertEqual(state["claims"][0]["outcome"], "origin_traced")

        reread = self.repo.read("trace-read")
        atoms = reread["claims"][0]["atoms"]
        self.assertTrue(atoms[0]["graph"]["nodes"])
        depths = {node["depth"] for node in atoms[0]["graph"]["nodes"]}
        self.assertEqual(depths, {0, 1})
        self.assertEqual(reread["status"], state["status"])


class CacheTests(TraceBase):
    def _seed(self, trace_id="trace-cache"):
        acquire = FakeAcquire({"r1": {"status": "acquired", "source": _primary()}})
        self.run_trace(
            trace_id,
            claims=[_claim()],
            sources={"review": _secondary_review()},
            policy=POLICY,
            review_checks=_checks(),
            acquire=acquire,
        )
        self.assertGreaterEqual(len(acquire.calls), 1)
        return trace_id

    def test_rerun_cache_hit_makes_zero_acquire_calls(self):
        trace_id = self._seed()
        acquire = FakeAcquire({"r1": {"status": "acquired", "source": _primary()}})
        state = self.run_trace(
            trace_id,
            claims=[_claim()],
            sources={"review": _secondary_review()},
            policy=POLICY,
            review_checks=_checks(),
            acquire=acquire,
        )
        self.assertEqual(len(acquire.calls), 0)
        self.assertEqual(state["claims"][0]["cache"], "hit")
        self.assertEqual(state["acquire_calls"], 0)

    def test_changed_claim_text_retraces(self):
        trace_id = self._seed()
        acquire = FakeAcquire({"r1": {"status": "acquired", "source": _primary()}})
        state = self.run_trace(
            trace_id,
            claims=[_claim(text="Compound X inhibits Y strongly.")],
            sources={"review": _secondary_review()},
            policy=POLICY,
            review_checks=_checks(),
            acquire=acquire,
        )
        self.assertEqual(state["claims"][0]["cache"], "miss")
        self.assertGreaterEqual(len(acquire.calls), 1)
        self.assertTrue(state["invalidated"])

    def test_changed_depth0_sha_retraces(self):
        trace_id = self._seed()
        acquire = FakeAcquire({"r1": {"status": "acquired", "source": _primary()}})
        state = self.run_trace(
            trace_id,
            claims=[_claim()],
            sources={"review": _secondary_review(sha="shaR2")},
            policy=POLICY,
            review_checks=_checks(),
            acquire=acquire,
        )
        self.assertEqual(state["claims"][0]["cache"], "miss")
        self.assertGreaterEqual(len(acquire.calls), 1)

    def test_changed_policy_retraces(self):
        trace_id = self._seed()
        acquire = FakeAcquire({"r1": {"status": "acquired", "source": _primary()}})
        changed = dict(POLICY)
        changed["max_hops"] = 5
        state = self.run_trace(
            trace_id,
            claims=[_claim()],
            sources={"review": _secondary_review()},
            policy=changed,
            review_checks=_checks(),
            acquire=acquire,
        )
        self.assertEqual(state["claims"][0]["cache"], "miss")
        self.assertGreaterEqual(len(acquire.calls), 1)

    def test_changed_step12_verdict_retraces(self):
        trace_id = self._seed()
        acquire = FakeAcquire({"r1": {"status": "acquired", "source": _primary()}})
        state = self.run_trace(
            trace_id,
            claims=[_claim()],
            sources={"review": _secondary_review()},
            policy=POLICY,
            review_checks=_checks(verdict="unsupported"),
            acquire=acquire,
        )
        self.assertEqual(state["claims"][0]["cache"], "miss")


class PolicyGateTests(TraceBase):
    def test_unset_policy_requires_configuration_and_no_acquire(self):
        acquire = FakeAcquire({"r1": {"status": "acquired", "source": _primary()}})
        state = self.run_trace(
            "trace-unset",
            claims=[_claim()],
            sources={"review": _secondary_review()},
            policy={},
            review_checks=_checks(),
            acquire=acquire,
        )
        self.assertEqual(state["status"], "configuration_required")
        self.assertEqual(len(acquire.calls), 0)
        self.assertEqual(state["claims"][0]["outcome"], "configuration_required")
        self.assertEqual(state["claims"][0]["atoms"], [])


class BudgetTests(TraceBase):
    def test_budget_limit_surfaces_warning_and_requires_approval(self):
        policy = dict(POLICY)
        policy["max_sources_per_claim"] = 0
        acquire = FakeAcquire({})
        state = self.run_trace(
            "trace-budget",
            claims=[_claim()],
            sources={"review": _secondary_review()},
            policy=policy,
            review_checks=_checks(),
            acquire=acquire,
        )
        self.assertEqual(state["status"], "blocked")
        check = state["section_checks"][0]
        self.assertEqual(check["origin_outcome"], "secondary_warning")
        self.assertTrue(check["requires_final_human_approval"])
        self.assertTrue(check["origin_warning"])

        report = origin_coverage(state)
        self.assertEqual(report["counts"]["secondary_warning"], 1)
        self.assertEqual(report["requires_human_approval"][0]["cite_source_key"], "review")
        self.assertTrue(report["requires_human_approval"][0]["warning"])
        self.assertFalse(report["export_ready"])
        self.assertFalse(report["edits_applied"])


class CoverageReadOnlyTests(TraceBase):
    def test_origin_coverage_does_not_mutate_or_persist(self):
        acquire = FakeAcquire({"r1": {"status": "acquired", "source": _primary()}})
        state = self.run_trace(
            "trace-ro",
            claims=[_claim()],
            sources={"review": _secondary_review()},
            policy=POLICY,
            review_checks=_checks(),
            acquire=acquire,
        )
        before = copy.deepcopy(state)
        head_before = self.repo.artifacts.current_revision("trace-ro").revision_id

        origin_coverage(state)

        self.assertEqual(state, before)
        head_after = self.repo.artifacts.current_revision("trace-ro").revision_id
        self.assertEqual(head_before, head_after)

    def test_correction_proposed_routes_to_step11_and_12(self):
        review = _src(
            "review", "shaR",
            [("p1", "According to [1], X has an effect.")],
            [_ref("r1", doi="10.1/x")],
            [_link("p1", "[1]", "resolved", ["r1"])],
        )
        primary = _src("primary", "shaP", [("p1", "We found no significant effect of X.")])
        acquire = FakeAcquire({"r1": {"status": "acquired", "source": primary}})
        state = self.run_trace(
            "trace-correction",
            claims=[_claim(text="X has an effect.")],
            sources={"review": review},
            policy=POLICY,
            review_checks=_checks(),
            acquire=acquire,
        )
        report = origin_coverage(state)
        self.assertIn("correction_proposed", report["counts"])
        routes = " ".join(blocker["route_to"] for blocker in report["blockers"])
        self.assertIn("step11_drafting/step12_review", routes)


class SectionCheckTests(TraceBase):
    def test_section_checks_keep_verdict_and_never_primary_verified(self):
        acquire = FakeAcquire({"r1": {"status": "acquired", "source": _primary()}})
        state = self.run_trace(
            "trace-checks",
            claims=[_claim()],
            sources={"review": _secondary_review()},
            policy=POLICY,
            review_checks=_checks(verdict="supported"),
            acquire=acquire,
        )
        self.assertNotIn("primary_verified", json.dumps(state))
        check = state["section_checks"][0]
        self.assertEqual(check["verdict"], "supported")
        self.assertEqual(check["citation_keys"], ["CIT-X"])
        self.assertEqual(check["origin_outcome"], "origin_traced")


if __name__ == "__main__":
    unittest.main()
