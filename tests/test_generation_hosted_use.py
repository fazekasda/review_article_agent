"""Fast-tier tests for the per-role generation hosted-use document + named gate.

Covers ``src/review_article_agent/generation/hosted_use.py`` (kickoff 16 §5):
the authorization document, its static validator, the named ``batch`` gate
(``generation.hosted_use.<role>``), the immutable-revision propose/require seam,
and the distinct-concrete-model rule. Real temp stores and a real, owner-
authenticated human approval are used (no fakes for the gate); no network.

Run:

    PYTHONPATH=src:tests python3 -m unittest tests.test_generation_hosted_use
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from review_article_agent.generation import hosted_use  # noqa: E402
from review_article_agent.generation.hosted_use import (  # noqa: E402
    GenerationHostedUseAuthorizationError,
    GenerationHostedUseDocumentError,
    GenerationHostedUseError,
    GenerationRoleModelError,
    hosted_use_gate_id,
    propose_hosted_use,
    require_hosted_use,
    role_models_distinct,
    validate_hosted_use_document,
)
from review_article_agent.runtime import generation, owner_auth  # noqa: E402
from review_article_agent.runtime.gates import prepare_gate_snapshot  # noqa: E402
from review_article_agent.runtime.lead import LeadConfig  # noqa: E402
from review_article_agent.runtime.owner_auth import OwnerApproval  # noqa: E402
from review_article_agent.storage import (  # noqa: E402
    ArtifactRepository,
    ProjectStore,
    Registry,
    paths,
)
from review_article_agent.storage.human_approvals import (  # noqa: E402
    HumanApprovalRepository,
)

_OPS = ("acquisition", "storage", "redistribution", "hosted_transfer")
_MODEL = "deepseek/deepseek-v4.1-flash"
_CRITIC_MODEL = "anthropic/claude-opus-4.1"
_GLOBAL_MODEL = "openai/gpt-5.1"
_EVIDENCE = {"ref": "evidence:synthetic", "verified_at": "2026-10-28T00:00:00Z"}
_FIXED_CHALLENGE = "TEST-CHAL-0001"
_OWNER_UID = 1000
_BASE_TIME = datetime(2026, 10, 28, 12, 0, 0, tzinfo=timezone.utc)


def _rights(project_id, artifact_id, revision_id, *, ops=None):
    operations = {op: "allowed" for op in _OPS}
    if ops:
        operations.update(ops)
    basis = {}
    for op in _OPS:
        if operations[op] == "allowed":
            basis[op] = {
                "kind": "license",
                "evidence": {"ref": "lic:syn", "provenance": "publisher:syn"},
            }
        else:
            basis[op] = {"kind": "unknown", "evidence": {"ref": "", "provenance": ""}}
    subject = {
        "kind": "source_revision",
        "project_id": project_id,
        "artifact_id": artifact_id,
        "revision_id": revision_id,
    }
    return {
        "rights_id": f"RIGHTS-{artifact_id}-{revision_id}",
        "project_id": project_id,
        "record_revision": 1,
        "subject": subject,
        "operations": operations,
        "basis": basis,
        "authorization": {
            "actor": "user",
            "method": "out_of_band_authenticated",
            "authorized_at": "2026-10-28T00:00:00Z",
            "against_subject": subject,
            "against_rights_revision": 1,
            "basis": {
                "kind": "license",
                "evidence": {"ref": "lic:syn", "provenance": "publisher:syn"},
            },
        },
        "restrictions": [],
        "privacy": {
            "personal_data": False,
            "confidentiality": "public",
            "denial": False,
        },
        "recorded_by": "user",
        "recorded_at": "2026-10-28T00:00:00Z",
        "derived_status": "valid",
    }


def _model_map(model, provider="deepseek"):
    return LeadConfig.from_mapping(
        {
            "model_id": model,
            "upstream_provider": provider,
            "max_output_tokens": 1024,
            "context_tokens": 128000,
            "input_price_per_million": "0.5",
            "output_price_per_million": "1.5",
            "pricing_ref": "pricing:synthetic:v1",
            "instructions_revision": "instr.v1",
            "timeout_seconds": 60.0,
            "max_response_bytes": 1048576,
        }
    ).to_mapping()


def _document(role="section_writer", *, model=_MODEL, provider="deepseek",
              budget="10.00", notes=None):
    return {
        "schema_version": 1,
        "document_type": "generation_hosted_use",
        "payload": {
            "role": role,
            "config": _model_map(model, provider),
            "budget_cap_usd": budget,
            "provider_terms": dict(_EVIDENCE),
            "privacy_review": dict(_EVIDENCE),
            "pricing_evidence": dict(_EVIDENCE),
            "notes": notes,
        },
    }


class HostedUseTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="raa-generation-hosted-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.registry = Registry(str(self.tmp / "app"))
        info = self.registry.create_project(str(self.tmp / "proj"))
        self.addCleanup(self._close_store)
        self.app_dir = paths.app_dir_for(info.root_path)
        self.store = ProjectStore.open_at(self.app_dir)
        self.project_id = self.store.project_id
        self.artifacts = ArtifactRepository(self.store)
        self._patches = []
        self.addCleanup(self._stop_patches)
        self._install_owner_auth()

    def _close_store(self):
        try:
            self.store.close()
        except Exception:  # pragma: no cover
            pass

    def _stop_patches(self):
        for patch in self._patches:
            patch.stop()

    def _install_owner_auth(self):
        class _FakeTty:
            def is_tty(self):
                return True

            def write(self, text, *, deadline=None):
                return None

            def read_line(self, *, max_chars, deadline):
                return _FIXED_CHALLENGE

            def close(self):
                return None

        for target, value in (
            ("_new_challenge", lambda: _FIXED_CHALLENGE),
            ("_current_owner_uid", lambda: _OWNER_UID),
            ("_real_uid", lambda: _OWNER_UID),
            ("_utcnow", lambda: _BASE_TIME),
            ("_monotonic", lambda: 1000.0),
            ("_open_controlling_tty", lambda: _FakeTty()),
        ):
            patch = mock.patch.object(owner_auth, target, value)
            patch.start()
            self._patches.append(patch)

    def approve(self, ref, gate_id, *, gate_kind="batch"):
        prepared = prepare_gate_snapshot(
            self.store,
            ref,
            gate_kind=gate_kind,
            gate_id=gate_id,
            decision="approved",
            rationale="synthetic gate",
        )
        capability = owner_auth.request_owner_confirmation(prepared.owner_action())
        assert isinstance(capability, OwnerApproval)
        HumanApprovalRepository(self.store).record_decision(prepared, capability)

    def put(self, document, *, artifact_id, revision_id):
        return propose_hosted_use(
            self.store,
            document,
            artifact_id=artifact_id,
            revision_id=revision_id,
            rights=_rights(self.project_id, artifact_id, revision_id),
        )

    def new_revision(self, document, *, artifact_id, revision_id, parent_revision_id):
        revision = self.artifacts.import_revision(
            artifact_id=artifact_id,
            revision_id=revision_id,
            parent_revision_id=parent_revision_id,
            rights=_rights(self.project_id, artifact_id, revision_id),
            content=document,
            origin_locator="generation:local",
            acquired_by="user",
        )
        return {
            "project_id": self.project_id,
            "artifact_id": revision.artifact_id,
            "revision_id": revision.revision_id,
        }


# --------------------------------------------------------------------------- #
# Static validation
# --------------------------------------------------------------------------- #
class ValidateDocumentTest(HostedUseTestBase):
    def test_valid_document_every_role(self):
        for role in generation.ROLES:
            document = _document(role)
            validated = validate_hosted_use_document(document)
            self.assertEqual(validated["document_type"], "generation_hosted_use")
            self.assertEqual(validated["schema_version"], 1)
            self.assertEqual(validated["payload"]["role"], role)
            self.assertEqual(
                validated["payload"]["config"]["model_id"], _MODEL
            )
            self.assertEqual(validated["payload"]["budget_cap_usd"], "10.00")
            self.assertIsNone(validated["payload"]["notes"])
            # A copy is returned, not the caller's object.
            self.assertIsNot(validated, document)

    def test_notes_optional_string(self):
        validated = validate_hosted_use_document(_document(notes="bounded note"))
        self.assertEqual(validated["payload"]["notes"], "bounded note")

    def test_envelope_malformed(self):
        document = _document()
        del document["payload"]
        with self.assertRaises(GenerationHostedUseDocumentError):
            validate_hosted_use_document(document)

    def test_schema_version_wrong_type_and_value(self):
        document = _document()
        document["schema_version"] = True
        with self.assertRaises(GenerationHostedUseDocumentError):
            validate_hosted_use_document(document)
        document["schema_version"] = 2
        with self.assertRaises(GenerationHostedUseDocumentError):
            validate_hosted_use_document(document)

    def test_document_type_wrong(self):
        document = _document()
        document["document_type"] = "discovery_hosted_use"
        with self.assertRaises(GenerationHostedUseDocumentError):
            validate_hosted_use_document(document)

    def test_unknown_role_and_non_string_role(self):
        for bad in ("not_a_role", 7, None):
            document = _document()
            document["payload"]["role"] = bad
            with self.assertRaises(GenerationHostedUseDocumentError):
                validate_hosted_use_document(document)

    def test_config_invalid(self):
        document = _document()
        document["payload"]["config"] = {"model_id": "x"}
        with self.assertRaises(GenerationHostedUseDocumentError):
            validate_hosted_use_document(document)

    def test_budget_cap_malformed(self):
        for bad in ("0", "-1", "0.0", "not-a-decimal", True, 10, 10.0, None):
            document = _document(budget=bad)
            with self.assertRaises(GenerationHostedUseDocumentError):
                validate_hosted_use_document(document)

    def test_missing_or_invalid_evidence(self):
        document = _document()
        del document["payload"]["pricing_evidence"]
        with self.assertRaises(GenerationHostedUseDocumentError):
            validate_hosted_use_document(document)

        for field in ("provider_terms", "privacy_review", "pricing_evidence"):
            document = _document()
            document["payload"][field] = {"ref": "", "verified_at": "2026-10-28T00:00:00Z"}
            with self.assertRaises(GenerationHostedUseDocumentError):
                validate_hosted_use_document(document)

            document = _document()
            document["payload"][field] = {"ref": "r", "verified_at": "not-a-time"}
            with self.assertRaises(GenerationHostedUseDocumentError):
                validate_hosted_use_document(document)

            document = _document()
            document["payload"][field] = {"ref": "r", "verified_at": "2026-10-28T00:00:00+05:00"}
            with self.assertRaises(GenerationHostedUseDocumentError):
                validate_hosted_use_document(document)

    def test_notes_malformed(self):
        document = _document(notes=5)
        with self.assertRaises(GenerationHostedUseDocumentError):
            validate_hosted_use_document(document)
        document = _document(notes="x" * (hosted_use.MAX_NOTES_LEN + 1))
        with self.assertRaises(GenerationHostedUseDocumentError):
            validate_hosted_use_document(document)

    def test_gate_id_per_role(self):
        self.assertEqual(
            hosted_use_gate_id("section_writer"),
            "generation.hosted_use.section_writer",
        )
        with self.assertRaises(GenerationHostedUseError):
            hosted_use_gate_id("not_a_role")


# --------------------------------------------------------------------------- #
# propose + require
# --------------------------------------------------------------------------- #
class ProposeRequireTest(HostedUseTestBase):
    def test_propose_require_with_satisfied_gate(self):
        ref = self.put(_document(), artifact_id="a-hosted", revision_id="rev-1")
        self.approve(ref, hosted_use_gate_id("section_writer"))
        document = require_hosted_use(self.store, ref, role="section_writer")
        self.assertEqual(document["payload"]["role"], "section_writer")
        self.assertEqual(document["payload"]["config"]["model_id"], _MODEL)
        self.assertEqual(document["payload"]["budget_cap_usd"], "10.00")

    def test_require_without_gate_fails(self):
        ref = self.put(_document(), artifact_id="a-hosted", revision_id="rev-1")
        with self.assertRaises(GenerationHostedUseAuthorizationError):
            require_hosted_use(self.store, ref, role="section_writer")

    def test_require_role_mismatch_fails(self):
        ref = self.put(_document(role="section_writer"), artifact_id="a-hosted", revision_id="rev-1")
        self.approve(ref, hosted_use_gate_id("section_writer"))
        with self.assertRaises(GenerationHostedUseDocumentError):
            require_hosted_use(self.store, ref, role="section_critic")

    def test_require_after_new_revision_fails(self):
        ref_v1 = self.put(_document(), artifact_id="a-hosted", revision_id="rev-1")
        self.approve(ref_v1, hosted_use_gate_id("section_writer"))
        # A new revision of the same artifact supersedes the approved one.
        ref_v2 = self.new_revision(
            _document(notes="v2"),
            artifact_id="a-hosted",
            revision_id="rev-2",
            parent_revision_id="rev-1",
        )
        # The old revision is no longer current.
        with self.assertRaises(GenerationHostedUseError):
            require_hosted_use(self.store, ref_v1, role="section_writer")
        # The new revision has no satisfied gate.
        with self.assertRaises(GenerationHostedUseAuthorizationError):
            require_hosted_use(self.store, ref_v2, role="section_writer")

    def test_require_unknown_revision_fails(self):
        ref = {"project_id": self.project_id, "artifact_id": "nope", "revision_id": "nope"}
        with self.assertRaises(GenerationHostedUseError):
            require_hosted_use(self.store, ref, role="section_writer")

    def test_propose_rejects_malformed_without_writing(self):
        document = _document()
        document["payload"]["budget_cap_usd"] = "-1"
        with self.assertRaises(GenerationHostedUseDocumentError):
            self.put(document, artifact_id="a-hosted", revision_id="rev-1")
        self.assertIsNone(self.artifacts.current_revision("a-hosted"))


# --------------------------------------------------------------------------- #
# Distinct-model rule
# --------------------------------------------------------------------------- #
class DistinctModelTest(HostedUseTestBase):
    def _ref(self, role, model, artifact_id):
        ref = self.put(
            _document(role=role, model=model),
            artifact_id=artifact_id,
            revision_id="rev-" + artifact_id,
        )
        return ref

    def test_distinct_models_ok(self):
        refs = {
            "section_writer": self._ref("section_writer", _MODEL, "a-writer"),
            "section_critic": self._ref("section_critic", _CRITIC_MODEL, "a-critic"),
            "global_writer": self._ref("global_writer", _MODEL, "a-gwriter"),
            "global_reviewer": self._ref("global_reviewer", _GLOBAL_MODEL, "a-greviewer"),
        }
        models = role_models_distinct(self.store, refs)
        self.assertEqual(models["section_writer"], _MODEL)
        self.assertEqual(models["section_critic"], _CRITIC_MODEL)

    def test_same_section_model_raises(self):
        refs = {
            "section_writer": self._ref("section_writer", _MODEL, "a-writer"),
            "section_critic": self._ref("section_critic", _MODEL, "a-critic"),
        }
        with self.assertRaises(GenerationRoleModelError):
            role_models_distinct(self.store, refs)

    def test_same_global_model_raises(self):
        refs = {
            "global_writer": self._ref("global_writer", _MODEL, "a-gwriter"),
            "global_reviewer": self._ref("global_reviewer", _MODEL, "a-greviewer"),
        }
        with self.assertRaises(GenerationRoleModelError):
            role_models_distinct(self.store, refs)

    def test_key_role_mismatch_raises(self):
        refs = {"section_writer": self._ref("section_critic", _CRITIC_MODEL, "a-x")}
        with self.assertRaises(GenerationHostedUseDocumentError):
            role_models_distinct(self.store, refs)


# --------------------------------------------------------------------------- #
# No secrets in errors
# --------------------------------------------------------------------------- #
class NoSecretEchoTest(HostedUseTestBase):
    def test_errors_never_echo_values(self):
        canary = "SECRET-CANARY-9f3a"
        document = _document(notes=canary)
        document["payload"]["budget_cap_usd"] = canary
        with self.assertRaises(GenerationHostedUseError) as ctx:
            validate_hosted_use_document(document)
        self.assertNotIn(canary, str(ctx.exception))

        # A malformed evidence ref must not be echoed either.
        document = _document()
        document["payload"]["provider_terms"] = {
            "ref": canary,
            "verified_at": canary,
        }
        with self.assertRaises(GenerationHostedUseError) as ctx:
            validate_hosted_use_document(document)
        self.assertNotIn(canary, str(ctx.exception))

        # An authorization failure carries no document bytes.
        ref = self.put(_document(notes=canary), artifact_id="a-hosted", revision_id="rev-1")
        with self.assertRaises(GenerationHostedUseError) as ctx:
            require_hosted_use(self.store, ref, role="section_writer")
        self.assertNotIn(canary, str(ctx.exception))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
