"""CLI acceptance tests for the read-only ``gate prepare`` preview surface (T9a).

Covers the Step 04 deliverable 5 ("App-owned human approvals presented through
CLI") and acceptance criteria 1 and 2 ("An agent cannot approve its own
artifact..."; "A saved generic tool permission cannot satisfy an
outline/pilot/exclusion approval"), plus ``docs/contracts/02-gates-and-runtime.md``
§1.3 (authority provenance is out-of-band) and §2 (revision-specific approvals
over an exact, immutable dependency snapshot), and the existing ``docs/cli.md``
output/exit/secret conventions.

Every case drives the CLI as a **real subprocess** (``python -m
review_article_agent``) over a **real local project store**, invoked from an
**unrelated working directory** so the store is reached only through the
registry. ``gate prepare`` is a read-only *preview*: it records no approval,
starts no worker, and authorizes nothing.

Asserted here:

* the summary carries the exact project/target, the named gate kind/id, the
  proposed decision, the snapshot id/digest, the pinned versions and every exact
  dependency pin, and the non-authorizing flags;
* distinct named gates produce distinct snapshots, and identical inputs are
  deterministic;
* an exact current revision prepares; a stale/superseded reference, an unknown
  revision, a cross-project/path mismatch, and a corrupt pin are all denied with
  a stable category and no traceback;
* a saved generic permission payload and every raw actor/authority flag are
  rejected (no ``gate approve`` exists);
* no source/rights bytes and no secret canary are ever reflected in output or in
  an error;
* preparation writes no approval rows and starts no worker.

All data is synthetic: no secrets, no real project, no scientific evidence.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from _tiers import slow

SRC = Path(__file__).resolve().parents[1] / "src"
REPO = Path(__file__).resolve().parents[1]
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from review_article_agent.storage import paths  # noqa: E402

_OPS = ("acquisition", "storage", "redistribution", "hosted_transfer")

#: A synthetic value that must never appear in output or an error message.
SECRET = "SYNTHETIC-SECRET-DO-NOT-ECHO"


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


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="raa-cli-gates-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.state = self.tmp / "state"
        # An unrelated working directory: the store is reached only through the
        # registry's absolute paths, never via the process CWD.
        self.cwd = self.tmp / "elsewhere"
        self.cwd.mkdir()

    def _env(self) -> dict:
        env = dict(os.environ)
        existing = env.get("PYTHONPATH")
        env["PYTHONPATH"] = str(SRC) + (os.pathsep + existing if existing else "")
        env.pop("XDG_DATA_HOME", None)
        return env

    def raw(self, *args: str):
        return subprocess.run(
            [sys.executable, "-m", "review_article_agent", *args],
            capture_output=True, text=True, env=self._env(), cwd=str(self.cwd),
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

    def gate_prepare(self, *, pid, artifact_id, revision_id, gate_kind,
                     gate_id, decision, rationale, extra=(), expect=0):
        return self.cli(
            "--data-dir", str(self.state), "gate", "prepare",
            "--project", pid, "--artifact-id", artifact_id,
            "--revision-id", revision_id, "--gate-kind", gate_kind,
            "--gate-id", gate_id, "--decision", decision,
            "--rationale", rationale, *extra, expect=expect,
        )

    def store_path(self, root_name: str) -> Path:
        return Path(paths.store_path_for(str(self.tmp / root_name)))

    def table_count(self, root_name: str, table: str) -> int:
        conn = sqlite3.connect(self.store_path(root_name))
        try:
            return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        finally:
            conn.close()


@slow
class GatePreparePreviewTest(_Base):
    def _project(self) -> str:
        pid = self.create_project("proj")
        self.artifact_create(pid, "art-A", "document")
        self.import_revision(pid=pid, artifact_id="art-A", revision_id="rev-A1", text="a1")
        return pid

    def test_exact_preview_is_non_authorizing_and_complete(self) -> None:
        pid = self._project()
        out = self.gate_prepare(
            pid=pid, artifact_id="art-A", revision_id="rev-A1",
            gate_kind="outline", gate_id="g-outline-1", decision="approved",
            rationale="synthetic human review",
        )
        gate = out["gate"]
        self.assertEqual(gate["project_id"], pid)
        self.assertEqual(gate["target"],
                         {"project_id": pid, "artifact_id": "art-A", "revision_id": "rev-A1"})
        self.assertEqual(gate["gate_kind"], "outline")
        self.assertEqual(gate["gate_id"], "g-outline-1")
        self.assertEqual(gate["proposed_decision"], "approved")
        self.assertTrue(gate["snapshot_id"].startswith("psnap_"))
        self.assertEqual(len(gate["snapshot_digest"]), 64)
        self.assertEqual(gate["snapshot_id"], "psnap_" + gate["snapshot_digest"])
        # Exact closure: target plus its (single) revision.
        self.assertGreaterEqual(gate["dependency_count"], 1)
        self.assertEqual(gate["dependencies"][0]["role"], "target")
        self.assertEqual(gate["dependencies"][0]["artifact_id"], "art-A")
        for dep in gate["dependencies"]:
            self.assertEqual(dep["project_id"], pid)
            self.assertTrue(dep["digest"].startswith("sha256:"))
            self.assertTrue(dep["rights_digest"].startswith("sha256:"))
        # Version info is pinned (human-authored; no invented model id).
        self.assertEqual(gate["versions"]["context_kind"], "human_authored")
        self.assertEqual(gate["versions"]["model"]["state"], "not_applicable")
        # Non-authorizing flags.
        self.assertIs(gate["approval_recorded"], False)
        self.assertIs(gate["authorizes_execution"], False)
        self.assertIs(gate["authorizes_export"], False)
        self.assertIs(gate["scientific_validation"], False)

    def test_prepare_writes_no_approval_and_starts_no_worker(self) -> None:
        pid = self._project()
        self.gate_prepare(
            pid=pid, artifact_id="art-A", revision_id="rev-A1",
            gate_kind="pilot", gate_id="g-pilot-1", decision="approved",
            rationale="synthetic",
        )
        self.assertEqual(self.table_count("proj", "fixture_approval"), 0)
        self.assertEqual(self.table_count("proj", "fixture_approval_entry"), 0)
        # No worker control runtime directory/socket was created.
        self.assertFalse((self.state / "runtime").exists())

    def test_no_source_bytes_or_secret_reflected(self) -> None:
        pid = self.create_project("proj")
        self.artifact_create(pid, "art-A", "document")
        self.import_revision(pid=pid, artifact_id="art-A", revision_id="rev-A1",
                             text=SECRET)
        out = self.gate_prepare(
            pid=pid, artifact_id="art-A", revision_id="rev-A1",
            gate_kind="artifact_revision", gate_id="g1", decision="approved",
            rationale=f"human note {SECRET}",
        )
        blob = json.dumps(out)
        self.assertNotIn(SECRET, blob)
        # The rationale is bound into the digest but is not echoed.
        self.assertNotIn("human note", blob)


@slow
class GatePrepareDeterminismTest(_Base):
    def _project(self) -> str:
        pid = self.create_project("proj")
        self.artifact_create(pid, "art-A", "document")
        self.import_revision(pid=pid, artifact_id="art-A", revision_id="rev-A1", text="a1")
        return pid

    def test_valid_named_gates_are_distinct_and_deterministic(self) -> None:
        pid = self._project()

        def digest(gate_kind: str, gate_id: str) -> str:
            return self.gate_prepare(
                pid=pid, artifact_id="art-A", revision_id="rev-A1",
                gate_kind=gate_kind, gate_id=gate_id, decision="approved",
                rationale="same rationale",
            )["gate"]["snapshot_digest"]

        outline = digest("outline", "g1")
        pilot = digest("pilot", "g1")
        outline_other_id = digest("outline", "g2")
        outline_again = digest("outline", "g1")
        # Distinct gate kind -> distinct snapshot (a generic tool grant cannot
        # stand in for an outline/pilot gate).
        self.assertNotEqual(outline, pilot)
        # Distinct gate instance -> distinct snapshot.
        self.assertNotEqual(outline, outline_other_id)
        # Identical inputs are deterministic.
        self.assertEqual(outline, outline_again)


@slow
class GatePrepareDenialTest(_Base):
    def test_exact_revision_prepares_but_stale_and_unknown_are_denied(self) -> None:
        pid = self.create_project("proj")
        self.artifact_create(pid, "art-A", "document")
        self.import_revision(pid=pid, artifact_id="art-A", revision_id="rev-A1", text="a1")
        self.import_revision(pid=pid, artifact_id="art-A", revision_id="rev-A2",
                             text="a2", parent="rev-A1")
        # The current head prepares.
        self.gate_prepare(pid=pid, artifact_id="art-A", revision_id="rev-A2",
                          gate_kind="outline", gate_id="g1", decision="approved",
                          rationale="current head")
        # The superseded revision is a stale reference -> conflict.
        stale = self.gate_prepare(pid=pid, artifact_id="art-A", revision_id="rev-A1",
                                  gate_kind="outline", gate_id="g1", decision="approved",
                                  rationale="stale", expect=3)
        self.assertEqual(stale["error"]["category"], "conflict")
        # An unknown revision is a validation failure.
        unknown = self.gate_prepare(pid=pid, artifact_id="art-A", revision_id="rev-missing",
                                    gate_kind="outline", gate_id="g1", decision="approved",
                                    rationale="unknown", expect=2)
        self.assertEqual(unknown["error"]["category"], "validation")

    def test_stale_live_dependency_is_denied(self) -> None:
        pid = self.create_project("proj")
        self.artifact_create(pid, "art-A", "document")
        self.artifact_create(pid, "art-B", "finding")
        self.import_revision(pid=pid, artifact_id="art-A", revision_id="rev-A1", text="a1")
        self.import_revision(
            pid=pid, artifact_id="art-B", revision_id="rev-B1", text="b1",
            deps=[{"project_id": pid, "artifact_id": "art-A",
                   "revision_id": "rev-A1", "role": "derived_from"}],
        )
        # B's own head is unchanged, but its live dependency A has moved.
        self.import_revision(pid=pid, artifact_id="art-A", revision_id="rev-A2",
                             text="a2", parent="rev-A1")
        stale = self.gate_prepare(pid=pid, artifact_id="art-B", revision_id="rev-B1",
                                  gate_kind="artifact_revision", gate_id="g1",
                                  decision="approved", rationale="stale dep", expect=3)
        self.assertEqual(stale["error"]["category"], "conflict")

    def test_registry_store_path_mismatch_is_denied(self) -> None:
        pid_a = self.create_project("proj-a")
        pid_b = self.create_project("proj-b")
        # Swap A's app-owned store for B's: the store's internal identity no
        # longer matches the registry location, so the CLI must refuse.
        app_a = self.tmp / "proj-a" / ".review_article_agent"
        app_b = self.tmp / "proj-b" / ".review_article_agent"
        shutil.rmtree(app_a)
        shutil.copytree(app_b, app_a)
        err = self.gate_prepare(pid=pid_a, artifact_id="art-A", revision_id="rev-1",
                                gate_kind="outline", gate_id="g1", decision="approved",
                                rationale="mismatch", expect=4)
        self.assertEqual(err["error"]["category"], "storage")
        self.assertNotIn("Traceback", json.dumps(err))

    def test_corrupt_pinned_revision_is_denied(self) -> None:
        pid = self.create_project("proj")
        self.artifact_create(pid, "art-A", "document")
        self.import_revision(pid=pid, artifact_id="art-A", revision_id="rev-A1", text="a1")
        # Test-only DB tamper: drop the immutability trigger and corrupt the
        # recorded digest so the pinned bytes no longer verify.
        conn = sqlite3.connect(self.store_path("proj"))
        try:
            conn.execute("DROP TRIGGER revision_no_update")
            conn.execute(
                "UPDATE revision SET content_sha256 = ? "
                "WHERE project_id = ? AND artifact_id = ? AND revision_id = ?",
                ("sha256:" + "0" * 64, pid, "art-A", "rev-A1"),
            )
            conn.commit()
        finally:
            conn.close()
        err = self.gate_prepare(pid=pid, artifact_id="art-A", revision_id="rev-A1",
                                gate_kind="outline", gate_id="g1", decision="approved",
                                rationale="corrupt", expect=4)
        self.assertEqual(err["error"]["category"], "storage")


@slow
class GatePrepareSafetyTest(_Base):
    def _project(self) -> str:
        pid = self.create_project("proj")
        self.artifact_create(pid, "art-A", "document")
        self.import_revision(pid=pid, artifact_id="art-A", revision_id="rev-A1", text="a1")
        return pid

    def test_saved_permission_payload_and_actor_flags_are_rejected(self) -> None:
        pid = self._project()
        bad_flags = [
            # A generic tool grant / saved permission is never a gate kind.
            ["--gate-kind", "generic_tool_permission"],
            ["--gate-kind", "saved_permission"],
            ["--decision", "granted"],
            # No raw actor/authority flag exists.
            ["--actor", "user"],
            ["--actor-authenticated", "true"],
            ["--authority", "owner_authenticated"],
            ["--owner-auth-ref", "auth_x"],
            ["--approved"],
            ["--authorizes-execution"],
            ["--approval-id", "apv_x"],
        ]
        for extra in bad_flags:
            proc = self.raw(
                "--data-dir", str(self.state), "gate", "prepare",
                "--project", pid, "--artifact-id", "art-A", "--revision-id", "rev-A1",
                "--gate-kind", "outline", "--gate-id", "g1",
                "--decision", "approved", "--rationale", "x", *extra,
            )
            self.assertNotEqual(proc.returncode, 0, extra)
            combined = proc.stdout + proc.stderr
            self.assertNotIn("Traceback", combined, extra)
            self.assertEqual(json.loads(proc.stderr)["error"]["category"], "usage", extra)

    def test_no_approve_command_exists(self) -> None:
        err = self.cli("gate", "approve", expect=2)
        self.assertEqual(err["error"]["category"], "usage")
        # A top-level approve is likewise not a command.
        err2 = self.cli("approve", expect=2)
        self.assertEqual(err2["error"]["category"], "usage")

    def test_invalid_choice_does_not_echo_secret(self) -> None:
        pid = self._project()
        proc = self.raw(
            "--data-dir", str(self.state), "gate", "prepare",
            "--project", pid, "--artifact-id", "art-A", "--revision-id", "rev-A1",
            "--gate-kind", SECRET, "--gate-id", "g1",
            "--decision", "approved", "--rationale", "x",
        )
        self.assertEqual(proc.returncode, 2)
        combined = proc.stdout + proc.stderr
        self.assertNotIn(SECRET, combined)
        self.assertNotIn("Traceback", combined)
        self.assertEqual(json.loads(proc.stderr)["error"]["category"], "usage")

    def test_unknown_argument_does_not_echo_secret(self) -> None:
        pid = self._project()
        proc = self.raw(
            "--data-dir", str(self.state), "gate", "prepare",
            "--project", pid, "--artifact-id", "art-A", "--revision-id", "rev-A1",
            "--gate-kind", "outline", "--gate-id", "g1",
            "--decision", "approved", "--rationale", "x",
            "--secret-option=" + SECRET,
        )
        self.assertEqual(proc.returncode, 2)
        combined = proc.stdout + proc.stderr
        self.assertNotIn(SECRET, combined)
        self.assertNotIn("Traceback", combined)
        self.assertEqual(json.loads(proc.stderr)["error"]["category"], "usage")

    def test_error_canary_is_not_reflected(self) -> None:
        pid = self._project()
        # A secret-looking but invalid gate id must not be echoed.
        bad_gate_id = self.gate_prepare(
            pid=pid, artifact_id="art-A", revision_id="rev-A1",
            gate_kind="outline", gate_id=f"../{SECRET}", decision="approved",
            rationale="x", expect=2,
        )
        self.assertEqual(bad_gate_id["error"]["category"], "validation")
        self.assertNotIn(SECRET, json.dumps(bad_gate_id))
        # A secret rationale on a failing prepare must not be echoed either.
        bad_rev = self.gate_prepare(
            pid=pid, artifact_id="art-A", revision_id="rev-missing",
            gate_kind="outline", gate_id="g1", decision="approved",
            rationale=SECRET, expect=2,
        )
        self.assertEqual(bad_rev["error"]["category"], "validation")
        self.assertNotIn(SECRET, json.dumps(bad_rev))


class GatePrepareDispatchTest(_Base):
    @slow
    def test_help_and_subcommand_dispatch(self) -> None:
        top = self.raw("--help")
        self.assertEqual(top.returncode, 0)
        self.assertIn("gate", top.stdout)
        gate_help = self.raw("gate", "--help")
        self.assertEqual(gate_help.returncode, 0)
        self.assertIn("prepare", gate_help.stdout)
        prep_help = self.raw("gate", "prepare", "--help")
        self.assertEqual(prep_help.returncode, 0)
        self.assertIn("--gate-kind", prep_help.stdout)

    def test_gate_without_subcommand_is_usage_error(self) -> None:
        err = self.cli("gate", expect=2)
        self.assertEqual(err["error"]["category"], "usage")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
