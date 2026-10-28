"""Stdlib-only PubMed ESearch + EFetch metadata search adapter (Step 05 slice 1, T2).

Authority
---------
``docs/implementation/05-discovery.md`` deliverables 2/4 and acceptance criteria
3/4/7 (reconstruct exact queries/timestamps/source/failures/counts/pages;
fixtures for pagination/overlap/missing IDs/partial failure; never hide an API
result cap as complete retrieval), ``docs/specification.md`` workflow steps 2-5
and the safety invariants ("treat documents/model outputs as untrusted data", no
credential leakage, bounded network handling), and
``docs/contracts/03-policy-decisions.md`` ``POL-OPS-COVERAGE`` (no universal
completeness claim; explicit provenance).

What it is
----------
* A bounded, deterministic **PubMed** adapter built on the shared
  :class:`~review_article_agent.discovery.transport.DiscoveryTransport` and the
  shared immutable value contracts
  (:class:`~review_article_agent.discovery.records.SearchResult` et al.). It
  performs one ESearch (``esearch.fcgi``, ``retmode=json``) per page to obtain a
  window of PMIDs, then one EFetch (``efetch.fcgi``, ``retmode=xml``) to obtain
  bibliographic metadata for exactly those PMIDs.
* **Bibliographic metadata only.** It parses the PMID, title (including nested
  markup), publication year, DOI, and PMCID. It never parses abstracts, full
  text, MeSH, or any other body content.
* **Explicit provenance.** Every page keeps the exact ESearch *and* EFetch
  :class:`~review_article_agent.discovery.transport.AttemptRecord` values, the
  exact transmitted query, the ESearch requested URL, and UTC timestamps.
* **Explicit partial outcomes.** A failure, a page/record budget, or the
  provider's own ESearch window is reported as an explicit ``failure`` /
  ``stop_reason``; it is never presented as a completed search.
* **No credentials, no paid calls.** It never sends an API key and never
  contacts anything except the allowlisted ESearch/EFetch endpoints (enforced by
  the shared transport).

What it is **not**
------------------
* Not a workflow, approval, or runner. It does not decide eligibility, does not
  freeze a corpus, and does not reconcile candidates across runs or providers
  (that is explicitly deferred). ``records`` is exactly the concatenation of the
  page records, so overlapping provider pages stay visible and are never
  silently deduplicated.
* Not a completeness claim. ``complete`` means *the bounded retrieval exhausted
  the provider's own result window*; it is never a claim that the literature was
  searched exhaustively, and it never implies accepted seed/approved context.
* Publication identity is not study identity: the adapter never equates a PMID
  with a study and never invents an identifier.

Source authority and disclosure
-------------------------------
The NCBI API index (https://www.ncbi.nlm.nih.gov/home/develop/api/) was fetched
by the coordinator. The E-utilities reference pages (NBK25499/NBK25497) were
**challenge-blocked** during that fetch, so the ESearch JSON shape implemented
here is taken from a tiny coordinator contract observation
(``esearch.fcgi?db=pubmed&term=31452104[uid]&retmax=1`` returning header
``type=esearch``/``version=0.3`` and an ``esearchresult`` with ``count``,
``retmax``, ``retstart``, ``idlist``, ``translationset``, and
``querytranslation``). The EFetch XML shape is parsed defensively and only for
the bibliographic fields named above. No live public API call is made by this
module's tests.
"""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

from ..domain.errors import ReviewAgentError
from .records import (
    MAX_PUBLICATION_YEAR,
    MAX_TITLE_LEN,
    MIN_PUBLICATION_YEAR,
    SOURCE_PUBMED,
    MetadataRecord,
    SearchPage,
    SearchResult,
)
from .transport import (
    AttemptRecord,
    DiscoveryTransport,
)

__all__ = [
    "PUBMED_ESEARCH_URL",
    "PUBMED_EFETCH_URL",
    "PUBMED_WINDOW_LIMIT",
    "DEFAULT_PAGE_SIZE",
    "MIN_PAGE_SIZE",
    "MAX_PAGE_SIZE",
    "DEFAULT_MAX_PAGES",
    "MIN_MAX_PAGES",
    "MAX_MAX_PAGES",
    "MAX_TOTAL_RECORDS",
    "MAX_QUERY_LEN",
    "PubMedAdapterError",
    "PubMedValidationError",
    "PubMedSearchResult",
    "PubMedClient",
]

# --------------------------------------------------------------------------- #
# Endpoints, bounds, and the provider window
# --------------------------------------------------------------------------- #

#: The two allowlisted NCBI E-utilities endpoints this adapter uses.
PUBMED_ESEARCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
PUBMED_EFETCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"

#: Conservative bound on the ESearch result window. NCBI ESearch only returns
#: roughly the first 9,999/10,000 records; a retrieval that would need to page
#: past this is reported as an explicit ``page_limit`` (never silently complete).
PUBMED_WINDOW_LIMIT = 9999

#: Modest, explicit page/record budgets (a hidden cap is never acceptable).
DEFAULT_PAGE_SIZE = 20
MIN_PAGE_SIZE = 1
MAX_PAGE_SIZE = 200
DEFAULT_MAX_PAGES = 5
MIN_MAX_PAGES = 1
MAX_MAX_PAGES = 100
#: Implicit memory cap: ``page_size * max_pages`` may never exceed this.
MAX_TOTAL_RECORDS = MAX_PAGE_SIZE * MAX_MAX_PAGES

#: Bound on the transmitted query text.
MAX_QUERY_LEN = 4096


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #
class PubMedAdapterError(ReviewAgentError):
    """Base class for every PubMed adapter failure."""


class PubMedValidationError(PubMedAdapterError, ValueError):
    """A locally-supplied query/page option is invalid (nothing was sent)."""


class _MalformedResponse(Exception):
    """Internal marker: the provider response was not the expected shape.

    Never escapes :meth:`PubMedClient.search`; it is translated into an explicit
    ``malformed_response`` page failure.
    """


# --------------------------------------------------------------------------- #
# Local validation helpers (no dependency on transport private helpers)
# --------------------------------------------------------------------------- #
def _require_bounded_int(value: object, field_name: str, *, minimum: int, maximum: int) -> int:
    """Validate a bounded integer; reject ``bool`` and non-integers."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise PubMedValidationError(f"{field_name}: must be an integer")
    if value < minimum or value > maximum:
        raise PubMedValidationError(f"{field_name}: is outside the permitted range")
    return value


def _require_query(value: object) -> str:
    """Validate the query text without altering it (the exact term is sent)."""
    if not isinstance(value, str):
        raise PubMedValidationError("query: must be a string")
    if not value.strip():
        raise PubMedValidationError("query: must be a non-empty string")
    if len(value) > MAX_QUERY_LEN:
        raise PubMedValidationError("query: is too long")
    for ch in value:
        if ord(ch) < 0x20 or ord(ch) == 0x7F:
            raise PubMedValidationError("query: must not contain control characters")
    return value


def _default_utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


# --------------------------------------------------------------------------- #
# Provider response parsing (defensive; every anomaly is _MalformedResponse)
# --------------------------------------------------------------------------- #
_DIGITS_RE = re.compile(r"\A[0-9]+\Z")
_YEAR_RE = re.compile(r"\b(\d{4})\b")
#: Reject any DTD/entity declaration before parsing so no external entity can be
#: fetched and no entity expansion can be triggered (stdlib ElementTree does not
#: resolve external entities, but a declaration is refused outright).
_UNSAFE_XML_RE = re.compile(r"<!\s*(ENTITY|DOCTYPE)", re.IGNORECASE)


def _parse_nonneg_int(value: object, field_name: str) -> int:
    """Parse a non-negative integer from a JSON string or int (never ``bool``)."""
    if isinstance(value, bool):
        raise _MalformedResponse(field_name)
    if isinstance(value, int):
        if value < 0:
            raise _MalformedResponse(field_name)
        return value
    if isinstance(value, str) and _DIGITS_RE.match(value):
        return int(value)
    raise _MalformedResponse(field_name)


@dataclass(frozen=True)
class _ESearchData:
    count: int
    idlist: tuple[str, ...]
    retmax: int
    retstart: int
    query_translation: str | None


def _parse_esearch(text: str, *, requested_offset: int, requested_retmax: int) -> _ESearchData:
    """Parse and validate one ESearch JSON response.

    An ``errorlist`` or a non-empty ``warninglist`` means the query translation
    was rejected or altered; it is refused so a translation-invalid query can
    never masquerade as a successful empty search.
    """
    try:
        payload = json.loads(text)
    except (ValueError, TypeError) as exc:
        raise _MalformedResponse("esearch json") from exc
    if not isinstance(payload, dict):
        raise _MalformedResponse("esearch json")
    result = payload.get("esearchresult")
    if not isinstance(result, dict):
        raise _MalformedResponse("esearchresult")
    for key in ("errorlist", "warninglist"):
        if result.get(key):
            raise _MalformedResponse(key)
    count = _parse_nonneg_int(result.get("count"), "count")
    retmax = _parse_nonneg_int(result.get("retmax"), "retmax")
    retstart = _parse_nonneg_int(result.get("retstart"), "retstart")
    if retstart != requested_offset:
        raise _MalformedResponse("retstart")
    if retmax > requested_retmax:
        raise _MalformedResponse("retmax")
    raw_ids = result.get("idlist")
    if not isinstance(raw_ids, list):
        raise _MalformedResponse("idlist")
    idlist: list[str] = []
    for item in raw_ids:
        if not isinstance(item, str) or not _DIGITS_RE.match(item):
            raise _MalformedResponse("idlist")
        idlist.append(item)
    if len(idlist) > requested_retmax:
        raise _MalformedResponse("idlist")
    if count == 0 and idlist:
        raise _MalformedResponse("idlist")
    if count > 0 and requested_offset + len(idlist) > count:
        raise _MalformedResponse("idlist")
    translation = result.get("querytranslation")
    if translation is not None and not isinstance(translation, str):
        raise _MalformedResponse("querytranslation")
    return _ESearchData(
        count=count,
        idlist=tuple(idlist),
        retmax=retmax,
        retstart=retstart,
        query_translation=translation or None,
    )


@dataclass(frozen=True)
class _FetchedArticle:
    pmid: str
    title: str | None
    publication_year: int | None
    doi: str | None
    pmcid: str | None


def _element_text(element: ET.Element | None) -> str | None:
    """Whitespace-normalized text of an element, including nested markup tails."""
    if element is None:
        return None
    text = " ".join("".join(element.itertext()).split())
    return text or None


def _find_text(parent: ET.Element | None, path: str) -> str | None:
    if parent is None:
        return None
    return _element_text(parent.find(path))


def _find_article_id(article_id_list: ET.Element | None, id_type: str) -> str | None:
    if article_id_list is None:
        return None
    for element in article_id_list.findall("ArticleId"):
        if element.get("IdType") == id_type:
            text = _element_text(element)
            if text:
                return text
    return None


def _find_elocation_doi(citation: ET.Element | None) -> str | None:
    if citation is None:
        return None
    for element in citation.findall("Article/ELocationID"):
        if element.get("EIdType") == "doi":
            text = _element_text(element)
            if text:
                return text
    return None


def _parse_publication_year(citation: ET.Element | None) -> int | None:
    """Parse a 4-digit publication year; absent/out-of-range stays ``None``."""
    year_text = _find_text(citation, "Article/Journal/JournalIssue/PubDate/Year")
    if year_text is not None and _DIGITS_RE.match(year_text):
        year = int(year_text)
        if MIN_PUBLICATION_YEAR <= year <= MAX_PUBLICATION_YEAR:
            return year
    medline = _find_text(citation, "Article/Journal/JournalIssue/PubDate/MedlineDate")
    if medline is not None:
        match = _YEAR_RE.search(medline)
        if match is not None:
            year = int(match.group(1))
            if MIN_PUBLICATION_YEAR <= year <= MAX_PUBLICATION_YEAR:
                return year
    return None


def _parse_efetch(text: str) -> tuple[_FetchedArticle, ...]:
    """Parse a ``PubmedArticleSet`` into bibliographic articles only."""
    if _UNSAFE_XML_RE.search(text):
        raise _MalformedResponse("unsafe xml declaration")
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise _MalformedResponse("xml") from exc
    if root.tag != "PubmedArticleSet":
        raise _MalformedResponse("xml root")
    articles: list[_FetchedArticle] = []
    for article in root.findall("PubmedArticle"):
        citation = article.find("MedlineCitation")
        pmid = _find_text(citation, "PMID")
        if pmid is None or not _DIGITS_RE.match(pmid):
            raise _MalformedResponse("pmid")
        title = _find_text(citation, "Article/ArticleTitle")
        if title is not None and len(title) > MAX_TITLE_LEN:
            title = title[:MAX_TITLE_LEN]
        pubmed_data = article.find("PubmedData")
        article_id_list = (
            pubmed_data.find("ArticleIdList") if pubmed_data is not None else None
        )
        doi = _find_article_id(article_id_list, "doi")
        if doi is None:
            doi = _find_elocation_doi(citation)
        pmcid = _find_article_id(article_id_list, "pmc")
        articles.append(
            _FetchedArticle(
                pmid=pmid,
                title=title,
                publication_year=_parse_publication_year(citation),
                doi=doi,
                pmcid=pmcid,
            )
        )
    return tuple(articles)


# --------------------------------------------------------------------------- #
# Result subtype (retains the provider query translation without a shared change)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class PubMedSearchResult(SearchResult):
    """A :class:`SearchResult` that also retains the provider's query translation.

    ``query_translation`` is the exact ``querytranslation`` string ESearch
    returned for the first page (``None`` when the provider omitted it). It is
    **informational**: the exact transmitted query remains ``query`` and the
    exact transmitted term remains in the ESearch attempt URL.
    """

    query_translation: str | None = None

    @classmethod
    def from_pages(
        cls,
        *,
        source: str,
        query: str,
        pages,
        started_at: str,
        completed_at: str,
        stop_reason: str,
        total_count: int | None = None,
        failure: str | None = None,
        query_translation: str | None = None,
    ) -> "PubMedSearchResult":
        """Assemble a result, deriving records/attempts/``complete`` from pages."""
        page_tuple = tuple(pages)
        records = tuple(record for page in page_tuple for record in page.records)
        attempts = tuple(attempt for page in page_tuple for attempt in page.attempts)
        complete = stop_reason == "completed" and failure is None
        return cls(
            source=source,
            query=query,
            pages=page_tuple,
            records=records,
            started_at=started_at,
            completed_at=completed_at,
            stop_reason=stop_reason,
            total_count=total_count,
            complete=complete,
            failure=failure,
            attempts=attempts,
            query_translation=query_translation,
        )


# --------------------------------------------------------------------------- #
# The adapter
# --------------------------------------------------------------------------- #
class PubMedClient:
    """A bounded PubMed ESearch+EFetch metadata search adapter.

    ``transport`` is the shared :class:`DiscoveryTransport` (or any object with
    the same ``get(url, params) -> RetrievalResult`` contract). When omitted, a
    real :class:`DiscoveryTransport` is constructed; it performs **no** network
    I/O until :meth:`search` is called, so importing or constructing this class
    never touches the network. ``utcnow`` is the injected UTC clock used for the
    page/result timestamps.

    ``page_size``/``max_pages`` are the default budgets; :meth:`search` may
    override them per call. ``window_limit`` is the provider ESearch window and
    may only be lowered (never raised above :data:`PUBMED_WINDOW_LIMIT`), so a
    caller can never hide a larger cap.
    """

    def __init__(
        self,
        *,
        transport: object = None,
        utcnow: Callable[[], str] | None = None,
        page_size: int = DEFAULT_PAGE_SIZE,
        max_pages: int = DEFAULT_MAX_PAGES,
        window_limit: int = PUBMED_WINDOW_LIMIT,
    ) -> None:
        self._page_size = _require_bounded_int(
            page_size, "page_size", minimum=MIN_PAGE_SIZE, maximum=MAX_PAGE_SIZE
        )
        self._max_pages = _require_bounded_int(
            max_pages, "max_pages", minimum=MIN_MAX_PAGES, maximum=MAX_MAX_PAGES
        )
        if self._page_size * self._max_pages > MAX_TOTAL_RECORDS:
            raise PubMedValidationError(
                "page_size * max_pages exceeds the total record cap"
            )
        self._window_limit = _require_bounded_int(
            window_limit, "window_limit", minimum=1, maximum=PUBMED_WINDOW_LIMIT
        )
        if utcnow is None:
            utcnow = _default_utcnow
        if not callable(utcnow):
            raise PubMedValidationError("utcnow must be callable")
        self._utcnow = utcnow
        if transport is None:
            transport = DiscoveryTransport()
        if not callable(getattr(transport, "get", None)):
            raise PubMedValidationError("transport must expose a callable get()")
        self._transport = transport

    # -- read-only view of the validated bounds ----------------------------- #
    @property
    def page_size(self) -> int:
        return self._page_size

    @property
    def max_pages(self) -> int:
        return self._max_pages

    @property
    def window_limit(self) -> int:
        return self._window_limit

    # -- the single public entry point -------------------------------------- #
    def search(
        self,
        query: object,
        page_size: object = None,
        max_pages: object = None,
    ) -> PubMedSearchResult:
        """Run a bounded PubMed search and return an immutable result.

        The query and the page/record budgets are validated **before** any
        request, so an invalid argument performs zero transport calls. Each page
        is one ESearch followed by one EFetch; the page records are exactly the
        bibliographic records EFetch returned for the requested PMIDs.

        ``complete`` is ``True`` only when the retrieval exhausted the provider's
        own result window with no failure. A page/record budget or the provider
        window yields ``stop_reason == "page_limit"``; a provider/transport
        failure yields ``stop_reason == "failure"`` with a static ``failure``.
        Neither is ever reported as a completed search.
        """
        query = _require_query(query)
        resolved_page_size = (
            self._page_size
            if page_size is None
            else _require_bounded_int(
                page_size, "page_size", minimum=MIN_PAGE_SIZE, maximum=MAX_PAGE_SIZE
            )
        )
        resolved_max_pages = (
            self._max_pages
            if max_pages is None
            else _require_bounded_int(
                max_pages, "max_pages", minimum=MIN_MAX_PAGES, maximum=MAX_MAX_PAGES
            )
        )
        if resolved_page_size * resolved_max_pages > MAX_TOTAL_RECORDS:
            raise PubMedValidationError(
                "page_size * max_pages exceeds the total record cap"
            )

        started_at = self._utcnow()
        pages: list[SearchPage] = []
        seen: set[str] = set()
        expected_count: int | None = None
        query_translation: str | None = None
        total_count: int | None = None
        offset = 0
        stop_reason = "completed"
        failure: str | None = None

        for page_index in range(resolved_max_pages):
            if offset >= self._window_limit:
                # The provider window is exhausted before the claimed total.
                stop_reason = "page_limit"
                break
            page, expected_count, query_translation, seen, next_offset = self._fetch_page(
                query=query,
                offset=offset,
                page_size=resolved_page_size,
                page_index=page_index,
                seen=seen,
                expected_count=expected_count,
                query_translation=query_translation,
            )
            pages.append(page)
            if page.total_count is not None:
                total_count = page.total_count
            if page.failure is not None:
                stop_reason = "failure"
                failure = page.failure
                break
            if next_offset is None:
                stop_reason = "completed"
                break
            offset = next_offset
        else:
            # The page budget ran out before the provider window was exhausted.
            stop_reason = "page_limit"

        completed_at = self._utcnow()
        return PubMedSearchResult.from_pages(
            source=SOURCE_PUBMED,
            query=query,
            pages=pages,
            started_at=started_at,
            completed_at=completed_at,
            stop_reason=stop_reason,
            total_count=total_count,
            failure=failure,
            query_translation=query_translation,
        )

    # -- internals ---------------------------------------------------------- #
    @staticmethod
    def _failed_page(
        query: str,
        requested_url: str,
        retrieved_at: str,
        page_index: int,
        page_size: int,
        attempts: list[AttemptRecord],
        failure: str,
        total_count: int | None = None,
    ) -> SearchPage:
        return SearchPage(
            source=SOURCE_PUBMED,
            query=query,
            requested_url=requested_url,
            retrieved_at=retrieved_at,
            page_index=page_index,
            records=(),
            page_size=page_size,
            total_count=total_count,
            next_cursor=None,
            complete=False,
            stop_reason="failure",
            failure=failure,
            attempts=tuple(attempts),
        )

    def _fetch_page(
        self,
        *,
        query: str,
        offset: int,
        page_size: int,
        page_index: int,
        seen: set[str],
        expected_count: int | None,
        query_translation: str | None,
    ) -> tuple[SearchPage, int | None, str | None, set[str], int | None]:
        """Fetch one ESearch+EFetch page.

        Returns ``(page, expected_count, query_translation, seen, next_offset)``.
        ``next_offset`` is ``None`` when the page failed or reached a natural end.
        """
        retrieved_at = self._utcnow()
        esearch_params = {
            "db": "pubmed",
            "retmode": "json",
            "term": query,
            "retstart": offset,
            "retmax": page_size,
        }
        esearch = self._transport.get(PUBMED_ESEARCH_URL, esearch_params)
        attempts: list[AttemptRecord] = list(esearch.attempts)
        if not esearch.ok:
            page = self._failed_page(
                query, esearch.requested_url, retrieved_at, page_index, page_size,
                attempts, esearch.failure,
            )
            return page, expected_count, query_translation, seen, None
        try:
            data = _parse_esearch(
                esearch.text(), requested_offset=offset, requested_retmax=page_size
            )
        except _MalformedResponse:
            page = self._failed_page(
                query, esearch.requested_url, retrieved_at, page_index, page_size,
                attempts, "malformed_response",
            )
            return page, expected_count, query_translation, seen, None

        translation = data.query_translation or query_translation
        if expected_count is None:
            expected_count = data.count
        elif data.count != expected_count:
            # The provider's total changed mid-run: never report this complete.
            page = self._failed_page(
                query, esearch.requested_url, retrieved_at, page_index, page_size,
                attempts, "malformed_response", total_count=data.count,
            )
            return page, expected_count, translation, seen, None

        if data.count == 0:
            page = SearchPage(
                source=SOURCE_PUBMED, query=query,
                requested_url=esearch.requested_url, retrieved_at=retrieved_at,
                page_index=page_index, records=(), page_size=page_size,
                total_count=0, next_cursor=None, complete=True,
                stop_reason="completed", failure=None, attempts=tuple(attempts),
            )
            return page, expected_count, translation, seen, None

        if not data.idlist:
            # An empty page before the claimed total is never a natural end.
            page = self._failed_page(
                query, esearch.requested_url, retrieved_at, page_index, page_size,
                attempts, "malformed_response", total_count=data.count,
            )
            return page, expected_count, translation, seen, None

        efetch = self._transport.get(
            PUBMED_EFETCH_URL,
            {"db": "pubmed", "retmode": "xml", "id": ",".join(data.idlist)},
        )
        attempts.extend(efetch.attempts)
        if not efetch.ok:
            page = self._failed_page(
                query, esearch.requested_url, retrieved_at, page_index, page_size,
                attempts, efetch.failure, total_count=data.count,
            )
            return page, expected_count, translation, seen, None
        try:
            fetched = _parse_efetch(efetch.text())
        except _MalformedResponse:
            page = self._failed_page(
                query, esearch.requested_url, retrieved_at, page_index, page_size,
                attempts, "malformed_response", total_count=data.count,
            )
            return page, expected_count, translation, seen, None

        requested_set = set(data.idlist)
        returned_ids = [article.pmid for article in fetched]
        returned_set = set(returned_ids)
        anomaly = (
            len(returned_ids) != len(returned_set)  # duplicate within one page
            or returned_set != requested_set  # missing and/or unrequested IDs
            or any(pmid in seen for pmid in returned_set)  # overlap with a prior page
        )
        records = tuple(
            MetadataRecord(
                source=SOURCE_PUBMED,
                source_id=article.pmid,
                pmid=article.pmid,
                pmcid=article.pmcid,
                doi=article.doi,
                title=article.title,
                publication_year=article.publication_year,
            )
            for article in fetched
            if article.pmid in requested_set
        )
        next_offset = offset + len(data.idlist)
        if anomaly:
            # Keep the observations visible, but never claim this page complete.
            page = SearchPage(
                source=SOURCE_PUBMED, query=query,
                requested_url=esearch.requested_url, retrieved_at=retrieved_at,
                page_index=page_index, records=records, page_size=page_size,
                total_count=data.count, next_cursor=None, complete=False,
                stop_reason="failure", failure="malformed_response",
                attempts=tuple(attempts),
            )
            return page, expected_count, translation, seen, None

        updated_seen = set(seen)
        updated_seen.update(data.idlist)
        next_cursor = str(next_offset) if next_offset < data.count else None
        page = SearchPage(
            source=SOURCE_PUBMED, query=query,
            requested_url=esearch.requested_url, retrieved_at=retrieved_at,
            page_index=page_index, records=records, page_size=page_size,
            total_count=data.count, next_cursor=next_cursor, complete=True,
            stop_reason="completed", failure=None, attempts=tuple(attempts),
        )
        return (
            page,
            expected_count,
            translation,
            updated_seen,
            next_offset if next_cursor is not None else None,
        )
