# Storage contract — private local backup, verify, and restore

Scope: the **private local storage snapshot and restore** contract implemented by
[`src/review_article_agent/storage/backup.py`](../src/review_article_agent/storage/backup.py)
for the Step 02 deliverable *"consistent backup/export and restore with
manifest/schema versions; secrets excluded"*
([`02-project-foundation.md`](implementation/02-project-foundation.md) deliverable
line 11, acceptance line 20). It records only the backup/storage interface and
behavior; it makes no scientific claim and decides no policy.

Related authority: the accepted `ARCH-MIGRATE` direction
([`03-policy-decisions.md` §ARCH-MIGRATE](contracts/03-policy-decisions.md)) —
versioned migrations with manifest/schema versions, a consistent backup/restore
that captures WAL state and is verified **including while writes occur during
backup**, unsupported newer schemas failing clearly, and restore verifying
artifact hashes and approvals. ADR 002 records the acceptance
([`002-step01-contracts-and-policies.md`](decisions/002-step01-contracts-and-policies.md)).

## 1. What a backup is, and is not

A backup is a **private local storage snapshot** of exactly one project's SQLite
store plus a manifest.

- It **is**: a consistent, transactionally captured copy of all project data
  (identity, artifacts, immutable revisions with their BLOB content, rights
  records, dependencies, import requests, local fixture approvals, and — under
  store schema 4 — the durable synthetic job/attempt/accepted-result/budget rows),
  suitable for restoring that project later.
- It **is not**: a manuscript export, a redistribution package, or a portable
  interchange format. **Never publish an archive** and **never store credentials
  as artifact content**. Private storage backup is distinct from manuscript
  export/redistribution rights.

Mechanism: a backup is produced with SQLite's **online backup API**
(`sqlite3.Connection.backup`) into a fresh, staged snapshot database. The live
database file and its `-wal`/`-shm` sidecars are **never** copied directly, so
committed WAL content is captured and a concurrent writer cannot produce a torn
backup. Because immutable content is stored as a SQLite BLOB, the snapshot
captures all data in one consistent database. The snapshot is converted to a
self-contained rollback-journal database; a conversion that does not take, or a
leftover sidecar, is a hard failure rather than a silently shipped WAL.

`create_backup` refuses early if the source connection has an **open write
transaction** (an online backup started inside the caller's transaction can
block indefinitely), and it **fully verifies the staged backup before publishing
it**, so a corrupt source never yields a valid-looking backup at the destination.

The manifest is derived **from the closed snapshot**, never from live state, so
the digests it records describe exactly the captured bytes.

## 2. Snapshot contents (allowlist)

A backup directory contains **only** these two allowlisted members:

| Member | Meaning |
| --- | --- |
| `manifest.json` | versioned manifest (`format_version` 1) |
| `snapshot.sqlite3` | the closed, self-contained snapshot database |

The project folder, `.env`, external configuration, the application registry,
provider credentials, and any loose files are **never** part of a snapshot. The
application never scans for or automatically ingests secrets; the guarantee is
withdrawal-by-design (only allowlisted members are written), **not** a general
credential scanner.

The snapshot database must match the canonical project schema for its recorded
store schema version **exactly**: the full set of tables, indices, triggers, and
views — including foreign keys and the immutability triggers — is compared against
the schema the canonical migrations generate **for that version** in memory. Two
store schema versions are supported: **3** (identity, artifacts, revisions,
rights, dependencies, import requests, and local fixture approvals) and **4**
(which adds the durable worker's synthetic job/attempt/accepted-result/budget
tables). Any other ("external") table, a missing or altered object, or a hidden
added column is refused, so a forged database that merely reuses the right table
names (and would pass `foreign_key_check`) is never accepted.

All output is owner-private: the backup directory is `0o700`, and
`manifest.json`/`snapshot.sqlite3` are `0o600`.

## 3. Manifest

`manifest.json` is canonical JSON with these fields:

| Field | Meaning |
| --- | --- |
| `format` | `review_article_agent.private_storage_backup` (foreign-JSON guard) |
| `format_version` | `1` (the only supported version) |
| `created_at_utc`, `app_version`, `project_id` | provenance and stable identity |
| `store_schema_version` | the snapshot's store schema (`3` or `4` are supported) |
| `database` | `{filename, sha256, byte_size}` of `snapshot.sqlite3` |
| `counts` | row counts for artifacts/revisions/rights/dependencies/imports/approvals/approval entries |
| `artifact_digest`, `revision_digest`, `rights_digest` | deterministic aggregate digests over identity + integrity columns |
| `approvals` | per-approval `{approval_id, snapshot_digest, entry_count}` |
| `notice` | the non-claim reminder (private snapshot; not an export) |

## 4. Public API

```python
from review_article_agent.storage.backup import (
    create_backup,      # (store: ProjectStore, destination) -> dict (JSON-safe summary)
    verify_backup,      # (backup_dir) -> dict (JSON-safe summary); raises BackupError
    restore_backup,     # (backup_dir, root, registry: Registry) -> ProjectInfo
    BackupError,        # subclass of review_article_agent.domain.errors.StoreError
    BackupConflictError,# subclass of BackupError
)
```

`create_backup` requires `destination` to not already exist. `verify_backup`
opens the supplied backup **read-only** and never modifies it. `restore_backup`
fully verifies the backup before creating anything, then stages and
register-last.

## 5. `verify_backup` — fail-closed

`verify_backup` raises `BackupError` (or `UnsupportedSchemaVersion` for a
newer manifest/store schema) on any of:

- a symlinked backup directory, member, or sidecar; an unexpected member
  (including a `-wal`/`-shm` sidecar); a non-file member;
- an invalid/unrecognised manifest (including duplicate JSON keys, non-finite
  constants, over-deep nesting, or invalid UTF-8 — rejected without echoing any
  untrusted key or value), a missing field, an unsafe or traversing database
  filename, a newer `format_version`, or a newer/unsupported
  `store_schema_version`;
- an identity mismatch between the manifest and the snapshot; a failed
  `PRAGMA integrity_check` or non-empty `PRAGMA foreign_key_check`;
- a snapshot schema that is not byte-for-byte the canonical definition for its
  recorded version (a missing/altered trigger or constraint, an added hidden
  column, or an external table);
- a **recomputed** revision/rights BLOB digest or byte size that does not match
  the stored column;
- a rights record whose canonical body does not equal its denormalized identity
  and subject columns, that is not a fully valid rights record, or that carries a
  cross-project key;
- a revision that does not pin a rights record whose subject binds **this exact
  revision** (same project, artifact, and revision), or whose pinned rights
  record does not permit `storage` (metadata-only / pre-acquisition records never
  authorize content bytes);
- a dangling or cross-project dependency/import-request reference, or a revision
  whose artifact or parent does not exist;
- a local fixture approval that is not the non-authorizing constant, or whose
  exact snapshot (digest, entry digest/size/rights, recorded schema) does not
  verify — including valid historical `stale`/`superseded` rows, which are never
  rejected as honest history;
- a mismatch between the snapshot and any manifest expected value (database
  hash/size, counts, aggregate digests, or per-approval snapshots);
- **schema-4 only** — a durable job/attempt/result/budget row that does not verify
  read-only: a cross-project key, a `job_id` that does not derive from its
  `(project_id, request_id)` key, a parameter or accepted-result fingerprint that
  does not match the stored canonical JSON, a stale/foreign accepted result that
  is not the job's current attempt and fence, an illegal job/attempt state or
  outcome flag, tampered attempt counters or `current_fence`, a malformed
  checkpoint, a bounded-exact-decimal budget/reserve violation, a duplicate
  reserve for one attempt, a reserve whose attempt belongs to another job, or a
  dangling reserve/attempt reference.

Because verification **recomputes every inner digest**, rewriting the outer
`database.sha256` in the manifest does not bypass inner validation.

## 6. `restore_backup` — verified atomic restore

1. **Validate first.** `verify_backup` runs in full before anything is created;
   a corrupt backup is rejected with the target root untouched.
2. **Conflict checks (no modifications).** Restoring where the same project is
   already registered at another root is refused (never a clone); restoring into
   a root registered to a different project is refused. A repeat restore to the
   same path and identity is a safe no-op.
3. **No overwrite.** An existing unregistered app-owned directory is refused
   rather than overwritten; unrelated files in the target root are preserved.
4. **Stage, then register last.** The snapshot is copied into an owner-private
   staging directory inside the target root. The staged copy's hash and byte size
   are re-checked against the verified snapshot digest (closing the window in
   which the source could change between verification and copy); on a mismatch the
   restore is refused and its staging removed. The staging directory is renamed
   atomically into the app-owned directory, and only then registered. On any
   failure, only this call's own directories are removed (rollback), leaving no
   half registry row and no stale approvals.

The restored project keeps the snapshot's exact `project_id`; all exact IDs and
immutable historical approval snapshots are preserved. A new scientific identity
is never created.

A **schema-3** snapshot is restored as-is; the ordinary `ProjectStore` open that
runs while the restore is registered migrates it `3 -> 4` (adding the empty
job/attempt/budget tables) and preserves every prior artifact byte and fixture
approval snapshot exactly. A **schema-4** snapshot is restored with its completed
job results, unresolved attempts, and budget reserves preserved exactly.

**Restore proves storage integrity, not remote quiescence.** A restored snapshot
records admitted/ambiguous durable attempts but is **not** proof that any external
operation has quiesced: no app-level execution activation and no synthetic
external provider journal is part of a per-project snapshot. A worker that opens a
restored project MUST reconcile or hold every restored `admitted`/`ambiguous`
attempt before dispatching new work or switching the active project; that
reconciliation is the engine's responsibility and is out of scope for this
module. A restore never fabricates a scientific or model approval.

## 7. Compatibility and limitations

- The supported store schema versions are **3 and 4**. A newer store schema is
  refused clearly; the `2 -> 3` and `3 -> 4` migrations are tested separately and
  are not part of this module (a schema-3 snapshot is upgraded to schema 4 by the
  ordinary `ProjectStore` open on restore). A fixture approval's *recorded*
  `storage_schema_version` is validated against the supported range (`1..current`),
  so a historical version is preserved where supported while an
  arbitrary/unknown version is refused. Determining the version-specific canonical
  schema and the job-row validation both reuse the canonical migration and
  `runtime.jobs` definitions, so a schema change cannot silently desynchronise
  verification.
- `verify_backup` re-validates stored rights records and recomputes content
  digests; for a schema-4 snapshot it additionally re-validates the durable
  job/attempt/result/budget rows. It is a structural/integrity check, not a
  scientific, rights, or remote-quiescence decision.
- The application never automatically ingests secrets; this module cannot prove
  the absence of any secret a user manually stored as artifact content. Do not
  store credentials as artifact content.
