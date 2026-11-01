# Worker runtime safety contract (Step 03, with bounded Step 05 discovery and Step 06 retrieval additions)

> Scope: a concise **runtime safety contract reference** for the integrated Step 03 foundation and
> engine — the detached-worker launcher, the owner-authenticated local control transport, the durable
> synthetic job/attempt store, the engine, and the synthetic external fixture backend — plus the
> integrated worker-lifecycle and synthetic job-control CLI. It changes no policy/spec/schema/CLI
> decision. Step 03 is **complete**: the new-job CLI, the full crash/`SIGKILL` matrix, and the
> step-acceptance accounting are now integrated and evidenced in the
> [Step 03 handoff](implementation/handoffs/03-durable-worker.md); the `job` control surface is
> documented in [`cli.md`](cli.md) §10. This document remains a **reference**, not the completion
> handoff.
>
> **Step 03 boundary (preserved).** The engine described in §1–§11 is the historical Step 03
> synthetic foundation: its `submit` op still accepts **only** `synthetic_numeric`/
> `synthetic_waiting_human` fixtures, its reserves remain **virtual** synthetic fault-test values,
> and no real model, provider, network call, scientific result, or approval is implied by that
> synthetic surface.
>
> **Step 05 addition (bounded).** On top of that unchanged foundation, the worker now also admits
> **owner-controlled discovery background jobs** through a *separate* `discovery_submit` op and a
> single serial discovery executor (§12). Those jobs make **real** public-metadata provider GET
> requests (`query`/`seed`/`expand`), a bounded citation-expansion traversal; `metadata_import` is a
> **local, zero-HTTP** observation of a caller-supplied record file; and (`lead`) **one** guarded
> direct-lead `POST` per durable job. §12 states their exact authority,
> fences, and limits; nothing in §1–§11 is weakened, and the direct-lead route leaves the existing
> app-owned OpenCode **production block unchanged**. Step 05 is **complete as a scoped discovery
> workflow** (final full combined check passed at head `f3d97fe`; see
> [`implementation/handoffs/05-discovery.md`](implementation/handoffs/05-discovery.md)); no
> scientific validation, completeness, review/writer, or later-step feature is implemented or
> claimed.
>
> **Step 06 addition (bounded).** The worker also schedules owner-controlled official
> full-text retrieval jobs (`discovery_submit` `mode=acquire`, one `acquisition_fulltext`
> job); §13 states their authority, fences, and limits.
> Authority: [`implementation/03-durable-worker.md`](implementation/03-durable-worker.md),
> [`decisions/001-runtime-language.md`](decisions/001-runtime-language.md),
> [`decisions/002-step01-contracts-and-policies.md`](decisions/002-step01-contracts-and-policies.md),
> [`contracts/02-gates-and-runtime.md`](contracts/02-gates-and-runtime.md) §5.6/§6.3/§8; storage
> snapshot: [`storage.md`](storage.md).

## 1. Approved shape

- **Python stdlib only, Linux**: no third-party dep, no service manager, no root.
- **Owner-only Unix-domain socket (UDS)** control: no TCP listener, no public/unauthenticated surface.
- **Owner identity, out of band**: the transport's owner identity comes from kernel `SO_PEERCRED`
  (uid, and PID where relevant), never a request field. This is a *local transport* identity, not
  proof of human presence or of scientific approval, and it does not distinguish same-UID processes as
  human vs model. No real agent runs in this slice: role-restricted model tools and scientific
  owner-approval gates belong to Step 04, and the engine's owner-control/audit record is not an
  `Approval`.
- **Runtime arbitration is shared, not duplicated**: the Step 04 bounded session runner
  ([`runtime/session_runner.py`](../src/review_article_agent/runtime/session_runner.py):
  `submit_session_turn`/`reconcile_session_turn`) is a **library** path, not a second worker, scheduler,
  or CLI. It shares this engine's single serial remote slot and its activation/switch-blocking
  arbitration: an outstanding runtime `opencode_session` dispatch (`submitted`/`admitted`/`unknown`)
  blocks a new synthetic dispatch and a safe switch, and an outstanding synthetic attempt blocks a
  competing runtime send. The Step 03 worker still executes its **synthetic** fixtures only through
  the `submit` op; since Step 05 it *also* schedules owner-controlled discovery background jobs on
  one serial discovery executor (§12), and the runtime ledger's rows are **not** synthetic jobs.
  Neither the discovery executor nor the runtime path is a second worker, scheduler, or CLI.
- **Private runtime paths**: the control transport validates existing paths before mutating them —
  `<data_dir>/runtime/` must be owner-private (`0700`) and the socket/lock `0600`; a symlink, a
  non-owner file/dir, or a foreign live endpoint at the control path is refused and left untouched,
  never chmodded or deleted. This is the transport's behavior for its *runtime* paths, not a claim
  that every application path is never chmodded (the registry may tighten the owner's data directory
  to `0700` before transport startup).
- **Explicit start only**: launched explicitly by the owner; no system service, no systemd lingering,
  no auto-start on logout/reboot.
- **A separate app worker**, not OpenCode's HTTP service and not an ordinary coding session.

## 2. Process, entry points, control transport

- Launcher [`worker/process.py`](../src/review_article_agent/worker/process.py): `start_worker` spawns
  `sys.executable -m review_article_agent.worker --data-dir <abs>` with `start_new_session=True`,
  `DEVNULL` stdio, no shell, no PID file; `PYTHONPATH` derived from its own location. Entry
  [`worker/__main__.py`](../src/review_article_agent/worker/__main__.py) is **safety-ordered**: it owns
  the IPC singleton lock before any recovery/scheduling mutation.
- **Readiness, not completion**: a launch is credited only from a verified control response (app
  marker `review_article_agent.worker`, integer `protocol_version` 1, integer `pid` equal to the
  kernel-reported peer PID); `worker start` acks the worker is up and proves no job completed.
- **Stop by authenticated request, never a signal**: `stop_worker` sends `{"version": 1, "op":
  "stop"}` bound to the verified peer PID; success requires the socket **confirmed gone**. An
  unconfirmed, unavailable, or foreign endpoint is a stable non-success; no signal, no PID file.
- Transport [`runtime/ipc.py`](../src/review_article_agent/runtime/ipc.py), one socket per `data_dir`:
  a **singleton** `flock` (`runtime/worker.lock`) held for the server's lifetime (stale-socket cleanup
  only under that lock, only for a socket we own, only when a probe proves it connection-refused);
  **authentication** by kernel uid and, for control, kernel peer PID, checked **before sending a
  byte** (`PeerIdentityMismatch`); a **bounded, strict protocol** (one newline-delimited strict-JSON
  request per connection, hard size caps, a hard I/O timeout, rejection of duplicate keys, non-finite
  numbers and over-deep nesting, curated non-reflective errors, no busy waiting).

## 3. Control protocol (version 1)

[`worker/engine.py`](../src/review_article_agent/worker/engine.py). Requests are `{"version": 1,
"op": "...", ...}`; only version `1` is accepted. Ops implemented **in this checkout**: `status,
submit, discovery_submit, job_status, job_list, pause, resume, cancel, reconcile, switch, drain,
stop`. Strict per-op key allowlist; any extra key (including a caller-supplied `uid`/`actor`) is
rejected.

- **`submit` is fixed synthetic work only**: kinds `synthetic_numeric` and `synthetic_waiting_human`;
  no model, provider, credential, document, path, or scientific task, and no default scientific roles
  or scientific identity. A submit is not a real model call or a scientific/approval decision.
- **`discovery_submit` is the separate Step 05 op** (§12): it admits one *owner-controlled discovery
  background job* (`query`/`seed`/`metadata_import`/`expand`/`lead`) as a durable queued job. It does
  **not** widen `submit`; both ops share the strict allowlist, the verified owner transport, and the
  same registry execution fence.
- **Fixed result shape**: `accepted_local_result` must be exactly `{"kind": "synthetic_numeric",
  "value": <int 0..999999>}`; any other kind, field, string payload, boolean, or out-of-range value is
  rejected before any write. No arbitrary command, payload, or data is ever executed.
- Lifecycle CLI spellings are exactly `worker start|status|stop` ([`cli.md`](cli.md) §9); the
  synthetic job-control CLI (`job run|status|list|pause|resume|cancel|switch|drain|reconcile|attach`,
  [`cli.md`](cli.md) §10) is integrated over this same IPC version 1 surface. This document describes
  IPC version 1 and its operations generically.

## 4. Durable jobs, attempts, results, budget

[`runtime/jobs.py`](../src/review_article_agent/runtime/jobs.py), over the project's SQLite store
(project store **schema 4**):

- **Opaque, exact IDs**: `job_id` derives from `(project_id, request_id)`, `UNIQUE` per request key
  (repeat submit = same row; divergent payload = conflict); `attempt_id` is an opaque UUID and `fence`
  monotonically increments per attempt; idempotent submit/accept never create a second identity.
- **One immutable accepted local result**: `job_accepted_result` unique per `(job, fence)` and per
  logical job; a stale-fence attempt cannot write one (`current_fence` re-checked on every mutation);
  accept is idempotent per fence+digest else conflict.
- **States** — job: `queued, admitted, running, waiting_human, waiting_external, paused, cancelled,
  completed, failed, interrupted, reconciliation_required`; attempt: `queued, admitted, running,
  succeeded, definitive_failure, ambiguous, interrupted`.
- **Bounded safe retries**: `MAX_SAFE_RETRIES = 2`, only a safe predispatch prior attempt (never
  externally admitted, no unresolved outcome) with explicit `retry_authorized_by`; an
  ambiguous/unresolved prior is never auto-retried — the same attempt reconciles or escalates.
- **Reserve before dispatch**: a synthetic external dispatch records an exact-decimal reserve before
  dispatch; caps refuse over-reservation; `unknown` retains its reserve; a `known`/`estimated` actual
  overrun counts at the larger amount. Durable in the store: job states, attempts, checkpoints, the
  single accepted result, and budget reserves — state survives a restart.

## 5. Synthetic external fixture backend

[`worker/synthetic.py`](../src/review_article_agent/worker/synthetic.py) models a durable external
provider with no real egress:

- Journal `<data_dir>/runtime/synthetic.sqlite3` (own **schema version 1**): a **separate external
  fixture**, not the project store and **not** part of a per-project backup.
- Records durable **admission separate from completion** and a deterministic synthetic result;
  `complete`/`failure` terminal, `ambiguous` intentionally cannot prove a terminal/quiescent outcome.
- **Continuity persisted before dispatch**: the engine calls `initialize()` before admitting, then
  records a continuity checkpoint before the first dispatch, so a crash after admission but before a
  local receipt is recoverable.
- **A missing local receipt alone is not absence**: absence is definitive only when nothing could have
  been admitted (no journal, or a proven-absent identity on the same stable generation). A
  lost/replaced journal, another project owning the identity, or an unreadable store is explicit
  `unknown` needing reconciliation; same-identity replay is allowed only for a definitively absent
  identity on a stable generation.
- **No exactly-once external claim**: the fixture fenced its own local journal; it makes no claim
  about arbitrary external side effects or external billing already incurred.

## 6. Recovery and reconciliation

- Startup reconciles recorded attempts across all registered projects (`reconcile_startup`). A
  **local** predispatch attempt becomes a safe retryable `interrupted`; a **synthetic external**
  attempt is treated as potentially ambiguous — reserve and unresolved bit retained, with the job
  state persisted as `reconciliation_required`. `recover()` also returns a `RecoveryOutcome.disposition`
  hint (`retry_safe`, `reconciliation_required`, `accept_pending_local_result`, or `terminal`); the
  `accept_pending_local_result` value is that *return hint* (a local result was checkpointed), not a
  persisted job state. No new identity is ever minted; paused/cancelled scheduling state is never
  silently undone.
- An unreadable store is never treated as safe: it is a recovery blocker forbidding new dispatch. If
  exactly one project holds unresolved remote work and no activation is held, the engine adopts it to
  reconcile it; multiple pending projects or any unreadable store block new dispatch/switch pending an
  explicit owner decision.
- Resolution is **explicit owner control** (`reconcile`: `accepted_local_result`,
  `definitive_failure`, `human_escalation`, `keep_unresolved`); quiescence is an explicit owner
  disposition, not an automatic retry of an ambiguous job.

## 7. Pause, cancel, activation, switching

- **At most one execution-active project.** Activation lives in the registry
  ([`storage/registry.py`](../src/review_article_agent/storage/registry.py), registry **schema 2**),
  token-fenced; every accepted local write is authorized under `execution_write_guard` — new dispatch
  active-only, reconciliation of already-admitted work may use `allow_draining=True` (never for new
  work).
- **Current fence + activation guard protect accepted local writes, not arbitrary external side
  effects**: a stale attempt cannot overwrite a newer authorized result, which does not fence an
  external effect that already happened.
- **Pause/cancel keep an unknown remote outcome unresolved** and retain the project's active
  arbitration; `cancel` is terminal for scheduling only. **Human/quiescent gates release local
  execution**, but a project with possible remote work keeps arbitration until reconciled or quiesced.
- **Safe switch drains the old project first**: `switch`/`drain` persists a durable pause on the old
  project's non-terminal jobs, drains and reconciles/quiesces its remote work, then releases and
  activates the new project. Old jobs cannot commit into the new project; budgets/approvals are never
  shared.

## 8. Serial executor, scheduling, cost caps

- **One serial synthetic executor**: at most one synthetic dispatch at a time for the active project
  — within the approved **at most 2 simultaneous external model calls** cap (C02 §5.6). A job
  occupying the serial slot (unresolved, `waiting_external`, admitted/running, or a queued attempt
  carrying continuity) blocks all other dispatch, including new jobs. The same single slot is shared
  with the Step 04 runtime dispatch ledger (§1): an outstanding runtime `opencode_session` dispatch
  occupies it and blocks a new synthetic dispatch.
- **No dispatch while held, capped, or recovery-blocked**: not while paused/draining/cancelled, not
  when a cap would be exceeded, not while an unreadable/other-store blocker exists.
- **No busy loop**: a `waiting_human`/unknown outcome is never polled; the loop blocks until an owner
  request or a known backend deadline.
- **Caps (defaults, `POL-OPS-COST`)**: project **USD 25**, stage **USD 10**, at most **2** concurrent
  external calls. Every route counts; a reserve is required before dispatch; reaching a cap stops new
  dispatch only (no retraction of executed work, no auto-pass, not reset by restart); only an explicit
  human decision may raise a cap. For the **synthetic** `submit` jobs here, reserves are **virtual**
  fault-test values and actual external spend is **0**; a Step 05 discovery `lead` job reserves and
  settles on the real runtime dispatch ledger instead (§12), and a discovery `query`/`seed`/`expand`
  job reserves nothing (read-only public-metadata GETs are not a paid route), while a
  `metadata_import` job reserves nothing and issues **zero HTTP** (it records a caller-supplied local
  observation).
- **Exact-decimal, context-independent exposure**: amounts are exact decimals, never floats; exposure
  is `max(reserve bound, reported actual)` once the actual is known; the original reserve bound is
  **conservatively never released**, even when the known actual is `0`. A job can be stopped before
  money is actually spent, so a reserve is an upper bound — an explicit limitation, not a promise that
  a paid execution is ready.

## 9. Retention and data boundaries

- **No record pruning implemented**: job/attempt/result/reserve rows and the provider journal are
  retained; there is no automatic prune. Real project data is not "disposable", and this document does
  not instruct deleting it — project retention and backups stay user-controlled (only owned, throwaway
  test fixtures are deleted, by tests). A private project snapshot captures the durable job rows
  (schema 4) and never the provider journal; backup **format 1 accepts store schemas 3 and 4**. It is a
  private snapshot, not a manuscript/redistribution export.
- **Secrets excluded by allowlist** (a snapshot contains only `manifest.json` and `snapshot.sqlite3`),
  not by scanning; do not store credentials as artifact content.
- **In memory, not durable**: the engine's owner-reconciliation audit, its `wait_reason` text, and its
  paused-project set are in-memory only and are not durable human approvals. Durable job
  states/results/reserves persist.
- **Logout/reboot retains state only**, with no continuity guarantee for in-flight remote work. After
  a restart, a queued job with no attempt needs an explicit owner resume/submit to acquire execution
  *only when no activation is held*; if a persisted authorized execution activation exists, the engine
  adopts it and may continue queued work. The system makes no blanket auto-resume claim.
- **Scientific fixture approvals never authorize the worker**: a local fixture approval
  ([`cli.md`](cli.md) §8) never authorizes a job, a provider, or science.

## 10. Known limits

- **Step 03 limits (preserved).** No real model/provider, network call, scientific result, or
  scientific approval exists in the synthetic `submit` slice; every `submit` task is a bounded
  synthetic numeric or waiting-human fixture, and no exactly-once external execution or billing is
  claimed. The full crash/`SIGKILL` matrix and the step-acceptance accounting are integrated and
  evidenced in the [Step 03 handoff](implementation/handoffs/03-durable-worker.md); a snapshot still
  proves storage integrity, not remote quiescence.
- **Step 05 limits (added).** The `discovery_submit` jobs do make real bounded public-metadata
  provider GET requests (`query`/`seed`/`expand`), a bounded citation-expansion traversal, and (for
  `lead`) **one** guarded direct-lead `POST` per durable job — under the exact owner, gate, rights,
  credential, and cost controls of §12; a `metadata_import` job issues **zero HTTP** and records only
  a caller-supplied local observation. They still make **no** scientific-validation, completeness,
  deduplication, coverage, freeze, or review/writer claim, and no exactly-once external-request or
  billing claim; Step 05 is complete as a scoped discovery workflow (verified at head `f3d97fe`).

## 11. Ownership map, safety tests, and verification

- **Ownership (minimal)**: [`runtime/jobs.py`](../src/review_article_agent/runtime/jobs.py)
  jobs/attempts/budget · [`runtime/ipc.py`](../src/review_article_agent/runtime/ipc.py) control
  transport · [`storage/registry.py`](../src/review_article_agent/storage/registry.py) activation ·
  [`storage/schema.py`](../src/review_article_agent/storage/schema.py) project schema/migrations ·
  [`worker/engine.py`](../src/review_article_agent/worker/engine.py) engine/protocol/scheduling ·
  [`worker/process.py`](../src/review_article_agent/worker/process.py) detached launcher ·
  [`worker/synthetic.py`](../src/review_article_agent/worker/synthetic.py) external fixture ·
  [`worker/__main__.py`](../src/review_article_agent/worker/__main__.py) worker entry point.
- **Safety tests**:
  [`tests/test_jobs.py`](../tests/test_jobs.py),
  [`tests/test_worker_ipc.py`](../tests/test_worker_ipc.py),
  [`tests/test_worker_engine.py`](../tests/test_worker_engine.py),
  [`tests/test_worker_process.py`](../tests/test_worker_process.py),
  [`tests/test_activation.py`](../tests/test_activation.py),
  [`tests/test_synthetic_backend.py`](../tests/test_synthetic_backend.py),
  [`tests/test_worker_backup.py`](../tests/test_worker_backup.py),
  [`tests/test_cli_worker.py`](../tests/test_cli_worker.py) — durable jobs/budget, transport/auth,
  engine recovery, process lifecycle, activation, fixture backend, worker+backup, lifecycle CLI.
- **Verification**: `python3 scripts/check_contract_docs.py --self-test` stays green (this document
  adds no fixture/schema semantics); `git diff --check` is clean; local markdown link targets in this
  document resolve to existing files. The full step acceptance (with latest test counts) is recorded
  in the [Step 03 handoff](implementation/handoffs/03-durable-worker.md).

## 12. Owner-controlled discovery background jobs (Step 05 addition)

This section extends §1–§11 without weakening them. It documents the **only**
worker surface that issues real provider/lead I/O: the `discovery_submit` op and
the single serial discovery executor. The Step 05 CLI spellings are in
[`cli.md`](cli.md) §12; the discovery value layers and their exact limits are in
[`discovery.md`](discovery.md). Step 05 is **complete as a scoped discovery
workflow** (final full combined check passed at head `f3d97fe`)
([handoff](implementation/handoffs/05-discovery.md)); nothing here is a scientific
workflow, a completion, or an approval.

### 12.1 The `discovery_submit` op and its five modes

- **Separate op, unchanged `submit`.** `discovery_submit` (`{"version": 1, "op":
  "discovery_submit", "project_id", "request_id", "mode", "parameters"}`) is
  distinct from the synthetic `submit` (§3). The parameters must match the mode's
  exact key allowlist — `query` (`plan_ref`, `query_index`), `seed` (`source`,
  `query`, `page_size`, `max_pages`), `metadata_import` (`plan_ref`, `source`,
  `records`, `origin_locator`, `observed_at`), `expand` (`plan_ref`, `corpus_ref`,
  `roots`), `lead` (`batch_ref`, `credential_env`) — and no actor/authority field
  is ever accepted.
- **Exact parameters, logical key.** The worker strictly re-validates and
  canonicalises the parameters, then persists one immutable `JobRepository` job
  under the caller's `request_id` as the **logical** idempotency key: an identical
  replay is the same row, a divergent replay is a `conflict`. The five modes map to
  task kinds `discovery_query`/`discovery_seed`/`discovery_import`/
  `discovery_expand`/`discovery_lead`.
- **Active-project global fence.** Submission acquires/verifies the same single
  global registry execution activation that new synthetic dispatch uses (§7); a
  different execution-active project, a paused/draining project, or a recovery
  blocker refuses the queuing (no row created).
- **Ownership.** [`worker/discovery.py`](../src/review_article_agent/worker/discovery.py)
  (strict submission validation, mode/task-kind mapping, the guarded transport, and
  the safe result projections) · [`worker/engine.py`](../src/review_article_agent/worker/engine.py)
  (`_op_discovery_submit`, `_maybe_start_discovery`, `_tick_discovery`,
  `_run_discovery_job`, `_run_lead_dispatch`).

### 12.2 Serial executor, thread, and store discipline

- **One scientific future per process.** At most **one** discovery dispatch runs at
  a time on a single-worker executor (`_discovery_future`/`_discovery_job`); it
  shares the engine's single serial slot arbitration (§1, §8) — an occupied slot
  (a queued attempt carrying continuity, a `waiting_external` job, or a running
  attempt) blocks a new discovery start.
- **Own store per thread; no lock over the network.** The provider work runs on the
  executor's own thread with its **own** `ProjectStore` connection (re-resolved
  from the registry, never carried across the thread lifetime). No store
  connection or lock is held across network I/O.
- **Admission, not completion.** `discovery_submit` returns a **queued admission**
  only; the provider work is scheduled asynchronously by the engine tick and is
  observed later with `job status`/`job list`/`job attach` (§3). A returning
  submission never means the search/expansion/lead completed.
- **Root `job` lifecycle controls apply.** `pause`/`resume`/`cancel`/`switch`/
  `drain`/`reconcile` (§3, §7) are the only lifecycle controls and apply to every
  discovery admission unchanged. They control scheduling/admission; they are **not**
  a parallel approval and grant no scientific authority. `cancel` stops scheduling
  but never terminal-clears an ambiguous outcome; `resume` neither clears an
  unknown outcome nor raises a cap nor bypasses a waiting gate.

### 12.3 Per-attempt revalidation, honest interruption, honest restart

- **Per-attempt gate/token/job-held checks.** Before **every** real provider
  attempt the job rechecks the exact approved current plan (via the S1 execution
  library), the execution activation **token** (the S1 executor revalidates
  `active` before each attempt), the current corpus and observed roots (for
  `expand`), and its own held state (paused/cancelled/draining). A stop before any
  attempt aborts cleanly; a stop *after* a real attempt raises the opt-in interrupt
  that the shared transport turns into a **failed page with no fabricated
  attempt** — pages already observed are retained, and only **real** low-level
  attempts are counted, so no fabricated socket or attempt is ever recorded.
- **A predispatch checkpoint before dispatch.** A durable `discovery_predispatch`
  checkpoint is recorded on an attempt *before* the first provider call. Its
  presence after a restart is the honest "may have dispatched" signal.
- **Honest restart, never a blind retry.** A possibly-dispatched discovery attempt
  is **never** blindly retried. The engine first tries a **replay-only** proof that
  the exact local outcome already exists on the **same fence** (no provider I/O:
  query/seed/import via the S1 replay short-circuit, `expand` from persisted
  relations only, `lead` from the durable runtime ledger without a credential). A
  proven local observation is accepted; an explicit missing remainder escalates
  once; when nothing can be proven the attempt is marked `ambiguous` and the job is
  escalated to `waiting_human` for an explicit owner `reconcile`. A proven-undispatched
  attempt is reopened; an `unknown`/unresolved outcome is never re-sent.
- **Unknown outcomes keep their reserve and occupancy.** An unresolved external
  outcome (notably a `lead` whose POST result is unknown) keeps its reserve and its
  runtime occupancy under the shared caps (§8) and moves to `waiting_human`; it is
  never re-POSTed on retry or restart, and only an explicit owner reconciliation
  releases it.

### 12.4 Cost, credential, and the direct-lead route

- **Synthetic stays virtual; no double-count.** The synthetic `submit` reserve is a
  **virtual** fault-test value with actual external spend **0** (§8); that is not
  real model billing. A discovery `lead` job reserves and settles a `direct_call`
  dispatch on the existing **runtime dispatch ledger**
  ([`runtime/dispatch.py`](../src/review_article_agent/runtime/dispatch.py)) before
  its one POST, and its cost is read from that ledger — it is **not** double-counted
  against the synthetic reserve bucket. The `query`/`seed`/`expand` modes contact only the read-only
  public-metadata endpoints and reserve nothing; a `metadata_import` job issues **zero HTTP** (a
  caller-supplied local observation) and reserves nothing.
- **Explicit worker env name; value never persisted.** The one permitted credential
  name is **`OPENROUTER_API_KEY`** ([`worker/discovery.py`](../src/review_article_agent/worker/discovery.py)
  `LEAD_CREDENTIAL_ENV`). The worker reads the variable from its **own** environment
  at dispatch; the credential **value** is never accepted over IPC, never written to
  the DB, and never logged. The CLI may ensure a worker that inherits its
  environment and warns when the key is absent, but starting/using an existing
  worker **without** the key does **not** auto-restart or reconfigure it (see
  [`cli.md`](cli.md) §12.4).
- **One guarded POST per durable job.** The direct-lead route delegates to the
  revision-scoped runner
  ([`discovery/lead_runner.py`](../src/review_article_agent/discovery/lead_runner.py)),
  which re-derives the current canonical hosted-use **batch** revision, revalidates
  the **exact named `batch` gate** and every pinned source's rights, checks the
  caps, and reserves a `direct_call` dispatch **before** the single POST. It
  machine-validates only the shape/presence/boundedness/freshness of the
  `provider_terms`/`privacy_review`/`pricing_evidence` references; their truth and
  all legal/provider-term/pricing judgments remain human-reviewed. A missing
  credential, an unapproved/stale batch, a denied rights record, or a cap block
  performs **zero** POST.
- **OpenCode production block unchanged.** The direct-lead route is a narrow,
  separate exception ([decision 003](decisions/003-step05-discovery-and-direct-lead.md));
  the existing app-owned OpenCode **production block**
  ([`runtime/execution_boundary.py`](../src/review_article_agent/runtime/execution_boundary.py))
  is **unchanged**, and no roles, ordinary-session isolation, or cost/concurrency
  defaults are altered.
- **Bounded automatic direct-call retry (decision 006, owner 2026-11-01).** A
  direct-call dispatch left **ambiguous** (`unknown` — a transport error, timeout,
  or ambiguous status) is retried automatically, **without human approval**, by
  [`runtime/retry.py`](../src/review_article_agent/runtime/retry.py): a **new**
  deterministic identity (`{base}-r1`/`{base}-r2`, at most 3 attempts; the same
  identity is never re-POSTed), the ambiguous predecessor resolved to terminal
  `failed`/`estimated` = its full reserve (it may have been billed) so the single
  serial remote slot is freed, bounded by a per-stage `30%` overhead cap over the
  stage's other spend (all existing per-role/stage/project caps still apply). When
  all attempts are ambiguous, or the cap refuses, the last attempt is resolved the
  same way and the caller reports a definitive non-completion. The
  `opencode_session` route is unchanged.
- **Untrusted sources.** Provider metadata, corpus records, imported metadata, and
  lead output are **untrusted, non-authorizing** observations; the latest-proposal
  / readback surfaces carry `authorizes_execution=false`,
  `authorizes_export=false`, and `scientific_validation=false`, and no field can
  self-grant an approval.

### 12.5 Named-gate guidance (recorded out of band)

A discovery proposal names the human gate it needs, but the worker records **no**
approval; only `gate prepare`/`gate confirm` on the controlling terminal records a
real named-gate human approval ([`cli.md`](cli.md) §11). The discovery gate names
are: individual seed revision → `artifact_revision`/`discovery.seed.<artifact_id>`;
search plan revision → `protocol`/`discovery.plan`; hosted-use batch →
`batch`/`discovery.hosted_use.<purpose>`; corpus freeze
→ `artifact_revision`/`discovery.freeze` (surfaced by `discovery freeze propose`
and required by `discovery freeze require`).

### 12.6 Known limits of this addition

- The **reporting** and **freeze** command groups are now **integrated** as CLI
  (CLI1B, `e15b5b4`): `discovery report create|show` and `discovery freeze
  propose|status|show|history|require`. [`cli.md`](cli.md) §12.1/§12.6 documents
  the exact flags; they record only non-authorizing proposals, ensure no worker,
  and record no approval. See [`discovery.md`](discovery.md) for the library
  details and the explicit per-candidate entry-review schema.
- No scientific validation, completeness, coverage, deduplication, scientific
  identity, corpus freeze, review, or writer capability is implemented or claimed,
  and no exactly-once external-request or billing claim is made. Later workflow
  steps remain unimplemented.
- **Safety tests (Step 05 addition)**:
  [`tests/test_discovery_worker.py`](../tests/test_discovery_worker.py),
  [`tests/test_discovery_job_routes.py`](../tests/test_discovery_job_routes.py),
  [`tests/test_cli_discovery_jobs.py`](../tests/test_cli_discovery_jobs.py),
  [`tests/test_cli_discovery_artifacts.py`](../tests/test_cli_discovery_artifacts.py),
  [`tests/test_cli_discovery_reports.py`](../tests/test_cli_discovery_reports.py) —
  worker discovery dispatch/replay/restart, submission route validation, and the
  discovery CLI surfaces.

## 13. Owner-controlled official retrieval jobs (Step 06 addition)

Extends §1–§12. The **only** official-full-text worker surface is `discovery_submit`
`mode=acquire` over the integrated acquisition executor; limits are in
[`acquisition.md`](acquisition.md) §7, [`cli.md`](cli.md) §16, and the [handoff](implementation/handoffs/06-acquisition.md).

- `mode=acquire` strictly/purely validates the exact parameters (freeze ref, candidate id,
  source/occurrence/report rights + revision ids, `terms_evidence`, optional
  `parent_revision_id`/`transport_options`) into one `acquisition_fulltext` job under the
  caller's `request_id`; no authority flag/URL/actor/source byte; returns a **queued admission only**.
- A may-have-dispatched mark is journaled **before** every GET and safe metadata after;
  the send admission is linearized with pause/cancel under the **captured** lease. **No
  transaction or lock is held across network work** (the store connection stays open);
  the safe result keeps refs/status/counts only, replays a proven result with zero I/O,
  and never silently re-sends an ambiguous outcome (tests: worker + retrieval CLI suites).

## 14. Owner-controlled extraction/quality runs (Step 09 addition)

Extends §1–§13. The **only** direct-extraction worker surface is `discovery_submit`
`mode=extraction_run` over the integrated
[`extraction/runner.py`](../src/review_article_agent/extraction/runner.py) `execute_run`
boundary; the runner owns every gate, right, budget, ledger, schema, and escalation
concern. The Step 09 CLI spellings are in [`cli.md`](cli.md) §18, the runner contract in
[`implementation/09-extraction.md`](implementation/09-extraction.md), and the direct
exception in [decision 004](decisions/004-step09-direct-extraction.md).

- **Exact parameters, logical key.** `mode=extraction_run` strictly/purely validates the
  exact parameters — `config_ref`, `report_refs` (a non-empty list of distinct same-project
  refs), `purpose` (`extraction`/`quality`), `mode` (`pilot`/`bulk`), `hosted_ref`, the
  optional `extraction_ref` (required for `quality`, forbidden otherwise) and `pilot_ref`
  (required for `bulk`, forbidden otherwise), the receipt `artifact_id`/`revision_id`, the
  caller's `rights` record, and the fixed `credential_env` name — into one
  `extraction_run` job under the caller's `request_id`; no actor/authority flag, no
  credential value, no model output, and no source byte. It returns a **queued admission
  only**.
- **Ownership.** [`worker/extraction.py`](../src/review_article_agent/worker/extraction.py)
  (strict submission validation, the safe result projection, the replay-only read, and the
  read seam) · [`worker/engine.py`](../src/review_article_agent/worker/engine.py)
  (`_run_extract_dispatch`) · [`worker/discovery.py`](../src/review_article_agent/worker/discovery.py)
  (the `MODE_EXTRACT`/`TASK_KIND_EXTRACT` delegation alongside `MODE_ACQUIRE`).
- **One guarded POST per durable job.** The dispatch delegates to `execute_run`, which
  re-derives the config/hosted-use revisions, revalidates the exact named `batch` gate
  (`extraction.hosted_use.<purpose>`), every pinned source's hosted-transfer rights, the
  execution token, and (in bulk mode) the approved pilot and the acquired manifest,
  reserves a `direct_call` dispatch on the runtime dispatch ledger **before** the one POST,
  and commits the receipt + proposal + accepted dispatch result + settled cost in one
  nested transaction. A missing credential, an unapproved/stale config or hosted-use
  revision, a denied rights record, or a gate/activation refusal performs **zero** POST.
- **Explicit worker env name; value never persisted.** The one permitted credential name
  is **`OPENROUTER_API_KEY`** (`worker/extraction.py` `EXTRACT_CREDENTIAL_ENV`, the same
  variable the lead job uses). The worker reads the variable from its **own** environment
  at dispatch; the credential **value** is never accepted over IPC, never written to the
  DB, and never logged.
- **Honest outcome; never a blind retry.** A terminal `completed`/`failed` outcome is
  persisted as the job's safe accepted result, which references the run **receipt**
  (`run_ref`) and carries no credential or raw output. An `unknown`/submitted external
  outcome moves the job to `waiting_human` (reserve and runtime occupancy retained) and is
  **never** re-POSTed: on restart the engine proves an existing outcome by replay from
  durable rows with **zero** POST (`worker.extraction.replay_extract`), and an unprovable
  attempt is escalated for an explicit owner `reconcile`.
- **Tests.** [`tests/test_extraction_worker.py`](../tests/test_extraction_worker.py) —
  submission validation, queued admission + asynchronous completion, receipt readback via
  `require_run`, missing credential/gate refusals, replay-is-one-run, ambiguous-outcome
  recovery, and the `extraction run` CLI surface.

## 15. Owner-controlled generation role runs (kickoff 16 §5 addition)

Extends §1–§14. The **only** model-backed generation-role worker surface is
`discovery_submit` `mode=generation_run` over the already-integrated role adapters in
[`generation/`](../src/review_article_agent/generation/) (and
[`screening/runner.py`](../src/review_article_agent/screening/runner.py) for
`screening_case`); the adapters own every gate, right, budget, ledger, schema,
and escalation concern. The CLI spellings are in [`cli.md`](cli.md) §23, and the
role reference is [`generation.md`](generation.md).

- **Exact operation, strict parameters.** `mode=generation_run` carries one closed
  `operation` plus its exact per-operation parameters — refs, the artifact/revision the
  adapter writes, the caller's `rights` record, and the fixed `credential_env` name —
  into one `generation_run` job under the caller's `request_id`; no actor/authority
  flag, no credential value, no model output, and no material. It returns a **queued
  admission only**. Every documented operation is wired: `synthesis`/`framework`/`outline`
  (`generation/synthesis_roles`); `blueprint`/`draft` (`generation/drafting_roles`);
  `section_review` (`review/workflow.SectionReviewRepository.run` with ports from
  `generation/review_roles.make_review_ports`); `global_review` and `notes_plan`
  (`generation/delivery_roles` over `delivery/workflow.DeliveryRepository`, reading the
  current manuscript/evidence from the recorded delivery state); and `screening_case`
  (`screening/runner.execute_screening_case`). No operation is refused.
- **Ownership.** [`worker/generation.py`](../src/review_article_agent/worker/generation.py)
  (strict submission validation, the per-operation dispatch, the safe result projection,
  the replay-only read, and the read seam) · [`worker/engine.py`](../src/review_article_agent/worker/engine.py)
  (`_run_generate_dispatch`) · [`worker/discovery.py`](../src/review_article_agent/worker/discovery.py)
  (the `MODE_GENERATION`/`TASK_KIND_GENERATION` delegation alongside `MODE_EXTRACT`).
- **One guarded POST per durable job.** Each adapter runs through
  `generation.runner.execute_generation_task`, which re-reads the exact hosted-use
  document and its named `batch`/`generation.hosted_use.<role>` gate, verifies every
  input revision's bytes and every original source's hosted-transfer rights, enforces
  the per-role budget cap, reserves a `direct_call` dispatch (stage `generation`) before
  the one POST, and commits the non-authoritative proposal together with the accepted
  dispatch result and settled cost. A missing credential, an unapproved/stale hosted-use
  document, a denied rights record, or a gate/budget/activation refusal performs
  **zero** POST.
- **Explicit worker env name; value never persisted.** The one permitted credential name
  is **`OPENROUTER_API_KEY`** (`worker/generation.py` `GENERATION_CREDENTIAL_ENV`, the
  same variable the lead/extraction jobs use). The worker reads the variable from its
  **own** environment at dispatch; the credential **value** is never accepted over IPC,
  never written to the DB, and never logged.
- **Honest outcome; never a blind retry.** A terminal adapter status — `proposed`,
  `invalid_proposal`, `model_not_completed`, or `complete`/`blocked` (section review) —
  is persisted as the job's safe accepted result, which references what was written (a
  workflow ref / a review state id) and carries no credential or raw output.
  `invalid_proposal` and `model_not_completed` are **terminal** results needing human
  attention, not crashes. Anything that may have been sent and has no terminal outcome
  moves the job to `waiting_human` and is **never** re-POSTed: on restart the engine
  proves an existing outcome by replay from durable rows with **zero** POST
  (`worker.generation.replay_generation`), and an unprovable attempt is escalated for an
  explicit owner `reconcile`.
- **Tests.** [`tests/test_generation_worker.py`](../tests/test_generation_worker.py) —
  submission validation, a synthesis operation end to end (stored, never approved), a
  section review through the worker, a `global_review` that completes (and one that
  blocks with bounded rounds), a `notes_plan` proposal that is never applied, a
  `screening_case` that commits a decision result, `invalid_proposal` as a terminal
  result, missing-credential refusal with zero POST, replay-is-one-run, the persisted
  read seam, and the `generation run` CLI surface.
  [`tests/test_cli_generation_hosted_use.py`](../tests/test_cli_generation_hosted_use.py)
  covers the `generation hosted-use propose|show` surface (propose → show unsatisfied →
  approve → show satisfied; malformed-document refusal with no write).
