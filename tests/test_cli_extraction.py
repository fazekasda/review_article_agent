"""In-process CLI tests for the ``extraction`` command group (Step 09, S9-CLI).

Covers ``src/review_article_agent/cli/extraction.py`` over the integrated
``ExtractionWorkflowRepository``:

* the ``extraction`` group is registered on the top-level parser (and its
  ``propose``/``show``/``accepted`` subcommands enforce their required options);
* ``extraction propose`` writes one inert ``config`` revision from explicit ids
  and an explicit caller rights record -- ``approved`` is always false;
* ``extraction show`` reads one exact current revision -- a safe summary by
  default that emits **no** prompt instructions or model configuration, and the
  private payload only with ``--content``;
* ``extraction accepted`` runs the synthesis gate -- ids/status/counts/guarantee
  by default, the accepted evidence detail only with ``--content``;
* a missing/malformed reference fails with a static ``validation`` error that
  never echoes the requested id.

Synthetic data only: a real temporary registry and project store. No network,
model, provider, approval, or runner call is performed (the synthesis gate is
patched with a synthetic accepted result). This is a minimal MVP and makes no
scientific-correctness or full Step 09 claim.

Run::

    PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_cli_extraction.py'
"""

from __future__ import annotations

import io
import json
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

import review_article_agent.cli.main as cli_main  # noqa: E402
from review_article_agent.extraction import workflow  # noqa: E402
from review_article_agent.parsing.core import NORMALIZATION_VERSION, PARSER_VERSION  # noqa: E402
from review_article_agent.storage import ProjectStore, Registry, paths  # noqa: E402
from test_discovery_freeze import _rights  # noqa: E402
from test_extraction_workflow import EXTRACTION_MODEL, QUALITY_MODEL  # noqa: E402

_SECRET_EXTRACT = "PRIVATE-EXTRACTION-INSTRUCTIONS"
_SECRET_QUALITY = "PRIVATE-QUALITY-INSTRUCTIONS"


def _config() -> dict:
    return {
        "config_schema_version": 1,
        "appraisal_dimensions": [{"dimension_id": "risk_of_bias"}],
        "pilot_size": 1,
        "random_size": 1,
        "risk_size": 1,
        "acceptance_rule": "No important errors; inspect all pilot sources.",
        "extraction_model": dict(EXTRACTION_MODEL),
        "quality_model": dict(QUALITY_MODEL),
        "extraction_instructions": _SECRET_EXTRACT,
        "extraction_instructions_revision": "extract-v1",
        "quality_instructions": _SECRET_QUALITY,
        "quality_instructions_revision": "quality-v1",
        "parser": PARSER_VERSION,
        "normalization": NORMALIZATION_VERSION,
    }


def _synthesis_result(ref) -> dict:
    project = ref["project_id"]

    def r(artifact: str, revision: str) -> dict:
        return {"project_id": project, "artifact_id": artifact, "revision_id": revision}

    return {
        "accepted": True,
        "audit_ref": ref,
        "batch_ref": r("batch", "batch-r1"),
        "config_ref": r("config", "config-r1"),
        "pilot_ref": r("pilot", "pilot-r1"),
        "acquisition_ref": r("acq", "acq-r1"),
        "case_ids": ["c1", "c2"],
        "audited_ids": ["c1"],
        "limitations": ["declared_omissions"],
        "escalations": ["none"],
        "gates_satisfied": ["extraction.outputs"],
        "guarantee": "sampled_audit_not_completeness",
        "authority": "named_gate_only",
        "notice": workflow.SYNTHESIS_NOTICE,
    }


class ExtractionCliBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="raa-cli-extraction-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.state = self.tmp / "state"
        self.registry = Registry(self.state)
        root = self.tmp / "project"
        info = self.registry.create_project(root, name="P")
        self.app_dir = paths.app_dir_for(str(root))
        self.store = ProjectStore.open_at(self.app_dir)
        self.project_id = self.store.project_id
        self.assertEqual(self.project_id, info.project_id)

    def tearDown(self) -> None:
        try:
            self.store.close()
        except Exception:  # pragma: no cover - already closed
            pass

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

    def propose_config(self, expect: int = 0) -> dict:
        payload = self.write_json("config.json", _config())
        rights = self.write_json(
            "config-rights.json", _rights(self.project_id, "config", "config-r1")
        )
        return self.cli(
            "extraction", "propose", "--project", self.project_id,
            "--kind", "config", "--input-file", payload, "--rights-file", rights,
            "--artifact-id", "config", "--revision-id", "config-r1", expect=expect,
        )


class ParserRegistrationTest(ExtractionCliBase):
    def test_group_registered_and_subcommands_enforced(self):
        parser = cli_main.build_parser()
        self.assertIn("extraction", parser.format_help())
        accepted = parser.parse_args(
            ["extraction", "accepted", "--artifact-id", "a", "--revision-id", "r", "--project", "p"]
        )
        self.assertEqual(accepted.extraction_command, "accepted")
        with self.assertRaises(cli_main.CliUsageError):
            parser.parse_args(["extraction", "show"])  # required --kind/--artifact-id/--revision-id
        with self.assertRaises(cli_main.CliUsageError):
            parser.parse_args(["extraction", "propose", "--kind", "bogus"])
        # the group alone is a usage error, not an internal failure
        self.assertEqual(self._run("extraction")[0], cli_main.EXIT_USAGE)


class ProposeShowTest(ExtractionCliBase):
    def test_propose_inert_and_show_privacy(self):
        proposed = self.propose_config()
        self.assertEqual(proposed["ref"]["artifact_id"], "config")
        self.assertFalse(proposed["approved"])
        self.assertFalse(proposed["authorizes_extraction"])
        self.assertFalse(proposed["authorizes_synthesis"])

        show = self.cli(
            "extraction", "show", "--project", self.project_id,
            "--kind", "config", "--artifact-id", "config", "--revision-id", "config-r1",
        )
        self.assertEqual(show["dimension_count"], 1)
        self.assertEqual(show["dimension_ids"], ["risk_of_bias"])
        self.assertFalse(show["private_content_included"])
        blob = json.dumps(show)
        self.assertNotIn(_SECRET_EXTRACT, blob)
        self.assertNotIn(_SECRET_QUALITY, blob)
        self.assertNotIn("extraction_model", show)

        content = self.cli(
            "extraction", "show", "--project", self.project_id,
            "--kind", "config", "--artifact-id", "config", "--revision-id", "config-r1",
            "--content",
        )
        self.assertTrue(content["private_content_included"])
        self.assertIn(_SECRET_EXTRACT, json.dumps(content))

    def test_missing_reference_is_static_validation_error(self):
        self.propose_config()
        error = self.cli(
            "extraction", "show", "--project", self.project_id,
            "--kind", "config", "--artifact-id", "no-such-artifact", "--revision-id", "no-such-rev",
            expect=cli_main.EXIT_VALIDATION,
        )
        self.assertEqual(error["error"]["category"], "validation")
        self.assertNotIn("no-such-artifact", json.dumps(error))
        self.assertNotIn("Traceback", json.dumps(error))


class AcceptedTest(ExtractionCliBase):
    def _accepted(self, *, content: bool = False) -> dict:
        ref = {"project_id": self.project_id, "artifact_id": "audit", "revision_id": "audit-r1"}
        with mock.patch.object(
            workflow.ExtractionWorkflowRepository,
            "require_for_synthesis",
            return_value=_synthesis_result(ref),
        ):
            args = ["extraction", "accepted", "--project", self.project_id,
                    "--artifact-id", "audit", "--revision-id", "audit-r1"]
            if content:
                args.append("--content")
            return self.cli(*args)

    def test_default_summary_and_content_evidence(self):
        summary = self._accepted()
        self.assertTrue(summary["accepted"])
        self.assertEqual(summary["status"], "accepted")
        self.assertEqual(summary["case_count"], 2)
        self.assertEqual(summary["audited_count"], 1)
        self.assertEqual(summary["guarantee"], "sampled_audit_not_completeness")
        self.assertTrue(summary["authorizes_synthesis"])
        self.assertNotIn("case_ids", summary)
        self.assertNotIn("limitations", summary)

        full = self._accepted(content=True)
        self.assertTrue(full["private_content_included"])
        self.assertEqual(full["case_ids"], ["c1", "c2"])
        self.assertEqual(full["limitations"], ["declared_omissions"])
        self.assertEqual(full["escalations"], ["none"])


if __name__ == "__main__":
    unittest.main()
