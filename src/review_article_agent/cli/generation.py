"""Local CLI surface for detached **generation role** jobs (kickoff 16 §5, S17-O2a).

This module adds a root ``generation`` command group over the detached worker's
``generation_run`` job kind (``worker/generation.py``). It is a **submit/read**
surface: it never dispatches a model itself, approves a gate, or reaches the
network. ``generation run submit`` performs strict local validation (and the
**real** generation submission validator) before a worker is ensured, then
returns the queued admission only. ``generation run status`` reads one recorded
run back locally, starting no worker and issuing no POST.

The one approved credential **value** is never accepted on the command line: only
the fixed environment-variable **name** is passed through, exactly as
``extraction run submit`` does. Every failure raises
:class:`GenerationControlError` with a stable category
(``validation``/``conflict``/``storage``) and a static message that never
reflects a path, id, rights value, credential, or exception text.
"""

from __future__ import annotations

import argparse
from typing import Any, Callable

from ..domain.errors import ReviewAgentError, StoreError
from ..domain.records import RecordValidationError, canonical_json
from ..runtime.jobs import JobRepository
from ..storage import ArtifactConflictError
from ..worker import discovery as worker_discovery
from ..worker import generation as worker_generation
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
from .parsing import _read_json_file, _resolve_project_id, _verified_store
from .worker import _resolve_timeout

__all__ = [
    "GenerationControlError",
    "build_generation_parser",
    "dispatch_generation",
    "generation_run_status",
    "generation_run_submit",
]

_CATEGORY_VALIDATION = "validation"
_CATEGORY_CONFLICT = "conflict"
_CATEGORY_STORAGE = "storage"

_RUN_CREDENTIAL_ENV_MESSAGE = (
    "invalid value for --credential-env; only the one approved generation "
    "environment variable name is accepted, and the credential value is never "
    "supplied on the command line. The supplied value is not shown. Run --help "
    "for usage."
)
_RUN_OPERATION_MESSAGE = (
    "invalid or unsupported --operation; the value is not shown. Run --help for "
    "the supported generation operations."
)
_RUN_PARAMS_MESSAGE = (
    "the parameters file must be a strict JSON object holding the operation's "
    "exact parameters (refs, ids, the rights record, and any optional fields). "
    "Nothing was sent."
)
_RUN_INVALID_MESSAGE = (
    "the generation run request was rejected by strict parameter validation (a "
    "wrong or missing field, a malformed reference or identifier, an invalid or "
    "ineligible rights record, or an unsupported operation). Nothing was sent and "
    "no job was created."
)
_RUN_NOT_FOUND_MESSAGE = (
    "no recorded generation run matches that request id in this project; nothing "
    "was read or sent."
)
_RUN_MISSING_KEY_NOTICE = (
    "the named generation credential is not present in this CLI's environment; the "
    "worker reads the environment variable at dispatch, so configure the worker's "
    "environment before it runs the job (this command never restarts or "
    "reconfigures a user's worker). The job is admitted and queued; inspect it with "
    "`job status`."
)


class GenerationControlError(Exception):
    """A generation command failed in a typed way.

    ``category`` is ``"validation"``, ``"conflict"``, or ``"storage"``; the
    message is always a static constant authored in this module.
    """

    def __init__(self, category: str, message: str) -> None:
        super().__init__(message)
        self.category = category


# --------------------------------------------------------------------------- #
# argument tree
# --------------------------------------------------------------------------- #
def _flag_name(key: str) -> str:
    return "--" + key.replace("_", "-")


def _ref_base(key: str) -> str:
    """The flag stem for a ``*_ref`` key (``audit_ref`` -> ``audit``)."""
    return key[:-4] if key.endswith("_ref") else key


def build_generation_parser(sub: "argparse._SubParsersAction") -> None:
    """Register the root ``generation`` command group on the top-level parser."""
    generation = sub.add_parser(
        "generation",
        help=(
            "detached model-backed generation role runs: submit one role operation "
            "(queued admission only) and read one run's recorded outcome"
        ),
    )
    actions = generation.add_subparsers(
        dest="generation_command", required=True, metavar="GENERATION_COMMAND"
    )
    run = actions.add_parser(
        "run",
        help=(
            "detached generation role runs: submit one bounded operation (queued "
            "admission only) and read one run's recorded outcome"
        ),
    )
    run_sub = run.add_subparsers(
        dest="generation_run_command", required=True, metavar="RUN_COMMAND"
    )

    p_run_submit = run_sub.add_parser(
        "submit",
        help=(
            "submit one exact generation role job (mandatory --operation and "
            "--credential-env); returns queued admission only"
        ),
    )
    p_run_submit.add_argument("--project", metavar="ID")
    p_run_submit.add_argument(
        "--operation",
        required=True,
        choices=worker_generation.RECOGNIZED_OPERATIONS,
        help="the generation role operation to run",
    )
    p_run_submit.add_argument("--request-id", required=True, metavar="ID")
    p_run_submit.add_argument(
        "--credential-env",
        required=True,
        metavar="NAME",
        help="the one approved environment variable name (the value is never accepted)",
    )
    p_run_submit.add_argument(
        "--params-file",
        metavar="PATH",
        help="strict JSON object with the operation's exact parameters",
    )
    p_run_submit.add_argument("--rights-file", metavar="PATH")
    _add_op_flags(p_run_submit)
    p_run_submit.add_argument("--timeout", metavar="SECONDS")

    p_run_status = run_sub.add_parser(
        "status",
        help="safe readback of one recorded generation run (no call)",
    )
    p_run_status.add_argument("--project", metavar="ID")
    p_run_status.add_argument("--request-id", required=True, metavar="ID")


def _add_op_flags(parser: argparse.ArgumentParser) -> None:
    """Add one optional flag (or flag pair) for every operation parameter key."""
    seen: set[str] = set()
    for required, optional in worker_generation.OPERATION_SPECS.values():
        for key, kind in (*required, *optional):
            if key in seen or key == "rights":
                continue
            seen.add(key)
            if kind == "ref":
                parser.add_argument(_flag_name(_ref_base(key)) + "-artifact-id", metavar="ID")
                parser.add_argument(_flag_name(_ref_base(key)) + "-revision-id", metavar="ID")
            elif kind == "ref_list":
                parser.add_argument(_flag_name(key) + "-file", metavar="PATH")
            else:
                parser.add_argument(_flag_name(key), metavar="ID")


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _guard(call: Callable[[], Any]) -> Any:
    try:
        return call()
    except GenerationControlError:
        raise
    except ArtifactConflictError:
        raise GenerationControlError(_CATEGORY_CONFLICT, _RUN_INVALID_MESSAGE) from None
    except StoreError:
        raise GenerationControlError(_CATEGORY_STORAGE, _RUN_INVALID_MESSAGE) from None
    except OSError:
        raise GenerationControlError(_CATEGORY_STORAGE, _RUN_INVALID_MESSAGE) from None
    except (ReviewAgentError, ValueError, TypeError, KeyError, AttributeError):
        raise GenerationControlError(_CATEGORY_VALIDATION, _RUN_INVALID_MESSAGE) from None


def _with_project(value: dict, project_id: str) -> dict:
    ref = dict(value)
    ref.setdefault("project_id", project_id)
    return ref


def _spec_for(operation: str):
    return worker_generation.OPERATION_SPECS.get(operation)


def _assemble_parameters(
    args: argparse.Namespace, project_id: str
) -> dict[str, Any]:
    """Assemble the operation parameters from ``--params-file`` and/or flags.

    The exact required/optional set is enforced by the real submission validator;
    this only builds the candidate dict and injects the project id into every
    reference so a caller never has to repeat it.
    """
    operation = args.operation
    spec = _spec_for(operation)
    if spec is None:
        raise GenerationControlError(_CATEGORY_VALIDATION, _RUN_OPERATION_MESSAGE)
    required, optional = spec

    if args.params_file is not None:
        value = _read_json_file(args.params_file, what="generation parameters")
        if not isinstance(value, dict):
            raise GenerationControlError(_CATEGORY_VALIDATION, _RUN_PARAMS_MESSAGE)
        parameters = dict(value)
    else:
        parameters = {}

    for key, kind in (*required, *optional):
        if kind == "ref":
            artifact = getattr(args, _ref_base(key) + "_artifact_id", None)
            revision = getattr(args, _ref_base(key) + "_revision_id", None)
            if artifact is None and revision is None:
                continue
            parameters[key] = {
                "project_id": project_id,
                "artifact_id": artifact,
                "revision_id": revision,
            }
        elif kind == "ref_list":
            path = getattr(args, key + "_file", None)
            if path is None:
                continue
            items = _read_json_file(path, what="reference list")
            if not isinstance(items, list):
                raise GenerationControlError(_CATEGORY_VALIDATION, _RUN_PARAMS_MESSAGE)
            parameters[key] = [
                _with_project(item, project_id) if isinstance(item, dict) else item
                for item in items
            ]
        else:
            value = getattr(args, key, None)
            if value is not None:
                parameters[key] = value

    if args.rights_file is not None:
        rights = _read_json_file(args.rights_file, what="rights record")
        if not isinstance(rights, dict):
            raise GenerationControlError(_CATEGORY_VALIDATION, _RUN_PARAMS_MESSAGE)
        parameters["rights"] = rights
    elif isinstance(parameters.get("rights"), dict):
        parameters["rights"] = _with_project(parameters["rights"], project_id)

    # Inject the project id into any ref that arrived via ``--params-file``.
    for key, kind in (*required, *optional):
        if kind == "ref" and isinstance(parameters.get(key), dict):
            parameters[key] = _with_project(parameters[key], project_id)

    parameters["operation"] = operation
    parameters["credential_env"] = worker_generation.GENERATION_CREDENTIAL_ENV
    return parameters


def _check_payload_size(parameters: dict) -> None:
    try:
        encoded = canonical_json(parameters)
    except (RecordValidationError, TypeError, ValueError):
        raise GenerationControlError(_CATEGORY_VALIDATION, _RUN_INVALID_MESSAGE) from None
    from ..runtime import ipc

    if len(encoded) > ipc.MAX_REQUEST_BYTES:
        raise GenerationControlError(_CATEGORY_VALIDATION, _RUN_INVALID_MESSAGE)


# --------------------------------------------------------------------------- #
# run handlers
# --------------------------------------------------------------------------- #
def generation_run_submit(args: argparse.Namespace, ctx: Any) -> dict[str, Any]:
    """``generation run submit`` -- submit one scoped generation role job.

    Every argument is validated locally (and the assembled parameters are
    canonicalised by the **real** generation submission validator) **before** a
    worker is ensured, so a bad value is a typed ``validation`` error and no daemon
    is started and no job is created. ``--credential-env`` must name the one
    approved variable, and the credential **value** is never accepted here. Only
    the queued admission is returned.
    """
    timeout = _resolve_timeout(args)
    project_id = _resolve_worker_project_id(ctx, args.project)
    request_id = _parse_identifier(args.request_id)
    if args.credential_env != worker_generation.GENERATION_CREDENTIAL_ENV:
        raise JobControlError(_CATEGORY_VALIDATION, _RUN_CREDENTIAL_ENV_MESSAGE)
    if args.operation not in worker_generation.SUPPORTED_OPERATIONS:
        raise GenerationControlError(_CATEGORY_VALIDATION, _RUN_OPERATION_MESSAGE)
    parameters = _assemble_parameters(args, project_id)
    _check_payload_size(parameters)
    try:
        canonical = worker_discovery.validate_submission(
            worker_discovery.MODE_GENERATION, parameters, project_id, request_id
        )
    except DiscoveryJobValidationError:
        raise GenerationControlError(_CATEGORY_VALIDATION, _RUN_INVALID_MESSAGE) from None
    request = {
        "version": PROTOCOL_VERSION,
        "op": "discovery_submit",
        "project_id": project_id,
        "request_id": request_id,
        "mode": worker_discovery.MODE_GENERATION,
        "parameters": canonical,
    }
    pid = _start_worker(ctx, timeout)
    result = _expect_ok(_rpc(ctx, pid, request, timeout))
    job = _job_projection(result.get("job"))
    output: dict[str, Any] = {
        "mode": worker_discovery.MODE_GENERATION,
        "operation": args.operation,
        "result_kind": worker_discovery.RESULT_KIND_GENERATION,
        "job": job,
        "job_id": job.get("job_id"),
        "submitted": True,
        "queued": True,
        "admission_only": True,
        "job_completion": False,
        "key_value_accepted": False,
        "scientific_validation": False,
        "authorizes_publication": False,
    }
    import os

    if not os.environ.get(worker_generation.GENERATION_CREDENTIAL_ENV):
        output["configuration_required"] = True
        output["notice"] = _RUN_MISSING_KEY_NOTICE
    else:
        output["configuration_required"] = False
    return output


def generation_run_status(args: argparse.Namespace, ctx: Any) -> dict[str, Any]:
    """``generation run status`` -- safe readback of one recorded run (no call).

    Reads the recorded job/outcome locally from the verified store; it starts no
    worker, issues no POST, and records no approval. The written reference and the
    adapter status are surfaced; the private model output is never included.
    """
    project_id = _resolve_project_id(ctx, args.project)
    request_id = _parse_identifier(args.request_id)
    with _verified_store(ctx, project_id) as store:
        run = worker_generation.persisted_generation_run(store, request_id=request_id)
        state = None
        operation = None
        if run is None:
            repo = JobRepository(store)
            for job in repo.list():
                if (
                    job.request_id == request_id
                    and job.task_kind == worker_generation.TASK_KIND_GENERATION
                ):
                    state = job.state
                    params = job.parameters if isinstance(job.parameters, dict) else {}
                    operation = params.get("operation")
                    break
        elif isinstance(run, dict):
            operation = run.get("operation")
    if run is None and state is None:
        raise GenerationControlError(_CATEGORY_VALIDATION, _RUN_NOT_FOUND_MESSAGE)
    return {
        "request_id": request_id,
        "mode": worker_generation.MODE_GENERATION,
        "operation": operation,
        "result_kind": worker_generation.RESULT_KIND_GENERATION,
        "run": run,
        "job_state": state,
        "authorizes_publication": False,
        "scientific_validation": False,
    }


# --------------------------------------------------------------------------- #
# dispatch
# --------------------------------------------------------------------------- #
GENERATION_RUN_COMMANDS: dict[str, Callable[[argparse.Namespace, Any], Any]] = {
    "submit": generation_run_submit,
    "status": generation_run_status,
}


def dispatch_generation(args: argparse.Namespace, ctx: Any) -> object | None:
    """Route one parsed ``generation`` invocation; ``None`` if unknown."""
    command = getattr(args, "generation_command", None)
    if command == "run":
        handler = GENERATION_RUN_COMMANDS.get(
            getattr(args, "generation_run_command", None)
        )
        return None if handler is None else handler(args, ctx)
    return None
