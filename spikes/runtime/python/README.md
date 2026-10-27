# Python HTTP integration spike (task C-python)

Minimal Python lifecycle prototype against the **official OpenCode V2 HTTP API**,
plus deterministic tests. Scope: this directory only.

Sources: `docs/implementation/00-runtime-spike.md` lines 5, 9, 15-20 (line 19 is
the typed Jev predicate/choice/score + per-question-failure criterion) and
`docs/specification.md` "Product and runtime". API shapes come from the published
V2 OpenAPI document, `https://opencode.ai/v2/openapi.json`
(`openapi 3.1.0`, `info.title = "opencode HttpApi"`, `info.version = "0.0.1"`,
"Experimental HttpApi surface for selected instance routes"), fetched 2026-10-27.
No API field is invented.

## Files

| File | Purpose |
| --- | --- |
| `opencode_v2.py` | Stdlib-only client: session create, admission, per-turn reconciliation, bounded wait, interrupt, fork, fresh critic, typed decisions. |
| `mock_server.py` | Deterministic local mock of the used endpoints (incl. cursor paging, a redirect route, and Bearer/Basic auth). |
| `test_opencode_v2.py` | 79 unittest cases (success, retry, reconciliation, paging query conformance, auth schemes, typed-decision boundary, error, invalid input, endpoint safety, restart-repair regression). |
| `demo_lifecycle.py` | Two-client durability demo against the mock. |

Python 3.14 (`python3`), standard library only. No third-party dependency.

## Run

```sh
cd spikes/runtime/python
python3 -m unittest -v test_opencode_v2   # 79 tests, ~2.5s
python3 demo_lifecycle.py                 # prints the two-client transcript
python3 mock_server.py --token secret     # optional: standalone mock, Bearer auth
python3 mock_server.py --basic-password pw # optional: standalone mock, Basic auth
```

## API shape used (from the published OpenAPI)

All responses with a body are `{"data": ...}` unless noted.

| operationId | Method + path | Request | Response |
| --- | --- | --- | --- |
| `server.info` | `GET /api/info` | – | `ServerInfo` (`version`, `pid`, `urls`, `paths`) |
| `session.create` | `POST /api/session` | `{id?, parentID?, title?, agent?, model?, location?, metadata?, permissions?}` | `{data: Session.Info}` |
| `session.get` | `GET /api/session/{sessionID}` | – | `{data: Session.Info}` |
| `session.prompt` | `POST /api/session/{sessionID}/prompt` | `{id?, text, files?, agents?, skills?, metadata?, delivery?, resume?}` | `{data: Session.Inbox.User}` |
| `session.inbox.list` | `GET /api/session/{sessionID}/inbox` | – | `{data: [Session.Inbox.Info]}` |
| `session.message.list` | `GET /api/session/{sessionID}/message` | `type?, limit?, order?, cursor?` | `SessionMessagesResponse` (`data`, `cursor`) |
| `session.message.get` | `GET /api/session/{sessionID}/message/{messageID}` | – | `{data: Session.Message.Info}` |
| `session.interrupt` | `POST /api/session/{sessionID}/interrupt` | `resume?` query | `SessionInterruptResponse` (`{interrupted}`) |
| `session.fork` | `POST /api/session/{sessionID}/fork` | `{before?}` | `{data: Session.Info}` |
| `experimental.session.wait` | `POST /api/experimental/session/{sessionID}/wait` | – | `204` when idle, `503` when unavailable |
| `experimental.generate.decisions` | `POST /api/experimental/generate/decisions` | `{model, input, questions}` | `{data: Generate.DecisionResult}` |

Key identities:

* `Session.ID` pattern `^ses`; `Session.Message.ID` pattern `^msg_`.
* `Model.Ref` = `{id, providerID, variant?}` (`id` and `providerID` required).
* Role pinning = `agent` (an `Agent.ID`) on session create; model pinning =
  `model` (`Model.Ref`).
* `Session.Inbox.User` = `{id, sessionID, time:{created}, type:"user", payload:{text,...}, delivery}`.
* `Session.Inbox.Delivery` = `"steer" | "queue"`.
* Errors are the `*ErrorEncoded` objects with `_tag` (e.g. `ConflictError`,
  `UnauthorizedError`, `ServiceUnavailableError`, `SessionNotFoundError`).
* Decisions: `Generate.DecisionAnswer` is `predicate` (`probability`),
  `choice` (`choice`, `probabilities`, `confidence`), `score` (`score`,
  `probabilities`, `confidence`), or `error` (`reason`, `message?`). The
  `error` answer is a per-question failure while other answers still return; it
  is **not** a scientific exclusion.

## Authentication and endpoint (explicit loopback only)

The published OpenAPI declares no `securitySchemes` and `security: []` on the
routes, so authentication is not part of the schema. The V2 client guide shows an
explicit endpoint plus `authorization: Bearer <token>` headers
(`Service.headers(endpoint)` in the Node service API). This prototype therefore:

* **Requires** an explicit `base_url` (constructor or `OPENCODE_SPIKE_BASE_URL`)
  and **requires** an explicit credential; the client fails closed with
  `ValueError` if the endpoint or the selected scheme's credential is missing or
  empty. `from_env` refuses to guess a default and performs **no** service
  discovery or start.
* **Authorization scheme is explicit, never guessed.** `auth="bearer"` (the
  default, for backward compatibility) sends `Authorization: Bearer <token>`;
  `auth="basic"` sends HTTP Basic `<username>:<password>` with the username
  defaulting to `opencode`. The disposable **2.2.0 harness was observed to answer
  HTTP Basic `opencode:<per-run-password>`** — its explicit app-owned descriptor
  carries the service URL and a per-run password file — so a Bearer-only client
  cannot connect to it. Supply the password via `password=` or an explicitly
  named `password_file=`, and the scheme via `auth=`. Supplying the wrong scheme's
  fields (a token to `basic`, or a password to `bearer`) is refused, so a
  misconfiguration cannot silently authenticate a different way. There is **no**
  arbitrary-header escape hatch.
  * Environment: `OPENCODE_SPIKE_AUTH` (`bearer`|`basic`), then either
    `OPENCODE_SPIKE_TOKEN` (bearer) or `OPENCODE_SPIKE_PASSWORD` /
    `OPENCODE_SPIKE_PASSWORD_FILE` (basic, with optional
    `OPENCODE_SPIKE_USERNAME`).
* **Enforces loopback.** The host must be `localhost`, a `127.0.0.0/8` address,
  or `::1`; any other host, a non-HTTP(S) scheme, or an embedded credential is
  refused. The spike never talks to a remote or shared service.
* **Refuses redirects.** A private opener rejects every redirect, so the
  credential (Bearer or Basic) is never forwarded to a redirect target
  (`OpenCodeApiError` with the 3xx status and a "redirect blocked" message).
* Never reads OpenCode's private database, service registration, or credentials,
  and deliberately ignores `OPENROUTER_API_KEY` (a provider key is not an
  endpoint or a service token). A `password_file` is read only from the explicit
  app-owned path the caller names; nothing is discovered.

A live run against the ordinary shared service is **not** part of this spike: no
shared discovery/start, no paid calls, no budget authorization.

## Lifecycle semantics demonstrated

* **Durable admission vs completion.** `admit()` returns a `Session.Inbox.User`
  receipt: the input is persisted and scheduled. Completion is a separate,
  correlated terminal for the *same turn*, found by `reconcile()`.
* **Only an authoritative `idle` completes a turn.** A turn ends only at its
  correlated `Session.Message.Idle`. A completed `assistant` message is an
  intermediate agent-loop step and **never** task success on its own — not with
  `finish=tool-calls` (more tools/loop steps follow), and not even with
  `finish=stop` (more steps may still run); `finish=length`/`content-filter`/
  `error` are truncated or failed steps, not completion. `idle` with
  `outcome=succeeded` → `completed`; `outcome=failed`/`interrupted` stay explicit
  and are never `is_complete`.
* **Per-turn reconciliation (never a false completion).** The published message
  schemas expose no parent/linkage field, so a turn is delimited positionally:
  the target `user` message, then the next `user`/`synthetic` message, with the
  authoritative `idle` in between. An `idle` that belongs to an earlier or later
  turn never completes the target. States are `queued` (inbox-only),
  `in_progress` (delivered, no correlated `idle` yet and nothing shows the turn
  died), `completed`, `failed`, `interrupted`, and `unknown`; only `completed` is
  `is_complete`.
* **Errored assistant ⇒ unknown (restart repair).** An assistant that ended in
  error (`finish="error"`, or a structured `error` field) with **no** authoritative
  `idle` is `unknown`, not `in_progress`: a live service persists exactly that
  (user input + partial `assistant finish="error"`, no terminal) when a turn is torn
  down, e.g. by a restart, and never writes an `idle`. It is never `completed` and
  never a confident `failed`/`interrupted` without an authoritative terminal.
* **Honest unknown.** If the timeline cannot be read far enough to be certain
  (paging bound reached, or a turn window open past the read boundary), the state
  is `unknown` with a `reason` — never success.
* **Bounded paging.** `collect_messages()` follows `cursor.next` from the oldest
  message (default 100 pages) so older admissions are not lost behind the first
  page. Query conformance: the published `session.message.list` applies `order`
  to the **first page** only and forbids combining it with `cursor`, and requires
  the same `type` filter on every page — so only the first page sends `order`,
  and later pages send the repeated `type` (when set) plus the opaque `cursor`.
* **Idempotent admission.** The client-supplied admission ID (`Session.Message.ID`)
  is the idempotency key. Exactly one input is accepted per ID. The mock returns
  `409 ConflictError` on a repeat ID; the `2.2.0` harness observed the same ID
  retried against a real service returning `200` (still one accepted input), so
  `admit()` treats `409` (or a transport failure) as "already admitted" and
  reconciles to the one persisted input, and a `200` repeat simply returns that
  durable receipt. Neither is mistaken for completion or resent under a new
  identity.
* **Bounded wait.** `wait()` uses `experimental.session.wait`, retrying `503`
  and transport timeouts until a deadline, then raises `QuiescenceTimeout` — it
  never blocks forever.
* **Fresh critic.** `start_critic()` always creates a new session via
  `session.create`, never forks writer history, and fails when the critic model
  equals the writer model. `fork()` is provided separately and is documented as
  *not* a clean critic.
* **Typed decisions (strict boundary).** `generate_decisions()` sends
  predicate/choice/score questions and, **at that boundary**, validates the
  response against the published answer schemas and the exact questions sent:
  required fields, no unknown fields, finite numeric bounds that exclude
  booleans, typed choice values, and one answer per question in question order
  with matching names/types and choice/score distributions over the declared
  choices/levels. A malformed or missing response is a `DecisionProtocolError`
  (a protocol failure); a valid per-question `error` answer is preserved (and is
  *not* a scientific exclusion), surfaced via `first_decision_error()` /
  `summarize_decisions()`. This is contract-level validation of client
  behaviour, not the Jev route verified live.

## Mock vs live

| Aspect | Mock (`mock_server.py`) | Live |
| --- | --- | --- |
| Transport / auth | local HTTP, Bearer or Basic checked | same shape, real credential |
| Endpoint safety | loopback + token enforced client-side; `302 /api/redirect` refused | same client-side enforcement |
| Session create | returns a `Session.Info` | same |
| Prompt idempotency | `409 ConflictError` on repeat ID | observed `200` on a same-ID retry (`2.2.0` harness), one input |
| Message paging | honours `order`/`limit`/`cursor` | per OpenAPI `cursor` contract (unverified live) |
| `wait` | `204` idle / `503` unavailable | per OpenAPI contract (unverified live) |
| Completion | synthetic `assistant`/`idle` messages | real agent turn |
| Decisions | fixed answers incl. an `error` answer | real `typesafe/jev-1.14` call (paid, not run) |

**Not verified against a live server here:** real version strings, real
`409`/`503` behaviour, real message projection, and paid decision calls. The
tests prove client behaviour against the documented contract, not server
conformance. The only live observation recorded so far is from the `2.2.0`
harness: retrying the same prompt ID returned `200` (one accepted input), not the
mock's `409`. A live smoke test requires an explicit loopback endpoint, an
explicit token, and (for decisions) explicit budget authorization.

## Acceptance criteria coverage (task C-python)

| Requirement | Evidence |
| --- | --- |
| CLI/session create with explicit role/model | `test_create_session_pins_role_and_model`; `create_session(agent=, model=)` |
| Explicit authenticated loopback endpoint, no discovery/start | `test_from_env_requires_explicit_base_url`, `test_unauthorized_error_tag`, `test_missing_token_fails_closed` |
| No private DB/credential parsing | `_headers`/`from_env` touch only explicit args/env; mock never reads files |
| Durable receipt ≠ completion | `test_submit_returns_durable_receipt_not_completion`, `test_reconcile_distinguishes_admission_from_completion` |
| Retry same admission ID → one input | `test_retry_same_admission_id_yields_single_input` |
| Retrieve persisted inbox/messages after missed events | `test_missed_events_second_client_retrieves_persisted_state`, `demo_lifecycle.py` steps 1-3 |
| Reconcile unknown/interrupted, bounded wait | `test_reconcile_recovers_admission_after_transport_drop`, `test_reconcile_unknown_never_reports_complete`, `test_bounded_wait_times_out` |
| Fresh critic (not fork); role/model pinning | `test_critic_session_is_fresh_not_fork`, `test_fork_carries_writer_history`, `test_critic_model_must_differ` |
| Typed decisions, per-question failure | `test_typed_answers_validate`, `test_per_question_failure_keeps_other_answers`, `test_generate_decisions_validates_at_the_boundary` |
| Invalid inputs rejected locally | `test_invalid_admission_id_and_text_rejected_locally`, `test_invalid_model_ref_is_rejected_locally`, `test_question_shapes_validated_locally` |

### Repair 1 — per-turn evidence and endpoint safety

| Requirement | Evidence |
| --- | --- |
| A queued/pending turn is not falsely completed by another turn's terminal | `test_second_queued_turn_not_falsely_complete`, `test_prior_idle_does_not_complete_later_turn`, `test_terminal_after_next_turn_does_not_complete_target` |
| Failure/interrupted states are distinct and never `is_complete` | `test_failed_outcome_is_distinct_from_success`, `test_interrupted_outcome_is_distinct_from_success`, `test_assistant_error_without_idle_is_unknown_not_terminal`, `test_unrecognized_idle_outcome_is_unknown` |
| Older admitted IDs are found by paging; a paging bound returns `unknown` | `test_paging_finds_older_admission`, `test_paging_bound_returns_unknown_not_complete`, `test_paging_limit_never_reports_success` |
| Missing/empty token fails closed | `test_missing_token_fails_closed`, `test_empty_token_fails_closed`, `test_from_env_requires_token` |
| Non-loopback / bad-scheme / credential-in-URL refused | `test_non_loopback_url_rejected`, `test_loopback_urls_accepted` |
| Redirects refused (no token forwarding) | `test_redirect_blocked_without_token_forwarding` |
| Provider key is not a substitute for endpoint/token | `test_from_env_ignores_openrouter_key` |

### Repair 2 — a completed assistant is not a completion

| Requirement | Evidence |
| --- | --- |
| Completed `assistant` with no correlated `idle` never means success | `test_tool_calls_assistant_without_idle_is_not_complete`, `test_completed_assistant_finish_stop_without_idle_is_not_complete`, `test_idle_succeeded_is_the_only_completion` |
| `finish=tool-calls` loop step remains in_progress/unknown | `test_tool_calls_assistant_without_idle_is_not_complete` |
| `finish=length` / `content-filter` / `error` without idle is not completion | `test_assistant_finish_length_without_idle_is_not_complete`, `test_assistant_finish_content_filter_without_idle_is_not_complete`, `test_assistant_error_without_idle_is_unknown_not_terminal` |

### Repair 4 — errored assistant without an idle is `unknown` (restart)

| Requirement | Evidence |
| --- | --- |
| An assistant that ended in error with no authoritative idle is `unknown`, not `in_progress` | `test_assistant_error_without_idle_is_unknown_not_terminal`, `test_restart_errored_assistant_without_idle_is_unknown` |
| The observed real restart shape (user + `assistant finish="error"`, no idle) reconciles to `unknown`, never success | `test_restart_errored_assistant_without_idle_is_unknown` (and the integration run, `spikes/runtime/integration/`) |
| Ordinary live in-flight turns stay `in_progress` | `test_tool_calls_assistant_without_idle_is_not_complete`, `test_completed_assistant_finish_stop_without_idle_is_not_complete`, `test_assistant_finish_length_without_idle_is_not_complete`, `test_assistant_finish_content_filter_without_idle_is_not_complete` |
| Only `idle succeeded` yields `completed`; `failed`/`interrupted` stay explicit | `test_idle_succeeded_is_the_only_completion`, `test_failed_outcome_is_distinct_from_success`, `test_interrupted_outcome_is_distinct_from_success` |

### Repair 3 — strict typed Jev decision boundary

| Requirement | Evidence |
| --- | --- |
| Answers are validated automatically at the `generate_decisions` boundary | `test_generate_decisions_validates_at_the_boundary` |
| Malformed/missing response is a distinct protocol failure, not a refusal | `test_missing_or_non_dict_response_is_protocol_failure`, `DecisionProtocolError` |
| Finite numeric bounds, excluding booleans/NaN/Infinity | `test_probability_rejects_bool_nan_and_infinity`, `test_confidence_and_probabilities_reject_non_finite` |
| Published schemas: required fields, no unknown fields | `test_unknown_and_extra_fields_rejected`, `_ANSWER_KEYS`/`_REJECT_EXTRA_FIELDS` |
| Cardinality/names/order/types match the original questions | `test_answer_cardinality_missing_duplicate_and_unknown_names`, `test_answer_type_mismatch_rejected` |
| Choice distribution/selected value over declared choices (typed) | `test_invalid_choice_selected_value_rejected`, `test_choice_distribution_must_cover_declared_choices`, `test_typed_choice_values_distinguish_true_from_string` |
| Score distribution over declared levels | `test_invalid_score_rejected` |
| Per-question `error` preserved (not an exclusion, not a protocol failure) | `test_per_question_failure_keeps_other_answers` |
| An HTTP error stays an API error, not a protocol failure | `test_decision_http_error_is_api_error_not_protocol_failure` |

### Task F-compat — Basic transport auth and paging query conformance

| Requirement | Evidence |
| --- | --- |
| Explicit Basic vs Bearer auth; default Bearer kept | `OpenCodeV2Client(auth=...)`; `test_basic_auth_reaches_the_server`, `test_bearer_remains_the_default_and_works` |
| Basic transport auth against the harness's `opencode:<password>` | `test_basic_auth_reaches_the_server` (asserts `Basic base64(opencode:...)`), `test_basic_custom_username` |
| Fails closed on missing/ambiguous credentials | `test_basic_missing_password_fails_closed`, `test_basic_and_bearer_credentials_are_mutually_exclusive`, `test_unknown_auth_scheme_rejected`, `test_basic_password_file_missing_fails_closed`, `test_basic_password_and_password_file_conflict` |
| Explicit app-owned password file (no discovery/private parsing) | `test_basic_password_file_is_read_explicitly`; `OpenCodeV2Client(password_file=)` reads only the named path |
| env/CLI documented | `from_env` + `test_from_env_basic_auth`, `test_from_env_basic_auth_requires_password`, `test_from_env_unknown_auth_scheme_rejected` |
| Credential never forwarded on a redirect (Basic included) | `test_redirect_is_refused_for_basic_auth`, `test_redirect_blocked_without_token_forwarding` |
| `order` only on the first page; never with `cursor` | `test_collect_messages_sends_order_only_on_the_first_page`, `test_mock_rejects_cursor_with_order` |
| `type` filter repeated on later pages | `test_collect_messages_repeats_type_filter_on_later_pages` |
| Paging regression (reconciliation still completes over pages) | `test_reconcile_still_pages_after_the_query_fix`, `test_paging_finds_older_admission` |

## Limitations

* Spike only: not production, no agent framework, no UI, no persistent store.
* Live API versions/behaviour and the Jev decision route are documented but not
  verified; the `typesafe/jev-1.14` decision call would be a paid call and is out
  of scope without budget authorization. The typed-decision validation in this
  spike is against the published answer schemas and the sent questions, so it
  proves the client boundary, not the live Jev route's real output. The published
  `Generate.ScoreAnswer` probability entry gives an integer `value` with no stated
  base, so score validation binds on the declared level `label`s and distinct
  integer values rather than inventing an index convention. `OPENROUTER_API_KEY`
  presence was checked as a boolean only (absent in this checkout) and is
  deliberately not read or used (no paid calls).
* Per-turn correlation uses positional timeline windowing because the published
  schemas expose no parent/linkage field; it assumes the server emits the terminal
  `idle` within the turn's window. A turn whose terminal is missing and shows no
  errored assistant is reported `in_progress`; an errored assistant without an
  `idle`, or an unreadable window, is reported `unknown`, never success.
* The mock keeps a delivered admission's inbox item as a simplification (real
  delivery removes it); reconciliation keys off the projected user message, so
  this does not change client behaviour.
* Real server restart/recovery of an in-flight turn is exercised by the combined
  integration run (`spikes/runtime/integration/`), not by this mock: the mock
  models "accepted then connection lost" and "unknown ID" only, and does not model
  the persisted errored-assistant shape (covered by
  `test_restart_errored_assistant_without_idle_is_unknown`).
