#!/usr/bin/env bash
# shellcheck shell=bash
#
# lib.sh — shared functions for the disposable, app-owned OpenCode V2 service
# harness (step 00 runtime spike). Sources: docs/implementation/00-runtime-spike.md
# and docs/specification.md ("Separate local worker…", "no access/config changes
# to ordinary coding sessions").
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
# Safety model (repair 1): every mutating operation (start/stop/restart/cleanup)
# is gated by ownership of the root and by proof of process identity. Signals are
# sent only to a process whose /proc identity matches the recorded instance
# (start time, exact binary, cwd, HOME, per-run password). Cleanup refuses to
# `rm -rf` any tree it did not create and mark.
#
# Callers choose their own strict mode (harness.sh uses `set -euo pipefail`; the
# tests use `set -uo pipefail` so they can record each assertion).

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

HARNESS_USERNAME="opencode"   # documented HTTP Basic user for `opencode serve`
HARNESS_HOST="127.0.0.1"
HARNESS_MARKER_NAME=".harness-owned.json"
HARNESS_MARKER_KIND="app-owned-disposable-harness"

# ---------------------------------------------------------------------------
# Paths (derived from HARNESS_ROOT, which callers may override)
# ---------------------------------------------------------------------------

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

HARNESS_MARKER="$HARNESS_ROOT/$HARNESS_MARKER_NAME"
HARNESS_PIDREC="$HARNESS_RUN/instance.pid.json"
HARNESS_PORT_FILE="$HARNESS_RUN/port"
HARNESS_SECRET_FILE="$HARNESS_RUN/password"
HARNESS_DESCRIPTOR="$HARNESS_RUN/instance.json"
HARNESS_STDOUT="$HARNESS_RUN/serve.out"
HARNESS_STDERR="$HARNESS_RUN/serve.err"
HARNESS_OPENAPI="$HARNESS_RUN/openapi.json"
HARNESS_LOCK_FILE="${HARNESS_ROOT}.lock"

# ---------------------------------------------------------------------------
# Logging / tiny helpers
# ---------------------------------------------------------------------------

harness_log()  { printf '[harness] %s\n' "$*" >&2; }
harness_die()  { printf '[harness][error] %s\n' "$*" >&2; exit 1; }

harness_repo_toplevel() {
  git -C "$(dirname "${BASH_SOURCE[0]}")" rev-parse --show-toplevel 2>/dev/null || true
}

# True when paths a and b are equal, or one is inside the other.
harness_paths_overlap() {
  local a="$1" b="$2"
  [[ "$a" == "$b" || "$a" == "$b"/* || "$b" == "$a"/* ]]
}

# ---------------------------------------------------------------------------
# Environment / binary resolution
# ---------------------------------------------------------------------------

harness_resolve_bin() {
  local bin="${HARNESS_OPENCODE_BIN:-}"
  if [[ -z "$bin" ]]; then
    bin="$(command -v opencode || true)"
  fi
  [[ -n "$bin" && -x "$bin" ]] || harness_die "opencode binary not found (set HARNESS_OPENCODE_BIN)"
  printf '%s\n' "$bin"
}

HARNESS_BIN="$(harness_resolve_bin)"
HARNESS_BIN_REAL="$(realpath -- "$HARNESS_BIN")"
HARNESS_BIN_DIR="$(cd "$(dirname "$HARNESS_BIN")" && pwd)"
# Minimal allowlisted PATH: the binary's own directory plus system locations.
HARNESS_PATH="${HARNESS_BIN_DIR}:/usr/local/bin:/usr/bin:/bin"

# ---------------------------------------------------------------------------
# Root safety (non-mutating)
# ---------------------------------------------------------------------------

# Directories whose exposure would let the child discover unowned
# configuration/instructions through its HOME or cwd ancestors.
harness_sensitive_dirs() {
  printf '%s\n' \
    "$HOME/.config" \
    "$HOME/.local" \
    "$HOME/.cache" \
    "$HOME/.opencode" \
    "$HOME/opencode.json" \
    "$HOME/opencode.jsonc" \
    "$HOME/.config/opencode" \
    "$HOME/.local/state/opencode" \
    "$HOME/.local/share/opencode" \
    "/etc" "/usr" "/var" "/home" "/root"
}

# Reject symlinked roots, dangerous ancestors, and any ancestor that already
# holds OpenCode configuration/instructions. Never creates anything.
harness_assert_root_safe() {
  local root_real toplevel anc found=0
  [[ -L "$HARNESS_ROOT" ]] && harness_die "refusing symlinked HARNESS_ROOT: $HARNESS_ROOT"
  root_real="$(realpath -m -- "$HARNESS_ROOT")"

  # Root must be a real, reasonably deep absolute path.
  [[ "$root_real" == /* ]] || harness_die "HARNESS_ROOT must be absolute: $HARNESS_ROOT"
  [[ "$root_real" == "/" || "$root_real" == "/tmp" || "$root_real" == "/home" || "$root_real" == "$HOME" ]] \
    && harness_die "refusing unsafe HARNESS_ROOT: $root_real"

  toplevel="$(harness_repo_toplevel)"
  if [[ -n "$toplevel" ]]; then
    toplevel="$(cd "$toplevel" && pwd -P)"
    harness_paths_overlap "$root_real" "$toplevel" \
      && harness_die "scratch root $root_real overlaps the repository ($toplevel)"
  fi

  local sensitive
  while IFS= read -r sensitive; do
    [[ -n "$sensitive" ]] || continue
    harness_paths_overlap "$root_real" "$(realpath -m -- "$sensitive")" \
      && harness_die "scratch root $root_real overlaps a protected path ($sensitive)"
  done < <(harness_sensitive_dirs)

  # Ancestor config/instructions discovery risk: root and its ancestors (above
  # the root itself) must not contain an OpenCode config document or AGENTS.md.
  anc="$(dirname "$root_real")"
  while [[ -n "$anc" && "$anc" != "/" ]]; do
    for f in opencode.json opencode.jsonc AGENTS.md .opencode; do
      if [[ -e "$anc/$f" ]]; then
        harness_die "scratch root ancestor $anc contains '$f'; the child could discover unowned config (choose another HARNESS_ROOT)"
      fi
    done
    anc="$(dirname "$anc")"
  done
  return 0
}

# ---------------------------------------------------------------------------
# Ownership marker
# ---------------------------------------------------------------------------

harness_write_marker() {
  local root_real instance_id
  root_real="$(realpath -m -- "$HARNESS_ROOT")"
  instance_id="$(python3 -c 'import secrets;print(secrets.token_hex(16))')"
  umask 077
  python3 - "$HARNESS_MARKER" "$root_real" "$instance_id" <<'PY'
import json, os, sys, time
marker, root, iid = sys.argv[1:4]
doc = {
    "kind": "app-owned-disposable-harness",
    "markerVersion": 1,
    "root": root,
    "ownerUid": os.getuid(),
    "instanceId": iid,
    "createdAtMs": int(time.time() * 1000),
}
with open(marker, "w") as fh:
    json.dump(doc, fh, indent=2)
    fh.write("\n")
PY
  chmod 600 "$HARNESS_MARKER"
  chmod 700 "$HARNESS_ROOT"
  umask 022
}

# 0 iff the root exists and carries a valid marker for this user + this path.
harness_require_marker() {
  [[ -f "$HARNESS_MARKER" && ! -L "$HARNESS_MARKER" ]] || return 1
  local root_real
  root_real="$(realpath -m -- "$HARNESS_ROOT")"
  python3 - "$HARNESS_MARKER" "$root_real" "$HARNESS_MARKER_KIND" <<'PY'
import json, os, sys
path, root, kind = sys.argv[1:4]
try:
    doc = json.load(open(path))
except Exception:
    sys.exit(1)
ok = (doc.get("kind") == kind and doc.get("root") == root
      and doc.get("ownerUid") == os.getuid())
sys.exit(0 if ok else 1)
PY
}

harness_marker_field() {
  python3 - "$HARNESS_MARKER" "$1" <<'PY' 2>/dev/null
import json, sys
print(json.load(open(sys.argv[1])).get(sys.argv[2], ""))
PY
}

# ---------------------------------------------------------------------------
# Concurrency exclusion
# ---------------------------------------------------------------------------

harness_lock() {
  [[ -n "${HARNESS_LOCK_FD_HELD:-}" ]] && return 0
  mkdir -p "$(dirname "$HARNESS_LOCK_FILE")"
  exec 9>"$HARNESS_LOCK_FILE"
  flock -w 15 9 || harness_die "another harness operation holds $HARNESS_LOCK_FILE"
  HARNESS_LOCK_FD_HELD=1
}

# ---------------------------------------------------------------------------
# Process identity (Linux /proc)
# ---------------------------------------------------------------------------

harness_proc_starttime() {
  local pid="$1" rest
  rest="$(sed 's/^[0-9]* ([^)]*) //' "/proc/$pid/stat" 2>/dev/null)" || return 1
  awk '{print $20}' <<<"$rest"
}

harness_proc_cwd()  { readlink -f "/proc/$1/cwd" 2>/dev/null; }
harness_proc_exe()  { readlink -f "/proc/$1/exe" 2>/dev/null; }
harness_proc_env()  { tr '\0' '\n' < "/proc/$1/environ" 2>/dev/null; }
harness_proc_env_value() {
  harness_proc_env "$1" | sed -n "s/^$2=//p" | head -n1 || true
}

# 0 iff a live process matches the recorded instance identity exactly.
harness_verify_live_owned() {
  [[ -f "$HARNESS_PIDREC" ]] || return 1
  local pid rec_pid rec_st rec_exe rec_cwd rec_home rec_pw
  read -r rec_pid rec_st rec_exe rec_cwd rec_home rec_pw < <(python3 - "$HARNESS_PIDREC" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
print(d.get("pid"), d.get("starttime"), d.get("exe"), d.get("cwd"), d.get("home"), d.get("passwordSha256"))
PY
) || return 1
  [[ -n "$rec_pid" ]] || return 1
  kill -0 "$rec_pid" 2>/dev/null || return 1
  [[ "$(harness_proc_starttime "$rec_pid")" == "$rec_st" ]] || return 1
  [[ "$(harness_proc_exe "$rec_pid")" == "$rec_exe" ]] || return 1
  [[ "$(harness_proc_cwd "$rec_pid")" == "$rec_cwd" ]] || return 1
  [[ "$(harness_proc_env_value "$rec_pid" HOME)" == "$rec_home" ]] || return 1
  [[ -f "$HARNESS_SECRET_FILE" ]] || return 1
  # Direct, exact comparison of the per-run password (command substitution strips
  # the trailing newline from both sides, so no hashing ambiguity).
  [[ "$(harness_proc_env_value "$rec_pid" OPENCODE_PASSWORD)" == "$(cat "$HARNESS_SECRET_FILE")" ]] || return 1
  return 0
}

# none | stopped | ours | foreign
harness_process_state() {
  if [[ ! -f "$HARNESS_PIDREC" ]]; then printf 'none\n'; return; fi
  if harness_verify_live_owned; then printf 'ours\n'; return; fi
  local pid
  pid="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1])).get("pid",""))' "$HARNESS_PIDREC" 2>/dev/null)"
  if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then printf 'foreign\n'; else printf 'stopped\n'; fi
}

harness_is_running() { [[ "$(harness_process_state)" == "ours" ]]; }

harness_current_pid() {
  [[ -f "$HARNESS_PIDREC" ]] && python3 -c 'import json,sys;print(json.load(open(sys.argv[1])).get("pid",""))' "$HARNESS_PIDREC" 2>/dev/null || true
}
harness_current_port() { [[ -f "$HARNESS_PORT_FILE" ]] && cat "$HARNESS_PORT_FILE" || true; }
harness_current_url()  {
  local p; p="$(harness_current_port)"
  [[ -n "$p" ]] && printf 'http://%s:%s\n' "$HARNESS_HOST" "$p"
}

harness_req() {
  local url="$1"; shift
  local pw; pw="$(cat "$HARNESS_SECRET_FILE")"
  curl -sS --max-time 15 -u "$HARNESS_USERNAME:$pw" "$@" "$url"
}

# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

harness_free_port() {
  python3 - <<'PY'
import socket
s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()
PY
}

harness_gen_password() { python3 -c 'import secrets;print(secrets.token_urlsafe(32))'; }

# Create or adopt an owned root; refuse a pre-existing unowned non-empty tree.
harness_ensure_owned_root() {
  if [[ -e "$HARNESS_ROOT" ]]; then
    [[ -d "$HARNESS_ROOT" && ! -L "$HARNESS_ROOT" ]] || harness_die "HARNESS_ROOT exists and is not a plain directory: $HARNESS_ROOT"
    if [[ -n "$(ls -A "$HARNESS_ROOT" 2>/dev/null)" ]]; then
      harness_require_marker || harness_die "refusing to use pre-existing unowned root $HARNESS_ROOT (choose another HARNESS_ROOT or remove it yourself)"
    else
      harness_write_marker
    fi
  else
    mkdir -p "$HARNESS_ROOT"
    chmod 700 "$HARNESS_ROOT"
    harness_write_marker
  fi
}

harness_start() {
  harness_assert_root_safe
  harness_lock
  local state; state="$(harness_process_state)"
  case "$state" in
    ours)    harness_log "already running (pid $(harness_current_pid))"; return 0 ;;
    foreign) harness_die "refusing to start: a foreign process is recorded in $HARNESS_PIDREC" ;;
  esac

  harness_ensure_owned_root
  mkdir -p "$HARNESS_HOME" "$HARNESS_XDG_CONFIG" "$HARNESS_XDG_DATA" \
           "$HARNESS_XDG_STATE" "$HARNESS_XDG_CACHE" "$HARNESS_CWD" "$HARNESS_RUN/tmp"
  chmod 700 "$HARNESS_HOME" "$HARNESS_XDG_CONFIG" "$HARNESS_XDG_DATA" \
           "$HARNESS_XDG_STATE" "$HARNESS_XDG_CACHE"
  umask 077
  [[ -f "$HARNESS_RUN/.instance-id" ]] || python3 -c 'import secrets;print(secrets.token_hex(16))' > "$HARNESS_RUN/.instance-id"
  umask 022

  local port pw
  port="$(harness_free_port)"
  pw="$(harness_gen_password)"
  umask 077
  printf '%s' "$pw" > "$HARNESS_SECRET_FILE"
  umask 022
  printf '%s' "$port" > "$HARNESS_PORT_FILE"

  # Rebuild the environment from an allowlist. `env -i` discards the caller's
  # environment entirely, including OPENCODE_CONFIG / OPENCODE_PASSWORD /
  # OPENCODE_SERVER_PASSWORD / OPENCHAMBER_* / AI_AGENT.
  # `9>&-` closes the concurrency-lock fd so the long-lived server does not
  # inherit it and hold the lock after this process exits.
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
    > "$HARNESS_STDOUT" 2> "$HARNESS_STDERR" 9>&- &
  local pid=$!
  harness_write_pidrec "$pid" "$port"
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
  # This pid is definitely our own direct child; safe to reap.
  kill -TERM "$pid" 2>/dev/null || true; wait "$pid" 2>/dev/null || true
  harness_die "timed out waiting for /api/info"
}

harness_write_pidrec() {
  local pid="$1" port="$2" starttime exe cwd home pwsha iid
  starttime="$(harness_proc_starttime "$pid")"
  exe="$(harness_proc_exe "$pid")"
  cwd="$(harness_proc_cwd "$pid")"
  home="$(realpath -m -- "$HARNESS_HOME")"
  pwsha="$(cat "$HARNESS_SECRET_FILE" | sha256sum | awk '{print $1}')"
  iid="$(cat "$HARNESS_RUN/.instance-id" 2>/dev/null || true)"
  python3 - "$HARNESS_PIDREC" "$pid" "$starttime" "$exe" "$cwd" "$home" "$port" "$pwsha" "$iid" <<'PY'
import json, os, sys
out, pid, st, exe, cwd, home, port, pwsha, iid = sys.argv[1:10]
doc = {"pid": int(pid), "starttime": st, "exe": exe, "cwd": cwd, "home": home,
       "port": int(port), "passwordSha256": pwsha, "instanceId": iid}
os.umask(0o077)
with open(out, "w") as fh:
    json.dump(doc, fh, indent=2); fh.write("\n")
PY
  chmod 600 "$HARNESS_PIDREC"
}

harness_write_descriptor() {
  local pid="$1" url="$2"
  python3 - "$HARNESS_DESCRIPTOR" "$url" "$pid" "$HARNESS_SECRET_FILE" \
           "$HARNESS_HOME" "$HARNESS_XDG_CONFIG" "$HARNESS_XDG_DATA" \
           "$HARNESS_XDG_STATE" "$HARNESS_XDG_CACHE" "$HARNESS_CWD" "$HARNESS_BIN" \
           "$HARNESS_MARKER" <<'PY'
import json, os, sys
out, url, pid, secret, home, cfg, data, state, cache, cwd, binn, marker = sys.argv[1:13]
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
    "descriptorVersion": 2,
    "kind": "app-owned-disposable",
    "binary": binn,
    "opencodeVersion": version,
    "url": url,
    "pid": int(pid),
    "ownership": {"markerFile": marker, "markerVersion": 1, "root": os.path.dirname(marker)},
    "auth": {"scheme": "http-basic", "username": "opencode", "passwordFile": os.path.abspath(secret)},
    "dirs": {"home": home, "xdgConfigHome": cfg, "xdgDataHome": data,
             "xdgStateHome": state, "xdgCacheHome": cache, "cwd": cwd},
    "documentedRegistration": os.path.expanduser("~/.local/state/opencode/service.json"),
    "note": "Read auth from passwordFile; do not print it. This instance writes no standard service registration.",
}
os.umask(0o077)
with open(out, "w") as fh:
    json.dump(doc, fh, indent=2); fh.write("\n")
PY
  chmod 600 "$HARNESS_DESCRIPTOR"
}

# Terminate the recorded instance, but ONLY after proving process identity.
harness_stop() {
  harness_assert_root_safe
  harness_lock
  if [[ ! -e "$HARNESS_ROOT" ]]; then harness_log "not running (no root)"; return 0; fi
  if ! harness_require_marker; then
    harness_log "refusing to stop: $HARNESS_ROOT is not harness-owned; leaving it untouched"
    return 0
  fi
  local state; state="$(harness_process_state)"
  case "$state" in
    none|stopped)
      harness_clear_run_records
      harness_log "not running" ;;
    foreign)
      harness_die "refusing to signal: process identity does not match this instance (stale or tampered record in $HARNESS_PIDREC)" ;;
    ours)
      local pid; pid="$(harness_current_pid)"
      harness_log "stopping pid=$pid (identity verified)"
      kill -TERM "$pid" 2>/dev/null || true
      local deadline=$((SECONDS + 15))
      while kill -0 "$pid" 2>/dev/null && (( SECONDS < deadline )); do sleep 0.2; done
      kill -0 "$pid" 2>/dev/null && kill -KILL "$pid" 2>/dev/null || true
      wait "$pid" 2>/dev/null || true
      harness_clear_run_records
      harness_log "stopped" ;;
  esac
}

harness_clear_run_records() {
  rm -f "$HARNESS_PIDREC" "$HARNESS_PORT_FILE" "$HARNESS_DESCRIPTOR" "$HARNESS_SECRET_FILE" "$HARNESS_RUN/.instance-id"
}

harness_restart() {
  harness_log "restart (same HOME/XDG/data; only the process is replaced)"
  harness_stop
  harness_start
}

harness_status() {
  local state; state="$(harness_process_state)"
  case "$state" in
    ours)
      local url code; url="$(harness_current_url)"
      code="$(curl -sS --max-time 5 -o /dev/null -w '%{http_code}' \
        -u "$HARNESS_USERNAME:$(cat "$HARNESS_SECRET_FILE")" "$url/api/info" 2>/dev/null || echo 000)"
      printf 'running pid=%s url=%s /api/info=%s owned=yes\n' "$(harness_current_pid)" "$url" "$code"
      return 0 ;;
    foreign) printf 'stopped (foreign process recorded; refused)\n'; return 1 ;;
    stopped) printf 'stopped\n'; return 1 ;;
    *) printf 'stopped\n'; return 1 ;;
  esac
}

harness_discover() {
  [[ -f "$HARNESS_DESCRIPTOR" ]] || harness_die "no instance descriptor (start it first)"
  cat "$HARNESS_DESCRIPTOR"
}

# Remove the scratch tree, but ONLY when it is harness-owned and no foreign
# process is recorded. Never follows a symlink and never deletes an unowned tree.
harness_cleanup() {
  harness_assert_root_safe
  harness_lock
  if [[ ! -e "$HARNESS_ROOT" && ! -L "$HARNESS_ROOT" ]]; then
    harness_log "nothing to clean ($HARNESS_ROOT absent)"; return 0
  fi
  [[ -L "$HARNESS_ROOT" ]] && harness_die "refusing cleanup: $HARNESS_ROOT is a symlink"
  [[ -d "$HARNESS_ROOT" ]] || harness_die "refusing cleanup: $HARNESS_ROOT is not a directory"
  harness_require_marker || harness_die "refusing cleanup: $HARNESS_ROOT is not harness-owned (not removing)"

  local state; state="$(harness_process_state)"
  [[ "$state" == "foreign" ]] && harness_die "refusing cleanup: a foreign process is recorded; resolve it first"
  [[ "$state" == "ours" ]] && harness_stop

  local root_real marker_root
  root_real="$(realpath -m -- "$HARNESS_ROOT")"
  marker_root="$(harness_marker_field root)"
  [[ "$root_real" == "$marker_root" ]] || harness_die "refusing cleanup: root path $root_real != marker root $marker_root"
  rm -rf -- "$root_real"
  harness_log "removed harness-owned root $root_real"
}
