# CLI contract — `review-article-agent` (Step 02, with bounded Step 03 and Step 04 additions)

> **Scope: Step 02, plus clearly bounded Step 03 and Step 04 additions.** This
> document is the **contract** for the command-line surface over the durable local
> project foundation. It describes exactly what the commands accept, what they
> print, and how they fail. It is **not** a scientific or approval contract and it
> claims **no** scientific validation.
>
> It covers: projects/registry, immutable artifact revisions, a **private local
> storage snapshot** (`backup`/`export`), **non-authorizing local storage-fixture
> review state** (`fixture`), the **worker lifecycle** commands
> `worker start|status|stop` over the already-integrated detached-worker process
> layer, the later Step 03 addition of **synthetic durable job control**
> (`job run|status|list|pause|resume|cancel|switch|drain|reconcile|attach`) over
> the integrated synthetic worker engine, and the bounded Step 04 addition of
> **app-owned exact named-gate human approvals** (`gate prepare|confirm|status|
> list`). The `fixture` commands record a local *fixture* only; they are **never**
> a real protocol/pilot/outline/final release approval and never authorize
> execution, export, or science. The `worker` commands manage the worker **process
> lifecycle** only. The `job` commands run and control **bounded synthetic** tasks
> only: they accept **no** real model, provider, credential, or budget setting,
> make no network call, and perform or imply no scientific approval. The `gate`
> commands preview or record **one exact named-gate human decision**; a recorded
> approval never authorizes global execution, manuscript export, or scientific
> validity, and no named gate — including `final_release` — waives scientific
> validation. The wider Step 04 execution surface — production paid provider
> sessions and hosted transfer — **remains blocked**, and the integrated restricted
> runtime (the bounded `submit_session_turn`/`reconcile_session_turn` library path,
> the Jev adapter, and durable result persistence) is **not exposed as a CLI** here.
>
> **This is not a real-execution product.** Every task is a bounded *synthetic
> numeric* task or a synthetic *waiting-human* gate. Durable job states, run/
> attach/job control, recovery, budgets, and project activation are all exercised
> locally against the synthetic engine; real agent integration follows in a later
> step. A `job run` reserve is a small, clearly **virtual** synthetic value — not
> real money — and nothing here is a scientific result.

The contract was decided in task T6a; the exact spellings below are the T6a
decision. The behavior boundaries are fixed by
[`docs/implementation/02-project-foundation.md`](implementation/02-project-foundation.md)
(AC1/AC2/AC3/AC4/AC7), [`docs/decisions/002-step01-contracts-and-policies.md`](decisions/002-step01-contracts-and-policies.md)
(ARCH-*), and [`docs/contracts/01-identities.md`](contracts/01-identities.md) /
[`docs/contracts/03-policy-decisions.md`](contracts/03-policy-decisions.md) (C01/C03).

## 1. Entry points

The same `main()` is reached three ways, all standard-library only:

| Form | Notes |
| --- | --- |
| `review-article-agent …` | Console script installed from packaging metadata. |
| `python -m review_article_agent …` | Module execution; no install required. |
| `python -m review_article_agent.cli …` | Explicit CLI-module execution. |

`--help` on any command prints usage and exits `0`. `--version` prints
`review-article-agent <version>` and exits `0`.

## 2. Global options

| Option | Meaning |
| --- | --- |
| `--data-dir PATH` | Application data directory. Highest precedence. |
| `--config PATH` | Optional versioned JSON config file. Middle precedence. |
| `--version` | Print the application version and exit. |
| `-h`, `--help` | Print help and exit. |

Data-directory precedence: **`--data-dir` > config file `data_dir` > XDG default**
(`$XDG_DATA_HOME/review_article_agent`, else `~/.local/share/review_article_agent`).
The data directory is created and kept **owner-private (`0700`)** by the registry
on first use.

## 3. Output and exit codes

**Output.** Every successful command prints exactly one JSON document to
`stdout`, with sorted keys and a fixed indentation, so it is stable and
machine-readable. The single exception is `artifact content` (see §6), which
streams raw bytes.

**Errors.** Every failure prints a JSON document to `stderr`:

```json
{"error": {"category": "<category>", "message": "<actionable summary>"}}
```

Messages are **curated, actionable summaries** built from the error *category*,
our own command/field names, and known structural context. They deliberately do
**not** echo a raw domain-exception string, an untrusted value, a supplied
identifier, an invalid choice, or a configuration field name — a caller may have
pasted a credential into any of those. They never contain a raw traceback, an
imported file's contents, SQL text, or a secret. JSON output (both results and
errors) is ASCII-escaped, so malformed Unicode cannot raise a raw encode
traceback. This is **not** a generic secret scanner: it only avoids reflecting
untrusted/error-path values, and `artifact content` intentionally reads content.

**Exit categories** (stable):

| Exit | Category | Meaning |
| --- | --- | --- |
| `0` | — | Success. |
| `2` | `usage` | Command-line usage or configuration-file problem. |
| `2` | `validation` | Invalid input data (bad identifier, malformed import, unknown id shape). |
| `2` | `not_found` | A referenced project id is not registered. |
| `3` | `conflict` | Divergent duplicate / immutable-state conflict, including a private-snapshot **restore** that would clone or overwrite an existing project identity. |
| `4` | `storage` | Store/registry/schema/path/filesystem/permission error, including a failed private-snapshot create/verify/restore. |
| `1` | `internal` | Unexpected error; no traceback is shown. |

Both `usage` and `validation` deliberately share exit `2` (matching `argparse`'s
convention); the JSON `category` distinguishes them. Snapshot failures use
curated `storage`/`conflict` messages and **never** echo a raw manifest, snapshot,
or exception detail.

## 4. Configuration file (optional)

A versioned JSON document. Exactly two fields are permitted:

```json
{"schema_version": 1, "data_dir": "/absolute/or/relative/path"}
```

* `schema_version` (required) must be the integer `1`. A **newer** version is
  refused with an actionable message — it is never read or downgraded.
* `data_dir` (optional) must be a non-empty string path.
* **Any other field is rejected**, including any credential/provider/model/budget
  field (`api_key`, `token`, `password`, `provider`, `model`, `budget`, …). The
  CLI never ingests a provider secret or execution setting from this file. A
  rejected field is reported **structurally**: neither the field name nor its
  value is echoed (the name itself could be a secret).
* The file must be **strict JSON**: duplicate object keys, non-finite numbers
  (`NaN`/`Infinity`/overflowing literals), and over-deep nesting are rejected
  cleanly (exit `2`, category `usage`), with no traceback and no payload.
* A missing, non-UTF-8, malformed-JSON, oversized, or unreadable file is refused
  cleanly (exit `2`, category `usage`).

## 5. Project commands

Projects are app-owned SQLite stores addressed by a **generated, path-independent**
project id (`prj_` + 32 hex). No command ever derives identity from a path or Git.
If `--project` is omitted and the command needs a project, the **active selected
project** is used.

| Command | Synopsis |
| --- | --- |
| `create ROOT [--name NAME]` | Create and register a project at `ROOT`. Idempotent for the same location/identity; a divergent request is a `conflict`. |
| `list` | List registered projects and the active project id. |
| `select ID` | Select the single active project. Selecting is **not** executing. |
| `status [--project ID]` | Global status (no `--project`) or one project's status. |
| `relocate ID ROOT [--move]` | Explicitly re-register `ID` at `ROOT`, keeping the identity. `--move` moves the app-owned store too (**same filesystem only**). |
| `register ROOT [--project ID] [--name NAME]` | Explicitly adopt an already-moved/restored app-owned store. Never implicit. |

**Verified access.** `status --project ID`, and every artifact command that uses a
project, open the project store through
`ProjectStore.open_at(app_dir_for(root), expected_project_id=ID)`. A registry
location whose store records a different identity is **refused** (exit `4`). An
unregistered orphan store is never adopted implicitly. Path traversal and
path-like ids are rejected before they reach the filesystem.

**Status summary.** Status output intentionally excludes source payloads,
provenance strings, and rights strings. It always reports `"running": false`
(selection is not execution) and `"scientific_validation": false` (no scientific
validation exists in Step 02).

## 6. Artifact commands

Artifacts are **logical identities**; each change is a **new immutable revision**
— never an in-place edit. Commands use the active project unless `--project ID`
is given.

| Command | Synopsis |
| --- | --- |
| `artifact create --artifact-id ID --kind KIND [--label L]` | Create a logical artifact. `KIND` ∈ `rubric framework outline report document study dataset experiment finding`. Idempotent for the same id+kind; a different kind is a `conflict`. |
| `artifact list` | List artifacts (safe metadata). |
| `artifact import …` / `artifact edit …` | Import a new immutable revision (see below). |
| `artifact get ID [--revision R]` (alias `show`) | Safe metadata for an artifact and its current (or named) revision. **No content.** |
| `artifact revisions ID` | List an artifact's revision metadata. |
| `artifact content ID --revision R [--output PATH]` | **Explicitly** output one revision's raw bytes (to stdout, or to `PATH`). |

`import`/`edit` options:

| Option | Meaning |
| --- | --- |
| `--artifact-id ID` | Target logical artifact (must already exist). |
| `--revision-id ID` | The **exact** revision id; the rights record must bind this value. |
| `--file PATH` | Content path, or `-` for stdin. A JSON envelope by default, or raw bytes with `--binary`. |
| `--binary` | Treat `--file` as raw bytes instead of a JSON envelope. |
| `--rights-file PATH` | A canonical `RightsRecord` v1 (C03 §5.1) that binds the exact `{project_id, artifact_id, revision_id}`. |
| `--parent CURRENT_ID` | The current head revision for an edit; absent for a first revision. |
| `--deps-file PATH` | Optional JSON list of dependency refs (each `{project_id, artifact_id, revision_id[, role]}`). |
| `--request-id ID` | Optional idempotency key; an identical replay is stable, a divergent replay is a `conflict`. |
| `--origin LOCATOR` | Optional origin locator (default `import:cli`). |
| `--project ID` | Target project (default: active). |

**Revision projections** include `"scientific_state": "proposal"` on every
artifact revision: all Step 02 revisions are unvalidated local proposals, and no
CLI command or `fixture` approval changes that.

### Content and untrusted imports

* The default content is a **generic versioned JSON envelope**
  `{"schema_version": 1, "document_type": "<id>", "payload": <object|array>}`.
  Unknown envelope fields are **rejected**.
* **Imported documents are untrusted data, never authority.** No field in an
  imported document (for example `approved`, `decision`, `actor`,
  `authorization`) can grant an approval or influence rights. The CLI rejects an
  envelope that carries extra top-level fields rather than treating them as
  meaningful.
* **No automatic rights grants.** A revision stores content only when a
  *separately supplied* `--rights-file` validates and its `storage` operation is
  `allowed` (C03 §5.2). The CLI never manufactures, infers, or upgrades a rights
  record.
* **Strict JSON everywhere.** Imported content, rights records, and dependency
  files are parsed with rejection of **duplicate object keys at every level**,
  non-finite numbers, and over-deep nesting. Any failure is a clean validation
  error (exit `2`) with no traceback and no echoed payload.

### Export output protection (`artifact content --output`)

`--output PATH` is a safety-sensitive write, so it is constrained:

* The file is created **exclusively** (`O_CREAT|O_EXCL|O_NOFOLLOW`, mode
  `0600`). An **existing file or symlink is never overwritten or followed** — the
  command fails with an actionable error and leaves the target byte-unchanged. No
  pre-existing user file is ever chmodded or deleted.
* Writing inside the **application data directory** or any registered project's
  **`.review_article_agent` app-owned directory** is refused, so the CLI can never
  overwrite its own `store.sqlite3` or another project's assets
  (immutability/isolation).
* Without `--output`, the revision's raw bytes are streamed to stdout. This is the
  one explicit content read; it is never part of a status or log line.

### Copyable synthetic rights-file setup

After `create`, read the project id from the output (or `list`), then write a
**synthetic** rights file. The example below is fully synthetic, is **not** an
authorization, and contains no licensed text. Replace `<PROJECT_ID>` and the
artifact/revision ids with your real values; every operation's `evidence.ref` and
`evidence.provenance` must be non-empty for `allowed`.

```json
{
  "rights_id": "RIGHTS-syn-1",
  "project_id": "<PROJECT_ID>",
  "record_revision": 1,
  "subject": {"kind": "source_revision", "project_id": "<PROJECT_ID>", "artifact_id": "art-syn", "revision_id": "rev-syn-1"},
  "operations": {
    "acquisition": "allowed",
    "storage": "allowed",
    "redistribution": "allowed",
    "hosted_transfer": "allowed"
  },
  "basis": {
    "acquisition": {"kind": "public_domain", "evidence": {"ref": "lic:pd-syn", "provenance": "publisher:pd-list"}},
    "storage": {"kind": "public_domain", "evidence": {"ref": "lic:pd-syn", "provenance": "publisher:pd-list"}},
    "redistribution": {"kind": "public_domain", "evidence": {"ref": "lic:pd-syn", "provenance": "publisher:pd-list"}},
    "hosted_transfer": {"kind": "public_domain", "evidence": {"ref": "lic:pd-syn", "provenance": "publisher:pd-list"}}
  },
  "authorization": null,
  "restrictions": [],
  "privacy": {"personal_data": false, "confidentiality": "public", "denial": false},
  "recorded_by": "user",
  "recorded_at": "2026-10-27T00:00:00Z",
  "derived_status": "valid"
}
```

A worked end-to-end sequence (all synthetic):

```console
$ review-article-agent --data-dir ~/.local/share/review_article_agent create ./project --name Demo
$ review-article-agent --data-dir ~/.local/share/review_article_agent select prj_…   # the id from `create`
$ review-article-agent --data-dir ~/.local/share/review_article_agent artifact create --artifact-id art-syn --kind document
$ review-article-agent --data-dir ~/.local/share/review_article_agent \
    artifact import --artifact-id art-syn --revision-id rev-syn-1 \
    --file ./content.json --rights-file ./rights.json
$ review-article-agent --data-dir ~/.local/share/review_article_agent artifact get art-syn
$ review-article-agent --data-dir ~/.local/share/review_article_agent artifact content art-syn --revision rev-syn-1 --output ./out.bin
```

## 7. Private storage snapshot — `backup` and `export`

`backup`/`export` operate over the private local storage snapshot API
([`storage.md`](storage.md)). **This is not a manuscript or redistribution
export** (no Markdown/BibTeX/RIS) and the archive **must never be published**.
The CLI never copies the live database file; it uses the storage layer's SQLite
online-backup API, so committed WAL content is captured consistently.

| Command | Synopsis |
| --- | --- |
| `backup create DEST [--project ID]` | Create a **new** private snapshot directory at `DEST`. Refused if `DEST` already exists, and refused inside the application data directory or any project's app-owned directory. |
| `backup verify DIR` | Read-only, fail-closed verification of a snapshot directory. Prints `{"backup": {ok, project_id, counts, …}}`. |
| `backup restore DIR ROOT` | Verify, then atomically restore `DIR` into a **new** project `ROOT`, preserving the snapshot's exact `project_id`. |
| `export DEST [--project ID]` | **Alias** of `backup create`: the same private local storage snapshot format. |

Snapshot output is owner-private: the directory `0700`, and `manifest.json` /
`snapshot.sqlite3` `0600`; the directory contains **only** those two members.
A snapshot command's JSON result is marked `"publishable": false` and
`"scientific_validation": false`.

Restore conflicts (exit `3`, `conflict`): restoring a snapshot whose project
identity is already registered at **another** root is refused (never a clone),
and restoring into a root registered to a different project is refused. To
restore a project a second time, use a **separate data directory** whose registry
does not already hold that identity. A failed snapshot create/verify/restore is
exit `4` (`storage`) with a curated message that never echoes a raw manifest,
snapshot, path, or exception detail.

## 8. Local fixture review state — `fixture`

`fixture` records **non-authorizing** local storage-fixture review state over
[`review_article_agent.storage.approvals`](../src/review_article_agent/storage/approvals.py).
It is a local *fixture*: it is **never** a real protocol/pilot/outline/final
release approval and **never** authorizes execution, export, or science. The CLI
accepts **no** in-band actor/authority flag, and no imported document can grant
one.

| Command | Synopsis |
| --- | --- |
| `fixture record-approval --artifact-id A --revision-id R --gate-id G --decision approved\|rejected --rationale TEXT [--project ID]` | Record a local fixture approval bound to the exact `{project_id, artifact_id, revision_id}`, with `gate_kind` fixed to `fixture_artifact_revision`. |
| `fixture list [--project ID]` | List local fixture approvals with their computed status. |
| `fixture status APPROVAL_ID [--project ID]` | Show one approval's computed status, snapshot verification, and the truthful `current_revision_fixture_approved` indicator. |

Every `fixture` output carries the non-authorizing constants
`scope="storage_fixture"`, `actor_authenticated=false`,
`authorizes_execution=false`, `authorizes_export=false`, and
`scientific_validation=false`. The `rationale` is stored but is **not** echoed in
`status`/`list` output.

`fixture status` reports `current_revision_fixture_approved`, a **fixture-only**
indicator that is `true` **only** when the decision is `approved`, the computed
status is still `active`, the snapshot verifies, and the exact approved revision
is still the artifact's current head. It is `false` for a rejected decision, a
stale/superseded/invalidated approval (for example an amended upstream dependency
or rights record), or a corrupt snapshot. The generic name deliberately stresses
that this is a local fixture, **not** a scientific approval.

**Status is recomputed, never carried forward.** A new revision of the approved
artifact, of any pinned live dependency, or of a pinned rights record makes the
approval `stale`; an independent approval for an unrelated artifact stays
`active`; a newer approval for the same gate instance `superseded` the older one.
An edited artifact's new revision is a `proposal`
(`scientific_state="proposal"`) and its prior content stays byte-immutable.

### Copyable synthetic example (fixture caveats)

A complete, synthetic, copyable sequence. It demonstrates the private snapshot
and the fixture lifecycle; **nothing here is a scientific result or a real
approval**, and the ``export`` archive must not be published. Replace
`<PROJECT_ID>` with the id printed by `create`.

```console
# ... create/select a project and import artifacts (see §6) ...
$ review-article-agent --data-dir "$DD" fixture record-approval \
    --artifact-id art-syn --revision-id rev-syn-1 --gate-id gate-local-1 \
    --decision approved --rationale "synthetic local fixture; not a scientific approval"
$ review-article-agent --data-dir "$DD" fixture list --project <PROJECT_ID>
$ review-article-agent --data-dir "$DD" fixture status apv_… --project <PROJECT_ID>
# Edit the artifact (new immutable revision, parent = current head); the fixture
# approval then reports "stale" and the new revision reports scientific_state=proposal.

# Private local storage snapshot (NOT a manuscript export; never publish):
$ review-article-agent --data-dir "$DD" backup create ./snap-1 --project <PROJECT_ID>
$ review-article-agent --data-dir "$DD" backup verify ./snap-1
$ review-article-agent --data-dir ./other-state backup restore ./snap-1 ./restored
$ review-article-agent --data-dir "$DD" export ./snap-1b --project <PROJECT_ID>   # same private format
```

## 9. Worker lifecycle — `worker` (Step 03 boundary)

`worker` manages the **process lifecycle** of the detached background worker over
the already-integrated launcher
[`review_article_agent.worker.process`](../src/review_article_agent/worker/process.py)
and the owner-only local control socket
([`review_article_agent.runtime.ipc`](../src/review_article_agent/runtime/ipc.py)).
It is **lifecycle control only**. Together with `job run` (which may ensure a
worker is running before it submits), it is the only surface that acts on the
worker *process*; no ordinary project command starts or stops a worker (see
below).

| Command | Synopsis |
| --- | --- |
| `worker start [--timeout SECONDS]` | Ensure a detached worker is running for the resolved data directory. Idempotent: if a verified worker already answers, no process is launched. Reports **readiness**, never job completion. |
| `worker status [--timeout SECONDS]` | Show the **verified** worker status for the data directory. |
| `worker stop [--timeout SECONDS]` | Stop the worker by **authenticated control request** (never a signal, never a PID file, never a deletion of an ordinary service). |

All three use the **global** data-directory resolution (`--data-dir` > config
`data_dir` > XDG default, §2) and the same optional config file (§4). No worker
command accepts a project, a model/provider/credential, or a budget setting.

`--timeout SECONDS` is optional and bounded: it is parsed from the raw string and
must be a finite number greater than `0` and at most `3600`. When omitted, the
launcher's own default is used. The value is **never echoed** — an invalid or
secret-looking token yields a static validation error (§3).

**`worker start` — readiness, not execution.** On success the result carries
`readiness="ready"`, `job_completion=false`, `scientific_validation=false`, and
the verified `pid`/`state`; `started` is `true` only when this call launched the
process (`false` for an idempotent re-start). Starting a worker runs no task and
proves no job completed.

**`worker status` — an explicit reason.** Output has `running` and `verified`
plus, for a non-running result, an explicit curated `reason` (`not_running`,
`foreign_endpoint`, or `unavailable`). `running=false` is **not** by itself proof
that all is well; the `reason` distinguishes a cleanly stopped worker from an
unavailable control plane or a foreign endpoint. `status` itself does not start,
stop, or otherwise act on a worker.

**`worker stop` — confirmed or not success.** The request is routed through the
owner-authenticated control socket and is credited only when the socket is then
**confirmed gone**:

| Outcome | Result |
| --- | --- |
| Stop confirmed (worker acknowledged and the endpoint is gone) | exit `0`; `stopped=true`, `already_stopped=false`. |
| Worker already confirmed **not running** | exit `0`; idempotent `stopped=true`, **`already_stopped=true`**. |
| Stop **unconfirmed** (delivered but the worker never confirmed shutdown) | exit `4` (`storage`), not success. |
| Control endpoint **unavailable** | exit `4` (`storage`), not success. |
| A **foreign** endpoint occupies the control path, or its acknowledgement is missing/malformed (not our verified worker) | exit `3` (`conflict`), not success; the shutdown is unconfirmed and nothing was signalled. |

An unconfirmed, unavailable, or foreign stop is a **stable non-success** with a
curated, actionable message; it is never reported as a successful stop, and no
signal is ever sent. `worker start` that never becomes ready is exit `4`
(`storage`). These typed errors use the existing exit taxonomy (§3):
`validation` `2`, `conflict` `3`, `storage` `4`.

**Selection is not execution.** `create`, `list`, `select`, and the top-level
`status` never start, stop, or probe the worker; selecting or creating a project
is not running it.

## 10. Synthetic job control — `job` (Step 03)

`job` runs and controls **bounded synthetic** durable jobs over the integrated
worker engine ([`review_article_agent.worker.engine`](../src/review_article_agent/worker/engine.py)),
reached through the owner-authenticated control transport
([`review_article_agent.runtime.ipc`](../src/review_article_agent/runtime/ipc.py)).
There is **no** real model, provider, network call, or scientific approval: the
only submittable task kinds are `synthetic_numeric` and
`synthetic_waiting_human`. No arbitrary result JSON, source document, path, or
credential is ever accepted.

| Command | Synopsis |
| --- | --- |
| `job run --request-id ID [--task-kind K] [--steps N] [--delay-ms MS] [--outcome O] [--stage S] [--reserve USD] [--project ID] [--timeout SECONDS]` | Ensure a worker is running and submit one synthetic job. Returns **admission**, never completion. |
| `job status JOB_ID [--project ID] [--timeout SECONDS]` | One job's durable status (read-only). |
| `job list [--project ID] [--timeout SECONDS]` | A project's jobs (read-only). |
| `job pause [JOB_ID] [--project ID]` | Stop scheduling new work (a durable state; preserves an unresolved outcome). |
| `job resume [JOB_ID] [--project ID]` | Re-acquire execution and resume scheduling. |
| `job cancel [JOB_ID] [--project ID]` | Stop scheduling; terminal for **scheduling** only. |
| `job switch --project ID [--timeout SECONDS]` | Safely make another project execution-active (drains the old one). |
| `job drain [--project ID] [--timeout SECONDS]` | Persist a durable pause and begin draining the execution-active project. |
| `job reconcile JOB_ID --resolution R [--result-value N] [--confirm-quiesced] [--project ID]` | Record an explicit **owner disposition**; no arbitrary result payload. |
| `job attach JOB_ID [--once\|--watch] [--poll-interval SECONDS] [--timeout SECONDS] [--project ID]` | Read-only status snapshots; `--watch` streams newline-delimited JSON. |

All commands use the **global** data-directory resolution (`--data-dir` > config
`data_dir` > XDG default, §2) and the same optional config file (§4). When
`--project` is omitted, the **active selected project** is used (§5). No `job`
command accepts a model/provider/credential/budget setting.

### `job run` — admission, not completion

`job run` **explicitly ensures a worker is running** (starting one only if
needed) and then sends one `submit` control request. It returns the durable job
acknowledgement with `submitted=true`, `job_completion=false`,
`virtual_reserve=true`, and `scientific_validation=false`. A returning `run`
means the job was **admitted**, never that it completed; completion is observed
later from a *new* CLI via `job status`/`job attach`. `--request-id` is a
required stable idempotency key: an identical replay is stable and a divergent
replay is a `conflict`.

Bounded parameters (all parsed as strings so an invalid or secret-looking token
is never echoed):

| Option | Meaning |
| --- | --- |
| `--task-kind K` | `synthetic_numeric` (default) or `synthetic_waiting_human`. |
| `--steps N` | Integer `1..1000` (default `1`). |
| `--delay-ms MS` | Integer `0..60000` (default `0`). |
| `--outcome O` | `complete` (default), `failure`, or `ambiguous`. |
| `--stage S` | Synthetic stage identifier (default `cli`). |
| `--reserve USD` | Small, exact, strictly positive **virtual** synthetic reserve (default `0.01`). It is **not real money** and no provider is contacted. |

### Authentication and detachment

Every `job` command first probes the worker
(`worker.process.worker_status`, verifying our application marker and the
kernel-reported PID) and sends its control request bound to that verified PID
(`expected_peer_pid`), so a replaced or foreign listener at the control path
cannot receive a payload. **Only `job run` ever starts a worker**; queries and
controls never start one implicitly. A missing/unverified worker is a stable
non-success (exit `4`); a foreign endpoint is a `conflict` (exit `3`).

`job attach` reads fresh job status snapshots and **never** cancels a job on CLI
exit — a normal exit, a Ctrl-C, or a closed downstream pipe detaches cleanly with
no traceback and sends no cancel. It is bounded by a poll interval
(`--poll-interval`, accepted range `0.05..60`, default `0.5`) and an overall
`--timeout` (default `60`), and stops at a terminal scheduling state. The overall
deadline is fixed **before** the first probe, every query's timeout is capped by
the remaining budget, and no query is issued after the deadline. By default it
prints one JSON snapshot (`--once`); `--watch` streams **one JSON object per
line** (newline-delimited JSON) — the one documented exception to the
single-JSON-document rule (§3).

To keep a read-only exit unambiguous, **each** `--watch` line carries
`"snapshot": true` and `"terminal": <bool>`, and the stream ends with an explicit
`{"watch_end": {"reason": "terminal"|"timeout", "terminal_reached": <bool>}}`
marker. A `timeout` marker reports `terminal_reached=false` and never claims a
terminal result; `job attach` never dispatches new work.

### Truthful outcomes (no blanket atomicity claim)

Because the control request may reach the worker and change durable state *before*
the CLI observes a reply, only a **pure local pre-RPC validation** (or a pre-send
peer refusal) states that nothing was sent. Every other failure is honest about
uncertainty:

* A **transport failure at or after the send boundary** (a timeout, a lost or
  truncated response, a closed connection, a socket error) is reported with an
  **unconfirmed outcome**: the request may already have been applied. Inspect the
  durable job with `job status`/`job list`, or retry with the **exact same
  `--request-id`** (never a new identity) so an idempotent replay cannot create a
  second job.
* A **conflict** (including a blocked `job switch`) is reported as not-success
  with a direction to inspect the state first — it makes **no** "nothing changed"
  claim, because a blocked switch persists a durable pause on the old project
  before it can find that arbitration is still blocked.
* A worker-reported **internal** failure is likewise reported as unconfirmed.

### `job reconcile` — explicit owner disposition only

`job reconcile` accepts only the engine's fixed owner dispositions, never an
arbitrary JSON result:

| `--resolution` | Meaning |
| --- | --- |
| `accepted_local_result` | Accept a fixed synthetic numeric result; requires `--result-value N` (`0..999999`) and `--confirm-quiesced`. |
| `definitive_failure` | Record a definitive local failure; requires `--confirm-quiesced`. |
| `human_escalation` | Move the job to `waiting_human` (a safe option). |
| `keep_unresolved` | Keep the job unresolved (`waiting_external`) (a safe option). |

A terminal disposition (`accepted_local_result`, `definitive_failure`) requires
the explicit **`--confirm-quiesced`** acknowledgement; without it the command
fails with a validation error and nothing is changed.

**Scientific approvals are not exposed.** There is no `approve`/`reject` command
and no in-band result, actor, or authority flag. `job resume` does not clear an
unknown/unresolved outcome, raise a budget cap, or bypass a waiting gate; it
reports the visible job state instead. `job cancel` stops scheduling but never
terminal-clears an ambiguous remote outcome.

## 11. App-owned human-gate approvals — `gate` (Step 04 addition)

`gate` records and reads back **one exact named-gate human approval** over the
integrated [`review_article_agent.runtime.gates`](../src/review_article_agent/runtime/gates.py)
and [`review_article_agent.storage.human_approvals`](../src/review_article_agent/storage/human_approvals.py)
layer. It is an app-owned, local metadata approval: it starts no worker, dispatches
no model/tool call, requires no runtime activation, and holds no registry execution
guard. A recorded approval satisfies **only** its one named gate instance
(`gate_kind` + `gate_id` + target artifact); it is **never** a global execution,
manuscript-export, scientific-correctness, or final-release-validation claim. No
named gate — including `final_release` — waives scientific validation.

| Command | Synopsis |
| --- | --- |
| `gate prepare --artifact-id A --revision-id R --gate-kind KIND --gate-id G --decision approved\|rejected --rationale TEXT [--project ID]` | **Read-only preview** of the exact gate snapshot over the target plus its full transitive dependency closure. Records **no** approval, mints no authority, and writes nothing. |
| `gate confirm … --snapshot-digest SHA256 [--timeout-seconds SECONDS]` | Re-prepare the exact snapshot, require the caller to echo the **exact** previewed `--snapshot-digest`, confirm out of band on the **controlling terminal**, then record **one** durable human approval. |
| `gate status --approval-id ID [--project ID]` | Read-only computed lifecycle status of one approval. |
| `gate list [--project ID]` | Read-only list of this project's approvals with their computed status. |

`--gate-kind` accepts only the approved C02 enum values (`protocol`, `pilot`,
`outline`, `framework`, `exclusion_set`, `artifact_revision`, `batch`,
`final_release`); a generic tool permission is never a gate kind. `--decision`
accepts only `approved`/`rejected`. An unrecognized value is rejected by
`argparse` and its raw value is not echoed. The optional `--project` defaults to
the active project; the store is opened through the registry with an expected
project id, so a path/identity mismatch is refused before any read.

**`prepare` is a preview, not an approval.** It prints a safe summary with the
exact project/target, gate kind/id, proposed decision, snapshot id/digest and
schema versions, the version set, the dependency count and each dependency pin's
identity/role/digest/byte size/rights key and digest. It never includes source or
rights bytes, a secret, a capability, or the caller's rationale text. Every
summary carries `approval_recorded`, `authorizes_execution=false`,
`authorizes_export=false`, and `scientific_validation=false`. There is
deliberately no raw actor/authority flag in this command group.

**`confirm` binds to the exact previewed pins and takes authority only from the
terminal.** It prepares the snapshot read-only and closes the store **before** any
terminal wait; it then refuses a malformed or mismatched `--snapshot-digest`
*first*, without opening the terminal. The expected digest is a *check*, never
authority. The confirmation opens `/dev/tty` (never standard input, so piped input
can never confirm), uses no `--yes`/actor/authenticated/capability flag, and accepts
no imported approval or in-band authority value; those flags are rejected as usage
errors. `--timeout-seconds` is optional and bounded by the owner-auth validators; an
out-of-range value is a `validation` failure before the terminal. A missing
controlling terminal, a cancelled/expired confirmation, or a failed owner check is
refused with a stable category. After confirmation, `confirm` opens a **fresh**
verified store and hands the exact prepared snapshot plus the minted capability to
`HumanApprovalRepository.record_decision`, which **revalidates inside one write
transaction** and consumes the capability once. If a live input or rights record
mutated, or the prepared snapshot became stale/superseded, the approval is denied
and **zero** rows are written; the caller must re-prepare and confirm again. A
failed write leaves no owner-auth evidence, approval, or entry row behind. The
successful result prints the safe gate summary with `approval_recorded=true` plus a
safe approval summary (identity, out-of-band `owner_auth_ref` pointer, computed
status); it still carries the three non-authorizing `false` constants.

**Status/list are computed history, not stored verdicts.** Each read recomputes the
lifecycle status (`active`/`stale`/`superseded`/`invalidated`) and returns retained
historical rows. `satisfies_named_gate` is the only satisfaction claim and is `true`
only for an `active` + `approved` exact named-gate instance. `gate status`/`gate
list` expose curated computed `reasons` and `superseded_by`, but **not** the stored
rationale text in their machine-readable JSON. The caller's `rationale` is bound
into the snapshot digest and stored, and it is displayed on the owner's
**controlling terminal** as part of the bound action the owner confirms; it is
never echoed in a command's machine-readable `stdout`/`stderr` JSON summary or
error. `gate list` fails the whole read closed as `storage` if a stored
row is corrupt rather than silently skipping it; `gate status` reports a corrupt
approval as `invalidated`. `gate status` for an unknown approval id is a
`validation` error.

**Error categories.** Failures use the stable CLI taxonomy in §3: `validation`
(exit 2) for a malformed/unknown gate identity or target, malformed digest, or
missing revision; `conflict` (exit 3) for a stale/superseded/cross-project pin, a
digest mismatch, a cancelled/expired/replayed/mismatched confirmation; `storage`
(exit 4) for a corrupt/missing pinned revision or rights record, an unsupported
schema, or an unavailable controlling terminal. Messages are static and curated;
they never echo a raw exception, store content, or a caller-supplied value (a caller
may have pasted a credential into an argument).

### Copyable synthetic named-gate example

All placeholders only; no real source upload, no live provider call, and no paid
test. `--gate-kind protocol` below is an approved enum value, but the decision is a
synthetic local approval for demonstration and authorizes nothing global.

```console
$ review-article-agent --data-dir "$DD" gate prepare \
    --artifact-id art-syn --revision-id rev-syn-1 \
    --gate-kind protocol --gate-id gate-proto-1 \
    --decision approved --rationale "synthetic named-gate preview; not a scientific approval" \
    --project <PROJECT_ID>
# Copy the printed "snapshot_digest" value into the next command:
$ review-article-agent --data-dir "$DD" gate confirm \
    --artifact-id art-syn --revision-id rev-syn-1 \
    --gate-kind protocol --gate-id gate-proto-1 \
    --decision approved --rationale "synthetic named-gate preview; not a scientific approval" \
    --snapshot-digest <SNAPSHOT_DIGEST> \
    --timeout-seconds 120 --project <PROJECT_ID>
# The owner types the displayed challenge on the controlling terminal (/dev/tty);
# piped stdin and a --yes/actor/imported-approval flag cannot confirm.
$ review-article-agent --data-dir "$DD" gate status --approval-id <APPROVAL_ID> --project <PROJECT_ID>
$ review-article-agent --data-dir "$DD" gate list --project <PROJECT_ID>
```

## 12. Explicit exclusions (Step 02, and the Step 03/Step 04 boundaries)

* No scientific `approve`/`reject` command exists here, and no arbitrary job
  `result` payload is accepted. `job reconcile` takes only the fixed owner
  dispositions in §10. `gate confirm` records a named-gate human approval only; it
  performs no scientific validation and authorizes no execution, export, or
  final-release waiver. The restricted runtime is integrated as a **library**, but no
  model-task CLI/executor exists; a model/scientific run surface follows in a later
  step.
* No `model`/provider/credential/budget command or setting exists here, and none
  is read from the config file (§4). `worker`, `job`, and `gate` accept no such
  setting.
* `fixture` records a local, non-authorizing fixture only. No real
  protocol/pilot/outline/final-release gate and no in-band actor/authority is
  accepted; an imported document still cannot self-grant any approval. The `gate`
  command group is the separate exact named-gate path and does not turn a fixture
  into a real approval.
* No scientific acquisition or evidence validation is performed or implied. All
  jobs, fixtures, and archives are synthetic/private and non-authorizing, and the
  worker lifecycle and job control are exercised only with owned disposable
  processes. A `gate` approval is a local metadata decision, not scientific
  correctness.
* `backup`/`export` is a **private local storage snapshot**, not a manuscript
  (Markdown/BibTeX/RIS) or redistribution export; never publish an archive.

## 13. Extension hooks

Adding a subcommand is intentionally small:

1. Add a parser in `build_parser()` (`review_article_agent/cli/main.py`).
2. Add a handler `cmd_<name>(args, ctx) -> dict | RawBytes | StreamNDJSON` that
   returns a JSON-ready object (or a newline-delimited stream for a `--watch`).
3. Register it in `_COMMANDS`, `_ARTIFACT_COMMANDS`, `_BACKUP_COMMANDS`,
   `_FIXTURE_COMMANDS`, `WORKER_COMMANDS` (in
   `review_article_agent/cli/worker.py`), `JOB_COMMANDS` (in
   `review_article_agent/cli/jobs.py`), or `GATE_COMMANDS` (in
   `review_article_agent/cli/gates.py`).

Handlers receive a `Context` holding the resolved `Registry`, the data directory,
the parsed config, and the environment, and never touch global state. New exit
categories are added to `classify_error()` and this table.

## 14. Verification

* `PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_*.py'` — includes
  the real-subprocess CLI acceptance tests (`tests/test_cli.py`,
  `tests/test_cli_snapshot.py`), the config tests (`tests/test_config.py`), the
  worker-lifecycle CLI tests (`tests/test_cli_worker.py`), the real
  detached worker-process tests (`tests/test_worker_process.py`), the
  real-subprocess job-control CLI tests (`tests/test_cli_jobs.py`), and the
  human-gate CLI tests (`tests/test_cli_human_gates.py`, including a real-PTY
  controlling-terminal case).
* `python3 scripts/check_contract_docs.py --self-test` — the Step 01 contract
  checks remain green (this document adds no fixtures or schema field semantics).
* The packaged console script and the absence of an invented license are checked
  by building a wheel offline in `tests/test_cli.py`.
