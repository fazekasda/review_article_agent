# 006 — Bounded automatic retry of ambiguous direct-call dispatches

## Status

**Accepted by the project owner — 2026-11-01.** The approving authority is the
**project owner**. The approving evidence is the owner's recorded **2026-11-01**
decision: *"Network errors or unknown errors are accepted. Add a retry mechanism.
~30% cost increase can be accepted for retries like this; no human approval
required."*

**What this approval is and is not.** It approves the **runtime/operational**
policy for retrying ambiguous direct-call dispatches and its exact accounting
bounds. It is **not** a scientific validation, **not** a claim that any
scientific feature is complete, and **not** authorization of a paid model call,
a hosted transfer, an upload of third-party papers, or approval of a real
project's pilot. It changes no product source beyond the retry policy, its
ledger primitives, tests, and the C02 amendment.

**Relationship to prior decisions.** It **extends** [decision 001](001-runtime-language.md)
(the direct-OpenRouter fallback route) and the direct-route records
[003](003-step05-discovery-and-direct-lead.md), [004](004-step09-direct-extraction.md),
and [005](005-generative-roles-direct-route.md), all of which dispatch on the
`direct_call` route with reserve-before-dispatch. It amends the C02 §8.1.1 "no
blind automatic retry" invariant and qualifies the §8.3 "never a new identity"
invariant for the `direct_call` route only.

## Context and constraints

- **Trigger (live pilot, 2026-11-01).** A direct Jev call ended
  `transport_error` → the dispatch became `unknown`, its cost `unknown`, its
  reserve held. By C02 §8.3 (v3.3) an ambiguous dispatch is never re-sent and
  only `definitive_rejection` evidence may turn `unknown` into `failed`
  ([`runtime/dispatch.py`](../../src/review_article_agent/runtime/dispatch.py)
  `_mark_failed_in_transaction`). There was no owner path to resolve it, and
  [`runtime_dispatch_remote_state`](../../src/review_article_agent/runtime/dispatch.py)
  treats `unknown` as occupying the project's **single serial remote slot**
  forever — every later job stayed `queued`. Any real network blip wedged the
  project.
- **Existing invariants (not weakened).** The immutable logical-call identity and
  idempotent reserve; reserve-before-dispatch; the shared per-role/stage/project
  caps (C02 §5.6); route/accounting-bucket separation (§6.3); and "never re-POST
  the **same** request identity" (§8.1.1/§8.3) all remain in force.
- **Direct route only.** The `opencode_session` route has a durable/idempotent
  admission and reconciles by identity; it is **unchanged** and never retried by
  this mechanism.

## Decision

An ambiguous (`unknown`) `direct_call` dispatch is retried automatically, without
human approval, under all of the following.

1. **Retryable set.** Only a `direct_call` dispatch in state `unknown` (transport
   error, timeout, ambiguous HTTP status, malformed/missing cost — anything the
   adapters already classify as ambiguous). Definitive outcomes (`completed`,
   `failed`) are never retried by this mechanism.
2. **New identity, never a re-POST.** A retry is a **new** dispatch under a
   **new** request id derived deterministically from the base: `{base}-r1`,
   `{base}-r2`. `RETRY_MAX_ATTEMPTS = 3` attempts per logical call. The **same**
   request identity is still never re-POSTed.
3. **Conservative supersession, durably recorded.** In one transaction the
   ambiguous predecessor becomes terminal `failed` with `cost` `estimated` = its
   full reserve (it may have been billed), via an explicit, named, validated
   primitive `supersede_unknown_for_retry(dispatch_id, successor_request_id)`.
   The primitive is refused for a non-`unknown` state, a non-`direct_call` route,
   or a successor id that is not the deterministic successor of the dispatch's
   request id. The successor link is derivable from the request ids
   (`X-r1` supersedes `X`), so **no storage migration** was needed: the durable
   transition itself is the record.
4. **Exhaustion releases the slot.** When all attempts are ambiguous, or the
   overhead cap refuses a further retry, the last ambiguous attempt is resolved
   to terminal `failed`/`estimated` = full reserve via
   `abandon_unknown_after_retries(dispatch_id)`, so the serial remote slot is
   released and the caller reports a definitive non-completion.
5. **Overhead cap.** Per accounting stage, a retry is allowed only if
   `superseded_total + retry_reserve <= max(RETRY_OVERHEAD_RATIO * base_total,
   retry_reserve)`, where `RETRY_OVERHEAD_RATIO = 0.30`, `superseded_total` is the
   summed estimated cost of dispatches already superseded/abandoned by this
   mechanism in the stage, and `base_total` is the stage's **other** (non-chain)
   dispatches at their known/estimated cost (or reserve while outstanding). The
   `max(..., retry_reserve)` floor lets the **first** retry in a fresh stage
   happen. All existing caps still apply unchanged to the new reserve; a cap
   refusal is a definitive pre-send refusal as today.
6. **Backoff.** 2 s then 8 s between attempts, injected as a `sleep` callable
   (tests pass a no-op).
7. **Crash recovery.** A `submitted` row recovered to `unknown` after a crash
   (`recover`) is retryable the same way on the next run.

The policy lives in [`runtime/retry.py`](../../src/review_article_agent/runtime/retry.py)
(`retry_request_ids`, `next_attempt`, `RetryDecision`, `backoff_seconds`) over the
ledger primitives in `runtime/dispatch.py`.

## Consequences

- **Positive.** A transient network failure no longer wedges a project: the
  ambiguous dispatch is bounded and resolved, the serial remote slot is freed,
  and the caller gets a definitive outcome. The retry cost is bounded at ~30% of
  a stage's other spend, per the owner's tolerance.
- **Cost.** A superseded predecessor is conservatively accounted at its full
  reserve as `estimated`, so the ledger may slightly over-count the true spend —
  deliberately, to never under-count a possibly-billed call.
- **No schema change.** No migration was required; `verify_all_integrity`,
  `runtime_dispatch_remote_state`, and `budget_usage` treat the resolved rows as
  ordinary terminal `failed` dispatches (not remote-occupying, cost counted).

## References

- Contract [`C02`](../contracts/02-gates-and-runtime.md) v3.4 §8.1.1/§8.3.
- [`runtime/dispatch.py`](../../src/review_article_agent/runtime/dispatch.py),
  [`runtime/retry.py`](../../src/review_article_agent/runtime/retry.py).
- [`docs/worker.md`](../worker.md) §12.4.
