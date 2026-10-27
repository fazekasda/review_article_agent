#!/usr/bin/env bash
# shellcheck shell=bash
#
# lib.sh — shared functions for the disposable, app-owned OpenCode V2 service
# harness (step 00 runtime spike).
#
# Design constraints (see README.md):
#   * Start, stop, restart, discover ONLY an app-owned foreground `opencode serve`
#     instance. Never register or restart the ordinary shared background service.
#   * Give the child a unique HOME + XDG_* tree under $HARNESS_ROOT and a working
#     directory outside every repository ancestor.
#   * Rebuild the child environment from an explicit allowlist so inherited
#     OPENCODE_* / OPENCHAMBER_* / AI_AGENT variables cannot leak into it.
#   * Loopback-only bind (127.0.0.1) plus a per-run HTTP Basic password.
#   * No provider credentials are ever injected and no model call is triggered.
#
# Callers choose their own strict mode (harness.sh uses `set -euo pipefail`; the
# smoke test uses `set -uo pipefail` so it can record each assertion).

# ---------------------------------------------------------------------------
# Paths and constants
# ---------------------------------------------------------------------------

# Explicit binary. Prefer HARNESS_OPENCODE_BIN, else the documented PATH lookup.
harness_resolve_bin() {
  local bin="${HARNESS_OPENCODE_BIN:-}"
  if [[ -z "$bin" ]]; then
    bin="$(command -v opencode || true)"
  fi
  [[ -n "$bin" && -x "$bin" ]] || harness_die "opencode binary not found (set HARNESS_OPENCODE_BIN)"
  printf '%s\n' "$bin"
}

HARNESS_BIN="$(harness_resolve_bin)"
HARNESS_BIN_DIR="$(cd "$(dirname "$HARNESS_BIN")" && pwd)"
# Minimal allowlisted PATH: the opencode binary's own directory (for its bundled
# node/runtime resolution) plus system locations. Nothing from the caller's PATH
# is trusted wholesale — only these prefixes pass through.
HARNESS_PATH="${HARNESS_BIN_DIR}:/usr/local/bin:/usr/bin:/bin"

# Scratch root. MUST live outside the repository (and outside any ancestor of it)
# so project/ancestor config discovery cannot reach it. Default under /tmp/opencode
# which is approved for external access.
harness_default_root() {
  printf '%s\n' "${HARNESS_ROOT:-${TMPDIR:-/tmp}/opencode/spike-harness}"
}

HARNESS_ROOT="$(harness_default_root)"
HARNESS_HOME="$HARNESS_ROOT/home"
HARNESS_XDG_CONFIG="$HARNESS_ROOT/config"
HARNESS_XDG_DATA="$HARNESS_ROOT/data"
HARNESS_XDG_STATE="$HARNESS_ROOT/state"
HARNESS_XDG_CACHE="$HARNESS_ROOT/cache"
HARNESS_CWD="$HARNESS_ROOT/cwd"
HARNESS_RUN="$HARNESS_ROOT/run"

HARNESS_PID_FILE="$HARNESS_RUN/pid"
HARNESS_PORT_FILE="$HARNESS_RUN/port"
HARNESS_SECRET_FILE="$HARNESS_RUN/password"
HARNESS_DESCRIPTOR="$HARNESS_RUN/instance.json"
HARNESS_STDOUT="$HARNESS_RUN/serve.out"
HARNESS_STDERR="$HARNESS_RUN/serve.err"
HARNESS_OPENAPI="$HARNESS_RUN/openapi.json"
HARNESS_GUARD_FILE="$HARNESS_RUN/ordinary-service.before.json"

HARNESS_USERNAME="opencode"   # documented HTTP Basic user for `opencode serve`
HARNESS_HOST="127.0.0.1"

# ---------------------------------------------------------------------------
# Logging / helpers
# ---------------------------------------------------------------------------

harness_log()  { printf '[harness] %s\n' "$*" >&2; }
harness_die()  { printf '[harness][error] %s\n' "$*" >&2; exit 1; }

harness_repo_toplevel() {
  git -C "$(dirname "${BASH_SOURCE[0]}")" rev-parse --show-toplevel 2>/dev/null || true
}

# Guard: refuse to run if the scratch root is inside the repository, or if it is
# itself an ancestor of the repository (either would expose project config).
harness_assert_root_safe() {
  local toplevel root_real
  toplevel="$(harness_repo_toplevel)"
  # Resolve without creating anything, so a rejected root leaves no artefacts.
  root_real="$(realpath -m -- "$HARNESS_ROOT")"
  if [[ -n "$toplevel" ]]; then
    toplevel="$(cd "$toplevel" && pwd -P)"
    case "$root_real" in
      "$toplevel"|"$toplevel"/*)
        harness_die "scratch root $root_real is inside the repository ($toplevel)" ;;
    esac
    # Is the scratch root an ancestor of the repo? (e.g. HARNESS_ROOT=/home)
    case "$toplevel" in
      "$root_real"|"$root_real"/*)
        harness_die "scratch root $root_real is an ancestor of the repository ($toplevel)" ;;
    esac
  fi
  # Never reuse the standard shared-service / config / data locations.
  case "$root_real" in
    "$HOME/.local/state/opencode"|"$HOME/.local/share/opencode"|"$HOME/.config/opencode"|*"/.local/state/opencode"*|*"/.local/share/opencode"*)
      harness_die "scratch root $root_real overlaps the ordinary OpenCode locations" ;;
  esac
}

harness_free_port() {
  python3 - <<'PY'
import socket
s = socket.socket()
s.bind(("127.0.0.1", 0))
print(s.getsockname()[1])
s.close()
PY
}

harness_gen_password() {
  python3 - <<'PY'
import secrets
print(secrets.token_urlsafe(32))
PY
}

# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

harness_is_running() {
  [[ -f "$HARNESS_PID_FILE" ]] || return 1
  local pid; pid="$(cat "$HARNESS_PID_FILE" 2>/dev/null || true)"
  [[ -n "$pid" ]] || return 1
  kill -0 "$pid" 2>/dev/null
}

harness_current_pid()  { [[ -f "$HARNESS_PID_FILE" ]] && cat "$HARNESS_PID_FILE" || true; }
harness_current_port() { [[ -f "$HARNESS_PORT_FILE" ]] && cat "$HARNESS_PORT_FILE" || true; }
harness_current_url()  {
  local p; p="$(harness_current_port)"
  [[ -n "$p" ]] && printf 'http://%s:%s\n' "$HARNESS_HOST" "$p"
}

# curl with the per-run HTTP Basic credentials.
harness_req() {
  local url="$1"; shift
  local pw; pw="$(cat "$HARNESS_SECRET_FILE")"
  curl -sS --max-time 15 -u "$HARNESS_USERNAME:$pw" "$@" "$url"
}

harness_start() {
  harness_assert_root_safe
  if harness_is_running; then
    harness_log "already running (pid $(harness_current_pid))"
    return 0
  fi

  mkdir -p "$HARNESS_HOME" "$HARNESS_XDG_CONFIG" "$HARNESS_XDG_DATA" \
           "$HARNESS_XDG_STATE" "$HARNESS_XDG_CACHE" "$HARNESS_CWD" "$HARNESS_RUN"
  chmod 700 "$HARNESS_HOME" "$HARNESS_XDG_CONFIG" "$HARNESS_XDG_DATA" \
           "$HARNESS_XDG_STATE" "$HARNESS_XDG_CACHE"

  local port pw
  port="$(harness_free_port)"
  pw="$(harness_gen_password)"
  umask 077
  printf '%s' "$pw" > "$HARNESS_SECRET_FILE"
  umask 022
  printf '%s' "$port" > "$HARNESS_PORT_FILE"

  # Rebuild the environment from an allowlist. `env -i` discards the caller's
  # environment entirely, including OPENCODE_CONFIG / OPENCODE_PASSWORD /
  # OPENCODE_SERVER_PASSWORD / OPENCHAMBER_* / AI_AGENT, so nothing inherited can
  # alter this instance or reach the ordinary shared service.
  env -i \
    PATH="$HARNESS_PATH" \
    HOME="$HARNESS_HOME" \
    XDG_CONFIG_HOME="$HARNESS_XDG_CONFIG" \
    XDG_DATA_HOME="$HARNESS_XDG_DATA" \
    XDG_STATE_HOME="$HARNESS_XDG_STATE" \
    XDG_CACHE_HOME="$HARNESS_XDG_CACHE" \
    TMPDIR="$HARNESS_RUN/tmp" \
    OPENCODE_PASSWORD="$pw" \
    bash -c "cd '$HARNESS_CWD' && exec '$HARNESS_BIN' serve --hostname '$HARNESS_HOST' --port '$port'" \
    > "$HARNESS_STDOUT" 2> "$HARNESS_STDERR" &
  local pid=$!
  printf '%s' "$pid" > "$HARNESS_PID_FILE"
  mkdir -p "$HARNESS_RUN/tmp"

  local url="http://$HARNESS_HOST:$port" deadline=$((SECONDS + 30))
  while (( SECONDS < deadline )); do
    if ! kill -0 "$pid" 2>/dev/null; then
      harness_log "server process exited during startup; stderr:"; cat "$HARNESS_STDERR" >&2 || true
      harness_die "failed to start isolated server"
    fi
    if curl -sS --max-time 3 -u "$HARNESS_USERNAME:$pw" -o /dev/null "$url/api/info" 2>/dev/null; then
      harness_write_descriptor "$pid" "$url"
      harness_log "started pid=$pid url=$url (loopback, basic auth, isolated HOME/XDG)"
      return 0
    fi
    sleep 0.3
  done
  harness_kill_pid "$pid"
  harness_die "timed out waiting for /api/info"
}

harness_write_descriptor() {
  local pid="$1" url="$2"
  python3 - "$HARNESS_DESCRIPTOR" "$url" "$pid" "$HARNESS_SECRET_FILE" \
           "$HARNESS_HOME" "$HARNESS_XDG_CONFIG" "$HARNESS_XDG_DATA" \
           "$HARNESS_XDG_STATE" "$HARNESS_XDG_CACHE" "$HARNESS_CWD" "$HARNESS_BIN" <<'PY'
import json, os, sys
out, url, pid, secret, home, cfg, data, state, cache, cwd, binn = sys.argv[1:12]
version = "unknown"
try:
    import urllib.request, base64
    req = urllib.request.Request(url + "/api/info")
    pw = open(secret).read()
    req.add_header("Authorization", "Basic " + base64.b64encode(("opencode:" + pw).encode()).decode())
    with urllib.request.urlopen(req, timeout=5) as r:
        version = json.load(r).get("version", "unknown")
except Exception:
    pass
doc = {
    "descriptorVersion": 1,
    "kind": "app-owned-disposable",
    "binary": binn,
    "opencodeVersion": version,
    "url": url,
    "pid": int(pid),
    "auth": {
        "scheme": "http-basic",
        "username": "opencode",
        "passwordFile": os.path.abspath(secret),
    },
    "dirs": {
        "home": home, "xdgConfigHome": cfg, "xdgDataHome": data,
        "xdgStateHome": state, "xdgCacheHome": cache, "cwd": cwd,
    },
    "documentedRegistration": os.path.expanduser("~/.local/state/opencode/service.json"),
    "note": "This instance writes no standard service registration. Read auth from passwordFile; do not print it.",
}
os.umask(0o077)
with open(out, "w") as fh:
    json.dump(doc, fh, indent=2)
    fh.write("\n")
PY
  chmod 600 "$HARNESS_DESCRIPTOR"
}

harness_kill_pid() {
  local pid="$1" deadline
  kill -0 "$pid" 2>/dev/null || return 0
  kill -TERM "$pid" 2>/dev/null || true
  deadline=$((SECONDS + 15))
  while kill -0 "$pid" 2>/dev/null && (( SECONDS < deadline )); do sleep 0.2; done
  if kill -0 "$pid" 2>/dev/null; then
    kill -KILL "$pid" 2>/dev/null || true
  fi
}

harness_stop() {
  if harness_is_running; then
    local pid; pid="$(harness_current_pid)"
    harness_log "stopping pid=$pid"
    harness_kill_pid "$pid"
    # Reap if it is our child; ignore if not.
    wait "$pid" 2>/dev/null || true
    harness_log "stopped"
  else
    harness_log "not running"
  fi
  rm -f "$HARNESS_PID_FILE" "$HARNESS_PORT_FILE" "$HARNESS_DESCRIPTOR" "$HARNESS_SECRET_FILE"
}

harness_restart() {
  harness_log "restart (same HOME/XDG/data; only the process is replaced)"
  harness_stop
  harness_start
}

harness_status() {
  if harness_is_running; then
    local url; url="$(harness_current_url)"
    local code
    code="$(curl -sS --max-time 5 -o /dev/null -w '%{http_code}' \
      -u "$HARNESS_USERNAME:$(cat "$HARNESS_SECRET_FILE")" "$url/api/info" 2>/dev/null || echo 000)"
    printf 'running pid=%s url=%s /api/info=%s\n' "$(harness_current_pid)" "$url" "$code"
    return 0
  fi
  printf 'stopped\n'
  return 1
}

harness_discover() {
  [[ -f "$HARNESS_DESCRIPTOR" ]] || harness_die "no instance descriptor (start it first)"
  cat "$HARNESS_DESCRIPTOR"
}

# Remove every scratch artefact. Safe to run when already stopped.
harness_cleanup() {
  if harness_is_running; then harness_stop; fi
  rm -rf "$HARNESS_ROOT"
  harness_log "removed scratch root $HARNESS_ROOT"
}
