# Handoff — kickoff 16: fast tests, CI, and MVP hardening (steps 1–10)

**Status: steps 1–10 complete and verified.** This handoff covers
`docs/implementation/kickoffs/16-mvp-hardening.md` steps 1–10. Steps 1–4
(working base, re-validation performance defect, GitHub Actions CI, test-suite
diet) are the sections below and are **unchanged**. Steps 5–10 (one real model
port, `pipeline run`, real PDF text extraction, data seams, CLI groups/worker
jobs, housekeeping) are in the [Steps 5–10](#steps-510) part. Step 10 ticks the
kickoff plan; `07`, `08`, and `15` remain unticked in
`docs/implementation/README.md` (partial / awaiting user review).

Authority: `docs/implementation/kickoffs/16-mvp-hardening.md`,
`docs/implementation/README.md` (completion/handoff protocol),
`docs/specification.md`, and predecessor handoffs `handoffs/00…15-*.md`.

## Scope and baseline revisions

- **Baseline (audit, 2026-10-30, `main` @ `9c1bdd9` / merged PR #19 `b9e1200`):**
  full suite `PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_*.py'`
  → **3,678 tests, 1,659.7 s serial (~28 min), OK, 9 skipped**; module-time sum
  **1,840 s**; drafting + synthesis modules **1,242 s (~67 %)**; **no CI**.
  Python 3.14.4, Linux, 4 CPUs. Top modules: `test_drafting_drafts` 674.7,
  `test_drafting_workflow` 207.6, `test_drafting_workspace` 116.4,
  `test_cli_drafting` 91.7, `test_drafting_package` 75.7,
  `test_synthesis_workflow` 59.2, `test_cli_jobs` 50.5, `test_cli_snapshot` 42.6,
  `test_step04_service_acceptance` 39.9, `test_cli_human_gates` 33.6.
- **Starting SHA of this session:** `3bac35d4979be09996eb9e5939305441a4dc0502`
  (`test/suite-diet` HEAD after steps 2–4 and their merges).
- **Base for all work:** `origin/main`. Step 2 landed on `perf/revalidation-cache`
  (PR #22), step 3 on `ci/github-actions` (PR #21), step 4 on `test/suite-diet`
  (PR to be opened by the coordinator).

## Step 2 — fix the upstream re-validation performance defect

**Merged:** PR #22, merge commit **`cfd970b`** (head `8ece47f`). CI run
[41046065204](https://github.com/fazekasda/review_article_agent/actions/runs/41046065204)
all green.

**Root cause (audited):** every drafting read/propose re-validated the whole
immutable upstream chain from scratch, recursively, on every operation — in one
test `domain/records.canonical_json` 184,406×, `_has_surrogate` 11.3 M×,
`_validate_approval_row` 15,056×, `named_gate_satisfied` 5,197×.

**Change:** a transaction-scoped validation memo, plus hot-path fast paths.

- `domain/records.py`: `_has_surrogate` `isascii()` / single `encode("utf-8")`
  fast path (replacing the per-character Python loop); lazy labels in
  `_check_jsonable`; `validate_identifier` fast path.
- `storage/opmemo.py` (new, 155 lines): **`memoize(conn, key, compute)`**,
  **`invalidate(conn)`**, **`stats(conn)`**. Caches **only** while
  `conn.in_transaction`; keyed `(key, conn.total_changes)`; cleared at the outer
  `BEGIN`/`COMMIT`/`ROLLBACK`, after every `ROLLBACK TO`, and around the
  `runtime/gates.py` read transactions; stores **deep copies**; **exceptions are
  not cached**. No process-global result cache (a stale approval can never be
  served later).
- Applied to `storage/human_approvals` (row revalidation, `evaluate`,
  `named_gate_satisfied` — audited to have **no wall-clock dependence**),
  `discovery/freeze`, `acquisition/manifest`, `extraction/workflow`,
  `synthesis/workflow`, and `drafting/package|workspace|workflow`.

**Tests added:** `test_storage_opmemo` (9), `test_validation_memo_lower` (12),
`test_validation_memo_upper` (5), equivalence tests in `test_records`.

**Results (PR #22):** full suite **3,716 tests, 185.5 s wall** with
`run_tests.py -j 4`, module sum **729.8 s**. Drafting + synthesis six modules
serial **72.5 s** (drafts 23.6, workflow 9.4, workspace 12.1, cli_drafting 8.4,
package 7.6, synthesis 11.4) vs 1,231 s. Profile of
`test_drafting_drafts.AssemblyLineageTests.test_parent_update_and_outline_order_and_historical_stability`:
**before** 241 s / 781 M calls → **after** 6.7 s / 16.2 M calls (then
`validate_identifier` became the top internal-time function, which motivated the
fast path).

**Acceptance (step 2):**

- *Full suite green, no test deleted or weakened* — **met** (3,716 tests OK).
- *Drafting + synthesis drop from ~1,240 s to well under a minute* — **not met
  as written; ~72.5 s serial (~25 s parallel) achieved.** Deviation disclosed
  below.

## Step 3 — GitHub Actions CI

**Merged:** PR #21, merge commit **`27a74f2`** (final head `6242879`). Green run
[41044096589](https://github.com/fazekasda/review_article_agent/actions/runs/41044096589).

**Files:** `.github/workflows/ci.yml` (new), `scripts/run_tests.py`,
`scripts/test_shards.py`, `tests/durations.json`, `docs/ci.md`.

**Design (as shipped):**

- Triggers `pull_request`, `push` to `main`, `workflow_dispatch`, nightly
  `schedule`; `concurrency: ci-${{ github.ref }}` with `cancel-in-progress`;
  `permissions: contents: read`.
- Actions pinned by commit SHA: `actions/checkout` v7.0.1,
  `actions/setup-python` v7.1.0, `actions/upload-artifact` v7.0.2,
  `actions/download-artifact` v8.0.2.
- Job **`fast`** (every push/PR): matrix **3.11 + 3.14**; byte-compile,
  contract docs, script tests, `git diff --check`, and the fast test tier under
  a wall-clock budget (`FAST_BUDGET_SECONDS=120`); `timeout-minutes: 10`.
- Job **`full`** (PRs to `main`, nightly, manual): **6 shards on 3.14 + 2 shards
  on 3.11**, balanced by `scripts/test_shards.py` from `tests/durations.json`;
  `--slow -j 4 --timeout 1800`; per-shard durations uploaded as artifacts;
  `timeout-minutes: 20`. A `full-durations` job merges the 3.14 shard timings
  so `tests/durations.json` can be refreshed by hand.
- Job **`release`** (manual dispatch with `release=true`, or a `v*` tag):
  `RAA_RELEASE_CHECK=1` clean-venv install check + the gated `RAA_SCALE_TESTS=1`
  1k/10k scale test.

**First-ever CI run** (run 41042701723 on `ecd7ca9`, later cancelled by the
superseding push) exposed **three environment-only failures**, fixed in
`6242879`:

1. `test_opencode_service` `Repair2ContainmentTests` depended on an `opencode`
   binary on `PATH` → now mocks `_resolve_binary`.
2. `test_worker_ipc` stale-socket test assumed inode numbers are not reused →
   now pins the stale inode with a hard link.
3. `test_cli` wheel test needs `setuptools` → CI installs `setuptools>=68`;
   the test fails with an actionable message otherwise.

Python 3.11 ran for the first time and passed.

**Red-check proof:** draft PR #23 (`scratch/ci-red-probe`, run
[41046105239](https://github.com/fazekasda/review_article_agent/actions/runs/41046105239))
with a deliberately failing test turned `fast` (3.11 + 3.14) and the full shards
containing it red; PR closed and branch deleted.

**Wall times on PR #22:** fast **1 m 56 s** (3.11) / **3 m 38 s** (3.14); full
shards **24 s – 2 m 34 s**.

**Acceptance (step 3):**

- *Green `fast` (3.11 + 3.14) and `full` on a PR; wall time and per-shard
  durations recorded* — **met** (run 41044096589; numbers above).
- *A deliberately failing test turns the check red* — **met** (PR #23).
- **Open user action (not changed by us):** enable branch protection on `main`
  requiring `fast` and `full`.

## Step 4 — test-suite diet

**Branch:** `test/suite-diet` (this branch; PR to be opened by the coordinator).

**Commits:** `0924718`, `5fe887c`, `be98023`, `478fabe`, `35ab69f`, `b904729`,
`f543571`, `dc5baac` (see commit IDs at the end).

### 4a — fast/slow tiers (`0924718` + `5fe887c`, S16-D1)

- New `tests/_tiers.py` exposes **`slow = unittest.skipUnless(RAA_SLOW_TESTS ==
  "1", …)`**. Set `RAA_SLOW_TESTS=1` to run the slow tier; `scripts/run_tests.py
  --slow` sets it for its children, and without it the variable is removed.
- **200 decorators across 58 test modules** marked slow (subprocess CLI, real
  detached worker, OpenCode service, SIGKILL/recovery, scale, off-MVP-path
  ledgers / session runner / execution boundary, and other expensive ≥ ~1 s
  tests). MVP drafting/synthesis fail-closed classes and all memo tests were
  **returned to the fast tier** by `5fe887c`.
- The moved-tests table (grouped by module with reason category) is in
  [Moved tests](#moved-tests-slow-tier) below.

### 4b — cheaper fixtures (`be98023`, S16-D2)

- **`RAA_TEST_SQLITE_SYNCHRONOUS`** test-only hook in `storage/schema.py`
  (accepts exactly `OFF` / `NORMAL`, anything else → `FULL`); resolved by
  `resolve_synchronous()`. `scripts/run_tests.py` sets it to `OFF` by default;
  `--durable-sqlite` keeps the production `PRAGMA synchronous = FULL`.
  **Production stays `FULL`.**
- Measured store-heavy modules: ext4 **180.5 → 97.5 s (−46 %)**, tmpfs
  **243.8 → 174.0 s**.

### 4c — prune / merge (S16-D4 `478fabe`, S16-D3 `35ab69f`, S16-D5 `b904729`)

Table-style validation tests collapsed into `subTest`-parameterized tests, all
cases preserved (verified method counts):

| Module | Before | After |
| --- | ---: | ---: |
| `test_jev_adapter` | 164 | 130 |
| `test_decision_records` | 124 | 92 |
| `test_records` | 117 | 47 |
| `test_opencode_client` | 112 | 100 |
| `test_lead_adapter` | 86 | 57 |
| `test_acquisition_validation` | 76 | 66 |
| `test_agent_roles` | 63 | 54 |
| `test_gate_snapshots` | 61 | 52 |

Net **−200 test methods**, every case retained as a subTest.

### 4d — delete dead code (`f543571`, S16-D6)

- Deleted `assert_production_execution_supported`, `ProductionExecutionBlocked`,
  `_PRODUCTION_BLOCKERS` and their messages from
  `runtime/execution_boundary.py`; removed
  `test_execution_boundary.py::ExecutionBoundaryTest::test_production_routes_are_blocked`.
  That test was cited as AC1 evidence in handoff 04; removal is **justified**
  because it exercised only the deleted dead function. The invariant
  `PRODUCTION_SUPPORTED_ROUTES == frozenset()` and the
  `EXECUTION_BOUNDARY_AUTHORIZES_*` flags remain asserted in
  `test_step05_core_acceptance`
  `test_cross_project_refs_and_foreign_token_refused_opencode_still_blocked`
  (the two assert calls inside that step-05 AC test were also removed).
- **No other docs-cited AC test was removed or renamed**; merged tests kept every
  docs-cited name.

### 4e — CI fast budget + refreshed durations (`dc5baac`, S16-D7)

- `run_tests.py` gains `--budget SECONDS`; CI `fast` sets
  `FAST_BUDGET_SECONDS=120` (proven: `--budget 1` exits 1); fast timeout 10 min,
  full 20 min. `tests/durations.json` refreshed for all 126 modules (6-shard plan
  max 144 s, 2-shard 432 s). `docs/ci.md` documents the test tiers; README gains
  a "Running tests" section.

### Step 4 acceptance

- *Fast tier < 2 min and full tier < 8 min, measured and recorded* — **met**
  (fast **42.5 s**, full **206.6 s**; tables below).
- *Every AC test cited in handoffs 02–15 still exists, or its removal is
  justified* — **met** (one justified removal, §4d).
- *Security/fail-closed behaviours keep at least one test each* — **met**
  (names verified present in the fast tier, below).
- *No product behavior change except test hooks and deleted dead code* —
  **met**.
- *Handoff includes before/after table and merged/deleted/moved lists* —
  **met** (this document).

### Fast/slow tier final measurement (at `3bac35d`, Python 3.14.4, 4 CPUs)

| Tier | Tests | Skipped | Wall | Module sum |
| --- | ---: | ---: | ---: | ---: |
| fast (default) | 3,516 | 670 (661 slow + 9 pre-existing) | **42.5 s** | 165.2 s |
| full (`--slow`) | 3,516 | 9 (7 pypdf absent + 2 opt-in gates) | **206.6 s** | 812.2 s |

Logs: `/tmp/opencode/s16-final-fast.log`, `/tmp/opencode/s16-final-full.log`.

### Before/after (test count, time per tier, top-10 slowest modules)

Before = audit baseline (`main` @ `9c1bdd9`); post-step-2 = after PR #22;
after = final at `3bac35d`.

| Metric | Before (audit) | Post-step-2 | After (final) |
| --- | ---: | ---: | ---: |
| Tests | 3,678 | 3,716 | 3,516 |
| Serial wall | 1,659.7 s | — | — |
| Module-time sum | 1,840 s | 729.8 s | 812.2 s (full tier) |
| Fast tier | (none) | — | 42.5 s |
| Full tier wall | ~1,660 s | 185.5 s (`-j 4`) | 206.6 s (`-j 4`) |
| Drafting+synthesis 6 modules | 1,242 s | 72.5 s serial | — |

**Top-10 slowest modules (full tier, `tests/durations.json`):**

| # | Before (audit) | After (final) |
| ---: | --- | --- |
| 1 | `test_drafting_drafts` 674.7 | `test_cli_jobs` 93.0 |
| 2 | `test_drafting_workflow` 207.6 | `test_cli_snapshot` 82.5 |
| 3 | `test_drafting_workspace` 116.4 | `test_cli_human_gates` 66.5 |
| 4 | `test_cli_drafting` 91.7 | `test_cli` 53.0 |
| 5 | `test_drafting_package` 75.7 | `test_cli_gates` 45.5 |
| 6 | `test_synthesis_workflow` 59.2 | `test_step04_service_acceptance` 43.7 |
| 7 | `test_cli_jobs` 50.5 | `test_opencode_service` 29.8 |
| 8 | `test_cli_snapshot` 42.6 | `test_drafting_drafts` 27.6 |
| 9 | `test_step04_service_acceptance` 39.9 | `test_session_runner_acceptance` 25.2 |
| 10 | `test_cli_human_gates` 33.6 | `test_step03_acceptance` 16.5 |

Per-module durations: before = `/tmp/opencode/s16-durations-baseline.json`
(after step 2, before step 4), after = `/tmp/opencode/s16-final-full.json`.

## Moved tests (slow tier)

200 decorators across 58 modules. Grouped by module; the class/method named is
the decorated one.

| module | reason | marked classes | marked methods |
|---|---|---|---|
| test_acquisition_execution | other expensive (>=~1s) | — | test_forged_fields_are_rejected |
| test_acquisition_manifest | other expensive (>=~1s) | ManifestAssetTests | — |
| test_acquisition_pdf_inspection | other expensive (>=~1s) | — | test_hostile_scalar_types_never_raise |
| test_activation | other expensive (>=~1s) | — | test_release_cannot_race_accepted_write; test_release_cannot_race_draining_reconciliation |
| test_adapter_decision_projection | off-MVP-path (ledgers/session runner/execution boundary) | MissingAnswerProjectionTest; ProviderErrorProjectionTest; MalformedProjectionTest; MixedOutcomesProjectionTest; StrictMappingStillRefusedTest; WholeCallFailureProjectionTest; MissingEvidenceRefusedTest | — |
| test_cli | subprocess CLI | ProjectFlowTest; ArtifactFlowTest; ConfigCliTest; StrictJsonCliTest; OutputProtectionTest | test_registry_store_mismatch_is_denied; test_wheel_metadata_has_console_script_and_no_invented_license; test_rights_record_secret_is_not_echoed |
| test_cli_acquisition | subprocess CLI | — | test_import_replay_duplicate_and_readbacks |
| test_cli_acquisition_manifest | subprocess CLI | ManifestCliTests | — |
| test_cli_acquisition_retrieval | subprocess CLI | — | test_real_worker_writes_manual_required_report_without_get |
| test_cli_discovery_artifacts | subprocess CLI | PlanAmendmentTest | test_import_and_read_every_context_type |
| test_cli_discovery_reports | subprocess CLI | FreezeCliTest | — |
| test_cli_drafting | subprocess CLI | — | test_propose_inert_and_show_privacy_and_content_opt_in; test_package_safe_counts_and_content_opt_in; test_blueprint_propose_show_privacy_gate_and_historical; test_accepted_requires_own_gate_then_reads_approved; test_status_reports_stale_then_not_stale |
| test_cli_gates | subprocess CLI | GatePreparePreviewTest; GatePrepareDeterminismTest; GatePrepareDenialTest; GatePrepareSafetyTest | test_help_and_subcommand_dispatch |
| test_cli_human_gates | subprocess CLI | GateConfirmRecordsApprovalTest; GateConfirmRefusalTest; GateConfirmRealTerminalBoundaryTest; GateConfirmSafetyTest; GateNamedGateBoundaryTest | test_real_pty_confirmation_records_a_row |
| test_cli_jobs | subprocess CLI | RunAdmissionTest; PauseResumeCancelTest; AttachTest; SwitchBlockedTruthfulnessTest; ConcurrentProjectTest; ErrorAndSecretTest; InProcessMappingTest; LocalValidationTest; WatchBoundTest | test_lost_reply_then_exact_request_id_replay_is_one_job |
| test_cli_snapshot | subprocess CLI | FixtureLifecycleTest; FixtureCurrentIndicatorTest; FixtureSafetyTest; BackupRoundTripTest; BackupSafetyTest | — |
| test_cli_synthesis | subprocess CLI | — | test_propose_inert_and_show_privacy_and_gate_metadata; test_accepted_real_approved_chain_counts_and_content_opt_in |
| test_decision_commit | off-MVP-path (ledgers/session runner/execution boundary) | CompletedCommitTest; DefinitiveFailureCommitTest; AmbiguousOutcomeCommitTest; IdempotenceAndRefusalTest; PendingInertObservationTest; FaultSeamTest; SingleTransactionTest | — |
| test_decision_commit_backup | off-MVP-path (ledgers/session runner/execution boundary) | CompletedKnownCostRoundTripTest; CompletedUnknownCostRoundTripTest; DefinitiveFailureRoundTripTest; AmbiguousOutcomeRoundTripTest; CompletedReplayAfterRestoreTest; InertObservationRestoreTest; ReadonlyVerifyTest | — |
| test_decision_intent_backup | off-MVP-path (ledgers/session runner/execution boundary) | RoundTripTest; InnerValidationTest; SchemaCompatibilityTest; DynamicHookTest | — |
| test_decision_intents | off-MVP-path (ledgers/session runner/execution boundary) | RecordIntentTest; ForgedPlanTest; RefsTest; VersionsTest; TimestampAndRouteTest; IntegrityTest; IntegrityRepairTest; MigrationTest | — |
| test_decision_result_backup | off-MVP-path (ledgers/session runner/execution boundary) | RoundTripTest; InnerValidationTest; SchemaCompatibilityTest; DynamicHookTest | — |
| test_decision_results | off-MVP-path (ledgers/session runner/execution boundary) | RecordResultTest; IntegrityAndHistoryTest; RepairRegressionTest; MigrationTest | — |
| test_discovery_candidates | scale | DecisionIdScaleTests | — |
| test_discovery_expansion | other expensive (>=~1s) | — | test_request_budget_counts_every_retry_attempt; test_request_budget_interrupts_and_makes_no_further_request; test_before_request_is_invoked_before_every_retry; test_retry_then_refusal_keeps_real_attempts_and_no_fictitious_attempt |
| test_discovery_freeze | other expensive (>=~1s) | — | test_authentic_freeze_survives_backup_and_restore |
| test_discovery_job_routes | other expensive (>=~1s) | LeadRoutesTest | test_large_graph_status_is_summary_only |
| test_discovery_reconciliation | other expensive (>=~1s) | — | test_ten_thousand_records_scale |
| test_discovery_worker | detached worker / SIGKILL-recovery | — | test_detached_worker_admits_then_escalates_unapproved_plan |
| test_drafting_drafts | other expensive (>=~1s) | DraftGroundingTests; DraftStalenessAndHistoryTests; AssemblyOrderingTests; AssemblyLabelTests; AssemblyLineageTests; DraftOverRealRunnerTest | test_draft_is_stored_with_its_blueprint_and_package_snapshot; test_draft_payload_contract_is_exact |
| test_drafting_package | other expensive (>=~1s) | MetadataRejectionTests; DraftingOverRealRunnerTest | test_each_finding_gets_its_own_source_and_citation; test_key_is_stable_across_reopen |
| test_drafting_workflow | other expensive (>=~1s) | ProposeReadAndGateTests; StalenessAndHistoryTests | test_failed_propose_writes_nothing |
| test_drafting_workspace | other expensive (>=~1s) | ProposeAndReadTests; HistoryAndDivergenceTests; PerSectionAndOutlineAdvanceTests | test_reusing_one_mapping_for_both_slots_refused; test_section_must_exist_in_outline; test_payload_contract_is_exact |
| test_execution_boundary | off-MVP-path (ledgers/session runner/execution boundary) | ExecutionBoundaryTest | — |
| test_extraction_runner | other expensive (>=~1s) | FullFlowIntegrationTest | — |
| test_extraction_workflow | other expensive (>=~1s) | BatchAndAuditTests | — |
| test_opencode_service | OpenCode service | HttpBoundaryTests; RealServiceTests | test_start_uses_a_single_deadline_for_all_steps; test_descriptor_refuses_tampered_document_and_requires_live_instance; test_connection_refuses_changed_port_file; test_idempotent_start_refuses_drifted_config_or_dirty_tree |
| test_owner_auth | other expensive (>=~1s) | — | test_full_pty_output_queue_write_is_bounded_not_hanging |
| test_runtime_worker_arbitration | other expensive (>=~1s) | — | test_reserved_and_terminal_runtime_are_not_blocking |
| test_screening | other expensive (>=~1s) | — | test_real_gates_audit_replay_and_restart; test_false_exclusion_permanently_pauses_exact_batch |
| test_session_intent_backup | off-MVP-path (ledgers/session runner/execution boundary) | RoundTripStatesTest; HistoryAndReadOnlyTest; TamperRefusalTest; OrphanAndForeignTest; SchemaCompatibilityTest; DynamicHookTest | — |
| test_session_intents | off-MVP-path (ledgers/session runner/execution boundary) | RecordAndRestartTest; ReplayTest; RefusalTest; ForgedProjectionTest; HistoricalReadTest; CorruptionSweepTest; ImmutabilityAndUniquenessTest; MigrationTest | — |
| test_session_runner | off-MVP-path (ledgers/session runner/execution boundary) | SubmitSessionTurnTest; SubmitRefusalTest; ReconcileSessionTurnTest; SerialSlotArbitrationTest | — |
| test_session_runner_acceptance | off-MVP-path (ledgers/session runner/execution boundary) | RunnerE2ETests; TwoProjectIsolationTests; RestartMidTurnRunnerTests | — |
| test_session_turns | off-MVP-path (ledgers/session runner/execution boundary) | PrepareSessionTurnTest; ParentVerificationTest; DispatchBindingTest; MappingBindingTest; LiveReferenceTest; StepAndTextBoundsTest; VersionsBindingTest; CriticPlanTest; SpecSelfCheckTest; StrictForgeryTest; LocationReadOnlyPlanTest; CriticConfigurationSanityTest | — |
| test_step03_acceptance | detached worker / SIGKILL-recovery | SigkillRestartMatrixTest; SwitchWhileInflightTest; AmbiguityPinsActivationTest; BackupRestoreInteropTest | test_job_completes_after_submitter_process_exits; test_two_concurrent_project_submits_only_one_admits; test_unknown_reserve_retained_and_cap_blocks_new_admission |
| test_step04_service_acceptance | OpenCode service | RealServiceAcceptanceTests; RestartMidTurnTests | — |
| test_step05_acceptance | other expensive (>=~1s) | RealProcessAcceptanceTest | test_search_again_requires_authentic_gate_and_keeps_history; test_restore_and_reopen_reproduce_exact_snapshots_and_status |
| test_step05_cli_acceptance | subprocess CLI | LaterWorkTest; SearchAgainTest; NegativeGateTest; FakeInstructionAndPrivacyTest | test_end_to_end_cli_chain_freezes_exact_roster; test_all_known_present_still_never_claims_completeness |
| test_step05_core_acceptance | other expensive (>=~1s) | ExpansionInterruptedTest | — |
| test_step05_job_adversarial | other expensive (>=~1s) | — | test_kill_during_first_fake_post_before_response_never_reposts |
| test_step06_core_acceptance | other expensive (>=~1s) | Ac5SupplementIntegrityTests | — |
| test_step15_e2e_front | other expensive (>=~1s) | — | test_front_half_chain_and_visual_escalation |
| test_step15_e2e_manuscript | other expensive (>=~1s) | EndToEndManuscriptTests | — |
| test_step15_recovery | detached worker / SIGKILL-recovery | Step15CliExitRecoveryTest; Step15WorkerKillAndDedupTest | — |
| test_synthesis_workflow | other expensive (>=~1s) | ChainAndGateTests; ImpactTests | test_step09_outputs_gate_consumed |
| test_worker_engine | detached worker / SIGKILL-recovery | DetachedEntrypointTest | — |
| test_worker_process | detached worker / SIGKILL-recovery | WorkerProcessTest | — |

## Merged tests

`subTest`-parameterized collapse of eight table-style modules (§4c above):
`test_jev_adapter`, `test_decision_records`, `test_records`,
`test_opencode_client`, `test_lead_adapter`, `test_acquisition_validation`,
`test_agent_roles`, `test_gate_snapshots`. Every original case is preserved as a
subTest; **no behaviour coverage was dropped**.

## Deleted tests

- `test_execution_boundary.py::ExecutionBoundaryTest::test_production_routes_are_blocked`
  (`f543571`) — exercised only the deleted dead function
  `assert_production_execution_supported`. Its invariant remains asserted in
  `test_step05_core_acceptance`. This is the **only** deleted docs-cited AC test.

## Security / fail-closed coverage (fast tier)

Each name was verified present in the fast tier with `grep -n`:

| Invariant | Test(s) |
| --- | --- |
| Gates cannot be bypassed | `test_discovery_serialization.TypeAndInvariantTests.test_complete_semantics_cannot_be_bypassed`; `test_drafting_workflow.OwnerGateIsolationTests` |
| No secret leakage | `test_cli.LeakBoundaryTest.test_unknown_command_does_not_echo_secret`; `test_worker_ipc.WorkerIPCTest.test_error_response_does_not_reflect_secret` |
| One active project | `test_registry.RegistryTest.test_single_active_arbitration_and_status`; `test_acquisition_worker.StopAndArbitrationTest.test_one_active_project_and_hold_guards` |
| No duplicate accepted result | `test_discovery_worker.ReplayTest.test_matching_replay_no_duplicate_and_divergent_conflicts`; `test_runtime_dispatch.ReopenTest.test_state_and_exposure_survive_reopen_and_replay_is_idempotent` |
| Stale approval detection | `test_approvals.StalenessTest.test_target_edit_makes_approval_stale`; `test_human_approvals.DenialTest.test_stale_prepared_after_target_edit_denies`; `test_validation_memo_lower` / `test_validation_memo_upper` |

## Files and public interfaces changed

- `src/review_article_agent/storage/opmemo.py` (new): `memoize(conn, key,
  compute)`, `invalidate(conn)`, `stats(conn)` — transaction-scoped validation
  memo.
- `src/review_article_agent/domain/records.py`: fast paths in `_has_surrogate`,
  `_check_jsonable`, `validate_identifier` (no API change).
- `src/review_article_agent/storage/schema.py`: `RAA_TEST_SQLITE_SYNCHRONOUS`
  hook + `resolve_synchronous()` / `common_pragmas(synchronous=...)`; production
  default unchanged (`FULL`).
- `src/review_article_agent/runtime/execution_boundary.py`: removed
  `assert_production_execution_supported`, `ProductionExecutionBlocked`,
  `_PRODUCTION_BLOCKERS`.
- Memo application in `storage/human_approvals.py`, `discovery/freeze.py`,
  `acquisition/manifest.py`, `extraction/workflow.py`, `synthesis/workflow.py`,
  `drafting/package.py`, `drafting/workspace.py`, `drafting/workflow.py`,
  `runtime/gates.py` (no public API change).
- `.github/workflows/ci.yml`, `scripts/run_tests.py` (CLI: `-j/--jobs`,
  `--slow`, `--durable-sqlite`, `--budget`, `--top`, `--timeout`,
  `--durations-out`), `scripts/test_shards.py` (CLI: `--shards`, `--index`,
  `--plan`, `--format`, `--merge`, `--durations`, `--tests-dir`),
  `tests/durations.json`, `docs/ci.md`, `README.md`.
- `tests/_tiers.py` (new): `slow` gate + `RAA_SLOW_TESTS`.
- Test-only: `test_storage_opmemo`, `test_validation_memo_lower`,
  `test_validation_memo_upper`, `test_migrations`, `scripts/test_run_tests.py`,
  `scripts/test_test_shards.py`, and the collapses/deletions above.

## Commands run and results

| Command | Result |
| --- | --- |
| `python3 scripts/run_tests.py` (fast tier, 3.14.4, 4 CPUs) | 126 modules, **3,516 tests, 0 failures, 670 skipped, wall 42.5 s** |
| `python3 scripts/run_tests.py --slow` (full tier) | 126 modules, **3,516 tests, 0 failures, 9 skipped, wall 206.6 s** |
| `python3 scripts/check_contract_docs.py` | **15 ok, 0 failed** — OK |
| `git diff --check` | clean |
| `python3 scripts/run_tests.py --budget 1` | exits 1 (budget guard proven) |

Not run here: CI jobs (already green on PRs #21/#22), the gated release checks,
and any live model call.

## Stubs, limitations, deviations

- **Step 2 deviation (explicit):** the kickoff target "drafting + synthesis well
  under a minute" was **not met** — achieved 72.5 s serial (~25 s parallel). The
  remaining cost is one chain validation per operation, which is the specified
  per-operation design; going further would need a cross-operation cache, which
  the kickoff forbids. **Met in spirit; the literal target is not met.**
- **Step 4 test-count deviation (explicit):** final count **3,516**, not the
  ≤ ~1,000 aim. Only the eight listed table-style modules were collapsed
  (−200 methods, all cases preserved). Reaching ~1,000 would require deleting
  behaviour coverage, left for a user decision.
- **Not done:** in-process conversion of subprocess CLI tests and replacing
  `time.sleep` polling. Those tests were **moved to the slow tier** instead
  (kickoff: "prefer moving expensive but meaningful tests to the slow tier"),
  and the time targets are met without it.
- **Under heavy CPU contention** (two full suites at once) a worker observed
  timeouts in `test_cli_jobs.AttachTest`; **not reproduced** in clean runs —
  watch CI.
- **Open user action:** enable branch protection on `main` requiring `fast` and
  `full`.
- The stale per-test timing annotations used while building the moved-tests table
  are not reproduced here; the module-level durations above are authoritative.

## Costs

**USD 0.** No live model calls, no provider/network egress; all tests use
deterministic fixtures.

## Cleanup

Task worktrees under `review_article_agent-worktrees/s16-*` are disposable once
their branches are merged.

## Next actions

1. User enables branch protection on `main` requiring `fast` and `full`.
2. Coordinator reviews/merges the step-4 PR (`test/suite-diet`).
3. Then kickoff steps 5–10 (the user authorized one small live OpenRouter smoke
   test for step 5).

## Commit IDs

| Commit | Work |
| --- | --- |
| `0924718`, `5fe887c` | S16-D1 — `RAA_SLOW_TESTS` slow tier; MVP/memo tests kept fast |
| `be98023` | S16-D2 — test-only SQLite synchronous hook |
| `478fabe` | S16-D4 — collapse `test_opencode_client`/`test_records`/`test_lead_adapter` |
| `35ab69f` | S16-D3 — collapse `test_jev_adapter`/`test_decision_records` |
| `b904729` | S16-D5 — collapse `test_acquisition_validation`/`test_agent_roles`/`test_gate_snapshots` |
| `f543571` | S16-D6 — delete dead `execution_boundary` path and its tests |
| `dc5baac` | S16-D7 — fast-tier CI budget, refreshed shard durations, test-tier docs |
| `cfd970b` | Step 2 merge (PR #22; head `8ece47f`) |
| `27a74f2` | Step 3 merge (PR #21; final head `6242879`) |
| `3bac35d` | `test/suite-diet` HEAD when this handoff was written |

## Steps 5–10

Kickoff steps 5–10 (`docs/implementation/kickoffs/16-mvp-hardening.md`) were
implemented across merged PRs #25–#28 on `origin/main` and the `feat/pipeline-run`
branch. Every fact below was verified against git (`git log --oneline --merges
origin/main`, `git log --oneline`, per-commit diffs), the code, and the docs.

### Scope

- **Step 5 — one real model port:** a generic direct-OpenRouter generation
  adapter plus per-role model-backed adapters, wired to the existing hosted-use /
  rights / budget gates (no parallel authorization path).
- **Step 6 — `pipeline run` orchestrator:** `pipeline status` / `pipeline run`.
- **Step 7 — real PDF text extraction** through the optional `pypdf` extra.
- **Step 8 — data seams:** bibliography metadata, extraction→synthesis, the
  tracing acquisition port, and recomputed revalidation.
- **Step 9 — CLI groups and worker jobs:** `review` / `tracing` / `delivery`
  groups plus the extraction and generation worker jobs.
- **Step 10 — housekeeping:** README, `docs/release.md` matrix, plan ticks, and
  this handoff.

### PRs and merge commits

Each merged PR was merged **only after all CI checks passed at its head**
(verified through the GitHub check rollup via `gh pr view`; the `release` job is
correctly `SKIPPED`, and `fast (py3.11)` / `fast (py3.14)`, the 6 py3.14 `full`
shards, the 2 py3.11 `full` shards, and `full-durations` are all `SUCCESS`).

| PR | Branch | Merge commit | Head | Merged (UTC) |
| --- | --- | --- | --- | --- |
| #25 | `feat/cli-worker-jobs` | `8ac7c36` | `40995ca` | 2026-10-30T12:49:43Z |
| #26 | `feat/pdf-text` | `795a13b` | `d02dd02` | 2026-10-30T12:56:20Z |
| #27 | `feat/data-seams` | `798c717` | `d6cf438` | 2026-10-30T13:07:32Z |
| #28 | `feat/model-port` | `40b1135` | `b93119a` | 2026-10-30T13:15:11Z |

- **#25 `feat/cli-worker-jobs`** (step 9): the extraction worker job
  `extraction_run` (`worker/extraction.py`, `cli/extraction.py`) running
  `extraction.runner.execute_run` in the detached worker; clearer CLI errors for
  a non-owner-private data dir / project root (0700) and for a `backup create`
  destination inside `--data-dir` (`cli/main.py`, `domain/errors.py`,
  `storage/paths.py`).
- **#26 `feat/pdf-text`** (step 7): isolated `pypdf` text extraction
  (`parsing/pdf_adapter.py`, `parsing/_pdf_text_child.py`) producing `parsed` PDF
  reports with 1-based page locators; re-parse verification; a verified `parsed`
  PDF feeds grounded extraction (`extraction/runner._derive_material`); CI
  installs `pypdf==6.21.4`.
- **#27 `feat/data-seams`** (step 8): `MetadataRecord` authors/journal carried
  through the discovery codec v2 (backward-compatible decode of v1 records);
  drafting citations carry real bibliographic metadata (`drafting/package.py`);
  `bibliography_from_packages` (`delivery/export.py`); the Step 13 tracing
  acquisition port (`tracing/acquisition_port.py`) plus real claims/sources
  derivation (`tracing/inputs.py`); recomputing `DeliveryRepository.revalidate`.
- **#28 `feat/model-port`** (step 5): decision 005
  (`docs/decisions/005-generative-roles-direct-route.md`); the generic adapter
  `runtime/generation.py`, the per-role hosted-use document `generation/hosted_use.py`,
  and the application-boundary runner `generation/runner.py`; role adapters
  synthesis/framework/outline (`generation/synthesis_roles.py`), blueprint/
  section_writer with the additive `direct_hosted_use` workspace binding
  (`generation/drafting_roles.py`), section_critic/section_repair
  (`generation/review_roles.py`), global_reviewer/global_writer/notes_planner
  (`generation/delivery_roles.py`); the Jev screening runner
  (`screening/runner.py`) with `verified_dispatch` provenance.

### `feat/pipeline-run` (steps 6 and 9; PR #<pending>)

Branch `feat/pipeline-run`, head `0300299`. The coordinator opens the PR.

- `pipeline status` / `pipeline run` (`pipeline/status.py`, `pipeline/run.py`,
  `cli/pipeline.py`).
- `generation run submit|status` worker job `generation_run`
  (`worker/generation.py`, `cli/generation.py`) — **all 9 operations**:
  `synthesis`, `framework`, `outline`, `blueprint`, `draft`, `section_review`,
  `global_review`, `notes_plan`, `screening_case`.
- `generation hosted-use propose|show` (`generation/hosted_use.py`).
- `review` / `tracing` / `delivery` CLI groups (`cli/review.py`,
  `cli/tracing.py`, `cli/delivery.py`).
- Owner-authenticated final-approval named gate `artifact_revision` /
  `delivery.final_approval`, plus a re-deriving `delivery export`
  (`cli/delivery.py`).
- Durable post-review manuscript recorded at `delivery start`
  (`delivery/manuscript.py`, `DeliveryRepository.start_global_review`).
- End-to-end test `tests/test_mvp_pipeline_e2e.py`: real extraction receipts →
  repeated `pipeline run` + scripted approvals → final approval → export of **7
  files** (`manuscript.md`, `references.bib`, `references.ris`, `companion.md`,
  `summary.md`, `ai_declaration.md`, `export_manifest.json`) with real BibTeX/RIS
  metadata; the manual (non-`pipeline run`) steps are listed in its docstring.

### Files and public interfaces changed (steps 5–10)

- `runtime/generation.py` (generic direct-OpenRouter generation adapter; `ROLES`),
  `generation/hosted_use.py` (per-role `generation_hosted_use` document +
  `generation.hosted_use.<role>` named gate), `generation/runner.py`
  (`execute_generation_task`, `get_generation_outcome`).
- `generation/synthesis_roles.py`, `generation/drafting_roles.py`,
  `generation/review_roles.py`, `generation/delivery_roles.py` (role ports).
- `screening/runner.py` (`execute_screening_case`).
- `parsing/pdf_adapter.py`, `parsing/_pdf_text_child.py`, `parsing/repository.py`
  (PDF text extraction with page locators).
- `discovery/records.py` (`MetadataRecord` authors/journal),
  `discovery/serialization.py` (codec v2, backward-compatible),
  `discovery/pubmed.py` / `discovery/europepmc.py` (metadata carry-through),
  `drafting/package.py` / `drafting/schema.py` (real citation metadata),
  `delivery/export.py` (`bibliography_from_packages`),
  `tracing/acquisition_port.py` (new), `tracing/inputs.py` (new),
  `delivery/workflow.py` (recomputing `revalidate`).
- `worker/extraction.py` (`extraction_run` job), `cli/extraction.py`,
  `cli/main.py` (private-path/backup CLI errors), `domain/errors.py`,
  `storage/paths.py`, `storage/registry.py`.
- `pipeline/status.py`, `pipeline/run.py`, `cli/pipeline.py`,
  `worker/generation.py`, `cli/generation.py`, `cli/review.py`, `cli/tracing.py`,
  `cli/delivery.py`, `delivery/manuscript.py`.
- `docs/decisions/005-generative-roles-direct-route.md`, `docs/generation.md`,
  `docs/release.md`, `README.md`, `docs/implementation/README.md`,
  the kickoff plan, and this handoff.
- Test-only: `tests/test_mvp_pipeline_e2e.py`, `tests/test_extraction_worker.py`,
  `tests/test_parsing_pdf_adapter.py`, `tests/test_parsing_repository.py`,
  `tests/test_tracing_acquisition_port.py`, `tests/test_tracing_inputs.py`,
  `tests/test_delivery_export.py`, `tests/test_delivery_workflow.py`,
  `tests/test_discovery_serialization.py`, `tests/test_discovery_pubmed.py`,
  `tests/test_discovery_europepmc.py`, `tests/test_cli_private_paths.py`, and the
  generation-role worker/CLI tests.

### Evidence per kickoff item (§5–§10)

- **§5 (one real model port)** — **met (locally, fake transport).** One generic
  adapter (`runtime/generation.py`) backs every model role; the per-role
  hosted-use gate reuses the existing `gate prepare` / `gate confirm` path
  (no parallel authorization); model output stays a proposal through the
  existing validators/gates; tests use recorded/fake transports. Decision 005
  records the architecture direction.
- **§6 (`pipeline run` orchestrator)** — **met (locally).** `pipeline status` is
  read-only and reports a stage complete only when the stage's own `require_*`
  check passes; `pipeline run` submits the next job(s) and stops at the next
  human gate / budget / pause / failure, and is idempotent (deterministic request
  ids). `tests/test_mvp_pipeline_e2e.py` drives a synthetic project end-to-end by
  repeated `pipeline run` + scripted approvals.
- **§7 (real PDF text extraction)** — **met (locally, optional extra).**
  `parsing/pdf_adapter.py` yields per-page passages with page locators and keeps
  the `needs_review` state for low-quality/empty text; a verified `parsed` PDF
  feeds grounded extraction.
- **§8 (data seams)** — **met (locally).** Real frozen-corpus metadata flows to
  the drafting citations and the export bibliography; synthesis consumes real
  Step 09 receipts; the Step 13 `acquire` port resolves over already-acquired
  sources (no network); Step 14 `revalidate` recomputes the Step 12/13 checks.
- **§9 (CLI groups and worker jobs)** — **met (locally).** Thin `review` /
  `tracing` / `delivery` groups with a real `delivery export`; the
  `extraction_run` and `generation_run` worker jobs; the two CLI error fixes.
- **§10 (housekeeping)** — **met.** README top section refreshed, `docs/release.md`
  matrix updated, plan ticks updated where acceptance passes, and this handoff.

### Commands run and results (this branch, `0300299`)

Environment: Python 3.14.4, Linux, 4 CPUs.

| Command | Result |
| --- | --- |
| `python3 scripts/run_tests.py -j 4` (fast tier) | 145 modules, **3,857 tests, 0 failures, 0 errors, 706 skipped, wall 56.7 s** |
| `python3 scripts/run_tests.py --slow -j 4` (full tier) | 145 modules, **3,857 tests, 0 failures, 0 errors, 17 skipped, wall 294.4 s** |
| `python3 scripts/check_contract_docs.py` | **15 ok, 0 failed** — OK |
| `git diff --check` | clean |

Full-tier slowest modules include `test_cli_jobs` 126.2 s, `test_cli_snapshot`
117.4 s, `test_cli_human_gates` 93.4 s, `test_pipeline_run` 76.0 s,
`test_mvp_pipeline_e2e` 64.7 s.

Not run here: any live model call, the CI jobs (already green on PRs #25–#28),
and the gated release checks.

### Deviations, stubs, and limits

- **Live model validation NOT run.** `OPENROUTER_API_KEY` was not available to
  the coordinator; the user's authorized small live smoke test remains pending.
  Every model path is verified with fake transports only.
- **Framework `role` / `scope` aliased for the model.** The framework schema's
  `concept.role` / `relationship.scope` keys collide with reserved names in a
  stored proposal, so the model-facing keys are non-colliding aliases mapped back
  to the schema field names (`generation/synthesis_roles.py`).
- **Screening runner stores per-question artifacts.** `execute_screening_case`
  runs one screening case per decision.
- **Multi-source sentences traced against the first source.** A drafted sentence
  citing several sources is traced against its first (sorted) cited source — a
  disclosed limitation (`tracing/inputs.py`).
- **Revalidation origin coverage is whole-state.** Step 14 `revalidate`
  recomputes the Step 12/13 checks over the whole delivery state.
- **Delivery `final_approval(...)` / `export(...)` library methods remain for
  tests.** The gate-authoritative path is the CLI (`delivery approve` /
  `delivery export`).
- **Manual steps remain** (not automatic): brief/plan, screening pilot,
  extraction config/pilot/audit, hosted-use approvals, the freshness decision,
  and every gate.

### Repairs

S17-O4 found that the pipeline never recorded a delivery manuscript: the delivery
global-review job read a `manuscript` key that was never set, so the export was
impossible while `pipeline status` falsely reported complete after final
approval. Fixed in `0300299` on the **first repair attempt** (`delivery/manuscript.py`
records the durable post-review manuscript at `delivery start`; `pipeline/status.py`
marks delivery complete only after a successful export).

### Costs

**USD 0.** No live model calls and no provider/network egress; all tests use
deterministic fixtures.

### Cleanup

Task worktrees under `review_article_agent-worktrees/s16-*` and
`s16-handoff2` are disposable once their branches are merged.

### Next actions

1. Coordinator opens the `feat/pipeline-run` PR and merges it after CI is green.
2. User runs the authorized small live OpenRouter smoke test.
3. Human review/merge of `feat/pipeline-run`; then the unticked steps `07` /
   `08` / `15` remain for the user's decision.

### Commit IDs (this part)

| Commit | Work |
| --- | --- |
| `8ac7c36` | Step 9 merge (PR #25; head `40995ca`) — extraction worker job + CLI errors |
| `795a13b` | Step 7 merge (PR #26; head `d02dd02`) — real PDF text extraction |
| `798c717` | Step 8 merge (PR #27; head `d6cf438`) — data seams |
| `40b1135` | Step 5 merge (PR #28; head `b93119a`) — one real model port |
| `0473c3f` | `pipeline status` read-only resolver (kickoff 16 §6) |
| `27952bd` | `review`/`tracing`/`delivery` CLI groups + real export (kickoff 16 §9) |
| `0a5b570` | `generation_run` worker job + `generation run` CLI (kickoff 16 §9) |
| `b8194fa` | delivery final-approval gate + re-deriving export (kickoff 16 §9) |
| `3d89a4f` | wire global_review/notes_plan/screening_case + hosted-use CLI (kickoff 16 §5/§9) |
| `6d55de3` | `pipeline run` orchestrator + hosted-use/budget blockers (kickoff 16 §6) |
| `d915c1d` | `pipeline run` drives every machine stage (kickoff 16 §6) |
| `f670aee` | housekeeping: README, release matrix, plan ticks (kickoff 16 §10) |
| `6e81e03` | `pipeline run` end-to-end test (kickoff 16 §6/§8) |
| `0300299` | repair: record the reviewed manuscript at `delivery start` (S17-O4) |
| `<this handoff commit>` | extend this handoff to steps 5–10, fix README §08, tick step 10 |
