"""Bounded, stdlib-only public literature discovery contracts (Step 05 slice 1).

This package owns the **first slice** of ``docs/implementation/05-discovery.md``:
a reusable, deterministic HTTP GET transport for the two public literature
providers (NCBI PubMed E-utilities and Europe PMC REST) plus the immutable
retrieval value contracts downstream workers consume.

What this package **is** (deliverables 2/4; AC3/AC4/AC7):

* :mod:`review_article_agent.discovery.transport` — a bounded GET client with a
  fixed allowlist of HTTPS origins/paths, no credentials, redirects and proxies
  disabled, a finite socket timeout, a hard response-size cap, bounded retry
  attempts, bounded retry delay (including ``Retry-After``), per-instance rate
  spacing, and injectable ``sleep``/``clock``/``transport`` for fixtures. Every
  attempt is retained as immutable provenance (exact requested URL, UTC times,
  status, body digest/bytes, attempt count) for both success and failure.
* :mod:`review_article_agent.discovery.records` — frozen, normalized
  bibliographic value contracts (:class:`MetadataRecord`, :class:`SearchPage`,
  :class:`SearchResult`) that preserve provider provenance, keep a missing
  identifier explicit, and never equate publication identity with study identity.

What this package is **not** (explicitly out of scope for this slice):

* no workflow, scientific context, approvals, worker, CLI, freeze, persistence,
  complementary sources, or provider response *models/parsers* — those are later
  Step 05 tasks. This slice only defines the transport and the value contracts.

The whole of Step 05 remains **partial**.
"""

from __future__ import annotations

from .records import (
    FAILURE_REASONS,
    SOURCE_EUROPEPMC,
    SOURCE_PUBMED,
    STOP_REASONS,
    DiscoveryRecordError,
    MetadataRecord,
    SearchPage,
    SearchResult,
)
from .transport import (
    ALLOWED_ENDPOINTS,
    ATTEMPT_OUTCOMES,
    DEFAULT_MAX_ATTEMPTS,
    DEFAULT_MAX_RESPONSE_BYTES,
    DEFAULT_RATE_SPACING_SECONDS,
    DEFAULT_RETRY_DELAY_SECONDS,
    DEFAULT_TIMEOUT_SECONDS,
    MAX_ATTEMPTS,
    MAX_RATE_SPACING_SECONDS,
    MAX_RESPONSE_BYTES,
    MAX_RETRY_DELAY_SECONDS,
    MAX_TIMEOUT_SECONDS,
    MIN_RATE_SPACING_SECONDS,
    MIN_RESPONSE_BYTES,
    MIN_TIMEOUT_SECONDS,
    RETRYABLE_STATUSES,
    TRANSPORT_FAILURE_REASONS,
    AttemptRecord,
    DiscoveryEndpointError,
    DiscoveryTransport,
    DiscoveryTransportError,
    DiscoveryValidationError,
    RetrievalResult,
    TransportNetworkError,
    TransportResponse,
    build_request_url,
    http_get,
)

__all__ = [
    # transport
    "ALLOWED_ENDPOINTS",
    "ATTEMPT_OUTCOMES",
    "DEFAULT_MAX_ATTEMPTS",
    "DEFAULT_MAX_RESPONSE_BYTES",
    "DEFAULT_RATE_SPACING_SECONDS",
    "DEFAULT_RETRY_DELAY_SECONDS",
    "DEFAULT_TIMEOUT_SECONDS",
    "MAX_ATTEMPTS",
    "MAX_RATE_SPACING_SECONDS",
    "MAX_RESPONSE_BYTES",
    "MAX_RETRY_DELAY_SECONDS",
    "MAX_TIMEOUT_SECONDS",
    "MIN_RATE_SPACING_SECONDS",
    "MIN_RESPONSE_BYTES",
    "MIN_TIMEOUT_SECONDS",
    "RETRYABLE_STATUSES",
    "TRANSPORT_FAILURE_REASONS",
    "AttemptRecord",
    "DiscoveryEndpointError",
    "DiscoveryTransport",
    "DiscoveryTransportError",
    "DiscoveryValidationError",
    "RetrievalResult",
    "TransportNetworkError",
    "TransportResponse",
    "http_get",
    "build_request_url",
    # records
    "FAILURE_REASONS",
    "SOURCE_EUROPEPMC",
    "SOURCE_PUBMED",
    "STOP_REASONS",
    "DiscoveryRecordError",
    "MetadataRecord",
    "SearchPage",
    "SearchResult",
]
