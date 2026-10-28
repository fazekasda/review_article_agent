"""Acceptance tests for the Step 02 CLI backup/export and local fixture surface.

Drives the CLI as a **real subprocess** (``python -m review_article_agent``):

* private storage snapshot: create/verify/restore (incl. capturing committed WAL
  content written by another process), the ``export`` alias, and restore into a
  **separate** data directory (the registry must not clone an existing identity);
* non-authorizing local storage-fixture review state: dependent artifacts, a
  fixture approval that goes `stale` when its upstream is edited while an
  independent approval stays `active`, immutable old content, and the
  ``proposal`` revision state;
* safety: corrupt archive, same-project restore conflict, fixture commands reject
  real/self-authority, no ``run``/``worker``/``model``/``scientific approve``
  commands, stable JSON, and no secret/traceback in errors.

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

SRC = Path(__file__).resolve().parents[1] / "src"
REPO = Path(__file__).resolve().parents[1]
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from review_article_agent.storage import ProjectStore, paths  # noqa: E402

_OPS = ("acquisition", "storage", "redistribution", "hosted_transfer")


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
        self._tmp = tempfile.TemporaryDirectory(prefix="raa-cli-snap-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.state = self.tmp / "state"

    def _env(self) -> dict:
        env = dict(os.environ)
        existing = env.get("PYTHONPATH")
        env["PYTHONPATH"] = str(SRC) + (os.pathsep + existing if existing else "")
        env.pop("XDG_DATA_HOME", None)
        return env

    def raw(self, *args: str):
        return subprocess.run(
            [sys.executable, "-m", "review_article_agent", *args],
            capture_output=True, text=True, env=self._env(), cwd=str(REPO),
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

    def create_project(self, name: str, *, state: Path | None = None) -> str:
        state = self.state if state is None else state
        out = self.cli("--data-dir", str(state), "create", str(self.tmp / name), "--name", name)
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

    def fixture_record(self, *, pid, artifact_id, revision_id, gate_id, decision,
                       rationale, extra=()):
        return self.cli(
            "--data-dir", str(self.state), "fixture", "record-approval",
            "--artifact-id", artifact_id, "--revision-id", revision_id,
            "--gate-id", gate_id, "--decision", decision,
            "--rationale", rationale, "--project", pid, *extra,
        )


class FixtureLifecycleTest(_Base):
    def _setup(self) -> str:
        pid = self.create_project("proj")
        self.cli("--data-dir", str(self.state), "select", pid)
        self.artifact_create(pid, "art-A", "document")
        self.artifact_create(pid, "art-B", "finding")
        self.artifact_create(pid, "art-C", "report")
        self.import_revision(pid=pid, artifact_id="art-A", revision_id="rev-A1", text="a1")
        self.import_revision(
            pid=pid, artifact_id="art-B", revision_id="rev-B1", text="b1",
            deps=[{"project_id": pid, "artifact_id": "art-A",
                   "revision_id": "rev-A1", "role": "derived_from"}],
        )
        self.import_revision(pid=pid, artifact_id="art-C", revision_id="rev-C1", text="c1")
        return pid

    def test_fixture_approval_stales_on_upstream_edit_but_independent_stays_active(self) -> None:
        pid = self._setup()
        a = self.fixture_record(pid=pid, artifact_id="art-A", revision_id="rev-A1",
                                gate_id="gA", decision="approved", rationale="synth A")
        b = self.fixture_record(pid=pid, artifact_id="art-B", revision_id="rev-B1",
                                gate_id="gB", decision="approved", rationale="synth B")
        c = self.fixture_record(pid=pid, artifact_id="art-C", revision_id="rev-C1",
                                gate_id="gC", decision="approved", rationale="synth C")
        for out in (a, b, c):
            flags = out["fixture_approval"]
            self.assertEqual(flags["scope"], "storage_fixture")
            self.assertFalse(flags["actor_authenticated"])
            self.assertFalse(flags["authorizes_execution"])
            self.assertFalse(flags["authorizes_export"])
            self.assertFalse(flags["scientific_validation"])

        approvals = {x["gate_id"]: x for x in
                     self.cli("--data-dir", str(self.state), "fixture", "list", "--project", pid)["fixture_approvals"]}
        self.assertEqual(approvals["gA"]["status"], "active")
        self.assertEqual(approvals["gB"]["status"], "active")
        self.assertEqual(approvals["gC"]["status"], "active")

        # Edit the upstream artifact A using its current head as parent.
        self.import_revision(pid=pid, artifact_id="art-A", revision_id="rev-A2",
                             text="a2", parent="rev-A1")

        list_out = self.cli("--data-dir", str(self.state), "fixture", "list", "--project", pid)
        statuses = {x["gate_id"]: x["status"] for x in list_out["fixture_approvals"]}
        self.assertEqual(statuses["gA"], "stale")   # target no longer current
        self.assertEqual(statuses["gB"], "stale")   # live dependency changed
        self.assertEqual(statuses["gC"], "active")  # independent -> unaffected

        # A fixture status shows the staleness and that the current revision is
        # no longer the approved one, without authorizing anything.
        status_a = self.cli("--data-dir", str(self.state), "fixture", "status",
                            a["fixture_approval"]["approval_id"], "--project", pid)["fixture_approval"]
        self.assertEqual(status_a["status"], "stale")
        self.assertFalse(status_a["current_revision_approved"])
        self.assertEqual(status_a["current_revision_id"], "rev-A2")
        self.assertFalse(status_a["authorizes_execution"])
        self.assertFalse(status_a["authorizes_export"])

        # Immutable old content is preserved (rev-A1 still byte-identical).
        content = self.raw("--data-dir", str(self.state), "artifact", "content",
                           "art-A", "--revision", "rev-A1", "--project", pid)
        self.assertEqual(content.returncode, 0)
        self.assertIn("a1", content.stdout)

        # The new revision is visible as an unvalidated proposal, not a science state.
        got = self.cli("--data-dir", str(self.state), "artifact", "get", "art-A", "--project", pid)
        self.assertEqual(got["current_revision"]["revision_id"], "rev-A2")
        self.assertEqual(got["current_revision"]["scientific_state"], "proposal")

    def test_rationale_is_not_echoed_in_status_or_list(self) -> None:
        pid = self._setup()
        secret_rationale = "SYNTHETIC-RATIONALE-SECRET-NO-REAL-CREDENTIAL"
        rec = self.fixture_record(pid=pid, artifact_id="art-A", revision_id="rev-A1",
                                  gate_id="gA", decision="approved", rationale=secret_rationale)
        approval_id = rec["fixture_approval"]["approval_id"]
        for out in (
            self.cli("--data-dir", str(self.state), "fixture", "status", approval_id, "--project", pid),
            self.cli("--data-dir", str(self.state), "fixture", "list", "--project", pid),
        ):
            self.assertNotIn(secret_rationale, json.dumps(out))


class FixtureSafetyTest(_Base):
    def _project(self) -> str:
        pid = self.create_project("proj")
        self.artifact_create(pid, "art-A", "document")
        self.import_revision(pid=pid, artifact_id="art-A", revision_id="rev-1")
        return pid

    def test_self_authority_and_real_gate_flags_are_rejected(self) -> None:
        pid = self._project()
        bad_flags = [
            ["--gate-kind", "protocol"],
            ["--actor", "user"],
            ["--actor-authenticated", "true"],
            ["--authorization", "yes"],
            ["--authorizes-execution"],
            ["--decision", "protocol"],
        ]
        for extra in bad_flags:
            proc = self.raw(
                "--data-dir", str(self.state), "fixture", "record-approval",
                "--artifact-id", "art-A", "--revision-id", "rev-1",
                "--gate-id", "g1", "--decision", "approved", "--rationale", "x",
                "--project", pid, *extra,
            )
            self.assertNotEqual(proc.returncode, 0, extra)
            self.assertNotIn("Traceback", proc.stdout + proc.stderr)
            self.assertEqual(json.loads(proc.stderr)["error"]["category"], "usage", extra)

    def test_no_execution_or_scientific_approval_commands(self) -> None:
        for name in ("run", "worker", "model", "approve", "scientific-approve",
                     "pause", "resume"):
            err = self.cli(name, expect=2)
            self.assertEqual(err["error"]["category"], "usage", name)
        help_text = self.raw("--help").stdout
        self.assertIn("fixture", help_text)
        self.assertIn("backup", help_text)


class BackupRoundTripTest(_Base):
    def _project_with_approval(self) -> str:
        pid = self.create_project("proj-a")
        self.cli("--data-dir", str(self.state), "select", pid)
        self.artifact_create(pid, "art-A", "document")
        self.import_revision(pid=pid, artifact_id="art-A", revision_id="rev-1", text="one")
        self.fixture_record(pid=pid, artifact_id="art-A", revision_id="rev-1",
                            gate_id="gA", decision="approved", rationale="synth")
        return pid

    def _hold_wal_writer(self, *, root: Path):
        """Start a subprocess that commits a WAL write and holds the store open."""
        code = (
            "import sys\n"
            f"sys.path.insert(0, {str(SRC)!r})\n"
            "from review_article_agent.storage import ProjectStore, paths\n"
            f"store = ProjectStore.open_at(paths.app_dir_for({str(root)!r}))\n"
            "store.connection.execute(\"INSERT OR REPLACE INTO meta (key, value) "
            "VALUES ('wal-marker', 'captured')\")\n"
            "store.connection.commit()\n"
            "print('ready', flush=True)\n"
            "sys.stdin.readline()\n"
            "store.close()\n"
        )
        proc = subprocess.Popen(
            [sys.executable, "-c", code], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, text=True, env=self._env(), cwd=str(REPO),
        )

        def _stop_writer() -> None:
            try:
                if proc.stdin is not None:
                    proc.stdin.close()
            except OSError:
                pass
            try:
                if proc.stdout is not None:
                    proc.stdout.close()
            except OSError:
                pass
            try:
                proc.wait(timeout=30)
            except Exception:  # noqa: BLE001 - best-effort teardown
                proc.kill()
                proc.wait(timeout=30)

        self.addCleanup(_stop_writer)
        self.assertIsNotNone(proc.stdout)
        line = proc.stdout.readline().strip()
        self.assertEqual(line, "ready")
        return proc

    def test_backup_captures_wal_and_restores_into_separate_registry(self) -> None:
        pid = self._project_with_approval()
        root = self.tmp / "proj-a"
        dest = self.tmp / "bk"
        dest2 = self.tmp / "bk-export"
        state2 = self.tmp / "state2"
        root2 = self.tmp / "restored"

        self._hold_wal_writer(root=root)
        db = paths.store_path_for(str(root))
        self.assertTrue(os.path.exists(db + "-wal"), "expected a live WAL sidecar")

        created = self.cli("--data-dir", str(self.state), "backup", "create",
                           str(dest), "--project", pid)
        self.assertEqual(created["backup"]["project_id"], pid)
        self.assertFalse(created["publishable"])

        verified = self.cli("--data-dir", str(self.state), "backup", "verify", str(dest))
        self.assertTrue(verified["backup"]["ok"])
        self.assertEqual(set(os.listdir(dest)), {"manifest.json", "snapshot.sqlite3"})

        # The committed WAL write made it into the snapshot database.
        conn = sqlite3.connect(f"file:{dest / 'snapshot.sqlite3'}?mode=ro", uri=True)
        try:
            row = conn.execute(
                "SELECT value FROM meta WHERE key = 'wal-marker'"
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(row[0], "captured")

        # ``export`` alias produces the same private format.
        exported = self.cli("--data-dir", str(self.state), "export", str(dest2), "--project", pid)
        self.assertEqual(exported["backup"]["format_version"], created["backup"]["format_version"])
        self.assertTrue(self.cli("--data-dir", str(self.state), "backup", "verify", str(dest2))["backup"]["ok"])

        # Restore into a fresh root + a SEPARATE data directory.
        restored = self.cli("--data-dir", str(state2), "backup", "restore", str(dest), str(root2))
        self.assertEqual(restored["project"]["project_id"], pid)

        # Exact IDs and content preserved.
        status2 = self.cli("--data-dir", str(state2), "status", "--project", pid)
        self.assertEqual(status2["project_id"], pid)
        self.assertFalse(status2["running"])
        self.assertFalse(status2["scientific_validation"])
        got = self.cli("--data-dir", str(state2), "artifact", "get", "art-A", "--project", pid)
        self.assertEqual(got["current_revision"]["revision_id"], "rev-1")
        content = self.raw("--data-dir", str(state2), "artifact", "content",
                           "art-A", "--revision", "rev-1", "--project", pid)
        self.assertIn("one", content.stdout)

        # Fixture history is preserved with the exact same approval id.
        appr = self.cli("--data-dir", str(state2), "fixture", "list", "--project", pid)
        self.assertEqual(len(appr["fixture_approvals"]), 1)
        restored_approval_id = appr["fixture_approvals"][0]["approval_id"]
        self.assertEqual(restored_approval_id, self._approval_id(pid))
        # The restored store carries the WAL-committed marker too.
        with ProjectStore.open_at(paths.app_dir_for(str(root2)), expected_project_id=pid) as store:
            self.assertEqual(store.metadata().get("wal-marker"), "captured")

    def _approval_id(self, pid: str) -> str:
        out = self.cli("--data-dir", str(self.state), "fixture", "list", "--project", pid)
        return out["fixture_approvals"][0]["approval_id"]

    def test_export_verify_is_deterministic(self) -> None:
        pid = self._project_with_approval()
        dest = self.tmp / "bk"
        self.cli("--data-dir", str(self.state), "backup", "create", str(dest), "--project", pid)
        first = self.raw("--data-dir", str(self.state), "backup", "verify", str(dest)).stdout
        second = self.raw("--data-dir", str(self.state), "backup", "verify", str(dest)).stdout
        self.assertEqual(first, second)


class BackupSafetyTest(_Base):
    def _backup(self) -> tuple[str, Path]:
        pid = self.create_project("proj-a")
        self.cli("--data-dir", str(self.state), "select", pid)
        self.artifact_create(pid, "art-A", "document")
        self.import_revision(pid=pid, artifact_id="art-A", revision_id="rev-1",
                             text="SYNTHETIC-VERIFY-SECRET-NO-REAL-CREDENTIAL")
        dest = self.tmp / "bk"
        self.cli("--data-dir", str(self.state), "backup", "create", str(dest), "--project", pid)
        return pid, dest

    def test_corrupt_archive_fails_closed_without_traceback_or_secret(self) -> None:
        _pid, dest = self._backup()
        corrupt = self.tmp / "corrupt"
        shutil.copytree(dest, corrupt)
        snapshot = corrupt / "snapshot.sqlite3"
        data = bytearray(snapshot.read_bytes())
        data[len(data) // 2] ^= 0xFF
        snapshot.write_bytes(bytes(data))

        proc = self.raw("--data-dir", str(self.state), "backup", "verify", str(corrupt))
        self.assertNotEqual(proc.returncode, 0)
        combined = proc.stdout + proc.stderr
        self.assertNotIn("Traceback", combined)
        self.assertNotIn("SYNTHETIC-VERIFY-SECRET-NO-REAL-CREDENTIAL", combined)
        self.assertEqual(json.loads(proc.stderr)["error"]["category"], "storage")

    def test_restore_same_project_conflicts(self) -> None:
        pid, dest = self._backup()
        state2 = self.tmp / "state2"
        self.cli("--data-dir", str(state2), "backup", "restore", str(dest), str(self.tmp / "r1"))
        err = self.cli("--data-dir", str(state2), "backup", "restore", str(dest),
                       str(self.tmp / "r2"), expect=3)
        self.assertEqual(err["error"]["category"], "conflict")

    def test_backup_create_refuses_existing_destination(self) -> None:
        pid, dest = self._backup()
        err = self.cli("--data-dir", str(self.state), "backup", "create", str(dest),
                       "--project", pid, expect=4)
        self.assertEqual(err["error"]["category"], "storage")

    def test_backup_create_refuses_destination_inside_data_dir(self) -> None:
        pid, _dest = self._backup()
        inside = self.state / "snapshot-out"
        err = self.cli("--data-dir", str(self.state), "backup", "create", str(inside),
                       "--project", pid, expect=4)
        self.assertEqual(err["error"]["category"], "storage")
        self.assertFalse(inside.exists())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
