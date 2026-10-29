"""Fail-closed, stdlib-only acquisition asset validation (Step 06, AC2/AC6/AC7).

This module owns **one bounded outcome**: given the exact bytes of a would-be
acquired source document, decide whether the *container* is recognisable and
whether its *exact* declared identity can be confirmed -- without ever reading
or interpreting scientific content.  It is deliberately pure byte validation:
it performs no network, no filesystem, no model, no storage, and no approval
call, and it never returns a result that authorises scientific digestion.

What it decides (``docs/implementation/06-acquisition.md`` AC2):

* A file extension (or a ``Content-Type`` header) **never** establishes success.
  A login/HTML response is rejected even when it is named ``.pdf`` or declared
  ``application/pdf``.
* A grossly corrupt PDF is rejected; a structurally plausible PDF stays
  ``needs_review`` with ``unknown`` scan/full-text/identity checks until a real
  bounded inspector (a later task) or an exact authenticated human review reads
  it.  A raw byte DOI inside a PDF is never promoted to identity.
* Malformed XML, XML with an internal subset / entity declarations, and
  external-DTD access attempts are rejected.  A benign publisher JATS
  ``DOCTYPE`` (external id, **no** internal subset) may be *safely removed* --
  no external resource is ever fetched -- and the article parsed.
* A JATS full article is parsed with bounded node count / depth / text; identity
  comes **only** from ``front/article-meta`` ``article-id`` elements (never
  bibliography ids) and must be *exactly consistent* with the caller's expected
  identifiers; a non-empty ``body`` is required.  Missing body/identity is
  ``needs_review``.  Identifier conflicts are rejected.
* The 32 MiB byte cap is an explicit rejection, never a silent truncation.

The result is a JSON-safe mapping (``schema_version`` 1).  It is an
**observation**, not an approval: ``authorizes_scientific_processing`` is always
``False``, and a ``ready`` status claims only container/structure/exact-identity
consistency -- never semantic correctness, completeness, or screen eligibility.

Only the standard library is used.  Identifier normalisation mirrors the
existing discovery convention (namespaced ``pmid`` / ``pmcid`` / ``doi`` tokens
compared exactly, no title/year fuzzing).
"""

from __future__ import annotations

import hashlib
import re
import xml.etree.ElementTree as ET
from collections.abc import Mapping

from ..domain.errors import ReviewAgentError

# --------------------------------------------------------------------------- #
# Public constants
# --------------------------------------------------------------------------- #
SCHEMA_VERSION = 1
DOCUMENT_TYPE = "acquisition_validation"
VALIDATOR_VERSION = "1"

#: Hard cap on the accepted byte length (32 MiB).  Crossing it is an explicit
#: rejection; the validator never truncates and never inspects a prefix.
MAX_BYTE_SIZE = 32 * 1024 * 1024

MAX_IDENTIFIER_CHARS = 256

#: Bounds applied to a parsed JATS tree (defence against hostile XML).
MAX_XML_NODES = 100_000
MAX_XML_DEPTH = 200
MAX_XML_TEXT_CHARS = 8 * 1024 * 1024

#: Windows used for the (gross-only) PDF header / trailer sniff.
PDF_HEADER_WINDOW = 1024
PDF_EOF_WINDOW = 2048

MEDIA_KINDS = ("pdf", "jats_xml", "html", "unknown")
STATUSES = ("ready", "needs_review", "rejected")
CHECK_NAMES = ("container", "structure", "full_text", "identity", "text_quality")
CHECK_STATES = ("pass", "fail", "unknown")
IDENTIFIER_NAMESPACES = ("pmid", "pmcid", "doi")

# --------------------------------------------------------------------------- #
# Static reason codes (never carry a value)
# --------------------------------------------------------------------------- #
REASON_EMPTY = "empty_input"
REASON_BYTE_CAP = "byte_cap_exceeded"
REASON_CONTENT_TYPE_MISMATCH = "content_type_mismatch"
REASON_UNRECOGNIZED = "unrecognized_container"

REASON_HTML = "html_not_an_article"
REASON_HTML_LOGIN = "html_login_page"

REASON_PDF_CORRUPT = "pdf_gross_corruption"
REASON_PDF_UNINSPECTED = "pdf_not_inspected"

REASON_XML_NOT_UTF8 = "xml_not_utf8"
REASON_XML_MALFORMED = "xml_malformed"
REASON_XML_UNDEFINED_ENTITY = "xml_undefined_entity"
REASON_XML_ENTITY = "xml_entity_declaration"
REASON_XML_INTERNAL_SUBSET = "xml_internal_subset"
REASON_XML_DOCTYPE_REJECTED = "xml_doctype_rejected"
REASON_XML_NODE_LIMIT = "xml_node_limit_exceeded"
REASON_XML_DEPTH_LIMIT = "xml_depth_limit_exceeded"
REASON_XML_TEXT_LIMIT = "xml_text_limit_exceeded"
REASON_XML_NOT_ARTICLE = "xml_not_jats_article"
REASON_JATS_DOCTYPE_REMOVED = "jats_doctype_removed"

REASON_JATS_STRUCTURE_INCOMPLETE = "jats_structure_incomplete"
REASON_JATS_BODY_MISSING = "jats_body_missing"
REASON_JATS_BODY_EMPTY = "jats_body_empty"
REASON_IDENTITY_MISMATCH = "identity_mismatch"
REASON_IDENTITY_UNCONFIRMED = "identity_unconfirmed"
REASON_IDENTIFIER_MALFORMED = "identifier_malformed"

REASONS = frozenset(
    {
        REASON_EMPTY,
        REASON_BYTE_CAP,
        REASON_CONTENT_TYPE_MISMATCH,
        REASON_UNRECOGNIZED,
        REASON_HTML,
        REASON_HTML_LOGIN,
        REASON_PDF_CORRUPT,
        REASON_PDF_UNINSPECTED,
        REASON_XML_NOT_UTF8,
        REASON_XML_MALFORMED,
        REASON_XML_UNDEFINED_ENTITY,
        REASON_XML_ENTITY,
        REASON_XML_INTERNAL_SUBSET,
        REASON_XML_DOCTYPE_REJECTED,
        REASON_XML_NODE_LIMIT,
        REASON_XML_DEPTH_LIMIT,
        REASON_XML_TEXT_LIMIT,
        REASON_XML_NOT_ARTICLE,
        REASON_JATS_DOCTYPE_REMOVED,
        REASON_JATS_STRUCTURE_INCOMPLETE,
        REASON_JATS_BODY_MISSING,
        REASON_JATS_BODY_EMPTY,
        REASON_IDENTITY_MISMATCH,
        REASON_IDENTITY_UNCONFIRMED,
        REASON_IDENTIFIER_MALFORMED,
    }
)

LIMITATIONS = (
    "Container/structure and exact-identifier validation only. This result does "
    "not read or interpret scientific content, does not assess scan or text "
    "completeness, and never authorises scientific digestion, screening, or "
    "eligibility. A 'ready' status means the container parsed and the exact "
    "declared identity is consistent; it still requires a bounded inspector or "
    "an exact authenticated human review before use."
)


class AcquisitionValidationError(ReviewAgentError, ValueError):
    """A validation *request* was malformed (bad argument types or shapes).

    This is a programming/caller error about the request, never a verdict about
    the asset bytes; content-level problems are reported through the returned
    mapping, not by raising.
    """


# --------------------------------------------------------------------------- #
# Identifier normalisation (mirrors the discovery namespaced-token convention)
# --------------------------------------------------------------------------- #
_PMID_RE = re.compile(r"^[0-9]{1,20}$")
_PMCID_RE = re.compile(r"^PMC[0-9]{1,20}$")
_DOI_RE = re.compile(r"^10\.[0-9]{4,9}/\S+$")

_DOI_PREFIXES = (
    "https://doi.org/",
    "http://doi.org/",
    "https://dx.doi.org/",
    "http://dx.doi.org/",
    "doi:",
)

_PUBID_TYPE_MAP = {
    "pmid": "pmid",
    "pmcid": "pmcid",
    "pmc": "pmcid",
    "doi": "doi",
}


def _normalize_identifier(namespace: str, value: object) -> str:
    """Return the canonical form of one exact identifier, or raise.

    The canonicalisation is deliberately conservative and value-exact: a DOI is
    lower-cased and stripped of a URL/``doi:`` prefix, a PMCID is upper-cased
    with a ``PMC`` prefix, a PMID stays a digit string.  No fuzzy/title matching
    is ever performed.  A value that does not match its namespace shape is a
    hard request error.
    """
    if namespace not in IDENTIFIER_NAMESPACES:
        raise AcquisitionValidationError(
            f"unsupported identifier namespace {namespace!r}"
        )
    if not isinstance(value, str):
        raise AcquisitionValidationError("identifier value must be a string")
    text = value.strip()
    if text == "":
        raise AcquisitionValidationError("identifier value must be non-empty")
    if len(text) > MAX_IDENTIFIER_CHARS:
        raise AcquisitionValidationError("identifier value is too long")
    for ch in text:
        if ord(ch) < 0x20 or ord(ch) == 0x7F:
            raise AcquisitionValidationError(
                "identifier value contains control characters"
            )

    if namespace == "pmid":
        lowered = text.lower()
        if lowered.startswith("pmid:"):
            text = text[5:].strip()
        if not _PMID_RE.match(text):
            raise AcquisitionValidationError("pmid is not a digit string")
        return text

    if namespace == "pmcid":
        lowered = text.lower()
        for prefix in ("pmcid:", "pmc:"):
            if lowered.startswith(prefix):
                text = text[len(prefix):].strip()
                break
        if not text.lower().startswith("pmc"):
            text = "PMC" + text
        upper = text.upper()
        if not _PMCID_RE.match(upper):
            raise AcquisitionValidationError("pmcid is not a valid PMC id")
        return upper

    # doi
    lowered = text.lower()
    for prefix in _DOI_PREFIXES:
        if lowered.startswith(prefix):
            text = text[len(prefix):].strip()
            break
    text = text.lower()
    if not _DOI_RE.match(text):
        raise AcquisitionValidationError("doi is not a valid DOI")
    return text


# --------------------------------------------------------------------------- #
# Container sniffing (head-only, bounded)
# --------------------------------------------------------------------------- #
def _decode_head(data: bytes, limit: int = 4096) -> str:
    return data[:limit].decode("utf-8", errors="replace")


def _looks_like_html(head: str) -> bool:
    text = head.lstrip().lower()
    if text.startswith("<!doctype html"):
        return True
    if text.startswith("<html"):
        return True
    if text.startswith("<?xml") and "<html" in text[:1024]:
        return True
    return False


def _looks_like_xml(head: str) -> bool:
    text = head.lstrip().lower()
    if text.startswith("<?xml"):
        return True
    if text.startswith("<!doctype"):
        return True
    if len(text) >= 2 and text[0] == "<" and (text[1].isalpha() or text[1] in "_:"):
        return True
    return False


def _looks_like_jats(data: bytes) -> bool:
    head = data[:8192].lower()
    return b"<article" in head or b"jats" in head


def _looks_like_login(head: str) -> bool:
    text = head.lower()
    if "password" in text and (
        "sign in" in text or "log in" in text or "login" in text
    ):
        return True
    if 'type="password"' in text or "type='password'" in text:
        return True
    return False


def _sniff_kind(data: bytes) -> str:
    """Best-effort media kind from leading bytes only (no parsing)."""
    if b"%PDF-" in data[:PDF_HEADER_WINDOW]:
        return "pdf"
    head = _decode_head(data)
    if _looks_like_html(head):
        return "html"
    if _looks_like_xml(head):
        return "jats_xml" if _looks_like_jats(data) else "unknown"
    return "unknown"


def _pdf_gross_ok(data: bytes) -> bool:
    """A *gross* structural sanity check for a PDF, not a real parser.

    Requires a ``%PDF-<digit>`` header, an ``%%EOF`` trailer marker near the
    end, and at least one indirect object.  This rejects gross corruption /
    truncation; it makes **no** claim that the PDF is valid or readable.
    """
    header = data[:PDF_HEADER_WINDOW].find(b"%PDF-")
    if header == -1:
        return False
    version_at = header + 5
    if version_at >= len(data) or not data[version_at:version_at + 1].isdigit():
        return False
    if b"%%EOF" not in data[-PDF_EOF_WINDOW:]:
        return False
    if b"obj" not in data:
        return False
    return True


def _declared_group(declared_media_type: str | None) -> str | None:
    if declared_media_type is None:
        return None
    text = declared_media_type.lower()
    if "pdf" in text:
        return "pdf"
    if "html" in text:
        return "html"
    if "xml" in text:
        return "xml"
    return None


# --------------------------------------------------------------------------- #
# XML preparation (DOCTYPE / entity defences)
# --------------------------------------------------------------------------- #
def _find_doctype(text: str):
    """Locate the prolog ``<!DOCTYPE ...>`` declaration, if any.

    Returns ``(start, end, declaration)`` or ``None``.  A ``<!DOCTYPE`` that
    sits inside a comment is ignored.  The scan respects quotes and internal
    subset brackets so a declaration is never cut short.
    """
    idx = text.find("<!DOCTYPE")
    if idx == -1:
        return None
    comment = text.find("<!--")
    if comment != -1 and comment < idx:
        close = text.find("-->", comment)
        if close == -1 or close > idx:
            return None
    i = idx
    quote = None
    depth = 0
    end = -1
    while i < len(text):
        ch = text[i]
        if quote is not None:
            if ch == quote:
                quote = None
        elif ch in ('"', "'"):
            quote = ch
        elif ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
        elif ch == ">" and depth == 0:
            end = i + 1
            break
        i += 1
    if end == -1:
        return (idx, len(text), text[idx:])
    return (idx, end, text[idx:end])


def _prepare_xml(text: str):
    """Return ``(text, doctype_removed, reason_or_None)``.

    A benign publisher ``DOCTYPE`` with **no** internal subset is removed (no
    external resource is ever fetched).  Any internal subset, entity
    declaration, or a second ``DOCTYPE`` is refused.
    """
    removed = False
    loc = _find_doctype(text)
    if loc is not None:
        idx, end, declaration = loc
        if "[" in declaration:
            if "<!ENTITY" in declaration:
                return text, removed, REASON_XML_ENTITY
            return text, removed, REASON_XML_INTERNAL_SUBSET
        text = text[:idx] + text[end:]
        removed = True
    if "<!ENTITY" in text:
        return text, removed, REASON_XML_ENTITY
    if "<!DOCTYPE" in text:
        return text, removed, REASON_XML_DOCTYPE_REJECTED
    return text, removed, None


# --------------------------------------------------------------------------- #
# XML tree traversal / identity / body
# --------------------------------------------------------------------------- #
def _local(tag: object) -> str:
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1]


def _measure(root) -> tuple[int, int, int, str | None]:
    """Bounded iterative traversal: (nodes, max_depth, text_chars, breach)."""
    nodes = 0
    max_depth = 0
    text_chars = 0
    stack = [(root, 1)]
    while stack:
        element, depth = stack.pop()
        nodes += 1
        if nodes > MAX_XML_NODES:
            return nodes, max_depth, text_chars, "nodes"
        if depth > max_depth:
            max_depth = depth
            if max_depth > MAX_XML_DEPTH:
                return nodes, max_depth, text_chars, "depth"
        if element.text:
            text_chars += len(element.text)
        if element.tail:
            text_chars += len(element.tail)
        if text_chars > MAX_XML_TEXT_CHARS:
            return nodes, max_depth, text_chars, "text"
        for child in element:
            stack.append((child, depth + 1))
    return nodes, max_depth, text_chars, None


def _first_descendant(element, name: str):
    for node in element.iter():
        if _local(node.tag) == name:
            return node
    return None


def _extract_identifiers(root):
    """Front-matter article ids only; bibliography ids are never read.

    Returns ``(observed, malformed, has_article_meta)`` where ``observed`` maps
    a namespace to its canonical value.
    """
    observed: dict[str, str] = {}
    malformed = False
    front = _first_descendant(root, "front")
    if front is None:
        return observed, malformed, False
    article_meta = _first_descendant(front, "article-meta")
    if article_meta is None:
        return observed, malformed, False
    for element in article_meta.iter():
        if _local(element.tag) != "article-id":
            continue
        pub_id_type = element.get("pub-id-type")
        if not isinstance(pub_id_type, str):
            continue
        namespace = _PUBID_TYPE_MAP.get(pub_id_type.strip().lower())
        if namespace is None:
            continue
        raw = (element.text or "").strip()
        if raw == "":
            continue
        try:
            observed[namespace] = _normalize_identifier(namespace, raw)
        except AcquisitionValidationError:
            malformed = True
    return observed, malformed, True


def _body_text_length(root) -> int | None:
    """Non-whitespace text length inside ``body``, or ``None`` when absent."""
    body = _first_descendant(root, "body")
    if body is None:
        return None
    total = 0
    for element in body.iter():
        if element.text:
            total += len(element.text.strip())
        if element.tail:
            total += len(element.tail.strip())
    return total


# --------------------------------------------------------------------------- #
# Result assembly
# --------------------------------------------------------------------------- #
def _dedupe(reasons) -> list:
    seen: set[str] = set()
    ordered: list[str] = []
    for reason in reasons:
        if reason not in seen:
            seen.add(reason)
            ordered.append(reason)
    return ordered


def _result(
    *,
    kind: str,
    status: str,
    checks: dict,
    observed: dict,
    reasons: list,
    digest: str,
    byte_size: int,
) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "document_type": DOCUMENT_TYPE,
        "validator_version": VALIDATOR_VERSION,
        "sha256": digest,
        "byte_size": byte_size,
        "media_kind": kind,
        "status": status,
        "checks": {name: checks[name] for name in CHECK_NAMES},
        "observed_identifiers": {
            ns: observed[ns] for ns in IDENTIFIER_NAMESPACES if ns in observed
        },
        "reasons": _dedupe(reasons),
        "authorizes_scientific_processing": False,
        "limitations": LIMITATIONS,
    }


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #
def validate_asset(
    data: bytes,
    *,
    expected_identifiers: Mapping[str, str],
    declared_media_type: str | None = None,
) -> dict:
    """Validate one acquisition asset's container/structure and exact identity.

    ``data`` is the exact bytes; ``expected_identifiers`` maps a namespace
    (``pmid`` / ``pmcid`` / ``doi``) to its expected value; ``declared_media_type``
    is an advisory ``Content-Type`` that never overrides content sniffing.

    Returns a JSON-safe mapping (see the module docstring).  Raises
    :class:`AcquisitionValidationError` only when the *request* is malformed.
    """
    if isinstance(data, (bytearray, memoryview)):
        data = bytes(data)
    if not isinstance(data, bytes):
        raise AcquisitionValidationError("data must be bytes")
    if not isinstance(expected_identifiers, Mapping):
        raise AcquisitionValidationError(
            "expected_identifiers must be a mapping"
        )
    expected: dict[str, str] = {}
    for key, value in expected_identifiers.items():
        if not isinstance(key, str):
            raise AcquisitionValidationError(
                "expected_identifiers keys must be strings"
            )
        namespace = key.strip().lower()
        if namespace not in IDENTIFIER_NAMESPACES:
            raise AcquisitionValidationError(
                f"unsupported expected identifier namespace {key!r}"
            )
        expected[namespace] = _normalize_identifier(namespace, value)
    if declared_media_type is not None and not isinstance(
        declared_media_type, str
    ):
        raise AcquisitionValidationError(
            "declared_media_type must be a string or None"
        )

    digest = hashlib.sha256(data).hexdigest()
    byte_size = len(data)
    reasons: list[str] = []
    checks = {name: "unknown" for name in CHECK_NAMES}
    observed: dict[str, str] = {}
    rejected = False
    force_review = False
    kind = "unknown"

    # -- hard size guards (explicit, never truncating) --------------------- #
    if byte_size == 0:
        checks["container"] = "fail"
        checks["structure"] = "fail"
        checks["full_text"] = "fail"
        reasons.append(REASON_EMPTY)
        return _result(
            kind="unknown",
            status="rejected",
            checks=checks,
            observed=observed,
            reasons=reasons,
            digest=digest,
            byte_size=byte_size,
        )

    if byte_size > MAX_BYTE_SIZE:
        kind = _sniff_kind(data)
        checks["container"] = "fail"
        reasons.append(REASON_BYTE_CAP)
        return _result(
            kind=kind,
            status="rejected",
            checks=checks,
            observed=observed,
            reasons=reasons,
            digest=digest,
            byte_size=byte_size,
        )

    # -- content sniff (extension / declared type never decide) ------------ #
    kind = _sniff_kind(data)
    head = _decode_head(data)
    declared_group = _declared_group(declared_media_type)
    actual_group = {
        "pdf": "pdf",
        "html": "html",
        "jats_xml": "xml",
        "unknown": "unknown",
    }[kind]
    if declared_group is not None and declared_group != actual_group:
        reasons.append(REASON_CONTENT_TYPE_MISMATCH)
        force_review = True

    if kind == "pdf":
        if _pdf_gross_ok(data):
            checks["container"] = "pass"
            reasons.append(REASON_PDF_UNINSPECTED)
        else:
            checks["container"] = "fail"
            reasons.append(REASON_PDF_CORRUPT)
            rejected = True

    elif kind == "html":
        checks["container"] = "fail"
        checks["structure"] = "fail"
        checks["full_text"] = "fail"
        checks["text_quality"] = "fail"
        reasons.append(REASON_HTML)
        if _looks_like_login(_decode_head(data, 8192)):
            reasons.append(REASON_HTML_LOGIN)
        rejected = True

    elif _looks_like_xml(head):
        return _xml_result(
            data=data,
            digest=digest,
            byte_size=byte_size,
            expected=expected,
            reasons=reasons,
            force_review=force_review,
        )

    else:
        checks["container"] = "fail"
        reasons.append(REASON_UNRECOGNIZED)

    status = "rejected" if rejected else (
        "needs_review" if force_review else (
            "ready" if all(v == "pass" for v in checks.values())
            else "needs_review"
        )
    )
    return _result(
        kind=kind,
        status=status,
        checks=checks,
        observed=observed,
        reasons=reasons,
        digest=digest,
        byte_size=byte_size,
    )


def _xml_result(
    *,
    data: bytes,
    digest: str,
    byte_size: int,
    expected: dict,
    reasons: list,
    force_review: bool,
) -> dict:
    """Full XML/JATS branch; returns the completed result mapping."""
    checks = {name: "unknown" for name in CHECK_NAMES}
    observed: dict[str, str] = {}
    rejected = False
    kind = "jats_xml" if _looks_like_jats(data) else "unknown"

    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        checks["container"] = "fail"
        checks["structure"] = "fail"
        checks["full_text"] = "fail"
        reasons.append(REASON_XML_NOT_UTF8)
        return _result(
            kind=kind,
            status="rejected",
            checks=checks,
            observed=observed,
            reasons=reasons,
            digest=digest,
            byte_size=byte_size,
        )
    if text.startswith("\ufeff"):
        text = text[1:]

    text, doctype_removed, refusal = _prepare_xml(text)
    if refusal is not None:
        checks["container"] = "fail"
        checks["structure"] = "fail"
        checks["full_text"] = "fail"
        reasons.append(refusal)
        return _result(
            kind=kind,
            status="rejected",
            checks=checks,
            observed=observed,
            reasons=reasons,
            digest=digest,
            byte_size=byte_size,
        )
    if doctype_removed:
        reasons.append(REASON_JATS_DOCTYPE_REMOVED)

    try:
        root = ET.fromstring(text.encode("utf-8"))
    except ET.ParseError as exc:
        low = str(exc).lower()
        checks["container"] = "fail"
        checks["structure"] = "fail"
        checks["full_text"] = "fail"
        if "undefined entity" in low:
            reasons.append(REASON_XML_UNDEFINED_ENTITY)
        else:
            reasons.append(REASON_XML_MALFORMED)
        return _result(
            kind=kind,
            status="rejected",
            checks=checks,
            observed=observed,
            reasons=reasons,
            digest=digest,
            byte_size=byte_size,
        )

    _nodes, _depth, _text, breach = _measure(root)
    if breach == "nodes":
        checks["container"] = "fail"
        checks["structure"] = "fail"
        reasons.append(REASON_XML_NODE_LIMIT)
        return _result(
            kind=kind,
            status="rejected",
            checks=checks,
            observed=observed,
            reasons=reasons,
            digest=digest,
            byte_size=byte_size,
        )
    if breach == "depth":
        checks["container"] = "fail"
        checks["structure"] = "fail"
        reasons.append(REASON_XML_DEPTH_LIMIT)
        return _result(
            kind=kind,
            status="rejected",
            checks=checks,
            observed=observed,
            reasons=reasons,
            digest=digest,
            byte_size=byte_size,
        )
    if breach == "text":
        checks["container"] = "fail"
        checks["structure"] = "fail"
        reasons.append(REASON_XML_TEXT_LIMIT)
        return _result(
            kind=kind,
            status="rejected",
            checks=checks,
            observed=observed,
            reasons=reasons,
            digest=digest,
            byte_size=byte_size,
        )

    if _local(root.tag) != "article":
        checks["container"] = "pass"
        checks["structure"] = "fail"
        reasons.append(REASON_XML_NOT_ARTICLE)
        return _result(
            kind="unknown",
            status="needs_review",
            checks=checks,
            observed=observed,
            reasons=reasons,
            digest=digest,
            byte_size=byte_size,
        )

    kind = "jats_xml"
    checks["container"] = "pass"

    observed, malformed, has_meta = _extract_identifiers(root)
    body_len = _body_text_length(root)

    if has_meta and body_len is not None:
        checks["structure"] = "pass"
    else:
        checks["structure"] = "fail"
        if not has_meta:
            reasons.append(REASON_JATS_STRUCTURE_INCOMPLETE)
        if body_len is None:
            reasons.append(REASON_JATS_BODY_MISSING)

    if body_len is None:
        checks["full_text"] = "fail"
        checks["text_quality"] = "fail"
    elif body_len > 0:
        checks["full_text"] = "pass"
        checks["text_quality"] = "pass"
    else:
        checks["full_text"] = "fail"
        checks["text_quality"] = "fail"
        reasons.append(REASON_JATS_BODY_EMPTY)

    conflicts = [
        ns for ns in expected if ns in observed and observed[ns] != expected[ns]
    ]
    if conflicts:
        checks["identity"] = "fail"
        reasons.append(REASON_IDENTITY_MISMATCH)
        rejected = True
    elif malformed:
        checks["identity"] = "unknown"
        reasons.append(REASON_IDENTIFIER_MALFORMED)
    elif not expected:
        checks["identity"] = "unknown"
        reasons.append(REASON_IDENTITY_UNCONFIRMED)
    else:
        missing = [ns for ns in expected if ns not in observed]
        if missing:
            checks["identity"] = "unknown"
            reasons.append(REASON_IDENTITY_UNCONFIRMED)
        else:
            checks["identity"] = "pass"

    if rejected:
        status = "rejected"
    elif force_review:
        status = "needs_review"
    elif all(value == "pass" for value in checks.values()):
        status = "ready"
    else:
        status = "needs_review"

    return _result(
        kind=kind,
        status=status,
        checks=checks,
        observed=observed,
        reasons=reasons,
        digest=digest,
        byte_size=byte_size,
    )
