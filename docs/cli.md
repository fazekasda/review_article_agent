# CLI contract — `review-article-agent` (Step 02, with bounded Step 03, Step 04, and Step 05 additions)

> **Scope: Step 02, plus clearly bounded Step 03, Step 04, and Step 05 additions.** This
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
> the integrated synthetic worker engine, the bounded Step 04 addition of
> **app-owned exact named-gate human approvals** (`gate prepare|confirm|status|
> list`), and the bounded Step 05 addition of the **`discovery` command group**
> (typed discovery context/corpus artifacts, seed/plan proposal documents,
> bounded search/expand background jobs, and a scoped hosted
> lead-proposal route). The `fixture` commands record a local *fixture* only; they
> are **never** a real protocol/pilot/outline/final release approval and never
> authorize execution, export, or science. The `worker` commands manage the worker
> **process lifecycle** only. The `job` commands run and control **bounded
> synthetic** tasks only: they accept **no** real model, provider, credential, or
> budget setting, make no network call, and perform or imply no scientific
> approval. The `gate` commands preview or record **one exact named-gate human
> decision**; a recorded approval never authorizes global execution, manuscript
> export, or scientific validity, and no named gate — including `final_release` —
> waives scientific validation. The `discovery` commands (§12) are a **separate**
> owner-controlled surface: they store discovery proposals only, submit bounded
> **background jobs** (real public-metadata provider GETs, a bounded citation
> expansion, and at most one guarded direct-lead `POST` per durable job), and
> record **no** approval, no completeness, and no scientific validation. The wider
> Step 04 execution surface — production paid provider sessions and hosted
> transfer — **remains blocked**, and the integrated restricted runtime (the
> bounded `submit_session_turn`/`reconcile_session_turn` library path, the Jev
> adapter, and durable result persistence) is **not exposed as a CLI** here.
>
> **This is not a real-execution product.** Every `job` task is a bounded
> *synthetic numeric* task or a synthetic *waiting-human* gate. Durable job states,
> run/attach/job control, recovery, budgets, and project activation are all
> exercised locally against the synthetic engine. The Step 05 `discovery` group
> additionally admits owner-controlled discovery background jobs that do issue
> bounded real metadata requests and one guarded direct-lead call under explicit
> owner/gate/rights/credential/cost controls (§12); it is still not a scientific
> workflow. A `job run` reserve is a small, clearly **virtual** synthetic value —
> not real money — and nothing here is a scientific result.

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
on first use. A pre-existing data directory, app-owned project directory, or
database file that is **not** owner-private is refused (never silently used or
chmodded) with a specific message naming the path and the `chmod` remedy — see
§3.

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

Two `storage` (exit `4`) failures carry a **specific, actionable** message rather
than the generic storage summary, because the fix is unambiguous and needs the
offending location named:

* **Not owner-private.** An existing data directory, app-owned project
  directory, or database file that grants group/other access is refused (never
  silently used or chmodded). The message names the path, the mode it has, and
  the exact remedy, e.g. `the data directory '/state' must be private to its
  owner (mode 0700); it is currently 0755, … Run: chmod 700 /state`.
* **Forbidden destination.** Writing a snapshot or export inside the application
  data directory or a project's `.review_article_agent` directory is refused with
  a message that says so and points to a destination outside `--data-dir`.

Both messages contain only the path the user supplied (via `--data-dir`, a
project root, or `--output`/`DEST`) and a permission mode — never a secret,
identifier, payload, or traceback.

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
| `backup create DEST [--project ID]` | Create a **new** private snapshot directory at `DEST`. Refused if `DEST` already exists, and refused inside the application data directory or any project's app-owned directory (the error says so and points to a destination outside `--data-dir`; see §3). |
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
For the `job` group there is **no** real model, provider, network call, source
processing, or scientific approval: the only submittable task kinds are
`synthetic_numeric` and `synthetic_waiting_human`, and the same **virtual**
synthetic reserve is exercised locally. No arbitrary result JSON, source
document, path, or credential is ever accepted. (The separate Step 05
`discovery` group (§12) admits **real** discovery background jobs through the
same engine; it is not a `job` subcommand and does not widen this group, but the
root `job` lifecycle controls below apply to every discovery admission too.)

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

## 12. Discovery artifact preparation and background jobs — `discovery` (Step 05 addition)

The root `discovery` command group is the bounded Step 05 addition over the
already-integrated discovery value layers ([`discovery/context.py`](../src/review_article_agent/discovery/context.py),
[`discovery/candidates.py`](../src/review_article_agent/discovery/candidates.py),
[`discovery/lead_runner.py`](../src/review_article_agent/discovery/lead_runner.py),
[`worker/discovery.py`](../src/review_article_agent/worker/discovery.py)).
It is implemented by [`cli/discovery.py`](../src/review_article_agent/cli/discovery.py)
(artifact/readback surface) and the two addons
[`cli/discovery_jobs.py`](../src/review_article_agent/cli/discovery_jobs.py)
(background-job submission) and
[`cli/discovery_reports.py`](../src/review_article_agent/cli/discovery_reports.py)
(report/freeze readbacks and proposals). It stores **proposals only**, records
**no** approval, and makes **no** scientific-validation, completeness,
deduplication, coverage, or freeze claim. **Step 05 is complete as a scoped
discovery workflow** (final full combined check passed at head `f3d97fe`; see the
[Step 05 handoff](implementation/handoffs/05-discovery.md)); this group is not a
review writer. Exact field spellings, bounds, and library APIs are in
[`discovery.md`](discovery.md); this section documents the CLI surface only.

### 12.1 Commands (exact `--help` spellings)

| Command | Synopsis |
| --- | --- |
| `discovery context import --artifact-id ID --revision-id ID --file PATH --rights-file PATH [--request-id ID] [--parent-revision-id ID] [--project ID]` | Strictly validate one typed context envelope, then store one immutable **proposal** revision (`report` kind, or `rubric` for a `discovery_rubric`). Records no approval. |
| `discovery context show --artifact-id ID [--revision-id ID] [--content] [--historical] [--project ID]` | Read one stored context revision. Default is a **safe summary**; `--content` explicitly returns the private validated document JSON; `--historical` reads an exact superseded revision without claiming current authority. `validate` is an alias of `show`. |
| `discovery corpus ingest --run-id ID [--artifact-id ID] --revision-id ID --request-id ID --rights-file PATH [--project ID]` | Record one cumulative publication-candidate snapshot from saved observations. Metadata is local only, never scientific-accepted context. |
| `discovery corpus show [--artifact-id ID] [--revision-id ID] [--content] [--project ID]` | Read one snapshot; the default summary never echoes raw titles/tokens, and `--content` is the explicit private opt-in. |
| `discovery search run --artifact-id ID --revision-id ID --query-index N --request-id ID [--project ID] [--timeout SECONDS]` | Submit one exact approved-plan **query** background job. Returns queued **admission**, never completion. |
| `discovery search seed --source SOURCE --query TEXT --page-size N --max-pages N --request-id ID [--project ID] [--timeout SECONDS]` | Submit one provisional **seed** search job; the worker enforces the hard ceilings and the result stays **provisional**. |
| `discovery search import --artifact-id ID --revision-id ID --file PATH --source SOURCE --origin-locator LOCATOR --observed-at INSTANT --request-id ID [--project ID] [--timeout SECONDS]` | Submit one local **metadata-import** job from a strict record-array file (a user-chosen local file only; no arbitrary download), planned-ref bound. |
| `discovery expand run --artifact-id ID --revision-id ID --corpus-artifact-id ID --corpus-revision-id ID --roots-file PATH --request-id ID [--project ID] [--timeout SECONDS]` | Submit one bounded citation-**expansion** job from a plan + corpus ref plus a strict `[{"source","id"}]` roots file. The only accepted limits are the approved plan's; there is no CLI override. |
| `discovery expand show [--job-id ID \| --request-id ID] [--content] [--project ID]` | Read one completed expansion report. Default is a **safe summary** (counts, limits, stop reasons, report digest — never the graph); `--content` is the explicit private **full mapping**. |
| `discovery lead status --request-id ID [--project ID]` | Safe readback of one recorded hosted-lead request (issues no call). |
| `discovery lead content --request-id ID [--project ID]` | Explicit **PRIVATE, NON-AUTHORIZING** view of the stored lead-proposal text/metadata (issues no call). A diagnostic outcome is refused rather than shown as a successful proposal. |
| `discovery lead configure --artifact-id ID --revision-id ID --file PATH --rights-file PATH [--request-id ID] [--parent-revision-id ID] [--project ID]` | Validate and store one `discovery_hosted_use` batch as one immutable **proposal** revision. Records no approval and verifies no provider term. |
| `discovery lead run --batch-artifact-id ID --batch-revision-id ID --credential-env NAME --request-id ID [--project ID] [--timeout SECONDS]` | Submit one scoped hosted lead-proposal job with the **mandatory** `--credential-env OPENROUTER_API_KEY`. Returns queued **admission** only; the credential value is never accepted. |
| `discovery report create --corpus-artifact-id ID --corpus-revision-id ID --plan-artifact-id ID --plan-revision-id ID --revision-id ID --request-id ID --rights-file PATH [--artifact-id ID] [--expansion-job-id ID ...] [--known-relevant-file PATH] [--limitations-file PATH] [--project ID]` | Record one immutable **coverage-report** revision from exact corpus/plan refs. `--expansion-job-id` resolves only a real completed expansion report; the default summary claims no completeness. No worker, no network. |
| `discovery report show [--artifact-id ID] [--revision-id ID] [--content] [--project ID]` | Read one coverage report; a **safe summary** by default, `--content` the explicit private opt-in. |
| `discovery freeze propose --corpus-artifact-id ID --corpus-revision-id ID --coverage-artifact-id ID --coverage-revision-id ID --decision freeze\|search_again --entry-reviews-file PATH --rationale TEXT --request-id ID --revision-id ID --rights-file PATH [--artifact-id ID] [--accept-coverage-limitations EXPLANATION] [--project ID]` | Record (or replay) one **inert** freeze/search-again proposal from an explicit caller per-candidate review file and a literal coverage-limitation acknowledgement. Returns the exact named-gate `approval_request` + `gate prepare`/`gate confirm` guidance; records no approval. |
| `discovery freeze status [--artifact-id ID] [--project ID]` | Read the current decision and the last approved freeze roster as a **safe summary** (never auto-selects a new corpus). |
| `discovery freeze show [--artifact-id ID] [--revision-id ID] [--content] [--project ID]` | Read one freeze decision; a **safe summary** by default, `--content` the explicit private opt-in. |
| `discovery freeze history [--artifact-id ID] [--project ID]` | Immutable previous decisions, newest-first, as **safe summaries**. |
| `discovery freeze require [--artifact-id ID] [--content] [--require-active] [--project ID]` | Read the selection **only** when a currently-active authentic freeze authorizes it; **safe summary** by default (exact corpus ref, `included_count`, decision, active approval). **Only `--content`** opts in to the private roster; `--require-active` is a redundant active-check alias that keeps the safe summary. Both paths consult `require_frozen`. Fails cleanly when no active freeze exists. |

Every command uses the global data-directory resolution (§2) and the same
optional config file (§4); `--project` defaults to the active selected project
(§5), and each project is opened through the registry with an expected project id
before any read/write. Every input file is read with a hard byte cap and the
strict parser (§6, "Strict JSON everywhere").

### 12.2 Exact references, rights files, and private `--content`

* **Exact same-project refs.** Plan/corpus/batch references are exact
  `{project_id, artifact_id, revision_id}` tuples in the current project; a
  cross-project or malformed reference is refused before anything is written or
  sent. `search run`/`import`/`expand run` reuse the approved discovery-plan
  library to revalidate the exact plan (and, for `expand`, the current corpus and
  the observed roots) **before every provider request**.
* **Explicit rights files.** `context import`, `corpus ingest`, and `lead
  configure` all require a separately supplied `--rights-file`: a canonical
  `RightsRecord` v1 (C03 §5.1) whose `source_revision` subject binds the exact
  `{project_id, artifact_id, revision_id}` and whose `storage` operation is
  `allowed`. The CLI **never** manufactures, infers, or upgrades a rights record
  (§6, "No automatic rights grants").
* **Explicit model config, no automatic paid call.** `lead configure` validates a
  `discovery_hosted_use` payload that carries an explicit `config` (a strict
  `LeadConfig` mapping), exact `source_refs`, and the three evidence references
  `provider_terms`/`privacy_review`/`pricing_evidence` (each `{ref, verified_at}`).
  Storing it reserves nothing and dispatches nothing. Only an explicit
  `lead run` — after the exact `batch` named gate is recorded and every pinned
  source's rights are revalidated — may issue **one** guarded `POST`; nothing here
  makes an automatic or implicit paid call.
* **Private `--content`.** A `--content` flag (or `lead content`) is the one
  explicit read of private data, and it is always marked
  `private`/`non_authorizing` with `authorizes_execution=false`,
  `authorizes_export=false`, and `scientific_validation=false`. Provider metadata,
  corpus titles/tokens, stored lead text, and the expansion graph are **untrusted,
  non-authorizing data**: no field of an imported document (or a model reply) can
  grant an approval or influence rights.

### 12.3 Background jobs: admission, fences, and lifecycle

`discovery search|expand|lead run` are submitted through the owner-only control
transport as a `discovery_submit` request over the worker engine
([`worker.md`](worker.md) §12). This surface is `owner-only`: only the verified
owner transport may submit; there is no unauthenticated path.

* **Five modes, exact parameters.** The accepted modes are exactly `query`,
  `seed`, `metadata_import`, `expand`, and `lead`, each with a fixed parameter-key
  allowlist (no actor/authority field). The CLI validates shape and bounds locally
  **before** a worker is ensured; the worker re-validates the exact canonical
  parameters and persists them with the caller's `--request-id` as the logical
  idempotency key (an identical replay is stable; a divergent replay is a
  `conflict`).
* **Active-project global fence.** Submission acquires/verifies the single global
  registry execution activation for the target project (the same old-project
  drain/switch arbitration as §10); another project cannot be silently drained,
  and a recovery blocker forbids new queuing.
* **One scientific future per process, own store, no lock over the network.** All
  discovery dispatch runs on **one** serial executor (one future at a time); the
  provider work runs on its own thread with its **own** store connection, and no
  store/lock is held across network I/O.
* **Admission, not completion; CLI detach.** A returned `run` means the job was
  **admitted** (queued), never that it completed; completion is observed later
  from a **new** CLI via `job status`/`job attach` (§10), which carry
  `job_completion=false`. `job attach` detaches on a normal exit, Ctrl-C, or a
  closed pipe without cancelling the job.
* **Per-attempt gate/token/job-held checks; no fabricated sockets.** Before
  **every** real provider attempt the job rechecks the exact approved plan (and,
  for `expand`, the current corpus, the observed roots, the activation **token**,
  and the job's held/paused/cancelled/draining state). A stop before any attempt
  aborts cleanly; a stop *after* a real attempt is an explicit **interrupted
  partial page** with **no fabricated attempt and no fabricated socket** — the
  pages already observed are retained and the attempt count reflects only real
  requests.
* **Honest restart.** A possibly-dispatched discovery attempt is **never**
  blind-retried. After a restart the engine proves an existing local observation
  on the **same fence** (and accepts it), or, when nothing can be proven, marks
  the attempt `ambiguous` and escalates to `waiting_human` for an explicit owner
  reconciliation. An `unknown`/unresolved outcome keeps its reserve and runtime
  occupancy and is never re-sent.
* **Root `job` lifecycle controls apply.** `job pause|resume|cancel|switch|drain|
  reconcile|status|list|attach` (§10) are the only lifecycle controls and apply to
  every discovery admission unchanged. They control **scheduling/admission** — they
  are not a parallel approval path and grant no scientific authority. `cancel`
  stops scheduling but never terminal-clears an ambiguous outcome; `resume` does
  not clear an unknown outcome, raise a cap, or bypass a waiting gate.

### 12.4 Cost, credential, and the direct-lead route

* **Synthetic jobs stay virtual.** A `job run` reserve is a small, clearly
  **virtual** synthetic fault-test value; synthetic actual external spend is
  **0**, and no real model billing occurs for the `job` group.
* **Direct-lead money is the existing runtime dispatch ledger.** A discovery
  `lead` job reserves and settles a `direct_call` dispatch on the existing runtime
  dispatch ledger ([`runtime/dispatch.py`](../src/review_article_agent/runtime/dispatch.py))
  **before** its one `POST`, and its cost is read from that ledger — it is **not**
  double-counted against the synthetic reserve bucket. Caps
  (project USD 25 / stage USD 10 / at most 2 concurrent) count every route; an
  `unknown` reserve is **retained** and continues to occupy the shared caps and
  runtime occupancy until reconciled.
* **Explicit worker environment variable.** The one permitted credential name is
  **`OPENROUTER_API_KEY`**. The credential **value** is never accepted on the
  command line, never sent over IPC, and never persisted or logged in the DB; the
  worker reads the named variable from its own environment at dispatch. `lead run`
  may ensure a worker that inherits the CLI's environment, and it **warns** when
  the key is absent; it **never** restarts or reconfigures an already-running
  worker to inject a credential. Starting an existing worker without the key does
  **not** auto-restart it.
* **One guarded `POST` per durable job.** The direct-lead route issues **at most
  one** `POST` per durable job, only after re-deriving the current, canonical
  hosted-use **batch** revision, revalidating the **exact named `batch` gate**, and
  revalidating every pinned source's `storage`/hosted rights plus the
  `provider_terms`/`privacy_review`/`pricing_evidence` shape. The route
  machine-validates only shape/presence/boundedness/freshness; the **truth** of
  those references and the legal/provider-term/pricing judgments remain
  human-reviewed. The existing app-owned OpenCode **production block is
  unchanged**; the direct-lead route is a narrow, separate exception and does not
  weaken it.
* **Sources are untrusted.** All provider metadata, corpus records, imported
  metadata, and lead output are untrusted, non-authorizing observations; they never
  become accepted context, eligibility, scientific identity, or a frozen corpus.

### 12.5 Named-gate guidance (recorded out of band)

The discovery proposals name the human gate they need, but they **record no
approval**: exactly as in §11, only `gate prepare`/`gate confirm` (on the
controlling terminal) records a real named-gate human approval. The gate names a
discovery command surfaces or requires are:

| Discovery object | `gate_kind` | `gate_id` |
| --- | --- | --- |
| individual seed revision | `artifact_revision` | `discovery.seed.<artifact_id>` |
| search plan revision | `protocol` | `discovery.plan` |
| hosted-use batch | `batch` | `discovery.hosted_use.<purpose>` |
| corpus freeze | `artifact_revision` | `discovery.freeze` |

### 12.6 Report / freeze CLI (integrated)

The **reporting** and **freeze** command groups are **integrated** (CLI1B,
`e15b5b4`): `discovery report create|show` and `discovery freeze
propose|status|show|history|require` (exact spellings in §12.1). They surface the
already-validated [`discovery/reporting.py`](../src/review_article_agent/discovery/reporting.py)
and [`discovery/freeze.py`](../src/review_article_agent/discovery/freeze.py)
value layers and introduce **no** new authority: they record only non-authorizing
proposals, ensure no worker, make no network/provider/model call, and record no
approval. Each JSON input file uses the same hard byte cap and strict parser as the
rest of the group, and every project is opened through the registry with an
expected project id.

* **Entry reviews are caller-supplied.** `freeze propose --entry-reviews-file` is
  a strict JSON list matching the freeze entry-review schema (one review per
  pinned candidate: `candidate_id`, `checked_at`, `provenance`, the four
  distinguished integrity statuses, a `disposition`, and a non-empty `rationale`);
  an `include` requires a resolved candidate whose statuses are all **known**
  (`unknown` is not clean), and unresolved/conflict candidates must be
  `exclude`/`defer`/`no_scientific_use`.
* **Coverage gaps need a literal acknowledgement.** A `freeze` over an explicit
  coverage gap is refused unless `--accept-coverage-limitations EXPLANATION`
  carries a literal explanation string — **never** a `--yes`/boolean or in-band
  actor/authority flag.
* **Exact named gate, out of band.** `freeze propose` returns the
  `artifact_revision`/`discovery.freeze` `approval_request` and `gate
  prepare`/`gate confirm` guidance; only the controlling-terminal `gate` path
  records the approval that lets `freeze require` return a selection. A refused
  append to an old frozen corpus artifact returns static guidance to choose a
  **new** corpus + coverage id.

No completeness, coverage, freeze, scientific-validation, study-identity,
eligibility, extraction, review, or writer claim is made or implied anywhere in
this group, and later workflow steps remain unimplemented.

## 13. Explicit exclusions (Step 02, and the Step 03/Step 04/Step 05 boundaries)

* No scientific `approve`/`reject` command exists here, and no arbitrary job
  `result` payload is accepted. `job reconcile` takes only the fixed owner
  dispositions in §10. `gate confirm` records a named-gate human approval only; it
  performs no scientific validation and authorizes no execution, export, or
  final-release waiver. The restricted runtime is integrated as a **library**. The
  Step 05 `discovery` group (§12) submits bounded discovery **background jobs** and
  a narrow, guarded direct-lead route, but it still exposes **no** general
  model-task executor and records no approval; a broader model/scientific run
  surface follows in a later step.
* No `model`/provider/credential/budget **command or setting** exists here, and
  none is read from the config file (§4). `worker`, `job`, and `gate` accept no
  such setting. The one credential input anywhere is `discovery lead run
  --credential-env`, which names the single approved environment variable
  (`OPENROUTER_API_KEY`) and accepts no credential **value** (§12.4).
* `fixture` records a local, non-authorizing fixture only. No real
  protocol/pilot/outline/final-release gate and no in-band actor/authority is
  accepted; an imported document still cannot self-grant any approval. The `gate`
  command group is the separate exact named-gate path and does not turn a fixture
  into a real approval.
* No scientific acquisition or evidence validation is performed or implied. The
  `job`/`fixture`/backup surfaces stay synthetic/private and non-authorizing, and
  the worker lifecycle and `job` control are exercised only with owned disposable
  processes. The Step 05 `discovery` group does issue **bounded real** public
  metadata requests and at most one guarded direct-lead `POST` per durable job,
  but it records only untrusted observations/proposals and makes no scientific,
  coverage, completeness, or freeze claim (§12). A `gate` approval is a local
  metadata decision — for a discovery gate too — not scientific correctness.
* `backup`/`export` is a **private local storage snapshot**, not a manuscript
  (Markdown/BibTeX/RIS) or redistribution export; never publish an archive.

## 14. Extension hooks

Adding a subcommand is intentionally small:

1. Add a parser in `build_parser()` (`review_article_agent/cli/main.py`).
2. Add a handler `cmd_<name>(args, ctx) -> dict | RawBytes | StreamNDJSON` that
   returns a JSON-ready object (or a newline-delimited stream for a `--watch`).
3. Register it in `_COMMANDS`, `_ARTIFACT_COMMANDS`, `_BACKUP_COMMANDS`,
   `_FIXTURE_COMMANDS`, `WORKER_COMMANDS` (in
   `review_article_agent/cli/worker.py`), `JOB_COMMANDS` (in
   `review_article_agent/cli/jobs.py`), `GATE_COMMANDS` (in
   `review_article_agent/cli/gates.py`), or the discovery group's own map
   `DISCOVERY_COMMANDS` / addon `DISCOVERY_JOB_COMMANDS` (in
   `review_article_agent/cli/discovery.py` / `cli/discovery_jobs.py`) / addon
   `DISCOVERY_REPORT_COMMANDS` (in `review_article_agent/cli/discovery_reports.py`;
   the discovery group is extended through the `build_discovery_parser` /
   `dispatch_discovery` addon hooks).

Handlers receive a `Context` holding the resolved `Registry`, the data directory,
the parsed config, and the environment, and never touch global state. New exit
categories are added to `classify_error()` and this table.

## 15. Verification

* `PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_*.py'` — includes
  the real-subprocess CLI acceptance tests (`tests/test_cli.py`,
  `tests/test_cli_snapshot.py`), the config tests (`tests/test_config.py`), the
  worker-lifecycle CLI tests (`tests/test_cli_worker.py`), the real
  detached worker-process tests (`tests/test_worker_process.py`), the
  real-subprocess job-control CLI tests (`tests/test_cli_jobs.py`), the
  human-gate CLI tests (`tests/test_cli_human_gates.py`, including a real-PTY
  controlling-terminal case), and the discovery CLI tests
  (`tests/test_cli_discovery_artifacts.py`, `tests/test_cli_discovery_jobs.py`).
* `python3 scripts/check_contract_docs.py --self-test` — the Step 01 contract
  checks remain green (this document adds no fixtures or schema field semantics).
* The packaged console script and the absence of an invented license are checked
  by building a wheel offline in `tests/test_cli.py`.

## 16. Acquisition — `acquisition` (Step 06 addition, MVP)

The root `acquisition` group is the integrated **local** Step 06 surface over
[`acquisition/imports.py`](../src/review_article_agent/acquisition/imports.py),
[`acquisition/manifest.py`](../src/review_article_agent/acquisition/manifest.py),
[`acquisition/validation.py`](../src/review_article_agent/acquisition/validation.py),
[`acquisition/pdf_inspection.py`](../src/review_article_agent/acquisition/pdf_inspection.py),
and [`acquisition/impact.py`](../src/review_article_agent/acquisition/impact.py)
(implemented in [`cli/acquisition.py`](../src/review_article_agent/cli/acquisition.py)
and [`cli/acquisition_manifest.py`](../src/review_article_agent/cli/acquisition_manifest.py)).
Most commands are local readbacks or local writes; `acquisition retrieve run`
submits one detached **official** retrieval job (queued admission only) and
`acquisition retrieve show` performs a verified readback that issues no request.
Step 06 is an **implemented MVP**; semantics, rights binding, P3 handling, and
bounds are in [`acquisition.md`](acquisition.md) and the
[handoff](implementation/handoffs/06-acquisition.md).

| Command (exact `--help` spellings) | Purpose |
| --- | --- |
| `acquisition import FILE --artifact-id ID --request-id ID --rights-file PATH --occurrence-rights-file PATH --occurrence-revision-id ID --origin-locator TEXT --revision-id ID [--parent-revision-id ID] [--dependencies-file PATH] [--project ID]` | Store one local regular file as an authorized source revision plus an occurrence record. A proposal only. |
| `acquisition missing --artifact-id ID --request-id ID --rights-file PATH --occurrence-rights-file PATH --occurrence-revision-id ID --origin-locator TEXT --reason TEXT [--project ID]` | Metadata-only "no full text acquired" event; stores no bytes and no source revision. |
| `acquisition occurrence id --request-id ID [--project ID]` | Print the deterministic occurrence artifact id needed for the occurrence rights subject. |
| `acquisition occurrence show [--request-id ID] [--artifact-id ID] [--revision-id ID] [--content] [--project ID]` | Safe summary; `--content` is the private opt-in (origin locator, reason). |
| `acquisition inspect FILE --identifiers-file PATH [--declared-media-type TYPE] [--pdf-inspect] [--content]` | Validate one local file against expected identifiers; stores nothing. `--pdf-inspect` runs the optional bounded `pypdf` inspector; `--content` reveals identifiers and a text preview. |
| `acquisition impact --artifact-id ID --revision-id ID [--project ID]` | Read-only dependency-impact report for a source revision. |
| `acquisition manifest propose --freeze-artifact-id ID --freeze-revision-id ID --entries-file PATH --rights-file PATH --revision-id ID --request-id ID [--artifact-id ID] [--project ID]` | Record one inert manifest proposal over the exact frozen roster. Records no approval. |
| `acquisition manifest show [--artifact-id ID] [--revision-id ID] [--content] [--project ID]` | Safe summary; `--content` returns the private validated document. |
| `acquisition manifest status [--artifact-id ID] [--project ID]` | Current manifest, gate state, and whether acquisition holds. |
| `acquisition manifest history [--artifact-id ID] [--project ID]` | Every manifest revision, newest first (safe summaries). |
| `acquisition manifest require [--artifact-id ID] [--revision-id ID] [--content] [--project ID]` | Succeeds only while the exact named gate `artifact_revision`/`acquisition.complete` currently makes acquisition complete. An acquisition prerequisite only. |
| `acquisition queue --freeze-artifact-id ID --freeze-revision-id ID [--artifact-id ID] [--project ID]` | Read-only blocker queue for the exact frozen roster; works before any manifest exists. |
| `acquisition folder import ROOT --entries-file PATH [--project ID] [--max-files N] [--max-total-bytes N]` | Atomic explicit folder-batch import of exactly the files named in `--entries-file`; all-or-nothing, a proposal only. |
| `acquisition retrieve id --request-id ID [--project ID]` | Print the deterministic report and occurrence artifact ids for a request id (read-only; prepares the exact rights). |
| `acquisition retrieve run --request-id ID --request-file PATH [--project ID] [--timeout SECONDS]` | Submit one exact acquire job from strict JSON; returns the queued admission only, never completion. |
| `acquisition retrieve show --request-id ID [--project ID] [--content]` | Verified stored-report readback (safe summary; `--content` is the private opt-in); issues no request. |

Notes:

* All `RightsRecord` files are explicit caller input bound to their exact subject
  (see [`acquisition.md`](acquisition.md) §3.1); nothing is defaulted or
  fabricated. Side-input JSON files are read with a 1 MiB cap and strict parsing.
* The manifest gate is recorded out of band with `gate prepare`/`gate confirm`
  (§11) for the exact manifest artifact/revision, `--gate-kind artifact_revision`
  and `--gate-id acquisition.complete`. It authorizes acquisition completion only,
  never scientific work, screening, seeding, pilots, or hosted use. A large roster
  can exceed the 10 000-entry gate-snapshot bound and block `gate prepare`.
* PDFs are always manual review in the manifest; the inspector is a diagnostic
  that is not wired into the gate.
* Errors use the exit categories of §3 with static, value-free messages.
* No test or app call in this section was run against a live source or paid
  service.

## 17. Section blueprints and drafting — `drafting` (Step 11, complete as a locally verified MVP)

The local, private-by-default `drafting` group binds a per-section planner/writer
workspace to existing app-owned runtime mappings over an exact approved outline and
assembles one section's reconstructible drafting package:
`propose|show --kind {workspace,blueprint,draft,assembly}`,
`status --kind {blueprint,draft,assembly}`, `accepted` (blueprint gate only), and
`package`; an assembly is simply `propose --kind assembly`. Private by default with
`--content` opt-in; the assembly Markdown keeps the approved paragraph structure. No
default model is selected and no live call is made. Status and disclosures:
[Step 11 handoff](implementation/handoffs/11-drafting.md).

## 18. Extraction runs — `extraction run` (Step 09 addition)

The `extraction` group's local `propose`/`show`/`accepted` commands (write/read inert
`config`/`pilot`/`batch`/`audit` revisions; no dispatch, no approval) are unchanged. This
section adds the detached **run** sub-group over the Step 09 direct-extraction worker
surface (`discovery_submit` `mode=extraction_run`; see
[`worker.md`](worker.md) §14). The existing `job status|list|attach|pause|resume|cancel|
drain|switch|reconcile` lifecycle commands apply to a queued run unchanged.

### 18.1 `extraction run submit`

`extraction run submit` assembles one exact `extraction_run` submission and returns a
**queued admission only** — never a completion and never a model output:

```
extraction run submit \
  --config-artifact-id ID --config-revision-id ID \
  --report-refs-file PATH \
  --hosted-artifact-id ID --hosted-revision-id ID \
  --purpose {extraction,quality} --mode {pilot,bulk} \
  [--extraction-artifact-id ID --extraction-revision-id ID] \
  [--pilot-artifact-id ID --pilot-revision-id ID] \
  --receipt-artifact-id ID --receipt-revision-id ID \
  --rights-file PATH \
  --credential-env OPENROUTER_API_KEY \
  --request-id ID [--project ID] [--timeout SECONDS]
```

* `--report-refs-file` is a strict JSON list of `{"artifact_id","revision_id"}` report
  references (the target project is filled in); `--rights-file` is a strict JSON object
  holding one `RightsRecord` for the **exact receipt revision**.
* `--extraction-ref` is required for `purpose=quality` and forbidden for
  `purpose=extraction`; `--pilot-ref` is required for `mode=bulk` and forbidden for
  `mode=pilot`.
* Every argument is validated locally, and the assembled parameters are canonicalised by
  the **real** extraction submission validator, **before** a worker is ensured — so a bad
  value is a typed `validation` error and no daemon is started and no job is created.
* `--credential-env` must name the one approved variable (`OPENROUTER_API_KEY`); the
  credential **value** is never accepted, sent over IPC, persisted, or logged. The worker
  reads the named variable from its own environment at dispatch, and the CLI warns
  (`configuration_required`/`notice`) when the key is absent — it never restarts or
  reconfigures a user's worker.
* The output carries `queued=true`, `admission_only=true`, `job_completion=false`,
  `key_value_accepted=false`, and `scientific_validation=false`.

### 18.2 `extraction run status`

`extraction run status --request-id ID [--project ID]` reads one recorded run **locally**
from the verified project store (no worker, no POST, no approval). The safe run projection
is returned under `run` (request/status/outcome/dispatch/task/cost/model/usage and the run
**receipt** reference `run_ref`); the private model output is never included. When the run
has not completed, `job_state` reports the durable scheduling state. The output carries
`authorizes_extraction=false`, `authorizes_synthesis=false`, and
`scientific_validation=false`. An unknown request id is a `validation` error.

Errors use the exit categories of §3 with static, value-free messages. Status and
disclosures: [`worker.md`](worker.md) §14 and
[decision 004](decisions/004-step09-direct-extraction.md).

## 19. Pipeline stage status — `pipeline` (kickoff 16 §6, read-only half)

The root `pipeline` group is a **read-only** local surface over
[`pipeline/status.py`](../src/review_article_agent/pipeline/status.py). It reports,
for one project, the project's **current pipeline stage** derived from durable state
and the exact next action a human must take. It is the read-only half of the
`pipeline run` orchestrator; it holds no authoritative state of its own, reuses the
stages' existing repositories/`require_*` checks, and writes nothing.

| Command | Synopsis |
| --- | --- |
| `pipeline status [--project ID]` | Report the current stage, the completed stages, what the project is blocked on, and the exact next action / CLI command. |

`pipeline status` walks the ordered stage table (discovery plan → coverage → freeze
→ acquisition → parsing → screening pilot/exclusions → extraction config/pilot/
batch/outputs → synthesis → framework → outline → drafting blueprint → review →
tracing → delivery) and stops at the first stage that is not complete. A stage is
**complete only when its own existing `require_*`/read check passes** (for example
`discovery.freeze.require_frozen`, `acquisition.manifest.require_acquired`,
`synthesis.workflow.require_outline`, `drafting.workflow.require_blueprint`), so a
reported "complete" is exactly the authority the stage itself grants. A stage with
no proposal revision is treated as *skipped* (not the current stage) when a later
stage has already begun.

The JSON output is:

```json
{
  "stage": "<stage id> | \"complete\"",
  "completed": ["<stage id>", "..."],
  "blocked_on": null | {"kind": "human_gate|missing_input|hosted_use|budget|failure|paused",
                        "detail": "...", "next_command": "<exact CLI line or null>"},
  "next_action": null | {"kind": "job|human", "role"/"job": "...", "inputs": {...}},
  "warnings": ["..."],
  "project_id": "prj_...",
  "authorizes_execution": false,
  "authorizes_export": false,
  "scientific_validation": false
}
```

* **Human gate.** When a proposal revision exists but its exact named human gate is
  not currently active and approved, `blocked_on.kind` is `"human_gate"` and
  `next_command` is the exact `gate confirm` line
  (`--artifact-id`/`--revision-id`/`--gate-kind`/`--gate-id`/`--decision approved`
  plus the required `--rationale`/`--snapshot-digest` placeholders). The `detail`
  also names the read-only `gate prepare` command needed to obtain the snapshot
  digest. A stale approval (a new revision after the approval) is reported as
  awaiting the gate again.
* **Not started.** When no proposal revision exists and no later stage has begun,
  `blocked_on` is `null` and `next_action` describes how to produce the proposal,
  naming the exact CLI command where one exists.
* **CLI command.** The review, tracing, and delivery stages now report their
  deterministic CLI command (kickoff 16 §9) in `next_action.inputs.command`
  (`review status`, `tracing run`, `delivery start`) alongside the library entry
  point (`SectionReviewRepository.run`, `OriginTraceRepository.run`,
  `DeliveryRepository.start_global_review`). A model-backed stage action (the
  section review itself) is submitted as a worker job through
  `generation run submit`; the read/record command is the deterministic next
  action reported here.

The command never dispatches a model, session, worker job, or network request,
records no approval, and writes nothing; re-running it over an unchanged store
returns equal output (idempotent). The project is opened through the registry with
an expected project id, and failures use the exit categories of §3 with static,
value-free messages.

## 20. Section review — `review` (kickoff 16 §9)

The root `review` group is a **read-only, local** surface over
[`review/workflow.py`](../src/review_article_agent/review/workflow.py). It reads a
durable Step 12 section-review state; it never dispatches a model, records an
approval, or exports.

| Command | Synopsis |
| --- | --- |
| `review show --review-id ID [--project ID] [--content]` | Read one section review's safe state summary. |
| `review status --review-id ID [--project ID] [--content]` | Read the review's status (rounds used, status, required human action). |

The safe summary carries `status`, `phase`, `rounds_used` (`critique`/`fact_check`),
`critic_calls`/`repair_calls`, `finding_count`/`check_count`, the
`required_human_action`, the read-only validation status, and the
`different_model`/`independent_human_verification` engine flags. The private raw
state (draft text, findings, checks) is included only with `--content`
(`private_content_included`). Every output carries `authorizes_execution=false`,
`authorizes_export=false`, and `scientific_validation=false`. An unknown review id
is a `validation` error whose message never echoes the id.

The model-backed review (critic/repair) is submitted as a worker job through
`generation run submit`; `review show`/`review status` are the deterministic
read steps.

## 21. Citation-origin tracing — `tracing` (kickoff 16 §9)

The root `tracing` group is a **deterministic, local** surface over
[`tracing/workflow.py`](../src/review_article_agent/tracing/workflow.py). It
derives the claim/source graph from the **real** draft, its approved drafting
package, and the recorded section-review checks, and resolves the acquisition port
over the project's already acquired sources; it makes **no network call**.

| Command | Synopsis |
| --- | --- |
| `tracing run --trace-id ID --draft-artifact-id ID --draft-revision-id ID [--review-id ID] [--policy-file PATH] --rights-file PATH [--project ID]` | Derive and record one origin trace (no network). |
| `tracing show --trace-id ID [--project ID] [--content]` | Read one trace's safe state summary. |
| `tracing coverage --trace-id ID [--project ID] [--content]` | Read the read-only `origin_coverage` audit. |

* `tracing run` reads the exact draft result and its approved drafting package via
  the drafting workflow, builds `claims`/`sources`/`source_digests` with
  `tracing.inputs.claims_and_sources`, resolves the injected acquisition port with
  `tracing.acquisition_port.make_acquire_port`, and records a resumable trace.
  `--policy-file` supplies the four required per-project limits; **without it the
  trace is `configuration_required` and nothing is acquired**.
* `--review-id` supplies the recorded section review whose checks annotate the
  trace; without it the checks list is empty.
* `--rights-file` is a strict JSON `RightsRecord` **template**; the CLI rebinds its
  subject/`rights_id` to each exact revision the workflow records (the workflow
  assigns its own opaque revision ids).
* `tracing coverage` reports `counts`, `requires_human_approval`, `blockers`,
  `complete`, and `export_ready`; `edits_applied` is always `false`.
* Every output carries the non-authorizing flags; the private raw trace state is
  included only with `--content`.

## 22. Delivery — `delivery` (kickoff 16 §9)

The root `delivery` group is a **local, human-gated** surface over
[`delivery/workflow.py`](../src/review_article_agent/delivery/workflow.py). It
covers the deterministic/human steps and the manuscript/BibTeX/RIS export; it
never dispatches a model or reaches the network.

| Command | Synopsis |
| --- | --- |
| `delivery start --delivery-id ID --section ID --review-id ID --trace-id ID --rights-file PATH [--project ID]` | Start the global review from recorded review/trace states (repeat `--section`/`--review-id`/`--trace-id` in lockstep). |
| `delivery freshness --delivery-id ID --cutoff-date DATE --decision {accept_cutoff,authorize_topup} [--approval-id ID] [--query-window-end DATE] --rights-file PATH` | Record the explicit freshness decision (never silently adds papers). |
| `delivery notes add --delivery-id ID --file PATH --rights-file PATH` | Store the human-notes action plan from a JSON array file. |
| `delivery notes apply --delivery-id ID --batch-file PATH --approval-id ID --rights-file PATH` | Apply one human-approved notes batch; a non-empty `--approval-id` is required and a batch is never auto-applied. |
| `delivery revalidate --delivery-id ID --paragraph ID --package-file PATH [--trace-id ID] --rights-file PATH` | Re-run the Step 12/13 checks for the named paragraphs from the approved package (never trusts the caller). |
| `delivery approve --prepare --delivery-id ID [--hashes-file PATH] [--approver LABEL]` | Print the exact-revision final-approval manifest digest and the `gate confirm` command; **records nothing**. |
| `delivery export --delivery-id ID --out-dir PATH --package-file PATH --rights-file PATH [--hashes-file PATH] [--policy-file PATH] [--ai-use-file PATH] [--venue NAME]` | Write the export package after the exact-revision final approval. |
| `delivery status --delivery-id ID [--project ID]` | Read one delivery's safe status summary. |

* `delivery start` requires every section to be review-complete and origin-complete
  (the workflow's `global_review_ready`/`origin_coverage` gate); the recorded
  review status and trace coverage fill the section list. `--section`/`--review-id`/
  `--trace-id` are paired by index and must have equal length.
* `--rights-file` is a strict JSON `RightsRecord` **template**; the CLI rebinds its
  subject/`rights_id` to each exact revision the workflow records.
* `delivery notes apply --batch-file` is a JSON object
  `{"batch_id": ID, "edits": [{"note_id", "paragraph_id", "new_text", "kind"}]}`.
  Only notes whose plan action is `local_edit`/`claim_edit` may be applied; a
  claim/citation edit (or a wording edit that changes numbers/citations) marks the
  paragraph stale for revalidation.
* `delivery approve` **cannot yet record** the final approval through the CLI: that
  requires owner authentication (a controlling-terminal confirmation) and a wiring
  between the named-gate ledger and `DeliveryRepository.final_approval`. So the
  command only runs with `--prepare`, which prints the manifest digest
  (`manifest_digest`) and the exact `gate confirm` command, and reports the gap
  (`approval_recorded=false`, `human_authenticated=false`). Without `--prepare` it
  refuses with a `validation` error explaining the gap. Nothing is written.
* `delivery export` builds `references`/`corpus` with
  `delivery.export.bibliography_from_packages` from the approved drafting packages
  (`--package-file`, repeatable), reads the recorded manuscript, and writes
  `manuscript.md`, `references.bib`, `references.ris`, `companion.md`,
  `summary.md`, `ai_declaration.md`, and `export_manifest.json`. It **refuses,
  writing nothing**, when the approval is missing/stale for the exact revision and
  artifact hashes (for example after a notes edit) or when a bibliography entry is
  inconsistent with the manuscript. An `--out-dir` inside the `--data-dir` (or a
  project's `.review_article_agent` directory) is refused like `backup create`.
  The output reports the per-file `file_hashes` (matching the manifest's
  `file_hashes` for the six content files) and `consistency_ok`.
* `delivery status` reports the global-review status/rounds, the freshness status,
  whether notes are planned, the applied-batch count, the paragraphs still
  requiring revalidation, and whether an approval/export is recorded. The private
  raw state is never emitted.

Errors use the exit categories of §3 with static, value-free messages, and every
output carries `authorizes_execution=false`, `authorizes_export=false`, and
`scientific_validation=false`.
