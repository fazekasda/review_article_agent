# Handoff — step 07: parsing, evidence assets, and reference structure

**Status: partial (scoped parsing MVP); not fully validated.** S7-CORE (`c235075`/`314589a`), S7-STORE
(`ace293b`) and S7-CLI (`e39f91c`, this task) are integrated on `task/step07-cli` from base `ace293b`: a
stdlib XML-first structural core, a durable report repository, and a personal `parsing` CLI. The step-07
index row is **not ticked** — an MVP only; no overall completion or combined result is claimed and no
human-adjudicated benchmark was run. Baseline: `docs/implementation/07-parsing.md` (deliverables lines
9-12, AC lines 16-22), `docs/specification.md` (immutable revisions; normalized text a separate
revision; distinct rights; no self-approval; failed evidence → human action), predecessor
`handoffs/06-acquisition.md`. **S7-CLI only** — core, repository, storage and acquisition were not edited.

## Files and interfaces changed

- `src/review_article_agent/cli/parsing.py` (new): root `parsing` group — `parse`/`show`/`locator`/`resolve`.
  Safe summaries by default; private passages/captions/section-headings/raw markers only with `--content`.
- `src/review_article_agent/cli/main.py`: routing, category exit codes and error-message integration
  for `ParsingControlError` (small, uniform diff).
- `tests/test_cli_parsing.py` (new): 5 combined CLI tests.
- `handoffs/07-parsing.md` (this file) + one annotated step-07 index line.

## Acceptance criteria → evidence (CLI/MVP scope)

- AC16 (resolve after restart; detect changed deps): parse → restart → `resolve` hits the exact report.
- AC19 (unresolved/malformed links fail that passage): a `ref9` passage reports `unresolved_count=1`;
  markers only with `--content`.
- AC20 (no correctness/exclusion claim): outputs carry `authorizes_scientific_processing=false`,
  `excludes_candidate=false`.
- AC21 (failed parse → human action): malformed XML → `rejected`, `human_action_required=true`.
- AC22 (raw immutable, normalized separate): S7-STORE invariant; CLI reads only exact stored ids.

## Verification

- `PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_cli_parsing.py'` → **5 tests, OK**;
  `-p 'test_parsing*.py'` → **35, OK**; `tests.test_cli tests.test_cli_acquisition` → **41, OK**.
- `git diff --check` clean; `compileall` clean; `parsing`/`parse`/`resolve` `--help` smoke OK.
- **Not run:** full `test_*.py` suite (real detached-worker smoke exceeds the 2-minute default), live retrieval, any real-network run.

## Constraints and known limitations

- **XML/JATS/TEI supported.** PDF, OCR, full-text extraction, table cells and visual/figure
  interpretation are **unsupported → human review**.
- **Synthetic fixtures only** (self-authored JATS); **not** a representative human-adjudicated
  scientific benchmark. No scientific correctness, completeness, originality, screening or extraction
  is claimed.
- No network/model/provider/approval call; no auto approvals; no new deps. Stdlib Python (PSF). An
  existing optional `pypdf` (BSD-3) PDF *diagnostic* is Step 06 only; no model weights. Local CPU, bounded
  input (≤32 MiB). CLI ~500 lines (> soft 300-line guide) for the 4-subcommand surface + privacy/errors.

## Copyable usage

```bash
export PYTHONPATH=src
raa() { local d="$1"; shift; python3 -m review_article_agent.cli --data-dir "$d" "$@"; }
raa ./state parsing parse --project "$PID" --source-artifact-id art-src \
  --source-revision-id rev-src --report-artifact-id art-rep \
  --report-revision-id rev-rep --rights-file report-rights.json
raa ./state parsing show --project "$PID" --report-artifact-id art-rep \
  --report-revision-id rev-rep [--content]
raa ./state parsing locator --project "$PID" --report-artifact-id art-rep \
  --report-revision-id rev-rep --passage-id p0002 [--content]
raa ./state parsing resolve --project "$PID" --locator-file locator.json [--content]
```

Next (not this task): coordinator full-suite + docs-head run, an authorized PDF/XML benchmark, and human
adjudication before any scientific claim. Do not tick step 07 until its full AC set is validated. Temp store only.
