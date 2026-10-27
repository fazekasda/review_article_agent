# Contract 02 — Gates, approvals, findings budgets, and runtime safety

## Status and authority

- **Contract version:** `v1` (draft representation, dated 2026-10-27).
- **Approval state: PROPOSED — approval pending.** The *requirements* below are
  carried over from already-confirmed sources and must not be weakened. The
  *representations* introduced here — record names, field names, field types,
  enum literals, and the proposed severity taxonomy — are **proposals for user
  review**, not accepted policy. Nothing in this document is an accepted
  scientific or product decision until the user ratifies it.
- **Scope:** revision-specific approvals and their dependency snapshots; the
  findings/issue lifecycle and review budgets; machine (model) decisions versus
  human decisions; and runtime safety (pause/cancel, project switching, restart
  reconciliation, admission vs completion). Durable storage, IPC, and package
  layout are **not** decided here (step 02); the SQLite schema remains a
  proposal (`docs/specification.md` line 16).
- **No scientific feature claim.** This contract defines record shapes and
  invariants only. It does not claim that screening, extraction, drafting,
  review, or origin tracing is implemented, correct, or scientifically
  validated.
- **Confirmed sources preserved** (read-only authority for this contract):
  [`docs/specification.md`](../specification.md) ("Gates and scientific
  invariants" lines 52–63; "Citation-origin policy" lines 44–50; "Confirmed
  scientific workflow" lines 22–42),
  [`docs/implementation/01-contracts.md`](../implementation/01-contracts.md)
  (deliverables lines 9–13, acceptance lines 17–23),
  [`docs/implementation/03-durable-worker.md`](../implementation/03-durable-worker.md),
  [`docs/implementation/04-agent-runtime.md`](../implementation/04-agent-runtime.md),
  [`docs/implementation/12-review.md`](../implementation/12-review.md),
  [`docs/implementation/13-origin-tracing.md`](../implementation/13-origin-tracing.md),
  [`docs/decisions/001-runtime-language.md`](../decisions/001-runtime-language.md),
  and [`docs/implementation/handoffs/00-runtime-spike.md`](../implementation/handoffs/00-runtime-spike.md).
- **Sibling dependency:** artifact references use the immutable reference tuple
  `{project_id, artifact_id, revision_id}` defined by the sibling
  identity-and-evidence contract produced alongside this document in step 01.
  That tuple is assumed, not redefined here.
- **Future reference (not a link):** rights, privacy, hosted-model transfer, and
  provider/journal policy are reserved for the planned policy contract
  `docs/contracts/03-policy-decisions.md`. That file is **absent** from this
  checkout and is referenced as a planned path only, deliberately without a
  link.

## Notation and typing rules

| Token | Meaning |
| --- | --- |
| `string` | UTF-8 text. |
| `id` | Opaque, stable, non-empty application-assigned identifier. |
| `ref` | Immutable artifact reference `{project_id, artifact_id, revision_id}` (sibling contract). |
| `enum{...}` | Exactly one listed literal; an unrecognized value is **invalid** (fail closed, never coerced). |
| `ts` | UTC timestamp, ISO-8601, with explicit offset. |
| `digest` | Lowercase hex content digest; the algorithm is named in `digest_alg`. |
| `list<T>` | Ordered sequence of `T`. |
| `map<K,V>` | Key→value mapping with `K`/`V` as named types. |
| `bool`, `int`, `number` | JSON boolean / integer / number. |
| `M` | Mandatory. **If absent, the containing record is `invalid` and every behavior depending on it is `blocked`.** |
| `O` | Optional; absence is itself a defined state, not silently defaulted. |
| `M*` | Mandatory-union: at least one member of a named set must be present. |

Invariant shorthand used below: **MUST**, **MUST NOT**, and **`invalid`/`blocked`**
are normative for this proposed contract. No numeric scientific threshold may be
invented to let a stage pass; where a limit is unresolved it is named as
`unresolved` and blocks the dependent behavior.

## 1. Actors, provenance, and proposals

### 1.1 `Actor`

| Field | Type | Presence | Invariant |
| --- | --- | --- | --- |
| `actor_kind` | `enum{human, model, system}` | M | `system` = the application worker, never an approver. |
| `actor_id` | `id` | M | Stable application identity. |
| `authenticated` | `bool` | M | MUST be `true` for any `Actor(human)` that grants an approval; a non-owner control attempt is rejected. |
| `provider_id` | `string` | O | Mandatory when `actor_kind = model`. |
| `model_id` | `string` | O | Mandatory when `actor_kind = model`. |
| `session_id` | `id` | O | Present when the work ran in an app-owned OpenCode session. |

`actor_kind` MUST NOT be inferable from `actor_id` text; provenance is the
explicit `actor_kind` field.

### 1.2 `Proposal` (machine output)

| Field | Type | Presence | Invariant |
| --- | --- | --- | --- |
| `proposal_id` | `id` | M | |
| `produced_by` | `Actor` | M | `actor_kind` MUST be `model`. |
| `target` | `ref` | M | The exact revision the proposal concerns. |
| `versions` | `VersionSet` | M | See §6.1. |
| `payload_digest` | `digest` | M | |

Invariant: a `Proposal` MUST NOT substitute for an `Approval`. A model edit MUST
NOT create, widen, or replenish any approval or budget.

## 2. Revision-specific approvals

### 2.1 `Approval` (human)

| Field | Type | Presence | Invariant |
| --- | --- | --- | --- |
| `approval_id` | `id` | M | |
| `granted_by` | `Actor` | M | `actor_kind` MUST be `human` and `authenticated` MUST be `true`. |
| `target` | `ref` | M | Exact `{project_id, artifact_id, revision_id}`. |
| `decision` | `enum{approved, rejected}` | M | |
| `scope` | `enum{artifact_revision, gate, batch, exclusion_set}` | M | Names the gate; MUST NOT be a generic tool permission. |
| `dependency_snapshot` | `Snapshot` (§2.2) | M | Absent snapshot ⇒ `invalid`. |
| `granted_at` | `ts` | M | |
| `status` | `enum{active, stale, invalidated, superseded}` | M | Lifecycle in §3. |
| `rationale` | `string` | M | Non-empty. |

Invariants:

- Approvals bind to immutable artifact revisions and dependencies, not to
  filenames or generic tool permissions.
- A saved generic (agent) tool permission MUST NOT satisfy an
  outline/pilot/exclusion/gate approval.
- Agents — and the application worker acting on untrusted instructions — MUST
  NOT self-approve, widen `scope`, or edit authoritative state.

### 2.2 `Snapshot` — exact immutable dependency snapshot

| Field | Type | Presence | Invariant |
| --- | --- | --- | --- |
| `snapshot_id` | `id` | M | |
| `entries` | `list<SnapshotEntry>` | M | Non-empty. |
| `versions` | `VersionSet` | M | Model/input/rubric/parser/adapter versions in force. |

`SnapshotEntry`:

| Field | Type | Presence | Invariant |
| --- | --- | --- | --- |
| `artifact_id` | `id` | M | |
| `revision_id` | `id` | M | Immutable. |
| `digest` | `digest` | M | Content digest of that revision. |
| `digest_alg` | `enum{sha256}` | M | Algorithm named explicitly. |
| `role` | `enum{source_document, summary, finding, rubric, framework, outline, parser_output, model_output, other}` | M | |

Invariant: the snapshot is **exact** — it enumerates each dependency by
`artifact_id` + `revision_id` + `digest`, never by name or path. A dependency
whose current revision/digest differs from an entry makes the bound approval
`stale` (revision changed) or `invalidated` (digest mismatch).

## 3. Stale and invalidated approvals

| Trigger | Resulting `status` | Effect |
| --- | --- | --- |
| Any dependency `revision_id` changes (new evidence/revision, spec line 33) | `stale` | Dependent stage `blocked` until re-approval; unaffected work preserved. |
| Stored `digest` ≠ recomputed digest | `invalidated` | Approval not honored. |
| Declared-material change to rubric / framework / parser / model / policy | `stale` | "Material rubric/model/parser changes reopen relevant pilot approval" (spec line 30). |
| New artifact revision replaces the approved one | `stale` | The prior approval does not carry forward to the new revision. |
| A newer approval for the same gate+target exists | `superseded` | Older approval retained for audit, not honored. |

Invariants:

- A `stale`/`invalidated`/`superseded` approval MUST NOT authorize execution or
  export.
- Re-approval creates a **new** `Approval` record bound to a new `Snapshot`; it
  MUST NOT mutate the old record's `status` back to `active` in place.
- "An approved artifact edited afterward becomes a proposal and invalidates
  affected checks" (step 02 acceptance); the edited object is a `Proposal`, not
  an approved revision.

## 4. Findings and issue lifecycle

### 4.1 `Issue`

| Field | Type | Presence | Invariant |
| --- | --- | --- | --- |
| `issue_id` | `id` | M | |
| `target` | `ref` | M | The exact revision reviewed. |
| `round_id` | `id` | M | Links to a `Round` (§5.2). |
| `raised_by` | `Actor` | M | Critic/author actor; provenance explicit. |
| `claim` | `string` | M | The objection or finding. |
| `evidence` | `EvidenceRef` | M* | |
| `missing_check` | `MissingCheck` | M* | |
| `severity` | `enum{blocking, major, minor, style}` | M | **Proposed taxonomy — see §4.4; unapproved.** |
| `state` | `enum{open, in_progress, fixed, rejected_with_reason, deferred, superseded}` | M | |
| `disposition_reason` | `string` | O | Mandatory (non-empty) when `state ∈ {fixed, rejected_with_reason, deferred}`. |
| `fix_ref` | `ref` | O | Mandatory when `state = fixed`; a fix MUST point at a new revision. |

Invariants:

- **M\*** union: at least one of `evidence` or `missing_check` MUST be present.
  If both are absent the `Issue` is `invalid` and MUST NOT be dispositioned.
- A finding needs **evidence or a concrete missing check**, a severity, and an
  explicit fixed/rejected-with-reason disposition.
- `state = fixed` without `fix_ref` ⇒ `invalid`.
- A **rejected** false objection MUST carry `disposition_reason`; rejecting an
  objection does **not** oblige a rewrite (see §5.3).
- Faithful scientific uncertainty is not a defect to erase; it is recorded, not
  "fixed" into false confidence.
- A summary, real quote, file hash, model probability, or human approval alone
  does **not** establish scientific correctness or completeness, and MUST NOT be
  recorded as if it did.

### 4.2 `EvidenceRef`

| Field | Type | Presence | Invariant |
| --- | --- | --- | --- |
| `kind` | `enum{quote, locator, numeric, digest, missing_asset}` | M | |
| `artifact_revision` | `ref` | M | Exact revision the evidence points into. |
| `locator` | `string` | M | Page/section/offset or equivalent. |
| `excerpt_digest` | `digest` | O | Optional corroboration only. |

### 4.3 `MissingCheck`

| Field | Type | Presence | Invariant |
| --- | --- | --- | --- |
| `check_id` | `id` | M | |
| `description` | `string` | M | The concrete check that would settle the issue. |
| `would_settle` | `bool` | M | |

### 4.4 Severity taxonomy — PROPOSED, requires user approval

The literal set `{blocking, major, minor, style}` above is a **proposal for user
review**; the specification confirms behavior (confirmed major
scientific defects/blockers prevent completion; minor/style findings may remain;
faithful uncertainty is not a defect) but does **not** fix these exact labels or
any counts. Until ratified:

- The taxonomy field is `severity` with `enum`, but its accepted literal set is
  **unresolved**; an unratified literal set blocks any behavior that keys on
  severity (fail closed).
- No numeric threshold, count, or probability is invented here to let a stage
  pass.
- Only `blocking`/`major` **confirmed** defects prevent completion; the mapping
  from observations to those classes is part of the pending taxonomy decision.

## 5. Review budgets and round identity

### 5.1 `ReviewBudget`

| Field | Type | Presence | Invariant |
| --- | --- | --- | --- |
| `budget_lineage_id` | `id` | M | Stable across edits (§5.3). |
| `project_id` | `id` | M | Per-project; never shared across projects. |
| `stage` | `enum{section_critique, original_source_fact_check, manuscript_review}` | M | |
| `limit` | `int` | M | **Inherited confirmed baseline** (§5.4). |
| `consumed_rounds` | `int` | M | Monotonic within the lineage. |
| `remaining` | `int` | M | Derived `= limit − consumed_rounds`; never negative. |

### 5.2 `Round`

| Field | Type | Presence | Invariant |
| --- | --- | --- | --- |
| `round_id` | `id` | M | |
| `budget_lineage_id` | `id` | M | |
| `stage` | `enum{...}` | M | Same enum as §5.1. |
| `findings_pass` | `FindingsPass` | M | Exactly one findings pass per round. |
| `repair_pass` | `RepairPass` | O | Absent when no repair was needed (early success). |
| `read_only` | `bool` | M | `true` for final read-only validation (§5.5). |

`FindingsPass`: `{ pass_id: id M, issues: list<Issue> M, produced_by: Actor M }`.
`RepairPass`: `{ pass_id: id M, target_ref_before: ref M, target_ref_after: ref M,
resolved_issues: list<id> M, model_generated: bool M }`.

A round is **one findings pass plus one repair pass if needed** (spec line 56).

### 5.3 Budget lineage is not reset by edits

Invariants:

- Creating or editing an artifact revision (including a model-generated revision)
  MUST NOT change `budget_lineage_id`, reset `consumed_rounds`, or increment
  `limit`. New model-generated draft revisions do **not** replenish exhausted
  repair budgets (spec line 55).
- Only an explicit, human-authenticated decision creates a new lineage or
  extends `limit`; that decision is recorded as an `Approval`/`Actor(human)`,
  never as a `Proposal`.
- A rejected false objection (`state = rejected_with_reason`) does **not** force a
  rewrite: the round may close with `repair_pass` absent or with no edits, and
  the reason is retained.

### 5.4 Inherited baseline limits (confirmed, not invented)

| `stage` | `limit` | Source |
| --- | --- | --- |
| `section_critique` | **3** | spec line 38 "Maximum **3** critique/repair rounds"; step 12 line 12. |
| `original_source_fact_check` | **2** | spec line 39 "maximum **2** rounds"; step 12 line 12. |
| `manuscript_review` | **2** | spec line 40 "Maximum **2** repair rounds". |

These three values are the confirmed baseline inherited from the sources above;
they are not this contract's invention and MUST NOT be exceeded without a user
decision.

### 5.5 Exhaustion and read-only validation

- `remaining = 0` ⇒ explicit `blocked`/human escalation; MUST NOT pass
  unresolved confirmed factual blockers or major scientific defects (spec line
  56).
- Final read-only validation adds **no** repair: such operations have
  `read_only = true`, MUST NOT create a `repair_pass`, and MUST NOT increment
  `consumed_rounds`. A repair requested by final validation MUST route back to an
  existing authorized stage, never open an unlimited terminal edit loop
  (spec line 42).
- Model-created revisions MUST NOT reset or bypass any budget; a worker MUST NOT
  schedule further model calls once a budget is exhausted or work is paused.

## 6. Machine decisions versus human decisions

### 6.1 `VersionSet` — persisted versions

| Field | Type | Presence | Invariant |
| --- | --- | --- | --- |
| `model_id` | `string` | M | |
| `prompt_digest` | `digest` | M | |
| `input_digest` | `digest` | M | |
| `rubric` | `VersionedRef` | O | Mandatory when the stage scores/classifies. |
| `parser` | `VersionedRef` | O | Mandatory when the stage consumes parsed source. |
| `adapter` | `VersionedRef` | O | Mandatory for Jev/typed-decision stages. |
| `route` | `enum{opencode_session, direct_call}` | M | |

`VersionedRef`: `{ version: string M, digest: digest M }`.

Invariant: for each input the stage actually consumed, the corresponding
version+digest MUST be present and persisted with the decision; an absent applied
version makes the record `invalid` and the dependent behavior `blocked`.

### 6.2 `DecisionRecord` (typed Jev/stateless decision)

| Field | Type | Presence | Invariant |
| --- | --- | --- | --- |
| `decision_id` | `id` | M | |
| `question_id` | `string` | M | |
| `target` | `ref` | M | |
| `classification` | `enum{yes, no, uncertain}` | M | |
| `score` | `number` | O | Legend used with scores. |
| `legend` | `map<string,string>` | O | |
| `outcome` | `enum{answered, partial_error, uncertain, omitted, malformed}` | M | |
| `per_question_error` | `PerQuestionError` | O | |
| `versions` | `VersionSet` | M | `adapter` mandatory for Jev. |
| `route` | `enum{opencode_session, direct_call}` | M | |
| `cost` | `CostRecord` | O | **Mandatory when `route = direct_call`.** |

`PerQuestionError`: `{ code: string M, question_id: string M, message_redacted: string M }`.

**Never-exclusion invariant (critical).** When `outcome ∈ {partial_error,
uncertain, omitted, malformed}`, or a `per_question_error` is present, or the
model/provider produced an error/refusal/invalid shape, the application:

- MUST NOT convert it into a screening exclusion (spec line 31) or a scientific
  exclusion;
- MUST NOT treat it as a negative finding or a negation of the question's
  premise;
- MUST route it to `uncertain` retention or human review.

An uncertain or errored decision is **never** an exclusion, and an omitted output
is **never** silently dropped.

### 6.3 `CostRecord` — direct-call cost separation

| Field | Type | Presence | Invariant |
| --- | --- | --- | --- |
| `amount_minor` | `int` | M | Integer minor units. |
| `currency` | `enum{USD}` | M | |
| `basis` | `enum{provider_reported, estimated}` | M | |
| `route` | `enum{opencode_session, direct_call}` | M | |
| `accounting_bucket` | `enum{session_statistics, direct_call}` | M | |

Invariant: costs from `direct_call` routes MUST NOT be aggregated into OpenCode
session statistics; direct calls are accounted separately (spec line 20
fallback; step 04 line 22; ADR `001` "direct calls … accounted separately").

## 7. Secondary-origin attribution and warned tracing

### 7.1 `OriginAttribution`

| Field | Type | Presence | Invariant |
| --- | --- | --- | --- |
| `attribution_id` | `id` | M | |
| `claim_ref` | `ref` | M | The exact claim revision. |
| `depth` | `int` | M | Depth 0 is the currently cited source. |
| `origin_class` | `enum{primary_verified, secondary_warned, mixed, unclear, own_result, conceptual_contribution}` | M | |
| `source_ref` | `ref` | M | The source **actually read**; never an unread original. |
| `limit_state` | `enum{none, hop_limit, source_limit, retry_limit, unavailable}` | M | |
| `warning_visible` | `bool` | M | |
| `approval_required` | `enum{none, human_final}` | M | |

### 7.2 Invariants

- When `limit_state ≠ none` or `origin_class = secondary_warned`, then
  `warning_visible` MUST be `true`, `approval_required` MUST be `human_final`,
  and `origin_class` MUST NOT be `primary_verified`.
- Supported secondary attribution stays a **warned** state requiring final human
  approval, **never** "verified primary support" (spec line 50; step 13 line 20;
  ADR `001`; kickoff "At a tracing limit, secondary attribution stays a warned
  state requiring final human approval").
- Cite the source actually read; never substitute an unread original or claim
  direct verification. The exception does not permit wording unsupported even by
  the secondary source.
- Section/manuscript checking MUST NOT relabel a real citation to a secondary
  source as verified primary support (step 12 line 21).
- Export/final approval is `blocked` until a human final approval is recorded (an
  `Approval`, §2.1, with the matching `scope`).
- Depth 0 is the currently cited source; trace direct support, not earliest
  discovery, and do not recurse every bibliography indiscriminately (step 13).
- **Numeric hop/source/retry defaults are unresolved** (owner: user; spec line
  48 "Numerical defaults are **not yet agreed**"). Until set, tracing stops at a
  configured limit and is `blocked` there; no number is invented. These are not
  the writer's repair budgets (§5).

## 8. Runtime safety

### 8.1 `AdmissionReceipt` and admission vs completion

| Field | Type | Presence | Invariant |
| --- | --- | --- | --- |
| `admission_id` | `id` | M | Client-supplied idempotency key. |
| `project_id` | `id` | M | |
| `route` | `enum{opencode_session, direct_call}` | M | |
| `submitted_at` | `ts` | M | |
| `has_outcome` | `bool` | M | MUST be `false` (the receipt is durable, never a completion). |

Invariants:

- Retrying the same `admission_id` MUST produce one accepted input; a durable
  receipt is **not** completion (step 00 criterion 2).
- **Admission ≠ completion.** Only an authoritative terminal (`idle`/explicit
  outcome) completes a turn; a receipt or a partial assistant MUST NOT be read
  as success.

### 8.2 Exactly-once boundary

- The application prevents duplicate **accepted local results** via
  reconciliation and fencing of accepted writes, and MUST NOT claim
  exactly-once **external** execution or billing (step 03 acceptance;
  README "Retries and reconciliation prevent duplicate accepted local results
  without claiming exactly-once external execution or billing").
- **Fencing** protects accepted writes against stale attempts; it does **not**
  fence arbitrary external side effects that already occurred.

### 8.3 `Job` states and ambiguous remote work

`Job`: `{ job_id: id M, project_id: id M, attempt_id: id M, admission: AdmissionReceipt O,
state: enum{queued, admitted, running, waiting_human, waiting_external, paused,
cancelled, completed, failed, interrupted, unknown, reconciliation_required} M }`.

Invariants:

- When the remote outcome cannot be established from durable state, the job state
  MUST be `unknown` or `reconciliation_required` (step 00 restart finding: an
  in-flight turn torn down by a service restart reconciles to an explicit
  unknown/interrupted, never a silent success).
- An ambiguous external job MUST NOT be re-executed with a new identity; the
  application MUST reconcile from durable state or escalate to a human (step 03:
  "Never blindly resend ambiguous external work with a new identity").
- A stale attempt MUST NOT overwrite a newer authorized result.
- Lost event streams MUST be reconciled from durable state/messages; missing
  events MUST NOT lose a completion or approval action (step 04).
- `paused` / budget-exhausted work MUST NOT schedule new model calls, and MUST
  NOT busy-loop while waiting for approval.

### 8.4 Pause, cancel, and detached CLI

| Action | Semantics | Invariant |
| --- | --- | --- |
| Close CLI (detached) | CLI lifetime ends; authorized background job continues. | Closing the CLI does NOT cancel an authorized job (README global DoD). |
| `pause` | Stop scheduling new work; preserve durable state; resumable. | Not terminal. |
| `cancel` | Terminal; no further calls scheduled. | Cancel cannot retract external side effects already executed; record `cancelled`, not `completed`. |

A terminal disconnect is distinct from a `pause` request (README "Proposed CLI
concepts"). Non-owner control MUST be rejected; no root or public unauthenticated
listener is required (step 03).

### 8.5 Project activation and safe switching

`ProjectActivation`: `{ active_project_id: id M, activation_token: id M,
state: enum{none, active, draining, switching} M }`.

Invariants:

- **At most one** active project may execute at once; a second concurrent
  activation attempt MUST be rejected (step 03 acceptance).
- A `switch` MUST `drain`/reconcile the old project's in-flight admitted work
  before another project executes (README: a safe project switch must reconcile
  or drain existing work before another project executes).
- Old-project jobs MUST NOT commit into the new project; per-project approvals
  and budgets are never shared (README global DoD).
- If the old project's job state is unknown, the switch is `blocked` pending
  reconciliation (§8.3), not silently completed.

## 9. Missing and unknown states (fail closed)

- Absent mandatory field ⇒ record `invalid`; dependent behavior `blocked`.
- Unrecognized enum literal ⇒ `invalid`; never coerced to a default.
- Unknown dependency revision/digest ⇒ approval `stale`/`invalidated`;
  stage `blocked`.
- Unknown remote outcome ⇒ `unknown`/`reconciliation_required`; never
  re-executed, never `completed`.
- `unresolved` limits (e.g., origin hop/source/retry defaults) block the
  dependent behavior rather than being guessed.

## 10. Rights, privacy, and untrusted data (boundary only)

Rights, privacy, hosted-model transfer, redistribution, and provider/journal
policy are **out of scope** here and are reserved for the planned policy
contract `docs/contracts/03-policy-decisions.md` (absent in this checkout;
planned path referenced without a link). This contract only fixes that:

- Documents and model outputs are untrusted data; no arbitrary source
  instructions may widen scope, grant approvals, or edit authoritative state.
- No hidden approvals; no public unauthenticated services; no credential
  leakage.
- Fixtures and logs MUST NOT contain secrets or licensed source text (spec lines
  62–63; step 00 "no secrets or licensed full text").

## 11. Worked examples

All examples are **synthetic** and illustrative of record shape and verdict only;
they assert no scientific result. `verdict` is this contract's own validity
judgement (valid / invalid / blocked), not a scientific conclusion.

### 11.1 Uncertain Jev decision

**Valid.** A screening decision for `{proj_A, candidate_17, rev_3}` returns
`outcome = uncertain`, `classification = uncertain`, `versions.adapter =
{jev-1.14-20261014, sha256:…}`, `route = direct_call`, `cost = {…, accounting_bucket:
direct_call}`. Application keeps the candidate in the retained-uncertain set and
routes to human review. It is **not** an exclusion. → **verdict: valid.**

**Invalid.** Same record but `outcome = answered`, `classification = no`, and the
setter also records `excluded = true` while `classification` is unresolved /
`outcome = uncertain`. Converting uncertainty or a per-question error into an
exclusion violates §6.2. → **verdict: invalid.**

**Missing.** `classification` absent (or `outcome` absent). → **verdict: invalid
(blocked)** — no exclusion and no retention may be written.

### 11.2 False critic objection

**Valid.** Critic raises `issue_88` on `{proj_A, draft_sec3, rev_5}`: `claim =
"paragraph 2 changes the population from mice to rats"`, `evidence = {kind:
locator, artifact_revision: {proj_A, draft_sec3, rev_5}, locator: "p2, L4"}`,
`severity = major` (proposed). The author checks the source and both revisions;
the objection is false. Disposition: `state = rejected_with_reason`,
`disposition_reason = "rev_5 p2 already reads 'mice'; critic cited rev_4"`.
`repair_pass` absent; budget lineage unchanged. → **verdict: valid.**

**Invalid.** `state = fixed` with no `fix_ref` (no new revision). → **verdict:
invalid.** Also invalid: `state = rejected_with_reason` with an empty
`disposition_reason`, or an `Issue` with neither `evidence` nor `missing_check`.

**Missing.** `severity` absent, or both `evidence` and `missing_check` absent. →
**verdict: invalid (blocked)** — cannot be dispositioned; severity taxonomy is
also unapproved until the user ratifies it (§4.4).

### 11.3 Stale approval / model-edited budget

**Valid (stale).** `Approval{gate=pilot, target={proj_A, jev_rubric, rev_3},
dependency_snapshot=S_7}` becomes `stale` when the rubric is revised to `rev_4`;
the pilot stage is `blocked` until a new approval on a new snapshot. Unaffected
extraction work is preserved.

**Valid (budget lineage).** The model regenerates `draft_sec3 rev_6`;
`budget_lineage_id = L_sec` and `consumed_rounds = 3`/`remaining = 0` are
unchanged. The exhausted budget does not replenish. → **verdict: valid; further
critique calls blocked.**

**Invalid.** A `Proposal` (model-generated) changes `budget_lineage_id` to `L_new`
or sets `consumed_rounds = 0` without a human-authenticated decision. → **verdict:
invalid.**

**Missing.** `dependency_snapshot` absent on the `Approval`, or `digest_alg`
absent on a `SnapshotEntry`. → **verdict: invalid (blocked).**

### 11.4 Secondary-origin warning

**Valid.** Origin trace for claim `{proj_A, claim_12, rev_2}` reaches a hop limit:
`origin_class = secondary_warned`, `source_ref = {proj_A, source_41, rev_1}` (the
source actually read), `limit_state = hop_limit`, `warning_visible = true`,
`approval_required = human_final`. The manuscript keeps a visible warning and
cannot export until a human final approval is recorded. → **verdict: valid.**

**Invalid.** `origin_class = primary_verified` while `limit_state = hop_limit`, or
the citation is replaced with an unread original / claims direct verification.
Violates spec line 50 / step 13 line 20. → **verdict: invalid.**

**Missing.** `origin_class` absent, or `limit_state` absent. → **verdict: invalid
(blocked)** — no primary claim may be emitted. Numeric hop/source/retry defaults
remain **unresolved** (owner: user); until set, tracing stops at a configured
limit and blocks rather than inventing a number.

### 11.5 Interrupted remote work

**Valid.** A turn was in flight during a service restart. `AdmissionReceipt`
exists with `has_outcome = false`; no authoritative terminal was written. `Job.state
= unknown`; the application reconciles from durable state, finds no terminal, and
escalates to a human. The work is **not** re-executed under a new identity. →
**verdict: valid.**

**Invalid.** `Job.state = completed` inferred from a partial `assistant
(finish: error)` message; or the application re-submits the work with a fresh
`admission_id` silently. → **verdict: invalid.**

**Missing.** No `AdmissionReceipt` and no durable attempt record. → **verdict:
blocked** — reconciliation is impossible; human disposition required.

### 11.6 Project switch

**Valid.** With project A active, a switch sets `state = draining`; project A's
in-flight admitted work is reconciled/quiesced; only then does project B activate
with a new `activation_token`. No A job commits after the switch; budgets and
approvals stay per-project. → **verdict: valid.**

**Invalid.** Activating project B while project A still has a committing job; or
a second activation attempt executing concurrently (two active projects); or a
shared budget/approval record reused across A and B. → **verdict: invalid.**

**Missing.** `active_project_id` absent, or the old project's job state unknown at
switch time. → **verdict: blocked** pending reconciliation.

## 12. Open decisions (owners) and dependency notes

| Item | State | Owner | Blocking effect |
| --- | --- | --- | --- |
| Severity taxonomy literal set (§4.4) | Proposed / unapproved | User | Any behavior keyed on severity is blocked (fail closed). |
| Origin hop/source/retry numeric defaults (§7.2) | Unresolved | User | Trace stops at configured limit and blocks; no invented number. |
| Cost/concurrency limits | Unresolved | User | Budget enforcement uses the inherited round limits; cost/concurrency caps blocked until set. |
| Pilot/audit samples and acceptance rules; coverage/stopping rules; explicit acceptance of secondary-origin warnings | Unresolved (spec "Pending decisions") | User | Dependent gates blocked. |
| Rights/hosted-model policy | Deferred to `docs/contracts/03-policy-decisions.md` (absent) | User | Hosted-model access not authorized by this contract. |
| Durable storage schema / IPC / package layout | Out of scope here (step 02; SQLite remains a proposal) | Step 02 + user | No schema decision is made or implied. |

## 13. Change history

| Version | Date | Change | Status |
| --- | --- | --- | --- |
| `v1` | 2026-10-27 | Initial proposed representation of gates, approvals, findings budgets, machine decisions, and runtime safety, with worked examples. | **Proposed — approval pending.** |
