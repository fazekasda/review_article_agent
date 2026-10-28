"""stdlib-only Europe PMC cursor-paginated metadata search adapter (Step 05 slice 2).

Authority
---------

``docs/implementation/05-discovery.md`` deliverables 2/4 and acceptance criteria
3/4/7 (reconstruct the exact query, timestamps, contributing sources, failures,
counts, and pages from records; fixed response fixtures exercise pagination,
overlap, missing IDs, and partial failures; no hidden result cap is reported as
complete retrieval), ``docs/specification.md`` workflow steps 2-5 and the safety
invariants (treat provider data as untrusted; bounded network handling; never
promise all papers), contract C01 (a publication identifier is **not** a study
identifier), and contract C03 ``POL-OPS-COVERAGE`` (a coverage report records
sources/queries/dates/yields; reporting never claims absolute completeness).

What this is
------------

* :class:`EuropePMCClient` — a small, deterministic **library** adapter over the
  shared :class:`~review_article_agent.discovery.transport.DiscoveryTransport`.
  It performs one explicit Europe PMC REST ``search`` call per page against the
  allowlisted ``https://www.ebi.ac.uk/europepmc/webservices/rest/search``
  endpoint, requesting ``format=json`` and ``resultType=lite`` (metadata only —
  no abstract), with ``pageSize`` bounded to ``<= 1000`` and cursor pagination
  starting at ``*`` and following ``nextCursorMark``.
* It returns the shared immutable :class:`~review_article_agent.discovery.records.SearchResult`
  (and its :class:`~review_article_agent.discovery.records.SearchPage` /
  :class:`~review_article_agent.discovery.records.MetadataRecord` values) so the
  exact query, counts, UTC times, requested URLs, and **every** transport attempt
  are retained as provenance.
* Provider identity is captured unambiguously: the shared ``source`` stays
  :data:`~review_article_agent.discovery.records.SOURCE_EUROPEPMC` while
  ``source_id`` encodes the native source and id (for example ``MED:31452104``,
  ``PMC:PMC123``, ``PPR:PPR123``), so a Europe PMC ``MED`` record and a
  ``PMC``/preprint record can never alias a bare numeric id. ``pmid``/``pmcid``/
  ``doi``/``title``/``publication_year`` stay nullable; a missing identifier is
  explicit and no candidate id is ever invented.

What this is **not**
--------------------

* **Not scientific context, eligibility, or an approval.** It returns provider
  *observations*. Untrusted metadata is never accepted project context.
* **Not identity reconciliation.** Cross-run and cross-provider scientific
  candidate reconciliation is **deferred**; records are flattened exactly as the
  pages returned them, so overlap stays visible and never creates a false
  completeness claim.
* **Not a completeness claim.** ``complete`` means *the bounded retrieval reached
  a natural end consistent with the provider's own ``hitCount`` and cursor
  signal*. A page limit or any anomaly is an explicit incomplete outcome.
* **Not a broad-search authorization runner or a CLI.** It is a library only; a
  later Step 05 task owns scope/query-plan approval and workflow integration.

Explicit incomplete outcomes
----------------------------

A malformed or untrustworthy response never becomes a silent success. A missing
or wrong-typed field, a changed ``hitCount``, an empty page that still offers a
cursor, a repeated/looping cursor, an over-long cursor, or a final page whose
collected record count disagrees with ``hitCount`` yields an explicit
``malformed_response`` failure page and an incomplete result. A transport failure
retains the earlier successful pages plus the failed page's full attempt
provenance. Reaching ``max_pages`` while the provider still offers a cursor yields
``page_limit`` and is never ``complete``.

No network, no credential, and no paid call happen here: the injected transport
owns bounded retries, rate handling, and the allowlist.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Callable

from .records import (
    MAX_PUBLICATION_YEAR,
    MAX_TITLE_LEN,
    MIN_PUBLICATION_YEAR,
    SOURCE_EUROPEPMC,
    DiscoveryRecordError,
    MetadataRecord,
    SearchPage,
    SearchResult,
)
from .transport import (
    AttemptRecord,
    DiscoveryTransport,
    DiscoveryValidationError,
)

__all__ = [
    "EPMC_SEARCH_URL",
    "EPMC_FORMAT",
    "EPMC_RESULT_TYPE",
    "EPMC_INITIAL_CURSOR",
    "MIN_PAGE_SIZE",
    "MAX_PAGE_SIZE",
    "DEFAULT_PAGE_SIZE",
    "MIN_MAX_PAGES",
    "MAX_MAX_PAGES",
    "DEFAULT_MAX_PAGES",
    "MAX_QUERY_LEN",
    "MAX_CURSOR_LEN",
    "MAX_ID_LEN",
    "EuropePMCClient",
]

#: The one allowlisted Europe PMC REST endpoint this adapter may call.
EPMC_SEARCH_URL = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"

#: Fixed request parameters: JSON, metadata-only (``lite``), cursor pagination.
EPMC_FORMAT = "json"
EPMC_RESULT_TYPE = "lite"
EPMC_INITIAL_CURSOR = "*"

#: Bounded page size (Europe PMC caps ``pageSize`` at 1000).
MIN_PAGE_SIZE = 1
MAX_PAGE_SIZE = 1000
DEFAULT_PAGE_SIZE = 100

#: Bounded page budget. Reaching it is an explicit ``page_limit``, never complete.
MIN_MAX_PAGES = 1
MAX_MAX_PAGES = 1000
DEFAULT_MAX_PAGES = 10

#: Bounds on untrusted request/response strings.
MAX_QUERY_LEN = 4096
MAX_CURSOR_LEN = 4096
MAX_ID_LEN = 256


# --------------------------------------------------------------------------- #
# Errors and small validators
# --------------------------------------------------------------------------- #
class _MalformedResponse(Exception):
    """Internal: a provider response cannot be trusted as a complete page.

    The message is a static, peer-text-free diagnostic; it is never surfaced as
    peer text (the caller records the static ``malformed_response`` reason).
    """


def _default_utcnow() -> str:
    """The default UTC clock: an ISO-8601 timestamp (injectable for fixtures)."""
    return datetime.now(timezone.utc).isoformat()


def _require_bounded_int(
    value: object, field_name: str, *, minimum: int, maximum: int
) -> int:
    """Validate a bounded integer; reject ``bool`` and non-integers.

    A ``float`` (including ``NaN``/``inf``) is rejected because it is not an
    ``int``, so a malformed numeric option can never reach the network.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise DiscoveryValidationError(f"{field_name}: must be an integer")
    if value < minimum or value > maximum:
        raise DiscoveryValidationError(f"{field_name}: is outside the permitted range")
    return value


def _validate_query(query: object) -> str:
    """Validate the exact query string before any request is made."""
    if not isinstance(query, str) or not query:
        raise DiscoveryValidationError("query: must be a non-empty string")
    if len(query) > MAX_QUERY_LEN:
        raise DiscoveryValidationError("query: is too long")
    if not query.strip():
        raise DiscoveryValidationError("query: must not be blank")
    for ch in query:
        if ord(ch) < 0x20 or ord(ch) == 0x7F:
            raise DiscoveryValidationError(
                "query: must not contain control characters"
            )
    return query


def _optional_identifier(item: dict[str, Any], key: str) -> str | None:
    """Read a nullable provider identifier; absent/empty stays ``None``.

    A present value that is not a safe identifier (wrong type, whitespace,
    control characters, or over-long) is malformed rather than silently dropped.
    """
    value = item.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise _MalformedResponse(f"{key}: not a string")
    if value == "":
        return None
    if len(value) > MAX_ID_LEN:
        raise _MalformedResponse(f"{key}: too long")
    if value != value.strip() or any(ch.isspace() for ch in value):
        raise _MalformedResponse(f"{key}: contains whitespace")
    for ch in value:
        if ord(ch) < 0x20 or ord(ch) == 0x7F:
            raise _MalformedResponse(f"{key}: contains control characters")
    return value


def _optional_title(item: dict[str, Any], key: str) -> str | None:
    """Read a nullable title; absent/empty stays ``None``."""
    value = item.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise _MalformedResponse(f"{key}: not a string")
    if value == "":
        return None
    if len(value) > MAX_TITLE_LEN:
        raise _MalformedResponse(f"{key}: too long")
    return value


def _optional_year(item: dict[str, Any], key: str) -> int | None:
    """Read a nullable publication year; absent/empty stays ``None``."""
    value = item.get(key)
    if value is None:
        return None
    if isinstance(value, bool):
        raise _MalformedResponse(f"{key}: not a year")
    if isinstance(value, int):
        year = value
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if not text.isdigit():
            raise _MalformedResponse(f"{key}: not a year")
        try:
            year = int(text)
        except (TypeError, ValueError):
            raise _MalformedResponse(f"{key}: not a year") from None
    else:
        raise _MalformedResponse(f"{key}: not a year")
    if year < MIN_PUBLICATION_YEAR or year > MAX_PUBLICATION_YEAR:
        raise _MalformedResponse(f"{key}: outside the permitted range")
    return year


def _native_source(item: dict[str, Any]) -> str:
    """Read the required native provider source (``MED``/``PMC``/``PPR``/...)."""
    value = item.get("source")
    if not isinstance(value, str):
        raise _MalformedResponse("source: missing")
    value = value.strip()
    if not value or len(value) > MAX_ID_LEN:
        raise _MalformedResponse("source: invalid")
    if any(ch.isspace() for ch in value) or "/" in value or "\\" in value:
        raise _MalformedResponse("source: invalid")
    for ch in value:
        if ord(ch) < 0x20 or ord(ch) == 0x7F:
            raise _MalformedResponse("source: invalid")
    return value


def _parse_page(body: bytes) -> tuple[int, tuple[MetadataRecord, ...], str | None]:
    """Parse one Europe PMC ``lite`` page or raise :class:`_MalformedResponse`.

    Returns ``(hit_count, records, next_cursor)``. The native source and id are
    composed into an unambiguous ``source_id`` (``MED:31452104``) so native
    sources cannot alias a bare numeric id. A record with no identifier is
    returned with ``source_id``/``pmid``/``pmcid``/``doi`` all ``None`` — explicit
    and never fabricated.
    """
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _MalformedResponse("body: not utf-8") from exc
    try:
        payload = json.loads(text)
    except (json.JSONDecodeError, ValueError) as exc:
        raise _MalformedResponse("body: not json") from exc
    if not isinstance(payload, dict):
        raise _MalformedResponse("body: not an object")

    hit_count = payload.get("hitCount")
    if isinstance(hit_count, bool) or not isinstance(hit_count, int) or hit_count < 0:
        raise _MalformedResponse("hitCount: missing or invalid")

    result_list = payload.get("resultList")
    if not isinstance(result_list, dict):
        raise _MalformedResponse("resultList: missing or invalid")
    results = result_list.get("result")
    if not isinstance(results, list):
        raise _MalformedResponse("resultList.result: missing or invalid")

    raw_cursor = payload.get("nextCursorMark")
    if raw_cursor is None:
        next_cursor: str | None = None
    elif isinstance(raw_cursor, str) and raw_cursor:
        if len(raw_cursor) > MAX_CURSOR_LEN:
            raise _MalformedResponse("nextCursorMark: too long")
        next_cursor = raw_cursor
    else:
        raise _MalformedResponse("nextCursorMark: invalid")

    records: list[MetadataRecord] = []
    for item in results:
        if not isinstance(item, dict):
            raise _MalformedResponse("result entry: not an object")
        source = _native_source(item)
        native_id = _optional_identifier(item, "id")
        source_id = f"{source}:{native_id}" if native_id is not None else None
        if source_id is not None and len(source_id) > MAX_ID_LEN:
            raise _MalformedResponse("id: too long")
        try:
            records.append(
                MetadataRecord(
                    source=SOURCE_EUROPEPMC,
                    source_id=source_id,
                    pmid=_optional_identifier(item, "pmid"),
                    pmcid=_optional_identifier(item, "pmcid"),
                    doi=_optional_identifier(item, "doi"),
                    title=_optional_title(item, "title"),
                    publication_year=_optional_year(item, "pubYear"),
                )
            )
        except DiscoveryRecordError as exc:
            raise _MalformedResponse("record: invalid") from exc
    return hit_count, tuple(records), next_cursor


def _failed_page(
    *,
    query: str,
    requested_url: str,
    retrieved_at: str,
    page_index: int,
    page_size: int,
    total_count: int | None,
    failure: str,
    attempts: tuple[AttemptRecord, ...],
    records: tuple[MetadataRecord, ...] = (),
) -> SearchPage:
    """Build an explicit failed page (never ``complete``).

    ``records`` is empty for an unparseable page. When a page parsed but its
    pagination was untrustworthy (a changed ``hitCount``, a repeated cursor, or a
    final count mismatch), its already-observed records are retained so overlap
    and the observations stay visible; the page is still explicitly incomplete.
    """
    return SearchPage(
        source=SOURCE_EUROPEPMC,
        query=query,
        requested_url=requested_url,
        retrieved_at=retrieved_at,
        page_index=page_index,
        records=records,
        page_size=page_size,
        total_count=total_count,
        next_cursor=None,
        complete=False,
        stop_reason="failure",
        failure=failure,
        attempts=attempts,
    )


# --------------------------------------------------------------------------- #
# The adapter
# --------------------------------------------------------------------------- #
class EuropePMCClient:
    """A bounded Europe PMC ``search`` adapter over the shared transport.

    ``transport`` is an injected :class:`DiscoveryTransport` (it owns the fixed
    allowlist, retries, rate handling, size cap, and no-credential policy).
    ``utcnow`` is an injectable UTC clock returning an ISO-8601 string, so page
    and result timestamps are deterministic in fixtures.

    :meth:`search` validates the query, ``page_size`` (``1..1000``), and
    ``max_pages`` (``1..1000``) **before** any request, so an invalid parameter
    causes zero network activity. It returns a shared immutable
    :class:`SearchResult`; a page limit or any anomaly is an explicit incomplete
    outcome, never a silent success.
    """

    def __init__(
        self,
        transport: DiscoveryTransport,
        *,
        utcnow: Callable[[], str] = _default_utcnow,
    ) -> None:
        if not callable(getattr(transport, "get", None)):
            raise DiscoveryValidationError(
                "transport: must be a DiscoveryTransport with a get method"
            )
        if not callable(utcnow):
            raise DiscoveryValidationError("utcnow: must be callable")
        self._transport = transport
        self._utcnow = utcnow

    @property
    def transport(self) -> DiscoveryTransport:
        """The injected transport (read-only)."""
        return self._transport

    def search(
        self,
        query: str,
        page_size: int = DEFAULT_PAGE_SIZE,
        max_pages: int = DEFAULT_MAX_PAGES,
    ) -> SearchResult:
        """Run one bounded, cursor-paginated Europe PMC metadata search.

        The exact ``query`` is sent unchanged with ``format=json``,
        ``resultType=lite``, the bounded ``pageSize``, and ``cursorMark`` starting
        at ``*`` and then following ``nextCursorMark``. Every page records the
        exact requested URL, the UTC retrieval time, the provider ``hitCount``,
        the page records, the next cursor, and every transport attempt.

        Completion requires the provider to signal the natural end (no
        ``nextCursorMark``) **and** the collected record count to equal the stable
        ``hitCount``. A changed ``hitCount``, an empty page that still offers a
        cursor, a repeated/looping/over-long cursor, a malformed or missing field,
        a final count mismatch (including overlap), a transport failure, or
        reaching ``max_pages`` all yield an explicit incomplete result.
        """
        validated_query = _validate_query(query)
        validated_page_size = _require_bounded_int(
            page_size, "page_size", minimum=MIN_PAGE_SIZE, maximum=MAX_PAGE_SIZE
        )
        validated_max_pages = _require_bounded_int(
            max_pages, "max_pages", minimum=MIN_MAX_PAGES, maximum=MAX_MAX_PAGES
        )

        started_at = self._utcnow()
        pages: list[SearchPage] = []
        collected: list[MetadataRecord] = []
        cursor = EPMC_INITIAL_CURSOR
        seen_cursors = {EPMC_INITIAL_CURSOR}
        hit_count: int | None = None
        stop_reason: str | None = None
        failure: str | None = None

        for page_index in range(validated_max_pages):
            retrieval = self._transport.get(
                EPMC_SEARCH_URL,
                {
                    "query": validated_query,
                    "format": EPMC_FORMAT,
                    "resultType": EPMC_RESULT_TYPE,
                    "pageSize": validated_page_size,
                    "cursorMark": cursor,
                },
            )
            retrieved_at = self._utcnow()

            if not retrieval.ok:
                pages.append(
                    _failed_page(
                        query=validated_query,
                        requested_url=retrieval.requested_url,
                        retrieved_at=retrieved_at,
                        page_index=page_index,
                        page_size=validated_page_size,
                        total_count=hit_count,
                        failure=retrieval.failure,
                        attempts=retrieval.attempts,
                    )
                )
                stop_reason = "failure"
                failure = retrieval.failure
                break

            try:
                page_hit_count, page_records, next_cursor = _parse_page(
                    retrieval.body
                )
            except _MalformedResponse:
                pages.append(
                    _failed_page(
                        query=validated_query,
                        requested_url=retrieval.requested_url,
                        retrieved_at=retrieved_at,
                        page_index=page_index,
                        page_size=validated_page_size,
                        total_count=hit_count,
                        failure="malformed_response",
                        attempts=retrieval.attempts,
                    )
                )
                stop_reason = "failure"
                failure = "malformed_response"
                break

            if hit_count is None:
                hit_count = page_hit_count

            anomaly: str | None = None
            if page_hit_count != hit_count:
                anomaly = "hitCount changed between pages"
            elif not page_records and next_cursor is not None:
                anomaly = "empty page with a next cursor"
            elif next_cursor is not None and next_cursor in seen_cursors:
                anomaly = "cursor repeated"
            elif (
                next_cursor is None
                and len(collected) + len(page_records) != hit_count
            ):
                anomaly = "final count disagrees with hitCount"

            if anomaly is not None:
                pages.append(
                    _failed_page(
                        query=validated_query,
                        requested_url=retrieval.requested_url,
                        retrieved_at=retrieved_at,
                        page_index=page_index,
                        page_size=validated_page_size,
                        total_count=hit_count,
                        failure="malformed_response",
                        attempts=retrieval.attempts,
                        records=page_records,
                    )
                )
                stop_reason = "failure"
                failure = "malformed_response"
                break

            pages.append(
                SearchPage(
                    source=SOURCE_EUROPEPMC,
                    query=validated_query,
                    requested_url=retrieval.requested_url,
                    retrieved_at=retrieved_at,
                    page_index=page_index,
                    records=page_records,
                    page_size=validated_page_size,
                    total_count=hit_count,
                    next_cursor=next_cursor,
                    complete=True,
                    stop_reason=None,
                    failure=None,
                    attempts=retrieval.attempts,
                )
            )
            collected.extend(page_records)

            if next_cursor is None:
                stop_reason = "completed"
                break
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        else:
            # The provider still offered a cursor when the page budget ran out.
            stop_reason = "page_limit"

        completed_at = self._utcnow()
        return SearchResult.from_pages(
            source=SOURCE_EUROPEPMC,
            query=validated_query,
            pages=pages,
            started_at=started_at,
            completed_at=completed_at,
            stop_reason=stop_reason,
            total_count=hit_count,
            failure=failure,
        )
