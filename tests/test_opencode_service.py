"""Acceptance tests for the app-owned OpenCode V2 service manager (task T4).

Covers ``docs/implementation/04-agent-runtime.md`` deliverable 1 (app-owned,
version-pinned OpenCode service integration) and acceptance line 23 ("Project/
session/model isolation holds across two saved projects; ordinary OpenCode
service is untouched"), ``docs/specification.md`` lines 14-16 and 62, ADR
``001``, and the retained step-00 harness lifecycle-safety findings.

The suite runs a **real**, disposable, app-owned ``opencode`` v2.2.0 process
under ``/tmp/opencode`` and asserts:

* start / status / stop / restart of an *owned* real process, loopback + HTTP
  Basic auth verified (401 unauthenticated), pinned ``2.2.0`` reported by both
  the binary and ``GET /api/info``;
* the child environment is scrubbed of every inherited ``OPENCODE_*`` /
  ``OPENCHAMBER_*`` / ``AI_AGENT`` variable and every provider secret, while an
  explicitly supplied, **known** provider credential name is injected and never
  persisted to the config or the ledger;
* the merged config denies **all** providers and allows only the configured role
  providers, denies all tools/integrations, disables update/warming/automatic
  compaction, and reads **only** the manager's own config source;
* two clean, owned, symlink-free project locations; and a restart that preserves
  sessions (real HTTP session create/read, ``cost == 0``);
* refusals that never signal or harm a foreign process: an unsafe/unowned root,
  a symlinked root, an inherited ancestor config, a foreign PID record, a stale
  PID, a wrong binary/server version, and a conflicting concurrent config.

**No model, provider, or paid call is made anywhere**, and no source text is
uploaded: sessions are created with an empty workspace and never prompted. Real
OpenCode tests skip when the pinned binary is absent; they run here.

Static/source-of-truth reminders live in the module docstring — this file is
evidence, not a second specification.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from review_article_agent.runtime import roles, service  # noqa: E402
from review_article_agent.runtime.roles import RoleConfiguration  # noqa: E402
from review_article_agent.runtime.service import (  # noqa: E402
    LoopbackFixtureProvider,
    OpenCodeServiceManager,
    ServiceConfigError,
    ServiceOwnershipError,
    ServiceProcessError,
    ServiceVersionError,
    build_service_config,
    service_config_bytes,
)

#: Synthetic canary that must never leak into a config, ledger, or message.
CANARY = "RAA_SYNTHETIC_SECRET_CANARY"

#: Scratch base requested by the task (an approved external location).
SCRATCH_BASE = "/tmp/opencode"


def _binary_version(binary: str) -> str:
    try:
        proc = subprocess.run(
            [binary, "--version"], capture_output=True, text=True, timeout=20, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    tokens = (proc.stdout or "").split()
    raw = tokens[-1] if tokens else ""
    return raw[1:] if raw[:1] in {"v", "V"} else raw


OPENCODE_BIN = shutil.which("opencode")
HAVE_REAL = bool(OPENCODE_BIN) and _binary_version(OPENCODE_BIN or "") == service.OPENCODE_PINNED_VERSION


def _role_config(
    writer: tuple[str, str] = ("openrouter", "deepseek/deepseek-v4.1-flash"),
    critic: tuple[str, str] = ("anthropic", "claude-sonnet-5-5"),
) -> RoleConfiguration:
    return RoleConfiguration.from_mapping(
        {
            "schema_version": 1,
            "roles": {
                "writer": {"provider_id": writer[0], "model_id": writer[1]},
                "section_critic": {"provider_id": critic[0], "model_id": critic[1]},
            },
        }
    )


def _auth_header(password: str) -> str:
    token = base64.b64encode(f"opencode:{password}".encode("utf-8")).decode("ascii")
    return "Basic " + token


def _http(method: str, url: str, path: str, password: str, body: object = None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(url.rstrip("/") + path, data=data, method=method)
    request.add_header("Authorization", _auth_header(password))
    if data is not None:
        request.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(request, timeout=15) as response:  # noqa: S310
        return response.status, json.loads(response.read().decode("utf-8"))


def _http_status(url: str, path: str, password: str | None = None) -> int:
    request = urllib.request.Request(url.rstrip("/") + path)
    if password is not None:
        request.add_header("Authorization", _auth_header(password))
    try:
        with urllib.request.urlopen(request, timeout=15) as response:  # noqa: S310
            return response.status
    except urllib.error.HTTPError as exc:
        code = exc.code
        exc.close()
        return code


def _proc_environ(pid: int) -> dict[str, str]:
    with open(f"/proc/{pid}/environ", "rb") as handle:
        raw = handle.read()
    out: dict[str, str] = {}
    for entry in raw.split(b"\x00"):
        if entry:
            name, _, value = entry.partition(b"=")
            out[name.decode("utf-8", "replace")] = value.decode("utf-8", "replace")
    return out


def _proc_cmdline(pid: int) -> list[str]:
    with open(f"/proc/{pid}/cmdline", "rb") as handle:
        raw = handle.read()
    return [part.decode("utf-8", "replace") for part in raw.split(b"\x00") if part]


def _pid_alive(pid: int) -> bool:
    try:
        with open(f"/proc/{pid}/stat", "rb") as handle:
            raw = handle.read().decode("utf-8", "replace")
    except OSError:
        return False
    fields = raw[raw.rfind(")") + 1 :].split()
    return bool(fields) and fields[0] != "Z"


def _file_fingerprint(path: Path):
    if not path.exists():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


# --------------------------------------------------------------------------- #
# Shared base: one scratch data dir, tracked managers, safe teardown
# --------------------------------------------------------------------------- #


class _ServiceCase(unittest.TestCase):
    def setUp(self) -> None:
        os.makedirs(SCRATCH_BASE, exist_ok=True)
        self.data_dir = Path(tempfile.mkdtemp(dir=SCRATCH_BASE, prefix="raa-service-test-"))
        self._managers: list[OpenCodeServiceManager] = []

    def tearDown(self) -> None:
        for manager in self._managers:
            try:
                if manager.is_owned_root():
                    manager.stop()
            except Exception:  # teardown must never mask a test result
                pass
        shutil.rmtree(self.data_dir, ignore_errors=True)

    def manager(self, **kwargs) -> OpenCodeServiceManager:
        kwargs.setdefault("binary", OPENCODE_BIN)
        kwargs.setdefault("configuration", _role_config())
        manager = OpenCodeServiceManager(data_dir=kwargs.pop("data_dir", self.data_dir), **kwargs)
        self._managers.append(manager)
        return manager


# --------------------------------------------------------------------------- #
# Pure configuration / fixture provider (no process)
# --------------------------------------------------------------------------- #


class PureConfigTests(unittest.TestCase):
    def test_provider_policies_deny_all_then_allow_configured_roles(self) -> None:
        config = build_service_config(_role_config())
        policies = config["policies"]
        self.assertEqual(policies[0], {"action": "provider.use", "resource": "*", "effect": "deny"})
        allows = [
            p for p in policies if p["action"] == "provider.use" and p["effect"] == "allow"
        ]
        self.assertEqual({p["resource"] for p in allows}, {"openrouter", "anthropic"})
        # The deny-all must precede every allow.
        deny_index = policies.index(policies[0])
        self.assertLess(deny_index, policies.index(allows[0]))
        self.assertIn({"action": "tool.use", "resource": "*", "effect": "deny"}, policies)
        self.assertIn({"action": "integration.use", "resource": "mcp:*", "effect": "deny"}, policies)

    def test_verified_v2_disable_fields_and_no_default_model(self) -> None:
        config = build_service_config(_role_config())
        self.assertEqual(config["update"], "disable")
        self.assertEqual(config["warming"], False)
        self.assertEqual(config["compaction"], {"auto": False})
        self.assertEqual(config["$schema"], roles.OPENCODE_CONFIG_SCHEMA_URL)
        # No top-level model/default_agent: the model is per-role and explicit.
        self.assertNotIn("model", config)
        self.assertNotIn("default_agent", config)
        for v1_key in roles.OPENCODE_DEPRECATED_V1_KEYS:
            self.assertNotIn(v1_key, config)
        # Deterministic bytes.
        self.assertEqual(service_config_bytes(_role_config()), service_config_bytes(_role_config()))

    def test_fixture_provider_is_fixed_loopback_only(self) -> None:
        fixture = LoopbackFixtureProvider(
            provider_id="fxprov", base_url="http://127.0.0.1:59321/v1", models=("m1", "m2")
        )
        config = build_service_config(_role_config(), fixture_provider=fixture)
        self.assertEqual(config["providers"]["fxprov"]["package"], service.FIXTURE_PROVIDER_PACKAGE)
        self.assertEqual(
            service.FIXTURE_PROVIDER_PACKAGE, "@opencode/ai/providers/openai-compatible"
        )
        self.assertEqual(
            config["providers"]["fxprov"]["settings"]["baseURL"], "http://127.0.0.1:59321/v1"
        )
        self.assertIn(
            {"action": "provider.use", "resource": "fxprov", "effect": "allow"},
            config["policies"],
        )
        refused = [
            ("fx", "https://evil.example/v1", ("m",)),  # remote host
            ("fx", "http://127.0.0.1:59321/v2", ("m",)),  # wrong path
            ("openrouter", "http://127.0.0.1:59321/v1", ("m",)),  # shadows a catalog provider
            ("fx", "http://127.0.0.1/v1", ("m",)),  # no port
            ("FX", "http://127.0.0.1:59321/v1", ("m",)),  # unsafe id
            ("fx", "http://127.0.0.1:59321/v1", ()),  # no models
        ]
        for provider_id, base_url, models in refused:
            with self.assertRaises(ServiceConfigError, msg=(provider_id, base_url)):
                LoopbackFixtureProvider(provider_id=provider_id, base_url=base_url, models=models)

    def test_provider_env_allowlist_is_enforced(self) -> None:
        base = Path(tempfile.mkdtemp(dir=SCRATCH_BASE, prefix="raa-service-env-"))
        try:
            with self.assertRaises(ServiceConfigError):
                OpenCodeServiceManager(
                    data_dir=base, configuration=_role_config(), provider_env={"EVIL_VAR": "x"}
                )
            with self.assertRaises(ServiceConfigError):
                OpenCodeServiceManager(
                    data_dir=base,
                    configuration=_role_config(),
                    provider_env={"OPENROUTER_API_KEY": ""},
                )
            with self.assertRaises(ServiceConfigError):
                OpenCodeServiceManager(
                    data_dir=base, configuration=_role_config(), provider_env={"lowercase": "x"}
                )
            # An explicit known name is accepted.
            ok = OpenCodeServiceManager(
                data_dir=base,
                configuration=_role_config(),
                provider_env={"OPENROUTER_API_KEY": "value"},
            )
            self.assertEqual(ok.provider_env, {"OPENROUTER_API_KEY": "value"})
        finally:
            shutil.rmtree(base, ignore_errors=True)

    def test_wrong_binary_version_fails_closed(self) -> None:
        base = Path(tempfile.mkdtemp(dir=SCRATCH_BASE, prefix="raa-service-bin-"))
        try:
            manager = OpenCodeServiceManager(
                data_dir=base, configuration=_role_config(), binary="/bin/true"
            )
            with self.assertRaises(ServiceVersionError):
                manager.start()
        finally:
            shutil.rmtree(base, ignore_errors=True)

    def test_bad_data_dir_requires_absolute(self) -> None:
        with self.assertRaises(ServiceConfigError):
            OpenCodeServiceManager(data_dir="relative/path", configuration=_role_config())


# --------------------------------------------------------------------------- #
# Refusals that need no running server
# --------------------------------------------------------------------------- #


class RefusalTests(_ServiceCase):
    def test_symlink_root_refused_and_target_untouched(self) -> None:
        target = self.data_dir / "real-target"
        target.mkdir()
        os.symlink(target, self.data_dir / service.SERVICE_ROOT_DIRNAME)
        manager = self.manager()
        with self.assertRaises(ServiceOwnershipError):
            manager.start()
        with self.assertRaises(ServiceOwnershipError):
            manager.location_for("prj_one")
        self.assertEqual(list(target.iterdir()), [])

    def test_unowned_nonempty_root_refused(self) -> None:
        root = self.data_dir / service.SERVICE_ROOT_DIRNAME
        root.mkdir()
        (root / "sentinel").write_text("keep me", encoding="utf-8")
        manager = self.manager()
        with self.assertRaises(ServiceOwnershipError):
            manager.start()
        self.assertTrue((root / "sentinel").is_file())
        self.assertFalse(manager.marker.exists())

    def test_inherited_ancestor_config_refused(self) -> None:
        outer = self.data_dir / "outer"
        inner = outer / "inner"
        inner.mkdir(parents=True)
        (outer / "opencode.json").write_text("{}", encoding="utf-8")
        manager = self.manager(data_dir=inner)
        with self.assertRaises(ServiceOwnershipError):
            manager.start()
        self.assertFalse((inner / service.SERVICE_ROOT_DIRNAME).exists())

    def test_stale_pid_record_cleared_without_signal(self) -> None:
        manager = self.manager()
        manager.location_for("prj_stale")  # creates + marks the owned root, no server
        dead_pid = 2_000_000_000  # far above any real pid space on this host
        manager.pid_record.write_text(
            json.dumps(
                {
                    "pid": dead_pid,
                    "starttime": "1",
                    "exe": "/nonexistent",
                    "cwd": "/nonexistent",
                    "home": "/nonexistent",
                    "passwordSha256": "0" * 64,
                }
            ),
            encoding="utf-8",
        )
        status = manager.stop()
        self.assertEqual(status.state, "stopped")
        self.assertFalse(manager.pid_record.exists())


# --------------------------------------------------------------------------- #
# Real, owned process (skipped only when the pinned binary is absent)
# --------------------------------------------------------------------------- #


@unittest.skipUnless(HAVE_REAL, "opencode v2.2.0 is not installed")
class RealServiceTests(_ServiceCase):
    def test_start_auth_version_config_and_stop(self) -> None:
        manager = self.manager()
        started = manager.start()
        self.assertTrue(started.running)
        self.assertEqual(started.state, "running")
        self.assertEqual(started.version, "2.2.0")
        self.assertEqual(started.url, f"http://127.0.0.1:{int(manager.port_file.read_text())}")

        # Unauthenticated access is refused; authenticated /api/info is pinned.
        self.assertEqual(_http_status(started.url, "/api/info"), 401)
        code, info = _http("GET", started.url, "/api/info", manager.read_password())
        self.assertEqual(code, 200)
        self.assertEqual(info["version"], "2.2.0")
        self.assertEqual(info["pid"], started.pid)

        # The merged config denies all providers, allows only role providers,
        # denies tools/integrations, and disables update/warming/compaction.
        sources = manager.config_sources()
        self.assertTrue(sources)
        for source in sources:
            self.assertTrue(
                str(source["path"]).startswith(str(manager.root)),
                f"config source outside the owned root: {source!r}",
            )
        document = next(s for s in sources if s.get("type") == "document")
        merged = document["info"]
        policies = merged["policies"]
        self.assertIn({"action": "provider.use", "resource": "*", "effect": "deny"}, policies)
        allows = {
            p["resource"]
            for p in policies
            if p["action"] == "provider.use" and p["effect"] == "allow"
        }
        self.assertEqual(allows, {"openrouter", "anthropic"})
        self.assertIn({"action": "tool.use", "resource": "*", "effect": "deny"}, policies)
        self.assertEqual(merged.get("update"), "disable")
        self.assertEqual(merged.get("warming"), False)
        self.assertEqual(merged.get("compaction"), {"auto": False})
        self.assertNotIn("model", merged)
        self.assertNotIn("default_agent", merged)

        # File modes are owner-only.
        self.assertEqual(os.stat(manager.secret_file).st_mode & 0o777, 0o600)
        self.assertEqual(os.stat(manager.marker).st_mode & 0o777, 0o600)
        for directory in (
            manager.home,
            manager.config_home,
            manager.data_home,
            manager.state_home,
            manager.cache_home,
            manager.cwd,
            manager.run,
        ):
            self.assertEqual(os.stat(directory).st_mode & 0o777, 0o700, directory)

        self.assertEqual(manager.status().state, "running")
        stopped = manager.stop()
        self.assertEqual(stopped.state, "stopped")
        self.assertFalse(manager.status().running)
        self.assertFalse(manager.secret_file.exists())
        # Ordinary data survives a stop; only run records are cleared.
        self.assertTrue(manager.root.is_dir())
        self.assertTrue(manager.home.is_dir())

    def test_child_argv_is_foreground_serve_only(self) -> None:
        manager = self.manager()
        started = manager.start()
        argv = _proc_cmdline(started.pid)
        port = int(manager.port_file.read_text())
        self.assertEqual(
            argv[1:], ["serve", "--hostname", "127.0.0.1", "--port", str(port)]
        )
        self.assertNotIn("--service", argv)
        self.assertNotIn("service", argv)

    def test_env_scrub_and_credential_non_persistence(self) -> None:
        leaked = {
            "OPENCODE_CONFIG": CANARY,
            "OPENCODE_SERVER_PASSWORD": CANARY,
            "OPENCODE_BINARY": CANARY,
            "OPENCHAMBER_UI_PASSWORD": CANARY,
            "AI_AGENT": CANARY,
        }
        saved = {k: os.environ.get(k) for k in leaked}
        os.environ.update(leaked)
        try:
            manager = self.manager(provider_env={"OPENROUTER_API_KEY": CANARY})
            started = manager.start()
            env = _proc_environ(started.pid)
            for name in ("OPENCODE_CONFIG", "OPENCODE_SERVER_PASSWORD", "OPENCODE_BINARY", "AI_AGENT"):
                self.assertNotIn(name, env)
            self.assertEqual([k for k in env if k.startswith("OPENCHAMBER")], [])
            self.assertEqual(env.get("HOME"), str(manager.home))
            self.assertEqual(env.get("OPENCODE_PASSWORD"), manager.read_password())
            self.assertNotEqual(env.get("OPENCODE_PASSWORD"), CANARY)
            # Only the explicitly supplied known credential name is injected.
            self.assertEqual(env.get("OPENROUTER_API_KEY"), CANARY)
            # The credential value is never persisted; only its name is recorded.
            self.assertNotIn(CANARY, manager.config_file.read_text(encoding="utf-8"))
            self.assertNotIn(CANARY, manager.descriptor_file.read_text(encoding="utf-8"))
            self.assertIn("OPENROUTER_API_KEY", manager.descriptor_file.read_text(encoding="utf-8"))
        finally:
            for name, value in saved.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value

    def test_secret_never_in_repr_config_or_ledger(self) -> None:
        manager = self.manager(provider_env={"OPENROUTER_API_KEY": CANARY})
        started = manager.start()
        password = manager.read_password()
        self.assertNotIn(password, manager.config_file.read_text(encoding="utf-8"))
        self.assertNotIn(password, manager.pid_record.read_text(encoding="utf-8"))
        self.assertNotIn(password, manager.descriptor_file.read_text(encoding="utf-8"))
        for text in (
            repr(manager),
            repr(manager.descriptor()),
            repr(manager.connection()),
            repr(started),
            str(manager.connection()),
        ):
            self.assertNotIn(password, text)
            self.assertNotIn(CANARY, text)

    def test_two_clean_project_locations(self) -> None:
        manager = self.manager()
        manager.start()
        first = manager.location_for("prj_alpha")
        second = manager.location_for("prj_beta")
        self.assertNotEqual(first.directory, second.directory)
        for location in (first, second):
            path = Path(location.directory)
            self.assertTrue(path.is_dir())
            self.assertFalse(path.is_symlink())
            self.assertTrue(path.is_relative_to(manager.root))
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o700)
            self.assertTrue((path / ".location-owned.json").is_file())
        # Idempotent for the same project.
        self.assertEqual(first.location_id, manager.location_for("prj_alpha").location_id)
        # Traversal / non-opaque ids are refused.
        for bad in ("../escape", "a/b", "", ".hidden", "a\\b", ".."):
            with self.assertRaises(ValueError):
                manager.location_for(bad)

    def test_restart_preserves_sessions(self) -> None:
        manager = self.manager()
        started = manager.start()
        location = manager.location_for("prj_persist")
        _, created = _http(
            "POST",
            started.url,
            "/api/session",
            manager.read_password(),
            {"location": {"directory": location.directory}},
        )
        session_id = created["data"]["id"]
        self.assertEqual(created["data"]["cost"], 0)
        # The application project id is not OpenCode's own project id.
        self.assertNotEqual(created["data"].get("projectID"), "prj_persist")

        restarted = manager.restart()
        self.assertTrue(restarted.running)
        self.assertNotEqual(restarted.pid, started.pid)
        _, listing = _http("GET", restarted.url, "/api/session", manager.read_password())
        ids = [entry["id"] for entry in listing["data"]]
        self.assertIn(session_id, ids)

    def test_concurrent_start_idempotent_and_conflicting_config_refused(self) -> None:
        manager = self.manager()
        results: list[tuple[str, object]] = []

        def go() -> None:
            try:
                results.append(("ok", manager.start()))
            except Exception as exc:  # noqa: BLE001 - recorded for assertion
                results.append(("err", exc))

        threads = [threading.Thread(target=go) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=90)
        oks = [value for kind, value in results if kind == "ok"]
        self.assertEqual(len(oks), 2, results)
        self.assertEqual(len({status.pid for status in oks}), 1)
        self.assertTrue(manager.status().running)

        # A second manager instance with the same config is idempotent.
        same = self.manager()
        self.assertEqual(same.start().pid, oks[0].pid)

        # A different config while an instance runs is refused, unharmed.
        conflicting = self.manager(
            configuration=_role_config(critic=("anthropic", "claude-different-5-5"))
        )
        with self.assertRaises(ServiceConfigError):
            conflicting.start()
        self.assertTrue(manager.status().running)

    def test_foreign_pid_refused_without_harm(self) -> None:
        manager = self.manager()
        started = manager.start()
        record = json.loads(manager.pid_record.read_text(encoding="utf-8"))
        original_starttime = record["starttime"]
        record["starttime"] = str(int(original_starttime) + 424242)  # simulate PID reuse
        manager.pid_record.write_text(json.dumps(record), encoding="utf-8")
        self.assertEqual(manager.status().state, "foreign")
        with self.assertRaises(ServiceProcessError):
            manager.stop()
        # The real process was never signalled.
        self.assertTrue(_pid_alive(started.pid))
        # Restore the truthful record and stop cleanly.
        record["starttime"] = original_starttime
        manager.pid_record.write_text(json.dumps(record), encoding="utf-8")
        self.assertEqual(manager.stop().state, "stopped")

    def test_wrong_server_version_fails_closed(self) -> None:
        def fake_http(url, username, password, timeout, path="/api/info"):
            return {"version": "0.0.1", "pid": 1}

        manager = self.manager(http_get=fake_http)
        with self.assertRaises(ServiceVersionError):
            manager.start()
        self.assertFalse(manager.pid_record.exists())
        self.assertFalse(manager.secret_file.exists())
        self.assertFalse(manager.status().running)

    def test_fixture_provider_config_loads_in_real_server(self) -> None:
        fixture = LoopbackFixtureProvider(
            provider_id="fxprov",
            base_url="http://127.0.0.1:59321/v1",
            models=("fixture-echo", "critic-echo"),
        )
        manager = self.manager(
            configuration=_role_config(
                writer=("fxprov", "fixture-echo"), critic=("fxprov", "critic-echo")
            ),
            fixture_provider=fixture,
        )
        started = manager.start()
        self.assertTrue(started.running)
        document = next(s for s in manager.config_sources() if s.get("type") == "document")
        providers = document["info"]["providers"]
        self.assertEqual(list(providers), ["fxprov"])
        self.assertEqual(providers["fxprov"]["package"], service.FIXTURE_PROVIDER_PACKAGE)
        self.assertEqual(providers["fxprov"]["settings"]["baseURL"], "http://127.0.0.1:59321/v1")

    def test_ordinary_service_registration_untouched(self) -> None:
        registration = Path(os.path.expanduser("~/.local/state/opencode/service.json"))
        before = _file_fingerprint(registration)
        manager = self.manager()
        manager.start()
        manager.stop()
        self.assertEqual(before, _file_fingerprint(registration))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
