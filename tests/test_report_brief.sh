#!/usr/bin/env bash
# tests/test_report_brief.sh - exercises maintenance/report.sh's brief_v2()
# in isolation (the `sed -n '/^fn()/,/^}/p'` idiom tests/test_report_budgets.sh
# uses), against a scratch MAINT_DIR. No real HOME, vault, board, docker,
# Postgres or network is touched, and nothing is ever sent.
set -u
unset CAPSULE_JOBS_DIR   # the jobs config dir comes from each case's fake home
unset CAPSULE_HOME AGENT_OPS_REPOS_JSON AGENT_OPS_NO_SEND
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
T="$(mktemp -d "${TMPDIR:-/tmp}/report-brief.XXXXXX")"; trap 'rm -rf "$T"' EXIT

# A fake docker, found through AGENT_OPS_DOCKER_BIN (report.sh rebuilds PATH): no real docker ever runs.
mkdir -p "$T/dockerstub"; printf '#!/bin/sh\nexit 0\n' >"$T/dockerstub/docker"; chmod +x "$T/dockerstub/docker"
export AGENT_OPS_DOCKER_BIN="$T/dockerstub"
# a fixture dir, so nothing here ever reads a real ~/.local/share/capsule
mkdir -p "$T/capsule-home"; export CAPSULE_HOME="$T/capsule-home"
# Per-run no-send spool: events.py honours CAPSULE_NO_SEND_SPOOL under AGENT_OPS_NO_SEND.
export CAPSULE_NO_SEND_SPOOL="$T/no-send-spool.jsonl"

fails=0
check() { if eval "$2"; then echo "PASS $1"; else echo "FAIL $1"; fails=$((fails+1)); fi; }

SRC="$(sed -n '/^brief_v2()/,/^}/p' "$REPO/maintenance/report.sh")"
check "brief_v2() found in report.sh" '[ -n "$SRC" ]'

mkdir -p "$T/state/jobs"
# brief.py lives directly in $T and imports `capsule` (host_registry.boards_served); expose the repo package
export PYTHONPATH="$REPO${PYTHONPATH:+:$PYTHONPATH}"
for f in brief.py messages.py budgets.py events.py docker_engine.py heal_table.py scrub.py vault_git.py vault_conf.py; do
  cp "$REPO/maintenance/$f" "$T/$f"
done
: >"$T/state/events.jsonl"

run_brief() {
  # OPS_BRIEF_TIMEOUT is normalised to the bare $OPS_BRIEF_TIMEOUT form inside
  # brief_v2() (NORMALISE fix) - report.sh itself defaults it once at the top
  # (":-30"), which this sed-extracted function body does not carry, so this
  # harness supplies the same default explicitly.
  MAINT_DIR="$T" OPS_BRIEF_V2="${1:-0}" OPS_BRIEF_TIMEOUT="${OPS_BRIEF_TIMEOUT:-30}" \
    AGENT_OPS_NO_SEND=1 OBSIDIAN_VAULT_PATH="$T/vault" bash -c "$SRC
brief_v2" 2>/dev/null
}

OUT="$(run_brief 0)"
check "flag off: brief_v2 prints nothing" '[ -z "$OUT" ]'

OUT="$(run_brief 1)"
check "flag on: the header is first"      'printf %s "$OUT" | head -1 | grep -q "^== Capsule - "'
check "flag on: the waiting block prints" 'printf %s "$OUT" | grep -q "^WAITING ON YOU - "'
check "flag on: the closing line prints"  'printf %s "$OUT" | grep -q "Nothing else needs you today."'
# R5-5 (fix round 1): the previous "no secret-looking value" check composed
# from an EMPTY scratch state directory, so no secret could have been present
# regardless of what the code did - vacuous. This scratch fixture set has no
# credential-bearing input anywhere in brief.py's read set (it never touches
# ops.env, capsule-secret or Grafana - report.sh's notices_section() does,
# and that stays outside the OPS_BRIEF_V2 branch per R5-1), so there is no
# fixture that would make this check non-vacuous here. Dropped rather than
# kept as decoration; secret-value hygiene is tests/test_no_secret_values.sh
# and tests/test_egress's job, not this suite's.

# R5-3 (fix round 1): the --extra-file plumbing - the entire point of this
# task - had zero coverage. run_brief above always calls bare `brief_v2`
# (extra=""), so every prior check took the else branch. Exercise the
# extra-file argument directly.
FIXTURE_EXTRA="$T/extra-fixture.txt"
printf 'power: 9 merged (24h), median 42 tokens\n' >"$FIXTURE_EXTRA"
run_brief_with_extra() {
  MAINT_DIR="$T" OPS_BRIEF_V2=1 OPS_BRIEF_TIMEOUT="${OPS_BRIEF_TIMEOUT:-30}" \
    AGENT_OPS_NO_SEND=1 OBSIDIAN_VAULT_PATH="$T/vault" EXTRA="$FIXTURE_EXTRA" bash -c "$SRC
brief_v2 \"\$EXTRA\"" 2>/dev/null
}
OUT="$(run_brief_with_extra)"
check "the --extra-file line reaches the composed brief" \
  'printf %s "$OUT" | grep -q "power: 9 merged (24h), median 42 tokens"'

# A crashing composer must degrade to nothing (report.sh then keeps v1), never
# to a half-message.
mv "$T/brief.py" "$T/brief.py.bak"; printf 'raise SystemExit(3)\n' >"$T/brief.py"
OUT="$(run_brief 1)"
check "a crashing brief.py prints nothing" '[ -z "$OUT" ]'
mv "$T/brief.py.bak" "$T/brief.py"

# R5-2 (fix round 1): "prints nothing" degrades correctly; "never returns"
# does not. A wedged composer must not hang the caller - brief_v2 wraps every
# call in `timeout "${OPS_BRIEF_TIMEOUT:-30}"`. Shrink that via the env
# override so the test doesn't actually wait out a real timeout: the fixture
# composer sleeps 2s, the timeout is set to 1s, so this case degrades to
# nothing and returns in ~1s, not 2+.
#
# row 511 G4 (flake fix): `date +%s` is whole-second resolution, so a start
# time captured near a second boundary can make a real ~1.0-1.3s duration
# read back as ELAPSED=2 even though nothing was slow - purely clock
# quantization, no actual hang. That is rare standalone (a narrow fractional-
# second window) but the process startup this measures (bash -c, python3,
# timeout) is slower under the CPU contention of the full shell loop, which
# widens the window - reproduced directly: 0/20 standalone, 1/20 with four
# `yes` processes competing for CPU, both against the old `date +%s` code;
# 0/20 against the fix below under the same contention. Use python3's
# float-seconds clock (stdlib, matches this repo's Python-only convention)
# so ELAPSED is the real wall-clock duration, not a rounded one.
mv "$T/brief.py" "$T/brief.py.bak"; printf 'import time\ntime.sleep(2)\n' >"$T/brief.py"
START="$(python3 -c 'import time; print(time.time())')"
OUT="$(MAINT_DIR="$T" OPS_BRIEF_V2=1 OPS_BRIEF_TIMEOUT=1 AGENT_OPS_NO_SEND=1 \
  OBSIDIAN_VAULT_PATH="$T/vault" bash -c "$SRC
brief_v2" 2>/dev/null)"
ELAPSED="$(python3 -c "import time; print(time.time() - $START)")"
check "a hanging brief.py prints nothing" '[ -z "$OUT" ]'
check "a hanging brief.py degrades promptly (<2s under a 1s timeout)" \
  'python3 -c "import sys; sys.exit(0 if float(\"$ELAPSED\") < 2 else 1)"'
mv "$T/brief.py.bak" "$T/brief.py"

# R5-4 (fix round 1): `grep -q "== Capsule" report.sh` / `grep -q "BOARD
# STANDUP" report.sh` asserted only that those strings appear SOMEWHERE in a
# 540-line file - both pass even with the entire mode-aware shape guard
# deleted. Extract the guard block itself (the same sed idiom this file
# already uses for brief_v2) and actually exercise it: it must let a
# correctly-shaped message through and must fail the job (JOB_EXIT=1, no
# send) on a malformed one, in both modes.
GUARD_SRC="$(sed -n '/^  # Shape guard, per mode/,/^  fi$/p' "$REPO/maintenance/report.sh")"
check "the shape guard block was extracted from report.sh" '[ -n "$GUARD_SRC" ]'

run_guard() {
  # $1=MESSAGE $2=GUARD; echoes GUARDED_OK only if the guard did not exit.
  # A3 (row 201): the guard block now also calls verdict_table() on the way
  # out - stub it here (this harness isolates only the guard `if`, not the
  # table it renders) so a rejection prints GUARDCALLED instead of erroring
  # "command not found", and so a reject case is distinguishable from a pass.
  MESSAGE="$1" GUARD="$2" JOB_EXIT="" bash -c "verdict_table() { echo GUARDCALLED; }
$GUARD_SRC
echo GUARDED_OK"
}

OUT="$(run_guard '== Capsule - Fri 11 Sep - 08:30 ====' '== Capsule')"
check "guard passes a well-shaped v2 message"   '[ "$OUT" = "GUARDED_OK" ]'
OUT="$(run_guard 'BOARD STANDUP\nsome digest text' 'BOARD STANDUP')"
check "guard passes a well-shaped v1 message"   '[ "$OUT" = "GUARDED_OK" ]'
OUT="$(run_guard 'nonsense, no header at all' '== Capsule')"
check "guard rejects a malformed message (v2 mode)" '[ "$OUT" != "GUARDED_OK" ]'
OUT="$(run_guard 'nonsense, no header at all' 'BOARD STANDUP')"
check "guard rejects a malformed message (v1 mode)" '[ "$OUT" != "GUARDED_OK" ]'
OUT="$(run_guard 'nonsense, no header at all' '== Capsule')"
check "guard rejects a malformed message and calls verdict_table" \
  '[ "$(printf %s "$OUT" | tail -1)" = "GUARDCALLED" ]'

# --- A3 (row 201): the six-line "report: verdicts" block (design
# 2026-09-12-report-verdict-table-design.md section 4, assertions a-f).
# Extract the whole compose -> guard -> send -> vault-copy -> budget -> exit
# section (same sed idiom as SRC/GUARD_SRC above) and drive it end to end
# against a fake tg_send and a fixture brief.py, so the block is exercised
# against the real report.sh code, not a reimplementation of it.
MAIN_SRC="$(sed -n "/# A3 (row 201): six-line verdict block/,/JOB_EXIT=\\\$send_rc; exit \\\$send_rc/p" "$REPO/maintenance/report.sh")"
check "the verdict-table section was extracted from report.sh" '[ -n "$MAIN_SRC" ]'
check "the extracted section defines verdict_table()" 'printf %s "$MAIN_SRC" | grep -q "verdict_table() {"'

cp "$T/brief.py" "$T/brief.py.real2"

run_main() {
  # $1=OPS_BRIEF_V2 $2=DIGEST_DISPLAY $3=FAKE_SEND_RC; prints combined
  # stdout+stderr (report.sh runs this whole section inside `>"$LOG" 2>&1`,
  # so merging the two here matches what actually lands in the job log).
  MAINT_DIR="$T" OPS_BRIEF_TIMEOUT="${OPS_BRIEF_TIMEOUT:-30}" OPS_BRIEF_V2="${1:-0}" \
    DIGEST_DISPLAY="$2" TARGET="test-chat" SUBJECT="Test Subject" FAKE_SEND_RC="${3:-0}" \
    POWER_LINES="" RETRIEVAL_LINES="" RETENTION_LINE="" INDEX_LINE="" SOULS_LINES="" \
    AGENT_OPS_NO_SEND=1 OBSIDIAN_VAULT_PATH="$T/vault" \
    bash -c "tg_send() { cat >/dev/null; return \"\${FAKE_SEND_RC:-0}\"; }
JOB_EXIT=\"\"
$SRC
$MAIN_SRC" 2>&1
  echo "RC=$?"
}

BOARD_MSG='BOARD STANDUP
some digest text'

# (a) flag off -> table with composer v1, OPS_BRIEF_V2=0
OUT="$(run_main 0 "$BOARD_MSG" 0)"
check "(a) flag off: table header present"   'printf %s "$OUT" | grep -q "^report: verdicts "'
check "(a) flag off: composer row is v1, OPS_BRIEF_V2=0" \
  'printf %s "$OUT" | grep -qE "^composer +v1 +OPS_BRIEF_V2=0$"'
check "(a) flag off: guard row is ok"        'printf %s "$OUT" | grep -qE "^guard +ok +BOARD STANDUP$"'
check "(a) flag off: send row is ok"         'printf %s "$OUT" | grep -qE "^send +ok +target test-chat, slack send exit 0$"'
check "(a) flag off: vault-copy row is skipped (v1)" 'printf %s "$OUT" | grep -qE "^vault-copy +skipped \(v1\)"'
check "(a) flag off: budget row is skipped"  'printf %s "$OUT" | grep -qE "^budget +skipped"'
check "(a) flag off: exits 0"                'printf %s "$OUT" | grep -q "^RC=0$"'

# (b) v2 success -> composer v2, send ok, vault-copy ok <path>, budget counted
cat >"$T/brief.py" <<'PY'
#!/usr/bin/env python3
import sys
if "--vault-copy" in sys.argv:
    print("Knowledge/Morning report 2026-09-12.md")
else:
    print("== Capsule - test")
    print("WAITING ON YOU - nothing")
PY
OUT="$(run_main 1 "$BOARD_MSG" 0)"
check "(b) v2 success: composer row is v2 with the brief.py detail" \
  'printf %s "$OUT" | grep -qE "^composer +v2 +brief\.py exit 0, .== Capsule. header present$"'
check "(b) v2 success: send row is ok"       'printf %s "$OUT" | grep -qE "^send +ok +target test-chat, slack send exit 0$"'
check "(b) v2 success: vault-copy row is ok with the note path" \
  'printf %s "$OUT" | grep -qE "^vault-copy +ok +Knowledge/Morning report 2026-09-12\.md$"'
check "(b) v2 success: budget row is counted scheduled" \
  'printf %s "$OUT" | grep -qE "^budget +counted +scheduled$"'
cp "$T/brief.py.real2" "$T/brief.py"

# (c) fake brief.py exit 3 -> composer v1, brief.py exit 3 - <stderr line>
cat >"$T/brief.py" <<'PY'
#!/usr/bin/env python3
import sys
sys.stderr.write("boom happened\n")
sys.exit(3)
PY
OUT="$(run_main 1 "$BOARD_MSG" 0)"
check "(c) composer crash: composer row is v1 with the brief.py reason" \
  'printf %s "$OUT" | grep -qE "^composer +v1 +brief\.py exit 3 - boom happened$"'
cp "$T/brief.py.real2" "$T/brief.py"

# (d) guard failure -> table present, send skipped (guard), rc 1. Forced by
# an (unrealistic in production, since the digest's own earlier guard - see
# report.sh:480 - already guarantees this) DIGEST_DISPLAY missing "BOARD
# STANDUP"; this harness controls DIGEST_DISPLAY directly, which is exactly
# what lets it reach the belt-and-suspenders guard at the compose site.
OUT="$(run_main 0 'no marker in here at all' 0)"
check "(d) guard failure: table header present"  'printf %s "$OUT" | grep -q "^report: verdicts "'
check "(d) guard failure: guard row is FAIL"     'printf %s "$OUT" | grep -qE "^guard +FAIL +BOARD STANDUP$"'
check "(d) guard failure: send row is skipped (guard)" 'printf %s "$OUT" | grep -qE "^send +skipped \(guard\)"'
check "(d) guard failure: vault-copy row is skipped (guard)" 'printf %s "$OUT" | grep -qE "^vault-copy +skipped \(guard\)"'
check "(d) guard failure: budget row is skipped" 'printf %s "$OUT" | grep -qE "^budget +skipped"'
check "(d) guard failure: exits 1"               'printf %s "$OUT" | grep -q "^RC=1$"'

# (e) fake old sender exit 1 -> send FAIL, vault-copy skipped (send failed), budget skipped
cat >"$T/brief.py" <<'PY'
#!/usr/bin/env python3
import sys
if "--vault-copy" in sys.argv:
    print("Knowledge/Morning report 2026-09-12.md")
else:
    print("== Capsule - test")
    print("WAITING ON YOU - nothing")
PY
OUT="$(run_main 1 "$BOARD_MSG" 1)"
check "(e) send failure: send row is FAIL"       'printf %s "$OUT" | grep -qE "^send +FAIL +target test-chat, slack send exit 1$"'
check "(e) send failure: vault-copy row is skipped (send failed)" \
  'printf %s "$OUT" | grep -qE "^vault-copy +skipped \(send failed\)"'
check "(e) send failure: budget row is skipped" 'printf %s "$OUT" | grep -qE "^budget +skipped"'
check "(e) send failure: exits 1"               'printf %s "$OUT" | grep -q "^RC=1$"'
cp "$T/brief.py.real2" "$T/brief.py"

# (f) the block is exactly six lines starting "report: verdicts" and is the
# last thing before "=== report done" (checked against the real full run.sh
# job log, not just this section in isolation - see the "end to end" run
# below, which drives the actual report.sh entry point).
cat >"$T/brief.py" <<'PY'
#!/usr/bin/env python3
import sys
if "--vault-copy" in sys.argv:
    print("Knowledge/Morning report 2026-09-12.md")
else:
    print("== Capsule - test")
    print("WAITING ON YOU - nothing")
PY
OUT="$(run_main 1 "$BOARD_MSG" 0)"
BLOCK="$(printf %s "$OUT" | grep -n '^report: verdicts ' | head -1 | cut -d: -f1)"
check "(f) exactly six lines starting at the header" \
  '[ -n "$BLOCK" ] && [ "$(printf %s "$OUT" | sed -n "${BLOCK},\$p" | sed -n "1,6p" | wc -l | tr -d " ")" = 6 ] && printf %s "$OUT" | sed -n "${BLOCK},\$p" | sed -n "1p" | grep -q "^report: verdicts " && printf %s "$OUT" | sed -n "${BLOCK},\$p" | sed -n "2p" | grep -q "^composer" && printf %s "$OUT" | sed -n "${BLOCK},\$p" | sed -n "6p" | grep -q "^budget"'
cp "$T/brief.py.real2" "$T/brief.py"

# --- (f), end to end: run the real report.sh entry point (not just the
# extracted section) with a fake old sender on PATH, the way tests/test_close.sh
# already does for --close, and check the job log itself: the block is the
# very last thing before "=== report done".
mkdir -p "$T/home/.local/bin" "$T/home/.config/capsule-corp"
OLD="$(python3 "$REPO/tests/denylist_term.py" retired)" || { echo "FAIL: denylist_term: release/private-denylist.txt is unusable"; exit 1; }
if [ -n "$OLD" ]; then
cat >"$T/home/.local/bin/$OLD" <<'SH'
#!/usr/bin/env bash
cat >>"$FAKE_SENT"
exit "${FAKE_SEND_RC:-0}"
SH
chmod +x "$T/home/.local/bin/$OLD"
fi
# The full brief mode's digest step (vault_jobs.py) imports sibling modules
# (board.py etc) by relative path off MAINT_DIR, unlike the fixture-only set
# test_close.sh gets away with for --close (which never reaches the digest
# at all) - so this fixture copies the whole maintenance/ directory.
cp -R "$REPO/maintenance" "$T/e2e-maint"
# board_fence.py (C29) imports the sibling `capsule` package from the maintenance dir's parent.
cp -R "$REPO/capsule" "$T/capsule"
mkdir -p "$T/e2e-maint/state/jobs"
: >"$T/e2e-maint/state/events.jsonl"
cat >"$T/e2e-maint/brief.py" <<'PY'
#!/usr/bin/env python3
import sys
if "--vault-copy" in sys.argv:
    print("Knowledge/Morning report 2026-09-12.md")
else:
    print("== Capsule - test")
    print("WAITING ON YOU - nothing")
    print("Nothing else needs you today.")
PY
printf 'OPS_CHAT_PROFILE=telegram\nOPS_ALERT_TARGET=telegram:-100:1\n' >"$T/home/.config/capsule-corp/ops.env"
mkdir -p "$T/home/vault"

FAKE_SENT="$T/e2e-sent.txt" FAKE_SEND_RC=0 HOME="$T/home" MAINT_DIR="$T/e2e-maint" \
  OBSIDIAN_VAULT_PATH="$T/home/vault" AGENT_OPS_NO_SEND=1 OPS_BRIEF_V2=1 \
  REPORT_NO_STAMP=1 TG_SEND_NO_SPOOL=1 OPS_MESSAGE_STATE="$T/e2e-message-state.json" \
  bash "$REPO/maintenance/report.sh" >/dev/null 2>&1
E2E_LOG="$(ls -t "$T/e2e-maint/logs"/report-*.log 2>/dev/null | head -1)"
check "(f) end to end: a job log was written"   '[ -n "$E2E_LOG" ]'
check "(f) end to end: report: verdicts appears exactly once" \
  '[ "$(grep -c "^report: verdicts " "$E2E_LOG")" = 1 ]'
check "(f) end to end: the block is the last thing before === report done" \
  'tail -7 "$E2E_LOG" | sed -n "1p" | grep -q "^report: verdicts " && tail -1 "$E2E_LOG" | grep -q "^=== report done"'
check "(f) end to end: the send line says slack" \
  'grep -q "^=== slack send exit [0-9]* ===$" "$E2E_LOG"'
if [ -n "$OLD" ]; then check "(f) end to end: the send line is never the old sender" '! grep -qi "$OLD send" "$E2E_LOG"'
else echo "SKIP (f) end to end: the send line is never the old sender: release/private-denylist.txt absent"; fi
check "(f) end to end: no secret-looking value in the block" \
  '! grep -A6 "^report: verdicts " "$E2E_LOG" | grep -Ei "bot[0-9]+:|xox[bp]-|api[_-]?key"'

# --- F6 (row 208): report.sh's tg_send must log every send as a `message`
# event, exactly as run.sh's tg_send already does - before this fix, the
# 08:30 brief and the 18:00 close (and their job-failure alerts) were
# invisible in events.jsonl / Postgres. Extract the real tg_send() (same sed
# idiom SRC/GUARD_SRC/MAIN_SRC already use above) and call it directly
# against a scratch EVENTS_SPOOL and a fake old sender on PATH.
TG_SEND_SRC="$(sed -n '/^tg_send()/,/^}/p' "$REPO/maintenance/report.sh")"
LIB_SRC=". \"$REPO/maintenance/lib_send.sh\""
check "report.sh carries a sourceable tg_send() function" '[ -n "$TG_SEND_SRC" ]'
check "tg_send() calls ev_log before delivery" \
  'printf %s "$TG_SEND_SRC" | grep -q "^  ev_log message report.sh"'

# Hermetic: the spool is pinned to this test's own dir, the python is plain python3 (the
# old default, a venv python under HOME, does not exist under a fresh HOME, so ev_log
# silently wrote nothing), and this run's own no-send spool stays untouched.
unset EVENTS_SPOOL CAPSULE_OPS_PY
# The OS-temp dir is private to this test (TMPDIR below), so no other process can touch it.
OS_T="$T/ostmp"; mkdir -p "$OS_T"
SHARED_SPOOL="$OS_T/capsule-corp-events-no-send.jsonl"
OWN_BEFORE="$(cksum <"$CAPSULE_NO_SEND_SPOOL" 2>/dev/null || echo none)"
TG_T="$(mktemp -d "$T/tg.XXXXXX")"
mkdir -p "$TG_T/fakebin" "$TG_T/state"
# Fakes: the old sender must never run (marker); python3 records the capsule argv, sends nothing.
[ -n "$OLD" ] && printf '#!/usr/bin/env bash\ntouch "%s/old-sender-ran"\n' "$TG_T" >"$TG_T/fakebin/$OLD"
REAL_PY="$(command -v python3)"
printf '#!/usr/bin/env bash\n[ "${1:-}" = "-P" ] && shift\n[ "$1 $2" = "-m capsule" ] && { echo "$*" >>"%s/capsule-argv"; exit 0; }\nexec "%s" "$@"\n' \
  "$TG_T" "$REAL_PY" >"$TG_T/fakebin/fakepy"
[ -n "$OLD" ] && chmod +x "$TG_T/fakebin/$OLD"
chmod +x "$TG_T/fakebin/fakepy"
TG_SPOOL="$TG_T/events.jsonl"
OUT="$(TMPDIR="$OS_T" PATH="$TG_T/fakebin:$PATH" CAPSULE_OPS_PY="$REAL_PY" CAPSULE_PY="$TG_T/fakebin/fakepy" \
  MAINT_DIR="$REPO/maintenance" EVENTS_SPOOL="$TG_SPOOL" \
  bash -c "$LIB_SRC
$TG_SEND_SRC
EV=\"\$CAPSULE_OPS_PY \$MAINT_DIR/events.py\"
ev_log() { \$EV log \"\$@\" >/dev/null 2>&1 || true; }
STATE_DIR=\"$TG_T/state\"
JOB=report
printf 'hello' | tg_send --to telegram:-100:10 --subject Test
echo rc=\$?")"
check "tg_send() exits 0 and delivers through capsule outbox-send (Slack)" \
  'printf %s "$OUT" | grep -q "^rc=0$" && grep -q "^-m capsule outbox-send blocked hello" "$TG_T/capsule-argv"'
if [ -n "$OLD" ]; then check "tg_send() never invokes the old sender" '[ ! -e "$TG_T/old-sender-ran" ]'
else echo "SKIP tg_send() never invokes the old sender: release/private-denylist.txt absent"; fi
rm -f "$TG_T/capsule-argv"
TMPDIR="$OS_T" PATH="$TG_T/fakebin:$PATH" CAPSULE_PY="$TG_T/fakebin/fakepy" AGENT_OPS_NO_SEND=1 bash -c "$LIB_SRC
printf hi | deliver_via_slack"
check "AGENT_OPS_NO_SEND sends nothing" '[ ! -e "$TG_T/capsule-argv" ]'
check "the events spool holds exactly one row" \
  '[ "$(grep -c . "$TG_SPOOL" 2>/dev/null)" = 1 ]'
check "that row is kind=message from report.sh with the target" \
  'python3 -c "
import json, sys
d = json.loads(open(sys.argv[1]).read().splitlines()[0])
sys.exit(0 if d[\"kind\"] == \"message\" and d[\"source\"] == \"report.sh\"
          and d[\"payload\"][\"target\"] == \"telegram:-100:10\" else 1)
" "$TG_SPOOL"'
check "the shared OS-temp no-send spool was not touched" \
  '[ ! -e "$SHARED_SPOOL" ]'
check "this run's own no-send spool is not the shared one, and was not written" \
  '[ "$CAPSULE_NO_SEND_SPOOL" != "$SHARED_SPOOL" ] && [ "$(cksum <"$CAPSULE_NO_SEND_SPOOL" 2>/dev/null || echo none)" = "$OWN_BEFORE" ]'
rm -rf "$TG_T"

# followups.md line 73: the "Job failed" line from on_exit() must carry
# no v2 class (it stays incident, log-only under v2: Grafana pages job-failed).
EXIT_SRC="$(sed -n '/^on_exit()/,/^trap on_exit EXIT/p' "$REPO/maintenance/report.sh" | sed '$d')"
check "on_exit() was extracted from report.sh" '[ -n "$EXIT_SRC" ]'
EXIT_ENV="$T/exit-env.txt"
JOB=close LOG="$T/x.log" LOG_DIR="$T" JOB_EXIT=3 EXIT_ENV="$EXIT_ENV" bash -c "write_stamp() { :; }
tg_send() { printf 'class=%s v2=%s\n' \"\$MSG_CLASS\" \"\${MSG_V2_CLASS:-}\" >\"\$EXIT_ENV\"; cat >/dev/null; }
TARGET=t
$EXIT_SRC
on_exit" >/dev/null 2>&1
check "a failed job's alert is class incident" 'grep -q "^class=incident " "$EXIT_ENV"'
check "a failed job's alert carries no v2 class" 'grep -q " v2=$" "$EXIT_ENV"'

[ "$fails" -eq 0 ] && echo "test_report_brief.sh: all passed" || echo "test_report_brief.sh: $fails failed"
exit "$fails"
