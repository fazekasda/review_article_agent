# 01 — Ratify contracts and technical decisions

## Dependencies

Step 00. This is a decision/specification session before substantial implementation.

## Deliverables

- Record user disposition of P1–P6 in the baseline; preserve deferred items explicitly.
- Specify artifact identities/revisions, evidence and attribution records, machine versus human decisions, issue states, approval dependencies, and missing/unknown states.
- Agree pilot/audit sampling and acceptance, search coverage/stopping, cost/concurrency limits, origin-hop/source/retry defaults, and secondary-warning approval.
- Choose durable storage/IPC/package layout based on the spike. Prefer one app-owned project-scoped store initially unless evidence justifies more databases.
- Define pause, cancellation, safe project switching, worker restart, and ambiguous remote execution semantics.

## Acceptance criteria

- Every mandatory field/rule has a versioned contract and examples, including invalid and missing data.
- No numeric scientific threshold is invented solely to allow a stage to pass. Open decisions have an owner and block dependent behavior.
- Distinguish rubric, framework, outline, report, document version, study/dataset, experiment, and finding.
- Approvals reference exact artifact/dependency revisions; a model edit cannot replenish review budgets.
- At a tracing limit, supported secondary attribution remains a warned state requiring final human approval, never verified primary support.
- Document rights/privacy boundaries and whether tools may access hosted models; forbid secret/source-text leakage in fixtures/logs.
- The user reviews scientific policy choices. Architectural recommendations do not silently override confirmed requirements.

## Verification and handoff

Walk concrete examples through contracts: duplicate PDF, alternate version, missing full text, uncertain Jev decision, omitted result, false critic objection, and secondary-origin warning. Handoff lists approved decisions and anything still blocking later steps. No scientific feature completion is claimed.
