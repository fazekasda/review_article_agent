# Step 12 — bounded critique and original-source fact-checking
Status: **locally verified MVP.** Two small, stdlib-only modules plus focused
tests. No live model call, no CLI, no storage migration, no new dependency.
**A different model is not independent human verification.** Full suite: see PR.

## Scope / modules
- `review/schema.py` (pure, JSON-portable): `normalize_finding`, `writer_response`,
  `check_repair`, `fact_check`, `stale_checks`, `evaluate`; `ROUND_LIMITS =
  {"critique": 3, "fact_check": 2}`, `SEVERITIES`, `BLOCKING_SEVERITIES`.
- `review/workflow.py`: `SectionReviewRepository.run/read` — durable, resumable
  review that persists the full state (draft text, findings ledger, checks,
  `rounds_used`, `critic_calls`/`repair_calls`, `status`, `required_human_action`)
  as a new artifact revision after every model call and resumes from the latest
  revision. `critic(draft, package)` / `writer(draft, findings)` are injected
  ports; `critic_model != writer_model` is required. Exported from `review/__init__.py`.

## Acceptance criteria (each pointer is an actual test method)
- AC1 (objection not passed by assertion; basis/missing check; false objections
  rejected): `FalseCriticTests.test_assertion_and_fabricated_objections_rejected_without_repair`;
  `test_review_schema.NormalizeFindingTests` (assertion-only, fabricated, false
  numeric, well-based, missing-check).
- AC2 (repairs preserve qualifiers/numbers/species/citations, else reopen):
  `RepairTests.test_repair_regression_rejected_and_blocked_after_three_rounds`;
  `test_review_schema.CheckRepairTests` (all).
- AC3 (confirmed major blocks; minor may remain; disagreement not forced):
  `RepairTests.test_one_major_repaired_in_round_one_completes`,
  `test_disputed_major_blocks_for_human`;
  `test_review_schema.EvaluateTests.test_minor_style_finding_remaining_is_complete`.
- AC4 (exhaustion → blocked/human action, never auto-pass; revisions do not reset
  budgets): `RepairTests.test_repair_regression_rejected_and_blocked_after_three_rounds`;
  `test_review_schema.EvaluateTests.test_confirmed_major_needs_repair_then_blocked_when_exhausted`.
- AC5 (read-only validation; counts findings/repairs incl. early success):
  `EarlySuccessTests.test_clean_draft_completes_with_one_critic_and_no_repair`,
  `InjectedFactCheckerTests.test_injected_fact_checker_is_actually_called`.
- AC6 (changed source invalidates checks; secondary not primary verified):
  `ChangedPackageTests.test_changed_evidence_passage_invalidates_and_rechecks`,
  `OriginHandoffTests.test_secondary_origin_flag_preserved_and_never_primary_verified`,
  `test_review_schema.FactCheckTests.test_changed_sentence_or_evidence_makes_check_stale`.
- AC7 (state survives interruption without duplicate repairs):
  `RestartTests.test_resume_after_crash_does_not_duplicate_repairs`.

## Handoff items
- **Call-count traces:** workflow tests assert `critic_calls`/`repair_calls`
  (`EarlySuccessTests`, `RepairTests.test_one_major_repaired_in_round_one_completes`, `RestartTests`).
- **False-critic + repair-regression fixtures:** `FalseCriticTests`;
  `RepairTests.test_repair_regression_rejected_and_blocked_after_three_rounds`
  (`qualifier_removed` recorded, finding stays unresolved).
- **Unresolved origin flags for Step 13:** `secondary_support` /
  `origin_flag = unresolved_secondary_origin`, `origin_status =
  pending_origin_tracing` preserved in the final state; `primary_verified` never
  emitted (`OriginHandoffTests`, `test_review_schema.FactCheckTests`).
- **Blocking-state demos:** `RepairTests.test_disputed_major_blocks_for_human`,
  `test_repair_regression_rejected_and_blocked_after_three_rounds`.
- **Not independent human verification:** `engine.independent_human_verification =
  False` + `engine.notice` (`EarlySuccessTests` asserts it).

## Disclosures
- Critic/writer are **injected fake ports** that count calls; no live model,
  network, provider, or OpenCode session call. No CLI is added.
- Rules are **heuristic lexical checks** (qualifier words, numbers, species/
  population terms) over synthetic fixtures; no arbitrary-prose semantics. No
  scientific-correctness validation is claimed.
- **No storage migration / new dependency**; the review state reuses the existing
  generic artifact document envelope.

## Verification
- `PYTHONPATH=src python3 -m unittest tests.test_review_schema tests.test_review_workflow`
  — **36 tests OK** (26 schema + 10 workflow).
- `python3 scripts/check_contract_docs.py` — **15 checks ok, 0 failed**.
- `git diff --check` — clean. Full suite: see PR.
