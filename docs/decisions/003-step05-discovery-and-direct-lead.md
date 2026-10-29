# 003 — Step 05 discovery scope and the direct lead-proposal exception

## Status

**Accepted by the user — 2026-10-28.** The approving authority is the **user**.
The approving evidence is this user session `ses_e7b3b9218ffemc0KnkBRFIjSxv`
(2026-10-28), against the source baseline merged
`fb8260d0f48fa73bde1e0bc57ec119fbe24dcabb` (PR #8,
`feat/step05-search-records`). The user's opening instruction was **"Implement
everithing remaind from step 05 at once"**; they then resolved four scope
questions and one architecture question, reproduced verbatim below. This record
captures the *architecture/scope direction* the user chose for Step 05 discovery
and for the direct lead route. It is a **documentation-only** decision record:
this task changes no product source, test, schema, contract, or policy, and it
describes code already visible in the base revision.

**Approval anchor — what was and was not reviewed.** The commit `f860429` is the
**coordinator-inspected implementation snapshot** the direction was discussed
against; it was **not** a user code review. The user approved the **behavioral and
architecture direction** described here **verbally in this session**, not a
line-by-line review of the code at `f860429`. This approval is therefore **not**
approval of paid execution and **not** approval of project science: it does not
authorize a paid model call, a real project search, or a hosted transfer, and it
makes no scientific-validation claim.

**What this approval is and is not.** It approves the described **technical
direction and scope boundaries** for Step 05. It is **not** a scientific
validation, **not** a claim that Step 05 (or any scientific feature) is complete,
**not** a promise of total review cost, **not** authorization of any **paid**
model call, and **not** authorization to run a **real project search** or any
**hosted transfer**. Approval of the code architecture is **not** approval to
execute a real project search or a hosted transfer. The user retains PR
review/merge; Step 05 remains **in progress** and unchecked in
[`docs/implementation/README.md`](../implementation/README.md).

**Current status amendment (2026-10-29).** The 2026-10-28 statement above that
Step 05 was *in progress and unchecked* is **preserved unchanged** as it stood at
creation. As of 2026-10-29 the **scoped Step 05 discovery workflow is complete and
verified**: Step 05 is now ticked `[x]` in
[`docs/implementation/README.md`](../implementation/README.md), and the final full
combined check passed at head `f3d97fe` (**2,920 tests, no skips**; 29 contract
checks) — see the
[Step 05 handoff](../implementation/handoffs/05-discovery.md). This amendment adds
**no** decision content: it authorizes **no** paid call, **no** real project
search, and **no** hosted transfer, and makes **no** scientific claim (no study
identity, eligibility, extraction, topic completeness, or review-writer
capability).

## Context and constraints

- **Step authority:** [`docs/implementation/05-discovery.md`](../implementation/05-discovery.md)
  (deliverables D1–D4, acceptance criteria AC1–AC7). The step document is **not**
  edited and its criteria are **not** weakened or marked met by this record.
- **Baseline (authority, not weakened):**
  [`docs/specification.md`](../specification.md) — "Product and runtime" line 14
  (an **app-owned OpenCode V2 service**, separate from ordinary coding sessions),
  the confirmed scientific workflow steps 1–5, and the "Gates and scientific
  invariants" (lines 52–63). This record amends the product/runtime execution
  *direction only as stated below*; the confirmed invariants stand.
- **Already-accepted decisions (not reopened):**
  [`decision 001`](001-runtime-language.md) (Python core with a supported,
  explicitly authenticated OpenCode V2 HTTP integration; the direct-OpenRouter Jev
  fallback is the retained, separately-accounted route) and
  [`decision 002`](002-step01-contracts-and-policies.md) (P1–P6, operational
  policies, rights behavior, and the accepted technical direction).
- **Contracts:** [`C02`](../contracts/02-gates-and-runtime.md) §5.6 (cost/
  concurrency caps; reserve before dispatch; unknown reserve retained) and §6.3
  (exact-decimal `direct_call` cost, accounted separately from OpenCode session
  statistics); [`C03`](../contracts/03-policy-decisions.md) §5.2 (rights
  eligibility is **not** final authorization; a hosted transfer needs an
  out-of-band, route/purpose/revision-scoped authorization) and §5.5
  (provider/journal term verification is operation-scoped and gates hosted use).
- **Implementation snapshot inspected:** `f860429` (branch
  `task/step05-approved-decisions`) — a **coordinator-inspected** snapshot, not a
  user code review (see the approval anchor above).
- **Implementation actually present at the base (no new claim is made about any
  of it here):** the direct, single-shot, tool-free OpenRouter lead adapter
  [`runtime/lead.py`](../../src/review_article_agent/runtime/lead.py); the
  revision-scoped hosted lead runner
  [`discovery/lead_runner.py`](../../src/review_article_agent/discovery/lead_runner.py);
  the typed discovery-context read-and-gate layer
  [`discovery/context.py`](../../src/review_article_agent/discovery/context.py);
  the approved-plan exact-search and metadata-import execution library
  [`discovery/execution.py`](../../src/review_article_agent/discovery/execution.py);
  the durable cumulative publication-candidate ledger
  [`discovery/candidates.py`](../../src/review_article_agent/discovery/candidates.py);
  and the bounded bidirectional citation traversal
  [`discovery/expansion.py`](../../src/review_article_agent/discovery/expansion.py),
  over the existing PubMed/Europe PMC adapters and observation store. The
  fail-closed production runner gate
  [`runtime/execution_boundary.py`](../../src/review_article_agent/runtime/execution_boundary.py)
  is **unchanged** (see D-5).

## Options and evidence

The user chose the direction below after being shown a recommendation plus
concrete alternatives for each question. The scope decisions (D-1…D-4) select a
**conservative, fail-closed, provenance-preserving** discovery direction; the
architecture question (D-5) selects a **narrow, bounded exception** rather than a
broad relaxation.

For the architecture question the alternatives considered were:

- **Keep fixture-only / block real generation.** Safest, but it leaves the
  confirmed workflow steps 1, 3 (lead interview/framing) with **no** live-capable
  route, so the discovery stage cannot be exercised beyond fixtures.
- **OpenCode-only until a verified all-request bound exists.** Preserves the
  original OpenCode-only generation direction, but the app-owned OpenCode
  production route is blocked precisely because a defensible **all-request-bound**
  cost proof is not yet implemented, so this would stall the lead step
  indefinitely for no safety gain over a separately-bounded direct route.
- **Weaken `execution_boundary.py`.** Rejected: that gate blocks the live
  `opencode_session` and `direct_call` routes because a virtual positive
  `reserve_usd` is **not** a defensible bound and scoped hosted-use/privacy/
  provider-term authorization is not implemented. Removing or loosening the gate
  would erase a real safety invariant while pretending a reserve were proof.
- **Approve direct lead exception (chosen).** Add a **separate**, narrow direct
  route with its own exact bounds, leaving the OpenCode production block intact.

Documentation claim vs observed behavior: this record documents the direction and
the boundaries; it makes **no** live/paid result claim. A **tiny, separately
authorized** public-metadata smoke was run by the coordinator: **3 transport-level
webfetch contract observations**, **3 real search-adapter GET attempts** (PubMed
**2** + Europe PMC **1**), and **2 citation-adapter GETs** — no paid app/model
call, no upload, and no live lead validation. These counts scope only the tested
transport/adapter mechanisms in this repository; they are **not** a claim of topic
coverage or scientific validation. Live model validation of the lead route remains
**unrun**.

## Decision

**Accepted by the user, 2026-10-28, against source baseline `fb8260d` (direction
discussed against the coordinator-inspected implementation snapshot `f860429`,
which was not a user code review).**

**Verbatim user selections (2026-10-28):**

| Question | Verbatim answer | Record |
| --- | --- | --- |
| Candidate matching | `"Conservative IDs (Recommended)"` | D-1 |
| Sources | `"Two APIs plus imports (Recommended)"` | D-2 |
| Lead route | `"Implement live-capable route"` | D-3 |
| Expansion/report/smoke | `"Yes, including public smoke"` | D-4 |
| Architecture (direct lead) | `"Approve direct lead exception"` | D-5 |

- **D-1 — Candidate matching (Conservative IDs).** Publication candidates are
  **reused only on an exact shared PMID/PMCID/DOI or a provider-native
  identifier**; **every occurrence is retained**; identifier **conflicts and
  missing IDs are given an explicit disposition**; there is **never** a title
  merge; and a candidate grouping is **not** a study-independence claim. (This
  matches the implemented cumulative-candidate ledger, W2.)
- **D-2 — Sources (Two APIs plus imports).** Query execution uses **PubMed and
  Europe PMC live adapters** plus **provenance-preserving metadata imports** from
  other sources that are **explicitly ratified in the project plan**
  (`metadata_import`); any **unsupported automated provider fails closed**. (No
  other API is contacted; the imported set is recorded as an honest metadata-only
  local observation.)
- **D-3 — Lead route (live-capable).** Implement an **explicit, configured,
  bounded** lead-model route with **rights/provider-term authorization** and
  **deterministic tests**. **No paid call** is made without **separate**
  approval; live model validation is **disclosed as unrun**. (This is the direct
  lead adapter/runner of D-5.)
- **D-4 — Expansion / reporting / smoke.** Require **approved, project-specific**
  discovery hops/sources/requests; adopt **no** scientific stopping or
  completeness threshold; **report work and known/unknown costs**; permit a few
  **documented tiny public known-ID requests**, and **no** paid calls or uploads.
  (This is the bounded citation traversal plus honest coverage/cost reporting
  direction.)
- **D-5 — Architecture exception (direct lead), and what it amends.** Accept a
  **narrow, direct** OpenRouter **tool-free chat** route for **lead
  interview/framing proposals**, alongside the existing direct-Jev typed-decision
  exception: **one bounded request per durable job**, **exact revision-bound,
  scoped hosted-use approval**, and a **defensible reserve computed before send**
  (an unknown reserve **retained**, never zeroed). The **existing OpenCode
  production block is unchanged.**
  - **What it amends:** the original **OpenCode-only generation direction** only,
    and only for this bounded direct-lead route.
  - **What it does not change:** this is **not** a global migration away from the
    **app-owned OpenCode service**, the scientific **roles**, or **ordinary-session
    isolation**; it selects **no blanket generation default**; and it leaves every
    other confirmed invariant and gate in force.
  - **Explicit project/model/provider/evidence/rights setup is still required**
    before any dispatch, and the already-accepted cost/concurrency defaults are
    **unchanged**: cumulative **USD 25 per project**, cumulative **USD 10 per
    stage**, and **at most 2 simultaneous** external-model calls (a stricter
    serial executor is acceptable).
  - The base implements this as: one `POST` to the single documented endpoint
    with the reserve derived from the **full configured model context capacity**
    (prompt) plus `max_output_tokens` (completion) at the configured unit prices
    — **never** from a token-length estimate; provider routing is **pinned to the
    single upstream slug with fallbacks disabled and parameters required**; **no
    tools**; and the accepted **model-metadata** record retained durably (so a completed
    lead output is non-authoritative data, and cost is always read from the
    direct-dispatch ledger, never copied from a response).

**Why a separate direct exception instead of weakening
`execution_boundary.py`.** The production runner gate is a fail-closed,
safety-only bound; it blocks live routes because no defensible all-request cost
bound and no scoped hosted-use/privacy/provider-term authorization are implemented.
Weakening it would remove a real invariant on the strength of a virtual reserve
that is not proof. Instead the direct lead route carries its **own** exact bounds
(context-capacity + max-output at unit price), pins the upstream with no fallback
and no tools, and retains durable model metadata — so the OpenCode production
block stays exactly as it is and the exception is narrow and auditable.

**Source/pricing/provider/legal truth vs machine shape validation.** The runner
only machine-validates the **shape, presence, boundedness, and freshness** of the
hosted-use evidence references (`provider_terms`, `privacy_review`,
`pricing_evidence`, each `{ref, verified_at}`), the exact revision-bound batch,
and the exact named gate. It makes **no** legal, provider-term, pricing, or
scientific determination: the **truth** of those references, and the rights and
provider/journal-term judgments, remain **human-reviewed**.

**Explicitly not decided / not authorized here.** No paid call; no real project
search; no hosted transfer; no blanket generation default; no change to the
OpenCode production block, roles, or ordinary-session isolation; no change to the
cost/concurrency caps; no scientific feature completion; no independent scientific
validation. Exact field spellings and any later production integration remain
separately reviewable.

## Consequences and verification

- **Unblocked direction.** Step 05 discovery (D-1…D-4) and the direct lead route
  (D-5) now have a recorded user-approved direction grounded in the base
  implementation; Step 05 itself **at creation** remained **partial/in-progress** (no criterion is
  marked met by this record).
- **Historical record preserved.** ADR [`001`](001-runtime-language.md) and
  [`002`](002-step01-contracts-and-policies.md) are **unchanged**; this record
  amends the OpenCode-only generation direction **only** through the named scoped
  exception and leaves the app-owned service, roles, and ordinary-session
  isolation intact.
- **Baseline crosslink.** [`docs/specification.md`](../specification.md) "Product
  and runtime" now names the accepted **2026-10-28 scoped exception** and points
  here, without weakening any confirmed invariant.
- **Verification performed (offline; no paid call, no hosted transfer,
  documentation-only).** `python3 scripts/check_contract_docs.py --self-test`
  (the Step 01 contract docs remain self-consistent and the negative-mutation
  self-tests still reject each mutation); `python3 -m unittest discover -s scripts
  -p 'test_*.py'`; `git diff --check`; and a manual relative-link check of every
  link added here and in the specification crosslink. This record adds narrative
  and a status/crosslink only; it changes **no** checker fixture, test, schema,
  contract, enum, policy, or numeric threshold.
- **Historical frozen checkpoint (not final-head evidence).** At the time this
  record was written the verified frozen checkpoint was `6ec58ea` (Python 3.14.4):
  **2,772 tests** (2,557 core + 20
  scripts + 79 python spike + 51 mock-Jev + 7 mock-integration + 58 TypeScript),
  **29 contract checks**, and `tsc`/`compileall`/`diffcheck` all clean. That
  checkpoint **predates** the E0/E1/E1B/E2 acceptance suites and the report/freeze
  CLI; it is **historical, not final-head evidence**. The **current** verified run
  is the `f3d97fe` full combined check (2,920 tests; see the *Current status
  amendment* above and the Step 05 handoff). Per-suite detail lives in the
  Step 05 handoff, not here. All claims in this record are scoped to mechanisms
  actually tested in this repository, never to literature/topic coverage.
- **Cost/rights honesty.** No paid call was authorized or made by this record; a
  tiny public metadata-adapter smoke was run by the coordinator **separately** (see
  the counts above). Live lead-model validation remains **unrun**.
- **Conditions for revisiting.** A user decision to change the discovery scope,
  the source set, the lead route, or the execution boundary; a material baseline
  change; or evidence that an accepted boundary fails in practice. Each change is
  a **new** exact-revision decision, not an in-place edit.

## Handoff

- Source baseline of this approval: merged `fb8260d0f48fa73bde1e0bc57ec119fbe24dcabb`
  (PR #8); reviewed code base `f860429`.
- Step authority and criterion status:
  [`docs/implementation/05-discovery.md`](../implementation/05-discovery.md),
  [`docs/implementation/handoffs/05-discovery.md`](../implementation/handoffs/05-discovery.md),
  [`docs/implementation/README.md`](../implementation/README.md).
- Baseline crosslink: [`docs/specification.md`](../specification.md) ("Product and
  runtime").
- Prior decisions: [`001`](001-runtime-language.md),
  [`002`](002-step01-contracts-and-policies.md).
- Contracts: [`C02`](../contracts/02-gates-and-runtime.md) §5.6/§6.3,
  [`C03`](../contracts/03-policy-decisions.md) §5.2/§5.5.
- Implementation surfaces: [`runtime/lead.py`](../../src/review_article_agent/runtime/lead.py),
  [`discovery/lead_runner.py`](../../src/review_article_agent/discovery/lead_runner.py),
  [`discovery/context.py`](../../src/review_article_agent/discovery/context.py),
  [`discovery/execution.py`](../../src/review_article_agent/discovery/execution.py),
  [`discovery/candidates.py`](../../src/review_article_agent/discovery/candidates.py),
  [`discovery/expansion.py`](../../src/review_article_agent/discovery/expansion.py).
- Decision format: [`docs/implementation/decision-template.md`](../implementation/decision-template.md).
- **Unresolved follow-up (not blockers):** the per-suite breakdown and the final
  completion handoff remain in the [Step 05 handoff](../implementation/handoffs/05-discovery.md);
  live lead-model validation (unrun); and the remaining Step 05 work — now
  **verification/release only** (the E2 full-CLI-chain case, a final frozen-HEAD
  full-suite run, and the PR; the E1B adversarial cases are now **integrated** at
  `630545c`); the report/freeze CLI is **integrated** (`e15b5b4`). No
  secrets and no licensed full text are included here.
