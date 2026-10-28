# Handoff — step 05: reproducible discovery and corpus freeze (partial)

**Status: PARTIAL — metadata adapter foundation.** Step 05 is **not** complete and
is **not** ticked in [`docs/implementation/README.md`](../README.md). This handoff
documents the **first partial slice** only: a bounded, stdlib-only public-metadata
transport plus two provider adapters. It is a **library foundation**, not a
scientific workflow, and it makes **no** scientific or completeness claim.

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

> **Evidence distinction.** The coordinator's **177 discovery tests passed at
> `de179b4`** (with `compileall`/`git diff --check` clean). That is the slice's
> targeted run. The broader **historical** checkpoints (core baseline `049a9c6`
> 1890 tests; a shared-only head `0c856cc1957`; retained spike runs Python 79 /
> live-Jev mocked 51 / integration mocks 7 / TypeScript 58 + `tsc`; scripts 20;
> doc checks 29) are **older heads, not one latest combined run**. The full
> coordinator combined final run is **pending** and, if recorded, will live
> outside these docs. **No final suite count or SHA is invented here.**

## 1. Scope, base revisions, and authority used

- **Step document (authoritative):**
  [`05-discovery.md`](../05-discovery.md) — deliverables D1–D4, AC1–AC7, and
  handoff/exclusions unedited.
- **Baseline:** [`docs/specification.md`](../../specification.md) (confirmed; not
  weakened).
- **Integrated dependencies T1/T2/T3:** `de179b4` (this slice's base and the
  starting HEAD of this handoff branch).
- **This handoff branch:** `task/step05-handoff`. It changes **documentation only**
  (see §7).

## 2. Deliverables → implementation and tests

| Step deliverable | Status | Implementation | Tests |
| --- | --- | --- | --- |
| D1 — brief/interview artifacts, seed search and individual approvals, framing challenge, provisional scope map, rubric/search-plan approval | **Deferred** | none | none |
| D2 — PubMed and Europe PMC adapters plus complementary sources ratified in the project plan | **Partial** (two adapters; complementary sources not ratified) | [`discovery/pubmed.py`](../../../src/review_article_agent/discovery/pubmed.py), [`discovery/europepmc.py`](../../../src/review_article_agent/discovery/europepmc.py) | [`test_discovery_pubmed.py`](../../../tests/test_discovery_pubmed.py), [`test_discovery_europepmc.py`](../../../tests/test_discovery_europepmc.py) |
| D3 — query runs, identity reconciliation, citation expansion, coverage report, explicit freeze/search-again decision | **Deferred** (only in-memory returned attempts) | none (values only) | none |
| D4 — bounded network retries/rate handling and project cost/work reporting | **Partial** (retries/rate handling only; project reporting deferred) | [`discovery/transport.py`](../../../src/review_article_agent/discovery/transport.py) | [`test_discovery_transport.py`](../../../tests/test_discovery_transport.py) |

Supporting value contracts:
[`discovery/records.py`](../../../src/review_article_agent/discovery/records.py)
([`test_discovery_records.py`](../../../tests/test_discovery_records.py)).

## 3. Acceptance criteria — criterion-by-criterion status (all partial/deferred)

| AC | Requirement (short) | Status | Concrete implemented evidence / what is missing |
| --- | --- | --- | --- |
| 1 | Seed abstracts remain provisional; unapproved seeds cannot silently become accepted project context | **Not fulfilled** | No workflow exists, so there is **no authorizing API** that could accept a seed as project context — but the criterion's provisional-seed/approval loop is **absent**. Nothing here makes a seed accepted; equally nothing implements the gate. |
| 2 | Broad searches cannot execute unapproved scope/query plans | **Not fulfilled** | No workflow/approval gate exists. The adapters are a **library**; a caller can call `search()` directly. There is **no** scope/query-plan approval surface, so the criterion is unmet (and no authorizing API was added). |
| 3 | Reconstruct exact queries, timestamps, contributing sources, failures, counts, and deduplication decisions from records | **Partial** | Exact query, UTC start/finish, source, requested URLs, statuses, body digests/bytes, failures, counts, and **every** attempt are retained **in memory** (`AttemptRecord`, `SearchPage`, `SearchResult`; PubMed also retains `query_translation`). **Durable** reconstruction is **not** implemented — nothing is persisted. |
| 4 | Fixed response fixtures test pagination, overlap, missing IDs, partial failures, and incremental reruns without duplicate scientific candidates | **Partial** | Fixtures cover adapter **pagination, overlap, missing IDs, and partial failures** (e.g. `test_discovery_pubmed.py::PartialOutcomeTests`, `test_discovery_europepmc.py::ConsistencyTests`/`CursorTests`). **Incremental rerun producing unique scientific candidates is NOT implemented** — there is no dedup and no cross-run repository. |
| 5 | Known-relevant fixture set and forward/backward citation expansion expose explicit coverage gaps; reporting never claims absolute completeness | **Deferred** | Citation expansion and coverage-gap reporting are **not** implemented. The adapters never claim completeness (`complete` only means a bounded retrieval reached a natural end). |
| 6 | Human approval freezes a specific corpus revision; subsequent imports/searches do not mutate it silently | **Deferred** | Corpus freeze and the human freeze gate are **not** implemented. |
| 7 | Public live smoke queries are small, documented, and separate from deterministic results; no arbitrary API result cap truncation hidden as complete retrieval | **Partial** | **No hidden cap**: a page/record budget or the provider window is always an explicit `stop_reason` (`page_limit`/`failure`), never `complete` (e.g. `test_discovery_pubmed.py::HardLimitTests`, `test_discovery_europepmc.py::BoundsTests`). Deterministic tests inject a scripted transport and make **no** network call. **No adapter live smoke** was run (see §5). |

**Remaining deferred work (do not read as done):**

- **D1** — brief/interview, individual seed approvals, framing, scope, rubric, and
  search-plan approval: **all deferred**.
- **D3** — durable query runs, candidate identity, citation expansion, coverage,
  and freeze: **all deferred**.
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
- **No scientific dedup / no repository.** No automatic scientific candidate
  deduplication and no cross-run/cross-provider candidate repository.
- **Each invocation repeats provider observations.** `search()` is **not**
  idempotent persisted candidate ingestion: it re-issues requests and returns
  fresh observations; nothing is persisted.

## 5. Verification — commands and results

Environment: **Python 3.14.4** actually used; `requires-python = ">=3.11"` is
**declared but 3.11 was not tested**.

| Command | Result |
| --- | --- |
| `PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_discovery_*.py'` | **177 OK** (this handoff's own run) |
| `python3 scripts/check_contract_docs.py --self-test` | **29 checks ok, 0 failed** |
| `python3 -m unittest discover -s scripts -p 'test_*.py'` | **20 OK** |
| `git diff --check` | clean |

Coordinator evidence (recorded, not re-run here): **177 discovery tests passed at
`de179b4`**, with `compileall`/`diff` clean. Historical checkpoints (older heads,
not one combined run): core baseline `049a9c6` **1890 tests pass**; shared-only
head `0c856cc1957` core pass; retained spike baseline Python **79** / live-Jev
mocked **51** / integration mocks **7** / TypeScript **58 + `tsc`**; scripts
**20**; doc checks **29**. **No final combined suite count or SHA is claimed.**

**Not run / not claimed:** the full coordinator combined final run (pending);
Python 3.11; any adapter live smoke; any paid model call; any hosted transfer.

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

- No CLI, worker scheduling, or approval gate is built.
- The storage schema remains **10**; there is **no persistence of searches**.
- Metadata is an **untrusted observation**; there is no accepted context,
  eligibility, scientific identity, or corpus freeze.
- Open decisions remain: date-partitioned PubMed paging past the window;
  complementary-source ratification; durable query/candidate storage; citation
  expansion; coverage reporting; corpus freeze; project cost/work reporting.

## 7. Commits, changed files, and next actions

- **Base / starting HEAD:** `de179b4` (integrated T1/T2/T3).
- **This documentation-only branch** (`task/step05-handoff`) changes only docs —
  **no source, test, config, contract, schema, baseline, step-criteria, or policy
  edit**:
  - `docs/implementation/handoffs/05-discovery.md` (**new**, this file);
  - `docs/discovery.md` (**new**, the adapter API/limits reference);
  - `docs/implementation/README.md` — marks Step 05 **partial — metadata adapter
    foundation** (still **unchecked**) and links this handoff;
  - `README.md` — adds a concise partial Step 05 section with placeholder imports
    and links.
- **Next actions:** review and merge are the user's. The remaining Step 05 work
  (D1, D3, D4, AC1/AC2/AC5/AC6, complementary sources, durable candidate identity,
  citation expansion, coverage, and freeze) is **not started** and needs its own
  bounded session(s).
