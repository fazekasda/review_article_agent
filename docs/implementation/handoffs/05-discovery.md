# Handoff — step 05: reproducible discovery and corpus freeze (partial)

**Status: PARTIAL — metadata adapter foundation plus an added durable
search-observation slice.** Step 05 is **not** complete and is **not** ticked in
[`docs/implementation/README.md`](../README.md). This handoff documents **two
partial library slices**: (1) the historic bounded, stdlib-only public-metadata
transport plus two provider adapters, preserved below with its original evidence
head; and (2) a newer, still inert **durable search-observation** codec, schema-11
table, repository, and backup/restore verification. Neither slice is a scientific
workflow, and neither makes a scientific, deduplication, coverage, or completeness
claim. The historic adapter evidence is **not** rewritten; the new slice is a
distinct section (§8).

**Scope boundary (authority).** The step document
[`docs/implementation/05-discovery.md`](../05-discovery.md) is authoritative and was
**not** edited. The baseline is [`docs/specification.md`](../../specification.md)
(workflow steps 2–5 and the safety invariants); the accepted policies are
[decision 002](../../decisions/002-step01-contracts-and-policies.md) and the
[policy register](../../contracts/03-policy-decisions.md) (`POL-OPS-COVERAGE`,
`POL-OPS-TRACE`, `POL-OPS-SECONDARY`); the versioned contracts are
[C01](../../contracts/01-identities.md), [C02](../../contracts/02-gates-and-runtime.md),
[C03](../../contracts/03-policy-decisions.md). No baseline invariant, acceptance
criterion, or policy was weakened.

> **Historic adapter evidence (recorded 2026-10-28, not rewritten).** The
> coordinator's **177 discovery tests passed at code HEAD `de179b4`** (with
> `compileall`/`git diff --check` clean) — the adapter slice's targeted run. The
> coordinator's **full frozen combined run** at the same code HEAD `de179b4` (log
> `/tmp/opencode/step05-combined-all-checks.log`) then **passed**: see §5 for the
> exact commands and counts (**2,282 tests total**). That historic run is at code
> HEAD `de179b4`, before any documentation update, and the merged adapter baseline
> was `6e20946` (same **2,282 total**: 2,067 core + 20 scripts + 195 retained
> spike tests). The older checkpoints (core baseline `049a9c6` 1890 tests;
> shared-only `0c856cc` 1957 tests; retained spike runs Python 79 / live-Jev mocked
> 51 / integration mocks 7 / TypeScript 58 + `tsc`; scripts 20; doc checks 29) are
> **historical heads**, superseded by that combined run.
>
> **New durable-observation evidence (2026-10-28).** The added slice is verified
> at integrated **code HEAD `25827dd`** (source-equivalent `7896af8`; T1 codec
> `c8e52f0`, T2 repository `9f1ff31`). The coordinator's earlier codec-only run
> was **223 discovery tests at `c8e52f0`**; this worker ran **252 discovery tests
> OK**, **29 contract-doc checks OK**, **20 scripts tests OK**, and
> `git diff --check` clean at `25827dd` before editing docs (§8.6). The
> coordinator's **full frozen combined suite at the same code HEAD `25827dd`**
> then completed **PASSED** (log
> `/tmp/opencode/search-records-combined.log`, read to verify): Python **3.14.4**,
> **2,142 core tests in 239.488 s**, **20 scripts**, **79 Python retained
> spikes**, **51 live-Jev mock**, **7 integration mock**, and **58 TypeScript**
> tests — **2,357 tests total, no skips**; **29 contract checks**; `tsc --noEmit`
> and `compileall -q src tests` pass; `git diff --check` pass. Existing
> SQLite/subprocess/unclosed-file `ResourceWarning`s are disclosed, not failures.
> No paid/provider/live adapter call or hosted transfer was made; Python 3.11
> remains untested. That combined run is at **code HEAD `25827dd`, before this
> docs update**, **not** at this handoff's own docs commit; the coordinator will
> verify the final docs head. **No full-suite claim is made at the final docs
> commit.**

## 1. Scope, base revisions, and authority used

- **Step document (authoritative):**
  [`05-discovery.md`](../05-discovery.md) — deliverables D1–D4, AC1–AC7, and
  handoff/exclusions unedited.
- **Baseline:** [`docs/specification.md`](../../specification.md) (confirmed; not
  weakened).
- **Historic adapter base:** `de179b4` (the adapter handoff's starting HEAD).
- **Added durable-observation dependencies:** T1 codec `c8e52f0`, T2 repository
  `9f1ff31`, and T2-R1 bounds fix `25827dd` (source-equivalent `7896af8`), which is
  also the starting HEAD of this updated handoff branch.
- **This handoff branch:** `task/step05-records-handoff`. It changes
  **documentation only** (see §7).

## 2. Deliverables → implementation and tests

| Step deliverable | Status | Implementation | Tests |
| --- | --- | --- | --- |
| D1 — brief/interview artifacts, seed search and individual approvals, framing challenge, provisional scope map, rubric/search-plan approval | **Deferred** | none | none |
| D2 — PubMed and Europe PMC adapters plus complementary sources ratified in the project plan | **Partial** (two adapters; complementary sources not ratified) | [`discovery/pubmed.py`](../../../src/review_article_agent/discovery/pubmed.py), [`discovery/europepmc.py`](../../../src/review_article_agent/discovery/europepmc.py) | [`test_discovery_pubmed.py`](../../../tests/test_discovery_pubmed.py), [`test_discovery_europepmc.py`](../../../tests/test_discovery_europepmc.py) |
| D3 — query runs, identity reconciliation, citation expansion, coverage report, explicit freeze/search-again decision | **Partial** (durable inert observation records only; identity reconciliation, citation expansion, coverage, freeze/search-again are deferred) | [`discovery/serialization.py`](../../../src/review_article_agent/discovery/serialization.py), [`storage/discovery.py`](../../../src/review_article_agent/storage/discovery.py) | [`test_discovery_serialization.py`](../../../tests/test_discovery_serialization.py), [`test_discovery_store.py`](../../../tests/test_discovery_store.py) |
| D4 — bounded network retries/rate handling and project cost/work reporting | **Partial** (retries/rate handling only; project reporting deferred) | [`discovery/transport.py`](../../../src/review_article_agent/discovery/transport.py) | [`test_discovery_transport.py`](../../../tests/test_discovery_transport.py) |

Supporting value contracts:
[`discovery/records.py`](../../../src/review_article_agent/discovery/records.py)
([`test_discovery_records.py`](../../../tests/test_discovery_records.py)).

Additional durable-observation support (§8): the versioned codec
[`discovery/serialization.py`](../../../src/review_article_agent/discovery/serialization.py)
([`test_discovery_serialization.py`](../../../tests/test_discovery_serialization.py)),
the schema-11 repository
[`storage/discovery.py`](../../../src/review_article_agent/storage/discovery.py)
([`test_discovery_store.py`](../../../tests/test_discovery_store.py)), and its
backup hook ([`test_discovery_backup.py`](../../../tests/test_discovery_backup.py)).
No adapter was changed by that slice.

## 3. Acceptance criteria — criterion-by-criterion status (all partial/deferred)

| AC | Requirement (short) | Status | Concrete implemented evidence / what is missing |
| --- | --- | --- | --- |
| 1 | Seed abstracts remain provisional; unapproved seeds cannot silently become accepted project context | **Not fulfilled** | No workflow exists, so there is **no authorizing API** that could accept a seed as project context — but the criterion's provisional-seed/approval loop is **absent**. Nothing here makes a seed accepted; equally nothing implements the gate. |
| 2 | Broad searches cannot execute unapproved scope/query plans | **Not fulfilled** | No workflow/approval gate exists. The adapters are a **library**; a caller can call `search()` directly. There is **no** scope/query-plan approval surface, so the criterion is unmet (and no authorizing API was added). |
| 3 | Reconstruct exact queries, timestamps, contributing sources, failures, counts, and deduplication decisions from records | **Partial (improved by §8)** | The adapters retain exact query, UTC start/finish, source, requested URLs, statuses, body digests/bytes, failures, counts, every attempt, and PubMed `query_translation` **in memory**. The §8 codec/store can now **durably** reconstruct those exact observations after reopen/replay for a recorded request key, preserving overlaps, missing IDs, and partial failures. **Deduplication decisions are still absent**: there is no candidate identity, no dedup decision field, and no scientific candidate store. |
| 4 | Fixed response fixtures test pagination, overlap, missing IDs, partial failures, and incremental reruns without duplicate scientific candidates | **Partial (improved by §8)** | Fixtures cover adapter **pagination, overlap, missing IDs, and partial failures** (e.g. `test_discovery_pubmed.py::PartialOutcomeTests`, `test_discovery_europepmc.py::ConsistencyTests`/`CursorTests`), and §8 adds lossless fixture round-trips plus **request-key replay**. Identical replay returns the historical observation; a divergent result is a conflict; different request keys remain separate observations even with identical payload. This is **not** cross-run scientific candidate ingestion, **not** deduplication, and does **not** make repeated `search()` calls idempotent candidate ingestion. **Incremental rerun producing unique scientific candidates remains absent.** |
| 5 | Known-relevant fixture set and forward/backward citation expansion expose explicit coverage gaps; reporting never claims absolute completeness | **Deferred** | Citation expansion and coverage-gap reporting are **not** implemented. The adapters never claim completeness (`complete` only means a bounded retrieval reached a natural end). |
| 6 | Human approval freezes a specific corpus revision; subsequent imports/searches do not mutate it silently | **Deferred** | Corpus freeze and the human freeze gate are **not** implemented. |
| 7 | Public live smoke queries are small, documented, and separate from deterministic results; no arbitrary API result cap truncation hidden as complete retrieval | **Partial** | **No hidden cap**: a page/record budget or the provider window is always an explicit `stop_reason` (`page_limit`/`failure`), never `complete` (e.g. `test_discovery_pubmed.py::HardLimitTests`, `test_discovery_europepmc.py::BoundsTests`). Deterministic tests inject a scripted transport and make **no** network call. **No adapter live smoke** was run (see §5). |

**Remaining deferred work (do not read as done):**

- **D1** — brief/interview, individual seed approvals, framing, scope, rubric, and
  search-plan approval: **all deferred**.
- **D3** — durable inert query **observation** replay is now partially
  implemented (§8); candidate identity/reconciliation, citation expansion,
  coverage reporting, and freeze/search-again remain **deferred**.
- **D4** — project durable cost/work reporting: **deferred** (this slice only
  returns attempts; **no model spend**).
- **Complementary sources** are **not** ratified per project. The historical list
  is configurable; **not every source is mandatory**.

## 4. Behaviour and limits documented

The exact API signatures, defaults, and maximum limits are in
[`docs/discovery.md`](../../discovery.md). Key limits:

- **PubMed window.** `PUBMED_WINDOW_LIMIT = 9999` is a **conservative** stand-in
  for NCBI's own ESearch window; a retrieval that would page past it is an explicit
  `page_limit`. **Date-partitioned paging past the window is deferred.**
- **Unsupported entries.** A `PubmedBookArticle` (or any unexpected child) is an
  explicit `malformed_response`, **never** a silently dropped entry.
- **DOCTYPE.** A normal NCBI EFetch reply's plain **external** `<!DOCTYPE ...>`
  is **accepted without fetching** it; an internal DTD subset, any entity
  declaration, or a non-http(s) DOCTYPE scheme is refused.
- **Europe PMC.** `format=json`, `resultType=lite` (**no abstracts**), native
  `source:id` identity, cursor pagination with a stable `hitCount`, nullable IDs,
  and strict `malformed_response` outcomes.
- **Provenance.** All `SearchPage` attempt provenance and the PubMed
  `query_translation` are retained.
- **Transport policy.** Bounded retries; hard body cap; no redirects; no proxies;
  no credentials.
- **Socket timeout is inactivity, not a wall-clock deadline.** A peer can keep one
  attempt alive far longer than `timeout` seconds.
- **Rate pacing is per-instance, not process-global.** A future caller must
  **serialize** its requests or **share** one transport instance.
- **`Retry-After` too-large sentinel is not the exact magnitude.** An
  unrepresentable value yields the conservative sentinel
  `MAX_RETRY_DELAY_SECONDS + 1.0` and an explicit `retry_after_exceeds_bound`.
- **No scientific dedup / no candidate repository.** No automatic scientific
  candidate deduplication and no cross-run/cross-provider candidate repository.
  The §8 durable table is an **observation store keyed by caller request id**, not
  a candidate store and not a deduplication decision.
- **Each `search()` invocation repeats provider observations.** `search()` itself
  persists nothing and is **not** idempotent candidate ingestion: it re-issues
  requests and returns fresh observations. A trusted caller may separately pass
  that returned value to `record_search`, which durably records the observation;
  an identical request-key replay returns that historical record, but it does not
  rerun the provider and does not ingest scientific candidates.

## 5. Historic adapter-slice verification — commands and results

Environment: **Python 3.14.4** actually used; `requires-python = ">=3.11"` is
**declared but 3.11 was not tested**.

| Command | Result |
| --- | --- |
| `PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_discovery_*.py'` | **177 OK** (this handoff's own run) |
| `python3 scripts/check_contract_docs.py --self-test` | **29 checks ok, 0 failed** |
| `python3 -m unittest discover -s scripts -p 'test_*.py'` | **20 OK** |
| `git diff --check` | clean |

Coordinator evidence — **full frozen combined run at code HEAD `de179b4`** (log
`/tmp/opencode/step05-combined-all-checks.log`), **ALL LOCAL CHECKS PASSED**:

| Check | Result |
| --- | --- |
| `PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_*.py'` | **2067 OK**, no skips |
| `python3 -m unittest discover -s scripts -p 'test_*.py'` | **20 OK** |
| `python3 scripts/check_contract_docs.py --self-test` | **29 checks** |
| `python3 -m unittest discover -s spikes/runtime/python -p 'test_*.py'` | **79 OK** |
| `python3 -m unittest discover -s spikes/runtime/live-jev -p 'test_*.py'` | **51 OK** |
| `python3 -m unittest discover -s spikes/runtime/integration -p 'test_*.py'` | **7 OK** |
| `npm --prefix spikes/runtime/typescript test` | **58 OK** |
| `(cd spikes/runtime/typescript && npx tsc --noEmit)` | pass |
| `python3 -m compileall -q src tests` | pass |
| `git diff --check` | pass |

**Total 2,282 tests.** Existing SQLite/subprocess/unclosed-file `ResourceWarning`s
remain (disclosed, not failures). This run is at the **code HEAD `de179b4`**, i.e.
**before** this documentation update, **not** at this handoff's own docs commit;
the coordinator will prove the product source/tests are identical and run the final
docs checks after cherry-pick. **No latest full-suite claim is made at the docs
commit.**

**Not run / not claimed:** Python 3.11; any adapter live smoke; any paid model call;
any hosted transfer.

**CI:** the repository has **0 configured GitHub workflows** and a prior PR with
an **empty rollup**; **no CI-green claim** is made.

### 5.1 LIVE evidence distinction (webfetch observations, not adapter smoke)

On **2026-10-28** the coordinator used `webfetch` for **two tiny public contract
observations only**:

- PubMed ESearch `term=31452104[uid]&retmax=1` returned `count=1` and one id.
- Europe PMC query `EXT_ID:31452104 AND SRC:MED`, `pageSize=1`,
  `resultType=lite` returned `version 6.9`, `hitCount=1`, one metadata record, and
  **no** `nextCursorMark`.

These are **live WEBFETCH observations**, **not** production adapter smoke — the
adapters were **not** live-tested. The NCBI API index
(`https://www.ncbi.nlm.nih.gov/home/develop/api/`) was fetched; the detailed
`NBK25499`/`NBK25497` pages were **challenge-blocked**. The Europe PMC REST docs
(`https://europepmc.org/RestfulWebService`) were fetched. T3 researched Solr
terminal-cursor guidance — **not** proof of the complete Europe PMC contract. **No
new public API request is authorized or needed for this task.**

## 6. Stubs, limitations, and open decisions

- No discovery CLI, worker scheduling, approval gate, or search-plan approval is
  built.
- The storage schema advances additively to **11** with the inert observation
  table (§8). It stores exact provider observations for a caller-supplied request
  key; it is **not** a scientific candidate store and performs **no**
  deduplication.
- Metadata is an **untrusted observation**; there is no accepted context,
  eligibility, scientific identity, deduplication decision, coverage claim, or
  corpus freeze.
- Open decisions remain: date-partitioned PubMed paging past the window;
  complementary-source ratification; scientific candidate identity and cross-run
  ingestion/dedup; citation expansion; coverage reporting; corpus freeze; project
  cost/work reporting.

## 7. Commits, changed files, and next actions

- **Historic adapter slice base:** `de179b4` (integrated adapter dependencies);
  its handoff commit history is preserved in Git.
- **Added durable-observation slice base:** `25827dd` (T1/T2 integrated; source
  equivalent `7896af8`), which is this updated handoff branch's starting HEAD.
- **This documentation-only update** (`task/step05-records-handoff`) changes only
  the allowlisted docs — **no source, test, config, contract, schema, baseline,
  step-criteria, or policy edit**:
  - `docs/implementation/handoffs/05-discovery.md` — this handoff: preserves the
    historic adapter evidence and adds the distinct §8 durable-slice account;
  - `docs/discovery.md` — adds the codec/repository API and durable-observation
    limits while preserving the adapter reference;
  - `docs/storage.md` — documents additive schema 11, the shared backup/restore
    integrity sweep, and legacy compatibility;
  - `docs/implementation/README.md` — keeps Step 05 **unchecked** and updates its
    partial description to include the inert observation slice;
  - `README.md` — updates the partial Step 05 summary without claiming a workflow
    or completion.
- **Next actions:** review and merge are the user's. Remaining Step 05 work
  (D1, D3 candidate identity/reconciliation/citation expansion/coverage/freeze,
  D4 project cost/work reporting, AC1/AC2/AC5/AC6, complementary-source
  ratification, and cross-run scientific candidate ingestion/dedup) is **not
  started** and needs its own bounded session(s). No comprehensive Step 05 claim
  is made.

## 8. Added partial slice — durable inert search observations (T3)

This section is **new** and distinct from the historic adapter evidence above. It
records what `25827dd` actually implements; it does not revise the adapter claims
or tick Step 05.

### 8.1 Lossless versioned codec (`discovery/serialization.py`)

`encode_search_result(result) -> bytes` emits canonical (sorted-key, compact,
UTF-8) JSON carrying an envelope with exactly `format`
`review_article_agent.discovery.search_result`, `format_version` **1**,
`result_type` (`search_result` or `pubmed_search_result`), and `result`.
`decode_search_result(data) -> SearchResult` rebuilds the exact concrete class
through the real dataclass constructors, so existing invariants still apply.

- **Type preservation.** The exact `SearchResult` or `PubMedSearchResult` subtype
  is preserved, as is the `int` vs `float` distinction of
  `retry_after_seconds`; arrays become tuples; frozen values stay frozen.
- **Lossless observation.** Exact query/source strings, UTC timestamps, requested
  URLs, counts, page order, overlapping and duplicate records, missing
  (`None`) identifiers, explicit stop/failure states, every attempt, and PubMed
  `query_translation` all survive a round trip. Nothing is re-fetched,
  re-parsed, deduplicated, or fabricated.
- **Strict decode.** It refuses invalid UTF-8, malformed or over-deep JSON,
  duplicate object keys, non-finite/overflowing numbers (including `1e999`),
  unknown/missing fields at every level, unknown format/version/type, wrong JSON
  types (including `bool` for `int`), lone Unicode surrogates, and values that
  violate the dataclass contracts. Errors are static and value-free: they never
  echo query, URL, title, identifier, or payload text.
- **Technical 512 MiB cap.** `MAX_SEARCH_RESULT_BYTES` is **536,870,912 bytes
  (512 MiB)**. It is checked before decode parsing and after encode. Crossing it
  is an explicit `DiscoverySerializationError` refusal — never silent truncation,
  a dropped page, or a re-fetch to fit. This is a resource bound derived from the
  adapters' documented envelopes (20,000 PubMed records; up to 1,000,000 Europe
  PMC lite records), **not** a scientific threshold and **not** a completeness
  claim. The envelope retains both the page arrays and the aggregate
  `result.records`/`result.attempts` fields (so a stored aggregate can be checked
  against its pages), therefore duplicated aggregate content **counts toward the
  same 512 MiB cap** in addition to the per-page copies.

### 8.2 Additive schema 11 and public repository API

Store migration 11 adds exactly one immutable table,
`discovery_search_run`, via `storage/schema.py::_store_v11`. Earlier migrations
are not rewritten. The table has `PRIMARY KEY (project_id, run_id)`,
`UNIQUE (project_id, request_id)`, a project foreign key, `UPDATE`/`DELETE`
abort triggers, and CHECK constraints for representation version 1, result type,
non-negative counts, and `complete ∈ {0,1}`. The canonical result bytes are the
truth; projected source/query/time/count/failure columns are re-derived from
them on every read.

Public API in `storage/discovery.py`:

```python
DiscoveryRepository(store)                        # bound to one ProjectStore
repo.project_id                                   # store's actual project id
repo.record_search(*, request_id: str, result: SearchResult) -> DiscoverySearchRecord
repo.get_search(run_id: str) -> DiscoverySearchRecord | None
repo.list_searches() -> list[DiscoverySearchRecord]   # deterministic insertion order
repo.verify_all_integrity() -> list[str]          # static problems; empty = intact
discovery_integrity_problems(conn, project_id) -> list[str]  # read-only backup hook
```

`DiscoverySearchRecord` exposes `project_id`, `run_id`, `request_id`,
`schema_version` (representation version, currently 1), `recorded_at_utc`,
`result_type`, `source`, `query`, `started_at`, `completed_at`, `stop_reason`,
`total_count`, `complete`, `failure`, `page_count`, `record_count`,
`attempt_count`, `result_sha256`, `result_bytes`, `canonical`,
`binding_sha256`, `binding_bytes`, `binding`, and the decoded `result`.

- **Deterministic project-local identity.** `run_id` is
  `"dsr_" + sha256(canonical_json([project_id, request_id]))[:32]`, derived by the
  repository, never supplied by the caller. `request_id` is bounded to 512
  characters and must satisfy the existing identifier contract; a lone surrogate
  is refused before any SQL.
- **Atomic record.** Inputs are canonicalised and round-trip checked before the
  transaction; insertion and immediate re-read happen in one `BEGIN IMMEDIATE`
  transaction. An induced failure writes no row, and concurrent same-key inserts
  yield one row.
- **Static errors.** `DiscoverySearchValidationError` (malformed/mutated/
  unsupported input), `DiscoverySearchConflictError` (divergent replay),
  `DiscoverySearchCorruptionError` (stored row fails digest/projection/binding/
  canonical revalidation), all under `DiscoverySearchError(StoreError)`.
  Messages never echo queries, sources, URLs, titles, identifiers, payload bytes,
  or digests.
- **SQLite projection bounds.** The projected non-negative `total_count` must fit
  signed 64-bit SQLite `INTEGER`; a larger value is refused before insertion with
  a static validation error and no row. Page/record/attempt/byte counts are
  checked too. Values are never truncated, coerced, or stored as floats.

### 8.3 Replay semantics — observation replay, not scientific dedup

- Identical `record_search(request_id=K, result=R)` replay returns the exact
  historical row, including its original `recorded_at_utc`, digest, binding, and
  canonical bytes — also after closing and reopening the store. It does **not**
  create a second row.
- A **different** result under the same project/request key raises
  `DiscoverySearchConflictError` and never overwrites the historical row.
- **Different request keys are separate observations** even when the payload is
  byte-identical (two rows, two run ids, same digest). This is not scientific
  deduplication. Conversely, a fresh `search()` call is **not** replay: it
  re-issues provider requests and returns a fresh observation; only passing that
  value to `record_search` with the same key replays the stored row.
- Every read revalidates the request key/run id, timestamps, canonical size and
  digest, binding, result type, source/query/time/count/failure projections,
  `complete`, and full canonical round-trip. A tampered projection, matching-but-
  forged payload, non-canonical blob, foreign project row, or changed timestamp
  is corruption, not a trusted value. Integrity hashes detect accidental or
  deliberate **corruption only**; they are not authenticity signatures.

### 8.4 Lossless source observations and scope

The recorded value preserves PubMed title/year/DOI/PMCID metadata and
`query_translation`, and Europe PMC native `source:id` identity plus every page's
provider URL/timestamp. Missing IDs stay `None`, overlapping/duplicate records
stay in order, and partial failures keep earlier successful pages plus the failed
page's attempts. The repository never merges, maps, scores, or drops a provider
record.

**Explicitly absent:** network/transport/provider execution, CLI, worker
scheduling, approved seed context, search-plan approval, scientific identity,
candidate deduplication, citation expansion, coverage reporting, corpus freeze,
and project cost/work reporting. A recorded row is private, untrusted provenance:
it may contain exact queries, source URLs, titles, and provider identifiers; it is
not a secret scanner and its hashes do not prove authenticity.

### 8.5 Backup, restore, and compatibility

Schema 11 is within the supported snapshot range **3..11**; manifest
`format_version` stays **1**. `verify_backup`/`restore_backup` run the shared
read-only `discovery_integrity_problems` sweep at schema 11, revalidating every
canonical/digest/binding/projection/run identity. Rewriting the outer manifest
database hash does **not** bypass this inner validation. Restore validates fully
before creating anything, stages the snapshot, registers last, and preserves the
observation bytes, timestamps, and projections exactly.

A schema-10 (legacy) snapshot without the table remains verifiable and restorable;
the ordinary `ProjectStore` open on first use upgrades it to schema 11 and adds
only the empty table. **Older software refuses a newer schema**, so back up before
upgrading and do not expect a downgrade path. Restore proves storage integrity,
not scientific validity or remote quiescence.

### 8.6 Verification of this added slice

Targeted checks at code HEAD `25827dd`, before docs edits:

| Command | Result |
| --- | --- |
| `PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_discovery_*.py'` | **252 OK** |
| `python3 scripts/check_contract_docs.py --self-test` | **29 checks ok, 0 failed** |
| `python3 -m unittest discover -s scripts -p 'test_*.py'` | **20 OK** |
| `git diff --check` | clean |

Confirmed **full frozen combined suite** at the same code HEAD `25827dd`
(log `/tmp/opencode/search-records-combined.log`, read to verify), **ALL CHECKS
PASSED**:

| Check | Result |
| --- | --- |
| `PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_*.py'` | **2,142 OK in 239.488 s**, no skips |
| `python3 -m unittest discover -s scripts -p 'test_*.py'` | **20 OK** |
| `python3 scripts/check_contract_docs.py --self-test` | **29 checks ok, 0 failed** |
| `python3 -m unittest discover -s spikes/runtime/python -p 'test_*.py'` | **79 OK** |
| `python3 -m unittest discover -s spikes/runtime/live-jev -p 'test_*.py'` | **51 OK** (mocked; no paid/provider call) |
| `python3 -m unittest discover -s spikes/runtime/integration -p 'test_*.py'` | **7 OK** |
| `npm --prefix spikes/runtime/typescript test` | **58 OK**, 0 skipped |
| `(cd spikes/runtime/typescript && npx tsc --noEmit)` | pass |
| `python3 -m compileall -q src tests` | pass |
| `git diff --check` | pass |

**Total 2,357 tests.** Python **3.14.4** was used; **3.11 remains untested**.
Existing SQLite/subprocess/unclosed-file `ResourceWarning`s are disclosed, not
failures. No paid/provider/live adapter call or hosted transfer was made. This
run is at **code HEAD `25827dd`, before this documentation update**, **not** at
this handoff's own docs commit; **no full-suite claim is made at the final docs
commit**. The historic adapter total remains **2,282 at `de179b4` / `6e20946`**
and is not conflated with this 2,357-test combined run.
