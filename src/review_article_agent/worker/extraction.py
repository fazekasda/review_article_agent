"""Owner-controlled detached **extraction/quality run** jobs (Step 09, S9-WORKER).

Authority
---------
``docs/implementation/09-extraction.md`` and ``docs/specification.md`` line 32
(grounded extraction from *original* text only), and
``docs/decisions/004-step09-direct-extraction.md`` (the user-approved direct
OpenRouter exception for bounded, tool-free extraction/quality). The durable job
plumbing mirrors the Step 05 discovery **lead** job and the Step 06 **acquire**
job: this module is only the thin glue between the durable worker engine and the
already-integrated :func:`review_article_agent.extraction.runner.execute_run`.

What this module is
-------------------
* a strict, **pure** validator/canonicaliser for one ``extraction_run`` submission
  (mirroring ``execute_run``'s keyword arguments: ``config_ref``, ``report_refs``,
  ``purpose``, ``mode``, ``hosted_ref``, the optional ``extraction_ref`` /
  ``pilot_ref``, the receipt ``artifact_id``/``revision_id``, and the caller's
  ``rights`` record) into the immutable ``JobRepository`` parameters — no store
  read, no I/O, no credential in the parameters;
* a single dispatch that calls :func:`~review_article_agent.extraction.runner.execute_run`
  with the credential supplied by the worker (from the one named environment
  variable), the job request id as the only replay key, and the worker's own
  ``transport`` seam;
* a safe accepted-result projection that references the run **receipt** (the
  runner's readback mapping) and never carries the credential, the model output,
  or any private material;
* a replay-only proof that reads a recorded run back from durable rows with
  **zero** POST, so an ambiguous outcome is never re-sent on recovery.

What this module is **not**
---------------------------
* It performs **no** approval, gate check, rights check, reserve, or scientific
  decision: :func:`execute_run` owns all of those and revalidates them before and
  immediately prior to its single POST. A recorded run is inert provenance.
* It never accepts, sends, persists, or logs the credential **value**; only the
  one fixed environment-variable **name** is carried in the parameters.
"""

from __future__ import annotations

from typing import Any

from ..domain.records import (
    RecordValidationError,
    validate_identifier,
    validate_ref,
    validate_rights_record,
)
from ..extraction.runner import MODES, PURPOSES

__all__ = [
    "MODE_EXTRACT",
    "TASK_KIND_EXTRACT",
    "RESULT_KIND_EXTRACTION",
    "EXTRACT_CREDENTIAL_ENV",
    "EXTRACT_REQUIRED_KEYS",
    "EXTRACT_OPTIONAL_KEYS",
    "ExtractRun",
    "validate_extract",
    "dispatch_extract",
    "replay_extract",
    "accepted_result",
    "status_projection",
    "persisted_extraction_run",
]

#: The one durable operation mode (and its task kind) for an extraction run.
MODE_EXTRACT = "extraction_run"
TASK_KIND_EXTRACT = "extraction_run"
#: The safe job-accepted-result kind of one extraction run.
RESULT_KIND_EXTRACTION = "extraction_run"

#: The one, fixed environment-variable name an extraction run may name. It is the
#: same hosted direct-OpenRouter credential the lead job uses; the value is never
#: accepted over IPC, persisted, or logged.
EXTRACT_CREDENTIAL_ENV = "OPENROUTER_API_KEY"

#: Exact, strict parameter keys (no actor/authority flag is permitted).
EXTRACT_REQUIRED_KEYS = frozenset(
    {
        "config_ref",
        "report_refs",
        "purpose",
        "mode",
        "hosted_ref",
        "artifact_id",
        "revision_id",
        "rights",
        "credential_env",
    }
)
EXTRACT_OPTIONAL_KEYS = frozenset({"extraction_ref", "pilot_ref"})

_REF_KEYS = ("project_id", "artifact_id", "revision_id")
_OPS_STORAGE = "storage"

_M_BAD_PARAMETERS = "parameters must be an exact object for the extraction_run mode"
_M_BAD_KEY = "parameters contained an unknown or missing field"
_M_BAD_CREDENTIAL_ENV = "credential_env must name the one approved environment variable"
_M_MISSING_CREDENTIAL = "the named extraction credential is not available to the worker"
_M_BAD_REF = "a reference must be an exact same-project revision reference"
_M_BAD_REPORTS = "report_refs must be a non-empty list of distinct same-project references"
_M_BAD_PURPOSE = "purpose must be 'extraction' or 'quality'"
_M_BAD_MODE = "mode must be 'pilot' or 'bulk'"
_M_BAD_IDENT = "an artifact/revision identifier is invalid or out of bounds"
_M_BAD_RIGHTS = "a rights record is invalid for its exact subject or not eligible"
_M_BAD_AUTHORITY = "extraction_ref/pilot_ref do not match the requested purpose/mode"


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
def _ident(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise _bad_type(_M_BAD_IDENT)
    try:
        return validate_identifier(value, field)
    except (RecordValidationError, TypeError, ValueError):
        raise _bad_value(_M_BAD_IDENT) from None


def _ref(value: object, project_id: str) -> dict[str, str]:
    try:
        ref = validate_ref(value, project_id)
    except (RecordValidationError, TypeError, ValueError):
        raise _bad_value(_M_BAD_REF) from None
    return {key: ref[key] for key in _REF_KEYS}


def _optional_ref(value: object, project_id: str) -> dict[str, str] | None:
    if value is None:
        return None
    return _ref(value, project_id)


def _report_refs(value: object, project_id: str) -> list[dict[str, str]]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, (list, tuple)):
        raise _bad_type(_M_BAD_REPORTS)
    if not value:
        raise _bad_value(_M_BAD_REPORTS)
    refs: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for item in value:
        ref = _ref(item, project_id)
        key = (ref["project_id"], ref["artifact_id"], ref["revision_id"])
        if key in seen:
            raise _bad_value(_M_BAD_REPORTS)
        seen.add(key)
        refs.append(ref)
    return refs


def _subject(project_id: str, artifact_id: str, revision_id: str) -> dict[str, str]:
    return {
        "kind": "source_revision",
        "project_id": project_id,
        "artifact_id": artifact_id,
        "revision_id": revision_id,
    }


def _rights(record: object, project_id: str, artifact_id: str, revision_id: str) -> dict:
    if not isinstance(record, dict):
        raise _bad_type(_M_BAD_RIGHTS)
    try:
        validated = validate_rights_record(
            record, project_id, subject=_subject(project_id, artifact_id, revision_id)
        )
    except (RecordValidationError, TypeError, ValueError):
        raise _bad_value(_M_BAD_RIGHTS) from None
    if validated["derived_status"] != "valid":
        raise _bad_value(_M_BAD_RIGHTS)
    if validated["operations"].get(_OPS_STORAGE) != "allowed":
        raise _bad_value(_M_BAD_RIGHTS)
    privacy = validated["privacy"]
    if privacy.get("denial") is not False or privacy.get("confidentiality") == "unknown":
        raise _bad_value(_M_BAD_RIGHTS)
    return validated


def validate_extract(
    parameters: object, project_id: str, request_id: object
) -> dict[str, Any]:
    """Strictly validate and canonicalise one ``extraction_run`` submission.

    Pure: no store read, no I/O, no credential. Raises a
    ``DiscoveryJobValidationError`` (``bad_type``/``bad_value``) before any durable
    write, activation change, or POST. The authoritative gate/rights/pilot/
    acquisition checks are repeated by :func:`execute_run` before its one POST.
    """
    if not isinstance(parameters, dict):
        raise _bad_type(_M_BAD_PARAMETERS)
    keys = set(parameters)
    if not EXTRACT_REQUIRED_KEYS <= keys or not keys <= (
        EXTRACT_REQUIRED_KEYS | EXTRACT_OPTIONAL_KEYS
    ):
        raise _bad_value(_M_BAD_KEY)
    if parameters["credential_env"] != EXTRACT_CREDENTIAL_ENV:
        raise _bad_value(_M_BAD_CREDENTIAL_ENV)

    purpose = parameters["purpose"]
    if purpose not in PURPOSES:
        raise _bad_value(_M_BAD_PURPOSE)
    mode = parameters["mode"]
    if mode not in MODES:
        raise _bad_value(_M_BAD_MODE)

    artifact_id = _ident(parameters["artifact_id"], "artifact_id")
    revision_id = _ident(parameters["revision_id"], "revision_id")
    config_ref = _ref(parameters["config_ref"], project_id)
    hosted_ref = _ref(parameters["hosted_ref"], project_id)
    report_refs = _report_refs(parameters["report_refs"], project_id)
    extraction_ref = _optional_ref(parameters.get("extraction_ref"), project_id)
    pilot_ref = _optional_ref(parameters.get("pilot_ref"), project_id)

    # Mirror ``execute_run``'s early purpose/mode authority so a structurally
    # impossible request is refused before any durable change.
    if purpose == "quality":
        if extraction_ref is None:
            raise _bad_value(_M_BAD_AUTHORITY)
    elif extraction_ref is not None:
        raise _bad_value(_M_BAD_AUTHORITY)
    if mode == "bulk":
        if pilot_ref is None:
            raise _bad_value(_M_BAD_AUTHORITY)
    elif pilot_ref is not None:
        raise _bad_value(_M_BAD_AUTHORITY)

    rights = _rights(parameters["rights"], project_id, artifact_id, revision_id)

    return {
        "config_ref": config_ref,
        "report_refs": report_refs,
        "purpose": purpose,
        "mode": mode,
        "hosted_ref": hosted_ref,
        "extraction_ref": extraction_ref,
        "pilot_ref": pilot_ref,
        "artifact_id": artifact_id,
        "revision_id": revision_id,
        "rights": rights,
        "credential_env": EXTRACT_CREDENTIAL_ENV,
    }


# --------------------------------------------------------------------------- #
# Dispatch / replay
# --------------------------------------------------------------------------- #
#: The runner readback ``status`` values that are terminal, resolved outcomes.
_TERMINAL_STATUSES = frozenset({"completed", "failed"})


def dispatch_extract(
    store: Any,
    registry: Any,
    token: str,
    job: Any,
    *,
    api_key: str | None,
    transport: Any = None,
) -> dict[str, Any]:
    """Execute one bounded extraction/quality run through :func:`execute_run`.

    A missing credential is refused here with **zero** POST. The runner owns every
    gate/rights/reserve check and the single POST; it returns a safe readback
    mapping whose ``run_ref`` names the persisted receipt. An already-recorded
    request is observed, never re-sent.
    """
    from ..extraction.runner import execute_run

    params = job.parameters if isinstance(job.parameters, dict) else {}
    if not isinstance(api_key, str) or api_key == "":
        from ..discovery.execution import DiscoveryExecutionError

        raise DiscoveryExecutionError(_M_MISSING_CREDENTIAL)
    return execute_run(
        store,
        registry=registry,
        activation_token=token,
        config_ref=params["config_ref"],
        report_refs=params["report_refs"],
        purpose=params["purpose"],
        request_id=job.request_id,
        hosted_ref=params["hosted_ref"],
        api_key=api_key,
        extraction_ref=params.get("extraction_ref"),
        transport=transport,
        mode=params["mode"],
        pilot_ref=params.get("pilot_ref"),
        artifact_id=params["artifact_id"],
        revision_id=params["revision_id"],
        rights=params["rights"],
    )


def replay_extract(store: Any, registry: Any, token: str, job: Any):
    """Prove an existing extraction run from durable rows with **zero** POST.

    Returns ``(mapping, complete)`` where ``mapping`` is the runner readback of the
    already-recorded request and ``complete`` is ``True`` only for a terminal,
    resolved outcome. A missing/absent dispatch, or an ``unknown``/``submitted``
    outcome, is never a silent completion (``None`` or ``complete=False``); no
    request is ever issued.
    """
    from ..extraction.runner import _replay

    try:
        mapping = _replay(store, job.request_id)
    except Exception:  # noqa: BLE001 - not a proven present outcome
        return None
    if not isinstance(mapping, dict) or mapping.get("dispatch_id") is None:
        return None
    return mapping, mapping.get("status") in _TERMINAL_STATUSES


# --------------------------------------------------------------------------- #
# Safe result projection
# --------------------------------------------------------------------------- #
def accepted_result(mapping: dict[str, Any]) -> dict[str, Any]:
    """Reduce a runner readback to the safe accepted-result mapping.

    Only the request id, the resolved lifecycle status/outcome, the dispatch/task/
    proposal ids, the settled cost, the reported model/provider/usage, and the run
    **receipt** reference. Never the credential, the raw model output, the private
    material, or any accepted proposal body.
    """
    return {
        "kind": RESULT_KIND_EXTRACTION,
        "mode": MODE_EXTRACT,
        "request_id": mapping.get("request_id"),
        "status": mapping.get("status"),
        "outcome": mapping.get("outcome"),
        "dispatch_id": mapping.get("dispatch_id"),
        "dispatch_state": mapping.get("dispatch_state"),
        "task_id": mapping.get("task_id"),
        "proposal_record_id": mapping.get("proposal_record_id"),
        "cost_state": mapping.get("cost_state"),
        "cost_amount_usd": mapping.get("cost_amount_usd"),
        "reserve_usd": mapping.get("reserve_usd"),
        "actual_model": mapping.get("actual_model"),
        "provider": mapping.get("provider"),
        "response_id": mapping.get("response_id"),
        "usage": mapping.get("usage"),
        "http_status": mapping.get("http_status"),
        "request_sha256": mapping.get("request_sha256"),
        "diagnostic_code": mapping.get("diagnostic_code"),
        "run_ref": mapping.get("run_ref"),
        "posted": mapping.get("posted"),
        "no_automatic_retry": mapping.get("no_automatic_retry"),
        "authorizing": False,
    }


#: Safe accepted-result keys (no credential, model output, or private material).
_SAFE_KEYS = (
    "kind", "mode", "request_id", "status", "outcome", "dispatch_id",
    "dispatch_state", "task_id", "proposal_record_id", "cost_state",
    "cost_amount_usd", "reserve_usd", "actual_model", "provider", "response_id",
    "usage", "http_status", "request_sha256", "diagnostic_code", "run_ref", "posted",
    "no_automatic_retry", "authorizing",
)


def status_projection(result: Any) -> Any:
    """Bound a saved accepted result for ``job_status`` (safe keys only)."""
    if not isinstance(result, dict):
        return result
    return {key: result[key] for key in _SAFE_KEYS if key in result}


def persisted_extraction_run(store: Any, *, request_id: str):
    """Return the saved accepted run mapping for a completed job, or ``None``.

    A thin, validating reader for a readback CLI: it refuses anything that is not
    an ``extraction_run`` job whose accepted result is the safe run projection. It
    performs no POST and no execution.
    """
    from ..runtime.jobs import JobRepository

    repo = JobRepository(store)
    job = None
    for candidate in repo.list():
        if candidate.request_id == request_id and candidate.task_kind == TASK_KIND_EXTRACT:
            job = candidate
            break
    if job is None:
        return None
    accepted = repo.accepted_result(job.job_id)
    if accepted is None or accepted.result_kind != RESULT_KIND_EXTRACTION:
        return None
    result = accepted.result
    if not isinstance(result, dict):
        return None
    return result
