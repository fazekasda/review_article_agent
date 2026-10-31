# Operator guide — approving the human gates

This guide is for the project owner running a first real pilot review. It explains,
gate by gate, what you are approving and exactly which command to type. You do not
need to read the source to use it.

Everything below is derived from the code and the contract documents; the exact
spellings are checked against [`cli.md`](cli.md) §11 (`gate`), §19 (`pipeline
status`), §20–24 (`review`/`tracing`/`delivery`/`generation run`/`pipeline run`),
[`generation.md`](generation.md), [`discovery.md`](discovery.md),
[`acquisition.md`](acquisition.md), and
[`src/review_article_agent/pipeline/status.py`](../src/review_article_agent/pipeline/status.py)
(the stage table that decides which gate is next).

Throughout, `$DD` is your data directory and `<PROJECT_ID>` is the `prj_…` id from
`list`/`create`. If you run from a checkout instead of an installed console script,
replace `review-article-agent` with `PYTHONPATH=src python3 -m review_article_agent`.

---

## 1. How gates work

A **gate** is one named, human decision that a stage needs before it becomes
authoritative. A stage's proposal revision is inert data; it only counts once its
exact named gate is recorded.

- **Approvals bind to the exact artifact revision (and its whole dependency
  closure), not to a filename or a generic permission.** `gate prepare` builds a
  snapshot of the target revision plus every transitive dependency and pins each
  one by digest.
- **Any change makes the approval stale.** Edit the artifact, or change any pinned
  upstream revision or rights record, and the recorded approval is recomputed as
  `stale` (or `superseded`); `pipeline status` will ask for the gate again. Status
  is always recomputed on read, never carried forward.
- **A model can never approve.** There is no in-band `approved`/`actor`/`authority`
  flag anywhere, and an imported document cannot grant an approval. The only path
  to authority is the owner-authenticated `gate confirm`.
- **`gate confirm` must run on a real interactive terminal.** It opens `/dev/tty`
  directly (never standard input), shows the exact bound action plus a random
  challenge, and requires you to type that challenge. Piped input, a `--yes` flag,
  or an actor/imported-approval flag can never confirm. If there is no controlling
  terminal (for example, you are in a non-interactive script or a CI job), the
  command refuses with a `storage` error. What this proves is narrow: the process
  runs with your effective UID and your terminal produced the challenge — it is
  not proof of a biological human, and it is not protection against another
  process running as the same user.
- **The flow is `gate prepare` → read → `gate confirm --snapshot-digest`.**
  `gate prepare` is a **read-only preview** that prints a `snapshot_digest` and
  records nothing. `gate confirm` re-prepares the same snapshot, refuses unless the
  `--snapshot-digest` you pass equals the freshly computed digest, then takes the
  terminal confirmation. A `prepare`→`confirm` sequence therefore cannot silently
  bless different pins.
- **Rejecting is supported.** `--decision` accepts `approved` or `rejected`. A
  `rejected` decision is recorded as history but does **not** satisfy the gate, so
  the stage stays blocked. After a rejection you regenerate or edit the artifact
  (a new immutable revision), then re-run `pipeline run`/`pipeline status` and
  prepare/confirm the new revision. You can also record a rejection for the record
  and fix the artifact in one go.

Both `gate prepare` and `gate confirm` take the same identity flags; the only
difference is that `confirm` additionally requires `--snapshot-digest` (and accepts
an optional `--timeout-seconds`).

```console
# 1. Preview (read-only; prints snapshot_digest). Records nothing.
review-article-agent --data-dir "$DD" gate prepare \
  --artifact-id <ARTIFACT_ID> --revision-id <REVISION_ID> \
  --gate-kind <GATE_KIND> --gate-id <GATE_ID> \
  --decision approved --rationale "<your reason>" \
  --project <PROJECT_ID>

# 2. Confirm on your real terminal; paste the printed digest.
review-article-agent --data-dir "$DD" gate confirm \
  --artifact-id <ARTIFACT_ID> --revision-id <REVISION_ID> \
  --gate-kind <GATE_KIND> --gate-id <GATE_ID> \
  --decision approved --rationale "<your reason>" \
  --snapshot-digest <SHA256_FROM_GATE_PREPARE> \
  --project <PROJECT_ID>
```

You normally do **not** have to build these by hand: `pipeline status` prints the
exact `gate prepare` line in `blocked_on.detail` and the exact `gate confirm` line
in `blocked_on.next_command`, already filled in with the artifact/revision id and
the gate kind/id. Copy them and add your `--rationale` and the digest.

`gate status`/`gate list` are read-only history views (`active`/`stale`/
`superseded`/`invalidated`); `satisfies_named_gate` is the only satisfaction claim
and is true only for an `active` + `approved` exact gate.

---

## 2. The daily loop

Run this loop whenever you sit down to work on a project.

```console
# A. Where am I, and what is the exact next action?
review-article-agent --data-dir "$DD" pipeline status --project <PROJECT_ID>
```

Read the JSON:

- `stage` — the current stage id, or `"complete"`.
- `completed` — stages whose own `require_*` check already passes.
- `blocked_on` — `null` (not started) or an object with `kind`, `detail`, and
  `next_command`. The kinds you will see are `human_gate`, `hosted_use`,
  `budget`, `failure` (and `human` for a finally-approved delivery whose export is
  still pending).
- `next_action` — for a gate, the artifact/revision/gate identity; for a not-yet
  started stage, the exact command that produces the proposal.

```console
# B. If blocked_on.kind == "human_gate": inspect the artifact first.
#    (Use the show command for that stage — see the per-gate sections below.)

# C. Prepare the gate (read-only), then confirm on your terminal.
review-article-agent --data-dir "$DD" gate prepare  ...   # copy from blocked_on.detail
review-article-agent --data-dir "$DD" gate confirm  ...   # copy from next_command + digest

# D. Drive the next machine jobs until the next gate/budget/pause/failure.
review-article-agent --data-dir "$DD" pipeline run --config pipeline.json --project <PROJECT_ID>
```

`pipeline run` re-derives everything from durable state, submits only the current
stage's machine jobs, and stops at the next human gate, budget limit, pause, or
failure, reporting the exact next command. It is idempotent: a job already present
under its deterministic request id is never re-submitted.

`pipeline run` deliberately submits **nothing** for the human-designed stages:
discovery, acquisition, parsing, screening, extraction, and any stage whose config
role/template is missing. Those you drive with the manual commands shown by
`pipeline status`.

---

## 3. Gates in pipeline order

The pipeline stage order (from the stage table) is: discovery plan → discovery
coverage → discovery freeze → acquisition manifest → parsing reports → screening
pilot → screening exclusions → extraction config → extraction pilot → extraction
batch → extraction outputs → synthesis memo → synthesis framework → synthesis
outline → drafting blueprint → review → tracing → delivery. Only the gated stages
are documented below; the gate-less stages (discovery coverage, parsing, review,
tracing) are produced and checked by their own commands and have no approval to
record.

For every gate, the identity comes from `pipeline status`:
`--artifact-id`/`--revision-id` from `blocked_on.next_command`, and `--gate-kind`/
`--gate-id` as listed here.

### 3.1 Discovery plan — `protocol` / `discovery.plan`

**What you are approving.** The brief/interview/seed/framing/scope/rubric context
and the combined search plan: which sources, which queries, and the bounds that
every later discovery job must obey.

**Inspect before approving.**
```console
review-article-agent --data-dir "$DD" discovery context show \
  --artifact-id <PLAN_ARTIFACT_ID> --content --project <PROJECT_ID>
```
Also approve each **seed** individually (per-seed gate
`artifact_revision` / `discovery.seed.<artifact_id>`); a seed abstract stays
provisional even after its seed approval.

**Checklist.**
- The queries match the actual research question (terms, synonyms, and the
  documented Boolean structure), not a generic keyword.
- The publication-year filter and language/source boundaries are what you intended.
- Citation-expansion limits are explicit and reasonable (there is **no** universal
  default; you set them).
- The plan's direct references resolve to the types they should (`brief_ref`,
  `interview_ref`, `framing_ref`, `scope_ref`, `rubric_ref`, and each seed).
- Seeds you meant to reject are excluded, not merely commented on.

**Approve.** Use the `protocol` / `discovery.plan` gate on the plan revision (and
`artifact_revision` / `discovery.seed.<artifact_id>` for each seed), following the
§1 flow.

**Common reasons to reject.** Queries too narrow/broad for the question; wrong
year window; a rubric that does not reflect your inclusion/exclusion intent; a
seed you did not intend to include.

### 3.2 Discovery freeze — `artifact_revision` / `discovery.freeze`

**What you are approving.** The exact frozen candidate roster: the corpus revision
you will acquire and analyse. A freeze is an explicit human approval; nothing in a
record implies it.

**Inspect before approving.**
```console
review-article-agent --data-dir "$DD" discovery report show \
  --artifact-id <COVERAGE_ARTIFACT_ID> --content --project <PROJECT_ID>
review-article-agent --data-dir "$DD" discovery freeze show \
  --artifact-id <FREEZE_ARTIFACT_ID> --content --project <PROJECT_ID>
```
(`discovery report show` is the coverage report; `discovery freeze show` is the
freeze proposal. Both default to a safe summary and return private content only
with `--content`.)

**Checklist.**
- Every candidate in the roster has been reviewed (a `freeze` requires every entry
  reviewed; only a `search_again` decision may carry unresolved gaps).
- Each `include` is a resolved candidate whose four integrity statuses are all
  **known** (`unknown` is not clean).
- You have acknowledged the coverage limitations explicitly (a freeze over an
  explicit coverage gap is refused without a literal acknowledgement).
- The decision is `freeze`, not `search_again`, if you intend to proceed.
- No obvious known-relevant paper is silently missing.

**Approve.** `artifact_revision` / `discovery.freeze` on the freeze revision.

**Common reasons to reject.** Coverage gaps you consider material; an `include`
with an `unknown` integrity status; a candidate you know is ineligible; deciding to
search again instead.

### 3.3 Acquisition manifest — `artifact_revision` / `acquisition.complete`

**What you are approving.** That for every frozen candidate there is an explicit
disposition and, where required, a pinned, human-reviewed full-text source — i.e.
acquisition is complete for this roster.

**Inspect before approving.**
```console
review-article-agent --data-dir "$DD" acquisition queue \
  --freeze-artifact-id <FREEZE_ARTIFACT_ID> --freeze-revision-id <FREEZE_REVISION_ID> \
  --project <PROJECT_ID>
review-article-agent --data-dir "$DD" acquisition manifest show \
  --artifact-id <MANIFEST_ARTIFACT_ID> --content --project <PROJECT_ID>
```
(`acquisition queue` lists blockers and is read-only; it works before any manifest
exists. An empty queue is **not** an acquisition approval.)

**Checklist.**
- Every frozen candidate has a disposition (`use_full_text`, `exclude`,
  `no_scientific_use`, or `defer`).
- Every `use_full_text` entry pins a source revision whose bytes were re-validated
  against the identifiers of the actual frozen candidate.
- PDF and missing-identifier sources have an explicit exact `human_review`
  (`identity_confirmed`, `full_text_readable`, `complete_article`, etc.).
- Required figures/tables/supplements are `available`; an `unavailable`/`unknown`
  required asset is a blocker.
- No `unknown` integrity status is treated as clean (P3: unknown is not clean).
- No blockers remain (`acquisition manifest status` reports whether acquisition
  holds).

**Approve.** `artifact_revision` / `acquisition.complete` on the manifest revision.
This authorizes acquisition completion only — never screening, seeding, pilots, or
hosted use.

**Common reasons to reject.** A blocker you do not accept; a wrong-paper source;
an unreviewed PDF; a missing required supplement.

**Note.** The gate snapshot closure is bounded by `MAX_GATE_SNAPSHOT_ENTRIES =
10000`. A very large roster can make `gate prepare` block; there is no hidden
truncation.

### 3.4 Screening pilot — `pilot` / `screening.pilot`

**What you are approving.** The mandatory human-adjudicated pilot on one iterative
example set: every case adjudicated, with no blocking failures.

**Inspect before approving.**
```console
review-article-agent --data-dir "$DD" screening show \
  --kind pilot --artifact-id <PILOT_ARTIFACT_ID> --revision-id <PILOT_REVISION_ID> \
  --content --project <PROJECT_ID>
```

**Checklist.**
- Every case has a human adjudication (`retain`/`exclude`/`uncertain`) with
  `inspected_source: true` and a non-empty rationale.
- The case count is at least the configured `pilot_size`.
- No important failure and no **false exclusion** (a case the machine proposed to
  exclude but you judged `retain`/`uncertain`) — either blocks the pilot, and a
  false exclusion needs a corrective action.
- The failure modes you care about are represented, not just easy cases.

**Approve.** `pilot` / `screening.pilot` on the pilot revision.

**Common reasons to reject.** A false exclusion with no corrective action; a case
you did not actually inspect; too few cases; the model clearly inadequate for the
task (change model/instructions and re-pilot).

### 3.5 Screening exclusions — `exclusion_set` / `screening.exclusions`

**What you are approving.** The bulk exclusion batch, after the sampled
random + risk-based original-source checks, with no important false exclusion.

**Inspect before approving.**
```console
review-article-agent --data-dir "$DD" screening show \
  --kind batch --artifact-id <BATCH_ARTIFACT_ID> --revision-id <BATCH_REVISION_ID> \
  --content --project <PROJECT_ID>
review-article-agent --data-dir "$DD" screening show \
  --kind audit --artifact-id <AUDIT_ARTIFACT_ID> --revision-id <AUDIT_REVISION_ID> \
  --content --project <PROJECT_ID>
review-article-agent --data-dir "$DD" screening exclusions \
  --artifact-id <AUDIT_ARTIFACT_ID> --revision-id <AUDIT_REVISION_ID> --project <PROJECT_ID>
```

**Checklist.**
- Every sampled check (the batch's `random_ids` + `risk_ids`) is present, with
  `inspected_source: true` and a rationale.
- No check reports a **false exclusion**. A false exclusion permanently pauses
  that exact batch; correction means a **new batch** and a fresh re-audit.
- The audit's `paused` is false.

**Approve.** `exclusion_set` / `screening.exclusions` on the audit revision.

**Common reasons to reject.** Any false exclusion; an uninspected sampled case;
disagreement with the exclusion boundary.

### 3.6 Extraction config — `batch` / `extraction.config`

**What you are approving.** The extraction configuration: the questions/fields, the
model and parser, normalization, and the sample sizes.

**Inspect before approving.**
```console
review-article-agent --data-dir "$DD" extraction show \
  --kind config --artifact-id <CONFIG_ARTIFACT_ID> --revision-id <CONFIG_REVISION_ID> \
  --content --project <PROJECT_ID>
```

**Checklist.**
- The extraction questions/fields match what the synthesis will need (summaries,
  structured findings, original passages/locators, context, numbers, limitations,
  attribution).
- `pilot_size`/`random_size`/`risk_size` are explicit and sensible.
- The configured model/parser/normalization is what you intend.
- No field is silently defaulted.

**Approve.** `batch` / `extraction.config` on the config revision.

**Common reasons to reject.** Missing fields the synthesis needs; wrong model;
sample sizes too small.

### 3.7 Extraction pilot — `batch` / `extraction.pilot`

**What you are approving.** The human-adjudicated extraction pilot: every
mandatory failure mode has an adjudicated example and there are no blocking
failures.

**Inspect before approving.**
```console
review-article-agent --data-dir "$DD" extraction show \
  --kind pilot --artifact-id <PILOT_ARTIFACT_ID> --revision-id <PILOT_REVISION_ID> \
  --content --project <PROJECT_ID>
```

**Checklist.**
- Every case is adjudicated with `inspected_original: true` and all three
  inspection statements true (`checked_omissions`, `checked_qualifications`,
  `checked_fields`).
- Every mandatory failure-mode label is covered by an adjudicated example.
- No `important_failure` and no `major`/`blocking` severity.
- Values match the original passage (spot-check numbers, units, and attribution);
  omissions are examined, not only wrong findings.

**Approve.** `batch` / `extraction.pilot` on the pilot revision.

**Common reasons to reject.** A missing mandatory failure case; an important
error; values that do not match the source; omitted qualifications.

### 3.8 Extraction batch — `batch` / `extraction.batch`

**What you are approving.** The bulk extraction batch over the acquired sources,
consistent with the approved pilot's pinned model.

**Inspect before approving.**
```console
review-article-agent --data-dir "$DD" extraction show \
  --kind batch --artifact-id <BATCH_ARTIFACT_ID> --revision-id <BATCH_REVISION_ID> \
  --content --project <PROJECT_ID>
```

**Checklist.**
- The batch uses the same config as the pilot, and every case's observed model
  matches the pilot's pinned identity.
- Every case's source reference is in the acquired `use_full_text` roster.
- No case is `blocked`.

**Approve.** `batch` / `extraction.batch` on the batch revision.

**Common reasons to reject.** A model mismatch with the pilot; a source not in the
approved acquisition; a blocked case.

### 3.9 Extraction outputs audit — `batch` / `extraction.outputs`

**What you are approving.** The bulk-extraction output audit: the sampled
random + risk checks of the actual extracted outputs, with no important error. This
is the gate that opens synthesis.

**Inspect before approving.**
```console
review-article-agent --data-dir "$DD" extraction show \
  --kind audit --artifact-id <AUDIT_ARTIFACT_ID> --revision-id <AUDIT_REVISION_ID> \
  --content --project <PROJECT_ID>
```

**Checklist.**
- Every sampled check is present, with `inspected_original: true` and all three
  inspection statements true.
- No check reports an `important_error`. An earlier important error permanently
  pauses that exact batch; a later clean audit cannot erase it — correction means a
  new batch and a fresh re-audit.
- Values match the original passage; units, context, and attribution are correct;
  omissions are examined.

**Approve.** `batch` / `extraction.outputs` on the audit revision.

**Common reasons to reject.** Any important error; a missed omission; a value that
does not match the source.

### 3.10 Synthesis memo — `artifact_revision` / `synthesis.memo`

**What you are approving.** The cross-paper evidence map / synthesis memo over the
accepted extraction outputs.

**Inspect before approving.**
```console
review-article-agent --data-dir "$DD" synthesis show \
  --kind synthesis --artifact-id <SYNTHESIS_ARTIFACT_ID> --revision-id <SYNTHESIS_REVISION_ID> \
  --content --project <PROJECT_ID>
```

**Checklist.**
- Claims trace to actual findings; no invented sources or results.
- Contradictions, applicability limits, and gaps are reported, not smoothed over.
- Uncertainty is preserved; no winner is picked by publication count, model votes,
  or Jev confidence.
- No blocked statement remains.

**Approve.** `artifact_revision` / `synthesis.memo` on the memo revision.

**Common reasons to reject.** A claim with no finding behind it; a hidden
contradiction; over-stated confidence.

### 3.11 Framework — `framework` / `synthesis.framework`

**What you are approving.** The scientific framework over the accepted synthesis:
definitions, scope constraints, relationships, and open questions.

**Inspect before approving.**
```console
review-article-agent --data-dir "$DD" synthesis show \
  --kind framework --artifact-id <FRAMEWORK_ARTIFACT_ID> --revision-id <FRAMEWORK_REVISION_ID> \
  --content --project <PROJECT_ID>
```

**Checklist.**
- Every concept is classified (no `unclassified_concepts`).
- Definitions, role, original support, counterevidence, and applicability limits
  are present where applicable.
- The framework traces to the accepted synthesis (which itself must remain
  approved).

**Approve.** `framework` / `synthesis.framework` on the framework revision.

**Common reasons to reject.** An unclassified concept; a concept with no support;
a framework that overreaches the evidence.

### 3.12 Outline — `outline` / `synthesis.outline`

**What you are approving.** The actual manuscript outline over the same accepted
synthesis and framework.

**Inspect before approving.**
```console
review-article-agent --data-dir "$DD" synthesis show \
  --kind outline --artifact-id <OUTLINE_ARTIFACT_ID> --revision-id <OUTLINE_REVISION_ID> \
  --content --project <PROJECT_ID>
```

**Checklist.**
- The section list and order match the intended argument.
- Each section's intent is supportable by the accepted evidence.
- Eligibility, scientific framework, and outline remain distinct.
- The outline traces to the accepted synthesis **and** framework.

**Approve.** `outline` / `synthesis.outline` on the outline revision.

**Common reasons to reject.** A section with no evidence; a missing section; a
structure that does not match the intended argument.

### 3.13 Drafting blueprint — `artifact_revision` / `drafting.blueprint`

**What you are approving.** A per-section workspace/blueprint: the paragraph-level
plan for one section, naming the sources and evidence for each paragraph, before
drafting.

**Inspect before approving.**
```console
review-article-agent --data-dir "$DD" drafting show \
  --kind blueprint --artifact-id <BLUEPRINT_ARTIFACT_ID> --revision-id <BLUEPRINT_REVISION_ID> \
  --content --project <PROJECT_ID>
```

**Checklist.**
- Every paragraph has a clear intent and names the evidence/sources behind it.
- The blueprint consumes the current approved outline and the section workspace.
- Scientific assertions are tied to appropriate support; nothing is invented.
- This blueprint's own gate is the only one that matters for this section (an
  unapproved other section does not block it).

**Approve.** `artifact_revision` / `drafting.blueprint` on the blueprint revision,
per section.

**Common reasons to reject.** A paragraph with no evidence; a plan that drifts from
the approved outline; an unsupported claim.

### 3.14 Generation hosted-use — `batch` / `generation.hosted_use.<role>`

See §4 below. These gates are not stages in the stage table; they gate the
model-backed roles that a stage needs. `pipeline status` reports a `hosted_use`
blocker (with the exact `gate prepare`/`gate confirm` commands) when the current
stage's role document is missing or unapproved.

### 3.15 Delivery final approval — `artifact_revision` / `delivery.final_approval`

**What you are approving.** The exact-revision final-approval manifest for the
whole manuscript, after the global review, freshness decision, and any notes
batches, and before export.

**Inspect before approving.**
```console
review-article-agent --data-dir "$DD" delivery status \
  --delivery-id <DELIVERY_ID> --project <PROJECT_ID>
```
`delivery approve --prepare` prints the `manifest_digest`, the `manifest_revision`,
and the exact `gate prepare`/`gate confirm` commands for that revision. The gate
must be recorded on the **current** manifest revision; any later notes edit or
re-prepare makes it stale.

**Checklist.**
- The manuscript revision is the one you intend to approve.
- Accepted warnings are the ones you accept (including any explicit secondary-origin
  attribution warnings).
- No unsupported claims remain; the reference-origin coverage audit is complete.
- The export manifest and file hashes are the ones you expect.
- Freshness: you have accepted the cutoff or authorized a top-up.

**Approve.** `artifact_revision` / `delivery.final_approval` on the manifest
revision. Then run `delivery export` (the exact command is reported by
`pipeline status` when the approval is granted but the export is pending). Final
approval alone is not completion: the export must run and record its file hashes.

**Common reasons to reject.** An unsupported claim; a warning you do not accept; a
stale manuscript revision; an inconsistency between the manuscript and the
bibliography.

---

## 4. Hosted-use documents (model roles)

Before a model-backed stage can submit its machine job, each generation role it
needs must have an approved `generation_hosted_use` document. The document
describes the authorization (the concrete model and pricing bound); the authority
is the named gate `batch` / `generation.hosted_use.<role>`.

Roles: `screening`, `synthesis`, `framework`, `outline`, `blueprint`,
`section_writer`, `section_critic`, `section_repair`, `fact_checker`,
`global_reviewer`, `global_writer`, `notes_planner`. (`fact_checker` remains a
deterministic checker with no production model port.)

### Create one per role

```console
review-article-agent --data-dir "$DD" generation hosted-use propose \
  --project <PROJECT_ID> --role <ROLE> \
  --config-file config.json --budget-cap-usd 10.00 \
  --provider-terms-ref "terms:<...>" \
  --privacy-review-ref "privacy:<...>" \
  --pricing-evidence-ref "pricing:<...>" \
  --artifact-id <HOSTED_ARTIFACT_ID> --revision-id <HOSTED_REVISION_ID> \
  --rights-file rights.json
```

- `--config-file` is a strict JSON object holding the `LeadConfig` (the concrete
  model and pricing bound for the role). Use a placeholder model id here; the
  coordinator will provide the actual pilot configs.
- `--budget-cap-usd` is an exact decimal string `> 0`.
- Each `--<field>-ref` is a non-empty reference to the human-reviewed evidence; its
  `verified_at` defaults to the propose instant and may be pinned with
  `--<field>-verified-at`.

Sample `config.json` for a role (placeholder model id; replace with the pilot
config the coordinator provides):

```json
{
  "model_id": "<model-id-from-coordinator>",
  "upstream_provider": "<provider>",
  "max_output_tokens": 1024,
  "context_tokens": 128000,
  "input_price_per_million": "0.5",
  "output_price_per_million": "1.5",
  "pricing_ref": "pricing:<...>",
  "instructions_revision": "<...>",
  "timeout_seconds": 60.0,
  "max_response_bytes": 1048576
}
```

`propose` stores one inert revision and prints a non-authorizing `gate` block whose
`prepare_command`/`confirm_command` are the exact `gate prepare`/`gate confirm`
invocations for `--gate-kind batch --gate-id generation.hosted_use.<role>` on the
new revision. It records no approval.

### Approve it

```console
review-article-agent --data-dir "$DD" generation hosted-use show \
  --artifact-id <HOSTED_ARTIFACT_ID> --project <PROJECT_ID>
# then the exact gate prepare / gate confirm printed by propose (gate kind `batch`,
# gate id `generation.hosted_use.<role>`), following the §1 flow.
```

`hosted-use show` prints the validated document and whether the exact gate is
satisfied for that revision (`false` for a superseded/stale revision).

**Distinct-model rule.** `section_critic` must use a different concrete `model_id`
than `section_writer`, and `global_reviewer` a different one than `global_writer`.
Use distinct model ids in those roles' configs.

---

## 5. Costs

There is **no dedicated `cost` or `spend` command**. Costs are visible per run and
in the coverage report:

- **Per model run.** `generation run status --request-id <ID>` returns a safe run
  projection whose `generation` block carries `cost`, `dispatch_id`, and
  `role`/`prompt_version`/`outcome`. `extraction run status --request-id <ID>`
  returns a run projection with `cost_state`, `cost_amount_usd`, `reserve_usd`,
  `usage`, `dispatch_id`, and `dispatch_state`.
- **Coverage report.** `discovery report show --content` includes the deterministic
  monetary view: `direct_call` vs `opencode_session` routes with
  `known`/`estimated`/`unknown` amounts and the combined budget exposure. A genuinely
  unknown cost stays `unknown` with **no** amount (never `0`).
- **Budget blocker.** `pipeline status` reports `blocked_on.kind == "budget"` when a
  role's budget cap is already fully consumed.

**How per-role budget caps behave.** Each hosted-use document carries
`budget_cap_usd`, the maximum **cumulative reserve** the runner may hold for that
role under that authorization. The reserve is computed **before** each call as the
full configured context capacity at the prompt unit price plus `max_output_tokens`
at the completion unit price (an exact decimal, never a token-length estimate). The
runner sums this role's existing dispatches under the same hosted revision and
**refuses to dispatch** if adding the new reserve would exceed the cap — before any
POST. To continue, raise the cap in a new hosted-use revision and re-approve it, or
reconcile the outstanding reserve.

Project/stage/concurrency caps (USD 25 per project, USD 10 per stage, at most 2
concurrent external calls) count every route.

---

## 6. Troubleshooting

**Stale approval.** You approved, then changed the artifact or an upstream
dependency. `gate status` shows `stale`/`superseded` and `pipeline status` asks for
the gate again. Re-run `gate prepare` (new digest) and `gate confirm`. Never reuse
an old digest.

**`gate confirm` refused because it is not a TTY.** `gate confirm` opens `/dev/tty`
and never reads standard input, so it fails with a `storage` error when there is no
controlling terminal (a non-interactive script, a CI job, a cron job). Run it
yourself at an interactive terminal.

**Missing credential environment variable.** The one permitted credential name is
`OPENROUTER_API_KEY` (see `.env.example`). The value is never accepted on the
command line, never sent over IPC, and never persisted; the **worker** reads the
named variable from its own environment at dispatch. Configure the worker's
environment before dispatching. If the worker is already running without the key,
the CLI warns (`configuration_required`) but never restarts or reconfigures it — so
stop and restart the worker with the variable set.

**`invalid_proposal` / `model_not_completed` job results.** A generation job's
result may be `invalid_proposal` (the completed proposal failed the schema
validator) or `model_not_completed` (the runner outcome was not `completed` —
`refused`, `protocol_error`, `unknown`, `failed`, or `oversized_output`). Either
way **nothing** was written. Read it back with `generation run status
--request-id <ID>` (the exact `next_command` `pipeline run` reports for such a
failure), then fix the input/model/config and re-run.

**Worker not running.** `pipeline run` and `job run` ensure a worker is running
before submitting; queries and controls never start one implicitly. If a query
reports a missing/unverified worker (a stable non-success, exit `4`) or a foreign
endpoint (a `conflict`, exit `3`), start it explicitly:

```console
review-article-agent --data-dir "$DD" worker start
review-article-agent --data-dir "$DD" worker status
```

`worker start` reports readiness, never job completion.

**Large roster.** `gate prepare` can block on a roster whose dependency closure
exceeds 10000 entries. There is no hidden truncation.

---

## 7. Cheat sheet

```console
# Where am I / what is next?
review-article-agent --data-dir "$DD" pipeline status --project <PROJECT_ID>

# Approve the gate pipeline status named (copy the lines from blocked_on):
review-article-agent --data-dir "$DD" gate prepare  ...   # read-only; prints snapshot_digest
review-article-agent --data-dir "$DD" gate confirm  ... --snapshot-digest <DIGEST>   # on a real terminal

# Drive the next machine jobs:
review-article-agent --data-dir "$DD" pipeline run --config pipeline.json --project <PROJECT_ID>

# Inspect an artifact (all default to a safe summary; --content opts in):
discovery context show --artifact-id <ID> --content
discovery report show  --artifact-id <ID> --content
discovery freeze show  --artifact-id <ID> --content
acquisition manifest show --artifact-id <ID> --content
acquisition queue --freeze-artifact-id <ID> --freeze-revision-id <ID>
screening show --kind pilot|batch|audit --artifact-id <ID> --revision-id <ID> --content
screening exclusions --artifact-id <AUDIT_ID> --revision-id <AUDIT_RID>
extraction show --kind config|pilot|batch|audit --artifact-id <ID> --revision-id <ID> --content
synthesis show --kind synthesis|framework|outline --artifact-id <ID> --revision-id <ID> --content
drafting show --kind blueprint --artifact-id <ID> --revision-id <ID> --content
delivery status --delivery-id <ID>

# Gate identities, in pipeline order:
#  protocol          / discovery.plan
#  artifact_revision / discovery.freeze
#  artifact_revision / acquisition.complete
#  pilot             / screening.pilot
#  exclusion_set     / screening.exclusions
#  batch             / extraction.config
#  batch             / extraction.pilot
#  batch             / extraction.batch
#  batch             / extraction.outputs
#  artifact_revision / synthesis.memo
#  framework         / synthesis.framework
#  outline           / synthesis.outline
#  artifact_revision / drafting.blueprint
#  batch             / generation.hosted_use.<role>
#  artifact_revision / delivery.final_approval

# Worker:
review-article-agent --data-dir "$DD" worker start|status|stop
```
