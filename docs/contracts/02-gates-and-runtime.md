# Contract 02 — Gates, approvals, findings budgets, and runtime safety

## Status and authority

- **Contract version:** `v2` (draft representation, dated 2026-10-27).
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
  layout are a **step-01 deliverable proposal** (`01-contracts.md` line 12),
  recorded here as proposed, not as an accepted schema; the SQLite schema remains
  a proposal (`docs/specification.md` line 16). Neither is decided by this
  document, and neither is categorically a step-02-only decision.
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
- **Sibling identity contract:** artifact references use the immutable reference
  tuple `{project_id, artifact_id, revision_id}` defined by the sibling
  identity-and-evidence contract produced alongside this document in step 01.
  That tuple is assumed, not redefined here.
- **Sibling policy contract:** rights, privacy, hosted-model transfer, and
  provider/journal policy are defined by the sibling policy contract
  [`03-policy-decisions.md`](03-policy-decisions.md), produced in the coordinated
  contract set (C04). This document does not define that policy; the link
  resolves once the sibling contract lands.

## Notation and typing rules

| Token | Meaning |
| --- | --- |
| `string` | UTF-8 text. |
| `id` | Opaque, stable, non-empty application-assigned identifier. |
| `ref` | Immutable artifact reference `{project_id, artifact_id, revision_id}` (sibling contract). |
| `enum{...}` | Exactly one listed literal; an unrecognized value is **invalid** (fail closed, never coerced). |
| `ts` | UTC timestamp, ISO-8601, with explicit offset. |
| `digest` | Canonical content digest: lowercase hex **SHA-256**, the single algorithm for every digest in this contract. There is no per-field algorithm field. |
| `decimal` | Exact base-10 amount encoded as a string (never a JSON float); preserves provider precision. |
| `list<T>` | Ordered sequence of `T`. |
| `map<K,V>` | Key→value mapping with `K`/`V` as named types. |
| `bool`, `int`, `number` | JSON boolean / integer / number. |
| `M` | Mandatory. **If absent, the containing record is `invalid` and every behavior depending on it is `blocked`.** |
| `O` | Optional; absence is itself a defined state, not silently defaulted. |
| `M*` | Mandatory-union: at least one member of a named set must be present. |

Invariant shorthand used below: **MUST**, **MUST NOT**, and **`invalid`/`blocked`**
are normative for this proposed contract. No numeric scientific threshold may be
invented to let a stage pass; where a limit is unresolved it is named as
`unresolved` (runtime: `configuration_required`) and blocks the dependent
behavior.

## 1. Actors, provenance, and proposals

### 1.1 `Actor`

| Field | Type | Presence | Invariant |
| --- | --- | --- | --- |
| `actor_kind` | `enum{human, model, system}` | M | `system` = the application worker, never an approver. |
| `actor_id` | `id` | M | Stable application identity. |
| `authority` | `enum{owner_authenticated, none}` | M | Authority is **not** self-declared: `owner_authenticated` MUST be produced by the application's out-of-band owner-auth check (§1.3) and MUST NOT be derived from any model, tool, document, or request field. |
| `owner_auth_ref` | `id` | O | Mandatory when `authority = owner_authenticated`; references the out-of-band validated owner-auth record. |
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
| `payload_digest` | `digest` | M | Canonical SHA-256 of the payload (same algorithm as every digest here). |

Invariants:

- A `Proposal` MUST NOT substitute for an `Approval`. A model edit MUST NOT
  create, widen, or replenish any approval or budget.
- A `Proposal` MUST NOT construct authority: it cannot set `actor_kind = human`,
  set `authority = owner_authenticated`, mint an `owner_auth_ref`, or create an
  `Approval` (§1.3).

### 1.3 Authority provenance (out-of-band)

- Human approval authority is established by the application's owner-auth check
  performed **outside** the model/tool data path; it is never taken from a
  record that a model, tool, retrieved document, or request payload can author.
- A `Proposal`, tool result, retrieved document, or request field MUST NOT set
  `actor_kind = human`, set `authority = owner_authenticated`, or mint an
  `owner_auth_ref`; attempting so makes the record `invalid` and MUST be
  rejected.
- `authority = owner_authenticated` without a matching validated
  `owner_auth_ref` ⇒ `invalid`.
- A plain boolean "authenticated" flag supplied in-band MUST NOT be trusted as
  authority.

## 2. Revision-specific approvals

### 2.1 `Approval` (human)

| Field | Type | Presence | Invariant |
| --- | --- | --- | --- |
| `approval_id` | `id` | M | |
| `granted_by` | `Actor` | M | `actor_kind` MUST be `human` and `authority` MUST be `owner_authenticated` (validated out-of-band, §1.3). |
| `project_id` | `id` | M | Project scope; MUST equal `target.project_id` and the snapshot `project_id`. |
| `target` | `ref` | M | Exact `{project_id, artifact_id, revision_id}`. |
| `decision` | `enum{approved, rejected}` | M | |
| `gate_kind` | `enum{protocol, pilot, outline, framework, exclusion_set, artifact_revision, batch, final_release}` | M | Names the actual gate type; `pilot` and `outline` are distinct. MUST NOT be a generic tool permission. |
| `gate_id` | `id` | M | The specific gate instance approved (distinguishes e.g. a pilot gate from an outline gate at the same stage). |
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
  NOT self-approve, widen the gate, or edit authoritative state.
- The approval `project_id`, `target.project_id`, snapshot `project_id`, and
  every `SnapshotEntry.project_id` MUST all be the **same** project; any
  mismatch (a cross-project entry) denies the approval (`invalid`).

### 2.2 `Snapshot` — exact immutable dependency snapshot

| Field | Type | Presence | Invariant |
| --- | --- | --- | --- |
| `snapshot_id` | `id` | M | |
| `project_id` | `id` | M | The single project all entries belong to. |
| `entries` | `list<SnapshotEntry>` | M | Non-empty. |
| `versions` | `VersionSet` | M | Persisted input/model/rubric/parser/adapter versions in force (§6.1). |

`SnapshotEntry`:

| Field | Type | Presence | Invariant |
| --- | --- | --- | --- |
| `project_id` | `id` | M | MUST equal the snapshot `project_id`; a foreign project ⇒ `invalid`. |
| `artifact_id` | `id` | M | |
| `revision_id` | `id` | M | Immutable. |
| `digest` | `digest` | M | Canonical SHA-256 content digest of that revision. |
| `role` | `enum{source_document, summary, finding, rubric, framework, outline, parser_output, model_output, other}` | M | |

Invariant: the snapshot is **exact** — it enumerates each dependency by
`project_id` + `artifact_id` + `revision_id` + canonical `digest`, never by name
or path, and all entries share one `project_id` (a cross-project entry denies the
snapshot/approval). A dependency whose current revision/digest differs from an
entry makes the bound approval `stale` (revision changed) or `invalidated`
(digest mismatch).

## 3. Stale and invalidated approvals

| Trigger | Resulting `status` | Effect |
| --- | --- | --- |
| Any dependency `revision_id` changes (new evidence/revision, spec line 33) | `stale` | Dependent stage `blocked` until re-approval; unaffected work preserved. |
| Stored `digest` ≠ recomputed digest | `invalidated` | Approval not honored. |
| Declared-material change to rubric / framework / parser / model / policy | `stale` | "Material rubric/model/parser changes reopen relevant pilot approval" (spec line 30). |
| New artifact revision replaces the approved one | `stale` | The prior approval does not carry forward to the new revision. |
| A newer approval for the same `gate_kind` + `gate_id` + target exists | `superseded` | Older approval retained for audit, not honored. |
| Any project-identity mismatch (approval vs target vs snapshot vs entry) | `invalidated` | Approval denied. |

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
| `disposition_reason` | `string` | O | Mandatory (non-empty) when `state ∈ {fixed, rejected_with_reason, deferred, superseded}`. |
| `fix_ref` | `ref` | O | Mandatory when `state = fixed`; a fix MUST point at a new revision. |
| `successor_issue_id` | `id` | O | Mandatory when `state = superseded`; MUST reference a successor issue that is itself resolved (`fixed`/`rejected_with_reason`). |

Invariants:

- **M\*** union: at least one of `evidence` or `missing_check` MUST be present.
  If both are absent the `Issue` is `invalid` and MUST NOT be dispositioned.
- A finding needs **evidence or a concrete missing check**, a severity, and an
  explicit fixed/rejected-with-reason disposition.
- `state = fixed` without `fix_ref` ⇒ `invalid`.
- A **rejected** false objection MUST carry `disposition_reason`; rejecting an
  objection does **not** oblige a rewrite (see §5.3).
- `state ∈ {superseded, deferred}` MUST NOT close a confirmed `blocking` or
  `major` issue: such an issue stays **unresolved** unless a validated resolved
  successor exists (`superseded` with a resolved `successor_issue_id`) or an
  explicit human disposition is recorded (`Actor` with `authority =
  owner_authenticated`). `state = superseded` without a resolved
  `successor_issue_id` is still unresolved, not closed.
- An unresolved `blocking`/`major` issue makes the **final gate reject**
  (`blocked`), never pass.
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
review**; the specification confirms behavior (confirmed major scientific
defects/blockers prevent completion; minor/style findings may remain; faithful
uncertainty is not a defect) but does **not** fix these exact labels or any
counts. Until ratified:

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
| `scope_id` | `id` | M | The **section/stage instance** the lineage is scoped to (e.g., the section artifact or stage instance). Two sections never share one lineage. |
| `stage` | `enum{section_critique, original_source_fact_check, manuscript_review}` | M | |
| `limit` | `int` | M | **Inherited confirmed baseline** (§5.4); not extendable by an `Approval` (§5.3). |
| `consumed_rounds` | `int` | M | Monotonic; incremented **atomically, exactly once** per completed `Round` (§5.2). |
| `remaining` | `int` | M | Derived `= limit − consumed_rounds`; never negative. |
| `state` | `enum{open, ready_for_validation, blocked, exhausted}` | M | See §5.5. |

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

Invariants:

- `consumed_rounds` increments **atomically and exactly once** per completed
  `Round` (a single transaction keyed by `round_id`); a retried or
  crash-recovered round MUST NOT double-increment.
- Final read-only validation is **not** a budgeted round: it has no
  `findings_pass`/`repair_pass` and MUST NOT increment `consumed_rounds` (§5.5).

### 5.3 Budget lineage is not reset by edits

Invariants:

- Creating or editing an artifact revision (including a model-generated revision)
  MUST NOT change `budget_lineage_id`, reset `consumed_rounds`, or increment
  `limit`. New model-generated draft revisions do **not** replenish exhausted
  repair budgets (spec line 55).
- A `Proposal` (model output) MUST NOT create a new lineage, reset
  `consumed_rounds`, or raise `limit`.
- A generic human `Approval` MUST NOT extend the baseline maxima (§5.4) or reset
  a lineage: extending a maximum requires an explicit specification change
  recorded outside the normal approval flow. A new lineage is only for a new
  `scope_id` (a genuinely new section/stage instance), never a mid-episode reset.
- A lineage MUST NOT be reset during the same editing/repair episode: no
  operation (model or human `Approval`) resets `consumed_rounds` or starts a new
  lineage within one episode.
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
they are not this contract's invention. They MUST NOT be exceeded, and a generic
`Approval` MUST NOT extend them or reset a lineage to add rounds; raising a
baseline maximum requires an explicit specification change (a user decision
recorded in the baseline, §5.3).

### 5.5 Exhaustion, readiness, and read-only validation

- When `remaining = 0` and the round's repair resolved **all** confirmed
  blockers/major defects and a read-only validation result is clear, the budget
  state MAY be `ready_for_validation` — preparation for the (unbudgeted) final
  read-only validation, **not** an automatic success or completion.
- When `remaining = 0` with any unresolved confirmed factual blocker or major
  scientific defect, the budget state is `blocked` with human escalation (spec
  line 56). **Exhaustion blocks further repair passes; it does not automatically
  pass or complete the stage.**
- Final read-only validation adds **no** repair and is **not** a budgeted round:
  it has no `findings_pass`/`repair_pass`, and MUST NOT increment
  `consumed_rounds`. A repair requested by final validation MUST route back to an
  existing authorized stage, never open an unlimited terminal edit loop (spec
  line 42).
- Model-created revisions MUST NOT reset or bypass any budget; a worker MUST NOT
  schedule further model calls once a budget is `blocked`/`exhausted` or work is
  paused.

## 6. Machine decisions versus human decisions

### 6.1 `VersionSet` — persisted versions

| Field | Type | Presence | Invariant |
| --- | --- | --- | --- |
| `context_kind` | `enum{model_derived, human_authored, mixed}` | M | Human-only approvals use `human_authored`. |
| `input_digest` | `digest` | M | Canonical SHA-256 of the inputs in force (always present). |
| `model` | `VersionSlot` | M | MUST be `known` when `context_kind ∈ {model_derived, mixed}`; MUST be `not_applicable` when `human_authored`. |
| `prompt` | `VersionSlot` | M | Same rule as `model`. |
| `rubric` | `VersionSlot` | M | `known` for stages that score/classify; otherwise `not_applicable`/`unknown`. |
| `parser` | `VersionSlot` | M | `known` for stages that consume parsed source; otherwise `not_applicable`/`unknown`. |
| `adapter` | `VersionSlot` | M | MUST be `known` for Jev/typed-decision stages. |
| `route` | `enum{opencode_session, direct_call, not_applicable}` | M | |

`VersionSlot`: `{ state: enum{known, unknown, not_applicable} M, ref: VersionedRef O }`
— `ref` is mandatory iff `state = known`, and MUST be absent otherwise.
`VersionedRef`: `{ version: string M, digest: digest M }`.

Invariants:

- No fake or placeholder identifier. A `known` slot MUST carry a real
  `version`+`digest`; an unknown applied version is `state = unknown` (which
  blocks the dependent behavior), never a fabricated ID.
- A human-only approval sets `context_kind = human_authored` with `model` and
  `prompt` as `not_applicable` — it MUST NOT invent a model ID to fill the slot.
- For each input the stage actually consumed, the corresponding slot MUST be
  `known` and persisted with the decision; an absent applied version makes the
  record `invalid` and the dependent behavior `blocked`.

### 6.2 `DecisionRecord` (typed Jev/stateless decision)

| Field | Type | Presence | Invariant |
| --- | --- | --- | --- |
| `decision_id` | `id` | M | |
| `question_id` | `string` | M | |
| `question_kind` | `enum{predicate, choice, score, box}` | M | The typed question/answer union of the accepted Jev route. |
| `target` | `ref` | M | |
| `answers` | `list<AnswerUnion>` | O | Present iff `outcome ∈ {answered, partial_error}`; per-question typed answers. |
| `outcome` | `enum{answered, partial_error, uncertain, omitted, refusal, malformed}` | M | |
| `per_question_errors` | `list<PerQuestionError>` | M | May be empty; must be present. |
| `diagnostics` | `Diagnostic` | O | **Mandatory when `outcome ∈ {partial_error, refusal, malformed}`.** |
| `versions` | `VersionSet` | M | `adapter` slot `known` for Jev. |
| `route` | `enum{opencode_session, direct_call}` | M | |
| `cost` | `CostRecord` | O | **Mandatory when `route = direct_call`.** |

`AnswerUnion` (discriminated by `question_kind`, no invented values):

- `PredicateAnswer`: `{ question_id: string M, value: bool M, probability: number O }`
  — `probability`, when present, is in `[0,1]`.
- `ChoiceAnswer`: `{ question_id: string M, choice: string M, confidence: number O }`
  — `choice` MUST be validated against the question's **declared option set**;
  an unknown option ⇒ `invalid`.
- `ScoreAnswer`: `{ question_id: string M, score: number M, legend: map<string,string> O, range: {min: number M, max: number M} O }`
  — `score` MUST lie within the declared `range` when known; out-of-range ⇒
  `invalid`.
- `BoxAnswer`: `{ question_id: string M, box: string M, value: string M }`.

`PerQuestionError`: `{ question_id: string M, code: string M, message_redacted: string M }`.
`Diagnostic`: `{ code: string M, detail_redacted: string M, raised_at: ts M }`.

**Typed answers / no invented values.** A typed answer MUST be taken from the
model result; the application MUST NOT synthesize, default, or coerce a choice,
score, or predicate. A `choice` MUST be validated against its declared options
and a `score` against its declared legend/range (with the `§6.1` `adapter` slot
`known`); an invalid shape ⇒ `outcome = malformed` with a persisted `Diagnostic`,
never a fabricated answer.

**Raw result ≠ scientific eligibility.** A `DecisionRecord` (or a Jev score) is a
machine proposal about paper text; it does **not** itself establish scientific
eligibility, exclusion, or selection. Only the human-gated screening decision
maps it, and a Jev answer alone MUST NOT exclude or admit a candidate.

**Never-exclusion invariant (critical).** When `outcome ∈ {partial_error,
uncertain, omitted, refusal, malformed}`, or a `per_question_error` is present,
or the model/provider produced an error/refusal/invalid shape, the application:

- MUST NOT convert it into a screening exclusion (spec line 31) or a scientific
  exclusion;
- MUST NOT treat it as a negative finding or a negation of the question's
  premise;
- MUST **retain the candidate** and persist the failure `Diagnostic` (or
  `per_question_errors`), then route it to `uncertain` retention or human review.

An uncertain, errored, refused, omitted, or malformed decision is **never** an
exclusion, and its candidate is **never** dropped: a missing/malformed decision
MUST still retain the candidate and persist a failure diagnostic.

### 6.3 `CostRecord` — direct-call cost separation

| Field | Type | Presence | Invariant |
| --- | --- | --- | --- |
| `state` | `enum{known, estimated, unknown, not_incurred}` | M | |
| `amount_usd` | `decimal` | O | **Mandatory iff `state = known`**; exact decimal string (e.g. `"0.00002037"`), never a float, preserving provider precision. |
| `currency` | `enum{USD}` | M | |
| `route` | `enum{opencode_session, direct_call}` | M | |
| `accounting_bucket` | `enum{session_statistics, direct_call}` | M | MUST be consistent with `route`. |
| `reserve_usd` | `decimal` | O | Pre-authorized reserve/cap; **mandatory before a paid dispatch is admitted**. |
| `dispatch_ref` | `id` | O | The `DispatchAttempt` (§8.1.1) this cost belongs to. |

Invariants:

- `amount_usd` is an **exact decimal string**, not an integer of minor units and
  not a float; provider-reported precision (e.g. `0.00002037`) is preserved.
- `state = unknown` ⇒ `amount_usd` is absent and the cost MUST NOT be recorded as
  `0`; an unknown cost is **not zero**.
- `state = not_incurred` ⇒ no cost was incurred (still recorded, bucket named).
- `state ∈ {known, estimated}` ⇒ `amount_usd` MUST be present.
- `accounting_bucket` MUST equal the bucket implied by `route`; a mismatch ⇒
  `invalid`. Costs from `direct_call` routes MUST NOT be aggregated into OpenCode
  session statistics; direct calls are accounted separately (spec line 20
  fallback; step 04 line 22; ADR `001` "direct calls … accounted separately").
- Reserve/dispatch accounting: a paid dispatch MUST carry a `reserve_usd` before
  admission; actual cost settles against the reserve, and an unsettled reserve is
  itself `estimated`/`unknown`, not `not_incurred`.

## 7. Secondary-origin attribution and warned tracing

### 7.1 `OriginAttribution`

| Field | Type | Presence | Invariant |
| --- | --- | --- | --- |
| `attribution_id` | `id` | M | |
| `claim_ref` | `ref` | M | The exact claim revision. |
| `depth` | `int` | M | Depth 0 is the currently cited source. |
| `origin_class` | `enum{primary_verified, secondary_warned, mixed, unclear, own_result, conceptual_contribution}` | M | |
| `source_ref` | `ref` | M | The source **actually read**; never an unread original. |
| `config_state` | `enum{configured, configuration_required}` | M | `configuration_required` when hop/source/retry defaults are unset. |
| `stop_reason` | `enum{none, hop_limit, source_limit, retry_limit, unavailable}` | O | Present iff `config_state = configured`; the actual reason tracing stopped. |
| `warning_visible` | `bool` | M | |
| `approval_required` | `enum{none, human_final}` | M | |

### 7.2 Invariants

- When `stop_reason ≠ none` (a configured limit was reached) or `origin_class =
  secondary_warned`, then `warning_visible` MUST be `true`, `approval_required`
  MUST be `human_final`, and `origin_class` MUST NOT be `primary_verified`.
- **Unresolved limits are `configuration_required`, not a reached limit.** When
  hop/source/retry defaults are unset (`config_state =
  configuration_required`), the application MUST NOT pretend a limit was
  configured or reached: tracing MUST NOT proceed past depth 0, `stop_reason`
  MUST be absent, and the trace is `blocked` pending configuration. No number is
  invented (spec line 48 "Numerical defaults are **not yet agreed**"). These are
  not the writer's repair budgets (§5).
- **Supported secondary evidence MUST be checked.** A `secondary_warned`
  attribution MUST carry an exact `source_ref` (the source actually read) with a
  verifiable locator/quote before final human approval; unsupported wording MUST
  NOT be approved. The exception does not permit wording unsupported even by the
  secondary source.
- Supported secondary attribution stays a **warned** state requiring final human
  approval, **never** "verified primary support" (spec line 50; step 13 line 20;
  ADR `001`; kickoff "At a tracing limit, secondary attribution stays a warned
  state requiring final human approval").
- Cite the source actually read; never substitute an unread original or claim
  direct verification.
- Section/manuscript checking MUST NOT relabel a real citation to a secondary
  source as verified primary support (step 12 line 21).
- Export/final approval is `blocked` until a human final approval is recorded (an
  `Approval`, §2.1, whose `gate_kind`/`gate_id` name the gate).
- Depth 0 is the currently cited source; trace direct support, not earliest
  discovery, and do not recurse every bibliography indiscriminately (step 13).

## 8. Runtime safety

### 8.1 `AdmissionReceipt` and admission vs completion

| Field | Type | Presence | Invariant |
| --- | --- | --- | --- |
| `admission_id` | `id` | M | Client-supplied idempotency key. |
| `project_id` | `id` | M | |
| `route` | `enum{opencode_session}` | M | **Only the observed durable OpenCode route.** |
| `submitted_at` | `ts` | M | |
| `has_outcome` | `bool` | M | MUST be `false` (the receipt is durable, never a completion). |

Invariants:

- A durable `AdmissionReceipt` applies **only** to the observed OpenCode durable
  idempotent route (`opencode_session`). Retrying the same `admission_id` MUST
  produce one accepted input; a durable receipt is **not** completion (step 00
  criterion 2).
- **Admission ≠ completion.** Only an authoritative terminal (`idle`/explicit
  outcome) completes a turn; a receipt or a partial assistant MUST NOT be read as
  success.
- The direct Jev route has **no** proven durable/idempotent admission; it MUST
  NOT use an `AdmissionReceipt` and MUST be tracked with `DispatchAttempt`
  records (§8.1.1).

### 8.1.1 `DispatchAttempt` (direct-call routes)

| Field | Type | Presence | Invariant |
| --- | --- | --- | --- |
| `dispatch_ref` | `id` | M | |
| `project_id` | `id` | M | |
| `route` | `enum{direct_call}` | M | |
| `attempt_no` | `int` | M | Monotonic per logical call; a retry is a **new** attempt. |
| `started_at` | `ts` | M | |
| `idempotent_admission_proven` | `bool` | M | MUST be `false` for direct calls (no proven durable admission). |
| `cost` | `CostRecord` | O | Reserve-bearing for paid dispatch (§6.3). |

Invariants:

- Direct calls are recorded as separate dispatch attempts, not as durable
  admission. A retry MAY re-dispatch and MUST be a new `DispatchAttempt`.
- The application MUST NOT claim exactly-once execution or billing for the direct
  route; it reconciles from the recorded attempts and their costs.

### 8.2 Exactly-once boundary

- The application prevents duplicate **accepted local results** via
  reconciliation and fencing of accepted writes, and MUST NOT claim exactly-once
  **external** execution or billing (step 03 acceptance; README "Retries and
  reconciliation prevent duplicate accepted local results without claiming
  exactly-once external execution or billing").
- **Fencing** protects accepted writes against stale attempts; it does **not**
  fence arbitrary external side effects that already occurred.
- Idempotent admission is claimed **only** where the durable route proves it
  (§8.1); the direct route makes no such claim (§8.1.1).

### 8.3 `Job` states and ambiguous remote work

`Job`: `{ job_id: id M, project_id: id M, attempt_id: id M, admission: AdmissionReceipt O,
state: enum{queued, admitted, running, waiting_human, waiting_external, paused,
cancelled, completed, failed, interrupted, unknown, reconciliation_required} M,
outcome_unresolved: bool M }`.

Invariants:

- When the remote outcome cannot be established from durable state, the job state
  MUST be `unknown` or `reconciliation_required`, and `outcome_unresolved` MUST
  be `true` (step 00 restart finding: an in-flight turn torn down by a service
  restart reconciles to an explicit unknown/interrupted, never a silent success).
- An ambiguous external job MUST NOT be re-executed with a new identity; the
  application MUST reconcile from durable state or escalate to a human (step 03:
  "Never blindly resend ambiguous external work with a new identity").
- A stale attempt MUST NOT overwrite a newer authorized result.
- Lost event streams MUST be reconciled from durable state/messages; missing
  events MUST NOT lose a completion or approval action (step 04).
- `waiting_human`, `waiting_external`, `paused`, and budget-exhausted work MUST
  explicitly **release execution resources** (no active executor/machine slot)
  and MUST NOT schedule new model calls or busy-loop while waiting.

### 8.4 Pause, cancel, and detached CLI

| Action | Semantics | Invariant |
| --- | --- | --- |
| Close CLI (detached) | CLI lifetime ends; authorized background job continues. | Closing the CLI does NOT cancel an authorized job (README global DoD). |
| `pause` | Stop scheduling new work; preserve durable state; resumable. | Not terminal; releases execution resources. |
| `cancel` | Stops scheduling new work; terminal for **scheduling** only. | Cancellation stops scheduling but MUST NOT terminal-clear an ambiguous remote outcome: if the remote result is unknown, `outcome_unresolved` stays `true` (§8.3). It is never `completed`, and external side effects already executed are not retracted. |

A terminal disconnect is distinct from a `pause` request (README "Proposed CLI
concepts"). Non-owner control MUST be rejected; no root or public unauthenticated
listener is required (step 03).

### 8.5 Project activation and safe switching

`ProjectActivation` (nullable identities when inactive):

| Field | Type | Presence | Invariant |
| --- | --- | --- | --- |
| `state` | `enum{none, active, draining, switching}` | M | |
| `active_project_id` | `id` | O | MUST be `null` when `state = none`; non-null and unique otherwise. |
| `activation_token` | `id` | O | MUST be `null` when `state = none`; non-null when `state ∈ {active, draining, switching}`. |

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
- Unknown remote outcome ⇒ `unknown`/`reconciliation_required` with
  `outcome_unresolved = true`; never re-executed, never `completed`, not cleared
  by cancellation.
- Unresolved tracing limits are `configuration_required` (§7), not a claimed
  reached limit; the dependent behavior is `blocked` rather than guessed.
- A cost that is unknown is recorded as `unknown` (never `0`); an unknown or
  malformed decision retains its candidate and its diagnostic.

## 10. Rights, privacy, and untrusted data (boundary only)

Rights, privacy, hosted-model transfer, redistribution, and provider/journal
policy are defined by the sibling policy contract
[`03-policy-decisions.md`](03-policy-decisions.md) (produced in the coordinated
contract set, C04), not by this document. This contract only fixes that:

- Documents and model outputs are untrusted data; no arbitrary source
  instructions may widen scope, grant approvals, or edit authoritative state.
- No hidden approvals; no public unauthenticated services; no credential
  leakage.
- Fixtures and logs MUST NOT contain secrets or licensed source text (spec lines
  62–63; step 00 "no secrets or licensed full text").

## 11. Worked examples

All examples are **synthetic** and are **abbreviated projections**: they show the
fields needed to make the point and omit repeated fields (ids, timestamps,
unrelated entries). They assert no scientific result. `verdict` is this
contract's own validity judgement (valid / invalid / blocked), not a scientific
conclusion. Two **complete minimal** records follow in §11.0.

### 11.0 Complete valid minimal records

A complete, valid **human approval** (every `M` field present):

```json
{
  "approval_id": "appr_0001",
  "granted_by": {
    "actor_kind": "human",
    "actor_id": "user_owner",
    "authority": "owner_authenticated",
    "owner_auth_ref": "authrec_0001"
  },
  "project_id": "proj_A",
  "target": {"project_id": "proj_A", "artifact_id": "jev_rubric", "revision_id": "rev_3"},
  "decision": "approved",
  "gate_kind": "pilot",
  "gate_id": "gate_pilot_1",
  "dependency_snapshot": {
    "snapshot_id": "snap_7",
    "project_id": "proj_A",
    "entries": [
      {"project_id": "proj_A", "artifact_id": "jev_rubric", "revision_id": "rev_3", "digest": "SHA256HEX", "role": "rubric"}
    ],
    "versions": {
      "context_kind": "human_authored",
      "input_digest": "SHA256HEX",
      "model": {"state": "not_applicable"},
      "prompt": {"state": "not_applicable"},
      "rubric": {"state": "known", "ref": {"version": "jev-rubric/1", "digest": "SHA256HEX"}},
      "parser": {"state": "not_applicable"},
      "adapter": {"state": "not_applicable"},
      "route": "not_applicable"
    }
  },
  "granted_at": "2026-10-27T12:00:00Z",
  "status": "active",
  "rationale": "Pilot rubric meets the confirmed workflow requirements."
}
```

A complete, valid **typed Jev decision** (every `M` field present):

```json
{
  "decision_id": "dec_0001",
  "question_id": "is_original_report",
  "question_kind": "predicate",
  "target": {"project_id": "proj_A", "artifact_id": "candidate_17", "revision_id": "rev_3"},
  "answers": [
    {"question_id": "is_original_report", "value": true, "probability": 0.83}
  ],
  "outcome": "answered",
  "per_question_errors": [],
  "versions": {
    "context_kind": "model_derived",
    "input_digest": "SHA256HEX",
    "model": {"state": "known", "ref": {"version": "jev-1.14-20261014", "digest": "SHA256HEX"}},
    "prompt": {"state": "known", "ref": {"version": "screen/1", "digest": "SHA256HEX"}},
    "rubric": {"state": "known", "ref": {"version": "jev-rubric/1", "digest": "SHA256HEX"}},
    "parser": {"state": "not_applicable"},
    "adapter": {"state": "known", "ref": {"version": "jev-adapter/1", "digest": "SHA256HEX"}},
    "route": "direct_call"
  },
  "route": "direct_call",
  "cost": {"state": "known", "amount_usd": "0.00002037", "currency": "USD", "route": "direct_call", "accounting_bucket": "direct_call", "reserve_usd": "0.25", "dispatch_ref": "disp_0001"}
}
```

Checks for these two records: every `M` field is present; each `VersionSlot` with
`state = known` carries a `ref` and no slot is a fake/placeholder ID; all
identities share one `project_id`; `accounting_bucket` matches `route`.

### 11.1 Uncertain Jev decision

**Valid.** A screening decision for `{proj_A, candidate_17, rev_3}` returns
`outcome = uncertain`, no `answers`, `versions.adapter.state = known`
(`jev-1.14-20261014`), `route = direct_call`, `cost = {state: known, amount_usd:
"0.00002037", accounting_bucket: direct_call}`. The candidate stays in the
retained-uncertain set and routes to human review. It is **not** an exclusion. →
**verdict: valid.**

**Invalid.** The record sets `outcome = answered` with a `PredicateAnswer.value`
the model did not return, or a `ChoiceAnswer.choice` not in the question's
declared options (an invented/coerced answer). Inventing or coercing an answer
violates §6.2. → **verdict: invalid.**

**Missing.** `outcome` absent (or `per_question_errors` absent). → **verdict:
invalid (blocked)** — no exclusion may be written; the candidate MUST be retained
and a failure diagnostic persisted (§6.2 never-exclusion).

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

**Invalid (closure).** A confirmed `blocking`/`major` issue marked
`state = superseded` without a resolved `successor_issue_id`, or `deferred` with
no human disposition — an unresolved blocker cannot be closed this way and makes
the final gate reject. → **verdict: invalid.**

**Missing.** `severity` absent, or both `evidence` and `missing_check` absent. →
**verdict: invalid (blocked)** — cannot be dispositioned; severity taxonomy is
also unapproved until the user ratifies it (§4.4).

### 11.3 Stale approval / model-edited budget

**Valid (stale).** `Approval{gate_kind=pilot, gate_id=gate_pilot_1,
target={proj_A, jev_rubric, rev_3}, dependency_snapshot=S_7}` becomes `stale` when
the rubric is revised to `rev_4`; the pilot stage is `blocked` until a new
approval on a new snapshot. Unaffected extraction work is preserved.

**Valid (budget lineage).** The model regenerates `draft_sec3 rev_6`;
`budget_lineage_id = L_sec`, `scope_id = sec_3`, `consumed_rounds = 3`,
`remaining = 0`, `state = ready_for_validation` (all confirmed defects resolved)
are unchanged. The exhausted budget does not replenish; a new model revision does
not start a new lineage. → **verdict: valid.**

**Valid (blocked exhaustion).** Same lineage with an unresolved confirmed
`major` defect: `remaining = 0`, `state = blocked`; further repair passes stop
and the stage escalates to a human — it does **not** auto-complete. → **verdict:
valid (blocked).**

**Invalid.** A `Proposal` (model output) changes `budget_lineage_id` to `L_new` or
sets `consumed_rounds = 0`; or a generic human `Approval` extends `limit` past
the §5.4 baseline maximum / resets the lineage mid-episode. Neither is permitted
without an explicit specification change. → **verdict: invalid.**

**Missing.** `dependency_snapshot` absent on the `Approval`; a `SnapshotEntry`
`digest` absent; or an entry whose `project_id` differs from the approval's. →
**verdict: invalid (blocked).**

### 11.4 Secondary-origin warning

**Valid.** Origin trace for claim `{proj_A, claim_12, rev_2}` returns
`origin_class = secondary_warned`, `source_ref = {proj_A, source_41, rev_1}` (the
source actually read, with a verifiable locator), `config_state = configured`,
`stop_reason = hop_limit`, `warning_visible = true`, `approval_required =
human_final`. The manuscript keeps a visible warning and cannot export until a
human final approval is recorded. → **verdict: valid.**

**Invalid.** `origin_class = primary_verified` while `stop_reason = hop_limit`;
the citation replaced with an unread original / claiming direct verification; or
a `secondary_warned` approval with unsupported wording and no checked evidence.
Violates spec line 50 / step 13 line 20. → **verdict: invalid.**

**Missing.** `origin_class` absent, or `config_state` absent. → **verdict:
invalid (blocked)** — no primary claim may be emitted. When hop/source/retry
defaults are unset the record is `config_state = configuration_required` and
tracing MUST NOT proceed past depth 0 or claim a reached limit; it blocks pending
configuration rather than inventing a number.

### 11.5 Interrupted remote work

**Valid.** An `opencode_session` turn was in flight during a service restart.
`AdmissionReceipt` (route `opencode_session`) exists with `has_outcome = false`;
no authoritative terminal was written. `Job.state = unknown` with
`outcome_unresolved = true`; the application reconciles from durable state, finds
no terminal, and escalates to a human. The work is **not** re-executed under a
new identity. → **verdict: valid.**

**Invalid.** `Job.state = completed` inferred from a partial `assistant
(finish: error)` message; an `AdmissionReceipt` with `route = direct_call`; or
the application re-submits the work with a fresh `admission_id` silently. →
**verdict: invalid.**

**Missing.** No durable record: neither an `AdmissionReceipt` (OpenCode route) nor
a `DispatchAttempt` (direct route). → **verdict: blocked** — reconciliation is
impossible; human disposition required.

### 11.6 Project switch

**Valid.** With project A active, a switch sets `state = draining`;
`active_project_id` and `activation_token` are non-null; project A's in-flight
admitted work is reconciled/quiesced; only then does project B activate with a
new `activation_token`. No A job commits after the switch; budgets and approvals
stay per-project. → **verdict: valid.**

**Invalid.** Activating project B while project A still has a committing job; a
second activation attempt executing concurrently (two active projects); a shared
budget/approval record reused across A and B; or `state = active` with a null
`active_project_id`. → **verdict: invalid.**

**Missing.** `state` absent, or the old project's job state unknown at switch
time. → **verdict: blocked** pending reconciliation.

## 12. Open decisions (owners) and dependency notes

| Item | State | Owner | Blocking effect |
| --- | --- | --- | --- |
| Severity taxonomy literal set (§4.4) | Proposed / unapproved | User | Any behavior keyed on severity is blocked (fail closed). |
| Origin hop/source/retry numeric defaults (§7.2) | Unresolved → `configuration_required` | User | Tracing blocks at depth 0; no limit is pretended configured/reached. |
| Cost/concurrency limits | Unresolved | User | Budget enforcement uses the inherited round limits; cost/concurrency caps (and paid-dispatch reserves) blocked until set. |
| Pilot/audit samples and acceptance rules; coverage/stopping rules; explicit acceptance of secondary-origin warnings | Unresolved (spec "Pending decisions") | User | Dependent gates blocked. |
| Rights/hosted-model policy | Defined by sibling [`03-policy-decisions.md`](03-policy-decisions.md) (coordinated in C04) | User | Hosted-model access is governed there, not by this contract. |
| Durable storage schema / IPC / package layout | Step-01 **proposal** (step 01 deliverable line 12); SQLite remains a proposal | User | Nothing here is an accepted schema; not a step-02-only decision. |

## 13. Change history

| Version | Date | Change | Status |
| --- | --- | --- | --- |
| `v1` | 2026-10-27 | Initial proposed representation of gates, approvals, findings budgets, machine decisions, and runtime safety, with worked examples. | **Proposed — approval pending.** |
| `v2` | 2026-10-27 | Review repairs: gate identity (`gate_kind`/`gate_id`) and project-scoped snapshot; out-of-band authority provenance; `VersionSlot` states (no fake model IDs) and canonical SHA-256 digests; typed Jev answer union, diagnostics, and candidate retention; exact-decimal `CostRecord` with unknown/not-incurred and reserve/dispatch accounting; section-scoped budget lineage with atomic increment, `ready_for_validation`, and no approval-based baseline extension; superseded/deferred cannot close blockers; `configuration_required` tracing with checked secondary evidence; `AdmissionReceipt` restricted to the OpenCode route with `DispatchAttempt`; cancellation not clearing unknown outcomes, nullable inactive activation, explicit resource release; complete minimal examples and abbreviated-projection note; step-01 storage/IPC proposal wording and sibling policy link. | **Proposed — approval pending.** |
