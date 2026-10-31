"""Offline regression tests for the direct-OpenRouter generic generation adapter.

Covers ``src/review_article_agent/runtime/generation.py`` (kickoff 16, step 5
foundation):

* the frozen ``LeadConfig`` is reused (**no model default**);
* ``plan_generation`` pins the trusted ``instructions``, the untrusted ``text``,
  the ``role``, and the ``prompt_version`` in an exact canonical request + digest,
  with the exact full-context reserve;
* exactly one no-retry POST is admitted; a forged/mutated plan or a missing
  credential is refused with **zero** I/O;
* a synthetic fake-transport success carries the precise documented payload and a
  parsed JSON ``proposal_json`` plus the observed model/upstream usage/cost;
* malformed / non-object / length / refusal / tool-call / mismatched-model /
  network responses are never accepted, and an unknown cost retains the reserve
  (never ``0``);
* the adapter itself discovers no credential and echoes no peer text.

No network, no credential, no paid call: the transport is always a local fixture.
Run:

    PYTHONPATH=src:tests python3 -m unittest tests.test_runtime_generation
"""

from __future__ import annotations

import dataclasses
import hashlib
import inspect
import json
import sys
import unittest
from decimal import Decimal, localcontext
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from review_article_agent.runtime import generation, lead  # noqa: E402
from review_article_agent.domain.records import canonical_json  # noqa: E402

API_KEY = "sk-or-v1-supersecret-should-never-appear"
CANARY = "PRIVATE_SOURCE_TEXT_CANARY"
MODEL = "deepseek/deepseek-v4.1-flash"
CRITIC_MODEL = "anthropic/claude-opus-4.1"
ROLE = "section_writer"
PROMPT_VERSION = "section_writer-v1"
INSTRUCTIONS = "Write the requested section per the frozen step12 schema revision."
TEXT = "The trial reported a mean reduction of 2.3 mmol/L (SD 0.4) at 12 weeks."


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
def config_mapping(**overrides):
    base = {
        "model_id": MODEL,
        "upstream_provider": "deepseek",
        "max_output_tokens": 4096,
        "context_tokens": 131072,
        "input_price_per_million": "0.10",
        "output_price_per_million": "0.40",
        "pricing_ref": "openrouter/deepseek/deepseek-v4.1-flash@2026-10-29",
        "instructions_revision": "generation-v1",
        "timeout_seconds": 60.0,
        "max_response_bytes": 100000,
    }
    base.update(overrides)
    return base


def cfg(**overrides):
    return lead.LeadConfig.from_mapping(config_mapping(**overrides))


def proposal_json():
    return {
        "section": "results",
        "text": "The trial reported a mean reduction of 2.3 mmol/L.",
        "citations": ["cit_abc"],
    }


def valid_choices(**overrides):
    choice = {
        "index": 0,
        "finish_reason": "stop",
        "message": {
            "role": "assistant",
            "content": json.dumps(proposal_json()),
        },
    }
    choice.update(overrides)
    return choice


def valid_body(**overrides):
    payload = {
        "id": "gen-generation-test",
        "model": MODEL,
        "provider": "DeepSeek",
        "choices": [valid_choices()],
        "usage": {
            "prompt_tokens": 512,
            "completion_tokens": 128,
            "total_tokens": 640,
            "cost": 0.0001024,
        },
    }
    payload.update(overrides)
    return payload


class FakeTransport:
    """Records every call; returns a canned response or raises."""

    def __init__(self, response=None, raises=None):
        self.response = response
        self.raises = raises
        self.calls = []

    def __call__(self, body, api_key, *, endpoint, timeout, max_response_bytes):
        self.calls.append(
            {
                "body": body,
                "api_key": api_key,
                "endpoint": endpoint,
                "timeout": timeout,
                "max_response_bytes": max_response_bytes,
            }
        )
        if self.raises is not None:
            raise self.raises
        return self.response


def resp(status, obj):
    body = obj if isinstance(obj, str) else json.dumps(obj)
    return lead.TransportResponse(status, body)


def plan(**overrides):
    text = overrides.pop("text", TEXT)
    role = overrides.pop("role", ROLE)
    prompt_version = overrides.pop("prompt_version", PROMPT_VERSION)
    instructions = overrides.pop("instructions", INSTRUCTIONS)
    return generation.plan_generation(
        cfg(**overrides),
        role=role,
        prompt_version=prompt_version,
        instructions=instructions,
        text=text,
    )


def run(
    transport,
    *,
    text=TEXT,
    role=ROLE,
    prompt_version=PROMPT_VERSION,
    instructions=INSTRUCTIONS,
    **cfg_overrides,
):
    the_plan = generation.plan_generation(
        cfg(**cfg_overrides),
        role=role,
        prompt_version=prompt_version,
        instructions=instructions,
        text=text,
    )
    return generation.execute_generation(the_plan, api_key=API_KEY, transport=transport)


# --------------------------------------------------------------------------- #
# Config reuse
# --------------------------------------------------------------------------- #
class ConfigReuseTests(unittest.TestCase):
    def test_config_is_the_lead_config_with_no_model_default(self):
        self.assertIs(generation.GenerationConfig, lead.LeadConfig)
        self.assertIsInstance(cfg(), lead.LeadConfig)

    def test_config_argument_is_required(self):
        signature = inspect.signature(generation.plan_generation)
        parameter = signature.parameters["config"]
        self.assertIs(parameter.default, inspect.Parameter.empty)

    def test_config_still_requires_a_model_id(self):
        mapping = config_mapping()
        del mapping["model_id"]
        with self.assertRaises(lead.LeadValidationError):
            generation.GenerationConfig.from_mapping(mapping)

    def test_non_lead_config_refused(self):
        with self.assertRaises(lead.LeadValidationError):
            generation.plan_generation(
                {"model_id": MODEL},
                role=ROLE,
                prompt_version=PROMPT_VERSION,
                instructions=INSTRUCTIONS,
                text=TEXT,
            )


# --------------------------------------------------------------------------- #
# Planning: exact payload, digest, reserve
# --------------------------------------------------------------------------- #
class PlanTests(unittest.TestCase):
    def expected_payload(
        self,
        *,
        role=ROLE,
        prompt_version=PROMPT_VERSION,
        model=MODEL,
        instructions=INSTRUCTIONS,
        text=TEXT,
    ):
        return {
            "model": model,
            "messages": [
                {
                    "role": "system",
                    "content": generation._system_instructions(
                        role, prompt_version, instructions
                    ),
                },
                {"role": "user", "content": text},
            ],
            "stream": False,
            "max_tokens": 4096,
            "provider": {
                "only": ["deepseek"],
                "allow_fallbacks": False,
                "require_parameters": True,
                "data_collection": "deny",
                "max_price": {"prompt": "0.10", "completion": "0.40"},
            },
        }

    def test_precise_payload_and_digest(self):
        the_plan = plan()
        expected = canonical_json(self.expected_payload())
        self.assertEqual(the_plan.request_body, expected)
        self.assertEqual(
            the_plan.request_sha256, hashlib.sha256(expected).hexdigest()
        )
        self.assertEqual(the_plan.request_bytes, len(expected))
        self.assertEqual(the_plan.endpoint, lead.ENDPOINT)
        self.assertEqual(
            the_plan.endpoint, "https://openrouter.ai/api/v1/chat/completions"
        )

    def test_payload_shape(self):
        payload = json.loads(plan().request_body)
        self.assertEqual(payload["model"], MODEL)
        self.assertIs(payload["stream"], False)
        self.assertEqual(payload["max_tokens"], 4096)
        self.assertEqual([m["role"] for m in payload["messages"]], ["system", "user"])
        self.assertEqual(payload["messages"][1]["content"], TEXT)
        provider = payload["provider"]
        self.assertEqual(provider["only"], ["deepseek"])
        self.assertIs(provider["allow_fallbacks"], False)
        self.assertIs(provider["require_parameters"], True)
        self.assertEqual(provider["data_collection"], "deny")
        self.assertEqual(provider["max_price"], {"prompt": "0.10", "completion": "0.40"})

    def test_role_and_prompt_version_pinned_in_system_message(self):
        system = json.loads(plan().request_body)["messages"][0]["content"]
        self.assertIn(ROLE, system)
        self.assertIn(PROMPT_VERSION, system)

    def test_role_is_pinned_in_digest(self):
        a = plan(role="section_writer")
        b = plan(role="section_critic")
        self.assertNotEqual(a.request_sha256, b.request_sha256)
        self.assertNotEqual(
            json.loads(a.request_body)["messages"][0]["content"],
            json.loads(b.request_body)["messages"][0]["content"],
        )

    def test_prompt_version_is_pinned_in_digest(self):
        a = plan(prompt_version="section_writer-v1")
        b = plan(prompt_version="section_writer-v2")
        self.assertNotEqual(a.request_sha256, b.request_sha256)
        self.assertEqual(
            a.prompt_version_sha256,
            hashlib.sha256(b"section_writer-v1").hexdigest(),
        )

    def test_trusted_instructions_pinned_in_digest(self):
        a = plan(instructions="instructions revision A")
        b = plan(instructions="instructions revision B")
        self.assertNotEqual(a.request_sha256, b.request_sha256)
        self.assertEqual(
            a.instructions_sha256,
            hashlib.sha256(b"instructions revision A").hexdigest(),
        )
        forged = dataclasses.replace(a, instructions="tampered")
        with self.assertRaises(lead.LeadValidationError):
            generation.execute_generation(
                forged, api_key=API_KEY, transport=FakeTransport()
            )

    def test_text_pinned_in_digest(self):
        a = plan(text="material A")
        b = plan(text="material B")
        self.assertNotEqual(a.request_sha256, b.request_sha256)

    def test_system_states_untrusted_no_authority_and_one_json_object(self):
        system = json.loads(plan().request_body)["messages"][0]["content"]
        self.assertIn(INSTRUCTIONS, system)
        self.assertIn("UNTRUSTED", system)
        self.assertIn("DATA ONLY", system)
        self.assertIn("NON-AUTHORITATIVE PROPOSAL", system)
        self.assertIn("Return ONE JSON object only.", system)

    def test_untrusted_text_is_user_material_only(self):
        the_plan = plan(text=CANARY)
        payload = json.loads(the_plan.request_body)
        self.assertEqual(payload["messages"][1]["content"], CANARY)
        self.assertNotIn(CANARY, payload["messages"][0]["content"])

    def test_reserve_is_exact_full_context_bound(self):
        the_plan = plan()
        expected = (
            Decimal(131072) * Decimal("0.10") / Decimal(1_000_000)
            + Decimal(4096) * Decimal("0.40") / Decimal(1_000_000)
        )
        self.assertEqual(Decimal(the_plan.reserve_usd), expected)
        self.assertEqual(the_plan.reserve_usd, "0.0147456")

    def test_reserve_independent_of_prompt_length_and_precision(self):
        short = plan(text="short")
        long = plan(text="x" * 20000)
        self.assertEqual(short.reserve_usd, long.reserve_usd)
        with localcontext() as ctx:
            ctx.prec = 2
            self.assertEqual(plan().reserve_usd, "0.0147456")

    def test_bound_evidence_recorded(self):
        evidence = dict(plan().bound_evidence)
        self.assertEqual(
            evidence["reserve_basis"],
            "context_capacity_prompt_plus_max_output_completion",
        )
        self.assertEqual(evidence["model_id"], MODEL)
        self.assertEqual(evidence["context_tokens"], 131072)
        self.assertEqual(evidence["max_output_tokens"], 4096)
        self.assertEqual(evidence["pricing_ref"], config_mapping()["pricing_ref"])
        self.assertEqual(evidence["reserve_usd"], "0.0147456")

    def test_role_required(self):
        for bad in ("other", "", None, 3):
            with self.assertRaises(lead.LeadValidationError):
                generation.plan_generation(
                    cfg(),
                    role=bad,
                    prompt_version=PROMPT_VERSION,
                    instructions=INSTRUCTIONS,
                    text=TEXT,
                )
        for good in generation.ROLES:
            generation.plan_generation(
                cfg(),
                role=good,
                prompt_version=PROMPT_VERSION,
                instructions=INSTRUCTIONS,
                text=TEXT,
            )

    def test_roles_tuple_is_the_documented_set(self):
        self.assertEqual(
            generation.ROLES,
            (
                "screening",
                "synthesis",
                "framework",
                "outline",
                "blueprint",
                "section_writer",
                "section_critic",
                "section_repair",
                "fact_checker",
                "global_reviewer",
                "global_writer",
                "notes_planner",
            ),
        )

    def test_prompt_version_required(self):
        for bad in (None, "", "   ", 3):
            with self.assertRaises(lead.LeadValidationError):
                generation.plan_generation(
                    cfg(),
                    role=ROLE,
                    prompt_version=bad,
                    instructions=INSTRUCTIONS,
                    text=TEXT,
                )

    def test_instructions_required(self):
        for bad in (None, "", "   ", 3):
            with self.assertRaises(lead.LeadValidationError):
                generation.plan_generation(
                    cfg(),
                    role=ROLE,
                    prompt_version=PROMPT_VERSION,
                    instructions=bad,
                    text=TEXT,
                )

    def test_text_must_be_a_string(self):
        with self.assertRaises(lead.LeadValidationError):
            generation.plan_generation(
                cfg(),
                role=ROLE,
                prompt_version=PROMPT_VERSION,
                instructions=INSTRUCTIONS,
                text=3,
            )

    def test_oversize_input_refused(self):
        with self.assertRaises(lead.LeadValidationError):
            generation.plan_generation(
                cfg(),
                role=ROLE,
                prompt_version=PROMPT_VERSION,
                instructions=INSTRUCTIONS,
                text="x" * (lead.MAX_REQUEST_BYTES + 1),
            )

    def test_bad_config_refused(self):
        with self.assertRaises(lead.LeadValidationError):
            generation.plan_generation(
                cfg(context_tokens=10),
                role=ROLE,
                prompt_version=PROMPT_VERSION,
                instructions=INSTRUCTIONS,
                text=TEXT,
            )

    def test_plan_is_deterministic(self):
        a = plan()
        b = plan()
        self.assertEqual(a.request_sha256, b.request_sha256)
        self.assertEqual(a.request_bytes, b.request_bytes)

    def test_payload_is_frozen_and_immutable(self):
        the_plan = plan()
        with self.assertRaises(TypeError):
            the_plan.payload["messages"] = ()
        with self.assertRaises(TypeError):
            the_plan.payload["provider"]["data_collection"] = "allow"


# --------------------------------------------------------------------------- #
# Success path (synthetic fake transport)
# --------------------------------------------------------------------------- #
class SuccessTests(unittest.TestCase):
    def test_completed_and_sent_body_matches_plan(self):
        the_plan = plan()
        transport = FakeTransport(resp(200, valid_body()))
        result = generation.execute_generation(
            the_plan, api_key=API_KEY, transport=transport
        )
        self.assertEqual(result.outcome, "completed")
        self.assertEqual(result.calls_attempted, 1)
        self.assertTrue(result.no_automatic_retry)
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(transport.calls[0]["body"], the_plan.request_body)
        self.assertEqual(
            hashlib.sha256(transport.calls[0]["body"]).hexdigest(),
            result.request_sha256,
        )
        self.assertEqual(transport.calls[0]["endpoint"], lead.ENDPOINT)
        self.assertEqual(transport.calls[0]["max_response_bytes"], 100000)
        self.assertEqual(transport.calls[0]["timeout"], 60.0)

    def test_exact_payload_digest_sent(self):
        the_plan = plan()
        transport = FakeTransport(resp(200, valid_body()))
        generation.execute_generation(the_plan, api_key=API_KEY, transport=transport)
        sent = json.loads(transport.calls[0]["body"])
        self.assertEqual(sent, json.loads(the_plan.request_body))
        self.assertEqual(sent["messages"][1]["content"], TEXT)

    def test_proposal_json_role_and_observed_model(self):
        result = run(FakeTransport(resp(200, valid_body())))
        self.assertEqual(result.actual_model, MODEL)
        self.assertEqual(result.model, MODEL)
        self.assertEqual(result.provider, "DeepSeek")
        self.assertEqual(result.response_id, "gen-generation-test")
        self.assertEqual(result.role, ROLE)
        self.assertEqual(result.prompt_version, PROMPT_VERSION)
        self.assertEqual(
            canonical_json(result.proposal_json), canonical_json(proposal_json())
        )
        self.assertFalse(result.is_authoritative())

    def test_usage_and_cost_reported(self):
        result = run(FakeTransport(resp(200, valid_body())))
        self.assertEqual(result.usage.prompt_tokens, 512)
        self.assertEqual(result.usage.completion_tokens, 128)
        self.assertEqual(result.usage.total_tokens, 640)
        self.assertEqual(result.cost.state, "known")
        self.assertEqual(result.cost.amount_usd, "0.0001024")
        self.assertFalse(result.cost.reserve_retained)
        self.assertEqual(result.cost.route, "direct_call")
        self.assertEqual(result.cost.accounting_bucket, "direct_call")

    def test_critic_role_success_uses_separate_config(self):
        transport = FakeTransport(resp(200, valid_body(model=CRITIC_MODEL)))
        result = run(
            transport,
            role="section_critic",
            prompt_version="section_critic-v1",
            model_id=CRITIC_MODEL,
            upstream_provider="anthropic",
        )
        self.assertEqual(result.outcome, "completed")
        self.assertEqual(result.role, "section_critic")
        self.assertEqual(result.prompt_version, "section_critic-v1")
        self.assertEqual(result.actual_model, CRITIC_MODEL)
        sent = json.loads(transport.calls[0]["body"])
        self.assertEqual(sent["model"], CRITIC_MODEL)

    def test_completed_without_cost_is_unknown_retained(self):
        body = valid_body()
        body["usage"] = {"prompt_tokens": 1, "completion_tokens": 1}
        result = run(FakeTransport(resp(200, body)))
        self.assertEqual(result.outcome, "completed")
        self.assertEqual(result.cost.state, "unknown")
        self.assertTrue(result.cost.reserve_retained)
        self.assertIsNone(result.cost.amount_usd)

    def test_zero_cost_is_known(self):
        body = valid_body()
        body["usage"]["cost"] = 0
        result = run(FakeTransport(resp(200, body)))
        self.assertEqual(result.cost.state, "known")
        self.assertEqual(result.cost.amount_usd, "0")

    def test_instructions_digest_reported(self):
        result = run(FakeTransport(resp(200, valid_body())))
        self.assertEqual(
            result.instructions_sha256,
            hashlib.sha256(INSTRUCTIONS.encode("utf-8")).hexdigest(),
        )


# --------------------------------------------------------------------------- #
# Never a silent success
# --------------------------------------------------------------------------- #
class NeverSuccessTests(unittest.TestCase):
    def _run_body(self, **overrides):
        return run(FakeTransport(resp(200, valid_body(**overrides))))

    def test_malformed_responses_are_never_success(self):
        """Table of malformed-provider-response input-validation cases (case
        labels are the original method names; every case/assertion preserved)."""

        def malformed_non_json_content():
            choice = valid_choices()
            choice["message"]["content"] = "not json at all"
            result = self._run_body(choices=[choice])
            self.assertEqual(result.outcome, "protocol_error")
            self.assertIsNone(result.proposal_json)
            self.assertIsNone(result.proposal_text)

        def valid_json_but_not_an_object():
            choice = valid_choices()
            choice["message"]["content"] = "[1, 2, 3]"
            result = self._run_body(choices=[choice])
            self.assertEqual(result.outcome, "protocol_error")
            self.assertIsNone(result.proposal_json)

        def wrong_model():
            result = run(FakeTransport(resp(200, valid_body(model="openai/other-model"))))
            self.assertEqual(result.outcome, "protocol_error")
            self.assertIsNone(result.proposal_json)
            self.assertEqual(result.cost.state, "known")
            self.assertEqual(result.cost.amount_usd, "0.0001024")

        def length_finish():
            result = self._run_body(choices=[valid_choices(finish_reason="length")])
            self.assertEqual(result.outcome, "protocol_error")
            self.assertIsNone(result.proposal_json)

        def tool_calls():
            choice = valid_choices()
            choice["message"]["tool_calls"] = [{"id": "t1", "type": "function"}]
            result = self._run_body(choices=[choice])
            self.assertEqual(result.outcome, "protocol_error")

        def top_level_error():
            body = valid_body()
            body["error"] = {"code": 500, "message": CANARY}
            result = run(FakeTransport(resp(200, body)))
            self.assertEqual(result.outcome, "protocol_error")

        def missing_choices():
            body = valid_body()
            del body["choices"]
            result = run(FakeTransport(resp(200, body)))
            self.assertEqual(result.outcome, "protocol_error")

        def non_text_content():
            choice = valid_choices()
            choice["message"]["content"] = [{"type": "text", "text": "hi"}]
            result = self._run_body(choices=[choice])
            self.assertEqual(result.outcome, "protocol_error")

        def empty_content():
            choice = valid_choices()
            choice["message"]["content"] = "   "
            result = self._run_body(choices=[choice])
            self.assertEqual(result.outcome, "protocol_error")

        for label, case in (
            ("test_malformed_non_json_content_is_protocol_error", malformed_non_json_content),
            ("test_valid_json_but_not_an_object_is_protocol_error", valid_json_but_not_an_object),
            ("test_wrong_model_is_protocol_error", wrong_model),
            ("test_length_finish_is_never_success", length_finish),
            ("test_tool_calls_are_never_success", tool_calls),
            ("test_top_level_error_is_protocol_error", top_level_error),
            ("test_missing_choices_is_protocol_error", missing_choices),
            ("test_non_text_content_is_protocol_error", non_text_content),
            ("test_empty_content_is_protocol_error", empty_content),
        ):
            with self.subTest(case=label):
                case()

    def test_refusal_is_refused(self):
        choice = valid_choices()
        choice["message"]["refusal"] = "I cannot help with that."
        choice["message"]["content"] = ""
        result = self._run_body(choices=[choice])
        self.assertEqual(result.outcome, "refused")
        self.assertIsNone(result.proposal_json)
        self.assertIsNone(result.proposal_text)

    def test_content_filter_is_refused(self):
        result = self._run_body(choices=[valid_choices(finish_reason="content_filter")])
        self.assertEqual(result.outcome, "refused")


# --------------------------------------------------------------------------- #
# Ambiguity / cost accounting
# --------------------------------------------------------------------------- #
class AmbiguityTests(unittest.TestCase):
    def test_ambiguous_statuses_keep_unknown(self):
        for status in (429, 500, 502, 503, 504, 301, 302, 201, 204):
            body = json.dumps({"error": {"code": status, "message": CANARY}})
            result = run(FakeTransport(lead.TransportResponse(status, body)))
            self.assertEqual(result.outcome, "unknown", status)
            self.assertEqual(result.cost.state, "unknown", status)
            self.assertTrue(result.cost.reserve_retained, status)
            self.assertIsNone(result.cost.amount_usd, status)

    def test_definitive_statuses_are_not_incurred(self):
        for status in (400, 401, 402, 403, 404, 413):
            body = json.dumps({"error": {"code": status, "message": CANARY}})
            result = run(FakeTransport(lead.TransportResponse(status, body)))
            self.assertEqual(result.outcome, "failed", status)
            self.assertEqual(result.cost.state, "not_incurred", status)
            self.assertFalse(result.cost.reserve_retained, status)

    def test_network_transport_error_is_unknown_retained(self):
        transport = FakeTransport(raises=lead.LeadTransportError("dropped"))
        result = run(transport)
        self.assertEqual(result.outcome, "unknown")
        self.assertEqual(result.calls_attempted, 1)
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(result.cost.state, "unknown")
        self.assertTrue(result.cost.reserve_retained)
        self.assertIsNone(result.cost.amount_usd)

    def test_unexpected_transport_exception_is_unknown_retained(self):
        result = run(FakeTransport(raises=RuntimeError("kaboom")))
        self.assertEqual(result.outcome, "unknown")
        self.assertEqual(result.cost.state, "unknown")
        self.assertTrue(result.cost.reserve_retained)

    def test_malformed_200_without_cost_stays_unknown(self):
        body = valid_body()
        body["choices"] = ["not", "a", "map"]
        body["usage"].pop("cost", None)
        result = run(FakeTransport(resp(200, body)))
        self.assertEqual(result.outcome, "protocol_error")
        self.assertEqual(result.cost.state, "unknown")
        self.assertTrue(result.cost.reserve_retained)
        self.assertNotEqual(result.cost.state, "not_incurred")

    def test_malformed_200_with_valid_cost_is_known(self):
        body = valid_body()
        body["choices"] = ["not", "a", "map"]
        result = run(FakeTransport(resp(200, body)))
        self.assertEqual(result.outcome, "protocol_error")
        self.assertEqual(result.cost.state, "known")
        self.assertEqual(result.cost.amount_usd, "0.0001024")

    def test_invalid_json_body_is_protocol_error_unknown(self):
        result = run(FakeTransport(resp(200, "<html>nope</html>")))
        self.assertEqual(result.outcome, "protocol_error")
        self.assertEqual(result.cost.state, "unknown")
        self.assertTrue(result.cost.reserve_retained)

    def test_oversize_response_is_protocol_error(self):
        result = run(
            FakeTransport(lead.TransportResponse(200, "x" * 50, truncated=True)),
            max_response_bytes=100000,
        )
        self.assertEqual(result.outcome, "protocol_error")
        self.assertEqual(result.cost.state, "unknown")
        self.assertTrue(result.cost.reserve_retained)

    def test_response_over_configured_bound(self):
        result = run(
            FakeTransport(lead.TransportResponse(200, json.dumps(valid_body()))),
            max_response_bytes=10,
        )
        self.assertEqual(result.outcome, "protocol_error")
        self.assertEqual(result.cost.state, "unknown")

    def test_huge_exponent_cost_is_bounded(self):
        raw = (
            '{"model":"%s","choices":[{"finish_reason":"stop",'
            '"message":{"role":"assistant","content":"{\\"a\\":1}"}}],'
            '"usage":{"prompt_tokens":1,"completion_tokens":1,"cost":1e999999999}}'
            % MODEL
        )
        result = run(FakeTransport(resp(200, raw)))
        self.assertEqual(result.outcome, "completed")
        self.assertEqual(result.cost.state, "unknown")
        self.assertTrue(result.cost.reserve_retained)


# --------------------------------------------------------------------------- #
# Guards: zero I/O on refusal, no retry, secrets, no raw peer text
# --------------------------------------------------------------------------- #
class GuardTests(unittest.TestCase):
    def _valid_plan(self):
        return plan()

    def test_missing_credential_refused_zero_calls(self):
        transport = FakeTransport(resp(200, valid_body()))
        for bad in (None, "", 0, 123):
            with self.assertRaises(lead.LeadCredentialError):
                generation.execute_generation(
                    self._valid_plan(), api_key=bad, transport=transport
                )
        self.assertEqual(transport.calls, [])

    def test_forged_role_refused(self):
        forged = dataclasses.replace(self._valid_plan(), role="other")
        with self.assertRaises(lead.LeadValidationError):
            generation.execute_generation(
                forged, api_key=API_KEY, transport=FakeTransport()
            )

    def test_forged_prompt_version_refused(self):
        forged = dataclasses.replace(self._valid_plan(), prompt_version="")
        with self.assertRaises(lead.LeadValidationError):
            generation.execute_generation(
                forged, api_key=API_KEY, transport=FakeTransport()
            )

    def test_forged_text_refused(self):
        forged = dataclasses.replace(self._valid_plan(), text="MUTATED")
        transport = FakeTransport(resp(200, valid_body()))
        with self.assertRaises(lead.LeadValidationError):
            generation.execute_generation(forged, api_key=API_KEY, transport=transport)
        self.assertEqual(transport.calls, [])

    def test_forged_instructions_refused(self):
        forged = dataclasses.replace(self._valid_plan(), instructions="MUTATED")
        with self.assertRaises(lead.LeadValidationError):
            generation.execute_generation(
                forged, api_key=API_KEY, transport=FakeTransport()
            )

    def test_forged_body_refused(self):
        forged = dataclasses.replace(self._valid_plan(), request_body=b"{}")
        with self.assertRaises(lead.LeadValidationError):
            generation.execute_generation(
                forged, api_key=API_KEY, transport=FakeTransport()
            )
        self.assertEqual(forged.request_body, b"{}")

    def test_forged_digest_refused(self):
        forged = dataclasses.replace(self._valid_plan(), request_sha256="0" * 64)
        with self.assertRaises(lead.LeadValidationError):
            generation.execute_generation(
                forged, api_key=API_KEY, transport=FakeTransport()
            )

    def test_forged_prompt_version_digest_refused(self):
        forged = dataclasses.replace(self._valid_plan(), prompt_version_sha256="0" * 64)
        with self.assertRaises(lead.LeadValidationError):
            generation.execute_generation(
                forged, api_key=API_KEY, transport=FakeTransport()
            )

    def test_forged_reserve_refused(self):
        forged = dataclasses.replace(self._valid_plan(), reserve_usd="0")
        with self.assertRaises(lead.LeadValidationError):
            generation.execute_generation(
                forged, api_key=API_KEY, transport=FakeTransport()
            )

    def test_forged_bound_evidence_refused(self):
        forged = dataclasses.replace(self._valid_plan(), bound_evidence={"a": 1})
        with self.assertRaises(lead.LeadValidationError):
            generation.execute_generation(
                forged, api_key=API_KEY, transport=FakeTransport()
            )

    def test_forged_config_refused(self):
        forged = dataclasses.replace(
            self._valid_plan(), config=cfg(model_id="openai/other")
        )
        with self.assertRaises(lead.LeadError):
            generation.execute_generation(
                forged, api_key=API_KEY, transport=FakeTransport()
            )

    def test_forged_endpoint_refused(self):
        forged = dataclasses.replace(
            self._valid_plan(), endpoint="https://evil.example/x"
        )
        with self.assertRaises(lead.LeadError):
            generation.execute_generation(
                forged, api_key=API_KEY, transport=FakeTransport()
            )

    def test_forged_payload_refused(self):
        forged = dataclasses.replace(self._valid_plan(), payload={"model": MODEL})
        with self.assertRaises(lead.LeadValidationError):
            generation.execute_generation(
                forged, api_key=API_KEY, transport=FakeTransport()
            )

    def test_non_plan_refused(self):
        with self.assertRaises(lead.LeadValidationError):
            generation.execute_generation(
                {"nope": 1}, api_key=API_KEY, transport=FakeTransport()
            )

    def test_no_retry_single_call(self):
        transport = FakeTransport(resp(200, valid_body()))
        run(transport)
        self.assertEqual(len(transport.calls), 1)

    def test_key_never_in_body_or_result(self):
        transport = FakeTransport(resp(200, valid_body()))
        result = run(transport)
        sent = transport.calls[0]["body"].decode("utf-8")
        self.assertNotIn(API_KEY, sent)
        self.assertNotIn(API_KEY, json.dumps(result.__dict__, default=str))
        self.assertEqual(transport.calls[0]["api_key"], API_KEY)

    def test_key_never_in_error_messages(self):
        transport = FakeTransport(resp(200, valid_body()))
        try:
            generation.execute_generation(
                plan(), api_key=API_KEY, transport=transport
            )
        except lead.LeadError as exc:  # pragma: no cover - success path has no error
            self.assertNotIn(API_KEY, str(exc))
        # A forged plan's refusal must not echo the credential either.
        forged = dataclasses.replace(plan(), request_body=b"{}")
        try:
            generation.execute_generation(forged, api_key=API_KEY, transport=transport)
        except lead.LeadError as exc:
            self.assertNotIn(API_KEY, str(exc))

    def test_ambiguous_peer_text_never_echoed(self):
        body = json.dumps(
            {"error": {"code": 400, "message": f"bad key {API_KEY} sk-or-v1-leak {CANARY}"}}
        )
        result = run(FakeTransport(resp(400, body)))
        detail = result.diagnostic.detail_redacted
        self.assertNotIn(API_KEY, detail)
        self.assertNotIn("sk-or-", detail)
        self.assertNotIn(CANARY, detail)
        self.assertIn("http 400", detail)

    def test_transport_error_message_is_static(self):
        result = run(FakeTransport(raises=lead.LeadTransportError("inner-secret")))
        self.assertNotIn("inner-secret", result.diagnostic.detail_redacted)

    def test_adapter_does_not_discover_credentials(self):
        source = Path(generation.__file__).read_text(encoding="utf-8")
        for forbidden in (
            "os.environ",
            "os.getenv",
            "getenv(",
            "dotenv",
            "load_dotenv",
            "Path.home()",
        ):
            self.assertNotIn(forbidden, source, forbidden)

    def test_adapter_has_no_scientific_validation_or_authority(self):
        self.assertTrue(hasattr(generation, "GenerationResult"))
        result = run(FakeTransport(resp(200, valid_body())))
        self.assertFalse(result.is_authoritative())


# --------------------------------------------------------------------------- #
# Installed-package shape
# --------------------------------------------------------------------------- #
class InstalledPackageTests(unittest.TestCase):
    def test_module_is_a_package_submodule(self):
        self.assertEqual(
            generation.__name__, "review_article_agent.runtime.generation"
        )

    def test_endpoint_is_the_documented_one(self):
        self.assertEqual(
            generation.ENDPOINT, "https://openrouter.ai/api/v1/chat/completions"
        )


if __name__ == "__main__":
    unittest.main()
