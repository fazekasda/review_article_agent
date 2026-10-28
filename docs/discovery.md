# Discovery metadata adapters (Step 05, partial)

**Status: partial — metadata adapter foundation.** This document describes the
**first partial slice** of [Step 05](implementation/05-discovery.md): a bounded,
stdlib-only public-metadata transport and two provider adapters. It is a
**library foundation**, not a workflow. The whole of Step 05 remains
**unchecked/partial**; see the
[Step 05 handoff](implementation/handoffs/05-discovery.md) for the
criterion-by-criterion status.

Nothing here is scientific context, eligibility, an approval, or a corpus freeze.
Every value is an **untrusted provider observation**. No accepted context, no
scientific identity, and no candidate repository exist yet.

## What exists

| Module | Role |
| --- | --- |
| `review_article_agent.discovery.transport` | Bounded, deterministic HTTP GET client with a fixed HTTPS allowlist, no credentials, redirects/proxies disabled, retries, size cap, per-instance rate spacing, and immutable attempt provenance. |
| `review_article_agent.discovery.records` | Frozen value contracts: `MetadataRecord`, `SearchPage`, `SearchResult`. |
| `review_article_agent.discovery.pubmed` | PubMed ESearch + EFetch metadata adapter (`PubMedClient`). |
| `review_article_agent.discovery.europepmc` | Europe PMC cursor-paginated metadata adapter (`EuropePMCClient`). |

## Example (placeholder — not executed by docs or tests)

Constructing a transport and the two adapters performs **no** network I/O; a
request happens only when `search()` is called. The `search()` calls below are
**placeholders** and are **not** run by this documentation or by the test suite.

```python
from review_article_agent.discovery.transport import DiscoveryTransport
from review_article_agent.discovery.pubmed import PubMedClient
from review_article_agent.discovery.europepmc import EuropePMCClient

transport = DiscoveryTransport()          # constructed; no I/O until search()
pubmed = PubMedClient(transport=transport)  # shares the one transport instance
europepmc = EuropePMCClient(transport)      # positional transport argument

# Placeholders only — never executed by docs or tests:
# pubmed_result = pubmed.search("asthma AND review[pt]")
# epmc_result = europepmc.search("EXT_ID:31452104 AND SRC:MED")
```

A caller that issues real requests must **serialize** them (or share one
transport) because rate pacing is **per-instance**, not process-global — see
*Behaviour notes*.

## API reference

### `discovery.transport`

```python
build_request_url(url: object, params: object = None) -> str
http_get(url: str, *, timeout: float, max_response_bytes: int) -> TransportResponse

DiscoveryTransport(
    *,
    timeout: float = 30.0,
    max_response_bytes: int = 8_388_608,   # 8 MiB
    max_attempts: int = 3,
    retry_delay: float = 1.0,
    max_retry_delay: float = 60.0,
    rate_spacing: float = 0.34,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    utcnow: Callable[[], str] = <UTC ISO-8601>,
    transport: Callable[..., TransportResponse] = http_get,
) -> None

DiscoveryTransport.get(url: str, params: object = None) -> RetrievalResult
```

| Bound | Default | Min | Max |
| --- | --- | --- | --- |
| `timeout` (s) | 30.0 | 0.1 | 300.0 |
| `max_response_bytes` | 8 MiB | 1 KiB | 32 MiB |
| `max_attempts` | 3 | 1 | 5 |
| `retry_delay` (s) | 1.0 | 0.0 | 60.0 |
| `max_retry_delay` (s) | 60.0 | 0.0 | 60.0 |
| `rate_spacing` (s) | 0.34 | 0.0 | 60.0 |

- **Fixed allowlist** (`ALLOWED_ENDPOINTS`): exactly
  `https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi`,
  `.../efetch.fcgi`, and
  `https://www.ebi.ac.uk/europepmc/webservices/rest/search`. Any other scheme,
  host, port, path, URL userinfo, or fragment is refused **before a socket is
  opened**.
- **No credentials.** No `Authorization` header is ever sent; a credential-looking
  query parameter (`api_key`, `token`, `secret`, `password`, `cookie`, `auth`, …)
  is refused.
- **No redirects, no proxies.** Every 3xx is refused; the opener registers an
  empty `ProxyHandler`, so `HTTP_PROXY`/`HTTPS_PROXY`/`no_proxy` are never used.
- **Retries** apply only to `RETRYABLE_STATUSES = {408, 429, 500, 502, 503, 504}`.
  Any other non-200 is a **permanent** failure (never retried).
- **Body bound.** At most `max_response_bytes + 1` bytes are read; a larger body
  is an explicit `response_too_large` failure, never a silent truncation.
- **`Retry-After`** is parsed from integer seconds or an HTTP-date. A value too
  large to represent (more than 18 significant digits, or beyond the permitted
  delay) yields the conservative sentinel `MAX_RETRY_DELAY_SECONDS + 1.0` and an
  explicit `retry_after_exceeds_bound` failure — the **sentinel is not the exact
  magnitude**; the raw header is never retained.
- **Provenance.** Every attempt (success or failure) is an immutable
  `AttemptRecord(attempt_no, requested_url, requested_at, completed_at, status,
  body_bytes, body_sha256, outcome, retry_after_seconds=None)` with
  `outcome ∈ ("ok", "retryable", "permanent", "network_error", "too_large")`.
  `RetrievalResult` carries `ok`, `requested_url`, `status`, `body`, `body_bytes`,
  `body_sha256`, `attempts`, and on failure a static `failure ∈
  ("http_status", "network_error", "retry_after_exceeds_bound",
  "response_too_large", "attempts_exhausted")` plus a peer-text-free `diagnostic`.
- **Validation vs. runtime.** An invalid URL/option raises
  `DiscoveryValidationError`/`DiscoveryEndpointError` before any request; once a
  request is attempted, no network/HTTP outcome raises — it is returned as a
  failed `RetrievalResult`.

### `discovery.records`

```python
MetadataRecord(
    source: str,
    source_id: str | None = None,
    pmid: str | None = None,
    pmcid: str | None = None,
    doi: str | None = None,
    title: str | None = None,
    publication_year: int | None = None,
)   # .has_identifier() / .is_missing_identifier()

SearchPage(
    source, query, requested_url, retrieved_at, page_index,
    records=(), page_size=None, total_count=None, next_cursor=None,
    complete=False, stop_reason=None, failure=None, attempts=(),
)

SearchResult(
    source, query, pages, records, started_at, completed_at, stop_reason,
    total_count=None, complete=False, failure=None, attempts=(),
)
SearchResult.from_pages(*, source, query, pages, started_at, completed_at,
                        stop_reason, total_count=None, failure=None)
```

- `MAX_TITLE_LEN = 4096`; `MIN_PUBLICATION_YEAR = 1000`;
  `MAX_PUBLICATION_YEAR = 9999`.
- `STOP_REASONS = ("completed", "budget_exhausted", "page_limit", "failure")`.
- `FAILURE_REASONS = ("http_status", "network_error",
  "retry_after_exceeds_bound", "response_too_large", "attempts_exhausted",
  "malformed_response")`.
- A missing identifier stays **explicit** (`has_identifier()` is `False`); no
  identifier is ever fabricated. A publication identifier is **not** a study
  identifier.
- `complete` is `True` **iff** `stop_reason == "completed"`, there is no
  `failure`, and every page is itself complete. A budget/page limit is **never**
  `complete` (enforced in `__post_init__`).
- `records` and `attempts` must equal the exact concatenation of the pages'
  records/attempts (enforced).

### `discovery.pubmed` — `PubMedClient`

```python
PubMedClient(
    *,
    transport: object = None,          # None -> constructs a real DiscoveryTransport
    utcnow: Callable[[], str] | None = None,
    page_size: int = 20,
    max_pages: int = 5,
    window_limit: int = 9999,
) -> None

PubMedClient.search(query: object, page_size: object = None,
                    max_pages: object = None) -> PubMedSearchResult
```

| Bound | Default | Min | Max |
| --- | --- | --- | --- |
| `page_size` | 20 | 1 | 200 |
| `max_pages` | 5 | 1 | 100 |
| `window_limit` | 9999 | 1 | 9999 |
| `page_size * max_pages` | — | — | 20000 (`MAX_TOTAL_RECORDS`) |
| `query` length | — | 1 | 4096 |

- One ESearch (`retmode=json`) per page, then one EFetch (`retmode=xml`) for
  exactly those PMIDs. **Bibliographic metadata only** (PMID, title, year, DOI,
  PMCID); abstracts are never parsed.
- `PUBMED_WINDOW_LIMIT = 9999` is a **conservative** stand-in for NCBI's own
  ~9,999/10,000 ESearch window. `retmax` is clamped to the remaining window, so a
  request can never reach past it. A retrieval that would need to page past the
  window is an explicit `stop_reason == "page_limit"` — **never** silently
  complete. (True date-partitioned paging past the window is **deferred**.)
- Only `PubmedArticle` entries are supported. A `PubmedBookArticle` (or any other
  unexpected child) is an explicit `malformed_response`, **not** a silently
  dropped entry.
- A normal NCBI EFetch reply's plain **external** `<!DOCTYPE ... PUBLIC ...>` is
  **accepted without fetching** it (stdlib `ElementTree` never resolves the
  external DTD). An **internal** DTD subset, any `<!ENTITY>` declaration, or a
  DOCTYPE naming a non-http(s) scheme is refused.
- Every page keeps the exact ESearch **and** EFetch `AttemptRecord`s, the exact
  transmitted query, the ESearch requested URL, and UTC timestamps.
  `PubMedSearchResult.query_translation` retains the provider's first-page
  `querytranslation` (informational; the exact transmitted query stays `query`).
- Overlap, a missing/unrequested EFetch id, a duplicate id within one page, a
  changed `count`, a changed translation, or an empty page before the claimed
  total is an explicit `malformed_response` page — records stay visible and the
  result is never complete.

### `discovery.europepmc` — `EuropePMCClient`

```python
EuropePMCClient(transport: DiscoveryTransport, *,
                utcnow: Callable[[], str] = <UTC ISO-8601>) -> None

EuropePMCClient.search(query: str, page_size: int = 100,
                       max_pages: int = 10) -> SearchResult
```

| Bound | Default | Min | Max |
| --- | --- | --- | --- |
| `page_size` | 100 | 1 | 1000 |
| `max_pages` | 10 | 1 | 1000 |
| `query` length | — | 1 | 4096 |
| cursor length | — | 1 | 4096 |
| id length | — | 1 | 256 |

- One REST `search` call per page with `format=json`, `resultType=lite`
  (**metadata only — no abstracts**), `pageSize` bounded to `<= 1000`, and
  `cursorMark` starting at `*` then following `nextCursorMark`.
- **Native identity** is composed as `source_id = "<native_source>:<native_id>"`
  (e.g. `MED:31452104`, `PMC:PMC123`, `PPR:PPR123`), so a `MED` record and a
  `PMC`/preprint record can never alias a bare numeric id. The native source must
  be a short uppercase ASCII token and never contains `:`.
- **Nullable IDs.** `pmid`/`pmcid`/`doi`/`title`/`publication_year` stay
  nullable; a record with no identifier is explicit (`source_id` etc. all `None`)
  and is never merged or fabricated.
- **Cursor + stable `hitCount`.** Completion requires the collected count to
  equal the stable `hitCount` and a natural end: no `nextCursorMark`, an unchanged
  `nextCursorMark` (the Solr-derived terminal signal), or an advancing cursor
  after the declared total was collected exactly once. A changed `hitCount`, a
  page larger than `pageSize`, a repeated **known identity** (within or across
  pages), over-collection, an empty page with an advancing cursor, a
  repeated/looping/over-long cursor, or a final count that disagrees with
  `hitCount` is an explicit `malformed_response` — never a silent success.
- A transport failure retains the earlier successful pages plus the failed page's
  full attempt provenance; reaching `max_pages` while a cursor is still offered
  yields `page_limit` and is never complete.

## Behaviour notes (limits to keep in mind)

- **Socket timeout is inactivity, not a deadline.** `timeout` bounds connection
  and read *inactivity*; a peer that dribbles bytes just under it can keep one
  attempt alive far longer than `timeout` seconds. The byte cap bounds memory but
  is not a total-deadline guarantee.
- **Rate pacing is per-instance, not process-global.** `rate_spacing` is tracked
  on each `DiscoveryTransport` instance. Two instances (or two processes) do not
  coordinate. A future caller that must respect a provider's request rate has to
  **serialize** its requests or **share** one transport instance.
- **No scientific deduplication.** Overlapping provider pages stay visible; the
  adapters never deduplicate scientific candidates. There is **no** cross-run or
  cross-provider candidate repository.
- **Each invocation repeats provider observations.** `search()` is **not**
  idempotent persisted candidate ingestion: calling it again re-issues the
  requests and returns fresh observations. Nothing is persisted; the returned
  `SearchResult` lives **in memory** only.
- **No hidden cap.** A page/record budget or the provider's own window is always
  an explicit `stop_reason` (`page_limit`/`failure`), never reported as complete
  retrieval.
- **Untrusted data.** Provider metadata is an untrusted observation. It is never
  accepted project context, eligibility, scientific identity, or a frozen corpus.
