#!/usr/bin/env bash
# Every shell suite that needs no docker, host or postgres (factory-autonomy Task 14, card
# t_95d328af): the capsule-corp board's third and fourth contract commands (--shard fast / --shard slow), run inside the network-none worker
# image. A suite whose first 10 lines carry `# needs: docker|host|postgres` is skipped by name (the
# allowed set is pinned in contracts/intent/run_shell_suites.jsonl); every other tests/test_*.sh
# runs with a timeout (SHELL_SUITE_TIMEOUT seconds, default 300:
# the tick's whole contract command gets 600). The first red stops the run, names the suite and
# exits 1. --dir D runs D/test_*.sh instead of tests/ (the runner's own test). `# shard: slow` in a
# suite's first 10 lines puts it in --shard slow; --shard fast runs the rest; no --shard runs all.
set -uo pipefail
cd "$(dirname "$0")/.."
DIR=tests; SHARD=all; AUTO=0; AUTO_BASE=""; ONLY=""
# --docker-auto BASE (row 615): a diff BASE...HEAD touching these paths turns RUN_DOCKER_TESTS=1 on, unless the caller set it.
DOCKER_PATHS='^(docker/|observability/|maintenance/.*\.sql$|.*_pg\.sh$'
DOCKER_PATHS="$DOCKER_PATHS|tests/lib_docker_optin\.sh$|tests/test_(docker_image|fluent_bit_scrub)\.sh$)"
while [ $# -gt 0 ]; do
  case "$1" in --dir) DIR="$2" ;; --shard) SHARD="$2" ;; --docker-auto) AUTO=1; AUTO_BASE="$2" ;;
    --only) ONLY=" $2 " ;;   # space-separated suite paths: the lander's selected tests
    *) echo "usage: $0 [--dir D] [--shard fast|slow] [--docker-auto BASE] [--only 'tests/a.sh ...']"; exit 2 ;; esac
  shift 2
done
# CAPSULE_DOCKER_AUTO_BASE (the lander's pin) acts as --docker-auto when no flag was given.
[ "$AUTO" = 0 ] && [ -n "${CAPSULE_DOCKER_AUTO_BASE:-}" ] && { AUTO=1; AUTO_BASE="$CAPSULE_DOCKER_AUTO_BASE"; }
unset CAPSULE_DOCKER_AUTO_BASE   # consumed: the suites below must not inherit it
if [ "$AUTO" = 1 ]; then
  [ -n "$AUTO_BASE" ] && names="$(git diff --name-only --no-renames "$AUTO_BASE"...HEAD)" \
    || { echo "DOCKER-AUTO: cannot diff $AUTO_BASE...HEAD" >&2; exit 2; }
  hit="$(grep -E "$DOCKER_PATHS" <<<"$names" | head -n 1)"
  [ -z "${RUN_DOCKER_TESTS:-}" ] && [ -n "$hit" ] && {
    export RUN_DOCKER_TESTS=1; echo "DOCKER-AUTO: RUN_DOCKER_TESTS=1 because $hit changed since $AUTO_BASE"; }
fi
case "$SHARD" in all | fast | slow) ;; *) echo "unknown shard: $SHARD"; exit 2 ;; esac
LIMIT="${SHELL_SUITE_TIMEOUT:-300}"
LOG="$(mktemp)"; NS_DIR="$(mktemp -d)"; trap 'rm -f "$LOG"; rm -rf "$NS_DIR"' EXIT
# One no-send events spool for the whole run (maintenance/events.py reads it only under
# AGENT_OPS_NO_SEND), so no suite appends to the shared OS-temp spool.
export CAPSULE_NO_SEND_SPOOL="$NS_DIR/no-send-spool.jsonl"
with_timeout() {
  if command -v timeout >/dev/null 2>&1; then timeout "$LIMIT" "$@"
  else perl -e 'alarm shift; exec @ARGV' "$LIMIT" "$@"; fi
}
n=0; k=0
for f in "$DIR"/test_*.sh; do
  if head -n 10 "$f" | grep -Eq '^# shard: slow$'; then mine=slow; else mine=fast; fi
  [ "$SHARD" != all ] && [ "$SHARD" != "$mine" ] && continue
  [ -n "$ONLY" ] && [[ "$ONLY" != *" $f "* ]] && continue
  need="$(head -n 10 "$f" | grep -Eo '^# needs: *(docker|host|postgres)( |$)' | head -n 1 | awk '{print $3}')"
  if [ -n "$need" ]; then echo "SKIP $f (needs: $need)"; k=$((k + 1)); continue; fi
  with_timeout bash "$f" >"$LOG" 2>&1; rc=$?
  if [ $rc -eq 0 ]; then
    echo "PASS $f"; n=$((n + 1))
    # docker opt-in (t_cf6ef8d1): each `SKIP <case>: ...` line a green suite printed is a skipped case
    c="$(grep -c '^SKIP ' "$LOG")"; k=$((k + c)); [ "$c" -gt 0 ] && grep '^SKIP ' "$LOG"
  elif [ $rc -eq 77 ] && grep -q '^SKIP ' "$LOG"; then
    echo "SKIP $f (whole suite skipped)"; grep '^SKIP ' "$LOG"; k=$((k + 1))
  else
    case $rc in 124 | 142) why="timed out after ${LIMIT}s" ;; 77) why="exit 77, no SKIP line" ;; *) why="exit $rc" ;; esac
    echo "FAIL $f ($why)"; tail -n 10 "$LOG"
    echo "shell suites: $n passed, 1 failed, $k skipped"; exit 1
  fi
done
echo "shell suites: $n passed, 0 failed, $k skipped"
