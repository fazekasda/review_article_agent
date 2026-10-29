"""Official full-text resolution waterfall (Step 06 slice R1).

Authority
---------

``docs/implementation/06-acquisition.md`` deliverable 1 ("Official/authorized
resolution waterfall, source/version provenance, missing-paper queue") and
acceptance criterion AC6 ("Unofficial acquisition is a manual queue/import path
only; no mirror discovery or captcha/paywall bypass code") and AC7 (storage,
redistribution, and hosted-transfer constraints remain distinct from
free-to-read status); ``docs/specification.md`` workflow step 6 ("Acquire full
text or an explicit human disposition for **every** frozen candidate … Automatic
open/authorized sources are allowed. Unofficial acquisition is external/manual
followed by import").

What it is
----------

A **pure, stdlib-only library** that resolves one frozen candidate's full text
through a **narrow two-step official XML waterfall**:

1. Europe PMC Open Access ``fullTextXML`` by PMC id
   (``GET https://www.ebi.ac.uk/europepmc/webservices/rest/{PMCID}/fullTextXML``).
2. NCBI PMC ``EFetch`` by PMC id
   (``GET https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi?db=pmc&id={PMCID}&retmode=xml``).

The exact endpoint spellings were verified against the providers' **primary**
documentation (read-only) on 2026-10-29; the module docstring of
:mod:`review_article_agent.acquisition.transport` records the exact URLs. The
legacy PMC OA ``oa.fcgi`` service is **not** used (reported retired August 2026),
and no Unpaywall/Crossref/bioRxiv or other new domain is contacted.

Authorization is **caller evidence**, not a scientific grant
-----------------------------------------------------------

The caller must pass ``authorization`` — the trusted caller's **derived,
validated RightsRecord eligibility** for this operation. This library does
**not** create, derive, or validate a rights record; it only refuses to make a
network call unless the caller's evidence is present and affirmative. The
booleans are **not** a scientific grant, a free-to-read judgement, or a
relevance/quality claim: they are the caller's per-operation acquisition/storage
eligibility plus a privacy veto. A missing, unknown, or malformed authorization
yields ``status="blocked"`` with **zero** network calls.

Canonical ``authorization`` shape (a mapping)::

    {
        "acquisition": {"allowed": True, "terms_ref": "...", "checked_at": "..."},
        "storage":     {"allowed": True, "terms_ref": "...", "checked_at": "..."},
        "privacy_denied": False,
    }

A flat convenience form is also accepted: top-level ``acquisition_allowed`` /
``storage_allowed`` booleans with per-operation or shared ``terms_ref`` /
``checked_at`` strings. In every form each operation needs a non-empty
``terms_ref`` and ``checked_at`` (the source terms/evidence actually checked).

What it is **not**
------------------

* **Not** a validating identity/quality module. Deciding whether a retrieved
  body is the *right* paper, complete, or scientifically acceptable belongs to a
  different module. Retrieval here **never** claims a paper is accepted,
  screened, digested, or scientifically validated — only that an official source
  returned an XML full-text article.
* **Not** durable storage. It returns a JSON-safe dict; the caller (a later
  Step 06 task) owns execution and persistence.
* **Not** a completeness or literature-coverage claim.
* **Not** an unofficial-source resolver. Unsupported, publisher, or otherwise
  unofficial locators become a **manual queue** item: no arbitrary URL is ever
  fetched, and no mirror/captcha/paywall bypass exists.

Minimal scientific-full-text check
----------------------------------

A ``200`` response is not enough. Before a source may win the waterfall, the
body must be **parseable XML** and must contain a JATS ``<article>`` with a
non-empty ``<body>``. HTML/login pages, metadata-only records (no ``<body>``),
malformed XML, and empty bodies are classified as failures, the observed
failure is retained, and the waterfall continues (or ends ``manual_required``).
A ``<!ENTITY`` declaration is refused defensively.
"""

from __future__ import annotations

import base64
import hashlib
import re
import xml.etree.ElementTree as ET
from collections.abc import Mapping as _AbcMapping
from typing import Any, Mapping

from .transport import (
    FullTextTransport,
    FullTextValidationError,
    build_epmc_fulltext_url,
    build_pmc_efetch_url,
)

__all__ = [
    "resolve_full_text",
    "RESOLUTION_STATUSES",
    "FAILURE_CODES",
    "RESOLVER_VERSION",
    "EPMC_SOURCE",
    "EPMC_SOURCE_VERSION",
    "PMC_EFETCH_SOURCE",
    "PMC_EFETCH_SOURCE_VERSION",
    "IDENTIFIER_KEYS",
    "MAX_REQUESTS",
    "AUTHORIZATION_SCHEMA",
]

#: The three statuses this library can return.
RESOLUTION_STATUSES = ("retrieved", "manual_required", "blocked")

#: The version of the resolution *policy* (the waterfall and its checks).
RESOLVER_VERSION = "s06-resolution-official-xml-waterfall/1"

#: Source identifiers for provenance.
EPMC_SOURCE = "europepmc_oa_fulltext_xml"
EPMC_SOURCE_VERSION = "europepmc/rest/fullTextXML"
PMC_EFETCH_SOURCE = "pmc_efetch_pmc"
PMC_EFETCH_SOURCE_VERSION = "ncbi/eutils/efetch?db=pmc&retmode=xml"

#: The recognized identifier keys (all optional; ``pmcid`` is required to fetch).
IDENTIFIER_KEYS = ("pmcid", "pmid", "doi")

#: The waterfall is exactly two official requests; there is no broad search and
#: no pagination.
MAX_REQUESTS = 2

#: A short, documented description of the accepted authorization evidence.
AUTHORIZATION_SCHEMA = (
    "mapping with per-operation acquisition/storage evidence "
    "({allowed, terms_ref, checked_at}) and a privacy_denied bool; the flat "
    "form (acquisition_allowed/storage_allowed + terms_ref/checked_at) is also "
    "accepted"
)

#: Static failure codes this library may attach (never peer text).
FAILURE_CODES = (
    # blocked (no network)
    "authorization_missing",
    "authorization_invalid",
    "acquisition_not_authorized",
    "storage_not_authorized",
    "privacy_denied",
    "terms_evidence_missing",
    # manual queue
    "missing_pmcid",
    "invalid_pmcid",
    "unofficial_locator",
    "not_found",
    "metadata_only",
    "html_login",
    "malformed_xml",
    "empty_body",
    "response_too_large",
    "network_error",
    "http_status",
)

_PMCID_RE = re.compile(r"PMC[0-9]{1,12}")

#: A value that looks like an absolute URL of any scheme.
_URLISH_RE = re.compile(r"[A-Za-z][A-Za-z0-9+.\-]*://")

#: Identifier keys that name a locator/URL rather than an official id.
_LOCATOR_KEYS = frozenset(
    {"url", "uri", "locator", "link", "publisher_url", "landing_page", "href"}
)

#: The classification of a bounded body.
_BODY_KINDS = ("fulltext", "html", "metadata_only", "malformed", "empty")

#: Map a non-full-text body kind to its static failure code.
_KIND_FAILURE = {
    "html": "html_login",
    "metadata_only": "metadata_only",
    "malformed": "malformed_xml",
    "empty": "empty_body",
}

#: The two ordered official steps: (source, source_version, url builder).
_STEPS = (
    (EPMC_SOURCE, EPMC_SOURCE_VERSION, build_epmc_fulltext_url),
    (PMC_EFETCH_SOURCE, PMC_EFETCH_SOURCE_VERSION, build_pmc_efetch_url),
)


# --------------------------------------------------------------------------- #
# Identifiers
# --------------------------------------------------------------------------- #
def _normalize_identifiers(
    identifiers: tuple[object, ...],
) -> tuple[dict[str, str], list[dict[str, Any]]]:
    """Merge identifier mappings into recognized ids and unofficial locators.

    Every argument must be a mapping of string keys to string values. A
    recognized key (:data:`IDENTIFIER_KEYS`) whose value is not URL-like is kept
    as an official id. A locator-named key or any URL-like value is recorded as
    an **unofficial locator** and is never turned into a request.
    """
    normalized: dict[str, str] = {}
    unofficial: list[dict[str, Any]] = []
    for mapping in identifiers:
        if not isinstance(mapping, _AbcMapping):
            raise FullTextValidationError("each identifier argument must be a mapping")
        for key, value in mapping.items():
            if not isinstance(key, str) or not key.strip():
                raise FullTextValidationError(
                    "identifier keys must be non-empty strings"
                )
            if not isinstance(value, str):
                raise FullTextValidationError(f"identifier {key!r} must be a string")
            normalized_key = key.strip().lower()
            stripped = value.strip()
            if (
                normalized_key in _LOCATOR_KEYS
                or _URLISH_RE.search(stripped)
            ):
                unofficial.append({"key": normalized_key, "is_url": True})
                continue
            if normalized_key in IDENTIFIER_KEYS and stripped:
                normalized[normalized_key] = stripped
    return normalized, unofficial


# --------------------------------------------------------------------------- #
# Authorization
# --------------------------------------------------------------------------- #
def _operation_evidence(
    authorization: Mapping[str, Any], operation: str
) -> Mapping[str, Any] | None:
    """Resolve one operation's evidence, accepting the flat convenience form."""
    entry = authorization.get(operation)
    if isinstance(entry, _AbcMapping):
        return entry
    if f"{operation}_allowed" in authorization:
        return {
            "allowed": authorization.get(f"{operation}_allowed"),
            "terms_ref": authorization.get(f"{operation}_terms_ref")
            or authorization.get("terms_ref"),
            "checked_at": authorization.get(f"{operation}_checked_at")
            or authorization.get("checked_at"),
        }
    return None


def _validate_authorization(
    authorization: object,
) -> tuple[bool, str | None]:
    """Return ``(authorized, failure_code)``; never performs any I/O."""
    if authorization is None:
        return False, "authorization_missing"
    if not isinstance(authorization, _AbcMapping):
        return False, "authorization_invalid"
    privacy = authorization.get("privacy_denied")
    if not isinstance(privacy, bool):
        return False, "authorization_invalid"
    if privacy:
        return False, "privacy_denied"
    for operation, code in (
        ("acquisition", "acquisition_not_authorized"),
        ("storage", "storage_not_authorized"),
    ):
        evidence = _operation_evidence(authorization, operation)
        if evidence is None:
            return False, "authorization_invalid"
        allowed = evidence.get("allowed")
        if not isinstance(allowed, bool):
            return False, "authorization_invalid"
        if not allowed:
            return False, code
        for field_name in ("terms_ref", "checked_at"):
            value = evidence.get(field_name)
            if not isinstance(value, str) or not value.strip():
                return False, "terms_evidence_missing"
    return True, None


# --------------------------------------------------------------------------- #
# Body classification
# --------------------------------------------------------------------------- #
def _localname(tag: object) -> str:
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1]


def _looks_like_html(body: bytes) -> bool:
    head = body[:1024].lstrip(b"\xef\xbb\xbf \t\r\n").lower()
    return head.startswith(b"<!doctype html") or head.startswith(b"<html")


def _article_has_body(article: ET.Element) -> bool:
    """True when a JATS ``article`` carries a non-empty ``body`` element."""
    for element in article.iter():
        if _localname(element.tag) == "body":
            if len(list(element)) > 0:
                return True
            if element.text and element.text.strip():
                return True
    return False


def classify_body(body: bytes, media_type: str | None) -> str:
    """Classify a bounded body as one of :data:`_BODY_KINDS`.

    A body is ``fulltext`` only when it parses as XML, contains a JATS
    ``<article>``, and that article has a non-empty ``<body>``. Everything else
    is an explicit non-success kind (never silently accepted).
    """
    if media_type is not None and media_type in ("text/html", "application/xhtml+xml"):
        return "html"
    if not body or not body.strip():
        return "empty"
    if _looks_like_html(body):
        return "html"
    if b"<!ENTITY" in body:
        # Defensive: refuse entity declarations rather than risk expansion.
        return "malformed"
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        return "malformed"
    articles = [
        element
        for element in root.iter()
        if _localname(element.tag) == "article"
    ]
    if not articles:
        return "metadata_only"
    if any(_article_has_body(article) for article in articles):
        return "fulltext"
    return "metadata_only"


# --------------------------------------------------------------------------- #
# Result assembly
# --------------------------------------------------------------------------- #
def _attempt_to_dict(attempt: object) -> dict[str, Any]:
    return {
        "attempt_no": getattr(attempt, "attempt_no", None),
        "requested_url": getattr(attempt, "requested_url", None),
        "requested_at": getattr(attempt, "requested_at", None),
        "completed_at": getattr(attempt, "completed_at", None),
        "status": getattr(attempt, "status", None),
        "body_bytes": getattr(attempt, "body_bytes", None),
        "body_sha256": getattr(attempt, "body_sha256", None),
        "outcome": getattr(attempt, "outcome", None),
        "media_type": getattr(attempt, "media_type", None),
    }


def _transport_caps(transport: object) -> dict[str, Any]:
    return {
        "max_requests": MAX_REQUESTS,
        "max_attempts": getattr(transport, "max_attempts", None),
        "timeout_seconds": getattr(transport, "timeout", None),
        "max_response_bytes": getattr(transport, "max_response_bytes", None),
        "rate_spacing_seconds": getattr(transport, "rate_spacing", None),
    }


def _base_result(
    *,
    status: str,
    normalized: dict[str, str],
    unofficial: list[dict[str, Any]],
    caps: dict[str, Any],
    authorization_checked: bool,
) -> dict[str, Any]:
    return {
        "status": status,
        "resolver_version": RESOLVER_VERSION,
        "identifiers": dict(normalized),
        "source": None,
        "source_version": None,
        "media_type": None,
        "origin_locator": None,
        "content": None,
        "content_encoding": None,
        "content_bytes": None,
        "content_sha256": None,
        "attempts": [],
        "failures": [],
        "caps": caps,
        "authorization_checked": authorization_checked,
        "unofficial_locator_present": bool(unofficial),
    }


# --------------------------------------------------------------------------- #
# The public entry point
# --------------------------------------------------------------------------- #
def resolve_full_text(
    *identifiers: Mapping[str, str],
    authorization: Mapping[str, Any],
    transport: object = None,
) -> dict[str, Any]:
    """Resolve one candidate's full text through the official XML waterfall.

    ``identifiers`` are one or more mappings of string keys to string values
    (for example ``{"pmcid": "PMC3257301"}`` or ``{"pmid": "31452104"}``). A
    ``pmcid`` is required to fetch; without one the result is
    ``manual_required``. Unsupported/publisher/unofficial locators are recorded
    and **never** fetched.

    ``authorization`` is the trusted caller's derived, validated RightsRecord
    eligibility for this operation (see the module docstring). A missing or
    non-affirmative authorization yields ``status="blocked"`` with **zero**
    network calls.

    ``transport`` is an optional object exposing ``get(url) -> RetrievalResult``
    (for example a :class:`~review_article_agent.acquisition.transport.FullTextTransport`
    or a deterministic fixture). When ``None`` a default
    :class:`FullTextTransport` is constructed (which performs no I/O).

    Returns a **JSON-safe** dict with ``status`` in
    :data:`RESOLUTION_STATUSES`, source/version provenance, the retained
    ``attempts`` and ``failures``, ``media_type``, ``origin_locator``, and the
    ``content`` (base64) **only** when ``status == "retrieved"``.
    """
    normalized, unofficial = _normalize_identifiers(identifiers)
    active_transport = transport if transport is not None else FullTextTransport()
    caps = _transport_caps(active_transport)

    authorized, code = _validate_authorization(authorization)
    if not authorized:
        result = _base_result(
            status="blocked",
            normalized=normalized,
            unofficial=unofficial,
            caps=caps,
            authorization_checked=False,
        )
        result["failures"].append({"code": code})
        return result

    result = _base_result(
        status="manual_required",
        normalized=normalized,
        unofficial=unofficial,
        caps=caps,
        authorization_checked=True,
    )
    failures: list[dict[str, Any]] = result["failures"]

    if unofficial:
        failures.append({"code": "unofficial_locator"})

    pmcid = normalized.get("pmcid")
    if not pmcid:
        failures.append({"code": "missing_pmcid"})
        return result
    if _PMCID_RE.fullmatch(pmcid) is None:
        failures.append({"code": "invalid_pmcid"})
        return result

    attempts: list[dict[str, Any]] = result["attempts"]
    for source, source_version, build_url in _STEPS:
        url = build_url(pmcid)
        retrieval = active_transport.get(url)
        for attempt in retrieval.attempts:
            attempts.append(_attempt_to_dict(attempt))
        if not retrieval.ok:
            failures.append(
                {
                    "source": source,
                    "code": retrieval.failure,
                    "status": retrieval.status,
                }
            )
            continue
        kind = classify_body(retrieval.body, retrieval.media_type)
        if kind != "fulltext":
            failures.append(
                {
                    "source": source,
                    "code": _KIND_FAILURE.get(kind, "metadata_only"),
                    "status": retrieval.status,
                    "media_type": retrieval.media_type,
                }
            )
            continue
        result["status"] = "retrieved"
        result["source"] = source
        result["source_version"] = source_version
        result["media_type"] = retrieval.media_type
        result["origin_locator"] = retrieval.requested_url
        result["content"] = base64.b64encode(retrieval.body).decode("ascii")
        result["content_encoding"] = "base64"
        result["content_bytes"] = retrieval.body_bytes
        result["content_sha256"] = retrieval.body_sha256 or hashlib.sha256(
            retrieval.body
        ).hexdigest()
        return result

    # No official source produced scientific full text: human disposition needed.
    return result
