"""Offline regression tests for the direct-OpenRouter extraction/quality adapter.

Covers ``src/review_article_agent/runtime/extraction.py`` against the acceptance
criteria of task S9-TRANSPORT:

* the frozen ``LeadConfig`` is reused (**no model default**);
* ``plan_extraction`` pins the trusted ``instructions`` and untrusted ``text`` in
  an exact canonical request + digest, with the exact full-context reserve;
* exactly one no-retry POST is admitted; a forged/mutated plan or a missing
  credential is refused with **zero** I/O;
* a synthetic fake-transport success carries the precise documented payload and a
  parsed JSON ``proposal_json`` plus the observed model/upstream usage/cost;
* malformed / non-object / length / refusal / mismatched-model / network responses
  are never accepted, and an unknown cost retains the reserve (never ``0``);
* the adapter itself discovers no credential and echoes no peer text.

No network, no credential, no paid call: the transport is always a local fixture.
Run:

    PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_extraction_transport.py'
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

from review_article_agent.runtime import extraction, lead  # noqa: E402
from review_article_agent.domain.records import canonical_json  # noqa: E402

API_KEY = "sk-or-v1-supersecret-should-never-appear"
CANARY = "PRIVATE_SOURCE_TEXT_CANARY"
MODEL = "deepseek/deepseek-v4.1-flash"
QUALITY_MODEL = "anthropic/claude-opus-4.1"
INSTRUCTIONS = "Extract findings per the frozen step09 schema revision 'extraction-v1'."
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
        "instructions_revision": "extraction-v1",
        "timeout_seconds": 60.0,
        "max_response_bytes": 100000,
    }
    base.update(overrides)
    return base


def cfg(**overrides):
    return lead.LeadConfig.from_mapping(config_mapping(**overrides))


def extraction_json():
    return {
        "findings": [
            {
                "finding": "mean reduction of 2.3 mmol/L",
                "locator": "abstract",
                "context": "12-week endpoint",
                "limitations": ["single trial"],
            }
        ],
        "missing": ["baseline values"],
    }


def valid_choices(**overrides):
    choice = {
        "index": 0,
        "finish_reason": "stop",
        "message": {
            "role": "assistant",
            "content": json.dumps(extraction_json()),
        },
    }
    choice.update(overrides)
    return choice


def valid_body(**overrides):
    payload = {
        "id": "gen-extraction-test",
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
    purpose = overrides.pop("purpose", "extraction")
    instructions = overrides.pop("instructions", INSTRUCTIONS)
    return extraction.plan_extraction(
        cfg(**overrides), purpose=purpose, instructions=instructions, text=text
    )


def run(transport, *, text=TEXT, purpose="extraction", instructions=INSTRUCTIONS, **cfg_overrides):
    the_plan = extraction.plan_extraction(
        cfg(**cfg_overrides), purpose=purpose, instructions=instructions, text=text
    )
    return extraction.execute_extraction(the_plan, api_key=API_KEY, transport=transport)


# --------------------------------------------------------------------------- #
# Config reuse
# --------------------------------------------------------------------------- #
class ConfigReuseTests(unittest.TestCase):
    def test_config_is_the_lead_config_with_no_model_default(self):
        self.assertIs(extraction.ExtractionConfig, lead.LeadConfig)
        self.assertIsInstance(cfg(), lead.LeadConfig)

    def test_config_argument_is_required(self):
        signature = inspect.signature(extraction.plan_extraction)
        parameter = signature.parameters["config"]
        self.assertIs(parameter.default, inspect.Parameter.empty)

    def test_config_still_requires_a_model_id(self):
        mapping = config_mapping()
        del mapping["model_id"]
        # The strict config mapping itself refuses a missing model.
        with self.assertRaises(lead.LeadValidationError):
            extraction.ExtractionConfig.from_mapping(mapping)

    def test_non_lead_config_refused(self):
        with self.assertRaises(lead.LeadValidationError):
            extraction.plan_extraction(
                {"model_id": MODEL}, purpose="extraction",
                instructions=INSTRUCTIONS, text=TEXT,
            )


# --------------------------------------------------------------------------- #
# Planning: exact payload, digest, reserve
# --------------------------------------------------------------------------- #
class PlanTests(unittest.TestCase):
    def expected_payload(self, *, purpose="extraction", model=MODEL,
                         instructions=INSTRUCTIONS, text=TEXT):
        return {
            "model": model,
            "messages": [
                {"role": "system", "content": extraction._system_instructions(purpose, instructions)},
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
            extraction.execute_extraction(
                forged, api_key=API_KEY, transport=FakeTransport()
            )

    def test_purpose_is_pinned_in_digest(self):
        a = plan(purpose="extraction")
        b = plan(purpose="quality")
        self.assertNotEqual(a.request_sha256, b.request_sha256)
        system_a = json.loads(a.request_body)["messages"][0]["content"]
        system_b = json.loads(b.request_body)["messages"][0]["content"]
        self.assertNotEqual(system_a, system_b)
        self.assertIn("grounded extraction", system_a)
        self.assertIn("quality checking", system_b)

    def test_system_states_untrusted_and_no_authority(self):
        system = json.loads(plan().request_body)["messages"][0]["content"]
        self.assertIn(INSTRUCTIONS, system)
        self.assertIn("UNTRUSTED", system)
        self.assertIn("no authority", system)
        self.assertIn("DATA ONLY", system)

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

    def test_invalid_plan_inputs_refused(self):
        # Each former standalone input-validation test is preserved verbatim as
        # a labelled subTest case.
        def case_test_purpose_required():
            for bad in ("other", "", None, 3):
                with self.assertRaises(lead.LeadValidationError):
                    extraction.plan_extraction(
                        cfg(), purpose=bad, instructions=INSTRUCTIONS, text=TEXT
                    )
            for good in extraction.PURPOSES:
                extraction.plan_extraction(
                    cfg(), purpose=good, instructions=INSTRUCTIONS, text=TEXT
                )

        def case_test_instructions_required():
            for bad in (None, "", "   ", 3):
                with self.assertRaises(lead.LeadValidationError):
                    extraction.plan_extraction(
                        cfg(), purpose="extraction", instructions=bad, text=TEXT
                    )

        def case_test_text_must_be_a_string():
            with self.assertRaises(lead.LeadValidationError):
                extraction.plan_extraction(
                    cfg(), purpose="extraction", instructions=INSTRUCTIONS, text=3
                )

        for label, case in (
            ("test_purpose_required", case_test_purpose_required),
            ("test_instructions_required", case_test_instructions_required),
            ("test_text_must_be_a_string", case_test_text_must_be_a_string),
        ):
            with self.subTest(case=label):
                case()

    def test_oversize_input_refused(self):
        with self.assertRaises(lead.LeadValidationError):
            extraction.plan_extraction(
                cfg(), purpose="extraction", instructions=INSTRUCTIONS,
                text="x" * (lead.MAX_REQUEST_BYTES + 1),
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
        result = extraction.execute_extraction(
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
        extraction.execute_extraction(the_plan, api_key=API_KEY, transport=transport)
        sent = json.loads(transport.calls[0]["body"])
        self.assertEqual(sent, json.loads(the_plan.request_body))
        self.assertEqual(
            sent["messages"][1]["content"], TEXT
        )

    def test_proposal_json_and_observed_model(self):
        result = run(FakeTransport(resp(200, valid_body())))
        self.assertEqual(result.actual_model, MODEL)
        self.assertEqual(result.model, MODEL)
        self.assertEqual(result.provider, "DeepSeek")
        self.assertEqual(result.response_id, "gen-extraction-test")
        self.assertEqual(result.purpose, "extraction")
        self.assertEqual(
            canonical_json(result.proposal_json), canonical_json(extraction_json())
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

    def test_quality_purpose_success_uses_stronger_config(self):
        transport = FakeTransport(resp(200, valid_body(model=QUALITY_MODEL)))
        result = run(
            transport, purpose="quality", model_id=QUALITY_MODEL,
            upstream_provider="anthropic", instructions_revision="quality-v1",
        )
        self.assertEqual(result.outcome, "completed")
        self.assertEqual(result.purpose, "quality")
        self.assertEqual(result.actual_model, QUALITY_MODEL)
        sent = json.loads(transport.calls[0]["body"])
        self.assertEqual(sent["model"], QUALITY_MODEL)

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

    def test_never_a_silent_success(self):
        # One table per malformed/refused 200 body: each former standalone test
        # is preserved verbatim as a labelled subTest case (no behaviour lost).
        def case_test_malformed_non_json_content_is_protocol_error():
            choice = valid_choices()
            choice["message"]["content"] = "not json at all"
            result = self._run_body(choices=[choice])
            self.assertEqual(result.outcome, "protocol_error")
            self.assertIsNone(result.proposal_json)
            self.assertIsNone(result.proposal_text)

        def case_test_valid_json_but_not_an_object_is_protocol_error():
            choice = valid_choices()
            choice["message"]["content"] = "[1, 2, 3]"
            result = self._run_body(choices=[choice])
            self.assertEqual(result.outcome, "protocol_error")
            self.assertIsNone(result.proposal_json)

        def case_test_wrong_model_is_protocol_error():
            result = run(FakeTransport(resp(200, valid_body(model="openai/other-model"))))
            self.assertEqual(result.outcome, "protocol_error")
            self.assertIsNone(result.proposal_json)
            # The reported cost is still salvaged and retained as known.
            self.assertEqual(result.cost.state, "known")
            self.assertEqual(result.cost.amount_usd, "0.0001024")

        def case_test_length_finish_is_never_success():
            result = self._run_body(choices=[valid_choices(finish_reason="length")])
            self.assertEqual(result.outcome, "protocol_error")
            self.assertIsNone(result.proposal_json)

        def case_test_tool_calls_are_never_success():
            choice = valid_choices()
            choice["message"]["tool_calls"] = [{"id": "t1", "type": "function"}]
            result = self._run_body(choices=[choice])
            self.assertEqual(result.outcome, "protocol_error")

        def case_test_top_level_error_is_protocol_error():
            body = valid_body()
            body["error"] = {"code": 500, "message": CANARY}
            result = run(FakeTransport(resp(200, body)))
            self.assertEqual(result.outcome, "protocol_error")

        def case_test_missing_choices_is_protocol_error():
            body = valid_body()
            del body["choices"]
            result = run(FakeTransport(resp(200, body)))
            self.assertEqual(result.outcome, "protocol_error")

        def case_test_non_text_content_is_protocol_error():
            choice = valid_choices()
            choice["message"]["content"] = [{"type": "text", "text": "hi"}]
            result = self._run_body(choices=[choice])
            self.assertEqual(result.outcome, "protocol_error")

        def case_test_empty_content_is_protocol_error():
            choice = valid_choices()
            choice["message"]["content"] = "   "
            result = self._run_body(choices=[choice])
            self.assertEqual(result.outcome, "protocol_error")

        cases = [
            ("test_malformed_non_json_content_is_protocol_error",
             case_test_malformed_non_json_content_is_protocol_error),
            ("test_valid_json_but_not_an_object_is_protocol_error",
             case_test_valid_json_but_not_an_object_is_protocol_error),
            ("test_wrong_model_is_protocol_error", case_test_wrong_model_is_protocol_error),
            ("test_length_finish_is_never_success", case_test_length_finish_is_never_success),
            ("test_tool_calls_are_never_success", case_test_tool_calls_are_never_success),
            ("test_top_level_error_is_protocol_error", case_test_top_level_error_is_protocol_error),
            ("test_missing_choices_is_protocol_error", case_test_missing_choices_is_protocol_error),
            ("test_non_text_content_is_protocol_error", case_test_non_text_content_is_protocol_error),
            ("test_empty_content_is_protocol_error", case_test_empty_content_is_protocol_error),
        ]
        for label, case in cases:
            with self.subTest(case=label):
                case()

    # K2 security/fail-closed: kept standalone (refusals are never merged).
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
# Bounded, static parser-failure diagnostic codes
# --------------------------------------------------------------------------- #
class ResponseDiagnosticCodeTests(unittest.TestCase):
    """A malformed 200 carries a *distinguishable*, bounded, static code.

    These are the branches a live protocol_error previously collapsed into one
    generic code; the code (never the body, finish-reason string, or provider
    message) is what becomes durable on the run.
    """

    def _code(self, response):
        return run(FakeTransport(response)).diagnostic.code

    def test_codes_are_from_the_bounded_set(self):
        for response in (
            resp(200, valid_body(model="openai/other")),
            resp(200, "<html>not json</html>"),
        ):
            with self.subTest(response=str(response)[:40]):
                self.assertIn(
                    self._code(response), extraction.RESPONSE_DIAGNOSTIC_CODES
                )

    def test_distinct_branches_have_distinct_codes(self):
        length = valid_choices()
        length["finish_reason"] = "length"
        other_finish = valid_choices()
        other_finish["finish_reason"] = "tool_calls"
        bad_json = valid_choices()
        bad_json["message"]["content"] = "not json at all"
        not_object = valid_choices()
        not_object["message"]["content"] = "[1, 2, 3]"
        empty = valid_choices()
        empty["message"]["content"] = "   "

        error_body = valid_body()
        error_body["error"] = {"code": 500, "message": CANARY}
        missing_choices = valid_body()
        del missing_choices["choices"]
        bad_usage = valid_body()
        bad_usage["usage"] = "not an object"

        observed = {
            "model_mismatch": self._code(resp(200, valid_body(model="openai/other"))),
            "finish_length": self._code(resp(200, valid_body(choices=[length]))),
            "finish_not_stop": self._code(resp(200, valid_body(choices=[other_finish]))),
            "content_not_json": self._code(resp(200, valid_body(choices=[bad_json]))),
            "content_not_object": self._code(resp(200, valid_body(choices=[not_object]))),
            "content_empty": self._code(resp(200, valid_body(choices=[empty]))),
            "error_object": self._code(resp(200, error_body)),
            "choices_malformed": self._code(resp(200, missing_choices)),
            "usage_malformed": self._code(resp(200, bad_usage)),
            "envelope_not_json": self._code(resp(200, "<html>nope</html>")),
            "transport_malformed": self._code("not-a-transport-response"),
        }
        # Every branch is a distinct, static code (no collapsed generic code).
        self.assertEqual(
            observed["finish_length"], "response_finish_length"
        )
        self.assertEqual(observed["content_not_json"], "response_content_not_json")
        self.assertEqual(observed["envelope_not_json"], "response_envelope_not_json")
        self.assertEqual(len(set(observed.values())), len(observed))
        self.assertNotIn("protocol_error", set(observed.values()))
        for code in observed.values():
            self.assertIn(code, extraction.RESPONSE_DIAGNOSTIC_CODES)

    def test_diagnostic_code_never_carries_peer_text(self):
        # The provider error object and the (untrusted) content carry the canary;
        # the diagnostic code and its redacted detail must not.
        error_body = valid_body()
        error_body["error"] = {"code": 400, "message": f"{API_KEY} {CANARY}"}
        result = run(FakeTransport(resp(200, error_body)))
        self.assertEqual(result.diagnostic.code, "response_error_object")
        self.assertNotIn(API_KEY, result.diagnostic.code)
        self.assertNotIn(CANARY, result.diagnostic.code)
        self.assertNotIn(CANARY, result.diagnostic.detail_redacted)

        bad_content = valid_choices()
        bad_content["message"]["content"] = f"{CANARY} not json"
        result = run(FakeTransport(resp(200, valid_body(choices=[bad_content]))))
        self.assertNotIn(CANARY, result.diagnostic.code)
        self.assertNotIn(CANARY, result.diagnostic.detail_redacted)

    def test_known_cost_preserved_on_classify_failure(self):
        # A reported cost survives a parser-branch failure (the reserve is not
        # released and the amount is never zeroed).
        result = run(FakeTransport(resp(200, valid_body(model="openai/other"))))
        self.assertEqual(result.outcome, "protocol_error")
        self.assertEqual(result.diagnostic.code, "response_model_mismatch")
        self.assertEqual(result.cost.state, "known")
        self.assertEqual(result.cost.amount_usd, "0.0001024")


# --------------------------------------------------------------------------- #
# Ambiguity / cost accounting
# --------------------------------------------------------------------------- #
class AmbiguityTests(unittest.TestCase):
    def test_status_resolution_matrix(self):
        # Status/transport outcome -> cost-state matrix. Every former standalone
        # test is preserved verbatim as a labelled subTest case.
        def case_test_ambiguous_statuses_keep_unknown():
            for status in (429, 500, 502, 503, 504, 301, 302, 201, 204):
                body = json.dumps({"error": {"code": status, "message": CANARY}})
                result = run(FakeTransport(lead.TransportResponse(status, body)))
                self.assertEqual(result.outcome, "unknown", status)
                self.assertEqual(result.cost.state, "unknown", status)
                self.assertTrue(result.cost.reserve_retained, status)
                self.assertIsNone(result.cost.amount_usd, status)

        def case_test_definitive_statuses_are_not_incurred():
            for status in (400, 401, 402, 403, 404, 413):
                body = json.dumps({"error": {"code": status, "message": CANARY}})
                result = run(FakeTransport(lead.TransportResponse(status, body)))
                self.assertEqual(result.outcome, "failed", status)
                self.assertEqual(result.cost.state, "not_incurred", status)
                self.assertFalse(result.cost.reserve_retained, status)

        def case_test_network_transport_error_is_unknown_retained():
            transport = FakeTransport(raises=lead.LeadTransportError("dropped"))
            result = run(transport)
            self.assertEqual(result.outcome, "unknown")
            self.assertEqual(result.calls_attempted, 1)
            self.assertEqual(len(transport.calls), 1)
            self.assertEqual(result.cost.state, "unknown")
            self.assertTrue(result.cost.reserve_retained)
            self.assertIsNone(result.cost.amount_usd)

        def case_test_unexpected_transport_exception_is_unknown_retained():
            result = run(FakeTransport(raises=RuntimeError("kaboom")))
            self.assertEqual(result.outcome, "unknown")
            self.assertEqual(result.cost.state, "unknown")
            self.assertTrue(result.cost.reserve_retained)

        def case_test_malformed_200_without_cost_stays_unknown():
            body = valid_body()
            body["choices"] = ["not", "a", "map"]
            body["usage"].pop("cost", None)
            result = run(FakeTransport(resp(200, body)))
            self.assertEqual(result.outcome, "protocol_error")
            self.assertEqual(result.cost.state, "unknown")
            self.assertTrue(result.cost.reserve_retained)
            self.assertNotEqual(result.cost.state, "not_incurred")

        def case_test_malformed_200_with_valid_cost_is_known():
            body = valid_body()
            body["choices"] = ["not", "a", "map"]
            result = run(FakeTransport(resp(200, body)))
            self.assertEqual(result.outcome, "protocol_error")
            self.assertEqual(result.cost.state, "known")
            self.assertEqual(result.cost.amount_usd, "0.0001024")

        def case_test_invalid_json_body_is_protocol_error_unknown():
            result = run(FakeTransport(resp(200, "<html>nope</html>")))
            self.assertEqual(result.outcome, "protocol_error")
            self.assertEqual(result.cost.state, "unknown")
            self.assertTrue(result.cost.reserve_retained)

        def case_test_oversize_response_is_protocol_error():
            result = run(
                FakeTransport(lead.TransportResponse(200, "x" * 50, truncated=True)),
                max_response_bytes=100000,
            )
            self.assertEqual(result.outcome, "protocol_error")
            self.assertEqual(result.cost.state, "unknown")
            self.assertTrue(result.cost.reserve_retained)

        def case_test_response_over_configured_bound():
            result = run(
                FakeTransport(lead.TransportResponse(200, json.dumps(valid_body()))),
                max_response_bytes=10,
            )
            self.assertEqual(result.outcome, "protocol_error")
            self.assertEqual(result.cost.state, "unknown")

        def case_test_huge_exponent_cost_is_bounded():
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

        cases = [
            ("test_ambiguous_statuses_keep_unknown", case_test_ambiguous_statuses_keep_unknown),
            ("test_definitive_statuses_are_not_incurred", case_test_definitive_statuses_are_not_incurred),
            ("test_network_transport_error_is_unknown_retained", case_test_network_transport_error_is_unknown_retained),
            ("test_unexpected_transport_exception_is_unknown_retained", case_test_unexpected_transport_exception_is_unknown_retained),
            ("test_malformed_200_without_cost_stays_unknown", case_test_malformed_200_without_cost_stays_unknown),
            ("test_malformed_200_with_valid_cost_is_known", case_test_malformed_200_with_valid_cost_is_known),
            ("test_invalid_json_body_is_protocol_error_unknown", case_test_invalid_json_body_is_protocol_error_unknown),
            ("test_oversize_response_is_protocol_error", case_test_oversize_response_is_protocol_error),
            ("test_response_over_configured_bound", case_test_response_over_configured_bound),
            ("test_huge_exponent_cost_is_bounded", case_test_huge_exponent_cost_is_bounded),
        ]
        for label, case in cases:
            with self.subTest(case=label):
                case()


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
                extraction.execute_extraction(
                    self._valid_plan(), api_key=bad, transport=transport
                )
        self.assertEqual(transport.calls, [])

    def test_forged_purpose_refused(self):
        forged = dataclasses.replace(self._valid_plan(), purpose="other")
        with self.assertRaises(lead.LeadValidationError):
            extraction.execute_extraction(
                forged, api_key=API_KEY, transport=FakeTransport()
            )

    def test_forged_text_refused(self):
        forged = dataclasses.replace(self._valid_plan(), text="MUTATED")
        transport = FakeTransport(resp(200, valid_body()))
        with self.assertRaises(lead.LeadValidationError):
            extraction.execute_extraction(forged, api_key=API_KEY, transport=transport)
        self.assertEqual(transport.calls, [])

    def test_forged_instructions_refused(self):
        forged = dataclasses.replace(self._valid_plan(), instructions="MUTATED")
        with self.assertRaises(lead.LeadValidationError):
            extraction.execute_extraction(
                forged, api_key=API_KEY, transport=FakeTransport()
            )

    def test_forged_body_refused(self):
        forged = dataclasses.replace(self._valid_plan(), request_body=b"{}")
        with self.assertRaises(lead.LeadValidationError):
            extraction.execute_extraction(
                forged, api_key=API_KEY, transport=FakeTransport()
            )
        self.assertEqual(forged.request_body, b"{}")

    def test_forged_digest_refused(self):
        forged = dataclasses.replace(self._valid_plan(), request_sha256="0" * 64)
        with self.assertRaises(lead.LeadValidationError):
            extraction.execute_extraction(
                forged, api_key=API_KEY, transport=FakeTransport()
            )

    def test_forged_reserve_refused(self):
        forged = dataclasses.replace(self._valid_plan(), reserve_usd="0")
        with self.assertRaises(lead.LeadValidationError):
            extraction.execute_extraction(
                forged, api_key=API_KEY, transport=FakeTransport()
            )

    def test_forged_bound_evidence_refused(self):
        forged = dataclasses.replace(self._valid_plan(), bound_evidence={"a": 1})
        with self.assertRaises(lead.LeadValidationError):
            extraction.execute_extraction(
                forged, api_key=API_KEY, transport=FakeTransport()
            )

    def test_forged_config_refused(self):
        forged = dataclasses.replace(self._valid_plan(), config=cfg(model_id="openai/other"))
        with self.assertRaises(lead.LeadError):
            extraction.execute_extraction(
                forged, api_key=API_KEY, transport=FakeTransport()
            )

    def test_forged_endpoint_refused(self):
        forged = dataclasses.replace(
            self._valid_plan(), endpoint="https://evil.example/x"
        )
        with self.assertRaises(lead.LeadError):
            extraction.execute_extraction(
                forged, api_key=API_KEY, transport=FakeTransport()
            )

    def test_forged_payload_refused(self):
        forged = dataclasses.replace(self._valid_plan(), payload={"model": MODEL})
        with self.assertRaises(lead.LeadValidationError):
            extraction.execute_extraction(
                forged, api_key=API_KEY, transport=FakeTransport()
            )

    def test_non_plan_refused(self):
        with self.assertRaises(lead.LeadValidationError):
            extraction.execute_extraction(
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
        source = Path(extraction.__file__).read_text(encoding="utf-8")
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
        source = Path(extraction.__file__).read_text(encoding="utf-8")
        self.assertTrue(hasattr(extraction, "ExtractionResult"))
        result = run(FakeTransport(resp(200, valid_body())))
        self.assertFalse(result.is_authoritative())


# --------------------------------------------------------------------------- #
# Installed-package shape
# --------------------------------------------------------------------------- #
class InstalledPackageTests(unittest.TestCase):
    def test_module_is_a_package_submodule(self):
        self.assertEqual(
            extraction.__name__, "review_article_agent.runtime.extraction"
        )

    def test_endpoint_is_the_documented_one(self):
        self.assertEqual(
            extraction.ENDPOINT, "https://openrouter.ai/api/v1/chat/completions"
        )


if __name__ == "__main__":
    unittest.main()
