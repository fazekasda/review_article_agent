# 001 — Runtime language and OpenCode integration

## Status

**Proposed** — 2026-10-27. Not accepted. The approving authority is the user;
nothing here is ratified and no stack-specific work may start until this record
is accepted (step 01 gate). A recommendation is not approval. This record is the
step-00 language/integration decision deliverable
(`docs/implementation/00-runtime-spike.md` line 11) and does **not** by itself
pass step 00: the acceptance criteria remain partial/blocked
(see [the step-00 handoff](../implementation/handoffs/00-runtime-spike.md)).

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
  (`spikes/runtime/typescript/`), and the direct-OpenRouter Jev fallback
  (`spikes/runtime/live-jev/`). Versions actually observed in this checkout:
  Python 3.14.4; Node v24.22.1; `opencode` v2.2.0; `@opencode/client` 2.2.0;
  TypeScript 5.9.3; published `opencode HttpApi` OpenAPI 3.1.0, `version 0.0.1`
  (experimental surface); live Jev snapshot `typesafe/jev-1.14-20261014`.
- **Evidence ceiling.** Both lifecycle prototypes were exercised against a
  **mock local transport only**. No live app-owned OpenCode service was started
  and no lifecycle acceptance was proven against a real server. The only live
  network evidence is the single authorized direct-OpenRouter Jev call. The
  disposable service harness and the real client↔service combination are **not
  integrated** in this record's baseline (`94fd89c`); see "Unknowns".

## Options and evidence

### Option A — Python core plus a hand-written OpenCode HTTP integration

- Implemented in `spikes/runtime/python/` (`opencode_v2.py`, stdlib-only).
- 59 `unittest` cases pass (Python 3.14.4): session create with pinned
  `agent`/`model.Ref`, admission as durable receipt, per-turn positional
  reconciliation, bounded wait, redirect/loopback/token safety, fresh critic vs
  fork, and a strict typed decision boundary validated against the published
  answer schemas.
- Pros: the language of the scientific/data tooling; the only live-verified model
  path (Jev) is already Python; stdlib-only, no dependency surface.
- Cons: it **re-implements the HTTP contract by hand**. Every field must track the
  experimental OpenAPI; the official client's envelope unwrapping, generated
  error `_tag`s, and descriptors are not reused, so drift risk is on us. Live
  server behaviour (paging, `409`/`503`, message projection) is unverified here.

### Option B — TypeScript core using the official `@opencode/client`

- Implemented in `spikes/runtime/typescript/` against `@opencode/client` 2.2.0.
- 47 top-level `node --test` cases and a strict `tsc --noEmit` compile; not
  re-executed in the step-00 handoff checkout because `node_modules` is absent
  (the count is a static count of test declarations; the integrator reports 47
  passing).
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
  (still unverified), the disposable-service isolation model (harness work is not
  integrated), and any production architecture.

## Consequences and verification

- If accepted, step 01 may use TypeScript-core assumptions for storage/IPC/package
  layout; until then, no stack-specific implementation work may start
  (`00-runtime-spike.md` line 25).
- Migrations/invalidation: none yet — no production code exists. Accepting a
  different option (Python core) would not invalidate the Python spike; accepting
  Option B makes the TypeScript prototype the base and keeps the Python Jev client
  as a tool.
- Verification that must still happen before this is more than "proposed": a real
  client↔app-owned-service lifecycle run (submit, disconnect, second-client
  retrieval, restart/interruption), and either the native decision route or an
  explicit acceptance of the direct fallback boundary. No tests were added by
  this record; it changes documentation only.
- Conditions for revisiting: a live lifecycle failure, a decision to use the
  native decision route instead of the fallback, or a user preference for a
  Python core.

## Handoff

- Step-00 evidence and per-criterion status:
  [`docs/implementation/handoffs/00-runtime-spike.md`](../implementation/handoffs/00-runtime-spike.md).
- Prototype sources: `spikes/runtime/python/README.md`,
  `spikes/runtime/typescript/README.md`, `spikes/runtime/live-jev/README.md`.
- Live Jev evidence (sanitized, no credential, no paper text):
  `spikes/runtime/live-jev/report/live-evidence.json`.
- Unresolved follow-up: real client↔service lifecycle and restart/interruption;
  native `generate.decisions`; harness isolation integration. No secrets or
  licensed full text are included here.
