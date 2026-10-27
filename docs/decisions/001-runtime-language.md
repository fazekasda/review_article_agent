# 001 — Runtime language and OpenCode integration

## Status

**Proposed** — 2026-10-27. Not accepted. The approving authority is the user;
nothing here is ratified and no stack-specific work may start until this record
is accepted (step 01 gate). A recommendation is not approval. This record is the
step-00 language/integration decision deliverable
(`docs/implementation/00-runtime-spike.md` line 11) and does **not** by itself
pass step 00: the acceptance criteria remain partial (criterion 4's native route
is blocked; criterion 6's global-config isolation is documented, not proven). See
[the step-00 handoff](../implementation/handoffs/00-runtime-spike.md).

**Evidence update (integrated baseline, branch `task/spike-combined`).** Criteria
1 (disconnect + second-client retrieval), 2 (idempotent admission), 3 (restart),
and 5 (fresh critic) are now verified against a **real** disposable app-owned
OpenCode v2.2.0 service for **both** prototypes; criterion 4 is met through the
direct-OpenRouter fallback that step line 19 explicitly allows, with per-question
failures still mock-only. Only the model transport was a deterministic local
fixture. The language recommendation below is unchanged and still **not
ratified**.

## Context and constraints

- Specification: `docs/specification.md` "Product and runtime" line 15 — *"Decide
  TypeScript core plus Python tools versus Python core plus supported OpenCode
  HTTP integration in a small spike. Neither is approved in advance."* Line 14 —
  the app owns an **app-owned OpenCode V2 service**, separate from ordinary coding
  sessions; the application owns projects, state, artifacts, approvals, budgets,
  and recovery. Line 16 — do not depend on OpenCode's private database or manually
  parse private credentials. Line 20 — Jev is `typesafe/jev-1.14` via OpenRouter
  through a tested typed-decision adapter.
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
- **Evidence ceiling (updated).** Both lifecycle prototypes are now exercised
  against a **real disposable app-owned OpenCode 2.2.0 service** on one integrated
  baseline (`spikes/runtime/integration/`, 16/16 checks): real HTTP Basic auth,
  real `session.create` with pinned role/model, real durable admission and SQLite
  persistence, a real server-owned agent turn, and a real owned-service restart.
  The **model transport only** is a deterministic loopback fixture
  (`mock_provider.py`) — no paid call, no credential, no egress — so model output
  content is a stub. Criterion 4's native `generate.decisions` route is still not
  exercised (paid); the only live network evidence remains the single authorized
  direct-OpenRouter Jev call. Global/ancestor configuration isolation is avoided
  by the harness but not proven against an arbitrary project tree.

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
- **Both prototypes now agree on a real service too** (`spikes/runtime/integration/`,
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

### Recommendation (proposed, not approved)

**Option B as the core: TypeScript using the official OpenCode V2 client, with
Python as a tool process** for the direct-OpenRouter Jev fallback and scientific
data tooling — **contingent on** the acceptance criteria in the handoff and on a
user decision. This is justified by evidence only in a limited sense: the
official client is TypeScript-first, so Option B reuses the supported client
instead of maintaining a hand-written HTTP integration, while the already
live-verified Jev call is preserved as a Python tool rather than discarded.

This recommendation is **not** ratified. If step 00's remaining criteria
(real client↔service lifecycle, restart/interruption, native decision route)
fail or cannot be verified, this record must be revisited before step 01 relies
on it.

## Decision

State the choice, scope, defaults, failure policy, and what is explicitly not
decided — **all pending user acceptance**:

- **Proposed choice:** TypeScript core on the official OpenCode V2 client;
  Python retained as a bounded tool process for the direct-Jev fallback and data
  work.
- **Scope:** runtime/integration language and client only. It decides nothing
  about the store schema (SQLite remains a *proposal*, `docs/specification.md`
  line 16), IPC, package layout, CLI spelling (step 02), or agent-framework
  choice.
- **Defaults / failure policy implied by the prototypes:** explicit endpoint and
  token, fail-closed; loopback-only; redirects refused; only an authoritative
  `idle` counts as completion; unknown/interrupted never silently success; a
  writer-history fork is not a clean critic; direct calls are not covered by
  session statistics and are accounted separately.
- **Explicitly not decided:** the native OpenCode `generate.decisions` route
  (unexercised; the direct fallback that line 19 allows is the path used) and any
  production architecture.

## Consequences and verification

- If accepted, step 01 may use TypeScript-core assumptions for storage/IPC/package
  layout; until then, no stack-specific implementation work may start
  (`00-runtime-spike.md` line 25).
- Migrations/invalidation: none yet — no production code exists. Accepting a
  different option (Python core) would not invalidate the Python spike; accepting
  Option B makes the TypeScript prototype the base and keeps the Python Jev client
  as a tool.
- Verification that must still happen before this is more than "proposed": the
  user's explicit acceptance of this language recommendation (and, if the native
  decision route is preferred over the documented direct fallback, its
  verification). The real client↔service lifecycle (submit, disconnect,
  second-client retrieval, restart) is **now recorded**
  (`spikes/runtime/integration/report/combined-evidence.json`).
- Conditions for revisiting: a live lifecycle failure, a decision to use the
  native decision route instead of the fallback, or a user preference for a
  Python core.

## Handoff

- Step-00 evidence and per-criterion status:
  [`docs/implementation/handoffs/00-runtime-spike.md`](../implementation/handoffs/00-runtime-spike.md).
- Prototype sources: `spikes/runtime/python/README.md`,
  `spikes/runtime/typescript/README.md`, `spikes/runtime/live-jev/README.md`,
  `spikes/runtime/harness/README.md`, `spikes/runtime/integration/README.md`.
- Live Jev evidence (sanitized, no credential, no paper text):
  `spikes/runtime/live-jev/report/live-evidence.json`.
- Combined real-service evidence (sanitized, fixture model disclosed):
  `spikes/runtime/integration/report/combined-evidence.json`.
- Unresolved follow-up: native `generate.decisions` (the direct fallback is the
  allowed path used); global config isolation; live per-question decision
  failures (mock-only). No secrets or licensed full text are included here.
