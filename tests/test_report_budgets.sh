#!/usr/bin/env bash
# tests/test_report_budgets.sh - exercises maintenance/report.sh's
# budget_lines() in isolation (the sed pattern tests/test_report_power.sh
# uses), against a scratch MAINT_DIR holding a fixture brief-batch.json. No
# real HOME, vault, board, docker or Postgres is touched.
set -u
unset CAPSULE_JOBS_DIR   # the jobs config dir comes from each case's fake home
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT

fails=0
check() { if eval "$2"; then echo "PASS $1"; else echo "FAIL $1"; fails=$((fails+1)); fi; }

SRC="$(sed -n '/^budget_lines()/,/^}/p' "$REPO/maintenance/report.sh")"
check "budget_lines() found in report.sh" '[ -n "$SRC" ]'

# budgets.py is read from the repo; only its state dir is redirected.
mkdir -p "$T/state"
# budgets.py refuses its default state dir under AGENT_OPS_NO_SEND (hotfix 2e6ba94): point it at the scratch dir explicitly.
export OPS_BUDGET_STATE="$T/state"
cp "$REPO/maintenance/budgets.py" "$T/budgets.py"
cp "$REPO/maintenance/obs_probe.py" "$T/obs_probe.py"
cp "$REPO/maintenance/obs_probe_ship.py" "$T/obs_probe_ship.py"
cp "$REPO/maintenance/obs_probe_util.py" "$T/obs_probe_util.py"
cp "$REPO/maintenance/ai_usage.py" "$T/ai_usage.py"
cp "$REPO/maintenance/events.py" "$T/events.py"
cp "$REPO/maintenance/docker_engine.py" "$T/docker_engine.py"
cp "$REPO/maintenance/legacy_gate.py" "$T/legacy_gate.py"
cp "$REPO/maintenance/scrub.py" "$T/scrub.py"

run_lines() { MAINT_DIR="$T" bash -c "$SRC
budget_lines" 2>/dev/null; }

# --- empty batch: no output ------------------------------------------------
OUT="$(run_lines)"
check "empty batch prints nothing" '[ -z "$OUT" ]'

# --- populated batch: one line per entry, newest last ------------------------
NOW="$(date +%s)"
cat >"$T/state/brief-batch.json" <<JSON
[{"ts": $NOW, "kind": "budget", "board": "sample-app", "task_id": "t_1",
  "text": "parked - cost \$2.31 over \$2.20 (7 runs)"},
 {"ts": $NOW, "kind": "route", "board": "sample-app", "task_id": "t_2",
  "text": "7 files - split"}]
JSON
OUT="$(run_lines)"
check "budget line rendered" 'printf %s "$OUT" | grep -q "^budget: sample-app/t_1 - parked - cost"'
check "route line rendered"  'printf %s "$OUT" | grep -q "^route: sample-app/t_2 - 7 files - split"'
check "exactly two lines"    '[ "$(printf %s\\n "$OUT" | wc -l | tr -d " ")" = 2 ]'

# --- a stale entry is not printed -------------------------------------------
OLD="$((NOW - 26 * 3600))"
cat >"$T/state/brief-batch.json" <<JSON
[{"ts": $OLD, "kind": "budget", "board": "b", "task_id": "t", "text": "old"}]
JSON
OUT="$(run_lines)"
check "an entry older than 24h is not printed" '[ -z "$OUT" ]'

# --- a corrupt batch file degrades to silence, never to an error -------------
echo "{ not json" >"$T/state/brief-batch.json"
OUT="$(run_lines)"
check "a corrupt batch prints nothing" '[ -z "$OUT" ]'

[ "$fails" -eq 0 ] && echo "test_report_budgets.sh: all passed" || echo "test_report_budgets.sh: $fails failed"
exit "$fails"
