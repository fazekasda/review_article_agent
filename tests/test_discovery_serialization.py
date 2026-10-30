"""Deterministic tests for the discovery search-result codec (Step 05, T1).

Covers ``src/review_article_agent/discovery/serialization.py`` against the T1
acceptance criteria:

* versioned, canonical, lossless roundtrips of :class:`SearchResult` and the
  exact :class:`PubMedSearchResult` subtype;
* exact queries/timestamps/sources/pages/records/attempts/counts/failures,
  nullable IDs, overlapping duplicates, no-page results, and
  ``query_translation`` all survive without re-fetching or manufacturing data;
* ``retry_after_seconds`` keeps its original ``int``/``float`` type;
* malformed/corrupt/unsupported-version/extra-field/duplicate-key/type-confusion/
  non-finite/over-deep/oversize inputs are refused with static value-free errors;
* decoded values remain immutable and are rebuilt through the real dataclass
  invariants (a payload cannot smuggle an inconsistent aggregate).

No network, no credentials, no persistence. Run:

    PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_discovery_*.py'
"""

from __future__ import annotations

import dataclasses
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from review_article_agent.discovery import serialization  # noqa: E402
from review_article_agent.discovery.pubmed import PubMedSearchResult  # noqa: E402
from review_article_agent.discovery.records import (  # noqa: E402
    SOURCE_EUROPEPMC,
    SOURCE_PUBMED,
    MetadataRecord,
    SearchPage,
    SearchResult,
)
from review_article_agent.discovery.serialization import (  # noqa: E402
    MAX_SEARCH_RESULT_BYTES,
    SEARCH_RESULT_FORMAT,
    SEARCH_RESULT_FORMAT_VERSION,
    DiscoverySerializationError,
    decode_search_result,
    encode_search_result,
)
from review_article_agent.discovery.transport import AttemptRecord  # noqa: E402
from review_article_agent.domain.records import canonical_json  # noqa: E402


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


def attempt(**overrides) -> AttemptRecord:
    kwargs = dict(
        attempt_no=1,
        requested_url="https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?term=x",
        requested_at="2026-10-28T00:00:00+00:00",
        completed_at="2026-10-28T00:00:01+00:00",
        status=200,
        body_bytes=42,
        body_sha256="a" * 64,
        outcome="ok",
        retry_after_seconds=None,
    )
    kwargs.update(overrides)
    return AttemptRecord(**kwargs)


def page(**overrides) -> SearchPage:
    kwargs = dict(
        source=SOURCE_PUBMED,
        query="cancer therapy",
        requested_url="https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?term=cancer",
        retrieved_at="2026-10-28T00:00:02+00:00",
        page_index=0,
        records=(record(),),
        page_size=20,
        total_count=1,
        complete=True,
        stop_reason="completed",
        attempts=(attempt(),),
    )
    kwargs.update(overrides)
    return SearchPage(**kwargs)


def result(pages=None, *, source=SOURCE_PUBMED, query="cancer therapy", **overrides):
    kwargs = dict(
        source=source,
        query=query,
        pages=tuple(pages) if pages is not None else (page(),),
        started_at="2026-10-28T00:00:00+00:00",
        completed_at="2026-10-28T00:00:05+00:00",
        stop_reason="completed",
    )
    kwargs.update(overrides)
    return SearchResult.from_pages(**kwargs)


def pubmed_result(pages=None, *, query_translation="cancer therapy", **overrides):
    kwargs = dict(
        source=SOURCE_PUBMED,
        query="cancer therapy",
        pages=tuple(pages) if pages is not None else (page(),),
        started_at="2026-10-28T00:00:00+00:00",
        completed_at="2026-10-28T00:00:05+00:00",
        stop_reason="completed",
        query_translation=query_translation,
    )
    kwargs.update(overrides)
    return PubMedSearchResult.from_pages(**kwargs)


def envelope_bytes(encoded: bytes) -> dict:
    return json.loads(encoded.decode("utf-8"))


def encode_mapping(mapping: dict) -> bytes:
    return json.dumps(mapping, separators=(",", ":")).encode("utf-8")


# --------------------------------------------------------------------------- #
# Roundtrips
# --------------------------------------------------------------------------- #
class RoundTripTests(unittest.TestCase):
    def test_search_result_roundtrip_preserves_pages_overlap_and_missing_ids(self):
        overlap = record(source=SOURCE_EUROPEPMC, source_id="MED:1", pmid="1")
        missing = MetadataRecord(source=SOURCE_EUROPEPMC, title="No identifiers")
        third = record(
            source=SOURCE_EUROPEPMC,
            source_id="PMC:3",
            pmid=None,
            pmcid="PMC3",
            doi=None,
            publication_year=None,
        )
        first = page(
            source=SOURCE_EUROPEPMC,
            query="cancer AND therapy",
            requested_url="https://www.ebi.ac.uk/europepmc/webservices/rest/search?query=cancer",
            page_index=0,
            records=(overlap, missing),
            page_size=100,
            total_count=3,
            next_cursor="cursor-1",
            attempts=(
                attempt(retry_after_seconds=1),
                attempt(attempt_no=2, retry_after_seconds=2.5),
            ),
        )
        second = page(
            source=SOURCE_EUROPEPMC,
            query="cancer AND therapy",
            requested_url=(
                "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
                "?query=cancer&cursor=cursor-1"
            ),
            retrieved_at="2026-10-28T00:00:03+00:00",
            page_index=1,
            records=(overlap, third),
            page_size=100,
            total_count=3,
            complete=False,
            stop_reason="budget_exhausted",
            attempts=(attempt(attempt_no=3),),
        )
        original = result(
            (first, second),
            source=SOURCE_EUROPEPMC,
            query="cancer AND therapy",
            stop_reason="budget_exhausted",
        )

        decoded = decode_search_result(encode_search_result(original))

        self.assertIs(type(decoded), SearchResult)
        self.assertEqual(decoded, original)
        self.assertEqual(decoded.records, (overlap, missing, overlap, third))
        self.assertEqual(decoded.attempts, original.attempts)
        self.assertTrue(decoded.records[1].is_missing_identifier())
        self.assertFalse(decoded.complete)
        self.assertEqual(decoded.stop_reason, "budget_exhausted")
        # Overlap is retained, never silently deduplicated.
        self.assertEqual(len(decoded.records), 4)
        # int/float distinction survives.
        self.assertIs(type(decoded.attempts[0].retry_after_seconds), int)
        self.assertIs(type(decoded.attempts[1].retry_after_seconds), float)
        self.assertIsInstance(decoded.pages, tuple)
        self.assertIsInstance(decoded.pages[0].records, tuple)
        self.assertIsInstance(decoded.pages[0].attempts, tuple)

    def test_pubmed_roundtrip_preserves_exact_subtype_and_translation(self):
        translation = "cancer therapy[Title/Abstract] OR neoplasms[MeSH Terms]"
        original = pubmed_result(query_translation=translation)

        encoded = encode_search_result(original)
        decoded = decode_search_result(encoded)

        self.assertIs(type(decoded), PubMedSearchResult)
        self.assertEqual(decoded, original)
        self.assertEqual(decoded.query_translation, translation)
        self.assertEqual(decoded.query, original.query)
        self.assertEqual(decoded.attempts, original.attempts)
        self.assertEqual(decode_search_result(encoded), decoded)

    def test_pubmed_none_translation_roundtrips(self):
        original = pubmed_result(query_translation=None)
        decoded = decode_search_result(encode_search_result(original))
        self.assertIs(type(decoded), PubMedSearchResult)
        self.assertIsNone(decoded.query_translation)

    def test_no_pages_result_roundtrips(self):
        original = SearchResult(
            source=SOURCE_PUBMED,
            query="nothing found",
            pages=(),
            records=(),
            started_at="2026-10-28T00:00:00+00:00",
            completed_at="2026-10-28T00:00:01+00:00",
            stop_reason="completed",
            total_count=0,
            complete=True,
            attempts=(),
        )
        decoded = decode_search_result(encode_search_result(original))
        self.assertEqual(decoded, original)
        self.assertEqual(decoded.pages, ())
        self.assertEqual(decoded.records, ())

    def test_partial_failure_roundtrips(self):
        ok_page = page(page_index=0)
        failed_page = page(
            page_index=1,
            records=(),
            total_count=1,
            complete=False,
            stop_reason="failure",
            failure="http_status",
            attempts=(
                attempt(
                    attempt_no=2,
                    status=503,
                    body_bytes=0,
                    body_sha256=None,
                    outcome="retryable",
                    retry_after_seconds=3,
                ),
            ),
        )
        original = result((ok_page, failed_page), stop_reason="failure", failure="http_status")

        decoded = decode_search_result(encode_search_result(original))

        self.assertEqual(decoded, original)
        self.assertFalse(decoded.complete)
        self.assertEqual(decoded.failure, "http_status")
        self.assertEqual(decoded.pages[1].failure, "http_status")
        self.assertEqual(decoded.pages[1].stop_reason, "failure")
        self.assertEqual(decoded.pages[1].attempts[0].status, 503)

    def test_duplicate_records_and_nullable_fields_roundtrip(self):
        same = record()
        original = result(
            (
                page(records=(same, same), total_count=2),
                page(
                    page_index=1,
                    records=(same,),
                    total_count=2,
                ),
            )
        )
        decoded = decode_search_result(encode_search_result(original))
        self.assertEqual(decoded.records, (same, same, same))
        self.assertEqual(decoded, original)

    def test_decoded_values_are_frozen(self):
        decoded = decode_search_result(encode_search_result(result()))
        with self.assertRaises(dataclasses.FrozenInstanceError):
            decoded.query = "changed"  # type: ignore[misc]
        with self.assertRaises(dataclasses.FrozenInstanceError):
            decoded.pages[0].records[0].title = "changed"  # type: ignore[misc]
        with self.assertRaises(AttributeError):
            decoded.pages[0].records += ()  # type: ignore[misc]

    def test_exact_unicode_query_and_timestamp_roundtrip(self):
        query = 'café AND "tumor\nsuppression" 😀'
        title = "Über die Therapie — 日本語 😀"
        original = result(
            pages=(
                page(
                    query=query,
                    records=(record(title=title),),
                    requested_url=(
                        "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
                        "?term=caf%C3%A9"
                    ),
                    retrieved_at="2026-10-28T00:00:02.123456+00:00",
                ),
            ),
            query=query,
            started_at="2026-10-28T00:00:00.000001+00:00",
            completed_at="2026-10-28T00:00:05.999999+00:00",
        )
        decoded = decode_search_result(encode_search_result(original))
        self.assertEqual(decoded.query, query)
        self.assertEqual(decoded.pages[0].records[0].title, title)
        self.assertEqual(decoded.started_at, "2026-10-28T00:00:00.000001+00:00")
        self.assertEqual(decoded.completed_at, "2026-10-28T00:00:05.999999+00:00")
        self.assertEqual(decoded, original)

    def test_canonical_deterministic_and_versioned(self):
        original = result()
        first = encode_search_result(original)
        second = encode_search_result(original)
        decoded = decode_search_result(first)

        self.assertEqual(first, second)
        self.assertIsInstance(first, bytes)
        self.assertEqual(json.loads(first.decode("utf-8")), json.loads(first.decode("utf-8")))
        self.assertEqual(canonical_json(json.loads(first.decode("utf-8"))), first)
        self.assertEqual(encode_search_result(decoded), first)

        envelope = envelope_bytes(first)
        self.assertEqual(set(envelope), {"format", "format_version", "result_type", "result"})
        self.assertEqual(envelope["format"], SEARCH_RESULT_FORMAT)
        # A result that carries no authors/journal stays on the oldest supported
        # format version, so a previously stored observation is byte-identical.
        self.assertEqual(envelope["format_version"], 1)
        self.assertEqual(envelope["result_type"], "search_result")
        self.assertGreaterEqual(SEARCH_RESULT_FORMAT_VERSION, 2)

    def test_bound_is_at_least_one_transport_response(self):
        from review_article_agent.discovery.transport import MAX_RESPONSE_BYTES

        self.assertGreater(MAX_SEARCH_RESULT_BYTES, MAX_RESPONSE_BYTES)


# --------------------------------------------------------------------------- #
# Unsupported version / type / format
# --------------------------------------------------------------------------- #
class EnvelopeValidationTests(unittest.TestCase):
    def test_unsupported_version_refused(self):
        env = envelope_bytes(encode_search_result(result()))
        env["format_version"] = SEARCH_RESULT_FORMAT_VERSION + 1
        with self.assertRaises(DiscoverySerializationError):
            decode_search_result(encode_mapping(env))

    def test_unsupported_format_tag_refused(self):
        env = envelope_bytes(encode_search_result(result()))
        env["format"] = "review_article_agent.something_else"
        with self.assertRaises(DiscoverySerializationError):
            decode_search_result(encode_mapping(env))

    def test_unsupported_result_type_refused(self):
        env = envelope_bytes(encode_search_result(result()))
        env["result_type"] = "other_result"
        with self.assertRaises(DiscoverySerializationError):
            decode_search_result(encode_mapping(env))

    def test_pubmed_tag_requires_translation_field(self):
        env = envelope_bytes(encode_search_result(result()))
        env["result_type"] = "pubmed_search_result"
        with self.assertRaises(DiscoverySerializationError):
            decode_search_result(encode_mapping(env))


# --------------------------------------------------------------------------- #
# Unknown / missing fields at every level
# --------------------------------------------------------------------------- #
class FieldSetTests(unittest.TestCase):
    def _assert_extra_rejected(self, mutate):
        env = envelope_bytes(encode_search_result(pubmed_result()))
        mutate(env)
        with self.assertRaises(DiscoverySerializationError):
            decode_search_result(encode_mapping(env))

    def test_extra_envelope_field(self):
        self._assert_extra_rejected(lambda env: env.update({"extra": 1}))

    def test_extra_result_field(self):
        self._assert_extra_rejected(lambda env: env["result"].update({"extra": 1}))

    def test_extra_page_field(self):
        self._assert_extra_rejected(lambda env: env["result"]["pages"][0].update({"extra": 1}))

    def test_extra_record_field(self):
        self._assert_extra_rejected(
            lambda env: env["result"]["pages"][0]["records"][0].update({"extra": 1})
        )

    def test_extra_attempt_field(self):
        self._assert_extra_rejected(
            lambda env: env["result"]["pages"][0]["attempts"][0].update({"extra": 1})
        )

    def test_missing_result_field(self):
        self._assert_extra_rejected(lambda env: env["result"].pop("complete"))

    def test_missing_page_field(self):
        self._assert_extra_rejected(lambda env: env["result"]["pages"][0].pop("attempts"))

    def test_missing_record_field(self):
        self._assert_extra_rejected(
            lambda env: env["result"]["pages"][0]["records"][0].pop("title")
        )

    def test_missing_attempt_field(self):
        self._assert_extra_rejected(
            lambda env: env["result"]["pages"][0]["attempts"][0].pop("outcome")
        )


# --------------------------------------------------------------------------- #
# Strict JSON
# --------------------------------------------------------------------------- #
class StrictJsonTests(unittest.TestCase):
    def test_duplicate_key_refused(self):
        raw = (
            '{"format":"%s","format":"%s","format_version":1,'
            '"result_type":"search_result","result":{}}'
            % (SEARCH_RESULT_FORMAT, SEARCH_RESULT_FORMAT)
        ).encode("utf-8")
        with self.assertRaises(DiscoverySerializationError):
            decode_search_result(raw)

    def test_invalid_utf8_refused(self):
        with self.assertRaises(DiscoverySerializationError):
            decode_search_result(b"\xff\xfe")

    def test_malformed_json_refused(self):
        for raw in (b"", b"{", b"[]", b"null", b"{}", b'{"format":'):
            with self.subTest(raw=raw):
                with self.assertRaises(DiscoverySerializationError):
                    decode_search_result(raw)

    def test_non_bytes_refused(self):
        for value in ("{}", memoryview(b"{}"), 1, None):
            with self.subTest(value=type(value).__name__):
                with self.assertRaises(DiscoverySerializationError):
                    decode_search_result(value)  # type: ignore[arg-type]

    def test_nonfinite_numbers_refused(self):
        encoded = encode_search_result(result())
        for token in (b"NaN", b"Infinity", b"-Infinity"):
            raw = encoded.replace(
                b'"retry_after_seconds":null',
                b'"retry_after_seconds":' + token,
                1,
            )
            with self.subTest(token=token):
                with self.assertRaises(DiscoverySerializationError):
                    decode_search_result(raw)
        overflow = encoded.replace(
            b'"retry_after_seconds":null', b'"retry_after_seconds":1e999', 1
        )
        with self.assertRaises(DiscoverySerializationError):
            decode_search_result(overflow)

    def test_overdeep_json_refused(self):
        raw = (
            b'{"format":"x","format_version":1,"result_type":"search_result","result":'
            + b"[" * 20
            + b"0"
            + b"]" * 20
            + b"}"
        )
        with self.assertRaises(DiscoverySerializationError):
            decode_search_result(raw)

    def test_lone_unicode_surrogate_refused(self):
        env = envelope_bytes(encode_search_result(result()))
        env["result"]["query"] = "\ud800"
        # ``json.dumps`` escapes the lone surrogate, so the parser can construct
        # it; the codec refuses to return an unencodable string.
        raw = json.dumps(env, separators=(",", ":")).encode("utf-8")
        with self.assertRaises(DiscoverySerializationError):
            decode_search_result(raw)

    def test_huge_integer_retry_after_refused(self):
        encoded = encode_search_result(result())
        raw = encoded.replace(
            b'"retry_after_seconds":null',
            b'"retry_after_seconds":1' + b"0" * 400,
            1,
        )
        with self.assertRaises(DiscoverySerializationError):
            decode_search_result(raw)

    def test_oversize_payload_refused_before_parse(self):
        with mock.patch.object(serialization, "MAX_SEARCH_RESULT_BYTES", 64):
            with self.assertRaises(DiscoverySerializationError):
                decode_search_result(b"x" * 65)

    def test_oversize_encode_refused(self):
        with mock.patch.object(serialization, "MAX_SEARCH_RESULT_BYTES", 32):
            with self.assertRaises(DiscoverySerializationError):
                encode_search_result(result())


# --------------------------------------------------------------------------- #
# Type confusion and invariant enforcement
# --------------------------------------------------------------------------- #
class TypeAndInvariantTests(unittest.TestCase):
    def _mutate(self, mutate):
        env = envelope_bytes(encode_search_result(result()))
        mutate(env)
        return encode_mapping(env)

    def test_int_bool_and_float_confusion_refused(self):
        cases = (
            lambda env: env["result"].update({"complete": 1}),
            lambda env: env["result"].update({"complete": "true"}),
            lambda env: env["result"].update({"total_count": True}),
            lambda env: env["result"].update({"total_count": "1"}),
            lambda env: env["result"].update({"query": 7}),
            lambda env: env["result"]["pages"][0].update({"page_index": 1.0}),
            lambda env: env["result"]["pages"][0].update({"page_index": True}),
            lambda env: env["result"]["pages"][0]["records"][0].update(
                {"publication_year": "2020"}
            ),
            lambda env: env["result"]["pages"][0]["records"][0].update(
                {"publication_year": 2020.0}
            ),
            lambda env: env["result"]["pages"][0]["attempts"][0].update(
                {"attempt_no": 1.0}
            ),
            lambda env: env["result"]["pages"][0]["attempts"][0].update(
                {"retry_after_seconds": "1"}
            ),
            lambda env: env["result"]["pages"][0]["records"][0].update(
                {"source_id": 123}
            ),
            lambda env: env["result"].update({"pages": {}}),
            lambda env: env["result"].update({"records": [{}]}),
        )
        for index, mutate in enumerate(cases):
            with self.subTest(case=index):
                with self.assertRaises(DiscoverySerializationError):
                    decode_search_result(self._mutate(mutate))

    def test_aggregate_must_match_pages(self):
        raw = self._mutate(lambda env: env["result"].update({"records": []}))
        with self.assertRaises(DiscoverySerializationError):
            decode_search_result(raw)
        raw = self._mutate(lambda env: env["result"].update({"attempts": []}))
        with self.assertRaises(DiscoverySerializationError):
            decode_search_result(raw)

    def test_page_query_mismatch_refused(self):
        raw = self._mutate(lambda env: env["result"]["pages"][0].update({"query": "other"}))
        with self.assertRaises(DiscoverySerializationError):
            decode_search_result(raw)

    def test_page_source_mismatch_refused(self):
        raw = self._mutate(lambda env: env["result"]["pages"][0].update({"source": "europepmc"}))
        with self.assertRaises(DiscoverySerializationError):
            decode_search_result(raw)

    def test_complete_semantics_cannot_be_bypassed(self):
        raw = self._mutate(
            lambda env: (
                env["result"]["pages"][0].update({"complete": False}),
                env["result"].update({"complete": True}),
            )
        )
        with self.assertRaises(DiscoverySerializationError):
            decode_search_result(raw)

    def test_failure_stop_consistency_cannot_be_bypassed(self):
        raw = self._mutate(
            lambda env: env["result"].update({"failure": "http_status", "complete": False})
        )
        with self.assertRaises(DiscoverySerializationError):
            decode_search_result(raw)
        raw = self._mutate(
            lambda env: env["result"]["pages"][0].update({"failure": "http_status"})
        )
        with self.assertRaises(DiscoverySerializationError):
            decode_search_result(raw)

    def test_invalid_record_value_refused(self):
        raw = self._mutate(
            lambda env: env["result"]["pages"][0]["records"][0].update(
                {"publication_year": 999}
            )
        )
        with self.assertRaises(DiscoverySerializationError):
            decode_search_result(raw)
        raw = self._mutate(
            lambda env: env["result"]["pages"][0]["records"][0].update({"doi": "has space"})
        )
        with self.assertRaises(DiscoverySerializationError):
            decode_search_result(raw)


# --------------------------------------------------------------------------- #
# Encoder rejection (subclasses, invalid runtime values)
# --------------------------------------------------------------------------- #
class EncoderValidationTests(unittest.TestCase):
    def test_subclasses_are_refused(self):
        @dataclasses.dataclass(frozen=True)
        class SearchResultSubclass(SearchResult):
            pass

        subclass = SearchResultSubclass(
            source=SOURCE_PUBMED,
            query="q",
            pages=(),
            records=(),
            started_at="2026-10-28T00:00:00+00:00",
            completed_at="2026-10-28T00:00:01+00:00",
            stop_reason="completed",
            complete=True,
        )
        with self.assertRaises(DiscoverySerializationError):
            encode_search_result(subclass)

        @dataclasses.dataclass(frozen=True)
        class PageSubclass(SearchPage):
            pass

        bad_page = PageSubclass(
            source=SOURCE_PUBMED,
            query="q",
            requested_url="https://example.invalid/x",
            retrieved_at="2026-10-28T00:00:00+00:00",
            page_index=0,
            complete=True,
            stop_reason="completed",
        )
        bad_result = SearchResult(
            source=SOURCE_PUBMED,
            query="q",
            pages=(bad_page,),
            records=(),
            started_at="2026-10-28T00:00:00+00:00",
            completed_at="2026-10-28T00:00:01+00:00",
            stop_reason="completed",
            complete=True,
        )
        with self.assertRaises(DiscoverySerializationError):
            encode_search_result(bad_result)

    def test_bad_query_translation_type_refused(self):
        original = pubmed_result(query_translation=123)  # constructor does not validate
        with self.assertRaises(DiscoverySerializationError):
            encode_search_result(original)

    def test_nonfinite_retry_after_refused_on_encode(self):
        bad_attempt = attempt()
        object.__setattr__(bad_attempt, "retry_after_seconds", float("nan"))
        bad_page = page(attempts=(bad_attempt,))
        bad_result = SearchResult.from_pages(
            source=SOURCE_PUBMED,
            query="cancer therapy",
            pages=(bad_page,),
            started_at="2026-10-28T00:00:00+00:00",
            completed_at="2026-10-28T00:00:05+00:00",
            stop_reason="completed",
        )
        with self.assertRaises(DiscoverySerializationError):
            encode_search_result(bad_result)

    def test_non_tuple_containers_refused_on_encode(self):
        bad_page = page()
        object.__setattr__(bad_page, "records", list(bad_page.records))
        bad_result = SearchResult.from_pages(
            source=SOURCE_PUBMED,
            query="cancer therapy",
            pages=(bad_page,),
            started_at="2026-10-28T00:00:00+00:00",
            completed_at="2026-10-28T00:00:05+00:00",
            stop_reason="completed",
        )
        with self.assertRaises(DiscoverySerializationError):
            encode_search_result(bad_result)


# --------------------------------------------------------------------------- #
# Extended bibliographic fields (authors/journal) and version 2
# --------------------------------------------------------------------------- #
class ExtendedBibliographicTests(unittest.TestCase):
    def _extended_result(self):
        return result(
            pages=(
                page(
                    records=(
                        record(
                            authors=("Ada Lovelace", "Alan Turing"),
                            journal="Journal of Synthetic Studies",
                        ),
                    )
                ),
            )
        )

    def test_roundtrip_preserves_authors_and_journal(self):
        original = self._extended_result()
        encoded = encode_search_result(original)
        decoded = decode_search_result(encoded)
        self.assertEqual(decoded, original)
        self.assertEqual(
            decoded.records[0].authors, ("Ada Lovelace", "Alan Turing")
        )
        self.assertEqual(decoded.records[0].journal, "Journal of Synthetic Studies")
        self.assertEqual(encode_search_result(decoded), encoded)

    def test_extended_result_uses_version_two(self):
        encoded = encode_search_result(self._extended_result())
        env = envelope_bytes(encoded)
        self.assertEqual(env["format_version"], 2)
        self.assertEqual(SEARCH_RESULT_FORMAT_VERSION, 2)
        record_obj = env["result"]["pages"][0]["records"][0]
        self.assertEqual(
            set(record_obj),
            {
                "source",
                "source_id",
                "pmid",
                "pmcid",
                "doi",
                "title",
                "publication_year",
                "authors",
                "journal",
            },
        )
        self.assertEqual(record_obj["authors"], ["Ada Lovelace", "Alan Turing"])
        self.assertEqual(record_obj["journal"], "Journal of Synthetic Studies")

    def test_empty_authors_and_null_journal_stay_version_one(self):
        # Explicit absent values do not force version 2.
        plain = result(pages=(page(records=(record(authors=(), journal=None),)),))
        env = envelope_bytes(encode_search_result(plain))
        self.assertEqual(env["format_version"], 1)
        self.assertNotIn("authors", env["result"]["pages"][0]["records"][0])
        self.assertNotIn("journal", env["result"]["pages"][0]["records"][0])

    def test_old_version_one_bytes_decode_and_reencode_identically(self):
        # A version-1 observation (no authors/journal) predates the new fields;
        # its stored digest must not change.
        old = encode_search_result(result())
        self.assertEqual(envelope_bytes(old)["format_version"], 1)
        decoded = decode_search_result(old)
        self.assertEqual(decoded.records[0].authors, ())
        self.assertIsNone(decoded.records[0].journal)
        self.assertEqual(encode_search_result(decoded), old)

    def test_version_one_record_with_authors_key_is_refused(self):
        env = envelope_bytes(encode_search_result(result()))
        env["result"]["pages"][0]["records"][0]["authors"] = ["Ada"]
        with self.assertRaises(DiscoverySerializationError):
            decode_search_result(encode_mapping(env))

    def test_version_two_record_missing_authors_key_is_refused(self):
        env = envelope_bytes(encode_search_result(self._extended_result()))
        env["result"]["pages"][0]["records"][0].pop("authors")
        with self.assertRaises(DiscoverySerializationError):
            decode_search_result(encode_mapping(env))

    def test_authors_wrong_type_refused(self):
        env = envelope_bytes(encode_search_result(self._extended_result()))
        env["result"]["pages"][0]["records"][0]["authors"] = "Ada Lovelace"
        with self.assertRaises(DiscoverySerializationError):
            decode_search_result(encode_mapping(env))

    def test_hostile_authors_and_journal_refused(self):
        env = envelope_bytes(encode_search_result(self._extended_result()))
        record_obj = env["result"]["pages"][0]["records"][0]
        # A non-string entry, a control character, and an over-long name are all
        # refused rather than returned.
        record_obj["authors"] = ["Ada", 7]
        with self.assertRaises(DiscoverySerializationError):
            decode_search_result(encode_mapping(env))
        env = envelope_bytes(encode_search_result(self._extended_result()))
        env["result"]["pages"][0]["records"][0]["authors"] = ["Ada\u0000Lovelace"]
        with self.assertRaises(DiscoverySerializationError):
            decode_search_result(encode_mapping(env))
        env = envelope_bytes(encode_search_result(self._extended_result()))
        env["result"]["pages"][0]["records"][0]["journal"] = 42
        with self.assertRaises(DiscoverySerializationError):
            decode_search_result(encode_mapping(env))

    def test_non_tuple_authors_refused_on_encode(self):
        original = self._extended_result()
        bad_record = original.records[0]
        object.__setattr__(bad_record, "authors", ["Ada Lovelace"])
        with self.assertRaises(DiscoverySerializationError):
            encode_search_result(original)


# --------------------------------------------------------------------------- #
# Error hygiene
# --------------------------------------------------------------------------- #
class ErrorHygieneTests(unittest.TestCase):
    def test_decode_errors_do_not_echo_private_payload_text(self):
        secret = "PRIVATE-QUERY-AND-URL-0123456789"
        original = result(query=secret, pages=(page(query=secret),))
        env = envelope_bytes(encode_search_result(original))
        env["unknown_field_" + secret] = secret
        with self.assertRaises(DiscoverySerializationError) as caught:
            decode_search_result(encode_mapping(env))
        self.assertNotIn(secret, str(caught.exception))

        env = envelope_bytes(encode_search_result(original))
        env["result"]["total_count"] = secret
        with self.assertRaises(DiscoverySerializationError) as caught:
            decode_search_result(encode_mapping(env))
        self.assertNotIn(secret, str(caught.exception))

        raw = ('{"format":"' + secret).encode("utf-8")
        with self.assertRaises(DiscoverySerializationError) as caught:
            decode_search_result(raw)
        self.assertNotIn(secret, str(caught.exception))

    def test_encode_errors_do_not_echo_private_payload_text(self):
        secret = "PRIVATE-URL-0123456789"
        original = pubmed_result(query_translation=secret)
        object.__setattr__(original, "query_translation", object())
        with self.assertRaises(DiscoverySerializationError) as caught:
            encode_search_result(original)
        self.assertNotIn(secret, str(caught.exception))


if __name__ == "__main__":
    unittest.main()