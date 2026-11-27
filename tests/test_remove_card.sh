#!/usr/bin/env bash
# tests/test_remove_card.sh - the quarterly "what do we remove?" card job (M256, row 139): run.sh
# remove-card, run against a scratch state dir with a FAKE capsule CLI. No real card is filed, no plist
# is loaded, nothing is sent. Modelled on tests/test_blocked_tonight.sh.
set -u
unset CAPSULE_JOBS_DIR   # the jobs config dir comes from each case's fake home
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT

fails=0
check() { if eval "$2"; then echo "PASS $1"; else echo "FAIL $1"; fails=$((fails+1)); fi; }
# The owner job plists (launchd/templates/*.obsidian-*.plist) are private-only: the public copy drops them. The
# private tree is told apart by release/private-denylist.txt, which the export never ships. A check on the plist
# runs in full in the private tree and skips, with a printed reason, in the public copy.
plcheck() {
  if [ -f "$REPO/release/private-denylist.txt" ]; then check "$@"; else echo "SKIP $1: the owner job plist is private-only (not in the public copy)"; fi
}
PLIST="$(ls "$REPO"/launchd/templates/*.obsidian-remove-card.plist 2>/dev/null | head -1)"; LABEL="$(basename "$PLIST" .plist)"   # the overlay template names its own prefix

# --- the wiring is present -------------------------------------------------
check "the card template exists"            '[ -s "$REPO/maintenance/templates/remove-card.md" ]'
plcheck "the plist runs run.sh remove-card"   'grep -q "run.sh" "$PLIST" && grep -q "<string>remove-card</string>" "$PLIST"'
plcheck "the plist fires on 4 calendar dicts" '[ "$(grep -c "<key>Day</key><integer>1</integer>" "$PLIST")" = 4 ]'
for m in 1 4 7 10; do
  plcheck "the plist has month $m at 09:00"   'grep -q "<key>Month</key><integer>'"$m"'</integer><key>Day</key><integer>1</integer><key>Hour</key><integer>9</integer>" "$PLIST"'
done
check "the plist is classified custodial (bin/lib_jobs.sh)" 'grep -q "obsidian-remove-card" "$REPO/bin/lib_jobs.sh"'
plcheck "the plist is loaded (not in launchd/unloaded)"  '! grep -qx "$LABEL" "$REPO/launchd/unloaded"'
check "status.sh CADENCE lists remove-card at 93 days" 'grep -qF "\"remove-card\": 93*86400" "$REPO/maintenance/status.sh"'
check "run.sh excludes remove-card from the PROMPT_FILE chain" 'grep -qF "[ \"\$JOB\" != \"remove-card\" ]" "$REPO/maintenance/run.sh"'

# --- run.sh remove-card ------------------------------------------------------
mkdir -p "$T/home/.local/bin" "$T/maint/templates" "$T/state" "$T/vault"
cp "$REPO/maintenance/templates/remove-card.md" "$T/maint/templates/"
REAL_PY="$(command -v python3)"
printf '%s\n' '#!/usr/bin/env bash' \
  '[ "${1:-}" = "-P" ] && shift' \
  'if [ "$1 $2" = "-m capsule" ]; then shift 2; echo "$@" >>"$FAKE_ARGS"; exit "${FAKE_RC:-0}"; fi' \
  "exec \"$REAL_PY\" \"\$@\"" >"$T/home/.local/bin/fakepy"
chmod +x "$T/home/.local/bin/fakepy"
run_rc() {
  FAKE_ARGS="$T/args.txt" HOME="$T/home" RUN_SH_MAINT_DIR="$T/maint" AGENT_OPS_STATE_DIR="$T/state" \
  OBSIDIAN_VAULT_PATH="$T/vault" CAPSULE_PY="$T/home/.local/bin/fakepy" env "$@" \
    bash "$REPO/maintenance/run.sh" remove-card >"$T/out.txt" 2>&1
  echo $?
}
Q="$(date +%Y)-Q$(( (10#$(date +%m) - 1) / 3 + 1 ))"
# run.sh resets PATH, so a fake `date` on PATH would never be found; shadow it with a function and
# eval run.sh's own quarter line, so months 08 and 09 (octal traps without the 10# guard) are covered.
quarter_for_month() {
  date() { if [ "$1" = +%Y ]; then echo 2026; else echo "$M"; fi; }
  M="$1"; eval "$(grep -m1 '^    Q=' "$REPO/maintenance/run.sh")"; echo "$Q"; }
check "month 08 is 2026-Q3 (octal guard)"   '[ "$(quarter_for_month 08)" = 2026-Q3 ]'
check "month 09 is 2026-Q3 (octal guard)"   '[ "$(quarter_for_month 09)" = 2026-Q3 ]'
check "month 12 is 2026-Q4"                 '[ "$(quarter_for_month 12)" = 2026-Q4 ]'

: >"$T/args.txt"
RC="$(run_rc RUN_SH_DRY_RUN=1)"
check "dry run exits 0"                     '[ "$RC" = 0 ]'
check "dry run says what it would do"       'grep -q "dry run: would file the quarterly remove card" "$T/out.txt"'
check "dry run files nothing, stamps nothing" '[ ! -s "$T/args.txt" ] && [ ! -e "$T/state/remove-card-$Q" ]'

RC="$(run_rc FAKE_RC=1)"
check "a failed intake exits non-zero"      '[ "$RC" != 0 ]'
check "a failed intake leaves no stamp (it retries)" '[ ! -e "$T/state/remove-card-$Q" ]'

: >"$T/args.txt"
RC="$(run_rc FAKE_RC=0)"
check "first run exits 0"                   '[ "$RC" = 0 ]'
check "first run files one card on capsule-corp with the quarter in the title" \
  '[ "$(wc -l <"$T/args.txt" | tr -d " ")" = 1 ] && grep -q "^intake capsule-corp --title Quarterly: what do we remove? $Q --body-file $T/maint/templates/remove-card.md" "$T/args.txt"'
check "first run writes the quarter stamp"  '[ -e "$T/state/remove-card-$Q" ]'

: >"$T/args.txt"
RC="$(run_rc FAKE_RC=0)"
check "second run in the same quarter exits 0" '[ "$RC" = 0 ]'
check "second run is a no-op (no second card)" '[ ! -s "$T/args.txt" ]'
check "the job never writes the vault"      '[ -z "$(find "$T/vault" -type f 2>/dev/null)" ]'

[ "$fails" -eq 0 ] && echo "test_remove_card.sh: all passed" || echo "test_remove_card.sh: $fails failed"
exit "$fails"
