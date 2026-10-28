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

    def __init__(self, *, replies=(), is_tty=True):
        self._replies = list(replies)
        self._is_tty = is_tty
        self.written: list[str] = []
        self.closed = False

    def is_tty(self) -> bool:
        return self._is_tty

    def write(self, text: str, *, deadline=None) -> None:
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
        # Mutable clocks the module's patched seams read, so a test can advance
        # wall or monotonic time without sleeping.
        self.clock = [_BASE_TIME]
        self.mono = [1000.0]

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
            mock.patch.object(owner_auth, "_monotonic", lambda: self.mono[0]),
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
        tty = _FakeTty(replies=[_FIXED_CHALLENGE])
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
        self.install(tty=_FakeTty(replies=[_FIXED_CHALLENGE]))
        cap = request_owner_confirmation(_action())
        with self.assertRaises(TypeError):
            json.dumps(cap)

    def test_capability_is_not_pickleable_or_copyable(self):
        self.install(tty=_FakeTty(replies=[_FIXED_CHALLENGE]))
        cap = request_owner_confirmation(_action())
        with self.assertRaises((TypeError, pickle.PicklingError)):
            pickle.dumps(cap)
        with self.assertRaises(TypeError):
            copy.copy(cap)
        with self.assertRaises(TypeError):
            copy.deepcopy(cap)

    def test_direct_construction_cannot_mint_authority(self):
        now = _BASE_TIME
        kwargs = dict(
            action_digest="a" * 64,
            owner_uid=_FIXED_OWNER_UID,
            created_at=now,
            expires_at=now + timedelta(seconds=60),
            created_monotonic=0.0,
            expires_monotonic=60.0,
            mint_pid=os.getpid(),
            process_token="tok",
        )
        with self.assertRaises(OwnerAuthError):
            OwnerApproval(object(), **kwargs)
        # Missing the private sentinel entirely is a TypeError.
        with self.assertRaises(TypeError):
            OwnerApproval(**kwargs)

    def test_wrong_challenge_fails_closed_and_mints_nothing(self):
        tty = _FakeTty(replies=["WRONG-0000", "WRONG-0000", "WRONG-0000"])
        self.install(tty=tty)
        with self.assertRaises(OwnerChallengeError):
            request_owner_confirmation(_action())
        self.assertTrue(tty.closed)

    def test_over_length_reply_fails_closed(self):
        too_long = "X" * (owner_auth.MAX_CHALLENGE_LINE_CHARS + 5)
        tty = _FakeTty(replies=[too_long])
        self.install(tty=tty)
        with self.assertRaises(OwnerChallengeError):
            request_owner_confirmation(_action())

    def test_exhausted_deadline_fails_closed(self):
        tty = _FakeTty(replies=[None])
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
            tty=_FakeTty(replies=[_FIXED_CHALLENGE]),
            owner_uid=0,
            real_uid=_FIXED_OWNER_UID,
        )
        with self.assertRaises(OwnerIdentityError):
            request_owner_confirmation(_action())

    def test_terminal_device_ownership_is_not_asserted(self):
        # /dev/tty resolves to a typically root-owned multiplexor, so no device
        # (PTY) owner is asserted: the check is euid + controlling-tty only.
        # The fake exposes no owner method and confirmation still succeeds.
        tty = _FakeTty(replies=[_FIXED_CHALLENGE])
        self.install(tty=tty)
        cap = request_owner_confirmation(_action())
        self.assertIsInstance(cap, OwnerApproval)
        # The boundary never queries a terminal device owner.
        self.assertFalse(hasattr(tty, "device_owner_uid"))

    def test_never_reads_standard_input(self):
        # stdin holds a *correct-looking* value; the boundary must ignore it.
        fake_stdin = io.StringIO(_FIXED_CHALLENGE + "\n")
        tty = _FakeTty(replies=[_FIXED_CHALLENGE])
        self.install(tty=tty)
        with mock.patch.object(sys, "stdin", fake_stdin):
            cap = request_owner_confirmation(_action())
        self.assertIsInstance(cap, OwnerApproval)
        self.assertEqual(fake_stdin.tell(), 0)  # stdin was never read

    def test_render_is_safe_against_terminal_control_injection(self):
        inject = "\x1b[2K\rOWNER APPROVAL GRANTED\nChallenge: HACK-0000\x07"
        action = _action(rationale=inject)
        tty = _FakeTty(replies=[_FIXED_CHALLENGE])
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
        self.install(tty=_FakeTty(replies=[_FIXED_CHALLENGE]))
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

    def test_target_is_frozen_and_mutation_raises(self):
        # The coordinator's exact repro: a frozen action must not be mutable
        # through its target mapping.
        action = _action()
        with self.assertRaises(TypeError):
            action.target["project_id"] = "FOREIGN"  # type: ignore[index]
        with self.assertRaises(TypeError):
            action.target["artifact_id"] = "FOREIGN"  # type: ignore[index]
        # The digest is unchanged and still binds the original target.
        self.assertEqual(action.target["project_id"], "prj_syn")

    def test_display_digest_equals_bound_digest(self):
        # The digest shown on the terminal is the same one the capability binds
        # (single capture; no time-of-check/time-of-use gap).
        action = _action(rationale="display-vs-bound check")
        tty = _FakeTty(replies=[_FIXED_CHALLENGE])
        self.install(tty=tty)
        cap = request_owner_confirmation(action)
        prompt = tty.written[0]
        shown = prompt.split("Action digest: ", 1)[1].splitlines()[0]
        self.assertEqual(shown, action_digest(action))
        evidence = consume_owner_approval(cap, action)
        self.assertEqual(evidence.action_digest, shown)

    def test_tampered_action_fails_closed_before_io(self):
        # A hand-tampered action (target swapped via object.__setattr__) is
        # re-validated on capture and fails closed before any terminal I/O.
        action = _action()
        object.__setattr__(
            action,
            "target",
            {"project_id": "prj_foreign", "artifact_id": "a", "revision_id": "r"},
        )
        tty = _FakeTty(replies=[_FIXED_CHALLENGE])
        self.install(tty=tty)
        with self.assertRaises(OwnerActionError):
            request_owner_confirmation(action)
        self.assertEqual(tty.written, [])  # nothing was written

    def test_write_failure_is_curated_typed_error(self):
        class _RaisingTty(_FakeTty):
            def write(self, text, *, deadline=None):
                raise OwnerAuthTtyUnavailable("curated write failure")

        self.install(tty=_RaisingTty(replies=[_FIXED_CHALLENGE]))
        with self.assertRaises(OwnerAuthTtyUnavailable):
            request_owner_confirmation(_action())


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

    def test_action_errors_do_not_echo_input(self):
        # Every failing field must produce a static message: no canary/secret
        # reflection of the offending identifier, gate kind, decision, reference,
        # digest, or rationale.
        canary = "SYNTHETIC_SECRET_CANARY"
        cases = [
            (_action, {"gate_kind": canary}),
            (_action, {"decision": canary}),
            (_action, {"gate_id": canary + " with space"}),
            (_action, {"snapshot_digest": canary}),
            (
                _action,
                {
                    "project_id": "prj_a",
                    "target": {
                        "project_id": "prj_b",
                        "artifact_id": canary,
                        "revision_id": canary,
                    },
                },
            ),
        ]
        for factory, overrides in cases:
            with self.subTest(overrides=overrides):
                with self.assertRaises(OwnerActionError) as ctx:
                    factory(**overrides)
                self.assertNotIn(canary, str(ctx.exception))
                self.assertNotIn("prj_b", str(ctx.exception))

    def test_long_identifier_rejected(self):
        too_long = "i" * (owner_auth.MAX_ACTION_IDENTIFIER_CHARS + 1)
        with self.assertRaises(OwnerActionError):
            _action(gate_id=too_long)

    def test_long_rationale_rejected(self):
        too_long = "r" * (owner_auth.MAX_RATIONALE_CHARS + 1)
        with self.assertRaises(OwnerActionError):
            _action(rationale=too_long)

    def test_all_target_identifiers_are_bounded(self):
        # The coordinator repro: a 10k-character artifact id was accepted and
        # rendered; every target identifier is now bounded and rejected.
        for key in ("artifact_id", "revision_id"):
            target = {
                "project_id": "prj_syn",
                "artifact_id": "art_syn",
                "revision_id": "rev_syn",
            }
            target[key] = "x" * 10000
            with self.subTest(key=key):
                with self.assertRaises(OwnerActionError):
                    _action(target=target)
        with self.assertRaises(OwnerActionError):
            OwnerAction(
                project_id="p",
                target={
                    "project_id": "p",
                    "artifact_id": "x" * 10000,
                    "revision_id": "r",
                },
                snapshot_digest=_SNAPSHOT,
                gate_kind="pilot",
                gate_id="g",
                decision="approved",
                rationale="ok",
            )

    def test_target_identifier_at_bound_is_accepted(self):
        at_bound = "a" * owner_auth.MAX_ACTION_IDENTIFIER_CHARS
        action = _action(
            target={
                "project_id": "prj_syn",
                "artifact_id": at_bound,
                "revision_id": "rev_syn",
            }
        )
        self.assertEqual(action.target["artifact_id"], at_bound)

    def test_snapshot_digest_rejects_trailing_newline(self):
        with self.assertRaises(OwnerActionError):
            _action(snapshot_digest="a" * 64 + "\n")


# --------------------------------------------------------------------------- #
# Bounded confirmation knobs (no raw OverflowError; no unbounded caller text)
# --------------------------------------------------------------------------- #
class TestBoundedKnobs(OwnerAuthTestBase):
    def _installed(self):
        tty = _FakeTty(replies=[_FIXED_CHALLENGE])
        self.install(tty=tty)
        return tty

    def test_huge_int_knob_is_a_typed_error_not_overflow(self):
        self._installed()
        for kwargs in (
            {"timeout_seconds": 10**1000},
            {"expires_after_seconds": 10**1000},
            {"max_attempts": 10**1000},
        ):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(owner_auth.OwnerAuthConfigError):
                    request_owner_confirmation(_action(), **kwargs)

    def test_out_of_range_and_bad_type_knobs_fail_closed(self):
        self._installed()
        cases = [
            {"timeout_seconds": 0},
            {"timeout_seconds": -1},
            {"timeout_seconds": float("inf")},
            {"timeout_seconds": float("nan")},
            {"timeout_seconds": True},
            {"timeout_seconds": "10"},
            {"timeout_seconds": owner_auth.MAX_OWNER_CONFIRM_TIMEOUT_SECONDS + 1},
            {"expires_after_seconds": 0},
            {"expires_after_seconds": owner_auth.MAX_OWNER_APPROVAL_TTL_SECONDS + 1},
            {"max_attempts": 0},
            {"max_attempts": -3},
            {"max_attempts": owner_auth.MAX_OWNER_CONFIRM_ATTEMPTS + 1},
            {"max_attempts": "3"},
        ]
        for kwargs in cases:
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(owner_auth.OwnerAuthConfigError):
                    request_owner_confirmation(_action(), **kwargs)

    def test_knob_error_raised_before_any_terminal_io(self):
        tty = self._installed()
        with self.assertRaises(owner_auth.OwnerAuthConfigError):
            request_owner_confirmation(_action(), timeout_seconds=10**1000)
        self.assertEqual(tty.written, [])

    def test_prompt_byte_cap_enforced_before_io(self):
        tty = self._installed()
        with mock.patch.object(owner_auth, "MAX_PROMPT_BYTES", 10):
            with self.assertRaises(OwnerActionError):
                request_owner_confirmation(_action())
        self.assertEqual(tty.written, [])

    def test_worst_case_escaped_rationale_within_prompt_cap(self):
        # A rationale of control characters expands ~4x under escaping; the
        # rendered prompt must still be within the byte cap and be accepted.
        tty = self._installed()
        action = _action(rationale="\x01" * owner_auth.MAX_RATIONALE_CHARS)
        request_owner_confirmation(action)
        rendered = tty.written[0]
        self.assertLessEqual(
            len(rendered.encode("utf-8")), owner_auth.MAX_PROMPT_BYTES
        )


# --------------------------------------------------------------------------- #
# Consumption: binding, expiry, replay, forgery
# --------------------------------------------------------------------------- #
class TestConsumption(OwnerAuthTestBase):
    def _minted(self, action=None):
        action = action or _action()
        self.install(tty=_FakeTty(replies=[_FIXED_CHALLENGE]))
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
        # The captured target is frozen: in-place mutation raises.
        with self.assertRaises(TypeError):
            action.target["artifact_id"] = "art_swapped"  # type: ignore[index]
        # A different-but-valid action still fails the digest binding.
        swapped = _action(
            target={
                "project_id": "prj_syn",
                "artifact_id": "art_swapped",
                "revision_id": "rev_syn",
            }
        )
        with self.assertRaises(ActionDigestMismatch):
            consume_owner_approval(cap, swapped)

    def test_expired_capability_fails_closed(self):
        action, cap = self._minted()
        self.clock[0] = _BASE_TIME + timedelta(
            seconds=owner_auth.OWNER_APPROVAL_TTL_SECONDS + 1
        )
        with self.assertRaises(ApprovalExpired):
            consume_owner_approval(cap, action)

    def test_monotonic_bound_expires_and_wall_rollback_cannot_prolong(self):
        # A short TTL, then advance monotonic time past it while rolling the wall
        # clock *backwards*. A wall-clock-only check would wrongly still honor the
        # capability; the monotonic bound expires it.
        action = _action()
        self.install(tty=_FakeTty(replies=[_FIXED_CHALLENGE]))
        cap = request_owner_confirmation(action, expires_after_seconds=10)
        self.mono[0] += 11  # elapsed real time
        self.clock[0] = _BASE_TIME - timedelta(seconds=3600)  # rolled back wall
        with self.assertRaises(ApprovalExpired):
            consume_owner_approval(cap, action)

    def test_uid_mismatch_at_consumption_fails_closed(self):
        action, cap = self._minted()
        with mock.patch.object(owner_auth, "_current_owner_uid", lambda: _FIXED_OWNER_UID + 1):
            with self.assertRaises(OwnerUidMismatch):
                consume_owner_approval(cap, action)

    def test_elevated_identity_at_consumption_fails_closed(self):
        action, cap = self._minted()
        with mock.patch.object(
            owner_auth, "_current_owner_uid", lambda: _FIXED_OWNER_UID
        ), mock.patch.object(
            owner_auth, "_real_uid", lambda: _FIXED_OWNER_UID - 1
        ):
            with self.assertRaises(OwnerIdentityError):
                consume_owner_approval(cap, action)

    def test_forked_child_cannot_consume_capability(self):
        action, cap = self._minted()
        read_fd, write_fd = os.pipe()
        pid = os.fork()
        if pid == 0:  # pragma: no cover - child process
            os.close(read_fd)
            try:
                consume_owner_approval(cap, action)
                os.write(write_fd, b"CONSUMED")
            except owner_auth.CrossProcessCapabilityError:
                os.write(write_fd, b"CROSSPROCESS")
            except BaseException as exc:  # noqa: BLE001 - report the class
                os.write(write_fd, ("OTHER:" + type(exc).__name__).encode())
            finally:
                os.close(write_fd)
                os._exit(0)
        os.close(write_fd)
        try:
            data = os.read(read_fd, 64)
        finally:
            os.close(read_fd)
            os.waitpid(pid, 0)
        self.assertEqual(data, b"CROSSPROCESS")
        # The minting process can still consume it exactly once.
        evidence = consume_owner_approval(cap, action)
        self.assertEqual(evidence.action_digest, action_digest(action))
        with self.assertRaises(ApprovalAlreadyConsumed):
            consume_owner_approval(cap, action)

    def test_forged_values_fail_closed(self):
        action, _ = self._minted()
        for forged in (True, False, None, "human", "owner_authenticated", {}, [], 1):
            with self.subTest(forged=forged):
                with self.assertRaises(InvalidCapabilityError):
                    consume_owner_approval(forged, action)

    def test_error_messages_do_not_leak_the_challenge(self):
        self.install(
            tty=_FakeTty(replies=["WRONG-0000"])
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

    def test_write_deadline_fails_closed(self):
        read_fd, write_fd = os.pipe()
        self.addCleanup(os.close, read_fd)
        self.addCleanup(os.close, write_fd)
        tty = owner_auth._RealTty(write_fd)
        with self.assertRaises(OwnerAuthTtyUnavailable):
            tty.write("prompt", deadline=time.monotonic() - 1)

    def test_write_to_full_undrained_pipe_fails_closed_before_deadline(self):
        # The read end is never drained, so the pipe fills (a stand-in for a PTY
        # output queue with no reader). A large write must fail closed under the
        # deadline instead of blocking forever.
        read_fd, write_fd = os.pipe()
        self.addCleanup(os.close, read_fd)
        self.addCleanup(os.close, write_fd)
        tty = owner_auth._RealTty(write_fd)
        self.assertFalse(os.get_blocking(write_fd))  # non-blocking handle
        start = time.monotonic()
        with self.assertRaises(OwnerAuthTtyUnavailable):
            tty.write("P" * 200000, deadline=time.monotonic() + 0.5)
        self.assertLess(time.monotonic() - start, 5.0)

    def test_write_is_chunked_and_nonblocking(self):
        read_fd, write_fd = os.pipe()
        self.addCleanup(os.close, read_fd)
        self.addCleanup(os.close, write_fd)
        tty = owner_auth._RealTty(write_fd)
        payload = "A" * (owner_auth.WRITE_CHUNK_BYTES + 7)
        tty.write(payload, deadline=time.monotonic() + 2)
        received = b""
        while len(received) < len(payload):
            chunk = os.read(read_fd, len(payload) - len(received))
            if not chunk:
                break
            received += chunk
        self.assertEqual(received, payload.encode())


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


#: Fills a real PTY's output queue (master never drained) then writes a large
#: payload through ``_RealTty``; must fail closed at the deadline, never hang.
_PTY_FILL_SCRIPT = """
import os, sys, time
sys.path.insert(0, __SRC__)
from review_article_agent.runtime import owner_auth as oa
master, slave = os.openpty()
os.set_blocking(slave, False)
while True:
    try:
        os.write(slave, b"x" * 8192)
    except BlockingIOError:
        break
tty = oa._RealTty(slave)  # owned handle; sets non-blocking
start = time.monotonic()
try:
    tty.write("P" * 20000, deadline=time.monotonic() + 1.0)
    sys.stdout.write("WROTE\\n")
except oa.OwnerAuthTtyUnavailable:
    sys.stdout.write("BOUNDED %.2f\\n" % (time.monotonic() - start))
finally:
    try:
        os.close(master)
        os.close(slave)
    except OSError:
        pass
sys.stdout.flush()
"""


def _pty_fill_script() -> str:
    return _PTY_FILL_SCRIPT.replace("__SRC__", repr(str(SRC)))


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

    @unittest.skipUnless(hasattr(os, "openpty"), "requires os.openpty")
    def test_full_pty_output_queue_write_is_bounded_not_hanging(self):
        # Real owned PTY with a filled output queue and no reader: the write must
        # fail closed at the deadline. The subprocess has a hard timeout, so a
        # regression to a blocking write fails the test instead of hanging.
        proc = subprocess.run(
            [sys.executable, "-c", _pty_fill_script()],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=20,
        )
        self.assertIn("BOUNDED", proc.stdout, proc.stdout + proc.stderr)
        self.assertNotIn("WROTE", proc.stdout)

    @unittest.skipUnless(hasattr(os, "forkpty"), "requires os.forkpty")
    def test_real_pty_device_owner_is_a_multiplexor_not_asserted(self):
        # Document real Linux behavior: a fd opened on /dev/tty resolves to
        # /dev/tty (a root-owned multiplexor), not the user's PTY node. The
        # boundary therefore does NOT assert a terminal device owner; it relies
        # on euid + controlling-tty, which is why the real confirmation above
        # still succeeds.
        probe = (
            "import os, json\n"
            "fd = os.open('/dev/tty', os.O_RDWR | os.O_NOCTTY)\n"
            "name = os.ttyname(fd)\n"
            "print('PROBE ' + json.dumps({'name': name, "
            "'isatty': os.isatty(fd), "
            "'node_owner': os.stat(name).st_uid, "
            "'euid': os.geteuid()}))\n"
        )
        pid, master_fd = os.forkpty()
        if pid == 0:  # pragma: no cover - child process
            with contextlib.suppress(Exception):
                os.execv(sys.executable, [sys.executable, "-c", probe])
            os._exit(127)
        buffer = ""
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and "PROBE " not in buffer:
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
        with contextlib.suppress(OSError):
            os.close(master_fd)
        with contextlib.suppress(ChildProcessError, OSError):
            os.waitpid(pid, 0)

        self.assertIn("PROBE ", buffer, buffer)
        info = json.loads(buffer.split("PROBE ", 1)[1].splitlines()[0])
        self.assertTrue(info["isatty"])
        # We deliberately make no ownership equality assertion: on Linux the
        # /dev/tty node is root-owned. We only assert the boundary does not
        # depend on it (the confirmation test above succeeds).
        self.assertIsInstance(info["node_owner"], int)
        self.assertIsInstance(info["euid"], int)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
