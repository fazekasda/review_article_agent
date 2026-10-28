"""Bounded, stdlib-only public literature GET transport (Step 05 slice 1).

Authority
---------

``docs/implementation/05-discovery.md`` deliverables 2/4 and acceptance criteria
3/4/7 (exact queries/timestamps/source/failures/counts, fixture pagination, no
hidden result cap), ``docs/specification.md`` workflow steps 2-5 and the safety
invariants ("treat documents/model outputs as untrusted data", no credential
leakage, bounded network handling), and ``docs/contracts/03-policy-decisions.md``
``POL-OPS-COVERAGE`` (no universal completeness claim; explicit provenance).

What it is
----------

* A **stdlib-only** GET client with a fixed allowlist of HTTPS origins/paths:
  NCBI E-utilities ``esearch.fcgi``/``efetch.fcgi``, the Europe PMC REST
  ``search`` endpoint, and one narrowly-shaped Europe PMC REST citation family
  (``.../rest/{source}/{id}/references`` or ``/citations``, both segments
  validated; see :func:`build_epmc_citation_url`). Any other host, scheme, port,
  or path is refused before a socket is opened.
* **No credentials.** No ``Authorization`` header is ever sent, a URL carrying
  userinfo is refused, and a query parameter whose name looks like a credential
  is refused. There is no paid call and no live smoke here.
* **Redirects and proxies disabled.** The opener refuses every 3xx and registers
  no proxy hook, so a request cannot be silently forwarded to another host or an
  environment-configured proxy.
* **Bounded.** A finite socket timeout, a hard response-size cap (an oversize
  body is an explicit failure, never a silent truncation), bounded retry
  attempts, a bounded retry delay, and per-instance rate spacing.
* **Deterministic.** ``sleep``, ``clock`` and the low-level ``transport`` are
  injectable, so fixtures drive retries, delays, and rate spacing without any
  real clock or network.
* **Immutable provenance.** Every attempt (success or failure) is retained as an
  :class:`AttemptRecord` with the exact requested URL, UTC start/finish times,
  HTTP status, body digest/bytes, and outcome; the result carries the full
  attempt tuple and an explicit failure reason.

What it is **not**
------------------

* Not a parser and not a provider model. It returns bounded bytes; decoding and
  provider-specific parsing belong to later Step 05 tasks.
* Not a workflow, cache, or persistence layer. It never writes a record.
* Not a completeness claim. A successful response is a bounded retrieval of one
  request, never proof that a search is exhaustive.

Honest socket-timeout limitation
--------------------------------

``timeout`` is handed to the socket layer. It bounds connection and read
*inactivity*, not total wall-clock time: a peer that dribbles bytes just under
the timeout can keep a single attempt alive far longer than ``timeout`` seconds.
The byte cap is enforced by reading at most ``max_response_bytes + 1`` bytes, so
an unbounded body cannot exhaust memory, but neither bound is a total-deadline
guarantee.
"""

from __future__ import annotations

import hashlib
import http.client
import math
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping as _AbcMapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from types import MappingProxyType
from typing import Any, Callable, Mapping, Sequence

from ..domain.errors import ReviewAgentError

__all__ = [
    "ALLOWED_ENDPOINTS",
    "EPMC_REST_BASE",
    "CITATION_RELATIONS",
    "MAX_CITATION_SOURCE_LEN",
    "MAX_CITATION_ID_LEN",
    "build_epmc_citation_url",
    "validate_epmc_citation_identity",
    "ATTEMPT_OUTCOMES",
    "TRANSPORT_FAILURE_REASONS",
    "RETRYABLE_STATUSES",
    "DEFAULT_TIMEOUT_SECONDS",
    "MIN_TIMEOUT_SECONDS",
    "MAX_TIMEOUT_SECONDS",
    "DEFAULT_MAX_RESPONSE_BYTES",
    "MIN_RESPONSE_BYTES",
    "MAX_RESPONSE_BYTES",
    "DEFAULT_MAX_ATTEMPTS",
    "MAX_ATTEMPTS",
    "DEFAULT_RETRY_DELAY_SECONDS",
    "MIN_RETRY_DELAY_SECONDS",
    "MAX_RETRY_DELAY_SECONDS",
    "DEFAULT_RATE_SPACING_SECONDS",
    "MIN_RATE_SPACING_SECONDS",
    "MAX_RATE_SPACING_SECONDS",
    "USER_AGENT",
    "DiscoveryTransportError",
    "DiscoveryValidationError",
    "DiscoveryEndpointError",
    "TransportNetworkError",
    "TransportResponse",
    "AttemptRecord",
    "RetrievalResult",
    "DiscoveryTransport",
    "http_get",
    "build_request_url",
]

# --------------------------------------------------------------------------- #
# Fixed allowlist (origin + path)
# --------------------------------------------------------------------------- #

#: The **only** hosts and paths this transport will contact. The key is the
#: lower-cased host; the value is the exact set of permitted paths on that host.
_ALLOWED_ORIGINS: Mapping[str, frozenset[str]] = {
    "eutils.ncbi.nlm.nih.gov": frozenset(
        {
            "/entrez/eutils/esearch.fcgi",
            "/entrez/eutils/efetch.fcgi",
        }
    ),
    "www.ebi.ac.uk": frozenset(
        {
            "/europepmc/webservices/rest/search",
        }
    ),
}

#: The full allowlisted endpoint URLs, for callers and diagnostics.
ALLOWED_ENDPOINTS = (
    "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi",
    "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi",
    "https://www.ebi.ac.uk/europepmc/webservices/rest/search",
)

#: The **one** bounded path *family* this transport may additionally contact: the
#: Europe PMC REST citation relation of a single paper. The scheme/host/port
#: rules are unchanged (still exactly https, default port, no userinfo/fragment),
#: and the only variable segments are a validated native ``source`` and ``id``
#: plus the fixed ``references``/``citations`` relation. This never adds a host;
#: it narrows one documented shape on the already allowlisted ``www.ebi.ac.uk``.
EPMC_REST_BASE = "https://www.ebi.ac.uk/europepmc/webservices/rest"

#: The two permitted citation relations (backward = references, forward = citations).
CITATION_RELATIONS = ("references", "citations")

#: Bounds on the two variable path segments of a citation URL.
MAX_CITATION_SOURCE_LEN = 64
MAX_CITATION_ID_LEN = 256

#: A native provider-source token (``MED``/``PMC``/``PPR``/...): a short
#: uppercase ASCII alphanumeric token. It never contains ``:``/``/``/``%``, so
#: the composed ``source:id`` stays unambiguous and can never smuggle URL syntax.
_NATIVE_SOURCE_RE = re.compile(r"[A-Z][A-Z0-9]*")

#: A bounded, URL-safe provider record id: it must start alphanumeric and may only
#: contain alphanumerics, ``.``, ``_`` and ``-``. This excludes ``/``, ``%``,
#: ``?``, ``#``, ``:``, ``\``, ``@``, whitespace and control characters, so an id
#: can never add a path segment, percent-encode the path, or carry a traversal.
_CITATION_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")

#: Fully-anchored regular expressions for the extra path *families* permitted on
#: an allowlisted host. A family is matched against the URL path only and its
#: captured segments are re-checked by :func:`_citation_path_is_safe`; it can
#: therefore never widen the scheme/host/port/query rules enforced above.
_CITATION_PATH_RE = re.compile(
    r"/europepmc/webservices/rest/"
    r"(?P<source>[A-Z][A-Z0-9]{0,63})/"
    r"(?P<id>[A-Za-z0-9][A-Za-z0-9._-]{0,255})/"
    r"(?P<relation>references|citations)"
)

_ALLOWED_PATH_PATTERNS: Mapping[str, tuple[re.Pattern[str], ...]] = {
    "www.ebi.ac.uk": (_CITATION_PATH_RE,),
}

#: A stable, non-credential User-Agent (identifies the tool; carries no secret).
USER_AGENT = "review-article-agent/0.0.0 (stdlib; local)"

# --------------------------------------------------------------------------- #
# Bounds
# --------------------------------------------------------------------------- #

#: Bounded wall-clock handed to the socket layer for one attempt.
DEFAULT_TIMEOUT_SECONDS = 30.0
MIN_TIMEOUT_SECONDS = 0.1
MAX_TIMEOUT_SECONDS = 300.0

#: Hard cap on a response body. An oversize body is an explicit failure.
DEFAULT_MAX_RESPONSE_BYTES = 8_388_608  # 8 MiB
MIN_RESPONSE_BYTES = 1_024
MAX_RESPONSE_BYTES = 33_554_432  # 32 MiB

#: Bounded retry attempts (a retry is a new attempt number).
DEFAULT_MAX_ATTEMPTS = 3
MAX_ATTEMPTS = 5

#: Bounded delay between retryable attempts.
DEFAULT_RETRY_DELAY_SECONDS = 1.0
MIN_RETRY_DELAY_SECONDS = 0.0
MAX_RETRY_DELAY_SECONDS = 60.0

#: Conservative finite stand-in for a ``Retry-After`` value too large to
#: represent as a float (e.g. a 400-digit integer). It always exceeds any
#: permitted ``max_retry_delay`` (which is itself bounded by
#: :data:`MAX_RETRY_DELAY_SECONDS`), so such a value yields an explicit
#: ``retry_after_exceeds_bound`` failure instead of an ``OverflowError``.
_RETRY_AFTER_OVERFLOW = MAX_RETRY_DELAY_SECONDS + 1.0

#: Any decimal ``Retry-After`` with more significant digits than this is a
#: genuine (huge) delay that cannot be a permitted retry: it is detected
#: lexically **before** ``int()`` so a value beyond Python's integer-string
#: digit limit (``sys.set_int_max_str_digits``, ~4300) is never silently
#: dropped, and it is reported as :data:`_RETRY_AFTER_OVERFLOW`.
_MAX_RETRY_AFTER_DIGITS = 18

#: Per-instance minimum spacing between consecutive requests (NCBI's public,
#: unauthenticated limit is ~3 requests/second, so the default is ~0.34s).
DEFAULT_RATE_SPACING_SECONDS = 0.34
MIN_RATE_SPACING_SECONDS = 0.0
MAX_RATE_SPACING_SECONDS = 60.0

#: Only these HTTP statuses are retried (safe, transient GET conditions). Every
#: other non-200 status is permanent: it is returned as a failure, never retried.
RETRYABLE_STATUSES = frozenset({408, 429, 500, 502, 503, 504})

#: The outcome recorded for one attempt.
ATTEMPT_OUTCOMES = ("ok", "retryable", "permanent", "network_error", "too_large")

#: Static failure reasons a :class:`RetrievalResult` may carry (never peer text).
TRANSPORT_FAILURE_REASONS = (
    "http_status",
    "network_error",
    "retry_after_exceeds_bound",
    "response_too_large",
    "attempts_exhausted",
)

#: Query-parameter names that look like credentials and are therefore refused.
_CREDENTIAL_KEY_RE = re.compile(
    r"(api[_-]?key|apikey|token|secret|password|passwd|credential|bearer|cookie"
    r"|session|auth)",
    re.IGNORECASE,
)


# --------------------------------------------------------------------------- #
# Errors (static messages; never peer text or a secret)
# --------------------------------------------------------------------------- #
class DiscoveryTransportError(ReviewAgentError):
    """Base class for every discovery-transport failure."""


class DiscoveryValidationError(DiscoveryTransportError, ValueError):
    """A locally-supplied option, URL, or parameter is invalid (nothing sent)."""


class DiscoveryEndpointError(DiscoveryTransportError):
    """A request was pointed at a URL outside the fixed allowlist."""


class TransportNetworkError(DiscoveryTransportError):
    """A low-level transport could not complete a request (no HTTP status)."""


# --------------------------------------------------------------------------- #
# Numeric / option bounds
# --------------------------------------------------------------------------- #
def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _require_finite_float(
    value: object, field_name: str, *, minimum: float, maximum: float
) -> float:
    """Validate a finite, bounded number; reject ``bool`` and ``NaN``/``inf``."""
    if isinstance(value, bool) or not _is_number(value):
        raise DiscoveryValidationError(f"{field_name}: must be a finite number")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:  # pragma: no cover
        raise DiscoveryValidationError(f"{field_name}: must be a finite number") from exc
    if not math.isfinite(number):
        raise DiscoveryValidationError(f"{field_name}: must be finite")
    if number < minimum or number > maximum:
        raise DiscoveryValidationError(f"{field_name}: is outside the permitted range")
    return number


def _require_bounded_int(
    value: object, field_name: str, *, minimum: int, maximum: int
) -> int:
    """Validate a bounded integer; reject ``bool`` and non-integers."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise DiscoveryValidationError(f"{field_name}: must be an integer")
    if value < minimum or value > maximum:
        raise DiscoveryValidationError(f"{field_name}: is outside the permitted range")
    return value


# --------------------------------------------------------------------------- #
# URL / parameter validation
# --------------------------------------------------------------------------- #
def _require_citation_segment(
    value: object,
    field_name: str,
    pattern: re.Pattern[str],
    max_len: int,
    *,
    forbid_traversal: bool = False,
) -> str:
    """Validate one variable citation-URL path segment (never URL syntax).

    A segment must be a non-empty string within its length bound, must match its
    strict alphabet (so ``/``, ``%``, ``?``, ``#``, ``:``, ``\\``, ``@``,
    whitespace and control characters are impossible), and — for the record id —
    must not contain a ``..`` traversal sequence. Nothing is ever stripped or
    encoded: a value that is not already safe is refused, not repaired.
    """
    if not isinstance(value, str) or not value:
        raise DiscoveryValidationError(f"{field_name}: must be a non-empty string")
    if len(value) > max_len:
        raise DiscoveryValidationError(f"{field_name}: is too long")
    if value != value.strip() or any(ch.isspace() for ch in value):
        raise DiscoveryValidationError(f"{field_name}: must not contain whitespace")
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
        raise DiscoveryValidationError(
            f"{field_name}: must not contain control characters"
        )
    if pattern.fullmatch(value) is None:
        raise DiscoveryValidationError(f"{field_name}: is not a safe identifier")
    if forbid_traversal and ".." in value:
        raise DiscoveryValidationError(
            f"{field_name}: must not contain a traversal sequence"
        )
    return value


def validate_epmc_citation_identity(
    source: object, native_id: object, relation: object
) -> tuple[str, str, str]:
    """Validate a citation identity and return the exact ``(source, id, relation)``.

    ``relation`` must be one of :data:`CITATION_RELATIONS` (``references`` for the
    backward reference list, ``citations`` for the forward citation list);
    ``source`` must be a Europe PMC native source token; ``native_id`` must be a
    bounded, URL-safe provider record id. Every value is validated **before** it
    is returned, and nothing is stripped or percent-encoded: an unsafe value is
    refused, never repaired. The returned triple is exactly the safe path
    segments :func:`build_epmc_citation_url` composes.
    """
    if relation not in CITATION_RELATIONS:
        raise DiscoveryValidationError(
            "relation: must be one of " + ", ".join(CITATION_RELATIONS)
        )
    validated_source = _require_citation_segment(
        source, "source", _NATIVE_SOURCE_RE, MAX_CITATION_SOURCE_LEN
    )
    validated_id = _require_citation_segment(
        native_id,
        "id",
        _CITATION_ID_RE,
        MAX_CITATION_ID_LEN,
        forbid_traversal=True,
    )
    return validated_source, validated_id, relation


def build_epmc_citation_url(source: object, native_id: object, relation: object) -> str:
    """Validate a paper identity and return its exact Europe PMC citation URL.

    Every argument is validated (see :func:`validate_epmc_citation_identity`)
    **before** a URL is returned, and the produced URL contains no query, no
    fragment, no userinfo, and only already-safe path segments — so
    :func:`build_request_url` (and therefore every real request) accepts it under
    the narrow citation path family and nothing wider.
    """
    validated_source, validated_id, validated_relation = (
        validate_epmc_citation_identity(source, native_id, relation)
    )
    return f"{EPMC_REST_BASE}/{validated_source}/{validated_id}/{validated_relation}"


def _citation_path_is_safe(match: re.Match[str]) -> bool:
    """Re-check the captured segments of a citation-family path match.

    The family regex already restricts the alphabet; this adds the traversal and
    relation checks so a matched path is only accepted when each variable segment
    is genuinely safe and the relation is one of the two documented values.
    """
    groups = match.groupdict()
    source = groups.get("source")
    native_id = groups.get("id")
    relation = groups.get("relation")
    if relation not in CITATION_RELATIONS:
        return False
    if source is None or _NATIVE_SOURCE_RE.fullmatch(source) is None:
        return False
    if native_id is None or _CITATION_ID_RE.fullmatch(native_id) is None:
        return False
    return ".." not in source and ".." not in native_id


def _validate_endpoint_parts(parts: urllib.parse.SplitResult) -> None:
    """Refuse anything that is not an allowlisted HTTPS origin+path."""
    if parts.scheme != "https":
        raise DiscoveryEndpointError("only https endpoints are permitted")
    if parts.fragment:
        raise DiscoveryEndpointError("url fragments are not permitted")
    if parts.username or parts.password:
        raise DiscoveryEndpointError("credentials in the url are not permitted")
    try:
        port = parts.port
    except ValueError as exc:
        raise DiscoveryEndpointError("url port is invalid") from exc
    if port not in (None, 443):
        raise DiscoveryEndpointError("only the default https port is permitted")
    host = parts.hostname
    if host is None:
        raise DiscoveryEndpointError("url has no host")
    allowed_paths = _ALLOWED_ORIGINS.get(host)
    if allowed_paths is not None and parts.path in allowed_paths:
        return
    for pattern in _ALLOWED_PATH_PATTERNS.get(host, ()):
        match = pattern.fullmatch(parts.path)
        if match is not None and _citation_path_is_safe(match):
            return
    raise DiscoveryEndpointError("url is not an allowlisted literature endpoint")


def _check_query_keys(query: str) -> None:
    """Refuse a credential-looking query parameter (no secrets accepted)."""
    for key, _value in urllib.parse.parse_qsl(query, keep_blank_values=True):
        if _CREDENTIAL_KEY_RE.search(key):
            raise DiscoveryValidationError(
                "query parameters must not carry credentials"
            )


def _stringify_param(key: str, value: object) -> str:
    if isinstance(value, bool):
        raise DiscoveryValidationError(
            f"params[{key!r}]: boolean values are not permitted"
        )
    if isinstance(value, str):
        return value
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise DiscoveryValidationError(
                f"params[{key!r}]: must be a finite number"
            )
        return str(value)
    raise DiscoveryValidationError(
        f"params[{key!r}]: must be a string or a finite number"
    )


def _normalize_params(params: object) -> list[tuple[str, str]] | None:
    """Normalize caller parameters to a sorted list of string pairs."""
    if params is None:
        return None
    if isinstance(params, Mapping):
        source: Sequence[Any] = list(params.items())
    elif isinstance(params, (list, tuple)):
        source = params
    else:
        raise DiscoveryValidationError(
            "params must be a mapping or a sequence of key/value pairs"
        )
    items: list[tuple[str, str]] = []
    for pair in source:
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            raise DiscoveryValidationError("params entries must be key/value pairs")
        key, value = pair
        if not isinstance(key, str) or not key:
            raise DiscoveryValidationError("params keys must be non-empty strings")
        if _CREDENTIAL_KEY_RE.search(key):
            raise DiscoveryValidationError(
                "params must not carry credentials"
            )
        items.append((key, _stringify_param(key, value)))
    items.sort()
    return items


def build_request_url(url: object, params: object = None) -> str:
    """Validate *url* against the allowlist and return the exact request URL.

    ``url`` must be one of :data:`ALLOWED_ENDPOINTS` (exact scheme/host/path).
    When ``params`` is supplied it is encoded as the query string and ``url``
    must not already carry a query (an ambiguous request is refused). When
    ``params`` is ``None`` the URL's own query string (if any) is used verbatim.
    The returned string is the exact URL that will be recorded and requested.
    """
    if not isinstance(url, str) or not url:
        raise DiscoveryValidationError("url must be a non-empty string")
    parts = urllib.parse.urlsplit(url)
    _validate_endpoint_parts(parts)
    normalized = _normalize_params(params)
    if normalized is not None and parts.query:
        raise DiscoveryValidationError(
            "url must not carry a query when params are supplied"
        )
    if normalized is None:
        _check_query_keys(parts.query)
        query = parts.query
    else:
        query = urllib.parse.urlencode(normalized)
    base = urllib.parse.urlunsplit(
        (parts.scheme, parts.netloc, parts.path, "", "")
    )
    return f"{base}?{query}" if query else base


# --------------------------------------------------------------------------- #
# HTTP transport (single GET; redirect/proxy disabled)
# --------------------------------------------------------------------------- #
class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse redirects so a request is never forwarded to another host."""

    def http_error_301(self, req, fp, code, msg, headers):  # noqa: D401
        raise urllib.error.HTTPError(
            req.full_url, code, "redirect refused", headers, fp
        )

    http_error_302 = http_error_303 = http_error_307 = http_error_308 = http_error_301


def _http_opener() -> urllib.request.OpenerDirector:
    """An opener that refuses redirects and never uses a proxy.

    Passing an **empty** :class:`urllib.request.ProxyHandler` makes
    :func:`urllib.request.build_opener` skip the ambient default ``ProxyHandler``
    (whose ``getproxies()`` would otherwise register ``HTTP_PROXY``/``HTTPS_PROXY``
    /``no_proxy``), and the empty handler registers no ``*_open`` hook, so no
    request is ever routed through a proxy.
    """
    return urllib.request.build_opener(_NoRedirect(), urllib.request.ProxyHandler({}))


@dataclass(frozen=True)
class TransportResponse:
    """One low-level HTTP response (or a flagged oversize body).

    ``headers`` is a lower-cased mapping; ``truncated`` is set by the real
    transport when the body exceeded the read cap. A fixture transport returns
    the same shape.
    """

    status: int
    body: bytes = b""
    headers: Mapping[str, str] = field(default_factory=dict)
    truncated: bool = False

    def __post_init__(self) -> None:
        if isinstance(self.status, bool) or not isinstance(self.status, int):
            raise DiscoveryValidationError("transport status must be an integer")
        if self.status < 100 or self.status > 599:
            raise DiscoveryValidationError("transport status is outside the HTTP range")
        if not isinstance(self.body, (bytes, bytearray)):
            raise DiscoveryValidationError("transport body must be bytes")
        if not isinstance(self.truncated, bool):
            raise DiscoveryValidationError("transport truncated flag must be a bool")
        if not isinstance(self.headers, _AbcMapping):
            raise DiscoveryValidationError("transport headers must be a mapping")
        copied_headers: dict[str, str] = {}
        for key, value in self.headers.items():
            if not isinstance(key, str) or not isinstance(value, str):
                raise DiscoveryValidationError(
                    "transport headers must map strings to strings"
                )
            copied_headers[key] = value
        # Defensive immutability: copy the body bytes and freeze the header
        # mapping so a caller mutating the original bytearray/dict cannot drift
        # a response that a later classification step reads.
        object.__setattr__(self, "body", bytes(self.body))
        object.__setattr__(self, "headers", MappingProxyType(copied_headers))


def http_get(
    url: str, *, timeout: float, max_response_bytes: int
) -> TransportResponse:
    """One bounded GET. Never retries; raises :class:`TransportNetworkError`.

    The URL is re-validated against the fixed allowlist (and its query checked
    for credential-looking parameters) and the timeout/size options are bounded
    **before** any socket is opened, so a disallowed URL or a malformed option
    raises with zero opener calls even when this function is called directly.
    Redirects are refused and proxies are disabled. At most
    ``max_response_bytes + 1`` bytes are read; a larger body is flagged
    ``truncated`` rather than silently returned. A 4xx/5xx is returned as a
    :class:`TransportResponse` carrying its (bounded) body for the caller to
    classify; a network-level failure raises.

    A broken connection or malformed status line (``http.client.HTTPException``,
    e.g. ``IncompleteRead``/``BadStatusLine``) is **not** an ``OSError``; it is
    caught here and re-raised as a static :class:`TransportNetworkError` so the
    caller records a bounded, retryable attempt instead of leaking a raw
    exception. A non-200 ``HTTPError`` whose error body cannot be read is
    returned as a bounded static HTTP outcome (its status, an empty body), never
    as a fabricated 200.
    """
    validated_url = build_request_url(url)
    timeout = _require_finite_float(
        timeout,
        "timeout",
        minimum=MIN_TIMEOUT_SECONDS,
        maximum=MAX_TIMEOUT_SECONDS,
    )
    max_response_bytes = _require_bounded_int(
        max_response_bytes,
        "max_response_bytes",
        minimum=MIN_RESPONSE_BYTES,
        maximum=MAX_RESPONSE_BYTES,
    )
    request = urllib.request.Request(
        validated_url,
        method="GET",
        headers={
            "Accept": "application/json, application/xml;q=0.9, text/xml;q=0.8",
            "User-Agent": USER_AGENT,
        },
    )
    opener = _http_opener()
    try:
        with opener.open(request, timeout=timeout) as response:
            raw = response.read(max_response_bytes + 1)
            headers = {str(k).lower(): str(v) for k, v in response.headers.items()}
            return TransportResponse(
                status=int(response.status),
                body=raw[:max_response_bytes],
                headers=headers,
                truncated=len(raw) > max_response_bytes,
            )
    except urllib.error.HTTPError as exc:
        # A non-200 status is a bounded static HTTP outcome. If its error body
        # cannot be read (a broken error stream), keep the status with an empty
        # body rather than fabricating a 200 or leaking the raw exception.
        try:
            raw = exc.read(max_response_bytes + 1)
            headers = (
                {str(k).lower(): str(v) for k, v in exc.headers.items()}
                if exc.headers is not None
                else {}
            )
        except Exception:  # noqa: BLE001 - a broken error body is not fatal
            raw, headers = b"", {}
        finally:
            try:
                exc.close()
            except Exception:  # pragma: no cover - defensive
                pass
        return TransportResponse(
            status=int(exc.code),
            body=raw[:max_response_bytes],
            headers=headers,
            truncated=len(raw) > max_response_bytes,
        )
    except (
        urllib.error.URLError,
        TimeoutError,
        OSError,
        http.client.HTTPException,
    ) as exc:
        raise TransportNetworkError("the request did not complete") from exc


# --------------------------------------------------------------------------- #
# Immutable provenance values
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class AttemptRecord:
    """Immutable provenance for one HTTP attempt (success or failure).

    ``requested_url`` is the exact URL sent (no secrets are ever accepted into
    it). ``requested_at``/``completed_at`` are UTC ISO-8601 strings.
    ``body_sha256``/``body_bytes`` describe the bounded body that was read.
    """

    attempt_no: int
    requested_url: str
    requested_at: str
    completed_at: str
    status: int | None
    body_bytes: int
    body_sha256: str | None
    outcome: str
    retry_after_seconds: float | None = None

    def __post_init__(self) -> None:
        if isinstance(self.attempt_no, bool) or not isinstance(self.attempt_no, int):
            raise DiscoveryValidationError("attempt_no must be an integer")
        if self.attempt_no < 1:
            raise DiscoveryValidationError("attempt_no must be positive")
        if self.outcome not in ATTEMPT_OUTCOMES:
            raise DiscoveryValidationError("attempt outcome is not a known literal")
        if self.status is not None and (
            isinstance(self.status, bool) or not isinstance(self.status, int)
        ):
            raise DiscoveryValidationError("attempt status must be an integer or None")
        if isinstance(self.body_bytes, bool) or not isinstance(self.body_bytes, int):
            raise DiscoveryValidationError("attempt body_bytes must be an integer")
        if self.body_bytes < 0:
            raise DiscoveryValidationError("attempt body_bytes must not be negative")
        if self.retry_after_seconds is not None:
            if isinstance(self.retry_after_seconds, bool) or not isinstance(
                self.retry_after_seconds, (int, float)
            ):
                raise DiscoveryValidationError(
                    "attempt retry_after_seconds must be a number or None"
                )
            if not math.isfinite(float(self.retry_after_seconds)):
                raise DiscoveryValidationError(
                    "attempt retry_after_seconds must be finite"
                )
            if self.retry_after_seconds < 0:
                raise DiscoveryValidationError(
                    "attempt retry_after_seconds must not be negative"
                )


@dataclass(frozen=True)
class RetrievalResult:
    """The immutable outcome of one bounded retrieval (success or failure).

    On success ``ok`` is ``True`` and ``body`` holds the bounded response bytes
    (decode with :meth:`text`). On failure ``ok`` is ``False``, ``body`` is
    empty (an error body is never exposed), ``failure`` is a static reason from
    :data:`TRANSPORT_FAILURE_REASONS`, and ``diagnostic`` is a static,
    peer-text-free message. Either way ``attempts`` retains the full provenance.
    """

    ok: bool
    requested_url: str
    status: int | None
    body: bytes
    body_bytes: int
    body_sha256: str | None
    attempts: tuple[AttemptRecord, ...]
    failure: str | None = None
    diagnostic: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.ok, bool):
            raise DiscoveryValidationError("ok must be a bool")
        if not isinstance(self.attempts, tuple):
            raise DiscoveryValidationError("attempts must be a tuple")
        if self.ok:
            if self.failure is not None:
                raise DiscoveryValidationError("a successful result carries no failure")
        else:
            if self.failure not in TRANSPORT_FAILURE_REASONS:
                raise DiscoveryValidationError(
                    "a failed result must carry a known failure reason"
                )

    @property
    def attempt_count(self) -> int:
        """The number of attempts made (always at least one)."""
        return len(self.attempts)

    def text(self) -> str:
        """Decode the bounded body as UTF-8 (invalid bytes replaced)."""
        return self.body.decode("utf-8", "replace")


# --------------------------------------------------------------------------- #
# The transport
# --------------------------------------------------------------------------- #
def _default_utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_retry_after(headers: Mapping[str, str]) -> float | None:
    """Parse a ``Retry-After`` header (integer seconds or an HTTP-date).

    Returns a non-negative number of seconds, or ``None`` when absent or
    unparseable (the caller then falls back to its own retry delay).

    A **decimal** value is detected lexically *before* ``int()``: a value with
    more significant digits than :data:`_MAX_RETRY_AFTER_DIGITS` is a genuine
    (huge) delay and is returned as the conservative finite sentinel
    :data:`_RETRY_AFTER_OVERFLOW`, which always exceeds any permitted bound and
    therefore yields an explicit ``retry_after_exceeds_bound`` failure. This
    holds even for a value beyond Python's integer-string digit limit (which
    would otherwise raise ``ValueError`` and be silently dropped). A malformed
    value (a non-decimal, non-date string) returns ``None``. The raw header is
    never retained.
    """
    raw: str | None = None
    for key, value in headers.items():
        if str(key).lower() == "retry-after":
            raw = value
            break
    if raw is None:
        return None
    raw = str(raw).strip()
    if not raw:
        return None
    digits = raw[1:] if raw.startswith("+") else raw
    if digits.isdecimal():
        # Lexical bound first: never let int() hit its digit limit and raise.
        significant = digits.lstrip("0")
        if len(significant) > _MAX_RETRY_AFTER_DIGITS:
            return _RETRY_AFTER_OVERFLOW
        try:
            seconds = int(significant or "0")
        except ValueError:  # pragma: no cover - defensive
            return _RETRY_AFTER_OVERFLOW
        try:
            value = float(seconds)
        except (OverflowError, ValueError):  # pragma: no cover - defensive
            return _RETRY_AFTER_OVERFLOW
        if not math.isfinite(value):
            return _RETRY_AFTER_OVERFLOW
        return value
    try:
        when = parsedate_to_datetime(raw)
    except (TypeError, ValueError, OverflowError):
        return None
    if when is None:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    try:
        delta = (when - datetime.now(timezone.utc)).total_seconds()
    except (OverflowError, ValueError):  # pragma: no cover - defensive
        return _RETRY_AFTER_OVERFLOW
    if not math.isfinite(delta):
        return _RETRY_AFTER_OVERFLOW
    return max(0.0, delta)


class DiscoveryTransport:
    """A bounded, deterministic GET client for the allowlisted providers.

    Every option is validated and bounded at construction; a malformed option
    (a ``bool`` where a number is expected, ``NaN``/``inf``, or an out-of-range
    value) raises :class:`DiscoveryValidationError` before any request.

    ``sleep``/``clock``/``utcnow``/``transport`` are injectable so tests drive
    retries, delays, rate spacing, and responses deterministically. ``transport``
    is a callable ``(url, *, timeout, max_response_bytes) -> TransportResponse``
    that raises :class:`TransportNetworkError` on a network-level failure.
    """

    def __init__(
        self,
        *,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        retry_delay: float = DEFAULT_RETRY_DELAY_SECONDS,
        max_retry_delay: float = MAX_RETRY_DELAY_SECONDS,
        rate_spacing: float = DEFAULT_RATE_SPACING_SECONDS,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        utcnow: Callable[[], str] = _default_utcnow,
        transport: Callable[..., TransportResponse] = http_get,
    ) -> None:
        self._timeout = _require_finite_float(
            timeout,
            "timeout",
            minimum=MIN_TIMEOUT_SECONDS,
            maximum=MAX_TIMEOUT_SECONDS,
        )
        self._max_response_bytes = _require_bounded_int(
            max_response_bytes,
            "max_response_bytes",
            minimum=MIN_RESPONSE_BYTES,
            maximum=MAX_RESPONSE_BYTES,
        )
        self._max_attempts = _require_bounded_int(
            max_attempts, "max_attempts", minimum=1, maximum=MAX_ATTEMPTS
        )
        self._retry_delay = _require_finite_float(
            retry_delay,
            "retry_delay",
            minimum=MIN_RETRY_DELAY_SECONDS,
            maximum=MAX_RETRY_DELAY_SECONDS,
        )
        self._max_retry_delay = _require_finite_float(
            max_retry_delay,
            "max_retry_delay",
            minimum=MIN_RETRY_DELAY_SECONDS,
            maximum=MAX_RETRY_DELAY_SECONDS,
        )
        if self._max_retry_delay < self._retry_delay:
            raise DiscoveryValidationError(
                "max_retry_delay must be at least retry_delay"
            )
        self._rate_spacing = _require_finite_float(
            rate_spacing,
            "rate_spacing",
            minimum=MIN_RATE_SPACING_SECONDS,
            maximum=MAX_RATE_SPACING_SECONDS,
        )
        if not callable(sleep) or not callable(clock) or not callable(utcnow):
            raise DiscoveryValidationError("sleep, clock, and utcnow must be callable")
        if not callable(transport):
            raise DiscoveryValidationError("transport must be callable")
        self._sleep = sleep
        self._clock = clock
        self._utcnow = utcnow
        self._transport = transport
        self._last_attempt_at: float | None = None

    # -- properties (read-only view of the validated bounds) ---------------- #
    @property
    def timeout(self) -> float:
        return self._timeout

    @property
    def max_response_bytes(self) -> int:
        return self._max_response_bytes

    @property
    def max_attempts(self) -> int:
        return self._max_attempts

    @property
    def rate_spacing(self) -> float:
        return self._rate_spacing

    # -- internals ---------------------------------------------------------- #
    def _wait_for_spacing(self) -> None:
        now = self._clock()
        if self._last_attempt_at is not None:
            elapsed = now - self._last_attempt_at
            if elapsed < self._rate_spacing:
                self._sleep(self._rate_spacing - elapsed)
        self._last_attempt_at = self._clock()

    def _sleep_and_mark(self, seconds: float) -> None:
        if seconds > 0:
            self._sleep(seconds)
        self._last_attempt_at = self._clock()

    @staticmethod
    def _failure(
        requested_url: str,
        attempts: list[AttemptRecord],
        failure: str,
        status: int | None,
        diagnostic: str,
    ) -> RetrievalResult:
        return RetrievalResult(
            ok=False,
            requested_url=requested_url,
            status=status,
            body=b"",
            body_bytes=0,
            body_sha256=None,
            attempts=tuple(attempts),
            failure=failure,
            diagnostic=diagnostic,
        )

    # -- the single public entry point -------------------------------------- #
    def get(self, url: str, params: object = None) -> RetrievalResult:
        """Perform one bounded, retried GET and return an immutable result.

        Validation failures (a non-allowlisted URL, a credential-looking
        parameter, an ambiguous URL/params combination) raise
        :class:`DiscoveryValidationError`/:class:`DiscoveryEndpointError` before
        any request. Once a request is attempted, no *network or HTTP* outcome
        raises: a permanent status, an exhausted retry budget, an oversize body,
        or a network failure is returned as a failed :class:`RetrievalResult`
        with static diagnostics and full attempt provenance. (A locally-injected
        ``transport`` that violates its contract is a caller bug and raises
        :class:`DiscoveryValidationError`.)
        """
        final_url = build_request_url(url, params)
        attempts: list[AttemptRecord] = []
        for attempt_no in range(1, self._max_attempts + 1):
            self._wait_for_spacing()
            requested_at = self._utcnow()
            try:
                response = self._transport(
                    final_url,
                    timeout=self._timeout,
                    max_response_bytes=self._max_response_bytes,
                )
            except TransportNetworkError:
                completed_at = self._utcnow()
                attempts.append(
                    AttemptRecord(
                        attempt_no=attempt_no,
                        requested_url=final_url,
                        requested_at=requested_at,
                        completed_at=completed_at,
                        status=None,
                        body_bytes=0,
                        body_sha256=None,
                        outcome="network_error",
                    )
                )
                if attempt_no < self._max_attempts:
                    self._sleep_and_mark(self._retry_delay)
                    continue
                return self._failure(
                    final_url,
                    attempts,
                    "network_error",
                    None,
                    "the request did not complete",
                )
            if not isinstance(response, TransportResponse):
                raise DiscoveryValidationError(
                    "transport must return a TransportResponse"
                )
            completed_at = self._utcnow()
            status = response.status
            body = bytes(response.body)
            digest = hashlib.sha256(body).hexdigest() if body else None
            if response.truncated or len(body) > self._max_response_bytes:
                attempts.append(
                    AttemptRecord(
                        attempt_no=attempt_no,
                        requested_url=final_url,
                        requested_at=requested_at,
                        completed_at=completed_at,
                        status=status,
                        body_bytes=len(body),
                        body_sha256=digest,
                        outcome="too_large",
                    )
                )
                return self._failure(
                    final_url,
                    attempts,
                    "response_too_large",
                    status,
                    "response body exceeded the permitted size",
                )
            if status == 200:
                attempts.append(
                    AttemptRecord(
                        attempt_no=attempt_no,
                        requested_url=final_url,
                        requested_at=requested_at,
                        completed_at=completed_at,
                        status=status,
                        body_bytes=len(body),
                        body_sha256=digest,
                        outcome="ok",
                    )
                )
                return RetrievalResult(
                    ok=True,
                    requested_url=final_url,
                    status=status,
                    body=body,
                    body_bytes=len(body),
                    body_sha256=digest,
                    attempts=tuple(attempts),
                    failure=None,
                    diagnostic=None,
                )
            retry_after = _parse_retry_after(response.headers)
            if status in RETRYABLE_STATUSES:
                attempts.append(
                    AttemptRecord(
                        attempt_no=attempt_no,
                        requested_url=final_url,
                        requested_at=requested_at,
                        completed_at=completed_at,
                        status=status,
                        body_bytes=len(body),
                        body_sha256=digest,
                        outcome="retryable",
                        retry_after_seconds=retry_after,
                    )
                )
                if attempt_no >= self._max_attempts:
                    return self._failure(
                        final_url,
                        attempts,
                        "attempts_exhausted",
                        status,
                        f"http {status}: retryable status; attempts exhausted",
                    )
                delay = self._retry_delay
                if retry_after is not None:
                    if retry_after > self._max_retry_delay:
                        return self._failure(
                            final_url,
                            attempts,
                            "retry_after_exceeds_bound",
                            status,
                            f"http {status}: server retry-after exceeds the permitted delay",
                        )
                    delay = max(delay, retry_after)
                self._sleep_and_mark(delay)
                continue
            attempts.append(
                AttemptRecord(
                    attempt_no=attempt_no,
                    requested_url=final_url,
                    requested_at=requested_at,
                    completed_at=completed_at,
                    status=status,
                    body_bytes=len(body),
                    body_sha256=digest,
                    outcome="permanent",
                )
            )
            return self._failure(
                final_url,
                attempts,
                "http_status",
                status,
                f"http {status}: request rejected",
            )
        # Unreachable: the loop always returns. Kept for defensive completeness.
        return self._failure(  # pragma: no cover
            final_url,
            attempts,
            "attempts_exhausted",
            None,
            "no attempt was made",
        )
