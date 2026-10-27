"""Deterministic tests for the OpenCode V2 lifecycle client (task C-python).

All tests run against the local mock server in ``mock_server.py``; none touch a
live service, the network, paid calls, service discovery, or private files.

Run:  python3 -m unittest -v test_opencode_v2
"""

from __future__ import annotations

import time
import unittest

from mock_server import MockServer
from opencode_v2 import (
    Admission,
    DecisionError,
    OpenCodeApiError,
    OpenCodeTransportError,
    OpenCodeV2Client,
    QuiescenceTimeout,
    first_decision_error,
    raise_on_decision_errors,
    summarize_decisions,
    validate_decision_result,
)

WRITER_MODEL = {"providerID": "openrouter", "id": "some/writer-model"}
CRITIC_MODEL = {"providerID": "openrouter", "id": "typesafe/jev-1.14"}


class ClientTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.server = MockServer(token="test-token")
        self.server.__enter__()
        self.addCleanup(self.server.__exit__, None, None, None)
        self.client = OpenCodeV2Client(
            self.server.base_url, token="test-token", directory="/tmp/spike"
        )


class TestSessionLifecycle(ClientTestCase):
    def test_create_session_pins_role_and_model(self) -> None:
        session = self.client.create_session(
            agent="writer", model=WRITER_MODEL, title="spike"
        )
        self.assertTrue(session["id"].startswith("ses"))
        self.assertEqual(session["agent"], "writer")
        self.assertEqual(session["model"], WRITER_MODEL)
        # location is pinned explicitly; nothing guessed
        self.assertEqual(session["location"]["directory"], "/tmp/spike")
        # durable state confirms the server recorded it
        self.assertEqual(self.client.get_session(session["id"])["id"], session["id"])

    def test_invalid_model_ref_is_rejected_locally(self) -> None:
        with self.assertRaises(ValueError):
            self.client.create_session(agent="writer", model={"id": "x"})  # no providerID
        with self.assertRaises(ValueError):
            self.client.create_session(agent="writer", model={"id": "x", "providerID": "p", "bogus": 1})

    def test_server_info_proves_explicit_endpoint(self) -> None:
        info = self.client.info()
        self.assertIn("version", info)


class TestAdmission(ClientTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.session = self.client.create_session(agent="writer", model=WRITER_MODEL)

    def test_submit_returns_durable_receipt_not_completion(self) -> None:
        admission = self.client.admit(self.session["id"], "hello", "msg_admit0001")
        self.assertIsInstance(admission, Admission)
        self.assertTrue(admission.has_durable_receipt)
        self.assertFalse(admission.duplicate)
        receipt = admission.receipt
        self.assertEqual(receipt["type"], "user")
        self.assertEqual(receipt["id"], "msg_admit0001")
        self.assertEqual(receipt["delivery"], "queue")
        # the receipt is admission only; the turn has not completed
        found = self.client.reconcile(self.session["id"], "msg_admit0001")
        self.assertEqual(found.state, "admitted")
        self.assertFalse(found.is_complete)

    def test_retry_same_admission_id_yields_single_input(self) -> None:
        first = self.client.admit(self.session["id"], "hello", "msg_retry0001")
        self.assertFalse(first.duplicate)
        second = self.client.admit(self.session["id"], "hello", "msg_retry0001")
        self.assertTrue(second.duplicate)
        self.assertTrue(second.reconciled)
        self.assertTrue(second.has_durable_receipt)
        inbox = self.client.list_inbox(self.session["id"])
        matching = [i for i in inbox if i["id"] == "msg_retry0001"]
        self.assertEqual(len(matching), 1)  # exactly one accepted input

    def test_missed_events_second_client_retrieves_persisted_state(self) -> None:
        first_client = self.client
        first_client.admit(self.session["id"], "durable work", "msg_missed0001")
        # the first client "closes": a brand-new client on the same endpoint
        second_client = OpenCodeV2Client(self.server.base_url, token="test-token")
        inbox = second_client.list_inbox(self.session["id"])
        messages = second_client.list_messages(self.session["id"])["data"]
        self.assertEqual([i["id"] for i in inbox], ["msg_missed0001"])
        self.assertTrue(
            any(m.get("id") == "msg_missed0001" for m in messages)
        )
        found = second_client.reconcile(self.session["id"], "msg_missed0001")
        self.assertTrue(found.is_durable)

    def test_reconcile_recovers_admission_after_transport_drop(self) -> None:
        self.server.state.prompt_drops = 1  # accepted durably, then connection lost
        admission = self.client.admit(self.session["id"], "flaky", "msg_drop0001")
        self.assertTrue(admission.reconciled)
        self.assertFalse(admission.duplicate)
        self.assertTrue(admission.has_durable_receipt)
        self.assertEqual(admission.receipt["id"], "msg_drop0001")

    def test_reconcile_unknown_never_reports_complete(self) -> None:
        found = self.client.reconcile(self.session["id"], "msg_ghost0001")
        self.assertEqual(found.state, "unknown")
        self.assertFalse(found.is_durable)
        self.assertFalse(found.is_complete)

    def test_reconcile_distinguishes_admission_from_completion(self) -> None:
        self.client.admit(self.session["id"], "work", "msg_done0001")
        self.server.state.add_assistant(self.session["id"], "result")
        self.server.state.complete(self.session["id"], outcome="succeeded")
        found = self.client.reconcile(self.session["id"], "msg_done0001")
        self.assertEqual(found.state, "completed")
        self.assertTrue(found.is_complete)
        self.assertEqual(found.completion["outcome"], "succeeded")

    def test_invalid_admission_id_and_text_rejected_locally(self) -> None:
        with self.assertRaises(ValueError):
            self.client.admit(self.session["id"], "hi", "nope0001")
        with self.assertRaises(ValueError):
            self.client.admit(self.session["id"], "   ", "msg_bad0001")
        with self.assertRaises(ValueError):
            self.client.admit(self.session["id"], "hi", "msg_bad0002", delivery="urgent")


class TestWaitInterrupt(ClientTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.session = self.client.create_session(agent="writer", model=WRITER_MODEL)

    def test_wait_returns_when_idle(self) -> None:
        self.server.state.wait_503_times = 2  # unavailable twice, then idle
        self.assertTrue(self.client.wait(self.session["id"], timeout=2.0, retry_interval=0.02))

    def test_bounded_wait_times_out(self) -> None:
        self.server.state.wait_status = 503
        start = time.monotonic()
        with self.assertRaises(QuiescenceTimeout):
            self.client.wait(self.session["id"], timeout=0.4, retry_interval=0.05)
        self.assertLess(time.monotonic() - start, 2.0)  # bounded, not indefinite

    def test_interrupt_returns_bool(self) -> None:
        self.server.state.interrupt_result = True
        self.assertTrue(self.client.interrupt(self.session["id"]))
        self.server.state.interrupt_result = False
        self.assertFalse(self.client.interrupt(self.session["id"], resume=False))


class TestCriticFreshContext(ClientTestCase):
    def test_critic_session_is_fresh_not_fork(self) -> None:
        writer = self.client.create_session(agent="writer", model=WRITER_MODEL)
        self.client.admit(writer["id"], "writer history", "msg_hist0001")
        critic = self.client.start_critic(
            agent="critic", model=CRITIC_MODEL, writer_model=WRITER_MODEL
        )
        self.assertNotEqual(critic["id"], writer["id"])
        self.assertNotIn("parentID", critic)
        self.assertNotIn("fork", critic)
        self.assertEqual(critic["agent"], "critic")
        self.assertEqual(critic["model"], CRITIC_MODEL)

    def test_fork_carries_writer_history(self) -> None:
        writer = self.client.create_session(agent="writer", model=WRITER_MODEL)
        self.client.admit(writer["id"], "writer history", "msg_hist0002")
        child = self.client.fork(writer["id"])
        # a fork is explicitly linked to writer history: not a clean critic
        self.assertEqual(child["parentID"], writer["id"])
        self.assertEqual(child["fork"]["sessionID"], writer["id"])

    def test_critic_model_must_differ(self) -> None:
        with self.assertRaises(ValueError):
            self.client.start_critic(
                agent="critic", model=WRITER_MODEL, writer_model=WRITER_MODEL
            )


class TestTypedDecisions(ClientTestCase):
    QUESTIONS = [
        {"type": "predicate", "name": "is_review", "instructions": "is it a review?"},
        {
            "type": "choice",
            "name": "study_design",
            "instructions": "design?",
            "choices": [{"value": "rct"}, {"value": "cohort"}],
        },
        {
            "type": "score",
            "name": "quality",
            "instructions": "quality?",
            "levels": [{"label": "low"}, {"label": "medium"}],
        },
    ]

    def test_typed_answers_validate(self) -> None:
        result = self.client.generate_decisions(
            model=CRITIC_MODEL,
            input_parts=[{"type": "text", "text": "abstract..."}],
            questions=self.QUESTIONS,
        )
        errors = validate_decision_result(result)
        self.assertEqual(errors, [])
        kinds = [a["type"] for a in result["answers"]]
        self.assertEqual(kinds, ["predicate", "choice", "score"])
        rows = summarize_decisions(result)
        self.assertFalse(any(r["failed"] for r in rows))

    def test_per_question_failure_keeps_other_answers(self) -> None:
        self.server.state.decision_answers = [
            {"type": "predicate", "name": "is_review", "probability": 0.5},
            {"type": "error", "name": "study_design", "reason": "refusal"},
            {"type": "score", "name": "quality", "score": 1.0, "probabilities": [], "confidence": 0.2},
        ]
        result = self.client.generate_decisions(
            model=CRITIC_MODEL,
            input_parts=[{"type": "text", "text": "x"}],
            questions=self.QUESTIONS,
        )
        errors = validate_decision_result(result)
        self.assertEqual([e["name"] for e in errors], ["study_design"])
        self.assertIsInstance(first_decision_error(result), DecisionError)
        with self.assertRaises(DecisionError):
            raise_on_decision_errors(result)
        rows = summarize_decisions(result)
        self.assertEqual([r["failed"] for r in rows], [False, True, False])

    def test_malformed_answer_rejected(self) -> None:
        with self.assertRaises(ValueError):
            validate_decision_result({"answers": [{"type": "predicate", "name": "x"}]})
        with self.assertRaises(ValueError):
            validate_decision_result(
                {"answers": [{"type": "predicate", "name": "x", "probability": 2}]}
            )
        with self.assertRaises(ValueError):
            self.client.generate_decisions(
                model=CRITIC_MODEL, input_parts=[], questions=self.QUESTIONS
            )
        with self.assertRaises(ValueError):
            self.client.generate_decisions(
                model=CRITIC_MODEL,
                input_parts=[{"type": "text", "text": "x"}],
                questions=[
                    {"type": "predicate", "name": "d", "instructions": "a"},
                    {"type": "predicate", "name": "d", "instructions": "b"},
                ],
            )


class TestAuthAndTransport(ClientTestCase):
    def test_unauthorized_error_tag(self) -> None:
        bad = OpenCodeV2Client(self.server.base_url, token="wrong-token")
        with self.assertRaises(OpenCodeApiError) as ctx:
            bad.list_inbox("ses_mock0001")
        self.assertEqual(ctx.exception.status, 401)
        self.assertEqual(ctx.exception.tag, "UnauthorizedError")

    def test_missing_token_unauthorized(self) -> None:
        anon = OpenCodeV2Client(self.server.base_url)
        with self.assertRaises(OpenCodeApiError) as ctx:
            anon.info()
        self.assertEqual(ctx.exception.status, 401)

    def test_unreachable_endpoint_is_transport_error(self) -> None:
        dead = OpenCodeV2Client("http://127.0.0.1:1", timeout=1.0)
        with self.assertRaises(OpenCodeTransportError):
            dead.info()

    def test_api_error_on_unknown_route(self) -> None:
        with self.assertRaises(OpenCodeApiError):
            self.client._request("GET", "/api/session/ses_missing")  # 404 SessionNotFound


class TestExplicitConfiguration(unittest.TestCase):
    def test_from_env_requires_explicit_base_url(self) -> None:
        with self.assertRaises(ValueError):
            OpenCodeV2Client.from_env({})  # no default endpoint, no discovery

    def test_from_env_uses_given_values(self) -> None:
        client = OpenCodeV2Client.from_env(
            {
                "OPENCODE_SPIKE_BASE_URL": "http://127.0.0.1:9999",
                "OPENCODE_SPIKE_TOKEN": "t",
                "OPENCODE_SPIKE_DIRECTORY": "/tmp/x",
            }
        )
        self.assertEqual(client.base_url, "http://127.0.0.1:9999")
        self.assertEqual(client.token, "t")
        self.assertEqual(client.directory, "/tmp/x")


if __name__ == "__main__":
    unittest.main(verbosity=2)
