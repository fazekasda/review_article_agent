"""Acceptance tests for the pure session-turn plan (task T7a-plan).

Synthetic data only: a real, temporary project store with real tasks, session
mappings, dispatches, and configuration snapshots, plus a *disposable* owned
service layout whose descriptor/connection seam is a narrow local fake (no server
is launched, no network call is made, and no credential is read).

The plan under test is read-only and deterministic. These tests cover:

* independent re-reading of the persisted parents (task/mapping/dispatch) and the
  refusal of cross-project or relabelled value objects;
* dispatch route/state/task/fingerprint binding;
* role/model/config/variant/service-instance/location binding, including an
  omitted variant being refused rather than defaulted;
* stale target and byte-corrupted input content being refused before a plan;
* a requested step limit that differs from the effective generated role steps;
* canonical/size-bounded application text (huge and non-portable values);
* VersionSet route/input/canonical-hash/slot/version mismatches;
* immutable, deterministic bytes/digest/admission id (and no writes);
* the fresh section-critic requirement and a same-concrete-model rejection.

Run with::

    PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_session_turns.py'
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from review_article_agent.domain.records import canonical_json  # noqa: E402
from review_article_agent.runtime import service  # noqa: E402
from review_article_agent.runtime.dispatch import RuntimeDispatchRepository  # noqa: E402
from review_article_agent.runtime.opencode import MESSAGE_ID_RE  # noqa: E402
from review_article_agent.runtime.roles import (  # noqa: E402
    BoundedTask,
    CriticConfigError,
    RoleConfiguration,
)
from review_article_agent.runtime.service import (  # noqa: E402
    OpenCodeServiceManager,
    ProjectLocation,
    ServiceConnection,
    ServiceDescriptor,
)
from review_article_agent.runtime.session_turns import (  # noqa: E402
    SESSION_TURN_PLAN_PERFORMS_NETWORK_IO,
    SESSION_TURN_SPEC_GRANTS_ADMISSION,
    SESSION_TURN_SPEC_GRANTS_APPROVAL,
    SESSION_TURN_SPEC_IS_PERSISTED,
    SESSION_TURN_SPEC_KIND,
    SessionTurnSpec,
    SessionTurnValidationError,
    prepare_session_turn,
)
from review_article_agent.storage import (  # noqa: E402
    ArtifactRepository,
    ProjectStore,
    paths,
)
from review_article_agent.storage.agent_runtime import (  # noqa: E402
    RuntimeRepository,
    validate_version_set,
)

_OPS = ("acquisition", "storage", "redistribution", "hosted_transfer")

_DIG_A = "a" * 64
_DIG_B = "b" * 64
_DIG_C = "c" * 64
_DIG_E = "e" * 64


def _rights(project_id, artifact_id, revision_id):
    return {
        "rights_id": f"RIGHTS-{artifact_id}-{revision_id}",
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
        "recorded_at": "2026-10-27T00:00:00Z",
        "derived_status": "valid",
    }


def _role_spec(model_id, variant, steps):
    spec = {"provider_id": "openrouter", "model_id": model_id, "steps": steps}
    if variant is not None:
        spec["variant"] = variant
    return spec


def _config(roles):
    """Build a RoleConfiguration from ``{role: (model_id, variant, steps)}``."""
    return RoleConfiguration.from_mapping(
        {
            "schema_version": 1,
            "roles": {role: _role_spec(*values) for role, values in roles.items()},
        }
    )


def _model_ref(model_id, variant):
    base = f"openrouter/{model_id}"
    return base if variant is None else f"{base}#{variant}"


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class _LayoutManager:
    """A disposable owned-layout seam: genuine service dataclasses, no server.

    ``location_for`` delegates to a real :class:`OpenCodeServiceManager`, so the
    per-project location is the manager's real clean, owned layout. The
    descriptor/connection are genuine frozen dataclasses derived from that layout;
    they are never used to connect, and the credential is never read.
    """

    def __init__(self, real: OpenCodeServiceManager, *, port: int = 4096) -> None:
        self._real = real
        self.configuration = real.configuration
        self._port = port
        self.read_password_calls = 0

    @property
    def real(self) -> OpenCodeServiceManager:
        return self._real

    def location_for(self, project_id: str) -> ProjectLocation:
        return self._real.location_for(project_id)

    def descriptor(self) -> ServiceDescriptor:
        real = self._real
        return ServiceDescriptor(
            url=f"http://127.0.0.1:{self._port}",
            pid=os.getpid(),
            version=service.OPENCODE_PINNED_VERSION,
            binary="/usr/bin/opencode",
            root=str(real.root),
            password_file=str(real.secret_file),
            home=str(real.home),
            xdg_config_home=str(real.config_home),
            xdg_data_home=str(real.data_home),
            xdg_state_home=str(real.state_home),
            xdg_cache_home=str(real.cache_home),
            cwd=str(real.cwd),
        )

    def connection(self) -> ServiceConnection:
        real = self._real
        return ServiceConnection(
            base_url=f"http://127.0.0.1:{self._port}",
            username=service.SERVICE_USERNAME,
            password_file=str(real.secret_file),
        )

    def read_password(self) -> str:  # pragma: no cover - must never be called
        self.read_password_calls += 1
        raise AssertionError("a pure session-turn plan must not read the credential")


def _versions(
    model_ref: str,
    prompt_version: str,
    *,
    input_digest: str = _DIG_A,
    route: str = "opencode_session",
    context_kind: str = "model_derived",
    model_digest: str = _DIG_B,
    prompt_digest: str = _DIG_C,
):
    """Return ``(raw_versions, canonical_versions, canonical_hash)``."""
    if context_kind == "human_authored":
        raw = {
            "context_kind": "human_authored",
            "input_digest": input_digest,
            "route": route,
            "model": {"state": "not_applicable"},
            "prompt": {"state": "not_applicable"},
            "rubric": {"state": "not_applicable"},
            "parser": {"state": "not_applicable"},
            "adapter": {"state": "not_applicable"},
        }
        canonical = validate_version_set(raw, consumed=())
    else:
        raw = {
            "context_kind": context_kind,
            "input_digest": input_digest,
            "route": route,
            "model": {
                "state": "known",
                "ref": {"version": model_ref, "digest": model_digest},
            },
            "prompt": {
                "state": "known",
                "ref": {"version": prompt_version, "digest": prompt_digest},
            },
            "rubric": {"state": "not_applicable"},
            "parser": {"state": "not_applicable"},
            "adapter": {"state": "not_applicable"},
        }
        canonical = validate_version_set(raw, consumed=("model", "prompt"))
    digest = _sha256_bytes(canonical_json(canonical))
    return raw, canonical, digest


class SessionTurnPlanTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp_obj = tempfile.TemporaryDirectory(prefix="raa-session-turn-")
        self.addCleanup(self._tmp_obj.cleanup)
        self.tmp = Path(self._tmp_obj.name)
        self._seq = 0

    # -- scenario construction ------------------------------------------
    def _seed_artifact(self, store, *, artifact_id=None, revision_id="rev_1", content=b"bytes"):
        repo = ArtifactRepository(store)
        artifact = repo.create_artifact(kind="document", artifact_id=artifact_id)
        revision = repo.import_revision(
            artifact_id=artifact.artifact_id,
            content=content,
            origin_locator="import:synthetic",
            rights=_rights(store.project_id, artifact.artifact_id, revision_id),
            revision_id=revision_id,
        )
        return artifact, revision

    def _scenario(
        self,
        *,
        roles=None,
        role="writer",
        mapping_role=None,
        task_role=None,
        mapping_configuration=None,
        task_configuration=None,
        manager_configuration=None,
        step_limit=None,
        size_limit=4096,
        session_id=None,
        mapping_clean_location=None,
        service_instance_id=None,
        route="opencode_session",
        reserve_usd="0.10",
        text="Draft the pinned section from the exact input pins.",
        prompt_version=None,
        versions_model_ref=None,
        versions_prompt_version=None,
        versions_route="opencode_session",
        versions_context_kind="model_derived",
        versions_input_digest=_DIG_A,
        dispatch_input_digest=None,
        dispatch_version_digest=None,
        create_writer_mapping=None,
    ):
        """Build a complete, real, temporary persisted scenario."""
        roles = roles or {"writer": ("anthropic/claude-sonnet-5-5", "high", 8)}
        mapping_role = mapping_role or role
        task_role = task_role or role
        configuration = mapping_configuration or _config(roles)
        mapping_configuration = mapping_configuration or configuration
        task_configuration = task_configuration or configuration
        manager_configuration = manager_configuration or configuration
        self._seq += 1
        name = f"p{self._seq}"
        project_id = paths.new_project_id()
        store = ProjectStore.create_at(paths.app_dir_for(str(self.tmp / name)), project_id)
        self.addCleanup(store.close)
        runtime = RuntimeRepository(store)
        dispatch_repo = RuntimeDispatchRepository(store, runtime=runtime)
        real_manager = OpenCodeServiceManager(
            data_dir=str(self.tmp / f"svc{self._seq}"),
            configuration=manager_configuration,
        )
        manager = _LayoutManager(real_manager)
        location = manager.location_for(store.project_id)

        target_artifact, target_revision = self._seed_artifact(
            store, artifact_id="art_target", revision_id="rev_1", content=b"target-bytes"
        )
        input_artifact, input_revision = self._seed_artifact(
            store, artifact_id="art_input", revision_id="rev_in", content=b"input-bytes"
        )

        if create_writer_mapping is None:
            create_writer_mapping = "section_critic" in roles and task_role == "section_critic"
        writer_mapping = None
        if create_writer_mapping:
            writer_mapping = runtime.record_session_mapping(
                service_instance_id=location.location_id,
                clean_location=location.directory,
                session_id="ses_writer_pre_1",
                role="writer",
                model=_model_ref(*roles["writer"][:2]),
                configuration=configuration,
            )

        mapping_model = _model_ref(*_role_model_pair(mapping_configuration, mapping_role))
        mapping = runtime.record_session_mapping(
            service_instance_id=service_instance_id or location.location_id,
            clean_location=mapping_clean_location or location.directory,
            session_id=session_id or f"ses_{mapping_role}_1",
            role=mapping_role,
            model=mapping_model,
            configuration=mapping_configuration,
        )

        prompt_version = prompt_version or f"{task_role}.v1"
        effective_steps = _config_role_steps(task_configuration, task_role)
        bounded = BoundedTask.from_mapping(
            {
                "project_id": store.project_id,
                "role": task_role,
                "target": {
                    "project_id": store.project_id,
                    "artifact_id": target_artifact.artifact_id,
                    "revision_id": "rev_1",
                },
                "input_refs": [
                    {
                        "project_id": store.project_id,
                        "artifact_id": input_artifact.artifact_id,
                        "revision_id": "rev_in",
                        "digest": _sha256_bytes(b"input-bytes"),
                    }
                ],
                "scope": ["section:introduction"],
                "prompt_version": prompt_version,
                "size_limit": size_limit,
                "step_limit": effective_steps if step_limit is None else step_limit,
            },
            configuration=task_configuration,
        )
        task = runtime.record_task(
            bounded, request_id=f"req-{self._seq}", configuration=task_configuration
        )

        versions_model_ref = versions_model_ref or mapping.model_ref
        versions_prompt_version = versions_prompt_version or prompt_version
        versions_raw, _canonical, versions_digest = _versions(
            versions_model_ref,
            versions_prompt_version,
            input_digest=versions_input_digest,
            route=versions_route,
            context_kind=versions_context_kind,
        )
        dispatch = dispatch_repo.reserve_dispatch(
            task,
            request_id=f"call-{self._seq}",
            route=route,
            stage="section_drafting",
            input_digest=dispatch_input_digest or versions_input_digest,
            version_digest=dispatch_version_digest or versions_digest,
            reserve_usd=reserve_usd,
            bound_evidence_digest=_DIG_E,
        )
        return SimpleNamespace(
            store=store,
            runtime=runtime,
            dispatch_repo=dispatch_repo,
            manager=manager,
            location=location,
            configuration=configuration,
            task_configuration=task_configuration,
            mapping_configuration=mapping_configuration,
            manager_configuration=manager_configuration,
            target_artifact=target_artifact,
            target_revision=target_revision,
            input_artifact=input_artifact,
            input_revision=input_revision,
            writer_mapping=writer_mapping,
            mapping=mapping,
            task=task,
            bounded=bounded,
            versions=versions_raw,
            dispatch=dispatch,
            text=text,
            prompt_version=prompt_version,
            role=task_role,
        )

    # -- prepare helper --------------------------------------------------
    def _prepare(self, scenario, **overrides):
        args = {
            "task": scenario.task,
            "dispatch": scenario.dispatch,
            "mapping": scenario.mapping,
            "manager": scenario.manager,
            "text": scenario.text,
            "versions": scenario.versions,
        }
        args.update(overrides)
        return prepare_session_turn(scenario.store, **args)


def _role_model_pair(configuration: RoleConfiguration, role: str):
    model = configuration.for_role(role).model
    return model.model_id, model.variant


def _config_role_steps(configuration: RoleConfiguration, role: str) -> int:
    return configuration.for_role(role).steps


class PrepareSessionTurnTest(SessionTurnPlanTestBase):
    def test_prepares_an_immutable_deterministic_spec(self) -> None:
        scenario = self._scenario()
        spec = self._prepare(scenario)
        self.assertIsInstance(spec, SessionTurnSpec)
        self.assertEqual(spec.project_id, scenario.store.project_id)
        self.assertEqual(spec.role, "writer")
        self.assertEqual(spec.session_id, scenario.mapping.session_id)
        self.assertEqual(spec.mapping_id, scenario.mapping.mapping_id)
        self.assertEqual(spec.dispatch_id, scenario.dispatch.dispatch_id)
        self.assertEqual(spec.dispatch_request_id, scenario.dispatch.request_id)
        self.assertEqual(spec.task_id, scenario.task.task_id)
        self.assertEqual(spec.task_fingerprint, scenario.task.fingerprint)
        self.assertEqual(spec.model, scenario.mapping.model_ref)
        self.assertEqual(spec.input_digest, scenario.dispatch.input_digest)
        self.assertEqual(spec.version_digest, scenario.dispatch.version_digest)
        self.assertEqual(spec.prompt_version, scenario.prompt_version)
        self.assertEqual(spec.step_limit, scenario.bounded.step_limit)
        self.assertEqual(spec.size_limit, scenario.bounded.size_limit)
        self.assertFalse(spec.fresh_context_required)
        self.assertIsInstance(spec.canonical, bytes)
        self.assertIsInstance(spec.message, bytes)
        self.assertEqual(spec.digest, _sha256_bytes(spec.canonical))
        self.assertEqual(spec.message_digest, _sha256_bytes(spec.message))

        plan = spec.to_mapping()
        self.assertEqual(plan["kind"], SESSION_TURN_SPEC_KIND)
        self.assertEqual(plan["spec_version"], 1)
        self.assertEqual(plan["admission_id"], spec.admission_id)
        self.assertEqual(plan["session"]["session_id"], scenario.mapping.session_id)
        self.assertEqual(plan["session"]["mapping_id"], scenario.mapping.mapping_id)
        self.assertEqual(plan["session"]["model"], scenario.mapping.model_ref)
        self.assertEqual(plan["context_history"], "unverified")
        self.assertNotIn("history_empty", plan)
        boundary = plan["execution_boundary"]
        self.assertEqual(boundary["kind"], "inert_metadata_planning_only")
        for key in (
            "correlation_persisted",
            "grants_admission",
            "grants_approval",
            "authorizes_paid_generation",
            "authorizes_source_upload",
        ):
            self.assertIs(boundary[key], False)
        self.assertIs(boundary["requires_later_runner"], True)
        self.assertFalse(SESSION_TURN_SPEC_IS_PERSISTED)
        self.assertFalse(SESSION_TURN_SPEC_GRANTS_ADMISSION)
        self.assertFalse(SESSION_TURN_SPEC_GRANTS_APPROVAL)
        self.assertFalse(SESSION_TURN_PLAN_PERFORMS_NETWORK_IO)

        # A second, independent preparation is byte-identical and reconstructible.
        again = self._prepare(scenario)
        self.assertEqual(again.canonical, spec.canonical)
        self.assertEqual(again.digest, spec.digest)
        self.assertEqual(again.admission_id, spec.admission_id)

        # A thawed copy is never an alias: mutating it cannot change the plan.
        copy = spec.to_mapping()
        copy["task"]["step_limit"] = 1
        copy["session"]["session_id"] = "ses_tampered"
        self.assertEqual(spec.to_mapping(), plan)
        self.assertEqual(spec.digest, _sha256_bytes(spec.canonical))

        # The value object is self-checking: a forged canonical blob is refused.
        with self.assertRaises(SessionTurnValidationError):
            dataclasses.replace(spec, canonical=b"{}")

    def test_admission_id_is_deterministic_and_reconstructible(self) -> None:
        scenario = self._scenario()
        spec = self._prepare(scenario)
        expected = "msg_" + _sha256_bytes(
            (scenario.store.project_id + "\x00" + scenario.dispatch.dispatch_id).encode("utf-8")
        )[:32]
        self.assertEqual(spec.admission_id, expected)
        self.assertIsNotNone(MESSAGE_ID_RE.fullmatch(spec.admission_id))
        self.assertEqual(spec.to_mapping()["admission_id"], expected)

    def test_message_binds_task_pins_versions_and_application_text(self) -> None:
        text = {"instruction": "draft", "section": "introduction"}
        scenario = self._scenario(text=text)
        spec = self._prepare(scenario)
        message = json.loads(spec.message)
        self.assertEqual(message["kind"], "session_turn_input")
        self.assertEqual(message["target"], dict(scenario.task.target))
        self.assertEqual(
            message["input_refs"], [dict(entry) for entry in scenario.task.input_refs]
        )
        self.assertEqual(message["scope"], list(scenario.bounded.scope))
        self.assertEqual(message["prompt_version"], scenario.prompt_version)
        self.assertEqual(message["step_limit"], scenario.bounded.step_limit)
        self.assertEqual(message["size_limit"], scenario.bounded.size_limit)
        self.assertEqual(message["versions"], validate_version_set(scenario.versions))
        self.assertEqual(message["text"], text)
        self.assertEqual(message["session_id"], scenario.mapping.session_id)
        self.assertEqual(message["mapping_id"], scenario.mapping.mapping_id)

    def test_prepare_writes_nothing_and_keeps_dispatch_reserved(self) -> None:
        scenario = self._scenario()
        before = (
            [record.task_id for record in scenario.runtime.list_tasks()],
            [m.mapping_id for m in scenario.runtime.list_session_mappings()],
            [snap.snapshot_id for snap in scenario.runtime.list_configurations()],
            [row.dispatch_id for row in scenario.dispatch_repo.list_dispatches()],
        )
        spec = self._prepare(scenario)
        after = (
            [record.task_id for record in scenario.runtime.list_tasks()],
            [m.mapping_id for m in scenario.runtime.list_session_mappings()],
            [snap.snapshot_id for snap in scenario.runtime.list_configurations()],
            [row.dispatch_id for row in scenario.dispatch_repo.list_dispatches()],
        )
        self.assertEqual(before, after)
        self.assertEqual(
            scenario.dispatch_repo.get_dispatch(spec.dispatch_id).state, "reserved"
        )
        self.assertEqual(scenario.manager.read_password_calls, 0)

    def test_manager_without_the_owned_surface_is_refused(self) -> None:
        scenario = self._scenario()
        with self.assertRaises(SessionTurnValidationError) as ctx:
            self._prepare(scenario, manager=object())
        self.assertEqual(ctx.exception.code, "manager")


class ParentVerificationTest(SessionTurnPlanTestBase):
    def test_cross_project_task_is_refused(self) -> None:
        first = self._scenario()
        second = self._scenario()
        with self.assertRaises(SessionTurnValidationError) as ctx:
            prepare_session_turn(
                first.store,
                task=second.task,
                dispatch=first.dispatch,
                mapping=first.mapping,
                manager=first.manager,
                text=first.text,
                versions=first.versions,
            )
        self.assertEqual(ctx.exception.code, "task")

    def test_cross_project_mapping_is_refused(self) -> None:
        first = self._scenario()
        second = self._scenario()
        with self.assertRaises(SessionTurnValidationError) as ctx:
            prepare_session_turn(
                first.store,
                task=first.task,
                dispatch=first.dispatch,
                mapping=second.mapping,
                manager=first.manager,
                text=first.text,
                versions=first.versions,
            )
        self.assertEqual(ctx.exception.code, "mapping")

    def test_cross_project_dispatch_is_refused(self) -> None:
        first = self._scenario()
        second = self._scenario()
        with self.assertRaises(SessionTurnValidationError) as ctx:
            prepare_session_turn(
                first.store,
                task=first.task,
                dispatch=second.dispatch,
                mapping=first.mapping,
                manager=first.manager,
                text=first.text,
                versions=first.versions,
            )
        self.assertEqual(ctx.exception.code, "dispatch")

    def test_relabelled_task_mapping_and_dispatch_are_refused(self) -> None:
        scenario = self._scenario()
        with self.assertRaises(SessionTurnValidationError) as ctx:
            self._prepare(scenario, task=dataclasses.replace(scenario.task, role="section_critic"))
        self.assertEqual(ctx.exception.code, "task")
        with self.assertRaises(SessionTurnValidationError) as ctx:
            self._prepare(
                scenario, mapping=dataclasses.replace(scenario.mapping, role="section_critic")
            )
        self.assertEqual(ctx.exception.code, "mapping")
        with self.assertRaises(SessionTurnValidationError) as ctx:
            self._prepare(
                scenario, dispatch=dataclasses.replace(scenario.dispatch, state="admitted")
            )
        self.assertEqual(ctx.exception.code, "dispatch")


class DispatchBindingTest(SessionTurnPlanTestBase):
    def test_non_reserved_dispatch_is_refused(self) -> None:
        scenario = self._scenario()
        submitted = scenario.dispatch_repo.mark_submitted(scenario.dispatch.dispatch_id)
        with self.assertRaises(SessionTurnValidationError) as ctx:
            self._prepare(scenario, dispatch=submitted)
        self.assertEqual(ctx.exception.code, "dispatch")

    def test_non_session_route_is_refused(self) -> None:
        scenario = self._scenario(route="direct_call")
        with self.assertRaises(SessionTurnValidationError) as ctx:
            self._prepare(scenario)
        self.assertEqual(ctx.exception.code, "dispatch")


class MappingBindingTest(SessionTurnPlanTestBase):
    def test_mismatched_role_is_refused(self) -> None:
        scenario = self._scenario(
            roles={
                "writer": ("anthropic/claude-sonnet-5-5", "high", 8),
                "section_critic": ("openai/gpt-6.1-sol", "high", 8),
            },
            role="section_critic",
            mapping_role="writer",
            task_role="section_critic",
        )
        with self.assertRaises(SessionTurnValidationError) as ctx:
            self._prepare(scenario)
        self.assertEqual(ctx.exception.code, "mapping")

    def test_mismatched_config_for_role_is_refused(self) -> None:
        scenario = self._scenario(
            mapping_configuration=_config(
                {"writer": ("anthropic/claude-sonnet-5-5", "high", 8)}
            ),
            task_configuration=_config(
                {"writer": ("openai/gpt-6.1-sol", "high", 8)}
            ),
        )
        with self.assertRaises(SessionTurnValidationError) as ctx:
            self._prepare(scenario)
        self.assertEqual(ctx.exception.code, "configuration")

    def test_mismatched_manager_configuration_is_refused(self) -> None:
        scenario = self._scenario(
            mapping_configuration=_config(
                {"writer": ("anthropic/claude-sonnet-5-5", "high", 8)}
            ),
            manager_configuration=_config(
                {"writer": ("openai/gpt-6.1-sol", "high", 8)}
            ),
        )
        with self.assertRaises(SessionTurnValidationError) as ctx:
            self._prepare(scenario)
        self.assertEqual(ctx.exception.code, "configuration")

    def test_mismatched_location_is_refused(self) -> None:
        scenario = self._scenario(mapping_clean_location=str(self.tmp / "elsewhere"))
        with self.assertRaises(SessionTurnValidationError) as ctx:
            self._prepare(scenario)
        self.assertEqual(ctx.exception.code, "manager")

    def test_mismatched_service_instance_is_refused(self) -> None:
        scenario = self._scenario(service_instance_id="svc_manual")
        with self.assertRaises(SessionTurnValidationError) as ctx:
            self._prepare(scenario)
        self.assertEqual(ctx.exception.code, "manager")

    def test_missing_variant_is_refused_instead_of_defaulted(self) -> None:
        scenario = self._scenario(
            roles={"writer": ("anthropic/claude-sonnet-5-5", None, 8)}
        )
        with self.assertRaises(SessionTurnValidationError) as ctx:
            self._prepare(scenario)
        self.assertEqual(ctx.exception.code, "model")


class LiveReferenceTest(SessionTurnPlanTestBase):
    def test_stale_target_is_refused(self) -> None:
        scenario = self._scenario()
        repo = ArtifactRepository(scenario.store)
        repo.import_revision(
            artifact_id=scenario.target_artifact.artifact_id,
            content=b"second-target-bytes",
            origin_locator="import:synthetic",
            rights=_rights(
                scenario.store.project_id,
                scenario.target_artifact.artifact_id,
                "rev_2",
            ),
            revision_id="rev_2",
            parent_revision_id="rev_1",
        )
        with self.assertRaises(SessionTurnValidationError) as ctx:
            self._prepare(scenario)
        self.assertEqual(ctx.exception.code, "task")

    def test_byte_corrupted_input_is_refused(self) -> None:
        scenario = self._scenario()
        conn = scenario.store.connection
        conn.execute("DROP TRIGGER revision_no_update")
        conn.execute(
            "UPDATE revision SET content = ? WHERE project_id = ? AND artifact_id = ? "
            "AND revision_id = ?",
            (
                b"tampered-bytes",
                scenario.store.project_id,
                scenario.input_artifact.artifact_id,
                "rev_in",
            ),
        )
        with self.assertRaises(SessionTurnValidationError) as ctx:
            self._prepare(scenario)
        self.assertEqual(ctx.exception.code, "task")


class StepAndTextBoundsTest(SessionTurnPlanTestBase):
    def test_smaller_step_limit_is_refused(self) -> None:
        scenario = self._scenario(step_limit=4)
        with self.assertRaises(SessionTurnValidationError) as ctx:
            self._prepare(scenario)
        self.assertEqual(ctx.exception.code, "step_limit")

    def test_huge_text_is_refused(self) -> None:
        scenario = self._scenario(text="x" * 5000)
        with self.assertRaises(SessionTurnValidationError) as ctx:
            self._prepare(scenario)
        self.assertEqual(ctx.exception.code, "text")

    def test_serialized_message_over_size_limit_is_refused(self) -> None:
        scenario = self._scenario(size_limit=512, text="x" * 300)
        with self.assertRaises(SessionTurnValidationError) as ctx:
            self._prepare(scenario)
        self.assertEqual(ctx.exception.code, "message")

    def test_non_portable_text_is_refused(self) -> None:
        deep: dict = {}
        cursor = deep
        for _ in range(70):
            cursor["nested"] = {}
            cursor = cursor["nested"]
        for bad in (float("nan"), {1, 2}, deep):
            with self.subTest(bad=type(bad).__name__):
                scenario = self._scenario(text=bad)
                with self.assertRaises(SessionTurnValidationError) as ctx:
                    self._prepare(scenario)
                self.assertEqual(ctx.exception.code, "canonical")


class VersionsBindingTest(SessionTurnPlanTestBase):
    def test_versions_route_mismatch_is_refused(self) -> None:
        scenario = self._scenario(versions_route="direct_call")
        with self.assertRaises(SessionTurnValidationError) as ctx:
            self._prepare(scenario)
        self.assertEqual(ctx.exception.code, "versions")

    def test_versions_input_digest_mismatch_is_refused(self) -> None:
        scenario = self._scenario(dispatch_input_digest=_DIG_B)
        with self.assertRaises(SessionTurnValidationError) as ctx:
            self._prepare(scenario)
        self.assertEqual(ctx.exception.code, "versions")

    def test_versions_canonical_hash_mismatch_is_refused(self) -> None:
        scenario = self._scenario(dispatch_version_digest="d" * 64)
        with self.assertRaises(SessionTurnValidationError) as ctx:
            self._prepare(scenario)
        self.assertEqual(ctx.exception.code, "versions")

    def test_versions_model_version_mismatch_is_refused(self) -> None:
        scenario = self._scenario(versions_model_ref="openrouter/other/model#high")
        with self.assertRaises(SessionTurnValidationError) as ctx:
            self._prepare(scenario)
        self.assertEqual(ctx.exception.code, "versions")

    def test_versions_prompt_version_mismatch_is_refused(self) -> None:
        scenario = self._scenario(versions_prompt_version="other.v1")
        with self.assertRaises(SessionTurnValidationError) as ctx:
            self._prepare(scenario)
        self.assertEqual(ctx.exception.code, "versions")

    def test_human_authored_versions_are_refused(self) -> None:
        scenario = self._scenario(versions_context_kind="human_authored")
        with self.assertRaises(SessionTurnValidationError) as ctx:
            self._prepare(scenario)
        self.assertEqual(ctx.exception.code, "versions")


class CriticPlanTest(SessionTurnPlanTestBase):
    def test_fresh_critic_is_marked_and_distinct(self) -> None:
        scenario = self._scenario(
            roles={
                "writer": ("anthropic/claude-sonnet-5-5", "high", 8),
                "section_critic": ("openai/gpt-6.1-sol", "high", 8),
            },
            role="section_critic",
        )
        spec = self._prepare(scenario)
        self.assertTrue(spec.fresh_context_required)
        plan = spec.to_mapping()
        self.assertIs(plan["fresh_context_required"], True)
        self.assertEqual(plan["context_history"], "unverified")
        self.assertNotIn("history_empty", plan)
        self.assertEqual(spec.model, scenario.mapping.model_ref)
        self.assertEqual(spec.session_id, scenario.mapping.session_id)
        self.assertNotEqual(
            scenario.writer_mapping.session_id, scenario.mapping.session_id
        )

    def test_same_concrete_model_critic_is_refused(self) -> None:
        scenario = self._scenario(
            roles={
                "writer": ("anthropic/claude-sonnet-5-5", "high", 8),
                "section_critic": ("anthropic/claude-sonnet-5-5", "max", 8),
            },
            role="section_critic",
        )
        with self.assertRaises(SessionTurnValidationError) as ctx:
            self._prepare(scenario)
        self.assertEqual(ctx.exception.code, "section_critic")

    def test_exact_same_model_critic_is_refused(self) -> None:
        scenario = self._scenario(
            roles={
                "writer": ("anthropic/claude-sonnet-5-5", "high", 8),
                "section_critic": ("anthropic/claude-sonnet-5-5", "high", 8),
            },
            role="section_critic",
        )
        with self.assertRaises(SessionTurnValidationError) as ctx:
            self._prepare(scenario)
        self.assertEqual(ctx.exception.code, "section_critic")


class CriticConfigurationSanityTest(unittest.TestCase):
    """The underlying role configuration refuses a same-concrete-model critic."""

    def test_critic_config_error_is_a_role_error(self) -> None:
        config = _config(
            {
                "writer": ("anthropic/claude-sonnet-5-5", "high", 8),
                "section_critic": ("anthropic/claude-sonnet-5-5", "max", 8),
            }
        )
        with self.assertRaises(CriticConfigError):
            config.validate_section_critic()


if __name__ == "__main__":
    unittest.main()
