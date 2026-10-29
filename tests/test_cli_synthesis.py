"""In-process CLI tests for the ``synthesis`` command group (Step 10, S10-CLI).

Covers ``src/review_article_agent/cli/synthesis.py`` over the integrated
``SynthesisWorkflowRepository``:

* the ``synthesis`` group is registered on the top-level parser (and its
  ``propose``/``show``/``accepted``/``impact`` subcommands enforce their required
  options);
* ``synthesis propose`` writes one inert revision from explicit ids and an
  explicit caller rights record -- ``approved`` is always false;
* ``synthesis show`` reads one exact current revision -- a safe structural
  count-only summary by default (never the private memo/statement text) that
  names the exact owner gate, and the private payload only with ``--content``;
* ``synthesis accepted`` runs the blueprint gate on an exact outline -- safe
  counts/approved refs by default, the full private package only with
  ``--content``; a missing outline gate fails closed;
* ``synthesis impact`` reads the stored advisory impact report -- safe impacted/
  unaffected section counts by default, the full report only with ``--content``;
* a missing/malformed reference fails with a static ``validation`` error that
  never echoes the requested id.

The approved chain comes from the real integrated fixture in
:mod:`test_synthesis_workflow` (``SynthesisBase.approved_chain``): the genuine
owner-capability gate flow and the inherited synthetic Step 09 receipt seam. No
model, network, provider, or runner call is made.

Run::

    PYTHONPATH=src:tests python3 -m unittest test_cli_synthesis -v
"""

from __future__ import annotations

import io
import json
import sys
import unittest
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))
_TESTS = Path(__file__).resolve().parent
if str(_TESTS) not in sys.path:
    sys.path.insert(0, str(_TESTS))

import review_article_agent.cli.main as cli_main  # noqa: E402
from review_article_agent.storage import Registry  # noqa: E402
from test_synthesis_workflow import SynthesisBase  # noqa: E402
from review_article_agent.synthesis.workflow import (  # noqa: E402
    GATE_FRAMEWORK,
    GATE_FRAMEWORK_KIND,
    GATE_OUTLINE_KIND,
    GATE_SYNTHESIS,
    GATE_SYNTHESIS_KIND,
)

_SECRET_MEMO = "PRIVATE-SYNTHESIS-MEMO"


class SynthesisCliBase(SynthesisBase):
    """Synthesis workflow fixture plus a registry the CLI can open it through."""

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
            "synthesis", "propose", "--project", self.project_id,
            "--kind", kind, "--input-file", payload_file, "--rights-file", rights,
            "--artifact-id", artifact_id, "--revision-id", revision,
        ]
        if parent is not None:
            args += ["--parent-revision-id", parent]
        return self.cli(*args, expect=expect)


class ParserRegistrationTest(SynthesisCliBase):
    def test_group_registered_and_subcommands_enforced(self):
        parser = cli_main.build_parser()
        self.assertIn("synthesis", parser.format_help())
        accepted = parser.parse_args(
            ["synthesis", "accepted", "--artifact-id", "a", "--revision-id", "r", "--project", "p"]
        )
        self.assertEqual(accepted.synthesis_command, "accepted")
        with self.assertRaises(cli_main.CliUsageError):
            parser.parse_args(["synthesis", "show"])  # required --kind/--artifact-id/--revision-id
        with self.assertRaises(cli_main.CliUsageError):
            parser.parse_args(["synthesis", "propose", "--kind", "bogus"])
        # the group alone is a usage error, not an internal failure
        self.assertEqual(self._run("synthesis")[0], cli_main.EXIT_USAGE)


class ProposeShowTest(SynthesisCliBase):
    def test_propose_inert_and_show_privacy_and_gate_metadata(self):
        audit_ref, _batch = self.accepted_audit()
        content = self.synthesis_content(memo=_SECRET_MEMO)
        proposed = self.cli_propose(
            "synthesis", {"audit_ref": audit_ref, "content": content},
            artifact_id="syn", revision="syn-r1",
        )
        self.assertEqual(proposed["ref"]["artifact_id"], "syn")
        self.assertEqual(proposed["kind"], "synthesis")
        self.assertFalse(proposed["approved"])
        self.assertFalse(proposed["authorizes_execution"])
        self.assertFalse(proposed["authorizes_export"])
        self.assertFalse(proposed["scientific_validation"])
        self.assertNotIn(_SECRET_MEMO, json.dumps(proposed))

        show = self.cli(
            "synthesis", "show", "--project", self.project_id,
            "--kind", "synthesis", "--artifact-id", "syn", "--revision-id", "syn-r1",
        )
        self.assertEqual(show["kind"], "synthesis")
        self.assertEqual(show["gate_kind"], GATE_SYNTHESIS_KIND)
        self.assertEqual(show["gate_id"], GATE_SYNTHESIS)
        self.assertEqual(show["statement_count"], 1)
        self.assertEqual(show["disposition_count"], 1)
        self.assertFalse(show["private_content_included"])
        self.assertNotIn("payload", show)
        blob = json.dumps(show)
        self.assertNotIn(_SECRET_MEMO, blob)
        self.assertNotIn("Compound Z reduced tumor volume", blob)

        content_out = self.cli(
            "synthesis", "show", "--project", self.project_id,
            "--kind", "synthesis", "--artifact-id", "syn", "--revision-id", "syn-r1",
            "--content",
        )
        self.assertTrue(content_out["private_content_included"])
        self.assertIn(_SECRET_MEMO, json.dumps(content_out))

    def test_missing_reference_is_static_validation_error(self):
        self.accepted_audit()
        error = self.cli(
            "synthesis", "show", "--project", self.project_id,
            "--kind", "synthesis", "--artifact-id", "no-such-artifact",
            "--revision-id", "no-such-rev",
            expect=cli_main.EXIT_VALIDATION,
        )
        self.assertEqual(error["error"]["category"], "validation")
        self.assertNotIn("no-such-artifact", json.dumps(error))
        self.assertNotIn("no-such-rev", json.dumps(error))
        self.assertNotIn("Traceback", json.dumps(error))


class AcceptedGateTest(SynthesisCliBase):
    def _chain_without_outline_gate(self):
        audit_ref, _batch = self.accepted_audit()
        synthesis_ref = self.put_synthesis(audit_ref)
        self.approve_ref(synthesis_ref, GATE_SYNTHESIS_KIND, GATE_SYNTHESIS)
        framework_ref = self.put_framework(synthesis_ref)
        self.approve_ref(framework_ref, GATE_FRAMEWORK_KIND, GATE_FRAMEWORK)
        outline_ref = self.put_outline(synthesis_ref, framework_ref)
        return outline_ref

    def test_accepted_real_approved_chain_counts_and_content_opt_in(self):
        _s, _f, outline_ref = self.approved_chain()
        summary = self.cli(
            "synthesis", "accepted", "--project", self.project_id,
            "--artifact-id", outline_ref["artifact_id"], "--revision-id", outline_ref["revision_id"],
        )
        self.assertTrue(summary["accepted"])
        self.assertEqual(summary["status"], "accepted")
        self.assertEqual(summary["outline_section_count"], 3)
        self.assertEqual(summary["framework_concept_count"], 2)
        self.assertEqual(summary["synthesis_statement_count"], 1)
        self.assertEqual(summary["authority"], "named_gate_only")
        self.assertFalse(summary["private_content_included"])
        self.assertNotIn("outline", summary)
        self.assertNotIn("synthesis", summary)

        full = self.cli(
            "synthesis", "accepted", "--project", self.project_id,
            "--artifact-id", outline_ref["artifact_id"], "--revision-id", outline_ref["revision_id"],
            "--content",
        )
        self.assertTrue(full["private_content_included"])
        self.assertEqual(full["outline"]["sections"][0]["section_id"], "sec1")
        self.assertEqual(full["synthesis"]["statements"][0]["statement_id"], "S-1")

    def test_accepted_missing_outline_gate_fails_closed(self):
        outline_ref = self._chain_without_outline_gate()
        error = self.cli(
            "synthesis", "accepted", "--project", self.project_id,
            "--artifact-id", outline_ref["artifact_id"], "--revision-id", outline_ref["revision_id"],
            expect=cli_main.EXIT_VALIDATION,
        )
        self.assertEqual(error["error"]["category"], "validation")
        self.assertNotIn(outline_ref["revision_id"], json.dumps(error))
        self.assertNotIn("Traceback", json.dumps(error))


class ImpactTest(SynthesisCliBase):
    def test_impact_default_counts_and_content_opt_in(self):
        synthesis_ref, framework_ref, _outline = self.approved_chain()
        edited = self.put_framework(
            synthesis_ref, revision="fw-r2", parent="fw-r1",
            c1_definition="A revised Compound Z definition.",
        )
        default = self.cli(
            "synthesis", "impact", "--project", self.project_id,
            "--kind", "framework", "--artifact-id", edited["artifact_id"],
            "--revision-id", edited["revision_id"],
        )
        self.assertTrue(default["impact_present"])
        self.assertFalse(default["private_content_included"])
        self.assertEqual(default["dependent_outline_count"], 1)
        self.assertGreaterEqual(default["affected_section_count"], 1)
        self.assertIn(GATE_FRAMEWORK, default["stale_gates"])
        self.assertNotIn("impact", default)
        self.assertNotIn("A revised Compound Z definition.", json.dumps(default))

        full = self.cli(
            "synthesis", "impact", "--project", self.project_id,
            "--kind", "framework", "--artifact-id", edited["artifact_id"],
            "--revision-id", edited["revision_id"], "--content",
        )
        self.assertTrue(full["private_content_included"])
        self.assertIn("C1", full["impact"]["changed"]["concepts"])

    def test_impact_absent_is_reported_without_error(self):
        synthesis_ref, _framework_ref, _outline = self.approved_chain()
        # The original framework revision has no parent and therefore no impact.
        result = self.cli(
            "synthesis", "impact", "--project", self.project_id,
            "--kind", "framework", "--artifact-id", "fw", "--revision-id", "fw-r1",
        )
        self.assertFalse(result["impact_present"])
        self.assertFalse(result["private_content_included"])


if __name__ == "__main__":
    unittest.main()
