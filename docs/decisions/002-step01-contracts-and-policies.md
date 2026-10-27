# 002 — Step 01 scientific contracts, policies, and technical direction

## Status

**Accepted by the user — 2026-10-27.** The approving authority is the **user**. This
record is the Step 01 decision deliverable of
[`docs/implementation/01-contracts.md`](../implementation/01-contracts.md)
("record user disposition of P1–P6" and agree the operational and technical items,
lines 9–14, 17–23). It records the disposition of every P1–P6 item, every operational
policy, the rights behavior, and the technical direction of the Step 01 contract set
([`docs/contracts/03-policy-decisions.md`](../contracts/03-policy-decisions.md)).

**Decision anchor (reviewed revision).** The dispositions bind to the reviewed
revision **`ee7f7eafca80baa6c1aaca278361e56a663493c3`** and are dated **2026-10-27**.
The user's evidence is the recorded Step 01 interview at
`/home/fazekasda/.local/state/opencode-coordinator/6803b8c89a0b0dd2de643e7cb58db51e763c4278393e2fb549f2b30cd4ebb0c9/20261027T215129Z-72e522b4/interview.json`
(conversation selections recorded against that same `source_revision`; not an
independent approval service), plus the user's follow-up instruction
**"Proceed forward and finalize this step until a ready PR."**

**What this approval is and what it is not.** It approves the **described behavior
and technical direction** — with the concrete **interview amendments** recorded here
and in the register. It is **not** approval of every field-level spelling of any
schema or record, **not** an independent scientific validation, and **not** a claim
that any scientific feature is implemented. The record/schema representation in the
Step 01 contract set is an **implementation-facing, versioned contract**; its exact
field spelling remains subject to versioned revision. It does **not** authorize any
new **paid model call**, any source upload to a hosted service, or any scientific
feature completion. It does **not** reopen or weaken any already-confirmed baseline
invariant ([`docs/specification.md`](../specification.md) lines 11–63).

## Context and constraints

- **Baseline (authority, not weakened):** [`docs/specification.md`](../specification.md)
  — "Pending decisions" (P1–P6, line 69–74), "Gates and scientific invariants"
  (lines 52–63), "Citation-origin policy" (lines 44–50), "Product and runtime"
  (lines 9–20). The runtime language/integration was already decided in
  [`docs/decisions/001-runtime-language.md`](001-runtime-language.md)
  (Python core; accepted 2026-10-27) and is **not** reopened.
- **Step authority:** [`docs/implementation/01-contracts.md`](../implementation/01-contracts.md);
  kickoff [`docs/implementation/kickoffs/01-contracts.md`](../implementation/kickoffs/01-contracts.md).
- **Contract set under decision:** [`docs/contracts/03-policy-decisions.md`](../contracts/03-policy-decisions.md)
  (decision register + canonical `RightsRecord` v1),
  [`docs/contracts/01-identities.md`](../contracts/01-identities.md),
  [`docs/contracts/02-gates-and-runtime.md`](../contracts/02-gates-and-runtime.md),
  and the bundle map [`docs/contracts/README.md`](../contracts/README.md). The
  behavioral contracts in 01/02 are the implementation-facing representation of the
  identity/evidence/recovery behaviors accepted here.
- **Evidence file (read-only):** the recorded interview JSON quoted in
  [`03-policy-decisions.md`](../contracts/03-policy-decisions.md) sections 2–5; the
  register's synthetic `proposed`/invalid fixtures are historical format examples for
  the offline checker, not current dispositions.
- **Constraints preserved:** no fabricated numeric scientific threshold; approvals
  bind to immutable revisions and agents cannot self-approve; secondary attribution at
  a tracing limit stays a warned state requiring final human approval; untrusted
  data; rights/privacy boundaries; no secret/source-text leakage.

## Options and evidence

The Step 01 session prepared a **proposed** register
([`03-policy-decisions.md`](../contracts/03-policy-decisions.md) at `ee7f7ea`) in which
every item was recorded `proposed`, with a recommendation plus concrete alternatives
(accept / modify-with-alternative / defer). The user resolved each item in the
recorded interview by selecting the recommended option (verbatim selections below),
adding the concrete behaviors stated as amendments. No option was deferred and none
was rejected. The interview's `source_revision` is
`ee7f7eafca80baa6c1aaca278361e56a663493c3`, matching this decision's anchor.

**Interview selections (verbatim answers and dates, 2026-10-27):**

| Topic | Verbatim answer | Record |
| --- | --- | --- |
| P1 contribution direction | `"Evidence-led synthesis (Recommended)"` | POL-P1 |
| P1 concept admission | `"Require safeguards (Recommended)"` | POL-P1 |
| P2 context fidelity | `"Structured plus review (Recommended)"` | POL-P2 |
| P3 integrity | `"Entry and final checks (Recommended)"` | POL-P3 |
| P4 reporting | `"Companion plus summary (Recommended)"` | POL-P4 |
| P5 AI accountability | `"Require process (Recommended)"` | POL-P5 |
| P6 venue | `"Optional venue profile (Recommended)"` | POL-P6 |
| Hosted source authorization | `"Scoped batch authorization (Recommended)"` | RIGHTS-v1 / §5.2 |
| Pilot/audit policy | `"Project-specific plan (Recommended)"` | POL-OPS-PILOT |
| Jev screening mapping | `"Pilot-calibrated rules (Recommended)"` | POL-OPS-PILOT / project gate |
| Issue severities | `"Accept four categories (Recommended)"` | POL-OPS-SEVERITY |
| Search stopping | `"Coverage report plus gate (Recommended)"` | POL-OPS-COVERAGE |
| Cost defaults | `"Set defaults now"` | POL-OPS-COST |
| Tracing defaults policy | `"Required per project (Recommended)"` | POL-OPS-TRACE |
| Numeric budget/concurrency defaults | `"$25 / $10 / 2 (Recommended)"` | POL-OPS-COST |
| Secondary-origin warning format | `"Review plus companion (Recommended)"` | POL-OPS-SECONDARY |
| Technical architecture | `"Approve direction (Recommended)"` | ARCH-* |
| Identity/evidence/rights behavioral contracts | `"Approve behavior (Recommended)"` | RIGHTS-v1 / C01–C03 |
| Recovery semantics | `"Approve semantics (Recommended)"` | ARCH-RECOVERY / C02 |

The interview's own note states the scope precisely: *"Approval of described behavior
and technical direction, not unreviewed field-level exact schema or completion of
Step01 acceptance verification. No paid call/source upload authorization."* That note
is adopted as a constraint of this decision.

## Decision

**Accepted by the user, 2026-10-27, against reviewed revision
`ee7f7eafca80baa6c1aaca278361e56a663493c3`.** The authoritative per-record detail is
[`03-policy-decisions.md`](../contracts/03-policy-decisions.md) sections 2–5; the
summary dispositions and the concrete interview amendments are:

- **P1 — accepted (POL-P1).** Evidence-led synthesis with mandatory concept
  safeguards: definition, role, original support, counterevidence, and applicability
  limits; hypotheses distinguished from findings; material evidence changes reopen
  the approval; meaningful testable implications where applicable. **Never force a
  novel framework; clearly label novelty.** No numeric threshold.
- **P2 — accepted (POL-P2).** Structured applicable-context/causal-limits capture at
  **extraction**, preserved through **synthesis**, checked at **review**;
  consequential missing context **blocks** the affected claim. No numeric threshold.
- **P3 — accepted (POL-P3).** Integrity statuses checked at **corpus entry and final
  approval**, with status/provenance/date and human review of affected uses;
  integrity **unknown is not clean**; correction / non-replication / retraction are
  **not automatically conflated**.
- **P4 — accepted (POL-P4).** A detailed exportable **methods/limitations companion**
  plus a concise **manuscript-ready summary**; publication is **user-controlled**;
  workflow limitations reported honestly.
- **P5 — accepted (POL-P5).** Record actual AI use and versions; an **editable
  disclosure** with explicit **human accountability**; **provider terms verified
  before hosted use**, **journal policy verified before submission-facing approval**.
  Operation-scoped gates, not a blanket prohibition.
- **P6 — accepted (POL-P6).** **Optional** venue profile: capture venue constraints
  **only when a venue is selected**, with a fresh submission checklist and human
  confirmation; **venue-neutral output retained**. An unselected/unknown venue is
  **not a blanket prohibition**.
- **Hosted-use authorization — accepted (RIGHTS-v1, §5.2).** A **scoped batch**
  authorization naming the project/source manifest, provider/model route, purpose,
  and exact revision, granted after rights/privacy/provider gates; additions and
  material changes reopen the batch; **no per-source override** and the batch cannot
  override an individual source's restrictions.
- **Pilot/audit — accepted (POL-OPS-PILOT).** Mandatory **project-specific explicit**
  pilot/audit sizes and rules **before bulk work**; one iterative pilot; random +
  risk-based exclusion checks and omission/error extraction audits; important
  failures pause/correct/re-audit the affected work. **No universal sample size.**
- **Jev mapping — accepted (POL-OPS-PILOT / project gate).** Rubric-specific,
  **pilot-calibrated** decision rules with an uncertainty region; **raw probabilities
  persisted**; human exclusion gates mandatory; **no universal cutoff**.
- **Severity — accepted (POL-OPS-SEVERITY).** Four categories — **blocking / major /
  minor / style**; blocking and major issues, and any consequential unclassified
  issue, prevent completion.
- **Coverage/stopping — accepted (POL-OPS-COVERAGE).** Coverage report (sources,
  queries, dates, terminology, citation expansion, yields, duplicates, gaps) plus an
  explicit **human freeze of the exact candidate revision**; **no universal numeric
  stop**.
- **Cost/concurrency — accepted (POL-OPS-COST).** **USD-25 cumulative per project**,
  **USD-10 cumulative per stage**, **maximum 2 simultaneous external-model calls**.
  All routes and read-only model calls are counted; the reserve upper bound is set
  before dispatch; an unbounded cost is not dispatched; an unknown cost retains its
  reserve; reaching a cap stops new calls; only a human raises a cap; a restart does
  not reset. **Not a promise of total review cost.**
- **Tracing defaults — accepted (POL-OPS-TRACE).** Explicit per-project max hops,
  sources per claim/project, and resolution retries **required before any upstream
  tracing work**; cycles detected; **no universal default**.
- **Secondary-origin warning — accepted (POL-OPS-SECONDARY).** Review warning plus
  export companion; accurate manuscript secondary attribution; final human approval
  against the **exact manuscript/dependency/warning set**; material changes reopen;
  the state is **never primary verified**.
- **Technical direction — accepted (ARCH-*).** Per-project **SQLite** plus a minimal
  **atomic global registry/arbitration**; **immutable assets/revisions** with exact
  immutable approval/dependency/rights revisions; an **independent worker** over an
  **owner-only authenticated Unix-domain socket on Linux**, separate from the OpenCode
  HTTP service; package layout `src/review_article_agent/{domain,storage,cli,worker,adapters}`
  with **no agent framework**; consistent backup plus manifest/hash/approval
  verification. **Recovery semantics accepted per [`02-gates-and-runtime.md`](../contracts/02-gates-and-runtime.md)**:
  CLI detach continues an authorized job; pause/cancel preserve an unknown remote
  outcome; safe switch reconciles/quiesces; no silent success, blind retry, or budget
  reset; human disposition of ambiguity.
- **Identity/evidence/rights behavior — accepted (RIGHTS-v1, C01–C03).** Distinct
  source identities/provenance/context; exact immutable approval/dependency/rights
  revisions; **rights are separate**; an `unknown`/`not_allowed` right **blocks** the
  affected operation; a hosted batch **cannot override** source restrictions. The
  rights authorization schema and the **same-project / exact-revision guarantees**
  remain.

**Explicitly not decided / not claimed.** No scientific feature is implemented; no
numeric scientific threshold is set beyond the accepted cost/concurrency defaults;
the exact field spelling of any schema, the SQLite schema, the socket protocol, the
revision hash scheme, the dependency surface, and CLI spelling remain
implementation-facing, versioned details for Step 02 onward. No paid call or source
upload is authorized.

**Resolved Step 01 decisions vs. future project-setup gates.** The items above are
**resolved Step 01 decisions**. The following are **per-project setup gates**, not
Step 01 blockers ([`03-policy-decisions.md`](../contracts/03-policy-decisions.md) §6.2):
(1) an explicit project pilot/audit sample size and acceptance plan before bulk work;
(2) a rubric-specific Jev mapping/uncertainty region calibrated in the pilot;
(3) explicit per-project tracing limits before upstream tracing; (4) actual source
rights/privacy and provider authorizations before the affected operations; (5)
optional venue current-policy checks before submission-facing claims.

**No arbitrary numeric pilot/tracing thresholds** are set: where the user's policy is
"per project", the value is set at project setup with a fail-closed default (bulk work
blocked / `configuration_required`), never invented here.

## Consequences and verification

- **Unblocked.** Step 01's policy/technical items are dispositioned, so dependent
  behavior is no longer blocked by an open Step 01 decision, and Step 02 may proceed
  on the accepted technical direction. The register
  ([`03-policy-decisions.md`](../contracts/03-policy-decisions.md)) now records
  `accepted` statuses, the accepted defaults, the corrected P5/P6 operation-scoped
  gating, and the resolved-vs-project-gate split.
- **Baseline updated.** [`docs/specification.md`](../specification.md) "Pending
  decisions" now records the accept dispositions, and the stale "proposed store" /
  "numerical defaults not yet agreed" phrasings are updated to the accepted direction
  (per-project SQLite + registry; per-project tracing values) without weakening any
  confirmed invariant.
- **Not invalidated.** No prior approval is invalidated. Both prototypes and the
  accepted runtime decision (001) are unaffected; the contract set remains
  implementation-facing.
- **Verification performed (offline, no paid calls).**
  `python3 scripts/check_contract_docs.py --self-test` — the Step 01 contract docs are
  self-consistent, the eight C03 JSON fixtures and the C01/C02 examples behave as
  documented, and the negative-mutation self-tests reject each mutation. This decision
  adds narrative and status text only; it does **not** change any checker fixture,
  test, or schema field semantics.
- **Conditions for revisiting.** A user decision to change any accepted policy or the
  technical direction; a material change to the baseline; or evidence that an accepted
  behavior fails in practice. Each change is a **new** exact-revision decision, not an
  in-place edit.

## Handoff

- Per-record detail and accepted defaults:
  [`docs/contracts/03-policy-decisions.md`](../contracts/03-policy-decisions.md).
- Baseline with recorded dispositions:
  [`docs/specification.md`](../specification.md) ("Pending decisions").
- Contract set: [`docs/contracts/README.md`](../contracts/README.md),
  [`01-identities.md`](../contracts/01-identities.md),
  [`02-gates-and-runtime.md`](../contracts/02-gates-and-runtime.md).
- Prior decision: [`docs/decisions/001-runtime-language.md`](001-runtime-language.md).
- Step authority: [`docs/implementation/01-contracts.md`](../implementation/01-contracts.md);
  kickoff [`docs/implementation/kickoffs/01-contracts.md`](../implementation/kickoffs/01-contracts.md);
  decision format [`docs/implementation/decision-template.md`](../implementation/decision-template.md).
- **Unresolved follow-up (not blockers):** the five per-project setup gates above; and
  the implementation-facing details (SQLite schema, socket protocol, revision hash
  scheme, dependency surface, CLI spelling) that Step 02 onward designs under the
  accepted direction.

No secrets and no licensed full text are included here.
