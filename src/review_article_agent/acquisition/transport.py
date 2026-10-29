"""Bounded, stdlib-only official full-text GET transport (Step 06 slice R1).

Authority
---------

``docs/implementation/06-acquisition.md`` (an official/authorized resolution
waterfall and a missing-paper queue; "Unofficial acquisition is a manual
queue/import path only; no mirror discovery or captcha/paywall bypass code") and
``docs/specification.md`` workflow step 6 (automatic open/authorized sources are
allowed; unofficial acquisition is external/manual followed by import).

Primary provider documentation actually checked (2026-10-29, read-only):

* **Europe PMC Articles RESTful API** — the ``fullTextXML`` operation is
  documented as ``GET /{id}/fullTextXML`` where ``id`` is a PMC external id
  (for example ``PMC3257301``). The OpenAPI operation is published at
  ``https://www.ebi.ac.uk/europepmc/webservices/rest/v3/api-docs`` and the
  human-readable API page is ``https://europepmc.org/RestfulWebService``.
* **NCBI E-utilities ``EFetch``** — ``db=pmc`` is a documented database value
  and ``retmode=xml`` returns XML. See
  ``https://www.nlm.nih.gov/dataguide/eutilities/utilities.html`` (the EFetch
  section) and ``https://www.ncbi.nlm.nih.gov/books/NBK25499/`` (E-utilities
  in depth). The legacy PMC OA ``oa.fcgi`` service is **not** used here (it is
  reported retired in August 2026).

What it is
----------

* A **stdlib-only** GET client with a fixed allowlist of HTTPS origins/paths:
  the two official XML endpoints above. Any other host, scheme, port, or path
  is refused before a socket is opened.
* **No credentials.** No ``Authorization`` header is ever sent, a URL carrying
  userinfo is refused, and a query parameter whose name looks like a credential
  is refused.
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
  HTTP status, media type, body digest/bytes, and outcome.

What it is **not**
------------------

* Not a parser and not a provider model. It returns bounded bytes; deciding
  whether a body is scientific full text belongs to
  :mod:`review_article_agent.acquisition.resolution`.
* Not a workflow, cache, or persistence layer. It never writes a record.
* Not a completeness claim. A successful response is a bounded retrieval of one
  request, never proof that the literature is complete.

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
    "EPMC_FULLTEXT_BASE",
    "PMC_EFETCH_URL",
    "ALLOWED_ENDPOINTS",
    "MAX_PMCID_DIGITS",
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
    "FullTextTransportError",
    "FullTextValidationError",
    "FullTextEndpointError",
    "TransportNetworkError",
    "FullTextRequestInterrupted",
    "TransportResponse",
    "AttemptRecord",
    "RetrievalResult",
    "FullTextTransport",
    "http_get",
    "build_request_url",
    "validate_pmcid",
    "build_epmc_fulltext_url",
    "build_pmc_efetch_url",
]

# --------------------------------------------------------------------------- #
# Fixed allowlist (origin + path)
# --------------------------------------------------------------------------- #

#: The **only** fixed paths this transport will contact, keyed by lower-cased
#: host. The value is the exact set of permitted paths on that host.
_ALLOWED_ORIGINS: Mapping[str, frozenset[str]] = {
    "eutils.ncbi.nlm.nih.gov": frozenset(
        {
            "/entrez/eutils/efetch.fcgi",
        }
    ),
}

#: The full allowlisted endpoint URLs, for callers and diagnostics.
ALLOWED_ENDPOINTS = (
    "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi",
)

#: The **one** bounded path *family* this transport may additionally contact:
#: the Europe PMC ``fullTextXML`` operation of a single PMC id. The
#: scheme/host/port rules are unchanged (exactly https, default port, no
#: userinfo/fragment) and the only variable segment is a validated ``PMC`` id.
EPMC_FULLTEXT_BASE = "https://www.ebi.ac.uk/europepmc/webservices/rest"

#: The fixed NCBI EFetch URL (the PMC full-text XML view is selected by query).
PMC_EFETCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"

#: Upper bound on the decimal digits of a PMC id (real ids are far shorter).
MAX_PMCID_DIGITS = 12

#: A PMC id: ``PMC`` followed by 1..:data:`MAX_PMCID_DIGITS` decimal digits. It
#: contains no URL syntax (no ``/``, ``%``, ``?``, ``#``, ``:``, ``@``,
#: whitespace or control characters), so it can never add a path segment,
#: percent-encode the path, or carry a traversal.
_PMCID_RE = re.compile(r"PMC[0-9]{1,%d}" % MAX_PMCID_DIGITS)

#: Fully-anchored regular expression for the Europe PMC ``fullTextXML`` path
#: family. It is matched against the URL path only; the captured id is
#: re-checked by :func:`_fulltext_path_is_safe`.
_EPMC_FULLTEXT_PATH_RE = re.compile(
    r"/europepmc/webservices/rest/"
    r"(?P<pmcid>PMC[0-9]{1,%d})/fullTextXML" % MAX_PMCID_DIGITS
)

_ALLOWED_PATH_PATTERNS: Mapping[str, tuple[re.Pattern[str], ...]] = {
    "www.ebi.ac.uk": (_EPMC_FULLTEXT_PATH_RE,),
}

#: A stable, non-credential User-Agent (identifies the tool; carries no secret).
USER_AGENT = "review-article-agent/0.0.0 (stdlib; local)"

# --------------------------------------------------------------------------- #
# Bounds
# --------------------------------------------------------------------------- #

DEFAULT_TIMEOUT_SECONDS = 30.0
MIN_TIMEOUT_SECONDS = 0.1
MAX_TIMEOUT_SECONDS = 300.0

DEFAULT_MAX_RESPONSE_BYTES = 16_777_216  # 16 MiB (full-text XML can be large)
MIN_RESPONSE_BYTES = 1_024
MAX_RESPONSE_BYTES = 67_108_864  # 64 MiB

DEFAULT_MAX_ATTEMPTS = 3
MAX_ATTEMPTS = 5

DEFAULT_RETRY_DELAY_SECONDS = 1.0
MIN_RETRY_DELAY_SECONDS = 0.0
MAX_RETRY_DELAY_SECONDS = 60.0

#: Conservative finite stand-in for a ``Retry-After`` value too large to
#: represent as a float. It always exceeds any permitted ``max_retry_delay``.
_RETRY_AFTER_OVERFLOW = MAX_RETRY_DELAY_SECONDS + 1.0
_MAX_RETRY_AFTER_DIGITS = 18

DEFAULT_RATE_SPACING_SECONDS = 0.34
MIN_RATE_SPACING_SECONDS = 0.0
MAX_RATE_SPACING_SECONDS = 60.0

RETRYABLE_STATUSES = frozenset({408, 429, 500, 502, 503, 504})

ATTEMPT_OUTCOMES = ("ok", "retryable", "permanent", "network_error", "too_large")

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
class FullTextTransportError(ReviewAgentError):
    """Base class for every full-text transport failure."""


class FullTextValidationError(FullTextTransportError, ValueError):
    """A locally-supplied option, URL, or parameter is invalid (nothing sent)."""


class FullTextEndpointError(FullTextTransportError):
    """A request was pointed at a URL outside the fixed allowlist."""


class TransportNetworkError(FullTextTransportError):
    """A low-level transport could not complete a request (no HTTP status)."""


class FullTextRequestInterrupted(FullTextTransportError):
    """A trusted low-level hook refused an attempt **before** any socket opened.

    This is an **opt-in** local signal (budget or gate refusal) raised by an
    injected low-level transport. :meth:`FullTextTransport.get` catches *only*
    this type and returns an explicit failed :class:`RetrievalResult`
    (``failure="attempts_exhausted"``) retaining every attempt already recorded,
    adding **no** attempt for the refused request. Any other exception type
    propagates unchanged.
    """


# --------------------------------------------------------------------------- #
# Numeric / option bounds
# --------------------------------------------------------------------------- #
def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _require_finite_float(
    value: object, field_name: str, *, minimum: float, maximum: float
) -> float:
    if isinstance(value, bool) or not _is_number(value):
        raise FullTextValidationError(f"{field_name}: must be a finite number")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:  # pragma: no cover
        raise FullTextValidationError(f"{field_name}: must be a finite number") from exc
    if not math.isfinite(number):
        raise FullTextValidationError(f"{field_name}: must be finite")
    if number < minimum or number > maximum:
        raise FullTextValidationError(f"{field_name}: is outside the permitted range")
    return number


def _require_bounded_int(
    value: object, field_name: str, *, minimum: int, maximum: int
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise FullTextValidationError(f"{field_name}: must be an integer")
    if value < minimum or value > maximum:
        raise FullTextValidationError(f"{field_name}: is outside the permitted range")
    return value


# --------------------------------------------------------------------------- #
# URL / parameter validation
# --------------------------------------------------------------------------- #
def validate_pmcid(value: object) -> str:
    """Validate and return an exact ``PMC`` id (never repaired, never encoded).

    A value must be a string of the form ``PMC`` followed by 1..
    :data:`MAX_PMCID_DIGITS` decimal digits. Nothing is stripped or
    percent-encoded: a value that is not already safe is refused.
    """
    if not isinstance(value, str) or not value:
        raise FullTextValidationError("pmcid: must be a non-empty string")
    if value != value.strip() or any(ch.isspace() for ch in value):
        raise FullTextValidationError("pmcid: must not contain whitespace")
    if _PMCID_RE.fullmatch(value) is None:
        raise FullTextValidationError("pmcid: is not a safe PMC identifier")
    return value


def build_epmc_fulltext_url(pmcid: object) -> str:
    """Return the exact Europe PMC ``fullTextXML`` URL for a PMC id.

    The id is validated first, so the produced URL contains no query, no
    fragment, no userinfo, and only an already-safe path segment.
    """
    validated = validate_pmcid(pmcid)
    return f"{EPMC_FULLTEXT_BASE}/{validated}/fullTextXML"


def build_pmc_efetch_url(pmcid: object) -> str:
    """Return the exact NCBI EFetch ``db=pmc`` XML URL for a PMC id.

    The id is validated first; the query is fixed (``db=pmc``, ``retmode=xml``)
    and carries no credential.
    """
    validated = validate_pmcid(pmcid)
    query = urllib.parse.urlencode(
        [("db", "pmc"), ("id", validated), ("retmode", "xml")]
    )
    return f"{PMC_EFETCH_URL}?{query}"


def _fulltext_path_is_safe(match: re.Match[str]) -> bool:
    """Re-check the captured PMC id of a ``fullTextXML`` path match."""
    pmcid = match.groupdict().get("pmcid")
    return pmcid is not None and _PMCID_RE.fullmatch(pmcid) is not None


def _validate_endpoint_parts(parts: urllib.parse.SplitResult) -> None:
    """Refuse anything that is not an allowlisted HTTPS origin+path."""
    if parts.scheme != "https":
        raise FullTextEndpointError("only https endpoints are permitted")
    if parts.fragment:
        raise FullTextEndpointError("url fragments are not permitted")
    if parts.username or parts.password:
        raise FullTextEndpointError("credentials in the url are not permitted")
    try:
        port = parts.port
    except ValueError as exc:
        raise FullTextEndpointError("url port is invalid") from exc
    if port not in (None, 443):
        raise FullTextEndpointError("only the default https port is permitted")
    host = parts.hostname
    if host is None:
        raise FullTextEndpointError("url has no host")
    allowed_paths = _ALLOWED_ORIGINS.get(host)
    if allowed_paths is not None and parts.path in allowed_paths:
        return
    for pattern in _ALLOWED_PATH_PATTERNS.get(host, ()):
        match = pattern.fullmatch(parts.path)
        if match is not None and _fulltext_path_is_safe(match):
            return
    raise FullTextEndpointError("url is not an allowlisted full-text endpoint")


def _check_query_keys(query: str) -> None:
    """Refuse a credential-looking query parameter (no secrets accepted)."""
    for key, _value in urllib.parse.parse_qsl(query, keep_blank_values=True):
        if _CREDENTIAL_KEY_RE.search(key):
            raise FullTextValidationError(
                "query parameters must not carry credentials"
            )


def _stringify_param(key: str, value: object) -> str:
    if isinstance(value, bool):
        raise FullTextValidationError(
            f"params[{key!r}]: boolean values are not permitted"
        )
    if isinstance(value, str):
        return value
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise FullTextValidationError(
                f"params[{key!r}]: must be a finite number"
            )
        return str(value)
    raise FullTextValidationError(
        f"params[{key!r}]: must be a string or a finite number"
    )


def _normalize_params(params: object) -> list[tuple[str, str]] | None:
    if params is None:
        return None
    if isinstance(params, Mapping):
        source: Sequence[Any] = list(params.items())
    elif isinstance(params, (list, tuple)):
        source = params
    else:
        raise FullTextValidationError(
            "params must be a mapping or a sequence of key/value pairs"
        )
    items: list[tuple[str, str]] = []
    for pair in source:
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            raise FullTextValidationError("params entries must be key/value pairs")
        key, value = pair
        if not isinstance(key, str) or not key:
            raise FullTextValidationError("params keys must be non-empty strings")
        if _CREDENTIAL_KEY_RE.search(key):
            raise FullTextValidationError("params must not carry credentials")
        items.append((key, _stringify_param(key, value)))
    items.sort()
    return items


def build_request_url(url: object, params: object = None) -> str:
    """Validate *url* against the allowlist and return the exact request URL."""
    if not isinstance(url, str) or not url:
        raise FullTextValidationError("url must be a non-empty string")
    parts = urllib.parse.urlsplit(url)
    _validate_endpoint_parts(parts)
    normalized = _normalize_params(params)
    if normalized is not None and parts.query:
        raise FullTextValidationError(
            "url must not carry a query when params are supplied"
        )
    if normalized is None:
        _check_query_keys(parts.query)
        query = parts.query
    else:
        query = urllib.parse.urlencode(normalized)
    base = urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
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
    """An opener that refuses redirects and never uses a proxy."""
    return urllib.request.build_opener(_NoRedirect(), urllib.request.ProxyHandler({}))


@dataclass(frozen=True)
class TransportResponse:
    """One low-level HTTP response (or a flagged oversize body)."""

    status: int
    body: bytes = b""
    headers: Mapping[str, str] = field(default_factory=dict)
    truncated: bool = False

    def __post_init__(self) -> None:
        if isinstance(self.status, bool) or not isinstance(self.status, int):
            raise FullTextValidationError("transport status must be an integer")
        if self.status < 100 or self.status > 599:
            raise FullTextValidationError("transport status is outside the HTTP range")
        if not isinstance(self.body, (bytes, bytearray)):
            raise FullTextValidationError("transport body must be bytes")
        if not isinstance(self.truncated, bool):
            raise FullTextValidationError("transport truncated flag must be a bool")
        if not isinstance(self.headers, _AbcMapping):
            raise FullTextValidationError("transport headers must be a mapping")
        copied_headers: dict[str, str] = {}
        for key, value in self.headers.items():
            if not isinstance(key, str) or not isinstance(value, str):
                raise FullTextValidationError(
                    "transport headers must map strings to strings"
                )
            copied_headers[key] = value
        object.__setattr__(self, "body", bytes(self.body))
        object.__setattr__(self, "headers", MappingProxyType(copied_headers))


def http_get(url: str, *, timeout: float, max_response_bytes: int) -> TransportResponse:
    """One bounded GET. Never retries; raises :class:`TransportNetworkError`."""
    validated_url = build_request_url(url)
    timeout = _require_finite_float(
        timeout, "timeout", minimum=MIN_TIMEOUT_SECONDS, maximum=MAX_TIMEOUT_SECONDS
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
            "Accept": "application/xml, text/xml;q=0.9, */*;q=0.1",
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
def _media_type(headers: Mapping[str, str]) -> str | None:
    """Return a normalized ``content-type`` media type, or ``None``."""
    raw: str | None = None
    for key, value in headers.items():
        if str(key).lower() == "content-type":
            raw = value
            break
    if raw is None:
        return None
    media = str(raw).split(";", 1)[0].strip().lower()
    return media or None


@dataclass(frozen=True)
class AttemptRecord:
    """Immutable provenance for one HTTP attempt (success or failure)."""

    attempt_no: int
    requested_url: str
    requested_at: str
    completed_at: str
    status: int | None
    body_bytes: int
    body_sha256: str | None
    outcome: str
    media_type: str | None = None
    retry_after_seconds: float | None = None

    def __post_init__(self) -> None:
        if isinstance(self.attempt_no, bool) or not isinstance(self.attempt_no, int):
            raise FullTextValidationError("attempt_no must be an integer")
        if self.attempt_no < 1:
            raise FullTextValidationError("attempt_no must be positive")
        if self.outcome not in ATTEMPT_OUTCOMES:
            raise FullTextValidationError("attempt outcome is not a known literal")
        if self.status is not None and (
            isinstance(self.status, bool) or not isinstance(self.status, int)
        ):
            raise FullTextValidationError("attempt status must be an integer or None")
        if isinstance(self.body_bytes, bool) or not isinstance(self.body_bytes, int):
            raise FullTextValidationError("attempt body_bytes must be an integer")
        if self.body_bytes < 0:
            raise FullTextValidationError("attempt body_bytes must not be negative")
        if self.media_type is not None and not isinstance(self.media_type, str):
            raise FullTextValidationError("attempt media_type must be a string or None")
        if self.retry_after_seconds is not None:
            if isinstance(self.retry_after_seconds, bool) or not isinstance(
                self.retry_after_seconds, (int, float)
            ):
                raise FullTextValidationError(
                    "attempt retry_after_seconds must be a number or None"
                )
            if not math.isfinite(float(self.retry_after_seconds)):
                raise FullTextValidationError(
                    "attempt retry_after_seconds must be finite"
                )
            if self.retry_after_seconds < 0:
                raise FullTextValidationError(
                    "attempt retry_after_seconds must not be negative"
                )


@dataclass(frozen=True)
class RetrievalResult:
    """The immutable outcome of one bounded retrieval (success or failure)."""

    ok: bool
    requested_url: str
    status: int | None
    body: bytes
    body_bytes: int
    body_sha256: str | None
    attempts: tuple[AttemptRecord, ...]
    failure: str | None = None
    diagnostic: str | None = None
    media_type: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.ok, bool):
            raise FullTextValidationError("ok must be a bool")
        if not isinstance(self.attempts, tuple):
            raise FullTextValidationError("attempts must be a tuple")
        if self.ok:
            if self.failure is not None:
                raise FullTextValidationError("a successful result carries no failure")
        else:
            if self.failure not in TRANSPORT_FAILURE_REASONS:
                raise FullTextValidationError(
                    "a failed result must carry a known failure reason"
                )

    @property
    def attempt_count(self) -> int:
        return len(self.attempts)

    def text(self) -> str:
        return self.body.decode("utf-8", "replace")


# --------------------------------------------------------------------------- #
# The transport
# --------------------------------------------------------------------------- #
def _default_utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_retry_after(headers: Mapping[str, str]) -> float | None:
    """Parse a ``Retry-After`` header (integer seconds or an HTTP-date)."""
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


class FullTextTransport:
    """A bounded, deterministic GET client for the two official XML endpoints.

    Every option is validated and bounded at construction; a malformed option
    raises :class:`FullTextValidationError` before any request. Constructing an
    instance performs **no** network I/O.

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
            timeout, "timeout", minimum=MIN_TIMEOUT_SECONDS, maximum=MAX_TIMEOUT_SECONDS
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
            raise FullTextValidationError(
                "max_retry_delay must be at least retry_delay"
            )
        self._rate_spacing = _require_finite_float(
            rate_spacing,
            "rate_spacing",
            minimum=MIN_RATE_SPACING_SECONDS,
            maximum=MAX_RATE_SPACING_SECONDS,
        )
        if not callable(sleep) or not callable(clock) or not callable(utcnow):
            raise FullTextValidationError("sleep, clock, and utcnow must be callable")
        if not callable(transport):
            raise FullTextValidationError("transport must be callable")
        self._sleep = sleep
        self._clock = clock
        self._utcnow = utcnow
        self._transport = transport
        self._last_attempt_at: float | None = None

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

    def get(self, url: str, params: object = None) -> RetrievalResult:
        """Perform one bounded, retried GET and return an immutable result."""
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
            except FullTextRequestInterrupted:
                return self._failure(
                    final_url,
                    attempts,
                    "attempts_exhausted",
                    None,
                    "the request was refused locally before it was attempted",
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
                raise FullTextValidationError(
                    "transport must return a TransportResponse"
                )
            completed_at = self._utcnow()
            status = response.status
            body = bytes(response.body)
            digest = hashlib.sha256(body).hexdigest() if body else None
            media = _media_type(response.headers)
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
                        media_type=media,
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
                        media_type=media,
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
                    media_type=media,
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
                        media_type=media,
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
                    media_type=media,
                )
            )
            return self._failure(
                final_url,
                attempts,
                "http_status",
                status,
                f"http {status}: request rejected",
            )
        return self._failure(  # pragma: no cover
            final_url,
            attempts,
            "attempts_exhausted",
            None,
            "no attempt was made",
        )
