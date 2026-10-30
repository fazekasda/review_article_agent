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
  the binary and ``GET /api/info``, and the server's ``pid`` equal to the owned
  child;
* the child environment is scrubbed of every inherited ``OPENCODE_*`` /
  ``OPENCHAMBER_*`` / ``AI_AGENT`` variable and every provider secret, while an
  explicitly supplied, **known** provider credential name is injected and never
  persisted to the config or the ledger;
* the merged config denies **all** providers and allows only the configured role
  providers, denies all tools/integrations, disables update/warming/automatic
  compaction, and the server reads only the manager's own config document;
* two clean, owned, symlink-free project locations; and a restart that preserves
  sessions (real HTTP session create/read, ``cost == 0``);
* refusals that never signal or harm a foreign process: an unsafe/unowned root, a
  symlinked root, a symlinked internal directory, a symlinked lock, a dirty
  marked location, an inherited ancestor config, a foreign PID record, a stale
  PID, a corrupted record (reported invalid, never absent), a wrong
  binary/server version, conflicting config/credential contexts, and invalid
  timeouts/ports.

The manager itself performs only bounded ``GET /api/info`` and ``GET /api/config``
and never submits a prompt. Session creation lives in this file's private HTTP
helper (used solely to evidence restart persistence); **no model, provider, paid
call, or source upload is made**.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import http.server
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from review_article_agent.runtime import roles, service  # noqa: E402
from review_article_agent.runtime.roles import RoleConfiguration  # noqa: E402
from review_article_agent.runtime.service import (  # noqa: E402
    LoopbackFixtureProvider,
    OpenCodeServiceManager,
    ServiceConfigError,
    ServiceLocationError,
    ServiceLockError,
    ServiceOwnershipError,
    ServiceProcessError,
    ServiceStartError,
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


def _pids_with_cwd(path: str) -> list[int]:
    wanted = os.path.realpath(path)
    found: list[int] = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            if os.path.realpath(os.readlink(f"/proc/{entry}/cwd")) == wanted:
                found.append(int(entry))
        except OSError:
            continue
    return found


def _file_fingerprint(path: Path):
    if not path.exists():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


class _MiniServer:
    """A tiny threaded HTTP server recording requests (hermetic tests)."""

    def __init__(self, handler) -> None:
        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def origin(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


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
# Pure configuration / fixture provider / knobs (no process)
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
        self.assertNotIn("model", config)
        self.assertNotIn("default_agent", config)
        for v1_key in roles.OPENCODE_DEPRECATED_V1_KEYS:
            self.assertNotIn(v1_key, config)
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
            ("fx", "https://evil.example/v1", ("m",)),
            ("fx", "http://127.0.0.1:59321/v2", ("m",)),
            ("openrouter", "http://127.0.0.1:59321/v1", ("m",)),
            ("fx", "http://127.0.0.1/v1", ("m",)),
            ("FX", "http://127.0.0.1:59321/v1", ("m",)),
            ("fx", "http://127.0.0.1:59321/v1", ()),
            ("fx", "http://user:pw@127.0.0.1:59321/v1", ("m",)),
        ]
        for provider_id, base_url, models in refused:
            with self.assertRaises(ServiceConfigError, msg=(provider_id, base_url)):
                LoopbackFixtureProvider(provider_id=provider_id, base_url=base_url, models=models)

    def test_provider_env_allowlist_is_enforced(self) -> None:
        base = Path(tempfile.mkdtemp(dir=SCRATCH_BASE, prefix="raa-service-env-"))
        try:
            for bad in (
                {"EVIL_VAR": "x"},
                {"OPENROUTER_API_KEY": ""},
                {"lowercase": "x"},
            ):
                with self.assertRaises(ServiceConfigError):
                    OpenCodeServiceManager(
                        data_dir=base, configuration=_role_config(), provider_env=bad
                    )
            ok = OpenCodeServiceManager(
                data_dir=base,
                configuration=_role_config(),
                provider_env={"OPENROUTER_API_KEY": "value"},
            )
            self.assertEqual(ok.provider_env, {"OPENROUTER_API_KEY": "value"})
        finally:
            shutil.rmtree(base, ignore_errors=True)

    def test_invalid_lifecycle_knobs_refused(self) -> None:
        base = Path(tempfile.mkdtemp(dir=SCRATCH_BASE, prefix="raa-service-knobs-"))
        try:
            for knob in ("start_timeout", "stop_timeout", "lock_timeout"):
                for value in (float("nan"), float("inf"), -1.0, 0.0, 1e9, True, "5"):
                    with self.assertRaises(ServiceConfigError, msg=(knob, value)):
                        OpenCodeServiceManager(
                            data_dir=base, configuration=_role_config(), **{knob: value}
                        )
            for port in (0, 70000, -1, True):
                with self.assertRaises(ServiceConfigError, msg=port):
                    OpenCodeServiceManager(
                        data_dir=base, configuration=_role_config(), port=port
                    )
            # A sane explicit port is accepted.
            ok = OpenCodeServiceManager(
                data_dir=base, configuration=_role_config(), port=45123
            )
            self.assertEqual(ok._port, 45123)
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
# Filesystem trust boundary (no running server)
# --------------------------------------------------------------------------- #


class FilesystemBoundaryTests(_ServiceCase):
    def test_default_xdg_data_dir_supported(self) -> None:
        with tempfile.TemporaryDirectory(dir=SCRATCH_BASE) as home:
            with mock.patch.dict(os.environ, {"HOME": home}, clear=False):
                data = os.path.join(home, ".local", "share", "review_article_agent")
                manager = OpenCodeServiceManager(
                    data_dir=data, configuration=_role_config(), binary=OPENCODE_BIN
                )
                self._managers.append(manager)
                manager._assert_root_safe()  # must not raise (blanket .local block removed)
                manager._prepare_data_dir()
                self.assertTrue(os.path.isdir(data))
                # An empty ancestor `.opencode` (e.g. ~/.opencode/plan) contributes
                # nothing and must not block the product default.
                os.makedirs(os.path.join(home, ".opencode", "plan"))
                manager._assert_root_safe()
                # A risky ancestor `.opencode/agents` is refused.
                os.makedirs(os.path.join(home, ".opencode", "agents"))
                with self.assertRaises(ServiceOwnershipError):
                    manager._assert_root_safe()

    def test_ordinary_opencode_locations_still_protected(self) -> None:
        with tempfile.TemporaryDirectory(dir=SCRATCH_BASE) as home:
            with mock.patch.dict(os.environ, {"HOME": home}, clear=False):
                for relative in (".config/opencode", ".local/share/opencode", ".opencode"):
                    data = os.path.join(home, relative)
                    manager = OpenCodeServiceManager(
                        data_dir=data, configuration=_role_config(), binary=OPENCODE_BIN
                    )
                    with self.assertRaises(ServiceOwnershipError, msg=relative):
                        manager._assert_root_safe()

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

    def test_wrong_marker_version_refused(self) -> None:
        manager = self.manager()
        manager.location_for("prj_marker")
        doc = json.loads(manager.marker.read_text(encoding="utf-8"))
        doc["markerVersion"] = 999
        manager.marker.write_text(json.dumps(doc), encoding="utf-8")
        self.assertFalse(manager.is_owned_root())
        with self.assertRaises(ServiceOwnershipError):
            manager.stop()

    def test_inherited_ancestor_config_refused(self) -> None:
        outer = self.data_dir / "outer"
        inner = outer / "inner"
        inner.mkdir(parents=True)
        (outer / "opencode.json").write_text("{}", encoding="utf-8")
        manager = self.manager(data_dir=inner)
        with self.assertRaises(ServiceOwnershipError):
            manager.start()
        self.assertFalse((inner / service.SERVICE_ROOT_DIRNAME).exists())

    def test_internal_dir_symlink_refused_without_chmod(self) -> None:
        manager = self.manager()
        manager.location_for("prj_ok")  # creates + marks the owned root, no server
        unrelated = self.data_dir / "unrelated"
        unrelated.mkdir()
        os.chmod(unrelated, 0o755)
        shutil.rmtree(manager.home)
        os.symlink(unrelated, manager.home)
        with self.assertRaises(ServiceOwnershipError):
            manager.location_for("prj_other")
        # The unrelated directory is untouched (never chmod-ed to 0700).
        self.assertEqual(os.stat(unrelated).st_mode & 0o777, 0o755)

    def test_dirty_marked_location_refused(self) -> None:
        manager = self.manager()
        location = manager.location_for("prj_dirty")
        (Path(location.directory) / "opencode.json").write_text("{}", encoding="utf-8")
        with self.assertRaises(ServiceOwnershipError):
            manager.location_for("prj_dirty")

    def test_existing_location_is_read_only_and_never_creates(self) -> None:
        # A fresh manager whose explicit data dir does not exist yet: a read-only
        # lookup refuses and creates no directory at all.
        missing_dir = self.data_dir / "not-created"
        absent = self.manager(data_dir=missing_dir)
        with self.assertRaises(ServiceOwnershipError):
            absent.existing_location("prj_absent")
        self.assertFalse(missing_dir.exists())

        # After one owned location exists, a different project stays absent and an
        # existing one is returned without any mkdir/chmod/marker/lock write.
        manager = self.manager()
        created = manager.location_for("prj_present")
        root_mode = os.stat(manager.root).st_mode & 0o777
        projects_mode = os.stat(manager.projects_dir).st_mode & 0o777
        marker = Path(created.directory) / service.LOCATION_MARKER_NAME
        marker_bytes = marker.read_bytes()
        marker_mode = os.stat(marker).st_mode & 0o777

        with self.assertRaises(ServiceLocationError):
            manager.existing_location("prj_absent")
        self.assertFalse((manager.projects_dir / "prj_absent").exists())

        # A stale permissive directory mode must NOT be "repaired" by a read.
        os.chmod(created.directory, 0o755)
        proved = manager.existing_location("prj_present")
        self.assertEqual(proved.directory, created.directory)
        self.assertEqual(proved.location_id, created.location_id)
        self.assertEqual(proved.root, created.root)
        self.assertEqual(os.stat(created.directory).st_mode & 0o777, 0o755)
        self.assertEqual(os.stat(manager.root).st_mode & 0o777, root_mode)
        self.assertEqual(os.stat(manager.projects_dir).st_mode & 0o777, projects_mode)
        self.assertEqual(marker.read_bytes(), marker_bytes)
        self.assertEqual(os.stat(marker).st_mode & 0o777, marker_mode)

    def test_lock_symlink_refused(self) -> None:
        manager = self.manager()
        manager.location_for("prj_lock")
        decoy = self.data_dir / "decoy.lock"
        decoy.write_text("", encoding="utf-8")
        manager.lock_file.unlink()  # location_for already created the real lock file
        os.symlink(decoy, manager.lock_file)
        with self.assertRaises(ServiceLockError):
            manager.stop()

    def test_corrupted_record_is_invalid_not_absent(self) -> None:
        manager = self.manager()
        manager.location_for("prj_corrupt")
        manager.pid_record.write_text("{not json", encoding="utf-8")
        self.assertEqual(manager.status().state, "invalid")
        with self.assertRaises(ServiceProcessError):
            manager.stop()
        with self.assertRaises(ServiceProcessError):
            manager.start()
        # The unresolved record is NOT cleared.
        self.assertTrue(manager.pid_record.exists())

    def test_stale_pid_record_cleared_without_signal(self) -> None:
        manager = self.manager()
        manager.location_for("prj_stale")
        manager.pid_record.write_text(
            json.dumps(
                {
                    "recordVersion": 1,
                    "pid": 2_000_000_000,  # far above any real pid space
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
# HTTP trust boundary (hermetic; no real opencode)
# --------------------------------------------------------------------------- #


class HttpBoundaryTests(unittest.TestCase):
    def test_redirect_refused_and_secret_not_forwarded(self) -> None:
        sink_hits: list[dict[str, str]] = []

        class Sink(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                sink_hits.append(dict(self.headers))
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"version":"2.2.0"}')

            def log_message(self, *args):  # noqa: ANN002
                pass

        sink = _MiniServer(Sink)
        sink_origin = sink.origin

        class Origin(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                self.send_response(302)
                self.send_header("Location", f"{sink_origin}/api/info")
                self.end_headers()

            def log_message(self, *args):  # noqa: ANN002
                pass

        origin = _MiniServer(Origin)
        try:
            with self.assertRaises(urllib.error.HTTPError):
                service._default_http_json(origin.origin, "opencode", CANARY, 3.0, "/api/info")
            self.assertEqual(sink_hits, [], "the redirect target must never be contacted")
        finally:
            origin.close()
            sink.close()

    def test_proxy_environment_ignored(self) -> None:
        proxy_hits: list[str] = []
        responses = {"n": 0}

        class Origin(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                responses["n"] += 1
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"version":"2.2.0","pid":7}')

            def log_message(self, *args):  # noqa: ANN002
                pass

        class Proxy(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                proxy_hits.append(self.path)
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, *args):  # noqa: ANN002
                pass

        origin = _MiniServer(Origin)
        proxy = _MiniServer(Proxy)
        try:
            with mock.patch.dict(
                os.environ,
                {"HTTP_PROXY": proxy.origin, "http_proxy": proxy.origin, "ALL_PROXY": proxy.origin},
                clear=False,
            ):
                result = service._default_http_json(origin.origin, "opencode", "pw", 3.0, "/api/info")
            self.assertEqual(result.get("version"), "2.2.0")
            self.assertEqual(proxy_hits, [], "environment proxies must be disabled")
        finally:
            origin.close()
            proxy.close()

    def test_probe_origin_and_path_restricted(self) -> None:
        # A non-loopback origin, an unknown path, and an origin with a path are refused.
        for url, path in (
            ("http://example.com", "/api/info"),
            ("http://127.0.0.1:1234", "/api/session"),
            ("http://127.0.0.1:1234/other", "/api/info"),
            ("https://127.0.0.1:1234", "/api/info"),
        ):
            with self.assertRaises(ServiceProcessError, msg=(url, path)):
                service._default_http_json(url, "opencode", "pw", 1.0, path)


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
        self.assertEqual(started.url, f"http://127.0.0.1:{manager._read_port()}")

        self.assertEqual(_http_status(started.url, "/api/info"), 401)
        code, info = _http("GET", started.url, "/api/info", manager.read_password())
        self.assertEqual(code, 200)
        self.assertEqual(info["version"], "2.2.0")
        self.assertEqual(info["pid"], started.pid)

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
        self.assertTrue(manager.root.is_dir())
        self.assertTrue(manager.home.is_dir())

    def test_child_argv_is_foreground_serve_only(self) -> None:
        manager = self.manager()
        started = manager.start()
        argv = _proc_cmdline(started.pid)
        port = manager._read_port()
        self.assertEqual(argv[1:], ["serve", "--hostname", "127.0.0.1", "--port", str(port)])
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
            self.assertEqual(env.get("OPENROUTER_API_KEY"), CANARY)
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
        self.assertNotIn(password, manager.config_bytes.decode("utf-8"))
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
        self.assertEqual(first.location_id, manager.location_for("prj_alpha").location_id)
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
        self.assertNotEqual(created["data"].get("projectID"), "prj_persist")

        restarted = manager.restart()
        self.assertTrue(restarted.running)
        self.assertNotEqual(restarted.pid, started.pid)
        _, listing = _http("GET", restarted.url, "/api/session", manager.read_password())
        ids = [entry["id"] for entry in listing["data"]]
        self.assertIn(session_id, ids)

    def test_concurrent_start_idempotent_and_conflicting_context_refused(self) -> None:
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

        # Same config + same credential context: idempotent.
        same = self.manager()
        self.assertEqual(same.start().pid, oks[0].pid)

        # Same config, DIFFERENT credential context: refused (never silently reuse).
        different_creds = self.manager(provider_env={"OPENROUTER_API_KEY": "other-value"})
        with self.assertRaises(ServiceConfigError):
            different_creds.start()

        # Different config while an instance runs: refused, unharmed.
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
        self.assertTrue(_pid_alive(started.pid))
        record["starttime"] = original_starttime
        manager.pid_record.write_text(json.dumps(record), encoding="utf-8")
        self.assertEqual(manager.stop().state, "stopped")

    def test_foreign_unrelated_process_never_signalled(self) -> None:
        manager = self.manager()
        manager.start()
        real_record = manager.pid_record.read_text(encoding="utf-8")
        unrelated = subprocess.Popen(["sleep", "300"], start_new_session=True)
        try:
            manager.pid_record.write_text(
                json.dumps(
                    {
                        "recordVersion": 1,
                        "pid": unrelated.pid,
                        "starttime": "1",
                        "exe": "/nonexistent",
                        "cwd": "/nonexistent",
                        "home": "/nonexistent",
                        "passwordSha256": "0" * 64,
                    }
                ),
                encoding="utf-8",
            )
            self.assertEqual(manager.status().state, "foreign")
            with self.assertRaises(ServiceProcessError):
                manager.stop()
            self.assertTrue(_pid_alive(unrelated.pid), "an unrelated process was harmed")
        finally:
            unrelated.terminate()
            try:
                unrelated.wait(timeout=10)
            except subprocess.TimeoutExpired:
                unrelated.kill()
            # Restore our truthful record and stop cleanly so no owned child leaks.
            manager.pid_record.write_text(real_record, encoding="utf-8")
            manager.stop()

    def test_failed_record_write_leaves_no_unmanaged_child(self) -> None:
        manager = self.manager()
        original = manager._write_pid_record
        state = {"calls": 0}

        def flaky(record):  # noqa: ANN001
            state["calls"] += 1
            if state["calls"] == 1:
                raise OSError("simulated record-write failure")
            return original(record)

        manager._write_pid_record = flaky  # type: ignore[assignment]
        with self.assertRaises(OSError):
            manager.start()
        self.assertIsNone(manager._proc)
        self.assertFalse(manager.pid_record.exists())
        self.assertEqual(_pids_with_cwd(str(manager.cwd)), [], "an unmanaged child survived")

    def test_wrong_server_version_fails_closed(self) -> None:
        def fake_http(url, username, password, timeout, path="/api/info"):
            return {"version": "0.0.1", "pid": 1}

        manager = self.manager(http_get=fake_http)
        with self.assertRaises(ServiceVersionError):
            manager.start()
        self.assertFalse(manager.pid_record.exists())
        self.assertFalse(manager.secret_file.exists())
        self.assertEqual(_pids_with_cwd(str(manager.cwd)), [])
        self.assertFalse(manager.status().running)

    def test_server_pid_mismatch_fails_closed(self) -> None:
        real = service._default_http_json

        def fake_http(url, username, password, timeout, path="/api/info"):
            info = real(url, username, password, timeout, path)
            if path == "/api/info" and isinstance(info, dict):
                info = dict(info)
                info["pid"] = (info.get("pid") or 0) + 1
            return info

        manager = self.manager(http_get=fake_http)
        with self.assertRaises(ServiceProcessError):
            manager.start()
        self.assertFalse(manager.pid_record.exists())
        self.assertEqual(_pids_with_cwd(str(manager.cwd)), [])

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
        registrations = [
            Path(os.path.expanduser("~/.config/opencode/service.json")),
            Path(os.path.expanduser("~/.local/state/opencode/service.json")),
        ]
        before = [_file_fingerprint(path) for path in registrations]
        manager = self.manager()
        manager.start()
        manager.stop()
        after = [_file_fingerprint(path) for path in registrations]
        self.assertEqual(before, after)


# --------------------------------------------------------------------------- #
# Repair R2: full managed-path containment (hermetic unless noted)
# --------------------------------------------------------------------------- #


class Repair2ContainmentTests(_ServiceCase):
    """Exact regressions for the remaining service containment gaps (repair R2).

    Each test targets one reviewed hole: the leaf-only path check, non-private
    secret modes, log-file truncation of a foreign/hardlinked file, the raw
    ``OverflowError`` on a huge timeout, the provider-env ``$``/NUL gaps, the
    split startup deadline, identity retention on a surviving child, and
    ``descriptor()``/``connection()``/idempotent ``start`` trusting disk state.
    """

    def test_huge_timeout_int_refused(self) -> None:
        for knob in ("start_timeout", "stop_timeout", "lock_timeout"):
            with self.assertRaises(ServiceConfigError, msg=knob):
                OpenCodeServiceManager(
                    data_dir=self.data_dir,
                    configuration=_role_config(),
                    **{knob: 10 ** 10000},
                )

    def test_provider_env_name_newline_and_invalid_value_refused(self) -> None:
        for bad in (
            {"OPENROUTER_API_KEY\n": "x"},
            {"OPENROUTER_API_KEY ": "x"},
            {"OPENROUTER_API_KEY": "has\x00nul"},
            {"OPENROUTER_API_KEY": "x" * (service.MAX_PROVIDER_ENV_VALUE_BYTES + 1)},
        ):
            with self.assertRaises(ServiceConfigError, msg=repr(bad)[:32]):
                OpenCodeServiceManager(
                    data_dir=self.data_dir, configuration=_role_config(), provider_env=bad
                )

    def test_swapped_run_dir_cannot_read_or_delete_external_files(self) -> None:
        manager = self.manager()
        manager.location_for("prj_swap")  # creates + marks the owned root
        external = self.data_dir / "external"
        external.mkdir()
        (external / service.PID_RECORD_NAME).write_text(
            json.dumps({"recordVersion": 1, "pid": 2}), encoding="utf-8"
        )
        (external / "password").write_text("external-secret", encoding="utf-8")
        os.chmod(external / "password", 0o600)
        shutil.rmtree(manager.run)
        os.symlink(external, manager.run)

        # The record is reported invalid (never absent/stopped), reads refuse,
        # and the external files are neither read nor deleted.
        self.assertEqual(manager.status().state, "invalid")
        with self.assertRaises(ServiceProcessError):
            manager.stop()
        with self.assertRaises(ServiceOwnershipError):
            manager.read_password()
        self.assertTrue((external / service.PID_RECORD_NAME).exists())
        self.assertEqual((external / "password").read_text(encoding="utf-8"), "external-secret")

    def test_swapped_run_dir_without_record_is_invalid_not_stopped(self) -> None:
        manager = self.manager()
        manager.location_for("prj_swap2")
        external = self.data_dir / "external2"
        external.mkdir()
        shutil.rmtree(manager.run)
        os.symlink(external, manager.run)
        self.assertEqual(manager.status().state, "invalid")
        with self.assertRaises(ServiceProcessError):
            manager.stop()

    def test_group_readable_secret_refused(self) -> None:
        manager = self.manager()
        manager.location_for("prj_secret")
        manager.secret_file.write_bytes(b"top-secret")
        os.chmod(manager.secret_file, 0o644)
        with self.assertRaises(ServiceOwnershipError):
            manager.read_password()
        os.chmod(manager.secret_file, 0o600)
        self.assertEqual(manager.read_password(), "top-secret")

    def test_log_open_refuses_symlink_dir_and_hardlink(self) -> None:
        manager = self.manager()
        manager.location_for("prj_log")
        target = self.data_dir / "target.log"
        target.write_text("keep", encoding="utf-8")
        os.symlink(target, manager.stdout_file)
        with self.assertRaises(ServiceOwnershipError):
            manager._open_log(manager.stdout_file)
        manager.stdout_file.unlink()
        manager.stdout_file.mkdir()
        with self.assertRaises(ServiceOwnershipError):
            manager._open_log(manager.stdout_file)
        manager.stdout_file.rmdir()
        hard_target = self.data_dir / "hard.log"
        hard_target.write_text("keep-hard", encoding="utf-8")
        os.link(hard_target, manager.stdout_file)
        with self.assertRaises(ServiceOwnershipError):
            manager._open_log(manager.stdout_file)
        self.assertEqual(hard_target.read_text(encoding="utf-8"), "keep-hard")
        # A fresh, owned, singly-linked file is opened (and truncated) normally.
        manager.stdout_file.unlink()
        fd = manager._open_log(manager.stdout_file)
        os.close(fd)
        self.assertTrue(manager.stdout_file.exists())

    def test_startup_deadline_is_shared_and_enforced(self) -> None:
        manager = self.manager()
        expired = time.monotonic() - 1.0
        with self.assertRaises(ServiceStartError):
            manager._verify_binary_version(OPENCODE_BIN or "/bin/true", expired)
        with self.assertRaises(ServiceStartError):
            manager._assert_server_owned(1, 1234, "pw", expired)
        fake_proc = mock.Mock()
        fake_proc.poll.return_value = None
        with self.assertRaises(ServiceStartError):
            manager._await_ready(fake_proc, 1234, "pw", expired)

    def test_start_failure_retains_identity_when_child_survives(self) -> None:
        manager = self.manager()
        fake_proc = mock.Mock()
        fake_proc.pid = 999_999
        fake_proc.poll.return_value = None
        # This test never runs a real binary: pin binary resolution to a
        # harmless path so the test does not depend on `opencode` being on the
        # developer's/CI runner's PATH.
        manager._resolve_binary = mock.Mock(return_value="/bin/true")
        manager._spawn = mock.Mock(return_value=fake_proc)
        manager._verify_binary_version = mock.Mock(return_value="2.2.0")
        manager._await_ready = mock.Mock(side_effect=ServiceStartError("x", "x"))
        manager._terminate_child = mock.Mock(return_value=False)
        with self.assertRaises(ServiceStartError):
            manager.start()
        # The child could not be proven gone: its identity record is retained.
        self.assertTrue(manager.pid_record.exists())
        self.assertTrue(manager.secret_file.exists())

    def test_start_failure_clears_identity_when_child_reaped(self) -> None:
        manager = self.manager()
        fake_proc = mock.Mock()
        fake_proc.pid = 999_999
        fake_proc.poll.return_value = None
        # This test never runs a real binary: pin binary resolution to a
        # harmless path so the test does not depend on `opencode` being on the
        # developer's/CI runner's PATH.
        manager._resolve_binary = mock.Mock(return_value="/bin/true")
        manager._spawn = mock.Mock(return_value=fake_proc)
        manager._verify_binary_version = mock.Mock(return_value="2.2.0")
        manager._await_ready = mock.Mock(side_effect=ServiceStartError("x", "x"))
        manager._terminate_child = mock.Mock(return_value=True)
        with self.assertRaises(ServiceStartError):
            manager.start()
        self.assertFalse(manager.pid_record.exists())
        self.assertFalse(manager.secret_file.exists())

    @unittest.skipUnless(HAVE_REAL, "opencode v2.2.0 is not installed")
    def test_start_uses_a_single_deadline_for_all_steps(self) -> None:
        manager = self.manager()
        seen: list[float] = []
        real_verify = manager._verify_binary_version
        real_ready = manager._await_ready
        real_owned = manager._assert_server_owned

        def spy_verify(binary, deadline):  # noqa: ANN001
            seen.append(deadline)
            return real_verify(binary, deadline)

        def spy_ready(proc, port, password, deadline):  # noqa: ANN001
            seen.append(deadline)
            return real_ready(proc, port, password, deadline)

        def spy_owned(pid, port, password, deadline):  # noqa: ANN001
            seen.append(deadline)
            return real_owned(pid, port, password, deadline)

        manager._verify_binary_version = spy_verify  # type: ignore[assignment]
        manager._await_ready = spy_ready  # type: ignore[assignment]
        manager._assert_server_owned = spy_owned  # type: ignore[assignment]
        manager.start()
        self.assertEqual(len(seen), 3)
        self.assertEqual(len(set(seen)), 1, seen)

    @unittest.skipUnless(HAVE_REAL, "opencode v2.2.0 is not installed")
    def test_descriptor_refuses_tampered_document_and_requires_live_instance(self) -> None:
        manager = self.manager()
        manager.start()
        good = manager.descriptor()
        self.assertEqual(good.root, str(manager.root))
        self.assertEqual(good.home, str(manager.home))
        original = manager.descriptor_file.read_bytes()
        doc = json.loads(original.decode("utf-8"))
        doc["url"] = "http://evil.example:9999"
        doc["dirs"]["home"] = "/etc"
        manager.descriptor_file.write_text(json.dumps(doc), encoding="utf-8")
        with self.assertRaises(ServiceProcessError):
            manager.descriptor()
        manager.descriptor_file.write_bytes(original)
        self.assertEqual(manager.descriptor().url, good.url)
        manager.stop()
        with self.assertRaises(ServiceProcessError):
            manager.descriptor()

    @unittest.skipUnless(HAVE_REAL, "opencode v2.2.0 is not installed")
    def test_connection_refuses_changed_port_file(self) -> None:
        manager = self.manager()
        manager.start()
        self.assertTrue(manager.connection().base_url.startswith("http://127.0.0.1:"))
        manager.port_file.write_text("1", encoding="utf-8")
        with self.assertRaises(ServiceProcessError):
            manager.connection()

    @unittest.skipUnless(HAVE_REAL, "opencode v2.2.0 is not installed")
    def test_idempotent_start_refuses_drifted_config_or_dirty_tree(self) -> None:
        manager = self.manager()
        manager.start()
        manager.config_file.write_text("{}", encoding="utf-8")
        with self.assertRaises(ServiceOwnershipError):
            manager.start()
        # Restore our config, then plant a risky entry in the owned tree.
        manager._write_config()
        (manager.home / "AGENTS.md").write_text("evil", encoding="utf-8")
        with self.assertRaises(ServiceOwnershipError):
            manager.start()


# --------------------------------------------------------------------------- #
# Repair R3: managed-file proof, shared-writable chains, non-blocking reads
# --------------------------------------------------------------------------- #


class Repair3ManagedFileProofTests(_ServiceCase):
    """Exact regressions for the three remaining service boundary causes (R3).

    Each test targets one reviewed hole: ``connection()``/``descriptor()``/
    idempotent ``start`` trusting in-memory bytes while the on-disk config is
    missing; a silently skipped symlinked internal config directory; a
    group/other-writable internal or root directory treated as owned; and an
    ``os.open`` of a planted FIFO blocking a lifecycle read forever.
    """

    def _mock_owned(self, manager: OpenCodeServiceManager, record: dict) -> object:
        """Patch a manager to look like a live owned instance without a process."""
        stack = contextlib.ExitStack()
        stack.enter_context(mock.patch.object(manager, "_process_state", return_value="ours"))
        stack.enter_context(mock.patch.object(manager, "_read_pid_record", return_value=("ok", record)))
        stack.enter_context(mock.patch.object(manager, "_read_port", return_value=record.get("port")))
        return stack

    def test_connection_and_descriptor_require_on_disk_config(self) -> None:
        manager = self.manager()
        manager.location_for("prj_missing_cfg")  # creates + marks the owned root
        manager._write_config()
        manager.config_file.unlink()  # in-memory bytes remain valid; disk document is gone
        record = {"port": 45678}
        with self._mock_owned(manager, record):
            with self.assertRaises(ServiceOwnershipError):
                manager.connection()
            with self.assertRaises(ServiceOwnershipError):
                manager.descriptor()
        # Restoring the exact on-disk document makes the same proof succeed, so
        # the refusal is about the missing document, not a blanket rejection.
        manager._write_config()
        with self._mock_owned(manager, record):
            self.assertEqual(manager.connection().base_url, "http://127.0.0.1:45678")

    def test_idempotent_start_requires_on_disk_config(self) -> None:
        manager = self.manager()
        manager.location_for("prj_start_cfg")
        manager._write_config()
        manager.config_file.unlink()
        record = {
            "port": 45678,
            "configFingerprint": manager.config_fingerprint,
            "instanceFence": manager.instance_fence,
        }
        with mock.patch.object(manager, "_process_state", return_value="ours"):
            with mock.patch.object(
                manager, "_read_pid_record", return_value=("ok", record)
            ):
                with self.assertRaises(ServiceOwnershipError):
                    manager.start()

    def test_symlinked_internal_config_dir_refused(self) -> None:
        manager = self.manager()
        manager.location_for("prj_sym_cfg")
        manager._write_config()
        external = self.data_dir / "external-config"
        external.mkdir()
        # Byte-identical config in the target: only the symlinked directory is
        # the problem, and it must be refused rather than silently skipped.
        (external / "opencode.json").write_bytes(manager._config_disk_bytes)
        config_dir = manager.config_file.parent
        shutil.rmtree(config_dir)
        os.symlink(external, config_dir)
        with self._mock_owned(manager, {"port": 45678}):
            with self.assertRaises(ServiceOwnershipError):
                manager.connection()
        # The external target was never read or modified through the symlink.
        self.assertEqual(
            (external / "opencode.json").read_bytes(), manager._config_disk_bytes
        )

    def test_shared_writable_dir_chain_rejected(self) -> None:
        manager = self.manager()
        manager.location_for("prj_shared")
        manager._write_config()
        manager.port_file.write_bytes(b"45678")
        os.chmod(manager.port_file, 0o600)
        self.assertTrue(manager._dir_chain_ok(manager.run))
        os.chmod(manager.run, 0o777)
        try:
            self.assertFalse(manager._dir_chain_ok(manager.run))
            # Even with a matching live record and port, a shared-writable run
            # chain is refused: the port read cannot be trusted through it.
            with mock.patch.object(manager, "_process_state", return_value="ours"):
                with mock.patch.object(
                    manager, "_read_pid_record", return_value=("ok", {"port": 45678})
                ):
                    with self.assertRaises(ServiceProcessError):
                        manager.connection()
        finally:
            os.chmod(manager.run, 0o700)
        # The owned root itself is subject to the same shared-writable refusal.
        os.chmod(manager.root, 0o777)
        try:
            self.assertFalse(manager._dir_chain_ok(manager.root))
        finally:
            os.chmod(manager.root, 0o700)

    def test_fifo_read_denied_without_blocking(self) -> None:
        manager = self.manager()
        manager.location_for("prj_fifo")
        fifo = manager.run / "probe-fifo"
        os.mkfifo(fifo)
        program = (
            "import sys\n"
            f"sys.path.insert(0, {str(SRC)!r})\n"
            "from pathlib import Path\n"
            "from review_article_agent.runtime.roles import RoleConfiguration\n"
            "from review_article_agent.runtime.service import OpenCodeServiceManager\n"
            "cfg = RoleConfiguration.from_mapping({'schema_version': 1, 'roles': {"
            "'writer': {'provider_id': 'openrouter', "
            "'model_id': 'deepseek/deepseek-v4.1-flash'}, "
            "'section_critic': {'provider_id': 'anthropic', "
            "'model_id': 'claude-sonnet-5-5'}}})\n"
            f"m = OpenCodeServiceManager(data_dir={str(self.data_dir)!r}, configuration=cfg)\n"
            f"print('RESULT', m._read_owned_file(Path({str(fifo)!r}), max_bytes=64))\n"
        )
        started = time.monotonic()
        proc = subprocess.run(
            [sys.executable, "-c", program],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        elapsed = time.monotonic() - started
        # A blocking open would hang this child until the timeout; the bounded
        # read must return a static denial well before that.
        self.assertLess(elapsed, 2.0, proc.stderr)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("RESULT None", proc.stdout)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
