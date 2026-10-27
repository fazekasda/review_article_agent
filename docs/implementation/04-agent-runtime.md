# 04 — Restricted agent roles, model adapters, and approvals

## Dependencies

00–03.

## Deliverables

- App-owned, version-pinned OpenCode service integration and project/location/session mappings.
- Role configuration, bounded task submission, admission/result reconciliation, and captured model/usage metadata.
- Restricted scientific tools or MCP interface returning proposals, not granting authoritative approvals.
- Jev decision adapter with validated outputs and durable application records, including direct fallback if selected in 00.
- App-owned human approvals presented through CLI; OpenCode forms may be presentation only.

## Acceptance criteria

- An agent cannot approve its own artifact, edit authoritative state, widen scope, or invoke unapproved tools via retrieved instructions.
- A saved generic tool permission cannot satisfy an outline/pilot/exclusion approval.
- Different-model section critic uses a fresh context and a different concrete model ID; invalid configuration fails rather than silently using the writer model.
- Lost event streams reconcile from durable state/messages. Missing events do not lose completion or approval actions.
- Model/provider errors, refusals, invalid shapes, and partial Jev errors are not interpreted as scientific exclusions.
- Stateless decision responses are persisted with rubric/model/input versions. Account separately for direct calls not covered by session statistics.
- Project/session/model isolation holds across two saved projects; ordinary OpenCode service is untouched.

## Handoff

Document actual API/SDK versions, adapter contracts, permission tests, supported authentication, costs of authorized smoke tests, and recovery limitations. Do not assume coding-agent defaults are safe for scientific literature roles.
