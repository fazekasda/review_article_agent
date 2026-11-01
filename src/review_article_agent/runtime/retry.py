"""The bounded **direct-call retry policy** (ADR 006; owner decision 2026-11-01).

A live pilot ended in ``transport_error``: the direct Jev call became an
ambiguous ``unknown`` dispatch, held its reserve, and — because the project's
single serial remote slot treats ``unknown`` as occupying — wedged every later
job. The owner decided that "network errors or unknown errors are accepted" and
directed an automatic retry bounded by a small cost overhead, with no human
approval.

This module is the small, explicit API the four direct-call runners (screening,
generation, lead, extraction) call. It is **pure policy plus the ledger
transitions**: it performs no network I/O, mints no authority, and never re-sends
the same request identity. The route it governs is ``direct_call`` **only**; the
``opencode_session`` route is never retried here (it reconciles by identity).

Policy (C02 §8.1.1/§8.3, v3.4)
-------------------------------

* **Retryable:** a ``direct_call`` dispatch in state ``unknown`` (a transport
  error, timeout, ambiguous HTTP status, malformed/missing cost — anything the
  adapters already classify as ambiguous). Definitive outcomes (``completed``,
  ``failed``) are never retried by this mechanism.
* **New identity, never a re-POST.** A retry is a **new** dispatch under a
  **new** request id derived deterministically: ``f"{base}-r{k}"`` for
  ``k = 1, 2`` (:func:`retry_request_ids`). At most
  :data:`RETRY_MAX_ATTEMPTS` = 3 attempts per logical call.
* **Superseding.** The ambiguous predecessor becomes terminal ``failed`` with
  ``estimated`` cost = its full reserve (conservative: assume it may have been
  billed), recorded durably by
  :meth:`~review_article_agent.runtime.dispatch.RuntimeDispatchRepository.supersede_unknown_for_retry`.
  The successor link is derivable from the request ids, so no storage migration
  is needed.
* **Exhausted.** When all attempts are ambiguous, or the overhead cap refuses a
  further retry, the last ambiguous dispatch is resolved by
  :meth:`~review_article_agent.runtime.dispatch.RuntimeDispatchRepository.abandon_unknown_after_retries`
  so the remote slot is released and the caller reports a definitive
  non-completion.
* **Overhead cap.** Per accounting stage, a retry is allowed only if
  ``superseded_total + retry_reserve <= max(RETRY_OVERHEAD_RATIO * base_total,
  retry_reserve)`` — see :data:`RETRY_OVERHEAD_RATIO` and
  :func:`_overhead_allows`. The ``max(..., retry_reserve)`` floor lets the first
  retry in a fresh stage happen. All existing caps (per-role hosted-use,
  stage/project in ``budget_usage``) still apply unchanged to the new reserve; a
  cap refusal stays a definitive pre-send refusal.
* **Backoff.** 2 s then 8 s between attempts, injected as a ``sleep`` callable
  (tests pass a no-op).

Runner integration recipe
------------------------

A runner's dispatch loop becomes::

    from review_article_agent.runtime.retry import next_attempt

    base_request_id = ...                       # the logical call's base id
    while True:
        decision = next_attempt(store, base_request_id)   # sleep defaults to time.sleep
        if decision.outcome == "dispatch":
            # Reuse today's exact path for this NEW id: record the task, reserve,
            # mark_submitted, POST, and commit the result/cost under
            # decision.request_id (idempotent for an already-``reserved`` row).
            run_one_attempt(decision.request_id)
            continue
        if decision.outcome == "completed":
            ...        # a prior attempt completed; read its result back
        else:          # "definitive" | "exhausted" | "refused_overhead"
            ...        # stop with a definitive non-completion
        break

``next_attempt`` owns the supersede/abandon transitions; the runner must **not**
re-implement them. The runner still calls ``recover`` after a crash so an
outstanding ``submitted`` row becomes ``unknown`` and is then retryable here.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Callable

from .dispatch import (
    RETRY_MAX_ATTEMPTS,
    DispatchRecord,
    DispatchStateError,
    RuntimeDispatchRepository,
    is_retry_resolved,
    retry_request_ids,
)
from .jobs import _add_exact

__all__ = [
    "RETRY_BACKOFF_SECONDS",
    "RETRY_MAX_ATTEMPTS",
    "RETRY_OVERHEAD_RATIO",
    "RetryDecision",
    "backoff_seconds",
    "next_attempt",
    "retry_request_ids",
]

#: The maximum cost overhead a stage's retries may add, relative to the stage's
#: non-retry spend (owner decision 2026-11-01: "~30% cost increase can be
#: accepted for retries like this").
RETRY_OVERHEAD_RATIO = Decimal("0.30")

#: Backoff before attempt ``k`` (k = 1, 2): 2 s then 8 s.
RETRY_BACKOFF_SECONDS = (2.0, 8.0)

#: The closed set of decision outcomes :func:`next_attempt` returns.
RETRY_OUTCOMES = (
    "completed",
    "definitive",
    "dispatch",
    "exhausted",
    "refused_overhead",
)


@dataclass(frozen=True)
class RetryDecision:
    """The outcome of inspecting one logical call's retry chain.

    ``outcome`` is one of :data:`RETRY_OUTCOMES`:

    * ``"completed"`` — a prior attempt completed; ``dispatch`` is that record.
    * ``"definitive"`` — a prior attempt failed definitively (never retried);
      ``dispatch`` is that record.
    * ``"dispatch"`` — send ``request_id`` (a **new** identity with no row yet, or
      an existing ``reserved`` row to re-claim). Any ambiguous predecessor was
      already superseded. ``delay_seconds`` is the backoff already slept.
    * ``"exhausted"`` — all attempts were ambiguous; the last was resolved to
      ``failed``/``estimated``; ``dispatch`` is that record.
    * ``"refused_overhead"`` — the overhead cap refused a further retry; the
      ambiguous predecessor was resolved to ``failed``/``estimated``; ``dispatch``
      is that record.
    """

    outcome: str
    request_id: str
    dispatch: DispatchRecord | None = None
    attempt: int = 0
    delay_seconds: float = 0.0

    @property
    def should_dispatch(self) -> bool:
        """``True`` iff the caller must send ``request_id`` now."""
        return self.outcome == "dispatch"


def backoff_seconds(attempt: int) -> float:
    """The backoff (seconds) before attempt ``attempt`` (0-based index).

    Attempt 0 (the base) has no backoff; attempt 1 waits
    ``RETRY_BACKOFF_SECONDS[0]`` (2 s); attempt 2 waits ``[1]`` (8 s).
    """
    if attempt <= 0:
        return 0.0
    index = attempt - 1
    if index < len(RETRY_BACKOFF_SECONDS):
        return RETRY_BACKOFF_SECONDS[index]
    return RETRY_BACKOFF_SECONDS[-1]


def next_attempt(
    store,
    base_request_id: str,
    *,
    sleep: Callable[[float], None] | None = None,
    ratio: Decimal = RETRY_OVERHEAD_RATIO,
    repo: RuntimeDispatchRepository | None = None,
) -> RetryDecision:
    """Inspect one logical call's retry chain and return the next decision.

    ``store`` is a verified :class:`~review_article_agent.storage.store.ProjectStore`
    (a pre-built ``repo`` may be injected for tests). ``base_request_id`` is the
    logical call's base id; the chain is
    ``retry_request_ids(base_request_id)``. The supersede/abandon transitions are
    performed here, in the same durable ledger the runners already use.

    ``sleep`` is called with the backoff delay before a retry ``dispatch``
    decision is returned (attempt >= 1); pass a no-op in tests. The default is
    :func:`time.sleep`, imported lazily so the module stays import-light.
    """
    dispatches = repo if repo is not None else RuntimeDispatchRepository(store)
    chain = retry_request_ids(base_request_id)
    chain_ids = set(chain)
    rows = [dispatches.get_dispatch_by_request(rid) for rid in chain]
    last_index = len(chain) - 1

    for index, request_id in enumerate(chain):
        row = rows[index]
        if row is None:
            if index == 0:
                return _dispatch_decision(request_id, 0, sleep)
            previous = rows[index - 1]
            if previous is None:  # pragma: no cover - guarded by iteration order
                raise DispatchStateError(
                    "the retry chain is missing its predecessor; reconcile the "
                    "ledger before retrying."
                )
            if previous.state == "unknown":
                if not _overhead_allows(dispatches, previous, chain_ids, ratio):
                    dispatches.abandon_unknown_after_retries(previous.dispatch_id)
                    return RetryDecision(
                        "refused_overhead",
                        previous.request_id,
                        previous,
                        attempt=index - 1,
                    )
                dispatches.supersede_unknown_for_retry(
                    previous.dispatch_id, successor_request_id=request_id
                )
                return _dispatch_decision(request_id, index, sleep)
            if is_retry_resolved(previous):
                # The predecessor was already superseded; send the successor.
                return _dispatch_decision(request_id, index, sleep)
            raise DispatchStateError(
                "the retry predecessor is neither ambiguous nor a resolved retry; "
                "reconcile it before retrying."
            )

        if row.state == "completed":
            return RetryDecision("completed", request_id, row, attempt=index)
        if row.state == "unknown":
            if index == last_index:
                dispatches.abandon_unknown_after_retries(row.dispatch_id)
                return RetryDecision("exhausted", request_id, row, attempt=index)
            # Not the last attempt: the successor is handled next iteration.
            continue
        if row.state == "failed":
            if is_retry_resolved(row):
                if index == last_index:
                    return RetryDecision("exhausted", request_id, row, attempt=index)
                continue
            return RetryDecision("definitive", request_id, row, attempt=index)
        if row.state == "reserved":
            # The reserve exists but nothing was sent; re-claim and send it.
            return _dispatch_decision(request_id, index, sleep)
        # submitted / admitted: in flight and not yet ambiguous. A retry must not
        # race an unreconciled send claim.
        raise DispatchStateError(
            "the dispatch is in flight; recover/reconcile it before a retry."
        )

    # Unreachable in normal operation: the loop always returns or resolves the
    # last attempt. Fail closed rather than silently dispatching.
    raise DispatchStateError(  # pragma: no cover - defensive
        "the retry chain ended without a decision; reconcile the ledger."
    )


def _dispatch_decision(
    request_id: str, attempt: int, sleep: Callable[[float], None] | None
) -> RetryDecision:
    """Build a ``dispatch`` decision, sleeping the attempt's backoff first."""
    delay = backoff_seconds(attempt)
    if delay and sleep is not None:
        sleep(delay)
    return RetryDecision(
        "dispatch", request_id, None, attempt=attempt, delay_seconds=delay
    )


def _counted_cost(record: DispatchRecord) -> Decimal:
    """A dispatch's conservative counted cost (known/estimated amount, else reserve)."""
    if record.cost_state in ("known", "estimated") and record.cost_amount_usd is not None:
        return max(Decimal(record.reserve_usd), Decimal(record.cost_amount_usd))
    return Decimal(record.reserve_usd)


def _overhead_allows(
    dispatches: RuntimeDispatchRepository,
    predecessor: DispatchRecord,
    chain_ids: set[str],
    ratio: Decimal,
) -> bool:
    """Apply the per-stage retry overhead cap for one further retry.

    ``superseded_total`` is the summed estimated cost of every dispatch already
    resolved by this mechanism in the predecessor's stage; ``base_total`` is the
    stage's **other** (non-chain) dispatches at their known/estimated cost, or
    reserve while still outstanding. The retry is allowed iff
    ``superseded_total + retry_reserve <= max(ratio * base_total, retry_reserve)``.
    """
    retry_reserve = Decimal(predecessor.reserve_usd)
    stage = predecessor.stage
    superseded_total = Decimal("0")
    base_total = Decimal("0")
    for record in dispatches.list_dispatches():
        if record.stage != stage:
            continue
        if is_retry_resolved(record):
            superseded_total = _add_exact(
                superseded_total, Decimal(record.cost_amount_usd or "0")
            )
            continue
        if record.request_id in chain_ids:
            continue
        base_total = _add_exact(base_total, _counted_cost(record))
    cap = max(ratio * base_total, retry_reserve)
    return _add_exact(superseded_total, retry_reserve) <= cap
