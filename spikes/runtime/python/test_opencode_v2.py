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
    DecisionProtocolError,
    OpenCodeApiError,
    OpenCodeTransportError,
    OpenCodeV2Client,
    QuiescenceTimeout,
    first_decision_error,
    raise_on_decision_errors,
    summarize_decisions,
    validate_decision_questions,
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
        self.assertEqual(found.state, "in_progress")
        self.assertTrue(found.is_durable)
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
        messages = self.client.list_messages(self.session["id"])["data"]
        self.assertEqual(
            [m["id"] for m in messages if m["type"] == "user"], ["msg_retry0001"]
        )

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


class TestPerTurnReconciliation(ClientTestCase):
    """A terminal must belong to the target turn; other turns never complete it."""

    def setUp(self) -> None:
        super().setUp()
        self.session = self.client.create_session(agent="writer", model=WRITER_MODEL)
        self.sid = self.session["id"]

    def test_second_queued_turn_not_falsely_complete(self) -> None:
        # turn 1 delivered and completed
        self.server.state.append_user(self.sid, "msg_turn1a", "first")
        self.server.state.append_assistant(self.sid, "first done")
        self.server.state.append_idle(self.sid, "succeeded")
        # turn 2 enqueued but not delivered
        self.server.state.enqueue(self.sid, "msg_turn2a", "second")
        found = self.client.reconcile(self.sid, "msg_turn2a")
        self.assertEqual(found.state, "queued")
        self.assertFalse(found.is_complete)  # the old any-idle bug returned completed
        self.assertTrue(found.is_durable)

    def test_prior_idle_does_not_complete_later_turn(self) -> None:
        self.server.state.append_user(self.sid, "msg_turn1b", "first")
        self.server.state.append_idle(self.sid, "succeeded")
        # a later turn is delivered but still running
        self.server.state.append_user(self.sid, "msg_turn2b", "second")
        found = self.client.reconcile(self.sid, "msg_turn2b")
        self.assertEqual(found.state, "in_progress")
        self.assertFalse(found.is_complete)

    def test_terminal_after_next_turn_does_not_complete_target(self) -> None:
        self.server.state.append_user(self.sid, "msg_turn1c", "first")
        self.server.state.append_user(self.sid, "msg_turn2c", "second")
        self.server.state.append_assistant(self.sid, "second done")
        self.server.state.append_idle(self.sid, "succeeded")
        found = self.client.reconcile(self.sid, "msg_turn1c")
        self.assertEqual(found.state, "in_progress")  # turn 1 has no terminal yet

    def test_failed_outcome_is_distinct_from_success(self) -> None:
        self.server.state.append_user(self.sid, "msg_fail001", "x")
        self.server.state.append_idle(self.sid, "failed")
        found = self.client.reconcile(self.sid, "msg_fail001")
        self.assertEqual(found.state, "failed")
        self.assertTrue(found.is_terminal)
        self.assertFalse(found.is_complete)
        self.assertEqual(found.outcome, "failed")

    def test_interrupted_outcome_is_distinct_from_success(self) -> None:
        self.server.state.append_user(self.sid, "msg_intr001", "x")
        self.server.state.append_idle(self.sid, "interrupted")
        found = self.client.reconcile(self.sid, "msg_intr001")
        self.assertEqual(found.state, "interrupted")
        self.assertFalse(found.is_complete)

    def test_assistant_error_without_idle_is_not_terminal(self) -> None:
        # an assistant error is an intermediate loop failure, not a turn terminal
        self.server.state.append_user(self.sid, "msg_aerr001", "x")
        self.server.state.append_assistant(
            self.sid, "boom", error={"message": "provider error"}, finish="error"
        )
        found = self.client.reconcile(self.sid, "msg_aerr001")
        self.assertEqual(found.state, "in_progress")
        self.assertFalse(found.is_terminal)
        self.assertFalse(found.is_complete)

    def test_tool_calls_assistant_without_idle_is_not_complete(self) -> None:
        # realistic agent loop: user -> assistant(finish=tool-calls) with a tool
        # part -> tool execution / next assistant -> no idle yet.
        self.server.state.append_user(self.sid, "msg_tools01", "do the work")
        self.server.state.append_assistant(
            self.sid,
            "",
            finish="tool-calls",
            content=[
                {
                    "type": "tool",
                    "id": "call_1",
                    "name": "read_file",
                    "state": {"status": "completed", "input": {"path": "a.txt"}, "content": []},
                    "time": {"created": 1, "completed": 2},
                }
            ],
        )
        self.server.state.add_assistant(self.sid, "still working")  # next loop step
        found = self.client.reconcile(self.sid, "msg_tools01")
        self.assertFalse(found.is_complete)
        self.assertIn(found.state, ("in_progress", "unknown"))
        # only a later authoritative idle may complete it
        self.server.state.append_idle(self.sid, "succeeded")
        self.assertEqual(self.client.reconcile(self.sid, "msg_tools01").state, "completed")

    def test_completed_assistant_finish_stop_without_idle_is_not_complete(self) -> None:
        # even finish=stop is not task success: more loop steps may follow
        self.server.state.append_user(self.sid, "msg_stop001", "x")
        self.server.state.append_assistant(self.sid, "partial", finish="stop")
        found = self.client.reconcile(self.sid, "msg_stop001")
        self.assertEqual(found.state, "in_progress")
        self.assertFalse(found.is_complete)

    def test_assistant_finish_length_without_idle_is_not_complete(self) -> None:
        self.server.state.append_user(self.sid, "msg_len0001", "x")
        self.server.state.append_assistant(self.sid, "truncated", finish="length")
        found = self.client.reconcile(self.sid, "msg_len0001")
        self.assertEqual(found.state, "in_progress")
        self.assertFalse(found.is_complete)

    def test_assistant_finish_content_filter_without_idle_is_not_complete(self) -> None:
        self.server.state.append_user(self.sid, "msg_cf00001", "x")
        self.server.state.append_assistant(self.sid, "", finish="content-filter")
        found = self.client.reconcile(self.sid, "msg_cf00001")
        self.assertEqual(found.state, "in_progress")
        self.assertFalse(found.is_complete)

    def test_idle_succeeded_is_the_only_completion(self) -> None:
        self.server.state.append_user(self.sid, "msg_ok00001", "x")
        self.server.state.append_assistant(self.sid, "done", finish="stop")
        self.assertFalse(self.client.reconcile(self.sid, "msg_ok00001").is_complete)
        self.server.state.append_idle(self.sid, "succeeded")
        found = self.client.reconcile(self.sid, "msg_ok00001")
        self.assertTrue(found.is_complete)
        self.assertEqual(found.outcome, "succeeded")
    def test_unrecognized_idle_outcome_is_unknown(self) -> None:
        self.server.state.append_user(self.sid, "msg_weird01", "x")
        self.server.state.append_idle(self.sid, "vanished")
        found = self.client.reconcile(self.sid, "msg_weird01")
        self.assertEqual(found.state, "unknown")
        self.assertFalse(found.is_complete)

    def _fill_turns(self, count: int) -> None:
        for index in range(count):
            self.server.state.append_user(self.sid, f"msg_fill{index:04d}", "filler")
            self.server.state.append_assistant(self.sid, "filler done")
            self.server.state.append_idle(self.sid, "succeeded")

    def test_paging_finds_older_admission(self) -> None:
        self._fill_turns(30)  # ~90 messages before the target
        self.server.state.append_user(self.sid, "msg_target01", "target")
        self.server.state.append_assistant(self.sid, "target done")
        self.server.state.append_idle(self.sid, "succeeded")
        self.server.state.message_page_size = 20
        found = self.client.reconcile(self.sid, "msg_target01")
        self.assertEqual(found.state, "completed")  # required paging to find it
        self.assertTrue(found.is_complete)

    def test_paging_bound_returns_unknown_not_complete(self) -> None:
        self._fill_turns(30)
        self.server.state.append_user(self.sid, "msg_target02", "target")
        self.server.state.append_assistant(self.sid, "target done")
        self.server.state.append_idle(self.sid, "succeeded")
        self.server.state.message_page_size = 20
        bounded = OpenCodeV2Client(
            self.server.base_url, token="test-token", max_message_pages=1
        )
        found = bounded.reconcile(self.sid, "msg_target02")
        self.assertEqual(found.state, "unknown")
        self.assertFalse(found.is_complete)
        self.assertIn("paging limit", found.reason)

    def test_paging_limit_never_reports_success(self) -> None:
        # target on the first page, its turn window open past the read bound
        self.server.state.append_user(self.sid, "msg_target03", "target")
        for _ in range(40):
            self.server.state.append_system(self.sid, "padding")
        self.server.state.message_page_size = 20
        bounded = OpenCodeV2Client(
            self.server.base_url, token="test-token", max_message_pages=1
        )
        found = bounded.reconcile(self.sid, "msg_target03")
        self.assertEqual(found.state, "unknown")
        self.assertFalse(found.is_complete)
        self.assertIn("not fully read", found.reason)


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
            {
                "type": "score",
                "name": "quality",
                "score": 1.0,
                "probabilities": [
                    {"value": 0, "label": "low", "probability": 0.4},
                    {"value": 1, "label": "medium", "probability": 0.6},
                ],
                "confidence": 0.2,
            },
        ]
        result = self.client.generate_decisions(
            model=CRITIC_MODEL,
            input_parts=[{"type": "text", "text": "x"}],
            questions=self.QUESTIONS,
        )
        errors = validate_decision_result(result, self.QUESTIONS)
        self.assertEqual([e["name"] for e in errors], ["study_design"])
        # a valid per-question error is preserved, not converted to an exclusion
        self.assertIsInstance(first_decision_error(result), DecisionError)
        with self.assertRaises(DecisionError):
            raise_on_decision_errors(result)
        rows = summarize_decisions(result)
        self.assertEqual([r["failed"] for r in rows], [False, True, False])
        # the error answer is a well-formed error answer, not a protocol failure
        error_answer = errors[0]
        self.assertEqual(error_answer["type"], "error")
        self.assertEqual(error_answer["reason"], "refusal")

    def test_generate_decisions_validates_at_the_boundary(self) -> None:
        # the default mock answers are valid: they must survive the boundary
        result = self.client.generate_decisions(
            model=CRITIC_MODEL,
            input_parts=[{"type": "text", "text": "abstract..."}],
            questions=self.QUESTIONS,
        )
        self.assertEqual(
            [a["name"] for a in result["answers"]],
            ["is_review", "study_design", "quality"],
        )
        self.assertEqual(validate_decision_result(result, self.QUESTIONS), [])

    def test_malformed_answer_rejected(self) -> None:
        with self.assertRaises(DecisionProtocolError):
            validate_decision_result({"answers": [{"type": "predicate", "name": "x"}]})
        with self.assertRaises(DecisionProtocolError):
            validate_decision_result(
                {"answers": [{"type": "predicate", "name": "x", "probability": 2}]}
            )
        # a protocol failure is still a ValueError (backwards compatible)
        with self.assertRaises(ValueError):
            validate_decision_result({"answers": [{"type": "predicate", "name": "x"}]})
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

    # -- strict numeric bounds (finite, never bool) --------------------------
    def test_probability_rejects_bool_nan_and_infinity(self) -> None:
        for bad in (True, False, float("nan"), float("inf"), -0.1, 1.1, "0.5", None):
            with self.assertRaises(DecisionProtocolError, msg=repr(bad)):
                validate_decision_result(
                    {"answers": [{"type": "predicate", "name": "x", "probability": bad}]}
                )
        # finite inclusive bounds are accepted, and 0/1 ints are numbers
        self.assertEqual(
            validate_decision_result(
                {"answers": [{"type": "predicate", "name": "x", "probability": 0}]}
            ),
            [],
        )

    def test_confidence_and_probabilities_reject_non_finite(self) -> None:
        for bad in (True, float("nan"), float("inf"), 2):
            with self.assertRaises(DecisionProtocolError, msg=f"confidence={bad!r}"):
                validate_decision_result(
                    {
                        "answers": [
                            {
                                "type": "choice",
                                "name": "x",
                                "choice": "a",
                                "probabilities": [{"value": "a", "probability": 0.5}],
                                "confidence": bad,
                            }
                        ]
                    }
                )
        with self.assertRaises(DecisionProtocolError):
            validate_decision_result(
                {
                    "answers": [
                        {
                            "type": "choice",
                            "name": "x",
                            "choice": "a",
                            "probabilities": [{"value": "a", "probability": float("nan")}],
                            "confidence": 0.5,
                        }
                    ]
                }
            )

    def test_unknown_and_extra_fields_rejected(self) -> None:
        with self.assertRaises(DecisionProtocolError):
            validate_decision_result(
                {
                    "answers": [
                        {"type": "predicate", "name": "x", "probability": 0.5, "bogus": 1}
                    ]
                }
            )
        with self.assertRaises(DecisionProtocolError):
            validate_decision_result({"answers": [], "bogus": 1})
        with self.assertRaises(DecisionProtocolError):
            validate_decision_result({"answers": [], "cost": True})
        with self.assertRaises(DecisionProtocolError):
            validate_decision_result({"answers": [], "tokens": {"input": "12"}})

    def test_missing_or_non_dict_response_is_protocol_failure(self) -> None:
        for payload in ({}, {"data": None}, {"data": []}, [{"answers": []}]):
            self.server.state.decision_response = payload
            with self.assertRaises(DecisionProtocolError, msg=repr(payload)):
                self.client.generate_decisions(
                    model=CRITIC_MODEL,
                    input_parts=[{"type": "text", "text": "x"}],
                    questions=self.QUESTIONS,
                )

    def test_answer_cardinality_missing_duplicate_and_unknown_names(self) -> None:
        cases = {
            "too_few": [
                {"type": "predicate", "name": "is_review", "probability": 0.5},
            ],
            "too_many": [
                {"type": "predicate", "name": "is_review", "probability": 0.5},
                {"type": "error", "name": "study_design", "reason": "r"},
                {
                    "type": "score",
                    "name": "quality",
                    "score": 1.0,
                    "probabilities": [
                        {"value": 0, "label": "low", "probability": 0.5},
                        {"value": 1, "label": "medium", "probability": 0.5},
                    ],
                    "confidence": 0.5,
                },
                {"type": "predicate", "name": "extra", "probability": 0.5},
            ],
            "unknown_name": [
                {"type": "predicate", "name": "is_review", "probability": 0.5},
                {"type": "error", "name": "not_a_question", "reason": "r"},
                {"type": "score", "name": "quality", "score": 1.0,
                 "probabilities": [{"value": 0, "label": "low", "probability": 0.5},
                                   {"value": 1, "label": "medium", "probability": 0.5}],
                 "confidence": 0.5},
            ],
            "wrong_order": [
                {"type": "error", "name": "study_design", "reason": "r"},
                {"type": "predicate", "name": "is_review", "probability": 0.5},
                {"type": "score", "name": "quality", "score": 1.0,
                 "probabilities": [{"value": 0, "label": "low", "probability": 0.5},
                                   {"value": 1, "label": "medium", "probability": 0.5}],
                 "confidence": 0.5},
            ],
            "duplicate_name": [
                {"type": "predicate", "name": "is_review", "probability": 0.5},
                {"type": "predicate", "name": "is_review", "probability": 0.5},
                {"type": "score", "name": "quality", "score": 1.0,
                 "probabilities": [{"value": 0, "label": "low", "probability": 0.5},
                                   {"value": 1, "label": "medium", "probability": 0.5}],
                 "confidence": 0.5},
            ],
        }
        for label, answers in cases.items():
            with self.assertRaises(DecisionProtocolError, msg=label):
                validate_decision_result({"answers": answers}, self.QUESTIONS)

    def test_answer_type_mismatch_rejected(self) -> None:
        # a predicate question answered as a choice is a protocol failure
        answers = [
            {"type": "choice", "name": "is_review", "choice": "rct",
             "probabilities": [{"value": "rct", "probability": 0.5},
                               {"value": "cohort", "probability": 0.5}],
             "confidence": 0.5},
            {"type": "error", "name": "study_design", "reason": "r"},
            {"type": "score", "name": "quality", "score": 1.0,
             "probabilities": [{"value": 0, "label": "low", "probability": 0.5},
                               {"value": 1, "label": "medium", "probability": 0.5}],
             "confidence": 0.5},
        ]
        with self.assertRaises(DecisionProtocolError):
            validate_decision_result({"answers": answers}, self.QUESTIONS)

    def test_invalid_choice_selected_value_rejected(self) -> None:
        answers = [
            {"type": "predicate", "name": "is_review", "probability": 0.5},
            {"type": "choice", "name": "study_design", "choice": "case-control",
             "probabilities": [{"value": "rct", "probability": 0.5},
                               {"value": "cohort", "probability": 0.5}],
             "confidence": 0.5},
            {"type": "score", "name": "quality", "score": 1.0,
             "probabilities": [{"value": 0, "label": "low", "probability": 0.5},
                               {"value": 1, "label": "medium", "probability": 0.5}],
             "confidence": 0.5},
        ]
        with self.assertRaises(DecisionProtocolError):
            validate_decision_result({"answers": answers}, self.QUESTIONS)

    def test_choice_distribution_must_cover_declared_choices(self) -> None:
        base = [
            {"type": "predicate", "name": "is_review", "probability": 0.5},
            None,  # placeholder for the choice answer
            {"type": "score", "name": "quality", "score": 1.0,
             "probabilities": [{"value": 0, "label": "low", "probability": 0.5},
                               {"value": 1, "label": "medium", "probability": 0.5}],
             "confidence": 0.5},
        ]
        bad_distributions = [
            [{"value": "rct", "probability": 1.0}],  # BBC: missing cohort
            [{"value": "rct", "probability": 0.5},
             {"value": "case-control", "probability": 0.5}],  # unknown value
            [{"value": "rct", "probability": 0.5},
             {"value": "rct", "probability": 0.5}],  # duplicate value
        ]
        for probabilities in bad_distributions:
            base[1] = {
                "type": "choice", "name": "study_design", "choice": "rct",
                "probabilities": probabilities, "confidence": 0.5,
            }
            with self.assertRaises(DecisionProtocolError, msg=repr(probabilities)):
                validate_decision_result({"answers": list(base)}, self.QUESTIONS)

    def test_typed_choice_values_distinguish_true_from_string(self) -> None:
        questions = [
            {
                "type": "choice",
                "name": "flag",
                "instructions": "flag?",
                "choices": [{"value": True}, {"value": "true"}],
            }
        ]
        good = {
            "answers": [
                {"type": "choice", "name": "flag", "choice": True,
                 "probabilities": [{"value": True, "probability": 0.6},
                                   {"value": "true", "probability": 0.4}],
                 "confidence": 0.6}
            ]
        }
        self.assertEqual(validate_decision_result(good, questions), [])
        # "true" (string) selected is a different, still-declared value
        self.assertEqual(
            validate_decision_result(
                {
                    "answers": [
                        {"type": "choice", "name": "flag", "choice": "true",
                         "probabilities": [{"value": True, "probability": 0.5},
                                           {"value": "true", "probability": 0.5}],
                         "confidence": 0.5}
                    ]
                },
                questions,
            ),
            [],
        )

    def test_invalid_score_rejected(self) -> None:
        base = [
            {"type": "predicate", "name": "is_review", "probability": 0.5},
            {"type": "error", "name": "study_design", "reason": "r"},
            None,
        ]
        bad_scores = [
            {  # an undeclared level label
                "type": "score", "name": "quality", "score": 1.0,
                "probabilities": [{"value": 0, "label": "low", "probability": 0.5},
                                  {"value": 5, "label": "high", "probability": 0.5}],
                "confidence": 0.5,
            },
            {  # label does not match the declared level
                "type": "score", "name": "quality", "score": 1.0,
                "probabilities": [{"value": 0, "label": "low", "probability": 0.5},
                                  {"value": 1, "label": "high", "probability": 0.5}],
                "confidence": 0.5,
            },
            {  # not a finite number
                "type": "score", "name": "quality", "score": float("inf"),
                "probabilities": [{"value": 0, "label": "low", "probability": 0.5},
                                  {"value": 1, "label": "medium", "probability": 0.5}],
                "confidence": 0.5,
            },
            {  # boolean is not a level index
                "type": "score", "name": "quality", "score": 1.0,
                "probabilities": [{"value": True, "label": "low", "probability": 0.5},
                                  {"value": 1, "label": "medium", "probability": 0.5}],
                "confidence": 0.5,
            },
            {  # duplicate level value
                "type": "score", "name": "quality", "score": 1.0,
                "probabilities": [{"value": 1, "label": "low", "probability": 0.5},
                                  {"value": 1, "label": "medium", "probability": 0.5}],
                "confidence": 0.5,
            },
            {  # missing a level
                "type": "score", "name": "quality", "score": 1.0,
                "probabilities": [{"value": 0, "label": "low", "probability": 1.0}],
                "confidence": 0.5,
            },
        ]
        for score in bad_scores:
            base[2] = score
            with self.assertRaises(DecisionProtocolError, msg=repr(score)):
                validate_decision_result({"answers": list(base)}, self.QUESTIONS)

    def test_question_shapes_validated_locally(self) -> None:
        with self.assertRaises(ValueError):
            validate_decision_questions([])
        with self.assertRaises(ValueError):
            validate_decision_questions(
                [{"type": "predicate", "name": "x"}]  # missing instructions
            )
        with self.assertRaises(ValueError):
            validate_decision_questions(
                [{"type": "choice", "name": "x", "instructions": "?", "choices": []}]
            )
        with self.assertRaises(ValueError):
            validate_decision_questions(
                [{"type": "score", "name": "x", "instructions": "?", "levels": []}]
            )
        with self.assertRaises(ValueError):
            validate_decision_questions(
                [{"type": "predicate", "name": "x", "instructions": "?", "bogus": 1}]
            )
        # a valid question set is returned unchanged
        self.assertEqual(validate_decision_questions(self.QUESTIONS), self.QUESTIONS)

    def test_decision_http_error_is_api_error_not_protocol_failure(self) -> None:
        self.server.state.decision_status = 503
        with self.assertRaises(OpenCodeApiError) as ctx:
            self.client.generate_decisions(
                model=CRITIC_MODEL,
                input_parts=[{"type": "text", "text": "x"}],
                questions=self.QUESTIONS,
            )
        self.assertEqual(ctx.exception.status, 503)


class TestAuthAndTransport(ClientTestCase):
    def test_unauthorized_error_tag(self) -> None:
        bad = OpenCodeV2Client(self.server.base_url, token="wrong-token")
        with self.assertRaises(OpenCodeApiError) as ctx:
            bad.list_inbox("ses_mock0001")
        self.assertEqual(ctx.exception.status, 401)
        self.assertEqual(ctx.exception.tag, "UnauthorizedError")

    def test_missing_token_fails_closed(self) -> None:
        with self.assertRaises(ValueError):
            OpenCodeV2Client(self.server.base_url)

    def test_empty_token_fails_closed(self) -> None:
        with self.assertRaises(ValueError):
            OpenCodeV2Client(self.server.base_url, token="   ")

    def test_non_loopback_url_rejected(self) -> None:
        for url in (
            "http://example.com:443",
            "https://10.1.2.3:1234",
            "http://0.0.0.0:1",
            "ftp://127.0.0.1:1",
            "http://user:pass@127.0.0.1:1",
        ):
            with self.assertRaises(ValueError, msg=url):
                OpenCodeV2Client(url, token="t")

    def test_loopback_urls_accepted(self) -> None:
        for url in ("http://127.0.0.1:1", "http://localhost:4096", "http://[::1]:1"):
            client = OpenCodeV2Client(url, token="t")
            self.assertTrue(client.token)

    def test_redirect_blocked_without_token_forwarding(self) -> None:
        with self.assertRaises(OpenCodeApiError) as ctx:
            self.client._request("GET", "/api/redirect")
        self.assertEqual(ctx.exception.status, 302)
        self.assertIn("redirect blocked", ctx.exception.message)

    def test_unreachable_endpoint_is_transport_error(self) -> None:
        dead = OpenCodeV2Client("http://127.0.0.1:1", token="t", timeout=1.0)
        with self.assertRaises(OpenCodeTransportError):
            dead.info()

    def test_api_error_on_unknown_route(self) -> None:
        with self.assertRaises(OpenCodeApiError):
            self.client._request("GET", "/api/session/ses_missing")  # 404 SessionNotFound


class TestExplicitConfiguration(unittest.TestCase):
    def test_from_env_requires_explicit_base_url(self) -> None:
        with self.assertRaises(ValueError):
            OpenCodeV2Client.from_env({})  # no default endpoint, no discovery

    def test_from_env_requires_token(self) -> None:
        with self.assertRaises(ValueError):
            OpenCodeV2Client.from_env({"OPENCODE_SPIKE_BASE_URL": "http://127.0.0.1:9999"})

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

    def test_from_env_ignores_openrouter_key(self) -> None:
        # A provider key must not substitute for the explicit spike endpoint/token.
        with self.assertRaises(ValueError):
            OpenCodeV2Client.from_env({"OPENROUTER_API_KEY": "present-but-not-used"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
