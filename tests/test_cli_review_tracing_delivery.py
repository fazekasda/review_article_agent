"""In-process CLI tests for the ``review``, ``tracing``, and ``delivery`` groups.

Covers ``src/review_article_agent/cli/review.py``, ``cli/tracing.py``, and
``cli/delivery.py`` over the integrated Step 12/13/14 repositories:

* ``review show``/``review status`` read one section review's safe state summary
  (rounds used, status, required human action) with ``--content`` opt-in;
* ``tracing run`` deterministically derives and records one origin trace from the
  real draft/package/review state (no network), and ``tracing show``/
  ``tracing coverage`` read it back;
* ``delivery start`` starts the global review from recorded review/trace states;
  ``freshness``/``notes add``/``notes apply``/``revalidate``/``status`` are the
  deterministic/human steps; ``approve --prepare`` prints the manifest digest and
  records nothing; ``delivery export`` writes the seven files after final approval
  and refuses (writing nothing) on a stale/missing approval or an out-dir inside
  the data dir.

Fixtures are reused from the accepted ``test_review_workflow`` /
``test_tracing_workflow`` / ``test_delivery_workflow`` / ``test_step15_e2e_manuscript``
modules; no model, network, provider, or OpenCode session call is made.

Run::

    PYTHONPATH=src:tests python3 -m unittest test_cli_review_tracing_delivery -v
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))
_TESTS = Path(__file__).resolve().parent
if str(_TESTS) not in sys.path:
    sys.path.insert(0, str(_TESTS))

import review_article_agent.cli.delivery as cli_delivery  # noqa: E402
import review_article_agent.cli.main as cli_main  # noqa: E402
import review_article_agent.cli.tracing as cli_tracing  # noqa: E402
from review_article_agent.delivery.workflow import DeliveryRepository  # noqa: E402
from review_article_agent.review.workflow import SectionReviewRepository  # noqa: E402
from review_article_agent.runtime import owner_auth  # noqa: E402
from review_article_agent.runtime.gates import prepare_gate_snapshot  # noqa: E402
from review_article_agent.storage import ProjectStore, Registry, paths  # noqa: E402
from review_article_agent.storage.human_approvals import (  # noqa: E402
    HumanApprovalRepository,
)
from review_article_agent.tracing.workflow import OriginTraceRepository  # noqa: E402

from test_delivery_workflow import (  # noqa: E402
    ORIGIN_OK,
    PACKAGE,
    SECTIONS_OK,
    _reviewer,
    _writer,
)
from test_human_approvals import (  # noqa: E402
    _BASE_TIME,
    _FakeTty,
    _FIXED_CHALLENGE,
    _OWNER_UID,
)
from test_review_workflow import (  # noqa: E402
    _CRITIC,
    _WRITER,
    FakeCritic,
    FakeWriter,
    _clean_draft,
)
from test_tracing_workflow import POLICY  # noqa: E402

_OPS = ("acquisition", "storage", "redistribution", "hosted_transfer")

#: A synthetic approved drafting package whose single citation key matches the
#: delivery manuscript text ``Body [@k1].`` (so the export consistency check passes).
EXPORT_PACKAGE = {
    "citations": {
        "k1": {
            "citation_key": "k1",
            "identity": {"doi": "10.1/x"},
            "version": "v1",
            "title": "A synthetic study",
            "authors": ["A. Author"],
            "year": 2020,
            "journal": "Journal of Synthetic Results",
            "verified": True,
        }
    },
    "evidence": [],
}

EXPORT_FILES = {
    "manuscript.md",
    "references.bib",
    "references.ris",
    "companion.md",
    "summary.md",
    "ai_declaration.md",
    "export_manifest.json",
}


def _rights(project_id, artifact_id, revision_id):
    """A synthetic, fully valid RightsRecord v1 bound to one exact revision."""
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
        "basis": {
            op: {
                "kind": "public_domain",
                "evidence": {"ref": "lic:pd-syn", "provenance": "publisher:pd-list"},
            }
            for op in _OPS
        },
        "authorization": None,
        "restrictions": [],
        "privacy": {"personal_data": False, "confidentiality": "public", "denial": False},
        "recorded_by": "user",
        "recorded_at": "2026-10-30T00:00:00Z",
        "derived_status": "valid",
    }


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class CliBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="raa-cli-rtd-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(os.path.realpath(self._tmp.name))
        self.proot = self.tmp / "project"
        self.store = ProjectStore.create_at(
            paths.app_dir_for(str(self.proot)), paths.new_project_id()
        )
        self.addCleanup(self._close)
        self.pid = self.store.project_id
        self.state = self.tmp / "state"
        self.registry = Registry(self.state)
        self.registry.register_restored(str(self.proot), project_id=self.pid)

    def _close(self) -> None:
        try:
            self.store.close()
        except Exception:  # noqa: BLE001
            pass

    def rights(self, artifact_id, revision_id):
        return _rights(self.pid, artifact_id, revision_id)

    def _run(self, *args: str):
        out, err = io.StringIO(), io.StringIO()
        code = cli_main.main(
            ["--data-dir", str(self.state), *args], env={}, stdout=out, stderr=err
        )
        return code, out.getvalue(), err.getvalue()

    def cli(self, *args: str, expect: int = 0) -> dict:
        code, out, err = self._run(*args)
        self.assertEqual(code, expect, f"args={args} rc={code}\n{out}\n{err}")
        return json.loads(out if expect == 0 else err)

    def write_json(self, name: str, data: object) -> str:
        path = self.tmp / name
        path.write_text(json.dumps(data), encoding="utf-8")
        return str(path)

    # -- real state builders -------------------------------------------------- #
    def make_review(self, review_id: str = "review-sec1") -> dict:
        draft, package = _clean_draft()
        return SectionReviewRepository(self.store).run(
            review_id,
            draft=draft,
            package=package,
            writer_model=_WRITER,
            critic_model=_CRITIC,
            critic=FakeCritic([]),
            writer=FakeWriter(),
            rights=self.rights,
        )

    def make_trace(self, trace_id: str = "trace-x") -> dict:
        repo = OriginTraceRepository(self.store)
        return repo.run(
            trace_id,
            claims=[],
            sources={},
            policy=POLICY,
            review_checks=[],
            acquire=lambda request: {"status": "unavailable"},
            rights=self.rights,
        )

    def make_reviewed_delivery(self, delivery_id: str = "delivery-1") -> dict:
        repo = DeliveryRepository(self.store)
        repo.start_global_review(delivery_id, SECTIONS_OK, ORIGIN_OK, rights=self.rights)
        reviewer, _ = _reviewer(find_if_missing=False)
        writer, _ = _writer()
        return repo.run_global_review(
            delivery_id,
            {
                "revision_id": "m1",
                "text": "Body [@k1].",
                "paragraphs": {"p1": {"text": "Body [@k1].", "checks": "stale"}},
            },
            [],
            reviewer,
            writer,
            rights=self.rights,
        )

    def make_approved_delivery(self, delivery_id: str = "delivery-1") -> dict:
        repo = DeliveryRepository(self.store)
        repo.start_global_review(delivery_id, SECTIONS_OK, ORIGIN_OK, rights=self.rights)
        reviewer, _ = _reviewer(find_if_missing=False)
        writer, _ = _writer()
        repo.run_global_review(
            delivery_id,
            {
                "revision_id": "m1",
                "text": "Body [@k1].",
                "paragraphs": {"p1": {"text": "Body [@k1].", "checks": "valid"}},
            },
            [],
            reviewer,
            writer,
            rights=self.rights,
        )
        repo.freshness(
            delivery_id, "2026-01-01", {"action": "accept_cutoff"}, rights=self.rights
        )
        return repo.final_approval(
            delivery_id,
            artifact_hashes={"art-1": "sha-1"},
            warnings=[],
            accepted_warning_ids=[],
            unsupported_major_claims=[],
            approver="alice",
            rights=self.rights,
        )

    # -- owner-authenticated named-gate helper (same seams as the gate tests) -- #
    def record_gate(self, artifact_id: str, revision_id: str) -> None:
        """Record one real owner-authenticated human approval over a revision.

        Only the owner-auth low-level terminal/identity seams are patched (as
        ``test_human_approvals`` does) -- never a capability bypass.
        """
        prepared = prepare_gate_snapshot(
            self.store,
            {
                "project_id": self.pid,
                "artifact_id": artifact_id,
                "revision_id": revision_id,
            },
            gate_kind="artifact_revision",
            gate_id="delivery.final_approval",
            decision="approved",
            rationale="human review of the exact delivery manifest revision",
        )
        with mock.patch.object(owner_auth, "_new_challenge", lambda: _FIXED_CHALLENGE), \
             mock.patch.object(owner_auth, "_current_owner_uid", lambda: _OWNER_UID), \
             mock.patch.object(owner_auth, "_real_uid", lambda: _OWNER_UID), \
             mock.patch.object(owner_auth, "_utcnow", lambda: _BASE_TIME), \
             mock.patch.object(owner_auth, "_monotonic", lambda: 1000.0), \
             mock.patch.object(
                 owner_auth,
                 "_open_controlling_tty",
                 lambda: _FakeTty(replies=[_FIXED_CHALLENGE]),
             ):
            capability = owner_auth.request_owner_confirmation(prepared.owner_action())
            HumanApprovalRepository(self.store).record_decision(prepared, capability)

    def make_gate_approved_delivery(self, delivery_id: str = "delivery-1") -> dict:
        """A delivery whose exact-revision final-approval named gate is recorded.

        Records each section's approved draft ref (for the re-deriving export),
        prepares the manifest revision, and records the owner-authenticated named
        gate over that exact revision.
        """
        repo = DeliveryRepository(self.store)
        sections = [
            {
                "section_id": "s1",
                "review_status": "complete",
                "origin_complete": True,
                "draft_ref": {
                    "project_id": self.pid,
                    "artifact_id": "draft-1",
                    "revision_id": "draft-r1",
                },
            }
        ]
        repo.start_global_review(delivery_id, sections, ORIGIN_OK, rights=self.rights)
        reviewer, _ = _reviewer(find_if_missing=False)
        writer, _ = _writer()
        repo.run_global_review(
            delivery_id,
            {
                "revision_id": "m1",
                "text": "Body [@k1].",
                "paragraphs": {"p1": {"text": "Body [@k1].", "checks": "valid"}},
            },
            [],
            reviewer,
            writer,
            rights=self.rights,
        )
        repo.freshness(
            delivery_id, "2026-01-01", {"action": "accept_cutoff"}, rights=self.rights
        )
        prepared = repo.prepare_final_approval(
            delivery_id,
            artifact_hashes={"art-1": "sha-1"},
            warnings=[],
            accepted_warning_ids=[],
            unsupported_major_claims=[],
            rights=self.rights,
        )
        self.record_gate(delivery_id, prepared["revision_id"])
        return prepared

    def make_prepared_delivery(self, delivery_id: str = "delivery-1") -> dict:
        """A delivery whose manifest revision is prepared but not yet approved."""
        repo = DeliveryRepository(self.store)
        sections = [
            {
                "section_id": "s1",
                "review_status": "complete",
                "origin_complete": True,
                "draft_ref": {
                    "project_id": self.pid,
                    "artifact_id": "draft-1",
                    "revision_id": "draft-r1",
                },
            }
        ]
        repo.start_global_review(delivery_id, sections, ORIGIN_OK, rights=self.rights)
        reviewer, _ = _reviewer(find_if_missing=False)
        writer, _ = _writer()
        repo.run_global_review(
            delivery_id,
            {
                "revision_id": "m1",
                "text": "Body [@k1].",
                "paragraphs": {"p1": {"text": "Body [@k1].", "checks": "valid"}},
            },
            [],
            reviewer,
            writer,
            rights=self.rights,
        )
        repo.freshness(
            delivery_id, "2026-01-01", {"action": "accept_cutoff"}, rights=self.rights
        )
        return repo.prepare_final_approval(
            delivery_id,
            artifact_hashes={"art-1": "sha-1"},
            warnings=[],
            accepted_warning_ids=[],
            unsupported_major_claims=[],
            rights=self.rights,
        )


class ParserRegistrationTest(CliBase):
    def test_groups_registered_and_required_options_enforced(self):
        parser = cli_main.build_parser()
        help_text = parser.format_help()
        for group in ("review", "tracing", "delivery"):
            self.assertIn(group, help_text)
        parsed = parser.parse_args(["review", "show", "--review-id", "r"])
        self.assertEqual(parsed.review_command, "show")
        parsed = parser.parse_args(["tracing", "coverage", "--trace-id", "t"])
        self.assertEqual(parsed.tracing_command, "coverage")
        parsed = parser.parse_args(["delivery", "status", "--delivery-id", "d"])
        self.assertEqual(parsed.delivery_command, "status")
        with self.assertRaises(cli_main.CliUsageError):
            parser.parse_args(["review", "show"])  # missing --review-id
        with self.assertRaises(cli_main.CliUsageError):
            parser.parse_args(["tracing", "run", "--trace-id", "t"])  # missing draft ref
        with self.assertRaises(cli_main.CliUsageError):
            parser.parse_args(["delivery", "export", "--delivery-id", "d"])  # missing out-dir
        # a bare group is a usage error, not an internal failure
        for group in ("review", "tracing", "delivery"):
            self.assertEqual(self._run(group)[0], cli_main.EXIT_USAGE)


class ReviewCliTest(CliBase):
    def test_show_and_status_safe_then_content_opt_in(self):
        self.make_review("review-sec1")
        show = self.cli(
            "review", "show", "--project", self.pid, "--review-id", "review-sec1"
        )
        self.assertEqual(show["review_id"], "review-sec1")
        self.assertEqual(show["status"], "complete")
        self.assertEqual(show["rounds_used"]["critique"], 1)
        self.assertEqual(show["rounds_used"]["fact_check"], 0)
        self.assertEqual(show["critic_calls"], 1)
        self.assertIsNone(show["required_human_action"])
        self.assertFalse(show["independent_human_verification"])
        self.assertFalse(show["private_content_included"])
        self.assertNotIn("payload", show)
        self.assertFalse(show["authorizes_export"])
        self.assertFalse(show["scientific_validation"])

        status = self.cli(
            "review", "status", "--project", self.pid, "--review-id", "review-sec1"
        )
        self.assertEqual(status["status"], "complete")
        self.assertEqual(status["rounds_used"], {"critique": 1, "fact_check": 0})
        self.assertNotIn("payload", status)

        content = self.cli(
            "review", "show", "--project", self.pid,
            "--review-id", "review-sec1", "--content",
        )
        self.assertTrue(content["private_content_included"])
        self.assertIn("payload", content)
        self.assertIn("paragraphs", json.dumps(content["payload"]))


class TracingCliTest(CliBase):
    def _run_trace(self, *, trace_id="trace-1", policy_file=None, expect=0):
        draft, package = _clean_draft()
        rights_file = self.write_json("trace-rights.json", self.rights("trace-1", "rev-1"))
        args = [
            "tracing", "run", "--project", self.pid, "--trace-id", trace_id,
            "--draft-artifact-id", "draft-1", "--draft-revision-id", "draft-r1",
            "--review-id", "review-sec1", "--rights-file", rights_file,
        ]
        if policy_file is not None:
            args += ["--policy-file", policy_file]
        with mock.patch.object(
            cli_tracing,
            "DraftingWorkflowRepository",
            lambda store: mock.Mock(
                read=lambda ref, kind: {"result": draft, "package": package}
            ),
        ):
            return self.cli(*args, expect=expect)

    def test_run_show_coverage_happy_path(self):
        self.make_review("review-sec1")
        policy_file = self.write_json("policy.json", POLICY)
        run = self._run_trace(policy_file=policy_file)
        self.assertEqual(run["trace_id"], "trace-1")
        self.assertTrue(run["claim_count"] > 0)
        self.assertFalse(run["private_content_included"])
        self.assertFalse(run["authorizes_export"])

        show = self.cli(
            "tracing", "show", "--project", self.pid, "--trace-id", "trace-1"
        )
        self.assertEqual(show["trace_id"], "trace-1")
        self.assertEqual(show["claim_count"], run["claim_count"])
        self.assertNotIn("payload", show)

        coverage = self.cli(
            "tracing", "coverage", "--project", self.pid, "--trace-id", "trace-1"
        )
        self.assertIn("counts", coverage)
        self.assertFalse(coverage["edits_applied"])
        self.assertIn("complete", coverage)
        self.assertIn("export_ready", coverage)

        content = self.cli(
            "tracing", "show", "--project", self.pid, "--trace-id", "trace-1", "--content"
        )
        self.assertTrue(content["private_content_included"])
        self.assertIn("payload", content)

    def test_unconfigured_policy_is_configuration_required(self):
        self.make_review("review-sec1")
        run = self._run_trace(trace_id="trace-nopolicy", policy_file=None)
        self.assertEqual(run["status"], "configuration_required")
        self.assertEqual(run["acquire_calls"], 0)

    def test_run_never_uses_the_network(self):
        self.make_review("review-sec1")
        policy_file = self.write_json("policy.json", POLICY)

        def _no_socket(*args, **kwargs):
            raise AssertionError("tracing run must not open a network socket")

        with mock.patch.object(socket, "socket", _no_socket):
            run = self._run_trace(trace_id="trace-nonet", policy_file=policy_file)
        self.assertEqual(run["trace_id"], "trace-nonet")


class DeliveryCliTest(CliBase):
    def test_start_freshness_status(self):
        self.make_review("review-sec1")
        self.make_trace("trace-x")
        rights_file = self.write_json("del-rights.json", self.rights("delivery-1", "r1"))
        started = self.cli(
            "delivery", "start", "--project", self.pid, "--delivery-id", "delivery-1",
            "--section", "s1", "--review-id", "review-sec1", "--trace-id", "trace-x",
            "--rights-file", rights_file,
        )
        self.assertEqual(started["status"], "in_progress")
        self.assertEqual(started["section_count"], 1)

        fresh = self.cli(
            "delivery", "freshness", "--project", self.pid, "--delivery-id", "delivery-1",
            "--cutoff-date", "2026-01-01", "--decision", "accept_cutoff",
            "--rights-file", rights_file,
        )
        self.assertEqual(fresh["freshness_status"], "cutoff_accepted")

        status = self.cli(
            "delivery", "status", "--project", self.pid, "--delivery-id", "delivery-1"
        )
        self.assertEqual(status["freshness_status"], "cutoff_accepted")
        self.assertFalse(status["approval_present"])

    def test_start_requires_paired_section_review_trace(self):
        self.make_review("review-sec1")
        self.make_trace("trace-x")
        rights_file = self.write_json("del-rights.json", self.rights("delivery-1", "r1"))
        error = self.cli(
            "delivery", "start", "--project", self.pid, "--delivery-id", "delivery-1",
            "--section", "s1", "--section", "s2",
            "--review-id", "review-sec1", "--trace-id", "trace-x",
            "--rights-file", rights_file,
            expect=cli_main.EXIT_VALIDATION,
        )
        self.assertEqual(error["error"]["category"], "validation")

    def test_notes_apply_requires_approval_id(self):
        self.make_approved_delivery("delivery-1")
        notes_file = self.write_json(
            "notes.json",
            [{"note_id": "n1", "text": "tighten wording", "category": "wording"}],
        )
        rights_file = self.write_json("del-rights.json", self.rights("delivery-1", "r1"))
        self.cli(
            "delivery", "notes", "add", "--project", self.pid,
            "--delivery-id", "delivery-1", "--file", notes_file,
            "--rights-file", rights_file,
        )
        batch_file = self.write_json(
            "batch.json",
            {
                "batch_id": "b1",
                "edits": [
                    {
                        "note_id": "n1",
                        "paragraph_id": "p1",
                        "new_text": "Body [@k1].",
                        "kind": "wording",
                    }
                ],
            },
        )
        # Missing --approval-id is a usage error (argparse enforces it).
        code, _out, _err = self._run(
            "delivery", "notes", "apply", "--project", self.pid,
            "--delivery-id", "delivery-1", "--batch-file", batch_file,
            "--rights-file", rights_file,
        )
        self.assertEqual(code, cli_main.EXIT_USAGE)
        # An empty approval id is refused by the repository.
        error = self.cli(
            "delivery", "notes", "apply", "--project", self.pid,
            "--delivery-id", "delivery-1", "--batch-file", batch_file,
            "--approval-id", "", "--rights-file", rights_file,
            expect=cli_main.EXIT_VALIDATION,
        )
        self.assertEqual(error["error"]["category"], "validation")

    def _patch_drafting_package(self):
        """Patch the drafting read so export re-derives the approved package."""
        return mock.patch.object(
            cli_delivery,
            "DraftingWorkflowRepository",
            lambda store: mock.Mock(
                read=lambda ref, kind: {"result": {}, "package": EXPORT_PACKAGE}
            ),
        )

    def test_export_writes_seven_files_with_matching_hashes(self):
        self.make_gate_approved_delivery("delivery-1")
        rights_file = self.write_json("del-rights.json", self.rights("delivery-1", "r1"))
        out_dir = self.tmp / "export"
        with self._patch_drafting_package():
            result = self.cli(
                "delivery", "export", "--project", self.pid, "--delivery-id", "delivery-1",
                "--out-dir", str(out_dir), "--rights-file", rights_file,
            )
        self.assertEqual(result["status"], "exported")
        self.assertTrue(result["consistency_ok"])
        self.assertEqual(set(os.listdir(out_dir)), EXPORT_FILES)
        self.assertEqual(set(result["file_hashes"]), EXPORT_FILES)
        for name, digest in result["file_hashes"].items():
            content = (out_dir / name).read_text(encoding="utf-8")
            self.assertEqual(_sha256(content), digest, f"hash mismatch for {name}")
        manifest = json.loads((out_dir / "export_manifest.json").read_text())
        # The manifest covers the six content files; its recorded hashes match the
        # bytes written and the CLI's per-file hashes.
        self.assertEqual(
            set(manifest["file_hashes"]), EXPORT_FILES - {"export_manifest.json"}
        )
        for name, digest in manifest["file_hashes"].items():
            self.assertEqual(digest, result["file_hashes"][name])
        self.assertEqual(manifest["manuscript_revision"], "m1")

    def test_export_refused_without_gate_approval_and_writes_nothing(self):
        # A delivery with recorded draft refs but no prepared manifest/gate.
        repo = DeliveryRepository(self.store)
        sections = [
            {
                "section_id": "s1",
                "review_status": "complete",
                "origin_complete": True,
                "draft_ref": {
                    "project_id": self.pid,
                    "artifact_id": "draft-1",
                    "revision_id": "draft-r1",
                },
            }
        ]
        repo.start_global_review("delivery-1", sections, ORIGIN_OK, rights=self.rights)
        reviewer, _ = _reviewer(find_if_missing=False)
        writer, _ = _writer()
        repo.run_global_review(
            "delivery-1",
            {
                "revision_id": "m1",
                "text": "Body [@k1].",
                "paragraphs": {"p1": {"text": "Body [@k1].", "checks": "valid"}},
            },
            [],
            reviewer,
            writer,
            rights=self.rights,
        )
        repo.freshness(
            "delivery-1", "2026-01-01", {"action": "accept_cutoff"}, rights=self.rights
        )
        rights_file = self.write_json("del-rights.json", self.rights("delivery-1", "r1"))
        out_dir = self.tmp / "export-no-approval"
        with self._patch_drafting_package():
            error = self.cli(
                "delivery", "export", "--project", self.pid, "--delivery-id", "delivery-1",
                "--out-dir", str(out_dir), "--rights-file", rights_file,
                expect=cli_main.EXIT_VALIDATION,
            )
        self.assertEqual(error["error"]["category"], "validation")
        self.assertFalse(os.path.exists(out_dir))

    def test_export_refuses_after_notes_edit_and_writes_nothing(self):
        self.make_gate_approved_delivery("delivery-1")
        notes_file = self.write_json(
            "notes.json",
            [{"note_id": "n1", "text": "tighten wording", "category": "wording"}],
        )
        rights_file = self.write_json("del-rights.json", self.rights("delivery-1", "r1"))
        self.cli(
            "delivery", "notes", "add", "--project", self.pid,
            "--delivery-id", "delivery-1", "--file", notes_file,
            "--rights-file", rights_file,
        )
        batch_file = self.write_json(
            "batch.json",
            {
                "batch_id": "b1",
                "edits": [
                    {
                        "note_id": "n1",
                        "paragraph_id": "p1",
                        "new_text": "Body [@k1].",
                        "kind": "wording",
                    }
                ],
            },
        )
        applied = self.cli(
            "delivery", "notes", "apply", "--project", self.pid,
            "--delivery-id", "delivery-1", "--batch-file", batch_file,
            "--approval-id", "appr-1", "--rights-file", rights_file,
        )
        self.assertFalse(applied["approval_present"])

        out_dir = self.tmp / "export-refused"
        with self._patch_drafting_package():
            error = self.cli(
                "delivery", "export", "--project", self.pid, "--delivery-id", "delivery-1",
                "--out-dir", str(out_dir), "--rights-file", rights_file,
                expect=cli_main.EXIT_VALIDATION,
            )
        self.assertEqual(error["error"]["category"], "validation")
        self.assertFalse(os.path.exists(out_dir))

    def test_export_refuses_out_dir_inside_data_dir(self):
        self.make_gate_approved_delivery("delivery-1")
        rights_file = self.write_json("del-rights.json", self.rights("delivery-1", "r1"))
        out_dir = self.state / "export"
        error = self.cli(
            "delivery", "export", "--project", self.pid, "--delivery-id", "delivery-1",
            "--out-dir", str(out_dir), "--rights-file", rights_file,
            expect=cli_main.EXIT_STORAGE,
        )
        self.assertEqual(error["error"]["category"], "storage")
        self.assertFalse(os.path.exists(out_dir))

    def test_export_drops_package_file_flag(self):
        # ``--package-file`` is removed: the packages are re-derived.
        with self.assertRaises(cli_main.CliUsageError):
            cli_main.build_parser().parse_args(
                [
                    "delivery", "export", "--delivery-id", "d", "--out-dir", "o",
                    "--package-file", "p", "--rights-file", "r",
                ]
            )

    def test_approve_has_no_approver_and_prepare_records_no_approval(self):
        # No API accepts an approver string for the CLI export/approval path.
        with self.assertRaises(cli_main.CliUsageError):
            cli_main.build_parser().parse_args(
                [
                    "delivery", "approve", "--delivery-id", "d", "--prepare",
                    "--rights-file", "r", "--approver", "alice",
                ]
            )
        self.make_prepared_delivery("delivery-1")
        rights_file = self.write_json("del-rights.json", self.rights("delivery-1", "r1"))
        prepared = self.cli(
            "delivery", "approve", "--project", self.pid, "--delivery-id", "delivery-1",
            "--prepare", "--rights-file", rights_file,
        )
        self.assertTrue(prepared["prepare_only"])
        self.assertFalse(prepared["approval_recorded"])
        self.assertFalse(prepared["human_authenticated"])
        self.assertRegex(prepared["manifest_digest"], r"^[0-9a-f]{64}$")
        self.assertIn("gate prepare", prepared["prepare_command"])
        self.assertIn("gate confirm", prepared["confirm_command"])
        self.assertIn(prepared["manifest_revision"], prepared["confirm_command"])
        # The gate is not yet satisfied: the export is refused until a human
        # records it (the CLI itself cannot).
        out_dir = self.tmp / "export-unapproved"
        with self._patch_drafting_package():
            error = self.cli(
                "delivery", "export", "--project", self.pid, "--delivery-id", "delivery-1",
                "--out-dir", str(out_dir), "--rights-file", rights_file,
                expect=cli_main.EXIT_VALIDATION,
            )
        self.assertEqual(error["error"]["category"], "validation")
        self.assertFalse(os.path.exists(out_dir))
        # Without --prepare the command refuses.
        error = self.cli(
            "delivery", "approve", "--project", self.pid, "--delivery-id", "delivery-1",
            "--rights-file", rights_file,
            expect=cli_main.EXIT_VALIDATION,
        )
        self.assertEqual(error["error"]["category"], "validation")

    def test_revalidate_recomputes_checks_from_package(self):
        self.make_reviewed_delivery("delivery-1")
        package_file = self.write_json("package.json", PACKAGE)
        rights_file = self.write_json("del-rights.json", self.rights("delivery-1", "r1"))
        result = self.cli(
            "delivery", "revalidate", "--project", self.pid, "--delivery-id", "delivery-1",
            "--paragraph", "p1", "--package-file", package_file,
            "--rights-file", rights_file,
        )
        self.assertEqual(result["revalidation_required"], [])
        self.assertEqual(result["revalidation_record_count"], 1)


class UnknownIdValidationTest(CliBase):
    def test_unknown_ids_are_static_validation_errors(self):
        # ``test_unknown_review_is_static_validation_error``,
        # ``test_unknown_trace_is_static_validation_error`` and
        # ``test_unknown_delivery_is_static_validation_error``: an unknown id in
        # each group is a static validation error that never echoes the id.
        cases = (
            ("test_unknown_review_is_static_validation_error",
             ("review", "show", "--project", self.pid, "--review-id", "no-such-review"),
             "no-such-review"),
            ("test_unknown_trace_is_static_validation_error",
             ("tracing", "show", "--project", self.pid, "--trace-id", "no-such-trace"),
             "no-such-trace"),
            ("test_unknown_delivery_is_static_validation_error",
             ("delivery", "status", "--project", self.pid, "--delivery-id", "no-such-delivery"),
             "no-such-delivery"),
        )
        for label, argv, echoed in cases:
            with self.subTest(case=label):
                error = self.cli(*argv, expect=cli_main.EXIT_VALIDATION)
                self.assertEqual(error["error"]["category"], "validation")
                self.assertNotIn(echoed, json.dumps(error))
                self.assertNotIn("Traceback", json.dumps(error))


if __name__ == "__main__":
    unittest.main()
