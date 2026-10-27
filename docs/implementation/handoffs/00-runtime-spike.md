# Handoff — step 00: OpenCode integration and language spike

Status: **partial / blocked**. Step 00 is **not** complete and is **not** ticked
in [`docs/implementation/README.md`](../README.md). This is a truthful handoff of
the evidence that exists, plus a **proposed** (not approved) language decision in
[`docs/decisions/001-runtime-language.md`](../../decisions/001-runtime-language.md).
No completion is claimed beyond the evidence below; the native OpenCode decision
route and the real client↔service lifecycle are unverified.

## Scope and baseline used

- Step document: `docs/implementation/00-runtime-spike.md` (scope lines 5, 9;
  acceptance lines 16–21; go/no-go line 25). Baseline: `docs/specification.md`
  "Product and runtime".
- This handoff records artefacts produced across the step-00 spike, consumed at
  the integration baseline **`94fd89c`** (branch `task/spike-handoff`).
- **Decision revisions used:** the language decision is the *proposed*
  `docs/decisions/001-runtime-language.md`; it is not accepted, so the stack is
  **not ratified**. No other decision record applies.
- This session changes **documentation only** (this file, the ADR, and the two
  README annotations); no code and no tests were added or modified.

## Files and interfaces changed in this handoff

| Path | Change |
| --- | --- |
| `docs/implementation/handoffs/00-runtime-spike.md` | This report (new). |
| `docs/decisions/001-runtime-language.md` | Proposed language/integration ADR (new). |
| `README.md` | Corrected "implementation has not started" to state that step 00 produced prototypes but no production application features. |
| `docs/implementation/README.md` | Annotated step 00 as **partial** (checkbox left **unticked**); step 01 remains blocked. |

No public interface or migration is introduced. Compatibility implication: none —
the step-00 code lives only under `spikes/runtime/` and is not wired into any
application.

## Prototype artefacts (where the evidence lives)

| Artefact | Language/runtime | Tests | Live? |
| --- | --- | --- | --- |
| `spikes/runtime/python/` | Python 3.14.4, stdlib only | 59 `unittest` cases | **Mock only** |
| `spikes/runtime/typescript/` | Node v24.22.1, `@opencode/client` 2.2.0, TypeScript 5.9.3 | 47 top-level `node --test` cases + strict `tsc --noEmit` | **Mock only** |
| `spikes/runtime/live-jev/` | Python 3.14.4, stdlib only | 51 `unittest` cases | 1 authorized **live** call |

Observed versions: `opencode` v2.2.0; published `opencode HttpApi` OpenAPI 3.1.0
`version 0.0.1` (experimental); live Jev response model
`typesafe/jev-1.14-20261014` from provider `TypeSafe`.

## Commands run and actual results (this checkout)

All commands below were run in `spike-handoff` at `94fd89c`, offline, with **no
paid calls**:

```sh
# Python lifecycle prototype — 59 tests
cd spikes/runtime/python && python3 -m unittest test_opencode_v2
#   Ran 59 tests in 1.961s — OK            (Python 3.14.4)

# Direct-OpenRouter Jev fallback (offline suite) — 51 tests
cd spikes/runtime/live-jev && python3 -m unittest test_live_jev
#   Ran 51 tests in 0.010s — OK

# Environment probes
node --version            # v24.22.1
opencode --version        # 2.2.0
python3 --version         # Python 3.14.4
```

**Unrun checks (explicitly not verified):**

- TypeScript suite (`npm install && npm run build && npm test`): **not re-run** in
  this checkout because `spikes/runtime/typescript/node_modules` is absent.
  Installing it would require an npm fetch, outside this documentation-only
  handoff. The "47 tests / strict tsc" figure is the integrator's reported result
  and a static count (47 top-level `test(`/`it(` declarations) in this checkout.
- Any live lifecycle against a real app-owned OpenCode service: **not run**.
- Native OpenCode `generate.decisions` route: **not run**.
- No CI is configured in this repository, so no pipeline result exists.

## Acceptance criteria — pass / partial / blocked

`docs/implementation/00-runtime-spike.md` lines 16–21. "Partial" means the client
behaviour is proven against a **mock** local transport but not against a real
service.

### 1. Closing the prototype CLI does not stop a bounded server-owned task; a second client retrieves its result after missed events — **PARTIAL**

- Evidence: `spikes/runtime/python/demo_lifecycle.py` (two-client durability
  demo) and `spikes/runtime/typescript/test/cli.integration.test.ts` (3 separate
  CLI processes against a loopback listener), both against the **mock**.
- Reconciliation reads persisted state only; no event stream is opened, so missed
  events are irrelevant to retrieval.
- **Honest limit:** the "server-owned task" is the mock, and the disconnected
  "CLI" is a mock client process. This does **not** prove that closing a real
  prototype CLI leaves a real app-owned OpenCode turn running. A mock CLI
  disconnect is not a real task. The disposable-service harness that would prove
  this is **not integrated** at this baseline (see "Not integrated / in progress").

### 2. Retrying a client-supplied admission ID produces one accepted input; admission is not mistaken for completion — **PARTIAL**

- Evidence: `test_retry_same_admission_id_yields_single_input` (Python);
  `test/submit-reconcile.test.ts`, `test/wait-bounds.test.ts` (TS). The
  client-supplied admission id is the idempotency key; the receipt has no
  `outcome` field, so a receipt is never a completion.
- Duplicate semantics: the mock answers a repeat with `409 ConflictError`; a live
  `2.2.0` harness retry was observed returning `200` (idempotent, one input). Both
  paths are handled and never enqueue a second input. The `200` variant is
  indistinguishable from a first accept in the response alone.
- **Honest limit:** mock-only in this checkout; the single live `200` observation
  comes from the harness work that is not integrated here.

### 3. Restarting only the disposable service yields recoverable continuation or an explicit interrupted/unknown outcome; no lost work silently declared complete — **BLOCKED (live) / PARTIAL (client logic)**

- Evidence (client logic): `test/reconcile-window.test.ts` (prior/later terminal,
  next-user and synthetic boundaries, assistant-shortcut rejection, pagination,
  repeated-cursor bound, page cap), `test/submit-reconcile.test.ts`
  (`interrupted`/`failed`/`unknown`), `test_bounded_wait_times_out`. Only an
  authoritative `idle` inside the target window means `succeeded`; anything
  absent/unreadable/truncated/ambiguous resolves to `unknown`.
- **Blocked part:** no real disposable service was restarted, and no in-flight
  turn was observed surviving (or not surviving) a real restart. This is the
  central empirical question the step document flags (line 12) and it is
  **unanswered**. Mock connection-loss/unknown-ID paths do not substitute for it.

### 4. Typed Jev predicate/choice/score handling and per-question failures via the selected route, or a documented direct-OpenRouter fallback with an accounting boundary — **PARTIAL**

- **Native OpenCode `generate.decisions` route: BLOCKED.** Exercised against the
  mock only (`test/decisions-critic.test.ts`, Python strict boundary); never
  called live. No native-route acceptance is claimed.
- **Direct-OpenRouter fallback: PASSED for that route.** One authorized live call
  (`spikes/runtime/live-jev/report/live-evidence.json`): HTTP 200, model
  `typesafe/jev-1.14-20261014` / `TypeSafe`, three valid typed answers —
  predicate (`is_original_report`) `0.83`; choice (`study_design`) `randomised`,
  confidence `0.36`; score (`confidence_level`) `1.09`, legend
  `{0:"low",1:"moderate",2:"high"}`. `per_question_errors` empty; cost
  **`$0.00002037`** (provider-reported), against a `$0.25` ceiling.
- **Per-question failures: BLOCKED live / mock-only.** The published Decisions
  `answers` union is `noul|choice|score|box` with **no per-question `error`
  variant**, so a per-question failure cannot be induced safely against the live
  API; the missing/error paths are exercised offline only and are labelled
  mock-only.
- **Honest limit:** a passing direct call does **not** prove the native OpenCode
  route works; direct calls are accounted separately from OpenCode session
  statistics (`docs/implementation/04-agent-runtime.md` line 22).

### 5. Fresh critic context and role/model pinning; a writer-history fork is not a clean critic — **PARTIAL**

- Evidence: `test_critic_session_is_fresh_not_fork`, `test_fork_carries_writer_history`,
  `test_critic_model_must_differ` (Python); `test/decisions-critic.test.ts` (TS —
  `createCriticSession` pins `agent` + `model.Ref` on a brand-new session, no
  `parentID`, no fork). Mock-only; role/model pinning was not verified on a live
  service.

### 6. No access/config changes to ordinary coding sessions; document global/ancestor configuration inheritance risks — **PARTIAL**

- Evidence: `test/client-guard.test.ts` proves the client never imports
  `@opencode/client/service` and never calls `Service.ensure`/`discover`/`stop`,
  and reads no `service.json`, database, or state path. Both prototypes require
  an explicit endpoint and token, fail closed, enforce loopback, and refuse
  redirects; the Python client ignores `OPENROUTER_API_KEY` (a provider key is
  not an endpoint token). No private DB or credential parsing anywhere.
- **Global/ancestor config inheritance risk (documented):** OpenCode merges
  discovered `opencode.json(c)` and `.opencode/opencode.json(c)` from the current
  directory through every ancestor, with global filesystem config at lower
  precedence. A disposable server started from an ordinary project directory
  would inherit that project's configuration. The spike only *avoids* the shared
  service; it does not by itself prove configuration isolation. The harness that
  would (env scrub, isolated `HOME`/`XDG`) is **not integrated** here.

### Step-00 deliverables

- Submit / durable admission receipt / completion retrieval / disconnect-reconnect
  / typed decisions: present for both languages, **mock-level**.
- Tested client/server/API versions, discovery/auth, configuration, isolation:
  recorded in the two prototype READMEs; live isolation not yet demonstrated.
- Language decision: **proposed** in the ADR; not accepted.
- Written restart findings: which state persists / whether an in-flight turn
  continues / interruption detection — **open**, mock-only evidence; the real
  question is unanswered.

## Stubs, limitations, open decisions

- **Stubs (disclosed, will not be presented as production):** the TypeScript and
  Python lifecycle prototypes run against a deterministic local mock
  (`spikes/runtime/python/mock_server.py`, TS test transport). The mock models
  "accepted then connection lost" and "unknown ID" only; real restart/recovery is
  not modelled.
- **Not integrated at this baseline (`94fd89c`):** the disposable app-owned
  service harness (branch `task/spike-harness`) and Basic-auth compatibility work
  (branch `task/spike-compat`). They are separate branches, not in this base.
  Therefore there is **no real client↔service combined proof** and **no in-flight
  restart/disconnect proof**. The step-00 acceptance cannot be marked passed until
  those are integrated and a live run is recorded.
- **Open decision:** the language/integration choice (ADR `001`, **Proposed**).
  Stack not ratified. Step 01 stays blocked until step 00 passes or the user
  explicitly accepts the partial state and the decision.
- The native OpenCode `generate.decisions` route, real server versions and
  `409`/`503`/paging behaviour, and per-question failures on the live route remain
  unverified.

## Security, cost, cleanup

- **Security:** no shared/ordinary OpenCode service was touched, started, or
  restarted by any step-00 prototype; no private database, service registration,
  or credential file was read or parsed. Endpoints are explicit, loopback-only,
  fail-closed; redirects are refused so the bearer token is never forwarded. The
  live Jev key was read from one explicit file/env name and is never printed,
  logged, or committed; `gitignore` covers `.env*` except `.env.example`. This
  handoff and the ADR contain no secrets and no licensed source text.
- **Cost incurred:** **$0.00002037** total — the single authorized Jev call
  (ceiling `$0.25`). All lifecycle tests are offline and free. No further paid
  calls are implied.
- **Safe cleanup:** the prototypes write nothing outside `spikes/runtime/`.
  Remove generated artefacts with
  `rm -rf spikes/runtime/typescript/node_modules` and any
  `spikes/runtime/typescript/*.local.json`. The scratch harness tree, when used,
  lives under `${TMPDIR:-/tmp}/opencode/spike-harness` and is removed by its own
  `bash harness.sh cleanup`. Restore any edit to the ordinary service's
  configuration only if one was made; none was.

## Exact next actions

1. **Integrate** the service harness and Basic-auth compatibility work into a
   single checkout, then run a **real** client↔app-owned-service lifecycle:
   submit → close CLI → second client retrieves → restart the disposable service
   → confirm recoverable continuation or an explicit interrupted/unknown outcome.
   Record it in this handoff. Step 00 criterion 1 and 3 depend on this.
2. Decide criterion 4 explicitly: either verify the native OpenCode
   `generate.decisions` route on the disposable service, or record acceptance of
   the direct-OpenRouter fallback plus its accounting boundary as the chosen route.
3. Obtain user disposition of the **proposed** language ADR `001`. Do **not**
   ratify a stack before that.
4. Only after step 00 passes (or the user explicitly accepts this partial state)
   may step 01 (`01-contracts.md`) begin storage/IPC/package-layout decisions.
   Until then step 01 stays blocked.

## Commits

No code was committed by this step-00 handoff; it changes documentation only. The
integration baseline is `94fd89c`. Prototype commits on other branches:
`5cdbbe1`, `71e0633`, `00c3c46`, `cf99a6b` (Python), `1567b8a`, `cbf79be`
(TypeScript), `683177d`, `94fd89c` (live-jev), `6531a6f` (harness, not
integrated). The documentation commit for this handoff is recorded in the
integration log by the coordinator.
