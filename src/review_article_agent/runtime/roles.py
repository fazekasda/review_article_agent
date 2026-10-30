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

A proposal is **always non-authoritative regardless of its text**. The payload
key denylist below is a strict *data-only envelope* aid, never a security proof:
nothing here turns model text into an approval, and no text can grant authority.

Hardening notes (repair R1)
---------------------------

* **Canonical, portable, bounded validation happens before any walk.** Every
  externally-supplied JSON value is passed through
  :func:`review_article_agent.domain.records.canonical_json` *first*; that
  routine rejects circular references, over-deep nesting (> 64), non-string
  keys, surrogates, and non-finite numbers, so a later structural walk is cheap
  and cannot loop. The walk carries its own visited/depth guard as defence in
  depth.
* **Every mandatory key yields a typed boundary error.** A missing reference
  digest is a :class:`TaskBoundaryError`, never a raw ``KeyError``.
* **Tasks are frozen and revalidated.** ``BoundedTask`` validates in
  ``__post_init__`` (so a direct constructor call cannot bypass validation) and
  stores immutable views; :func:`submit_proposal` rebuilds the task from its
  canonical form under the supplied configuration and requires exact equality,
  so a task can never be relabelled to another role or model.
* **No aliasing after hashing.** Reference maps and proposal content are frozen
  (read-only mappings and tuples), so a caller mutating its own object after
  submission cannot change a proposal's stored content or digests.
* **Static, redacted errors.** Errors raised from external input carry a
  value-free ``code`` and a constant message; they never echo a model id, role,
  key, path, value, or exception text.

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
from types import MappingProxyType
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
#:
#: The **generative model roles** of the direct OpenRouter route (kickoff 16 §5;
#: :data:`review_article_agent.runtime.generation.ROLES`) are part of this same
#: closed set: a generation task must be able to name its exact role, and the
#: stored non-authoritative proposal records that role. ``section_critic`` and
#: ``fact_checker`` already exist above and are shared; the remaining generation
#: roles are appended here.
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
    "screening",
    "synthesis",
    "framework",
    "outline",
    "blueprint",
    "section_writer",
    "section_repair",
    "global_reviewer",
    "global_writer",
    "notes_planner",
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

#: Maximum walk depth for the defence-in-depth content key scan. Independent of,
#: and no larger than, ``records``' canonical JSON depth bound.
MAX_CONTENT_DEPTH = 64

#: ``docs/specification.md`` line 38: a section critique/repair loop is at most
#: three rounds.
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
    # Generative model roles (kickoff 16 §5): a single bounded direct-call step
    # per durable request.
    "screening": 1,
    "synthesis": 1,
    "framework": 1,
    "outline": 1,
    "blueprint": 1,
    "section_writer": 1,
    "section_repair": 1,
    "global_reviewer": 1,
    "global_writer": 1,
    "notes_planner": 1,
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
    "screening": (
        "Classifies untrusted screening material and proposes inclusion or "
        "exclusion with reasons; never invents evidence."
    ),
    "synthesis": (
        "Proposes a grounded synthesis / evidence map from untrusted material; "
        "links every claim to its source and reports uncertainty."
    ),
    "framework": (
        "Proposes a review framework from untrusted material; never invents a "
        "claim."
    ),
    "outline": (
        "Proposes a review outline from untrusted material; never invents a "
        "claim."
    ),
    "blueprint": (
        "Proposes section blueprints from untrusted material; never invents a "
        "claim."
    ),
    "section_writer": (
        "Drafts the requested section from untrusted material; never invents a "
        "citation."
    ),
    "section_repair": (
        "Proposes a bounded repair of a section from critic findings; never "
        "invents a citation."
    ),
    "global_reviewer": (
        "Reviews the assembled manuscript for consistency and gaps; reports "
        "uncertainty and limitations."
    ),
    "global_writer": (
        "Proposes global edits or notes for the manuscript; never invents a "
        "citation."
    ),
    "notes_planner": (
        "Plans review notes from untrusted material; never invents a claim."
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
# Static, redacted error messages (never interpolate external input)
# --------------------------------------------------------------------------- #

_MSG_BAD_CONFIG = "A role configuration must be an object with schema_version and roles."
_MSG_CONFIG_UNKNOWN_FIELD = (
    "The role configuration contains a field that is not permitted; only "
    "'schema_version' and 'roles' are allowed."
)
_MSG_CONFIG_SCHEMA = "The role configuration schema_version is missing or unsupported."
_MSG_CONFIG_ROLES = "The role configuration 'roles' field must be an object of role entries."
_MSG_CONFIG_UNKNOWN_ROLE = (
    "The role configuration names a role that is not a known scientific role."
)
_MSG_CONFIG_ROLE_FIELD = "A role entry has a field that is not permitted."
_MSG_CONFIG_MODEL = (
    "A role entry must give an explicit provider_id and model_id (and only "
    "those plus variant/steps)."
)
_MSG_MODEL_TYPE = (
    "A role model must be a 'provider/model#variant' string or an object "
    "naming a provider and model."
)
_MSG_MODEL_REF = "A role model reference must be 'provider/model' with an optional '#variant'."
_MSG_MODEL_TOKEN = (
    "A model reference component must be a non-empty, whitespace-free, "
    "bounded string."
)
_MSG_STEPS = "Role steps must be a bounded positive integer."
_MSG_ROLES_TYPE = "A role set must be a tuple of role entries."
_MSG_ROLE_DUPLICATE = "A role is configured more than once."
_MSG_KNOWN_ROLE = "The requested role is not a known scientific role."
_MSG_ROLE_UNCONFIGURED = (
    "The requested role has no configured provider/model; there is no fallback."
)
_MSG_CRITIC_SAME = (
    "The section critic must be a different concrete provider/model than the writer; "
    "a different variant is not a different model."
)
_MSG_TASK_OBJECT = "A bounded task must be an object."
_MSG_TASK_UNKNOWN_FIELD = (
    "The task contains a field that is not permitted; a task is data only and cannot "
    "carry an actor, approval, authority, budget, or tool permission."
)
_MSG_TASK_MISSING_FIELD = "The task is missing a required field."
_MSG_TASK_ROLE = "The task role must be a string naming a configured scientific role."
_MSG_TASK_ID = "A task or artifact identifier is not a valid opaque identifier."
_MSG_TASK_REF = "A task reference must be an exact same-project revision reference."
_MSG_TASK_REF_FIELD = "A task reference contains a field that is not permitted."
_MSG_TASK_REF_MISSING = "A task reference is missing a required field (including its digest)."
_MSG_TASK_REF_DIGEST = "A task reference digest must be a lowercase hex SHA-256 digest."
_MSG_TASK_REF_CROSS_PROJECT = "A task reference must belong to the task's own project."
_MSG_TASK_REFS_TYPE = "Task input references must be a bounded list of exact references."
_MSG_TASK_SCOPE_TYPE = "Task scope must be a bounded list of non-empty tokens."
_MSG_TASK_SCOPE_TOKEN = "A task scope token must be a non-empty, whitespace-free, bounded string."
_MSG_TASK_PROMPT = "Task prompt_version must be a non-empty, whitespace-free, bounded string."
_MSG_TASK_SIZE = "Task size_limit must be a bounded positive integer."
_MSG_TASK_STEP = (
    "Task step_limit must be a bounded positive integer no larger than the "
    "role's step limit."
)
_MSG_TASK_ROLE_CONFIG = "The task's role configuration must match its role."
_MSG_PAYLOAD_OBJECT = "A proposal payload must be an object with a single 'content' field."
_MSG_PAYLOAD_UNKNOWN_FIELD = (
    "A proposal payload may carry only 'content'; it cannot supply an actor, approval, "
    "authority, model, scope, or tool permission."
)
_MSG_PAYLOAD_MISSING_CONTENT = "A proposal payload must include a 'content' field."
_MSG_PROPOSAL_TASK = "submit_proposal requires a validated BoundedTask."
_MSG_PROPOSAL_CONFIG = "submit_proposal requires a validated RoleConfiguration."
_MSG_PROPOSAL_TASK_DRIFT = (
    "The task does not match the supplied configuration; refusing to relabel it to "
    "another role or model."
)
_MSG_PROPOSAL_CONTENT_JSON = (
    "Proposal content must be canonical, portable, bounded JSON (no cycles, no "
    "over-deep nesting, no non-finite numbers, no non-string keys)."
)
_MSG_PROPOSAL_CONTENT_SIZE = "Proposal content is larger than the task size limit."
_MSG_PROPOSAL_CONTENT_KEY = (
    "Proposal content contains a key that is not permitted; a proposal may carry "
    "scientific data only and cannot approve, edit state, set budgets, grant tool "
    "permissions, or re-pin the task."
)
_MSG_PROPOSAL_CONTENT_SHAPE = "Proposal content nests too deeply or is circular."


def _wrap(error_cls: type["AgentRoleError"], exc: "AgentRoleError") -> "AgentRoleError":
    """Return a same-code error of ``error_cls`` carrying only a static message."""
    return error_cls(exc.code, str(exc))


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #


class AgentRoleError(ReviewAgentError):
    """Base class for every role/task/proposal boundary failure.

    ``code`` is a static, value-free identifier and the message is a constant, so
    an error raised from untrusted input never echoes a model id, role name, key,
    path, value, or exception text.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


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


def _canonical_bytes(
    value: object,
    *,
    error_cls: type[AgentRoleError],
    code: str,
    message: str,
) -> bytes:
    """Return canonical, portable, bounded JSON bytes or raise ``error_cls``.

    Runs first for any external value: it rejects cycles, over-deep nesting,
    non-string keys, surrogates, and non-finite numbers, and never echoes input.
    """
    try:
        return canonical_json(value)
    except ValueError as exc:  # RecordValidationError is a ValueError subclass
        raise error_cls(code, message) from exc


def _require_plain_int(
    value: object,
    *,
    error_cls: type[AgentRoleError],
    code: str,
    message: str,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise error_cls(code, message)
    return value


def _normalize_key(key: str) -> str:
    return key.strip().lower().replace("-", "").replace("_", "")


def _freeze_mapping(value: Mapping[str, str]) -> Mapping[str, str]:
    """Return a read-only view over a private copy of ``value`` (no aliasing)."""
    return MappingProxyType(dict(value))


def _deep_freeze(value: Any) -> Any:
    """Return an immutable copy of a validated JSON value (run only post-canonical)."""
    if isinstance(value, Mapping):
        return MappingProxyType({key: _deep_freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_deep_freeze(item) for item in value)
    return value


def _thaw(value: Any) -> Any:
    """Return a plain JSON-safe copy of a frozen value."""
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


def _valid_model_token(value: object, *, pattern: re.Pattern[str]) -> str:
    if not isinstance(value, str) or value == "":
        raise RoleConfigError("model_token", _MSG_MODEL_TOKEN)
    if value != value.strip() or any(ch.isspace() for ch in value):
        raise RoleConfigError("model_token", _MSG_MODEL_TOKEN)
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
        raise RoleConfigError("model_token", _MSG_MODEL_TOKEN)
    if len(value) > MAX_MODEL_TOKEN_LEN:
        raise RoleConfigError("model_token", _MSG_MODEL_TOKEN)
    if not pattern.match(value):
        raise RoleConfigError("model_token", _MSG_MODEL_TOKEN)
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
            self, "provider_id", _valid_model_token(self.provider_id, pattern=_PROVIDER_RE)
        )
        object.__setattr__(
            self, "model_id", _valid_model_token(self.model_id, pattern=_MODEL_RE)
        )
        if self.variant is not None:
            object.__setattr__(
                self, "variant", _valid_model_token(self.variant, pattern=_VARIANT_RE)
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
        present in the input. Any failure is a redacted, typed error.
        """
        if isinstance(value, str):
            return cls._parse_ref_string(value, error_cls=error_cls)
        if isinstance(value, Mapping):
            allowed = {"provider_id", "model_id", "variant"}
            for key in value:
                if not isinstance(key, str) or key not in allowed:
                    raise error_cls("model_field", _MSG_CONFIG_ROLE_FIELD)
            if "provider_id" not in value or "model_id" not in value:
                raise error_cls("model_missing", _MSG_CONFIG_MODEL)
            try:
                return cls(
                    provider_id=value["provider_id"],
                    model_id=value["model_id"],
                    variant=value.get("variant"),
                )
            except RoleConfigError as exc:
                raise _wrap(error_cls, exc) from exc
        raise error_cls("model_type", _MSG_MODEL_TYPE)

    @classmethod
    def _parse_ref_string(cls, value: str, *, error_cls: type[AgentRoleError]) -> "ModelRef":
        if value == "" or value != value.strip():
            raise error_cls("model_ref", _MSG_MODEL_REF)
        variant: str | None = None
        base = value
        if "#" in value:
            base, _, variant = value.partition("#")
            if variant == "":
                raise error_cls("model_ref", _MSG_MODEL_REF)
        provider, sep, model = base.partition("/")
        if sep == "" or provider == "" or model == "":
            raise error_cls("model_ref", _MSG_MODEL_REF)
        try:
            return cls(provider_id=provider, model_id=model, variant=variant)
        except RoleConfigError as exc:
            raise _wrap(error_cls, exc) from exc


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
        if not isinstance(self.role, str) or self.role not in SCIENTIFIC_ROLES:
            raise RoleConfigError("unknown_role", _MSG_KNOWN_ROLE)
        if not isinstance(self.model, ModelRef):
            raise RoleConfigError("model_type", _MSG_MODEL_TYPE)
        steps = _require_plain_int(
            self.steps, error_cls=RoleConfigError, code="steps", message=_MSG_STEPS
        )
        if steps < 1 or steps > MAX_ROLE_STEPS:
            raise RoleConfigError("steps", _MSG_STEPS)


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
            raise RoleConfigError("roles_type", _MSG_ROLES_TYPE)
        seen: set[str] = set()
        for entry in self.roles:
            if not isinstance(entry, RoleConfig):
                raise RoleConfigError("roles_type", _MSG_ROLES_TYPE)
            if entry.role in seen:
                raise RoleConfigError("role_duplicate", _MSG_ROLE_DUPLICATE)
            seen.add(entry.role)

    @property
    def role_names(self) -> tuple[str, ...]:
        return tuple(entry.role for entry in self.roles)

    def for_role(self, role: str) -> RoleConfig:
        """Return the configured role, or fail closed (never a fallback)."""
        if not isinstance(role, str) or role not in SCIENTIFIC_ROLES:
            raise RoleConfigError("unknown_role", _MSG_KNOWN_ROLE)
        for entry in self.roles:
            if entry.role == role:
                return entry
        raise RoleConfigError("role_unconfigured", _MSG_ROLE_UNCONFIGURED)

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
            raise CriticConfigError("critic_same_model", _MSG_CRITIC_SAME)
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
        schema version are all rejected with a redacted error.
        """
        if not isinstance(value, Mapping):
            raise RoleConfigError("config_type", _MSG_BAD_CONFIG)
        for key in value:
            if not isinstance(key, str) or key not in {"schema_version", "roles"}:
                raise RoleConfigError("config_field", _MSG_CONFIG_UNKNOWN_FIELD)
        if "schema_version" not in value:
            raise RoleConfigError("config_schema", _MSG_CONFIG_SCHEMA)
        version = _require_plain_int(
            value["schema_version"],
            error_cls=RoleConfigError,
            code="config_schema",
            message=_MSG_CONFIG_SCHEMA,
        )
        if version != ROLE_CONFIG_SCHEMA_VERSION:
            raise RoleConfigError("config_schema", _MSG_CONFIG_SCHEMA)
        roles_raw = value.get("roles")
        if not isinstance(roles_raw, Mapping):
            raise RoleConfigError("config_roles", _MSG_CONFIG_ROLES)
        entries: list[RoleConfig] = []
        for role, spec in roles_raw.items():
            if not isinstance(role, str) or role not in SCIENTIFIC_ROLES:
                raise RoleConfigError("unknown_role", _MSG_CONFIG_UNKNOWN_ROLE)
            if isinstance(spec, Mapping):
                for key in spec:
                    if not isinstance(key, str) or key not in {
                        "provider_id",
                        "model_id",
                        "variant",
                        "steps",
                    }:
                        raise RoleConfigError("role_field", _MSG_CONFIG_ROLE_FIELD)
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
                    steps_raw,
                    error_cls=RoleConfigError,
                    code="steps",
                    message=_MSG_STEPS,
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

_TASK_KEYS = (
    "project_id",
    "role",
    "target",
    "input_refs",
    "scope",
    "prompt_version",
    "size_limit",
    "step_limit",
)
_TASK_KEY_SET = frozenset(_TASK_KEYS)
_REF_KEYS = ("project_id", "artifact_id", "revision_id")
_INPUT_REF_KEYS = ("project_id", "artifact_id", "revision_id", "digest")


def _freeze_ref(
    value: object,
    project_id: str,
    *,
    with_digest: bool,
) -> Mapping[str, str]:
    """Validate and freeze one exact same-project revision reference."""
    if not isinstance(value, Mapping):
        raise TaskBoundaryError("task_ref", _MSG_TASK_REF)
    allowed = set(_INPUT_REF_KEYS) if with_digest else set(_REF_KEYS)
    for key in value:
        if not isinstance(key, str) or key not in allowed:
            raise TaskBoundaryError("task_ref_field", _MSG_TASK_REF_FIELD)
    required = list(_INPUT_REF_KEYS) if with_digest else list(_REF_KEYS)
    for key in required:
        if key not in value:  # includes the digest: a missing digest is typed, not a KeyError
            raise TaskBoundaryError("task_ref_missing", _MSG_TASK_REF_MISSING)
    try:
        ref_project = validate_identifier(value["project_id"], "project_id")
        artifact_id = validate_identifier(value["artifact_id"], "artifact_id")
        revision_id = validate_identifier(value["revision_id"], "revision_id")
    except ValueError as exc:
        raise TaskBoundaryError("task_id", _MSG_TASK_ID) from exc
    if ref_project != project_id:
        raise TaskBoundaryError("task_ref_cross_project", _MSG_TASK_REF_CROSS_PROJECT)
    ref: dict[str, str] = {
        "project_id": ref_project,
        "artifact_id": artifact_id,
        "revision_id": revision_id,
    }
    if with_digest:
        digest = value["digest"]
        if not isinstance(digest, str) or not _DIGEST_RE.match(digest):
            raise TaskBoundaryError("task_ref_digest", _MSG_TASK_REF_DIGEST)
        ref["digest"] = digest
    return _freeze_mapping(ref)


@dataclass(frozen=True)
class BoundedTask:
    """A strictly-bounded, data-only task pinned to one project, role, and revision.

    A task is **input data**, never a request: it cannot carry an actor, an
    approval, authority, a budget, or tool permissions. Every field is pinned by
    the application (not the model): the exact project, the exact target
    revision, the exact input revision refs and their canonical digests, the
    explicit scope, prompt version, size limit, and step limit.

    ``__post_init__`` validates and freezes the instance, so a **direct
    constructor call cannot bypass validation**, and reference maps are read-only
    views (``task.target["project_id"] = ...`` raises ``TypeError``).
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

    def __post_init__(self) -> None:
        if not isinstance(self.role, str) or self.role not in SCIENTIFIC_ROLES:
            raise TaskBoundaryError("task_role", _MSG_TASK_ROLE)
        if not isinstance(self.role_config, RoleConfig) or self.role_config.role != self.role:
            raise TaskBoundaryError("task_role_config", _MSG_TASK_ROLE_CONFIG)
        try:
            project_id = validate_identifier(self.project_id, "project_id")
        except ValueError as exc:
            raise TaskBoundaryError("task_id", _MSG_TASK_ID) from exc

        target = _freeze_ref(self.target, project_id, with_digest=False)

        refs = self.input_refs
        if not isinstance(refs, (list, tuple)):
            raise TaskBoundaryError("task_refs_type", _MSG_TASK_REFS_TYPE)
        if len(refs) > MAX_INPUT_REFS:
            raise TaskBoundaryError("task_refs_type", _MSG_TASK_REFS_TYPE)
        frozen_refs = tuple(
            _freeze_ref(entry, project_id, with_digest=True) for entry in refs
        )

        scope_raw = self.scope
        if not isinstance(scope_raw, (list, tuple)):
            raise TaskBoundaryError("task_scope_type", _MSG_TASK_SCOPE_TYPE)
        if len(scope_raw) > MAX_SCOPE_TOKENS:
            raise TaskBoundaryError("task_scope_type", _MSG_TASK_SCOPE_TYPE)
        scope: list[str] = []
        for token in scope_raw:
            if (
                not isinstance(token, str)
                or token == ""
                or token != token.strip()
                or len(token) > MAX_SCOPE_TOKEN_LEN
            ):
                raise TaskBoundaryError("task_scope_token", _MSG_TASK_SCOPE_TOKEN)
            scope.append(token)

        prompt_version = self.prompt_version
        if (
            not isinstance(prompt_version, str)
            or prompt_version == ""
            or prompt_version != prompt_version.strip()
            or any(ch.isspace() for ch in prompt_version)
            or len(prompt_version) > MAX_PROMPT_VERSION_LEN
        ):
            raise TaskBoundaryError("task_prompt", _MSG_TASK_PROMPT)

        size_limit = _require_plain_int(
            self.size_limit, error_cls=TaskBoundaryError, code="task_size", message=_MSG_TASK_SIZE
        )
        if size_limit < 1 or size_limit > MAX_TASK_SIZE_BYTES:
            raise TaskBoundaryError("task_size", _MSG_TASK_SIZE)

        step_limit = _require_plain_int(
            self.step_limit, error_cls=TaskBoundaryError, code="task_step", message=_MSG_TASK_STEP
        )
        if step_limit < 1 or step_limit > self.role_config.steps:
            raise TaskBoundaryError("task_step", _MSG_TASK_STEP)

        object.__setattr__(self, "project_id", project_id)
        object.__setattr__(self, "target", target)
        object.__setattr__(self, "input_refs", frozen_refs)
        object.__setattr__(self, "scope", tuple(scope))
        object.__setattr__(self, "prompt_version", prompt_version)
        object.__setattr__(self, "size_limit", size_limit)
        object.__setattr__(self, "step_limit", step_limit)

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

    def fingerprint_mapping(self) -> dict[str, Any]:
        """The full task identity: pins, scope/prompt versions, and role config."""
        mapping = self.to_mapping()
        mapping["role_config"] = {
            "provider_id": self.role_config.model.provider_id,
            "model_id": self.role_config.model.model_id,
            "variant": self.role_config.model.variant,
            "steps": self.role_config.steps,
        }
        return mapping

    def fingerprint(self) -> str:
        """Canonical SHA-256 over the full pinned task (not just the input refs)."""
        return hashlib.sha256(
            _canonical_bytes(
                self.fingerprint_mapping(),
                error_cls=TaskBoundaryError,
                code="task_fingerprint",
                message=_MSG_TASK_OBJECT,
            )
        ).hexdigest()

    def canonical_bytes(self) -> bytes:
        return _canonical_bytes(
            self.to_mapping(),
            error_cls=TaskBoundaryError,
            code="task_canonical",
            message=_MSG_TASK_OBJECT,
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
        if not isinstance(configuration, RoleConfiguration):
            raise TaskBoundaryError("task_config", _MSG_PROPOSAL_CONFIG)
        if not isinstance(value, Mapping):
            raise TaskBoundaryError("task_object", _MSG_TASK_OBJECT)
        for key in value:
            if not isinstance(key, str) or key not in _TASK_KEY_SET:
                raise TaskBoundaryError("task_field", _MSG_TASK_UNKNOWN_FIELD)
        for required in _TASK_KEYS:
            if required not in value:
                raise TaskBoundaryError("task_missing", _MSG_TASK_MISSING_FIELD)
        role = value["role"]
        if not isinstance(role, str):
            raise TaskBoundaryError("task_role", _MSG_TASK_ROLE)
        role_config = configuration.for_role(role)
        return cls(
            project_id=value["project_id"],
            role=role,
            target=value["target"],
            input_refs=value["input_refs"],
            scope=value["scope"],
            prompt_version=value["prompt_version"],
            size_limit=value["size_limit"],
            step_limit=value["step_limit"],
            role_config=role_config,
        )


# --------------------------------------------------------------------------- #
# Proposal-only interface (pure; non-authorizing)
# --------------------------------------------------------------------------- #

_PAYLOAD_KEY_SET = frozenset({"content"})

#: Keys that must never appear inside a proposal payload (top level or nested).
#: They spell authority, tool/permission control, budgets, gate/approval
#: decisions, state edits, or task-shape/widening fields. This is a strict
#: *data-only envelope* aid, **not** a security proof: a proposal is
#: non-authoritative regardless of its text. Content keys are therefore
#: restricted on purpose and fail-closed; the envelope is ``{"content": <data>}``.
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
    a separate owner/human handler (out of scope here). ``content`` and ref maps
    are frozen, so no post-hash mutation is possible.
    """

    kind: str
    proposal_id: str
    role: str
    model: ModelRef
    target: Mapping[str, str]
    task_digest: str
    input_digest: str
    payload_digest: str
    content: Any

    def to_mapping(self) -> dict[str, Any]:
        """A deterministic, JSON-safe view (authority-free)."""
        return {
            "kind": self.kind,
            "proposal_id": self.proposal_id,
            "role": self.role,
            "model": {
                "provider_id": self.model.provider_id,
                "model_id": self.model.model_id,
                "variant": self.model.variant,
            },
            "target": dict(self.target),
            "task_digest": self.task_digest,
            "input_digest": self.input_digest,
            "payload_digest": self.payload_digest,
            "content": _thaw(self.content),
        }


def submit_proposal(
    task: BoundedTask,
    payload: object,
    *,
    configuration: RoleConfiguration,
) -> Proposal:
    """Validate a payload and return a **non-authorizing** :class:`Proposal`.

    Pure function: no I/O, no state change, no approval. The payload envelope is
    exactly ``{"content": <data>}``; the model provenance is taken from the task's
    trusted role configuration, which must exactly match the supplied
    configuration. The content is canonical-validated and bounded *before* any
    structural walk, then deep-frozen.
    """
    if not isinstance(configuration, RoleConfiguration):
        raise ProposalError("invalid_configuration", _MSG_PROPOSAL_CONFIG)
    if not isinstance(task, BoundedTask):
        raise ProposalError("invalid_task", _MSG_PROPOSAL_TASK)

    # Rebuild the task from its canonical form under this configuration and
    # require exact equality: this re-validates a directly-constructed task and
    # refuses to relabel an earlier task to another role or model.
    try:
        canonical_task = BoundedTask.from_mapping(task.to_mapping(), configuration=configuration)
    except AgentRoleError as exc:
        raise _wrap(ProposalError, exc) from exc
    if canonical_task != task:
        raise ProposalError("task_drift", _MSG_PROPOSAL_TASK_DRIFT)
    task = canonical_task

    if not isinstance(payload, Mapping):
        raise ProposalError("payload_type", _MSG_PAYLOAD_OBJECT)
    for key in payload:
        if not isinstance(key, str) or key not in _PAYLOAD_KEY_SET:
            raise ProposalError("payload_field", _MSG_PAYLOAD_UNKNOWN_FIELD)
    if "content" not in payload:
        raise ProposalError("payload_content", _MSG_PAYLOAD_MISSING_CONTENT)

    content = payload["content"]
    # Canonical, portable, bounded validation FIRST (rejects cycles, over-deep
    # nesting, non-finite numbers, non-string keys).
    encoded = _canonical_bytes(
        content,
        error_cls=ProposalError,
        code="content_json",
        message=_MSG_PROPOSAL_CONTENT_JSON,
    )
    if len(encoded) > task.size_limit:
        raise ProposalError("content_size", _MSG_PROPOSAL_CONTENT_SIZE)
    # Only now is a structural walk cheap and guaranteed to terminate.
    _reject_forbidden_keys(content)

    model = task.role_config.model  # trusted provenance; never in-band
    payload_digest = hashlib.sha256(encoded).hexdigest()
    task_digest = task.fingerprint()
    input_digest = hashlib.sha256(
        _canonical_bytes(
            [dict(entry) for entry in task.input_refs],
            error_cls=ProposalError,
            code="content_json",
            message=_MSG_PROPOSAL_CONTENT_JSON,
        )
    ).hexdigest()
    identity = _canonical_bytes(
        {
            "project_id": task.project_id,
            "role": task.role,
            "target": dict(task.target),
            "model": model.ref,
            "task_digest": task_digest,
            "input_digest": input_digest,
            "payload_digest": payload_digest,
        },
        error_cls=ProposalError,
        code="content_json",
        message=_MSG_PROPOSAL_CONTENT_JSON,
    )
    proposal_id = hashlib.sha256(identity).hexdigest()

    return Proposal(
        kind=PROPOSAL_KIND,
        proposal_id=proposal_id,
        role=task.role,
        model=model,
        target=task.target,
        task_digest=task_digest,
        input_digest=input_digest,
        payload_digest=payload_digest,
        content=_deep_freeze(content),
    )


def _reject_forbidden_keys(value: object) -> None:
    """Reject a data-only envelope violation; bounded and cycle-safe by design.

    Run only after canonical validation, so the value is acyclic and depth-bounded
    (<= 64). The visited set and depth counter are defence in depth.
    """
    stack: list[tuple[object, int]] = [(value, 0)]
    seen: set[int] = set()
    while stack:
        node, depth = stack.pop()
        if depth > MAX_CONTENT_DEPTH:
            raise ProposalError("content_shape", _MSG_PROPOSAL_CONTENT_SHAPE)
        if isinstance(node, Mapping):
            if id(node) in seen:
                raise ProposalError("content_shape", _MSG_PROPOSAL_CONTENT_SHAPE)
            seen.add(id(node))
            for key, item in node.items():
                if isinstance(key, str) and _normalize_key(key) in _FORBIDDEN_CONTENT_KEYS:
                    raise ProposalError("content_key", _MSG_PROPOSAL_CONTENT_KEY)
                stack.append((item, depth + 1))
        elif isinstance(node, (list, tuple)):
            if id(node) in seen:
                raise ProposalError("content_shape", _MSG_PROPOSAL_CONTENT_SHAPE)
            seen.add(id(node))
            for item in node:
                stack.append((item, depth + 1))


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
    if not isinstance(configuration, RoleConfiguration):
        raise RoleConfigError("config_type", _MSG_BAD_CONFIG)

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
        code="config_canonical",
        message=_MSG_BAD_CONFIG,
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
    "MAX_CONTENT_DEPTH",
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
