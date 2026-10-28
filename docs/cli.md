# CLI contract — `review-article-agent` (Step 02)

> **Scope: Step 02 only.** This document is the **contract** for the command-line
> surface over the durable local project foundation. It describes exactly what the
> commands accept, what they print, and how they fail. It is **not** a scientific
> or approval contract and it claims **no** scientific validation.
>
> It covers: projects/registry, immutable artifact revisions, a **private local
> storage snapshot** (`backup`/`export`) and **non-authorizing local
> storage-fixture review state** (`fixture`). The `fixture` commands record a
> local *fixture* only; they are **never** a real protocol/pilot/outline/final
> release approval and never authorize execution, export, or science. No
> agent-execution (`run`/`worker`/`model`) or scientific-`approve` command exists
> here.

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
| `fixture status APPROVAL_ID [--project ID]` | Show one approval's computed status, snapshot verification, and whether the approved revision is still current. |

Every `fixture` output carries the non-authorizing constants
`scope="storage_fixture"`, `actor_authenticated=false`,
`authorizes_execution=false`, `authorizes_export=false`, and
`scientific_validation=false`. The `rationale` is stored but is **not** echoed in
`status`/`list` output.

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

## 9. Explicit exclusions (Step 02)

* No scientific `approve`/`reject` and no `run`/`pause`/`resume`, `worker`, or
  `model`/provider command exists here. Those depend on later steps' execution
  and scientific-approval layers and are **not** reimplemented in the CLI.
* `fixture` records a local, non-authorizing fixture only. No real
  protocol/pilot/outline/final-release gate and no in-band actor/authority is
  accepted; an imported document still cannot self-grant any approval.
* No scientific acquisition, evidence validation, or approval is performed or
  implied. All fixtures and archives are synthetic/private and non-authorizing.
* `backup`/`export` is a **private local storage snapshot**, not a manuscript
  (Markdown/BibTeX/RIS) or redistribution export; never publish an archive.

## 10. Extension hooks

Adding a subcommand is intentionally small:

1. Add a parser in `build_parser()` (`review_article_agent/cli/main.py`).
2. Add a handler `cmd_<name>(args, ctx) -> dict | RawBytes` that returns a
   JSON-ready object.
3. Register it in `_COMMANDS`, `_ARTIFACT_COMMANDS`, `_BACKUP_COMMANDS`, or
   `_FIXTURE_COMMANDS`.

Handlers receive a `Context` holding the resolved `Registry`, the data directory,
the parsed config, and the environment, and never touch global state. New exit
categories are added to `classify_error()` and this table.

## 11. Verification

* `PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_*.py'` — includes
  the real-subprocess CLI acceptance tests (`tests/test_cli.py`,
  `tests/test_cli_snapshot.py`) and the config tests (`tests/test_config.py`).
* `python3 scripts/check_contract_docs.py --self-test` — the Step 01 contract
  checks remain green (this document adds no fixtures or schema field semantics).
* The packaged console script and the absence of an invented license are checked
  by building a wheel offline in `tests/test_cli.py`.
