# Runtime spike: disposable app-owned OpenCode V2 service harness

Step 00 (`docs/implementation/00-runtime-spike.md`) requires a *disposable
app-owned* OpenCode V2 service to run the TypeScript-vs-Python integration spike
without touching the ordinary shared coding service. This directory provides that
harness plus the observed service/contract/isolation findings.

Everything here is bash + `curl` + `python3` (present on this machine). No Bun,
npm, or provider credentials are required. The harness never triggers a model
call.

## Files

| Path | Purpose |
| --- | --- |
| `harness.sh` | Lifecycle CLI: `start`, `stop`, `restart`, `status`, `discover`, `info`, `contract`, `cleanup`, `selftest`. |
| `lib.sh` | Shared lifecycle/isolation functions (sourced by the CLI and tests). |
| `tests/smoke.sh` | Success / error / cleanup / isolation tests (28 assertions). |
| `contract/opencode-v2-session-contract.md` | Curated admission-ID / receipt / retrieval contract for the TS and Python prototypes. |

## Usage

```sh
cd spikes/runtime/harness
bash harness.sh start           # start the app-owned instance
bash harness.sh discover        # print endpoint descriptor + auth passwordFile
bash harness.sh contract        # fetch /openapi.json, print version summary
bash harness.sh restart         # replace process, keep data (durability)
bash harness.sh status
bash harness.sh stop
bash harness.sh cleanup         # stop + delete the whole scratch tree
bash harness.sh selftest        # run tests/smoke.sh
```

Overrides: `HARNESS_ROOT` (default `${TMPDIR:-/tmp}/opencode/spike-harness`) and
`HARNESS_OPENCODE_BIN` (explicit binary; default `command -v opencode`).

## Safety / isolation model

1. **Explicit binary.** Uses `$HARNESS_OPENCODE_BIN` or `command -v opencode`,
   resolves `opencode --version`, and runs `opencode serve --hostname 127.0.0.1
   --port <free port>` in the foreground. It never runs `opencode serve
   --service` and never calls `opencode service start|stop|restart`, so it can
   neither register nor restart the ordinary shared background service.
2. **Allowlisted child environment.** The child is spawned with `env -i` and only
   `PATH`, `HOME`, `XDG_*`, `TMPDIR`, and the per-run `OPENCODE_PASSWORD`. Every
   inherited `OPENCODE_*`, `OPENCHAMBER_*`, and `AI_AGENT` variable is dropped.
   This matters on this workstation: the parent session exports
   `OPENCODE_CONFIG` (a managed config path), `OPENCODE_PASSWORD` /
   `OPENCODE_SERVER_PASSWORD` (the *ordinary* service password),
   `OPENCODE_BINARY`, `OPENCODE_SESSION_ID`, and `OPENCHAMBER_*`. Without
   scrubbing, a child could read the parent's config/credentials and even target
   the ordinary service. Verified in `tests/smoke.sh` by reading
   `/proc/<pid>/environ`.
3. **Unique HOME/XDG tree.** `HOME` and all four `XDG_*_HOME` variables point into
   `$HARNESS_ROOT` (mode 700). Config, database, logs, and shell state all land
   there (`$HARNESS_ROOT/data/opencode/opencode.db`), never in
   `~/.config/opencode`, `~/.local/share/opencode`, or
   `~/.local/state/opencode`.
4. **cwd outside repository ancestors.** The server runs in
   `$HARNESS_ROOT/cwd` (e.g. `/tmp/opencode/spike-harness/cwd`). `harness.sh`
   refuses to start if `$HARNESS_ROOT` is inside the repository or is an ancestor
   of it, and if it overlaps the ordinary OpenCode locations.
5. **Loopback + auth.** Binds `127.0.0.1` only. `OPENCODE_PASSWORD` is a random
   per-run secret written to `$HARNESS_ROOT/run/password` (mode 600); HTTP Basic
   user is `opencode`. Unauthenticated and wrong-password requests get `401`.
6. **No standard registration.** A plain `opencode serve` writes *no*
   `~/.local/state/opencode/service.json`; the harness scratch `state/` stays
   empty. Discovery is via the harness-owned descriptor, not the shared-service
   registration. (Consequence: do **not** use `@opencode/client`'s
   `Service.ensure()` / `Service.discover()` against this harness — they read the
   standard registration and could start or target the ordinary service.)
7. **No paid calls.** No provider credentials are ever injected, no event
   subscription is opened, and no `/generate` endpoint is called. Tests only
   create a session, admit a prompt envelope, and read it back. Verified: the
   isolated log shows **zero** model/generate/assistant activity after admission.

## Findings

### API contract (v2.2.0)

Recorded from the live server (`GET /api/info`, `GET /openapi.json`):

* `opencode v2.2.0`; `/api/info.version = "2.2.0"`;
  `/openapi.json` `info.version = "0.0.1"` (`title: "opencode HttpApi"`), 156
  operations / 337 schemas.
* Auth: HTTP Basic `opencode:<OPENCODE_PASSWORD>`. If `OPENCODE_PASSWORD` is
  unset the server generates one and prints `server password <token>` on stdout.
* `POST /api/session` with `{location:{directory}}` → `{data: Session.Info}`
  (`Session.ID` = `^ses`).
* `POST /api/session/{sid}/prompt` with `{id, text, delivery?, resume?}` →
  `{data: Session.Inbox.User}` receipt (`Session.Message.ID` = `^msg`); `text`
  required; bad `id` / missing `text` → `400 InvalidRequestError`.
* **Admission is idempotent by client ID and is not completion.** Retrying the
  same `id` returned `200` again, yet `GET …/message` held exactly one entry.
  The `delivery` value defaults to `"steer"` and is echoed.
* Errors are a `_tag` union: `InvalidRequestError` (400), `UnauthorizedError`
  (401), `SessionNotFoundError` (404), `ConflictError` (409, unused in the
  retry case we tested).
* Full snippets: `contract/opencode-v2-session-contract.md`.

### Restart / durability

* Sessions and admitted messages **persist** across a full process restart when
  the same `XDG_DATA_HOME` is reused (SQLite at
  `$XDG_DATA_HOME/opencode/opencode.db`); verified by restarting the process on a
  new port and re-reading the session and its message.
* The harness restarts **only** this disposable process. In-flight *turn*
  continuation and interrupted/unknown reconciliation were **not** tested here
  because exercising a turn requires a model call, which was out of scope for the
  harness (that is prototype work under an authorized budget). Treat
  "which state persists / how interruption is detected" as still open, as the
  step document requires.

### Config / plugins inheritance

* With a fresh `XDG_CONFIG_HOME`, `GET /api/config` lists only the harness config
  directory — no ancestor or global documents. Setting `OPENCODE_CONFIG` (as the
  parent session does) *does* add that document to the loaded config, which is
  exactly why the harness scrubs it.
* No plugins are loaded (no plugin sources in the scratch config). Because
  `OPENCODE_CONFIG` and global config are excluded, inherited plugins/agents
  cannot affect the app-owned instance — and, conversely, the harness writes
  nothing that ordinary sessions read.

### Risk noted (no action taken)

An isolated server still exposes a built-in `opencode` provider with free models
(`big-pickle`, `deepseek-v4.1-flash-free`, …). `provider.opencode.disabled` in
config was **not** honoured at v2.2.0 (normalized away). This does not affect the
harness because it never starts a turn, but the prototypes must be equally
careful: a prompt that is actually *processed* could reach that provider. Guard
by never supplying credentials for paid providers and by gating every model call
behind explicit budget authorization.

## Verification

```
$ bash spikes/runtime/harness/tests/smoke.sh
== summary: 28 passed, 0 failed ==
```

Covers: auth success/401, loopback bind, environment scrubbing
(`/proc/<pid>/environ`), config-source containment, admission receipt + retry
idempotency, typed errors (400/404), restart durability, cleanup/port release,
and non-interference (ordinary endpoint URL and registration hash unchanged).
An unsafe `HARNESS_ROOT` (inside the repo / ancestor of it) is rejected with no
artefacts left behind.

## Limitations

* Bash + `curl` + `python3` only; the `@opencode/client` TypeScript path is not
  exercised here (that is task A-ts).
* Live-only events, in-flight turn continuation, and interrupted-turn
  reconciliation are out of scope for the harness.
* Scratch data lives under `/tmp/opencode/spike-harness`; always run
  `bash harness.sh cleanup`.
