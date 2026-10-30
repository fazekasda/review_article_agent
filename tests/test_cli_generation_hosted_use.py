"""In-process CLI acceptance tests for ``generation hosted-use`` (kickoff 16 §5).

Covers ``src/review_article_agent/cli/generation.py``'s new
``generation hosted-use propose``/``show`` sub-group over
``src/review_article_agent/generation/hosted_use.py``:

* ``propose`` strictly validates one ``generation_hosted_use`` document with
  ``validate_hosted_use_document`` (nothing is written on a malformed document),
  stores it through ``propose_hosted_use`` as an inert revision, and prints the
  exact ``gate prepare``/``gate confirm`` commands for the ``batch`` /
  ``generation.hosted_use.<role>`` gate on the new revision;
* ``show`` prints the validated document and whether its exact gate is satisfied;
* an unsatisfied gate becomes satisfied only after an owner-authenticated approval
  of that exact revision;
* a malformed document is a typed ``validation`` error with no write.

Real temp registry/store, a real owner-authenticated gate, and real rights records
are used; no worker and no network call are involved.

Run::

    PYTHONPATH=src:tests python3 -m unittest test_cli_generation_hosted_use
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
TESTS = Path(__file__).resolve().parent
for _path in (str(SRC), str(TESTS)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from review_article_agent.generation import hosted_use  # noqa: E402
from review_article_agent.storage.artifacts import ArtifactRepository  # noqa: E402

import test_cli_discovery_jobs as cli_fx  # noqa: E402

_CONFIG = {
    "model_id": "deepseek/deepseek-v4.1-flash",
    "upstream_provider": "deepseek",
    "max_output_tokens": 1024,
    "context_tokens": 128000,
    "input_price_per_million": "0.5",
    "output_price_per_million": "1.5",
    "pricing_ref": "pricing:synthetic:v1",
    "instructions_revision": "instr.v1",
    "timeout_seconds": 60.0,
    "max_response_bytes": 1048576,
}


class GenerationHostedUseCliTest(cli_fx.DiscoveryJobsCliBase):
    def _propose_args(
        self,
        pid: str,
        *,
        artifact_id: str = "a-hu",
        revision_id: str = "rev-hu",
        role: str = "global_writer",
        budget: str = "10.00",
    ) -> list[str]:
        return [
            "generation", "hosted-use", "propose", "--project", pid,
            "--role", role,
            "--config-file", str(self.write_json("config.json", _CONFIG)),
            "--budget-cap-usd", budget,
            "--provider-terms-ref", "terms:synthetic",
            "--privacy-review-ref", "privacy:synthetic",
            "--pricing-evidence-ref", "pricing:synthetic",
            "--artifact-id", artifact_id,
            "--revision-id", revision_id,
            "--rights-file", str(self.write_json(
                "rights.json",
                cli_fx._rights_record(pid, artifact_id, revision_id),
            )),
        ]

    def test_propose_then_show_gate_flow(self) -> None:
        pid = self.create_project()
        self.install_owner_auth()

        out = self.cli(*self._propose_args(pid))
        gate = out["gate"]
        self.assertEqual(gate["gate_kind"], "batch")
        self.assertEqual(gate["gate_id"], "generation.hosted_use.global_writer")
        self.assertFalse(gate["satisfied"])
        self.assertTrue(gate["current"])
        self.assertIn("gate prepare", gate["prepare_command"])
        self.assertIn("gate confirm", gate["confirm_command"])
        self.assertIn("--gate-kind batch", gate["prepare_command"])
        self.assertIn(
            "--gate-id generation.hosted_use.global_writer", gate["prepare_command"]
        )
        self.assertFalse(out["authorizes_execution"])
        self.assertFalse(out["approval_recorded"])

        # Show (gate unsatisfied).
        show = self.cli(
            "generation", "hosted-use", "show", "--project", pid,
            "--artifact-id", "a-hu",
        )
        self.assertEqual(show["document"]["payload"]["role"], "global_writer")
        self.assertEqual(show["document"]["payload"]["budget_cap_usd"], "10.00")
        self.assertEqual(show["revision"]["revision_id"], "rev-hu")
        self.assertFalse(show["gate_satisfied"])

        # Record the exact out-of-band gate via the test helper.
        self.approve(
            pid, "a-hu", "rev-hu", "batch", "generation.hosted_use.global_writer"
        )

        # Show (gate now satisfied).
        show2 = self.cli(
            "generation", "hosted-use", "show", "--project", pid,
            "--artifact-id", "a-hu",
        )
        self.assertTrue(show2["gate_satisfied"])
        self.assertFalse(show2["authorizes_execution"])

    def test_malformed_document_refused_without_write(self) -> None:
        pid = self.create_project()
        # A non-positive budget cap is refused by strict validation.
        self.cli(*self._propose_args(pid, budget="0"), expect=2)

        def _check(store):
            return ArtifactRepository(store).get_artifact("a-hu")

        self.assertIsNone(self.with_store(pid, _check))

    def test_unknown_role_refused(self) -> None:
        pid = self.create_project()
        args = self._propose_args(pid)
        args[args.index("--role") + 1] = "not_a_role"
        self.cli(*args, expect=2)

    def test_show_unknown_revision_refused(self) -> None:
        pid = self.create_project()
        self.cli(
            "generation", "hosted-use", "show", "--project", pid,
            "--artifact-id", "absent", expect=2,
        )

    def test_help_documents_hosted_use_commands(self) -> None:
        parser = cli_fx.cli_main.build_parser()
        import contextlib
        import io

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            with self.assertRaises(SystemExit):
                parser.parse_args(["generation", "hosted-use", "propose", "--help"])
        text = buf.getvalue()
        self.assertIn("--role", text)
        self.assertIn("--config-file", text)
        self.assertIn("--budget-cap-usd", text)
        self.assertIn("--rights-file", text)
        self.assertIn("--provider-terms-ref", text)

    def test_gate_id_helper_matches_prefix(self) -> None:
        self.assertEqual(
            hosted_use.hosted_use_gate_id("global_reviewer"),
            "generation.hosted_use.global_reviewer",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
