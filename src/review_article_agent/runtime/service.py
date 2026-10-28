"""App-owned, version-pinned OpenCode V2 **service manager** (Step 04, task T4).

This module owns the *lifecycle* of one disposable, app-owned OpenCode V2
service: it creates a private, owner-only tree under an **explicit**
``data_dir``, writes the V2 configuration emitted by the integrated pure role
boundary (:mod:`review_article_agent.runtime.roles`), starts a *foreground*
``opencode serve --hostname 127.0.0.1 --port …`` process with a scrubbed
environment, proves the pinned binary/API version, and stops or restarts **only**
a process whose Linux identity it owns.

Authoritative sources (read-only):

* ``docs/implementation/04-agent-runtime.md`` deliverables 1/4 and the
  handoff note ("Document actual API/SDK versions, adapter contracts, …").
* ``docs/specification.md`` lines 14-16 (app-owned OpenCode V2 service, separate
  from ordinary coding sessions; do not depend on OpenCode's private database or
  parse private credentials) and line 62 (documents/model outputs are untrusted
  data; no public unauthenticated services or credential leakage).
* ``docs/decisions/001-runtime-language.md`` (Python core; fail-closed; explicit
  endpoint and token; loopback-only).
* The retained step-00 disposable harness findings
  (``spikes/runtime/harness/README.md`` and ``lib.sh``): explicit binary, an
  allowlisted child environment, a unique ``HOME``/``XDG_*`` tree, a cwd outside
  repository ancestors, loopback + per-run HTTP Basic password, no standard
  service registration, and ownership/``/proc``-identity guards. The harness is
  a **reference**: this module does not import it and never calls it at runtime.

What this manager owns and does **not** own
-------------------------------------------

Owned here: the private tree, the ownership marker, the run records, the child
process family, the HTTP Basic secret, and the *lifecycle* verbs ``start``,
``status``, ``stop``, ``restart``. Nothing else is discovered or started
automatically: there is **no** call to OpenCode's shared-service registration and
**no** ``opencode service …`` invocation. A caller asks this manager to start;
the manager never hunts the machine for an instance.

Deliberately **not** here: any model or provider call, any prompt/turn
submission, any scientific decision, any approval, and any access to OpenCode's
private database or its ordinary ``~/.local/state/opencode/service.json``
registration. The only HTTP this module performs is a bounded readiness
``GET /api/info`` and a config-source ``GET /api/config``; no session is created
here. The connection parameters a future client needs (loopback URL, Basic user,
the mode-0600 password file) are exposed safely — the secret itself is never
placed in a config, a ledger, a log, an exception, or a ``repr``.

Security boundary (do not overclaim)
------------------------------------

OpenCode and this application run as the **same** user, so the environment scrub,
the owner-only tree, the allowlisted child environment, the provider deny
policies, and the identity-checked signalling are *lifecycle and configuration
containment*, not a same-UID OS sandbox. A caller that already controls the user
could defeat them. The generated ``permissions``/``policies`` are the same
**model tool boundary** documented by :mod:`…runtime.roles`; they are not a proof
against a compromised OpenCode process.
"""

from __future__ import annotations

import base64
import errno
import fcntl
import hashlib
import json
import os
import re
import secrets
import shutil
import signal
import socket
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

from ..domain.errors import ReviewAgentError
from ..domain.records import canonical_json, validate_identifier
from .roles import RoleConfiguration, build_opencode_config

# --------------------------------------------------------------------------- #
# Vocabulary and hard bounds
# --------------------------------------------------------------------------- #

#: The exact OpenCode version this manager accepts. Both ``opencode --version``
#: and the live ``GET /api/info`` ``version`` must equal this; anything else is
#: refused **before** the service is left running (fail closed).
OPENCODE_PINNED_VERSION = "2.2.0"

#: Fixed, app-owned directory name under the explicit ``data_dir``.
SERVICE_ROOT_DIRNAME = "opencode-service"

#: Documented HTTP Basic user for ``opencode serve``.
SERVICE_USERNAME = "opencode"

#: Loopback-only bind address (``docs/decisions/001``).
SERVICE_HOST = "127.0.0.1"

#: The one provider runtime package this manager will ever name. It is used only
#: for an explicitly-supplied **loopback fixture** provider (disposable tests);
#: arbitrary packages/plugins/URLs are refused.
FIXTURE_PROVIDER_PACKAGE = "@opencode/ai/providers/openai-compatible"

#: Fixed, non-secret placeholder used by the documented step-00 fixture provider.
FIXTURE_PROVIDER_APIKEY = "fixture-no-secret"

#: Marker / record schema versions (bumped only with an explicit migration).
MARKER_NAME = "service-owned.json"
MARKER_KIND = "app-owned-review-agent-opencode-service"
MARKER_VERSION = 1
LOCATION_MARKER_NAME = ".location-owned.json"
LOCATION_MARKER_KIND = "app-owned-review-agent-opencode-location"
LOCATION_MARKER_VERSION = 1
PID_RECORD_NAME = "instance.pid.json"
DESCRIPTOR_NAME = "instance.json"
DESCRIPTOR_VERSION = 1
DESCRIPTOR_KIND = "app-owned-review-agent-opencode-service"

#: Bounded lifecycle deadlines (seconds).
DEFAULT_START_TIMEOUT_S = 30.0
DEFAULT_STOP_TIMEOUT_S = 15.0
DEFAULT_LOCK_TIMEOUT_S = 20.0
_PROBE_TIMEOUT_S = 3.0
_PROBE_INTERVAL_S = 0.2
_TERM_POLL_S = 0.15

#: The (*only*) provider credential environment variable names that may be
#: supplied explicitly and injected into the child. Values are **never** written
#: to the config or the ledger; only the names are recorded.
KNOWN_PROVIDER_ENV_NAMES: frozenset[str] = frozenset(
    {
        "ANTHROPIC_API_KEY",
        "AZURE_API_KEY",
        "AZURE_RESOURCE_NAME",
        "DEEPSEEK_API_KEY",
        "GEMINI_API_KEY",
        "GOOGLE_GENERATIVE_AI_API_KEY",
        "GROQ_API_KEY",
        "MISTRAL_API_KEY",
        "OPENAI_API_KEY",
        "OPENROUTER_API_KEY",
        "TOGETHER_API_KEY",
        "XAI_API_KEY",
    }
)

#: Ancestor entries that would let OpenCode discover *unowned* configuration,
#: instructions, or plugin directories for the child. Refused outright.
_INHERITED_NAMES: tuple[str, ...] = (
    "opencode.json",
    "opencode.jsonc",
    "AGENTS.md",
    ".opencode",
)

#: Machine/system configuration directories that a freshly-started server reads
#: regardless of ``HOME``/``XDG_*``. Refused when present (Linux system dir plus
#: the macOS managed directory, which is inert on Linux).
_SYSTEM_CONFIG_DIRS: tuple[str, ...] = (
    "/etc/opencode",
    "/Library/Application Support/opencode",
)

_ENV_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
_PROVIDER_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_FIXTURE_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")

# --------------------------------------------------------------------------- #
# Curated, static error messages (never interpolate a secret or external value)
# --------------------------------------------------------------------------- #

_MSG_CONFIG_TYPE = "The service manager requires a validated RoleConfiguration."
_MSG_PROVIDER_ENV = (
    "Provider environment variables must map a known provider credential name "
    "to a non-empty string value."
)
_MSG_DATA_DIR = "The service data directory must be an absolute path."
_MSG_ROOT_SYMLINK = "The app-owned service root must not be a symbolic link."
_MSG_ROOT_UNSAFE = "The app-owned service root is not a safe, owner-only location."
_MSG_ROOT_INHERITED = (
    "An ancestor or system location exposes unowned OpenCode configuration, "
    "instructions, or plugin directories; choose another data directory."
)
_MSG_ROOT_UNOWNED = (
    "The service root already exists but is not owned by this application; "
    "refusing to use or modify it."
)
_MSG_ROOT_NOT_DIR = "The app-owned service root exists and is not a directory."
_MSG_BINARY = "No executable opencode binary was found (supply an explicit path)."
_MSG_BINARY_VERSION = (
    "The opencode binary version does not match the pinned version this "
    "application requires; refusing to start."
)
_MSG_SERVER_VERSION = (
    "The running OpenCode server reported a version that does not match the "
    "pinned version; refusing to use it."
)
_MSG_LOCK_TIMEOUT = (
    "Another lifecycle operation holds the singleton lock for this data "
    "directory; timed out without changing state."
)
_MSG_LOCK_ERROR = "The singleton service lock could not be acquired."
_MSG_CONFLICTING_CONFIG = (
    "A service instance is already running for this data directory with a "
    "different configuration; refusing to start a conflicting instance."
)
_MSG_FOREIGN_PROCESS = (
    "The recorded process does not match this instance's identity; refusing to "
    "signal it."
)
_MSG_STOP_FOREIGN = (
    "A process is recorded that this manager does not own; refusing to stop it."
)
_MSG_START_FAILED = "The OpenCode server did not become ready within the deadline."
_MSG_START_EXITED = "The OpenCode server process exited during startup."
_MSG_LOCATION_SYMLINK = "A project location must not be a symbolic link."
_MSG_LOCATION_UNOWNED = (
    "A project location already exists but is not owned by this application; "
    "refusing to use it."
)
_MSG_LOCATION_NOT_DIR = "A project location exists and is not a directory."
_MSG_NOT_OWNED_STOP = "The service root is not owned by this application; refusing to stop."
_MSG_PROBE = "The service did not answer a bounded readiness request."
_MSG_FIXTURE = (
    "A fixture provider is accepted only as a loopback OpenAI-compatible "
    "endpoint with a fixed safe package."
)
_MSG_NO_SECRET = "No service secret is recorded for this instance."


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #


class ServiceError(ReviewAgentError):
    """Base class for every service lifecycle failure.

    ``code`` is a static, value-free identifier and the message is a constant;
    an error never echoes a password, a provider credential, a config document,
    or raw server output.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ServiceConfigError(ServiceError):
    """A manager input (role configuration, provider env, fixture) is invalid."""


class ServiceOwnershipError(ServiceError):
    """A root/location is unsafe, unowned, or not this application's."""


class ServiceProcessError(ServiceError):
    """A recorded process is foreign, stale, or not the owned instance."""


class ServiceVersionError(ServiceError):
    """The binary or the running server version is not the pinned version."""


class ServiceLockError(ServiceError):
    """The singleton lock could not be taken within its deadline."""


class ServiceStartError(ServiceError):
    """The owned server failed to start or become ready."""


class ServiceLocationError(ServiceError):
    """A project location could not be created or verified safely."""


class ServiceBinaryError(ServiceError):
    """No usable pinned opencode binary is available."""


# --------------------------------------------------------------------------- #
# Small pure helpers
# --------------------------------------------------------------------------- #


def _sha256_hex(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _paths_overlap(a: str, b: str) -> bool:
    return a == b or a.startswith(b + os.sep) or b.startswith(a + os.sep)


def _atomic_write(path: Path, data: bytes, mode: int) -> None:
    """Write ``data`` to ``path`` atomically with ``mode`` (owner-only default)."""
    directory = path.parent
    tmp = directory / f".{path.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    try:
        os.replace(str(tmp), str(path))
    except OSError:
        try:
            os.unlink(str(tmp))
        except OSError:
            pass
        raise
    os.chmod(str(path), mode)


def _read_json(path: Path) -> Any:
    with open(str(path), "rb") as handle:
        return json.loads(handle.read().decode("utf-8"))


def _free_loopback_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind((SERVICE_HOST, 0))
        return int(sock.getsockname()[1])
    finally:
        sock.close()


def _pid_alive(pid: int) -> bool:
    """True while ``pid`` is a live, non-zombie process we can inspect."""
    try:
        with open(f"/proc/{pid}/stat", "rb") as handle:
            raw = handle.read().decode("utf-8", "replace")
    except OSError:
        return False
    close = raw.rfind(")")
    if close < 0:
        return False
    fields = raw[close + 1 :].split()
    if not fields:
        return False
    return fields[0] != "Z"


def _proc_starttime(pid: int) -> str | None:
    try:
        with open(f"/proc/{pid}/stat", "rb") as handle:
            raw = handle.read().decode("utf-8", "replace")
    except OSError:
        return None
    close = raw.rfind(")")
    if close < 0:
        return None
    fields = raw[close + 1 :].split()
    # After "pid (comm) " the fields begin at ``state`` (overall field 3), so the
    # overall field 22 (``starttime``) is index 19 here.
    if len(fields) <= 19:
        return None
    return fields[19]


def _proc_exe(pid: int) -> str | None:
    try:
        return os.path.realpath(os.readlink(f"/proc/{pid}/exe"))
    except OSError:
        return None


def _proc_cwd(pid: int) -> str | None:
    try:
        return os.path.realpath(os.readlink(f"/proc/{pid}/cwd"))
    except OSError:
        return None


def _proc_environ(pid: int) -> dict[str, str]:
    try:
        with open(f"/proc/{pid}/environ", "rb") as handle:
            raw = handle.read()
    except OSError:
        return {}
    out: dict[str, str] = {}
    for entry in raw.split(b"\x00"):
        if not entry:
            continue
        name, sep, value = entry.partition(b"=")
        if not sep:
            continue
        out[name.decode("utf-8", "replace")] = value.decode("utf-8", "replace")
    return out


def _default_http_json(
    url: str,
    username: str,
    password: str,
    timeout: float,
    path: str = "/api/info",
) -> Any:
    """Bounded stdlib HTTP GET returning parsed JSON (readiness/config only)."""
    request = urllib.request.Request(url.rstrip("/") + path)
    token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
    request.add_header("Authorization", "Basic " + token)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        exc.close()
        raise


# --------------------------------------------------------------------------- #
# Fixture provider (disposable tests only; strictly validated)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class LoopbackFixtureProvider:
    """An **explicit, loopback-only** OpenAI-compatible fixture provider.

    A fixture exists so a disposable test can point a real server at a
    deterministic local model transport. It is deliberately narrow: the provider
    id is a safe token, the base URL is ``http`` on a loopback host with an
    explicit port and the documented ``/v1`` path, and the package is the single
    fixed constant :data:`FIXTURE_PROVIDER_PACKAGE`. No arbitrary package,
    plugin, command, or remote URL is accepted.
    """

    provider_id: str
    base_url: str
    models: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.provider_id, str) or not _PROVIDER_ID_RE.match(self.provider_id):
            raise ServiceConfigError("fixture_provider_id", _MSG_FIXTURE)
        if self.provider_id in KNOWN_PROVIDER_ENV_NAMES or self.provider_id in {
            "opencode",
            "openrouter",
            "openai",
            "anthropic",
        }:
            # Never shadow a built-in/catalog provider id.
            raise ServiceConfigError("fixture_provider_id", _MSG_FIXTURE)
        parsed = urllib.parse.urlsplit(self.base_url)
        if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise ServiceConfigError("fixture_provider_url", _MSG_FIXTURE)
        try:
            port = parsed.port
        except ValueError as exc:  # bad/out-of-range port
            raise ServiceConfigError("fixture_provider_url", _MSG_FIXTURE) from exc
        if port is None or not (1 <= port <= 65535):
            raise ServiceConfigError("fixture_provider_url", _MSG_FIXTURE)
        if parsed.path.rstrip("/") != "/v1":
            raise ServiceConfigError("fixture_provider_url", _MSG_FIXTURE)
        if parsed.query or parsed.fragment:
            raise ServiceConfigError("fixture_provider_url", _MSG_FIXTURE)
        models = tuple(self.models)
        if not models or len(models) > 16:
            raise ServiceConfigError("fixture_provider_models", _MSG_FIXTURE)
        for model in models:
            if not isinstance(model, str) or not _FIXTURE_MODEL_RE.match(model):
                raise ServiceConfigError("fixture_provider_models", _MSG_FIXTURE)
        object.__setattr__(self, "models", models)

    def to_config(self) -> dict[str, Any]:
        """The V2 ``providers.<id>`` object (fixed package, no credential)."""
        return {
            "name": f"Fixture Provider ({self.provider_id})",
            "package": FIXTURE_PROVIDER_PACKAGE,
            "settings": {
                "baseURL": self.base_url,
                "apiKey": FIXTURE_PROVIDER_APIKEY,
            },
            "models": {model: {} for model in self.models},
        }


# --------------------------------------------------------------------------- #
# Pure configuration builder
# --------------------------------------------------------------------------- #


def build_service_config(
    configuration: RoleConfiguration,
    *,
    fixture_provider: LoopbackFixtureProvider | None = None,
) -> dict[str, Any]:
    """Build the OpenCode **V2** config the manager writes for the service.

    Starts from the integrated, pure :func:`…roles.build_opencode_config` (which
    already denies every built-in tool action, hard-denies ``tool.use`` and
    ``integration.use``, blocks inherited MCP servers/skills/plugins, and pins
    each role's explicit model). It then adds:

    * ``provider.use`` policies: **deny all providers** first, then **allow only**
      the explicit providers named by the configured scientific roles (plus an
      explicitly-supplied loopback fixture provider, when present);
    * ``update: "disable"``, ``warming: false`` and ``compaction.auto: false`` —
      the verified V2 fields that stop update checks, warming requests, and
      automatic context compaction;
    * an optional ``providers`` map for the single loopback fixture provider.

    It is pure data: no I/O, no model, no credential. It never emits a top-level
    ``model``/``default_agent`` (the model is per-role and explicit) and never
    emits a V1 config key.
    """
    if not isinstance(configuration, RoleConfiguration):
        raise ServiceConfigError("config_type", _MSG_CONFIG_TYPE)

    base = build_opencode_config(configuration)

    allowed_providers = sorted({entry.model.provider_id for entry in configuration.roles})
    provider_policies: list[dict[str, str]] = [
        {"action": "provider.use", "resource": "*", "effect": "deny"}
    ]
    for provider_id in allowed_providers:
        provider_policies.append(
            {"action": "provider.use", "resource": provider_id, "effect": "allow"}
        )

    config: dict[str, Any] = dict(base)
    config["policies"] = provider_policies + list(base["policies"])
    config["update"] = "disable"
    config["warming"] = False
    config["compaction"] = {"auto": False}

    if fixture_provider is not None:
        if not isinstance(fixture_provider, LoopbackFixtureProvider):
            raise ServiceConfigError("fixture_type", _MSG_FIXTURE)
        providers = dict(config.get("providers") or {})
        providers[fixture_provider.provider_id] = fixture_provider.to_config()
        config["providers"] = providers
        config["policies"] = provider_policies + [
            {"action": "provider.use", "resource": fixture_provider.provider_id, "effect": "allow"}
        ] + list(base["policies"])

    return config


def service_config_bytes(
    configuration: RoleConfiguration,
    *,
    fixture_provider: LoopbackFixtureProvider | None = None,
) -> bytes:
    """Canonical JSON bytes of :func:`build_service_config` (deterministic)."""
    return canonical_json(
        build_service_config(configuration, fixture_provider=fixture_provider)
    )


# --------------------------------------------------------------------------- #
# Public value objects (no secret in any field or repr)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ProjectLocation:
    """A clean, app-owned session location for one application project.

    ``app_project_id`` is the application's opaque project id. OpenCode derives
    its **own** ``projectID`` (a content hash) for a session; the two are *not*
    the same and this manager does not pretend otherwise — a later repository
    persists the exact mapping. ``directory`` is a private owned subdirectory
    that is deliberately **separate** from the real project directory.
    """

    app_project_id: str
    directory: str
    location_id: str
    root: str


@dataclass(frozen=True)
class ServiceDescriptor:
    """A safe, secret-free description of a running/owned instance."""

    url: str
    pid: int
    version: str
    binary: str
    root: str
    password_file: str
    home: str
    xdg_config_home: str
    xdg_data_home: str
    xdg_state_home: str
    xdg_cache_home: str
    cwd: str
    provider_env_names: tuple[str, ...] = ()


@dataclass(frozen=True)
class ServiceConnection:
    """Explicit connection parameters for a future client (never the secret)."""

    base_url: str
    username: str
    password_file: str


@dataclass(frozen=True)
class ServiceStatus:
    """The result of :meth:`OpenCodeServiceManager.status` (never raises)."""

    state: str  # running | stopped | foreign | unowned | unsafe | absent
    running: bool
    pid: int | None = None
    url: str | None = None
    version: str | None = None
    root: str = ""


# --------------------------------------------------------------------------- #
# The manager
# --------------------------------------------------------------------------- #


class OpenCodeServiceManager:
    """Owns one app-owned OpenCode V2 service for one explicit ``data_dir``.

    The public lifecycle is deliberately explicit: :meth:`start`, :meth:`status`,
    :meth:`stop`, :meth:`restart`, plus :meth:`location_for`. There is **no**
    discovery of, or interaction with, the ordinary shared coding service, and
    **no** model/provider call anywhere in this class.
    """

    def __init__(
        self,
        *,
        data_dir: str | os.PathLike[str],
        configuration: RoleConfiguration,
        binary: str | os.PathLike[str] | None = None,
        port: int | None = None,
        provider_env: Mapping[str, str] | None = None,
        fixture_provider: LoopbackFixtureProvider | None = None,
        start_timeout: float = DEFAULT_START_TIMEOUT_S,
        stop_timeout: float = DEFAULT_STOP_TIMEOUT_S,
        lock_timeout: float = DEFAULT_LOCK_TIMEOUT_S,
        http_get: Callable[..., Any] | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not isinstance(configuration, RoleConfiguration):
            raise ServiceConfigError("config_type", _MSG_CONFIG_TYPE)

        self.data_dir = Path(os.fspath(data_dir)).expanduser()
        if not self.data_dir.is_absolute():
            raise ServiceConfigError("data_dir", _MSG_DATA_DIR)
        self.root = self.data_dir / SERVICE_ROOT_DIRNAME
        self.lock_file = self.data_dir / f"{SERVICE_ROOT_DIRNAME}.lock"

        self.configuration = configuration
        self.fixture_provider = fixture_provider
        self.config = build_service_config(configuration, fixture_provider=fixture_provider)
        self.config_fingerprint = _sha256_hex(canonical_json(self.config))
        self.provider_env = self._validate_provider_env(provider_env)

        self._binary = os.fspath(binary) if binary is not None else None
        self._port = port
        self.start_timeout = float(start_timeout)
        self.stop_timeout = float(stop_timeout)
        self.lock_timeout = float(lock_timeout)
        self._http_get = http_get or _default_http_json
        self._sleep = sleep
        # Holds this process's own Popen only to keep the child identity stable
        # for a same-process stop/reap; signalling never relies on it.
        self._proc: subprocess.Popen[bytes] | None = None

        self.home = self.root / "home"
        self.config_home = self.root / "config"
        self.data_home = self.root / "data"
        self.state_home = self.root / "state"
        self.cache_home = self.root / "cache"
        self.cwd = self.root / "cwd"
        self.run = self.root / "run"
        self.tmp = self.run / "tmp"
        self.projects_dir = self.root / "projects"

        self.marker = self.root / MARKER_NAME
        self.pid_record = self.run / PID_RECORD_NAME
        self.port_file = self.run / "port"
        self.secret_file = self.run / "password"
        self.descriptor_file = self.run / DESCRIPTOR_NAME
        self.stdout_file = self.run / "serve.out"
        self.stderr_file = self.run / "serve.err"
        self.config_file = self.config_home / "opencode" / "opencode.json"

    # -- input validation ------------------------------------------------- #

    @staticmethod
    def _validate_provider_env(provider_env: Mapping[str, str] | None) -> dict[str, str]:
        if provider_env is None:
            return {}
        if not isinstance(provider_env, Mapping):
            raise ServiceConfigError("provider_env", _MSG_PROVIDER_ENV)
        out: dict[str, str] = {}
        for name, value in provider_env.items():
            if not isinstance(name, str) or not _ENV_NAME_RE.match(name):
                raise ServiceConfigError("provider_env", _MSG_PROVIDER_ENV)
            if name not in KNOWN_PROVIDER_ENV_NAMES:
                raise ServiceConfigError("provider_env", _MSG_PROVIDER_ENV)
            if not isinstance(value, str) or value == "":
                raise ServiceConfigError("provider_env", _MSG_PROVIDER_ENV)
            out[name] = value
        return out

    # -- lock ------------------------------------------------------------- #

    @contextmanager
    def _locked(self) -> Iterator[None]:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(str(self.lock_file), os.O_CREAT | os.O_RDWR, 0o600)
        except OSError as exc:
            raise ServiceLockError("lock_open", _MSG_LOCK_ERROR) from exc
        deadline = time.monotonic() + self.lock_timeout
        try:
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError as exc:
                    if exc.errno not in (errno.EACCES, errno.EAGAIN):
                        raise ServiceLockError("lock_error", _MSG_LOCK_ERROR) from exc
                    if time.monotonic() >= deadline:
                        raise ServiceLockError("lock_timeout", _MSG_LOCK_TIMEOUT)
                    self._sleep(0.05)
            yield
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    # -- safety / ownership ---------------------------------------------- #

    def _assert_root_safe(self) -> None:
        """Refuse a symlinked, dangerous, or configurably-inherited root.

        Runs before anything is created. It rejects a symlinked root, ``/``,
        ``/tmp``, ``/home``, ``/root`` and the user's home, any overlap with a
        protected location, any ancestor above the root that contains unowned
        OpenCode config/instructions/plugin directories, and the machine/system
        configuration directories a fresh server would read.
        """
        if self.root.is_symlink():
            raise ServiceOwnershipError("root_symlink", _MSG_ROOT_SYMLINK)
        resolved = os.path.realpath(self.root)
        if resolved in {"/", "/tmp", "/home", "/root"} or resolved == os.path.realpath(
            os.path.expanduser("~")
        ):
            raise ServiceOwnershipError("root_unsafe", _MSG_ROOT_UNSAFE)

        protected = [
            os.path.join(os.path.expanduser("~"), ".config"),
            os.path.join(os.path.expanduser("~"), ".local"),
            os.path.join(os.path.expanduser("~"), ".cache"),
            "/etc",
            "/usr",
            "/var",
        ]
        for entry in protected:
            if _paths_overlap(resolved, os.path.realpath(entry)):
                raise ServiceOwnershipError("root_unsafe", _MSG_ROOT_UNSAFE)

        # Ancestors above the root must not expose unowned config/instructions.
        ancestor = os.path.dirname(resolved)
        while True:
            for name in _INHERITED_NAMES:
                if os.path.lexists(os.path.join(ancestor, name)):
                    raise ServiceOwnershipError("root_inherited", _MSG_ROOT_INHERITED)
            parent = os.path.dirname(ancestor)
            if parent == ancestor:
                break
            ancestor = parent

        for system_dir in _SYSTEM_CONFIG_DIRS:
            if os.path.lexists(system_dir):
                raise ServiceOwnershipError("root_inherited", _MSG_ROOT_INHERITED)

    def _marker_valid(self) -> bool:
        if not os.path.isfile(self.marker) or self.marker.is_symlink():
            return False
        try:
            doc = _read_json(self.marker)
        except (OSError, ValueError):
            return False
        return (
            isinstance(doc, dict)
            and doc.get("kind") == MARKER_KIND
            and doc.get("root") == os.path.realpath(self.root)
            and doc.get("ownerUid") == os.getuid()
        )

    def is_owned_root(self) -> bool:
        """True when the app-owned root exists with a valid ownership marker."""
        return self.root.is_dir() and not self.root.is_symlink() and self._marker_valid()

    def _ensure_owned_root(self) -> None:
        if self.root.is_symlink():
            raise ServiceOwnershipError("root_symlink", _MSG_ROOT_SYMLINK)
        if self.root.exists():
            if not self.root.is_dir():
                raise ServiceOwnershipError("root_not_dir", _MSG_ROOT_NOT_DIR)
            if any(self.root.iterdir()):
                if not self._marker_valid():
                    raise ServiceOwnershipError("root_unowned", _MSG_ROOT_UNOWNED)
                return
            self._write_marker()
            return
        os.makedirs(self.root, mode=0o700, exist_ok=True)
        os.chmod(self.root, 0o700)
        self._write_marker()

    def _write_marker(self) -> None:
        doc = {
            "kind": MARKER_KIND,
            "markerVersion": MARKER_VERSION,
            "root": os.path.realpath(self.root),
            "ownerUid": os.getuid(),
            "instanceId": secrets.token_hex(16),
            "createdAtMs": int(time.time() * 1000),
        }
        _atomic_write(self.marker, canonical_json(doc) + b"\n", 0o600)

    def _create_private_dirs(self) -> None:
        for directory in (
            self.home,
            self.config_home,
            self.data_home,
            self.state_home,
            self.cache_home,
            self.cwd,
            self.run,
            self.tmp,
            self.projects_dir,
        ):
            os.makedirs(directory, mode=0o700, exist_ok=True)
            os.chmod(directory, 0o700)

    # -- process identity ------------------------------------------------- #

    def _read_pid_record(self) -> dict[str, Any] | None:
        if not os.path.isfile(self.pid_record):
            return None
        try:
            doc = _read_json(self.pid_record)
        except (OSError, ValueError):
            return None
        return doc if isinstance(doc, dict) else None

    def _secret_bytes(self) -> bytes | None:
        if not os.path.isfile(self.secret_file):
            return None
        try:
            with open(str(self.secret_file), "rb") as handle:
                return handle.read()
        except OSError:
            return None

    def _identity_matches(self, pid: int, record: Mapping[str, Any]) -> bool:
        """Prove ``pid`` is exactly the recorded, owned instance."""
        try:
            rec_pid = int(record["pid"])
        except (KeyError, TypeError, ValueError):
            return False
        if rec_pid != pid or not _pid_alive(pid):
            return False
        if _proc_starttime(pid) != record.get("starttime"):
            return False
        if _proc_exe(pid) != record.get("exe"):
            return False
        if _proc_cwd(pid) != record.get("cwd"):
            return False
        environ = _proc_environ(pid)
        if environ.get("HOME") != record.get("home"):
            return False
        secret = self._secret_bytes()
        if secret is None:
            return False
        recorded = environ.get("OPENCODE_PASSWORD", "")
        if _sha256_hex(recorded.encode("utf-8")) != record.get("passwordSha256"):
            return False
        if _sha256_hex(secret) != record.get("passwordSha256"):
            return False
        return True

    def _process_state(self) -> str:
        """One of ``none`` | ``stopped`` | ``ours`` | ``foreign``."""
        record = self._read_pid_record()
        if record is None:
            return "none"
        try:
            pid = int(record["pid"])
        except (KeyError, TypeError, ValueError):
            return "foreign"
        if self._identity_matches(pid, record):
            return "ours"
        if _pid_alive(pid):
            return "foreign"
        return "stopped"

    def _signal_owned(self, pid: int, record: Mapping[str, Any], sig: int) -> None:
        """Send ``sig`` only after re-proving identity (pidfd when available)."""
        if not self._identity_matches(pid, record):
            raise ServiceProcessError("foreign_process", _MSG_FOREIGN_PROCESS)
        pidfd: int | None = None
        try:
            pidfd = os.pidfd_open(pid, 0)  # type: ignore[attr-defined]
        except (AttributeError, OSError):
            pidfd = None
        try:
            if pidfd is not None and hasattr(signal, "pidfd_send_signal"):
                signal.pidfd_send_signal(pidfd, sig)  # type: ignore[attr-defined]
            else:
                os.kill(pid, sig)
        finally:
            if pidfd is not None:
                try:
                    os.close(pidfd)
                except OSError:
                    pass

    # -- environment / binary -------------------------------------------- #

    def _resolve_binary(self) -> str:
        candidate = self._binary if self._binary is not None else shutil.which("opencode")
        if not candidate:
            raise ServiceBinaryError("binary_missing", _MSG_BINARY)
        resolved = os.path.realpath(candidate)
        if not os.path.isfile(resolved) or not os.access(resolved, os.X_OK):
            raise ServiceBinaryError("binary_missing", _MSG_BINARY)
        return candidate

    def _binary_env(self) -> dict[str, str]:
        binary_dir = os.path.dirname(os.path.realpath(self._resolve_binary()))
        return {
            "PATH": f"{binary_dir}:/usr/local/bin:/usr/bin:/bin",
            "HOME": str(self.home),
            "TMPDIR": str(self.tmp),
            "LANG": "C.UTF-8",
        }

    def _verify_binary_version(self, binary: str) -> str:
        try:
            proc = subprocess.run(  # noqa: S603 - explicit argv, shell=False
                [binary, "--version"],
                capture_output=True,
                text=True,
                env=self._binary_env(),
                cwd=str(self.cwd) if self.cwd.is_dir() else str(self.data_dir),
                timeout=20,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise ServiceBinaryError("binary_version", _MSG_BINARY) from exc
        tokens = (proc.stdout or "").split()
        raw = tokens[-1] if tokens else ""
        version = raw[1:] if raw[:1] in {"v", "V"} else raw
        if not _VERSION_RE.match(version) or version != OPENCODE_PINNED_VERSION:
            raise ServiceVersionError("binary_version", _MSG_BINARY_VERSION)
        return version

    def _child_env(self, password: str) -> dict[str, str]:
        """The allowlisted child environment.

        Everything inherited from the caller is dropped: only ``PATH``, the
        private ``HOME``/``XDG_*``/``TMPDIR``, the per-run ``OPENCODE_PASSWORD``,
        ``LANG``, and the explicitly supplied **known** provider credential
        names are present. Inherited ``OPENCODE_*``, ``OPENCHAMBER_*`` and
        ``AI_AGENT`` variables (including the ordinary service password) are
        therefore absent by construction.
        """
        env = {
            "PATH": f"{os.path.dirname(os.path.realpath(self._resolve_binary()))}:/usr/local/bin:/usr/bin:/bin",
            "HOME": str(self.home),
            "XDG_CONFIG_HOME": str(self.config_home),
            "XDG_DATA_HOME": str(self.data_home),
            "XDG_STATE_HOME": str(self.state_home),
            "XDG_CACHE_HOME": str(self.cache_home),
            "TMPDIR": str(self.tmp),
            "LANG": "C.UTF-8",
            "OPENCODE_PASSWORD": password,
        }
        for name, value in self.provider_env.items():
            env[name] = value
        return env

    # -- config / secret -------------------------------------------------- #

    def _write_config(self) -> None:
        self.config_file.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(self.config_file.parent, 0o700)
        _atomic_write(self.config_file, canonical_json(self.config) + b"\n", 0o600)

    def _write_secret(self, password: str) -> None:
        _atomic_write(self.secret_file, password.encode("utf-8"), 0o600)

    def read_password(self) -> str:
        """Return the per-run HTTP Basic secret (callers must not log it)."""
        data = self._secret_bytes()
        if data is None:
            raise ServiceOwnershipError("no_secret", _MSG_NO_SECRET)
        return data.decode("utf-8")

    def _base_url(self) -> str | None:
        if not os.path.isfile(self.port_file):
            return None
        try:
            with open(str(self.port_file), "rb") as handle:
                port = int(handle.read().decode("ascii").strip())
        except (OSError, ValueError):
            return None
        return f"http://{SERVICE_HOST}:{port}"

    def _fetch(self, path: str, timeout: float = _PROBE_TIMEOUT_S) -> Any:
        url = self._base_url()
        if url is None:
            raise ServiceProcessError("not_running", _MSG_PROBE)
        return self._http_get(url, SERVICE_USERNAME, self.read_password(), timeout, path)

    # -- spawn / readiness ------------------------------------------------ #

    def _spawn(self, binary: str, port: int, password: str) -> subprocess.Popen[bytes]:
        env = self._child_env(password)
        with open(str(self.stdout_file), "wb") as out, open(str(self.stderr_file), "wb") as err:
            return subprocess.Popen(  # noqa: S603 - explicit argv, shell=False
                [binary, "serve", "--hostname", SERVICE_HOST, "--port", str(port)],
                cwd=str(self.cwd),
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=err,
                start_new_session=True,
                close_fds=True,
            )

    def _capture_identity(self, pid: int, port: int, password: str) -> dict[str, Any]:
        return {
            "recordVersion": 1,
            "pid": pid,
            "starttime": _proc_starttime(pid),
            "exe": _proc_exe(pid),
            "cwd": _proc_cwd(pid),
            "home": os.path.realpath(self.home),
            "port": port,
            "passwordSha256": _sha256_hex(password.encode("utf-8")),
            "configFingerprint": self.config_fingerprint,
            "providerEnvNames": sorted(self.provider_env),
        }

    def _await_ready(
        self, proc: subprocess.Popen[bytes], port: int, password: str
    ) -> dict[str, Any]:
        url = f"http://{SERVICE_HOST}:{port}"
        deadline = time.monotonic() + self.start_timeout
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                raise ServiceStartError("server_exited", _MSG_START_EXITED)
            try:
                info = self._http_get(url, SERVICE_USERNAME, password, _PROBE_TIMEOUT_S, "/api/info")
            except Exception:  # bounded readiness: not-yet-up is expected
                info = None
            if isinstance(info, dict) and info.get("version"):
                return info
            self._sleep(_PROBE_INTERVAL_S)
        raise ServiceStartError("start_timeout", _MSG_START_FAILED)

    def _terminate_child(self, proc: subprocess.Popen[bytes]) -> None:
        """Terminate our own direct child (safe: the Popen object pins it)."""
        try:
            proc.terminate()
        except OSError:
            pass
        try:
            proc.wait(timeout=max(self.stop_timeout, 1.0))
            return
        except subprocess.TimeoutExpired:
            pass
        try:
            proc.kill()
        except OSError:
            pass
        try:
            proc.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            pass

    def _write_pid_record(self, record: Mapping[str, Any]) -> None:
        _atomic_write(self.pid_record, canonical_json(dict(record)) + b"\n", 0o600)

    def _write_descriptor(self, record: Mapping[str, Any], version: str) -> None:
        doc = {
            "descriptorVersion": DESCRIPTOR_VERSION,
            "kind": DESCRIPTOR_KIND,
            "binary": self._resolve_binary(),
            "opencodeVersion": version,
            "url": self._base_url(),
            "pid": int(record["pid"]),
            "auth": {
                "scheme": "http-basic",
                "username": SERVICE_USERNAME,
                "passwordFile": str(self.secret_file),
            },
            "dirs": {
                "root": str(self.root),
                "home": str(self.home),
                "xdgConfigHome": str(self.config_home),
                "xdgDataHome": str(self.data_home),
                "xdgStateHome": str(self.state_home),
                "xdgCacheHome": str(self.cache_home),
                "cwd": str(self.cwd),
            },
            "providerEnvNames": sorted(self.provider_env),
            "note": (
                "Read the Basic password from passwordFile; never print it. "
                "This instance writes no standard service registration."
            ),
        }
        _atomic_write(self.descriptor_file, canonical_json(doc) + b"\n", 0o600)

    def _clear_run_records(self) -> None:
        for path in (self.pid_record, self.port_file, self.secret_file, self.descriptor_file):
            try:
                os.unlink(str(path))
            except OSError:
                pass

    # -- public lifecycle ------------------------------------------------- #

    def start(self) -> ServiceStatus:
        """Start the owned service, or return it idempotently if already running.

        Concurrent/idempotent: a second ``start`` with the **same** configuration
        returns the running instance; a start with a **different** configuration
        while an instance runs is refused. A foreign or stale record never
        causes a signal to be sent.
        """
        self._assert_root_safe()
        with self._locked():
            return self._start_locked()

    def _start_locked(self) -> ServiceStatus:
        state = self._process_state()
        if state == "ours":
            record = self._read_pid_record() or {}
            if record.get("configFingerprint") != self.config_fingerprint:
                raise ServiceConfigError("conflicting_config", _MSG_CONFLICTING_CONFIG)
            return self._running_status(inspect=False)
        if state == "foreign":
            raise ServiceProcessError("foreign_process", _MSG_STOP_FOREIGN)

        self._ensure_owned_root()
        self._create_private_dirs()

        binary = self._resolve_binary()
        self._verify_binary_version(binary)
        self._write_config()

        port = int(self._port) if self._port else _free_loopback_port()
        password = secrets.token_urlsafe(32)
        self._write_secret(password)
        _atomic_write(self.port_file, str(port).encode("ascii"), 0o600)

        proc = self._spawn(binary, port, password)
        self._proc = proc
        try:
            info = self._await_ready(proc, port, password)
        except Exception:
            self._terminate_child(proc)
            self._proc = None
            self._clear_run_records()
            raise
        version = str(info.get("version", ""))
        if version != OPENCODE_PINNED_VERSION:
            self._terminate_child(proc)
            self._proc = None
            self._clear_run_records()
            raise ServiceVersionError("server_version", _MSG_SERVER_VERSION)

        record = self._capture_identity(proc.pid, port, password)
        record["version"] = version
        self._write_pid_record(record)
        self._write_descriptor(record, version)
        return ServiceStatus(
            state="running",
            running=True,
            pid=proc.pid,
            url=self._base_url(),
            version=version,
            root=str(self.root),
        )

    def stop(self) -> ServiceStatus:
        """Stop **only** the owned instance; always keep the session data.

        There is deliberately no force-cleanup and no deletion of ordinary data
        anywhere in this manager: only the run records are cleared. Sessions,
        config, and the private tree persist.
        """
        self._assert_root_safe()
        with self._locked():
            return self._stop_locked()

    def _stop_locked(self) -> ServiceStatus:
        if not self.root.exists() and not self.root.is_symlink():
            return ServiceStatus(state="absent", running=False, root=str(self.root))
        if self.root.is_symlink():
            raise ServiceOwnershipError("root_symlink", _MSG_ROOT_SYMLINK)
        if not self._marker_valid():
            raise ServiceOwnershipError("root_unowned", _MSG_NOT_OWNED_STOP)

        state = self._process_state()
        if state in {"none", "stopped"}:
            self._clear_run_records()
            self._proc = None
            return ServiceStatus(state="stopped", running=False, root=str(self.root))
        if state == "foreign":
            raise ServiceProcessError("foreign_process", _MSG_STOP_FOREIGN)

        record = self._read_pid_record() or {}
        pid = int(record["pid"])
        self._signal_owned(pid, record, signal.SIGTERM)
        deadline = time.monotonic() + self.stop_timeout
        while _pid_alive(pid) and time.monotonic() < deadline:
            self._sleep(_TERM_POLL_S)
        if _pid_alive(pid):
            self._signal_owned(pid, record, signal.SIGKILL)
            deadline = time.monotonic() + 5.0
            while _pid_alive(pid) and time.monotonic() < deadline:
                self._sleep(_TERM_POLL_S)
        try:
            os.waitpid(pid, os.WNOHANG)
        except (ChildProcessError, OSError):
            pass
        if self._proc is not None:
            try:
                self._proc.poll()  # settle returncode so the Popen can be dropped
            except Exception:  # pragma: no cover - defensive
                pass
            self._proc = None
        self._clear_run_records()
        return ServiceStatus(state="stopped", running=False, root=str(self.root))

    def restart(self) -> ServiceStatus:
        """Replace **only** the owned process, keeping the private data tree."""
        self._assert_root_safe()
        with self._locked():
            self._stop_locked()
            return self._start_locked()

    def status(self) -> ServiceStatus:
        """Report the current state without ever raising or signalling."""
        if not self.root.exists() and not self.root.is_symlink():
            return ServiceStatus(state="absent", running=False, root=str(self.root))
        if self.root.is_symlink() or not self.root.is_dir():
            return ServiceStatus(state="unsafe", running=False, root=str(self.root))
        if not self._marker_valid():
            return ServiceStatus(state="unowned", running=False, root=str(self.root))
        state = self._process_state()
        if state == "ours":
            return self._running_status(inspect=True)
        if state == "foreign":
            return ServiceStatus(state="foreign", running=False, root=str(self.root))
        return ServiceStatus(state="stopped", running=False, root=str(self.root))

    def _running_status(self, *, inspect: bool) -> ServiceStatus:
        record = self._read_pid_record() or {}
        pid = int(record.get("pid", 0)) or None
        version = record.get("version")
        if inspect:
            try:
                info = self._fetch("/api/info")
                if isinstance(info, dict) and info.get("version"):
                    version = str(info["version"])
            except Exception:
                pass
        return ServiceStatus(
            state="running",
            running=True,
            pid=pid,
            url=self._base_url(),
            version=version,
            root=str(self.root),
        )

    # -- locations -------------------------------------------------------- #

    def location_for(self, project_id: str) -> ProjectLocation:
        """Return a clean, owned session location for ``project_id``.

        The application project id is validated as an opaque, traversal-safe
        identifier; the location is a private owned subdirectory under the
        service root, deliberately separate from the real project directory. A
        symlink, an unowned non-empty directory, or a file at the location is
        refused.
        """
        validate_identifier(project_id, "project_id")
        self._assert_root_safe()
        with self._locked():
            self._ensure_owned_root()
            self._create_private_dirs()
            location = self.projects_dir / project_id
            if location.is_symlink():
                raise ServiceLocationError("location_symlink", _MSG_LOCATION_SYMLINK)
            if location.exists():
                if not location.is_dir():
                    raise ServiceLocationError("location_not_dir", _MSG_LOCATION_NOT_DIR)
                if any(location.iterdir()):
                    if not self._location_marker_valid(location, project_id):
                        raise ServiceOwnershipError("location_unowned", _MSG_LOCATION_UNOWNED)
                    doc = _read_json(location / LOCATION_MARKER_NAME)
                    return ProjectLocation(
                        app_project_id=project_id,
                        directory=str(location),
                        location_id=str(doc.get("instanceId", "")),
                        root=str(self.root),
                    )
            else:
                os.makedirs(location, mode=0o700, exist_ok=True)
                os.chmod(location, 0o700)
            return self._write_location_marker(location, project_id)

    def _location_marker_valid(self, location: Path, project_id: str) -> bool:
        marker = location / LOCATION_MARKER_NAME
        if not os.path.isfile(marker) or marker.is_symlink():
            return False
        try:
            doc = _read_json(marker)
        except (OSError, ValueError):
            return False
        return (
            isinstance(doc, dict)
            and doc.get("kind") == LOCATION_MARKER_KIND
            and doc.get("root") == os.path.realpath(location)
            and doc.get("ownerUid") == os.getuid()
            and doc.get("appProjectId") == project_id
        )

    def _write_location_marker(self, location: Path, project_id: str) -> ProjectLocation:
        instance_id = secrets.token_hex(16)
        doc = {
            "kind": LOCATION_MARKER_KIND,
            "markerVersion": LOCATION_MARKER_VERSION,
            "root": os.path.realpath(location),
            "ownerUid": os.getuid(),
            "appProjectId": project_id,
            "instanceId": instance_id,
            "createdAtMs": int(time.time() * 1000),
        }
        _atomic_write(location / LOCATION_MARKER_NAME, canonical_json(doc) + b"\n", 0o600)
        return ProjectLocation(
            app_project_id=project_id,
            directory=str(location),
            location_id=instance_id,
            root=str(self.root),
        )

    # -- safe discovery for a future client ------------------------------ #

    def connection(self) -> ServiceConnection:
        """Explicit connection parameters for a future client (no secret)."""
        url = self._base_url()
        if url is None:
            raise ServiceProcessError("not_running", _MSG_PROBE)
        return ServiceConnection(
            base_url=url, username=SERVICE_USERNAME, password_file=str(self.secret_file)
        )

    def descriptor(self) -> ServiceDescriptor:
        """The safe, secret-free descriptor of the currently recorded instance."""
        if os.path.isfile(self.descriptor_file):
            try:
                doc = _read_json(self.descriptor_file)
            except (OSError, ValueError):
                doc = None
            if isinstance(doc, dict) and doc.get("descriptorVersion") == DESCRIPTOR_VERSION:
                dirs = doc.get("dirs", {})
                url = doc.get("url")
                if isinstance(url, str):
                    return ServiceDescriptor(
                        url=url,
                        pid=int(doc.get("pid", 0)),
                        version=str(doc.get("opencodeVersion", "")),
                        binary=str(doc.get("binary", "")),
                        root=str(dirs.get("root", self.root)),
                        password_file=str(self.secret_file),
                        home=str(dirs.get("home", self.home)),
                        xdg_config_home=str(dirs.get("xdgConfigHome", self.config_home)),
                        xdg_data_home=str(dirs.get("xdgDataHome", self.data_home)),
                        xdg_state_home=str(dirs.get("xdgStateHome", self.state_home)),
                        xdg_cache_home=str(dirs.get("xdgCacheHome", self.cache_home)),
                        cwd=str(dirs.get("cwd", self.cwd)),
                        provider_env_names=tuple(doc.get("providerEnvNames", ())),
                    )
        url = self._base_url() or ""
        record = self._read_pid_record() or {}
        return ServiceDescriptor(
            url=url,
            pid=int(record.get("pid", 0)) or 0,
            version=str(record.get("version", "")),
            binary=str(record.get("exe", "")),
            root=str(self.root),
            password_file=str(self.secret_file),
            home=str(self.home),
            xdg_config_home=str(self.config_home),
            xdg_data_home=str(self.data_home),
            xdg_state_home=str(self.state_home),
            xdg_cache_home=str(self.cache_home),
            cwd=str(self.cwd),
            provider_env_names=tuple(record.get("providerEnvNames", ())),
        )

    def config_sources(self) -> list[Any]:
        """The config *source* list from ``GET /api/config`` (readiness only).

        Used to prove that the running instance reads only this manager's own
        generated config directory and no inherited global/ancestor/system
        document.
        """
        sources = self._fetch("/api/config")
        if not isinstance(sources, list):
            raise ServiceProcessError("bad_config_sources", _MSG_PROBE)
        return sources


__all__ = [
    "DESCRIPTOR_KIND",
    "DESCRIPTOR_VERSION",
    "DEFAULT_LOCK_TIMEOUT_S",
    "DEFAULT_START_TIMEOUT_S",
    "DEFAULT_STOP_TIMEOUT_S",
    "FIXTURE_PROVIDER_APIKEY",
    "FIXTURE_PROVIDER_PACKAGE",
    "KNOWN_PROVIDER_ENV_NAMES",
    "LoopbackFixtureProvider",
    "MARKER_KIND",
    "MARKER_NAME",
    "OPENCODE_PINNED_VERSION",
    "OpenCodeServiceManager",
    "ProjectLocation",
    "SERVICE_HOST",
    "SERVICE_ROOT_DIRNAME",
    "SERVICE_USERNAME",
    "ServiceBinaryError",
    "ServiceConfigError",
    "ServiceConnection",
    "ServiceDescriptor",
    "ServiceError",
    "ServiceLocationError",
    "ServiceLockError",
    "ServiceOwnershipError",
    "ServiceProcessError",
    "ServiceStartError",
    "ServiceStatus",
    "ServiceVersionError",
    "build_service_config",
    "service_config_bytes",
]
