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
- **Integrated base for this proof repair:** `dcb56c4`
  (`dcb56c476f50574c3f571b2c238fb6f35196193f`), which contains the policy
  ratification (`a6b451d`), the C01/C02 behavioral alignment (`cedf8b0`), the F03
  finalization (`dcb56c4`), and the F04 bounded ratification-checker coverage
  (`5e9c73c`). This proof-repair branch is `task/step01-final-proof`.
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
| 6 | Rights/privacy boundaries; whether tools may use hosted models; no secret/source-text leakage in fixtures/logs | C03 §5.1–§5.5 (separate operations; scoped hosted-transfer batch §5.2); C01 §5.6 (operation-scoped provider/journal gates; pinned `RightsRecordRef` projection); C02 §10 | C03 §5.6 rights examples A–D (valid / invalid / raw-incomplete); C01 §7.0 rights-pinning example (immutable `RightsRecordRef`) | Met |
| 7 | The user reviews scientific policy choices; architecture does not silently override confirmed requirements | C03 §0, §2–§4; C01 §8; C02 §12; ADR 002 | Decision 002 "Explicitly not decided / not claimed"; register §6.1 vs. §6.2 | Met |

**All seven acceptance criteria are met.** No dense numeric scientific threshold is
invented; confirmed baseline invariants (repair maxima 3/2/2, immutability,
no-self-approval, warned secondary attribution) are preserved and not weakened.

Two evidence-scope notes to avoid overclaim:

- **AC6** is documented **normatively** — the scoped hosted-transfer authorization is
  defined in C01 §5.6 / C03 §5.2. The C01 §7.0 example is a **rights-pinning**
  example (immutable `RightsRecordRef`), not a scoped-batch run; **no tested runtime
  batch authorization is asserted** (there is no runtime).
- **AC1** is achieved by the typed definitions plus the complete valid record set
  (C01 §7.0) and the invalid/missing examples **overall** — **not** by every named
  example carrying all three variants (see §4).

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

Evidence variety is as each document actually frames it — **not** every named example
carries all three variants:

- **paired valid + invalid/missing walkthroughs:** duplicate PDF (C01 §7.1 valid /
  §7.2 invalid-missing), alternate version (C01 §7.3 valid / §7.4 invalid-missing),
  uncertain Jev decision (C02 §11.1 valid / invalid / missing), false critic
  objection (C02 §11.2 valid / invalid / missing), secondary-origin warning
  (C02 §11.4 valid / invalid / missing);
- **single-variant walkthroughs:** missing full text (C01 §7.5) is one explicit
  **blocked**/missing-state walkthrough; omitted result (C01 §7.6) is one single
  audit-found example.

C01 §7.0 is a complete, machine-parsed conforming record set and C01 §7.7 is the
one-dataset case. The offline checker parses the §7.0 set and the C02 §11.0 / C03
§5.6 JSON examples. AC1's "invalid and missing examples" is satisfied by the typed
definitions and the invalid/missing examples **overall**, not by each named example
carrying every variant.

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

Environment: `Python 3.14.4`, run in this checkout on branch
`task/step01-final-proof` (integrated base `dcb56c4`), **offline, no network, no
paid calls**. All commands below run **from the repository root**.

```sh
# Documentation validator (bundle self-consistency + embedded C01 validator)
python3 scripts/check_contract_docs.py
#   checks: 15 ok, 0 failed
#   OK: Step 01 contract docs are self-consistent

# Negative-mutation self-tests (all must be rejected)
python3 scripts/check_contract_docs.py --self-test
#   checks: 29 ok, 0 failed
#   OK: Step 01 contract docs are self-consistent (including self-test)

# Checker's own unit tests (F04)
python3 -m unittest discover -s scripts -p 'test_*.py'
#   Ran 20 tests — OK

# Whitespace/conflict hygiene
git diff --check
#   (no output)

# Existing offline prototype suites (re-run from the repo root; no paid call)
python3 -m unittest discover -s spikes/runtime/python      -p 'test_*.py'  # Ran 79 tests — OK
python3 -m unittest discover -s spikes/runtime/live-jev    -p 'test_*.py'  # Ran 51 tests — OK
python3 -m unittest discover -s spikes/runtime/integration -p 'test_*.py'  # Ran 7 tests — OK
```

The checker verifies, at minimum: local markdown links/fragments exist (including the
ADR-002, baseline, step, kickoff, handoff and root-README targets added by F04); every
fenced `json` block parses; the **embedded** C01 §7.0 validator (extracted from the
document markers) passes; the three C02 §11.0 complete examples are
type/range/enum/digest checked; and the eight C03 examples are checked with
distinguished expectations (valid rights A/C; valid full record and illustrative
patch; deliberately invalid rights B/D and rejected decisions), including the
canonical rights identity `{project_id, rights_id, record_revision}` and the
subject/authorization bindings. F04 adds bounded ratification-evidence checks:
ADR-002's accepted scope anchored to `ee7f7ea`, register status rows for every
accepted record id, register/C02 agreement on the USD 25 / USD 10 / 2 defaults, and
per-project pilot/coverage/tracing `numeric_value` staying `UNSET`. `--self-test`
mutates the real examples and asserts rejection of a bad hash, a missing required
field, a `known` state without a value, a cross-project rights subject, an `allowed`
operation without basis evidence, an unknown cost recorded as `0`, wrong typed
answers, and a fabricated numeric cutoff.

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
- **The doc checks are bounded, stable-anchor checks** — link/fragment existence,
  counted records, selected field/value equality, and negative mutations. They are
  **not** a full semantic proof of the contracts; the criterion-by-criterion
  walkthrough (§3–§4) is **manual** and remains required. No runtime behavior is
  exercised by these checks.
- **Disclosed explicitly and unchanged:** no CI is configured; no live or paid call
  was made; no real service was re-run; and **no scientific guarantee** is claimed by
  this task. The F04 checker coverage is now present and included above (it is no
  longer a future/parallel worker).

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
- C01/C02 behavioral alignment: `cedf8b0`.
- F03 finalization / stale-status cleanup: `dcb56c4`.
- F04 bounded ratification-checker coverage: `5e9c73c`.
- **Integrated base for this proof repair:** `dcb56c4`
  (`dcb56c476f50574c3f571b2c238fb6f35196193f`); this proof-repair branch is
  `task/step01-final-proof`.
- This proof repair's scoped changes (status/crossref fixes + handoff evidence) are
  committed on `task/step01-final-proof` (see the branch tip; the coordinator records
  the ledger/PR). Files changed here:
  `docs/contracts/{01-identities.md,03-policy-decisions.md}` and
  `docs/implementation/handoffs/01-contracts.md`. No baseline invariant,
  step-acceptance criterion, register policy, script, or production code changed.
