# 03 — Policy and architecture decision register (v1)

> **Status: ACCEPTED / RATIFIED — 2026-10-27.** The user dispositioned every P1–P6
> item, the operational policies, the technical direction, and the rights behavior
> in the recorded Step 01 interview; this register now carries those dispositions as
> **`accepted`**. The ratification is anchored to the **reviewed revision
> `ee7f7eafca80baa6c1aaca278361e56a663493c3`** and is recorded in
> [`../decisions/002-step01-contracts-and-policies.md`](../decisions/002-step01-contracts-and-policies.md).
> **What the ratification is:** approval of the **described behavior and direction**,
> with the concrete **interview amendments** stated per record below.
> **What it is not:** it is **not** approval of every field-level schema spelling,
> **not** an independent scientific validation, and **not** a claim that any
> scientific feature is implemented. The record representation here is an
> **implementation-facing, versioned contract**; its exact field spellings remain
> subject to versioned revision, and no numeric scientific threshold is invented.
> The synthetic `proposed`/invalid fixtures in section 1.4 are retained as
> **historical format examples** for the offline checker, independent of the
> ratified status of the records they illustrate.

This register implements the Step 01 [deliverable](../implementation/01-contracts.md)
"record user disposition of P1–P6 … and agree pilot/audit sampling and acceptance,
search coverage/stopping, cost/concurrency limits, origin-hop/source/retry defaults,
and secondary-warning approval" (lines 9–12). It is a **decision register**, not a
decision record: it defines the record format (section 1), states each item
faithfully against the [baseline](../specification.md) (sections 2–3), and records
the **accepted** technical direction (sections 4–5). Per the register's own rules
(section 1.3), a session or coordinator may **write** the register but may **not**
change a record's status; the `accepted` status comes from the user's interview
selections plus the user's instruction to finalize the step
([`../decisions/002-step01-contracts-and-policies.md`](../decisions/002-step01-contracts-and-policies.md)).

## 0. Preserved, already-confirmed baseline (not open here)

These are confirmed and are **not** reopened by this register; they are recorded so
proposals below cannot silently weaken them.

| Confirmed item | Source | Status |
| --- | --- | --- |
| **Python core** with a supported, explicitly authenticated OpenCode V2 HTTP integration | [decision 001](../decisions/001-runtime-language.md); [spec line 15](../specification.md) | Accepted by user 2026-10-27 |
| **One active project at a time** | [spec line 11](../specification.md) | Confirmed |
| Approvals bind to **immutable artifact/dependency revisions**; agents cannot self-approve | [spec lines 54–55](../specification.md) | Confirmed |
| Secondary attribution at a tracing limit or unavailable upstream origin is a **warned state requiring final human approval**, never verified primary support | [spec line 50](../specification.md) | Confirmed (mechanism) |
| **Max 3** critique/repair rounds (section review) | [spec line 38](../specification.md) | Confirmed numeric maximum |
| **Max 2** original-source factual-check/repair rounds | [spec line 39](../specification.md) | Confirmed numeric maximum |
| **Max 2** whole-manuscript repair rounds | [spec line 40](../specification.md) | Confirmed numeric maximum |
| Treat documents/model outputs as **untrusted data** | [spec line 62](../specification.md) | Confirmed |

The three repair-round maxima (3, 2, 2) above are **already-confirmed numeric
limits** and must **not** be confused with the **open** thresholds listed in section
3 (tracing hop/source/retry defaults, coverage stop rules, costs/concurrency). A
model edit cannot replenish an exhausted repair budget, and **no decision record in
this register — and no ordinary approval — lifts these fixed repair-round maxima**
([spec line 55](../specification.md)).

---

## 1. Decision record format v1

### 1.1 Fields and types

Each recorded decision is one record with at least these fields. Types are given so
later steps can validate records mechanically.

| Field | Type | Meaning |
| --- | --- | --- |
| `id` | string | Stable identifier, e.g. `POL-P1`, `POL-OPS-TRACE`, `RIGHTS-R1`. |
| `title` | string | Short human label. |
| `status` | enum | `proposed` \| `accepted` \| `modified` \| `deferred` (see 1.2). |
| `baseline_excerpt` | string | **Verbatim** baseline/reference text the decision resolves. |
| `baseline_ref` | string | File + line of the excerpt. |
| `recommendation` | string | A proposal only; **never** approval. |
| `options` | string[] | Concrete alternatives considered, each with a one-line trade-off. |
| `owner` | string | Accountable party. For every policy item here: **the user**. |
| `approval_required` | bool | True when only the user may move the record. |
| `user_evidence` | string \| null | Verbatim user statement (quoted) + date. `null` while `proposed`. |
| `decided_against_revision` | string \| null | Exact artifact revision (commit SHA) the user decision binds to. `null` while `proposed`. |
| `affected_stages` | string[] | Implementation step ids whose behavior depends on this record. |
| `blocking_dependencies` | string[] | Named behaviors that must stay blocked while the record is open. |
| `fail_closed_behavior` | string | What the system does when the value is unset/unknown. |
| `numeric_value` | number \| string \| `UNSET` | A **single representative scalar** only, set only when the **user** set it; `UNSET` otherwise. A structured value (e.g. a multi-ceiling caps object) is carried in `notes` and its detail stays unresolved. Never invented to let a stage pass. |
| `notes` | string | Caveats, limitations, revisit conditions. |

**Hard rule — no fabricated thresholds.** `numeric_value` is `UNSET` unless the user
explicitly supplies it. A stage may not pass by inventing a numeric scientific
threshold ([01-contracts acceptance](../implementation/01-contracts.md), line 18;
[kickoff line 103](../implementation/kickoffs/01-contracts.md)).

**Projections vs. records.** The fields above are **mandatory on every record**. The
human-readable entries in sections 2–5 are **projections** of a full record, not the
record itself: they state the fields that differ from the defaults and inherit the
rest. For the **accepted** records in sections 2–5 the projection states, at minimum,
`status = accepted`, `owner = user`, `approval_required = true`, the quoted
`user_evidence` (verbatim interview selection + date 2026-10-27), and
`decided_against_revision = ee7f7eafca80baa6c1aaca278361e56a663493c3`; every other
field is taken from the entry text. A validator can materialize a complete record for
any entry by combining its stated fields with these values; no entry may rely on an
**unstated, non-default** value. `numeric_value` is a single representative scalar;
where a value is structured (multiple ceilings, a caps object), the projection shows
only a representative scalar and the structure is left unresolved in `notes`.

### 1.2 Status values and transitions

```text
                 (user accepts unchanged)
   proposed ──────────────────────────────► accepted
      │  │
      │  │        (user accepts with a change)
      │  └────────────────────────────────► modified   (terminal; supersedes proposed)
      │
      │           (user explicitly defers)
      └───────────────────────────────────► deferred   (terminal; reopen needs the user)

   open ("pending") = proposed with user_evidence = null and decided_against_revision = null
```

- `proposed` — recorded, owned, **not** ratified. This was the initial state of
  **every** policy item in this register **before** the 2026-10-27 interview; the
  records in sections 2–5 have since been moved to `accepted` by the user.
- `accepted` — the user accepted the recommendation; a per-record **interview
  amendment** records any concrete behavior the user added (an accepted
  refinement of the recommendation's direction, not a rewrite of the baseline).
- `modified` — the user accepted a changed variant; the variant text is the
  authoritative record.
- `deferred` — the user explicitly chose to defer; **only the user may defer**.
- **Pending ≠ deferred.** An item still awaiting the user's disposition stays
  `proposed`; a coordinator/session may **not** record it as `deferred`.

### 1.3 Transition rules (who may do what)

1. A session/coordinator **may create or update** a record (fields,
   recommendation, options, blockers).
2. Only the **user** may move a record `proposed → accepted | modified | deferred`,
   and only by supplying `user_evidence` (verbatim quote + date) and
   `decided_against_revision` (exact SHA). Without both, the transition is invalid.
3. A record once `accepted`/`modified`/`deferred` is not silently reopened; a user
   revision produces a **new** exact-revision decision, consistent with approvals
   binding to revisions ([spec line 54](../specification.md)).
4. `modified` is a terminal acceptance variant; a later change is recorded as a new
   revision decision, not an in-place edit of the accepted text.
5. No agent/model output may set any status; only explicit user evidence may.

The 2026-10-27 interviews supplied exactly the evidence rule 1.3(2) requires: the
**verbatim** selections (quoted below) with their dates, and the reviewed revision
`ee7f7eafca80baa6c1aaca278361e56a663493c3`. The user's follow-up instruction
`"Proceed forward and finalize this step until a ready PR"` is the direction to
record those dispositions in the baseline, this register, and the decision record.

### 1.4 Decision-record examples (types checked; illustrative, never an acceptance)

These examples are **historical format examples** retained for the offline checker.
They show the record format as it stood at the pre-ratification revision; none is a
current disposition, none is an acceptance, `user_evidence` values shown are
placeholder shapes (never a real approval), and no example lifts the fixed
repair-round maxima (section 0).

**Full proposed record (every field present).**

```json
{
  "id": "POL-OPS-COST",
  "title": "Cost and concurrency limits",
  "status": "proposed",
  "baseline_excerpt": "Within-project concurrency may be bounded independently.",
  "baseline_ref": "docs/specification.md line 11",
  "recommendation": "Define per-project and per-stage cost ceilings and a within-project concurrency bound.",
  "options": [
    "explicit ceilings + concurrency bound",
    "user-set per project with no default",
    "defer but block all paid calls"
  ],
  "owner": "user",
  "approval_required": true,
  "user_evidence": null,
  "decided_against_revision": null,
  "affected_stages": ["03", "04"],
  "blocking_dependencies": ["any new paid call", "concurrency above the bound"],
  "fail_closed_behavior": "With unset ceilings, no new paid call is issued; unknown cost retains its reserve.",
  "numeric_value": "UNSET",
  "notes": "Representative scalar only; a multi-ceiling caps object is structured and unresolved. Distinct from the confirmed repair-round maxima."
}
```

The `baseline_excerpt` above is an **exact** source fragment (spec line 11). The
broader cost/concurrency baseline is the paraphrase recorded in POL-OPS-COST; a record
must never present a paraphrase as an excerpt.

**Illustrative transition PATCH (NOT a full record; synthetic, not an acceptance).**
This shows only the fields a user transition changes; every other mandatory field is
inherited from the full record above and is **not** re-listed here.

```json
{
  "id": "POL-OPS-COST",
  "status": "accepted",
  "user_evidence": "<verbatim user statement>, <date>",
  "decided_against_revision": "<40-char commit SHA>",
  "numeric_value": "<user-supplied ceiling>"
}
```

**Invalid record — accepted without evidence (rejected).**

```json
{ "id": "POL-P1", "status": "accepted", "user_evidence": null, "decided_against_revision": null }
```

Rejected: rule 1.3(2) requires `user_evidence` **and** `decided_against_revision`.

**Invalid record — invented threshold (rejected).**

```json
{ "id": "POL-OPS-PILOT", "status": "proposed", "numeric_value": 0.95 }
```

Rejected: a coordinator/session may not set a numeric scientific threshold; only the
user may, and only on an accepted/modified transition. The value `0.95` is a
deliberately invalid fixture, **not** a recommendation.

---

## 2. Policy decisions P1–P6 (owner: user; dispositioned `accepted` 2026-10-27)

The baseline requires Step 01 to record accept/modify/defer dispositions
([spec lines 65–74](../specification.md)). All six were dispositioned by the user in
the recorded Step 01 interview: each is now **`accepted`**, binding to the reviewed
revision `ee7f7eafca80baa6c1aaca278361e56a663493c3` and dated 2026-10-27. The
per-record **Interview amendment** bullet states the concrete behavior the user chose
(an accepted refinement of the recommendation's direction, not a rewrite of the
baseline). Approval covers the **described behavior and direction**, not the exact
field-level spelling of any record, and is **not** scientific validation.

### POL-P1 — Framework contribution and concept-admission/support/revision criteria

- **Baseline excerpt (verbatim).** "P1: explicit framework contribution,
  concept-admission/support/revision criteria and testable implications where
  applicable." ([spec line 69](../specification.md))
- **Recommendation (accepted).** Define, in a versioned artifact, what counts
  as the review's *own* framework contribution versus a summary of others', plus
  admission criteria for concepts, the support required before a concept is used,
  and revision criteria when support changes; where applicable, require a testable
  implication per admitted concept. **No numeric scientific threshold is implied.**
- **Interview amendment (2026-10-27).** The user selected **evidence-led synthesis**
  and **required safeguards**: organize evidence and allow justified new
  relationships/hypotheses, **clearly label novelty, and never force a novel
  framework**; concept admission requires **definition, role, original support,
  counterevidence, and applicability limits**; hypotheses are **distinguished**
  from findings, **material changes reopen** the affected approval, and
  **meaningful testable implications** are required where applicable. No numeric
  threshold.
- **User evidence.** `"Evidence-led synthesis (Recommended)"`; `"Require safeguards
  (Recommended)"` — 2026-10-27.
- **Decided against revision.** `ee7f7eafca80baa6c1aaca278361e56a663493c3`.
- **Options.** (a) full formal criteria set at synthesis; (b) minimal criteria +
  per-claim "framework contribution" flag; (c) defer the formal criteria but block
  framework/outline finalization.
- **Owner / approval.** User. `approval_required = true`; `status = accepted`.
- **Affected stages.** [10 §synthesis](../implementation/README.md),
  [11 §drafting](../implementation/README.md), [12 §review](../implementation/README.md).
- **Blocking dependencies.** Resolved: the "shared approved argument record" and
  framework proposal ([spec lines 34–35](../specification.md)) may now be finalized
  under the accepted criteria; the safeguard set is the standard against which a
  framework-bearing claim is admitted.
- **Fail-closed.** A framework-contribution claim that lacks the required safeguards
  (definition/role/original support/counterevidence/applicability limits) stays
  **unclassified** and cannot be marked approved; the synthesis stage proposes only.
- **numeric_value:** `UNSET` (criteria are categorical, not numeric).

### POL-P2 — Downstream causal/context-fidelity checks

- **Baseline excerpt (verbatim).** "P2: detailed downstream causal/context-fidelity
  checks." ([spec line 70](../specification.md))
- **Recommendation (accepted).** Define the checks that verify a downstream
  conclusion preserves the upstream study's causal/associational status and context
  (species/population/assay/outcome limits), consistent with the confirmed invariant
  that these limitations must be carried into conclusions
  ([spec line 60](../specification.md)). No numeric threshold.
- **Interview amendment (2026-10-27).** The user selected **structured plus review**:
  capture the **applicable context and causal limits at extraction**, preserve them
  through synthesis, and **check synthesis and review for unsupported
  generalization**; **consequential missing context blocks the affected claim**.
- **User evidence.** `"Structured plus review (Recommended)"` — 2026-10-27.
- **Decided against revision.** `ee7f7eafca80baa6c1aaca278361e56a663493c3`.
- **Options.** (a) structured fidelity checklist per extracted finding; (b) a review-time
  fidelity finding type; (c) defer but block causal claim approval.
- **Owner / approval.** User. `approval_required = true`; `status = accepted`.
- **Affected stages.** [09 §extraction](../implementation/README.md),
  [10 §synthesis](../implementation/README.md), [12 §review](../implementation/README.md),
  [13 §origin-tracing](../implementation/README.md).
- **Blocking dependencies.** Only the **pass/approval** of conclusions that depend on
  a fidelity dimension is blocked when context is consequentially missing. Detection is
  **not** blocked: fidelity findings/issues may still be **recorded as proposals** and
  surfaced for the user; they simply cannot be auto-dispositioned to pass.
- **Fail-closed.** An unchecked fidelity dimension is a **missing check**, not a pass
  ([spec line 57](../specification.md)); affected conclusions remain unapproved.
- **numeric_value:** `UNSET`.

### POL-P3 — Integrity-status checks (retractions, expressions of concern, corrections)

- **Baseline excerpt (verbatim).** "P3: integrity-status checks for retractions,
  expressions of concern, corrections, and adjudication of affected uses. Do not
  treat non-replication as retraction or automatically prohibit historical
  discussion." ([spec line 71](../specification.md))
- **Recommendation (accepted).** Define an integrity-status record per source
  and per cited use, with the four statuses distinguished, an adjudication path for
  affected uses, and the two explicit non-rules preserved (non-replication ≠
  retraction; no automatic historical-discussion prohibition).
- **Interview amendment (2026-10-27).** The user selected **entry and final checks**:
  document integrity checks at **corpus entry** and at **final approval**, with
  **statuses/provenance/date** and **human review of affected uses**; an **unknown**
  integrity status is **not clean**; and correction / non-replication / retraction
  are **not automatically conflated**.
- **User evidence.** `"Entry and final checks (Recommended)"` — 2026-10-27.
- **Decided against revision.** `ee7f7eafca80baa6c1aaca278361e56a663493c3`.
- **Options.** (a) per-source integrity record + per-use adjudication; (b) review-time
  integrity finding only; (c) defer but block any use flagged by an integrity signal.
- **Owner / approval.** User. `approval_required = true`; `status = accepted`.
- **Affected stages.** [05 §discovery](../implementation/README.md),
  [07 §parsing](../implementation/README.md), [09 §extraction](../implementation/README.md),
  [10 §synthesis](../implementation/README.md), [13 §origin-tracing](../implementation/README.md),
  [14 §delivery](../implementation/README.md).
- **Blocking dependencies.** Retraction/expression-of-concern/correction handling and
  adjudication of affected uses are now governed by the accepted statuses; an
  integrity-**unknown** source cannot be silently treated as clean.
- **Fail-closed.** Unknown integrity status is an explicit **unknown** state that
  blocks approval of dependent uses; it is never "no issue". A correction or a
  non-replication is adjudicated on its own terms and does not automatically become a
  retraction.
- **numeric_value:** `UNSET`.

### POL-P4 — Public selection/synthesis and limitations reporting

- **Baseline excerpt (verbatim).** "P4: public selection/synthesis and limitations
  reporting alongside the manuscript." ([spec line 72](../specification.md))
- **Recommendation (accepted).** Define the reporting artifact that accompanies
  the manuscript: how selection/synthesis decisions and limitations are reported
  publicly, aligned with the confirmed rule that a pilot and sampled audits are *not*
  held-out validation ([spec line 59](../specification.md)).
- **Interview amendment (2026-10-27).** The user selected **companion plus summary**:
  a detailed, exportable **methods/limitations companion** together with a concise
  **manuscript-ready summary**; **publication is user-controlled**; the workflow's
  limitations are reported honestly.
- **User evidence.** `"Companion plus summary (Recommended)"` — 2026-10-27.
- **Decided against revision.** `ee7f7eafca80baa6c1aaca278361e56a663493c3`.
- **Options.** (a) companion report in the export set; (b) a reporting section inside
  the manuscript; (c) defer but block final export.
- **Owner / approval.** User. `approval_required = true`; `status = accepted`.
- **Affected stages.** [14 §delivery](../implementation/README.md),
  [15 §release](../implementation/README.md).
- **Blocking dependencies.** Resolved: the companion + summary format is the accepted
  reporting requirement; export may record that the requirement is met once the
  accepted format is produced.
- **Fail-closed.** Absent the accepted companion, the export records an explicit
  marker that the accepted reporting requirement is unmet, rather than silently
  omitting a report.
- **numeric_value:** `UNSET`.

### POL-P5 — AI-use declaration, human accountability, provider terms, journal policies

- **Baseline excerpt (verbatim).** "P5: AI-use declaration, human accountability,
  applicable provider terms and journal policies." ([spec line 73](../specification.md))
- **Recommendation (accepted).** Define the AI-use declaration content, the
  human-accountability statement, and the process for confirming applicable provider
  terms and target-journal policies before any submission-facing output or any
  hosted-model transfer.
- **Interview amendment (2026-10-27).** The user selected **require process**:
  record the **actual AI use and versions**, maintain an **editable disclosure** with
  explicit **human accountability**, verify **provider terms before hosted use**, and
  verify **journal policy before submission-facing approval**. This is the
  operational definition; **no blanket prohibition is derived from an unverified or
  unknown venue** (see POL-P6).
- **User evidence.** `"Require process (Recommended)"` — 2026-10-27.
- **Decided against revision.** `ee7f7eafca80baa6c1aaca278361e56a663493c3`.
- **Options.** (a) declaration template + accountability statement + a per-venue
  terms checklist; (b) declaration only, terms checked at submission; (c) defer but
  block submission-facing output and hosted transfer.
- **Owner / approval.** User. `approval_required = true`; `status = accepted`.
- **Affected stages.** [04 §agent-runtime](../implementation/README.md),
  [14 §delivery](../implementation/README.md), [15 §release](../implementation/README.md).
- **Blocking dependencies.** The AI-use declaration, human-accountability statement,
  and editable disclosure are accepted requirements. **Provider-term verification
  gates hosted use**; **journal-policy verification gates submission-facing
  approval**. (Hosted transfer is *also* independently gated by rights — section 5.)
- **Fail-closed.** A **hosted-model transfer** defaults to blocked until the provider
  terms are verified and the transfer is authorized (sections 5.2–5.3). A
  **submission-facing output** defaults to unapproved until the applicable journal
  policy is verified. An unverified provider/journal state is a **missing check**,
  not permission; it is not a blanket prohibition of processing one's own authorized
  draft (a venue-specific question, POL-P6).
- **numeric_value:** `UNSET`.

### POL-P6 — Optional target-venue constraints and submission-time verification

- **Baseline excerpt (verbatim).** "P6: optional target-venue constraint capture and
  submission-time verification; retain venue-neutral output." ([spec line 74](../specification.md))
- **Recommendation (accepted).** Define optional capture of target-venue
  constraints and a submission-time verification step, while keeping the retained
  output **venue-neutral** (matching the confirmed venue-neutral output rule).
- **Interview amendment (2026-10-27).** The user selected **optional venue profile**:
  venue constraints are captured **only when a venue is selected**, with a **fresh
  submission checklist** and **human confirmation**; **venue-neutral output is
  retained**. **Venue-specific checks apply only when a venue is selected; an
  unselected or unknown venue is not a blanket prohibition** — it simply means no
  venue-specific constraint is claimed and no venue-compliance assertion is made.
- **User evidence.** `"Optional venue profile (Recommended)"` — 2026-10-27.
- **Decided against revision.** `ee7f7eafca80baa6c1aaca278361e56a663493c3`.
- **Options.** (a) optional venue-constraint record + verification checklist;
  (b) verification only, no capture; (c) defer but block any venue-specific claim.
- **Owner / approval.** User. `approval_required = true`; `status = accepted`.
- **Affected stages.** [10 §synthesis](../implementation/README.md) (outline
  constraints), [14 §delivery](../implementation/README.md),
  [15 §release](../implementation/README.md).
- **Blocking dependencies.** Venue-specific constraint checks and submission-time
  verification apply **only when a venue is selected**. With no selected venue, the
  **venue-neutral** output is preserved and nothing is blocked by the missing venue.
- **Fail-closed.** Without a captured venue, the pipeline produces venue-neutral
  output and **cannot assert venue compliance** — but it does **not** prohibit
  processing the draft. No unknown-venue blanket prohibition.
- **numeric_value:** `UNSET`.

---

## 3. Operational policies (owner: user; dispositioned `accepted` 2026-10-27)

Baseline: "Also decide pilot/audit samples and acceptance rules, issue severities,
coverage/stopping rules, cost/concurrency limits, tracing defaults, and explicit
acceptance of secondary-origin warnings." ([spec line 76](../specification.md)).
Each operational policy is now **`accepted`**; where the user chose a
**project-specific** rule rather than a universal number, `numeric_value` stays
`UNSET` and the concrete value is a **future project-setup gate** (per project,
before the affected work), with the fail-closed behavior stated per record.

### POL-OPS-PILOT — Pilot/audit sampling and acceptance rules

- **Baseline.** One iterative pilot example set, no separate held-out set; human
  audits examine omissions and incorrect findings; important failures block bulk
  digestion ([spec lines 30, 32](../specification.md)).
- **Recommendation (accepted).** Define the pilot sample, the exclusion-batch
  audit (random + risk-based original-source checks) sample, the extraction-audit
  sample, and the acceptance rule for each. Samples are **not** held-out validation
  ([spec line 59](../specification.md)).
- **Interview amendment (2026-10-27).** The user selected **project-specific plan**:
  an explicit, approved, **project-specific** pilot/audit size and rule is **mandatory
  before bulk work**; one **iterative pilot**; **random + risk-based exclusion
  checks** and **omission/error extraction audits**; **important failures
  pause/correct/re-audit** the affected work. **No universal sample size** is set.
- **User evidence.** `"Project-specific plan (Recommended)"` — 2026-10-27.
- **Decided against revision.** `ee7f7eafca80baa6c1aaca278361e56a663493c3`.
- **Options.** (a) explicit sampling rule + acceptance thresholds; (b) sampling
  proportions left to per-project user choice with a default; (c) defer but block
  pilot approval.
- **Affected stages.** [08 §screening](../implementation/README.md),
  [09 §extraction](../implementation/README.md), [12 §review](../implementation/README.md).
- **Blocking dependencies.** Bulk-digestion gates cannot be triggered to pass until
  the project-specific pilot/audit plan is approved.
- **Fail-closed.** With no approved project plan, no bulk-digestion approval can be
  granted; the pilot remains open. **No universal sample size is invented.**
- **numeric_value:** `UNSET` (project-specific; no invented accuracy/acceptance number).

### POL-OPS-SEVERITY — Issue severity scale

- **Baseline.** Findings need "evidence or a concrete missing check, severity, and
  explicit fixed/rejected-with-reason disposition" ([spec line 57](../specification.md)).
- **Recommendation (accepted).** Define the severity scale used by findings
  (critic, fact-check, whole-manuscript, origin) and which severities block. The
  *requirement* that a severity exists is confirmed; the *scale* is now accepted.
- **Interview amendment (2026-10-27).** The user selected **four categories**:
  **blocking** (factual/support/essential-check), **major** (consequential scientific
  defect), **minor** (non-consequential), and **style** (preference); **blocking and
  major issues, and any consequential unclassified issue, prevent completion**.
- **User evidence.** `"Accept four categories (Recommended)"` — 2026-10-27.
- **Decided against revision.** `ee7f7eafca80baa6c1aaca278361e56a663493c3`.
- **Options.** (a) small ordinal scale with defined blocking severities; (b) two-level
  blocking/non-blocking; (c) defer but block any finding from being auto-passed.
- **Affected stages.** [08](../implementation/README.md)–[14](../implementation/README.md)
  (all finding producers).
- **Blocking dependencies.** Findings cannot be dispositioned to pass without a
  severity from the accepted scale.
- **Fail-closed.** An unclassified-severity finding is treated as **blocking** until
  classified; a consequential unclassified issue blocks completion.
- **numeric_value:** `UNSET`.

### POL-OPS-COVERAGE — Search coverage report and stopping rule

- **Baseline.** "A coverage report supports an explicit human decision to search
  again or freeze the candidate revision. Never promise all papers."
  ([spec line 28](../specification.md)).
- **Recommendation (accepted).** Define what the coverage report contains and
  the criteria under which the human freezes the candidate revision (no "all papers"
  guarantee).
- **Interview amendment (2026-10-27).** The user selected **coverage report plus
  gate**: the report records **sources, queries, dates, terminology, citation
  expansion, yields, duplicates, and gaps**, and the **exact candidate revision is
  frozen by explicit human approval**. **No universal numeric stopping/completeness
  rule** is set.
- **User evidence.** `"Coverage report plus gate (Recommended)"` — 2026-10-27.
- **Decided against revision.** `ee7f7eafca80baa6c1aaca278361e56a663493c3`.
- **Options.** (a) documented coverage metrics + human freeze decision;
  (b) human-only freeze with no numeric stop rule; (c) defer but block corpus freeze.
- **Affected stages.** [05 §discovery](../implementation/README.md).
- **Blocking dependencies.** Candidate-revision freeze and downstream acquisition
  cannot proceed on an unreported coverage state.
- **Fail-closed.** Without the coverage report, the state stays **"search open"** and
  cannot be frozen to pass. **No universal numeric stop is invented.**
- **numeric_value:** `UNSET` (no universal coverage/stop number).

### POL-OPS-COST — Cost and concurrency limits

- **Baseline.** Bounded budgets/concurrency; no busy loop while waiting
  ([spec lines 11, 13](../specification.md);
  [03 lines 12, 21](../implementation/03-durable-worker.md)). Only one paid call
  ($0.00002037) is recorded in the project so far
  ([00 handoff](../implementation/handoffs/00-runtime-spike.md)).
- **Recommendation (accepted).** Define per-project and per-stage cost ceilings
  and the within-project concurrency bound; keep the global
  **one-active-project** limit (ARCH-ACTIVE-ENF).
- **Interview amendment (2026-10-27).** The user selected **`"$25 / $10 / 2"
  (Recommended)`**: a **USD-25 cumulative per-project cap**, a **USD-10 cumulative
  per-stage cap**, and a **maximum of 2 simultaneous external-model calls**. **All
  routes and read-only model calls are counted**; the **reserve upper bound is set
  before dispatch**; when a cost **cannot be bounded the call is not dispatched**;
  an **unknown cost retains its reserve**; reaching a cap **stops new calls** (it
  does not promise the review's total cost); only a **human** may increase a cap; a
  **restart does not reset** the counters.
- **User evidence.** `"$25 / $10 / 2 (Recommended)"` — 2026-10-27.
- **Decided against revision.** `ee7f7eafca80baa6c1aaca278361e56a663493c3`.
- **Options.** (a) explicit ceilings + concurrency bound; (b) user-set per project
  with no default; (c) defer but block all paid calls.
- **Affected stages.** [03](../implementation/03-durable-worker.md),
  [04](../implementation/04-agent-runtime.md), and every paid-capable stage.
- **Blocking dependencies.** A new paid call or concurrency above the bound is
  blocked at the cap until a human raises it.
- **Fail-closed.** With an unbounded cost, **no new paid call** is issued; unknown
  cost retains its reserve and is never reported as zero
  ([live-jev accounting](../../spikes/runtime/live-jev/README.md)).
- **numeric_value:** `25` (representative scalar: the USD-25 cumulative per-project
  cap). The full structured caps object — USD-10 per-stage cap, maximum 2
  simultaneous external-model calls — is recorded in `notes`.
- **notes.** Structured caps: `{project_cap_usd: 25, stage_cap_usd: 10,
  max_concurrent_external_model_calls: 2}`; all routes and read-only model calls are
  counted; caps stop new calls and are raised only by a human; restart does not reset
  them. This is a cost/concurrency default only, distinct from the confirmed
  repair-round maxima (section 0), and is not a promise of total review cost.

### POL-OPS-TRACE — Citation tracing hop/source/retry defaults

- **Baseline excerpt (verbatim).** "Configure maximum citation hops, new sources per
  claim/project, and resolution retries; detect cycles. Numerical defaults are **not
  yet agreed**. These are not the writer's repair budgets." ([spec line 48](../specification.md))
- **Recommendation (accepted).** Define maximum citation hops, new sources per
  claim and per project, and resolution retries, with cycle detection. These are
  explicitly **distinct from** the confirmed repair budgets (section 0).
- **Interview amendment (2026-10-27).** The user selected **required per project**:
  explicit **maximum hops, sources per claim and per project, and resolution
  retries are required per project before any upstream tracing work**; **cycles are
  detected**; there is **no universal default**.
- **User evidence.** `"Required per project (Recommended)"` — 2026-10-27.
- **Decided against revision.** `ee7f7eafca80baa6c1aaca278361e56a663493c3`.
- **Options.** (a) explicit numbers; (b) user-set per project with no default;
  (c) defer the numbers but keep tracing in `configuration_required` (no remote work).
- **Affected stages.** [13 §origin-tracing](../implementation/README.md).
- **Blocking dependencies.** Until the per-project limits are set, tracing enters
  **`configuration_required`**: it performs **no remote/upstream tracing work** and
  does **not** infer that any limit was reached. A depth-0 claim is not treated as
  trivially resolved.
- **Fail-closed.** An **unset** limit means **`configuration_required`**, not a
  reached limit. The warned **secondary-attribution** state is entered **only** when a
  source that was **actually read** supports the claim only secondarily **and** the
  missing upstream origin has been **adjudicated** ([spec line 50](../specification.md));
  it is never a stand-in for an unconfigured limit and never "verified primary
  support". No universal hop/source/retry number is invented.
- **numeric_value:** `UNSET` (per project; no universal default).

### POL-OPS-SECONDARY — Explicit acceptance of secondary-origin warnings

- **Baseline excerpt (verbatim).** "At a limit or unavailable upstream origin, the
  user chose **explicit secondary attribution with a visible warning and final human
  approval**. Cite the source actually read; never substitute an unread original or
  claim direct verification." ([spec line 50](../specification.md))
- **Status nuance.** The **mechanism** is confirmed baseline (section 0): the warned
  secondary state exists and requires final human approval. The **operational
  definition** — the warning's record type/severity, where the approval binds (exact
  revision), and how it surfaces in export — is now **accepted**.
- **Recommendation (accepted).** A first-class "secondary-attribution warning"
  record bound to the exact artifact revision, surfaced at final read-only validation
  and in export, requiring a recorded human approval.
- **Interview amendment (2026-10-27).** The user selected **review plus companion**:
  a **review warning** plus an **export companion**, **accurate manuscript secondary
  attribution**, and **final human approval against the exact manuscript/dependency/
  warning set**; **material changes reopen** the approval; the state is **never
  primary verified**.
- **User evidence.** `"Review plus companion (Recommended)"` — 2026-10-27.
- **Decided against revision.** `ee7f7eafca80baa6c1aaca278361e56a663493c3`.
- **Options.** (a) dedicated warning record + revision-bound approval; (b) reuse the
  findings/episode severity with a secondary flag; (c) defer the record type but keep
  the confirmed warned-state invariant.
- **Affected stages.** [12 §review](../implementation/README.md),
  [13 §origin-tracing](../implementation/README.md),
  [14 §delivery](../implementation/README.md).
- **Blocking dependencies.** Final approval of a claim reached only through a
  secondary source cannot be recorded as verified primary support.
- **Fail-closed.** Every secondary-origin claim stays **warned and unapproved** until
  a human approval is recorded against the exact revision. The warned state is entered
  only from an **actually-read** supporting source plus an **adjudicated missing
  upstream origin** — never from an unconfigured tracing limit (see POL-OPS-TRACE).
- **numeric_value:** `UNSET` (not numeric).

---

## 4. Accepted technical direction (user-gated; accepted 2026-10-27)

Everything in this section was a **proposal** chosen against the Step 00 spike
evidence; the user **accepted the direction** in the recorded Step 01 interview
(after the runtime language was already confirmed, section 0). The acceptance covers
the **described behavior and direction** — not every schema field, and not an
approval of any executable architecture or of the exact schema spelling, which remain
implementation-facing, versioned details for Step 02 onward. Each entry below records
the accepted direction and the interview amendment.

### ARCH-STORE — One app-owned project-scoped SQLite store + minimal app-level registry

- **Accepted direction.** One app-owned project-scoped SQLite store per project, plus a
  **minimal application-level registry** that maps application project IDs to store
  locations and performs **active-project arbitration**. Application project IDs are
  independent of paths or Git ([02 lines 9, 16](../implementation/02-project-foundation.md)).
- **Alternatives.** (a) a single global SQLite file with a project column;
  (b) one SQLite file per project + a registry file; (c) an embedded daemon store.
- **Interview amendment (2026-10-27).** The user selected **approve direction**:
  per-project SQLite plus a **small atomic global registry/arbitration**. The exact
  schema remains an implementation-facing, versioned contract.
- **User evidence.** `"Approve direction (Recommended)"` — 2026-10-27.
- **Decided against revision.** `ee7f7eafca80baa6c1aaca278361e56a663493c3`.
- **Evidence.** SQLite is the accepted local store direction; "prefer the simplest
  design that passes the recovery tests"; do not depend on OpenCode's private database
  ([spec line 16](../specification.md)). The Step 00 harness persisted sessions across
  a real restart using SQLite at `$XDG_DATA_HOME/opencode/opencode.db`
  ([harness README](../../spikes/runtime/harness/README.md)); the spike did **not**
  define an application schema.
- **Limitations.** The schema is an implementation-facing, versioned detail, and the
  store must satisfy untested recovery/migration/backup criteria
  ([02 lines 20–21](../implementation/02-project-foundation.md)). A registry is an
  app-owned file, not an OpenCode database.
- **Remaining detail.** Store-per-project vs single-store and the registry shape are
  recorded as accepted direction; the exact schema is Step 02 work under this
  direction.

### ARCH-ASSETS — Immutable assets and revision dependencies

- **Accepted direction.** Assets are immutable revisions identified by the canonical
  tuple `{project_id, artifact_id, revision_id}`; approvals and dependency edges
  reference those exact tuples, not filenames or generic permissions
  ([spec lines 54–55](../specification.md)). An approved artifact edited
  afterward becomes a proposal and invalidates affected checks
  ([02 lines 16–17](../implementation/02-project-foundation.md)).
- **Alternatives.** (a) hash-addressed immutable revisions + dependency edges;
  (b) mutable rows with an audit log; (c) external content store + manifest.
- **Interview amendment (2026-10-27).** The user selected **approve direction**:
  **immutable assets/revisions** and **exact immutable approval/dependency/rights
  revisions**.
- **User evidence.** `"Approve direction (Recommended)"` — 2026-10-27.
- **Decided against revision.** `ee7f7eafca80baa6c1aaca278361e56a663493c3`.
- **Evidence.** Confirmed invariant that approvals bind to immutable revisions and
  that a model edit cannot replenish a review budget; Step 02 deliverable "immutable
  artifact revisions … approvals/dependencies"
  ([02 lines 10–11](../implementation/02-project-foundation.md)).
- **Limitations.** The exact revision identity/hash scheme remains an
  implementation-facing detail; the invalidation propagation graph is Step 02 work.
- **Remaining detail.** Revision identity/hash scheme and invalidation graph are
  Step 02 work under the accepted immutable-revision direction.

### ARCH-IPC — Local authenticated IPC

- **Accepted direction.** Owner-only, authenticated local IPC between CLI and the
  background worker; no root or public unauthenticated listener required;
  authentication rejects non-owner control ([03 lines 9, 22](../implementation/03-durable-worker.md)).
- **Alternatives.** (a) authenticated loopback HTTP; (b) OS-owner-scoped socket/pipe;
  (c) an **embedded in-process worker — excluded**: a worker embedded in the CLI
  process cannot continue after the CLI closes, violating the durable-continuity
  requirement ([spec line 12](../specification.md)).
- **Interview amendment (2026-10-27).** The user selected **approve direction**: an
  **independent worker** reached over an **owner-only authenticated Unix-domain socket
  on Linux**, **separate from the OpenCode HTTP service**.
- **User evidence.** `"Approve direction (Recommended)"` — 2026-10-27.
- **Decided against revision.** `ee7f7eafca80baa6c1aaca278361e56a663493c3`.
- **Evidence.** The accepted runtime decision's defaults: explicit endpoint and
  credential only, loopback-only, fail-closed, redirects refused
  ([decision 001](../decisions/001-runtime-language.md)); the harness binds
  `127.0.0.1` with a per-run password and refuses foreign control
  ([harness README](../../spikes/runtime/harness/README.md)). Step 03 requires
  authenticated local control.
- **Limitations.** The wire protocol remains an implementation-facing detail; the
  transport choice is untested as an application contract.
- **Remaining detail.** The socket protocol/encoding is Step 03 work under the
  accepted owner-only authenticated UDS direction.

### ARCH-PKG — Python package layout

- **Accepted direction.** A Python-core package rooted at `src/review_article_agent/`
  with bounded modules `domain/`, `storage/`, `cli/`, `worker/`, `adapters/` (the
  OpenCode HTTP integration isolated behind one adapter module), and **no agent
  framework**.
- **Alternatives.** (a) single application package with adapter submodules;
  (b) core package + separate tool processes; (c) core + plugin packages.
- **Interview amendment (2026-10-27).** The user selected **approve direction**:
  `src/review_article_agent/{domain,storage,cli,worker,adapters}`, **no framework**.
- **User evidence.** `"Approve direction (Recommended)"` — 2026-10-27.
- **Decided against revision.** `ee7f7eafca80baa6c1aaca278361e56a663493c3`.
- **Evidence.** Python core is accepted; the tested Jev route is the Python
  direct-OpenRouter adapter; the native route is unexercised
  ([decision 001](../decisions/001-runtime-language.md)). The Python prototype is
  stdlib-only.
- **Limitations.** New external dependencies remain an implementation-facing detail;
  CLI spelling is Step 02, not here.
- **Remaining detail.** Dependency surface and CLI spelling are Step 02 work under
  the accepted package layout.

### ARCH-MIGRATE — Migrations and backup/WAL consistency

- **Accepted direction.** Versioned storage migrations with manifest/schema versions;
  a consistent backup/export and restore that captures WAL state, verified including
  while writes occur during backup; unsupported newer schemas fail clearly; restore
  verifies artifact hashes and approvals
  ([02 lines 10, 20–21](../implementation/02-project-foundation.md)).
- **Alternatives.** (a) SQLite online backup / snapshot API with versioned
  migrations; (b) file-level copy of db+`-wal`+`-shm` — **rejected**: a live arbitrary
  file copy is non-compliant with the baseline requirement to use SQLite's
  snapshot/backup mechanism and to account for WAL state; (c) export/import manifest.
- **Interview amendment (2026-10-27).** The user selected **approve direction**:
  **consistent backup plus manifest/hash/approval verification**.
- **User evidence.** `"Approve direction (Recommended)"` — 2026-10-27.
- **Decided against revision.** `ee7f7eafca80baa6c1aaca278361e56a663493c3`.
- **Evidence.** Step 02 explicitly forbids copying "only the live database file and
  ignor[ing] WAL state" and requires using SQLite's snapshot/backup mechanism, not an
  arbitrary copy of a live WAL database
  ([02 line 20](../implementation/02-project-foundation.md);
  [README line 88](../implementation/README.md)). The Step 00 harness demonstrated
  durable persistence across process restart on the same data home.
- **Limitations.** No migration tool or schema version exists yet; WAL-consistency
  during concurrent writes is a test obligation of Step 02, not proven here.
- **Remaining detail.** Migration tooling and schema versioning are Step 02 work
  under the accepted backup/verification direction.

### ARCH-ACTIVE-ENF — Active-project enforcement mechanism (invariant already confirmed)

- **Invariant status.** The **invariant** "exactly one active project at a time
  globally" is **already confirmed baseline** ([spec line 11](../specification.md);
  [README line 66](../implementation/README.md)) and is **not** reopened here. Only
  the **enforcement mechanism** was `proposed`; it is now **accepted**.
- **Accepted direction (mechanism).** Concurrent activation attempts cannot execute two
  projects; a safe switch reconciles or drains existing work before another project
  executes and cannot leave old-project jobs committing into the new project
  ([03 lines 11, 19](../implementation/03-durable-worker.md)). Waiting gates release
  execution resources.
- **Alternatives.** (a) a single global lock/lease for the active project;
  (b) per-project locks with a global arbiter; (c) a single-worker model.
- **Interview amendment (2026-10-27).** The user selected **approve direction**: a
  **minimal atomic global registry/arbitration** (consistent with ARCH-STORE).
- **User evidence.** `"Approve direction (Recommended)"` — 2026-10-27.
- **Decided against revision.** `ee7f7eafca80baa6c1aaca278361e56a663493c3`.
- **Evidence.** Confirmed baseline "one active project at a time"
  ([spec line 11](../specification.md)); global definition of done "Exactly one
  project may execute at once"
  ([README line 66](../implementation/README.md)); the Step 00 harness used `flock`
  to exclude concurrent mutation ([harness README](../../spikes/runtime/harness/README.md)).
- **Limitations.** **Within-project** concurrency may be bounded independently
  ([spec line 11](../specification.md)) and is not defined here; fencing protects
  accepted writes, not arbitrary external side effects
  ([03 line 20](../implementation/03-durable-worker.md)).
- **Remaining detail.** The registry mechanism is accepted; the confirmed invariant
  needs no new gate.

### ARCH-RECOVERY — Recovery semantics (accepted per the contract)

- **Accepted semantics.** The user selected **approve semantics** for the recovery
  behavior the sibling contract
  [`02-gates-and-runtime.md`](02-gates-and-runtime.md) specifies: CLI detach
  continues an authorized background job; pause/cancel stop scheduling and preserve
  an **unknown** remote outcome; a safe project switch reconciles or quiesces; there
  is **no silent success, no blind retry, and no budget reset**; an ambiguous remote
  outcome is dispositioned by a human.
- **User evidence.** `"Approve semantics (Recommended)"` — 2026-10-27.
- **Decided against revision.** `ee7f7eafca80baa6c1aaca278361e56a663493c3`.
- **Note.** The behavioral contract lives in
  [`02-gates-and-runtime.md`](02-gates-and-runtime.md) (v3.2); this record ratifies
  the described semantics and does not restate or weaken that contract.

---

## 5. Rights and privacy model v1 (behavior accepted 2026-10-27)

The user accepted the **RightsRecord v1 behavior** — the described behavior and the
same-project/exact-revision guarantees below — without accepting every field-level
spelling. The record identity, discriminated-union subject, per-operation typed
basis, and out-of-band authorization binding remain the canonical
implementation-facing contract, subject to versioned revision.

### 5.1 RightsRecord v1 — canonical immutable identity and typing

**Canonical immutable reference.** Across the Step 01 contracts, content is identified
by the canonical immutable tuple `{project_id, artifact_id, revision_id}`. Two tuples
denote the same content revision only if **all three** components match, and a
reference used by a record must be **same-project** as that record. Sibling contract
**C01** maps its reference/projection fields onto this canonical record (mapping below).

**Record identity (immutable).** A `RightsRecord` v1 is immutable and identified by
`{project_id, rights_id, record_revision}`; any change produces a **new**
`record_revision` (a new immutable record), never an in-place edit.

**Subject — discriminated union.** The record's subject is exactly one of:

- `{"kind": "source_revision", "project_id": ..., "artifact_id": ..., "revision_id": ...}`
  — used whenever a content **revision exists**.
- `{"kind": "subject_artifact_ref", "project_id": ..., "artifact_id": ...}` — used
  **only** for pre-acquisition / missing-full-text cases where **no content revision
  exists yet**. It carries **no** `revision_id`; a content version is never fabricated.

**Fields (all mandatory):**

| Field | Type | Meaning |
| --- | --- | --- |
| `rights_id` | string | Stable id; identity = `{project_id, rights_id, record_revision}`. |
| `project_id` | string | Owning app-owned project id. |
| `record_revision` | integer | Immutable revision of this record; a change makes a new record. |
| `subject` | object | Discriminated union above (`kind` ∈ `source_revision` \| `subject_artifact_ref`). |
| `operations` | object | Map over the four operations → `allowed` \| `not_allowed` \| `unknown` \| `not_applicable`. |
| `basis` | object | Per-operation typed evidence (shape below). |
| `authorization` | object \| null | Exact **out-of-band** auth provenance `{actor: string, method: enum[out_of_band_authenticated], authorized_at: timestamp, against_subject: <this record's subject>, against_rights_revision: int, basis: {kind, evidence: {ref, provenance}}}` — binds **both** the exact source `subject` **and** the exact immutable rights `record_revision`; `null` until authorized. |
| `restrictions` | string[] | Declared limits, e.g. `no_redistribution`, `no_hosting`, `quote_limit`. |
| `privacy` | object | `{personal_data: bool, confidentiality: public \| confidential \| unknown, denial: bool}`. |
| `recorded_by` | string | Actor that recorded the record. **Not** an authorization. |
| `recorded_at` | string | ISO-8601 timestamp. |
| `derived_status` | enum | `incomplete` \| `valid` \| `inconsistent` — **computed**, not hand-set. |

**Operation enum (canonical).** `allowed` | `not_allowed` | `unknown` |
`not_applicable` — the canonical operation-status values used across the Step 01
contracts (sibling contract C01 uses the same four).

**Operations (four, separate).** `acquisition`, `storage`, `redistribution`,
`hosted_transfer`. The four rights are **separate**: "Acquisition, storage,
redistribution, and hosted-model transfer rights are separate. Uploading a file is not
proof of all rights." ([spec line 63](../specification.md)).

**Basis evidence (typed, per operation).** Each operation's basis is
`{"kind": license|public_domain|statutory|publisher_permission|user_authorization|unknown,
"evidence": {"ref": string, "provenance": string}}`; both `ref` and `provenance` must
be non-empty for `allowed`.

**C01 reference/projection mapping.** C01
([`01-identities.md` §5.6](01-identities.md#56-rightsreference--projection-of-the-canonical-rights-record-i9-p5-accepted),
part of this coordinated contract set) holds a **read-only projection**; its
per-operation fields map by name onto the canonical record:
`acquisition_right -> operations.acquisition`,
`storage_right -> operations.storage`,
`redistribution_right -> operations.redistribution`,
`hosted_model_transfer_right -> operations.hosted_transfer`. C01's projected
`subject` maps onto the canonical `subject` **discriminated union**, and C01's
`basis` is a read-only summary of the canonical per-operation
`basis.<operation>.{kind, evidence:{ref, provenance}}`. A C01 reference pins the
canonical record to its **immutable identity** `{project_id, rights_id,
record_revision}` (C01 typed `RightsRecordRef`); a mutable bare `rights_id` MUST
NOT be used as the reference.

**Status semantics (raw-incomplete vs. valid, with no contradiction).**

1. A record with a missing or ill-typed mandatory field is **raw incomplete input** — a
   **diagnostic only** (`derived_status = incomplete`). It is **not** an operational
   `RightsRecord` and authorizes **nothing**. There is **no accepted operational
   record** until every mandatory field is present and valid.
2. `derived_status = inconsistent` (e.g. `allowed` without evidence, or a subject/evidence
   in a different project) means the **whole record is invalid and authorizes nothing** —
   no partial operation proceeds from it.
3. Only a fully valid record (`derived_status = valid`) yields **rights-only
   eligibility** (section 5.2).
4. **A user cannot confer a nonexistent right.** `user_authorization` records the user's
   own permission to act within the rights they hold; it **cannot** set `redistribution`
   or `hosted_transfer` to `allowed` for a third-party copyrighted source without a
   `license` / `publisher_permission` / `public_domain` basis.
5. **Unknown storage retains metadata only.** When `storage` is `unknown`, only source
   **metadata** is retained (identity/locator/provenance), pending a **lawful quarantine
   disposition**; source **bytes are not unconditionally retained**.
6. **Exports distinguish citation from redistribution.** A **bibliographic citation** of
   a source in the manuscript is governed by attribution and is **not** redistribution.
   Including source **content/excerpts** (redistribution) requires
   `redistribution: allowed`.

### 5.2 Rights eligibility is not final authorization

- **Rights-only eligibility.** A valid `allowed` operation grants **rights-only
  eligibility**. It is **not** final authorization and does **not** bypass any other
  gate: P5 (AI-use / provider / journal terms), budget/concurrency limits, privacy, and
  the exact-revision human approval gates all remain **required**.
- **Privacy denial outranks an allowed right.** If `privacy.denial` is true (or
  confidentiality constraints deny), the affected operation is **blocked** even when its
  rights status is `allowed`.
- **Hosted transfer needs out-of-band authorization.** `recorded_by` (in-band recording)
  **cannot** grant `hosted_transfer`. Hosted-model transfer requires an **out-of-band
  authenticated human authorization** recorded in `authorization` that binds **both** the
  exact source `subject` **and** the exact immutable `record_revision` of this rights
  record (not merely a mutable `rights_id`); no in-band recorded grant confers it. This is
  additional to the P5 gate.
- **Scoped hosted-use authorization.** A hosted-use authorization is granted as a
  **scoped batch**: it names the **project/source manifest**, the **provider/model
  route**, the **purpose**, and the exact **revision**, after the rights/privacy/
  provider gates are satisfied. **Additions and material changes reopen** the batch;
  the batch **cannot override** an individual source's restrictions, and there is
  **no per-source override** of a batch decision.
- **Unknown/not_allowed block; not_applicable never unblocks.** An operation whose right
  is `unknown` or `not_allowed` is **blocked**, not attempted; `not_applicable` never
  unblocks a real operation.

### 5.3 Logging, fixtures, and secrets

- Logs and test fixtures must contain **no secrets and no licensed source text**
  ([01-contracts line 22](../implementation/01-contracts.md);
  [README line 58](../implementation/README.md)). The Step 00 reports record
  endpoints/ports only and include no credential or paper text
  ([handoff](../implementation/handoffs/00-runtime-spike.md);
  [live-jev README](../../spikes/runtime/live-jev/README.md);
  [integration README](../../spikes/runtime/integration/README.md)). This register
  contains no licensed excerpts.

### 5.4 Source content is untrusted

"Treat documents/model outputs as untrusted data. No arbitrary source instructions,
authoritative database edits by agents, hidden approvals, public unauthenticated
services, or credential leakage." ([spec line 62](../specification.md)). Retrieved
source text is data, never instructions that can widen scope, approve, or invoke
unapproved tools ([04 line 17](../implementation/04-agent-runtime.md)).

### 5.5 Provider/journal verification gates its own operation, not everything

Confirming applicable provider terms and target-journal policies is an accepted
**process** under P5/P6 ([spec lines 73–74](../specification.md)). The gates are
**operation-scoped**: **provider-term verification gates hosted-model use**;
**journal-policy verification gates submission-facing approval**. The **absence** of a
known prohibition is **not** permission for those operations — until the provider
terms (for a hosted transfer) or the applicable journal policy (for submission-facing
output) are verified, that operation stays gated. Venue-specific checks apply **only
when a venue is selected** (POL-P6); an unselected or unknown venue is **not** a
blanket prohibition. "Uploading a file is not proof of all rights"
([spec line 63](../specification.md)).

### 5.6 RightsRecord examples (structural/type-checked; illustrative, not authorizations)

All examples are **synthetic** (no licensed text) and are **not** authorizations. A
structural validator checks field **types**, the immutable identity
`{project_id, rights_id, record_revision}`, and the **same-project** invariant on
the subject and on each operation's evidence; it also checks that, when
`authorization` is present, it binds **both** the record's `subject` **and** this
record's exact `record_revision` with `method = out_of_band_authenticated`. A
mismatch makes the record invalid.

**Example A — valid record (public-domain source; content revision exists).** Subject
is the canonical tuple `{project_id, artifact_id, revision_id}`; every operation has
typed evidence; hosted transfer carries an **out-of-band** human authorization.

```json
{
  "rights_id": "RIGHTS-syn-A",
  "project_id": "proj-syn-1",
  "record_revision": 1,
  "subject": {"kind": "source_revision", "project_id": "proj-syn-1", "artifact_id": "art-syn-A", "revision_id": "rev-syn-A"},
  "operations": {
    "acquisition": "allowed",
    "storage": "allowed",
    "redistribution": "allowed",
    "hosted_transfer": "allowed"
  },
  "basis": {
    "acquisition": {"kind": "public_domain", "evidence": {"ref": "lic:pd-syn-A", "provenance": "publisher:pd-list"}},
    "storage": {"kind": "public_domain", "evidence": {"ref": "lic:pd-syn-A", "provenance": "publisher:pd-list"}},
    "redistribution": {"kind": "public_domain", "evidence": {"ref": "lic:pd-syn-A", "provenance": "publisher:pd-list"}},
    "hosted_transfer": {"kind": "public_domain", "evidence": {"ref": "lic:pd-syn-A", "provenance": "publisher:pd-list"}}
  },
  "authorization": {
    "actor": "human:user",
    "method": "out_of_band_authenticated",
    "authorized_at": "2026-10-27T00:00:00Z",
    "against_subject": {"kind": "source_revision", "project_id": "proj-syn-1", "artifact_id": "art-syn-A", "revision_id": "rev-syn-A"},
    "against_rights_revision": 1,
    "basis": {"kind": "public_domain", "evidence": {"ref": "lic:pd-syn-A", "provenance": "publisher:pd-list"}}
  },
  "restrictions": [],
  "privacy": {"personal_data": false, "confidentiality": "public", "denial": false},
  "recorded_by": "agent:recorder",
  "recorded_at": "2026-10-27T00:00:00Z",
  "derived_status": "valid"
}
```

Expected outcome: **rights-only eligible** for the four operations — **not** final
authorization. P5 terms, budget/concurrency limits, privacy, and the exact user
approval gates **remain required**. `recorded_by` is an agent and confers nothing;
`hosted_transfer` eligibility rests on the out-of-band `authorization` entry.

**Example B — invalid/inconsistent (the whole record authorizes nothing).**

```json
{
  "rights_id": "RIGHTS-syn-B",
  "project_id": "proj-syn-1",
  "record_revision": 1,
  "subject": {"kind": "source_revision", "project_id": "proj-syn-1", "artifact_id": "art-syn-B", "revision_id": "rev-syn-B"},
  "operations": {
    "acquisition": "allowed",
    "storage": "allowed",
    "redistribution": "unknown",
    "hosted_transfer": "allowed"
  },
  "basis": {
    "acquisition": {"kind": "statutory", "evidence": {"ref": "law:syn", "provenance": "statute:id"}},
    "storage": {"kind": "statutory", "evidence": {"ref": "law:syn", "provenance": "statute:id"}},
    "redistribution": {"kind": "unknown", "evidence": {"ref": "", "provenance": ""}},
    "hosted_transfer": {"kind": "user_authorization", "evidence": {"ref": "", "provenance": ""}}
  },
  "authorization": null,
  "restrictions": ["no_hosting"],
  "privacy": {"personal_data": false, "confidentiality": "public", "denial": false},
  "recorded_by": "human:user",
  "recorded_at": "2026-10-27T00:00:00Z",
  "derived_status": "inconsistent"
}
```

Rejected: `hosted_transfer: allowed` has empty evidence `ref`/`provenance` (status
rule 1); a `user_authorization` basis cannot confer `hosted_transfer` on a third-party
source; and in-band `recorded_by` cannot grant it (status rule 4, §5.2). Because the
record is `inconsistent`, **the whole record authorizes nothing** — no operation
(including acquisition/storage) proceeds from it. Previously valid rights are
**separate** records and are unaffected; a corrected **new valid revision**
(`record_revision: 2`) may be recorded authorizing only the rights-clean operations.

**Example C — pre-acquisition / missing full text (no content revision).** Uses the
`subject_artifact_ref` discriminator with **no** `revision_id`; nothing implies a
content version.

```json
{
  "rights_id": "RIGHTS-syn-C",
  "project_id": "proj-syn-1",
  "record_revision": 1,
  "subject": {"kind": "subject_artifact_ref", "project_id": "proj-syn-1", "artifact_id": "art-syn-C"},
  "operations": {
    "acquisition": "unknown",
    "storage": "unknown",
    "redistribution": "unknown",
    "hosted_transfer": "unknown"
  },
  "basis": {
    "acquisition": {"kind": "unknown", "evidence": {"ref": "", "provenance": ""}},
    "storage": {"kind": "unknown", "evidence": {"ref": "", "provenance": ""}},
    "redistribution": {"kind": "unknown", "evidence": {"ref": "", "provenance": ""}},
    "hosted_transfer": {"kind": "unknown", "evidence": {"ref": "", "provenance": ""}}
  },
  "authorization": null,
  "restrictions": [],
  "privacy": {"personal_data": false, "confidentiality": "unknown", "denial": false},
  "recorded_by": "agent:recorder",
  "recorded_at": "2026-10-27T00:00:00Z",
  "derived_status": "valid"
}
```

Expected: a **valid** record whose operations are all `unknown` → **all four blocked**;
only source **metadata** is retained pending a lawful quarantine disposition (status
rule 5). No content revision is asserted.

**Example D — raw incomplete input (diagnostic only).** A record missing mandatory
fields (`basis`, `authorization`, `privacy`) is **raw incomplete input**:
`derived_status = incomplete`. It is **not** an operational record and authorizes
**nothing**.

```json
{
  "rights_id": "RIGHTS-syn-D",
  "project_id": "proj-syn-1",
  "record_revision": 1,
  "subject": {"kind": "source_revision", "project_id": "proj-syn-1", "artifact_id": "art-syn-D", "revision_id": "rev-syn-D"},
  "operations": {"acquisition": "allowed", "storage": "allowed", "redistribution": "unknown", "hosted_transfer": "unknown"},
  "derived_status": "incomplete"
}
```

`basis`, `authorization`, and `privacy` are absent, so a validator reports `incomplete`
and rejects this as an operational record; no field here authorizes any operation.

---

## 6. Recorded dispositions and remaining project-setup gates

Every Step 01 policy/technical item has a recorded disposition (section 6.1). The
items below are **not** Step 01 blockers: they are **future project-setup gates** —
per-project values or verifications that must be established **before the affected
work** for a given project (section 6.2).

### 6.1 Recorded dispositions (2026-10-27)

| Record | Disposition |
| --- | --- |
| POL-P1 … POL-P6 | **accepted**, with interview amendments (section 2) |
| POL-OPS-PILOT, POL-OPS-SEVERITY, POL-OPS-COVERAGE, POL-OPS-TRACE, POL-OPS-SECONDARY | **accepted** (section 3) |
| POL-OPS-COST | **accepted** with the `$25 / $10 / 2` defaults (section 3) |
| ARCH-STORE, ARCH-ASSETS, ARCH-IPC, ARCH-PKG, ARCH-MIGRATE, ARCH-ACTIVE-ENF | **accepted** direction (section 4) |
| ARCH-RECOVERY | **accepted** semantics per [`02-gates-and-runtime.md`](02-gates-and-runtime.md) (section 4) |
| RIGHTS-v1 | **accepted** behavior, incl. same-project / exact-revision guarantees (section 5) |

### 6.2 Project-setup gates (per project; not Step 01 blockers)

1. an explicit **pilot/audit sample size and acceptance plan** before bulk work;
2. a **rubric-specific Jev mapping / uncertainty region**, calibrated and approved in
   the pilot (no universal cutoff);
3. explicit **maximum hops, sources per claim/project, and resolution retries**
   before any upstream tracing work (no universal default);
4. actual **source rights/privacy and provider authorizations** before the affected
   operations;
5. optional **venue actual current-policy checks** before submission-facing claims
   (only when a venue is selected).

No further Step 01 decision is required. None of these gates invents a numeric
scientific threshold; where the user's policy is "per project", the number is set at
project setup, not here.

---

## 7. Status summary

| Record | Topic | Owner | Status | Numeric | Blocks |
| --- | --- | --- | --- | --- | --- |
| POL-P1 | Framework contribution / concept criteria | user | **accepted (2026-10-27)** | UNSET | — |
| POL-P2 | Downstream causal/context-fidelity checks | user | **accepted (2026-10-27)** | UNSET | — |
| POL-P3 | Integrity-status checks | user | **accepted (2026-10-27)** | UNSET | — |
| POL-P4 | Public selection/synthesis + limitations reporting | user | **accepted (2026-10-27)** | UNSET | — |
| POL-P5 | AI-use declaration / accountability / provider+journal terms | user | **accepted (2026-10-27)** | UNSET | provider gate (hosted use), journal gate (submission) |
| POL-P6 | Target-venue constraints + submission verification | user | **accepted (2026-10-27)** | UNSET | only when a venue is selected |
| POL-OPS-PILOT | Pilot/audit sampling and acceptance | user | **accepted (2026-10-27)** | UNSET | project-setup gate |
| POL-OPS-SEVERITY | Issue severity scale | user | **accepted (2026-10-27)** | UNSET | — |
| POL-OPS-COVERAGE | Coverage/stopping rule | user | **accepted (2026-10-27)** | UNSET | human freeze required |
| POL-OPS-COST | Cost/concurrency limits | user | **accepted (2026-10-27)** | 25 / 10 / 2 | paid caps |
| POL-OPS-TRACE | Tracing hop/source/retry defaults | user | **accepted (2026-10-27)** | UNSET | project-setup gate |
| POL-OPS-SECONDARY | Secondary-warning operational acceptance | user | **accepted (2026-10-27)** | UNSET | exact-revision human approval |
| ARCH-STORE | Store + registry + active-project arbitration | user | **accepted (2026-10-27)** | — | — |
| ARCH-ASSETS | Immutable assets + revision dependencies | user | **accepted (2026-10-27)** | — | — |
| ARCH-IPC | Local authenticated IPC | user | **accepted (2026-10-27)** | — | — |
| ARCH-PKG | Python package layout | user | **accepted (2026-10-27)** | — | — |
| ARCH-MIGRATE | Migrations + backup/WAL consistency | user | **accepted (2026-10-27)** | — | — |
| ARCH-ACTIVE-ENF | Active-project enforcement mechanism (invariant confirmed) | user | **accepted (2026-10-27)** | — | — |
| ARCH-RECOVERY | Recovery semantics (per C02) | user | **accepted (2026-10-27)** | — | — |
| RIGHTS-v1 | Canonical `RightsRecord` v1 tuples + rights-only eligibility | user | **accepted (2026-10-27)** | — | — |

**All Step 01 policy/technical records above are accepted (2026-10-27), anchored to
the reviewed revision `ee7f7eafca80baa6c1aaca278361e56a663493c3`.** Dependent
behavior is unblocked; the exact schema spelling remains an implementation-facing,
versioned detail, and **no scientific feature is implemented by this register.**

---

## 8. Related records

- Baseline: [`docs/specification.md`](../specification.md) — "Pending decisions",
  "Gates and scientific invariants", "Citation-origin policy", "Product and runtime".
- Step authority: [`docs/implementation/01-contracts.md`](../implementation/01-contracts.md);
  kickoff [`docs/implementation/kickoffs/01-contracts.md`](../implementation/kickoffs/01-contracts.md).
- Ratification record:
  [`docs/decisions/002-step01-contracts-and-policies.md`](../decisions/002-step01-contracts-and-policies.md).
- Decision format: [`docs/implementation/decision-template.md`](../implementation/decision-template.md);
  adopted language decision [`docs/decisions/001-runtime-language.md`](../decisions/001-runtime-language.md).
- Step 00 evidence: [`docs/implementation/handoffs/00-runtime-spike.md`](../implementation/handoffs/00-runtime-spike.md);
  plan index [`docs/implementation/README.md`](../implementation/README.md).
- Consumers of Step 01 decisions:
  [`02 §CLI, projects, artifacts, backups`](../implementation/02-project-foundation.md),
  [`04 §restricted agent roles`](../implementation/04-agent-runtime.md)
  (and the worker in [`03 §background worker`](../implementation/03-durable-worker.md)).

No secrets and no licensed full text are included in this document.
