#!/usr/bin/env bash
# tests/test_status.sh - exercises maintenance/status.sh's vault_index_block()
# in isolation (row 125 / C4): extracts the function (the sed pattern
# tests/test_report_power.sh uses for report.sh's power_lines()) and drives it
# with a fake `uv` on PATH, so this never shells out to the real
# notes-vault skill checkout, the real notes vault, or the network. status.sh
# itself hard-codes /opt/homebrew/bin ahead of $PATH, which would otherwise
# shadow a fake `uv` with the real one - extracting the function sidesteps
# that override entirely.
set -u
unset CAPSULE_JOBS_DIR   # the jobs config dir comes from each case's fake home
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT

fails=0
check() { if eval "$2"; then echo "PASS $1"; else echo "FAIL $1"; fails=$((fails+1)); fi; }

SRC="$(sed -n '/^vault_index_block()/,/^}/p' "$REPO/maintenance/status.sh")"
check "vault_index_block() found in status.sh" '[ -n "$SRC" ]'

FAKEBIN="$T/fakebin"; mkdir -p "$FAKEBIN"
write_fake_uv() {  # write_fake_uv <script-body>
  cat >"$FAKEBIN/uv" <<SH
#!/usr/bin/env bash
$1
SH
  chmod +x "$FAKEBIN/uv"
}

run_block() {  # run_block -> stdout+stderr of vault_index_block(), no color codes
  PATH="$FAKEBIN:/usr/bin:/bin" YEL="" RST="" \
    OBSIDIAN_SECOND_BRAIN_REPO="$T/sb-repo" OBSIDIAN_VAULT_PATH="$T/vault" \
    bash -c "$SRC
vault_index_block" 2>&1
}

# --- populated: the three lines, exact format -------------------------------
NEWEST_TS="$(python3 -c 'import time; print(time.time() - 3600*30)')"  # ~1.25 days ago
write_fake_uv "cat <<JSON
{
  \"exists\": true,
  \"notes_indexed\": 88,
  \"notes_eligible\": 96,
  \"excluded\": {\"denied dir: copilot/\": 17, \"skill mirror (**/skills/<tool>/<role>/<doc>.md)\": 719, \"denied prefix: ledger/\": 3},
  \"excluded_total\": 739,
  \"newest_write\": $NEWEST_TS
}
JSON"
OUT="$(run_block)"
check "indexed line"  'printf %s "$OUT" | grep -qx "  indexed 88/96 eligible"'
check "newest write line" 'printf %s "$OUT" | grep -qE "^  newest write [0-9.]+ days ago$"'
check "excluded line sums mirror and copilot reasons" \
  'printf %s "$OUT" | grep -qx "  excluded 739 (mirror 719, copilot 17)"'

# --- no index built -----------------------------------------------------
write_fake_uv 'echo "{\"exists\": false}"'
OUT="$(run_block)"
check "no index built degrades to one line" 'printf %s "$OUT" | grep -qx "  index: unavailable (no index built)"'

# --- uv exits non-zero with nothing on stdout -------------------------------
write_fake_uv 'echo "boom" >&2; exit 1'
OUT="$(run_block)"
check "uv failure degrades to one line" 'printf %s "$OUT" | grep -qx "  index: unavailable (boom)"'

# --- uv prints garbage -------------------------------------------------------
write_fake_uv 'echo "not json"'
OUT="$(run_block)"
check "bad json degrades to one line" 'printf %s "$OUT" | grep -qx "  index: unavailable (bad json)"'

# --- uv not on PATH at all ---------------------------------------------------
rm -f "$FAKEBIN/uv"
OUT="$(run_block)"
check "missing uv degrades to one line" 'printf %s "$OUT" | grep -qx "  index: unavailable (uv not found)"'

# --- uv hangs past the 20s cap -----------------------------------------------
write_fake_uv 'sleep 30'
START="$(date +%s)"
OUT="$(run_block)"
ELAPSED=$(( $(date +%s) - START ))
check "hung uv is capped at 20s, not left to run" '[ "$ELAPSED" -lt 25 ]'
check "timeout degrades to one line" 'printf %s "$OUT" | grep -qx "  index: unavailable (timeout)"'

# --- never touches a real vault path or job state ---------------------------
check "block never reads OBSIDIAN_VAULT_PATH other than what it was given" \
  '[ ! -e "$T/vault/.obsidian-semantic-index.json" ]'

# --- absence_block() (C9 / M215, row 137) ------------------------------------
# Same extraction trick, driven against temp merge-gate/absence-state dirs so
# this never reads the real ~/.local/state/capsule-corp/merge-gate.
ASRC="$(sed -n '/^absence_block()/,/^}/p' "$REPO/maintenance/status.sh")"
check "absence_block() found in status.sh" '[ -n "$ASRC" ]'

ABS_GATE="$T/abs-gate"; ABS_STATE="$T/abs-state"
mkdir -p "$ABS_GATE/approvals/spec" "$ABS_STATE"
touch "$ABS_GATE/approvals/spec/some-slug.json"

age_abs_approval() {  # age_abs_approval <hours>
  python3 -c '
import os, sys, time
t = time.time() - float(sys.argv[1]) * 3600
os.utime(sys.argv[2], (t, t))
' "$1" "$ABS_GATE/approvals/spec/some-slug.json"
}

run_absence() {  # run_absence [OPS_ABSENCE_PROTOCOL value] -> stdout+stderr of absence_block()
  MAINT_DIR="$REPO/maintenance" MERGE_GATE_STATE="$ABS_GATE" AGENT_OPS_STATE_DIR="$ABS_STATE" \
    OPS_ABSENCE_PROTOCOL="${1-}" \
    bash -c "$ASRC
absence_block" 2>&1
}

age_abs_approval 2   # well under the 48h DRAIN floor: tier 1
AOUT="$(run_absence 1)"
check "tier 1 with the flag on prints nothing at all - heading included" '[ -z "$AOUT" ]'

AOUT="$(run_absence "")"
check "flag off prints nothing at all - heading included, even tier-3-aged" '[ -z "$AOUT" ]'

age_abs_approval 72  # tier 2 (DRAIN)
AOUT="$(run_absence 1)"
check "tier 2 with the flag on prints the heading" \
  'printf %s "$AOUT" | grep -qx "== absence"'
check "tier 2 line carries the day count" \
  'printf %s "$AOUT" | tail -1 | grep -qE "^  tier 2 \(drain\), day [0-9]+"'

AOUT="$(run_absence "")"
check "tier 2 with the flag off still prints nothing at all" '[ -z "$AOUT" ]'

# C9 fix wave 1 item 5: flag off must mean ZERO approvals-tree IO, not just
# an absent line - absence_block() must check absence.enabled() before ever
# calling into absence.state() (the same order board_bridge.py:1257 uses),
# so a directory the flag-off path is not even allowed to look at cannot
# make the block behave any differently.
check "absence_block() checks absence.enabled() before calling absence.state()" \
  '[ "$(printf "%s" "$ASRC" | grep -n "enabled()" | head -1 | cut -d: -f1)" -lt "$(printf "%s" "$ASRC" | grep -n "absence\.state()" | head -1 | cut -d: -f1)" ]'

age_abs_approval 72  # tier 2 (DRAIN) again
chmod 000 "$ABS_GATE/approvals"
AOUT="$(run_absence "")"
RC_UNREADABLE=$?
check "flag off with an unreadable approvals dir: still nothing at all, never a crash" \
  '[ $RC_UNREADABLE -eq 0 ] && [ -z "$AOUT" ]'
chmod 700 "$ABS_GATE/approvals"

# --- report.sh's absence_line() (C9 / M215, row 137) -------------------------
# Same guard-order rule, same reason: flag off must never touch the
# approvals tree, not just print nothing.
RSRC="$(sed -n '/^absence_line()/,/^}/p' "$REPO/maintenance/report.sh")"
check "absence_line() found in report.sh" '[ -n "$RSRC" ]'
check "absence_line() checks absence.enabled() before calling absence.brief_line()" \
  '[ "$(printf "%s" "$RSRC" | grep -n "enabled()" | head -1 | cut -d: -f1)" -lt "$(printf "%s" "$RSRC" | grep -n "brief_line()" | head -1 | cut -d: -f1)" ]'

run_report_absence() {  # run_report_absence [OPS_ABSENCE_PROTOCOL value] -> stdout+stderr of absence_line()
  MAINT_DIR="$REPO/maintenance" MERGE_GATE_STATE="$ABS_GATE" AGENT_OPS_STATE_DIR="$ABS_STATE" \
    OPS_ABSENCE_PROTOCOL="${1-}" \
    bash -c "$RSRC
absence_line" 2>&1
}

age_abs_approval 72  # tier 2 (DRAIN)
ROUT="$(run_report_absence 1)"
check "report.sh: tier 2 with the flag on prints the tier line" 'printf %s "$ROUT" | grep -qi "tier 2"'
chmod 000 "$ABS_GATE/approvals"
ROUT="$(run_report_absence "")"
RC_REPORT_UNREADABLE=$?
check "report.sh: flag off with an unreadable approvals dir: nothing at all, never a crash" \
  '[ $RC_REPORT_UNREADABLE -eq 0 ] && [ -z "$ROUT" ]'
chmod 700 "$ABS_GATE/approvals"

rm -rf "$ABS_GATE" "$ABS_STATE"

# --- decision budget block (C2 / M026) --------------------------------------
DSRC="$(sed -n '/^decision_budget_block()/,/^}/p' "$REPO/maintenance/status.sh")"
check "decision_budget_block() found in status.sh" '[ -n "$DSRC" ]'

mkdir -p "$T/maint/state" "$T/vault/Decisions/sample-app"
cp "$REPO/maintenance/budgets.py" "$T/maint/budgets.py"
cp "$REPO/maintenance/legacy_gate.py" "$T/maint/legacy_gate.py"
cp "$REPO/maintenance/obs_probe.py" "$T/maint/obs_probe.py"
cp "$REPO/maintenance/obs_probe_ship.py" "$T/maint/obs_probe_ship.py"
cp "$REPO/maintenance/obs_probe_util.py" "$T/maint/obs_probe_util.py"
cp "$REPO/maintenance/ai_usage.py" "$T/maint/ai_usage.py"
cp "$REPO/maintenance/events.py" "$T/maint/events.py"
cp "$REPO/maintenance/docker_engine.py" "$T/maint/docker_engine.py"
cp "$REPO/maintenance/scrub.py" "$T/maint/scrub.py"
cp "$REPO/maintenance/vault_conf.py" "$T/maint/vault_conf.py"
ln -s "$REPO/capsule" "$T/capsule"
TODAY="$(date +%F)"
printf -- "---\nclass: R3\n---\n" >"$T/vault/Decisions/sample-app/$TODAY-a-question.md"
printf -- "| 1 | Something (owner, %s 07:50) | yes |\n" "$TODAY" >"$T/vault/Decisions/log.md"

run_decisions() {  # run_decisions [OPS_DECISION_BUDGET value] -> stdout+stderr of decision_budget_block()
  MAINT_DIR="$T/maint" OBSIDIAN_VAULT_PATH="$T/vault" \
    OPS_DECISION_LOG="$T/vault/Decisions/log.md" OPS_DECISION_BUDGET="${1-1}" YEL="" RST="" \
    bash -c "$DSRC
decision_budget_block" 2>&1
}
DOUT="$(run_decisions)"
check "counts today's card and log row" 'printf %s "$DOUT" | grep -q "decisions: 2/10 today (1 cards, 1 log rows; 1 R3)"'

rm -f "$T/vault/Decisions/sample-app/$TODAY-a-question.md" "$T/vault/Decisions/log.md"
DOUT="$(run_decisions)"
check "an empty day still prints a line" 'printf %s "$DOUT" | grep -q "decisions: 0/10 today"'

# Fix round 2: OPS_DECISION_BUDGET off (unset, the default) means the block is
# absent entirely - heading included - the same rule vault_index_block()
# follows; on means the decisions: line prints as before.
DOUT="$(run_decisions "")"
check "flag unset prints nothing at all" '[ -z "$DOUT" ]'
DOUT="$(run_decisions "1")"
check "flag on prints the decisions: line" 'printf %s "$DOUT" | grep -q "decisions: 0/10 today"'

# --- the CADENCE registry (I7) ----------------------------------------------
# Every launchd script job has to be in status.sh's CADENCE dict or the
# one-screen view never shows it, and a job that stopped running (bad plist,
# launchctl unloaded, a crash on the first line) is invisible. Same pin as
# tests/test_run_sh.sh's fork-sync lines.
check "status.sh CADENCE lists quality-nightly" \
  'grep -qF "\"quality-nightly\": 86400" "$REPO/maintenance/status.sh"'
# row 136 / Task 6 review: the 18:00 close job had no
# CADENCE entry, so `ops` never showed it or flagged it overdue - same bug
# quality-nightly's pin above guards against.
check "status.sh CADENCE lists close" \
  'grep -qF "\"close\": 86400" "$REPO/maintenance/status.sh"'

echo "---"; [ "$fails" -eq 0 ] && echo "test_status: all passed" || { echo "test_status: $fails failed"; exit 1; }
