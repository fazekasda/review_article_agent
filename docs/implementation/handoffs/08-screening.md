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
  exact source bytes), but it does **not** rerun the current model/parser/config/gates
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

**103 passed** (`test_screening` now has 22 tests; 5 new historical-inspection
tests), synthetic sources and simulated terminal confirmation only. Covers
authentic named gates, acquisition rejection, restart, stale config/source, audit
replay/coverage, pilot failures, false-exclusion pause, summary-only CLI reads,
pilot model-string pinning, and explicit historical reads (after iteration and
after config/source change, rejected on tampered target/dependency bytes, and
unable to authorize stale work). No full-suite run, network calls, costs, or
scientific validation.

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
