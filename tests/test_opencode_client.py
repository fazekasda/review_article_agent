"""Unit suite for the packaged bounded OpenCode V2 lifecycle client.

Covers ``docs/implementation/04-agent-runtime.md`` deliverables 1-2 and acceptance
criteria 3 (fresh, different-concrete-model critic), 4 (lost event streams
reconcile from durable state/messages), and 7 (session/model isolation; the
ordinary OpenCode service is untouched), ``docs/specification.md`` "Product and
runtime" lines 14-16 and workflow line 38, ``docs/contracts/02-gates-and-runtime.md``
§8 (admission != completion; a same-ID replay is one accepted input; ambiguous
remote work reconciles, never resends under a new identity; missing events do not
lose a completion), and ``docs/decisions/001-runtime-language.md`` (explicit
loopback endpoint + credential, fail closed; redirects refused; only an
authoritative ``idle`` completes; a writer-history fork is not a clean critic).

Every test runs against the local, deterministic HTTP fixture in
``tests/_opencode_mock.py``. No live service, network, paid call, service
discovery, or private file is touched.

Run:  PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_opencode_client.py'
"""

from __future__ import annotations

import base64
import os
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
SRC = HERE.parent / "src"
for _path in (str(SRC), str(HERE)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from _opencode_mock import MockServer  # noqa: E402
from review_article_agent.runtime.opencode import (  # noqa: E402
    MAX_ID_LENGTH,
    MAX_JSON_DEPTH,
    MAX_MESSAGE_LIMIT,
    MAX_MESSAGE_PAGES_CAP,
    MAX_REQUEST_BYTES_CAP,
    MAX_RESPONSE_BYTES_CAP,
    MAX_RETRY_INTERVAL,
    MAX_TIMEOUT,
    MIN_RETRY_INTERVAL,
    Admission,
    AdmissionConflictError,
    AdmissionUnverifiedError,
    OpenCodeApiError,
    OpenCodeProtocolError,
    OpenCodeRequestTooLargeError,
    OpenCodeResponseTooLargeError,
    OpenCodeTransportError,
    OpenCodeV2Client,
    QuiescenceTimeout,
    ReconcileResult,
    validate_model_ref,
)

WRITER_MODEL = {"providerID": "openrouter", "id": "some/writer-model"}
CRITIC_MODEL = {"providerID": "openrouter", "id": "typesafe/jev-1.14"}


class _RecorderHandler(BaseHTTPRequestHandler):
    """Answers any request with a fixed body and records the request paths."""

    def log_message(self, *args: object) -> None:
        return

    def _respond(self) -> None:
        self.server.seen.append(self.path)  # type: ignore[attr-defined]
        body = self.server.body  # type: ignore[attr-defined]
        self.send_response(self.server.status)  # type: ignore[attr-defined]
        self.send_header("Content-Type", self.server.content_type)  # type: ignore[attr-defined]
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = _respond
    do_POST = _respond


class _RecorderServer:
    """A tiny loopback server used as a proxy decoy or a malformed-body origin."""

    def __init__(
        self,
        *,
        body: bytes = b"{}",
        content_type: str = "application/json",
        status: int = 200,
    ):
        self.seen: list[str] = []
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), _RecorderHandler)
        self._httpd.seen = self.seen  # type: ignore[attr-defined]
        self._httpd.body = body  # type: ignore[attr-defined]
        self._httpd.content_type = content_type  # type: ignore[attr-defined]
        self._httpd.status = status  # type: ignore[attr-defined]
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
        )

    @property
    def base_url(self) -> str:
        host, port = self._httpd.server_address[:2]
        return f"http://{host}:{port}"

    def __enter__(self) -> "_RecorderServer":
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=5)


class ClientTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.server = MockServer(token="test-token")
        self.server.__enter__()
        self.addCleanup(self.server.__exit__, None, None, None)
        self.client = OpenCodeV2Client(
            self.server.base_url, token="test-token", directory="/tmp/step04"
        )

    def _session(self) -> str:
        return self.client.create_session(agent="writer", model=WRITER_MODEL)["id"]


# ---------------------------------------------------------------------------
# Endpoint, auth, and redacted errors
# ---------------------------------------------------------------------------
class TestLoopbackAndAuth(unittest.TestCase):
    def test_loopback_urls_accepted(self) -> None:
        for url in ("http://127.0.0.1:1", "http://localhost:4096", "http://[::1]:1"):
            client = OpenCodeV2Client(url, token="t")
            self.assertEqual(client.auth, "bearer")

    def test_invalid_client_construction_rejected(self) -> None:
        # One table per validator: every rejected constructor input.
        cases = {
            "non_loopback": lambda: OpenCodeV2Client("http://example.com:443", token="t"),
            "https_private": lambda: OpenCodeV2Client("https://10.1.2.3:1234", token="t"),
            "wildcard_host": lambda: OpenCodeV2Client("http://0.0.0.0:1", token="t"),
            "bad_scheme": lambda: OpenCodeV2Client("ftp://127.0.0.1:1", token="t"),
            "embedded_credentials": lambda: OpenCodeV2Client("http://user:pass@127.0.0.1:1", token="t"),
            "not_a_url": lambda: OpenCodeV2Client("not a url", token="t"),
            "missing_token": lambda: OpenCodeV2Client("http://127.0.0.1:1"),
            "blank_token": lambda: OpenCodeV2Client("http://127.0.0.1:1", token="   "),
            "timeout_zero": lambda: OpenCodeV2Client("http://127.0.0.1:1", token="t", timeout=0),
            "timeout_negative": lambda: OpenCodeV2Client("http://127.0.0.1:1", token="t", timeout=-1.0),
            "max_message_pages_zero": lambda: OpenCodeV2Client(
                "http://127.0.0.1:1", token="t", max_message_pages=0
            ),
            "max_response_bytes_zero": lambda: OpenCodeV2Client(
                "http://127.0.0.1:1", token="t", max_response_bytes=0
            ),
            "unknown_auth_scheme": lambda: OpenCodeV2Client(
                "http://127.0.0.1:1", auth="digest", password="x"
            ),
            "basic_with_token": lambda: OpenCodeV2Client(
                "http://127.0.0.1:1", token="t", auth="basic", password="p"
            ),
            "token_and_password": lambda: OpenCodeV2Client(
                "http://127.0.0.1:1", token="t", password="p"
            ),
            "basic_without_password": lambda: OpenCodeV2Client("http://127.0.0.1:1", auth="basic"),
            "basic_username_without_password": lambda: OpenCodeV2Client(
                "http://127.0.0.1:1", auth="basic", username="opencode"
            ),
        }
        for label, build in cases.items():
            with self.subTest(case=label):
                with self.assertRaises(ValueError):
                    build()

    def test_info_exposes_server_version(self) -> None:
        with MockServer(token="t") as server:
            client = OpenCodeV2Client(server.base_url, token="t")
            info = client.info()
        self.assertIsInstance(info["version"], str)
        self.assertTrue(info["version"])

    def test_repr_never_leaks_the_credential(self) -> None:
        client = OpenCodeV2Client("http://127.0.0.1:1", token="super-secret-token")
        self.assertNotIn("super-secret-token", repr(client))


class TestBasicAuth(unittest.TestCase):
    """The observed 2.2.0 service answers HTTP Basic ``opencode:<password>``."""

    def setUp(self) -> None:
        self.server = MockServer(basic_password="per-run-pw")
        self.server.__enter__()
        self.addCleanup(self.server.__exit__, None, None, None)

    def test_basic_auth_reaches_the_server(self) -> None:
        client = OpenCodeV2Client(self.server.base_url, auth="basic", password="per-run-pw")
        self.assertEqual(client.auth, "basic")
        self.assertEqual(client.username, "opencode")  # documented default
        self.assertIsNone(client.token)
        self.assertIn("version", client.info())
        expected = "Basic " + base64.b64encode(b"opencode:per-run-pw").decode("ascii")
        auths = [req["authorization"] for req in self.server.state.requests]
        self.assertTrue(auths)
        self.assertTrue(all(auth == expected for auth in auths), auths)

    def test_basic_wrong_password_is_a_redacted_unauthorized_error(self) -> None:
        client = OpenCodeV2Client(self.server.base_url, auth="basic", password="wrong")
        with self.assertRaises(OpenCodeApiError) as ctx:
            client.info()
        self.assertEqual(ctx.exception.status, 401)
        self.assertEqual(ctx.exception.tag, "UnauthorizedError")
        # static/redacted: no peer message text or secret is surfaced
        self.assertNotIn("peer-secret-leak-42", str(ctx.exception))


class TestTransportSafety(unittest.TestCase):
    def setUp(self) -> None:
        self.server = MockServer(token="test-token")
        self.server.__enter__()
        self.addCleanup(self.server.__exit__, None, None, None)
        self.client = OpenCodeV2Client(self.server.base_url, token="test-token")

    def test_redirect_is_refused_without_forwarding_the_credential(self) -> None:
        before = len(self.server.state.requests)
        with self.assertRaises(OpenCodeApiError) as ctx:
            self.client._request("GET", "/api/redirect")
        self.assertEqual(ctx.exception.status, 302)
        self.assertIn("redirect refused", str(ctx.exception))
        # only the original request happened: the credential never left the origin
        self.assertEqual(len(self.server.state.requests), before + 1)

    def test_proxies_are_bypassed(self) -> None:
        with _RecorderServer() as decoy:
            env = {"http_proxy": decoy.base_url, "HTTP_PROXY": decoy.base_url,
                   "no_proxy": "", "NO_PROXY": ""}
            with mock.patch.dict(os.environ, env):
                info = self.client.info()
        self.assertTrue(info["version"])  # reached the real origin directly
        self.assertEqual(decoy.seen, [])  # the proxy received nothing

    def test_response_size_is_bounded(self) -> None:
        self.server.state.info_padding = 4096
        bounded = OpenCodeV2Client(
            self.server.base_url, token="test-token", max_response_bytes=128
        )
        with self.assertRaises(OpenCodeResponseTooLargeError):
            bounded.info()

    def test_unreachable_endpoint_is_a_static_transport_error(self) -> None:
        dead = OpenCodeV2Client("http://127.0.0.1:1", token="t", timeout=1.0)
        with self.assertRaises(OpenCodeTransportError) as ctx:
            dead.info()
        self.assertEqual(str(ctx.exception), "GET /api/info: transport error")

    def test_non_json_body_is_a_protocol_error(self) -> None:
        with _RecorderServer(body=b"<html>not json</html>", content_type="text/html") as origin:
            client = OpenCodeV2Client(origin.base_url, token="t")
            with self.assertRaises(OpenCodeProtocolError):
                client.info()


# ---------------------------------------------------------------------------
# Malformed and foreign-identity responses
# ---------------------------------------------------------------------------
class TestMalformedAndForeignIdentity(ClientTestCase):
    def test_info_malformed_fails_closed(self) -> None:
        self.server.state.info_payload = ["not", "an", "object"]
        with self.assertRaises(OpenCodeProtocolError):
            self.client.info()
        self.server.state.info_payload = {"pid": 1}  # no version
        with self.assertRaises(OpenCodeProtocolError):
            self.client.info()

    def test_create_session_envelope_malformed_fails_closed(self) -> None:
        self.server.state.info_payload = None
        # a missing "data" object is a protocol failure
        self.server.state.create_model_override = None
        original = self.client._request

        def fake(method: str, path: str, **kwargs: object) -> object:
            if path == "/api/session":
                return {"nope": 1}
            return original(method, path, **kwargs)

        with mock.patch.object(self.client, "_request", side_effect=fake):
            with self.assertRaises(OpenCodeProtocolError):
                self.client.create_session(agent="writer", model=WRITER_MODEL)

    def test_create_session_refuses_a_foreign_pinned_model(self) -> None:
        # the server silently returned the writer model for a critic request
        self.server.state.create_model_override = WRITER_MODEL
        with self.assertRaises(OpenCodeProtocolError):
            self.client.create_session(agent="critic", model=CRITIC_MODEL)
        self.server.state.create_model_override = None
        self.server.state.create_agent_override = "build"
        with self.assertRaises(OpenCodeProtocolError):
            self.client.create_session(agent="critic", model=CRITIC_MODEL)

    def test_get_session_foreign_id_fails_closed(self) -> None:
        sid = self._session()
        self.server.state.session_get_id_override = "ses_someoneelse"
        with self.assertRaises(OpenCodeProtocolError):
            self.client.get_session(sid)

    def test_inbox_foreign_owner_fails_closed(self) -> None:
        sid = self._session()
        self.server.state.enqueue(sid, "msg_owned0001", "x")
        self.server.state.inbox_owner_override = "ses_someoneelse"
        with self.assertRaises(OpenCodeProtocolError):
            self.client.list_inbox(sid)

    def test_message_list_malformed_and_foreign_fail_closed(self) -> None:
        sid = self._session()
        self.client.admit(sid, "x", "msg_ml000001")
        self.server.state.message_list_payload = {"data": {"not": "a list"}}
        with self.assertRaises(OpenCodeProtocolError):
            self.client.list_messages(sid)
        self.server.state.message_list_payload = {"data": [{"id": "bad-id", "type": "user"}]}
        with self.assertRaises(OpenCodeProtocolError):
            self.client.list_messages(sid)
        self.server.state.message_list_payload = {
            "data": [{"id": "msg_ok000001", "type": "user", "sessionID": "ses_someoneelse"}]
        }
        with self.assertRaises(OpenCodeProtocolError):
            self.client.list_messages(sid)

    def test_prompt_receipt_foreign_id_fails_closed(self) -> None:
        sid = self._session()
        self.server.state.receipt_id_override = "msg_someoneelse"
        with self.assertRaises(OpenCodeProtocolError):
            self.client.admit(sid, "x", "msg_asked0001")

    def test_message_get_foreign_id_fails_closed(self) -> None:
        sid = self._session()
        self.client.admit(sid, "x", "msg_get00001")
        self.server.state.message_get_id_override = "msg_other0001"
        with self.assertRaises(OpenCodeProtocolError):
            self.client.get_message(sid, "msg_get00001")


# ---------------------------------------------------------------------------
# Session lifecycle (pinning) and input validation
# ---------------------------------------------------------------------------
class TestSessionLifecycle(ClientTestCase):
    def test_create_session_pins_role_and_model(self) -> None:
        session = self.client.create_session(agent="writer", model=WRITER_MODEL, title="t")
        self.assertTrue(session["id"].startswith("ses"))
        self.assertEqual(session["agent"], "writer")
        self.assertEqual(session["model"], WRITER_MODEL)
        self.assertEqual(session["location"]["directory"], "/tmp/step04")
        self.assertEqual(self.client.get_session(session["id"])["id"], session["id"])

    def test_create_session_requires_agent_and_model(self) -> None:
        with self.assertRaises(TypeError):
            self.client.create_session(model=WRITER_MODEL)  # type: ignore[call-arg]
        with self.assertRaises(ValueError):
            self.client.create_session(agent="  ", model=WRITER_MODEL)
        with self.assertRaises(ValueError):
            self.client.create_session(agent="writer", model={"id": "x"})  # no providerID
        with self.assertRaises(ValueError):
            self.client.create_session(
                agent="writer", model={"id": "x", "providerID": "p", "bogus": 1}
            )


# ---------------------------------------------------------------------------
# Admission: durable receipt, idempotency, divergent replay, missed events
# ---------------------------------------------------------------------------
class TestAdmission(ClientTestCase):
    def test_admit_returns_durable_receipt_not_completion(self) -> None:
        sid = self._session()
        admission = self.client.admit(sid, "hello", "msg_admit0001")
        self.assertIsInstance(admission, Admission)
        self.assertTrue(admission.has_durable_receipt)
        self.assertFalse(admission.duplicate)
        receipt = admission.receipt
        self.assertEqual(receipt["type"], "user")
        self.assertEqual(receipt["id"], "msg_admit0001")
        found = self.client.reconcile(sid, "msg_admit0001")
        self.assertEqual(found.state, "in_progress")
        self.assertTrue(found.is_durable)
        self.assertFalse(found.is_complete)

    def test_retry_same_id_same_text_yields_single_input(self) -> None:
        sid = self._session()
        first = self.client.admit(sid, "hello", "msg_retry0001")
        self.assertFalse(first.duplicate)
        second = self.client.admit(sid, "hello", "msg_retry0001")  # 409 -> reconcile
        self.assertTrue(second.duplicate)
        self.assertTrue(second.reconciled)
        self.assertTrue(second.has_durable_receipt)
        inbox = self.client.list_inbox(sid)
        self.assertEqual([i["id"] for i in inbox if i["id"] == "msg_retry0001"], ["msg_retry0001"])

    def test_retry_same_id_same_text_on_200_replay_yields_single_input(self) -> None:
        # The observed real 2.2.0 service returned 200 again on a same-ID retry.
        self.server.state.prompt_repeat_409 = False
        sid = self._session()
        self.client.admit(sid, "hello", "msg_replay001")
        second = self.client.admit(sid, "hello", "msg_replay001")
        self.assertTrue(second.has_durable_receipt)
        users = [
            m["id"]
            for m in self.client.list_messages(sid)["data"]
            if m.get("type") == "user"
        ]
        self.assertEqual(users, ["msg_replay001"])

    def test_missed_events_second_client_retrieves_persisted_state(self) -> None:
        sid = self._session()
        self.client.admit(sid, "durable work", "msg_missed0001")
        second = OpenCodeV2Client(self.server.base_url, token="test-token")
        self.assertEqual([i["id"] for i in second.list_inbox(sid)], ["msg_missed0001"])
        found = second.reconcile(sid, "msg_missed0001")
        self.assertTrue(found.is_durable)
        self.assertFalse(found.is_complete)

    def test_transport_drop_is_reconciled_not_resent(self) -> None:
        sid = self._session()
        self.server.state.prompt_drops = 1  # accepted durably, then connection lost
        admission = self.client.admit(sid, "flaky", "msg_drop0001")
        self.assertTrue(admission.reconciled)
        self.assertFalse(admission.duplicate)
        self.assertEqual(admission.receipt["id"], "msg_drop0001")

    def test_unknown_admission_never_reports_complete(self) -> None:
        sid = self._session()
        found = self.client.reconcile(sid, "msg_ghost0001")
        self.assertEqual(found.state, "unknown")
        self.assertFalse(found.is_durable)
        self.assertFalse(found.is_complete)

    def test_reconcile_completes_only_on_authoritative_idle(self) -> None:
        sid = self._session()
        self.client.admit(sid, "work", "msg_done0001")
        self.server.state.append_assistant(sid, "result")
        self.assertFalse(self.client.reconcile(sid, "msg_done0001").is_complete)
        self.server.state.append_idle(sid, "succeeded")
        found = self.client.reconcile(sid, "msg_done0001")
        self.assertEqual(found.state, "completed")
        self.assertEqual(found.outcome, "succeeded")

    def test_invalid_admission_inputs_rejected_locally(self) -> None:
        sid = self._session()
        with self.assertRaises(ValueError):
            self.client.admit(sid, "hi", "nope0001")  # not ^msg_
        with self.assertRaises(ValueError):
            self.client.admit(sid, "   ", "msg_bad0001")
        with self.assertRaises(ValueError):
            self.client.admit(sid, "hi", "msg_bad0002", delivery="urgent")
        with self.assertRaises(ValueError):
            self.client.admit("bad-session", "hi", "msg_bad0003")


class TestSameIdDivergentInput(ClientTestCase):
    def test_conflict_replay_with_divergent_text_is_refused(self) -> None:
        sid = self._session()
        self.client.admit(sid, "first input", "msg_diverge01")
        with self.assertRaises(AdmissionConflictError):
            self.client.admit(sid, "second input", "msg_diverge01")

    def test_200_replay_with_divergent_text_is_refused(self) -> None:
        self.server.state.prompt_repeat_409 = False
        sid = self._session()
        self.client.admit(sid, "first input", "msg_diverge02")
        with self.assertRaises(AdmissionConflictError):
            self.client.admit(sid, "second input", "msg_diverge02")

    def test_conflict_is_a_value_error_for_existing_handlers(self) -> None:
        sid = self._session()
        self.client.admit(sid, "first", "msg_diverge03")
        with self.assertRaises(ValueError):
            self.client.admit(sid, "other", "msg_diverge03")


# ---------------------------------------------------------------------------
# Per-turn reconciliation: cross-turn terminals and restart-repair
# ---------------------------------------------------------------------------
class TestPerTurnReconciliation(ClientTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.sid = self._session()

    def test_second_queued_turn_not_falsely_complete(self) -> None:
        self.server.state.append_user(self.sid, "msg_turn1a", "first")
        self.server.state.append_idle(self.sid, "succeeded")
        self.server.state.enqueue(self.sid, "msg_turn2a", "second")
        found = self.client.reconcile(self.sid, "msg_turn2a")
        self.assertEqual(found.state, "queued")
        self.assertFalse(found.is_complete)
        self.assertTrue(found.is_durable)

    def test_prior_idle_does_not_complete_later_turn(self) -> None:
        self.server.state.append_user(self.sid, "msg_turn1b", "first")
        self.server.state.append_idle(self.sid, "succeeded")
        self.server.state.append_user(self.sid, "msg_turn2b", "second")
        found = self.client.reconcile(self.sid, "msg_turn2b")
        self.assertEqual(found.state, "in_progress")
        self.assertFalse(found.is_complete)

    def test_terminal_after_next_turn_does_not_complete_target(self) -> None:
        self.server.state.append_user(self.sid, "msg_turn1c", "first")
        self.server.state.append_user(self.sid, "msg_turn2c", "second")
        self.server.state.append_assistant(self.sid, "second done")
        self.server.state.append_idle(self.sid, "succeeded")
        self.assertEqual(self.client.reconcile(self.sid, "msg_turn1c").state, "in_progress")

    def test_success_failure_refusal_and_interruption_are_distinct(self) -> None:
        self.server.state.append_user(self.sid, "msg_ok000001", "a")
        self.server.state.append_idle(self.sid, "succeeded")
        self.server.state.append_user(self.sid, "msg_fail0001", "b")
        self.server.state.append_idle(self.sid, "failed")
        self.server.state.append_user(self.sid, "msg_intr0001", "c")
        self.server.state.append_idle(self.sid, "interrupted")
        self.server.state.append_user(self.sid, "msg_refuse01", "d")
        self.server.state.append_assistant(
            self.sid, "", finish="error", error={"message": "refused"}
        )
        self.server.state.append_user(self.sid, "msg_weird01", "e")
        self.server.state.append_idle(self.sid, "vanished")

        states = {
            name: self.client.reconcile(self.sid, mid)
            for name, mid in (
                ("success", "msg_ok000001"),
                ("failed", "msg_fail0001"),
                ("interrupted", "msg_intr0001"),
                ("refusal", "msg_refuse01"),
                ("unknown", "msg_weird01"),
            )
        }
        self.assertEqual(states["success"].state, "completed")
        self.assertEqual(states["failed"].state, "failed")
        self.assertEqual(states["interrupted"].state, "interrupted")
        # an assistant refusal with no authoritative idle is unknown, never
        # success and never a confident failed/interrupted
        self.assertEqual(states["refusal"].state, "unknown")
        self.assertEqual(states["unknown"].state, "unknown")
        self.assertEqual(
            {s.state for s in states.values()},
            {"completed", "failed", "interrupted", "unknown"},
        )
        self.assertEqual(
            [s.state for s in states.values() if s.is_complete], ["completed"]
        )

    def test_restart_errored_assistant_is_unknown_not_terminal(self) -> None:
        # Observed real restart shape: user input + partial assistant finish="error",
        # no idle. Reconciliation reports unknown, never success/in_progress.
        self.server.state.append_user(self.sid, "msg_restart01", "restart probe")
        self.server.state.append_assistant(self.sid, "", finish="error")
        found = self.client.reconcile(self.sid, "msg_restart01")
        self.assertEqual(found.state, "unknown")
        self.assertFalse(found.is_terminal)
        self.assertFalse(found.is_complete)
        self.assertIsNotNone(found.durable_receipt)
        # a later authoritative idle still resolves the same admission
        self.server.state.append_idle(self.sid, "succeeded")
        self.assertEqual(self.client.reconcile(self.sid, "msg_restart01").state, "completed")

    def test_unrecognized_idle_outcome_is_unknown(self) -> None:
        self.server.state.append_user(self.sid, "msg_unrec001", "x")
        self.server.state.append_idle(self.sid, "bogus")
        self.assertEqual(self.client.reconcile(self.sid, "msg_unrec001").state, "unknown")

    def test_reconcile_result_type_is_documented(self) -> None:
        self.server.state.append_user(self.sid, "msg_type0001", "x")
        found = self.client.reconcile(self.sid, "msg_type0001")
        self.assertIsInstance(found, ReconcileResult)
        self.assertFalse(found.is_terminal)


# ---------------------------------------------------------------------------
# Paging: older admissions and honest bounds
# ---------------------------------------------------------------------------
class TestPaging(ClientTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.sid = self._session()

    def _fill_turns(self, count: int) -> None:
        for index in range(count):
            self.server.state.append_user(self.sid, f"msg_fill{index:04d}", "filler")
            self.server.state.append_idle(self.sid, "succeeded")

    def test_paging_finds_older_admission(self) -> None:
        self._fill_turns(30)
        self.server.state.append_user(self.sid, "msg_target01", "target")
        self.server.state.append_idle(self.sid, "succeeded")
        self.server.state.message_page_size = 20
        found = self.client.reconcile(self.sid, "msg_target01")
        self.assertEqual(found.state, "completed")

    def test_paging_bound_returns_unknown_not_complete(self) -> None:
        self._fill_turns(30)
        self.server.state.append_user(self.sid, "msg_target02", "target")
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
        self.server.state.append_user(self.sid, "msg_target03", "target")
        for _ in range(40):
            self.server.state.append_system(self.sid, "padding")
        self.server.state.message_page_size = 20
        bounded = OpenCodeV2Client(
            self.server.base_url, token="test-token", max_message_pages=1
        )
        found = bounded.reconcile(self.sid, "msg_target03")
        self.assertEqual(found.state, "unknown")
        self.assertIn("not fully read", found.reason)

    def test_collect_messages_order_only_on_first_page_and_type_repeated(self) -> None:
        for index in range(6):
            if index % 2 == 0:
                self.server.state.append_user(self.sid, f"msg_u{index:04d}", "u")
            else:
                self.server.state.append_system(self.sid, "s")
        self.server.state.message_page_size = 2
        messages, truncated = self.client.collect_messages(self.sid, type="system")
        self.assertFalse(truncated)
        self.assertTrue(messages)
        self.assertTrue(all(m["type"] == "system" for m in messages))
        calls = [
            r
            for r in self.server.state.requests
            if r["path"] == f"/api/session/{self.sid}/message"
        ]
        self.assertGreater(len(calls), 1)
        self.assertEqual(calls[0]["query"].get("order"), ["asc"])
        self.assertNotIn("cursor", calls[0]["query"])
        for call in calls:
            self.assertEqual(call["query"].get("type"), ["system"])
        for call in calls[1:]:
            self.assertIn("cursor", call["query"])
            self.assertNotIn("order", call["query"])

    def test_fixture_rejects_cursor_with_order(self) -> None:
        with self.assertRaises(OpenCodeApiError) as ctx:
            self.client.list_messages(self.sid, order="asc", cursor="2")
        self.assertEqual(ctx.exception.status, 400)


# ---------------------------------------------------------------------------
# Bounded wait and interrupt
# ---------------------------------------------------------------------------
class TestWaitInterrupt(ClientTestCase):
    def test_wait_returns_when_idle(self) -> None:
        sid = self._session()
        self.server.state.wait_503_times = 2
        self.assertTrue(self.client.wait(sid, timeout=2.0, retry_interval=0.02))

    def test_bounded_wait_times_out(self) -> None:
        sid = self._session()
        self.server.state.wait_status = 503
        start = time.monotonic()
        with self.assertRaises(QuiescenceTimeout):
            self.client.wait(sid, timeout=0.4, retry_interval=0.05)
        self.assertLess(time.monotonic() - start, 2.0)

    def test_interrupt_returns_bool(self) -> None:
        sid = self._session()
        self.server.state.interrupt_result = True
        self.assertTrue(self.client.interrupt(sid))
        self.server.state.interrupt_result = False
        self.assertFalse(self.client.interrupt(sid, resume=False))


# ---------------------------------------------------------------------------
# Fresh critic: new session, different concrete model, never a fork
# ---------------------------------------------------------------------------
class TestFreshCritic(ClientTestCase):
    def test_critic_is_a_fresh_new_session_not_a_fork(self) -> None:
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
        # the critic session carries no writer history
        self.assertEqual(self.client.list_inbox(critic["id"]), [])

    def test_critic_model_must_differ_concretely(self) -> None:
        with self.assertRaises(ValueError):
            self.client.start_critic(
                agent="critic", model=WRITER_MODEL, writer_model=WRITER_MODEL
            )

    def test_variant_only_change_is_insufficient(self) -> None:
        variant_only = {
            "providerID": WRITER_MODEL["providerID"],
            "id": WRITER_MODEL["id"],
            "variant": "high",
        }
        with self.assertRaises(ValueError):
            self.client.start_critic(
                agent="critic", model=variant_only, writer_model=WRITER_MODEL
            )

    def test_critic_requires_writer_model_and_role(self) -> None:
        # writer_model is required, so a critic can never silently reuse the writer
        with self.assertRaises(TypeError):
            self.client.start_critic(agent="critic", model=CRITIC_MODEL)  # type: ignore[call-arg]
        with self.assertRaises(ValueError):
            self.client.start_critic(agent="", model=CRITIC_MODEL, writer_model=WRITER_MODEL)

    def test_server_cannot_substitute_the_writer_model_for_the_critic(self) -> None:
        self.server.state.create_model_override = WRITER_MODEL
        with self.assertRaises(OpenCodeProtocolError):
            self.client.start_critic(
                agent="critic", model=CRITIC_MODEL, writer_model=WRITER_MODEL
            )


# ---------------------------------------------------------------------------
# Repair R1: boundedness, redaction, URL-path safety, strict JSON, pin proof
# ---------------------------------------------------------------------------
class TestFiniteBounds(unittest.TestCase):
    """Every accepted limit is finite and capped, including per-call overrides."""

    def _reject(self, **kwargs: object) -> None:
        with self.assertRaises(ValueError, msg=repr(kwargs)):
            OpenCodeV2Client("http://127.0.0.1:1", token="t", **kwargs)

    def test_invalid_bounds_rejected(self) -> None:
        # One table per validator: every rejected per-call/constructor bound.
        cases = {
            "timeout_nan": {"timeout": float("nan")},
            "timeout_inf": {"timeout": float("inf")},
            "timeout_negative_inf": {"timeout": float("-inf")},
            "timeout_bool_true": {"timeout": True},
            "timeout_bool_false": {"timeout": False},
            "timeout_zero": {"timeout": 0},
            "timeout_negative": {"timeout": -1.0},
            "timeout_over_cap": {"timeout": MAX_TIMEOUT + 1},
            "max_message_pages_inf": {"max_message_pages": float("inf")},
            "max_message_pages_nan": {"max_message_pages": float("nan")},
            "max_message_pages_bool": {"max_message_pages": True},
            "max_message_pages_float": {"max_message_pages": 3.0},
            "max_message_pages_str": {"max_message_pages": "3"},
            "max_message_pages_zero": {"max_message_pages": 0},
            "max_message_pages_negative": {"max_message_pages": -1},
            "max_message_pages_over_cap": {"max_message_pages": MAX_MESSAGE_PAGES_CAP + 1},
            "max_message_pages_huge": {"max_message_pages": 10**9},
            "max_response_bytes_inf": {"max_response_bytes": float("inf")},
            "max_response_bytes_bool": {"max_response_bytes": True},
            "max_response_bytes_zero": {"max_response_bytes": 0},
            "max_response_bytes_over_cap": {"max_response_bytes": MAX_RESPONSE_BYTES_CAP + 1},
            "max_request_bytes_inf": {"max_request_bytes": float("inf")},
            "max_request_bytes_bool": {"max_request_bytes": True},
            "max_request_bytes_zero": {"max_request_bytes": 0},
            "max_request_bytes_over_cap": {"max_request_bytes": MAX_REQUEST_BYTES_CAP + 1},
        }
        for label, kwargs in cases.items():
            with self.subTest(case=label):
                self._reject(**kwargs)

    def test_valid_bounds_are_accepted(self) -> None:
        client = OpenCodeV2Client(
            "http://127.0.0.1:1",
            token="t",
            timeout=1,
            max_message_pages=5,
            max_response_bytes=1024,
            max_request_bytes=1024,
        )
        self.assertEqual(client.timeout, 1.0)
        self.assertEqual(client.max_message_pages, 5)
        self.assertEqual(client.max_request_bytes, 1024)


class TestOriginOnlyBaseUrl(unittest.TestCase):
    def test_path_query_fragment_userinfo_and_control_chars_rejected(self) -> None:
        for bad in (
            "http://127.0.0.1:4096/api",
            "http://127.0.0.1:4096/x?y=1",
            "http://127.0.0.1:4096#frag",
            "http://user:pass@127.0.0.1:4096",
            "http://127.0.0.1:4096@evil.invalid",
            "http://127.0.0.1:4096\n",
            "http://127.0.0.1:bad",
        ):
            with self.assertRaises(ValueError, msg=bad):
                OpenCodeV2Client(bad, token="t")

    def test_plain_origins_accepted(self) -> None:
        for good in ("http://127.0.0.1:4096", "http://localhost:4096/", "http://[::1]:1"):
            OpenCodeV2Client(good, token="t")


class TestUrlSafeIdentifiers(unittest.TestCase):
    """A published ``^ses``/``^msg`` prefix is not URL path safety."""

    def setUp(self) -> None:
        self.server = MockServer(token="t")
        self.server.__enter__()
        self.addCleanup(self.server.__exit__, None, None, None)
        self.client = OpenCodeV2Client(self.server.base_url, token="t")
        self.sid = self.client.create_session(agent="writer", model=WRITER_MODEL)["id"]

    def test_pathlike_session_ids_rejected_without_a_request(self) -> None:
        for bad in (
            "ses/../../api/anything?x=1",
            "ses x",
            "ses%2f",
            "ses..",
            "ses\n",
            "ses#f",
            "ses?a=b",
            "ses",
            "",
            "ses/",
            "ses.foo",
            "sess/../ses_ok",
        ):
            before = len(self.server.state.requests)
            with self.assertRaises(ValueError, msg=bad):
                self.client.get_session(bad)
            self.assertEqual(len(self.server.state.requests), before)  # no request sent

    def test_safe_session_ids_accepted(self) -> None:
        for good in ("ses_abc123", "ses-X_Y", "ses_0"):
            OpenCodeV2Client._require_session_id(good)

    def test_trailing_newline_and_control_ids_rejected(self) -> None:
        # ``re.match`` + ``$`` accepts a trailing newline; ``fullmatch`` does not.
        for bad in ("ses_foo\n", "ses_foo\r", "ses_foo\t", "ses_foo\x00"):
            with self.assertRaises(ValueError, msg=repr(bad)):
                self.client.get_session(bad)
        for bad in ("msg_foo\n", "msg_foo\r"):
            with self.assertRaises(ValueError, msg=repr(bad)):
                self.client.admit(self.sid, "x", bad)

    def test_over_long_ids_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.client.get_session("ses_" + "a" * 200)
        with self.assertRaises(ValueError):
            self.client.admit(self.sid, "x", "msg_" + "a" * 200)
        # exactly at the length bound the helper still accepts the id
        OpenCodeV2Client._require_session_id("ses_" + "a" * (MAX_ID_LENGTH - 4))

    def test_pathlike_message_ids_rejected(self) -> None:
        for bad in ("msg_../x", "msg_a/b", "msg_a?b", "msg_%2f", "msg_", "msg_a b", "msg_a.b"):
            with self.assertRaises(ValueError, msg=bad):
                self.client.admit(self.sid, "x", bad)

    def test_safe_message_ids_accepted(self) -> None:
        OpenCodeV2Client._require_message_id("msg_abc-1_2")


class TestCuratedErrors(unittest.TestCase):
    """Only an allowlisted ``_tag`` may reach an exception; never peer text."""

    def test_unknown_tag_is_not_echoed(self) -> None:
        err = OpenCodeApiError(400, "SYNTHETIC_SECRET_CANARY")
        self.assertIsNone(err.tag)
        self.assertNotIn("SYNTHETIC_SECRET_CANARY", str(err))

    def test_known_tag_is_preserved(self) -> None:
        err = OpenCodeApiError(409, "ConflictError")
        self.assertEqual(err.tag, "ConflictError")
        self.assertIn("ConflictError", str(err))

    def test_unknown_server_tag_is_dropped(self) -> None:
        body = b'{"_tag":"SYNTHETIC_SECRET_CANARY","message":"peer-secret-leak-42"}'
        with _RecorderServer(status=400, body=body) as origin:
            client = OpenCodeV2Client(origin.base_url, token="t")
            with self.assertRaises(OpenCodeApiError) as ctx:
                client._request("GET", "/api/info")
        self.assertIsNone(ctx.exception.tag)
        self.assertNotIn("SYNTHETIC_SECRET_CANARY", str(ctx.exception))
        self.assertNotIn("peer-secret-leak-42", str(ctx.exception))

    def test_known_server_tag_is_surfaced(self) -> None:
        with _RecorderServer(status=409, body=b'{"_tag":"ConflictError","message":"x"}') as origin:
            client = OpenCodeV2Client(origin.base_url, token="t")
            with self.assertRaises(OpenCodeApiError) as ctx:
                client._request("GET", "/api/info")
        self.assertEqual(ctx.exception.tag, "ConflictError")


class TestStrictJson(unittest.TestCase):
    """``json.loads`` leniency (duplicate keys, NaN/Infinity, deep nesting) is off."""

    def test_malformed_json_rejected(self) -> None:
        # One table per validator: every malformed body _decode must refuse.
        cases = {
            "duplicate_keys": b'{"data":1,"data":2}',
            "non_finite_nan": b'{"data":NaN}',
            "non_finite_infinity": b'{"data":Infinity}',
            "non_finite_negative_infinity": b'{"data":-Infinity}',
            "over_deep_json": b"[" * 100000 + b"]" * 100000,
            "numeric_overflow_1e999": b'{"cost":1e999}',
            "numeric_overflow_negative_1e999": b'{"cost":-1e999}',
            "numeric_overflow_1e309": b'{"cost":1e309}',
            "nesting_depth_over_cap": b"[" * (MAX_JSON_DEPTH + 1) + b"]" * (MAX_JSON_DEPTH + 1),
            "non_json": b"not json",
        }
        for label, body in cases.items():
            with self.subTest(case=label):
                with self.assertRaises(OpenCodeProtocolError):
                    OpenCodeV2Client._decode(body)

    def test_valid_exponents_and_depth_at_cap_are_accepted(self) -> None:
        # a non-overflowing exponent and the exact depth cap are still valid JSON
        self.assertEqual(OpenCodeV2Client._decode(b'{"cost":1e308}'), {"cost": 1e308})
        value = OpenCodeV2Client._decode(b"[" * MAX_JSON_DEPTH + b"]" * MAX_JSON_DEPTH)
        self.assertIsInstance(value, list)

    def test_brackets_inside_strings_do_not_count_toward_depth(self) -> None:
        self.assertEqual(
            OpenCodeV2Client._decode(b'{"s":"[[[[[[[[[[."}'), {"s": "[[[[[[[[[[."}
        )

    def test_valid_json_round_trips_and_empty_is_none(self) -> None:
        self.assertIsNone(OpenCodeV2Client._decode(b""))
        self.assertEqual(OpenCodeV2Client._decode(b'{"data":1}'), {"data": 1})

    def test_non_json_response_over_http_is_protocol_error(self) -> None:
        with _RecorderServer(body=b'{"data":1,"data":2}') as origin:
            client = OpenCodeV2Client(origin.base_url, token="t")
            with self.assertRaises(OpenCodeProtocolError):
                client.info()


class TestRequestSizeBound(unittest.TestCase):
    def setUp(self) -> None:
        self.server = MockServer(token="t")
        self.server.__enter__()
        self.addCleanup(self.server.__exit__, None, None, None)
        # large enough for a pinned create, too small for the oversized requests
        self.client = OpenCodeV2Client(self.server.base_url, token="t", max_request_bytes=200)

    def test_oversized_admission_request_rejected(self) -> None:
        sid = self.client.create_session(agent="writer", model=WRITER_MODEL)["id"]
        with self.assertRaises(OpenCodeRequestTooLargeError):
            self.client.admit(sid, "x" * 500, "msg_big00001")

    def test_oversized_create_request_rejected(self) -> None:
        with self.assertRaises(OpenCodeRequestTooLargeError):
            self.client.create_session(agent="writer", model=WRITER_MODEL, title="t" * 500)

    def test_request_error_is_a_value_error(self) -> None:
        self.assertTrue(issubclass(OpenCodeRequestTooLargeError, ValueError))


class TestPagingCursorValidation(unittest.TestCase):
    def setUp(self) -> None:
        self.server = MockServer(token="t")
        self.server.__enter__()
        self.addCleanup(self.server.__exit__, None, None, None)
        self.client = OpenCodeV2Client(self.server.base_url, token="t")
        self.sid = self.client.create_session(agent="writer", model=WRITER_MODEL)["id"]

    def test_malformed_cursor_next_is_protocol_error(self) -> None:
        message = {"id": "msg_cur000001", "type": "user"}
        for bad_next in (["x"], 123, "", {}, 0):
            self.server.state.message_list_payload = {
                "data": [message],
                "cursor": {"next": bad_next},
            }
            with self.assertRaises(OpenCodeProtocolError, msg=repr(bad_next)):
                self.client.collect_messages(self.sid)

    def test_null_cursor_next_is_a_clean_end(self) -> None:
        self.server.state.message_list_payload = {
            "data": [{"id": "msg_cur000002", "type": "user"}],
            "cursor": {"next": None},
        }
        messages, truncated = self.client.collect_messages(self.sid)
        self.assertFalse(truncated)
        self.assertEqual([m["id"] for m in messages], ["msg_cur000002"])

    def test_paging_arguments_validated(self) -> None:
        # One table per validator: every rejected collect_messages/list_messages arg.
        cases = {
            "max_pages_zero": lambda: self.client.collect_messages(self.sid, max_pages=0),
            "max_pages_negative": lambda: self.client.collect_messages(self.sid, max_pages=-1),
            "max_pages_bool": lambda: self.client.collect_messages(self.sid, max_pages=True),
            "max_pages_float": lambda: self.client.collect_messages(self.sid, max_pages=3.5),
            "max_pages_str": lambda: self.client.collect_messages(self.sid, max_pages="3"),
            "max_pages_nan": lambda: self.client.collect_messages(self.sid, max_pages=float("nan")),
            "max_pages_inf": lambda: self.client.collect_messages(self.sid, max_pages=float("inf")),
            "max_pages_over_cap": lambda: self.client.collect_messages(
                self.sid, max_pages=MAX_MESSAGE_PAGES_CAP + 1
            ),
            "limit_zero": lambda: self.client.list_messages(self.sid, limit=0),
            "limit_negative": lambda: self.client.list_messages(self.sid, limit=-1),
            "limit_bool": lambda: self.client.list_messages(self.sid, limit=True),
            "limit_over_max": lambda: self.client.list_messages(self.sid, limit=MAX_MESSAGE_LIMIT + 1),
            "cursor_int": lambda: self.client.list_messages(self.sid, cursor=123),
            "cursor_empty": lambda: self.client.list_messages(self.sid, cursor=""),
        }
        for label, call in cases.items():
            with self.subTest(case=label):
                with self.assertRaises(ValueError):
                    call()


class TestWaitBounds(unittest.TestCase):
    """``wait`` cannot busy-loop or hang on a bad timeout/interval."""

    def setUp(self) -> None:
        self.server = MockServer(token="t")
        self.server.__enter__()
        self.addCleanup(self.server.__exit__, None, None, None)
        self.client = OpenCodeV2Client(self.server.base_url, token="t")
        self.sid = self.client.create_session(agent="writer", model=WRITER_MODEL)["id"]

    def test_wait_bounds_validated(self) -> None:
        # One table per validator: every rejected wait timeout/interval.
        cases = {
            "retry_interval_zero": lambda: self.client.wait(self.sid, retry_interval=0),
            "retry_interval_negative": lambda: self.client.wait(self.sid, retry_interval=-1.0),
            "retry_interval_nan": lambda: self.client.wait(self.sid, retry_interval=float("nan")),
            "retry_interval_inf": lambda: self.client.wait(self.sid, retry_interval=float("inf")),
            "retry_interval_bool": lambda: self.client.wait(self.sid, retry_interval=True),
            "retry_interval_over_max": lambda: self.client.wait(
                self.sid, retry_interval=MAX_RETRY_INTERVAL + 1
            ),
            "timeout_zero": lambda: self.client.wait(self.sid, timeout=0),
            "timeout_negative": lambda: self.client.wait(self.sid, timeout=-1.0),
            "timeout_nan": lambda: self.client.wait(self.sid, timeout=float("nan")),
            "timeout_inf": lambda: self.client.wait(self.sid, timeout=float("inf")),
            "timeout_bool": lambda: self.client.wait(self.sid, timeout=True),
            "timeout_over_max": lambda: self.client.wait(self.sid, timeout=MAX_TIMEOUT + 1),
        }
        for label, call in cases.items():
            with self.subTest(case=label):
                with self.assertRaises(ValueError):
                    call()


class TestPinVerification(unittest.TestCase):
    def setUp(self) -> None:
        self.server = MockServer(token="t")
        self.server.__enter__()
        self.addCleanup(self.server.__exit__, None, None, None)
        self.client = OpenCodeV2Client(self.server.base_url, token="t")

    def test_verify_session_pins_proves_the_pins(self) -> None:
        sid = self.client.create_session(agent="writer", model=WRITER_MODEL)["id"]
        data = self.client.verify_session_pins(sid, agent="writer", model=WRITER_MODEL)
        self.assertEqual(data["agent"], "writer")
        self.assertEqual(data["model"], WRITER_MODEL)

    def test_create_tolerates_omitted_pins_but_verify_fails_closed(self) -> None:
        self.server.state.omit_pinned = True
        sid = self.client.create_session(agent="writer", model=WRITER_MODEL)["id"]
        # create does not invent a required wire field: omission is tolerated...
        self.assertNotIn("model", self.client.get_session(sid))
        # ...but the explicit proof requirement fails closed when the pin is absent
        with self.assertRaises(OpenCodeProtocolError):
            self.client.verify_session_pins(sid, agent="writer", model=WRITER_MODEL)

    def test_verify_session_pins_fails_when_recorded_field_differs(self) -> None:
        sid = self.client.create_session(agent="writer", model=WRITER_MODEL)["id"]
        self.server.state.sessions[sid]["model"] = CRITIC_MODEL
        with self.assertRaises(OpenCodeProtocolError):
            self.client.verify_session_pins(sid, agent="writer", model=WRITER_MODEL)

    def test_verify_session_pins_validates_its_arguments(self) -> None:
        sid = self.client.create_session(agent="writer", model=WRITER_MODEL)["id"]
        with self.assertRaises(ValueError):
            self.client.verify_session_pins(sid, agent="  ", model=WRITER_MODEL)
        with self.assertRaises(ValueError):
            self.client.verify_session_pins(sid, agent="writer", model={"id": "x"})


class TestUnverifiableAdmissionPayload(unittest.TestCase):
    def setUp(self) -> None:
        self.server = MockServer(token="t")
        self.server.__enter__()
        self.addCleanup(self.server.__exit__, None, None, None)
        self.client = OpenCodeV2Client(self.server.base_url, token="t")

    def test_admit_fails_closed_when_receipt_text_absent(self) -> None:
        self.server.state.receipt_omit_text = True
        sid = self.client.create_session(agent="writer", model=WRITER_MODEL)["id"]
        with self.assertRaises(AdmissionUnverifiedError):
            self.client.admit(sid, "first", "msg_notext01")

    def test_reconcile_path_fails_closed_when_text_unverifiable(self) -> None:
        self.server.state.receipt_omit_text = True
        self.server.state.prompt_drops = 1
        sid = self.client.create_session(agent="writer", model=WRITER_MODEL)["id"]
        with self.assertRaises(AdmissionUnverifiedError):
            self.client.admit(sid, "first", "msg_notext02")

    def test_unverified_error_is_a_value_error(self) -> None:
        self.assertTrue(issubclass(AdmissionUnverifiedError, ValueError))


class TestStaticReasonsAndRepr(unittest.TestCase):
    def setUp(self) -> None:
        self.server = MockServer(token="t")
        self.server.__enter__()
        self.addCleanup(self.server.__exit__, None, None, None)
        self.client = OpenCodeV2Client(self.server.base_url, token="t")
        self.sid = self.client.create_session(agent="writer", model=WRITER_MODEL)["id"]

    def test_unknown_idle_outcome_reason_is_static(self) -> None:
        self.server.state.append_user(self.sid, "msg_canary001", "x")
        self.server.state.append_idle(self.sid, "SECRET_OUTCOME_CANARY")
        found = self.client.reconcile(self.sid, "msg_canary001")
        self.assertEqual(found.state, "unknown")
        self.assertEqual(found.reason, "unrecognized idle outcome")
        self.assertNotIn("SECRET_OUTCOME_CANARY", found.reason)
        self.assertNotIn("SECRET_OUTCOME_CANARY", repr(found))

    def test_admission_receipt_is_hidden_from_repr(self) -> None:
        admission = Admission("msg_x0000001", {"payload": {"text": "PEER_SECRET"}})
        self.assertNotIn("PEER_SECRET", repr(admission))

    def test_reconcile_result_hides_peer_evidence_from_repr(self) -> None:
        result = ReconcileResult(
            "msg_x0000001",
            "completed",
            {"secret": "PEER_SECRET"},
            {"outcome": "PEER_SECRET"},
        )
        self.assertNotIn("PEER_SECRET", repr(result))


# ---------------------------------------------------------------------------
# Repair R2: fullmatch IDs, numeric overflow, malformed idle, int bounds,
# wait floor, typed static errors
# ---------------------------------------------------------------------------
class TestMalformedIdleOutcome(unittest.TestCase):
    """A malformed idle is a static ``unknown``, never a raw TypeError."""

    def _turn(self, messages: list) -> ReconcileResult:
        return OpenCodeV2Client._reconcile_turn(messages, 0, "msg_x", None, False)

    def test_non_string_or_unhashable_outcome_is_unknown(self) -> None:
        for outcome in ([], {}, 1, None, True):
            result = self._turn(
                [{"type": "user", "id": "msg_x"}, {"type": "idle", "outcome": outcome}]
            )
            self.assertEqual(result.state, "unknown", msg=repr(outcome))
            self.assertFalse(result.is_complete)
            self.assertEqual(result.reason, "malformed idle outcome (not a string)")

    def test_missing_outcome_is_unknown(self) -> None:
        result = self._turn([{"type": "user", "id": "msg_x"}, {"type": "idle"}])
        self.assertEqual(result.state, "unknown")
        self.assertEqual(result.reason, "malformed idle outcome (not a string)")

    def test_non_dict_messages_are_ignored(self) -> None:
        result = self._turn(
            [
                {"type": "user", "id": "msg_x"},
                "garbage",
                {"type": "idle", "outcome": "succeeded"},
            ]
        )
        self.assertEqual(result.state, "completed")


class TestIntegerBoundsNoOverflow(unittest.TestCase):
    """A huge int is a clean ValueError, never a raw OverflowError."""

    def test_huge_int_bounds_are_clean_value_errors(self) -> None:
        # One table per validator: a huge int is a ValueError, never OverflowError.
        huge = 10**1000
        constructor_cases = {
            "constructor_timeout": {"timeout": huge},
            "constructor_max_message_pages": {"max_message_pages": huge},
            "constructor_max_response_bytes": {"max_response_bytes": huge},
        }
        for label, kwargs in constructor_cases.items():
            with self.subTest(case=label):
                with self.assertRaises(ValueError):
                    OpenCodeV2Client("http://127.0.0.1:1", token="t", **kwargs)

        server = MockServer(token="t")
        server.__enter__()
        self.addCleanup(server.__exit__, None, None, None)
        client = OpenCodeV2Client(server.base_url, token="t")
        sid = client.create_session(agent="writer", model=WRITER_MODEL)["id"]
        wait_cases = {
            "wait_timeout_positive": lambda: client.wait(sid, timeout=huge),
            "wait_timeout_negative": lambda: client.wait(sid, timeout=-huge),
            "wait_retry_interval_positive": lambda: client.wait(sid, retry_interval=huge),
            "wait_retry_interval_negative": lambda: client.wait(sid, retry_interval=-huge),
        }
        for label, call in wait_cases.items():
            with self.subTest(case=label):
                with self.assertRaises(ValueError):
                    call()


class TestWaitMinimumInterval(unittest.TestCase):
    """A tiny poll interval would busy-loop; a meaningful floor is enforced."""

    def setUp(self) -> None:
        self.server = MockServer(token="t")
        self.server.__enter__()
        self.addCleanup(self.server.__exit__, None, None, None)
        self.client = OpenCodeV2Client(self.server.base_url, token="t")
        self.sid = self.client.create_session(agent="writer", model=WRITER_MODEL)["id"]

    def test_interval_below_floor_is_refused(self) -> None:
        for bad in (0.0000001, 0.009, 0.0, -0.01):
            with self.assertRaises(ValueError, msg=repr(bad)):
                self.client.wait(self.sid, retry_interval=bad)

    def test_interval_at_floor_is_accepted(self) -> None:
        self.server.state.wait_503_times = 1
        self.assertTrue(
            self.client.wait(self.sid, timeout=2.0, retry_interval=MIN_RETRY_INTERVAL)
        )

    def test_documented_floor_value(self) -> None:
        self.assertEqual(MIN_RETRY_INTERVAL, 0.01)


class TestTypedStaticErrors(unittest.TestCase):
    """Local-value reflection must not raise a raw TypeError."""

    def test_model_ref_mixed_type_keys_are_value_error(self) -> None:
        with self.assertRaises(ValueError):
            validate_model_ref({"id": "x", "providerID": "y", "a": 1, 2: 3})
        with self.assertRaises(ValueError):
            validate_model_ref({1: 2})
        with self.assertRaises(ValueError):
            validate_model_ref({"id": "x"})  # no providerID

    def test_model_ref_unknown_string_field_is_reported(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            validate_model_ref({"id": "x", "providerID": "y", "bogus": 1})
        self.assertIn("bogus", str(ctx.exception))

    def test_non_string_base_url_is_value_error(self) -> None:
        with self.assertRaises(ValueError):
            OpenCodeV2Client(123, token="t")  # type: ignore[arg-type]

    def test_repr_never_contains_any_credential(self) -> None:
        token_client = OpenCodeV2Client("http://127.0.0.1:1", token="tok-secret")
        self.assertNotIn("tok-secret", repr(token_client))
        basic_client = OpenCodeV2Client("http://127.0.0.1:1", auth="basic", password="pw-secret")
        self.assertNotIn("pw-secret", repr(basic_client))
        self.assertNotIn("pw-secret", repr(basic_client))
        self.assertIsNone(basic_client.token)

    def test_timeout_is_documented_as_a_per_operation_idle_timeout(self) -> None:
        self.assertIn("per-operation", OpenCodeV2Client._request.__doc__ or "")
        self.assertIn("wall-clock", OpenCodeV2Client.__doc__ or "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
