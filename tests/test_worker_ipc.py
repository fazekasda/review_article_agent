"""Acceptance tests for the owner-authenticated worker control transport (T2).

Covers ``docs/implementation/03-durable-worker.md`` deliverable 1 / acceptance
criterion "Authentication rejects non-owner control; no root or public
unauthenticated listener is required", ``ARCH-IPC`` (owner-only Unix-domain
socket on Linux), ``docs/contracts/02-gates-and-runtime.md`` §1.3 (authority is
out-of-band, never from a payload) / §8.4 (non-owner control rejected) / §10
(no public unauthenticated services, no secret leakage), and the accepted
``docs/contracts/03-policy-decisions.md`` ``ARCH-IPC`` decision.

A genuine cross-UID peer denial needs a second OS user and is not available in
this environment; that path is exercised with a substituted
``read_peer_credentials`` (see ``test_mocked_peer_uid_denial_*``) and disclosed
in the task report.
"""

from __future__ import annotations

import json
import os
import socket
import stat
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from review_article_agent.runtime import ipc  # noqa: E402


def _decode(raw: bytes) -> dict:
    assert raw, "expected a response, got nothing"
    return json.loads(raw.split(b"\n", 1)[0].decode("utf-8"))


class _Handler:
    """Records verified peer identities and raises on a sentinel key."""

    def __init__(self) -> None:
        self.calls: list[tuple[dict, ipc.PeerCredentials]] = []

    def __call__(self, request: dict, peer: ipc.PeerCredentials) -> dict:
        self.calls.append((request, peer))
        if "__boom__" in request:
            raise RuntimeError("internal failure: " + str(request["__boom__"]))
        return {
            "echo": request.get("x"),
            "peer_uid": peer.uid,
            "peer_pid": peer.pid,
        }


class WorkerIPCTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="raa-ipc-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.data_dir = str(self.tmp / "state")
        self.handler = _Handler()
        self._servers: list[ipc.WorkerIPCServer] = []
        self._stops: list[threading.Event] = []
        self._threads: list[threading.Thread] = []

    # -- helpers ------------------------------------------------------------- #
    def _serve(self, *, io_timeout: float = 1.0, **kw) -> ipc.WorkerIPCServer:
        server = ipc.WorkerIPCServer(
            self.data_dir, self.handler, io_timeout=io_timeout, **kw
        )
        server.start()
        stop = threading.Event()
        thread = threading.Thread(
            target=server.serve_forever,
            kwargs={"stop": stop, "poll_timeout": 0.05},
            daemon=True,
        )
        thread.start()
        self._servers.append(server)
        self._stops.append(stop)
        self._threads.append(thread)
        return server

    def tearDown(self) -> None:
        for stop in self._stops:
            stop.set()
        for thread in self._threads:
            thread.join(2)
        for server in self._servers:
            server.close()

    def _raw_request(self, server: ipc.WorkerIPCServer, payload: bytes,
                     timeout: float = 2.0) -> bytes:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        try:
            sock.connect(server.socket_path)
            sock.sendall(payload)
            buf = bytearray()
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                buf.extend(chunk)
                if b"\n" in buf:
                    break
            return bytes(buf)
        finally:
            sock.close()

    # -- happy path + modes -------------------------------------------------- #
    def test_valid_request_round_trip_uses_verified_peer(self) -> None:
        server = self._serve()
        client = ipc.WorkerIPCClient(self.data_dir, io_timeout=2.0)
        result = client.request({"x": 7, "uid": 999999, "pid": 1})
        self.assertEqual(result["echo"], 7)
        # Identity comes from SO_PEERCRED, never from the spoofed request fields.
        self.assertEqual(result["peer_uid"], os.getuid())
        self.assertEqual(self.handler.calls[0][1].uid, os.getuid())

    def test_private_modes(self) -> None:
        server = self._serve()
        self.assertEqual(stat.S_IMODE(os.stat(server.runtime_dir).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.stat(server.socket_path).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(server.lock_path).st_mode), 0o600)

    def test_one_server_per_data_dir(self) -> None:
        server = self._serve()
        second = ipc.WorkerIPCServer(self.data_dir, self.handler, io_timeout=1.0)
        with self.assertRaises(ipc.WorkerAlreadyRunning):
            second.start()
        # The lock is released on close, allowing a fresh owner.
        server.close()
        replacement = ipc.WorkerIPCServer(self.data_dir, self.handler, io_timeout=1.0)
        replacement.start()
        replacement.close()

    # -- malformed / oversized / duplicate / deep ---------------------------- #
    def test_malformed_requests_rejected(self) -> None:
        server = self._serve()
        for payload in (
            b"not json\n",
            b"[1,2,3]\n",
            b'{"a":}\n',
            b'{"a":1,}\n',
            b'"just a string"\n',
            b"123\n",
            b"null\n",
            b'{\xff}\n',
        ):
            raw = self._raw_request(server, payload)
            body = _decode(raw)
            self.assertFalse(body["ok"])
            self.assertEqual(body["error"]["code"], "malformed_request")

    def test_oversized_request_rejected_without_reflection(self) -> None:
        server = self._serve()
        canary = b"CANARY-SECRET-OVERSIZE"
        payload = b'{"p":"' + canary + b"A" * (ipc.MAX_REQUEST_BYTES + 64) + b'"}\n'
        raw = self._raw_request(server, payload)
        body = _decode(raw)
        self.assertFalse(body["ok"])
        self.assertEqual(body["error"]["code"], "request_too_large")
        self.assertNotIn(canary, raw)

    def test_duplicate_key_rejected(self) -> None:
        server = self._serve()
        body = _decode(self._raw_request(server, b'{"a":1,"a":2}\n'))
        self.assertFalse(body["ok"])
        self.assertEqual(body["error"]["code"], "malformed_request")

    def test_nonfinite_rejected(self) -> None:
        server = self._serve()
        for payload in (b'{"x": NaN}\n', b'{"x": Infinity}\n', b'{"x": -Infinity}\n'):
            body = _decode(self._raw_request(server, payload))
            self.assertFalse(body["ok"])
            self.assertEqual(body["error"]["code"], "malformed_request")

    def test_deep_nesting_rejected(self) -> None:
        server = self._serve()
        payload = b'{"a":' + b"[" * 32 + b"]" * 32 + b"}\n"
        body = _decode(self._raw_request(server, payload))
        self.assertFalse(body["ok"])
        self.assertEqual(body["error"]["code"], "malformed_request")

    def test_client_refuses_nonfinite_local_request(self) -> None:
        self._serve()
        client = ipc.WorkerIPCClient(self.data_dir, io_timeout=2.0)
        with self.assertRaises(ipc.RequestError):
            client.request({"x": float("nan")})

    # -- authentication ------------------------------------------------------ #
    def test_spoofed_owner_fields_do_not_authenticate(self) -> None:
        server = self._serve()
        foreign = ipc.PeerCredentials(pid=os.getpid() + 1, uid=os.getuid() + 4242, gid=0)
        spoof = (
            b'{"actor_kind":"human","authority":"owner_authenticated",'
            b'"owner_auth_ref":"self-minted","uid":0,"gid":0,"pid":1}\n'
        )
        with mock.patch.object(ipc, "read_peer_credentials", return_value=foreign):
            raw = self._raw_request(server, spoof)
        body = _decode(raw)
        self.assertFalse(body["ok"])
        self.assertEqual(body["error"]["code"], "unauthorized_peer")
        # The handler (dispatch) never ran for the foreign peer.
        self.assertEqual(self.handler.calls, [])

    def test_mocked_peer_uid_denial_blocks_dispatch(self) -> None:
        server = self._serve()
        foreign = ipc.PeerCredentials(pid=1, uid=os.getuid() + 1, gid=os.getgid())
        with mock.patch.object(ipc, "read_peer_credentials", return_value=foreign):
            body = _decode(self._raw_request(server, b'{"x": 1}\n'))
        self.assertFalse(body["ok"])
        self.assertEqual(body["error"]["code"], "unauthorized_peer")
        self.assertEqual(self.handler.calls, [])

    def test_client_verifies_server_uid(self) -> None:
        runtime = ipc.runtime_dir_for(self.data_dir)
        os.makedirs(runtime, mode=0o700, exist_ok=True)
        fake = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(fake.close)
        fake.bind(ipc.WorkerIPCServer(self.data_dir, self.handler).socket_path)
        fake.listen(1)
        foreign = ipc.PeerCredentials(pid=999, uid=os.getuid() + 7, gid=os.getgid())
        client = ipc.WorkerIPCClient(self.data_dir, io_timeout=1.0)
        with mock.patch.object(ipc, "read_peer_credentials", return_value=foreign):
            with self.assertRaises(ipc.PeerNotAuthorized):
                client.request({"x": 1})

    def test_callback_receives_verified_peer_not_json(self) -> None:
        self._serve()
        client = ipc.WorkerIPCClient(self.data_dir, io_timeout=2.0)
        client.request({"peer_uid": 0, "peer_pid": -1})
        self.assertEqual(self.handler.calls[0][1].uid, os.getuid())
        self.assertGreater(self.handler.calls[0][1].pid, 0)

    # -- unsafe paths -------------------------------------------------------- #
    def test_symlinked_socket_refused_without_deleting_target(self) -> None:
        runtime = ipc.runtime_dir_for(self.data_dir)
        os.makedirs(runtime, mode=0o700)
        target = self.tmp / "foreign-target"
        target.write_text("do not delete")
        os.symlink(str(target), os.path.join(runtime, ipc.DEFAULT_SOCKET_FILENAME))
        server = ipc.WorkerIPCServer(self.data_dir, self.handler, io_timeout=1.0)
        with self.assertRaises(ipc.RuntimeDirectoryError):
            server.start()
        self.assertTrue(os.path.islink(os.path.join(runtime, ipc.DEFAULT_SOCKET_FILENAME)))
        self.assertEqual(target.read_text(), "do not delete")

    def test_foreign_regular_file_at_socket_path_refused(self) -> None:
        runtime = ipc.runtime_dir_for(self.data_dir)
        os.makedirs(runtime, mode=0o700)
        victim = os.path.join(runtime, ipc.DEFAULT_SOCKET_FILENAME)
        Path(victim).write_text("foreign file")
        server = ipc.WorkerIPCServer(self.data_dir, self.handler, io_timeout=1.0)
        with self.assertRaises(ipc.RuntimeDirectoryError):
            server.start()
        self.assertEqual(Path(victim).read_text(), "foreign file")

    def test_symlinked_data_dir_refused(self) -> None:
        real = self.tmp / "real-state"
        real.mkdir()
        link = self.tmp / "link-state"
        os.symlink(str(real), str(link))
        server = ipc.WorkerIPCServer(str(link), self.handler, io_timeout=1.0)
        with self.assertRaises(ipc.RuntimeDirectoryError):
            server.start()

    def test_foreign_owner_runtime_dir_refused(self) -> None:
        # Simulate a runtime directory owned by someone else without root by
        # telling the server to expect a different owner UID.
        runtime = ipc.runtime_dir_for(self.data_dir)
        os.makedirs(runtime, mode=0o700)
        server = ipc.WorkerIPCServer(
            self.data_dir, self.handler, io_timeout=1.0, owner_uid=os.getuid() + 1234
        )
        with self.assertRaises(ipc.RuntimeDirectoryError):
            server.start()

    # -- stale recovery ------------------------------------------------------ #
    def test_stale_owned_socket_recovered_without_touching_others(self) -> None:
        runtime = ipc.runtime_dir_for(self.data_dir)
        os.makedirs(runtime, mode=0o700)
        other_sock_path = os.path.join(runtime, "unrelated.sock")
        other = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        other.bind(other_sock_path)
        other.listen(1)
        self.addCleanup(other.close)
        bystander = os.path.join(runtime, "bystander.txt")
        Path(bystander).write_text("untouched")

        stale_path = os.path.join(runtime, ipc.DEFAULT_SOCKET_FILENAME)
        stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        stale.bind(stale_path)
        stale.close()
        stale_inode = os.stat(stale_path).st_ino

        server = self._serve()
        self.assertNotEqual(os.stat(server.socket_path).st_ino, stale_inode)
        # Bystanders untouched.
        self.assertTrue(os.path.exists(other_sock_path))
        self.assertEqual(Path(bystander).read_text(), "untouched")
        # Recovered socket actually works.
        client = ipc.WorkerIPCClient(self.data_dir, io_timeout=2.0)
        self.assertEqual(client.request({"x": 1})["echo"], 1)

    def test_close_does_not_unlink_replaced_socket(self) -> None:
        server = self._serve()
        ours = os.stat(server.socket_path).st_ino
        os.unlink(server.socket_path)
        replacement = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        replacement.bind(server.socket_path)
        replacement.listen(1)
        self.addCleanup(replacement.close)
        replacement_inode = os.stat(server.socket_path).st_ino
        self.assertNotEqual(ours, replacement_inode)
        server.close()
        # The replacement instance's socket must still exist.
        self.assertTrue(os.path.exists(server.socket_path))
        self.assertEqual(os.stat(server.socket_path).st_ino, replacement_inode)

    # -- bounded I/O and error hygiene --------------------------------------- #
    def test_partial_read_is_bounded(self) -> None:
        server = ipc.WorkerIPCServer(self.data_dir, self.handler, io_timeout=0.3)
        server.start()
        self.addCleanup(server.close)
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(3.0)
        sock.connect(server.socket_path)
        sock.sendall(b'{"partial":')  # no newline, connection held open
        started = time.monotonic()
        handled = server.serve_once(timeout=1.0)
        elapsed = time.monotonic() - started
        sock.close()
        self.assertTrue(handled)
        self.assertLess(elapsed, 2.0)  # bounded by io_timeout, not hung
        self.assertEqual(self.handler.calls, [])

    def test_slow_drip_sender_is_bounded(self) -> None:
        server = ipc.WorkerIPCServer(self.data_dir, self.handler, io_timeout=0.4)
        server.start()
        self.addCleanup(server.close)
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(3.0)
        sock.connect(server.socket_path)
        feeding = threading.Event()

        def drip() -> None:
            while not feeding.is_set():
                try:
                    sock.sendall(b"{")
                except OSError:
                    return
                time.sleep(0.05)

        feeder = threading.Thread(target=drip, daemon=True)
        feeder.start()
        started = time.monotonic()
        server.serve_once(timeout=3.0)
        elapsed = time.monotonic() - started
        feeding.set()
        feeder.join(1)
        sock.close()
        # Total read time is bounded by io_timeout, not by the drip rate.
        self.assertLess(elapsed, 1.5)

    def test_silent_connection_is_bounded(self) -> None:
        server = ipc.WorkerIPCServer(self.data_dir, self.handler, io_timeout=0.3)
        server.start()
        self.addCleanup(server.close)
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(3.0)
        sock.connect(server.socket_path)
        started = time.monotonic()
        server.serve_once(timeout=1.0)
        elapsed = time.monotonic() - started
        sock.close()
        self.assertLess(elapsed, 2.0)

    def test_error_response_does_not_reflect_secret(self) -> None:
        server = self._serve()
        secret = "s3cr3t-canary-9271"
        raw = self._raw_request(server, json.dumps({"__boom__": secret}).encode() + b"\n")
        body = _decode(raw)
        self.assertFalse(body["ok"])
        self.assertEqual(body["error"]["code"], "handler_error")
        self.assertNotIn(secret, raw.decode("utf-8", "replace"))

    def test_malformed_body_secret_not_reflected(self) -> None:
        server = self._serve()
        secret = b"CANARY-MALFORMED-4410"
        raw = self._raw_request(server, b'{"a":' + secret + b',"a":1}\n')
        self.assertNotIn(secret, raw)

    def test_server_survives_malformed_then_serves_valid(self) -> None:
        server = self._serve()
        bad = self._raw_request(server, b'{"a": NaN}\n')
        self.assertFalse(_decode(bad)["ok"])
        client = ipc.WorkerIPCClient(self.data_dir, io_timeout=2.0)
        self.assertEqual(client.request({"x": 5})["echo"], 5)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
