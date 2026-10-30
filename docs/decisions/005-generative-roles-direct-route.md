# 005 — Generative roles on the direct OpenRouter route

## Status

**Accepted by the user — 2026-10-30.** The approving authority is the **user**.
The approving evidence is the coordinator's recorded **2026-10-30 instruction**
to implement kickoff 16 steps 5–10 (`docs/implementation/kickoffs/16-mvp-hardening.md`
§5), which directed that "for the MVP the direct OpenRouter path is enough (step
00 accepted it as fallback)", to "Reuse those gates; do not invent a parallel
authorization path", and to "Open the OpenCode production route only if the user
decides to". The user's 2026-10-30 instruction session is recorded as
**`ses_e73abb6baffeVk8BLNhLn10U3p`** (the OpenChamber coordinator session).

**What this approval is and is not.** It approves the **architecture/scope
direction** for the generative model roles on the direct route, grounded in code
already on this branch. It is **not** a scientific validation, **not** a claim
that any scientific feature is complete, and **not** authorization of a paid
model call, a hosted transfer, an upload of third-party papers, or approval of a
real project's pilot. The user separately authorized **one small live OpenRouter
smoke test**; that is **not** authorization to upload third-party papers or to
approve a real project. This is a **documentation-only** decision record: this
task changes no product source, test, schema, contract, or policy.

**Relationship to prior decisions.** This record **extends** [decision 003](003-step05-discovery-and-direct-lead.md)
(direct, single-shot, tool-free lead interview/framing proposals) and [decision
004](004-step09-direct-extraction.md) (bounded direct extraction and quality
checking) to the **generative roles**. It reuses their rights, provider-term,
scoped human-approval, activation, and cost-ledger safeguards rather than opening
a new route.

## Context and constraints

- **Step authority:** [`docs/implementation/kickoffs/16-mvp-hardening.md`](../implementation/kickoffs/16-mvp-hardening.md)
  §5 ("One real model port"): "every model-backed role runs through one small
  adapter"; "Reuse those gates; do not invent a parallel authorization path";
  "Model per role is configurable"; "Model output stays a proposal: it goes
  through the existing validators and human gates; nothing becomes authoritative
  without the existing approvals"; "Open the OpenCode production route only if
  the user decides to; for the MVP the direct OpenRouter path is enough".
- **Already-accepted decisions (not reopened):** [001](001-runtime-language.md)
  (Python core with a supported, explicitly authenticated OpenCode V2 HTTP
  integration; the direct-OpenRouter fallback is the retained, separately
  accounted route), [002](002-step01-contracts-and-policies.md),
  [003](003-step05-discovery-and-direct-lead.md) (direct lead), and
  [004](004-step09-direct-extraction.md) (direct extraction/quality).
- **Contracts:** [`C02`](../contracts/02-gates-and-runtime.md) §5.6 (cost/
  concurrency caps; reserve before dispatch; unknown reserve retained) and §6.3
  (exact-decimal `direct_call` cost, accounted separately from OpenCode session
  statistics); [`C03`](../contracts/03-policy-decisions.md) §5.2/§5.5 (rights
  eligibility is not final authorization; provider/journal-term verification is
  operation-scoped and gates hosted use).
- **Baseline of this approval:** branch `task/s16-gen-docs` head
  `5fe2ccb2e8890518b5e359beec1e7ad9cd7c6bce` (the merge of the S17 generation-port
  work into `feat/model-port`). The generative-role port code this record
  describes was merged on the branch as: `9ffc8db` (generic direct-OpenRouter
  generation adapter, S17-G1), `ade0a80` (per-role hosted-use document + named
  gate), `7f3913e` (application-boundary generation runner, S17-G4), `06f6109`
  (section critic + repair ports, S17-G7), `443a9df` (synthesis/framework/outline
  proposers, S17-G5), `df55fa5` (blueprint/section-writer drafting roles, S17-G6).
  The delivery-role and screening ports this record now also describes landed
  **after** the approval baseline (see below): `647c34b` (global reviewer/writer +
  notes planner ports, S17-G8) and `e6e8719` (Jev screening runner bound to the
  decision ledger, S17-G9).
- **Architecture actually present (verified against this branch):**
  - [`runtime/generation.py`](../../src/review_article_agent/runtime/generation.py)
    — a **pure single-shot adapter** over `lead.post_chat`: `ROLES` (the closed
    generative-role tuple), a fixed per-role directive, `role` + `prompt_version`
    pinned into the request digest and the synthetic system message, strict
    single-JSON-object output, typed cost classification, and **no retry**.
  - [`generation/hosted_use.py`](../../src/review_article_agent/generation/hosted_use.py)
    — the per-role `generation_hosted_use` document (`config` = the concrete
    model, `budget_cap_usd`, and `provider_terms`/`privacy_review`/
    `pricing_evidence` evidence), the named gate id
    `generation.hosted_use.<role>` (existing `batch` gate kind, confirmed through
    the **existing** `gate prepare`/`gate confirm` CLI), and `role_models_distinct`
    (critic ≠ writer; global reviewer ≠ global writer).
  - [`generation/runner.py`](../../src/review_article_agent/generation/runner.py)
    — `execute_generation_task`: activation token, exact hosted-use gate, per
    original-source hosted-transfer rights, per-role cumulative budget cap,
    reserve **before** the single POST, and an atomic commit of the
    non-authoritative proposal + dispatch result + settled cost; replay by
    request id; an ambiguous outcome is `unknown` with the reserve retained.
  - [`runtime/roles.py`](../../src/review_article_agent/runtime/roles.py) —
    `SCIENTIFIC_ROLES` extended with the generation roles (all with a bounded
    `steps = 1`).
  - Role modules: [`generation/synthesis_roles.py`](../../src/review_article_agent/generation/synthesis_roles.py)
    (framework `role`/`scope` keys are aliased `concept_role`/`relationship_scope`
    for the model because the proposal guard forbids `role`/`scope` keys; mapped
    back before validation), [`generation/drafting_roles.py`](../../src/review_article_agent/generation/drafting_roles.py)
    (the drafting workspace gains an additive `direct_hosted_use` binding kind
    alongside the OpenCode session binding), and
    [`generation/review_roles.py`](../../src/review_article_agent/generation/review_roles.py)
    (critic sees only draft + package; repair edits only targeted sentences,
    never citation keys; deterministic request ids `<id>.<role>.<sha16>` so
    resumes replay).
  - **Delivery roles (added after the baseline, merged on this branch as
    `647c34b`, S17-G8):** [`generation/delivery_roles.py`](../../src/review_article_agent/generation/delivery_roles.py)
    — `make_global_review_ports` builds the `global_reviewer`/`global_writer`
    ports for [`delivery/workflow.DeliveryRepository.run_global_review`](../../src/review_article_agent/delivery/workflow.py)
    (the writer edits only targeted paragraphs, and a paragraph edit that would
    add a citation key not already present is rejected; the distinct-concrete-model
    rule is enforced via `role_models_distinct`); `plan_note_edits` runs one
    bounded `notes_planner` request that proposes an edit batch, drops invalid
    edits with static reasons, and **never** calls `apply_notes` (the human
    approves the batch first). Both derive deterministic request ids
    `<delivery_id>.<role>.<sha16>`.
  - **Screening (added after the baseline, merged on this branch as `e6e8719`,
    S17-G9):** [`screening/runner.py`](../../src/review_article_agent/screening/runner.py)
    — `execute_screening_case` runs the **Jev** direct path (not a chat fallback):
    it reuses the `generation.hosted_use.screening` gate, reserves a `direct_call`
    dispatch via `runtime/dispatch`, records the immutable pre-dispatch intent
    (including runner-stored per-question `QuestionDef`/criteria artifacts),
    dispatches once, and commits via `DecisionResultRepository.commit_result`.
    Screening cases may reference a committed result
    (`{"decision_result_ref": {...}}`) and are then recorded with provenance
    `verified_dispatch`; imported responses keep `imported_unverified_dispatch`.
    A Jev answer never auto-excludes.

## Options and evidence

- **Open the app-owned OpenCode production route.** Rejected for the MVP: the
  fail-closed production runner gate
  [`runtime/execution_boundary.py`](../../src/review_article_agent/runtime/execution_boundary.py)
  still blocks the live `opencode_session` route because no defensible
  all-request cost bound and no scoped hosted-use/privacy/provider-term
  authorization are implemented for it. Opening it would require exactly the
  cost/authorization proof the direct route already carries, for no MVP gain;
  kickoff 16 §5 says to open it "only if the user decides to".
- **Per-call human approval for every generative request.** Rejected: it would
  make the review workflow unusable (a section draft, critique, repair, and every
  synthesis/outline step would each block on a controlling-terminal approval),
  and it invents a *second* authorization path alongside the already-approved
  `batch`/named-gate mechanism that kickoff 16 §5 says to reuse.
- **Chosen — one adapter, per-role hosted-use document + named gate.** Extend the
  tested direct route (decisions 003/004) to the generative roles: one pure
  adapter, one application-boundary runner, one per-role authorization document,
  and the existing human gates. This is the smallest change that makes a real
  project able to produce real model output without relaxing any invariant.

Documentation claim vs observed behavior: this record documents the direction and
boundaries; it makes **no** live/paid result claim. No paid call and no hosted
transfer is authorized by this record; live model validation of these roles
remains **unrun**.

## Decision

**Accepted by the user, 2026-10-30, against baseline `5fe2ccb` (direction
discussed against the code already merged on the branch).**

- **Roles covered.** The generative roles
  [`runtime/generation.py`](../../src/review_article_agent/runtime/generation.py)
  `ROLES`: `screening`, `synthesis`, `framework`, `outline`, `blueprint`,
  `section_writer`, `section_critic`, `section_repair`, `fact_checker`,
  `global_reviewer`, `global_writer`, `notes_planner`. `fact_checker` **remains**
  the deterministic [`review/schema.fact_check`](../../src/review_article_agent/review/schema.py)
  (line 492); the adapter declares a directive for it but no production port
  replaces the deterministic checker. `screening` is wired to the Jev direct path
  ([`screening/runner.py`](../../src/review_article_agent/screening/runner.py)
  `execute_screening_case`), which commits to the same decision ledger. The
  delivery roles `global_reviewer`, `global_writer`, and `notes_planner` are
  wired to production ports
  ([`generation/delivery_roles.py`](../../src/review_article_agent/generation/delivery_roles.py)
  `make_global_review_ports` / `plan_note_edits`); the notes planner only
  *proposes* a batch and never applies it without a human `approval_id`.
- **One adapter.** Every model-backed generative call runs through
  [`runtime/generation.py`](../../src/review_article_agent/runtime/generation.py)
  and the application-boundary
  [`generation/runner.py`](../../src/review_article_agent/generation/runner.py).
  The concrete model is **never defaulted**: it comes from the per-role hosted-use
  document's `config`.
- **Reuse the gates.** Authorization is the per-role `generation_hosted_use`
  document plus the out-of-band, owner-authenticated named gate
  `generation.hosted_use.<role>` (`batch` kind) recorded through the **existing**
  `gate prepare`/`gate confirm` CLI. No parallel authorization path is created.
- **Proposal only.** Model output is non-authoritative **data**: it is validated
  by the existing schema validators and stored as an inert proposal; the existing
  named human gates remain required before anything is authoritative. No invented
  citations are possible (the schemas restrict citations to supplied keys).
- **Bounded and accounted.** Per-role cumulative budget cap from the document's
  `budget_cap_usd`; a `direct_call` dispatch is reserved **before** the single
  POST and settled with an exact-decimal cost, accounted separately from OpenCode
  session statistics; an ambiguous outcome is `unknown` with the reserve retained;
  an already-recorded request id is observed, never re-sent.
- **Distinct models.** `section_critic` must use a different concrete `model_id`
  than `section_writer`, and `global_reviewer` a different one than
  `global_writer` (`role_models_distinct`).
- **Explicitly not decided / not authorized here.** No paid call; no hosted
  transfer; no upload of third-party papers; no approval of a real project's
  pilot; no change to the OpenCode production block, the scientific roles, or
  ordinary-session isolation; no change to the cost/concurrency caps; no
  scientific-feature completion or scientific validation. The live smoke test the
  user separately authorized is outside this decision.

## Consequences and verification

- **Unblocked direction.** The generative roles now have one recorded,
  user-approved architecture grounded in the code on the branch, so a real
  project can be wired to real model output without opening the OpenCode
  production route.
- **Risks and mitigations.**
  - *Cost.* Bounded per-role by `budget_cap_usd` and the reserve-before-dispatch
    rule (C02 §5.6); cost is read from the direct-dispatch ledger, never copied
    from a response.
  - *Prompt injection.* The untrusted material is the user message; the trusted
    instructions are the synthetic system message that states the untrusted-data
    and no-authority rules, and `role`/`prompt_version` are pinned in the request
    digest (`runtime/generation.py`).
  - *Model drift.* Each output is bound to the exact `prompt_version` (and
    `instructions_sha256`) that produced it, so a change of model or prompt is a
    new recorded version rather than a silent change of meaning.
- **Verification performed (offline; no paid call, no hosted transfer,
  documentation-only).** `python3 scripts/check_contract_docs.py` and
  `git diff --check` (see the handoff/commit for results). This record adds
  narrative only; it changes **no** checker fixture, test, schema, contract,
  enum, policy, or numeric threshold.
- **Conditions for revisiting.** A user decision to open the OpenCode production
  route, to change the per-role model policy, or to change the cost/concurrency
  caps; a material baseline change; or evidence that an accepted boundary fails
  in practice. Each change is a **new** exact-revision decision, not an in-place
  edit.

## Handoff

- Baseline of this approval: branch `task/s16-gen-docs` head `5fe2ccb`.
- Operator/developer reference: [`docs/generation.md`](../generation.md).
- Prior decisions: [003](003-step05-discovery-and-direct-lead.md),
  [004](004-step09-direct-extraction.md).
- Step authority: [`docs/implementation/kickoffs/16-mvp-hardening.md`](../implementation/kickoffs/16-mvp-hardening.md) §5.
- Contracts: [`C02`](../contracts/02-gates-and-runtime.md) §5.6/§6.3,
  [`C03`](../contracts/03-policy-decisions.md) §5.2/§5.5.
- Implementation surfaces: [`runtime/generation.py`](../../src/review_article_agent/runtime/generation.py),
  [`generation/hosted_use.py`](../../src/review_article_agent/generation/hosted_use.py),
  [`generation/runner.py`](../../src/review_article_agent/generation/runner.py),
  [`generation/synthesis_roles.py`](../../src/review_article_agent/generation/synthesis_roles.py),
  [`generation/drafting_roles.py`](../../src/review_article_agent/generation/drafting_roles.py),
  [`generation/review_roles.py`](../../src/review_article_agent/generation/review_roles.py),
  [`generation/delivery_roles.py`](../../src/review_article_agent/generation/delivery_roles.py),
  [`screening/runner.py`](../../src/review_article_agent/screening/runner.py),
  [`runtime/roles.py`](../../src/review_article_agent/runtime/roles.py).
- Decision format: [`docs/implementation/decision-template.md`](../implementation/decision-template.md).
- **Unresolved follow-up (not blockers):** a CLI for proposing the
  `generation_hosted_use` document (none exists today — the Python API is the
  only route; see `docs/generation.md`); a worker job kind for the generation
  roles (none exists yet — a pipeline/orchestrator change will add them); and
  live model validation (unrun). No secrets and no licensed full text are
  included here.
