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
import json
from datetime import datetime, timezone
from typing import Any, Callable

from ..domain.errors import ReviewAgentError, StoreError
from ..domain.records import RecordValidationError, canonical_json
from ..generation import hosted_use
from ..runtime import generation as runtime_generation
from ..runtime.jobs import JobRepository
from ..storage import ArtifactConflictError
from ..storage.artifacts import ArtifactRepository
from ..storage.human_approvals import HumanApprovalRepository
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
    "generation_hosted_use_propose",
    "generation_hosted_use_show",
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
_HOSTED_USE_INVALID_MESSAGE = (
    "the generation hosted-use document failed strict typed validation (a wrong or "
    "missing envelope/payload field, an unknown role, an invalid configuration, a "
    "non-positive budget cap, or a malformed evidence record). Nothing was written. "
    "The supplied values are not shown."
)
_HOSTED_USE_CONFLICT_MESSAGE = (
    "the generation hosted-use artifact already exists with a different kind, or a "
    "divergent revision already occupies that artifact/revision. Nothing was written."
)
_HOSTED_USE_NOT_FOUND_MESSAGE = (
    "no stored generation hosted-use revision matches that artifact/revision in this "
    "project; nothing was read."
)
_HOSTED_USE_ROLE_MESSAGE = (
    "invalid --role; the value is not one of the recognized generation roles and is "
    "not shown. Run --help for the supported roles."
)


def _utc_now_rfc3339() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


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

    hosted = actions.add_parser(
        "hosted-use",
        help=(
            "per-role generation hosted-use authorization documents: propose one "
            "(stores an inert revision and prints its gate commands) and show one "
            "(the validated document and whether its gate is satisfied)"
        ),
    )
    hosted_sub = hosted.add_subparsers(
        dest="generation_hosted_use_command",
        required=True,
        metavar="HOSTED_USE_COMMAND",
    )

    p_hu_propose = hosted_sub.add_parser(
        "propose",
        help=(
            "validate and store one generation_hosted_use document as an inert "
            "revision (no approval); prints the exact gate prepare/confirm commands"
        ),
    )
    p_hu_propose.add_argument("--project", metavar="ID")
    p_hu_propose.add_argument(
        "--role",
        required=True,
        choices=runtime_generation.ROLES,
        help="the generation role this authorization is for",
    )
    p_hu_propose.add_argument(
        "--config-file",
        required=True,
        metavar="PATH",
        help="strict JSON object: the LeadConfig (concrete model + pricing) for the role",
    )
    p_hu_propose.add_argument(
        "--budget-cap-usd",
        required=True,
        metavar="DECIMAL",
        help="exact decimal string > 0 (the cumulative reserve cap for this role)",
    )
    p_hu_propose.add_argument(
        "--provider-terms-ref", required=True, metavar="REF",
        help="non-empty reference to the human-reviewed provider-terms evidence",
    )
    p_hu_propose.add_argument("--provider-terms-verified-at", metavar="TS")
    p_hu_propose.add_argument(
        "--privacy-review-ref", required=True, metavar="REF",
        help="non-empty reference to the human-reviewed privacy-review evidence",
    )
    p_hu_propose.add_argument("--privacy-review-verified-at", metavar="TS")
    p_hu_propose.add_argument(
        "--pricing-evidence-ref", required=True, metavar="REF",
        help="non-empty reference to the human-reviewed pricing evidence",
    )
    p_hu_propose.add_argument("--pricing-evidence-verified-at", metavar="TS")
    p_hu_propose.add_argument("--notes", metavar="TEXT")
    p_hu_propose.add_argument("--artifact-id", required=True, metavar="ID")
    p_hu_propose.add_argument("--revision-id", required=True, metavar="ID")
    p_hu_propose.add_argument("--rights-file", required=True, metavar="PATH")

    p_hu_show = hosted_sub.add_parser(
        "show",
        help="print one stored generation hosted-use document and its gate status",
    )
    p_hu_show.add_argument("--project", metavar="ID")
    p_hu_show.add_argument("--artifact-id", required=True, metavar="ID")
    p_hu_show.add_argument("--revision-id", metavar="ID")


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
# hosted-use handlers
# --------------------------------------------------------------------------- #
def _hosted_use_gate_block(
    store: Any, *, artifact_id: str, revision_id: str, role: str
) -> dict[str, Any]:
    """The non-authorizing gate status plus the exact gate CLI commands.

    ``satisfied`` is the real repository's answer **for this exact revision**: it
    can only be true when this revision is still the artifact's current head. The
    ``prepare_command``/``confirm_command`` strings are the exact commands an owner
    runs to record the out-of-band ``batch``/``generation.hosted_use.<role>`` gate.
    """
    gate_id = hosted_use.hosted_use_gate_id(role)
    head = ArtifactRepository(store).current_revision(artifact_id)
    is_current = head is not None and head.revision_id == revision_id
    satisfied = is_current and HumanApprovalRepository(store).named_gate_satisfied(
        artifact_id, gate_kind=hosted_use.HOSTED_USE_GATE_KIND, gate_id=gate_id
    )
    base = (
        f"--artifact-id {artifact_id} --revision-id {revision_id} "
        f"--gate-kind {hosted_use.HOSTED_USE_GATE_KIND} --gate-id {gate_id} "
        "--decision approved --rationale <RATIONALE>"
    )
    return {
        "gate_kind": hosted_use.HOSTED_USE_GATE_KIND,
        "gate_id": gate_id,
        "artifact_id": artifact_id,
        "revision_id": revision_id,
        "satisfied": bool(satisfied),
        "current": bool(is_current),
        "stale": not is_current,
        "prepare_command": "gate prepare " + base,
        "confirm_command": (
            "gate confirm " + base + " --snapshot-digest <SHA256 from gate prepare>"
        ),
        "authorizes_execution": False,
    }


def generation_hosted_use_propose(args: argparse.Namespace, ctx: Any) -> dict[str, Any]:
    """``generation hosted-use propose`` -- store one hosted-use document.

    Strictly validates the envelope/payload with ``validate_hosted_use_document``
    (nothing is written on a malformed document), stores it through
    ``propose_hosted_use`` as one immutable artifact revision with the caller's
    rights, and prints the exact ``gate prepare``/``gate confirm`` commands for the
    new revision. It records **no** approval and accepts **no** secret.
    """
    project_id = _resolve_project_id(ctx, args.project)
    role = args.role
    if role not in runtime_generation.ROLES:
        raise GenerationControlError(_CATEGORY_VALIDATION, _HOSTED_USE_ROLE_MESSAGE)
    config = _read_json_file(args.config_file, what="hosted-use configuration")
    if not isinstance(config, dict):
        raise GenerationControlError(_CATEGORY_VALIDATION, _HOSTED_USE_INVALID_MESSAGE)

    def _evidence(ref: object, verified_at: object) -> dict[str, Any]:
        return {
            "ref": ref,
            "verified_at": verified_at if verified_at is not None else _utc_now_rfc3339(),
        }

    document = {
        "schema_version": hosted_use.SCHEMA_VERSION,
        "document_type": hosted_use.HOSTED_USE_DOCUMENT_TYPE,
        "payload": {
            "role": role,
            "config": config,
            "budget_cap_usd": args.budget_cap_usd,
            "provider_terms": _evidence(
                args.provider_terms_ref, args.provider_terms_verified_at
            ),
            "privacy_review": _evidence(
                args.privacy_review_ref, args.privacy_review_verified_at
            ),
            "pricing_evidence": _evidence(
                args.pricing_evidence_ref, args.pricing_evidence_verified_at
            ),
            "notes": args.notes,
        },
    }
    try:
        validated = hosted_use.validate_hosted_use_document(document)
    except hosted_use.GenerationHostedUseError:
        raise GenerationControlError(_CATEGORY_VALIDATION, _HOSTED_USE_INVALID_MESSAGE) from None

    rights = _read_json_file(args.rights_file, what="rights record")
    if not isinstance(rights, dict):
        raise GenerationControlError(_CATEGORY_VALIDATION, _HOSTED_USE_INVALID_MESSAGE)

    with _verified_store(ctx, project_id) as store:
        try:
            ref = hosted_use.propose_hosted_use(
                store,
                validated,
                artifact_id=args.artifact_id,
                revision_id=args.revision_id,
                rights=rights,
            )
        except ArtifactConflictError:
            raise GenerationControlError(_CATEGORY_CONFLICT, _HOSTED_USE_CONFLICT_MESSAGE) from None
        except hosted_use.GenerationHostedUseError:
            raise GenerationControlError(_CATEGORY_VALIDATION, _HOSTED_USE_INVALID_MESSAGE) from None
        except (RecordValidationError, ReviewAgentError, ValueError, TypeError, OSError):
            raise GenerationControlError(_CATEGORY_VALIDATION, _HOSTED_USE_INVALID_MESSAGE) from None
        gate = _hosted_use_gate_block(
            store,
            artifact_id=ref["artifact_id"],
            revision_id=ref["revision_id"],
            role=role,
        )
    return {
        "revision": ref,
        "role": role,
        "document": validated,
        "gate": gate,
        "gate_satisfied": gate["satisfied"],
        "authorizes_execution": False,
        "scientific_validation": False,
        "approval_recorded": False,
    }


def generation_hosted_use_show(args: argparse.Namespace, ctx: Any) -> dict[str, Any]:
    """``generation hosted-use show`` -- print a stored document and its gate status.

    Reads one exact revision (the current head when ``--revision-id`` is omitted),
    validates its bytes with ``validate_hosted_use_document``, and reports whether
    the exact ``batch``/``generation.hosted_use.<role>`` gate is satisfied for that
    revision. No secret is printed and nothing is written.
    """
    project_id = _resolve_project_id(ctx, args.project)
    artifact_id = _parse_identifier(args.artifact_id)
    with _verified_store(ctx, project_id) as store:
        repo = ArtifactRepository(store)
        if args.revision_id is None:
            head = repo.current_revision(artifact_id)
            if head is None:
                raise GenerationControlError(
                    _CATEGORY_VALIDATION, _HOSTED_USE_NOT_FOUND_MESSAGE
                )
            revision_id = head.revision_id
        else:
            revision_id = _parse_identifier(args.revision_id)
        try:
            content = repo.read_content(artifact_id, revision_id)
        except (RecordValidationError, ReviewAgentError, ValueError, TypeError, OSError):
            raise GenerationControlError(
                _CATEGORY_VALIDATION, _HOSTED_USE_NOT_FOUND_MESSAGE
            ) from None
        try:
            document = hosted_use.validate_hosted_use_document(json.loads(content))
        except (hosted_use.GenerationHostedUseError, ValueError, UnicodeDecodeError):
            raise GenerationControlError(
                _CATEGORY_VALIDATION, _HOSTED_USE_INVALID_MESSAGE
            ) from None
        role = document["payload"]["role"]
        gate = _hosted_use_gate_block(
            store, artifact_id=artifact_id, revision_id=revision_id, role=role
        )
    return {
        "revision": {
            "project_id": project_id,
            "artifact_id": artifact_id,
            "revision_id": revision_id,
        },
        "role": role,
        "document": document,
        "gate": gate,
        "gate_satisfied": gate["satisfied"],
        "authorizes_execution": False,
        "scientific_validation": False,
    }


# --------------------------------------------------------------------------- #
# dispatch
# --------------------------------------------------------------------------- #
GENERATION_RUN_COMMANDS: dict[str, Callable[[argparse.Namespace, Any], Any]] = {
    "submit": generation_run_submit,
    "status": generation_run_status,
}

GENERATION_HOSTED_USE_COMMANDS: dict[str, Callable[[argparse.Namespace, Any], Any]] = {
    "propose": generation_hosted_use_propose,
    "show": generation_hosted_use_show,
}


def dispatch_generation(args: argparse.Namespace, ctx: Any) -> object | None:
    """Route one parsed ``generation`` invocation; ``None`` if unknown."""
    command = getattr(args, "generation_command", None)
    if command == "run":
        handler = GENERATION_RUN_COMMANDS.get(
            getattr(args, "generation_run_command", None)
        )
        return None if handler is None else handler(args, ctx)
    if command == "hosted-use":
        handler = GENERATION_HOSTED_USE_COMMANDS.get(
            getattr(args, "generation_hosted_use_command", None)
        )
        return None if handler is None else handler(args, ctx)
    return None
