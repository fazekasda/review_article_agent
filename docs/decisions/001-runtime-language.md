# 001 — Runtime language and OpenCode integration

## Status

**Accepted by the user — 2026-10-27.** The approving authority is the user. This
record is the step-00 language/integration decision deliverable
(`docs/implementation/00-runtime-spike.md` line 11). Step 00 passes its six
acceptance criteria (see
[the step-00 handoff](../implementation/handoffs/00-runtime-spike.md)), and on
2026-10-27 the user selected the core language/integration below, replacing the
step-00 session's *proposed* recommendation (kept as historical context).

**What this approval does and does not do.** It **selects the runtime
language/integration** for step 01 onward. It does **not** waive any acceptance
criterion of step 00 or any later step; it does **not** approve the SQLite schema,
IPC, package layout, CLI spelling, or any agent framework; and it does **not**
authorize any new paid model call. The only paid call recorded in step 00 remains
the single pre-authorized Jev call described below.

**Evidence update (integrated baseline on `feat/runtime-spike`).** Criteria 1
(disconnect + second-client retrieval), 2 (idempotent admission), 3 (restart),
and 5 (fresh critic) are verified against a **real** disposable app-owned
OpenCode v2.2.0 service for **both** prototypes; criterion 4 is met through the
direct-OpenRouter fallback that step line 19 explicitly allows, with
per-question failures still mock-only; criterion 6 is met as written (no changes
to ordinary coding sessions, with global/ancestor inheritance risks documented).
Only the model transport was a deterministic local fixture.

## Context and constraints

- Specification: `docs/specification.md` "Product and runtime" line 15 — the
  language/integration decision (now recorded there after this acceptance);
  line 14 — the app owns an **app-owned OpenCode V2 service**, separate from
  ordinary coding sessions; the application owns projects, state, artifacts,
  approvals, budgets, and recovery. Line 16 — do not depend on OpenCode's private
  database or manually parse private credentials. Line 20 — Jev is
  `typesafe/jev-1.14` via OpenRouter through a tested typed-decision adapter.
- Step: `docs/implementation/00-runtime-spike.md` (deliverables, acceptance
  criteria, exclusions). Step 01 (`docs/implementation/01-contracts.md`) chooses
  durable storage/IPC/package layout **based on the spike**, so this record is its
  precondition.
- Evidence base (all in this repository): the Python HTTP prototype
  (`spikes/runtime/python/`), the TypeScript official-client prototype
  (`spikes/runtime/typescript/`), the direct-OpenRouter Jev fallback
  (`spikes/runtime/live-jev/`), the disposable service harness
  (`spikes/runtime/harness/`), and the combined integration run
  (`spikes/runtime/integration/`). Versions actually observed in this checkout:
  Python 3.14.4; Node v24.22.1; `opencode` v2.2.0; `@opencode/client` 2.2.0;
  TypeScript 5.9.3; published `opencode HttpApi` OpenAPI 3.1.0, `version 0.0.1`
  (experimental surface); live Jev snapshot `typesafe/jev-1.14-20261014`.
- **Evidence ceiling.** Both lifecycle prototypes are exercised against a **real
  disposable app-owned OpenCode 2.2.0 service** on one integrated baseline
  (`spikes/runtime/integration/`, 16/16 checks): real HTTP Basic auth, real
  `session.create` with pinned role/model, real durable admission and SQLite
  persistence, a real server-owned agent turn, and a real owned-service restart.
  The **model transport only** is a deterministic loopback fixture
  (`mock_provider.py`) — no paid call, no credential, no egress — so model output
  content is a stub. Criterion 4's native `generate.decisions` route is still not
  exercised (paid); the only live network evidence remains the single authorized
  direct-OpenRouter Jev call. Global/ancestor configuration inheritance is
  avoided by the harness (and an ancestor-config root is refused by
  `tests/safety.sh` case C) and documented as a risk; it is not claimed as a
  proof against an arbitrary project tree — the criterion only requires the risk
  to be documented.

## Options and evidence

### Option A — Python core plus a hand-written OpenCode HTTP integration

- Implemented in `spikes/runtime/python/` (`opencode_v2.py`, stdlib-only).
- 79 `unittest` cases pass (Python 3.14.4): session create with pinned
  `agent`/`model.Ref`, admission as durable receipt, per-turn positional
  reconciliation, bounded wait, redirect/loopback/token safety, fresh critic vs
  fork, and a strict typed decision boundary validated against the published
  answer schemas.
- Verified against the real disposable service: create with pinned role/model,
  disconnect + second-process retrieval (`completed`), idempotent retry (one
  input), and a fresh pinned critic. On the in-flight **restart** it reports the
  explicit `unknown` (an errored assistant with no authoritative `idle` is no
  longer treated as `in_progress`).
- Pros: the language of the scientific/data tooling; the only live-verified model
  path (Jev) is already Python; stdlib-only, no dependency surface.
- Cons: it **re-implements the HTTP contract by hand**. Every field must track the
  experimental OpenAPI; the official client's envelope unwrapping, generated
  error `_tag`s, and descriptors are not reused, so drift risk is on us.

### Option B — TypeScript core using the official `@opencode/client`

- Implemented in `spikes/runtime/typescript/` against `@opencode/client` 2.2.0.
- 58 top-level `node --test` cases pass and `tsc --noEmit` is clean (re-run in
  this checkout after `npm ci`).
- Verified against the real disposable service: create with pinned role/model,
  disconnect + second-process retrieval (`succeeded`), idempotent retry (one
  input), and a fresh pinned critic. On the in-flight **restart** it reports the
  explicit `unknown`.
- Pros: uses the **official** client, so the envelope handling, generated error
  types (`ConflictError`, `SessionNotFoundError`, …), and descriptors follow the
  published package rather than a private re-implementation; fewer places to
  drift from the experimental API. Role/model pinning maps directly onto
  `session.create` (`agent`, `model.Ref`).
- Cons: TypeScript for the core means the Jev adapter and scientific data tooling
  either move to TS or run as a separate process; the live-verified Jev path is
  currently Python.

### Cross-cutting evidence and unknowns

- Both prototypes agree on the lifecycle semantics that matter
  (admission ≠ completion; only an authoritative `idle` completes a turn;
  unknown/interrupted are explicit; fresh critic is a new session, never a fork).
- **Both prototypes agree on a real service too** (`spikes/runtime/integration/`,
  16/16 checks): same service, real Basic auth, pinned create, real disconnect +
  second-process retrieval, idempotent retry (one input, HTTP `200`), a fresh
  pinned critic, and an in-flight **restart** after which **both** report the
  explicit `unknown` (the server wrote no terminal outcome; the turn does not
  continue).
- **Criterion 4 is met via the allowed fallback.** Line 19 explicitly permits a
  tested direct-OpenRouter fallback and accounting boundary in place of the native
  route, so the native route's absence is not by itself an unmet mandatory
  criterion; per-question decision failures remain mock-only and visible.
- **Native OpenCode `generate.decisions` route is unverified.** Only the
  documented **direct-OpenRouter fallback** was exercised live, and it is
  accounted separately from OpenCode session statistics
  (`docs/implementation/04-agent-runtime.md` line 22).
- No CI is configured in this repository.

### Recommendation made by the step-00 session (historical, not adopted)

**Historical context — the step-00 session proposed Option B, and the user did
not adopt it.** The session recommended Option B as the core: TypeScript using
the official OpenCode V2 client, with Python as a bounded tool process for the
direct-OpenRouter Jev fallback and scientific data tooling. Its stated basis was
that the official client is TypeScript-first, so Option B reuses the supported
client instead of maintaining a hand-written HTTP integration, while the
already-live-verified Jev call is preserved as a Python tool rather than
discarded. That recommendation is retained here only as the record that was
considered and rejected (see Decision for the recorded rationale — coordinator
analysis, not a stated user motive).

## Decision

**Accepted by the user, 2026-10-27:**

- **Choice:** **Python core** with a supported, explicitly authenticated OpenCode
  V2 HTTP integration (Option A). The current tested Python direct-OpenRouter Jev
  adapter (`spikes/runtime/live-jev/`) is **retained as the tested** Jev route; the
  native OpenCode `generate.decisions` route remains **unexercised** and is not
  required by step line 19 while the documented fallback is the chosen route.
- **Rejected option (recorded).** The step-00 session's TypeScript-core
  recommendation (Option B) was considered and **not adopted**: the user approved
  the Python implementation (their stated approval was "I approve the Python
  implementation"), and gave no motive, so none is attributed to them.
- **Engineering rationale and trade-offs (coordinator analysis, not the user's
  motive).** A TypeScript core would mean the already-live-verified Python Jev
  adapter and the scientific/data tooling either move to TypeScript or run as a
  second process; Option A instead accepts a **hand-maintained HTTP contract
  cost** — every field must track the experimental OpenAPI, and the official
  client's envelope handling and generated error `_tag`s are not reused, so drift
  risk is ours. This is a language/tooling trade-off only: it is **not** a claim
  that Python is faster, more accurate, or scientifically better, and it changes
  no scientific requirement.
- **Scope:** runtime/integration language and client only. It decides nothing
  about the store schema (SQLite remains a *proposal*, `docs/specification.md`
  line 16), IPC, package layout, CLI spelling (step 02), or agent-framework
  choice. Those remain step-01/02 decisions, taken with the user.
- **Defaults / failure policy implied by the prototypes:** explicit endpoint and
  token, fail-closed; loopback-only; redirects refused; only an authoritative
  `idle` counts as completion; unknown/interrupted never silently success; a
  writer-history fork is not a clean critic; direct calls are not covered by
  session statistics and are accounted separately.
- **Explicitly not decided / not authorized:** the native OpenCode
  `generate.decisions` route (unexercised; the tested direct fallback that line 19
  allows is the retained route), any production architecture, and any new paid
  call. No scientific policy, database schema, IPC, or framework choice is
  approved by this record.

## Consequences and verification

- Step 01 may now use Python-core assumptions for storage/IPC/package layout.
  Step 00's six criteria pass on the recorded evidence.
- Migrations/invalidation: none yet — no production code exists. Accepting Python
  core does not invalidate the TypeScript prototype; it remains available as a
  reference comparison, while the Python Jev client is the base adapter.
- Verification that has now happened: the user's explicit acceptance of the
  Python-core decision (this record, 2026-10-27), plus the recorded lifecycle
  evidence in `spikes/runtime/integration/report/combined-evidence.json` and the
  single live Jev call in `spikes/runtime/live-jev/report/live-evidence.json`.
- Conditions for revisiting: a live lifecycle failure, a decision to exercise and
  prefer the native `generate.decisions` route instead of the retained fallback,
  or a user decision to change the core language.

## Handoff

- Step-00 evidence and per-criterion status:
  [`docs/implementation/handoffs/00-runtime-spike.md`](../implementation/handoffs/00-runtime-spike.md).
- Prototype sources: `spikes/runtime/python/README.md`,
  `spikes/runtime/typescript/README.md`, `spikes/runtime/live-jev/README.md`,
  `spikes/runtime/harness/README.md`, `spikes/runtime/integration/README.md`.
- Step-01 kickoff: [`docs/implementation/kickoffs/01-contracts.md`](../implementation/kickoffs/01-contracts.md).
- Live Jev evidence (sanitized, no credential, no paper text):
  `spikes/runtime/live-jev/report/live-evidence.json`.
- Combined real-service evidence (sanitized, fixture model disclosed):
  `spikes/runtime/integration/report/combined-evidence.json`.
- Unresolved follow-up: native `generate.decisions` (the direct fallback is the
  retained path used); global config isolation (documented as a risk, not proven
  against an arbitrary tree); live per-question decision failures (mock-only). No
  secrets or licensed full text are included here.
