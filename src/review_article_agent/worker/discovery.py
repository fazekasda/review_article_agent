"""Owner-controlled detached **discovery search** jobs for the worker engine.

Authority
---------
* ``docs/implementation/05-discovery.md`` deliverable 2 and acceptance criteria
  2/3/4 (a broad search cannot execute an unapproved scope/query plan; exact
  queries/timestamps/sources/failures/counts are reconstructable; fixed fixtures
  cover pagination/overlap/missing IDs/partial failures/incremental reruns).
* ``docs/specification.md`` "Confirmed scientific workflow" steps 2-5 and the
  gates/safety invariants (approvals bind exact artifact revisions; provider
  output is untrusted data; one execution-active project).
* ``docs/contracts/02-gates-and-runtime.md`` §8 (durable jobs, pause/cancel,
  detached CLI, one serial remote executor).

What this module **is**
-----------------------
The thin glue between the durable worker engine and the already-integrated
approved-plan execution **library** (:mod:`review_article_agent.discovery.execution`,
S1). It:

* strictly validates and canonicalises the owner's submission into immutable
  ``JobRepository`` parameters (one job ``task_kind`` per operation, no reserve,
  no actor/authority field);
* builds the low-level transport the S1 executor retries, adding a **job-level**
  stop check (paused / cancelled / draining) that runs *before every real
  attempt* — the S1 guard already rechecks the approved plan and the execution
  activation;
* dispatches one job through the S1 API using the exact job request id as the
  S1 replay key, so the S1 observation identity durably encodes the plan
  revision, its content digest, the query index, and the query fingerprint;
* projects an observation into **safe** job-result metadata (run id, digest,
  source, counts, completeness, and a hashed query binding) — never the raw
  query text or provider bytes;
* provides a replay-only probe that proves an existing local observation without
  any provider I/O, for honest restart reconciliation.

What this module **is not**
---------------------------
* It makes **no** exactly-once external-request or billing claim, fabricates
  **no** receipt, and performs **no** scientific decision. A recorded
  observation is inert, project-local provenance.
* It performs **no** approval. The S1 library revalidates the exact approved plan
  and the activation before every attempt; this module never bypasses that.
* It never reads a caller-supplied file path and never opens a store across the
  network: a caller supplies the store, the registry, and the activation token,
  and the whole per-job flow runs on one thread with one store connection.
"""

from __future__ import annotations

import hashlib
import hmac
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable

from ..discovery.candidates import CandidateRepository
from ..discovery.context import (
    PLAN_GATE_ID,
    PROTOCOL_GATE_KIND,
    require_approved_context,
)
from ..discovery.execution import (
    MAX_IMPORT_RECORDS,
    MAX_ORIGIN_LOCATOR_CHARS,
    PLAN_QUERY_SOURCES,
    SEED_MAX_PAGES,
    SEED_MAX_RECORDS,
    DiscoveryExecutionError,
    DiscoveryExecutor,
    query_fingerprint,
)
from ..discovery.expansion import CitationNode, ExpansionLimits, expand_citations
from ..discovery.records import SOURCE_EUROPEPMC, MetadataRecord
from ..discovery.transport import DiscoveryRequestInterrupted
from ..domain.errors import ReviewAgentError
from ..domain.records import (
    RecordValidationError,
    canonical_json,
    validate_ref,
)
from ..runtime import ipc
from . import acquisition as acquisition_jobs

__all__ = [
    "MODE_QUERY",
    "MODE_SEED",
    "MODE_METADATA_IMPORT",
    "MODE_EXPAND",
    "MODE_LEAD",
    "MODE_ACQUIRE",
    "DISCOVERY_MODES",
    "TASK_KIND_QUERY",
    "TASK_KIND_SEED",
    "TASK_KIND_IMPORT",
    "TASK_KIND_EXPAND",
    "TASK_KIND_LEAD",
    "TASK_KIND_ACQUIRE",
    "DISCOVERY_TASK_KINDS",
    "MODE_PARAMETER_KEYS",
    "LEAD_CREDENTIAL_ENV",
    "RESULT_KIND_OBSERVATION",
    "RESULT_KIND_EXPANSION",
    "RESULT_KIND_LEAD",
    "RESULT_KIND_ACQUISITION",
    "MAX_DISCOVERY_PARAMETERS_BYTES",
    "MAX_EXPAND_ROOTS",
    "PREDISPATCH_PHASE",
    "DiscoveryJobError",
    "DiscoveryJobValidationError",
    "DiscoveryJobStopped",
    "AttemptCounter",
    "ReplayedOutcome",
    "guard_transport",
    "mode_for_task_kind",
    "task_kind_for_mode",
    "is_discovery_task_kind",
    "predispatch_checkpoint",
    "is_predispatch_checkpoint",
    "observation_metadata",
    "accepted_result",
    "status_projection",
    "result_kind_for_mode",
    "lead_outcome_accepted",
    "validate_submission",
    "dispatch",
    "dispatch_expand",
    "dispatch_lead",
    "replay_only",
    "persisted_expansion_report",
]

# --------------------------------------------------------------------------- #
# Protocol vocabulary
# --------------------------------------------------------------------------- #
#: The five supported operation modes (one durable job task kind each).
MODE_QUERY = "query"
MODE_SEED = "seed"
MODE_METADATA_IMPORT = "metadata_import"
MODE_EXPAND = "expand"
MODE_LEAD = "lead"
#: Official full-text retrieval (Step 06); validated/dispatched by ``worker.acquisition``.
MODE_ACQUIRE = acquisition_jobs.MODE_ACQUIRE

#: Every accepted discovery mode.
DISCOVERY_MODES = (
    MODE_QUERY,
    MODE_SEED,
    MODE_METADATA_IMPORT,
    MODE_EXPAND,
    MODE_LEAD,
    MODE_ACQUIRE,
)

#: Durable job task kinds (the existing ``JobRepository`` accepts these strings).
TASK_KIND_QUERY = "discovery_query"
TASK_KIND_SEED = "discovery_seed"
TASK_KIND_IMPORT = "discovery_import"
TASK_KIND_EXPAND = "discovery_expand"
TASK_KIND_LEAD = "discovery_lead"
#: A public-network task in the shared serial detached pool (not a discovery search).
TASK_KIND_ACQUIRE = acquisition_jobs.TASK_KIND_ACQUIRE

#: Every discovery job task kind.
DISCOVERY_TASK_KINDS = frozenset(
    {
        TASK_KIND_QUERY,
        TASK_KIND_SEED,
        TASK_KIND_IMPORT,
        TASK_KIND_EXPAND,
        TASK_KIND_LEAD,
        TASK_KIND_ACQUIRE,
    }
)

_MODE_BY_TASK_KIND = {
    TASK_KIND_QUERY: MODE_QUERY,
    TASK_KIND_SEED: MODE_SEED,
    TASK_KIND_IMPORT: MODE_METADATA_IMPORT,
    TASK_KIND_EXPAND: MODE_EXPAND,
    TASK_KIND_LEAD: MODE_LEAD,
    TASK_KIND_ACQUIRE: MODE_ACQUIRE,
}
_TASK_KIND_BY_MODE = {mode: kind for kind, mode in _MODE_BY_TASK_KIND.items()}

#: Exact, strict parameter keys per mode (no actor/authority flag is permitted).
MODE_PARAMETER_KEYS: dict[str, frozenset[str]] = {
    MODE_QUERY: frozenset({"plan_ref", "query_index"}),
    MODE_SEED: frozenset({"source", "query", "page_size", "max_pages"}),
    MODE_METADATA_IMPORT: frozenset(
        {"plan_ref", "source", "records", "origin_locator", "observed_at"}
    ),
    MODE_EXPAND: frozenset({"plan_ref", "corpus_ref", "roots"}),
    MODE_LEAD: frozenset({"batch_ref", "credential_env"}),
    MODE_ACQUIRE: acquisition_jobs.ACQUIRE_REQUIRED_KEYS
    | acquisition_jobs.ACQUIRE_OPTIONAL_KEYS,
}

#: The one, fixed environment-variable name a hosted-lead job may name.
LEAD_CREDENTIAL_ENV = "OPENROUTER_API_KEY"

#: Safe job-accepted-result kinds, one per result family.
RESULT_KIND_OBSERVATION = "discovery_observation"
RESULT_KIND_EXPANSION = "discovery_expansion"
RESULT_KIND_LEAD = "discovery_lead"
RESULT_KIND_ACQUISITION = acquisition_jobs.RESULT_KIND_ACQUISITION

#: Technical bound on the number of explicit expansion roots.
MAX_EXPAND_ROOTS = 10_000

#: Hard bound on the persisted (canonical) parameters, matching the IPC request
#: cap so a metadata payload can never exceed what the control socket accepts.
MAX_DISCOVERY_PARAMETERS_BYTES = ipc.MAX_REQUEST_BYTES

#: The attempt-checkpoint phase that records "this attempt may have dispatched".
PREDISPATCH_PHASE = "discovery_predispatch"

#: Exact accepted ``MetadataRecord`` field names for a strict mapping import.
_RECORD_FIELDS = (
    "source",
    "source_id",
    "pmid",
    "pmcid",
    "doi",
    "title",
    "publication_year",
)

_UTC_TIMESTAMP_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|\+00:00)$"
)

# Static, value-free messages (never echo a query, source, locator, or byte).
_M_BAD_MODE = "mode is not a supported discovery operation"
_M_BAD_PARAMETERS = "parameters must be an exact object for the requested mode"
_M_BAD_KEY = "parameters contained an unknown or missing field"
_M_BAD_PLAN_REF = "plan_ref must be an exact same-project revision reference"
_M_BAD_INDEX = "query_index must be a non-negative integer"
_M_BAD_SOURCE = "source is not one of the two supported query APIs"
_M_BAD_QUERY = "query must be a non-empty query string"
_M_BAD_BOUND = "the search budget is out of range or exceeds a hard ceiling"
_M_BAD_RECORDS = "records are not strict MetadataRecord data for this source"
_M_BAD_RECORD_BOUND = "the metadata import exceeds the permitted record bound"
_M_BAD_TIME = "observed_at must be an RFC 3339 UTC instant"
_M_BAD_LOCATOR = "origin_locator must be a bounded non-empty opaque locator"
_M_BAD_CORPUS_REF = "corpus_ref must be an exact same-project revision reference"
_M_BAD_ROOTS = "roots must be observed, distinct expandable native publication identities"
_M_BAD_CREDENTIAL_ENV = "credential_env must name the one approved environment variable"
_M_MISSING_CREDENTIAL = "the named hosted-lead credential is not available to the worker"
_M_BAD_BATCH_REF = "batch_ref must be an exact same-project revision reference"
_M_TOO_LARGE = "the discovery parameters exceed the control-transport byte bound"
_M_BAD_TRANSPORT = "the low-level discovery transport must be callable"
_M_INTERRUPTED = "the discovery job was stopped before the next provider request"
_M_BLOCKED_PLAN = (
    "the exact approved discovery plan is not current and approved; expansion blocked"
)
_M_BLOCKED_CORPUS = (
    "the requested corpus snapshot is not the current, intact corpus; expansion blocked"
)
_M_UNOBSERVED_ROOT = "an expansion root is not an observed identity of the current corpus"
_M_BAD_LIMITS = "the approved plan's expansion limits are not usable"


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #
class DiscoveryJobError(ReviewAgentError):
    """A discovery job submission or dispatch failed (static, curated)."""


class DiscoveryJobValidationError(DiscoveryJobError, ValueError):
    """A submitted discovery parameter set was malformed (fail closed).

    ``code`` is ``"bad_type"`` or ``"bad_value"`` so the engine can surface its
    existing curated error code without echoing the offending value.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class DiscoveryJobStopped(Exception):
    """Internal signal: the durable job is paused/cancelled/draining.

    Deliberately **not** a :class:`ReviewAgentError`: it must propagate unchanged
    through the S1 adapter boundary (which converts a generic
    :class:`ReviewAgentError` into a static plan-query refusal) so the worker can
    distinguish "we stopped it" from "the plan was refused".
    """


# --------------------------------------------------------------------------- #
# Transport guard
# --------------------------------------------------------------------------- #
class AttemptCounter:
    """A shared, single-writer counter/flag for one discovery dispatch.

    ``value`` counts **real** low-level provider attempts. ``interrupted`` is set
    when the job-level stop check refused an attempt, so the caller can label a
    returned partial result as an explicit interruption rather than a natural end.
    """

    __slots__ = ("value", "interrupted")

    def __init__(self) -> None:
        self.value = 0
        self.interrupted = False


def guard_transport(
    base: Callable[..., Any],
    *,
    stop_requested: Callable[[], bool],
    counter: AttemptCounter,
) -> Callable[..., Any]:
    """Wrap ``base`` so the durable job's held state is checked per attempt.

    The S1 executor already rechecks the approved plan and the execution
    activation before every real attempt; this adds the **job-level** stop check
    (paused/cancelled/draining). ``max_response_bytes`` and ``timeout`` are read
    from the adapter request and forwarded unchanged; unsafe provider URLs are
    rejected earlier by the adapter's fixed endpoint allowlist, so no URL is
    trusted here.

    When at least one real attempt has already been made, a stop raises the
    opt-in :class:`~review_article_agent.discovery.transport.DiscoveryRequestInterrupted`
    (never a worker-specific type): the shared transport turns it into a failed
    page with **no fabricated attempt**, so the provider adapter keeps the pages
    already observed and the S1 completion path can commit the partial
    observation. With no attempt yet there is nothing to preserve, so a hard
    :class:`DiscoveryJobStopped` aborts the dispatch cleanly (no empty result).
    """
    if not callable(base) or not callable(stop_requested):
        raise DiscoveryJobValidationError("bad_type", _M_BAD_TRANSPORT)

    def lowlevel(url: str, *, timeout: float, max_response_bytes: int) -> Any:
        if stop_requested():
            counter.interrupted = True
            if counter.value == 0:
                raise DiscoveryJobStopped()
            raise DiscoveryRequestInterrupted(_M_INTERRUPTED)
        counter.value += 1
        return base(url, timeout=timeout, max_response_bytes=max_response_bytes)

    return lowlevel


# --------------------------------------------------------------------------- #
# Task-kind / mode helpers
# --------------------------------------------------------------------------- #
def is_discovery_task_kind(task_kind: object) -> bool:
    return isinstance(task_kind, str) and task_kind in DISCOVERY_TASK_KINDS


def mode_for_task_kind(task_kind: object) -> str | None:
    if not isinstance(task_kind, str):
        return None
    return _MODE_BY_TASK_KIND.get(task_kind)


def task_kind_for_mode(mode: object) -> str | None:
    if not isinstance(mode, str):
        return None
    return _TASK_KIND_BY_MODE.get(mode)


# --------------------------------------------------------------------------- #
# Dispatch-intent checkpoint
# --------------------------------------------------------------------------- #
def predispatch_checkpoint(mode: str) -> dict[str, Any]:
    """The durable marker recorded *before* a discovery provider call is issued.

    Its presence after a restart is the honest "may have dispatched" signal: the
    recovery path never blind-retries such an attempt, and instead proves an
    existing observation (accept same fence) or escalates.
    """
    return {"discovery_predispatch": True, "phase": PREDISPATCH_PHASE, "mode": mode}


def is_predispatch_checkpoint(checkpoint: object) -> bool:
    return isinstance(checkpoint, dict) and checkpoint.get("discovery_predispatch") is True


# --------------------------------------------------------------------------- #
# Strict submission validation
# --------------------------------------------------------------------------- #
def _bad_type(message: str) -> DiscoveryJobValidationError:
    return DiscoveryJobValidationError("bad_type", message)


def _bad_value(message: str) -> DiscoveryJobValidationError:
    return DiscoveryJobValidationError("bad_value", message)


def _plain_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _has_surrogate(value: str) -> bool:
    return any(0xD800 <= ord(ch) <= 0xDFFF for ch in value)


def _require_keys(parameters: object, allowed: frozenset[str]) -> dict[str, Any]:
    if not isinstance(parameters, dict):
        raise _bad_type(_M_BAD_PARAMETERS)
    if set(parameters) != set(allowed):
        raise _bad_value(_M_BAD_KEY)
    return parameters


def _validate_plan_ref(value: object, project_id: str) -> dict[str, str]:
    try:
        return validate_ref(value, project_id)
    except (RecordValidationError, TypeError, ValueError):
        raise _bad_value(_M_BAD_PLAN_REF) from None


def _validate_utc_instant(value: object) -> str:
    if not isinstance(value, str) or _UTC_TIMESTAMP_RE.fullmatch(value) is None:
        raise _bad_value(_M_BAD_TIME)
    candidate = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        raise _bad_value(_M_BAD_TIME) from None
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise _bad_value(_M_BAD_TIME)
    return value


def _validate_locator(value: object) -> str:
    if not isinstance(value, str) or value.strip() == "":
        raise _bad_value(_M_BAD_LOCATOR)
    if len(value) > MAX_ORIGIN_LOCATOR_CHARS or _has_surrogate(value):
        raise _bad_value(_M_BAD_LOCATOR)
    for ch in value:
        if ord(ch) < 0x20 or ord(ch) == 0x7F:
            raise _bad_value(_M_BAD_LOCATOR)
    return value


def _validate_records(value: object, source: str) -> list[dict[str, Any]]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, (list, tuple)):
        raise _bad_type(_M_BAD_RECORDS)
    if len(value) > MAX_IMPORT_RECORDS:
        raise _bad_value(_M_BAD_RECORD_BOUND)
    out: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            raise _bad_type(_M_BAD_RECORDS)
        for key in item:
            if not isinstance(key, str) or key not in _RECORD_FIELDS:
                raise _bad_value(_M_BAD_RECORDS)
        try:
            record = MetadataRecord(**item)
        except Exception:  # noqa: BLE001 - a bad record is an explicit refusal
            raise _bad_value(_M_BAD_RECORDS) from None
        if record.source != source:
            raise _bad_value(_M_BAD_RECORDS)
        out.append({field: getattr(record, field) for field in _RECORD_FIELDS})
    return out


def _check_parameter_size(parameters: dict[str, Any]) -> None:
    try:
        encoded = canonical_json(parameters)
    except (RecordValidationError, TypeError, ValueError):
        raise _bad_value(_M_BAD_RECORDS) from None
    if len(encoded) > MAX_DISCOVERY_PARAMETERS_BYTES:
        raise _bad_value(_M_TOO_LARGE)


def validate_submission(
    mode: object,
    parameters: object,
    project_id: str,
    request_id: object = None,
) -> dict[str, Any]:
    """Strictly validate and canonicalise one discovery submission.

    Returns the exact, immutable parameter dict that becomes the durable
    ``JobRepository`` job's ``parameters`` (so a divergent replay under the same
    request id is a job conflict). Raises :class:`DiscoveryJobValidationError`
    **before** any durable write, activation change, or provider I/O.
    """
    if mode not in DISCOVERY_MODES:
        raise _bad_value(_M_BAD_MODE)
    if mode == MODE_ACQUIRE:
        canonical = acquisition_jobs.validate_acquire(parameters, project_id, request_id)
        _check_parameter_size(canonical)
        return canonical
    allowed = MODE_PARAMETER_KEYS[mode]
    params = _require_keys(parameters, allowed)

    if mode == MODE_QUERY:
        canonical = {
            "plan_ref": _validate_plan_ref(params["plan_ref"], project_id),
            "query_index": _validate_query_index(params["query_index"]),
        }
    elif mode == MODE_SEED:
        canonical = _validate_seed(params)
    elif mode == MODE_METADATA_IMPORT:
        canonical = _validate_import(params, project_id)
    elif mode == MODE_EXPAND:
        canonical = _validate_expand(params, project_id)
    else:
        canonical = _validate_lead(params, project_id)

    _check_parameter_size(canonical)
    return canonical


def _validate_query_index(value: object) -> int:
    if not _plain_int(value):
        raise _bad_type(_M_BAD_INDEX)
    if value < 0:
        raise _bad_value(_M_BAD_INDEX)
    return value


def _validate_seed(params: dict[str, Any]) -> dict[str, Any]:
    source = params["source"]
    if source not in PLAN_QUERY_SOURCES:
        raise _bad_value(_M_BAD_SOURCE)
    query = params["query"]
    if not isinstance(query, str) or query.strip() == "" or _has_surrogate(query):
        raise _bad_value(_M_BAD_QUERY)
    page_size = params["page_size"]
    max_pages = params["max_pages"]
    if not _plain_int(page_size) or not _plain_int(max_pages):
        raise _bad_type(_M_BAD_BOUND)
    if not (1 <= page_size <= SEED_MAX_RECORDS) or not (1 <= max_pages <= SEED_MAX_PAGES):
        raise _bad_value(_M_BAD_BOUND)
    if page_size * max_pages > SEED_MAX_RECORDS:
        raise _bad_value(_M_BAD_BOUND)
    return {
        "source": source,
        "query": query,
        "page_size": page_size,
        "max_pages": max_pages,
    }


def _validate_import(params: dict[str, Any], project_id: str) -> dict[str, Any]:
    source = params["source"]
    if not isinstance(source, str) or source.strip() == "" or _has_surrogate(source):
        raise _bad_value(_M_BAD_SOURCE)
    if any(ch.isspace() for ch in source) or "/" in source or "\\" in source:
        raise _bad_value(_M_BAD_SOURCE)
    return {
        "plan_ref": _validate_plan_ref(params["plan_ref"], project_id),
        "source": source,
        "records": _validate_records(params["records"], source),
        "origin_locator": _validate_locator(params["origin_locator"]),
        "observed_at": _validate_utc_instant(params["observed_at"]),
    }


def _validate_root_item(item: object) -> tuple[str, str]:
    if not isinstance(item, dict) or set(item) != {"source", "id"}:
        raise _bad_value(_M_BAD_ROOTS)
    source = item["source"]
    native_id = item["id"]
    if not isinstance(source, str) or not isinstance(native_id, str):
        raise _bad_type(_M_BAD_ROOTS)
    if _has_surrogate(source) or _has_surrogate(native_id):
        raise _bad_value(_M_BAD_ROOTS)
    try:
        node = CitationNode(source, native_id)
    except Exception:  # noqa: BLE001 - an unsafe identity is an explicit refusal
        raise _bad_value(_M_BAD_ROOTS) from None
    return node.source, node.id


def _validate_roots(value: object) -> list[dict[str, str]]:
    """Validate the roots as safe, distinct native publication identities.

    Only the *shape* and the native-identity safety are enforced here; whether a
    root is an **observed** candidate of the current corpus is a store-bound check
    performed at dispatch time (still before any provider request). A duplicate
    root is collapsed (order-preserving) rather than refused.
    """
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, (list, tuple)):
        raise _bad_type(_M_BAD_ROOTS)
    if not value:
        raise _bad_value(_M_BAD_ROOTS)
    if len(value) > MAX_EXPAND_ROOTS:
        raise _bad_value(_M_BAD_ROOTS)
    roots: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for item in value:
        source, native_id = _validate_root_item(item)
        key = (source, native_id)
        if key in seen:
            continue
        seen.add(key)
        roots.append({"source": source, "id": native_id})
    return roots


def _validate_expand(params: dict[str, Any], project_id: str) -> dict[str, Any]:
    return {
        "plan_ref": _validate_plan_ref(params["plan_ref"], project_id),
        "corpus_ref": _validate_plan_ref(params["corpus_ref"], project_id),
        "roots": _validate_roots(params["roots"]),
    }


def _validate_lead(params: dict[str, Any], project_id: str) -> dict[str, Any]:
    credential_env = params["credential_env"]
    if credential_env != LEAD_CREDENTIAL_ENV:
        raise _bad_value(_M_BAD_CREDENTIAL_ENV)
    return {
        "batch_ref": _validate_plan_ref(params["batch_ref"], project_id),
        "credential_env": LEAD_CREDENTIAL_ENV,
    }


# --------------------------------------------------------------------------- #
# Safe result projection
# --------------------------------------------------------------------------- #
def observation_metadata(job: Any, record: Any) -> dict[str, Any]:
    """Project an S1 observation into safe, value-free job-result metadata.

    Never includes the raw query text, provider bytes, record titles, or the raw
    import locator: the query binding is a stable fingerprint and the locator is
    reduced to a SHA-256 digest.
    """
    mode = mode_for_task_kind(job.task_kind)
    params = job.parameters if isinstance(job.parameters, dict) else {}
    meta: dict[str, Any] = {
        "kind": "discovery_observation",
        "mode": mode,
        "run_id": record.run_id,
        "source": record.source,
        "result_type": record.result_type,
        "result_sha256": record.result_sha256,
        "result_bytes": record.result_bytes,
        "page_count": record.page_count,
        "record_count": record.record_count,
        "attempt_count": record.attempt_count,
        "complete": record.complete,
        "stop_reason": record.stop_reason,
        "failure": record.failure,
        "binding": {},
    }
    binding = meta["binding"]
    if mode in (MODE_QUERY, MODE_METADATA_IMPORT):
        plan_ref = params.get("plan_ref")
        if isinstance(plan_ref, dict):
            binding["plan_ref"] = {
                "project_id": plan_ref.get("project_id"),
                "artifact_id": plan_ref.get("artifact_id"),
                "revision_id": plan_ref.get("revision_id"),
            }
    if mode == MODE_QUERY:
        binding["query_index"] = params.get("query_index")
    elif mode == MODE_SEED:
        binding["query_fingerprint"] = query_fingerprint(
            source=params.get("source"),
            query=params.get("query"),
            page_size=params.get("page_size"),
            max_pages=params.get("max_pages"),
        )
    elif mode == MODE_METADATA_IMPORT:
        locator = params.get("origin_locator")
        if isinstance(locator, str):
            binding["origin_locator_sha256"] = hashlib.sha256(
                locator.encode("utf-8")
            ).hexdigest()
        binding["observed_at"] = params.get("observed_at")
    return meta


def result_kind_for_mode(mode: str | None) -> str:
    """The accepted-result kind of one discovery mode."""
    if mode == MODE_EXPAND:
        return RESULT_KIND_EXPANSION
    if mode == MODE_LEAD:
        return RESULT_KIND_LEAD
    if mode == MODE_ACQUIRE:
        return RESULT_KIND_ACQUISITION
    return RESULT_KIND_OBSERVATION


def accepted_result(job: Any, raw: Any) -> tuple[str, Any]:
    """Project a dispatch outcome into ``(result_kind, safe_result)``.

    A query/seed/import observation is reduced to value-free metadata; an
    expansion report is stored as its **full** canonical mapping (bounded by the
    report itself and read back explicitly by a later coverage reader); a hosted
    lead outcome is stored as its safe status/dispatch mapping (never the API key
    or the proposal text). Nothing here fabricates a scientific result.
    """
    mode = mode_for_task_kind(job.task_kind)
    if mode == MODE_EXPAND:
        return RESULT_KIND_EXPANSION, raw.to_mapping()
    if mode == MODE_LEAD:
        return RESULT_KIND_LEAD, dict(raw)
    if mode == MODE_ACQUIRE:
        return RESULT_KIND_ACQUISITION, acquisition_jobs.accepted_result(raw)
    return RESULT_KIND_OBSERVATION, observation_metadata(job, raw)


def _expansion_summary(result: dict[str, Any]) -> dict[str, Any]:
    """A bounded status projection of a saved expansion report (never the graph)."""
    stops = result.get("stops")
    reasons: list[str] = []
    if isinstance(stops, list):
        for stop in stops:
            if isinstance(stop, dict) and isinstance(stop.get("reason"), str):
                if stop["reason"] not in reasons:
                    reasons.append(stop["reason"])
    return {
        "kind": RESULT_KIND_EXPANSION,
        "mode": MODE_EXPAND,
        "project_id": result.get("project_id"),
        "request_id": result.get("request_id"),
        "operation_fingerprint": (result.get("operation") or {}).get("fingerprint"),
        "roots_count": len(result.get("roots") or []),
        "limits": result.get("limits"),
        "counts": result.get("counts"),
        "guarantees": result.get("guarantees"),
        "stop_reasons": reasons,
        "report_sha256": hashlib.sha256(canonical_json(result)).hexdigest(),
    }


def _lead_summary(result: dict[str, Any]) -> dict[str, Any]:
    """A safe status/usage/cost projection of a saved hosted-lead outcome."""
    return {
        "kind": RESULT_KIND_LEAD,
        "mode": MODE_LEAD,
        "request_id": result.get("request_id"),
        "status": result.get("status"),
        "outcome": result.get("outcome"),
        "dispatch_id": result.get("dispatch_id"),
        "dispatch_state": result.get("dispatch_state"),
        "task_id": result.get("task_id"),
        "cost_state": result.get("cost_state"),
        "cost_amount_usd": result.get("cost_amount_usd"),
        "reserve_usd": result.get("reserve_usd"),
        "actual_model": result.get("actual_model"),
        "provider": result.get("provider"),
        "response_id": result.get("response_id"),
        "usage": result.get("usage"),
        "http_status": result.get("http_status"),
        "no_automatic_retry": result.get("no_automatic_retry"),
    }


def status_projection(result_kind: str, result: Any) -> Any:
    """Bound a saved accepted result for ``job_status``/``job_list`` output.

    A saved expansion graph may be very large, so status never dumps it: it shows
    the mode, counts, stop reasons, limits and a report digest, while the **full**
    mapping stays in ``JobRepository`` for an explicit later read. A hosted lead
    outcome is reduced to safe status/usage/cost (no API key, no proposal text);
    every other kind is returned as-is.
    """
    if not isinstance(result, dict):
        return result
    if result_kind == RESULT_KIND_EXPANSION:
        return _expansion_summary(result)
    if result_kind == RESULT_KIND_LEAD:
        return _lead_summary(result)
    if result_kind == RESULT_KIND_ACQUISITION:
        return acquisition_jobs.status_projection(result)
    return result


# --------------------------------------------------------------------------- #
# Expansion scope revalidation (store-bound; before any provider request)
# --------------------------------------------------------------------------- #
def _approved_expansion_plan(store: Any, plan_ref: object) -> dict[str, Any]:
    """Read the exact approved current plan, or raise a fail-closed block."""
    try:
        return require_approved_context(
            store,
            plan_ref,
            gate_kind=PROTOCOL_GATE_KIND,
            gate_id=PLAN_GATE_ID,
            expected_type="discovery_plan",
        )
    except Exception as exc:  # noqa: BLE001 - any refusal is a fail-closed block
        raise DiscoveryExecutionError(_M_BLOCKED_PLAN) from exc


def _current_corpus(store: Any, corpus_ref: object) -> dict[str, Any]:
    """Read the exact current, intact, validated corpus snapshot, or raise."""
    repo = CandidateRepository(store)
    try:
        snapshot = repo.get(corpus_ref)
        current = repo.current(snapshot["artifact_id"])
    except Exception as exc:  # noqa: BLE001 - a bad/stale corpus fails closed
        raise DiscoveryExecutionError(_M_BLOCKED_CORPUS) from exc
    if (
        current is None
        or current.get("artifact_id") != snapshot.get("artifact_id")
        or current.get("revision_id") != snapshot.get("revision_id")
    ):
        raise DiscoveryExecutionError(_M_BLOCKED_CORPUS)
    return snapshot


def _observed_identity_sets(snapshot: dict[str, Any]) -> tuple[set[str], set[str], set[str]]:
    """Return the corpus's observed ``native:europepmc``/``pmid``/``pmcid`` values."""
    native: set[str] = set()
    pmids: set[str] = set()
    pmcids: set[str] = set()
    for candidate in snapshot.get("candidates") or []:
        for token in candidate.get("tokens") or []:
            if not isinstance(token, list) or len(token) != 2:
                continue
            namespace, value = token
            if namespace == f"native:{SOURCE_EUROPEPMC}":
                native.add(value)
            elif namespace == "pmid":
                pmids.add(value)
            elif namespace == "pmcid":
                pmcids.add(value)
    return native, pmids, pmcids


def _require_observed_roots(roots: list[dict[str, str]], snapshot: dict[str, Any]) -> None:
    """Fail closed unless every root names an observed candidate identity.

    A root ``SOURCE:id`` is observed when the current corpus records it as a
    provider-native ``native:europepmc`` token (the exact Europe PMC citation
    locator), or as an asserted ``pmid`` (``MED:id``) / ``pmcid`` (``PMC:id``)
    token. This is a known **API locator** binding, never a study-identity claim.
    """
    native, pmids, pmcids = _observed_identity_sets(snapshot)
    for root in roots:
        source, native_id = root["source"], root["id"]
        observed = f"{source}:{native_id}" in native
        if not observed and source == "MED":
            observed = native_id in pmids
        if not observed and source == "PMC":
            observed = native_id in pmcids
        if not observed:
            raise DiscoveryExecutionError(_M_UNOBSERVED_ROOT)


def _expansion_limits(plan_document: dict[str, Any]) -> ExpansionLimits:
    """Build the exact limits from ``plan.payload.expansion`` (never a caller).

    There is no caller override: the only accepted bounds are the approved plan's.
    """
    payload = plan_document.get("payload")
    raw = payload.get("expansion") if isinstance(payload, dict) else None
    try:
        return ExpansionLimits(
            max_hops=raw["max_hops"],
            max_sources=raw["max_sources"],
            max_requests=raw["max_requests"],
            page_size=raw["page_size"],
            max_pages=raw["max_pages"],
        )
    except Exception as exc:  # noqa: BLE001 - unusable limits fail closed
        raise DiscoveryExecutionError(_M_BAD_LIMITS) from exc


def _token_authorizes(registry: Any, project_id: str, token: str) -> bool:
    """True only when ``token`` is this project's active, non-draining activation."""
    if not isinstance(token, str) or token == "":
        return False
    try:
        activation = registry.activation()
    except Exception:  # noqa: BLE001 - unreadable activation fails closed
        return False
    return (
        activation.state == "active"
        and activation.active_project_id == project_id
        and hmac.compare_digest(activation.activation_token or "", token)
    )


def _expand_before_request(
    store: Any,
    registry: Any,
    token: str,
    plan_ref: object,
    corpus_ref: object,
    roots: list[dict[str, str]],
    job_held: Callable[[], bool] | None,
) -> Callable[[Any], object]:
    """A trusted guard that revalidates plan/corpus/roots/token/job before a request.

    It runs before **every** real low-level attempt (through
    :func:`~review_article_agent.discovery.expansion.expand_citations`); returning
    ``False`` refuses the upcoming request and stops the traversal with an explicit
    gap, so a changed plan, a superseded corpus, a withdrawn root, a lost
    activation, or a paused/cancelled/draining job stops further provider I/O.
    """

    def before_request(_context: Any) -> object:
        if job_held is not None and job_held():
            return False
        try:
            plan_document = _approved_expansion_plan(store, plan_ref)
            _expansion_limits(plan_document)
            snapshot = _current_corpus(store, corpus_ref)
            _require_observed_roots(roots, snapshot)
        except Exception:  # noqa: BLE001 - any revalidation failure refuses the request
            return False
        return _token_authorizes(registry, store.project_id, token)

    return before_request


def _expand_roots(roots: list[dict[str, str]]) -> list[CitationNode]:
    return [CitationNode(root["source"], root["id"]) for root in roots]


def dispatch_expand(
    store: Any,
    registry: Any,
    token: str,
    job: Any,
    transport: Any,
    *,
    job_held: Callable[[], bool] | None = None,
) -> Any:
    """Execute one bounded citation-expansion job; returns an ``ExpansionReport``.

    The approved current plan and the current, intact corpus are revalidated
    **before** the first attempt (so an unapproved/stale plan, a stale corpus, or
    an unobserved root performs zero provider I/O), and again before every
    subsequent low-level attempt through ``before_request``. The traversal limits
    come only from ``plan.payload.expansion``. The report's per-relation
    observations are persisted through the existing discovery store, so an exact
    replay reuses them with no new provider request.
    """
    params = job.parameters if isinstance(job.parameters, dict) else {}
    plan_ref = params["plan_ref"]
    corpus_ref = params["corpus_ref"]
    roots = list(params["roots"])
    plan_document = _approved_expansion_plan(store, plan_ref)
    snapshot = _current_corpus(store, corpus_ref)
    _require_observed_roots(roots, snapshot)
    limits = _expansion_limits(plan_document)
    before_request = _expand_before_request(
        store, registry, token, plan_ref, corpus_ref, roots, job_held
    )
    return expand_citations(
        store,
        _expand_roots(roots),
        request_id=job.request_id,
        limits=limits,
        transport=transport,
        before_request=before_request,
    )


def dispatch_lead(
    store: Any,
    registry: Any,
    token: str,
    job: Any,
    *,
    api_key: str | None,
    lead_transport: Any = None,
) -> dict[str, Any]:
    """Execute one scoped hosted lead-proposal job (at most one POST).

    Delegates to the revision-scoped runner
    (:func:`~review_article_agent.discovery.lead_runner.execute_lead_proposal`),
    which re-derives the approved batch, revalidates the exact named hosted-use
    gate and every pinned source's rights, reserves on the **runtime dispatch
    ledger** before the one POST, and returns a safe status/dispatch mapping. A
    missing credential is refused here with **zero** POST.
    """
    from ..discovery.lead_runner import execute_lead_proposal

    params = job.parameters if isinstance(job.parameters, dict) else {}
    if not isinstance(api_key, str) or api_key == "":
        raise DiscoveryExecutionError(_M_MISSING_CREDENTIAL)
    return execute_lead_proposal(
        store,
        registry=registry,
        activation_token=token,
        batch_ref=params["batch_ref"],
        request_id=job.request_id,
        api_key=api_key,
        transport=lead_transport,
    )


def persisted_expansion_report(store: Any, *, job_id: str | None = None, request_id: str | None = None):
    """Return the full saved expansion mapping for a completed expand job, or None.

    A thin, validating reader for a later coverage reader: it refuses anything
    that is not a completed ``discovery_expand`` job whose accepted result is a
    non-authorizing ``discovery_expansion`` report. It never treats a supplied
    authoritative/authorizing flag as meaningful (the stored report always
    carries ``completeness_claimed=False`` / ``verified_support=False``).
    """
    from ..runtime.jobs import JobRepository

    repo = JobRepository(store)
    job = None
    if job_id is not None:
        job = repo.get(job_id)
    elif request_id is not None:
        for candidate in repo.list():
            if candidate.request_id == request_id:
                job = candidate
                break
    if job is None or mode_for_task_kind(job.task_kind) != MODE_EXPAND:
        return None
    accepted = repo.accepted_result(job.job_id)
    if accepted is None or accepted.result_kind != RESULT_KIND_EXPANSION:
        return None
    result = accepted.result
    if not isinstance(result, dict):
        return None
    guarantees = result.get("guarantees")
    if isinstance(guarantees, dict) and (
        guarantees.get("completeness_claimed") is not False
        or guarantees.get("verified_support") is not False
    ):
        return None
    return result


# --------------------------------------------------------------------------- #
# Dispatch through the S1 execution library
# --------------------------------------------------------------------------- #
def dispatch(
    store: Any,
    registry: Any,
    token: str,
    job: Any,
    transport: Any,
    *,
    job_held: Callable[[], bool] | None = None,
    lead_api_key: str | None = None,
    lead_transport: Any = None,
) -> Any:
    """Execute one discovery job through the S1 API under ``token``.

    The exact job request id is the S1 replay key; the S1 library derives a
    durable identity that binds the plan revision, its content digest, the query
    index, and the query fingerprint, and revalidates the approved plan and the
    activation before every attempt. A provider failure is recorded by S1 as an
    explicit partial observation and returned; only an approval/activation
    refusal or a local store fault raises. ``expand`` and ``lead`` route to their
    own bounded executors.
    """
    mode = mode_for_task_kind(job.task_kind)
    params = job.parameters if isinstance(job.parameters, dict) else {}
    if mode == MODE_ACQUIRE:
        # Acquisition has its own journaled runner (``acquisition.dispatch_acquire``).
        raise DiscoveryJobValidationError("bad_value", _M_BAD_MODE)
    if mode == MODE_EXPAND:
        return dispatch_expand(store, registry, token, job, transport, job_held=job_held)
    if mode == MODE_LEAD:
        return dispatch_lead(
            store, registry, token, job, api_key=lead_api_key, lead_transport=lead_transport
        )
    executor = DiscoveryExecutor(
        store, registry=registry, activation_token=token, transport=transport
    )
    if mode == MODE_QUERY:
        return executor.execute_query(
            params["plan_ref"], params["query_index"], request_id=job.request_id
        )
    if mode == MODE_SEED:
        return executor.seed_search(
            params["source"],
            params["query"],
            request_id=job.request_id,
            page_size=params["page_size"],
            max_pages=params["max_pages"],
        )
    if mode == MODE_METADATA_IMPORT:
        return executor.import_metadata(
            params["plan_ref"],
            source=params["source"],
            records=params["records"],
            origin_locator=params["origin_locator"],
            observed_at=params["observed_at"],
            request_id=job.request_id,
        )
    raise DiscoveryJobValidationError("bad_value", _M_BAD_MODE)


class _ReplayMiss(Exception):
    """Internal: an S1 replay lookup found no standing observation."""


def _replay_miss_transport(url: str, *, timeout: float, max_response_bytes: int) -> Any:
    # Reached only when S1 found NO existing observation: refuse any real I/O.
    raise _ReplayMiss()


@dataclass(frozen=True)
class ReplayedOutcome:
    """A durable outcome proven by replay, and whether it is a complete proof.

    ``complete`` is ``False`` only for an expansion whose traversal needs a
    provider request that was never persisted (an explicit missing remainder):
    the caller must escalate that rather than accept a silent completion.
    """

    result_kind: str
    result: Any
    complete: bool


def _replay_expand(store: Any, registry: Any, token: str, job: Any) -> ReplayedOutcome | None:
    """Redrive an expansion from stored relations only; never a new request.

    Every relation already persisted is reused verbatim. If any relation that the
    traversal needs was never persisted (a crash mid-traversal), the read-only
    repository refuses the write and the whole reconstruction fails closed: the
    caller must escalate — a missing remainder is never a silent completion. No
    low-level request is ever issued (a refusing ``before_request`` plus an
    exploding transport back that up).
    """
    from ..storage.discovery import DiscoveryRepository

    params = job.parameters if isinstance(job.parameters, dict) else {}
    try:
        plan_document = _approved_expansion_plan(store, params["plan_ref"])
        snapshot = _current_corpus(store, params["corpus_ref"])
        _require_observed_roots(params["roots"], snapshot)
        limits = _expansion_limits(plan_document)
    except Exception:  # noqa: BLE001 - cannot reconstruct: not a proven outcome
        return None

    class _ReadOnlyRepository(DiscoveryRepository):
        def record_search(self, *, request_id: str, result: Any) -> Any:  # type: ignore[override]
            # A relation that was never persisted cannot be reconstructed and is
            # never written from a replay.
            raise _ReplayMiss()

    def _refuse(_context: Any) -> object:
        return False

    def _explode(url: str, *, timeout: float, max_response_bytes: int) -> Any:
        raise _ReplayMiss()

    try:
        report = expand_citations(
            _ReadOnlyRepository(store),
            _expand_roots(list(params["roots"])),
            request_id=job.request_id,
            limits=limits,
            transport=_explode,
            before_request=_refuse,
        )
    except Exception:  # noqa: BLE001 - any failure means "not proven present"
        return None
    return ReplayedOutcome(
        result_kind=RESULT_KIND_EXPANSION,
        result=report.to_mapping(),
        complete=True,
    )


_LEAD_ACCEPTED_STATUSES = frozenset(
    {"completed", "refused", "protocol_error", "oversized_output", "failed"}
)


def lead_outcome_accepted(mapping: object) -> bool:
    """True when a hosted-lead outcome is a resolved, persistable lifecycle result."""
    return isinstance(mapping, dict) and mapping.get("status") in _LEAD_ACCEPTED_STATUSES


def _replay_lead(store: Any, job: Any) -> ReplayedOutcome | None:
    """Read a saved hosted-lead outcome from durable rows (no credential needed).

    A terminal accepted outcome is a complete proof; an ``unknown``/``submitted``
    outcome is **not** (it must be escalated to a human, and is never re-sent).
    """
    from ..discovery.lead_runner import get_lead_outcome

    try:
        mapping = get_lead_outcome(store, job.request_id)
    except Exception:  # noqa: BLE001 - not provable
        return None
    if mapping is None:
        return None
    status = mapping.get("status")
    return ReplayedOutcome(
        result_kind=RESULT_KIND_LEAD,
        result=mapping,
        complete=status in _LEAD_ACCEPTED_STATUSES,
    )


def _replay_acquire(store: Any, registry: Any, token: str, job: Any) -> ReplayedOutcome | None:
    """Reconstruct an acquisition result from the verified E1 report (zero GET)."""
    proven = acquisition_jobs.replay_acquire(store, registry, token, job)
    if proven is None:
        return None
    summary, complete = proven
    return ReplayedOutcome(
        result_kind=RESULT_KIND_ACQUISITION,
        result=acquisition_jobs.accepted_result(summary),
        complete=complete,
    )


def replay_only(store: Any, registry: Any, token: str, job: Any) -> ReplayedOutcome | None:
    """Prove an existing durable outcome for ``job`` without any provider I/O.

    Query/seed/import use the S1 replay short-circuit (identical caller key + plan
    binding). Expansion reuses only persisted relations and never issues a request.
    Hosted lead reads its durable outcome from the runtime ledger without needing a
    credential. A missing/rejected plan, a stale activation, an absent observation,
    or any other refusal returns ``None``.
    """
    mode = mode_for_task_kind(job.task_kind)
    if mode == MODE_EXPAND:
        return _replay_expand(store, registry, token, job)
    if mode == MODE_LEAD:
        return _replay_lead(store, job)
    if mode == MODE_ACQUIRE:
        return _replay_acquire(store, registry, token, job)
    try:
        record = dispatch(store, registry, token, job, _replay_miss_transport)
    except Exception:  # noqa: BLE001 - any failure means "not proven present"
        return None
    return ReplayedOutcome(
        result_kind=RESULT_KIND_OBSERVATION,
        result=observation_metadata(job, record),
        complete=True,
    )
