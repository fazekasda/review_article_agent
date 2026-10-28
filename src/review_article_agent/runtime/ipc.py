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
import os
import socket
import stat
import struct
import time
from dataclasses import dataclass
from typing import Any, Callable

from ..domain.errors import ProjectPathError, ReviewAgentError
from ..storage import paths

__all__ = [
    "IPCError",
    "RuntimeDirectoryError",
    "WorkerAlreadyRunning",
    "WorkerNotRunning",
    "PeerNotAuthorized",
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

#: Default per-connection / connect I/O timeout, in seconds.
DEFAULT_IO_TIMEOUT = 5.0

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


def parse_request(payload: bytes, *, max_depth: int = MAX_JSON_DEPTH) -> dict[str, Any]:
    """Parse one request payload as strict JSON and return a dict.

    Rejects: invalid UTF-8, invalid JSON, duplicate keys, non-finite numbers
    (``NaN``/``Infinity``), nesting deeper than ``max_depth``, and any top-level
    value that is not a JSON object. Raises :class:`RequestError`.
    """
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RequestError("request is not valid UTF-8") from exc
    try:
        obj = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
        )
    except RecursionError as exc:
        raise RequestError("request JSON is nested too deeply") from exc
    except ValueError as exc:
        raise RequestError("request is not valid strict JSON") from exc
    if not isinstance(obj, dict):
        raise RequestError("request must be a JSON object")
    try:
        _check_depth(obj, max_depth)
    except ValueError as exc:
        raise RequestError(str(exc)) from exc
    return obj


def _encode_json(obj: Any) -> bytes:
    """Serialise ``obj`` to compact UTF-8 JSON, refusing non-finite numbers."""
    try:
        return json.dumps(obj, allow_nan=False, separators=(",", ":")).encode("utf-8")
    except (ValueError, TypeError) as exc:
        raise RequestError("value cannot be encoded as strict JSON") from exc


# --------------------------------------------------------------------------- #
# paths + owner checks
# --------------------------------------------------------------------------- #
def runtime_dir_for(data_dir: str | os.PathLike[str]) -> str:
    """Return the owner-private runtime directory path for ``data_dir``."""
    return os.path.join(os.path.abspath(os.fspath(data_dir)), RUNTIME_DIR_NAME)


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
        self._data_dir = os.path.abspath(os.fspath(data_dir))
        self._handler = handler
        self._socket_filename = socket_filename
        self._lock_filename = lock_filename
        self._max_request_bytes = max_request_bytes
        self._io_timeout = io_timeout
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
    def _assert_owned_by_us(self, path: str, *, what: str) -> os.stat_result:
        st = os.lstat(path)
        if st.st_uid != self._owner_uid:
            raise RuntimeDirectoryError(
                f"{what} {path!r} is owned by uid {st.st_uid}, not the expected "
                f"uid {self._owner_uid}; refusing to use it."
            )
        return st

    def _ensure_runtime_dir(self) -> None:
        # The explicit data directory must not be a symlink (it would redirect
        # every app-owned file to a foreign location).
        try:
            paths.assert_not_symlink(self._data_dir, what="data directory")
            paths.ensure_owner_private_dir(self.runtime_dir)
            paths.assert_owner_private_dir(self.runtime_dir)
        except ProjectPathError as exc:
            raise RuntimeDirectoryError(str(exc)) from exc
        if os.path.lexists(self._data_dir):
            self._assert_owned_by_us(self._data_dir, what="data directory")
        self._assert_owned_by_us(self.runtime_dir, what="runtime directory")

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
            self._assert_owned_by_us(self.lock_path, what="lock file")
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

    def _prepare_socket_path(self) -> None:
        """Remove a stale owned socket, refusing anything else.

        Called only while the singleton lock is held, so a still-live server
        cannot own the path.
        """
        path = self.socket_path
        if os.path.islink(path):
            raise RuntimeDirectoryError(
                f"refusing to use socket path {path!r}: it is a symlink."
            )
        if os.path.lexists(path):
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
        try:
            encoded = _encode_json({"ok": True, "result": result})
        except RequestError:
            self._send_error(conn, _CODE_INVALID, "the handler returned an invalid result")
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
        self._socket_filename = socket_filename
        self._io_timeout = io_timeout
        self._owner_uid = os.getuid() if owner_uid is None else owner_uid

    @property
    def runtime_dir(self) -> str:
        return runtime_dir_for(self._data_dir)

    @property
    def socket_path(self) -> str:
        return os.path.join(self.runtime_dir, self._socket_filename)

    def request(self, request: dict[str, Any], *, timeout: float | None = None) -> Any:
        """Send one request and return the handler's result.

        Raises :class:`WorkerNotRunning` if no socket exists,
        :class:`PeerNotAuthorized` if the server is not the owner, and
        :class:`IPCRequestError` for a curated server error response.
        """
        if not isinstance(request, dict):
            raise RequestError("request must be a JSON object")
        payload = _encode_json(request) + b"\n"
        path = self.socket_path
        if os.path.islink(path):
            raise RuntimeDirectoryError(f"refusing to use socket {path!r}: it is a symlink.")
        if not os.path.exists(path):
            raise WorkerNotRunning(f"no control socket at {path!r}.")
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.settimeout(self._io_timeout if timeout is None else timeout)
            sock.connect(path)
            peer = read_peer_credentials(sock)
            if peer.uid != self._owner_uid:
                raise PeerNotAuthorized(
                    "the control socket is not owned by this user; refusing to "
                    "trust its responses."
                )
            sock.sendall(payload)
            line = self._read_bounded_response(sock)
        finally:
            sock.close()
        if line is None:
            raise IPCError("the server closed the connection without a response")
        try:
            response = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise IPCError("the server returned an invalid response") from exc
        if not isinstance(response, dict) or "ok" not in response:
            raise IPCError("the server returned an invalid response")
        if not response["ok"]:
            error = response.get("error") or {}
            raise IPCRequestError(
                str(error.get("code", "error")),
                str(error.get("message", "the request was rejected")),
            )
        return response.get("result")

    def _read_bounded_response(self, conn: socket.socket) -> bytes | None:
        buf = bytearray()
        while True:
            chunk = conn.recv(4096)
            if not chunk:
                return bytes(buf) if buf else None
            buf.extend(chunk)
            idx = buf.find(b"\n")
            if idx != -1:
                return bytes(buf[:idx])
            if len(buf) > MAX_RESPONSE_BYTES:
                return None
