# Step 09 — grounded extraction MVP

Status: **complete as a locally verified, XML-text MVP**. Authority:
`09-extraction.md`, `docs/specification.md`, Step 01 policies, and
`docs/decisions/004-step09-direct-extraction.md`. No synthesis implementation.

## Interfaces

- `extraction/schema.py`: strict original-passage grounding, explicit missing
  values, context, attribution, numbers, coverage and separate source/study/
  dataset/experiment/result identities. `raw_extraction_example()` and
  `EXTRACTION_EXAMPLE_PASSAGES` provide a synthetic schema example. Links—even
  model-labelled “confirmed” links—remain proposals; unlinked is not independent.
- `runtime/extraction.py`: bounded single-shot direct OpenRouter extraction and
  quality checks; explicit models/provider/prices; one POST per plan, no retry in
  the adapter itself (the runner applies the bounded retry, below).
- `extraction/runner.py`: scoped transfer gates, active-project fencing, cost
  reservations and durable dispatch/proposal-bound receipts. `execute_run(...)`
  is an explicit library call, not a detached worker job or automatic scheduler.
  An **ambiguous** (`unknown`) `direct_call` outcome (transport error/timeout/
  ambiguous status) is retried automatically and boundedly (decision 006): each
  attempt is a new deterministic identity (`{request_id}`, `-r1`, `-r2`; the same
  identity is never re-POSTed), the ambiguous predecessor is resolved to terminal
  `failed`/`estimated` = its full reserve so the serial remote slot is freed, and
  the overhead cap bounds the retry cost; an exhausted/overhead-refused chain is a
  definitive non-completion. `require_run(store, ref, purpose=...)` revalidates
  the original material and exact accepted dispatch. No imported response can
  substitute for a dispatch.
- `extraction/workflow.py`: project-approved appraisal and sampling config, one
  iterative extraction/quality pilot, random/risk output audits, and
  `require_for_synthesis(audit_ref)`. Important audit errors permanently pause the
  exact batch; correction requires new output and re-audit.
- `cli/extraction.py`: `extraction propose|show|accepted --help`. JSON inputs,
  explicit rights files, exact artifact/revision IDs; no automatic approval.
  `show --content` opts into private evidence. Use the existing `gate` CLI for
  owner approvals, never fields in model JSON. Named `batch` gates are
  `extraction.config`, `extraction.pilot`, `extraction.batch`, and final
  `extraction.outputs`; the runner also requires the purpose-specific
  `extraction.hosted_use.extraction|quality` gate.

## Scientific checks and limits

The pilot requires adjudicated labels for missing findings, negation, nulls,
numeric units/denominators, species/models, author spin, tables and supplements.
Quality checking uses original material plus extraction, not snippets alone.
Audits explicitly inspect original sources for omissions, qualifications and
returned-field correctness. Missing values stay missing; weak studies retain
their limitations. Formal appraisal tools require an approved applicability
rationale, not a universal quality score.

XML text is supported by the existing parser. PDF/OCR/visual interpretation and
any parser-flagged non-text evidence remain human-review blockers: a table or
supplement caption is not a substitute for inspecting its content. Structural grounding
does not establish scientific correctness; sampled audits are not completeness,
held-out accuracy, or independent dual-human review. Real project approvals and
human-adjudicated scientific acceptance are not supplied by implementation tests.

## Verification

- Baseline: `PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_*.py'`
  — 3,146 tests, 7 skips, passed before Step 09 integration.
- Final focused Step 09 suite: `PYTHONPATH=src:tests python3 -m unittest
  test_extraction_runner test_extraction_workflow test_extraction_schema
  test_extraction_transport test_cli_extraction` — **140 passed**.
- Integrated regression set before the final caption-review fix: **513 passed**,
  including screening, parsing, discovery model calls, approvals, dispatch,
  acquisition, gate snapshots and artifacts. Contract-doc checker: **15 passed**;
  package `compileall` and `git diff --check` passed.
- The real-store/fake-transport end-to-end test covers approved config, actual
  dispatch receipts, adjudicated pilot, genuinely acquired corpus, bulk
  extraction/quality, random/risk audits, synthesis gate, permanent pause and
  correction/re-audit. Unit workflow tests separately patch the receipt seam;
  those alone are not evidence of dispatch integration.
- Final full suite at source/test head `79525d0`: `PYTHONPATH=src python3 -m
  unittest discover -s tests -p 'test_*.py'` — **3,286 tests passed, 7 skips**
  (425.718 seconds). These are local implementation checks, not live-model or
  scientific validation. This handoff/index update is documentation-only.
- Synthetic fixtures and fake transports only so far. No live cost incurred.
  The user authorized a small live smoke via `OPENROUTER_API_KEY`, but it is not
  visible to the agent's shell or its accessible ancestor processes. Never put
  the key in repository files or test output.

No migration or external dependency added. No scientific accuracy claim.

Quantitative failure cases are exercised in `test_extraction_schema`: unavailable
values remain null, missing units/denominators are not invented, and faithful
null/negative reports are distinct from missing data. The model checker and human
audit—not a genuine snippet or structural validator—must detect semantic errors.
Escalations preserve missing checks and limitations; they never auto-exclude a
weak study. No live scientific pilot/audit has been performed for a real project.

## Amendment 2026-11-01: numeric representation (prompt `extraction.direct.v2`)

The first live pilot response put all 29 known numbers in as JSON strings (18 plain
numbers such as `"15.35"` and 11 qualified values such as `"<0.01"`, `"2-3"`,
`"Thirty"` and `"0, 10, 20, and 40"`). It also gave one omission with
`target_kind: "topic"`. The validator rejected the response correctly. Policy from
this amendment on:

- A known `numeric_details[].value.value` is a single JSON number. Strings are
  still refused and are never coerced into numbers.
- A qualified value (range, inequality, approximation, list, or number in words)
  uses `value: {"state": "not_applicable", "value": null}`. It carries the exact
  original text in the new optional `reported_as` field, which must be an exact
  substring of the finding's passage. `reported_as` is required whenever the
  value is `not_applicable`.
- An omission's `target_kind` is only `passage` or `asset`. A topic-level
  omission leaves out both `target_kind` and `target_id`.
- The instructions and the example output now state these rules. `PROMPT_VERSION`
  is `extraction.direct.v2`, so receipts made under v1 are not reusable.
- A schema-invalid escalation now keeps `validation_error`: the validator's first
  failing field path, which uses schema vocabulary only and never model or source
  text. The run readback shows it.

## Amendment 2026-11-01 (b): identifiers and snippets (prompt `extraction.direct.v3`)

The first v2 live call met the numeric contract: no numeric strings, and qualified
values went into `reported_as`. It was still rejected, for these reasons:

- It used short passage ids (`p0021` for `rep-…:p0021`).
- It gave asset-caption passage ids (`asset-0`) where the asset ids belong, so it
  missed the double coverage that an asset caption passage needs.
- Its `report_ref` was free text.
- One snippet joined two spans with "...".

The instructions now require copying ids exactly from the headers. They explain the
asset-id versus passage-id distinction and the double coverage, tell the model to
leave out `report_ref`, and forbid ellipsis-joined snippets. The example no longer
shows a `report_ref`. The validator is unchanged and still strict.
`extraction run status` now prints `validation_error`.

## Amendment 2026-11-02: several findings per passage (prompt `extraction.direct.v4`)

The v3 call met the id rules but showed a gap in the schema. Coverage takes exactly
one entry per passage, that entry named one `finding_id`, and every finding had to be
claimed, so a passage could only yield one finding. The call returned 67 findings
from 35 passages.

- A `finding` coverage entry may now give `finding_ids` (a non-empty list without
  duplicates) instead of `finding_id`, but not both. Every listed finding must cite
  that passage. Every finding must still be claimed.
- The normalized entry keeps `finding_id` (the first id) and adds `finding_ids` when
  there are several.

The same call also put an invented `conditions_note` key in `context`. The validator
still refuses it; the prompt now says `context` has exactly five keys plus `note`.
Re-expressed in these two forms, the live v3 response validates, with all snippets
and `reported_as` values grounded.

## Amendment 2026-11-02 (b): coverage of empty supplied passages

The first v4 call on PMC13450824 was **accepted**. The first v4 call on PMC13714240
was rejected only because it covered 14 asset headers that have no caption as
`omitted` (`empty_asset_header`). An empty passage carries no evidence, so it is
not required, and before this amendment a coverage entry for it counted as an
"unknown required item". Coverage may now also account for any supplied passage
id. Required items must still be covered exactly once, ids outside the pool are
still refused, and a finding still cannot cite empty text. This changes the
validator only; the prompt stays `extraction.direct.v4`. The stored rejected
response is not re-classified; it remains an escalation.

## Amendment 2026-11-02 (c): pilot hosted-use must not declare an acquisition

The accepted PMC13450824 pilot receipt could not feed its quality check. Its
hosted-use document declared `acquisition_ref`, so the pilot receipt recorded that
reference, and `require_run` refuses a pilot receipt that declares bulk authority.
`execute_run` now refuses that combination before any reserve or POST. The pilot
hosted-use documents were revised with `acquisition_ref: null`; bulk documents
keep theirs. The unusable receipt stays in history and is not edited.

## Amendment 2026-11-02 (d): prompt `extraction.direct.v5` and quality output budget

- PMC13714240 attempts 2 and 3 were rejected for two reasons. Some findings cited
  passages that the model's own coverage marked `no_finding`. One snippet left out
  an inline citation marker ("as published,66 whereas"). The instructions now say
  to keep citation markers inside a snippet or end the snippet before them, and
  that a `no_finding`/`omitted` passage is never cited. The validator is unchanged.
- The first Haiku quality check of the accepted PMC13450824 receipt (66 findings)
  stopped at the 8,192-token output limit (`response_finish_length`). The quality
  contract now asks for concise items and groups minor issues. It still lists every
  blocking and major issue. The pilot config r6 raises the quality
  `max_output_tokens`.
