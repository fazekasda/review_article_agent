"""Acceptance tests for out-of-band owner confirmation on a controlling terminal.

Synthetic data only (no secrets, no licensed source text, no real scientific
approval, no credentials, no paid calls). These tests assert the **application
authority boundary** of ``review_article_agent.runtime.owner_auth``:

* authority is minted **only** by a successful controlling-terminal challenge;
* a capability is opaque, process-local, non-serializable, single-use, and bound
  to the canonical action digest, owner UID, and an expiration window;
* forged/absent tty, piped stdin, wrong challenge, expired, wrong-action, and
  replayed confirmations all **fail closed** with static typed errors;
* the rendered summary is terminal-control-injection-safe.

The confirmation path is exercised with **real controlling terminals** where
practical: a forkpty child (real ``/dev/tty``) and a detached child with no
controlling terminal. All other tests substitute only the low-level terminal
operations (the module's private seams), never a production bypass parameter.

Run with::

    PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_owner_auth.py'
"""

from __future__ import annotations

import contextlib
import copy
import errno
import io
import json
import os
import pickle
import re
import select
import subprocess
import sys
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from review_article_agent.runtime import owner_auth  # noqa: E402
from review_article_agent.runtime.owner_auth import (  # noqa: E402
    ActionDigestMismatch,
    ApprovalAlreadyConsumed,
    ApprovalExpired,
    InvalidCapabilityError,
    OwnerAction,
    OwnerActionError,
    OwnerApproval,
    OwnerAuthError,
    OwnerAuthEvidence,
    OwnerAuthTtyUnavailable,
    OwnerChallengeError,
    OwnerIdentityError,
    OwnerUidMismatch,
    action_digest,
    consume_owner_approval,
    request_owner_confirmation,
)

_FIXED_CHALLENGE = "TEST-CHAL-0001"
_FIXED_OWNER_UID = 1000
_SNAPSHOT = "a" * 64
_BASE_TIME = datetime(2026, 10, 27, 12, 0, 0, tzinfo=timezone.utc)


def _action(**overrides) -> OwnerAction:
    kwargs = dict(
        project_id="prj_syn",
        target={
            "project_id": "prj_syn",
            "artifact_id": "art_syn",
            "revision_id": "rev_syn",
        },
        snapshot_digest=_SNAPSHOT,
        gate_kind="pilot",
        gate_id="gate_pilot_1",
        decision="approved",
        rationale="synthetic rationale for the owner-auth tests",
    )
    kwargs.update(overrides)
    return OwnerAction(**kwargs)


class _FakeTty:
    """A low-level terminal double: only the terminal operations are mocked.

    ``replies`` are returned in order by :meth:`read_line`; a ``None`` element
    simulates EOF/timeout and an ``Exception`` element is raised.
    """

    def __init__(self, *, replies=(), is_tty=True, device_owner=None):
        self._replies = list(replies)
        self._is_tty = is_tty
        self._device_owner = device_owner
        self.written: list[str] = []
        self.closed = False

    def is_tty(self) -> bool:
        return self._is_tty

    def device_owner_uid(self) -> int:
        return self._device_owner

    def write(self, text: str) -> None:
        self.written.append(text)

    def read_line(self, *, max_chars: int, deadline: float):
        if not self._replies:
            return None
        item = self._replies.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def close(self) -> None:
        self.closed = True


class OwnerAuthTestBase(unittest.TestCase):
    def setUp(self) -> None:
        # A mutable clock the module's patched ``_utcnow`` reads, so a test can
        # advance time (an expiration test) without sleeping.
        self.clock = [_BASE_TIME]

    def install(
        self,
        *,
        tty=None,
        challenge=_FIXED_CHALLENGE,
        owner_uid=_FIXED_OWNER_UID,
        real_uid=_FIXED_OWNER_UID,
        patch_open=True,
    ):
        """Patch only the module's private low-level seams."""
        patches = [
            mock.patch.object(owner_auth, "_new_challenge", lambda: challenge),
            mock.patch.object(owner_auth, "_current_owner_uid", lambda: owner_uid),
            mock.patch.object(owner_auth, "_real_uid", lambda: real_uid),
            mock.patch.object(owner_auth, "_utcnow", lambda: self.clock[0]),
        ]
        if patch_open:
            patches.append(
                mock.patch.object(
                    owner_auth, "_open_controlling_tty", lambda: tty
                )
            )
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        return tty

    def mint(self, action=None, tty=None, **conf_kwargs) -> OwnerApproval:
        action = action or _action()
        return request_owner_confirmation(action, **conf_kwargs)


# --------------------------------------------------------------------------- #
# Minting and capability opacity
# --------------------------------------------------------------------------- #
class TestMinting(OwnerAuthTestBase):
    def test_successful_confirmation_mints_opaque_capability(self):
        tty = _FakeTty(
            replies=[_FIXED_CHALLENGE], device_owner=_FIXED_OWNER_UID
        )
        self.install(tty=tty)
        action = _action()
        cap = request_owner_confirmation(action)

        self.assertIsInstance(cap, OwnerApproval)
        # Opaque: no __dict__, no public data, no meaningful repr.
        self.assertFalse(hasattr(cap, "__dict__"))
        with self.assertRaises(TypeError):
            vars(cap)
        self.assertIn("opaque", repr(cap))
        self.assertTrue(tty.closed)

        prompt = tty.written[0]
        self.assertIn(action_digest(action), prompt)
        self.assertIn("OWNER APPROVAL REQUIRED", prompt)
        self.assertIn("Challenge: " + _FIXED_CHALLENGE, prompt)

    def test_capability_is_not_json_serializable(self):
        self.install(tty=_FakeTty(replies=[_FIXED_CHALLENGE], device_owner=_FIXED_OWNER_UID))
        cap = request_owner_confirmation(_action())
        with self.assertRaises(TypeError):
            json.dumps(cap)

    def test_capability_is_not_pickleable_or_copyable(self):
        self.install(tty=_FakeTty(replies=[_FIXED_CHALLENGE], device_owner=_FIXED_OWNER_UID))
        cap = request_owner_confirmation(_action())
        with self.assertRaises((TypeError, pickle.PicklingError)):
            pickle.dumps(cap)
        with self.assertRaises(TypeError):
            copy.copy(cap)
        with self.assertRaises(TypeError):
            copy.deepcopy(cap)

    def test_direct_construction_cannot_mint_authority(self):
        now = _BASE_TIME
        with self.assertRaises(OwnerAuthError):
            OwnerApproval(
                object(),
                action_digest="a" * 64,
                owner_uid=_FIXED_OWNER_UID,
                created_at=now,
                expires_at=now + timedelta(seconds=60),
            )
        # Missing the private sentinel entirely is a TypeError.
        with self.assertRaises(TypeError):
            OwnerApproval(
                action_digest="a" * 64,
                owner_uid=_FIXED_OWNER_UID,
                created_at=now,
                expires_at=now + timedelta(seconds=60),
            )

    def test_wrong_challenge_fails_closed_and_mints_nothing(self):
        tty = _FakeTty(
            replies=["WRONG-0000", "WRONG-0000", "WRONG-0000"],
            device_owner=_FIXED_OWNER_UID,
        )
        self.install(tty=tty)
        with self.assertRaises(OwnerChallengeError):
            request_owner_confirmation(_action())
        self.assertTrue(tty.closed)

    def test_over_length_reply_fails_closed(self):
        too_long = "X" * (owner_auth.MAX_CHALLENGE_LINE_CHARS + 5)
        tty = _FakeTty(replies=[too_long], device_owner=_FIXED_OWNER_UID)
        self.install(tty=tty)
        with self.assertRaises(OwnerChallengeError):
            request_owner_confirmation(_action())

    def test_exhausted_deadline_fails_closed(self):
        tty = _FakeTty(replies=[None], device_owner=_FIXED_OWNER_UID)
        self.install(tty=tty)
        with self.assertRaises(OwnerChallengeError):
            request_owner_confirmation(_action())

    def test_absent_controlling_tty_fails_closed(self):
        # No tty seam substituted; the real opener hits a failing os.open.
        self.install(patch_open=False)
        with mock.patch.object(
            owner_auth.os, "open", side_effect=OSError(errno.ENXIO, "no tty")
        ):
            with self.assertRaises(OwnerAuthTtyUnavailable):
                request_owner_confirmation(_action())

    def test_non_tty_fails_closed(self):
        tty = _FakeTty(replies=[_FIXED_CHALLENGE], is_tty=False)
        self.install(tty=tty)
        with self.assertRaises(OwnerAuthTtyUnavailable):
            request_owner_confirmation(_action())
        self.assertTrue(tty.closed)

    def test_elevated_identity_mismatch_fails_closed(self):
        # euid != ruid => an elevated/setuid identity; refuse before any I/O.
        self.install(
            tty=_FakeTty(replies=[_FIXED_CHALLENGE], device_owner=_FIXED_OWNER_UID),
            owner_uid=0,
            real_uid=_FIXED_OWNER_UID,
        )
        with self.assertRaises(OwnerIdentityError):
            request_owner_confirmation(_action())

    def test_foreign_terminal_owner_fails_closed(self):
        tty = _FakeTty(replies=[_FIXED_CHALLENGE], device_owner=999)
        self.install(tty=tty)
        with self.assertRaises(OwnerIdentityError):
            request_owner_confirmation(_action())

    def test_never_reads_standard_input(self):
        # stdin holds a *correct-looking* value; the boundary must ignore it.
        fake_stdin = io.StringIO(_FIXED_CHALLENGE + "\n")
        tty = _FakeTty(replies=[_FIXED_CHALLENGE], device_owner=_FIXED_OWNER_UID)
        self.install(tty=tty)
        with mock.patch.object(sys, "stdin", fake_stdin):
            cap = request_owner_confirmation(_action())
        self.assertIsInstance(cap, OwnerApproval)
        self.assertEqual(fake_stdin.tell(), 0)  # stdin was never read

    def test_render_is_safe_against_terminal_control_injection(self):
        inject = "\x1b[2K\rOWNER APPROVAL GRANTED\nChallenge: HACK-0000\x07"
        action = _action(rationale=inject)
        tty = _FakeTty(replies=[_FIXED_CHALLENGE], device_owner=_FIXED_OWNER_UID)
        self.install(tty=tty)
        request_owner_confirmation(action)
        prompt = tty.written[0]

        self.assertNotIn("\x1b", prompt)
        self.assertNotIn("\x07", prompt)
        self.assertIn("\\x1b", prompt)
        # The injected newline is escaped, so the ONLY challenge line is ours.
        challenge_lines = [
            line for line in prompt.split("\n") if line.startswith("Challenge: ")
        ]
        self.assertEqual(challenge_lines, ["Challenge: " + _FIXED_CHALLENGE])
        # The injected text cannot start a line, so it cannot spoof a prompt.
        self.assertFalse(
            any(line.startswith("OWNER APPROVAL GRANTED") for line in prompt.split("\n"))
        )

    def test_digest_binds_all_action_fields(self):
        self.install(tty=_FakeTty(replies=[_FIXED_CHALLENGE], device_owner=_FIXED_OWNER_UID))
        digests = set()
        for action in (
            _action(),
            _action(project_id="prj_other", target={"project_id": "prj_other", "artifact_id": "art_syn", "revision_id": "rev_syn"}),
            _action(gate_kind="outline"),
            _action(decision="rejected"),
            _action(rationale="different"),
            _action(snapshot_digest="b" * 64),
        ):
            digests.add(action_digest(action))
        self.assertEqual(len(digests), 6)


# --------------------------------------------------------------------------- #
# Action typing / validation
# --------------------------------------------------------------------------- #
class TestActionValidation(unittest.TestCase):
    def test_unknown_gate_kind_rejected(self):
        with self.assertRaises(OwnerActionError):
            _action(gate_kind="tool_permission")

    def test_bad_decision_rejected(self):
        with self.assertRaises(OwnerActionError):
            _action(decision="maybe")

    def test_cross_project_target_rejected(self):
        with self.assertRaises(OwnerActionError):
            _action(
                project_id="prj_a",
                target={"project_id": "prj_b", "artifact_id": "art", "revision_id": "rev"},
            )

    def test_empty_rationale_rejected(self):
        with self.assertRaises(OwnerActionError):
            _action(rationale="")

    def test_bad_snapshot_digest_rejected(self):
        with self.assertRaises(OwnerActionError):
            _action(snapshot_digest="not-a-digest")

    def test_snapshot_digest_accepts_sha256_prefix(self):
        action = _action(snapshot_digest="sha256:" + "c" * 64)
        self.assertEqual(action.snapshot_digest, "c" * 64)

    def test_data_cannot_describe_an_action(self):
        raw = {
            "project_id": "prj_syn",
            "target": {"project_id": "prj_syn", "artifact_id": "a", "revision_id": "r"},
            "snapshot_digest": _SNAPSHOT,
            "gate_kind": "pilot",
            "gate_id": "g",
            "decision": "approved",
            "rationale": "x",
        }
        with self.assertRaises(OwnerActionError):
            action_digest(raw)
        with self.assertRaises(OwnerActionError):
            request_owner_confirmation(raw)


# --------------------------------------------------------------------------- #
# Consumption: binding, expiry, replay, forgery
# --------------------------------------------------------------------------- #
class TestConsumption(OwnerAuthTestBase):
    def _minted(self, action=None):
        action = action or _action()
        self.install(tty=_FakeTty(replies=[_FIXED_CHALLENGE], device_owner=_FIXED_OWNER_UID))
        cap = request_owner_confirmation(action)
        return action, cap

    def test_happy_path_returns_safe_evidence(self):
        action, cap = self._minted()
        evidence = consume_owner_approval(cap, action)
        self.assertIsInstance(evidence, OwnerAuthEvidence)
        self.assertEqual(evidence.method, owner_auth.OWNER_APPROVAL_METHOD)
        self.assertEqual(evidence.owner_uid, _FIXED_OWNER_UID)
        self.assertEqual(evidence.action_digest, action_digest(action))
        self.assertEqual(evidence.verified_at, _BASE_TIME)
        self.assertTrue(evidence.auth_id.startswith("auth_"))
        record = evidence.as_record()
        # The persisted evidence carries no challenge/secret value.
        self.assertNotIn(_FIXED_CHALLENGE, json.dumps(record))
        self.assertEqual(
            set(record),
            {"auth_id", "method", "owner_uid", "verified_at", "action_digest"},
        )

    def test_replay_fails_closed(self):
        action, cap = self._minted()
        consume_owner_approval(cap, action)
        with self.assertRaises(ApprovalAlreadyConsumed):
            consume_owner_approval(cap, action)

    def test_wrong_action_fails_closed(self):
        action, cap = self._minted()
        with self.assertRaises(ActionDigestMismatch):
            consume_owner_approval(cap, _action(rationale="tampered"))

    def test_mutable_payload_fails_closed(self):
        action, cap = self._minted()
        action.target["artifact_id"] = "art_swapped"
        with self.assertRaises(ActionDigestMismatch):
            consume_owner_approval(cap, action)

    def test_expired_capability_fails_closed(self):
        action, cap = self._minted()
        self.clock[0] = _BASE_TIME + timedelta(
            seconds=owner_auth.OWNER_APPROVAL_TTL_SECONDS + 1
        )
        with self.assertRaises(ApprovalExpired):
            consume_owner_approval(cap, action)

    def test_uid_mismatch_at_consumption_fails_closed(self):
        action, cap = self._minted()
        with mock.patch.object(owner_auth, "_current_owner_uid", lambda: _FIXED_OWNER_UID + 1):
            with self.assertRaises(OwnerUidMismatch):
                consume_owner_approval(cap, action)

    def test_forged_values_fail_closed(self):
        action, _ = self._minted()
        for forged in (True, False, None, "human", "owner_authenticated", {}, [], 1):
            with self.subTest(forged=forged):
                with self.assertRaises(InvalidCapabilityError):
                    consume_owner_approval(forged, action)

    def test_error_messages_do_not_leak_the_challenge(self):
        self.install(
            tty=_FakeTty(replies=["WRONG-0000"], device_owner=_FIXED_OWNER_UID)
        )
        with self.assertRaises(OwnerChallengeError) as ctx:
            request_owner_confirmation(_action())
        self.assertNotIn(_FIXED_CHALLENGE, str(ctx.exception))
        self.assertNotIn("WRONG-0000", str(ctx.exception))


# --------------------------------------------------------------------------- #
# Low-level terminal reader: real file-descriptor behavior
# --------------------------------------------------------------------------- #
class TestRealTtyReader(unittest.TestCase):
    def test_reads_a_line_and_normalizes_crlf(self):
        read_fd, write_fd = os.pipe()
        self.addCleanup(os.close, read_fd)
        self.addCleanup(os.close, write_fd)
        tty = owner_auth._RealTty(read_fd)
        os.write(write_fd, b"AB\r\n")
        self.assertEqual(
            tty.read_line(max_chars=64, deadline=time.monotonic() + 2), "AB"
        )

    def test_bounds_over_length_input(self):
        read_fd, write_fd = os.pipe()
        self.addCleanup(os.close, read_fd)
        self.addCleanup(os.close, write_fd)
        tty = owner_auth._RealTty(read_fd)
        os.write(write_fd, b"x" * 100)  # no newline
        self.assertIsNone(
            tty.read_line(max_chars=64, deadline=time.monotonic() + 2)
        )

    def test_expired_deadline_returns_none(self):
        read_fd, write_fd = os.pipe()
        self.addCleanup(os.close, read_fd)
        self.addCleanup(os.close, write_fd)
        tty = owner_auth._RealTty(read_fd)
        self.assertIsNone(
            tty.read_line(max_chars=64, deadline=time.monotonic() - 1)
        )


# --------------------------------------------------------------------------- #
# Real controlling terminal (acceptance): a forkpty child on a real /dev/tty
# --------------------------------------------------------------------------- #
_CHILD_SCRIPT = """
import json, sys
sys.path.insert(0, __SRC__)
from review_article_agent.runtime import owner_auth as oa
action = oa.OwnerAction(
    project_id="prj_syn",
    target={"project_id": "prj_syn", "artifact_id": "art_syn", "revision_id": "rev_syn"},
    snapshot_digest=__DIGEST__,
    gate_kind="pilot",
    gate_id="gate_pilot_1",
    decision="approved",
    rationale="synthetic pty acceptance confirmation",
)
try:
    cap = oa.request_owner_confirmation(action, timeout_seconds=25.0)
    ev = oa.consume_owner_approval(cap, action)
    sys.stdout.write("RESULT " + json.dumps(ev.as_record(), sort_keys=True) + "\\n")
except Exception as exc:
    sys.stdout.write("ERROR " + type(exc).__name__ + "\\n")
sys.stdout.flush()
"""


def _child_script(digest: str) -> str:
    return _CHILD_SCRIPT.replace("__SRC__", repr(str(SRC))).replace(
        "__DIGEST__", repr(digest)
    )


def _drive_pty(master_fd: int, *, mode: str, deadline: float) -> str:
    """Read the child's prompt, answer the challenge, and capture its result."""
    buffer = ""
    answered = False
    while time.monotonic() < deadline:
        try:
            ready, _, _ = select.select([master_fd], [], [], 0.5)
        except (OSError, ValueError):
            break
        if not ready:
            continue
        try:
            chunk = os.read(master_fd, 4096)
        except OSError:
            break
        if not chunk:
            break
        buffer += chunk.decode("utf-8", "replace")
        if not answered:
            match = re.search(r"Challenge: ([A-Z0-9-]+)", buffer)
            if match:
                answered = True
                if mode == "correct":
                    os.write(master_fd, (match.group(1) + "\n").encode())
                else:
                    os.write(master_fd, b"WRONG-CHAL-0000\n" * 3)
        if "RESULT " in buffer or "ERROR " in buffer:
            break
    return buffer


class TestRealControllingTerminal(unittest.TestCase):
    @unittest.skipUnless(hasattr(os, "forkpty"), "requires os.forkpty")
    def test_real_pty_confirm_and_consume(self):
        digest = "d" * 64
        expected = action_digest(
            OwnerAction(
                project_id="prj_syn",
                target={
                    "project_id": "prj_syn",
                    "artifact_id": "art_syn",
                    "revision_id": "rev_syn",
                },
                snapshot_digest=digest,
                gate_kind="pilot",
                gate_id="gate_pilot_1",
                decision="approved",
                rationale="synthetic pty acceptance confirmation",
            )
        )
        pid, master_fd = os.forkpty()
        if pid == 0:  # pragma: no cover - child process
            with contextlib.suppress(Exception):
                os.execv(
                    sys.executable,
                    [sys.executable, "-c", _child_script(digest)],
                )
            os._exit(127)
        try:
            output = _drive_pty(
                master_fd, mode="correct", deadline=time.monotonic() + 30
            )
        finally:
            with contextlib.suppress(OSError):
                os.close(master_fd)
            with contextlib.suppress(ChildProcessError, OSError):
                os.waitpid(pid, 0)

        self.assertIn("RESULT ", output, output)
        record = json.loads(output.split("RESULT ", 1)[1].splitlines()[0])
        self.assertEqual(record["action_digest"], expected)
        self.assertEqual(record["owner_uid"], os.geteuid())
        self.assertEqual(record["method"], owner_auth.OWNER_APPROVAL_METHOD)
        self.assertTrue(record["auth_id"].startswith("auth_"))

    @unittest.skipUnless(hasattr(os, "forkpty"), "requires os.forkpty")
    def test_real_pty_wrong_challenge_fails_closed(self):
        pid, master_fd = os.forkpty()
        if pid == 0:  # pragma: no cover - child process
            with contextlib.suppress(Exception):
                os.execv(
                    sys.executable,
                    [sys.executable, "-c", _child_script("e" * 64)],
                )
            os._exit(127)
        try:
            output = _drive_pty(
                master_fd, mode="wrong", deadline=time.monotonic() + 30
            )
        finally:
            with contextlib.suppress(OSError):
                os.close(master_fd)
            with contextlib.suppress(ChildProcessError, OSError):
                os.waitpid(pid, 0)

        self.assertIn("ERROR OwnerChallengeError", output, output)
        self.assertNotIn("RESULT ", output)

    def test_detached_process_without_controlling_tty_fails_closed(self):
        # start_new_session detaches from the controlling terminal (as a piped,
        # no-tty process would be): /dev/tty cannot be opened, so it fails
        # closed instead of reading standard input.
        proc = subprocess.run(
            [sys.executable, "-c", _child_script("f" * 64)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
            timeout=30,
        )
        self.assertIn("ERROR OwnerAuthTtyUnavailable", proc.stdout, proc.stdout + proc.stderr)
        self.assertNotIn("RESULT ", proc.stdout)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
