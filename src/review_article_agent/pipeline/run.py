"""``pipeline run`` orchestrator (kickoff 16 §6, the write half).

For one already-verified project store this module determines the current stage
from durable state alone (reusing the read-only :mod:`review_article_agent.pipeline.status`
resolver), derives the **next machine job(s)** the stage needs, and submits them to
the detached worker until the project reaches the next human gate, budget limit,
pause, or failure. It holds no authoritative state of its own: every decision is
re-derived from the same repositories the stages already use, and the worker's
durable jobs are the only idempotency record.

Two entry points:

* :func:`plan_next` -- **pure** (no writes, no network): from ``resolve()`` and a
  small project pipeline configuration it returns the ordered list of job specs the
  current stage needs. A spec is either a deterministic ``prepare_workspace`` step
  (a local, gate-free drafting workspace revision) or a ``generation_run`` worker
  job.
* :func:`run` -- executes those specs through an injected ``submit`` callable (the
  CLI supplies the real IPC client; tests supply the in-process worker engine),
  optionally waits for the jobs to settle, and re-plans until it stops.

Idempotence
-----------
Every ``generation_run`` spec carries a **deterministic request id** derived from
the stage, the section, and the exact input revisions. The worker replays an
identical request id (one job, never a second POST), and :func:`run` additionally
skips a request id that already exists in the durable ``JobRepository``. Re-running
``pipeline run`` therefore submits nothing new.

Configuration
-------------
``--config`` names a small JSON file (strict, stdlib only) that supplies the
information not recorded anywhere else: the credential **environment-variable
name** (never its value), the sections to drive, the per-role hosted-use artifact
ids (the current revision is read from the store), the target artifact/revision id
templates, and one rights **template** rebound to each written revision. See
``docs/cli.md`` §24 for the exact format.

This module never invents a model, a rights value, or a credential; a missing
configuration entry means "no automatic job for this stage -- report the manual
command" rather than a guessed default.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from ..domain.errors import ReviewAgentError
from ..domain.records import (
    RecordValidationError,
    canonical_json,
    validate_identifier,
)
from ..drafting.workspace import (
    DIRECT_BINDING_KIND,
    PLANNER_GENERATION_ROLE,
    WRITER_GENERATION_ROLE,
    SectionWorkspaceError,
    SectionWorkspaceRepository,
)
from ..runtime.generation import ROLES as GENERATION_ROLES
from ..runtime.jobs import JobRepository, TERMINAL_JOB_STATES
from ..storage.artifacts import ArtifactRepository
from ..synthesis.workflow import SynthesisWorkflowError, SynthesisWorkflowRepository
from ..worker import discovery as worker_discovery
from ..worker import generation as worker_generation
from ..worker.engine import PROTOCOL_VERSION
from .status import resolve

__all__ = [
    "DEFAULT_CREDENTIAL_ENV",
    "DEFAULT_MAX_STEPS",
    "PipelineConfigError",
    "PipelineRunError",
    "RolePlan",
    "PipelineConfig",
    "load_config",
    "plan_next",
    "run",
]

#: The one approved credential environment-variable name a generation job may use.
DEFAULT_CREDENTIAL_ENV = worker_generation.GENERATION_CREDENTIAL_ENV

#: Bounded default for the number of orchestration steps in one ``run`` call.
DEFAULT_MAX_STEPS = 10

#: The job states that stop a ``--wait`` (a human is needed, or the job ended).
_WAIT_STOP_STATES = frozenset(TERMINAL_JOB_STATES) | frozenset({"waiting_human", "paused"})

#: The generic document types this module scans for.
_OUTLINE_DOCUMENT_TYPE = "synthesis_outline"
_WORKSPACE_DOCUMENT_TYPE = "drafting_section_workspace"

#: The stage -> generation operation table for the stages this module can drive.
#: A stage absent from this table has no automatic job (its manual command is
#: reported by ``pipeline status``).
_STAGE_OPERATIONS = {
    "drafting.blueprint": "blueprint",
}

_M_BAD_CONFIG = (
    "the pipeline configuration file must be a strict JSON object naming "
    "credential_env, sections, roles, and the workspace/rights templates. "
    "Nothing was sent."
)
_M_MISSING_CREDENTIAL = (
    "the configured generation credential is not present in this process's "
    "environment; no job was submitted and nothing was written. Export the named "
    "environment variable (its value is never accepted on the command line), or "
    "configure the worker's environment, then retry."
)


class PipelineConfigError(ReviewAgentError):
    """The pipeline configuration is malformed or incomplete."""


class PipelineRunError(Exception):
    """A ``pipeline run`` step failed in a typed way.

    ``category`` is ``"validation"``, ``"conflict"``, or ``"storage"``; the
    message is always a static constant authored in this module.
    """

    def __init__(self, category: str, message: str, code: str | None = None) -> None:
        super().__init__(message)
        self.category = category
        self.code = code


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
def _template(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise PipelineConfigError(f"{field}: must be a non-empty string template")
    return value


def _identifier(value: object, field: str) -> str:
    try:
        return validate_identifier(value, field)
    except (RecordValidationError, TypeError, ValueError):
        raise PipelineConfigError(f"{field}: must be a valid opaque identifier") from None


@dataclass(frozen=True)
class RolePlan:
    """One generation role's configured inputs."""

    role: str
    hosted_artifact_id: str
    hosted_revision_id: str | None
    target_artifact_id: str | None
    target_revision_id: str | None


@dataclass(frozen=True)
class PipelineConfig:
    """A validated project pipeline configuration (never carries a secret value)."""

    credential_env: str
    sections: tuple[str, ...]
    roles: Mapping[str, RolePlan]
    workspace_artifact_id: str
    workspace_revision_id: str
    rights_template: dict | None

    @classmethod
    def from_mapping(cls, value: object) -> "PipelineConfig":
        if not isinstance(value, dict):
            raise PipelineConfigError(_M_BAD_CONFIG)
        allowed = {
            "credential_env",
            "sections",
            "roles",
            "workspace_artifact_id",
            "workspace_revision_id",
            "rights",
        }
        if not set(value) <= allowed:
            raise PipelineConfigError(_M_BAD_CONFIG)

        credential_env = value.get("credential_env", DEFAULT_CREDENTIAL_ENV)
        if credential_env != DEFAULT_CREDENTIAL_ENV:
            # Only the one approved variable name is ever accepted; the value is
            # never read here.
            raise PipelineConfigError(_M_BAD_CONFIG)

        raw_sections = value.get("sections", [])
        if not isinstance(raw_sections, (list, tuple)):
            raise PipelineConfigError(_M_BAD_CONFIG)
        sections = tuple(_identifier(s, "sections[]") for s in raw_sections)

        raw_roles = value.get("roles", {})
        if not isinstance(raw_roles, dict):
            raise PipelineConfigError(_M_BAD_CONFIG)
        roles: dict[str, RolePlan] = {}
        for role, spec in raw_roles.items():
            if not isinstance(role, str) or role not in GENERATION_ROLES:
                raise PipelineConfigError(_M_BAD_CONFIG)
            if not isinstance(spec, dict) or not set(spec) <= {
                "hosted_artifact_id",
                "hosted_revision_id",
                "target_artifact_id",
                "target_revision_id",
            }:
                raise PipelineConfigError(_M_BAD_CONFIG)
            if "hosted_artifact_id" not in spec:
                raise PipelineConfigError(_M_BAD_CONFIG)
            hosted_revision = spec.get("hosted_revision_id")
            target_artifact = spec.get("target_artifact_id")
            target_revision = spec.get("target_revision_id")
            roles[role] = RolePlan(
                role=role,
                hosted_artifact_id=_identifier(
                    spec["hosted_artifact_id"], "roles[].hosted_artifact_id"
                ),
                hosted_revision_id=(
                    None
                    if hosted_revision is None
                    else _identifier(hosted_revision, "roles[].hosted_revision_id")
                ),
                target_artifact_id=(
                    None
                    if target_artifact is None
                    else _template(target_artifact, "roles[].target_artifact_id")
                ),
                target_revision_id=(
                    None
                    if target_revision is None
                    else _template(target_revision, "roles[].target_revision_id")
                ),
            )

        workspace_artifact_id = value.get(
            "workspace_artifact_id", "ws-{section_id}"
        )
        workspace_revision_id = value.get(
            "workspace_revision_id", "ws-{section_id}-r1"
        )
        rights = value.get("rights")
        if rights is not None and not isinstance(rights, dict):
            raise PipelineConfigError(_M_BAD_CONFIG)
        return cls(
            credential_env=credential_env,
            sections=sections,
            roles=roles,
            workspace_artifact_id=_template(
                workspace_artifact_id, "workspace_artifact_id"
            ),
            workspace_revision_id=_template(
                workspace_revision_id, "workspace_revision_id"
            ),
            rights_template=copy.deepcopy(rights) if rights is not None else None,
        )


def load_config(path: str) -> PipelineConfig:
    """Read and strictly validate one pipeline configuration JSON file."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, ValueError, UnicodeError):
        raise PipelineConfigError(_M_BAD_CONFIG) from None
    return PipelineConfig.from_mapping(value)


# --------------------------------------------------------------------------- #
# Small durable-state readers
# --------------------------------------------------------------------------- #
def _heads(store: Any) -> list[dict]:
    artifacts = ArtifactRepository(store)
    heads: list[dict] = []
    for artifact in artifacts.list_artifacts():
        head = artifacts.current_revision(artifact.artifact_id)
        if head is None:
            continue
        try:
            document = json.loads(
                artifacts.read_content(artifact.artifact_id, head.revision_id).decode(
                    "utf-8"
                )
            )
        except Exception:  # noqa: BLE001 - a non-JSON head is not a candidate
            continue
        if isinstance(document, dict):
            heads.append(
                {
                    "project_id": store.project_id,
                    "artifact_id": artifact.artifact_id,
                    "revision_id": head.revision_id,
                    "document_type": document.get("document_type"),
                    "document": document,
                }
            )
    return heads


def _ref(entry: dict) -> dict:
    return {
        "project_id": entry["project_id"],
        "artifact_id": entry["artifact_id"],
        "revision_id": entry["revision_id"],
    }


def _current_approved_outline(store: Any) -> dict | None:
    """The exact current approved synthesis outline revision, or ``None``."""
    repo = SynthesisWorkflowRepository(store)
    for entry in _heads(store):
        if entry["document_type"] != _OUTLINE_DOCUMENT_TYPE:
            continue
        ref = _ref(entry)
        try:
            repo.require_outline(ref)
        except (SynthesisWorkflowError, ReviewAgentError, ValueError, KeyError):
            continue
        return ref
    return None


def _find_current_workspace(
    store: Any, outline_ref: dict, section_id: str
) -> dict | None:
    """The current workspace revision for an outline/section, or ``None``."""
    wanted_outline = canonical_json(
        {
            "project_id": outline_ref["project_id"],
            "artifact_id": outline_ref["artifact_id"],
            "revision_id": outline_ref["revision_id"],
        }
    )
    for entry in _heads(store):
        if entry["document_type"] != _WORKSPACE_DOCUMENT_TYPE:
            continue
        payload = entry["document"].get("payload") or {}
        input_payload = payload.get("input") or {}
        if input_payload.get("section_id") != section_id:
            continue
        stored_outline = input_payload.get("outline_ref")
        if not isinstance(stored_outline, dict):
            continue
        if canonical_json(
            {
                "project_id": stored_outline.get("project_id"),
                "artifact_id": stored_outline.get("artifact_id"),
                "revision_id": stored_outline.get("revision_id"),
            }
        ) != wanted_outline:
            continue
        return _ref(entry)
    return None


def _hosted_ref(store: Any, plan: RolePlan) -> dict:
    """The exact hosted-use revision for a role (the configured or current head)."""
    artifact_id = plan.hosted_artifact_id
    if plan.hosted_revision_id is not None:
        return {
            "project_id": store.project_id,
            "artifact_id": artifact_id,
            "revision_id": plan.hosted_revision_id,
        }
    head = ArtifactRepository(store).current_revision(artifact_id)
    if head is None:
        raise PipelineConfigError(
            "roles[].hosted_artifact_id: no revision exists for the configured "
            "hosted-use artifact"
        )
    return {
        "project_id": store.project_id,
        "artifact_id": artifact_id,
        "revision_id": head.revision_id,
    }


def _bind_rights(template: dict, project_id: str, artifact_id: str, revision_id: str) -> dict:
    """Rebind a rights template's project/subject to one written revision.

    The template supplies the rights *content* (operations, basis, privacy,
    authorization); this only retargets its subject to the exact revision the
    pipeline writes, exactly as the worker's section-review rebind does. No
    authority is created: rights eligibility is not a gate.
    """
    record = copy.deepcopy(template)
    subject = {
        "kind": "source_revision",
        "project_id": project_id,
        "artifact_id": artifact_id,
        "revision_id": revision_id,
    }
    record["project_id"] = project_id
    record["subject"] = subject
    record["rights_id"] = f"RIGHTS-{artifact_id}-{revision_id}"
    authorization = record.get("authorization")
    if isinstance(authorization, dict):
        authorization["against_subject"] = subject
    return record


def _request_id(stage_id: str, section_id: str, refs: list) -> str:
    material = canonical_json(
        {"stage": stage_id, "section": section_id, "refs": refs}
    )
    digest = hashlib.sha256(material).hexdigest()[:24]
    return f"pipe.{stage_id}.{section_id}.{digest}"


# --------------------------------------------------------------------------- #
# plan_next
# --------------------------------------------------------------------------- #
def _prepare_workspace_spec(
    store: Any,
    config: PipelineConfig,
    *,
    section_id: str,
    outline_ref: dict,
    blueprint: RolePlan,
    writer: RolePlan,
) -> dict:
    artifact_id = config.workspace_artifact_id.format(section_id=section_id)
    revision_id = config.workspace_revision_id.format(section_id=section_id)
    payload = {
        "outline_ref": outline_ref,
        "section_id": section_id,
        "planner": {
            "kind": DIRECT_BINDING_KIND,
            "role": PLANNER_GENERATION_ROLE,
            "hosted_ref": _hosted_ref(store, blueprint),
        },
        "writer": {
            "kind": DIRECT_BINDING_KIND,
            "role": WRITER_GENERATION_ROLE,
            "hosted_ref": _hosted_ref(store, writer),
        },
    }
    rights = _bind_rights(
        config.rights_template, store.project_id, artifact_id, revision_id
    )
    return {
        "kind": "prepare_workspace",
        "stage": "drafting.blueprint",
        "section_id": section_id,
        "artifact_id": artifact_id,
        "revision_id": revision_id,
        "payload": payload,
        "rights": rights,
    }


def _blueprint_spec(
    store: Any,
    config: PipelineConfig,
    *,
    section_id: str,
    outline_ref: dict,
    workspace_ref: dict,
    blueprint: RolePlan,
) -> dict:
    hosted_ref = _hosted_ref(store, blueprint)
    target_artifact_id = blueprint.target_artifact_id.format(section_id=section_id)
    target_revision_id = (blueprint.target_revision_id or "r1").format(
        section_id=section_id
    )
    rights = _bind_rights(
        config.rights_template, store.project_id, target_artifact_id, target_revision_id
    )
    parameters = {
        "operation": "blueprint",
        "workspace_ref": workspace_ref,
        "hosted_ref": hosted_ref,
        "artifact_id": target_artifact_id,
        "revision_id": target_revision_id,
        "rights": rights,
        "credential_env": worker_generation.GENERATION_CREDENTIAL_ENV,
    }
    request_id = _request_id(
        "drafting.blueprint",
        section_id,
        [outline_ref, workspace_ref, hosted_ref],
    )
    return {
        "kind": "generation_run",
        "stage": "drafting.blueprint",
        "section_id": section_id,
        "operation": "blueprint",
        "role": blueprint.role,
        "request_id": request_id,
        "parameters": parameters,
    }


def _plan_drafting_blueprint(
    store: Any, registry: Any, config: PipelineConfig
) -> list[dict]:
    outline_ref = _current_approved_outline(store)
    if outline_ref is None:
        return []
    blueprint = config.roles.get("blueprint")
    writer = config.roles.get("section_writer")
    if blueprint is None or config.rights_template is None:
        return []
    if blueprint.target_artifact_id is None:
        return []
    specs: list[dict] = []
    for section_id in config.sections:
        workspace_ref = _find_current_workspace(store, outline_ref, section_id)
        if workspace_ref is None:
            if writer is None:
                continue
            specs.append(
                _prepare_workspace_spec(
                    store,
                    config,
                    section_id=section_id,
                    outline_ref=outline_ref,
                    blueprint=blueprint,
                    writer=writer,
                )
            )
        else:
            specs.append(
                _blueprint_spec(
                    store,
                    config,
                    section_id=section_id,
                    outline_ref=outline_ref,
                    workspace_ref=workspace_ref,
                    blueprint=blueprint,
                )
            )
    return specs


def plan_next(
    store: Any, registry: Any, *, config: PipelineConfig
) -> list[dict]:
    """The ordered machine-job specs the current stage needs (pure; no writes).

    Returns ``[]`` when the project is complete, blocked on a human gate / budget
    / pause / failure (a ``resolve()`` blocker), or when the current stage has no
    configured automatic job. A returned spec is a plain dict: a
    ``prepare_workspace`` local step or a ``generation_run`` worker job.
    """
    state = resolve(store, registry)
    if state["stage"] == "complete" or state["blocked_on"] is not None:
        return []
    operation = _STAGE_OPERATIONS.get(state["stage"])
    if operation is None:
        return []
    if operation == "blueprint":
        return _plan_drafting_blueprint(store, registry, config)
    return []  # pragma: no cover - the table is closed


# --------------------------------------------------------------------------- #
# run
# --------------------------------------------------------------------------- #
def _job_exists(store: Any, request_id: str) -> bool:
    repo = JobRepository(store)
    for job in repo.list():
        if job.request_id == request_id:
            return True
    return False


def _build_request(store: Any, spec: dict) -> dict:
    try:
        canonical = worker_discovery.validate_submission(
            worker_discovery.MODE_GENERATION,
            spec["parameters"],
            store.project_id,
            spec["request_id"],
        )
    except Exception as exc:  # noqa: BLE001 - never echo the value
        raise PipelineRunError(
            "validation",
            "the derived generation job failed strict validation; no job was "
            "submitted.",
        ) from exc
    return {
        "version": PROTOCOL_VERSION,
        "op": "discovery_submit",
        "project_id": store.project_id,
        "request_id": spec["request_id"],
        "mode": worker_discovery.MODE_GENERATION,
        "parameters": canonical,
    }


def _execute_prepare_workspace(store: Any, spec: dict) -> None:
    try:
        SectionWorkspaceRepository(store).propose(
            spec["payload"],
            artifact_id=spec["artifact_id"],
            revision_id=spec["revision_id"],
            rights=spec["rights"],
        )
    except SectionWorkspaceError as exc:
        raise PipelineRunError(
            "validation",
            "the drafting workspace could not be written from the configured "
            "hosted-use bindings; nothing was sent.",
        ) from exc


def _next_command(state: dict) -> str | None:
    blocked = state.get("blocked_on")
    if isinstance(blocked, dict) and blocked.get("next_command"):
        return blocked["next_command"]
    action = state.get("next_action")
    if isinstance(action, dict):
        inputs = action.get("inputs")
        if isinstance(inputs, dict) and inputs.get("command"):
            return inputs["command"]
    return None


def _job_state(store: Any, request_id: str) -> str | None:
    repo = JobRepository(store)
    for job in repo.list():
        if job.request_id == request_id:
            return job.state
    return None


def _wait_for(
    store: Any,
    request_ids: list[str],
    *,
    timeout: float | None,
    poll: Callable[[str], str | None] | None,
    sleep: Callable[[float], None],
) -> None:
    if not request_ids:
        return
    budget = 60.0 if timeout is None else max(0.0, timeout)
    deadline = time.monotonic() + budget
    pending = list(request_ids)
    reader = poll if poll is not None else (lambda rid: _job_state(store, rid))
    interval = 0.05
    while pending and time.monotonic() < deadline:
        still: list[str] = []
        for request_id in pending:
            try:
                state = reader(request_id)
            except Exception:  # noqa: BLE001 - a read failure never blocks the loop
                state = None
            if state not in _WAIT_STOP_STATES:
                still.append(request_id)
        pending = still
        if not pending:
            return
        sleep(min(interval, max(0.0, deadline - time.monotonic())))
        interval = min(interval * 2, 0.5)


def run(
    store: Any,
    registry: Any,
    *,
    config: PipelineConfig,
    submit: Callable[[dict], Any],
    wait: bool = False,
    timeout: float | None = None,
    max_steps: int | None = None,
    environ: Mapping[str, str] | None = None,
    poll: Callable[[str], str | None] | None = None,
    sleep: Callable[[float], None] | None = None,
) -> dict:
    """Drive the project to its next gate/budget/pause/failure, submitting jobs.

    ``submit`` receives one version-1 ``discovery_submit`` request dict and returns
    whatever the transport returns (the CLI's IPC admission or the engine result);
    it is the only injection point. ``poll`` (optional) reads one request id's job
    state for ``--wait``. Idempotent: a request id already present in the durable
    job ledger is never re-submitted.
    """
    environ = os.environ if environ is None else environ
    sleep = time.sleep if sleep is None else sleep
    limit = DEFAULT_MAX_STEPS if max_steps is None else max(1, int(max_steps))

    submitted: list[str] = []
    already_done: list[str] = []
    steps = 0
    state = resolve(store, registry)

    # A model-backed stage that is otherwise ready needs its credential before any
    # local write or submission; refuse up front so a missing credential creates
    # neither a workspace nor a job.
    if (
        state["blocked_on"] is None
        and state["stage"] in _STAGE_OPERATIONS
        and config.roles
        and not environ.get(config.credential_env)
    ):
        raise PipelineRunError(
            "validation", _M_MISSING_CREDENTIAL, code="missing_credential"
        )

    while steps < limit:
        specs = plan_next(store, registry, config=config)
        if not specs:
            break

        needs_generation = any(spec["kind"] == "generation_run" for spec in specs)
        if needs_generation and not environ.get(config.credential_env):
            raise PipelineRunError(
                "validation", _M_MISSING_CREDENTIAL, code="missing_credential"
            )

        progressed = False
        pending_waits: list[str] = []
        for spec in specs:
            if steps >= limit:
                break
            if spec["kind"] == "prepare_workspace":
                steps += 1
                _execute_prepare_workspace(store, spec)
                progressed = True
                continue
            request_id = spec["request_id"]
            if request_id in submitted:
                # Already submitted earlier in this same call; never re-count it
                # as pre-existing work.
                continue
            if _job_exists(store, request_id):
                already_done.append(request_id)
                continue
            steps += 1
            request = _build_request(store, spec)
            submit(request)
            submitted.append(request_id)
            pending_waits.append(request_id)
            progressed = True

        if wait and pending_waits:
            _wait_for(
                store, pending_waits, timeout=timeout, poll=poll, sleep=sleep
            )

        state = resolve(store, registry)
        if not progressed:
            break

    return {
        "stage": state["stage"],
        "stopped_at": state.get("blocked_on"),
        "next_action": state.get("next_action"),
        "next_command": _next_command(state),
        "submitted": submitted,
        "already_done": already_done,
        "authorizes_execution": False,
        "scientific_validation": False,
    }
