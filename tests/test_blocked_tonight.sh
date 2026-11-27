#!/usr/bin/env bash
# tests/test_blocked_tonight.sh - maintenance/report.sh --blocked-tonight (the 20:15 job, M190),
# run against a scratch MAINT_DIR with a FAKE sender. Nothing is sent, no vault is written, no
# plist is loaded, no network is touched. Modelled on tests/test_close.sh.
set -u
unset CAPSULE_JOBS_DIR   # the jobs config dir comes from each case's fake home
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT

# These tests check delivery through their own fake sender (CAPSULE_PY); a caller's AGENT_OPS_NO_SEND=1 would make lib_send skip it.
unset AGENT_OPS_NO_SEND
fails=0
check() { if eval "$2"; then echo "PASS $1"; else echo "FAIL $1"; fails=$((fails+1)); fi; }
# The owner job plists (launchd/templates/*.obsidian-*.plist) are private-only: the public copy drops them. The
# private tree is told apart by release/private-denylist.txt, which the export never ships. A check on the plist
# runs in full in the private tree and skips, with a printed reason, in the public copy.
plcheck() {
  if [ -f "$REPO/release/private-denylist.txt" ]; then check "$@"; else echo "SKIP $1: the owner job plist is private-only (not in the public copy)"; fi
}
PLIST="$(ls "$REPO"/launchd/templates/*.obsidian-blocked-tonight.plist 2>/dev/null | head -1)"; LABEL="$(basename "$PLIST" .plist)"   # the overlay template names its own prefix

# --- the wiring is present -------------------------------------------------
check "report.sh has a --blocked-tonight mode" 'grep -q -- "--blocked-tonight) MODE=blocked-tonight" "$REPO/maintenance/report.sh"'
check "run.sh knows the blocked-tonight job" 'grep -q "JOB\" = \"blocked-tonight\"" "$REPO/maintenance/run.sh"'
plcheck "the plist exists" '[ -f "$PLIST" ]'
plcheck "the plist fires at 20:15, before the 20:30 quiet hours" \
  'python3 -c "import plistlib,sys; sys.exit(plistlib.load(open(sys.argv[1],\"rb\"))[\"StartCalendarInterval\"] != {\"Hour\": 20, \"Minute\": 15})" "$PLIST"'
plcheck "the plist runs run.sh blocked-tonight" 'grep -q "run.sh" "$PLIST" && grep -q "<string>blocked-tonight</string>" "$PLIST"'
check "run.sh excludes blocked-tonight from the PROMPT_FILE chain" \
  'grep -qE "\\[ \"\\\$JOB\" != \"close\" \\] && \\[ \"\\\$JOB\" != \"blocked-tonight\" \\]" "$REPO/maintenance/run.sh"'
check "the job prints no model line" \
  'grep -qE "blocked-tonight\|sprint-sync\|retrieval-eval" "$REPO/maintenance/run.sh"'
check "the plist is classified custodial (bin/lib_jobs.sh)" 'grep -q "}.obsidian-blocked-tonight" "$REPO/bin/lib_jobs.sh"'
check "status.sh CADENCE lists blocked-tonight" 'grep -qF "\"blocked-tonight\": 86400" "$REPO/maintenance/status.sh"'
plcheck "the plist is switched on: not listed in launchd/unloaded (D65, 2026-10-28)" '! grep -qx "$LABEL" "$REPO/launchd/unloaded"'

# --- run.sh blocked-tonight (dry), the way the plist invokes it --------------------
mkdir -p "$T/run-sh-vault" "$T/run-sh-home"
RUN_SH_OUT="$(HOME="$T/run-sh-home" OBSIDIAN_VAULT_PATH="$T/run-sh-vault" RUN_SH_DRY_RUN=1 bash "$REPO/maintenance/run.sh" blocked-tonight 2>&1)"; RUN_SH_RC=$?
check "run.sh blocked-tonight (dry) exits 0"            '[ "$RUN_SH_RC" = 0 ]'
check "run.sh blocked-tonight (dry) never says no prompt file" '! grep -q "no prompt file" <<<"$RUN_SH_OUT"'
check "run.sh blocked-tonight (dry) would run report.sh --blocked-tonight" 'grep -q "dry run: would run report.sh --blocked-tonight" <<<"$RUN_SH_OUT"'
check "run.sh blocked-tonight (dry) prints no model line" '! grep -qi "model" <<<"$RUN_SH_OUT"'

# --- run it for real, with a fake Slack sender (same harness as test_close.sh) ----
mkdir -p "$T/home/.local/bin" "$T/maint/state/jobs" "$T/maint/logs" "$T/home/.config/capsule-corp"
REAL_PY="$(command -v python3)"
printf '%s\n' '#!/usr/bin/env bash' \
  '[ "${1:-}" = "-P" ] && shift' \
  'if [ "$1 $2 $3" = "-m capsule outbox-send" ]; then' \
  '  printf "%s\n" "$5" >>"$FAKE_SENT"; echo "$@" >>"$FAKE_ARGS"; exit "${FAKE_SEND_RC:-0}"; fi' \
  "exec \"$REAL_PY\" \"\$@\"" >"$T/home/.local/bin/fakepy"
OLD="$(python3 "$REPO/tests/denylist_term.py" retired)" || { echo "FAIL: denylist_term: release/private-denylist.txt is unusable"; exit 1; }
[ -n "$OLD" ] && { printf '#!/usr/bin/env bash\ntouch "%s/old-sender-ran"\n' "$T" >"$T/home/.local/bin/$OLD"; chmod +x "$T/home/.local/bin/$OLD"; }
chmod +x "$T/home/.local/bin/fakepy"
for f in brief.py messages.py budgets.py events.py docker_engine.py heal_table.py scrub.py vault_git.py vault_conf.py; do
  cp "$REPO/maintenance/$f" "$T/maint/$f"
done
# brief.py imports `capsule` (host_registry.boards_served) from the dir above MAINT_DIR
ln -s "$REPO/capsule" "$T/capsule"
cp "$REPO/maintenance/report.sh" "$REPO/maintenance/lib_state.sh" "$REPO/maintenance/lib_config.sh" "$REPO/maintenance/lib_send.sh" "$REPO/maintenance/lib_vault.sh" "$T/maint/"
cp "$T/maint/brief.py" "$T/maint/brief.py.real"
: >"$T/maint/state/events.jsonl"
printf 'OPS_CHAT_PROFILE=telegram\nOPS_ALERT_TARGET=telegram:-100:1\n' >"$T/home/.config/capsule-corp/ops.env"
mkdir -p "$T/home/my-vault"

run_bt() {
  FAKE_SENT="$T/sent.txt" FAKE_ARGS="$T/args.txt" FAKE_SEND_RC="${FAKE_SEND_RC:-0}" \
  HOME="$T/home" MAINT_DIR="$T/maint" CAPSULE_PY="$T/home/.local/bin/fakepy" \
  OBSIDIAN_VAULT_PATH="$T/home/my-vault" \
  OPS_BRIEF_V2="${1:-0}" REPORT_NO_STAMP=1 TG_SEND_NO_SPOOL=1 \
  OPS_MESSAGE_STATE="$T/message-budget.json" \
    bash "$T/maint/report.sh" --blocked-tonight >/dev/null 2>&1
  echo $?
}
# A composer double: prints $BT_LINE (or nothing) so the job is tested apart from the board.
set_composer() {
  printf '%s\n' '#!/usr/bin/env python3' 'import os, sys' \
    'if "--blocked-tonight" in sys.argv:' \
    '    sys.stdout.write(os.environ.get("BT_LINE", ""))' \
    '    sys.exit(int(os.environ.get("BT_RC", "0")))' >"$T/maint/brief.py"
}

: >"$T/sent.txt"
RC="$(run_bt 0)"
check "flag off: the job exits 0"           '[ "$RC" = 0 ]'
check "flag off: nothing is sent"           '[ ! -s "$T/sent.txt" ]'

set_composer
: >"$T/sent.txt"; : >"$T/args.txt"
RC="$(BT_LINE="" run_bt 1)"
check "nothing blocked: the job exits 0"    '[ "$RC" = 0 ]'
check "nothing blocked: nothing is sent"    '[ ! -s "$T/sent.txt" ] && [ ! -s "$T/args.txt" ]'

: >"$T/sent.txt"; : >"$T/args.txt"
RC="$(BT_LINE="Blocked tonight: Decision - capsule-corp/t_1 - Model routing (open 9 h)" run_bt 1)"
check "one blocked item: the job exits 0"   '[ "$RC" = 0 ]'
check "one blocked item: exactly one message, the one line" \
  '[ "$(wc -l <"$T/sent.txt" | tr -d " ")" = 1 ] && grep -q "^Blocked tonight: Decision - capsule-corp/t_1" "$T/sent.txt"'
check "one blocked item: goes out as class blocked, through capsule outbox-send" 'grep -q "^-m capsule outbox-send blocked" "$T/args.txt"'
check "one blocked item: counts against no scheduled slot" '[ ! -f "$T/message-budget.json" ]'
check "REPORT_NO_STAMP writes no stamp"     '[ ! -f "$T/maint/state/jobs/blocked-tonight.json" ]'
check "the job never writes the vault"      '[ -z "$(find "$T/home/my-vault" -name "*.md" 2>/dev/null)" ]'

# No notes vault configured: the blocked-tonight line needs no vault, so it is still composed and sent.
mkdir -p "$T/nohost"; printf '{}\n' >"$T/nohost/host.json"
: >"$T/sent.txt"
BT_LINE="Blocked tonight: Decision - capsule-corp/t_1 - Model routing (open 9 h)" \
FAKE_SENT="$T/sent.txt" FAKE_ARGS="$T/args.txt" FAKE_LEGACY_RC=0 \
env -u OBSIDIAN_VAULT_PATH HOME="$T/home" MAINT_DIR="$T/maint" CAPSULE_PY="$T/home/.local/bin/fakepy" \
  CAPSULE_HOME="$T/nohost" REPORT_CAPSULE_CORP_DIR="$REPO" OPS_BRIEF_V2=1 REPORT_NO_STAMP=1 TG_SEND_NO_SPOOL=1 \
  OPS_MESSAGE_STATE="$T/message-budget.json" bash "$T/maint/report.sh" --blocked-tonight >/dev/null 2>&1
RC=$?
check "no vault: the blocked-tonight job exits 0 and still sends its line" \
  '[ "$RC" = 0 ] && grep -q "^Blocked tonight: Decision - capsule-corp/t_1" "$T/sent.txt"'

: >"$T/sent.txt"
RC="$(FAKE_SEND_RC=1 BT_LINE="Blocked tonight: Stuck - t_1 - blocked (open 3 h)" run_bt 1)"
check "failed send: the job exits non-zero" '[ "$RC" != 0 ]'

# a crashing composer is a loud failure, never a silent "nothing blocked"
: >"$T/sent.txt"
RC="$(BT_RC=3 BT_LINE="" run_bt 1)"
check "composer crash: nothing is sent"     '[ ! -s "$T/sent.txt" ]'
check "composer crash: the job fails loudly" '[ "$RC" != 0 ]'

# the shape guard: exit 0 but not a "Blocked tonight: " line must never be sent
: >"$T/sent.txt"
RC="$(BT_RC=0 BT_LINE="garbage" run_bt 1)"
check "composer garbage (no shape): nothing is sent" '[ ! -s "$T/sent.txt" ]'
check "composer garbage (no shape): the job fails loudly" '[ "$RC" != 0 ]'
cp "$T/maint/brief.py.real" "$T/maint/brief.py"

if [ -n "$OLD" ]; then check "delivery never invokes the old sender" '[ ! -e "$T/old-sender-ran" ]'; else echo "SKIP delivery never invokes the old sender: release/private-denylist.txt absent"; fi
[ "$fails" -eq 0 ] && echo "test_blocked_tonight.sh: all passed" || echo "test_blocked_tonight.sh: $fails failed"
exit "$fails"
