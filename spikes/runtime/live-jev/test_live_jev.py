#!/usr/bin/env python3
"""Offline regression tests for the direct OpenRouter Jev fallback.

No network, no credential, no paid call.  Run:

    python3 -m unittest -v test_live_jev
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest

import live_jev as lj


# --- helpers --------------------------------------------------------------


def valid_response(**overrides):
    payload = {
        "id": "gen-dec-test",
        "model": "typesafe/jev-1.14-20261014",
        "provider": "TypeSafe",
        "answers": {
            "is_original_report": {"type": "noul", "noul": 0.97},
            "study_design": {
                "type": "choice",
                "choice": "randomised",
                "confidence": 0.81,
                "probabilities": {"randomised": 0.9, "observational": 0.1, "other": 0.0},
            },
            "confidence_level": {
                "type": "score",
                "score": 1.2,
                "confidence": 0.66,
                "probabilities": {"0": 0.1, "1": 0.6, "2": 0.3},
                "legend": {"0": "low", "1": "moderate", "2": "high"},
            },
        },
        "usage": {"input_tokens": 123, "output_tokens": 9, "cost": 0.000005166},
    }
    payload.update(overrides)
    return payload


class FakeTransport:
    """Records calls; returns a canned response or raises."""

    def __init__(self, response=None, raises=None):
        self.response = response
        self.raises = raises
        self.calls = []

    def __call__(self, body, api_key, *, endpoint, timeout):
        self.calls.append({"body": body, "api_key": api_key, "endpoint": endpoint, "timeout": timeout})
        if self.raises is not None:
            raise self.raises
        return self.response


def run_with(transport, **kwargs):
    params = dict(
        questions=lj.default_questions(),
        state=lj.default_state(),
        api_key="sk-or-v1-secret-should-never-appear",
        ceiling_usd=0.25,
        authorized=True,
        transport=transport,
    )
    params.update(kwargs)
    return lj.run_live(**params)


# --- dry run default ------------------------------------------------------


class DryRunTests(unittest.TestCase):
    def test_default_report_sends_nothing(self):
        report = lj.build_dry_run_report(lj.default_questions(), lj.default_state())
        self.assertEqual(report["status"], "dry_run")
        self.assertEqual(report["calls_attempted"], 0)
        self.assertEqual(report["cost_status"], "not_incurred")
        self.assertIsNone(report["cost_usd"])
        self.assertTrue(report["reserve_retained"])

    def test_predicate_serializes_to_noul_and_others_unchanged(self):
        request = lj.build_request(lj.default_questions(), lj.default_state())
        self.assertEqual(request["questions"]["is_original_report"]["type"], "noul")
        self.assertEqual(request["questions"]["study_design"]["type"], "choice")
        self.assertEqual(request["questions"]["confidence_level"]["type"], "score")
        self.assertEqual(request["model"], "typesafe/jev-1.14")

    def test_server_cap_is_optional(self):
        with_cap = lj.build_request(lj.default_questions(), lj.default_state(), max_request_cost_usd=0.25)
        without_cap = lj.build_request(lj.default_questions(), lj.default_state())
        self.assertEqual(with_cap["provider"]["max_price"]["request"], "0.250000")
        self.assertNotIn("provider", without_cap)

    def test_cost_bound_is_conservative_and_within_ceiling(self):
        request = lj.build_request(lj.default_questions(), lj.default_state())
        body = lj.serialize_request(request)
        bound = lj.estimate_cost_bound(len(body))
        self.assertGreaterEqual(bound["input_tokens_upper_bound"], len(body))
        self.assertLess(bound["cost_upper_bound_usd"], 0.25)


# --- preflight / budget ---------------------------------------------------


class PreflightTests(unittest.TestCase):
    def test_missing_ceiling_is_refused(self):
        with self.assertRaises(lj.LiveJevBudgetError):
            lj.preflight_cost(1000, None)
        with self.assertRaises(lj.LiveJevBudgetError):
            lj.preflight_cost(1000, 0)

    def test_over_ceiling_is_refused(self):
        # The 32 KiB default-mode size cap bounds any in-range request to well
        # under $0.25, so an over-ceiling refusal needs a deliberately tiny cap.
        with self.assertRaises(lj.LiveJevBudgetError):
            lj.preflight_cost(1000, 1e-9)

    def test_oversize_request_is_refused(self):
        with self.assertRaises(lj.LiveJevValidationError):
            lj.preflight_cost(lj.SAFE_MAX_REQUEST_BYTES + 1, 0.25)

    def test_live_without_authorization_is_refused(self):
        transport = FakeTransport(lj.TransportResponse(200, json.dumps(valid_response())))
        with self.assertRaises(lj.LiveJevAuthorizationError):
            run_with(transport, authorized=False)
        self.assertEqual(transport.calls, [])

    def test_live_over_budget_makes_no_call(self):
        transport = FakeTransport(lj.TransportResponse(200, json.dumps(valid_response())))
        with self.assertRaises(lj.LiveJevBudgetError):
            run_with(transport, ceiling_usd=1e-12)
        self.assertEqual(transport.calls, [])

    def test_max_calls_capped(self):
        transport = FakeTransport(lj.TransportResponse(200, json.dumps(valid_response())))
        with self.assertRaises(lj.LiveJevAuthorizationError):
            run_with(transport, max_calls=3)
        self.assertEqual(transport.calls, [])

    def test_empty_key_is_refused(self):
        transport = FakeTransport(lj.TransportResponse(200, json.dumps(valid_response())))
        with self.assertRaises(lj.LiveJevCredentialError):
            run_with(transport, api_key="")
        self.assertEqual(transport.calls, [])


# --- response validation --------------------------------------------------


class ResponseValidationTests(unittest.TestCase):
    def test_valid_typed_answers(self):
        result = lj.validate_response(valid_response(), lj.default_questions())
        self.assertEqual(result.answers["is_original_report"]["type"], "predicate")
        self.assertAlmostEqual(result.answers["is_original_report"]["probability"], 0.97)
        self.assertEqual(result.answers["study_design"]["choice"], "randomised")
        self.assertAlmostEqual(result.answers["confidence_level"]["score"], 1.2)
        self.assertEqual(result.errors, {})
        self.assertEqual(result.cost_status, "provider_reported")

    def test_missing_cost_is_explicit_unknown(self):
        payload = valid_response()
        payload["usage"] = {"input_tokens": 10, "output_tokens": 2}
        result = lj.validate_response(payload, lj.default_questions())
        self.assertIsNone(result.cost_usd)
        self.assertEqual(result.cost_status, "unknown")

    def test_missing_answer_is_per_question_error_not_failure(self):
        payload = valid_response()
        del payload["answers"]["study_design"]
        result = lj.validate_response(payload, lj.default_questions())
        self.assertIn("study_design", result.errors)
        self.assertEqual(result.errors["study_design"]["reason"], "missing_answer")
        # the other answers survive
        self.assertIn("is_original_report", result.answers)
        self.assertIn("confidence_level", result.answers)

    def test_error_type_answer_preserved_not_exclusion(self):
        payload = valid_response()
        payload["answers"]["study_design"] = {"type": "error", "reason": "refused"}
        result = lj.validate_response(payload, lj.default_questions())
        self.assertEqual(result.errors["study_design"]["reason"], "refused")
        self.assertNotIn("exclusion", json.dumps(result.errors).lower())

    def test_unknown_question_name_is_protocol_error(self):
        payload = valid_response()
        payload["answers"]["unexpected"] = {"type": "noul", "noul": 0.5}
        with self.assertRaises(lj.LiveJevProtocolError):
            lj.validate_response(payload, lj.default_questions())

    def test_answer_type_mismatch_is_protocol_error(self):
        payload = valid_response()
        payload["answers"]["study_design"] = {"type": "noul", "noul": 0.5}
        with self.assertRaises(lj.LiveJevProtocolError):
            lj.validate_response(payload, lj.default_questions())

    def test_noul_out_of_range(self):
        payload = valid_response()
        payload["answers"]["is_original_report"] = {"type": "noul", "noul": 1.4}
        with self.assertRaises(lj.LiveJevProtocolError):
            lj.validate_response(payload, lj.default_questions())

    def test_choice_not_in_declared_options(self):
        payload = valid_response()
        payload["answers"]["study_design"] = {"type": "choice", "choice": "meta-analysis"}
        with self.assertRaises(lj.LiveJevProtocolError):
            lj.validate_response(payload, lj.default_questions())

    def test_probability_rejects_bool_and_non_finite(self):
        for bad in (True, float("inf")):
            payload = valid_response()
            payload["answers"]["confidence_level"] = {"type": "score", "score": 1.0, "confidence": bad}
            with self.assertRaises(lj.LiveJevProtocolError):
                lj.validate_response(payload, lj.default_questions())

    def test_undeclared_probability_key_rejected(self):
        payload = valid_response()
        payload["answers"]["study_design"]["probabilities"] = {"randomised": 0.5, "nope": 0.5}
        with self.assertRaises(lj.LiveJevProtocolError):
            lj.validate_response(payload, lj.default_questions())

    def test_score_out_of_levels(self):
        payload = valid_response()
        payload["answers"]["confidence_level"]["score"] = 5.0
        with self.assertRaises(lj.LiveJevProtocolError):
            lj.validate_response(payload, lj.default_questions())

    def test_bad_envelopes(self):
        for payload in ([], {"answers": {}, "usage": {}}, {"model": "m", "answers": {}, "usage": {"input_tokens": 1, "output_tokens": 1}}):
            with self.assertRaises(lj.LiveJevProtocolError):
                lj.validate_response(payload, lj.default_questions())

    def test_non_object_answer_rejected(self):
        payload = valid_response()
        payload["answers"]["study_design"] = "randomised"
        with self.assertRaises(lj.LiveJevProtocolError):
            lj.validate_response(payload, lj.default_questions())


# --- live orchestration (fake transport) ----------------------------------


class LiveOrchestrationTests(unittest.TestCase):
    def test_completed_reports_provider_cost(self):
        transport = FakeTransport(lj.TransportResponse(200, json.dumps(valid_response())))
        report = run_with(transport)
        self.assertEqual(report["status"], "completed")
        self.assertEqual(report["calls_attempted"], 1)
        self.assertAlmostEqual(report["cost_usd"], 0.000005166)
        self.assertEqual(report["cost_status"], "provider_reported")
        self.assertFalse(report["reserve_retained"])
        self.assertEqual(len(transport.calls), 1)

    def test_completed_without_cost_retains_reserve(self):
        payload = valid_response()
        payload["usage"] = {"input_tokens": 10, "output_tokens": 2}
        transport = FakeTransport(lj.TransportResponse(200, json.dumps(payload)))
        report = run_with(transport)
        self.assertEqual(report["status"], "completed")
        self.assertIsNone(report["cost_usd"])
        self.assertEqual(report["cost_status"], "unknown")
        self.assertTrue(report["reserve_retained"])

    def test_definitive_400_not_billed(self):
        body = json.dumps({"error": {"code": 400, "message": "Invalid request parameters"}})
        transport = FakeTransport(lj.TransportResponse(400, body))
        report = run_with(transport)
        self.assertEqual(report["status"], "api_error")
        self.assertEqual(report["cost_status"], "not_incurred")
        self.assertFalse(report["reserve_retained"])
        self.assertEqual(report["error"]["code"], 400)

    def test_ambiguous_500_retains_reserve(self):
        transport = FakeTransport(lj.TransportResponse(500, json.dumps({"error": {"code": 500, "message": "boom"}})))
        report = run_with(transport)
        self.assertEqual(report["status"], "api_error")
        self.assertEqual(report["cost_status"], "unknown")
        self.assertTrue(report["reserve_retained"])

    def test_transport_error_no_retry(self):
        transport = FakeTransport(raises=lj.LiveJevTransportError("connection reset"))
        report = run_with(transport)
        self.assertEqual(report["status"], "transport_error")
        self.assertEqual(report["calls_attempted"], 1)
        self.assertEqual(report["cost_status"], "unknown")
        self.assertTrue(report["reserve_retained"])
        self.assertEqual(len(transport.calls), 1)

    def test_unparseable_200_body(self):
        transport = FakeTransport(lj.TransportResponse(200, "<html>nope</html>"))
        report = run_with(transport)
        self.assertEqual(report["status"], "protocol_error")
        self.assertEqual(report["calls_attempted"], 1)
        self.assertEqual(report["cost_status"], "unknown")
        self.assertNotEqual(report["cost_status"], "not_incurred")
        self.assertTrue(report["reserve_retained"])

    def test_request_body_never_contains_the_key(self):
        key = "sk-or-v1-topsecretvalue"
        transport = FakeTransport(lj.TransportResponse(200, json.dumps(valid_response())))
        report = run_with(transport, api_key=key)
        sent = transport.calls[0]["body"].decode("utf-8")
        self.assertNotIn(key, sent)
        self.assertNotIn(key, json.dumps(report))
        self.assertNotIn("sk-or-", json.dumps(report))


# --- credential loading ---------------------------------------------------


class CredentialTests(unittest.TestCase):
    def test_env_file_parses_only_the_key(self):
        with tempfile.NamedTemporaryFile("w", suffix=".env", delete=False) as handle:
            handle.write("# comment\nOTHER_KEY=do-not-use\n")
            handle.write('OPENROUTER_API_KEY="sk-or-v1-abc123"\n')
            handle.write("SECOND=value\n")
            path = handle.name
        try:
            key, source = lj.load_api_key(path)
            self.assertEqual(key, "sk-or-v1-abc123")
            self.assertEqual(source, "env-file")
        finally:
            os.unlink(path)

    def test_env_file_missing_key_fails_closed(self):
        with tempfile.NamedTemporaryFile("w", suffix=".env", delete=False) as handle:
            handle.write("SOMETHING_ELSE=1\n")
            path = handle.name
        try:
            with self.assertRaises(lj.LiveJevCredentialError):
                lj.load_api_key(path)
        finally:
            os.unlink(path)

    def test_missing_env_file_fails_closed(self):
        with self.assertRaises(lj.LiveJevCredentialError):
            lj.load_api_key("/nonexistent/path/.env")

    def test_environment_fallback(self):
        previous = os.environ.get(lj.KEY_NAME)
        os.environ[lj.KEY_NAME] = "sk-or-v1-envvalue"
        try:
            key, source = lj.load_api_key(None)
            self.assertEqual(key, "sk-or-v1-envvalue")
            self.assertEqual(source, "environment")
        finally:
            if previous is None:
                os.environ.pop(lj.KEY_NAME, None)
            else:
                os.environ[lj.KEY_NAME] = previous


# --- redaction ------------------------------------------------------------


class RedactionTests(unittest.TestCase):
    def test_redacts_keys_and_bearer(self):
        self.assertNotIn("sk-or-v1-secret", lj.redact("failed with sk-or-v1-secret value"))
        self.assertNotIn("sk-or-v1-secret", lj.redact("Authorization: Bearer sk-or-v1-secret"))

    def test_truncates(self):
        self.assertLessEqual(len(lj.redact("x" * 5000)), 301)


# --- CLI ------------------------------------------------------------------


class CliTests(unittest.TestCase):
    def test_dry_run_exit_zero(self):
        self.assertEqual(lj.main(["--max-cost-usd", "0.25"]), 0)

    def test_live_without_authorization_exit_two(self):
        self.assertEqual(lj.main(["--live", "--max-cost-usd", "0.25"]), 2)


# --- post-submission boundary (repair 1) ----------------------------------


class PostSubmissionBoundaryTests(unittest.TestCase):
    """A malformed 200 must never be reported as ``not_incurred``."""

    def _report(self, body):
        return run_with(FakeTransport(lj.TransportResponse(200, body)))

    def test_malformed_answers_never_not_incurred(self):
        payload = valid_response()  # carries a finite usage.cost
        payload["answers"] = ["not", "a", "map"]
        report = self._report(json.dumps(payload))
        self.assertEqual(report["status"], "protocol_error")
        self.assertEqual(report["calls_attempted"], 1)
        self.assertNotEqual(report["cost_status"], "not_incurred")
        # a finite, independently validated reported cost is still honoured
        self.assertEqual(report["cost_status"], "provider_reported")
        self.assertAlmostEqual(report["cost_usd"], 0.000005166)
        self.assertFalse(report["reserve_retained"])

    def test_malformed_answers_without_cost_is_unknown_retained(self):
        payload = valid_response()
        payload["answers"] = ["not", "a", "map"]
        payload["usage"].pop("cost", None)
        report = self._report(json.dumps(payload))
        self.assertEqual(report["status"], "protocol_error")
        self.assertEqual(report["cost_status"], "unknown")
        self.assertNotEqual(report["cost_status"], "not_incurred")
        self.assertTrue(report["reserve_retained"])

    def test_empty_answers_is_protocol_error_retained(self):
        payload = valid_response()
        payload["answers"] = {}
        payload["usage"].pop("cost", None)
        report = self._report(json.dumps(payload))
        self.assertEqual(report["status"], "protocol_error")
        self.assertEqual(report["cost_status"], "unknown")
        self.assertTrue(report["reserve_retained"])

    def test_mismatched_answer_type_is_protocol_error_retained(self):
        payload = valid_response()
        payload["answers"]["study_design"] = {"type": "noul", "noul": 0.4}
        payload["usage"].pop("cost", None)
        report = self._report(json.dumps(payload))
        self.assertEqual(report["status"], "protocol_error")
        self.assertEqual(report["cost_status"], "unknown")
        self.assertTrue(report["reserve_retained"])
        self.assertEqual(report["error"]["kind"], "protocol_error")

    def test_malformed_200_with_valid_cost_releases_reserve(self):
        payload = valid_response()
        payload["answers"] = {"study_design": {"type": "noul", "noul": 0.4}}
        report = self._report(json.dumps(payload))
        self.assertEqual(report["status"], "protocol_error")
        self.assertEqual(report["cost_status"], "provider_reported")
        self.assertAlmostEqual(report["cost_usd"], 0.000005166)
        self.assertFalse(report["reserve_retained"])

    def test_malformed_200_with_invalid_cost_stays_unknown(self):
        for bad in ("abc", -1, float("inf")):
            payload = valid_response()
            payload["answers"] = {}
            payload["usage"] = {"input_tokens": 1, "output_tokens": 1, "cost": bad}
            report = self._report(json.dumps(payload))
            self.assertEqual(report["status"], "protocol_error")
            self.assertEqual(report["cost_status"], "unknown")
            self.assertTrue(report["reserve_retained"])

    def test_validation_error_on_200_does_not_escape_run_live(self):
        # Any exception raised post-submission is absorbed into a report.
        payload = valid_response()
        payload["answers"] = {"unexpected": {"type": "noul", "noul": 0.5}}
        report = self._report(json.dumps(payload))  # must not raise
        self.assertEqual(report["status"], "protocol_error")


# --- endpoint guard (repair 1) --------------------------------------------


class EndpointGuardTests(unittest.TestCase):
    def test_arbitrary_endpoint_rejected_before_transport(self):
        transport = FakeTransport(lj.TransportResponse(200, json.dumps(valid_response())))
        with self.assertRaises(lj.LiveJevEndpointError):
            run_with(transport, endpoint="https://evil.example/api/alpha/decisions")
        self.assertEqual(transport.calls, [])

    def test_trailing_slash_endpoint_rejected(self):
        transport = FakeTransport(lj.TransportResponse(200, json.dumps(valid_response())))
        with self.assertRaises(lj.LiveJevEndpointError):
            run_with(transport, endpoint=lj.ENDPOINT + "/")
        self.assertEqual(transport.calls, [])

    def test_documented_endpoint_allowed(self):
        transport = FakeTransport(lj.TransportResponse(200, json.dumps(valid_response())))
        report = run_with(transport, endpoint=lj.ENDPOINT)
        self.assertEqual(report["status"], "completed")
        self.assertEqual(len(transport.calls), 1)


# --- CLI billing path (repair 1) ------------------------------------------


class CliBillingTests(unittest.TestCase):
    def _env_file(self):
        handle = tempfile.NamedTemporaryFile("w", suffix=".env", delete=False)
        handle.write('OPENROUTER_API_KEY="sk-or-v1-cli-test"\n')
        handle.close()
        return handle.name

    def test_cli_malformed_200_reports_unknown_not_not_incurred(self):
        payload = valid_response()
        payload["answers"] = {"study_design": {"type": "noul", "noul": 0.4}}
        payload["usage"].pop("cost", None)
        env = self._env_file()
        out = env + ".report.json"
        transport = FakeTransport(lj.TransportResponse(200, json.dumps(payload)))
        try:
            code = lj.main(
                ["--live", "--i-authorize-live", "--max-cost-usd", "0.25",
                 "--env-file", env, "--report-out", out],
                transport=transport,
            )
            self.assertEqual(code, 4)
            report = json.load(open(out))
            self.assertEqual(report["status"], "protocol_error")
            self.assertEqual(report["calls_attempted"], 1)
            self.assertNotEqual(report["cost_status"], "not_incurred")
            self.assertTrue(report["reserve_retained"])
            self.assertNotIn("sk-or-", json.dumps(report))
        finally:
            os.unlink(env)
            if os.path.exists(out):
                os.unlink(out)

    def test_cli_endpoint_rejection_blocked_before_any_call(self):
        env = self._env_file()
        out = env + ".report.json"
        transport = FakeTransport(lj.TransportResponse(200, json.dumps(valid_response())))
        try:
            code = lj.main(
                ["--live", "--i-authorize-live", "--max-cost-usd", "0.25",
                 "--endpoint", "https://evil.example/collect",
                 "--env-file", env, "--report-out", out],
                transport=transport,
            )
            self.assertEqual(code, 3)
            self.assertEqual(transport.calls, [])
            report = json.load(open(out))
            self.assertEqual(report["status"], "blocked")
            self.assertEqual(report["calls_attempted"], 0)
            self.assertEqual(report["cost_status"], "not_incurred")
        finally:
            os.unlink(env)
            if os.path.exists(out):
                os.unlink(out)


if __name__ == "__main__":
    unittest.main()
