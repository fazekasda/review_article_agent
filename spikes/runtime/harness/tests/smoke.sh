#!/usr/bin/env bash
# shellcheck shell=bash
#
# smoke.sh — success / error / cleanup / isolation tests for the disposable
# app-owned OpenCode V2 service harness.
#
# Safety: uses no provider credentials and triggers no model call. It only
# creates a session, admits prompt envelopes, and reads them back. It never
# subscribes to events and never calls a generate endpoint.
#
# Run:  bash spikes/runtime/harness/tests/smoke.sh
# Exits 0 when every check passes.

set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../lib.sh
source "$HERE/../lib.sh"

PASS=0; FAIL=0
ok()   { PASS=$((PASS+1)); printf '  ok   %s\n' "$*"; }
bad()  { FAIL=$((FAIL+1)); printf '  FAIL %s\n' "$*"; }
check(){ if [[ "$2" == "$3" ]]; then ok "$1 ($2)"; else bad "$1 (got '$2', want '$3')"; fi; }
checkr(){ if [[ "$2" =~ $3 ]]; then ok "$1 (matches $3)"; else bad "$1 (got '$2', want match $3)"; fi; }
checkne(){ if [[ "$2" != "$3" ]]; then ok "$1"; else bad "$1 (unexpectedly equal)"; fi; }

# Always clean up our own scratch tree, even on failure.
trap 'harness_cleanup >/dev/null 2>&1 || true' EXIT

echo "== 0. preconditions =="
harness_log "opencode: $HARNESS_BIN ($("$HARNESS_BIN" --version))"
harness_log "scratch root: $HARNESS_ROOT"

# Record ordinary shared-service state so we can prove non-interference.
ORD_STATUS_BEFORE="$(opencode service status 2>&1 || true)"
ORD_REG="$HOME/.local/state/opencode/service.json"
ORD_REG_BEFORE="$( [[ -f "$ORD_REG" ]] && sha256sum "$ORD_REG" | awk '{print $1}' || echo absent )"

echo "== 1. start / status / auth =="
harness_cleanup >/dev/null 2>&1 || true
harness_start >/dev/null
check "status reports running" "$(harness_status >/dev/null 2>&1 && echo running || echo stopped)" "running"
URL="$(harness_current_url)"
PW="$(cat "$HARNESS_SECRET_FILE")"

code_noauth="$(curl -sS --max-time 5 -o /dev/null -w '%{http_code}' "$URL/api/info")"
check "/api/info without auth is 401" "$code_noauth" "401"
code_badauth="$(curl -sS --max-time 5 -o /dev/null -w '%{http_code}' -u "opencode:wrongpass" "$URL/api/info")"
check "/api/info wrong password is 401" "$code_badauth" "401"
info="$(harness_req "$URL/api/info")"
checkr "/api/info advertises 2.x" "$(printf '%s' "$info" | python3 -c 'import json,sys;print(json.load(sys.stdin)["version"])')" '^2\.'
checkr "/api/info url is loopback" "$(printf '%s' "$info" | python3 -c 'import json,sys;print(json.load(sys.stdin)["urls"][0])')" '^http://127\.0\.0\.1:'

echo "== 2. inherited environment / config isolation =="
PID="$(harness_current_pid)"
PROC_ENV="$(tr '\0' '\n' < "/proc/$PID/environ")"
check "child env has no inherited OPENCODE_CONFIG" \
  "$(printf '%s\n' "$PROC_ENV" | grep -c '^OPENCODE_CONFIG=' || true)" "0"
check "child env has no inherited OPENCHAMBER_* vars" \
  "$(printf '%s\n' "$PROC_ENV" | grep -c '^OPENCHAMBER_' || true)" "0"
check "child env has no AI_AGENT var" \
  "$(printf '%s\n' "$PROC_ENV" | grep -c '^AI_AGENT=' || true)" "0"
check "child HOME is the scratch HOME" \
  "$(printf '%s\n' "$PROC_ENV" | grep '^HOME=' | cut -d= -f2-)" "$HARNESS_HOME"
if [[ -n "${OPENCODE_PASSWORD:-}" ]]; then
  checkne "child password is not the caller's OPENCODE_PASSWORD" \
    "$(printf '%s\n' "$PROC_ENV" | grep '^OPENCODE_PASSWORD=' | cut -d= -f2-)" "$OPENCODE_PASSWORD"
fi
# Only the harness config directory may be a config source.
cfg_sources="$(harness_req "$URL/api/config" | python3 -c '
import json,sys
for e in json.load(sys.stdin):
    print(e.get("path",""))')"
outside="$(printf '%s\n' "$cfg_sources" | grep -v "^$HARNESS_ROOT" | grep -v '^$' || true)"
check "no config source outside the scratch root" "$(printf '%s' "$outside" | wc -l | tr -d ' ')" "0"
check "no standard service registration written in scratch state" \
  "$( [[ -f "$HARNESS_XDG_STATE/opencode/service.json" ]] && echo present || echo absent )" "absent"

echo "== 3. session create + durable admission (no model call) =="
SID="$(harness_req "$URL/api/session" -H 'content-type: application/json' \
  -d "{\"location\":{\"directory\":\"$HARNESS_CWD\"},\"title\":\"smoke\"}" \
  | python3 -c 'import json,sys;print(json.load(sys.stdin)["data"]["id"])')"
checkr "session id pattern" "$SID" '^ses'

rec1="$(harness_req "$URL/api/session/$SID/prompt" -H 'content-type: application/json' \
  -d '{"id":"msg_smoke001","text":"admission probe","delivery":"queue"}')"
admit_id="$(printf '%s' "$rec1" | python3 -c 'import json,sys;print(json.load(sys.stdin)["data"]["id"])')"
check "admission receipt echoes the client id" "$admit_id" "msg_smoke001"
check "admission receipt type is user" \
  "$(printf '%s' "$rec1" | python3 -c 'import json,sys;print(json.load(sys.stdin)["data"]["type"])')" "user"
checkr "admission receipt has a created timestamp" \
  "$(printf '%s' "$rec1" | python3 -c 'import json,sys;print(int(json.load(sys.stdin)["data"]["time"]["created"]))')" '^[0-9]+$'

# Retry the SAME client-supplied id: must not create a second accepted input.
harness_req "$URL/api/session/$SID/prompt" -H 'content-type: application/json' \
  -d '{"id":"msg_smoke001","text":"admission probe","delivery":"queue"}' >/dev/null
msg_count="$(harness_req "$URL/api/session/$SID/message" \
  | python3 -c 'import json,sys;print(sum(1 for m in json.load(sys.stdin)["data"] if m.get("id")=="msg_smoke001"))')"
check "retried admission id yields exactly one stored message" "$msg_count" "1"

# No model activity must have occurred (no credentials, no turn triggered).
model_lines="$(grep -ciE 'generate|chat\.completions|assistant' "$HARNESS_XDG_DATA/opencode/log/opencode.log" 2>/dev/null || true)"
check "no model/generate activity in the isolated log" "$model_lines" "0"

echo "== 4. typed errors =="
code="$(harness_req "$URL/api/session/ses_missing/prompt" -H 'content-type: application/json' \
  -d '{"text":"x"}' -o /dev/null -w '%{http_code}')"
check "unknown session is 404" "$code" "404"
body="$(harness_req "$URL/api/session/$SID/prompt" -H 'content-type: application/json' -d '{"id":"msg_x"}')"
check "missing text is InvalidRequestError" \
  "$(printf '%s' "$body" | python3 -c 'import json,sys;print(json.load(sys.stdin)["_tag"])' 2>/dev/null)" "InvalidRequestError"
body="$(harness_req "$URL/api/session/$SID/prompt" -H 'content-type: application/json' -d '{"id":"bad","text":"x"}')"
check "bad admission id pattern is InvalidRequestError" \
  "$(printf '%s' "$body" | python3 -c 'import json,sys;print(json.load(sys.stdin)["_tag"])' 2>/dev/null)" "InvalidRequestError"

echo "== 5. restart durability =="
harness_restart >/dev/null
URL="$(harness_current_url)"
check "session survives process restart" \
  "$(harness_req "$URL/api/session/$SID" -o /dev/null -w '%{http_code}')" "200"
check "admitted message survives process restart" \
  "$(harness_req "$URL/api/session/$SID/message" \
      | python3 -c 'import json,sys;print(sum(1 for m in json.load(sys.stdin)["data"] if m.get("id")=="msg_smoke001"))')" "1"

echo "== 6. cleanup and non-interference =="
STOP_PORT="$(harness_current_port)"
harness_stop >/dev/null
check "status reports stopped" "$(harness_status >/dev/null 2>&1 && echo running || echo stopped)" "stopped"
closed="$(python3 - "$HARNESS_HOST" "${STOP_PORT:-0}" <<'PY'
import socket, sys
host, port = sys.argv[1], int(sys.argv[2])
s = socket.socket(); s.settimeout(1)
try:
    s.connect((host, port)); print("open")
except Exception:
    print("closed")
finally:
    s.close()
PY
)"
check "port is released after stop" "$closed" "closed"

ORD_STATUS_AFTER="$(opencode service status 2>&1 || true)"
check "ordinary service endpoint unchanged" "$ORD_STATUS_AFTER" "$ORD_STATUS_BEFORE"
ORD_REG_AFTER="$( [[ -f "$ORD_REG" ]] && sha256sum "$ORD_REG" | awk '{print $1}' || echo absent )"
check "ordinary service registration unchanged" "$ORD_REG_AFTER" "$ORD_REG_BEFORE"

harness_cleanup >/dev/null
check "scratch root removed by cleanup" "$( [[ -d "$HARNESS_ROOT" ]] && echo present || echo absent )" "absent"

echo
echo "== summary: $PASS passed, $FAIL failed =="
trap - EXIT
[[ "$FAIL" -eq 0 ]]
