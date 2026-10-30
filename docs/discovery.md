# Discovery core, metadata adapters, durable observations, and CLI (Step 05)

**Status: complete for the scoped Step 05 discovery workflow** (core library +
personal discovery CLI implemented and verified; final full combined check passed
at head `f3d97fe`, 2,920 tests, no skips). This document
describes the implemented
[Step 05](implementation/05-discovery.md) layers: (1) a bounded, stdlib-only
public-metadata transport and provider adapters; (2) a lossless versioned codec
and an inert, project-local, durable search-observation repository (store schema
11); (3) the **discovery core library** — a typed context read-and-gate layer, an
approved-plan/seed/metadata-import execution library, a durable cumulative
candidate ledger, bounded bidirectional citation expansion, a deterministic
coverage/work/monetary report, an exact human-approved freeze/search-again record,
and a revision-scoped hosted lead-proposal runner; and (4) the **`discovery` CLI
group** that submits/inspects these as detached background jobs. The
criterion-by-criterion status and the final-head check rollup remain in
the [Step 05 handoff](implementation/handoffs/05-discovery.md).

**Current completion state.** The report/freeze **CLI** groups are
**integrated** (`discovery report create|show` and `discovery freeze
propose|status|show|history|require`); this document documents both their library
API and their CLI flags below. The `context`/`corpus`/`lead status|content`
readbacks and the `search`/`expand`/`lead configure|run` job submissions below
are also integrated.

Nothing here is scientific validation, eligibility, study identity, or a
scientific completeness claim. Every provider value is an **untrusted
observation**. Accepted project context exists only behind an exact named human
gate; a candidate grouping is a **review** grouping (never a study-independence
claim); and a corpus freeze is an explicit human approval, never something a
record implies. Recorded observations, snapshots, and reports are private, inert
provenance unless an exact named gate says otherwise.

## What exists

| Module | Role |
| --- | --- |
| `review_article_agent.discovery.transport` | Bounded, deterministic HTTP GET client with a fixed HTTPS allowlist, no credentials, redirects/proxies disabled, retries, size cap, per-instance rate spacing, and immutable attempt provenance. |
| `review_article_agent.discovery.records` | Frozen value contracts: `MetadataRecord`, `SearchPage`, `SearchResult`. |
| `review_article_agent.discovery.pubmed` | PubMed ESearch + EFetch metadata adapter (`PubMedClient`). |
| `review_article_agent.discovery.europepmc` | Europe PMC cursor-paginated metadata adapter (`EuropePMCClient`). |
| `review_article_agent.discovery.serialization` | Versioned, strict, lossless canonical codec for `SearchResult`/`PubMedSearchResult` (`encode_search_result`, `decode_search_result`). |
| `review_article_agent.storage.discovery` | Inert, immutable project-local search-observation repository (`DiscoveryRepository`) over additive store schema 11. |
| `review_article_agent.discovery.citations` | Europe PMC **citation-family** adapter (`EuropePMCCitationClient`) for bounded `references`/`citations` retrieval. |
| `review_article_agent.discovery.context` | Typed discovery-context read-and-gate layer (`validate_context_document`, `read_context_document`, `require_approved_context`, `seed_context`). |
| `review_article_agent.discovery.execution` | Approved-plan query / bounded seed / ratified metadata-import execution library (`DiscoveryExecutor`). |
| `review_article_agent.discovery.reconciliation` | Pure deterministic identifier-overlap review report (`reconcile_searches`). |
| `review_article_agent.discovery.candidates` | Durable cumulative publication-candidate ledger (`CandidateRepository`) over artifact revisions. |
| `review_article_agent.discovery.expansion` | Bounded bidirectional citation traversal (`expand_citations`, `ExpansionReport`). |
| `review_article_agent.discovery.reporting` | Deterministic coverage/work/monetary report builder and repository (`build_coverage_report`, `CoverageRepository`). |
| `review_article_agent.discovery.freeze` | Exact human-approved freeze/search-again record (`FreezeRepository`, `require_frozen`). |
| `review_article_agent.discovery.lead_runner` | Revision-scoped hosted lead-proposal runner (`validate_hosted_use_document`, `execute_lead_proposal`, `get_lead_outcome`). |
| `review_article_agent.runtime.lead` | Direct single-shot tool-free OpenRouter lead adapter (`plan_lead`, `execute_lead`). |
| `review_article_agent.worker.discovery` | Owner-controlled detached discovery **jobs** (five modes) over the execution library. |
| `review_article_agent.cli.discovery` / `.cli.discovery_jobs` | The `discovery` CLI group (context/corpus/lead readbacks; search/expand/lead job submissions). |

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

A caller that issues real requests must **serialize** them — rate pacing is
**per-instance** and the transport is **not thread-safe**, so sharing one instance
preserves spacing only for sequential calls — see *Behaviour notes*.

The durable observation surface is separate from transport. The following is a
**placeholder** and is **not** executed by the docs or tests (it also assumes an
already-open `ProjectStore`, and `search()` would perform network I/O):

```python
from review_article_agent.storage.discovery import DiscoveryRepository

# Placeholder only — never executed by docs or tests:
# repo = DiscoveryRepository(open_store)
# record = repo.record_search(request_id="search-2026-10-28-a", result=pubmed_result)
# same = repo.record_search(request_id="search-2026-10-28-a", result=pubmed_result)
# rows = repo.list_searches()
```

`record_search` stores an **observation**; it does not approve, deduplicate,
rank, expand citations, report coverage, or freeze anything.

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

- **Fixed allowlist** (`ALLOWED_ENDPOINTS`): the NCBI E-utilities
  `https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi` and `.../efetch.fcgi`,
  the Europe PMC REST `https://www.ebi.ac.uk/europepmc/webservices/rest/search`,
  and the **narrowly-shaped Europe PMC citation family** on the same host —
  `.../rest/{source}/{id}/references` and `.../rest/{source}/{id}/citations`
  where both the source and id segments are validated (`build_epmc_citation_url`,
  `validate_epmc_citation_identity`). Any other scheme, host, port, path, URL
  userinfo, or fragment is refused **before a socket is opened**.
- **No credentials.** No `Authorization` header is ever sent; a credential-looking
  query parameter (`api_key`, `token`, `secret`, `password`, `cookie`, `auth`, …)
  is refused.
- **No redirects, no proxies.** Every 3xx is refused; the opener registers an
  empty `ProxyHandler`, so `HTTP_PROXY`/`HTTPS_PROXY`/`no_proxy` are never used.
- **Retries.** A retryable HTTP status (`RETRYABLE_STATUSES = {408, 429, 500, 502,
  503, 504}`) **and** a network/HTTP-protocol error (a broken connection, a
  timeout, or a malformed status line, surfaced as `network_error`) are retried up
  to `max_attempts`. Any **other** non-200 HTTP status is a **static permanent**
  failure and is **never** retried.
- **Body bound.** At most `max_response_bytes + 1` bytes are read; a larger body
  is an explicit `response_too_large` failure, never a silent truncation.
- **`Retry-After`** is parsed from integer seconds or an HTTP-date. Only a value
  that is **unrepresentable** or has **more than 18 significant digits** is
  replaced by the conservative sentinel `MAX_RETRY_DELAY_SECONDS + 1.0` (`61.0`)
  and reported as an explicit `retry_after_exceeds_bound` failure. An ordinary
  value that merely **exceeds the permitted bound** is recorded at its **actual
  magnitude** (e.g. `120` seconds when `max_retry_delay` is `60`) and is likewise
  an explicit `retry_after_exceeds_bound` failure — the sentinel is **not** the
  exact magnitude; the raw header is never retained.
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
  **accepted without fetching** it: stdlib `ElementTree` never fetches **any**
  external DTD, regardless of scheme. An **internal** DTD subset and any
  `<!ENTITY>` declaration are refused, and a DOCTYPE naming a **known unsafe**
  scheme (`file`, `data`, `ftp`, `gopher`, `jar`, `php`, `expect`, `netdoc`,
  `ldap`, `dict`, `smb`) is refused by an explicit check.
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

## Durable search observations (schema 11)

### `discovery.serialization` — codec versions 1 and 2

```python
SEARCH_RESULT_FORMAT = "review_article_agent.discovery.search_result"
SEARCH_RESULT_FORMAT_VERSION = 2             # newest; version 1 still decodes
MAX_SEARCH_RESULT_BYTES = 536_870_912        # 512 MiB

encode_search_result(result: SearchResult) -> bytes
decode_search_result(data: bytes) -> SearchResult
```

- Emits **canonical** (sorted-key, compact, UTF-8) JSON with envelope keys
  exactly `format`, `format_version`, `result_type`, `result`; `result_type` is
  `search_result` or `pubmed_search_result`.
- **Version 2** adds the optional per-record fields `authors` (a JSON array of
  display-name strings, provider order preserved) and `journal` (a string or
  `null`). **Version 1** is the earlier seven-key record with neither field.
  The codec decodes both versions.
- **Backward-compatible encoding:** a result whose records carry neither field
  is still emitted as **version 1** with the exact bytes it had before, so a
  previously stored observation's canonical bytes (and therefore its stored
  digest) never change; a result with at least one non-empty `authors` or
  non-null `journal` is emitted as version 2. `encode(decode(bytes)) == bytes`
  therefore holds for old version-1 rows too.
- Decodes only the exact base `SearchResult` or `PubMedSearchResult` and rebuilds
  it through the real dataclass constructors, so page/source/query agreement,
  flattened aggregates, identifier shapes, `complete` semantics, and
  failure/stop consistency are enforced rather than trusted.
- **Lossless:** exact query/source strings, UTC timestamps, URLs, counts, page
  order, overlapping and duplicate records, missing (`None`) identifiers,
  explicit failures/stop reasons, every attempt, PubMed `query_translation`,
  author lists, journal titles, and
  the `int`/`float` distinction of `retry_after_seconds` survive.
- **Strict:** invalid UTF-8, malformed or over-deep JSON, duplicate object keys,
  non-finite/overflowing numbers (including `1e999`), unknown or missing fields
  at every level, unknown format/version/type, wrong JSON types (including
  `bool` for `int`), lone Unicode surrogates, and value-contract violations are
  refused. Messages are static and never echo payload text.
- **512 MiB technical cap:** checked before decode parsing and after encode. A
  larger payload is an explicit refusal, never silent truncation; it is a
  resource bound, not a scientific threshold or completeness claim. Because
  aggregate `records`/`attempts` are retained alongside the page arrays,
  duplicated aggregate content counts toward the same cap.

### `storage.discovery` — `DiscoveryRepository` (store schema 11)

```python
DiscoveryRepository(store)                        # bound to one ProjectStore
repo.project_id                                   # actual store project id

repo.record_search(*, request_id: str, result: SearchResult) -> DiscoverySearchRecord
repo.get_search(run_id: str) -> DiscoverySearchRecord | None
repo.list_searches() -> list[DiscoverySearchRecord]   # insertion order
repo.verify_all_integrity() -> list[str]              # empty = intact
discovery_integrity_problems(conn, project_id) -> list[str]   # read-only backup hook
```

`DiscoverySearchRecord` fields: `project_id`, `run_id`, `request_id`,
`schema_version` (observation representation, currently `1`), `recorded_at_utc`,
`result_type`, `source`, `query`, `started_at`, `completed_at`, `stop_reason`,
`total_count`, `complete`, `failure`, `page_count`, `record_count`,
`attempt_count`, `result_sha256`, `result_bytes`, `canonical`,
`binding_sha256`, `binding_bytes`, `binding`, `result`.

- Schema migration 11 adds only the immutable `discovery_search_run` table
  (`PRIMARY KEY (project_id, run_id)`, `UNIQUE (project_id, request_id)`, project
  FK, abort-on-UPDATE/DELETE triggers). Earlier migrations are unchanged.
- `run_id` is deterministic and project-local:
  `"dsr_" + sha256(canonical_json([project_id, request_id]))[:32]`; the caller
  supplies only the bounded opaque `request_id` (≤512 characters).
- The exact canonical codec bytes, their SHA-256/size, and a canonical binding
  (project/run/request/recorded time/type/digest/size) are stored. Projected
  source/query/time/count/failure columns are re-derived from the decoded bytes on
  every read; a tampered projection, digest, binding, timestamp, non-canonical
  blob, or foreign-project row is `DiscoverySearchCorruptionError`.
- `total_count` must be a non-negative signed 64-bit SQLite integer; a larger
  value is refused before insertion with no row. Other projected counts are range
  checked too. Values are never truncated or coerced.
- Errors are static and value-free: `DiscoverySearchValidationError`,
  `DiscoverySearchConflictError`, `DiscoverySearchCorruptionError` (all under
  `DiscoverySearchError(StoreError)`).

**Replay semantics.** An identical request-key + canonical-result replay returns
the exact historical row, including its original `recorded_at_utc`, also after a
reopen. A changed result under the same key is a static conflict and never
overwrites. Different request keys are separate observations even for identical
payloads — this is **not** scientific deduplication. A fresh `search()` is **not**
replay: it re-issues provider requests; only `record_search` with the same key
replays stored bytes.

**Preserved source observations.** PubMed title/year/DOI/PMCID, authors, journal,
and `query_translation`, Europe PMC native `source:id` identity, authors/journal,
requested URLs and timestamps, missing IDs, overlaps/duplicates in page order,
and partial failures with earlier successful pages are all retained exactly. No
merge, map, score, or drop occurs.

`verify_backup`/`restore_backup` share the same schema-11 read-only integrity
sweep; a schema-10 legacy snapshot is still supported and is upgraded by the
ordinary store open. Older software refuses a newer schema, so back up before
upgrading; there is no downgrade path. Restore proves storage integrity only.

**Boundaries.** The repository performs no network, provider, transport, CLI,
worker, approval, or model call and makes no scientific decision. It is not
accepted context, eligibility, scientific identity, candidate deduplication,
citation expansion, coverage, corpus freeze, or cost reporting. Its private bytes
may contain exact queries, source URLs, titles, and provider identifiers; hashes
detect corruption, **not** authenticity, and this is **not** a secret scanner.

## Discovery core library (Step 05)

The Step 05 core adds **no new store table** beyond the schema-11
`discovery_search_run` observation above: its typed context, candidate,
coverage, and freeze records are stored as immutable **artifact revisions**
(`report`/`rubric` kinds in the existing schema-3 artifact/revision tables), and
its detached **jobs** use the existing schema-4 job tables. All core modules are
stdlib-only, perform no approval or scientific decision, and expose **no in-band
authority**: there is deliberately no `approved`/`actor`/`authority`/
`authenticated` field, and any extra envelope/payload field is refused.

### `discovery.context` — typed read-and-gate layer

```python
DISCOVERY_CONTEXT_SCHEMA_VERSION = 1
DOCUMENT_TYPES = ("discovery_brief", "discovery_interview", "discovery_seed",
                  "discovery_framing", "discovery_scope", "discovery_rubric",
                  "discovery_plan")
SEED_ABSTRACT_CONTEXT = "provisional"
RUBRIC_UNCERTAIN_ACTION = "retain"
PLAN_QUERY_SOURCES = ("pubmed", "europepmc")
COMPLEMENTARY_SOURCE_MODE = "metadata_import"

ARTIFACT_REVISION_GATE_KIND = "artifact_revision"
SEED_GATE_PREFIX = "discovery.seed."      # per-seed gate id: "discovery.seed.<artifact_id>"
PROTOCOL_GATE_KIND = "protocol"
PLAN_GATE_ID = "discovery.plan"

validate_context_document(document: object) -> dict
read_context_document(store, ref, *, expected_type=None, require_current=True) -> dict
require_approved_context(store, ref, *, gate_kind, gate_id, expected_type) -> dict
seed_context(store, seed_refs: object) -> tuple[dict, ...]
```

- Every document is one `{schema_version, document_type, payload}` envelope
  (`DISCOVERY_ENVELOPE_KEYS`, schema version `1`); **explicit** payload fields are
  required per type — **no** scientific default is supplied for any of them.
- A plan's direct references are typed exactly: `brief_ref`→`discovery_brief`,
  `interview_ref`→`discovery_interview`, `framing_ref`→`discovery_framing`,
  `scope_ref`→`discovery_scope`, `rubric_ref`→`discovery_rubric`, and every
  seed→`discovery_seed`; an interview pins a `discovery_brief`. The expected type
  is checked **before** the referenced document's own cross-checks, so a
  self-referential or cyclic reference is refused, never followed.
- `require_approved_context` and `seed_context` succeed only when the **exact**
  named gate instance is currently satisfied by a real human approval in the
  [`HumanApprovalRepository`](../src/review_article_agent/storage/human_approvals.py)
  (per-seed `artifact_revision`/`discovery.seed.<artifact_id>` for seeds;
  `protocol`/`discovery.plan` for a plan), never a bare gate name.
- A **seed abstract stays `abstract_context = "provisional"`** with or without its
  individual seed approval; a validated/approved document is still untrusted data.

### `discovery.execution` — approved-plan / seed / import library

```python
DiscoveryExecutor(store, *, registry, activation_token, transport=None)
  .execute_query(plan_ref, query_index, *, request_id)
  .seed_search(source, query, *, request_id, page_size, max_pages)
  .import_metadata(plan_ref, *, source, records, origin_locator, observed_at, request_id)

SEED_MAX_RECORDS = 20          # page_size * max_pages
SEED_MAX_PAGES = 1
MAX_IMPORT_RECORDS = 100_000
MAX_ORIGIN_LOCATOR_CHARS = 4096
MAX_REQUEST_ID_CHARS = 512
METADATA_IMPORT_MODE = "metadata_import"
PLAN_QUERY_SOURCES = ("pubmed", "europepmc")
query_fingerprint(*, source, query, page_size, max_pages) -> str
```

- `execute_query` runs the **exact** query at `query_index` of the
  currently-approved plan; the caller can **never** override the source, query
  text, or page budget (they come from the pinned plan). An unapproved, stale,
  superseded, mistyped, or cross-project plan is a fail-closed block with **zero**
  provider I/O.
- `seed_search` is bounded exploratory search (`<= SEED_MAX_RECORDS` records over
  `<= SEED_MAX_PAGES` page) and is **always provisional**; no plan is inferred.
- `import_metadata` records a ratified metadata-only observation; `source` must
  appear **exactly** in the approved plan's `complementary_sources` with mode
  `metadata_import`. The library downloads **nothing** and reads **no** file.
- Durability: the caller key plus the exact plan binding + query index +
  `query_fingerprint` (or the import operation fingerprint) derive a deterministic
  `request_id`; a divergent plan/query/import under the same caller key produces a
  **separate** observation. There is **no** fabricated "receipt" artifact; the
  observation identity is the derived key.
- The plan **and** the active execution activation are revalidated before **every**
  low-level HTTP attempt and again before the local commit, so a change, drain, or
  pause stops further attempts. Already-received pages survive a mid-search drain.

### `discovery.reconciliation` / `discovery.candidates` — conservative matching

```python
reconcile_searches(runs) -> ReconciliationReport           # pure, deterministic

CandidateRepository(store)
  .ingest(run_ids, *, request_id, rights, revision_id=None,
          artifact_id="discovery.corpus", dependencies=()) -> Revision
  .current(artifact_id="discovery.corpus") -> dict | None
  .get(ref) -> dict

DOCUMENT_TYPE = "discovery_corpus"
DEFAULT_CORPUS_ARTIFACT_ID = "discovery.corpus"
UNRESOLVED_REASONS = ("missing_identifiers", "identifier_conflict")
DECISION_KINDS = ("minted", "occurrence_attached", "merged_bridging")
```

- Matching is **exact asserted identifier tokens only** — provider-native
  `(source, source_id)` and namespaced `pmid`/`pmcid`/`doi`. Cases, prefixes,
  DOI/URL spellings, titles, and years are never normalized, guessed, or fuzzed.
  A group is a **review** grouping, **never** a study-independence claim.
- **Every occurrence is retained.** A missing-identifier record stays an explicit
  occurrence-local unresolved candidate; an identifier **conflict** is never
  silently merged (prior candidates are preserved and the conflict is recorded).
  There is **no title/year merge, ever**.
- A `request_id` replay returns the historical revision; a divergent reuse
  conflicts. A new ingest names the current head as parent, so a concurrent stale
  write is refused. A default ingest refuses to append to a logical corpus
  artifact that already carries an authentic approved freeze (see below).

### `discovery.expansion` — bounded bidirectional citation traversal

```python
ExpansionLimits(max_hops, max_sources, max_requests, page_size, max_pages)
expand_citations(store, roots, *, request_id, limits,
                 transport=None, before_request=None) -> ExpansionReport

DIRECTIONS = ("references", "citations")     # backward then forward
MAX_HOPS_HARD = 256; MAX_SOURCES_HARD = 1_000_000; MAX_REQUESTS_HARD = 1_000_000
```

- Breadth-first, fixed direction order `references` then `citations`; discovered
  children are queued in provider order; cycles and already-seen nodes are never
  re-expanded. **No universal default limits** — the caller supplies every value;
  the module only enforces generous **technical** bounds, never a scientific
  cutoff.
- `max_requests` counts **every** real low-level attempt (including retries).
  Hitting it, a page limit, a failure, or a `before_request` refusal is an explicit
  `request_limit`/`page_limit`/`failure`/`before_request_refused` **gap**, never
  "complete".
- A **citation/reference link is bibliographic metadata only**: `verified_support`
  and `completeness_claimed` are always `False`, and an edge is **not** evidence
  that a claim is supported, contradicted, or new. An interrupted relation keeps
  every already-observed page and edge; a replay reuses the saved result with no
  provider request.

### `discovery.reporting` — deterministic coverage/work/monetary report

```python
build_coverage_report(store, corpus_ref, *, plan_ref, expansion_reports=(),
                      known_relevant=(), limitations=()) -> dict
CoverageRepository(store).record_report(corpus_ref, *, plan_ref, request_id, rights,
                      expansion_reports=(), known_relevant=(), limitations=(),
                      revision_id=None, artifact_id="discovery.coverage")

DOCUMENT_TYPE = "discovery_coverage"
DEFAULT_COVERAGE_ARTIFACT_ID = "discovery.coverage"
```

- **Every count is derived from actual stored rows**, never a caller statistic.
  The report separates corpus scope (`sources`) from project scope (`work`: the
  cumulative per-run observations, pages, real HTTP attempts, response bytes,
  statuses, failures, and metadata-import occurrences of **every** observation).
- A supplied `known_relevant` token set drives explicit **known-relevant gaps**
  (`status`, `tokens`, `matched`, `missing`); the report never claims absolute
  completeness.
- The **monetary** view separates `direct_call` from `opencode_session` routes
  with `known`/`estimated`/`unknown` amounts and reports the single authoritative
  combined budget exposure from `JobRepository.budget_usage`. A genuinely unknown
  cost stays `unknown` with **no** amount (**never `0`**); synthetic virtual
  reserves are labelled separately and never claimed as paid spend.
- The report is **non-authorizing** (`authorizing`/`completeness_claimed` always
  `False`) and never freezes a corpus. It is stored as an immutable `report`
  artifact revision pinning the exact corpus/plan (and any expansion) revisions.

### `discovery.freeze` — exact human-approved freeze / search-again

```python
FreezeRepository(store)
  .propose(corpus_ref, coverage_ref, *, decision, entry_reviews, rationale,
           request_id, rights, revision_id=None, artifact_id="discovery.freeze",
           accept_coverage_limitations=None) -> Revision
  .get(ref) -> dict
  .current_decision(artifact_id="discovery.freeze") -> dict | None
  .frozen_corpus(artifact_id="discovery.freeze") -> dict | None
  .require_frozen(artifact_id="discovery.freeze", *, at_ref=None) -> dict
  .history(artifact_id="discovery.freeze") -> list[dict]

DECISIONS = ("freeze", "search_again")
DISPOSITIONS = ("include", "exclude", "defer", "no_scientific_use")
DOCUMENT_TYPE = "discovery_freeze_decision"
FREEZE_GATE_KIND = "artifact_revision"; FREEZE_GATE_ID = "discovery.freeze"
```

- A `propose` is **inert**: it records the proposed `freeze`/`search_again`
  decision, the exact corpus/coverage/plan pins, per-candidate entry reviews (the
  four distinguished integrity statuses, a disposition, a rationale), the
  explicit coverage-limitation acknowledgement, and the rationale — with **no**
  `approved`/`actor`/`authority` field. `require_frozen` returns a selection
  **only** when the exact revision carries a currently-active authentic human
  approval of the exact named gate `artifact_revision`/`discovery.freeze`, the plan
  is still approved, and every pinned input is intact. **A generic tool permission
  never satisfies the gate**, and an in-band flag would be refused, not honored.
- A `search_again` decision may carry unresolved-review gaps; a freeze requires
  every entry reviewed. Freezes are immutable: a default corpus ingest refuses to
  append to a logical corpus artifact that already carries an authentic freeze —
  the caller must choose a **new** corpus artifact. Later observations still
  record, and a **new corpus/coverage artifact** reconciles freely, leaving the
  **old frozen roster unchanged**; a superseded freeze (by a later `search_again`)
  is preserved as honest history, never erased.

### `discovery.lead_runner` + `runtime.lead` — hosted lead proposal

```python
HOSTED_USE_DOCUMENT_TYPE = "discovery_hosted_use"
HOSTED_USE_GATE_KIND = "batch"
HOSTED_USE_GATE_PREFIX = "discovery.hosted_use."   # gate id: "<prefix><purpose>"
HOSTED_USE_SCHEMA_VERSION = 1
HOSTED_USE_PURPOSES = ("interview", "framing")

validate_hosted_use_document(document: object) -> dict
hosted_use_gate_id(purpose: str) -> str
execute_lead_proposal(store, *, registry, activation_token, batch_ref,
                      request_id, api_key, transport=None) -> dict
get_lead_outcome(store, request_id) -> dict | None
```

- A hosted-use batch is a `discovery_hosted_use` report whose payload is
  `{purpose, config, source_refs, provider_terms, privacy_review,
  pricing_evidence}`. It only **describes** an authorization; the real authority
  is the out-of-band owner-authenticated `batch`/`discovery.hosted_use.<purpose>`
  gate recorded through the human-approval repository. Evidence refs are checked
  only to be present, non-empty, and time-valid — never a machine legal or
  scientific determination.
- `execute_lead_proposal` re-reads the exact approved batch, revalidates the named
  gate and every pinned source's hosted-transfer rights and context, records a
  one-step lead task, and reserves a `direct_call` dispatch **before** the single
  POST. One logical request per durable job: an already-recorded request is
  returned from storage without a second POST; an ambiguous outcome keeps its
  reserve and is **never** re-sent; a process restart reconciles to `unknown`
  (no blind retry). It never activates execution itself.
- The underlying `runtime.lead` adapter is stdlib-only, single-shot, tool-free,
  and POSTs **one** documented endpoint (`ENDPOINT`); `plan_lead` computes an
  exact `Decimal` reserve from the **full configured model context capacity** plus
  `max_output_tokens` at the configured unit prices — **never** a token-length
  estimate. `PURPOSES = ("interview", "framing")`; `OUTCOMES = ("completed",
  "refused", "protocol_error", "unknown", "failed")`; `COST_STATES = ("known",
  "estimated", "unknown", "not_incurred")`. A malformed 200 is never
  `not_incurred`; a response error/refusal/tool call/`length` finish/wrong model is
  never `completed`. Every `config` (project/model/provider/pricing/rights) is
  supplied explicitly — **no** scientific or provider default is invented.

## Detached discovery background jobs (`worker.discovery`)

```python
MODE_QUERY = "query"; MODE_SEED = "seed"; MODE_METADATA_IMPORT = "metadata_import"
MODE_EXPAND = "expand"; MODE_LEAD = "lead"
DISCOVERY_MODES = (MODE_QUERY, MODE_SEED, MODE_METADATA_IMPORT, MODE_EXPAND, MODE_LEAD)
LEAD_CREDENTIAL_ENV = "OPENROUTER_API_KEY"
RESULT_KIND_OBSERVATION = "discovery_observation"
RESULT_KIND_EXPANSION = "discovery_expansion"
RESULT_KIND_LEAD = "discovery_lead"

validate_submission(..., mode, parameters, project_id) -> dict
dispatch(store, registry, token, job, transport, *, job_held=None,
         lead_api_key=None, lead_transport=None) -> Any
persisted_expansion_report(store, *, job_id=None, request_id=None) -> dict | None
```

- This is the thin owner-controlled glue between the durable worker engine and the
  execution library. It strictly validates and canonicalises a submission into
  immutable job parameters (one `task_kind` per mode, **no** reserve, **no**
  actor/authority field), and dispatches one job through the execution library
  using the exact job request id as the replay key.
- **Lifecycle.** Jobs are submitted through the owner-only control transport and
  run in the **detached worker**; the existing
  `job status|list|attach|pause|resume|cancel|drain|switch|reconcile` controls
  apply unchanged. A submission returns a **queued admission only** — never a
  completion, never a provider call at submit time.
- **Global serial.** Exactly one project is execution-active app-wide; the
  job-level stop check (paused/cancelled/draining) runs **before every real
  attempt**, and the execution library revalidates the approved plan and the
  activation before each attempt.
- **Restart / unknown.** An already-recorded observation is replayed with **no**
  provider I/O (a replay-only probe proves it); an interrupted/unknown job recovers
  to `unknown` and is **never** blindly re-sent. Reconciliation is the worker's
  responsibility, not this library's.
- **The in-process guard is a write precondition, not an authorizer.** The trusted
  Python `before_request`/`guard_transport` callbacks and the S1 `_guard` run
  inside the worker's single process and only **refuse** an upcoming write/request;
  they confer **no** authority, are not a cross-process security boundary, and
  never substitute for the exact named human gate or the activation token.

## The `discovery` CLI group (`cli.discovery`, `cli.discovery_jobs`)

The `discovery` group is integrated and adds **no** lifecycle authority: the
existing `job …` controls remain the only lifecycle controls. **Default outputs
are private, safe summaries** (counts, refs, flags — never raw abstracts, titles,
tokens, or graphs); **`--content` is the explicit opt-in** to private,
non-authorizing content. Every submission returns a queued admission
(`admission_only: true`, `job_completion: false`).

```console
# Typed context (proposal only; records no approval)
discovery context import --artifact-id ID --revision-id ID --file ctx.json --rights-file rights.json [--request-id ID] [--parent-revision-id ID]
discovery context show   --artifact-id ID [--revision-id ID] [--content] [--historical]
discovery context validate --artifact-id ID [--revision-id ID] [--content] [--historical]

# Cumulative candidate snapshots
discovery corpus ingest --run-id ID [--run-id ID ...] --revision-id ID --request-id ID --rights-file rights.json [--artifact-id ID]
discovery corpus show   [--artifact-id ID] [--revision-id ID] [--content]

# Hosted lead readback (no call)
discovery lead status  --request-id ID
discovery lead content --request-id ID          # PRIVATE, NON-AUTHORIZING

# Detached background job submissions (queued admission only)
discovery search run    --artifact-id ID --revision-id ID --query-index N --request-id ID [--timeout S]
discovery search seed   --source SOURCE --query TEXT --page-size N --max-pages N --request-id ID [--timeout S]
discovery search import --artifact-id ID --revision-id ID --file records.json --source SOURCE --origin-locator LOCATOR --observed-at INSTANT --request-id ID
discovery expand run    --artifact-id ID --revision-id ID --corpus-artifact-id ID --corpus-revision-id ID --roots-file roots.json --request-id ID
discovery expand show   (--job-id ID | --request-id ID) [--content]
discovery lead configure --artifact-id ID --revision-id ID --file hosted_use.json --rights-file rights.json [--request-id ID] [--parent-revision-id ID]
discovery lead run       --batch-artifact-id ID --batch-revision-id ID --credential-env OPENROUTER_API_KEY --request-id ID

# Deterministic coverage report + exact freeze/search-again readbacks (no worker, no network)
discovery report create --corpus-artifact-id ID --corpus-revision-id ID --plan-artifact-id ID --plan-revision-id ID --revision-id ID --request-id ID --rights-file rights.json [--artifact-id ID] [--expansion-job-id ID ...] [--known-relevant-file PATH] [--limitations-file PATH]
discovery report show   [--artifact-id ID] [--revision-id ID] [--content]
discovery freeze propose --corpus-artifact-id ID --corpus-revision-id ID --coverage-artifact-id ID --coverage-revision-id ID --decision freeze|search_again --entry-reviews-file reviews.json --rationale TEXT --request-id ID --revision-id ID --rights-file rights.json [--artifact-id ID] [--accept-coverage-limitations EXPLANATION]
discovery freeze status  [--artifact-id ID]
discovery freeze show    [--artifact-id ID] [--revision-id ID] [--content]
discovery freeze history [--artifact-id ID]
discovery freeze require [--artifact-id ID] [--content] [--require-active]
```

- `search run` needs an exact approved-plan artifact/revision and a query index;
  the plan's query/limits are **not** overridable. `search seed` is provisional;
  `search import` reads a **user-chosen local file only** (no arbitrary download)
  and is byte-bounded before a worker is ensured. `expand run` takes the only
  accepted limits from the approved plan (**no** CLI override); `expand show`
  returns a safe summary and the full private mapping only with `--content`.
- `lead configure` stores one hosted-use batch as a proposal and returns an
  **non-authorizing** approval request plus snapshot-preparation guidance
  (`gate prepare`/`gate confirm`); it records no approval and verifies no term.
- **Credential handling.** `lead run` **requires**
  `--credential-env OPENROUTER_API_KEY`; the credential **value** is never
  accepted on the command line, sent over IPC, persisted, or logged. The worker
  reads the named environment variable from **its own** environment at dispatch;
  the CLI may ensure a worker inherits its environment and warns when the key is
  absent, but it **never** restarts or reconfigures a user's worker. There is no
  key over IPC and **no** automatic paid call.
- **Report / freeze CLI (integrated).** `report create` records one immutable
  coverage revision (exact corpus/plan refs; `--expansion-job-id` resolves only a
  real completed expansion report; optional strict known-relevant/limitations
  files) and claims no completeness. `report show` and `freeze show` are safe
  summaries by default with `--content` the explicit private opt-in. `freeze
  propose` records one **inert** freeze/search-again proposal from an explicit
  caller per-candidate `--entry-reviews-file` (the `entry_reviews` schema
  documented with the `discovery.freeze` module above: every candidate exactly
  once; an `include` needs a resolved candidate whose four integrity statuses are
  all **known** — `unknown` is not clean),
  a literal `--accept-coverage-limitations` explanation, and `--rights-file`, and
  returns the exact named-gate `approval_request` plus `gate prepare`/`gate
  confirm` guidance — it records no approval. `freeze status|history|require` are
  read-only; `require` always consults `require_frozen` and returns a safe summary
  by default (exact corpus ref, `included_count`, decision, active approval);
  **only `--content`** opts in to the private roster, while `--require-active` is a
  redundant active-check alias that keeps the safe summary and never returns the
  roster.

## Behaviour notes (limits to keep in mind)

- **Socket timeout is inactivity, not a deadline.** `timeout` bounds connection
  and read *inactivity*; a peer that dribbles bytes just under it can keep one
  attempt alive far longer than `timeout` seconds. The byte cap bounds memory but
  is not a total-deadline guarantee.
- **Rate pacing is per-instance and the transport is not thread-safe.**
  `rate_spacing` is tracked on each `DiscoveryTransport` instance, and two
  instances (or two processes) do not coordinate. A shared instance preserves
  spacing only for **sequential** calls; concurrent threads or processes are
  **not** coordinated. A caller must **serialize** its `search()`/`get()` calls
  (even when sharing one transport) to respect a provider's request rate.
- **Conservative matching, never a study-independence claim.** Overlapping
  provider pages stay visible in the observation store. The cumulative candidate
  ledger groups occurrences **only** on exact shared identifiers (provider-native
  or `pmid`/`pmcid`/`doi`); it never merges on title/year, never decides study
  independence, and leaves missing IDs and identifier conflicts explicit and
  unresolved. A candidate is a **review** grouping, not accepted context.
- **Adapters: bound and metadata-only.** PubMed pages only within
  `PUBMED_WINDOW_LIMIT = 9999` (a conservative stand-in for NCBI's window; a
  retrieval that would page past it is an explicit `page_limit`, never complete)
  and keeps **bibliographic metadata only** (PMID/title/year/DOI/PMCID — abstracts
  are never parsed). Europe PMC search uses `resultType=lite` (**metadata only, no
  abstracts**) with a bounded page size and cursor pagination. The citation-family
  adapter returns relation metadata only, with no support/evidence meaning.
- **Each invocation repeats provider observations.** `search()` is **not**
  idempotent persisted candidate ingestion: calling it again re-issues the
  requests and returns fresh observations. `search()` itself persists nothing; a
  trusted caller may pass its returned value to `record_search`, which durably
  records that exact observation. Request-key replay does not rerun the provider.
- **No hidden cap.** A page/record budget or the provider's own window is always
  an explicit `stop_reason` (`page_limit`/`failure`), never reported as complete
  retrieval.
- **Untrusted data.** Provider metadata is an untrusted observation. It is never
  accepted project context, eligibility, scientific identity, or a frozen corpus.
- **Default private summaries.** CLI readbacks default to a safe summary; the
  private validated document/JSON is returned only with the explicit `--content`.
  The summary never echoes raw abstracts, titles, tokens, or graphs.
- **Evidence scope.** A tiny, separately authorized public-metadata smoke
  exercised the tested **transport/adapter mechanisms** only (3 transport-level
  webfetch contract observations, 3 real search-adapter GET attempts — PubMed 2 +
  Europe PMC 1 — and 2 citation-adapter GETs); no paid app/model call, upload, or
  live lead validation was performed. These counts say nothing about literature or
  topic coverage, and the frozen verification checkpoint is not final-head
  evidence.
