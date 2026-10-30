"""End-to-end acceptance tests for the ``review-article-agent`` CLI surface.

These tests drive the CLI as a **real subprocess** (``python -m
review_article_agent``) for the project/artifact acceptance slice, and also
build a wheel offline to check the packaged console entry point.

All data is synthetic: no secrets, no real project, no scientific evidence.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from _tiers import slow

SRC = Path(__file__).resolve().parents[1] / "src"
REPO = Path(__file__).resolve().parents[1]
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import review_article_agent.cli.main as cli_main  # noqa: E402

_OPS = ("acquisition", "storage", "redistribution", "hosted_transfer")


def _rights_record(project_id: str, artifact_id: str, revision_id: str) -> dict:
    """A fully valid, synthetic RightsRecord v1 bound to one exact revision."""
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


def _envelope(document_type: str = "fixture_note", **payload) -> dict:
    return {
        "schema_version": 1,
        "document_type": document_type,
        "payload": payload or {"note": "synthetic"},
    }


class CliTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="raa-cli-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.state = self.tmp / "state"

    # -- subprocess helpers -------------------------------------------------- #
    def _env(self) -> dict:
        env = dict(os.environ)
        existing = env.get("PYTHONPATH")
        env["PYTHONPATH"] = str(SRC) + (os.pathsep + existing if existing else "")
        env.pop("XDG_DATA_HOME", None)
        return env

    def raw(self, *args: str, input_text: str | None = None, module: str = "review_article_agent"):
        return subprocess.run(
            [sys.executable, "-m", module, *args],
            capture_output=True,
            text=True,
            input=input_text,
            env=self._env(),
            cwd=str(REPO),
        )

    def cli(self, *args: str, expect: int = 0) -> dict:
        proc = self.raw(*args)
        self.assertEqual(
            proc.returncode,
            expect,
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

    def import_revision(self, *, pid, artifact_id, revision_id, content, parent=None,
                        request_id=None, expect=0, extra=()):
        content_path = self.write_json(f"content-{revision_id}.json", content)
        rights_path = self.write_json(
            f"rights-{revision_id}.json", _rights_record(pid, artifact_id, revision_id)
        )
        args = [
            "--data-dir", str(self.state), "artifact", "import",
            "--artifact-id", artifact_id,
            "--revision-id", revision_id,
            "--file", str(content_path),
            "--rights-file", str(rights_path),
            "--project", pid,
        ]
        if parent is not None:
            args += ["--parent", parent]
        if request_id is not None:
            args += ["--request-id", request_id]
        args += list(extra)
        return self.cli(*args, expect=expect)

    def make_artifact_with_revision(
        self,
        *,
        root_name: str = "proj-a",
        artifact_id: str = "art-A",
        revision_id: str = "rev-1",
        content: dict | None = None,
    ) -> str:
        pid = self.create_project(root_name)
        self.cli("--data-dir", str(self.state), "artifact", "create",
                 "--artifact-id", artifact_id, "--kind", "document", "--project", pid)
        self.import_revision(pid=pid, artifact_id=artifact_id, revision_id=revision_id,
                             content=content or _envelope())
        return pid


class EntryPointTest(CliTestCase):
    def test_module_help_exits_zero(self) -> None:
        proc = self.raw("--help")
        self.assertEqual(proc.returncode, 0)
        self.assertIn("review-article-agent", proc.stdout)
        self.assertIn("artifact", proc.stdout)

    def test_cli_module_help_exits_zero(self) -> None:
        proc = self.raw("--help", module="review_article_agent.cli")
        self.assertEqual(proc.returncode, 0)

    def test_version_exits_zero(self) -> None:
        proc = self.raw("--version")
        self.assertEqual(proc.returncode, 0)
        self.assertIn("review-article-agent", proc.stdout)

    def test_no_command_is_usage_error(self) -> None:
        err = self.cli(expect=2)
        self.assertEqual(err["error"]["category"], "usage")


@slow
class ProjectFlowTest(CliTestCase):
    def test_two_projects_recover_across_restart_and_stay_independent(self) -> None:
        pid_a = self.create_project("proj-a")
        pid_b = self.create_project("proj-b")
        self.assertNotEqual(pid_a, pid_b)

        # A fresh subprocess ("restart") still sees both projects.
        listing = self.cli("--data-dir", str(self.state), "list")
        self.assertEqual(
            {p["project_id"] for p in listing["projects"]}, {pid_a, pid_b}
        )

        # Distinct identities are independent stores (non-Git roots).
        for pid, root in ((pid_a, "proj-a"), (pid_b, "proj-b")):
            status = self.cli("--data-dir", str(self.state), "status", "--project", pid)
            self.assertEqual(status["project_id"], pid)
            self.assertFalse(status["running"])
            self.assertFalse(status["scientific_validation"])
            self.assertFalse((self.tmp / root / ".git").exists())

    def test_select_is_persisted_and_is_not_execution(self) -> None:
        pid = self.create_project("proj-a")
        self.cli("--data-dir", str(self.state), "select", pid)
        listing = self.cli("--data-dir", str(self.state), "list")
        self.assertEqual(listing["active_project_id"], pid)
        global_status = self.cli("--data-dir", str(self.state), "status")
        self.assertEqual(global_status["active_project_id"], pid)
        self.assertFalse(global_status["running"])
        self.assertFalse(global_status["scientific_validation"])


@slow
class ArtifactFlowTest(CliTestCase):
    def test_artifacts_are_isolated_per_project(self) -> None:
        pid_a = self.create_project("proj-a")
        pid_b = self.create_project("proj-b")
        self.cli("--data-dir", str(self.state), "artifact", "create",
                 "--artifact-id", "art-shared", "--kind", "document", "--project", pid_a)
        self.cli("--data-dir", str(self.state), "artifact", "create",
                 "--artifact-id", "art-shared", "--kind", "report", "--project", pid_b)

        list_a = self.cli("--data-dir", str(self.state), "artifact", "list", "--project", pid_a)
        list_b = self.cli("--data-dir", str(self.state), "artifact", "list", "--project", pid_b)
        self.assertEqual([a["artifact_id"] for a in list_a["artifacts"]], ["art-shared"])
        self.assertEqual(list_a["artifacts"][0]["kind"], "document")
        self.assertEqual(list_b["artifacts"][0]["kind"], "report")

    def test_import_and_read_content_round_trip(self) -> None:
        pid = self.create_project("proj-a")
        self.cli("--data-dir", str(self.state), "artifact", "create",
                 "--artifact-id", "art-A", "--kind", "document", "--project", pid)
        out = self.import_revision(
            pid=pid, artifact_id="art-A", revision_id="rev-1", content=_envelope()
        )
        self.assertEqual(out["revision"]["revision_id"], "rev-1")
        self.assertIsNone(out["revision"]["parent_revision_id"])

        meta = self.cli("--data-dir", str(self.state), "artifact", "get", "art-A", "--project", pid)
        self.assertEqual(meta["current_revision"]["revision_id"], "rev-1")
        # Safe metadata only: the payload text must not appear.
        self.assertNotIn("synthetic", json.dumps(meta))

        content = self.raw("--data-dir", str(self.state), "artifact", "content",
                           "art-A", "--revision", "rev-1", "--project", pid)
        self.assertEqual(content.returncode, 0)
        self.assertIn("synthetic", content.stdout)

    def test_invalid_edit_cannot_overwrite_current_revision(self) -> None:
        pid = self.create_project("proj-a")
        self.cli("--data-dir", str(self.state), "artifact", "create",
                 "--artifact-id", "art-A", "--kind", "document", "--project", pid)
        self.import_revision(pid=pid, artifact_id="art-A", revision_id="rev-1",
                             content=_envelope(note="first"))

        # A stale parent (rev-1 does not exist as an earlier head here) is refused.
        self.import_revision(pid=pid, artifact_id="art-A", revision_id="rev-bad",
                             content=_envelope(note="stale"), parent="rev-missing", expect=2)

        # An envelope with an unknown field must not be importable (no self-grant).
        bad = _envelope(note="x")
        bad["approved"] = True
        self.import_revision(pid=pid, artifact_id="art-A", revision_id="rev-bad2",
                             content=bad, parent="rev-1", expect=2)

        current = self.cli("--data-dir", str(self.state), "artifact", "get", "art-A", "--project", pid)
        self.assertEqual(current["current_revision"]["revision_id"], "rev-1")

    def test_duplicate_import_is_stable_and_divergence_conflicts(self) -> None:
        pid = self.create_project("proj-a")
        self.cli("--data-dir", str(self.state), "artifact", "create",
                 "--artifact-id", "art-A", "--kind", "document", "--project", pid)

        first = self.import_revision(pid=pid, artifact_id="art-A", revision_id="rev-1",
                                     content=_envelope(note="same"), request_id="req-1")
        second = self.import_revision(pid=pid, artifact_id="art-A", revision_id="rev-1",
                                      content=_envelope(note="same"), request_id="req-1")
        self.assertEqual(first["revision"]["content_sha256"], second["revision"]["content_sha256"])

        # Reusing req-1 with different content must be a non-success conflict.
        err = self.import_revision(pid=pid, artifact_id="art-A", revision_id="rev-1",
                                   content=_envelope(note="different"), request_id="req-1",
                                   expect=3)
        self.assertEqual(err["error"]["category"], "conflict")


class DenialTest(CliTestCase):
    def test_project_id_traversal_is_denied(self) -> None:
        err = self.cli("--data-dir", str(self.state), "select", "../../etc/passwd", expect=2)
        self.assertEqual(err["error"]["category"], "validation")

    def test_unknown_project_id_is_a_clean_error(self) -> None:
        err = self.cli("--data-dir", str(self.state), "status",
                       "--project", "prj_" + "0" * 32, expect=2)
        self.assertEqual(err["error"]["category"], "not_found")

    @slow
    def test_registry_store_mismatch_is_denied(self) -> None:
        pid_a = self.create_project("proj-a")
        pid_b = self.create_project("proj-b")
        # Swap A's app-owned store for B's: the store's internal identity no
        # longer matches the registry location, so the CLI must refuse.
        app_a = self.tmp / "proj-a" / ".review_article_agent"
        app_b = self.tmp / "proj-b" / ".review_article_agent"
        shutil.rmtree(app_a)
        shutil.copytree(app_b, app_a)
        err = self.cli("--data-dir", str(self.state), "status", "--project", pid_a, expect=4)
        self.assertEqual(err["error"]["category"], "storage")
        self.assertNotIn("Traceback", json.dumps(err))


@slow
class ConfigCliTest(CliTestCase):
    def test_invalid_config_is_nonzero_without_echoing_secret(self) -> None:
        secret = "SUPER_SECRET_VALUE_DO_NOT_ECHO"
        path = self.tmp / "c.json"
        path.write_text(json.dumps({"schema_version": 1, "api_key": secret}), encoding="utf-8")
        proc = self.raw("--config", str(path), "list")
        self.assertEqual(proc.returncode, 2)
        combined = proc.stdout + proc.stderr
        self.assertNotIn(secret, combined)
        self.assertNotIn("Traceback", combined)
        err = json.loads(proc.stderr)
        self.assertEqual(err["error"]["category"], "usage")

    def test_newer_config_version_is_rejected(self) -> None:
        path = self.tmp / "c.json"
        path.write_text(json.dumps({"schema_version": 2}), encoding="utf-8")
        err = self.cli("--config", str(path), "list", expect=2)
        self.assertIn("newer", err["error"]["message"])

    def test_config_data_dir_is_used_when_no_explicit_flag(self) -> None:
        pid = self.create_project("proj-a")
        path = self.tmp / "c.json"
        path.write_text(
            json.dumps({"schema_version": 1, "data_dir": str(self.state)}), encoding="utf-8"
        )
        out = self.cli("--config", str(path), "list")
        self.assertEqual([p["project_id"] for p in out["projects"]], [pid])

    def test_explicit_data_dir_overrides_config(self) -> None:
        pid = self.create_project("proj-a")
        other = self.tmp / "other-state"
        path = self.tmp / "c.json"
        path.write_text(
            json.dumps({"schema_version": 1, "data_dir": str(other)}), encoding="utf-8"
        )
        out = self.cli("--data-dir", str(self.state), "--config", str(path), "list")
        self.assertEqual([p["project_id"] for p in out["projects"]], [pid])


class WheelPackagingTest(unittest.TestCase):
    """Build a wheel offline and inspect its metadata (no install, no network)."""

    @slow
    def test_wheel_metadata_has_console_script_and_no_invented_license(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="raa-wheel-")
        self.addCleanup(tmp.cleanup)
        build_root = Path(tmp.name) / "build-root"
        build_root.mkdir()
        shutil.copy(REPO / "pyproject.toml", build_root / "pyproject.toml")
        shutil.copy(REPO / "README.md", build_root / "README.md")
        shutil.copytree(SRC, build_root / "src")
        (build_root / "wheelhouse").mkdir()

        script = (
            "import setuptools.build_meta as b;"
            "print(b.build_wheel('wheelhouse'))"
        )
        proc = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            cwd=str(build_root),
        )
        if proc.returncode != 0 and "No module named 'setuptools'" in proc.stderr:
            # The build backend is a declared build requirement of this
            # project (pyproject.toml: requires = ["setuptools>=68"]). This is
            # a real coverage failure, not something to skip: report exactly
            # how to fix the environment.
            self.fail(
                "the build backend 'setuptools' is not importable in this "
                "interpreter, so the wheel cannot be built. Install it, e.g. "
                "`python -m pip install \"setuptools>=68\"` (CI installs it "
                "right after setup-python). "
                f"stderr={proc.stderr}"
            )
        self.assertEqual(proc.returncode, 0, f"stderr={proc.stderr}")
        wheels = list((build_root / "wheelhouse").glob("*.whl"))
        self.assertEqual(len(wheels), 1, "expected exactly one wheel")

        with zipfile.ZipFile(wheels[0]) as archive:
            names = archive.namelist()
            entry_points_name = next(
                n for n in names if n.endswith(".dist-info/entry_points.txt")
            )
            metadata_name = next(n for n in names if n.endswith(".dist-info/METADATA"))
            entry_points = archive.read(entry_points_name).decode("utf-8")
            metadata = archive.read(metadata_name).decode("utf-8")
            self.assertIn("[console_scripts]", entry_points)
            self.assertIn(
                "review-article-agent = review_article_agent.cli.main:main", entry_points
            )
            self.assertNotIn("Proprietary", metadata)
            self.assertNotIn("\nLicense:", metadata)


class LeakBoundaryTest(CliTestCase):
    """Untrusted input must never be reflected in an error message."""

    SECRET = "SYNTHETIC-SECRET-NO-REAL-CREDENTIAL"

    @slow
    def test_rights_record_secret_is_not_echoed(self) -> None:
        pid = self.create_project("proj-a")
        self.cli("--data-dir", str(self.state), "artifact", "create",
                 "--artifact-id", "art-A", "--kind", "document", "--project", pid)
        # An otherwise valid record whose operation status is the secret.
        bad = _rights_record(pid, "art-A", "rev-1")
        bad["operations"]["storage"] = self.SECRET
        rights_path = self.write_json("rights-bad.json", bad)
        content_path = self.write_json("content-1.json", _envelope())
        proc = self.raw(
            "--data-dir", str(self.state), "artifact", "import",
            "--artifact-id", "art-A", "--revision-id", "rev-1",
            "--file", str(content_path), "--rights-file", str(rights_path),
            "--project", pid,
        )
        self.assertNotEqual(proc.returncode, 0)
        combined = proc.stdout + proc.stderr
        self.assertNotIn(self.SECRET, combined)
        self.assertNotIn("Traceback", combined)
        self.assertEqual(json.loads(proc.stderr)["error"]["category"], "validation")

    def test_invalid_choice_does_not_echo_secret(self) -> None:
        proc = self.raw("--data-dir", str(self.state), "artifact", "create",
                        "--artifact-id", "art-A", "--kind", self.SECRET)
        self.assertEqual(proc.returncode, 2)
        combined = proc.stdout + proc.stderr
        self.assertNotIn(self.SECRET, combined)
        self.assertNotIn("Traceback", combined)
        self.assertEqual(json.loads(proc.stderr)["error"]["category"], "usage")

    def test_unknown_argument_does_not_echo_secret(self) -> None:
        proc = self.raw("--data-dir", str(self.state), "--secret-option=" + self.SECRET, "list")
        self.assertEqual(proc.returncode, 2)
        combined = proc.stdout + proc.stderr
        self.assertNotIn(self.SECRET, combined)
        self.assertEqual(json.loads(proc.stderr)["error"]["category"], "usage")

    def test_unknown_command_does_not_echo_secret(self) -> None:
        proc = self.raw(self.SECRET)
        self.assertEqual(proc.returncode, 2)
        self.assertNotIn(self.SECRET, proc.stdout + proc.stderr)

    def test_unknown_config_field_name_is_not_echoed(self) -> None:
        secret_name = "SYNTHETIC-SECRET-FIELD-NAME"
        path = self.tmp / "c.json"
        path.write_text(json.dumps({"schema_version": 1, secret_name: "x"}), encoding="utf-8")
        proc = self.raw("--config", str(path), "list")
        self.assertEqual(proc.returncode, 2)
        combined = proc.stdout + proc.stderr
        self.assertNotIn(secret_name, combined)
        self.assertNotIn("Traceback", combined)


@slow
class StrictJsonCliTest(CliTestCase):
    """Duplicate keys / non-finite / over-deep input fail cleanly (no payload)."""

    def _import(self, content_path: Path, rights_path: Path, *, extra=()):
        return self.raw(
            "--data-dir", str(self.state), "artifact", "import",
            "--artifact-id", "art-A", "--revision-id", "rev-1",
            "--file", str(content_path), "--rights-file", str(rights_path),
            *extra,
        )

    def test_duplicate_keys_rejected_for_content_rights_and_deps(self) -> None:
        pid = self.create_project("proj-a")
        self.cli("--data-dir", str(self.state), "artifact", "create",
                 "--artifact-id", "art-A", "--kind", "document", "--project", pid)
        rights = self.write_json("rights.json", _rights_record(pid, "art-A", "rev-1"))
        good_content = self.write_json("content-ok.json", _envelope())

        dup_content = self.tmp / "content-dup.json"
        dup_content.write_text(
            '{"schema_version":1,"document_type":"n","payload":{"a":1},"payload":{"b":2}}',
            encoding="utf-8",
        )
        dup_rights = self.tmp / "rights-dup.json"
        rec = json.dumps(_rights_record(pid, "art-A", "rev-1"))
        dup_rights.write_text(rec[:-1] + ',"record_revision":1}', encoding="utf-8")
        dup_deps = self.tmp / "deps-dup.json"
        dup_deps.write_text(
            '[{"project_id":"x","project_id":"x","artifact_id":"art-A","revision_id":"rev-1"}]',
            encoding="utf-8",
        )

        for proc in (
            self._import(dup_content, rights),
            self._import(good_content, dup_rights),
            self._import(good_content, rights, extra=["--deps-file", str(dup_deps)]),
        ):
            self.assertEqual(proc.returncode, 2)
            self.assertNotIn("Traceback", proc.stdout + proc.stderr)
            self.assertEqual(json.loads(proc.stderr)["error"]["category"], "validation")

    def test_nonfinite_surrogate_and_deep_content_are_stable(self) -> None:
        pid = self.create_project("proj-a")
        self.cli("--data-dir", str(self.state), "artifact", "create",
                 "--artifact-id", "art-A", "--kind", "document", "--project", pid)
        rights = self.write_json("rights.json", _rights_record(pid, "art-A", "rev-1"))

        cases = {
            "nonfinite.json": '{"schema_version":1,"document_type":"n","payload":{"a":NaN}}',
            "surrogate.json": '{"schema_version":1,"document_type":"n","payload":{"a":"\\ud800"}}',
            "deep.json": "[" * 300 + "]" * 300,
        }
        for name, text in cases.items():
            path = self.tmp / name
            path.write_text(text, encoding="utf-8")
            proc = self._import(path, rights)
            self.assertEqual(proc.returncode, 2, name)
            combined = proc.stdout + proc.stderr
            self.assertNotIn("Traceback", combined)
            self.assertEqual(json.loads(proc.stderr)["error"]["category"], "validation", name)


@slow
class OutputProtectionTest(CliTestCase):
    """``artifact content --output`` creates exclusively and never clobbers state."""

    def setUp(self) -> None:
        super().setUp()
        self.pid = self.make_artifact_with_revision()

    def content_args(self, out: Path):
        return ("--data-dir", str(self.state), "artifact", "content",
                "art-A", "--revision", "rev-1", "--project", self.pid,
                "--output", str(out))

    def test_new_output_is_owner_private(self) -> None:
        out = self.tmp / "export.bin"
        result = self.cli(*self.content_args(out))
        self.assertEqual(result["byte_size"], out.stat().st_size)
        self.assertEqual(out.stat().st_mode & 0o777, 0o600)

    def test_existing_file_is_not_overwritten(self) -> None:
        out = self.tmp / "existing.bin"
        out.write_bytes(b"USER-DATA")
        proc = self.raw(*self.content_args(out))
        self.assertNotEqual(proc.returncode, 0)
        self.assertNotIn("Traceback", proc.stdout + proc.stderr)
        self.assertEqual(out.read_bytes(), b"USER-DATA")

    def test_symlink_target_is_unchanged(self) -> None:
        target = self.tmp / "target.bin"
        target.write_bytes(b"ORIGINAL")
        link = self.tmp / "link.bin"
        os.symlink(target, link)
        proc = self.raw(*self.content_args(link))
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(target.read_bytes(), b"ORIGINAL")
        self.assertTrue(link.is_symlink())

    def test_output_inside_data_dir_is_rejected(self) -> None:
        self.state.mkdir(parents=True, exist_ok=True)
        proc = self.raw(*self.content_args(self.state / "evil.bin"))
        self.assertNotEqual(proc.returncode, 0)
        self.assertFalse((self.state / "evil.bin").exists())

    def test_output_inside_app_dir_is_rejected_and_store_unchanged(self) -> None:
        db = self.tmp / "proj-a" / ".review_article_agent" / "store.sqlite3"
        before = hashlib.sha256(db.read_bytes()).hexdigest()
        proc = self.raw(*self.content_args(db))
        self.assertNotEqual(proc.returncode, 0)
        after = hashlib.sha256(db.read_bytes()).hexdigest()
        self.assertEqual(before, after)


class OutputEmitUnicodeTest(unittest.TestCase):
    """Malformed Unicode in a result/error must not raise a raw traceback."""

    def test_emit_result_escapes_surrogate(self) -> None:
        buf = io.StringIO()
        cli_main._emit_result({"label": "x\ud800y"}, buf)
        self.assertIn("\\ud800", buf.getvalue())

    def test_emit_error_escapes_surrogate(self) -> None:
        buf = io.StringIO()
        code = cli_main._emit_error(
            cli_main.CliUsageError("bad \ud800 value"), buf
        )
        self.assertEqual(code, cli_main.EXIT_USAGE)
        self.assertIn("\\ud800", buf.getvalue())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
