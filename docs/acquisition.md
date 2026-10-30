# Acquisition reference — local import, manifest, human gate, and official retrieval (Step 06, implemented MVP)

> **Status: reference for the integrated Step 06 acquisition MVP.** The local acquisition CLI, the
> libraries behind it, the atomic folder-batch import CLI, and the detached
> official full-text retrieval job with its public `acquisition retrieve
> id|run|show` CLI are all integrated. Final delivery verification is recorded in
> the PR; no CI is configured. Scientific parsing/screening/digestion are **Step
> 07+**. Evidence and honest limits are in
> [`implementation/handoffs/06-acquisition.md`](implementation/handoffs/06-acquisition.md).
> No command here is a scientific, screening, seed, pilot, or hosted-use
> authorization, and no human approval is invented.

Acceptance criteria and deliverables are those of
[`implementation/06-acquisition.md`](implementation/06-acquisition.md), unchanged.
Specification: [`specification.md`](specification.md) workflow steps 6 and 10.
Contracts: C01 ([`contracts/01-identities.md`](contracts/01-identities.md)), C02
([`contracts/02-gates-and-runtime.md`](contracts/02-gates-and-runtime.md)) and
C03/P3 ([`contracts/03-policy-decisions.md`](contracts/03-policy-decisions.md)).
CLI spellings: [`cli.md`](cli.md) §16. Storage facts: [`storage.md`](storage.md) §8.

Source anchors (read these for exact field spellings and bounds):
[`acquisition/imports.py`](../src/review_article_agent/acquisition/imports.py),
[`acquisition/validation.py`](../src/review_article_agent/acquisition/validation.py),
[`acquisition/pdf_inspection.py`](../src/review_article_agent/acquisition/pdf_inspection.py),
[`acquisition/manifest.py`](../src/review_article_agent/acquisition/manifest.py),
[`acquisition/impact.py`](../src/review_article_agent/acquisition/impact.py),
[`acquisition/folders.py`](../src/review_article_agent/acquisition/folders.py),
[`acquisition/batches.py`](../src/review_article_agent/acquisition/batches.py),
[`acquisition/resolution.py`](../src/review_article_agent/acquisition/resolution.py),
[`acquisition/transport.py`](../src/review_article_agent/acquisition/transport.py),
[`acquisition/execution.py`](../src/review_article_agent/acquisition/execution.py),
[`cli/acquisition.py`](../src/review_article_agent/cli/acquisition.py),
[`cli/acquisition_manifest.py`](../src/review_article_agent/cli/acquisition_manifest.py).

## 1. What is and is not integrated

| Capability | Status at this base |
| --- | --- |
| Local single-file import (`acquisition import`), metadata-only `missing`, occurrence readback | Integrated CLI. |
| Local `inspect` (validation, optional PDF inspector) and read-only `impact` | Integrated CLI. |
| Revision-specific manifest `propose/show/status/history/require` and blocker `queue` | Integrated CLI. |
| Exact named human gate `artifact_revision` / `acquisition.complete` | Integrated through the existing `gate prepare`/`gate confirm` (see §6). |
| Atomic explicit folder-batch import (`read_folder_batch` + `import_folder_batch`) | Integrated CLI (`acquisition folder import`). |
| Official full-text acquisition (`AcquisitionExecutor`, two fixed official XML endpoints) with per-GET `before_attempt`/`after_attempt` journal seams | Integrated; retrieval runs as a detached job (`discovery_submit` `mode=acquire`, task kind `acquisition_fulltext`) with the public `acquisition retrieve id\|run\|show` CLI. |

Everything exercised so far was run through fixtures and local files. All app
calls and tests ran **without** live source retrieval, paid/hosted calls, or
upload; those paths are **unrun** against real providers.

## 2. Provenance and sources

* The only automatic sources in the executor are two fixed official endpoints, by
  PMC id only: the Europe PMC `fullTextXML` operation and NCBI PMC `EFetch`
  (`db=pmc`, `retmode=xml`). The exact URLs and the provider pages read are in the
  [`transport.py`](../src/review_article_agent/acquisition/transport.py) module
  docstring. Any other host, scheme, port, or path is refused before a socket is
  opened; no credentials are sent; redirects and proxies are disabled.
* The legacy PMC `oa.fcgi` service is **not** used. There is no mirror discovery,
  no captcha/paywall bypass, and no arbitrary-URL fetch. Unofficial acquisition
  is a **manual queue plus local import** path only.
* **Terms are human references, not legal truth.** A canonical `RightsRecord`
  (C03) records, per operation, `basis.<operation>.evidence` with `ref` and
  `provenance` strings, plus `recorded_at`. The executor's `terms_evidence`
  argument (`terms_ref` and `checked_at` per operation) is a **separate**
  caller input that is passed *alongside* the `RightsRecord`; `terms_ref` must
  equal the record's evidence `ref` for that operation. `terms_ref`/`checked_at`
  are not canonical rights fields. Neither is verified by the software, and
  neither is a legal determination.
* **`adapter_version` / `source_version` name the endpoint adapter, not an
  edition.** `document_version` is always explicitly
  `{"status": "unknown", ...}`: no published-vs-preprint edition evidence is
  observed. Do not read an adapter version as a document edition.
* The transport bounds connection/read *inactivity*, not total wall-clock time;
  the future detached worker owns lifecycle and kills.

## 3. Local import, missing, and occurrences

### 3.1 Rights binding (exact, caller-supplied)

Nothing is defaulted or fabricated; every `RightsRecord` is explicit caller input
and is validated against its **exact subject**:

| Operation | Rights file | Required subject |
| --- | --- | --- |
| `import` source | `--rights-file` | `source_revision` for `{project, --artifact-id, --revision-id}`. |
| `import` occurrence | `--occurrence-rights-file` | `source_revision` for `{project, occurrence artifact id, --occurrence-revision-id}`. Print the id with `acquisition occurrence id --request-id ID`. |
| `missing` source rights | `--rights-file` | A pre-acquisition `subject_artifact_ref` for `--artifact-id` (no revision exists). |
| `missing` occurrence | `--occurrence-rights-file` | Same occurrence subject as for `import`. |

The acquisition import wrapper writes **new** bytes only if the source rights allow **acquisition** *and*
**storage** (checked separately), the record is the latest `record_revision` for
its `rights_id`, and no privacy denial is recorded. Acquisition, storage,
transfer, and redistribution stay independent: `unknown` storage stores **no**
bytes (use `missing`), and free-to-read, storage, and hosted transfer are never
merged. This is a property of the acquisition wrapper/executor, not of the generic store:
the foundation `ArtifactRepository` validates storage only, so project bytes are
not globally proved acquisition-authorized or currently rights-valid, and
backups keep historical bytes after a later supersession (see
[`storage.md`](storage.md) §8). New operations and gates block on supersession;
retention is not a fresh legal or redistribution approval. A rights record that names another subject, a superseded revision, or a
changed record that cannot authorize reuse of an existing source fails closed
with a static message.

A synthetic rights-file setup is in [`cli.md`](cli.md) §6 (the rights-file
example). The values below are placeholders; copy a real, valid record, never
invent a grant.

```bash
# placeholders only; <...> must come from a rights file you prepared and reviewed
review-article-agent acquisition occurrence id --request-id req-1
review-article-agent acquisition import ./paper.xml \
  --artifact-id src-1 --revision-id src-1-r1 --request-id req-1 \
  --rights-file ./source-rights.json \
  --occurrence-rights-file ./occurrence-rights.json \
  --occurrence-revision-id occ-r1 \
  --origin-locator "<where you got the file>"
```

### 3.2 Behavior and bounds

* One import is atomic: source revision, rights, artifact rows and the typed
  `acquisition_import_occurrence` record commit in one transaction.
* **Occurrence is not a new source document revision.** Each call records the
  physical import event as its own immutable `report` revision (the artifact id
  is derived from the `request_id`), an honest **proposal** that authorizes no
  use. A duplicate-bytes import adds an occurrence revision but **no** new source
  revision.
* **Exact-byte duplicates, same artifact only.** Equal bytes inside the same
  document artifact reuse the earlier source revision and add a new occurrence;
  equal bytes in a *different* artifact are never merged. Alternate versions are
  distinct revisions, linked by `--parent-revision-id`.
* Exact replay of a `request_id` returns the historical result without writing;
  a divergent replay is a conflict.
* `--dependencies-file` is a JSON list of explicit `{project_id, artifact_id,
  revision_id, role}` relations (roles: `reports`, `uses_dataset`,
  `describes_experiment`, `derived_from`). Several reports of one study are
  linked **explicitly**; nothing is inferred from filenames, titles, or DOIs, and
  **no automatic study or title merge exists**.
* Bounds: 32 MiB source, 200-char ids, 2048-char locator, 1000-char reason, 64
  dependencies, 64 KiB rights, 1 MiB JSON side inputs. Over-limit is refused,
  never truncated.
* `missing` stores a metadata-only `no_full_text_acquired` event: no bytes, no
  source revision.
* Imported papers **do not bypass screening or seed approval** and are not proof
  of unrestricted processing rights. Output marks the result a proposal.

### 3.3 Read-only safe summary versus private `--content`

`occurrence show`, `manifest show`, and `manifest require` print a safe summary
by default (ids, counts, status, static codes). `--content` is the single
explicit opt-in to private data (origin locator, reason, rationale, identifiers,
document JSON); output is then marked private and non-authorizing. `inspect`
likewise returns status/checks/reasons by default and reveals identifiers and a
text preview only with `--content`. Error messages are static and never echo a
path, id, title, DOI, source byte, or exception text.

### 3.4 Atomic folder batch (library + `acquisition folder import` CLI)

`acquisition.folders.read_folder_batch` and `acquisition.batches` import an
**explicit manifest** of relative paths below one root. There is no directory
listing, glob, or filename-derived identity. Bounds and safety are from the
module constants: up to 1000 files by default, 32 MiB per file, 64 MiB batch
total by default (hard ceiling 256 MiB), `O_NOFOLLOW` descriptor-anchored reads
that refuse symlinks anywhere in the chain (a root under a symlinked ancestor
such as a symlinked `/tmp` is refused), regular files only. The whole batch is
read and validated before any write, imported in one transaction, and rolled
back whole on any failure. Exact replay returns the historical results; changed
bytes under an existing `request_id` is a conflict. Two different contents for
one document inside a batch need an explicit `parent_revision_id` on the later
entry. The result always has `authorizes_scientific_processing=False`. This is
not a sandbox; a same-user process can still race the tree.

## 4. Validation and PDF inspection

`acquisition inspect FILE --identifiers-file ids.json` (expected `pmid`/`pmcid`/
`doi` tokens) and the manifest both use the same pure
[`validate_asset`](../src/review_article_agent/acquisition/validation.py):

* An extension or declared `Content-Type` never establishes success. HTML/login
  responses (even named `.pdf`), grossly corrupt PDFs, malformed XML, and
  entity-declaring XML are `rejected`.
* A JATS full article is parsed with bounds (100 000 nodes, depth 200, 8 MiB of
  text; 32 MiB bytes). Identity comes only from `front/article-meta`
  `article-id` elements and must be exactly consistent with expected identifiers;
  a body is required. An identifier conflict is `rejected`; a missing body or
  identity is `needs_review`.
* **PDFs are always manual review.** A structurally plausible PDF is
  `needs_review` with `unknown` scan/full-text/identity checks. A raw byte DOI in
  a PDF is never promoted to identity. In the manifest a PDF source always needs
  an explicit exact `human_review`.
* `ready` claims only container/structure/exact-identity consistency, never
  correctness, completeness, or screen eligibility
  (`authorizes_scientific_processing` is always `false`).
* **Source asset availability `readable`** (for required figures, tables,
  supplements) means only that bytes are present and are not obviously HTML,
  empty, or oversize. It is not interpreted scientifically; Step 07 still has to
  interpret the asset.

**Optional PDF inspector (`--pdf-inspect`).** Backed by `pypdf==6.21.4`
(BSD-3-Clause; install with the `[pdf]` extra in `pyproject.toml`). It checks
*input integrity only*: container parses, encrypted or not, page count, which
pages yielded text, a bounded private preview, and identifiers found only when
exactly one distinct value exists. It does **not** choose a scientific parser
and does not claim the file is the intended paper or complete; Step 07 owns
scientific parsing, locators, and fidelity. Step 07 now performs bounded
per-page text extraction (with page-locatored passages) through the same
optional extra in its own isolated child; the Step 06 inspector stays
diagnostic-only and is not that extractor. Without `pypdf` it reports
`unavailable` explicitly. It runs in an isolated child process (`python -I`, no shell, a minimal
credential-free environment of only `PYTHONIOENCODING=utf-8`, no inheritance of
the parent environment, working directory `/`, new session) with a wall-clock timeout (at most 120 s), CPU,
address-space and file-size limits, page and text caps, and a 256 KiB reply
cap. The inspector is a **diagnostic**; it is not wired into the manifest gate,
and the CLI makes no automatic wrong-paper or completeness claim from it.

## 5. Manifest, queue, and dependency impact

### 5.1 Blocker queue

`acquisition queue --freeze-artifact-id A --freeze-revision-id R` is read-only
and works before any manifest exists. It needs an exact currently approved
discovery freeze (a stale/unapproved freeze is refused) and reports, for that roster, candidates with no manifest entry plus every
persisted blocker (full-text, visual, supplement, integrity) and any pinned source
that is no longer current. An empty queue is **not** an acquisition approval.

### 5.2 Manifest proposal

`acquisition manifest propose` records one **inert** `acquisition_manifest`
revision (a `report` artifact) over exactly the frozen roster, one entry per
frozen candidate. `--entries-file` is a strict JSON list; each entry has exactly
`candidate_id`, `disposition`, `rationale`, `source_ref`, `human_review`,
`required_assets`, `integrity_review`; any extra key, including `approved`,
`actor`, or `authority`, is refused. There is no actor or approve flag.

| Disposition | Meaning |
| --- | --- |
| `use_full_text` | Needs a pinned source document revision. Its stored bytes are re-read and re-validated against the identifiers of the *actual frozen candidate*. |
| `exclude`, `no_scientific_use` | Rationale only; no source, review, asset, or integrity claim allowed. |
| `defer` | Rationale only. It means **non-use for this revision only**; it is an explicit human disposition, not a blocker, and no candidate is reported as acquired. |

A `ready` source satisfies syntax/identity. A `needs_review` source (PDF, missing
identifiers) additionally needs `human_review` of those exact pinned bytes
(`identity_confirmed`, `full_text_readable`, `complete_article`, `checked_at`,
`provenance`, `rationale`); a `rejected` source (HTML/login, wrong paper,
corruption, identifier conflict) can **never** be waived. A human review is a
factual statement, not scientific proof.

**Required supplements and visuals.** Each `required` asset (`figure`, `table`,
`supplement`) needs `status: available` with pinned bytes; `unavailable` or
`unknown` is a **blocker**, and absence is never negative evidence. A
non-required asset never blocks. Blocker categories: `missing`, `fulltext`,
`visual`, `supplement`, `integrity`; the codes are listed in
`BLOCKER_CODES` in [`manifest.py`](../src/review_article_agent/acquisition/manifest.py).
A proposal with blockers is stored (the queue needs it) but cannot make
acquisition complete.

**P3 integrity (frozen review reuse).** With `integrity_review: null` the
freeze's own per-candidate review is reused. Retraction, expression-of-concern,
correction, and non-replication stay distinct statuses. **`unknown` is never
asserted clean and is not treated as flagged**: an unknown status blocks use for
that candidate until a review supplies a known status (P3: unknown is not clean;
the user's per-use rationale, not an automatic ban, decides a flagged candidate).
A `flagged` status needs an explicit
per-use `integrity_review` rationale; the flag itself is not an automatic ban.
A given review replaces the frozen one and may not predate it.

The manifest is re-derived from the store on every read. A tampered document, a
forged or missing dependency, or a changed source byte is reported as corruption.
Exact replay of a `request_id` returns the historical revision; a divergent
reuse is a conflict. A new proposal re-derives its inputs inside the writing
transaction, so a racing source or freeze change cannot slip in.

### 5.3 Dependency impact

`acquisition impact --artifact-id A --revision-id R` is read-only. For the
**current** head of a source artifact that has earlier revisions, it lists
current revisions that still use an earlier revision (direct and transitive),
plus the unaffected refs and count, and informational human-approval status.
It never writes, deletes, supersedes, or invalidates anything; `authorizing` is
always `false`. Older revisions and unaffected work are retained
(`historical_work_retained`). A brand-new source artifact with no recorded
dependency affects nothing, and relatedness is never inferred (no title or
relevance matching). **New versions flag dependency impact; they do not delete
old work.**

## 6. The exact human acquisition gate

`acquisition manifest require` calls `require_acquired`. Acquisition is
complete only while **all** hold: the exact named gate
`artifact_revision` / `acquisition.complete` has a currently active, authentic
human approval of **this exact manifest revision**; the discovery freeze is still
authentic and its head; the roster is unchanged; every pinned source/asset is still
its artifact's current revision (a newer source stales only manifests that pin it);
source rights still allow storage; and no blocker remains. A generic approval of
the manifest artifact, a payload field, or an imported document can never stand
in. The result carries static reason codes only (`no_manifest`,
`manifest_invalid`, `gate_missing`, `gate_not_active`, `freeze_not_authentic`,
`roster_mismatch`, `blockers_present`, `source_superseded`).

`manifest propose` records **no approval**. It returns a non-authorizing
`approval_request` for the named gate with `gate prepare`/`gate confirm`
guidance; the owner confirms out of band on the controlling terminal
([`cli.md`](cli.md) §11). That approval is an acquisition prerequisite **only**:
it is not a scientific, pilot, screening, seed, or hosted-use authorization.

**Large rosters.** The gate snapshot closure includes sources, context, and
history and is bounded by `MAX_GATE_SNAPSHOT_ENTRIES = 10000`
([`runtime/gates.py`](../src/review_article_agent/runtime/gates.py)). For a large
roster, `gate prepare` can **block**; there is no hidden truncation, and no
10k-scale operation was run (Step 15 scale testing is unrun).

## 7. Official acquisition executor and the detached retrieval job

`AcquisitionExecutor.acquire(freeze_ref, candidate_id, ...)` in
[`execution.py`](../src/review_article_agent/acquisition/execution.py) acquires
for **one** frozen candidate. The caller supplies the freeze, the exact candidate,
a replay-key `request_id`, source and occurrence/report rights, and per-operation
`terms_evidence`; identifiers and URLs come only from the frozen corpus. Highlights:

* Resolver authorization is **derived from the validated canonical rights
  record**, never from a caller flag. The executor retains **new** source bytes only where
  acquisition **and** storage are allowed and no privacy veto exists; unknown
  storage stores metadata only. Transfer and redistribution are independent and
  are not granted by this path.
* The outcome is one immutable `acquisition_retrieval` report pinning the freeze
  and, if retained, the source and occurrence revisions. It contains no bytes,
  base64, or preview, and has `authorizing=False` and
  `eligibility="eligibility_not_assessed"`. Status is `retrieved`,
  `manual_required`, or `interrupted`.
* **Fencing and journals.** Before every GET the executor re-asserts activation,
  the freeze, and `stop_requested()`, then calls `before_attempt`. After every
  dispatched GET it calls `after_attempt` with bounded metadata. The detached
  `acquire` job persists its checkpoint through these seams: a may-have-dispatched
  mark before each GET under the registry write guard, and safe metadata after;
  the send admission is linearized with pause/cancel under the **captured** lease,
  and no lock is held across network work. The public CLI `acquisition retrieve
  id|run|show` (strict request JSON; `run` returns a **queued admission only**,
  never completion; `show` is a verified safe readback with an explicit private
  `--content` opt-in) drives it; see [`cli.md`](cli.md) §16 and
  [`worker.md`](worker.md) §13. Direct library use without a journal can lose the
  observations of a call whose local commit is rejected.
* An `interrupted` report is durable; resuming needs a **new** `request_id`.
* Bounds: two sources maximum, at most 5 attempts each (default 3), response cap
  16 MiB default (64 MiB ceiling), timeout default 30 s (300 s ceiling).
* An instance is not thread-safe, and no lock is held across network work.

## 8. Explicit non-claims

* No source retrieval, paid/hosted call, or upload was run live; all evidence is
  fixtures and local files. No 10k-scale operation was run.
* No claim of literature completeness, screening eligibility, or scientific
  correctness from any acquisition result.
* Scientific parsing, screening, and digestion are **Step 07+**. The global
  cross-artifact exact-byte guard (`cross_artifact_duplicate`, `matching_refs`,
  explicit canonical-target retry, no automatic identity merge/catalog) is
  required Step 06 behavior (C01 §6.1 R1), now integrated at `246df21`
  (`4e1e5a3` + `d2f3d57`); it is not evidenced by the `dbe613a` checkpoint.
