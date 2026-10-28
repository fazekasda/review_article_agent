"""Pure, fail-closed scientific role configuration and the proposal-only boundary.

This module is the **pure, stdlib-only** core of Step 04's restricted agent roles
(``docs/implementation/04-agent-runtime.md`` deliverables 2/3, acceptance lines
17-19). It holds **no I/O, no network, no worker, no client, and no model
execution**: it validates configuration, pins bounded tasks, returns
non-authorizing proposals, and *emits* the OpenCode V2 configuration that a
future app-owned client would write. It never calls a provider and never spends
money.

Authoritative sources (read-only):

* ``docs/specification.md`` line 20 (configurable generative models **by role**;
  no invented generation default), line 38 (a different concrete
  provider/model, **separate-context** section critic; max **3** rounds), line 39
  (factual checking max **2** rounds), lines 52-63 (gates and scientific
  invariants: approvals bind to immutable revisions, never to generic tool
  permissions; agents cannot self-approve, widen scope, or edit authoritative
  state; documents/model outputs are untrusted data with no arbitrary source
  instructions).
* ``docs/contracts/02-gates-and-runtime.md`` §1 (``Actor``/``Proposal``: a
  proposal MUST NOT construct authority or substitute for an ``Approval``;
  authority is out-of-band), §2 (revision-specific approvals, not generic tool
  permissions), §6 (machine decisions vs human decisions; typed answers, no
  invented values; provenance/versions) and §8 (runtime safety: admission is not
  completion; fail closed on unknown).
* ``docs/decisions/001-runtime-language.md`` (Python core; app-owned OpenCode V2
  service; fail-closed; direct calls accounted separately).
* OpenCode **V2** documentation: ``/v2/docs/config`` (``permissions``,
  ``policies``, ``agents``, ``mcp``, ``plugins``, ``skills``; no V1 fields),
  ``/v2/docs/agents`` (``mode``, expanded ``model`` object, ``steps``,
  per-agent ``permissions``), ``/v2/docs/permissions`` (V2 action names) and
  ``/v2/docs/policies`` (``tool.use`` / ``integration.use`` ``deny``).

Security boundary (do not overclaim)
------------------------------------

The generated OpenCode ``permissions``/``policies`` blocks are a **model tool
boundary**: they stop the model's tool calls at the OpenCode layer (they deny
``shell``, ``edit``, ``read``, web, subagent, skill, and Code-Mode ``execute``,
and block inherited MCP servers, skills, and plugins). They are **not** a
same-UID operating-system sandbox: OpenCode and this application run as the same
user, so a denial here does not contain a compromised process that bypasses
OpenCode entirely. The application-service isolation (owner-only control
transport, per-project store, non-owner denial) is a **separate** concern and is
tested elsewhere (Step 03 IPC/activation suites); it is **not** proven by this
module and is not claimed here. This module also makes **no** scientific-claim:
a validated structure is format/consistency only, never scientific correctness,
eligibility, or approval.

What this module deliberately does **not** do
---------------------------------------------

* It does not implement an approval, a gate decision, or an owner-auth check.
  There is **no** ``Approval`` type here; a proposal cannot be turned into one.
* It does not pick a default generation model. Every role's ``provider``/``model``
  is **explicit** in configuration; a missing or unknown role fails closed with
  no fallback to another role, a global model, or a variant.
* It does not read or write files and does not enable any MCP server, skill, or
  plugin.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from ..domain.errors import ReviewAgentError
from ..domain.records import canonical_json, validate_identifier

# --------------------------------------------------------------------------- #
# Vocabulary and hard bounds
# --------------------------------------------------------------------------- #

#: The versioned configuration schema this module accepts.
ROLE_CONFIG_SCHEMA_VERSION = 1

#: The finite set of dedicated scientific roles (``docs/specification.md``
#: "Confirmed scientific workflow"). No role outside this set is accepted, so a
#: typo cannot silently become an unpermissioned actor.
SCIENTIFIC_ROLES: tuple[str, ...] = (
    "lead",
    "search",
    "extractor",
    "synthesizer",
    "writer",
    "section_critic",
    "fact_checker",
    "manuscript_reviewer",
    "origin_tracer",
)

#: Upper bound on any role's agent ``steps`` (finite, bounded model steps).
MAX_ROLE_STEPS = 32

#: Upper bound on a task's ``step_limit`` (never larger than :data:`MAX_ROLE_STEPS`).
MAX_TASK_STEPS = MAX_ROLE_STEPS

#: Upper bound, in bytes of canonical JSON, on an accepted proposal payload.
MAX_TASK_SIZE_BYTES = 262_144

#: Bounds on a task's explicit scope tokens.
MAX_SCOPE_TOKENS = 64
MAX_SCOPE_TOKEN_LEN = 200

#: Upper bound on the number of exact input references a task may pin.
MAX_INPUT_REFS = 256

#: Bounds on the explicit prompt-version string and model tokens.
MAX_PROMPT_VERSION_LEN = 128
MAX_MODEL_TOKEN_LEN = 200

#: ``docs/specification.md`` line 38: a section critique/repair loop is at most
#: three rounds; line 15 of the same baseline fixes that limit.
MAX_SECTION_CRITIQUE_ROUNDS = 3

#: ``docs/specification.md`` line 39: original-source factual checking is at most
#: two rounds.
MAX_FACT_CHECK_ROUNDS = 2

#: The critic runs in a **fresh** app-owned session, never a fork of the writer's
#: history (``docs/specification.md`` line 38; ADR ``001`` "a writer-history fork
#: is not a clean critic"). Exposed as data so a future client cannot forget it.
CRITIC_REQUIRES_FRESH_CONTEXT = True

#: A proposal is data about a model's output; it is **never** authoritative.
PROPOSAL_KIND = "proposal"
PROPOSAL_IS_AUTHORITATIVE = False

#: Default bounded ``steps`` per role (a bounded operational limit, never a model
#: choice). Each is <= :data:`MAX_ROLE_STEPS`.
ROLE_DEFAULT_STEPS: dict[str, int] = {
    "lead": 16,
    "search": 12,
    "extractor": 12,
    "synthesizer": 12,
    "writer": 16,
    "section_critic": 8,
    "fact_checker": 8,
    "manuscript_reviewer": 8,
    "origin_tracer": 10,
}

#: One-line role descriptions (used for the agent catalog and system prompt).
ROLE_DESCRIPTIONS: dict[str, str] = {
    "lead": (
        "Interviews the user about the brief and challenges terminology, "
        "frameworks, and evidence."
    ),
    "search": (
        "Executes documented queries and citation expansion and reports "
        "coverage; never promises all papers."
    ),
    "extractor": (
        "Extracts readable summaries, structured findings, and original "
        "passages/locators from original text."
    ),
    "synthesizer": (
        "Builds the cross-paper evidence map / synthesis memo, preserving "
        "uncertainty and contradictions."
    ),
    "writer": (
        "Drafts a section from summaries and linked original evidence; "
        "proposes, never approves."
    ),
    "section_critic": (
        "Critiques a section with a different concrete model in a fresh "
        "context; proposes findings only."
    ),
    "fact_checker": (
        "Checks a draft against original-source evidence and proposes repairs "
        "within bounded rounds."
    ),
    "manuscript_reviewer": (
        "Reviews the whole manuscript for coherence, precision, "
        "contradictions, and omissions."
    ),
    "origin_tracer": (
        "Traces claim-relevant citations and proposes origin attributions, "
        "warnings, and open questions."
    ),
}

# --------------------------------------------------------------------------- #
# OpenCode V2 configuration vocabulary
# --------------------------------------------------------------------------- #

#: V2 config schema URL written as ``$schema``.
OPENCODE_CONFIG_SCHEMA_URL = "https://opencode.ai/config.json"

#: Every dedicated role runs in its own app-owned session as the session's main
#: agent; the application (not the model) decides when to create it.
OPENCODE_AGENT_MODE = "primary"

#: V2 built-in tool permission actions that the role config denies outright.
#: Names are V2 (``shell``/``subagent``), never V1 (``bash``/``task``).
OPENCODE_BUILTIN_TOOL_ACTIONS: tuple[str, ...] = (
    "read",
    "edit",
    "glob",
    "grep",
    "shell",
    "subagent",
    "skill",
    "question",
    "webfetch",
    "websearch",
    "external_directory",
    "execute",
    "opencode_list_mcp_resources",
    "opencode_read_mcp_resource",
)

#: ``tool.use`` policy resources that hard-deny each of the above actions.
OPENCODE_DENIED_TOOL_RESOURCES: tuple[str, ...] = tuple(
    f"{action}:*" for action in OPENCODE_BUILTIN_TOOL_ACTIONS
)

#: ``integration.use`` resources that stop inherited MCP servers, skills, and
#: plugins from loading for these roles.
OPENCODE_DENIED_INTEGRATIONS: tuple[str, ...] = ("mcp:*", "skill:*", "plugin:*")

#: V1 / deprecated top-level agent and config keys this module never emits
#: (``/v2/docs/agents`` "Do not use legacy top-level fields"). Present so tests
#: can prove the generated config stays on V2.
OPENCODE_DEPRECATED_V1_KEYS = frozenset(
    {
        "permission",
        "bash",
        "task",
        "tools",
        "disable",
        "maxSteps",
        "temperature",
        "top_p",
        "prompt",
    }
)

_PROVIDER_RE = re.compile(r"^[^\s#/]+$")
_MODEL_RE = re.compile(r"^[^\s#]+$")
_VARIANT_RE = re.compile(r"^[^\s#]+$")
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")

# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #


class AgentRoleError(ReviewAgentError):
    """Base class for every role/task/proposal boundary failure."""


class RoleConfigError(AgentRoleError):
    """A scientific role configuration is invalid (and never defaulted)."""


class CriticConfigError(RoleConfigError):
    """The section critic is not a distinct concrete model in a fresh context."""


class TaskBoundaryError(AgentRoleError):
    """A bounded task is malformed or tries to carry authority/tool overrides."""


class ProposalError(AgentRoleError):
    """A proposal payload is malformed or tries to smuggle authority/state."""


# --------------------------------------------------------------------------- #
# Small validation helpers
# --------------------------------------------------------------------------- #


def _canonical_bytes(value: object, *, error_cls: type[AgentRoleError], label: str) -> bytes:
    """Return canonical JSON bytes or raise ``error_cls`` (never echo the value)."""
    try:
        return canonical_json(value)
    except ValueError as exc:  # RecordValidationError is a ValueError subclass
        raise error_cls(
            f"{label} is not bounded, well-formed JSON ({type(exc).__name__}); "
            f"supply portable JSON data only."
        ) from exc


def _require_plain_int(value: object, *, error_cls: type[AgentRoleError], field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise error_cls(f"{field} must be an integer.")
    return value


def _normalize_key(key: str) -> str:
    return key.strip().lower().replace("-", "").replace("_", "")


def _validate_model_token(
    value: object,
    *,
    field: str,
    pattern: re.Pattern[str],
) -> str:
    if not isinstance(value, str) or value == "":
        raise RoleConfigError(f"model {field} must be a non-empty string.")
    if value != value.strip() or any(ch.isspace() for ch in value):
        raise RoleConfigError(f"model {field} must not contain whitespace.")
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
        raise RoleConfigError(f"model {field} must not contain control characters.")
    if len(value) > MAX_MODEL_TOKEN_LEN:
        raise RoleConfigError(f"model {field} is longer than {MAX_MODEL_TOKEN_LEN} characters.")
    if not pattern.match(value):
        raise RoleConfigError(
            f"model {field} contains a character that is not allowed in a "
            f"concrete provider/model reference."
        )
    return value


# --------------------------------------------------------------------------- #
# Explicit concrete model reference
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ModelRef:
    """An **explicit, concrete** provider/model reference with an optional variant.

    ``provider_id`` and ``model_id`` identify the model; ``variant`` only selects
    a reasoning/effort profile. Two references with the same ``provider_id`` and
    ``model_id`` are the **same model** even when their variants differ, which is
    exactly what the section-critic rule forbids (``docs/specification.md`` line 38).
    """

    provider_id: str
    model_id: str
    variant: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "provider_id",
            _validate_model_token(self.provider_id, field="provider_id", pattern=_PROVIDER_RE),
        )
        object.__setattr__(
            self,
            "model_id",
            _validate_model_token(self.model_id, field="model_id", pattern=_MODEL_RE),
        )
        if self.variant is not None:
            object.__setattr__(
                self,
                "variant",
                _validate_model_token(self.variant, field="variant", pattern=_VARIANT_RE),
            )

    @property
    def ref(self) -> str:
        """The ``provider/model`` (or ``provider/model#variant``) spelling."""
        base = f"{self.provider_id}/{self.model_id}"
        return base if self.variant is None else f"{base}#{self.variant}"

    def same_model(self, other: "ModelRef") -> bool:
        """True when both references select the **same concrete model**.

        Compares ``provider_id`` + ``model_id`` only: a different ``variant`` is a
        different effort setting, *not* a different model.
        """
        return self.provider_id == other.provider_id and self.model_id == other.model_id

    def to_config(self) -> dict[str, str]:
        """The expanded OpenCode V2 ``agents.<id>.model`` object (no V1 fields)."""
        model: dict[str, str] = {
            "providerID": self.provider_id,
            "model": self.model_id,
        }
        if self.variant is not None:
            model["variant"] = self.variant
        return model

    @classmethod
    def from_mapping(
        cls,
        value: object,
        *,
        error_cls: type[AgentRoleError] = RoleConfigError,
    ) -> "ModelRef":
        """Parse ``"provider/model#variant"`` or ``{provider_id, model_id, variant}``.

        Never invents a provider, model, or variant: every component must be
        present in the input.
        """
        if isinstance(value, str):
            return cls._parse_ref_string(value, error_cls=error_cls)
        if isinstance(value, Mapping):
            allowed = {"provider_id", "model_id", "variant"}
            unknown = [k for k in value if k not in allowed]
            if unknown:
                raise error_cls(
                    "a role model has an unknown field; only provider_id, "
                    "model_id, and variant are permitted."
                )
            for required in ("provider_id", "model_id"):
                if required not in value:
                    raise error_cls(f"a role model is missing required field {required!r}.")
            try:
                return cls(
                    provider_id=value["provider_id"],
                    model_id=value["model_id"],
                    variant=value.get("variant"),
                )
            except RoleConfigError as exc:
                raise error_cls(str(exc)) from exc
        raise error_cls(
            "a role model must be a 'provider/model#variant' string or an object "
            "with provider_id/model_id."
        )

    @classmethod
    def _parse_ref_string(cls, value: str, *, error_cls: type[AgentRoleError]) -> "ModelRef":
        if value == "" or value != value.strip():
            raise error_cls("a role model reference must be non-empty with no outer whitespace.")
        variant: str | None = None
        base = value
        if "#" in value:
            base, _, variant = value.partition("#")
            if variant == "":
                raise error_cls("a role model variant after '#' must not be empty.")
        provider, sep, model = base.partition("/")
        if sep == "" or provider == "" or model == "":
            raise error_cls(
                "a role model reference must be 'provider/model' (optionally "
                "'provider/model#variant'); a bare model name is not accepted."
            )
        try:
            return cls(provider_id=provider, model_id=model, variant=variant)
        except RoleConfigError as exc:
            raise error_cls(str(exc)) from exc


# --------------------------------------------------------------------------- #
# Role configuration
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class RoleConfig:
    """One scientific role's explicit model and bounded step limit."""

    role: str
    model: ModelRef
    steps: int

    def __post_init__(self) -> None:
        if self.role not in SCIENTIFIC_ROLES:
            raise RoleConfigError(
                f"{self.role!r} is not a known scientific role; the known roles "
                f"are {', '.join(SCIENTIFIC_ROLES)}."
            )
        if not isinstance(self.model, ModelRef):
            raise RoleConfigError("role model must be a ModelRef.")
        steps = _require_plain_int(self.steps, error_cls=RoleConfigError, field="role steps")
        if steps < 1 or steps > MAX_ROLE_STEPS:
            raise RoleConfigError(
                f"role steps must be between 1 and {MAX_ROLE_STEPS} (got {steps})."
            )


@dataclass(frozen=True)
class RoleConfiguration:
    """A validated set of explicitly-configured scientific roles.

    There is **no** fallback: a role that is not configured cannot be used, and
    asking for it raises :class:`RoleConfigError` rather than borrowing another
    role's model or a global default.
    """

    roles: tuple[RoleConfig, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.roles, tuple):
            raise RoleConfigError("roles must be a tuple of RoleConfig entries.")
        seen: set[str] = set()
        for entry in self.roles:
            if not isinstance(entry, RoleConfig):
                raise RoleConfigError("every configured role must be a RoleConfig.")
            if entry.role in seen:
                raise RoleConfigError(f"role {entry.role!r} is configured more than once.")
            seen.add(entry.role)

    @property
    def role_names(self) -> tuple[str, ...]:
        return tuple(entry.role for entry in self.roles)

    def for_role(self, role: str) -> RoleConfig:
        """Return the configured role, or fail closed (never a fallback)."""
        if role not in SCIENTIFIC_ROLES:
            raise RoleConfigError(
                f"{role!r} is not a known scientific role; the known roles are "
                f"{', '.join(SCIENTIFIC_ROLES)}."
            )
        for entry in self.roles:
            if entry.role == role:
                return entry
        raise RoleConfigError(
            f"role {role!r} has no configured provider/model; refusing to fall "
            f"back to another role or a global default. Configure it explicitly."
        )

    def validate_section_critic(self) -> "CriticPlan":
        """Return the critic plan, requiring a distinct concrete model.

        The critic MUST be a **different** concrete ``provider``+``model`` than the
        writer (a different ``variant`` is insufficient) and MUST run in a
        **fresh** context (``docs/specification.md`` line 38). Any violation
        raises :class:`CriticConfigError`; the caller never silently uses the
        writer model.
        """
        writer = self.for_role("writer")
        critic = self.for_role("section_critic")
        if critic.model.same_model(writer.model):
            raise CriticConfigError(
                "the section critic must be a different concrete provider/model "
                f"than the writer; both resolve to '{critic.model.provider_id}/"
                f"{critic.model.model_id}'. A different variant is not a different "
                "model."
            )
        return CriticPlan(
            critic=critic,
            writer=writer,
            fresh_context=CRITIC_REQUIRES_FRESH_CONTEXT,
            max_rounds=MAX_SECTION_CRITIQUE_ROUNDS,
        )

    def as_mapping(self) -> dict[str, Any]:
        """A deterministic, JSON-safe representation for persistence."""
        roles: dict[str, Any] = {}
        for entry in sorted(self.roles, key=lambda item: item.role):
            spec: dict[str, Any] = {
                "provider_id": entry.model.provider_id,
                "model_id": entry.model.model_id,
                "steps": entry.steps,
            }
            if entry.model.variant is not None:
                spec["variant"] = entry.model.variant
            roles[entry.role] = spec
        return {"schema_version": ROLE_CONFIG_SCHEMA_VERSION, "roles": roles}

    @classmethod
    def from_mapping(cls, value: object) -> "RoleConfiguration":
        """Validate an explicit role configuration mapping (fail closed).

        Accepted shape::

            {"schema_version": 1,
             "roles": {"writer": {"provider_id": "...", "model_id": "...",
                                  "variant": "...", "steps": 16}, ...}}

        Each role value may also be the ``"provider/model#variant"`` string form.
        Unknown fields, unknown roles, a missing/blank model, or an unsupported
        schema version are all rejected.
        """
        if not isinstance(value, Mapping):
            raise RoleConfigError("a role configuration must be an object.")
        allowed_top = {"schema_version", "roles"}
        unknown = [key for key in value if key not in allowed_top]
        if unknown:
            raise RoleConfigError(
                "the role configuration contains an unknown field; only "
                "'schema_version' and 'roles' are permitted."
            )
        if "schema_version" not in value:
            raise RoleConfigError(
                f"the role configuration is missing 'schema_version' "
                f"(expected {ROLE_CONFIG_SCHEMA_VERSION})."
            )
        version = _require_plain_int(
            value["schema_version"], error_cls=RoleConfigError, field="schema_version"
        )
        if version != ROLE_CONFIG_SCHEMA_VERSION:
            raise RoleConfigError(
                f"role configuration schema_version {version} is not supported "
                f"(this build accepts exactly {ROLE_CONFIG_SCHEMA_VERSION})."
            )
        roles_raw = value.get("roles")
        if not isinstance(roles_raw, Mapping):
            raise RoleConfigError("the role configuration 'roles' field must be an object.")
        entries: list[RoleConfig] = []
        for role, spec in roles_raw.items():
            if role not in SCIENTIFIC_ROLES:
                raise RoleConfigError(
                    f"{role!r} is not a known scientific role; the known roles "
                    f"are {', '.join(SCIENTIFIC_ROLES)}."
                )
            if isinstance(spec, Mapping):
                allowed_spec = {"provider_id", "model_id", "variant", "steps"}
                unknown_spec = [key for key in spec if key not in allowed_spec]
                if unknown_spec:
                    raise RoleConfigError(
                        f"role {role!r} has an unknown field; only provider_id, "
                        "model_id, variant, and steps are permitted."
                    )
                _model_keys = ("provider_id", "model_id", "variant")
                model_spec: object = {k: spec[k] for k in _model_keys if k in spec}
                steps_raw = spec.get("steps")
            else:
                model_spec = spec
                steps_raw = None
            model = ModelRef.from_mapping(model_spec)
            if steps_raw is None:
                steps = ROLE_DEFAULT_STEPS[role]
            else:
                steps = _require_plain_int(
                    steps_raw, error_cls=RoleConfigError, field=f"{role}.steps"
                )
            entries.append(RoleConfig(role=role, model=model, steps=steps))
        return cls(roles=tuple(entries))


@dataclass(frozen=True)
class CriticPlan:
    """The validated section-critic arrangement handed to a future client.

    ``fresh_context`` is always ``True``: the client MUST create a **new**
    app-owned session for the critic, never a fork of the writer's history.
    ``max_rounds`` is the fixed three-round critique/repair budget.
    """

    critic: RoleConfig
    writer: RoleConfig
    fresh_context: bool
    max_rounds: int


# --------------------------------------------------------------------------- #
# Bounded task
# --------------------------------------------------------------------------- #

_TASK_KEYS = frozenset(
    {
        "project_id",
        "role",
        "target",
        "input_refs",
        "scope",
        "prompt_version",
        "size_limit",
        "step_limit",
    }
)
_REF_KEYS = ("project_id", "artifact_id", "revision_id")
_INPUT_REF_KEYS = ("project_id", "artifact_id", "revision_id", "digest")


@dataclass(frozen=True)
class BoundedTask:
    """A strictly-bounded, data-only task pinned to one project, role, and revision.

    A task is **input data**, never a request: it cannot carry an actor, an
    approval, authority, a budget, or tool permissions. Every field is pinned by
    the application (not the model): the exact project, the exact target
    revision, the exact input revision refs and their canonical digests, the
    explicit scope, prompt version, size limit, and step limit.
    """

    project_id: str
    role: str
    target: Mapping[str, str]
    input_refs: tuple[Mapping[str, str], ...]
    scope: tuple[str, ...]
    prompt_version: str
    size_limit: int
    step_limit: int
    role_config: RoleConfig

    def to_mapping(self) -> dict[str, Any]:
        """A deterministic, JSON-safe representation (no authority fields)."""
        return {
            "project_id": self.project_id,
            "role": self.role,
            "target": dict(self.target),
            "input_refs": [dict(entry) for entry in self.input_refs],
            "scope": list(self.scope),
            "prompt_version": self.prompt_version,
            "size_limit": self.size_limit,
            "step_limit": self.step_limit,
        }

    def canonical_bytes(self) -> bytes:
        return _canonical_bytes(
            self.to_mapping(), error_cls=TaskBoundaryError, label="bounded task"
        )

    @classmethod
    def from_mapping(
        cls,
        value: object,
        *,
        configuration: RoleConfiguration,
    ) -> "BoundedTask":
        """Validate a task mapping against the configured role bounds.

        Unknown top-level fields (which is how an actor, approval, authority,
        budget, or tool-permission override would arrive) are rejected outright.
        """
        if not isinstance(value, Mapping):
            raise TaskBoundaryError("a bounded task must be an object.")
        unknown = [key for key in value if key not in _TASK_KEYS]
        if unknown:
            raise TaskBoundaryError(
                "the task contains a field that is not permitted; a task is "
                "data only and cannot carry an actor, approval, authority, "
                "budget, or tool permission."
            )
        for required in _TASK_KEYS:
            if required not in value:
                raise TaskBoundaryError(f"the task is missing required field {required!r}.")

        try:
            project_id = validate_identifier(value["project_id"], "task.project_id")
        except ValueError as exc:
            raise TaskBoundaryError(f"task.project_id is invalid ({exc}).") from exc

        role = value["role"]
        if not isinstance(role, str):
            raise TaskBoundaryError("task.role must be a string.")
        role_config = configuration.for_role(role)  # fail closed, no fallback

        target = _validate_ref(value["target"], project_id, "task.target", with_digest=False)

        input_refs = value["input_refs"]
        if not isinstance(input_refs, (list, tuple)):
            raise TaskBoundaryError("task.input_refs must be a list of exact references.")
        if len(input_refs) > MAX_INPUT_REFS:
            raise TaskBoundaryError(
                f"task.input_refs has more than {MAX_INPUT_REFS} entries."
            )
        refs: list[dict[str, str]] = []
        for index, entry in enumerate(input_refs):
            refs.append(
                _validate_ref(entry, project_id, f"task.input_refs[{index}]", with_digest=True)
            )

        scope_raw = value["scope"]
        if not isinstance(scope_raw, (list, tuple)):
            raise TaskBoundaryError("task.scope must be a list of scope tokens.")
        if len(scope_raw) > MAX_SCOPE_TOKENS:
            raise TaskBoundaryError(f"task.scope has more than {MAX_SCOPE_TOKENS} tokens.")
        scope: list[str] = []
        for index, token in enumerate(scope_raw):
            if not isinstance(token, str) or token == "" or token != token.strip():
                raise TaskBoundaryError(
                    f"task.scope[{index}] must be a non-empty string with no outer whitespace."
                )
            if len(token) > MAX_SCOPE_TOKEN_LEN:
                raise TaskBoundaryError(
                    f"task.scope[{index}] is longer than {MAX_SCOPE_TOKEN_LEN} characters."
                )
            scope.append(token)

        prompt_version = value["prompt_version"]
        if (
            not isinstance(prompt_version, str)
            or prompt_version == ""
            or prompt_version != prompt_version.strip()
            or any(ch.isspace() for ch in prompt_version)
            or len(prompt_version) > MAX_PROMPT_VERSION_LEN
        ):
            raise TaskBoundaryError(
                "task.prompt_version must be a non-empty, whitespace-free string "
                f"of at most {MAX_PROMPT_VERSION_LEN} characters."
            )

        size_limit = _require_plain_int(
            value["size_limit"], error_cls=TaskBoundaryError, field="task.size_limit"
        )
        if size_limit < 1 or size_limit > MAX_TASK_SIZE_BYTES:
            raise TaskBoundaryError(
                f"task.size_limit must be between 1 and {MAX_TASK_SIZE_BYTES} bytes."
            )

        step_limit = _require_plain_int(
            value["step_limit"], error_cls=TaskBoundaryError, field="task.step_limit"
        )
        if step_limit < 1 or step_limit > role_config.steps:
            raise TaskBoundaryError(
                f"task.step_limit must be between 1 and the configured {role!r} "
                f"limit of {role_config.steps} steps (got {step_limit})."
            )

        return cls(
            project_id=project_id,
            role=role,
            target=target,
            input_refs=tuple(refs),
            scope=tuple(scope),
            prompt_version=prompt_version,
            size_limit=size_limit,
            step_limit=step_limit,
            role_config=role_config,
        )


def _validate_ref(
    value: object,
    project_id: str,
    label: str,
    *,
    with_digest: bool,
) -> dict[str, str]:
    """Validate an exact same-project revision reference (with optional digest)."""
    if not isinstance(value, Mapping):
        raise TaskBoundaryError(f"{label} must be a reference object.")
    allowed = set(_INPUT_REF_KEYS) if with_digest else set(_REF_KEYS)
    unknown = [key for key in value if key not in allowed]
    if unknown:
        pinned = (
            "project_id/artifact_id/revision_id/digest"
            if with_digest
            else "project_id/artifact_id/revision_id"
        )
        raise TaskBoundaryError(
            f"{label} contains an unknown field; a reference pins only {pinned}."
        )
    for required in ("project_id", "artifact_id", "revision_id"):
        if required not in value:
            raise TaskBoundaryError(f"{label} is missing required field {required!r}.")
    try:
        ref_project = validate_identifier(value["project_id"], f"{label}.project_id")
        artifact_id = validate_identifier(value["artifact_id"], f"{label}.artifact_id")
        revision_id = validate_identifier(value["revision_id"], f"{label}.revision_id")
    except ValueError as exc:
        raise TaskBoundaryError(f"{label} is invalid ({exc}).") from exc
    if ref_project != project_id:
        raise TaskBoundaryError(
            f"{label} is a cross-project reference; it must belong to project "
            f"{project_id!r}."
        )
    ref = {
        "project_id": ref_project,
        "artifact_id": artifact_id,
        "revision_id": revision_id,
    }
    if with_digest:
        digest = value["digest"]
        if not isinstance(digest, str) or not _DIGEST_RE.match(digest):
            raise TaskBoundaryError(
                f"{label}.digest must be a lowercase hex SHA-256 digest."
            )
        ref["digest"] = digest
    return ref


# --------------------------------------------------------------------------- #
# Proposal-only interface (pure; non-authorizing)
# --------------------------------------------------------------------------- #

_PAYLOAD_KEYS = frozenset({"content"})

#: Keys that must never appear inside a proposal payload (top level or nested).
#: They spell authority, tool/permission control, budgets, gate/approval
#: decisions, state edits, or task-shape/widening fields — none of which a model
#: output may carry. Content keys are therefore restricted, on purpose and
#: fail-closed; the payload envelope is ``{"content": <data>}`` only.
_FORBIDDEN_CONTENT_KEYS = frozenset(
    _normalize_key(key)
    for key in (
        "actor",
        "actor_kind",
        "actor_id",
        "authority",
        "authorize",
        "authorise",
        "authorization",
        "auth",
        "authenticate",
        "authenticated",
        "owner_auth",
        "owner_auth_ref",
        "owner_authenticated",
        "approval",
        "approvals",
        "approved",
        "approve",
        "approver",
        "reject",
        "rejected",
        "decision",
        "gate",
        "gate_kind",
        "gate_id",
        "grant",
        "grants",
        "allow",
        "allowlist",
        "permission",
        "permissions",
        "tool",
        "tools",
        "tool_permission",
        "tool_permissions",
        "tool_use",
        "capability",
        "capabilities",
        "budget",
        "budget_usd",
        "reserve",
        "reserve_usd",
        "cost",
        "cost_usd",
        "spend",
        "spending",
        "override",
        "bypass",
        "widen",
        "widen_scope",
        "expand_scope",
        "scope_override",
        "supersede",
        "escalate",
        "self_approve",
        "self_approval",
        "edit",
        "write",
        "delete",
        "commit",
        "apply",
        "finalize",
        "mutate",
        "publish",
        "export",
        # Task-shape / widening fields: a model output cannot re-pin the task,
        # rename its role, or name a different model/provider in-band.
        "scope",
        "target",
        "input_refs",
        "refs",
        "project_id",
        "role",
        "task",
        "task_id",
        "model",
        "provider",
        "provider_id",
        "model_id",
        "variant",
        "prompt_version",
        "size_limit",
        "step_limit",
        "session",
        "session_id",
        "versions",
    )
)


@dataclass(frozen=True)
class Proposal:
    """A **non-authorizing** machine proposal about one exact revision.

    A proposal is opaque model data plus the trusted provenance the application
    attached. It has **no** authority field, no approval, and no way to widen the
    task, edit authoritative state, or grant a tool: it can only ever be input to
    a separate owner/human handler (out of scope here).
    """

    kind: str
    proposal_id: str
    role: str
    model: ModelRef
    target: Mapping[str, str]
    input_digest: str
    payload_digest: str
    content: Any


def submit_proposal(
    task: BoundedTask,
    payload: object,
    *,
    configuration: RoleConfiguration,
) -> Proposal:
    """Validate a payload and return a **non-authorizing** :class:`Proposal`.

    Pure function: no I/O, no state change, no approval. The payload envelope is
    exactly ``{"content": <data>}``; the model provenance is taken from the
    trusted configuration for the task's role, **never** from the payload. The
    content is deep-scanned for authority/tool/budget/state keys and bounded by
    the task's ``size_limit`` (canonical JSON bytes).
    """
    if not isinstance(task, BoundedTask):
        raise ProposalError("submit_proposal requires a validated BoundedTask.")
    if not isinstance(payload, Mapping):
        raise ProposalError("a proposal payload must be an object with a single 'content' field.")
    unknown = [key for key in payload if key not in _PAYLOAD_KEYS]
    if unknown:
        raise ProposalError(
            "a proposal payload may carry only 'content'; it cannot supply an "
            "actor, approval, authority, model, scope, or tool permission."
        )
    if "content" not in payload:
        raise ProposalError("a proposal payload must include a 'content' field.")

    content = payload["content"]
    _reject_forbidden_keys(content, "proposal content")

    encoded = _canonical_bytes(content, error_cls=ProposalError, label="proposal content")
    if len(encoded) > task.size_limit:
        raise ProposalError(
            f"proposal content is {len(encoded)} bytes, over the task size limit "
            f"of {task.size_limit}."
        )

    # Provenance and identity come from the trusted configuration + task, not the
    # payload: a model cannot name itself, another model, or another revision.
    model = configuration.for_role(task.role).model
    payload_digest = hashlib.sha256(encoded).hexdigest()
    input_digest = hashlib.sha256(
        _canonical_bytes(
            [dict(entry) for entry in task.input_refs],
            error_cls=ProposalError,
            label="proposal input refs",
        )
    ).hexdigest()
    identity = _canonical_bytes(
        {
            "project_id": task.project_id,
            "role": task.role,
            "target": dict(task.target),
            "model": model.ref,
            "input_digest": input_digest,
            "payload_digest": payload_digest,
        },
        error_cls=ProposalError,
        label="proposal identity",
    )
    proposal_id = hashlib.sha256(identity).hexdigest()

    return Proposal(
        kind=PROPOSAL_KIND,
        proposal_id=proposal_id,
        role=task.role,
        model=model,
        target=dict(task.target),
        input_digest=input_digest,
        payload_digest=payload_digest,
        content=content,
    )


def _reject_forbidden_keys(value: object, label: str) -> None:
    """Walk ``value`` and reject any nested key that tries to carry authority."""
    stack: list[tuple[object, str]] = [(value, label)]
    while stack:
        node, path = stack.pop()
        if isinstance(node, Mapping):
            for key, item in node.items():
                if isinstance(key, str) and _normalize_key(key) in _FORBIDDEN_CONTENT_KEYS:
                    raise ProposalError(
                        f"{path} contains a forbidden key ({key!r}); a proposal "
                        "may carry scientific data only and cannot approve, edit "
                        "state, set budgets, or grant tool permissions."
                    )
                stack.append((item, f"{path}.{key}"))
        elif isinstance(node, (list, tuple)):
            for index, item in enumerate(node):
                stack.append((item, f"{path}[{index}]"))


# --------------------------------------------------------------------------- #
# OpenCode V2 role configuration
# --------------------------------------------------------------------------- #


def build_opencode_config(configuration: RoleConfiguration) -> dict[str, Any]:
    """Build the OpenCode **V2** configuration for the dedicated scientific roles.

    The returned document is pure data (never written here). It uses only V2
    keys (``permissions``/``policies``/``agents``/``mcp``/``plugins``/``skills``),
    denies every built-in tool action for every role, hard-denies them again with
    ``tool.use`` policies, blocks inherited MCP servers/skills/plugins, and pins
    each role's explicit model and finite ``steps``. It sets **no** top-level
    ``model`` or ``default_agent``: the model is per-role and explicit.

    As documented in the module boundary note, these denies are a **model tool
    boundary**, not a same-UID OS sandbox.
    """
    permissions: list[dict[str, str]] = [{"action": "*", "resource": "*", "effect": "deny"}]
    permissions.extend(
        {"action": action, "resource": "*", "effect": "deny"}
        for action in OPENCODE_BUILTIN_TOOL_ACTIONS
    )

    policies: list[dict[str, str]] = [{"action": "tool.use", "resource": "*", "effect": "deny"}]
    policies.extend(
        {"action": "tool.use", "resource": resource, "effect": "deny"}
        for resource in OPENCODE_DENIED_TOOL_RESOURCES
    )
    policies.extend(
        {"action": "integration.use", "resource": resource, "effect": "deny"}
        for resource in OPENCODE_DENIED_INTEGRATIONS
    )

    agents: dict[str, Any] = {}
    for entry in sorted(configuration.roles, key=lambda item: item.role):
        agents[entry.role] = {
            "description": ROLE_DESCRIPTIONS[entry.role],
            "mode": OPENCODE_AGENT_MODE,
            "model": entry.model.to_config(),
            "system": _role_system_prompt(entry.role),
            "permissions": [{"action": "*", "resource": "*", "effect": "deny"}],
            "steps": entry.steps,
        }

    return {
        "$schema": OPENCODE_CONFIG_SCHEMA_URL,
        "permissions": permissions,
        "policies": policies,
        "agents": agents,
        "mcp": {"servers": {}},
        "plugins": [],
        "skills": [],
    }


def opencode_config_bytes(configuration: RoleConfiguration) -> bytes:
    """Canonical JSON bytes of :func:`build_opencode_config` (deterministic)."""
    return _canonical_bytes(
        build_opencode_config(configuration),
        error_cls=RoleConfigError,
        label="opencode configuration",
    )


def _role_system_prompt(role: str) -> str:
    return (
        f"You are the dedicated {role!r} scientific role "
        f"({ROLE_DESCRIPTIONS[role]}). You have no tools: do not read or write "
        "files, run shell commands, launch subagents, fetch the web, load "
        "skills, or execute code. Reply only with the typed, data-only proposal "
        "payload the application asks for. You cannot approve or reject "
        "anything, edit authoritative state, set a budget, or widen the task; "
        "those remain human and application responsibilities."
    )


__all__ = [
    "AgentRoleError",
    "BoundedTask",
    "CRITIC_REQUIRES_FRESH_CONTEXT",
    "CriticConfigError",
    "CriticPlan",
    "MAX_FACT_CHECK_ROUNDS",
    "MAX_INPUT_REFS",
    "MAX_PROMPT_VERSION_LEN",
    "MAX_ROLE_STEPS",
    "MAX_SCOPE_TOKENS",
    "MAX_SCOPE_TOKEN_LEN",
    "MAX_SECTION_CRITIQUE_ROUNDS",
    "MAX_TASK_SIZE_BYTES",
    "MAX_TASK_STEPS",
    "ModelRef",
    "OPENCODE_AGENT_MODE",
    "OPENCODE_BUILTIN_TOOL_ACTIONS",
    "OPENCODE_CONFIG_SCHEMA_URL",
    "OPENCODE_DENIED_INTEGRATIONS",
    "OPENCODE_DENIED_TOOL_RESOURCES",
    "OPENCODE_DEPRECATED_V1_KEYS",
    "PROPOSAL_IS_AUTHORITATIVE",
    "PROPOSAL_KIND",
    "Proposal",
    "ProposalError",
    "ROLE_CONFIG_SCHEMA_VERSION",
    "ROLE_DEFAULT_STEPS",
    "ROLE_DESCRIPTIONS",
    "RoleConfig",
    "RoleConfigError",
    "RoleConfiguration",
    "SCIENTIFIC_ROLES",
    "TaskBoundaryError",
    "build_opencode_config",
    "opencode_config_bytes",
    "submit_proposal",
]
