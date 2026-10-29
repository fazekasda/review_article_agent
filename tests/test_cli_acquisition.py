"""In-process CLI acceptance tests for the ``acquisition`` group (Step 06, C1).

Every case drives the real entry point (``cli.main.main`` with in-memory streams)
over a real temporary registry and project store and the integrated acquisition
value layers. Synthetic data only: no network, no model call, no approval.

Run::

    PYTHONPATH=src python3 -m unittest discover -s tests -p test_cli_acquisition.py
"""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from review_article_agent.acquisition import pdf_inspection  # noqa: E402
from review_article_agent.cli import main as cli_main  # noqa: E402
from review_article_agent.storage import (  # noqa: E402
    ArtifactRepository,
    ProjectStore,
    paths,
)

_OPS = ("acquisition", "storage", "redistribution", "hosted_transfer")
_SECRET = "SYNTHETIC-SECRET-DO-NOT-ECHO"
_DOI = "10.1234/synthetic.doi.donotecho"
_BYTES = b"synthetic opaque source bytes " + _SECRET.encode()
_HTML = b"<!DOCTYPE html><html><body>Please log in</body></html>"
_PDF = b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\nendobj\ntrailer\n<< /Root 1 0 R >>\n%%EOF\n"


def rights(project_id, artifact_id, revision_id, rights_id=None, **ops_override):
    ops = {op: "allowed" for op in _OPS} | ops_override
    return {
        "rights_id": rights_id or f"RIGHTS-{revision_id}",
        "project_id": project_id,
        "record_revision": 1,
        "subject": {
            "kind": "source_revision",
            "project_id": project_id,
            "artifact_id": artifact_id,
            "revision_id": revision_id,
        },
        "operations": ops,
        "basis": {
            op: (
                {
                    "kind": "public_domain",
                    "evidence": {"ref": "lic:pd-syn", "provenance": "publisher:pd-list"},
                }
                if ops[op] == "allowed"
                else {"kind": "unknown", "evidence": {"ref": "", "provenance": ""}}
            )
            for op in _OPS
        },
        "authorization": None,
        "restrictions": [],
        "privacy": {"personal_data": False, "confidentiality": "public", "denial": False},
        "recorded_by": "user",
        "recorded_at": "2026-10-27T00:00:00Z",
        "derived_status": "valid",
    }


def missing_rights(project_id, artifact_id):
    record = rights(
        project_id, artifact_id, "x", rights_id="RIGHTS-missing",
        **{op: "unknown" for op in _OPS},
    )
    record["subject"] = {
        "kind": "subject_artifact_ref",
        "project_id": project_id,
        "artifact_id": artifact_id,
    }
    return record


class AcquisitionCliTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="raa-cli-acq-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.state = self.tmp / "state"
        self.pid = self.create_project()

    # -- driving ------------------------------------------------------------- #
    def _run(self, *args: str):
        out, err = io.StringIO(), io.StringIO()
        code = cli_main.main(
            ["--data-dir", str(self.state), *args], env={}, stdout=out, stderr=err
        )
        return code, out.getvalue(), err.getvalue()

    def cli(self, *args: str, expect: int = 0) -> dict:
        code, out, err = self._run(*args)
        self.assertEqual(code, expect, f"args={args} rc={code}\n{out}\n{err}")
        self.assertNotIn("Traceback", err)
        for blob in (out, err):
            self.assertNotIn(_SECRET, blob)
            self.assertNotIn(_DOI, blob)
        return json.loads(out if expect == 0 else err)

    def write_json(self, name: str, data: object) -> str:
        path = self.tmp / name
        path.write_text(json.dumps(data), encoding="utf-8")
        return str(path)

    def write_bytes(self, name: str, data: bytes) -> str:
        path = self.tmp / name
        path.write_bytes(data)
        return str(path)

    def create_project(self) -> str:
        code, out, err = self._run("create", str(self.tmp / "proj"), "--name", "proj")
        self.assertEqual(code, 0, err)
        return json.loads(out)["project"]["project_id"]

    def occ_id(self, request_id: str) -> str:
        return self.cli(
            "acquisition", "occurrence", "id", "--project", self.pid,
            "--request-id", request_id,
        )["occurrence_artifact_id"]

    def import_args(self, *, file, art="ART-1", rev="REV-1", req="REQ-1",
                    occ_rev="OREV-1", src_rights=None, occ_rights=None, project=None,
                    extra=()):
        pid = project or self.pid
        rights_path = self.write_json(
            f"r-{req}-{rev}.json",
            src_rights if src_rights is not None else rights(pid, art, rev),
        )
        occ_path = self.write_json(
            f"o-{req}-{occ_rev}.json",
            occ_rights
            if occ_rights is not None
            else rights(pid, self.occ_id(req), occ_rev, rights_id=f"RIGHTS-O-{req}"),
        )
        return [
            "acquisition", "import", file, "--project", pid,
            "--artifact-id", art, "--revision-id", rev, "--request-id", req,
            "--rights-file", rights_path, "--occurrence-rights-file", occ_path,
            "--occurrence-revision-id", occ_rev,
            "--origin-locator", f"file://{_SECRET}", *extra,
        ]

    def store_counts(self) -> tuple[int, int]:
        root = str(self.tmp / "proj")
        store = ProjectStore.open_at(
            paths.app_dir_for(root), expected_project_id=self.pid
        )
        try:
            repo = ArtifactRepository(store)
            arts = repo.list_artifacts()
            return len(arts), sum(len(repo.list_revisions(a.artifact_id)) for a in arts)
        finally:
            store.close()

    # -- tests ---------------------------------------------------------------- #
    def test_import_replay_duplicate_and_readbacks(self) -> None:
        src = self.write_bytes("src.bin", _BYTES)
        first = self.cli(*self.import_args(file=src))
        self.assertEqual(first["disposition"], "created_new_revision")
        self.assertFalse(first["replayed"])
        self.assertTrue(first["proposal_only"])
        self.assertFalse(first["authorizes_use"])
        self.assertEqual(first["byte_size"], len(_BYTES))
        self.assertEqual(first["source_ref"]["revision_id"], "REV-1")
        self.assertEqual(first["occurrence_ref"]["artifact_id"], self.occ_id("REQ-1"))

        again = self.cli(*self.import_args(file=src))
        self.assertTrue(again["replayed"])
        self.assertEqual(again["occurrence_ref"], first["occurrence_ref"])

        dup = self.cli(*self.import_args(file=src, rev="REV-2", req="REQ-2", occ_rev="OREV-2"))
        self.assertEqual(dup["disposition"], "duplicate_bytes_of_existing_revision")
        self.assertEqual(dup["source_ref"]["revision_id"], "REV-1")
        self.assertNotEqual(dup["occurrence_ref"], first["occurrence_ref"])

        # restart: every later command is a fresh store open
        shown = self.cli(
            "acquisition", "occurrence", "show", "--project", self.pid,
            "--request-id", "REQ-1",
        )
        self.assertEqual(shown["occurrence_ref"], first["occurrence_ref"])
        self.assertFalse(shown["private_content_included"])
        self.assertNotIn("origin_locator", shown)
        self.assertTrue(shown["origin_locator_present"])
        self.assertTrue(shown["proposal_only"])

        current = self.cli(
            "acquisition", "occurrence", "show", "--project", self.pid,
            "--artifact-id", self.occ_id("REQ-1"),
        )
        self.assertEqual(current["occurrence_ref"], first["occurrence_ref"])
        exact = self.cli(
            "acquisition", "occurrence", "show", "--project", self.pid,
            "--artifact-id", self.occ_id("REQ-1"), "--revision-id", "OREV-1",
        )
        self.assertEqual(exact["sha256"], first["sha256"])

        # --content is the explicit private opt-in (the secret is only here)
        code, out, err = self._run(
            "acquisition", "occurrence", "show", "--project", self.pid,
            "--request-id", "REQ-1", "--content",
        )
        self.assertEqual(code, 0, err)
        body = json.loads(out)
        self.assertTrue(body["private_content_included"])
        self.assertIn(_SECRET, body["origin_locator"])

        impact = self.cli(
            "acquisition", "impact", "--project", self.pid,
            "--artifact-id", "ART-1", "--revision-id", "REV-1",
        )
        self.assertEqual(impact["report"], "source_impact")
        self.assertFalse(impact["authorizing"])
        self.assertFalse(impact["claims_change"])

    def test_missing_event_records_no_bytes(self) -> None:
        subj = self.write_json("subj.json", missing_rights(self.pid, "ART-M"))
        occ = self.write_json(
            "occ.json",
            rights(self.pid, self.occ_id("REQ-M"), "OREV-M", rights_id="RIGHTS-OM"),
        )
        out = self.cli(
            "acquisition", "missing", "--project", self.pid, "--artifact-id", "ART-M",
            "--request-id", "REQ-M", "--rights-file", subj,
            "--occurrence-rights-file", occ, "--occurrence-revision-id", "OREV-M",
            "--origin-locator", "paywalled", "--reason", f"no access {_SECRET}",
        )
        self.assertEqual(out["disposition"], "no_full_text_acquired")
        self.assertIsNone(out["source_ref"])
        self.assertIsNone(out["sha256"])
        shown = self.cli(
            "acquisition", "occurrence", "show", "--project", self.pid,
            "--request-id", "REQ-M",
        )
        self.assertTrue(shown["reason_present"])
        self.assertNotIn("reason", shown)
        self.assertIsNone(shown["source_ref"])

    def test_rights_and_project_failures_write_nothing(self) -> None:
        src = self.write_bytes("src.bin", _BYTES)
        before = self.store_counts()
        # storage not allowed -> no bytes retained
        denied = self.cli(
            *self.import_args(file=src, src_rights=rights(self.pid, "ART-1", "REV-1", storage="unknown")),
            expect=cli_main.EXIT_VALIDATION,
        )
        self.assertEqual(denied["error"]["category"], "validation")
        # occurrence rights bound to the wrong subject
        wrong = self.cli(
            *self.import_args(file=src, occ_rights=rights(self.pid, "OTHER", "OREV-1")),
            expect=cli_main.EXIT_VALIDATION,
        )
        self.assertEqual(wrong["error"]["category"], "validation")
        # unknown project
        self.cli(
            "acquisition", "impact", "--project", "no-such-project",
            "--artifact-id", "A", "--revision-id", "R",
            expect=cli_main.EXIT_VALIDATION,
        )
        self.assertEqual(self.store_counts(), before)

    def test_request_and_rights_conflicts(self) -> None:
        src = self.write_bytes("src.bin", _BYTES)
        self.cli(*self.import_args(file=src))
        other = self.write_bytes("other.bin", b"different bytes")
        conflict = self.cli(
            *self.import_args(file=other), expect=cli_main.EXIT_CONFLICT
        )
        self.assertEqual(conflict["error"]["category"], "conflict")

    def test_malformed_and_unsafe_inputs(self) -> None:
        src = self.write_bytes("src.bin", _BYTES)
        dup_keys = self.tmp / "dup.json"
        dup_keys.write_text('{"a": 1, "a": "%s"}' % _SECRET, encoding="utf-8")
        args = self.import_args(file=src)
        idx = args.index("--rights-file")
        for bad in (
            dup_keys.read_bytes(),
            b'{"a": NaN}',
            b"\xff\xfe",
            b"[" * 500 + b"]" * 500,
        ):
            badp = self.write_bytes("bad.json", bad)
            patched = args[:idx + 1] + [badp] + args[idx + 2:]
            err = self.cli(*patched, expect=cli_main.EXIT_VALIDATION)
            self.assertEqual(err["error"]["category"], "validation")
        # directory / symlink / missing as the source file
        for target in (str(self.tmp), str(self.tmp / "absent")):
            self.cli(*self.import_args(file=target), expect=cli_main.EXIT_VALIDATION)
        link = self.tmp / "link.bin"
        os.symlink(src, link)
        err = self.cli(*self.import_args(file=str(link)), expect=cli_main.EXIT_VALIDATION)
        self.assertNotIn(str(link), json.dumps(err))
        self.assertEqual(self.store_counts(), (0, 0))

    def test_occurrence_id_is_read_only_and_deterministic(self) -> None:
        before = self.store_counts()
        a = self.occ_id("REQ-X")
        self.assertEqual(a, self.occ_id("REQ-X"))
        self.assertNotEqual(a, self.occ_id("REQ-Y"))
        self.assertEqual(self.store_counts(), before)
        missing = self.cli(
            "acquisition", "occurrence", "show", "--project", self.pid,
            "--request-id", "REQ-X", expect=cli_main.EXIT_VALIDATION,
        )
        self.assertEqual(missing["error"]["category"], "validation")
        both = self._run(
            "acquisition", "occurrence", "show", "--project", self.pid,
        )
        self.assertEqual(both[0], cli_main.EXIT_VALIDATION)

    def test_inspect_html_masquerading_as_pdf(self) -> None:
        html = self.write_bytes("fake.pdf", _HTML)
        ids = self.write_json("ids.json", {"doi": _DOI})
        out = self.cli(
            "acquisition", "inspect", html, "--identifiers-file", ids,
            "--declared-media-type", "application/pdf", "--pdf-inspect",
        )
        self.assertEqual(out["status"], "rejected")
        self.assertEqual(out["media_kind"], "html")
        self.assertFalse(out["authorizes_scientific_processing"])
        self.assertEqual(out["pdf_inspection"]["status"], "skipped")
        self.assertNotIn("observed_identifiers", out)

    def test_inspect_pdf_dependency_unavailable_and_privacy(self) -> None:
        pdf = self.write_bytes("a.pdf", _PDF)
        ids = self.write_json("ids.json", {"doi": _DOI})
        with mock.patch.object(pdf_inspection, "_pypdf_available", return_value=False):
            out = self.cli(
                "acquisition", "inspect", pdf, "--identifiers-file", ids,
                "--pdf-inspect",
            )
            self.assertEqual(out["media_kind"], "pdf")
            self.assertEqual(out["pdf_inspection"]["status"], "unavailable")
            self.assertEqual(out["pdf_inspection"]["failure_code"], "dependency_missing")
            self.assertNotIn("pdf_inspection_content", out)
            code, body, err = self._run(
                "acquisition", "inspect", pdf, "--identifiers-file", ids,
                "--pdf-inspect", "--content",
            )
            self.assertEqual(code, 0, err)
            doc = json.loads(body)
            self.assertTrue(doc["private_content_included"])
            self.assertIn("text_preview", doc["pdf_inspection_content"])
    
    def test_inspect_rejects_bad_identifier_mapping(self) -> None:
        pdf = self.write_bytes("a.pdf", _PDF)
        bad = self.write_json("bad.json", {"isbn": _SECRET})
        err = self.cli(
            "acquisition", "inspect", pdf, "--identifiers-file", bad,
            expect=cli_main.EXIT_VALIDATION,
        )
        self.assertEqual(err["error"]["category"], "validation")
        notmap = self.write_json("list.json", [_SECRET])
        self.cli(
            "acquisition", "inspect", pdf, "--identifiers-file", notmap,
            expect=cli_main.EXIT_VALIDATION,
        )


if __name__ == "__main__":
    unittest.main()
