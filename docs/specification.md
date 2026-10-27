# Scientific and product baseline

## Authority and status

This is the repository-local baseline distilled from the planning discussion. The session implementation plan links here so future sessions do not require access to earlier chat history or files in another user's home directory. All implementation steps are initially incomplete.

Confirmed requirements below must not be silently weakened. Technical choices remain proposals until their decision gates pass. The longer planning history is in `~/.opencode/plan/scientific-review-workflow-and-methodology.md` and `scientific-review-agent-project-plan.md`; those are historical references, not runtime dependencies. If they conflict with this baseline, obtain a user decision and record it.

## Product and runtime

- Personal command-line application; multiple persisted scientific review projects, **one active project at a time**. Within-project concurrency may be bounded independently.
- A separate local worker continues after the CLI closes, until a human gate, budget, pause, or failure. Restarting the application must recover durable project progress.
- Initial scope does not require automatic startup after logout/reboot or remote execution while the local machine is off. It must retain state across such interruptions and reconcile on restart.
- Use an **app-owned OpenCode V2 service**, separate from ordinary coding sessions. OpenCode executes bounded agent work; our application owns projects, state, artifacts, approvals, budgets, and recovery.
- Decide TypeScript core plus Python tools versus Python core plus supported OpenCode HTTP integration in a small spike. Neither is approved in advance. **Decided 2026-10-27 (step-00 spike): Python core plus a supported, explicitly authenticated OpenCode V2 HTTP integration.** TypeScript with the official OpenCode V2 client was implemented and compared and remains a reference, not the selected runtime; see `docs/decisions/001-runtime-language.md`.
- SQLite is the proposed local store, not an approved schema. Prefer the simplest design that passes the recovery tests. Do not depend on OpenCode's private database or manually parse private credentials.
- Target approximately 1,000–10,000 candidates per project. Do not create an autonomous session for every deterministic paper operation.
- Initially export Markdown, BibTeX, and RIS. No generated manuscript figures/tables, web frontend, statistical meta-analysis, or complete formal systematic-review implementation.
- Source figures, tables, and supplements **are inputs** when a decision or finding depends on them. Unreadable necessary evidence requires human review.
- Configurable generative models by role. Jev is `typesafe/jev-1.14` via OpenRouter, using a tested typed-decision adapter. Planning research's DeepSeek choice is not the application's generation default.

## Confirmed scientific workflow

1. An advanced lead model interviews the user about a brief.
2. Exploratory seed search, individual seed approval, and repeated discussion until the user advances. Abstract-derived context is provisional.
3. The lead challenges alternative terminology, frameworks, and evidence against the starting hypothesis.
4. Approve one flexible protocol, provisional scope map, Jev rubric, and broad-search plan. Keep eligibility, scientific framework, and manuscript outline distinct. Record scope/date/language/source boundaries and intended synthesis; substantive amendments require impact review.
5. Configurable search agents execute documented queries and citation expansion. A coverage report supports an explicit human decision to search again or freeze the candidate revision. Never promise all papers.
6. Acquire full text or an explicit human disposition for **every** frozen candidate before scientific digestion. Automatic open/authorized sources are allowed. Unofficial acquisition is external/manual followed by import; no automatic SciDownl/Sci-Hub integration.
7. Mandatory human-adjudicated pilot on **one iterative example set**, without a separate held-out set. Important failures block bulk digestion. Change model/instructions or escalate when a small model is inadequate. Material rubric/model/parser changes reopen relevant pilot approval.
8. Jev conservatively classifies original paper text and contextualized passages. Uncertain cases remain. Human-approved exclusion batches require random and risk-based original-source checks; important false exclusions pause the batch.
9. A configurable smaller model extracts from original text: readable summaries plus structured findings, original passages/locators, context, numerical details where relevant, limitations, and attribution. Human bulk-extraction audits examine omissions as well as incorrect findings before synthesis.
10. Folder imports work throughout the project. Exact duplicates, alternate document versions, linked reports of the same study, experiments, and supplements are different cases. New evidence creates an explicit revision and reopens affected approvals without erasing unaffected work.
11. Approve a cross-paper evidence map/synthesis memo comparing independent evidence, applicability, limitations, contradictions, and gaps. Preserve uncertainty; do not pick a winner by publication count, statistical-significance votes, Jev confidence, or model votes.
12. Propose and approve the scientific framework and actual manuscript outline. A shared approved argument record carries definitions, scope constraints, relationships, and open questions.
13. Each section has a persistent planner/writer workspace; approve sources and paragraph blueprints before drafting. Sections may progress independently after global outline approval; all must finish before global review. Approved artifacts, not chat history, are authoritative.
14. Draft from summaries **and linked original evidence**. Scientific assertions require appropriate support. Do not invent data, conditions, citations, or consensus.
15. Different concrete model, separate-context section critic; original drafting model repairs. Maximum **3** critique/repair rounds.
16. Original-source factual checking and repairs: maximum **2** rounds. Preserve source context, numbers, and attribution.
17. Whole-manuscript review: coherence, wording, scientific precision, contradictions, and consequential omissions. Maximum **2** repair rounds. A freshness checkpoint lets the user authorize a top-up or accept the existing cutoff.
18. Human notes get a proposed action plan separating wording, claim changes, and scope/source changes. Apply each approved notes batch once, revalidate affected content, and return to the user.
19. Final read-only reference-origin coverage audit after factual edits, before exact-revision human approval and export. Repairs return to existing authorized stages, not an unlimited terminal edit loop.

## Citation-origin policy

Find direct support for an atomic claim, not the earliest historical discovery. Classify own results/analysis, original conceptual contributions, secondary accounts, mixed, or unclear. Follow claim-relevant in-text citations into resolved bibliography entries and acquired upstream papers. Nearby citations and absence of markers do not prove attribution or originality.

The initially cited source is depth 0. Configure maximum citation hops, new sources per claim/project, and resolution retries; detect cycles. Numerical defaults are **not yet agreed**. These are not the writer's repair budgets.

At a limit or unavailable upstream origin, the user chose **explicit secondary attribution with a visible warning and final human approval**. Cite the source actually read; never substitute an unread original or claim direct verification. This exception does not permit wording unsupported even by the secondary source. Conceptual reviews can originate frameworks; new analyses can directly support their own results even when reusing data.

## Gates and scientific invariants

- Approvals bind to immutable artifact revisions and dependencies, not filenames or generic tool permissions. Agents cannot self-approve.
- Material edits invalidate affected checks. New model-generated draft revisions do not replenish exhausted repair budgets.
- A round is one findings pass plus one repair pass if needed. Final read-only validation adds no repair. Exhaustion escalates; it does not pass unresolved confirmed factual blockers or major scientific defects.
- Findings need evidence or a concrete missing check, severity, and explicit fixed/rejected-with-reason disposition. Minor style findings may remain. Faithful scientific uncertainty is not a defect to erase.
- A summary, real quote, file hash, model probability, or human approval alone does not establish scientific correctness or completeness.
- One iterative pilot and sampled human audits are not held-out accuracy validation or independent dual-human review.
- Separate direct observations, author interpretations, secondary reports, and proposed synthesis. Carry species/population/assay/outcome and causal limitations into conclusions.
- Multiple reports of one dataset are not independent confirmation. Nulls are not automatically refutations. Do not force inappropriate clinical appraisal instruments or a global quality score onto all evidence.
- Treat documents/model outputs as untrusted data. No arbitrary source instructions, authoritative database edits by agents, hidden approvals, public unauthenticated services, or credential leakage.
- Acquisition, storage, redistribution, and hosted-model transfer rights are separate. Uploading a file is not proof of all rights.

## Pending decisions: must be resolved explicitly

Step 01 must record accept/modify/defer decisions on the independent-review recommendations:

- P1: explicit framework contribution, concept-admission/support/revision criteria and testable implications where applicable.
- P2: detailed downstream causal/context-fidelity checks.
- P3: integrity-status checks for retractions, expressions of concern, corrections, and adjudication of affected uses. Do not treat non-replication as retraction or automatically prohibit historical discussion.
- P4: public selection/synthesis and limitations reporting alongside the manuscript.
- P5: AI-use declaration, human accountability, applicable provider terms and journal policies.
- P6: optional target-venue constraint capture and submission-time verification; retain venue-neutral output.

Also decide pilot/audit samples and acceptance rules, issue severities, coverage/stopping rules, cost/concurrency limits, tracing defaults, and explicit acceptance of secondary-origin warnings. These are not permission to redesign already-confirmed choices.

## Scientific anchors

- [Hallmarks 2013, authorized manuscript](https://pmc.ncbi.nlm.nih.gov/articles/PMC3836174/) and [Hallmarks 2023 repository record](https://discovery.ucl.ac.uk/id/eprint/10207541/): scientific writing exemplars, not universal formatting or conduct standards.
- [SANRA](https://pmc.ncbi.nlm.nih.gov/articles/PMC6434870/): appraisal aid, not certification.
- [PRISMA-ScR](https://www.prisma-statement.org/scoping): reporting guidance where appropriate, not a mandatory early profile.
- [Responsible-AI position statement](https://pmc.ncbi.nlm.nih.gov/articles/PMC12603384/): contextual evaluation, oversight, disclosure.
- [Citation distortion](https://www.bmj.com/content/339/bmj.b2680): why attribution matters.
- [ICMJE author AI guidance](https://www.icmje.org/recommendations/browse/artificial-intelligence/ai-use-by-authors.html): verify current target-venue applicability. Restrictions on uploading confidential peer-review manuscripts do not automatically prohibit processing one's own authorized draft.
