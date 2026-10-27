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
