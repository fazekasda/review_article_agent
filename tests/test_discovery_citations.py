"""Deterministic tests for the bounded Europe PMC citation adapter (Step 05).

Covers ``src/review_article_agent/discovery/citations.py`` and the narrow
Europe PMC citation path family added to
``src/review_article_agent/discovery/transport.py`` against
``docs/implementation/05-discovery.md`` D3 AC5 (forward/backward citation
expansion exposes explicit coverage gaps, never an absolute completeness claim)
and AC7 (no arbitrary API result cap truncation is hidden as complete retrieval):

* one explicit library call per page with ``format=json``, a 1-based ``page``, a
  bounded ``pageSize``, and the deterministic relation descriptor on the result;
* the shared immutable ``SearchResult`` retains the exact URLs/times/counts, the
  page index, and **every** transport attempt;
* ``references`` (backward) and ``citations`` (forward) both work;
* multi-page success, page cap, zero hits, unmatched/partial identities, a changed
  ``hitCount``, an empty short page, overcollection, repeated occurrences, and
  malformed bodies produce explicit outcomes; ``page_limit``/anomalies are never
  complete and only ``collected == hitCount`` is complete;
* invalid identities/bounds cause **zero** requests, construction performs no
  network work, and the citation path family refuses adversarial paths before a
  socket is opened.

No network, no credential, no paid call: the low-level transport, clock, and
sleep are always local fixtures. Run:

    PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_discovery_*.py'
"""

from __future__ import annotations

import hashlib
import json
import sys
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlsplit

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from review_article_agent.discovery import citations, records, transport  # noqa: E402

BASE = "https://www.ebi.ac.uk/europepmc/webservices/rest"


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
class FakeClock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


class FakeSleep:
    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self.calls: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)
        self.clock.advance(seconds)


class FakeUtcNow:
    """A deterministic, strictly increasing UTC clock."""

    def __init__(self) -> None:
        self.n = 0

    def __call__(self) -> str:
        self.n += 1
        return f"2026-10-28T00:00:{self.n:02d}+00:00"


class FakeTransport:
    """A scripted low-level transport: each item is a response or an exception."""

    def __init__(self, script: list[object]) -> None:
        self.script = list(script)
        self.calls: list[tuple[str, float, int]] = []

    def __call__(self, url, *, timeout, max_response_bytes):
        self.calls.append((url, timeout, max_response_bytes))
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def response(status: int, body: bytes = b"", headers=None, truncated: bool = False):
    return transport.TransportResponse(
        status=status, body=body, headers=headers or {}, truncated=truncated
    )


def json_response(payload) -> transport.TransportResponse:
    return response(200, json.dumps(payload).encode("utf-8"))


def ref_payload(hit_count, items, **extra) -> dict:
    """A synthetic Europe PMC ``referenceList`` payload."""
    payload = {
        "version": "6.9",
        "hitCount": hit_count,
        "referenceList": {"reference": list(items)},
    }
    payload.update(extra)
    return payload


def cit_payload(hit_count, items, **extra) -> dict:
    """A synthetic Europe PMC ``citationList`` payload."""
    payload = {
        "version": "6.9",
        "hitCount": hit_count,
        "citationList": {"citation": list(items)},
    }
    payload.update(extra)
    return payload


def entry(rec_id=None, source="MED", **extra) -> dict:
    """A synthetic Europe PMC reference/citation entry."""
    item: dict = {}
    if source is not None:
        item["source"] = source
    if rec_id is not None:
        item["id"] = rec_id
    item.update(extra)
    return item


def make_client(script, **overrides):
    """Build (citation client, low-level fake, sleep, client clock)."""
    clock = FakeClock()
    sleep = FakeSleep(clock)
    fake = FakeTransport(script)
    kwargs = dict(
        sleep=sleep,
        clock=clock,
        utcnow=FakeUtcNow(),
        transport=fake,
        rate_spacing=0.0,
    )
    kwargs.update(overrides)
    shared = transport.DiscoveryTransport(**kwargs)
    client_clock = FakeUtcNow()
    client = citations.EuropePMCCitationClient(shared, utcnow=client_clock)
    return client, fake, sleep, client_clock


def params_of(url: str) -> dict[str, list[str]]:
    return parse_qs(urlsplit(url).query)


# --------------------------------------------------------------------------- #
# Success, pagination, provenance
# --------------------------------------------------------------------------- #
class SuccessTests(unittest.TestCase):
    def test_multi_page_reference_pagination_and_provenance(self):
        script = [
            json_response(
                ref_payload(
                    3,
                    [
                        entry("111", title="First", pubYear="2001"),
                        entry("222", title="Second"),
                    ],
                )
            ),
            json_response(ref_payload(3, [entry("333", source="PMC")])),
        ]
        client, fake, _, _ = make_client(script)
        result = client.references("MED", "123", page_size=2, max_pages=5)

        self.assertTrue(result.complete)
        self.assertEqual(result.stop_reason, "completed")
        self.assertEqual(result.source, records.SOURCE_EUROPEPMC)
        self.assertEqual(result.query, "MED:123 references")
        self.assertEqual(result.total_count, 3)
        self.assertEqual(len(result.pages), 2)
        self.assertEqual([p.page_index for p in result.pages], [0, 1])
        self.assertEqual(
            [r.source_id for r in result.records], ["MED:111", "MED:222", "PMC:333"]
        )
        self.assertEqual(result.records[0].title, "First")
        self.assertEqual(result.records[0].publication_year, 2001)
        # Every page retains the exact requested URL, params, and UTC timestamp.
        self.assertEqual(
            [p.requested_url for p in result.pages],
            [
                f"{BASE}/MED/123/references?format=json&page=1&pageSize=2",
                f"{BASE}/MED/123/references?format=json&page=2&pageSize=2",
            ],
        )
        self.assertEqual(params_of(result.pages[1].requested_url)["page"], ["2"])
        self.assertTrue(all(p.complete for p in result.pages))
        # Every transport attempt is retained, and the flattened attempts match.
        self.assertEqual(len(result.attempts), 2)
        self.assertEqual(
            result.attempts, tuple(a for p in result.pages for a in p.attempts)
        )
        self.assertEqual([call[0] for call in fake.calls], [
            f"{BASE}/MED/123/references?format=json&page=1&pageSize=2",
            f"{BASE}/MED/123/references?format=json&page=2&pageSize=2",
        ])

    def test_default_page_size_is_the_bounded_api_default(self):
        client, fake, _, _ = make_client([json_response(ref_payload(0, []))])
        client.references("MED", "123")
        self.assertEqual(params_of(fake.calls[0][0])["pageSize"], ["25"])
        self.assertEqual(params_of(fake.calls[0][0])["page"], ["1"])

    def test_forward_citations_success(self):
        script = [
            json_response(
                cit_payload(2, [entry("900", source="MED"), entry("901", source="PPR")])
            )
        ]
        client, _, _, _ = make_client(script)
        result = client.citations("MED", "123", page_size=25, max_pages=5)
        self.assertTrue(result.complete)
        self.assertEqual(result.query, "MED:123 citations")
        self.assertEqual([r.source_id for r in result.records], ["MED:900", "PPR:901"])
        self.assertEqual(result.pages[0].requested_url, f"{BASE}/MED/123/citations"
                         "?format=json&page=1&pageSize=25")

    def test_backward_and_forward_use_distinct_relation_descriptors(self):
        backward, _, _, _ = make_client([json_response(ref_payload(0, []))])
        forward, _, _, _ = make_client([json_response(cit_payload(0, []))])
        self.assertEqual(backward.references("MED", "1").query, "MED:1 references")
        self.assertEqual(forward.citations("MED", "1").query, "MED:1 citations")

    def test_zero_hits_is_a_retrieval_end_not_a_coverage_claim(self):
        # ``hitCount == 0`` completes the bounded retrieval; it is NOT a claim that
        # the paper has no references in any scientific sense.
        client, _, _, _ = make_client([json_response(ref_payload(0, []))])
        result = client.references("MED", "123")
        self.assertTrue(result.complete)
        self.assertEqual(result.stop_reason, "completed")
        self.assertEqual(result.total_count, 0)
        self.assertEqual(result.records, ())
        self.assertEqual(len(result.pages), 1)

    def test_unmatched_entries_keep_metadata_and_no_fake_ids(self):
        # An unmatched reference carries match/citedOrder/title/pubYear but no
        # resolvable identity: metadata is kept, no pmid/pmcid/doi is invented.
        script = [
            json_response(
                ref_payload(
                    2,
                    [
                        entry(None, source=None, title="Unmatched A", pubYear=1999,
                              match="N", citedOrder=3),
                        entry(None, source=None, title="Unmatched B"),
                    ],
                )
            )
        ]
        client, _, _, _ = make_client(script)
        result = client.references("MED", "123", page_size=25)
        self.assertTrue(result.complete)
        self.assertEqual(len(result.records), 2)
        first = result.records[0]
        self.assertIsNone(first.source_id)
        self.assertIsNone(first.pmid)
        self.assertIsNone(first.pmcid)
        self.assertIsNone(first.doi)
        self.assertEqual(first.title, "Unmatched A")
        self.assertEqual(first.publication_year, 1999)
        self.assertTrue(first.is_missing_identifier())
        # Entries with no identity are never treated as repeats of one another.
        self.assertEqual(citations.repeated_identities(result.records), ())

    def test_partial_identity_does_not_fabricate_a_composed_id(self):
        script = [
            json_response(
                ref_payload(
                    2,
                    [
                        entry("42", source=None),  # id without a native source
                        entry(None, source="MED"),  # source without an id
                    ],
                )
            )
        ]
        client, _, _, _ = make_client(script)
        result = client.references("MED", "123")
        self.assertEqual([r.source_id for r in result.records], [None, None])

    def test_raw_body_digest_covers_extra_fields(self):
        # Extra provider fields (match/citedOrder/extraField) are not modelled but
        # are covered by the retained raw-body digest, so a later edge builder can
        # re-fetch and verify them without this adapter inventing fields.
        payload = ref_payload(
            1,
            [
                entry(
                    "111",
                    title="T",
                    pubYear="2010",
                    match="Y",
                    citedOrder=1,
                    extraField={"nested": [1, 2, 3]},
                )
            ],
        )
        body = json.dumps(payload).encode("utf-8")
        client, _, _, _ = make_client([response(200, body)])
        result = client.references("MED", "123")
        attempt = result.pages[0].attempts[0]
        self.assertEqual(attempt.body_sha256, hashlib.sha256(body).hexdigest())
        self.assertEqual(attempt.body_bytes, len(body))


# --------------------------------------------------------------------------- #
# Page budget
# --------------------------------------------------------------------------- #
class PageLimitTests(unittest.TestCase):
    def test_page_limit_is_explicit_and_never_complete(self):
        script = [
            json_response(ref_payload(6, [entry("1"), entry("2")])),
            json_response(ref_payload(6, [entry("3"), entry("4")])),
        ]
        client, fake, _, _ = make_client(script)
        result = client.references("MED", "123", page_size=2, max_pages=2)
        self.assertFalse(result.complete)
        self.assertEqual(result.stop_reason, "page_limit")
        self.assertIsNone(result.failure)
        self.assertEqual(len(result.pages), 2)
        self.assertTrue(all(p.complete for p in result.pages))
        self.assertEqual(len(fake.calls), 2)
        self.assertEqual(result.total_count, 6)
        # Only 4 of the declared total were collected under a 2-page budget cap.
        self.assertEqual(len(result.records), 4)


# --------------------------------------------------------------------------- #
# Anomalies -> explicit malformed_response, never complete
# --------------------------------------------------------------------------- #
class AnomalyTests(unittest.TestCase):
    def _assert_malformed(self, result, *, pages):
        self.assertFalse(result.complete)
        self.assertEqual(result.stop_reason, "failure")
        self.assertEqual(result.failure, "malformed_response")
        self.assertEqual(len(result.pages), pages)
        self.assertFalse(result.pages[-1].complete)

    def test_changed_hit_count_is_malformed_and_retains_observations(self):
        script = [
            json_response(ref_payload(3, [entry("1"), entry("2")])),
            json_response(ref_payload(4, [entry("3"), entry("4")])),
        ]
        client, _, _, _ = make_client(script)
        result = client.references("MED", "123", page_size=2, max_pages=5)
        self._assert_malformed(result, pages=2)
        # The offending page's parsed observations are retained (no silent loss).
        self.assertEqual([r.source_id for r in result.pages[1].records],
                         ["MED:3", "MED:4"])

    def test_short_empty_page_with_remaining_expected_is_malformed(self):
        script = [
            json_response(ref_payload(3, [entry("1"), entry("2")])),
            json_response(ref_payload(3, [])),
        ]
        client, _, _, _ = make_client(script)
        result = client.references("MED", "123", page_size=2, max_pages=5)
        self._assert_malformed(result, pages=2)
        self.assertEqual(len(result.records), 2)

    def test_overcollection_is_malformed(self):
        script = [json_response(ref_payload(1, [entry("1"), entry("2")]))]
        client, _, _, _ = make_client(script)
        result = client.references("MED", "123", page_size=25)
        self._assert_malformed(result, pages=1)

    def test_page_larger_than_requested_is_malformed(self):
        script = [json_response(ref_payload(2, [entry("1"), entry("2")]))]
        client, _, _, _ = make_client(script)
        result = client.references("MED", "123", page_size=1)
        self._assert_malformed(result, pages=1)

    def test_malformed_json_is_malformed(self):
        client, _, _, _ = make_client([response(200, b"{not json")])
        result = client.references("MED", "123")
        self._assert_malformed(result, pages=1)
        self.assertEqual(result.pages[0].records, ())
        self.assertEqual(len(result.pages[0].attempts), 1)

    def test_wrong_typed_body_and_fields_are_malformed(self):
        cases = [
            b"[]",  # top-level not an object
            b"null",
            json.dumps({"referenceList": {"reference": []}}).encode(),  # no hitCount
            json.dumps({"hitCount": -1, "referenceList": {}}).encode(),
            json.dumps({"hitCount": 1, "referenceList": []}).encode(),  # bad container
            json.dumps(
                {"hitCount": 1, "referenceList": {"reference": {"id": "1"}}}
            ).encode(),  # bad items type
            json.dumps(
                {"hitCount": 1, "referenceList": {"reference": ["not an object"]}}
            ).encode(),
        ]
        for body in cases:
            with self.subTest(body=body):
                client, _, _, _ = make_client([response(200, body)])
                result = client.references("MED", "123")
                self._assert_malformed(result, pages=1)

    def test_malformed_year_is_malformed(self):
        script = [json_response(ref_payload(1, [entry("1", pubYear="not-a-year")]))]
        client, _, _, _ = make_client(script)
        result = client.references("MED", "123")
        self._assert_malformed(result, pages=1)

    def test_citation_relation_wrong_list_is_malformed(self):
        # A citations request that receives a referenceList envelope is malformed.
        script = [json_response(ref_payload(1, [entry("1")]))]
        client, _, _, _ = make_client(script)
        result = client.citations("MED", "123")
        self._assert_malformed(result, pages=1)


# --------------------------------------------------------------------------- #
# Partial transport failure
# --------------------------------------------------------------------------- #
class PartialFailureTests(unittest.TestCase):
    def test_partial_transport_failure_retains_prior_page_and_attempts(self):
        script = [
            json_response(ref_payload(4, [entry("1"), entry("2")])),
            transport.TransportNetworkError("boom"),
            transport.TransportNetworkError("boom"),
            transport.TransportNetworkError("boom"),
        ]
        client, fake, _, _ = make_client(script)
        result = client.references("MED", "123", page_size=2, max_pages=5)

        self.assertFalse(result.complete)
        self.assertEqual(result.stop_reason, "failure")
        self.assertEqual(result.failure, "network_error")
        self.assertEqual(len(result.pages), 2)
        self.assertTrue(result.pages[0].complete)
        self.assertFalse(result.pages[1].complete)
        # The successful prior page's records survive; the failed page is empty.
        self.assertEqual([r.source_id for r in result.records], ["MED:1", "MED:2"])
        self.assertEqual(result.pages[1].records, ())
        # The failed page keeps the full attempt provenance (3 retries).
        failed_attempts = result.pages[1].attempts
        self.assertEqual(len(failed_attempts), 3)
        self.assertEqual(
            [a.outcome for a in failed_attempts], ["network_error"] * 3
        )
        self.assertEqual(len(result.attempts), 4)
        self.assertEqual(len(fake.calls), 4)


# --------------------------------------------------------------------------- #
# Duplicate-occurrence semantics
# --------------------------------------------------------------------------- #
class DuplicateTests(unittest.TestCase):
    def test_repeated_occurrence_within_a_page_is_retained_with_no_loss(self):
        script = [
            json_response(ref_payload(3, [entry("1"), entry("1")])),
            json_response(ref_payload(3, [entry("2")])),
        ]
        client, _, _, _ = make_client(script)
        result = client.references("MED", "123", page_size=2, max_pages=5)
        # A repeated reference is legitimate: retained in full, never deduplicated.
        self.assertTrue(result.complete)
        self.assertEqual([r.source_id for r in result.records],
                         ["MED:1", "MED:1", "MED:2"])
        # The repetition is exposed without any occurrence being lost.
        self.assertEqual(citations.repeated_identities(result.records), ("MED:1",))

    def test_repeated_occurrence_across_pages_is_retained(self):
        script = [
            json_response(ref_payload(3, [entry("1"), entry("2")])),
            json_response(ref_payload(3, [entry("1")])),
        ]
        client, _, _, _ = make_client(script)
        result = client.references("MED", "123", page_size=2, max_pages=5)
        self.assertTrue(result.complete)
        self.assertEqual([r.source_id for r in result.records],
                         ["MED:1", "MED:2", "MED:1"])
        self.assertEqual(citations.repeated_identities(result.records), ("MED:1",))

    def test_duplicate_overcollection_is_explicit_incomplete(self):
        # A duplicate that pushes the collected count past hitCount is inconsistent
        # and must be an explicit malformed response, never a silent success.
        script = [json_response(ref_payload(1, [entry("1"), entry("1")]))]
        client, _, _, _ = make_client(script)
        result = client.references("MED", "123")
        self.assertFalse(result.complete)
        self.assertEqual(result.failure, "malformed_response")


# --------------------------------------------------------------------------- #
# Argument validation and construction
# --------------------------------------------------------------------------- #
class BadArgumentTests(unittest.TestCase):
    def test_construction_performs_no_network(self):
        class ExplodingTransport:
            def __call__(self, *a, **k):  # pragma: no cover - must never run
                raise AssertionError("no request may be made")

        shared = transport.DiscoveryTransport(
            transport=ExplodingTransport(), rate_spacing=0.0
        )
        client = citations.EuropePMCCitationClient(shared)
        self.assertIs(client.transport, shared)

    def test_invalid_identity_or_bounds_make_zero_requests(self):
        cases = [
            dict(source="med", id="1"),          # lowercase native source
            dict(source="ME:D", id="1"),         # colon in source
            dict(source="", id="1"),
            dict(source=None, id="1"),
            dict(source=123, id="1"),
            dict(source="MED", id=""),
            dict(source="MED", id=None),
            dict(source="MED", id="a/b"),        # extra path segment
            dict(source="MED", id="a%2Fb"),      # percent-encoded
            dict(source="MED", id=".."),         # traversal
            dict(source="MED", id="1..2"),       # traversal
            dict(source="MED", id="1 2"),        # whitespace
            dict(source="MED", id="a?b"),
            dict(source="MED", id="a#b"),
            dict(source="M" * 65, id="1"),       # source too long
            dict(source="MED", id="x" * 257),    # id too long
            dict(source="MED", id="1", page_size=0),
            dict(source="MED", id="1", page_size=1001),
            dict(source="MED", id="1", page_size=True),
            dict(source="MED", id="1", page_size=1.5),
            dict(source="MED", id="1", max_pages=0),
            dict(source="MED", id="1", max_pages=1001),
            dict(source="MED", id="1", max_pages=True),
        ]
        for case in cases:
            with self.subTest(case=case):
                client, fake, _, _ = make_client([])
                for method in (client.references, client.citations):
                    with self.assertRaises(transport.DiscoveryValidationError):
                        method(**case)
                self.assertEqual(fake.calls, [])

    def test_invalid_transport_and_utcnow_are_refused(self):
        with self.assertRaises(transport.DiscoveryValidationError):
            citations.EuropePMCCitationClient(object())
        good = transport.DiscoveryTransport(rate_spacing=0.0)
        with self.assertRaises(transport.DiscoveryValidationError):
            citations.EuropePMCCitationClient(good, utcnow=object())


# --------------------------------------------------------------------------- #
# Narrow citation endpoint family (transport additions)
# --------------------------------------------------------------------------- #
class CitationEndpointTests(unittest.TestCase):
    def test_builder_and_identity_validation_table(self):
        self.assertEqual(
            transport.build_epmc_citation_url("MED", "123", "references"),
            f"{BASE}/MED/123/references",
        )
        self.assertEqual(
            transport.validate_epmc_citation_identity("PMC", "PMC9", "citations"),
            ("PMC", "PMC9", "citations"),
        )
        bad = [
            ("MED", "1", "reference"),      # bad relation
            ("MED", "1", "REFERENCES"),
            ("med", "1", "references"),
            ("MED", "1/2", "references"),
            ("MED", "1%2F2", "references"),
            ("MED", "..", "references"),
            ("MED", "1..2", "references"),
            ("MED", " ", "references"),
            ("", "1", "references"),
        ]
        for source, ident, relation in bad:
            with self.subTest(source=source, ident=ident, relation=relation):
                with self.assertRaises(transport.DiscoveryValidationError):
                    transport.build_epmc_citation_url(source, ident, relation)

    def test_citation_url_is_accepted_and_params_appended(self):
        url = transport.build_epmc_citation_url("MED", "31452104", "references")
        built = transport.build_request_url(
            url, {"format": "json", "page": 1, "pageSize": 25}
        )
        self.assertEqual(
            built,
            f"{BASE}/MED/31452104/references?format=json&page=1&pageSize=25",
        )

    def test_adversarial_citation_paths_are_refused_with_zero_requests(self):
        bad = [
            # extra segment / trailing slash
            f"{BASE}/MED/123/references/extra",
            f"{BASE}/MED/123/references/",
            # missing / wrong relation
            f"{BASE}/MED/123",
            f"{BASE}/MED/123/REFERENCES",
            f"{BASE}/MED/123/referencesX",
            # wrong source alphabet
            f"{BASE}/med/123/references",
            f"{BASE}/ME-D/123/references",
            # traversal / percent
            f"{BASE}/MED/1..2/references",
            f"{BASE}/MED/1%2F2/references",
            f"{BASE}/MED/%2e%2e/references",
            # a different URL shape on the same host
            f"{BASE}/search/extra",
            "https://www.ebi.ac.uk/europepmc/webservices/rest/MED/123/references/../../search",
            # other hosts / schemes / ports / userinfo / fragment
            "https://evil.example.com/europepmc/webservices/rest/MED/123/references",
            "http://www.ebi.ac.uk/europepmc/webservices/rest/MED/123/references",
            "https://www.ebi.ac.uk:8443/europepmc/webservices/rest/MED/123/references",
            "https://u:p@www.ebi.ac.uk/europepmc/webservices/rest/MED/123/references",
            f"{BASE}/MED/123/references#frag",
        ]
        client, fake, _, _ = make_client([])
        for url in bad:
            with self.subTest(url=url):
                with self.assertRaises(transport.DiscoveryEndpointError):
                    transport.build_request_url(url)
                with self.assertRaises(transport.DiscoveryEndpointError):
                    client.transport.get(url)
        # Nothing above reached the low-level transport (zero requests).
        self.assertEqual(fake.calls, [])

    def test_query_credentials_on_a_citation_url_are_refused(self):
        url = transport.build_epmc_citation_url("MED", "1", "citations")
        with self.assertRaises(transport.DiscoveryValidationError):
            transport.build_request_url(url + "?api_key=leak")

    def test_http_get_refuses_a_bad_citation_url_before_opening_a_socket(self):
        bad = f"{BASE}/MED/123/references/extra"
        with mock.patch.object(transport, "_http_opener") as opener_mock:
            with self.assertRaises(transport.DiscoveryEndpointError):
                transport.http_get(bad, timeout=5.0, max_response_bytes=1024)
            opener_mock.assert_not_called()

    def test_existing_exact_endpoints_still_allowlisted(self):
        # The citation family must not have widened the fixed exact-path set.
        self.assertEqual(
            set(transport.ALLOWED_ENDPOINTS),
            {
                "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi",
                "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi",
                f"{BASE}/search",
            },
        )


if __name__ == "__main__":
    unittest.main()
