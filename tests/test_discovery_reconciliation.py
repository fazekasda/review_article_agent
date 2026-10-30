"""Deterministic tests for the publication-overlap review report (Step 05, T1).

Covers ``src/review_article_agent/discovery/reconciliation.py`` against the T1
acceptance criteria:

* cross-provider shared ``pmid``; same-source native ids; overlap within one run
  and across runs; incremental reruns;
* identical replay collapse and divergent replay conflict (static, value-free);
* a chain bridge with conflicting ids flags the **whole** component as a
  conflict and never a resolved duplicate;
* different providers with the same ``source_id`` do **not** match; title-only
  records do **not** merge; missing-identifier records stay explicit singletons;
* exact, case-sensitive comparison (no case/prefix/URL/title/year fuzzing);
* partial failed results are retained with their stop reason/failure;
* invalid types/bools/surrogates and forged/mutated dataclasses are refused with
  static value-free errors (never echoing queries/titles/identifiers/run ids);
* outputs are immutable and permutation-stable (sorted run ids, preserved
  record index);
* an empty input is a valid empty report;
* a technical occurrence bound is an explicit refusal, never a truncation, and a
  10k-record synthetic input is handled without an all-pairs blow-up.

No network, no credentials, no persistence. Run:

    PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_discovery_reconciliation.py'
"""

from __future__ import annotations

import dataclasses
import sys
import time
import unittest
from pathlib import Path
from unittest import mock
from _tiers import slow

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from review_article_agent.discovery import reconciliation  # noqa: E402
from review_article_agent.discovery.pubmed import PubMedSearchResult  # noqa: E402
from review_article_agent.discovery.records import (  # noqa: E402
    SOURCE_EUROPEPMC,
    SOURCE_PUBMED,
    MetadataRecord,
    SearchPage,
    SearchResult,
)
from review_article_agent.discovery.reconciliation import (  # noqa: E402
    CLASS_IDENTIFIER_CONFLICT,
    CLASS_IDENTIFIER_OVERLAP,
    CLASS_MISSING_IDENTIFIERS,
    CLASS_UNMATCHED,
    CLASSIFICATIONS,
    MAX_TOTAL_OCCURRENCES,
    DiscoveryReconciliationError,
    IdentifierToken,
    Occurrence,
    SearchRun,
    reconcile_searches,
)


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
def record(**overrides) -> MetadataRecord:
    kwargs = dict(
        source=SOURCE_PUBMED,
        source_id="12345678",
        pmid="12345678",
        pmcid="PMC1234567",
        doi="10.1000/xyz",
        title="A study of things",
        publication_year=2020,
    )
    kwargs.update(overrides)
    return MetadataRecord(**kwargs)


def _page(records, *, source=SOURCE_PUBMED, query="q", page_index=0,
          stop_reason="completed", failure=None, complete=None):
    if complete is None:
        complete = stop_reason == "completed" and failure is None
    return SearchPage(
        source=source,
        query=query,
        requested_url="https://example.test/search?term=q",
        retrieved_at="2026-10-28T00:00:00+00:00",
        page_index=page_index,
        records=tuple(records),
        page_size=20,
        total_count=len(records),
        complete=complete,
        stop_reason=stop_reason,
        failure=failure,
    )


def _result(records, *, source=SOURCE_PUBMED, query="q", stop_reason="completed",
            failure=None, pages=None):
    if pages is None:
        pages = (
            _page(
                records,
                source=source,
                query=query,
                stop_reason="failure" if failure is not None else stop_reason,
                failure=failure,
            ),
        )
    return SearchResult.from_pages(
        source=source,
        query=query,
        pages=pages,
        started_at="2026-10-28T00:00:00+00:00",
        completed_at="2026-10-28T00:00:05+00:00",
        stop_reason=stop_reason,
        failure=failure,
    )


def _run(run_id, records, **overrides) -> SearchRun:
    return SearchRun(run_id=run_id, result=_result(records, **overrides))


# --------------------------------------------------------------------------- #
# Cross-provider and native-id matching
# --------------------------------------------------------------------------- #
class MatchingTests(unittest.TestCase):
    def test_cross_provider_shared_pmid_overlaps(self):
        first = _run(
            "run-a",
            [record(source=SOURCE_PUBMED, source_id="A1", pmid="111", pmcid=None, doi=None)],
        )
        second = _run(
            "run-b",
            [record(source=SOURCE_EUROPEPMC, source_id="B1", pmid="111", pmcid=None, doi=None)],
            source=SOURCE_EUROPEPMC,
        )
        report = reconcile_searches([first, second])
        self.assertEqual(report.group_count, 1)
        group = report.groups[0]
        self.assertEqual(group.classification, CLASS_IDENTIFIER_OVERLAP)
        self.assertEqual(len(group.occurrences), 2)
        self.assertEqual(
            group.shared_tokens, (IdentifierToken("pmid", "111"),)
        )
        self.assertEqual(group.conflicts, ())
        # Occurrences are ordered by run id then record index.
        self.assertEqual(
            [occ.key for occ in group.occurrences], [("run-a", 0), ("run-b", 0)]
        )

    def test_same_source_native_ids_match_across_runs(self):
        first = _run(
            "run-a",
            [record(source=SOURCE_PUBMED, source_id="N1", pmid=None, pmcid=None, doi=None)],
        )
        second = _run(
            "run-b",
            [record(source=SOURCE_PUBMED, source_id="N1", pmid=None, pmcid=None, doi=None)],
        )
        report = reconcile_searches([first, second])
        self.assertEqual(report.group_count, 1)
        group = report.groups[0]
        self.assertEqual(group.classification, CLASS_IDENTIFIER_OVERLAP)
        self.assertEqual(
            group.shared_tokens, (IdentifierToken("native:pubmed", "N1"),)
        )

    def test_overlap_within_one_run_preserves_both_occurrences(self):
        run = _run(
            "run-a",
            [
                record(source=SOURCE_PUBMED, source_id=None, pmid="42", pmcid=None, doi=None),
                record(source=SOURCE_PUBMED, source_id=None, pmid="42", pmcid=None, doi=None),
            ],
        )
        report = reconcile_searches([run])
        self.assertEqual(report.total_occurrences, 2)
        self.assertEqual(report.group_count, 1)
        group = report.groups[0]
        self.assertEqual(group.classification, CLASS_IDENTIFIER_OVERLAP)
        self.assertEqual([occ.record_index for occ in group.occurrences], [0, 1])
        self.assertEqual([occ.run_id for occ in group.occurrences], ["run-a", "run-a"])

    def test_incremental_rerun_overlap_and_new_candidate(self):
        original = _run("run-1", [record(pmid="1", pmcid=None, doi=None, source_id=None)])
        rerun = _run(
            "run-2",
            [
                record(pmid="1", pmcid=None, doi=None, source_id=None),
                record(pmid="2", pmcid=None, doi=None, source_id=None),
            ],
        )
        report = reconcile_searches([original, rerun])
        self.assertEqual(report.total_occurrences, 3)
        self.assertEqual(report.group_count, 2)
        overlap = report.groups_of(CLASS_IDENTIFIER_OVERLAP)
        self.assertEqual(len(overlap), 1)
        self.assertEqual(len(overlap[0].occurrences), 2)
        unmatched = report.groups_of(CLASS_UNMATCHED)
        self.assertEqual(len(unmatched), 1)
        self.assertEqual(unmatched[0].occurrences[0].record.pmid, "2")

    def test_identifier_union_bridges_components(self):
        pubmed = _run(
            "run-a",
            [record(source=SOURCE_PUBMED, source_id="n1", pmid=None, pmcid=None, doi="10.1/x")],
        )
        epmc = _run(
            "run-b",
            [
                record(
                    source=SOURCE_EUROPEPMC,
                    source_id="n2",
                    pmid=None,
                    pmcid=None,
                    doi="10.1/x",
                )
            ],
            source=SOURCE_EUROPEPMC,
        )
        report = reconcile_searches([pubmed, epmc])
        self.assertEqual(report.group_count, 1)
        self.assertEqual(
            report.groups[0].shared_tokens, (IdentifierToken("doi", "10.1/x"),)
        )


# --------------------------------------------------------------------------- #
# Exact comparison: no matching when normalization would be required
# --------------------------------------------------------------------------- #
class ExactComparisonTests(unittest.TestCase):
    def test_different_providers_same_source_id_do_not_match(self):
        pubmed = _run(
            "run-a",
            [record(source=SOURCE_PUBMED, source_id="SAME", pmid=None, pmcid=None, doi=None)],
        )
        epmc = _run(
            "run-b",
            [
                record(
                    source=SOURCE_EUROPEPMC,
                    source_id="SAME",
                    pmid=None,
                    pmcid=None,
                    doi=None,
                )
            ],
            source=SOURCE_EUROPEPMC,
        )
        report = reconcile_searches([pubmed, epmc])
        self.assertEqual(report.group_count, 2)
        self.assertEqual(report.count(CLASS_UNMATCHED), 2)
        self.assertEqual(report.count(CLASS_IDENTIFIER_OVERLAP), 0)

    def test_title_only_records_do_not_merge(self):
        first = MetadataRecord(source=SOURCE_PUBMED, title="Shared exact title")
        second = MetadataRecord(source=SOURCE_PUBMED, title="Shared exact title")
        report = reconcile_searches(
            [_run("run-a", [first]), _run("run-b", [second])]
        )
        self.assertEqual(report.group_count, 2)
        self.assertEqual(report.count(CLASS_MISSING_IDENTIFIERS), 2)
        self.assertEqual(report.count(CLASS_IDENTIFIER_OVERLAP), 0)

    def test_identifier_comparison_is_case_sensitive(self):
        upper = _run(
            "run-a", [record(pmid=None, pmcid="PMC1", doi=None, source_id=None)]
        )
        lower = _run(
            "run-b", [record(pmid=None, pmcid="pmc1", doi=None, source_id=None)]
        )
        report = reconcile_searches([upper, lower])
        self.assertEqual(report.group_count, 2)
        self.assertEqual(report.count(CLASS_UNMATCHED), 2)

    def test_identifier_prefix_and_url_are_not_normalized(self):
        bare = _run("run-a", [record(pmid=None, pmcid=None, doi="10.1/x", source_id=None)])
        prefixed = _run(
            "run-b", [record(pmid=None, pmcid=None, doi="doi:10.1/x", source_id=None)]
        )
        url = _run(
            "run-c",
            [record(pmid=None, pmcid=None, doi="https://doi.org/10.1/x", source_id=None)],
        )
        report = reconcile_searches([bare, prefixed, url])
        self.assertEqual(report.group_count, 3)
        self.assertEqual(report.count(CLASS_UNMATCHED), 3)

    def test_missing_identifier_is_an_explicit_singleton(self):
        report = reconcile_searches([_run("run-a", [MetadataRecord(source=SOURCE_PUBMED)])])
        self.assertEqual(report.group_count, 1)
        group = report.groups[0]
        self.assertEqual(group.classification, CLASS_MISSING_IDENTIFIERS)
        self.assertEqual(group.shared_tokens, ())
        self.assertEqual(group.tokens, ())
        self.assertTrue(group.occurrences[0].record.is_missing_identifier())


# --------------------------------------------------------------------------- #
# Conflicts: whole-component flagging, never a resolved duplicate
# --------------------------------------------------------------------------- #
class ConflictTests(unittest.TestCase):
    def test_chain_bridge_conflicting_ids_flag_whole_component(self):
        a = record(source=SOURCE_PUBMED, source_id=None, pmid="1", pmcid=None, doi=None)
        b = record(source=SOURCE_PUBMED, source_id=None, pmid="1", pmcid=None, doi="10.1/x")
        c = record(source=SOURCE_PUBMED, source_id=None, pmid="2", pmcid=None, doi="10.1/x")
        report = reconcile_searches([_run("run-a", [a, b, c])])

        # One component; A-B share pmid 1, B-C share doi, A/C share nothing.
        self.assertEqual(report.group_count, 1)
        group = report.groups[0]
        self.assertEqual(len(group.occurrences), 3)
        self.assertEqual(group.classification, CLASS_IDENTIFIER_CONFLICT)
        self.assertTrue(group.is_conflict)
        self.assertNotEqual(group.classification, CLASS_IDENTIFIER_OVERLAP)
        self.assertEqual(group.conflicting_namespaces, ("pmid",))
        self.assertEqual(group.conflicts, (("pmid", ("1", "2")),))
        # Shared tokens still show the bridging evidence (sorted by namespace).
        self.assertEqual(
            group.shared_tokens,
            (IdentifierToken("doi", "10.1/x"), IdentifierToken("pmid", "1")),
        )

    def test_conflicting_namespaced_ids_across_runs(self):
        first = _run("run-a", [record(pmid="1", pmcid=None, doi=None, source_id=None)])
        second = _run("run-b", [record(pmid="2", pmcid=None, doi="1", source_id=None)])
        report = reconcile_searches([first, second])
        # No shared exact token -> two singletons, no conflict.
        self.assertEqual(report.group_count, 2)

    def test_same_native_id_conflicting_pmid_is_a_conflict(self):
        first = _run(
            "run-a", [record(source=SOURCE_PUBMED, source_id="N", pmid="1", pmcid=None, doi=None)]
        )
        second = _run(
            "run-b", [record(source=SOURCE_PUBMED, source_id="N", pmid="2", pmcid=None, doi=None)]
        )
        report = reconcile_searches([first, second])
        self.assertEqual(report.group_count, 1)
        group = report.groups[0]
        self.assertEqual(group.classification, CLASS_IDENTIFIER_CONFLICT)
        self.assertEqual(group.conflicts, (("pmid", ("1", "2")),))
        # The shared native token is the matching evidence.
        self.assertEqual(
            group.shared_tokens, (IdentifierToken("native:pubmed", "N"),)
        )

    def test_same_provider_divergent_native_ids_conflict(self):
        # Two records from the SAME provider assert the same pmid but different
        # provider-native ids; the native namespace is a publication namespace,
        # so the whole component is a conflict, never a resolved duplicate.
        first = _run(
            "run-a",
            [record(source=SOURCE_PUBMED, source_id="A", pmid="1", pmcid=None, doi=None)],
        )
        second = _run(
            "run-b",
            [record(source=SOURCE_PUBMED, source_id="B", pmid="1", pmcid=None, doi=None)],
        )
        report = reconcile_searches([first, second])
        self.assertEqual(report.group_count, 1)
        group = report.groups[0]
        self.assertEqual(group.classification, CLASS_IDENTIFIER_CONFLICT)
        self.assertEqual(group.conflicting_namespaces, ("native:pubmed",))
        self.assertEqual(group.conflicts, (("native:pubmed", ("A", "B")),))
        self.assertEqual(group.shared_tokens, (IdentifierToken("pmid", "1"),))


# --------------------------------------------------------------------------- #
# Replay / divergence
# --------------------------------------------------------------------------- #
class ReplayTests(unittest.TestCase):
    def test_identical_replay_collapses(self):
        run = _run("run-a", [record(pmid="1", pmcid=None, doi=None)])
        report = reconcile_searches([run, run])
        self.assertEqual(report.total_runs, 1)
        self.assertEqual(report.total_occurrences, 1)
        self.assertEqual(report.group_count, 1)

    def test_equal_but_distinct_replay_collapses(self):
        first = _run("run-a", [record(pmid="1", pmcid=None, doi=None)])
        second = _run("run-a", [record(pmid="1", pmcid=None, doi=None)])
        report = reconcile_searches([first, second])
        self.assertEqual(report.total_runs, 1)
        self.assertEqual(report.total_occurrences, 1)

    def test_divergent_replay_is_a_static_conflict(self):
        first = _run("run-a", [record(pmid="1", pmcid=None, doi=None)])
        second = _run("run-a", [record(pmid="2", pmcid=None, doi=None)])
        with self.assertRaises(DiscoveryReconciliationError) as ctx:
            reconcile_searches([first, second])
        message = str(ctx.exception)
        # Static and value-free: no run id, query, or identifier is echoed.
        self.assertNotIn("run-a", message)
        self.assertNotIn("q", message)
        self.assertNotIn("1", message)
        self.assertEqual(message, "search runs contain a divergent duplicate run id")


# --------------------------------------------------------------------------- #
# Partial failures are retained
# --------------------------------------------------------------------------- #
class PartialFailureTests(unittest.TestCase):
    def test_failed_result_is_retained_with_its_reason(self):
        run = _run(
            "run-a",
            [record(pmid="1", pmcid=None, doi=None)],
            stop_reason="failure",
            failure="http_status",
        )
        report = reconcile_searches([run])
        self.assertEqual(report.total_runs, 1)
        self.assertEqual(report.total_occurrences, 1)
        self.assertEqual(report.runs[0].result.failure, "http_status")
        self.assertEqual(report.runs[0].result.stop_reason, "failure")
        self.assertFalse(report.runs[0].result.complete)

    def test_incomplete_but_retained_mixed_with_complete_run(self):
        failed = _run(
            "run-a",
            [record(pmid="1", pmcid=None, doi=None)],
            stop_reason="failure",
            failure="network_error",
        )
        complete = _run("run-b", [record(pmid="1", pmcid=None, doi=None)])
        report = reconcile_searches([failed, complete])
        self.assertEqual(report.total_occurrences, 2)
        self.assertEqual(report.group_count, 1)
        self.assertEqual(report.groups[0].classification, CLASS_IDENTIFIER_OVERLAP)
        reasons = {run.run_id: run.result.stop_reason for run in report.runs}
        self.assertEqual(reasons, {"run-a": "failure", "run-b": "completed"})


# --------------------------------------------------------------------------- #
# Validation: types, bools, surrogates, forged dataclasses
# --------------------------------------------------------------------------- #
class ValidationTests(unittest.TestCase):
    def test_non_iterable_input_is_refused(self):
        for bad in (42, None, object()):
            with self.subTest(bad=bad):
                with self.assertRaises(DiscoveryReconciliationError):
                    reconcile_searches(bad)  # type: ignore[arg-type]

    def test_string_input_is_refused_not_iterated(self):
        with self.assertRaises(DiscoveryReconciliationError):
            reconcile_searches("not runs")  # type: ignore[arg-type]

    def test_wrong_element_type_is_refused(self):
        with self.assertRaises(DiscoveryReconciliationError):
            reconcile_searches([object()])  # type: ignore[list-item]

    def test_bool_run_id_is_refused(self):
        with self.assertRaises(DiscoveryReconciliationError):
            SearchRun(run_id=True, result=_result([record()]))  # type: ignore[arg-type]

    def test_empty_or_control_run_id_is_refused(self):
        for bad in ("", "has\x00nul", "line\nbreak"):
            with self.subTest(bad=bad):
                with self.assertRaises(DiscoveryReconciliationError):
                    SearchRun(run_id=bad, result=_result([record()]))

    def test_surrogate_run_id_is_refused(self):
        with self.assertRaises(DiscoveryReconciliationError):
            SearchRun(run_id="\ud800", result=_result([record()]))

    def test_result_type_is_validated(self):
        with self.assertRaises(DiscoveryReconciliationError):
            SearchRun(run_id="run-a", result="not a result")  # type: ignore[arg-type]

    def test_surrogate_in_result_is_refused(self):
        bad = record(title="bad \ud800 title")
        with self.assertRaises(DiscoveryReconciliationError):
            reconcile_searches([SearchRun(run_id="run-a", result=_result([bad]))])

    def test_forged_mutated_records_field_is_refused(self):
        result = _result([record(pmid="1", pmcid=None, doi=None)])
        object.__setattr__(result, "records", ("not a record",))
        with self.assertRaises(DiscoveryReconciliationError):
            reconcile_searches([SearchRun(run_id="run-a", result=result)])

    def test_mutated_inconsistent_aggregate_is_refused(self):
        result = _result([record(pmid="1", pmcid=None, doi=None)])
        # Break the records/pages agreement enforced by the real constructor.
        object.__setattr__(result, "records", ())
        with self.assertRaises(DiscoveryReconciliationError):
            reconcile_searches([SearchRun(run_id="run-a", result=result)])

    def test_mutated_record_identifier_is_refused(self):
        rec = record(pmid="1", pmcid=None, doi=None)
        # Bypass the frozen dataclass to forge a value the real constructor
        # would have rejected (whitespace in an identifier).
        object.__setattr__(rec, "pmid", " 1 ")
        with self.assertRaises(DiscoveryReconciliationError):
            reconcile_searches([SearchRun(run_id="run-a", result=_result([rec]))])

    def test_validation_errors_are_static_and_value_free(self):
        secret_title = "UNIQUE-SECRET-TITLE-12345"
        secret_pmid = "999888777"
        result = _result([record(pmid="1", pmcid=None, doi=None, title=secret_title)])
        object.__setattr__(result, "records", ())
        with self.assertRaises(DiscoveryReconciliationError) as ctx:
            reconcile_searches([SearchRun(run_id="run-a", result=result)])
        message = str(ctx.exception)
        self.assertNotIn(secret_title, message)
        self.assertNotIn(secret_pmid, message)
        self.assertNotIn("run-a", message)

    def test_unknown_classification_query_is_refused(self):
        report = reconcile_searches([])
        with self.assertRaises(DiscoveryReconciliationError):
            report.count("not_a_classification")
        with self.assertRaises(DiscoveryReconciliationError):
            report.groups_of("not_a_classification")

    def test_pubmed_subtype_result_is_accepted_and_retained(self):
        # The exact PubMed subtype must pass the codec round-trip guard and be
        # preserved as the original input value.
        page = _page([record(pmid="1", pmcid=None, doi=None, source_id=None)])
        pubmed = PubMedSearchResult.from_pages(
            source=SOURCE_PUBMED,
            query="q",
            pages=(page,),
            started_at="2026-10-28T00:00:00+00:00",
            completed_at="2026-10-28T00:00:05+00:00",
            stop_reason="completed",
            query_translation="exact translation",
        )
        report = reconcile_searches([SearchRun(run_id="run-a", result=pubmed)])
        self.assertIs(report.runs[0].result, pubmed)
        self.assertEqual(report.runs[0].result.query_translation, "exact translation")
        self.assertEqual(report.total_occurrences, 1)


# --------------------------------------------------------------------------- #
# Empty, immutability, determinism
# --------------------------------------------------------------------------- #
class ReportPropertiesTests(unittest.TestCase):
    def test_empty_input_is_a_valid_empty_report(self):
        report = reconcile_searches([])
        self.assertEqual(report.total_runs, 0)
        self.assertEqual(report.total_occurrences, 0)
        self.assertEqual(report.group_count, 0)
        self.assertEqual(report.groups, ())
        self.assertEqual(report.runs, ())
        self.assertEqual(report.sources, ())
        for classification in CLASSIFICATIONS:
            self.assertEqual(report.count(classification), 0)
        self.assertFalse(report.authorizing)
        self.assertFalse(report.completeness_claimed)
        self.assertTrue(report.notice)

    def test_report_is_explicitly_non_authorizing(self):
        report = reconcile_searches([_run("run-a", [record(pmid="1", pmcid=None, doi=None)])])
        self.assertIs(report.authorizing, False)
        self.assertIs(report.completeness_claimed, False)
        self.assertIn("not", report.notice)

    def test_group_ids_are_report_local_and_stable(self):
        report = reconcile_searches(
            [
                _run("run-a", [record(pmid="1", pmcid=None, doi=None, source_id=None)]),
                _run("run-b", [record(pmid="2", pmcid=None, doi=None, source_id=None)]),
            ]
        )
        self.assertEqual(
            [group.group_id for group in report.groups], ["group-0001", "group-0002"]
        )

    def test_outputs_are_immutable(self):
        report = reconcile_searches([_run("run-a", [record(pmid="1", pmcid=None, doi=None)])])
        with self.assertRaises(dataclasses.FrozenInstanceError):
            report.groups = ()  # type: ignore[misc]
        with self.assertRaises(dataclasses.FrozenInstanceError):
            report.runs[0].run_id = "changed"  # type: ignore[misc]
        group = report.groups[0]
        with self.assertRaises(dataclasses.FrozenInstanceError):
            group.classification = "changed"  # type: ignore[misc]
        with self.assertRaises(dataclasses.FrozenInstanceError):
            group.occurrences[0].record_index = 5  # type: ignore[misc]
        self.assertIsInstance(report.runs, tuple)
        self.assertIsInstance(report.groups, tuple)
        self.assertIsInstance(group.occurrences, tuple)
        self.assertIsInstance(group.shared_tokens, tuple)

    def test_classification_counts_cover_every_classification(self):
        report = reconcile_searches(
            [
                _run("run-a", [record(pmid="1", pmcid=None, doi=None, source_id=None)]),
                _run("run-b", [record(pmid="1", pmcid=None, doi=None, source_id=None)]),
                _run("run-c", [record(pmid="9", pmcid=None, doi=None, source_id=None)]),
                _run("run-d", [MetadataRecord(source=SOURCE_PUBMED)]),
            ]
        )
        counts = dict(report.classification_counts)
        self.assertEqual(counts[CLASS_IDENTIFIER_OVERLAP], 1)
        self.assertEqual(counts[CLASS_UNMATCHED], 1)
        self.assertEqual(counts[CLASS_MISSING_IDENTIFIERS], 1)
        self.assertEqual(counts[CLASS_IDENTIFIER_CONFLICT], 0)
        self.assertEqual(report.total_occurrences, 4)
        self.assertEqual(report.sources, (SOURCE_PUBMED,))

    def test_occurrence_retains_exact_record(self):
        original = record(pmid="1", pmcid=None, doi=None, title="Exact title")
        report = reconcile_searches([_run("run-a", [original])])
        occ = report.groups[0].occurrences[0]
        self.assertIsInstance(occ, Occurrence)
        self.assertEqual(occ.record, original)
        self.assertEqual(occ.key, ("run-a", 0))

    def test_duplicate_metadata_within_run_is_preserved(self):
        identical = record(pmid="7", pmcid=None, doi=None)
        report = reconcile_searches([_run("run-a", [identical, identical])])
        self.assertEqual(report.total_occurrences, 2)
        self.assertEqual(report.group_count, 1)
        self.assertEqual(len(report.groups[0].occurrences), 2)


class PermutationStabilityTests(unittest.TestCase):
    def _runs(self):
        return [
            _run("run-b", [record(pmid="1", pmcid=None, doi=None, source_id=None)]),
            _run("run-a", [record(pmid="1", pmcid=None, doi=None, source_id=None)]),
            _run("run-c", [MetadataRecord(source=SOURCE_PUBMED)]),
            _run("run-d", [record(pmid="5", pmcid=None, doi=None, source_id=None)]),
        ]

    def test_report_is_identical_for_any_input_order(self):
        runs = self._runs()
        forward = reconcile_searches(runs)
        backward = reconcile_searches(list(reversed(runs)))
        shuffled = reconcile_searches([runs[2], runs[0], runs[3], runs[1]])
        self.assertEqual(forward, backward)
        self.assertEqual(forward, shuffled)
        # Run ids sorted; group order derived from them deterministically.
        self.assertEqual([run.run_id for run in forward.runs], ["run-a", "run-b", "run-c", "run-d"])
        self.assertEqual(
            [g.group_id for g in forward.groups], [g.group_id for g in backward.groups]
        )

    def test_group_ids_do_not_depend_on_input_order(self):
        runs = self._runs()
        baseline = reconcile_searches(runs)
        other = reconcile_searches(list(reversed(runs)))
        for left, right in zip(baseline.groups, other.groups):
            self.assertEqual(left.group_id, right.group_id)
            self.assertEqual(left.classification, right.classification)
            self.assertEqual(
                [occ.key for occ in left.occurrences],
                [occ.key for occ in right.occurrences],
            )


# --------------------------------------------------------------------------- #
# Bounds and scale
# --------------------------------------------------------------------------- #
class BoundAndScaleTests(unittest.TestCase):
    def test_occurrence_bound_is_an_explicit_refusal(self):
        with mock.patch.object(reconciliation, "MAX_TOTAL_OCCURRENCES", 2):
            runs = [
                _run("run-a", [record(pmid="1", pmcid=None, doi=None)]),
                _run("run-b", [record(pmid="2", pmcid=None, doi=None)]),
                _run("run-c", [record(pmid="3", pmcid=None, doi=None)]),
            ]
            with self.assertRaises(DiscoveryReconciliationError) as ctx:
                reconcile_searches(runs)
            self.assertEqual(
                str(ctx.exception),
                "reconciliation input exceeds the supported occurrence bound",
            )

    def test_occurrence_bound_is_inclusive_at_the_limit(self):
        with mock.patch.object(reconciliation, "MAX_TOTAL_OCCURRENCES", 2):
            runs = [
                _run("run-a", [record(pmid="1", pmcid=None, doi=None)]),
                _run("run-b", [record(pmid="2", pmcid=None, doi=None)]),
            ]
            report = reconcile_searches(runs)
            self.assertEqual(report.total_occurrences, 2)

    def test_documented_bound_is_generous(self):
        self.assertGreaterEqual(MAX_TOTAL_OCCURRENCES, 10_000)

    @slow
    def test_ten_thousand_records_scale(self):
        records = []
        for index in range(5_000):
            pmid = str(index)
            records.append(record(pmid=pmid, pmcid=None, doi=None, source_id=None))
            records.append(record(pmid=pmid, pmcid=None, doi=None, source_id=None))
        run = _run("run-scale", records)

        started = time.monotonic()
        report = reconcile_searches([run])
        elapsed = time.monotonic() - started

        self.assertEqual(report.total_occurrences, 10_000)
        self.assertEqual(report.group_count, 5_000)
        self.assertEqual(report.count(CLASS_IDENTIFIER_OVERLAP), 5_000)
        self.assertEqual(report.count(CLASS_IDENTIFIER_CONFLICT), 0)
        # Generous ceiling: the point is that it is not an O(n^2) blow-up.
        self.assertLess(elapsed, 60.0)


if __name__ == "__main__":
    unittest.main()
