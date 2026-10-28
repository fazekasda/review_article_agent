"""Deterministic tests for the immutable discovery value contracts (Step 05 slice 1).

Covers ``src/review_article_agent/discovery/records.py`` against the task T1
acceptance criteria:

* :class:`MetadataRecord` is frozen, normalizes nullable bibliographic fields,
  preserves provider provenance, and keeps a missing identifier explicit;
* publication identity is never equated with study identity and no ID is invented;
* :class:`SearchPage`/`SearchResult` are immutable and carry exact
  query/source/timestamps/counts/pages/records plus failure/stop_reason;
* a budget or page limit is **never** ``complete``.

Run:

    PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_discovery_*.py'
"""

from __future__ import annotations

import dataclasses
import sys
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from review_article_agent.discovery import records  # noqa: E402
from review_article_agent.discovery.records import (  # noqa: E402
    DiscoveryRecordError,
    MetadataRecord,
    SearchPage,
    SearchResult,
)


def record(**overrides) -> MetadataRecord:
    kwargs = dict(
        source=records.SOURCE_PUBMED,
        source_id="12345678",
        pmid="12345678",
        pmcid="PMC1234567",
        doi="10.1000/xyz",
        title="A study of things",
        publication_year=2020,
    )
    kwargs.update(overrides)
    return MetadataRecord(**kwargs)


# --------------------------------------------------------------------------- #
# MetadataRecord
# --------------------------------------------------------------------------- #
class MetadataRecordTests(unittest.TestCase):
    def test_full_record_round_trips_fields(self):
        rec = record()
        self.assertEqual(rec.source, "pubmed")
        self.assertEqual(rec.pmid, "12345678")
        self.assertEqual(rec.doi, "10.1000/xyz")
        self.assertEqual(rec.publication_year, 2020)
        self.assertTrue(rec.has_identifier())
        self.assertFalse(rec.is_missing_identifier())

    def test_nullable_fields_default_to_none(self):
        rec = MetadataRecord(source=records.SOURCE_EUROPEPMC)
        self.assertIsNone(rec.source_id)
        self.assertIsNone(rec.pmid)
        self.assertIsNone(rec.pmcid)
        self.assertIsNone(rec.doi)
        self.assertIsNone(rec.title)
        self.assertIsNone(rec.publication_year)

    def test_missing_identifier_is_explicit(self):
        rec = MetadataRecord(source=records.SOURCE_PUBMED, title="No ids here")
        self.assertFalse(rec.has_identifier())
        self.assertTrue(rec.is_missing_identifier())

    def test_frozen(self):
        rec = record()
        with self.assertRaises(dataclasses.FrozenInstanceError):
            rec.title = "changed"  # type: ignore[misc]

    def test_invalid_source_is_refused(self):
        for source in ("", "has space", "a/b", "a\\b"):
            with self.subTest(source=source):
                with self.assertRaises(DiscoveryRecordError):
                    MetadataRecord(source=source)

    def test_invalid_identifiers_are_refused(self):
        for field_name in ("source_id", "pmid", "pmcid", "doi"):
            with self.subTest(field=field_name):
                with self.assertRaises(DiscoveryRecordError):
                    record(**{field_name: ""})
                with self.assertRaises(DiscoveryRecordError):
                    record(**{field_name: "has space"})

    def test_invalid_year_is_refused(self):
        for year in (True, 0, 999, 10000, "2020", 2020.0):
            with self.subTest(year=year):
                with self.assertRaises(DiscoveryRecordError):
                    record(publication_year=year)

    def test_invalid_title_is_refused(self):
        with self.assertRaises(DiscoveryRecordError):
            record(title="")
        with self.assertRaises(DiscoveryRecordError):
            record(title="x" * (records.MAX_TITLE_LEN + 1))


# --------------------------------------------------------------------------- #
# SearchPage
# --------------------------------------------------------------------------- #
class SearchPageTests(unittest.TestCase):
    def page(self, **overrides) -> SearchPage:
        kwargs = dict(
            source=records.SOURCE_PUBMED,
            query="cancer therapy",
            requested_url="https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?term=cancer",
            retrieved_at="2026-10-28T00:00:00+00:00",
            page_index=0,
            records=(record(),),
            page_size=20,
            total_count=1,
            complete=True,
            stop_reason="completed",
        )
        kwargs.update(overrides)
        return SearchPage(**kwargs)

    def test_valid_page(self):
        page = self.page()
        self.assertTrue(page.complete)
        self.assertEqual(page.page_index, 0)
        self.assertEqual(page.total_count, 1)
        self.assertTrue(page.has_identifier_coverage())

    def test_page_is_frozen(self):
        page = self.page()
        with self.assertRaises(dataclasses.FrozenInstanceError):
            page.complete = False  # type: ignore[misc]

    def test_failed_page_cannot_be_complete(self):
        with self.assertRaises(DiscoveryRecordError):
            self.page(complete=True, failure="network_error", stop_reason="failure")

    def test_unknown_failure_and_stop_reason_are_refused(self):
        with self.assertRaises(DiscoveryRecordError):
            self.page(failure="mystery")
        with self.assertRaises(DiscoveryRecordError):
            self.page(stop_reason="mystery")

    def test_records_must_be_metadata_records(self):
        with self.assertRaises(DiscoveryRecordError):
            self.page(records=("not a record",))

    def test_page_index_and_counts_are_bounded(self):
        with self.assertRaises(DiscoveryRecordError):
            self.page(page_index=-1)
        with self.assertRaises(DiscoveryRecordError):
            self.page(page_size=0)
        with self.assertRaises(DiscoveryRecordError):
            self.page(total_count=-1)


# --------------------------------------------------------------------------- #
# SearchResult
# --------------------------------------------------------------------------- #
class SearchResultTests(unittest.TestCase):
    def pages(self):
        return (
            SearchPage(
                source=records.SOURCE_PUBMED,
                query="q",
                requested_url="https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?term=q",
                retrieved_at="2026-10-28T00:00:00+00:00",
                page_index=0,
                records=(record(source_id="1", pmid="1"),),
                complete=True,
                stop_reason="completed",
            ),
            SearchPage(
                source=records.SOURCE_PUBMED,
                query="q",
                requested_url="https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?term=q&retstart=1",
                retrieved_at="2026-10-28T00:00:01+00:00",
                page_index=1,
                records=(record(source_id="2", pmid="2"),),
                complete=True,
                stop_reason="completed",
            ),
        )

    def test_from_pages_flattens_records_and_marks_complete(self):
        result = SearchResult.from_pages(
            source=records.SOURCE_PUBMED,
            query="q",
            pages=self.pages(),
            started_at="2026-10-28T00:00:00+00:00",
            completed_at="2026-10-28T00:00:02+00:00",
            stop_reason="completed",
        )
        self.assertTrue(result.complete)
        self.assertEqual(len(result.records), 2)
        self.assertEqual([r.pmid for r in result.records], ["1", "2"])

    def test_budget_limit_is_never_complete(self):
        result = SearchResult.from_pages(
            source=records.SOURCE_PUBMED,
            query="q",
            pages=self.pages(),
            started_at="2026-10-28T00:00:00+00:00",
            completed_at="2026-10-28T00:00:02+00:00",
            stop_reason="budget_exhausted",
        )
        self.assertFalse(result.complete)

    def test_page_limit_is_never_complete(self):
        result = SearchResult.from_pages(
            source=records.SOURCE_PUBMED,
            query="q",
            pages=self.pages(),
            started_at="2026-10-28T00:00:00+00:00",
            completed_at="2026-10-28T00:00:02+00:00",
            stop_reason="page_limit",
        )
        self.assertFalse(result.complete)

    def test_failure_requires_failure_stop_reason(self):
        with self.assertRaises(DiscoveryRecordError):
            SearchResult(
                source=records.SOURCE_PUBMED,
                query="q",
                pages=self.pages(),
                records=tuple(r for p in self.pages() for r in p.records),
                started_at="2026-10-28T00:00:00+00:00",
                completed_at="2026-10-28T00:00:02+00:00",
                stop_reason="completed",
                complete=True,
                failure="network_error",
            )

    def test_complete_flag_must_match_stop_reason(self):
        with self.assertRaises(DiscoveryRecordError):
            SearchResult(
                source=records.SOURCE_PUBMED,
                query="q",
                pages=self.pages(),
                records=tuple(r for p in self.pages() for r in p.records),
                started_at="2026-10-28T00:00:00+00:00",
                completed_at="2026-10-28T00:00:02+00:00",
                stop_reason="budget_exhausted",
                complete=True,
            )

    def test_records_must_equal_flattened_pages(self):
        with self.assertRaises(DiscoveryRecordError):
            SearchResult(
                source=records.SOURCE_PUBMED,
                query="q",
                pages=self.pages(),
                records=(record(source_id="9", pmid="9"),),
                started_at="2026-10-28T00:00:00+00:00",
                completed_at="2026-10-28T00:00:02+00:00",
                stop_reason="completed",
                complete=True,
            )

    def test_result_is_frozen(self):
        result = SearchResult.from_pages(
            source=records.SOURCE_PUBMED,
            query="q",
            pages=self.pages(),
            started_at="2026-10-28T00:00:00+00:00",
            completed_at="2026-10-28T00:00:02+00:00",
            stop_reason="completed",
        )
        with self.assertRaises(dataclasses.FrozenInstanceError):
            result.complete = False  # type: ignore[misc]


if __name__ == "__main__":
    unittest.main()
