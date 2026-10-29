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
- Seeded hash-order random selection plus boundary-nearest risk selection (may
  overlap; capped at available exclusions). Audits bind to the exact batch and
  require original-source inspection statements for every selected case.
- `exclusion_set / screening.exclusions` owner approval is required to obtain
  exclusion IDs. Any recorded false exclusion pauses that exact batch permanently;
  correct the inputs, create a new batch, and re-audit. History is not overwritten.
- Pilot reports show observed examples only, not held-out/population accuracy.
  No artifact authorizes extraction. Confidence is not study quality.

## Use

`PYTHONPATH=src python3 -m review_article_agent.cli screening --help`

The small JSON-file interface has `propose`, `show`, and `exclusions` commands.
`propose` takes `--kind config|pilot|batch|audit`, `--input-file`, `--rights-file`,
`--artifact-id`, `--revision-id`, and optionally `--project`/`--parent-revision-id`.
Use the same pilot artifact with a new revision when iterating. `show` takes the
kind and exact IDs; private content requires `--content`. Approve reports through
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

**92 passed** (11 new screening tests), synthetic sources and simulated terminal
confirmation only. Covers authentic named gates, acquisition rejection, restart,
stale config/source, audit replay/coverage, pilot failures, false-exclusion pause,
and summary-only CLI reads. No full-suite run, network calls, costs, or scientific
validation. No commits or push performed.

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
