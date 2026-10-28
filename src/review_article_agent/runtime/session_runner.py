"""Thin application-facing bounded OpenCode submit/reconcile path.

This is the small vertical slice between the existing persisted task/session
stores and the existing OpenCode V2 client. It deliberately is **not** another
orchestration framework, scheduler, worker, or CLI:

* :func:`submit_session_turn` creates one fresh owned-location session for a
  persisted, reserved ``opencode_session`` dispatch, persists the exact
  app-owned mapping and the pre-transport session-turn correlation, revalidates
  the fixture-only execution boundary and the live session pins, then records
  the single-use send claim and admits the deterministic application message.
  It returns the durable admission without waiting for the turn to finish.
* :func:`reconcile_session_turn` is stateless: it reads the exact persisted
  intent, proves the current owned service/location and session pins, reconciles
  from durable server messages (never events), and only an authoritative idle
  success may persist a bounded proposal and accept the dispatch's single local
  result. A missing or ambiguous terminal remains ``unknown`` with its slot held.

Safety boundaries that are *not* relaxed here:

* no paid provider, hosted/real-source route, or production generation is
  authorized; the existing :func:`assert_fixture_session_execution_allowed`
  gate must pass before the send claim is recorded and no bypass argument exists;
* a proposal remains non-authorizing model data: it cannot approve, edit
  authoritative state, widen scope, or grant a tool/approval;
* remote I/O is performed outside registry locks and SQLite transactions; only
  short local commits are fenced;
* the only in-process seam is an injected client used by deterministic tests.
  The application default constructs the real :class:`OpenCodeV2Client` from the
  owned manager's supported connection/password interface.

Known limitation: a real disposable OpenCode service end-to-end run is a separate
acceptance task. These functions prove the application-facing lifecycle over the
existing stores, registry, and owned manager layout; the model transport in tests
is an injected deterministic seam.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Iterator, Mapping

from ..domain.errors import ReviewAgentError
from ..domain.records import canonical_json, validate_identifier
from .dispatch import (
    DispatchConflictError,
    DispatchCorruptionError,
    DispatchRecord,
    DispatchStateError,
    DispatchValidationError,
    RuntimeDispatchRepository,
)
from .execution_boundary import (
    ExecutionBoundaryBlocked,
    assert_fixture_session_execution_allowed,
)
from .opencode import (
    Admission,
    OpenCodeError,
    OpenCodeV2Client,
    ReconcileResult,
    TURN_OPENING_TYPES,
)
from .roles import AgentRoleError, ModelRef, ProposalError, RoleConfiguration
from .service import OpenCodeServiceManager, ProjectLocation
from .session_turns import SessionTurnSpec, prepare_session_turn
from ..storage.agent_runtime import (
    RuntimeConflictError,
    RuntimeRepository,
    RuntimeSessionMapping,
    RuntimeTaskRecord,
    RuntimeValidationError,
    validate_version_set,
)
from ..storage.session_intents import (
    SessionIntentError,
    SessionIntentRecord,
    SessionIntentRepository,
)

__all__ = [
    "SESSION_RUNNER_RESULT_KIND",
    "ReconcileOutcome",
    "SessionRunnerConflictError",
    "SessionRunnerError",
    "SessionRunnerStateError",
    "SessionRunnerValidationError",
    "SubmitAdmission",
    "reconcile_session_turn",
    "submit_session_turn",
]

#: The dispatch ``result_kind`` accepted for a persisted session proposal.
SESSION_RUNNER_RESULT_KIND = "runtime_proposal"

_SESSION_ROUTE = "opencode_session"
_CONSUMED_VERSION_SLOTS = ("model", "prompt")

#: Static, value-free refusal messages. No id, digest, path, model, or peer text.
_M_BLOCKED = "session runner: the request is not a valid persisted bounded turn."
_M_MANAGER = "manager: must be the real owned OpenCode service manager."
_M_LOCATION = "manager: no current owned per-project location is available."
_M_CONFIG = "configuration: does not match the persisted bounded task."
_M_STEPS = "step_limit: does not match the persisted bounded task."
_M_VARIANT = "model: an explicit variant is required; omission is refused."
_M_CRITIC = "section_critic: the critic must be a different concrete model."
_M_DISPATCH = "dispatch: must be a persisted opencode_session dispatch."
_M_DISPATCH_STATE = "dispatch: must be reserved before a new session is created."
_M_DISPATCH_MISMATCH = "dispatch: does not match the persisted dispatch."
_M_TASK = "task: must be a persisted RuntimeTaskRecord."
_M_TASK_MISMATCH = "task: does not match the persisted task."
_M_INTENT = "intent: no durable session-turn intent is recorded for this dispatch."
_M_INTENT_MISMATCH = "intent: does not match the supplied dispatch/task/version/text."
_M_CORRUPT = "store: a persisted record failed integrity revalidation."
_M_SESSION = "session: the created session did not prove its role/model pins."
_M_SESSION_EMPTY = "session: a fresh session must have empty inbox and messages before send."
_M_REPLAY = "intent: the persisted correlation is not an exact replay of this request."
_M_UNKNOWN = "the durable remote outcome is unknown; reconcile before any retry."
_M_COMPLETED = "dispatch: the accepted result triple is not the recorded proposal."
_M_WINDOW = "messages: the correlated assistant window is missing or unreadable."
_M_MALFORMED = "message: the authoritative success did not contain a valid proposal."
_M_COST = "cost: the reported actual cost is not defensible; retaining unknown reserve."


class SessionRunnerError(ReviewAgentError):
    """A bounded session submit/reconcile request was refused (fail closed)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class SessionRunnerValidationError(SessionRunnerError, ValueError):
    """An input, persisted parent, or configuration is missing or inconsistent."""


class SessionRunnerStateError(SessionRunnerError):
    """The durable session/dispatch state cannot safely proceed."""


class SessionRunnerConflictError(SessionRunnerError):
    """A replay or accepted result conflicts with the durable identity."""


@dataclass(frozen=True)
class SubmitAdmission:
    """The durable admission identity returned by :func:`submit_session_turn`.

    ``state`` is the dispatch's durable state after the call. ``admitted`` means a
    durable server receipt was proven; ``unknown`` means the send outcome is
    ambiguous and must be reconciled, never retried under a new identity. The
    function returns as soon as admission is established and does not wait for a
    turn terminal.
    """

    dispatch_id: str
    intent_id: str
    spec_digest: str
    message_digest: str
    admission_id: str
    session_id: str
    state: str
    duplicate: bool = False
    reconciled: bool = False
    reconciliation_required: bool = False
    reason: str | None = None


@dataclass(frozen=True)
class ReconcileOutcome:
    """The bounded durable outcome of one reconciliation call.

    ``state`` follows the client's durable states plus ``reserved`` (no send claim
    was ever recorded). ``completed`` is the only state that carries an accepted
    proposal/result triple. ``duplicate`` means the exact accepted triple was
    already present and this call performed no new write or remote read.
    """

    dispatch_id: str
    state: str
    reconciliation_required: bool
    duplicate: bool = False
    reason: str | None = None
    proposal_record_id: str | None = None
    result_kind: str | None = None
    result_ref: str | None = None
    result_digest: str | None = None
    cost_state: str | None = None
    cost_amount_usd: str | None = None


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def _project_id(store: object) -> str:
    value = getattr(store, "project_id", None)
    if not isinstance(value, str):
        raise SessionRunnerValidationError("store", _M_BLOCKED)
    try:
        return validate_identifier(value, "project_id")
    except ValueError as exc:
        raise SessionRunnerValidationError("store", _M_BLOCKED) from exc


@contextmanager
def _execution_guard(
    registry: object,
    activation_token: str,
    project_id: str,
    *,
    allow_draining: bool = False,
) -> Iterator[None]:
    """Hold the matching registry execution guard for one short local commit."""
    guard = getattr(registry, "execution_write_guard", None)
    if not callable(guard):
        raise SessionRunnerValidationError("registry", _M_BLOCKED)
    with guard(
        activation_token, project_id, allow_draining=allow_draining
    ):
        yield


def _verify_execution_token(
    registry: object, activation_token: str, project_id: str, *, allow_draining: bool
) -> None:
    """Prove the caller-supplied token belongs to this project's execution."""
    if not isinstance(activation_token, str) or not activation_token:
        raise SessionRunnerValidationError("token", _M_BLOCKED)
    with _execution_guard(
        registry, activation_token, project_id, allow_draining=allow_draining
    ):
        pass


def _persisted_task(
    runtime: RuntimeRepository, project_id: str, task: object
) -> RuntimeTaskRecord:
    if not isinstance(task, RuntimeTaskRecord):
        raise SessionRunnerValidationError("task", _M_TASK)
    try:
        stored = runtime.get_task(task.task_id)
    except (RuntimeValidationError, ValueError) as exc:
        raise SessionRunnerValidationError("task", _M_CORRUPT) from exc
    if stored is None or stored.project_id != project_id:
        raise SessionRunnerValidationError("task", _M_TASK_MISMATCH)
    if stored.to_mapping() != task.to_mapping():
        raise SessionRunnerValidationError("task", _M_TASK_MISMATCH)
    return stored


def _persisted_dispatch(
    dispatch_repo: RuntimeDispatchRepository, project_id: str, dispatch: object
) -> DispatchRecord:
    if not isinstance(dispatch, DispatchRecord):
        raise SessionRunnerValidationError("dispatch", _M_DISPATCH)
    try:
        stored = dispatch_repo.get_dispatch(dispatch.dispatch_id)
    except (DispatchValidationError, DispatchCorruptionError, ValueError) as exc:
        raise SessionRunnerValidationError("dispatch", _M_CORRUPT) from exc
    if stored is None or stored.project_id != project_id:
        raise SessionRunnerValidationError("dispatch", _M_DISPATCH)
    # The operational state changes across submit/reconcile; only the immutable
    # dispatch identity must match the caller's record.
    if stored.identity_tuple() != dispatch.identity_tuple():
        raise SessionRunnerValidationError("dispatch", _M_DISPATCH_MISMATCH)
    return stored


def _configuration(runtime: RuntimeRepository, task: RuntimeTaskRecord) -> RoleConfiguration:
    try:
        snapshot = runtime.get_configuration(task.config_snapshot_id)
    except (RuntimeValidationError, ValueError) as exc:
        raise SessionRunnerValidationError("configuration", _M_CORRUPT) from exc
    if snapshot is None or snapshot.config_digest != task.config_digest:
        raise SessionRunnerValidationError("configuration", _M_CONFIG)
    configuration = snapshot.role_configuration()
    try:
        configured = configuration.for_role(task.role)
    except AgentRoleError as exc:
        raise SessionRunnerValidationError("configuration", _M_CONFIG) from exc
    try:
        stored = runtime.get_task(task.task_id)
    except (RuntimeValidationError, ValueError) as exc:
        raise SessionRunnerValidationError("task", _M_CORRUPT) from exc
    if stored is None:
        raise SessionRunnerValidationError("task", _M_TASK_MISMATCH)
    # The stored task's own canonical role configuration must equal this snapshot's
    # role configuration. Rebuilding it is the fail-closed check.
    from .roles import BoundedTask

    try:
        import json

        bounded = BoundedTask.from_mapping(
            json.loads(stored.canonical.decode("utf-8")), configuration=configuration
        )
    except Exception as exc:  # noqa: BLE001 - any rebuild failure is a static refusal
        raise SessionRunnerValidationError("task", _M_CORRUPT) from exc
    if bounded.role_config != configured or bounded.fingerprint() != stored.fingerprint:
        raise SessionRunnerValidationError("configuration", _M_CONFIG)
    return configuration


def _role_config(configuration: RoleConfiguration, role: str):
    try:
        return configuration.for_role(role)
    except AgentRoleError as exc:
        raise SessionRunnerValidationError("configuration", _M_CONFIG) from exc


def _preflight_manager(
    manager: object,
    project_id: str,
    role: str,
    configuration: RoleConfiguration,
) -> ProjectLocation:
    """Local preflight before creating an empty session (no prompt, no boundary).

    An empty session may be created before the boundary/spec exists, but the role,
    model/variant, configuration and owned location must already be coherent so a
    mismatched configuration cannot create a misleading session.
    """
    if not isinstance(manager, OpenCodeServiceManager):
        raise SessionRunnerValidationError("manager", _M_MANAGER)
    manager_configuration = getattr(manager, "configuration", None)
    if not isinstance(manager_configuration, RoleConfiguration):
        raise SessionRunnerValidationError("manager", _M_MANAGER)
    try:
        configured = manager_configuration.for_role(role)
    except AgentRoleError as exc:
        raise SessionRunnerValidationError("configuration", _M_CONFIG) from exc
    if configured != configuration.for_role(role):
        raise SessionRunnerValidationError("configuration", _M_CONFIG)
    if configured.model.variant is None:
        raise SessionRunnerValidationError("model", _M_VARIANT)
    if role == "section_critic":
        try:
            configuration.validate_section_critic()
        except AgentRoleError as exc:
            raise SessionRunnerValidationError("section_critic", _M_CRITIC) from exc
    try:
        location = manager.existing_location(project_id)
    except ReviewAgentError as exc:
        raise SessionRunnerStateError("manager", _M_LOCATION) from exc
    except Exception as exc:  # noqa: BLE001 - any failure is a static refusal
        raise SessionRunnerStateError("manager", _M_LOCATION) from exc
    if not isinstance(location, ProjectLocation):
        raise SessionRunnerValidationError("manager", _M_MANAGER)
    if location.app_project_id != project_id:
        raise SessionRunnerValidationError("session", _M_LOCATION)
    return location


def _canonical_versions(versions: object) -> dict[str, Any]:
    raw = versions.as_mapping() if hasattr(versions, "as_mapping") else versions
    try:
        return validate_version_set(raw, consumed=_CONSUMED_VERSION_SLOTS)
    except Exception as exc:  # noqa: BLE001 - canonical/version failure is static
        raise SessionRunnerValidationError("versions", _M_BLOCKED) from exc


def _check_versions(
    canonical_versions: Mapping[str, Any],
    dispatch: DispatchRecord,
    role_config,
    prompt_version: str,
) -> None:
    if canonical_versions.get("route") != _SESSION_ROUTE:
        raise SessionRunnerValidationError("versions", _M_BLOCKED)
    if canonical_versions.get("input_digest") != dispatch.input_digest:
        raise SessionRunnerValidationError("versions", _M_BLOCKED)
    import hashlib

    if (
        hashlib.sha256(canonical_json(canonical_versions)).hexdigest()
        != dispatch.version_digest
    ):
        raise SessionRunnerValidationError("versions", _M_BLOCKED)
    model_slot = canonical_versions.get("model")
    prompt_slot = canonical_versions.get("prompt")
    if (
        not isinstance(model_slot, Mapping)
        or model_slot.get("state") != "known"
        or not isinstance(model_slot.get("ref"), Mapping)
        or model_slot["ref"].get("version") != role_config.model.ref
        or not isinstance(prompt_slot, Mapping)
        or prompt_slot.get("state") != "known"
        or not isinstance(prompt_slot.get("ref"), Mapping)
        or prompt_slot["ref"].get("version") != prompt_version
    ):
        raise SessionRunnerValidationError("versions", _M_BLOCKED)


def _canonical_text(text: object, size_limit: int) -> bytes:
    try:
        encoded = canonical_json(text)
    except Exception as exc:  # noqa: BLE001 - canonical text failure is static
        raise SessionRunnerValidationError("text", _M_BLOCKED) from exc
    if len(encoded) > size_limit:
        raise SessionRunnerValidationError("text", _M_BLOCKED)
    return encoded


def _model_config(role_config) -> dict[str, str]:
    """The concrete OpenCode ``Model.Ref`` (``id``/``providerID``/``variant``)."""
    model = role_config.model
    if model.variant is None:
        raise SessionRunnerValidationError("model", _M_VARIANT)
    return {
        "providerID": model.provider_id,
        "id": model.model_id,
        "variant": model.variant,
    }


def _session_id(created: object) -> str:
    if not isinstance(created, Mapping):
        raise SessionRunnerStateError("session", _M_SESSION)
    session_id = created.get("id")
    if not isinstance(session_id, str) or not session_id:
        raise SessionRunnerStateError("session", _M_SESSION)
    return session_id


def _build_default_client(manager: OpenCodeServiceManager, location: ProjectLocation):
    """Construct the real client from the owned manager's supported interface.

    Only the service auth password is read, through ``manager.read_password``; no
    provider key, private OpenCode database, or registration secret is touched.
    """
    connection = manager.connection()
    return OpenCodeV2Client(
        connection.base_url,
        auth="basic",
        username=connection.username,
        password=manager.read_password(),
        directory=location.directory,
    )


def _empty_session(client: object, session_id: str) -> None:
    try:
        inbox = client.list_inbox(session_id)
        page = client.list_messages(session_id)
    except (OpenCodeError, ValueError) as exc:
        raise SessionRunnerStateError("session", _M_SESSION) from exc
    if inbox:
        raise SessionRunnerStateError("session", _M_SESSION_EMPTY)
    if not isinstance(page, Mapping) or page.get("data") != []:
        raise SessionRunnerStateError("session", _M_SESSION_EMPTY)


def _receipt(
    intent: SessionIntentRecord,
    dispatch: DispatchRecord,
    *,
    duplicate: bool,
    reconciled: bool = False,
    reason: str | None = None,
) -> SubmitAdmission:
    state = dispatch.state
    return SubmitAdmission(
        dispatch_id=dispatch.dispatch_id,
        intent_id=intent.intent_id,
        spec_digest=intent.spec_digest,
        message_digest=intent.message_digest,
        admission_id=intent.admission_id,
        session_id=intent.session_id,
        state=state,
        duplicate=duplicate,
        reconciled=reconciled,
        reconciliation_required=state in ("submitted", "unknown"),
        reason=reason,
    )


def _verify_replay(
    intent: SessionIntentRecord,
    *,
    project_id: str,
    task: RuntimeTaskRecord,
    dispatch: DispatchRecord,
    role_config,
    text_bytes: bytes,
    canonical_versions: Mapping[str, Any],
) -> SessionTurnSpec:
    try:
        spec = intent.spec()
    except Exception as exc:  # noqa: BLE001 - a corrupt intent is a static refusal
        raise SessionRunnerValidationError("intent", _M_CORRUPT) from exc
    message = intent.message_mapping()
    import hashlib

    if (
        intent.project_id != project_id
        or intent.task_id != task.task_id
        or intent.task_fingerprint != task.fingerprint
        or intent.dispatch_id != dispatch.dispatch_id
        or intent.role != task.role
        or intent.version_digest != dispatch.version_digest
        or intent.input_digest != dispatch.input_digest
        or spec.dispatch_id != dispatch.dispatch_id
        or spec.task_id != task.task_id
        or spec.task_fingerprint != task.fingerprint
        or spec.model != role_config.model.ref
        or message.get("text") is None
        or canonical_json(message["text"]) != text_bytes
        or hashlib.sha256(canonical_json(canonical_versions)).hexdigest()
        != intent.version_digest
    ):
        raise SessionRunnerConflictError("intent", _M_REPLAY)
    return spec


# --------------------------------------------------------------------------- #
# Submit
# --------------------------------------------------------------------------- #
def submit_session_turn(
    store: object,
    *,
    registry: object,
    activation_token: str,
    task: RuntimeTaskRecord,
    dispatch: DispatchRecord,
    manager: object,
    text: object,
    versions: object,
    client: object | None = None,
) -> SubmitAdmission:
    """Submit one bounded turn and return its durable admission identity.

    A repeated call for the same dispatch validates and returns the existing
    correlation without creating another session or issuing another prompt; a
    divergent replay is refused. The function never waits for completion.
    """
    project_id = _project_id(store)
    _verify_execution_token(registry, activation_token, project_id, allow_draining=False)
    runtime = RuntimeRepository(store)
    dispatch_repo = RuntimeDispatchRepository(store, runtime=runtime)
    intents = SessionIntentRepository(store, runtime=runtime, dispatch=dispatch_repo)

    persisted_task = _persisted_task(runtime, project_id, task)
    persisted_dispatch = _persisted_dispatch(dispatch_repo, project_id, dispatch)
    if (
        persisted_dispatch.route != _SESSION_ROUTE
        or persisted_dispatch.task_id != persisted_task.task_id
        or persisted_dispatch.task_fingerprint != persisted_task.fingerprint
    ):
        raise SessionRunnerValidationError("dispatch", _M_DISPATCH)
    configuration = _configuration(runtime, persisted_task)
    role_config = _role_config(configuration, persisted_task.role)
    text_bytes = _canonical_text(text, persisted_task.size_limit)
    canonical_versions = _canonical_versions(versions)
    _check_versions(
        canonical_versions, persisted_dispatch, role_config, persisted_task.prompt_version
    )

    existing = _get_intent(intents, persisted_dispatch.dispatch_id)
    if existing is not None:
        _verify_replay(
            existing,
            project_id=project_id,
            task=persisted_task,
            dispatch=persisted_dispatch,
            role_config=role_config,
            text_bytes=text_bytes,
            canonical_versions=canonical_versions,
        )
        if persisted_dispatch.state != "reserved":
            return _receipt(existing, persisted_dispatch, duplicate=True)
        return _finish_submit(
            store,
            registry=registry,
            activation_token=activation_token,
            project_id=project_id,
            runtime=runtime,
            dispatch_repo=dispatch_repo,
            manager=manager,
            task=persisted_task,
            intent=existing,
            spec=existing.spec(),
            client=client,
            duplicate=True,
        )

    if persisted_dispatch.state != "reserved":
        raise SessionRunnerStateError("dispatch", _M_DISPATCH_STATE)

    location = _preflight_manager(
        manager, project_id, persisted_task.role, configuration
    )
    active_client = client if client is not None else _build_default_client(manager, location)
    model_config = _model_config(role_config)
    if persisted_task.role == "section_critic":
        writer_config = _role_config(configuration, "writer")
        created = active_client.start_critic(
            agent=persisted_task.role,
            model=model_config,
            writer_model=_model_config(writer_config),
            directory=location.directory,
        )
    else:
        created = active_client.create_session(
            agent=persisted_task.role,
            model=model_config,
            directory=location.directory,
        )
    session_id = _session_id(created)
    try:
        active_client.verify_session_pins(
            session_id, agent=persisted_task.role, model=model_config
        )
    except (OpenCodeError, ValueError) as exc:
        raise SessionRunnerStateError("session", _M_SESSION) from exc
    directory = created.get("location", {}).get("directory") if isinstance(created, Mapping) else None
    if directory is not None and directory != location.directory:
        raise SessionRunnerStateError("session", _M_SESSION)

    with _execution_guard(registry, activation_token, project_id):
        mapping = runtime.record_session_mapping(
            service_instance_id=location.location_id,
            clean_location=location.directory,
            session_id=session_id,
            role=persisted_task.role,
            model=role_config.model,
            configuration=configuration,
            opencode_project_id=(
                created.get("projectID") if isinstance(created.get("projectID"), str) else None
            ),
        )
    try:
        spec = prepare_session_turn(
            store,
            task=persisted_task,
            dispatch=persisted_dispatch,
            mapping=mapping,
            manager=manager,
            text=text,
            versions=versions,
        )
    except Exception as exc:  # noqa: BLE001 - plan/spec refusal is fail closed
        raise SessionRunnerValidationError("spec", _M_BLOCKED) from exc

    with _execution_guard(registry, activation_token, project_id):
        intent = intents.record_intent(spec, fence=persisted_dispatch.current_fence)

    return _finish_submit(
        store,
        registry=registry,
        activation_token=activation_token,
        project_id=project_id,
        runtime=runtime,
        dispatch_repo=dispatch_repo,
        manager=manager,
        task=persisted_task,
        intent=intent,
        spec=spec,
        client=active_client,
        duplicate=False,
    )


def _finish_submit(
    store: object,
    *,
    registry: object,
    activation_token: str,
    project_id: str,
    runtime: RuntimeRepository,
    dispatch_repo: RuntimeDispatchRepository,
    manager: object,
    task: RuntimeTaskRecord,
    intent: SessionIntentRecord,
    spec: SessionTurnSpec,
    client: object | None,
    duplicate: bool,
) -> SubmitAdmission:
    """Revalidate and send one already-correlated turn; no waiting."""
    current_dispatch = dispatch_repo.get_dispatch(spec.dispatch_id)
    if current_dispatch is None:
        raise SessionRunnerStateError("dispatch", _M_DISPATCH)
    if current_dispatch.state != "reserved":
        return _receipt(intent, current_dispatch, duplicate=True)
    if current_dispatch.current_fence != intent.fence:
        raise SessionRunnerConflictError("dispatch", _M_DISPATCH_STATE)

    mapping = _persisted_mapping(runtime, project_id, intent.mapping_id)
    try:
        refreshed = prepare_session_turn(
            store,
            task=task,
            dispatch=current_dispatch,
            mapping=mapping,
            manager=manager,
            text=intent.message_mapping()["text"],
            versions=intent.spec().to_mapping()["versions"],
        )
    except Exception as exc:  # noqa: BLE001 - live refs/plan drift is a refusal
        raise SessionRunnerStateError("spec", _M_BLOCKED) from exc
    if refreshed.canonical != intent.canonical or refreshed.message != intent.message:
        raise SessionRunnerConflictError("spec", _M_REPLAY)
    assert_fixture_session_execution_allowed(manager, refreshed)
    active_client = client if client is not None else _build_default_client(
        manager, _preflight_manager(manager, project_id, task.role, _configuration(runtime, task))
    )
    role_config = _role_config(_configuration(runtime, task), task.role)
    try:
        active_client.verify_session_pins(
            intent.session_id, agent=task.role, model=_model_config(role_config)
        )
    except (OpenCodeError, ValueError) as exc:
        raise SessionRunnerStateError("session", _M_SESSION) from exc
    _empty_session(active_client, intent.session_id)

    with _execution_guard(registry, activation_token, project_id):
        dispatch_repo.mark_submitted(intent.dispatch_id)

    try:
        admission = active_client.admit(
            intent.session_id, intent.spec().message_text, intent.admission_id
        )
    except Exception:  # noqa: BLE001 - after the send claim every failure is ambiguous
        _mark_dispatch_unknown(
            registry, activation_token, project_id, dispatch_repo, intent.dispatch_id
        )
        return SubmitAdmission(
            dispatch_id=intent.dispatch_id,
            intent_id=intent.intent_id,
            spec_digest=intent.spec_digest,
            message_digest=intent.message_digest,
            admission_id=intent.admission_id,
            session_id=intent.session_id,
            state="unknown",
            duplicate=duplicate,
            reconciliation_required=True,
            reason=_M_UNKNOWN,
        )

    if not isinstance(admission, Admission) or not admission.has_durable_receipt:
        _mark_dispatch_unknown(
            registry, activation_token, project_id, dispatch_repo, intent.dispatch_id
        )
        return SubmitAdmission(
            dispatch_id=intent.dispatch_id,
            intent_id=intent.intent_id,
            spec_digest=intent.spec_digest,
            message_digest=intent.message_digest,
            admission_id=intent.admission_id,
            session_id=intent.session_id,
            state="unknown",
            duplicate=duplicate,
            reconciliation_required=True,
            reason=_M_UNKNOWN,
        )

    try:
        with _execution_guard(
            registry, activation_token, project_id, allow_draining=True
        ):
            admitted = dispatch_repo.mark_admitted(
                intent.dispatch_id, admission_id=intent.admission_id
            )
    except Exception:  # noqa: BLE001 - durable admission already exists; reconcile later
        return SubmitAdmission(
            dispatch_id=intent.dispatch_id,
            intent_id=intent.intent_id,
            spec_digest=intent.spec_digest,
            message_digest=intent.message_digest,
            admission_id=intent.admission_id,
            session_id=intent.session_id,
            state="submitted",
            duplicate=duplicate,
            reconciled=admission.reconciled,
            reconciliation_required=True,
            reason=_M_UNKNOWN,
        )
    return SubmitAdmission(
        dispatch_id=admitted.dispatch_id,
        intent_id=intent.intent_id,
        spec_digest=intent.spec_digest,
        message_digest=intent.message_digest,
        admission_id=intent.admission_id,
        session_id=intent.session_id,
        state=admitted.state,
        duplicate=duplicate or admission.duplicate,
        reconciled=admission.reconciled,
        reconciliation_required=False,
    )


def _get_intent(
    intents: SessionIntentRepository, dispatch_id: str
) -> SessionIntentRecord | None:
    try:
        return intents.get_intent(dispatch_id)
    except (SessionIntentError, ValueError) as exc:
        raise SessionRunnerValidationError("intent", _M_CORRUPT) from exc


def _persisted_mapping(
    runtime: RuntimeRepository, project_id: str, mapping_id: str
) -> RuntimeSessionMapping:
    try:
        mapping = runtime.get_session_mapping(mapping_id)
    except (RuntimeValidationError, ValueError) as exc:
        raise SessionRunnerValidationError("mapping", _M_CORRUPT) from exc
    if mapping is None or mapping.project_id != project_id:
        raise SessionRunnerValidationError("mapping", _M_CORRUPT)
    return mapping


def _mark_dispatch_unknown(
    registry: object,
    activation_token: str,
    project_id: str,
    dispatch_repo: RuntimeDispatchRepository,
    dispatch_id: str,
) -> None:
    try:
        with _execution_guard(
            registry, activation_token, project_id, allow_draining=True
        ):
            dispatch_repo.recover(dispatch_id)
    except Exception:  # noqa: BLE001 - best effort; the durable row stays ambiguous
        return


# --------------------------------------------------------------------------- #
# Reconcile
# --------------------------------------------------------------------------- #
def reconcile_session_turn(
    store: object,
    *,
    registry: object,
    activation_token: str,
    dispatch_id: str,
    manager: object,
    client: object | None = None,
) -> ReconcileOutcome:
    """Reconcile one persisted turn from durable server messages, statelessly.

    A completed accepted triple is returned without a new remote read or write.
    Unresolved or malformed terminals remain ``unknown`` with the dispatch slot
    held; only an authoritative idle success with a valid assistant window and a
    matching concrete model may persist a non-authorizing proposal and accept the
    dispatch's single local result.
    """
    project_id = _project_id(store)
    _verify_execution_token(registry, activation_token, project_id, allow_draining=True)
    try:
        dispatch_id = validate_identifier(dispatch_id, "dispatch_id")
    except ValueError as exc:
        raise SessionRunnerValidationError("dispatch", _M_DISPATCH) from exc
    runtime = RuntimeRepository(store)
    dispatch_repo = RuntimeDispatchRepository(store, runtime=runtime)
    intents = SessionIntentRepository(store, runtime=runtime, dispatch=dispatch_repo)

    intent = _get_intent(intents, dispatch_id)
    if intent is None:
        raise SessionRunnerStateError("intent", _M_INTENT)
    if intent.project_id != project_id or intent.dispatch_id != dispatch_id:
        raise SessionRunnerValidationError("intent", _M_INTENT_MISMATCH)
    dispatch = dispatch_repo.get_dispatch(dispatch_id)
    if dispatch is None:
        raise SessionRunnerStateError("dispatch", _M_DISPATCH)
    if (
        dispatch.task_id != intent.task_id
        or dispatch.task_fingerprint != intent.task_fingerprint
        or dispatch.route != _SESSION_ROUTE
    ):
        raise SessionRunnerValidationError("dispatch", _M_DISPATCH)

    if dispatch.state == "completed":
        return _existing_completed(runtime, dispatch, intent)
    if dispatch.state == "failed":
        return ReconcileOutcome(
            dispatch_id=dispatch_id,
            state="failed",
            reconciliation_required=False,
            duplicate=True,
            reason="the dispatch is already a recorded definitive failure.",
            cost_state=dispatch.cost_state,
            cost_amount_usd=dispatch.cost_amount_usd,
        )
    if dispatch.state == "reserved":
        return ReconcileOutcome(
            dispatch_id=dispatch_id,
            state="reserved",
            reconciliation_required=True,
            reason="no send claim was recorded; the turn was never submitted.",
            cost_state=dispatch.cost_state,
            cost_amount_usd=dispatch.cost_amount_usd,
        )

    try:
        spec = intent.spec()
    except Exception as exc:  # noqa: BLE001 - a corrupt intent is a static refusal
        raise SessionRunnerValidationError("intent", _M_CORRUPT) from exc
    configuration, task = _revalidate_intent(runtime, project_id, intent, spec)

    try:
        assert_fixture_session_execution_allowed(manager, spec)
    except ExecutionBoundaryBlocked as exc:
        _mark_dispatch_unknown(
            registry, activation_token, project_id, dispatch_repo, dispatch_id
        )
        return ReconcileOutcome(
            dispatch_id=dispatch_id,
            state="unknown",
            reconciliation_required=True,
            reason="the owned service/location or fixture boundary is unavailable.",
            cost_state=dispatch.cost_state,
            cost_amount_usd=dispatch.cost_amount_usd,
        )
    except Exception:  # noqa: BLE001 - any boundary failure is an explicit unknown
        _mark_dispatch_unknown(
            registry, activation_token, project_id, dispatch_repo, dispatch_id
        )
        return ReconcileOutcome(
            dispatch_id=dispatch_id,
            state="unknown",
            reconciliation_required=True,
            reason="the owned service/location or fixture boundary is unavailable.",
            cost_state=dispatch.cost_state,
            cost_amount_usd=dispatch.cost_amount_usd,
        )

    active_client = client if client is not None else _build_default_client(
        manager, _preflight_manager(manager, project_id, intent.role, configuration)
    )
    role_config = _role_config(configuration, intent.role)
    try:
        active_client.verify_session_pins(
            intent.session_id, agent=intent.role, model=_model_config(role_config)
        )
    except Exception:  # noqa: BLE001 - a missing/mismatched live session is unknown
        _mark_dispatch_unknown(
            registry, activation_token, project_id, dispatch_repo, dispatch_id
        )
        return ReconcileOutcome(
            dispatch_id=dispatch_id,
            state="unknown",
            reconciliation_required=True,
            reason="the owned session pins are unavailable or have changed.",
            cost_state=dispatch.cost_state,
            cost_amount_usd=dispatch.cost_amount_usd,
        )

    try:
        result = active_client.reconcile(intent.session_id, intent.admission_id)
    except Exception:  # noqa: BLE001 - an unreadable remote outcome is explicit unknown
        _mark_dispatch_unknown(
            registry, activation_token, project_id, dispatch_repo, dispatch_id
        )
        return ReconcileOutcome(
            dispatch_id=dispatch_id,
            state="unknown",
            reconciliation_required=True,
            reason="the durable remote outcome could not be read.",
            cost_state=dispatch.cost_state,
            cost_amount_usd=dispatch.cost_amount_usd,
        )
    if not isinstance(result, ReconcileResult):
        return ReconcileOutcome(
            dispatch_id=dispatch_id,
            state="unknown",
            reconciliation_required=True,
            reason="the durable remote outcome was malformed.",
            cost_state=dispatch.cost_state,
            cost_amount_usd=dispatch.cost_amount_usd,
        )

    _record_admission_if_submitted(
        registry, activation_token, project_id, dispatch_repo, intent, result
    )

    if result.is_complete:
        return _accept_completed(
            store,
            registry=registry,
            activation_token=activation_token,
            project_id=project_id,
            runtime=runtime,
            dispatch_repo=dispatch_repo,
            intent=intent,
            task=task,
            configuration=configuration,
            result=result,
            client=active_client,
        )
    if result.state in ("failed", "interrupted"):
        return _mark_authoritative_failure(
            store,
            registry=registry,
            activation_token=activation_token,
            project_id=project_id,
            dispatch_repo=dispatch_repo,
            intent=intent,
            result=result,
        )
    if result.state in ("queued", "in_progress"):
        return ReconcileOutcome(
            dispatch_id=dispatch_id,
            state=result.state,
            reconciliation_required=True,
            reason=result.reason,
            cost_state=dispatch.cost_state,
            cost_amount_usd=dispatch.cost_amount_usd,
        )
    # ``unknown`` and any unrecognized state are never success.
    _mark_dispatch_unknown(
        registry, activation_token, project_id, dispatch_repo, dispatch_id
    )
    return ReconcileOutcome(
        dispatch_id=dispatch_id,
        state="unknown",
        reconciliation_required=True,
        reason=result.reason or _M_UNKNOWN,
        cost_state=dispatch.cost_state,
        cost_amount_usd=dispatch.cost_amount_usd,
    )


def _revalidate_intent(
    runtime: RuntimeRepository,
    project_id: str,
    intent: SessionIntentRecord,
    spec: SessionTurnSpec,
) -> tuple[RoleConfiguration, RuntimeTaskRecord]:
    try:
        task = runtime.get_task(intent.task_id)
        snapshot = runtime.get_configuration(intent.config_snapshot_id)
        mapping = runtime.get_session_mapping(intent.mapping_id)
    except (RuntimeValidationError, ValueError) as exc:
        raise SessionRunnerValidationError("intent", _M_CORRUPT) from exc
    if task is None or task.project_id != project_id:
        raise SessionRunnerValidationError("intent", _M_CORRUPT)
    if (
        task.fingerprint != intent.task_fingerprint
        or task.role != intent.role
        or task.config_digest != intent.config_digest
        or task.config_snapshot_id != intent.config_snapshot_id
    ):
        raise SessionRunnerValidationError("intent", _M_CORRUPT)
    if snapshot is None or snapshot.config_digest != intent.config_digest:
        raise SessionRunnerValidationError("configuration", _M_CONFIG)
    configuration = snapshot.role_configuration()
    try:
        role_config = configuration.for_role(intent.role)
    except AgentRoleError as exc:
        raise SessionRunnerValidationError("configuration", _M_CONFIG) from exc
    if role_config.model.ref != intent.model or role_config.steps != spec.step_limit:
        raise SessionRunnerValidationError("configuration", _M_CONFIG)
    if (
        mapping is None
        or mapping.project_id != project_id
        or mapping.mapping_id != intent.mapping_id
        or mapping.session_id != intent.session_id
        or mapping.service_instance_id != intent.service_instance_id
        or mapping.clean_location != spec.clean_location
        or mapping.role != intent.role
        or mapping.model_ref != intent.model
        or mapping.config_digest != intent.config_digest
        or mapping.config_snapshot_id != intent.config_snapshot_id
    ):
        raise SessionRunnerValidationError("mapping", _M_CORRUPT)
    import json

    from .roles import BoundedTask

    try:
        bounded = BoundedTask.from_mapping(
            json.loads(task.canonical.decode("utf-8")), configuration=configuration
        )
    except Exception as exc:  # noqa: BLE001 - corrupt task is a static refusal
        raise SessionRunnerValidationError("task", _M_CORRUPT) from exc
    if (
        bounded.role_config != role_config
        or bounded.prompt_version != spec.prompt_version
        or bounded.step_limit != spec.step_limit
        or bounded.size_limit != spec.size_limit
        or bounded.fingerprint() != task.fingerprint
    ):
        raise SessionRunnerValidationError("configuration", _M_CONFIG)
    return configuration, task


def _existing_completed(
    runtime: RuntimeRepository, dispatch: DispatchRecord, intent: SessionIntentRecord
) -> ReconcileOutcome:
    if dispatch.result_kind != SESSION_RUNNER_RESULT_KIND or not dispatch.result_ref:
        raise SessionRunnerConflictError("dispatch", _M_COMPLETED)
    try:
        proposal = runtime.get_proposal(dispatch.result_ref)
    except (RuntimeValidationError, ValueError) as exc:
        raise SessionRunnerValidationError("proposal", _M_CORRUPT) from exc
    if (
        proposal is None
        or proposal.project_id != intent.project_id
        or proposal.task_id != intent.task_id
        or proposal.stored_digest != dispatch.result_digest
    ):
        raise SessionRunnerConflictError("dispatch", _M_COMPLETED)
    return ReconcileOutcome(
        dispatch_id=dispatch.dispatch_id,
        state="completed",
        reconciliation_required=False,
        duplicate=True,
        proposal_record_id=proposal.record_id,
        result_kind=dispatch.result_kind,
        result_ref=dispatch.result_ref,
        result_digest=dispatch.result_digest,
        cost_state=dispatch.cost_state,
        cost_amount_usd=dispatch.cost_amount_usd,
    )


def _record_admission_if_submitted(
    registry: object,
    activation_token: str,
    project_id: str,
    dispatch_repo: RuntimeDispatchRepository,
    intent: SessionIntentRecord,
    result: ReconcileResult,
) -> None:
    dispatch = dispatch_repo.get_dispatch(intent.dispatch_id)
    if dispatch is None or dispatch.state != "submitted":
        return
    if not result.is_durable:
        _mark_dispatch_unknown(
            registry, activation_token, project_id, dispatch_repo, intent.dispatch_id
        )
        return
    try:
        with _execution_guard(
            registry, activation_token, project_id, allow_draining=True
        ):
            dispatch_repo.mark_admitted(
                intent.dispatch_id, admission_id=intent.admission_id
            )
    except Exception:  # noqa: BLE001 - a later reconcile will retry the local transition
        return


def _accept_completed(
    store: object,
    *,
    registry: object,
    activation_token: str,
    project_id: str,
    runtime: RuntimeRepository,
    dispatch_repo: RuntimeDispatchRepository,
    intent: SessionIntentRecord,
    task: RuntimeTaskRecord,
    configuration: RoleConfiguration,
    result: ReconcileResult,
    client: object,
) -> ReconcileOutcome:
    """Accept only an authoritative idle success with a valid assistant window."""
    try:
        messages, truncated = client.collect_messages(
            intent.session_id, order="asc"
        )
    except Exception:  # noqa: BLE001 - an unreadable window is not success
        return _unknown_outcome(intent, result, "the assistant message window could not be read.")
    if truncated:
        return _unknown_outcome(intent, result, "the assistant message window was not fully read.")
    window = _correlated_window(messages, intent.admission_id)
    if window is None:
        return _unknown_outcome(intent, result, _M_WINDOW)
    assistant = [message for message in window if isinstance(message, dict) and message.get("type") == "assistant"]
    if not assistant:
        return _unknown_outcome(intent, result, _M_MALFORMED)
    final = assistant[-1]
    if _message_refused_or_errored(final):
        return _unknown_outcome(intent, result, "the assistant reported an error or refusal.")
    text = _assistant_text(final)
    if text is None:
        return _unknown_outcome(intent, result, _M_MALFORMED)
    actual_model = _actual_model(final)
    pinned_model = _role_config(configuration, intent.role).model
    if actual_model is None or not actual_model.same_model(pinned_model):
        return _unknown_outcome(intent, result, "the assistant message model does not match the pinned model.")
    agent = final.get("agent")
    if isinstance(agent, str) and agent != intent.role:
        return _unknown_outcome(intent, result, "the assistant message role does not match the pinned role.")
    usage = final.get("usage")
    if usage is None and "tokens" in final:
        usage = final.get("tokens")
    if usage is not None and not isinstance(usage, Mapping):
        return _unknown_outcome(intent, result, "the assistant usage metadata is malformed.")
    cost_amount = _actual_cost(usage, final)
    cost_state = "known" if cost_amount is not None else "unknown"
    try:
        safe_usage = _safe_usage(usage)
    except SessionRunnerValidationError:
        return _unknown_outcome(intent, result, "the assistant usage metadata is malformed.")
    payload = {
        "content": {
            "text": text,
            "actual_model": actual_model.ref,
            "actual_provider": actual_model.provider_id,
            "actual_model_id": actual_model.model_id,
            "actual_variant": actual_model.variant,
            "actual_usage": safe_usage,
            "actual_cost_usd": cost_amount,
        }
    }
    try:
        with _execution_guard(
            registry, activation_token, project_id, allow_draining=True
        ):
            with store.transaction():
                proposal = runtime._record_proposal_in_transaction(
                    task, payload, configuration=configuration
                )
                dispatch_repo._accept_result_in_transaction(
                    intent.dispatch_id,
                    fence=intent.fence,
                    result_kind=SESSION_RUNNER_RESULT_KIND,
                    result_ref=proposal.record_id,
                    result_digest=proposal.stored_digest,
                )
                _settle_actual_cost(
                    dispatch_repo,
                    intent.dispatch_id,
                    cost_state=cost_state,
                    amount_usd=cost_amount,
                )
                final_dispatch = dispatch_repo.get_dispatch(intent.dispatch_id)
    except ProposalError:
        return _unknown_outcome(
            intent, result, "the proposal content was not bounded, portable, or authority-free."
        )
    except DispatchConflictError as exc:
        # A divergent accepted result is a durable conflict, never a false success.
        raise SessionRunnerConflictError("dispatch", _M_COMPLETED) from exc
    except DispatchStateError as exc:
        return _unknown_outcome(
            intent, result, "the dispatch was no longer acceptable for a local result."
        )
    except (RuntimeConflictError, RuntimeValidationError) as exc:
        raise SessionRunnerValidationError("proposal", _M_CORRUPT) from exc
    if final_dispatch is None:
        raise SessionRunnerStateError("dispatch", _M_DISPATCH)
    return ReconcileOutcome(
        dispatch_id=final_dispatch.dispatch_id,
        state="completed",
        reconciliation_required=False,
        proposal_record_id=proposal.record_id,
        result_kind=final_dispatch.result_kind,
        result_ref=final_dispatch.result_ref,
        result_digest=final_dispatch.result_digest,
        cost_state=final_dispatch.cost_state,
        cost_amount_usd=final_dispatch.cost_amount_usd,
    )


def _mark_authoritative_failure(
    store: object,
    *,
    registry: object,
    activation_token: str,
    project_id: str,
    dispatch_repo: RuntimeDispatchRepository,
    intent: SessionIntentRecord,
    result: ReconcileResult,
) -> ReconcileOutcome:
    dispatch = dispatch_repo.get_dispatch(intent.dispatch_id)
    if dispatch is None:
        raise SessionRunnerStateError("dispatch", _M_DISPATCH)
    if dispatch.state in ("completed", "failed"):
        return ReconcileOutcome(
            dispatch_id=dispatch.dispatch_id,
            state=dispatch.state,
            reconciliation_required=False,
            duplicate=True,
            reason="the dispatch already has a terminal local disposition.",
            cost_state=dispatch.cost_state,
            cost_amount_usd=dispatch.cost_amount_usd,
        )
    if dispatch.state == "unknown":
        # An ambiguous dispatch cannot be reclassified as a definitive failure
        # without a validated pre-billing rejection; keep it unknown and held.
        return _unknown_outcome(
            intent, result, "an authoritative failure arrived on an ambiguous dispatch; it remains unknown."
        )
    try:
        with _execution_guard(
            registry, activation_token, project_id, allow_draining=True
        ):
            with store.transaction():
                dispatch_repo._mark_failed_in_transaction(intent.dispatch_id)
                _settle_actual_cost(
                    dispatch_repo, intent.dispatch_id, cost_state="unknown", amount_usd=None
                )
                final_dispatch = dispatch_repo.get_dispatch(intent.dispatch_id)
    except DispatchConflictError as exc:
        return _unknown_outcome(
            intent, result, "the failure settlement conflicted with durable state."
        )
    except DispatchStateError as exc:
        return _unknown_outcome(
            intent, result, "the failure could not be recorded on the current dispatch state."
        )
    if final_dispatch is None:
        raise SessionRunnerStateError("dispatch", _M_DISPATCH)
    return ReconcileOutcome(
        dispatch_id=final_dispatch.dispatch_id,
        state=result.state,
        reconciliation_required=False,
        cost_state=final_dispatch.cost_state,
        cost_amount_usd=final_dispatch.cost_amount_usd,
        reason=result.reason,
    )


def _settle_actual_cost(
    dispatch_repo: RuntimeDispatchRepository,
    dispatch_id: str,
    *,
    cost_state: str,
    amount_usd: str | None,
) -> None:
    """Apply a conservative cost settlement, preserving any stronger durable one."""
    try:
        dispatch_repo._settle_cost_in_transaction(
            dispatch_id, cost_state=cost_state, amount_usd=amount_usd
        )
    except (DispatchConflictError, DispatchValidationError):
        # The row already carries a stronger/established settlement or the reported
        # amount is not defensible. Keep the reserve and the stronger evidence.
        return


def _unknown_outcome(
    intent: SessionIntentRecord, result: ReconcileResult, reason: str
) -> ReconcileOutcome:
    return ReconcileOutcome(
        dispatch_id=intent.dispatch_id,
        state="unknown",
        reconciliation_required=True,
        reason=reason,
        cost_state=None,
        cost_amount_usd=None,
    )


def _correlated_window(
    messages: object, admission_id: str
) -> list[dict[str, Any]] | None:
    if not isinstance(messages, list):
        return None
    start = None
    for index, message in enumerate(messages):
        if (
            isinstance(message, dict)
            and message.get("type") == "user"
            and message.get("id") == admission_id
        ):
            start = index
            break
    if start is None:
        return None
    end = len(messages)
    for index in range(start + 1, len(messages)):
        message = messages[index]
        if isinstance(message, dict) and message.get("type") in TURN_OPENING_TYPES:
            end = index
            break
    window = messages[start + 1 : end]
    return [message for message in window if isinstance(message, dict)]


def _message_refused_or_errored(message: Mapping[str, Any]) -> bool:
    if message.get("error") is not None:
        return True
    if message.get("refusal") is True:
        return True
    finish = message.get("finish")
    return isinstance(finish, str) and finish in ("error", "refusal")


def _assistant_text(message: Mapping[str, Any]) -> str | None:
    content = message.get("content")
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        parts = [
            part.get("text")
            for part in content
            if isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str)
        ]
        text = "\n".join(parts)
    else:
        text = message.get("text")
    if not isinstance(text, str) or not text.strip():
        return None
    return text


def _actual_model(message: Mapping[str, Any]) -> ModelRef | None:
    raw = message.get("model")
    if isinstance(raw, Mapping):
        provider = raw.get("providerID")
        model_id = raw.get("id")
        variant = raw.get("variant")
    elif isinstance(raw, str):
        try:
            return ModelRef.from_mapping(raw)
        except Exception:  # noqa: BLE001 - malformed actual model is not evidence
            return None
    else:
        provider = message.get("providerID")
        model_id = message.get("modelID")
        variant = message.get("variant")
    if not isinstance(provider, str) or not isinstance(model_id, str):
        return None
    if variant is not None and not isinstance(variant, str):
        return None
    try:
        return ModelRef(provider_id=provider, model_id=model_id, variant=variant)
    except Exception:  # noqa: BLE001 - malformed actual model is not evidence
        return None


def _actual_cost(usage: object, final: Mapping[str, Any]) -> str | None:
    candidates: list[object] = []
    if isinstance(usage, Mapping) and "cost" in usage:
        candidates.append(usage.get("cost"))
    if "cost" in final:
        candidates.append(final.get("cost"))
    for value in candidates:
        if isinstance(value, bool) or value is None:
            continue
        try:
            amount = Decimal(str(value))
        except (InvalidOperation, ValueError):
            continue
        if not amount.is_finite() or amount < 0:
            continue
        text = format(amount, "f")
        if len(text) > 64:
            continue
        return text
    return None


#: Usage keys that would trip the non-authorizing proposal content key scan are
#: preserved under explicit "reported_*" names instead of being dropped.
_USAGE_KEY_ALIASES = {
    "cost": "reported_cost",
    "costusd": "reported_cost_usd",
    "spend": "reported_spend",
    "budget": "reported_budget",
    "reserve": "reported_reserve",
    "reserveusd": "reported_reserve_usd",
    "model": "reported_model",
    "provider": "reported_provider",
    "providerid": "reported_provider_id",
    "modelid": "reported_model_id",
    "variant": "reported_variant",
}


def _safe_usage(value: object) -> object:
    """Return a JSON-safe copy of actual usage metadata without authority keys."""
    if value is None:
        return None
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise SessionRunnerValidationError("usage", _M_MALFORMED)
            normalized = key.strip().lower().replace("-", "").replace("_", "")
            alias = _USAGE_KEY_ALIASES.get(normalized, key)
            out[alias] = _safe_usage(item)
        return out
    if isinstance(value, list):
        return [_safe_usage(item) for item in value]
    return value
