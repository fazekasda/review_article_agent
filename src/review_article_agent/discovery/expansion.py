"""Trusted-caller bounded bidirectional citation traversal (Step 05).

Authority
---------

``docs/implementation/05-discovery.md`` D3 AC5 ("A known-relevant fixture set and
forward/backward citation expansion expose explicit coverage gaps; reporting
never claims absolute completeness") and AC7 ("No arbitrary API result cap
truncation is hidden as complete retrieval"), ``docs/specification.md`` workflow
step 5 ("citation expansion ... a coverage report supports an explicit human
decision to search again or freeze ... Never promise all papers") and the
citation-origin policy (``maximum citation hops``, ``new sources per
claim/project``, ``detect cycles``; **no universal numerical default** — explicit
per-project limits are required before upstream tracing), plus contract C03
``POL-OPS-COVERAGE`` (record sources/queries/dates/yields; never a universal
completeness rule).

What this is
------------

A **library** mechanism for a *trusted caller* (a later approved executor) that
walks the citation graph around a set of explicit **roots** using the existing
:class:`~review_article_agent.discovery.citations.EuropePMCCitationClient` over
the shared :class:`~review_article_agent.discovery.transport.DiscoveryTransport`:

* **Breadth-first, bidirectional.** Every expanded publication node is walked in
  a fixed direction order (``references`` = backward, then ``citations`` =
  forward), and discovered children are queued in the exact order the provider
  returned them. Cycles and already-seen nodes are never re-expanded, so the
  traversal is finite and deterministic.
* **Explicit, caller-supplied limits.** :class:`ExpansionLimits` carries
  ``max_hops``, ``max_sources``, ``max_requests``, ``page_size`` and
  ``max_pages``. There is **no universal default**: the caller (the outer
  approved executor, enforcing a per-project decision) supplies every value. The
  module only enforces generous *technical* safety bounds (see
  :data:`MAX_HOPS_HARD`), never a scientific completeness cutoff. Each stored
  relation result is itself bounded by the existing shared codec
  (``MAX_SEARCH_RESULT_BYTES``, 512 MiB) and the Europe PMC page envelope, so no
  new record-size rule is invented here.
* **Every low-level HTTP attempt is budgeted.** The request cap counts **every**
  real low-level attempt, including transport retries, not merely one per relation
  call (see :class:`_GuardedLowLevel`). The shared transport is reused unchanged in
  behaviour: its low-level hook is injected, and the only transport addition is one
  opt-in exception type it catches (see below).
* **Durable, replayable provenance.** Each relation result is persisted through
  the existing :class:`~review_article_agent.storage.discovery.DiscoveryRepository`
  under a deterministic relation request key derived from the operation
  fingerprint, the parent native id and the direction. On an exact replay the
  saved relation result is reused (no provider request); the returned report is
  semantically identical, including the historical timestamps and result hashes.
* **Immutable, serializable report.** :meth:`ExpansionReport.to_mapping` yields a
  canonical, JSON-representable object with the format/version, project id, roots,
  limits, relation run refs/result hashes, every edge occurrence, the visited
  nodes and the unexpanded frontier, the request/attempt counts, the explicit
  stop/gap list, and ``completeness_claimed=False`` / ``verified_support=False``.

What this is **not**
--------------------

* **Not an authorizer and not a gate.** It never approves scope, queries,
  candidates, or a corpus freeze; the ``before_request`` callback and the outer
  executor own that. It performs **no** scientific decision.
* **Not verified scientific support.** A reference/citation link is
  **bibliographic metadata only**: there is no evidence that a claim is
  supported, contradicted, or new. ``verified_support`` is always ``False``.
* **Not a completeness claim.** A completed traversal is a bounded traversal of
  *the configured graph*. A known-relevant neighbour that the provider did not
  return, an empty relation, a page limit, a failure, or any reached budget is an
  explicit gap; ``completeness_claimed`` is always ``False``.
* **Not identity reconciliation.** No deduplication across providers, no study
  identity, no candidate ingestion. Occurrences stay exactly as observed.

Operation identity and the documented split
-------------------------------------------

The **relation request key** of every per-node/per-direction observation embeds a
deterministic *operation fingerprint* derived from the roots and the limits, so a
changed roots/limits configuration produces **distinct** keys and can never
overwrite a prior operation's history (the store treats a different request key as
a separate observation run). Detecting a *divergent logical request* — the same
caller ``request_id`` reused with a different configuration — is deliberately
**the outer job layer's responsibility**: this module does not fabricate an
"operation idempotency" record it cannot honestly guarantee. Replaying the *same*
``request_id`` with the *same* fingerprint reuses the original stored relation
results.

Known limitations (honest, not hidden)
--------------------------------------

* **An interrupted relation is preserved and marked, never silently lost.** A
  request budget or a ``before_request`` refusal stops the traversal with an
  explicit ``request_limit``/``before_request_refused`` gap (plus
  ``relation_interrupted``). The refusal is raised by the injected low-level hook
  as the opt-in
  :class:`~review_article_agent.discovery.transport.DiscoveryRequestInterrupted`
  and caught only by :meth:`DiscoveryTransport.get`, which returns an explicit
  failed retrieval with the attempts already recorded and **no** fabricated
  attempt; the shared adapter then returns a partial :class:`SearchResult` that
  keeps every already-observed page and edge. That partial result is persisted
  like any other, so a replay reproduces the identical report with no provider
  request. The only thing not reconstructed is a page whose request was never
  made, which is exactly the explicit local gap.
* **The request budget is not a research budget.** ``max_requests`` counts
  low-level attempts, including retries; hitting it is an explicit stop, never
  "we searched everything".

Request boundary
----------------

This module opens no socket itself: every request is an explicit
:class:`~review_article_agent.discovery.citations.EuropePMCCitationClient` call
routed through an injected low-level hook (default
:func:`~review_article_agent.discovery.transport.http_get`). Construction performs
no network work. A ``max_requests`` of zero performs **no** request and returns an
explicit ``request_limit`` gap. The committed tests inject a scripted low-level
hook, so the test suite makes no network call.
"""

from __future__ import annotations

import hashlib
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping

from ..domain.errors import ReviewAgentError
from ..domain.records import canonical_json, validate_identifier
from .citations import (
    MAX_MAX_PAGES,
    MAX_PAGE_SIZE,
    MIN_MAX_PAGES,
    MIN_PAGE_SIZE,
    EuropePMCCitationClient,
)
from .records import SearchResult
from .transport import (
    DEFAULT_MAX_ATTEMPTS,
    DiscoveryRequestInterrupted,
    DiscoveryTransport,
    DiscoveryValidationError,
    http_get,
    validate_epmc_citation_identity,
)

__all__ = [
    "EXPANSION_FORMAT",
    "EXPANSION_FORMAT_VERSION",
    "EXPANSION_FINGERPRINT_VERSION",
    "DIRECTIONS",
    "MAX_HOPS_HARD",
    "MAX_SOURCES_HARD",
    "MAX_REQUESTS_HARD",
    "MAX_REQUEST_ID_CHARS",
    "EXPANSION_STOP_REASONS",
    "ExpansionError",
    "ExpansionValidationError",
    "ExpansionLimits",
    "CitationNode",
    "RequestAttemptContext",
    "RelationExpansion",
    "EdgeOccurrence",
    "NodeObservation",
    "FrontierNode",
    "ExpansionStop",
    "ExpansionReport",
    "expand_citations",
]

#: Stable report tag; a different family is never mistaken for this report.
EXPANSION_FORMAT = "review_article_agent.discovery.citation_expansion"

#: Version of the report representation below.
EXPANSION_FORMAT_VERSION = 1

#: Version of the operation-fingerprint material.
EXPANSION_FINGERPRINT_VERSION = 1

#: The two permitted directions, in the fixed traversal order.
DIRECTIONS = ("references", "citations")

#: Generous **technical** hard bounds. These guard a single process against an
#: unbounded traversal; they are not a scientific completeness rule and the caller
#: still has to choose a (usually far smaller) per-project limit.
MAX_HOPS_HARD = 256
MAX_SOURCES_HARD = 1_000_000
MAX_REQUESTS_HARD = 1_000_000

#: The caller's replay key follows the existing bounded opaque-identifier shape.
MAX_REQUEST_ID_CHARS = 512

#: The retry budget this module configures on its internal transport. It is fixed
#: (never caller-supplied) so that a local refusal — which can only happen at
#: attempt ``1.._INTERNAL_MAX_ATTEMPTS`` — is unambiguously distinguishable, from
#: the persisted result alone, from a genuine provider ``attempts_exhausted``
#: (which always records exactly this many attempts). This keeps a replayed
#: interrupted relation identical to the original without any new field.
_INTERNAL_MAX_ATTEMPTS = DEFAULT_MAX_ATTEMPTS

#: Static, curated expansion stop reasons (never peer text).
#: ``hop_limit`` / ``source_limit`` / ``request_limit`` / ``before_request_refused``
#: are budget or policy stops; ``relation_incomplete`` / ``empty_relation`` /
#: ``unsafe_native_id`` / ``relation_interrupted`` are explicit coverage gaps.
EXPANSION_STOP_REASONS = (
    "hop_limit",
    "source_limit",
    "request_limit",
    "before_request_refused",
    "relation_interrupted",
    "relation_incomplete",
    "empty_relation",
    "unsafe_native_id",
)


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #
class ExpansionError(ReviewAgentError):
    """Base class for every citation-expansion failure."""


class ExpansionValidationError(ExpansionError, ValueError):
    """A locally supplied argument is invalid (nothing was requested)."""


# --------------------------------------------------------------------------- #
# Small strict helpers
# --------------------------------------------------------------------------- #
def _require_bounded_int(
    value: object, field_name: str, *, minimum: int, maximum: int
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ExpansionValidationError(f"{field_name}: must be an integer")
    if value < minimum or value > maximum:
        raise ExpansionValidationError(
            f"{field_name}: is outside the permitted technical range"
        )
    return value


def _validate_native(source: object, native_id: object) -> None:
    """Refuse anything that is not a safe, validated Europe PMC native identity."""
    try:
        validate_epmc_citation_identity(source, native_id, "references")
    except DiscoveryValidationError as exc:
        raise ExpansionValidationError(
            "a native publication identity is not a safe expandable identifier"
        ) from exc


# --------------------------------------------------------------------------- #
# Limits
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ExpansionLimits:
    """Explicit caller-supplied traversal limits (no universal default).

    ``max_hops`` (>= 0), ``max_sources`` (>= 0) and ``max_requests`` (>= 0) are
    non-negative; ``page_size`` and ``max_pages`` are positive and bounded exactly
    as the Europe PMC citation adapter documents (``1..1000``). Every value is
    validated against a generous **technical** hard bound; no scientific
    completeness cutoff is imposed.
    """

    max_hops: int
    max_sources: int
    max_requests: int
    page_size: int
    max_pages: int

    def __post_init__(self) -> None:
        _require_bounded_int(self.max_hops, "max_hops", minimum=0, maximum=MAX_HOPS_HARD)
        _require_bounded_int(
            self.max_sources, "max_sources", minimum=0, maximum=MAX_SOURCES_HARD
        )
        _require_bounded_int(
            self.max_requests, "max_requests", minimum=0, maximum=MAX_REQUESTS_HARD
        )
        _require_bounded_int(
            self.page_size,
            "page_size",
            minimum=MIN_PAGE_SIZE,
            maximum=MAX_PAGE_SIZE,
        )
        _require_bounded_int(
            self.max_pages,
            "max_pages",
            minimum=MIN_MAX_PAGES,
            maximum=MAX_MAX_PAGES,
        )


def _coerce_limits(value: object) -> ExpansionLimits:
    if isinstance(value, ExpansionLimits):
        return value
    if isinstance(value, Mapping):
        expected = {"max_hops", "max_sources", "max_requests", "page_size", "max_pages"}
        if set(value.keys()) != expected:
            raise ExpansionValidationError(
                "limits: must provide exactly max_hops, max_sources, max_requests, "
                "page_size and max_pages"
            )
        return ExpansionLimits(
            max_hops=value["max_hops"],
            max_sources=value["max_sources"],
            max_requests=value["max_requests"],
            page_size=value["page_size"],
            max_pages=value["max_pages"],
        )
    raise ExpansionValidationError(
        "limits: must be an ExpansionLimits or a mapping of its exact fields"
    )


# --------------------------------------------------------------------------- #
# A public citation node (native provider identity)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class CitationNode:
    """One explicit, safe Europe PMC native publication identity (``SOURCE:id``).

    ``source`` is a native provider token (e.g. ``MED``/``PMC``/``PPR``) and ``id``
    is the native record id; both are validated exactly as the citation URL builder
    validates them, so a node can never smuggle URL syntax. This is a
    *publication* identity, never a study identity.
    """

    source: str
    id: str

    def __post_init__(self) -> None:
        _validate_native(self.source, self.id)

    @property
    def native_id(self) -> str:
        """The unambiguous ``SOURCE:id`` string."""
        return f"{self.source}:{self.id}"


def _coerce_root(value: object) -> CitationNode:
    if isinstance(value, CitationNode):
        return value
    if isinstance(value, str):
        if ":" not in value:
            raise ExpansionValidationError(
                "roots: a string root must be a 'SOURCE:id' pair"
            )
        source, native_id = value.split(":", 1)
        return CitationNode(source, native_id)
    if isinstance(value, (tuple, list)) and len(value) == 2:
        return CitationNode(value[0], value[1])
    raise ExpansionValidationError(
        "roots: each root must be a CitationNode, a (source, id) pair, or 'SOURCE:id'"
    )


def _coerce_roots(roots: object) -> tuple[CitationNode, ...]:
    if isinstance(roots, (str, bytes, CitationNode)):
        raise ExpansionValidationError("roots: must be a sequence of roots")
    try:
        items = list(roots)  # type: ignore[arg-type]
    except TypeError as exc:
        raise ExpansionValidationError("roots: must be a sequence of roots") from exc
    if not items:
        raise ExpansionValidationError("roots: must contain at least one root")
    return tuple(_coerce_root(item) for item in items)


def _request_identifier(value: object) -> str:
    try:
        normalized = validate_identifier(value, "request_id")
    except Exception as exc:  # RecordValidationError / TypeError
        raise ExpansionValidationError("request_id: is not a valid identifier") from exc
    if len(normalized) > MAX_REQUEST_ID_CHARS:
        raise ExpansionValidationError("request_id: is too long")
    return normalized


# --------------------------------------------------------------------------- #
# Operation identity
# --------------------------------------------------------------------------- #
def _operation_fingerprint(
    roots: tuple[CitationNode, ...], limits: ExpansionLimits
) -> str:
    """The deterministic fingerprint of the roots + limits configuration."""
    material = canonical_json(
        {
            "v": EXPANSION_FINGERPRINT_VERSION,
            "roots": [[node.source, node.id] for node in roots],
            "limits": {
                "max_hops": limits.max_hops,
                "max_sources": limits.max_sources,
                "max_requests": limits.max_requests,
                "page_size": limits.page_size,
                "max_pages": limits.max_pages,
            },
        }
    )
    return hashlib.sha256(material).hexdigest()


def _relation_key(
    request_id: str,
    fingerprint: str,
    direction: str,
    node: CitationNode,
) -> str:
    """The deterministic relation request key (never collisions on config)."""
    material = canonical_json(
        {
            "v": EXPANSION_FINGERPRINT_VERSION,
            "request_id": request_id,
            "fingerprint": fingerprint,
            "direction": direction,
            "source": node.source,
            "id": node.id,
        }
    )
    return "dsc_" + hashlib.sha256(material).hexdigest()[:40]


# --------------------------------------------------------------------------- #
# The guarded low-level hook (budget + before_request, before every attempt)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class RequestAttemptContext:
    """Inert metadata handed to the trusted ``before_request`` callback.

    It names the exact relation, parent, direction and upcoming attempt; it carries
    no secret, no credential, and no provider payload.
    """

    request_id: str
    operation_fingerprint: str
    relation_request_id: str
    parent: CitationNode
    direction: str
    attempt_no: int
    requested_url: str


class _Budget:
    """The shared request-attempt budget for one expansion."""

    __slots__ = ("remaining", "attempts")

    def __init__(self, max_requests: int) -> None:
        self.remaining = max_requests
        self.attempts = 0


class _GuardedLowLevel:
    """A low-level transport hook that enforces the budget and ``before_request``.

    It is installed as the ``transport`` of a
    :class:`~review_article_agent.discovery.transport.DiscoveryTransport`, so it is
    invoked **before every real low-level attempt** — including each transport
    retry. When the budget is exhausted, or the callback refuses (returns ``False``
    or raises), it raises the public, opt-in
    :class:`~review_article_agent.discovery.transport.DiscoveryRequestInterrupted`
    **before** any request. The shared transport then returns an explicit failed
    retrieval that preserves the attempts already recorded and fabricates none;
    no synthetic HTTP response is ever returned. The refusal cause is kept on
    :attr:`interrupt_reason`.
    """

    def __init__(
        self,
        lowlevel: Callable[..., Any],
        budget: _Budget,
        before_request: Callable[[RequestAttemptContext], object] | None,
        *,
        request_id: str,
        fingerprint: str,
        relation_key: str,
        parent: CitationNode,
        direction: str,
    ) -> None:
        self._lowlevel = lowlevel
        self._budget = budget
        self._before = before_request
        self._request_id = request_id
        self._fingerprint = fingerprint
        self._relation_key = relation_key
        self._parent = parent
        self._direction = direction
        self.interrupted = False
        self.interrupt_reason: str | None = None

    def _refuse(self, reason: str) -> None:
        self.interrupted = True
        self.interrupt_reason = reason
        raise DiscoveryRequestInterrupted(reason)

    def __call__(self, url: str, *, timeout: float, max_response_bytes: int) -> Any:
        if self._budget.remaining <= 0:
            self._refuse("request_limit")
        if self._before is not None:
            context = RequestAttemptContext(
                request_id=self._request_id,
                operation_fingerprint=self._fingerprint,
                relation_request_id=self._relation_key,
                parent=self._parent,
                direction=self._direction,
                attempt_no=self._budget.attempts + 1,
                requested_url=url,
            )
            try:
                decision = self._before(context)
            except Exception:  # noqa: BLE001 - a refusing callback stops us
                self._refuse("before_request_refused")
            if decision is False:
                self._refuse("before_request_refused")
        self._budget.remaining -= 1
        self._budget.attempts += 1
        return self._lowlevel(
            url, timeout=timeout, max_response_bytes=max_response_bytes
        )


# --------------------------------------------------------------------------- #
# Report values
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class RelationExpansion:
    """One persisted relation observation (its store refs and result summary)."""

    request_id: str
    run_id: str
    result_sha256: str
    parent: CitationNode
    direction: str
    stop_reason: str
    complete: bool
    failure: str | None
    page_count: int
    record_count: int
    attempt_count: int
    started_at: str
    completed_at: str
    recorded_at_utc: str


@dataclass(frozen=True)
class EdgeOccurrence:
    """One observed reference/citation occurrence of a relation result.

    ``child_source_id`` is the raw native ``SOURCE:id`` the provider presented (or
    ``None`` for an unmatched entry). ``child_source``/``child_id`` are set only
    when that raw id splits into a safe, expandable native identity; ``resolved``
    is ``True`` when any native id was present, and ``expandable`` only when it can
    legitimately be expanded. No identifier is ever invented.
    """

    parent: CitationNode
    direction: str
    relation_request_id: str
    run_id: str
    occurrence_index: int
    child_source_id: str | None
    child_source: str | None
    child_id: str | None
    resolved: bool
    expandable: bool


@dataclass(frozen=True)
class NodeObservation:
    """One expanded publication node (a relation pair was requested/reused)."""

    node: CitationNode
    depth: int
    expanded: bool


@dataclass(frozen=True)
class FrontierNode:
    """One observed but **unexpanded** node, with the reason it was not expanded."""

    node: CitationNode
    depth: int
    reason: str


@dataclass(frozen=True)
class ExpansionStop:
    """One explicit stop or coverage gap (static, value-free detail)."""

    reason: str
    detail: str
    node: CitationNode | None = None
    direction: str | None = None


class _StopLog:
    """An order-preserving, de-duplicating log of stops."""

    def __init__(self) -> None:
        self._stops: list[ExpansionStop] = []
        self._seen: set[tuple[Any, ...]] = set()

    def add(
        self,
        reason: str,
        detail: str,
        node: CitationNode | None = None,
        direction: str | None = None,
    ) -> None:
        if reason not in EXPANSION_STOP_REASONS:
            raise ExpansionError("an expansion stop reason is not a known literal")
        key = (reason, detail, node, direction)
        if key in self._seen:
            return
        self._seen.add(key)
        self._stops.append(ExpansionStop(reason, detail, node, direction))

    def as_tuple(self) -> tuple[ExpansionStop, ...]:
        return tuple(self._stops)


def _node_mapping(node: CitationNode) -> dict[str, Any]:
    return {"source": node.source, "id": node.id}


@dataclass(frozen=True)
class ExpansionReport:
    """An immutable, serializable bounded-expansion report.

    Everything in it is derived from the persisted relation observations and the
    deterministic traversal; no wall-clock "now" is added. ``to_mapping`` is
    JSON-representable and stable across an exact replay after a store reopen.
    """

    project_id: str
    request_id: str
    operation_fingerprint: str
    limits: ExpansionLimits
    roots: tuple[CitationNode, ...]
    relations: tuple[RelationExpansion, ...]
    edges: tuple[EdgeOccurrence, ...]
    visited: tuple[NodeObservation, ...]
    frontier: tuple[FrontierNode, ...]
    stops: tuple[ExpansionStop, ...]

    def to_mapping(self) -> dict[str, Any]:
        """Return the canonical, JSON-representable report object."""
        attempts = sum(relation.attempt_count for relation in self.relations)
        return {
            "format": EXPANSION_FORMAT,
            "format_version": EXPANSION_FORMAT_VERSION,
            "project_id": self.project_id,
            "request_id": self.request_id,
            "operation": {
                "fingerprint": self.operation_fingerprint,
                "fingerprint_version": EXPANSION_FINGERPRINT_VERSION,
            },
            "roots": [_node_mapping(node) for node in self.roots],
            "limits": {
                "max_hops": self.limits.max_hops,
                "max_sources": self.limits.max_sources,
                "max_requests": self.limits.max_requests,
                "page_size": self.limits.page_size,
                "max_pages": self.limits.max_pages,
            },
            "relations": [
                {
                    "request_id": relation.request_id,
                    "run_id": relation.run_id,
                    "result_sha256": relation.result_sha256,
                    "parent": _node_mapping(relation.parent),
                    "direction": relation.direction,
                    "stop_reason": relation.stop_reason,
                    "complete": relation.complete,
                    "failure": relation.failure,
                    "page_count": relation.page_count,
                    "record_count": relation.record_count,
                    "attempt_count": relation.attempt_count,
                    "started_at": relation.started_at,
                    "completed_at": relation.completed_at,
                    "recorded_at_utc": relation.recorded_at_utc,
                }
                for relation in self.relations
            ],
            "edges": [
                {
                    "parent": _node_mapping(edge.parent),
                    "direction": edge.direction,
                    "relation_request_id": edge.relation_request_id,
                    "run_id": edge.run_id,
                    "occurrence_index": edge.occurrence_index,
                    "child_source_id": edge.child_source_id,
                    "child_source": edge.child_source,
                    "child_id": edge.child_id,
                    "resolved": edge.resolved,
                    "expandable": edge.expandable,
                }
                for edge in self.edges
            ],
            "visited": [
                {
                    "source": observation.node.source,
                    "id": observation.node.id,
                    "depth": observation.depth,
                    "expanded": observation.expanded,
                }
                for observation in self.visited
            ],
            "frontier": [
                {
                    "source": entry.node.source,
                    "id": entry.node.id,
                    "depth": entry.depth,
                    "reason": entry.reason,
                }
                for entry in self.frontier
            ],
            "stops": [
                {
                    "reason": stop.reason,
                    "detail": stop.detail,
                    "node": _node_mapping(stop.node) if stop.node is not None else None,
                    "direction": stop.direction,
                }
                for stop in self.stops
            ],
            "counts": {
                "nodes_visited": len(self.visited),
                "nodes_frontier": len(self.frontier),
                "relations": len(self.relations),
                "edges": len(self.edges),
                "unresolved_edges": sum(1 for edge in self.edges if not edge.resolved),
                "unexpandable_edges": sum(
                    1 for edge in self.edges if edge.resolved and not edge.expandable
                ),
                "request_attempts": attempts,
                "stops": len(self.stops),
            },
            "guarantees": {
                "completeness_claimed": False,
                "verified_support": False,
            },
        }


# --------------------------------------------------------------------------- #
# Store wiring
# --------------------------------------------------------------------------- #
def _repository_for(store: object) -> Any:
    """Accept a DiscoveryRepository, or adapt a project store to one."""
    from ..storage.discovery import DiscoveryRepository

    if isinstance(store, DiscoveryRepository):
        return store
    if hasattr(store, "connection") and hasattr(store, "project_id"):
        return DiscoveryRepository(store)
    raise ExpansionValidationError(
        "store: must be a DiscoveryRepository or a project store"
    )


def _relation_expansion(
    record: Any, node: CitationNode, direction: str
) -> RelationExpansion:
    result: SearchResult = record.result
    return RelationExpansion(
        request_id=record.request_id,
        run_id=record.run_id,
        result_sha256=record.result_sha256,
        parent=node,
        direction=direction,
        stop_reason=result.stop_reason,
        complete=result.complete,
        failure=result.failure,
        page_count=len(result.pages),
        record_count=len(result.records),
        attempt_count=len(result.attempts),
        started_at=result.started_at,
        completed_at=result.completed_at,
        recorded_at_utc=record.recorded_at_utc,
    )


def _local_interruption_cause(
    result: SearchResult, cumulative_attempts: int, max_requests: int
) -> str | None:
    """Classify a locally interrupted relation result, or return ``None``.

    A local budget/gate refusal produces a failed relation whose **last** page is
    an ``attempts_exhausted`` failure holding *fewer* than
    :data:`_INTERNAL_MAX_ATTEMPTS` attempts: the shared transport returns as soon
    as its hook refuses, so it never records its final attempt. A genuine provider
    exhaustion always records exactly :data:`_INTERNAL_MAX_ATTEMPTS` attempts, so
    the two are distinguishable from the persisted result alone (and therefore a
    replay agrees without any new stored field). The precise cause is then derived
    deterministically: the budget boundary is reached exactly when the cumulative
    recorded attempts equal ``max_requests``; otherwise a gate refused.
    """
    if result.stop_reason != "failure" or result.failure != "attempts_exhausted":
        return None
    if not result.pages:
        return None
    last = result.pages[-1]
    if last.complete or last.failure != "attempts_exhausted":
        return None
    if len(last.attempts) >= _INTERNAL_MAX_ATTEMPTS:
        return None  # a genuine provider exhaustion, not a local refusal
    if cumulative_attempts >= max_requests:
        return "request_limit"
    return "before_request_refused"


def _split_native(
    source_id: str | None,
) -> tuple[str | None, str | None, bool]:
    """Split a raw native ``SOURCE:id`` into an expandable native identity.

    Returns ``(source, id, expandable)``. An absent/unsplittable/unsafe value never
    yields an invented identifier: the parse simply fails closed.
    """
    if source_id is None or ":" not in source_id:
        return (None, None, False)
    source, native_id = source_id.split(":", 1)
    try:
        validate_epmc_citation_identity(source, native_id, "references")
    except DiscoveryValidationError:
        return (source, native_id, False)
    return (source, native_id, True)


# --------------------------------------------------------------------------- #
# The traversal
# --------------------------------------------------------------------------- #
def expand_citations(
    store: object,
    roots: Iterable[object],
    *,
    request_id: str,
    limits: ExpansionLimits,
    transport: Callable[..., Any] | None = None,
    before_request: Callable[[RequestAttemptContext], object] | None = None,
) -> ExpansionReport:
    """Walk the bounded citation graph around ``roots`` and return a report.

    ``store`` is a :class:`~review_article_agent.storage.discovery.DiscoveryRepository`
    (or a project store from which one is built). ``roots`` is a sequence of
    :class:`CitationNode`, ``(source, id)`` pairs, or ``"SOURCE:id"`` strings.
    ``request_id`` is the caller's bounded replay key. ``limits`` supplies every
    explicit bound. ``transport`` is an optional **low-level** hook
    (``(url, *, timeout, max_response_bytes) -> TransportResponse``; default
    :func:`~review_article_agent.discovery.transport.http_get`). ``before_request``
    is an optional trusted callback invoked before **every** real attempt; it may
    refuse by returning ``False`` or by raising (both stop the expansion with an
    explicit gap and make no further request).

    The traversal is breadth-first; per expanded node the fixed direction order is
    ``references`` then ``citations``; discovered children are queued in provider
    occurrence order; already-seen nodes and cycles are never re-expanded. Each
    relation result is persisted under a deterministic key, so an exact replay
    reuses the saved result instead of re-fetching.
    """
    repo = _repository_for(store)
    request_id = _request_identifier(request_id)
    validated_limits = _coerce_limits(limits)
    root_nodes = _coerce_roots(roots)
    if transport is not None and not callable(transport):
        raise ExpansionValidationError("transport: must be callable or None")
    if before_request is not None and not callable(before_request):
        raise ExpansionValidationError("before_request: must be callable or None")

    lowlevel = transport if transport is not None else http_get
    fingerprint = _operation_fingerprint(root_nodes, validated_limits)

    # Load the project's existing observations once; a matching relation key is
    # reused verbatim so an exact replay performs no provider request.
    existing = {record.request_id: record for record in repo.list_searches()}

    budget = _Budget(validated_limits.max_requests)
    stops = _StopLog()
    known: dict[tuple[str, str], int] = {}
    frontier_seen: set[tuple[str, str]] = set()
    frontier: list[FrontierNode] = []
    edges: list[EdgeOccurrence] = []
    relations: list[RelationExpansion] = []
    visited: list[NodeObservation] = []
    queue: deque[tuple[CitationNode, int]] = deque()
    halted = False
    halt_reason: str | None = None

    # -- seed the frontier with the explicit roots ---------------------------- #
    for root in root_nodes:
        node_key = (root.source, root.id)
        if node_key in known or node_key in frontier_seen:
            continue
        if len(known) >= validated_limits.max_sources:
            frontier_seen.add(node_key)
            frontier.append(FrontierNode(root, 0, "source_limit"))
            stops.add(
                "source_limit",
                "the source budget was reached before every root could be expanded",
            )
            continue
        known[node_key] = 0
        queue.append((root, 0))

    # -- breadth-first traversal --------------------------------------------- #
    while queue and not halted:
        node, depth = queue.popleft()
        node_key = (node.source, node.id)
        if depth >= validated_limits.max_hops:
            if node_key not in frontier_seen:
                frontier_seen.add(node_key)
                frontier.append(FrontierNode(node, depth, "hop_limit"))
            stops.add(
                "hop_limit",
                "the hop budget was reached; a known publication was not expanded",
                node,
            )
            continue
        if budget.remaining <= 0:
            if node_key not in frontier_seen:
                frontier_seen.add(node_key)
                frontier.append(FrontierNode(node, depth, "request_limit"))
            stops.add("request_limit", "the request budget is exhausted")
            halt_reason = "request_limit"
            halted = True
            continue

        visited.append(NodeObservation(node, depth, True))

        for direction in DIRECTIONS:
            if halted:
                break
            if budget.remaining <= 0:
                stops.add("request_limit", "the request budget is exhausted")
                halt_reason = "request_limit"
                halted = True
                break

            relation_key = _relation_key(
                request_id, fingerprint, direction, node
            )
            record = _retrieve(
                repo=repo,
                existing=existing,
                lowlevel=lowlevel,
                budget=budget,
                before_request=before_request,
                request_id=request_id,
                fingerprint=fingerprint,
                relation_key=relation_key,
                node=node,
                direction=direction,
                limits=validated_limits,
            )

            relations.append(_relation_expansion(record, node, direction))
            result: SearchResult = record.result
            local_cause = _local_interruption_cause(
                result, budget.attempts, validated_limits.max_requests
            )
            if local_cause is not None:
                # A trusted local refusal (budget or gate) — not a provider
                # failure. The persisted partial result and the edges below retain
                # everything already observed; only the not-yet-requested pages
                # are missing, and that is an explicit, typed gap.
                stops.add(
                    local_cause,
                    "a local budget/gate refusal stopped this relation before "
                    "its remaining requests were made",
                    node,
                    direction,
                )
                stops.add(
                    "relation_interrupted",
                    "a relation retrieval was interrupted locally before it completed",
                    node,
                    direction,
                )
                halted = True
                halt_reason = local_cause
            if result.stop_reason != "completed":
                stops.add(
                    "relation_incomplete",
                    f"a relation stopped with '{result.stop_reason}'",
                    node,
                    direction,
                )
            if not result.records:
                stops.add(
                    "empty_relation",
                    "a relation returned no entries; this is not a claim of no references",
                    node,
                    direction,
                )

            for index, entry in enumerate(result.records):
                child_source_id = entry.source_id
                child_source, child_id, expandable = _split_native(child_source_id)
                resolved = child_source_id is not None
                edges.append(
                    EdgeOccurrence(
                        parent=node,
                        direction=direction,
                        relation_request_id=record.request_id,
                        run_id=record.run_id,
                        occurrence_index=index,
                        child_source_id=child_source_id,
                        child_source=child_source,
                        child_id=child_id,
                        resolved=resolved,
                        expandable=expandable,
                    )
                )
                if not resolved:
                    continue
                if not expandable:
                    stops.add(
                        "unsafe_native_id",
                        "a related entry carried a native id that cannot be safely expanded",
                        node,
                        direction,
                    )
                    continue
                child = CitationNode(child_source, child_id)
                child_key = (child.source, child.id)
                if child_key in known:
                    # A cycle or a repeated node: the occurrence is retained above
                    # but the node is never re-expanded.
                    continue
                child_depth = depth + 1
                if len(known) >= validated_limits.max_sources:
                    if child_key not in frontier_seen:
                        frontier_seen.add(child_key)
                        frontier.append(
                            FrontierNode(child, child_depth, "source_limit")
                        )
                    stops.add(
                        "source_limit",
                        "the source budget was reached; related publications were "
                        "recorded but not expanded",
                    )
                    continue
                known[child_key] = child_depth
                if child_depth >= validated_limits.max_hops:
                    if child_key not in frontier_seen:
                        frontier_seen.add(child_key)
                        frontier.append(FrontierNode(child, child_depth, "hop_limit"))
                    stops.add(
                        "hop_limit",
                        "the hop budget was reached; related publications were "
                        "recorded but not expanded",
                    )
                else:
                    queue.append((child, child_depth))

    # A halt abandons the remaining queue; surface every abandoned node as an
    # explicit unexpanded frontier entry rather than dropping it silently.
    if halted and halt_reason is not None:
        while queue:
            node, depth = queue.popleft()
            node_key = (node.source, node.id)
            if node_key in frontier_seen:
                continue
            frontier_seen.add(node_key)
            frontier.append(FrontierNode(node, depth, halt_reason))

    return ExpansionReport(
        project_id=repo.project_id,
        request_id=request_id,
        operation_fingerprint=fingerprint,
        limits=validated_limits,
        roots=root_nodes,
        relations=tuple(relations),
        edges=tuple(edges),
        visited=tuple(visited),
        frontier=tuple(frontier),
        stops=stops.as_tuple(),
    )


def _retrieve(
    *,
    repo: Any,
    existing: Mapping[str, Any],
    lowlevel: Callable[..., Any],
    budget: _Budget,
    before_request: Callable[[RequestAttemptContext], object] | None,
    request_id: str,
    fingerprint: str,
    relation_key: str,
    node: CitationNode,
    direction: str,
    limits: ExpansionLimits,
) -> Any:
    """Return the persisted relation observation for one node/direction.

    A relation already recorded for this exact key is reused with **no** provider
    request; its recorded attempt count is still debited from the request budget so
    an exact replay stops at exactly the same point. Otherwise the relation is
    requested up to ``max_pages`` pages; if the request budget runs out mid-relation
    the injected hook refuses the next request locally and the shared transport
    returns a partial result that keeps every page already observed, which is then
    persisted through the repository.
    """
    saved = existing.get(relation_key)
    if saved is not None:
        reused_attempts = len(saved.result.attempts)
        budget.remaining -= reused_attempts
        budget.attempts += reused_attempts
        return saved

    guard = _GuardedLowLevel(
        lowlevel,
        budget,
        before_request,
        request_id=request_id,
        fingerprint=fingerprint,
        relation_key=relation_key,
        parent=node,
        direction=direction,
    )
    client = EuropePMCCitationClient(
        DiscoveryTransport(transport=guard, max_attempts=_INTERNAL_MAX_ATTEMPTS)
    )
    if direction == "references":
        result = client.references(
            node.source, node.id, page_size=limits.page_size, max_pages=limits.max_pages
        )
    else:
        result = client.citations(
            node.source, node.id, page_size=limits.page_size, max_pages=limits.max_pages
        )
    record = repo.record_search(request_id=relation_key, result=result)
    if isinstance(existing, dict):
        existing[relation_key] = record
    return record
