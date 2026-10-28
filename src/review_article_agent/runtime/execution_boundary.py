"""Explicit, fail-closed **production-execution boundary** for the Step 04 runner.

This module is the small, safety-only gate a future session runner MUST consult
**before** it submits anything. It is *not* model execution, *not* approval, and
*not* a proof of scientific authority. It answers exactly one question:

*may this runner drive one turn against the app-owned service, using the one
verified **synthetic, loopback-fixture** provider, for this exact prepared plan?*

For every other route the answer is a static, typed refusal.

Authoritative sources (read-only):

* ``docs/contracts/02-gates-and-runtime.md`` §5.6 (cost/concurrency caps:
  ``reserve_usd`` MUST be recorded before a paid dispatch is admitted; a reserve
  is a cap, **not** a promise or a scientific threshold) and §6.3 (a ``CostRecord``
  ``reserve_usd`` is mandatory before a **paid** dispatch is admitted — a virtual
  positive reserve alone therefore never authorizes a live paid generation).
* ``docs/contracts/03-policy-decisions.md`` §5.2 (rights eligibility is **not**
  final authorization: an out-of-band, route/purpose/revision-scoped hosted-use
  authorization plus privacy and provider/journal-term verification are required)
  and §5.5 (provider/journal verification is **operation-scoped** and gates
  hosted-model use; a hosted transfer defaults to **blocked** until its terms are
  verified and the transfer is authorized).
* ``docs/implementation/04-agent-runtime.md`` acceptance criteria 1 (an agent
  cannot widen scope or invoke unapproved tools) and 7 (project/session/model
  isolation).

What this module is (state it plainly)
--------------------------------------

The helper :func:`assert_fixture_session_execution_allowed` returns ``None`` only
for the narrow, **synthetic** case it can actually verify end to end:

* the caller hands in a concrete :class:`~review_article_agent.runtime.service.OpenCodeServiceManager`
  configured with a validated :class:`~review_article_agent.runtime.service.LoopbackFixtureProvider`;
* the plan is a real, self-checking :class:`~review_article_agent.runtime.session_turns.SessionTurnSpec`
  that is **re-validated here** (a duck-typed or forged object is refused);
* the manager's own current owned descriptor/connection/location and its owned
  role configuration exactly pin the plan's role, model (with an **explicit**
  variant) and step limit, and the manager's recomputed service configuration
  still equals its own recorded bytes (configuration drift is refused);
* the plan's model is the fixture provider's provider id with a model id drawn
  from the fixture provider's own model list;
* **no** provider credential environment variable is present on the manager or in
  the owned descriptor.

Everything else is refused with a static, typed :class:`ExecutionBoundaryBlocked`.

What it deliberately does **not** do
------------------------------------

* It is **not** an out-of-band approval and **not** a persisted proof. It grants
  **no** scientific authority, admits **no** input, writes nothing, and creates no
  session. A runner MUST still persist its own correlation and call this gate at
  submission time; the gate must never be treated as a receipt.
* It sends **nothing** and retrieves **no** provider credential. It never calls
  ``manager.read_password``; the owned manager's ``descriptor()``/``connection()``
  ownership proof *may* internally read the service auth file to bind the live
  process identity, and this module neither triggers nor inspects that internal
  proof. The read-only ``existing_location`` lookup reads no credential at all.
* It accepts **no** imported authority: there is no ``authorized``/``token``/
  ``paid_budget``/bypass argument, and no field on the plan can grant authority.
  A caller cannot pass in a boolean, a token, or a budget and change the answer.
* It **does not** authorize a production paid generation or a hosted/real-provider
  route. :func:`assert_production_execution_supported` raises an explicit static
  blocker for the live ``opencode_session`` and ``direct_call`` routes, because a
  defensible all-request-bound cost bound plus scoped hosted-use rights, privacy
  review, and provider/journal-term verification are **not implemented yet**. A
  virtual positive ``reserve_usd`` (which *is* present on every reserved dispatch)
  is **not** that proof.
* The injected direct-transport seams that the tests use are **test-only** and are
  not authorized by this helper.

Security boundary (do not overclaim)
------------------------------------

This is a **loopback-fixture execution bound**, not an operating-system sandbox.
OpenCode and this application run as the same user, so nothing here constitutes a
same-UID containment guarantee. This module makes **no** assertion that a remote
model server (or any native process of the same UID) is trusted or trustworthy:
the fixture provider is a local synthetic transport, and the same-UID native
attacker is explicitly out of scope for this gate.
"""

from __future__ import annotations

import json
import os
import urllib.parse

from ..domain.errors import ReviewAgentError
from ..domain.records import canonical_json
from .opencode import assert_explicit_loopback_url
from .roles import AgentRoleError, ModelRef, RoleConfiguration, build_opencode_config
from .service import (
    LoopbackFixtureProvider,
    OpenCodeServiceManager,
    ProjectLocation,
    ServiceConnection,
    ServiceDescriptor,
    build_service_config,
)
from .session_turns import SessionTurnSpec
from .session_turns import _validate_spec as _revalidate_session_turn_spec

__all__ = [
    "DIRECT_ROUTE",
    "EXECUTION_BOUNDARY_AUTHORIZES_HOSTED_TRANSFER",
    "EXECUTION_BOUNDARY_AUTHORIZES_PAID_GENERATION",
    "EXECUTION_BOUNDARY_GRANTS_SCIENTIFIC_AUTHORITY",
    "EXECUTION_BOUNDARY_IS_OS_SANDBOX",
    "EXECUTION_BOUNDARY_IS_OUT_OF_BAND_APPROVAL",
    "EXECUTION_BOUNDARY_IS_PERSISTED_PROOF",
    "EXECUTION_BOUNDARY_VERIFIES_REMOTE_MODEL_SERVER",
    "ExecutionBoundaryBlocked",
    "ExecutionBoundaryError",
    "FIXTURE_ROUTE_IS_SYNTHETIC_LOOPBACK",
    "FixtureSessionBlocked",
    "KNOWN_EXECUTION_ROUTES",
    "PRODUCTION_SUPPORTED_ROUTES",
    "ProductionExecutionBlocked",
    "SESSION_ROUTE",
    "assert_fixture_session_execution_allowed",
    "assert_production_execution_supported",
]

#: The only accepted session route (C02 §6.3). Its wire name is shared with the
#: fixture case, but a *production* turn on it is still blocked (see below).
SESSION_ROUTE = "opencode_session"

#: The separately-accounted direct-call route (C02 §6.3; ADR 001 "direct calls …
#: accounted separately").
DIRECT_ROUTE = "direct_call"

#: The execution routes this module knows about. An unknown route is refused.
KNOWN_EXECUTION_ROUTES: tuple[str, ...] = (SESSION_ROUTE, DIRECT_ROUTE)

#: Routes for which a *production* (live-provider) execution is currently
#: supported. **Empty on purpose.** There is no defensible all-request-bound cost
#: proof and no scoped hosted-use/privacy/provider-term authorization implemented
#: yet, so no live route is supported and a virtual positive reserve is not proof.
PRODUCTION_SUPPORTED_ROUTES: frozenset[str] = frozenset()

#: Fixed, non-authorizing data. These are stated as data so a later consumer
#: cannot mistake this gate for authority it does not hold.
EXECUTION_BOUNDARY_IS_OUT_OF_BAND_APPROVAL = False
EXECUTION_BOUNDARY_IS_PERSISTED_PROOF = False
EXECUTION_BOUNDARY_GRANTS_SCIENTIFIC_AUTHORITY = False
EXECUTION_BOUNDARY_IS_OS_SANDBOX = False
EXECUTION_BOUNDARY_VERIFIES_REMOTE_MODEL_SERVER = False
EXECUTION_BOUNDARY_AUTHORIZES_PAID_GENERATION = False
EXECUTION_BOUNDARY_AUTHORIZES_HOSTED_TRANSFER = False

#: The fixture case that *is* verifiable is a synthetic, loopback-only transport.
FIXTURE_ROUTE_IS_SYNTHETIC_LOOPBACK = True

#: The per-project owned-location directory name under the service root (mirrors
#: ``service``'s layout; the exact directory still comes from the manager).
_LOCATION_PARENT_NAME = "projects"

#: The loopback hosts a fixture base URL may use (already enforced by
#: :class:`LoopbackFixtureProvider`; re-checked here so the bound is explicit).
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})

# --------------------------------------------------------------------------- #
# Static, value-free messages (never interpolate an id, path, model, key, value)
# --------------------------------------------------------------------------- #

_MSG_MANAGER = (
    "manager: must be an actual app-owned OpenCodeServiceManager exposing its "
    "owned descriptor, connection, and read-only existing location."
)
_MSG_FIXTURE = (
    "fixture: the manager must be configured with a validated loopback fixture "
    "provider."
)
_MSG_FIXTURE_LOOPBACK = "fixture: the fixture provider base URL must be loopback-only."
_MSG_CONFIGURATION = "configuration: must be a validated RoleConfiguration."
_MSG_CONFIG_DRIFT = (
    "configuration: the manager's recorded configuration bytes no longer match a "
    "fresh rebuild from its own inputs; refusing to trust a drifted manager."
)
_MSG_CREDENTIALS = (
    "credentials: no provider credential environment variable may be present for a "
    "fixture execution."
)
_MSG_SPEC = (
    "spec: must be an exact, strict, canonical session-turn plan (a forged or "
    "unvalidated object is refused)."
)
_MSG_MODEL = "model: the plan's model reference is not a valid explicit reference."
_MSG_MODEL_VARIANT = (
    "model: an explicit, resolved variant is required; an omitted or defaulted "
    "variant is refused."
)
_MSG_MODEL_PIN = (
    "model: the effective generated role model does not match the plan's model."
)
_MSG_FIXTURE_PROVIDER = (
    "model: the plan's model provider must be the configured fixture provider."
)
_MSG_FIXTURE_MODELS = (
    "model: the plan's model id must be one of the fixture provider's own models."
)
_MSG_ROLE = (
    "configuration: the manager's owned role configuration must equal the plan's "
    "role, model, and steps exactly."
)
_MSG_STEPS = "step_limit: the effective generated role steps must equal the plan step limit."
_MSG_LOOPBACK = "endpoint: the owned descriptor endpoint is not an explicit loopback origin."
_MSG_DESCRIPTOR = "manager: the owned descriptor and connection are not internally consistent."
_MSG_LOCATION = "session: the owned project location does not match the plan's recorded location."
_MSG_SERVICE_INSTANCE = (
    "session: the owned service instance does not match the plan's recorded session."
)
_MSG_UNKNOWN_ROUTE = "route: unknown execution route."
_MSG_SESSION_BLOCKED = (
    "route: a production opencode_session execution is not supported yet: a "
    "defensible all-request-bound cost proof, scoped hosted-use rights, privacy "
    "review, and provider/journal-term verification are not implemented. A virtual "
    "positive reserve alone is not proof."
)
_MSG_DIRECT_BLOCKED = (
    "route: a production direct_call execution is not supported yet, and it would "
    "additionally require separate cost and hosted-use authorization. A virtual "
    "positive reserve alone is not proof."
)

#: The static blocker code/message for each known production route.
_PRODUCTION_BLOCKERS: dict[str, tuple[str, str]] = {
    SESSION_ROUTE: ("session_route_blocked", _MSG_SESSION_BLOCKED),
    DIRECT_ROUTE: ("direct_route_blocked", _MSG_DIRECT_BLOCKED),
}


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #
class ExecutionBoundaryError(ReviewAgentError):
    """Base class for an execution-boundary refusal.

    ``code`` is a static, value-free identifier and the message is a constant, so
    a refusal raised from untrusted input never echoes an id, digest, path, model,
    role, key, or value.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ExecutionBoundaryBlocked(ExecutionBoundaryError):
    """A requested execution was refused (fail closed). This is the typed blocker."""


class FixtureSessionBlocked(ExecutionBoundaryBlocked):
    """The synthetic fixture session case was not exactly satisfied."""


class ProductionExecutionBlocked(ExecutionBoundaryBlocked):
    """A production (live-provider) execution route is not supported yet."""


def _block(code: str, message: str) -> "FixtureSessionBlocked":
    return FixtureSessionBlocked(code, message)


def _require(condition: bool, code: str, message: str) -> None:
    if not condition:
        raise FixtureSessionBlocked(code, message)


# --------------------------------------------------------------------------- #
# Guarded reads (nothing about the manager is trusted before it is re-derived)
# --------------------------------------------------------------------------- #
def _revalidate_plan(spec: object) -> SessionTurnSpec:
    """Re-validate the plan as its exact, self-checking value object."""
    if not isinstance(spec, SessionTurnSpec):
        raise _block("spec", _MSG_SPEC)
    try:
        _revalidate_session_turn_spec(spec)
    except (ReviewAgentError, ValueError, TypeError, KeyError, AttributeError) as exc:
        raise _block("spec", _MSG_SPEC) from exc
    return spec


def _fixture_base_url_is_loopback(fixture: LoopbackFixtureProvider) -> bool:
    parsed = urllib.parse.urlsplit(fixture.base_url)
    return parsed.scheme == "http" and parsed.hostname in _LOOPBACK_HOSTS


def _config_is_undrifted(
    manager: OpenCodeServiceManager,
    configuration: RoleConfiguration,
    fixture: LoopbackFixtureProvider,
) -> bool:
    """True only when the manager's own recorded config bytes are a fresh rebuild.

    This never trusts the manager's cached descriptor/config: the bytes are
    recomputed from the manager's own ``configuration`` + fixture provider and
    byte-compared, then required to carry exactly this fixture provider and its
    deny-all-then-allow-fixture provider policy.
    """
    try:
        expected = canonical_json(
            build_service_config(configuration, fixture_provider=fixture)
        )
    except (ReviewAgentError, ValueError, TypeError) as exc:
        raise _block("configuration", _MSG_CONFIGURATION) from exc
    stored = getattr(manager, "config_bytes", None)
    if not isinstance(stored, bytes) or stored != expected:
        return False
    try:
        doc = json.loads(stored.decode("utf-8"))
    except (ValueError, UnicodeError):
        return False
    if not isinstance(doc, dict):
        return False
    providers = doc.get("providers")
    if not isinstance(providers, dict) or set(providers) != {fixture.provider_id}:
        return False
    policies = doc.get("policies")
    if not isinstance(policies, list):
        return False
    deny_all = {"action": "provider.use", "resource": "*", "effect": "deny"}
    allow_fixture = {
        "action": "provider.use",
        "resource": fixture.provider_id,
        "effect": "allow",
    }
    return deny_all in policies and allow_fixture in policies


def _owned_surface(
    manager: OpenCodeServiceManager, project_id: str
) -> tuple[ServiceDescriptor, ServiceConnection, ProjectLocation]:
    """Read the manager's own current owned descriptor/connection/location.

    Every failure (a foreign/absent location, a drifted record, an unowned root,
    a not-running service) is a fail-closed refusal. This performs **no** network
    probe, creates nothing, and never reads a credential value directly.
    """
    try:
        descriptor = manager.descriptor()
        connection = manager.connection()
        location = manager.existing_location(project_id)
    except ReviewAgentError as exc:
        raise _block("manager", _MSG_MANAGER) from exc
    except Exception as exc:  # noqa: BLE001 - any failure is a fail-closed refusal
        raise _block("manager", _MSG_MANAGER) from exc
    if (
        not isinstance(descriptor, ServiceDescriptor)
        or not isinstance(connection, ServiceConnection)
        or not isinstance(location, ProjectLocation)
    ):
        raise _block("manager", _MSG_MANAGER)
    return descriptor, connection, location


def _model_from_plan(spec: SessionTurnSpec) -> ModelRef:
    try:
        model = ModelRef.from_mapping(spec.model)
    except (AgentRoleError, ValueError, TypeError) as exc:
        raise _block("model", _MSG_MODEL) from exc
    if model.variant is None or model.ref != spec.model:
        raise _block("model", _MSG_MODEL_VARIANT)
    return model


# --------------------------------------------------------------------------- #
# Public gate: the synthetic fixture session case (and only that case)
# --------------------------------------------------------------------------- #
def assert_fixture_session_execution_allowed(manager: object, spec: object) -> None:
    """Return ``None`` only for the one verified synthetic fixture session case.

    The runner MUST call this before it submits anything. Nothing about the
    caller's claims is trusted: the plan is re-validated as an exact session-turn
    plan, and the manager's current owned descriptor/connection/location and its
    owned role configuration are re-derived and required to pin the plan exactly.
    Any other case raises a static, typed :class:`ExecutionBoundaryBlocked`.

    There is **no** ``authorized``/``token``/``paid_budget``/bypass argument: no
    imported authority can change the outcome.
    """
    _require(isinstance(manager, OpenCodeServiceManager), "manager", _MSG_MANAGER)
    fixture = getattr(manager, "fixture_provider", None)
    _require(isinstance(fixture, LoopbackFixtureProvider), "fixture", _MSG_FIXTURE)
    _require(_fixture_base_url_is_loopback(fixture), "fixture", _MSG_FIXTURE_LOOPBACK)

    configuration = getattr(manager, "configuration", None)
    _require(isinstance(configuration, RoleConfiguration), "configuration", _MSG_CONFIGURATION)
    _require(
        _config_is_undrifted(manager, configuration, fixture),
        "configuration",
        _MSG_CONFIG_DRIFT,
    )

    provider_env = getattr(manager, "provider_env", None)
    _require(
        isinstance(provider_env, dict) and not provider_env,
        "credentials",
        _MSG_CREDENTIALS,
    )

    plan = _revalidate_plan(spec)
    # The plan's own declared route must be the one session route; a direct-call
    # plan is never a fixture session case (and is blocked as production).
    _require(plan.to_mapping()["dispatch"]["route"] == SESSION_ROUTE, "spec", _MSG_SPEC)
    model = _model_from_plan(plan)

    # The plan's model must be exactly the fixture provider's id and one of its
    # own models (a real/catalog provider id is never accepted here).
    _require(model.provider_id == fixture.provider_id, "model", _MSG_FIXTURE_PROVIDER)
    _require(model.model_id in fixture.models, "model", _MSG_FIXTURE_MODELS)

    # The manager's owned role configuration must pin the role/model/steps exactly.
    try:
        role_config = configuration.for_role(plan.role)
    except (AgentRoleError, ValueError, TypeError) as exc:
        raise _block("configuration", _MSG_ROLE) from exc
    _require(role_config.model == model, "configuration", _MSG_ROLE)
    _require(role_config.steps == plan.step_limit, "configuration", _MSG_STEPS)

    # The *declared generated* agent pins for the role must match the plan too.
    generated = build_opencode_config(configuration)
    agent = generated.get("agents", {}).get(plan.role)
    _require(isinstance(agent, dict), "configuration", _MSG_ROLE)
    _require(agent["steps"] == plan.step_limit, "step_limit", _MSG_STEPS)
    _require(agent["model"] == model.to_config(), "model", _MSG_MODEL_PIN)

    # The manager's own current owned surface, re-derived (never trusted).
    descriptor, connection, location = _owned_surface(manager, plan.project_id)
    try:
        assert_explicit_loopback_url(descriptor.url)
    except (ValueError, TypeError) as exc:
        raise _block("endpoint", _MSG_LOOPBACK) from exc
    _require(str(manager.root) == descriptor.root, "manager", _MSG_DESCRIPTOR)
    _require(
        connection.base_url == descriptor.url
        and connection.password_file == descriptor.password_file,
        "manager",
        _MSG_DESCRIPTOR,
    )
    _require(
        tuple(descriptor.provider_env_names) == (),
        "credentials",
        _MSG_CREDENTIALS,
    )

    # Clean mapping project/location ownership must equal the plan exactly.
    _require(location.app_project_id == plan.project_id, "session", _MSG_LOCATION)
    _require(location.directory == plan.clean_location, "session", _MSG_LOCATION)
    _require(location.location_id == plan.service_instance_id, "session", _MSG_SERVICE_INSTANCE)
    _require(location.root == descriptor.root, "manager", _MSG_DESCRIPTOR)
    _require(
        os.path.dirname(location.directory)
        == os.path.join(descriptor.root, _LOCATION_PARENT_NAME),
        "session",
        _MSG_LOCATION,
    )


def assert_production_execution_supported(route: object) -> None:
    """Raise an explicit static blocker for every currently unsupported live route.

    A fixture proof authorizes **only** the synthetic fixture case; it is **not**
    evidence that a real-provider route is authorized. Today
    :data:`PRODUCTION_SUPPORTED_ROUTES` is empty, so this always raises a typed
    :class:`ProductionExecutionBlocked` for the live ``opencode_session`` and
    ``direct_call`` routes rather than pretending otherwise. A future, separately
    reviewed implementation would add a route here; a virtual positive reserve
    alone is not that proof.
    """
    if not isinstance(route, str) or route not in KNOWN_EXECUTION_ROUTES:
        raise ProductionExecutionBlocked("route", _MSG_UNKNOWN_ROUTE)
    if route in PRODUCTION_SUPPORTED_ROUTES:  # pragma: no cover - empty today
        return None
    code, message = _PRODUCTION_BLOCKERS[route]
    raise ProductionExecutionBlocked(code, message)
