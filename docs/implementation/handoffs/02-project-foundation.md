# Handoff — step 02: CLI, projects, artifacts, and backups

**Status: COMPLETE (local foundation).** Step 02 is **ticked** in
[`docs/implementation/README.md`](../README.md). This step delivers the durable
**local** foundation: a stdlib-only CLI, a path-independent project registry, a
per-project SQLite store with immutable artifact revisions, validated user edits,
non-authorizing local fixture approvals, and a consistent private
backup/verify/restore. It implements **no** agent execution, **no** OpenCode
session, and **no** scientific approval or scientific feature.

**Scope boundary (authority).** The step document
[`docs/implementation/02-project-foundation.md`](../02-project-foundation.md)
(lines 3–26) is the authoritative scope; its acceptance criteria and exclusions
were **not** edited. The accepted policy/technical direction is
[decision 002](../../decisions/002-step01-contracts-and-policies.md); the accepted
runtime language/integration is [decision 001](../../decisions/001-runtime-language.md).
No baseline invariant, acceptance criterion, or policy was weakened.

> **Evidence distinction.** The **coordinator's** full verification was performed on
> the exact reviewed revision `5efa4f2` (see §7 and the optionally recorded log
> `/tmp/opencode/step02-final-checks-pre-handoff.log`; that path is **provenance
> only, with no runtime dependency**). This documentation task re-ran the Python core,
> docs, and script checks on the handoff checkout at the same `5efa4f2` and reports
> its own numbers; the unchanged TypeScript/spike results are used **at the same
> base** and are labelled as such, not re-run here.

## 1. Scope, base revisions, and authority used

- **Step document (authoritative):**
  [`docs/implementation/02-project-foundation.md`](../02-project-foundation.md) —
  deliverables, acceptance criteria (AC1–AC7, lines 16–22), and handoff/exclusions
  (lines 24–26) unedited.
- **Baseline:** [`docs/specification.md`](../../specification.md) (confirmed; not
  weakened). This step implements the "Product and runtime" durable-local-store
  direction (lines 11–16) only.
- **Ratified decisions used:**
  - [`docs/decisions/001-runtime-language.md`](../../decisions/001-runtime-language.md)
    — accepted 2026-10-27: **Python core**, stdlib-only, with a supported,
    explicitly authenticated OpenCode V2 HTTP integration (retained for later steps;
    not exercised here). Approves the runtime language/integration **only**.
  - [`docs/decisions/002-step01-contracts-and-policies.md`](../../decisions/002-step01-contracts-and-policies.md)
    — accepted by the user 2026-10-27 against reviewed revision `ee7f7ea`; records
    the operational/technical direction (ARCH-MIGRATE, ARCH-*, RIGHTS-v1, cost caps).
- **Approved contract versions (representation reviewable):**
  [`C01 identities v1.4`](../../contracts/01-identities.md),
  [`C02 gates-and-runtime v3.2`](../../contracts/02-gates-and-runtime.md),
  [`C03 policy-decisions v1`](../../contracts/03-policy-decisions.md). Exact field
  spellings remain a versioned implementation representation.
- **Step 02 scope base:** `9d0300e`
  (`9d0300e` — merge of `feat/step01-contracts`, PR #3); the accepted Step 01
  end-state.
- **Current reviewed / integrated revision:** `5efa4f2`
  (`5efa4f2e80273a3b36d35155616b404c1963597c`), the integrated tip of the 18
  Step-02 commits above the scope base (T1–T7).
- **This handoff branch:** `task/step02-handoff`, based at `5efa4f2`. It changes
  **documentation only** (see §8).

## 2. Deliverables and public interfaces

### 2.1 Step deliverables → where they live

| Deliverable (step lines 9–12) | Implementation | Contract doc |
| --- | --- | --- |
| CLI entry, config validation, project create/list/select/status, path-independent IDs | [`src/review_article_agent/cli/main.py`](../../../src/review_article_agent/cli/main.py), [`cli/config.py`](../../../src/review_article_agent/cli/config.py) | [`docs/cli.md`](../../cli.md) |
| Versioned migrations, immutable artifact revisions, validated edits, approvals/dependencies, per-project directories | [`storage/schema.py`](../../../src/review_article_agent/storage/schema.py), [`storage/artifacts.py`](../../../src/review_article_agent/storage/artifacts.py), [`storage/approvals.py`](../../../src/review_article_agent/storage/approvals.py), [`storage/paths.py`](../../../src/review_article_agent/storage/paths.py), [`storage/store.py`](../../../src/review_article_agent/storage/store.py) | [`docs/storage.md`](../../storage.md), [`docs/cli.md`](../../cli.md) |
| Consistent backup/export and restore with manifest/schema versions; secrets excluded | [`storage/backup.py`](../../../src/review_article_agent/storage/backup.py) | [`docs/storage.md`](../../storage.md) |
| Fixture projects and interface tests; exact CLI spellings documented | [`tests/`](../../../tests) | [`docs/cli.md`](../../cli.md) |

### 2.2 Public modules and CLI (links)

- CLI contract: [`docs/cli.md`](../../cli.md) — entry points, global options,
  output/exit codes, config, project/artifact/backup/export/fixture commands,
  exclusions, extension hooks.
- Storage contract: [`docs/storage.md`](../../storage.md) — private snapshot API,
  manifest, fail-closed verification, verified atomic restore, compatibility.
- Storage public surface (`review_article_agent.storage`):
  `ProjectStore`, `Registry`, `ProjectInfo`, `ArtifactRepository`, `Revision`,
  `RightsRef`, `DependencyRef`, `ApprovalRepository`, `FixtureApproval`,
  `FixtureApprovalStatus`, `transaction`, plus `storage.paths`, `storage.schema`,
  `storage.backup` (`create_backup`, `verify_backup`, `restore_backup`,
  `BackupError`, `BackupConflictError`).
- CLI entry points (same `main()`): `review-article-agent`,
  `python -m review_article_agent`, `python -m review_article_agent.cli`.

## 3. Storage authority, identity, and versions

### 3.1 Storage authority (per decision 002 ARCH-*)

- **Per project:** one app-owned SQLite store (`store.sqlite3`) under the
  project's `.review_article_agent/` directory. Immutable revision content is
  stored as a SQLite **BLOB** in that store, so a single consistent snapshot
  captures identity, artifacts, revisions, rights records, dependencies, import
  requests, and local fixture approvals together.
- **App level:** a **minimal atomic registry** (`registry.sqlite3`) guarded by a
  Linux **`flock`** (`registry.lock`) so concurrent processes cannot interleave a
  registry mutation. The registry stores project → root mapping and the single
  active selection; it is deliberately minimal.
- **Identity is generated, never derived:** a project id is `prj_` + 32 hex,
  independent of path or Git. Renaming/moving a project is an **explicit**
  relocate/register and never creates a new scientific identity.

### 3.2 Versioned formats

| Format | Version | Where |
| --- | --- | --- |
| Project store schema | **3** | `storage.schema.STORE_SCHEMA_VERSION` |
| Registry schema | **1** | `storage.schema.REGISTRY_SCHEMA_VERSION` |
| CLI config file | **1** | `cli/config.py` (`schema_version`) |
| Private backup format | **1** | `storage.backup` manifest `format_version` |
| Generic content envelope | **1** | `cli/main.py` (`schema_version`, `document_type`, `payload`) |
| Fixture approval snapshot | **1** | `storage/approvals.py` recorded `storage_schema_version` (validated `1..current`) |

The backup manifest additionally records the snapshot's `store_schema_version`
(only `3` supported).

## 4. Acceptance criteria — criterion-by-criterion evidence

Step document lines 16–22. All commands run from the repository root with
`PYTHONPATH=src` unless noted. Every criterion is executed by the **core** suite:

```sh
PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_*.py'
```

| AC | Requirement (short) | Concrete tests (file::Class::test) | Status |
| --- | --- | --- | --- |
| 1 | Two non-Git projects, CLI restart, independent recovery; rename/move explicit, stable identity | `test_cli.py::ProjectFlowTest::test_two_projects_recover_across_restart_and_stay_independent`; `::test_select_is_persisted_and_is_not_execution`; `test_projects.py::ProjectFoundationTest::test_two_projects_persist_and_recover_independently`; `::test_only_one_selected_project`; `::test_explicit_move_preserves_id`; `::test_copy_then_explicit_relocate_refuses_stale_duplicate`; `test_identity_path_safety.py::test_relocate_move_update_failure_rolls_back`; `::test_relocate_cross_device_refused_without_moving`; `::test_idempotent_shortcuts_revalidate_store` | Met |
| 2 | Invalid edits cannot overwrite approved revisions; an approved artifact edited afterward becomes a proposal and invalidates affected checks | `test_artifacts.py::RevisionEditTest::test_invalid_edits_do_not_mutate_earlier_revision`; `::test_edit_creates_new_revision_with_parent`; `ImmutabilityTest::test_raw_revision_update_and_delete_denied`; `test_cli.py::ArtifactFlowTest::test_invalid_edit_cannot_overwrite_current_revision`; `test_approvals.py::StalenessTest::test_target_edit_makes_approval_stale`; `::test_dependency_edit_makes_stale_transitively_and_leaves_unrelated_active`; `::test_reapproval_supersedes_old_without_overwriting`; `::test_historical_rows_are_immutable`; `test_cli_snapshot.py::FixtureLifecycleTest::test_fixture_approval_stales_on_upstream_edit_but_independent_stays_active`; `FixtureCurrentIndicatorTest` | Met |
| 3 | Project id/path mismatch or traversal cannot read/write another project's assets | `test_identity_path_safety.py::IdentityPathSafetyTest::test_foreign_identity_refused_without_mutating_store_bytes_or_journal`; `::test_foreign_identity_refused_for_newer_schema_before_write`; `::test_registry_db_symlink_refused_and_foreign_file_untouched`; `::test_registry_lock_symlink_refused`; `::test_read_only_probe_absent_file_stays_absent`; `::test_no_writable_fallback_on_readonly_open_failure`; `::test_project_id_pattern_has_no_version_specific_escape`; `test_cli.py::DenialTest::test_project_id_traversal_is_denied`; `::test_registry_store_mismatch_is_denied`; `::test_unknown_project_id_is_a_clean_error`; `test_artifacts.py::CrossProjectTest::test_traversal_identifiers_are_rejected` | Met |
| 4 | Duplicate create/import safe; failed transactions leave no half-created projects or dangling approvals | `test_projects.py::test_repeated_create_is_idempotent_and_divergence_conflicts`; `::test_injected_registration_failure_leaves_no_half_store`; `::test_created_root_removed_when_empty_after_failure`; `test_artifacts.py::ImportIdempotencyTest` (all four); `IdempotencyBoundaryTest::test_exact_duplicate_returns_stable_revision_no_new_head`; `::test_conflict_leaves_no_new_rows_or_rights`; `::test_rights_divergence_conflict_leaves_no_rows`; `TransactionFaultTest::test_injected_failure_rolls_back_all_rows`; `::test_failure_leaves_no_dangling_foreign_keys`; `test_approvals.py::TransactionFaultTest::test_injected_failure_leaves_no_half_approval`; `test_cli.py::ArtifactFlowTest::test_duplicate_import_is_stable_and_divergence_conflicts`; `test_migrations.py::test_failed_migration_rolls_back_transactionally` | Met |
| 5 | Restore a consistent snapshot and verify artifact hashes and approvals, including while writes occurred during backup; never copy only the live DB and ignore WAL | `test_step02_acceptance.py::Step02AcceptanceTest::test_committed_wal_write_lands_inside_real_sqlite_backup`; `::test_backup_restore_keeps_exact_old_revision_and_non_authorizing_decision`; `test_backup.py::ConcurrentWriteTest::test_committed_wal_content_is_included`; `::test_concurrent_writes_overlap_backup_and_stay_consistent`; `CreateAndVerifyTest`; `RestoreTest::test_roundtrip_preserves_ids_content_and_approvals`; `FailClosedVerificationTest`; `RepairTrustBoundaryTest`; `test_cli_snapshot.py::BackupRoundTripTest::test_backup_captures_wal_and_restores_into_separate_registry`; `::test_export_verify_is_deterministic` | Met |
| 6 | Unsupported newer schemas fail clearly; migrations tested on fixtures and preserve prior artifacts | `test_migrations.py::test_newer_schema_is_refused`; `::test_migrate_from_empty_sets_version`; `::test_migration_preserves_existing_rows`; `::test_project_store_open_refuses_newer`; `::test_concurrent_applied_migration_is_not_reapplied`; `::test_threaded_dual_migrate_applies_exactly_once`; `::test_registry_refuses_newer_schema`; `test_artifacts.py::MigrationTest::test_migration1_metadata_preserved_across_v2`; `::test_newer_store_schema_refused`; `test_approvals.py::MigrationUpgradeTest::test_migration2_fixture_upgrade3_preserves_bytes_and_refs`; `test_backup.py::FailClosedVerificationTest::test_newer_manifest_version_rejected`; `::test_newer_store_schema_in_manifest_rejected`; `::test_newer_snapshot_store_schema_rejected`; `test_cli.py::ConfigCliTest::test_newer_config_version_is_rejected`; `test_projects.py::test_newer_store_schema_is_refused` | Met |
| 7 | CLI errors actionable with stable non-success exit behavior; no credentials in status/export | `test_cli.py::LeakBoundaryTest` (all five); `StrictJsonCliTest`; `OutputProtectionTest` (all five); `OutputEmitUnicodeTest`; `test_config.py` (all); `test_cli_snapshot.py::BackupSafetyTest::test_corrupt_archive_fails_closed_without_traceback_or_secret`; `test_backup.py::SecretExclusionTest::test_env_and_loose_credentials_never_in_snapshot_or_manifest`; `EntryPointTest` | Met |

### 4.1 The decisive WAL-overlap proof (AC5)

`tests/test_step02_acceptance.py` (commit `8e330e9`, "prove WAL write commits
inside the real SQLite backup API") is stronger than a naive before/after test:

- `test_committed_wal_write_lands_inside_real_sqlite_backup` triggers the
  concurrent writer from **inside the real `sqlite3.Connection.backup` progress
  callback** used by the production `create_backup` path, and requires the writer
  to **commit before the actual `backup()` call returns**
  (`writer_finished_inside_callback` asserted `True`, `remaining_at_trigger > 0`,
  `backup_returned_at_trigger is False`). This is direct evidence the online
  backup observed a real overlapping WAL write, not merely two events in one
  wall-clock window.
- It then verifies both revisions are captured (`counts.revisions == 2`) and,
  after restore, re-reads both BLOBs byte-identically, checks recomputed
  `content_sha256`/`byte_size`, verifies the restored fixture approval's snapshot,
  and asserts the (now stale) decision still `authorizes_execution == False` and
  `authorizes_export == False`.
- `test_backup_restore_keeps_exact_old_revision_and_non_authorizing_decision`
  proves the restore preserves the **exact** pre-edit history (only `rev-1`,
  byte-identical) and that the fixture decision is honest non-authorizing history.

The earlier `test_backup.py::ConcurrentWriteTest` remains, but its writer could
in principle commit after the backup finished; the acceptance test closes that
gap, and `create_backup` refuses an open write transaction and fully verifies the
staged snapshot before publishing.

### 4.2 Immutable edits, dependency/rights pinning, and staleness (AC2)

- Revisions are immutable: raw `UPDATE`/`DELETE` on revisions, dependencies, and
  rights rows are denied by triggers (`test_raw_revision_update_and_delete_denied`,
  `test_raw_dependency_and_rights_update_denied`).
- An edit creates a **new** revision parented on the current head
  (`RevisionEditTest`); a stale/wrong parent is refused; the prior revision is
  byte-unchanged. Every revision projection carries
  `"scientific_state": "proposal"` — Step 02 revisions are unvalidated local
  proposals and no CLI or `fixture` command changes that.
- Right/dependency records pin **exact** `{project_id, artifact_id, revision_id}`;
  cross-project or superseded/unknown/`not_allowed`-storage rights are rejected
  before any bytes persist (`RightsPinningTest`, `RightsRefImportTest`,
  `SupersededRightsTest`, `ExplicitProvenanceTest`, `IdempotencyFingerprintTest`).
- Approvals bind an exact snapshot; a new revision of the approved artifact, of
  any pinned live dependency, or of a pinned rights record makes the approval
  `stale`/`invalidated`; a newer approval for the same gate instance `supersedes`
  the older one (`StalenessTest`, `PinnedStructureTest`, `LineageSnapshotTest`).

### 4.3 Safety boundaries executed by tests

- **Traversal / foreign store untouched (AC3):** a project id/path mismatch or a
  foreign-identity store is refused **before any write**, and the foreign file's
  bytes and journal are asserted unchanged
  (`test_foreign_identity_refused_without_mutating_store_bytes_or_journal`,
  `test_registry_db_symlink_refused_and_foreign_file_untouched`).
- **Idempotency + transaction fault rollback (AC4):** an injected failure leaves
  no half store/approval and no dangling foreign keys; a divergent replay is a
  `conflict` and leaves no new rows; an identical replay is stable.
- **Error categories + secrets + exclusive output (AC7):** stable exit codes
  (`2` usage/validation, `3` conflict, `4` storage, `1` internal) with a curated
  JSON error; untrusted/error-path values and config field names are never echoed;
  imported content/rights/deps are strict JSON (duplicate keys, non-finite, deep
  nesting rejected); `artifact content --output` creates the file exclusively
  (`O_CREAT|O_EXCL|O_NOFOLLOW`, `0600`) and refuses to overwrite or write inside
  the app-owned store.
- **Migration 2→3 preserves prior artifacts / newer refused (AC6):**
  `test_approvals.py::MigrationUpgradeTest::test_migration2_fixture_upgrade3_preserves_bytes_and_refs`
  proves a v2 fixture upgrades to v3 preserving bytes and references; newer
  store/registry/config/backup schemas are refused cleanly.

## 5. Fixture approvals are non-authorizing — ALWAYS

`fixture` records **local storage-fixture review state only**. Every `fixture`
output carries, unconditionally:

- `scope = "storage_fixture"`
- `actor_authenticated = false`
- `authorizes_execution = false`
- `authorizes_export = false`
- `scientific_validation = false`

`gate_kind` is fixed to `fixture_artifact_revision`; real
protocol/pilot/outline/final-release gate kinds are not exposed; no in-band
actor/authority flag is accepted and no imported document can self-grant an
approval (`NoSelfGrantTest`, `RejectionTest::test_real_scientific_gate_kinds_are_not_exposed`,
`test_cli_snapshot.py::FixtureSafetyTest`). `current_revision_fixture_approved`
is a **fixture-only** indicator, true only when the decision is `approved`, the
status is still `active`, the snapshot verifies, and the exact approved revision
is still the artifact head; it is `false` otherwise.

Consequently, for this step: **no scientific approval is operational**, every
artifact revision is `scientific_state = "proposal"`, and there is **no** hosted
transfer, `run`/`worker`/`model`, or scientific processing.

## 6. Explicit claims, limits, and exclusions

### 6.1 What is claimed

- A portable, **local** durable project core with verified backup/restore.
- `export` (an **alias** of `backup create`) produces a **private local storage
  snapshot only** — it is **not** a manuscript export and contains no
  Markdown/BibTeX/RIS (those are pending Step 14). A snapshot result is marked
  `"publishable": false` and `"scientific_validation": false`, and an archive
  **must never be published**.

### 6.2 Honest limitations

- **Secrets:** `.env`, external configuration, the registry, credentials, and
  loose files are excluded **by design** (an allowlist of exactly `manifest.json`
  and `snapshot.sqlite3`). This is withdrawal-by-design, **not** a credential
  scanner: the module **cannot detect** a secret a user manually stored as
  artifact content. Do not store credentials as artifact content.
- **Rights are structural, not legal:** rights-record validation (and every
  backup re-validation) is a structural/integrity check, **not** a legal or
  scientific rights determination.
- **Configuration is minimal:** the optional config file has exactly
  `schema_version` and `data_dir`. Credential/provider/model/budget fields are
  rejected (structurally, without echoing the name). The accepted cost/concurrency
  **policy defaults** — USD **25** cumulative per project, USD **10** cumulative
  per stage, at most **2** simultaneous external-model calls — are *policy*;
  **execution enforcement is future** (Step 03+), and this step makes no external
  application/model calls. All per-project scientific gate values (pilot/audit
  sample sizes, Jev mapping, tracing limits) remain **unset**, never invented.
- **Costs:** none incurred. No paid model/scientific call was made by this step.

### 6.3 Compatibility and recovery limits

- Only the **current store schema (version 3)** archives are supported; a **newer**
  store/manifest schema is refused, and **older** archives are refused rather
  than silently upgraded by this module (2→3 migration is tested separately).
- `relocate --move` is **refused across devices** (the app-owned store is not
  copied across filesystems); cross-device replacement is **not** automatically
  supported.
- Restoring a snapshot whose identity is **already registered at another root in
  the same registry** is a `conflict` — restores **never clone** a project
  identity. A restore into a **fresh** registry preserves the snapshot's exact
  `project_id`; a repeat restore to the same path and identity is a safe no-op.
- Register/relocate use **register-last / rename-last across resources**, which is
  **not** claimed to be power-loss atomic. Recovery is **explicit**, not implicit:
  - an **unregistered, complete orphan** store is never adopted automatically — it
    is recovered with the explicit `register` command;
  - an **interrupted relocation** is completed with an explicit
    `relocate ID ROOT` for the known id;
  - on an exception, only this call's own directories are removed (rollback), and
    the manual cleanup guidance is documented (`docs/storage.md` §6, `docs/cli.md`
    §5) — it never deletes user assets.
- **Exactly one project may be selected/executed at a time** is realized here only
  as the single **active selection**; actual execution arbitration belongs to the
  Step 03 worker and is **not implemented** here.

### 6.4 Excluded / next (Step 03)

- **Not implemented:** the durable background worker, UDS, durable jobs, pause/
  cancel semantics, and restart reconciliation (Step 03,
  [`03-durable-worker.md`](../03-durable-worker.md)); agent roles/model adapters
  (Step 04); and all scientific features. This handoff does **not** implement or
  claim them.

## 7. Verification — commands and results

### 7.1 Coordinator full verification at exact `5efa4f2`

Recorded at `/tmp/opencode/step02-final-checks-pre-handoff.log` (**provenance
only; no runtime dependency**). Environment: Python **3.14.4**, SQLite **3.46.1**,
Node **24.22.1**, TypeScript **5.9.3**. `>=3.11` is declared, but the actual
older-runtime path was not tested (syntax-parsed for 3.11 only).

| Check | Result |
| --- | --- |
| `PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_*.py'` | **355 pass** |
| `python3 -m unittest discover -s scripts -p 'test_*.py'` | **20 pass** |
| spikes/runtime/python | 79 pass |
| spikes/runtime/live-jev | 51 pass (pre-existing `ResourceWarning: unclosed ...report.json` at `test_live_jev.py:507`/`:531`) |
| spikes/runtime/integration | 7 pass |
| `python3 scripts/check_contract_docs.py --self-test` | **29 checks pass** |
| `npm --prefix spikes/runtime/typescript test` | 58 pass |
| `(cd spikes/runtime/typescript && npx tsc --noEmit)` | clean |
| compileall + `git diff --check` | clean |

**Total: 570 tests** (512 Python — 355 core + 79 + 51 + 7 + 20 scripts — and 58
TypeScript), plus **29 additional documentation checks**. **No CI is configured**
(GitHub workflows API returns 0; no `.github/workflow` files). No application
model/service calls and no scientific testing were performed.

### 7.2 Independent re-run on this handoff checkout (documentation-only branch)

Run from the repository root at base `5efa4f2`, Python 3.14.4 / SQLite 3.46.1:

```sh
PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_*.py'
#   Ran 355 tests in 20.599s — OK
python3 -m unittest discover -s scripts -p 'test_*.py'
#   Ran 20 tests — OK
python3 scripts/check_contract_docs.py --self-test
#   checks: 29 ok, 0 failed — OK: Step 01 contract docs are self-consistent
git diff --check
#   (no output, exit 0)
```

The unchanged **TypeScript** (58) and **spike** (79 + 51 + 7) results are reported
**at the same base `5efa4f2`** from §7.1; they were **not** re-run here, and no
TypeScript dependency install was performed in this checkout.

### 7.3 Packaging / install limits (not overclaimed)

- `setuptools 78.1.1` built an **offline wheel**; the console-script entry point,
  package data, and the absence of an invented license are verified by
  `tests/test_cli.py::WheelPackagingTest` (a core test within the 355).
- **`pip`/`ensurepip` are unavailable** in this environment, so a clean `pip
  install` was **not** performed and is **not** claimed.

## 8. Commits, changed files, and next actions

- **Step 02 scope base:** `9d0300e`. **Reviewed/integrated revision:** `5efa4f2`
  (T1–T7, 18 commits).
- **This documentation-only handoff branch** (`task/step02-handoff`, based at
  `5efa4f2`) changes exactly three paths — **no source, test, config, contract,
  baseline, or step-criteria edit**:
  - `docs/implementation/handoffs/02-project-foundation.md` (**new**, this file);
  - `docs/implementation/README.md` — **ticks** the Step 02 line and links this
    handoff (and fixes the stale "Step 02 is next" wording in the Step 01 entry);
  - `README.md` — records the completed local core, the getting-started module
    invocation, and the CLI/storage/handoff links (stale "next" wording fixed).
- **Next actions:** the user owns **PR review and merge**. Completion of this
  step is documentation-complete with the acceptance checks passing locally; the
  coordinator opens the PR **without merging**. The next implementation step is
  **Step 03 — background worker, durable jobs, and recovery**
  ([`03-durable-worker.md`](../03-durable-worker.md)), which is **not**
  implemented here.
