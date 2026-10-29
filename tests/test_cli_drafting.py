"""In-process CLI tests for the ``drafting`` command group (Step 11, S11-CLI).

Covers ``src/review_article_agent/cli/drafting.py`` over the integrated
``SectionWorkspaceRepository``, ``DraftingPackageRepository``, and
``DraftingWorkflowRepository``:

* the ``drafting`` group is registered on the top-level parser (and its
  ``propose``/``show``/``package``/``accepted``/``status`` subcommands enforce
  their required options; ``workspace``/``blueprint``/``draft``/``assembly`` are
  the valid propose/show kinds and ``blueprint``/``draft``/``assembly`` the
  status kinds);
* ``drafting propose --kind workspace`` writes one inert workspace revision from
  explicit ids and an explicit caller rights record -- no authority flag is set;
* ``drafting propose --kind blueprint`` writes one inert blueprint revision and
  names its exact owner gate (``artifact_revision`` / ``drafting.blueprint``);
  ``--kind draft``/``assembly`` write inert revisions with **no** invented gate;
* ``drafting show`` reads one exact revision -- a safe structural summary by
  default (never the private session/model pins, blueprint evidence, draft text,
  Markdown, or citations) and the private payload only with ``--content``;
  ``--historical`` reads a non-current revision without live-gate authority;
* ``drafting package`` assembles one approved section's package -- safe counts
  by default, the full package only with ``--content``;
* ``drafting accepted`` requires **this blueprint's own** named owner gate
  (read-only; it never approves) -- safe counts and the exact gate ids by
  default, the full private envelope only with ``--content``;
* ``drafting status`` reads one current blueprint's structured status
  (``stale``/``reasons``/``pending``/non-export) with no raw content;
* the CLI delegates to the repositories (it never fabricates a runtime mapping or
  creates a session), forwarding the exact refs/args;
* a missing/malformed reference fails with a static ``validation`` error that
  never echoes the requested id.

The approved chain, the synthetic planner/writer runtime mappings, the blueprint
content, and the owner-gate approval come from the real integrated fixtures in
:mod:`test_drafting_workflow` (which inherits the accepted Step 09/10 seams and
the mocked controlling-tty owner-authorization seam). No model, network,
provider, session, or runner call is made.

Run::

    PYTHONPATH=src:tests python3 -m unittest test_cli_drafting -v
"""

from __future__ import annotations

import io
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))
_TESTS = Path(__file__).resolve().parent
if str(_TESTS) not in sys.path:
    sys.path.insert(0, str(_TESTS))

import review_article_agent.cli.drafting as cli_drafting  # noqa: E402
import review_article_agent.cli.main as cli_main  # noqa: E402
from review_article_agent.drafting.package import DraftingPackageRepository  # noqa: E402
from review_article_agent.drafting.workflow import (  # noqa: E402
    GATE_ID,
    GATE_KIND,
)
from review_article_agent.storage import Registry  # noqa: E402
from test_drafting_workflow import blueprint_content  # noqa: E402
from test_drafting_workspace import WorkspaceBase  # noqa: E402

#: Private values carried only in the runtime mapping pin (never by default).
_SECRET_SESSION = "ses_planner_1"
_SECRET_MODEL = "deepseek/v4.1-flash"
#: A raw blueprint claim text (never emitted by default).
_SECRET_CLAIM_TEXT = "Compound Z did not reduce tumor volume in mice."
#: Raw draft/assembly content (never emitted by default; only with --content).
_SECRET_DRAFT_TEXT = "SECRET-DRAFT-SENTENCE-TEXT"
_SECRET_MARKDOWN = "# SECRET-ASSEMBLY-MARKDOWN"
_SECRET_CITATION_TITLE = "SECRET-CITATION-TITLE"


class DraftingCliBase(WorkspaceBase):
    """Drafting fixture plus a registry the CLI can open it through."""

    def setUp(self) -> None:
        super().setUp()
        self.state = self.tmp / "state"
        self.registry = Registry(self.state)
        self.root = self.tmp / "project"
        self.registry.register_restored(str(self.root), project_id=self.project_id)

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

    def cli_propose(self, kind: str, payload: dict, *, artifact_id: str, revision: str,
                    parent: str | None = None, expect: int = 0) -> dict:
        payload_file = self.write_json(f"{kind}-input.json", payload)
        rights = self.write_json(
            f"{kind}-rights.json", self.rights(artifact_id, revision)
        )
        args = [
            "drafting", "propose", "--project", self.project_id,
            "--kind", kind, "--input-file", payload_file, "--rights-file", rights,
            "--artifact-id", artifact_id, "--revision-id", revision,
        ]
        if parent is not None:
            args += ["--parent-revision-id", parent]
        return self.cli(*args, expect=expect)

    def workspace_fixture(self):
        _s, _f, outline_ref = self.approved_chain()
        planner = self.planner_mapping()
        writer = self.writer_mapping()
        return self.payload(outline_ref, planner=planner, writer=writer), outline_ref

    def make_workspace(self, outline_ref, *, section_id="sec1", artifact_id="ws",
                       revision="ws-r1"):
        return self.workspace.propose(
            self.payload(
                outline_ref, section_id=section_id,
                planner=self.planner_mapping(), writer=self.writer_mapping(),
            ),
            artifact_id=artifact_id, revision_id=revision,
            rights=self.rights(artifact_id, revision),
        )

    def blueprint_payload(self, workspace_ref, *, section_id="sec1"):
        ws = self.workspace.read(workspace_ref)["result"]
        package = DraftingPackageRepository(self.store).build(
            ws["outline_ref"], section_id
        )
        finding_key = package["evidence"][0]["finding_key"]
        return {
            "workspace_ref": workspace_ref,
            "content": blueprint_content(section_id=section_id, finding_key=finding_key),
        }

    def assert_non_authorizing(self, data: dict) -> None:
        self.assertFalse(data["authorizes_execution"])
        self.assertFalse(data["authorizes_export"])
        self.assertFalse(data["scientific_validation"])
        self.assertEqual(data["status"], "provisional")


class ParserRegistrationTest(DraftingCliBase):
    def test_group_registered_and_subcommands_enforced(self):
        parser = cli_main.build_parser()
        self.assertIn("drafting", parser.format_help())
        parsed = parser.parse_args(
            [
                "drafting", "package", "--outline-artifact-id", "o",
                "--outline-revision-id", "r", "--section-id", "s",
            ]
        )
        self.assertEqual(parsed.drafting_command, "package")
        accepted = parser.parse_args(
            ["drafting", "accepted", "--artifact-id", "a", "--revision-id", "r"]
        )
        self.assertEqual(accepted.drafting_command, "accepted")
        status = parser.parse_args(
            ["drafting", "status", "--kind", "blueprint",
             "--artifact-id", "a", "--revision-id", "r"]
        )
        self.assertEqual(status.drafting_command, "status")
        # workspace/blueprint/draft/assembly are all valid propose/show kinds now
        for kind in ("workspace", "blueprint", "draft", "assembly"):
            parsed_kind = parser.parse_args(
                ["drafting", "propose", "--kind", kind, "--input-file", "f",
                 "--rights-file", "r", "--artifact-id", "a", "--revision-id", "rev"]
            )
            self.assertEqual(parsed_kind.kind, kind)
        # status accepts the workflow-backed kinds
        for kind in ("blueprint", "draft", "assembly"):
            parsed_status = parser.parse_args(
                ["drafting", "status", "--kind", kind,
                 "--artifact-id", "a", "--revision-id", "r"]
            )
            self.assertEqual(parsed_status.kind, kind)
        with self.assertRaises(cli_main.CliUsageError):
            parser.parse_args(["drafting", "show"])  # required options missing
        with self.assertRaises(cli_main.CliUsageError):
            parser.parse_args(["drafting", "propose", "--kind", "manuscript"])
        with self.assertRaises(cli_main.CliUsageError):
            parser.parse_args(
                ["drafting", "status", "--kind", "workspace",
                 "--artifact-id", "a", "--revision-id", "r"]
            )
        # the group alone is a usage error, not an internal failure
        self.assertEqual(self._run("drafting")[0], cli_main.EXIT_USAGE)


class WorkspaceProposeShowTest(DraftingCliBase):
    def test_propose_inert_and_show_privacy_and_content_opt_in(self):
        payload, outline_ref = self.workspace_fixture()
        proposed = self.cli_propose("workspace", payload, artifact_id="ws", revision="ws-r1")
        self.assertEqual(proposed["ref"]["artifact_id"], "ws")
        self.assertEqual(proposed["kind"], "workspace")
        self.assertFalse(proposed["private_content_included"])
        self.assert_non_authorizing(proposed)
        self.assertNotIn(_SECRET_SESSION, json.dumps(proposed))

        show = self.cli(
            "drafting", "show", "--project", self.project_id,
            "--kind", "workspace", "--artifact-id", "ws", "--revision-id", "ws-r1",
        )
        self.assertEqual(show["section_id"], "sec1")
        self.assertEqual(show["outline_ref"], outline_ref)
        self.assertTrue(show["distinct_sessions"])
        self.assertTrue(show["current"])
        self.assertFalse(show["private_content_included"])
        self.assertNotIn("payload", show)
        self.assert_non_authorizing(show)
        blob = json.dumps(show)
        self.assertNotIn(_SECRET_SESSION, blob)
        self.assertNotIn(_SECRET_MODEL, blob)

        content = self.cli(
            "drafting", "show", "--project", self.project_id,
            "--kind", "workspace", "--artifact-id", "ws", "--revision-id", "ws-r1",
            "--content",
        )
        self.assertTrue(content["private_content_included"])
        self.assertIn(_SECRET_SESSION, json.dumps(content))
        self.assert_non_authorizing(content)

    def test_show_historical_flag_reads_without_current_authority(self):
        payload, _outline_ref = self.workspace_fixture()
        self.cli_propose("workspace", payload, artifact_id="ws", revision="ws-r1")
        historical = self.cli(
            "drafting", "show", "--project", self.project_id,
            "--kind", "workspace", "--artifact-id", "ws", "--revision-id", "ws-r1",
            "--historical",
        )
        self.assertFalse(historical["current"])
        self.assert_non_authorizing(historical)


class PackageTest(DraftingCliBase):
    def test_package_safe_counts_and_content_opt_in(self):
        _s, _f, outline_ref = self.approved_chain()
        summary = self.cli(
            "drafting", "package", "--project", self.project_id,
            "--outline-artifact-id", outline_ref["artifact_id"],
            "--outline-revision-id", outline_ref["revision_id"],
            "--section-id", "sec1",
        )
        self.assertEqual(summary["document_type"], "drafting_package")
        self.assertEqual(summary["section_id"], "sec1")
        self.assertEqual(summary["outline_ref"], outline_ref)
        self.assertEqual(summary["evidence_count"], 1)
        self.assertEqual(summary["citation_count"], 1)
        self.assertEqual(summary["authority"], "named_gate_only")
        self.assertFalse(summary["private_content_included"])
        self.assertNotIn("package", summary)
        self.assert_non_authorizing(summary)

        full = self.cli(
            "drafting", "package", "--project", self.project_id,
            "--outline-artifact-id", outline_ref["artifact_id"],
            "--outline-revision-id", outline_ref["revision_id"],
            "--section-id", "sec1", "--content",
        )
        self.assertTrue(full["private_content_included"])
        self.assertEqual(full["package"]["section"]["section_id"], "sec1")
        self.assert_non_authorizing(full)


class BlueprintProposeShowTest(DraftingCliBase):
    def test_blueprint_propose_show_privacy_gate_and_historical(self):
        _s, _f, outline_ref = self.approved_chain()
        ws_ref = self.make_workspace(outline_ref)
        proposed = self.cli_propose(
            "blueprint", self.blueprint_payload(ws_ref),
            artifact_id="bp", revision="bp-r1",
        )
        self.assertEqual(proposed["ref"]["artifact_id"], "bp")
        self.assertEqual(proposed["kind"], "blueprint")
        self.assertEqual(proposed["gate_kind"], GATE_KIND)
        self.assertEqual(proposed["gate_id"], GATE_ID)
        self.assertFalse(proposed["private_content_included"])
        self.assert_non_authorizing(proposed)

        show = self.cli(
            "drafting", "show", "--project", self.project_id,
            "--kind", "blueprint", "--artifact-id", "bp", "--revision-id", "bp-r1",
        )
        self.assertEqual(show["section_id"], "sec1")
        self.assertEqual(show["gate_kind"], GATE_KIND)
        self.assertEqual(show["gate_id"], GATE_ID)
        self.assertEqual(show["paragraph_count"], 1)
        self.assertEqual(show["intent_count"], 1)
        self.assertEqual(show["claim_count"], 1)
        self.assertEqual(show["pending"], ["source_checks", "origin_tracing"])
        self.assertTrue(show["current"])
        self.assertFalse(show["private_content_included"])
        self.assertNotIn("payload", show)
        self.assert_non_authorizing(show)
        blob = json.dumps(show)
        self.assertNotIn(_SECRET_CLAIM_TEXT, blob)
        self.assertNotIn(_SECRET_SESSION, blob)
        self.assertNotIn("package", show)

        content = self.cli(
            "drafting", "show", "--project", self.project_id,
            "--kind", "blueprint", "--artifact-id", "bp", "--revision-id", "bp-r1",
            "--content",
        )
        self.assertTrue(content["private_content_included"])
        self.assertIn(_SECRET_CLAIM_TEXT, json.dumps(content))
        self.assert_non_authorizing(content)

        historical = self.cli(
            "drafting", "show", "--project", self.project_id,
            "--kind", "blueprint", "--artifact-id", "bp", "--revision-id", "bp-r1",
            "--historical",
        )
        self.assertFalse(historical["current"])
        self.assertEqual(historical["gate_kind"], GATE_KIND)
        self.assert_non_authorizing(historical)


class AcceptedTest(DraftingCliBase):
    def _make_blueprint(self):
        _s, _f, outline_ref = self.approved_chain()
        ws_ref = self.make_workspace(outline_ref)
        return self.cli_propose(
            "blueprint", self.blueprint_payload(ws_ref),
            artifact_id="bp", revision="bp-r1",
        )["ref"]

    def test_accepted_requires_own_gate_then_reads_approved(self):
        ref = self._make_blueprint()
        # Not approved yet: the own-gate requirement fails closed.
        error = self.cli(
            "drafting", "accepted", "--project", self.project_id,
            "--artifact-id", "bp", "--revision-id", "bp-r1",
            expect=cli_main.EXIT_VALIDATION,
        )
        self.assertEqual(error["error"]["category"], "validation")
        self.assertNotIn("bp-r1", json.dumps(error))
        self.assertNotIn("Traceback", json.dumps(error))

        # Record the human approval out-of-band (real owner-gate flow).
        self.approve_ref(ref, GATE_KIND, GATE_ID)

        approved = self.cli(
            "drafting", "accepted", "--project", self.project_id,
            "--artifact-id", "bp", "--revision-id", "bp-r1",
        )
        self.assertTrue(approved["blueprint_approved"])
        self.assertEqual(approved["gate_kind"], GATE_KIND)
        self.assertEqual(approved["gate_id"], GATE_ID)
        self.assertEqual(approved["blueprint_ref"], ref)
        self.assertEqual(approved["section_id"], "sec1")
        self.assertEqual(approved["claim_count"], 1)
        self.assertEqual(approved["pending"], ["source_checks", "origin_tracing"])
        self.assertFalse(approved["private_content_included"])
        self.assertNotIn("blueprint", approved)
        self.assert_non_authorizing(approved)

        full = self.cli(
            "drafting", "accepted", "--project", self.project_id,
            "--artifact-id", "bp", "--revision-id", "bp-r1", "--content",
        )
        self.assertTrue(full["private_content_included"])
        self.assertIn(_SECRET_CLAIM_TEXT, json.dumps(full))
        self.assert_non_authorizing(full)


class StatusTest(DraftingCliBase):
    def test_status_reports_stale_then_not_stale(self):
        _s, _f, outline_ref = self.approved_chain()
        ws_ref = self.make_workspace(outline_ref)
        ref = self.cli_propose(
            "blueprint", self.blueprint_payload(ws_ref),
            artifact_id="bp", revision="bp-r1",
        )["ref"]

        stale = self.cli(
            "drafting", "status", "--project", self.project_id,
            "--kind", "blueprint", "--artifact-id", "bp", "--revision-id", "bp-r1",
        )
        self.assertTrue(stale["stale"])
        self.assertTrue(stale["reasons"])
        self.assertEqual(stale["gate_kind"], GATE_KIND)
        self.assertEqual(stale["gate_id"], GATE_ID)
        self.assertEqual(stale["pending"], ["source_checks", "origin_tracing"])
        self.assertTrue(stale["provisional"])
        self.assertFalse(stale["authorizes_export"])
        self.assert_non_authorizing(stale)

        self.approve_ref(ref, GATE_KIND, GATE_ID)
        fresh = self.cli(
            "drafting", "status", "--project", self.project_id,
            "--kind", "blueprint", "--artifact-id", "bp", "--revision-id", "bp-r1",
        )
        self.assertFalse(fresh["stale"])
        self.assertEqual(fresh["reasons"], [])
        self.assert_non_authorizing(fresh)


class DraftAssemblyViewTest(DraftingCliBase):
    """Draft/assembly show/status privacy and content opt-in (mock-backed)."""

    def _fake_workflow(self):
        class FakeWorkflow:
            def __init__(self, store):
                self.store = store

            def propose(self, kind, payload, *, artifact_id, revision_id, rights,
                        parent_revision_id=None):
                return {
                    "project_id": self.store.project_id,
                    "artifact_id": artifact_id, "revision_id": revision_id,
                }

            def read(self, ref, kind="blueprint", *, current=True):
                if kind == "draft":
                    return {"result": {
                        "section_id": "sec1",
                        "paragraphs": [{"paragraph_id": "P1", "sentences": [
                            {"intent_id": "I1", "text": _SECRET_DRAFT_TEXT}]}],
                        "citations": {"cit_1": {"title": _SECRET_CITATION_TITLE}},
                        "pending": ["source_checks", "origin_tracing"],
                    }}
                return {"result": {
                    "sections": [{"section_id": "sec1", "paragraphs": [
                        {"paragraph_id": "P1", "sentences": [
                            {"intent_id": "I1", "text": _SECRET_DRAFT_TEXT}]}]}],
                    "citations": {"cit_1": {"title": _SECRET_CITATION_TITLE}},
                    "markdown": _SECRET_MARKDOWN,
                    "pending": ["source_checks", "origin_tracing"],
                }}

            def status(self, ref, kind="blueprint"):
                if kind == "draft":
                    return {"kind": "draft", "draft_ref": ref,
                            "blueprint_ref": {**ref, "artifact_id": "bp"},
                            "stale": True, "reasons": ["r"], "pending": [],
                            "provisional": True, "authorizes_export": False,
                            "final": False, "scientific_validation": False}
                return {"kind": "assembly", "assembly_ref": ref,
                        "outline_ref": {**ref, "artifact_id": "out"},
                        "stale": False, "reasons": [], "pending": [],
                        "provisional": True, "authorizes_export": False,
                        "final": False, "scientific_validation": False}

        return FakeWorkflow

    def test_draft_show_safe_counts_and_content_opt_in(self):
        with mock.patch.object(
            cli_drafting, "DraftingWorkflowRepository", self._fake_workflow()
        ):
            proposed = self.cli_propose(
                "draft", {"blueprint_ref": "x", "content": {}},
                artifact_id="draft", revision="draft-r1",
            )
            self.assertEqual(proposed["kind"], "draft")
            self.assertNotIn("gate_kind", proposed)
            self.assert_non_authorizing(proposed)

            show = self.cli(
                "drafting", "show", "--project", self.project_id,
                "--kind", "draft", "--artifact-id", "draft",
                "--revision-id", "draft-r1",
            )
            self.assertEqual(show["section_id"], "sec1")
            self.assertEqual(show["paragraph_count"], 1)
            self.assertEqual(show["sentence_count"], 1)
            self.assertEqual(show["citation_count"], 1)
            self.assertEqual(show["pending"], ["source_checks", "origin_tracing"])
            self.assertNotIn("gate_kind", show)
            self.assertNotIn("payload", show)
            self.assert_non_authorizing(show)
            blob = json.dumps(show)
            self.assertNotIn(_SECRET_DRAFT_TEXT, blob)
            self.assertNotIn(_SECRET_CITATION_TITLE, blob)

            content = self.cli(
                "drafting", "show", "--project", self.project_id,
                "--kind", "draft", "--artifact-id", "draft",
                "--revision-id", "draft-r1", "--content",
            )
            self.assertTrue(content["private_content_included"])
            self.assertIn(_SECRET_DRAFT_TEXT, json.dumps(content))

    def test_assembly_show_safe_counts_and_content_opt_in(self):
        with mock.patch.object(
            cli_drafting, "DraftingWorkflowRepository", self._fake_workflow()
        ):
            proposed = self.cli_propose(
                "assembly", {"outline_ref": "x", "draft_refs": ["y"]},
                artifact_id="assembly", revision="assembly-r1",
            )
            self.assertEqual(proposed["kind"], "assembly")
            self.assertNotIn("gate_kind", proposed)
            self.assert_non_authorizing(proposed)

            show = self.cli(
                "drafting", "show", "--project", self.project_id,
                "--kind", "assembly", "--artifact-id", "assembly",
                "--revision-id", "assembly-r1",
            )
            self.assertEqual(show["section_count"], 1)
            self.assertEqual(show["paragraph_count"], 1)
            self.assertEqual(show["sentence_count"], 1)
            self.assertEqual(show["citation_count"], 1)
            self.assertEqual(show["pending"], ["source_checks", "origin_tracing"])
            self.assertNotIn("section_id", show)
            self.assertNotIn("payload", show)
            self.assert_non_authorizing(show)
            blob = json.dumps(show)
            self.assertNotIn(_SECRET_MARKDOWN, blob)
            self.assertNotIn(_SECRET_DRAFT_TEXT, blob)
            self.assertNotIn(_SECRET_CITATION_TITLE, blob)

            content = self.cli(
                "drafting", "show", "--project", self.project_id,
                "--kind", "assembly", "--artifact-id", "assembly",
                "--revision-id", "assembly-r1", "--content",
            )
            self.assertTrue(content["private_content_included"])
            self.assertIn(_SECRET_MARKDOWN, json.dumps(content))

    def test_status_keyed_refs_per_kind_without_keyerror(self):
        with mock.patch.object(
            cli_drafting, "DraftingWorkflowRepository", self._fake_workflow()
        ):
            draft = self.cli(
                "drafting", "status", "--project", self.project_id,
                "--kind", "draft", "--artifact-id", "draft",
                "--revision-id", "draft-r1",
            )
            self.assertEqual(draft["draft_ref"]["artifact_id"], "draft")
            self.assertIn("blueprint_ref", draft)
            self.assertTrue(draft["stale"])
            self.assert_non_authorizing(draft)

            assembly = self.cli(
                "drafting", "status", "--project", self.project_id,
                "--kind", "assembly", "--artifact-id", "assembly",
                "--revision-id", "assembly-r1",
            )
            self.assertEqual(assembly["assembly_ref"]["artifact_id"], "assembly")
            self.assertIn("outline_ref", assembly)
            self.assertFalse(assembly["stale"])
            self.assert_non_authorizing(assembly)


class ErrorRedactionTest(DraftingCliBase):
    def test_missing_workspace_revision_is_static_validation_error(self):
        self.workspace_fixture()
        error = self.cli(
            "drafting", "show", "--project", self.project_id,
            "--kind", "workspace", "--artifact-id", "no-such-artifact",
            "--revision-id", "no-such-rev",
            expect=cli_main.EXIT_VALIDATION,
        )
        self.assertEqual(error["error"]["category"], "validation")
        blob = json.dumps(error)
        self.assertNotIn("no-such-artifact", blob)
        self.assertNotIn("no-such-rev", blob)
        self.assertNotIn("Traceback", blob)

    def test_missing_blueprint_revision_is_static_validation_error(self):
        self.approved_chain()
        error = self.cli(
            "drafting", "show", "--project", self.project_id,
            "--kind", "blueprint", "--artifact-id", "no-such-bp",
            "--revision-id", "no-such-rev",
            expect=cli_main.EXIT_VALIDATION,
        )
        self.assertEqual(error["error"]["category"], "validation")
        self.assertNotIn("no-such-bp", json.dumps(error))
        self.assertNotIn("Traceback", json.dumps(error))

    def test_package_missing_outline_is_static_validation_error(self):
        self.approved_chain()
        error = self.cli(
            "drafting", "package", "--project", self.project_id,
            "--outline-artifact-id", "no-such-outline",
            "--outline-revision-id", "no-such-rev", "--section-id", "sec1",
            expect=cli_main.EXIT_VALIDATION,
        )
        self.assertEqual(error["error"]["category"], "validation")
        self.assertNotIn("no-such-outline", json.dumps(error))
        self.assertNotIn("Traceback", json.dumps(error))


class DelegationTest(DraftingCliBase):
    """The CLI delegates to the repositories; it never fabricates a mapping."""

    def test_workspace_propose_and_read_delegate_exact_arguments(self):
        payload, _outline_ref = self.workspace_fixture()
        recorded: dict = {}

        class FakeWorkspace:
            def __init__(self, store):
                self.store = store

            def propose(self, payload, *, artifact_id, revision_id, rights,
                        parent_revision_id=None):
                recorded["propose"] = {
                    "payload": payload, "artifact_id": artifact_id,
                    "revision_id": revision_id, "rights": rights,
                    "parent_revision_id": parent_revision_id,
                }
                return {
                    "project_id": self.store.project_id,
                    "artifact_id": artifact_id, "revision_id": revision_id,
                }

            def read(self, ref, current=True):
                recorded["read"] = {"ref": ref, "current": current}
                return {
                    "input": {},
                    "result": {
                        "section_id": "sec1", "outline_ref": ref,
                        "planner": {"role": "synthesizer"},
                        "writer": {"role": "writer"},
                        "distinct_sessions": True, "grants_execution": False,
                    },
                }

        payload_file = self.write_json("d-input.json", payload)
        rights_file = self.write_json("d-rights.json", self.rights("ws", "ws-r1"))
        with mock.patch.object(cli_drafting, "SectionWorkspaceRepository", FakeWorkspace):
            self.cli(
                "drafting", "propose", "--project", self.project_id,
                "--kind", "workspace", "--input-file", payload_file,
                "--rights-file", rights_file, "--artifact-id", "ws",
                "--revision-id", "ws-r1", "--parent-revision-id", "ws-r0",
            )
            self.cli(
                "drafting", "show", "--project", self.project_id,
                "--kind", "workspace", "--artifact-id", "ws",
                "--revision-id", "ws-r1", "--historical",
            )

        self.assertEqual(recorded["propose"]["artifact_id"], "ws")
        self.assertEqual(recorded["propose"]["revision_id"], "ws-r1")
        self.assertEqual(recorded["propose"]["parent_revision_id"], "ws-r0")
        self.assertEqual(recorded["propose"]["payload"], payload)
        self.assertIsInstance(recorded["propose"]["rights"], dict)
        self.assertEqual(recorded["read"]["current"], False)
        self.assertEqual(recorded["read"]["ref"], {
            "project_id": self.project_id, "artifact_id": "ws", "revision_id": "ws-r1",
        })

    def test_blueprint_propose_read_accepted_status_delegate(self):
        payload, _outline_ref = self.workspace_fixture()
        recorded: dict = {}

        class FakeWorkflow:
            def __init__(self, store):
                self.store = store

            def propose(self, kind, payload, *, artifact_id, revision_id, rights,
                        parent_revision_id=None):
                recorded.setdefault("proposes", []).append({
                    "kind": kind, "payload": payload, "artifact_id": artifact_id,
                    "revision_id": revision_id, "rights": rights,
                    "parent_revision_id": parent_revision_id,
                })
                return {
                    "project_id": self.store.project_id,
                    "artifact_id": artifact_id, "revision_id": revision_id,
                }

            def read(self, ref, kind="blueprint", *, current=True):
                recorded.setdefault("reads", []).append(
                    {"ref": ref, "kind": kind, "current": current}
                )
                if kind == "draft":
                    result = {"section_id": "sec1", "paragraphs": [], "citations": {}}
                elif kind == "assembly":
                    result = {"sections": [], "citations": {}}
                else:
                    result = {"section_id": "sec1", "paragraphs": []}
                result["pending"] = ["source_checks", "origin_tracing"]
                return {"result": result}

            def require_blueprint(self, ref):
                recorded["accepted"] = ref
                return {
                    "blueprint_ref": ref, "gate_kind": GATE_KIND, "gate_id": GATE_ID,
                    "approved": True, "input": {}, "workspace": {}, "package": {},
                    "result": {"section_id": "sec1", "paragraphs": [], "pending": []},
                }

            def status(self, ref, kind="blueprint"):
                recorded["status"] = {"ref": ref, "kind": kind}
                common = {
                    "stale": False, "reasons": [], "pending": [],
                    "provisional": True, "authorizes_export": False,
                }
                if kind == "draft":
                    return {**common, "kind": "draft", "draft_ref": ref,
                            "blueprint_ref": {**ref, "artifact_id": "bp"},
                            "final": False, "scientific_validation": False}
                if kind == "assembly":
                    return {**common, "kind": "assembly", "assembly_ref": ref,
                            "outline_ref": {**ref, "artifact_id": "out"},
                            "final": False, "scientific_validation": False}
                return {**common, "kind": "blueprint", "blueprint_ref": ref,
                        "gate_kind": GATE_KIND, "gate_id": GATE_ID}

        payload_file = self.write_json("bp-input.json", payload)
        rights_file = self.write_json("bp-rights.json", self.rights("bp", "bp-r1"))
        with mock.patch.object(cli_drafting, "DraftingWorkflowRepository", FakeWorkflow):
            self.cli(
                "drafting", "propose", "--project", self.project_id,
                "--kind", "blueprint", "--input-file", payload_file,
                "--rights-file", rights_file, "--artifact-id", "bp",
                "--revision-id", "bp-r1",
            )
            self.cli(
                "drafting", "show", "--project", self.project_id,
                "--kind", "blueprint", "--artifact-id", "bp",
                "--revision-id", "bp-r1", "--historical",
            )
            self.cli(
                "drafting", "accepted", "--project", self.project_id,
                "--artifact-id", "bp", "--revision-id", "bp-r1",
            )
            self.cli(
                "drafting", "status", "--project", self.project_id,
                "--kind", "blueprint", "--artifact-id", "bp",
                "--revision-id", "bp-r1",
            )
            # draft/assembly pass args.kind straight through
            for kind in ("draft", "assembly"):
                self.cli(
                    "drafting", "propose", "--project", self.project_id,
                    "--kind", kind, "--input-file", payload_file,
                    "--rights-file", rights_file, "--artifact-id", kind,
                    "--revision-id", f"{kind}-r1",
                )
                self.cli(
                    "drafting", "show", "--project", self.project_id,
                    "--kind", kind, "--artifact-id", kind,
                    "--revision-id", f"{kind}-r1",
                )
                self.cli(
                    "drafting", "status", "--project", self.project_id,
                    "--kind", kind, "--artifact-id", kind,
                    "--revision-id", f"{kind}-r1",
                )

        proposes = {entry["kind"]: entry for entry in recorded["proposes"]}
        self.assertEqual(set(proposes), {"blueprint", "draft", "assembly"})
        self.assertEqual(proposes["blueprint"]["payload"], payload)
        self.assertEqual(proposes["blueprint"]["artifact_id"], "bp")
        self.assertEqual(proposes["draft"]["payload"], payload)
        self.assertEqual(proposes["assembly"]["payload"], payload)
        self.assertEqual(recorded["accepted"], {
            "project_id": self.project_id, "artifact_id": "bp", "revision_id": "bp-r1",
        })
        reads = {entry["kind"]: entry for entry in recorded["reads"]}
        self.assertEqual(reads["blueprint"]["current"], False)
        self.assertEqual(reads["blueprint"]["ref"]["revision_id"], "bp-r1")
        self.assertEqual(reads["draft"]["ref"]["artifact_id"], "draft")
        self.assertEqual(reads["assembly"]["ref"]["artifact_id"], "assembly")
        self.assertEqual(recorded["status"]["kind"], "assembly")

    def test_package_delegates_exact_outline_ref_and_section(self):
        _s, _f, outline_ref = self.approved_chain()
        recorded: dict = {}

        class FakePackage:
            def __init__(self, store):
                self.store = store

            def build(self, outline_ref, section_id):
                recorded["build"] = {"outline_ref": outline_ref, "section_id": section_id}
                return {
                    "outline_ref": outline_ref, "section_id": section_id,
                    "document_type": "drafting_package", "evidence": [],
                    "citations": {}, "methods": {"original_sources": []},
                    "limitations": [], "authority": "named_gate_only",
                }

        with mock.patch.object(cli_drafting, "DraftingPackageRepository", FakePackage):
            summary = self.cli(
                "drafting", "package", "--project", self.project_id,
                "--outline-artifact-id", outline_ref["artifact_id"],
                "--outline-revision-id", outline_ref["revision_id"],
                "--section-id", "sec2",
            )

        self.assertEqual(recorded["build"]["outline_ref"], outline_ref)
        self.assertEqual(recorded["build"]["section_id"], "sec2")
        self.assertEqual(summary["evidence_count"], 0)
        self.assert_non_authorizing(summary)


if __name__ == "__main__":
    unittest.main()
