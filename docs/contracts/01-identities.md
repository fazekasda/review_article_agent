# 01 — Identity and evidence contract (v1, **proposed**)

> **Status: PROPOSED — approval pending.** No part of this document is ratified
> science, an approved schema, or a claim of production completion. It drafts the
> identity/evidence contract for review; only the user approves scientific policy
> and the baseline decisions (§8).
>
> **Document version:** `v1`. Versioning rule and change log in §9.
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
| known | `known` | Value is present and asserted by a source/record; usable downstream. |
| missing | `missing` | A required value was **expected** for this record but is **absent**; absence is observed. Blocks dependent behavior until a human disposition. |
| unknown | `unknown` | The value **cannot be determined** from available evidence; **no claim of absence** is made. Blocks dependent behavior until a human disposition. |
| not_applicable | `not_applicable` | The field does not apply to this record kind/context. MUST carry a reason; MUST be used only where this contract marks the field N/A-eligible. |

`missing` and `unknown` MUST NOT be silently defaulted or treated as `known`.

### 2.3 Validation outcomes

A record is validated to exactly one outcome:

| Outcome | Meaning |
| --- | --- |
| accepted | All mandatory fields present with valid types/states; relations resolve. |
| rejected_invalid | A mandatory field is absent, has the wrong type, or a declared relation is violated (e.g., false independence). The record MUST NOT be persisted as usable. |
| blocked | The record is well-formed but a mandatory field is `missing`/`unknown` where the contract requires `known`. Dependent behavior MUST NOT proceed until an explicit human disposition. |

### 2.4 Type grammar

Types are written in a small grammar: `string`, `int`, `bool`, `timestamp`
(RFC 3339, UTC), `enum[a|b|…]`, `hash{alg}`, `list<T>`, `ref<Record>`, and
`stated<T>` meaning a value plus a §2.2 state token. Partial references are
written as `{project_id, artifact_id, revision_id}`.

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

---

## 5. Record definitions (v1 proposals)

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

| Field | Type | Req | Meaning |
| --- | --- | --- | --- |
| `project_id`,`artifact_id`,`revision_id` | RefRevision | Req | Immutable identity of this revision (§3.2). |
| `parent_revision_id` | stated<ref<RevisionRecord>> | Req | Revision derived from; state `not_applicable` only for a first revision. |
| `content_hash` | stated<hash{alg}> | Req | Integrity key (§3.3). When bytes are available this MUST be `known`; `unknown` blocks evidence use. |
| `hash_algorithm` | enum[sha256\|sha512] | Req when `content_hash` known | Algorithm for `content_hash`. |
| `byte_size` | stated<int> | Req when bytes available | Size in bytes. |
| `media_type` | string | Opt | e.g., `application/pdf`; advisory, not identity. |
| `origin_locator` | stated<string> | Req | Where the bytes came from (path/URL/import batch). Evidence of provenance, not identity. |
| `acquired_at` | timestamp | Req | Acquisition time. |
| `acquired_by` | enum[user\|agent\|import] | Req | Actor class. |
| `rights_id` | stated<ref<RightsReference>> | Req | Rights record (§5.6); may be `not_applicable` for our own produced reports, else §5.6 applies. |
| `immutable` | bool (constant `true`) | Req | Declaration that this revision is immutable (I1/I2). |
| `notes` | string | Opt | Free text. |

### 5.3 `ProvenanceRecord` (how a value/record came to exist)

| Field | Type | Req | Meaning |
| --- | --- | --- | --- |
| `provenance_id` | string | Req | Identity. |
| `project_id` | string | Req | Owning project. |
| `subject` | one of RefRevision \| `{record_type, record_id}` | Req | The record/value this provenance describes. |
| `method` | enum[human\|model\|import\|derived] | Req | How it was produced. |
| `actor` | string | Req | Who/what performed it (user id, agent role, import tool). |
| `model_ref` | Opt<string> | Req when `method=model` | Model/agent + instruction revision used; treated as untrusted data (I11). |
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
| `original_locator` | stated<{kind: enum[page\|pages\|section\|figure\|table\|offset\|timecode\|url_anchor], value: string}> | Req | Where in the **original** the evidence sits. |
| `passage` | stated<string> | Req | Verbatim original excerpt; `not_applicable` allowed only when the evidence is a figure/table/supplement carried in `source_assets`. |
| `context` | stated<string> | Req | Surrounding context (species/population/assay/outcome and comparable conditions where relevant). |
| `attribution` | stated<enum[direct_observation\|author_interpretation\|secondary_account\|mixed\|unclear\|proposed_synthesis]> | Req | Attribution class (I8). |
| `limitations` | stated<string> | Req | Known limits/caveats; "none identified" must be explicit. |
| `source_assets` | list<{asset_ref, media_type, locator}> | Req (may be empty) | Figures/tables/supplements that are inputs (line 19). |
| `rights_reference` | stated<ref<RightsReference>> | Req | Rights record for the asset(s) read (I9). |
| `numeric_details` | Opt<object> | Opt | Numbers/units where relevant (line 32). |
| `extraction_provenance` | ref<ProvenanceRecord> | Req | Who/what/how extracted. |
| `secondary_warning` | stated<bool> | Req | `known:true` when attribution is limited by a tracing limit; then final human approval is required and this MUST NOT be presented as verified primary support (I10). |

### 5.5 `DependencyReference`

| Field | Type | Req | Meaning |
| --- | --- | --- | --- |
| `dependency_id` | string | Req | Identity. |
| `project_id` | string | Req | Owning project. |
| `from_revision` | RefRevision | Req | The dependent (consumer) revision. |
| `to_revision` | stated<RefRevision> | Req | The dependency (provider) revision. `missing`/`unknown` is allowed only when a later stage must still record the unresolved dependency; it blocks that behavior. |
| `to_artifact` | RefArtifact | Req | Logical target, so an unresolved revision can still be located. |
| `role` | enum[version_of\|reports\|uses_dataset\|describes_experiment\|extracted_from\|derived_from\|summarizes\|supports_claim\|duplicate_bytes_of] | Req | The relation kind. |
| `created_at` | timestamp | Req | When declared. |
| `created_by` | enum[user\|agent\|import] | Req | Actor class. |
| `state` | §2.2 token | Req | Overall resolution state. |

### 5.6 `RightsReference` (I9; P5 stays open)

| Field | Type | Req | Meaning |
| --- | --- | --- | --- |
| `rights_id` | string | Req | Identity. |
| `project_id` | string | Req | Owning project. |
| `subject` | RefRevision \| RefArtifact | Req | What the rights apply to. |
| `acquisition_right` | enum[allowed\|not_allowed\|unknown\|not_applicable] | Req | Right to acquire. |
| `storage_right` | enum[allowed\|not_allowed\|unknown\|not_applicable] | Req | Right to retain. |
| `redistribution_right` | enum[allowed\|not_allowed\|unknown\|not_applicable] | Req | Right to redistribute. |
| `hosted_model_transfer_right` | enum[allowed\|not_allowed\|unknown\|not_applicable] | Req | Right to send to a hosted model (P5 open; default until decided: `unknown`, which blocks transfer). |
| `basis` | stated<string> | Req | License/agreement/permission/own-work. |
| `restrictions` | stated<string> | Req | Constraints or "none identified". |
| `decided_by`,`decided_at` | string, timestamp | Req | Who and when. |

**Constant reminder:** uploading a file is NOT proof of any of these rights; each
field is decided independently.

### 5.7 `OmissionCheckRecord` (does not imply completeness, I7)

| Field | Type | Req | Meaning |
| --- | --- | --- | --- |
| `omission_check_id` | string | Req | Identity. |
| `project_id` | string | Req | Owning project. |
| `scope` | {revision_set: list<RefRevision>, sample_rule: stated<ref>} | Req | What was checked; `sample_rule` points to an approved rule (§8), not an invented number. |
| `check_kind` | enum[random\|risk_based] | Req | Audit type (line 31). |
| `method_ref` | ref<ProvenanceRecord> | Req | How performed. |
| `performed_by`,`performed_at` | string, timestamp | Req | Who and when. |
| `result` | enum[no_omission_observed\|omission_found\|not_performed\|unknown] | Req | Outcome. |
| `found_omissions` | list<{ref finding\|RefRevision, description}> | Req (may be empty) | Omissions detected. |
| `limitations` | stated<string> | Req | Coverage limits of this check. |
| `guarantee` | constant `sampled_audit_not_completeness` | Req | Explicit non-guarantee: a pass does **not** prove completeness. |

---

## 6. Identity relations — duplicate bytes, alternate versions, one dataset

### 6.1 Rules

- **R1 — Exact duplicate bytes.** Two revisions with equal `content_hash`,
  `hash_algorithm`, `byte_size`, and `media_type` are the same bytes. Represent
  one stored revision and record the additional import with a
  `duplicate_bytes_of` dependency; do **not** create an independent artifact or
  evidence source.
- **R2 — Alternate document version.** Revisions of one `document` artifact linked
  by `parent_revision_id` / role `version_of` (e.g., preprint → published PDF).
  These are distinct revisions; a change may alter findings. New evidence creates
  an explicit revision and reopens affected approvals without erasing unaffected
  work (I5).
- **R3 — Multiple reports of one study/dataset.** Distinct `document` artifacts
  that each declare role `reports` (or `uses_dataset`) toward the same `study`/
  `dataset` revision. They are **not** independent confirmation (I6).
- **R4 — No false independent support.** Two evidence records count as
  independent support **only** if their `source_revision`s resolve to (a) distinct
  study/dataset/experiment revisions **and** (b) distinct `document` artifacts
  that are neither duplicate bytes nor versions of one another. If independence
  cannot be established, its state is `unknown` and downstream text MUST NOT
  claim independent support.

### 6.2 Distinguishing the three relations

| Case | Identity | Link | Independent support? |
| --- | --- | --- | --- |
| duplicate bytes | same revision content; one stored revision | `duplicate_bytes_of` | No |
| alternate version | same `artifact_id`, different `revision_id` | `version_of` | No |
| linked reports of one study | different `artifact_id`, one `study` | `reports` | No |
| genuinely distinct evidence | different `artifact_id` and distinct study/dataset | — | Yes (if R4 holds) |

---

## 7. Worked examples (synthetic)

All ids/values are synthetic. Each example names its outcome and the invariants
it exercises. Examples use `P-0001` as the project.

### 7.1 Duplicate PDF — valid

Two imports of "Smith 2020" as `a.pdf` and `b.pdf` with identical bytes.

```
ArtifactRecord{ project_id:"P-0001", artifact_id:"A1", kind:"document", ... }
RevisionRecord{ project_id:"P-0001", artifact_id:"A1", revision_id:"R1",
  content_hash:"sha256:aaaa…", hash_algorithm:"sha256", byte_size:812345,
  media_type:"application/pdf", origin_locator:"import:batch-1/a.pdf", ... }
RevisionRecord{ project_id:"P-0001", artifact_id:"A1", revision_id:"R2",
  content_hash:"sha256:aaaa…", hash_algorithm:"sha256", byte_size:812345,
  media_type:"application/pdf", origin_locator:"import:batch-2/b.pdf",
  parent_revision_id: not_applicable, ... }   # import event, same bytes
DependencyReference{ from_revision:RefRevision{A1,R2}, to_revision:RefRevision{A1,R1},
  role:"duplicate_bytes_of", state:"known" }
EvidenceRecord{ target_finding_revision:RefRevision{F1,r1}, source_revision:RefRevision{A1,R1}, ... }
```

- **Outcome:** `accepted`. One stored source; the second import is a
  `duplicate_bytes_of` link, not a new artifact.
- **Invariants:** §3.3 (hash is not identity), R1, R4 (no independent support),
  I7.

### 7.2 Duplicate PDF — invalid / missing

(a) A revision with no hash, and (b) a synthesis that counts duplicates as
independent.

```
RevisionRecord{ project_id:"P-0001", artifact_id:"A1", revision_id:"R3",
  content_hash: MISSING, byte_size: 812345, media_type:"application/pdf", ... }
# (a) bytes exist but content_hash is absent

report_claim{ text:"two independent papers agree",
  sources:[RefRevision{A1,R1}, RefRevision{A1,R2}] }   # R1,R2 share content_hash
# (b) counts duplicate bytes as independent
```

- **Outcome:** (a) `rejected_invalid` — mandatory `content_hash` absent while
  bytes are available (§5.2, §2.3). (b) `blocked`/invalid — violates R4/I6; the
  claim MUST be corrected or marked not independent.
- **Invariants:** §2.3, §3.3, R1, R4, I6, I7.

### 7.3 Alternate version — valid

Preprint `R1`, then the published PDF `R2` of the same document `A1`.

```
RevisionRecord{ ... artifact_id:"A1", revision_id:"R2",
  parent_revision_id:"R1", content_hash:"sha256:bbbb…", ... }
DependencyReference{ from_revision:RefRevision{A1,R2}, to_revision:RefRevision{A1,R1},
  role:"version_of", state:"known" }
# new evidence extracted from R2:
DependencyReference{ from_revision:RefRevision{F1,r1}, to_revision:RefRevision{A1,R2},
  role:"extracted_from" }
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
RevisionRecord{ artifact_id:"A1", revision_id:"R1", content_hash:"sha256:cccc…" }
# R1 object mutated to the published bytes in place            # (b) immutability break
```

- **Outcome:** (a) `rejected_invalid`/`blocked` — approval does not cover `R2`;
  material edit invalidates affected checks (I1/I2). (b) `rejected_invalid` — a
  revision's bytes/hash MUST NOT change; a change is a new `revision_id` (§3.2).
- **Invariants:** §3.2, §2.3, I1, I2.

### 7.5 Missing full text — explicit state, not silence

A frozen candidate has no acquired full text.

```
ArtifactRecord{ artifact_id:"A2", kind:"document", status:"active" }
RevisionRecord{ artifact_id:"A2", revision_id:"RX",  # metadata-only stub
  content_hash: not_applicable, byte_size: not_applicable, ... }
EvidenceRecord{ source_revision: stated<RefRevision>{state:"missing",
    reason:"no full text acquired for A2 (frozen candidate)"},
  passage: stated<string>{state:"missing", reason:"no source text"},
  ...
  text from abstract only, attribution:{state:"known", value:"author_interpretation"} }
DependencyReference{ from_revision:RefRevision{F1,r1}, to_revision: stated{state:"missing"},
  to_artifact:RefArtifact{A2}, role:"extracted_from", state:"missing" }
```

- **Outcome:** `blocked` for scientific digestion of `A2` until a **human
  disposition** (line 29, I3): acquire full text, or explicitly exclude/defer.
  Abstract-derived context is provisional (line 25) and MUST NOT be recorded as
  primary support.
- **Invariants:** §2.2, I3, I4, I7.

### 7.6 Omitted result — audit records an omission

Bulk extraction missed a reported null result; the audit finds it.

```
OmissionCheckRecord{ omission_check_id:"OC1", scope:{revision_set:[RefRevision{A1,R1}],
    sample_rule:{state:"known", value:"pilot rule PR-1"}},
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
```

- **Outcome:** `accepted` as relations; a synthesis memo counting `A10`+`A11` as
  **independent** confirmation is `rejected_invalid` (R3/R4, I6). The synthesis
  MUST present the underlying single study and preserve the correlation.
- **Invariants:** R3, R4, I6.

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

- This is document version **`v1`** (`docs/contracts/01-identities.md`).
- Because the contract itself is versioned, a change to any mandatory field,
  type, state, or rule MUST increment the document version and record the change
  below with its date and approver. Field/record semantics are immutable within a
  version once approved.
- Until approved, edits are proposal revisions, not ratified changes.

| Doc version | Date | Change | Approver |
| --- | --- | --- | --- |
| v1 | 2026-10-27 | Initial proposed identity/evidence contract; open items recorded in §8. | **pending (user)** |

---

## 10. Verification hooks for reviewers

- Every mandatory field above carries a type and meaning (§5).
- Missing/invalid/missing data maps to `rejected_invalid` or `blocked` (§2.3),
  demonstrated in §7.2, §7.4, §7.5.
- The four required examples (duplicate PDF, alternate version, missing full
  text, omitted result) each state an outcome and the invariants walked
  (§7.1–§7.6), plus the required one-dataset case (§7.7).
- All distinctions in implementation `01-contracts.md` acceptance criterion 3 are
  made explicit in §4 and §3.2.
- No P1–P6 are accepted and no numeric scientific threshold is introduced (§8).
