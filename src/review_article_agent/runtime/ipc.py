"""Owner-authenticated local IPC transport for the background worker.

Transport (``ARCH-IPC``): an **owner-only Unix-domain socket on Linux**,
separate from the OpenCode HTTP service. There is **no TCP listener**, no root
requirement, and no public unauthenticated control surface. Authentication
rejects non-owner control (``03-durable-worker.md`` deliverable 1 / acceptance
criterion "Authentication rejects non-owner control; no root or public
unauthenticated listener is required").

Authority is established **out-of-band** by the kernel's ``SO_PEERCRED`` peer
credentials, never from fields a client can place in the JSON request
(``docs/contracts/02-gates-and-runtime.md`` §1.3: no payload may mint
``actor_kind = human`` / ``authority = owner_authenticated`` / an owner ref).
The callback a caller registers therefore receives the *verified* peer identity
as a separate argument; the request object is untrusted data.

Safety properties implemented here:

* **Owner-private runtime directory** (``0o700``) under an **explicit**
  ``data_dir``; a pre-existing shared directory, a symlink, or a directory/file
  owned by another user is refused rather than silently used.
* **Singleton ownership**: an exclusive ``flock`` is held for the whole server
  lifetime, so exactly one server exists per ``data_dir``. Stale-socket cleanup
  happens only while that lock is held, and only for a socket we own; a regular
  file or symlink at the socket path is refused, never deleted.
* **Bounded, strict wire protocol**: one newline-delimited JSON request per
  connection, with a hard size limit, an I/O timeout, and strict JSON parsing
  that rejects duplicate keys, non-finite numbers, and over-deep nesting.
* **Curated errors only**: error responses carry a static code/message and never
  reflect the request payload, a raw exception, or any secret it contained.
* **No busy waiting**: the accept loop waits with a bounded timeout and checks a
  caller-supplied stop flag between iterations; a stalled or partial connection
  cannot hang the server past its I/O timeout.

The module is deliberately **callback-driven** and defines no operation names:
the engine main loop supplies a handler and knows nothing here about jobs,
budgets, or future operations.
"""

from __future__ import annotations

import errno
import fcntl
import json
import math
import os
import socket
import stat
import struct
import time
from dataclasses import dataclass
from typing import Any, Callable

from ..domain.errors import ReviewAgentError
from ..storage import paths

__all__ = [
    "IPCError",
    "RuntimeDirectoryError",
    "WorkerAlreadyRunning",
    "WorkerNotRunning",
    "PeerNotAuthorized",
    "PeerIdentityMismatch",
    "RequestError",
    "IPCRequestError",
    "PeerCredentials",
    "WorkerIPCServer",
    "WorkerIPCClient",
    "read_peer_credentials",
    "runtime_dir_for",
    "parse_request",
    "MAX_REQUEST_BYTES",
    "MAX_RESPONSE_BYTES",
    "MAX_JSON_DEPTH",
    "DEFAULT_IO_TIMEOUT",
]

#: Runtime sub-directory created under the explicit data directory.
RUNTIME_DIR_NAME = "runtime"

#: Socket filename inside the runtime directory.
DEFAULT_SOCKET_FILENAME = "worker.sock"

#: Singleton-lock filename inside the runtime directory.
DEFAULT_LOCK_FILENAME = "worker.lock"

#: Hard per-request size limit for a single newline-delimited JSON request.
MAX_REQUEST_BYTES = 64 * 1024

#: Hard per-response size limit the client will read.
MAX_RESPONSE_BYTES = 1024 * 1024

#: Maximum accepted JSON nesting depth (top-level object counts as depth 1).
MAX_JSON_DEPTH = 8

#: Maximum accepted nesting depth for a server response (more generous than a
#: request so the engine may return structured results, but still bounded).
MAX_RESPONSE_DEPTH = 64

#: Default per-connection / connect I/O timeout, in seconds.
DEFAULT_IO_TIMEOUT = 5.0

#: Upper bounds for caller-supplied numeric knobs (positive, finite, bounded).
MAX_REQUEST_BYTES_CAP = 64 * 1024 * 1024
MAX_TIMEOUT_SECONDS = 3600.0

#: Short timeout used only to probe whether a leftover socket is stale.
DEFAULT_PROBE_TIMEOUT = 1.0

#: Owner-private masks (no group/other access).
_DIR_MODE = 0o700
_FILE_MODE = 0o600

# Curated error codes (static; never derived from payload content).
_CODE_MALFORMED = "malformed_request"
_CODE_TOO_LARGE = "request_too_large"
_CODE_TIMEOUT = "request_timeout"
_CODE_UNAUTHORIZED = "unauthorized_peer"
_CODE_HANDLER = "handler_error"
_CODE_INVALID = "invalid_response"

#: The only error codes a client will surface as a curated transport error, each
#: mapped to a **static** curated message. The peer's own ``message`` is never
#: surfaced (it could reflect arbitrary content), and an unknown code is never
#: trusted as curated. Any structured application result is passed through the
#: ``ok`` success path.
_CURATED_MESSAGES = {
    _CODE_MALFORMED: "the request was malformed",
    _CODE_TOO_LARGE: "the request was too large",
    _CODE_TIMEOUT: "the request timed out",
    _CODE_UNAUTHORIZED: "control is restricted to the owner",
    _CODE_HANDLER: "the request could not be handled",
    _CODE_INVALID: "the server returned an invalid result",
}

_CURATED_CODES = frozenset(_CURATED_MESSAGES)


# --------------------------------------------------------------------------- #
# errors
# --------------------------------------------------------------------------- #
class IPCError(ReviewAgentError):
    """Base class for local control-transport errors."""


class RuntimeDirectoryError(IPCError):
    """The runtime directory, socket, or lock path is unsafe to use."""


class WorkerAlreadyRunning(IPCError):
    """Another server already owns the singleton lock for this data directory."""


class WorkerNotRunning(IPCError):
    """No server socket exists for this data directory."""


class PeerNotAuthorized(IPCError):
    """The peer on the other end is not the owning user."""


class PeerIdentityMismatch(IPCError):
    """The control-socket peer is not the expected process.

    Raised by the client when a caller supplies ``expected_peer_pid`` and the
    kernel-reported peer PID (``SO_PEERCRED``) differs, so control is never sent
    to a replaced or foreign listener at the same path. This is a *kernel*
    binding, not a value taken from the request or response payload.
    """


class RequestError(IPCError):
    """A request payload is not valid strict JSON of the expected shape."""


class IPCRequestError(IPCError):
    """The server returned a curated error response to a client request.

    ``code`` is one of the static codes above; ``message`` is the server's
    curated, non-reflective description. Neither ever carries the request body
    or a raw exception.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"control request rejected ({code}): {message}")
        self.code = code
        self.message = message


# --------------------------------------------------------------------------- #
# peer identity
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class PeerCredentials:
    """Kernel-reported identity of the peer at the other end of a UDS.

    Obtained via ``SO_PEERCRED``; this is the **only** authoritative source of
    caller identity. A request that claims an owner identity does not thereby
    become one.
    """

    pid: int
    uid: int
    gid: int


def read_peer_credentials(sock: socket.socket) -> PeerCredentials:
    """Return the kernel-verified credentials of ``sock``'s peer.

    Exposed as a module function so it can be substituted in tests that need to
    simulate a foreign peer without a second UID.
    """
    raw = sock.getsockopt(
        socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")
    )
    pid, uid, gid = struct.unpack("3i", raw)
    return PeerCredentials(pid=pid, uid=uid, gid=gid)


# --------------------------------------------------------------------------- #
# strict JSON
# --------------------------------------------------------------------------- #
def _reject_constant(value: str) -> Any:
    # ``json`` passes NaN/Infinity/-Infinity here.
    raise ValueError(f"non-finite JSON value {value!r} is not allowed")


def _strict_float(text: str) -> float:
    """Parse a JSON number, rejecting overflow to infinity.

    ``parse_constant`` only catches the literal ``NaN``/``Infinity`` tokens; a
    numeric literal such as ``1e400`` silently overflows to ``inf`` under the
    default ``float`` parser, so floats must be parsed through this hook.
    """
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


def _check_depth(obj: Any, limit: int) -> None:
    stack: list[tuple[Any, int]] = [(obj, 1)]
    while stack:
        node, depth = stack.pop()
        if isinstance(node, dict):
            if depth > limit:
                raise ValueError(f"JSON nesting deeper than {limit} is not allowed")
            for value in node.values():
                stack.append((value, depth + 1))
        elif isinstance(node, list):
            if depth > limit:
                raise ValueError(f"JSON nesting deeper than {limit} is not allowed")
            for value in node:
                stack.append((value, depth + 1))


def _load_strict_json(payload: bytes, *, what: str, max_depth: int) -> Any:
    """Decode ``payload`` as strict JSON without imposing a top-level shape.

    Rejects invalid UTF-8, invalid JSON, duplicate keys, non-finite numbers
    (including numeric overflow), and over-deep nesting. Raises
    :class:`RequestError`.
    """
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RequestError(f"{what} is not valid UTF-8") from exc
    try:
        obj = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
            parse_float=_strict_float,
        )
    except RecursionError as exc:
        raise RequestError(f"{what} JSON is nested too deeply") from exc
    except ValueError as exc:
        raise RequestError(f"{what} is not valid strict JSON") from exc
    try:
        _check_depth(obj, max_depth)
    except ValueError as exc:
        raise RequestError(str(exc)) from exc
    return obj


def parse_request(payload: bytes, *, max_depth: int = MAX_JSON_DEPTH) -> dict[str, Any]:
    """Parse one request payload as strict JSON and return a dict.

    Rejects: invalid UTF-8, invalid JSON, duplicate keys, non-finite numbers
    (``NaN``/``Infinity`` and overflow such as ``1e400``), nesting deeper than
    ``max_depth``, and any top-level value that is not a JSON object. Raises
    :class:`RequestError`.
    """
    obj = _load_strict_json(payload, what="request", max_depth=max_depth)
    if not isinstance(obj, dict):
        raise RequestError("request must be a JSON object")
    return obj


def _encode_json(obj: Any) -> bytes:
    """Serialise ``obj`` to compact UTF-8 JSON, refusing non-finite numbers.

    Raises :class:`RequestError` for unencodable values **and** for values whose
    nesting overflows the recursion limit, so a pathological object can never
    crash the caller.
    """
    try:
        data = json.dumps(obj, allow_nan=False, separators=(",", ":"))
    except RecursionError as exc:
        raise RequestError("value is nested too deeply to encode") from exc
    except (ValueError, TypeError) as exc:
        raise RequestError("value cannot be encoded as strict JSON") from exc
    return data.encode("utf-8")


# --------------------------------------------------------------------------- #
# paths + owner checks
# --------------------------------------------------------------------------- #
def runtime_dir_for(data_dir: str | os.PathLike[str]) -> str:
    """Return the owner-private runtime directory path for ``data_dir``."""
    return os.path.join(os.path.abspath(os.fspath(data_dir)), RUNTIME_DIR_NAME)


def _validate_basename(name: object, *, what: str) -> str:
    """Return ``name`` if it is a simple filename, else raise.

    Rejects non-strings, empty names, ``.``/``..``, absolute paths, path
    separators, and NUL bytes, so a caller cannot escape the runtime directory
    via a filename.
    """
    if not isinstance(name, str) or not name:
        raise IPCError(f"{what} must be a non-empty filename")
    if name in (".", "..") or "\x00" in name:
        raise IPCError(f"{what} {name!r} is not a valid filename")
    if os.path.isabs(name) or os.path.basename(name) != name:
        raise IPCError(
            f"{what} {name!r} must be a simple filename with no path components"
        )
    if os.sep in name or (os.altsep and os.altsep in name):
        raise IPCError(f"{what} {name!r} must not contain path separators")
    return name


def _validate_positive_int(value: object, *, what: str, cap: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise IPCError(f"{what} must be an integer")
    if value <= 0 or value > cap:
        raise IPCError(f"{what} must be between 1 and {cap}")
    return value


def _validate_timeout(value: object, *, what: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise IPCError(f"{what} must be a number")
    number = float(value)
    if not math.isfinite(number) or number <= 0 or number > MAX_TIMEOUT_SECONDS:
        raise IPCError(f"{what} must be a finite value in (0, {MAX_TIMEOUT_SECONDS}]")
    return number


class WorkerIPCServer:
    """Singleton, owner-authenticated UDS server for one data directory.

    Usage from an engine main loop::

        server = WorkerIPCServer(data_dir, handler)
        server.start()
        try:
            while not stop.is_set():
                server.serve_once(poll_timeout)   # bounded wait, no busy loop
        finally:
            server.close()

    ``handler(request: dict, peer: PeerCredentials) -> dict`` is invoked with the
    parsed request (untrusted data) and the **kernel-verified** peer identity.
    It must return a JSON-serialisable ``dict``; any exception it raises becomes
    a curated ``handler_error`` response with no reflected content.
    """

    def __init__(
        self,
        data_dir: str | os.PathLike[str],
        handler: Callable[[dict[str, Any], PeerCredentials], dict[str, Any]],
        *,
        socket_filename: str = DEFAULT_SOCKET_FILENAME,
        lock_filename: str = DEFAULT_LOCK_FILENAME,
        max_request_bytes: int = MAX_REQUEST_BYTES,
        io_timeout: float = DEFAULT_IO_TIMEOUT,
        owner_uid: int | None = None,
    ) -> None:
        if not callable(handler):
            raise IPCError("handler must be callable")
        socket_filename = _validate_basename(socket_filename, what="socket filename")
        lock_filename = _validate_basename(lock_filename, what="lock filename")
        if socket_filename == lock_filename:
            raise IPCError("socket filename and lock filename must differ")
        max_request_bytes = _validate_positive_int(
            max_request_bytes, what="max_request_bytes", cap=MAX_REQUEST_BYTES_CAP
        )
        io_timeout = _validate_timeout(io_timeout, what="io_timeout")
        self._data_dir = os.path.abspath(os.fspath(data_dir))
        self._handler = handler
        self._socket_filename = socket_filename
        self._lock_filename = lock_filename
        self._max_request_bytes = max_request_bytes
        self._io_timeout = io_timeout
        self._probe_timeout = min(io_timeout, DEFAULT_PROBE_TIMEOUT)
        self._owner_uid = os.getuid() if owner_uid is None else owner_uid
        self._listener: socket.socket | None = None
        self._lock_fd: int | None = None
        self._socket_ident: tuple[int, int] | None = None
        self._running = False

    # -- locations ----------------------------------------------------------- #
    @property
    def runtime_dir(self) -> str:
        return runtime_dir_for(self._data_dir)

    @property
    def socket_path(self) -> str:
        return os.path.join(self.runtime_dir, self._socket_filename)

    @property
    def lock_path(self) -> str:
        return os.path.join(self.runtime_dir, self._lock_filename)

    @property
    def is_running(self) -> bool:
        return self._running

    # -- safety helpers ------------------------------------------------------ #
    def _require_owner_private_dir(self, path: str, *, what: str) -> bool:
        """Validate an existing directory before any mutation.

        Returns ``False`` when the path does not exist. Otherwise it must be a
        real directory (never a symlink), owned by the expected user, and
        owner-private; anything else raises :class:`RuntimeDirectoryError`
        **without** creating or chmodding anything.
        """
        try:
            st = os.lstat(path)
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise RuntimeDirectoryError(
                f"{what} {path!r} cannot be inspected."
            ) from exc
        if stat.S_ISLNK(st.st_mode):
            raise RuntimeDirectoryError(
                f"refusing to use {what} {path!r}: it is a symlink."
            )
        if not stat.S_ISDIR(st.st_mode):
            raise RuntimeDirectoryError(
                f"refusing to use {what} {path!r}: it is not a directory."
            )
        if st.st_uid != self._owner_uid:
            raise RuntimeDirectoryError(
                f"{what} {path!r} is owned by uid {st.st_uid}, not the expected "
                f"uid {self._owner_uid}; refusing to use it."
            )
        if not paths.is_owner_private(st.st_mode):
            raise RuntimeDirectoryError(
                f"{what} {path!r} is not owner-private (mode "
                f"{stat.S_IMODE(st.st_mode):04o}); refusing to use a shared "
                f"directory rather than chmodding it."
            )
        return True

    def _create_owner_private_dir(self, path: str, *, what: str) -> None:
        """Create ``path`` (and parents) owner-private; only for missing paths."""
        try:
            os.makedirs(path, mode=_DIR_MODE, exist_ok=True)
            os.chmod(path, _DIR_MODE)
        except OSError as exc:
            raise RuntimeDirectoryError(
                f"cannot create {what} {path!r}: {exc.strerror}."
            ) from exc

    def _ensure_runtime_dir(self) -> None:
        # Validate everything that already exists *before* creating or chmodding
        # anything, so a foreign or shared path is never mutated.
        if not self._require_owner_private_dir(self._data_dir, what="data directory"):
            self._create_owner_private_dir(self._data_dir, what="data directory")
        if not self._require_owner_private_dir(
            self.runtime_dir, what="runtime directory"
        ):
            self._create_owner_private_dir(self.runtime_dir, what="runtime directory")
        # Re-validate after creation (and close any race window).
        self._require_owner_private_dir(self._data_dir, what="data directory")
        self._require_owner_private_dir(self.runtime_dir, what="runtime directory")

    def _acquire_lock(self) -> None:
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(
                self.lock_path,
                os.O_CREAT | os.O_RDWR | nofollow,
                _FILE_MODE,
            )
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise RuntimeDirectoryError(
                    f"refusing to use lock file {self.lock_path!r}: it is a symlink."
                ) from exc
            raise RuntimeDirectoryError(
                f"cannot open lock file {self.lock_path!r}: {exc.strerror}."
            ) from exc
        try:
            # Validate the *opened* file (not the path again) before chmodding:
            # a regular file, owned by us, with a single link (no hardlink into
            # a foreign location). O_NOFOLLOW already refused a symlink.
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):
                raise RuntimeDirectoryError(
                    f"refusing to use lock file {self.lock_path!r}: it is not a "
                    f"regular file."
                )
            if st.st_uid != self._owner_uid:
                raise RuntimeDirectoryError(
                    f"lock file {self.lock_path!r} is owned by uid {st.st_uid}, not "
                    f"the expected uid {self._owner_uid}; refusing to use it."
                )
            if st.st_nlink != 1:
                raise RuntimeDirectoryError(
                    f"refusing to use lock file {self.lock_path!r}: it has "
                    f"{st.st_nlink} hard links."
                )
            os.fchmod(fd, _FILE_MODE)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                if exc.errno in (errno.EWOULDBLOCK, errno.EAGAIN):
                    raise WorkerAlreadyRunning(
                        f"another worker already holds the control lock at "
                        f"{self.lock_path!r}; exactly one server is allowed per "
                        f"data directory."
                    ) from exc
                raise RuntimeDirectoryError(
                    f"cannot lock {self.lock_path!r}: "
                    f"{getattr(exc, 'strerror', exc)}."
                ) from exc
        except BaseException:
            os.close(fd)
            raise
        self._lock_fd = fd

    def _probe_socket_stale(self, path: str) -> bool | None:
        """Report whether the socket at ``path`` is definitively stale.

        ``True`` = connection refused (safe to remove), ``False`` = a live
        listener accepted the probe (must not be removed), ``None`` = unknown
        (timeout or any other error; must not be removed).
        """
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            probe.settimeout(self._probe_timeout)
            probe.connect(path)
        except (ConnectionRefusedError, FileNotFoundError):
            return True
        except OSError:
            return None
        else:
            return False
        finally:
            try:
                probe.close()
            except OSError:  # pragma: no cover - defensive
                pass

    def _prepare_socket_path(self) -> None:
        """Remove a definitively stale owned socket, refusing anything else.

        Called only while the singleton lock is held. A same-UID *non-worker*
        service that does not hold our lock may legitimately own this fixed
        path, so the existing socket is **probed**: only a connection-refused
        socket is removed, and only after an inode re-check. A live or
        unverifiable socket is left untouched.
        """
        path = self.socket_path
        if not os.path.lexists(path):
            return
        if os.path.islink(path):
            raise RuntimeDirectoryError(
                f"refusing to use socket path {path!r}: it is a symlink."
            )
        st = os.lstat(path)
        if st.st_uid != self._owner_uid:
            raise RuntimeDirectoryError(
                f"refusing to touch {path!r}: it is owned by uid {st.st_uid}, "
                f"not the expected uid {self._owner_uid}."
            )
        if not stat.S_ISSOCK(st.st_mode):
            raise RuntimeDirectoryError(
                f"refusing to delete {path!r}: it is not a socket (foreign "
                f"file at the control path)."
            )
        verdict = self._probe_socket_stale(path)
        if verdict is False:
            raise RuntimeDirectoryError(
                f"a live service is listening on {path!r}; refusing to remove a "
                f"socket that is not ours to reclaim."
            )
        if verdict is None:
            raise RuntimeDirectoryError(
                f"could not confirm {path!r} is a stale socket; refusing to "
                f"remove it."
            )
        # Re-check identity immediately before unlinking to avoid removing a
        # socket that was replaced while we probed.
        try:
            st2 = os.lstat(path)
        except FileNotFoundError:
            return
        if (
            (st2.st_dev, st2.st_ino) != (st.st_dev, st.st_ino)
            or not stat.S_ISSOCK(st2.st_mode)
            or st2.st_uid != self._owner_uid
        ):
            raise RuntimeDirectoryError(
                f"{path!r} changed while checking for staleness; refusing to "
                f"remove it."
            )
        os.unlink(path)

    # -- lifecycle ----------------------------------------------------------- #
    def start(self) -> None:
        """Become the sole server for this data directory.

        Raises :class:`WorkerAlreadyRunning` if another server holds the lock,
        or :class:`RuntimeDirectoryError` for any unsafe path.
        """
        if self._running:
            raise IPCError("server is already started")
        self._ensure_runtime_dir()
        self._acquire_lock()
        try:
            self._prepare_socket_path()
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                listener.bind(self.socket_path)
                os.chmod(self.socket_path, _FILE_MODE)
                listener.listen(8)
            except BaseException:
                listener.close()
                raise
        except BaseException:
            self._release_lock()
            raise
        st = os.lstat(self.socket_path)
        self._socket_ident = (st.st_dev, st.st_ino)
        self._listener = listener
        self._running = True

    def _release_lock(self) -> None:
        fd, self._lock_fd = self._lock_fd, None
        if fd is not None:
            try:
                os.close(fd)
            except OSError:  # pragma: no cover - defensive
                pass

    def close(self) -> None:
        """Stop serving, release the lock, and remove only our own socket."""
        self._running = False
        listener, self._listener = self._listener, None
        if listener is not None:
            try:
                listener.close()
            except OSError:  # pragma: no cover - defensive
                pass
        self._unlink_own_socket()
        self._release_lock()

    def _unlink_own_socket(self) -> None:
        """Unlink the socket only if it is still the inode we bound.

        If another instance has since replaced it, this leaves it untouched.
        """
        if self._socket_ident is None:
            return
        path = self.socket_path
        try:
            st = os.lstat(path)
        except OSError:
            return
        if not stat.S_ISSOCK(st.st_mode):
            return
        if (st.st_dev, st.st_ino) != self._socket_ident:
            return
        if st.st_uid != self._owner_uid:
            return
        try:
            os.unlink(path)
        except OSError:  # pragma: no cover - defensive
            pass

    def __enter__(self) -> "WorkerIPCServer":
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- serving ------------------------------------------------------------- #
    def serve_once(self, timeout: float | None = None) -> bool:
        """Accept and handle at most one connection.

        Waits up to ``timeout`` seconds (``None`` waits indefinitely). Returns
        ``True`` when a connection was accepted and handled, ``False`` on a
        timeout. A handled connection is always closed before returning.
        """
        if not self._running or self._listener is None:
            raise WorkerNotRunning("server is not running")
        self._listener.settimeout(timeout)
        try:
            conn, _ = self._listener.accept()
        except socket.timeout:
            return False
        except OSError as exc:
            if exc.errno in (errno.EINVAL, errno.EWOULDBLOCK):
                return False
            raise
        try:
            self._handle_connection(conn)
        finally:
            try:
                conn.close()
            except OSError:  # pragma: no cover - defensive
                pass
        return True

    def serve_forever(
        self,
        stop: Any = None,
        *,
        poll_timeout: float = 0.5,
    ) -> None:
        """Serve until ``stop`` (a ``threading.Event``-like) is set.

        Uses a bounded accept timeout between stop checks, so this is not a
        tight busy loop. ``stop=None`` runs until :meth:`close` is called from
        another thread.
        """
        while True:
            if stop is not None and stop.is_set():
                return
            try:
                self.serve_once(poll_timeout)
            except WorkerNotRunning:
                return
            except OSError:
                # A concurrent close() invalidated the listener; exit quietly.
                if not self._running:
                    return
                raise

    # -- connection handling ------------------------------------------------- #
    def _handle_connection(self, conn: socket.socket) -> None:
        conn.settimeout(self._io_timeout)
        try:
            line = self._read_bounded_request(conn)
        except socket.timeout:
            self._send_error(conn, _CODE_TIMEOUT, "the request timed out")
            return
        except _TooLarge:
            self._send_error(conn, _CODE_TOO_LARGE, "the request was too large")
            return
        except OSError:
            return
        if line is None:
            self._send_error(conn, _CODE_MALFORMED, "expected a JSON object")
            return
        # Restore a full I/O timeout for the response phase (the read loop
        # narrowed it to the request deadline).
        conn.settimeout(self._io_timeout)
        try:
            peer = read_peer_credentials(conn)
        except OSError:
            self._send_error(conn, _CODE_UNAUTHORIZED, "peer identity unavailable")
            return
        if peer.uid != self._owner_uid:
            self._send_error(
                conn, _CODE_UNAUTHORIZED, "control is restricted to the owner"
            )
            return
        try:
            request = parse_request(line)
        except RequestError:
            self._send_error(conn, _CODE_MALFORMED, "expected a JSON object")
            return
        try:
            result = self._handler(request, peer)
        except Exception:
            self._send_error(conn, _CODE_HANDLER, "the request could not be handled")
            return
        if not isinstance(result, dict):
            self._send_error(conn, _CODE_INVALID, "the handler returned an invalid result")
            return
        envelope = {"ok": True, "result": result}
        try:
            _check_depth(envelope, MAX_RESPONSE_DEPTH)
        except ValueError:
            self._send_error(conn, _CODE_INVALID, "the handler returned too deep a result")
            return
        try:
            encoded = _encode_json(envelope)
        except RequestError:
            self._send_error(conn, _CODE_INVALID, "the handler returned an invalid result")
            return
        if len(encoded) > MAX_RESPONSE_BYTES:
            self._send_error(conn, _CODE_INVALID, "the handler returned too large a result")
            return
        self._send_bytes(conn, encoded)

    def _read_bounded_request(self, conn: socket.socket) -> bytes | None:
        """Read one newline-delimited request, bounded by time and size.

        Returns the first line's bytes (without the newline), ``None`` on EOF
        before a newline, or raises :class:`_TooLarge` past the size limit. The
        whole read is bounded by ``io_timeout`` via a monotonic deadline, so a
        slow-drip sender cannot hold the connection (and the control loop) open
        indefinitely.
        """
        deadline = time.monotonic() + self._io_timeout
        buf = bytearray()
        limit = self._max_request_bytes
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise socket.timeout("request read deadline exceeded")
            conn.settimeout(remaining)
            chunk = conn.recv(4096)
            if not chunk:
                return None
            buf.extend(chunk)
            idx = buf.find(b"\n")
            if idx != -1:
                if idx > limit:
                    raise _TooLarge()
                return bytes(buf[:idx])
            if len(buf) > limit:
                raise _TooLarge()

    def _send_error(self, conn: socket.socket, code: str, message: str) -> None:
        try:
            conn.settimeout(self._io_timeout)
        except OSError:  # pragma: no cover - defensive
            pass
        self._send_bytes(
            conn, _encode_json({"ok": False, "error": {"code": code, "message": message}})
        )

    @staticmethod
    def _send_bytes(conn: socket.socket, payload: bytes) -> None:
        # Responses are newline-terminated too, so a reader can frame them
        # unambiguously; the connection is then closed.
        try:
            conn.sendall(payload + b"\n")
        except OSError:
            pass


class _TooLarge(Exception):
    """Internal signal: a request exceeded the size limit."""


# --------------------------------------------------------------------------- #
# client
# --------------------------------------------------------------------------- #
class WorkerIPCClient:
    """Client for the owner-only control socket.

    Verifies the **server's** peer UID via ``SO_PEERCRED`` before trusting a
    response, so a foreign process cannot impersonate the worker's socket.
    """

    def __init__(
        self,
        data_dir: str | os.PathLike[str],
        *,
        socket_filename: str = DEFAULT_SOCKET_FILENAME,
        io_timeout: float = DEFAULT_IO_TIMEOUT,
        owner_uid: int | None = None,
    ) -> None:
        self._data_dir = os.path.abspath(os.fspath(data_dir))
        self._socket_filename = _validate_basename(
            socket_filename, what="socket filename"
        )
        self._io_timeout = _validate_timeout(io_timeout, what="io_timeout")
        self._owner_uid = os.getuid() if owner_uid is None else owner_uid
        self._last_verified_peer: PeerCredentials | None = None

    @property
    def runtime_dir(self) -> str:
        return runtime_dir_for(self._data_dir)

    @property
    def socket_path(self) -> str:
        return os.path.join(self.runtime_dir, self._socket_filename)

    @property
    def last_verified_peer(self) -> PeerCredentials | None:
        """Kernel-verified credentials of the peer of the most recent request.

        ``None`` until a request has verified a peer; reset at the start of each
        request, so a caller can rely on it belonging to the request just made.
        Callers can bind an in-band PID from a response to this out-of-band,
        kernel-reported PID rather than trusting the response alone.
        """
        return self._last_verified_peer

    def request(
        self,
        request: dict[str, Any],
        *,
        timeout: float | None = None,
        expected_peer_pid: int | None = None,
    ) -> Any:
        """Send one request and return the handler's result.

        Raises :class:`WorkerNotRunning` if no worker is listening,
        :class:`PeerNotAuthorized` if the server is not the owner,
        :class:`PeerIdentityMismatch` if ``expected_peer_pid`` is supplied and
        the kernel-reported peer PID differs (checked **before** any control
        byte is sent), :class:`IPCRequestError` for a curated transport error
        response, and :class:`IPCError` for any other transport or protocol
        failure.
        """
        if not isinstance(request, dict):
            raise RequestError("request must be a JSON object")
        if expected_peer_pid is not None:
            if (
                isinstance(expected_peer_pid, bool)
                or not isinstance(expected_peer_pid, int)
                or expected_peer_pid <= 0
            ):
                raise IPCError("expected_peer_pid must be a positive process id")
        self._last_verified_peer = None
        payload = _encode_json(request) + b"\n"
        effective_timeout = (
            self._io_timeout
            if timeout is None
            else _validate_timeout(timeout, what="timeout")
        )
        path = self.socket_path
        if os.path.islink(path):
            raise RuntimeDirectoryError(f"refusing to use socket {path!r}: it is a symlink.")
        if not os.path.exists(path):
            raise WorkerNotRunning(f"no control socket at {path!r}.")
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.settimeout(effective_timeout)
            try:
                sock.connect(path)
            except (ConnectionRefusedError, FileNotFoundError) as exc:
                # A leftover socket with no listener is a stopped worker, not a
                # protocol failure; restart callers can recover from this.
                raise WorkerNotRunning(
                    f"no worker is listening at {path!r}."
                ) from exc
            except OSError as exc:
                raise IPCError(
                    f"could not connect to the control socket: "
                    f"{getattr(exc, 'strerror', exc)}."
                ) from exc
            peer = read_peer_credentials(sock)
            if peer.uid != self._owner_uid:
                raise PeerNotAuthorized(
                    "the control socket is not owned by this user; refusing to "
                    "trust its responses."
                )
            if expected_peer_pid is not None and peer.pid != expected_peer_pid:
                raise PeerIdentityMismatch(
                    "the control socket is not served by the expected process "
                    "(kernel-reported peer differs); refusing to send control."
                )
            self._last_verified_peer = peer
            sock.sendall(payload)
            line = self._read_bounded_response(sock, effective_timeout)
        finally:
            sock.close()
        return self._decode_response(line)

    @staticmethod
    def _decode_response(line: bytes | None) -> Any:
        if line is None:
            raise IPCError("the server closed the connection without a response")
        try:
            response = _load_strict_json(
                line, what="response", max_depth=MAX_RESPONSE_DEPTH
            )
        except RequestError as exc:
            raise IPCError("the server returned an invalid response") from exc
        if not isinstance(response, dict):
            raise IPCError("the server returned an invalid response")
        ok = response.get("ok")
        if ok is True:
            if "result" not in response:
                raise IPCError("the server returned an invalid response")
            result = response["result"]
            # The server contract is a dict result; anything else is invalid.
            if not isinstance(result, dict):
                raise IPCError("the server returned an invalid response")
            return result
        if ok is False:
            error = response.get("error")
            code = error.get("code") if isinstance(error, dict) else None
            # Only a string code from the static transport allowlist is
            # surfaced, always with our own static message; the peer-supplied
            # message is never reflected, and an unhashable/unknown code yields
            # a clean IPCError rather than a TypeError.
            if isinstance(code, str) and code in _CURATED_MESSAGES:
                raise IPCRequestError(code, _CURATED_MESSAGES[code])
            raise IPCError("the server rejected the request")
        # Not a real boolean: never coerce truthiness from untrusted input.
        raise IPCError("the server returned an invalid response")

    def _read_bounded_response(self, conn: socket.socket, timeout: float) -> bytes | None:
        """Read one newline-terminated response, bounded by time and size.

        The whole read shares one monotonic deadline (a slow-drip sender cannot
        reset it chunk by chunk), and the size cap is enforced before any line
        is returned.
        """
        deadline = time.monotonic() + timeout
        buf = bytearray()
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise IPCError("the server response timed out")
            conn.settimeout(remaining)
            try:
                chunk = conn.recv(4096)
            except socket.timeout:
                raise IPCError("the server response timed out") from None
            except OSError as exc:
                raise IPCError("the server connection failed") from exc
            if not chunk:
                return bytes(buf) if buf else None
            buf.extend(chunk)
            idx = buf.find(b"\n")
            if idx != -1:
                if idx > MAX_RESPONSE_BYTES:
                    raise IPCError("the server response was too large")
                return bytes(buf[:idx])
            if len(buf) > MAX_RESPONSE_BYTES:
                raise IPCError("the server response was too large")
