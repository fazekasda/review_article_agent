#!/usr/bin/env bash
# shellcheck shell=bash
#
# harness.sh — lifecycle CLI for the disposable app-owned OpenCode V2 service.
#
#   ./harness.sh start|stop|restart|status|discover|info|contract|cleanup|selftest
#
# It only ever manages its own foreground `opencode serve` process under
# $HARNESS_ROOT. It never calls `opencode service start|restart|stop`, never
# writes the standard service registration, and never touches ordinary sessions.
#
# Scope: step 00 runtime spike. No provider credentials, no model calls.

set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "$HERE/lib.sh"

usage() {
  cat <<'EOF'
Usage: harness.sh <command> [args]

Commands:
  start      Start the isolated app-owned server (idempotent).
  stop       Stop and de-register only this instance.
  restart    Replace the process, keeping HOME/XDG data (durability probe).
  status     Print running/stopped and the /api/info HTTP code.
  discover   Print the app-owned instance descriptor (auth passwordFile path).
  info       Print the live /api/info document.
  contract   Capture the live OpenAPI document + print version summary.
  cleanup    Stop and delete the whole scratch tree (harness-owned only).
  selftest   Run tests/smoke.sh and tests/safety.sh end to end.

Environment overrides:
  HARNESS_ROOT          scratch root (default: ${TMPDIR:-/tmp}/opencode/spike-harness)
  HARNESS_OPENCODE_BIN  explicit opencode binary path
EOF
}

cmd_contract() {
  harness_is_running || harness_die "start the instance first"
  local url; url="$(harness_current_url)"
  harness_log "capturing OpenAPI document -> $HARNESS_OPENAPI"
  harness_req "$url/openapi.json" -o "$HARNESS_OPENAPI"
  local info; info="$(harness_req "$url/api/info")"
  printf '%s\n' "$info" | python3 -c '
import json,sys
d=json.load(sys.stdin)
print("server.version   =", d.get("version"))
print("server.url       =", ", ".join(d.get("urls", [])))
print("server.pid       =", d.get("pid"))
print("server.paths.tmp =", (d.get("paths") or {}).get("tmp"))
print("capabilities     =", json.dumps(d.get("capabilities", {})))
'
  python3 - "$HARNESS_OPENAPI" <<'PY'
import json, sys
with open(sys.argv[1]) as fh:
    doc = json.load(fh)
print("openapi.info.version =", doc.get("info", {}).get("version"))
print("openapi.title        =", doc.get("info", {}).get("title"))
print("openapi.operations   =", sum(len(v) for v in doc.get("paths", {}).values()))
print("openapi.schemas      =", len(doc.get("components", {}).get("schemas", {})))
PY
}

cmd_selftest() {
  local rc=0
  bash "$HERE/tests/smoke.sh" || rc=1
  bash "$HERE/tests/safety.sh" || rc=1
  return "$rc"
}

case "${1:-}" in
  start)    harness_start ;;
  stop)     harness_stop ;;
  restart)  harness_restart ;;
  status)   harness_status ;;
  discover) harness_discover ;;
  info)
    harness_is_running || harness_die "start the instance first"
    harness_req "$(harness_current_url)/api/info" | python3 -m json.tool
    ;;
  contract) cmd_contract ;;
  cleanup)  harness_cleanup ;;
  selftest) cmd_selftest ;;
  ""|-h|--help|help) usage ;;
  *) usage; exit 2 ;;
esac
