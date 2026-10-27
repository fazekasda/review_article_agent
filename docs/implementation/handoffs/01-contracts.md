# Handoff — step 01: Ratify scientific contracts and technical decisions

**Status: PARTIAL — awaiting user ratification.** This is **not** Step 01
completion, and Step 01 is **not ticked** in
[`docs/implementation/README.md`](../README.md). The session produced a
**proposed** contract bundle and a reproducible documentation checker. Every
scientific-policy and technical item is still owned by the **user** and remains
`proposed`; nothing here is ratified and no dependent behavior is unblocked.

## 1. Scope, base revisions, and authority used

- Step document (authoritative): [`docs/implementation/01-contracts.md`](../01-contracts.md);
  kickoff: [`kickoffs/01-contracts.md`](../kickoffs/01-contracts.md).
- Baseline: [`docs/specification.md`](../../specification.md) (confirmed; not weakened).
- **Source base:** the spike pull request (PR #2, `feat/runtime-spike`) was **merged
  into `main` at `3ef5bf9`**, so `main` already contains `spikes/`, the step-00
  handoff, and decision 001. The stale "draft/unmerged PR" paragraph in the kickoff
  was corrected to this fact.
- **Integrated baseline for this task:** `c65a4fd` (branch
  `task/step01-contract-handoff`); this task builds the reviewable Step 01 bundle on
  top of it.
- **Ratified decisions used: only [`docs/decisions/001-runtime-language.md`](../../decisions/001-runtime-language.md)
  (accepted 2026-10-27: Python core + supported, explicitly authenticated OpenCode
  V2 HTTP integration).** It approves the runtime language/integration **only** — no
  scientific policy, schema, IPC, or framework. **No P1–P6 item and no
  operational/technical item is accepted, modified, or deferred**; all stay
  `proposed`, owner **user**.

## 2. Deliverables in this bundle

| Path | What it is |
| --- | --- |
| [`docs/contracts/README.md`](../../contracts/README.md) | Bundle reading map: authority vs. proposal, requirement-to-section map (AC1–7 + the seven named examples), document-version refs, cross-document rights/identity reconciliation, and concrete technical **recommendations for the user's decision** (not approvals). |
| [`docs/contracts/01-identities.md`](../../contracts/01-identities.md) | Identity/evidence contract, **v1.3 proposed**. |
| [`docs/contracts/02-gates-and-runtime.md`](../../contracts/02-gates-and-runtime.md) | Gates/approvals/budgets/runtime-safety contract, **v3.1 proposed** (not edited by this task). |
| [`docs/contracts/03-policy-decisions.md`](../../contracts/03-policy-decisions.md) | Policy/architecture decision register, **v1 proposed** (canonical `RightsRecord` v1). |
| [`scripts/check_contract_docs.py`](../../../scripts/check_contract_docs.py) | Offline, stdlib-only documentation validator (see §6). |

**Integration alignment performed (C04).** The shared record set was reconciled
across the three documents:

- C01's `RightsReference` projection now maps to the canonical `RightsRecord`
  fields that exist — `subject` (discriminated union), per-operation
  `basis.<operation>.{kind, evidence:{ref, provenance}}`, and `authorization`;
  the earlier map named nonexistent `source_revision`, `evidence_ref`, and
  `authorizing_revision` (fixed in C01 §5.6).
- The wrong operation name `hosted_transfer_right` in C03 §5.1 was fixed to C01's
  exact field `hosted_model_transfer_right`.
- Identity links now **pin** the rights record: a typed **`RightsRecordRef`**
  `{project_id, rights_id, record_revision}` replaces the mutable bare `rights_id`
  on `RevisionRecord`/`ImportOccurrenceRecord` and on
  `EvidenceRecord.rights_reference`, `record_revision` is in the projection, and
  the §7.0 examples + inline validator resolve the pinned refs and reject
  cross-project/ill-typed refs.
- C03's `authorization` now binds **both** the exact source `subject` **and** the
  exact immutable rights `record_revision`, with explicit types and out-of-band
  provenance; the Example A fixture was updated.
- The pre-acquisition `subject_artifact_ref` union member is retained (policy
  subject union preserved), and the absent-sibling / "C04 reconciles" internal
  references were removed.
- C01 was bumped to **v1.3** with a §9 change-log row, per its own versioning rule.

## 3. Acceptance criteria — draft evidence vs. pending ratification

`docs/implementation/01-contracts.md` lines 17–23. **Every row is draft evidence
only; ratification is pending the user's dispositions (§7).**

| AC | Draft evidence in this bundle | Pending ratification |
| --- | --- | --- |
| 1 — versioned mandatory fields/rules with examples incl. invalid/missing | C01 §5 + §7.2/§7.4/§7.5 + §9 version log; C02 §11.1–§11.6; C03 §5.6 examples A–D; checker verifies the machine-parseable examples | The user must accept the contract versions/field sets |
| 2 — no invented numeric threshold; open items owned + blocked | C01 §8; C02 §12; C03 §0/§1/§2/§3/§7 (all `UNSET`, owner user); checker rejects a proposed record that carries a number | The user must set/defers each value |
| 3 — distinguish rubric/framework/outline/report/document-version/study-dataset/experiment/finding | C01 §4, §3.2, §6.2 | Ratification |
| 4 — approvals bind exact revisions; model edit cannot replenish budgets | C01 §3.2; C02 §2, §3, §5.3, §5.4 | Ratification |
| 5 — tracing-limit secondary attribution stays warned + final human approval | C02 §7; C03 POL-OPS-SECONDARY; C01 §5.4 `secondary_warning` | Ratification (mechanism is confirmed baseline) |
| 6 — rights/privacy boundaries + hosted-model access + no secret/source-text leakage | C03 §5; C01 §5.6; C02 §10 | Ratification of the rights schema |
| 7 — user reviews scientific policy; architecture does not silently override confirmed requirements | C03 §0, §2–§4; C01 §8; C02 §12; bundle README "Authority vs. proposal" | Ratification of policy + technical direction |

## 4. Seven named worked examples — exact sections

| Named example | Section |
| --- | --- |
| Duplicate PDF | C01 §7.1 (valid) / §7.2 (invalid/missing) |
| Alternate version | C01 §7.3 (valid) / §7.4 (invalid/missing) |
| Missing full text | C01 §7.5 |
| Uncertain Jev decision | C02 §11.1 |
| Omitted result | C01 §7.6 |
| False critic objection | C02 §11.2 |
| Secondary-origin warning | C02 §11.4 |

## 5. Open decisions and owners (nothing silently decided)

P1–P6 and the operational/technical items remain **`proposed`, owner user** with
dependent behavior blocked ([`03-policy-decisions.md` §2, §3, §7](../../contracts/03-policy-decisions.md)).
The canonical `RightsRecord` v1 (§5.1) is likewise `proposed`. **Only the user**
moves a record out of `proposed`, and only with `user_evidence` + a
`decided_against_revision` SHA.

## 6. Verification — exact commands run and results

Run in this checkout, offline, **no paid calls, no network**:

```sh
# Documentation validator (bundle self-consistency + embedded C01 validator)
python3 scripts/check_contract_docs.py
#   checks: 12 ok, 0 failed
#   OK: Step 01 contract docs are self-consistent

# Negative-mutation self-tests (all must be rejected)
python3 scripts/check_contract_docs.py --self-test
#   checks: 21 ok, 0 failed
#   OK: ... (including self-test)

# Whitespace/conflict hygiene
git diff --check
#   (no output)

# Existing offline prototype suites (re-run here; no paid call)
cd spikes/runtime/python     && python3 -m unittest test_opencode_v2   # Ran 79 tests — OK
cd spikes/runtime/live-jev   && python3 -m unittest test_live_jev      # Ran 51 tests — OK
cd spikes/runtime/integration && python3 -m unittest test_mock_provider # Ran 7 tests — OK
```

The checker verifies, at minimum: local markdown links exist; every fenced `json`
block parses; the **embedded** C01 §7.0 validator (extracted from the document
markers) passes; the three C02 complete examples are type/range/enum/digest
checked (`Approval`, predicate `DecisionRecord`, score `DecisionRecord`,
`CostRecord`); and the eight C03 examples are checked **with distinguished
expectations** — valid (rights A/C; full proposed record; illustrative patch) vs.
deliberately invalid (rights B/D; rejected decisions) — including the canonical
rights identity `{project_id, rights_id, record_revision}` and the
subject/authorization bindings.

`--self-test` mutates the real examples and asserts rejection of: a bad hash; a
missing required field and a `known` state without a value; a cross-project
rights subject; an `allowed` operation without basis evidence; an unknown cost
recorded as `0`; and wrong typed answers (predicate probability out of `[0,1]`; a
score outside the declared levels).

### Coordinator offline checks (disclosed; also re-run here)

- Python client suite: **79** passed. Jev mock suite: **51** passed. Integration
  fixture suite: **7** passed.
- **Existing Jev `ResourceWarning`s (disclosed, not introduced here):** the Jev
  suite emits `ResourceWarning: unclosed file ...env.report.json` at
  `spikes/runtime/live-jev/test_live_jev.py:531` and `:507` (fixture report reads
  are not closed). The suite still reports `OK`; this is a pre-existing
  test-hygiene warning, recorded honestly, not fixed by this task.

## 7. Checks not run / limits

- **No CI is configured** in this repository, so there is no pipeline result.
- **No paid calls, no live/real-service re-run.** This task ran only offline
  documentation and prototype-fixture checks; it did **not** re-run the real
  service or any live model call, and it does **not** claim a real-service rerun.
- **No scientific code** and no production schema is produced; the checker is a
  documentation validator, and no scientific-correctness claim is made.
- The checker treats C01 §7.1–§7.7 invalid/missing examples as **prose** (they are
  not fenced JSON) and therefore does not machine-parse them; only §7.0 and the
  C02/C03 fenced examples are machine-checked. Markdown fragments are checked with
  a best-effort GitHub-style slug (disclosed in the checker docstring).

## 8. Step 02 is blocked

**Step 02 ([`02-project-foundation.md`](../02-project-foundation.md)) must not
start** until the user (a) dispositions the open P1–P6 and operational-policy
items and (b) accepts, modifies, or defers the technical direction (store/registry,
IPC, package layout). Until then, no storage schema, IPC mechanism, or package
layout is authoritative and dependent behavior stays blocked.

## 9. Next actions

1. User disposes P1–P6 and the operational-policy items, and the technical
   direction, against an exact revision ([`03-policy-decisions.md` §6](../../contracts/03-policy-decisions.md)).
2. Re-run `python3 scripts/check_contract_docs.py --self-test` after any contract
   edit; do not merge automatically.
3. On ratification, update the contract status headers and tick Step 01 only when
   all its acceptance criteria pass.

## 10. Commits

This task's scoped changes are one commit on `task/step01-contract-handoff`
(see the branch tip; the coordinator records the ledger/PR). Changed paths:
`docs/contracts/{README.md,01-identities.md,03-policy-decisions.md}`,
`docs/implementation/handoffs/01-contracts.md`,
`docs/implementation/{README.md,kickoffs/01-contracts.md}`, `README.md`,
`scripts/check_contract_docs.py`. No baseline, no authoritative step criteria, no
production code, and no `02` runtime document were changed.
