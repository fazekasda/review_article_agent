# 09 — Grounded extraction, appraisal, and output audits

## Dependencies

08. Extend the iterative pilot to the actual extraction model/instructions and evidence schema.

## Deliverables

- Original-text extraction producing summaries plus structured, source-linked findings.
- Context, available numerical details, limitations, direct/interpretive/secondary distinctions, and relevant citation flags.
- Approved appraisal dimensions with formal tools only when appropriate.
- Small-model quality gate, stronger-model/human escalation, and bulk output audits before synthesis.

## Acceptance criteria

- No extraction from Jev decisions or paper summaries alone. Each accepted finding has resolvable original material and necessary context.
- Test missing findings, negations, nulls, numerical units/denominators, model/species differences, author spin, tables and supplements.
- Missing data remains missing. Genuine snippets do not substitute for completeness checks.
- Important pilot failures block bulk extraction; material model/instruction/parser changes reopen relevant approval.
- Random/risk human audits inspect original sources for omissions and qualifications as well as returned-field correctness. Important errors pause affected output for correction/re-audit.
- A weak study is not automatically excluded; uncertainty/limitations remain attached. No inappropriate universal quality score.
- Separate source records, studies/datasets, experiments and results; linking does not assert unproven identity.

## Handoff

Provide accepted schema examples, pilot/audit evidence, known omissions, escalations, and quantitative-field failure cases. No synthesis or framework can bypass this audit gate. Do not claim sampled checks establish complete correctness.

## Operator note — direct-call response diagnostics

A direct extraction/quality dispatch that receives an HTTP 200 whose body cannot
be accepted is an accepted **diagnostic**, never a completion. The raw response
body is deliberately not retained, so the exact failing branch is not
reconstructable after the fact; instead the adapter records one bounded, static
diagnostic **code** (a fixed literal, never peer text, a finish-reason string, or
a provider message). The code is persisted in the non-authorizing proposal's
`execution_metadata.diagnostic_code` and surfaced by `extraction run status`
(readback `diagnostic_code`). It distinguishes branches such as
`response_finish_length` (the completion hit `max_output_tokens`),
`response_content_not_json` / `response_content_not_object` (the assistant text
was not one JSON object), `response_model_mismatch` (the reply was not the
requested model), `response_error_object`, `response_envelope_not_json` (the body
was not JSON at all), and `response_too_large`. A provider-reported cost is
preserved (exact decimal) on these protocol failures; the reserve is retained when
no finite cost was reported. No schema, pilot, or approval gate is relaxed by
this observability, and no raw response is persisted.
