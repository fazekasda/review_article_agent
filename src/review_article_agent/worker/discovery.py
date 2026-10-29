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
import re
from datetime import datetime, timedelta
from typing import Any, Callable

from ..discovery.execution import (
    MAX_IMPORT_RECORDS,
    MAX_ORIGIN_LOCATOR_CHARS,
    PLAN_QUERY_SOURCES,
    SEED_MAX_PAGES,
    SEED_MAX_RECORDS,
    DiscoveryExecutor,
    query_fingerprint,
)
from ..discovery.records import MetadataRecord
from ..discovery.transport import DiscoveryRequestInterrupted
from ..domain.errors import ReviewAgentError
from ..domain.records import (
    RecordValidationError,
    canonical_json,
    validate_ref,
)
from ..runtime import ipc

__all__ = [
    "MODE_QUERY",
    "MODE_SEED",
    "MODE_METADATA_IMPORT",
    "DISCOVERY_MODES",
    "TASK_KIND_QUERY",
    "TASK_KIND_SEED",
    "TASK_KIND_IMPORT",
    "DISCOVERY_TASK_KINDS",
    "MODE_PARAMETER_KEYS",
    "MAX_DISCOVERY_PARAMETERS_BYTES",
    "PREDISPATCH_PHASE",
    "DiscoveryJobError",
    "DiscoveryJobValidationError",
    "DiscoveryJobStopped",
    "AttemptCounter",
    "guard_transport",
    "mode_for_task_kind",
    "task_kind_for_mode",
    "is_discovery_task_kind",
    "predispatch_checkpoint",
    "is_predispatch_checkpoint",
    "observation_metadata",
    "validate_submission",
    "dispatch",
    "replay_only",
]

# --------------------------------------------------------------------------- #
# Protocol vocabulary
# --------------------------------------------------------------------------- #
#: The three supported operation modes (one durable job task kind each).
MODE_QUERY = "query"
MODE_SEED = "seed"
MODE_METADATA_IMPORT = "metadata_import"

#: Every accepted discovery mode.
DISCOVERY_MODES = (MODE_QUERY, MODE_SEED, MODE_METADATA_IMPORT)

#: Durable job task kinds (the existing ``JobRepository`` accepts these strings).
TASK_KIND_QUERY = "discovery_query"
TASK_KIND_SEED = "discovery_seed"
TASK_KIND_IMPORT = "discovery_import"

#: Every discovery job task kind.
DISCOVERY_TASK_KINDS = frozenset({TASK_KIND_QUERY, TASK_KIND_SEED, TASK_KIND_IMPORT})

_MODE_BY_TASK_KIND = {
    TASK_KIND_QUERY: MODE_QUERY,
    TASK_KIND_SEED: MODE_SEED,
    TASK_KIND_IMPORT: MODE_METADATA_IMPORT,
}
_TASK_KIND_BY_MODE = {mode: kind for kind, mode in _MODE_BY_TASK_KIND.items()}

#: Exact, strict parameter keys per mode (no actor/authority flag is permitted).
MODE_PARAMETER_KEYS: dict[str, frozenset[str]] = {
    MODE_QUERY: frozenset({"plan_ref", "query_index"}),
    MODE_SEED: frozenset({"source", "query", "page_size", "max_pages"}),
    MODE_METADATA_IMPORT: frozenset(
        {"plan_ref", "source", "records", "origin_locator", "observed_at"}
    ),
}

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
_M_TOO_LARGE = "the discovery parameters exceed the control-transport byte bound"
_M_BAD_TRANSPORT = "the low-level discovery transport must be callable"
_M_INTERRUPTED = "the discovery job was stopped before the next provider request"


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
    mode: object, parameters: object, project_id: str
) -> dict[str, Any]:
    """Strictly validate and canonicalise one discovery submission.

    Returns the exact, immutable parameter dict that becomes the durable
    ``JobRepository`` job's ``parameters`` (so a divergent replay under the same
    request id is a job conflict). Raises :class:`DiscoveryJobValidationError`
    **before** any durable write, activation change, or provider I/O.
    """
    if mode not in DISCOVERY_MODES:
        raise _bad_value(_M_BAD_MODE)
    allowed = MODE_PARAMETER_KEYS[mode]
    params = _require_keys(parameters, allowed)

    if mode == MODE_QUERY:
        canonical = {
            "plan_ref": _validate_plan_ref(params["plan_ref"], project_id),
            "query_index": _validate_query_index(params["query_index"]),
        }
    elif mode == MODE_SEED:
        canonical = _validate_seed(params)
    else:
        canonical = _validate_import(params, project_id)

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


# --------------------------------------------------------------------------- #
# Dispatch through the S1 execution library
# --------------------------------------------------------------------------- #
def dispatch(store: Any, registry: Any, token: str, job: Any, transport: Any) -> Any:
    """Execute one discovery job through the S1 API under ``token``.

    The exact job request id is the S1 replay key; the S1 library derives a
    durable identity that binds the plan revision, its content digest, the query
    index, and the query fingerprint, and revalidates the approved plan and the
    activation before every attempt. A provider failure is recorded by S1 as an
    explicit partial observation and returned; only an approval/activation
    refusal or a local store fault raises.
    """
    mode = mode_for_task_kind(job.task_kind)
    params = job.parameters if isinstance(job.parameters, dict) else {}
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


def replay_only(store: Any, registry: Any, token: str, job: Any) -> Any | None:
    """Return an existing observation for ``job`` without any provider I/O.

    Uses the S1 replay short-circuit (identical caller key + plan binding). A
    missing/rejected plan, a stale activation, an absent observation, or any
    other refusal returns ``None``. A metadata import (a purely local, bounded
    write) may (re)record its *identical* observation; the S1 replay still
    returns the historical row unchanged on a repeat.
    """
    try:
        return dispatch(store, registry, token, job, _replay_miss_transport)
    except Exception:  # noqa: BLE001 - any failure means "not proven present"
        return None
