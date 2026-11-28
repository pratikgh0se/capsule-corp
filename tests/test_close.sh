#!/usr/bin/env bash
# tests/test_close.sh - maintenance/report.sh --close (the 18:00 job), run
# against a scratch MAINT_DIR with a FAKE sender on PATH. Nothing is sent,
# no vault is written, no plist is loaded, no network is touched.
set -u
unset CAPSULE_JOBS_DIR   # the jobs config dir comes from each case's fake home
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT

# These tests check delivery through their own fake sender (CAPSULE_PY); a caller's AGENT_OPS_NO_SEND=1 would make lib_send skip it.
unset AGENT_OPS_NO_SEND
fails=0
check() { if eval "$2"; then echo "PASS $1"; else echo "FAIL $1"; fails=$((fails+1)); fi; }

# --- the wiring is present -------------------------------------------------
check "report.sh has a --close mode" 'grep -q -- "--close) MODE=close" "$REPO/maintenance/report.sh"'
check "run.sh knows the close job" 'grep -q "JOB\" = \"close\"" "$REPO/maintenance/run.sh"'
PLIST="$(ls "$REPO"/launchd/templates/*.obsidian-close.plist 2>/dev/null | head -1)"; LABEL="$(basename "$PLIST" .plist)"   # the overlay template names its own prefix
check "the close plist exists" '[ -f "$PLIST" ]'
check "the close plist fires at 18:00" 'grep -q "<integer>18</integer>" "$PLIST" && grep -q "<integer>0</integer>" "$PLIST"'
check "the close plist runs run.sh close" 'grep -q "run.sh" "$PLIST" && grep -q "<string>close</string>" "$PLIST"'
# Content match, not a line-number window: run.sh's PROMPT_FILE exclusion
# chain must list "close" beside every other script job (T6-C1: this used
# to be checked by grepping a hard-coded line window, which cannot tell
# "the job runs" from "the job dies before reaching this branch" - see the
# behavioural checks below, which are the real regression test for T6-C1).
check "run.sh excludes close from the PROMPT_FILE chain" \
  'grep -qE "\\[ \"\\\$JOB\" != \"cleanup\" \\] && \\[ \"\\\$JOB\" != \"close\" \\]" "$REPO/maintenance/run.sh"'
check "the close job prints no model line" \
  'grep -qE "sprint-sync\|retrieval-eval\|heal\|obs-probe\|obs-inventory\|cleanup\|close\|morning\|health-check\|backup\|restore-verify\|fork-sync\|quality-nightly\|deps-sync\)" "$REPO/maintenance/run.sh"'

# --- run.sh close, behaviourally (T6-C1): the plist invokes `run.sh close`,
# not `report.sh --close` directly, so a source grep on report.sh proves
# nothing about whether run.sh ever reaches it. Before the fix, run.sh:178's
# PROMPT_FILE exclusion chain did not list "close" and maintenance/close.md
# does not exist, so the job died at "no prompt file at .../close.md" before
# ever calling report.sh - a nightly false failure alert, every night, with
# the close never sending. This must fail against that broken state.
# Same overrides run.sh honours (see tests/test_run_sh.sh:11-18) - even in a
# dry run, run.sh:73 sources the operator's real ops.env via ambient $HOME,
# and run.sh cd's into the configured notes-vault path, so on CI (neither exists) or a
# real workstation (the real notes vault) this must not touch either.
mkdir -p "$T/run-sh-vault" "$T/run-sh-home"
RUN_SH_OUT="$(HOME="$T/run-sh-home" OBSIDIAN_VAULT_PATH="$T/run-sh-vault" RUN_SH_DRY_RUN=1 bash "$REPO/maintenance/run.sh" close 2>&1)"; RUN_SH_RC=$?
check "run.sh close (dry) exits 0"             '[ "$RUN_SH_RC" = 0 ]'
check "run.sh close (dry) never says no prompt file" '! grep -q "no prompt file" <<<"$RUN_SH_OUT"'
check "run.sh close (dry) would run report.sh --close" 'grep -q "dry run: would run report.sh --close" <<<"$RUN_SH_OUT"'
check "run.sh close (dry) prints no model line"        '! grep -qi "model" <<<"$RUN_SH_OUT"'

# --- run it for real, with a fake Slack sender ------------------------------------
# The fake sender goes in $HOME/.local/bin, because report.sh:23 REPLACES PATH
# with a fixed list ending in $HOME/.local/bin - a $T/bin would be dropped.
mkdir -p "$T/home/.local/bin" "$T/maint/state/jobs" "$T/maint/logs" "$T/home/.config/capsule-corp"
# fake interpreter (CAPSULE_PY): `-m capsule outbox-send CLASS BODY` records BODY, never leaves
# the machine, and exits $FAKE_SEND_RC; anything else runs the real python3.
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
cp "$REPO/maintenance/report.sh" "$REPO/maintenance/lib_state.sh" "$REPO/maintenance/lib_config.sh" "$REPO/maintenance/lib_send.sh" "$REPO/maintenance/lib_vault.sh" "$REPO/maintenance/lib_docker_engine.sh" "$T/maint/"
cp "$T/maint/brief.py" "$T/maint/brief.py.real"
: >"$T/maint/state/events.jsonl"
printf 'OPS_CHAT_PROFILE=telegram\nOPS_ALERT_TARGET=telegram:-100:1\n' >"$T/home/.config/capsule-corp/ops.env"
mkdir -p "$T/home/my-vault"

run_close() {
  FAKE_SENT="$T/sent.txt" FAKE_ARGS="$T/args.txt" FAKE_SEND_RC="${FAKE_SEND_RC:-0}" \
  HOME="$T/home" MAINT_DIR="$T/maint" CAPSULE_PY="$T/home/.local/bin/fakepy" \
  OBSIDIAN_VAULT_PATH="$T/home/my-vault" \
  OPS_BRIEF_V2="${1:-0}" REPORT_NO_STAMP=1 TG_SEND_NO_SPOOL=1 \
  OPS_MESSAGE_STATE="${OPS_MESSAGE_STATE:-}" \
    bash "$T/maint/report.sh" --close >/dev/null 2>&1
  echo $?
}

: >"$T/sent.txt"
RC="$(run_close 0)"
check "flag off: the close job exits 0"    '[ "$RC" = 0 ]'
check "flag off: nothing is sent"          '[ ! -s "$T/sent.txt" ]'

: >"$T/sent.txt"
RC="$(run_close 1)"
check "flag on: the close job exits 0"     '[ "$RC" = 0 ]'
check "flag on: exactly one message"       '[ "$(grep -c "== Capsule" "$T/sent.txt")" = 1 ]'
check "flag on: the quiet contract is last" 'tail -2 "$T/sent.txt" | grep -q "Quiet until 07:30 unless something pages."'
check "flag on: the digest goes out as class scheduled" 'grep -q "^-m capsule outbox-send scheduled" "$T/args.txt"'
check "flag on: TONIGHT is named"         'grep -q "^TONIGHT" "$T/sent.txt"'
check "REPORT_NO_STAMP writes no stamp"    '[ ! -f "$T/maint/state/jobs/close.json" ]'
check "the close never writes the vault"   '[ -z "$(find "$T/home/my-vault" -name "*.md" 2>/dev/null)" ]'
# t_c8b3d583: the job stamp follows AGENT_OPS_STATE_DIR (not MAINT_DIR/state) when REPORT_NO_STAMP is off.
: >"$T/sent.txt"
FAKE_SENT="$T/sent.txt" FAKE_ARGS="$T/args.txt" FAKE_SEND_RC=0 HOME="$T/home" MAINT_DIR="$T/maint" CAPSULE_PY="$T/home/.local/bin/fakepy" \
  AGENT_OPS_STATE_DIR="$T/stamp-root" OBSIDIAN_VAULT_PATH="$T/home/my-vault" AGENT_OPS_NO_SEND=1 \
  OPS_BRIEF_V2=0 TG_SEND_NO_SPOOL=1 bash "$T/maint/report.sh" --close >/dev/null 2>&1
check "the close job stamp lands in AGENT_OPS_STATE_DIR, not beside report.sh" \
  '[ -f "$T/stamp-root/jobs/close.json" ] && [ ! -f "$T/maint/state/jobs/close.json" ]'

# --- Ruling 15 (T6-I4): brief.py must never be CALLED at all with the flag
# off, not merely "not sent". Swap in a brief.py that touches a sentinel
# unconditionally (regardless of --close/args) and prove the sentinel is
# absent after a flag-off run, then present after a flag-on run as a sanity
# check that the harness would actually catch a regression.
SENTINEL="$T/brief-was-called"
cat >"$T/maint/brief.py" <<PY
#!/usr/bin/env python3
import pathlib
pathlib.Path(r"$SENTINEL").touch()
print("== Capsule - sentinel")
print("TONIGHT")
print("Quiet until 07:30 unless something pages.")
PY
rm -f "$SENTINEL"
: >"$T/sent.txt"
RC="$(run_close 0)"
check "Ruling 15: flag off never calls brief.py at all" '[ ! -e "$SENTINEL" ]'
rm -f "$SENTINEL"
: >"$T/sent.txt"
RC="$(run_close 1)"
check "Ruling 15 sanity: flag on does call brief.py (sentinel proves the harness works)" '[ -e "$SENTINEL" ]'
cp "$T/maint/brief.py.real" "$T/maint/brief.py"

# --- messages.record("scheduled") (T6-I2): under AGENT_OPS_NO_SEND=1 with no
# OPS_MESSAGE_STATE override, messages._resolve_write_path REFUSES the
# default path and raises - swallowed by report.sh's `|| true`. Point it at a
# scratch file so the write actually lands, and pin it in both directions:
# recorded exactly once on a successful send, never recorded on a failed one.
MSG_STATE_OK="$T/state-ok/message-budget.json"; mkdir -p "$(dirname "$MSG_STATE_OK")"
: >"$T/sent.txt"
OPS_MESSAGE_STATE="$MSG_STATE_OK"
RC="$(run_close 1)"
check "successful send: exit 0"                  '[ "$RC" = 0 ]'
check "successful send: messages.record fires exactly once" \
  '[ -f "$MSG_STATE_OK" ] && [ "$(python3 -c "import json,sys; print(json.load(open(sys.argv[1])).get(\"scheduled\", 0))" "$MSG_STATE_OK")" = 1 ]'

MSG_STATE_FAIL="$T/state-fail/message-budget.json"; mkdir -p "$(dirname "$MSG_STATE_FAIL")"
: >"$T/sent.txt"
OPS_MESSAGE_STATE="$MSG_STATE_FAIL"
FAKE_SEND_RC=1
RC="$(run_close 1)"
FAKE_SEND_RC=0
OPS_MESSAGE_STATE=""
check "failed send: exits non-zero"              '[ "$RC" != 0 ]'
check "failed send: nothing recorded"            '[ ! -f "$MSG_STATE_FAIL" ]'

# --- composer failure (T6-I3): the one path that can page in production, so
# it needs its own fixtures rather than relying on the happy-path brief.py.
# Both a crashing composer and a composer that prints garbage with no shape
# guard must send NOTHING and fail the job loudly.
cat >"$T/maint/brief.py" <<'PY'
#!/usr/bin/env python3
import sys
sys.stderr.write("boom\n")
sys.exit(3)
PY
: >"$T/sent.txt"
RC="$(run_close 1)"
check "composer crash: nothing is sent"          '[ ! -s "$T/sent.txt" ]'
check "composer crash: the job fails loudly"     '[ "$RC" != 0 ]'

cat >"$T/maint/brief.py" <<'PY'
#!/usr/bin/env python3
print("not a brief, just noise")
PY
: >"$T/sent.txt"
RC="$(run_close 1)"
check "composer garbage (no shape guard): nothing is sent" '[ ! -s "$T/sent.txt" ]'
check "composer garbage (no shape guard): the job fails loudly" '[ "$RC" != 0 ]'
cp "$T/maint/brief.py.real" "$T/maint/brief.py"

if [ -n "$OLD" ]; then check "close delivery never invokes the old sender" '[ ! -e "$T/old-sender-ran" ]'; else echo "SKIP close delivery never invokes the old sender: release/private-denylist.txt absent"; fi
[ "$fails" -eq 0 ] && echo "test_close.sh: all passed" || echo "test_close.sh: $fails failed"
exit "$fails"
