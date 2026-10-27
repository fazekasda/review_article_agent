# Kickoff — step 01: Ratify scientific contracts and technical decisions

This is a short orientation for the session that implements **step 01** in a
fresh checkout. The authoritative scope, deliverables, acceptance criteria, and
verification for step 01 are in
[`docs/implementation/01-contracts.md`](../01-contracts.md): **read it and follow
it verbatim.** This kickoff only adds the checkout base, read order, and context
the step document assumes. Do not edit step 01's criteria.

## The decision this session follows

Step 00 passed its six acceptance criteria, and on **2026-10-27 the user accepted
the runtime-language decision**: **Python core with a supported, explicitly
authenticated OpenCode V2 HTTP integration** (`docs/decisions/001-runtime-language.md`).
The official TypeScript `@opencode/client` prototype is retained as a
**reference comparison only**, not the selected runtime. The tested Python
direct-OpenRouter Jev adapter (`spikes/runtime/live-jev/`) is the **retained
tested** Jev route; the native OpenCode `generate.decisions` route remains
**unexercised**.

The user's approval **selected the language/integration**; it did **not** waive
any acceptance criterion and did **not** approve any scientific policy, the
SQLite schema, IPC, package layout, or an agent framework.

## First read order

1. [`docs/implementation/README.md`](../README.md) — plan index, start/completion
   protocol, global definition of done, test levels.
2. [`docs/implementation/01-contracts.md`](../01-contracts.md) — **authoritative**
   for this session.
3. [`docs/specification.md`](../../specification.md) — especially "Pending
   decisions: must be resolved explicitly" (P1–P6), "Gates and scientific
   invariants", and "Citation-origin policy".
4. [`docs/decisions/001-runtime-language.md`](../../decisions/001-runtime-language.md)
   — the accepted runtime-language decision.
5. [`docs/implementation/handoffs/00-runtime-spike.md`](../handoffs/00-runtime-spike.md)
   — step-00 evidence and the restart finding.
6. [`docs/implementation/decision-template.md`](../decision-template.md) — the
   decision-record format.
7. The spike READMEs as needed: `spikes/runtime/python/README.md`,
   `spikes/runtime/harness/README.md`, `spikes/runtime/integration/README.md`,
   `spikes/runtime/live-jev/README.md`.
8. Consumers of step-01 decisions: `docs/implementation/02-project-foundation.md`
   and `docs/implementation/04-agent-runtime.md`.

## Checkout base — read this before branching

The step documents are on `origin/main`, but the **spike is not**: it lives on the
`feat/runtime-spike` branch, which is a **draft/unmerged pull request**. A fresh
checkout from `main` alone therefore lacks `spikes/`, the step-00 handoff, and
decision 001, and step 01 would be missing the evidence it must build on.

Do **one** of the following — never assume `main` contains the spike:

- the **user merges** the spike pull request into `main` first, then the session
  branches from the updated `main`; or
- the session branches **explicitly from the `feat/runtime-spike` tip**.

The coordinator will give the exact base SHA for the step-01 session. Confirm the
base contains both `docs/implementation/01-contracts.md` and `spikes/runtime/`
before starting.

## Session shape — contracts and decisions before code

Step 01 is a **decision/specification session before substantial
implementation**. It produces versioned contracts with worked examples, not a
production system. In particular it must cover:

- artifact identities and revisions; evidence and attribution records; machine
  versus human decisions; issue states; approval dependencies; and missing/unknown
  states;
- rights/privacy boundaries, including whether tools may access hosted models;
- pause, cancellation, safe project switching, worker restart, and ambiguous
  remote-execution semantics;
- durable storage, IPC, and package-layout **proposals** chosen from the spike
  evidence (prefer one app-owned project-scoped store initially).

All the original examples in `01-contracts.md` must be walked through the
contracts: **duplicate PDF, alternate version, missing full text, uncertain Jev
decision, omitted result, false critic objection, and secondary-origin warning.**

## Still open — do not silently decide

The user's pending decisions (specification "Pending decisions") stay **OPEN**
until explicitly dispositioned in this session with an owner:

- **P1** explicit framework contribution, concept-admission/support/revision
  criteria, and testable implications where applicable;
- **P2** detailed downstream causal/context-fidelity checks;
- **P3** integrity-status checks (retractions, expressions of concern,
  corrections) and adjudication of affected uses;
- **P4** public selection/synthesis and limitations reporting alongside the
  manuscript;
- **P5** AI-use declaration, human accountability, applicable provider terms and
  journal policies;
- **P6** optional target-venue constraint capture and submission-time
  verification, retaining venue-neutral output.

Also still open and theirs to decide: pilot/audit samples and acceptance rules,
issue severities, coverage/stopping rules, cost/concurrency limits, origin
hop/source/retry defaults, and explicit acceptance of secondary-origin warnings.
Record accept/modify/defer, give open items an owner, and let unresolved items
block dependent behavior. Do not invent a numeric scientific threshold to let a
stage pass, and do not treat the language approval as approving any scientific
policy, the SQLite schema, IPC, or a framework.

## Runtime invariants to preserve

These come from the accepted decision and the step-00 evidence; preserve them as
contract-level agreements, not new policies:

- Use an **app-owned OpenCode V2 service**, separate from ordinary coding
  sessions. The application owns projects, state, artifacts, approvals, budgets,
  and recovery.
- **Durable job continuity:** closing the CLI does not cancel an authorized
  background job. The **application owns restart reconciliation**, and an
  in-flight turn torn down by a service restart reconciles to an explicit
  **unknown/interrupted**, never to a silent success (the step-00 restart
  finding).
- **No shared service, no private database, no manual private-credential
  parsing.** Explicit endpoint and explicit credential only; loopback,
  fail-closed; redirects refused.
- **Approvals bind to immutable artifact/dependency revisions.** Agents cannot
  self-approve, edit authoritative state, or widen scope; a model edit cannot
  replenish a review budget.
- At a tracing limit, **secondary attribution stays a warned state requiring
  final human approval**, never "verified primary support".
- Document rights/privacy boundaries and forbid secret/source-text leakage in
  fixtures and logs.

## Exclusions

No production daemon, no full agent framework, no scientific corpus, and no UI.
No scientific feature completion is claimed by step 01.

## Handoff required

End the session with `docs/implementation/handoffs/01-contracts.md` containing
acceptance evidence per criterion, exactly which checks were run and which were
not, open decisions with owners, and exact next actions. **Do not merge
automatically and do not run new paid model tests.** The only paid call recorded
so far is the single authorized Jev call in step 00.

## Launch prompt (copy and paste into the new session)

> Implement step 01. Read `docs/implementation/01-contracts.md` (authoritative)
> and `docs/implementation/kickoffs/01-contracts.md`, then follow the kickoff's
> read order. Follow the step's deliverables, acceptance criteria, and
> verification verbatim; do not edit its criteria. Leave the handoff at
> `docs/implementation/handoffs/01-contracts.md`. Do not merge and do not run
> paid tests.
