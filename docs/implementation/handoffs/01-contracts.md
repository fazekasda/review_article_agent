# Handoff — step 01: Ratify scientific contracts and technical decisions

**Status: COMPLETE (decision/contract documentation step).** On 2026-10-27 the user
dispositioned every P1–P6 and operational/technical item in the recorded Step 01
interview against the reviewed revision `ee7f7ea`; the dispositions are recorded in
[decision 002](../../decisions/002-step01-contracts-and-policies.md) and the
[policy register](../../contracts/03-policy-decisions.md). Step 01 is **ticked** in
[`docs/implementation/README.md`](../README.md). This step produced **documents and
an offline documentation checker only** — no production code, no database, no
scientific feature, and no scientific validation.

**What is approved vs. what remains reviewable.** The user approved the
**described behavior and technical direction** (with the concrete interview
amendments). The user did **not** personally approve every field-level spelling of
any schema or record. The record/reference representation in C01/C02/C03 is an
**implementation-facing, versioned contract**; a field-spelling change is a
versioned representation revision and does **not** require a new human round and is
**not** a blanket pending Step 01 blocker. "This contract alone is not Step 01
completion" (C01 status header) means a single document is not by itself completion
proof — the completion statement is this handoff.

## 1. Scope, base revisions, and authority used

- Step document (authoritative): [`docs/implementation/01-contracts.md`](../01-contracts.md);
  kickoff: [`kickoffs/01-contracts.md`](../kickoffs/01-contracts.md). The step's
  deliverables, acceptance criteria (lines 17–23), and required worked examples were
  **not edited**.
- Baseline: [`docs/specification.md`](../../specification.md) (confirmed; not
  weakened). Its "Pending decisions" section now records the 2026-10-27
  dispositions.
- **Ratified decisions used:**
  - [`docs/decisions/001-runtime-language.md`](../../decisions/001-runtime-language.md)
    — accepted 2026-10-27: Python core + supported, explicitly authenticated OpenCode
    V2 HTTP integration. Approves the runtime language/integration **only**.
  - [`docs/decisions/002-step01-contracts-and-policies.md`](../../decisions/002-step01-contracts-and-policies.md)
    — **accepted by the user 2026-10-27 against reviewed revision
    `ee7f7eafca80baa6c1aaca278361e56a663493c3`**, with the verbatim interview
    amendments. This is the Step 01 policy/technical decision deliverable.
- **Integrated base for this task:** `cedf8b0` (`cedf8b0a652e18ad1cd278e5aed9a2f01a6c9217`),
  which contains the policy ratification (`a6b451d`) and the C01/C02 behavioral
  alignment. Branch for this task: `task/step01-completion`.
- **History vs. current authority.** Earlier partial artifacts (the pre-ratification
  `proposed` contract statuses, the "partial / awaiting user ratification" index
  wording, and the earlier "Step 02 is blocked" handoff) are **historical**. Current
  authority is decision 002 and the accepted register; this handoff supersedes the
  partial version.

## 2. Deliverables in this bundle

| Path | Doc version | Status |
| --- | --- | --- |
| [`docs/contracts/README.md`](../../contracts/README.md) | — | Reading map: authority vs. representation, requirement-to-section map (AC1–7 + the seven named examples), cross-document rights/identity reconciliation, accepted technical direction. |
| [`docs/contracts/01-identities.md`](../../contracts/01-identities.md) | `v1.4` | Behavior approved 2026-10-27; representation reviewable. |
| [`docs/contracts/02-gates-and-runtime.md`](../../contracts/02-gates-and-runtime.md) | `v3.2` | Behavior approved 2026-10-27; representation reviewable. |
| [`docs/contracts/03-policy-decisions.md`](../../contracts/03-policy-decisions.md) | `v1` | **ACCEPTED** (all P1–P6, operational, ARCH-*, RIGHTS-v1 items). |
| [`docs/decisions/002-step01-contracts-and-policies.md`](../../decisions/002-step01-contracts-and-policies.md) | — | Accepted decision record (self-contained interview table). |
| [`scripts/check_contract_docs.py`](../../../scripts/check_contract_docs.py) | — | Offline, stdlib-only documentation validator (§6). |

**Contract version table.** C01 `v1.4`, C02 `v3.2`, C03 `v1`, per each document's own
versioning rule and change log (C01 §9; C02 §13). The remaining edits made here that
touch C01/C02 are status/link/policy-gate clarifications and do **not** require a new
version.

## 3. Acceptance criteria — criterion-by-criterion evidence

`docs/implementation/01-contracts.md` lines 17–23. Each criterion names its actual
normative sections plus valid/invalid/missing examples. Section numbers are live in
the contract documents.

| AC | Requirement (short) | Normative sections | Valid / invalid / missing examples | Status |
| --- | --- | --- | --- | --- |
| 1 | Every mandatory field/rule versioned, with examples incl. invalid and missing data | C01 §2.2–§2.4, §5.1–§5.9, §9; C02 §1–§6, §11.0; C03 §1, §5.6 | C01 §7.0 (complete valid set, parseable); C01 §7.2/§7.4/§7.5 (invalid/missing); C02 §11.1–§11.6 (valid/invalid/missing each); C03 §5.6 rights examples A–D | Met |
| 2 | No numeric scientific threshold invented to let a stage pass; open items owned + blocked | C01 §8; C02 §9, §12; C03 §0, §3, §6 | C03 §1.4 rejected-invented-threshold fixture; the checker rejects a `proposed` record carrying a number | Met |
| 3 | Distinguish rubric, framework, outline, report, document version, study/dataset, experiment, finding | C01 §3.2, §4, §6.2 | C01 §7.7 (study/document distinction); the §4 kind table's "explicitly **not**" column | Met |
| 4 | Approvals reference exact artifact/dependency revisions; a model edit cannot replenish budgets | C01 §3.2; C02 §2.1–§2.2, §3, §5.3, §5.4 | C02 §11.0 valid `Approval`; C02 §11.3 (stale approval / model-edited budget: valid-stale, valid-budget, invalid-reset, missing-snapshot) | Met |
| 5 | At a tracing limit, secondary attribution stays warned + final human approval, never verified primary support | C02 §7.1–§7.2; C03 POL-OPS-SECONDARY; C01 §5.4 (`secondary_warning`) | C02 §11.4 (valid warned / invalid primary_verified / missing) | Met |
| 6 | Rights/privacy boundaries; whether tools may use hosted models; no secret/source-text leakage in fixtures/logs | C03 §5.1–§5.5; C01 §5.6; C02 §10 | C03 §5.6 examples A–D; C01 §7.0 scoped-batch/rights pinning | Met |
| 7 | The user reviews scientific policy choices; architecture does not silently override confirmed requirements | C03 §0, §2–§4; C01 §8; C02 §12; ADR 002 | Decision 002 "Explicitly not decided / not claimed"; register §6.1 vs. §6.2 | Met |

**All seven acceptance criteria are met.** No dense numeric scientific threshold is
invented; confirmed baseline invariants (repair maxima 3/2/2, immutability,
no-self-approval, warned secondary attribution) are preserved and not weakened.

## 4. Seven named worked examples — exact sections

| Named example | Section (valid / invalid / missing) |
| --- | --- |
| Duplicate PDF | C01 [§7.1](../../contracts/01-identities.md#71-duplicate-pdf--valid) (valid) / [§7.2](../../contracts/01-identities.md#72-duplicate-pdf--invalid--missing) (invalid/missing) |
| Alternate version | C01 [§7.3](../../contracts/01-identities.md#73-alternate-version--valid) (valid) / [§7.4](../../contracts/01-identities.md#74-alternate-version--invalid--missing) (invalid/missing) |
| Missing full text | C01 [§7.5](../../contracts/01-identities.md#75-missing-full-text--explicit-state-not-silence) (explicit state, not silence) |
| Uncertain Jev decision | C02 [§11.1](../../contracts/02-gates-and-runtime.md#111-uncertain-jev-decision) (valid / invalid / missing) |
| Omitted result | C01 [§7.6](../../contracts/01-identities.md#76-omitted-result--audit-records-an-omission) (audit records an omission) |
| False critic objection | C02 [§11.2](../../contracts/02-gates-and-runtime.md#112-false-critic-objection) (valid / invalid / missing) |
| Secondary-origin warning | C02 [§11.4](../../contracts/02-gates-and-runtime.md#114-secondary-origin-warning) (valid / invalid / missing) |

All seven are present with valid **and** invalid/missing cases, plus C01 §7.0 (a
complete, machine-parsed conforming record set) and C01 §7.7 (multiple reports of
one dataset). The offline checker parses the §7.0 set and the C02 §11.0/C03 §5.6
JSON examples.

## 5. Decisions, ownership, and future project gates

- **Resolved Step 01 decisions.** Every P1–P6 and operational/technical item is
  `accepted` (register §2–§4, §6.1; decision 002). Dependent behavior is no longer
  blocked by an open **Step 01 decision**.
- **Future project-setup / runtime gates (not Step 01 blockers).** Register §6.2:
  (1) an explicit **project pilot/audit sample size and acceptance plan** before bulk
  work; (2) a **rubric-specific Jev mapping / uncertainty region** calibrated in the
  pilot (no universal cutoff); (3) explicit **per-project tracing limits** (max hops,
  new sources per claim/project, resolution retries) before upstream tracing — while
  unset, `config_state = configuration_required` blocks tracing past depth 0;
  (4) actual **source rights/privacy and provider authorizations** before the
  affected operations; (5) optional **venue current-policy checks** only when a venue
  is selected. These are set per project at setup with a fail-closed default, never
  invented here. No further Step 01 decision is required.

## 6. Verification — exact commands run and results

Environment: `Python 3.14.4`, run in this checkout on branch `task/step01-completion`,
**offline, no network, no paid calls**.

```sh
# Documentation validator (bundle self-consistency + embedded C01 validator)
python3 scripts/check_contract_docs.py
#   checks: 12 ok, 0 failed
#   OK: Step 01 contract docs are self-consistent

# Negative-mutation self-tests (all must be rejected)
python3 scripts/check_contract_docs.py --self-test
#   checks: 21 ok, 0 failed
#   OK: Step 01 contract docs are self-consistent (including self-test)

# Whitespace/conflict hygiene
git diff --check
#   (no output)

# Existing offline prototype suites (re-run here; no paid call)
cd spikes/runtime/python       && python3 -m unittest test_opencode_v2    # Ran 79 tests — OK
cd spikes/runtime/live-jev     && python3 -m unittest test_live_jev      # Ran 51 tests — OK
cd spikes/runtime/integration  && python3 -m unittest test_mock_provider # Ran 7 tests — OK
```

The checker verifies, at minimum: local markdown links/fragments exist; every fenced
`json` block parses; the **embedded** C01 §7.0 validator (extracted from the document
markers) passes; the three C02 §11.0 complete examples are type/range/enum/digest
checked; and the eight C03 examples are checked with distinguished expectations
(valid rights A/C; valid full record and illustrative patch; deliberately invalid
rights B/D and rejected decisions), including the canonical rights identity
`{project_id, rights_id, record_revision}` and the subject/authorization bindings.
`--self-test` mutates the real examples and asserts rejection of a bad hash, a
missing required field, a `known` state without a value, a cross-project rights
subject, an `allowed` operation without basis evidence, an unknown cost recorded as
`0`, and wrong typed answers.

### Pre-existing test-hygiene warning (disclosed, not introduced here)

The Jev suite emits `ResourceWarning: unclosed file ...env.report.json` at
`spikes/runtime/live-jev/test_live_jev.py:531` and `:507` (fixture report reads are
not closed). The suite still reports `Ran 51 tests — OK`; this is a **pre-existing**
test-hygiene warning, recorded honestly, not fixed by this task.

### Checks not run / limits

- **No CI is configured** in this repository, so there is no pipeline result.
- **No paid calls, no live/real-service re-run.** This task ran only offline
  documentation and prototype-fixture checks; it did **not** re-run the real service
  or any live model call and does **not** claim a real-service rerun.
- **No scientific code** and no production schema is produced; the checker is a
  documentation validator and makes no scientific-correctness claim.
- The C01 §7.1–§7.7 invalid/missing examples are illustrative pseudo-JSON in prose
  (not fenced JSON), so the checker does not machine-parse them; only §7.0 and the
  C02/C03 fenced examples are machine-checked. Markdown fragments are checked with a
  best-effort GitHub-style slug (disclosed in the checker docstring).
- A newer parallel checker worker (F04) may change the final counts; the coordinator
  final PR includes the combined latest checks.

## 7. Stubs, migrations, and next step

- **No production runtime, database, or migration is produced.** Migration: **none**
  (documentation-only step). Backup/restore is a Step 02 test obligation, not
  performed here. No daemon, no agent framework, no scientific feature, no
  scientific validation.
- **Stub / project values.** The per-project values in §5 remain unset; they are
  project-setup gates, and while unset the dependent behavior is fail-closed
  (`configuration_required` / `blocked`), never a guessed number.
- **Next task (02) — authoritative reference.** The next scoped implementation task
  is **Step 02: CLI, project registry, artifacts, and backups** —
  [`docs/implementation/02-project-foundation.md`](../02-project-foundation.md) is its
  authoritative step document. Step 02 is scoped to the **CLI and local storage**
  under the accepted direction; it does **not** implement a daemon, agent framework,
  or any scientific feature. This handoff does not implement Step 02.
- **Merge decision retained by the user.** The branch is **PR-ready but not merged**;
  the user retains the merge decision.

## 8. Commits

- Reviewed decision revision (anchor): `ee7f7ea` (`ee7f7eafca80baa6c1aaca278361e56a663493c3`).
- Policy ratification: `a6b451d`.
- **Integrated base for this task:** `cedf8b0` (`cedf8b0a652e18ad1cd278e5aed9a2f01a6c9217`).
- This task's scoped changes are committed on `task/step01-completion` (see the branch
  tip; the coordinator records the ledger/PR). Files changed here:
  `README.md`, `docs/contracts/README.md`,
  `docs/implementation/{README.md,handoffs/01-contracts.md,kickoffs/01-contracts.md}`,
  `docs/contracts/{01-identities.md,02-gates-and-runtime.md}`, and
  `docs/decisions/002-step01-contracts-and-policies.md` (portability wording only).
  No baseline invariant, no step-acceptance criterion, no register, no script, and no
  production code were changed.
