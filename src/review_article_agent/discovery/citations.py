"""stdlib-only Europe PMC bounded citation/reference expansion adapter (Step 05).

Authority
---------

``docs/implementation/05-discovery.md`` D3 AC5 (a known-relevant fixture set and
forward/backward citation expansion expose **explicit coverage gaps**; reporting
never claims absolute completeness) and AC7 (no arbitrary API result cap
truncation is hidden as complete retrieval), ``docs/specification.md`` workflow
step 5 (citation expansion feeds an explicit search-again/freeze decision and
"never promise all papers") and the citation-origin policy / safety invariants
(treat provider data as untrusted; bounded network handling), contract C01 (a
publication identifier is **not** a study identifier), and contract C03
``POL-OPS-COVERAGE`` (record sources/queries/dates/yields; never a universal
completeness rule).

What this is
------------

* :class:`EuropePMCCitationClient` — a small, deterministic **library** adapter
  over the shared :class:`~review_article_agent.discovery.transport.DiscoveryTransport`.
  :meth:`~EuropePMCCitationClient.references` retrieves the **backward** reference
  list (what a paper cites) and :meth:`~EuropePMCCitationClient.citations` the
  **forward** citation list (who cites a paper), each via one explicit Europe PMC
  REST GET per page to the narrowly allowlisted citation path family
  (``.../rest/{source}/{id}/references`` or ``/citations``; see
  :func:`~review_article_agent.discovery.transport.build_epmc_citation_url`).
* It returns the shared immutable
  :class:`~review_article_agent.discovery.records.SearchResult` (with its
  :class:`~review_article_agent.discovery.records.SearchPage` /
  :class:`~review_article_agent.discovery.records.MetadataRecord` values), so the
  deterministic relation descriptor, the exact requested URLs, the UTC times, the
  page indices, the declared ``hitCount``, and **every** transport attempt are
  retained as provenance. No new result subclass, codec, or storage change is
  introduced.
* ``page`` is 1-based and ``pageSize`` is bounded to ``1..1000`` exactly as the
  Europe PMC RESTful Web Service documents; the envelope is ``hitCount`` plus
  ``referenceList.reference`` / ``citationList.citation``.
* Provider identity is captured unambiguously: the shared ``source`` stays
  :data:`~review_article_agent.discovery.records.SOURCE_EUROPEPMC` while
  ``source_id`` encodes the native source and id (for example ``MED:31452104``,
  ``PMC:PMC123``), so a native ``MED`` record can never alias a bare numeric id.

What this is **not**
--------------------

* **Not verified scientific support and not an approval.** A reference/citation
  link is **bibliographic metadata**, not evidence that a claim is supported,
  contradicted, or new. This adapter performs **no** support/originality/coverage
  adjudication; a later task (and a human) decides that.
* **Not identity reconciliation.** No deduplication, merging, or cross-provider
  reconciliation is performed here; records are returned exactly as the pages
  presented them, in provider order.
* **Not a completeness or coverage claim.** ``complete`` means *the bounded
  retrieval collected exactly the provider's declared ``hitCount`` entries*. It
  never means "all papers on the topic", "all citations that exist", or that a
  reference list is scientifically exhaustive. A ``page_limit``, an anomaly, or a
  transport failure is always an explicit incomplete outcome.
* **Not a runner or a CLI.** It is a library only; workflow integration, scope
  approval, and edge construction (parent/source/occurrence) belong to later tasks.

Identity and unmatched references
---------------------------------

Europe PMC annotates every entry with a native ``source`` and ``id`` **when it was
matched**; an unmatched entry instead carries matching metadata such as ``match``,
``citedOrder``, ``title`` and ``pubYear`` but no resolvable identity. This adapter
therefore treats both ``source`` and ``id`` as nullable: when both are present the
unambiguous ``source_id`` is ``"{source}:{id}"``; when either is absent/empty the
record keeps its metadata (``title``/``publication_year``) with ``source_id``,
``pmid``, ``pmcid`` and ``doi`` all ``None`` — explicit, never fabricated. No
``pmid``/``pmcid``/``doi`` is ever invented, and a partial identity (only ``source``
or only ``id``) yields no composed id rather than a guessed one. The raw response
body participates in the transport's per-attempt ``body_sha256``/``body_bytes``
provenance (every extra provider field is covered by that digest), so a later edge
builder can pin the relation parent/source/occurrence from a re-fetched, digests-
verified response without this adapter inventing fields it does not model.

Duplicate-occurrence semantics
------------------------------

A reference/citation list may legitimately list the **same** work more than once
(a paper can cite a work twice; a provider can return both a matched and an
unmatched form). This adapter therefore **never deduplicates and never drops an
occurrence**: duplicates are retained in provider order, exactly as returned. A
repeated identity is **not by itself** an anomaly. It becomes an explicit
incomplete outcome only when it is *inconsistent* with the provider's declared
totals — specifically when the collected occurrence count exceeds ``hitCount``
(overcollection). :func:`repeated_identities` exposes the repeated native
identities so a caller can see the repetition without any loss.

Explicit incomplete outcomes
----------------------------

A malformed or untrustworthy response never becomes a silent success. A missing
or wrong-typed field (including a non-list ``referenceList``/``citationList``,
deeply nested JSON, or an invalid year), a changed ``hitCount``, a page larger
than the requested ``pageSize``, more collected entries than ``hitCount``, and an
empty page that arrives before the declared total is reached all yield an explicit
``malformed_response`` failure page and an incomplete result. A transport failure
retains the earlier successful pages plus the failed page's full attempt
provenance. Reaching ``max_pages`` while entries remain yields ``page_limit`` and
is never ``complete``. A successful retrieval is ``complete`` **only** when the
collected entry count equals the stable ``hitCount``.

Request boundary
----------------

This module opens no socket itself and makes no implicit or automatic request:
construction performs no network work, and every request is one **explicit**
library call routed through the injected transport, which owns the fixed
allowlist, bounded retries, rate handling, the response-size cap, and the
no-credential policy. Given a real
:class:`~review_article_agent.discovery.transport.DiscoveryTransport` the adapter
therefore *does* issue public, unauthenticated GETs to Europe PMC; the committed
tests inject a scripted transport, so the test suite makes no network call. No
credential and no paid call are ever used.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Callable

# The Europe PMC search adapter already owns the shared JSON/field validators for
# this provider's untrusted payloads; reuse them here (rather than duplicating or
# editing ``europepmc.py``) so both adapters reject identical malformed shaping.
from .europepmc import (
    EPMC_FORMAT,
    MAX_ID_LEN,
    _MalformedResponse,
    _NATIVE_SOURCE_RE,
    _optional_identifier,
    _optional_title,
    _optional_year,
    _require_bounded_int,
)
from .records import (
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
    build_epmc_citation_url,
    validate_epmc_citation_identity,
)

__all__ = [
    "MIN_PAGE_SIZE",
    "MAX_PAGE_SIZE",
    "DEFAULT_PAGE_SIZE",
    "MIN_MAX_PAGES",
    "MAX_MAX_PAGES",
    "DEFAULT_MAX_PAGES",
    "EuropePMCCitationClient",
    "repeated_identities",
]

#: Bounded per-page size (Europe PMC caps ``pageSize`` at 1000).
MIN_PAGE_SIZE = 1
MAX_PAGE_SIZE = 1000
DEFAULT_PAGE_SIZE = 25

#: Bounded page budget. Reaching it is an explicit ``page_limit``, never complete.
MIN_MAX_PAGES = 1
MAX_MAX_PAGES = 1000
DEFAULT_MAX_PAGES = 10

#: The Europe PMC citation envelopes: relation -> (list container, entry key).
_RELATION_LISTS = {
    "references": ("referenceList", "reference"),
    "citations": ("citationList", "citation"),
}


def _default_utcnow() -> str:
    """The default UTC clock: an ISO-8601 timestamp (injectable for fixtures)."""
    return datetime.now(timezone.utc).isoformat()


def _optional_native_source(item: dict[str, Any]) -> str | None:
    """Read a nullable native provider source token (``MED``/``PMC``/...).

    Absent or empty stays ``None`` (an unmatched entry). A present value that is
    not a safe native-source token (wrong type, whitespace, too long, or outside
    the uppercase ASCII alphanumeric alphabet) is malformed, not silently dropped.
    """
    value = item.get("source")
    if value is None:
        return None
    if not isinstance(value, str):
        raise _MalformedResponse("source: not a string")
    if value == "":
        return None
    if len(value) > MAX_ID_LEN:
        raise _MalformedResponse("source: too long")
    if _NATIVE_SOURCE_RE.fullmatch(value) is None:
        raise _MalformedResponse("source: invalid")
    return value


def _parse_entry(entry: dict[str, Any]) -> MetadataRecord:
    """Parse one reference/citation entry into an explicit ``MetadataRecord``.

    The native identity is composed only when **both** ``source`` and ``id`` are
    present; otherwise the entry keeps its metadata (title/year) with no identity
    and no fabricated identifier. ``pmid``/``pmcid``/``doi`` stay nullable and are
    never invented.
    """
    source = _optional_native_source(entry)
    native_id = _optional_identifier(entry, "id")
    source_id = (
        f"{source}:{native_id}"
        if source is not None and native_id is not None
        else None
    )
    if source_id is not None and len(source_id) > MAX_ID_LEN:
        raise _MalformedResponse("id: too long")
    try:
        return MetadataRecord(
            source=SOURCE_EUROPEPMC,
            source_id=source_id,
            pmid=_optional_identifier(entry, "pmid"),
            pmcid=_optional_identifier(entry, "pmcid"),
            doi=_optional_identifier(entry, "doi"),
            title=_optional_title(entry, "title"),
            publication_year=_optional_year(entry, "pubYear"),
        )
    except DiscoveryRecordError as exc:
        raise _MalformedResponse("record: invalid") from exc


def _parse_relation_page(
    body: bytes, relation: str
) -> tuple[int, tuple[MetadataRecord, ...]]:
    """Parse one Europe PMC citation/reference page or raise :class:`_MalformedResponse`.

    Returns ``(hit_count, entries)``. ``hit_count`` is the provider's declared
    total; the entry list is taken from ``referenceList.reference`` (references)
    or ``citationList.citation`` (citations). An absent list container or key is
    treated as an empty list (a zero-hit provider response may omit it); a present
    container/key of the wrong type is malformed.
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

    list_key, item_key = _RELATION_LISTS[relation]
    container = payload.get(list_key)
    if container is None:
        items: object = []
    elif not isinstance(container, dict):
        raise _MalformedResponse(f"{list_key}: invalid")
    else:
        items = container.get(item_key)
        if items is None:
            items = []
    if not isinstance(items, list):
        raise _MalformedResponse(f"{list_key}.{item_key}: invalid")

    records: list[MetadataRecord] = []
    for entry in items:
        if not isinstance(entry, dict):
            raise _MalformedResponse("entry: not an object")
        records.append(_parse_entry(entry))
    return hit_count, tuple(records)


def repeated_identities(
    records: tuple[MetadataRecord, ...],
) -> tuple[str, ...]:
    """Return the native identities that occur more than once, in first-seen order.

    A repeated reference/citation occurrence is legitimate and is always retained
    (never deduplicated); this helper only **exposes** which identities repeat so a
    caller can reason about them without any occurrence being lost. Entries with no
    identity (``source_id is None``) are never treated as repeats of one another.
    """
    counts: dict[str, int] = {}
    order: list[str] = []
    for record in records:
        source_id = record.source_id
        if source_id is None:
            continue
        if source_id not in counts:
            counts[source_id] = 0
            order.append(source_id)
        counts[source_id] += 1
    return tuple(source_id for source_id in order if counts[source_id] > 1)


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
    pagination was untrustworthy (a changed ``hitCount`` or an overcollection),
    its already-observed records are retained so the observations stay visible;
    the page is still explicitly incomplete.
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


class EuropePMCCitationClient:
    """A bounded Europe PMC ``references``/``citations`` adapter over the transport.

    ``transport`` is an injected
    :class:`~review_article_agent.discovery.transport.DiscoveryTransport` (it owns
    the fixed allowlist, retries, rate handling, size cap, and no-credential
    policy). ``utcnow`` is an optional injectable UTC clock returning an ISO-8601
    string (default: the real clock), so page and result timestamps are
    deterministic in fixtures. Construction performs **no network work**.

    :meth:`references`/:meth:`citations` validate the ``source``/``id`` identity
    and the ``page_size`` (``1..1000``) / ``max_pages`` (``1..1000``) bounds
    **before** any request, so an invalid argument causes zero network activity.
    They return a shared immutable
    :class:`~review_article_agent.discovery.records.SearchResult`; a page limit or
    any protocol anomaly is an explicit incomplete outcome, never a silent success.
    """

    def __init__(
        self,
        transport: DiscoveryTransport,
        *,
        utcnow: Callable[[], str] | None = None,
    ) -> None:
        if not callable(getattr(transport, "get", None)):
            raise DiscoveryValidationError(
                "transport: must be a DiscoveryTransport with a get method"
            )
        if utcnow is not None and not callable(utcnow):
            raise DiscoveryValidationError("utcnow: must be callable or None")
        self._transport = transport
        self._utcnow = utcnow if utcnow is not None else _default_utcnow

    @property
    def transport(self) -> DiscoveryTransport:
        """The injected transport (read-only)."""
        return self._transport

    def references(
        self,
        source: str,
        id: str,
        *,
        page_size: int = DEFAULT_PAGE_SIZE,
        max_pages: int = DEFAULT_MAX_PAGES,
    ) -> SearchResult:
        """Retrieve a paper's **backward** reference list (bounded, paginated)."""
        return self._expand("references", source, id, page_size, max_pages)

    def citations(
        self,
        source: str,
        id: str,
        *,
        page_size: int = DEFAULT_PAGE_SIZE,
        max_pages: int = DEFAULT_MAX_PAGES,
    ) -> SearchResult:
        """Retrieve a paper's **forward** citation list (bounded, paginated)."""
        return self._expand("citations", source, id, page_size, max_pages)

    def _expand(
        self,
        relation: str,
        source: str,
        native_id: str,
        page_size: int,
        max_pages: int,
    ) -> SearchResult:
        # Validate the identity and the bounds BEFORE any request is made, so an
        # invalid argument causes zero network activity.
        validated_source, validated_id, validated_relation = (
            validate_epmc_citation_identity(source, native_id, relation)
        )
        validated_page_size = _require_bounded_int(
            page_size, "page_size", minimum=MIN_PAGE_SIZE, maximum=MAX_PAGE_SIZE
        )
        validated_max_pages = _require_bounded_int(
            max_pages, "max_pages", minimum=MIN_MAX_PAGES, maximum=MAX_MAX_PAGES
        )
        request_url = build_epmc_citation_url(
            validated_source, validated_id, validated_relation
        )
        query = f"{validated_source}:{validated_id} {validated_relation}"

        started_at = self._utcnow()
        pages: list[SearchPage] = []
        collected: list[MetadataRecord] = []
        hit_count: int | None = None
        stop_reason: str | None = None
        failure: str | None = None

        for page_index in range(validated_max_pages):
            # ``page`` is 1-based; the exact requested URL is recorded on the page.
            retrieval = self._transport.get(
                request_url,
                {
                    "format": EPMC_FORMAT,
                    "page": page_index + 1,
                    "pageSize": validated_page_size,
                },
            )
            retrieved_at = self._utcnow()

            if not retrieval.ok:
                pages.append(
                    _failed_page(
                        query=query,
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
                page_hit_count, page_records = _parse_relation_page(
                    retrieval.body, validated_relation
                )
            except _MalformedResponse:
                pages.append(
                    _failed_page(
                        query=query,
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

            new_total = len(collected) + len(page_records)

            anomaly: str | None = None
            if len(page_records) > validated_page_size:
                anomaly = "page returned more entries than requested"
            elif page_hit_count != hit_count:
                anomaly = "hitCount changed between pages"
            elif new_total > hit_count:
                anomaly = "more entries collected than hitCount"
            elif not page_records and new_total < hit_count:
                anomaly = "empty page before the declared total"

            if anomaly is not None:
                pages.append(
                    _failed_page(
                        query=query,
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
                    query=query,
                    requested_url=retrieval.requested_url,
                    retrieved_at=retrieved_at,
                    page_index=page_index,
                    records=page_records,
                    page_size=validated_page_size,
                    total_count=hit_count,
                    next_cursor=None,
                    complete=True,
                    stop_reason=None,
                    failure=None,
                    attempts=retrieval.attempts,
                )
            )
            collected.extend(page_records)

            if new_total == hit_count:
                # Every declared entry has been collected exactly once, so the
                # bounded retrieval is complete with no truncation.
                stop_reason = "completed"
                break
        else:
            # The provider still had entries left when the page budget ran out.
            stop_reason = "page_limit"

        completed_at = self._utcnow()
        return SearchResult.from_pages(
            source=SOURCE_EUROPEPMC,
            query=query,
            pages=pages,
            started_at=started_at,
            completed_at=completed_at,
            stop_reason=stop_reason,
            total_count=hit_count,
            failure=failure,
        )
