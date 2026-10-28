# Handoff — step 03: background worker, durable jobs, and recovery

**Status: COMPLETE.** Step 03 is **ticked** in
[`docs/implementation/README.md`](../README.md). This step delivers a
stdlib-only, owner-authenticated detached worker with durable synthetic
job/attempt/result/budget state, a recovery/reconciliation path, bounded safe
retries, one execution-active project globally, and a crash/concurrency
acceptance matrix over **owned, disposable, real OS processes**. It implements
**no** model, provider, network call, scientific result, or scientific approval:
every task is a bounded **synthetic** numeric/waiting-human task and the external
provider is a **synthetic fixture** with no egress.

**Scope boundary (authority).** The step document
[`docs/implementation/03-durable-worker.md`](../03-durable-worker.md) is the
authoritative scope; its deliverables, acceptance criteria (AC1–AC7), and
handoff/exclusions were **not** edited. The accepted policy/technical direction is
[decision 002](../../decisions/002-step01-contracts-and-policies.md); the accepted
runtime language/integration is [decision 001](../../decisions/001-runtime-language.md).
No baseline invariant, acceptance criterion, or policy was weakened.

> **Evidence distinction.** The **coordinator's** full verification was performed on
> the exact reviewed revision `2530955a18a991abd686528ab38af7aed3fe2577` (see §6 and
> the recorded logs `/tmp/opencode/step03-pre-handoff-checks.log`,
> `/tmp/opencode/step03-wheel-cli-final.log`, and
> `/tmp/opencode/step03-wheel-runtime-smoke.log`; those paths are **provenance
> only, with no runtime dependency**). This documentation task re-ran the full
> check script on the handoff checkout at the **same** `2530955` and reports its
> own numbers (`/tmp/opencode/step03-handoff-worker-checks.log`); the coordinator's
> installed-wheel CLI evidence is used **at the same base** and is labelled as such,
> not re-run here.

## 1. Scope, base revisions, and authority used

- **Step document (authoritative):**
  [`docs/implementation/03-durable-worker.md`](../03-durable-worker.md) —
  dependencies, deliverables, acceptance criteria, and handoff/exclusions
  unedited.
- **Baseline:** [`docs/specification.md`](../../specification.md) (confirmed; not
  weakened).
- **Ratified decisions used:**
  - [`docs/decisions/001-runtime-language.md`](../../decisions/001-runtime-language.md)
    — accepted 2026-10-27: **Python core**, stdlib-only, with a supported,
    explicitly authenticated OpenCode V2 HTTP integration (retained for later
    steps; **not** exercised here).
  - [`docs/decisions/002-step01-contracts-and-policies.md`](../../decisions/002-step01-contracts-and-policies.md)
    — accepted by the user 2026-10-27 against reviewed revision `ee7f7ea`.
- **Approved contract versions (representation reviewable):**
  [`C01 identities v1.4`](../../contracts/01-identities.md),
  [`C02 gates-and-runtime v3.2`](../../contracts/02-gates-and-runtime.md) (§5.6
  concurrency cap, §6.3 cost/budget, §8.4 pause/cancel/detached CLI),
  [`C03 policy-decisions v1`](../../contracts/03-policy-decisions.md).
- **Step 03 scope base:** `5efa4f2` (`5efa4f2e80273a3b36d35155616b404c1963597c`),
  the integrated Step 02 end-state (merge PR #4).
- **Current reviewed / integrated revision:** `2530955`
  (`2530955a18a991abd686528ab38af7aed3fe2577`), the integrated tip of the T1–T8a
  Step-03 commits above the scope base.
- **This handoff branch:** `task/step03-handoff`, based at `2530955`. It changes
  **documentation only** (see §7).

## 2. Deliverables and public interfaces

### 2.1 Step deliverables → where they live

| Deliverable (step doc) | Implementation | Contract doc |
| --- | --- | --- |
| Local worker independent of CLI lifetime; start/status/attach/pause/resume/stop; authenticated local control | [`worker/process.py`](../../../src/review_article_agent/worker/process.py), [`worker/__main__.py`](../../../src/review_article_agent/worker/__main__.py), [`runtime/ipc.py`](../../../src/review_article_agent/runtime/ipc.py), [`cli/worker.py`](../../../src/review_article_agent/cli/worker.py) | [`cli.md`](../../cli.md) §9, [`worker.md`](../../worker.md) |
| Durable job/attempt states, authorization/budget checks, bounded retries, checkpoints, recovery reconciliation | [`runtime/jobs.py`](../../../src/review_article_agent/runtime/jobs.py), [`worker/engine.py`](../../../src/review_article_agent/worker/engine.py) | [`worker.md`](../../worker.md) §4/§6 |
| Exactly one active project globally; safe switching/draining; waiting gates release execution resources | [`storage/registry.py`](../../../src/review_article_agent/storage/registry.py), [`worker/engine.py`](../../../src/review_article_agent/worker/engine.py) | [`worker.md`](../../worker.md) §7 |
| Explicit paused / waiting-human / waiting-external / failed / interrupted / reconciliation-required outcomes | [`runtime/jobs.py`](../../../src/review_article_agent/runtime/jobs.py), [`worker/engine.py`](../../../src/review_article_agent/worker/engine.py) | [`worker.md`](../../worker.md) §4/§6 |
| Synthetic external fixture backend (no egress) | [`worker/synthetic.py`](../../../src/review_article_agent/worker/synthetic.py) | [`worker.md`](../../worker.md) §5 |

### 2.2 Public modules, CLI, and docs (links)

- Worker runtime safety contract: [`docs/worker.md`](../../worker.md) — the
  approved shape, control protocol v1, durable job/budget model, synthetic
  fixture, recovery, pause/switch semantics, cost caps, retention, known limits.
- CLI contract: [`docs/cli.md`](../../cli.md) §9 (`worker start|status|stop`) and
  §10 (`job run|status|list|pause|resume|cancel|switch|drain|reconcile|attach`).
- Storage contract: [`docs/storage.md`](../../storage.md) — private snapshot API;
  worker-integration test [`tests/test_worker_backup.py`](../../../tests/test_worker_backup.py).
- Worker public surface (`review_article_agent.worker`):
  `engine` (`APP_NAME`, `PROTOCOL_VERSION`, `OPERATIONS`, `FAULT_SEAMS`,
  `TEST_PAUSE_ENV`, `SYNTHETIC_NUMERIC_TASK`, `SYNTHETIC_WAITING_HUMAN_TASK`),
  `process` (`start_worker`, `worker_status`, `stop_worker`), `synthetic`.
- Runtime public surface (`review_article_agent.runtime`):
  `ipc.WorkerIPCClient`, `ipc.WorkerIPCClient.request`, `ipc.PeerNotAuthorized`,
  `ipc.PeerIdentityMismatch`, `ipc.WorkerNotRunning`, `ipc.IPCError`;
  `jobs.JobRepository`, `jobs.MAX_SAFE_RETRIES` (=2),
  `jobs.DEFAULT_PROJECT_CAP_USD` (=25), `jobs.DEFAULT_STAGE_CAP_USD` (=10),
  `jobs.BudgetExceededError`, `jobs.StaleFenceError`, `jobs.TERMINAL_JOB_STATES`.
- New CLI surfaces (same `main()`):
  `review-article-agent worker start|status|stop` and
  `review-article-agent job run|status|list|pause|resume|cancel|switch|drain|reconcile|attach`.

## 3. Storage interfaces, versions, and migrations

| Format | Version | Where | Note |
| --- | --- | --- | --- |
| Project store schema | **4** | `storage.schema.STORE_SCHEMA_VERSION` | adds durable job/attempt/result/reserve rows to the Step 02 store (3) |
| Registry schema | **2** | `storage.schema.REGISTRY_SCHEMA_VERSION` | adds the token-fenced execution activation |
| Synthetic provider journal | **1** | `worker.synthetic` (`<data_dir>/runtime/synthetic.sqlite3`) | **external fixture**, not the project store |
| Private backup format | **1** | `storage.backup` manifest `format_version` | **accepts store schemas 3 and 4** |

- **Migrations preserve bytes/fixture approval pins.** Schema 3→4 and registry
  1→2 upgrade in place; prior artifact revisions and `fixture` approval snapshots
  are preserved (all `fixture` approvals remain non-authorizing). A **newer**
  store/registry schema is refused cleanly by an **older** binary.
- **Backup before upgrade** is the documented safe path; the private snapshot
  carries the durable job rows (schema 4) and **never** the provider journal.
- **Data directory is owner-only on Linux**: the control transport validates
  `<data_dir>/runtime/` (`0700`) and the socket/lock (`0600`); there is **no TCP
  listener**, **no root**, and **no public/unauthenticated** surface.

## 4. Acceptance criteria — criterion-by-criterion evidence

Step document AC1–AC7. The decisive evidence is the real-process matrix
[`tests/test_step03_acceptance.py`](../../../tests/test_step03_acceptance.py)
(9 tests; T7), the product CLI tests
[`tests/test_cli_jobs.py`](../../../tests/test_cli_jobs.py) (29 tests; T5c), and
the transport/process suites. All commands run from the repository root with
`PYTHONPATH=src`.

| AC | Requirement (short) | Concrete tests | Status |
| --- | --- | --- | --- |
| 1 | Start a task, close the CLI, confirm completion/checkpoint from a new CLI | `test_step03_acceptance.py::DetachedSubmitterTest::test_job_completes_after_submitter_process_exits` (submitter PID gone **before** completion, then fresh client confirms); product CLI: `test_cli_jobs.py::RunAdmissionTest::test_run_returns_admission_not_completion`, `::test_separate_cli_later_sees_completion`, `::test_run_ensures_worker_and_status_reads_it`; installed wheel (`step03-wheel-cli-final.log`): `job run` returned `queued`/`admitted` with `job_completion=false` and the submitter CLI exited, then a fresh CLI `watch`/`status` saw `completed` | Met |
| 2 | Kill/restart the worker before dispatch, after external admission, and before local commit; recover one accepted local result without claiming exactly-once external execution | `test_step03_acceptance.py::SigkillRestartMatrixTest::test_matrix_all_seams_multiple_passes` — `SIGSTOP` the worker's own image at each of `before_dispatch`/`after_admission`/`before_commit` (`RAA_WORKER_TEST_PAUSE_AT`), capture persisted job/attempt/fence/reserve and backend admission count, `SIGKILL`, restart without the hook: **2 passes × 3 seams = 6 cases**, same attempt id and `fence 1`, backend `0/1/1` at the seams, `accepted==None` at the seam, one accepted row and one backend admission after recovery; no exactly-once **external** claim | Met |
| 3 | Never blindly resend ambiguous external work with a new identity; demonstrate reconciliation or human escalation | `test_step03_acceptance.py::AmbiguityPinsActivationTest::test_pause_cancel_ambiguity_pins_activation_and_no_busy_loop` (pause/cancel keep the unknown outcome unresolved, pin the global activation and reserve, admit nothing new); `::BackupRestoreInteropTest::test_restored_ambiguous_without_journal_blocks_other_project` (a restored ambiguous job with **no** external journal is an explicit unknown after restart, blocks switch/dispatch of another project, never a silent success or new identity); engine `reconcile` dispositions (`accepted_local_result`/`definitive_failure`/`human_escalation`/`keep_unresolved`); `test_cli_jobs.py::ErrorAndSecretTest::test_reconcile_confirm_required_before_any_request` | Met |
| 4 | Concurrent activation cannot execute two projects; a switch cannot leave old-project jobs committing into the new project | `test_step03_acceptance.py::ConcurrentActivationTest::test_two_concurrent_project_submits_only_one_admits` (two concurrent project submitters, exactly one admits); `::SwitchWhileInflightTest::test_switch_blocks_then_old_result_only_in_old_store` (switch blocked while in flight, old result accepted **only** in the old store, then the new project runs); `test_cli_jobs.py::SwitchBlockedTruthfulnessTest`, `::ConcurrentProjectTest` | Met |
| 5 | Stale attempts cannot overwrite newer authorized results; fencing protects accepted writes, not arbitrary external side effects | `test_step03_acceptance.py::StaleFenceTest::test_stale_attempt_cannot_overwrite_newer_result` (`StaleFenceError`; newer accepted result retained); the contract states fence/activation guard **local** accepted writes only, not external side effects that already happened | Met |
| 6 | Paused/budget-exhausted work does not schedule new calls; no busy loop while waiting for approval | `test_step03_acceptance.py::BudgetCapTest::test_unknown_reserve_retained_and_cap_blocks_new_admission` (unknown reserve retained, cap blocks new admission, no new attempt); `::AmbiguityPinsActivationTest::...::no_busy_loop` (bounded `/proc` CPU ticks over a wall interval while the gate waits); `test_cli_jobs.py::WatchBoundTest` (no query after deadline) | Met |
| 7 | Authentication rejects non-owner control; no root or public unauthenticated listener required | Owner-only UDS control: `test_worker_ipc.py` (47 tests: real Unix-domain-socket control plus injected foreign-UID denial; **no** privileged live cross-UID test) and `test_worker_process.py` (18 tests: kernel PID binding, foreign socket, bounded deadline, reap); `test_step03_acceptance.py` cites them rather than duplicating | Met |

### 4.1 AC7 cross-UID limitation (explicit)

The owner check is a **kernel `SO_PEERCRED` uid/PID** check on a local Unix-domain
socket. It rejects a **non-owner** control connection (proven with an injected
foreign UID). A **privileged live cross-UID** test (a second real OS user) is
**not** run and is **not** claimed; it would need a privileged second user in the
test environment. This is a local-transport identity, **not** proof of human
presence and **not** a scientific approval.

## 5. Restart/failure matrix, identities, retention, retry policy

- **Dispatch seams** (`engine.FAULT_SEAMS`): `before_dispatch`, `after_admission`,
  `before_commit`. At the seam the persisted state is captured, the worker is
  `SIGKILL`ed, and a fresh worker recovers **one** accepted local result under the
  **same** `attempt_id` and `fence` (no new identity, no extra reservation).
- **Identities:** `job_id` derives from `(project_id, request_id)` (`UNIQUE` per
  request key; repeat submit = same row, divergent payload = conflict); the
  `attempt_id` is opaque and `fence` monotonically increments per attempt.
- **Bounded safe retries:** `MAX_SAFE_RETRIES = 2`, allowed **only** for a safe
  **predispatch** prior attempt (never externally admitted, no unresolved outcome)
  with explicit `retry_authorized_by`. An **ambiguous/unresolved** prior is never
  auto-retried; the **same** attempt reconciles or escalates (same-identity
  recovery).
- **Retention:** **no record pruning** is implemented — job/attempt/result/reserve
  rows and the synthetic provider journal are retained. Project retention and
  backups stay user-controlled; only owned, throwaway test fixtures are deleted.
- **Recovery:** `reconcile_startup` + `recover()`. A local predispatch attempt
  becomes a safe retryable `interrupted`; a synthetic external attempt is treated
  as potentially ambiguous (reserve retained, job persisted as
  `reconciliation_required`). An unreadable store is a recovery blocker forbidding
  new dispatch. A **lost/replaced journal**, another project owning the identity,
  or an unreadable store is explicit `unknown`, **not** absence.

## 6. Verification — commands and results

### 6.1 Authoritative full check at exact `2530955` (re-run on this handoff checkout)

From the repository root, `bash /tmp/opencode/step03-final-checks.sh`
(log: `/tmp/opencode/step03-handoff-worker-checks.log`):

| Check | Result |
| --- | --- |
| `PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_*.py'` | **659 pass** |
| `python3 -m unittest discover -s scripts -p 'test_*.py'` | **20 pass** |
| `spikes/runtime/python` | **79 pass** |
| `spikes/runtime/live-jev` | **51 pass** (pre-existing `ResourceWarning: unclosed ...report.json` at `test_live_jev.py:507`/`:531` only; not a new failure) |
| `spikes/runtime/integration` | **7 pass** |
| `python3 scripts/check_contract_docs.py --self-test` | **29 checks pass** |
| `npm --prefix spikes/runtime/typescript test` | **58 pass** |
| `(cd spikes/runtime/typescript && npx tsc --noEmit)` | clean |
| `python3 -m compileall -q src tests` + `git diff --check` | clean |
| `ALL LOCAL CHECKS PASSED` | exit `0` |

**Total: 874 tests** (659 + 20 + 79 + 51 + 7 + 58), plus **29 additional
documentation checks**. (Reaching the TypeScript result required a one-time
`npm install` in `spikes/runtime/typescript`, whose dependencies were not
pre-installed in this fresh worktree; `node_modules/` is git-ignored and no
tracked file changed.) Environment: Python **3.14.4**, SQLite **3.46.1**, Node
**v24.22.1**, npm **12.2.1**. Python `>=3.11` is declared, but **Python 3.11 is
not installed/tested** here.

### 6.2 Coordinator verification reused **at the same base** (not re-run here)

- `/tmp/opencode/step03-pre-handoff-checks.log` — the same `659 + 20 + 79 + 51 +
  7 + 29 doc checks + 58 TS` results and `ALL LOCAL CHECKS PASSED` at
  `2530955`.
- `/tmp/opencode/step03-wheel-cli-final.log` — an **installed wheel** built and
  the **real CLI** run from an unrelated CWD using only the extracted wheel:
  `job run` returned `queued`/`admitted` with `job_completion=false` and the
  submitter CLI exited; a **fresh** CLI `watch`/`status` reported `completed`; an
  **exact-key replay** produced one job/result; `backup create`/`verify` passed;
  an **owned** worker `stop` was confirmed. (Fault tests use disposable app-owned
  instances; the user's ordinary OpenCode service/data is never restarted or
  deleted.)
- `/tmp/opencode/step03-wheel-runtime-smoke.log` — an earlier installed-wheel
  RPC smoke (real detached CLI worker start, numeric job completion, private
  backup+verify).

### 6.3 Test-level totals used in this handoff

- `tests/test_step03_acceptance.py`: **9 tests** (T7): `SigkillRestartMatrixTest`
  = 1 test / **6 cases** (2 passes × 3 seams); `DetachedSubmitterTest` = 1;
  `ConcurrentActivationTest` = 1; `SwitchWhileInflightTest` = 1;
  `AmbiguityPinsActivationTest` = 1; `StaleFenceTest` = 1; `BudgetCapTest` = 1;
  `BackupRestoreInteropTest` = 2.
- `tests/test_cli_jobs.py`: **29 tests** (the real **product CLI** job surface;
  T5c), independently of the acceptance module.
- `tests/test_worker_ipc.py`: **47 tests** (real UDS + injected foreign UID).
- `tests/test_worker_process.py`: **18 tests** (kernel PID binding, foreign
  socket, deadline, reap).
- The whole `test_worker_engine.py` module (T7) was repeated **3×** plus the
  coordinator's run, and is included again in the final full suite.

## 7. Explicit claims, limits, and exclusions

### 7.1 What is claimed

- A stdlib-only, owner-authenticated **local** detached worker with durable
  synthetic job/attempt/result/budget state, explicit recovery/reconciliation,
  and a crash/concurrency matrix proven on real owned processes.

### 7.2 Honest limitations (not overclaimed)

- **No real integration:** no model, provider, network call, scientific result,
  scientific approval, source processing, model role, adapter, agent execution,
  or paper-writing feature exists in this slice. The provider journal is an
  **external synthetic fixture**, excluded from project backups. `application`
  accepted writes are **fenced locally**; that fencing does **not** fence
  arbitrary external side effects. A lost continuity is explicit **unknown**, not
  absence.
- **Cost:** reserves are **virtual synthetic** fault-test values; actual external
  spend is **USD 0**. Defaults (`POL-OPS-COST`): project **USD 25**, stage **USD
  10**, at most **2** concurrent external calls (one serial synthetic executor in
  this slice). The original reserve is **conservatively never released**, even
  when the known actual is `0`. There is **no** budget reset and **no** cancel of
  blind quiescence.
- **No exactly-once:** no exactly-once external execution or billing is claimed.
- **No auto-start:** explicit start only; **no** autostart after
  logout/reboot, **no** systemd/services/lingering, **no** distributed engine.
- **No pruning:** job/attempt/result/reserve rows and the provider journal are
  retained; the engine's owner-reconciliation **audit**, `wait_reason` text, and
  paused-project set are **in-memory only**; actual job states/checkpoints/
  results/reserves are **durable**.
- **Queued work:** a queued job with no attempt needs an explicit owner
  resume/submit to acquire execution **only if no activation is held**; if a
  persisted authorized activation exists, the engine may continue queued work.
  There is no blanket auto-resume claim.
- **Scientific approvals are not operational:** `job reconcile` records an owner
  disposition only; no real scientific `Approval` and no arbitrary result JSON is
  accepted. UID/PID auth is **not** proof of human presence and does not
  distinguish same-UID human vs model; those gates belong to Step 04.
- **Retention/retry policy:** `MAX_SAFE_RETRIES = 2`, safe **predispatch only**,
  with explicit authorization and same-identity recovery.

### 7.3 Excluded / next (Step 04)

- **Not implemented:** restricted agent roles, model adapters, provider
  integration, agent execution, and scientific approvals (Step 04,
  [`04-agent-runtime.md`](../04-agent-runtime.md)); and all scientific features.
  This handoff does **not** implement or claim them.

## 8. Commits, changed files, and next actions

- **Step 03 scope base:** `5efa4f2`. **Reviewed/integrated revision:** `2530955`
  (`2530955a18a991abd686528ab38af7aed3fe2577`, T1–T8a).
- **This documentation-only handoff branch** (`task/step03-handoff`, based at
  `2530955`) changes exactly these paths — **no source, test, config, contract,
  baseline, step-criteria, or policy edit**:
  - `docs/implementation/handoffs/03-durable-worker.md` (**new**, this file);
  - `docs/implementation/README.md` — **ticks** the Step 03 line with a short
    annotation and this handoff link, and corrects the stale "Step 03 is the next
    implementation step" sentence in the Step 02 entry (historical/current, without
    rewriting the Step 02 evidence);
  - `README.md` — records the now-local foundation **plus** the synthetic detached
    worker, adds a short copyable `job` lifecycle example, updates the CLI/worker/
    storage/handoff pointers, and states Step 04 is next and **not** implemented;
  - `docs/worker.md` — **finalizes** the runtime safety contract (title/top/§3/
    §10/§11): removes the stale *partial/pending* framing for the now-integrated
    new-job CLI and full acceptance, links this handoff and the actual `job` CLI,
    and **preserves** its accuracy clarifications and every limitation.
- **CI:** **no CI workflows are configured** (observed via `gh`: `gh run list` and
  `gh workflow list` return empty; no `.github/` directory). There is **no CI
  green** claim — verification is **local only**.
- **Next actions:** the user owns **PR review and merge**; the PR is **not** opened
  until after this documentation step, and the coordinator will inspect the actual
  PR head checks after any push. The next implementation step is **Step 04 —
  restricted agent roles, model adapters, and approvals**
  ([`04-agent-runtime.md`](../04-agent-runtime.md)), which is **not** implemented
  here.
