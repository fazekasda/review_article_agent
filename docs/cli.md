# CLI contract — `review-article-agent` (Step 02)

> **Scope: Step 02 only.** This document is the **contract** for the command-line
> surface over the durable local project foundation. It describes exactly what the
> commands accept, what they print, and how they fail. It is **not** a scientific
> or approval contract and it claims **no** scientific validation. Approval,
> execution, worker, model, backup, and restore commands are **out of scope here**
> and belong to later steps.

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

Messages are **actionable summaries with safe structural context**. They never
contain a raw traceback, a raw exception payload, imported file contents, SQL
text, or a secret. Configuration errors name the offending *field*, never its
*value*.

**Exit categories** (stable):

| Exit | Category | Meaning |
| --- | --- | --- |
| `0` | — | Success. |
| `2` | `usage` | Command-line usage or configuration-file problem. |
| `2` | `validation` | Invalid input data (bad identifier, malformed import, unknown id shape). |
| `2` | `not_found` | A referenced project id is not registered. |
| `3` | `conflict` | Divergent duplicate / immutable-state conflict. |
| `4` | `storage` | Store/registry/schema/path/filesystem/permission error. |
| `1` | `internal` | Unexpected error; no traceback is shown. |

Both `usage` and `validation` deliberately share exit `2` (matching `argparse`'s
convention); the JSON `category` distinguishes them.

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
  rejected field is reported by **name only** — its value is never echoed.
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

## 7. Explicit exclusions (Step 02)

* No `approve`/`reject`, `run`/`pause`/`resume`, `worker`, or `model`/provider
  command exists here. Those depend on later steps' storage and approval layers
  and are **not** reimplemented in the CLI.
* No scientific acquisition, evidence validation, or approval is performed or
  implied. Fixtures are synthetic and fixture-only.
* Backup/restore/export commands are a **separate** task once their storage layer
  is integrated; the data-directory and store layout here is designed for it.

## 8. Extension hooks

Adding a subcommand is intentionally small:

1. Add a parser in `build_parser()` (`review_article_agent/cli/main.py`).
2. Add a handler `cmd_<name>(args, ctx) -> dict | RawBytes` that returns a
   JSON-ready object.
3. Register it in `_COMMANDS` (or `_ARTIFACT_COMMANDS`).

Handlers receive a `Context` holding the resolved `Registry`, the data directory,
the parsed config, and the environment, and never touch global state. New exit
categories are added to `classify_error()` and this table.

## 9. Verification

* `PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_*.py'` — includes
  the real-subprocess CLI acceptance tests (`tests/test_cli.py`) and the config
  tests (`tests/test_config.py`).
* `python3 scripts/check_contract_docs.py --self-test` — the Step 01 contract
  checks remain green (this document adds no fixtures or schema field semantics).
* The packaged console script and the absence of an invented license are checked
  by building a wheel offline in `tests/test_cli.py`.
