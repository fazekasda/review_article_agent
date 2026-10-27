# Python HTTP integration spike (task C-python)

Minimal Python lifecycle prototype against the **official OpenCode V2 HTTP API**,
plus deterministic tests. Scope: this directory only.

Sources: `docs/implementation/00-runtime-spike.md` lines 5, 9, 15-18, 20 and
`docs/specification.md` "Product and runtime". API shapes come from the published
V2 OpenAPI document, `https://opencode.ai/v2/openapi.json`
(`openapi 3.1.0`, `info.title = "opencode HttpApi"`, `info.version = "0.0.1"`,
"Experimental HttpApi surface for selected instance routes"), fetched 2026-10-27.
No API field is invented.

## Files

| File | Purpose |
| --- | --- |
| `opencode_v2.py` | Stdlib-only client: session create, admission, per-turn reconciliation, bounded wait, interrupt, fork, fresh critic, typed decisions. |
| `mock_server.py` | Deterministic local mock of the used endpoints (incl. cursor paging and a redirect route). |
| `test_opencode_v2.py` | 41 unittest cases (success, retry, reconciliation, paging, error, invalid input, endpoint safety). |
| `demo_lifecycle.py` | Two-client durability demo against the mock. |

Python 3.14 (`python3`), standard library only. No third-party dependency.

## Run

```sh
cd spikes/runtime/python
python3 -m unittest -v test_opencode_v2   # 41 tests, ~1.5s
python3 demo_lifecycle.py                 # prints the two-client transcript
python3 mock_server.py --token secret     # optional: standalone mock on a free port
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
  and **requires** an explicit bearer token (`OPENCODE_SPIKE_TOKEN`); the client
  fails closed with `ValueError` if either is missing or empty. `from_env`
  refuses to guess a default and performs **no** service discovery or start.
* **Enforces loopback.** The host must be `localhost`, a `127.0.0.0/8` address,
  or `::1`; any other host, a non-HTTP(S) scheme, or an embedded credential is
  refused. The spike never talks to a remote or shared service.
* **Refuses redirects.** A private opener rejects every redirect, so the bearer
  token is never forwarded to a redirect target (`OpenCodeApiError` with the 3xx
  status and a "redirect blocked" message).
* Never reads OpenCode's private database, service registration, or credentials,
  and deliberately ignores `OPENROUTER_API_KEY` (a provider key is not an
  endpoint or a service token).

A live run against the ordinary shared service is **not** part of this spike: no
shared discovery/start, no paid calls, no budget authorization.

## Lifecycle semantics demonstrated

* **Durable admission vs completion.** `admit()` returns a `Session.Inbox.User`
  receipt: the input is persisted and scheduled. Completion is a separate
  terminal marker for the *same turn*, found by `reconcile()`.
* **Per-turn reconciliation (never a false completion).** The published message
  schemas expose no parent/linkage field, so a turn is delimited positionally:
  the target `user` message, then the next `user`/`synthetic` message, with the
  terminal `idle`/completed `assistant` in between. A terminal that belongs to an
  earlier or later turn never completes the target. States are `queued`
  (inbox-only), `in_progress` (delivered, no terminal yet), `completed`, `failed`,
  `interrupted`, and `unknown`; only `completed` is `is_complete`.
* **Honest unknown.** If the timeline cannot be read far enough to be certain
  (paging bound reached, or a turn window open past the read boundary), the state
  is `unknown` with a `reason` — never success.
* **Bounded paging.** `collect_messages()` follows `cursor.next` from the oldest
  message (default 100 pages) so older admissions are not lost behind the first
  page.
* **Idempotent admission.** The client-supplied admission ID (`Session.Message.ID`)
  is the idempotency key. A retry returns `409 ConflictError`; `admit()` treats
  that (or a transport failure) as "already admitted" and reconciles to the one
  accepted input instead of resending under a new identity.
* **Bounded wait.** `wait()` uses `experimental.session.wait`, retrying `503`
  and transport timeouts until a deadline, then raises `QuiescenceTimeout` — it
  never blocks forever.
* **Fresh critic.** `start_critic()` always creates a new session via
  `session.create`, never forks writer history, and fails when the critic model
  equals the writer model. `fork()` is provided separately and is documented as
  *not* a clean critic.
* **Typed decisions.** `generate_decisions()` sends predicate/choice/score
  questions and validates the typed answers, surfacing per-question `error`
  answers via `first_decision_error()` / `summarize_decisions()`.

## Mock vs live

| Aspect | Mock (`mock_server.py`) | Live |
| --- | --- | --- |
| Transport / auth | local HTTP, bearer checked | same shape, real token |
| Endpoint safety | loopback + token enforced client-side; `302 /api/redirect` refused | same client-side enforcement |
| Session create | returns a `Session.Info` | same |
| Prompt idempotency | `409 ConflictError` on repeat ID | per OpenAPI contract (unverified live) |
| Message paging | honours `order`/`limit`/`cursor` | per OpenAPI `cursor` contract (unverified live) |
| `wait` | `204` idle / `503` unavailable | per OpenAPI contract (unverified live) |
| Completion | synthetic `assistant`/`idle` messages | real agent turn |
| Decisions | fixed answers incl. an `error` answer | real `typesafe/jev-1.14` call (paid, not run) |

**Not verified against a live server here:** real version strings, real
`409`/`503` behaviour, real message projection, and paid decision calls. The
tests prove client behaviour against the documented contract, not server
conformance. A live smoke test requires an explicit loopback endpoint, an
explicit token, and (for decisions) explicit budget authorization.

## Acceptance criteria coverage (task C-python)

| Requirement | Evidence |
| --- | --- |
| CLI/session create with explicit role/model | `test_create_session_pins_role_and_model`; `create_session(agent=, model=)` |
| Explicit authenticated loopback endpoint, no discovery/start | `test_from_env_requires_explicit_base_url`, `test_unauthorized_error_tag`, `test_missing_token_unauthorized` |
| No private DB/credential parsing | `_headers`/`from_env` touch only explicit args/env; mock never reads files |
| Durable receipt ≠ completion | `test_submit_returns_durable_receipt_not_completion`, `test_reconcile_distinguishes_admission_from_completion` |
| Retry same admission ID → one input | `test_retry_same_admission_id_yields_single_input` |
| Retrieve persisted inbox/messages after missed events | `test_missed_events_second_client_retrieves_persisted_state`, `demo_lifecycle.py` steps 1-3 |
| Reconcile unknown/interrupted, bounded wait | `test_reconcile_recovers_admission_after_transport_drop`, `test_reconcile_unknown_never_reports_complete`, `test_bounded_wait_times_out` |
| Fresh critic (not fork); role/model pinning | `test_critic_session_is_fresh_not_fork`, `test_fork_carries_writer_history`, `test_critic_model_must_differ` |
| Typed decisions, per-question failure | `test_typed_answers_validate`, `test_per_question_failure_keeps_other_answers`, `test_malformed_answer_rejected` |
| Invalid inputs rejected locally | `test_invalid_admission_id_and_text_rejected_locally`, `test_invalid_model_ref_is_rejected_locally`, `test_malformed_answer_rejected` |

### Repair 1 — per-turn evidence and endpoint safety

| Requirement | Evidence |
| --- | --- |
| A queued/pending turn is not falsely completed by another turn's terminal | `test_second_queued_turn_not_falsely_complete`, `test_prior_idle_does_not_complete_later_turn`, `test_terminal_after_next_turn_does_not_complete_target` |
| Failure/interrupted states are distinct and never `is_complete` | `test_failed_outcome_is_distinct_from_success`, `test_interrupted_outcome_is_distinct_from_success`, `test_assistant_error_is_failed`, `test_unrecognized_idle_outcome_is_unknown` |
| Older admitted IDs are found by paging; a paging bound returns `unknown` | `test_paging_finds_older_admission`, `test_paging_bound_returns_unknown_not_complete`, `test_paging_limit_never_reports_success` |
| Missing/empty token fails closed | `test_missing_token_fails_closed`, `test_empty_token_fails_closed`, `test_from_env_requires_token` |
| Non-loopback / bad-scheme / credential-in-URL refused | `test_non_loopback_url_rejected`, `test_loopback_urls_accepted` |
| Redirects refused (no token forwarding) | `test_redirect_blocked_without_token_forwarding` |
| Provider key is not a substitute for endpoint/token | `test_from_env_ignores_openrouter_key` |

## Limitations

* Spike only: not production, no agent framework, no UI, no persistent store.
* Live API versions/behaviour and the Jev decision route are documented but not
  verified; the `typesafe/jev-1.14` decision call would be a paid call and is out
  of scope without budget authorization. `OPENROUTER_API_KEY` presence was checked
  as a boolean only and is deliberately not read or used (no paid calls).
* Per-turn correlation uses positional timeline windowing because the published
  schemas expose no parent/linkage field; it assumes the server emits the terminal
  `idle` within the turn's window. A turn whose terminal is missing is reported
  `in_progress`; an unreadable window is reported `unknown`, never success.
* The mock keeps a delivered admission's inbox item as a simplification (real
  delivery removes it); reconciliation keys off the projected user message, so
  this does not change client behaviour.
* Real server restart/recovery is not exercised; the mock models "accepted then
  connection lost" and "unknown ID" only.
