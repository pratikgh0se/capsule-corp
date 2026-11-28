#!/usr/bin/env bash
# tests/test_cleanup_flags.sh - cleanup.sh argument handling (recurrence test for inc_df92b446):
# a typo such as --dryrun once ran the script live. Every scratch path is a temp dir.
set -euo pipefail
unset CAPSULE_JOBS_DIR CAPSULE_HOME CLEANUP_RETENTION_APPLY CLEANUP_STATUTORY_DIRS
unset CAPSULE_CLAUDE_PROJECTS_DIR
R="$(cd "$(dirname "$0")/.." && pwd)"; T="$(mktemp -d)"
trap 'find "$T" -delete' EXIT; pass=0; fail=0
check(){ echo "PASS $1"; pass=$((pass+1)); }; bad(){ echo "FAIL $1"; fail=$((fail+1)); }
mkdir -p "$T/maint/logs"
SENTINEL="$T/maint/logs/nightly-2026-01-01.log"
echo old > "$SENTINEL"; touch -t 202601010000 "$SENTINEL"
cleanup() {  # cleanup <arg>...: stdout to $T/out, stderr to $T/err, prints the exit code
  local rc=0
  CLEANUP_HOME="$T" CLEANUP_MAINT_LOGS="$T/maint/logs" CLEANUP_NO_DOCKER=1 CLEANUP_NO_PG=1 \
    CLEANUP_NO_EVENT=1 AGENT_OPS_NO_SEND=1 CAPSULE_HOME="$T/capsule" CLEANUP_TMPDIR="$T/no-tmp" \
    CLEANUP_EVENTS_SPOOL="$T/no-such/events.jsonl" CLEANUP_STATE_DIR="$T/state" \
    bash "$R/maintenance/cleanup.sh" "$@" >"$T/out" 2>"$T/err" || rc=$?
  echo "$rc"
}
untouched() { [ -f "$SENTINEL" ] && [ ! -e "$T/state/retention.json" ]; }
rc="$(cleanup --dryrun)"
[ "$rc" = 2 ] && [ ! -s "$T/out" ] && grep -q "^usage:" "$T/err" && untouched \
  && check "an unknown flag exits 2 and deletes nothing" \
  || bad "an unknown flag exits 2 and deletes nothing"
rc="$(cleanup -n)"
[ "$rc" = 2 ] && untouched && check "-n exits 2 and deletes nothing" || bad "-n ran"
rc="$(cleanup --dry-run extra)"
[ "$rc" = 2 ] && untouched && check "two arguments exit 2" || bad "two arguments ran"
for h in -h --help; do
  rc="$(cleanup "$h")"
  [ "$rc" = 0 ] && grep -q "usage:" "$T/out" && untouched \
    && check "$h prints usage, exits 0, does nothing" || bad "$h"
done
rc="$(cleanup)"
[ "$rc" = 0 ] && [ ! -e "$SENTINEL" ] && check "no argument stays live" || bad "not live"

echo "test_cleanup_flags: $pass passed, $fail failed"; [ "$fail" = 0 ]
