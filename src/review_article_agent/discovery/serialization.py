"""Versioned, strict, lossless codec for discovery search results (Step 05).

Authority
---------

``docs/implementation/05-discovery.md`` D3/AC3 ("Reconstruct exact queries,
timestamps, contributing sources, failures, counts, and deduplication decisions
from records") and AC4 (fixed fixtures for pagination/overlap/missing IDs/partial
failure), ``docs/specification.md`` workflow steps 2-5 and the untrusted-data
safety invariant, and ``docs/contracts/03-policy-decisions.md``
``POL-OPS-COVERAGE`` (explicit provenance; no universal completeness claim).

What this module is
-------------------

* An **inert** byte codec for the existing immutable discovery values
  (:class:`~review_article_agent.discovery.records.SearchResult`, its
  :class:`~review_article_agent.discovery.pubmed.PubMedSearchResult` subtype,
  pages, metadata records, and attempt provenance). It serializes exactly the
  observations already present; it never re-fetches, re-parses, deduplicates,
  or manufactures one.
* **Versioned and canonical.** :func:`encode_search_result` emits canonical
  (sorted-key, compact, UTF-8) JSON bytes with an explicit
  :data:`SEARCH_RESULT_FORMAT` tag and :data:`SEARCH_RESULT_FORMAT_VERSION`.
  The same logical value always yields the same bytes, so a later private store
  can hash them as an immutable revision key.
* **Strict on decode.** :func:`decode_search_result` rejects invalid UTF-8,
  malformed JSON, duplicate object keys, non-finite numbers (including numeric
  overflow such as ``1e999``), over-deep nesting, unknown or missing fields,
  unknown format versions/types, wrong JSON types, and values that violate the
  existing dataclass invariants. It reconstructs the exact concrete classes
  through their real constructors, so decoding cannot bypass ``__post_init__``.
* **Lossless for the documented values.** Exact query/source strings, UTC
  timestamps, URLs, counts, page order, overlapping/duplicate records, missing
  (``None``) identifiers, explicit failures/stop reasons, every attempt, and
  the PubMed ``query_translation`` are preserved byte-for-byte semantically.
  ``retry_after_seconds`` keeps its original ``int``/``float`` distinction.

What this module is **not**
---------------------------

* **Not context, study identity, eligibility, approval, or a completeness
  claim.** These bytes are inert provenance. A decoded result is untrusted
  provider metadata, exactly like a freshly parsed one; it is never accepted
  project context. Overlapping records stay visible and are not deduplicated.
* **Not a store.** It defines no path, schema, index, or durability policy; a
  later private storage task owns those.
* **Not a scientific threshold.** The byte bound below is a technical resource
  bound, chosen from the adapters' documented retrieval envelopes; crossing it
  is an explicit refusal, never a silent truncation.

Format (version 1)
------------------

An envelope object with exactly ``format``, ``format_version``, ``result_type``
and ``result``. ``result_type`` is ``search_result`` or
``pubmed_search_result`` (the latter carries ``query_translation``). The
``result`` object carries exactly the ``SearchResult`` fields, each page exactly
the ``SearchPage`` fields, each record exactly the ``MetadataRecord`` fields,
and each attempt exactly the ``AttemptRecord`` fields. Arrays are JSON arrays;
the codec rebuilds them as tuples. ``result.records``/``result.attempts`` are
retained even though they are derivable from the pages, so the codec can reject
a record whose aggregate disagrees with its pages rather than silently
re-deriving (or dropping) a stored observation.

Bounds
------

:data:`MAX_SEARCH_RESULT_BYTES` is 512 MiB. Rationale: the adapters' documented
retrieval envelopes are 20,000 PubMed records (``MAX_TOTAL_RECORDS``) and up to
1,000,000 Europe PMC *lite* records (``MAX_PAGE_SIZE`` 1000 x ``MAX_MAX_PAGES``
1000). At 512 MiB the PubMed maximum has ~26 KiB per record, enough for a
maximal 4,096-character title even at UTF-8's 4 bytes/character plus every
other field and attempt overhead; the Europe PMC envelope has ~537 bytes per
record, which covers the documented lite shape (short native/PubMed/PMC ids, a
DOI, an ordinary title, year, and per-page provenance). The *absolute* worst
case -- a maximal multi-byte title on every one of a million records, roughly
16 GiB of title bytes alone -- is intentionally refused rather than admitted by
an unbounded in-memory parser; no part of it is ever truncated, dropped, or
re-fetched to fit. The bound is enforced before parsing and after encoding.
"""

from __future__ import annotations

import json
import math
from typing import Any

from ..domain.errors import ReviewAgentError
from ..domain.records import canonical_json
from .pubmed import PubMedSearchResult
from .records import MetadataRecord, SearchPage, SearchResult
from .transport import AttemptRecord

__all__ = [
    "SEARCH_RESULT_FORMAT",
    "SEARCH_RESULT_FORMAT_VERSION",
    "MAX_SEARCH_RESULT_BYTES",
    "DiscoverySerializationError",
    "encode_search_result",
    "decode_search_result",
]

#: Stable envelope tag; a different family is never decoded as this record.
SEARCH_RESULT_FORMAT = "review_article_agent.discovery.search_result"

#: Version of the record representation below (independent of any store schema).
SEARCH_RESULT_FORMAT_VERSION = 1

#: Technical upper bound on one encoded/decode payload. See the module docstring
#: for the documented-adapter rationale; this is not a scientific threshold.
MAX_SEARCH_RESULT_BYTES = 536_870_912  # 512 MiB

#: The fixed schema nesting is at most ~7 levels; this generous technical bound
#: only stops a hostile document from making a recursive walk expensive.
_MAX_JSON_DEPTH = 16

_RESULT_TYPE_SEARCH = "search_result"
_RESULT_TYPE_PUBMED = "pubmed_search_result"
_RESULT_TYPES = (_RESULT_TYPE_SEARCH, _RESULT_TYPE_PUBMED)

_ENVELOPE_KEYS = frozenset({"format", "format_version", "result_type", "result"})
_RESULT_KEYS = frozenset(
    {
        "source",
        "query",
        "pages",
        "records",
        "started_at",
        "completed_at",
        "stop_reason",
        "total_count",
        "complete",
        "failure",
        "attempts",
    }
)
_PUBMED_RESULT_KEYS = _RESULT_KEYS | {"query_translation"}
_PAGE_KEYS = frozenset(
    {
        "source",
        "query",
        "requested_url",
        "retrieved_at",
        "page_index",
        "records",
        "page_size",
        "total_count",
        "next_cursor",
        "complete",
        "stop_reason",
        "failure",
        "attempts",
    }
)
_RECORD_KEYS = frozenset(
    {
        "source",
        "source_id",
        "pmid",
        "pmcid",
        "doi",
        "title",
        "publication_year",
    }
)
_ATTEMPT_KEYS = frozenset(
    {
        "attempt_no",
        "requested_url",
        "requested_at",
        "completed_at",
        "status",
        "body_bytes",
        "body_sha256",
        "outcome",
        "retry_after_seconds",
    }
)


# --------------------------------------------------------------------------- #
# Error (static, value-free messages only; never peer text or a payload)
# --------------------------------------------------------------------------- #
class DiscoverySerializationError(ReviewAgentError, ValueError):
    """A search-result record could not be encoded or decoded.

    Messages are static and value-free: they never echo a query, URL, title,
    identifier, or any other payload text, so a validation failure is safe to
    log next to private discovery records.
    """


# --------------------------------------------------------------------------- #
# Strict JSON decoding
# --------------------------------------------------------------------------- #
class _StrictJsonError(Exception):
    """Internal marker for a rejected JSON lexical/document feature."""


class _DuplicateKey(_StrictJsonError):
    pass


class _NonFiniteNumber(_StrictJsonError):
    pass


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    seen: set[str] = set()
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in seen:
            raise _DuplicateKey()
        seen.add(key)
        out[key] = value
    return out


def _reject_constant(_token: str) -> Any:
    raise _NonFiniteNumber()


def _strict_float(token: str) -> float:
    value = float(token)
    if not math.isfinite(value):
        # ``parse_constant`` only sees NaN/Infinity tokens; ``1e999`` silently
        # becomes inf under the default parser.
        raise _NonFiniteNumber()
    return value


def _check_depth(value: Any) -> None:
    """Iteratively refuse JSON nested past :data:`_MAX_JSON_DEPTH`."""
    stack: list[tuple[Any, int]] = [(value, 0)]
    while stack:
        node, depth = stack.pop()
        if depth > _MAX_JSON_DEPTH:
            raise DiscoverySerializationError(
                "search result record nests too deeply"
            )
        if isinstance(node, dict):
            for item in node.values():
                stack.append((item, depth + 1))
        elif isinstance(node, list):
            for item in node:
                stack.append((item, depth + 1))


def _load_strict_json(data: object) -> Any:
    if not isinstance(data, (bytes, bytearray)):
        raise DiscoverySerializationError(
            "search result record must be bytes"
        )
    raw = bytes(data)
    if len(raw) == 0 or len(raw) > MAX_SEARCH_RESULT_BYTES:
        raise DiscoverySerializationError(
            "search result record is outside the permitted byte size"
        )
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise DiscoverySerializationError(
            "search result record is not valid UTF-8"
        ) from exc
    try:
        parsed = json.loads(
            text,
            object_pairs_hook=_no_duplicate_keys,
            parse_constant=_reject_constant,
            parse_float=_strict_float,
        )
    except _DuplicateKey as exc:
        raise DiscoverySerializationError(
            "search result record contains a duplicate JSON key"
        ) from exc
    except _NonFiniteNumber as exc:
        raise DiscoverySerializationError(
            "search result record contains a non-finite number"
        ) from exc
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise DiscoverySerializationError(
            "search result record is not valid JSON"
        ) from exc
    _check_depth(parsed)
    return parsed


# --------------------------------------------------------------------------- #
# Exact-type helpers (decode: no coercion; encode: no silent subclass loss)
# --------------------------------------------------------------------------- #
def _mapping(value: object, keys: frozenset[str]) -> dict[str, Any]:
    if type(value) is not dict:
        raise DiscoverySerializationError(
            "search result field is not a JSON object"
        )
    if set(value.keys()) != keys:
        # Deliberately does not echo an unknown key: it is payload text.
        raise DiscoverySerializationError(
            "search result object has unknown or missing fields"
        )
    return value


def _list(value: object) -> list[Any]:
    if type(value) is not list:
        raise DiscoverySerializationError(
            "search result field is not a JSON array"
        )
    return value


def _str(value: object) -> str:
    if type(value) is not str:
        raise DiscoverySerializationError(
            "search result field has the wrong type"
        )
    for char in value:
        if 0xD800 <= ord(char) <= 0xDFFF:
            # JSON can encode a lone surrogate escape; such a Python string is
            # not portably encodable, so it is refused rather than returned in a
            # state that a later canonical encode would reject.
            raise DiscoverySerializationError(
                "search result field contains an invalid Unicode surrogate"
            )
    return value


def _opt_str(value: object) -> str | None:
    if value is None:
        return None
    return _str(value)


def _int(value: object) -> int:
    if type(value) is not int:
        raise DiscoverySerializationError(
            "search result field has the wrong type"
        )
    return value


def _opt_int(value: object) -> int | None:
    if value is None:
        return None
    return _int(value)


def _bool(value: object) -> bool:
    if type(value) is not bool:
        raise DiscoverySerializationError(
            "search result field has the wrong type"
        )
    return value


def _opt_number(value: object) -> int | float | None:
    if value is None:
        return None
    if type(value) is int:
        try:
            finite = math.isfinite(float(value))
        except OverflowError as exc:
            raise DiscoverySerializationError(
                "search result field contains a non-finite number"
            ) from exc
        if not finite:
            raise DiscoverySerializationError(
                "search result field contains a non-finite number"
            )
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise DiscoverySerializationError(
                "search result field contains a non-finite number"
            )
        return value
    raise DiscoverySerializationError(
        "search result field has the wrong type"
    )


# --------------------------------------------------------------------------- #
# Encode
# --------------------------------------------------------------------------- #
def _encode_record(record: object) -> dict[str, Any]:
    if type(record) is not MetadataRecord:
        raise DiscoverySerializationError(
            "search result contains an unsupported metadata record type"
        )
    return {
        "source": _str(record.source),
        "source_id": _opt_str(record.source_id),
        "pmid": _opt_str(record.pmid),
        "pmcid": _opt_str(record.pmcid),
        "doi": _opt_str(record.doi),
        "title": _opt_str(record.title),
        "publication_year": _opt_int(record.publication_year),
    }


def _encode_attempt(attempt: object) -> dict[str, Any]:
    if type(attempt) is not AttemptRecord:
        raise DiscoverySerializationError(
            "search result contains an unsupported attempt record type"
        )
    return {
        "attempt_no": _int(attempt.attempt_no),
        "requested_url": _str(attempt.requested_url),
        "requested_at": _str(attempt.requested_at),
        "completed_at": _str(attempt.completed_at),
        "status": _opt_int(attempt.status),
        "body_bytes": _int(attempt.body_bytes),
        "body_sha256": _opt_str(attempt.body_sha256),
        "outcome": _str(attempt.outcome),
        "retry_after_seconds": _opt_number(attempt.retry_after_seconds),
    }


def _encode_record_tuple(records: object) -> list[dict[str, Any]]:
    if type(records) is not tuple:
        raise DiscoverySerializationError(
            "search result records must be an immutable tuple"
        )
    return [_encode_record(record) for record in records]


def _encode_attempt_tuple(attempts: object) -> list[dict[str, Any]]:
    if type(attempts) is not tuple:
        raise DiscoverySerializationError(
            "search result attempts must be an immutable tuple"
        )
    return [_encode_attempt(attempt) for attempt in attempts]


def _encode_page(page: object) -> dict[str, Any]:
    if type(page) is not SearchPage:
        raise DiscoverySerializationError(
            "search result contains an unsupported page type"
        )
    return {
        "source": _str(page.source),
        "query": _str(page.query),
        "requested_url": _str(page.requested_url),
        "retrieved_at": _str(page.retrieved_at),
        "page_index": _int(page.page_index),
        "records": _encode_record_tuple(page.records),
        "page_size": _opt_int(page.page_size),
        "total_count": _opt_int(page.total_count),
        "next_cursor": _opt_str(page.next_cursor),
        "complete": _bool(page.complete),
        "stop_reason": _opt_str(page.stop_reason),
        "failure": _opt_str(page.failure),
        "attempts": _encode_attempt_tuple(page.attempts),
    }


def _encode_result_body(result: SearchResult, *, pubmed: bool) -> dict[str, Any]:
    if type(result.pages) is not tuple:
        raise DiscoverySerializationError(
            "search result pages must be an immutable tuple"
        )
    body: dict[str, Any] = {
        "source": _str(result.source),
        "query": _str(result.query),
        "pages": [_encode_page(page) for page in result.pages],
        "records": _encode_record_tuple(result.records),
        "started_at": _str(result.started_at),
        "completed_at": _str(result.completed_at),
        "stop_reason": _str(result.stop_reason),
        "total_count": _opt_int(result.total_count),
        "complete": _bool(result.complete),
        "failure": _opt_str(result.failure),
        "attempts": _encode_attempt_tuple(result.attempts),
    }
    if pubmed:
        # ``PubMedSearchResult`` does not itself constrain this informational
        # field; the codec's type contract is ``str | None`` and never coerces.
        body["query_translation"] = _opt_str(result.query_translation)
    return body


def encode_search_result(result: SearchResult) -> bytes:
    """Encode one exact ``SearchResult``/``PubMedSearchResult`` as canonical bytes.

    Only the exact base class and the exact PubMed subtype are accepted; any
    other subclass is refused rather than silently flattened to its base fields.
    The returned bytes are deterministic, versioned, and at most
    :data:`MAX_SEARCH_RESULT_BYTES`. Encoding is inert: it never validates
    anything scientific, never re-fetches, and never mutates ``result``.
    """
    if type(result) is PubMedSearchResult:
        pubmed = True
        result_type = _RESULT_TYPE_PUBMED
    elif type(result) is SearchResult:
        pubmed = False
        result_type = _RESULT_TYPE_SEARCH
    else:
        raise DiscoverySerializationError(
            "search result is not a supported concrete result type"
        )

    try:
        envelope = {
            "format": SEARCH_RESULT_FORMAT,
            "format_version": SEARCH_RESULT_FORMAT_VERSION,
            "result_type": result_type,
            "result": _encode_result_body(result, pubmed=pubmed),
        }
        payload = canonical_json(envelope)
    except DiscoverySerializationError:
        raise
    except (
        ReviewAgentError,
        ValueError,
        TypeError,
        UnicodeError,
        RecursionError,
    ) as exc:
        raise DiscoverySerializationError(
            "search result cannot be encoded as canonical JSON"
        ) from exc

    if len(payload) > MAX_SEARCH_RESULT_BYTES:
        raise DiscoverySerializationError(
            "search result record is outside the permitted byte size"
        )
    return payload


# --------------------------------------------------------------------------- #
# Decode
# --------------------------------------------------------------------------- #
def _decode_record(value: object) -> MetadataRecord:
    obj = _mapping(value, _RECORD_KEYS)
    try:
        return MetadataRecord(
            source=_str(obj["source"]),
            source_id=_opt_str(obj["source_id"]),
            pmid=_opt_str(obj["pmid"]),
            pmcid=_opt_str(obj["pmcid"]),
            doi=_opt_str(obj["doi"]),
            title=_opt_str(obj["title"]),
            publication_year=_opt_int(obj["publication_year"]),
        )
    except DiscoverySerializationError:
        raise
    except (ReviewAgentError, TypeError, ValueError, OverflowError) as exc:
        raise DiscoverySerializationError(
            "search result metadata record violates its value contract"
        ) from exc


def _decode_attempt(value: object) -> AttemptRecord:
    obj = _mapping(value, _ATTEMPT_KEYS)
    try:
        return AttemptRecord(
            attempt_no=_int(obj["attempt_no"]),
            requested_url=_str(obj["requested_url"]),
            requested_at=_str(obj["requested_at"]),
            completed_at=_str(obj["completed_at"]),
            status=_opt_int(obj["status"]),
            body_bytes=_int(obj["body_bytes"]),
            body_sha256=_opt_str(obj["body_sha256"]),
            outcome=_str(obj["outcome"]),
            retry_after_seconds=_opt_number(obj["retry_after_seconds"]),
        )
    except DiscoverySerializationError:
        raise
    except (ReviewAgentError, TypeError, ValueError, OverflowError) as exc:
        raise DiscoverySerializationError(
            "search result attempt record violates its value contract"
        ) from exc


def _decode_record_array(value: object) -> tuple[MetadataRecord, ...]:
    return tuple(_decode_record(item) for item in _list(value))


def _decode_attempt_array(value: object) -> tuple[AttemptRecord, ...]:
    return tuple(_decode_attempt(item) for item in _list(value))


def _decode_page(value: object) -> SearchPage:
    obj = _mapping(value, _PAGE_KEYS)
    try:
        return SearchPage(
            source=_str(obj["source"]),
            query=_str(obj["query"]),
            requested_url=_str(obj["requested_url"]),
            retrieved_at=_str(obj["retrieved_at"]),
            page_index=_int(obj["page_index"]),
            records=_decode_record_array(obj["records"]),
            page_size=_opt_int(obj["page_size"]),
            total_count=_opt_int(obj["total_count"]),
            next_cursor=_opt_str(obj["next_cursor"]),
            complete=_bool(obj["complete"]),
            stop_reason=_opt_str(obj["stop_reason"]),
            failure=_opt_str(obj["failure"]),
            attempts=_decode_attempt_array(obj["attempts"]),
        )
    except DiscoverySerializationError:
        raise
    except (ReviewAgentError, TypeError, ValueError, OverflowError) as exc:
        raise DiscoverySerializationError(
            "search result page violates its value contract"
        ) from exc


def _decode_result_body(obj: dict[str, Any], *, pubmed: bool) -> SearchResult:
    try:
        common = {
            "source": _str(obj["source"]),
            "query": _str(obj["query"]),
            "pages": tuple(_decode_page(page) for page in _list(obj["pages"])),
            "records": _decode_record_array(obj["records"]),
            "started_at": _str(obj["started_at"]),
            "completed_at": _str(obj["completed_at"]),
            "stop_reason": _str(obj["stop_reason"]),
            "total_count": _opt_int(obj["total_count"]),
            "complete": _bool(obj["complete"]),
            "failure": _opt_str(obj["failure"]),
            "attempts": _decode_attempt_array(obj["attempts"]),
        }
        if pubmed:
            return PubMedSearchResult(
                query_translation=_opt_str(obj["query_translation"]),
                **common,
            )
        return SearchResult(**common)
    except DiscoverySerializationError:
        raise
    except (ReviewAgentError, TypeError, ValueError, OverflowError) as exc:
        raise DiscoverySerializationError(
            "search result violates its value contract"
        ) from exc


def decode_search_result(data: bytes) -> SearchResult:
    """Decode one versioned record into its exact immutable value.

    Returns the exact :class:`SearchResult` base class or
    :class:`~review_article_agent.discovery.pubmed.PubMedSearchResult` subtype
    named by the record. Every accepted value is built through the real
    dataclass constructors, so the existing invariants (page/source/query
    agreement, flattened records/attempts, ``complete`` semantics, identifier
    shapes, failure/stop consistency) are enforced rather than trusted from the
    payload. All failures raise :class:`DiscoverySerializationError` with a
    static, value-free message; no payload text is echoed.
    """
    parsed = _load_strict_json(data)
    envelope = _mapping(parsed, _ENVELOPE_KEYS)

    if _str(envelope["format"]) != SEARCH_RESULT_FORMAT:
        raise DiscoverySerializationError(
            "search result record has an unsupported format tag"
        )
    version = _int(envelope["format_version"])
    if version != SEARCH_RESULT_FORMAT_VERSION:
        raise DiscoverySerializationError(
            "search result record has an unsupported format version"
        )
    result_type = _str(envelope["result_type"])
    if result_type not in _RESULT_TYPES:
        raise DiscoverySerializationError(
            "search result record has an unsupported result type"
        )

    pubmed = result_type == _RESULT_TYPE_PUBMED
    body = _mapping(
        envelope["result"], _PUBMED_RESULT_KEYS if pubmed else _RESULT_KEYS
    )
    return _decode_result_body(body, pubmed=pubmed)