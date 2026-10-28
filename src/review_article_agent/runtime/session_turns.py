"""Pure, read-only preparation of an exact, immutable session-turn specification.

This module is the Step 04 (task T7a-plan) **planning** half of the durable
session-turn runner. It reads an already-verified
:class:`~review_article_agent.storage.store.ProjectStore` and prepares **one
immutable, deterministic** :class:`SessionTurnSpec` for a *already reserved*
``opencode_session`` dispatch, from the **persisted** records — it never writes,
never opens a network connection, never reads a credential, and never creates a
session.

Authoritative sources (read-only):

* ``docs/implementation/04-agent-runtime.md`` deliverables 1/2 and acceptance
  criteria 1 (an agent cannot approve its own artifact, edit authoritative state,
  widen scope, or invoke unapproved tools), 3 (a *different concrete* model
  section critic in a *fresh* context; invalid configuration fails rather than
  silently reusing the writer model), 4 (lost event streams reconcile from durable
  state/messages), and 7 (project/session/model isolation).
* ``docs/contracts/02-gates-and-runtime.md`` §6.1 (``VersionSet``: no fabricated
  version; a consumed slot MUST be ``known``), §8 (runtime safety: admission is
  not completion; a dispatch receipt grants no authority; fail closed on unknown).
* ``docs/decisions/001-runtime-language.md`` (Python core; app-owned OpenCode V2
  service; only an authoritative terminal completes a turn; explicit endpoint and
  credential, fail closed; a writer-history fork is not a clean critic; direct
  calls are accounted separately).

What this module deliberately does **not** do
---------------------------------------------

* It is **not** the runner. It performs **no** transport, admits **no** input,
  marks no dispatch submitted/admitted, and creates **no** session.
* It grants **no** admission, **no** approval, and **no** execution permission.
  The returned :class:`SessionTurnSpec` is **inert planning metadata**: it does not
  *prove* it was persisted, and it does not authorize paid model generation or a
  hosted/source upload. Authorization, budgets, and rights remain separate
  concerns; this module reads no credential and makes no network call.
* It does **not** modify the schema, dispatch ledger, CLI, worker, or any storage
  module; it only reads through the existing repositories.

What it verifies (all fail closed)
----------------------------------

The caller hands in ``task``/``dispatch``/``mapping`` value objects and a
``manager``. **Nothing is trusted from those claims**: each is re-read from the
store through :class:`~review_article_agent.storage.agent_runtime.RuntimeRepository`
and :class:`~review_article_agent.runtime.dispatch.RuntimeDispatchRepository` and
required to equal the persisted row. The plan is then built only from persisted
rows plus the application-supplied ``text``:

* the dispatch is ``opencode_session`` **and still ``reserved``** and names this
  exact task id + fingerprint;
* the session mapping belongs to the same project, its immutable
  configuration snapshot exists, and its role and **full model reference
  (including an explicit resolved ``variant``)** match the task's role
  configuration — an omitted variant is refused, never defaulted;
* the task is rebuilt from its stored canonical bytes under its own stored
  configuration, its target and every live input are revalidated for existence,
  byte-level content, pinned digest, currentness, and current storage-allowed
  rights (reusing the :class:`RuntimeRepository` helper and rebuild conventions,
  with **no** write);
* the manager's owned, current descriptor/connection and its clean per-project
  location are locally cross-checked (the ownership/currentness proof is the
  manager's own ``descriptor()``/``connection()``; this module only checks local
  consistency and performs no network probe), the mapping's ``clean_location`` must
  equal the owned location's directory and its ``service_instance_id`` must equal
  that owned location's instance id, and the manager's configuration for the role
  must equal the task's role configuration;
* the **effective** generated role ``steps`` for the role must equal the task's
  ``step_limit`` exactly (a smaller requested limit is refused, because the task's
  step field is not assumed to affect the server);
* the ``versions`` set is a validated ``VersionSet`` whose ``route`` is
  ``opencode_session``, whose ``input_digest`` equals the dispatch's
  ``input_digest``, whose canonical hash equals the dispatch's ``version_digest``,
  whose ``model``/``prompt`` slots are ``known``, and whose model/prompt versions
  equal the recorded session model and the task prompt version;
* the application ``text`` is canonical, portable, bounded JSON and the serialized
  session-turn input is not larger than the task ``size_limit``.

A fresh section critic additionally marks ``fresh_context_required`` and refuses a
critic that is not a *different concrete* model from the writer (a different
``variant`` is not a different model). The plan **cannot claim the critic history
is empty** — that requires an API proof this pure module does not have; the plan
records ``context_history = "unverified"`` and leaves session creation to the
later ``start_critic`` step.

The spec is deterministic: the same persisted rows and inputs always produce the
same canonical bytes, digest, and client ``admission_id`` (derived from the same
project + dispatch id, matching the OpenCode V2 ``Session.Message.ID`` shape), so
a later runner can persist the correlation **before** transport and reconstruct it.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from typing import Any

from ..domain.errors import ReviewAgentError
from ..domain.records import (
    RecordValidationError,
    canonical_json,
    validate_identifier,
)
from ..storage.agent_runtime import (
    PureJsonVersionSet,
    RuntimeCorruptionError,
    RuntimeRepository,
    RuntimeSessionMapping,
    RuntimeTaskRecord,
    RuntimeValidationError,
    validate_version_set,
)
from .dispatch import DispatchRecord, RuntimeDispatchRepository
from .opencode import MESSAGE_ID_RE, MAX_ID_LENGTH, assert_explicit_loopback_url
from .roles import (
    AgentRoleError,
    BoundedTask,
    RoleConfiguration,
    build_opencode_config,
)
from .service import ProjectLocation, ServiceConnection, ServiceDescriptor

__all__ = [
    "SESSION_TURN_PLAN_PERFORMS_NETWORK_IO",
    "SESSION_TURN_PLAN_READS_CREDENTIAL",
    "SESSION_TURN_SPEC_GRANTS_ADMISSION",
    "SESSION_TURN_SPEC_GRANTS_APPROVAL",
    "SESSION_TURN_SPEC_IS_PERSISTED",
    "SESSION_TURN_SPEC_KIND",
    "SESSION_TURN_SPEC_VERSION",
    "SessionTurnError",
    "SessionTurnSpec",
    "SessionTurnValidationError",
    "prepare_session_turn",
]

#: The plan's canonical wire kind and version (part of the digest, never inferred).
SESSION_TURN_SPEC_KIND = "session_turn_spec"
SESSION_TURN_SPEC_VERSION = 1

#: The plan is inert metadata: it is not persisted by this call (a later runner
#: persists the correlation), it grants no admission or approval, it performs no
#: network I/O, and it never reads a credential. These are stated as data so a
#: later consumer cannot mistake planning for permission.
SESSION_TURN_SPEC_IS_PERSISTED = False
SESSION_TURN_SPEC_GRANTS_ADMISSION = False
SESSION_TURN_SPEC_GRANTS_APPROVAL = False
SESSION_TURN_PLAN_PERFORMS_NETWORK_IO = False
SESSION_TURN_PLAN_READS_CREDENTIAL = False

#: The version slots a session turn consumes (C02 §6.1): the role model and the
#: prompt. Both MUST be ``known``; an unknown applied version is refused.
_DEFAULT_CONSUMED: tuple[str, ...] = ("model", "prompt")

#: The per-project owned-location directory name under the service root (mirrors
#: ``service``'s ``projects`` directory; the exact directory still comes from the
#: manager, this only cross-checks the layout).
_LOCATION_PARENT_NAME = "projects"

#: The fixed, non-authorizing execution boundary recorded in every plan.
_EXECUTION_BOUNDARY: dict[str, Any] = {
    "kind": "inert_metadata_planning_only",
    "correlation_persisted": False,
    "grants_admission": False,
    "grants_approval": False,
    "authorizes_paid_generation": False,
    "authorizes_source_upload": False,
    "requires_later_runner": True,
}

#: The only accepted session-turn route (an admission receipt exists only here).
_SESSION_ROUTE = "opencode_session"

#: Static, value-free error messages: an error raised from untrusted input never
#: echoes an id, digest, path, model, role, key, or value.
_MSG_STORE = "store: must be an opened project store with a valid project id."
_MSG_TASK_TYPE = "task: must be a persisted RuntimeTaskRecord."
_MSG_TASK_MISSING = "task: is not persisted in this project."
_MSG_TASK_MISMATCH = "task: the supplied record does not match the persisted task."
_MSG_MAPPING_TYPE = "mapping: must be a persisted RuntimeSessionMapping."
_MSG_MAPPING_MISSING = "mapping: is not persisted in this project."
_MSG_MAPPING_MISMATCH = "mapping: the supplied record does not match the persisted mapping."
_MSG_DISPATCH_TYPE = "dispatch: must be a persisted DispatchRecord."
_MSG_DISPATCH_MISSING = "dispatch: is not persisted in this project."
_MSG_DISPATCH_MISMATCH = "dispatch: the supplied record does not match the persisted dispatch."
_MSG_DISPATCH_ROUTE = "dispatch: only the opencode_session route may plan a session turn."
_MSG_DISPATCH_STATE = (
    "dispatch: must still be reserved before a session-turn plan is prepared."
)
_MSG_DISPATCH_TASK = "dispatch: must name exactly this task id and fingerprint."
_MSG_ROLE_MISMATCH = "mapping: the session role must equal the task role."
_MSG_CONFIG_MISSING = "configuration: a required immutable configuration snapshot is missing."
_MSG_CONFIG_ROLE = (
    "configuration: the role configuration does not match the task role configuration."
)
_MSG_VARIANT = (
    "model: an explicit variant is required; an omitted variant is refused rather "
    "than silently defaulted."
)
_MSG_MANAGER = "manager: must expose the owned descriptor, connection, and per-project location."
_MSG_LOCATION = "manager: the per-project location does not match the recorded session location."
_MSG_SERVICE_INSTANCE = (
    "manager: the owned service instance does not match the recorded session mapping."
)
_MSG_DESCRIPTOR = "manager: the descriptor and connection are not internally consistent."
_MSG_LOOPBACK = "manager: the descriptor endpoint is not an explicit loopback origin."
_MSG_STEPS = "step_limit: the effective generated role steps must equal the task step limit."
_MSG_MODEL_PIN = (
    "model: the effective generated role model does not match the recorded session model."
)
_MSG_LIVE_REFS = (
    "task: the target or an input is missing, stale, or its bytes, digest, or rights "
    "are not valid now."
)
_MSG_CANONICAL = "text: must be canonical, portable, bounded JSON."
_MSG_TEXT_SIZE = "text: is larger than the task size limit."
_MSG_MESSAGE_SIZE = (
    "message: the serialized session-turn input is larger than the task size limit."
)
_MSG_VERSIONS = "versions: must be a validated VersionSet."
_MSG_VERSIONS_ROUTE = "versions: route must be opencode_session."
_MSG_VERSIONS_INPUT = "versions: input_digest must equal the dispatch input digest."
_MSG_VERSIONS_HASH = "versions: canonical version hash must equal the dispatch version digest."
_MSG_VERSIONS_SLOTS = "versions: the model and prompt slots must be known."
_MSG_VERSIONS_MODEL = "versions: the model version must equal the recorded session model."
_MSG_VERSIONS_PROMPT = "versions: the prompt version must equal the task prompt version."
_MSG_CRITIC = (
    "section_critic: the configured critic must be a different concrete model than "
    "the writer; a different variant is not a different model."
)
_MSG_CRITIC_SESSION = "section_critic: the critic session must be fresh and must not reuse a writer session."
_MSG_REPO_HELPER = "task: the runtime repository cannot revalidate the live references."
_MSG_ADMISSION = "admission: the derived admission id is not an OpenCode-compatible message id."
_MSG_CORRUPT = "store: a persisted runtime record failed integrity revalidation."


class SessionTurnError(ReviewAgentError):
    """A session-turn plan could not be prepared (fail closed).

    ``code`` is a static, value-free identifier and the message is a constant, so
    an error raised from untrusted input never echoes an id, digest, path, model,
    role, key, or value.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class SessionTurnValidationError(SessionTurnError, ValueError):
    """A session-turn input was missing, foreign, stale, or inconsistent."""


# --------------------------------------------------------------------------- #
# The immutable spec
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class SessionTurnSpec:
    """An exact, immutable, deterministic plan for one ``opencode_session`` turn.

    The value is self-checking: ``__post_init__`` re-parses the canonical bytes and
    requires the recorded admission id, message digest, session/mapping ids, and
    project id to agree, so a hand-constructed or tampered instance is refused.
    Every field is an immutable scalar or ``bytes``, so the instance is deeply
    immutable; both ``canonical`` and ``message`` are immutable ``bytes`` and
    :meth:`to_mapping` returns a fresh plain copy (never an alias), so no caller can
    mutate a plan after it has been digested.
    """

    project_id: str
    role: str
    task_id: str
    task_fingerprint: str
    dispatch_id: str
    dispatch_request_id: str
    mapping_id: str
    session_id: str
    service_instance_id: str
    clean_location: str
    config_snapshot_id: str
    config_digest: str
    model: str
    admission_id: str
    input_digest: str
    version_digest: str
    prompt_version: str
    step_limit: int
    size_limit: int
    fresh_context_required: bool
    message_digest: str
    canonical: bytes
    message: bytes

    def __post_init__(self) -> None:
        if not isinstance(self.canonical, bytes) or not isinstance(self.message, bytes):
            raise SessionTurnValidationError("spec", _MSG_CORRUPT)
        if hashlib.sha256(self.message).hexdigest() != self.message_digest:
            raise SessionTurnValidationError("spec", _MSG_CORRUPT)
        if MESSAGE_ID_RE.fullmatch(self.admission_id) is None:
            raise SessionTurnValidationError("spec", _MSG_ADMISSION)
        try:
            plan = json.loads(self.canonical.decode("utf-8"))
        except (ValueError, UnicodeError) as exc:
            raise SessionTurnValidationError("spec", _MSG_CORRUPT) from exc
        if not isinstance(plan, dict) or plan.get("kind") != SESSION_TURN_SPEC_KIND:
            raise SessionTurnValidationError("spec", _MSG_CORRUPT)
        session = plan.get("session")
        if not isinstance(session, dict):
            raise SessionTurnValidationError("spec", _MSG_CORRUPT)
        if (
            plan.get("project_id") != self.project_id
            or plan.get("admission_id") != self.admission_id
            or plan.get("message_digest") != self.message_digest
            or session.get("session_id") != self.session_id
            or session.get("mapping_id") != self.mapping_id
        ):
            raise SessionTurnValidationError("spec", _MSG_CORRUPT)

    @property
    def digest(self) -> str:
        """The canonical SHA-256 of the plan bytes (reconstructible before transport)."""
        return hashlib.sha256(self.canonical).hexdigest()

    @property
    def message_text(self) -> str:
        """The exact application text a later runner admits (never model-authored)."""
        return self.message.decode("utf-8")

    def to_mapping(self) -> dict[str, Any]:
        """A fresh, plain JSON-safe copy of the plan (never an alias)."""
        return json.loads(self.canonical.decode("utf-8"))


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def _fail(code: str, message: str) -> "SessionTurnValidationError":
    return SessionTurnValidationError(code, message)


def _require(condition: bool, code: str, message: str) -> None:
    if not condition:
        raise SessionTurnValidationError(code, message)


def _canonical(value: object, message: str) -> bytes:
    """Canonical, portable, bounded JSON bytes, or a static redacted error."""
    try:
        return canonical_json(value)
    except (RecordValidationError, ValueError, TypeError, RecursionError, UnicodeError) as exc:
        raise SessionTurnValidationError("canonical", message) from exc


def _store_project_id(store: object) -> str:
    project_id = getattr(store, "project_id", None)
    if not isinstance(project_id, str):
        raise _fail("store", _MSG_STORE)
    try:
        return validate_identifier(project_id, "project_id")
    except ValueError as exc:
        raise _fail("store", _MSG_STORE) from exc


def _admission_id(project_id: str, dispatch_id: str) -> str:
    """Deterministically derive the client admission id from project + dispatch.

    The result matches the OpenCode V2 ``Session.Message.ID`` shape (``msg_`` + a
    URL-safe opaque suffix) and is bounded, so it can be reconstructed byte-for-byte
    before transport and used as the idempotency key without inventing a value.
    """
    digest = hashlib.sha256((project_id + "\x00" + dispatch_id).encode("utf-8")).hexdigest()
    admission_id = "msg_" + digest[:32]
    if MESSAGE_ID_RE.fullmatch(admission_id) is None or len(admission_id) > MAX_ID_LENGTH:
        raise _fail("admission", _MSG_ADMISSION)
    return admission_id


# --------------------------------------------------------------------------- #
# Persisted-parent verification (nothing is trusted from the caller's claims)
# --------------------------------------------------------------------------- #
def _require_persisted_task(
    runtime: RuntimeRepository, project_id: str, task: object
) -> RuntimeTaskRecord:
    if not isinstance(task, RuntimeTaskRecord):
        raise _fail("task", _MSG_TASK_TYPE)
    try:
        persisted = runtime.get_task(task.task_id)
    except (RuntimeCorruptionError, RuntimeValidationError, ValueError) as exc:
        raise SessionTurnValidationError("task", _MSG_CORRUPT) from exc
    if persisted is None or persisted.project_id != project_id:
        raise _fail("task", _MSG_TASK_MISSING)
    if persisted.to_mapping() != task.to_mapping():
        raise _fail("task", _MSG_TASK_MISMATCH)
    return persisted


def _require_persisted_mapping(
    runtime: RuntimeRepository, project_id: str, mapping: object
) -> RuntimeSessionMapping:
    if not isinstance(mapping, RuntimeSessionMapping):
        raise _fail("mapping", _MSG_MAPPING_TYPE)
    try:
        persisted = runtime.get_session_mapping(mapping.mapping_id)
    except (RuntimeCorruptionError, RuntimeValidationError, ValueError) as exc:
        raise SessionTurnValidationError("mapping", _MSG_CORRUPT) from exc
    if persisted is None or persisted.project_id != project_id:
        raise _fail("mapping", _MSG_MAPPING_MISSING)
    if persisted.to_mapping() != mapping.to_mapping():
        raise _fail("mapping", _MSG_MAPPING_MISMATCH)
    return persisted


def _require_persisted_dispatch(
    dispatch_repo: RuntimeDispatchRepository, project_id: str, dispatch: object
) -> DispatchRecord:
    if not isinstance(dispatch, DispatchRecord):
        raise _fail("dispatch", _MSG_DISPATCH_TYPE)
    try:
        persisted = dispatch_repo.get_dispatch(dispatch.dispatch_id)
    except (RuntimeCorruptionError, ValueError) as exc:
        raise SessionTurnValidationError("dispatch", _MSG_CORRUPT) from exc
    if persisted is None or persisted.project_id != project_id:
        raise _fail("dispatch", _MSG_DISPATCH_MISSING)
    if persisted.to_mapping() != dispatch.to_mapping():
        raise _fail("dispatch", _MSG_DISPATCH_MISMATCH)
    return persisted


def _get_configuration(runtime: RuntimeRepository, snapshot_id: str):
    try:
        snapshot = runtime.get_configuration(snapshot_id)
    except (RuntimeCorruptionError, RuntimeValidationError, ValueError) as exc:
        raise SessionTurnValidationError("configuration", _MSG_CORRUPT) from exc
    if snapshot is None:
        raise _fail("configuration", _MSG_CONFIG_MISSING)
    return snapshot


def _rebuild_bounded_task(task: RuntimeTaskRecord, configuration: RoleConfiguration) -> BoundedTask:
    """Rebuild the exact bounded task from its stored bytes under its stored config."""
    try:
        stored = json.loads(task.canonical.decode("utf-8"))
        bounded = BoundedTask.from_mapping(stored, configuration=configuration)
    except (ValueError, UnicodeError, RecordValidationError, AgentRoleError) as exc:
        raise SessionTurnValidationError("task", _MSG_CORRUPT) from exc
    if bounded.canonical_bytes() != task.canonical or bounded.fingerprint() != task.fingerprint:
        raise _fail("task", _MSG_TASK_MISMATCH)
    return bounded


def _revalidate_live_refs(runtime: RuntimeRepository, bounded: BoundedTask) -> None:
    """Revalidate the target/inputs now (bytes, digest, rights, currentness), no write.

    Reuses the repository's existing rebuild/verification convention (the same
    helper ``record_task`` uses for a **new** task) so the live content, pinned
    digests, pinned rights, and current revision of the target and every input are
    proven before a plan is produced. Nothing is written.
    """
    verify = getattr(runtime, "_verify_new_task_refs", None)
    if not callable(verify):
        raise _fail("task", _MSG_REPO_HELPER)
    try:
        verify(bounded)
    except RuntimeCorruptionError as exc:
        raise SessionTurnValidationError("task", _MSG_CORRUPT) from exc
    except (ValueError, AgentRoleError) as exc:
        # A missing/stale/content-mismatched/superseded-rights reference is a
        # ValueError (or a typed role error); surface it as a static redacted refusal.
        raise SessionTurnValidationError("task", _MSG_LIVE_REFS) from exc


# --------------------------------------------------------------------------- #
# Manager surface (local validation only; the ownership proof is the manager's)
# --------------------------------------------------------------------------- #
def _manager_surface(
    manager: object, project_id: str
) -> tuple[ServiceDescriptor, ServiceConnection, ProjectLocation, RoleConfiguration]:
    for name in ("descriptor", "connection", "location_for"):
        if not callable(getattr(manager, name, None)):
            raise _fail("manager", _MSG_MANAGER)
    configuration = getattr(manager, "configuration", None)
    if not isinstance(configuration, RoleConfiguration):
        raise _fail("manager", _MSG_MANAGER)
    try:
        descriptor = manager.descriptor()  # type: ignore[attr-defined]
        connection = manager.connection()  # type: ignore[attr-defined]
        location = manager.location_for(project_id)  # type: ignore[attr-defined]
    except ReviewAgentError as exc:
        # The manager's own ownership/currentness refusal is already typed and
        # static; surface it unchanged rather than masking it.
        raise
    except Exception as exc:  # noqa: BLE001 - any failure is a fail-closed refusal
        raise _fail("manager", _MSG_MANAGER) from exc
    if (
        not isinstance(descriptor, ServiceDescriptor)
        or not isinstance(connection, ServiceConnection)
        or not isinstance(location, ProjectLocation)
    ):
        raise _fail("manager", _MSG_MANAGER)
    return descriptor, connection, location, configuration


def _validate_manager_binding(
    descriptor: ServiceDescriptor,
    connection: ServiceConnection,
    location: ProjectLocation,
    mapping: RuntimeSessionMapping,
    project_id: str,
) -> None:
    try:
        assert_explicit_loopback_url(descriptor.url)
    except ValueError as exc:
        raise _fail("manager", _MSG_LOOPBACK) from exc
    _require(location.app_project_id == project_id, "manager", _MSG_LOCATION)
    _require(location.directory == mapping.clean_location, "manager", _MSG_LOCATION)
    _require(mapping.service_instance_id == location.location_id, "manager", _MSG_SERVICE_INSTANCE)
    _require(location.root == descriptor.root, "manager", _MSG_DESCRIPTOR)
    _require(
        os.path.dirname(location.directory)
        == os.path.join(descriptor.root, _LOCATION_PARENT_NAME),
        "manager",
        _MSG_LOCATION,
    )
    _require(
        connection.base_url == descriptor.url
        and connection.password_file == descriptor.password_file,
        "manager",
        _MSG_DESCRIPTOR,
    )


def _critic_guard(
    runtime: RuntimeRepository,
    mapping: RuntimeSessionMapping,
    mapping_configuration: RoleConfiguration,
) -> None:
    """A fresh section critic must be a different concrete model in a fresh session."""
    try:
        mapping_configuration.validate_section_critic()
    except AgentRoleError as exc:
        raise _fail("section_critic", _MSG_CRITIC) from exc
    try:
        mappings = runtime.list_session_mappings()
    except (RuntimeCorruptionError, RuntimeValidationError) as exc:
        raise SessionTurnValidationError("section_critic", _MSG_CORRUPT) from exc
    for other in mappings:
        if other.role == "writer" and other.session_id == mapping.session_id:
            raise _fail("section_critic", _MSG_CRITIC_SESSION)


# --------------------------------------------------------------------------- #
# Versions + message
# --------------------------------------------------------------------------- #
def _canonical_versions(versions: object, consumed: tuple[str, ...]) -> dict[str, Any]:
    raw = versions.as_mapping() if isinstance(versions, PureJsonVersionSet) else versions
    try:
        return validate_version_set(raw, consumed=consumed)
    except (RecordValidationError, ValueError, TypeError) as exc:
        raise SessionTurnValidationError("versions", _MSG_VERSIONS) from exc


def _check_versions(
    canonical_versions: dict[str, Any],
    dispatch: DispatchRecord,
    mapping: RuntimeSessionMapping,
    prompt_version: str,
) -> None:
    _require(canonical_versions["route"] == _SESSION_ROUTE, "versions", _MSG_VERSIONS_ROUTE)
    _require(
        canonical_versions["input_digest"] == dispatch.input_digest,
        "versions",
        _MSG_VERSIONS_INPUT,
    )
    _require(
        hashlib.sha256(canonical_json(canonical_versions)).hexdigest()
        == dispatch.version_digest,
        "versions",
        _MSG_VERSIONS_HASH,
    )
    model_slot = canonical_versions["model"]
    prompt_slot = canonical_versions["prompt"]
    _require(
        model_slot.get("state") == "known" and prompt_slot.get("state") == "known",
        "versions",
        _MSG_VERSIONS_SLOTS,
    )
    _require(
        model_slot.get("ref", {}).get("version") == mapping.model_ref,
        "versions",
        _MSG_VERSIONS_MODEL,
    )
    _require(
        prompt_slot.get("ref", {}).get("version") == prompt_version,
        "versions",
        _MSG_VERSIONS_PROMPT,
    )


def _build_message(
    project_id: str,
    bounded: BoundedTask,
    task: RuntimeTaskRecord,
    dispatch: DispatchRecord,
    mapping: RuntimeSessionMapping,
    canonical_versions: dict[str, Any],
    text: object,
) -> bytes:
    message_doc = {
        "kind": "session_turn_input",
        "project_id": project_id,
        "role": bounded.role,
        "task_id": task.task_id,
        "task_fingerprint": task.fingerprint,
        "dispatch_id": dispatch.dispatch_id,
        "mapping_id": mapping.mapping_id,
        "session_id": mapping.session_id,
        "scope": list(bounded.scope),
        "target": dict(task.target),
        "input_refs": [dict(entry) for entry in task.input_refs],
        "prompt_version": bounded.prompt_version,
        "size_limit": bounded.size_limit,
        "step_limit": bounded.step_limit,
        "versions": canonical_versions,
        "text": text,
    }
    return _canonical(message_doc, _MSG_CANONICAL)


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #
def prepare_session_turn(
    store: object,
    *,
    task: RuntimeTaskRecord,
    dispatch: DispatchRecord,
    mapping: RuntimeSessionMapping,
    manager: object,
    text: object,
    versions: object,
    consumed: tuple[str, ...] = _DEFAULT_CONSUMED,
) -> SessionTurnSpec:
    """Prepare one immutable, deterministic session-turn plan (read-only, fail closed).

    See the module docstring for the full verification list. The function makes no
    network call, reads no credential, writes nothing, admits nothing, and creates
    no session; it returns inert, self-checking planning metadata whose bytes,
    digest, and client admission id are reconstructible before transport.
    """
    project_id = _store_project_id(store)
    runtime = RuntimeRepository(store)
    dispatch_repo = RuntimeDispatchRepository(store, runtime=runtime)

    persisted_task = _require_persisted_task(runtime, project_id, task)
    persisted_mapping = _require_persisted_mapping(runtime, project_id, mapping)
    persisted_dispatch = _require_persisted_dispatch(dispatch_repo, project_id, dispatch)

    # Dispatch: the durable opencode_session route, still reserved, exact task.
    _require(persisted_dispatch.route == _SESSION_ROUTE, "dispatch", _MSG_DISPATCH_ROUTE)
    _require(persisted_dispatch.state == "reserved", "dispatch", _MSG_DISPATCH_STATE)
    _require(
        persisted_dispatch.task_id == persisted_task.task_id
        and persisted_dispatch.task_fingerprint == persisted_task.fingerprint,
        "dispatch",
        _MSG_DISPATCH_TASK,
    )

    role = persisted_task.role
    _require(persisted_mapping.role == role, "mapping", _MSG_ROLE_MISMATCH)

    # The task's own persisted configuration snapshot + canonical rebuild.
    task_snapshot = _get_configuration(runtime, persisted_task.config_snapshot_id)
    _require(
        task_snapshot.config_digest == persisted_task.config_digest,
        "configuration",
        _MSG_CONFIG_MISSING,
    )
    task_configuration = task_snapshot.role_configuration()
    bounded = _rebuild_bounded_task(persisted_task, task_configuration)

    # The mapping's persisted configuration snapshot must agree for this role.
    mapping_snapshot = _get_configuration(runtime, persisted_mapping.config_snapshot_id)
    _require(
        mapping_snapshot.config_digest == persisted_mapping.config_digest,
        "configuration",
        _MSG_CONFIG_MISSING,
    )
    mapping_configuration = mapping_snapshot.role_configuration()
    try:
        mapping_role_config = mapping_configuration.for_role(role)
    except AgentRoleError as exc:
        raise _fail("configuration", _MSG_CONFIG_ROLE) from exc
    _require(
        mapping_role_config == bounded.role_config,
        "configuration",
        _MSG_CONFIG_ROLE,
    )

    # An explicit resolved variant is required; an omitted variant is never defaulted.
    _require(persisted_mapping.variant is not None, "model", _MSG_VARIANT)
    _require(bounded.role_config.model.variant is not None, "model", _MSG_VARIANT)
    _require(
        (persisted_mapping.provider_id, persisted_mapping.model_id, persisted_mapping.variant)
        == (
            bounded.role_config.model.provider_id,
            bounded.role_config.model.model_id,
            bounded.role_config.model.variant,
        ),
        "model",
        _MSG_CONFIG_ROLE,
    )

    # Manager: local validation of the owned, current descriptor/connection/location.
    descriptor, connection, location, manager_configuration = _manager_surface(
        manager, project_id
    )
    _validate_manager_binding(
        descriptor, connection, location, persisted_mapping, project_id
    )

    try:
        manager_role_config = manager_configuration.for_role(role)
    except AgentRoleError as exc:
        raise _fail("configuration", _MSG_CONFIG_ROLE) from exc
    _require(manager_role_config == bounded.role_config, "configuration", _MSG_CONFIG_ROLE)

    fresh_context_required = role == "section_critic"
    if fresh_context_required:
        try:
            mapping_writer_config = mapping_configuration.for_role("writer")
            manager_writer_config = manager_configuration.for_role("writer")
        except AgentRoleError as exc:
            raise _fail("section_critic", _MSG_CRITIC) from exc
        _require(
            mapping_writer_config == manager_writer_config,
            "section_critic",
            _MSG_CRITIC,
        )
        try:
            manager_configuration.validate_section_critic()
        except AgentRoleError as exc:
            raise _fail("section_critic", _MSG_CRITIC) from exc
        _critic_guard(runtime, persisted_mapping, mapping_configuration)

    # The effective generated steps/model must be exactly what the task pins.
    generated = build_opencode_config(manager_configuration)
    agent = generated.get("agents", {}).get(role)
    _require(isinstance(agent, dict), "configuration", _MSG_CONFIG_ROLE)
    _require(agent["steps"] == bounded.step_limit, "step_limit", _MSG_STEPS)
    _require(
        agent["model"] == bounded.role_config.model.to_config(),
        "model",
        _MSG_MODEL_PIN,
    )

    # Revalidate the live target/inputs now (no write) before producing a new plan.
    _revalidate_live_refs(runtime, bounded)

    # Versions must be a validated VersionSet consistent with the dispatch.
    canonical_versions = _canonical_versions(versions, consumed)
    _check_versions(
        canonical_versions, persisted_dispatch, persisted_mapping, bounded.prompt_version
    )

    # Application text: canonical, portable, bounded, and serialized-size enforced.
    text_bytes = _canonical(text, _MSG_CANONICAL)
    _require(len(text_bytes) <= bounded.size_limit, "text", _MSG_TEXT_SIZE)
    message = _build_message(
        project_id,
        bounded,
        persisted_task,
        persisted_dispatch,
        persisted_mapping,
        canonical_versions,
        text,
    )
    _require(len(message) <= bounded.size_limit, "message", _MSG_MESSAGE_SIZE)

    admission_id = _admission_id(project_id, persisted_dispatch.dispatch_id)
    message_digest = hashlib.sha256(message).hexdigest()

    plan = {
        "kind": SESSION_TURN_SPEC_KIND,
        "spec_version": SESSION_TURN_SPEC_VERSION,
        "project_id": project_id,
        "role": role,
        "task": {
            "task_id": persisted_task.task_id,
            "request_id": persisted_task.request_id,
            "fingerprint": persisted_task.fingerprint,
            "target": dict(persisted_task.target),
            "input_refs": [dict(entry) for entry in persisted_task.input_refs],
            "scope": list(bounded.scope),
            "prompt_version": bounded.prompt_version,
            "size_limit": bounded.size_limit,
            "step_limit": bounded.step_limit,
        },
        "dispatch": {
            "dispatch_id": persisted_dispatch.dispatch_id,
            "request_id": persisted_dispatch.request_id,
            "route": persisted_dispatch.route,
            "stage": persisted_dispatch.stage,
            "input_digest": persisted_dispatch.input_digest,
            "version_digest": persisted_dispatch.version_digest,
        },
        "session": {
            "mapping_id": persisted_mapping.mapping_id,
            "session_id": persisted_mapping.session_id,
            "service_instance_id": persisted_mapping.service_instance_id,
            "clean_location": persisted_mapping.clean_location,
            "opencode_project_id": persisted_mapping.opencode_project_id,
            "config_snapshot_id": persisted_mapping.config_snapshot_id,
            "config_digest": persisted_mapping.config_digest,
            "model": persisted_mapping.model_ref,
        },
        "versions": canonical_versions,
        "admission_id": admission_id,
        "message_digest": message_digest,
        "fresh_context_required": fresh_context_required,
        "context_history": "unverified",
        "execution_boundary": dict(_EXECUTION_BOUNDARY),
    }
    canonical = _canonical(plan, _MSG_CANONICAL)

    return SessionTurnSpec(
        project_id=project_id,
        role=role,
        task_id=persisted_task.task_id,
        task_fingerprint=persisted_task.fingerprint,
        dispatch_id=persisted_dispatch.dispatch_id,
        dispatch_request_id=persisted_dispatch.request_id,
        mapping_id=persisted_mapping.mapping_id,
        session_id=persisted_mapping.session_id,
        service_instance_id=persisted_mapping.service_instance_id,
        clean_location=persisted_mapping.clean_location,
        config_snapshot_id=persisted_mapping.config_snapshot_id,
        config_digest=persisted_mapping.config_digest,
        model=persisted_mapping.model_ref,
        admission_id=admission_id,
        input_digest=persisted_dispatch.input_digest,
        version_digest=persisted_dispatch.version_digest,
        prompt_version=bounded.prompt_version,
        step_limit=bounded.step_limit,
        size_limit=bounded.size_limit,
        fresh_context_required=fresh_context_required,
        message_digest=message_digest,
        canonical=canonical,
        message=message,
    )
