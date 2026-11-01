# Contract 02 — Gates, approvals, findings budgets, and runtime safety

## Status and authority

- **Contract version:** `v3.4` (representation dated 2026-10-27; minor clarifications 2026-10-31 and 2026-11-01).
- **Approval state: behavioral direction APPROVED (2026-10-27); representation
  pending field-level review.** On 2026-10-27 the user approved the **behavior**
  described by the coordinated Step 01 contracts and selected the Step 01
  operational/technical policy values recorded below (severity categories,
  pilot/audit sampling, Jev screening mapping, cost/concurrency caps, tracing
  defaults, secondary-warning handling, recovery semantics). The *requirements*
  below are carried over from already-confirmed sources and the selected policies
  and must not be weakened. The *representation* introduced here — record names,
  field names, field types, and enum literal spellings — remains a **versioned
  implementation proposal** for review, **not** a line-by-line user ratification of
  every field. The canonical policy disposition is recorded in the sibling decision
  record
  [`../decisions/002-step01-contracts-and-policies.md`](../decisions/002-step01-contracts-and-policies.md)
  and in
  [`03-policy-decisions.md`](03-policy-decisions.md); where that policy record and
  this contract differ, the policy record governs policy. This contract still makes
  **no** production or scientific-validation claim.
- **Scope:** revision-specific approvals and their dependency snapshots; the
  findings/issue lifecycle and review budgets; machine (model) decisions versus
  human decisions; and runtime safety (pause/cancel, project switching, restart
  reconciliation, admission vs completion). Durable storage, IPC, and package
  layout are a **step-01 deliverable proposal** (`01-contracts.md` line 12); the
  architecture **direction** was approved 2026-10-27 (policy register §4), but the
  concrete **schema** remains a proposal (`docs/specification.md` line 16) and is
  not decided by this document. Migration/backup semantics remain a
  step-02 test obligation, not categorically a step-02-only decision.
- **No scientific feature claim.** This contract defines record shapes and
  invariants only. It does not claim that screening, extraction, drafting,
  review, or origin tracing is implemented, correct, or scientifically
  validated.
- **Adapter-faithful decisions.** The typed decision shapes (§6.2) are taken from
  the retained, tested direct-OpenRouter adapter
  [`spikes/runtime/live-jev/live_jev.py`](../../spikes/runtime/live-jev/live_jev.py)
  (`noul` predicate probability, `choice` options/confidence/probabilities,
  `score` levels/legend; `box` unsupported), not from the unexercised native
  route.
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
- **Selected policies (2026-10-27):** the Step 01 behavioral approvals and selected
  operational/technical values are recorded in
  [`../decisions/002-step01-contracts-and-policies.md`](../decisions/002-step01-contracts-and-policies.md)
  and the policy register
  [`03-policy-decisions.md`](03-policy-decisions.md). They are authority for the
  **policy/behavior** represented here.
- **Sibling identity contract:** artifact references use the immutable reference
  tuple `{project_id, artifact_id, revision_id}` defined by the sibling
  identity-and-evidence contract of this coordinated step-01 contract set. That
  tuple is assumed, not redefined here.
- **Sibling policy contract:** rights, privacy, hosted-model transfer, and
  provider/journal policy are defined by the sibling policy contract
  [`03-policy-decisions.md`](03-policy-decisions.md) of the coordinated step-01
  contract set. This document does not define that policy; the link resolves once
  the sibling contract lands.

## Notation and typing rules

| Token | Meaning |
| --- | --- |
| `string` | UTF-8 text. |
| `id` | Opaque, stable, non-empty application-assigned identifier. |
| `id|null` | An `id` or JSON `null`; used where a value is mandatory but legitimately absent in a named state. |
| `ref` | Immutable artifact reference `{project_id, artifact_id, revision_id}` (sibling contract). |
| `enum{...}` | Exactly one listed literal; an unrecognized value is **invalid** (fail closed, never coerced). |
| `ts` | UTC timestamp, ISO-8601, with explicit offset. |
| `digest` | Canonical content digest: lowercase hex **SHA-256** (`^[0-9a-f]{64}$`), the single algorithm for every digest in this contract. There is no per-field algorithm field. |
| `decimal` | Exact base-10 amount encoded as a string (never a JSON float); preserves provider precision. |
| `probability` | JSON number in `[0,1]`. |
| `list<T>` | Ordered sequence of `T`. |
| `map<K,V>` | Key→value mapping with `K`/`V` as named types. |
| `bool`, `int`, `number` | JSON boolean / integer / number. |
| `M` | Mandatory. **If absent, the containing record is `invalid` and every behavior depending on it is `blocked`.** |
| `O` | Optional; absence is itself a defined state, not silently defaulted. |
| `M*` | Mandatory-union: at least one member of a named set must be present. |

Invariant shorthand used below: **MUST**, **MUST NOT**, and **`invalid`/`blocked`**
are normative for this contract. No numeric scientific threshold may be invented
to let a stage pass; where a **mandatory per-project** limit or value is unset it
is named as **`configuration_required`** (a runtime project gate) and blocks the
dependent behavior.

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
  `owner_auth_ref` ⇒ `invalid`. The `owner_auth_ref` is a **pointer** to an
  out-of-band check; a record that merely contains the fields does not itself
  prove authority (see the §11.0 precondition).
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
| `role` | `enum{source_document, summary, finding, rubric, framework, outline, parser_output, model_output, question_def, other}` | M | |

Invariant: the snapshot is **exact** — it enumerates each dependency by
`project_id` + `artifact_id` + `revision_id` + canonical `digest`, never by name
or path, and all entries share one `project_id` (a cross-project entry denies the
snapshot/approval). A dependency whose current revision/digest differs from an
entry makes the bound approval `stale` (revision changed) or `invalidated`
(digest mismatch).

**What "dependency" means for staleness (clarified 2026-10-31).** For approval
staleness, a target's **dependencies** are exactly the revisions reachable from
the target through a path made **only of live (non-`version_of`) edges**. A
revision reachable **only** through a `version_of` ancestry edge — the ancestor
itself and, transitively, that ancestor's own historical inputs — is **historical
lineage**: it MUST still be enumerated in the snapshot and its exact `digest`
MUST verify (integrity is unchanged), but a change to it MUST NOT make the
target's approval `stale`. A source the target itself depends on through a live
path MUST still make the approval `stale` when its revision changes. When a
revision is reachable both through a live path and through a `version_of`
ancestry edge, the **live** role wins. This is the user decision of 2026-10-31
(OpenChamber session `ses_e73abb6baffeVk8BLNhLn10U3p`); the runtime realization
is `runtime/gates._exact_closure` and
`storage/approvals.ApprovalRepository._build_snapshot`.

## 3. Stale and invalidated approvals

| Trigger | Resulting `status` | Effect |
| --- | --- | --- |
| Any **live-path** dependency `revision_id` changes (new evidence/revision, spec line 33) | `stale` | Dependent stage `blocked` until re-approval; unaffected work preserved. |
| Stored `digest` ≠ recomputed digest | `invalidated` | Approval not honored. |
| Declared-material change to rubric / framework / parser / model / policy | `stale` | "Material rubric/model/parser changes reopen relevant pilot approval" (spec line 30). |
| New artifact revision replaces the approved one | `stale` | The prior approval does not carry forward to the new revision. |
| The approved tuple was an **original/ancestor revision** and a dependency has since been edited | `stale` **for affected current use** | The historical exact-revision approval is retained for audit only; it MUST NOT authorize current use. |
| A newer approval for the same `gate_kind` + `gate_id` + target exists | `superseded` | Older approval retained for audit, not honored. |
| Any project-identity mismatch (approval vs target vs snapshot vs entry) | `invalidated` | Approval denied. |

Invariants:

- A `stale`/`invalidated`/`superseded` approval MUST NOT authorize execution or
  export.
- **A dependency is a live-path input.** The dependencies whose change can make an
  approval `stale` are exactly the revisions reachable from the target through a
  path made **only of live (non-`version_of`) edges** (§2.2). A revision reachable
  **only** through a `version_of` ancestry edge — the ancestor and, transitively,
  its own historical inputs — is historical lineage: it MUST remain pinned and
  integrity-verified, but a change to it MUST NOT make the target's approval
  `stale`. A source the target itself depends on through a live path MUST still
  make the approval `stale` when its revision changes. (User decision, 2026-10-31;
  OpenChamber session `ses_e73abb6baffeVk8BLNhLn10U3p`.)
- A historical exact-revision approval (bound to a now-superseded ancestor tuple)
  is preserved for **audit history**; it MUST NOT authorize current use. This
  document does not create a new historical-export policy — the existing safe
  block (deny current use) is sufficient.
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
| `severity` | `enum{blocking, major, minor, style}` | M | **Selected categories — see §4.4 (behaviorally approved 2026-10-27); representation of the literal set remains a proposal.** |
| `state` | `enum{open, in_progress, fixed, rejected_with_reason, deferred, superseded}` | M | |
| `disposition_reason` | `string` | O | Mandatory (non-empty) when `state ∈ {fixed, rejected_with_reason, deferred, superseded}`. |
| `fix_ref` | `ref` | O | Mandatory when `state = fixed`; a fix MUST point at a new revision. |
| `counter_evidence` | `EvidenceRef` | O | Mandatory when a **confirmed** `blocking`/`major` issue is `rejected_with_reason` (proof it is demonstrably false). |
| `successor_issue_id` | `id` | O | Mandatory when `state = superseded`; MUST reference a successor issue that is itself resolved (`fixed`/`rejected_with_reason` with counter-evidence). |

Invariants:

- **M\*** union: at least one of `evidence` or `missing_check` MUST be present.
  If both are absent the `Issue` is `invalid` and MUST NOT be dispositioned.
- A finding needs **evidence or a concrete missing check**, a severity, and an
  explicit fixed/rejected-with-reason disposition.
- `state = fixed` without `fix_ref` ⇒ `invalid`.
- A **rejected** false objection MUST carry `disposition_reason`; rejecting an
  objection does **not** oblige a rewrite (see §5.3).
- **A human disposition may not waive a confirmed blocker.** A `blocking`/`major`
  issue carrying validated `evidence` can be closed only by a fix (`fix_ref`) or
  by a rejection with `counter_evidence` proving it demonstrably false. A bare
  human disposition, `deferred`, or `superseded`-without-resolved-successor MUST
  NOT clear a confirmed `blocking`/`major` issue to let the stage pass: it stays
  **unresolved**, and the **final gate rejects** (`blocked`). Human disposition
  is available only for non-confirmed or below-`major` items.
- `state ∈ {superseded, deferred}` with no resolved successor and no
  `counter_evidence` for a `blocking`/`major` issue leaves it unresolved, not
  closed.
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

### 4.4 Severity taxonomy — four categories (selected 2026-10-27)

The user approved a **four-category** severity scale on 2026-10-27 (the `severity`
field's literal set and enum representation remain a proposal; the **behavior** is
selected):

| Category | Meaning |
| --- | --- |
| `blocking` | A **factual, support, or essential-check** blocker. |
| `major` | A **consequential scientific defect**. |
| `minor` | A **non-consequential** issue. |
| `style` | A **style preference**. |

Rules:

- **What prevents completion.** Any **`blocking`** or **`major`** item, and any
  **unclassified** item that is consequential, prevents completion. `minor` and
  `style` findings may remain (specification line 57); faithful scientific
  uncertainty is not a defect and is recorded, not erased.
- **Fail closed on the unclassified.** A consequential item whose severity is
  missing or not yet assigned is treated as **blocking** until classified; it does
  **not** pass.
- **No numeric score.** Severity is categorical; **no** numeric threshold, count, or
  probability is invented here to let a stage pass.

## 5. Review budgets and round identity

### 5.1 `ReviewBudget`

| Field | Type | Presence | Invariant |
| --- | --- | --- | --- |
| `budget_lineage_id` | `id` | M | Stable across edits (§5.3). |
| `project_id` | `id` | M | Per-project; never shared across projects. |
| `scope_id` | `id` | M | The **section/stage instance** the lineage is scoped to. Two sections never share one lineage. |
| `stage` | `enum{section_critique, original_source_fact_check, manuscript_review}` | M | |
| `limit` | `int` | M | **Inherited confirmed baseline** (§5.4); not extendable by an `Approval` (§5.3). |
| `consumed_rounds` | `int` | M | Monotonic; incremented **atomically at round reservation, before dispatch** (§5.2). |
| `remaining` | `int` | M | Derived `= limit − consumed_rounds`; never negative. |
| `state` | `enum{open, ready_for_validation, blocked}` | M | See §5.5. |

### 5.2 `Round`

| Field | Type | Presence | Invariant |
| --- | --- | --- | --- |
| `round_id` | `id` | M | |
| `budget_lineage_id` | `id` | M | |
| `stage` | `enum{...}` | M | Same enum as §5.1. |
| `state` | `enum{reserved, findings_recorded, repair_recorded, finalized, abandoned}` | M | Reservation precedes dispatch. |
| `reserved_at` | `ts` | M | |
| `finalized_at` | `ts` | O | Mandatory when `state = finalized`. |
| `findings_pass` | `FindingsPass` | O | Mandatory when `state ∈ {findings_recorded, repair_recorded, finalized}`. |
| `repair_pass` | `RepairPass` | O | Present iff a repair was performed (absent for early success). |

`FindingsPass`: `{ pass_id: id M, issues: list<Issue> M, produced_by: Actor M }`.
`RepairPass`: `{ pass_id: id M, target_ref_before: ref M, target_ref_after: ref M,
resolved_issues: list<id> M, model_generated: bool M }`.

A round is **one findings pass plus one repair pass if needed** (spec line 56).

Invariants:

- A round slot is **reserved atomically before dispatch**: `consumed_rounds`
  increments once at `state = reserved` in a single transaction keyed by
  `round_id`. Because the reservation precedes the findings call, a crash or
  replay after dispatch cannot create unlimited findings passes.
- `finalized` completes the round **exactly once** (idempotent by `round_id`); a
  repeated finalize MUST NOT double-increment. `consumed_rounds` is monotonic.
- A `reserved`/`abandoned` round that never finalized still counts toward
  `consumed_rounds`; it MUST NOT be silently released to free budget except by
  explicit reconciliation, and MUST NOT be double-counted.
- Final read-only validation is a `ValidationRecord` (§5.5), **not** a `Round`:
  the `Round` shape has no read-only flag and no findings/repair pass for it.

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
- A lineage MUST NOT be reset during the same editing/repair episode.
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

### 5.5 Readiness, validation, and exhaustion

**Readiness is decided before validation; success only after checks.**

- `state = ready_for_validation` means the budget is closed with **no unresolved
  confirmed blocker/major defect** and a final validation is pending. It is
  readiness, **not** success: no completion claim may be made yet.
- `state = blocked` when any unresolved confirmed factual blocker or major
  defect remains (spec line 56). Exhaustion blocks further repair passes; it does
  **not** automatically pass or complete the stage.
- A stage completes only after a `ValidationRecord` with `result = clear`. A
  `ready_for_validation` budget whose validation is `defects_found`/`blocked` is
  not complete.

`ValidationRecord` (the final read-only validation; **not** a budgeted `Round`):

| Field | Type | Presence | Invariant |
| --- | --- | --- | --- |
| `validation_id` | `id` | M | |
| `budget_lineage_id` | `id` | M | |
| `scope_id` | `id` | M | |
| `target_ref` | `ref` | M | The exact validated revision. |
| `result` | `enum{clear, defects_found, blocked}` | M | |
| `repair_passes` | `int` | M | MUST be `0`; read-only forbids edits, not authorized model-based checks. |
| `performed_at` | `ts` | M | |

Invariants: a `ValidationRecord` adds **no** repair, MUST have `repair_passes = 0`,
MUST NOT create a `Round`, and MUST NOT increment `consumed_rounds`. A repair
requested by validation MUST route back to an existing authorized stage, never
open an unlimited terminal edit loop (spec line 42). Model-created revisions MUST
NOT reset or bypass any budget; a worker MUST NOT schedule further model calls
once a budget is `blocked` or work is paused. Separately authorized read-only
model checks may run subject to the usual rights, cost, and approval gates; they
cannot perform a repair or replenish the round budget.

### 5.6 Cost and concurrency caps (selected defaults, 2026-10-27)

The user selected reusable cost/concurrency defaults on 2026-10-27. These are
**operational caps**, distinct from the confirmed repair-round maxima (§5.4) and
**not** a promised cost or a scientific threshold:

| Cap | Value | Scope |
| --- | --- | --- |
| Cumulative project cap | **USD 25** | Per project. |
| Cumulative stage cap | **USD 10** | Per stage. |
| Concurrent external model calls | **at most 2** | Simultaneously, within the project. |

Invariants:

- **Everything counts.** Every route counts toward the caps, including
  **read-only** model checks and every cost bucket (`session_statistics` and
  `direct_call`, §6.3).
- **Reserve before dispatch.** A **defensible upper-bound** reserve (`reserve_usd`)
  MUST be recorded **before** a paid dispatch is admitted. If the cost cannot be
  bounded, the dispatch MUST **not** be issued.
- **Unknown reserve is retained.** An `unknown`/`estimated` cost retains its
  reserve (never released, never zeroed) until settlement (§6.3).
- **Caps stop new dispatch.** Reaching a cap stops **new** dispatch/queries; it
  does **not** retract work already executed and does **not** auto-pass a stage.
  Only an explicit **human** approval may raise a cap; a restart does **not** reset
  accumulated cost (see §8.3 on restart).
- The caps are enforced **independently** of the fixed repair-round maxima
  (§5.4), which no approval or policy change in this contract alters.

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

### 6.2 `DecisionRecord` (typed Jev/stateless decision) — adapter-faithful

Wire vs canonical: the retained adapter sends question type `noul` for the
predicate primitive and returns wire answers `{"type":"noul","noul":0..1}`,
`{"type":"choice","choice":str,"confidence?","probabilities?"}`, and
`{"type":"score","score":float,"confidence?","probabilities?","legend?"}`. It maps
these to canonical kinds `predicate`, `choice`, `score` and records `wire_type`.
A `box` question has **no** wire mapping in the retained adapter and is not an
accepted live answer.

One `DecisionRecord` is **one question** (singular typed answer; no list of
answers with an ambiguous global kind):

| Field | Type | Presence | Invariant |
| --- | --- | --- | --- |
| `decision_id` | `id` | M | |
| `question_id` | `string` | M | Unique within the record set. |
| `question_def_ref` | `ref` | M | The immutable question definition revision (`QuestionDef`) with its options/levels. |
| `target` | `ref` | M | |
| `answer` | `AnswerUnion` | O | Singular; present iff `outcome ∈ {answered, partial_error}`. |
| `outcome` | `enum{answered, partial_error, uncertain, omitted, refusal, malformed, unsupported}` | M | |
| `per_question_error` | `PerQuestionError` | O | Present for a per-question failure; mirrors the adapter's `missing_answer`/`provider_error`. |
| `diagnostics` | `Diagnostic` | O | **Mandatory when `outcome ∈ {partial_error, refusal, malformed, unsupported}`.** |
| `versions` | `VersionSet` | M | `adapter` slot `known` for Jev. |
| `route` | `enum{opencode_session, direct_call}` | M | |
| `cost` | `CostRecord` | O | **Mandatory when `route = direct_call`.** |

`QuestionDef` (immutable definition, referenced by `question_def_ref`):

| Field | Type | Presence | Invariant |
| --- | --- | --- | --- |
| `question_id` | `string` | M | |
| `question_kind` | `enum{predicate, choice, score}` | M | `box` is not an accepted kind (unsupported, §6.2). |
| `instructions` | `string` | M | |
| `options` | `list<string>` | O | Mandatory for `choice`; the declared, ordered option set. |
| `levels` | `list<string>` | O | Mandatory for `score`; the declared, ordered levels; a score is an index `0..len(levels)-1`. |
| `criteria_ref` | `ref` | M | Immutable revision holding the full criteria. |

`AnswerUnion` — discriminated by `answer_kind` (no invented values):

| Field | Type | Presence | Invariant |
| --- | --- | --- | --- |
| `answer_kind` | `enum{predicate, choice, score}` | M | Exactly one typed answer; `box` excluded. |
| `wire_type` | `enum{noul, choice, score}` | M | The adapter wire kind (`predicate`→`noul`); persisted for audit. |
| `probability` | `probability` | O | Mandatory iff `answer_kind = predicate`, absent otherwise; the raw `noul` value in `[0,1]` (no boolean, no threshold). |
| `choice` | `string` | O | Mandatory iff `answer_kind = choice`; MUST be one of the declared `options`. |
| `score` | `number` | O | Mandatory iff `answer_kind = score`; index within `[0, len(levels)-1]`. |
| `legend` | `map<string,string>` | O | Present iff `answer_kind = score`; keys MUST be declared level indices (as decimal strings). |
| `confidence` | `probability` | O | Adapter `confidence`; `[0,1]` when present. |
| `probabilities` | `map<string,number>` | O | Adapter `probabilities`; keys MUST be declared option/level identifiers, values `[0,1]`. |

`PerQuestionError`: `{ question_id: string M, reason: enum{missing_answer, provider_error, malformed_answer, unsupported_question_kind} M, code: string O, message_redacted: string M }`.
`Diagnostic`: `{ code: string M, detail_redacted: string M, raised_at: ts M }`.

**Predicate.** A predicate answer is a **probability** in `[0,1]` (the adapter's
raw `noul` value). It is **not** required to be boolean. There is **no universal
numeric cutoff** that turns a probability into `yes`/`no`. How a probability maps
to a screening decision is a **rubric-specific, pilot-calibrated policy** (selected
2026-10-27): per rubric, the pilot approves explicit **decision rules** and an
**uncertainty region** before bulk screening, the **raw probability is retained**,
and a human approves exclusions. No cutoff is invented here; an uncalibrated
rubric leaves the mapping **unresolved** and blocks any exclusion that would key on
it (fail closed; §6.2 never-exclusion invariant).

**Screening mapping is pilot-calibrated and human-gated (selected 2026-10-27).**
A `noul` probability, a `choice`, or a `score` is a machine proposal about paper
text; the **rubric-specific** decision rules and uncertainty region are approved in
the pilot, and **exclusion requires human approval** (random + risk-based
original-source checks). A Jev answer alone (and any `box` result, which is
unsupported) MUST NOT exclude or admit a candidate; an answer inside the
uncertainty region is retained as uncertain and routed to human review.

**Choice / score.** `choice` MUST be one of the declared `options`; `score` MUST
be within the declared `levels` index range; `legend` keys MUST be declared level
indices; `confidence` and `probabilities` are `[0,1]` with declared keys. Any
violation ⇒ `outcome = malformed` with a persisted `Diagnostic`, never a
fabricated answer.

**Box unsupported.** A `box` question is **not supported by the retained
adapter** (no wire type mapping). It MUST NOT produce an invented string result
and is excluded from the accepted live answer union: it yields `outcome =
unsupported` (or `per_question_error.reason = unsupported_question_kind`) with a
`Diagnostic`.

**Typed answers / no invented values.** A typed answer MUST be taken from the
model result; the application MUST NOT synthesize, default, or coerce a
probability, choice, or score.

**Raw result ≠ scientific eligibility.** A `DecisionRecord` (or a Jev
probability/score) is a machine proposal about paper text; it does **not** itself
establish scientific eligibility, exclusion, or selection. Only the human-gated
screening decision maps it, and a Jev answer alone MUST NOT exclude or admit a
candidate.

**Never-exclusion invariant (critical).** When `outcome ∈ {partial_error,
uncertain, omitted, refusal, malformed, unsupported}`, or a `per_question_error`
is present, or the adapter reports a missing answer / defensive `type:"error"` /
invalid shape, the application:

- MUST NOT convert it into a screening exclusion (spec line 31) or a scientific
  exclusion;
- MUST NOT treat it as a negative finding or a negation of the question's
  premise;
- MUST **retain the candidate** and persist the failure `Diagnostic` (or
  `per_question_error`), then route it to `uncertain` retention or human review.

An uncertain, errored, refused, omitted, malformed, or unsupported decision is
**never** an exclusion, and its candidate is **never** dropped: a missing or
malformed decision MUST still retain the candidate and persist a failure
diagnostic.

### 6.3 `CostRecord` — direct-call cost separation

| Field | Type | Presence | Invariant |
| --- | --- | --- | --- |
| `state` | `enum{known, estimated, unknown, not_incurred}` | M | Mirrors the adapter `cost_status` (`provider_reported`→`known`, `unknown`→`unknown`, `not_incurred`→`not_incurred`). |
| `amount_usd` | `decimal` | O | **Mandatory iff `state ∈ {known, estimated}`**; exact decimal string (e.g. `"0.00002037"`), never a float. |
| `currency` | `enum{USD}` | M | |
| `route` | `enum{opencode_session, direct_call}` | M | |
| `accounting_bucket` | `enum{session_statistics, direct_call}` | M | MUST be consistent with `route`. |
| `reserve_usd` | `decimal` | O | Pre-authorized reserve/cap; **mandatory before a paid dispatch is admitted**. |
| `dispatch_ref` | `id` | O | The `DispatchAttempt` (§8.1.1) this cost belongs to. |

Invariants:

- `amount_usd` is an **exact decimal string**, not an integer of minor units and
  not a float; provider-reported precision (e.g. `0.00002037`) is preserved.
- `state ∈ {known, estimated}` ⇒ `amount_usd` MUST be present.
- `state = unknown` ⇒ `amount_usd` is absent and the cost MUST NOT be recorded as
  `0`; an unknown cost is **not zero**.
- `state = not_incurred` ⇒ `amount_usd` is absent (no cost incurred; bucket still
  named).
- **Reserves are retained for unknown/estimated costs.** When `state ∈ {unknown,
  estimated}`, `reserve_usd` MUST be retained (not released, not set to zero)
  until a later settlement resolves the amount.
- `accounting_bucket` MUST equal the bucket implied by `route`; a mismatch ⇒
  `invalid`. Costs from `direct_call` routes MUST NOT be aggregated into OpenCode
  session statistics; direct calls are accounted separately (spec line 20
  fallback; step 04 line 22; ADR `001` "direct calls … accounted separately").
- **Caps apply across buckets.** The selected cost caps (§5.6) count every bucket
  and every route against the per-project and per-stage ceilings; a
  `reserve_usd` MUST be recorded before a paid dispatch is admitted (§5.6).

## 7. Secondary-origin attribution and warned tracing

### 7.1 `OriginAttribution`

| Field | Type | Presence | Invariant |
| --- | --- | --- | --- |
| `attribution_id` | `id` | M | |
| `claim_ref` | `ref` | M | The exact claim revision. |
| `depth` | `int` | M | Depth 0 is the currently cited source. |
| `origin_class` | `enum{primary_verified, secondary_warned, mixed, unclear, own_result, conceptual_contribution}` | M | |
| `source_ref` | `ref` | M | The source **actually read**; never an unread original. |
| `config_state` | `enum{configured, configuration_required}` | M | `configuration_required` when the project's hop/source/retry limits are unset. This is a **runtime project gate**, not a Step 01 open item (§7.2). |
| `stop_reason` | `enum{none, hop_limit, source_limit, retry_limit, unavailable}` | O | Present iff `config_state = configured`; the actual reason tracing stopped. |
| `warning_visible` | `bool` | M | |
| `approval_required` | `enum{none, human_final}` | M | |

### 7.2 Invariants

- **Tracing limits are required per project (selected 2026-10-27).** Before any
  upstream tracing work, each project MUST have explicit **maximum citation hops,
  new sources per claim and per project, and resolution retries**, with cycle
  detection. There is **no universal numeric default**: the values are a
  **mandatory per-project configuration**. While unset, `config_state =
  configuration_required` is a **runtime project gate** (the trace does not
  proceed past depth 0) — it is **not** a Step 01 unresolved decision.
- When `stop_reason ≠ none` (a configured limit was reached) or `origin_class =
  secondary_warned`, then `warning_visible` MUST be `true`, `approval_required`
  MUST be `human_final`, and `origin_class` MUST NOT be `primary_verified`.
- **Unconfigured limits are not a reached limit.** When the project's
  hop/source/retry limits are unset (`config_state = configuration_required`), the
  application MUST NOT pretend a limit was configured or reached: tracing MUST NOT
  proceed past depth 0, `stop_reason` MUST be absent, and the trace is `blocked`
  pending project configuration. No number is invented (spec line 48). These are
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
- **Secondary warnings are handled as review warning + export companion (selected
  2026-10-27).** The manuscript MUST carry **accurate secondary attribution**; it
  does **not** need to print a raw warning tag. Final human approval binds the
  **exact manuscript, its dependency set, and the warning set**; a material edit to
  any of those **reopens** the approval (I2/I5). The warning stays visible in the
  review and in the export companion.
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
| `prior_outcome` | `enum{none, definitive_failure, ambiguous, succeeded}` | M | The established outcome of the prior attempt this one follows. |
| `retry_authorized_by` | `Actor` | O | Mandatory for any retry (§8.1.1). |
| `cost` | `CostRecord` | O | Reserve-bearing for paid dispatch (§6.3). |

Invariants:

- Direct calls are recorded as separate dispatch attempts, not as durable
  admission. The application MUST NOT claim exactly-once execution or billing for
  the direct route.
- **No blind automatic retry.** A retry MAY be dispatched only with (a) explicit
  retry authorization/budget (`retry_authorized_by` recorded) **and** (b) either
  an established prior **definitive failure** (a non-billing rejection) or an
  explicit human risk disposition. An **ambiguous** prior outcome MUST NOT be
  retried automatically **except** under the bounded direct-call retry policy
  below; otherwise it reconciles from recorded state or escalates to a human,
  consistent with §8.3.
- **Bounded automatic direct-call retry (amendment, 2026-11-01; ADR 006).** On
  the owner's 2026-11-01 decision ("network errors or unknown errors are accepted
  … ~30% cost increase can be accepted for retries like this; no human approval
  required"), an ambiguous (`unknown`) **`direct_call`** dispatch MAY be retried
  automatically without a human, under all of:
  - **New deterministic identity, never a re-POST.** Each retry is a **new**
    dispatch attempt under a new request id derived deterministically from the
    base (`{base}-r1`, `{base}-r2`); the **same** request identity is still never
    re-POSTed. At most **3** attempts per logical call.
  - **Conservative supersession.** In one transaction the ambiguous predecessor
    becomes terminal `failed` with `cost` `estimated` = its full reserve (it may
    have been billed), recorded by an explicit, named, validated primitive that
    refuses any non-ambiguous/non-`direct_call` predecessor or a successor id that
    is not the deterministic successor.
  - **Bounded overhead.** Per accounting stage a retry is allowed only while the
    accumulated estimated cost of superseded/abandoned attempts plus the new
    reserve stays within `30%` of the stage's other (known/estimated, or reserve
    if outstanding) spend — floored so the first retry in a fresh stage is always
    allowed. All existing per-role/stage/project caps still apply to the new
    reserve.
  - **Exhaustion releases the slot.** When all attempts are ambiguous, or the
    overhead cap refuses a further retry, the last ambiguous attempt is resolved
    to terminal `failed`/`estimated` by a named primitive, so the project's single
    serial remote slot is released and the caller reports a definitive
    non-completion.
  - `retry_authorized_by` is not required for this bounded automatic path (the
    owner decision is the standing authorization); the `opencode_session` route is
    **unchanged** and is never retried this way.

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
  "Never blindly resend ambiguous external work with a new identity"). **Exception
  (amendment, 2026-11-01; ADR 006):** an ambiguous **`direct_call`** dispatch MAY
  be retried automatically under the bounded §8.1.1 policy — a **new deterministic
  identity** per attempt with the ambiguous predecessor conservatively resolved to
  terminal `failed`/`estimated` — but the **same** request identity is still never
  re-POSTed, and the `opencode_session` route is unchanged.
- A stale attempt MUST NOT overwrite a newer authorized result.
- Lost event streams MUST be reconciled from durable state/messages; missing
  events MUST NOT lose a completion or approval action (step 04).
- `waiting_human`, `waiting_external`, and `paused` release **local compute**
  (no local executor/machine slot) and MUST NOT schedule new model calls or
  busy-loop. `waiting_external` and `paused` MUST NOT release the **project's
  active arbitration**: a remote operation may still be executing, so the project
  remains the active project until its work is reconciled or quiesced (§8.5).

### 8.4 Pause, cancel, and detached CLI

| Action | Semantics | Invariant |
| --- | --- | --- |
| Close CLI (detached) | CLI lifetime ends; authorized background job continues. | Closing the CLI does NOT cancel an authorized job (README global DoD). |
| `pause` | Stop scheduling new work; preserve durable state; resumable. | Not terminal; releases **local compute** but keeps active-project arbitration while a remote operation may still be executing. |
| `cancel` | Stops scheduling new work; terminal for **scheduling** only. | Cancellation stops scheduling but MUST NOT terminal-clear an ambiguous remote outcome: if the remote result is unknown, `outcome_unresolved` stays `true` (§8.3). It is never `completed`, external side effects already executed are not retracted, and active-project arbitration is retained until the work is reconciled/quiesced. |

A terminal disconnect is distinct from a `pause` request (README "Proposed CLI
concepts"). Non-owner control MUST be rejected; no root or public unauthenticated
listener is required (step 03).

### 8.5 Project activation and safe switching

`ProjectActivation` (mandatory fields typed `id|null`; `null` is required when
inactive):

| Field | Type | Presence | Invariant |
| --- | --- | --- | --- |
| `state` | `enum{none, active, draining, switching}` | M | |
| `active_project_id` | `id|null` | M | MUST be `null` iff `state = none`; non-null and unique otherwise. |
| `activation_token` | `id|null` | M | MUST be `null` iff `state = none`; non-null when `state ∈ {active, draining, switching}`. |

Invariants:

- **At most one** active project may execute at once; a second concurrent
  activation attempt MUST be rejected (step 03 acceptance).
- A `switch` MUST `drain`/reconcile the old project's in-flight admitted work
  before another project executes (README: a safe project switch must reconcile
  or drain existing work before another project executes).
- Old-project jobs MUST NOT commit into the new project; per-project approvals
  and budgets are never shared (README global DoD).
- While the old project has any still-executing remote work, its active
  arbitration is retained; if its job state is unknown, the switch is `blocked`
  pending reconciliation (§8.3), not silently completed.

## 9. Missing and unknown states (fail closed)

- Absent mandatory field ⇒ record `invalid`; dependent behavior `blocked`.
- Unrecognized enum literal ⇒ `invalid`; never coerced to a default.
- Unknown dependency revision/digest ⇒ approval `stale`/`invalidated`;
  stage `blocked`.
- Unknown remote outcome ⇒ `unknown`/`reconciliation_required` with
  `outcome_unresolved = true`; never re-executed, never `completed`, not cleared
  by cancellation, and no blind retry (§8.1.1).
- Unresolved tracing limits are `configuration_required` (§7), not a claimed
  reached limit; the dependent behavior is `blocked` rather than guessed.
- A cost that is unknown is recorded as `unknown` (never `0`) with its reserve
  retained; a missing/malformed/unsupported decision retains its candidate and
  its diagnostic.
- A predicate without a `probability`, a choice outside declared `options`, a
  score outside declared `levels`, or a `box` question ⇒ `invalid`/`unsupported`;
  never an invented answer.

## 10. Rights, privacy, and untrusted data (boundary only)

Rights, privacy, hosted-model transfer, redistribution, and provider/journal
policy are defined by the sibling policy contract
[`03-policy-decisions.md`](03-policy-decisions.md) of the coordinated step-01
contract set, not by this document. This contract only fixes that:

- Documents and model outputs are untrusted data; no arbitrary source
  instructions may widen scope, grant approvals, or edit authoritative state.
- No hidden approvals; no public unauthenticated services; no credential
  leakage.
- Fixtures and logs MUST NOT contain secrets or licensed source text (spec lines
  62–63; step 00 "no secrets or licensed full text").

## 11. Worked examples

All examples are **synthetic**: they are **abbreviated projections** (showing the
fields needed to make the point, omitting repeated fields) or **complete minimal
records** (§11.0). Every digest shown is a **valid-shaped but synthetic** 64-hex
SHA-256 string, not the digest of any real artifact, and none of these examples is
evidence of a scientific result. `verdict` is this contract's own validity
judgement (valid / invalid / blocked), not a scientific conclusion.

### 11.0 Complete valid minimal records

**Out-of-band precondition (not proven by the JSON).** A valid human `Approval`
requires that the application's owner-auth check (§1.3) already succeeded outside
the data path; the `owner_auth_ref` below only **points at** that check. The JSON
does not by itself prove authority, and the digest strings are synthetic.

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
      {"project_id": "proj_A", "artifact_id": "jev_rubric", "revision_id": "rev_3", "digest": "00112233445566778899aabbccddeeff00112233445566778899aabbccddeeff", "role": "rubric"}
    ],
    "versions": {
      "context_kind": "human_authored",
      "input_digest": "00112233445566778899aabbccddeeff00112233445566778899aabbccddeeff",
      "model": {"state": "not_applicable"},
      "prompt": {"state": "not_applicable"},
      "rubric": {"state": "known", "ref": {"version": "jev-rubric/1", "digest": "ffeeddccbbaa99887766554433221100ffeeddccbbaa99887766554433221100"}},
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

A complete, valid **predicate decision** (probability mandatory; no boolean):

```json
{
  "decision_id": "dec_0001",
  "question_id": "is_original_report",
  "question_def_ref": {"project_id": "proj_A", "artifact_id": "question_def", "revision_id": "qd_rev_1"},
  "target": {"project_id": "proj_A", "artifact_id": "candidate_17", "revision_id": "rev_3"},
  "answer": {"answer_kind": "predicate", "wire_type": "noul", "probability": 0.83},
  "outcome": "answered",
  "versions": {
    "context_kind": "model_derived",
    "input_digest": "00112233445566778899aabbccddeeff00112233445566778899aabbccddeeff",
    "model": {"state": "known", "ref": {"version": "jev-1.14-20261014", "digest": "ffeeddccbbaa99887766554433221100ffeeddccbbaa99887766554433221100"}},
    "prompt": {"state": "known", "ref": {"version": "screen/1", "digest": "ffeeddccbbaa99887766554433221100ffeeddccbbaa99887766554433221100"}},
    "rubric": {"state": "known", "ref": {"version": "jev-rubric/1", "digest": "ffeeddccbbaa99887766554433221100ffeeddccbbaa99887766554433221100"}},
    "parser": {"state": "not_applicable"},
    "adapter": {"state": "known", "ref": {"version": "jev-adapter/1", "digest": "ffeeddccbbaa99887766554433221100ffeeddccbbaa99887766554433221100"}},
    "route": "direct_call"
  },
  "route": "direct_call",
  "cost": {"state": "known", "amount_usd": "0.00002037", "currency": "USD", "route": "direct_call", "accounting_bucket": "direct_call", "reserve_usd": "0.25", "dispatch_ref": "disp_0001"}
}
```

A complete, valid **score decision** (score within declared levels; legend on
level indices):

```json
{
  "decision_id": "dec_0002",
  "question_id": "confidence_level",
  "question_def_ref": {"project_id": "proj_A", "artifact_id": "question_def", "revision_id": "qd_rev_1"},
  "target": {"project_id": "proj_A", "artifact_id": "candidate_17", "revision_id": "rev_3"},
  "answer": {
    "answer_kind": "score",
    "wire_type": "score",
    "score": 1.09,
    "legend": {"0": "low", "1": "moderate", "2": "high"},
    "confidence": 0.36
  },
  "outcome": "answered",
  "versions": {
    "context_kind": "model_derived",
    "input_digest": "00112233445566778899aabbccddeeff00112233445566778899aabbccddeeff",
    "model": {"state": "known", "ref": {"version": "jev-1.14-20261014", "digest": "ffeeddccbbaa99887766554433221100ffeeddccbbaa99887766554433221100"}},
    "prompt": {"state": "known", "ref": {"version": "screen/1", "digest": "ffeeddccbbaa99887766554433221100ffeeddccbbaa99887766554433221100"}},
    "rubric": {"state": "known", "ref": {"version": "jev-rubric/1", "digest": "ffeeddccbbaa99887766554433221100ffeeddccbbaa99887766554433221100"}},
    "parser": {"state": "not_applicable"},
    "adapter": {"state": "known", "ref": {"version": "jev-adapter/1", "digest": "ffeeddccbbaa99887766554433221100ffeeddccbbaa99887766554433221100"}},
    "route": "direct_call"
  },
  "route": "direct_call",
  "cost": {"state": "known", "amount_usd": "0.00002037", "currency": "USD", "route": "direct_call", "accounting_bucket": "direct_call", "reserve_usd": "0.25", "dispatch_ref": "disp_0001"}
}
```

The `confidence_level` question declares `levels = ["low","moderate","high"]`, so
a valid `score` lies in `[0, 2]` and the observed live value `1.09` is in range.

### 11.1 Uncertain Jev decision

**Valid.** A screening decision for `{proj_A, candidate_17, rev_3}` returns
`outcome = uncertain`, no `answer`, `versions.adapter.state = known`
(`jev-1.14-20261014`), `route = direct_call`, `cost = {state: known, amount_usd:
"0.00002037", accounting_bucket: direct_call}`. The candidate stays in the
retained-uncertain set and routes to human review. It is **not** an exclusion. →
**verdict: valid.**

**Invalid.** The record sets `outcome = answered` with a `predicate.probability`
the adapter did not return; a `choice` not in the declared `options`; a `score`
outside the declared `levels`; or a `box` result (unsupported). Inventing,
coercing, or thresholding an answer violates §6.2. → **verdict: invalid.**

**Missing.** `outcome` absent, or a predicate `answer` present without
`probability`. → **verdict: invalid (blocked)** — no exclusion may be written;
the candidate MUST be retained and a failure diagnostic persisted (§6.2).

### 11.2 False critic objection

**Valid.** Critic raises `issue_88` on `{proj_A, draft_sec3, rev_5}`: `claim =
"paragraph 2 changes the population from mice to rats"`, `evidence = {kind:
locator, artifact_revision: {proj_A, draft_sec3, rev_5}, locator: "p2, L4"}`,
`severity = major` (category per §4.4; consequential scientific defect). The
author checks both revisions and the source;
the objection is demonstrably false. Disposition: `state =
rejected_with_reason`, `disposition_reason = "rev_5 p2 already reads 'mice';
critic cited rev_4"`, with `counter_evidence` locating the correct text.
`repair_pass` absent; budget lineage unchanged. Rejecting a false objection with
`counter_evidence` does **not** force a rewrite. → **verdict: valid.**

**Invalid.** `state = fixed` with no `fix_ref` (no new revision). Also invalid: a
`blocking`/`major` issue with validated `evidence` closed by a bare human
disposition, by `deferred`, or by `superseded` without a resolved successor or
`counter_evidence` — a human cannot waive a confirmed factual blocker/major
defect to pass; the final gate rejects. → **verdict: invalid.**

**Missing.** `severity` absent, or both `evidence` and `missing_check` absent. →
**verdict: invalid (blocked)** — cannot be dispositioned; a consequential item
with unclassified severity is treated as `blocking` until classified (§4.4).

### 11.3 Stale approval / model-edited budget

**Valid (stale).** `Approval{gate_kind=pilot, gate_id=gate_pilot_1,
target={proj_A, jev_rubric, rev_3}, dependency_snapshot=S_7}` becomes `stale` when
the rubric is revised to `rev_4`; the pilot stage is `blocked` until a new
approval on a new snapshot. The historical approval on `rev_3` remains for audit
only and MUST NOT authorize current use. Unaffected extraction work is preserved.

**Valid (budget lineage).** The model regenerates `draft_sec3 rev_6`;
`budget_lineage_id = L_sec`, `scope_id = sec_3`, `consumed_rounds = 3`,
`remaining = 0` are unchanged. The lineage does not replenish and is not reset. If
no unresolved confirmed defect remains, `state = ready_for_validation` — a
pending validation, **not** success; the stage completes only after a
`ValidationRecord{result = clear}`, which adds no repair and no round.

**Valid (blocked exhaustion).** Same lineage with an unresolved confirmed `major`
defect: `remaining = 0`, `state = blocked`; further repair passes stop and the
stage escalates to a human — it does **not** auto-complete. → **verdict: valid
(blocked).**

**Invalid.** A `Proposal` (model output) changes `budget_lineage_id` to `L_new` or
sets `consumed_rounds = 0`; a generic human `Approval` extends `limit` past the
§5.4 baseline maximum or resets the lineage mid-episode; a round is finalized
twice (double-increment); or a `ValidationRecord` carries `model_calls > 0` or is
counted as a budgeted round. → **verdict: invalid.**

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
(finish: error)` message; an `AdmissionReceipt` with `route = direct_call`; a
direct retry dispatched with `prior_outcome = ambiguous` and no authorization;
or the application re-submits the work with a fresh `admission_id` silently. →
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
budget/approval record reused across A and B; `state = active` with a null
`active_project_id`; or `state = none` with a non-null `active_project_id`. →
**verdict: invalid.**

**Missing.** `state` absent, or the old project's job state unknown at switch
time. → **verdict: blocked** pending reconciliation.

## 12. Selected policies, mandatory per-project values, and dependency notes

On **2026-10-27** the user approved the behavioral direction and selected the
operational/technical values below. This section separates **selected global
policy** from **mandatory per-project value** (no universal default). The canonical
disposition text is in the policy record (ADR 002
`docs/decisions/002-step01-contracts-and-policies.md` /
[`03-policy-decisions.md`](03-policy-decisions.md)). Owner for every item is the
**user** (with the **project** supplying mandatory per-project values).

| Item | Status (2026-10-27) | Owner | Effect |
| --- | --- | --- | --- |
| Severity categories (§4.4) | **Selected** — four accepted categories (`blocking`/`major`/`minor`/`style`) | User | `blocking`/`major` and any unclassified **consequential** item cannot pass; `minor`/`style` may remain. |
| Predicate probability mapping (§6.2) | **Selected** — no universal cutoff; **rubric-specific, pilot-calibrated** decision rules + uncertainty region; raw probability retained; human exclusion approval | User | An uncalibrated rubric blocks any exclusion keyed on the mapping (fail closed); a `box` result is unsupported. |
| Origin hop/source/retry limits (§7.2) | **Mandatory per-project** — explicit hops / new sources per claim and project / retries before upstream work; no universal numeric default | Project (User approves) | Unset ⇒ runtime project gate `configuration_required`: tracing blocks at depth 0, no reached limit pretended. |
| Cost/concurrency caps (§5.6) | **Selected defaults** — USD 25 per project, USD 10 per stage, at most 2 simultaneous external model calls | User | Caps stop **new** dispatch; reserve required before dispatch; human approval to raise; restart does not reset. Not a promised cost or scientific threshold. |
| Pilot/audit sampling and acceptance | **Mandatory per-project** — explicit sample sizes/rules approved before bulk work | Project (User approves) | Missing plan blocks bulk digestion; important failures pause/correct/re-audit. |
| Coverage/stopping | **Selected** — coverage report + explicit human freeze of the exact candidate revision; no universal numeric stopping/completeness | User | Unreported coverage state cannot be frozen to pass. |
| Secondary-origin warnings | **Selected** — review warning + export companion; accurate manuscript secondary attribution (no raw warning tag required); final human approval of the exact manuscript + dependency + warning set | User | Warned claims stay unapproved until that final approval; never verified primary support; material edits reopen. |
| Rights/hosted-model policy | **Selected** — separate operations; `unknown` blocks; scoped batch authorization (manifest/route/purpose/revision) after rights/privacy/provider terms; never overrides a source restriction | User | Hosted transfer blocked without the scoped authorization; governed with sibling [`03-policy-decisions.md`](03-policy-decisions.md). |
| Durable storage schema / IPC / package layout | Step-01 **proposal** — architecture **direction** approved 2026-10-27; SQLite **schema** remains a proposal | User | Nothing here is an accepted schema; not a step-02-only decision. |

**Nothing above invents a numeric scientific threshold**, and the confirmed
repair-round maxima **3 / 2 / 2** are unchanged (§5.4). Where a **mandatory
per-project** value is unset, dependent behavior is `blocked` (fail closed, §9).

## 13. Change history

| Version | Date | Change | Status |
| --- | --- | --- | --- |
| `v1` | 2026-10-27 | Initial representation of gates, approvals, findings budgets, machine decisions, and runtime safety, with worked examples. | Historical (superseded). |
| `v2` | 2026-10-27 | Review repairs: gate identity and project-scoped snapshot; out-of-band authority; `VersionSlot` states; typed answers; exact-decimal costs; section-scoped budgets and readiness; superseded/deferred; `configuration_required`; `DispatchAttempt`; cancellation and nullable activation; complete examples; step-01 storage wording. | Historical (superseded). |
| `v3` | 2026-10-27 | Adapter-faithful decisions: predicate probability mandatory (no boolean/threshold), choice options/confidence/probabilities and score levels/legend per `live_jev.py`, `box` unsupported and excluded, singular typed `answer` with immutable `QuestionDef`/`question_def_ref`, `wire_type` persisted, per-question `missing_answer`/`provider_error`; human disposition cannot waive a confirmed blocker (`counter_evidence` required); `ValidationRecord` separated from `Round`, readiness before validation, reservation-before-dispatch with exactly-once finalize; `CostRecord` amount iff known/estimated with retained reserves; nullable `id|null` activation; waiting-external/pause retain project arbitration; direct retry needs authorization and a non-ambiguous prior outcome; ancestor-tuple stale clause; sibling references as the coordinated contract set; valid synthetic 64-hex digests with explicit out-of-band precondition; example validation against types, ranges, and digests. | Historical (superseded). |
| `v3.1` | 2026-10-27 | Coordinator's bounded consistency correction after two worker repairs: predicate probability is conditionally mandatory, not mandatory for choice/score answers; read-only validation forbids repairs, not separately authorized model checks, and consumes no repair round. | Historical (superseded). |
| `v3.2` | 2026-10-27 | Behavioral-direction alignment (2026-10-27 approval): status reframed — behavior approved, representation still proposed. Severity §4.4 becomes the four selected categories with `blocking`/`major`/unclassified-consequential preventing completion. Predicate mapping §6.2 becomes rubric-specific pilot-calibrated rules + uncertainty region (no universal cutoff; raw probability retained; human exclusion approval). Tracing §7.2 becomes required per-project limits (not a Step 01 unresolved item); `configuration_required` is a runtime project gate. New §5.6 records the selected USD 25 / USD 10 / 2 caps and reserve-before-dispatch. §7 adds the review-warning+companion secondary handling (no raw warning tag required; exact manuscript/dependency/warning-set approval; material edits reopen). §12 rewritten to selected policies vs mandatory per-project values. No numeric scientific threshold invented; the 3/2/2 repair maxima are unchanged. Prior "proposed/unapproved" wording retained only in the historical rows above. | Historical (superseded). |
| `v3.3` | 2026-10-31 | Owner-approved clarification of **"dependency" for approval staleness**: a target's dependencies are the revisions reachable from the target through a path made **only of live (non-`version_of`) edges**; a revision reachable **only** through a `version_of` ancestry edge (the ancestor itself and, transitively, its own historical inputs) is historical lineage — pinned in the snapshot and integrity-verified (digest, size, rights), but a change to it does **not** make the target's approval `stale`. A source the target itself depends on through a live path still makes it `stale`. Added to the §2.2 snapshot invariant and the §3 stale table + invariants; the "any dependency `revision_id` changes" row is qualified as a **live-path** dependency. User decision 2026-10-31 (OpenChamber session `ses_e73abb6baffeVk8BLNhLn10U3p`); realized in `runtime/gates._exact_closure` and `storage/approvals.ApprovalRepository._build_snapshot`. | Historical (superseded). |
| `v3.4` | 2026-11-01 | Owner-approved **bounded automatic direct-call retry** (ADR 006). An ambiguous (`unknown`) `direct_call` dispatch MAY now be retried automatically, without human approval, under: new deterministic identities (`{base}-r1`/`{base}-r2`, at most 3 attempts, the same identity never re-POSTed); conservative supersession of the ambiguous predecessor to terminal `failed`/`estimated` = full reserve via a named, validated primitive; a per-stage overhead cap of `30%` of the stage's other spend (floored for the first retry); and exhaustion (all attempts ambiguous, or cap refusal) resolving the last ambiguous attempt to `failed`/`estimated` so the serial remote slot is released. Added to the §8.1.1 retry invariant and qualified the §8.3 "new identity" invariant. The `opencode_session` route is unchanged. Owner decision 2026-11-01 (network/unknown errors accepted; ~30% retry cost accepted; no human approval); realized in `runtime/dispatch.py` (`supersede_unknown_for_retry`/`abandon_unknown_after_retries`) and `runtime/retry.py`. | **Current (amendment; behavior approved 2026-11-01).** |
