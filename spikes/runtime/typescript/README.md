# TypeScript lifecycle spike (implementation step 00)

Minimal prototype of the OpenCode V2 lifecycle using the **official**
`@opencode/client` package, built for `docs/implementation/00-runtime-spike.md`
(lines 5, 9, 15–18, 20) and the runtime section of `docs/specification.md`.

This is the TypeScript half of step 00. A sibling worker owns the Python HTTP
integration and a shared harness; this directory is self-contained and edits
nothing outside `spikes/runtime/typescript/`.

> **Scope and honesty.** These tests use a mock local transport. Passing them
> proves the client-side logic, the envelope/contract handling, and the recovery
> algorithm. It does **not** prove live acceptance against a real app-owned
> server. No paid model calls were made; the Jev live decision route is left
> deliberately unexercised (see "Accounting boundary").

## Confirmed contract (not guesses)

Verified against the published OpenAPI document (`https://opencode.ai/v2/openapi.json`,
OpenAPI 3.1.0, title `opencode HttpApi`) and the installed package's generated
descriptors in `node_modules/@opencode/client/dist/promise/generated/`.

| Concept | Operation | Route | Shape |
| --- | --- | --- | --- |
| server identity | `server.info` | `GET /api/info` | `ServerInfo` (not enveloped) |
| create session | `session.create` | `POST /api/session` | `{ data: Session.Info }` |
| read session | `session.get` | `GET /api/session/{sessionID}` | `{ data: Session.Info }` |
| **admit input** | `session.prompt` | `POST /api/session/{sessionID}/prompt` | `{ data: Session.Inbox.User }`; body `{ id?, text, delivery? }`; a repeated `id` returns `200` (idempotent) **or** `409 ConflictError` — see below |
| enqueued work | `session.inbox.list` | `GET /api/session/{sessionID}/inbox` | `{ data: Session.Inbox.Info[] }` |
| timeline | `session.message.list` | `GET /api/session/{sessionID}/message` | `{ data: Session.Message.Info[], cursor }` |
| events | `event.subscribe` | `GET /api/event` | live-only; **no replay** (missed events are missed) |
| typed decisions | `generate.decisions` | `POST /api/experimental/generate/decisions` | `{ data: GenerateDecisionResult }` |

Identifiers: `Session.ID` matches `^ses`; `Session.Message.ID` matches `^msg_`.
The client-supplied admission id is the request `id`; the server returns the
`Session.Inbox.User` it durably holds. A terminal outcome is recorded in the
message timeline as a `Session.Message.Idle` with `outcome: "succeeded" | "failed"
| "interrupted"`, and on `Session.Info.outcome`.

**Duplicate admission semantics (corrected).** A repeated admission id is *not*
always answered with `409`. A live harness retry of the same id returned `200`
with the already-held inbox item. Both mean exactly one accepted input:

- `200` — idempotent; the server returns the held `Session.Inbox.User`. This is
  indistinguishable from a first accept in the response alone, so the receipt is
  reported as `accepted`.
- `409 ConflictError` — the client reconciles against the inbox and the paginated
  timeline and reports `duplicate-accepted`, or rethrows if no record survives.

Neither result is a completion. The single-input guarantee is the server's; the
client never enqueues a second time.

The generated client unwraps `{ data: ... }` for `create`/`get`/`prompt`/`inbox.list`/`decisions`,
returns `{ data, cursor }` unchanged for `message.list`, and throws declared
errors named from the response `_tag` (e.g. `ConflictError` for 409,
`SessionNotFoundError` for 404). Undeclared statuses and body problems throw
`ClientError` with `reason` `UnexpectedStatus` / `UnsupportedContentType` /
`MalformedResponse` / `Transport`.

## Install and run

```sh
cd spikes/runtime/typescript
npm install                 # official client + typescript + @types/node
npm run build               # tsc -p tsconfig.json --noEmit
npm test                    # node --test test/*.test.ts (includes the tsc check)
```

Node 24 runs the `.ts` sources directly via type stripping; nothing is emitted.

CLI (URL/token/directory come only from arguments or environment; no service
discovery, no database or credential parsing):

```sh
node src/cli.ts check      --url http://127.0.0.1:4096 --token "$OPENCODE_TOKEN"
node src/cli.ts submit     --url ... --token ... --session ses_x --id msg_x --text "review this"
node src/cli.ts result     --url ... --token ... --session ses_x --id msg_x [--wait-ms 5000]
node src/cli.ts reconcile  --url ... --token ... --session ses_x --id msg_x
node src/cli.ts critic     --url ... --token ... --dir /workspace --agent critic --model openrouter/typesafe/jev-1.14
node src/cli.ts decisions  --url ... --token ... --model openrouter/typesafe/jev-1.14   # dry run; --live to call
```

Environment fallbacks: `OPENCODE_URL`, `OPENCODE_TOKEN` (or `OPENCODE_PASSWORD`),
`OPENCODE_ALLOW_UNAUTHENTICATED=1` (loopback only), `OPENCODE_ALLOW_NON_LOOPBACK=1`.
The client refuses an implicit URL, refuses an unauthenticated connection, and
refuses a non-loopback endpoint, all by default.

## Reconciliation semantics (bounded per turn)

`reconcile` reconstructs exactly one admission's state and never guesses:

- **Windowed.** The target turn starts at the user message whose id equals the
  admission id and ends at the next `user`/`synthetic` message (exclusive) or the
  end of the timeline. A terminal `idle` before the target (an earlier turn) or
  after the next user message (a later turn) is **not** attributed to the target.
- **Authoritative only.** Only an `idle` outcome inside the window means
  `succeeded`/`failed`/`interrupted`. An assistant message with `time.completed`
  is deliberately not accepted as a shortcut.
- **Paginated.** The timeline is walked oldest-first following `cursor.next`.
- **Bounded and honest.** A page cap, a repeated-cursor guard, and a repeated
  message-id guard stop the walk; a caller cancellation and a per-request deadline
  bound every HTTP request. Absent, unreadable, truncated, or ambiguous reads
  resolve to `unknown` — never success.

`waitForResolution` respects an external `AbortSignal` (returns `cancelled: true`)
and a per-request `requestTimeoutMs` (default: the remaining wait budget), so a
single stalled request cannot hang the caller.

## Acceptance criteria → evidence

| Criterion (00-runtime-spike.md) | Where | Notes |
| --- | --- | --- |
| Closing the prototype CLI does not stop a bounded server-owned task; a second client retrieves the result after missed events. | `test/cli.integration.test.ts` (3 separate CLI processes against a loopback listener), `test/submit-reconcile.test.ts` "a second client recovers the result after missed events" | Reconciliation reads persisted state only; no event stream is opened. Mocked transport. |
| Retrying a client-supplied admission id produces one accepted session input; admission is not mistaken for completion. | `test/submit-reconcile.test.ts`, `test/wait-bounds.test.ts` (200-idempotent retry), receipt key set asserted in `test/cli.integration.test.ts` | Both the `409` path (reconcile against paginated inbox/timeline) and the live-observed `200` idempotent path yield one accepted input. The receipt has no `outcome` field. |
| Restart yields recoverable continuation or an explicit interrupted/unknown outcome; no lost work silently declared complete. | `test/submit-reconcile.test.ts` (`interrupted`, `failed`, `unknown` cases), `test/reconcile-window.test.ts` (prior/later terminal, next-user and synthetic boundaries, assistant shortcut, pagination, repeated-cursor bound, page cap), `test/wait-bounds.test.ts` | Only an authoritative `idle` inside the target window counts; anything absent/unreadable/truncated/ambiguous is `unknown`. Mocked. |
| Typed Jev predicate/choice/score handling and per-question failures. | `test/decisions-critic.test.ts`, `src/lifecycle.ts` (`runDecisions`, `summarizeDecisions`) | The route and types are exercised against the mock; live call is **blocked** pending budget authorization. |
| Fresh critic context and role/model pinning; a writer-history fork is not a clean critic. | `test/decisions-critic.test.ts` | `createCriticSession` pins `agent` + `model.Ref` on a brand-new session (no `parentID`, no fork). |
| No access/config changes to ordinary coding sessions. | `test/client-guard.test.ts` | The client never imports `@opencode/client/service` and never calls `Service.ensure`/`discover`/`stop`; no `service.json`, database, or state path is read. |

## Accounting boundary and live unknowns

- **No paid calls were made.** `decisions` is dry-run unless `--live` is passed,
  and `--live` was never run. The Jev live criterion for step 00 remains
  **blocked**, not passed.
- 00-runtime-spike.md records two **empirical** questions this spike cannot
  answer from the client contract: whether an in-flight turn survives a service
  restart, and the exact Jev routing/accounting. Both require an authorized
  disposable app-owned server and budget approval.
- **Global/ancestor configuration inheritance risk** (00-runtime-spike.md line 21):
  OpenCode merges discovered `opencode.json(c)` and `.opencode/opencode.json(c)`
  from the current directory through every ancestor, and global filesystem
  config has lower precedence. A disposable server started from an ordinary
  project directory would therefore inherit that project's configuration. The
  isolated harness must start servers in a controlled directory and pin its own
  config; this spike only avoids touching the shared service, it does not by
  itself prove configuration isolation.

## Determinism, versions, and cleanup

Recorded versions in this checkout:

- `@opencode/client` 2.2.0 (npm `latest` at the time of writing; `dist-tags`:
  latest `2.2.0`, beta `0.0.0-beta-19507`, dev `0.0.0-dev-106`)
- `typescript` 5.9.3, `@types/node` 24.19.2, Node v24.22.1, npm 12.2.1
- Published OpenAPI: `opencode HttpApi`, `version 0.0.1` (experimental surface)

Cleanup: `rm -rf spikes/runtime/typescript/node_modules` and delete any
`*.local.json`; nothing here writes to the repository outside this directory.
