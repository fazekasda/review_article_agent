"""Public CLI surface for the existing detached official **retrieval** route (Step 06, C4).

An **addon** to :mod:`review_article_agent.cli.acquisition` (the ``acquisition``
group). It owns only the thin CLI over the already-integrated Step 06 value
layers; it adds no authority and no new architecture:

* :mod:`review_article_agent.worker.acquisition` -- the deterministic report /
  occurrence artifact-id helpers and the safe
  :func:`~review_article_agent.worker.acquisition.accepted_result` projection of
  the E1 retrieval summary.
* :func:`review_article_agent.worker.discovery.validate_submission` (with
  ``mode="acquire"``) -- the **exact** strict, pure driver-parameter validation
  (no store read, no I/O) reused **before** any worker start or admission.
* :class:`review_article_agent.acquisition.execution.AcquisitionExecutor` (E1) --
  only its **read-only** ``find_request`` / ``get_report`` readbacks, constructed
  with ``activation_token=None`` and a low-level transport that refuses to issue
  any request.
* :mod:`review_article_agent.cli.jobs` -- the existing worker-ensure /
  authenticated-control / IPC-error-mapping patterns, reused unchanged, reached
  through the existing owner-only ``discovery_submit`` control operation.

Commands (all local; the readback commands issue no network, provider/model,
approval, or acquisition call):

* ``acquisition retrieve id --request-id ID [--project ID]`` -- print the
  deterministic **report** and **occurrence** artifact ids for a request id, so a
  caller can prepare the exact source / occurrence / report rights. Read-only.
* ``acquisition retrieve run --request-id ID --request-file PATH [--project ID]``
  -- read one strict, bounded JSON object of the **exact** acquire driver
  parameters (freeze ref, candidate id, the source/occurrence/report rights and
  revision ids, ``terms_evidence``, and the optional ``parent_revision_id`` /
  ``transport_options``), validate it with the real
  :func:`~review_article_agent.worker.discovery.validate_submission`
  (``mode="acquire"``) **before** a worker is ensured, then submit one
  ``discovery_submit`` (``mode="acquire"``) job. It returns the **queued
  admission only** -- never a completion. There is no second request key, no
  actor/approved/URL flag, and no source byte in the control payload.
* ``acquisition retrieve show --request-id ID [--project ID] [--content]`` -- read
  the exact, verified, stored E1 retrieval report. The default output is a safe
  summary (refs, status, counts, validation state, non-authorizing flags); the
  explicit ``--content`` opt-in additionally returns the private report body
  (origin URLs, identifiers, terms evidence). The stored report never holds source
  bytes. The readback issues **no** GET and acquires no execution.

The existing ``job status|list|attach|pause|resume|cancel|...`` lifecycle
commands apply to the queued job unchanged; this addon does not duplicate them.

Design rules mirror the parent group: projects are opened through the registry
with ``expected_project_id`` and closed on every path; JSON input is strict and
bounded; failures are the parent group's
:class:`~review_article_agent.cli.acquisition.AcquisitionControlError`
(local validation/inspection) or the reused
:class:`~review_article_agent.cli.jobs.JobControlError` (worker control), each a
static, value-free message that never reflects a path, id, title, URL, term, or
source byte.
"""

from __future__ import annotations

import argparse
from typing import Any, Callable

from ..acquisition.execution import AcquisitionExecutionError, AcquisitionExecutor
from ..acquisition.transport import TransportNetworkError
from ..domain.records import RecordValidationError, validate_identifier
from ..worker import acquisition as worker_acquisition
from ..worker import discovery as worker_discovery
from ..worker.discovery import DiscoveryJobValidationError
from ..worker.engine import PROTOCOL_VERSION
from .acquisition import (
    AcquisitionControlError,
    _CATEGORY_STORAGE,
    _CATEGORY_VALIDATION,
    _read_json_file,
    _resolve_project_id,
    _verified_store,
)
from .jobs import (
    _expect_ok,
    _job_projection,
    _rpc,
    _start_worker,
)
from .worker import _resolve_timeout

__all__ = [
    "RETRIEVAL_COMMANDS",
    "acquisition_retrieve_id",
    "acquisition_retrieve_run",
    "acquisition_retrieve_show",
    "build_retrieval_parsers",
    "dispatch_retrieval",
]

# --------------------------------------------------------------------------- #
# curated, value-free messages (never built from a caller value or an exception)
# --------------------------------------------------------------------------- #
_IDENTIFIER_MESSAGE = (
    "invalid value for --request-id; it must be a bounded opaque identifier. The "
    "supplied value is not shown. Run --help for usage."
)
_REQUEST_FILE_SHAPE_MESSAGE = (
    "the request input must be a single JSON object of the exact acquire driver "
    "parameters. Nothing was sent and no job was created."
)
_REQUEST_INVALID_MESSAGE = (
    "the acquire request was rejected by strict driver-parameter validation (a "
    "wrong or missing field, a malformed reference or identifier, an invalid or "
    "ineligible rights record, distinct-identity or terms-evidence mismatch, or an "
    "out-of-bounds transport option). Nothing was sent and no job was created."
)
_NOT_FOUND_MESSAGE = (
    "no stored retrieval report matches that request id in this project; nothing "
    "was read or changed."
)
_REPORT_INVALID_MESSAGE = (
    "the stored retrieval report could not be verified against its recorded bytes; "
    "nothing was changed."
)


# --------------------------------------------------------------------------- #
# argument tree (addon)
# --------------------------------------------------------------------------- #
def build_retrieval_parsers(acq_sub: "argparse._SubParsersAction") -> None:
    """Register ``retrieve`` on the ``acquisition`` subparsers."""
    retrieve = acq_sub.add_parser(
        "retrieve",
        help=(
            "detached official full-text retrieval: deterministic ids, one exact "
            "acquire submission (queued admission only), and verified report readback"
        ),
    )
    rsub = retrieve.add_subparsers(
        dest="acquisition_retrieve_command", required=True, metavar="RETRIEVE_COMMAND"
    )

    p_id = rsub.add_parser(
        "id",
        help=(
            "print the deterministic report and occurrence artifact ids for a "
            "request id (read-only; prepares the exact rights)"
        ),
    )
    p_id.add_argument("--request-id", required=True, metavar="ID")
    p_id.add_argument("--project", metavar="ID")

    p_run = rsub.add_parser(
        "run",
        help=(
            "submit one exact acquire job from a strict parameter file; returns "
            "queued admission, never completion"
        ),
    )
    p_run.add_argument("--request-id", required=True, metavar="ID")
    p_run.add_argument(
        "--request-file",
        required=True,
        metavar="PATH",
        help="strict JSON object of the exact acquire driver parameters",
    )
    p_run.add_argument("--project", metavar="ID")
    p_run.add_argument("--timeout", metavar="SECONDS")

    p_show = rsub.add_parser(
        "show",
        help=(
            "read the verified stored retrieval report (safe summary; --content is "
            "the explicit private opt-in); issues no call"
        ),
    )
    p_show.add_argument("--request-id", required=True, metavar="ID")
    p_show.add_argument(
        "--content",
        action="store_true",
        help="explicitly include the private report body (URLs, identifiers, terms)",
    )
    p_show.add_argument("--project", metavar="ID")


# --------------------------------------------------------------------------- #
# shared helpers
# --------------------------------------------------------------------------- #
def _request_identifier(raw: object) -> str:
    """Validate a request id locally (never echoes the value)."""
    if not isinstance(raw, str) or not raw:
        raise AcquisitionControlError(_CATEGORY_VALIDATION, _IDENTIFIER_MESSAGE)
    try:
        return validate_identifier(raw, "request_id")
    except RecordValidationError:
        raise AcquisitionControlError(_CATEGORY_VALIDATION, _IDENTIFIER_MESSAGE) from None


def _no_io(url, *, timeout, max_response_bytes):  # pragma: no cover - never reached
    raise TransportNetworkError("retrieval readback must not issue a request")


def _validate_acquire(parameters: dict, project_id: str, request_id: str) -> dict:
    """Run the real, pure acquire submission validation (never anything else)."""
    try:
        return worker_discovery.validate_submission(
            worker_discovery.MODE_ACQUIRE, parameters, project_id, request_id
        )
    except DiscoveryJobValidationError:
        raise AcquisitionControlError(
            _CATEGORY_VALIDATION, _REQUEST_INVALID_MESSAGE
        ) from None


# --------------------------------------------------------------------------- #
# handlers
# --------------------------------------------------------------------------- #
def acquisition_retrieve_id(args: argparse.Namespace, ctx: Any) -> dict[str, Any]:
    """``acquisition retrieve id`` -- the deterministic retrieval artifact ids.

    Read-only: the project is verified (opened with its expected id and closed),
    then the pure helpers derive the report and occurrence artifact ids for the
    request id. No worker is started and nothing is written.
    """
    project_id = _resolve_project_id(ctx, args.project)
    request_id = _request_identifier(args.request_id)
    with _verified_store(ctx, project_id):
        pass
    return {
        "project_id": project_id,
        "request_id": request_id,
        "report_artifact_id": worker_acquisition.report_artifact_id(
            project_id, request_id
        ),
        "occurrence_artifact_id": worker_acquisition.occurrence_artifact_id(
            project_id, request_id
        ),
        "prepares_rights_for": ["source_revision", "occurrence_ref", "report_ref"],
    }


def acquisition_retrieve_run(args: argparse.Namespace, ctx: Any) -> dict[str, Any]:
    """``acquisition retrieve run`` -- submit one exact acquire job.

    The parameter file is read strictly and bounded, its shape is checked, and the
    **real** acquire submission validator
    (:func:`~review_article_agent.worker.discovery.validate_submission` with
    ``mode="acquire"``) canonicalises it **before** a worker is ensured, so a bad
    value is a typed ``validation`` error and no daemon is started and no job is
    created. The canonical parameters are sent over the existing owner-only
    ``discovery_submit`` operation; only the queued admission is returned.
    """
    timeout = _resolve_timeout(args)
    project_id = _resolve_project_id(ctx, args.project)
    request_id = _request_identifier(args.request_id)
    parameters = _read_json_file(args.request_file, what="request")
    if not isinstance(parameters, dict):
        raise AcquisitionControlError(
            _CATEGORY_VALIDATION, _REQUEST_FILE_SHAPE_MESSAGE
        )
    with _verified_store(ctx, project_id):
        pass
    canonical = _validate_acquire(parameters, project_id, request_id)
    request = {
        "version": PROTOCOL_VERSION,
        "op": "discovery_submit",
        "project_id": project_id,
        "request_id": request_id,
        "mode": worker_discovery.MODE_ACQUIRE,
        "parameters": canonical,
    }
    pid = _start_worker(ctx, timeout)
    result = _expect_ok(_rpc(ctx, pid, request, timeout))
    job = _job_projection(result.get("job"))
    return {
        "mode": worker_discovery.MODE_ACQUIRE,
        "result_kind": worker_discovery.result_kind_for_mode(
            worker_discovery.MODE_ACQUIRE
        ),
        "job": job,
        "job_id": job.get("job_id"),
        "submitted": True,
        "queued": True,
        "admission_only": True,
        "job_completion": False,
        "scientific_validation": False,
        "eligibility": "eligibility_not_assessed",
    }


def acquisition_retrieve_show(args: argparse.Namespace, ctx: Any) -> dict[str, Any]:
    """``acquisition retrieve show`` -- read the verified stored E1 report.

    The report is loaded and fully re-verified against its recorded bytes by the
    read-only E1 ``find_request`` path (constructed with ``activation_token=None``
    and a refusing low-level transport). It issues **no** GET and acquires no
    execution. The default output is a safe summary; ``--content`` is the explicit
    opt-in for the private report body.
    """
    project_id = _resolve_project_id(ctx, args.project)
    request_id = _request_identifier(args.request_id)
    with _verified_store(ctx, project_id) as store:
        executor = AcquisitionExecutor(
            store,
            registry=ctx.registry,
            activation_token=None,
            lowlevel=_no_io,
            stop_requested=lambda: True,
        )
        try:
            summary = executor.find_request(request_id)
        except AcquisitionExecutionError:
            raise AcquisitionControlError(
                _CATEGORY_STORAGE, _REPORT_INVALID_MESSAGE
            ) from None
    if summary is None:
        raise AcquisitionControlError(_CATEGORY_VALIDATION, _NOT_FOUND_MESSAGE)
    if args.content:
        return {
            "mode": worker_discovery.MODE_ACQUIRE,
            "result_kind": worker_discovery.RESULT_KIND_ACQUISITION,
            "report_ref": dict(summary["report_ref"]),
            "replayed": True,
            "historical": True,
            "report": summary["report"],
            "private": True,
            "non_authorizing": True,
            "authorizing": False,
            "eligibility": "eligibility_not_assessed",
            "scientific_validation": False,
        }
    safe = worker_acquisition.accepted_result(summary)
    safe.update(
        {
            "mode": worker_discovery.MODE_ACQUIRE,
            "replayed": True,
            "historical": True,
            "non_authorizing": True,
            "private_content_included": False,
        }
    )
    return safe


# --------------------------------------------------------------------------- #
# dispatch (addon)
# --------------------------------------------------------------------------- #
_RETRIEVAL_COMMANDS: dict[str, Callable[[argparse.Namespace, Any], Any]] = {
    "id": acquisition_retrieve_id,
    "run": acquisition_retrieve_run,
    "show": acquisition_retrieve_show,
}

#: The addon's leaf-command map (exported for symmetry with the other addons).
RETRIEVAL_COMMANDS = _RETRIEVAL_COMMANDS


def dispatch_retrieval(args: argparse.Namespace, ctx: Any) -> object | None:
    """Route ``acquisition retrieve ...``; ``None`` if the leaf is unknown."""
    handler = _RETRIEVAL_COMMANDS.get(
        getattr(args, "acquisition_retrieve_command", None)
    )
    if handler is None:
        return None
    return handler(args, ctx)
