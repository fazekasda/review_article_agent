# Handoff — step 00: OpenCode integration and language spike

Status: **complete**. Step 00 is **ticked** in
[`docs/implementation/README.md`](../README.md) after a reassessment against the
six original acceptance criteria (see "Reassessment against the original six
criteria" below). On 2026-10-27 the user accepted the runtime-language decision
(**Python core** with a supported, explicitly authenticated OpenCode V2 HTTP
integration) — see
[`docs/decisions/001-runtime-language.md`](../../decisions/001-runtime-language.md).
No completion is claimed beyond the evidence below.

The real client↔service lifecycle is **verified for both prototypes on one real
disposable service** (criteria 1, 2, 3 and 5). Criterion 4 is met through the
**direct-OpenRouter fallback that step line 19 explicitly allows**; the native
route is unexercised and per-question failures remain mock-only. Criterion 6 is
met as written (no change to ordinary coding sessions; global/ancestor
inheritance risks documented). The application stays unbuilt — step 00 is a
spike, not production.

## Scope and baseline used

- Step document: `docs/implementation/00-runtime-spike.md` (scope lines 5, 9;
  acceptance lines 16–21; go/no-go line 25). Baseline: `docs/specification.md`
  "Product and runtime".
- This handoff records artefacts produced across the step-00 spike at the
  **integrated baseline on branch `task/spike-combined`** — the commit that adds
  `spikes/runtime/integration/` and folds the previously separate service harness
  and Basic-auth compatibility work into one checkout. The earlier
  documentation-only handoff was consumed at `94fd89c`; that base is superseded.
- **Decision revisions used:** at the time of the spike the language decision
  (`docs/decisions/001-runtime-language.md`) was *proposed*; the user **accepted**
  it on 2026-10-27 (**Python core**), so the runtime language is now ratified. No
  other decision record applies.
- This session adds code under `spikes/runtime/integration/` and updates this
  handoff, the ADR evidence, and the step-00 annotation. It also makes the small
  Python **repair 4** (`opencode_v2.py` and its test) so an errored assistant with
  no authoritative `idle` reconciles to `unknown`. No other prototype client code
  and no harness code was changed.

## Files and interfaces changed in this handoff

| Path | Change |
| --- | --- |
| `spikes/runtime/integration/run_lifecycle.py` | **New** orchestrator: both prototypes, one real service, recorded report, safe cleanup. |
| `spikes/runtime/integration/mock_provider.py` | **New** deterministic loopback fixture model provider (model transport only). |
| `spikes/runtime/integration/py_lifecycle.py` | **New** per-step worker for the Python prototype (separate process per step). |
| `spikes/runtime/integration/ts_lifecycle.ts` | **New** per-step worker for the TypeScript prototype (real `@opencode/client`). |
| `spikes/runtime/integration/test_mock_provider.py` | **New** 7 offline unit tests for the fixture helpers. |
| `spikes/runtime/integration/report/combined-evidence.json` | **New** sanitized evidence from the real run. |
| `spikes/runtime/integration/README.md`, `package.json`, `.gitignore` | **New** documentation and ESM/module config for the directory. |
| `spikes/runtime/python/opencode_v2.py` | **Repair 4 (small):** an assistant that ended in error with no authoritative `idle` now reconciles to `unknown` (not `in_progress`) — the observed post-restart state. |
| `spikes/runtime/python/test_opencode_v2.py` | Updated the errored-assistant expectation and **added a restart regression** (79 cases now). |
| `spikes/runtime/integration/run_lifecycle.py` | Restart assertion now requires the **explicit** `unknown`/`interrupted`, not merely "≠ success". |
| `docs/implementation/handoffs/00-runtime-spike.md` | This report (rewritten for the integrated baseline). |
| `docs/decisions/001-runtime-language.md` | ADR evidence updated (status was *Proposed* at spike time; **accepted** by the user on 2026-10-27 — Python core). |
| `docs/implementation/README.md` | Step-00 annotation updated (the checkbox was left unticked at spike time; it is ticked after the reassessment below). |

No public interface or migration is introduced. Compatibility implication: none —
the step-00 code lives only under `spikes/runtime/` and is not wired into any
application. The only prototype change is the Python **repair 4** above; no other
prototype client code, no harness code, and no other test was modified.

## Prototype artefacts (where the evidence lives)

| Artefact | Language/runtime | Tests | Live service? |
| --- | --- | --- | --- |
| `spikes/runtime/integration/` | Python 3.14.4 + Node v24.22.1 orchestrator | 16 real checks + 7 offline fixture tests | **REAL service, fixture model** |
| `spikes/runtime/python/` | Python 3.14.4, stdlib only | 79 `unittest` cases | Mock only |
| `spikes/runtime/typescript/` | Node v24.22.1, `@opencode/client` 2.2.0, TypeScript 5.9.3 | 58 `node --test` cases + strict `tsc --noEmit` | Mock only |
| `spikes/runtime/harness/` | bash + `curl` + `python3` | 28 smoke + 31 safety assertions (59) | **REAL app-owned service** |
| `spikes/runtime/live-jev/` | Python 3.14.4, stdlib only | 51 `unittest` cases | 1 authorized **live** call |

Observed versions: `opencode` v2.2.0; published `opencode HttpApi` OpenAPI 3.1.0
`version 0.0.1` (experimental); live Jev response model
`typesafe/jev-1.14-20261014` from provider `TypeSafe`; Node v24.22.1; Python
3.14.4.

**Fixture disclosure (do not over-read the evidence).** In the integration run
only the **model transport** is a fixture (`mock_provider.py`, a loopback
OpenAI-compatible server with a deterministic reply and a bounded delay). The
service process, HTTP API, Basic auth, session create with explicit role/model,
prompt admission, SQLite persistence, the server-owned agent turn, and the
owned-service restart are **real** 2.2.0 behaviour. No paid call, no provider
credential, no network egress.

## Commands run and actual results (this checkout)

All commands below were run in this checkout, offline, with **no paid calls**:

```sh
# Combined integration: both prototypes on one real disposable service
cd spikes/runtime/integration
python3 run_lifecycle.py
#   checks: 16/16 passed, 0 failed   (exit 0)   -> report/combined-evidence.json
python3 -m unittest test_mock_provider
#   Ran 7 tests — OK

# Existing harness self-tests (real app-owned service)
HARNESS_ROOT=/tmp/opencode/<unique> bash spikes/runtime/harness/harness.sh selftest
#   == summary: 28 passed, 0 failed ==   (smoke)
#   == summary: 31 passed, 0 failed ==   (safety)   [59 total]

# Prototype suites (re-run in this checkout)
cd spikes/runtime/python && python3 -m unittest test_opencode_v2
#   Ran 79 tests — OK
cd spikes/runtime/typescript && npm ci && npm run build && npm test
#   tsc --noEmit clean; tests 58, pass 58, fail 0

# Versions
opencode --version ; node --version ; python3 --version   # v2.2.0 ; v24.22.1 ; 3.14.4
```

**Unrun checks (explicitly not verified):**

- The native OpenCode `generate.decisions` route: **not run** (would be a paid
  call; no budget authorization in this task). Line 19 explicitly allows a tested
  direct-OpenRouter fallback instead, which is verified — see criterion 4.
- Per-question decision failures against the live route: **not run**; exercised
  **mock-only** and kept labelled as such.
- Global/ancestor configuration *isolation* against a real ordinary project:
  documented as a risk; not empirically proven (only the harness's own
  env-scrub/containment assertions cover it).
- No CI is configured in this repository, so no pipeline result exists.

## Acceptance criteria — pass / partial / blocked

`docs/implementation/00-runtime-spike.md` lines 16–21.

### 1. Closing the prototype CLI does not stop a bounded server-owned task; a second client retrieves its result after missed events — **PASS (real, both prototypes)**

- Evidence: `spikes/runtime/integration/run_lifecycle.py` submits the prompt in a
  **separate OS process that exits** (`py_lifecycle.py`/`ts_lifecycle.ts`
  `submit`), then a **different** process (`reconcile`) recovers the outcome from
  persisted inbox/message state only; no event stream is opened.
- Observed against the real service: Python `completed` (`outcome=succeeded`),
  TypeScript `succeeded`. The server-owned turn ran the fixture transport while no
  client was connected.
- Report keys: `observations.python.disconnect_resolution`,
  `observations.ts.disconnect_resolution`.

### 2. Retrying a client-supplied admission ID produces one accepted input; admission is not mistaken for completion — **PASS (real, both prototypes)**

- Evidence: retrying the same id against the real 2.2.0 service returned HTTP
  **200** (idempotent — *not* `409`), and the session timeline contained exactly
  **one** `user` message with that id for each prototype. The durable receipt has
  no `outcome` field, so it is never a completion.
- The fixture request log shows **no second model call** for the retry: one
  accepted input, no re-execution.
- **Anomaly observed (server-side):** the retry emitted an **extra `idle`
  message** with no matching user message. Both clients' windowed reconciliation
  still resolves correctly; this is noted, not attributed to the clients.
- Report keys: `observations.<proto>.retry`, `.retry_messages`.

### 3. Restarting only the disposable service yields recoverable continuation or an explicit interrupted/unknown outcome; no lost work is silently declared complete — **PASS (real, both prototypes)**

- Evidence: two turns (Python + TypeScript) were in flight (fixture reached, no
  session `outcome`) when `harness.sh restart` replaced **only** the owned
  process. After restart, persistence was recovered (session + admitted user
  message present) and no terminal `idle` was written; `Session.Info.outcome`
  stayed absent. **Both** prototypes now return the explicit `unknown`.
- **Finding (the step's central question).** The in-flight turn does **not**
  continue across a restart, and the server does **not** mark it `interrupted`; a
  partial `assistant` (`finish: "error"`) is persisted with no terminal. Re-reading
  after the original delay elapsed changed nothing.
- **Repair 4 (small).** The Python prototype previously reported `in_progress`
  (never success, but not the explicit state the criterion names). It now treats a
  window whose assistant ended in error with no authoritative `idle` as
  **`unknown`** — the observed post-restart state — while keeping ordinary live
  `in_progress` for non-errored in-flight turns (`finish=tool-calls`, `stop`,
  `length`, `content-filter`) and never inventing a confident
  `failed`/`interrupted` without an authoritative terminal.
- Report keys: `observations.restart`.

### 4. Typed Jev predicate/choice/score handling and per-question failures via the selected route, or a documented direct-OpenRouter fallback with an accounting boundary — **PASS (via the documented fallback that step 19 explicitly allows)**

- **Per-question failures: BLOCKED live / mock-only (stays visible).** The
  published Decisions `answers` union is `noul|choice|score|box` with no
  per-question `error` variant, so a live per-question failure cannot be induced
  safely; the missing/error paths are exercised offline only and remain labelled
  mock-only.
- **Direct-OpenRouter fallback: PASSED for that route, and this is the allowed
  path.** `docs/implementation/00-runtime-spike.md` line 19 explicitly permits
  *"a tested direct-OpenRouter fallback and accounting boundary"* in place of the
  native route, so the absence of the native `generate.decisions` verification is
  **not, by itself, an unmet mandatory acceptance**. Evidence
  (`spikes/runtime/live-jev/report/live-evidence.json`): one authorized live call,
  HTTP 200, model `typesafe/jev-1.14-20261014` / `TypeSafe`, three valid typed
  answers — predicate (`is_original_report`) `0.83`; choice (`study_design`)
  `randomised`, confidence `0.36`; score (`confidence_level`) `1.09`, legend
  `{0:"low",1:"moderate",2:"high"}`. `per_question_errors` empty; cost
  **`$0.00002037`** (provider-reported) against a `$0.25` ceiling. Verified
  separately; this task made **no** paid calls.
- **Native OpenCode `generate.decisions` route: not exercised** (would be a paid
  call). It is not required by line 19 provided the documented fallback is the
  chosen route — which remains a **user decision** recorded in the ADR.

### 5. Fresh critic context and role/model pinning; a writer-history fork is not a clean critic — **PASS (real metadata, both prototypes)**

- Evidence: for both prototypes, `session.create` returned a brand-new session
  with pinned `agent=plan`, `model=mockprov/critic-echo` (different from the writer
  model `mockprov/delayed-echo`), **no `parentID`**, and **0 history** messages —
  a real server-side session, not a fork of writer history. Fork vs fresh is still
  contrasted only in the mock unit tests (`test_critic_session_is_fresh_not_fork`,
  `test/decisions-critic.test.ts`).
- Report keys: `observations.<proto>.critic`.

### 6. No access/config changes to ordinary coding sessions; document global/ancestor configuration inheritance risks — **PASS (as written)**

This criterion has two parts: demonstrate no access/config changes to ordinary
coding sessions, and **document** global/ancestor configuration inheritance
risks. It does **not** require proving isolation against an arbitrary project
tree.

- **No ordinary-service change (demonstrated).** `tests/smoke.sh` records the
  ordinary shared-service state before the run and asserts it is unchanged
  afterwards: "ordinary service endpoint unchanged" (`opencode service status`)
  and "ordinary service registration unchanged" (sha256 of
  `~/.local/state/opencode/service.json`). The integration run uses a **unique
  owned `HARNESS_ROOT`**, the harness's scrubbed child environment, and
  **owned-only** stop/restart; it never calls `opencode service …`. The same
  smoke suite asserts child-env/config containment: no inherited
  `OPENCODE_CONFIG`/`OPENCHAMBER_*`/`AI_AGENT`, child `HOME` is the scratch home,
  and no config source outside the scratch root. `safety.sh` case C additionally
  refuses a root whose ancestor holds `opencode.json`. `test/client-guard.test.ts`
  proves the TS client never imports `@opencode/client/service`; the Python client
  ignores `OPENROUTER_API_KEY`.
- **Global/ancestor config inheritance risk (documented).** OpenCode merges
  discovered `opencode.json(c)` and `.opencode/opencode.json(c)` from the current
  directory through every ancestor. The harness starts the child in
  `$HARNESS_ROOT/cwd` and refuses roots whose ancestors contain config, which
  *avoids* the risk; the risk itself is documented here and in the harness README
  as an inheritance risk, not claimed as proven isolation against an arbitrary
  project tree.

### Step-00 deliverables

- Submit / durable admission receipt / completion retrieval / disconnect-reconnect
  / typed decisions: present for both languages; the first four are **verified on
  one real service**; typed decisions are verified via the allowed direct-OpenRouter
  fallback, with the native route unexercised and per-question failures mock-only.
- Tested client/server/API versions, supported discovery/auth, effective
  configuration, isolation: recorded in the prototype and integration READMEs;
  owned-service isolation demonstrated; global config isolation still documented
  as a risk.
- Language decision: **accepted by the user 2026-10-27** — Python core with a
  supported, explicitly authenticated OpenCode V2 HTTP integration; TypeScript is
  retained as reference only. See ADR `001`.
- Written restart findings: which state persists / whether an in-flight turn
  continues / interruption detection — **answered empirically**: sessions and
  admitted user inputs (and a partial assistant `finish:error`) persist; an
  in-flight turn does **not** continue; the server records **no** terminal outcome,
  so the honest client answer is the explicit **`unknown`** (both prototypes), never
  success.

## Stubs, limitations, open decisions

- **Stubs (disclosed, not production):** the integration model transport is a
  deterministic local fixture (`spikes/runtime/integration/mock_provider.py`); the
  reply content is a stub. The service/admission/persistence/restart under it are
  real. The Python and TypeScript prototype suites still run against their local
  mocks (`spikes/runtime/python/mock_server.py`, TS test transport).
- **Retry anomaly:** a same-id retry produced an extra server-side `idle` with no
  user message (no second model call). Recorded as an observation, not a client
  fix.
- **Decision resolved:** the language/integration choice (ADR `001`) was accepted
  by the user on 2026-10-27 (**Python core** with a supported, explicitly
  authenticated OpenCode V2 HTTP integration); TypeScript is retained as
  reference only. Step 01 may now begin.
- The native OpenCode `generate.decisions` route, real `409`/`503`/paging
  behaviour, per-question failures on the live route, and global config isolation
  remain unverified. None of these is by itself an unmet mandatory criterion where
  line 19 permits the direct fallback; the per-question failure gap stays visible.

## Security, cost, cleanup

- **Security:** no shared/ordinary OpenCode service was touched, started, or
  restarted by any step-00 prototype. The integration run owns a unique root under
  `/tmp/opencode`, injects exactly one fixture provider into that root's config,
  and uses the harness's guarded lifecycle. No private database, service
  registration, or credential file was read or parsed. Endpoints are explicit,
  loopback-only, fail-closed; redirects are refused so the credential is never
  forwarded. No secret is printed, logged, or committed; the committed report
  records endpoints/ports only.
- **Cost incurred:** **$0.00002037** total across step 00 — the single authorized
  Jev call (ceiling `$0.25`). This task made **no** paid calls; all integration and
  prototype tests are offline and free.
- **Safe cleanup:** the integration run always calls `harness.sh cleanup` and
  removes its own `.lock`. With `--keep`, remove the printed root with
  `bash spikes/runtime/harness/harness.sh cleanup` under the same `HARNESS_ROOT`.
  The prototype suites write nothing outside their directories; restore any edit
  to the ordinary service's configuration only if one was made — none was.

## Reassessment against the original six criteria

Step 00 was reassessed strictly against the six criteria **as written** in
`docs/implementation/00-runtime-spike.md` lines 16–21 — not against a stronger
"prove isolation against an arbitrary project tree" reading that criterion 6 never
asked for:

| # | Criterion (as written) | Status | Basis |
| --- | --- | --- | --- |
| 1 | Closing the CLI does not stop the server-owned task; a second client retrieves the result | **PASS** | real service, both prototypes |
| 2 | Retrying an admission ID yields one accepted input; admission ≠ completion | **PASS** | real service, both prototypes |
| 3 | Restarting only the disposable service yields continuation or an explicit unknown/interrupted | **PASS** | real service, both prototypes → explicit `unknown` |
| 4 | Typed predicate/choice/score and per-question failures via the selected route, **or** a documented direct-OpenRouter fallback with accounting boundary | **PASS** | fallback tested live ($0.00002037 ≤ $0.25); native route unexercised; per-question failures mock-only and disclosed |
| 5 | Fresh critic context and role/model pinning; a fork is not automatically a clean critic | **PASS** | real metadata, both prototypes; fork-vs-fresh contrasted in mock unit tests |
| 6 | No access/config changes to ordinary coding sessions; **document** global/ancestor inheritance risks | **PASS** | `smoke.sh` asserts ordinary endpoint + registration unchanged and env/config contained; `safety.sh` case C refuses an ancestor-config root; the risk is documented |

All six are evidenced, with the limits stated honestly (the model transport is a
fixture; the native decision route and live per-question failures are unexercised;
config inheritance is documented rather than proven against an arbitrary tree). On
that basis the plan index ticks step 00, and the user accepted the language
decision.

## Exact next actions

1. Begin step 01 (`docs/implementation/01-contracts.md`) — a contracts/decision
   session before substantial implementation. Its
   [kickoff](../kickoffs/01-contracts.md) carries the read order and the open user
   decisions (P1–P6 and friends).
2. The retained Jev route is the tested direct-OpenRouter fallback plus its
   accounting boundary (which line 19 allows). The native `generate.decisions`
   route remains unexercised and is **not** required while that fallback is the
   chosen route; do not run a native paid verification without explicit user
   authorization and a budget.

## Commits

The integration code, this handoff, and the ADR/step-00 evidence updates are on
branch `task/spike-combined`: `486aa64` adds `spikes/runtime/integration/` and the
first evidence update; the follow-up commit(s) apply the small Python reconciliation
repair, the tightened integration restart assertion, the refreshed evidence, and
these doc updates; the coordinator records the integration-log entry. Predecessor
commits on this branch: `2682f4a` (Basic-auth compatibility), `0394d17` (earlier
handoff), `94da0ca` (harness repair 1), `cb3ca70` (harness). Prototype commits on
other branches: `5cdbbe1`, `71e0633`, `00c3c46`, `cf99a6b` (Python), `1567b8a`,
`cbf79be` (TypeScript), `683177d`, `94fd89c` (live-jev).
