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
    Admission,
    AdmissionConflictError,
    OpenCodeApiError,
    OpenCodeProtocolError,
    OpenCodeResponseTooLargeError,
    OpenCodeTransportError,
    OpenCodeV2Client,
    QuiescenceTimeout,
    ReconcileResult,
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

    def test_non_loopback_or_bad_scheme_or_embedded_credentials_rejected(self) -> None:
        for url in (
            "http://example.com:443",
            "https://10.1.2.3:1234",
            "http://0.0.0.0:1",
            "ftp://127.0.0.1:1",
            "http://user:pass@127.0.0.1:1",
            "not a url",
        ):
            with self.assertRaises(ValueError, msg=url):
                OpenCodeV2Client(url, token="t")

    def test_missing_or_empty_token_fails_closed(self) -> None:
        with self.assertRaises(ValueError):
            OpenCodeV2Client("http://127.0.0.1:1")
        with self.assertRaises(ValueError):
            OpenCodeV2Client("http://127.0.0.1:1", token="   ")

    def test_bad_numeric_and_page_bounds_rejected(self) -> None:
        for kwargs in (
            {"timeout": 0},
            {"timeout": -1.0},
            {"max_message_pages": 0},
            {"max_response_bytes": 0},
        ):
            with self.assertRaises(ValueError, msg=repr(kwargs)):
                OpenCodeV2Client("http://127.0.0.1:1", token="t", **kwargs)

    def test_unknown_auth_scheme_and_mutually_exclusive_fields(self) -> None:
        with self.assertRaises(ValueError):
            OpenCodeV2Client("http://127.0.0.1:1", auth="digest", password="x")
        with self.assertRaises(ValueError):
            OpenCodeV2Client("http://127.0.0.1:1", token="t", auth="basic", password="p")
        with self.assertRaises(ValueError):
            OpenCodeV2Client("http://127.0.0.1:1", token="t", password="p")

    def test_basic_requires_explicit_password(self) -> None:
        with self.assertRaises(ValueError):
            OpenCodeV2Client("http://127.0.0.1:1", auth="basic")
        with self.assertRaises(ValueError):
            OpenCodeV2Client("http://127.0.0.1:1", auth="basic", username="opencode")

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
