"""Minimal local CLI surface for the immutable extraction workflow (S9-CLI).

This module adds only the root ``extraction`` command group over the already
integrated :class:`~review_article_agent.extraction.workflow.ExtractionWorkflowRepository`.
It is a **local, read/propose** surface: it never dispatches a model, executes a
runner, approves a gate, or reaches the network. A stored revision only means a
proposal exists -- it is **never** an approval, and it never authorizes
extraction or synthesis.

* ``extraction propose`` -- write one *inert* ``config`` / ``pilot`` / ``batch`` /
  ``audit`` revision from an explicit side-input, an explicit caller rights
  record, an explicit artifact/revision id, and an optional parent revision.
* ``extraction show`` -- read one exact current extraction revision back (safe
  summary; ``--content`` is the explicit private opt-in for the raw payload).
* ``extraction accepted`` -- run the synthesis gate for one exact ``audit``
  revision (safe summary of ids/status/counts/guarantee; ``--content`` includes
  the accepted structured evidence detail).

Privacy/safety rules mirror :mod:`review_article_agent.cli.parsing`: projects are
opened through the registry with ``expected_project_id`` and closed on every
path; side-inputs are read from a bounded regular file that never follows a final
symlink; and every failure raises :class:`ExtractionControlError` with a stable
category (``validation``/``conflict``/``storage``) and a static message that
never reflects a path, id, rights value, instruction, model output, dispatch key,
or exception text. By default no prompt instructions, model configuration, or
accepted evidence bodies are emitted.
"""

from __future__ import annotations

import argparse
import os
from typing import Any, Callable

from ..domain.errors import ReviewAgentError, StoreError
from ..domain.records import RecordValidationError, canonical_json, validate_identifier
from ..extraction.workflow import ExtractionWorkflowRepository
from ..runtime.jobs import JobRepository
from ..storage import ArtifactConflictError
from ..worker import discovery as worker_discovery
from ..worker import extraction as worker_extraction
from ..worker.discovery import DiscoveryJobValidationError
from ..worker.engine import PROTOCOL_VERSION
from .jobs import (
    JobControlError,
    _expect_ok,
    _job_projection,
    _parse_identifier,
    _resolve_project_id as _resolve_worker_project_id,
    _rpc,
    _start_worker,
)
from .parsing import _read_json_file, _read_object, _resolve_project_id, _verified_store
from .worker import _resolve_timeout

__all__ = [
    "ExtractionControlError",
    "build_extraction_parser",
    "dispatch_extraction",
    "extraction_run_status",
    "extraction_run_submit",
]

_CATEGORY_VALIDATION = "validation"
_CATEGORY_CONFLICT = "conflict"
_CATEGORY_STORAGE = "storage"

#: The local upper bound for a plain non-negative integer argument.
_MAX_INT = 1_000_000_000

_KINDS = ("config", "pilot", "batch", "audit")

_REJECTED_MESSAGE = (
    "the extraction request was rejected (a malformed or out-of-bounds identifier, "
    "artifact/revision reference, rights record, or workflow document). Nothing was "
    "written."
)
_NOT_AUTHORIZED_MESSAGE = (
    "the extraction synthesis gate is not satisfied for this audit (a missing named "
    "approval, a blocking or paused workflow, or a stale dependency); nothing was "
    "authorized."
)
_CONFLICT_MESSAGE = (
    "the request conflicts with existing immutable state; nothing was overwritten."
)
_STORAGE_MESSAGE = "a local storage operation failed; nothing was changed."

# -- run submit/status (detached extraction job) ---------------------------- #
_RUN_REF_MESSAGE = (
    "invalid artifact or revision identifier for a run reference; it must be a "
    "bounded opaque id. The supplied value is not shown. Run --help for usage."
)
_RUN_REPORT_REFS_MESSAGE = (
    "the report-refs file must be a strict JSON list of "
    '{"artifact_id", "revision_id"} objects with non-empty string values. '
    "Nothing was sent."
)
_RUN_RIGHTS_MESSAGE = (
    "the rights file must be a strict JSON object holding one RightsRecord for the "
    "exact receipt revision. Nothing was sent."
)
_RUN_CREDENTIAL_ENV_MESSAGE = (
    "invalid value for --credential-env; only the one approved extraction "
    "environment variable name is accepted, and the credential value is never "
    "supplied on the command line. The supplied value is not shown. Run --help "
    "for usage."
)
_RUN_INVALID_MESSAGE = (
    "the extraction run request was rejected by strict parameter validation (a "
    "wrong or missing field, a malformed reference or identifier, an invalid or "
    "ineligible rights record, or a purpose/mode that does not match the supplied "
    "extraction/pilot reference). Nothing was sent and no job was created."
)
_RUN_NOT_FOUND_MESSAGE = (
    "no recorded extraction run matches that request id in this project; nothing "
    "was read or sent."
)
_RUN_MISSING_KEY_NOTICE = (
    "the named extraction credential is not present in this CLI's environment; the "
    "worker reads the environment variable at dispatch, so configure the worker's "
    "environment before it runs the job (this command never restarts or "
    "reconfigures a user's worker). The job is admitted and queued; inspect it with "
    "`job status`."
)


class ExtractionControlError(Exception):
    """An extraction command failed in a typed way.

    ``category`` is ``"validation"``, ``"conflict"``, or ``"storage"``; the
    message is always a static constant authored in this module.
    """

    def __init__(self, category: str, message: str) -> None:
        super().__init__(message)
        self.category = category


# --------------------------------------------------------------------------- #
# argument tree
# --------------------------------------------------------------------------- #
def build_extraction_parser(sub: "argparse._SubParsersAction") -> None:
    """Register the root ``extraction`` command group on the top-level parser."""
    extraction = sub.add_parser(
        "extraction",
        help=(
            "propose and read immutable extraction config/pilot/batch/audit "
            "revisions, and inspect the accepted synthesis evidence; local only, "
            "no dispatch and no approval"
        ),
    )
    actions = extraction.add_subparsers(
        dest="extraction_command", required=True, metavar="EXTRACTION_COMMAND"
    )

    p_propose = actions.add_parser(
        "propose", help="write one inert extraction workflow revision"
    )
    p_propose.add_argument("--project", metavar="ID")
    p_propose.add_argument("--kind", required=True, choices=_KINDS)
    p_propose.add_argument("--input-file", required=True, metavar="PATH")
    p_propose.add_argument("--rights-file", required=True, metavar="PATH")
    p_propose.add_argument("--artifact-id", required=True, metavar="ID")
    p_propose.add_argument("--revision-id", required=True, metavar="ID")
    p_propose.add_argument("--parent-revision-id", metavar="ID")

    p_show = actions.add_parser(
        "show",
        help="read one exact current extraction revision (safe summary; --content to opt in)",
    )
    p_show.add_argument("--project", metavar="ID")
    p_show.add_argument("--kind", required=True, choices=_KINDS)
    p_show.add_argument("--artifact-id", required=True, metavar="ID")
    p_show.add_argument("--revision-id", required=True, metavar="ID")
    p_show.add_argument(
        "--content",
        action="store_true",
        help="explicitly include the private raw payload (instructions/model config)",
    )

    p_accepted = actions.add_parser(
        "accepted",
        help="run the synthesis gate for one exact audit revision",
    )
    p_accepted.add_argument("--project", metavar="ID")
    p_accepted.add_argument("--artifact-id", required=True, metavar="ID")
    p_accepted.add_argument("--revision-id", required=True, metavar="ID")
    p_accepted.add_argument(
        "--content",
        action="store_true",
        help="explicitly include the accepted evidence detail (ids, limitations, escalations)",
    )

    # -- run (detached extraction/quality background job) -------------------- #
    run = actions.add_parser(
        "run",
        help=(
            "detached extraction/quality runs: submit one bounded run (queued "
            "admission only) and read one run's recorded outcome"
        ),
    )
    run_sub = run.add_subparsers(
        dest="extraction_run_command", required=True, metavar="RUN_COMMAND"
    )

    p_run_submit = run_sub.add_parser(
        "submit",
        help=(
            "submit one exact extraction/quality run job (mandatory --credential-env); "
            "returns queued admission only"
        ),
    )
    p_run_submit.add_argument("--project", metavar="ID")
    p_run_submit.add_argument("--config-artifact-id", required=True, metavar="ID")
    p_run_submit.add_argument("--config-revision-id", required=True, metavar="ID")
    p_run_submit.add_argument(
        "--report-refs-file",
        required=True,
        metavar="PATH",
        help='strict JSON list of {"artifact_id","revision_id"} report references',
    )
    p_run_submit.add_argument("--hosted-artifact-id", required=True, metavar="ID")
    p_run_submit.add_argument("--hosted-revision-id", required=True, metavar="ID")
    p_run_submit.add_argument("--purpose", required=True, choices=("extraction", "quality"))
    p_run_submit.add_argument("--mode", required=True, choices=("pilot", "bulk"))
    p_run_submit.add_argument("--extraction-artifact-id", metavar="ID")
    p_run_submit.add_argument("--extraction-revision-id", metavar="ID")
    p_run_submit.add_argument("--pilot-artifact-id", metavar="ID")
    p_run_submit.add_argument("--pilot-revision-id", metavar="ID")
    p_run_submit.add_argument("--receipt-artifact-id", required=True, metavar="ID")
    p_run_submit.add_argument("--receipt-revision-id", required=True, metavar="ID")
    p_run_submit.add_argument(
        "--rights-file",
        required=True,
        metavar="PATH",
        help="explicit caller RightsRecord JSON for the exact receipt revision",
    )
    p_run_submit.add_argument(
        "--credential-env",
        required=True,
        metavar="NAME",
        help="the one approved environment variable name (the value is never accepted)",
    )
    p_run_submit.add_argument("--request-id", required=True, metavar="ID")
    p_run_submit.add_argument("--timeout", metavar="SECONDS")

    p_run_status = run_sub.add_parser(
        "status",
        help="safe readback of one recorded extraction/quality run (no call)",
    )
    p_run_status.add_argument("--project", metavar="ID")
    p_run_status.add_argument("--request-id", required=True, metavar="ID")


# --------------------------------------------------------------------------- #
# shared helpers
# --------------------------------------------------------------------------- #
def _guard(call: Callable[[], Any], *, invalid_message: str) -> Any:
    """Run a repository call, converting every failure to a static typed error."""
    try:
        return call()
    except ExtractionControlError:
        raise
    except ArtifactConflictError:
        raise ExtractionControlError(_CATEGORY_CONFLICT, _CONFLICT_MESSAGE) from None
    except StoreError:
        raise ExtractionControlError(_CATEGORY_STORAGE, _STORAGE_MESSAGE) from None
    except OSError:
        raise ExtractionControlError(_CATEGORY_STORAGE, _STORAGE_MESSAGE) from None
    except (ReviewAgentError, ValueError, TypeError, KeyError, AttributeError, StopIteration):
        raise ExtractionControlError(_CATEGORY_VALIDATION, invalid_message) from None


def _ref(project_id: str, artifact_id: str, revision_id: str) -> dict:
    return {
        "project_id": project_id,
        "artifact_id": artifact_id,
        "revision_id": revision_id,
    }


#: Safe per-kind projections: counts + safe structural identity only. No prompt
#: instructions, model configuration, or accepted evidence bodies.
_SHOW_SUMMARIES: dict[str, Callable[[dict], dict]] = {
    "config": lambda payload: {
        "config_schema_version": payload["config_schema_version"],
        "dimension_count": len(payload["appraisal_dimensions"]),
        "dimension_ids": [d["dimension_id"] for d in payload["appraisal_dimensions"]],
        "pilot_size": payload["pilot_size"],
        "random_size": payload["random_size"],
        "risk_size": payload["risk_size"],
        "parser": payload["parser"],
        "normalization": payload["normalization"],
    },
    "pilot": lambda payload: {
        "case_count": len(payload.get("cases", [])),
        "blocking_failure_count": len(payload.get("blocking_failures", [])),
        "mandatory_labels": list(payload.get("mandatory_labels", [])),
    },
    "batch": lambda payload: {
        "case_count": len(payload.get("cases", [])),
        "random_count": len(payload.get("random_ids", [])),
        "risk_count": len(payload.get("risk_ids", [])),
    },
    "audit": lambda payload: {
        "paused": bool(payload.get("paused", False)),
        "important_error_count": len(payload.get("important_errors", [])),
        "guarantee": payload.get("guarantee"),
    },
}


# --------------------------------------------------------------------------- #
# handlers
# --------------------------------------------------------------------------- #
def extraction_propose(args: argparse.Namespace, ctx: Any) -> dict[str, Any]:
    """Write one inert extraction revision; never an approval."""
    payload = _read_object(args.input_file, what="extraction")
    rights = _read_object(args.rights_file, what="rights")
    project_id = _resolve_project_id(ctx, args.project)
    with _verified_store(ctx, project_id) as store:
        repo = ExtractionWorkflowRepository(store)
        ref = _guard(
            lambda: repo.propose(
                args.kind,
                payload,
                artifact_id=args.artifact_id,
                revision_id=args.revision_id,
                rights=rights,
                parent_revision_id=args.parent_revision_id,
            ),
            invalid_message=_REJECTED_MESSAGE,
        )
    return {
        "ref": ref,
        "kind": args.kind,
        "approved": False,
        "private_content_included": False,
        "authorizes_extraction": False,
        "authorizes_synthesis": False,
    }


def extraction_show(args: argparse.Namespace, ctx: Any) -> dict[str, Any]:
    """Read one exact current extraction revision; safe summary unless ``--content``."""
    project_id = _resolve_project_id(ctx, args.project)
    ref = _ref(project_id, args.artifact_id, args.revision_id)
    with _verified_store(ctx, project_id) as store:
        repo = ExtractionWorkflowRepository(store)
        payload = _guard(
            lambda: repo.read(ref, args.kind), invalid_message=_REJECTED_MESSAGE
        )
    summary: dict[str, Any] = {
        "ref": ref,
        "kind": args.kind,
        "private_content_included": False,
        "authorizes_extraction": False,
        "authorizes_synthesis": False,
    }
    summary.update(_SHOW_SUMMARIES[args.kind](payload))
    if args.content:
        summary["private_content_included"] = True
        summary["payload"] = payload
    return summary


def extraction_accepted(args: argparse.Namespace, ctx: Any) -> dict[str, Any]:
    """Run the synthesis gate for one audit; safe summary unless ``--content``."""
    project_id = _resolve_project_id(ctx, args.project)
    ref = _ref(project_id, args.artifact_id, args.revision_id)
    with _verified_store(ctx, project_id) as store:
        repo = ExtractionWorkflowRepository(store)
        result = _guard(
            lambda: repo.require_for_synthesis(ref),
            invalid_message=_NOT_AUTHORIZED_MESSAGE,
        )
    summary: dict[str, Any] = {
        "audit_ref": result["audit_ref"],
        "batch_ref": result["batch_ref"],
        "config_ref": result["config_ref"],
        "pilot_ref": result["pilot_ref"],
        "acquisition_ref": result["acquisition_ref"],
        "accepted": bool(result["accepted"]),
        "status": "accepted" if result["accepted"] else "not_accepted",
        "case_count": len(result["case_ids"]),
        "audited_count": len(result["audited_ids"]),
        "gates_satisfied": list(result["gates_satisfied"]),
        "guarantee": result["guarantee"],
        "authority": result["authority"],
        "notice": result["notice"],
        "private_content_included": False,
        "authorizes_synthesis": True,
    }
    if args.content:
        summary["private_content_included"] = True
        summary["case_ids"] = list(result["case_ids"])
        summary["audited_ids"] = list(result["audited_ids"])
        summary["limitations"] = list(result["limitations"])
        summary["escalations"] = list(result["escalations"])
    return summary


# --------------------------------------------------------------------------- #
# run handlers (detached extraction/quality job)
# --------------------------------------------------------------------------- #
def _run_ref(project_id: str, artifact_id: object, revision_id: object) -> dict:
    """Build one exact same-project ``RefRevision`` (validated locally)."""
    try:
        artifact = validate_identifier(artifact_id, "artifact_id")
        revision = validate_identifier(revision_id, "revision_id")
    except (RecordValidationError, TypeError, ValueError):
        raise ExtractionControlError(_CATEGORY_VALIDATION, _RUN_REF_MESSAGE) from None
    return {
        "project_id": project_id,
        "artifact_id": artifact,
        "revision_id": revision,
    }


def _read_report_refs(path: str) -> list:
    """Read one strict JSON ``[{"artifact_id","revision_id"}]`` list."""
    value = _read_json_file(path, what="report references")
    if not isinstance(value, list) or not value:
        raise ExtractionControlError(_CATEGORY_VALIDATION, _RUN_REPORT_REFS_MESSAGE)
    refs: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, dict) or set(item) != {"artifact_id", "revision_id"}:
            raise ExtractionControlError(_CATEGORY_VALIDATION, _RUN_REPORT_REFS_MESSAGE)
        artifact_id, revision_id = item["artifact_id"], item["revision_id"]
        if (
            not isinstance(artifact_id, str)
            or not isinstance(revision_id, str)
            or artifact_id == ""
            or revision_id == ""
        ):
            raise ExtractionControlError(_CATEGORY_VALIDATION, _RUN_REPORT_REFS_MESSAGE)
        refs.append({"artifact_id": artifact_id, "revision_id": revision_id})
    return refs


def _read_rights(path: str) -> dict:
    """Read one strict JSON RightsRecord object."""
    value = _read_json_file(path, what="rights record")
    if not isinstance(value, dict):
        raise ExtractionControlError(_CATEGORY_VALIDATION, _RUN_RIGHTS_MESSAGE)
    return value


def _check_run_payload_size(parameters: dict) -> None:
    try:
        encoded = canonical_json(parameters)
    except (RecordValidationError, TypeError, ValueError):
        raise ExtractionControlError(_CATEGORY_VALIDATION, _RUN_INVALID_MESSAGE) from None
    from ..runtime import ipc

    if len(encoded) > ipc.MAX_REQUEST_BYTES:
        raise ExtractionControlError(_CATEGORY_VALIDATION, _RUN_INVALID_MESSAGE)


def extraction_run_submit(args: argparse.Namespace, ctx: Any) -> dict[str, Any]:
    """``extraction run submit`` -- submit one scoped extraction/quality run job.

    Every argument is validated locally (and the assembled parameters are
    canonicalised by the **real** extraction submission validator) **before** a
    worker is ensured, so a bad value is a typed ``validation`` error and no daemon
    is started and no job is created. ``--credential-env`` must name the one
    approved variable, and the credential **value** is never accepted here. Only
    the queued admission is returned.
    """
    timeout = _resolve_timeout(args)
    project_id = _resolve_worker_project_id(ctx, args.project)
    request_id = _parse_identifier(args.request_id)
    if args.credential_env != worker_extraction.EXTRACT_CREDENTIAL_ENV:
        raise JobControlError(_CATEGORY_VALIDATION, _RUN_CREDENTIAL_ENV_MESSAGE)
    report_refs = [
        _run_ref(project_id, item["artifact_id"], item["revision_id"])
        for item in _read_report_refs(args.report_refs_file)
    ]
    rights = _read_rights(args.rights_file)
    extraction_ref = None
    if args.extraction_artifact_id is not None or args.extraction_revision_id is not None:
        extraction_ref = _run_ref(
            project_id, args.extraction_artifact_id, args.extraction_revision_id
        )
    pilot_ref = None
    if args.pilot_artifact_id is not None or args.pilot_revision_id is not None:
        pilot_ref = _run_ref(project_id, args.pilot_artifact_id, args.pilot_revision_id)
    parameters = {
        "config_ref": _run_ref(
            project_id, args.config_artifact_id, args.config_revision_id
        ),
        "report_refs": report_refs,
        "purpose": args.purpose,
        "mode": args.mode,
        "hosted_ref": _run_ref(
            project_id, args.hosted_artifact_id, args.hosted_revision_id
        ),
        "extraction_ref": extraction_ref,
        "pilot_ref": pilot_ref,
        "artifact_id": args.receipt_artifact_id,
        "revision_id": args.receipt_revision_id,
        "rights": rights,
        "credential_env": worker_extraction.EXTRACT_CREDENTIAL_ENV,
    }
    _check_run_payload_size(parameters)
    try:
        canonical = worker_discovery.validate_submission(
            worker_discovery.MODE_EXTRACT, parameters, project_id, request_id
        )
    except DiscoveryJobValidationError:
        raise ExtractionControlError(_CATEGORY_VALIDATION, _RUN_INVALID_MESSAGE) from None
    request = {
        "version": PROTOCOL_VERSION,
        "op": "discovery_submit",
        "project_id": project_id,
        "request_id": request_id,
        "mode": worker_discovery.MODE_EXTRACT,
        "parameters": canonical,
    }
    pid = _start_worker(ctx, timeout)
    result = _expect_ok(_rpc(ctx, pid, request, timeout))
    job = _job_projection(result.get("job"))
    output: dict[str, Any] = {
        "mode": worker_discovery.MODE_EXTRACT,
        "result_kind": worker_discovery.result_kind_for_mode(
            worker_discovery.MODE_EXTRACT
        ),
        "job": job,
        "job_id": job.get("job_id"),
        "submitted": True,
        "queued": True,
        "admission_only": True,
        "job_completion": False,
        "key_value_accepted": False,
        "scientific_validation": False,
    }
    if not os.environ.get(worker_extraction.EXTRACT_CREDENTIAL_ENV):
        output["configuration_required"] = True
        output["notice"] = _RUN_MISSING_KEY_NOTICE
    else:
        output["configuration_required"] = False
    return output


def extraction_run_status(args: argparse.Namespace, ctx: Any) -> dict[str, Any]:
    """``extraction run status`` -- safe readback of one recorded run (no call).

    Reads the recorded job/outcome locally from the verified store; it starts no
    worker, issues no POST, and records no approval. The run receipt reference is
    surfaced; the private model output is never included.
    """
    project_id = _resolve_project_id(ctx, args.project)
    request_id = _parse_identifier(args.request_id)
    with _verified_store(ctx, project_id) as store:
        run = worker_extraction.persisted_extraction_run(store, request_id=request_id)
        state = None
        if run is None:
            repo = JobRepository(store)
            for job in repo.list():
                if (
                    job.request_id == request_id
                    and job.task_kind == worker_extraction.TASK_KIND_EXTRACT
                ):
                    state = job.state
                    break
    if run is None and state is None:
        raise ExtractionControlError(_CATEGORY_VALIDATION, _RUN_NOT_FOUND_MESSAGE)
    return {
        "request_id": request_id,
        "mode": worker_extraction.MODE_EXTRACT,
        "result_kind": worker_extraction.RESULT_KIND_EXTRACTION,
        "run": run,
        "job_state": state,
        "authorizes_extraction": False,
        "authorizes_synthesis": False,
        "scientific_validation": False,
    }


# --------------------------------------------------------------------------- #
# dispatch
# --------------------------------------------------------------------------- #
EXTRACTION_COMMANDS: dict[str, Callable[[argparse.Namespace, Any], Any]] = {
    "propose": extraction_propose,
    "show": extraction_show,
    "accepted": extraction_accepted,
}

#: The ``run`` sub-group's leaf-command map.
EXTRACTION_RUN_COMMANDS: dict[str, Callable[[argparse.Namespace, Any], Any]] = {
    "submit": extraction_run_submit,
    "status": extraction_run_status,
}


def dispatch_extraction(args: argparse.Namespace, ctx: Any) -> object | None:
    """Route one parsed ``extraction`` invocation; ``None`` if unknown."""
    command = getattr(args, "extraction_command", None)
    if command == "run":
        handler = EXTRACTION_RUN_COMMANDS.get(
            getattr(args, "extraction_run_command", None)
        )
        return None if handler is None else handler(args, ctx)
    handler = EXTRACTION_COMMANDS.get(command)
    return None if handler is None else handler(args, ctx)
