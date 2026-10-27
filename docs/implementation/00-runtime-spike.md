# 00 — OpenCode integration and language spike

## Dependencies and scope

First step. Read the specification; do not build the scientific application yet. Compare TypeScript with the official V2 client against a Python HTTP integration on the same minimal lifecycle. Use a disposable app-owned harness, never the ordinary shared service.

## Deliverables

- Minimal reproducible prototypes for submit, durable admission receipt, completion retrieval, disconnect/reconnect, and typed decisions.
- Record tested client/server/API versions, supported service discovery/authentication, effective configuration, and isolation.
- Decision record selecting the core language/integration and explaining the rejected option. Do not rely on undocumented private credential/DB parsing.
- Written restart findings: which state persists, whether an in-flight turn continues, and how interruption is detected/reconciled.

## Acceptance criteria and tests

- Closing a prototype CLI does not stop a bounded server-owned task; a second client retrieves its result after missed events.
- Retrying a client-supplied admission ID produces one accepted session input; admission is not mistaken for completion.
- Restarting only the disposable service yields either recoverable continuation or an explicit interrupted/unknown outcome. No lost work is silently declared complete.
- Demonstrate typed Jev predicate/choice/score handling and per-question failures using the selected route, or document a tested direct-OpenRouter fallback and accounting boundary. Paid calls require explicit budget authorization; otherwise this criterion remains blocked, not passed.
- Test fresh critic context and role/model pinning. A writer-history fork is not automatically a clean critic.
- Demonstrate no access/config changes to ordinary coding sessions. Document global/ancestor configuration inheritance risks.

## Exclusions and handoff

No full agent framework, scientific corpus, production daemon, or UI. Handoff includes reproducible commands, redacted results, unresolved restart behavior, and the language decision. Later steps must not begin stack-specific work until this passes.
