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
    """Records every call; returns a canned response or raises."""

    def __init__(self, response=None, raises=None):
        self.response = response
        self.raises = raises
        self.calls = []

    def __call__(self, body, api_key, *, endpoint, timeout):
        self.calls.append(
            {"body": body, "api_key": api_key, "endpoint": endpoint, "timeout": timeout}
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

    def test_missing_field_refused(self):
        mapping = config_mapping()
        del mapping["pricing_ref"]
        with self.assertRaises(lead.LeadValidationError):
            lead.LeadConfig.from_mapping(mapping)

    def test_unknown_field_refused(self):
        with self.assertRaises(lead.LeadValidationError):
            lead.LeadConfig.from_mapping(config_mapping(tools=[]))

    def test_non_mapping_refused(self):
        for bad in (None, [], "x", 3):
            with self.assertRaises(lead.LeadValidationError):
                lead.LeadConfig.from_mapping(bad)

    def test_missing_or_none_values_refused(self):
        with self.assertRaises(lead.LeadValidationError):
            lead.LeadConfig.from_mapping(config_mapping(model_id=None))
        with self.assertRaises(lead.LeadValidationError):
            lead.LeadConfig.from_mapping(config_mapping(model_id=""))

    def test_context_below_max_output_refused(self):
        with self.assertRaises(lead.LeadValidationError):
            cfg(context_tokens=1024, max_output_tokens=4096)

    def test_unbounded_prices_refused(self):
        for bad in ("1e999999", "0e-999999", "1e25", 0, "-1", "0", float("inf"), True):
            with self.assertRaises(lead.LeadValidationError):
                cfg(input_price_per_million=bad)
        for bad in ("1e999999", 0.4, 0, "-2", True):
            with self.assertRaises(lead.LeadValidationError):
                cfg(output_price_per_million=bad)

    def test_bool_and_nonfinite_numeric_fields_refused(self):
        with self.assertRaises(lead.LeadValidationError):
            cfg(max_output_tokens=True)
        with self.assertRaises(lead.LeadValidationError):
            cfg(max_output_tokens=10 ** 30)
        with self.assertRaises(lead.LeadValidationError):
            cfg(context_tokens=10 ** 30)
        with self.assertRaises(lead.LeadValidationError):
            cfg(max_response_bytes=10 ** 30)
        for bad in (float("nan"), float("inf"), float("-inf"), 0, -1, 1e9, True):
            with self.assertRaises(lead.LeadValidationError):
                cfg(timeout_seconds=bad)

    def test_whitespace_in_identifiers_refused(self):
        with self.assertRaises(lead.LeadValidationError):
            cfg(upstream_provider="deep seek")
        with self.assertRaises(lead.LeadValidationError):
            cfg(model_id=" deepseek/model")

    def test_direct_construction_is_validated(self):
        with self.assertRaises(lead.LeadValidationError):
            lead.LeadConfig(
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
    def _run_body(self, **overrides):
        return run(FakeTransport(resp(200, valid_body(**overrides))))

    def test_wrong_model_is_protocol_error(self):
        body = valid_body(model="openai/other-model")
        result = run(FakeTransport(resp(200, body)))
        self.assertEqual(result.outcome, "protocol_error")
        self.assertIsNone(result.proposal_text)
        self.assertEqual(result.cost.state, "known")
        self.assertEqual(result.cost.amount_usd, "0.00002037")

    def test_length_finish_is_never_success(self):
        result = self._run_body(choices=[valid_choices(finish_reason="length")])
        self.assertEqual(result.outcome, "protocol_error")

    def test_tool_calls_are_never_success(self):
        choice = valid_choices()
        choice["message"]["tool_calls"] = [{"id": "t1", "type": "function"}]
        result = self._run_body(choices=[choice])
        self.assertEqual(result.outcome, "protocol_error")

    def test_refusal_is_refused(self):
        choice = valid_choices()
        choice["message"]["refusal"] = "I cannot help with that."
        choice["message"]["content"] = ""
        result = self._run_body(choices=[choice])
        self.assertEqual(result.outcome, "refused")
        self.assertIsNone(result.proposal_text)

    def test_content_filter_is_refused(self):
        result = self._run_body(choices=[valid_choices(finish_reason="content_filter")])
        self.assertEqual(result.outcome, "refused")

    def test_top_level_error_is_protocol_error(self):
        body = valid_body()
        body["error"] = {"code": 500, "message": CANARY}
        result = run(FakeTransport(resp(200, body)))
        self.assertEqual(result.outcome, "protocol_error")

    def test_missing_choices_is_protocol_error(self):
        body = valid_body()
        del body["choices"]
        result = run(FakeTransport(resp(200, body)))
        self.assertEqual(result.outcome, "protocol_error")

    def test_multiple_choices_is_protocol_error(self):
        result = self._run_body(choices=[valid_choices(), valid_choices()])
        self.assertEqual(result.outcome, "protocol_error")

    def test_non_text_content_is_protocol_error(self):
        choice = valid_choices()
        choice["message"]["content"] = [{"type": "text", "text": "hi"}]
        result = self._run_body(choices=[choice])
        self.assertEqual(result.outcome, "protocol_error")

    def test_empty_content_is_protocol_error(self):
        choice = valid_choices()
        choice["message"]["content"] = "   "
        result = self._run_body(choices=[choice])
        self.assertEqual(result.outcome, "protocol_error")

    def test_invalid_usage_tokens_is_protocol_error(self):
        body = valid_body()
        body["usage"]["prompt_tokens"] = -1
        result = run(FakeTransport(resp(200, body)))
        self.assertEqual(result.outcome, "protocol_error")


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

    def test_forged_model_refused(self):
        forged = dataclasses.replace(self._valid_plan(), config=cfg(model_id="openai/other"))
        transport = FakeTransport(resp(200, valid_body()))
        with self.assertRaises(lead.LeadError):
            lead.execute_lead(forged, api_key=API_KEY, transport=transport)
        self.assertEqual(transport.calls, [])

    def test_forged_text_refused(self):
        forged = dataclasses.replace(self._valid_plan(), text="MUTATED")
        transport = FakeTransport(resp(200, valid_body()))
        with self.assertRaises(lead.LeadValidationError):
            lead.execute_lead(forged, api_key=API_KEY, transport=transport)
        self.assertEqual(transport.calls, [])

    def test_forged_body_refused(self):
        forged = dataclasses.replace(self._valid_plan(), request_body=b"{}")
        with self.assertRaises(lead.LeadValidationError):
            lead.execute_lead(forged, api_key=API_KEY, transport=FakeTransport())
        self.assertEqual(forged.request_body, b"{}")

    def test_forged_reserve_refused(self):
        forged = dataclasses.replace(self._valid_plan(), reserve_usd="0")
        with self.assertRaises(lead.LeadValidationError):
            lead.execute_lead(forged, api_key=API_KEY, transport=FakeTransport())

    def test_forged_bound_evidence_refused(self):
        forged = dataclasses.replace(self._valid_plan(), bound_evidence={"a": 1})
        with self.assertRaises(lead.LeadValidationError):
            lead.execute_lead(forged, api_key=API_KEY, transport=FakeTransport())

    def test_forged_endpoint_refused(self):
        forged = dataclasses.replace(self._valid_plan(), endpoint="https://evil.example/x")
        with self.assertRaises(lead.LeadError):
            lead.execute_lead(forged, api_key=API_KEY, transport=FakeTransport())

    def test_forged_payload_refused(self):
        forged = dataclasses.replace(self._valid_plan(), payload={"model": MODEL})
        with self.assertRaises(lead.LeadValidationError):
            lead.execute_lead(forged, api_key=API_KEY, transport=FakeTransport())

    def test_non_plan_refused(self):
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

    def test_known_requires_amount(self):
        with self.assertRaises(lead.LeadValidationError):
            lead.CostRecord(state="known")

    def test_not_incurred_rejects_amount(self):
        with self.assertRaises(lead.LeadValidationError):
            lead.CostRecord(state="not_incurred", amount_usd="0.1")

    def test_unknown_rejects_zero_amount(self):
        with self.assertRaises(lead.LeadValidationError):
            lead.CostRecord(state="unknown", amount_usd="0")

    def test_bucket_mismatch_rejected(self):
        with self.assertRaises(lead.LeadValidationError):
            lead.CostRecord(state="unknown", reserve_usd="1", reserve_retained=True,
                            accounting_bucket="session_statistics")

    def test_reserve_retained_must_match_state(self):
        with self.assertRaises(lead.LeadValidationError):
            lead.CostRecord(state="unknown", reserve_usd="1", reserve_retained=False)


# --------------------------------------------------------------------------- #
# Installed-package shape
# --------------------------------------------------------------------------- #
class InstalledPackageTests(unittest.TestCase):
    def test_module_is_a_package_submodule(self):
        self.assertEqual(lead.__name__, "review_article_agent.runtime.lead")

    def test_endpoint_is_the_documented_one(self):
        self.assertEqual(lead.ENDPOINT, "https://openrouter.ai/api/v1/chat/completions")


if __name__ == "__main__":
    unittest.main()
