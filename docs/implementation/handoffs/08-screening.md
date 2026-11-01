# Step 08 — local screening MVP (partial)

Scope: `08-screening.md`, the scientific baseline, and Step 01's project-specific
sampling policy. Builds on Step 07's partial XML parser. Step 08 remains unticked.

## Implemented

- `screening/workflow.py`: immutable config, iterative pilot, batch, and audit
  reports over the existing artifact store. No migration or new dependencies.
- Explicit predicate eligibility questions, project-chosen thresholds and sample
  counts. Jev responses are validated with the existing typed adapter; raw
  probabilities remain separate from human adjudications. Errors, missing
  context, oversize articles, and uncertainty retain cases for review.
- **Windowed whole text (S19-SCRWIN-WF).** `SAFE_MAX_REQUEST_BYTES` (32 KiB) is
  far below a real article (≈200 KB), so the whole parsed article is split by the
  pure `case_windows(definitions, context)` into deterministic, document-order
  windows, each of which fits one Jev request (size is measured with the real
  `build_request`/`canonical_json`, not estimated). Every window begins with a
  fixed `Article window {k} of {n}.` header (and the `Target passage: …` text in
  its header region for a passage case) and, when the report declares non-text
  assets, ends with the fixed non-text limitation line (reserved in every
  window). No text is dropped or reordered: `window_bodies(...)` concatenates
  back to the whole article text exactly; a single passage too large for one
  window is split at whitespace boundaries (character boundary as a fallback).
  `case_context` is now ready for any cleanly `parsed` report with non-empty text
  regardless of size, and the old "fits the bound" limitation fallback is gone.
  `WINDOWING_VERSION = "screening-windows/1"` is recorded in every case.
- A case binds one verified Jev result per window, in window order, via
  `{"decision_result_refs": [ref, ...]}` (length must equal the window count);
  the single `{"decision_result_ref": ...}` form stays valid only for a
  one-window case. Multi-window imports use
  `{"window_responses": [envelope, ...]}`. The per-question raw probability is
  stored per window (`window_decisions`) and aggregated conservatively by
  `aggregate_window_decisions`: a question is a proposed exclusion only if
  **every** window genuinely answered it and the **maximum** probability is below
  the exclude threshold; a refusal/malformed/missing answer in any window keeps
  that question uncertain and can never exclude. `observed_model` must be
  identical across windows (refused otherwise). The case `request_sha256` is the
  digest of the ordered list of per-window request digests; `windows` lists each
  window's `request_sha256`/`input_sha256`, and `input_sha256` is the whole
  article text.
- Exact parser/model/config versions and source/report dependencies. Current
  acquisition plus `pilot / screening.pilot` approval are required for batches.
  A pilot does not depend on the corpus, so adding papers alone does not stale it.
- The pilot pins the exact model *string* the validated response envelope reported
  (`observed_model`); a base alias and a dated snapshot are distinct strings and
  never silently substitute for each other. Equal strings cannot prove identical
  frozen weights, and imported responses remain unverified dispatches.
- Seeded hash-order random selection plus boundary-nearest risk selection (may
  overlap; capped at available exclusions). Audits bind to the exact batch and
  require original-source inspection statements for every selected case.
- `exclusion_set / screening.exclusions` owner approval is required to obtain
  exclusion IDs. Any recorded false exclusion pauses that exact batch permanently;
  correct the inputs, create a new batch, and re-audit. History is not overwritten.
  The exclusion check is one atomic point-in-time read transaction; a consumer that
  acts on the returned decision must revalidate inside its own write.
- Superseded/stale revisions stay inspectable read-only via
  `ScreeningRepository.read(ref, kind, historical=True)` and
  `screening show --historical`. A historical read verifies the stored target
  bytes/envelope, the exact declared dependency refs, and bounded transitive
  dependency content integrity (a report pins its parsing report, which pins the
  exact source bytes). The walk is iterative and cycle-safe, prunes no depth, and
  validates every edge's own exact same-project ref (before any skip or dedup, so
  a foreign-project edge reusing local ids is rejected). It is bounded only by a
  unique-revision count (the `MAX_GATE_SNAPSHOT_ENTRIES` convention, enforced at
  discovery): exceeding it raises rather than silently
  checking a partial closure. It does **not** rerun the current model/parser/config/gates
  (an old algorithm may no longer exist). Its output is marked stored evidence only —
  never current validation, scientific, or eligibility authority. Default `show`
  stays summary-only, and `propose`/`exclusions` expose no historical option, so stale
  evidence can never authorize work.
- Pilot reports show observed examples only, not held-out/population accuracy.
  No artifact authorizes extraction. Confidence is not study quality.

## Use

`PYTHONPATH=src python3 -m review_article_agent.cli screening --help`

The small JSON-file interface has `propose`, `show`, and `exclusions` commands.
`propose` takes `--kind config|pilot|batch|audit`, `--input-file`, `--rights-file`,
`--artifact-id`, `--revision-id`, and optionally `--project`/`--parent-revision-id`.
Use the same pilot artifact with a new revision when iterating. `show` takes the
kind and exact IDs; private content requires `--content`, and `--historical` reads a
superseded/stale revision as stored evidence only. Approve reports through
the existing `gate` CLI, not through JSON fields. `exclusions` takes the exact
approved **audit** IDs and returns case IDs, not paper-wide eligibility labels.

Input shapes are demonstrated in `tests/test_screening.py` (`config`,
`pilot_input`, `batch_input`, `audit_input`). Counts/thresholds there are synthetic
test choices, not scientific defaults. `acceptance_rule` records the additional
human-reviewed project rule; the application enforces minimum samples, complete
inspection statements, and no important pilot failures/false exclusions, not
arbitrary natural-language rules. Only `purpose: eligibility` predicates are
supported; framework/hypothesis classification is intentionally absent.

## Verification / limitations

Python 3, stdlib only. Executed:

```sh
PYTHONPATH=src:tests python3 -m unittest test_screening test_parsing_core test_parsing_repository test_cli_parsing test_human_approvals
```

**106 passed** (`test_screening` now has 25 tests; 8 new historical-inspection
tests), synthetic sources and simulated terminal confirmation only. Covers
authentic named gates, acquisition rejection, restart, stale config/source, audit
replay/coverage, pilot failures, false-exclusion pause, summary-only CLI reads,
pilot model-string pinning, and explicit historical reads (after iteration and
after config/source change, rejected on tampered target/dependency bytes,
deeper-than-any-depth-prune corruption, a foreign-project transitive edge with
local ids, an over-bound graph failing closed, and unable to authorize stale
work). No full-suite run, network calls, costs, or scientific validation.

**S19-SCRWIN-WF windowing.** `tests/test_screening_windows.py` (19 tests) covers
short/large/oversize single-passage/passage-case packing (bounded, lossless,
deterministic, limitation reserved, target in every window), the
`aggregate_window_decisions` max/retain/refusal rules, the `_case`
`window_responses`/`decision_result_refs` forms (wrong count, per-window digest
binding, mixed `observed_model`), and the oversize-header-region fail-closed
hardening (a header/target region that alone cannot fit one request raises
instead of emitting one-character windows; `case_context`/`_case` treat it as not
ready and retain the case). Full fast tier `python3 scripts/run_tests.py -j 4`
passes (147 modules, 3681 tests), and
`python3 scripts/run_tests.py --slow -j 4 test_screening test_screening_runner
test_screening_windows test_generation_worker test_step15_e2e_front
test_step15_injected_faults test_mvp_pipeline_e2e` passes.

**S19-SCRWIN-RUN windowed dispatch.** `execute_screening_case` now splits the
whole article with `case_windows` and issues **one Jev request per window**
(request ids `"{request_id}-wNN"`), each its own runtime task/dispatch/intent and
ledger-committed result, exactly like the former single dispatch. Every window is
planned/validated and the total not-yet-dispatched reserve is checked against the
per-role cap **before** the first reserve (per-dispatch check retained); a
`JevError` or not-ready context in that phase is a static `ScreeningRunnerError`
(zero reserve, zero POST). Windows are processed sequentially and the first
ambiguous/unknown window stops the run (later windows unsent). An already-recorded
window is observed, never re-POSTed, so a same-input re-run replays completed
windows and dispatches only the missing ones. A post-reserve pre-POST
revalidation failure resolves the reserved dispatch to a definitive pre-send
failure (state `failed`, cost `not_incurred`, reserve released) and raises
`ScreeningRunnerError`; it is never left `submitted` with an outstanding reserve.
`tests/test_screening_runner.py` (19 tests) and `tests/test_generation_worker.py`
(26 tests) add multi-window dispatch/ordered-ref binding, full/partial re-run,
pre-reserve budget and oversize-header refusal (through the worker → `blocked`),
post-reserve revalidation failure, one-window back-compat, and worker replay of a
missing window (not complete). `docs/generation.md` and `docs/operator-guide.md`
describe the per-window ids/cost/replay and the `{"decision_result_refs": [...]}`
job result.

**S19-RETRY-SCREEN bounded automatic retry (ADR 006).** The per-window dispatch
loop now runs the `runtime/retry.py` recipe: `next_attempt(store, "{request_id}-wNN")`
inspects the deterministic chain (`-wNN`, `-wNN-r1`, `-wNN-r2`), owns the
supersede/abandon ledger transitions, and returns the next action. An ambiguous
attempt is superseded by a **new** dispatch under a new id (never a re-POST of the
same identity); the retry's reserve is checked through the existing per-dispatch
`_check_budget` (a superseded/abandoned predecessor counts toward the per-role
exposure at its full reserve via `_dispatch_exposure`). A `completed` window
contributes its completed member's readback; `definitive`/`exhausted`/
`refused_overhead` stop the case with a definitive `failed` status (the serial
remote slot is freed), and later windows are never sent. `_window_request_ids`
reserves room for the `-rK` suffix in `MAX_REQUEST_ID_CHARS`. `readback_screening_case`
follows each window's chain to its completed member (never claiming completion for
a window with no completed member) while keeping the legacy single-id back-compat.
The `transport=None` live bug is fixed in `runtime/jev.py`: `dispatch_plan` (and
`execute`) now map `None` to the real `post_decisions`, matching the other
direct-call runtimes — previously the worker's `transport=None` called `None(...)`
and every window became an ambiguous `unknown`. `tests/test_screening_runner.py`
adds retry-success, retry-exhaustion (slot freed), pre-existing-`unknown`
supersede+retry, and superseded-exposure tests (all with a no-op sleep);
`tests/test_jev_adapter.py` adds the `transport=None` → `post_decisions` test;
`tests/test_generation_worker.py` adds the worker-path `transport=None` test.

This MVP imports recorded Jev responses with explicit
`imported_unverified_dispatch` provenance. Input/request digests are recorded,
but do **not** prove those responses came from a dispatch on those inputs.
Live dispatch, worker routing, budget/transfer authorization integration and
durable response-ledger binding are deferred; do not treat this as a production
bulk classifier. It never sends source text or calls a provider. Only bounded,
current XML text is supported; unreadable/non-text context is retained. Sampling
is diagnostic, not completeness. Human adjudications are statements until an
owner approves the exact report. Next: bind this flow to the existing authorized
Jev dispatch ledger, then conduct an authorized human-adjudicated pilot; Step 09
must separately extend pilot coverage to extraction.
