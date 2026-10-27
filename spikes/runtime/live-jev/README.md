# Direct OpenRouter Jev spike (task E-live-jev)

Task **E-live-jev**: implement the **documented direct-OpenRouter fallback** for
typed Jev predicate/choice/score decisions and per-question failure handling, with
an explicit accounting boundary, for implementation step 00
(`docs/implementation/00-runtime-spike.md` line 19) and
`docs/implementation/04-agent-runtime.md` (adapter + "account separately for
direct calls not covered by session statistics", line 22).

> **Not the OpenCode route.** This exercises OpenRouter's Decisions API directly.
> A passing direct call does **not** prove the native OpenCode
> `generate.decisions` route works; that route remains a separate, still
> unverified step-00 question. The step-00 criterion is claimed as **passed for
> the documented direct fallback only**, not for the native route.

## Primary sources (fetched 2026-10-27)

| Source | URL |
| --- | --- |
| Jev hub | https://openrouter.ai/docs/guides/community/jev |
| Jev tutorial (real request/response) | https://openrouter.ai/docs/guides/community/jev-tutorial |
| Decisions API reference (OpenAPI) | https://openrouter.ai/docs/api/api-reference/alphadecisions/submit-a-decisions-questions-and-answers-request |
| Model + pricing page | https://openrouter.ai/typesafe/jev-1.14 |

No API field is invented. Every shape below is from those pages.

## Files

| File | Purpose |
| --- | --- |
| `live_jev.py` | Stdlib-only reusable client: synthetic request builder, conservative cost preflight, strict response validator, single-shot live runner, CLI (dry-run default). |
| `test_live_jev.py` | 39 offline `unittest` cases (validation, error, accounting, dry-run/live gating, redaction). No network, no key. |
| `report/live-evidence.json` | Sanitized machine-readable result of the one authorized live call. |

Python 3.14, standard library only. No third-party dependency; nothing is
installed and no service is started.

## Run

```sh
cd spikes/runtime/live-jev
python3 -m unittest -v test_live_jev            # 39 tests, offline, ~0.006s
python3 live_jev.py                             # dry run (default): plans, sends nothing

# live (needs an explicit key file AND the authorization flag AND a ceiling):
python3 live_jev.py --live --i-authorize-live \
  --max-cost-usd 0.25 --max-calls 1 \
  --env-file /home/fazekasda/projects/review_article_agent-worktrees/runtime-spike/.env \
  --report-out report/live-evidence.json
```

`--live` alone is refused (exit 2). Live additionally requires a positive
`--max-cost-usd` and `--i-authorize-live`. Credentials come from `--env-file`
(explicitly authorized path) or the `OPENROUTER_API_KEY` environment variable;
**only** that one name is parsed and it is never printed, logged, echoed in a
header, or written to the repo (verified: no file in this worktree contains the
real key value).

## Confirmed route and schema

* **Endpoint** `POST https://openrouter.ai/api/alpha/decisions`, header
  `Authorization: Bearer $OPENROUTER_API_KEY`, `Content-Type: application/json`.
  (The operation overrides the OpenAPI default server with `https://openrouter.ai`.)
* **Model** `typesafe/jev-1.14`. The response reported the dated snapshot
  `typesafe/jev-1.14-20261014` from provider `TypeSafe`.
* **Request** `{ model, state, questions }` (all required). `questions` maps a
  name to a typed question. The **predicate** primitive is spelled `noul` on the
  wire: `{"type":"noul","instructions":…,"criteria":{"true":…,"false":…}}`.
  `choice` carries an option→description `criteria` map; `score` carries an
  ordered `criteria` list.
* **Response** `{ id?, model, provider?, answers, usage }`. `answers` maps a
  name to a typed answer:
  * `noul`  → `{"type":"noul","noul":0..1}`
  * `choice` → `{"type":"choice","choice":str,"confidence?":0..1,"probabilities?":{opt:0..1}}`
  * `score`  → `{"type":"score","score":float,"confidence?":0..1,"probabilities?":{idx:0..1},"legend?":{idx:label}}`
  * `usage` requires integer `input_tokens`/`output_tokens`; `cost` (USD) is documented as optional.
* **Predicate↔noul mapping.** The task phrase "predicate/choice/score" follows
  OpenCode's internal `Generate.DecisionAnswer`. This adapter normalizes the wire
  `noul` answer into an internal `{"type":"predicate","probability":…}` so the
  same code can consume both routes. `test_predicate_serializes_to_noul_and_others_unchanged`
  pins the serialization; `test_valid_typed_answers` pins the normalization.

## Accounting boundary (explicit, conservative)

* **Pricing** `$0.042` / 1M **input** tokens, output tokens free (model page),
  context 262,144. Cost bound = `request_bytes × 0.042 / 1e6`, assuming one input
  token per byte (a deliberate over-estimate; real tokenisation is looser).
  A 32 KiB default-mode size cap bounds any accepted request to **≤ $0.00138**.
* **Preflight** computes the bound *before* the call and refuses (no call) when it
  cannot be shown `≤ --max-cost-usd`, when the request is oversize, or when the
  ceiling is missing. A second, server-side cap is sent as
  `provider.max_price.request = <ceiling>` (documented `ProviderPreferences`), so
  OpenRouter rejects rather than overspends even if the client bound were wrong.
* **Calls** at most `MAX_LIVE_CALLS` (2) per invocation, default 1, **never
  retried**. A transport failure or an ambiguous 5xx/429/524/529 leaves the cost
  `unknown` and **retains the reserve** (fail closed); only a definitive 4xx is
  recorded `not_incurred`. `usage.cost` is used when the provider reports it;
  otherwise the cost is `unknown` and the reserve is retained.
* **Direct calls are not in OpenCode session statistics** and are accounted here
  separately.

### The one authorized live call

Max allocated for this task: **$0.25**. One call was made (no retry).

| Field | Value |
| --- | --- |
| status | `completed` (HTTP 200), calls attempted 1 |
| request | 921 bytes, sha256 `fad8bbfe19fb9e4469a935bcec046b5480e4d7d097273aad796ff72704b545fa` |
| cost upper bound (preflight) | `$0.000038682` (≤ $0.25) |
| provider-reported `usage.cost` | **`$0.00002037`** |
| usage | input 485 / output 76 tokens |
| response model / provider | `typesafe/jev-1.14-20261014` / `TypeSafe` |
| reserve released | yes (provider reported a cost well under the ceiling) |

Real typed answers (synthetic state, tiny passage — no papers, no scientific claim):

* predicate (`is_original_report`, wire `noul`) → probability `0.83`
* choice (`study_design`) → `randomised`, confidence `0.36`,
  probabilities `{randomised:0.57, observational:0.0, other:0.43}`
* score (`confidence_level`) → `1.09`, confidence `0.0`,
  probabilities `{0:0.43, 1:0.05, 2:0.52}`,
  legend `{0:"low",1:"moderate",2:"high"}`

All three primitives returned valid typed outputs; the validator accepted them
and `per_question_errors` was empty.

## Per-question failure handling

Per `04-agent-runtime.md` line 21, model/provider errors, refusals, invalid
shapes, and partial errors must **not** be interpreted as scientific exclusions.

* **Documented-API gap.** The published Decisions API `answers` union is
  `noul|choice|score|box` with **no per-question `error` variant**, so a
  per-question failure **cannot be induced safely** against the live API. The
  live run therefore shows zero per-question errors.
* **Mock-only paths (distinctly marked).** The validator treats a *missing*
  answer for a declared question as a per-question failure
  (`{"type":"error","reason":"missing_answer"}`) and preserves the other answers;
  it also preserves a defensive `type:"error"` answer (matching OpenCode's
  internal decision schema, not the published Decisions API). It never emits an
  exclusion. These two paths are exercised offline only:
  `test_missing_answer_is_per_question_error_not_failure`,
  `test_error_type_answer_preserved_not_exclusion`.
* Envelope/shape problems (not a JSON object; missing/empty `answers`; missing or
  malformed `usage`; undeclared question names; a typed answer that does not
  match its question, an out-of-range probability, a choice outside the declared
  options, a score outside the declared levels, a `bool`/non-finite number) raise
  `LiveJevProtocolError` — a loud protocol failure, not a silent exclusion.

## Acceptance criteria → evidence (task E-live-jev)

| Requirement | Evidence |
| --- | --- |
| Official docs researched before calling | Sources table above; shapes encoded in `live_jev.py` |
| Model exactly `typesafe/jev-1.14` via OpenRouter | `MODEL` constant; `test_predicate_serializes…`; live `request.model` |
| Native typed decision route/schema confirmed | Decisions endpoint + request/response schema in `live_jev.py`; `report/live-evidence.json` |
| Conservative preflight cost ≤ ceiling; no call if unbounded | `preflight_cost` + `test_missing_ceiling_is_refused`, `test_over_ceiling_is_refused`, `test_oversize_request_is_refused`; live bound `$0.0000387` |
| Max 2 calls, no automatic retries, fail closed | `MAX_LIVE_CALLS`; `test_max_calls_capped`, `test_transport_error_no_retry`, `test_ambiguous_500_retains_reserve` |
| Real predicate/choice/score outputs | live report: three valid typed answers |
| Per-question failures | mock-only missing/error paths; documented gap (above) |
| Provider cost tracked; unknown explicit + reserve retained | live `cost_usd=$0.00002037`; `test_completed_without_cost_retains_reserve`, `test_missing_cost_is_explicit_unknown` |
| Reusable safe script, dry-run default, explicit live flag + ceiling | CLI + `test_dry_run_exit_zero`, `test_live_without_authorization_exit_two`, `test_live_without_authorization_is_refused` |
| Offline regression tests | 39 `unittest` cases, all pass |
| Credential handling | `load_api_key` (only `OPENROUTER_API_KEY`); `test_env_file_parses_only_the_key`, `test_env_file_missing_key_fails_closed`, `test_request_body_never_contains_the_key`; worktree leak scan found none |

## Limitations

* Spike only: no agent framework, no UI, no persistent store, no service lifecycle.
* This is the **direct fallback**; the native OpenCode `generate.decisions` route
  and the app-owned-service path are untouched and unverified here.
* Per-question `error` answers cannot be produced by the live documented API; that
  path is mock-only and labelled as such.
* The client bound uses a 1-byte-per-token over-estimate; the provider's own
  `usage.cost` is authoritative and is what the live run reports.
* Only one live call was made; no retry, no repeated sampling of Jev's output.
