# Handoff — step 15: end-to-end evaluation, scale, and local release

**Status: locally verified MVP; awaiting user review before declaring initial
release.** All Step 15 deliverables and acceptance criteria are implemented and
verified locally on **synthetic/recorded fixtures only**. The user has not yet
reviewed the demonstration or the limitations, so this is **not** an initial
release. No automatic publish, push, autostart, deploy, or submission is
performed.

Authority: `docs/implementation/15-release.md`, `docs/specification.md`, and the
predecessor handoffs `handoffs/02…14-*.md`. Full suite: see PR.

## Scope and baseline

Step 15 is integration/evaluation only — it does not replace missing predecessor
tests. This delivery adds documents and an evaluation harness; it changes **no
product module and no storage schema**. Base is the branch HEAD when this session
started.

## Files

- `docs/release.md` (new) — supported runtime, install/upgrade/migration,
  operations, troubleshooting table, scientific limitations, feature/support
  matrix, release checklist.
- `docs/implementation/handoffs/15-release.md` (this file, new).
- `README.md` — one Planning-documents line linking `docs/release.md` and this
  handoff. Step 15 is **not** ticked in `docs/implementation/README.md`
  (needs user review).

Already integrated at this branch HEAD (read for facts; unchanged here):
`tests/test_step15_e2e_front.py`, `tests/test_step15_e2e_manuscript.py`,
`tests/test_step15_injected_faults.py`, `tests/test_step15_recovery.py`,
`tests/test_step15_scale.py`, `tests/test_step15_release_check.py`,
`scripts/scale_test.py`, `scripts/release_check.py`,
`docs/implementation/handoffs/15-scale-report.json`.

## Commands / results

| Command | Result |
| --- | --- |
| `PYTHONPATH=src python3 -m unittest tests.test_step15_e2e_front tests.test_step15_e2e_manuscript tests.test_step15_injected_faults tests.test_step15_recovery tests.test_step15_scale tests.test_step15_release_check` | **36 tests, OK, 2 skipped** (the two opt-in gates: `GatedScaleTests`, `FullReleaseCheckTests`) in 35.9 s |
| `python3 scripts/check_contract_docs.py` | **15 ok, 0 failed** — OK |
| `git diff --check` | clean |

Full suite: see PR. The two skipped tests are opt-in because they are expensive
(1k/10k scale; clean-venv install) — see below.

## Acceptance-criterion evidence

Every pointer is an actual test method.

- **AC1 (full pipeline demonstration, front + back).**
  `test_step15_e2e_front.FrontHalfEndToEndTest.test_front_half_chain_and_visual_escalation`
  (brief → seed approval → plan → freeze → acquisition → parsing → screening
  pilot/batch → exclusion audit → extraction pilot/batch → output audit →
  synthesis gate) and
  `test_step15_e2e_manuscript.EndToEndManuscriptTests.test_synthesis_to_final_exports`
  (synthesis → framework/outline → blueprints → draft → section review → origin
  tracing → global review/freshness/notes → final approval → exports).
- **AC2 (seven injected faults, each with a required stop and a correction).**
  `test_step15_injected_faults`: `UnavailableSourceTests`,
  `FalseExclusionTests`, `OmittedFindingTests`, `UnsupportedClaimTests`,
  `FalseCriticObjectionTests`, `AmbiguousCitationChainTests`,
  `PostApprovalEditTests`.
- **AC3 (recovery on disposable instances; no lost approval, no duplicate
  result, ambiguous spend documented).** `test_step15_recovery`:
  `Step15CliExitRecoveryTest.test_cli_exits_after_admission_and_a_new_cli_sees_completion`,
  `Step15WorkerKillAndDedupTest.test_sigkill_mid_job_then_restart_has_no_duplicate_result`,
  `…test_lost_events_reconcile_from_persisted_state`,
  `…test_duplicate_admission_same_request_id_returns_same_job`,
  `Step15SessionReconcileRecoveryTest.test_interrupted_turn_reconciles_without_duplicate_result`,
  `…test_ambiguous_dispatch_stays_unknown_with_spend_documented` (mock OpenCode
  server; ambiguous spend stays `unknown`), and
  `Step15ApprovalBackupRecoveryTest.test_human_approval_survives_backup_verify_restore`.
- **AC4 (two projects isolated, no concurrent execution, safe switch).**
  `Step15WorkerKillAndDedupTest.test_two_projects_isolated_no_concurrent_execution_safe_switch`.
- **AC5 (scale measured against budgets agreed in 01).** `scripts/scale_test.py` +
  `tests/test_step15_scale.py` (`ScaleReportShapeTests`; `GatedScaleTests` runs
  1k/10k); report
  `docs/implementation/handoffs/15-scale-report.json`. Measured: 1,000
  candidates in ~3.9 s; 10,000 in ~84.5 s, max RSS ~223 MB, zero model/network
  calls. **No numeric scale budget was agreed in Step 01** — the report says so
  and claims no universal latency/cost promise.
- **AC6 (pilot/audit observations are calibration/inspected-sample evidence).**
  The front-half chain asserts `authority == "named_gate_only"` and
  `guarantee == "sampled_audit_not_completeness"`; documented in
  `docs/release.md` §Scientific limitations.
- **AC7 (required visual/supplement evidence interpreted or escalated).**
  `…test_front_half_chain_and_visual_escalation` (a missing figure `f1` and an
  unavailable supplement `s1` surface as `blockers`, the manifest is not
  acquired, and the assets are queued for human review).
- **AC8 (clean-environment install + documented recovery + secrets excluded).**
  `scripts/release_check.py` + `tests/test_step15_release_check.py` (canary +
  pattern secret scan unit regressions; the full clean-venv install + console
  script + backup/verify/restore run is gated behind `RAA_RELEASE_CHECK=1`).

## BRIDGES (stubs disclosed)

`test_step15_e2e_front.BRIDGES`: the screening `response` is an imported,
unverified Jev response (no live-dispatch binding); the extraction dispatch
receipts are synthetic verified receipts injected by patching
`workflow._require_run`; both consume recorded model-shaped outputs, not live
calls.

`test_step15_e2e_manuscript.BRIDGES`: `synthesis_inputs` (synthetic Step 09
receipt fixture), `tracing_claims_sources` (synthetic Step 13 claim/source
fixture; only `review_checks` are chained), `export_bibliography` (export uses a
synthetic approved-corpus record because `cit_*` records carry no
title/author/year), and `manuscript_text` (a short synthetic bridge text).

`test_step15_injected_faults` and `test_step15_recovery` reuse existing
synthetic fakes and fixtures by import. No bridge is a live integration.

## Known gaps

- **Human-reviewed demonstration on authorized REAL papers is not done** — it
  requires the user and authorized papers; this is the gating item before an
  initial release.
- **No live model/provider run.** All model outputs are recorded fakes; no
  credential or network egress. No scientific feature is validated against a
  live model.
- **Scale measured on one machine** (CPython 3.14.4, Linux x86_64, 4 CPUs); the
  numbers are raw and not universal promises. Python 3.11 was not executed.
- **Scale-test defect found and fixed in this PR:** a single candidate-ledger
  revision minting ≥ 10,000 dedup decisions failed read-back revalidation (the
  `dedup-\d{4}` id pattern rejected the 10,000th id). Task S15-dedup-fix relaxes
  the pattern to `\d{4,}` with a regression test, in this same PR.
- **pip bootstrap note:** `release_check.py`'s clean-venv install bootstrapped
  pip via `get-pip.py` in this sandbox because `ensurepip` is absent; the script
  reports the path actually used.

## Costs incurred

**USD 0.** No model, provider, or network call was made; all fixtures are
synthetic.

## Next authorized actions (require the user)

1. Run the human-reviewed end-to-end demonstration on **authorized real
   papers** and review the limitations and the scale report.
2. Optionally authorize a small **live smoke** model call with an explicit
   credential (not in the repository) to exercise the real transport.
3. After review, tick Step 15 in `docs/implementation/README.md`, then perform
   any release/publish action **explicitly** (none is automatic).
