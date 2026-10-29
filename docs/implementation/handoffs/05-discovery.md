# Handoff — step 05: reproducible discovery and corpus freeze

**Status: implementation INTEGRATED; FINAL ACCEPTANCE PENDING — Step 05 stays
UNCHECKED in [`docs/implementation/README.md`](../README.md).** Every deliverable
D1–D4 and acceptance criterion AC1–AC7 below is now realised by integrated
source and tests on this branch's base `ec9c32d`. What remains is **verification
and release work only** — the E2 full-CLI-chain case, a final frozen-HEAD
full-suite run, and the PR/merge — not any deferred feature. The E1B adversarial
cases are now **integrated** (§5.2).
Nothing here is a scientific claim: no study identity, eligibility, primary
support, evidence extraction, coverage, completeness, review, or review-writer
capability is asserted.

## 1. Scope, base revisions, and authority

- **Step document (authoritative, unedited):**
  [`05-discovery.md`](../05-discovery.md) — deliverables D1–D4 and AC1–AC7, not
  weakened or marked met by any doc.
- **Baseline:** [`docs/specification.md`](../../specification.md) — workflow steps
  2–5 and the gates/scientific invariants (not weakened).
- **Accepted direction/policies:** [decision 002](../../decisions/002-step01-contracts-and-policies.md)
  and the [policy register](../../contracts/03-policy-decisions.md) (`POL-OPS-COVERAGE`,
  `POL-OPS-TRACE`, `POL-OPS-SECONDARY`); the [direct-lead exception (decision 003)](../../decisions/003-step05-discovery-and-direct-lead.md).
- **Contracts:** [C01](../../contracts/01-identities.md), [C02](../../contracts/02-gates-and-runtime.md), [C03](../../contracts/03-policy-decisions.md).
- **This branch:** `task/step05-completion-handoff`; starting HEAD **`e15b5b4`**
  (`e15b5b417a4d41342e1f24ab52fd4f3beb92b1fd`). This task changes
  **documentation only** (see §6).

## 2. Historical foundation (preserved anchors, superseded status)

Two earlier partial slices are now **subsumed** by the integrated core; their
measured evidence is kept only as history, not as the current claim.

- **Adapter foundation** (base `de179b4`; merged `6e20946`): the bounded
  stdlib-only transport, PubMed and Europe PMC metadata adapters, and value
  contracts. Targeted run **177 discovery tests OK**; the coordinator's full
  combined run then reported **2,282 tests** (log
  `/tmp/opencode/step05-combined-all-checks.log`).
- **Durable inert observation slice** (`25827dd`; codec T1 `c8e52f0`, repository
  T2 `9f1ff31`): lossless codec plus additive schema-11 `discovery_search_run`
  observation repository. Targeted run **252 discovery tests OK**; full combined
  run **2,357 tests** (log `/tmp/opencode/search-records-combined.log`).

Those counts are historical. The frozen checkpoint below (§5.1) verified only
the code present at its own head `6ec58ea`; it **predates** the E1/E1B acceptance
suites, the CLI1A report/freeze CLI, and later fixes, so those additions have
their **own separate selective-run evidence** (§5.2) and are **not** covered by
that checkpoint.

## 3. Deliverables → integrated source and named tests

| Deliverable | Integrated implementation | Named tests / gates |
| --- | --- | --- |
| **D1** — brief/interview artifacts, seed search + individual approvals, framing challenge, provisional scope map, rubric/search-plan approval | [`discovery/context.py`](../../../src/review_article_agent/discovery/context.py) (typed envelopes, `require_approved_context`, `seed_context`), [`discovery/execution.py`](../../../src/review_article_agent/discovery/execution.py) (`seed_search`, always provisional) | [`test_discovery_context.py`](../../../tests/test_discovery_context.py)::`StaticValidationTest`/`ReadContextTest`/`SeedContextTest`/`PlanApprovalTest`; [`test_step05_core_acceptance.py`](../../../tests/test_step05_core_acceptance.py)::`SeedApprovalGateTest`/`PlanAmendmentTest`; [`test_step05_acceptance.py`](../../../tests/test_step05_acceptance.py)::`InjectionCannotExpandAuthorityTest`. Gates: seed `artifact_revision`/`discovery.seed.<artifact_id>`; plan `protocol`/`discovery.plan`. |
| **D2** — PubMed and Europe PMC adapters plus complementary sources ratified in the project plan | [`discovery/pubmed.py`](../../../src/review_article_agent/discovery/pubmed.py), [`discovery/europepmc.py`](../../../src/review_article_agent/discovery/europepmc.py), [`discovery/citations.py`](../../../src/review_article_agent/discovery/citations.py); ratified complementary sources via `DiscoveryExecutor.import_metadata` | [`test_discovery_pubmed.py`](../../../tests/test_discovery_pubmed.py), [`test_discovery_europepmc.py`](../../../tests/test_discovery_europepmc.py), [`test_discovery_citations.py`](../../../tests/test_discovery_citations.py); `test_step05_core_acceptance.py`::`MetadataImportTest` |
| **D3** — query runs, identity reconciliation, citation expansion, coverage report, explicit freeze/search-again decision | [`discovery/execution.py`](../../../src/review_article_agent/discovery/execution.py) (`execute_query`), [`discovery/serialization.py`](../../../src/review_article_agent/discovery/serialization.py), [`storage/discovery.py`](../../../src/review_article_agent/storage/discovery.py), [`discovery/reconciliation.py`](../../../src/review_article_agent/discovery/reconciliation.py), [`discovery/candidates.py`](../../../src/review_article_agent/discovery/candidates.py), [`discovery/expansion.py`](../../../src/review_article_agent/discovery/expansion.py), [`discovery/reporting.py`](../../../src/review_article_agent/discovery/reporting.py), [`discovery/freeze.py`](../../../src/review_article_agent/discovery/freeze.py); `discovery report\|freeze` CLI | [`test_discovery_execution.py`](../../../tests/test_discovery_execution.py), [`test_discovery_serialization.py`](../../../tests/test_discovery_serialization.py), [`test_discovery_store.py`](../../../tests/test_discovery_store.py), [`test_discovery_reconciliation.py`](../../../tests/test_discovery_reconciliation.py), [`test_discovery_candidates.py`](../../../tests/test_discovery_candidates.py), [`test_discovery_expansion.py`](../../../tests/test_discovery_expansion.py), [`test_discovery_reporting.py`](../../../tests/test_discovery_reporting.py), [`test_discovery_freeze.py`](../../../tests/test_discovery_freeze.py), [`test_cli_discovery_reports.py`](../../../tests/test_cli_discovery_reports.py); E0/E1 acceptance. Gate: `artifact_revision`/`discovery.freeze`. |
| **D4** — bounded network retries/rate handling and project cost/work reporting | [`discovery/transport.py`](../../../src/review_article_agent/discovery/transport.py) (bounded retries, allowlist, size cap, per-instance pacing), [`discovery/reporting.py`](../../../src/review_article_agent/discovery/reporting.py) (cumulative work + known/estimated/unknown monetary view) | [`test_discovery_transport.py`](../../../tests/test_discovery_transport.py); `test_discovery_reporting.py`::`MonetaryTests`/`SourceReconstructionTests`/`MetadataImportProvenanceTests`; `test_cli_discovery_reports.py` |

The **detached job surface** that drives D3 is [`worker/discovery.py`](../../../src/review_article_agent/worker/discovery.py)
(five modes) over the execution library, with [`test_discovery_worker.py`](../../../tests/test_discovery_worker.py),
[`test_discovery_job_routes.py`](../../../tests/test_discovery_job_routes.py),
[`test_cli_discovery_jobs.py`](../../../tests/test_cli_discovery_jobs.py).

## 4. Acceptance criteria — integrated, final acceptance pending

Each criterion is realised in integrated source/tests; the step-level
**acceptance decision** (frozen-HEAD full run + E2 + PR) is what remains.

| AC | Status | Integrated evidence |
| --- | --- | --- |
| **1** Seed abstracts stay provisional; an unapproved seed cannot become accepted context | **Integrated** | `seed_context`/`require_approved_context` require the exact per-seed named gate; a seed's `abstract_context` stays `provisional` with or without approval (`test_discovery_context.py`::`SeedContextTest`; `test_step05_core_acceptance.py`::`SeedApprovalGateTest`). |
| **2** Broad searches cannot run against an unapproved scope/query plan; a substantive change produces an approval request | **Integrated** | `execute_query` is pinned to the exact approved plan and fails closed (zero I/O) otherwise; plan amendments invalidate stale gates (`ExecuteQueryGateTest`; E0 `PlanAmendmentTest`; E1 `StaleApprovalTest`). |
| **3** Reconstruct exact queries, timestamps, contributing sources, failures, counts, and dedup decisions from records | **Integrated** | Lossless codec + schema-11 observation store + reconciliation + report `SourceReconstructionTests` reconstruct exact observations and explicit dedup/conflict decisions from durable rows. |
| **4** Fixtures cover pagination, overlap, missing IDs, partial failures, incremental reruns without duplicate candidates | **Integrated** | Adapter fixture suites (pagination/overlap/missing/partial) plus the cumulative ledger (`CumulativeDedupTests`; E0 `CumulativeCorpusTest`/`CorpusConflictTest`). |
| **5** Known-relevant set + forward/backward expansion expose explicit coverage gaps; reporting never claims absolute completeness | **Integrated** | `test_discovery_reporting.py`::`KnownRelevantTests`/`CitationTests`; `test_discovery_expansion.py`; E1 `ExplicitGapsTest`/`NoForcedCompletenessTest`. |
| **6** Human approval freezes a specific corpus revision; later imports/searches do not mutate it silently | **Integrated** | `FreezeAuthorityTests`/`CorpusFreezeImmutabilityTests`; E1 `IntegratedFreezeAuthorityTest`/`LaterWorkImmutabilityTest`/`SearchAgainTest`. |
| **7** Live smoke small/documented/separate; no arbitrary cap truncation hidden as complete | **Integrated** | Adapters return explicit `page_limit`/`failure`, never `complete`, at any budget/window boundary (`test_discovery_pubmed.py`::`HardLimitTests`; `test_discovery_europepmc.py`::`BoundsTests`); the live observations/smoke are separately documented (§5.3) and are distinct from the deterministic fixtures. |

## 5. Verification

### 5.1 Frozen checkpoint (last full combined run)

The last **full frozen combined** run is at `VERIFIED_END_SHA=6ec58ea`
(`6ec58ea2877ac9cc9edb86c7b60e98b2601c7ad2`), a **frozen checkpoint, not the
current HEAD**, log `/tmp/opencode/step05-pre-final-checks.log`: Python
**3.14.4**, **2,557 core tests in 295.874 s**, **20 scripts**, **79 Python
retained spikes**, **51 live-Jev mock**, **7 integration mock**, **58
TypeScript** — **2,772 tests total, no skips**; **29 contract-doc checks**;
`tsc --noEmit`, `compileall -q src tests`, `git diff --check` all clean.
Pre-existing SQLite/subprocess/unclosed-file `ResourceWarning`s are disclosed,
not failures. **Python 3.11 is declared (`requires-python >=3.11`) but
untested.**

### 5.2 Commits after the checkpoint and the current base

- **E0** core acceptance ([`test_step05_core_acceptance.py`](../../../tests/test_step05_core_acceptance.py)): `a6d21fb` (in the frozen run above).
- **E1** integrated final acceptance: integrated `a7d1cd9` (library half) and
  `752d359` (real-process half); source-equivalent `3eccfeb`/`f2a3ad2`. Adds
  [`test_step05_acceptance.py`](../../../tests/test_step05_acceptance.py) — **9
  library scenarios + 2 real owned-process scenarios** (real detached worker:
  detach/reconnect of an authorized metadata-import job with zero provider
  attempts; restart proves a standing accepted result on the same fence and a
  durable POST counter stays at exactly one — no blind re-POST).
- **DOC1** (`cb6e964`), **DOC2** (`3131031`): discovery/CLI/worker doc
  reconciliation.
- **CLI1A** (`c325b22`): discovery background-job CLI. **CLI1B** (`9085241`,
  fix `e15b5b4`): `discovery report`/`freeze` readbacks (§6).
- **E1B** adversarial J1 admission/interruption seams: **integrated at `630545c`**
  (source-equivalent `8939db5`). Adds `tests/test_step05_job_adversarial.py` (not
  present in this checkout until `630545c` is integrated) with three cases:
  `ExpansionInterruptionAdversarialTest` (stale corpus /
  drained token stops further I/O mid-expansion),
  `ReservedUnsentLeadBoundaryTest` (drained before POST never sends and retains
  its reserve), and `RealProcessKillDuringPostTest` (kill during the first fake
  POST before response never re-posts). The combined **E0 + E1 + E1B** suite
  reported **26 tests passed** at `630545c`.
- **The CLI1B integration at `e15b5b4`** has only a **selective 84-test** run
  recorded by the coordinator, **not** a full-HEAD suite. A final frozen-HEAD
  full-suite run is **pending** (§8), and E2 (full CLI chain) is still running.

### 5.3 Authorized live observations (separate from deterministic results)

A tiny, separately authorized public-metadata smoke was run by the coordinator:
**3 transport-level `webfetch` contract observations** (MED `15019270` refs
pages 1/2 each `size=1` `hitCount=27`; citations page 1 `size=1` `hitCount=78`),
**3 real search-adapter GET attempts** (PubMed `31452104[uid]` two GETs; Europe
PMC `EXT_ID:31452104 AND SRC:MED` one GET — each 1/1 complete), and **2
citation-adapter GETs** (MED `15019270` references 1/27 and citations 1/78, then
stopped at `page_limit`, `complete=False`). The search- and citation-adapter
attempts were **real production adapters** driven through the real
`DiscoveryTransport` (`timeout=15`, `max_attempts=1`; search `page_size=1`,
`max_pages=1`; citation `page_size=1`, `max_pages=1`) and **passed**, separate
from the 3 `webfetch` contract observations and the deterministic fixture
suites. These scope the tested **transport/adapter mechanisms only** — never
literature/topic coverage, which remains **partial and unvalidated**. **No
further public request is needed or authorized; no paid application-model call,
upload, or live lead-model validation was made** in this run (the internal
worker's own deterministic usage is not an application-model smoke).

### 5.4 Not run / not claimed

Python 3.11; any paid app/model call, upload, or live lead-model validation; the
final frozen-HEAD full-suite run; the E2 full-CLI-chain case (§8). **No
CI is configured**, so no CI-green claim is made and the PR checks must be
inspected at PR time.

## 6. Files, interfaces, migrations, compatibility

- **Public interfaces** (library, unchanged by this doc task): the modules in
  §3 plus the `discovery` CLI group
  ([`cli/discovery.py`](../../../src/review_article_agent/cli/discovery.py),
  [`cli/discovery_jobs.py`](../../../src/review_article_agent/cli/discovery_jobs.py),
  [`cli/discovery_reports.py`](../../../src/review_article_agent/cli/discovery_reports.py)).
- **Migrations:** **none beyond the existing schema 11.** Step 05 core stores its
  typed context/candidate/coverage/freeze records as immutable **artifact
  revisions** (existing schema-3 tables) and its jobs in the existing schema-4
  job tables; the only Step 05 table is the schema-11 `discovery_search_run`
  observation (additive; earlier migrations unchanged). See
  [`docs/storage.md`](../../storage.md).
- **Compatibility / backup rights.** Schema 11 is within the supported snapshot
  range; manifest `format_version` stays 1. `verify_backup`/`restore_backup`
  share the read-only schema-11 integrity sweep; a legacy schema-10 snapshot is
  still restorable (upgraded by ordinary store open). Older software refuses a
  newer schema — back up before upgrading, no downgrade path. Restore proves
  storage integrity, **not** scientific validity or remote quiescence.
- **CLI1B readbacks (integrated at `e15b5b4`).** `discovery report
  create|show` and `discovery freeze propose|status|show|history|require`:
  - `report create` needs exact `--corpus-artifact-id/--corpus-revision-id`,
    `--plan-artifact-id/--plan-revision-id`, `--revision-id`, `--request-id`,
    `--rights-file`, optional repeatable `--expansion-job-id` (resolved only from
    a real completed expansion report), optional `--known-relevant-file` and
    `--limitations-file`; it records one immutable coverage revision and claims
    no completeness.
  - `freeze propose` needs exact corpus/coverage refs, `--decision
    freeze|search_again`, `--entry-reviews-file`, `--rationale`, `--request-id`,
    `--revision-id`, `--rights-file`, optional `--accept-coverage-limitations
    EXPLANATION`; returns the exact named-gate `approval_request` and `gate
    prepare`/`gate confirm` guidance, records no approval.
  - `freeze require` consults `require_frozen` on **both** paths. The default is a
    **safe summary** (exact corpus ref, `included_count`, decision, active
    approval) and **only `--content`** opts in to the private validated document +
    exact roster. `--require-active` is a redundant active-check alias that keeps
    the safe summary; it **never** returns the roster.
- **Caller-supplied reviews.** Entry reviews are an **explicit caller
  per-candidate review list** following the schema in
  [`discovery/freeze.py`](../../../src/review_article_agent/discovery/freeze.py):
  one review per pinned candidate — `candidate_id`, `checked_at`, `provenance`,
  the four distinguished integrity statuses (`retraction`,
  `expression_of_concern`, `correction`, `non_replication`), a `disposition`, and
  a non-empty `rationale`. An `include` requires a resolved (non-conflict)
  candidate with all four statuses **known** — `unknown` is **not** clean;
  unresolved/conflict candidates need `exclude`/`defer`/`no_scientific_use`. A
  freeze over any explicit coverage gap is refused unless the exact decision
  passes a **literal `--accept-coverage-limitations` explanation** (a string, not
  a `--yes` boolean).
- **DOC2 correction.** `metadata_import` performs **zero HTTP** (a local,
  caller-supplied observation); only `query`/`seed`/`expand` may issue real
  public-metadata GETs. `docs/worker.md` is corrected accordingly.
- **Hosted lead route.** The direct OpenRouter lead proposal is **live-capable
  and implemented** but **unrun/not authorized** in this run; it is a narrow,
  separate exception and the **existing app-owned OpenCode production block is
  unchanged** ([decision 003](../../decisions/003-step05-discovery-and-direct-lead.md)).

## 7. Stubs, limits, and open decisions

- **PubMed window:** `PUBMED_WINDOW_LIMIT = 9999` is a conservative explicit cap
  (a retrieval needing to page past it is `page_limit`, never complete);
  date-partitioned paging past the window is deferred.
- **Rate spacing is per-instance and the transport is not thread-safe**; the
  socket timeout is **inactivity, not a wall-clock deadline**.
- **Source coverage is partial.** Query execution is PubMed + Europe PMC;
  additional sources are provenance-preserving `metadata_import` observations
  explicitly ratified in the plan, never automatic. **No abstract auto-fetch**:
  PubMed parses bibliographic metadata only, Europe PMC uses `resultType=lite`.
- **Freeze protects the approved roster only.** A later search records normally,
  a **new** corpus/coverage artifact reconciles freely, and the **old frozen
  roster is unchanged**; there is **no automatic "latest"** following, and a
  superseded freeze is preserved as honest history. A stale freeze is never
  reported active.
- **Costs/terms are human-reviewed.** Model/provider terms, context pricing, and
  hosted-science approval are **not** machine-decided; an unknown reserve/cost
  stays `unknown` (never `0`) and is retained under the shared caps.
- **No scientific claim:** no study identity, eligibility, primary-support,
  evidence-extraction, coverage-completeness, review, or review-writer promise.
- **Open decisions:** complementary-source ratification per project; scientific
  candidate identity beyond exact-identifier grouping; date-partitioned paging.

## 8. Next actions and commits (scoped; not done here)

1. **E2** — full CLI-chain acceptance case (in progress; do not claim done).
2. **Final frozen-HEAD full-suite run** on the frozen base, and **PR checks**
   inspection (no CI is configured).
3. **User review and merge**; the coordinator owns the PR and the README
   checkbox (which **stays unchecked until 1–2 pass**).
4. **Step 06** (full-text acquisition, imports, reconciliation) is **not
   implemented**.

The **E1B** adversarial cases are **integrated** (`630545c`, source `8939db5`;
three cases in `tests/test_step05_job_adversarial.py`; combined E0+E1+E1B **26
tests passed**) and are no longer pending; **E2** and the final frozen-HEAD run
remain.

Evidence log paths: frozen checkpoint `/tmp/opencode/step05-pre-final-checks.log`;
historical `/tmp/opencode/search-records-combined.log`,
`/tmp/opencode/step05-combined-all-checks.log`. Key commits: this docs base
`ec9c32d`; CLI1B `9085241`/`e15b5b4`; E1B `630545c` (source `8939db5`); E1
`a7d1cd9`/`752d359` (source `3eccfeb`/`f2a3ad2`);
E0 `a6d21fb`; frozen checkpoint `6ec58ea`. No secrets, licensed full text, or
raw provider payloads are included here.
