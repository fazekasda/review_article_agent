"""Deterministic tests for the PubMed ESearch+EFetch adapter (Step 05 slice 1, T2).

Covers ``src/review_article_agent/discovery/pubmed.py`` against the task T2
acceptance criteria:

* a bounded ESearch+EFetch metadata search returns the shared immutable
  :class:`SearchResult` with explicit partial outcomes;
* exact query/source/times/failures/counts/page provenance and both ESearch and
  EFetch attempt records are preserved;
* deterministic pagination, out-of-order EFetch, nested titles, missing IDs,
  overlap, malformed/error JSON/XML, transient/partial failure, count/cursor
  mismatch, and the provider window are all handled without a false
  ``complete``;
* invalid parameters perform zero transport calls; provenance is immutable.

No network, no credential, no paid call: the low-level transport, clock, and
sleep are always local fixtures. Run:

    PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_discovery_*.py'
"""

from __future__ import annotations

import dataclasses
import json
import sys
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from review_article_agent.discovery import pubmed  # noqa: E402
from review_article_agent.discovery.pubmed import (  # noqa: E402
    PUBMED_ESEARCH_URL,
    PUBMED_EFETCH_URL,
    PUBMED_WINDOW_LIMIT,
    PubMedClient,
    PubMedSearchResult,
    PubMedValidationError,
)
from review_article_agent.discovery.records import (  # noqa: E402
    MAX_AUTHORS,
    MAX_TITLE_LEN,
    SOURCE_PUBMED,
    MetadataRecord,
    SearchResult,
)
from review_article_agent.discovery.transport import (  # noqa: E402
    AttemptRecord,
    DiscoveryTransport,
    TransportNetworkError,
    TransportResponse,
)


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
class StepClock:
    """A deterministic, strictly increasing UTC timestamp source."""

    def __init__(self) -> None:
        self.n = 0

    def __call__(self) -> str:
        self.n += 1
        return f"2026-10-28T00:00:{self.n:02d}+00:00"


class ScriptedTransport:
    """A scripted low-level transport: each item is a response or an exception."""

    def __init__(self, script: list[object]) -> None:
        self.script = list(script)
        self.calls: list[str] = []

    def __call__(self, url, *, timeout, max_response_bytes):
        self.calls.append(url)
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class DynamicPubMedTransport:
    """A synthetic PubMed transport that generates pages from the request."""

    def __init__(self, *, count: int) -> None:
        self.count = count
        self.calls: list[str] = []

    def __call__(self, url, *, timeout, max_response_bytes):
        self.calls.append(url)
        parsed = urllib.parse.urlsplit(url)
        params = dict(urllib.parse.parse_qsl(parsed.query))
        if parsed.path.endswith("esearch.fcgi"):
            offset = int(params["retstart"])
            retmax = int(params["retmax"])
            end = min(offset + retmax, self.count)
            ids = [str(i) for i in range(offset + 1, end + 1)]
            body = esearch_body(self.count, ids, retstart=offset, retmax=retmax)
            return TransportResponse(status=200, body=body, headers={})
        ids = params["id"].split(",")
        articles = [{"pmid": pid, "title": f"Title {pid}", "year": 2020} for pid in ids]
        return TransportResponse(status=200, body=efetch_body(articles), headers={})


def ok(body: bytes) -> TransportResponse:
    return TransportResponse(status=200, body=body, headers={})


def esearch_body(
    count: int,
    idlist,
    *,
    retstart: int = 0,
    retmax: int | None = None,
    translation: str | None = None,
    errorlist=None,
    warninglist=None,
) -> bytes:
    result = {
        "count": str(count),
        "retmax": str(retmax if retmax is not None else len(idlist)),
        "retstart": str(retstart),
        "idlist": [str(item) for item in idlist],
    }
    if translation is not None:
        result["querytranslation"] = translation
    if errorlist is not None:
        result["errorlist"] = errorlist
    if warninglist is not None:
        result["warninglist"] = warninglist
    payload = {"header": {"type": "esearch", "version": "0.3"}, "esearchresult": result}
    return json.dumps(payload).encode()


def _author_xml(author) -> str:
    """Render one synthetic ``Author`` element (a dict or a bare collective name)."""
    if isinstance(author, str):
        return f"<Author><CollectiveName>{author}</CollectiveName></Author>"
    parts = []
    if author.get("last") is not None:
        parts.append(f"<LastName>{author['last']}</LastName>")
    if author.get("fore") is not None:
        parts.append(f"<ForeName>{author['fore']}</ForeName>")
    if author.get("initials") is not None:
        parts.append(f"<Initials>{author['initials']}</Initials>")
    return "<Author>" + "".join(parts) + "</Author>"


def efetch_body(articles) -> bytes:
    parts = ["<PubmedArticleSet>"]
    for article in articles:
        pmid = str(article["pmid"])
        title = article.get("title")
        year = article.get("year")
        doi = article.get("doi")
        pmcid = article.get("pmcid")
        authors = article.get("authors")
        journal = article.get("journal")
        title_xml = f"<ArticleTitle>{title}</ArticleTitle>" if title is not None else ""
        year_xml = f"<PubDate><Year>{year}</Year></PubDate>" if year is not None else ""
        author_xml = ""
        if authors is not None:
            author_xml = (
                "<AuthorList>" + "".join(_author_xml(a) for a in authors) + "</AuthorList>"
            )
        journal_title_xml = (
            f"<Title>{journal}</Title>" if journal is not None else ""
        )
        ids = [f'<ArticleId IdType="pubmed">{pmid}</ArticleId>']
        if doi is not None:
            ids.append(f'<ArticleId IdType="doi">{doi}</ArticleId>')
        if pmcid is not None:
            ids.append(f'<ArticleId IdType="pmc">{pmcid}</ArticleId>')
        parts.append(
            "<PubmedArticle><MedlineCitation>"
            f'<PMID Version="1">{pmid}</PMID>'
            f"<Article>{title_xml}{author_xml}<Journal>{journal_title_xml}"
            f"<JournalIssue>{year_xml}"
            "</JournalIssue></Journal></Article>"
            "</MedlineCitation><PubmedData><ArticleIdList>"
            + "".join(ids)
            + "</ArticleIdList></PubmedData></PubmedArticle>"
        )
    parts.append("</PubmedArticleSet>")
    return "".join(parts).encode()


#: The plain external DOCTYPE a normal NCBI EFetch reply carries.
CANONICAL_DOCTYPE = (
    '<!DOCTYPE PubmedArticleSet PUBLIC "-//NLM//DTD PubMedArticle, 1st January 2025//EN" '
    '"https://dtd.nlm.nih.gov/ncbi/pubmed/out/pubmed_250101.dtd">'
)


def efetch_body_with_doctype(articles, doctype: str = CANONICAL_DOCTYPE) -> bytes:
    return doctype.encode() + efetch_body(articles)


def esearch_retmaxes(urls):
    """The ``retmax`` value of each ESearch request URL, in call order."""
    return [
        urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)["retmax"][0]
        for url in urls
        if "esearch.fcgi" in url
    ]


def make_client(script, **overrides):
    fake = ScriptedTransport(script)
    clock = StepClock()
    transport = DiscoveryTransport(
        transport=fake,
        sleep=lambda seconds: None,
        clock=lambda: 0.0,
        utcnow=clock,
        rate_spacing=0.0,
    )
    kwargs = dict(transport=transport, utcnow=clock)
    kwargs.update(overrides)
    return PubMedClient(**kwargs), fake


def make_dynamic_client(count, **overrides):
    fake = DynamicPubMedTransport(count=count)
    clock = StepClock()
    transport = DiscoveryTransport(
        transport=fake,
        sleep=lambda seconds: None,
        clock=lambda: 0.0,
        utcnow=clock,
        rate_spacing=0.0,
    )
    kwargs = dict(transport=transport, utcnow=clock)
    kwargs.update(overrides)
    return PubMedClient(**kwargs), fake


# --------------------------------------------------------------------------- #
# Success paths
# --------------------------------------------------------------------------- #
class SuccessTests(unittest.TestCase):
    def test_success_multipage(self):
        script = [
            ok(esearch_body(3, [1, 2], retstart=0, retmax=2)),
            ok(
                efetch_body(
                    [
                        {"pmid": 1, "title": "One", "year": 2019, "doi": "10.1/a", "pmcid": "PMC1"},
                        {"pmid": 2, "title": "Two", "year": 2020},
                    ]
                )
            ),
            ok(esearch_body(3, [3], retstart=2, retmax=2)),
            ok(efetch_body([{"pmid": 3, "title": "Three", "year": 2021}])),
        ]
        client, fake = make_client(script)
        result = client.search("cancer therapy", page_size=2, max_pages=5)

        self.assertIsInstance(result, SearchResult)
        self.assertIsInstance(result, PubMedSearchResult)
        self.assertTrue(result.complete)
        self.assertEqual(result.stop_reason, "completed")
        self.assertIsNone(result.failure)
        self.assertEqual(result.query, "cancer therapy")
        self.assertEqual(result.source, SOURCE_PUBMED)
        self.assertEqual(result.total_count, 3)
        self.assertEqual(len(result.pages), 2)
        self.assertEqual([r.pmid for r in result.records], ["1", "2", "3"])
        self.assertEqual(len(result.attempts), 4)
        # exact transmitted query and both endpoint URLs are recorded
        self.assertIn("term=cancer+therapy", result.attempts[0].requested_url)
        self.assertIn("id=1%2C2", result.attempts[1].requested_url)
        self.assertEqual(result.pages[0].requested_url, result.attempts[0].requested_url)
        self.assertEqual(result.pages[1].next_cursor, None)
        self.assertEqual(result.pages[0].next_cursor, "2")
        self.assertEqual(len(fake.calls), 4)

    def test_zero_count_is_complete_without_efetch(self):
        client, fake = make_client([ok(esearch_body(0, []))])
        result = client.search("nothing here", page_size=5, max_pages=3)

        self.assertTrue(result.complete)
        self.assertEqual(result.stop_reason, "completed")
        self.assertEqual(result.total_count, 0)
        self.assertEqual(result.records, ())
        self.assertEqual(len(result.pages), 1)
        self.assertEqual(len(result.attempts), 1)
        self.assertEqual(len(fake.calls), 1)  # no EFetch for an empty window

    def test_out_of_order_efetch_records(self):
        script = [
            ok(esearch_body(3, [1, 2, 3], retstart=0, retmax=3)),
            ok(
                efetch_body(
                    [
                        {"pmid": 3, "title": "Three"},
                        {"pmid": 1, "title": "One"},
                        {"pmid": 2, "title": "Two"},
                    ]
                )
            ),
        ]
        client, _ = make_client(script)
        result = client.search("q", page_size=3, max_pages=1)

        self.assertTrue(result.complete)
        self.assertEqual([r.pmid for r in result.records], ["3", "1", "2"])

    def test_nested_markup_title_is_flattened(self):
        title = "A study of <i>nested</i> and <sup>markup</sup> titles"
        script = [
            ok(esearch_body(1, [7], retstart=0, retmax=1)),
            ok(efetch_body([{"pmid": 7, "title": title, "year": 2018}])),
        ]
        client, _ = make_client(script)
        result = client.search("q", page_size=1, max_pages=1)

        self.assertTrue(result.complete)
        self.assertEqual(result.records[0].title, "A study of nested and markup titles")

    def test_absent_optional_metadata_stays_none(self):
        script = [
            ok(esearch_body(1, [5], retstart=0, retmax=1)),
            ok(efetch_body([{"pmid": 5}])),
        ]
        client, _ = make_client(script)
        result = client.search("q", page_size=1, max_pages=1)

        record = result.records[0]
        self.assertEqual(record.pmid, "5")
        self.assertIsNone(record.title)
        self.assertIsNone(record.doi)
        self.assertIsNone(record.pmcid)
        self.assertIsNone(record.publication_year)
        self.assertTrue(record.has_identifier())

    def test_medline_date_year_is_parsed(self):
        raw = (
            "<PubmedArticleSet><PubmedArticle><MedlineCitation>"
            '<PMID Version="1">9</PMID>'
            "<Article><ArticleTitle>Dated</ArticleTitle>"
            "<Journal><JournalIssue><PubDate>"
            "<MedlineDate>2019 Jan-Feb</MedlineDate>"
            "</PubDate></JournalIssue></Journal></Article>"
            "</MedlineCitation></PubmedArticle></PubmedArticleSet>"
        ).encode()
        script = [ok(esearch_body(1, [9], retstart=0, retmax=1)), ok(raw)]
        client, _ = make_client(script)
        result = client.search("q", page_size=1, max_pages=1)

        self.assertTrue(result.complete)
        self.assertEqual(result.records[0].publication_year, 2019)

    def test_query_translation_is_retained(self):
        script = [
            ok(esearch_body(1, [31452104], retstart=0, retmax=1, translation="31452104[UID]")),
            ok(efetch_body([{"pmid": 31452104, "title": "Observed"}])),
        ]
        client, _ = make_client(script)
        result = client.search("31452104[uid]", page_size=1, max_pages=1)

        self.assertTrue(result.complete)
        self.assertEqual(result.query_translation, "31452104[UID]")
        self.assertIn("term=31452104%5Buid%5D", result.attempts[0].requested_url)

    def test_authors_and_journal_are_parsed_in_order(self):
        script = [
            ok(esearch_body(1, [7], retstart=0, retmax=1)),
            ok(
                efetch_body(
                    [
                        {
                            "pmid": 7,
                            "title": "With authors",
                            "journal": "Journal of Synthetic Studies",
                            "authors": [
                                {"fore": "Ada", "last": "Lovelace"},
                                {"last": "Turing", "initials": "AM"},
                                {"last": "Solo"},
                                "The Consortium",
                            ],
                        }
                    ]
                )
            ),
        ]
        client, _ = make_client(script)
        record = client.search("q", page_size=1, max_pages=1).records[0]
        self.assertEqual(
            record.authors,
            ("Ada Lovelace", "Turing AM", "Solo", "The Consortium"),
        )
        self.assertEqual(record.journal, "Journal of Synthetic Studies")

    def test_absent_authors_and_journal_stay_absent(self):
        script = [
            ok(esearch_body(1, [7], retstart=0, retmax=1)),
            ok(efetch_body([{"pmid": 7, "title": "No authors"}])),
        ]
        client, _ = make_client(script)
        record = client.search("q", page_size=1, max_pages=1).records[0]
        self.assertEqual(record.authors, ())
        self.assertIsNone(record.journal)


# --------------------------------------------------------------------------- #
# Partial / anomalous provider responses
# --------------------------------------------------------------------------- #
class PartialOutcomeTests(unittest.TestCase):
    def test_missing_efetch_items_never_complete(self):
        script = [
            ok(esearch_body(3, [1, 2, 3], retstart=0, retmax=3)),
            ok(efetch_body([{"pmid": 1}, {"pmid": 2}])),
        ]
        client, _ = make_client(script)
        result = client.search("q", page_size=3, max_pages=1)

        self.assertFalse(result.complete)
        self.assertEqual(result.stop_reason, "failure")
        self.assertEqual(result.failure, "malformed_response")
        self.assertEqual([r.pmid for r in result.records], ["1", "2"])
        self.assertEqual(result.pages[0].failure, "malformed_response")
        self.assertFalse(result.pages[0].complete)

    def test_unrequested_efetch_id_is_not_accepted(self):
        script = [
            ok(esearch_body(2, [1, 2], retstart=0, retmax=2)),
            ok(efetch_body([{"pmid": 1}, {"pmid": 2}, {"pmid": 3}])),
        ]
        client, _ = make_client(script)
        result = client.search("q", page_size=2, max_pages=1)

        self.assertFalse(result.complete)
        self.assertEqual(result.failure, "malformed_response")
        self.assertEqual([r.pmid for r in result.records], ["1", "2"])

    def test_duplicate_id_within_one_page_never_complete(self):
        script = [
            ok(esearch_body(2, [1, 2], retstart=0, retmax=2)),
            ok(efetch_body([{"pmid": 1}, {"pmid": 1}, {"pmid": 2}])),
        ]
        client, _ = make_client(script)
        result = client.search("q", page_size=2, max_pages=1)

        self.assertFalse(result.complete)
        self.assertEqual(result.failure, "malformed_response")

    def test_overlapping_pages_are_visible_and_never_complete(self):
        script = [
            ok(esearch_body(4, [1, 2], retstart=0, retmax=2)),
            ok(efetch_body([{"pmid": 1}, {"pmid": 2}])),
            ok(esearch_body(4, [2, 3], retstart=2, retmax=2)),
            ok(efetch_body([{"pmid": 2}, {"pmid": 3}])),
        ]
        client, _ = make_client(script)
        result = client.search("q", page_size=2, max_pages=5)

        self.assertFalse(result.complete)
        self.assertEqual(result.stop_reason, "failure")
        # the overlap is preserved verbatim (no silent deduplication)
        self.assertEqual([r.pmid for r in result.records], ["1", "2", "2", "3"])
        self.assertEqual(len(result.pages), 2)
        self.assertEqual(result.pages[1].failure, "malformed_response")

    def test_count_change_mid_run_never_complete(self):
        script = [
            ok(esearch_body(4, [1, 2], retstart=0, retmax=2)),
            ok(efetch_body([{"pmid": 1}, {"pmid": 2}])),
            ok(esearch_body(5, [3, 4], retstart=2, retmax=2)),
        ]
        client, _ = make_client(script)
        result = client.search("q", page_size=2, max_pages=5)

        self.assertFalse(result.complete)
        self.assertEqual(result.failure, "malformed_response")
        self.assertEqual(len(result.pages), 2)

    def test_cursor_mismatch_never_complete(self):
        script = [
            ok(esearch_body(4, [1, 2], retstart=0, retmax=2)),
            ok(efetch_body([{"pmid": 1}, {"pmid": 2}])),
            ok(esearch_body(4, [3, 4], retstart=99, retmax=2)),
        ]
        client, _ = make_client(script)
        result = client.search("q", page_size=2, max_pages=5)

        self.assertFalse(result.complete)
        self.assertEqual(result.failure, "malformed_response")

    def test_empty_page_before_claimed_total_never_complete(self):
        client, fake = make_client([ok(esearch_body(4, [], retstart=0, retmax=2))])
        result = client.search("q", page_size=2, max_pages=5)

        self.assertFalse(result.complete)
        self.assertEqual(result.failure, "malformed_response")
        self.assertEqual(len(fake.calls), 1)  # never reaches EFetch


# --------------------------------------------------------------------------- #
# Malformed provider responses
# --------------------------------------------------------------------------- #
class MalformedResponseTests(unittest.TestCase):
    def test_malformed_json_never_complete(self):
        client, fake = make_client([ok(b"{not json")])
        result = client.search("q", page_size=2, max_pages=3)

        self.assertFalse(result.complete)
        self.assertEqual(result.failure, "malformed_response")
        self.assertEqual(len(fake.calls), 1)

    def test_missing_esearchresult_never_complete(self):
        client, _ = make_client([ok(json.dumps({"header": {}}).encode())])
        result = client.search("q", page_size=2, max_pages=3)

        self.assertFalse(result.complete)
        self.assertEqual(result.failure, "malformed_response")

    def test_errorlist_never_successful_empty_search(self):
        client, _ = make_client([ok(esearch_body(0, [], errorlist=["Invalid query"]))])
        result = client.search("q", page_size=2, max_pages=3)

        self.assertFalse(result.complete)
        self.assertEqual(result.failure, "malformed_response")

    def test_warninglist_never_successful_empty_search(self):
        client, _ = make_client(
            [ok(esearch_body(0, [], warninglist=["Quoted phrase not found"]))]
        )
        result = client.search("q", page_size=2, max_pages=3)

        self.assertFalse(result.complete)
        self.assertEqual(result.failure, "malformed_response")

    def test_malformed_xml_never_complete(self):
        script = [
            ok(esearch_body(1, [1], retstart=0, retmax=1)),
            ok(b"<not-xml"),
        ]
        client, _ = make_client(script)
        result = client.search("q", page_size=1, max_pages=1)

        self.assertFalse(result.complete)
        self.assertEqual(result.failure, "malformed_response")

    def test_wrong_xml_root_never_complete(self):
        script = [
            ok(esearch_body(1, [1], retstart=0, retmax=1)),
            ok(b"<OtherSet></OtherSet>"),
        ]
        client, _ = make_client(script)
        result = client.search("q", page_size=1, max_pages=1)

        self.assertFalse(result.complete)
        self.assertEqual(result.failure, "malformed_response")

    def test_entity_declaration_is_refused(self):
        evil = (
            b'<!DOCTYPE foo [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>'
            b"<PubmedArticleSet>&xxe;</PubmedArticleSet>"
        )
        script = [
            ok(esearch_body(1, [1], retstart=0, retmax=1)),
            ok(evil),
        ]
        client, _ = make_client(script)
        result = client.search("q", page_size=1, max_pages=1)

        self.assertFalse(result.complete)
        self.assertEqual(result.failure, "malformed_response")


# --------------------------------------------------------------------------- #
# Transport failure and retries
# --------------------------------------------------------------------------- #
class TransportFailureTests(unittest.TestCase):
    def test_transient_status_is_retried_then_succeeds(self):
        script = [
            TransportResponse(status=503, body=b"", headers={}),
            ok(esearch_body(1, [1], retstart=0, retmax=1)),
            ok(efetch_body([{"pmid": 1, "title": "One"}])),
        ]
        client, _ = make_client(script)
        result = client.search("q", page_size=1, max_pages=1)

        self.assertTrue(result.complete)
        page = result.pages[0]
        self.assertEqual(len(page.attempts), 3)
        self.assertEqual(page.attempts[0].outcome, "retryable")
        self.assertEqual(page.attempts[1].outcome, "ok")
        self.assertEqual(page.attempts[2].outcome, "ok")

    def test_partial_failure_retains_earlier_pages_and_attempts(self):
        script = [
            ok(esearch_body(4, [1, 2], retstart=0, retmax=2)),
            ok(efetch_body([{"pmid": 1}, {"pmid": 2}])),
            TransportResponse(status=500, body=b"", headers={}),
            TransportResponse(status=500, body=b"", headers={}),
            TransportResponse(status=500, body=b"", headers={}),
        ]
        client, _ = make_client(script)
        result = client.search("q", page_size=2, max_pages=5)

        self.assertFalse(result.complete)
        self.assertEqual(result.stop_reason, "failure")
        self.assertEqual(result.failure, "attempts_exhausted")
        self.assertEqual(len(result.pages), 2)
        self.assertTrue(result.pages[0].complete)
        self.assertEqual(result.pages[1].failure, "attempts_exhausted")
        # the earlier page's records and attempts survive the later failure
        self.assertEqual([r.pmid for r in result.records], ["1", "2"])
        self.assertEqual(len(result.attempts), 5)

    def test_network_error_is_reported_explicitly(self):
        script = [
            TransportNetworkError("boom"),
            TransportNetworkError("boom"),
            TransportNetworkError("boom"),
        ]
        client, _ = make_client(script)
        result = client.search("q", page_size=2, max_pages=5)

        self.assertFalse(result.complete)
        self.assertEqual(result.failure, "network_error")
        self.assertEqual(len(result.pages), 1)


# --------------------------------------------------------------------------- #
# Hard limits
# --------------------------------------------------------------------------- #
class HardLimitTests(unittest.TestCase):
    def test_page_budget_is_explicit_page_limit(self):
        script = [
            ok(esearch_body(100, [1, 2], retstart=0, retmax=2)),
            ok(efetch_body([{"pmid": 1}, {"pmid": 2}])),
        ]
        client, _ = make_client(script)
        result = client.search("q", page_size=2, max_pages=1)

        self.assertFalse(result.complete)
        self.assertEqual(result.stop_reason, "page_limit")
        self.assertIsNone(result.failure)
        self.assertEqual(len(result.pages), 1)

    def test_provider_window_is_explicit_page_limit(self):
        client, fake = make_dynamic_client(100, window_limit=5)
        result = client.search("q", page_size=2, max_pages=10)

        self.assertFalse(result.complete)
        self.assertEqual(result.stop_reason, "page_limit")
        self.assertEqual(len(result.pages), 3)  # offsets 0, 2, 4; then window guard
        self.assertEqual(result.total_count, 100)
        # the final request is clamped to the remaining window (5 - 4 = 1)
        self.assertEqual(len(result.records), 5)
        self.assertEqual(esearch_retmaxes(fake.calls), ["2", "2", "1"])

    def test_window_clamp_prevents_false_completion(self):
        # Regression: window_limit=3, count=4, page_size=2. Without clamping the
        # second page would request [3, 4] and falsely report a completed search.
        client, fake = make_dynamic_client(4, window_limit=3)
        result = client.search("q", page_size=2, max_pages=10)

        self.assertFalse(result.complete)
        self.assertEqual(result.stop_reason, "page_limit")
        self.assertEqual(len(result.pages), 2)
        self.assertEqual([r.pmid for r in result.records], ["1", "2", "3"])
        self.assertEqual(esearch_retmaxes(fake.calls), ["2", "1"])

    def test_response_beyond_clamped_window_is_malformed(self):
        # window_limit=3 clamps page 2 to retmax=1; a provider that returns two
        # IDs anyway is rejected rather than accepted as progress.
        script = [
            ok(esearch_body(4, [1, 2], retstart=0, retmax=2)),
            ok(efetch_body([{"pmid": 1}, {"pmid": 2}])),
            ok(esearch_body(4, [3, 4], retstart=2, retmax=2)),
        ]
        client, _ = make_client(script, window_limit=3)
        result = client.search("q", page_size=2, max_pages=10)

        self.assertFalse(result.complete)
        self.assertEqual(result.failure, "malformed_response")

    def test_default_window_limit_matches_provider_cap(self):
        client, _ = make_client([])
        self.assertEqual(client.window_limit, PUBMED_WINDOW_LIMIT)

    def test_window_limit_cannot_exceed_provider_cap(self):
        with self.assertRaises(PubMedValidationError):
            PubMedClient(window_limit=PUBMED_WINDOW_LIMIT + 1)

    def test_invalid_parameters_perform_zero_transport_calls(self):
        client, fake = make_client([])
        bad_calls = [
            ("", 2, 3),
            ("   ", 2, 3),
            (123, 2, 3),
            (True, 2, 3),
            ("q", True, 3),
            ("q", 0, 3),
            ("q", pubmed.MAX_PAGE_SIZE + 1, 3),
            ("q", 2, True),
            ("q", 2, 0),
            ("q", 2, pubmed.MAX_MAX_PAGES + 1),
            ("q", 200, 200),  # page_size * max_pages exceeds the record cap
        ]
        for query, page_size, max_pages in bad_calls:
            with self.subTest(query=query, page_size=page_size, max_pages=max_pages):
                with self.assertRaises(PubMedValidationError):
                    client.search(query, page_size, max_pages)
        self.assertEqual(fake.calls, [])

    def test_invalid_construction_is_refused(self):
        for kwargs in (
            {"page_size": True},
            {"page_size": 0},
            {"page_size": pubmed.MAX_PAGE_SIZE + 1},
            {"max_pages": True},
            {"max_pages": 0},
            {"max_pages": pubmed.MAX_MAX_PAGES + 1},
            {"window_limit": 0},
            {"utcnow": 5},
            {"transport": object()},
        ):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(PubMedValidationError):
                    PubMedClient(**kwargs)


# --------------------------------------------------------------------------- #
# Immutable provenance
# --------------------------------------------------------------------------- #
class ProvenanceTests(unittest.TestCase):
    def _result(self):
        script = [
            ok(esearch_body(1, [31452104], retstart=0, retmax=1, translation="31452104[UID]")),
            ok(efetch_body([{"pmid": 31452104, "title": "Observed", "year": 2019}])),
        ]
        client, _ = make_client(script)
        return client.search("31452104[uid]", page_size=1, max_pages=1)

    def test_result_and_pages_are_frozen(self):
        result = self._result()
        with self.assertRaises(dataclasses.FrozenInstanceError):
            result.query = "changed"  # type: ignore[misc]
        with self.assertRaises(dataclasses.FrozenInstanceError):
            result.pages[0].query = "changed"  # type: ignore[misc]
        with self.assertRaises(dataclasses.FrozenInstanceError):
            result.records[0].title = "changed"  # type: ignore[misc]

    def test_attempts_are_immutable_records_with_both_endpoints(self):
        result = self._result()
        self.assertTrue(all(isinstance(a, AttemptRecord) for a in result.attempts))
        self.assertEqual(len(result.attempts), 2)
        self.assertIn(PUBMED_ESEARCH_URL, result.attempts[0].requested_url)
        self.assertIn(PUBMED_EFETCH_URL, result.attempts[1].requested_url)
        self.assertEqual(result.pages[0].requested_url, result.attempts[0].requested_url)

    def test_timestamps_are_recorded(self):
        result = self._result()
        self.assertTrue(result.started_at)
        self.assertTrue(result.completed_at)
        self.assertTrue(result.pages[0].retrieved_at)
        self.assertTrue(result.attempts[0].requested_at)
        self.assertTrue(result.attempts[0].completed_at)

    def test_records_are_metadata_records_for_the_page_source(self):
        result = self._result()
        self.assertTrue(all(isinstance(r, MetadataRecord) for r in result.records))
        self.assertTrue(all(r.source == SOURCE_PUBMED for r in result.records))


# --------------------------------------------------------------------------- #
# T2-R1 repairs: real EFetch protocol, malformed-input safety, window clamp
# --------------------------------------------------------------------------- #
class RealEfetchProtocolTests(unittest.TestCase):
    def test_canonical_external_doctype_parses_without_network(self):
        body = efetch_body_with_doctype(
            [{"pmid": 1, "title": "One", "year": 2019, "doi": "10.1/a", "pmcid": "PMC1"}]
        )
        script = [ok(esearch_body(1, [1], retstart=0, retmax=1)), ok(body)]
        client, _ = make_client(script)
        with mock.patch("socket.socket", side_effect=AssertionError("network")):
            result = client.search("q", page_size=1, max_pages=1)

        self.assertTrue(result.complete)
        self.assertEqual(result.records[0].pmid, "1")
        self.assertEqual(result.records[0].title, "One")
        self.assertEqual(result.records[0].doi, "10.1/a")
        self.assertEqual(result.records[0].pmcid, "PMC1")

    def test_doctype_internal_subset_without_entity_is_refused(self):
        body = (
            b"<!DOCTYPE PubmedArticleSet [<!ELEMENT x EMPTY>]>"
            b"<PubmedArticleSet></PubmedArticleSet>"
        )
        script = [ok(esearch_body(1, [1], retstart=0, retmax=1)), ok(body)]
        client, _ = make_client(script)
        result = client.search("q", page_size=1, max_pages=1)

        self.assertFalse(result.complete)
        self.assertEqual(result.failure, "malformed_response")

    def test_doctype_file_scheme_is_refused(self):
        body = (
            b'<!DOCTYPE PubmedArticleSet SYSTEM "file:///etc/passwd">'
            b"<PubmedArticleSet></PubmedArticleSet>"
        )
        script = [ok(esearch_body(1, [1], retstart=0, retmax=1)), ok(body)]
        client, _ = make_client(script)
        result = client.search("q", page_size=1, max_pages=1)

        self.assertFalse(result.complete)
        self.assertEqual(result.failure, "malformed_response")

    def test_pubmed_book_article_is_explicitly_unsupported(self):
        body = (
            b"<PubmedArticleSet><PubmedBookArticle><BookDocument>"
            b"<PMID>1</PMID></BookDocument></PubmedBookArticle></PubmedArticleSet>"
        )
        script = [ok(esearch_body(1, [1], retstart=0, retmax=1)), ok(body)]
        client, _ = make_client(script)
        result = client.search("q", page_size=1, max_pages=1)

        self.assertFalse(result.complete)
        self.assertEqual(result.failure, "malformed_response")


class MalformedInputSafetyTests(unittest.TestCase):
    def test_malformed_doi_metadata_is_explicit_failure(self):
        # a DOI with internal whitespace is invalid for MetadataRecord
        script = [
            ok(esearch_body(1, [1], retstart=0, retmax=1)),
            ok(efetch_body([{"pmid": 1, "title": "One", "doi": "10.1/a b"}])),
        ]
        client, _ = make_client(script)
        result = client.search("q", page_size=1, max_pages=1)

        self.assertFalse(result.complete)
        self.assertEqual(result.failure, "malformed_response")
        self.assertEqual(result.pages[0].failure, "malformed_response")
        self.assertEqual(len(result.attempts), 2)  # attempts retained

    def test_malformed_pmcid_metadata_is_explicit_failure(self):
        script = [
            ok(esearch_body(1, [1], retstart=0, retmax=1)),
            ok(efetch_body([{"pmid": 1, "title": "One", "pmcid": "PMC 1"}])),
        ]
        client, _ = make_client(script)
        result = client.search("q", page_size=1, max_pages=1)

        self.assertFalse(result.complete)
        self.assertEqual(result.failure, "malformed_response")

    def test_huge_count_digit_string_is_malformed(self):
        body = json.dumps(
            {
                "esearchresult": {
                    "count": "1" * 5000,
                    "retmax": "0",
                    "retstart": "0",
                    "idlist": [],
                }
            }
        ).encode()
        client, _ = make_client([ok(body)])
        result = client.search("q", page_size=1, max_pages=1)

        self.assertFalse(result.complete)
        self.assertEqual(result.failure, "malformed_response")

    def test_deeply_nested_json_is_malformed(self):
        body = ("[" * 200000 + "]" * 200000).encode()
        client, _ = make_client([ok(body)])
        result = client.search("q", page_size=1, max_pages=1)

        self.assertFalse(result.complete)
        self.assertEqual(result.failure, "malformed_response")

    def test_invalid_utf8_esearch_is_malformed(self):
        client, _ = make_client([ok(b"\xff\xfe")])
        result = client.search("q", page_size=1, max_pages=1)

        self.assertFalse(result.complete)
        self.assertEqual(result.failure, "malformed_response")

    def test_invalid_utf8_efetch_is_malformed(self):
        script = [
            ok(esearch_body(1, [1], retstart=0, retmax=1)),
            ok(b"<PubmedArticleSet>\xff\xfe</PubmedArticleSet>"),
        ]
        client, _ = make_client(script)
        result = client.search("q", page_size=1, max_pages=1)

        self.assertFalse(result.complete)
        self.assertEqual(result.failure, "malformed_response")

    def test_duplicate_esearch_ids_are_malformed(self):
        script = [ok(esearch_body(2, [1, 1], retstart=0, retmax=2))]
        client, fake = make_client(script)
        result = client.search("q", page_size=2, max_pages=1)

        self.assertFalse(result.complete)
        self.assertEqual(result.failure, "malformed_response")
        self.assertEqual(len(fake.calls), 1)  # never reaches EFetch

    def test_idlist_longer_than_retmax_is_malformed(self):
        script = [ok(esearch_body(2, [1, 2], retstart=0, retmax=1))]
        client, _ = make_client(script)
        result = client.search("q", page_size=2, max_pages=1)

        self.assertFalse(result.complete)
        self.assertEqual(result.failure, "malformed_response")

    def test_overlong_title_is_malformed_not_truncated(self):
        long_title = "x" * (MAX_TITLE_LEN + 1)
        script = [
            ok(esearch_body(1, [1], retstart=0, retmax=1)),
            ok(efetch_body([{"pmid": 1, "title": long_title}])),
        ]
        client, _ = make_client(script)
        result = client.search("q", page_size=1, max_pages=1)

        self.assertFalse(result.complete)
        self.assertEqual(result.failure, "malformed_response")

    def test_overlong_author_list_is_malformed_not_truncated(self):
        authors = [{"last": f"a{i}"} for i in range(MAX_AUTHORS + 1)]
        script = [
            ok(esearch_body(1, [1], retstart=0, retmax=1)),
            ok(efetch_body([{"pmid": 1, "authors": authors}])),
        ]
        client, _ = make_client(script)
        result = client.search("q", page_size=1, max_pages=1)

        self.assertFalse(result.complete)
        self.assertEqual(result.failure, "malformed_response")

    def test_huge_year_digit_string_does_not_crash(self):
        raw = (
            "<PubmedArticleSet><PubmedArticle><MedlineCitation>"
            '<PMID Version="1">1</PMID>'
            "<Article><ArticleTitle>Big year</ArticleTitle>"
            "<Journal><JournalIssue><PubDate><Year>"
            + "1" * 5000
            + "</Year></PubDate></JournalIssue></Journal></Article>"
            "</MedlineCitation></PubmedArticle></PubmedArticleSet>"
        ).encode()
        script = [ok(esearch_body(1, [1], retstart=0, retmax=1)), ok(raw)]
        client, _ = make_client(script)
        result = client.search("q", page_size=1, max_pages=1)

        self.assertTrue(result.complete)
        self.assertIsNone(result.records[0].publication_year)


class QueryTranslationDriftTests(unittest.TestCase):
    def test_translation_drift_is_malformed_and_first_is_retained(self):
        script = [
            ok(esearch_body(4, [1, 2], retstart=0, retmax=2, translation="A[UID]")),
            ok(efetch_body([{"pmid": 1}, {"pmid": 2}])),
            ok(esearch_body(4, [3, 4], retstart=2, retmax=2, translation="B[UID]")),
        ]
        client, _ = make_client(script)
        result = client.search("q", page_size=2, max_pages=5)

        self.assertFalse(result.complete)
        self.assertEqual(result.failure, "malformed_response")
        self.assertEqual(result.query_translation, "A[UID]")

    def test_translation_stable_across_pages_is_complete(self):
        script = [
            ok(esearch_body(4, [1, 2], retstart=0, retmax=2, translation="A[UID]")),
            ok(efetch_body([{"pmid": 1}, {"pmid": 2}])),
            ok(esearch_body(4, [3, 4], retstart=2, retmax=2, translation="A[UID]")),
            ok(efetch_body([{"pmid": 3}, {"pmid": 4}])),
        ]
        client, _ = make_client(script)
        result = client.search("q", page_size=2, max_pages=5)

        self.assertTrue(result.complete)
        self.assertEqual(result.query_translation, "A[UID]")


if __name__ == "__main__":
    unittest.main()
