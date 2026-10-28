"""App-owned, version-pinned OpenCode V2 **service manager** (Step 04, task T4).

This module owns the *lifecycle* of one disposable, app-owned OpenCode V2
service: it creates a private, owner-only tree under an **explicit**
``data_dir``, writes the V2 configuration emitted by the integrated pure role
boundary (:mod:`review_article_agent.runtime.roles`), starts a *foreground*
``opencode serve --hostname 127.0.0.1 --port …`` process with a scrubbed
environment, proves the pinned binary/API version, and stops or restarts **only**
a process whose Linux identity it owns.

Authoritative sources (read-only):

* ``docs/implementation/04-agent-runtime.md`` deliverables 1/4 and the handoff
  note ("Document actual API/SDK versions, adapter contracts, …").
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

Trust boundary (repair R1)
--------------------------

The first revision copied the *disposable harness*' blanket protection of the
whole of ``$HOME/.local``, ``$HOME/.config`` and ``$HOME/.cache``. That made the
product's own XDG default data directory
(``~/.local/share/review_article_agent``) unusable. This revision instead:

* allows any **private, explicit** data directory, including the XDG default,
  and protects only the *ordinary* OpenCode locations specifically
  (``~/.config/opencode``, ``~/.local/{share,state}/opencode``,
  ``~/.cache/opencode``, ``~/.opencode``), plus ``/etc``, ``/usr`` and ``/var``;
* treats an inherited directory as a configuration **risk** only when it
  actually carries configuration/instructions/plugins (an ``opencode.json(c)``,
  ``AGENTS.md``, or an ``agents``/``skills``/``plugins``/``commands`` subdir). An
  empty scratch directory such as a user's ``~/.opencode/plan`` contributes
  nothing and is not a blocker;
* ``lstat``-checks every path it manages (``data_dir``, root, ``home``, ``run``,
  ``config``, ``projects``, locations, lock, marker, secret, records) and refuses
  a symlink or a shared/foreign path **before** reading, chmod-ing, writing, or
  signalling — it never follows an internal symlink and never chmods a foreign
  directory;
* requires an existing non-empty root to carry a correct marker
  (``markerVersion`` + canonical ``root`` + ``ownerUid`` + non-empty
  ``instanceId``) whose file is owned by the current user and is not a symlink.

Trust boundary (repair R2)
--------------------------

R1 vetted only the *leaf* of a managed path, so swapping an owned intermediate
directory (``run``/``config``/…) for a symlink could redirect a read, write,
``chmod``, or record deletion to an external file that still looked like an
owner file. R2 proves the *whole* chain from the owned root before every managed
read, write, ``chmod``, or signal, and refuses to trust disk documents over the
manager's own state:

* the secret is read only from an owner-only (no group/other bits) file, and a
  swapped ``run`` chain is refused;
* the server's log files are opened with ``O_NOFOLLOW`` and are refused when
  pre-existing as a symlink, a non-regular entry, a foreign-owner file, or a
  multiply-linked file, so a spawn can never truncate someone else's data;
* ``descriptor()``/``connection()``/an idempotent ``start`` re-prove the live
  owned process, the owned tree/config, and the port-vs-record agreement, and
  derive their values from the manager rather than from a disk document;
* one startup deadline covers the version subprocess, readiness, and the
  post-ready probes; a failed start retains the identity record when the child
  cannot be proven gone; and provider credential names/values are bounded and
  NUL/newline-free before spawn.

What this manager owns and does **not** own
-------------------------------------------

Owned here: the private tree, the ownership marker, the run records, the child
process family, the HTTP Basic secret, and the *lifecycle* verbs ``start``,
``status``, ``stop``, ``restart``, plus ``location_for``. Nothing else is
discovered or started automatically: there is **no** call to OpenCode's shared
service registration and **no** ``opencode service …`` invocation. A caller asks
this manager to start; the manager never hunts the machine for an instance.

Deliberately **not** here: any model or provider call, any prompt/turn
submission, any scientific decision, any approval, and any access to OpenCode's
private database or its ordinary shared-service registration file. The only HTTP
this module performs is a bounded ``GET /api/info`` and ``GET /api/config``; no
session is created here. The connection parameters a future client needs
(loopback URL, Basic user, the mode-0600 password file) are exposed safely — the
secret itself is never placed in a config, a ledger, a log, an exception, or a
``repr``.

Security boundary (do not overclaim)
------------------------------------

OpenCode and this application run as the **same** user, so the environment scrub,
the owner-only tree, the allowlisted child environment, the provider deny
policies, the identity-checked signalling, and the effective-config check are
*lifecycle and configuration containment*, not a same-UID OS sandbox. A caller
that already controls the user could defeat them. The generated
``permissions``/``policies`` are the same **model tool boundary** documented by
:mod:`…runtime.roles`; they are not a proof against a compromised OpenCode
process.
"""

from __future__ import annotations

import base64
import errno
import fcntl
import hashlib
import json
import math
import os
import re
import secrets
import shutil
import signal
import socket
import stat
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
PID_RECORD_VERSION = 1
DESCRIPTOR_NAME = "instance.json"
DESCRIPTOR_VERSION = 1
DESCRIPTOR_KIND = "app-owned-review-agent-opencode-service"

#: Bounded lifecycle deadlines (seconds). Every externally supplied timeout must
#: be a finite number inside these bounds (``NaN``/``inf`` are refused).
DEFAULT_START_TIMEOUT_S = 30.0
DEFAULT_STOP_TIMEOUT_S = 15.0
DEFAULT_LOCK_TIMEOUT_S = 20.0
MIN_LIFECYCLE_TIMEOUT_S = 0.05
MAX_LIFECYCLE_TIMEOUT_S = 600.0
_PROBE_TIMEOUT_S = 3.0
_PROBE_INTERVAL_S = 0.2
_TERM_POLL_S = 0.15

#: Bounds for on-disk records and HTTP bodies, and strict-JSON depth.
MAX_RECORD_BYTES = 262_144
MAX_HTTP_BODY_BYTES = 4_194_304
MAX_JSON_DEPTH = 64

#: Upper bound (bytes) for one explicitly supplied provider credential value.
#: A value is refused **before** spawn when it is empty, contains a NUL, or is
#: longer than this; it is never persisted.
MAX_PROVIDER_ENV_VALUE_BYTES = 8192

#: The (*only*) HTTP paths this manager will request.
_ALLOWED_PROBE_PATHS: frozenset[str] = frozenset({"/api/info", "/api/config"})

#: The (*only*) provider credential environment variable names that may be
#: supplied explicitly and injected into the child. Values are **never** written
#: to the config or the ledger; only the names (and a non-secret name fence) are
#: recorded.
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

#: Configuration/instruction documents that make a directory an inheritance
#: risk. Checked directly in every ancestor and in every inherited source
#: directory.
_INHERITED_DOCS: tuple[str, ...] = ("opencode.json", "opencode.jsonc", "AGENTS.md")

#: Subdirectories that OpenCode loads as agents/skills/plugins/commands. Present
#: in an ancestor or inherited ``.opencode`` they are an inheritance risk.
_INHERITED_SUBDIRS: tuple[str, ...] = (
    "agents",
    "agent",
    "skills",
    "plugins",
    "plugin",
    "commands",
    "command",
)

#: OpenCode's project-source directory name.
_OPENCODE_SOURCE_DIRNAME = ".opencode"

#: Every entry name this manager treats as a configuration/instruction/plugin
#: risk inside a directory it owns (the one exception is the manager's own
#: generated config file, byte-compared).
_RISK_ENTRY_NAMES: frozenset[str] = frozenset(
    _INHERITED_DOCS + (_OPENCODE_SOURCE_DIRNAME,) + _INHERITED_SUBDIRS
)

#: Machine/system configuration directories a fresh server reads regardless of
#: ``HOME``/``XDG_*``. Refused when present (Linux system dir plus the macOS
#: managed directory, which is inert on Linux).
_SYSTEM_CONFIG_DIRS: tuple[str, ...] = (
    "/etc/opencode",
    "/Library/Application Support/opencode",
)

#: Anchored with ``fullmatch`` (not ``$``) so a trailing newline can never be
#: smuggled into a provider credential name.
_ENV_NAME_RE = re.compile(r"[A-Z][A-Z0-9_]*")
_PROVIDER_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_FIXTURE_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")


def _ordinary_opencode_paths() -> tuple[str, ...]:
    """The **ordinary** OpenCode locations the app-owned tree must never touch.

    Deliberately specific rather than a blanket ``$HOME/.local`` block, so the
    product's own XDG default (``~/.local/share/review_article_agent``) remains a
    supported data directory.
    """
    home = os.path.expanduser("~")
    return (
        os.path.join(home, ".config", "opencode"),
        os.path.join(home, ".local", "share", "opencode"),
        os.path.join(home, ".local", "state", "opencode"),
        os.path.join(home, ".cache", "opencode"),
        os.path.join(home, ".opencode"),
    )


# --------------------------------------------------------------------------- #
# Curated, static error messages (never interpolate a secret or external value)
# --------------------------------------------------------------------------- #

_MSG_CONFIG_TYPE = "The service manager requires a validated RoleConfiguration."
_MSG_PROVIDER_ENV = (
    "Provider environment variables must map a known provider credential name "
    "to a non-empty string value."
)
_MSG_DATA_DIR = "The service data directory must be an absolute path."
_MSG_DATA_DIR_UNSAFE = (
    "The service data directory is a symlink, is not a directory, is owned by "
    "another user, or is writable by other users."
)
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
_MSG_DIR_UNSAFE = (
    "An internal service directory is a symlink, is not a directory, or is not "
    "owned by this application; refusing to follow or modify it."
)
_MSG_TREE_DIRTY = (
    "The app-owned tree contains unowned configuration, instructions, or plugin "
    "entries; refusing to start."
)
_MSG_BAD_TIMEOUT = (
    "Lifecycle timeouts must be finite numbers within the supported bounds."
)
_MSG_BAD_PORT = "An explicit port must be an integer between 1 and 65535."
_MSG_BINARY = "No executable opencode binary was found (supply an explicit path)."
_MSG_BINARY_VERSION = (
    "The opencode binary version does not match the pinned version this "
    "application requires; refusing to start."
)
_MSG_SERVER_VERSION = (
    "The running OpenCode server reported a version that does not match the "
    "pinned version; refusing to use it."
)
_MSG_SERVER_PID = (
    "The running OpenCode server did not report the owned child's process id; "
    "refusing to accept it."
)
_MSG_SERVER_CONFIG = (
    "The running OpenCode server's effective configuration is not exactly this "
    "manager's own; refusing to accept it."
)
_MSG_LOCK_TIMEOUT = (
    "Another lifecycle operation holds the singleton lock for this data "
    "directory; timed out without changing state."
)
_MSG_LOCK_ERROR = "The singleton service lock could not be acquired."
_MSG_CONFLICTING_CONFIG = (
    "A service instance is already running for this data directory with a "
    "different configuration or credential context; refusing to start a "
    "conflicting instance (call restart to adopt a new context)."
)
_MSG_FOREIGN_PROCESS = (
    "The recorded process does not match this instance's identity; refusing to "
    "signal it."
)
_MSG_STOP_FOREIGN = (
    "A process is recorded that this manager does not own; refusing to stop it."
)
_MSG_NO_RECORD = "No process record is available for this instance."
_MSG_INVALID_RECORD = (
    "The recorded process state is unreadable or tampered; refusing to treat it "
    "as absent."
)
_MSG_STILL_ALIVE = (
    "The owned process did not exit within the stop deadline; refusing to "
    "report it stopped or to clear its records."
)
_MSG_START_FAILED = "The OpenCode server did not become ready within the deadline."
_MSG_START_EXITED = "The OpenCode server process exited during startup."
_MSG_LOCATION_SYMLINK = "A project location must not be a symbolic link."
_MSG_LOCATION_UNOWNED = (
    "A project location already exists but is not owned by this application; "
    "refusing to use it."
)
_MSG_LOCATION_NOT_DIR = "A project location exists and is not a directory."
_MSG_LOCATION_ABSENT = (
    "No owned project location exists; a read-only lookup never creates one."
)
_MSG_NOT_OWNED_STOP = "The service root is not owned by this application; refusing to stop."
_MSG_PROBE = "The service did not answer a bounded readiness request."
_MSG_PROBE_ORIGIN = "A readiness request was refused (origin, path, or redirect)."
_MSG_PROBE_BODY = "A readiness response was not bounded, strict JSON."
_MSG_FIXTURE = (
    "A fixture provider is accepted only as a loopback OpenAI-compatible "
    "endpoint with a fixed safe package."
)
_MSG_NO_SECRET = "No service secret is recorded for this instance."
_MSG_CONFIG_DRIFT = (
    "The generated configuration no longer matches its recorded bytes; refusing "
    "to write or use a drifted configuration."
)


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
    """A manager input (role configuration, provider env, fixture, knob) is invalid."""


class ServiceOwnershipError(ServiceError):
    """A root/location is unsafe, unowned, or not this application's."""


class ServiceProcessError(ServiceError):
    """A recorded process is foreign, stale, invalid, or not the owned instance."""


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


def _is_within(path: str, root: str) -> bool:
    return path == root or path.startswith(root + os.sep)


def _validate_timeout(name: str, value: object) -> float:
    """Return a finite, bounded timeout or raise a redacted error."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ServiceConfigError(name, _MSG_BAD_TIMEOUT)
    try:
        number = float(value)
    except (OverflowError, ValueError) as exc:
        # e.g. ``float(10 ** 10000)`` overflows; refuse it as a bounded error.
        raise ServiceConfigError(name, _MSG_BAD_TIMEOUT) from exc
    if not math.isfinite(number) or number < MIN_LIFECYCLE_TIMEOUT_S or number > MAX_LIFECYCLE_TIMEOUT_S:
        raise ServiceConfigError(name, _MSG_BAD_TIMEOUT)
    return number


def _validate_port(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ServiceConfigError("port", _MSG_BAD_PORT)
    if value < 1 or value > 65535:
        raise ServiceConfigError("port", _MSG_BAD_PORT)
    return value


def _lstat(path: str) -> os.stat_result | None:
    try:
        return os.lstat(path)
    except OSError:
        return None


def _file_bytes_strict(path: str, *, max_bytes: int) -> bytes | None:
    """Bounded read of a regular, non-symlink file owned by this user."""
    st = _lstat(path)
    if st is None or stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
        return None
    if st.st_uid != os.getuid() or st.st_size > max_bytes:
        return None
    try:
        with open(path, "rb") as handle:
            data = handle.read(max_bytes + 1)
    except OSError:
        return None
    if len(data) > max_bytes:
        return None
    return data


def _dir_has_risk(path: str) -> bool:
    """True when ``path`` carries configuration/instructions/plugins.

    A directory is only a *risk* when it actually contributes config: a config
    document, or an agents/skills/plugins/commands subdirectory. An empty scratch
    directory contributes nothing and is not a blocker.
    """
    st = _lstat(path)
    if st is None or stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        # A missing / non-directory / symlinked entry is suspicious: treat as risk.
        return st is not None
    try:
        names = os.listdir(path)
    except OSError:
        return True
    for name in names:
        if name in _INHERITED_DOCS:
            return True
        if name in _INHERITED_SUBDIRS:
            child = os.path.join(path, name)
            child_st = _lstat(child)
            if child_st is not None and stat.S_ISDIR(child_st.st_mode):
                return True
    return False


def _atomic_write(path: Path, data: bytes, mode: int) -> None:
    """Write ``data`` to ``path`` atomically, owner-only, fsyncing the directory.

    Refuses to follow a symlink at the target, loops over short writes, fsyncs
    the file and the containing directory, and never leaves a temp file behind.
    """
    target = str(path)
    if os.path.lexists(target):
        st = _lstat(target)
        if st is None or stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
            raise OSError(errno.ELOOP, "refusing to replace a non-regular or symlinked path")
    directory = str(path.parent)
    tmp = os.path.join(directory, f".{path.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        view = memoryview(data)
        offset = 0
        while offset < len(view):
            offset += os.write(fd, view[offset:])
        os.fsync(fd)
    finally:
        os.close(fd)
    try:
        os.replace(tmp, target)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    os.chmod(target, mode)
    try:
        dirfd = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(dirfd)
        finally:
            os.close(dirfd)
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# Strict, bounded JSON
# --------------------------------------------------------------------------- #


def _reject_constant(value: str) -> Any:
    raise ValueError(f"non-finite JSON value {value!r} is not allowed")


def _strict_float(text: str) -> float:
    value = float(text)
    if not math.isfinite(value):
        raise ValueError(f"non-finite JSON number {text!r} is not allowed")
    return value


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    seen: set[str] = set()
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in seen:
            raise ValueError(f"duplicate JSON key {key!r} is not allowed")
        seen.add(key)
        result[key] = value
    return result


def _check_json_depth(obj: Any, limit: int) -> None:
    stack: list[tuple[Any, int]] = [(obj, 1)]
    while stack:
        node, depth = stack.pop()
        if isinstance(node, dict):
            if depth > limit:
                raise ValueError("JSON nesting too deep")
            for value in node.values():
                stack.append((value, depth + 1))
        elif isinstance(node, list):
            if depth > limit:
                raise ValueError("JSON nesting too deep")
            for value in node:
                stack.append((value, depth + 1))


def _strict_json(payload: bytes, *, max_depth: int = MAX_JSON_DEPTH) -> Any:
    """Decode bounded ``payload`` as strict JSON, rejecting every ambiguity."""
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("not valid UTF-8") from exc
    try:
        obj = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
            parse_float=_strict_float,
        )
    except RecursionError as exc:
        raise ValueError("JSON nested too deeply") from exc
    _check_json_depth(obj, max_depth)
    return obj


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


def _proc_field(pid: int, index: int) -> str | None:
    try:
        with open(f"/proc/{pid}/stat", "rb") as handle:
            raw = handle.read().decode("utf-8", "replace")
    except OSError:
        return None
    close = raw.rfind(")")
    if close < 0:
        return None
    fields = raw[close + 1 :].split()
    # After "pid (comm) " the fields begin at ``state`` (overall field 3).
    if len(fields) <= index:
        return None
    return fields[index]


def _proc_starttime(pid: int) -> str | None:
    return _proc_field(pid, 19)  # overall field 22


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


# --------------------------------------------------------------------------- #
# Bounded, proxy-free, redirect-free loopback HTTP
# --------------------------------------------------------------------------- #


class _RefuseRedirects(urllib.request.HTTPRedirectHandler):
    """Refuse every redirect: a redirect must never carry the Basic secret."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


def _build_loopback_opener() -> urllib.request.OpenerDirector:
    # An empty ProxyHandler disables environment proxies entirely, so the Basic
    # credential can never be forwarded to a proxy.
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        _RefuseRedirects(),
    )


_LOOPBACK_OPENER = _build_loopback_opener()


def _validate_probe_url(url: str, path: str) -> str:
    """Return ``url + path`` only for an exact loopback origin and known path."""
    if not isinstance(url, str) or not isinstance(path, str) or path not in _ALLOWED_PROBE_PATHS:
        raise ServiceProcessError("probe_path", _MSG_PROBE_ORIGIN)
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "http" or parsed.hostname != SERVICE_HOST:
        raise ServiceProcessError("probe_origin", _MSG_PROBE_ORIGIN)
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ServiceProcessError("probe_origin", _MSG_PROBE_ORIGIN)
    try:
        port = parsed.port
    except ValueError as exc:
        raise ServiceProcessError("probe_origin", _MSG_PROBE_ORIGIN) from exc
    if port is None or not (1 <= port <= 65535):
        raise ServiceProcessError("probe_origin", _MSG_PROBE_ORIGIN)
    if parsed.path not in ("", "/"):
        raise ServiceProcessError("probe_origin", _MSG_PROBE_ORIGIN)
    return f"http://{SERVICE_HOST}:{port}{path}"


def _default_http_json(
    url: str,
    username: str,
    password: str,
    timeout: float,
    path: str = "/api/info",
) -> Any:
    """Bounded, proxy-free, redirect-free stdlib GET returning strict JSON."""
    target = _validate_probe_url(url, path)
    if not math.isfinite(float(timeout)) or float(timeout) <= 0:
        raise ServiceProcessError("probe_timeout", _MSG_PROBE)
    request = urllib.request.Request(target)
    token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
    request.add_header("Authorization", "Basic " + token)
    try:
        with _LOOPBACK_OPENER.open(request, timeout=float(timeout)) as response:  # noqa: S310
            if response.geturl() != target:
                raise ServiceProcessError("probe_redirect", _MSG_PROBE_ORIGIN)
            body = response.read(MAX_HTTP_BODY_BYTES + 1)
    except urllib.error.HTTPError as exc:
        exc.close()
        raise
    except urllib.error.URLError:
        raise
    if len(body) > MAX_HTTP_BODY_BYTES:
        raise ServiceProcessError("probe_body", _MSG_PROBE_BODY)
    try:
        return _strict_json(body)
    except ValueError as exc:
        raise ServiceProcessError("probe_json", _MSG_PROBE_BODY) from exc


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
        if self.provider_id in {
            "opencode",
            "openrouter",
            "openai",
            "anthropic",
        }:
            # Never shadow a built-in/catalog provider id.
            raise ServiceConfigError("fixture_provider_id", _MSG_FIXTURE)
        if not isinstance(self.base_url, str):
            raise ServiceConfigError("fixture_provider_url", _MSG_FIXTURE)
        parsed = urllib.parse.urlsplit(self.base_url)
        if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise ServiceConfigError("fixture_provider_url", _MSG_FIXTURE)
        if parsed.username or parsed.password:
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
        """The V2 ``providers.<id>`` object (fixed package, no real credential)."""
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
    if fixture_provider is not None:
        if not isinstance(fixture_provider, LoopbackFixtureProvider):
            raise ServiceConfigError("fixture_type", _MSG_FIXTURE)
        if fixture_provider.provider_id not in allowed_providers:
            provider_policies.append(
                {
                    "action": "provider.use",
                    "resource": fixture_provider.provider_id,
                    "effect": "allow",
                }
            )

    config: dict[str, Any] = dict(base)
    config["policies"] = provider_policies + list(base["policies"])
    config["update"] = "disable"
    config["warming"] = False
    config["compaction"] = {"auto": False}

    if fixture_provider is not None:
        config["providers"] = {fixture_provider.provider_id: fixture_provider.to_config()}

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

    state: str  # running | stopped | foreign | invalid | unowned | unsafe | absent
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
        # The exact bytes are the source of truth; the fingerprint is derived
        # from them, never from a mutable dict. ``config_bytes`` is recomputed
        # and byte-compared at every write.
        self._config_bytes = canonical_json(
            build_service_config(configuration, fixture_provider=fixture_provider)
        )
        # The exact bytes written to disk (canonical JSON plus a trailing newline).
        self._config_disk_bytes = self._config_bytes + b"\n"
        self.config_fingerprint = _sha256_hex(self._config_bytes)
        self.provider_env = self._validate_provider_env(provider_env)
        # A non-secret fence over the config plus the *names* of injected provider
        # credentials. It lets a repeated start detect a changed credential
        # context without ever persisting a secret value.
        self.instance_fence = _sha256_hex(
            self.config_fingerprint.encode("ascii")
            + b"\x00"
            + "\n".join(sorted(self.provider_env)).encode("utf-8")
        )

        self._binary = os.fspath(binary) if binary is not None else None
        self._port = _validate_port(port)
        self.start_timeout = _validate_timeout("start_timeout", start_timeout)
        self.stop_timeout = _validate_timeout("stop_timeout", stop_timeout)
        self.lock_timeout = _validate_timeout("lock_timeout", lock_timeout)
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

    @property
    def config_bytes(self) -> bytes:
        """The exact canonical config bytes (source of truth, immutable)."""
        return self._config_bytes

    @staticmethod
    def _validate_provider_env(provider_env: Mapping[str, str] | None) -> dict[str, str]:
        if provider_env is None:
            return {}
        if not isinstance(provider_env, Mapping):
            raise ServiceConfigError("provider_env", _MSG_PROVIDER_ENV)
        out: dict[str, str] = {}
        for name, value in provider_env.items():
            # ``fullmatch`` (never ``match`` with ``$``) so a trailing newline is
            # refused rather than accepted as an allowlisted name.
            if not isinstance(name, str) or _ENV_NAME_RE.fullmatch(name) is None:
                raise ServiceConfigError("provider_env", _MSG_PROVIDER_ENV)
            if name not in KNOWN_PROVIDER_ENV_NAMES:
                raise ServiceConfigError("provider_env", _MSG_PROVIDER_ENV)
            if not isinstance(value, str) or value == "":
                raise ServiceConfigError("provider_env", _MSG_PROVIDER_ENV)
            # Reject an invalid env value *before* spawn: a NUL byte cannot be
            # passed to ``execve`` and an unbounded value is not a credential.
            if "\x00" in value or len(value.encode("utf-8")) > MAX_PROVIDER_ENV_VALUE_BYTES:
                raise ServiceConfigError("provider_env", _MSG_PROVIDER_ENV)
            out[name] = value
        return out

    def _expected_config_bytes(self) -> bytes:
        """Recompute the config from stored inputs (never a mutable cache)."""
        return canonical_json(
            build_service_config(self.configuration, fixture_provider=self.fixture_provider)
        )

    def _expected_effective_config(self) -> dict[str, Any]:
        return json.loads(self._config_bytes)

    # -- data dir / lock -------------------------------------------------- #

    def _prepare_data_dir(self) -> None:
        """Create/verify the explicit data dir: no symlink, owned, private."""
        target = str(self.data_dir)
        existing = _lstat(target)
        if existing is None:
            os.makedirs(target, mode=0o700, exist_ok=True)
            existing = _lstat(target)
        if existing is None:
            raise ServiceOwnershipError("data_dir", _MSG_DATA_DIR_UNSAFE)
        if (
            stat.S_ISLNK(existing.st_mode)
            or not stat.S_ISDIR(existing.st_mode)
            or existing.st_uid != os.getuid()
            or (existing.st_mode & 0o022)
        ):
            raise ServiceOwnershipError("data_dir", _MSG_DATA_DIR_UNSAFE)

    @contextmanager
    def _locked(self) -> Iterator[None]:
        flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(str(self.lock_file), flags, 0o600)
        except OSError as exc:
            raise ServiceLockError("lock_open", _MSG_LOCK_ERROR) from exc
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid():
                raise ServiceLockError("lock_open", _MSG_LOCK_ERROR)
            deadline = time.monotonic() + self.lock_timeout
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError as exc:
                    if exc.errno not in (errno.EACCES, errno.EAGAIN):
                        raise ServiceLockError("lock_error", _MSG_LOCK_ERROR) from exc
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise ServiceLockError("lock_timeout", _MSG_LOCK_TIMEOUT)
                    self._sleep(min(0.05, remaining))
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
        ``/tmp``, ``/home``, ``/root`` and the user's home, any overlap with an
        *ordinary OpenCode* location or a system directory, any ancestor above
        the root that actually carries unowned configuration/instructions/plugin
        entries, and the machine/system configuration directories a fresh server
        would read.
        """
        if self.root.is_symlink():
            raise ServiceOwnershipError("root_symlink", _MSG_ROOT_SYMLINK)
        data_st = _lstat(str(self.data_dir))
        if data_st is not None and stat.S_ISLNK(data_st.st_mode):
            raise ServiceOwnershipError("data_dir", _MSG_DATA_DIR_UNSAFE)

        # The whole ancestor chain of the (explicit) data directory must be
        # symlink-free. This is a deliberate fail-closed choice: a symlinked
        # ancestor would let the tree's real location differ from the path the
        # caller believes they are using (a foreign/shared path).
        probe = str(self.data_dir)
        while True:
            parent = os.path.dirname(probe)
            if parent == probe:
                break
            if os.path.islink(parent):
                raise ServiceOwnershipError("ancestor_symlink", _MSG_ROOT_UNSAFE)
            probe = parent

        resolved = os.path.realpath(self.root)
        if resolved in {"/", "/tmp", "/home", "/root"} or resolved == os.path.realpath(
            os.path.expanduser("~")
        ):
            raise ServiceOwnershipError("root_unsafe", _MSG_ROOT_UNSAFE)

        protected = list(_ordinary_opencode_paths()) + ["/etc", "/usr", "/var"]
        for entry in protected:
            if _paths_overlap(resolved, os.path.realpath(entry)):
                raise ServiceOwnershipError("root_unsafe", _MSG_ROOT_UNSAFE)

        # Ancestors above the root must not expose unowned config/instructions.
        ancestor = os.path.dirname(resolved)
        while True:
            for name in _INHERITED_DOCS:
                if os.path.lexists(os.path.join(ancestor, name)):
                    raise ServiceOwnershipError("root_inherited", _MSG_ROOT_INHERITED)
            source = os.path.join(ancestor, _OPENCODE_SOURCE_DIRNAME)
            if os.path.lexists(source) and _dir_has_risk(source):
                raise ServiceOwnershipError("root_inherited", _MSG_ROOT_INHERITED)
            parent = os.path.dirname(ancestor)
            if parent == ancestor:
                break
            ancestor = parent

        for system_dir in _SYSTEM_CONFIG_DIRS:
            if os.path.lexists(system_dir):
                raise ServiceOwnershipError("root_inherited", _MSG_ROOT_INHERITED)

    # -- managed-path safety (repair R2) ---------------------------------- #
    #
    # ``_file_bytes_strict`` only vets the *leaf*. A leaf check is not enough:
    # replacing an owned intermediate directory (``run``/``config``/…) with a
    # symlink would let a read/write/chmod/signal reach an external file that
    # still looks like a regular owner file. Every managed operation therefore
    # first proves the *whole* chain from the owned root is real, owned, and
    # symlink-free.

    def _dir_chain_ok(self, directory: Path) -> bool:
        """True when ``directory`` and every ancestor up to the owned root are
        real, owner-owned, non-symlink, and **not shared-writable** directories.

        A group/other-writable component is refused: another user could then
        swap a leaf inside it, so a value read through that chain is never
        trusted (and the directory is never ``chmod``-ed as if it were ours).
        """
        root = self.root
        try:
            rel = directory.relative_to(root)
        except ValueError:
            return False
        components = [root]
        current = root
        for part in rel.parts:
            current = current / part
            components.append(current)
        for component in components:
            st = _lstat(str(component))
            if (
                st is None
                or stat.S_ISLNK(st.st_mode)
                or not stat.S_ISDIR(st.st_mode)
                or st.st_uid != os.getuid()
                or (st.st_mode & 0o022)
            ):
                return False
        return True

    def _dir_chain_safe(self, directory: Path) -> bool:
        """True when every *existing* directory from the owned root down to
        ``directory`` is real, owned, and symlink-free.

        Unlike :meth:`_dir_chain_ok` this tolerates components that do not exist
        yet (a missing ``run`` is "nothing recorded", not "unsafe"), but it still
        refuses a symlinked/foreign ancestor so a swapped directory can never be
        read as an empty tree.
        """
        root = self.root
        root_st = _lstat(str(root))
        if (
            root_st is None
            or stat.S_ISLNK(root_st.st_mode)
            or not stat.S_ISDIR(root_st.st_mode)
            or root_st.st_uid != os.getuid()
        ):
            return False
        try:
            rel = directory.relative_to(root)
        except ValueError:
            return False
        current = root
        for part in rel.parts:
            current = current / part
            st = _lstat(str(current))
            if st is None:
                return True  # nothing deeper can exist; the existing chain is safe
            if (
                stat.S_ISLNK(st.st_mode)
                or not stat.S_ISDIR(st.st_mode)
                or st.st_uid != os.getuid()
            ):
                return False
        return True

    def _file_chain_ok(self, path: Path) -> bool:
        """True when ``path``'s parent chain from the owned root is safe."""
        return self._dir_chain_ok(path.parent)

    def _assert_managed_dirs(self, path: Path) -> None:
        if not self._file_chain_ok(path):
            raise ServiceOwnershipError("dir_unsafe", _MSG_DIR_UNSAFE)

    def _read_owned_file(
        self, path: Path, *, max_bytes: int, private: bool = False
    ) -> bytes | None:
        """Bounded read of a managed file whose whole directory chain is owned.

        Returns ``None`` (a static denial) when any component of the path is a
        symlink, a foreign directory, or a shared-writable directory; when the
        leaf is not a regular owner file within the bound; or — with ``private``
        — when it is not owner-only.

        The leaf is ``lstat``-checked as a regular file *before* it is opened and
        the open carries ``O_NONBLOCK``: opening a non-regular entry (a FIFO, a
        device, a socket) can never block the caller, so a planted FIFO is
        denied promptly instead of hanging a lifecycle operation.
        """
        if not self._file_chain_ok(path):
            return None
        pre = _lstat(str(path))
        if pre is None or stat.S_ISLNK(pre.st_mode) or not stat.S_ISREG(pre.st_mode):
            return None
        flags = (
            os.O_RDONLY
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NONBLOCK", 0)
        )
        try:
            fd = os.open(str(path), flags)
        except OSError:
            return None
        try:
            st = os.fstat(fd)
            if (
                not stat.S_ISREG(st.st_mode)
                or st.st_uid != os.getuid()
                or st.st_size > max_bytes
                or (private and (st.st_mode & 0o077))
            ):
                return None
            data = b""
            while len(data) <= max_bytes:
                chunk = os.read(fd, max_bytes + 1 - len(data))
                if not chunk:
                    break
                data += chunk
        except OSError:
            return None
        finally:
            os.close(fd)
        if len(data) > max_bytes:
            return None
        return data

    def _read_owned_json(self, path: Path, *, max_bytes: int = MAX_RECORD_BYTES) -> Any:
        data = self._read_owned_file(path, max_bytes=max_bytes)
        if data is None:
            raise ValueError("record is not a readable managed file")
        return _strict_json(data)

    def _atomic_write_owned(self, path: Path, data: bytes, mode: int) -> None:
        """Atomically write a managed file only when its chain is safe."""
        self._assert_managed_dirs(path)
        _atomic_write(path, data, mode)

    def _marker_doc(self) -> dict[str, Any] | None:
        try:
            doc = self._read_owned_json(self.marker)
        except (OSError, ValueError):
            return None
        return doc if isinstance(doc, dict) else None

    def _marker_valid(self) -> bool:
        doc = self._marker_doc()
        if doc is None:
            return False
        instance_id = doc.get("instanceId")
        return (
            doc.get("kind") == MARKER_KIND
            and doc.get("markerVersion") == MARKER_VERSION
            and doc.get("root") == os.path.realpath(self.root)
            and doc.get("ownerUid") == os.getuid()
            and isinstance(instance_id, str)
            and len(instance_id) >= 16
        )

    def _marker_instance_id(self) -> str | None:
        doc = self._marker_doc()
        if doc is None or doc.get("kind") != MARKER_KIND:
            return None
        instance_id = doc.get("instanceId")
        return instance_id if isinstance(instance_id, str) else None

    def is_owned_root(self) -> bool:
        """True when the app-owned root exists with a valid ownership marker."""
        return (
            self.root.is_dir()
            and not self.root.is_symlink()
            and self._marker_valid()
        )

    def _ensure_owned_root(self) -> None:
        root_st = _lstat(str(self.root))
        if root_st is None:
            os.makedirs(str(self.root), mode=0o700, exist_ok=True)
            os.chmod(str(self.root), 0o700)
            self._write_marker()
            return
        if stat.S_ISLNK(root_st.st_mode):
            raise ServiceOwnershipError("root_symlink", _MSG_ROOT_SYMLINK)
        if not stat.S_ISDIR(root_st.st_mode):
            raise ServiceOwnershipError("root_not_dir", _MSG_ROOT_NOT_DIR)
        if root_st.st_uid != os.getuid() or (root_st.st_mode & 0o022):
            raise ServiceOwnershipError("root_unowned", _MSG_ROOT_UNOWNED)
        try:
            entries = os.listdir(str(self.root))
        except OSError as exc:
            raise ServiceOwnershipError("root_unowned", _MSG_ROOT_UNOWNED) from exc
        if entries:
            # A pre-existing non-empty root must carry our exact marker.
            if not self._marker_valid():
                raise ServiceOwnershipError("root_unowned", _MSG_ROOT_UNOWNED)
            os.chmod(str(self.root), 0o700)
            return
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
        self._atomic_write_owned(self.marker, canonical_json(doc) + b"\n", 0o600)

    def _ensure_private_dir(self, directory: Path) -> None:
        """Create or verify one owned, owner-private, non-symlink directory.

        The *parent* chain from the owned root is proven safe first, so a
        symlinked intermediate directory can never redirect the ``makedirs`` or
        the ``chmod`` to an external location.
        """
        if not self._dir_chain_ok(directory.parent):
            raise ServiceOwnershipError("dir_unsafe", _MSG_DIR_UNSAFE)
        target = str(directory)
        st = _lstat(target)
        if st is None:
            os.makedirs(target, mode=0o700, exist_ok=True)
            st = _lstat(target)
        if (
            st is None
            or stat.S_ISLNK(st.st_mode)
            or not stat.S_ISDIR(st.st_mode)
            or st.st_uid != os.getuid()
        ):
            raise ServiceOwnershipError("dir_unsafe", _MSG_DIR_UNSAFE)
        os.chmod(target, 0o700)

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
            self._ensure_private_dir(directory)

    # -- own-tree configuration hygiene ---------------------------------- #

    def _scan_dir_for_risks(self, directory: Path) -> None:
        if os.path.islink(str(directory)) or not os.path.isdir(str(directory)):
            return
        try:
            entries = sorted(os.listdir(str(directory)))
        except OSError:
            return
        for entry in entries:
            if entry not in _RISK_ENTRY_NAMES:
                continue
            full = os.path.join(str(directory), entry)
            if (
                os.path.realpath(full) == os.path.realpath(str(self.config_file))
                and _file_bytes_strict(full, max_bytes=MAX_RECORD_BYTES)
                == self._config_disk_bytes
                and self._config_disk_bytes == self._expected_config_bytes() + b"\n"
            ):
                continue  # the one exact, known generated config file
            raise ServiceOwnershipError("tree_dirty", _MSG_TREE_DIRTY)

    def _assert_tree_clean(self) -> None:
        """Refuse unowned config/instruction/plugin entries inside the owned tree."""
        self._scan_dir_for_risks(self.root)
        self._scan_dir_for_risks(self.cwd)
        self._scan_dir_for_risks(self.home)
        self._scan_dir_for_risks(self.config_home)
        self._scan_dir_for_risks(self.config_file.parent)
        self._scan_dir_for_risks(self.projects_dir)
        if os.path.isdir(str(self.projects_dir)) and not os.path.islink(str(self.projects_dir)):
            for name in sorted(os.listdir(str(self.projects_dir))):
                location = self.projects_dir / name
                if os.path.isdir(str(location)) and not os.path.islink(str(location)):
                    self._scan_dir_for_risks(location)

    # -- process identity ------------------------------------------------- #

    def _read_pid_record(self) -> tuple[str, dict[str, Any] | None]:
        """Return ``("absent"|"invalid"|"ok", doc)`` — never silently "absent".

        An unsafe ``run`` chain is reported ``invalid`` (never ``absent``), so a
        symlinked run directory can never be mistaken for "nothing recorded".
        """
        root_st = _lstat(str(self.root))
        if (
            root_st is None
            or stat.S_ISLNK(root_st.st_mode)
            or not stat.S_ISDIR(root_st.st_mode)
            or root_st.st_uid != os.getuid()
        ):
            # No owned root (or an unsafe one) means "nothing recorded"; an
            # unsafe *intermediate* is reported invalid below.
            return ("absent", None) if root_st is None else ("invalid", None)
        if not os.path.lexists(str(self.pid_record)):
            # A genuinely missing record is "absent" only while the existing
            # ``run`` chain is safe; a swapped run directory is "invalid".
            return ("absent", None) if self._dir_chain_safe(self.run) else ("invalid", None)
        if not self._file_chain_ok(self.pid_record):
            return ("invalid", None)
        try:
            doc = self._read_owned_json(self.pid_record)
        except (OSError, ValueError):
            return ("invalid", None)
        if not isinstance(doc, dict):
            return ("invalid", None)
        if doc.get("recordVersion") != PID_RECORD_VERSION:
            return ("invalid", None)
        return ("ok", doc)

    def _secret_bytes(self) -> bytes | None:
        return self._read_owned_file(self.secret_file, max_bytes=1024, private=True)

    def _expected_exe(self) -> str | None:
        try:
            return os.path.realpath(self._resolve_binary())
        except ServiceError:
            return None

    def _identity_matches(self, pid: int, record: Mapping[str, Any]) -> bool:
        """Prove ``pid`` is exactly the recorded, owned instance.

        Binds the live ``/proc`` view to **expected manager paths** (cwd, HOME,
        exe) and to the marker's ``instanceId`` and the pinned version — not just
        to the record's own claims. That closes the "record says the right thing,
        but it is not our tree/process" gap.
        """
        try:
            rec_pid = int(record["pid"])
        except (KeyError, TypeError, ValueError):
            return False
        if rec_pid != pid or not _pid_alive(pid):
            return False

        starttime = _proc_starttime(pid)
        if starttime is None or starttime != record.get("starttime"):
            return False

        live_exe = _proc_exe(pid)
        expected_exe = self._expected_exe()
        if live_exe is None or live_exe != record.get("exe"):
            return False
        if expected_exe is not None and live_exe != expected_exe:
            return False

        expected_cwd = os.path.realpath(str(self.cwd))
        live_cwd = _proc_cwd(pid)
        if live_cwd is None or live_cwd != record.get("cwd") or live_cwd != expected_cwd:
            return False

        expected_home = os.path.realpath(str(self.home))
        environ = _proc_environ(pid)
        live_home = environ.get("HOME")
        if live_home is None or live_home != record.get("home") or live_home != expected_home:
            return False

        marker_instance = self._marker_instance_id()
        if not marker_instance or record.get("markerInstanceId") != marker_instance:
            return False

        if record.get("version") not in (None, OPENCODE_PINNED_VERSION):
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
        """One of ``none`` | ``stopped`` | ``ours`` | ``foreign`` | ``invalid``."""
        kind, record = self._read_pid_record()
        if kind == "absent":
            return "none"
        if kind == "invalid" or record is None:
            return "invalid"
        try:
            pid = int(record["pid"])
        except (KeyError, TypeError, ValueError):
            return "invalid"
        if self._identity_matches(pid, record):
            return "ours"
        if _pid_alive(pid):
            return "foreign"
        return "stopped"

    def _signal_owned(self, pid: int, record: Mapping[str, Any], sig: int) -> None:
        """Signal ``pid`` only after pinning and re-proving identity.

        ``pidfd_open`` runs **first** so the process image is pinned before the
        identity check; a pid reused between check and signal cannot be
        retargeted through the pidfd. Where pidfds are unavailable, identity is
        re-proved immediately before ``os.kill``.
        """
        pidfd: int | None = None
        try:
            pidfd = os.pidfd_open(pid, 0)  # type: ignore[attr-defined]
        except (AttributeError, OSError):
            pidfd = None
        try:
            if not self._identity_matches(pid, record):
                raise ServiceProcessError("foreign_process", _MSG_FOREIGN_PROCESS)
            if pidfd is not None and hasattr(signal, "pidfd_send_signal"):
                signal.pidfd_send_signal(pidfd, sig)  # type: ignore[attr-defined]
            else:
                if not self._identity_matches(pid, record):
                    raise ServiceProcessError("foreign_process", _MSG_FOREIGN_PROCESS)
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

    def _path_dir(self) -> str:
        return os.path.dirname(os.path.realpath(self._resolve_binary()))

    def _binary_env(self) -> dict[str, str]:
        return {
            "PATH": f"{self._path_dir()}:/usr/local/bin:/usr/bin:/bin",
            "HOME": str(self.home),
            "TMPDIR": str(self.tmp),
            "LANG": "C.UTF-8",
        }

    def _remaining(self, deadline: float) -> float:
        """Seconds left before ``deadline`` or a bounded start-timeout error."""
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ServiceStartError("start_timeout", _MSG_START_FAILED)
        return remaining

    def _verify_binary_version(self, binary: str, deadline: float) -> str:
        # The version subprocess shares the one startup deadline: it can never
        # outlive the caller's ``start_timeout`` and eat the readiness window.
        timeout = min(20.0, max(0.05, self._remaining(deadline)))
        try:
            proc = subprocess.run(  # noqa: S603 - explicit argv, shell=False
                [binary, "--version"],
                capture_output=True,
                text=True,
                env=self._binary_env(),
                cwd=str(self.cwd) if self.cwd.is_dir() else str(self.data_dir),
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise ServiceStartError("start_timeout", _MSG_START_FAILED) from exc
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
            "PATH": f"{self._path_dir()}:/usr/local/bin:/usr/bin:/bin",
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

    def _verify_expected_config(self) -> bytes:
        expected = self._expected_config_bytes()
        if expected != self._config_bytes:
            raise ServiceConfigError("config_drift", _MSG_CONFIG_DRIFT)
        return expected

    def _verify_on_disk_config(self) -> bytes:
        """Prove the *on-disk* config is exactly this manager's own document.

        The in-memory comparison in :meth:`_verify_expected_config` cannot see a
        deleted or swapped config file. This proof additionally requires the
        whole managed chain down to the config file to be real and owned (so a
        symlinked internal config directory is **refused**, never silently
        skipped) and the bytes on disk to equal the canonical expected config
        plus its trailing newline. A missing, symlinked, foreign, or drifted
        document is refused before an idempotent reuse, a connection, or a
        descriptor value is derived from disk state.
        """
        if not self._file_chain_ok(self.config_file):
            raise ServiceOwnershipError("dir_unsafe", _MSG_DIR_UNSAFE)
        expected = self._verify_expected_config() + b"\n"
        on_disk = self._read_owned_file(self.config_file, max_bytes=MAX_RECORD_BYTES)
        if on_disk != expected:
            raise ServiceOwnershipError("config_disk", _MSG_TREE_DIRTY)
        return on_disk

    def _write_config(self) -> None:
        data = self._verify_expected_config()
        expected = data + b"\n"
        self._ensure_private_dir(self.config_file.parent)
        self._atomic_write_owned(self.config_file, expected, 0o600)
        # Verify the bytes actually on disk are exactly ours.
        on_disk = self._read_owned_file(self.config_file, max_bytes=MAX_RECORD_BYTES)
        if on_disk != expected:
            raise ServiceOwnershipError("config_disk", _MSG_TREE_DIRTY)

    def _write_secret(self, password: str) -> None:
        self._atomic_write_owned(self.secret_file, password.encode("utf-8"), 0o600)

    def read_password(self) -> str:
        """Return the per-run HTTP Basic secret (callers must not log it).

        The secret is read only from a fully owned, symlink-free ``run`` chain
        and only when the file is owner-only; a swapped ``run`` directory, a
        foreign file, or a group/other-readable secret is refused.
        """
        if not self.is_owned_root():
            raise ServiceOwnershipError("root_unowned", _MSG_NOT_OWNED_STOP)
        if not self._file_chain_ok(self.secret_file):
            raise ServiceOwnershipError("dir_unsafe", _MSG_DIR_UNSAFE)
        data = self._read_owned_file(self.secret_file, max_bytes=1024, private=True)
        if data is None:
            raise ServiceOwnershipError("no_secret", _MSG_NO_SECRET)
        return data.decode("utf-8")

    def _read_port(self) -> int | None:
        data = self._read_owned_file(self.port_file, max_bytes=64)
        if data is None:
            return None
        try:
            port = int(data.decode("ascii").strip())
        except (ValueError, UnicodeDecodeError):
            return None
        return port if 1 <= port <= 65535 else None

    def _validated_base_url(self, record: Mapping[str, Any]) -> str | None:
        """Return the loopback URL only when the live port file still matches the
        owned record's port (a changed port file is refused, not trusted)."""
        port = self._read_port()
        if port is None:
            return None
        recorded = record.get("port")
        if isinstance(recorded, bool) or not isinstance(recorded, int) or port != recorded:
            return None
        return f"http://{SERVICE_HOST}:{port}"

    def _base_url(self) -> str | None:
        port = self._read_port()
        return None if port is None else f"http://{SERVICE_HOST}:{port}"

    def _fetch(self, path: str, timeout: float = _PROBE_TIMEOUT_S) -> Any:
        if path not in _ALLOWED_PROBE_PATHS:
            raise ServiceProcessError("probe_path", _MSG_PROBE_ORIGIN)
        url = self._base_url()
        if url is None:
            raise ServiceProcessError("not_running", _MSG_PROBE)
        return self._http_get(url, SERVICE_USERNAME, self.read_password(), timeout, path)

    # -- spawn / readiness ------------------------------------------------ #

    def _open_log(self, path: Path) -> int:
        """Open a managed log file for truncation without following a symlink.

        The whole ``run`` chain must be owned and symlink-free; a pre-existing
        symlink, a non-regular entry, a foreign-owner file, or a multiply-linked
        file (a hardlink into someone else's data) is refused rather than
        truncated.
        """
        if not self._file_chain_ok(path):
            raise ServiceOwnershipError("dir_unsafe", _MSG_DIR_UNSAFE)
        st = _lstat(str(path))
        if st is not None and (
            stat.S_ISLNK(st.st_mode)
            or not stat.S_ISREG(st.st_mode)
            or st.st_uid != os.getuid()
            or st.st_nlink != 1
        ):
            raise ServiceOwnershipError("dir_unsafe", _MSG_DIR_UNSAFE)
        flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_TRUNC
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0)
        )
        fd = os.open(str(path), flags, 0o600)
        try:
            fst = os.fstat(fd)
            if (
                not stat.S_ISREG(fst.st_mode)
                or fst.st_uid != os.getuid()
                or fst.st_nlink != 1
            ):
                raise ServiceOwnershipError("dir_unsafe", _MSG_DIR_UNSAFE)
        except BaseException:
            os.close(fd)
            raise
        return fd

    def _spawn(self, binary: str, port: int, password: str) -> subprocess.Popen[bytes]:
        env = self._child_env(password)
        out_fd = self._open_log(self.stdout_file)
        try:
            err_fd = self._open_log(self.stderr_file)
        except BaseException:
            os.close(out_fd)
            raise
        with os.fdopen(out_fd, "wb") as out, os.fdopen(err_fd, "wb") as err:
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
            "recordVersion": PID_RECORD_VERSION,
            "pid": pid,
            "starttime": _proc_starttime(pid),
            "exe": _proc_exe(pid),
            "cwd": _proc_cwd(pid),
            "home": os.path.realpath(str(self.home)),
            "port": port,
            "passwordSha256": _sha256_hex(password.encode("utf-8")),
            "configFingerprint": self.config_fingerprint,
            "instanceFence": self.instance_fence,
            "markerInstanceId": self._marker_instance_id(),
            "providerEnvNames": sorted(self.provider_env),
        }

    def _await_ready(
        self,
        proc: subprocess.Popen[bytes],
        port: int,
        password: str,
        deadline: float,
    ) -> dict[str, Any]:
        url = f"http://{SERVICE_HOST}:{port}"
        while True:
            remaining = self._remaining(deadline)
            if proc.poll() is not None:
                raise ServiceStartError("server_exited", _MSG_START_EXITED)
            try:
                info = self._http_get(
                    url,
                    SERVICE_USERNAME,
                    password,
                    min(_PROBE_TIMEOUT_S, max(0.05, remaining)),
                    "/api/info",
                )
            except Exception:  # bounded readiness: not-yet-up is expected
                info = None
            if isinstance(info, dict) and info.get("version"):
                return info
            remaining = self._remaining(deadline)
            self._sleep(min(_PROBE_INTERVAL_S, remaining))

    def _assert_server_owned(
        self, pid: int, port: int, password: str, deadline: float
    ) -> None:
        """Prove the ready server is our child and runs only our effective config.

        The two post-ready probes share the *same* startup deadline as readiness
        and the version check, so fixed 3 s probes can never push startup past
        ``start_timeout``.
        """
        url = f"http://{SERVICE_HOST}:{port}"
        info = self._http_get(
            url,
            SERVICE_USERNAME,
            password,
            min(_PROBE_TIMEOUT_S, max(0.05, self._remaining(deadline))),
            "/api/info",
        )
        if not isinstance(info, dict) or info.get("pid") != pid:
            raise ServiceProcessError("server_pid", _MSG_SERVER_PID)
        if info.get("version") != OPENCODE_PINNED_VERSION:
            raise ServiceVersionError("server_version", _MSG_SERVER_VERSION)
        sources = self._http_get(
            url,
            SERVICE_USERNAME,
            password,
            min(_PROBE_TIMEOUT_S, max(0.05, self._remaining(deadline))),
            "/api/config",
        )
        if not isinstance(sources, list):
            raise ServiceProcessError("server_config", _MSG_SERVER_CONFIG)
        merged: dict[str, Any] | None = None
        for source in sources:
            if not isinstance(source, dict):
                raise ServiceProcessError("server_config", _MSG_SERVER_CONFIG)
            source_path = source.get("path")
            if not isinstance(source_path, str):
                raise ServiceProcessError("server_config", _MSG_SERVER_CONFIG)
            real = os.path.realpath(source_path)
            if source.get("type") == "document":
                if real != os.path.realpath(str(self.config_file)):
                    raise ServiceProcessError("server_config", _MSG_SERVER_CONFIG)
                if merged is None and isinstance(source.get("info"), dict):
                    merged = source["info"]
            elif not _is_within(real, os.path.realpath(str(self.root))):
                # An inherited source directory is only acceptable if it carries
                # no configuration/instructions/plugins of its own.
                if _dir_has_risk(real):
                    raise ServiceProcessError("server_config", _MSG_SERVER_CONFIG)
        if merged is None:
            raise ServiceProcessError("server_config", _MSG_SERVER_CONFIG)
        self._assert_effective_config_owned(merged)

    def _assert_effective_config_owned(self, merged: Mapping[str, Any]) -> None:
        expected = self._expected_effective_config()
        if merged.get("policies") != expected.get("policies"):
            raise ServiceProcessError("server_config", _MSG_SERVER_CONFIG)
        if merged.get("permissions") != expected.get("permissions"):
            raise ServiceProcessError("server_config", _MSG_SERVER_CONFIG)
        if merged.get("agents") != expected.get("agents"):
            raise ServiceProcessError("server_config", _MSG_SERVER_CONFIG)
        if merged.get("update") != "disable":
            raise ServiceProcessError("server_config", _MSG_SERVER_CONFIG)
        if merged.get("warming") is not False:
            raise ServiceProcessError("server_config", _MSG_SERVER_CONFIG)
        if merged.get("compaction") != {"auto": False}:
            raise ServiceProcessError("server_config", _MSG_SERVER_CONFIG)
        for forbidden in ("model", "default_agent"):
            if forbidden in merged:
                raise ServiceProcessError("server_config", _MSG_SERVER_CONFIG)
        expected_providers = set(expected.get("providers", {}) or {})
        if set(merged.get("providers", {}) or {}) != expected_providers:
            raise ServiceProcessError("server_config", _MSG_SERVER_CONFIG)

    def _terminate_child(self, proc: subprocess.Popen[bytes]) -> bool:
        """Terminate our own direct child; return True only when it is gone.

        A ``False`` result means the child could not be proven reaped, so the
        caller must retain (never clear) its identity record.
        """
        try:
            proc.terminate()
        except OSError:
            pass
        try:
            proc.wait(timeout=max(self.stop_timeout, 1.0))
            return True
        except subprocess.TimeoutExpired:
            pass
        try:
            proc.kill()
        except OSError:
            pass
        try:
            proc.wait(timeout=5.0)
            return True
        except subprocess.TimeoutExpired:
            return False

    def _write_pid_record(self, record: Mapping[str, Any]) -> None:
        self._atomic_write_owned(self.pid_record, canonical_json(dict(record)) + b"\n", 0o600)

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
        self._atomic_write_owned(self.descriptor_file, canonical_json(doc) + b"\n", 0o600)

    def _clear_run_records(self) -> None:
        # Never unlink through a swapped ``run`` directory: if the chain is not
        # the owned one, removing the paths would delete external files.
        if not self._dir_chain_ok(self.run):
            return
        for path in (self.pid_record, self.port_file, self.secret_file, self.descriptor_file):
            try:
                os.unlink(str(path))
            except OSError:
                pass

    # -- public lifecycle ------------------------------------------------- #

    def start(self) -> ServiceStatus:
        """Start the owned service, or return it idempotently if already running.

        Concurrent/idempotent: a second ``start`` with the **same** configuration
        and credential context returns the running instance; a start with a
        **different** configuration or credential context while an instance runs
        is refused. A foreign, invalid, or stale record never causes a signal.
        """
        self._assert_root_safe()
        self._prepare_data_dir()
        with self._locked():
            return self._start_locked()

    def _start_locked(self) -> ServiceStatus:
        state = self._process_state()
        if state == "ours":
            _, record = self._read_pid_record()
            record = record or {}
            if (
                record.get("configFingerprint") != self.config_fingerprint
                or record.get("instanceFence") != self.instance_fence
            ):
                raise ServiceConfigError("conflicting_config", _MSG_CONFLICTING_CONFIG)
            # An already-running instance is re-proved: the tree and the on-disk
            # config must still be exactly ours, and the live port must still
            # match the owned record. A drifted tree/port is refused, never
            # reported as a usable running instance.
            self._assert_tree_clean()
            self._verify_on_disk_config()
            if self._validated_base_url(record) is None:
                raise ServiceProcessError("not_running", _MSG_PROBE)
            return self._running_status(inspect=False)
        if state == "foreign":
            raise ServiceProcessError("foreign_process", _MSG_STOP_FOREIGN)
        if state == "invalid":
            raise ServiceProcessError("invalid_record", _MSG_INVALID_RECORD)

        self._ensure_owned_root()
        self._create_private_dirs()
        self._assert_tree_clean()

        binary = self._resolve_binary()
        # One deadline for *every* startup step: the version subprocess, the
        # readiness poll, and the two post-ready config probes.
        deadline = time.monotonic() + self.start_timeout
        self._verify_binary_version(binary, deadline)
        self._write_config()

        port = self._port if self._port else _free_loopback_port()
        password = secrets.token_urlsafe(32)
        self._write_secret(password)
        self._atomic_write_owned(self.port_file, str(port).encode("ascii"), 0o600)

        proc = self._spawn(binary, port, password)
        self._proc = proc
        try:
            # Record the owned child identity *immediately* after spawn so a crash
            # between here and readiness still leaves a discoverable, owned record.
            initial = self._capture_identity(proc.pid, port, password)
            self._write_pid_record(initial)

            info = self._await_ready(proc, port, password, deadline)
            version = str(info.get("version", ""))
            if version != OPENCODE_PINNED_VERSION:
                raise ServiceVersionError("server_version", _MSG_SERVER_VERSION)
            self._assert_server_owned(proc.pid, port, password, deadline)

            record = self._capture_identity(proc.pid, port, password)
            record["version"] = version
            self._write_pid_record(record)
            self._write_descriptor(record, version)
        except BaseException:
            # Any failure after spawn must leave no unmanaged child behind. When
            # the child cannot be proven gone, retain (never clear) its identity
            # so it stays discoverable and owned.
            gone = self._terminate_child(proc)
            self._proc = None
            if gone:
                self._clear_run_records()
            else:
                try:
                    if not os.path.lexists(str(self.pid_record)):
                        self._write_pid_record(self._capture_identity(proc.pid, port, password))
                except Exception:  # pragma: no cover - best-effort retention
                    pass
            raise
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
        anywhere in this manager: only the run records are cleared, and only
        after the owned process is proven gone. If the process is still alive at
        the deadline, or its identity can no longer be proven, nothing is
        cleared and no "stopped" is reported.
        """
        self._assert_root_safe()
        self._prepare_data_dir()
        with self._locked():
            return self._stop_locked()

    def _stop_locked(self) -> ServiceStatus:
        if not os.path.lexists(str(self.root)):
            return ServiceStatus(state="absent", running=False, root=str(self.root))
        if os.path.islink(str(self.root)):
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
        if state == "invalid":
            raise ServiceProcessError("invalid_record", _MSG_INVALID_RECORD)

        _, record = self._read_pid_record()
        record = record or {}
        pid = int(record["pid"])
        self._signal_owned(pid, record, signal.SIGTERM)
        deadline = time.monotonic() + self.stop_timeout
        while _pid_alive(pid) and time.monotonic() < deadline:
            self._sleep(min(_TERM_POLL_S, max(0.0, deadline - time.monotonic())))
        if _pid_alive(pid):
            # The identity may have changed under us; re-prove before the hard kill.
            self._signal_owned(pid, record, signal.SIGKILL)
            hard_deadline = time.monotonic() + min(self.stop_timeout, 5.0)
            while _pid_alive(pid) and time.monotonic() < hard_deadline:
                self._sleep(min(_TERM_POLL_S, max(0.0, hard_deadline - time.monotonic())))
        if _pid_alive(pid):
            raise ServiceProcessError("still_alive", _MSG_STILL_ALIVE)
        try:
            os.waitpid(pid, os.WNOHANG)
        except (ChildProcessError, OSError):
            pass
        if self._proc is not None:
            try:
                self._proc.poll()
            except Exception:  # pragma: no cover - defensive
                pass
            self._proc = None
        self._clear_run_records()
        return ServiceStatus(state="stopped", running=False, root=str(self.root))

    def restart(self) -> ServiceStatus:
        """Replace **only** the owned process, keeping the private data tree."""
        self._assert_root_safe()
        self._prepare_data_dir()
        with self._locked():
            self._stop_locked()
            return self._start_locked()

    def status(self) -> ServiceStatus:
        """Report the current state without ever raising or signalling."""
        if not os.path.lexists(str(self.root)):
            return ServiceStatus(state="absent", running=False, root=str(self.root))
        if os.path.islink(str(self.root)) or not os.path.isdir(str(self.root)):
            return ServiceStatus(state="unsafe", running=False, root=str(self.root))
        if not self._marker_valid():
            return ServiceStatus(state="unowned", running=False, root=str(self.root))
        state = self._process_state()
        if state == "ours":
            return self._running_status(inspect=True)
        if state == "foreign":
            return ServiceStatus(state="foreign", running=False, root=str(self.root))
        if state == "invalid":
            return ServiceStatus(state="invalid", running=False, root=str(self.root))
        return ServiceStatus(state="stopped", running=False, root=str(self.root))

    def _running_status(self, *, inspect: bool) -> ServiceStatus:
        _, record = self._read_pid_record()
        record = record or {}
        try:
            pid = int(record.get("pid", 0)) or None
        except (TypeError, ValueError):
            pid = None
        version = record.get("version")
        # Report a URL only while the live port file still matches the owned
        # record; a changed port is never reported as a usable endpoint.
        url = self._validated_base_url(record)
        if inspect and url is not None:
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
            url=url,
            version=version,
            root=str(self.root),
        )

    # -- locations -------------------------------------------------------- #

    def location_for(self, project_id: str) -> ProjectLocation:
        """Return a clean, owned session location for ``project_id``.

        The application project id is validated as an opaque, traversal-safe
        identifier; the location is a private owned subdirectory under the
        service root, deliberately separate from the real project directory. A
        symlink, an unowned non-empty directory, a file at the location, or a
        location that has acquired unowned configuration entries is refused.
        """
        validate_identifier(project_id, "project_id")
        self._assert_root_safe()
        self._prepare_data_dir()
        with self._locked():
            self._ensure_owned_root()
            self._create_private_dirs()
            self._assert_tree_clean()
            location = self.projects_dir / project_id
            st = _lstat(str(location))
            if st is not None and stat.S_ISLNK(st.st_mode):
                raise ServiceLocationError("location_symlink", _MSG_LOCATION_SYMLINK)
            if st is not None and not stat.S_ISDIR(st.st_mode):
                raise ServiceLocationError("location_not_dir", _MSG_LOCATION_NOT_DIR)
            if st is not None and st.st_uid != os.getuid():
                raise ServiceOwnershipError("location_unowned", _MSG_LOCATION_UNOWNED)
            if st is not None:
                if not self._dir_chain_ok(location):
                    raise ServiceOwnershipError("dir_unsafe", _MSG_DIR_UNSAFE)
                try:
                    entries = os.listdir(str(location))
                except OSError as exc:
                    raise ServiceOwnershipError("location_unowned", _MSG_LOCATION_UNOWNED) from exc
                if entries and not self._location_marker_valid(location, project_id):
                    raise ServiceOwnershipError("location_unowned", _MSG_LOCATION_UNOWNED)
                os.chmod(str(location), 0o700)
                self._scan_dir_for_risks(location)
                doc = self._read_location_marker(location)
                return ProjectLocation(
                    app_project_id=project_id,
                    directory=str(location),
                    location_id=str((doc or {}).get("instanceId", "")),
                    root=str(self.root),
                )
            self._ensure_private_dir(location)
            result = self._write_location_marker(location, project_id)
            self._scan_dir_for_risks(location)
            return result

    def existing_location(self, project_id: str) -> ProjectLocation:
        """Return the **already existing** owned location for ``project_id``.

        Read-only counterpart to :meth:`location_for`: it proves the owned root,
        the safe directory chain, the clean owned tree, and the location's owned
        marker, and it **creates nothing** — no ``data_dir``/root/location
        ``mkdir``, no ``chmod``, no lock file, no marker or config write. A
        missing location (or an unsafe/unowned one) is refused; unlike
        :meth:`location_for` it is **never** created.
        """
        validate_identifier(project_id, "project_id")
        self._assert_root_safe()
        if not self.is_owned_root():
            raise ServiceOwnershipError("root_unowned", _MSG_ROOT_UNOWNED)
        if not self._dir_chain_ok(self.projects_dir):
            raise ServiceOwnershipError("dir_unsafe", _MSG_DIR_UNSAFE)
        location = self.projects_dir / project_id
        st = _lstat(str(location))
        if st is None:
            raise ServiceLocationError("location_absent", _MSG_LOCATION_ABSENT)
        if stat.S_ISLNK(st.st_mode):
            raise ServiceLocationError("location_symlink", _MSG_LOCATION_SYMLINK)
        if not stat.S_ISDIR(st.st_mode):
            raise ServiceLocationError("location_not_dir", _MSG_LOCATION_NOT_DIR)
        if st.st_uid != os.getuid():
            raise ServiceOwnershipError("location_unowned", _MSG_LOCATION_UNOWNED)
        if not self._dir_chain_ok(location):
            raise ServiceOwnershipError("dir_unsafe", _MSG_DIR_UNSAFE)
        self._assert_tree_clean()
        if not self._location_marker_valid(location, project_id):
            raise ServiceOwnershipError("location_unowned", _MSG_LOCATION_UNOWNED)
        self._scan_dir_for_risks(location)
        doc = self._read_location_marker(location)
        instance_id = str((doc or {}).get("instanceId", ""))
        if not instance_id:
            raise ServiceOwnershipError("location_unowned", _MSG_LOCATION_UNOWNED)
        return ProjectLocation(
            app_project_id=project_id,
            directory=str(location),
            location_id=instance_id,
            root=str(self.root),
        )

    def _read_location_marker(self, location: Path) -> dict[str, Any] | None:
        """Read a location marker only from a fully owned, symlink-free chain."""
        marker = location / LOCATION_MARKER_NAME
        if not self._file_chain_ok(marker):
            return None
        try:
            doc = self._read_owned_json(marker)
        except (OSError, ValueError):
            return None
        return doc if isinstance(doc, dict) else None

    def _location_marker_valid(self, location: Path, project_id: str) -> bool:
        doc = self._read_location_marker(location)
        if doc is None:
            return False
        return (
            doc.get("kind") == LOCATION_MARKER_KIND
            and doc.get("markerVersion") == LOCATION_MARKER_VERSION
            and doc.get("root") == os.path.realpath(str(location))
            and doc.get("ownerUid") == os.getuid()
            and doc.get("appProjectId") == project_id
            and isinstance(doc.get("instanceId"), str)
            and len(doc.get("instanceId", "")) >= 16
        )

    def _write_location_marker(self, location: Path, project_id: str) -> ProjectLocation:
        instance_id = secrets.token_hex(16)
        doc = {
            "kind": LOCATION_MARKER_KIND,
            "markerVersion": LOCATION_MARKER_VERSION,
            "root": os.path.realpath(str(location)),
            "ownerUid": os.getuid(),
            "appProjectId": project_id,
            "instanceId": instance_id,
            "createdAtMs": int(time.time() * 1000),
        }
        self._atomic_write_owned(location / LOCATION_MARKER_NAME, canonical_json(doc) + b"\n", 0o600)
        return ProjectLocation(
            app_project_id=project_id,
            directory=str(location),
            location_id=instance_id,
            root=str(self.root),
        )

    # -- safe discovery for a future client ------------------------------ #

    def _current_owned_record(self) -> dict[str, Any]:
        """The record of the *current* live owned instance, or a refusal.

        Proves the owned root, the live owned process, the safe ``run`` chain,
        the on-disk config, and the port all still agree before any descriptor
        or connection value is derived from them.
        """
        if not self.is_owned_root():
            raise ServiceOwnershipError("root_unowned", _MSG_NOT_OWNED_STOP)
        if self._process_state() != "ours":
            raise ServiceProcessError("not_running", _MSG_PROBE)
        self._assert_tree_clean()
        self._verify_on_disk_config()
        _, record = self._read_pid_record()
        record = record or {}
        if self._validated_base_url(record) is None:
            raise ServiceProcessError("not_running", _MSG_PROBE)
        return record

    def connection(self) -> ServiceConnection:
        """Explicit connection parameters for a future client (no secret).

        Requires a validated owned root, the live owned process, an unchanged
        owned tree/config, and a port file that still matches the owned record;
        a corrupted, drifted, or absent instance is refused rather than reported
        as a usable connection.
        """
        record = self._current_owned_record()
        url = self._validated_base_url(record)
        if url is None:  # pragma: no cover - re-checked after the proof above
            raise ServiceProcessError("not_running", _MSG_PROBE)
        return ServiceConnection(
            base_url=url, username=SERVICE_USERNAME, password_file=str(self.secret_file)
        )

    def _descriptor_binary(self, record: Mapping[str, Any]) -> str:
        try:
            return self._resolve_binary()
        except ServiceError:
            return str(record.get("exe", ""))

    def descriptor(self) -> ServiceDescriptor:
        """The safe, secret-free descriptor of the *current* owned instance.

        Values are derived from this manager's own expected paths plus the
        validated live record — never trusted from the on-disk document. When a
        descriptor document exists it must match this manager's own state
        exactly; a drifted or tampered document is refused.
        """
        record = self._current_owned_record()
        url = self._validated_base_url(record)
        if url is None:  # pragma: no cover - re-checked after the proof above
            raise ServiceProcessError("not_running", _MSG_PROBE)
        expected = ServiceDescriptor(
            url=url,
            pid=int(record["pid"]),
            version=str(record.get("version", "")),
            binary=self._descriptor_binary(record),
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
        if os.path.lexists(str(self.descriptor_file)):
            self._assert_descriptor_matches(expected)
        return expected

    def _assert_descriptor_matches(self, expected: ServiceDescriptor) -> None:
        try:
            doc = self._read_owned_json(self.descriptor_file)
        except (OSError, ValueError) as exc:
            raise ServiceProcessError("invalid_record", _MSG_INVALID_RECORD) from exc
        if (
            not isinstance(doc, dict)
            or doc.get("descriptorVersion") != DESCRIPTOR_VERSION
            or doc.get("kind") != DESCRIPTOR_KIND
        ):
            raise ServiceProcessError("invalid_record", _MSG_INVALID_RECORD)
        dirs = doc.get("dirs")
        if not isinstance(dirs, dict):
            raise ServiceProcessError("invalid_record", _MSG_INVALID_RECORD)
        matches = (
            doc.get("url") == expected.url,
            doc.get("pid") == expected.pid,
            doc.get("opencodeVersion") == expected.version,
            doc.get("binary") == expected.binary,
            dirs.get("root") == expected.root,
            dirs.get("home") == expected.home,
            dirs.get("xdgConfigHome") == expected.xdg_config_home,
            dirs.get("xdgDataHome") == expected.xdg_data_home,
            dirs.get("xdgStateHome") == expected.xdg_state_home,
            dirs.get("xdgCacheHome") == expected.xdg_cache_home,
            dirs.get("cwd") == expected.cwd,
            tuple(doc.get("providerEnvNames", ())) == expected.provider_env_names,
        )
        if not all(matches):
            raise ServiceProcessError("invalid_record", _MSG_INVALID_RECORD)

    def config_sources(self) -> list[Any]:
        """The config *source* list from ``GET /api/config`` (readiness only).

        Used to prove that the running instance reads only this manager's own
        generated config document and no inherited configuration.
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
    "MAX_HTTP_BODY_BYTES",
    "MAX_JSON_DEPTH",
    "MAX_LIFECYCLE_TIMEOUT_S",
    "MAX_RECORD_BYTES",
    "MIN_LIFECYCLE_TIMEOUT_S",
    "OPENCODE_PINNED_VERSION",
    "OpenCodeServiceManager",
    "PID_RECORD_VERSION",
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
