# Worker runtime safety contract (Step 03, partial)

> Scope: a concise **runtime safety contract reference** for the already-integrated Step 03
> foundation and engine — the detached-worker launcher, the owner-authenticated local control
> transport, the durable synthetic job/attempt store, the engine, and the synthetic external fixture
> backend. It changes no policy/spec/schema/CLI decision. **It is not a completion handoff, and not
> evidence Step 03 is complete**: the new-job CLI, the full crash/`SIGKILL` matrix, and final
> step-acceptance accounting are parallel/pending (a Step 03 handoff is pending). Everything here is
> synthetic; no real model, provider, network, scientific result, or approval is involved or claimed.
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
submit, job_status, job_list, pause, resume, cancel, reconcile, switch, drain, stop`. Strict per-op
key allowlist; any extra key (including a caller-supplied `uid`/`actor`) is rejected.

- **`submit` is fixed synthetic work only**: kinds `synthetic_numeric` and `synthetic_waiting_human`;
  no model, provider, credential, document, path, or scientific task, and no default scientific roles
  or scientific identity. A submit is not a real model call or a scientific/approval decision.
- **Fixed result shape**: `accepted_local_result` must be exactly `{"kind": "synthetic_numeric",
  "value": <int 0..999999>}`; any other kind, field, string payload, boolean, or out-of-range value is
  rejected before any write. No arbitrary command, payload, or data is ever executed.
- Lifecycle CLI spellings here are exactly `worker start|status|stop` ([`cli.md`](cli.md) §9);
  parallel task T5c will extend that surface. This document describes IPC version 1 and its current
  operations generically and does not speculate on future CLI.

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
  carrying continuity) blocks all other dispatch, including new jobs.
- **No dispatch while held, capped, or recovery-blocked**: not while paused/draining/cancelled, not
  when a cap would be exceeded, not while an unreadable/other-store blocker exists.
- **No busy loop**: a `waiting_human`/unknown outcome is never polled; the loop blocks until an owner
  request or a known backend deadline.
- **Caps (defaults, `POL-OPS-COST`)**: project **USD 25**, stage **USD 10**, at most **2** concurrent
  external calls. Every route counts; a reserve is required before dispatch; reaching a cap stops new
  dispatch only (no retraction of executed work, no auto-pass, not reset by restart); only an explicit
  human decision may raise a cap. Here reserves are **virtual synthetic** fault-test values and actual
  external spend is **0**.
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

- No real model/provider, network call, scientific result, or scientific approval exists in this slice;
  every task is a bounded synthetic numeric task, and no exactly-once external execution or billing is
  claimed. The recovery matrix documented by tests is not the complete `SIGKILL` matrix; the new-job
  CLI and full step acceptance are pending (Step 03 handoff, T7), and a snapshot proves storage
  integrity, not remote quiescence.

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
  document resolve to existing files. Completion claims stay unticked here: the pending CLI/matrix
  status and the full step acceptance (with latest test counts) will be recorded by the final Step 03
  handoff, not by this partial reference.
