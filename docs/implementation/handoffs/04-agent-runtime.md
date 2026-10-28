# Handoff — step 04: restricted agent roles, model adapters, and approvals

**Status: COMPLETE (restricted runtime).** Step 04 is **ticked** in
[`docs/implementation/README.md`](../README.md). Every original deliverable and
acceptance criterion has concrete evidence, and the coordinator's **final frozen
full-repository run passed at `c1f927b`** (see §6.3). Step 04 completes as a
**restricted runtime validated against a real app-owned service and a fixture
model** — it is **not** a scientific or paid-generation milestone.

**Scope boundary (authority).** The step document
[`docs/implementation/04-agent-runtime.md`](../04-agent-runtime.md) is
authoritative; its deliverables, acceptance criteria (AC1–AC7), and
handoff/exclusions were **not** edited. The accepted policy/technical direction is
[decision 002](../../decisions/002-step01-contracts-and-policies.md) and the
accepted runtime language/integration is
[decision 001](../../decisions/001-runtime-language.md); the versioned contracts
are [C01 v1.4](../../contracts/01-identities.md),
[C02 v3.2](../../contracts/02-gates-and-runtime.md),
[C03 v1](../../contracts/03-policy-decisions.md). The confirmed short replan
`finish-step-04-with-the-smallest-runtime-integration` is an **execution replan**,
not a replacement specification. Steps 05–15 scientific workflows are out of
scope. No baseline invariant, acceptance criterion, or policy was weakened.

> **Evidence distinction.** Each targeted log below is the coordinator's run at
> its **own** revision (paths are provenance, with no runtime dependency); they are
> **not** all at one SHA. The frozen full-core result (1,776 tests at `7ef7a07`) is
> an **older, historical** checkpoint. The **final full run passed at `c1f927b`**
> (§6.3); this completion update is a **docs-only** change made after that run, and
> no full run is claimed at this update's own (still-unknown) commit.

## 1. Scope, base revisions, and authority used

- **Step document (authoritative):**
  [`04-agent-runtime.md`](../04-agent-runtime.md) — deliverables, AC1–AC7, and
  handoff/exclusions unedited.
- **Baseline:** [`docs/specification.md`](../../specification.md) (confirmed; not
  weakened).
- **Step 04 scope base:** the Step 03 implementation `2530955`, merged as
  `c9ee4e69a981b101e59e2c61330c5906105817d1`.
- **Integrated product code:** `8626cee`
  (`8626cee9e3c511a26a899abf21b6da2c2c0d943a`); the docs-only handoff commit is
  `c1f927b` (this handoff, integrated).
- **This handoff branch:** `task/step04-final-handoff`. It changes
  **documentation only** (see §9).

## 2. Deliverables → implementation and tests

| Step deliverable | Implementation modules | Tests |
| --- | --- | --- |
| App-owned, version-pinned OpenCode service integration; project/location/session mappings | [`runtime/service.py`](../../../src/review_article_agent/runtime/service.py), [`runtime/opencode.py`](../../../src/review_article_agent/runtime/opencode.py), [`storage/agent_runtime.py`](../../../src/review_article_agent/storage/agent_runtime.py) | [`test_opencode_service.py`](../../../tests/test_opencode_service.py), [`test_opencode_client.py`](../../../tests/test_opencode_client.py), [`test_agent_runtime_store.py`](../../../tests/test_agent_runtime_store.py), [`test_step04_service_acceptance.py`](../../../tests/test_step04_service_acceptance.py) |
| Role configuration; bounded task submission; admission/result reconciliation; captured model/usage metadata | [`runtime/roles.py`](../../../src/review_article_agent/runtime/roles.py), [`runtime/session_turns.py`](../../../src/review_article_agent/runtime/session_turns.py), [`runtime/session_runner.py`](../../../src/review_article_agent/runtime/session_runner.py), [`runtime/dispatch.py`](../../../src/review_article_agent/runtime/dispatch.py), [`storage/session_intents.py`](../../../src/review_article_agent/storage/session_intents.py) | [`test_agent_roles.py`](../../../tests/test_agent_roles.py), [`test_session_turns.py`](../../../tests/test_session_turns.py), [`test_session_runner.py`](../../../tests/test_session_runner.py), [`test_session_runner_acceptance.py`](../../../tests/test_session_runner_acceptance.py), [`test_session_intents.py`](../../../tests/test_session_intents.py), [`test_runtime_dispatch.py`](../../../tests/test_runtime_dispatch.py) |
| Restricted scientific tools / proposal-only interface (no authoritative approvals) | [`runtime/roles.py`](../../../src/review_article_agent/runtime/roles.py), [`runtime/execution_boundary.py`](../../../src/review_article_agent/runtime/execution_boundary.py) | [`test_agent_roles.py`](../../../tests/test_agent_roles.py), [`test_execution_boundary.py`](../../../tests/test_execution_boundary.py), [`test_step04_service_acceptance.py`](../../../tests/test_step04_service_acceptance.py) |
| Jev decision adapter with validated outputs and durable records; direct fallback | [`runtime/jev.py`](../../../src/review_article_agent/runtime/jev.py), [`runtime/decision_records.py`](../../../src/review_article_agent/runtime/decision_records.py), [`storage/decision_intents.py`](../../../src/review_article_agent/storage/decision_intents.py), [`storage/decision_results.py`](../../../src/review_article_agent/storage/decision_results.py) | [`test_jev_adapter.py`](../../../tests/test_jev_adapter.py), [`test_decision_records.py`](../../../tests/test_decision_records.py), [`test_decision_intents.py`](../../../tests/test_decision_intents.py), [`test_decision_results.py`](../../../tests/test_decision_results.py), [`test_decision_commit.py`](../../../tests/test_decision_commit.py) |
| App-owned human approvals presented through the CLI | [`runtime/gates.py`](../../../src/review_article_agent/runtime/gates.py), [`runtime/owner_auth.py`](../../../src/review_article_agent/runtime/owner_auth.py), [`storage/human_approvals.py`](../../../src/review_article_agent/storage/human_approvals.py), [`cli/gates.py`](../../../src/review_article_agent/cli/gates.py) | [`test_gate_snapshots.py`](../../../tests/test_gate_snapshots.py), [`test_human_approvals.py`](../../../tests/test_human_approvals.py), [`test_cli_gates.py`](../../../tests/test_cli_gates.py), [`test_cli_human_gates.py`](../../../tests/test_cli_human_gates.py) |

## 3. The session runner API boundary (what was actually built)

The one vertical execution path is a **thin application-facing library**, not a
new orchestration framework, scheduler, worker, or CLI
([`runtime/session_runner.py`](../../../src/review_article_agent/runtime/session_runner.py)):

- `submit_session_turn(store, *, registry, activation_token, task, dispatch, manager, text, versions, client=None)`
  creates one fresh owned-location session for a persisted, reserved
  `opencode_session` dispatch, persists the exact app-owned mapping and the
  **pre-transport** session-turn correlation, revalidates the fixture-only
  execution boundary and the live session pins, then records the single-use send
  claim and admits the deterministic application message. It returns the durable
  **admission without waiting** for completion.
- `reconcile_session_turn(store, *, registry, activation_token, dispatch_id, manager, client=None)`
  is stateless: it reads the exact persisted intent, proves the current owned
  service/location and session pins, reconciles from **durable server messages
  (never events)**, and only an authoritative idle success may persist a bounded
  proposal and accept the dispatch's single local result.

Boundaries kept: the caller supplies the activation token; the manager and the
registered store/tasks/dispatches already exist; there is **one external serial
slot**; the app-owned service owns the bounded turn **after the submitting process
exits**; reconciliation is an **explicit application call**, **not** an automatic
workflow daemon. **No scientific autonomous workflow is implemented at all** —
there is no run surface (and no upcoming run) beyond the human-gated library
calls. There is **no** new model-task CLI and **no** detached executor. The Step 03
worker remains a **synthetic** executor but now sees runtime-outstanding /
switch-blocking arbitration; it does not natively schedule model tasks.

## 4. Storage versions and migrations

| Format | Version | Where | Note |
| --- | --- | --- | --- |
| Project store schema | **10** | [`storage/schema.py`](../../../src/review_article_agent/storage/schema.py) `STORE_SCHEMA_VERSION` | adds 5 runtime mappings, 6 dispatch/cost ledger, 7 human approvals, 8 decision intents, 9 decision-result observations, 10 session-turn intents |
| Registry schema | **2** | `REGISTRY_SCHEMA_VERSION` | token-fenced execution activation (Step 03) |
| Private backup format | **1** | [`storage/backup.py`](../../../src/review_article_agent/storage/backup.py) manifest `format_version` | **accepts store schemas 3..10**; a newer schema is refused |

- Migrations are **strictly additive**: versions 1..9 are unchanged; version 10
  adds the new append-only `session_turn_intent` table (keyed by its
  `opencode_session` dispatch). Prior rows are never rewritten; a newer schema is
  refused cleanly.
- A backup/restore **preserves the exact recorded correlation** (session/message/
  admission pins, accepted `result_kind`/`result_ref`/`result_digest` triple, and
  an `unknown` dispatch's reserve). Restore **never** executes, resumes, retries,
  or promotes a turn — it is correlation, not execution.

## 5. Acceptance criteria — criterion-by-criterion evidence

Step document AC1–AC7. Names are `tests/<module>::<Class>::<test>`.

| AC | Requirement (short) | Concrete tests | Status |
| --- | --- | --- | --- |
| 1 | An agent cannot approve its own artifact, edit authoritative state, widen scope, or invoke unapproved tools via retrieved instructions | `test_agent_roles.py::ProposalBoundaryTests::test_payload_cannot_supply_actor_approval_authority_model_or_scope`, `::test_nested_content_injection_rejected`, `::test_generic_permission_payload_cannot_become_approval`, `::test_does_not_widen_or_repin_task`, `::test_provenance_comes_from_trusted_config_not_payload`; `test_agent_roles.py::BoundedTaskTests::test_rejects_authority_actor_approval_budget_tool_keys`; `test_step04_service_acceptance.py::RealServiceAcceptanceTests::test_synthetic_retrieved_text_cannot_change_state`, `::test_adversarial_tool_call_is_never_executed`, `::test_fixture_is_offered_no_dangerous_tools`, `::test_builtin_permission_actions_denied_by_public_permission_api`, `::test_policy_denies_enforced_by_public_permission_api`; `test_execution_boundary.py::ExecutionBoundaryTest::test_declared_boundary_is_non_authorizing`, `::test_production_routes_are_blocked` | Met |
| 2 | A saved generic tool permission cannot satisfy an outline/pilot/exclusion approval | `test_step04_service_acceptance.py::RealServiceAcceptanceTests::test_real_saved_always_grant_cannot_override_configured_deny` (real durable saved `always` grant refused as a capability by the app approval repo; zero rows), `::test_generic_permission_is_not_app_human_approval`; `test_gate_snapshots.py::PrepareTest::test_generic_saved_permission_and_in_band_authority_rejected`; `test_human_approvals.py::DenialTest::test_imported_permission_mapping_as_prepared_denies`, `::test_forged_flags_and_imported_permission_deny`; `test_cli_gates.py::GatePrepareSafetyTest::test_saved_permission_payload_and_actor_flags_are_rejected`; `test_cli_human_gates.py::GateNamedGateBoundaryTest::test_a_generic_tool_permission_is_never_a_gate_kind` | Met |
| 3 | Different-model section critic uses a fresh context and a different concrete model ID; invalid configuration fails rather than silently reusing the writer model | `test_agent_roles.py::SectionCriticTests::test_distinct_model_and_fresh_context_plan`, `::test_same_provider_and_id_rejected_even_with_different_variant`; `test_session_runner.py::SubmitSessionTurnTest::test_critic_uses_a_fresh_different_concrete_model_session`, `::test_invalid_critic_configuration_is_refused_before_creating_a_session`; `test_session_runner_acceptance.py::RunnerE2ETests::test_fresh_critic_different_model_and_invalid_same_model_refused_before_prompt`; `test_step04_service_acceptance.py::RealServiceAcceptanceTests::test_fresh_critic_different_model_and_invalid_same_model` | Met |
| 4 | Lost event streams reconcile from durable state/messages; missing events do not lose completion or approval actions | `test_session_runner.py::ReconcileSessionTurnTest::test_new_instance_reconciles_completed_with_exact_metadata`; `test_session_runner_acceptance.py::RunnerE2ETests::test_child_submits_admission_then_separate_process_reconciles_one_proposal`, `::RestartMidTurnRunnerTests::test_restart_mid_turn_reconciles_unknown_and_blocks_new_dispatch`; `test_step04_service_acceptance.py::RealServiceAcceptanceTests::test_submission_receipt_is_not_completion`, `::test_second_process_reconciles_completed_without_events`, `::RestartMidTurnTests::test_owned_restart_mid_turn_is_unknown_not_false_completion`; `test_runtime_dispatch.py::StateTransitionTest::test_submitted_becomes_unknown_on_restart_and_is_not_retried`; `test_session_intents.py::RecordAndRestartTest::test_records_immutable_correlation_and_reloads_after_restart`; `test_human_approvals.py::ReopenTest::test_approval_survives_reopen` | Met |
| 5 | Model/provider errors, refusals, invalid shapes, and partial Jev errors are not interpreted as scientific exclusions | `test_jev_adapter.py::PerQuestionFailureTests::test_never_an_exclusion`, `::test_refused_answer_is_refusal`, `::test_provider_error_answer_retained`, `::test_choice_outside_options_is_malformed_not_protocol`, `::test_score_outside_levels_is_malformed`; `test_decision_records.py::OutcomeRetentionTest::test_refusal_and_malformed_and_unsupported_retain_candidate`; `test_decision_commit.py::CompletedCommitTest::test_per_question_failures_retained_and_never_exclusion`; `test_session_runner.py::ReconcileSessionTurnTest::test_refusal_success_never_becomes_a_result`, `::test_malformed_model_success_never_becomes_a_result`, `::test_failed_authoritative_terminal_marks_failed_without_a_result` | Met |
| 6 | Stateless decision responses are persisted with rubric/model/input versions; direct calls accounted separately | `test_decision_intents.py::RecordIntentTest::test_records_exact_intent`, `::VersionsTest::test_version_hash_mismatch_refused`; `test_decision_records.py::ProjectionBasicsTest::test_version_digests_preserved_not_invented`, `::test_provider_metadata_recorded_from_trusted_result`, `::CostAndLedgerTest::test_two_questions_share_cost_counted_once`; `test_decision_results.py::RecordResultTest::test_records_complete_set_and_rows`, `::test_single_direct_charge_identical_across_rows`; `test_decision_commit.py::CompletedCommitTest::test_completed_http_200_accepted_once_and_cost_settled`; `test_runtime_dispatch.py::CostLedgerTest::test_route_cost_separation`, `::SharedCapsTest::test_combined_budget_usage_reports_both`; `test_session_runner.py::ReconcileSessionTurnTest::test_all_assistant_steps_costs_are_summed_exactly`, `::test_missing_earlier_step_cost_reports_unknown_total_not_final_only` | Met |
| 7 | Project/session/model isolation holds across two saved projects; ordinary OpenCode service is untouched | `test_session_runner_acceptance.py::TwoProjectIsolationTests::test_cross_project_session_model_mapping_token_and_safe_switch`; `test_step04_service_acceptance.py::RealServiceAcceptanceTests::test_two_projects_locations_and_pinned_models_isolated`, `::test_ordinary_service_registration_untouched`; `test_session_runner.py::SubmitRefusalTest::test_second_project_cannot_inherit_execution_authority`; `test_runtime_worker_arbitration.py::CrossProjectTest::test_crossproject_runtime_blocks_new_work_and_switch` | Met |

## 6. Verification — commands and results

### 6.1 Coordinator targeted integrated runs (read-only logs; each at its own SHA)

| Log | Revision | Result |
| --- | --- | --- |
| `/tmp/opencode/step04-real-runner-integrated.log` | `8626cee` | **41 OK** = 37 deterministic (`test_session_runner.py`) + 4 real-service acceptance (`test_session_runner_acceptance.py`); only a pre-existing `ResourceWarning: subprocess … is still running` |
| `/tmp/opencode/step04-session-path-integrated.log` | `b791d6f` | **314 OK** |
| `/tmp/opencode/step04-consolidation-integrated.log` | `f6005df` | **159 OK** (backup suite) + **56 OK** (intent/commit suite) + **29 contract-doc checks OK** |
| `/tmp/opencode/step04-saved-grant-integrated.log` | `3106687` | **17 OK, no skips** (real-service acceptance module) |

### 6.2 Frozen historical checkpoint (older, **not** current)

- `/tmp/opencode/step04-frozen-7ef7a07-core.log` — **1,776 tests OK** at the older
  `7ef7a07`. This is **historical** and does **not** verify the current head.

### 6.3 Final full verification — `bash /tmp/opencode/step04-checks.sh`

Coordinator run at `c1f927b` (clean, unchanged head), log
`/tmp/opencode/step04-final-all-checks.log`; **ALL LOCAL CHECKS PASSED**:

| Check | Result |
| --- | --- |
| `PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_*.py'` | **1890 OK**, no skips |
| `python3 -m unittest discover -s scripts -p 'test_*.py'` | **20 OK** |
| `python3 scripts/check_contract_docs.py --self-test` | **29 checks passed** |
| `spikes/runtime/python` | **79 OK** |
| `spikes/runtime/live-jev` | **51 OK** |
| `spikes/runtime/integration` | **7 OK** |
| `npm --prefix spikes/runtime/typescript test` | **58 OK** |
| `(cd spikes/runtime/typescript && npx tsc --noEmit)` | passed |
| `python3 -m compileall -q src tests` + `git diff --check` | passed |

**Total 2,105 tests, no skips.** Pre-existing SQLite/subprocess
`ResourceWarning`s remain (disclosed, not failures). **CI:** `gh` API reports **0
workflows**; no CI-green claim. This run is at `c1f927b` and is **pre** this
docs-only completion update; the reproduce command is the script above, and
`scripts/check_contract_docs.py --self-test` on this checkout reports **29 checks
ok, 0 failed**.

## 7. REAL vs FIXTURE, and the explicit limits (do not overclaim)

**REAL (actually exercised):** the app-owned `opencode` **v2.2.0** service process
and its HTTP API; HTTP **Basic** auth (app-owned; no provider key needed for the
fixture); session create with an explicit pinned role/model; durable prompt
admission; SQLite persistence; per-project owned locations; process ownership /
`/proc` PID binding; the **owned** service restart; the public
permission-evaluation API; the runner's **default real client** construction; the
durable intent/mapping/dispatch/proposal rows; a **real** durable saved `always`
grant and the app human-approval boundary that refuses it; and a **fresh** critic
with a different concrete model and empty history. **Permission deny** here means
an **enforced denial** by the service's permission API plus a **proposal-only**
application interface — **not** tool absence (`skill` may remain advertised but is
denied), **not** an OS sandbox, and **not** native same-UID containment.

**FIXTURE (synthetic):** **only the model transport** —
[`tests/_runtime_model_fixture.py`](../../../tests/_runtime_model_fixture.py), a
loopback OpenAI-compatible child that returns a deterministic **stub** reply (and,
on request, one adversarial tool call). There is **no paid provider call, no
provider credential, and no network egress**; the real cost of every turn is
**USD 0**. The stub is **not** a scientific claim. The Jev acceptance path uses the
existing adapter with **injected/canned transports**
(`plan → intent → mark_submitted → dispatch_plan(injected transport) → commit_result`):
no real OpenRouter call, **no new daemon or direct CLI loop**.

**Limits (explicit):**

- **Not** a working review writer; **no** production paid generation; **no**
  hosted transfer or real-source upload; **no** scientific workflow; **no**
  scientific validation.
- An agent **cannot mint an authoritative gate**; the controlling-TTY CLI binds the
  **exact snapshot + capability**. The owner check is a controlling-terminal
  challenge, **not** proof of a biological human and **not** a native sandbox.
- Price cap / hosted rights / privacy / provider terms remain **unresolved** →
  production live generation and hosted transfer are **BLOCKED**; there were **no
  authorized paid tests** in this implementation.
- Model errors, refusals, malformed shapes, and non-answers are **never**
  scientific exclusions; records are versioned and direct cost is counted **once**.
- Session cost is the **sum of all assistant step costs**; a missing step cost
  yields **unknown** and the reserve is **retained**.
- Actual source/private content **can contain caller text**; this is **not** a
  secret scanner and makes **no** "never contains source" guarantee.
- The Step 03 worker remains a **synthetic** executor; it sees runtime-outstanding /
  switch-blocking arbitration but does **not** natively schedule model tasks.

## 8. Recovery limits (state them plainly)

- **Missed events are durable.** Reconciliation reads **durable server messages**,
  never the live event stream; a lost event does not lose a completion or an
  approval action.
- **Restart with an in-flight turn → unknown, retained, never success.** An
  ambiguous/unknown outcome keeps the dispatch slot and its reserve; it is never
  auto-retried or re-declared resolved.
- **No exactly-once external billing** is claimed; fencing protects the single
  **accepted local result**, not arbitrary external side effects.
- Reconciliation is an **explicit application call**, **not** an automatic
  workflow daemon.
- Production live generation / hosted transfer remain **blocked**.

## 9. Commits, changed files, and next actions

- **Step 04 scope base:** Step 03 implementation `2530955`, merged as
  `c9ee4e69a981b101e59e2c61330c5906105817d1`. **Integrated product code:**
  `8626cee` (`8626cee9e3c511a26a899abf21b6da2c2c0d943a`). **Docs-only handoff
  commit:** `c1f927b` (this handoff, integrated; same file tree as the branch
  commit `8440c9b`).
- **This documentation-only branch** (`task/step04-final-handoff`) changes only
  docs — **no source, test, config, contract, schema, baseline, step-criteria, or
  policy edit**:
  - `docs/implementation/handoffs/04-agent-runtime.md` (**new**, this file);
  - `docs/implementation/README.md` — ticks Step 04 as **complete (restricted
    runtime)** and links this handoff;
  - `README.md` — replaces the false *"no model, provider, network, agent
    execution"* claim with the accurate restricted-runtime state;
  - `docs/storage.md` — corrects the stale *"runner/CLI integration is not done"*
    paragraphs to the integrated bounded session-runner library state;
  - `docs/worker.md` — clarifies the runtime arbitration / API boundary;
  - `docs/cli.md` — a tiny accuracy fix to the stale Step 04 wording.
- **Provenance of the tick:** the coordinator's final run was at `c1f927b`; the
  completion edits above are a **later docs-only** change. The coordinator will
  prove the product tree is code-identical and re-check the final docs at the final
  head (no SHA invented here).
- **Next actions:** review and merge are the user's. The next implementation step is
  **Step 05 — reproducible discovery and corpus freeze**
  ([`05-discovery.md`](../05-discovery.md)); no other feature is needed here.
