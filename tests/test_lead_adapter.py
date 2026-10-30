"""Offline regression tests for the production direct-OpenRouter lead adapter.

Covers ``src/review_article_agent/runtime/lead.py`` against the acceptance
criteria of task M1:

* a strict frozen ``LeadConfig.from_mapping`` rejects missing/unknown/invalid
  fields, unbounded prices, and a context below the maximum output;
* the reserve is the **exact** full-context-plus-max-output bound, independent of
  the prompt length (never a token estimate);
* an invalid config/credential/forged plan is refused with **zero** I/O;
* success carries the exact request body/model/provider caps and a parsed Decimal
  cost; error/refusal/tool-call/length/wrong-model are never success;
* ambiguous HTTP (429/5xx/3xx) and a transport failure keep the reserve
  ``unknown`` (never ``0``); a malformed 200 is never ``not_incurred``;
* exactly one POST is attempted and nothing is ever retried; redirects/proxies
  are refused; no credential is discovered from the environment.

No network, no credential, no paid call: the transport is always a local fixture.
Run:

    PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_lead_adapter.py'
"""

from __future__ import annotations

import dataclasses
import hashlib
import io
import json
import sys
import unittest
import urllib.error
from decimal import Decimal, localcontext
from pathlib import Path
from unittest import mock

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from review_article_agent.runtime import lead  # noqa: E402
from review_article_agent.domain.records import canonical_json  # noqa: E402

API_KEY = "sk-or-v1-supersecret-should-never-appear"
CANARY = "PRIVATE_SOURCE_TEXT_CANARY"
MODEL = "deepseek/deepseek-v4.1-flash"


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
        "pricing_ref": "openrouter/deepseek/deepseek-v4.1-flash@2026-10-28",
        "instructions_revision": "lead-adapter-v1",
        "timeout_seconds": 60.0,
        "max_response_bytes": 100000,
    }
    base.update(overrides)
    return base


def cfg(**overrides):
    return lead.LeadConfig.from_mapping(config_mapping(**overrides))


def valid_choices(**overrides):
    choice = {
        "index": 0,
        "finish_reason": "stop",
        "message": {"role": "assistant", "content": '{"proposal": "draft"}'},
    }
    choice.update(overrides)
    return choice


def valid_body(**overrides):
    payload = {
        "id": "gen-lead-test",
        "model": MODEL,
        "provider": "DeepSeek",
        "choices": [valid_choices()],
        "usage": {
            "prompt_tokens": 120,
            "completion_tokens": 40,
            "total_tokens": 160,
            "cost": 0.00002037,
        },
    }
    payload.update(overrides)
    return payload


class FakeTransport:
    """Records every call; returns a canned response or raises.

    Implements the documented transport protocol, including the configured
    ``max_response_bytes`` cap passed by :func:`execute_lead`.
    """

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
    text = overrides.pop("text", "Draft a brief about X.")
    purpose = overrides.pop("purpose", "interview")
    return lead.plan_lead(cfg(**overrides), purpose=purpose, text=text)


def run(transport, *, text="Draft a brief about X.", purpose="interview", **cfg_overrides):
    the_plan = lead.plan_lead(cfg(**cfg_overrides), purpose=purpose, text=text)
    return lead.execute_lead(the_plan, api_key=API_KEY, transport=transport)


# --------------------------------------------------------------------------- #
# LeadConfig
# --------------------------------------------------------------------------- #
class ConfigTests(unittest.TestCase):
    def test_valid_config_normalizes_prices(self):
        config = cfg()
        self.assertEqual(config.model_id, MODEL)
        self.assertIsInstance(config.input_price_per_million, Decimal)
        self.assertEqual(config.input_price_per_million, Decimal("0.10"))

    def test_invalid_configs_refused(self):
        # One table per validator: every rejected LeadConfig construction.
        def missing_field():
            mapping = config_mapping()
            del mapping["pricing_ref"]
            return lead.LeadConfig.from_mapping(mapping)

        def direct_construction():
            return lead.LeadConfig(
                model_id="",
                upstream_provider="deepseek",
                max_output_tokens=1,
                context_tokens=1,
                input_price_per_million=Decimal("1"),
                output_price_per_million=Decimal("1"),
                pricing_ref="r",
                instructions_revision="i",
                timeout_seconds=1.0,
                max_response_bytes=1,
            )

        cases = {
            "missing_field": missing_field,
            "unknown_field": lambda: lead.LeadConfig.from_mapping(config_mapping(tools=[])),
            "non_mapping_none": lambda: lead.LeadConfig.from_mapping(None),
            "non_mapping_list": lambda: lead.LeadConfig.from_mapping([]),
            "non_mapping_str": lambda: lead.LeadConfig.from_mapping("x"),
            "non_mapping_int": lambda: lead.LeadConfig.from_mapping(3),
            "missing_value_none": lambda: cfg(model_id=None),
            "missing_value_empty": lambda: cfg(model_id=""),
            "context_below_max_output": lambda: cfg(context_tokens=1024, max_output_tokens=4096),
            "input_price_1e999999": lambda: cfg(input_price_per_million="1e999999"),
            "input_price_0e-999999": lambda: cfg(input_price_per_million="0e-999999"),
            "input_price_1e25": lambda: cfg(input_price_per_million="1e25"),
            "input_price_zero_int": lambda: cfg(input_price_per_million=0),
            "input_price_negative": lambda: cfg(input_price_per_million="-1"),
            "input_price_zero_str": lambda: cfg(input_price_per_million="0"),
            "input_price_inf": lambda: cfg(input_price_per_million=float("inf")),
            "input_price_bool": lambda: cfg(input_price_per_million=True),
            "output_price_1e999999": lambda: cfg(output_price_per_million="1e999999"),
            "output_price_float": lambda: cfg(output_price_per_million=0.4),
            "output_price_zero": lambda: cfg(output_price_per_million=0),
            "output_price_negative": lambda: cfg(output_price_per_million="-2"),
            "output_price_bool": lambda: cfg(output_price_per_million=True),
            "max_output_tokens_bool": lambda: cfg(max_output_tokens=True),
            "max_output_tokens_huge": lambda: cfg(max_output_tokens=10 ** 30),
            "context_tokens_huge": lambda: cfg(context_tokens=10 ** 30),
            "max_response_bytes_huge": lambda: cfg(max_response_bytes=10 ** 30),
            "timeout_nan": lambda: cfg(timeout_seconds=float("nan")),
            "timeout_inf": lambda: cfg(timeout_seconds=float("inf")),
            "timeout_negative_inf": lambda: cfg(timeout_seconds=float("-inf")),
            "timeout_zero": lambda: cfg(timeout_seconds=0),
            "timeout_negative": lambda: cfg(timeout_seconds=-1),
            "timeout_over_cap": lambda: cfg(timeout_seconds=1e9),
            "timeout_bool": lambda: cfg(timeout_seconds=True),
            "provider_whitespace": lambda: cfg(upstream_provider="deep seek"),
            "model_id_whitespace": lambda: cfg(model_id=" deepseek/model"),
            "direct_construction": direct_construction,
        }
        for label, build in cases.items():
            with self.subTest(case=label):
                with self.assertRaises(lead.LeadValidationError):
                    build()


# --------------------------------------------------------------------------- #
# Planning: payload + exact reserve
# --------------------------------------------------------------------------- #
class PlanTests(unittest.TestCase):
    def test_plan_payload_shape(self):
        the_plan = plan()
        payload = json.loads(the_plan.request_body)
        self.assertEqual(payload["model"], MODEL)
        self.assertIs(payload["stream"], False)
        self.assertEqual(payload["max_tokens"], 4096)
        provider = payload["provider"]
        self.assertEqual(provider["only"], ["deepseek"])
        self.assertIs(provider["allow_fallbacks"], False)
        self.assertIs(provider["require_parameters"], True)
        self.assertEqual(provider["data_collection"], "deny")
        self.assertEqual(provider["max_price"], {"prompt": "0.10", "completion": "0.40"})
        self.assertEqual([m["role"] for m in payload["messages"]], ["system", "user"])
        self.assertEqual(len(payload["messages"]), 2)

    def test_reserve_is_exact_full_capacity_bound(self):
        the_plan = plan()
        expected = (
            Decimal(131072) * Decimal("0.10") / Decimal(1_000_000)
            + Decimal(4096) * Decimal("0.40") / Decimal(1_000_000)
        )
        self.assertEqual(Decimal(the_plan.reserve_usd), expected)
        self.assertEqual(the_plan.reserve_usd, "0.0147456")

    def test_reserve_independent_of_prompt_length(self):
        short = plan(text="short")
        long = plan(text="x" * 20000)
        self.assertEqual(short.reserve_usd, long.reserve_usd)
        self.assertNotEqual(short.request_sha256, long.request_sha256)

    def test_reserve_independent_of_ambient_precision(self):
        with localcontext() as ctx:
            ctx.prec = 2
            the_plan = plan()
        self.assertEqual(the_plan.reserve_usd, "0.0147456")

    def test_bound_evidence_recorded(self):
        evidence = dict(plan().bound_evidence)
        self.assertEqual(evidence["reserve_basis"], "context_capacity_prompt_plus_max_output_completion")
        self.assertEqual(evidence["context_tokens"], 131072)
        self.assertEqual(evidence["max_output_tokens"], 4096)
        self.assertEqual(evidence["input_price_per_million"], "0.10")
        self.assertEqual(evidence["output_price_per_million"], "0.40")
        self.assertEqual(evidence["pricing_ref"], "openrouter/deepseek/deepseek-v4.1-flash@2026-10-28")
        self.assertEqual(evidence["reserve_usd"], "0.0147456")

    def test_system_prompt_states_untrusted_and_no_authority(self):
        payload = json.loads(plan().request_body)
        system = payload["messages"][0]["content"]
        self.assertIn("UNTRUSTED", system)
        self.assertIn("no authority", system)
        self.assertIn("cannot approve", system)

    def test_purpose_required(self):
        for bad in ("other", "", None, 3):
            with self.assertRaises(lead.LeadValidationError):
                lead.plan_lead(cfg(), purpose=bad, text="x")
        # both documented purposes plan
        for good in lead.PURPOSES:
            lead.plan_lead(cfg(), purpose=good, text="x")

    def test_text_must_be_a_string(self):
        with self.assertRaises(lead.LeadValidationError):
            lead.plan_lead(cfg(), purpose="interview", text=3)

    def test_oversize_input_refused(self):
        with self.assertRaises(lead.LeadValidationError):
            lead.plan_lead(
                cfg(), purpose="interview", text="x" * (lead.MAX_REQUEST_BYTES + 1)
            )

    def test_plan_is_deterministic(self):
        a = plan()
        b = plan()
        self.assertEqual(a.request_sha256, b.request_sha256)
        self.assertEqual(a.request_bytes, b.request_bytes)

    def test_request_body_is_frozen_and_immutable(self):
        the_plan = plan()
        with self.assertRaises(TypeError):
            the_plan.payload["messages"] = ()
        with self.assertRaises(TypeError):
            the_plan.payload["provider"]["data_collection"] = "allow"


# --------------------------------------------------------------------------- #
# Success path
# --------------------------------------------------------------------------- #
class SuccessTests(unittest.TestCase):
    def test_completed_and_sent_body_matches_plan(self):
        the_plan = plan()
        transport = FakeTransport(resp(200, valid_body()))
        result = lead.execute_lead(the_plan, api_key=API_KEY, transport=transport)
        self.assertEqual(result.outcome, "completed")
        self.assertEqual(result.calls_attempted, 1)
        self.assertTrue(result.no_automatic_retry)
        self.assertEqual(transport.calls[0]["body"], the_plan.request_body)
        self.assertEqual(
            hashlib.sha256(transport.calls[0]["body"]).hexdigest(), result.request_sha256
        )
        self.assertEqual(transport.calls[0]["endpoint"], lead.ENDPOINT)

    def test_actual_model_provider_usage(self):
        result = run(FakeTransport(resp(200, valid_body())))
        self.assertEqual(result.actual_model, MODEL)
        self.assertEqual(result.provider, "DeepSeek")
        self.assertEqual(result.response_id, "gen-lead-test")
        self.assertEqual(result.usage.prompt_tokens, 120)
        self.assertEqual(result.usage.completion_tokens, 40)
        self.assertEqual(result.usage.total_tokens, 160)

    def test_proposal_is_data_only(self):
        result = run(FakeTransport(resp(200, valid_body())))
        self.assertEqual(result.proposal_text, '{"proposal": "draft"}')
        self.assertEqual(dict(result.proposal_json), {"proposal": "draft"})
        self.assertFalse(result.is_authoritative())

    def test_non_json_proposal_text_is_kept_as_text(self):
        body = valid_body()
        body["choices"][0]["message"]["content"] = "A prose proposal."
        result = run(FakeTransport(resp(200, body)))
        self.assertEqual(result.outcome, "completed")
        self.assertEqual(result.proposal_text, "A prose proposal.")
        self.assertIsNone(result.proposal_json)

    def test_reported_cost_is_exact_decimal(self):
        result = run(FakeTransport(resp(200, valid_body())))
        self.assertEqual(result.cost.state, "known")
        self.assertEqual(result.cost.amount_usd, "0.00002037")
        self.assertEqual(Decimal(result.cost.amount_usd), Decimal("0.00002037"))
        self.assertFalse(result.cost.reserve_retained)

    def test_zero_cost_is_known_not_unknown(self):
        body = valid_body()
        body["usage"]["cost"] = 0
        result = run(FakeTransport(resp(200, body)))
        self.assertEqual(result.cost.state, "known")
        self.assertEqual(result.cost.amount_usd, "0")

    def test_completed_without_cost_is_unknown_retained(self):
        body = valid_body()
        body["usage"] = {"prompt_tokens": 1, "completion_tokens": 1}
        result = run(FakeTransport(resp(200, body)))
        self.assertEqual(result.outcome, "completed")
        self.assertEqual(result.cost.state, "unknown")
        self.assertTrue(result.cost.reserve_retained)
        self.assertIsNone(result.cost.amount_usd)

    def test_cost_carries_reserve_and_bucket(self):
        result = run(FakeTransport(resp(200, valid_body())))
        self.assertEqual(result.cost.reserve_usd, result.reserve_usd)
        self.assertEqual(result.cost.route, "direct_call")
        self.assertEqual(result.cost.accounting_bucket, "direct_call")

    def test_sent_body_matches_plan_bytes(self):
        the_plan = plan()
        transport = FakeTransport(resp(200, valid_body()))
        result = lead.execute_lead(the_plan, api_key=API_KEY, transport=transport)
        self.assertEqual(len(transport.calls[0]["body"]), result.request_bytes)
        self.assertEqual(transport.calls[0]["body"], the_plan.request_body)


# --------------------------------------------------------------------------- #
# Never a silent success
# --------------------------------------------------------------------------- #
class NeverSuccessTests(unittest.TestCase):
    def test_never_a_silent_success(self):
        # One table per validator: each malformed/refused 200 becomes a subTest.
        def wrong_model():
            return valid_body(model="openai/other-model")

        def length_finish():
            return valid_body(choices=[valid_choices(finish_reason="length")])

        def tool_calls():
            choice = valid_choices()
            choice["message"]["tool_calls"] = [{"id": "t1", "type": "function"}]
            return valid_body(choices=[choice])

        def refusal():
            choice = valid_choices()
            choice["message"]["refusal"] = "I cannot help with that."
            choice["message"]["content"] = ""
            return valid_body(choices=[choice])

        def content_filter():
            return valid_body(choices=[valid_choices(finish_reason="content_filter")])

        def top_level_error():
            body = valid_body()
            body["error"] = {"code": 500, "message": CANARY}
            return body

        def missing_choices():
            body = valid_body()
            del body["choices"]
            return body

        def multiple_choices():
            return valid_body(choices=[valid_choices(), valid_choices()])

        def non_text_content():
            choice = valid_choices()
            choice["message"]["content"] = [{"type": "text", "text": "hi"}]
            return valid_body(choices=[choice])

        def empty_content():
            choice = valid_choices()
            choice["message"]["content"] = "   "
            return valid_body(choices=[choice])

        def invalid_usage_tokens():
            body = valid_body()
            body["usage"]["prompt_tokens"] = -1
            return body

        def wrong_model_extra(result):
            self.assertIsNone(result.proposal_text)
            self.assertEqual(result.cost.state, "known")
            self.assertEqual(result.cost.amount_usd, "0.00002037")

        def refusal_extra(result):
            self.assertIsNone(result.proposal_text)

        cases = [
            ("wrong_model", wrong_model, "protocol_error", wrong_model_extra),
            ("length_finish", length_finish, "protocol_error", None),
            ("tool_calls", tool_calls, "protocol_error", None),
            ("refusal", refusal, "refused", refusal_extra),
            ("content_filter", content_filter, "refused", None),
            ("top_level_error", top_level_error, "protocol_error", None),
            ("missing_choices", missing_choices, "protocol_error", None),
            ("multiple_choices", multiple_choices, "protocol_error", None),
            ("non_text_content", non_text_content, "protocol_error", None),
            ("empty_content", empty_content, "protocol_error", None),
            ("invalid_usage_tokens", invalid_usage_tokens, "protocol_error", None),
        ]
        for label, build, expected, extra in cases:
            with self.subTest(case=label):
                result = run(FakeTransport(resp(200, build())))
                self.assertEqual(result.outcome, expected)
                if extra is not None:
                    extra(result)


# --------------------------------------------------------------------------- #
# Ambiguity / cost accounting
# --------------------------------------------------------------------------- #
class AmbiguityTests(unittest.TestCase):
    def test_ambiguous_statuses_keep_unknown(self):
        for status in (429, 500, 501, 502, 503, 504, 507, 524, 529, 301, 302, 201, 204):
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

    def test_transport_error_is_unknown_retained(self):
        transport = FakeTransport(raises=lead.LeadTransportError("dropped"))
        result = run(transport)
        self.assertEqual(result.outcome, "unknown")
        self.assertEqual(result.calls_attempted, 1)
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(result.cost.state, "unknown")
        self.assertTrue(result.cost.reserve_retained)

    def test_unexpected_transport_exception_still_unknown(self):
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
        self.assertEqual(result.cost.amount_usd, "0.00002037")

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
        body = json.dumps(valid_body())
        result = run(
            FakeTransport(lead.TransportResponse(200, body)),
            max_response_bytes=10,
        )
        self.assertEqual(result.outcome, "protocol_error")
        self.assertEqual(result.cost.state, "unknown")

    def test_huge_exponent_cost_is_bounded(self):
        raw = (
            '{"model":"%s","choices":[{"finish_reason":"stop",'
            '"message":{"role":"assistant","content":"ok"}}],'
            '"usage":{"prompt_tokens":1,"completion_tokens":1,"cost":1e999999999}}'
            % MODEL
        )
        result = run(FakeTransport(resp(200, raw)))
        self.assertEqual(result.outcome, "completed")
        self.assertEqual(result.cost.state, "unknown")
        self.assertTrue(result.cost.reserve_retained)


# --------------------------------------------------------------------------- #
# Guards: zero I/O on refusal, no retry, redirects/proxies, secrets
# --------------------------------------------------------------------------- #
class GuardTests(unittest.TestCase):
    def _valid_plan(self):
        return plan()

    def test_missing_credential_refused_zero_calls(self):
        transport = FakeTransport(resp(200, valid_body()))
        for bad in (None, "", 0, 123):
            with self.assertRaises(lead.LeadCredentialError):
                lead.execute_lead(self._valid_plan(), api_key=bad, transport=transport)
        self.assertEqual(transport.calls, [])

    def test_forged_plans_refused(self):
        # One table per validator: each forged/foreign plan is refused with zero I/O.
        def forged_model():
            return dataclasses.replace(self._valid_plan(), config=cfg(model_id="openai/other"))

        def forged_text():
            return dataclasses.replace(self._valid_plan(), text="MUTATED")

        def forged_body():
            return dataclasses.replace(self._valid_plan(), request_body=b"{}")

        def forged_reserve():
            return dataclasses.replace(self._valid_plan(), reserve_usd="0")

        def forged_bound_evidence():
            return dataclasses.replace(self._valid_plan(), bound_evidence={"a": 1})

        def forged_endpoint():
            return dataclasses.replace(self._valid_plan(), endpoint="https://evil.example/x")

        def forged_payload():
            return dataclasses.replace(self._valid_plan(), payload={"model": MODEL})

        cases = [
            ("forged_model", forged_model, lead.LeadError),
            ("forged_text", forged_text, lead.LeadValidationError),
            ("forged_body", forged_body, lead.LeadValidationError),
            ("forged_reserve", forged_reserve, lead.LeadValidationError),
            ("forged_bound_evidence", forged_bound_evidence, lead.LeadValidationError),
            ("forged_endpoint", forged_endpoint, lead.LeadError),
            ("forged_payload", forged_payload, lead.LeadValidationError),
        ]
        for label, build, expected in cases:
            with self.subTest(case=label):
                transport = FakeTransport(resp(200, valid_body()))
                forged = build()
                with self.assertRaises(expected):
                    lead.execute_lead(forged, api_key=API_KEY, transport=transport)
                self.assertEqual(transport.calls, [])

        with self.subTest(case="forged_body_is_not_rewritten"):
            forged = dataclasses.replace(self._valid_plan(), request_body=b"{}")
            with self.assertRaises(lead.LeadValidationError):
                lead.execute_lead(forged, api_key=API_KEY, transport=FakeTransport())
            self.assertEqual(forged.request_body, b"{}")

        with self.subTest(case="non_plan"):
            with self.assertRaises(lead.LeadValidationError):
                lead.execute_lead({"nope": 1}, api_key=API_KEY, transport=FakeTransport())

    def test_no_retry_single_call(self):
        transport = FakeTransport(resp(200, valid_body()))
        run(transport)
        self.assertEqual(len(transport.calls), 1)

    def test_post_chat_refuses_other_endpoint(self):
        with self.assertRaises(lead.LeadEndpointError):
            lead.post_chat(b"{}", API_KEY, endpoint="https://evil.example/x")

    def test_post_chat_requires_key(self):
        with self.assertRaises(lead.LeadCredentialError):
            lead.post_chat(b"{}", "", endpoint=lead.ENDPOINT)

    def test_redirect_is_refused(self):
        opener = lead._http_opener()
        handler = next(h for h in opener.handlers if isinstance(h, lead._NoRedirect))

        class _Req:
            full_url = lead.ENDPOINT

        with self.assertRaises(urllib.error.HTTPError) as ctx:
            handler.http_error_302(_Req(), io.BytesIO(b""), 302, "Found", {})
        self.assertEqual(ctx.exception.code, 302)
        ctx.exception.close()

    def test_proxy_handler_suppressed_even_when_environment_configured(self):
        import urllib.request

        with mock.patch.dict(
            "os.environ",
            {"HTTP_PROXY": "http://evil:8080", "HTTPS_PROXY": "http://evil:8080"},
        ):
            with mock.patch.object(
                urllib.request,
                "getproxies",
                return_value={"http": "http://evil:8080"},
            ):
                opener = lead._http_opener()
        self.assertFalse(
            any(isinstance(h, urllib.request.ProxyHandler) for h in opener.handlers)
        )
        self.assertTrue(any(isinstance(h, lead._NoRedirect) for h in opener.handlers))

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

    def test_module_does_not_discover_credentials(self):
        source = Path(lead.__file__).read_text(encoding="utf-8")
        for forbidden in ("os.environ", "os.getenv", "getenv(", "dotenv", "load_dotenv", "Path.home()"):
            self.assertNotIn(forbidden, source, forbidden)

    def test_redact_peer_text(self):
        out = lead.redact_peer_text("failed sk-or-v1-abc123 with Bearer sk-or-v1-xyz\nnext")
        self.assertNotIn("sk-or-", out)
        self.assertNotIn("\n", out)


# --------------------------------------------------------------------------- #
# CostRecord invariants
# --------------------------------------------------------------------------- #
class CostRecordTests(unittest.TestCase):
    def test_unknown_requires_no_amount(self):
        record = lead.CostRecord(state="unknown", reserve_usd="0.25", reserve_retained=True)
        self.assertIsNone(record.amount_usd)
        self.assertEqual(record.route, "direct_call")

    def test_invalid_cost_records_refused(self):
        cases = {
            "known_requires_amount": {"state": "known"},
            "not_incurred_rejects_amount": {"state": "not_incurred", "amount_usd": "0.1"},
            "unknown_rejects_zero_amount": {"state": "unknown", "amount_usd": "0"},
            "bucket_mismatch_rejected": {
                "state": "unknown",
                "reserve_usd": "1",
                "reserve_retained": True,
                "accounting_bucket": "session_statistics",
            },
            "reserve_retained_must_match_state": {
                "state": "unknown",
                "reserve_usd": "1",
                "reserve_retained": False,
            },
        }
        for label, kwargs in cases.items():
            with self.subTest(case=label):
                with self.assertRaises(lead.LeadValidationError):
                    lead.CostRecord(**kwargs)


# --------------------------------------------------------------------------- #
# Installed-package shape
# --------------------------------------------------------------------------- #
class InstalledPackageTests(unittest.TestCase):
    def test_module_is_a_package_submodule(self):
        self.assertEqual(lead.__name__, "review_article_agent.runtime.lead")

    def test_endpoint_is_the_documented_one(self):
        self.assertEqual(lead.ENDPOINT, "https://openrouter.ai/api/v1/chat/completions")


# --------------------------------------------------------------------------- #
# Focused repair (M1-R1): UTF-8 byte cap, bounded real read, portable config
# --------------------------------------------------------------------------- #
class RepairR1Tests(unittest.TestCase):
    def test_multibyte_body_over_utf8_cap_is_protocol_error(self):
        # 100 code points, 400 UTF-8 bytes: the code-point length is under the
        # cap while the UTF-8 byte length is over it.
        content = "\U0001f9ea" * 100
        body = {
            "model": MODEL,
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": content},
                }
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "cost": 0.00002037},
        }
        text = json.dumps(body, ensure_ascii=False)
        char_len = len(text)
        byte_len = len(text.encode("utf-8"))
        self.assertGreater(byte_len, char_len)
        cap = char_len  # old (str-length) check would NOT flag this body
        self.assertLessEqual(char_len, cap)
        self.assertGreater(byte_len, cap)
        result = run(
            FakeTransport(lead.TransportResponse(200, text)), max_response_bytes=cap
        )
        self.assertEqual(result.outcome, "protocol_error")
        self.assertIsNone(result.proposal_text)
        self.assertEqual(result.cost.state, "unknown")
        self.assertTrue(result.cost.reserve_retained)
        self.assertNotEqual(result.cost.state, "not_incurred")

    def test_utf8_cap_helper_counts_bytes_not_code_points(self):
        text = "\U0001f9ea"
        self.assertEqual(lead._utf8_len(text), 4)
        self.assertEqual(lead._utf8_len("ascii"), 5)

    def test_real_transport_reads_at_configured_cap(self):
        recorded = {}

        class _FakeResponse:
            status = 200

            def __init__(self, data):
                self._data = data

            def read(self, n):
                recorded["n"] = n
                return self._data[:n]

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        class _FakeOpener:
            def open(self, request, timeout=None):
                recorded["timeout"] = timeout
                return _FakeResponse(b"x" * 500)

        with mock.patch.object(lead, "_http_opener", return_value=_FakeOpener()):
            response = lead.post_chat(
                b"{}",
                API_KEY,
                endpoint=lead.ENDPOINT,
                timeout=1.0,
                max_response_bytes=375,
            )
        self.assertEqual(recorded["n"], 376)
        self.assertTrue(response.truncated)
        self.assertEqual(len(response.body_text), 375)

    def test_real_transport_default_cap_is_global(self):
        recorded = {}

        class _FakeResponse:
            status = 200

            def read(self, n):
                recorded["n"] = n
                return b""

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        class _FakeOpener:
            def open(self, request, timeout=None):
                return _FakeResponse()

        with mock.patch.object(lead, "_http_opener", return_value=_FakeOpener()):
            lead.post_chat(b"{}", API_KEY, endpoint=lead.ENDPOINT, timeout=1.0)
        self.assertEqual(recorded["n"], lead.MAX_RESPONSE_BYTES + 1)

    def test_execute_passes_configured_cap_to_transport(self):
        transport = FakeTransport(resp(200, valid_body()))
        run(transport, max_response_bytes=1234)
        self.assertEqual(transport.calls[0]["max_response_bytes"], 1234)

    def test_configured_cap_is_checked_before_parsing(self):
        # A body whose byte length exceeds the cap is refused even though it is
        # valid JSON with a valid reported cost (nothing is parsed/trusted).
        text = json.dumps(valid_body(), ensure_ascii=False)
        cap = len(text) - 1
        result = run(FakeTransport(lead.TransportResponse(200, text)),
                     max_response_bytes=cap)
        self.assertEqual(result.outcome, "protocol_error")
        self.assertEqual(result.cost.state, "unknown")
        self.assertTrue(result.cost.reserve_retained)

    def test_config_to_mapping_is_json_portable_and_roundtrips(self):
        config = cfg()
        mapping = config.to_mapping()
        # JSON-portable: no Decimal anywhere, json.dumps and canonical_json work.
        self.assertTrue(all(not isinstance(v, Decimal) for v in mapping.values()))
        self.assertIsInstance(mapping["input_price_per_million"], str)
        self.assertEqual(mapping["input_price_per_million"], "0.10")
        blob = canonical_json(mapping)
        reloaded = json.loads(json.dumps(mapping, ensure_ascii=False))
        rebuilt = lead.LeadConfig.from_mapping(reloaded)
        self.assertEqual(rebuilt, config)
        self.assertEqual(
            rebuilt.input_price_per_million, config.input_price_per_million
        )
        self.assertEqual(
            rebuilt.output_price_per_million, config.output_price_per_million
        )
        self.assertEqual(canonical_json(rebuilt.to_mapping()), blob)

    def test_plan_still_uses_portable_config_roundtrip(self):
        the_plan = plan()
        self.assertEqual(the_plan.config.input_price_per_million, Decimal("0.10"))
        self.assertEqual(lead.LeadConfig.from_mapping(the_plan.config.to_mapping()), the_plan.config)


if __name__ == "__main__":
    unittest.main()
