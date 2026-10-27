# 08 — Iterative pilot, Jev screening, and exclusion audits

## Dependencies

07. Requires the human pilot and audit policy from 01.

## Deliverables

- Single iterative pilot workflow, human adjudications, diagnostic reports, and exact-version approval.
- Jev original-paper/contextual-passage classification using approved bounded questions.
- Uncertain retention and proposed exclusion batches with source-linked random/risk audit selection.
- Material-change detection and pause/re-pilot paths.

## Acceptance criteria

- Bulk screening cannot run without acquisition and relevant pilot approvals.
- Classifier failures/refusals/missing context are not negative eligibility labels. Uncertain or unexpected-category sources are retained for review.
- Hypothesis agreement or a provisional framework label cannot silently serve as eligibility.
- Human approval binds to the actual batch and inspected source examples. An important false exclusion pauses that batch and records corrective actions.
- Changes to model/rubric/parser behavior invalidate relevant approvals; ordinary new papers do not trigger needless whole-project re-piloting.
- Store typed probabilities, rubric/input/model versions, and machine/human decisions separately. Confidence is not study quality.
- Pilot reports show observed example behavior and limitations, not held-out or population-wide accuracy.

## Handoff

Provide adjudicated test cases, exact question contracts, audit selection/replay evidence, false-exclusion handling, and unresolved parameters. Stage 09 must extend pilot coverage to extraction before bulk extraction is authorized; passing screening alone is insufficient.
