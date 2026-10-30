# Step 14 — manuscript integration and delivery

Status: **locally verified MVP (S14-T3).** One new stdlib-only module
(`delivery/workflow.py`) plus focused tests, binding the already-integrated pure
Step 14 rules (`delivery/schema.py`, `delivery/export.py`) to durable storage and
the Step 13 read-only `origin_coverage`. No live model/network/CLI call, no
storage migration, no new dependency. `schema.py`/`export.py` behavior is
**unchanged**. Full suite: see PR.

## Scope / modules
- `delivery/schema.py` (pure, pre-existing): `global_review_ready`,
  `inspect_omissions`, `evaluate_global`, `freshness_checkpoint`, `plan_notes`,
  `apply_notes_batch`, `final_approval_manifest`, `approval_current`; constants
  `GLOBAL_ROUND_LIMIT`, `ROUTE_SCOPE`.
- `delivery/export.py` (pure, pre-existing): `cited_keys`, `check_consistency`,
  `render_bibtex`, `render_ris`, `build_disclosures`, `build_export_package`.
- `delivery/workflow.py` (new): `DeliveryRepository(store)` with
  `start_global_review`, `run_global_review`, `freshness`, `notes`, `apply_notes`,
  `revalidated`, `final_approval`, `export`, each persisting the **full state as a
  new immutable artifact revision** and `read(delivery_id)` returning the latest.
  `reviewer`/`writer` are injected ports; `rights(artifact_id, revision_id)`
  authorises each revision.
- `delivery/__init__.py` re-exports the public API of schema/export/workflow.

## Acceptance criteria (each pointer is an actual test method)
- AC1 (global review cannot start while required section checks are incomplete;
  inspect consequential omitted contrary/qualifying evidence, not just cited
  claims): `GateTests.test_incomplete_section_refuses_start_and_persists_nothing`,
  `GateTests.test_incomplete_origin_refuses_start`,
  `GateTests.test_omitted_contrary_evidence_is_surfaced_and_blocks`.
- AC2 (freshness never silently adds papers or loops; the user can accept the
  existing cutoff): `FreshnessTests.test_second_topup_refused`,
  `HappyPathTests.test_complete_example_delivery_and_backup_restore` (the
  `cutoff_accepted` step).
- AC3 (scope/source notes require impact approval, not a forced local edit;
  ambiguous notes require clarification): `NotesTests.test_scope_note_cannot_be_applied_as_edit`
  (plan rules in `tests.test_delivery_schema`).
- AC4 (post-review factual/citation edits trigger support/origin revalidation; no
  final edit bypasses checks):
  `NotesTests.test_citation_edit_blocks_final_approval_until_revalidated`.
- AC5 (final secondary warnings visible and explicitly accepted; unsupported major
  claims cannot be waived):
  `ApprovalAndExportTests.test_unsupported_major_claim_cannot_be_waived`,
  `HappyPathTests.test_complete_example_delivery_and_backup_restore`
  (`secondary_accepted`).
- AC6 (citation keys / bibliography identities / versions / RIS consistent; no
  duplicate-as-independent or invented reference):
  `HappyPathTests.test_complete_example_delivery_and_backup_restore`
  (`@article{k1,` and `ID  - k1`, `consistency.ok`; pure rules in
  `tests.test_delivery_export`).
- AC7 (validate revisions/hashes immediately before export; any later change
  invalidates approval):
  `ApprovalAndExportTests.test_hash_change_after_approval_refuses_export`,
  `ApprovalAndExportTests.test_later_notes_edit_invalidates_approval_and_blocks_export`
  (a change after approval clears `state["approval"]` into
  `invalidated_approvals` and blocks export).
- AC8 (export disclosures only under the ratified contract and venue policy;
  never assert independent validation or submission compliance):
  `HappyPathTests.test_complete_example_delivery_and_backup_restore`
  (`build_disclosures`, venue `not_asserted`; forbidden-phrase guard in
  `tests.test_delivery_export`).

## Handoff items
- **Complete example delivery:**
  `HappyPathTests.test_complete_example_delivery_and_backup_restore` — start →
  global review (one repair round) → accept cutoff → wording notes batch → final
  approval with one accepted secondary warning → export → backup/verify/restore.
- **Actual loop counters observed:** happy path `reviewer_calls=2`,
  `repair_rounds=1`; two-round exhaustion
  (`GlobalReviewLoopTests.test_two_round_exhaustion_blocks`) `reviewer_calls=3`,
  `repair_rounds=2`, status `blocked` with a `required_human_action`; restart
  (`GlobalReviewLoopTests.test_restart_resumes_counters_without_extra_reviewer_calls`)
  resumes from persisted counters with **zero** extra reviewer calls.
- **Final approval manifest:** the happy path asserts `approved is True`,
  `manuscript_revision == "m1"`, and the accepted warning's released status is
  `secondary_accepted` (never `primary_verified`/`verified`).
- **Export checks:** seven files written (`manuscript.md`, `references.bib`,
  `references.ris`, `companion.md`, `summary.md`, `ai_declaration.md`,
  `export_manifest.json`); `checks.consistency.ok`; BibTeX/RIS keys equal the
  manuscript's cited keys; recorded `file_hashes` cover all files. Hashes are
  re-checked by `approval_current` and then `build_export_package` **immediately
  before** writing.
- **Backup/restore evidence:** `create_backup` → `verify_backup` (`ok`) →
  `restore_backup` into a fresh root; the restored store's
  `DeliveryRepository.read(delivery_id)` **equals** the original persisted state.
- **Failure examples:** incomplete section/ origin start refusal; 2-round
  exhaustion → `blocked`; second top-up refused; scope note refused as an edit;
  citation edit → stale blocks approval until `revalidated`; unsupported major
  claim refused; hash change after approval → export refused (no files written).

## Disclosures
- The `reviewer`/`writer` ports and all sources are **injected fakes that count
  calls**; no live model, network, provider, or OpenCode session call. No CLI is
  added and no storage migration is made. `schema.py`/`export.py` are unchanged.
- Detection of omitted contrary/qualifying evidence is a **lexical/structural
  heuristic** (`cited_keys` regex over `[@key]` + the pure `inspect_omissions`);
  the workflow stamps each such finding `status: "open"`, so a consequential
  omission is a real blocking major finding rather than a note.
- **MVP limits:** the persisted state is the latest full revision (there is no
  separate per-call event log); the freshness gate is simplified to requiring
  `cutoff_accepted` before final approval; `revalidated` trusts the caller's fresh
  Step 12/13 results (it does not re-run them); resume is idempotent at the
  operation granularity (a completed/blocked global review is returned unchanged).

## Verification
- `PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_*.py'` — **3639
  tests OK, 7 skips** (all optional pypdf absent) in 1597.8 s.
- `python3 scripts/check_contract_docs.py` — **15 checks ok, 0 failed**.
- `git diff --check` — clean.
- Focused run: `PYTHONPATH=src python3 -m unittest tests.test_delivery_schema tests.test_delivery_export tests.test_delivery_workflow`
  — **59 tests OK** (12 workflow).
