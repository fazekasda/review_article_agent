#!/usr/bin/env bash
# shellcheck shell=bash
#
# safety.sh — non-destructive tests for the ownership / process-identity guards
# (repair 1). Every case uses a private scratch base under /tmp/opencode and
# never signals, deletes, or mutates anything it does not own.
#
# Run:  bash spikes/runtime/harness/tests/safety.sh
# Exits 0 when every check passes.

set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HARNESS_SH="$HERE/../harness.sh"

BASE="/tmp/opencode/safety-tests.$$"
# shellcheck disable=SC2317
cleanup_base() {
  { [[ -n "${REAL_PID:-}" ]] && { kill -KILL "$REAL_PID" 2>/dev/null; wait "$REAL_PID" 2>/dev/null; }; } || true
  { [[ -n "${UNREL_PID:-}" ]] && { kill -KILL "$UNREL_PID" 2>/dev/null; wait "$UNREL_PID" 2>/dev/null; }; } || true
  { [[ -n "${STALE_UNREL:-}" ]] && { kill -KILL "$STALE_UNREL" 2>/dev/null; wait "$STALE_UNREL" 2>/dev/null; }; } || true
  rm -rf "$BASE"
}
trap cleanup_base EXIT
mkdir -p "$BASE"

PASS=0; FAIL=0
ok()  { PASS=$((PASS+1)); printf '  ok   %s\n' "$*"; }
bad() { FAIL=$((FAIL+1)); printf '  FAIL %s\n' "$*"; }
check(){ if [[ "$2" == "$3" ]]; then ok "$1 ($2)"; else bad "$1 (got '$2', want '$3')"; fi; }
nonzero(){ if [[ "$2" -ne 0 ]]; then ok "$1 (exit $2)"; else bad "$1 (expected non-zero exit)"; fi; }
zero(){ if [[ "$2" -eq 0 ]]; then ok "$1"; else bad "$1 (expected zero exit, got $2)"; fi; }
exists(){ if [[ -e "$2" ]]; then ok "$1"; else bad "$1 (missing $2)"; fi; }
absent(){ if [[ ! -e "$2" ]]; then ok "$1"; else bad "$1 (unexpected $2)"; fi; }

echo "== A. pre-existing UNOWNED non-empty root is refused (start + cleanup) =="
A="$BASE/unowned"; mkdir -p "$A"; printf 'keep\n' > "$A/sentinel.txt"
HARNESS_ROOT="$A" bash "$HARNESS_SH" start >/dev/null 2>&1; nonzero "start refuses unowned root" "$?"
exists "start left the sentinel untouched" "$A/sentinel.txt"
HARNESS_ROOT="$A" bash "$HARNESS_SH" cleanup >/dev/null 2>&1; nonzero "cleanup refuses unowned root" "$?"
exists "cleanup left the sentinel untouched" "$A/sentinel.txt"
check "sentinel content intact" "$(cat "$A/sentinel.txt")" "keep"

echo "== B. symlinked root is refused; target untouched =="
mkdir -p "$BASE/real"; printf 'keep\n' > "$BASE/real/sentinel.txt"
ln -s "$BASE/real" "$BASE/link"
HARNESS_ROOT="$BASE/link" bash "$HARNESS_SH" start >/dev/null 2>&1; nonzero "start refuses symlinked root" "$?"
HARNESS_ROOT="$BASE/link" bash "$HARNESS_SH" cleanup >/dev/null 2>&1; nonzero "cleanup refuses symlinked root" "$?"
exists "symlink target sentinel untouched" "$BASE/real/sentinel.txt"

echo "== C. ancestor config/discovery risk is refused =="
mkdir -p "$BASE/anc"; printf '{}\n' > "$BASE/anc/opencode.json"
HARNESS_ROOT="$BASE/anc/root" bash "$HARNESS_SH" start >/dev/null 2>&1; nonzero "start refuses ancestor opencode.json" "$?"
absent "no root created next to ancestor config" "$BASE/anc/root"

echo "== D. protected HOME/config locations are refused, nothing created =="
GUARD_ROOT="$HOME/.config/opencode/spike-harness-safety-test"
HARNESS_ROOT="$GUARD_ROOT" bash "$HARNESS_SH" start >/dev/null 2>&1; nonzero "start refuses \$HOME/.config root" "$?"
absent "no protected directory created" "$GUARD_ROOT"

echo "== E. tampered/stale PID must NOT signal an unrelated process =="
E="$BASE/own-tamper"
HARNESS_ROOT="$E" bash "$HARNESS_SH" start >/dev/null 2>&1; zero "start owned instance" "$?"
REAL_PID="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["pid"])' "$E/run/instance.pid.json")"
sleep 300 & UNREL_PID=$!
# Tamper: point the record at the unrelated process but keep the old starttime.
python3 - "$E/run/instance.pid.json" "$UNREL_PID" <<'PY'
import json, sys
p = sys.argv[1]; d = json.load(open(p)); d["pid"] = int(sys.argv[2])
json.dump(d, open(p, "w"))
PY
HARNESS_ROOT="$E" bash "$HARNESS_SH" stop >/dev/null 2>&1; nonzero "stop refuses tampered/foreign record" "$?"
if kill -0 "$UNREL_PID" 2>/dev/null; then ok "unrelated process survived stop"; else bad "unrelated process was signalled!"; fi
if kill -0 "$REAL_PID" 2>/dev/null; then ok "real instance not signalled either"; else bad "real instance died unexpectedly"; fi
HARNESS_ROOT="$E" bash "$HARNESS_SH" cleanup >/dev/null 2>&1; nonzero "cleanup refuses while foreign record present" "$?"
exists "owned root preserved by refused cleanup" "$E/.harness-owned.json"
{ kill -TERM "$REAL_PID" 2>/dev/null; wait "$REAL_PID" 2>/dev/null; } || true
{ kill -KILL "$UNREL_PID" 2>/dev/null; wait "$UNREL_PID" 2>/dev/null; } || true
UNREL_PID=""
rm -rf "$E" "$E.lock"; REAL_PID=""

echo "== F. stale (dead) PID is cleaned without signalling anyone =="
F="$BASE/own-stale"
HARNESS_ROOT="$F" bash "$HARNESS_SH" start >/dev/null 2>&1; zero "start owned instance" "$?"
SPID="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["pid"])' "$F/run/instance.pid.json")"
kill -KILL "$SPID" 2>/dev/null; for _ in $(seq 1 50); do kill -0 "$SPID" 2>/dev/null || break; sleep 0.1; done
sleep 300 & STALE_UNREL=$!
HARNESS_ROOT="$F" bash "$HARNESS_SH" stop >/dev/null 2>&1; zero "stop on stale record succeeds" "$?"
if kill -0 "$STALE_UNREL" 2>/dev/null; then ok "unrelated process survived stale-stop"; else bad "unrelated process was signalled!"; fi
{ kill -KILL "$STALE_UNREL" 2>/dev/null; wait "$STALE_UNREL" 2>/dev/null; } || true
STALE_UNREL=""
absent "run records cleared after stale stop" "$F/run/instance.pid.json"
HARNESS_ROOT="$F" bash "$HARNESS_SH" cleanup >/dev/null 2>&1; zero "cleanup owned root after stale stop" "$?"
absent "owned root removed" "$F"

echo "== G. owned cleanup removes only the owned tree =="
G="$BASE/own-clean"; SIB="$BASE/sibling"; mkdir -p "$SIB"; printf 'keep\n' > "$SIB/sentinel.txt"
HARNESS_ROOT="$G" bash "$HARNESS_SH" start >/dev/null 2>&1; zero "start owned instance" "$?"
HARNESS_ROOT="$G" bash "$HARNESS_SH" stop    >/dev/null 2>&1; zero "stop owned instance" "$?"
HARNESS_ROOT="$G" bash "$HARNESS_SH" cleanup >/dev/null 2>&1; zero "cleanup owned root" "$?"
absent "owned root removed" "$G"
exists "sibling sentinel survived" "$SIB/sentinel.txt"

echo "== H. concurrency lock serialises operations =="
H="$BASE/locktest"
mkdir -p "$(dirname "$H")"
( flock -x 8; sleep 3 ) 8>"$H.lock" & HOLDER=$!
sleep 0.3
T0=$SECONDS
HARNESS_ROOT="$H" timeout 60 bash "$HARNESS_SH" start >/dev/null 2>&1; RC=$?
ELAPSED=$((SECONDS - T0))
wait "$HOLDER" 2>/dev/null || true
zero "start eventually succeeds after lock release" "$RC"
if (( ELAPSED >= 2 )); then ok "start waited for the held lock (${ELAPSED}s)"; else bad "start did not wait for the lock (${ELAPSED}s)"; fi
HARNESS_ROOT="$H" bash "$HARNESS_SH" stop    >/dev/null 2>&1
HARNESS_ROOT="$H" bash "$HARNESS_SH" cleanup >/dev/null 2>&1

echo
printf '== summary: %d passed, %d failed ==\n' "$PASS" "$FAIL"
trap - EXIT
cleanup_base
[[ "$FAIL" -eq 0 ]]
