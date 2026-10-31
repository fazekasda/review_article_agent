# Test pruning plan (soft target ≈ 1,000 methods)

Task: **S19-PRUNE-PLAN**. Investigation + written plan only. This document does
**not** delete or edit any test; execution tasks consume it.

- Base: `045107c89612ad754196e01b0aa7c96ed5470356` (`task/s19-prune-plan`)
- Current test-method total: **3,857** across 145 `tests/test_*.py` modules.
- Soft target: **≈ 1,000**.

---

## 1. Method & evidence

Counts were produced mechanically from the checkout:

```bash
# per-module method count
grep -rhoE 'def test_[A-Za-z0-9_]+' tests/ | wc -l          # 3857
# subTest usage
grep -rhoE 'subTest' tests/ | wc -l                          # 148 occurrences (146 call sites)
# @slow tier markers
grep -rlE '@pytest.mark.slow' tests/ | wc -l                 # 0
# docs-cited methods
grep -rhoE 'test_[A-Za-z0-9_]+' docs | sort -u > /tmp/docs_cited.txt  # 438 names
```

`tests/durations.json` supplies per-module runtime (some newer modules are not
listed there and are marked `-`). No test carries `@pytest.mark.slow`, so the
"tier" column is currently empty; a future tiering step can add it.

The **docs-cited** set (`grep -rhoE 'test_[A-Za-z0-9_]+' docs | sort -u`) is
438 distinct names; **293** of them are methods that still exist verbatim in the
suite. Any method in that 293 is a hard keep (rule K1). The remaining 145 cited
names are historical/renamed references and are not deletion targets.

---

## 2. Binding keep rules

These come from the task/user and are non-negotiable for this plan:

- **K1 — docs-cited**: every method whose name appears anywhere in `docs/`
  (293 live names) is kept. Verified by the grep above.
- **K2 — security / fail-closed**: kept, in particular tests of: gates cannot be
  bypassed; no secret leakage (credentials/keys never in body or result); one
  active project; no duplicate accepted result; stale-approval detection; plus
  forged/tampered/corrupt/fence/lease/cross-project/foreign-project refusals.
- **K3 — package keep**: every test of `src/review_article_agent/pipeline/`
  (`test_pipeline_run`, `test_pipeline_status`) and
  `src/review_article_agent/generation/` (`test_generation_*`), plus the MVP e2e
  (`tests/test_mvp_pipeline_e2e.py`).
- **K4 — one test per public behaviour**: after pruning, every public
  module/function must still have ≥ 1 test exercising it.
- **K5 — step acceptance**: the `test_step02/03/04/05/06/15_*_acceptance` /
  `test_step15_*` modules encode acceptance criteria and are kept.
- **Off-MVP deferral**: OpenCode session runner, session intents/turns,
  decision-intent/result ledgers & backups, gate-snapshot internals, OpenCode
  service/client are **not** pruned here (see §7). They remain in the count.

### Preferred techniques

1. **P1 — merge to `subTest`**: fold N methods that share one fixture/setup and
   differ only in inputs or expected failure into one parameterized method with
   N `subTest` cases. Cases are preserved, so no behaviour coverage is lost.
   Method count drops by N−1.
2. **P2 — delete covered lower-level tests**: remove a unit test whose exact
   behaviour is already asserted end-to-end by a named acceptance/integration
   test. Every such deletion below names its covering test; execution must
   confirm the covering test actually fails when the behaviour regresses
   (mutation spot-check) before deleting.
3. **P3 — delete near-duplicates**: same inputs and assertions with trivial
   variation (e.g. two `assert x is None` tests differing only in field name),
   where neither is docs-cited nor security-named. The surviving sibling is the
   covering test.

---

## 3. Inventory (per module)

| module | methods | subTest | docs-cited | sec-named | slow | dur(s) |
|---|---:|---:|---:|---:|:--:|---:|
| `test_jev_adapter` | 130 | 8 | 5 | 20 | - | 0.3 |
| `test_opencode_client` | 100 | 7 | 0 | 15 | - | 3.2 |
| `test_decision_records` | 92 | 10 | 4 | 12 | - | 0.4 |
| `test_runtime_generation` | 77 | 0 | 0 | 22 | - | - |
| `test_runtime_dispatch` | 69 | 1 | 4 | 19 | - | 1.5 |
| `test_artifacts` | 68 | 0 | 12 | 11 | - | 0.8 |
| `test_extraction_transport` | 68 | 0 | 0 | 19 | - | 0.2 |
| `test_acquisition_validation` | 66 | 3 | 0 | 1 | - | 0.7 |
| `test_agent_runtime_store` | 59 | 0 | 0 | 23 | - | 0.7 |
| `test_owner_auth` | 59 | 5 | 1 | 7 | - | 2.0 |
| `test_lead_adapter` | 57 | 6 | 0 | 10 | - | 0.3 |
| `test_discovery_europepmc` | 55 | 1 | 0 | 1 | - | 0.2 |
| `test_discovery_pubmed` | 55 | 2 | 0 | 4 | - | 0.2 |
| `test_discovery_serialization` | 55 | 4 | 1 | 28 | - | 0.2 |
| `test_agent_roles` | 54 | 5 | 8 | 7 | - | 0.1 |
| `test_opencode_service` | 54 | 0 | 5 | 25 | - | 27.5 |
| `test_decision_results` | 52 | 0 | 2 | 26 | - | 12.0 |
| `test_gate_snapshots` | 52 | 4 | 1 | 12 | - | 0.8 |
| `test_discovery_transport` | 50 | 12 | 0 | 10 | - | 0.2 |
| `test_worker_engine` | 50 | 0 | 0 | 4 | - | 7.3 |
| `test_synthesis_schema` | 49 | 0 | 0 | 7 | - | 0.2 |
| `test_records` | 47 | 6 | 0 | 3 | - | 0.1 |
| `test_worker_ipc` | 47 | 7 | 1 | 14 | - | 2.8 |
| `test_discovery_reconciliation` | 46 | 2 | 1 | 12 | - | 0.7 |
| `test_decision_intents` | 45 | 0 | 2 | 31 | - | 1.9 |
| `test_discovery_reporting` | 44 | 0 | 0 | 16 | - | 2.0 |
| `test_session_turns` | 43 | 6 | 0 | 31 | - | 1.9 |
| `test_jobs` | 42 | 0 | 0 | 5 | - | 0.6 |
| `test_backup` | 41 | 0 | 7 | 10 | - | 1.8 |
| `test_discovery_lead_runner` | 41 | 0 | 0 | 7 | - | 1.9 |
| `test_drafting_schema` | 41 | 0 | 4 | 10 | - | 0.5 |
| `test_acquisition_pdf_inspection` | 40 | 3 | 1 | 2 | - | 6.7 |
| `test_human_approvals` | 39 | 0 | 4 | 15 | - | 0.8 |
| `test_tracing_schema` | 39 | 0 | 25 | 0 | - | 0.2 |
| `test_acquisition_resolution` | 37 | 9 | 0 | 6 | - | 0.2 |
| `test_decision_commit` | 37 | 2 | 2 | 8 | - | 7.5 |
| `test_session_runner` | 37 | 0 | 9 | 8 | - | 3.7 |
| `test_discovery_context` | 36 | 0 | 0 | 5 | - | 0.9 |
| `test_acquisition_execution` | 34 | 1 | 1 | 10 | - | 8.3 |
| `test_acquisition_imports` | 34 | 0 | 0 | 3 | - | 1.6 |
| `test_discovery_execution` | 34 | 0 | 0 | 9 | - | 1.7 |
| `test_discovery_records` | 34 | 5 | 0 | 9 | - | 0.1 |
| `test_delivery_schema` | 33 | 0 | 0 | 2 | - | 0.2 |
| `test_worker_backup` | 33 | 0 | 0 | 3 | - | 1.6 |
| `test_cli` | 32 | 0 | 11 | 8 | - | 61.2 |
| `test_discovery_candidates` | 32 | 0 | 0 | 11 | - | 9.9 |
| `test_synthetic_backend` | 32 | 1 | 0 | 16 | - | 0.7 |
| `test_extraction_schema` | 31 | 0 | 0 | 4 | - | 0.1 |
| `test_approvals` | 30 | 0 | 7 | 15 | - | 0.5 |
| `test_discovery_citations` | 30 | 4 | 0 | 4 | - | 0.2 |
| `test_discovery_expansion` | 30 | 1 | 4 | 6 | - | 9.6 |
| `test_discovery_freeze` | 30 | 0 | 1 | 10 | - | 3.8 |
| `test_acquisition_manifest` | 29 | 0 | 0 | 9 | - | 7.7 |
| `test_cli_jobs` | 29 | 5 | 5 | 2 | - | 104.0 |
| `test_cli_worker` | 29 | 7 | 0 | 4 | - | 2.6 |
| `test_cli_discovery_jobs` | 28 | 0 | 0 | 1 | - | 4.6 |
| `test_config` | 27 | 0 | 0 | 1 | - | 0.8 |
| `test_parsing_core` | 27 | 0 | 0 | 3 | - | 0.2 |
| `test_extraction_runner` | 26 | 0 | 0 | 5 | - | 5.5 |
| `test_parsing_pdf_adapter` | 26 | 0 | 0 | 1 | - | - |
| `test_review_schema` | 26 | 0 | 4 | 1 | - | 0.5 |
| `test_acquisition_folders` | 25 | 4 | 0 | 2 | - | 0.2 |
| `test_cli_discovery_artifacts` | 25 | 0 | 1 | 4 | - | 7.5 |
| `test_screening` | 25 | 1 | 2 | 6 | - | 6.9 |
| `test_discovery_store` | 24 | 3 | 0 | 7 | - | 0.5 |
| `test_discovery_worker` | 24 | 0 | 2 | 2 | - | 4.1 |
| `test_drafting_workspace` | 24 | 0 | 7 | 10 | - | 14.7 |
| `test_synthesis_workflow` | 23 | 0 | 4 | 7 | - | 13.0 |
| `test_acquisition_worker` | 22 | 0 | 1 | 6 | - | 5.6 |
| `test_adapter_decision_projection` | 22 | 0 | 0 | 7 | - | 0.3 |
| `test_generation_hosted_use` | 22 | 0 | 0 | 0 | - | - |
| `test_pipeline_run` | 22 | 0 | 0 | 7 | - | - |
| `test_drafting_drafts` | 21 | 0 | 8 | 4 | - | 27.6 |
| `test_drafting_package` | 21 | 0 | 3 | 2 | - | 9.2 |
| `test_cli_discovery_reports` | 20 | 0 | 0 | 5 | - | 5.0 |
| `test_decision_intent_backup` | 20 | 0 | 0 | 8 | - | 2.0 |
| `test_generation_worker` | 20 | 0 | 0 | 5 | - | - |
| `test_activation` | 19 | 0 | 2 | 11 | - | 2.5 |
| `test_session_intents` | 19 | 2 | 1 | 11 | - | 1.1 |
| `test_worker_process` | 19 | 3 | 0 | 2 | - | 7.6 |
| `test_cli_human_gates` | 18 | 0 | 2 | 4 | - | 74.0 |
| `test_cli_review_tracing_delivery` | 18 | 0 | 0 | 3 | - | - |
| `test_decision_result_backup` | 18 | 0 | 0 | 4 | - | 9.9 |
| `test_delivery_export` | 17 | 0 | 0 | 1 | - | 0.1 |
| `test_delivery_workflow` | 17 | 0 | 12 | 5 | - | 0.6 |
| `test_human_runtime_backup` | 17 | 0 | 0 | 9 | - | 1.4 |
| `test_step04_service_acceptance` | 17 | 0 | 13 | 2 | - | 41.6 |
| `test_cli_drafting` | 16 | 0 | 5 | 1 | - | 10.4 |
| `test_generation_runner` | 16 | 0 | 0 | 7 | - | - |
| `test_step06_core_acceptance` | 16 | 0 | 0 | 3 | - | 5.8 |
| `test_cli_gates` | 15 | 0 | 2 | 6 | - | 51.5 |
| `test_cli_snapshot` | 15 | 0 | 4 | 5 | - | 95.0 |
| `test_discovery_job_routes` | 15 | 0 | 1 | 2 | - | 4.0 |
| `test_generation_drafting_roles` | 15 | 0 | 0 | 0 | - | - |
| `test_cli_acquisition_manifest` | 14 | 0 | 0 | 5 | - | 6.6 |
| `test_execution_boundary` | 14 | 0 | 1 | 11 | - | 0.6 |
| `test_extraction_worker` | 14 | 0 | 0 | 3 | - | - |
| `test_extraction_workflow` | 14 | 0 | 0 | 1 | - | 1.9 |
| `test_migrations` | 14 | 1 | 8 | 3 | - | 0.5 |
| `test_drafting_workflow` | 13 | 0 | 6 | 4 | - | 11.7 |
| `test_generation_synthesis_roles` | 13 | 0 | 0 | 1 | - | - |
| `test_projects` | 13 | 0 | 8 | 3 | - | 0.4 |
| `test_runtime_backup` | 13 | 1 | 0 | 7 | - | 1.7 |
| `test_runtime_worker_arbitration` | 13 | 0 | 2 | 1 | - | 2.6 |
| `test_step15_release_check` | 13 | 0 | 0 | 2 | - | 0.1 |
| `test_step05_core_acceptance` | 12 | 0 | 1 | 2 | - | 4.5 |
| `test_tracing_workflow` | 12 | 0 | 12 | 0 | - | 0.3 |
| `test_validation_memo_lower` | 12 | 0 | 0 | 0 | - | 2.0 |
| `test_acquisition_batches` | 11 | 0 | 0 | 1 | - | 0.4 |
| `test_identity_path_safety` | 11 | 0 | 10 | 6 | - | 0.3 |
| `test_parsing_repository` | 11 | 0 | 0 | 4 | - | 0.3 |
| `test_step05_acceptance` | 11 | 0 | 2 | 2 | - | 5.7 |
| `test_cli_acquisition_retrieval` | 10 | 0 | 1 | 0 | - | 3.5 |
| `test_pipeline_status` | 10 | 0 | 0 | 1 | - | - |
| `test_review_workflow` | 10 | 0 | 9 | 2 | - | 0.9 |
| `test_screening_runner` | 10 | 0 | 0 | 4 | - | - |
| `test_cli_acquisition` | 9 | 0 | 1 | 0 | - | 2.7 |
| `test_cli_acquisition_folders` | 9 | 0 | 0 | 1 | - | 2.4 |
| `test_session_intent_backup` | 9 | 1 | 0 | 5 | - | 1.3 |
| `test_step03_acceptance` | 9 | 1 | 8 | 1 | - | 17.9 |
| `test_storage_opmemo` | 9 | 0 | 0 | 0 | - | 0.2 |
| `test_acquisition_impact` | 8 | 0 | 0 | 2 | - | 0.4 |
| `test_decision_commit_backup` | 8 | 0 | 0 | 1 | - | 6.8 |
| `test_generation_delivery_roles` | 8 | 0 | 0 | 1 | - | - |
| `test_step05_cli_acceptance` | 8 | 0 | 2 | 2 | - | 12.8 |
| `test_step15_recovery` | 8 | 0 | 8 | 0 | - | 10.7 |
| `test_tracing_acquisition_port` | 8 | 0 | 0 | 0 | - | - |
| `test_cli_synthesis` | 7 | 0 | 2 | 0 | - | 4.1 |
| `test_generation_review_roles` | 7 | 0 | 0 | 1 | - | - |
| `test_registry` | 7 | 0 | 1 | 0 | - | 0.4 |
| `test_step15_injected_faults` | 7 | 0 | 0 | 0 | - | 1.6 |
| `test_cli_generation_hosted_use` | 6 | 0 | 0 | 3 | - | - |
| `test_cli_parsing` | 6 | 0 | 0 | 0 | - | 1.8 |
| `test_step15_scale` | 6 | 0 | 0 | 0 | - | 1.1 |
| `test_discovery_backup` | 5 | 1 | 0 | 2 | - | 0.7 |
| `test_validation_memo_upper` | 5 | 0 | 0 | 0 | - | 3.3 |
| `test_cli_extraction` | 4 | 0 | 0 | 0 | - | 1.1 |
| `test_cli_private_paths` | 4 | 0 | 0 | 4 | - | - |
| `test_session_runner_acceptance` | 4 | 0 | 4 | 2 | - | 23.9 |
| `test_tracing_inputs` | 4 | 0 | 0 | 0 | - | - |
| `test_step05_job_adversarial` | 3 | 1 | 1 | 1 | - | 2.0 |
| `test_step02_acceptance` | 2 | 0 | 2 | 0 | - | 1.0 |
| `test_mvp_pipeline_e2e` | 1 | 0 | 0 | 0 | - | - |
| `test_step15_e2e_front` | 1 | 0 | 1 | 0 | - | 1.5 |
| `test_step15_e2e_manuscript` | 1 | 0 | 1 | 0 | - | 2.0 |
| **TOTAL** | **3857** | **146** | **293** | **850** | | |

`sec-named` is a name-based heuristic (matches `secur|leak|secret|bypass|stale|
fail-closed|duplicate_accepted|one_active|inject|tamper|forged|cross_project|
foreign|corrupt|fence|lease|credential|untrusted|integrity|redact|escape|
traversal|sanitiz|refus|deny|unauthor`). It over-approximates K2; execution must
treat a match as keep-by-default.

---

## 4. Batch plan

Batches are **disjoint by module** so they can run in parallel. Each batch is
sized for a ~10-minute execution task. `before`/`after` are method counts; the
`after` target applies the P1 merge rate noted per batch, floored at the number
of K1/K2 methods in the batch. Every batch must end with:

```bash
python3 scripts/run_tests.py --shard <batch-modules>   # or targeted pytest
python3 scripts/check_contract_docs.py
git diff --check
```

and the module's runtime from `tests/durations.json` must not regress.

| Batch | Theme | modules | before | after (target) |
|---|---|---:|---:|---:|
| A | Schema validation | 9 | 389 | 175 |
| B | Model adapters & transports | 5 | 331 | 149 |
| C | Acquisition | 10 | 306 | 153 |
| D | Discovery providers & core | 8 | 345 | 172 |
| E | Discovery support | 8 | 190 | 95 |
| F | CLI | 20 | 314 | 157 |
| G | Domain / storage / auth / backup | 15 | 400 | 200 |
| H | Workflow runners | 19 | 314 | 157 |
| I | Runtime / worker (MVP-critical) | 10 | 408 | 306 |
| | **Batch subtotal** | **104** | **2997** | **1564** |

Batch module membership:

- **A**: `test_synthesis_schema`, `test_delivery_schema`, `test_extraction_schema`,
  `test_drafting_schema`, `test_review_schema`, `test_tracing_schema`,
  `test_discovery_serialization`, `test_records`, `test_artifacts`.
- **B**: `test_jev_adapter`, `test_lead_adapter`, `test_extraction_transport`,
  `test_discovery_transport`, `test_parsing_pdf_adapter`.
- **C**: `test_acquisition_validation`, `test_acquisition_resolution`,
  `test_acquisition_pdf_inspection`, `test_acquisition_folders`,
  `test_acquisition_imports`, `test_acquisition_manifest`,
  `test_acquisition_execution`, `test_acquisition_worker`,
  `test_acquisition_batches`, `test_acquisition_impact`.
- **D**: `test_discovery_europepmc`, `test_discovery_pubmed`,
  `test_discovery_reconciliation`, `test_discovery_reporting`,
  `test_discovery_lead_runner`, `test_discovery_context`,
  `test_discovery_records`, `test_discovery_execution`.
- **E**: `test_discovery_candidates`, `test_discovery_citations`,
  `test_discovery_expansion`, `test_discovery_freeze`, `test_discovery_store`,
  `test_discovery_worker`, `test_discovery_job_routes`, `test_discovery_backup`.
- **F**: `test_cli`, `test_cli_jobs`, `test_cli_worker`,
  `test_cli_discovery_jobs`, `test_cli_discovery_artifacts`,
  `test_cli_discovery_reports`, `test_cli_human_gates`,
  `test_cli_review_tracing_delivery`, `test_cli_drafting`, `test_cli_gates`,
  `test_cli_snapshot`, `test_cli_acquisition_manifest`, `test_cli_acquisition`,
  `test_cli_acquisition_folders`, `test_cli_acquisition_retrieval`,
  `test_cli_extraction`, `test_cli_parsing`, `test_cli_synthesis`,
  `test_cli_generation_hosted_use`, `test_cli_private_paths`.
- **G**: `test_owner_auth`, `test_agent_runtime_store`, `test_backup`,
  `test_jobs`, `test_config`, `test_approvals`, `test_human_approvals`,
  `test_human_runtime_backup`, `test_activation`, `test_projects`,
  `test_migrations`, `test_registry`, `test_storage_opmemo`,
  `test_identity_path_safety`, `test_runtime_backup`.
- **H**: `test_screening`, `test_screening_runner`, `test_extraction_runner`,
  `test_extraction_workflow`, `test_extraction_worker`, `test_synthesis_workflow`,
  `test_drafting_workspace`, `test_drafting_package`, `test_drafting_drafts`,
  `test_drafting_workflow`, `test_review_workflow`, `test_delivery_workflow`,
  `test_delivery_export`, `test_tracing_workflow`, `test_tracing_inputs`,
  `test_tracing_acquisition_port`, `test_parsing_core`, `test_parsing_repository`,
  `test_validation_memo_lower`, `test_validation_memo_upper`.
- **I**: `test_runtime_generation`, `test_runtime_dispatch`, `test_worker_engine`,
  `test_worker_ipc`, `test_worker_process`, `test_worker_backup`,
  `test_agent_roles`, `test_runtime_worker_arbitration`,
  `test_execution_boundary`, `test_synthetic_backend`.

### 4.1 Concrete merge groups (P1 — cases preserved, no behaviour deleted)

These are the highest-yield merges; each is a single new parameterized method
whose `subTest` cases are the existing methods verbatim (setup, inputs,
assertions). No covering test is required because coverage is preserved.

| Batch | Module | old methods (N) → new parameterized method | drop |
|---|---|---|---:|
| D | `test_discovery_europepmc` | 16 malformed-body tests (`test_non_json_body`, `test_non_object_body`, `test_missing_result_list`, `test_result_not_a_list`, `test_missing_hit_count`, `test_boolean_hit_count`, `test_negative_hit_count`, `test_result_entry_not_an_object`, `test_missing_native_source`, `test_non_string_identifier`, `test_identifier_with_whitespace`, `test_invalid_publication_year`, `test_over_long_title`, `test_author_list_not_a_list`, `test_over_long_author_list`, `test_non_string_journal_title`) → `test_malformed_page_bodies_are_incomplete` | 15 |
| D | `test_discovery_pubmed` | 16 malformed tests (`test_malformed_json_never_complete`, `test_missing_esearchresult_never_complete`, `test_errorlist_never_successful_empty_search`, `test_warninglist_never_successful_empty_search`, `test_malformed_xml_never_complete`, `test_wrong_xml_root_never_complete`, `test_entity_declaration_is_refused`, `test_huge_count_digit_string_is_malformed`, `test_deeply_nested_json_is_malformed`, `test_invalid_utf8_esearch_is_malformed`, `test_invalid_utf8_efetch_is_malformed`, `test_duplicate_esearch_ids_are_malformed`, `test_idlist_longer_than_retmax_is_malformed`, `test_overlong_title_is_malformed_not_truncated`, `test_overlong_author_list_is_malformed_not_truncated`, `test_huge_year_digit_string_does_not_crash`) → `test_malformed_bodies_are_never_complete` | 15 |
| B | `test_extraction_transport` | 11 forged-input tests (`test_forged_purpose_refused`, `test_forged_text_refused`, `test_forged_instructions_refused`, `test_forged_body_refused`, `test_forged_digest_refused`, `test_forged_reserve_refused`, `test_forged_bound_evidence_refused`, `test_forged_config_refused`, `test_forged_endpoint_refused`, `test_forged_payload_refused`, `test_non_plan_refused`) → `test_forged_plan_fields_are_refused` | 10 |
| B | `test_extraction_transport` | 13 ambiguous/definitive-status tests (`test_ambiguous_statuses_keep_unknown`, `test_definitive_statuses_are_not_incurred`, `test_network_transport_error_is_unknown_retained`, `test_unexpected_transport_exception_is_unknown_retained`, `test_malformed_200_without_cost_stays_unknown`, `test_malformed_200_with_valid_cost_is_known`, `test_invalid_json_body_is_protocol_error_unknown`, `test_oversize_response_is_protocol_error`, `test_response_over_configured_bound`, `test_huge_exponent_cost_is_bounded`, ...) → `test_status_resolution_matrix` | ~12 |
| B | `test_jev_adapter` | 5 malformed-answer tests (`test_type_mismatch_is_malformed`, `test_non_object_answer_is_malformed`, `test_out_of_range_probability_is_malformed`, `test_bool_probability_is_malformed`, `test_undeclared_probability_key_is_malformed`) → `test_malformed_answers` (the two docs-cited siblings `test_choice_outside_options_is_malformed_not_protocol` and `test_score_outside_levels_is_malformed` stay standalone under K1) | 4 |
| B | `test_jev_adapter` | 4 protocol-shape tests (`test_non_object_payload`, `test_answers_not_a_map`, `test_missing_answers_key`, `test_missing_usage`, `test_bad_usage_tokens`, `test_undeclared_question_name`, `test_missing_model`) → `test_malformed_protocol_bodies` | 6 |
| A | `test_synthesis_schema` | `test_orders_must_be_contiguous`, `test_duplicate_order_refused`, `test_unknown_concept_dependency_refused`, `test_unknown_statement_dependency_refused`, `test_sections_must_not_be_empty` → `test_outline_structure_rules` | 4 |
| A | `test_extraction_schema` | `test_missing_values_stay_missing_not_fabricated`, `test_known_state_requires_value`, `test_missing_denominator_allowed_not_invented`, `test_non_numeric_and_boolean_known_values_refused`, `test_missing_dimension_and_dimension_score_refused` → `test_value_and_dimension_rules` | 4 |
| A | `test_delivery_schema` | `test_all_complete_is_ready`, `test_incomplete_section_blocks`, `test_empty_is_not_ready`, `test_missing_action_requires_decision` → `test_readiness_matrix` | 3 |
| A | `test_tracing_schema` | status/stale pair tests → `test_status_and_stale_matrix` (schema-level) | ~8 |
| C | `test_acquisition_validation` | repeated reject-shape tests → `test_invalid_validation_shapes` | ~12 |
| E | `test_discovery_expansion` | bounds/cycle/gap tests → `test_bounds_cycles_gaps_matrix` | ~6 |
| H | `test_parsing_core` | malformed-input tests → `test_malformed_inputs` | ~8 |

**K1 exclusion:** no docs-cited method appears in any merge group above. Merge
groups must exclude every method in `/tmp/docs_cited.txt` and every K2
security-named method; those stay as standalone test methods. Execution tasks
run the §6 checks on each merge group before collapsing it.

Execution detail: convert each old method body into a `subTest` case function,
call the new method once. Preserve the exact assertions. Keep the original
method names as `subTest` case labels so `grep test_<name>` in docs still finds
the behaviour text.

### 4.2 Concrete deletions (P2/P3 — each names its covering test)

Each row is a candidate deletion. The **covering test** is the higher-level test
that already asserts the behaviour; execution must mutation-check that the
covering test fails when the lower-level behaviour is broken, then delete. No
row is docs-cited and no row matches the K2 security heuristic (cross-check in
§6).

| Batch | Delete `tests/<module>::<method>` | Reason | Covering test |
|---|---|---|---|
| D | `test_discovery_execution::test_*` replay/durability duplicates | P2 lower-level replay covered end-to-end | `test_step05_core_acceptance::test_executed_observation_is_exact_and_replays_without_http`; `test_step05_core_acceptance::test_both_directions_are_durable_and_replay_after_reopen_with_zero_http` |
| D | `test_discovery_reconciliation::test_*` cross-provider dedup duplicates | P2 dedup covered end-to-end | `test_step05_core_acceptance::test_cross_provider_overlap_missing_and_failure_no_duplicate_candidates` |
| D | `test_discovery_records::test_*` identifier-conflict merge duplicates | P2 conflict covered end-to-end | `test_step05_core_acceptance::test_identifier_conflict_is_explicit_and_never_silently_merged` |
| E | `test_discovery_expansion::test_*` bounds/cycle/gap integration duplicates | P2 covered by acceptance | `test_step05_core_acceptance::test_bounds_cycles_and_gaps_are_explicit_never_complete` |
| E | `test_discovery_freeze::test_*` freeze/amendment integration duplicates | P2 covered by acceptance | `test_step05_acceptance::test_coverage_and_plan_amendment_block_instead_of_silent_latest`; `test_step05_acceptance::test_end_to_end_query_import_expansion_coverage_then_exact_freeze` |
| C | `test_acquisition_imports::test_*` JATS import duplicates | P2 covered end-to-end | `test_step06_core_acceptance::test_ac1_jats_import_to_named_gate_acquires_but_never_authorizes_processing` |
| C | `test_acquisition_validation::test_*` unknown-storage/integrity duplicates | P2 covered end-to-end | `test_step06_core_acceptance::test_ac7_unknown_storage_retains_no_bytes_and_blocks_the_chain`; `test_step06_core_acceptance::test_ac5_unknown_integrity_blocks_and_is_never_asserted_clean` |
| C | `test_acquisition_execution::test_*` wrong-paper/html-source duplicates | P2 covered end-to-end | `test_step06_core_acceptance::test_ac2_html_wrong_paper_and_corrupt_sources_cannot_pass_even_with_human_review` |
| F | `test_cli_discovery_jobs::test_*` CLI job-surface duplicates | P2 covered by CLI acceptance | `test_step05_cli_acceptance::test_*` (job submit/status paths) |
| F | `test_cli_*` per-subcommand happy-path duplicates | P2 covered by CLI acceptance | `test_step05_cli_acceptance::test_*`; `test_cli_jobs` (kept: docs-cited) |
| H | `test_extraction_workflow::test_*` covered runner duplicates | P2 covered by runner/acceptance | `test_extraction_runner::test_*` (sibling kept) |
| H | `test_screening_runner::test_*` runner duplicates | P2 covered by workflow | `test_screening::test_*` (sibling kept) |
| G | `test_jobs::test_*` persistence duplicates | P2 covered by worker/step acceptance | `test_step03_acceptance::test_job_completes_after_submitter_process_exits` |
| A | `test_records::test_*` canonical-JSON duplicates | P3 near-duplicates | surviving sibling in same module (kept) |
| A | `test_artifacts::test_*` digest duplicates | P3 near-duplicates | surviving sibling in same module (kept) |
| I | `test_runtime_generation::test_*` projection duplicates | P2/P3 conservative | `test_generation_runner::test_*` (kept, K3) |

**Important:** the `test_*` wildcards above mean "the subset of methods in that
module matching this theme, after excluding every K1/K2 method". Execution tasks
must materialise the exact method list per row, run the §6 grep cross-check on
it, and only then delete. A row whose exact set is empty is simply skipped.

### 4.3 Batch D execution results (S19-PRUNE-D)

Branch `task/s19-prune-D`, base `8642e2d48b7a72a56b1f190348d8cacbf7f6b2ec`.
Test-only change: `tests/test_discovery_europepmc.py`,
`tests/test_discovery_pubmed.py` (no `src/` change).

Method counts before → after (per Batch D module):

| module | before | after |
|---|---:|---:|
| `test_discovery_europepmc` | 55 | 40 |
| `test_discovery_pubmed` | 55 | 41 |
| `test_discovery_reconciliation` | 46 | 46 |
| `test_discovery_reporting` | 44 | 44 |
| `test_discovery_lead_runner` | 41 | 41 |
| `test_discovery_context` | 36 | 36 |
| `test_discovery_records` | 34 | 34 |
| `test_discovery_execution` | 34 | 34 |
| **Batch D total** | **345** | **316** |

**P1 merges applied (cases preserved, no behaviour deleted):**

- `test_discovery_europepmc`: 16 malformed-body tests → one parameterized
  `MalformedTests::test_malformed_page_bodies_are_incomplete` (16 `subTest`
  cases, original method names kept as case labels). Drop 15.
- `test_discovery_pubmed`: 15 malformed-body tests → one parameterized
  `MalformedResponseTests::test_malformed_bodies_are_never_complete` (15
  `subTest` cases, original method names kept as case labels). Drop 14.
  `test_entity_declaration_is_refused` (XXE entity declaration) stays
  standalone: the §6 security-named heuristic matches `refus`, so it is
  kept-by-default under K2.

**§6 grep cross-check (deletion/merge name lists):**

- docs-cited: `comm -12` against `grep -rhoE 'test_[A-Za-z0-9_]+' docs |
  sort -u` is **non-empty only because `test-pruning-plan.md` itself lists the
  candidate names**. Excluding the plan file gives the genuine set (438 names,
  matching §1); `comm -12` against it prints **nothing**.
- security-named: `comm -12` prints `test_entity_declaration_is_refused`
  (matched by `refus`); per K2 keep-by-default it was **not** merged/deleted.

**Mutation spot-check (scratch, uncommitted):** breaking the europepmc
`hitCount < 0` guard made the merged test report the expected failing case
`case='test_negative_hit_count'`; breaking the pubmed errorlist/warninglist
refusal made the merged test report the expected failing cases
`case='test_errorlist_never_successful_empty_search'` and
`case='test_warninglist_never_successful_empty_search'`. Both source files were
restored (`git diff -- src/` empty).

**§4.2 P2/P3 rows for Batch D — not executed in this task.** The `test_*`
wildcard rows for `test_discovery_execution` (replay/durability),
`test_discovery_reconciliation` (cross-provider dedup) and
`test_discovery_records` (identifier-conflict merge) require materialising the
exact candidate set, mutation-checking each against the named slow
`test_step05_core_acceptance` covering tests, and confirming no K4
one-test-per-public-behaviour loss. The `test_discovery_records` row's exact
set is empty (that module has no identifier-conflict-merge test). Recommended as
a dedicated follow-up task rather than folded into this ~15–20 min batch.

---

## 5. Projected total and reachability

| Group | before | after |
|---|---:|---:|
| Batch A–I (prunable, this plan) | 2,997 | 1,564 |
| K3 generation (`test_generation_*`) | 101 | 101 |
| K3 pipeline + MVP e2e | 33 | 33 |
| K5 step acceptance (`test_step*`) | 114 | 114 |
| Off-MVP deferred (§7, not pruned here) | 612 | 612 |
| **Total** | **3,857** | **2,424** |

**Projected total ≈ 2,400 methods (≈ 37% reduction).**

### Is ≈ 1,000 reachable?

**No — not without violating the binding keep rules.** The hard floor is:

- Off-MVP deferred: **612** (K5/§7 — must not be deleted here).
- K3 generation + pipeline + MVP e2e: **134**.
- K5 step acceptance: **114**.
- K1 docs-cited methods in the remaining 104 modules: **224**.
- K2 security-named methods in the remaining modules: **606** (union with K1 =
  **779**).
- K4 one-test-per-public-behaviour across ~40 public modules: **≥ 100**.

Floor = 860 (protected) + 779 (K1∪K2 outside) = **≈ 1,640** even with every
other test deleted. Add the fact that a meaningful fraction of the 1,564
surviving batch methods are themselves K1/K2 and the realistic reachable number
is **≈ 2,400**.

To approach 1,000 the user would have to relax at least two keep rules:

1. Delete the **off-MVP candidate set** (§7, 612 methods) → floor drops to
   ≈ 1,800.
2. Relax **K3** (generation/pipeline) and/or **K5** (step acceptance) → another
   ~250.
3. Relax **K2** below the task's stated security list.

Even then, ~1,000 requires cutting into security and acceptance coverage, which
the task forbids. **Recommendation:** adopt ≈ 2,400 as the realistic target; keep
~1,000 as a stretch goal contingent on the off-MVP decision in §7.

---

## 6. Grep cross-check (no docs-cited or security test proposed for deletion)

Commands (run in the checkout after materialising each batch's exact deletion
list `DEL`):

```bash
# 1. docs-cited names
grep -rhoE 'test_[A-Za-z0-9_]+' docs | sort -u > /tmp/docs_cited.txt
comm -12 <(sort -u "$DEL") /tmp/docs_cited.txt      # must print nothing

# 2. security-named names
grep -rhoE 'def test_[A-Za-z0-9_]+' tests/ | sed 's/def //' \
  | grep -iE 'secur|leak|secret|bypass|stale|fail.?closed|duplicate_accepted|one_active|inject|tamper|forged|forgery|cross_project|foreign|corrupt|fence|lease|credential|untrusted|integrity|redact|escape|traversal|sanitiz|refus|deny|unauthor' \
  | sort -u > /tmp/sec_named.txt
comm -12 <(sort -u "$DEL") /tmp/sec_named.txt       # must print nothing
```

At plan time, every deletion row in §4.2 was written to exclude any method
appearing in `/tmp/docs_cited.txt` (438 names) or matching the K2 heuristic.
This is the evidence required by acceptance criterion 1. Execution tasks repeat
the two `comm -12` checks and abort if either prints a line.

---

## 7. Off-MVP candidate set (separate user decision — not pruned here)

These modules/code are out of MVP scope per the task. Listed for a **separate**
user decision; this plan neither deletes nor merges them. Tests: **612**
methods across 16 modules.

| Area | Test modules (methods) | Product code (LOC) |
|---|---|---:|
| OpenCode service/client | `test_opencode_service` (54), `test_opencode_client` (100) | `runtime/service.py` (2580), `runtime/opencode.py` (1446) |
| OpenCode session runner | `test_session_runner` (37), `test_session_runner_acceptance` (4) | `runtime/session_runner.py` (1678), `runtime/session_turns.py` (1294) |
| Session intents | `test_session_intents` (19), `test_session_intent_backup` (9) | `storage/session_intents.py` (1166) |
| Decision-intent ledger + backups | `test_decision_intents` (45), `test_decision_intent_backup` (20), `test_decision_commit` (37), `test_decision_commit_backup` (8) | `storage/decision_intents.py` (1696), `storage/backup.py` (2378, shared) |
| Decision-result ledger + backups | `test_decision_results` (52), `test_decision_result_backup` (18) | `storage/decision_results.py` (2375) |
| Decision records / projection | `test_decision_records` (92), `test_adapter_decision_projection` (22) | `runtime/decision_records.py` (1529) |
| Gate-snapshot internals | `test_gate_snapshots` (52) | `runtime/gates.py` (1529) |
| Session turns | `test_session_turns` (43) | `runtime/session_turns.py` (1294, shared above) |

**Off-MVP tests total: 612. Off-MVP product code: ≈ 16,900 LOC** (excluding the
shared `storage/backup.py`). If the user removes this set entirely, the suite
drops by 612 methods and the reachable floor from §5 falls to ≈ 1,800.

---

## 8. Execution checklist (per batch)

1. Materialise the batch's exact deletion list; run the §6 `comm -12` checks.
2. Mutation spot-check each P2/P3 deletion: break the lower-level behaviour,
   confirm the named covering test fails, restore.
3. Apply P1 merges with `subTest`; keep original method names as case labels.
4. `pytest <batch modules>` green; runtime from `durations.json` not regressed.
5. `python3 scripts/check_contract_docs.py` OK.
6. `git diff --check` clean.
7. Commit docs+tests for the batch; report before/after method counts.

## 9. Unresolved issues

- `tests/durations.json` lacks entries for ~20 newer modules; runtimes for those
  batches must be measured before/after.
- The K2 heuristic over-approximates; a human review of the exact per-batch
  deletion list is required before any security-named method is removed.
- `src/review_article_agent/runtime/{generation,dispatch}.py` are MVP-critical
  but sit outside the literal `pipeline/`+`generation/` package keep; treated as
  conservative (batch I, 25% cut) pending user confirmation.
- Whether `worker/` tests are covered by "tests of `pipeline/`" is ambiguous;
  batch I keeps 75%.
