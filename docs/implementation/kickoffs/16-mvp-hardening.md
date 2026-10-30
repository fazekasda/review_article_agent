# Handover — review_article_agent: fast tests, CI, and a working MVP

## Steps

- [x] 1. Set up the working base (fresh worktree from `main`, baseline check)
- [x] 2. Fix the upstream re-validation performance defect (biggest test-time win)
- [x] 3. Add GitHub Actions CI (fast + sharded full suite)
- [x] 4. Test-suite diet: fast/slow tiers, cheaper fixtures, prune duplicates
- [x] 5. One real model port (OpenRouter chat) wired into the model-backed stages
- [x] 6. `pipeline run` orchestrator (run until the next human gate)
- [x] 7. Real PDF text extraction
- [x] 8. Close the data seams (bibliography metadata, extraction → synthesis)
- [x] 9. CLI groups for `review`, `tracing`, `delivery`, and extraction as a worker job
- [ ] 10. Housekeeping: README, plan ticks, handoff

Steps 2–4 are about test time and CI and should land first (separate PRs are fine). Steps 5–9 make the MVP usable. Step 10 closes out.

---

## 0. Read this first

### What the project is
`review_article_agent` is a personal Python CLI that writes evidence-grounded scientific review articles: brief → literature discovery → corpus freeze → full-text acquisition → parsing → screening (Jev model) → extraction → synthesis → outline → section blueprints → drafting → critique/fact-check → citation-origin tracing → global review/notes → final approval → Markdown/BibTeX/RIS export, with human approval gates between stages. Python stdlib only (`dependencies = []`, optional `pypdf`), `requires-python >= 3.11`, SQLite storage, detached background worker.

Governing documents (in the repo): `docs/specification.md` (scientific/product baseline; confirmed requirements must not be weakened), `docs/implementation/README.md` (step plan 00–15, start/handoff protocol, global definition of done), `docs/implementation/handoffs/*.md` (what each previous session claims), `docs/release.md` (feature/support matrix), `docs/cli.md`, `docs/storage.md`, `docs/worker.md`, `docs/contracts/`, `docs/decisions/`.

### Where the code is
- All implementation (steps 00–15) is on **`main`**: PR #19 (`feat/step15-release`) was merged on 2026-10-30 as `b9e1200`; its tree is identical to `feat/step15-release` head `588291d`, which the audit below was run against. The repo is **public**.
- Branch new work from `origin/main`. The main checkout `/home/fazekasda/projects/review_article_agent` is on the stale docs branch `docs/session-implementation-plan`, so use a new worktree rather than that checkout. There are ~100 leftover task worktrees under `review_article_agent-worktrees/` (including `step15-release`); leave them alone.
- Run things with `PYTHONPATH=src` (tests also import from `tests/`, e.g. `PYTHONPATH=src:tests`).

### Validated state (audit of 2026-10-30)
- Full suite: `PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_*.py'` → **3,678 tests, 1,659.7 s (~28 min), OK, 9 skipped** (7 = optional pypdf absent, 2 = opt-in Step 15 scale/release gates). Python 3.14.4, Linux, 4 CPUs. ~92k LOC src, ~91k LOC tests, 123 test modules, no CI.
- Solid and working: project registry/artifacts/backups (step 02), detached worker with durable jobs and crash recovery (03), discovery with real PubMed/Europe PMC HTTP, citation expansion, coverage report, corpus freeze (05), open-access JATS XML retrieval + folder import + dedup (06), human gate CLI (`gate`).
- Not a working review writer yet:
  - **Almost no stage calls a model.** Writer/critic/fact-checker/reviewer/tracer are injected `Callable` ports that only tests fill with fakes (`review/workflow.py`, `tracing/workflow.py`, `delivery/workflow.py`). Synthesis (`synthesis/`) and drafting (`drafting/`) only validate caller-supplied JSON/text. Screening only accepts imported Jev responses tagged `imported_unverified_dispatch` (`screening/workflow.py:21,265`).
  - Real model adapters exist but are mostly unwired: `runtime/lead.py:971 post_chat` (OpenRouter chat, used by `discovery lead run`, the **only** CLI-reachable real model path; needs `OPENROUTER_API_KEY`), `runtime/extraction.py:663 execute_extraction` + `extraction/runner.py` (has prompts at `runner.py:591–641`, but `execute_run` has no CLI/worker entry), `runtime/jev.py` (decisions endpoint, no production caller).
  - OpenCode session route is fixture-only (`runtime/execution_boundary.py:386`); `assert_production_execution_supported` (`execution_boundary.py:464`) is dead code.
  - **No orchestrator**: nothing moves a project from stage to stage; the Step 15 e2e tests glue library calls by hand and use synthetic `BRIDGES` (`tests/test_step15_e2e_front.py`, `tests/test_step15_e2e_manuscript.py`).
  - **No CLI** for review (12), tracing (13), delivery (14). Top-level `export` is only a private SQLite snapshot.
  - **PDFs yield no text**: `parsing/pdf_adapter.py:77–138` always returns `needs_review` with no passages. Only JATS/TEI XML is parsed.
  - **Bibliography seam broken**: drafting citation keys `cit_*` are `sha256(project+candidate)` with no title/author/year (`drafting/package.py:105–114`); the e2e export uses a made-up reference (`test_step15_e2e_manuscript.py:110`).
  - Plan ticks: 07 and 08 unticked (partial), 15 unticked (awaiting user review), 09–14 ticked as local MVPs. README top section is stale (describes step 05 as latest).

### Rules for this work
- Follow `docs/implementation/README.md` start/handoff protocol; write a handoff in `docs/implementation/handoffs/` for this work (suggested `16-mvp-hardening.md`).
- Keep the scientific invariants of `docs/specification.md`: approvals bind to exact revisions, agents cannot self-approve, stale approvals must be detected, bounded repair rounds (critique ≤3, fact-check ≤2, global ≤2), no invented citations, no automatic unofficial acquisition, documents/model output are untrusted.
- No secrets in the repo, tests, fixtures, or CI logs (public repo). Live model calls only when the user explicitly authorizes them, with the key from the environment.
- No automatic push/merge/publish unless the user asks; the user reviews PRs.
- Use the existing code; do not add an agent framework. New runtime dependencies are allowed only where the plan says so (PDF text) and should stay optional extras.

---

## 1. Set up the working base

- Done: PR #19 is merged; `main` (`b9e1200`) is the base for all new work and every PR targets `main`.
- Create a fresh worktree/branch from `origin/main` for each step group (e.g. `perf/revalidation-cache`, `ci/github-actions`, `test/suite-diet`, `feat/mvp-pipeline`).
- Baseline: run the full suite once (or rely on the numbers above) and keep the per-module timing table below as the before-state.

Per-module timing command used for the baseline (4 modules in parallel):

```bash
ls tests/test_*.py | sed 's|tests/||;s|\.py$||' | xargs -P4 -I{} bash -c 's=$(date +%s.%N); out=$(PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src:tests python3 -m unittest tests.{} 2>&1 | tail -3 | tr "\n" " "); e=$(date +%s.%N); printf "%.1f\t{}\t%s\n" $(echo "$e - $s" | bc) "$out"' > modules.tsv
```

Raw baseline data from the audit: `/tmp/opencode/raa_times/modules.tsv` (may be gone after a reboot; the top of it is below).

---

## 2. Fix the upstream re-validation performance defect

### Evidence
Test time is concentrated, not spread over the 3,678 tests. Sum of module times 1,840 s; 65 of 123 modules take < 2 s; all 123 passed when run 4 at a time (module-level parallelism is safe).

| Module | Tests | Seconds |
| --- | ---: | ---: |
| test_drafting_drafts | 21 | 674.7 |
| test_drafting_workflow | 13 | 207.6 |
| test_drafting_workspace | 24 | 116.4 |
| test_cli_drafting | 16 | 91.7 |
| test_drafting_package | 15 | 75.7 |
| test_synthesis_workflow | 23 | 59.2 |
| test_cli_jobs | 29 | 50.5 |
| test_cli_snapshot | 15 | 42.6 |
| test_step04_service_acceptance | 17 | 39.9 |
| test_cli_human_gates | 18 | 33.6 |
| test_opencode_service | 54 | 29.7 |
| test_step15_e2e_manuscript | 1 | 29.4 |
| test_cli | 32 | 27.0 |
| test_cli_gates | 15 | 22.8 |
| test_session_runner_acceptance | 4 | 21.9 |

Drafting + synthesis modules = **1,242 s (~67%)** for ~130 tests.

cProfile of `tests.test_drafting_drafts.AssemblyLineageTests.test_parent_update_and_outline_order_and_historical_stability` (241 s, 781 M function calls): every drafting read/propose re-validates the **whole upstream chain from scratch**, recursively and repeatedly:

`drafting.workflow.read` / `require_blueprint` / `_rederive_current` → `synthesis.workflow.require_outline` / `require_framework` / `require_synthesis` / `_normalize` (235 calls) → `extraction.workflow.require_batch` (757) / `require_for_synthesis` (252) → `acquisition.manifest.require_acquired` (774) → `discovery.freeze.require_frozen` (776) → full payload re-validation (`freeze._validate_payload`, `manifest._validate_payload`).

In that one test: `storage/human_approvals._validate_approval_row` 15,056×, `named_gate_satisfied` 5,197×, `evaluate`/`current_approval` 7,521×, `domain/records.canonical_json` 184,406× (104 s cumulative), `_check_jsonable` 7.8 M recursive calls, `_has_surrogate` 11.3 M× (85 s; `domain/records.py:172` is a per-character Python loop). `drafting/workspace._current_outline_section_ids` (100 s for 18 calls) and `drafting/package.build` (107 s for 16 calls) are the main entry points.

This is a product bug, not only a test problem: the real product would slow down the same way at 1k–10k candidates (the Step 15 scale test only covers discovery).

### Work
1. Make `_has_surrogate` cheap: `text.isascii()` fast path, otherwise one `text.encode("utf-8")` (raises on lone surrogates) instead of a Python per-char loop. Check `_check_jsonable`/`canonical_json` for similar hot spots; avoid re-canonicalizing payloads that were hashed and validated at write time when reading them back by hash.
2. Validate each immutable revision once per operation: an operation-scoped (per transaction / per top-level call) memo for the `require_*` gate checks, keyed by the inputs that make the result valid (revision id + content hash + approval-ledger state/version). Alternatively pass the already-validated upstream context down the call chain instead of re-deriving it in every layer. Prefer the smallest change that removes the repeated work.
3. Do **not** introduce a process-global cache that could hide a stale approval. Fail-closed behavior must stay exactly the same.
4. Add tests: a changed upstream revision / revoked approval still invalidates downstream reads within and across operations; a micro-benchmark-style regression test is optional (avoid flaky timing asserts; counting calls via a patched counter is fine).

### Acceptance
- Full suite still green with no test deleted or weakened.
- Drafting + synthesis modules drop from ~1,240 s to well under a minute; full suite re-measured and recorded (expected: a few minutes instead of ~28).
- Profile of the same test recorded before/after in the handoff.

---

## 3. GitHub Actions CI

### Facts (checked 2026-10-30)
- Repo is public → standard GitHub-hosted runners are free and unmetered; `ubuntu-latest` = 4 vCPU / 16 GB for public repos. Do not use larger runners (always billed). If the repo ever goes private: 2 vCPU / 8 GB and 2,000 included minutes/month on the Free plan.
- Tests are POSIX-specific (Unix domain sockets, 0700 permissions, SIGKILL, detached worker) → Ubuntu only.
- Python 3.11 is declared but has never been run → test 3.11 and 3.14.
- No secrets needed; never add live-model jobs (logs are public).

### Design
- `.github/workflows/ci.yml`; triggers `pull_request`, `push` to `main`, `workflow_dispatch`, nightly `schedule`. `concurrency: ci-${{ github.ref }}`, `cancel-in-progress: true`. `permissions: contents: read`. Pin actions (`actions/checkout`, `actions/setup-python`) by commit SHA.
- Job **`fast`** (every push/PR): matrix 3.11 + 3.14; fast test tier (step 4), `python -m compileall -q src`, `python3 scripts/check_contract_docs.py`, `git diff --check`. Target < 3 min.
- Job **`full`** (PRs to `main`, nightly, manual): whole suite including the slow tier, sharded with a matrix (start with 6–8 shards on 3.14, 1–2 on 3.11). Shards assigned by a new stdlib-only `scripts/test_shards.py` that balances modules by recorded duration from `tests/durations.json` (seed it from the table above / a fresh measurement). `timeout-minutes` per job; upload per-shard timing as an artifact so `durations.json` can be refreshed.
- Job **`release`** (manual/tag only): `RAA_RELEASE_CHECK=1` clean-venv install check (`scripts/release_check.py`) and the gated 1k/10k scale test (`scripts/scale_test.py`).
- Leave the TypeScript spike tests (`spikes/runtime/typescript`) out of CI.
- Ask the user to enable branch protection on `main` requiring `fast` and `full` (this is a repository setting; do not change it without asking).

### Notes
- A shard can't finish faster than its slowest module; until step 2 lands, `test_drafting_drafts` (~11 min) dominates. Do step 2 first, or shard by test class.
- CI does not replace a fast local loop: agents need a local command under ~2 min (step 4).
- Fix flaky tests (injected clocks, event waits); don't add automatic retries.
- After CI exists, handoffs should cite the CI run URL instead of self-reported suite counts.

### Acceptance
- Green `fast` (3.11 + 3.14) and `full` on a PR; wall time and per-shard durations recorded in the handoff.
- A deliberately failing test on a scratch branch turns the check red (then delete the branch).

---

## 4. Test-suite diet

Re-measure after step 2 and size this work by what is still slow.

### Targets
- Fast tier (default local command and CI `fast`): **< 2 min** on 4 CPUs.
- Full tier: **< 8 min** locally; sharded in CI.
- Total tests: aim for **≤ ~1,000** (from 3,678) without losing coverage of behavior.

### Work
1. **Tiering**: a `slow` gate via env var (`RAA_SLOW_TESTS=1`, mirroring the existing `RAA_RELEASE_CHECK=1` pattern): real subprocess CLI runs, real detached worker, mock OpenCode service, SIGKILL/recovery matrices, scale, clean-venv install. Default run excludes them; CI `full` includes them.
2. **Local runner**: small stdlib `scripts/run_tests.py` that runs modules in parallel (`concurrent.futures`, one subprocess per module), `--slow` flag, prints top-N slowest modules. Module-level parallelism was verified safe.
3. **Cheaper fixtures**:
   - Test-only SQLite durability option for temp stores (`PRAGMA synchronous = FULL` is set at `storage/schema.py:37`); production stays `FULL`.
   - Build a template store/project once per class (`setUpClass`) and copy it per test (today ~117 per-test `setUp`s build fresh stores; only 3 class/module-level setups exist).
   - Use in-process `cli_main.main([...], env=..., stdout=..., stderr=...)` (already used in `tests/test_cli_acquisition*.py`) instead of `subprocess.run([sys.executable, "-m", ...])` (e.g. `tests/test_cli.py:82`, `tests/test_cli_gates.py:107`); keep one subprocess smoke test per entry point in the slow tier. 24 modules spawn subprocesses today.
   - Replace real `time.sleep` polling (`test_worker_process.py`, `test_worker_engine.py`, acceptance suites) with injected clocks or event waits.
4. **Prune / merge**:
   - Collapse table-style validation tests into one `subTest`-parameterized test per validator: `test_jev_adapter` (164 tests), `test_decision_records` (124), `test_opencode_client` (112), `test_records` (105), `test_lead_adapter` (86), `test_acquisition_validation` (76), `test_agent_roles` (63), `test_gate_snapshots` (61).
   - Delete tests that repeat a lower-level test of the same invariant; keep the per-step acceptance test that names each AC.
   - Code off the MVP path (OpenCode session runner, decision-intent/result ledgers, gate-snapshot internals): move its tests to the slow tier; delete dead code such as `assert_production_execution_supported` (`runtime/execution_boundary.py:464`) with its tests.
   - Step 15: keep one front-half and one manuscript e2e chain and one test per injected fault.
   - With CI covering the full suite, prefer moving expensive but meaningful tests to the slow tier over deleting them.
5. **Guardrail**: CI `fast` fails if it exceeds its time budget.

### Acceptance
- Fast tier < 2 min and full tier < 8 min, measured and recorded.
- Every AC test cited in handoffs 02–15 still exists, or its removal is justified in the handoff. Security/fail-closed behaviors keep at least one test each: gates cannot be bypassed, no secret leakage, one active project, no duplicate accepted result, stale approval detection.
- No product behavior change except test hooks (durability option, injectable clock) and deleted dead code.
- Handoff includes a before/after table (test count, time per tier, top-10 slowest modules) and the list of merged/deleted/moved tests with reasons.

---

## 5. One real model port

Goal: every model-backed role runs through one small adapter so a real project can produce real outputs.

- Build on `runtime/lead.py:post_chat` (direct OpenRouter chat, key from `OPENROUTER_API_KEY`, already gated by human hosted-use approval, rights, and budget reserve via `discovery lead configure` / `gate confirm`). Reuse those gates; do not invent a parallel authorization path.
- Roles to implement as concrete port implementations (replacing test-only fakes in production code paths): screening classifier (Jev via `runtime/jev.py`, or chat fallback, bound to the existing decision ledger in `storage/decision_intents.py`/`decision_results.py`), synthesis/evidence-map proposer, outline/framework proposer, section writer, section critic (different concrete model, separate context), fact-checker, global reviewer, notes planner. Model per role is configurable (spec: "configurable generative models by role").
- Versioned prompt templates in the code (one module or a `prompts/` package), with `prompt_version` recorded on outputs (the pins already exist).
- Model output stays a **proposal**: it goes through the existing validators and human gates; nothing becomes authoritative without the existing approvals.
- Tests use a recorded/fake transport; one optional live smoke test runs only when the user explicitly authorizes it and a key is set (never in CI).
- Open the OpenCode production route only if the user decides to; for the MVP the direct OpenRouter path is enough (step 00 accepted it as fallback).

## 6. `pipeline run` orchestrator

- New CLI command (e.g. `pipeline run` / `pipeline status`) that, for the active project, determines the current stage from durable state, submits the next job(s) to the worker, and stops at the next human gate, budget limit, pause, or failure, reporting what the user must do next (which `gate`/approve command).
- Reuse existing repositories/workflows and the worker; the orchestrator holds no authoritative state of its own beyond what's already stored. Re-running it is idempotent.
- Integration test: a small synthetic project driven end-to-end by repeated `pipeline run` + scripted gate approvals using the fake transport. This replaces the hand glue in the Step 15 e2e tests for the MVP path.

## 7. Real PDF text extraction

- Implement text extraction in `parsing/pdf_adapter.py` with the optional `pypdf` extra (already declared `pdf = ["pypdf==6.21.4"]` in `pyproject.toml`), producing passages with page locators.
- Keep the existing quality states: low-quality or empty text → `needs_review`, never an automatic exclusion. Figures/tables/supplements needed for a decision still escalate to human review.
- Optional for the MVP: Unpaywall lookup in acquisition to reduce `manual_required` candidates (Europe PMC/PMC OA XML only today, `acquisition/resolution.py`).

## 8. Close the data seams

- Carry title, authors, year, DOI/PMID from the frozen corpus records into the drafting citation records so `delivery/export.py` (`render_bibtex`/`render_ris`) emits real bibliography entries; remove the `_BRIDGE_REF` workaround in the e2e test.
- Feed accepted extraction output (Step 09 receipts) into synthesis inputs instead of the synthetic receipt fixture.
- Step 13 tracing: wire the `acquire` port to Step 06 acquisition (for the MVP, depth 0 with the explicit secondary-attribution warning is acceptable per the spec).
- Step 14: make revalidation re-run Step 12/13 checks instead of trusting caller-supplied results.

## 9. CLI groups and worker jobs

- Add thin `review`, `tracing`, and `delivery` CLI groups mirroring the existing `drafting` group (`cli/drafting.py`), including a real `delivery export` that writes Markdown/BibTeX/RIS after the exact-revision final approval.
- Add an extraction job kind so `extraction/runner.execute_run` runs in the worker (it already has receipts, gates, and a cost ledger).
- Small CLI fixes found in the audit: clearer error when the data dir/project root is not owner-private (0700); `backup create` fails when the destination is inside `--data-dir`.

## 10. Housekeeping and handoff

- Refresh the README top section (currently describes step 05 as the latest state) and the feature/support matrix in `docs/release.md`.
- Update ticks in `docs/implementation/README.md` only where acceptance criteria actually pass.
- Handoff (`docs/implementation/handoffs/16-mvp-hardening.md` or per step group): scope, files changed, commands and CI run links, before/after test timings, stubs and known gaps, costs incurred, next actions.
- Ask the user before pushing, opening PRs, merging, or making any live model call.