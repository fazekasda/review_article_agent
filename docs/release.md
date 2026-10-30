# Release guide

Operator-facing release, upgrade, and support notes for the local
`review-article-agent` core. This is a **personal, local, stdlib-only**
application: it publishes nothing by itself, starts no service at login, and
makes no scientific-validation claim. The authoritative interfaces are the
[CLI contract](cli.md), the [storage contract](storage.md), and the
[worker runtime safety contract](worker.md); the step-15 evidence is in the
[Step 15 handoff](implementation/handoffs/15-release.md).

## Supported runtime

| Item | Supported | Notes |
| --- | --- | --- |
| Python | **>= 3.11** | `pyproject.toml` `requires-python = ">=3.11"`. |
| Tested interpreter | **CPython 3.14.4 on Linux (x86_64)** | The only interpreter the full suite and the scale run were executed on; 3.11 is supported but **unrun**. |
| Runtime dependencies | **None** (standard library only) | `dependencies = []`; no agent framework. |
| Optional extra | `pypdf==6.21.4` via `.[pdf]` | **PDF text extraction** (Step 07): bounded per-page text with page locators, plus the Step 06 input-integrity inspector. It is **not** a scientific parser; OCR, figures, tables, and column order are not interpreted and escalate to human review, and without it PDF text stays `needs_review`. |
| OpenCode service | **v2.2.0, pinned** | `runtime/service.py` `OPENCODE_PINNED_VERSION = "2.2.0"` (Step 04); the app owns a version-pinned service separate from ordinary coding sessions. |
| Model transport in tests | **recorded fakes only** | No live model/provider call was made in any test; every model output is a recorded or injected fake. **Live model validation is unrun** (no credential was available to the coordinator). |

## Installation, upgrade, and migration

Install as a package (console script `review-article-agent`):

```console
python3 -m venv .venv && . .venv/bin/activate
pip install .            # or: pip install '.[pdf]' for the optional PDF diagnostic
review-article-agent --help
```

Or run in place without installing (standard-library core):

```console
PYTHONPATH=src python3 -m review_article_agent --help
```

**Storage migration behavior** (see [`storage/schema.py`](../src/review_article_agent/storage/schema.py)
and [storage contract](storage.md)): the store and the application registry keep
independent, independently-versioned SQLite schemas (store schema 11, registry
schema 2). Migrations are **applied automatically when the store or registry is
opened** — each numbered migration runs inside its own `BEGIN IMMEDIATE`
transaction and advances `PRAGMA user_version` in the same transaction, so a
failed migration rolls back completely and leaves the previous version intact.
Migrations are **strictly additive** and **never rewrite earlier data**; a
database written by a **newer** schema version is **refused** with a clear
`UnsupportedSchemaVersion` error rather than read or downgraded. Upgrade path:

1. `review-article-agent backup create DEST --project ID` for each project
   (a consistent private snapshot, not a manuscript export).
2. Upgrade the application; open the project/registry once so pending
   migrations apply.
3. If an older build reports `UnsupportedSchemaVersion`, upgrade the
   application — do **not** attempt to downgrade the database.

**Always back up before upgrading.** A private snapshot accepts store schema
versions 3 through the current version.

## Operations

The day-2 command surface (exact spellings in the [CLI contract](cli.md)):

| Area | Commands | Reference |
| --- | --- | --- |
| Worker lifecycle | `worker start` \| `worker status` \| `worker stop` | [cli.md §9](cli.md), [worker.md](worker.md) |
| Jobs | `job run` \| `attach` \| `status` \| `list` \| `pause` \| `resume` \| `cancel` \| `switch` \| `drain` \| `reconcile` | [cli.md §10](cli.md), [worker.md §6–§8](worker.md) |
| Approvals | `gate …` (app-owned named-gate human approvals) | [cli.md §11](cli.md) |
| Orchestration | `pipeline status` \| `pipeline run` | [cli.md §19](cli.md), [cli.md §24](cli.md) |
| Model-backed roles | `generation run submit` \| `status`; `generation hosted-use propose` \| `show` | [cli.md §23](cli.md), [generation.md](generation.md) |
| Review / tracing / delivery | `review …` \| `tracing …` \| `delivery …` | [cli.md §20–§22](cli.md) |
| Private snapshot | `backup create` \| `verify` \| `restore` (and the `export` alias) | [cli.md §7](cli.md), [storage.md](storage.md) |

Key points:

- `job run` returns **admission, not completion**; observe results later from a
  new CLI with `job status`/`job attach`. A stable `--request-id` is the
  idempotency key.
- At most **one project is execution-active** at a time; `job switch`/`job drain`
  safely drain the old project before another executes.
- `worker stop` is an authenticated control request (never a signal); a foreign
  or unavailable endpoint is a stable non-success, never a silent success.
- `backup`/`export` produce a **private local storage snapshot** (no
  Markdown/BibTeX/RIS) that must never be published.

## Troubleshooting

| Symptom | Likely cause | Action |
| --- | --- | --- |
| `worker stop` exits `3` (`conflict`) | A foreign endpoint occupies the control path, or its acknowledgement is missing/malformed | Nothing was signalled; inspect the control socket owner and retry. Never sends a signal. |
| `worker stop` exits `4` (`storage`) / stop unconfirmed | Stop delivered but the worker never confirmed shutdown, or the control endpoint is unavailable | Retry `worker start`/`worker stop`; check that the data directory is readable. |
| `backup restore` exits `3` (`conflict`) | The snapshot's project identity is already registered at another root (never a clone) | Restore into a **separate, empty data directory**. |
| Open fails with `UnsupportedSchemaVersion` | The database was written by a newer build | Upgrade the application; do not downgrade or hand-edit the schema. |
| A job stays `reconciliation_required` | An attempt had ambiguous external work after interruption | Resolve with `job reconcile --resolution …` (explicit owner disposition); do not mint a new identity or blind-retry. |
| Two projects cannot run at once | The one-active-project invariant | `job drain`/`job switch` the current project first. |
| PDF asset status is `unavailable` | Optional `pypdf` extra not installed | Install `.[pdf]` for the **diagnostic** inspector, or route the asset to human review (installing it does not make PDF a scientific parser). |
| A `gate` approval cannot be confirmed | Approval requires the owner on the controlling terminal (`/dev/tty`) | Confirm interactively; piped stdin / `--yes` cannot confirm by design. |

## Scientific limitations

The application is an **assistant**, not an oracle. Read these limits together
with [`specification.md`](specification.md) and the
[Step 15 handoff](implementation/handoffs/15-release.md):

- **Pilot/audit observations are calibration and inspected-sample evidence**,
  not held-out accuracy, completeness, or population-wide guarantees. A passed
  pilot or audit does not prove that un-inspected material is correct.
- **Omission detection is heuristic** (lexical/structural cues) and can miss a
  consequential omission that the human reviewer would catch.
- **Citation-origin tracing is not exhaustive**; attribution cues and support
  are lexical heuristics over tagged links, and cached reuse does not re-read
  the upstream source. No exactly-correct or exhaustive historical-origin claim
  is made.
- **No independent validation** exists: a different model is **not** independent
  human verification, and sampled audits are not independent dual-human review.
- **No live model was used in tests.** All model outputs in the test suite are
  recorded or injected fakes; there is no paid provider call, credential, or
  network egress, and no scientific feature has been validated against a live
  model.
- **Visual/supplement evidence is escalated, not machine-interpreted.** A
  required figure/supplement that cannot be read produces a visible blocker and
  a human-review action; it is never silently dropped or treated as negative
  evidence.
- **Scale numbers are single-machine measurements and not promises.** No numeric
  scale budget was agreed in Step 01; the target is ~1,000–10,000 candidates per
  project.

## Feature / support matrix

Capability status for Steps 02–14 and the kickoff-16 additions, derived from the
handoff reports and the step documents. "No CLI" means the capability is a library
API exercised by tests, without a command-line surface. Every model output used
anywhere below is a recorded/injected fake; no live model or provider call is
involved, and live model validation is **unrun**.

| Step | Capability | Status |
| --- | --- | --- |
| 02 | CLI, project registry, immutable artifacts, private backup/restore | **Implemented + tested** (CLI + library) |
| 03 | Detached worker, durable synthetic jobs, recovery/reconciliation | **Implemented + tested** (CLI + library; synthetic tasks only) |
| 04 | Restricted roles, model adapters, named-gate approvals, app-owned OpenCode service | **Implemented + tested** (library session path + `gate` CLI; model transport is a loopback fixture) |
| 05 | Reproducible discovery, candidate ledger, citation expansion, coverage/freeze | **Implemented + tested** (CLI + library) |
| 06 | Full-text acquisition, imports, reconciliation, rights | **Implemented + tested** (CLI + library; PDF inspector optional/diagnostic) |
| 07 | Parsing, source assets, reference structure | **Partial**: XML/JATS/TEI core **implemented + tested** (CLI); **PDF text extraction implemented** through the optional `pypdf` extra (bounded page-locator passages, conservative `parsed`/`needs_review` states); OCR/visual/table-cell interpretation **not implemented** (human review) |
| 08 | Iterative pilot, Jev screening, exclusion audits | **Implemented + tested** (CLI + library); responses are imported/fixture, no live-dispatch binding |
| 09 | Grounded extraction, appraisal, output audits | **Implemented + tested** (CLI + library); dispatch receipts are synthetic, no live transport |
| 10 | Synthesis, framework, outline, argument record | **Implemented + tested** (CLI + library; synthetic upstream fixtures) |
| 11 | Section blueprints, evidence-grounded drafting | **Implemented + tested** (CLI + library; planner/writer mappings are fixture rows) |
| 12 | Bounded critique and original-source fact-checking | **Implemented + tested** (`review` CLI + library); model-backed critic/repair run through `generation run submit`, test ports are fakes (no live model) |
| 13 | Citation-origin tracing | **Implemented + tested** (`tracing` CLI + library); resolves the acquisition port over the project's already-acquired sources, no network (depth-0 attribution caveat) |
| 14 | Manuscript integration, author notes, freshness, delivery/export | **Implemented + tested** (`delivery` CLI + library); owner-authenticated exact-revision final-approval gate and a real Markdown/BibTeX/RIS **export with a corpus-derived bibliography** (title/authors/year/journal); model ports are fakes (no live model) |
| 16 §5 | Model-backed generative roles on the direct OpenRouter route | **Implemented + tested** (library + `generation run` worker job + `generation hosted-use` CLI); recorded/fake transports in tests, **live validation unrun** |
| 16 §6 | Pipeline orchestrator (run until the next human gate) | **Implemented + tested** (`pipeline status` + `pipeline run` CLI + library; fake model transport; screening and pilot design stay manual) |
| — | GitHub Actions CI (fast + sharded full suite) | **Configured + green** (`.github/workflows/ci.yml`; `fast` on 3.11/3.14, `full` 6+2 shards; see [ci.md](ci.md)) |

Not implemented as of this release: OCR/visual/table/figure scientific parsing, any
live model/provider execution (**live validation unrun**), real acquisition uploads,
and any hosted transfer.

## Release checklist

- [ ] Full test suite passes (`PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_*.py'`).
- [ ] Contract-document check passes (`python3 scripts/check_contract_docs.py`).
- [ ] Clean-environment release check passes (`python3 scripts/release_check.py`).
- [ ] Scale run reproduced and its report reviewed (`RAA_SCALE_TESTS=1 PYTHONPATH=src python3 -m unittest tests.test_step15_scale`; see `docs/implementation/handoffs/15-scale-report.json`).
- [ ] **User** reviews the end-to-end demonstration, the scale report, and the scientific-limitations section.
- [ ] No automatic publish, push, service autostart/lingering, cloud deployment, or journal submission is performed.
