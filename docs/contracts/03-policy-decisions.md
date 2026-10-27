# 03 — Policy and architecture decision register (v1)

> **Status: PROPOSED — awaiting the user. This document is not an approval, and it
> is not Step 01 completion.** The user authorized preparing the Step 01 contracts
> ("proceed preparing step01 contracts"); that instruction is **not** approval of
> any outstanding scientific policy, of the SQLite schema, of IPC, of the package
> layout, or of any architecture claim. Every policy item below is recorded as
> **OPEN (`proposed`) and owned by the user** until the user dispositions it with
> the evidence and exact revision the register requires (sections 1 and 2). No
> policy, schema, or architecture is ratified here; no behavior is unblocked by
> this file.

This register implements the Step 01 [deliverable](../implementation/01-contracts.md)
"record user disposition of P1–P6 … and agree pilot/audit sampling and acceptance,
search coverage/stopping, cost/concurrency limits, origin-hop/source/retry defaults,
and secondary-warning approval" (lines 9–12). It is a **decision register**, not a
decision record: it defines the record format (section 1), states each open item
faithfully against the [baseline](../specification.md) (sections 2–3), and records
**proposed** technical direction (sections 4–5) that the user has not accepted.
Per the register's own rules (section 1.3), a session or coordinator may **write**
the register but may **not** move a record out of `proposed`.

## 0. Preserved, already-confirmed baseline (not open here)

These are confirmed and are **not** reopened by this register; they are recorded so
proposals below cannot silently weaken them.

| Confirmed item | Source | Status |
| --- | --- | --- |
| **Python core** with a supported, explicitly authenticated OpenCode V2 HTTP integration | [decision 001](../decisions/001-runtime-language.md); [spec line 15](../specification.md) | Accepted by user 2026-10-27 |
| **One active project at a time** | [spec line 11](../specification.md) | Confirmed |
| Approvals bind to **immutable artifact/dependency revisions**; agents cannot self-approve | [spec lines 54–55](../specification.md) | Confirmed |
| Secondary attribution at a tracing limit is a **warned state requiring final human approval**, never verified primary support | [spec line 50](../specification.md) | Confirmed (mechanism) |
| **Max 3** critique/repair rounds (section review) | [spec line 38](../specification.md) | Confirmed numeric maximum |
| **Max 2** original-source factual-check/repair rounds | [spec line 39](../specification.md) | Confirmed numeric maximum |
| **Max 2** whole-manuscript repair rounds | [spec line 40](../specification.md) | Confirmed numeric maximum |
| Treat documents/model outputs as **untrusted data** | [spec line 62](../specification.md) | Confirmed |

The three repair-round maxima (3, 2, 2) above are **already-confirmed numeric
limits** and must **not** be confused with the **open** thresholds listed in section
3 (tracing hop/source/retry defaults, coverage stop rules, costs/concurrency). A
model edit cannot replenish an exhausted repair budget ([spec line 55](../specification.md)).

---

## 1. Decision record format v1

### 1.1 Fields and types

Each recorded decision is one record with at least these fields. Types are given so
later steps can validate records mechanically.

| Field | Type | Meaning |
| --- | --- | --- |
| `id` | string | Stable identifier, e.g. `POL-P1`, `POL-OPS-TRACE`, `RIGHTS-R1`. |
| `title` | string | Short human label. |
| `status` | enum | `proposed` \| `accepted` \| `modified` \| `deferred` (see 1.2). |
| `baseline_excerpt` | string | **Verbatim** baseline/reference text the decision resolves. |
| `baseline_ref` | string | File + line of the excerpt. |
| `recommendation` | string | A proposal only; **never** approval. |
| `options` | string[] | Concrete alternatives considered, each with a one-line trade-off. |
| `owner` | string | Accountable party. For every policy item here: **the user**. |
| `approval_required` | bool | True when only the user may move the record. |
| `user_evidence` | string \| null | Verbatim user statement (quoted) + date. `null` while `proposed`. |
| `decided_against_revision` | string \| null | Exact artifact revision (commit SHA) the user decision binds to. `null` while `proposed`. |
| `affected_stages` | string[] | Implementation step ids whose behavior depends on this record. |
| `blocking_dependencies` | string[] | Named behaviors that must stay blocked while the record is open. |
| `fail_closed_behavior` | string | What the system does when the value is unset/unknown. |
| `numeric_value` | number \| string \| `UNSET` | A number only when the **user** set it; `UNSET` otherwise. Never invented to let a stage pass. |
| `notes` | string | Caveats, limitations, revisit conditions. |

**Hard rule — no fabricated thresholds.** `numeric_value` is `UNSET` unless the user
explicitly supplies it. A stage may not pass by inventing a numeric scientific
threshold ([01-contracts acceptance](../implementation/01-contracts.md), line 18;
[kickoff line 103](../implementation/kickoffs/01-contracts.md)).

### 1.2 Status values and transitions

```text
                 (user accepts unchanged)
   proposed ──────────────────────────────► accepted
      │  │
      │  │        (user accepts with a change)
      │  └────────────────────────────────► modified   (terminal; supersedes proposed)
      │
      │           (user explicitly defers)
      └───────────────────────────────────► deferred   (terminal; reopen needs the user)

   open ("pending") = proposed with user_evidence = null and decided_against_revision = null
```

- `proposed` — recorded, owned, **not** ratified. This is the state of **every**
  policy item in this register.
- `accepted` — the user accepted the recommendation unchanged.
- `modified` — the user accepted a changed variant; the variant text is the
  authoritative record.
- `deferred` — the user explicitly chose to defer; **only the user may defer**.
- **Pending ≠ deferred.** An item still awaiting the user's disposition stays
  `proposed`; a coordinator/session may **not** record it as `deferred`. Deferral is
  a user act, not a way to clear a queue.

### 1.3 Transition rules (who may do what)

1. A session/coordinator **may create or update** a `proposed` record (fields,
   recommendation, options, blockers).
2. Only the **user** may move a record `proposed → accepted | modified | deferred`,
   and only by supplying `user_evidence` (verbatim quote + date) and
   `decided_against_revision` (exact SHA). Without both, the transition is invalid.
3. A record once `accepted`/`modified`/`deferred` is not silently reopened; a user
   revision produces a **new** exact-revision decision, consistent with approvals
   binding to revisions ([spec line 54](../specification.md)).
4. `modified` is a terminal acceptance variant; a later change is recorded as a new
   revision decision, not an in-place edit of the accepted text.
5. No agent/model output may set any status; only explicit user evidence may.

---

## 2. Policy decisions P1–P6 (each owned by the user; each OPEN / `proposed`)

The baseline requires Step 01 to record accept/modify/defer dispositions
([spec lines 65–74](../specification.md)). Nothing below is dispositioned: all six
are recorded `proposed`, owner **user**, with dependent behavior blocked.

### POL-P1 — Framework contribution and concept-admission/support/revision criteria

- **Baseline excerpt (verbatim).** "P1: explicit framework contribution,
  concept-admission/support/revision criteria and testable implications where
  applicable." ([spec line 69](../specification.md))
- **Recommendation (proposal only).** Define, in a versioned artifact, what counts
  as the review's *own* framework contribution versus a summary of others', plus
  admission criteria for concepts, the support required before a concept is used,
  and revision criteria when support changes; where applicable, require a testable
  implication per admitted concept. **No numeric scientific threshold is implied.**
- **Options.** (a) full formal criteria set at synthesis; (b) minimal criteria +
  per-claim "framework contribution" flag; (c) defer the formal criteria but block
  framework/outline finalization.
- **Owner / approval.** User. `approval_required = true`.
- **Affected stages.** [10 §synthesis](../implementation/README.md),
  [11 §drafting](../implementation/README.md), [12 §review](../implementation/README.md).
- **Blocking dependencies.** The "shared approved argument record" and framework
  proposal ([spec lines 34–35](../specification.md)) cannot be finalized; drafting of
  framework-bearing claims cannot be approved.
- **Fail-closed.** While open, framework-contribution claims stay **unclassified** and
  cannot be marked approved; the synthesis stage may produce *proposals* only.
- **numeric_value:** `UNSET` (criteria are categorical, not numeric).

### POL-P2 — Downstream causal/context-fidelity checks

- **Baseline excerpt (verbatim).** "P2: detailed downstream causal/context-fidelity
  checks." ([spec line 70](../specification.md))
- **Recommendation (proposal only).** Define the checks that verify a downstream
  conclusion preserves the upstream study's causal/associational status and context
  (species/population/assay/outcome limits), consistent with the confirmed invariant
  that these limitations must be carried into conclusions
  ([spec line 60](../specification.md)). No numeric threshold.
- **Options.** (a) structured fidelity checklist per extracted finding; (b) a review-time
  fidelity finding type; (c) defer but block causal claim approval.
- **Owner / approval.** User. `approval_required = true`.
- **Affected stages.** [09 §extraction](../implementation/README.md),
  [10 §synthesis](../implementation/README.md), [12 §review](../implementation/README.md),
  [13 §origin-tracing](../implementation/README.md).
- **Blocking dependencies.** Causal/context-fidelity findings cannot be generated or
  dispositioned; conclusions that depend on them cannot be approved.
- **Fail-closed.** An unchecked fidelity dimension is a **missing check**, not a pass
  ([spec line 57](../specification.md)); affected conclusions remain unapproved.
- **numeric_value:** `UNSET`.

### POL-P3 — Integrity-status checks (retractions, expressions of concern, corrections)

- **Baseline excerpt (verbatim).** "P3: integrity-status checks for retractions,
  expressions of concern, corrections, and adjudication of affected uses. Do not
  treat non-replication as retraction or automatically prohibit historical
  discussion." ([spec line 71](../specification.md))
- **Recommendation (proposal only).** Define an integrity-status record per source
  and per cited use, with the four statuses distinguished, an adjudication path for
  affected uses, and the two explicit non-rules preserved (non-replication ≠
  retraction; no automatic historical-discussion prohibition).
- **Options.** (a) per-source integrity record + per-use adjudication; (b) review-time
  integrity finding only; (c) defer but block any use flagged by an integrity signal.
- **Owner / approval.** User. `approval_required = true`.
- **Affected stages.** [05 §discovery](../implementation/README.md),
  [07 §parsing](../implementation/README.md), [09 §extraction](../implementation/README.md),
  [10 §synthesis](../implementation/README.md), [13 §origin-tracing](../implementation/README.md),
  [14 §delivery](../implementation/README.md).
- **Blocking dependencies.** Retraction/expression-of-concern/correction handling and
  adjudication of affected uses are blocked; an integrity-unknown source cannot be
  silently treated as clean.
- **Fail-closed.** Unknown integrity status is an explicit **unknown** state that
  blocks approval of dependent uses; it is never "no issue".
- **numeric_value:** `UNSET`.

### POL-P4 — Public selection/synthesis and limitations reporting

- **Baseline excerpt (verbatim).** "P4: public selection/synthesis and limitations
  reporting alongside the manuscript." ([spec line 72](../specification.md))
- **Recommendation (proposal only).** Define the reporting artifact that accompanies
  the manuscript: how selection/synthesis decisions and limitations are reported
  publicly, aligned with the confirmed rule that a pilot and sampled audits are *not*
  held-out validation ([spec line 59](../specification.md)).
- **Options.** (a) companion report in the export set; (b) a reporting section inside
  the manuscript; (c) defer but block final export.
- **Owner / approval.** User. `approval_required = true`.
- **Affected stages.** [14 §delivery](../implementation/README.md),
  [15 §release](../implementation/README.md).
- **Blocking dependencies.** The required reporting artifact cannot be declared
  complete; export cannot claim the reporting requirement is satisfied.
- **Fail-closed.** Without an agreed format, the export carries an explicit
  "reporting requirement open" flag rather than omitting it.
- **numeric_value:** `UNSET`.

### POL-P5 — AI-use declaration, human accountability, provider terms, journal policies

- **Baseline excerpt (verbatim).** "P5: AI-use declaration, human accountability,
  applicable provider terms and journal policies." ([spec line 73](../specification.md))
- **Recommendation (proposal only).** Define the AI-use declaration content, the
  human-accountability statement, and the process for confirming applicable provider
  terms and target-journal policies before any submission-facing output or any
  hosted-model transfer. **Verification of a provider's or journal's terms is itself
  pending under P5** — its absence is not permission (see section 5.5).
- **Options.** (a) declaration template + accountability statement + a per-venue
  terms checklist; (b) declaration only, terms checked at submission; (c) defer but
  block submission-facing output and hosted transfer.
- **Owner / approval.** User. `approval_required = true`.
- **Affected stages.** [04 §agent-runtime](../implementation/README.md),
  [14 §delivery](../implementation/README.md), [15 §release](../implementation/README.md).
- **Blocking dependencies.** Hosted-model transfer authorization and any
  submission-facing AI declaration are blocked. (Hosted transfer is *also*
  independently gated by rights — section 5.)
- **Fail-closed.** Hosted-model transfer defaults to **denied** until authorized
  (sections 5.2–5.3); the declaration is a mandatory output, not optional.
- **numeric_value:** `UNSET`.

### POL-P6 — Optional target-venue constraints and submission-time verification

- **Baseline excerpt (verbatim).** "P6: optional target-venue constraint capture and
  submission-time verification; retain venue-neutral output." ([spec line 74](../specification.md))
- **Recommendation (proposal only).** Define optional capture of target-venue
  constraints and a submission-time verification step, while keeping the retained
  output **venue-neutral** (matching the confirmed venue-neutral output rule).
- **Options.** (a) optional venue-constraint record + verification checklist;
  (b) verification only, no capture; (c) defer but block any venue-specific claim.
- **Owner / approval.** User. `approval_required = true`.
- **Affected stages.** [10 §synthesis](../implementation/README.md) (outline
  constraints), [14 §delivery](../implementation/README.md),
  [15 §release](../implementation/README.md).
- **Blocking dependencies.** Venue-specific constraint capture and submission-time
  verification are blocked; the **venue-neutral** output is preserved regardless.
- **Fail-closed.** Without a captured venue, the pipeline produces venue-neutral
  output and cannot assert venue compliance.
- **numeric_value:** `UNSET`.

---

## 3. Operational policies still open (owner: user; status `proposed`)

Baseline: "Also decide pilot/audit samples and acceptance rules, issue severities,
coverage/stopping rules, cost/concurrency limits, tracing defaults, and explicit
acceptance of secondary-origin warnings." ([spec line 76](../specification.md)).
Proposed values may remain **unset**; each has an explicit fail-closed behavior.

### POL-OPS-PILOT — Pilot/audit sampling and acceptance rules

- **Baseline.** One iterative pilot example set, no separate held-out set; human
  audits examine omissions and incorrect findings; important failures block bulk
  digestion ([spec lines 30, 32](../specification.md)).
- **Recommendation (proposal only).** Define the pilot sample, the exclusion-batch
  audit (random + risk-based original-source checks) sample, the extraction-audit
  sample, and the acceptance rule for each. Samples are **not** held-out validation
  ([spec line 59](../specification.md)).
- **Options.** (a) explicit sampling rule + acceptance thresholds; (b) sampling
  proportions left to per-project user choice with a default; (c) defer but block
  pilot approval.
- **Affected stages.** [08 §screening](../implementation/README.md),
  [09 §extraction](../implementation/README.md), [12 §review](../implementation/README.md).
- **Blocking dependencies.** Pilot approval and bulk-digestion gates cannot be
  triggered to pass.
- **Fail-closed.** With unset samples, no bulk-digestion approval can be granted;
  the pilot remains open.
- **numeric_value:** `UNSET` (no invented accuracy/acceptance number).

### POL-OPS-SEVERITY — Issue severity scale

- **Baseline.** Findings need "evidence or a concrete missing check, severity, and
  explicit fixed/rejected-with-reason disposition" ([spec line 57](../specification.md)).
- **Recommendation (proposal only).** Define the severity scale used by findings
  (critic, fact-check, whole-manuscript, origin) and which severities block. The
  *requirement* that a severity exists is confirmed; the *scale* is open.
- **Options.** (a) small ordinal scale with defined blocking severities; (b) two-level
  blocking/non-blocking; (c) defer but block any finding from being auto-passed.
- **Affected stages.** [08](../implementation/README.md)–[14](../implementation/README.md)
  (all finding producers).
- **Blocking dependencies.** Findings cannot be dispositioned to pass without a
  severity.
- **Fail-closed.** An unclassified-severity finding is treated as **blocking** until
  classified.
- **numeric_value:** `UNSET`.

### POL-OPS-COVERAGE — Search coverage report and stopping rule

- **Baseline.** "A coverage report supports an explicit human decision to search
  again or freeze the candidate revision. Never promise all papers."
  ([spec line 28](../specification.md)).
- **Recommendation (proposal only).** Define what the coverage report contains and
  the criteria under which the human freezes the candidate revision (no "all papers"
  guarantee).
- **Options.** (a) documented coverage metrics + human freeze decision;
  (b) human-only freeze with no numeric stop rule; (c) defer but block corpus freeze.
- **Affected stages.** [05 §discovery](../implementation/README.md).
- **Blocking dependencies.** Candidate-revision freeze and downstream acquisition
  cannot proceed on an unreported coverage state.
- **Fail-closed.** Without a stopping rule, the state stays **"search open"** and
  cannot be frozen to pass.
- **numeric_value:** `UNSET` (coverage/stop numbers not agreed).

### POL-OPS-COST — Cost and concurrency limits

- **Baseline.** Bounded budgets/concurrency; no busy loop while waiting
  ([spec lines 11, 13](../specification.md);
  [03 lines 12, 21](../implementation/03-durable-worker.md)). Only one paid call
  ($0.00002037) is recorded in the project so far
  ([00 handoff](../implementation/handoffs/00-runtime-spike.md)).
- **Recommendation (proposal only).** Define per-project and per-stage cost ceilings
  and the within-project concurrency bound; keep the global
  **one-active-project** limit (section 4.6).
- **Options.** (a) explicit ceilings + concurrency bound; (b) user-set per project
  with no default; (c) defer but block all paid calls.
- **Affected stages.** [03](../implementation/03-durable-worker.md),
  [04](../implementation/04-agent-runtime.md), and every paid-capable stage.
- **Blocking dependencies.** Any new paid call or concurrency above the bound is
  blocked.
- **Fail-closed.** With unset ceilings, **no new paid call** is issued; unknown cost
  retains its reserve and is never reported as zero
  ([live-jev accounting](../implementation/README.md)).
- **numeric_value:** `UNSET` (currency ceilings and concurrency bound not agreed).

### POL-OPS-TRACE — Citation tracing hop/source/retry defaults

- **Baseline excerpt (verbatim).** "Configure maximum citation hops, new sources per
  claim/project, and resolution retries; detect cycles. Numerical defaults are **not
  yet agreed**. These are not the writer's repair budgets." ([spec line 48](../specification.md))
- **Recommendation (proposal only).** Define maximum citation hops, new sources per
  claim and per project, and resolution retries, with cycle detection. These are
  explicitly **distinct from** the confirmed repair budgets (section 0).
- **Options.** (a) explicit numbers; (b) user-set per project with no default;
  (c) defer but treat any limit as reached.
- **Affected stages.** [13 §origin-tracing](../implementation/README.md).
- **Blocking dependencies.** Tracing cannot run past an unset limit; a depth-0 claim
  is not treated as trivially resolved.
- **Fail-closed.** At an **unset** limit, the tracer behaves as if the limit is
  reached: it stops and produces the **warned secondary-attribution state requiring
  final human approval** ([spec line 50](../specification.md)) — never "verified
  primary support".
- **numeric_value:** `UNSET`.

### POL-OPS-SECONDARY — Explicit acceptance of secondary-origin warnings

- **Baseline excerpt (verbatim).** "At a limit or unavailable upstream origin, the
  user chose **explicit secondary attribution with a visible warning and final human
  approval**. Cite the source actually read; never substitute an unread original or
  claim direct verification." ([spec line 50](../specification.md))
- **Status nuance.** The **mechanism** is confirmed baseline (section 0): the warned
  secondary state exists and requires final human approval. What remains **open** is
  the **operational definition** — the warning's record type/severity, where the
  approval binds (exact revision), and how it surfaces in export. The register keeps
  this `proposed` while **never** weakening the confirmed mechanism.
- **Recommendation (proposal only).** A first-class "secondary-attribution warning"
  record bound to the exact artifact revision, surfaced at final read-only validation
  and in export, requiring a recorded human approval.
- **Options.** (a) dedicated warning record + revision-bound approval; (b) reuse the
  findings/episode severity with a secondary flag; (c) defer the record type but keep
  the confirmed warned-state invariant.
- **Affected stages.** [12 §review](../implementation/README.md),
  [13 §origin-tracing](../implementation/README.md),
  [14 §delivery](../implementation/README.md).
- **Blocking dependencies.** Final approval of a claim reached only through a
  secondary source cannot be recorded as verified primary support.
- **Fail-closed.** Every secondary-origin claim stays **warned and unapproved** until
  a human approval is recorded against the exact revision.
- **numeric_value:** `UNSET` (not numeric).

---

## 4. Proposed technical direction (NOT accepted — user gate required)

Everything in this section is a **proposal** chosen against the Step 00 spike
evidence; the user has not accepted it, and no schema or executable architecture is
approved. The runtime language is already confirmed (section 0); that does not
approve any of the choices below, which Step 01 must present for the user's decision
([01-contracts lines 12, 23](../implementation/01-contracts.md)).

### ARCH-STORE — One app-owned project-scoped SQLite store + minimal app-level registry

- **Proposal.** One app-owned project-scoped SQLite store per project, plus a
  **minimal application-level registry** that maps application project IDs to store
  locations and performs **active-project arbitration**. Application project IDs are
  independent of paths or Git ([02 lines 9, 16](../implementation/02-project-foundation.md)).
- **Alternatives.** (a) a single global SQLite file with a project column;
  (b) one SQLite file per project + a registry file; (c) an embedded daemon store.
- **Evidence.** SQLite is a *proposed* local store, not an approved schema; "prefer
  the simplest design that passes the recovery tests"; do not depend on OpenCode's
  private database ([spec line 16](../specification.md)). The Step 00 harness
  persisted sessions across a real restart using SQLite at
  `$XDG_DATA_HOME/opencode/opencode.db`
  ([harness README](../implementation/README.md)); the spike did **not** define an
  application schema.
- **Limitations.** No schema is proposed here; the store must satisfy untested
  recovery/migration/backup criteria ([02 lines 20–21](../implementation/02-project-foundation.md)).
  A registry is an app-owned file, not an OpenCode database.
- **User gate.** The user decides store-per-project vs single-store and the registry
  shape; until then, no schema is authoritative.

### ARCH-ASSETS — Immutable assets and revision dependencies

- **Proposal.** Assets are content-addressed immutable revisions; approvals and
  dependency edges reference exact artifact revisions, not filenames or generic
  permissions ([spec lines 54–55](../specification.md)). An approved artifact edited
  afterward becomes a proposal and invalidates affected checks
  ([02 lines 16–17](../implementation/02-project-foundation.md)).
- **Alternatives.** (a) hash-addressed immutable revisions + dependency edges;
  (b) mutable rows with an audit log; (c) external content store + manifest.
- **Evidence.** Confirmed invariant that approvals bind to immutable revisions and
  that a model edit cannot replenish a review budget; Step 02 deliverable "immutable
  artifact revisions … approvals/dependencies"
  ([02 lines 10–11](../implementation/02-project-foundation.md)).
- **Limitations.** The exact revision identity/hash scheme is not designed here; the
  invalidation propagation graph is a Step 02 concern.
- **User gate.** The user confirms revision identity and dependency semantics.

### ARCH-IPC — Local authenticated IPC

- **Proposal.** Owner-only, authenticated local IPC between CLI and the background
  worker; no root or public unauthenticated listener required; authentication rejects
  non-owner control ([03 lines 9, 22](../implementation/03-durable-worker.md)).
- **Alternatives.** (a) authenticated loopback HTTP; (b) OS-owner-scoped socket/pipe;
  (c) an embedded in-process worker.
- **Evidence.** The accepted runtime decision's defaults: explicit endpoint and
  credential only, loopback-only, fail-closed, redirects refused
  ([decision 001](../decisions/001-runtime-language.md)); the harness binds
  `127.0.0.1` with a per-run password and refuses foreign control
  (harness README). Step 03 requires authenticated local control.
- **Limitations.** No IPC protocol is specified; the transport choice is untested as
  an application contract.
- **User gate.** The user selects the IPC mechanism; nothing is bound to it yet.

### ARCH-PKG — Python package layout

- **Proposal (proposal only).** A Python-core package layout (application core +
  bounded tool/adapter modules for the tested direct-OpenRouter Jev route), keeping
  the hand-maintained OpenCode HTTP integration isolated behind one adapter module.
- **Alternatives.** (a) single application package with adapter submodules;
  (b) core package + separate tool processes; (c) core + plugin packages.
- **Evidence.** Python core is accepted; the tested Jev route is the Python
  direct-OpenRouter adapter; the native route is unexercised
  ([decision 001](../decisions/001-runtime-language.md)). The Python prototype is
  stdlib-only.
- **Limitations.** Package/module names are not decided; no dependency surface is
  chosen. CLI spelling is Step 02, not here.
- **User gate.** The user confirms the package layout (or defers to Step 02).

### ARCH-MIGRATE — Migrations and backup/WAL consistency

- **Proposal.** Versioned storage migrations with manifest/schema versions; a
  consistent backup/export and restore that captures WAL state, verified including
  while writes occur during backup; unsupported newer schemas fail clearly; restore
  verifies artifact hashes and approvals
  ([02 lines 10, 20–21](../implementation/02-project-foundation.md)).
- **Alternatives.** (a) SQLite online backup / snapshot API with versioned
  migrations; (b) file-level copy of db+`-wal`+`-shm`; (c) export/import manifest.
- **Evidence.** Step 02 explicitly forbids copying "only the live database file and
  ignor[ing] WAL state" and requires using SQLite's snapshot/backup mechanism, not an
  arbitrary copy of a live WAL database
  ([02 line 20](../implementation/02-project-foundation.md);
  [README line 88](../implementation/README.md)). The Step 00 harness demonstrated
  durable persistence across process restart on the same data home.
- **Limitations.** No migration tool or schema version exists yet; WAL-consistency
  during concurrent writes is a test obligation of Step 02, not proven here.
- **User gate.** The user confirms migration/backup semantics alongside ARCH-STORE.

### ARCH-ACTIVE — One global active-project invariant

- **Proposal.** Exactly **one** active project executes at a time globally; concurrent
  activation attempts cannot execute two projects; a safe switch reconciles or drains
  existing work before another project executes and cannot leave old-project jobs
  committing into the new project
  ([03 lines 11, 19](../implementation/03-durable-worker.md);
  [README line 66](../implementation/README.md)). Waiting gates release execution
  resources.
- **Alternatives.** (a) a single global lock/lease for the active project;
  (b) per-project locks with a global arbiter; (c) a single-worker model.
- **Evidence.** Confirmed baseline "one active project at a time"
  ([spec line 11](../specification.md)); global definition of done "Exactly one
  project may execute at once"
  ([README line 66](../implementation/README.md)); the Step 00 harness used `flock`
  to exclude concurrent mutation (harness README).
- **Limitations.** **Within-project** concurrency may be bounded independently
  ([spec line 11](../specification.md)) and is not defined here; fencing protects
  accepted writes, not arbitrary external side effects
  ([03 line 20](../implementation/03-durable-worker.md)).
- **User gate.** The user confirms the invariant's enforcement is contract-level; it
  is not an accepted architecture until then.

---

## 5. Rights and privacy model v1

### 5.1 Mandatory record types

A rights/privacy record is mandatory for every acquired source and every use of it.
At minimum a record carries:

| Type | Meaning |
| --- | --- |
| `source_id` | The acquired source/asset the rights attach to. |
| `rights.acquisition` | Right to obtain the source by the chosen method. |
| `rights.storage` | Right to store the source locally. |
| `rights.redistribution` | Right to include/redistribute the source in output. |
| `rights.hosted_transfer` | Right to send the source to a hosted model/provider. |
| `rights_evidence` | The provenance/authority for each right (or `unknown`). |
| `privacy_flags` | Confidentiality/personal-data constraints relevant to the source. |
| `status` | `granted` \| `denied` \| `unknown` per right (default `unknown`). |

The four rights are **separate**: "Acquisition, storage, redistribution, and
hosted-model transfer rights are separate. Uploading a file is not proof of all
rights." ([spec line 63](../specification.md)).

### 5.2 Rights enforcement

- **Unknown blocks the affected operation.** An operation whose required right is
  `unknown` or `denied` is **blocked**, not attempted; the block is an explicit state.
- **Hosted tools default denied.** Sending source content to a hosted model/provider
  defaults to **denied** until the user authorizes `hosted_transfer` for that source.
  This is independent of, and additional to, the P5 gate (section 2, POL-P5).

### 5.3 Logging, fixtures, and secrets

- Logs and test fixtures must contain **no secrets and no licensed source text**
  ([01-contracts line 22](../implementation/01-contracts.md);
  [README line 58](../implementation/README.md)). The Step 00 reports record
  endpoints/ports only and include no credential or paper text (handoff; live-jev
  and integration READMEs). This register contains no licensed excerpts.

### 5.4 Source content is untrusted

"Treat documents/model outputs as untrusted data. No arbitrary source instructions,
authoritative database edits by agents, hidden approvals, public unauthenticated
services, or credential leakage." ([spec line 62](../specification.md)). Retrieved
source text is data, never instructions that can widen scope, approve, or invoke
unapproved tools ([04 line 17](../implementation/04-agent-runtime.md)).

### 5.5 Provider/journal verification is pending, not permission

Confirming applicable provider terms and target-journal policies is **pending under
P5/P6** ([spec lines 73–74](../specification.md)). The **absence** of a known
prohibition is **not** permission: until the user dispositions P5 (and any P6 venue
constraint), hosted-model transfer and submission-facing output stay gated
(sections 5.2, POL-P5). "Uploading a file is not proof of all rights"
([spec line 63](../specification.md)).

### 5.6 Synthetic rights examples (invalid / missing permissions)

These examples are **synthetic**; they reference no licensed text.

**Example A — mixed rights (redistribution unknown).** A synthetic source record:

```json
{
  "source_id": "syn-example-A",
  "rights": {
    "acquisition": "granted",
    "storage": "granted",
    "redistribution": "unknown",
    "hosted_transfer": "denied"
  },
  "privacy_flags": [],
  "status": "partial"
}
```

Expected behavior: acquisition and storage are allowed; **redistribution is blocked**
(output/export of the source content), and **hosted transfer is blocked** (denied).
The pipeline proceeds only with operations permitted by granted rights,
fail-closed.

**Example B — missing permissions (no rights record).** A synthetic source with no
rights record at all resolves every right to its default `unknown`. Expected
behavior: **all four operations are blocked**; the source is retained as an explicit
`rights_unknown` state and cannot be redistributed or transferred until the user
supplies rights evidence.

**Invalid-transition example.** An attempt to record `hosted_transfer: granted` with
`rights_evidence: unknown` is **rejected**: a granted right requires provenance; a
grant without evidence cannot unblock the operation.

---

## 6. Exact questions the user can answer next

Answer any subset; each answer is a producer of `user_evidence` + a target revision
for section 1.2. Short answers are enough; no long prose required.

1. **P1:** Accept, modify, or defer the framework-contribution/concept criteria
   (POL-P1)? If modify, what must the criteria add or drop?
2. **P2:** Which downstream causal/context-fidelity checks are required (POL-P2)?
3. **P3:** Confirm the integrity statuses and the adjudication path for affected uses
   (POL-P3). Is the "non-replication ≠ retraction" wording correct as recorded?
4. **P4:** What selection/synthesis + limitations reporting accompanies the
   manuscript (POL-P4)?
5. **P5:** Approve the AI-use declaration content and the human-accountability
   statement (POL-P5)?
6. **P6:** Is target-venue constraint capture **on** (optional) or **off**, and who
   performs submission-time verification (POL-P6)?
7. **Pilot/audit:** What pilot and audit sample(s) and acceptance rule (POL-OPS-PILOT)?
8. **Severity:** What severity scale, and which severities block (POL-OPS-SEVERITY)?
9. **Coverage/stopping:** What coverage-report contents and freeze rule
   (POL-OPS-COVERAGE)?
10. **Cost/concurrency:** What per-project/per-stage cost ceilings and
    within-project concurrency bound (POL-OPS-COST)?
11. **Tracing:** What max citation hops, new sources per claim/project, and resolution
    retries (POL-OPS-TRACE)?
12. **Secondary warnings:** Confirm the warning record type/severity and where final
    human approval binds (POL-OPS-SECONDARY).
13. **Technical direction:** Accept, modify, or defer ARCH-STORE, ARCH-ASSETS,
    ARCH-IPC, ARCH-PKG, ARCH-MIGRATE, ARCH-ACTIVE (section 4) — and, if accepted,
    against which exact revision?
14. **Rights:** Confirm the mandatory rights fields (section 5.1) and that unknown
    rights block the affected operation.

---

## 7. Status summary

| Record | Topic | Owner | Status | Numeric | Blocks |
| --- | --- | --- | --- | --- | --- |
| POL-P1 | Framework contribution / concept criteria | user | **proposed (OPEN)** | UNSET | 10, 11, 12 |
| POL-P2 | Downstream causal/context-fidelity checks | user | **proposed (OPEN)** | UNSET | 09, 10, 12, 13 |
| POL-P3 | Integrity-status checks | user | **proposed (OPEN)** | UNSET | 05, 07, 09, 10, 13, 14 |
| POL-P4 | Public selection/synthesis + limitations reporting | user | **proposed (OPEN)** | UNSET | 14, 15 |
| POL-P5 | AI-use declaration / accountability / provider+journal terms | user | **proposed (OPEN)** | UNSET | 04, 14, 15 |
| POL-P6 | Target-venue constraints + submission verification | user | **proposed (OPEN)** | UNSET | 10, 14, 15 |
| POL-OPS-PILOT | Pilot/audit sampling and acceptance | user | **proposed (OPEN)** | UNSET | 08, 09, 12 |
| POL-OPS-SEVERITY | Issue severity scale | user | **proposed (OPEN)** | UNSET | 08–14 |
| POL-OPS-COVERAGE | Coverage/stopping rule | user | **proposed (OPEN)** | UNSET | 05 |
| POL-OPS-COST | Cost/concurrency limits | user | **proposed (OPEN)** | UNSET | 03, 04, paid stages |
| POL-OPS-TRACE | Tracing hop/source/retry defaults | user | **proposed (OPEN)** | UNSET | 13 |
| POL-OPS-SECONDARY | Secondary-warning operational acceptance | user | **proposed (OPEN)** | UNSET | 12, 13, 14 |
| ARCH-STORE | Store + registry + active-project arbitration | user | **proposed (OPEN)** | — | 02 |
| ARCH-ASSETS | Immutable assets + revision dependencies | user | **proposed (OPEN)** | — | 02 |
| ARCH-IPC | Local authenticated IPC | user | **proposed (OPEN)** | — | 03 |
| ARCH-PKG | Python package layout | user | **proposed (OPEN)** | — | 02–04 |
| ARCH-MIGRATE | Migrations + backup/WAL consistency | user | **proposed (OPEN)** | — | 02 |
| ARCH-ACTIVE | One global active-project invariant | user | **proposed (OPEN)** | — | 02, 03 |
| RIGHTS-* | Rights/privacy record types and enforcement | user | **proposed (OPEN)** | — | 04, 06, 09, 14 |

**No record above is accepted. No dependent behavior is unblocked. Step 01 is not
complete.**

---

## 8. Related records

- Baseline: [`docs/specification.md`](../specification.md) — "Pending decisions",
  "Gates and scientific invariants", "Citation-origin policy", "Product and runtime".
- Step authority: [`docs/implementation/01-contracts.md`](../implementation/01-contracts.md);
  kickoff [`docs/implementation/kickoffs/01-contracts.md`](../implementation/kickoffs/01-contracts.md).
- Decision format: [`docs/implementation/decision-template.md`](../implementation/decision-template.md);
  adopted language decision [`docs/decisions/001-runtime-language.md`](../decisions/001-runtime-language.md).
- Step 00 evidence: [`docs/implementation/handoffs/00-runtime-spike.md`](../implementation/handoffs/00-runtime-spike.md);
  plan index [`docs/implementation/README.md`](../implementation/README.md).
- Consumers of Step 01 decisions:
  [`02 §CLI, projects, artifacts, backups`](../implementation/02-project-foundation.md),
  [`04 §restricted agent roles`](../implementation/04-agent-runtime.md)
  (and the worker in [`03 §background worker`](../implementation/03-durable-worker.md)).

No secrets and no licensed full text are included in this document.
