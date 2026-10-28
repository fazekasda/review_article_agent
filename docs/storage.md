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
snapshots), schema 8 (the immutable pre-dispatch Jev decision request intent),
schema 9 (the immutable, complete per-question Jev decision-result observation),
and schema 10 (the immutable pre-transport session-turn intent correlation).
The extension is strictly additive — earlier migrations are never rewritten — and
the manifest `format_version` stays `1`. A schema-9 row written by the inert
`record_result` is an **OBSERVATION**, not an accepted local result: the module
records what a trusted direct-call result contained and grants no approval,
execution, or completion claim. The single atomic `commit_result` bridge is now
implemented: in one store transaction it records the same observation, settles the
exact cost monotonically, and applies the derived local transition — accept a
completed HTTP 200 result as the dispatch's single local result, fail a documented
definitive pre-billing rejection as `not_incurred`, or reconcile an ambiguous
outcome to `unknown` with the reserve retained. Neither path is a transport: no
network call, no new identity, and no claim of remote quiescence. Production live
generation and hosted transfer remain blocked, and runner/CLI integration is not
done, so no snapshot or restore API carries, executes, resumes, or retries a
runner. A schema-10 row records the exact pre-transport session-turn correlation;
it grants no admission, approval, or execution authority and is never a
completion.

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
  the exact named-gate human-approval/evidence/entry rows under schema 7, the
  immutable pre-dispatch decision request intent rows under schema 8, the
  immutable complete per-question decision-result observation rows under schema 9,
  and the immutable pre-transport session-turn intent correlation rows under
  schema 10), suitable for restoring that project later.
- It **is not**: a manuscript export, a redistribution package, or a portable
  interchange format. **Never publish an archive** and **never store credentials
  as artifact content**. Private storage backup is distinct from manuscript
  export/redistribution rights. A snapshot is a *private local* copy: because
  artifact content is captured as BLOBs, a snapshot may contain source bytes even
  though its manifest and inner validation carry only digests and pinned
  references. It is never a manuscript export and must never be published. A
  schema-8 intent's canonical bytes carry bounded private request/question metadata
  — opaque ids, exact decimal amounts, digests, and the **submitted request state**
  (`plan_request.state`), which is caller-provided text that may itself be source
  text or any other submitted content. The intent therefore reproduces the exact
  request and may contain source/state text inside the private snapshot. No
  credential, capability, or authority is *automatically* serialized by the
  application, but this module cannot prove the absence of a secret a caller placed
  into the request state or artifact content: never put credentials in artifact
  content or a request state. A **schema-9** result observation's canonical bytes
  similarly carry source-derived per-question text — the provider envelope, a score
  `legend`, and the declared `options`/`levels` — and its referenced question
  instructions/source bytes stay in the private revision BLOBs. This module is
  **not** a secret scanner and makes **no** "never contains source" promise; the
  public manifest and static verification errors do not carry that per-question or
  request-state text (see §3 and §5). A **schema-10** session-turn intent's canonical
  plan and message bytes similarly carry bounded private application text — the
  target/input refs, role/model/config/version pins, limits, the deterministic
  session/message/admission correlation, and the submitted application text. The
  application does not automatically serialize a credential or opaque authority,
  but this module is **not** a secret scanner and makes **no** "never contains
  source" promise: never put credentials in application text. The public manifest
  and static verification errors carry none of that text.

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
**10**), and the versions are:
**3** (identity, artifacts, revisions, rights, dependencies, import requests, and
local fixture approvals); **4** (adds the durable worker's synthetic
job/attempt/accepted-result/budget tables); **5** (adds the immutable runtime
configuration/session/task/proposal mappings); **6** (adds the immutable runtime
dispatch identity row and its mutable operational state row, with the
route-separated cost ledger); **7** (adds the append-only owner-auth evidence,
human-approval, and human-approval-entry rows); **8** (adds the append-only
pre-dispatch Jev decision request intent row keyed by its direct dispatch);
**9** (adds the append-only ``decision_result_set`` observation row keyed by its
direct dispatch, plus one ``decision_result_question`` canonical C02 §6.2 row per
question); and **10** (adds the append-only ``session_turn_intent`` row keyed by its
direct ``opencode_session`` dispatch, with its exact canonical plan/message bytes,
digests, and session/message/admission pins). Any other ("external") table, a
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
| `store_schema_version` | the snapshot's store schema (the supported range `3..current`, currently `3..10`) |
| `database` | `{filename, sha256, byte_size}` of `snapshot.sqlite3` |
| `counts` | row counts for artifacts/revisions/rights/dependencies/imports/**fixture** approvals/fixture approval entries |
| `artifact_digest`, `revision_digest`, `rights_digest` | deterministic aggregate digests over identity + integrity columns |
| `approvals` | per-**fixture**-approval `{approval_id, snapshot_digest, entry_count}` |
| `notice` | the non-claim reminder (private snapshot; not an export) |

The `counts.approvals`/`counts.approval_entries` and `approvals` manifest fields
describe the **local fixture approvals only** (the pre-existing storage-fixture
rows). Durable schema-7 named-gate human approvals, durable schema-8 decision
request intents, durable schema-9 decision-result observations, and durable
schema-10 session-turn intents are not duplicated into the manifest; they are
inner-validated read-only from the snapshot database, exactly like the
schema-4/5/6 rows. The manifest therefore never carries a schema-8 intent's
request state text, a schema-9 per-question answer/legend/option text, a schema-10
session-turn intent's canonical plan/message text, or any artifact/request BLOB
content — only ids, counts, and aggregate digests.

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
  (history is preserved). The bounded request/question metadata it revalidates
  includes the recorded request state text. The sweep does not read or serialize a
  credential/capability/authority *field* (none exists), and it never echoes the
  state text: its problems are static and value-free. It does not scan the
  caller-provided state for secrets, so a secret the caller put into the state
  remains in the private snapshot.
- **schema 9 and above** — every durable decision-result observation is
  re-validated read-only, the whole-call set row and each per-question record
  independently: the recomputed canonical bytes/content digest and projected
  columns; the parent dispatch/task/intent pins; the single dispatch cost and the
  provider envelope; and, for each question, the record rebuilt from the recorded
  refs, the descriptor digest, options/levels/kind, the version set/consumed slots,
  the request pin, the per-record cost and provider envelope, and the exact
  **complete ordered** question set measured against the recorded intent's own
  questions — never the set's own array alone. A foreign-project row, a
  dangling/orphan or partial set, a self-consistent subset whose set array matches
  its surviving rows, and a corrupt set/record row all fail closed. A legitimate
  observation whose target head has since advanced remains structurally valid
  (history is preserved). It imposes **no** current-head requirement, performs
  **no** accept/execution/retry itself, and reads no credential/capability/authority
  field (none exists). A row written by the inert `record_result` leaves the
  dispatch in its submitted/unknown state; a row written by the atomic
  `commit_result` bridge additionally carries the derived local transition — the
  dispatch's accepted `result_kind`/`result_ref`/`result_digest` triple and its
  completed/failed/unknown state — and verification preserves those exactly as
  committed rather than re-performing the accept. An `unknown` cost stays
  `unknown` with **no** amount (never `0`), and a known reported amount on an
  ambiguous outcome remains bookkeeping that does not resolve the operation. The
  per-question canonical may contain source-derived provider/legend/options text;
  the module is not a secret scanner and makes no "never contains source" promise.
  Its problems are static and value-free.
- **schema 10 and above** — every durable pre-transport session-turn intent is
  re-validated read-only: the exact canonical plan/message bytes and their
  digests, every projected session/message/admission column, and the independent
  binding to the persisted direct `opencode_session` dispatch, bounded task,
  session mapping, configuration snapshots, target/input refs, versions, and the
  deterministic admission id. A foreign-project row, a dangling/orphan intent
  (missing dispatch, task, or mapping), and a corrupt row all fail closed. A
  legitimate intent whose target head has since advanced remains structurally
  valid. Revalidation needs no live service and no live filesystem path. It reads
  no credential, capability, admission, approval, or execution authority (none
  exists in the row) and grants none; its problems are static and value-free. The
  canonical/message bytes are private application text and may contain
  caller-submitted source text; the module is not a secret scanner and makes no
  "never contains source" promise.

The schema-specific inner hooks are selected by **minimum version**: a snapshot
records exactly one schema version and is always compared against the canonical
schema for that version, and it runs every read-only hook whose minimum version is
at or below its recorded version (so a schema-6 snapshot runs the schema-4/5/6
hooks but not the schema-7 approval hook; a schema-7 snapshot runs the schema-4/5/6/7
hooks; a schema-8 snapshot runs all five earlier hooks but not the schema-9
result hook; a schema-9 snapshot runs all six earlier hooks but not the schema-10
session-turn hook; and a schema-10 snapshot runs all seven). The hook list is an
ordered `_SCHEMA_VALIDATORS` table keyed by minimum version; a later schema must
add its own explicit read-only validator entry, and this document does not promise
that future schema fields are validated automatically.

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

A **schema-3 through schema-10** snapshot is restored as-is; the ordinary
`ProjectStore` open that runs while the restore is registered upgrades an **older**
snapshot to the current schema and preserves every prior artifact byte and
fixture-approval snapshot exactly. An older snapshot therefore gains only the later
empty tables on its **first open**; it is never rewritten in place by the backup
module. A **schema-4** snapshot keeps its completed job results, unresolved attempts,
and budget reserves; a **schema-5** snapshot keeps its runtime mappings; a
**schema-6** snapshot keeps its dispatch identity/state and cost ledger; a
**schema-7** snapshot keeps its owner-auth evidence and human approvals; a
**schema-8** snapshot keeps its pre-dispatch decision request intents; a
**schema-9** snapshot keeps its immutable complete per-question decision-result
observations; and a **schema-10** snapshot keeps its immutable pre-transport
session-turn intent correlations. A schema-7 snapshot's `stale`/`superseded`
approval history is preserved and remains structurally valid. A schema-8 intent is
restored exactly as recorded and is **not** executed, resumed, retried, or promoted
to a result; the restored store records intent only. A schema-9 observation is
likewise restored exactly as recorded. Restore never accepts, executes, resumes,
retries, or promotes it: a row recorded by the inert `record_result` keeps its
submitted/unknown dispatch state (an **OBSERVATION NOT ACCEPTED**), while a row
committed by the atomic `commit_result` bridge keeps the exact accepted
kind/ref/digest triple, the derived completed/failed/unknown state, and the settled
cost that `commit_result` recorded. A schema-10 session-turn intent is restored
exactly as recorded — its canonical plan/message bytes and session/message/
admission pins — and is **not** submitted, admitted, executed, or promoted; restore
requires no live service or live path. A schema-6/7/8/9/10 `unknown` dispatch keeps
its reserve and is never re-declared resolved or automatically retried. Its remote
*state* and its settled *cost* are separate: an unresolved dispatch may carry a
`known`/`estimated` amount, and a genuinely unknown cost stays `unknown` with no
amount (never `0`). A recorded cost is bookkeeping only and never proves the remote
operation has quiesced.

The durable runtime surface in Step 04 is still partial. A schema-8 intent records
the **exact pre-dispatch question and request binding** before submission, a
schema-9 row records the trusted direct-call result, and a schema-10 row records the
exact pre-transport session-turn correlation; the atomic local accept/transition
bridge (`commit_result`) is implemented. Real provider sessions, the Jev runner,
and runner/CLI integration are not done, and production live generation/hosted
transfer remains blocked, so no snapshot or restore API claims to execute, carry,
accept, resume, or retry a runner. A restored snapshot is a storage artifact only;
it is not proof of remote quiescence. Step 04 is **not** declared complete and no
runner is live.

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
  (`STORE_SCHEMA_VERSION`, currently **10**). A newer store schema is refused
  clearly. Older snapshots are upgraded to the current schema by the ordinary
  `ProjectStore` open on **first open after restore**; the `2 -> 3` through
  `9 -> 10` migrations are tested separately and are not part of this module. A
  fixture approval's *recorded* `storage_schema_version` is validated against the
  supported range (`1..current`), so a historical version is preserved where
  supported while an arbitrary/unknown version is refused. Determining the
  version-specific canonical schema and the schema-4/5/6/7/8/9/10 row validation
  reuse the canonical migration, `runtime.jobs`, `storage.agent_runtime`,
  `runtime.dispatch`, `storage.human_approvals`, `storage.decision_intents`,
  `storage.decision_results`, and `storage.session_intents`
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
  refs, consumed version set, request digest/bytes/bound/pricing/reserve,
  and the submitted request state). Its canonical bytes are **private** and carry
  bounded request/question metadata, opaque ids, exact decimal amounts, SHA-256
  digests, and the caller-provided `plan_request.state` text — so the intent may
  contain source text (or any other content the caller submitted as the state). No
  credential, capability, or opaque authority is *automatically* serialized, but the
  module cannot prove the absence of a secret a caller placed into the state or
  artifact content: never put credentials there. The intent canonical is never
  published or redistributed, and the public manifest and static verification errors
  never carry the state text. It makes no scientific decision and grants no approval
  or execution right: it records intent only, and an `unknown` dispatch that carries
  an intent keeps its reserve and is never re-declared resolved, executed, or
  automatically retried.
- A **schema-9** decision-result observation is an append-only, immutable record of
  **one** trusted direct Jev dispatch's **complete** per-question result set: one
  set row (keyed by its direct dispatch, carrying the ordered per-question
  record-id/digest array, the whole-call status/diagnostic, the single dispatch
  cost, and the provider envelope) plus one canonical C02 §6.2 `DecisionRecord` row
  per question. Its canonical bytes are **private** and may contain source-derived
  per-question text — the provider envelope, a score `legend`, and the declared
  `options`/`levels` — while the referenced question `instructions` and target
  source bytes stay in the private revision BLOBs. The module is **not** a secret
  scanner and makes **no** "never contains source" promise: a caller who submits
  source text into a request state or artifact content places it in the private
  archive. A row written by the inert `record_result` grants **no** authority and
  makes **no** completion claim: it is an **OBSERVATION NOT ACCEPTED**, never an
  accepted local result and never a claim of remote quiescence. It never calls
  `accept_result`/`recover`/`settle_cost` and never advances the dispatch state
  machine. A row written by the atomic `commit_result` bridge additionally carries
  the derived local transition: a completed HTTP 200 result is accepted as the
  dispatch's single local result (`result_kind`/`result_ref`/`result_digest`), a
  documented definitive pre-billing rejection marks the dispatch failed with
  `not_incurred` and no accepted result, and an ambiguous outcome is reconciled to
  `unknown` with the reserve retained. A known reported amount on an ambiguous
  outcome is bookkeeping and does not resolve the operation. Neither path turns an
  `unknown` cost into `0`; backup/restore preserves either kind of row and the
  recorded transition exactly but never performs an accept, retries/resubmits, or
  claims remote quiescence. The canonical bytes are never published or
  redistributed; the public manifest and static verification errors never carry the
  per-question or request-state text. The Jev runner and runner/CLI integration are
  not done, production live generation/hosted transfer remains blocked, and no
  runner is live.
- A **schema-10** session-turn intent is an append-only, immutable correlation of
  one reserved `opencode_session` turn's deterministic specification, recorded
  **before** transport. It stores the exact canonical plan/message bytes and their
  digests plus the projected session/message/admission/role/model/config/version
  pins. Its canonical/message bytes are private application text and may contain
  caller-submitted source text; the module is not a secret scanner and makes no
  "never contains source" promise. It grants no admission, approval, or execution
  authority and makes no completion claim; it never changes the dispatch's state or
  cost. A replay of the identical bytes is idempotent and a divergent replay is
  refused. Restore preserves the exact bytes and pins without requiring a live
  service or a current head; it does not submit, admit, execute, resume, retry, or
  promote the turn. The public manifest and static verification errors carry none of
  the canonical/message text.
- `verify_backup` re-validates stored rights records and recomputes content
  digests; from schema 4 upward it additionally re-validates the durable
  job/attempt/result/budget rows, from schema 5 upward the runtime mappings, from
  schema 6 upward the dispatch/cost ledger, from schema 7 upward the human-approval
  rows, from schema 8 upward the pre-dispatch decision request intents, from
  schema 9 upward the full per-question decision-result observations, and from
  schema 10 upward the pre-transport session-turn intent correlations. It is a
  structural/integrity check, not a scientific, rights, or remote-quiescence
  decision. A later schema that adds a new table must add its own explicit
  read-only inner validator; verification does not infer that a future schema's
  fields are valid.
- A snapshot is a **private local storage copy** and may contain source bytes as
  BLOB content; it is never a manuscript export or redistribution package, and it
  must never be published. The application never automatically ingests secrets;
  this module cannot prove the absence of any secret a user manually stored as
  artifact content. Do not store credentials as artifact content.
