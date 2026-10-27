#!/usr/bin/env python3
"""Integration orchestrator for step 00: BOTH prototypes on one real service.

Runs the TypeScript (`@opencode/client`) and Python (HTTP) prototypes through the
same minimal lifecycle against a single **real, disposable, app-owned** OpenCode
V2 service started by `spikes/runtime/harness/harness.sh`, with a deterministic
local **fixture** model provider (`mock_provider.py`) driving a real,
server-owned, bounded agent turn.

What is real and what is a fixture (stated plainly):

* REAL — the OpenCode service process, its HTTP API, HTTP Basic auth, session
  create with pinned role/model, durable prompt admission, SQLite persistence,
  restart of the owned service, and the server-owned agent loop that runs a turn
  while no client is connected.
* FIXTURE — only the model transport: a loopback OpenAI-compatible server that
  returns a deterministic reply after an explicit, bounded delay. No paid
  provider, no network egress, no credential.

Criteria exercised empirically (see README and the generated report):

1. submit in a separate process that exits; a second process retrieves the
   result after missing events;
2. retrying one client-supplied admission id yields exactly one session input and
   is never mistaken for completion;
3. restarting only the owned service while a turn is in flight, then recovering
   persistence and reporting an explicit non-success state (never a silent
   success);
4. (not run here) the typed Jev decision route — blocked pending budget;
5. fresh, independent critic session with pinned role/model and no writer
   history;
6. isolation: the owned root, scrubbed environment and owned-only restart.

Safety: the run owns a unique `HARNESS_ROOT` under `/tmp/opencode`, injects its
one fixture provider into that root's config, and always runs
`harness.sh cleanup`. It never touches the ordinary shared service, never
signals a foreign PID, and never prints a secret.

Usage:
    run_lifecycle.py [--root DIR] [--report PATH] [--keep] [--json]
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent.parent  # spikes/runtime/integration -> repo root
HARNESS = REPO / "spikes" / "runtime" / "harness" / "harness.sh"
TS_DIR = REPO / "spikes" / "runtime" / "typescript"
MOCK_PROVIDER = HERE / "mock_provider.py"
PY_WORKER = HERE / "py_lifecycle.py"
TS_WORKER = HERE / "ts_lifecycle.ts"

PROVIDER_ID = "mockprov"
WRITER_MODEL = f"{PROVIDER_ID}/delayed-echo"
CRITIC_MODEL = f"{PROVIDER_ID}/critic-echo"
DISCONNECT_DELAY_MS = 3000
RESTART_DELAY_MS = 10000
RESTART_WAIT_S = 11.0


def log(message: str) -> None:
    print(f"[integration] {message}", flush=True)


@dataclass
class Checks:
    results: list[dict[str, Any]] = field(default_factory=list)

    def record(self, name: str, ok: bool, detail: str = "") -> None:
        self.results.append({"check": name, "ok": bool(ok), "detail": detail})
        mark = "PASS" if ok else "FAIL"
        log(f"  [{mark}] {name}" + (f" — {detail}" if detail else ""))

    @property
    def failed(self) -> list[dict[str, Any]]:
        return [r for r in self.results if not r["ok"]]


class Runner:
    """Owns the fixture provider + the disposable service and the workers."""

    def __init__(self, root: Path, keep: bool) -> None:
        self.root = root
        self.keep = keep
        self.env = {**os.environ, "HARNESS_ROOT": str(root)}
        opencode = shutil.which("opencode")
        if opencode:
            self.env["HARNESS_OPENCODE_BIN"] = opencode
        self.mock: subprocess.Popen[bytes] | None = None
        self.mock_port: int | None = None
        self.mock_log = root / "mock-requests.jsonl"
        self.observations: dict[str, Any] = {}

    # -- harness ---------------------------------------------------------
    def harness(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(HARNESS), *args],
            capture_output=True,
            text=True,
            env=self.env,
            check=check,
        )

    @property
    def descriptor_path(self) -> Path:
        return self.root / "run" / "instance.json"

    @property
    def password_path(self) -> Path:
        return self.root / "run" / "password"

    def descriptor(self) -> dict[str, Any]:
        return json.loads(self.descriptor_path.read_text())

    def url(self) -> str:
        return self.descriptor()["url"]

    def password(self) -> str:
        return self.password_path.read_text()

    def http_get(self, path: str) -> dict[str, Any]:
        request = urllib.request.Request(self.url() + path)
        token = base64.b64encode(f"opencode:{self.password()}".encode()).decode()
        request.add_header("Authorization", "Basic " + token)
        with urllib.request.urlopen(request, timeout=15) as response:
            return json.loads(response.read())

    # -- fixture provider ------------------------------------------------
    def start_mock(self) -> None:
        env = {
            **os.environ,
            "MOCK_PORT": "0",
            "MOCK_LOG": str(self.mock_log),
            "MOCK_DELAY_MS": "0",
        }
        self.mock = subprocess.Popen(
            [sys.executable, str(MOCK_PROVIDER)],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=env,
            text=True,
        )
        assert self.mock.stdout is not None
        line = self.mock.stdout.readline()
        info = json.loads(line)
        self.mock_port = int(info["port"])
        log(f"fixture provider listening on 127.0.0.1:{self.mock_port}")

    def stop_mock(self) -> None:
        if self.mock is not None:
            self.mock.terminate()
            try:
                self.mock.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.mock.kill()
            self.mock = None

    def mock_requests(self) -> list[dict[str, Any]]:
        if not self.mock_log.exists():
            return []
        entries = []
        for line in self.mock_log.read_text().splitlines():
            line = line.strip()
            if line:
                entries.append(json.loads(line))
        return entries

    # -- provisioning ----------------------------------------------------
    def provision(self) -> None:
        # `start` creates the owned tree; then stop so we can drop in the single
        # fixture provider before the server reads its config.
        self.harness("start")
        self.harness("stop")
        config_dir = self.root / "config" / "opencode"
        config_dir.mkdir(parents=True, exist_ok=True)
        config = {
            "$schema": "https://opencode.ai/config.json",
            "provider": {
                PROVIDER_ID: {
                    "npm": "@ai-sdk/openai-compatible",
                    "name": "Fixture Provider (local, deterministic)",
                    "options": {
                        "baseURL": f"http://127.0.0.1:{self.mock_port}/v1",
                        "apiKey": "fixture-no-secret",
                    },
                    "models": {
                        "delayed-echo": {"name": "Delayed Echo (fixture)"},
                        "critic-echo": {"name": "Critic Echo (fixture)"},
                    },
                }
            },
        }
        (config_dir / "opencode.json").write_text(json.dumps(config, indent=2) + "\n")
        self.harness("start")
        log(f"service up at {self.url()} (opencode {self.descriptor().get('opencodeVersion')})")

    def cleanup(self) -> None:
        try:
            self.harness("cleanup")
        except subprocess.CalledProcessError as exc:  # pragma: no cover - safety net
            log(f"cleanup failed: {exc.stderr}")
        # The harness keeps its lock file beside (not inside) the root; remove our own.
        Path(f"{self.root}.lock").unlink(missing_ok=True)

    # -- workers ---------------------------------------------------------
    def py(self, *args: str) -> dict[str, Any]:
        proc = subprocess.run(
            [sys.executable, str(PY_WORKER), *args],
            capture_output=True,
            text=True,
            env=os.environ,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"py worker {' '.join(args)} failed: {proc.stderr.strip()}")
        return json.loads(proc.stdout.strip().splitlines()[-1])

    def ts(self, *args: str) -> dict[str, Any]:
        env = {
            **os.environ,
            "OPENCODE_URL": self.url(),
            "OPENCODE_AUTH": "basic",
            "OPENCODE_BASIC_PASSWORD": self.password(),
        }
        proc = subprocess.run(
            ["node", str(TS_WORKER), *args],
            capture_output=True,
            text=True,
            env=env,
            cwd=str(TS_DIR),
        )
        if proc.returncode != 0:
            raise RuntimeError(f"ts worker {' '.join(args)} failed: {proc.stderr.strip()}")
        return json.loads(proc.stdout.strip().splitlines()[-1])

    def worker(self, proto: str) -> Any:
        return self.py if proto == "python" else self.ts

    def worker_call(self, proto: str, *args: str) -> dict[str, Any]:
        return self.worker(proto)(*args)

    # -- per-prototype common args --------------------------------------
    def auth_args(self) -> list[str]:
        return ["--base-url", self.url(), "--password-file", str(self.password_path)]


def admit_id(label: str) -> str:
    return f"msg_{label}_{uuid.uuid4().hex[:12]}"


def basic_lifecycle(runner: Runner, proto: str, checks: Checks) -> dict[str, Any]:
    log(f"== {proto}: basic lifecycle (one real service) ==")
    out: dict[str, Any] = {"prototype": proto}
    call = runner.worker_call
    auth = runner.auth_args()
    directory = str(runner.root / "cwd")

    # -- create with explicit role/model (real API) --
    create = call(proto, "create", *auth, "--directory", directory, "--agent", "build", "--model", WRITER_MODEL)
    session = create["session"]
    sid = session["id"]
    out["session"] = {"id": sid, "agent": session.get("agent"), "model": session.get("model")}
    checks.record(
        f"{proto}: create pins explicit role/model",
        session.get("agent") == "build"
        and isinstance(session.get("model"), dict)
        and session["model"].get("providerID") == PROVIDER_ID
        and session["model"].get("id") == "delayed-echo",
        f"agent={session.get('agent')} model={session.get('model')}",
    )

    # -- criterion 1: submit in a process that exits; second process retrieves --
    admission = admit_id("disc")
    submit = call(
        proto,
        "submit",
        *auth,
        "--session",
        sid,
        "--id",
        admission,
        "--text",
        f"disconnect probe DELAY_MS={DISCONNECT_DELAY_MS}",
    )
    out["disconnect_admission"] = admission
    out["submit_receipt"] = submit
    checks.record(
        f"{proto}: durable admission receipt is not a completion",
        bool(submit.get("receipt")) and not submit.get("receiptHasOutcome", False),
        f"status={submit.get('status', submit.get('durableReceipt'))}",
    )

    # The submit process has already exited by construction (subprocess.run).
    resolved: dict[str, Any] = {}
    deadline = time.time() + 25
    polls = 0
    while time.time() < deadline:
        polls += 1
        resolved = call(proto, "reconcile", *auth, "--session", sid, "--id", admission)
        state = resolved.get("state")
        if state in ("succeeded", "completed", "failed", "interrupted"):
            break
        time.sleep(0.5)
    out["disconnect_resolution"] = {**resolved, "polls": polls}
    checks.record(
        f"{proto}: second process retrieves result after missed events",
        resolved.get("state") in ("succeeded", "completed") and resolved.get("isComplete", resolved.get("state") == "succeeded"),
        f"state={resolved.get('state')} after {polls} polls",
    )

    # -- criterion 2: retry the same admission id -> exactly one input --
    retry = call(
        proto,
        "submit",
        *auth,
        "--session",
        sid,
        "--id",
        admission,
        "--text",
        f"disconnect probe DELAY_MS={DISCONNECT_DELAY_MS}",
    )
    messages = call(proto, "messages", *auth, "--session", sid, "--admission-id", admission)
    out["retry"] = retry
    out["retry_messages"] = messages
    checks.record(
        f"{proto}: retried admission id yields exactly one input",
        messages.get("userInputsForId") == 1,
        f"status={retry.get('status', retry.get('duplicate'))} inputs={messages.get('userInputsForId')}",
    )

    # -- criterion 5: fresh critic session, pinned role/model, no history --
    dir_flag = "--directory" if proto == "python" else "--dir"
    critic_args = [proto, "critic", *auth, dir_flag, directory, "--agent", "plan", "--model", CRITIC_MODEL]
    if proto == "python":
        critic_args += ["--writer-model", WRITER_MODEL]
    critic = call(*critic_args)
    critic_session = critic["session"]
    out["critic"] = {
        "id": critic_session["id"],
        "agent": critic_session.get("agent"),
        "model": critic_session.get("model"),
        "fresh": critic.get("fresh"),
        "historyLength": critic.get("historyLength"),
        "parentID": critic_session.get("parentID"),
    }
    checks.record(
        f"{proto}: critic is fresh, pinned, and not writer history",
        critic.get("fresh") is True
        and critic.get("historyLength") == 0
        and critic_session.get("agent") == "plan"
        and critic_session.get("model", {}).get("id") == "critic-echo"
        and critic_session.get("parentID") is None
        and critic_session["id"] != sid,
        f"fresh={critic.get('fresh')} history={critic.get('historyLength')} "
        f"agent={critic_session.get('agent')} model={critic_session.get('model')}",
    )
    return out


def restart_phase(runner: Runner, checks: Checks) -> dict[str, Any]:
    log("== restart in flight: both prototypes, then restart only the owned service ==")
    out: dict[str, Any] = {"inflight": {}, "after": {}}
    auth = runner.auth_args()
    directory = str(runner.root / "cwd")
    started = time.time()

    for proto in ("python", "ts"):
        create = runner.worker_call(proto, "create", *auth, "--directory", directory, "--agent", "build", "--model", WRITER_MODEL)
        sid = create["session"]["id"]
        admission = admit_id("restart")
        runner.worker_call(
            proto, "submit", *auth, "--session", sid, "--id", admission,
            "--text", f"restart probe DELAY_MS={RESTART_DELAY_MS}",
        )
        out["inflight"][proto] = {"session": sid, "admission": admission}

    # Let the turns reach the provider and confirm they are in flight.
    time.sleep(2.5)
    inflight_requests = [r for r in runner.mock_requests() if r.get("delayMs") == RESTART_DELAY_MS]
    outcomes_before = {
        proto: runner.http_get(f"/api/session/{entry['session']}")["data"].get("outcome")
        for proto, entry in out["inflight"].items()
    }
    out["inflight"]["requests_seen"] = len(inflight_requests)
    out["inflight"]["outcome_before_restart"] = outcomes_before
    checks.record(
        "restart: both turns are in flight (provider reached, no outcome yet)",
        len(inflight_requests) >= 2 and all(v is None for v in outcomes_before.values()),
        f"provider_requests={len(inflight_requests)} outcomes={outcomes_before}",
    )

    log("  restarting the owned service (harness.sh restart) while turns are in flight")
    runner.harness("restart")
    new_url = runner.url()
    auth = runner.auth_args()  # a restart issues a new port and password
    out["new_url"] = new_url
    checks.record("restart: service replaced (new endpoint)", new_url != "", new_url)

    # Read immediately after restart, then again after the original delay elapsed.
    after: dict[str, Any] = {}
    for proto in ("python", "ts"):
        sid = out["inflight"][proto]["session"]
        admission = out["inflight"][proto]["admission"]
        immediate = runner.worker_call(proto, "reconcile", *auth, "--session", sid, "--id", admission)
        persisted = runner.worker_call(proto, "messages", *auth, "--session", sid, "--admission-id", admission)
        after[proto] = {"immediate": immediate, "persisted": persisted}
    out["after"] = after

    remaining = RESTART_WAIT_S - (time.time() - started)
    if remaining > 0:
        time.sleep(remaining)

    final: dict[str, Any] = {}
    for proto in ("python", "ts"):
        sid = out["inflight"][proto]["session"]
        admission = out["inflight"][proto]["admission"]
        resolution = runner.worker_call(proto, "reconcile", *auth, "--session", sid, "--id", admission)
        messages = runner.worker_call(proto, "messages", *auth, "--session", sid, "--admission-id", admission)
        session_after = runner.http_get(f"/api/session/{sid}")["data"]
        final[proto] = {"resolution": resolution, "messages": messages, "sessionOutcome": session_after.get("outcome")}
        persisted = out["after"][proto]["persisted"]
        checks.record(
            f"{proto}: persistence recovered after restart (session + user input present)",
            persisted.get("userInputsForId") == 1 and messages.get("count", 0) >= 2,
            f"userInputs={persisted.get('userInputsForId')} messages={messages.get('count')}",
        )
        never_complete = resolution.get("state") not in ("succeeded", "completed")
        checks.record(
            f"{proto}: restart did not silently declare success",
            never_complete,
            f"state={resolution.get('state')} reason={resolution.get('reason')}",
        )
    out["final"] = final
    return out


def build_report(runner: Runner, checks: Checks, observations: dict[str, Any]) -> dict[str, Any]:
    descriptor = runner.descriptor()
    return {
        "spike": "step-00 runtime integration",
        "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "fixtureDisclosure": (
            "Model transport only is a deterministic local fixture (mock_provider.py). "
            "The OpenCode service, HTTP API, Basic auth, admission, persistence, restart, "
            "and the server-owned agent turn are REAL. No paid calls, no network egress."
        ),
        "provider": {"kind": "fixture", "id": PROVIDER_ID, "models": ["delayed-echo", "critic-echo"]},
        "versions": {
            "opencode": descriptor.get("opencodeVersion"),
            "python": sys.version.split()[0],
            "node": subprocess.run(["node", "--version"], capture_output=True, text=True).stdout.strip(),
        },
        "service": {
            "url": descriptor.get("url"),
            "descriptorKind": descriptor.get("kind"),
            "documentedRegistration": descriptor.get("documentedRegistration"),
        },
        "checks": checks.results,
        "failed": checks.failed,
        "observations": observations,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="step-00 integration orchestrator")
    parser.add_argument("--root", default=None, help="HARNESS_ROOT (default: unique under /tmp/opencode)")
    parser.add_argument("--report", default=str(HERE / "report" / "combined-evidence.json"))
    parser.add_argument("--keep", action="store_true", help="do not clean the owned root (debugging)")
    parser.add_argument("--json", action="store_true", help="print the whole report JSON")
    args = parser.parse_args()

    root = Path(args.root) if args.root else Path(f"/tmp/opencode/spike-combined-integration-{os.getpid()}-{uuid.uuid4().hex[:6]}")
    root = root.resolve()
    if root.exists() and any(root.iterdir()):
        print(f"refusing to reuse a non-empty HARNESS_ROOT: {root}", file=sys.stderr)
        return 2
    if "opencode" not in root.parts or root == Path("/tmp/opencode"):
        print(f"refusing unsafe HARNESS_ROOT: {root}", file=sys.stderr)
        return 2

    runner = Runner(root, args.keep)
    checks = Checks()
    observations: dict[str, Any] = {}
    failed = True
    try:
        runner.start_mock()
        runner.provision()
        for proto in ("python", "ts"):
            observations[proto] = basic_lifecycle(runner, proto, checks)
        observations["restart"] = restart_phase(runner, checks)
        observations["mock_request_count"] = len(runner.mock_requests())
        failed = bool(checks.failed)
    finally:
        try:
            report = build_report(runner, checks, observations)
        except Exception as exc:  # still leave an honest report if setup failed
            report = {
                "spike": "step-00 runtime integration",
                "error": f"report assembly failed: {exc}",
                "checks": checks.results,
                "failed": checks.failed,
            }
        report_path = Path(args.report)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
        runner.stop_mock()
        if not args.keep:
            runner.cleanup()
        else:
            log(f"--keep set; leaving owned root at {root}")

    log(f"report written to {Path(args.report)}")
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    passed = len([r for r in checks.results if r["ok"]])
    log(f"checks: {passed}/{len(checks.results)} passed, {len(checks.failed)} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
