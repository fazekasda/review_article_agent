"""Acceptance tests for the pure, fail-closed scientific role boundary (task T2).

Covers ``docs/implementation/04-agent-runtime.md`` deliverables 2/3 and
acceptance lines 17-19, ``docs/specification.md`` lines 20/38/39 and 52-63,
``docs/contracts/02-gates-and-runtime.md`` §1/§2/§6, and ADR ``001``:

* injection attempts in a proposal payload cannot approve, widen, or bypass
  tools;
* a generic tool/permission payload cannot become an approval;
* an invalid or same-model section critic (including a variant-only difference)
  is refused, and the critic needs a fresh context;
* explicit role configuration has no fallback (no default generation model);
* bounded refs and project isolation hold;
* the emitted OpenCode **V2** config has broad deny rules and the proposal
  interface stays proposal-only.

No model, provider, network, filesystem, or paid call is exercised here.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from review_article_agent.domain.records import canonical_json  # noqa: E402
from review_article_agent.runtime import roles  # noqa: E402
from review_article_agent.runtime.roles import (  # noqa: E402
    BoundedTask,
    CriticConfigError,
    ModelRef,
    ProposalError,
    RoleConfigError,
    RoleConfiguration,
    TaskBoundaryError,
    build_opencode_config,
    submit_proposal,
)


def _role_model(provider="openrouter", model="deepseek/deepseek-v4.1-flash", variant=None):
    spec = {"provider_id": provider, "model_id": model}
    if variant is not None:
        spec["variant"] = variant
    return spec


def _full_roles(writer=None, section_critic=None):
    writer_spec = writer if writer is not None else {
        "provider_id": "openrouter",
        "model_id": "anthropic/claude-sonnet-5-5",
        "variant": "high",
    }
    critic_spec = section_critic if section_critic is not None else {
        "provider_id": "openrouter",
        "model_id": "openai/gpt-6.1-sol",
        "variant": "high",
    }
    return {
        "lead": _role_model(variant="high"),
        "search": _role_model(),
        "extractor": _role_model(),
        "synthesizer": _role_model(),
        "writer": writer_spec,
        "section_critic": critic_spec,
        "fact_checker": {"provider_id": "openrouter", "model_id": "openai/gpt-6.1-sol"},
        "manuscript_reviewer": {"provider_id": "openrouter", "model_id": "openai/gpt-6.1-sol"},
        "origin_tracer": _role_model(),
    }


_WRITER_MODEL = {"provider_id": "openrouter", "model_id": "anthropic/claude-sonnet-5-5"}
_CRITIC_MODEL = {"provider_id": "openrouter", "model_id": "openai/gpt-6.1-sol"}


def _single_role_config(role, model):
    return RoleConfiguration.from_mapping(
        {"schema_version": 1, "roles": {role: model}}
    )


def _config(writer=None, section_critic=None):
    return RoleConfiguration.from_mapping(
        {"schema_version": 1, "roles": _full_roles(writer, section_critic)}
    )


def _task_mapping(**overrides):
    task = {
        "project_id": "prj_demo",
        "role": "writer",
        "target": {"project_id": "prj_demo", "artifact_id": "art_draft", "revision_id": "rev_1"},
        "input_refs": [
            {
                "project_id": "prj_demo",
                "artifact_id": "art_source",
                "revision_id": "rev_9",
                "digest": "a" * 64,
            }
        ],
        "scope": ["section:introduction", "paragraph:1"],
        "prompt_version": "writer.v1",
        "size_limit": 4096,
        "step_limit": 4,
    }
    task.update(overrides)
    return task


def _task(config=None, **overrides):
    return BoundedTask.from_mapping(
        _task_mapping(**overrides), configuration=config or _config()
    )


def _all_dict_keys(value):
    keys = set()
    stack = [value]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            for key, item in node.items():
                keys.add(key)
                stack.append(item)
        elif isinstance(node, list):
            stack.extend(node)
    return keys


class RoleVocabularyTests(unittest.TestCase):
    def test_role_set_is_explicit_and_finite(self):
        self.assertIsInstance(roles.SCIENTIFIC_ROLES, tuple)
        self.assertEqual(len(roles.SCIENTIFIC_ROLES), len(set(roles.SCIENTIFIC_ROLES)))
        for required in (
            "lead",
            "search",
            "extractor",
            "writer",
            "section_critic",
            "fact_checker",
        ):
            self.assertIn(required, roles.SCIENTIFIC_ROLES)
        # Every role has a bounded default step limit within the hard bound.
        for role in roles.SCIENTIFIC_ROLES:
            self.assertGreaterEqual(roles.ROLE_DEFAULT_STEPS[role], 1)
            self.assertLessEqual(roles.ROLE_DEFAULT_STEPS[role], roles.MAX_ROLE_STEPS)

    def test_module_declares_no_default_generation_model(self):
        self.assertFalse(hasattr(roles, "DEFAULT_MODEL"))
        self.assertFalse(hasattr(roles, "DEFAULT_PROVIDER"))
        for name in dir(roles):
            self.assertNotEqual(name.upper(), "DEFAULT_MODEL")


class ModelRefTests(unittest.TestCase):
    def test_parses_and_formats_ref_string_with_variant(self):
        ref = ModelRef.from_mapping("openrouter/anthropic/claude-sonnet-5-5#high")
        self.assertEqual(ref.provider_id, "openrouter")
        self.assertEqual(ref.model_id, "anthropic/claude-sonnet-5-5")
        self.assertEqual(ref.variant, "high")
        self.assertEqual(ref.ref, "openrouter/anthropic/claude-sonnet-5-5#high")
        self.assertEqual(
            ref.to_config(),
            {
                "providerID": "openrouter",
                "model": "anthropic/claude-sonnet-5-5",
                "variant": "high",
            },
        )

    def test_object_form_and_variant_optional(self):
        ref = ModelRef.from_mapping({"provider_id": "typesafe", "model_id": "jev-1.14"})
        self.assertIsNone(ref.variant)
        self.assertEqual(ref.ref, "typesafe/jev-1.14")
        self.assertEqual(ref.to_config(), {"providerID": "typesafe", "model": "jev-1.14"})

    def test_rejects_bare_model_name_and_empty_parts(self):
        for bad in ("claude-sonnet-5-5", "/model", "provider/", "", "provider/model#"):
            with self.assertRaises(RoleConfigError):
                ModelRef.from_mapping(bad)

    def test_rejects_whitespace_and_control_characters(self):
        with self.assertRaises(RoleConfigError):
            ModelRef.from_mapping("open router/model")
        with self.assertRaises(RoleConfigError):
            ModelRef.from_mapping({"provider_id": "openrouter", "model_id": "bad\nmodel"})

    def test_same_model_ignores_variant_but_compares_provider_and_id(self):
        base = ModelRef("openrouter", "openai/gpt-6.1-sol", "high")
        variant_only = ModelRef("openrouter", "openai/gpt-6.1-sol", "low")
        other_provider = ModelRef("other", "openai/gpt-6.1-sol", "high")
        other_id = ModelRef("openrouter", "openai/gpt-6.1-sol-2", "high")
        self.assertTrue(base.same_model(variant_only))
        self.assertFalse(base.same_model(other_provider))
        self.assertFalse(base.same_model(other_id))


class RoleConfigurationTests(unittest.TestCase):
    def test_requires_explicit_provider_and_model(self):
        for bad_spec in ({"provider_id": "openrouter"}, {"model_id": "m"}, {}):
            with self.assertRaises(RoleConfigError):
                RoleConfiguration.from_mapping(
                    {"schema_version": 1, "roles": {"writer": bad_spec}}
                )

    def test_rejects_unknown_role(self):
        with self.assertRaises(RoleConfigError):
            RoleConfiguration.from_mapping(
                {"schema_version": 1, "roles": {"mystery_role": _role_model()}}
            )

    def test_rejects_unknown_top_level_and_role_fields(self):
        with self.assertRaises(RoleConfigError):
            RoleConfiguration.from_mapping(
                {"schema_version": 1, "roles": _full_roles(), "extra": True}
            )
        with self.assertRaises(RoleConfigError):
            RoleConfiguration.from_mapping(
                {
                    "schema_version": 1,
                    "roles": {
                        "writer": {
                            "provider_id": "p",
                            "model_id": "m",
                            "temperature": 0.2,
                        }
                    },
                }
            )

    def test_rejects_bad_schema_version(self):
        with self.assertRaises(RoleConfigError):
            RoleConfiguration.from_mapping({"roles": _full_roles()})
        with self.assertRaises(RoleConfigError):
            RoleConfiguration.from_mapping({"schema_version": 2, "roles": _full_roles()})

    def test_rejects_unbounded_steps(self):
        for steps in (0, -1, roles.MAX_ROLE_STEPS + 1, True, "8"):
            roles_raw = _full_roles()
            roles_raw["writer"] = dict(roles_raw["writer"], steps=steps)
            with self.assertRaises(RoleConfigError):
                RoleConfiguration.from_mapping({"schema_version": 1, "roles": roles_raw})

    def test_no_fallback_for_unconfigured_role(self):
        partial = _single_role_config("writer", _WRITER_MODEL)
        # The configured role resolves...
        self.assertEqual(partial.for_role("writer").model.model_id, "anthropic/claude-sonnet-5-5")
        # ...and an unconfigured or unknown role fails closed, never borrowing one.
        with self.assertRaises(RoleConfigError):
            partial.for_role("section_critic")
        with self.assertRaises(RoleConfigError):
            partial.for_role("not_a_role")

    def test_duplicate_role_rejected(self):
        entry = roles.RoleConfig("writer", ModelRef("openrouter", "m"), 4)
        with self.assertRaises(RoleConfigError):
            RoleConfiguration(roles=(entry, entry))

    def test_as_mapping_round_trips(self):
        config = _config()
        again = RoleConfiguration.from_mapping(config.as_mapping())
        self.assertEqual(set(again.role_names), set(config.role_names))
        for role in config.role_names:
            self.assertEqual(
                again.for_role(role).model.ref, config.for_role(role).model.ref
            )


class SectionCriticTests(unittest.TestCase):
    def test_distinct_model_and_fresh_context_plan(self):
        plan = _config().validate_section_critic()
        self.assertNotEqual(plan.critic.model.ref, plan.writer.model.ref)
        self.assertFalse(plan.critic.model.same_model(plan.writer.model))
        self.assertTrue(plan.fresh_context)
        self.assertIs(roles.CRITIC_REQUIRES_FRESH_CONTEXT, True)
        self.assertEqual(plan.max_rounds, roles.MAX_SECTION_CRITIQUE_ROUNDS)
        self.assertEqual(plan.max_rounds, 3)

    def test_same_provider_and_id_rejected_even_with_different_variant(self):
        writer = {
            "provider_id": "openrouter",
            "model_id": "openai/gpt-6.1-sol",
            "variant": "high",
        }
        critic = {
            "provider_id": "openrouter",
            "model_id": "openai/gpt-6.1-sol",
            "variant": "low",
        }
        with self.assertRaises(CriticConfigError):
            _config(writer=writer, section_critic=critic).validate_section_critic()

    def test_same_model_rejected(self):
        same = {"provider_id": "openrouter", "model_id": "openai/gpt-6.1-sol", "variant": "high"}
        with self.assertRaises(CriticConfigError):
            _config(writer=same, section_critic=dict(same)).validate_section_critic()

    def test_different_provider_same_id_accepted(self):
        writer = {"provider_id": "openrouter", "model_id": "openai/gpt-6.1-sol"}
        critic = {"provider_id": "openai-direct", "model_id": "openai/gpt-6.1-sol"}
        plan = _config(writer=writer, section_critic=critic).validate_section_critic()
        self.assertEqual(plan.critic.model.provider_id, "openai-direct")

    def test_missing_critic_or_writer_refused(self):
        only_writer = _single_role_config("writer", _WRITER_MODEL)
        with self.assertRaises(RoleConfigError):
            only_writer.validate_section_critic()
        only_critic = _single_role_config("section_critic", _CRITIC_MODEL)
        with self.assertRaises(RoleConfigError):
            only_critic.validate_section_critic()


class BoundedTaskTests(unittest.TestCase):
    def test_valid_task_pins_everything(self):
        config = _config()
        task = _task(config)
        self.assertEqual(task.project_id, "prj_demo")
        self.assertEqual(task.role, "writer")
        self.assertEqual(task.target["revision_id"], "rev_1")
        self.assertEqual(task.input_refs[0]["digest"], "a" * 64)
        self.assertEqual(task.scope, ("section:introduction", "paragraph:1"))
        self.assertEqual(task.prompt_version, "writer.v1")
        self.assertEqual(task.size_limit, 4096)
        self.assertEqual(task.step_limit, 4)
        self.assertEqual(task.role_config, config.for_role("writer"))

    def test_canonical_bytes_are_deterministic(self):
        task = _task()
        self.assertEqual(task.canonical_bytes(), task.canonical_bytes())
        self.assertEqual(task.canonical_bytes(), canonical_json(task.to_mapping()))

    def test_rejects_non_object(self):
        with self.assertRaises(TaskBoundaryError):
            BoundedTask.from_mapping(["not", "an", "object"], configuration=_config())

    def test_rejects_authority_actor_approval_budget_tool_keys(self):
        for key, value in (
            ("actor", {"actor_kind": "human"}),
            ("approval", {"decision": "approved"}),
            ("authority", "owner_authenticated"),
            ("owner_auth_ref", "oa_1"),
            ("budget", {"budget_usd": "25"}),
            ("budget_usd", "25"),
            ("permissions", [{"action": "*", "effect": "allow"}]),
            ("tools", ["shell"]),
            ("tool_permissions", {"shell": "allow"}),
            ("approved", True),
        ):
            with self.assertRaises(TaskBoundaryError, msg=key):
                BoundedTask.from_mapping(
                    _task_mapping(**{key: value}), configuration=_config()
                )

    def test_rejects_missing_fields(self):
        for key in (
            "project_id",
            "role",
            "target",
            "input_refs",
            "scope",
            "prompt_version",
            "size_limit",
            "step_limit",
        ):
            mapping = _task_mapping()
            del mapping[key]
            with self.assertRaises(TaskBoundaryError, msg=key):
                BoundedTask.from_mapping(mapping, configuration=_config())

    def test_rejects_cross_project_refs(self):
        mapping = _task_mapping()
        mapping["target"] = {"project_id": "prj_other", "artifact_id": "a", "revision_id": "r"}
        with self.assertRaises(TaskBoundaryError):
            BoundedTask.from_mapping(mapping, configuration=_config())

        mapping = _task_mapping()
        mapping["input_refs"] = [
            {
                "project_id": "prj_other",
                "artifact_id": "a",
                "revision_id": "r",
                "digest": "b" * 64,
            }
        ]
        with self.assertRaises(TaskBoundaryError):
            BoundedTask.from_mapping(mapping, configuration=_config())

    def test_rejects_bad_digest_and_unknown_ref_keys(self):
        mapping = _task_mapping()
        mapping["input_refs"] = [
            {"project_id": "prj_demo", "artifact_id": "a", "revision_id": "r", "digest": "XYZ"}
        ]
        with self.assertRaises(TaskBoundaryError):
            BoundedTask.from_mapping(mapping, configuration=_config())

        mapping = _task_mapping()
        mapping["input_refs"] = [
            {
                "project_id": "prj_demo",
                "artifact_id": "a",
                "revision_id": "r",
                "digest": "b" * 64,
                "approved": True,
            }
        ]
        with self.assertRaises(TaskBoundaryError):
            BoundedTask.from_mapping(mapping, configuration=_config())

    def test_rejects_unconfigured_role(self):
        partial = _single_role_config("writer", _WRITER_MODEL)
        with self.assertRaises(RoleConfigError):
            BoundedTask.from_mapping(_task_mapping(role="extractor"), configuration=partial)

    def test_step_limit_bounded_by_role(self):
        with self.assertRaises(TaskBoundaryError):
            _task(step_limit=0)
        with self.assertRaises(TaskBoundaryError):
            _task(step_limit=17)  # writer default limit is 16
        with self.assertRaises(TaskBoundaryError):
            _task(step_limit=roles.MAX_TASK_STEPS + 1)
        with self.assertRaises(TaskBoundaryError):
            _task(step_limit=True)
        self.assertEqual(_task(step_limit=16).step_limit, 16)

    def test_size_limit_bounded(self):
        for size in (0, -3, roles.MAX_TASK_SIZE_BYTES + 1, "10"):
            with self.assertRaises(TaskBoundaryError, msg=size):
                _task(size_limit=size)

    def test_scope_and_prompt_version_bounds(self):
        with self.assertRaises(TaskBoundaryError):
            _task(scope=[{"widen": True}])
        with self.assertRaises(TaskBoundaryError):
            _task(scope=[""])
        with self.assertRaises(TaskBoundaryError):
            _task(scope=["x" * (roles.MAX_SCOPE_TOKEN_LEN + 1)])
        with self.assertRaises(TaskBoundaryError):
            _task(scope=["t"] * (roles.MAX_SCOPE_TOKENS + 1))
        with self.assertRaises(TaskBoundaryError):
            _task(prompt_version=" writer.v1")
        with self.assertRaises(TaskBoundaryError):
            _task(prompt_version="writer v1")

    def test_rejects_unbounded_input_refs(self):
        mapping = _task_mapping()
        mapping["input_refs"] = [
            {"project_id": "prj_demo", "artifact_id": "a", "revision_id": "r", "digest": "c" * 64}
        ] * (roles.MAX_INPUT_REFS + 1)
        with self.assertRaises(TaskBoundaryError):
            BoundedTask.from_mapping(mapping, configuration=_config())


class ProposalBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.config = _config()
        self.task = _task(self.config)

    def test_returns_nonauthorizing_proposal(self):
        proposal = submit_proposal(
            self.task, {"content": {"summary": "text"}}, configuration=self.config
        )
        self.assertEqual(proposal.kind, roles.PROPOSAL_KIND)
        self.assertIs(roles.PROPOSAL_IS_AUTHORITATIVE, False)
        self.assertEqual(proposal.role, "writer")
        self.assertEqual(proposal.target, dict(self.task.target))
        self.assertEqual(len(proposal.proposal_id), 64)
        self.assertEqual(len(proposal.payload_digest), 64)
        # No approval / authority surface is exposed at all.
        for forbidden in (
            "approved",
            "approval",
            "authority",
            "owner_auth_ref",
            "decision",
            "gate",
            "budget",
        ):
            self.assertFalse(hasattr(proposal, forbidden), forbidden)
        for method in ("approve", "reject", "apply", "edit_state", "widen"):
            self.assertFalse(hasattr(proposal, method), method)

    def test_requires_bounded_task_and_content(self):
        with self.assertRaises(ProposalError):
            submit_proposal("not a task", {"content": {}}, configuration=self.config)
        with self.assertRaises(ProposalError):
            submit_proposal(self.task, ["nope"], configuration=self.config)
        with self.assertRaises(ProposalError):
            submit_proposal(self.task, {}, configuration=self.config)

    def test_payload_cannot_supply_actor_approval_authority_model_or_scope(self):
        for key, value in (
            ("approved", True),
            ("approval", {"decision": "approved"}),
            ("actor", {"actor_kind": "human"}),
            ("authority", "owner_authenticated"),
            ("owner_auth_ref", "oa_1"),
            ("permissions", [{"action": "*", "effect": "allow"}]),
            ("tools", ["shell"]),
            ("budget", {"budget_usd": "25"}),
            ("model", {"provider_id": "evil", "model_id": "evil"}),
            ("provider_id", "evil"),
            ("role", "lead"),
            ("scope", ["*"]),
            ("target", {"project_id": "prj_evil"}),
        ):
            with self.assertRaises(ProposalError, msg=key):
                submit_proposal(
                    self.task, {"content": {"ok": 1}, key: value}, configuration=self.config
                )

    def test_nested_content_injection_rejected(self):
        for content in (
            {"summary": {"actor": {"authority": "owner_authenticated"}}},
            {"items": [{"permissions": [{"action": "*", "effect": "allow"}]}]},
            {"gate_kind": "protocol"},
            {"approved": True},
            {"tools": []},
            {"scope": ["everything"]},
            {"widen_scope": True},
            {"bypass": "yes"},
        ):
            with self.assertRaises(ProposalError, msg=content):
                submit_proposal(self.task, {"content": content}, configuration=self.config)

    def test_generic_permission_payload_cannot_become_approval(self):
        # A "permission grant" cannot ride in-band and no output turns it into one.
        with self.assertRaises(ProposalError):
            submit_proposal(
                self.task,
                {"content": {"tool_permissions": {"shell": "allow"}, "approved": True}},
                configuration=self.config,
            )
        proposal = submit_proposal(
            self.task, {"content": {"findings": ["minor wording"]}}, configuration=self.config
        )
        self.assertEqual(proposal.kind, "proposal")
        self.assertFalse(hasattr(proposal, "gate_kind"))

    def test_provenance_comes_from_trusted_config_not_payload(self):
        other = _config(
            writer={"provider_id": "openrouter", "model_id": "some/other-model", "variant": "low"}
        )
        task = _task(other)
        proposal = submit_proposal(task, {"content": {"summary": "x"}}, configuration=other)
        self.assertEqual(proposal.model.ref, "openrouter/some/other-model#low")
        # The trusted config, not any payload field, determines the model.
        self.assertEqual(proposal.model, other.for_role("writer").model)

    def test_content_size_bounded_by_task(self):
        task = _task(size_limit=16)
        with self.assertRaises(ProposalError):
            submit_proposal(task, {"content": {"big": "x" * 64}}, configuration=self.config)
        ok = submit_proposal(task, {"content": "tiny"}, configuration=self.config)
        self.assertEqual(ok.content, "tiny")

    def test_non_json_content_rejected(self):
        with self.assertRaises(ProposalError):
            submit_proposal(
                self.task, {"content": {"v": float("nan")}}, configuration=self.config
            )

    def test_deterministic_and_distinct_per_input(self):
        first = submit_proposal(
            self.task, {"content": {"summary": "a"}}, configuration=self.config
        )
        again = submit_proposal(
            self.task, {"content": {"summary": "a"}}, configuration=self.config
        )
        differs = submit_proposal(
            self.task, {"content": {"summary": "b"}}, configuration=self.config
        )
        self.assertEqual(first.proposal_id, again.proposal_id)
        self.assertNotEqual(first.proposal_id, differs.proposal_id)

    def test_does_not_widen_or_repin_task(self):
        proposal = submit_proposal(
            self.task, {"content": {"note": "data"}}, configuration=self.config
        )
        self.assertEqual(proposal.target, dict(self.task.target))
        self.assertEqual(proposal.input_digest, proposal.input_digest)
        # A task for one project keeps that project; the proposal carries no scope.
        self.assertFalse(hasattr(proposal, "scope"))


class OpenCodeV2ConfigTests(unittest.TestCase):
    def setUp(self):
        self.config = _config()
        self.doc = build_opencode_config(self.config)

    def test_uses_v2_keys_and_no_default_model_or_agent(self):
        self.assertEqual(self.doc["$schema"], roles.OPENCODE_CONFIG_SCHEMA_URL)
        self.assertNotIn("model", self.doc)
        self.assertNotIn("default_agent", self.doc)
        for key in ("permissions", "policies", "agents", "mcp", "plugins", "skills"):
            self.assertIn(key, self.doc)
        # No V1 / deprecated keys anywhere in the document.
        deprecated = roles.OPENCODE_DEPRECATED_V1_KEYS
        present = deprecated & _all_dict_keys(self.doc)
        self.assertFalse(present, present)

    def test_broad_deny_all_permission_first_and_every_tool_denied(self):
        self.assertEqual(
            self.doc["permissions"][0], {"action": "*", "resource": "*", "effect": "deny"}
        )
        denied_actions = {
            rule["action"] for rule in self.doc["permissions"] if rule["effect"] == "deny"
        }
        for action in roles.OPENCODE_BUILTIN_TOOL_ACTIONS:
            self.assertIn(action, denied_actions, action)
        # Explicitly deny the filesystem/shell/subagent/web/execute/skill surface.
        for action in (
            "read",
            "edit",
            "glob",
            "grep",
            "shell",
            "subagent",
            "skill",
            "webfetch",
            "websearch",
            "external_directory",
            "execute",
        ):
            self.assertIn(action, denied_actions, action)

    def test_hard_tool_use_and_integration_policies(self):
        policies = self.doc["policies"]
        self.assertEqual(policies[0], {"action": "tool.use", "resource": "*", "effect": "deny"})
        tool_resources = {
            rule["resource"] for rule in policies if rule["action"] == "tool.use"
        }
        for resource in roles.OPENCODE_DENIED_TOOL_RESOURCES:
            self.assertIn(resource, tool_resources, resource)
        integration = {
            rule["resource"] for rule in policies if rule["action"] == "integration.use"
        }
        for resource in ("mcp:*", "skill:*", "plugin:*"):
            self.assertIn(resource, integration, resource)
        self.assertTrue(all(rule["effect"] == "deny" for rule in policies))

    def test_no_mcp_plugin_or_skill_is_enabled(self):
        self.assertEqual(self.doc["mcp"], {"servers": {}})
        self.assertEqual(self.doc["plugins"], [])
        self.assertEqual(self.doc["skills"], [])

    def test_each_role_pins_explicit_model_and_bounded_steps(self):
        agents = self.doc["agents"]
        self.assertEqual(set(agents), set(self.config.role_names))
        for role, agent in agents.items():
            self.assertEqual(
                agent["permissions"], [{"action": "*", "resource": "*", "effect": "deny"}]
            )
            self.assertIsInstance(agent["steps"], int)
            self.assertGreaterEqual(agent["steps"], 1)
            self.assertLessEqual(agent["steps"], roles.MAX_ROLE_STEPS)
            model = agent["model"]
            self.assertEqual(model["providerID"], self.config.for_role(role).model.provider_id)
            self.assertEqual(model["model"], self.config.for_role(role).model.model_id)
            self.assertNotIn("temperature", agent)
            self.assertNotIn("maxSteps", agent)
            self.assertTrue(agent["system"])

    def test_config_is_canonical_json_serializable_and_deterministic(self):
        self.assertEqual(
            roles.opencode_config_bytes(self.config), roles.opencode_config_bytes(self.config)
        )
        self.assertEqual(
            roles.opencode_config_bytes(self.config), canonical_json(self.doc)
        )


if __name__ == "__main__":
    unittest.main()
