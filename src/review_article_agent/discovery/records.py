"""Immutable bibliographic retrieval value contracts (Step 05 slice 1).

Authority
---------

``docs/implementation/05-discovery.md`` deliverables 2/4 and acceptance criteria
3/4/7 (reconstruct exact queries, timestamps, contributing sources, failures,
counts, and pages from records; fixed fixtures; no hidden completeness claim),
``docs/specification.md`` workflow steps 2-5 and the safety invariants, and
``docs/contracts/03-policy-decisions.md`` ``POL-OPS-COVERAGE`` (the coverage
report records sources/queries/dates/yields and the human freezes the exact
revision; no universal numeric completeness rule).

What these values are
---------------------

* :class:`MetadataRecord` — a frozen, normalized bibliographic record. It carries
  the **provider provenance** (``source`` + ``source_id``) and the nullable
  identifiers ``pmid``/``pmcid``/``doi``, ``title``, and ``publication_year``.
  A missing identifier stays **explicit** (``has_identifier()`` is ``False``); it
  is never fabricated.
* :class:`SearchPage` — one immutable page of a provider response with the exact
  query, source, requested URL, UTC retrieval timestamp, page index/size, total
  count, records, an optional next cursor, an explicit ``complete`` flag, and a
  static ``failure``/``stop_reason``.
* :class:`SearchResult` — the immutable aggregate over pages: exact query/source,
  start/finish UTC timestamps, flattened records, counts, pages, and an explicit
  ``complete`` flag.

What these values are **not**
-----------------------------

* **Not scientific context and not an approval.** Untrusted provider metadata is
  never project context, eligibility, or an approval; a later task decides that.
* **Not study identity.** A publication identifier is not a study identifier.
  These values never equate the two and never invent an ID.
* **Not a completeness claim.** ``complete`` means *the bounded retrieval ran to
  a natural end*; a budget or page limit is **never** ``complete`` (enforced in
  :meth:`SearchResult.__post_init__`).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from ..domain.errors import ReviewAgentError
from .transport import AttemptRecord

__all__ = [
    "SOURCE_PUBMED",
    "SOURCE_EUROPEPMC",
    "STOP_REASONS",
    "FAILURE_REASONS",
    "MAX_TITLE_LEN",
    "MIN_PUBLICATION_YEAR",
    "MAX_PUBLICATION_YEAR",
    "DiscoveryRecordError",
    "MetadataRecord",
    "SearchPage",
    "SearchResult",
]

#: Canonical provider source ids for the two Step 05 providers.
SOURCE_PUBMED = "pubmed"
SOURCE_EUROPEPMC = "europepmc"

#: Why a search/page stopped. Only ``completed`` may be ``complete``.
STOP_REASONS = ("completed", "budget_exhausted", "page_limit", "failure")

#: Static failure reasons a page/result may carry (never peer text).
FAILURE_REASONS = (
    "http_status",
    "network_error",
    "retry_after_exceeds_bound",
    "response_too_large",
    "attempts_exhausted",
    "malformed_response",
)

#: Bounds on untrusted bibliographic text.
MAX_TITLE_LEN = 4096
MIN_PUBLICATION_YEAR = 1000
MAX_PUBLICATION_YEAR = 9999


class DiscoveryRecordError(ReviewAgentError, ValueError):
    """A discovery value contract failed validation."""


# --------------------------------------------------------------------------- #
# Validation helpers
# --------------------------------------------------------------------------- #
def _require_nonempty_str(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value:
        raise DiscoveryRecordError(f"{field_name}: must be a non-empty string")
    return value


def _require_optional_id(value: object, field_name: str) -> str | None:
    """Validate a nullable provider identifier (explicit when present)."""
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise DiscoveryRecordError(f"{field_name}: must be a non-empty string or None")
    if value != value.strip() or any(ch.isspace() for ch in value):
        raise DiscoveryRecordError(f"{field_name}: must not contain whitespace")
    for ch in value:
        if ord(ch) < 0x20 or ord(ch) == 0x7F:
            raise DiscoveryRecordError(
                f"{field_name}: must not contain control characters"
            )
    return value


def _require_optional_title(value: object, field_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise DiscoveryRecordError(f"{field_name}: must be a non-empty string or None")
    if len(value) > MAX_TITLE_LEN:
        raise DiscoveryRecordError(f"{field_name}: is too long")
    return value


def _require_optional_year(value: object, field_name: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise DiscoveryRecordError(f"{field_name}: must be an integer or None")
    if value < MIN_PUBLICATION_YEAR or value > MAX_PUBLICATION_YEAR:
        raise DiscoveryRecordError(f"{field_name}: is outside the permitted range")
    return value


def _require_optional_count(value: object, field_name: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise DiscoveryRecordError(f"{field_name}: must be an integer or None")
    if value < 0:
        raise DiscoveryRecordError(f"{field_name}: must not be negative")
    return value


def _require_optional_reason(
    value: object, field_name: str, allowed: tuple[str, ...]
) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or value not in allowed:
        raise DiscoveryRecordError(f"{field_name}: is not a known literal")
    return value


# --------------------------------------------------------------------------- #
# MetadataRecord
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class MetadataRecord:
    """One frozen, normalized bibliographic record from a provider.

    ``source`` is the provider id (e.g. :data:`SOURCE_PUBMED`); ``source_id`` is
    the provider's own record identifier. ``pmid``/``pmcid``/``doi``/``title``/
    ``publication_year`` are nullable. A record with no identifier is valid but
    explicit: :meth:`has_identifier` is ``False``. Publication identity is never
    treated as study identity and no identifier is ever fabricated.
    """

    source: str
    source_id: str | None = None
    pmid: str | None = None
    pmcid: str | None = None
    doi: str | None = None
    title: str | None = None
    publication_year: int | None = None

    def __post_init__(self) -> None:
        _require_nonempty_str(self.source, "source")
        if any(ch.isspace() for ch in self.source) or "/" in self.source or "\\" in self.source:
            raise DiscoveryRecordError("source: must be a safe provider identifier")
        _require_optional_id(self.source_id, "source_id")
        _require_optional_id(self.pmid, "pmid")
        _require_optional_id(self.pmcid, "pmcid")
        _require_optional_id(self.doi, "doi")
        _require_optional_title(self.title, "title")
        _require_optional_year(self.publication_year, "publication_year")

    def has_identifier(self) -> bool:
        """True when at least one provider identifier is present."""
        return any(
            value is not None
            for value in (self.source_id, self.pmid, self.pmcid, self.doi)
        )

    def is_missing_identifier(self) -> bool:
        """True when no provider identifier is present (explicit, not hidden)."""
        return not self.has_identifier()


# --------------------------------------------------------------------------- #
# SearchPage
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class SearchPage:
    """One immutable page of a provider response.

    ``complete`` means *this page was retrieved in full* (a successful, untruncated
    provider page). A page that failed or was capped is never ``complete``.
    """

    source: str
    query: str
    requested_url: str
    retrieved_at: str
    page_index: int
    records: tuple[MetadataRecord, ...] = ()
    page_size: int | None = None
    total_count: int | None = None
    next_cursor: str | None = None
    complete: bool = False
    stop_reason: str | None = None
    failure: str | None = None
    attempts: tuple[AttemptRecord, ...] = ()

    def __post_init__(self) -> None:
        _require_nonempty_str(self.source, "source")
        _require_nonempty_str(self.query, "query")
        _require_nonempty_str(self.requested_url, "requested_url")
        _require_nonempty_str(self.retrieved_at, "retrieved_at")
        if isinstance(self.page_index, bool) or not isinstance(self.page_index, int):
            raise DiscoveryRecordError("page_index: must be an integer")
        if self.page_index < 0:
            raise DiscoveryRecordError("page_index: must not be negative")
        if not isinstance(self.records, tuple):
            raise DiscoveryRecordError("records: must be a tuple")
        for record in self.records:
            if not isinstance(record, MetadataRecord):
                raise DiscoveryRecordError("records: must contain MetadataRecord values")
            if record.source != self.source:
                raise DiscoveryRecordError(
                    "records: every record source must match the page source"
                )
        if not isinstance(self.attempts, tuple):
            raise DiscoveryRecordError("attempts: must be a tuple")
        for attempt in self.attempts:
            if not isinstance(attempt, AttemptRecord):
                raise DiscoveryRecordError(
                    "attempts: must contain AttemptRecord values"
                )
        if self.page_size is not None:
            if isinstance(self.page_size, bool) or not isinstance(self.page_size, int):
                raise DiscoveryRecordError("page_size: must be an integer or None")
            if self.page_size < 1:
                raise DiscoveryRecordError("page_size: must be positive")
        _require_optional_count(self.total_count, "total_count")
        if self.next_cursor is not None:
            _require_nonempty_str(self.next_cursor, "next_cursor")
        if not isinstance(self.complete, bool):
            raise DiscoveryRecordError("complete: must be a bool")
        _require_optional_reason(self.stop_reason, "stop_reason", STOP_REASONS)
        _require_optional_reason(self.failure, "failure", FAILURE_REASONS)
        if self.failure is not None and self.complete:
            raise DiscoveryRecordError("a failed page must not be complete")
        if self.failure is not None and self.stop_reason != "failure":
            raise DiscoveryRecordError(
                "a failed page must carry stop_reason 'failure'"
            )
        if self.stop_reason == "failure" and self.failure is None:
            raise DiscoveryRecordError(
                "stop_reason 'failure' requires a failure reason"
            )

    def has_identifier_coverage(self) -> bool:
        """True when every record on the page carries at least one identifier."""
        return all(record.has_identifier() for record in self.records)


# --------------------------------------------------------------------------- #
# SearchResult
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class SearchResult:
    """The immutable aggregate of a bounded search over one or more pages.

    ``complete`` is ``True`` **iff** ``stop_reason == "completed"``, there is no
    ``failure``, and **every** page is itself complete (a fully retrieved,
    untruncated provider page). A budget or page limit therefore can never be
    ``complete``, and a failed/capped page can never be counted complete.
    ``records`` is exactly the concatenation of the pages' records and
    ``attempts`` exactly the concatenation of the pages' attempts (both enforced).
    Every page must share this result's ``source`` and ``query``.
    """

    source: str
    query: str
    pages: tuple[SearchPage, ...]
    records: tuple[MetadataRecord, ...]
    started_at: str
    completed_at: str
    stop_reason: str
    total_count: int | None = None
    complete: bool = False
    failure: str | None = None
    attempts: tuple[AttemptRecord, ...] = ()

    def __post_init__(self) -> None:
        _require_nonempty_str(self.source, "source")
        _require_nonempty_str(self.query, "query")
        _require_nonempty_str(self.started_at, "started_at")
        _require_nonempty_str(self.completed_at, "completed_at")
        if not isinstance(self.pages, tuple):
            raise DiscoveryRecordError("pages: must be a tuple")
        for page in self.pages:
            if not isinstance(page, SearchPage):
                raise DiscoveryRecordError("pages: must contain SearchPage values")
            if page.source != self.source:
                raise DiscoveryRecordError(
                    "pages: every page source must match the result source"
                )
            if page.query != self.query:
                raise DiscoveryRecordError(
                    "pages: every page query must match the result query"
                )
        if not isinstance(self.records, tuple):
            raise DiscoveryRecordError("records: must be a tuple")
        for record in self.records:
            if not isinstance(record, MetadataRecord):
                raise DiscoveryRecordError("records: must contain MetadataRecord values")
        flattened = tuple(record for page in self.pages for record in page.records)
        if flattened != self.records:
            raise DiscoveryRecordError(
                "records: must equal the concatenation of the page records"
            )
        if not isinstance(self.attempts, tuple):
            raise DiscoveryRecordError("attempts: must be a tuple")
        for attempt in self.attempts:
            if not isinstance(attempt, AttemptRecord):
                raise DiscoveryRecordError(
                    "attempts: must contain AttemptRecord values"
                )
        flattened_attempts = tuple(
            attempt for page in self.pages for attempt in page.attempts
        )
        if flattened_attempts != self.attempts:
            raise DiscoveryRecordError(
                "attempts: must equal the concatenation of the page attempts"
            )
        _require_optional_count(self.total_count, "total_count")
        if not isinstance(self.complete, bool):
            raise DiscoveryRecordError("complete: must be a bool")
        if self.stop_reason not in STOP_REASONS:
            raise DiscoveryRecordError("stop_reason: is not a known literal")
        _require_optional_reason(self.failure, "failure", FAILURE_REASONS)
        if self.complete != (self.stop_reason == "completed" and self.failure is None):
            raise DiscoveryRecordError(
                "complete must be true only for a completed search without failure"
            )
        if self.failure is not None and self.stop_reason != "failure":
            raise DiscoveryRecordError(
                "a failed search must carry stop_reason 'failure'"
            )
        if self.stop_reason == "failure" and self.failure is None:
            raise DiscoveryRecordError(
                "stop_reason 'failure' requires a failure reason"
            )
        if self.complete:
            for page in self.pages:
                if not page.complete or page.failure is not None:
                    raise DiscoveryRecordError(
                        "a complete search requires every page to be complete"
                    )

    @classmethod
    def from_pages(
        cls,
        *,
        source: str,
        query: str,
        pages: Sequence[SearchPage],
        started_at: str,
        completed_at: str,
        stop_reason: str,
        total_count: int | None = None,
        failure: str | None = None,
    ) -> "SearchResult":
        """Assemble a result from pages, deriving records/attempts/``complete``.

        This is the preferred constructor: it flattens the page records and
        attempts and derives ``complete`` from ``stop_reason``/``failure`` so a
        budget or page limit can never be reported complete by accident. A page
        whose source/query differs from the result, or a failed page under a
        ``completed`` stop reason, is refused by ``__post_init__``.
        """
        page_tuple = tuple(pages)
        records = tuple(record for page in page_tuple for record in page.records)
        attempts = tuple(
            attempt for page in page_tuple for attempt in page.attempts
        )
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
        )
