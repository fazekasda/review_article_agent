"""Deterministic tests for the Europe PMC cursor-paginated adapter (Step 05 T3).

Covers ``src/review_article_agent/discovery/europepmc.py`` against the task T3
acceptance criteria and ``docs/implementation/05-discovery.md`` AC3/4/7:

* one explicit library ``search`` call per page with the exact query,
  ``format=json``, ``resultType=lite``, a bounded ``pageSize``, and cursor
  pagination from ``*`` to ``nextCursorMark``;
* the shared immutable ``SearchResult`` retains the exact query/counts/times/
  URLs and **every** transport attempt on the pages;
* provider identity is unambiguous (``MED:123`` vs ``PMC:123`` vs ``PPR:123``)
  and a missing identifier stays explicit with no fabricated id;
* multi-page success, no hits, overlap, changed totals, missing/repeated/empty
  cursors, malformed bodies/fields, a bounded page cap, and partial failures all
  produce explicit outcomes; ``page_limit`` and every anomaly are never complete;
* invalid parameters cause **zero** requests; the adapter makes no scientific
  authority claim and performs no reconciliation.

No network, no credential, no paid call: the low-level transport, clock, and
sleep are always local fixtures. Run:

    PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_discovery_*.py'
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from review_article_agent.discovery import europepmc, records, transport  # noqa: E402

EPMC = europepmc.EPMC_SEARCH_URL


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


def epmc_payload(*, hit_count, results, next_cursor=None) -> dict:
    """A synthetic Europe PMC ``lite`` search payload (metadata only)."""
    payload = {
        "version": "6.9",
        "hitCount": hit_count,
        "request": {
            "queryString": "q",
            "internalQuery": "q",
            "resultType": "LITE",
            "cursorMark": "*",
            "pageSize": 25,
            "sort": "",
            "synonym": False,
        },
        "resultList": {"result": list(results)},
    }
    if next_cursor is not None:
        payload["nextCursorMark"] = next_cursor
    return payload


def item(rec_id=None, source="MED", **extra) -> dict:
    """A synthetic Europe PMC result entry."""
    entry: dict = {"source": source}
    if rec_id is not None:
        entry["id"] = rec_id
    entry.update(extra)
    return entry


def make_client(script, **overrides):
    clock = FakeClock()
    sleep = FakeSleep(clock)
    fake = FakeTransport(script)
    utc = FakeUtcNow()
    kwargs = dict(
        sleep=sleep,
        clock=clock,
        utcnow=utc,
        transport=fake,
        rate_spacing=0.0,
    )
    kwargs.update(overrides)
    shared = transport.DiscoveryTransport(**kwargs)
    client = europepmc.EuropePMCClient(shared, utcnow=utc)
    return client, fake, sleep, clock


def query_params(url: str) -> dict[str, list[str]]:
    return parse_qs(urlsplit(url).query)


# --------------------------------------------------------------------------- #
# Success, pagination, and provenance
# --------------------------------------------------------------------------- #
class SuccessTests(unittest.TestCase):
    def test_multi_page_cursor_pagination_and_provenance(self):
        script = [
            json_response(
                epmc_payload(
                    hit_count=3,
                    results=[item("1"), item("2")],
                    next_cursor="CUR1",
                )
            ),
            json_response(epmc_payload(hit_count=3, results=[item("3")])),
        ]
        client, fake, _, _ = make_client(script)
        result = client.search("EXT_ID:1 AND SRC:MED", page_size=2, max_pages=5)

        self.assertTrue(result.complete)
        self.assertEqual(result.stop_reason, "completed")
        self.assertEqual(result.source, records.SOURCE_EUROPEPMC)
        self.assertEqual(result.query, "EXT_ID:1 AND SRC:MED")
        self.assertEqual(result.total_count, 3)
        self.assertEqual(len(result.pages), 2)
        self.assertEqual(
            [r.source_id for r in result.records], ["MED:1", "MED:2", "MED:3"]
        )
        self.assertEqual(len(fake.calls), 2)

        first, second = result.pages
        self.assertEqual(first.page_index, 0)
        self.assertEqual(second.page_index, 1)
        self.assertEqual(first.next_cursor, "CUR1")
        self.assertIsNone(second.next_cursor)
        self.assertTrue(first.complete and second.complete)

        # Exact query/format/resultType/pageSize and the cursor walk.
        q0 = query_params(first.requested_url)
        self.assertEqual(q0["query"], ["EXT_ID:1 AND SRC:MED"])
        self.assertEqual(q0["format"], ["json"])
        self.assertEqual(q0["resultType"], ["lite"])
        self.assertEqual(q0["pageSize"], ["2"])
        self.assertEqual(q0["cursorMark"], ["*"])
        q1 = query_params(second.requested_url)
        self.assertEqual(q1["cursorMark"], ["CUR1"])
        self.assertTrue(first.requested_url.startswith(EPMC + "?"))

        # Every attempt is retained on the page and on the aggregate.
        self.assertEqual(len(first.attempts), 1)
        self.assertEqual(first.attempts[0].outcome, "ok")
        self.assertEqual(first.attempts[0].requested_url, first.requested_url)
        self.assertEqual(len(result.attempts), 2)
        # Times are recorded from the injected clock.
        self.assertTrue(result.started_at and result.completed_at)
        self.assertTrue(first.retrieved_at)

    def test_no_hits_is_a_complete_empty_result(self):
        script = [json_response(epmc_payload(hit_count=0, results=[]))]
        client, fake, _, _ = make_client(script)
        result = client.search("nothing matches")

        self.assertTrue(result.complete)
        self.assertEqual(result.stop_reason, "completed")
        self.assertEqual(result.records, ())
        self.assertEqual(result.total_count, 0)
        self.assertEqual(len(result.pages), 1)
        self.assertEqual(len(fake.calls), 1)

    def test_page_size_boundary_1000_is_allowed_and_sent(self):
        script = [json_response(epmc_payload(hit_count=0, results=[]))]
        client, fake, _, _ = make_client(script)
        client.search("q", page_size=1000)
        self.assertEqual(query_params(fake.calls[0][0])["pageSize"], ["1000"])

    def test_records_flatten_exactly_from_pages_without_reconciliation(self):
        script = [
            json_response(
                epmc_payload(
                    hit_count=2, results=[item("1")], next_cursor="C1"
                )
            ),
            json_response(epmc_payload(hit_count=2, results=[item("2")])),
        ]
        client, _, _, _ = make_client(script)
        result = client.search("q")
        flattened = tuple(r for page in result.pages for r in page.records)
        self.assertEqual(result.records, flattened)


# --------------------------------------------------------------------------- #
# Identity
# --------------------------------------------------------------------------- #
class IdentityTests(unittest.TestCase):
    def test_native_source_identity_cannot_alias_bare_numeric_ids(self):
        results = [
            item("123", source="MED", pmid="123"),
            item("123", source="PMC", pmcid="PMC123"),
            item("123", source="PPR"),
        ]
        script = [json_response(epmc_payload(hit_count=3, results=results))]
        client, _, _, _ = make_client(script)
        result = client.search("q")

        self.assertEqual(
            [r.source_id for r in result.records],
            ["MED:123", "PMC:123", "PPR:123"],
        )
        self.assertEqual(len({r.source_id for r in result.records}), 3)
        self.assertTrue(all(r.source == "europepmc" for r in result.records))

    def test_missing_identifier_stays_explicit_and_is_not_invented(self):
        results = [item(None, source="MED", title="No identifiers", pubYear="2020")]
        script = [json_response(epmc_payload(hit_count=1, results=results))]
        client, _, _, _ = make_client(script)
        result = client.search("q")

        record = result.records[0]
        self.assertIsNone(record.source_id)
        self.assertIsNone(record.pmid)
        self.assertIsNone(record.pmcid)
        self.assertIsNone(record.doi)
        self.assertFalse(record.has_identifier())
        self.assertTrue(record.is_missing_identifier())
        self.assertTrue(result.complete)

    def test_native_source_containing_a_colon_is_rejected(self):
        # source="MED:A", id="B" would otherwise collide with source="MED",
        # id="A:B" once composed as "MED:A:B".
        script = [
            json_response(
                epmc_payload(hit_count=1, results=[item("B", source="MED:A")])
            )
        ]
        client, _, _, _ = make_client(script)
        result = client.search("q")
        self.assertFalse(result.complete)
        self.assertEqual(result.failure, "malformed_response")

    def test_colon_in_the_id_is_unambiguous(self):
        # The native source has no ":", so the first ":" delimits source from id.
        script = [
            json_response(
                epmc_payload(hit_count=1, results=[item("A:B", source="MED")])
            )
        ]
        client, _, _, _ = make_client(script)
        result = client.search("q")
        self.assertTrue(result.complete)
        self.assertEqual(result.records[0].source_id, "MED:A:B")

    def test_nullable_metadata_fields_are_preserved(self):
        results = [
            item(
                "9",
                source="MED",
                pmid="9",
                pmcid="PMC9",
                doi="10.1/x",
                title="A title",
                pubYear="2019",
            )
        ]
        script = [json_response(epmc_payload(hit_count=1, results=results))]
        client, _, _, _ = make_client(script)
        record = client.search("q").records[0]
        self.assertEqual(record.pmid, "9")
        self.assertEqual(record.pmcid, "PMC9")
        self.assertEqual(record.doi, "10.1/x")
        self.assertEqual(record.title, "A title")
        self.assertEqual(record.publication_year, 2019)
        self.assertEqual(record.authors, ())
        self.assertIsNone(record.journal)

    def test_structured_authors_and_journal_are_parsed_in_order(self):
        results = [
            item(
                "9",
                source="MED",
                title="With authors",
                journalTitle="Journal of Synthetic Studies",
                authorList={
                    "author": [
                        {"fullName": "Ada Lovelace"},
                        {"firstName": "Alan", "lastName": "Turing"},
                        {"lastName": "Solo", "initials": "A"},
                        {"collectiveName": "The Consortium"},
                    ]
                },
            )
        ]
        script = [json_response(epmc_payload(hit_count=1, results=results))]
        client, _, _, _ = make_client(script)
        record = client.search("q").records[0]
        self.assertEqual(
            record.authors,
            ("Ada Lovelace", "Alan Turing", "Solo A", "The Consortium"),
        )
        self.assertEqual(record.journal, "Journal of Synthetic Studies")

    def test_author_string_fallback_and_journal_info_fallback(self):
        results = [
            item(
                "9",
                source="MED",
                authorString="Ada Lovelace, Alan Turing",
                journalInfo={"journal": {"title": "Fallback Journal"}},
            )
        ]
        script = [json_response(epmc_payload(hit_count=1, results=results))]
        client, _, _, _ = make_client(script)
        record = client.search("q").records[0]
        self.assertEqual(record.authors, ("Ada Lovelace", "Alan Turing"))
        self.assertEqual(record.journal, "Fallback Journal")

    def test_control_characters_are_stripped_from_author_and_journal(self):
        results = [
            item(
                "9",
                source="MED",
                authorString="Ada\u0000 Lovelace",
                journalTitle="Journal\u0007 of\nSynthetic  Studies",
            )
        ]
        script = [json_response(epmc_payload(hit_count=1, results=results))]
        client, _, _, _ = make_client(script)
        record = client.search("q").records[0]
        self.assertEqual(record.authors, ("Ada Lovelace",))
        self.assertEqual(record.journal, "Journal of Synthetic Studies")


# --------------------------------------------------------------------------- #
# Overlap and count consistency
# --------------------------------------------------------------------------- #
class ConsistencyTests(unittest.TestCase):
    def test_overlap_stays_visible_and_never_claims_completeness(self):
        a, b, c = item("A"), item("B"), item("C")
        script = [
            json_response(
                epmc_payload(hit_count=3, results=[a, b], next_cursor="C1")
            ),
            json_response(epmc_payload(hit_count=3, results=[b, c])),
        ]
        client, _, _, _ = make_client(script)
        result = client.search("q")

        self.assertFalse(result.complete)
        self.assertEqual(result.stop_reason, "failure")
        self.assertEqual(result.failure, "malformed_response")
        # The duplicate is retained (visible), not deduplicated.
        self.assertEqual(
            [r.source_id for r in result.records],
            ["MED:A", "MED:B", "MED:B", "MED:C"],
        )
        self.assertEqual(
            sum(1 for r in result.records if r.source_id == "MED:B"), 2
        )
        # The earlier successful page is retained alongside the anomaly page.
        self.assertEqual(len(result.pages), 2)
        self.assertTrue(result.pages[0].complete)
        self.assertFalse(result.pages[1].complete)

    def test_duplicate_identity_when_raw_count_equals_total_is_malformed(self):
        # hitCount 4 and four raw rows, but one identity repeats: never complete.
        a, b, c = item("A"), item("B"), item("C")
        script = [
            json_response(
                epmc_payload(hit_count=4, results=[a, b], next_cursor="C1")
            ),
            json_response(epmc_payload(hit_count=4, results=[b, c])),
        ]
        client, _, _, _ = make_client(script)
        result = client.search("q")

        self.assertFalse(result.complete)
        self.assertEqual(result.failure, "malformed_response")
        self.assertEqual(len(result.records), 4)
        self.assertEqual(
            sum(1 for r in result.records if r.source_id == "MED:B"), 2
        )

    def test_duplicate_identity_within_one_page_is_malformed(self):
        script = [
            json_response(
                epmc_payload(hit_count=2, results=[item("A"), item("A")])
            )
        ]
        client, _, _, _ = make_client(script)
        result = client.search("q")
        self.assertFalse(result.complete)
        self.assertEqual(result.failure, "malformed_response")

    def test_missing_native_id_is_not_merged_or_treated_as_a_duplicate(self):
        # Two records with no native id and an identical title are distinct
        # explicit observations, never merged and never a fabricated identity.
        rows = [
            item(None, source="MED", title="Same title"),
            item(None, source="MED", title="Same title"),
        ]
        script = [json_response(epmc_payload(hit_count=2, results=rows))]
        client, _, _, _ = make_client(script)
        result = client.search("q")

        self.assertTrue(result.complete)
        self.assertEqual(len(result.records), 2)
        self.assertTrue(all(r.source_id is None for r in result.records))
        self.assertTrue(all(r.is_missing_identifier() for r in result.records))

    def test_page_returning_more_records_than_requested_is_malformed(self):
        script = [
            json_response(
                epmc_payload(hit_count=2, results=[item("1"), item("2")])
            )
        ]
        client, _, _, _ = make_client(script)
        result = client.search("q", page_size=1)
        self.assertFalse(result.complete)
        self.assertEqual(result.failure, "malformed_response")

    def test_over_collection_is_caught_early_not_at_the_page_cap(self):
        # hitCount 2 but the first page already returns three rows with an
        # advancing cursor: fail immediately instead of requesting more pages.
        script = [
            json_response(
                epmc_payload(
                    hit_count=2,
                    results=[item("1"), item("2"), item("3")],
                    next_cursor="C1",
                )
            ),
            json_response(epmc_payload(hit_count=2, results=[])),
        ]
        client, fake, _, _ = make_client(script)
        result = client.search("q", max_pages=5)

        self.assertFalse(result.complete)
        self.assertEqual(result.failure, "malformed_response")
        self.assertEqual(len(fake.calls), 1)

    def test_changing_total_count_is_malformed_and_incomplete(self):
        script = [
            json_response(
                epmc_payload(hit_count=2, results=[item("1")], next_cursor="C1")
            ),
            json_response(epmc_payload(hit_count=5, results=[item("2")])),
        ]
        client, _, _, _ = make_client(script)
        result = client.search("q")

        self.assertFalse(result.complete)
        self.assertEqual(result.failure, "malformed_response")
        # The stable first count is the reported total; the earlier page survives.
        self.assertEqual(result.total_count, 2)
        self.assertEqual(len(result.pages), 2)
        self.assertEqual([r.source_id for r in result.pages[0].records], ["MED:1"])

    def test_final_page_shorter_than_hit_count_is_incomplete(self):
        script = [
            json_response(
                epmc_payload(hit_count=3, results=[item("1")], next_cursor="C1")
            ),
            json_response(epmc_payload(hit_count=3, results=[item("2")])),
        ]
        client, _, _, _ = make_client(script)
        result = client.search("q")
        self.assertFalse(result.complete)
        self.assertEqual(result.failure, "malformed_response")


# --------------------------------------------------------------------------- #
# Cursor anomalies
# --------------------------------------------------------------------------- #
class CursorTests(unittest.TestCase):
    def test_unchanged_cursor_at_total_is_a_complete_terminal(self):
        # Solr-derived terminal signal: nextCursorMark equals the cursorMark sent.
        script = [
            json_response(
                epmc_payload(hit_count=2, results=[item("1")], next_cursor="C1")
            ),
            json_response(
                epmc_payload(hit_count=2, results=[item("2")], next_cursor="C1")
            ),
        ]
        client, _, _, _ = make_client(script)
        result = client.search("q")
        self.assertTrue(result.complete)
        self.assertEqual(result.stop_reason, "completed")
        self.assertEqual(len(result.pages), 2)

    def test_unchanged_cursor_before_total_is_incomplete(self):
        script = [
            json_response(
                epmc_payload(hit_count=3, results=[item("1")], next_cursor="C1")
            ),
            json_response(
                epmc_payload(hit_count=3, results=[item("2")], next_cursor="C1")
            ),
        ]
        client, _, _, _ = make_client(script)
        result = client.search("q")
        self.assertFalse(result.complete)
        self.assertEqual(result.failure, "malformed_response")
        self.assertEqual(len(result.pages), 2)

    def test_advancing_cursor_after_total_is_a_complete_terminal(self):
        # The provider still offers an advancing cursor, but every declared hit
        # has been collected exactly once: no truncation, so complete.
        script = [
            json_response(
                epmc_payload(hit_count=2, results=[item("1")], next_cursor="C1")
            ),
            json_response(
                epmc_payload(hit_count=2, results=[item("2")], next_cursor="C2")
            ),
        ]
        client, _, _, _ = make_client(script)
        result = client.search("q")
        self.assertTrue(result.complete)
        self.assertEqual(result.stop_reason, "completed")
        self.assertEqual(result.pages[-1].next_cursor, "C2")

    def test_cursor_loop_to_a_previous_value_is_malformed(self):
        script = [
            json_response(
                epmc_payload(hit_count=3, results=[item("1")], next_cursor="C1")
            ),
            json_response(
                epmc_payload(hit_count=3, results=[item("2")], next_cursor="*")
            ),
        ]
        client, _, _, _ = make_client(script)
        result = client.search("q")
        self.assertFalse(result.complete)
        self.assertEqual(result.failure, "malformed_response")

    def test_empty_cursor_mark_is_malformed(self):
        script = [
            json_response(
                epmc_payload(hit_count=1, results=[item("1")], next_cursor="")
            )
        ]
        client, _, _, _ = make_client(script)
        result = client.search("q")
        self.assertFalse(result.complete)
        self.assertEqual(result.failure, "malformed_response")

    def test_empty_page_with_an_advancing_cursor_is_malformed(self):
        script = [
            json_response(
                epmc_payload(hit_count=2, results=[], next_cursor="C1")
            )
        ]
        client, _, _, _ = make_client(script)
        result = client.search("q")
        self.assertFalse(result.complete)
        self.assertEqual(result.failure, "malformed_response")

    def test_empty_terminal_page_with_no_results_is_complete(self):
        # hitCount 0 with an unchanged cursor is a legitimate empty terminal.
        script = [
            json_response(
                epmc_payload(hit_count=0, results=[], next_cursor="*")
            )
        ]
        client, _, _, _ = make_client(script)
        result = client.search("q")
        self.assertTrue(result.complete)
        self.assertEqual(result.records, ())

    def test_over_long_cursor_is_malformed(self):
        script = [
            json_response(
                epmc_payload(
                    hit_count=2,
                    results=[item("1")],
                    next_cursor="x" * (europepmc.MAX_CURSOR_LEN + 1),
                )
            )
        ]
        client, _, _, _ = make_client(script)
        result = client.search("q")
        self.assertFalse(result.complete)
        self.assertEqual(result.failure, "malformed_response")

    def test_missing_cursor_with_unsatisfied_count_is_incomplete(self):
        # hitCount claims two hits but the only page offers no cursor and one row.
        script = [json_response(epmc_payload(hit_count=2, results=[item("1")]))]
        client, _, _, _ = make_client(script)
        result = client.search("q")
        self.assertFalse(result.complete)
        self.assertEqual(result.failure, "malformed_response")


# --------------------------------------------------------------------------- #
# Malformed responses
# --------------------------------------------------------------------------- #
class MalformedTests(unittest.TestCase):
    def _assert_malformed(self, payload_or_bytes):
        if isinstance(payload_or_bytes, (bytes, bytearray)):
            script = [response(200, bytes(payload_or_bytes))]
        else:
            script = [json_response(payload_or_bytes)]
        client, _, _, _ = make_client(script)
        result = client.search("q")
        self.assertFalse(result.complete)
        self.assertEqual(result.stop_reason, "failure")
        self.assertEqual(result.failure, "malformed_response")
        return result

    def test_malformed_page_bodies_are_incomplete(self):
        # P1 merge: each former standalone malformed-body test is preserved
        # verbatim as a subTest case (original method name kept as the label).
        def case_non_json_body():
            self._assert_malformed(b"not json at all")

        def case_non_object_body():
            self._assert_malformed([1, 2, 3])

        def case_missing_result_list():
            self._assert_malformed({"hitCount": 1})

        def case_result_not_a_list():
            self._assert_malformed({"hitCount": 1, "resultList": {"result": {}}})

        def case_missing_hit_count():
            self._assert_malformed({"resultList": {"result": []}})

        def case_boolean_hit_count():
            self._assert_malformed({"hitCount": True, "resultList": {"result": []}})

        def case_negative_hit_count():
            self._assert_malformed({"hitCount": -1, "resultList": {"result": []}})

        def case_result_entry_not_an_object():
            self._assert_malformed(
                epmc_payload(hit_count=1, results=["not-an-object"])
            )

        def case_missing_native_source():
            self._assert_malformed(epmc_payload(hit_count=1, results=[{"id": "1"}]))

        def case_non_string_identifier():
            self._assert_malformed(
                epmc_payload(hit_count=1, results=[item(123, source="MED")])
            )

        def case_identifier_with_whitespace():
            self._assert_malformed(
                epmc_payload(hit_count=1, results=[item("1 2", source="MED")])
            )

        def case_invalid_publication_year():
            self._assert_malformed(
                epmc_payload(
                    hit_count=1, results=[item("1", source="MED", pubYear="in press")]
                )
            )

        def case_over_long_title():
            self._assert_malformed(
                epmc_payload(
                    hit_count=1,
                    results=[
                        item(
                            "1",
                            source="MED",
                            title="x" * (records.MAX_TITLE_LEN + 1),
                        )
                    ],
                )
            )

        def case_author_list_not_a_list():
            self._assert_malformed(
                epmc_payload(
                    hit_count=1,
                    results=[item("1", source="MED", authorList={"author": "Ada"})],
                )
            )

        def case_over_long_author_list():
            authors = [{"fullName": f"a{i}"} for i in range(records.MAX_AUTHORS + 1)]
            self._assert_malformed(
                epmc_payload(
                    hit_count=1,
                    results=[item("1", source="MED", authorList={"author": authors})],
                )
            )

        def case_non_string_journal_title():
            self._assert_malformed(
                epmc_payload(
                    hit_count=1, results=[item("1", source="MED", journalTitle=7)]
                )
            )

        cases = [
            ("test_non_json_body", case_non_json_body),
            ("test_non_object_body", case_non_object_body),
            ("test_missing_result_list", case_missing_result_list),
            ("test_result_not_a_list", case_result_not_a_list),
            ("test_missing_hit_count", case_missing_hit_count),
            ("test_boolean_hit_count", case_boolean_hit_count),
            ("test_negative_hit_count", case_negative_hit_count),
            ("test_result_entry_not_an_object", case_result_entry_not_an_object),
            ("test_missing_native_source", case_missing_native_source),
            ("test_non_string_identifier", case_non_string_identifier),
            ("test_identifier_with_whitespace", case_identifier_with_whitespace),
            ("test_invalid_publication_year", case_invalid_publication_year),
            ("test_over_long_title", case_over_long_title),
            ("test_author_list_not_a_list", case_author_list_not_a_list),
            ("test_over_long_author_list", case_over_long_author_list),
            ("test_non_string_journal_title", case_non_string_journal_title),
        ]
        for name, case in cases:
            with self.subTest(case=name):
                case()

    def test_malformed_page_retains_attempt_provenance(self):
        script = [response(200, b"{not json")]
        client, _, _, _ = make_client(script)
        result = client.search("q")
        self.assertEqual(len(result.pages), 1)
        self.assertEqual(len(result.pages[0].attempts), 1)
        self.assertEqual(result.pages[0].attempts[0].outcome, "ok")
        self.assertEqual(len(result.attempts), 1)

    def test_deeply_nested_json_is_malformed_and_keeps_attempts(self):
        # Deep nesting exhausts the JSON parser's recursion budget; it must be an
        # explicit malformed_response, not a RecursionError, with provenance kept.
        depth = 100_000
        body = b"[" * depth + b"]" * depth
        script = [response(200, body)]
        client, _, _, _ = make_client(script)
        result = client.search("q")
        self.assertFalse(result.complete)
        self.assertEqual(result.stop_reason, "failure")
        self.assertEqual(result.failure, "malformed_response")
        self.assertEqual(len(result.pages), 1)
        self.assertEqual(len(result.pages[0].attempts), 1)
        self.assertEqual(result.pages[0].attempts[0].outcome, "ok")


# --------------------------------------------------------------------------- #
# Bounds and partial failure
# --------------------------------------------------------------------------- #
class BoundsTests(unittest.TestCase):
    def test_page_limit_is_explicit_and_never_complete(self):
        script = [
            json_response(
                epmc_payload(hit_count=10, results=[item("1")], next_cursor="C1")
            ),
            json_response(
                epmc_payload(hit_count=10, results=[item("2")], next_cursor="C2")
            ),
        ]
        client, fake, _, _ = make_client(script)
        result = client.search("q", page_size=1, max_pages=2)

        self.assertFalse(result.complete)
        self.assertEqual(result.stop_reason, "page_limit")
        self.assertIsNone(result.failure)
        self.assertEqual(len(result.pages), 2)
        self.assertEqual(len(fake.calls), 2)
        self.assertTrue(all(page.complete for page in result.pages))
        # No hidden cap: the provider still offered a cursor.
        self.assertEqual(result.pages[-1].next_cursor, "C2")

    def test_partial_failure_retains_earlier_pages_and_attempts(self):
        script = [
            json_response(
                epmc_payload(hit_count=5, results=[item("1")], next_cursor="C1")
            ),
            response(503, b"busy"),
            response(503, b"busy"),
            response(503, b"busy"),
        ]
        client, fake, sleep, _ = make_client(script, max_attempts=3)
        result = client.search("q", max_pages=5)

        self.assertFalse(result.complete)
        self.assertEqual(result.stop_reason, "failure")
        self.assertEqual(result.failure, "attempts_exhausted")
        self.assertEqual(len(result.pages), 2)
        # Earlier successful page survives with its record.
        self.assertEqual([r.source_id for r in result.pages[0].records], ["MED:1"])
        self.assertTrue(result.pages[0].complete)
        # The failed page carries all three retry attempts.
        failed = result.pages[1]
        self.assertFalse(failed.complete)
        self.assertEqual(failed.failure, "attempts_exhausted")
        self.assertEqual(len(failed.attempts), 3)
        self.assertEqual([a.outcome for a in failed.attempts], ["retryable"] * 3)
        # All attempts (success + retries) are on the aggregate.
        self.assertEqual(len(result.attempts), 4)
        self.assertEqual(len(fake.calls), 4)

    def test_transient_status_then_success_retries_via_shared_transport(self):
        script = [
            response(429, b"slow down", headers={"Retry-After": "0"}),
            json_response(epmc_payload(hit_count=0, results=[])),
        ]
        client, fake, sleep, _ = make_client(script, max_attempts=3)
        result = client.search("q")
        self.assertTrue(result.complete)
        self.assertEqual(len(result.pages[0].attempts), 2)
        self.assertEqual(
            [a.outcome for a in result.pages[0].attempts], ["retryable", "ok"]
        )
        self.assertEqual(len(fake.calls), 2)


# --------------------------------------------------------------------------- #
# Invalid parameters cause zero requests
# --------------------------------------------------------------------------- #
class ValidationTests(unittest.TestCase):
    def test_invalid_parameters_cause_zero_requests(self):
        bad_calls = [
            ("", {}),
            ("   ", {}),
            (None, {}),
            (123, {}),
            ("q", {"page_size": 0}),
            ("q", {"page_size": 1001}),
            ("q", {"page_size": True}),
            ("q", {"page_size": 1.5}),
            ("q", {"page_size": float("nan")}),
            ("q", {"max_pages": 0}),
            ("q", {"max_pages": 1001}),
            ("q", {"max_pages": True}),
            ("q", {"max_pages": 2.0}),
            ("x" * (europepmc.MAX_QUERY_LEN + 1), {}),
            ("bad\nquery", {}),
        ]
        for query, kwargs in bad_calls:
            with self.subTest(query=query, kwargs=kwargs):
                client, fake, _, _ = make_client([])
                with self.assertRaises(transport.DiscoveryValidationError):
                    client.search(query, **kwargs)
                self.assertEqual(fake.calls, [])

    def test_transport_must_be_injected(self):
        with self.assertRaises(transport.DiscoveryValidationError):
            europepmc.EuropePMCClient(None)
        with self.assertRaises(transport.DiscoveryValidationError):
            europepmc.EuropePMCClient(object())

    def test_utcnow_must_be_callable(self):
        client, _, _, _ = make_client([])
        with self.assertRaises(transport.DiscoveryValidationError):
            europepmc.EuropePMCClient(client.transport, utcnow="not-callable")


# --------------------------------------------------------------------------- #
# No scientific authority
# --------------------------------------------------------------------------- #
class AuthorityTests(unittest.TestCase):
    def test_adapter_makes_no_scientific_or_approval_claim(self):
        # The adapter exposes only the bounded search entry point; there is no
        # approval/eligibility/coverage API and no reconciliation.
        for forbidden in (
            "approve",
            "accept",
            "eligible",
            "eligibility",
            "reconcile",
            "coverage",
            "freeze",
        ):
            self.assertFalse(
                hasattr(europepmc.EuropePMCClient, forbidden),
                f"unexpected scientific-authority attribute: {forbidden}",
            )
        # A completed result is a bounded retrieval, not a coverage claim: the
        # shared aggregate exposes no coverage/completeness-of-science field.
        for forbidden in ("coverage", "scientific_complete", "accepted"):
            self.assertFalse(hasattr(records.SearchResult, forbidden))

    def test_module_documents_observations_not_scientific_context(self):
        doc = europepmc.__doc__ or ""
        self.assertIn("Not scientific context", doc)
        self.assertIn("deferred", doc)
        self.assertIn("never claims absolute completeness", doc)


if __name__ == "__main__":
    unittest.main()
