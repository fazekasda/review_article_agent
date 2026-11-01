# S19 — extraction response/result-commit exception

Status: **fixed** (two untrusted-input defects at the adapter→runner→worker seam).
Authority: `docs/implementation/09-extraction.md`, `docs/specification.md` workflow 9,
C02 §5.6/§6.3/§8.3, ADR 004, ADR 006.

## Symptom

A live extraction run could end `waiting_human`/`discovery_indeterminate` with
`outcome_unresolved` true and **no receipt**, even though the provider responded
(well under the timeout). The worker's `_run_extract_dispatch` `except
BaseException` swallowed an unexpected exception with no log, so the cause was
invisible. Both defects below raise a **non-`ExtractionRunnerError`** out of
`execute_run`, which is exactly that silent path.

## Defect 1 — cost settled before the state transition (`extraction/runner.py`)

`_commit_atomic` handled a definitive `failed` outcome by settling cost
`not_incurred` **while the dispatch was still `submitted`**, then marking it
`failed`. `runtime_dispatch_state` has
`CHECK (state NOT IN ('submitted','admitted','unknown') OR cost_state <>
'not_incurred')`, so the settlement raised an uncaught `sqlite3.IntegrityError`
(a `BaseException`, not `ExtractionRunnerError`). Fixed by marking the failure
first, then settling — matching the screening/decision-results call sites.

Reachable via any definitive pre-billing HTTP rejection (400/401/402/403/404/413).

## Defect 2 — `Decimal` leaked from the strict parser into untrusted content

The shared strict JSON parser (`runtime.lead._parse_json`) parses floats as
`Decimal` for exact cost arithmetic. The extraction adapter reused it for the
model's **content** and deep-froze it into `proposal_json`. The runner's
canonical-JSON step (`canonical_json(result.proposal_json)`) then raised an
uncaught `RecordValidationError` — a `ValueError`, not a `ReviewAgentError`.
Fixed in `runtime/extraction.py`: the parsed content is narrowed to JSON-native
floats (`_json_portable`) before freezing, so a schema-valid numeric detail
(e.g. `{"state":"known","value":5.2}`) is accepted and any float elsewhere is a
clean escalation, never a crash. The schema is unchanged (`_is_number` still
accepts only `int`/`float`).

## Diagnostics

`worker/engine.py::_run_extract_dispatch` now logs only the unexpected exception
**class name** to stderr (mirroring the generation branch), never the message,
source, provider body, or credential — so a future indeterminate event is not
silent. Logging alone does not fix a defect.

## Confidence

The retained evidence cannot distinguish which defect caused the specific r2
call (the raw body was not kept). Both are reproducible at the real seam and both
produce the identical silent `indeterminate` outcome; both are now fixed. No paid
or live call was made.

## Latent siblings (out of scope, follow-up)

`generation/runner.py` has the same settle-before-fail order, and
`runtime/generation.py` reuses `lead._parse_json` for content the same way. These
were not patched here (extraction-only scope) and should be verified separately.
