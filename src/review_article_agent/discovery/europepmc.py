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
  ``doi``/``title``/``publication_year``/``journal`` stay nullable and
  ``authors`` is an ordered display-name list; a missing identifier is explicit
  and no candidate id is ever invented.

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

Terminal cursor semantics
-------------------------

Europe PMC's ``cursorMark``/``nextCursorMark`` pagination is Solr-derived. The
documented terminal signal is either an **absent** ``nextCursorMark`` or a
``nextCursorMark`` **equal to the cursorMark that was sent** (unchanged, possibly
with an empty final page); Solr's guide states the returned ``nextCursorMark``
matches the ``cursorMark`` used once there are no more documents. This adapter
therefore treats both as terminal, and also completes when the collected count
exactly equals the stable ``hitCount`` even if the provider still offers an
advancing cursor — every declared hit has been collected exactly once, so there
is no truncation. A missing or repeating cursor *before* the declared total
stays an explicit incomplete ``malformed_response``.

Explicit incomplete outcomes
----------------------------

A malformed or untrustworthy response never becomes a silent success. A missing
or wrong-typed field, an over-long cursor, deeply nested JSON that exhausts the
parser, a changed ``hitCount``, a page larger than the requested ``pageSize``, a
repeated provider identity (within or across pages), more collected records than
``hitCount``, an empty page that still offers an advancing cursor, a cursor that
loops to a previous value, or a terminal page whose collected count disagrees
with ``hitCount`` yields an explicit ``malformed_response`` failure page and an
incomplete result. A transport failure retains the earlier successful pages plus
the failed page's full attempt provenance. Reaching ``max_pages`` while the
provider still offers a cursor yields ``page_limit`` and is never ``complete``.

Request boundary
----------------

This module opens no socket itself and makes no implicit or automatic request:
every request is one **explicit** library :meth:`EuropePMCClient.search` call
routed through the injected transport, which owns the fixed allowlist, bounded
retries, rate handling, the response-size cap, and the no-credential policy.
Given a real :class:`~review_article_agent.discovery.transport.DiscoveryTransport`
the adapter therefore *does* issue public, unauthenticated GETs to Europe PMC;
the committed tests inject a scripted transport, so the test suite makes no
network call. No credential and no paid call are ever used.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any, Callable

from .records import (
    MAX_AUTHORS,
    MAX_AUTHOR_LEN,
    MAX_JOURNAL_LEN,
    MAX_PUBLICATION_YEAR,
    MAX_TITLE_LEN,
    MIN_PUBLICATION_YEAR,
    SOURCE_EUROPEPMC,
    DiscoveryRecordError,
    MetadataRecord,
    SearchPage,
    SearchResult,
    sanitize_display_text,
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

#: The documented Europe PMC native-source alphabet: a short uppercase ASCII
#: alphanumeric token (``MED``/``PMC``/``PPR``/...). It deliberately excludes
#: ``:`` so the composed ``source_id`` is unambiguous.
_NATIVE_SOURCE_RE = re.compile(r"[A-Z][A-Z0-9]*")


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


def _author_display_name(entry: dict[str, Any]) -> str | None:
    """Best-effort display name from one Europe PMC author object.

    Prefers the provider's own ``fullName``; otherwise composes ``firstName
    lastName`` (or ``lastName initials``), then a collective/consortium name.
    Optional subfields with a non-string type are ignored (treated absent); the
    container types are validated by the caller.
    """
    full = entry.get("fullName")
    if isinstance(full, str) and full.strip():
        return full
    first = entry.get("firstName")
    last = entry.get("lastName")
    initials = entry.get("initials")
    if isinstance(first, str) and isinstance(last, str) and first.strip() and last.strip():
        return f"{first} {last}"
    if isinstance(last, str) and last.strip():
        if isinstance(initials, str) and initials.strip():
            return f"{last} {initials}"
        return last
    collective = entry.get("collectiveName")
    if isinstance(collective, str) and collective.strip():
        return collective
    return None


def _optional_authors(item: dict[str, Any]) -> tuple[str, ...]:
    """Read the ordered author display-name list; absent/empty stays ``()``.

    Prefers the structured ``authorList.author[].*`` names; falls back to the
    provider's ``authorString`` (split on commas) only when no structured names
    are present. Untrusted text is sanitised; an over-long list or name is
    malformed rather than silently truncated.
    """
    names: list[str] = []
    author_list = item.get("authorList")
    if author_list is not None:
        if not isinstance(author_list, dict):
            raise _MalformedResponse("authorList: not an object")
        authors = author_list.get("author")
        if authors is not None:
            if not isinstance(authors, list):
                raise _MalformedResponse("authorList.author: not a list")
            for entry in authors:
                if not isinstance(entry, dict):
                    raise _MalformedResponse("authorList.author: not an object")
                raw = _author_display_name(entry)
                if raw is None:
                    continue
                name = sanitize_display_text(raw)
                if name:
                    names.append(name)
    if not names:
        raw_string = item.get("authorString")
        if raw_string is not None:
            if not isinstance(raw_string, str):
                raise _MalformedResponse("authorString: not a string")
            for part in raw_string.split(","):
                name = sanitize_display_text(part)
                if name:
                    names.append(name)
    if len(names) > MAX_AUTHORS:
        raise _MalformedResponse("author list too long")
    for name in names:
        if len(name) > MAX_AUTHOR_LEN:
            raise _MalformedResponse("author name too long")
    return tuple(names)


def _optional_journal(item: dict[str, Any]) -> str | None:
    """Read a nullable journal title; absent/empty stays ``None``.

    Prefers ``journalTitle``; falls back to ``journalInfo.journal.title``.
    """
    raw = item.get("journalTitle")
    if raw is None:
        journal_info = item.get("journalInfo")
        if isinstance(journal_info, dict):
            journal = journal_info.get("journal")
            if isinstance(journal, dict):
                candidate = journal.get("title")
                if isinstance(candidate, str) and candidate:
                    raw = candidate
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise _MalformedResponse("journalTitle: not a string")
    journal = sanitize_display_text(raw)
    if not journal:
        return None
    if len(journal) > MAX_JOURNAL_LEN:
        raise _MalformedResponse("journalTitle: too long")
    return journal


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
    """Read the required native provider source token (``MED``/``PMC``/``PPR``/...).

    Europe PMC native source codes are short **uppercase ASCII alphanumeric**
    tokens. Requiring that alphabet (and rejecting ``:``) keeps the composed
    ``source_id`` unambiguous: because the native source never contains ``:``,
    the first ``:`` always delimits source from id, so ``source="MED:A", id="B"``
    (rejected) can never collide with ``source="MED", id="A:B"``.
    """
    value = item.get("source")
    if not isinstance(value, str):
        raise _MalformedResponse("source: missing")
    value = value.strip()
    if not value or len(value) > MAX_ID_LEN:
        raise _MalformedResponse("source: invalid")
    if _NATIVE_SOURCE_RE.fullmatch(value) is None:
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
    except RecursionError as exc:
        # Deeply nested JSON exhausts the parser's recursion budget; treat it as
        # an explicit malformed response rather than letting it escape.
        raise _MalformedResponse("body: too deeply nested") from exc
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
                    authors=_optional_authors(item),
                    journal=_optional_journal(item),
                )
            )
        except DiscoveryRecordError as exc:
            raise _MalformedResponse("record: invalid") from exc
    return hit_count, tuple(records), next_cursor


def _has_duplicate_identity(
    records: tuple[MetadataRecord, ...], seen: set[str]
) -> bool:
    """True when a provider-native identity repeats within the page or a prior page.

    Only ``source_id`` (the composed native source + id) is an identity. A record
    whose native id is missing has no identity: it stays explicit and is never
    merged or fabricated, and a title is never treated as an identity. A repeated
    identity is an explicit malformed response — the observations are preserved
    (no scientific deduplication) and the result is never complete.
    """
    local: set[str] = set()
    for record in records:
        source_id = record.source_id
        if source_id is None:
            continue
        if source_id in seen or source_id in local:
            return True
        local.add(source_id)
    return False


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
    :class:`SearchResult`; a page limit, a repeated provider identity, or any
    protocol anomaly is an explicit incomplete outcome, never a silent success.
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

        Completion requires the collected record count to equal the stable
        ``hitCount`` and the provider to have reached a natural end: no
        ``nextCursorMark``, an unchanged ``nextCursorMark`` (the Solr-derived
        terminal signal), or an advancing cursor after the declared total has
        been collected exactly once (no truncation). A changed ``hitCount``, a
        page larger than the requested ``pageSize``, a repeated provider identity,
        more collected records than ``hitCount``, an empty page that still offers
        an advancing cursor, a repeated/looping/over-long cursor, a malformed or
        missing field (including deeply nested JSON), a short terminal page, a
        transport failure, or reaching ``max_pages`` all yield an explicit
        incomplete result.
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
        seen_identities: set[str] = set()
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

            # A Solr-derived cursor protocol signals the end either by omitting
            # nextCursorMark or by returning the cursorMark it was sent
            # (unchanged, possibly with an empty page).
            is_terminal = next_cursor is None or next_cursor == cursor
            new_total = len(collected) + len(page_records)

            anomaly: str | None = None
            if len(page_records) > validated_page_size:
                anomaly = "page returned more records than requested"
            elif page_hit_count != hit_count:
                anomaly = "hitCount changed between pages"
            elif _has_duplicate_identity(page_records, seen_identities):
                anomaly = "duplicate provider identity"
            elif new_total > hit_count:
                anomaly = "more records collected than hitCount"
            elif not page_records and not is_terminal:
                anomaly = "empty page with an advancing cursor"
            elif (
                next_cursor is not None
                and next_cursor != cursor
                and next_cursor in seen_cursors
            ):
                anomaly = "cursor looped to a previous value"
            elif is_terminal and new_total != hit_count:
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
            seen_identities.update(
                record.source_id
                for record in page_records
                if record.source_id is not None
            )

            if new_total == hit_count:
                # Every declared hit has been collected exactly once, so the
                # bounded retrieval is complete with no truncation even when the
                # provider still offers an advancing cursor.
                stop_reason = "completed"
                break
            # Not terminal and still short: follow the advancing cursor.
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
