# Step 01 contracts — reading map

> **Status: ACCEPTED direction (2026-10-27); representation reviewable.** The user
> approved the **behavior and technical direction** of this bundle in the recorded
> Step 01 interview (see
> [`../decisions/002-step01-contracts-and-policies.md`](../decisions/002-step01-contracts-and-policies.md)
> and [`03-policy-decisions.md`](03-policy-decisions.md)). The record names, field
> names, enum spellings, state vocabulary, rights schema, and architecture **schema**
> remain a **versioned implementation-facing representation** — reviewable
> line-by-line, but **not** a new human-ratification gate: the user approved the
> described behavior, not every field spelling, and a field-spelling change is a
> versioned representation revision, not a Step 01 blocker. This page is the reading
> map to the accepted direction; the Step 01
> [handoff](../implementation/handoffs/01-contracts.md) carries the
> criterion-by-criterion evidence and the completion statement.

## What is in the bundle

| Document | Doc version | Status | What it represents |
| --- | --- | --- | --- |
| [`01-identities.md`](01-identities.md) | `v1.4` | Behavior approved 2026-10-27; representation reviewable | Artifact/revision identity, evidence and attribution records, provenance, dependency links, missing/unknown states, rights **projection**, worked examples. |
| [`02-gates-and-runtime.md`](02-gates-and-runtime.md) | `v3.3` | Behavior approved 2026-10-27; representation reviewable | Revision-specific approvals and dependency snapshots, findings/issue lifecycle, review budgets, machine vs. human decisions, runtime safety (pause/cancel, project switching, restart reconciliation). |
| [`03-policy-decisions.md`](03-policy-decisions.md) | `v1` | **ACCEPTED** (all items, 2026-10-27) | The decision **register** for P1–P6 and operational/technical items, the canonical `RightsRecord` v1, and the accepted architecture direction. |

Confirmed sources that must not be weakened (read-only authority) are
[`../specification.md`](../specification.md) and the Step 01 step document
[`../implementation/01-contracts.md`](../implementation/01-contracts.md); the
Step 01 [kickoff](../implementation/kickoffs/01-contracts.md) adds read order and
runtime invariants, and the accepted
[decision 001](../decisions/001-runtime-language.md) selects the runtime
language/integration only.

## Authority vs. representation

- **Authority (not reopened here):** the confirmed requirements in
  [`../specification.md`](../specification.md) and the Step 01 acceptance criteria
  in [`../implementation/01-contracts.md`](../implementation/01-contracts.md), plus
  the runtime invariants in the [kickoff](../implementation/kickoffs/01-contracts.md).
  The accepted policies below **must not silently override** them.
- **Accepted behavior (2026-10-27):** the described behavior and technical direction
  of the P1–P6 and operational items, recorded in
  [`03-policy-decisions.md`](03-policy-decisions.md) and the decision record
  [`../decisions/002-step01-contracts-and-policies.md`](../decisions/002-step01-contracts-and-policies.md).
- **Reviewable representation (this bundle):** the record names, field names, field
  types, enum literals, state vocabulary, and rights schema. They are written
  normatively so reviewers can check them mechanically, and they are an
  **implementation-facing, versioned contract**. The user approved the behavior, not
  every field spelling; a field-spelling change is a versioned representation
  revision, not a new human-approval round.
- **Who decides:** the **user** owns every policy/technical item; the accepted
  statuses and the exact-revision anchor are recorded in
  [`03-policy-decisions.md`](03-policy-decisions.md). Only the **user** reopens an
  accepted item, by a new exact-revision decision.

## Requirement-to-section map

Step 01 acceptance criteria ([`../implementation/01-contracts.md`](../implementation/01-contracts.md)
lines 17–23):

| AC | Requirement (short) | Where the bundle addresses it |
| --- | --- | --- |
| 1 | Every mandatory field/rule versioned, with examples incl. invalid and missing data | C01 §5 (records), §7.2/§7.4/§7.5 (invalid/missing), §9 (versioning); C02 §11.1–§11.6; C03 §5.6 (rights examples A–D) |
| 2 | No numeric scientific threshold invented to let a stage pass; open items owned + blocked | C01 §8; C02 §12; C03 §0/§1.1/§2/§3/§7 |
| 3 | Distinguish rubric, framework, outline, report, document version, study/dataset, experiment, finding | C01 §4, §6.2, §3.2 |
| 4 | Approvals reference exact artifact/dependency revisions; a model edit cannot replenish budgets | C01 §3.2; C02 §2, §3, §5.3, §5.4 |
| 5 | At a tracing limit, secondary attribution stays warned + final human approval, never verified primary support | C02 §7; C03 POL-OPS-SECONDARY/§3; C01 §5.4 (`secondary_warning`) |
| 6 | Rights/privacy boundaries; whether tools may use hosted models; no secret/source-text leakage in fixtures/logs | C03 §5; C01 §5.6; C02 §10 |
| 7 | The user reviews scientific policy choices; architecture does not silently override confirmed requirements | C03 §0, §2–§4; C01 §8; C02 §12 |

The seven named worked examples (kickoff lines 79–80):

| Named example | Exact section |
| --- | --- |
| Duplicate PDF | C01 [§7.1](01-identities.md#71-duplicate-pdf--valid) (valid) / [§7.2](01-identities.md#72-duplicate-pdf--invalid--missing) (invalid/missing) |
| Alternate version | C01 [§7.3](01-identities.md#73-alternate-version--valid) (valid) / [§7.4](01-identities.md#74-alternate-version--invalid--missing) (invalid/missing) |
| Missing full text | C01 [§7.5](01-identities.md#75-missing-full-text--explicit-state-not-silence) |
| Uncertain Jev decision | C02 [§11.1](02-gates-and-runtime.md#111-uncertain-jev-decision) |
| Omitted result | C01 [§7.6](01-identities.md#76-omitted-result--audit-records-an-omission) |
| False critic objection | C02 [§11.2](02-gates-and-runtime.md#112-false-critic-objection) |
| Secondary-origin warning | C02 [§11.4](02-gates-and-runtime.md#114-secondary-origin-warning) |

## Cross-document consistency (identity and rights)

- C01's `RightsReference` is a **read-only projection** of the canonical
  `RightsRecord` v1 in C03 §5.1; the projection field names and canonical targets
  are reconciled in C01 §5.6 and C03 §5.1 (including
  `hosted_model_transfer_right -> operations.hosted_transfer`).
- A C01 reference to a canonical rights record is pinned to its immutable
  identity by the typed **`RightsRecordRef`** `{project_id, rights_id, record_revision}`
  (C01 §2.4/§5.6), never a mutable bare `rights_id`.
- C03's `authorization` binds **both** the exact source `subject` **and** the
  exact immutable `record_revision`, with explicit types and out-of-band
  provenance (C03 §5.1/§5.2).

## Accepted technical direction (2026-10-27)

These were the coordinator's **concrete technical recommendations**; the user
**accepted the direction** in the recorded Step 01 interview (the described
behavior and direction — not every schema field). The alternatives remain recorded
in [`03-policy-decisions.md`](03-policy-decisions.md) §4, and the exact schema
spelling is Step 02 work under the accepted direction.

1. **Storage (ARCH-STORE / ARCH-ACTIVE-ENF).** One **app-owned project-scoped
   SQLite store per project**, plus a **minimal atomic application-level registry**
   that maps application project IDs to store locations and performs
   **active-project arbitration** (the single global "one active project"
   invariant). Keep the registry small and atomic (a single guarded record with an
   atomic OS lock such as `flock`, as the Step 00 harness used); it is an app-owned
   file, never OpenCode's private database.
2. **IPC (ARCH-IPC).** An **authenticated owner-only Unix domain socket on Linux**
   (path under the app's data directory, mode `0600`, peer-credential check),
   **separate from** the OpenCode loopback HTTP service. No root, no public
   unauthenticated listener; non-owner control is rejected.
3. **Package layout (ARCH-PKG).** A Python-core package rooted at
   `src/review_article_agent/` with bounded modules `domain/`, `storage/`,
   `cli/`, `worker/`, `adapters/` (the OpenCode HTTP integration isolated behind
   one adapter module), and **no agent framework**.

No numeric **scientific** value (threshold, sample size, severity score, or
hop/source/retry default) is set here; per-project values remain project-setup
gates ([`03-policy-decisions.md`](03-policy-decisions.md)). The selected
cost/concurrency defaults are USD 25 cumulative per project, USD 10 cumulative per
stage, and at most 2 simultaneous external-model calls.

## Status and handoff

- The behavioral directions are accepted (2026-10-27); the representation remains a
  versioned implementation contract (above). The Step 01
  [handoff](../implementation/handoffs/01-contracts.md) records criterion-by-criterion
  evidence with valid/invalid/missing examples. This map is not itself completion
  proof; the handoff is the completion statement.
- Offline documentation checks for this bundle:
  [`scripts/check_contract_docs.py`](../../scripts/check_contract_docs.py)
  (`python3 scripts/check_contract_docs.py`).
