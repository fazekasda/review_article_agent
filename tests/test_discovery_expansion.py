"""Deterministic tests for bounded bidirectional citation traversal (Step 05).

Covers ``src/review_article_agent/discovery/expansion.py`` against
``docs/implementation/05-discovery.md`` D3 AC5 (forward/backward citation
expansion exposes explicit coverage gaps; never an absolute completeness claim)
and AC7 (no arbitrary result cap is hidden as complete retrieval), and the
citation-origin policy (explicit per-project limits; detect cycles).

Everything runs against a **real** project store and a **fake low-level** provider
hook: no network, no credential, no paid call. Run::

    PYTHONPATH=src python3 -m unittest tests.test_discovery_expansion -v
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from urllib.parse import urlsplit
from _tiers import slow

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from review_article_agent.discovery import expansion, transport  # noqa: E402
from review_article_agent.discovery.expansion import (  # noqa: E402
    CitationNode,
    ExpansionLimits,
    ExpansionValidationError,
    expand_citations,
)
from review_article_agent.domain.records import canonical_json  # noqa: E402
from review_article_agent.storage import ProjectStore, paths  # noqa: E402
from review_article_agent.storage.discovery import DiscoveryRepository  # noqa: E402

BASE = "https://www.ebi.ac.uk/europepmc/webservices/rest"


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
def response(status: int, body: bytes = b""):
    return transport.TransportResponse(status=status, body=body, headers={})


def json_response(payload) -> transport.TransportResponse:
    return response(200, json.dumps(payload).encode("utf-8"))


def ref_payload(hit_count, items) -> dict:
    return {
        "version": "6.9",
        "hitCount": hit_count,
        "referenceList": {"reference": list(items)},
    }


def cit_payload(hit_count, items) -> dict:
    return {
        "version": "6.9",
        "hitCount": hit_count,
        "citationList": {"citation": list(items)},
    }


def entry(rec_id=None, source="MED", **extra) -> dict:
    item: dict = {}
    if source is not None:
        item["source"] = source
    if rec_id is not None:
        item["id"] = rec_id
    item.update(extra)
    return item


class FakeProvider:
    """A scripted low-level provider keyed by ``(source, id, relation)``.

    Each key maps to a list of responses consumed one per low-level attempt. A
    missing/empty script yields an empty completed relation (``hitCount == 0``).
    """

    def __init__(self, script=None) -> None:
        self._script = {key: list(value) for key, value in (script or {}).items()}
        self.calls: list[str] = []

    def __call__(self, url, *, timeout, max_response_bytes):
        self.calls.append(url)
        parts = urlsplit(url)
        segments = parts.path.rstrip("/").split("/")
        source, rec_id, relation = segments[-3], segments[-2], segments[-1]
        key = (source, rec_id, relation)
        items = self._script.get(key)
        if not items:
            payload = ref_payload(0, []) if relation == "references" else cit_payload(0, [])
            return json_response(payload)
        item = items.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def relation_urls(self) -> list[str]:
        return list(self.calls)


def limits(**overrides) -> ExpansionLimits:
    kwargs = dict(
        max_hops=3,
        max_sources=32,
        max_requests=64,
        page_size=25,
        max_pages=5,
    )
    kwargs.update(overrides)
    return ExpansionLimits(**kwargs)


def _edges_for(report, parent_native: str, direction: str):
    return [
        edge
        for edge in report.edges
        if edge.parent.native_id == parent_native and edge.direction == direction
    ]


class ExpansionBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="raa-citation-expansion-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.project_id = paths.new_project_id()
        self.app_dir = paths.app_dir_for(str(self.tmp / "project"))
        self.store = ProjectStore.create_at(self.app_dir, self.project_id)
        self.addCleanup(self.store.close)
        self.repo = DiscoveryRepository(self.store)

    def run_expansion(self, provider, roots, **kwargs):
        params = dict(request_id="run-1", limits=limits(), transport=provider)
        params.update(kwargs)
        return expand_citations(self.repo, roots, **params)


# --------------------------------------------------------------------------- #
# Traversal shape
# --------------------------------------------------------------------------- #
class TraversalTests(ExpansionBase):
    def test_forward_and_backward_cycle_is_bounded_and_deterministic(self):
        provider = FakeProvider(
            {
                ("MED", "1", "references"): [json_response(ref_payload(2, [entry("2"), entry("3")]))],
                ("MED", "1", "citations"): [json_response(cit_payload(1, [entry("4")]))],
                ("MED", "2", "references"): [json_response(ref_payload(1, [entry("1")]))],  # cycle
                ("MED", "2", "citations"): [json_response(cit_payload(0, []))],
                ("MED", "3", "references"): [json_response(ref_payload(1, [entry("4")]))],  # repeat
                ("MED", "3", "citations"): [json_response(cit_payload(0, []))],
                ("MED", "4", "references"): [json_response(ref_payload(0, []))],
                ("MED", "4", "citations"): [json_response(cit_payload(0, []))],
            }
        )
        report = self.run_expansion(provider, [CitationNode("MED", "1")])

        visited = [obs.node.native_id for obs in report.visited]
        # Breadth-first, roots first, in provider occurrence order.
        self.assertEqual(visited, ["MED:1", "MED:2", "MED:3", "MED:4"])
        self.assertTrue(all(obs.expanded for obs in report.visited))
        # The cycle edge MED:2 -> MED:1 is retained but never re-expanded.
        cycle = _edges_for(report, "MED:2", "references")
        self.assertEqual([(e.child_source_id, e.expandable) for e in cycle], [("MED:1", True)])
        # No node is expanded twice.
        self.assertEqual(len(visited), len(set(visited)))
        # Deterministic: an identical operation replays byte-for-byte from the store.
        replay = self.run_expansion(FakeProvider(), [CitationNode("MED", "1")])
        self.assertEqual(report.to_mapping(), replay.to_mapping())
        self.assertEqual(report.to_mapping()["guarantees"], {
            "completeness_claimed": False,
            "verified_support": False,
        })

    def test_repeated_occurrences_are_retained_and_one_node(self):
        provider = FakeProvider(
            {
                ("MED", "1", "references"): [json_response(ref_payload(2, [entry("2"), entry("2")]))],
                ("MED", "2", "references"): [json_response(ref_payload(0, []))],
                ("MED", "2", "citations"): [json_response(cit_payload(0, []))],
            }
        )
        report = self.run_expansion(provider, [CitationNode("MED", "1")])
        occurrences = _edges_for(report, "MED:1", "references")
        self.assertEqual(len(occurrences), 2)
        self.assertEqual([e.occurrence_index for e in occurrences], [0, 1])
        self.assertEqual([e.child_source_id for e in occurrences], ["MED:2", "MED:2"])
        # MED:2 is a single node: exactly one references + one citations relation.
        med2 = [r for r in report.relations if r.parent.native_id == "MED:2"]
        self.assertEqual(sorted(r.direction for r in med2), ["citations", "references"])

    def test_unmatched_reference_keeps_no_invented_id(self):
        provider = FakeProvider(
            {
                ("MED", "1", "references"): [
                    json_response(ref_payload(1, [entry(None, source=None, title="Unmatched")]))
                ],
                ("MED", "1", "citations"): [json_response(cit_payload(0, []))],
            }
        )
        report = self.run_expansion(provider, [CitationNode("MED", "1")])
        edge = _edges_for(report, "MED:1", "references")[0]
        self.assertIsNone(edge.child_source_id)
        self.assertIsNone(edge.child_source)
        self.assertIsNone(edge.child_id)
        self.assertFalse(edge.resolved)
        self.assertFalse(edge.expandable)
        # No relation was ever attempted for a non-existent child.
        self.assertEqual({r.parent.native_id for r in report.relations}, {"MED:1"})
        self.assertEqual(report.to_mapping()["counts"]["unresolved_edges"], 1)


# --------------------------------------------------------------------------- #
# Limits
# --------------------------------------------------------------------------- #
class LimitTests(ExpansionBase):
    def test_hop_limit_records_frontier_and_is_explicit(self):
        provider = FakeProvider(
            {
                ("MED", "1", "references"): [json_response(ref_payload(1, [entry("2")]))],
                ("MED", "1", "citations"): [json_response(cit_payload(0, []))],
            }
        )
        report = self.run_expansion(provider, [CitationNode("MED", "1")], limits=limits(max_hops=1))
        self.assertEqual([obs.node.native_id for obs in report.visited], ["MED:1"])
        self.assertEqual(
            [(f.node.native_id, f.reason) for f in report.frontier],
            [("MED:2", "hop_limit")],
        )
        self.assertIn("hop_limit", {stop.reason for stop in report.stops})
        # Only the root was ever queried.
        self.assertEqual({r.parent.native_id for r in report.relations}, {"MED:1"})

    def test_source_limit_retains_edges_and_marks_frontier(self):
        provider = FakeProvider(
            {
                ("MED", "1", "references"): [
                    json_response(ref_payload(2, [entry("2"), entry("3")]))
                ],
                ("MED", "1", "citations"): [json_response(cit_payload(0, []))],
                ("MED", "2", "references"): [json_response(ref_payload(0, []))],
                ("MED", "2", "citations"): [json_response(cit_payload(0, []))],
            }
        )
        report = self.run_expansion(
            provider, [CitationNode("MED", "1")], limits=limits(max_sources=2)
        )
        # Both occurrences are retained; the over-budget node is an explicit frontier.
        self.assertEqual(len(_edges_for(report, "MED:1", "references")), 2)
        self.assertEqual(
            [(f.node.native_id, f.reason) for f in report.frontier],
            [("MED:3", "source_limit")],
        )
        self.assertIn("source_limit", {stop.reason for stop in report.stops})
        self.assertNotIn("MED:3", {r.parent.native_id for r in report.relations})

    def test_zero_request_budget_performs_no_request(self):
        provider = FakeProvider()
        report = self.run_expansion(
            provider, [CitationNode("MED", "1")], limits=limits(max_requests=0)
        )
        self.assertEqual(provider.calls, [])
        self.assertEqual(report.relations, ())
        self.assertEqual(report.visited, ())
        self.assertEqual(
            [(f.node.native_id, f.reason) for f in report.frontier],
            [("MED:1", "request_limit")],
        )
        self.assertIn("request_limit", {stop.reason for stop in report.stops})

    def test_zero_hop_and_zero_source_bounds_perform_no_request(self):
        provider = FakeProvider()
        report = self.run_expansion(
            provider,
            [CitationNode("MED", "1")],
            limits=limits(max_hops=0, max_sources=0),
        )
        self.assertEqual(provider.calls, [])
        self.assertIn("source_limit", {stop.reason for stop in report.stops})

    @slow
    def test_request_budget_counts_every_retry_attempt(self):
        provider = FakeProvider(
            {
                ("MED", "1", "references"): [
                    response(503),
                    response(503),
                    json_response(ref_payload(0, [])),
                ],
                ("MED", "1", "citations"): [json_response(cit_payload(0, []))],
            }
        )
        report = self.run_expansion(
            provider, [CitationNode("MED", "1")], limits=limits(max_requests=8)
        )
        references = [r for r in report.relations if r.direction == "references"][0]
        # Two retryable attempts plus the success are all counted.
        self.assertEqual(references.attempt_count, 3)
        self.assertEqual(report.to_mapping()["counts"]["request_attempts"], 4)  # 3 + 1

    @slow
    def test_request_budget_interrupts_and_makes_no_further_request(self):
        provider = FakeProvider(
            {
                ("MED", "1", "references"): [
                    response(503),
                    response(503),
                    json_response(ref_payload(0, [])),
                ],
            }
        )
        report = self.run_expansion(
            provider, [CitationNode("MED", "1")], limits=limits(max_requests=2)
        )
        # Exactly the two budgeted attempts, and no third.
        self.assertEqual(len(provider.calls), 2)
        reasons = {stop.reason for stop in report.stops}
        self.assertIn("request_limit", reasons)
        self.assertIn("relation_interrupted", reasons)

    def test_declared_entries_beyond_page_budget_are_an_explicit_gap(self):
        provider = FakeProvider(
            {
                ("MED", "1", "references"): [
                    json_response(ref_payload(2, [entry("2")])),  # declares 2, returns 1
                ],
                ("MED", "1", "citations"): [json_response(cit_payload(0, []))],
            }
        )
        report = self.run_expansion(
            provider, [CitationNode("MED", "1")], limits=limits(max_pages=1)
        )
        references = [r for r in report.relations if r.direction == "references"][0]
        self.assertFalse(references.complete)
        self.assertEqual(references.stop_reason, "page_limit")
        self.assertIn("relation_incomplete", {stop.reason for stop in report.stops})
        # A known-relevant entry the bounded retrieval did not reach is an explicit
        # gap; completeness is never claimed.
        self.assertFalse(report.to_mapping()["guarantees"]["completeness_claimed"])


# --------------------------------------------------------------------------- #
# Failures and the before_request gate
# --------------------------------------------------------------------------- #
class FailureTests(ExpansionBase):
    def test_partial_failure_retains_prior_pages(self):
        provider = FakeProvider(
            {
                ("MED", "1", "references"): [
                    json_response(ref_payload(2, [entry("2")])),
                    response(404),
                ],
                ("MED", "1", "citations"): [json_response(cit_payload(0, []))],
                ("MED", "2", "references"): [json_response(ref_payload(0, []))],
                ("MED", "2", "citations"): [json_response(cit_payload(0, []))],
            }
        )
        report = self.run_expansion(
            provider, [CitationNode("MED", "1")], limits=limits(max_pages=5)
        )
        references = [r for r in report.relations if r.direction == "references"][0]
        self.assertFalse(references.complete)
        self.assertEqual(references.stop_reason, "failure")
        self.assertEqual(references.failure, "http_status")
        self.assertEqual(references.page_count, 2)  # page 1 retained + failed page 2
        # The observed page-1 occurrence is retained and its node still expanded.
        self.assertEqual(len(_edges_for(report, "MED:1", "references")), 1)
        self.assertIn("MED:2", {r.parent.native_id for r in report.relations})
        self.assertIn("relation_incomplete", {s.reason for s in report.stops})

    def test_empty_relation_is_an_explicit_coverage_limitation(self):
        provider = FakeProvider()
        report = self.run_expansion(provider, [CitationNode("MED", "1")])
        self.assertIn("empty_relation", {stop.reason for stop in report.stops})
        self.assertFalse(report.to_mapping()["guarantees"]["completeness_claimed"])

    def test_before_request_denial_makes_no_request(self):
        provider = FakeProvider()
        seen = []

        def refuse(context):
            seen.append(context)
            return False

        report = self.run_expansion(
            provider, [CitationNode("MED", "1")], before_request=refuse
        )
        self.assertEqual(provider.calls, [])
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0].attempt_no, 1)
        self.assertTrue(seen[0].requested_url.startswith(f"{BASE}/MED/1/references"))
        reasons = {stop.reason for stop in report.stops}
        self.assertIn("before_request_refused", reasons)
        self.assertIn("relation_interrupted", reasons)

    def test_before_request_exception_denial_makes_no_request(self):
        provider = FakeProvider()

        def boom(context):
            raise RuntimeError("gate refused")

        report = self.run_expansion(
            provider, [CitationNode("MED", "1")], before_request=boom
        )
        self.assertEqual(provider.calls, [])
        self.assertIn("before_request_refused", {s.reason for s in report.stops})

    @slow
    def test_before_request_is_invoked_before_every_retry(self):
        provider = FakeProvider(
            {
                ("MED", "1", "references"): [
                    response(503),
                    json_response(ref_payload(0, [])),
                ],
                ("MED", "1", "citations"): [json_response(cit_payload(0, []))],
            }
        )
        attempts: list[int] = []

        def allow(context):
            attempts.append(context.attempt_no)
            return True

        report = self.run_expansion(
            provider,
            [CitationNode("MED", "1")],
            limits=limits(max_requests=8),
            before_request=allow,
        )
        # references: attempt 1 (503) + attempt 2 (ok); citations: attempt 3.
        self.assertEqual(attempts, [1, 2, 3])
        self.assertEqual(len(provider.calls), 3)
        self.assertEqual(report.to_mapping()["counts"]["request_attempts"], 3)


# --------------------------------------------------------------------------- #
# Durable replay and operation identity
# --------------------------------------------------------------------------- #
class ExplodingProvider:
    """A low-level provider that must never be called (asserts no I/O)."""

    calls: list[str] = []

    def __call__(self, url, *, timeout, max_response_bytes):
        ExplodingProvider.calls.append(url)
        raise transport.TransportNetworkError("must not be called")


class PersistenceTests(ExpansionBase):
    def test_relations_are_persisted_under_deterministic_keys(self):
        provider = FakeProvider(
            {
                ("MED", "1", "references"): [json_response(ref_payload(0, []))],
                ("MED", "1", "citations"): [json_response(cit_payload(0, []))],
            }
        )
        report = self.run_expansion(provider, [CitationNode("MED", "1")])
        stored = {record.request_id for record in self.repo.list_searches()}
        self.assertEqual(
            {relation.request_id for relation in report.relations}, stored
        )
        for relation in report.relations:
            self.assertTrue(relation.request_id.startswith("dsc_"))
            self.assertEqual(len(relation.result_sha256), 64)

    def test_exact_replay_after_reopen_reuses_results_without_provider_call(self):
        first_provider = FakeProvider(
            {
                ("MED", "1", "references"): [json_response(ref_payload(1, [entry("2")]))],
                ("MED", "1", "citations"): [json_response(cit_payload(0, []))],
                ("MED", "2", "references"): [json_response(ref_payload(0, []))],
                ("MED", "2", "citations"): [json_response(cit_payload(0, []))],
            }
        )
        first = self.run_expansion(first_provider, [CitationNode("MED", "1")])
        self.assertGreater(len(first_provider.calls), 0)

        # Reopen the durable store.
        self.store.close()
        reopened = ProjectStore.open_at(self.app_dir, expected_project_id=self.project_id)
        self.addCleanup(reopened.close)
        repo = DiscoveryRepository(reopened)

        class Exploding:
            calls: list[str] = []

            def __call__(self, url, *, timeout, max_response_bytes):
                Exploding.calls.append(url)
                raise transport.TransportNetworkError("must not be called")

        replay = expand_citations(
            repo,
            [CitationNode("MED", "1")],
            request_id="run-1",
            limits=limits(),
            transport=Exploding(),
        )
        self.assertEqual(Exploding.calls, [])
        self.assertEqual(first.to_mapping(), replay.to_mapping())

    def test_changed_limits_produce_a_distinct_operation_and_preserve_history(self):
        provider = FakeProvider(
            {
                ("MED", "1", "references"): [json_response(ref_payload(0, []))],
                ("MED", "1", "citations"): [json_response(cit_payload(0, []))],
            }
        )
        first = self.run_expansion(
            provider, [CitationNode("MED", "1")], limits=limits(max_hops=1)
        )
        second = self.run_expansion(
            provider, [CitationNode("MED", "1")], limits=limits(max_hops=2)
        )
        self.assertNotEqual(first.operation_fingerprint, second.operation_fingerprint)
        first_keys = {r.request_id for r in first.relations}
        second_keys = {r.request_id for r in second.relations}
        self.assertEqual(first_keys & second_keys, set())
        stored = {record.request_id for record in self.repo.list_searches()}
        # Both operations are retained; history is never overwritten.
        self.assertTrue(first_keys <= stored)
        self.assertTrue(second_keys <= stored)
        # And the first operation still replays unchanged from its own keys.
        replay_first = self.run_expansion(
            FakeProvider(), [CitationNode("MED", "1")], limits=limits(max_hops=1)
        )
        self.assertEqual(first.to_mapping(), replay_first.to_mapping())

    def _reopen_repo(self):
        self.store.close()
        reopened = ProjectStore.open_at(
            self.app_dir, expected_project_id=self.project_id
        )
        self.addCleanup(reopened.close)
        self.store = reopened
        self.repo = DiscoveryRepository(reopened)
        return self.repo

    def test_budget_refusal_mid_relation_keeps_prior_page_and_edge_across_reopen(self):
        provider = FakeProvider(
            {
                ("MED", "1", "references"): [
                    json_response(ref_payload(2, [entry("2")])),  # page 1: declares 2
                    json_response(ref_payload(2, [entry("3")])),  # page 2 (would be needed)
                ],
                ("MED", "1", "citations"): [json_response(cit_payload(0, []))],
            }
        )
        limited = limits(max_requests=1, max_pages=5, page_size=1)
        first = self.run_expansion(
            provider, [CitationNode("MED", "1")], limits=limited
        )
        # Page 1 was the only real request; page 2 was refused locally.
        self.assertEqual(len(provider.calls), 1)
        self.assertLessEqual(len(provider.calls), limited.max_requests)
        reasons = {stop.reason for stop in first.stops}
        self.assertIn("request_limit", reasons)
        self.assertIn("relation_interrupted", reasons)
        # The partial relation is persisted and the page-1 edge is retained.
        references = [r for r in first.relations if r.direction == "references"][0]
        self.assertEqual(references.page_count, 2)  # page 1 + the refused failed page
        self.assertEqual(references.record_count, 1)
        self.assertEqual(
            [e.child_source_id for e in _edges_for(first, "MED:1", "references")],
            ["MED:2"],
        )
        self.assertFalse(first.to_mapping()["guarantees"]["completeness_claimed"])

        # Reopen and replay with an exploding provider: identical report, no I/O.
        repo = self._reopen_repo()
        ExplodingProvider.calls = []
        replay = expand_citations(
            repo,
            [CitationNode("MED", "1")],
            request_id="run-1",
            limits=limited,
            transport=ExplodingProvider(),
        )
        self.assertEqual(ExplodingProvider.calls, [])
        self.assertEqual(first.to_mapping(), replay.to_mapping())

    def test_callback_refusal_mid_relation_keeps_prior_page_and_edge_across_reopen(self):
        provider = FakeProvider(
            {
                ("MED", "1", "references"): [
                    json_response(ref_payload(2, [entry("2")])),  # page 1 allowed
                    json_response(ref_payload(2, [entry("3")])),  # page 2 refused
                ],
                ("MED", "1", "citations"): [json_response(cit_payload(0, []))],
            }
        )
        allowed_first_only = {"count": 0}

        def gate(_context):
            allowed_first_only["count"] += 1
            return allowed_first_only["count"] <= 1

        lim = limits(max_requests=8, max_pages=5, page_size=1)
        first = self.run_expansion(
            provider, [CitationNode("MED", "1")], limits=lim, before_request=gate
        )
        self.assertEqual(len(provider.calls), 1)
        reasons = {stop.reason for stop in first.stops}
        self.assertIn("before_request_refused", reasons)
        self.assertNotIn("request_limit", reasons)
        self.assertEqual(
            [e.child_source_id for e in _edges_for(first, "MED:1", "references")],
            ["MED:2"],
        )

        # Replay WITHOUT the callback must still reproduce the identical report,
        # because the cause is derived from the persisted partial result.
        repo = self._reopen_repo()
        ExplodingProvider.calls = []
        replay = expand_citations(
            repo,
            [CitationNode("MED", "1")],
            request_id="run-1",
            limits=lim,
            transport=ExplodingProvider(),
        )
        self.assertEqual(ExplodingProvider.calls, [])
        self.assertEqual(first.to_mapping(), replay.to_mapping())

    @slow
    def test_retry_then_refusal_keeps_real_attempts_and_no_fictitious_attempt(self):
        provider = FakeProvider(
            {
                ("MED", "1", "references"): [
                    response(503),
                    json_response(ref_payload(0, [])),  # never reached
                ],
            }
        )
        lim = limits(max_requests=1, max_pages=5)
        report = self.run_expansion(provider, [CitationNode("MED", "1")], limits=lim)
        self.assertEqual(len(provider.calls), 1)
        self.assertLessEqual(len(provider.calls), lim.max_requests)
        references = [r for r in report.relations if r.direction == "references"][0]
        self.assertEqual(references.stop_reason, "failure")
        self.assertFalse(references.complete)
        # Exactly the one *real* retryable attempt is retained; no fabricated one.
        self.assertEqual(references.attempt_count, 1)
        stored = {r.request_id: r for r in self.repo.list_searches()}
        partial = stored[references.request_id].result
        last = partial.pages[-1]
        self.assertEqual([a.outcome for a in last.attempts], ["retryable"])
        self.assertEqual([a.status for a in last.attempts], [503])
        self.assertIn("request_limit", {s.reason for s in report.stops})

    def test_accepts_a_project_store_directly(self):
        provider = FakeProvider(
            {
                ("MED", "1", "references"): [json_response(ref_payload(0, []))],
                ("MED", "1", "citations"): [json_response(cit_payload(0, []))],
            }
        )
        report = expand_citations(
            self.store,
            [CitationNode("MED", "1")],
            request_id="run-1",
            limits=limits(),
            transport=provider,
        )
        self.assertEqual(report.project_id, self.project_id)

    def test_report_mapping_is_canonical_json_serializable(self):
        provider = FakeProvider(
            {
                ("MED", "1", "references"): [json_response(ref_payload(0, []))],
                ("MED", "1", "citations"): [json_response(cit_payload(0, []))],
            }
        )
        report = self.run_expansion(provider, [CitationNode("MED", "1")])
        payload = canonical_json(report.to_mapping())
        self.assertIsInstance(payload, bytes)
        self.assertIn(b"citation_expansion", payload)


# --------------------------------------------------------------------------- #
# Validation (fail-closed, zero network)
# --------------------------------------------------------------------------- #
class ValidationTests(ExpansionBase):
    def test_limits_reject_out_of_range_values(self):
        for bad in (
            dict(max_hops=-1),
            dict(max_sources=-1),
            dict(max_requests=-1),
            dict(page_size=0),
            dict(page_size=1001),
            dict(max_pages=0),
            dict(max_pages=1001),
            dict(max_hops=expansion.MAX_HOPS_HARD + 1),
        ):
            with self.subTest(bad=bad):
                with self.assertRaises(ExpansionValidationError):
                    limits(**bad)

    def test_limits_reject_non_integers(self):
        with self.assertRaises(ExpansionValidationError):
            ExpansionLimits(max_hops=True, max_sources=1, max_requests=1, page_size=1, max_pages=1)

    def test_mapping_limits_require_exact_fields(self):
        with self.assertRaises(ExpansionValidationError):
            expand_citations(
                self.repo,
                [CitationNode("MED", "1")],
                request_id="run-1",
                limits={"max_hops": 1},
                transport=FakeProvider(),
            )

    def test_invalid_root_is_refused_before_any_request(self):
        provider = FakeProvider()
        with self.assertRaises(ExpansionValidationError):
            expand_citations(
                self.repo, ["MED"], request_id="run-1", limits=limits(), transport=provider
            )
        with self.assertRaises(ExpansionValidationError):
            expand_citations(
                self.repo,
                [CitationNode("MED", "1"), ("lower", "case")],
                request_id="run-1",
                limits=limits(),
                transport=provider,
            )
        self.assertEqual(provider.calls, [])

    def test_bare_pair_is_not_an_accepted_root_collection(self):
        with self.assertRaises(ExpansionValidationError):
            expand_citations(
                self.repo, ("MED", "1"), request_id="run-1", limits=limits(),
                transport=FakeProvider(),
            )

    def test_invalid_request_id_is_refused(self):
        with self.assertRaises(ExpansionValidationError):
            expand_citations(
                self.repo,
                [CitationNode("MED", "1")],
                request_id="../escape",
                limits=limits(),
                transport=FakeProvider(),
            )

    def test_non_callable_transport_and_before_request_are_refused(self):
        with self.assertRaises(ExpansionValidationError):
            expand_citations(
                self.repo, [CitationNode("MED", "1")], request_id="run-1",
                limits=limits(), transport="not-callable",
            )
        with self.assertRaises(ExpansionValidationError):
            expand_citations(
                self.repo, [CitationNode("MED", "1")], request_id="run-1",
                limits=limits(), transport=FakeProvider(), before_request=object(),
            )


if __name__ == "__main__":
    unittest.main()
