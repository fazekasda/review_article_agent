"""Owner-controlled detached **official full-text retrieval** jobs (Step 06 slice J1).

Authority
---------
``docs/implementation/06-acquisition.md`` deliverable 1 and acceptance criteria
AC6/AC7 (no unofficial acquisition; storage/redistribution stay distinct from
free-to-read); the product CLI contract (a CLI disconnect is **not** a cancel;
one execution-active project; explicit stop/recovery); and the already-integrated
discovery job engine pattern (:mod:`review_article_agent.worker.discovery`).

What this module is
-------------------
The thin glue between the durable worker engine and the integrated
:class:`~review_article_agent.acquisition.execution.AcquisitionExecutor` (E1). It:

* strictly and **purely** validates and canonicalises the owner's ``acquire``
  submission (no store read, no clock beyond the terms check, no I/O) into the
  immutable ``JobRepository`` parameters of an ``acquisition_fulltext`` job;
* forwards exactly those parameters to ``AcquisitionExecutor.acquire`` with the
  durable **job request id** as the only replay key (no second caller key, no
  authority flag, no URL, no callback in the JSON);
* keeps a bounded, cumulative **attempt journal** in the job attempt checkpoint:
  a *may-have-dispatched* entry is persisted under the registry write guard
  **before** every GET, and the safe observation metadata is persisted after it;
* projects the E1 result into a safe accepted result (no bytes, URLs, identifiers,
  terms, or rights) and proves an existing result by replay with **zero** I/O.

What it is not
--------------
It never fetches an unofficial source, never makes a scientific/eligibility
decision (``eligibility_not_assessed`` always), reserves no paid cost (no model
call), and never re-sends a request whose outcome is unknown.

Journal schema (``attempt.checkpoint``)
---------------------------------------
``{"discovery_predispatch": true, "phase": "discovery_predispatch", "mode":
"acquire", "acquisition": {"journal_version": 1, "max_get": <int>, "pending":
null | {"attempt_index", "source"}, "attempts": [{"attempt_index", "source",
"outcome", "status", "byte_count", "body_sha256", "truncated", "started_at",
"completed_at", "failure"}, ...]}}``. The journal never contains source bytes,
base64, a preview, a URL, or terms. It is cumulative and bounded by
``max_get`` (<= ``2 * max_attempts``; the actual maximum GET count).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Callable

from ..acquisition.execution import (
    MAX_REQUEST_ID_CHARS,
    AcquisitionExecutionError,
    AcquisitionExecutor,
)
from ..acquisition.imports import AssetImportRepository, _NestedStore
from ..acquisition.resolution import MAX_REQUESTS
from ..acquisition.transport import (
    DEFAULT_MAX_ATTEMPTS,
    MAX_ATTEMPTS,
    FullTextTransport,
    TransportNetworkError,
)
from ..domain.errors import ReviewAgentError
from ..domain.records import (
    RecordValidationError,
    validate_identifier,
    validate_ref,
    validate_rights_record,
)
from ..runtime.jobs import JobRepository

__all__ = [
    "MODE_ACQUIRE",
    "TASK_KIND_ACQUIRE",
    "RESULT_KIND_ACQUISITION",
    "ACQUIRE_REQUIRED_KEYS",
    "ACQUIRE_OPTIONAL_KEYS",
    "TRANSPORT_OPTION_KEYS",
    "AcquireJournal",
    "AcquireRun",
    "validate_acquire",
    "dispatch_acquire",
    "replay_acquire",
    "accepted_result",
    "status_projection",
    "journal_checkpoint",
]

MODE_ACQUIRE = "acquire"
TASK_KIND_ACQUIRE = "acquisition_fulltext"
RESULT_KIND_ACQUISITION = "acquisition_retrieval"

ACQUIRE_REQUIRED_KEYS = frozenset(
    {
        "freeze_ref",
        "candidate_id",
        "source_artifact_id",
        "source_revision_id",
        "source_rights",
        "occurrence_revision_id",
        "occurrence_rights",
        "report_revision_id",
        "report_rights",
        "terms_evidence",
    }
)
ACQUIRE_OPTIONAL_KEYS = frozenset({"parent_revision_id", "transport_options"})

#: Bounded technical transport values a submission may carry (numbers only).
TRANSPORT_OPTION_KEYS = frozenset(
    {
        "timeout",
        "max_response_bytes",
        "max_attempts",
        "retry_delay",
        "max_retry_delay",
        "rate_spacing",
    }
)

_PHASE = "discovery_predispatch"
_OPS = ("acquisition", "storage")
_UTC_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|\+00:00)$"
)
#: ``interrupted_reason`` values meaning the journal could not follow a sent GET.
_AMBIGUOUS_REASONS = frozenset({"after_attempt_callback_failed"})

_M_BAD_PARAMETERS = "parameters must be an exact object for the acquire mode"
_M_BAD_KEY = "parameters contained an unknown or missing field"
_M_BAD_REQUEST_ID = "the job request id must be a bounded opaque identifier"
_M_BAD_REF = "freeze_ref must be an exact same-project revision reference"
_M_BAD_IDENT = "an identifier field is invalid or out of bounds"
_M_BAD_RIGHTS = "a rights record is invalid for its exact subject or not eligible"
_M_BAD_TERMS = "terms_evidence must name the per-operation terms and a past UTC check time"
_M_BAD_OPTIONS = "transport_options must be bounded technical numbers only"
_M_BAD_DISTINCT = "source, occurrence and report identities must be distinct"


def _error(code: str, message: str):
    # Local import: ``discovery`` imports this module for delegation.
    from .discovery import DiscoveryJobValidationError

    return DiscoveryJobValidationError(code, message)


def _bad_value(message: str):
    return _error("bad_value", message)


def _bad_type(message: str):
    return _error("bad_type", message)


# --------------------------------------------------------------------------- #
# Pure validation
# --------------------------------------------------------------------------- #
def _ident(value: object, message: str = _M_BAD_IDENT) -> str:
    if not isinstance(value, str):
        raise _bad_type(message)
    try:
        validate_identifier(value, "identifier")
    except (RecordValidationError, TypeError, ValueError):
        raise _bad_value(message) from None
    if len(value) > MAX_REQUEST_ID_CHARS:
        raise _bad_value(message)
    return value


def _ref(value: object, project_id: str) -> dict[str, str]:
    try:
        ref = validate_ref(value, project_id)
    except (RecordValidationError, TypeError, ValueError):
        raise _bad_value(_M_BAD_REF) from None
    for part in ref.values():
        _ident(part, _M_BAD_REF)
    return {k: ref[k] for k in ("project_id", "artifact_id", "revision_id")}


def _subject(project_id: str, artifact_id: str, revision_id: str) -> dict[str, str]:
    return {
        "kind": "source_revision",
        "project_id": project_id,
        "artifact_id": artifact_id,
        "revision_id": revision_id,
    }


def _rights(record: object, project_id: str, subject: dict, *, source: bool) -> dict:
    if not isinstance(record, dict):
        raise _bad_type(_M_BAD_RIGHTS)
    try:
        validated = validate_rights_record(record, project_id, subject=subject)
    except (RecordValidationError, TypeError, ValueError):
        raise _bad_value(_M_BAD_RIGHTS) from None
    if validated["privacy"]["denial"] is not False:
        raise _bad_value(_M_BAD_RIGHTS)
    if source and (
        validated["operations"]["acquisition"] != "allowed"
        or validated["operations"]["storage"] != "allowed"
    ):
        raise _bad_value(_M_BAD_RIGHTS)
    if not source and validated["operations"]["storage"] != "allowed":
        raise _bad_value(_M_BAD_RIGHTS)
    return validated


def _terms(evidence: object, source_rights: dict) -> dict[str, dict[str, str]]:
    if not isinstance(evidence, dict) or set(evidence) != set(_OPS):
        raise _bad_value(_M_BAD_TERMS)
    now = datetime.now(timezone.utc)
    out: dict[str, dict[str, str]] = {}
    for op in _OPS:
        entry = evidence[op]
        if not isinstance(entry, dict) or set(entry) != {"terms_ref", "checked_at"}:
            raise _bad_value(_M_BAD_TERMS)
        ref, checked = entry["terms_ref"], entry["checked_at"]
        expected = source_rights["basis"][op]["evidence"]["ref"]
        if not isinstance(ref, str) or ref == "" or ref != expected or len(ref) > 512:
            raise _bad_value(_M_BAD_TERMS)
        if not isinstance(checked, str) or _UTC_RE.fullmatch(checked) is None:
            raise _bad_value(_M_BAD_TERMS)
        try:
            parsed = datetime.fromisoformat(
                checked[:-1] + "+00:00" if checked.endswith("Z") else checked
            )
        except ValueError:
            raise _bad_value(_M_BAD_TERMS) from None
        if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0) or parsed > now:
            raise _bad_value(_M_BAD_TERMS)
        out[op] = {"terms_ref": ref, "checked_at": checked}
    return out


def _transport_options(value: object) -> dict[str, int | float]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise _bad_type(_M_BAD_OPTIONS)
    if not set(value) <= TRANSPORT_OPTION_KEYS:
        raise _bad_value(_M_BAD_OPTIONS)
    options: dict[str, int | float] = {}
    for key in sorted(value):
        item = value[key]
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            raise _bad_type(_M_BAD_OPTIONS)
        options[key] = item
    try:
        # Bounds are enforced by the transport itself; this performs no I/O.
        FullTextTransport(transport=_never_called, **options)
    except Exception:  # noqa: BLE001 - any out-of-bound value is a refusal
        raise _bad_value(_M_BAD_OPTIONS) from None
    return options


def _never_called(url, *, timeout, max_response_bytes):  # pragma: no cover
    raise TransportNetworkError("validation transport must never be called")


def report_artifact_id(project_id: str, request_id: str) -> str:
    return AcquisitionExecutor.report_artifact_id(
        SimpleNamespace(_project_id=project_id), request_id
    )


def occurrence_artifact_id(project_id: str, request_id: str) -> str:
    return AssetImportRepository.occurrence_artifact_id(
        SimpleNamespace(project_id=project_id), request_id
    )


def validate_acquire(
    parameters: object, project_id: str, request_id: object
) -> dict[str, Any]:
    """Strictly validate and canonicalise one ``acquire`` submission.

    Pure: no store read, no I/O. Raises a ``DiscoveryJobValidationError``
    (``bad_type``/``bad_value``) before any durable write, activation change or
    GET. The authoritative freeze/rights/lease checks are repeated by E1 before
    the first GET.
    """
    if not isinstance(parameters, dict):
        raise _bad_type(_M_BAD_PARAMETERS)
    keys = set(parameters)
    if not ACQUIRE_REQUIRED_KEYS <= keys or not keys <= (
        ACQUIRE_REQUIRED_KEYS | ACQUIRE_OPTIONAL_KEYS
    ):
        raise _bad_value(_M_BAD_KEY)
    if not isinstance(request_id, str):
        raise _bad_value(_M_BAD_REQUEST_ID)
    request_id = _ident(request_id, _M_BAD_REQUEST_ID)

    source_artifact_id = _ident(parameters["source_artifact_id"])
    source_revision_id = _ident(parameters["source_revision_id"])
    occurrence_revision_id = _ident(parameters["occurrence_revision_id"])
    report_revision_id = _ident(parameters["report_revision_id"])
    parent = parameters.get("parent_revision_id")
    parent = None if parent is None else _ident(parent)
    report_id = report_artifact_id(project_id, request_id)
    occ_id = occurrence_artifact_id(project_id, request_id)
    if len({source_artifact_id, report_id, occ_id}) != 3:
        raise _bad_value(_M_BAD_DISTINCT)

    source = _rights(
        parameters["source_rights"], project_id,
        _subject(project_id, source_artifact_id, source_revision_id), source=True,
    )
    occurrence = _rights(
        parameters["occurrence_rights"], project_id,
        _subject(project_id, occ_id, occurrence_revision_id), source=False,
    )
    report = _rights(
        parameters["report_rights"], project_id,
        _subject(project_id, report_id, report_revision_id), source=False,
    )
    if len({source["rights_id"], occurrence["rights_id"], report["rights_id"]}) != 3:
        raise _bad_value(_M_BAD_DISTINCT)

    return {
        "freeze_ref": _ref(parameters["freeze_ref"], project_id),
        "candidate_id": _ident(parameters["candidate_id"]),
        "source_artifact_id": source_artifact_id,
        "source_revision_id": source_revision_id,
        "source_rights": source,
        "occurrence_revision_id": occurrence_revision_id,
        "occurrence_rights": occurrence,
        "report_revision_id": report_revision_id,
        "report_rights": report,
        "terms_evidence": _terms(parameters["terms_evidence"], source),
        "parent_revision_id": parent,
        "transport_options": _transport_options(parameters.get("transport_options")),
    }


# --------------------------------------------------------------------------- #
# Journal
# --------------------------------------------------------------------------- #
def journal_checkpoint(
    entries: list[dict[str, Any]], pending: dict[str, Any] | None, max_get: int
) -> dict[str, Any]:
    """The cumulative attempt checkpoint (keeps the predispatch marker)."""
    return {
        "discovery_predispatch": True,
        "phase": _PHASE,
        "mode": MODE_ACQUIRE,
        "acquisition": {
            "journal_version": 1,
            "max_get": max_get,
            "pending": pending,
            "attempts": [dict(entry) for entry in entries],
        },
    }


_ENTRY_KEYS = (
    "attempt_index", "source", "outcome", "status", "byte_count", "body_sha256",
    "truncated", "started_at", "completed_at", "failure",
)


class AcquireJournal:
    """Persist a may-have-dispatched mark before, and the safe metadata after, each GET.

    Every write is fenced by the **captured original dispatch token** (not the
    project's *current* token): a released/re-acquired same-project lease can
    never silently authorize this old run. The *before* (send-admission) write
    additionally re-reads the durable job/attempt/fence and the paused/cancelled
    state **inside the same SQLite write transaction as the checkpoint**, so no
    GET can be admitted once a pause/cancel has committed (a *legitimate*
    admission that committed first may outlive a later pause). The *after* record
    uses ``allow_draining=True`` and never re-checks the held state, so an
    observation is never lost to a pause/drain that happened after the send.
    """

    def __init__(
        self,
        store: Any,
        registry: Any,
        token: str,
        *,
        project_id: str,
        job_id: str,
        attempt_id: str,
        fence: int,
        max_get: int,
    ):
        self._store = store
        self._registry = registry
        self._token = token
        self._project_id = project_id
        self._job_id = job_id
        self._attempt_id = attempt_id
        self._fence = fence
        # Nested repo so the checkpoint write shares the admission transaction
        # (a SAVEPOINT inside it) rather than opening a second BEGIN IMMEDIATE.
        self._repo = JobRepository(_NestedStore(store))
        self.max_get = max_get
        self.entries: list[dict[str, Any]] = []
        self.pending: dict[str, Any] | None = None
        self.failed = False

    @property
    def sent(self) -> bool:
        """True when any GET may have been dispatched."""
        return bool(self.entries) or self.pending is not None

    def _guard(self, allow_draining: bool) -> Any:
        return self._registry.execution_write_guard(
            self._token, self._project_id, allow_draining=allow_draining
        )

    def _admission_check(self) -> None:
        """Refuse the send unless it is linearizable with pause/cancel/switch.

        Runs inside the admission SQLite write transaction: it re-reads the job
        row (current attempt, fence) and the attempt state, and refuses when the
        job is paused/cancelled or the fence has moved on. The registry guard
        already re-checked the project/lease (active only) under the captured
        original token.
        """
        conn = self._store.connection
        row = conn.execute(
            "SELECT state, current_attempt_id, current_fence FROM job "
            "WHERE project_id = ? AND job_id = ?",
            (self._project_id, self._job_id),
        ).fetchone()
        if (
            row is None
            or row["current_attempt_id"] != self._attempt_id
            or row["current_fence"] != self._fence
            or row["state"] in ("paused", "cancelled")
        ):
            raise ReviewAgentError(
                "the acquisition job is held or its fence moved; refusing to "
                "admit a provider request."
            )
        attempt = conn.execute(
            "SELECT state FROM job_attempt WHERE project_id = ? AND attempt_id = ?",
            (self._project_id, self._attempt_id),
        ).fetchone()
        if attempt is None or attempt["state"] not in ("queued", "admitted", "running"):
            raise ReviewAgentError(
                "the acquisition attempt is not in an admissible state; refusing "
                "to admit a provider request."
            )

    def _persist(self, allow_draining: bool) -> None:
        checkpoint = journal_checkpoint(self.entries, self.pending, self.max_get)
        with self._guard(allow_draining):
            if allow_draining:
                self._repo.record_checkpoint(self._attempt_id, checkpoint)
            else:
                with self._store.transaction():
                    self._admission_check()
                    self._repo.record_checkpoint(self._attempt_id, checkpoint)

    def before(self, info: dict[str, Any]) -> None:
        if len(self.entries) >= self.max_get:
            raise ReviewAgentError("acquisition journal bound reached")
        self.pending = {
            "attempt_index": int(info["attempt_index"]),
            "source": str(info["source"]),
        }
        try:
            self._persist(False)
        except BaseException:
            # The mark may not be durable and nothing was sent yet: no pending.
            self.pending = None
            raise

    def after(self, info: dict[str, Any]) -> None:
        entry = {key: info.get(key) for key in _ENTRY_KEYS}
        self.entries.append(entry)
        self.pending = None
        try:
            self._persist(True)
        except BaseException:
            self.failed = True
            raise


# --------------------------------------------------------------------------- #
# Dispatch / replay
# --------------------------------------------------------------------------- #
@dataclass
class AcquireRun:
    """The classified outcome of one ``acquire`` dispatch."""

    summary: dict[str, Any] | None
    error_code: str | None
    blocked: bool
    sent: bool
    journal_failed: bool


def _max_get(options: dict[str, Any]) -> int:
    attempts = options.get("max_attempts", DEFAULT_MAX_ATTEMPTS)
    return MAX_REQUESTS * min(int(attempts), MAX_ATTEMPTS)


def _forward(params: dict[str, Any], request_id: str) -> dict[str, Any]:
    return {
        "request_id": request_id,
        "source_artifact_id": params["source_artifact_id"],
        "source_revision_id": params["source_revision_id"],
        "source_rights": params["source_rights"],
        "occurrence_revision_id": params["occurrence_revision_id"],
        "occurrence_rights": params["occurrence_rights"],
        "report_revision_id": params["report_revision_id"],
        "report_rights": params["report_rights"],
        "terms_evidence": params["terms_evidence"],
        "parent_revision_id": params.get("parent_revision_id"),
    }


def dispatch_acquire(
    store: Any,
    registry: Any,
    token: str,
    job: Any,
    *,
    attempt_id: str,
    fence: int,
    lowlevel: Callable[..., Any],
    stop_requested: Callable[[], bool],
) -> AcquireRun:
    """Run one ``acquire`` job through E1 with the durable attempt journal.

    ``token`` is the exact lease captured when this dispatch started; the journal
    fences every checkpoint on it (never the project's current token).
    """
    from ..acquisition.execution import AcquisitionExecutionBlockedError

    params = job.parameters if isinstance(job.parameters, dict) else {}
    options = dict(params.get("transport_options") or {})
    journal = AcquireJournal(
        store,
        registry,
        token,
        project_id=job.project_id,
        job_id=job.job_id,
        attempt_id=attempt_id,
        fence=fence,
        max_get=_max_get(options),
    )
    try:
        executor = AcquisitionExecutor(
            store,
            registry=registry,
            activation_token=token,
            lowlevel=lowlevel,
            transport_options=options,
            stop_requested=stop_requested,
            before_attempt=journal.before,
            after_attempt=journal.after,
        )
        summary = executor.acquire(
            params["freeze_ref"],
            params["candidate_id"],
            **_forward(params, job.request_id),
        )
    except AcquisitionExecutionError as exc:
        return AcquireRun(
            None, exc.code, isinstance(exc, AcquisitionExecutionBlockedError),
            journal.sent, journal.failed,
        )
    except BaseException:  # noqa: BLE001 - fail closed; the GET may have happened
        return AcquireRun(None, "indeterminate", False, journal.sent, journal.failed)
    return AcquireRun(summary, None, False, journal.sent, journal.failed)


def _no_io(url, *, timeout, max_response_bytes):  # pragma: no cover - never reached
    raise TransportNetworkError("replay must not issue a request")


def replay_acquire(store: Any, registry: Any, token: str, job: Any):
    """Prove an existing result by exact replay with **zero** network, or ``None``.

    Returns ``(summary, complete)``. A missing report, a divergent request, a
    stale lease, or a report whose journal could not follow a sent GET is never a
    silent completion (``None`` or ``complete=False``).
    """
    params = job.parameters if isinstance(job.parameters, dict) else {}
    try:
        executor = AcquisitionExecutor(
            store,
            registry=registry,
            activation_token=token,
            lowlevel=_no_io,
            transport_options=dict(params.get("transport_options") or {}),
            stop_requested=lambda: True,  # a miss must never reach a GET
        )
        summary = executor.acquire(
            params["freeze_ref"],
            params["candidate_id"],
            **_forward(params, job.request_id),
        )
    except Exception:  # noqa: BLE001 - not a proven present result
        return None
    report = summary["report"]
    complete = not (
        report["status"] == "interrupted"
        and report["interrupted_reason"] in _AMBIGUOUS_REASONS
    )
    return summary, complete


# --------------------------------------------------------------------------- #
# Safe result projection
# --------------------------------------------------------------------------- #
def accepted_result(summary: dict[str, Any]) -> dict[str, Any]:
    """Reduce an E1 summary to the safe accepted-result mapping.

    Only report/source/occurrence refs, status, counts and the validation status.
    Never the report body, URLs, identifiers, terms, rights or any bytes.
    """
    report = summary["report"]
    return {
        "kind": RESULT_KIND_ACQUISITION,
        "mode": MODE_ACQUIRE,
        "report_ref": dict(summary["report_ref"]),
        "status": summary["status"],
        "source_ref": summary["source_ref"],
        "occurrence_ref": summary["occurrence_ref"],
        "disposition": summary["disposition"],
        "validation_status": summary["validation_status"],
        "validation_check": summary["validation_check"],
        "attempt_count": summary["attempt_count"],
        "failure_count": len(report["failures"]),
        "interrupted_reason": report["interrupted_reason"],
        "authorizing": False,
        "eligibility": "eligibility_not_assessed",
    }


_SAFE_KEYS = (
    "kind", "mode", "report_ref", "status", "source_ref", "occurrence_ref",
    "disposition", "validation_status", "validation_check", "attempt_count",
    "failure_count", "interrupted_reason", "authorizing", "eligibility",
)


def status_projection(result: Any) -> Any:
    """Bound a saved accepted result for ``job_status`` (safe keys only)."""
    if not isinstance(result, dict):
        return result
    return {key: result[key] for key in _SAFE_KEYS if key in result}
