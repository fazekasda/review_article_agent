# Storage contract — private local backup, verify, and restore

Scope: the **private local storage snapshot and restore** contract implemented by
[`src/review_article_agent/storage/backup.py`](../src/review_article_agent/storage/backup.py)
for the Step 02 deliverable *"consistent backup/export and restore with
manifest/schema versions; secrets excluded"*
([`02-project-foundation.md`](implementation/02-project-foundation.md) deliverable
line 11, acceptance line 20). It records only the backup/storage interface and
behavior; it makes no scientific claim and decides no policy.

The same snapshot contract now covers the **additive** store schemas introduced
by the Step 03 synthetic worker and the Step 04 durable runtime/human-approval
slices: schema 4 (jobs/attempts/results/budgets), schema 5 (runtime
configuration/session/task/proposal mappings), schema 6 (durable runtime dispatch
and cost ledger), schema 7 (out-of-band human approvals and their exact
snapshots), and schema 8 (the immutable pre-dispatch Jev decision request intent).
The extension is strictly additive — migrations 1-7 are never rewritten — and the
manifest `format_version` stays `1`. Schema 9 (result observation) is **not** part
of this contract yet and its fields are not asserted here.

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
  records, dependencies, import requests, local fixture approvals, the durable
  synthetic job/attempt/accepted-result/budget rows under store schema 4, the
  durable runtime configuration/session/task/proposal mappings under schema 5,
  the durable runtime dispatch identity/operational/cost ledger under schema 6,
  the exact named-gate human-approval/evidence/entry rows under schema 7, and the
  immutable pre-dispatch decision request intent rows under schema 8), suitable
  for restoring that project later.
- It **is not**: a manuscript export, a redistribution package, or a portable
  interchange format. **Never publish an archive** and **never store credentials
  as artifact content**. Private storage backup is distinct from manuscript
  export/redistribution rights. A snapshot is a *private local* copy: because
  artifact content is captured as BLOBs, a snapshot may contain source bytes even
  though its manifest and inner validation carry only digests and pinned
  references. It is never a manuscript export and must never be published. A
  schema-8 intent's canonical bytes carry only bounded request/question metadata,
  opaque ids, exact decimal amounts, and digests — never raw source bytes, a
  credential, a capability, or an authority claim.

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
the schema the canonical migrations generate **for that version** in memory. The
supported snapshot store-schema versions are **3 through the current store schema**
(the authoritative value is `STORE_SCHEMA_VERSION` in
[`storage/schema.py`](../src/review_article_agent/storage/schema.py); currently
**8**), and the versions are:
**3** (identity, artifacts, revisions, rights, dependencies, import requests, and
local fixture approvals); **4** (adds the durable worker's synthetic
job/attempt/accepted-result/budget tables); **5** (adds the immutable runtime
configuration/session/task/proposal mappings); **6** (adds the immutable runtime
dispatch identity row and its mutable operational state row, with the
route-separated cost ledger); **7** (adds the append-only owner-auth evidence,
human-approval, and human-approval-entry rows); and **8** (adds the append-only
pre-dispatch Jev decision request intent row keyed by its direct dispatch). Any
other ("external") table, a
missing or altered object, or a hidden added column is refused, so a forged
database that merely reuses the right table names (and would pass
`foreign_key_check`) is never accepted.

All output is owner-private: the backup directory is `0o700`, and
`manifest.json`/`snapshot.sqlite3` are `0o600`.

## 3. Manifest

`manifest.json` is canonical JSON with these fields:

| Field | Meaning |
| --- | --- |
| `format` | `review_article_agent.private_storage_backup` (foreign-JSON guard) |
| `format_version` | `1` (the only supported version) |
| `created_at_utc`, `app_version`, `project_id` | provenance and stable identity |
| `store_schema_version` | the snapshot's store schema (the supported range `3..current`, currently `3..8`) |
| `database` | `{filename, sha256, byte_size}` of `snapshot.sqlite3` |
| `counts` | row counts for artifacts/revisions/rights/dependencies/imports/**fixture** approvals/fixture approval entries |
| `artifact_digest`, `revision_digest`, `rights_digest` | deterministic aggregate digests over identity + integrity columns |
| `approvals` | per-**fixture**-approval `{approval_id, snapshot_digest, entry_count}` |
| `notice` | the non-claim reminder (private snapshot; not an export) |

The `counts.approvals`/`counts.approval_entries` and `approvals` manifest fields
describe the **local fixture approvals only** (the pre-existing storage-fixture
rows). Durable schema-7 named-gate human approvals and durable schema-8 decision
request intents are not duplicated into the manifest; they are inner-validated
read-only from the snapshot database, exactly like the schema-4/5/6 rows.

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
- **schema 4 and above** — a durable job/attempt/result/budget row that does not verify
  read-only: a cross-project key, a `job_id` that does not derive from its
  `(project_id, request_id)` key, a malformed identifier (job/attempt id, request
  key, reserve stage), a parameter or accepted-result fingerprint that does not
  match the stored canonical JSON, an illegal job/attempt state or outcome flag,
  tampered attempt counters or `current_fence`, a malformed checkpoint (including a
  pathologically deep JSON body, rejected as a clean error rather than a leaked
  payload), a budget/reserve exact-decimal that is not bounded (or a reserve that
  is not strictly positive), a duplicate reserve for one attempt, a reserve whose
  attempt belongs to another job, or a dangling reserve/attempt reference;
- **schema 4 and above lifecycle correspondence** — a `completed` job with no accepted
  result; a `succeeded` attempt not backed by its job's single accepted result; an
  accepted result on a job that is not `completed`/`paused`/`cancelled` (a
  paused/cancelled job may legitimately retain one); a job whose unresolved bit
  disagrees with its current attempt (or a job with no attempt that nonetheless
  claims a remote unresolved outcome); and a forged retry chain — more than the
  bounded safe retries, or a follow-up attempt after an `ambiguous`/`succeeded`/
  `admitted`/unresolved prior attempt (`definitive_failure` or a local
  `interrupted` prior only). A reported `known`/`estimated` actual cost of exactly
  `0` is valid and is not rejected; the immutable reserve upper bound is never
  reduced.
- **schema 5 and above** — every durable runtime configuration/session/task/
  proposal mapping row is re-validated read-only: recomputed canonical digests and
  fingerprints, projected columns, config/task/input/proposal links, and
  cross-project keys. A historical task is not required to still name the
  *current* revision, and a persisted proposal is never treated as an approval.
- **schema 6 and above** — every durable runtime dispatch identity row and its
  operational state row is re-validated read-only: the route/accounting-bucket
  identity, canonical input/version/evidence digests, the exact-decimal reserve and
  settled cost, state/result/cost consistency, and the linked task. An `unknown`
  (ambiguous) remote dispatch keeps its reserve and is never re-declared resolved
  or automatically retried; a legitimate historical unresolved row is preserved
  rather than "repaired". Remote state and settled cost are independent: an
  unresolved dispatch may legitimately carry a `known`/`estimated` settled amount,
  and when the actual cost is itself unknown it stays `unknown` with **no** amount
  recorded (never `0`).
- **schema 7 and above** — every durable human-approval row is re-validated
  read-only: the recomputed canonical snapshot digest and structure, projected
  columns, every entry pin, and the out-of-band owner-auth binding. Legitimate
  historical `stale`/`superseded` approvals remain structurally valid and are
  preserved. No capability, challenge, credential, or opaque authority is read or
  serialized; the approval only *points at* its safe owner-auth evidence row.
- **schema 8 and above** — every durable pre-dispatch decision request intent is
  re-validated read-only: the recomputed canonical bytes and content digest, every
  projected column, the exact per-question definition/criteria refs, the consumed
  version set (input digest / version hash / prompt version bindings), the linked
  parent direct dispatch and its pinned task, and the recomputed request
  digest/bytes/bound/pricing/reserve. A foreign-project row, a dangling/orphan
  intent (missing parent dispatch or task), and a corrupt row all fail closed. A
  legitimate intent whose target head has since advanced remains structurally valid
  (history is preserved). No credential, capability, or authority is read or
  serialized; only the bounded request/question metadata, opaque ids, exact decimal
  amounts, and digests are revalidated.

The schema-specific inner hooks are selected by **minimum version**: a snapshot
records exactly one schema version and is always compared against the canonical
schema for that version, and it runs every read-only hook whose minimum version is
at or below its recorded version (so a schema-6 snapshot runs the schema-4/5/6
hooks but not the schema-7 approval hook; a schema-7 snapshot runs the schema-4/5/6/7
hooks; and a schema-8 snapshot runs all five). The hook list is an ordered
`_SCHEMA_VALIDATORS` table keyed by minimum version; a later schema must add its
own explicit read-only validator entry, and this document does not promise that
future schema fields are validated automatically.

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

A **schema-3 through schema-7** snapshot is restored as-is; the ordinary
`ProjectStore` open that runs while the restore is registered upgrades it to the
current schema and preserves every prior artifact byte and fixture-approval
snapshot exactly. An older snapshot therefore gains only the later empty tables on
its **first open**; it is never rewritten in place by the backup module. A
**schema-4** snapshot keeps its completed job results, unresolved attempts, and
budget reserves; a **schema-5** snapshot keeps its runtime mappings; a **schema-6**
snapshot keeps its dispatch identity/state and cost ledger; a **schema-7** snapshot
keeps its owner-auth evidence and human approvals; and a **schema-8** snapshot
keeps its pre-dispatch decision request intents. A schema-7 snapshot's
`stale`/`superseded` approval history is preserved and remains structurally valid.
A schema-8 intent is restored exactly as recorded and is **not** executed, resumed,
retried, or promoted to a result; the restored store records intent only. A
schema-6/7/8 `unknown`
dispatch keeps its reserve and is never re-declared resolved or automatically
retried. Its remote *state* and its settled *cost* are separate: an unresolved
dispatch may carry a `known`/`estimated` amount, and a genuinely unknown cost
stays `unknown` with no amount (never `0`). A recorded cost is bookkeeping only
and never proves the remote operation has quiesced.

The durable runtime surface in Step 04 is still partial. A schema-8 intent records
the **exact pre-dispatch question and request binding** before submission, but real
provider sessions, the Jev runner, and result persistence/observation are **not yet
implemented** (the schema-9 result-observation slice is not integrated), so no
snapshot or restore API claims to execute, carry, or resume them. A restored
snapshot is a storage artifact only; it is not proof of remote quiescence. Step 04
is **not** declared complete and no runner is live.

**Restore proves storage integrity, not remote quiescence.** A restored snapshot
records admitted/ambiguous durable attempts but is **not** proof that any external
operation has quiesced: no app-level execution activation and no synthetic
external provider journal is part of a per-project snapshot. A worker that opens a
restored project MUST reconcile or hold every restored `admitted`/`ambiguous`
attempt before dispatching new work or switching the active project; that
reconciliation is the engine's responsibility and is out of scope for this
module. A restore never fabricates a scientific or model approval.

## 7. Compatibility and limitations

- The supported store schema versions are **3 through the current store schema**
  (`STORE_SCHEMA_VERSION`, currently **8**). A newer store schema is refused
  clearly. Older snapshots are upgraded to the current schema by the ordinary
  `ProjectStore` open on **first open after restore**; the `2 -> 3` through
  `7 -> 8` migrations are tested separately and are not part of this module. A
  fixture approval's *recorded* `storage_schema_version` is validated against the
  supported range (`1..current`), so a historical version is preserved where
  supported while an arbitrary/unknown version is refused. Determining the
  version-specific canonical schema and the schema-4/5/6/7/8 row validation reuse
  the canonical migration, `runtime.jobs`, `storage.agent_runtime`,
  `runtime.dispatch`, `storage.human_approvals`, and `storage.decision_intents`
  definitions, so a schema change cannot silently desynchronise verification.
  An upgraded predecessor store preserves its prior artifact bytes and rows exactly;
  only the later empty tables are added on the first open.
- A **schema-7** human approval is an append-only, exact named-gate record. Its
  authority is out of band: the row only *points at* the safe owner-auth evidence
  row (auth id, method, owner uid, verification time, action digest). No opaque
  auth capability or challenge is ever serialized, and importing an approval or
  actor value cannot mint authority. A historical `stale`/`superseded` row is
  honest history and is never rejected as corrupt.
- A **schema-6** dispatch separates unresolved remote *state* from settled *cost*.
  An `unknown`/ambiguous remote state keeps its reserve and is never re-declared
  resolved or automatically retried, but the cost ledger is independent: such a
  dispatch may legitimately carry a `known`/`estimated` settled amount. When the
  actual cost is genuinely unknown it stays `unknown` with **no** amount recorded
  (never `0`). A recorded `known`/`estimated` cost is conservative bookkeeping and
  never proves that the remote operation has quiesced. The immutable dispatch
  identity row and the mutable operational state row are distinct: only the
  operational row changes, as an explicit state transition.
- A **schema-8** decision request intent is an append-only, immutable record of the
  exact pre-dispatch Jev question and the exact request binding (target/question
  refs, consumed version set, request digest/bytes/bound/pricing/reserve). Its
  canonical bytes carry only bounded request/question metadata, opaque ids, exact
  decimal amounts, and SHA-256 digests — never raw source bytes, a credential, a
  capability, or an opaque authority. It makes no scientific decision and grants no
  approval or execution right: it records intent only, and an `unknown` dispatch
  that carries an intent keeps its reserve and is never re-declared resolved,
  executed, or automatically retried.
- `verify_backup` re-validates stored rights records and recomputes content
  digests; from schema 4 upward it additionally re-validates the durable
  job/attempt/result/budget rows, from schema 5 upward the runtime mappings, from
  schema 6 upward the dispatch/cost ledger, from schema 7 upward the human-approval
  rows, and from schema 8 upward the pre-dispatch decision request intents. It is a
  structural/integrity check, not a scientific, rights, or remote-quiescence
  decision. A later schema that adds a new table must add its own explicit
  read-only inner validator; verification does not infer that a future schema's
  fields are valid.
- A snapshot is a **private local storage copy** and may contain source bytes as
  BLOB content; it is never a manuscript export or redistribution package, and it
  must never be published. The application never automatically ingests secrets;
  this module cannot prove the absence of any secret a user manually stored as
  artifact content. Do not store credentials as artifact content.
