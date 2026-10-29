# Step 11 — section blueprints and evidence-grounded drafting

Status: **PARTIAL — locally verified core; awaiting the final CLI `draft`/`assembly` adapter and the coordinator's combined verification.** Integrated baseline `847c386` (Step 10 merged). Authority: step document `11-drafting.md`; `docs/specification.md` workflow **13–14** (persistent per-section planner/writer workspace; approve sources and paragraph blueprints before drafting; draft from summaries **and** linked original evidence; do not invent data, conditions, citations, or consensus). All source/tests since `847c386` live under `src/review_article_agent/drafting/` and `tests/test_drafting_*`; **no `pyproject.toml` or `storage/` change — no storage migration and no new dependency**. No default model is selected; no live/paid model call, no new dispatch route, and the one-active-project execution boundary is unchanged (a local proposal never executes). **Not ticked**; no acceptance criterion is claimed as fully passed.

## Interfaces, gates, and CLI

- `drafting/schema.py` (pure, JSON-portable): `validate_blueprint`/`validate_draft`; exact schema, forbidden context/scope/final flags, and no model bibliography. `PENDING_CHECKS = ("source_checks","origin_tracing")`. Synthetic builders `drafting_example_package`/`blueprint_example`/`draft_example` are the machine-checked schema example.
- `drafting/workspace.py`: `SectionWorkspaceRepository.propose/read/require_for_drafting`. Pins **existing** app-owned runtime mappings — planner slot = configured `synthesizer` role, writer slot = `writer` role (`PLANNER_ROLE`/`WRITER_ROLE`), each a **distinct** per-section persistent mapping; the concrete model is read from the stored row, never in-band and never defaulted. Per-section exclusivity; historical reads are inspectable but non-authorizing.
- `drafting/package.py`: `DraftingPackageRepository.build(outline_ref, section_id)` reconstructs each finding's own verified passage/source plus readable summary; `citation_key(project_id, candidate_id)` = `cit_` + `sha256` of project+candidate identity only (never title/author/journal/year).
- `drafting/workflow.py`: `DraftingWorkflowRepository.propose/read/require_blueprint/status`; `KINDS = ("blueprint","draft","assembly")`; own gate `artifact_revision`/`drafting.blueprint`; `_render_markdown` emits a `PROVISIONAL` assembly with per-sentence labels and corpus citation keys.
- Payload structure (exact keys): workspace `(outline_ref, section_id, planner_mapping_id, writer_mapping_id)` → blueprint `(workspace_ref, content)` → draft `(blueprint_ref, content)` → assembly `(outline_ref, draft_refs)`.
- `cli/drafting.py` (+ `cli/main.py`): local read/propose only — `drafting propose|show|package|accepted|status`; private-by-default with `--content` opt-in. Only `workspace`/`blueprint` kinds are registered; **`draft`/`assembly` kinds and the assembly command are deliberately pending**.

## Acceptance criteria (each pointer is an actual test method)

- AC1 (draft only with approved blueprint + global framework/outline; unapproved sections don't block): `test_drafting_workflow.test_unapproved_other_section_does_not_block`, `test_generic_artifact_approval_cannot_bypass_own_gate`; `test_drafting_drafts.test_approved_own_section_draft_despite_other_unapproved_section`, `test_unapproved_blueprint_refuses_draft_and_writes_nothing`; `test_drafting_workspace.test_ungated_outline_refused`.
- AC2 (reconstruct from approved artifacts, not raw history): `test_drafting_workspace.test_propose_and_reopen_reconstructs_mapping`, `test_require_for_drafting_returns_approved_context`, `test_old_revision_inspectable_after_global_change`; `test_drafting_workflow.test_reopened_context_equality`; `test_drafting_drafts.test_draft_is_stored_with_its_blueprint_and_package_snapshot`.
- AC3 (summaries + relevant original evidence; methods/context available): `test_drafting_package.test_package_is_reconstructible_and_reopens_identically`, `test_each_finding_gets_its_own_source_and_citation`; `test_drafting_schema.PackageContextTests`.
- AC4 (compound/multi-source/unsupported generalization/direct-vs-secondary/novel proposals): `test_drafting_schema.GroundingTests`, `ContextFidelityTests`, `AttributionTests`, `AttributionFidelityTests`.
- AC5 (citation from verified corpus metadata; no model bibliography/fabricated details): `test_drafting_package.CitationKeyTests`, `MetadataRejectionTests`; `test_drafting_schema.ForbiddenOverrideTests.test_model_supplied_bibliography_is_refused`; `test_drafting_drafts.DraftGroundingTests.test_invented_text_is_rejected`.
- AC6 (shared-argument changes flag affected blueprints/drafts; no silent new definitions/widened scope): `test_drafting_workflow.test_changing_shared_framework_flags_stale_and_refuses_require`, `test_new_blueprint_revision_invalidates_old_approval`; `test_drafting_drafts.test_framework_change_stales_draft_and_history_inspectable`; `test_drafting_schema.ForbiddenOverrideTests.test_context_and_scope_overrides_are_refused`.
- AC7 (provisional; source checks + origin tracing pending; no final export): `test_drafting_schema.test_result_is_provisional_and_not_exportable`, `test_no_claim_is_primary_verified`; `test_drafting_drafts.AssemblyLabelTests.test_provisional_labels_and_corpus_citations`; `PENDING_CHECKS`.

## Context, pinning, citations, and draft constraint

- Context construction uses the **actual verified original passage** bound to each finding's own source key plus the readable summary; a compound claim may not union heterogeneous context (`HeterogeneousContextTests`). Source/secondary/proposed labels are rendered from per-sentence metadata only, never inferred from prose.
- Citation representation is deterministic: `cit_sha256(project_id + candidate_id)`, stable across revisions/sessions/reopenings and distinct across projects; identifiers-only from **verified frozen** corpus metadata (`_bibliographic_record`), never a model bibliography.
- Draft text is constrained to the **exact approved atomic text** (a prose edit requires a blueprint revision/re-approval) to keep the MVP simple. Every history revision is inspectable and stale dependencies are explicit.

## Verification (observed; not a combined run)

- This handoff's own focused run at `55e5a5a` (`PYTHONPATH=src:tests python3 -m unittest test_drafting_schema test_drafting_workspace test_drafting_package test_drafting_workflow test_drafting_drafts test_cli_drafting`) — **126 tests, OK, 0 failures/skips (1072.847s)**.
- Suites recorded by the implementing sessions (overlapping; do not sum): schema+synthesis 90; workspace 24 (102.075s); package+schema 56 (65.705s); CLI-gates+synthesis 20 (92.384s); worker draft+blueprint 31 (716s at `8753f12`); repair targeted 3 (161s at `140c534`).
- Contract checker `python3 scripts/check_contract_docs.py` — **15 checks ok, 0 failed**; `git diff --check` clean. **No CI workflows are configured** (count 0). **Final combined coordinator check is pending and is not invented here.**

## Disclosures

- Inherited **synthetic Step 09 extraction receipt seam** (`extraction.workflow._require_run` patched) and the **controlling-tty seam**; the single real path is the **real-runner / fake-transport** case (`test_drafting_package.DraftingOverRealRunnerTest`, `test_drafting_drafts.DraftOverRealRunnerTest`) — no live transport.
- The workspace suite's planner/writer **mappings are synthetic app-owned fixture rows** (no live OpenCode session); test models/session rows are **fixtures only**, not live observed model execution. No default model is selected.
- A **single-source legacy fixture fallback** binds a legacy receipt passage lacking a per-passage source ref to the case's unique source, disclosed in `limitations`/`source_ref_fallback`; the real path has exact per-passage locators. **No genuine scientific/live validation is claimed.**
