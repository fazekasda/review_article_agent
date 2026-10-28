#!/usr/bin/env python3
"""Real disposable OpenCode 2.2.0 **end-to-end** acceptance for the integrated
application-facing ``session_runner`` (task R3-runtime-acceptance).

This suite drives a **real**, disposable, app-owned ``opencode`` v2.2.0 service
through the packaged runner boundary — :func:`session_runner.submit_session_turn`
and :func:`session_runner.reconcile_session_turn` — using the **default real
client** (:class:`OpenCodeV2Client`, never an injected seam), a **real** temporary
project store + registry, and the real owned :class:`OpenCodeServiceManager`.

What is real and what is a fixture (stated plainly)
---------------------------------------------------

* **REAL** — the ``opencode`` v2.2.0 process, its HTTP API, HTTP Basic auth,
  session creation with an explicit role/model, durable prompt admission, SQLite
  persistence, the server-owned agent turn, per-project owned locations, the
  durable session-turn intent/mapping/dispatch rows, the app-owned service
  restart, and the runner's own default client construction.
* **FIXTURE** — *only* the model transport. ``tests/_runtime_model_fixture.py`` is
  a child process: a loopback OpenAI-compatible server returning a deterministic
  synthetic reply after a bounded, request-controlled delay. There is **no paid
  provider call, no provider credential, and no network egress**. The synthetic
  reply is a stub and is **not** a scientific claim. The real cost of every turn
  is therefore exactly ``0``.

Deliberate non-goals (do not overclaim)
---------------------------------------

* The suite never reads or prints OpenCode's private credential/registration
  bytes; it only hashes the *ordinary* service-registration files before/after to
  prove they are untouched.
* Same-UID containment is configuration, not an OS sandbox.

Observed real model-usage schema (recorded evidence)
----------------------------------------------------

The real 2.2.0 assistant message carries ``tokens`` (not ``usage``) shaped
``{"input", "output", "reasoning", "cache": {"read", "write"}}`` and a top-level
``cost`` (``0`` for the fixture transport). This suite asserts the runner's
accepted proposal against that **real** schema.

Run with::

    PYTHONPATH=src:tests python3 -m unittest test_session_runner_acceptance
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
TESTS = Path(__file__).resolve().parent
if str(TESTS) not in sys.path:
    sys.path.insert(0, str(TESTS))

import test_session_turns as _turns  # noqa: E402
from test_step04_service_acceptance import (  # noqa: E402
    _binary_version,
    _fingerprint,
    _FixtureModel,
    _teardown_owned_service,
)

from review_article_agent.runtime import roles, session_runner, service  # noqa: E402
from review_article_agent.runtime.dispatch import RuntimeDispatchRepository  # noqa: E402
from review_article_agent.runtime.opencode import OpenCodeV2Client  # noqa: E402
from review_article_agent.runtime.roles import BoundedTask, RoleConfiguration  # noqa: E402
from review_article_agent.runtime.service import (  # noqa: E402
    LoopbackFixtureProvider,
    OpenCodeServiceManager,
)
from review_article_agent.storage import (  # noqa: E402
    ArtifactRepository,
    ProjectStore,
    Registry,
    paths,
)
from review_article_agent.storage.agent_runtime import RuntimeRepository  # noqa: E402
from review_article_agent.storage.session_intents import SessionIntentRepository  # noqa: E402
from review_article_agent.worker.engine import PROTOCOL_VERSION, WorkerEngine  # noqa: E402

# --------------------------------------------------------------------------- #
# Constants / binary gate
# --------------------------------------------------------------------------- #

#: Approved external scratch base; every service root is a unique child of it.
SCRATCH_BASE = "/tmp/opencode"

FIXTURE_PROVIDER = "fixturelocal"
WRITER_MODEL_ID = "writer-model"
CRITIC_MODEL_ID = "critic-model"

#: The exact ``Model.Ref`` the runner pins (variant explicit; see step04 suite).
WRITER_MODEL = {"providerID": FIXTURE_PROVIDER, "id": WRITER_MODEL_ID, "variant": "default"}
CRITIC_MODEL = {"providerID": FIXTURE_PROVIDER, "id": CRITIC_MODEL_ID, "variant": "default"}

#: Delay (ms) that holds a turn in-flight long enough to observe admission and
#: to restart the owned service mid-turn.
TURN_DELAY_MS = 8000

#: The ordinary OpenCode service-registration files that must never move.
ORDINARY_REGISTRATIONS = (
    Path(os.path.expanduser("~/.config/opencode/service.json")),
    Path(os.path.expanduser("~/.local/state/opencode/service.json")),
)

OPENCODE_BIN = shutil.which("opencode")
HAVE_REAL = bool(OPENCODE_BIN) and _binary_version(OPENCODE_BIN or "") == service.OPENCODE_PINNED_VERSION


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _configuration(writer_model: str = WRITER_MODEL_ID, critic_model: str = CRITIC_MODEL_ID) -> RoleConfiguration:
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


def _fixture_provider(fixture: _FixtureModel) -> LoopbackFixtureProvider:
    return LoopbackFixtureProvider(
        provider_id=FIXTURE_PROVIDER,
        base_url=fixture.base_url,
        models=(WRITER_MODEL_ID, CRITIC_MODEL_ID),
    )


def _open_store(info) -> ProjectStore:
    return ProjectStore.open_at(
        paths.app_dir_for(info.root_path), expected_project_id=info.project_id
    )


def _seed_artifacts(store: ProjectStore, project_id: str) -> None:
    repo = ArtifactRepository(store)
    target = repo.create_artifact(kind="document", artifact_id="art_target")
    repo.import_revision(
        artifact_id=target.artifact_id,
        content=b"target-bytes",
        origin_locator="import:synthetic",
        rights=_turns._rights(project_id, target.artifact_id, "rev_1"),
        revision_id="rev_1",
    )
    source = repo.create_artifact(kind="document", artifact_id="art_input")
    repo.import_revision(
        artifact_id=source.artifact_id,
        content=b"input-bytes",
        origin_locator="import:synthetic",
        rights=_turns._rights(project_id, source.artifact_id, "rev_in"),
        revision_id="rev_in",
    )


def _scenario(
    store: ProjectStore,
    project_id: str,
    *,
    role: str = "writer",
    configuration: RoleConfiguration | None = None,
    request_id: str = "req-1",
    call_id: str = "call-1",
    text: str = "Draft the pinned section from the exact input pins.",
    prompt_version: str | None = None,
):
    """Persist one real bounded task + reserved ``opencode_session`` dispatch."""
    configuration = configuration or _configuration()
    runtime = RuntimeRepository(store)
    role_config = configuration.for_role(role)
    prompt_version = prompt_version or f"{role}.v1"
    bounded = BoundedTask.from_mapping(
        {
            "project_id": project_id,
            "role": role,
            "target": {"project_id": project_id, "artifact_id": "art_target", "revision_id": "rev_1"},
            "input_refs": [
                {
                    "project_id": project_id,
                    "artifact_id": "art_input",
                    "revision_id": "rev_in",
                    "digest": _turns._sha256_bytes(b"input-bytes"),
                }
            ],
            "scope": ["section:introduction"],
            "prompt_version": prompt_version,
            "size_limit": 4096,
            "step_limit": role_config.steps,
        },
        configuration=configuration,
    )
    task = runtime.record_task(bounded, request_id=request_id, configuration=configuration)
    versions, _canonical, versions_digest = _turns._versions(role_config.model.ref, prompt_version)
    dispatch = RuntimeDispatchRepository(store, runtime=runtime).reserve_dispatch(
        task,
        request_id=call_id,
        route="opencode_session",
        stage="section_drafting",
        input_digest=_turns._DIG_A,
        version_digest=versions_digest,
        reserve_usd="0.10",
        bound_evidence_digest=_turns._DIG_E,
    )
    return {"task": task, "dispatch": dispatch, "versions": versions, "text": text}


def _client(manager: OpenCodeServiceManager) -> OpenCodeV2Client:
    conn = manager.connection()
    return OpenCodeV2Client(
        conn.base_url,
        auth="basic",
        username=conn.username,
        password=manager.read_password(),
        timeout=60,
    )


def _run_child(script: Path, env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(script)],
        capture_output=True,
        text=True,
        env=env,
        timeout=180,
    )


def _last_json_line(proc: subprocess.CompletedProcess) -> dict:
    for line in reversed((proc.stdout or "").strip().splitlines()):
        line = line.strip()
        if line:
            return json.loads(line)
    raise AssertionError(f"child produced no JSON line; stderr={proc.stderr!r}")


def _delayed_requests(fixture: _FixtureModel, *, minimum_ms: int = TURN_DELAY_MS) -> list[dict]:
    """Fixture log entries whose parsed ``DELAY_MS`` is at least ``minimum_ms``."""
    return [e for e in fixture.requests() if int(e.get("delayMs") or 0) >= minimum_ms]


def _wait_for_delayed_request(fixture: _FixtureModel, *, minimum_ms: int = TURN_DELAY_MS, timeout: float = 30.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _delayed_requests(fixture, minimum_ms=minimum_ms):
            return True
        time.sleep(0.2)
    return False


# --------------------------------------------------------------------------- #
# Child-process scripts (a *separate process* per runner call)
# --------------------------------------------------------------------------- #

_CHILD_PREAMBLE = """
import json, os, sys
sys.path.insert(0, os.environ["RAA_SRC"])
from review_article_agent.runtime import session_runner
from review_article_agent.runtime.dispatch import RuntimeDispatchRepository
from review_article_agent.runtime.roles import RoleConfiguration
from review_article_agent.runtime.service import LoopbackFixtureProvider, OpenCodeServiceManager
from review_article_agent.storage import ProjectStore, Registry, paths
from review_article_agent.storage.agent_runtime import RuntimeRepository

configuration = RoleConfiguration.from_mapping(json.load(open(os.environ["RAA_CONFIG_FILE"])))
fixture = LoopbackFixtureProvider(
    provider_id=os.environ["RAA_FIXTURE_PROVIDER"],
    base_url=os.environ["RAA_FIXTURE_URL"],
    models=tuple(m for m in os.environ["RAA_FIXTURE_MODELS"].split(",") if m),
)
manager = OpenCodeServiceManager(
    data_dir=os.environ["RAA_DATA_DIR"], configuration=configuration,
    binary=os.environ["RAA_BINARY"], fixture_provider=fixture, start_timeout=45.0,
)
registry = Registry(os.environ["RAA_REGISTRY_DIR"])
info = registry.get(os.environ["RAA_PROJECT_ID"])
store = ProjectStore.open_at(paths.app_dir_for(info.root_path), expected_project_id=os.environ["RAA_PROJECT_ID"])
runtime = RuntimeRepository(store)
"""

_CHILD_SUBMIT_SCRIPT = _CHILD_PREAMBLE + """
task = runtime.get_task(os.environ["RAA_TASK_ID"])
dispatch = RuntimeDispatchRepository(store, runtime=runtime).get_dispatch(os.environ["RAA_DISPATCH_ID"])
versions = json.load(open(os.environ["RAA_VERSIONS_FILE"]))
receipt = session_runner.submit_session_turn(
    store, registry=registry, activation_token=os.environ["RAA_TOKEN"],
    task=task, dispatch=dispatch, manager=manager,
    text=os.environ["RAA_TEXT"], versions=versions,
)
print(json.dumps({
    "state": receipt.state, "session_id": receipt.session_id,
    "admission_id": receipt.admission_id, "dispatch_id": receipt.dispatch_id,
    "intent_id": receipt.intent_id, "duplicate": receipt.duplicate,
    "reconciliation_required": receipt.reconciliation_required,
}))
"""

_CHILD_RECONCILE_SCRIPT = _CHILD_PREAMBLE + """
import time
deadline = time.monotonic() + float(os.environ.get("RAA_RECONCILE_DEADLINE", "120"))
last = None
while time.monotonic() < deadline:
    last = session_runner.reconcile_session_turn(
        store, registry=registry, activation_token=os.environ["RAA_TOKEN"],
        dispatch_id=os.environ["RAA_DISPATCH_ID"], manager=manager,
    )
    if last.state in ("completed", "failed"):
        break
    time.sleep(1.0)
print(json.dumps({
    "state": last.state, "duplicate": last.duplicate,
    "reconciliation_required": last.reconciliation_required,
    "proposal_record_id": last.proposal_record_id, "result_kind": last.result_kind,
    "result_ref": last.result_ref, "cost_state": last.cost_state,
    "cost_amount_usd": last.cost_amount_usd, "reason": last.reason,
}))
"""


# --------------------------------------------------------------------------- #
# Scenario 1 + 2: one shared real owned service
# --------------------------------------------------------------------------- #


@unittest.skipUnless(HAVE_REAL, "opencode v2.2.0 is not installed")
class RunnerE2ETests(unittest.TestCase):
    """Child submit + separate-process reconcile, and the fresh critic."""

    @classmethod
    def setUpClass(cls) -> None:
        os.makedirs(SCRATCH_BASE, exist_ok=True)
        cls._cleanup_state: dict = {"manager": None, "fixture": None, "scratch": None, "leaked": False}
        cls.addClassCleanup(_teardown_owned_service, cls._cleanup_state)
        cls.scratch = Path(tempfile.mkdtemp(dir=SCRATCH_BASE, prefix="raa-runner-e2e-"))
        cls._cleanup_state["scratch"] = cls.scratch
        cls.fixture = _FixtureModel(cls.scratch, canary=cls.scratch / "fixture-canary.txt")
        cls._cleanup_state["fixture"] = cls.fixture
        cls.data_dir = cls.scratch / "data"
        cls.data_dir.mkdir(mode=0o700)
        cls.configuration = _configuration()
        cls.manager = OpenCodeServiceManager(
            data_dir=str(cls.data_dir),
            configuration=cls.configuration,
            binary=OPENCODE_BIN,
            fixture_provider=_fixture_provider(cls.fixture),
            start_timeout=60.0,
        )
        cls._cleanup_state["manager"] = cls.manager
        cls.start_status = cls.manager.start()
        cls.registry = Registry(str(cls.scratch / "state"))

    def _project(self, name: str):
        info = self.registry.create_project(str(self.scratch / name), name=name)
        self.manager.location_for(info.project_id)  # create the owned location
        store = _open_store(info)
        self.addCleanup(store.close)
        _seed_artifacts(store, info.project_id)
        # One project holds execution at a time; release any prior activation so
        # this test's project can acquire its own token.
        current = self.registry.activation()
        if current.state != "none":
            self.registry.release_execution(current.activation_token)
        token = self.registry.activate_execution(info.project_id).activation_token
        self.addCleanup(self._release, token)
        return info, store, token

    def _release(self, token: str) -> None:
        try:
            current = self.registry.activation()
            if current.state != "none" and current.activation_token == token:
                self.registry.release_execution(token)
        except Exception:  # noqa: BLE001 - best-effort cleanup only
            pass

    def _child_env(self, info, scenario, token, *, text: str) -> dict:
        config_file = self.scratch / f"config-{info.project_id}.json"
        config_file.write_text(json.dumps(self.configuration.as_mapping()), encoding="utf-8")
        versions_file = self.scratch / f"versions-{info.project_id}.json"
        versions_file.write_text(json.dumps(scenario["versions"]), encoding="utf-8")
        return {
            **os.environ,
            "RAA_SRC": str(SRC),
            "RAA_CONFIG_FILE": str(config_file),
            "RAA_FIXTURE_PROVIDER": FIXTURE_PROVIDER,
            "RAA_FIXTURE_URL": self.fixture.base_url,
            "RAA_FIXTURE_MODELS": f"{WRITER_MODEL_ID},{CRITIC_MODEL_ID}",
            "RAA_DATA_DIR": str(self.data_dir),
            "RAA_BINARY": OPENCODE_BIN,
            "RAA_REGISTRY_DIR": str(self.scratch / "state"),
            "RAA_PROJECT_ID": info.project_id,
            "RAA_TASK_ID": scenario["task"].task_id,
            "RAA_DISPATCH_ID": scenario["dispatch"].dispatch_id,
            "RAA_VERSIONS_FILE": str(versions_file),
            "RAA_TOKEN": token,
            "RAA_TEXT": text,
        }

    def test_child_submits_admission_then_separate_process_reconciles_one_proposal(self) -> None:
        info, store, token = self._project("writer-proj")
        marker = f"r3-writer-{uuid.uuid4().hex[:8]}"
        text = f"{marker} DELAY_MS={TURN_DELAY_MS}"
        scenario = _scenario(store, info.project_id, text=text, request_id="req-w", call_id="call-w")

        submit_script = self.scratch / "child_submit.py"
        submit_script.write_text(_CHILD_SUBMIT_SCRIPT, encoding="utf-8")
        pid_before = self.manager.status().pid

        # (1) A child process submits the turn and exits with an *admission*.
        submitted = _run_child(submit_script, self._child_env(info, scenario, token, text=text))
        self.assertEqual(submitted.returncode, 0, submitted.stderr)
        receipt = _last_json_line(submitted)
        self.assertEqual(receipt["state"], "admitted")
        self.assertFalse(receipt["reconciliation_required"])
        self.assertTrue(receipt["session_id"].startswith("ses"))
        self.assertTrue(receipt["admission_id"].startswith("msg_"))
        self.assertNotIn("completed", json.dumps(receipt))

        # (2) The turn is NOT terminal at child exit (the fixture is still holding).
        early = _client(self.manager).reconcile(receipt["session_id"], receipt["admission_id"])
        self.assertIn(early.state, ("queued", "in_progress"))
        self.assertFalse(early.is_complete)

        # (3) Closing the child did not stop the owned service.
        status = self.manager.status()
        self.assertTrue(status.running)
        self.assertEqual(status.pid, pid_before)
        self.assertTrue(self.manager.is_owned_root())

        # The delayed turn really reached the model transport.
        self.assertTrue(_wait_for_delayed_request(self.fixture), "delayed turn never reached the transport")

        # (4) A *separate* process reconciles from durable messages and persists
        #     exactly one accepted proposal.
        reconcile_script = self.scratch / "child_reconcile.py"
        reconcile_script.write_text(_CHILD_RECONCILE_SCRIPT, encoding="utf-8")
        reconciled = _run_child(reconcile_script, self._child_env(info, scenario, token, text=text))
        self.assertEqual(reconciled.returncode, 0, reconciled.stderr)
        outcome = _last_json_line(reconciled)
        self.assertEqual(outcome["state"], "completed")
        self.assertEqual(outcome["result_kind"], session_runner.SESSION_RUNNER_RESULT_KIND)
        self.assertEqual(outcome["cost_state"], "known")
        self.assertEqual(outcome["cost_amount_usd"], "0")
        proposal_record_id = outcome["proposal_record_id"]
        self.assertTrue(proposal_record_id)

        # (5) Inspect the durable store through a fresh handle.
        inspect = _open_store(info)
        self.addCleanup(inspect.close)
        runtime = RuntimeRepository(inspect)
        proposals = runtime.list_proposals()
        self.assertEqual(len(proposals), 1)
        proposal = proposals[0]
        self.assertEqual(proposal.record_id, proposal_record_id)
        dispatch = RuntimeDispatchRepository(inspect).get_dispatch(scenario["dispatch"].dispatch_id)
        self.assertEqual(dispatch.state, "completed")
        self.assertEqual(dispatch.result_kind, session_runner.SESSION_RUNNER_RESULT_KIND)
        self.assertEqual(dispatch.result_ref, proposal_record_id)
        self.assertEqual(dispatch.result_digest, proposal.stored_digest)
        self.assertEqual(dispatch.reserve_usd, "0.10")

        # (6) The proposal carries the REAL model/usage/cost metadata.
        content = json.loads(proposal.canonical.decode("utf-8"))["content"]
        self.assertEqual(content["actual_model"], f"{FIXTURE_PROVIDER}/{WRITER_MODEL_ID}#default")
        self.assertEqual(content["actual_provider"], FIXTURE_PROVIDER)
        self.assertEqual(content["actual_model_id"], WRITER_MODEL_ID)
        self.assertEqual(content["actual_variant"], "default")
        self.assertEqual(content["actual_cost_usd"], "0")
        self.assertEqual(content["final_cost_usd"], "0")
        # The real 2.2.0 usage shape (tokens, incl. cache read/write).
        usage = content["actual_usage"]
        self.assertEqual(
            {k: usage[k] for k in ("input", "output", "reasoning")}, {"input": 0, "output": 0, "reasoning": 0}
        )
        self.assertIn("cache", usage)
        self.assertEqual(len(content["assistant_steps"]), 1)
        self.assertEqual(content["assistant_steps"][0]["actual_model"], content["actual_model"])

        # (7) Exact session/admission correlation (durable, real server).
        intent = SessionIntentRepository(inspect).get_intent(scenario["dispatch"].dispatch_id)
        self.assertIsNotNone(intent)
        self.assertEqual(intent.admission_id, receipt["admission_id"])
        self.assertEqual(intent.session_id, receipt["session_id"])
        messages, truncated = _client(self.manager).collect_messages(receipt["session_id"])
        self.assertFalse(truncated)
        user_ids = [m.get("id") for m in messages if m.get("type") == "user"]
        self.assertEqual(user_ids, [receipt["admission_id"]])
        assistant = [m for m in messages if m.get("type") == "assistant"]
        self.assertTrue(assistant)
        # Reconciliation came from the durable assistant message, not an event
        # stream: the accepted text is exactly the durable assistant content.
        self.assertEqual(content["text"], assistant[0]["content"][0]["text"])

        # (8) A repeated reconcile is a no-op: no duplicate proposal/result/cost,
        #     and the model transport was never re-asked (no re-send/retry).
        delayed_before = len(_delayed_requests(self.fixture))
        again = session_runner.reconcile_session_turn(
            inspect,
            registry=self.registry,
            activation_token=token,
            dispatch_id=scenario["dispatch"].dispatch_id,
            manager=self.manager,
        )
        self.assertEqual(again.state, "completed")
        self.assertTrue(again.duplicate)
        self.assertEqual(again.proposal_record_id, proposal_record_id)
        self.assertEqual(len(RuntimeRepository(inspect).list_proposals()), 1)
        dispatch_after = RuntimeDispatchRepository(inspect).get_dispatch(scenario["dispatch"].dispatch_id)
        self.assertEqual(dispatch_after.cost_amount_usd, dispatch.cost_amount_usd)
        self.assertEqual(dispatch_after.result_ref, proposal_record_id)
        self.assertEqual(len(_delayed_requests(self.fixture)), delayed_before)

    def test_fresh_critic_different_model_and_invalid_same_model_refused_before_prompt(self) -> None:
        info, store, token = self._project("critic-proj")
        text = f"r3-critic-{uuid.uuid4().hex[:8]} DELAY_MS=4000"
        scenario = _scenario(
            store,
            info.project_id,
            role="section_critic",
            request_id="req-c",
            call_id="call-c",
            text=text,
            prompt_version="critic.v1",
        )
        before = len(self.fixture.requests())
        receipt = session_runner.submit_session_turn(
            store,
            registry=self.registry,
            activation_token=token,
            task=scenario["task"],
            dispatch=scenario["dispatch"],
            manager=self.manager,
            text=text,
            versions=scenario["versions"],
        )
        self.assertEqual(receipt.state, "admitted")
        self.assertTrue(receipt.session_id.startswith("ses"))

        client = _client(self.manager)
        session = client.get_session(receipt.session_id)
        self.assertIsNone(session.get("parentID"))  # fresh, never a writer fork
        client.verify_session_pins(receipt.session_id, agent="section_critic", model=CRITIC_MODEL)
        # The critic started from an EMPTY history: while the delayed turn is live
        # the only projected message is this turn's own admission.
        messages, truncated = client.collect_messages(receipt.session_id)
        self.assertFalse(truncated)
        self.assertEqual([m.get("id") for m in messages if m.get("type") == "user"], [receipt.admission_id])
        self.assertEqual([m for m in messages if m.get("type") == "assistant"], [])

        # An invalid (same concrete model) critic configuration is refused BEFORE
        # any session or prompt: no new request reaches the model transport.
        bad = _configuration(writer_model=WRITER_MODEL_ID, critic_model=WRITER_MODEL_ID)
        bad_scenario = _scenario(
            store,
            info.project_id,
            role="section_critic",
            configuration=bad,
            request_id="req-bad",
            call_id="call-bad",
            text=f"r3-bad-critic-{uuid.uuid4().hex[:8]}",
            prompt_version="critic.v1",
        )
        bad_manager = OpenCodeServiceManager(
            data_dir=str(self.data_dir),
            configuration=bad,
            binary=OPENCODE_BIN,
            fixture_provider=_fixture_provider(self.fixture),
            start_timeout=60.0,
        )
        after_critic = len(self.fixture.requests())
        with self.assertRaises(session_runner.SessionRunnerValidationError):
            session_runner.submit_session_turn(
                store,
                registry=self.registry,
                activation_token=token,
                task=bad_scenario["task"],
                dispatch=bad_scenario["dispatch"],
                manager=bad_manager,
                text=bad_scenario["text"],
                versions=bad_scenario["versions"],
            )
        self.assertEqual(len(self.fixture.requests()), after_critic, "a refused critic reached the model")
        self.assertGreaterEqual(after_critic, before)
        bad_dispatch = RuntimeDispatchRepository(store).get_dispatch(bad_scenario["dispatch"].dispatch_id)
        self.assertEqual(bad_dispatch.state, "reserved")


# --------------------------------------------------------------------------- #
# Scenario 3: two saved registered projects cannot cross; safe switch blocked
# --------------------------------------------------------------------------- #


@unittest.skipUnless(HAVE_REAL, "opencode v2.2.0 is not installed")
class TwoProjectIsolationTests(unittest.TestCase):
    """Two registered projects; no cross session/model/mapping/token; switch blocked."""

    def setUp(self) -> None:
        os.makedirs(SCRATCH_BASE, exist_ok=True)
        self._cleanup_state: dict = {"manager": None, "fixture": None, "scratch": None, "leaked": False}
        self.addCleanup(_teardown_owned_service, self._cleanup_state)
        self.scratch = Path(tempfile.mkdtemp(dir=SCRATCH_BASE, prefix="raa-runner-isolation-"))
        self._cleanup_state["scratch"] = self.scratch
        self.fixture = _FixtureModel(self.scratch, canary=self.scratch / "fixture-canary.txt")
        self._cleanup_state["fixture"] = self.fixture
        self.data_dir = self.scratch / "data"
        self.data_dir.mkdir(mode=0o700)
        self.configuration = _configuration()
        self.manager = OpenCodeServiceManager(
            data_dir=str(self.data_dir),
            configuration=self.configuration,
            binary=OPENCODE_BIN,
            fixture_provider=_fixture_provider(self.fixture),
            start_timeout=60.0,
        )
        self._cleanup_state["manager"] = self.manager
        self.manager.start()
        self.registry = Registry(str(self.scratch / "state"))

    def _project(self, name: str):
        info = self.registry.create_project(str(self.scratch / name), name=name)
        self.manager.location_for(info.project_id)
        store = _open_store(info)
        self.addCleanup(store.close)
        _seed_artifacts(store, info.project_id)
        return info, store

    def test_cross_project_session_model_mapping_token_and_safe_switch(self) -> None:
        before = [_fingerprint(path) for path in ORDINARY_REGISTRATIONS]
        info_a, store_a = self._project("proj-a")
        info_b, store_b = self._project("proj-b")
        token_a = self.registry.activate_execution(info_a.project_id).activation_token

        scenario_a = _scenario(store_a, info_a.project_id, text="r3-iso-a DELAY_MS=6000", request_id="req-a", call_id="call-a")
        receipt_a = session_runner.submit_session_turn(
            store_a,
            registry=self.registry,
            activation_token=token_a,
            task=scenario_a["task"],
            dispatch=scenario_a["dispatch"],
            manager=self.manager,
            text=scenario_a["text"],
            versions=scenario_a["versions"],
        )
        self.assertEqual(receipt_a.state, "admitted")
        mapping_a = RuntimeRepository(store_a).get_session_mapping(
            SessionIntentRepository(store_a).get_intent(scenario_a["dispatch"].dispatch_id).mapping_id
        )
        self.assertEqual(mapping_a.session_id, receipt_a.session_id)

        # Project B has a reserved dispatch of its own and no cross contamination.
        scenario_b = _scenario(store_b, info_b.project_id, text="r3-iso-b", request_id="req-b", call_id="call-b")
        self.assertEqual(RuntimeRepository(store_b).list_session_mappings(), [])

        # (1) Project B's dispatch cannot be driven with project A's token.
        with self.assertRaises(Exception) as ctx:
            session_runner.submit_session_turn(
                store_b,
                registry=self.registry,
                activation_token=token_a,
                task=scenario_b["task"],
                dispatch=scenario_b["dispatch"],
                manager=self.manager,
                text=scenario_b["text"],
                versions=scenario_b["versions"],
            )
        self.assertEqual(type(ctx.exception).__name__, "ProjectConflict")
        # (2) Project A's dispatch cannot be driven with a foreign/absent token.
        with self.assertRaises(Exception) as ctx:
            session_runner.submit_session_turn(
                store_a,
                registry=self.registry,
                activation_token="act_" + "f" * 32,
                task=scenario_a["task"],
                dispatch=scenario_a["dispatch"],
                manager=self.manager,
                text=scenario_a["text"],
                versions=scenario_a["versions"],
            )
        self.assertEqual(type(ctx.exception).__name__, "ProjectConflict")
        # B still holds no mapping/session and its dispatch is still reserved.
        self.assertEqual(RuntimeRepository(store_b).list_session_mappings(), [])
        self.assertEqual(
            RuntimeDispatchRepository(store_b).get_dispatch(scenario_b["dispatch"].dispatch_id).state, "reserved"
        )

        # (3) The real WorkerEngine switch API blocks a safe switch while A's
        #     admitted dispatch is outstanding.
        engine = WorkerEngine(str(self.scratch / "state"), registry=self.registry)
        self.addCleanup(engine.close)
        switched = engine.handle({"version": PROTOCOL_VERSION, "op": "switch", "project_id": info_b.project_id})
        self.assertFalse(switched["ok"])
        self.assertEqual(switched["code"], "switch_blocked")
        self.assertEqual(self.registry.activation().active_project_id, info_a.project_id)

        # (4) The ordinary OpenCode service registrations are untouched.
        self.assertEqual(before, [_fingerprint(path) for path in ORDINARY_REGISTRATIONS])

        # Restore the registry activation (the switch attempt left A draining).
        self.registry.release_execution(token_a)


# --------------------------------------------------------------------------- #
# Scenario 4: owned restart mid-turn -> unknown, never a false completion
# --------------------------------------------------------------------------- #


@unittest.skipUnless(HAVE_REAL, "opencode v2.2.0 is not installed")
class RestartMidTurnRunnerTests(unittest.TestCase):
    """Restart the owned service mid-turn; the runner reconciles unknown, never completed."""

    def setUp(self) -> None:
        os.makedirs(SCRATCH_BASE, exist_ok=True)
        self._cleanup_state: dict = {"manager": None, "fixture": None, "scratch": None, "leaked": False}
        self.addCleanup(_teardown_owned_service, self._cleanup_state)
        self.scratch = Path(tempfile.mkdtemp(dir=SCRATCH_BASE, prefix="raa-runner-restart-"))
        self._cleanup_state["scratch"] = self.scratch
        self.fixture = _FixtureModel(self.scratch, canary=self.scratch / "fixture-canary.txt")
        self._cleanup_state["fixture"] = self.fixture
        self.data_dir = self.scratch / "data"
        self.data_dir.mkdir(mode=0o700)
        self.configuration = _configuration()
        self.manager = OpenCodeServiceManager(
            data_dir=str(self.data_dir),
            configuration=self.configuration,
            binary=OPENCODE_BIN,
            fixture_provider=_fixture_provider(self.fixture),
            start_timeout=60.0,
        )
        self._cleanup_state["manager"] = self.manager
        self.manager.start()
        self.registry = Registry(str(self.scratch / "state"))
        info = self.registry.create_project(str(self.scratch / "restart-proj"), name="Restart")
        self.info = info
        self.manager.location_for(info.project_id)
        self.store = _open_store(info)
        self.addCleanup(self.store.close)
        _seed_artifacts(self.store, info.project_id)
        self.token = self.registry.activate_execution(info.project_id).activation_token

    def test_restart_mid_turn_reconciles_unknown_and_blocks_new_dispatch(self) -> None:
        scenario = _scenario(
            self.store, self.info.project_id, text=f"r3-restart DELAY_MS={TURN_DELAY_MS}",
            request_id="req-r", call_id="call-r",
        )
        receipt = session_runner.submit_session_turn(
            self.store,
            registry=self.registry,
            activation_token=self.token,
            task=scenario["task"],
            dispatch=scenario["dispatch"],
            manager=self.manager,
            text=scenario["text"],
            versions=scenario["versions"],
        )
        self.assertEqual(receipt.state, "admitted")
        self.assertTrue(_wait_for_delayed_request(self.fixture), "the delayed turn never reached the transport")
        before = _client(self.manager).reconcile(receipt.session_id, receipt.admission_id)
        self.assertIn(before.state, ("queued", "in_progress"))
        self.assertFalse(before.is_complete)

        old_pid = self.manager.status().pid
        restarted = self.manager.restart()
        self.assertTrue(restarted.running)
        self.assertNotEqual(restarted.pid, old_pid)

        outcome = session_runner.reconcile_session_turn(
            self.store,
            registry=self.registry,
            activation_token=self.token,
            dispatch_id=scenario["dispatch"].dispatch_id,
            manager=self.manager,
        )
        # Never a false completion. The real service tore the turn down with an
        # errored assistant and no authoritative idle, so the truth is `unknown`
        # (an authoritative `failed` would be reported as `failed`, never success).
        self.assertNotEqual(outcome.state, "completed")
        self.assertIn(outcome.state, ("unknown", "failed", "interrupted"))
        self.assertIsNone(outcome.proposal_record_id)
        self.assertEqual(len(RuntimeRepository(self.store).list_proposals()), 0)

        # Mapping/correlation survive the restart.
        intent = SessionIntentRepository(self.store).get_intent(scenario["dispatch"].dispatch_id)
        self.assertIsNotNone(intent)
        self.assertEqual(intent.session_id, receipt.session_id)
        self.assertEqual(intent.admission_id, receipt.admission_id)
        mapping = RuntimeRepository(self.store).get_session_mapping(intent.mapping_id)
        self.assertEqual(mapping.session_id, receipt.session_id)

        # The reserve survives; no result was accepted.
        dispatch = RuntimeDispatchRepository(self.store).get_dispatch(scenario["dispatch"].dispatch_id)
        self.assertEqual(dispatch.reserve_usd, "0.10")
        self.assertIsNone(dispatch.result_ref)
        self.assertNotEqual(dispatch.state, "completed")

        if outcome.state == "unknown":
            self.assertTrue(outcome.reconciliation_required)
            # An unresolved outcome keeps the single serial remote slot held.
            second = _scenario(
                self.store, self.info.project_id, text="r3-restart-second",
                request_id="req-r2", call_id="call-r2",
            )
            with self.assertRaises(session_runner.SessionRunnerStateError):
                session_runner.submit_session_turn(
                    self.store,
                    registry=self.registry,
                    activation_token=self.token,
                    task=second["task"],
                    dispatch=second["dispatch"],
                    manager=self.manager,
                    text=second["text"],
                    versions=second["versions"],
                )
            self.assertEqual(
                RuntimeDispatchRepository(self.store).get_dispatch(second["dispatch"].dispatch_id).state,
                "reserved",
            )
        else:  # an authoritative failure is terminal and releases the slot
            self.assertFalse(outcome.reconciliation_required)


if __name__ == "__main__":
    unittest.main()
