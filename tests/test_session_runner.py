"""Deterministic tests for the thin session submit/reconcile path (task R2-step2).

Synthetic data only: a **real** temporary project store and registry, real
persisted tasks/mappings/dispatches/configuration snapshots, and the existing
owned-manager fixture layout (``_ProofSeamManager`` + ``LoopbackFixtureProvider``).
The OpenCode client is an **injected deterministic seam** that records every call,
refuses to run while a registry lock or SQLite transaction is held, and proves the
mapping/correlation rows exist before it accepts a prompt. A real disposable
OpenCode 2.2.0 service end-to-end run is a separate acceptance task and is not
claimed here.

Covered:

* exact session/role/model/variant pins and empty context before send;
* persisted mapping + exact pre-transport intent correlation before any prompt;
* single durable admission, duplicate submit returns the existing identity;
* transport ambiguity becomes ``unknown``/reconciliation-required, never a retry;
* stateless new-instance reconciliation of an authoritative idle success, with
  actual assistant model/provider/usage metadata and an accepted result triple;
* duplicate reconciliation is a no-op on the accepted triple;
* failed/interrupted, refusal, and malformed terminals never become an accepted
  scientific result;
* wrong project/token/config/steps/spec, missing owned service, and a non-fixture
  manager are refused;
* the fresh, different-concrete-model section critic;
* remote I/O never runs under a registry lock or store transaction;
* a failing accepted-result transition rolls the proposal insert back atomically.

Run with::

    PYTHONPATH=src:tests python3 -m unittest test_session_runner
"""

from __future__ import annotations

import fcntl
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import test_session_turns as scenario_helpers  # noqa: E402
from test_execution_boundary import _ProofSeamManager  # noqa: E402

from review_article_agent.domain.errors import ProjectConflict  # noqa: E402
from review_article_agent.runtime import session_runner  # noqa: E402
from review_article_agent.runtime.dispatch import (  # noqa: E402
    DispatchStateError,
    RuntimeDispatchRepository,
)
from review_article_agent.runtime.opencode import (  # noqa: E402
    Admission,
    OpenCodeProtocolError,
    OpenCodeTransportError,
    OpenCodeV2Client,
    ReconcileResult,
)
from review_article_agent.runtime.roles import BoundedTask, RoleConfiguration  # noqa: E402
from review_article_agent.runtime.service import LoopbackFixtureProvider  # noqa: E402
from review_article_agent.storage import (  # noqa: E402
    ArtifactRepository,
    ProjectStore,
    Registry,
    paths,
)
from review_article_agent.storage.agent_runtime import RuntimeRepository  # noqa: E402
from review_article_agent.storage.session_intents import SessionIntentRepository  # noqa: E402

FIXTURE_PROVIDER = "fixturelocal"
WRITER_MODEL_ID = "writer-model"
CRITIC_MODEL_ID = "critic-model"
FIXTURE_BASE_URL = "http://127.0.0.1:9/v1"
SEAM_URL = "http://127.0.0.1:4096"

_DIG_A = scenario_helpers._DIG_A
_DIG_E = scenario_helpers._DIG_E
_sha256_bytes = scenario_helpers._sha256_bytes


def _configuration(writer_model: str = WRITER_MODEL_ID, critic_model: str = CRITIC_MODEL_ID):
    return RoleConfiguration.from_mapping(
        {
            "schema_version": 1,
            "roles": {
                "writer": {
                    "provider_id": FIXTURE_PROVIDER,
                    "model_id": writer_model,
                    "variant": "default",
                    "steps": 4,
                },
                "section_critic": {
                    "provider_id": FIXTURE_PROVIDER,
                    "model_id": critic_model,
                    "variant": "default",
                    "steps": 4,
                },
            },
        }
    )


class _ClientState:
    """Durable in-memory server state shared by fresh client instances."""

    def __init__(self) -> None:
        self.sessions: dict[str, dict] = {}
        self.messages: dict[str, list[dict]] = {}
        self.inbox: dict[str, list[dict]] = {}
        self.counter = 0


class _InMemoryClient:
    """Deterministic injected client seam over a real store and registry."""

    def __init__(self, store, registry, *, state: _ClientState | None = None) -> None:
        self.store = store
        self.registry = registry
        self.state = state if state is not None else _ClientState()
        self.calls: list[dict] = []
        self.admissions: list[dict] = []
        self.critic_calls: list[dict] = []
        self.admit_error: Exception | None = None
        self.verify_error: Exception | None = None
        self.reconcile_error: Exception | None = None
        self.collect_error: Exception | None = None
        self.verify_fail_after: int | None = None
        self.reconcile_unknown = False
        self._verify_count = 0

    def _external(self, name: str, **info) -> None:
        # Prove the call is not made under the registry lock or a store transaction.
        fd = os.open(self.registry.lock_path, os.O_RDWR)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:  # pragma: no cover - failure path
                raise AssertionError("external I/O while the registry lock is held") from exc
            else:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
        if self.store.connection.in_transaction:
            raise AssertionError("external I/O inside a store transaction")
        self.calls.append({"name": name, **info})

    def create_session(self, *, agent, model, title=None, directory=None):
        self._external("create_session", agent=agent, model=dict(model), directory=directory)
        self.state.counter += 1
        session_id = f"ses_runner{self.state.counter:04d}"
        info = {
            "id": session_id,
            "agent": agent,
            "model": dict(model),
            "projectID": "prj_fixture",
            "location": {"directory": directory or "/tmp/fixture"},
        }
        self.state.sessions[session_id] = info
        self.state.messages[session_id] = []
        self.state.inbox[session_id] = []
        return info

    def start_critic(self, *, agent, model, writer_model, title=None, directory=None):
        self.critic_calls.append({"model": dict(model), "writer_model": dict(writer_model)})
        if (model["providerID"], model["id"]) == (
            writer_model["providerID"],
            writer_model["id"],
        ):
            raise ValueError("critic model must differ from the writer model")
        return self.create_session(
            agent=agent, model=model, title=title, directory=directory
        )

    def verify_session_pins(self, session_id, *, agent, model):
        self._external("verify_session_pins", session_id=session_id)
        self._verify_count += 1
        if self.verify_error is not None:
            raise self.verify_error
        if self.verify_fail_after is not None and self._verify_count > self.verify_fail_after:
            raise OpenCodeProtocolError("session pins unavailable")
        info = self.state.sessions.get(session_id)
        if info is None:
            raise OpenCodeProtocolError("session missing")
        if info.get("agent") != agent or info.get("model") != model:
            raise OpenCodeProtocolError("session pins mismatch")
        return info

    def list_inbox(self, session_id):
        self._external("list_inbox", session_id=session_id)
        return [dict(item) for item in self.state.inbox.get(session_id, [])]

    def list_messages(self, session_id, **kwargs):
        self._external("list_messages", session_id=session_id)
        return {
            "data": [dict(message) for message in self.state.messages.get(session_id, [])],
            "cursor": {"next": None},
        }

    def collect_messages(self, session_id, *, type=None, order="asc", max_pages=None):
        self._external("collect_messages", session_id=session_id)
        if self.collect_error is not None:
            raise self.collect_error
        return [dict(message) for message in self.state.messages.get(session_id, [])], False

    def admit(self, session_id, text, admission_id, delivery="queue"):
        self._external("admit", session_id=session_id, admission_id=admission_id, text=text)
        if self.admit_error is not None:
            raise self.admit_error
        if self.state.inbox.get(session_id) or self.state.messages.get(session_id):
            raise AssertionError("prompt sent to a non-empty session")
        intent = self.store.connection.execute(
            "SELECT 1 FROM session_turn_intent WHERE project_id = ? AND admission_id = ?",
            (self.store.project_id, admission_id),
        ).fetchone()
        if intent is None:
            raise AssertionError("prompt sent before the session-turn intent was persisted")
        mapping = self.store.connection.execute(
            "SELECT 1 FROM runtime_session_mapping WHERE project_id = ? AND session_id = ?",
            (self.store.project_id, session_id),
        ).fetchone()
        if mapping is None:
            raise AssertionError("prompt sent before the session mapping was persisted")
        item = {
            "id": admission_id,
            "sessionID": session_id,
            "type": "user",
            "payload": {"text": text},
            "delivery": delivery,
        }
        self.state.inbox.setdefault(session_id, []).append(item)
        self.state.messages.setdefault(session_id, []).append(
            {"id": admission_id, "type": "user", "text": text}
        )
        self.admissions.append(
            {"session_id": session_id, "admission_id": admission_id, "text": text}
        )
        return Admission(admission_id, item)

    def reconcile(self, session_id, admission_id):
        self._external("reconcile", session_id=session_id, admission_id=admission_id)
        if self.reconcile_error is not None:
            raise self.reconcile_error
        receipt = next(
            (
                dict(item)
                for item in self.state.inbox.get(session_id, [])
                if item.get("id") == admission_id
            ),
            None,
        )
        messages = self.state.messages.get(session_id, [])
        start = next(
            (
                index
                for index, message in enumerate(messages)
                if message.get("type") == "user" and message.get("id") == admission_id
            ),
            None,
        )
        if self.reconcile_unknown or start is None:
            return ReconcileResult(admission_id, "unknown", receipt, None, reason="no server trace")
        end = len(messages)
        for index in range(start + 1, len(messages)):
            if messages[index].get("type") in ("user", "synthetic"):
                end = index
                break
        window = messages[start + 1 : end]
        idle = next((m for m in window if m.get("type") == "idle"), None)
        if idle is None:
            return ReconcileResult(
                admission_id, "in_progress", receipt, None, reason="no authoritative idle"
            )
        outcome = idle.get("outcome")
        state = {
            "succeeded": "completed",
            "failed": "failed",
            "interrupted": "interrupted",
        }.get(outcome, "unknown")
        return ReconcileResult(
            admission_id, state, receipt, {"outcome": outcome, "message": idle}
        )

    # -- test helpers ------------------------------------------------------
    def append_assistant(
        self,
        session_id,
        text="synthetic proposal text",
        *,
        model=None,
        agent=None,
        usage=None,
        error=None,
        finish=None,
    ):
        info = self.state.sessions[session_id]
        message = {
            "id": f"msg_runner{len(self.state.messages[session_id]) + 1:04d}",
            "type": "assistant",
            "agent": agent if agent is not None else info["agent"],
            "model": dict(model) if model is not None else dict(info["model"]),
            "content": [{"type": "text", "text": text}],
        }
        if usage is not None:
            message["usage"] = usage
        if error is not None:
            message["error"] = error
        if finish is not None:
            message["finish"] = finish
        self.state.messages[session_id].append(message)
        return message

    def append_idle(self, session_id, outcome="succeeded"):
        message = {
            "id": f"msg_runner{len(self.state.messages[session_id]) + 1:04d}",
            "type": "idle",
            "outcome": outcome,
        }
        self.state.messages[session_id].append(message)
        return message


class _PasswordManager(_ProofSeamManager):
    """A seam manager that can answer ``read_password`` for the default-client test."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.password_reads = 0

    def read_password(self) -> str:
        self.password_reads += 1
        return "synthetic-password"


class SessionRunnerTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="raa-session-runner-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.registry = Registry(str(self.tmp / "state"))
        self.info = self.registry.create_project(str(self.tmp / "proj"), name="Runner")
        self.store = ProjectStore.open_at(
            paths.app_dir_for(self.info.root_path), expected_project_id=self.info.project_id
        )
        self.addCleanup(self.store.close)
        self.activation = self.registry.activate_execution(self.info.project_id)
        self.token = self.activation.activation_token
        self.configuration = _configuration()
        self.fixture = LoopbackFixtureProvider(
            provider_id=FIXTURE_PROVIDER,
            base_url=FIXTURE_BASE_URL,
            models=(WRITER_MODEL_ID, CRITIC_MODEL_ID),
        )
        self.manager = _ProofSeamManager(
            data_dir=str(self.tmp / "svc"),
            configuration=self.configuration,
            fixture_provider=self.fixture,
            seam_url=SEAM_URL,
        )
        self.location = self.manager.location_for(self.info.project_id)
        self.client = _InMemoryClient(self.store, self.registry)
        self._seed_artifacts()

    # -- construction ------------------------------------------------------
    def _seed_artifacts(self) -> None:
        repo = ArtifactRepository(self.store)
        target = repo.create_artifact(kind="document", artifact_id="art_target")
        repo.import_revision(
            artifact_id=target.artifact_id,
            content=b"target-bytes",
            origin_locator="import:synthetic",
            rights=scenario_helpers._rights(
                self.info.project_id, target.artifact_id, "rev_1"
            ),
            revision_id="rev_1",
        )
        input_artifact = repo.create_artifact(kind="document", artifact_id="art_input")
        repo.import_revision(
            artifact_id=input_artifact.artifact_id,
            content=b"input-bytes",
            origin_locator="import:synthetic",
            rights=scenario_helpers._rights(
                self.info.project_id, input_artifact.artifact_id, "rev_in"
            ),
            revision_id="rev_in",
        )

    def _scenario(
        self,
        *,
        role="writer",
        configuration=None,
        request_id="req-1",
        call_id="call-1",
        text="Draft the pinned section from the exact input pins.",
        prompt_version=None,
    ):
        configuration = configuration or self.configuration
        runtime = RuntimeRepository(self.store)
        role_config = configuration.for_role(role)
        prompt_version = prompt_version or f"{role}.v1"
        bounded = BoundedTask.from_mapping(
            {
                "project_id": self.info.project_id,
                "role": role,
                "target": {
                    "project_id": self.info.project_id,
                    "artifact_id": "art_target",
                    "revision_id": "rev_1",
                },
                "input_refs": [
                    {
                        "project_id": self.info.project_id,
                        "artifact_id": "art_input",
                        "revision_id": "rev_in",
                        "digest": _sha256_bytes(b"input-bytes"),
                    }
                ],
                "scope": ["section:introduction"],
                "prompt_version": prompt_version,
                "size_limit": 4096,
                "step_limit": role_config.steps,
            },
            configuration=configuration,
        )
        task = runtime.record_task(
            bounded, request_id=request_id, configuration=configuration
        )
        versions, _canonical, versions_digest = scenario_helpers._versions(
            role_config.model.ref, prompt_version
        )
        dispatch = RuntimeDispatchRepository(self.store, runtime=runtime).reserve_dispatch(
            task,
            request_id=call_id,
            route="opencode_session",
            stage="section_drafting",
            input_digest=_DIG_A,
            version_digest=versions_digest,
            reserve_usd="0.10",
            bound_evidence_digest=_DIG_E,
        )
        return {
            "runtime": runtime,
            "task": task,
            "bounded": bounded,
            "versions": versions,
            "dispatch": dispatch,
            "text": text,
            "prompt_version": prompt_version,
        }

    def _submit(self, scenario, *, client=None, manager=None, token=None, **kwargs):
        return session_runner.submit_session_turn(
            self.store,
            registry=self.registry,
            activation_token=token if token is not None else self.token,
            task=scenario["task"],
            dispatch=scenario["dispatch"],
            manager=manager if manager is not None else self.manager,
            text=kwargs.pop("text", scenario["text"]),
            versions=kwargs.pop("versions", scenario["versions"]),
            client=client if client is not None else self.client,
            **kwargs,
        )

    def _reconcile(self, scenario, *, client=None, manager=None, token=None, store=None):
        return session_runner.reconcile_session_turn(
            store if store is not None else self.store,
            registry=self.registry,
            activation_token=token if token is not None else self.token,
            dispatch_id=scenario["dispatch"].dispatch_id,
            manager=manager if manager is not None else self.manager,
            client=client if client is not None else self.client,
        )


class SubmitSessionTurnTest(SessionRunnerTestBase):
    def test_persists_exact_pins_and_correlation_before_admit(self) -> None:
        scenario = self._scenario()
        receipt = self._submit(scenario)
        self.assertEqual(receipt.state, "admitted")
        self.assertFalse(receipt.duplicate)
        self.assertEqual(receipt.session_id, "ses_runner0001")
        self.assertEqual(receipt.admission_id, receipt.admission_id)

        intent = SessionIntentRepository(self.store).get_intent(scenario["dispatch"].dispatch_id)
        self.assertIsNotNone(intent)
        self.assertEqual(intent.admission_id, receipt.admission_id)
        self.assertEqual(intent.session_id, receipt.session_id)
        self.assertEqual(intent.task_id, scenario["task"].task_id)
        self.assertEqual(intent.fence, scenario["dispatch"].current_fence)
        spec = intent.spec()
        self.assertEqual(spec.role, "writer")
        self.assertEqual(spec.model, f"{FIXTURE_PROVIDER}/{WRITER_MODEL_ID}#default")
        self.assertTrue(spec.fresh_context_required is False)

        mapping = RuntimeRepository(self.store).get_session_mapping(intent.mapping_id)
        self.assertEqual(mapping.session_id, receipt.session_id)
        self.assertEqual(mapping.role, "writer")
        self.assertEqual(mapping.service_instance_id, self.location.location_id)

        self.assertEqual(len(self.client.admissions), 1)
        admission = self.client.admissions[0]
        self.assertEqual(admission["admission_id"], receipt.admission_id)
        self.assertEqual(admission["text"], spec.message_text)
        names = [call["name"] for call in self.client.calls]
        self.assertIn("list_inbox", names)
        self.assertIn("list_messages", names)
        self.assertLess(names.index("list_inbox"), names.index("admit"))
        self.assertLess(names.index("list_messages"), names.index("admit"))

    def test_duplicate_submit_returns_existing_identity_without_new_calls(self) -> None:
        scenario = self._scenario()
        first = self._submit(scenario)
        calls_before = len(self.client.calls)
        second = self._submit(scenario)
        self.assertTrue(second.duplicate)
        self.assertEqual(second.admission_id, first.admission_id)
        self.assertEqual(second.session_id, first.session_id)
        self.assertEqual(len(self.client.calls), calls_before)
        self.assertEqual(len(self.client.admissions), 1)

    def test_divergent_replay_is_refused_without_new_calls(self) -> None:
        scenario = self._scenario()
        self._submit(scenario)
        calls_before = len(self.client.calls)
        with self.assertRaises(session_runner.SessionRunnerConflictError):
            self._submit(scenario, text="a different bounded text")
        self.assertEqual(len(self.client.calls), calls_before)

    def test_transport_ambiguity_is_unknown_and_not_retried(self) -> None:
        scenario = self._scenario()
        self.client.admit_error = OpenCodeTransportError("synthetic transport failure")
        receipt = self._submit(scenario)
        self.assertEqual(receipt.state, "unknown")
        self.assertTrue(receipt.reconciliation_required)
        self.assertEqual(len(self.client.admissions), 0)
        self.assertEqual([call["name"] for call in self.client.calls].count("admit"), 1)
        dispatch = RuntimeDispatchRepository(self.store).get_dispatch(
            scenario["dispatch"].dispatch_id
        )
        self.assertEqual(dispatch.state, "unknown")

    def test_critic_uses_a_fresh_different_concrete_model_session(self) -> None:
        runtime = RuntimeRepository(self.store)
        runtime.record_session_mapping(
            service_instance_id=self.location.location_id,
            clean_location=self.location.directory,
            session_id="ses_writer_existing",
            role="writer",
            model=self.configuration.for_role("writer").model,
            configuration=self.configuration,
        )
        scenario = self._scenario(
            role="section_critic",
            request_id="req-critic",
            call_id="call-critic",
            prompt_version="critic.v1",
        )
        receipt = self._submit(scenario)
        self.assertEqual(receipt.state, "admitted")
        self.assertNotEqual(receipt.session_id, "ses_writer_existing")
        self.assertEqual(len(self.client.critic_calls), 1)
        critic_call = self.client.critic_calls[0]
        self.assertNotEqual(
            (critic_call["model"]["providerID"], critic_call["model"]["id"]),
            (critic_call["writer_model"]["providerID"], critic_call["writer_model"]["id"]),
        )

    def test_invalid_critic_configuration_is_refused_before_creating_a_session(self) -> None:
        bad_configuration = _configuration(writer_model=WRITER_MODEL_ID, critic_model=WRITER_MODEL_ID)
        scenario = self._scenario(
            role="section_critic",
            configuration=bad_configuration,
            request_id="req-bad-critic",
            call_id="call-bad-critic",
        )
        manager = _ProofSeamManager(
            data_dir=str(self.tmp / "svc"),
            configuration=bad_configuration,
            fixture_provider=self.fixture,
            seam_url=SEAM_URL,
        )
        with self.assertRaises(session_runner.SessionRunnerValidationError):
            self._submit(scenario, manager=manager)
        self.assertEqual(self.client.calls, [])

    def test_non_fixture_manager_is_blocked_before_any_prompt(self) -> None:
        scenario = self._scenario()
        plain_manager = _ProofSeamManager(
            data_dir=str(self.tmp / "svc"),
            configuration=self.configuration,
            fixture_provider=None,
            seam_url=SEAM_URL,
        )
        with self.assertRaises(session_runner.ExecutionBoundaryBlocked):
            self._submit(scenario, manager=plain_manager)
        self.assertEqual(len(self.client.admissions), 0)
        dispatch = RuntimeDispatchRepository(self.store).get_dispatch(
            scenario["dispatch"].dispatch_id
        )
        self.assertEqual(dispatch.state, "reserved")

    def test_no_imported_authority_argument_is_accepted(self) -> None:
        scenario = self._scenario()
        with self.assertRaises(TypeError):
            session_runner.submit_session_turn(
                self.store,
                registry=self.registry,
                activation_token=self.token,
                task=scenario["task"],
                dispatch=scenario["dispatch"],
                manager=self.manager,
                text=scenario["text"],
                versions=scenario["versions"],
                client=self.client,
                authorized=True,
            )

    def test_default_client_is_the_real_packaged_client(self) -> None:
        manager = _PasswordManager(
            data_dir=str(self.tmp / "svc"),
            configuration=self.configuration,
            fixture_provider=self.fixture,
            seam_url=SEAM_URL,
        )
        client = session_runner._build_default_client(manager, self.location)
        self.assertIsInstance(client, OpenCodeV2Client)
        self.assertEqual(client.base_url, SEAM_URL)
        self.assertEqual(client.directory, self.location.directory)
        self.assertEqual(manager.password_reads, 1)


class SubmitRefusalTest(SessionRunnerTestBase):
    def test_wrong_token_is_refused_without_client_calls(self) -> None:
        scenario = self._scenario()
        with self.assertRaises(ProjectConflict):
            self._submit(scenario, token="act_" + "f" * 32)
        self.assertEqual(self.client.calls, [])

    def test_second_project_cannot_inherit_execution_authority(self) -> None:
        other = self.registry.create_project(str(self.tmp / "other"), name="Other")
        other_store = ProjectStore.open_at(
            paths.app_dir_for(other.root_path), expected_project_id=other.project_id
        )
        self.addCleanup(other_store.close)
        scenario = self._scenario()
        with self.assertRaises(ProjectConflict):
            session_runner.submit_session_turn(
                other_store,
                registry=self.registry,
                activation_token=self.token,
                task=scenario["task"],
                dispatch=scenario["dispatch"],
                manager=self.manager,
                text=scenario["text"],
                versions=scenario["versions"],
                client=self.client,
            )
        self.assertEqual(self.client.calls, [])
        self.assertEqual(RuntimeRepository(other_store).list_session_mappings(), [])

    def test_wrong_configuration_is_refused_before_creating_a_session(self) -> None:
        scenario = self._scenario()
        other_configuration = _configuration(writer_model="other-model")
        manager = _ProofSeamManager(
            data_dir=str(self.tmp / "svc"),
            configuration=other_configuration,
            fixture_provider=self.fixture,
            seam_url=SEAM_URL,
        )
        with self.assertRaises(session_runner.SessionRunnerValidationError):
            self._submit(scenario, manager=manager)
        self.assertEqual(self.client.calls, [])

    def test_wrong_steps_are_refused_before_creating_a_session(self) -> None:
        scenario = self._scenario()
        other_configuration = RoleConfiguration.from_mapping(
            {
                "schema_version": 1,
                "roles": {
                    "writer": {
                        "provider_id": FIXTURE_PROVIDER,
                        "model_id": WRITER_MODEL_ID,
                        "variant": "default",
                        "steps": 8,
                    },
                    "section_critic": {
                        "provider_id": FIXTURE_PROVIDER,
                        "model_id": CRITIC_MODEL_ID,
                        "variant": "default",
                        "steps": 4,
                    },
                },
            }
        )
        manager = _ProofSeamManager(
            data_dir=str(self.tmp / "svc"),
            configuration=other_configuration,
            fixture_provider=self.fixture,
            seam_url=SEAM_URL,
        )
        with self.assertRaises(session_runner.SessionRunnerValidationError):
            self._submit(scenario, manager=manager)
        self.assertEqual(self.client.calls, [])

    def test_wrong_versions_are_refused_before_creating_a_session(self) -> None:
        scenario = self._scenario()
        bad_versions, _canonical, _digest = scenario_helpers._versions(
            f"{FIXTURE_PROVIDER}/{WRITER_MODEL_ID}#default",
            scenario["prompt_version"],
            input_digest="b" * 64,
        )
        with self.assertRaises(session_runner.SessionRunnerValidationError):
            self._submit(scenario, versions=bad_versions)
        self.assertEqual(self.client.calls, [])


class ReconcileSessionTurnTest(SessionRunnerTestBase):
    def _admitted(self, scenario):
        receipt = self._submit(scenario)
        self.assertEqual(receipt.state, "admitted")
        return receipt

    def test_new_instance_reconciles_completed_with_exact_metadata(self) -> None:
        scenario = self._scenario()
        receipt = self._admitted(scenario)
        self.client.append_assistant(
            receipt.session_id,
            text="Synthetic bounded proposal.",
            usage={"input_tokens": 12, "output_tokens": 4, "cost": "0.0002"},
        )
        self.client.append_idle(receipt.session_id, "succeeded")

        store2 = ProjectStore.open_at(
            paths.app_dir_for(self.info.root_path), expected_project_id=self.info.project_id
        )
        self.addCleanup(store2.close)
        client2 = _InMemoryClient(store2, self.registry, state=self.client.state)
        outcome = self._reconcile(scenario, client=client2, store=store2)
        self.assertEqual(outcome.state, "completed")
        self.assertFalse(outcome.reconciliation_required)
        self.assertEqual(outcome.result_kind, session_runner.SESSION_RUNNER_RESULT_KIND)
        self.assertIsNotNone(outcome.proposal_record_id)
        self.assertEqual(outcome.cost_state, "known")
        self.assertEqual(outcome.cost_amount_usd, "0.0002")

        proposal = RuntimeRepository(store2).get_proposal(outcome.proposal_record_id)
        self.assertIsNotNone(proposal)
        content = json.loads(proposal.canonical.decode("utf-8"))["content"]
        self.assertEqual(content["text"], "Synthetic bounded proposal.")
        self.assertEqual(content["actual_model"], f"{FIXTURE_PROVIDER}/{WRITER_MODEL_ID}#default")
        self.assertEqual(content["actual_provider"], FIXTURE_PROVIDER)
        self.assertEqual(content["actual_model_id"], WRITER_MODEL_ID)
        self.assertEqual(content["actual_variant"], "default")
        self.assertEqual(content["actual_cost_usd"], "0.0002")
        self.assertEqual(content["actual_usage"]["input_tokens"], 12)
        self.assertEqual(content["actual_usage"]["reported_cost"], "0.0002")

        dispatch = RuntimeDispatchRepository(store2).get_dispatch(scenario["dispatch"].dispatch_id)
        self.assertEqual(dispatch.state, "completed")
        self.assertEqual(dispatch.result_ref, outcome.proposal_record_id)
        self.assertEqual(dispatch.result_digest, proposal.stored_digest)

        reconcile_calls = [call for call in client2.calls if call["name"] == "reconcile"]
        self.assertEqual(len(reconcile_calls), 1)
        self.assertEqual(reconcile_calls[0]["session_id"], receipt.session_id)
        self.assertEqual(reconcile_calls[0]["admission_id"], receipt.admission_id)
        self.assertIn("collect_messages", [call["name"] for call in client2.calls])

        # Duplicate reconciliation is a no-op on the accepted triple.
        calls_before = len(client2.calls)
        again = self._reconcile(scenario, client=client2, store=store2)
        self.assertTrue(again.duplicate)
        self.assertEqual(again.state, "completed")
        self.assertEqual(again.proposal_record_id, outcome.proposal_record_id)
        self.assertEqual(len(client2.calls), calls_before)
        self.assertEqual(len(RuntimeRepository(store2).list_proposals()), 1)

    def test_unknown_cost_retains_the_reserve(self) -> None:
        scenario = self._scenario()
        receipt = self._admitted(scenario)
        self.client.append_assistant(
            receipt.session_id, usage={"input_tokens": 3, "output_tokens": 1}
        )
        self.client.append_idle(receipt.session_id, "succeeded")
        outcome = self._reconcile(scenario)
        self.assertEqual(outcome.state, "completed")
        self.assertEqual(outcome.cost_state, "unknown")
        self.assertIsNone(outcome.cost_amount_usd)
        dispatch = RuntimeDispatchRepository(self.store).get_dispatch(
            scenario["dispatch"].dispatch_id
        )
        self.assertEqual(dispatch.cost_state, "unknown")
        self.assertEqual(dispatch.reserve_usd, "0.10")
        proposal = RuntimeRepository(self.store).get_proposal(outcome.proposal_record_id)
        content = json.loads(proposal.canonical.decode("utf-8"))["content"]
        self.assertIsNone(content["actual_cost_usd"])

    def test_failed_authoritative_terminal_marks_failed_without_a_result(self) -> None:
        scenario = self._scenario()
        receipt = self._admitted(scenario)
        self.client.append_assistant(receipt.session_id, text="partial")
        self.client.append_idle(receipt.session_id, "failed")
        outcome = self._reconcile(scenario)
        self.assertEqual(outcome.state, "failed")
        dispatch = RuntimeDispatchRepository(self.store).get_dispatch(
            scenario["dispatch"].dispatch_id
        )
        self.assertEqual(dispatch.state, "failed")
        self.assertIsNone(dispatch.result_ref)
        self.assertEqual(RuntimeRepository(self.store).list_proposals(), [])

    def test_refusal_and_malformed_success_never_become_a_result(self) -> None:
        for label, message_kwargs in (
            ("refusal", {"finish": "refusal"}),
            ("malformed-model", {"model": {"providerID": FIXTURE_PROVIDER, "id": CRITIC_MODEL_ID}}),
        ):
            with self.subTest(label=label):
                scenario = self._scenario(request_id=f"req-{label}", call_id=f"call-{label}")
                receipt = self._admitted(scenario)
                self.client.append_assistant(receipt.session_id, **message_kwargs)
                self.client.append_idle(receipt.session_id, "succeeded")
                outcome = self._reconcile(scenario)
                self.assertEqual(outcome.state, "unknown")
                self.assertTrue(outcome.reconciliation_required)
                dispatch = RuntimeDispatchRepository(self.store).get_dispatch(
                    scenario["dispatch"].dispatch_id
                )
                self.assertNotEqual(dispatch.state, "completed")
                self.assertIsNone(dispatch.result_ref)
                self.assertEqual(
                    RuntimeRepository(self.store).proposals_for_task(scenario["task"].task_id),
                    [],
                )

    def test_missing_owned_service_is_explicit_unknown_not_success(self) -> None:
        scenario = self._scenario()
        self._admitted(scenario)
        foreign_manager = _ProofSeamManager(
            data_dir=str(self.tmp / "foreign-service"),
            configuration=self.configuration,
            fixture_provider=self.fixture,
            seam_url=SEAM_URL,
        )
        outcome = self._reconcile(scenario, manager=foreign_manager)
        self.assertEqual(outcome.state, "unknown")
        self.assertTrue(outcome.reconciliation_required)
        dispatch = RuntimeDispatchRepository(self.store).get_dispatch(
            scenario["dispatch"].dispatch_id
        )
        self.assertNotEqual(dispatch.state, "completed")

    def test_unreadable_remote_outcome_is_unknown_and_slot_held(self) -> None:
        scenario = self._scenario()
        self._admitted(scenario)
        self.client.reconcile_unknown = True
        outcome = self._reconcile(scenario)
        self.assertEqual(outcome.state, "unknown")
        dispatch = RuntimeDispatchRepository(self.store).get_dispatch(
            scenario["dispatch"].dispatch_id
        )
        self.assertEqual(dispatch.state, "admitted")
        self.assertIsNone(dispatch.result_ref)

    def test_failed_accept_transition_rolls_the_proposal_back_atomically(self) -> None:
        scenario = self._scenario()
        receipt = self._admitted(scenario)
        self.client.append_assistant(receipt.session_id, text="accepted candidate")
        self.client.append_idle(receipt.session_id, "succeeded")
        original = RuntimeDispatchRepository._accept_result_in_transaction

        def _boom(self, *args, **kwargs):
            raise DispatchStateError("synthetic accept failure")

        RuntimeDispatchRepository._accept_result_in_transaction = _boom
        try:
            outcome = self._reconcile(scenario)
        finally:
            RuntimeDispatchRepository._accept_result_in_transaction = original
        self.assertEqual(outcome.state, "unknown")
        self.assertEqual(RuntimeRepository(self.store).list_proposals(), [])
        dispatch = RuntimeDispatchRepository(self.store).get_dispatch(
            scenario["dispatch"].dispatch_id
        )
        self.assertEqual(dispatch.state, "admitted")
        self.assertIsNone(dispatch.result_ref)

    def test_external_io_is_never_performed_under_a_lock_or_transaction(self) -> None:
        scenario = self._scenario()
        receipt = self._admitted(scenario)
        self.client.append_assistant(receipt.session_id)
        self.client.append_idle(receipt.session_id, "succeeded")
        self._reconcile(scenario)
        names = {call["name"] for call in self.client.calls}
        self.assertIn("admit", names)
        self.assertIn("reconcile", names)
        self.assertIn("collect_messages", names)
        # The seam raises on any lock/transaction violation, so reaching this
        # point with the expected calls recorded proves the ordering.
        self.assertGreaterEqual(len(self.client.calls), 5)

    def test_wrong_token_is_refused_before_remote_io(self) -> None:
        scenario = self._scenario()
        self._admitted(scenario)
        calls_before = len(self.client.calls)
        with self.assertRaises(ProjectConflict):
            self._reconcile(scenario, token="act_" + "e" * 32)
        self.assertEqual(len(self.client.calls), calls_before)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
