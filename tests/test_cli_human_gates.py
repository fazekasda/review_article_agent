"""CLI acceptance tests for the integrated app-owned human-approval surface.

Covers Step 04 deliverable 5 ("App-owned human approvals presented through CLI")
and acceptance criteria 1, 2, and 4, ``docs/contracts/02-gates-and-runtime.md``
§1.3 (authority provenance is out-of-band), §2 (revision-specific approvals over
an exact, immutable dependency snapshot), and §3 (stale/invalidated/superseded),
plus the existing ``docs/cli.md`` output/exit/secret conventions.

Every case drives the **real CLI** (``review_article_agent.cli.main``) over a
**real local project store**, invoked from an **unrelated working directory** so
the store is reached only through the registry. The owner confirmation is
exercised by patching **only** the owner-auth low-level private terminal/identity
seams (exactly as ``tests/test_human_approvals.py`` does) — never by patching
``request_owner_confirmation``/``consume_owner_approval`` into a fabricated
authority or by importing a capability. One end-to-end case uses a **real PTY**
(``os.forkpty``) and a real ``/dev/tty``.

Asserted here:

* ``gate confirm`` with the exact previewed ``--snapshot-digest`` records one
  durable human approval (``actor_kind=human``,
  ``authority=owner_authenticated``, an out-of-band ``owner_auth_ref``) and
  ``gate status``/``gate list`` report the named gate as satisfied — and nothing
  more;
* a wrong or malformed expected digest, a stale/superseded pin, a cancelled
  confirmation, a missing controlling terminal, and any imported
  authority/actor flag are all refused with a stable category and **zero** rows;
* a mutation that lands between the terminal confirmation and the pre-commit
  revalidation denies the approval (no rows);
* piped standard input can never confirm;
* no source bytes, rationale text, or secret canary is reflected in the CLI
  output, and no named-gate claim (including ``final_release``) authorizes
  execution/export or asserts scientific validation.

All data is synthetic: no secrets, no real project, no scientific evidence, no
network, no model/paid calls.

Run with::

    PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_cli*gates.py'
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import select
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from _tiers import slow

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from review_article_agent.storage import paths  # noqa: E402

_OPS = ("acquisition", "storage", "redistribution", "hosted_transfer")

#: A synthetic value that must never appear in output, an error, or a prompt.
SECRET = "SYNTHETIC-SECRET-DO-NOT-ECHO"

#: A caller-authored rationale canary: shown to the owner on the terminal (the
#: owner approves the exact action), but never echoed in CLI output.
_RATIONALE_CANARY = "CANARY-RATIONALE-7731"

#: The fixed per-operation challenge the patched owner-auth seam returns.
CHALLENGE = "TEST-CHAL-0001"

_DIGEST = "d" * 64


def _rights_record(project_id: str, artifact_id: str, revision_id: str) -> dict:
    evidence = {"ref": "lic:pd-synthetic", "provenance": "publisher:pd-list"}
    return {
        "rights_id": f"RIGHTS-{revision_id}",
        "project_id": project_id,
        "record_revision": 1,
        "subject": {
            "kind": "source_revision",
            "project_id": project_id,
            "artifact_id": artifact_id,
            "revision_id": revision_id,
        },
        "operations": {op: "allowed" for op in _OPS},
        "basis": {op: {"kind": "public_domain", "evidence": dict(evidence)} for op in _OPS},
        "authorization": None,
        "restrictions": [],
        "privacy": {"personal_data": False, "confidentiality": "public", "denial": False},
        "recorded_by": "user",
        "recorded_at": "2026-10-27T00:00:00Z",
        "derived_status": "valid",
    }


def _envelope(text: str = "synthetic") -> dict:
    return {"schema_version": 1, "document_type": "fixture_note", "payload": {"t": text}}


# --------------------------------------------------------------------------- #
# The in-process CLI runner: only the owner-auth low-level seams are patched.
# --------------------------------------------------------------------------- #
_SEAM_RUNNER = '''
import json
import sys

SRC = __SRC__
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from datetime import datetime
from unittest import mock

from review_article_agent.runtime import owner_auth as oa

cfg = json.loads(sys.argv[1])


class _FakeTty:
    """A low-level terminal double: only the terminal operations are mocked."""

    def __init__(self, replies, is_tty, on_read):
        self._replies = list(replies)
        self._is_tty = is_tty
        self._on_read = on_read
        self.written = []
        self.closed = False

    def is_tty(self):
        return self._is_tty

    def write(self, text, *, deadline=None):
        self.written.append(text)

    def read_line(self, *, max_chars, deadline):
        if self._on_read is not None:
            callback, self._on_read = self._on_read, None
            callback()
        return self._replies.pop(0) if self._replies else None

    def close(self):
        self.closed = True


def _mutate(spec):
    """Import a newer revision of the target artifact on a fresh connection."""
    from review_article_agent.storage import ArtifactRepository, ProjectStore, paths

    ops = ("acquisition", "storage", "redistribution", "hosted_transfer")
    rights = {
        "rights_id": "RIGHTS-" + spec["revision_id"],
        "project_id": spec["project_id"],
        "record_revision": 1,
        "subject": {
            "kind": "source_revision",
            "project_id": spec["project_id"],
            "artifact_id": spec["artifact_id"],
            "revision_id": spec["revision_id"],
        },
        "operations": {op: "allowed" for op in ops},
        "basis": {
            op: {"kind": "public_domain", "evidence": {"ref": "lic:pd-syn", "provenance": "publisher:pd-list"}}
            for op in ops
        },
        "authorization": None,
        "restrictions": [],
        "privacy": {"personal_data": False, "confidentiality": "public", "denial": False},
        "recorded_by": "user",
        "recorded_at": "2026-10-28T00:00:00Z",
        "derived_status": "valid",
    }
    store = ProjectStore.open_at(
        paths.app_dir_for(spec["root"]), expected_project_id=spec["project_id"]
    )
    try:
        repo = ArtifactRepository(store)
        repo.import_revision(
            artifact_id=spec["artifact_id"],
            content=spec["content"].encode(),
            origin_locator="import:synthetic",
            rights=rights,
            revision_id=spec["revision_id"],
            parent_revision_id=spec.get("parent"),
        )
    finally:
        store.close()


tty = _FakeTty(
    cfg.get("replies", []),
    cfg.get("is_tty", True),
    (lambda: _mutate(cfg["mutate"])) if cfg.get("mutate") else None,
)

patches = [
    mock.patch.object(oa, "_new_challenge", lambda: cfg.get("challenge", "TEST-CHAL-0001")),
    mock.patch.object(oa, "_current_owner_uid", lambda: cfg.get("owner_uid", 1000)),
    mock.patch.object(oa, "_real_uid", lambda: cfg.get("real_uid", 1000)),
    mock.patch.object(oa, "_utcnow", lambda: datetime.fromisoformat(cfg["now"])),
    mock.patch.object(oa, "_monotonic", lambda: cfg.get("mono", 1000.0)),
    mock.patch.object(oa, "_open_controlling_tty", lambda: tty),
]
for _p in patches:
    _p.start()

from review_article_agent.cli import main as climain

rc = climain.main(cfg["argv"])
with open(cfg["prompt_file"], "w", encoding="utf-8") as fh:
    json.dump(tty.written, fh)
sys.stdout.flush()
sys.stderr.flush()
sys.exit(rc)
'''

#: Real-PTY child: run the actual CLI so ``/dev/tty`` is the pty slave.
_PTY_CHILD = '''
import json
import sys

SRC = __SRC__
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from review_article_agent.cli import main as climain

sys.exit(climain.main(json.loads(__ARGV__)))
'''


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="raa-cli-hgates-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.state = self.tmp / "state"
        self.cwd = self.tmp / "elsewhere"
        self.cwd.mkdir()

    def _env(self) -> dict:
        env = dict(os.environ)
        existing = env.get("PYTHONPATH")
        env["PYTHONPATH"] = str(SRC) + (os.pathsep + existing if existing else "")
        env.pop("XDG_DATA_HOME", None)
        return env

    def raw(self, *args: str, **kwargs):
        return subprocess.run(
            [sys.executable, "-m", "review_article_agent", *args],
            capture_output=True, text=True, env=self._env(), cwd=str(self.cwd),
            **kwargs,
        )

    def cli(self, *args: str, expect: int = 0) -> dict:
        proc = self.raw(*args)
        self.assertEqual(
            proc.returncode, expect,
            f"args={args} rc={proc.returncode}\nstdout={proc.stdout}\nstderr={proc.stderr}",
        )
        if expect == 0:
            return json.loads(proc.stdout)
        self.assertNotIn("Traceback", proc.stderr)
        return json.loads(proc.stderr)

    def write_json(self, name: str, data: object) -> Path:
        path = self.tmp / name
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    def create_project(self, name: str) -> str:
        out = self.cli("--data-dir", str(self.state), "create", str(self.tmp / name), "--name", name)
        return out["project"]["project_id"]

    def artifact_create(self, pid: str, artifact_id: str, kind: str) -> None:
        self.cli("--data-dir", str(self.state), "artifact", "create",
                 "--artifact-id", artifact_id, "--kind", kind, "--project", pid)

    def import_revision(self, *, pid, artifact_id, revision_id, text="x",
                        parent=None, deps=None):
        content = self.write_json(f"content-{revision_id}.json", _envelope(text))
        rights = self.write_json(f"rights-{revision_id}.json",
                                 _rights_record(pid, artifact_id, revision_id))
        args = ["--data-dir", str(self.state), "artifact", "import",
                "--artifact-id", artifact_id, "--revision-id", revision_id,
                "--file", str(content), "--rights-file", str(rights), "--project", pid]
        if parent is not None:
            args += ["--parent", parent]
        if deps is not None:
            deps_path = self.write_json(f"deps-{revision_id}.json", deps)
            args += ["--deps-file", str(deps_path)]
        return self.cli(*args)

    def project_with_revision(self, text="a1") -> str:
        pid = self.create_project("proj")
        self.artifact_create(pid, "art-A", "document")
        self.import_revision(pid=pid, artifact_id="art-A", revision_id="rev-A1", text=text)
        return pid

    def preview_digest(self, *, pid, gate_kind="outline", gate_id="g1",
                       decision="approved", rationale="synthetic human review",
                       revision_id="rev-A1", artifact_id="art-A") -> str:
        out = self.cli(
            "--data-dir", str(self.state), "gate", "prepare",
            "--project", pid, "--artifact-id", artifact_id,
            "--revision-id", revision_id, "--gate-kind", gate_kind,
            "--gate-id", gate_id, "--decision", decision, "--rationale", rationale,
        )
        return out["gate"]["snapshot_digest"]

    def store_path(self, root_name: str = "proj") -> Path:
        return Path(paths.store_path_for(str(self.tmp / root_name)))

    def table_count(self, table: str, root_name: str = "proj") -> int:
        conn = sqlite3.connect(self.store_path(root_name))
        try:
            return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        finally:
            conn.close()

    def assert_no_rows(self) -> None:
        for table in ("human_approval", "human_approval_entry", "owner_auth_evidence"):
            self.assertEqual(self.table_count(table), 0, table)

    # -- the in-process seam runner ----------------------------------------- #
    def seam_cli(self, *argv: str, replies=(CHALLENGE,), challenge=CHALLENGE,
                 owner_uid=1000, real_uid=1000, is_tty=True, mutate=None,
                 now="2026-10-28T12:00:00+00:00", mono=1000.0):
        """Run the real CLI in-process with only the owner-auth seams patched."""
        prompt_file = self.tmp / "prompts.json"
        if prompt_file.exists():
            prompt_file.unlink()
        cfg = {
            "argv": ["--data-dir", str(self.state), *argv],
            "replies": list(replies),
            "challenge": challenge,
            "owner_uid": owner_uid,
            "real_uid": real_uid,
            "is_tty": is_tty,
            "now": now,
            "mono": mono,
            "prompt_file": str(prompt_file),
        }
        if mutate is not None:
            cfg["mutate"] = mutate
        runner = self.tmp / "seam_runner.py"
        runner.write_text(_SEAM_RUNNER.replace("__SRC__", repr(str(SRC))), encoding="utf-8")
        proc = subprocess.run(
            [sys.executable, str(runner), json.dumps(cfg)],
            capture_output=True, text=True, env=self._env(), cwd=str(self.cwd),
        )
        prompts = json.loads(prompt_file.read_text()) if prompt_file.exists() else []
        return proc, prompts

    def confirm_args(self, *, pid, digest, gate_kind="outline", gate_id="g1",
                     decision="approved", rationale="synthetic human review",
                     revision_id="rev-A1", artifact_id="art-A", extra=()):
        return (
            "gate", "confirm",
            "--project", pid, "--artifact-id", artifact_id,
            "--revision-id", revision_id, "--gate-kind", gate_kind,
            "--gate-id", gate_id, "--decision", decision,
            "--rationale", rationale, "--snapshot-digest", digest, *extra,
        )

    def mutation_spec(self, pid, *, artifact_id="art-A", revision_id="rev-A2",
                      content="a2", parent="rev-A1"):
        return {
            "root": str(self.tmp / "proj"),
            "project_id": pid,
            "artifact_id": artifact_id,
            "revision_id": revision_id,
            "content": content,
            "parent": parent,
        }


# --------------------------------------------------------------------------- #
# Happy path + read-back
# --------------------------------------------------------------------------- #
@slow
class GateConfirmRecordsApprovalTest(_Base):
    def test_exact_digest_records_durable_human_approval(self) -> None:
        pid = self.project_with_revision()
        digest = self.preview_digest(pid=pid)
        proc, prompts = self.seam_cli(
            *self.confirm_args(pid=pid, digest=digest)
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)
        out = json.loads(proc.stdout)

        self.assertTrue(out["gate"]["approval_recorded"])
        self.assertIs(out["gate"]["authorizes_execution"], False)
        self.assertIs(out["gate"]["authorizes_export"], False)
        self.assertIs(out["gate"]["scientific_validation"], False)

        approval = out["approval"]
        self.assertEqual(approval["project_id"], pid)
        self.assertEqual(approval["gate_kind"], "outline")
        self.assertEqual(approval["gate_id"], "g1")
        self.assertEqual(approval["decision"], "approved")
        self.assertEqual(approval["snapshot_digest"], digest)
        self.assertEqual(approval["status"], "active")
        self.assertTrue(approval["satisfies_named_gate"])
        self.assertIs(approval["authorizes_execution"], False)
        self.assertIs(approval["authorizes_export"], False)
        self.assertIs(approval["scientific_validation"], False)
        self.assertEqual(approval["granted_by"]["actor_kind"], "human")
        self.assertEqual(approval["granted_by"]["authority"], "owner_authenticated")
        self.assertTrue(approval["granted_by"]["owner_auth_ref"].startswith("auth_"))

        # Exactly one evidence + approval row, and the exact closure entries.
        self.assertEqual(self.table_count("human_approval"), 1)
        self.assertEqual(self.table_count("owner_auth_evidence"), 1)
        self.assertGreaterEqual(self.table_count("human_approval_entry"), 1)

        # The confirmation was presented on the terminal (bound to the snapshot).
        self.assertTrue(prompts)
        prompt = "\n".join(prompts)
        self.assertIn("OWNER APPROVAL REQUIRED", prompt)
        self.assertIn(digest, prompt)
        self.assertIn("g1", prompt)

    def test_status_and_list_report_the_named_gate_only(self) -> None:
        pid = self.project_with_revision()
        digest = self.preview_digest(pid=pid)
        proc, _ = self.seam_cli(*self.confirm_args(pid=pid, digest=digest))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        approval_id = json.loads(proc.stdout)["approval"]["approval_id"]

        status = self.cli(
            "--data-dir", str(self.state), "gate", "status",
            "--project", pid, "--approval-id", approval_id,
        )["approval"]
        self.assertEqual(status["approval_id"], approval_id)
        self.assertEqual(status["status"], "active")
        self.assertEqual(status["snapshot_digest"], digest)
        self.assertTrue(status["satisfies_named_gate"])
        self.assertIs(status["authorizes_execution"], False)
        self.assertIs(status["authorizes_export"], False)
        self.assertIs(status["scientific_validation"], False)
        self.assertTrue(status["owner_auth_ref"].startswith("auth_"))

        listed = self.cli("--data-dir", str(self.state), "gate", "list", "--project", pid)
        self.assertEqual(listed["project_id"], pid)
        self.assertEqual(listed["approval_count"], 1)
        self.assertEqual(listed["approvals"][0]["approval_id"], approval_id)

    def test_unknown_approval_id_is_a_validation_error(self) -> None:
        pid = self.project_with_revision()
        err = self.cli(
            "--data-dir", str(self.state), "gate", "status",
            "--project", pid, "--approval-id", "hapv_missing", expect=2,
        )
        self.assertEqual(err["error"]["category"], "validation")
        self.assertNotIn("Traceback", json.dumps(err))


# --------------------------------------------------------------------------- #
# Refusals that must write zero rows
# --------------------------------------------------------------------------- #
@slow
class GateConfirmRefusalTest(_Base):
    def test_wrong_expected_digest_is_refused_before_the_terminal(self) -> None:
        pid = self.project_with_revision()
        self.preview_digest(pid=pid)  # the previewed pins
        proc, prompts = self.seam_cli(*self.confirm_args(pid=pid, digest=_DIGEST))
        self.assertEqual(proc.returncode, 3, proc.stderr)
        self.assertEqual(json.loads(proc.stderr)["error"]["category"], "conflict")
        self.assertEqual(prompts, [])  # no terminal I/O before the check
        self.assert_no_rows()

    def test_malformed_expected_digest_is_a_validation_error(self) -> None:
        pid = self.project_with_revision()
        proc, prompts = self.seam_cli(*self.confirm_args(pid=pid, digest="not-a-digest"))
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertEqual(json.loads(proc.stderr)["error"]["category"], "validation")
        self.assertEqual(prompts, [])
        self.assert_no_rows()

    def test_stale_pins_are_refused_with_no_rows(self) -> None:
        pid = self.project_with_revision()
        digest = self.preview_digest(pid=pid)
        # Move the target's head after the preview, so the pinned revision is no
        # longer current; re-preparation inside confirm must refuse.
        self.import_revision(pid=pid, artifact_id="art-A", revision_id="rev-A2",
                             text="a2", parent="rev-A1")
        proc, prompts = self.seam_cli(*self.confirm_args(pid=pid, digest=digest))
        self.assertEqual(proc.returncode, 3, proc.stderr)
        self.assertEqual(json.loads(proc.stderr)["error"]["category"], "conflict")
        self.assertEqual(prompts, [])
        self.assert_no_rows()

    def test_mutation_between_confirmation_and_precommit_denies(self) -> None:
        pid = self.project_with_revision()
        digest = self.preview_digest(pid=pid)
        # The mutation lands during the terminal read, after the snapshot was
        # prepared but before record_decision revalidates it.
        proc, prompts = self.seam_cli(
            *self.confirm_args(pid=pid, digest=digest),
            mutate=self.mutation_spec(pid),
        )
        self.assertEqual(proc.returncode, 3, proc.stderr)
        self.assertEqual(json.loads(proc.stderr)["error"]["category"], "conflict")
        self.assertTrue(prompts)  # the terminal was reached before the mutation
        self.assert_no_rows()

    def test_cancelled_confirmation_records_no_rows(self) -> None:
        pid = self.project_with_revision()
        digest = self.preview_digest(pid=pid)
        # EOF/abort at the challenge prompt: nothing typed, so nothing confirmed.
        proc, prompts = self.seam_cli(*self.confirm_args(pid=pid, digest=digest), replies=())
        self.assertEqual(proc.returncode, 3, proc.stderr)
        self.assertEqual(json.loads(proc.stderr)["error"]["category"], "conflict")
        self.assertTrue(prompts)
        self.assert_no_rows()

    def test_out_of_range_timeout_is_a_validation_error_before_the_terminal(self) -> None:
        pid = self.project_with_revision()
        digest = self.preview_digest(pid=pid)
        # The owner-auth bounded validator refuses an out-of-range timeout before
        # any terminal I/O; nothing is recorded.
        proc, prompts = self.seam_cli(
            *self.confirm_args(pid=pid, digest=digest, extra=["--timeout-seconds", "0"])
        )
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertEqual(json.loads(proc.stderr)["error"]["category"], "validation")
        self.assertEqual(prompts, [])
        self.assert_no_rows()

    def test_imported_authority_flags_are_rejected_with_no_rows(self) -> None:
        pid = self.project_with_revision()
        digest = self.preview_digest(pid=pid)
        bad_flags = [
            ["--yes"],
            ["--actor", "user"],
            ["--actor-authenticated", "true"],
            ["--authority", "owner_authenticated"],
            ["--owner-auth-ref", "auth_x"],
            ["--approval-id", "hapv_x"],
            ["--authorizes-execution"],
            ["--capability", "opaque"],
        ]
        for extra in bad_flags:
            proc, _ = self.seam_cli(*self.confirm_args(pid=pid, digest=digest, extra=extra))
            self.assertEqual(proc.returncode, 2, (extra, proc.stderr))
            combined = proc.stdout + proc.stderr
            self.assertNotIn("Traceback", combined, extra)
            self.assertEqual(json.loads(proc.stderr)["error"]["category"], "usage", extra)
        self.assert_no_rows()


# --------------------------------------------------------------------------- #
# Real controlling-terminal boundary (no seams): piped stdin / missing tty
# --------------------------------------------------------------------------- #
@slow
class GateConfirmRealTerminalBoundaryTest(_Base):
    def test_piped_stdin_and_missing_tty_cannot_confirm(self) -> None:
        pid = self.project_with_revision()
        digest = self.preview_digest(pid=pid)
        # A new session has no controlling terminal, so /dev/tty cannot be opened.
        # The challenge is offered on standard input; it must be ignored.
        proc = self.raw(
            "--data-dir", str(self.state),
            *self.confirm_args(pid=pid, digest=digest),
            input=CHALLENGE + "\n",
            start_new_session=True,
            timeout=60,
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertNotIn("Traceback", proc.stdout + proc.stderr)
        self.assertEqual(json.loads(proc.stderr)["error"]["category"], "storage")
        self.assert_no_rows()


# --------------------------------------------------------------------------- #
# Output safety and redaction
# --------------------------------------------------------------------------- #
@slow
class GateConfirmSafetyTest(_Base):
    def test_no_source_bytes_or_rationale_text_is_reflected(self) -> None:
        pid = self.create_project("proj")
        self.artifact_create(pid, "art-A", "document")
        self.import_revision(pid=pid, artifact_id="art-A", revision_id="rev-A1", text=SECRET)
        rationale = f"human note {_RATIONALE_CANARY}"
        digest = self.preview_digest(pid=pid, rationale=rationale)
        proc, prompts = self.seam_cli(
            *self.confirm_args(pid=pid, digest=digest, rationale=rationale)
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        combined = proc.stdout + proc.stderr
        # Neither source bytes nor the caller's rationale text reach the output.
        self.assertNotIn(SECRET, combined)
        self.assertNotIn(_RATIONALE_CANARY, combined)
        self.assertNotIn("human note", combined)
        # Source bytes are never rendered on the terminal either.
        terminal = "\n".join(prompts)
        self.assertNotIn(SECRET, terminal)
        # The rationale *is* shown to the owner on the terminal, by design: the
        # confirmation is bound to the exact action the owner is approving.
        self.assertIn(_RATIONALE_CANARY, terminal)

    def test_rejection_does_not_satisfy_the_named_gate(self) -> None:
        pid = self.project_with_revision()
        digest = self.preview_digest(pid=pid, decision="rejected", rationale="not ready")
        proc, _ = self.seam_cli(
            *self.confirm_args(pid=pid, digest=digest, decision="rejected", rationale="not ready")
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        approval = json.loads(proc.stdout)["approval"]
        self.assertEqual(approval["decision"], "rejected")
        self.assertFalse(approval["satisfies_named_gate"])
        self.assertIs(approval["scientific_validation"], False)


# --------------------------------------------------------------------------- #
# Named-gate claim is bounded; historical rows are retained
# --------------------------------------------------------------------------- #
@slow
class GateNamedGateBoundaryTest(_Base):
    def test_final_release_never_bypasses_scientific_validation(self) -> None:
        pid = self.project_with_revision()
        digest = self.preview_digest(pid=pid, gate_kind="final_release", gate_id="g-final")
        proc, _ = self.seam_cli(
            *self.confirm_args(pid=pid, digest=digest, gate_kind="final_release",
                               gate_id="g-final")
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        approval_id = json.loads(proc.stdout)["approval"]["approval_id"]
        status = self.cli(
            "--data-dir", str(self.state), "gate", "status",
            "--project", pid, "--approval-id", approval_id,
        )["approval"]
        self.assertTrue(status["satisfies_named_gate"])
        self.assertIs(status["authorizes_execution"], False)
        self.assertIs(status["authorizes_export"], False)
        self.assertIs(status["scientific_validation"], False)

    def test_a_generic_tool_permission_is_never_a_gate_kind(self) -> None:
        pid = self.project_with_revision()
        proc = self.raw(
            "--data-dir", str(self.state),
            *self.confirm_args(pid=pid, digest=_DIGEST, gate_kind="generic_tool_permission"),
        )
        self.assertEqual(proc.returncode, 2)
        self.assertNotIn("Traceback", proc.stdout + proc.stderr)
        self.assertEqual(json.loads(proc.stderr)["error"]["category"], "usage")
        self.assert_no_rows()

    def test_stale_historical_status_is_retained(self) -> None:
        pid = self.project_with_revision()
        digest = self.preview_digest(pid=pid)
        proc, _ = self.seam_cli(*self.confirm_args(pid=pid, digest=digest))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        approval_id = json.loads(proc.stdout)["approval"]["approval_id"]
        # Move the target's head: the approval is retained for audit but stale.
        self.import_revision(pid=pid, artifact_id="art-A", revision_id="rev-A2",
                             text="a2", parent="rev-A1")
        status = self.cli(
            "--data-dir", str(self.state), "gate", "status",
            "--project", pid, "--approval-id", approval_id,
        )["approval"]
        self.assertEqual(status["status"], "stale")
        self.assertFalse(status["satisfies_named_gate"])
        self.assertIs(status["authorizes_execution"], False)
        self.assertIs(status["scientific_validation"], False)

    def test_reapproval_supersedes_and_retains_the_older_row(self) -> None:
        pid = self.project_with_revision()
        digest = self.preview_digest(pid=pid)
        first, _ = self.seam_cli(*self.confirm_args(pid=pid, digest=digest))
        self.assertEqual(first.returncode, 0, first.stderr)
        first_id = json.loads(first.stdout)["approval"]["approval_id"]
        second, _ = self.seam_cli(*self.confirm_args(pid=pid, digest=digest))
        self.assertEqual(second.returncode, 0, second.stderr)

        old = self.cli(
            "--data-dir", str(self.state), "gate", "status",
            "--project", pid, "--approval-id", first_id,
        )["approval"]
        self.assertEqual(old["status"], "superseded")
        self.assertFalse(old["satisfies_named_gate"])
        self.assertEqual(self.table_count("human_approval"), 2)


# --------------------------------------------------------------------------- #
# Real PTY end-to-end (real /dev/tty)
# --------------------------------------------------------------------------- #
def _drive_pty(master_fd: int, *, deadline: float) -> str:
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
                os.write(master_fd, (match.group(1) + "\n").encode())
    return buffer


class GateConfirmRealPtyTest(_Base):
    @unittest.skipUnless(hasattr(os, "forkpty"), "requires os.forkpty")
    @slow
    def test_real_pty_confirmation_records_a_row(self) -> None:
        pid = self.project_with_revision()
        digest = self.preview_digest(pid=pid)
        argv = [
            "--data-dir", str(self.state),
            *self.confirm_args(pid=pid, digest=digest),
        ]
        script = _PTY_CHILD.replace("__SRC__", repr(str(SRC))).replace(
            "__ARGV__", repr(json.dumps(argv))
        )
        pid_child, master_fd = os.forkpty()
        if pid_child == 0:  # pragma: no cover - child process
            with contextlib.suppress(Exception):
                os.chdir(str(self.cwd))
                os.execv(sys.executable, [sys.executable, "-c", script])
            os._exit(127)
        try:
            output = _drive_pty(master_fd, deadline=time.monotonic() + 60)
        finally:
            with contextlib.suppress(OSError):
                os.close(master_fd)
            _, wait_status = os.waitpid(pid_child, 0)
        self.assertEqual(os.waitstatus_to_exitcode(wait_status), 0, output)
        self.assertIn("satisfies_named_gate", output, output)
        self.assertEqual(self.table_count("human_approval"), 1)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
