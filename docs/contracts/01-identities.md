# 01 — Identity and evidence contract (v1.3, **proposed**)

> **Status: PROPOSED — approval pending.** No part of this document is ratified
> science, an approved schema, or a claim of production completion. It drafts the
> identity/evidence contract for review; only the user approves scientific policy
> and the baseline decisions (§8).
>
> **Document version:** `v1.3` (proposal revision of `v1`; see §9 change log).
> Versioning rule and change log in §9.
>
> **This is not Step 01 completion.** This contract is one of several Step 01
> deliverables; no Step 01 pass, no scientific policy, and no schema is claimed
> here. All approvals and open items remain pending (§8).
>
> **Sources this document is derived from:**
> - [Scientific and product baseline](../specification.md) — Confirmed scientific
>   workflow steps 6, 9, 10, 11 and "Gates and scientific invariants" (baseline,
>   not weakened here).
> - [Step 01 step document](../implementation/01-contracts.md) — deliverables,
>   acceptance criteria, and the required worked examples.
> - [Step 01 kickoff](../implementation/kickoffs/01-contracts.md) — runtime
>   invariants and the "still open" list (P1–P6).
> - [Runtime language decision 001](../decisions/001-runtime-language.md) —
>   accepted runtime decision; it approves **no** scientific policy, schema, IPC,
>   or framework.
>
> **Non-claims.** This document invents no numeric scientific threshold, accepts
> none of P1–P6, and asserts no audited completeness. Sampling audits described
> here are explicitly **not** independent dual-human review, held-out accuracy
> validation, or proof that nothing was omitted (specification line 59).

---

## 1. Scope, status, and reading guide

### 1.1 Baseline-required invariants (confirmed — restated, not proposed)

These are already confirmed in `docs/specification.md` and the kickoff. This
contract must preserve them; it must not weaken them.

- **I1** Approvals bind to **immutable artifact revisions and dependencies**, not
  filenames or generic tool permissions. Agents cannot self-approve
  (specification lines 54–55).
- **I2** Material edits invalidate affected checks. A new model-generated draft
  revision does not replenish an exhausted repair budget (line 55).
- **I3** The pipeline acquires full text **or an explicit human disposition** for
  every frozen candidate before scientific digestion (line 29).
- **I4** Extraction records readable summaries **plus** structured findings,
  original passages/locators, context, numerical details where relevant,
  limitations, and attribution (line 32).
- **I5** Exact duplicates, alternate document versions, and linked reports of one
  study are **different cases**. New evidence creates an explicit revision and
  reopens affected approvals without erasing unaffected work (line 33).
- **I6** Multiple reports of one dataset are **not** independent confirmation
  (line 61).
- **I7** A summary, quote, file hash, model probability, or human approval alone
  does not establish scientific correctness or completeness (line 58).
- **I8** Separate direct observations, author interpretations, secondary reports,
  and proposed synthesis (line 60).
- **I9** Acquisition, storage, redistribution, and hosted-model transfer rights
  are **separate**; uploading a file is not proof of all rights (line 63).
- **I10** At a citation/tracing limit, secondary attribution stays a **warned
  state requiring final human approval** — never "verified primary support"
  (line 50; kickoff).
- **I11** Documents and model outputs are **untrusted data**; no credential or
  source-text leakage (line 62; kickoff).

### 1.2 Proposed representations (this document — not ratified)

Everything in §2–§7 is a **proposal for review**. In particular: the field names,
enum spellings, state vocabulary, record types, and worked examples are design
proposals, not approved schema. They are written normatively so reviewers can
verify them, but nothing here is in force until the user approves it (§8).

### 1.3 What this document does not decide

- It does **not** accept, modify, or defer P1–P6; they remain open with the user
  as owner (§8).
- It does **not** choose storage/IPC/package layout (step 01's other deliverables).
- It does **not** set any numeric threshold (sample sizes, severity scores, hop
  limits, budgets).
- It does **not** claim any scientific feature is implemented or validated.

---

## 2. Normative vocabulary, types, and states

### 2.1 Requirement keywords

`MUST`, `MUST NOT`, `SHOULD`, `MAY` are used in the RFC 2119 sense **within this
proposal**. A `MUST` here is a proposal-level requirement until approved.

### 2.2 Value states (explicit, mutually exclusive)

Every field whose value may be unavailable carries **one** state token and, for
non-`known` states, a **reason string**:

| State | Type token | Meaning |
| --- | --- | --- |
| known | `known` | Value is present and asserted by a named source/record. **Availability only** — `known` says nothing about correctness, approval, rights clearance, attribution quality, or scientific support. |
| missing | `missing` | A required value was **expected** for this record but is **absent**; absence is observed. Blocks dependent behavior until a human disposition. |
| unknown | `unknown` | The value **cannot be determined** from available evidence; **no claim of absence** is made. Blocks dependent behavior until a human disposition. |
| not_applicable | `not_applicable` | The field does not apply to this record kind/context. MUST carry a reason; MUST be used only where this contract marks the field N/A-eligible. |

`missing` and `unknown` MUST NOT be silently defaulted or treated as `known`.
A `known` value is **availability, not verification**: downstream approval,
rights-clearance, attribution, and scientific-support checks still apply and may
block or reject a record whose fields are all `known` (I7, §2.3).

### 2.3 Validation outcomes

A record is validated to exactly one outcome:

| Outcome | Meaning |
| --- | --- |
| accepted | All mandatory fields present with valid types/states; relations resolve. |
| rejected_invalid | A mandatory field is absent, has the wrong type, or a declared relation is violated (e.g., false independence). The record MUST NOT be persisted as usable. |
| blocked | The record is well-formed but a mandatory field is `missing`/`unknown` where the contract requires `known`. Dependent behavior MUST NOT proceed until an explicit human disposition. |

### 2.4 Type grammar

Types are written in a small grammar:

- scalars: `string`, `int`, `bool`, `timestamp` (RFC 3339, UTC);
- `enum[a|b|…]` — one of the listed members, compared exactly (case-sensitive);
- `hash{alg}` — a `"<alg>:<hex>…"` string; `alg` MUST be in
  `hash_algorithm`;
- `object` — a structured value whose required members are named where it is
  used (a named subtype, below, or an inline `{k: T, …}`);
- `list<T>` — ordered; may be empty unless the field says otherwise;
- `Opt<T>` — optional field (absent is allowed and is **not** a §2.2 state);
- `ref<Record>` — an id reference to another record of this contract;
- `stated<T>` — the explicit-state wrapper, defined below;
- partial references are written `{project_id, artifact_id, revision_id}`.

**`stated<T>` shape (definition).** A `stated<T>` field is an object:

```
stated<T> = {
  state:  enum[known | missing | unknown | not_applicable],
  value?: T,        # present iff state == known; absent otherwise
  reason?: string   # REQUIRED for missing | unknown | not_applicable
}
```

Rules: `value` MUST be present and well-typed iff `state` is `known`; for any
other state `value` MUST be absent; `reason` MUST be a non-empty string unless
`state` is `known`. `not_applicable` is allowed only where the field's row marks
the field N/A-eligible.

**Named structured subtypes** used by §5:

| Subtype | Members | Used by |
| --- | --- | --- |
| `AssetRef` | `{asset_ref: string, media_type: string, locator: string}` | `EvidenceRecord.source_assets` |
| `LocatorValue` | `{kind: enum[page\|pages\|section\|figure\|table\|offset\|timecode\|url_anchor], value: string}` | `EvidenceRecord.original_locator` |
| `RuleRef` | `{rule_id: string, revision_id: string}` — an approved rule revision (`§8`), never a bare number | `OmissionCheckRecord.scope.sample_rule` |
| `OmissionRef` | `{ref: RefRevision \| string, description: string}` | `OmissionCheckRecord.found_omissions` |
| `DependencyTarget` | `{to_revision: stated<RefRevision>, to_artifact: RefArtifact}` | `DependencyReference`; see §5.5 |
| `SubjectRef` | one of `RefRevision` \| `{record_type: string, record_id: string}` | `ProvenanceRecord.subject` |
| `ModelRef` | `{agent: string, model: string, instructions_revision: string}` | `ProvenanceRecord.model_ref` |
| `EvidenceRef` | `{target_finding_revision: RefRevision, evidence_id?: string}` — the compared unit is a **finding revision** (which MUST be kind `finding`); `evidence_id` names a specific `EvidenceRecord` when needed and is optional | `IndependenceRecord.evidence_a`/`evidence_b` |
| `ActorRef` | `{actor_kind: enum[user\|agent], actor_id: string, confirmation_ref: stated<RefRevision>}` — who; `confirmation_ref` MUST be `known` (a human-confirmation revision) when `actor_kind` is `agent`, and `not_applicable` (with reason) for a human | `IndependenceRecord.adjudicated_by` |
| `RightsRecordRef` | `{project_id: string, rights_id: string, record_revision: int}` — pinned reference to a **canonical** policy `RightsRecord`; all three parts are the record's immutable identity, so a stale reference does not resolve after the rights record is revised | `RevisionRecord.rights_ref`; `ImportOccurrenceRecord.rights_ref`; `EvidenceRecord.rights_reference` |

---

## 3. Shared references

### 3.1 Shared reference types

These three reference shapes are used everywhere this document points at an
immutable artifact revision. All three parts are required together; a bare
filename, path, content hash, or title is **not** a reference.

| Type | Fields | Meaning |
| --- | --- | --- |
| `RefProject` | `{project_id: string}` | One persisted scientific review project namespace. |
| `RefArtifact` | `{project_id, artifact_id}` | One logical artifact within a project. |
| `RefRevision` | `{project_id, artifact_id, revision_id}` | One immutable revision of one artifact. |

`project_id`, `artifact_id`, and `revision_id` are opaque strings, unique within
their parent scope. Naming/encoding of these ids is an implementation proposal,
not fixed here.

### 3.2 Identity vs immutable revision (the central distinction)

- An **artifact** (`artifact_id`) is a *logical* identity that persists across
  edits: e.g., "the Jev rubric for project P-0001", "the document Smith 2020".
- A **revision** (`revision_id`) is one **immutable** snapshot of that artifact.
  Once written, its bytes, its hash, and its linked evidence/approvals MUST NOT
  change. A change produces a **new** `revision_id`.
- **Approvals and checks bind to `RefRevision`, not to `artifact_id`.** A filename
  change, a re-import, or a same-name overwrite does not move an approval (I1).
- `artifact_id` MUST NOT be reused for a different logical artifact. Two
  different studies, two different rubrics, or a preprint vs a formally distinct
  paper are separate artifacts, not two revisions, unless the user decides they
  are versions of one artifact.

### 3.3 Content hash is not scientific identity (I7)

`content_hash` is a **byte-level integrity key and duplicate detector only**.

| Observed | Means | Does **not** mean |
| --- | --- | --- |
| equal `content_hash` (same alg + `byte_size` + `media_type`) | identical bytes (exact duplicate) | same study, or independent support |
| unequal `content_hash` | bytes differ (re-encode, re-scan, new version) | different science, or a different document |

Therefore a hash MUST NOT be used as an artifact identifier, as a study identity,
or as evidence of independence. Scientific identity is carried only by
`{project_id, artifact_id, revision_id}` plus the declared relations of §6.

---

## 4. Artifact kind taxonomy (distinctions are mandatory, I5)

Every artifact has exactly one `kind`. These kinds MUST remain distinct and MUST
NOT be conflated (implementation 01-contracts.md acceptance criterion 3):

| kind | What it is | Explicitly **not** |
| --- | --- | --- |
| `rubric` | An appraisal/scoring criterion set used to judge evidence (e.g., the Jev rubric), versioned. | not a framework, not an outline, not a produced report |
| `framework` | The scientific/conceptual framework: definitions, scope constraints, relationships, open questions (line 35). | not a rubric, not a manuscript outline, not a report |
| `outline` | The manuscript **outline**: section structure of the deliverable (lines 27, 35). | not a framework, not a rubric |
| `report` | A **produced output** of the application (e.g., coverage report, synthesis memo, findings report). | not an acquired source document |
| `document` | An **acquired/imported source** (paper, preprint, supplement). Its versions are revisions (line 33). | not a produced report; not a study |
| `study` | A scientific study record that one or more `document`s report. | not a document blob; not a dataset |
| `dataset` | Data underlying one or more documents/analyses. | not a study, not an experiment |
| `experiment` | A specific experiment described by a document/study. | not a study, not a dataset |
| `finding` | A structured extracted finding (with `EvidenceRecord`s), line 32. | not raw text, not a summary paragraph alone |

"**Document version**" is **not** a kind: it is the revision axis of one
`document` artifact (`parent_revision_id`), see §6.2. A `report` and its versions
are likewise revisions of one `report` artifact.

**"Reports" terminology is not lost (repair).** The baseline phrase "linked
reports of the same study" (specification line 33) means **acquired research
reports** — published papers/preprints — which are modeled as `document` artifacts
and related to a `study` by a `DependencyReference` with role `reports`. The
`report` **kind** is reserved for the application's **own produced outputs**
(coverage report, synthesis memo, findings report). So:

- acquired scientific report / paper / preprint ⇒ kind `document` (+ `reports` link);
- application output ⇒ kind `report`.

The two MUST NOT be conflated. Dependency targets are typed by role (not by the
`report` kind):

- `reports` targets a **`study`** (a `document` reports a study);
- `uses_dataset` targets a **`dataset`**;
- `describes_experiment` targets an **`experiment`**.

A `reports`/`uses_dataset` dependency MUST NOT target a `report` (app output).

---

## 5. Record definitions (v1.1 proposals)

Field tables use `Req` = mandatory; `Opt` = optional. A mandatory field whose
value is unavailable takes a §2.2 state; if the contract requires `known` and the
value is `missing`/`unknown`, the record is `blocked` (§2.3).

### 5.1 `ArtifactRecord`

| Field | Type | Req | Meaning |
| --- | --- | --- | --- |
| `project_id` | string | Req | Owning project (RefProject). |
| `artifact_id` | string | Req | Stable logical identity within the project (§3.2). |
| `kind` | enum[rubric\|framework\|outline\|report\|document\|study\|dataset\|experiment\|finding] | Req | §4 taxonomy. |
| `created_at` | timestamp | Req | When the artifact identity was created. |
| `created_by` | enum[user\|agent\|import] | Req | Actor class that created it. |
| `label` | string | Opt | Human title. **Not identity.** |
| `status` | enum[active\|superseded\|withdrawn] | Opt (default `active`) | Lifecycle only; does not alter revision immutability. |
| `revision_ids` | list<string> | Req | Ordered revision ids known for this artifact (may grow; existing entries immutable). |

### 5.2 `RevisionRecord`

A `RevisionRecord` represents one **immutable revision with acquired/source
content**. If no bytes/content were acquired, **there is no `RevisionRecord`**;
use an `ImportOccurrenceRecord` (§5.8) instead — do not fabricate a content
revision to stand in for missing full text.

| Field | Type | Req | Meaning |
| --- | --- | --- | --- |
| `project_id`,`artifact_id`,`revision_id` | RefRevision | Req | Immutable identity of this revision (§3.2). |
| `parent_revision_id` | stated<ref<RevisionRecord>> | Req | Revision derived from; `not_applicable` **only** for a first revision that is not derived from another content revision. |
| `content_hash` | stated<hash{alg}> | Req | Integrity key (§3.3). This record exists only because content was acquired, so this field MUST be `known`; it is **not N/A-eligible**. `unknown` blocks evidence use. |
| `hash_algorithm` | enum[sha256\|sha512] | Req | Algorithm of `content_hash`; MUST equal the `alg` part of the hash string. |
| `byte_size` | int | Req | Size in bytes (bytes were acquired, §3.3). |
| `media_type` | string | Opt | e.g., `application/pdf`; advisory, not identity. |
| `origin_locator` | string | Req | Where the bytes came from (path/URL/import batch). Provenance, not identity. |
| `acquired_at` | timestamp | Req | Acquisition time. |
| `acquired_by` | enum[user\|agent\|import] | Req | Actor class. |
| `dependencies` | list<ref<DependencyReference>> | Req (may be empty) | **Immutable dependency set** this revision was produced under (e.g., `version_of`, `extracted_from`, `reports`). This is the set approvals bind to (I1); each entry MUST reference a `DependencyReference`. It MAY later gain entries only by creating a **new** revision, never by mutation. |
| `provenance_id` | ref<ProvenanceRecord> | Req | How this revision came to exist (§5.3). |
| `rights_ref` | `RightsRecordRef` (§2.4) | Req | Pinned reference to the **canonical** policy `RightsRecord` ([`03-policy-decisions.md` §5](03-policy-decisions.md#51-rightsrecord-v1--canonical-immutable-identity-and-typing); §5.6). Every revision MUST reference one, including our own reports that embed or derive from third-party content. **No blanket own-work N/A:** derived "own" artifacts still inherit the source's restrictions on storage/redistribution/hosted transfer (§5.6). |
| `immutable` | bool (constant `true`) | Req | Declaration that this revision is immutable (I1/I2). |
| `notes` | string | Opt | Free text. |

### 5.3 `ProvenanceRecord` (how a value/record came to exist)

| Field | Type | Req | Meaning |
| --- | --- | --- | --- |
| `provenance_id` | string | Req | Identity. |
| `project_id` | string | Req | Owning project. |
| `subject` | `SubjectRef` | Req | The record/value this provenance describes (§2.4). |
| `method` | enum[human\|model\|import\|derived] | Req | How it was produced. |
| `actor` | string | Req | Who/what performed it (user id, agent role, import tool). |
| `model_ref` | `ModelRef` | Req when `method=model` | Model/agent + instruction revision used; treated as untrusted data (I11). |
| `inputs` | list<RefRevision> | Req (may be empty) | Input revisions consumed. |
| `performed_at` | timestamp | Req | When. |
| `limitations` | stated<string> | Req | Known limits of this production step. |

### 5.4 `EvidenceRecord` (the evidence/attribution unit, I4/I8/I9/I10)

Every evidence record MUST carry: an original locator, context, attribution,
limitations, source assets, a rights reference, and revision linkage.

| Field | Type | Req | Meaning |
| --- | --- | --- | --- |
| `evidence_id` | string | Req | Identity. |
| `project_id` | string | Req | Owning project. |
| `target_finding_revision` | RefRevision (kind `finding`) | Req | **Revision linkage (dependent):** the finding revision this evidence supports. |
| `source_revision` | stated<RefRevision> (kind `document`) | Req | **Revision linkage (provider):** the document revision actually read. If full text was never acquired, state `missing` (see §7.5). |
| `original_locator` | stated<`LocatorValue`> | Req | Where in the **original** the evidence sits (§2.4). |
| `passage` | stated<string> | Req | Verbatim original excerpt; `not_applicable` allowed only when the evidence is a figure/table/supplement carried in `source_assets`. |
| `context` | stated<string> | Req | Surrounding context (species/population/assay/outcome and comparable conditions where relevant). |
| `attribution` | stated<enum[direct_observation\|author_interpretation\|secondary_account\|mixed\|unclear\|proposed_synthesis]> | Req | Attribution class (I8). |
| `limitations` | stated<string> | Req | Known limits/caveats; "none identified" must be explicit. |
| `source_assets` | list<`AssetRef`> | Req (may be empty) | Figures/tables/supplements that are inputs (line 19; §2.4). |
| `rights_reference` | stated<`RightsRecordRef`> | Req | Pinned rights record for the asset(s) read (I9; §5.6). |
| `numeric_details` | Opt<object> | Opt | Numbers/units where relevant (line 32). |
| `extraction_provenance` | ref<ProvenanceRecord> | Req | Who/what/how extracted. |
| `secondary_warning` | stated<bool> | Req | `known:true` when attribution is limited by a tracing limit; then final human approval is required and this MUST NOT be presented as verified primary support (I10). |

### 5.5 `DependencyReference`

| Field | Type | Req | Meaning |
| --- | --- | --- | --- |
| `dependency_id` | string | Req | Identity. |
| `project_id` | string | Req | Owning project. |
| `from_revision` | RefRevision | Req | The dependent (consumer) revision. |
| `target` | `DependencyTarget` = `{to_revision: stated<RefRevision>, to_artifact: RefArtifact}` | Req | The provider. `to_revision` `missing`/`unknown` is allowed only to record an unresolved dependency; it blocks dependent behavior, and `to_artifact` keeps it locatable. |
| `role` | enum[version_of\|reports\|uses_dataset\|describes_experiment\|extracted_from\|derived_from\|summarizes\|supports_claim\|duplicate_bytes_of] | Req | Relation kind: `version_of` = same artifact's older revision (§6.2); `reports` targets a `study` (the baseline "linked reports of one study", §4); `uses_dataset` targets a `dataset`; `describes_experiment` targets an `experiment`; `duplicate_bytes_of` = the `from_revision`'s bytes equal the provider revision's bytes (dedup link between two **pre-existing** revisions, §6.1 R1) — a later duplicate *import* is an `ImportOccurrenceRecord` (§5.8), not a new revision and not a role on a revision. |
| `created_at` | timestamp | Req | When declared. |
| `created_by` | enum[user\|agent\|import] | Req | Actor class. |
| `state` | §2.2 token | Req | Overall resolution state. |

### 5.6 `RightsReference` — projection of the canonical rights record (I9; P5 open)

C01 does **not** define a canonical rights schema. The canonical record is
`RightsRecord` v1 in the sibling policy contract
[`03-policy-decisions.md` §5.1](03-policy-decisions.md#51-rightsrecord-v1--canonical-immutable-identity-and-typing)
(C03, part of this coordinated proposal bundle). C01 holds a **read-only
projection** of that canonical record and never re-decides rights. The shared
operation enum is `allowed | not_allowed | unknown | not_applicable`.

**Pinned reference — `RightsRecordRef` (§2.4).** A reference from C01 to a
canonical rights record MUST be pinned to the record's **immutable identity**,
never a mutable bare `rights_id`:

```
RightsRecordRef = {project_id: string, rights_id: string, record_revision: int}
```

All three components MUST match the canonical record's identity
(`{project_id, rights_id, record_revision}`; C03 §5.1). `record_revision` is
immutable, so editing a rights record produces a **new** `record_revision` that a
stale reference does not resolve (I1). A reference used by a record MUST be
**same-project** as that record.

The fields below are the **read-only projection** C01 keeps for identity/evidence
references:

| Projection field (C01) | Type | Req | Canonical `RightsRecord` field |
| --- | --- | --- | --- |
| `rights_id` | string | Req | `rights_id` (same id) |
| `project_id` | string | Req | `project_id` |
| `record_revision` | int | Req | `record_revision` (immutable identity pin) |
| `subject` | object (canonical `subject`) | Req | `subject` — the canonical **discriminated union** (`kind` ∈ `source_revision` \| `subject_artifact_ref`), **not** a bare `source_revision` |
| `acquisition_right` | enum[allowed\|not_allowed\|unknown\|not_applicable] | Req | `operations.acquisition` |
| `storage_right` | enum[allowed\|not_allowed\|unknown\|not_applicable] | Req | `operations.storage` |
| `redistribution_right` | enum[allowed\|not_allowed\|unknown\|not_applicable] | Req | `operations.redistribution` |
| `hosted_model_transfer_right` | enum[allowed\|not_allowed\|unknown\|not_applicable] | Req | `operations.hosted_transfer` |
| `basis` | stated<string> | Req | summary of the per-operation `basis.<operation>.{kind, evidence:{ref, provenance}}` |
| `restrictions` | stated<string> | Req | `restrictions[]` |
| `decided_by`,`decided_at` | string, timestamp | Req | `recorded_by`, `recorded_at` |
| — (canonical-only, not projected here) | — | — | `authorization`, `privacy`, `derived_status` |

Rules (the projection adds no rights; the policy record is authoritative):

- The four operations are **separate** (I9). `acquisition_right` MAY be
  `not_applicable` on a derived finding: producing the finding performed no
  acquisition operation **even when the finding embeds a third-party excerpt**.
- `not_applicable` MUST NOT be used to escape an operation that actually
  happened. For a derived artifact that embeds or derives from third-party
  content, `storage_right`, `redistribution_right`, and
  `hosted_model_transfer_right` **inherit** the source record's restrictions and
  MUST NOT be set to `not_applicable` merely because the artifact is "ours".
- `unknown` **blocks** the affected operation. In particular, if
  `hosted_model_transfer_right` is `unknown`, no hosted transfer may be attempted;
  extraction must use a **local model or a human**. P5 remains open (§8).

**Constant reminder:** uploading a file is NOT proof of any of these rights; each
operation is decided independently.

### 5.7 `OmissionCheckRecord` (does not imply completeness, I7)

| Field | Type | Req | Meaning |
| --- | --- | --- | --- |
| `omission_check_id` | string | Req | Identity. |
| `project_id` | string | Req | Owning project. |
| `scope` | `{revision_set: list<RefRevision>, sample_rule: stated<RuleRef>}` | Req | What was checked; `sample_rule` points to an approved rule revision (§8), not an invented number (§2.4). |
| `check_kind` | enum[random\|risk_based] | Req | Audit type (line 31). |
| `method_ref` | ref<ProvenanceRecord> | Req | How performed. |
| `performed_by`,`performed_at` | string, timestamp | Req | Who and when. |
| `result` | enum[no_omission_observed\|omission_found\|not_performed\|unknown] | Req | Outcome. |
| `found_omissions` | list<`OmissionRef`> | Req (may be empty) | Omissions detected (§2.4). |
| `limitations` | stated<string> | Req | Coverage limits of this check. |
| `guarantee` | constant `sampled_audit_not_completeness` | Req | Explicit non-guarantee: a pass does **not** prove completeness. |

### 5.8 `ImportOccurrenceRecord` (acquisition event — **not** a scientific revision)

An import/acquisition **occurrence** is a distinct typed record from a
`RevisionRecord`. It records that an import happened and how it was disposed of,
without inventing a content revision. Its `content_hash` is N/A-eligible because
an occurrence may have no bytes (e.g., missing full text).

| Field | Type | Req | Meaning |
| --- | --- | --- | --- |
| `import_id` | string | Req | Identity of this acquisition event. |
| `project_id` | string | Req | Owning project. |
| `artifact_id` | string | Req | Artifact the occurrence is about (`RefArtifact`). |
| `origin_locator` | string | Req | Where the bytes would come from / were read (path/URL/import batch). |
| `content_hash` | stated<hash{alg}> | Req | Observed bytes' hash when bytes were read; `not_applicable` (with reason) when no bytes were acquired. N/A-eligible. |
| `hash_algorithm` | enum[sha256\|sha512] | Req when `content_hash` known | Algorithm. |
| `byte_size` | int | Req when bytes read | Size in bytes. |
| `media_type` | string | Opt | e.g., `application/pdf`. |
| `acquired_at` | timestamp | Req | When the occurrence happened. |
| `acquired_by` | enum[user\|agent\|import] | Req | Actor class. |
| `disposition` | enum[created_new_revision\|duplicate_bytes_of_existing_revision\|no_full_text_acquired\|rejected] | Req | What happened. |
| `result_revision_id` | stated<ref<RevisionRecord>> | Req | The revision created (disposition `created_new_revision`) or **matched** (disposition `duplicate_bytes_of_existing_revision`); `missing` when `no_full_text_acquired`. |
| `rights_ref` | `RightsRecordRef` (§2.4) | Req | Pinned reference to the canonical policy `RightsRecord` (§5.6); no blanket own-work N/A. |
| `provenance_id` | ref<ProvenanceRecord> | Req | Provenance of the occurrence. |

Invariant: a `duplicate_bytes_of_existing_revision` occurrence MUST NOT create a
new `RevisionRecord`; it points at the existing revision (§5.2, §6.1 R1).

### 5.9 `IndependenceRecord` (affirmative, provenance-backed adjudication — R4)

Independence of two evidence records is **never** inferred from distinct ids or
files; it is an explicit adjudicated claim.

| Field | Type | Req | Meaning |
| --- | --- | --- | --- |
| `independence_id` | string | Req | Identity. |
| `project_id` | string | Req | Owning project. |
| `evidence_a`,`evidence_b` | `EvidenceRef` = `{target_finding_revision: RefRevision (kind `finding`), evidence_id?: string}` | Req | The two compared units, named by their **finding revisions** (an optional `evidence_id` pins a specific `EvidenceRecord`). |
| `underlying_a`,`underlying_b` | RefArtifact (kind `study` \| `dataset` \| `experiment`) | Req | The underlying study/dataset/experiment each unit is attributed to. |
| `conclusion` | enum[independent\|not_independent\|unknown] | Req | Adjudicated result. |
| `basis` | stated<string> | Req | The affirmative provenance-backed ground (shared vs distinct data source, overlap, etc.). |
| `adjudicated_by` | `ActorRef` = `{actor_kind: enum[user\|agent], actor_id: string, confirmation_ref: stated<RefRevision>}` | Req | Who adjudicated. For `actor_kind:agent`, `confirmation_ref` MUST be `known` (a **human-confirmation** revision); agent output is untrusted until confirmed (I11). For `actor_kind:user`, `confirmation_ref` is `not_applicable` with a reason. |
| `adjudicated_at` | timestamp | Req | When. |
| `provenance_id` | ref<ProvenanceRecord> | Req | Inputs/sources examined. |
| `limitations` | stated<string> | Req | Known limits. |

Rules:

- `conclusion:independent` requires the `ActorRef` to be human, or agent-produced
  **and** human-confirmed (`confirmation_ref` `known`).
- **Shared underlying data is a hard prohibition.** If `underlying_a` and
  `underlying_b` are the same `study`/`dataset` (or the sharedness is `unknown`),
  the conclusion MUST be `not_independent` or `unknown` — never `independent`.
- Distinct `document` artifacts, artifact ids, or revisions are **never**
  sufficient on their own: `conclusion:independent` requires `basis` to affirm
  **demonstrably distinct** underlying data **and** distinct source documents.
  Distinct experiments/analyses (e.g., separate `experiment` artifacts) still
  require an affirmative adjudication — there is no default or bypass.
- Downstream text MAY claim "independent support" only when an `IndependenceRecord`
  with `conclusion:independent` exists (R4, I6).

---

## 6. Identity relations — duplicate bytes, alternate versions, one dataset

### 6.1 Rules

- **R1 — Exact duplicate bytes (import occurrence ≠ scientific revision).** Two
  revisions with equal `content_hash`, `hash_algorithm`, `byte_size`, and
  `media_type` have the same bytes. Represent **one** stored revision. A later
  import of those same bytes is an `ImportOccurrenceRecord` (§5.8) with
  `disposition:duplicate_bytes_of_existing_revision` pointing at the existing
  revision — it MUST NOT create a second `RevisionRecord`. If two pre-existing
  revisions are found to share bytes, link them with a `duplicate_bytes_of`
  `DependencyReference`. Do **not** create an independent artifact or evidence
  source.
- **R2 — Alternate document version.** Revisions of one `document` artifact linked
  by `parent_revision_id` / role `version_of` (e.g., preprint → published PDF).
  These are distinct revisions; a change may alter findings. New evidence creates
  an explicit revision and reopens affected approvals without erasing unaffected
  work (I5).
- **R3 — Multiple reports of one study/dataset.** Distinct `document` artifacts
  (acquired research reports, §4) that each declare role `reports` toward the same
  `study` revision, or `uses_dataset` toward the same `dataset` revision. They are
  **not** independent confirmation (I6).
- **R4 — No false independent support.** Two evidence records count as
  independent support **only** when an `IndependenceRecord` (§5.9) with
  `conclusion:independent` exists, adjudicated on a provenance-backed basis that
  the underlying data are demonstrably distinct. Distinct `document` artifacts,
  artifact ids, or revisions are **never** sufficient on their own. **Sharing
  underlying data is a hard prohibition:** when the two units share a
  `study`/`dataset` (or sharedness is `unknown`), the conclusion MUST be
  `not_independent`/`unknown`, and downstream text MUST NOT then claim
  independent support. Distinct `experiment`/analysis artifacts likewise require
  an affirmative adjudication — there is no automatic or bypass independence.

### 6.2 Distinguishing the three relations

| Case | Identity | Link | Independent support? |
| --- | --- | --- | --- |
| duplicate bytes | one stored revision; later import is an occurrence | `ImportOccurrenceRecord` (disposition `duplicate_bytes_of_existing_revision`), else `duplicate_bytes_of` dependency | No |
| alternate version | same `artifact_id`, different `revision_id` | `version_of` | No |
| linked reports of one study | different `document` `artifact_id`, one shared `study`/`dataset` | `reports` | **Never** — shared underlying data cannot be independent (R3/R4); `conclusion:independent` is prohibited |
| candidate distinct evidence | different `artifact_id`, different (non-shared) study/dataset, or distinct experiments | — | Only with an affirmative `IndependenceRecord conclusion:independent` (§5.9, human-confirmed if agent-produced); otherwise `unknown`. Distinct ids alone never suffice |

---

## 7. Worked examples (synthetic)

All ids/values are synthetic. Each example names its outcome and the invariants
it exercises. Examples use `P-0001` as the project.

**Reading note.** Except §7.0, the code blocks below are **abridged projections**
(fields elided with `…` or omitted) chosen to isolate one issue; they are **not**
complete accepted records and their `…` elisions are not valid values. §7.0 is a
**complete conforming** record set in real JSON, checked by the inline validator
printed there.

### 7.0 Complete conforming record set — validity anchor (parseable)

A source document acquired once (with rights, provenance, and its acquisition
occurrence), a derived finding revision, the dependency between them, and the
evidence record linking finding to source. Every mandatory §5 field is present
with a valid type/state; **outcome `accepted`** for the set.

Hashes are **synthetic digest fixtures** (valid 64-hex `sha256` strings over
invented bytes) — they assert nothing about any real paper's integrity. The
`source_assets` list is empty (no `FIG1`); the passage/locator/context are one
matching synthetic sentence. The block is real JSON, and the inline validator
below parses it and checks required keys, `stated` shapes, hash format, enum
values, and record-reference resolution.

<!-- C7.0-JSON-BEGIN -->
```json
{
  "artifact_records": [
    {"project_id": "P-0001", "artifact_id": "A1", "kind": "document",
     "created_at": "2026-10-27T10:00:00Z", "created_by": "import",
     "label": "syn-source-1", "status": "active", "revision_ids": ["R1"]},
    {"project_id": "P-0001", "artifact_id": "F1", "kind": "finding",
     "created_at": "2026-10-27T11:00:00Z", "created_by": "agent",
     "label": "syn-source-1 finding 1", "status": "active", "revision_ids": ["f1"]}
  ],
  "revision_records": [
    {"project_id": "P-0001", "artifact_id": "A1", "revision_id": "R1",
     "parent_revision_id": {"state": "not_applicable", "reason": "first content revision"},
     "content_hash": {"state": "known", "value": "sha256:3c0874e293b210d1dc68326d6fc3d2057c0c3f147b9fb4f9d097ec3aa184e2b6"},
     "hash_algorithm": "sha256", "byte_size": 812345, "media_type": "application/pdf",
     "origin_locator": "import:batch-1/a.pdf", "acquired_at": "2026-10-27T10:00:01Z",
     "acquired_by": "import", "dependencies": [], "provenance_id": "PV1",
     "rights_ref": {"project_id": "P-0001", "rights_id": "RT1", "record_revision": 1}, "immutable": true},
    {"project_id": "P-0001", "artifact_id": "F1", "revision_id": "f1",
     "parent_revision_id": {"state": "not_applicable", "reason": "first content revision"},
     "content_hash": {"state": "known", "value": "sha256:9d88eb16b7efd644083b219334f91c6fc8524f933c2db92295da2b6d7c5bd09b"},
     "hash_algorithm": "sha256", "byte_size": 2048, "media_type": "application/json",
     "origin_locator": "store:finding/F1/f1", "acquired_at": "2026-10-27T11:00:00Z",
     "acquired_by": "agent", "dependencies": ["DP1"], "provenance_id": "PV2",
     "rights_ref": {"project_id": "P-0001", "rights_id": "RT2", "record_revision": 1}, "immutable": true}
  ],
  "import_occurrence_records": [
    {"import_id": "IM1", "project_id": "P-0001", "artifact_id": "A1",
     "origin_locator": "import:batch-1/a.pdf",
     "content_hash": {"state": "known", "value": "sha256:3c0874e293b210d1dc68326d6fc3d2057c0c3f147b9fb4f9d097ec3aa184e2b6"},
     "hash_algorithm": "sha256", "byte_size": 812345, "media_type": "application/pdf",
     "acquired_at": "2026-10-27T10:00:01Z", "acquired_by": "import",
     "disposition": "created_new_revision",
     "result_revision_id": {"state": "known", "value": "R1"},
     "rights_ref": {"project_id": "P-0001", "rights_id": "RT1", "record_revision": 1}, "provenance_id": "PV1"}
  ],
  "provenance_records": [
    {"provenance_id": "PV1", "project_id": "P-0001",
     "subject": {"project_id": "P-0001", "artifact_id": "A1", "revision_id": "R1"},
     "method": "import", "actor": "import-tool", "inputs": [],
     "performed_at": "2026-10-27T10:00:01Z",
     "limitations": {"state": "known", "value": "import step only; content not yet extracted"}},
    {"provenance_id": "PV2", "project_id": "P-0001",
     "subject": {"record_type": "RevisionRecord", "record_id": "f1"},
     "method": "model", "actor": "local-extractor-agent",
     "model_ref": {"agent": "extractor", "model": "syn-local-model-A", "instructions_revision": "instr-7"},
     "inputs": [{"project_id": "P-0001", "artifact_id": "A1", "revision_id": "R1"}],
     "performed_at": "2026-10-27T11:00:00Z",
     "limitations": {"state": "known", "value": "local synthetic model run; no hosted transfer (RT1.hosted_transfer=unknown)"}}
  ],
  "dependency_references": [
    {"dependency_id": "DP1", "project_id": "P-0001",
     "from_revision": {"project_id": "P-0001", "artifact_id": "F1", "revision_id": "f1"},
     "target": {
       "to_revision": {"state": "known", "value": {"project_id": "P-0001", "artifact_id": "A1", "revision_id": "R1"}},
       "to_artifact": {"project_id": "P-0001", "artifact_id": "A1"}},
     "role": "extracted_from", "created_at": "2026-10-27T11:00:00Z",
     "created_by": "agent", "state": "known"}
  ],
  "evidence_records": [
    {"evidence_id": "EV1", "project_id": "P-0001",
     "target_finding_revision": {"project_id": "P-0001", "artifact_id": "F1", "revision_id": "f1"},
     "source_revision": {"state": "known", "value": {"project_id": "P-0001", "artifact_id": "A1", "revision_id": "R1"}},
     "original_locator": {"state": "known", "value": {"kind": "page", "value": "p.4"}},
     "passage": {"state": "known", "value": "In the synthetic assay, compound Z reduced signal Y under condition C1."},
     "context": {"state": "known", "value": "synthetic in vitro assay; cell line SYN-1; endpoint signal Y"},
     "attribution": {"state": "known", "value": "author_interpretation"},
     "limitations": {"state": "known", "value": "single synthetic condition; no in vivo analogue"},
     "source_assets": [],
     "rights_reference": {"state": "known", "value": {"project_id": "P-0001", "rights_id": "RT1", "record_revision": 1}},
     "extraction_provenance": "PV2",
     "secondary_warning": {"state": "known", "value": false}}
  ],
  "rights_references": [
    {"rights_id": "RT1", "project_id": "P-0001", "record_revision": 1,
     "subject": {"kind": "source_revision", "project_id": "P-0001", "artifact_id": "A1", "revision_id": "R1"},
     "acquisition_right": "allowed", "storage_right": "allowed",
     "redistribution_right": "not_allowed", "hosted_model_transfer_right": "unknown",
     "basis": {"state": "known", "value": "synthetic human grant (grant:syn-grant-1): local processing only"},
     "restrictions": {"state": "known", "value": "local_processing_only; no_redistribution; no_hosting"},
     "decided_by": "user", "decided_at": "2026-10-27T10:05:00Z"},
    {"rights_id": "RT2", "project_id": "P-0001", "record_revision": 1,
     "subject": {"kind": "source_revision", "project_id": "P-0001", "artifact_id": "F1", "revision_id": "f1"},
     "acquisition_right": "not_applicable", "storage_right": "allowed",
     "redistribution_right": "not_allowed", "hosted_model_transfer_right": "unknown",
     "basis": {"state": "known", "value": "derived finding; inherits RT1 restrictions"},
     "restrictions": {"state": "known", "value": "inherits RT1: local_processing_only; no_redistribution; no_hosting"},
     "decided_by": "user", "decided_at": "2026-10-27T11:05:00Z"}
  ]
}
```
<!-- C7.0-JSON-END -->

**Inline validator** (stdlib only; no production code file). It reads the JSON
block marked above from this document and validates the record set:

<!-- C7.0-VALIDATOR-BEGIN -->
```python
import json, re, sys

REQUIRED = {
 "artifact_records": {"project_id","artifact_id","kind","created_at","created_by","revision_ids"},
 "revision_records": {"project_id","artifact_id","revision_id","parent_revision_id","content_hash",
   "hash_algorithm","byte_size","origin_locator","acquired_at","acquired_by","dependencies",
   "provenance_id","rights_ref","immutable"},
 "import_occurrence_records": {"import_id","project_id","artifact_id","origin_locator","content_hash",
   "acquired_at","acquired_by","disposition","result_revision_id","rights_ref","provenance_id"},
 "provenance_records": {"provenance_id","project_id","subject","method","actor","inputs",
   "performed_at","limitations"},
 "dependency_references": {"dependency_id","project_id","from_revision","target","role",
   "created_at","created_by","state"},
 "evidence_records": {"evidence_id","project_id","target_finding_revision","source_revision",
   "original_locator","passage","context","attribution","limitations","source_assets",
   "rights_reference","extraction_provenance","secondary_warning"},
 "rights_references": {"rights_id","project_id","record_revision","subject","acquisition_right","storage_right",
   "redistribution_right","hosted_model_transfer_right","basis","restrictions","decided_by","decided_at"},
}
KINDS = {"rubric","framework","outline","report","document","study","dataset","experiment","finding"}
OPS   = {"allowed","not_allowed","unknown","not_applicable"}
STATE = {"known","missing","unknown","not_applicable"}
HASH  = re.compile(r"^sha256:[0-9a-f]{64}$")
HOSTED= re.compile(r"hosted|openrouter|remote|cloud", re.I)
errors = []

def check_stated(path, v):
    if set(v) - {"state","value","reason"}:
        errors.append(f"{path}: unexpected keys {sorted(set(v)-{'state','value','reason'})}")
    st = v.get("state")
    if st not in STATE:
        errors.append(f"{path}: bad state {st!r}"); return
    if st == "known":
        if "value" not in v: errors.append(f"{path}: known without value")
    else:
        if "value" in v: errors.append(f"{path}: non-known {st} carries value")
        if not (isinstance(v.get("reason"), str) and v["reason"]):
            errors.append(f"{path}: {st} needs non-empty reason")

def walk(obj, path=""):
    if isinstance(obj, dict):
        if obj.get("state") in STATE and set(obj) <= {"state","value","reason"}:
            check_stated(path, obj); return
        for k, val in obj.items(): walk(val, f"{path}.{k}" if path else k)
    elif isinstance(obj, list):
        for i, el in enumerate(obj): walk(el, f"{path}[{i}]")

def refrev(r): return (r["project_id"], r["artifact_id"], r["revision_id"])

def refrights(r): return (r["project_id"], r["rights_id"], r["record_revision"])

def main(path):
    doc = open(path, encoding="utf-8").read()
    m = re.search(r"<!--\s*C7\.0-JSON-BEGIN\s*-->(.*?)<!--\s*C7\.0-JSON-END\s*-->", doc, re.S)
    if not m: print("FAIL: JSON markers not found"); sys.exit(1)
    data = json.loads(re.search(r"```json\s*(.*?)```", m.group(1), re.S).group(1))

    for key, req in REQUIRED.items():
        for i, rec in enumerate(data[key]):
            miss = req - set(rec)
            if miss: errors.append(f"{key}[{i}] missing keys {sorted(miss)}")
    for i, a in enumerate(data["artifact_records"]):
        if a["kind"] not in KINDS: errors.append(f"artifact_records[{i}].kind invalid {a['kind']!r}")
    for k, v in data.items(): walk(v, k)
    for k in ("revision_records", "import_occurrence_records"):
        for i, r in enumerate(data[k]):
            h = r["content_hash"]
            if h.get("state") == "known" and not HASH.match(str(h.get("value", ""))):
                errors.append(f"{k}[{i}].content_hash is not 64-hex sha256")
            if "hash_algorithm" in r and r["hash_algorithm"] not in ("sha256","sha512"):
                errors.append(f"{k}[{i}].hash_algorithm invalid")
    for i, r in enumerate(data["rights_references"]):
        for op in ("acquisition_right","storage_right","redistribution_right","hosted_model_transfer_right"):
            if r[op] not in OPS: errors.append(f"rights_references[{i}].{op} invalid enum {r[op]!r}")
        if not isinstance(r["record_revision"], int) or isinstance(r["record_revision"], bool):
            errors.append(f"rights_references[{i}].record_revision must be an int identity pin")
        subj = r["subject"]
        if not isinstance(subj, dict) or subj.get("kind") not in ("source_revision","subject_artifact_ref"):
            errors.append(f"rights_references[{i}].subject is not the canonical discriminated union")
        elif subj.get("project_id") != r["project_id"]:
            errors.append(f"rights_references[{i}].subject cross-project {subj.get('project_id')!r}")

    prov  = {p["provenance_id"] for p in data["provenance_records"]}
    rights= {refrights(r) for r in data["rights_references"]}
    deps  = {d["dependency_id"] for d in data["dependency_references"]}
    revs  = {refrev(r) for r in data["revision_records"]}
    arts  = {(a["project_id"], a["artifact_id"]) for a in data["artifact_records"]}

    for i, r in enumerate(data["revision_records"]):
        if r["provenance_id"] not in prov: errors.append(f"revision_records[{i}].provenance_id unresolved")
        if refrights(r["rights_ref"]) not in rights: errors.append(f"revision_records[{i}].rights_ref unresolved")
        for d in r["dependencies"]:
            if d not in deps: errors.append(f"revision_records[{i}].dependencies {d!r} unresolved")
    for i, r in enumerate(data["artifact_records"]):
        for rid in r["revision_ids"]:
            if (r["project_id"], r["artifact_id"], rid) not in revs:
                errors.append(f"artifact_records[{i}].revision_ids {rid!r} unresolved")
    for i, o in enumerate(data["import_occurrence_records"]):
        if refrights(o["rights_ref"]) not in rights: errors.append(f"import_occurrence_records[{i}].rights_ref unresolved")
        if o["provenance_id"] not in prov: errors.append(f"import_occurrence_records[{i}].provenance_id unresolved")
        rv = o["result_revision_id"]
        if rv.get("state") == "known" and (o["project_id"], o["artifact_id"], rv["value"]) not in revs:
            errors.append(f"import_occurrence_records[{i}].result_revision_id unresolved")
    for i, d in enumerate(data["dependency_references"]):
        if refrev(d["from_revision"]) not in revs: errors.append(f"dependency_references[{i}].from_revision unresolved")
        if (d["target"]["to_artifact"]["project_id"], d["target"]["to_artifact"]["artifact_id"]) not in arts:
            errors.append(f"dependency_references[{i}].to_artifact unresolved")
        tr = d["target"]["to_revision"]
        if tr.get("state") == "known" and refrev(tr["value"]) not in revs:
            errors.append(f"dependency_references[{i}].to_revision unresolved")
    for i, e in enumerate(data["evidence_records"]):
        if refrev(e["target_finding_revision"]) not in revs: errors.append(f"evidence_records[{i}].target_finding_revision unresolved")
        sr = e["source_revision"]
        if sr.get("state") == "known" and refrev(sr["value"]) not in revs:
            errors.append(f"evidence_records[{i}].source_revision unresolved")
        rr = e["rights_reference"]
        if rr.get("state") == "known" and refrights(rr["value"]) not in rights:
            errors.append(f"evidence_records[{i}].rights_reference unresolved")
        if e["extraction_provenance"] not in prov: errors.append(f"evidence_records[{i}].extraction_provenance unresolved")

    if any(r["hosted_model_transfer_right"] == "unknown" for r in data["rights_references"]):
        for i, p in enumerate(data["provenance_records"]):
            if p["method"] == "model":
                if "model_ref" not in p: errors.append(f"provenance_records[{i}] model without model_ref")
                elif HOSTED.search(p["model_ref"].get("model","")):
                    errors.append(f"provenance_records[{i}] hosted model used while hosted_transfer is unknown")

    if errors:
        print("INVALID:"); [print(" -", e) for e in errors]; sys.exit(1)
    print(f"OK: §7.0 record set valid ({sum(len(v) for v in data.values())} records)")

if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "docs/contracts/01-identities.md")
```
<!-- C7.0-VALIDATOR-END -->

- **Outcome:** `accepted`. Every mandatory field above has a declared type and a
  valid state; the inline validator passes (required keys, `stated` shapes,
  hash format, operation enums, and all record references resolve). `RT2` exists
  because `F1` embeds content derived from `RT1` (no blanket own-work N/A); the
  extraction uses a **local** synthetic model since `hosted_transfer` is
  `unknown`.

### 7.1 Duplicate PDF — valid

Two imports of "Smith 2020" as `a.pdf` and `b.pdf` with identical bytes.

```
ArtifactRecord{ project_id:"P-0001", artifact_id:"A1", kind:"document", ... }
RevisionRecord{ project_id:"P-0001", artifact_id:"A1", revision_id:"R1",
  content_hash:{state:"known", value:"sha256:aaaa…"}, hash_algorithm:"sha256",
  byte_size:812345, media_type:"application/pdf",
  origin_locator:"import:batch-1/a.pdf", ... }
# second import of the SAME bytes → an occurrence, NOT a second revision:
ImportOccurrenceRecord{ import_id:"IM2", project_id:"P-0001", artifact_id:"A1",
  origin_locator:"import:batch-2/b.pdf",
  content_hash:{state:"known", value:"sha256:aaaa…"}, hash_algorithm:"sha256",
  byte_size:812345, media_type:"application/pdf",
  acquired_by:"import", disposition:"duplicate_bytes_of_existing_revision",
  result_revision_id:{state:"known", value:"R1"}, ... }
# (no RevisionRecord R2 exists)
EvidenceRecord{ target_finding_revision:RefRevision{F1,r1},
  source_revision:{state:"known", value:RefRevision{A1,R1}}, ... }
```

- **Outcome:** `accepted`. **One** stored scientific revision `R1`; the second
  import is an `ImportOccurrenceRecord`, not a second revision and not a new
  artifact.
- **Invariants:** §3.3 (hash is not identity), R1 (occurrence ≠ revision), R4 (no
  independent support), I7.

### 7.2 Duplicate PDF — invalid / missing

(a) A revision with no hash, and (b) a synthesis that counts duplicates as
independent.

```
RevisionRecord{ project_id:"P-0001", artifact_id:"A1", revision_id:"R3",
  byte_size:812345, media_type:"application/pdf", ... }
# (a) content_hash is absent for a revision whose bytes were acquired

report_claim{ text:"two independent papers agree",
  sources:[RefRevision{A1,R1}, RefRevision{A1,R1}] }   # same revision, two imports
# (b) no IndependenceRecord{conclusion:"independent"} exists for these units
```

- **Outcome:** (a) `rejected_invalid` — mandatory `content_hash` absent while
  bytes are available; it is **not N/A-eligible** on a `RevisionRecord` (§5.2,
  §2.3). (b) `blocked`/invalid — violates R4/I6: distinct ids, artifact names, or
  repeated imports are **never** independent on their own, and here the source
  revision is literally shared. The claim MUST be corrected or marked not
  independent.
- **Invariants:** §2.3, §3.3, §5.9, R1, R4, I6, I7.

### 7.3 Alternate version — valid

Preprint `R1`, then the published PDF `R2` of the same document `A1`.

```
RevisionRecord{ project_id:"P-0001", artifact_id:"A1", revision_id:"R2",
  parent_revision_id:{state:"known", value:"R1"},
  content_hash:{state:"known", value:"sha256:bbbb…"},
  hash_algorithm:"sha256", ... }
DependencyReference{ dependency_id:"DP3", project_id:"P-0001",
  from_revision:{project_id:"P-0001", artifact_id:"A1", revision_id:"R2"},
  target:{to_revision:{state:"known",
            value:{project_id:"P-0001", artifact_id:"A1", revision_id:"R1"}},
          to_artifact:{project_id:"P-0001", artifact_id:"A1"}},
  role:"version_of", state:"known", ... }
# new evidence extracted from R2:
DependencyReference{ dependency_id:"DP4", project_id:"P-0001",
  from_revision:RefRevision{F1,r1},
  target:{to_revision:{state:"known", value:RefRevision{A1,R2}},
          to_artifact:RefArtifact{A1}},
  role:"extracted_from", ... }
# R2 changes a numeric detail → the finding revision r1 must be superseded by r2,
# and affected approvals reopen (I2/I5).
```

- **Outcome:** `accepted`. `R1` stays immutable; `R2` is a new revision; affected
  approvals bind to the new revision and reopen.
- **Invariants:** §3.2, R2, I1, I2, I5.

### 7.4 Alternate version — invalid / missing

Reusing an approval, or overwriting bytes in place.

```
Approval{ bound_to: RefRevision{A1,R1} }   # then R2 (published) arrives
report uses R2 content while the approval still cites R1   # (a) stale approval
RevisionRecord{ project_id:"P-0001", artifact_id:"A1", revision_id:"R1",
  content_hash:{state:"known", value:"sha256:cccc…"}, ... }
# R1 object mutated in place to the published bytes            # (b) immutability break
```

- **Outcome:** (a) `rejected_invalid`/`blocked` — approval does not cover `R2`;
  material edit invalidates affected checks (I1/I2). (b) `rejected_invalid` — a
  revision's bytes/hash MUST NOT change; a change is a new `revision_id` (§3.2).
- **Invariants:** §3.2, §2.3, I1, I2.

### 7.5 Missing full text — explicit state, not silence

A frozen candidate has no acquired full text. There is **no `RevisionRecord`** for
`A2` — a metadata-only "stub revision" would be a fabricated content revision.
The absence is carried by an `ImportOccurrenceRecord` (§5.8) and by `missing`
states.

```
ArtifactRecord{ project_id:"P-0001", artifact_id:"A2", kind:"document",
  created_at:"2026-10-27T12:00:00Z", created_by:"import",
  status:"active", revision_ids:[] }          # no content revision exists
ImportOccurrenceRecord{ import_id:"IM4", project_id:"P-0001", artifact_id:"A2",
  origin_locator:"candidate:A2 (no source obtained)",
  content_hash:{state:"not_applicable", reason:"no bytes acquired"},
  acquired_at:"2026-10-27T12:00:00Z", acquired_by:"agent",
  disposition:"no_full_text_acquired",
  result_revision_id:{state:"missing", reason:"no full text acquired"},
  rights_ref:RefRights{P-0001,RT3,1}, provenance_id:"PV3" }  # RT3/PV3 omitted here (abridged)
EvidenceRecord{ project_id:"P-0001",
  target_finding_revision:RefRevision{F1,r1},
  source_revision:{state:"missing", reason:"no full text acquired for A2 (frozen candidate)"},
  original_locator:{state:"missing", reason:"no original text read"},
  passage:{state:"missing", reason:"no source text read"},
  context:{state:"unknown", reason:"only abstract-derived, provisional context"},
  attribution:{state:"known", value:"author_interpretation"},
  limitations:{state:"known", value:"abstract-derived; provisional (line 25)"},
  source_assets:[], rights_reference:{state:"known", value:RefRights{P-0001,RT3,1}},
  extraction_provenance:"PV3",
  secondary_warning:{state:"known", value:false} }
DependencyReference{ dependency_id:"DP2", project_id:"P-0001",
  from_revision:RefRevision{F1,r1},
  target:{to_revision:{state:"missing", reason:"no content revision"},
          to_artifact:RefArtifact{A2}},
  role:"extracted_from", created_at:"2026-10-27T12:00:00Z", created_by:"agent",
  state:"missing" }
```

- **Outcome:** `blocked` for scientific digestion of `A2` until a **human
  disposition** (line 29, I3): acquire full text, or explicitly exclude/defer.
  Abstract-derived context is provisional (line 25) and MUST NOT be recorded as
  primary support.
- **Invariants:** §2.2, §5.2 (no revision without content), §5.8, I3, I4, I7.

### 7.6 Omitted result — audit records an omission

Bulk extraction missed a reported null result; the audit finds it.

```
OmissionCheckRecord{ omission_check_id:"OC1", scope:{revision_set:[RefRevision{A1,R1}],
    sample_rule:{state:"known", value:{rule_id:"PR-1", revision_id:"r1"}}},
  check_kind:"risk_based", result:"omission_found",
  found_omissions:[{ref:RefRevision{F1,r1}, description:"reported null result omitted"}],
  limitations:{state:"known", value:"sampled audit; not completeness"},
  guarantee:"sampled_audit_not_completeness" }
```

- **Outcome:** `accepted` as a record; the effect is to **reopen** the affected
  extraction/finding and re-run it. A `no_omission_observed` result MUST NOT be
  reported as "nothing was omitted".
- **Invariants:** §5.7, I7, I2 (affected checks invalidated).

### 7.7 Multiple reports of one dataset — no false independent support

Reports `A10` and `A11` both report the same study `S1`/dataset `D1`.

```
ArtifactRecord{ artifact_id:"S1", kind:"study" }
ArtifactRecord{ artifact_id:"A10", kind:"document" }
ArtifactRecord{ artifact_id:"A11", kind:"document" }
DependencyReference{ from_revision:RefRevision{A10,a1}, to_revision:RefRevision{S1,s1},
  role:"reports", state:"known" }
DependencyReference{ from_revision:RefRevision{A11,a1}, to_revision:RefRevision{S1,s1},
  role:"reports", state:"known" }
# adjudicating independence (required before any "independent" claim):
IndependenceRecord{ independence_id:"IND1", project_id:"P-0001",
  evidence_a:{target_finding_revision:RefRevision{F1,r1}},
  evidence_b:{target_finding_revision:RefRevision{F2,r1}},
  underlying_a:RefArtifact{S1}, underlying_b:RefArtifact{S1},  # same study/dataset
  conclusion:"not_independent",
  basis:{state:"known", value:"both reports share study S1 / dataset D1"},
  adjudicated_by:{actor_kind:"user", actor_id:"user:reviewer-1",
                  confirmation_ref:{state:"not_applicable", reason:"human adjudication"}},
  adjudicated_at:"2026-10-27T12:00:00Z", provenance_id:"PV-IND1", ... }
# PV-IND1 and remaining fields omitted here (abridged)
```

- **Outcome:** `accepted` as relations; a synthesis memo counting `A10`+`A11` as
  **independent** confirmation is `rejected_invalid` (R3/R4, I6). Even without the
  shared-study link, distinct `document` ids alone would still not license an
  independent label — only an `IndependenceRecord` with `conclusion:independent`
  (and a demonstrably distinct underlying source) would. The synthesis MUST
  present the underlying single study and preserve the correlation.
- **Invariants:** §5.9, R3, R4, I6.

---

## 8. Open approvals and ownership (do not silently decide)

This section records that the items below remain **open** and are **not** settled
by this proposal. Owner for all is the **user** unless noted. None are waived.

| Id | Open item | Owner | Blocks (until decided) | Status here |
| --- | --- | --- | --- | --- |
| P1 | Explicit framework contribution, concept-admission/support/revision criteria, testable implications | user | `framework` artifact admission | **open — not accepted** |
| P2 | Detailed downstream causal/context-fidelity checks | user | `context`-field checks | **open — not accepted** |
| P3 | Integrity-status checks (retractions, expressions of concern, corrections) and adjudication | user | source-integrity gating | **open — not accepted** |
| P4 | Public selection/synthesis and limitations reporting alongside the manuscript | user | public artifacts | **open — not accepted** |
| P5 | AI-use declaration, human accountability, provider terms, journal policies | user | `hosted_model_transfer_right` default | **open — not accepted** |
| P6 | Optional target-venue constraint capture and submission-time verification | user | venue constraints | **open — not accepted** |
| A1 | Pilot/audit samples and acceptance rules | user | `OmissionCheckRecord.sample_rule` | **open — no numeric threshold invented** |
| A2 | Issue severities | user | finding severity | **open** |
| A3 | Coverage/stopping rules | user | search freeze | **open** |
| A4 | Cost/concurrency limits | user | budgets | **open** |
| A5 | Tracing hop/source/retry defaults | user | `secondary_warning` triggering | **open** |
| A6 | Explicit acceptance of secondary-origin warnings | user | final approval at a tracing limit | **open (I10 stands regardless)** |

Where a decision is unresolved, the dependent field defaults to `unknown` and
therefore **blocks** dependent behavior (§2.2/§2.3). No numeric scientific
threshold is invented to let a stage proceed.

---

## 9. Versioning and change log

- This is document version **`v1.3`** (`docs/contracts/01-identities.md`).
- Because the contract itself is versioned, a change to any mandatory field,
  type, state, or rule MUST increment the document version and record the change
  below with its date and approver. Field/record semantics are immutable within a
  version once approved.
- Until approved, edits are proposal revisions, not ratified changes.

| Doc version | Date | Change | Approver |
| --- | --- | --- | --- |
| v1 | 2026-10-27 | Initial proposed identity/evidence contract; open items recorded in §8. | **pending (user)** |
| v1.1 | 2026-10-27 | Repair: separate import occurrence from scientific revision (§5.8), coherent missing-full-text representation (no fake revision), `stated<T>`/subtype grammar and a complete conforming record set (§2.4, §7.0), `known` = availability not verification, affirmative `IndependenceRecord` (§5.9), report/scientific-report terminology, no blanket rights N/A (§5.2/§5.6), mandatory revision `dependencies`+`provenance_id` (§5.2). Still **proposed**. | **pending (user)** |
| v1.2 | 2026-10-27 | Repair: §7.0 now parseable JSON with 64-hex synthetic digests, matching synthetic passage/locator/context, empty `source_assets`, and an inline Python validator (required keys, `stated` shapes, hash format, enum values, reference resolution); `IndependenceRecord` uses typed `EvidenceRef` and structured `ActorRef` with mandatory human confirmation for agent conclusions (§2.4, §5.9); `shared` underlying data is a hard no-independence rule (§6.1 R4, §6.2); `reports`→`study`, `uses_dataset`→`dataset` targets fixed (§4, §5.5); `RightsReference` is a **projection** of the canonical `RightsRecord` (`docs/contracts/03-policy-decisions.md` §5) with an explicit field mapping and no blanket own-work N/A (§5.2, §5.6); synthetic rights sample is local-processing-only with an explicit synthetic grant. Still **proposed**; not Step 01 completion. | **pending (user)** |
| v1.3 | 2026-10-27 | Integration alignment: `RightsReference` projection corrected to the canonical fields (`subject` discriminated union, per-operation `basis.<operation>.{kind, evidence:{ref, provenance}}`, `authorization`) — the earlier map named nonexistent `source_revision`/`evidence_ref`/`authorizing_revision` (§5.6); typed pinned **`RightsRecordRef`** `{project_id, rights_id, record_revision}` replaces the mutable bare `rights_id` on `RevisionRecord`/`ImportOccurrenceRecord` and on `EvidenceRecord.rights_reference`, `record_revision` added to the projection, and §7.0 examples + the inline validator resolve the pinned refs and reject cross-project/ill-typed rights refs (§2.4, §5.2, §5.4, §5.6, §5.8, §7.0). Still **proposed**. | **pending (user)** |

---

## 10. Verification hooks for reviewers

- Every mandatory field above carries a type and meaning (§5); §7.0 is a complete
  conforming record set in parseable JSON with mandatory fields and valid
  types/states. `stated<T>` shape and named subtypes are defined in §2.4.
- Run the inline §7.0 validator on this file to confirm required keys, `stated`
  shapes, 64-hex `sha256` format, operation enums, and that every record reference
  resolves: `python3 <extracted-validator-block> docs/contracts/01-identities.md`
  (expected output begins `OK:`).
- Missing/invalid data maps to `rejected_invalid` or `blocked` (§2.3),
  demonstrated in §7.2, §7.4, §7.5.
- The four required examples (duplicate PDF, alternate version, missing full
  text, omitted result) each state an outcome and the invariants walked
  (§7.1–§7.6), plus the required one-dataset case (§7.7).
- All distinctions in implementation `01-contracts.md` acceptance criterion 3 are
  made explicit in §4, §3.2, and §6.2.
- Independence is an affirmative adjudication (§5.9); a `known` field asserts
  availability only (§2.2); no P1–P6 are accepted and no numeric scientific
  threshold is introduced (§8).
