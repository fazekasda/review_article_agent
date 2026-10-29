# Session-by-session implementation plan

## Steps

- [x] [00 — OpenCode integration and language spike](00-runtime-spike.md) — **complete**. All six acceptance criteria are evidenced on one real disposable OpenCode v2.2.0 service (real HTTP Basic auth, session create with pinned role/model, durable admission, SQLite persistence, and an owned-service restart). Criteria 1, 2, 3, and 5 are verified for **both** prototypes with only the model transport a deterministic local fixture; criterion 4 is met through the direct-OpenRouter fallback that `00-runtime-spike.md` line 19 explicitly allows (the native route is unexercised and per-question failures stay mock-only); criterion 6 is met as written — no change to ordinary coding sessions (ordinary endpoint/registration unchanged, child env/config contained) with global/ancestor inheritance risks documented and an ancestor-config root refused. The user accepted the runtime-language decision on 2026-10-27 (**Python core** with a supported, explicitly authenticated OpenCode V2 HTTP integration; TypeScript retained as reference). See the [step 00 handoff](handoffs/00-runtime-spike.md), [decision 001](../decisions/001-runtime-language.md), and the [step 01 kickoff](kickoffs/01-contracts.md).
- [x] [01 — Ratify scientific contracts and technical decisions](01-contracts.md) — **complete (decision/contract documentation step)**. On 2026-10-27 the user dispositioned every P1–P6 and operational/technical item in the recorded Step 01 interview, binding to the reviewed revision `ee7f7ea`; the dispositions are recorded in [decision 002](../decisions/002-step01-contracts-and-policies.md) and the [policy register](../contracts/03-policy-decisions.md). The versioned implementation contracts are [C01 (v1.4)](../contracts/01-identities.md), [C02 (v3.2)](../contracts/02-gates-and-runtime.md), and [C03 (v1)](../contracts/03-policy-decisions.md). The step's seven acceptance criteria and all seven named worked examples (valid/invalid/missing) are evidenced in the [handoff](handoffs/01-contracts.md). **Behavior is approved; the exact field/schema spelling remains a reviewable versioned representation, not a new human-ratification gate that blocks the step.** No production feature and no scientific validation is claimed.
- [x] [02 — CLI, project registry, artifacts, and backups](02-project-foundation.md) — **complete (local foundation)**. Integrated at `5efa4f2`; the path-independent project registry, immutable artifact revisions, non-authorizing local fixture approvals, and consistent private backup/verify/restore are implemented stdlib-only, with all seven acceptance criteria evidenced in the [handoff](handoffs/02-project-foundation.md). The CLI and storage interfaces are documented as [CLI](../cli.md)/[storage](../storage.md) contracts. It implements **no** agent execution, **no** OpenCode session, and **no** scientific approval or scientific feature: every revision is `scientific_state="proposal"`, all `fixture` approvals are non-authorizing (`scope="storage_fixture"`, `actor_authenticated=false`), and `export` is a **private local storage snapshot**, not a manuscript export (never publish). At the time of this handoff Step 03 was the next implementation step; Step 03 is now **complete** (see the [step 03 handoff](handoffs/03-durable-worker.md)).
- [x] [03 — Background worker, durable jobs, and recovery](03-durable-worker.md) — **complete**. Integrated at `2530955`; the stdlib-only, owner-authenticated detached worker, durable synthetic job/attempt/result/budget state, owner-only UDS control, explicit recovery/reconciliation, one execution-active project globally, and the real-process crash/concurrency matrix are implemented, with all seven acceptance criteria evidenced in the [handoff](handoffs/03-durable-worker.md). It implements **no** model, provider, network call, scientific result, or scientific approval: every task is a bounded **synthetic** numeric/waiting-human task, the external provider is a **synthetic fixture** (no egress), reserves are **virtual** (actual spend USD 0), and no exactly-once external execution is claimed. The worker/job surfaces are documented as [CLI](../cli.md) §9/§10 and the [worker runtime safety contract](../worker.md). Step 04 is the next implementation step and is now integrated (see the [Step 04 handoff](handoffs/04-agent-runtime.md)).
- [x] [04 — Restricted agent roles, model adapters, and approvals](04-agent-runtime.md) — **complete (restricted runtime)**. Integrated at `8626cee` and completed as a **restricted runtime validated against a real app-owned OpenCode v2.2.0 service and a fixture model**: an app-owned, version-pinned service and mappings; a pure fail-closed scientific role/proposal boundary; a thin bounded session `submit_session_turn`/`reconcile_session_turn` **library** path (not a new CLI/executor); the direct-Jev typed adapter with durable versioned records and an atomic commit; and app-owned exact named-gate human approvals through the `gate` CLI. Every original deliverable and acceptance criterion (AC1–AC7) is individually evidenced in the [handoff](handoffs/04-agent-runtime.md); the coordinator's final full-repository run at `c1f927b` passed (**2,105 tests, no skips**; contract checks 29; `tsc --noEmit` clean; pre-existing SQLite/subprocess `ResourceWarning`s disclosed; no CI workflows). It is **not** a working review writer: **no** scientific autonomous workflow, production paid generation, or hosted transfer is implemented or authorized (**only the model transport is a loopback fixture**; actual spend **USD 0**), and **no** scientific validation is claimed. Step 05 is the next implementation step.
- [x] [05 — Reproducible discovery and corpus freeze](05-discovery.md) — **complete (scoped discovery workflow)**. All four deliverables and all seven acceptance criteria are implemented and verified: a bounded stdlib-only public-metadata transport and PubMed/Europe PMC adapters (plus the Europe PMC citation family); a lossless versioned codec and immutable schema-11 observation repository; a typed context read-and-gate layer; an approved-plan/seed/ratified-metadata-import execution library; a conservative cumulative candidate ledger; bounded bidirectional citation expansion; a deterministic coverage/work/monetary report; an exact human-approved freeze/search-again record with an explicit caller per-candidate review schema; and a live-capable, separately-authorized direct lead-proposal runner. The `discovery` CLI group (including `report create|show` and `freeze propose|status|show|history|require`) submits/inspects these as detached background jobs, keeps the existing app-owned OpenCode production block unchanged, and records only non-authorizing proposals. The final full combined check passed at the exact source/test/docs head `f3d97fe` (**2,920 tests, no skips**; 29 contract checks; `tsc`/`compileall`/`diffcheck` clean), including **E0 12 + E1 11 + E1B 3 + E2 8 = 34 integration-acceptance cases**; see the [handoff](handoffs/05-discovery.md) and the [discovery API/limits](../discovery.md). This is the scoped **discovery workflow** only — not a review writer: no study identity, eligibility, extraction, topic/literature completeness, or review-writer capability is claimed. Live lead-**model** validation and Python 3.11 remain unrun; final PR delivery/check rollup and user review/merge are release actions, not a step criterion.
- [x] [06 — Full-text acquisition, imports, and reconciliation](06-acquisition.md) — **complete (scoped acquisition MVP), locally verified.** Full integration code+tests at `246df21` passed **3,079 tests in 412.6 s, OK, no skips** (retained 20 scripts / 79 Python / 51 Jev / 7 integration / 58 TS, 29 contract checks, `tsc`/`compileall`/`diffcheck` clean; non-fatal SQLite `ResourceWarning`s; no CI). The exact final docs-head result is recorded in the PR. Scientific parsing/screening/digestion are **Step 07+**. See the [handoff](handoffs/06-acquisition.md).
- [ ] [07 — Parsing, source assets, and reference structure](07-parsing.md) — **partial (scoped parsing MVP); not fully validated.** Integrated S7-CORE (stdlib XML-first structural core, locators), S7-STORE (durable report repository), and S7-CLI (personal `parsing` CLI) at `e39f91c`; XML/JATS/TEI supported. Not a full Step 07: no scientific correctness, PDF/OCR/full-text/visual extraction, human-adjudicated benchmark, or combined result is claimed — see the [handoff](handoffs/07-parsing.md).
- [ ] [08 — Pilot, Jev screening, and exclusion audits](08-screening.md)
- [ ] [09 — Grounded extraction, appraisal, and output audits](09-extraction.md)
- [ ] [10 — Synthesis, framework, outline, and argument record](10-synthesis.md)
- [ ] [11 — Section blueprints and evidence-grounded drafting](11-drafting.md)
- [ ] [12 — Bounded critique and original-source fact-checking](12-review.md)
- [ ] [13 — Citation-origin tracing](13-origin-tracing.md)
- [ ] [14 — Manuscript integration, author notes, and delivery](14-delivery.md)
- [ ] [15 — End-to-end evaluation, scale, and release](15-release.md)

## How to use this plan

Each numbered step is a separately assignable implementation unit. Prefer one step per session; a step may need several sessions if its acceptance criteria are not met. Do not call it complete merely because its code was written. The step documents specify dependencies, scope, deliverables, verification, acceptance, and handoff requirements.

The [scientific/product baseline](../specification.md) governs all steps. The stack was resolved at step 00: the runtime language is **Python core** (accepted 2026-10-27). The steps describe behavior, not speculative framework-specific code. No feature is implemented by writing these documents.

### Dependency order

```text
00 -> 01 -> 02 -> 03 -> 04 -> 05 -> 06 -> 07 -> 08 -> 09
                                                        |
                                                        v
                  15 <- 14 <- 13 <- 12 <- 11 <- 10 <------+
```

This deliberately serial baseline reduces integration risk. Independent work may be split later only after shared interfaces and ownership are explicit. Within-step local tests can use fixtures for later dependencies, but the handoff must disclose every stub. A fixture is not production completion of another step.

### Start-of-session protocol

1. Read this index, the assigned step, `docs/specification.md`, applicable repository instructions, and predecessor handoffs.
2. Inspect branch and working-tree changes; preserve other work. Agree on isolation before changing a busy checkout.
3. Check that dependencies actually passed. Read decisions rather than assuming the previous session's language, API, or scientific policy.
4. State the bounded implementation goal and identify required user decisions, credentials, optional paid tests, and tool permissions.
5. Implement only the assigned scope. Record specification changes as explicit decisions; do not use code to settle an open scientific question silently.

### Completion and handoff protocol

Every implementation session writes `docs/implementation/handoffs/NN-<topic>.md` (created when that session runs) containing:

- Scope and baseline/decision revisions used.
- Files and public interfaces changed; migrations and compatibility implications.
- Commands/tests run, environment/model versions, actual results and unrun checks.
- Evidence for each acceptance criterion, including failure/restart tests where required.
- Stubs, known limitations, open decisions, costs incurred, and safe cleanup instructions.
- Exact next actions and relevant commit IDs if commits were requested/performed.

Tick a step here only after all its acceptance criteria pass, or explicitly mark it blocked/partial without ticking. Never claim live integration passed based only on mocks. Redact credentials and licensed source text from public test logs. Do not commit or push automatically merely because the plan mentions a handoff.

### Global definition of done

- Changes follow the approved decision records; tests cover success, invalid inputs, interrupted operations, and stale approvals.
- Authoritative scientific artifacts are validated and revision-bound; agent chat history is not the database.
- Human gates cannot be bypassed by model instructions, tools, cached results, or a generic OpenCode permission grant.
- CLI closure does not cancel an authorized background job; explicit pause/cancel semantics are tested.
- Exactly one project may execute at once. Saved projects remain isolated. Per-project approvals and budgets are never shared.
- Retries and reconciliation prevent duplicate accepted local results without claiming exactly-once external execution or billing.
- No tests restart/delete the user's ordinary OpenCode service or data. Fault tests use disposable app-owned instances.
- No automatic unofficial acquisition. No scientific finalization before later required verification modules are integrated.
- Pending policy decisions, failures, and partial results remain visible, not converted into success.

### Test levels

1. **Unit/contract:** deterministic fixtures, schema validation, adapters, invariants.
2. **Integration:** real local store, filesystem, worker processes, and isolated harness; mocked model/provider responses where appropriate.
3. **Live smoke:** explicitly authorized small synthetic model calls and public API queries; record actual versions/costs.
4. **Scientific acceptance:** human-adjudicated authorized papers and sampled outputs; does not claim held-out validation.
5. **Scale/fault:** synthetic metadata and authorized small assets, process interruption, event gaps, duplicate submissions, resource limits.

### Proposed CLI concepts

Exact spelling is decided in step 02. Required capabilities include project create/list/select/status; run-until-gate; attach/watch; pause/resume; revision-specific approve/reject; import/disposition; review findings; export/backup/restore. A terminal disconnect is distinct from a pause request. A safe project switch must reconcile or drain existing work before another project executes.

### Research boundaries

OpenCode V2 client/API documentation supports durable prompt admission and persisted messages; events are live-only. In-flight restart behavior and exact Jev routing are empirical spike questions, not presumed guarantees. Embedded SDK in a daemon is viable but not the selected initial direction. LangGraph, DBOS, and Temporal remain alternatives, not additional mandatory layers.

Use [OpenCode V2 documentation](https://opencode.ai/v2/docs/) and the tested server's OpenAPI contract. Do not copy V1 SDK examples. Use SQLite's snapshot/backup mechanism, not an arbitrary copy of a live WAL database.
