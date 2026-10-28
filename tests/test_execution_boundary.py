"""Acceptance tests for the fail-closed production-execution boundary (task T7-boundary).

Synthetic data only. Every case uses a **real** temporary project store, real
persisted tasks/mappings/dispatches/configuration snapshots, and a *genuine*
:class:`~review_article_agent.runtime.session_turns.SessionTurnSpec` prepared by
:func:`prepare_session_turn`. The service manager is a real
:class:`~review_article_agent.runtime.service.OpenCodeServiceManager` configured
with a validated :class:`LoopbackFixtureProvider`; only its ``descriptor``/
``connection`` are replaced by a narrow, **non-launching** proof seam (no server is
started, no network call is made, and no credential is read).

Covered:

* the correct owned fixture case is accepted **read-only** (no launch, no
  credential read, ``existing_location`` used rather than the mutating lookup);
* a non-manager, a missing fixture provider, a foreign endpoint, a foreign
  fixture provider/model, a provider credential env, configuration drift, a
  drifted role configuration, and a drifted location are each refused;
* a forged/non-spec plan is refused;
* no imported authority (a bypass/``authorized`` flag) is accepted;
* the production ``opencode_session`` and ``direct_call`` routes raise an explicit
  static blocker (a virtual positive reserve is not proof), as does an unknown
  route;
* the declared, non-authorizing boundary data is false.

Run with::

    PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_execution_boundary.py'
"""

from __future__ import annotations

import os
import sys
import unittest
from types import SimpleNamespace
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
SRC = HERE.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

# Reuse the integrated, real store/session-turn scenario helpers (rights,
# versions, artifact seeding) rather than re-implementing them.
import test_session_turns as scenario_helpers  # noqa: E402

from review_article_agent.runtime import execution_boundary as eb  # noqa: E402
from review_article_agent.runtime import service  # noqa: E402
from review_article_agent.runtime.dispatch import RuntimeDispatchRepository  # noqa: E402
from review_article_agent.runtime.roles import (  # noqa: E402
    BoundedTask,
    RoleConfiguration,
)
from review_article_agent.runtime.service import (  # noqa: E402
    LoopbackFixtureProvider,
    ServiceConnection,
    ServiceDescriptor,
)
from review_article_agent.runtime.session_turns import (  # noqa: E402
    prepare_session_turn,
)
from review_article_agent.storage import ProjectStore, paths  # noqa: E402
from review_article_agent.storage.agent_runtime import RuntimeRepository  # noqa: E402

FIXTURE_PROVIDER = "fixturelocal"
WRITER_MODEL_ID = "writer-model"
CRITIC_MODEL_ID = "critic-model"
FIXTURE_BASE_URL = "http://127.0.0.1:9/v1"

_sha256_bytes = scenario_helpers._sha256_bytes
_versions = scenario_helpers._versions
_DIG_A = scenario_helpers._DIG_A
_DIG_E = scenario_helpers._DIG_E


class _ProofSeamManager(service.OpenCodeServiceManager):
    """A real fixture-configured manager whose descriptor/connection are a seam.

    Nothing is launched and no network call is made: ``descriptor``/``connection``
    return genuine frozen dataclasses derived from this manager's own owned layout.
    ``existing_location``/``location_for``/``read_password`` are counted so a test
    can prove the boundary helper used the read-only lookup and never read a
    credential.
    """

    def __init__(self, *args, seam_url=None, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._seam_url = seam_url or "http://127.0.0.1:4096"
        self.read_password_calls = 0
        self.location_for_calls = 0
        self.existing_location_calls = 0
        self.launch_calls = 0

    def descriptor(self) -> ServiceDescriptor:
        return ServiceDescriptor(
            url=self._seam_url,
            pid=os.getpid(),
            version=service.OPENCODE_PINNED_VERSION,
            binary="/usr/bin/opencode",
            root=str(self.root),
            password_file=str(self.secret_file),
            home=str(self.home),
            xdg_config_home=str(self.config_home),
            xdg_data_home=str(self.data_home),
            xdg_state_home=str(self.state_home),
            xdg_cache_home=str(self.cache_home),
            cwd=str(self.cwd),
            provider_env_names=tuple(sorted(self.provider_env)),
        )

    def connection(self) -> ServiceConnection:
        return ServiceConnection(
            base_url=self._seam_url,
            username=service.SERVICE_USERNAME,
            password_file=str(self.secret_file),
        )

    def existing_location(self, project_id):
        self.existing_location_calls += 1
        return super().existing_location(project_id)

    def location_for(self, project_id):
        self.location_for_calls += 1
        return super().location_for(project_id)

    def read_password(self):  # pragma: no cover - must never be called
        self.read_password_calls += 1
        raise AssertionError("the boundary helper must not read the credential")


def _writer_config(*, provider=FIXTURE_PROVIDER, model_id=WRITER_MODEL_ID, variant="default", steps=None):
    role: dict = {"provider_id": provider, "model_id": model_id}
    if variant is not None:
        role["variant"] = variant
    if steps is not None:
        role["steps"] = steps
    return RoleConfiguration.from_mapping({"schema_version": 1, "roles": {"writer": role}})


class ExecutionBoundaryTest(scenario_helpers.SessionTurnPlanTestBase):
    # -- scenario ---------------------------------------------------------
    def _fixture_scenario(
        self,
        *,
        provider=FIXTURE_PROVIDER,
        model_id=WRITER_MODEL_ID,
        variant="default",
        fixture_provider_id=None,
        fixture_models=(WRITER_MODEL_ID, CRITIC_MODEL_ID),
        manager_provider_env=None,
        seam_url=None,
    ):
        self._seq += 1
        seq = self._seq
        data_dir = str(self.tmp / f"fsvc{seq}")
        project_id = paths.new_project_id()
        store = ProjectStore.create_at(paths.app_dir_for(str(self.tmp / f"fxp{seq}")), project_id)
        self.addCleanup(store.close)
        runtime = RuntimeRepository(store)
        dispatch_repo = RuntimeDispatchRepository(store, runtime=runtime)

        configuration = _writer_config(provider=provider, model_id=model_id, variant=variant)
        fixture = LoopbackFixtureProvider(
            provider_id=fixture_provider_id or provider,
            base_url=FIXTURE_BASE_URL,
            models=tuple(fixture_models),
        )
        manager = _ProofSeamManager(
            data_dir=data_dir,
            configuration=configuration,
            fixture_provider=fixture,
            seam_url=seam_url,
            provider_env=manager_provider_env,
        )
        location = manager.location_for(store.project_id)

        target_artifact, _ = self._seed_artifact(
            store, artifact_id="art_fx_target", revision_id="rev_1", content=b"fixture-target"
        )
        input_artifact, _ = self._seed_artifact(
            store, artifact_id="art_fx_input", revision_id="rev_in", content=b"fixture-input"
        )

        model_ref = f"{provider}/{model_id}" + ("" if variant is None else f"#{variant}")
        mapping = runtime.record_session_mapping(
            service_instance_id=location.location_id,
            clean_location=location.directory,
            session_id=f"ses_writer_fx{seq}",
            role="writer",
            model=model_ref,
            configuration=configuration,
        )
        prompt_version = f"writer.fx{seq}"
        bounded = BoundedTask.from_mapping(
            {
                "project_id": store.project_id,
                "role": "writer",
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
                        "digest": _sha256_bytes(b"fixture-input"),
                    }
                ],
                "scope": ["section:introduction"],
                "prompt_version": prompt_version,
                "size_limit": 4096,
                "step_limit": configuration.for_role("writer").steps,
            },
            configuration=configuration,
        )
        task = runtime.record_task(
            bounded, request_id=f"req-fx-{seq}", configuration=configuration
        )
        versions_raw, _canonical, versions_digest = _versions(model_ref, prompt_version)
        dispatch = dispatch_repo.reserve_dispatch(
            task,
            request_id=f"call-fx-{seq}",
            route=eb.SESSION_ROUTE,
            stage="section_drafting",
            input_digest=_DIG_A,
            version_digest=versions_digest,
            reserve_usd="0.10",
            bound_evidence_digest=_DIG_E,
        )
        spec = prepare_session_turn(
            store,
            task=task,
            dispatch=dispatch,
            mapping=mapping,
            manager=manager,
            text="Synthetic fixture section draft from the pinned inputs.",
            versions=versions_raw,
        )
        return SimpleNamespace(
            data_dir=data_dir,
            store=store,
            runtime=runtime,
            configuration=configuration,
            fixture=fixture,
            manager=manager,
            location=location,
            mapping=mapping,
            task=task,
            dispatch=dispatch,
            spec=spec,
        )

    def _sibling(self, scenario, *, configuration=None, fixture_provider="default", **kwargs):
        """A second manager over the *same* owned root (so the location still matches)."""
        if fixture_provider == "default":
            fixture = scenario.fixture
        else:
            fixture = fixture_provider
        return _ProofSeamManager(
            data_dir=scenario.data_dir,
            configuration=configuration or scenario.configuration,
            fixture_provider=fixture,
            **kwargs,
        )

    # -- positive ---------------------------------------------------------
    def test_owned_fixture_case_is_accepted_read_only(self) -> None:
        scenario = self._fixture_scenario()
        before = scenario.manager.existing_location_calls
        self.assertIsNone(
            eb.assert_fixture_session_execution_allowed(scenario.manager, scenario.spec)
        )
        # The helper re-read the manager's owned surface (read-only) and read no
        # credential.
        self.assertEqual(scenario.manager.existing_location_calls, before + 1)
        self.assertEqual(scenario.manager.location_for_calls, 1)  # setup only
        self.assertEqual(scenario.manager.read_password_calls, 0)
        self.assertEqual(scenario.manager.launch_calls, 0)
        # The dispatch carried a *virtual* positive reserve; that alone is not proof.
        self.assertEqual(scenario.dispatch.reserve_usd, "0.10")
        self.assertEqual(scenario.spec.model, f"{FIXTURE_PROVIDER}/{WRITER_MODEL_ID}#default")

    # -- refusals ---------------------------------------------------------
    def test_non_manager_is_refused(self) -> None:
        scenario = self._fixture_scenario()
        for bogus in (object(), "manager", SimpleNamespace(configuration=scenario.configuration)):
            with self.assertRaises(eb.ExecutionBoundaryBlocked) as ctx:
                eb.assert_fixture_session_execution_allowed(bogus, scenario.spec)
            self.assertEqual(ctx.exception.code, "manager")
            self.assertIsInstance(ctx.exception, eb.FixtureSessionBlocked)

    def test_missing_fixture_provider_is_refused(self) -> None:
        scenario = self._fixture_scenario()
        sibling = self._sibling(scenario, fixture_provider=None)
        with self.assertRaises(eb.ExecutionBoundaryBlocked) as ctx:
            eb.assert_fixture_session_execution_allowed(sibling, scenario.spec)
        self.assertEqual(ctx.exception.code, "fixture")

    def test_foreign_endpoint_is_refused(self) -> None:
        scenario = self._fixture_scenario()
        sibling = self._sibling(scenario, seam_url="http://203.0.113.5:4096")
        with self.assertRaises(eb.ExecutionBoundaryBlocked) as ctx:
            eb.assert_fixture_session_execution_allowed(sibling, scenario.spec)
        self.assertEqual(ctx.exception.code, "endpoint")

    def test_foreign_fixture_provider_is_refused(self) -> None:
        scenario = self._fixture_scenario()
        sibling = self._sibling(
            scenario,
            fixture_provider=LoopbackFixtureProvider(
                provider_id="otherfixture", base_url=FIXTURE_BASE_URL, models=(WRITER_MODEL_ID,)
            ),
        )
        with self.assertRaises(eb.ExecutionBoundaryBlocked) as ctx:
            eb.assert_fixture_session_execution_allowed(sibling, scenario.spec)
        self.assertEqual(ctx.exception.code, "model")

    def test_model_not_in_fixture_models_is_refused(self) -> None:
        scenario = self._fixture_scenario()
        sibling = self._sibling(
            scenario,
            fixture_provider=LoopbackFixtureProvider(
                provider_id=FIXTURE_PROVIDER, base_url=FIXTURE_BASE_URL, models=(CRITIC_MODEL_ID,)
            ),
        )
        with self.assertRaises(eb.ExecutionBoundaryBlocked) as ctx:
            eb.assert_fixture_session_execution_allowed(sibling, scenario.spec)
        self.assertEqual(ctx.exception.code, "model")

    def test_provider_credential_env_is_refused(self) -> None:
        scenario = self._fixture_scenario()
        sibling = self._sibling(scenario, provider_env={"OPENROUTER_API_KEY": "sk-synthetic"})
        with self.assertRaises(eb.ExecutionBoundaryBlocked) as ctx:
            eb.assert_fixture_session_execution_allowed(sibling, scenario.spec)
        self.assertEqual(ctx.exception.code, "credentials")

    def test_configuration_drift_is_refused(self) -> None:
        scenario = self._fixture_scenario()
        # Mutate the manager's own configuration after construction: the recorded
        # bytes no longer match a fresh rebuild.
        scenario.manager.configuration = _writer_config(steps=8)
        with self.assertRaises(eb.ExecutionBoundaryBlocked) as ctx:
            eb.assert_fixture_session_execution_allowed(scenario.manager, scenario.spec)
        self.assertEqual(ctx.exception.code, "configuration")

    def test_drifted_role_steps_are_refused(self) -> None:
        scenario = self._fixture_scenario()
        sibling = self._sibling(scenario, configuration=_writer_config(steps=8))
        with self.assertRaises(eb.ExecutionBoundaryBlocked) as ctx:
            eb.assert_fixture_session_execution_allowed(sibling, scenario.spec)
        self.assertEqual(ctx.exception.code, "configuration")

    def test_missing_owned_location_is_refused(self) -> None:
        scenario = self._fixture_scenario()
        # A real manager with the same fixture inputs but a *different* root: no
        # owned location exists for this project there.
        other = _ProofSeamManager(
            data_dir=str(self.tmp / "foreign-root"),
            configuration=scenario.configuration,
            fixture_provider=scenario.fixture,
        )
        with self.assertRaises(eb.ExecutionBoundaryBlocked) as ctx:
            eb.assert_fixture_session_execution_allowed(other, scenario.spec)
        self.assertEqual(ctx.exception.code, "manager")

    def test_drifted_location_is_refused(self) -> None:
        scenario = self._fixture_scenario()
        other = _ProofSeamManager(
            data_dir=str(self.tmp / "other-root"),
            configuration=scenario.configuration,
            fixture_provider=scenario.fixture,
        )
        other.location_for(scenario.store.project_id)  # create a *different* location
        with self.assertRaises(eb.ExecutionBoundaryBlocked) as ctx:
            eb.assert_fixture_session_execution_allowed(other, scenario.spec)
        self.assertEqual(ctx.exception.code, "session")

    def test_forged_or_duck_typed_plan_is_refused(self) -> None:
        scenario = self._fixture_scenario()
        forged = SimpleNamespace(
            project_id=scenario.spec.project_id,
            role=scenario.spec.role,
            model=scenario.spec.model,
            clean_location=scenario.spec.clean_location,
            service_instance_id=scenario.spec.service_instance_id,
            step_limit=scenario.spec.step_limit,
        )
        for bogus in (object(), forged, scenario.spec.to_mapping(), None):
            with self.assertRaises(eb.ExecutionBoundaryBlocked) as ctx:
                eb.assert_fixture_session_execution_allowed(scenario.manager, bogus)
            self.assertEqual(ctx.exception.code, "spec")

    def test_no_imported_authority_argument_is_accepted(self) -> None:
        scenario = self._fixture_scenario()
        with self.assertRaises(TypeError):
            eb.assert_fixture_session_execution_allowed(
                scenario.manager, scenario.spec, authorized=True
            )

    # -- production routes ------------------------------------------------
    def test_production_routes_are_blocked(self) -> None:
        for route, code in (
            (eb.SESSION_ROUTE, "session_route_blocked"),
            (eb.DIRECT_ROUTE, "direct_route_blocked"),
        ):
            with self.assertRaises(eb.ExecutionBoundaryBlocked) as ctx:
                eb.assert_production_execution_supported(route)
            self.assertEqual(ctx.exception.code, code)
            self.assertIsInstance(ctx.exception, eb.ProductionExecutionBlocked)
        for bogus in ("bogus", None, 1, ""):
            with self.assertRaises(eb.ExecutionBoundaryBlocked) as ctx:
                eb.assert_production_execution_supported(bogus)
            self.assertEqual(ctx.exception.code, "route")
        self.assertEqual(eb.PRODUCTION_SUPPORTED_ROUTES, frozenset())

    def test_declared_boundary_is_non_authorizing(self) -> None:
        self.assertFalse(eb.EXECUTION_BOUNDARY_IS_OUT_OF_BAND_APPROVAL)
        self.assertFalse(eb.EXECUTION_BOUNDARY_IS_PERSISTED_PROOF)
        self.assertFalse(eb.EXECUTION_BOUNDARY_GRANTS_SCIENTIFIC_AUTHORITY)
        self.assertFalse(eb.EXECUTION_BOUNDARY_IS_OS_SANDBOX)
        self.assertFalse(eb.EXECUTION_BOUNDARY_VERIFIES_REMOTE_MODEL_SERVER)
        self.assertFalse(eb.EXECUTION_BOUNDARY_AUTHORIZES_PAID_GENERATION)
        self.assertFalse(eb.EXECUTION_BOUNDARY_AUTHORIZES_HOSTED_TRANSFER)
        self.assertTrue(eb.FIXTURE_ROUTE_IS_SYNTHETIC_LOOPBACK)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
