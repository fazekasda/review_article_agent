# Runtime spike: combined integration (both prototypes on one real service)

Step 00 (`docs/implementation/00-runtime-spike.md`) asks to compare the
TypeScript official-client prototype with the Python HTTP prototype **on the same
minimal lifecycle**, against a *disposable, app-owned* OpenCode V2 service, and to
answer the empirical restart question. This directory does exactly that: it runs
**both** prototypes through one minimal lifecycle against a **single real**
disposable service started by `spikes/runtime/harness/harness.sh`.

## What is real and what is a fixture — stated plainly

| Aspect | Real / fixture |
| --- | --- |
| OpenCode service process, HTTP API, HTTP Basic auth | **REAL** (`opencode serve` v2.2.0, owned root) |
| Session create with explicit role/model | **REAL** |
| Prompt admission, durable receipt, SQLite persistence | **REAL** |
| Server-owned agent turn running while no client is connected | **REAL** |
| Restart of only the owned service | **REAL** |
| Model transport (the LLM reply) | **FIXTURE** — `mock_provider.py`, a loopback OpenAI-compatible server with a deterministic reply and an explicit bounded delay |

The model is a fixture **only**. No paid call, no provider credential, no network
egress. The service, admission, persistence, restart, and turn are the real
2.2.0 behaviour; a critic should judge the lifecycle evidence as real and the
*content* of the model reply as a stub.

## Files

| Path | Purpose |
| --- | --- |
| `run_lifecycle.py` | Orchestrator: owns a unique `HARNESS_ROOT`, starts the fixture, provisions the service, runs both prototypes, records the report, cleans up. |
| `mock_provider.py` | Deterministic OpenAI-compatible fixture provider (loopback only; delay via `MOCK_DELAY_MS` or `DELAY_MS=<n>` in the prompt). |
| `py_lifecycle.py` | One process per step for the **Python** prototype (`spikes/runtime/python/opencode_v2.py`). |
| `ts_lifecycle.ts` | One process per step for the **TypeScript** prototype (its real `@opencode/client` modules). |
| `test_mock_provider.py` | Offline unit tests for the fixture's pure helpers. |
| `report/combined-evidence.json` | Committed, sanitized evidence captured by the last real run. |

## Run

Requires `opencode` v2.2.0 on `PATH`, Node v24 (type stripping), Python 3.14, and
`npm ci` already run in `spikes/runtime/typescript`. It writes only under
`/tmp/opencode` and cleans up after itself.

```sh
cd spikes/runtime/integration
python3 -m unittest test_mock_provider          # 7 offline fixture tests
python3 run_lifecycle.py                        # the combined real-service run
python3 run_lifecycle.py --keep                 # keep the owned root for debugging
```

`run_lifecycle.py` exits non-zero if any check fails and rewrites
`report/combined-evidence.json`.

## What the run proves (observed on this checkout)

Versions: `opencode` 2.2.0, Python 3.14.4, Node v24.22.1. 16/16 checks passed.

| Criterion (`00-runtime-spike.md` line) | Result | Evidence |
| --- | --- | --- |
| Closing the prototype CLI does not stop the server-owned task; a second client retrieves the result after missed events (16) | **PASS (real)** | Admission is submitted in a **separate OS process that exits**; a **different** process then reconciles over persisted state only (no event stream). Python `completed`, TS `succeeded`. `observations.<proto>.disconnect_resolution`. |
| Retrying a client-supplied admission ID produces one accepted input; admission is not mistaken for completion (17) | **PASS (real)** | Retry returned HTTP **200** (idempotent, not `409`) and exactly **one** `user` message with that id; the receipt carries no `outcome`. No second model call (fixture request log unchanged). `observations.<proto>.retry*`. |
| Restarting only the disposable service yields recoverable continuation or an explicit interrupted/unknown outcome; no lost work silently declared complete (18) | **PASS (real)** | Both turns were in flight (fixture reached, `outcome` absent) when `harness.sh restart` replaced only the owned process. Persistence recovered (session + user input survive). **No continuation**: the server recorded no terminal `idle` and `outcome` stayed absent. **Both** prototypes report the explicit `unknown`. `observations.restart`. |
| Typed Jev predicate/choice/score + per-question failures, or a documented direct fallback (19) | **NOT RUN here** | No paid calls in this task. The direct-OpenRouter fallback was verified separately ($0.00002037); the native `generate.decisions` route remains blocked. |
| Fresh critic context and role/model pinning; a fork is not a clean critic (20) | **PASS (real, metadata)** | For both prototypes `session.create` returns a brand-new session with pinned `agent=plan`, `model=mockprov/critic-echo` (≠ writer model), **no `parentID`**, and **0 history** messages. `observations.<proto>.critic`. |
| No access/config changes to ordinary coding sessions (21) | **PARTIAL** | The run uses a unique owned root, scrubbed child env, and owned-only restart; it never calls `opencode service …`. Isolation *risk* documentation and the client-guard tests live in the prototype/harness READMEs; this orchestrator adds an empirical owned-service run, not a proof of global config isolation. |

### Precise restart finding (the step's central question)

With the owned process stopped mid-turn (fixture streaming an 8–10 s reply):

* the session and the admitted **user message persist** across the restart;
* a **partial assistant** message is persisted with `finish: "error"`;
* **no `idle` terminal is written and `Session.Info.outcome` stays absent** — the
  turn does **not** continue, and it is not marked `interrupted` by the server;
* therefore the correct client answer is *indeterminate*: **both** prototypes
  return the explicit `unknown` (the Python prototype treats an errored assistant
  with no authoritative `idle` as `unknown`, not `in_progress`). Re-reading after
  the original delay elapsed changed nothing.

## Honest caveats and stubs

* **Model transport is a stub.** The reply text is `fixture-echo:…`, not a model.
* **Retry artifact.** A same-id retry returned `200` and left the single `user`
  input untouched, but the server emitted an **extra `idle` message** with no
  matching user message (the fixture's request log shows no second turn). Both
  clients' windowed reconciliation still resolves correctly; the anomaly is the
  server's, not the clients'.
* **Decisions route not exercised** live or natively here (budget). Step
  `00-runtime-spike.md` line 19 explicitly allows documenting a tested
  direct-OpenRouter fallback in place of the native route, so the native route's
  absence is not, by itself, an unmet mandatory criterion. Per-question decision
  failures are exercised **mock-only** and must stay visible as such.
* The fixture provider is only for this spike; it declares two models
  (`delayed-echo`, `critic-echo`) and ignores tool calls.

## Safety and cleanup

* Owns a unique `HARNESS_ROOT` under `/tmp/opencode`; refuses to reuse a non-empty
  root; injects exactly one fixture provider into that root's config.
* Uses the harness's own guards (ownership marker, `/proc` identity, flock) via
  `harness.sh start|stop|restart|cleanup`. Never signals a foreign PID and never
  touches `~/.local/state/opencode/service.json`.
* Cleans up with `harness.sh cleanup` and removes its own `.lock` file. With
  `--keep`, remove the printed root with `bash ../harness/harness.sh cleanup` under
  the same `HARNESS_ROOT`.
* Contains no secrets; the report records endpoints/ports only.
