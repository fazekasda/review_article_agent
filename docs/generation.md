# Generative roles on the direct OpenRouter route

Operator/developer reference for the one real model port that the generative
roles run through (kickoff 16 §5). It complements [decision 005](decisions/005-generative-roles-direct-route.md),
[decision 003](decisions/003-step05-discovery-and-direct-lead.md), and
[decision 004](decisions/004-step09-direct-extraction.md).

Everything here is stdlib-only and offline unless a call is explicitly dispatched.
**No test in this repository makes a live model call**, and a returned model
output is **non-authoritative data**: it is validated by the existing schema
validators and stored as an inert proposal, and the existing named human gates
remain required before anything is authoritative.

## What exists

- [`runtime/generation.py`](../src/review_article_agent/runtime/generation.py) —
  the pure single-shot adapter (`ROLES`, `plan_generation`, `execute_generation`).
  It builds a bounded plan and performs exactly one POST; it never authorizes,
  reserves, persists, or retries. It reuses the tested `runtime/lead.py`
  primitives (endpoint, `LeadConfig`, strict JSON parse, exact-decimal cost).
- [`generation/hosted_use.py`](../src/review_article_agent/generation/hosted_use.py) —
  the per-role `generation_hosted_use` authorization document and the named gate.
- [`generation/runner.py`](../src/review_article_agent/generation/runner.py) —
  the application boundary: `execute_generation_task` (gate, rights, budget,
  reserve, single POST, atomic commit) and `get_generation_outcome` (readback).
- Role modules: [`generation/synthesis_roles.py`](../src/review_article_agent/generation/synthesis_roles.py),
  [`generation/drafting_roles.py`](../src/review_article_agent/generation/drafting_roles.py),
  [`generation/review_roles.py`](../src/review_article_agent/generation/review_roles.py),
  [`generation/delivery_roles.py`](../src/review_article_agent/generation/delivery_roles.py)
  (`make_global_review_ports`, `plan_note_edits`).
- [`screening/runner.py`](../src/review_article_agent/screening/runner.py) —
  `execute_screening_case`, the application boundary that runs the **Jev** direct
  path (not the chat fallback) through the same hosted-use gate/rights/budget
  machinery and commits via the decision ledger.

### Windowed screening dispatch (S19-SCRWIN-RUN)

A real article (≈200 KB) is far larger than one Jev request
(`runtime/jev.SAFE_MAX_REQUEST_BYTES`, 32 KiB), so `execute_screening_case` now
splits the whole parsed article with the pure
`screening/workflow.case_windows(definitions, context)` and issues **one Jev
request per window** (request ids `"{request_id}-wNN"`, 1-based, window order).
Every window is planned, cost-bounded, and validated **before** the first
reserve; the total reserve of all not-yet-dispatched windows must fit the
per-role cap (a whole-case pre-reserve check) in addition to the existing
per-dispatch check. Each window then reserves its own runtime
task/dispatch/intent **before** its single POST and commits its accepted result
through the decision ledger, exactly like the former single dispatch. Windows are
processed sequentially; the first ambiguous/unknown window stops the run and
later windows are not sent. An already-recorded window is observed (never
re-POSTed), so a same-input re-run replays the completed windows and dispatches
only the missing ones. If the immediate pre-POST revalidation (gate/rights/token)
fails after the reserve, the reserved dispatch is resolved to a definitive
pre-send failure (state `failed`, cost `not_incurred`, reserve released) — never
left `submitted` with an outstanding reserve while no POST happened.

`execute_screening_case` returns a safe case-level mapping: `status` is
`completed` only when **every** window completed; it also carries `window_count`,
`windowing_version`, the ordered `windows` list
(`request_id`/`dispatch_id`/`status`/`cost`/`decision_result_ref`), an ordered
`decision_result_refs` list when all windows completed (else `None`), a single
`decision_result_ref` only for a one-window case (back-compat), and an aggregate
cost summary (`known` only when every window's cost is known). A pilot/batch case
binds these refs as `{"decision_result_refs": [ref, ...]}` (see the screening
workflow `_case`); the single `{"decision_result_ref": ...}` form remains valid
only for a one-window case. The worker `screening_case` operation returns
`status: "complete"` only when the runner status is `completed`, and its
`accepted_result` keeps `decision_result_refs` and `window_count` (safe IDs
only). Replay (`worker/generation._replay_screening_case`) recomputes the window
count from the current inputs via the same `case_windows` split and reads every
window's durable dispatch; a missing window is **not** complete (`None`), so
nothing is silently accepted. A pre-windowing job that recorded the bare
`request_id` for a one-window case is still read.

## Roles

The closed role set is `runtime.generation.ROLES`. The direct-call dispatch stage
for the generative roles is `generation` (`generation.runner.GENERATION_STAGE`).
"Wired" means a production port module exists on this branch and drives the role
through its runner — for the `generation` stage, `execute_generation_task`; for
`screening`, the Jev direct path in `screening/runner.py` (stage `screening`).

| Role | Stage | Prompt version constant | Output contract owner / validator | Consumed by | Wired? |
| --- | --- | --- | --- | --- | --- |
| `screening` | `screening` | `screening/runner.PROMPT_VERSION = "screening.jev.v1"` | Jev rubric (`runtime/jev.py`, deterministic) | Jev decision ledger ([`screening/workflow.py`](../src/review_article_agent/screening/workflow.py)) via [`screening/runner.py`](../src/review_article_agent/screening/runner.py) `execute_screening_case` | Yes |
| `synthesis` | `generation` | `PROMPT_VERSION_SYNTHESIS = "synthesis/1"` | `synthesis/schema.validate_synthesis` | `synthesis/workflow.SynthesisWorkflowRepository.propose`; gate `artifact_revision` / `synthesis.memo` | Yes |
| `framework` | `generation` | `PROMPT_VERSION_FRAMEWORK = "framework/1"` | `synthesis/schema.validate_framework` | `synthesis.workflow…propose`; gate `framework` / `synthesis.framework` | Yes |
| `outline` | `generation` | `PROMPT_VERSION_OUTLINE = "outline/1"` | `synthesis/schema.validate_outline` | `synthesis.workflow…propose`; gate `outline` / `synthesis.outline` | Yes |
| `blueprint` | `generation` | `BLUEPRINT_PROMPT_VERSION = "drafting.blueprint.v1"` | `drafting/schema.validate_blueprint` | `drafting/workflow…propose`; gate `artifact_revision` / `drafting.blueprint` | Yes |
| `section_writer` | `generation` | `SECTION_WRITER_PROMPT_VERSION = "drafting.section_writer.v1"` | `drafting/schema.validate_draft` | `drafting/workflow…propose` (only over an approved blueprint) | Yes |
| `section_critic` | `generation` | `SECTION_CRITIC_PROMPT_VERSION = "section_critic.generation.v1"` | `review/schema.normalize_finding` | `review/workflow.SectionReviewRepository.run` via `review_roles.make_review_ports` | Yes |
| `section_repair` | `generation` | `SECTION_REPAIR_PROMPT_VERSION = "section_repair.generation.v1"` | `review/schema.check_repair` | `review/workflow.SectionReviewRepository.run` via `make_review_ports` | Yes |
| `fact_checker` | `generation` | — | `review/schema.fact_check` (deterministic) | `review/workflow` | **No** — the deterministic `review/schema.fact_check` (line 492) remains the mechanism; `ROLES` declares a directive only |
| `global_reviewer` | `generation` | `GLOBAL_REVIEWER_PROMPT_VERSION = "global_reviewer.generation.v1"` | delivery findings (`delivery/schema.evaluate_global`) | `delivery/workflow.DeliveryRepository.run_global_review` via [`generation/delivery_roles.py`](../src/review_article_agent/generation/delivery_roles.py) `make_global_review_ports` | Yes |
| `global_writer` | `generation` | `GLOBAL_WRITER_PROMPT_VERSION = "global_writer.generation.v1"` | delivery schema (`delivery/schema.py`; targeted-paragraph, no new citation key) | `delivery/workflow.DeliveryRepository.run_global_review` via [`generation/delivery_roles.py`](../src/review_article_agent/generation/delivery_roles.py) `make_global_review_ports` | Yes |
| `notes_planner` | `generation` | `NOTES_PLANNER_PROMPT_VERSION = "notes_planner.generation.v1"` | delivery schema (`EDIT_KINDS`) | `delivery/workflow.DeliveryRepository.apply_notes` (only after a human `approval_id`) via [`generation/delivery_roles.py`](../src/review_article_agent/generation/delivery_roles.py) `plan_note_edits` (proposal only; human approves the batch) | Yes |

The prompt-version constants above are the ones actually defined in the role
modules.

`role` and `prompt_version` are pinned into the request digest and the synthetic
system message by the adapter, so a stored output is bound to the exact prompt
version that produced it.

### Key aliasing (framework)

The proposal envelope forbids the key names `role` and `scope` (they would let a
model re-pin its own task). The framework model-facing contract therefore emits
`concept_role` / `relationship_scope`, and
[`generation/synthesis_roles.py`](../src/review_article_agent/generation/synthesis_roles.py)
maps them back to the schema field names **before** validation. The critic
contract names the target sentence `sentence` (not `target`) for the same reason,
and `review_roles` restores `target` before `normalize_finding` runs.

## Creating and approving a hosted-use document per role

Authorization for a role is the per-role `generation_hosted_use` **document** plus
the out-of-band, owner-authenticated named gate
`generation.hosted_use.<role>` (gate kind `batch`).

### 1. Store the document (`generation hosted-use propose`)

The `generation hosted-use` CLI group proposes and shows a
`generation_hosted_use` document (see [`cli.md`](cli.md) §23):

```console
# The concrete model + pricing bound for the role (a strict JSON object).
$ cat config.json
{"model_id": "…", "upstream_provider": "…", "max_output_tokens": 1024,
 "context_tokens": 128000, "input_price_per_million": "0.5",
 "output_price_per_million": "1.5", "pricing_ref": "pricing:…",
 "instructions_revision": "…", "timeout_seconds": 60.0,
 "max_response_bytes": 1048576}

# Validate and store one inert revision (no approval, no call). The evidence
# refs are human-reviewed; verified_at defaults to now (RFC 3339 UTC).
$ review-article-agent --data-dir "$DD" generation hosted-use propose \
    --project <PROJECT_ID> --role section_writer \
    --config-file config.json --budget-cap-usd 10.00 \
    --provider-terms-ref "terms:…" --privacy-review-ref "privacy:…" \
    --pricing-evidence-ref "pricing:…" \
    --artifact-id art-gen-writer --revision-id rev-gen-writer-1 \
    --rights-file rights.json

# Print the stored document and whether its exact gate is satisfied.
$ review-article-agent --data-dir "$DD" generation hosted-use show \
    --project <PROJECT_ID> --artifact-id art-gen-writer
```

The `propose` output carries the new `revision`, the validated `document`, and a
non-authorizing `gate` block whose `prepare_command`/`confirm_command` are the exact
`gate prepare`/`gate confirm` invocations for the named gate on the new revision.

`validate_hosted_use_document` (pure, no I/O) checks the envelope and payload
exactly; `propose_hosted_use` validates first (nothing is written on a malformed
document) then stores one immutable artifact revision with **caller-supplied**
rights. Rights eligibility is not authority — the named gate below is. The Python
API (`propose_hosted_use`/`validate_hosted_use_document`) remains available for
programmatic use.

### 2. Approve the document (existing `gate` CLI)

The document is a *description* of an authorization; the authority is the named
gate recorded through the existing CLI:

```console
# Read-only preview: prints the exact snapshot digest. Records nothing.
$ review-article-agent --data-dir "$DD" gate prepare \
    --artifact-id art-gen-writer --revision-id rev-gen-writer-1 \
    --gate-kind batch --gate-id generation.hosted_use.section_writer \
    --decision approved --rationale "authorize the section-writer role" \
    --project <PROJECT_ID>

# Confirm out of band on the controlling terminal; echo the exact digest.
$ review-article-agent --data-dir "$DD" gate confirm \
    --artifact-id art-gen-writer --revision-id rev-gen-writer-1 \
    --gate-kind batch --gate-id generation.hosted_use.section_writer \
    --decision approved --rationale "authorize the section-writer role" \
    --snapshot-digest <SHA256 from gate prepare> \
    --project <PROJECT_ID>
```

`hosted_use_gate_id(role)` returns `generation.hosted_use.<role>`; the gate kind is
the shared `batch` capability. The runner re-reads the **exact current** document
revision, validates its bytes and canonical encoding, requires the document's
`role` to match the requested role, and requires this exact gate to be satisfied
for that revision — a superseded revision, changed bytes, a role mismatch, or a
missing gate fails closed.

### 3. Distinct-model rule

`role_models_distinct(store, refs)` enforces that `section_critic` uses a
different concrete `model_id` than `section_writer`, and `global_reviewer` a
different one than `global_writer`. Pass the role documents' exact current
revisions; an equal pair raises `GenerationRoleModelError`.

## Budgets

- Each document carries `budget_cap_usd`: the maximum **cumulative reserve** the
  runner may hold for that role under that authorization.
- `_check_budget` sums this role's existing `generation`-stage dispatches under the
  same hosted revision (a settled `known`/`estimated` cost, else the still-held
  reserve) and refuses to dispatch if adding the new reserve would exceed the cap
  — before any POST.
- The reserve is the **full configured context capacity** at the prompt unit price
  plus `max_output_tokens` at the completion unit price (an exact `Decimal`, never
  a token-length estimate). Cost is read from the direct-dispatch ledger, never
  copied from a response (C02 §6.3).

## Replay and readback

- The runner pins the role and the exact hosted-use revision in the durable task
  scope, so the per-role budget sum is computable and a replay with a different
  authorization conflicts rather than returning an unrelated run.
- An already-recorded `request_id` is **observed, never re-sent**
  (`get_dispatch_by_request` under the registry write guard).
- The critic/repair ports derive a **deterministic** request id
  `f"{review_id}.{role}.{sha256(canonical(material))[:16]}"`, so a resumed review
  replays the recorded dispatch (same material → same id → observed) instead of
  paying for a second POST.
- `get_generation_outcome(store, request_id)` reads back a durable logical request
  (role, prompt version, outcome, cost, proposal ref) without issuing a call. The
  mapping carries only safe IDs/status — never the API key or the material.

## Failure statuses

The role proposers return a status rather than raising for a model/schema outcome:

| Status | Meaning | Written? |
| --- | --- | --- |
| `model_not_completed` | the runner's outcome was not `completed` (e.g. `refused`, `protocol_error`, `unknown`, `failed`, `oversized_output`); the readback `outcome` is included | **Nothing** |
| `invalid_proposal` | the completed proposal failed the existing schema validator (a static `reason` code, e.g. `schema_invalid`, `unknown_reference`, `invalid_enum`, or `disallowed_content_key`) | **Nothing** |
| `proposed` | the validated proposal was stored as an **inert** revision; a `ref` and the generation readback are included | An inert revision (never approved) |

The critic/repair ports are different: the review workflow has no documented way to
record a failed model call, so a non-`completed` outcome raises a static
`ReviewPortError` and the run stops; the workflow has persisted its state after
every completed call, so the review resumes from the last persisted revision (a
human escalation point).

Underlying runner outcomes (from `runtime/generation.py`) are `completed`,
`refused`, `protocol_error`, `unknown`, `failed`, and the runner-level
`oversized_output` diagnostic.

### Worker dispatch classification

For a detached `generation_run` job, the worker classifies a dispatch that raises
*before any reserve/POST* as **`blocked`** (zero attempts counted as dispatched,
the same attempt reopened, and the job escalated to a human) — this covers
`GenerationRunnerError`, `GenerationHostedUseError`, `BudgetExceededError`, and the
screening runner's pre-dispatch refusals (`ScreeningRunnerError` and its
subclasses, e.g. `ScreeningBudgetError`). Any *other* exception is an unknown
outcome and is classified **`indeterminate`** (fail closed: the attempt is marked
ambiguous, the job escalates, and nothing is re-POSTed); the worker writes one
line to stderr naming the exception **class** only — never the message, which may
carry content.

## Testing with fake transports

No test makes a live call. Inject a fake transport into `execute_generation_task`
or `execute_generation`; the protocol is
`transport(body, api_key, *, endpoint, timeout, max_response_bytes) -> TransportResponse`
(the default is `runtime.lead.post_chat`).

```python
# Placeholder only — never executed by the docs or the tests.
from review_article_agent.runtime.lead import TransportResponse

class FakeTransport:
    def __init__(self, responses=()):
        self.responses = list(responses)
        self.calls = []
    def __call__(self, body, api_key, *, endpoint, timeout, max_response_bytes):
        self.calls.append({"body": body, "api_key": api_key})
        return self.responses.pop(0)

# A synthetic 200: one choice, finish_reason "stop", model == the configured one,
# assistant content a JSON object, usage with a finite cost.
response = TransportResponse(
    status=200,
    body_text=json.dumps({
        "id": "gen-1", "model": "<the configured model_id>", "provider": "…",
        "choices": [{"finish_reason": "stop",
                     "message": {"role": "assistant",
                                 "content": json.dumps({...proposal...})}}],
        "usage": {"prompt_tokens": 12, "completion_tokens": 8,
                  "total_tokens": 20, "cost": "0.0002"},
    }),
    truncated=False,
)
```

Tests are in `tests/test_runtime_generation.py`, `tests/test_generation_hosted_use.py`,
`tests/test_generation_runner.py`, `tests/test_generation_synthesis_roles.py`,
`tests/test_generation_drafting_roles.py`, `tests/test_generation_review_roles.py`,
`tests/test_generation_delivery_roles.py`, `tests/test_screening_runner.py`, and
`tests/test_cli_generation_hosted_use.py`.
The runner's `_FakeTransport` (queued responses; raises `RuntimeError` when empty)
is the canonical pattern.

## Limits to keep in mind

- The adapter is **not** an authorizer and performs **no** scientific or schema
  validation; the runner/role module owns gates, rights, budget, ledger, schema
  validation, and any human escalation.
- The OpenCode production route is **unchanged** and still fail-closed
  (`runtime/execution_boundary.py`); this route does not relax it.
- `fact_checker` remains the deterministic `review/schema.fact_check`; `ROLES`
  declares a directive for it but no production port replaces the deterministic
  checker.
- The delivery roles (`global_reviewer`/`global_writer`/`notes_planner`) and
  `screening` **are** wired now (`generation/delivery_roles.py`,
  `screening/runner.py`), and a `generation_hosted_use` document can be proposed
  and shown from the CLI (`generation hosted-use propose|show`, §1 above); the
  Python API (`propose_hosted_use`) remains available.
- There **is** now a worker job kind for the generation roles: the detached
  `generation_run` job (`discovery_submit` `mode=generation_run`) with the
  `generation run submit|status` CLI. It wires **every** documented operation: the
  single-shot `synthesis`/`framework`/`outline` (`synthesis_roles`) and
  `blueprint`/`draft` (`drafting_roles`); `section_review` (`review_roles` ports over
  `review/workflow.SectionReviewRepository.run`); the delivery `global_review` and
  `notes_plan` (`delivery_roles` over `delivery/workflow.DeliveryRepository`, reading
  the current manuscript/evidence from the recorded delivery state; `notes_plan`
  proposes an edit batch and never applies it); and `screening_case`
  (`screening/runner.execute_screening_case`, now one Jev request **per article
  window** — request ids `"{request_id}-wNN"` — with an ordered
  `decision_result_refs` result). No operation is refused. See
  [`worker.md`](worker.md) §15 and [`cli.md`](cli.md) §23.
- Live model validation of these roles is **unrun**; the user separately
  authorized one small live OpenRouter smoke test, which is not authorization to
  upload third-party papers or approve a real project.
