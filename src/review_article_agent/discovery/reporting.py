"""Deterministic **coverage / work / monetary report artifacts** (Step 05, R1).

Authority
---------

* ``docs/implementation/05-discovery.md`` deliverable 3 (query runs, identity
  reconciliation, citation expansion, coverage report, explicit freeze /
  search-again decision), deliverable 4 (project cost/work reporting), and
  acceptance criteria 3 (reconstruct exact queries, timestamps, contributing
  sources, failures, counts, and deduplication decisions from records), 5 (a
  known-relevant fixture set plus forward/backward citation expansion expose
  explicit coverage gaps; reporting never claims absolute completeness), and 7
  (no arbitrary result cap hidden as complete retrieval).
* ``docs/specification.md`` workflow step 5 ("a coverage report supports an
  explicit human decision to search again or freeze the candidate revision; never
  promise all papers").
* ``docs/contracts/03-policy-decisions.md`` ``POL-OPS-COVERAGE`` (the report
  records sources, queries, dates, terminology, citation expansion, yields,
  duplicates, and gaps; **no universal numeric stop/completeness rule**; the
  freeze is a separate explicit human approval) and ``POL-OPS-COST`` (project /
  stage cost ceilings; all routes counted; an unknown cost retains its reserve and
  is never reported as zero).

What this module **is**
-----------------------

A **read-only, deterministic builder** plus an immutable **report-artifact
repository** that turn the already-durable discovery layers into one review
report:

* :func:`build_coverage_report` reads — never re-fetches, never calls a provider
  or model, never writes — the exact cumulative candidate snapshot
  (:class:`~review_article_agent.discovery.candidates.CandidateRepository`), the
  plan context (:func:`~review_article_agent.discovery.context.read_context_document`),
  the durable per-run observations
  (:class:`~review_article_agent.storage.discovery.DiscoveryRepository`), any
  supplied citation-expansion reports, and the runtime / synthetic budget
  ledgers, and returns one canonical coverage-report envelope.
* :class:`CoverageRepository` stores that envelope as an **immutable** ``report``
  artifact revision (default logical id :data:`DEFAULT_COVERAGE_ARTIFACT_ID`),
  pinning the exact corpus and plan revisions (and any supplied expansion
  artifact revisions) as dependencies, and reads one exact revision back.

**Every count is derived from actual stored rows**, never from a caller-supplied
statistic. The report is **non-authorizing**: it never freezes a corpus, never
claims literature completeness, and never asserts scientific eligibility or
support. Freezing the corpus and deciding to search again remain explicit human
approvals handled elsewhere.

Two distinct scopes are kept separate, per the Step 05 work-reporting
requirement:

* **corpus scope** — ``sources``: the exact source / query / timestamps / counts
  / failures / stop-reasons / yields of the runs named by the pinned corpus
  snapshot;
* **project scope** — ``work``: the cumulative per-run observations, pages, actual
  HTTP attempts, response bytes, statuses, failures, metadata-import occurrences,
  and timestamps of **every** observation in the project. An attempt is counted
  once per immutable run; genuinely different rerun keys are separate runs.

The **monetary** view separates runtime dispatch routes (``direct_call`` vs
``opencode_session``) with known / estimated / unknown amounts, reports the single
authoritative combined budget exposure from
:meth:`~review_article_agent.runtime.jobs.JobRepository.budget_usage`, and labels
the residual **synthetic virtual** reserves separately — they are never claimed
as paid spend. The runtime and synthetic aggregates are never summed into a
fabricated total; the combined ceiling count comes only from ``budget_usage``,
and the per-dispatch rows are pinned so a historical snapshot stays honest.

Determinism, replay, and bounds
-------------------------------

The builder never reads a wall clock: its output is a pure function of durable
inputs, so an identical replay produces byte-identical content. Replay / conflict
uses the existing
:class:`~review_article_agent.storage.artifacts.ArtifactRepository` request key:
an identical ``request_id`` replay returns the historical revision; a divergent
reuse is a static :class:`CoverageConflictError`.

On read, the report is fully re-validated from its own durable bytes and its
**pinned** references only — the corpus / plan revisions must still exist with
matching digests, every embedded expansion relation must still match its stored
observation, every embedded list must reproduce its totals, and no embedded
guarantee may claim completeness or support. A report frozen earlier is **never
invalidated** because later observations grew the cumulative work totals.

Every failure raises a static, value-free error: no run id, query, source, URL,
title, identifier, digest, or payload byte is ever echoed.
"""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from typing import Iterable, Mapping

from ..domain.errors import StoreError
from ..domain.records import (
    RecordValidationError,
    canonical_json,
    validate_identifier,
    validate_ref,
)
from ..runtime.dispatch import DISPATCH_ROUTES, RuntimeDispatchRepository, runtime_reserve_exposure
from ..runtime.jobs import JobRepository
from ..storage.artifacts import (
    ArtifactConflictError,
    ArtifactRepository,
    Revision,
    sha256_digest,
)
from ..storage.discovery import DiscoveryRepository
from .candidates import CandidateRepository
from .context import read_context_document
from .execution import METADATA_IMPORT_QUERY_PREFIX
from .expansion import (
    DIRECTIONS,
    EXPANSION_FORMAT,
    EXPANSION_FORMAT_VERSION,
    EXPANSION_FINGERPRINT_VERSION,
    EXPANSION_STOP_REASONS,
    ExpansionError,
    ExpansionLimits,
    ExpansionReport,
    _coerce_limits,
)

__all__ = [
    "DOCUMENT_TYPE",
    "COVERAGE_ENVELOPE_SCHEMA_VERSION",
    "COVERAGE_PAYLOAD_VERSION",
    "DEFAULT_COVERAGE_ARTIFACT_ID",
    "MAX_REQUEST_ID_CHARS",
    "MAX_RESPONSE_BYTES_TOTAL",
    "COVERAGE_NOTICE",
    "CoverageError",
    "CoverageValidationError",
    "CoverageConflictError",
    "CoverageCorruptionError",
    "CoverageRepository",
    "build_coverage_report",
]

#: The generic-document ``document_type`` of one coverage report.
DOCUMENT_TYPE = "discovery_coverage"

#: The only supported generic-document envelope ``schema_version``.
COVERAGE_ENVELOPE_SCHEMA_VERSION = 1

#: The only supported report ``payload_version``.
COVERAGE_PAYLOAD_VERSION = 1

#: Default logical artifact that carries the coverage report.
DEFAULT_COVERAGE_ARTIFACT_ID = "discovery.coverage"

#: Request replay keys follow the existing bounded opaque-identifier convention.
MAX_REQUEST_ID_CHARS = 512

#: The monetary section is an immutable **report-time accounting snapshot**: the
#: per-dispatch identity (id/route/reserve) is pinned and re-checked against the
#: ledger on read, but the observed cost state/amount is explicit observed-at
#: evidence, never an authenticity signature over a still-mutable ledger.
MONETARY_BASIS = "report_time_accounting_snapshot"

#: Explicit note carried by every monetary section.
MONETARY_NOTE = (
    "report-time accounting snapshot: dispatch identity/route/reserve are pinned "
    "and re-checked against the ledger on read, but the observed cost state/amount "
    "is explicit observed-at evidence and not a signature over later settlement"
)

#: Technical total response-byte cap for one report (not a scientific threshold).
#: Crossing it is a static refusal, never silent truncation.
MAX_RESPONSE_BYTES_TOTAL = 1 << 40

#: Static, value-free non-claim attached to every coverage report.
COVERAGE_NOTICE = (
    "Non-authorizing coverage report: it reconstructs exact source/query "
    "observations, candidate totals, explicit gaps, cumulative project work, and "
    "budget exposure. It makes no literature-completeness claim, asserts no "
    "scientific eligibility or support, and does not freeze the corpus; freezing "
    "and search-again are separate explicit human decisions."
)

_REPORT_KEYS = frozenset(
    {
        "payload_version",
        "input_binding",
        "corpus",
        "plan",
        "sources",
        "totals",
        "citations",
        "known_relevant",
        "work",
        "monetary",
        "limitations",
        "limitations_summary",
        "guarantees",
        "authorizing",
        "completeness_claimed",
        "notice",
    }
)

_REF_KEYS = ("project_id", "artifact_id", "revision_id")

_CORPUS_KEYS = frozenset(
    {
        "ref",
        "content_sha256",
        "document_type",
        "payload_version",
        "request_id",
        "input_run_ids",
        "run_ids",
        "counts",
    }
)

_CORPUS_COUNT_KEYS = frozenset(
    {
        "candidates",
        "resolved_candidates",
        "unresolved_candidates",
        "missing_identifier_candidates",
        "identifier_conflict_candidates",
        "occurrences",
        "conflicts",
        "aliases",
        "dedup_decisions",
    }
)

_PLAN_KEYS = frozenset(
    {
        "ref",
        "content_sha256",
        "document_type",
        "boundaries",
        "terminology",
        "source_config",
    }
)

_SOURCES_KEYS = frozenset({"runs", "totals"})

_SOURCE_ROW_KEYS = frozenset(
    {
        "run_id",
        "source",
        "query",
        "started_at",
        "completed_at",
        "stop_reason",
        "failure",
        "complete",
        "total_count",
        "page_count",
        "record_count",
        "attempt_count",
        "result_sha256",
        "is_metadata_import",
    }
)

_SOURCE_TOTAL_KEYS = frozenset(
    {
        "runs",
        "pages",
        "http_attempts",
        "record_count",
        "failures",
        "stop_reasons",
        "sources",
    }
)

_WORK_ROW_KEYS = frozenset(
    {
        "run_id",
        "source",
        "query",
        "started_at",
        "completed_at",
        "stop_reason",
        "failure",
        "complete",
        "total_count",
        "page_count",
        "record_count",
        "attempt_count",
        "attempt_bytes",
        "attempt_statuses",
        "attempt_outcomes",
        "is_metadata_import",
    }
)

_WORK_TOTAL_KEYS = frozenset(
    {
        "observations",
        "pages",
        "http_attempts",
        "response_bytes",
        "failures",
        "metadata_imports",
        "imported_occurrences",
        "statuses",
        "stop_reasons",
        "started_at",
        "completed_at",
    }
)

_CITATION_KEYS = frozenset(
    {
        "reports",
        "pinned_refs",
        "roots",
        "directions",
        "frontier",
        "gaps",
        "counts",
        "expansion_performed",
    }
)

_EXPANSION_COUNT_KEYS = (
    "nodes_visited",
    "nodes_frontier",
    "relations",
    "edges",
    "unresolved_edges",
    "unexpandable_edges",
    "request_attempts",
    "stops",
)

_KNOWN_KEYS = frozenset({"status", "tokens", "matched", "missing"})

_KNOWN_TOKEN_KEYS = frozenset(
    {"namespace", "value", "present", "candidate_matches", "occurrence_count"}
)

_MONETARY_KEYS = frozenset(
    {"basis", "note", "runtime_dispatch", "synthetic_jobs", "shared_exposure", "pins"}
)

_ROUTE_KEYS = frozenset(
    {
        "dispatches",
        "known_dispatches",
        "estimated_dispatches",
        "unknown_dispatches",
        "not_incurred_dispatches",
        "known_amount_usd",
        "estimated_amount_usd",
        "unknown_reserve_usd",
        "reserve_usd",
    }
)

_SHARED_KEYS = frozenset(
    {"project_cap_usd", "stage_cap_usd", "project_reserved_usd", "project_remaining_usd"}
)

_PIN_KEYS = frozenset(
    {"dispatch_id", "route", "state", "cost_state", "cost_amount_usd", "reserve_usd"}
)

_KNOWN_NAMESPACES = ("pmid", "pmcid", "doi")

#: Guarantee keys that would (falsely) read as a verified completeness/support
#: claim when ``True``.
_FORBIDDEN_GUARANTEE_TRUE = (
    "literature_complete",
    "completeness_claimed",
    "scientific_eligibility_claimed",
    "verified_support",
    "support_claimed",
)


# --------------------------------------------------------------------------- #
# Errors (static, curated, non-reflective)
# --------------------------------------------------------------------------- #
class CoverageError(StoreError):
    """Base class for every coverage-report failure.

    Every message is static and curated: it never echoes a run id, query, source,
    URL, title, identifier, digest, or payload byte.
    """


class CoverageValidationError(CoverageError, ValueError):
    """The supplied report inputs are malformed, missing, or not acceptable."""


class CoverageConflictError(CoverageError):
    """A divergent reuse of an existing report request key."""


class CoverageCorruptionError(CoverageError):
    """A stored report fails a digest, envelope, payload, or pin revalidation."""


_M_BAD_CORPUS = "the coverage corpus reference or snapshot is not valid"
_M_BAD_PLAN = "the coverage plan reference or context document is not valid"
_M_BAD_REQUEST_ID = "a coverage request id is not a valid bounded identifier"
_M_BAD_ARTIFACT_ID = "the coverage artifact id is not a valid identifier"
_M_BAD_REF = "the coverage report reference is not a valid same-project revision reference"
_M_NOT_FOUND = "no coverage report revision exists for the supplied reference"
_M_BAD_INPUT = "the coverage report arguments are not acceptable to the artifact store"
_M_DIVERGENT = "a different coverage report is already recorded for this request key"
_M_BAD_EXPANSION = "a supplied citation-expansion report is not a valid bounded expansion"
_M_BAD_EXPANSION_PIN = "a supplied citation-expansion report disagrees with its stored observation"
_M_BAD_KNOWN = "the known-relevant token set is not a valid bounded token list"
_M_BAD_LIMITATIONS = "the limitations list is not a bounded list of non-empty strings"
_M_CORRUPT_DIGEST = "a coverage report content digest does not match its stored bytes"
_M_CORRUPT_ENVELOPE = "a coverage report is not a valid versioned coverage document"
_M_CORRUPT_CANONICAL = "a coverage report is not stored as canonical bytes"
_M_CORRUPT_PAYLOAD = "a coverage report payload is structurally invalid"
_M_CORRUPT_PIN = "a coverage report pin disagrees with its stored revision"
_M_CORRUPT_TOTALS = "a coverage report derived total disagrees with its stored rows"
_M_CORRUPT_CLAIM = "a coverage report asserts a forbidden completeness or support claim"
_M_BOUND_TOTAL = "the coverage report response-byte total exceeds the technical bound"


# --------------------------------------------------------------------------- #
# Strict helpers
# --------------------------------------------------------------------------- #
def _fail(message: str, error=CoverageValidationError) -> None:
    raise error(message)


def _has_surrogate(value: str) -> bool:
    return any(0xD800 <= ord(ch) <= 0xDFFF for ch in value)


def _plain_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _exact_keys(obj: object, allowed: frozenset, message: str) -> dict:
    if not isinstance(obj, dict):
        _fail(message)
    for key in obj:
        if not isinstance(key, str):
            _fail(message)
    if set(obj) != set(allowed):
        _fail(message)
    return obj


def _require_str(value: object, message: str = _M_CORRUPT_PAYLOAD, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or _has_surrogate(value):
        _fail(message)
    if not allow_empty and value.strip() == "":
        _fail(message)
    return value


def _require_int(value: object, message: str = _M_CORRUPT_PAYLOAD, *, minimum: int = 0) -> int:
    if not _plain_int(value) or value < minimum:
        _fail(message)
    return value


def _require_optional_int(value: object, message: str = _M_CORRUPT_PAYLOAD, *, minimum: int = 0):
    if value is None:
        return None
    return _require_int(value, message, minimum=minimum)


def _require_bool(value: object, message: str = _M_CORRUPT_PAYLOAD) -> bool:
    if not isinstance(value, bool):
        _fail(message)
    return value


def _require_int_map(value: object, message: str = _M_CORRUPT_PAYLOAD) -> dict:
    if not isinstance(value, dict):
        _fail(message)
    out: dict[str, int] = {}
    for key, count in value.items():
        if not isinstance(key, str) or _has_surrogate(key):
            _fail(message)
        out[key] = _require_int(count, message)
    return out


def _canonical_int_map(value: Mapping[str, int]) -> dict:
    return {key: value[key] for key in sorted(value)}


def _ref_from_mapping(value: object, project_id: str, message: str) -> dict:
    if not isinstance(value, dict) or set(value) != set(_REF_KEYS):
        _fail(message)
    try:
        normalized = validate_ref(value, project_id)
    except (RecordValidationError, TypeError, ValueError):
        _fail(message)
    return {
        "project_id": normalized["project_id"],
        "artifact_id": normalized["artifact_id"],
        "revision_id": normalized["revision_id"],
    }


# --------------------------------------------------------------------------- #
# Exact-decimal helpers
# --------------------------------------------------------------------------- #
def _exact_decimal(value: object, message: str = _M_CORRUPT_PAYLOAD) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        _fail(message)
    try:
        parsed = Decimal(value)
    except Exception:  # InvalidOperation
        _fail(message)
    if not parsed.is_finite() or parsed < 0:
        _fail(message)
    return parsed


def _decimal_str(value: Decimal) -> str:
    return format(value, "f")


# --------------------------------------------------------------------------- #
# Corpus / plan capture
# --------------------------------------------------------------------------- #
def _corpus_section(corpus: dict) -> dict:
    candidates = corpus["candidates"]
    occurrences = corpus["occurrences"]
    resolved = sum(1 for doc in candidates if doc["resolved"])
    counts = {
        "candidates": len(candidates),
        "resolved_candidates": resolved,
        "unresolved_candidates": len(candidates) - resolved,
        "missing_identifier_candidates": sum(
            1 for doc in candidates if doc["unresolved_reason"] == "missing_identifiers"
        ),
        "identifier_conflict_candidates": sum(
            1 for doc in candidates if doc["unresolved_reason"] == "identifier_conflict"
        ),
        "occurrences": len(occurrences),
        "conflicts": len(corpus["conflicts"]),
        "aliases": len(corpus["aliases"]),
        "dedup_decisions": len(corpus["dedup_decisions"]),
    }
    return {
        "ref": {
            "project_id": corpus["project_id"],
            "artifact_id": corpus["artifact_id"],
            "revision_id": corpus["revision_id"],
        },
        "content_sha256": corpus["content_sha256"],
        "document_type": corpus["document_type"],
        "payload_version": corpus["payload_version"],
        "request_id": corpus["request_id"],
        "input_run_ids": list(corpus["input_run_ids"]),
        "run_ids": sorted(entry["run_id"] for entry in corpus["runs"]),
        "counts": counts,
    }


def _plan_section(plan_doc: dict, plan_revision: Revision) -> dict:
    payload = plan_doc["payload"]
    queries = [
        {
            "source": entry["source"],
            "query": entry["query"],
            "page_size": entry["page_size"],
            "max_pages": entry["max_pages"],
        }
        for entry in payload["queries"]
    ]
    expansion = payload["expansion"]
    return {
        "ref": {
            "project_id": plan_revision.project_id,
            "artifact_id": plan_revision.artifact_id,
            "revision_id": plan_revision.revision_id,
        },
        "content_sha256": plan_revision.content_sha256,
        "document_type": plan_doc["document_type"],
        "boundaries": {
            "expansion": {
                "max_hops": expansion["max_hops"],
                "max_sources": expansion["max_sources"],
                "max_requests": expansion["max_requests"],
                "page_size": expansion["page_size"],
                "max_pages": expansion["max_pages"],
            },
            "approved_queries": queries,
        },
        "terminology": sorted({entry["query"] for entry in queries}),
        "source_config": {
            "query_sources": sorted({entry["source"] for entry in queries}),
            "complementary_sources": [
                {
                    "source": entry["source"],
                    "mode": entry["mode"],
                    "rationale": entry["rationale"],
                }
                for entry in payload["complementary_sources"]
            ],
        },
    }


# --------------------------------------------------------------------------- #
# Sources (corpus scope)
# --------------------------------------------------------------------------- #
def _is_metadata_import(record) -> bool:
    """Conservatively classify one stored observation as a local metadata import.

    Publication **text cannot decide provenance**: an approved provider/seed query
    is an arbitrary string and may literally start with the import descriptor
    prefix. A genuine local metadata import therefore requires *all* of the actual
    import observation semantics:

    * the query descriptor carries the ``metadata_import:`` prefix;
    * **zero** captured HTTP attempts — a local import performs no provider
      request, so any attempt (or any recorded attempt on the result) is provider
      retrieval, never an import; and
    * a successful **local** supplied-row result — ``complete``, no ``failure``,
      and ``stop_reason == "completed"`` — so a zero-call failed/interrupted
      provider query that merely shares the prefix is not an import either.

    Nothing here infers the provenance of an unknown native caller observation.
    """
    result = record.result
    if not record.query.startswith(METADATA_IMPORT_QUERY_PREFIX):
        return False
    if record.attempt_count != 0 or result.attempts:
        return False
    return (
        record.complete
        and record.failure is None
        and record.stop_reason == "completed"
    )


def _source_row(record) -> dict:
    return {
        "run_id": record.run_id,
        "source": record.source,
        "query": record.query,
        "started_at": record.started_at,
        "completed_at": record.completed_at,
        "stop_reason": record.stop_reason,
        "failure": record.failure,
        "complete": record.complete,
        "total_count": record.total_count,
        "page_count": record.page_count,
        "record_count": record.record_count,
        "attempt_count": record.attempt_count,
        "result_sha256": record.result_sha256,
        "is_metadata_import": _is_metadata_import(record),
    }


def _sources_section(discovery: DiscoveryRepository, corpus: dict) -> tuple:
    rows = []
    for run_id in sorted(entry["run_id"] for entry in corpus["runs"]):
        record = discovery.get_search(run_id)
        if record is None:
            raise CoverageCorruptionError(_M_CORRUPT_PIN)
        rows.append(_source_row(record))
    totals = {
        "runs": len(rows),
        "pages": sum(row["page_count"] for row in rows),
        "http_attempts": sum(row["attempt_count"] for row in rows),
        "record_count": sum(row["record_count"] for row in rows),
        "failures": sum(
            1 for row in rows if row["failure"] is not None or row["stop_reason"] == "failure"
        ),
        "stop_reasons": _canonical_int_map(_counts(row["stop_reason"] for row in rows)),
        "sources": _canonical_int_map(_counts(row["source"] for row in rows)),
    }
    return rows, totals


def _counts(values: Iterable[str]) -> dict:
    out: dict[str, int] = {}
    for value in values:
        out[value] = out.get(value, 0) + 1
    return out


# --------------------------------------------------------------------------- #
# Work (project scope)
# --------------------------------------------------------------------------- #
def _work_row(record) -> dict:
    statuses: dict[str, int] = {}
    outcomes: dict[str, int] = {}
    attempt_bytes = 0
    for attempt in record.result.attempts:
        key = "none" if attempt.status is None else str(attempt.status)
        statuses[key] = statuses.get(key, 0) + 1
        outcomes[attempt.outcome] = outcomes.get(attempt.outcome, 0) + 1
        attempt_bytes += attempt.body_bytes
    return {
        "run_id": record.run_id,
        "source": record.source,
        "query": record.query,
        "started_at": record.started_at,
        "completed_at": record.completed_at,
        "stop_reason": record.stop_reason,
        "failure": record.failure,
        "complete": record.complete,
        "total_count": record.total_count,
        "page_count": record.page_count,
        "record_count": record.record_count,
        "attempt_count": record.attempt_count,
        "attempt_bytes": attempt_bytes,
        "attempt_statuses": _canonical_int_map(statuses),
        "attempt_outcomes": _canonical_int_map(outcomes),
        "is_metadata_import": _is_metadata_import(record),
    }


def _work_section(discovery: DiscoveryRepository) -> tuple:
    rows = [_work_row(record) for record in discovery.list_searches()]
    rows.sort(key=lambda row: row["run_id"])
    statuses: dict[str, int] = {}
    for row in rows:
        for key, count in row["attempt_statuses"].items():
            statuses[key] = statuses.get(key, 0) + count
    totals = {
        "observations": len(rows),
        "pages": sum(row["page_count"] for row in rows),
        "http_attempts": sum(row["attempt_count"] for row in rows),
        "response_bytes": sum(row["attempt_bytes"] for row in rows),
        "failures": sum(
            1 for row in rows if row["failure"] is not None or row["stop_reason"] == "failure"
        ),
        "metadata_imports": sum(1 for row in rows if row["is_metadata_import"]),
        "imported_occurrences": sum(
            row["record_count"] for row in rows if row["is_metadata_import"]
        ),
        "statuses": _canonical_int_map(statuses),
        "stop_reasons": _canonical_int_map(_counts(row["stop_reason"] for row in rows)),
        "started_at": min((row["started_at"] for row in rows), default=None),
        "completed_at": max((row["completed_at"] for row in rows), default=None),
    }
    if totals["response_bytes"] > MAX_RESPONSE_BYTES_TOTAL:
        raise CoverageValidationError(_M_BOUND_TOTAL)
    return rows, totals


# --------------------------------------------------------------------------- #
# Monetary (runtime + synthetic; one combined ceiling)
# --------------------------------------------------------------------------- #
def _blank_route() -> dict:
    return {
        "dispatches": 0,
        "known_dispatches": 0,
        "estimated_dispatches": 0,
        "unknown_dispatches": 0,
        "not_incurred_dispatches": 0,
        "known_amount_usd": Decimal("0"),
        "estimated_amount_usd": Decimal("0"),
        "unknown_reserve_usd": Decimal("0"),
        "reserve_usd": Decimal("0"),
    }


def _accumulate_route(bucket: dict, pin: Mapping[str, object]) -> None:
    bucket["dispatches"] += 1
    reserve = _exact_decimal(pin["reserve_usd"])
    bucket["reserve_usd"] += reserve
    state = pin["cost_state"]
    if state == "known":
        bucket["known_dispatches"] += 1
        bucket["known_amount_usd"] += _exact_decimal(pin["cost_amount_usd"])
    elif state == "estimated":
        bucket["estimated_dispatches"] += 1
        bucket["estimated_amount_usd"] += _exact_decimal(pin["cost_amount_usd"])
    elif state == "unknown":
        bucket["unknown_dispatches"] += 1
        bucket["unknown_reserve_usd"] += reserve
    elif state == "not_incurred":
        bucket["not_incurred_dispatches"] += 1
    else:
        _fail(_M_CORRUPT_PAYLOAD)


def _route_to_mapping(bucket: dict) -> dict:
    return {
        key: _decimal_str(value) if isinstance(value, Decimal) else value
        for key, value in bucket.items()
    }


def _monetary_section(store) -> dict:
    dispatch_repo = RuntimeDispatchRepository(store)
    jobs_repo = JobRepository(store)
    dispatches = dispatch_repo.list_dispatches()
    routes: dict[str, dict] = {route: _blank_route() for route in DISPATCH_ROUTES}
    pins: list[dict] = []
    for record in dispatches:
        bucket = routes.setdefault(record.route, _blank_route())
        pin = {
            "dispatch_id": record.dispatch_id,
            "route": record.route,
            "state": record.state,
            "cost_state": record.cost_state,
            "cost_amount_usd": record.cost_amount_usd,
            "reserve_usd": record.reserve_usd,
        }
        _accumulate_route(bucket, pin)
        pins.append(pin)
    runtime_dispatch = {route: _route_to_mapping(bucket) for route, bucket in routes.items()}
    totals = _blank_route()
    for bucket in routes.values():
        for key, value in bucket.items():
            totals[key] += value
    runtime_dispatch["totals"] = _route_to_mapping(totals)

    usage = jobs_repo.budget_usage()
    runtime_exposure = runtime_reserve_exposure(store.connection, store.project_id)
    combined = _exact_decimal(usage.project_reserved_usd)
    synthetic = combined - runtime_exposure
    if synthetic < 0:
        raise CoverageCorruptionError(_M_CORRUPT_TOTALS)
    return {
        "basis": MONETARY_BASIS,
        "note": MONETARY_NOTE,
        "runtime_dispatch": runtime_dispatch,
        "synthetic_jobs": {
            "virtual_reserve_usd": _decimal_str(synthetic),
            "note": (
                "virtual synthetic reservation only; never paid provider/model "
                "spend and never claimed as paid"
            ),
        },
        "shared_exposure": {
            "project_cap_usd": usage.project_cap_usd,
            "stage_cap_usd": usage.stage_cap_usd,
            "project_reserved_usd": usage.project_reserved_usd,
            "project_remaining_usd": usage.project_remaining_usd,
        },
        "pins": pins,
    }


# --------------------------------------------------------------------------- #
# Expansion validation (against durable observations)
# --------------------------------------------------------------------------- #
def _native_node(value: object) -> dict:
    node = _exact_keys(value, frozenset({"source", "id"}), _M_BAD_EXPANSION)
    return {
        "source": _require_str(node["source"], _M_BAD_EXPANSION),
        "id": _require_str(node["id"], _M_BAD_EXPANSION),
    }


def _relation_against_store(relation: dict, discovery: DiscoveryRepository) -> str:
    _exact_keys(
        relation,
        frozenset(
            {
                "request_id",
                "run_id",
                "result_sha256",
                "parent",
                "direction",
                "stop_reason",
                "complete",
                "failure",
                "page_count",
                "record_count",
                "attempt_count",
                "started_at",
                "completed_at",
                "recorded_at_utc",
            }
        ),
        _M_BAD_EXPANSION,
    )
    run_id = _require_str(relation["run_id"], _M_BAD_EXPANSION)
    _require_str(relation["request_id"], _M_BAD_EXPANSION)
    _native_node(relation["parent"])
    if relation["direction"] not in DIRECTIONS:
        _fail(_M_BAD_EXPANSION)
    _require_str(relation["stop_reason"], _M_BAD_EXPANSION)
    _require_bool(relation["complete"], _M_BAD_EXPANSION)
    if relation["failure"] is not None:
        _require_str(relation["failure"], _M_BAD_EXPANSION)
    _require_int(relation["page_count"], _M_BAD_EXPANSION)
    _require_int(relation["record_count"], _M_BAD_EXPANSION)
    _require_int(relation["attempt_count"], _M_BAD_EXPANSION)
    _require_str(relation["started_at"], _M_BAD_EXPANSION)
    _require_str(relation["completed_at"], _M_BAD_EXPANSION)
    _require_str(relation["recorded_at_utc"], _M_BAD_EXPANSION)
    result_sha = _require_str(relation["result_sha256"], _M_BAD_EXPANSION)

    record = discovery.get_search(run_id)
    if record is None:
        _fail(_M_BAD_EXPANSION_PIN)
    if (
        record.result_sha256 != result_sha
        or record.page_count != relation["page_count"]
        or record.record_count != relation["record_count"]
        or record.attempt_count != relation["attempt_count"]
        or record.started_at != relation["started_at"]
        or record.completed_at != relation["completed_at"]
        or record.recorded_at_utc != relation["recorded_at_utc"]
        or record.stop_reason != relation["stop_reason"]
        or record.complete != relation["complete"]
        or record.failure != relation["failure"]
    ):
        _fail(_M_BAD_EXPANSION_PIN)
    return run_id


def validate_expansion_mapping(
    mapping: object, project_id: str, discovery: DiscoveryRepository
) -> dict:
    """Strictly re-validate one citation-expansion mapping against stored runs.

    Caller-forged counts, support flags, or gaps cannot manufacture coverage: the
    format/version/project/limits must be exact, every relation must match its
    durable observation (run id, digest, counts, timestamps, stop state), every
    edge index must reference a declared relation run, counts must reproduce the
    embedded rows, and no guarantee may claim completeness or support.
    """
    doc = _exact_keys(
        mapping,
        frozenset(
            {
                "format",
                "format_version",
                "project_id",
                "request_id",
                "operation",
                "roots",
                "limits",
                "relations",
                "edges",
                "visited",
                "frontier",
                "stops",
                "counts",
                "guarantees",
            }
        ),
        _M_BAD_EXPANSION,
    )
    if doc["format"] != EXPANSION_FORMAT or doc["format_version"] != EXPANSION_FORMAT_VERSION:
        _fail(_M_BAD_EXPANSION)
    if doc["project_id"] != project_id:
        _fail(_M_BAD_EXPANSION)
    _require_str(doc["request_id"], _M_BAD_EXPANSION)
    operation = _exact_keys(
        doc["operation"], frozenset({"fingerprint", "fingerprint_version"}), _M_BAD_EXPANSION
    )
    _require_str(operation["fingerprint"], _M_BAD_EXPANSION)
    if operation["fingerprint_version"] != EXPANSION_FINGERPRINT_VERSION:
        _fail(_M_BAD_EXPANSION)

    roots = doc["roots"]
    if not isinstance(roots, list) or not roots:
        _fail(_M_BAD_EXPANSION)
    for node in roots:
        _native_node(node)

    try:
        limits = _coerce_limits(doc["limits"])
    except (ExpansionError, ValueError, TypeError):
        _fail(_M_BAD_EXPANSION)
    if not isinstance(limits, ExpansionLimits):
        _fail(_M_BAD_EXPANSION)

    relations = doc["relations"]
    if not isinstance(relations, list):
        _fail(_M_BAD_EXPANSION)
    run_ids = []
    attempts = 0
    for relation in relations:
        run_ids.append(_relation_against_store(relation, discovery))
        attempts += relation["attempt_count"]
    run_id_set = set(run_ids)

    edges = doc["edges"]
    if not isinstance(edges, list):
        _fail(_M_BAD_EXPANSION)
    unresolved_edges = 0
    unexpandable_edges = 0
    for edge in edges:
        _exact_keys(
            edge,
            frozenset(
                {
                    "parent",
                    "direction",
                    "relation_request_id",
                    "run_id",
                    "occurrence_index",
                    "child_source_id",
                    "child_source",
                    "child_id",
                    "resolved",
                    "expandable",
                }
            ),
            _M_BAD_EXPANSION,
        )
        _native_node(edge["parent"])
        if edge["direction"] not in DIRECTIONS:
            _fail(_M_BAD_EXPANSION)
        _require_str(edge["relation_request_id"], _M_BAD_EXPANSION)
        if edge["run_id"] not in run_id_set:
            _fail(_M_BAD_EXPANSION_PIN)
        _require_int(edge["occurrence_index"], _M_BAD_EXPANSION)
        for key in ("child_source_id", "child_source", "child_id"):
            if edge[key] is not None:
                _require_str(edge[key], _M_BAD_EXPANSION)
        resolved = _require_bool(edge["resolved"], _M_BAD_EXPANSION)
        expandable = _require_bool(edge["expandable"], _M_BAD_EXPANSION)
        if not resolved:
            unresolved_edges += 1
        elif not expandable:
            unexpandable_edges += 1

    visited = doc["visited"]
    if not isinstance(visited, list):
        _fail(_M_BAD_EXPANSION)
    for observation in visited:
        observation = _exact_keys(
            observation, frozenset({"source", "id", "depth", "expanded"}), _M_BAD_EXPANSION
        )
        _require_str(observation["source"], _M_BAD_EXPANSION)
        _require_str(observation["id"], _M_BAD_EXPANSION)
        _require_int(observation["depth"], _M_BAD_EXPANSION)
        _require_bool(observation["expanded"], _M_BAD_EXPANSION)

    frontier = doc["frontier"]
    if not isinstance(frontier, list):
        _fail(_M_BAD_EXPANSION)
    for entry in frontier:
        entry = _exact_keys(
            entry, frozenset({"source", "id", "depth", "reason"}), _M_BAD_EXPANSION
        )
        _require_str(entry["source"], _M_BAD_EXPANSION)
        _require_str(entry["id"], _M_BAD_EXPANSION)
        _require_int(entry["depth"], _M_BAD_EXPANSION)
        _require_str(entry["reason"], _M_BAD_EXPANSION)

    stops = doc["stops"]
    if not isinstance(stops, list):
        _fail(_M_BAD_EXPANSION)
    for stop in stops:
        stop = _exact_keys(
            stop, frozenset({"reason", "detail", "node", "direction"}), _M_BAD_EXPANSION
        )
        if stop["reason"] not in EXPANSION_STOP_REASONS:
            _fail(_M_BAD_EXPANSION)
        _require_str(stop["detail"], _M_BAD_EXPANSION)
        if stop["node"] is not None:
            _native_node(stop["node"])
        if stop["direction"] is not None and stop["direction"] not in DIRECTIONS:
            _fail(_M_BAD_EXPANSION)

    counts = _exact_keys(doc["counts"], frozenset(_EXPANSION_COUNT_KEYS), _M_BAD_EXPANSION)
    expected = {
        "nodes_visited": len(visited),
        "nodes_frontier": len(frontier),
        "relations": len(relations),
        "edges": len(edges),
        "unresolved_edges": unresolved_edges,
        "unexpandable_edges": unexpandable_edges,
        "request_attempts": attempts,
        "stops": len(stops),
    }
    for key, value in expected.items():
        if counts[key] != value:
            _fail(_M_CORRUPT_TOTALS)

    guarantees = _exact_keys(
        doc["guarantees"], frozenset({"completeness_claimed", "verified_support"}), _M_BAD_EXPANSION
    )
    if guarantees["completeness_claimed"] is not False or guarantees["verified_support"] is not False:
        _fail(_M_CORRUPT_CLAIM)
    return doc


def _coerce_expansion(store, item: object, project_id: str, discovery: DiscoveryRepository) -> tuple:
    if isinstance(item, ExpansionReport):
        if item.project_id != project_id:
            _fail(_M_BAD_EXPANSION)
        return validate_expansion_mapping(item.to_mapping(), project_id, discovery), None
    if isinstance(item, Mapping) and "format" in item:
        return validate_expansion_mapping(dict(item), project_id, discovery), None
    if isinstance(item, Mapping) and set(item) == set(_REF_KEYS):
        ref = _ref_from_mapping(item, project_id, _M_BAD_EXPANSION)
        artifacts = ArtifactRepository(store)
        revision = artifacts.get_revision(ref["artifact_id"], ref["revision_id"])
        if revision is None:
            _fail(_M_BAD_EXPANSION)
        content = artifacts.read_content(ref["artifact_id"], ref["revision_id"])
        if sha256_digest(content) != revision.content_sha256:
            _fail(_M_BAD_EXPANSION)
        try:
            parsed = json.loads(content.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            _fail(_M_BAD_EXPANSION)
        mapping = parsed
        if isinstance(parsed, dict) and set(parsed) == {"schema_version", "document_type", "payload"}:
            mapping = parsed["payload"]
        return validate_expansion_mapping(mapping, project_id, discovery), ref
    _fail(_M_BAD_EXPANSION)


def _citations_section(store, discovery, project_id: str, expansion_reports) -> dict:
    reports = []
    pinned_refs = []
    roots = []
    seen_roots: set = set()
    directions: set = set()
    frontier = []
    gaps = []
    totals = {key: 0 for key in _EXPANSION_COUNT_KEYS}
    for index, item in enumerate(expansion_reports):
        mapping, ref = _coerce_expansion(store, item, project_id, discovery)
        reports.append(mapping)
        if ref is not None:
            pinned_refs.append(ref)
        for node in mapping["roots"]:
            key = (node["source"], node["id"])
            if key not in seen_roots:
                seen_roots.add(key)
                roots.append({"source": node["source"], "id": node["id"]})
        for relation in mapping["relations"]:
            directions.add(relation["direction"])
        for edge in mapping["edges"]:
            directions.add(edge["direction"])
        frontier.extend(
            {
                "report_index": index,
                "source": entry["source"],
                "id": entry["id"],
                "depth": entry["depth"],
                "reason": entry["reason"],
            }
            for entry in mapping["frontier"]
        )
        gaps.extend(
            {
                "report_index": index,
                "reason": stop["reason"],
                "detail": stop["detail"],
                "node": stop["node"],
                "direction": stop["direction"],
            }
            for stop in mapping["stops"]
        )
        for key in _EXPANSION_COUNT_KEYS:
            totals[key] += mapping["counts"][key]
    return {
        "reports": reports,
        "pinned_refs": pinned_refs,
        "roots": roots,
        "directions": sorted(directions),
        "frontier": frontier,
        "gaps": gaps,
        "counts": totals,
        "expansion_performed": bool(reports),
    }


# --------------------------------------------------------------------------- #
# Known-relevant tokens
# --------------------------------------------------------------------------- #
def _coerce_known_relevant(known_relevant: object) -> list:
    if known_relevant is None:
        return []
    if isinstance(known_relevant, (str, bytes, bytearray)):
        _fail(_M_BAD_KNOWN)
    try:
        iterator = iter(known_relevant)
    except TypeError:
        _fail(_M_BAD_KNOWN)
    tokens = []
    seen: set = set()
    for item in iterator:
        if not isinstance(item, dict) or set(item) != {"namespace", "value"}:
            _fail(_M_BAD_KNOWN)
        namespace = item["namespace"]
        value = item["value"]
        if not isinstance(namespace, str) or _has_surrogate(namespace):
            _fail(_M_BAD_KNOWN)
        if namespace not in _KNOWN_NAMESPACES and not (
            namespace.startswith("native:") and len(namespace) > len("native:")
        ):
            _fail(_M_BAD_KNOWN)
        if not isinstance(value, str) or value == "" or _has_surrogate(value):
            _fail(_M_BAD_KNOWN)
        key = (namespace, value)
        if key not in seen:
            seen.add(key)
            tokens.append({"namespace": namespace, "value": value})
    return tokens


def _known_section(corpus: dict, tokens: list) -> dict:
    candidate_tokens: dict = {}
    occurrence_counts: dict = {}
    for doc in corpus["candidates"]:
        for namespace, value in doc["tokens"]:
            key = (namespace, value)
            candidate_tokens.setdefault(key, []).append(doc["candidate_id"])
            occurrence_counts[key] = occurrence_counts.get(key, 0) + len(doc["occurrences"])
    if not tokens:
        return {"status": "not_assessed", "tokens": [], "matched": 0, "missing": 0}
    rows = []
    matched = 0
    for token in tokens:
        key = (token["namespace"], token["value"])
        matches = sorted(set(candidate_tokens.get(key, [])))
        present = bool(matches)
        matched += 1 if present else 0
        rows.append(
            {
                "namespace": token["namespace"],
                "value": token["value"],
                "present": present,
                "candidate_matches": matches,
                "occurrence_count": occurrence_counts.get(key, 0),
            }
        )
    return {
        "status": "assessed",
        "tokens": rows,
        "matched": matched,
        "missing": len(rows) - matched,
    }


# --------------------------------------------------------------------------- #
# Limitations
# --------------------------------------------------------------------------- #
def _coerce_limitations(limitations: object) -> list:
    if limitations is None:
        return []
    if isinstance(limitations, (str, bytes, bytearray)):
        _fail(_M_BAD_LIMITATIONS)
    try:
        iterator = iter(limitations)
    except TypeError:
        _fail(_M_BAD_LIMITATIONS)
    out = []
    for item in iterator:
        if not isinstance(item, str) or _has_surrogate(item) or item.strip() == "":
            _fail(_M_BAD_LIMITATIONS)
        out.append(item)
    return out


def _static_limitations(
    corpus_counts: dict, sources, citations: dict, known: dict, limitations: list
) -> list:
    out = list(limitations)
    out.append(
        "No universal numeric completeness or stopping threshold applies; the "
        "corpus freeze and any search-again decision are explicit human decisions."
    )
    if not citations["expansion_performed"]:
        out.append(
            "No citation expansion was supplied; forward/backward citation coverage "
            "is not assessed."
        )
    for gap in citations["gaps"]:
        out.append(f"citation expansion gap: {gap['reason']}.")
    if corpus_counts["missing_identifier_candidates"]:
        out.append(
            "Some publication candidates have missing provider identifiers and "
            "remain explicit, unresolved occurrence-local candidates."
        )
    if corpus_counts["identifier_conflict_candidates"]:
        out.append(
            "Some occurrence groups carry identifier conflicts and were never "
            "silently merged."
        )
    out.append(
        "Every candidate's integrity status is unreviewed (retraction / "
        "expression-of-concern / correction / publication date unknown; provenance "
        "missing); no clean scientific check is asserted."
    )
    for row in sources:
        if row["failure"] is not None or row["stop_reason"] in ("failure", "page_limit"):
            out.append(
                "At least one corpus query run stopped at an explicit failure or "
                "page limit; retrieval was not necessarily complete."
            )
            break
    if any(row["is_metadata_import"] for row in sources):
        out.append(
            "Metadata-import observations download nothing: they retain only the "
            "caller-supplied rows, carry no provider request/attempt, and claim no "
            "source or literature completeness; they make no model call and no cost "
            "is inferred from an absent model cost."
        )
    if known["status"] == "not_assessed":
        out.append(
            "No known-relevant token set was supplied; sensitivity against a "
            "known-relevant fixture is not assessed."
        )
    elif known["missing"]:
        out.append(
            "Some supplied known-relevant tokens were not found among the candidate "
            "observations (explicit coverage gaps)."
        )
    return out


# --------------------------------------------------------------------------- #
# Payload validation
# --------------------------------------------------------------------------- #
def _validate_corpus(section: dict, project_id: str, store) -> dict:
    """Re-derive the corpus section from the actual same-project snapshot.

    The exact (possibly historical) corpus revision is read through
    :class:`CandidateRepository`, which revalidates its digest, envelope,
    occurrence coverage, and run/payload agreement. The stored section must equal
    the re-derived section exactly, so forged counts cannot survive.
    """
    _exact_keys(section, _CORPUS_KEYS, _M_CORRUPT_PAYLOAD)
    ref = _ref_from_mapping(section["ref"], project_id, _M_CORRUPT_PAYLOAD)
    try:
        snapshot = CandidateRepository(store).get(ref)
    except Exception as exc:  # CandidateError / validate_ref
        raise CoverageCorruptionError(_M_CORRUPT_PIN) from exc
    rederived = _corpus_section(snapshot)
    if rederived != section:
        raise CoverageCorruptionError(_M_CORRUPT_TOTALS)
    return snapshot


def _validate_plan(section: dict, project_id: str, store) -> None:
    """Re-derive the plan section from the pinned context document revision.

    The exact pinned plan revision is read with ``require_current=False`` (a
    historical snapshot preserves its exact ref) and typed ``discovery_plan``,
    then re-mapped. The stored section must equal the re-derived section.
    """
    _exact_keys(section, _PLAN_KEYS, _M_CORRUPT_PAYLOAD)
    ref = _ref_from_mapping(section["ref"], project_id, _M_CORRUPT_PAYLOAD)
    try:
        document = read_context_document(
            store, ref, expected_type="discovery_plan", require_current=False
        )
    except Exception as exc:  # DiscoveryContextError
        raise CoverageCorruptionError(_M_CORRUPT_PIN) from exc
    revision = ArtifactRepository(store).get_revision(
        ref["artifact_id"], ref["revision_id"]
    )
    if revision is None:
        raise CoverageCorruptionError(_M_CORRUPT_PIN)
    if _plan_section(document, revision) != section:
        raise CoverageCorruptionError(_M_CORRUPT_TOTALS)


def _recompute_source_totals(rows: list) -> dict:
    return {
        "runs": len(rows),
        "pages": sum(row["page_count"] for row in rows),
        "http_attempts": sum(row["attempt_count"] for row in rows),
        "record_count": sum(row["record_count"] for row in rows),
        "failures": sum(
            1 for row in rows if row["failure"] is not None or row["stop_reason"] == "failure"
        ),
        "stop_reasons": _canonical_int_map(_counts(row["stop_reason"] for row in rows)),
        "sources": _canonical_int_map(_counts(row["source"] for row in rows)),
    }


def _validate_source_rows(rows: list) -> None:
    if not isinstance(rows, list):
        _fail(_M_CORRUPT_PAYLOAD)
    seen: set = set()
    for row in rows:
        _exact_keys(row, _SOURCE_ROW_KEYS, _M_CORRUPT_PAYLOAD)
        run_id = _require_str(row["run_id"])
        if run_id in seen:
            _fail(_M_CORRUPT_PAYLOAD)
        seen.add(run_id)
        _require_str(row["source"])
        _require_str(row["query"])
        _require_str(row["started_at"])
        _require_str(row["completed_at"])
        _require_str(row["stop_reason"])
        if row["failure"] is not None:
            _require_str(row["failure"])
        _require_bool(row["complete"])
        _require_optional_int(row["total_count"])
        _require_int(row["page_count"])
        _require_int(row["record_count"])
        _require_int(row["attempt_count"])
        _require_str(row["result_sha256"])
        _require_bool(row["is_metadata_import"])


def _validate_sources(section: dict, corpus: dict, discovery) -> None:
    section = _exact_keys(section, _SOURCES_KEYS, _M_CORRUPT_PAYLOAD)
    rows = section["runs"]
    _validate_source_rows(rows)
    if sorted(row["run_id"] for row in rows) != corpus["run_ids"]:
        _fail(_M_CORRUPT_TOTALS)
    # Durable source pin: every corpus-scope row must be re-derived exactly from
    # its stored run, including the conservative metadata-import classification.
    for row in rows:
        record = discovery.get_search(row["run_id"])
        if record is None:
            raise CoverageCorruptionError(_M_CORRUPT_PIN)
        if _source_row(record) != row:
            raise CoverageCorruptionError(_M_CORRUPT_PIN)
    totals = _exact_keys(section["totals"], _SOURCE_TOTAL_KEYS, _M_CORRUPT_PAYLOAD)
    expected = _recompute_source_totals(rows)
    for key, value in expected.items():
        if totals[key] != value:
            _fail(_M_CORRUPT_TOTALS)


def _validate_totals(totals: dict, corpus: dict) -> None:
    totals = _exact_keys(totals, _CORPUS_COUNT_KEYS, _M_CORRUPT_PAYLOAD)
    for value in totals.values():
        _require_int(value)
    if totals != corpus["counts"]:
        _fail(_M_CORRUPT_TOTALS)


def _validate_citations(section: dict, project_id: str, discovery) -> None:
    section = _exact_keys(section, _CITATION_KEYS, _M_CORRUPT_PAYLOAD)
    reports = section["reports"]
    if not isinstance(reports, list):
        _fail(_M_CORRUPT_PAYLOAD)
    for mapping in reports:
        validate_expansion_mapping(mapping, project_id, discovery)
    if section["expansion_performed"] != bool(reports):
        _fail(_M_CORRUPT_TOTALS)
    if not isinstance(section["pinned_refs"], list):
        _fail(_M_CORRUPT_PAYLOAD)
    for ref in section["pinned_refs"]:
        _ref_from_mapping(ref, project_id, _M_CORRUPT_PAYLOAD)
    if not isinstance(section["roots"], list):
        _fail(_M_CORRUPT_PAYLOAD)
    for node in section["roots"]:
        _native_node(node)
    if not isinstance(section["directions"], list):
        _fail(_M_CORRUPT_PAYLOAD)
    for direction in section["directions"]:
        if direction not in DIRECTIONS:
            _fail(_M_CORRUPT_PAYLOAD)
    if not isinstance(section["frontier"], list) or not isinstance(section["gaps"], list):
        _fail(_M_CORRUPT_PAYLOAD)
    # Recompute the citation aggregates from the embedded (validated) reports.
    expected_roots = []
    seen_roots: set = set()
    expected_directions: set = set()
    for mapping in reports:
        for node in mapping["roots"]:
            key = (node["source"], node["id"])
            if key not in seen_roots:
                seen_roots.add(key)
                expected_roots.append({"source": node["source"], "id": node["id"]})
        for relation in mapping["relations"]:
            expected_directions.add(relation["direction"])
        for edge in mapping["edges"]:
            expected_directions.add(edge["direction"])
    if section["roots"] != expected_roots:
        _fail(_M_CORRUPT_TOTALS)
    if section["directions"] != sorted(expected_directions):
        _fail(_M_CORRUPT_TOTALS)
    counts = _exact_keys(section["counts"], frozenset(_EXPANSION_COUNT_KEYS), _M_CORRUPT_PAYLOAD)
    expected = {key: 0 for key in _EXPANSION_COUNT_KEYS}
    for mapping in reports:
        for key in _EXPANSION_COUNT_KEYS:
            expected[key] += mapping["counts"][key]
    for key, value in counts.items():
        if value != expected[key]:
            _fail(_M_CORRUPT_TOTALS)


def _validate_known(known: dict, corpus_snapshot: dict) -> None:
    """Re-derive the known-relevant section from the actual corpus candidates.

    The declared tokens are re-matched against the exact same-project snapshot, so
    forged candidate ids, present flags, and matched/missing counts are refused.
    """
    known = _exact_keys(known, _KNOWN_KEYS, _M_CORRUPT_PAYLOAD)
    if known["status"] not in ("assessed", "not_assessed"):
        _fail(_M_CORRUPT_PAYLOAD)
    tokens = known["tokens"]
    if not isinstance(tokens, list):
        _fail(_M_CORRUPT_PAYLOAD)
    declared = []
    for token in tokens:
        token = _exact_keys(token, _KNOWN_TOKEN_KEYS, _M_CORRUPT_PAYLOAD)
        namespace = _require_str(token["namespace"])
        if namespace not in _KNOWN_NAMESPACES and not namespace.startswith("native:"):
            _fail(_M_CORRUPT_PAYLOAD)
        _require_str(token["value"])
        declared.append({"namespace": namespace, "value": token["value"]})
    rederived = _known_section(corpus_snapshot, declared)
    if rederived != known:
        _fail(_M_CORRUPT_TOTALS)


def _validate_work(work: dict, discovery) -> None:
    work = _exact_keys(work, frozenset({"observations", "totals"}), _M_CORRUPT_PAYLOAD)
    rows = work["observations"]
    if not isinstance(rows, list):
        _fail(_M_CORRUPT_PAYLOAD)
    seen: set = set()
    pages = http_attempts = response_bytes = failures = metadata_imports = imported = 0
    statuses: dict = {}
    stop_reasons: dict = {}
    started_values = []
    completed_values = []
    for row in rows:
        _exact_keys(row, _WORK_ROW_KEYS, _M_CORRUPT_PAYLOAD)
        run_id = _require_str(row["run_id"])
        if run_id in seen:
            _fail(_M_CORRUPT_PAYLOAD)
        seen.add(run_id)
        # Each stored historical row is re-derived from its immutable observation;
        # later unrelated runs must not stale the report, but a forged historical
        # row must fail even when its totals are made internally consistent.
        record = discovery.get_search(run_id)
        if record is None:
            raise CoverageCorruptionError(_M_CORRUPT_PIN)
        if _work_row(record) != row:
            raise CoverageCorruptionError(_M_CORRUPT_TOTALS)
        _require_str(row["source"])
        _require_str(row["query"])
        _require_str(row["started_at"])
        _require_str(row["completed_at"])
        stop_reason = _require_str(row["stop_reason"])
        if row["failure"] is not None:
            _require_str(row["failure"])
        _require_bool(row["complete"])
        _require_optional_int(row["total_count"])
        page_count = _require_int(row["page_count"])
        record_count = _require_int(row["record_count"])
        attempt_count = _require_int(row["attempt_count"])
        attempt_bytes = _require_int(row["attempt_bytes"])
        row_statuses = _require_int_map(row["attempt_statuses"])
        _require_int_map(row["attempt_outcomes"])
        is_import = _require_bool(row["is_metadata_import"])
        pages += page_count
        http_attempts += attempt_count
        response_bytes += attempt_bytes
        if row["failure"] is not None or stop_reason == "failure":
            failures += 1
        if is_import:
            metadata_imports += 1
            imported += record_count
        for key, count in row_statuses.items():
            statuses[key] = statuses.get(key, 0) + count
        stop_reasons[stop_reason] = stop_reasons.get(stop_reason, 0) + 1
        started_values.append(row["started_at"])
        completed_values.append(row["completed_at"])
    totals = _exact_keys(work["totals"], _WORK_TOTAL_KEYS, _M_CORRUPT_PAYLOAD)
    expected = {
        "observations": len(rows),
        "pages": pages,
        "http_attempts": http_attempts,
        "response_bytes": response_bytes,
        "failures": failures,
        "metadata_imports": metadata_imports,
        "imported_occurrences": imported,
        "statuses": _canonical_int_map(statuses),
        "stop_reasons": _canonical_int_map(stop_reasons),
        "started_at": min(started_values, default=None),
        "completed_at": max(completed_values, default=None),
    }
    for key, value in expected.items():
        if totals[key] != value:
            _fail(_M_CORRUPT_TOTALS)
    if response_bytes > MAX_RESPONSE_BYTES_TOTAL:
        _fail(_M_BOUND_TOTAL)


def _validate_monetary(monetary: dict, store) -> None:
    monetary = _exact_keys(monetary, _MONETARY_KEYS, _M_CORRUPT_PAYLOAD)
    if monetary["basis"] != MONETARY_BASIS:
        _fail(_M_CORRUPT_PAYLOAD)
    _require_str(monetary["note"])
    pins = monetary["pins"]
    if not isinstance(pins, list):
        _fail(_M_CORRUPT_PAYLOAD)
    dispatches = RuntimeDispatchRepository(store)
    routes: dict = {route: _blank_route() for route in DISPATCH_ROUTES}
    seen: set = set()
    for pin in pins:
        pin = _exact_keys(pin, _PIN_KEYS, _M_CORRUPT_PAYLOAD)
        dispatch_id = _require_str(pin["dispatch_id"])
        if dispatch_id in seen:
            _fail(_M_CORRUPT_PAYLOAD)
        seen.add(dispatch_id)
        route = _require_str(pin["route"])
        _require_str(pin["state"])
        # Immutable identity pin: the dispatch must exist and its immutable route
        # and reserve must match the ledger. The observed cost state/amount is
        # report-time evidence and is NOT required to equal a later settlement
        # (that would invalidate a legitimately frozen historical report).
        try:
            live = dispatches.get_dispatch(dispatch_id)
        except Exception as exc:  # DispatchCorruptionError
            raise CoverageCorruptionError(_M_CORRUPT_PIN) from exc
        if live is None or live.route != route or live.reserve_usd != pin["reserve_usd"]:
            raise CoverageCorruptionError(_M_CORRUPT_PIN)
        bucket = routes.setdefault(route, _blank_route())
        _accumulate_route(bucket, pin)
    for route in DISPATCH_ROUTES:
        stored = monetary["runtime_dispatch"].get(route)
        if stored is None:
            _fail(_M_CORRUPT_PAYLOAD)
        stored = _exact_keys(stored, _ROUTE_KEYS, _M_CORRUPT_PAYLOAD)
        for key, value in routes[route].items():
            want = _decimal_str(value) if isinstance(value, Decimal) else value
            if stored[key] != want:
                _fail(_M_CORRUPT_TOTALS)
    totals = monetary["runtime_dispatch"].get("totals")
    if totals is None:
        _fail(_M_CORRUPT_PAYLOAD)
    totals = _exact_keys(totals, _ROUTE_KEYS, _M_CORRUPT_PAYLOAD)
    expected_totals = _blank_route()
    for bucket in routes.values():
        for key, value in bucket.items():
            expected_totals[key] += value
    for key, value in expected_totals.items():
        want = _decimal_str(value) if isinstance(value, Decimal) else value
        if totals[key] != want:
            _fail(_M_CORRUPT_TOTALS)
    synthetic = _exact_keys(
        monetary["synthetic_jobs"], frozenset({"virtual_reserve_usd", "note"}), _M_CORRUPT_PAYLOAD
    )
    _exact_decimal(synthetic["virtual_reserve_usd"])
    _require_str(synthetic["note"])
    shared = _exact_keys(monetary["shared_exposure"], _SHARED_KEYS, _M_CORRUPT_PAYLOAD)
    for key in _SHARED_KEYS:
        _exact_decimal(shared[key])


def _validate_guarantees(guarantees: dict, *, authorizing: bool, completeness: bool) -> None:
    if not isinstance(guarantees, dict):
        _fail(_M_CORRUPT_PAYLOAD)
    for key in guarantees:
        if not isinstance(key, str):
            _fail(_M_CORRUPT_PAYLOAD)
    for key in _FORBIDDEN_GUARANTEE_TRUE:
        if guarantees.get(key) is True:
            _fail(_M_CORRUPT_CLAIM)
    if guarantees.get("literature_completeness") == "claimed":
        _fail(_M_CORRUPT_CLAIM)
    if authorizing is not False or completeness is not False:
        _fail(_M_CORRUPT_CLAIM)


def _validate_payload(payload: dict, *, project_id: str, store) -> dict:
    payload = _exact_keys(payload, _REPORT_KEYS, _M_CORRUPT_PAYLOAD)
    if payload["payload_version"] != COVERAGE_PAYLOAD_VERSION:
        _fail(_M_CORRUPT_PAYLOAD)
    if payload["notice"] != COVERAGE_NOTICE:
        _fail(_M_CORRUPT_PAYLOAD)
    discovery = DiscoveryRepository(store)
    corpus_snapshot = _validate_corpus(payload["corpus"], project_id, store)
    _validate_plan(payload["plan"], project_id, store)
    _validate_sources(payload["sources"], payload["corpus"], discovery)
    _validate_totals(payload["totals"], payload["corpus"])
    _validate_citations(payload["citations"], project_id, discovery)
    _validate_known(payload["known_relevant"], corpus_snapshot)
    _validate_work(payload["work"], discovery)
    _validate_monetary(payload["monetary"], store)
    if not isinstance(payload["limitations"], list):
        _fail(_M_CORRUPT_PAYLOAD)
    if not isinstance(payload["limitations_summary"], list):
        _fail(_M_CORRUPT_PAYLOAD)
    for text in payload["limitations"] + payload["limitations_summary"]:
        _require_str(text)
    # The logical request binding must equal the one re-derived from this payload.
    if payload["input_binding"] != _binding_from_payload(payload):
        _fail(_M_CORRUPT_TOTALS)
    # The human-readable summary is re-derived so a malicious payload cannot erase
    # an automatic known gap.
    expected_summary = _static_limitations(
        payload["corpus"]["counts"],
        payload["sources"]["runs"],
        payload["citations"],
        payload["known_relevant"],
        payload["limitations"],
    )
    if payload["limitations_summary"] != expected_summary:
        _fail(_M_CORRUPT_TOTALS)
    _validate_guarantees(
        payload["guarantees"],
        authorizing=payload["authorizing"],
        completeness=payload["completeness_claimed"],
    )
    return payload


# --------------------------------------------------------------------------- #
# Logical request binding (replay identity that excludes mutable work/monetary)
# --------------------------------------------------------------------------- #
def _expansion_digests(citations: dict) -> list:
    return [
        hashlib.sha256(canonical_json(mapping)).hexdigest()
        for mapping in citations["reports"]
    ]


def _logical_binding(
    corpus_section: dict,
    plan_section: dict,
    citations: dict,
    known_tokens: list,
    limitations_list: list,
) -> dict:
    """The canonical, deterministic identity of one logical coverage request.

    It covers exactly the caller/durable inputs — corpus ref + digest, plan ref +
    digest, the ordered expansion inputs (pinned refs and validated-mapping
    digests), the declared known-relevant tokens, and the caller limitations — and
    deliberately excludes the mutable cumulative work/monetary sections, so an
    identical logical replay is recognised even after project work grows.
    """
    return {
        "corpus": {
            "artifact_id": corpus_section["ref"]["artifact_id"],
            "revision_id": corpus_section["ref"]["revision_id"],
            "content_sha256": corpus_section["content_sha256"],
        },
        "plan": {
            "artifact_id": plan_section["ref"]["artifact_id"],
            "revision_id": plan_section["ref"]["revision_id"],
            "content_sha256": plan_section["content_sha256"],
        },
        "citation_pinned_refs": [
            [ref["artifact_id"], ref["revision_id"]] for ref in citations["pinned_refs"]
        ],
        "citation_report_digests": _expansion_digests(citations),
        "known_relevant": [[token["namespace"], token["value"]] for token in known_tokens],
        "limitations": list(limitations_list),
    }


def _binding_from_payload(payload: dict) -> dict:
    corpus = payload["corpus"]
    plan = payload["plan"]
    citations = payload["citations"]
    known = payload["known_relevant"]
    return {
        "corpus": {
            "artifact_id": corpus["ref"]["artifact_id"],
            "revision_id": corpus["ref"]["revision_id"],
            "content_sha256": corpus["content_sha256"],
        },
        "plan": {
            "artifact_id": plan["ref"]["artifact_id"],
            "revision_id": plan["ref"]["revision_id"],
            "content_sha256": plan["content_sha256"],
        },
        "citation_pinned_refs": [
            [ref["artifact_id"], ref["revision_id"]] for ref in citations["pinned_refs"]
        ],
        "citation_report_digests": _expansion_digests(citations),
        "known_relevant": [[token["namespace"], token["value"]] for token in known["tokens"]],
        "limitations": list(payload["limitations"]),
    }


# --------------------------------------------------------------------------- #
# Public builder
# --------------------------------------------------------------------------- #
def _prepare_logical(
    store, corpus_ref, plan_ref, expansion_reports, known_relevant, limitations
) -> dict:
    """Read and validate only the durable/logical inputs of one report."""
    project_id = store.project_id
    artifacts = ArtifactRepository(store)
    discovery = DiscoveryRepository(store)
    candidates = CandidateRepository(store)

    try:
        corpus = candidates.get(corpus_ref)
    except Exception as exc:  # CandidateError / validate_ref etc.
        raise CoverageValidationError(_M_BAD_CORPUS) from exc

    # The exact pinned plan revision is read with ``require_current=False`` so a
    # historical snapshot keeps its exact ref even after the plan head moves.
    try:
        plan_doc = read_context_document(
            store, plan_ref, expected_type="discovery_plan", require_current=False
        )
    except Exception as exc:  # DiscoveryContextError etc.
        raise CoverageValidationError(_M_BAD_PLAN) from exc
    plan_ref_norm = _ref_from_mapping(plan_ref, project_id, _M_BAD_PLAN)
    plan_revision = artifacts.get_revision(
        plan_ref_norm["artifact_id"], plan_ref_norm["revision_id"]
    )
    if plan_revision is None:
        raise CoverageValidationError(_M_BAD_PLAN)

    corpus_section = _corpus_section(corpus)
    plan_section = _plan_section(plan_doc, plan_revision)
    citations = _citations_section(store, discovery, project_id, expansion_reports)
    known_tokens = _coerce_known_relevant(known_relevant)
    limitations_list = _coerce_limitations(limitations)
    known = _known_section(corpus, known_tokens)
    binding = _logical_binding(
        corpus_section, plan_section, citations, known_tokens, limitations_list
    )
    return {
        "project_id": project_id,
        "corpus": corpus,
        "corpus_section": corpus_section,
        "plan_section": plan_section,
        "citations": citations,
        "known": known,
        "known_tokens": known_tokens,
        "limitations_list": limitations_list,
        "binding": binding,
    }


def _assemble_payload(prepared: dict, store) -> dict:
    discovery = DiscoveryRepository(store)
    corpus_section = prepared["corpus_section"]
    citations = prepared["citations"]
    known = prepared["known"]
    limitations_list = prepared["limitations_list"]
    source_rows, source_totals = _sources_section(discovery, prepared["corpus"])
    work_rows, work_totals = _work_section(discovery)
    return {
        "payload_version": COVERAGE_PAYLOAD_VERSION,
        "input_binding": prepared["binding"],
        "corpus": corpus_section,
        "plan": prepared["plan_section"],
        "sources": {"runs": source_rows, "totals": source_totals},
        "totals": dict(corpus_section["counts"]),
        "citations": citations,
        "known_relevant": known,
        "work": {"observations": work_rows, "totals": work_totals},
        "monetary": _monetary_section(store),
        "limitations": limitations_list,
        "limitations_summary": _static_limitations(
            corpus_section["counts"], source_rows, citations, known, limitations_list
        ),
        "guarantees": {
            "literature_complete": False,
            "literature_completeness": "not_claimed",
            "scientific_eligibility_claimed": False,
            "verified_support": False,
        },
        "authorizing": False,
        "completeness_claimed": False,
        "notice": COVERAGE_NOTICE,
    }


def build_coverage_report(
    store,
    corpus_ref,
    *,
    plan_ref,
    expansion_reports=(),
    known_relevant=(),
    limitations=(),
) -> dict:
    """Build one deterministic coverage-report envelope from durable inputs.

    Reads the exact corpus and plan revisions, the stored observations, any
    supplied expansion reports, and the budget ledgers. Makes **no** provider,
    model, worker, or approval call, and writes nothing. Returns the generic
    document envelope ``{schema_version, document_type, payload}`` ready to store.
    """
    prepared = _prepare_logical(
        store, corpus_ref, plan_ref, expansion_reports, known_relevant, limitations
    )
    payload = _assemble_payload(prepared, store)
    _validate_payload(payload, project_id=prepared["project_id"], store=store)
    return {
        "schema_version": COVERAGE_ENVELOPE_SCHEMA_VERSION,
        "document_type": DOCUMENT_TYPE,
        "payload": payload,
    }


# --------------------------------------------------------------------------- #
# Storage-backed read helpers
# --------------------------------------------------------------------------- #
def _read_document(artifacts: ArtifactRepository, revision: Revision) -> dict:
    content = artifacts.read_content(revision.artifact_id, revision.revision_id)
    if sha256_digest(content) != revision.content_sha256 or len(content) != revision.byte_size:
        raise CoverageCorruptionError(_M_CORRUPT_DIGEST)
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        raise CoverageCorruptionError(_M_CORRUPT_ENVELOPE)
    try:
        parsed = json.loads(text, parse_constant=_reject_constant)
    except (ValueError, TypeError, RecursionError):
        raise CoverageCorruptionError(_M_CORRUPT_ENVELOPE)
    if not isinstance(parsed, dict) or set(parsed) != {"schema_version", "document_type", "payload"}:
        raise CoverageCorruptionError(_M_CORRUPT_ENVELOPE)
    if parsed["schema_version"] != COVERAGE_ENVELOPE_SCHEMA_VERSION:
        raise CoverageCorruptionError(_M_CORRUPT_ENVELOPE)
    if parsed["document_type"] != DOCUMENT_TYPE:
        raise CoverageCorruptionError(_M_CORRUPT_ENVELOPE)
    if not isinstance(parsed["payload"], dict):
        raise CoverageCorruptionError(_M_CORRUPT_ENVELOPE)
    try:
        canonical = canonical_json(parsed)
    except (RecordValidationError, TypeError, ValueError):
        raise CoverageCorruptionError(_M_CORRUPT_ENVELOPE)
    if canonical != content:
        raise CoverageCorruptionError(_M_CORRUPT_CANONICAL)
    return parsed


def _reject_constant(_value: str) -> object:
    raise CoverageCorruptionError(_M_CORRUPT_ENVELOPE)


def _assemble(revision: Revision, payload: dict) -> dict:
    return {
        "project_id": revision.project_id,
        "artifact_id": revision.artifact_id,
        "revision_id": revision.revision_id,
        "content_sha256": revision.content_sha256,
        "byte_size": revision.byte_size,
        "document_type": DOCUMENT_TYPE,
        "schema_version": COVERAGE_ENVELOPE_SCHEMA_VERSION,
        "acquired_at_utc": revision.acquired_at_utc,
        "payload": payload,
    }


def _artifact_identifier(value: object) -> str:
    if not isinstance(value, str) or _has_surrogate(value):
        _fail(_M_BAD_ARTIFACT_ID)
    try:
        return validate_identifier(value, "artifact_id")
    except (RecordValidationError, TypeError, ValueError):
        _fail(_M_BAD_ARTIFACT_ID)


def _request_identifier(value: object) -> str:
    if not isinstance(value, str) or _has_surrogate(value):
        _fail(_M_BAD_REQUEST_ID)
    if len(value) > MAX_REQUEST_ID_CHARS:
        _fail(_M_BAD_REQUEST_ID)
    try:
        return validate_identifier(value, "request_id")
    except (RecordValidationError, TypeError, ValueError):
        _fail(_M_BAD_REQUEST_ID)


# --------------------------------------------------------------------------- #
# Repository
# --------------------------------------------------------------------------- #
class CoverageRepository:
    """Immutable coverage-report revisions for one project store.

    Typical use::

        coverage = CoverageRepository(store)
        rid = CoverageRepository.new_revision_id()
        rights = build_valid_rights_record(rid)          # caller-supplied, exact
        coverage.record_report(
            corpus_ref, plan_ref=plan_ref, request_id="coverage-1",
            rights=rights, revision_id=rid,
        )
        report = coverage.current()                       # dict | None
        exact = coverage.get({"project_id": store.project_id,
                              "artifact_id": "discovery.coverage",
                              "revision_id": rid})

    It performs **no** network, provider, approval, model, or science work.
    """

    def __init__(self, store) -> None:
        self._store = store
        self._artifacts = ArtifactRepository(store)

    @property
    def project_id(self) -> str:
        """The actual store's project id; no caller may select a project."""
        return self._store.project_id

    @staticmethod
    def new_revision_id() -> str:
        """A fresh opaque revision id (caller-assigned for exact rights binding)."""
        return ArtifactRepository.new_revision_id()

    # -- write path ---------------------------------------------------------- #
    def record_report(
        self,
        corpus_ref,
        *,
        plan_ref,
        request_id,
        rights,
        expansion_reports=(),
        known_relevant=(),
        limitations=(),
        revision_id: str | None = None,
        artifact_id: str = DEFAULT_COVERAGE_ARTIFACT_ID,
    ) -> Revision:
        """Build and store one immutable coverage-report revision.

        ``rights`` (a validated ``RightsRecord`` or a stored ``RightsRecordRef``)
        is pinned to this exact revision and storage is allowed only when it
        permits it. The exact corpus and plan revisions — and any supplied
        expansion artifact revisions — are pinned as dependencies. An identical
        replay of ``request_id`` returns the historical revision; a divergent
        reuse raises :class:`CoverageConflictError`.
        """
        artifact_id = _artifact_identifier(artifact_id)
        request_id = _request_identifier(request_id)

        # Read only the durable/logical inputs first, so an identical logical
        # replay is recognised even after unrelated work/monetary state changed.
        prepared = _prepare_logical(
            self._store, corpus_ref, plan_ref, expansion_reports, known_relevant, limitations
        )

        existing = self._find_request(artifact_id, request_id)
        if existing is not None:
            existing_revision, existing_binding = existing
            if existing_binding == prepared["binding"]:
                return existing_revision
            raise CoverageConflictError(_M_DIVERGENT)

        payload = _assemble_payload(prepared, self._store)
        _validate_payload(payload, project_id=prepared["project_id"], store=self._store)
        document = {
            "schema_version": COVERAGE_ENVELOPE_SCHEMA_VERSION,
            "document_type": DOCUMENT_TYPE,
            "payload": payload,
        }

        dependencies = [
            {**document["payload"]["corpus"]["ref"], "role": "derived_from"},
            {**document["payload"]["plan"]["ref"], "role": "derived_from"},
        ]
        for ref in document["payload"]["citations"]["pinned_refs"]:
            dependencies.append({**ref, "role": "derived_from"})

        self._artifacts.create_artifact(artifact_id, kind="report", created_by="import")
        # A genuinely new revision names the artifact's current head as its exact
        # parent, so a second report can be recorded after a new search/decision.
        current = self._artifacts.current_revision(artifact_id)
        parent_revision_id = current.revision_id if current is not None else None
        try:
            return self._artifacts.import_revision(
                artifact_id=artifact_id,
                content=document,
                origin_locator=_origin_locator(request_id),
                rights=rights,
                revision_id=revision_id,
                parent_revision_id=parent_revision_id,
                dependencies=dependencies,
                acquired_by="import",
                request_id=request_id,
            )
        except ArtifactConflictError as exc:
            raise CoverageConflictError(_M_DIVERGENT) from exc

    # -- read path ----------------------------------------------------------- #
    def current(self, artifact_id: str = DEFAULT_COVERAGE_ARTIFACT_ID) -> dict | None:
        """Read the current coverage report, or ``None`` when absent."""
        artifact_id = _artifact_identifier(artifact_id)
        revision = self._artifacts.current_revision(artifact_id)
        if revision is None:
            return None
        return self._read(revision)

    def get(self, ref: object) -> dict:
        """Read one exact report by explicit same-project revision reference."""
        try:
            normalized = validate_ref(ref, self.project_id)
        except (RecordValidationError, TypeError, ValueError):
            _fail(_M_BAD_REF)
        revision = self._artifacts.get_revision(
            normalized["artifact_id"], normalized["revision_id"]
        )
        if revision is None:
            _fail(_M_NOT_FOUND)
        return self._read(revision)

    # -- internals ----------------------------------------------------------- #
    def _find_request(self, artifact_id: str, request_id: str):
        """Return ``(revision, input_binding)`` for a recorded request key, if any.

        The stored revision is fully revalidated (its binding is re-derived from
        its own bytes and durable pins), so a corrupt historical report fails
        closed rather than replaying.
        """
        locator = _origin_locator(request_id)
        for revision in self._artifacts.list_revisions(artifact_id):
            if revision.origin_locator != locator:
                continue
            assembled = self._read(revision)
            return revision, assembled["payload"]["input_binding"]
        return None

    def _read(self, revision: Revision) -> dict:
        document = _read_document(self._artifacts, revision)
        try:
            payload = _validate_payload(
                document["payload"], project_id=self.project_id, store=self._store
            )
        except CoverageValidationError as exc:
            # A stored report that fails its own payload validation is corrupt.
            raise CoverageCorruptionError(_M_CORRUPT_PAYLOAD) from exc
        return _assemble(revision, payload)


def _origin_locator(request_id: str) -> str:
    digest = hashlib.sha256(request_id.encode("utf-8")).hexdigest()
    return f"coverage:{digest}"
