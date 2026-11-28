#!/usr/bin/env bash
# Exercises maintenance/report.sh's power_lines() in isolation: extracts the
# function (the sed pattern tests/test_report_notices.sh uses) and drives it
# with a fake `docker` on PATH, so no real HOME, board, vault or Postgres is
# touched. Registered in CI in Task 8.
set -u
unset CAPSULE_JOBS_DIR   # the jobs config dir comes from each case's fake home
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT

fails=0
check() { if eval "$2"; then echo "PASS $1"; else echo "FAIL $1"; fails=$((fails+1)); fi; }

SRC="$(sed -n '/^power_lines()/,/^}/p' "$REPO/maintenance/report.sh")"
check "power_lines() found in report.sh" '[ -n "$SRC" ]'

# The legacy-era tokens/turns/usd medians are gone with the legacy sessions
# scan (D198/D202); the card.retrieval.tokens digest line went with the dead
# `merged` card writer (sync_done is no longer called).
check "no power line, token, turn or usd clause is left" \
  '! printf %s "$SRC" | grep -qE "capsule.card.(tokens.%|turns|cost_usd)|power:"'
check "no retrieval clause is left (nothing writes the merged card set it read)" \
  '! printf %s "$SRC" | grep -qE "capsule.card.retrieval.tokens|per merged card"'

FAKEBIN="$T/fakebin"; mkdir -p "$FAKEBIN"
write_fake_docker() {   # write_fake_docker <csv-body>
  cat >"$FAKEBIN/docker" <<SH
#!/usr/bin/env bash
cat <<'CSV'
$1
CSV
SH
  chmod +x "$FAKEBIN/docker"
}

run_lines() {  # run_lines  -> stdout of power_lines()
  PATH="$FAKEBIN:$PATH" MAINT_DIR="$T" bash -c "$SRC
power_lines" 2>/dev/null
}

# --- populated: every line present -----------------------------------------
# The triage row's own value CONTAINS a comma (the SQL builds
# `<board>/<task_id>,<n>` as one column), so psql --csv quotes the field -
# exactly as it arrives in production. Fix round 2: the fixture used to leave
# it bare, which is the one shape the real query can never emit, and hid a
# parser that split on the wrong comma.
write_fake_docker "rulings,68
decisions,10
triage,\"capsule-corp/t_94f19b88,3\""
OUT="$(run_lines)"
check "no power line is printed" '! printf %s "$OUT" | grep -q "^power:"'
check "delegation line present"  'printf %s "$OUT" | grep -q "^delegation: 68 machine rulings : 10 your decisions"'
check "triage line present"      'printf %s "$OUT" | grep -q "^triage: capsule-corp/t_94f19b88 failed 3x"'
check "no window line when flag off" '! printf %s "$OUT" | grep -q "^window:"'

# --- empty: every line absent, not blank -----------------------------------
write_fake_docker "rulings,0
decisions,0"
OUT="$(run_lines)"
check "delegation absent when both zero"      '! printf %s "$OUT" | grep -q "^delegation:"'
check "triage absent with no three-strike card" '! printf %s "$OUT" | grep -q "^triage:"'

# --- window line only behind the flag --------------------------------------
mkdir -p "$T/state"
printf '[{"board":"capsule-corp","task_id":"t_1","reason":"headroom 2.0%% under 10.0%%","ts":1}]' \
  >"$T/state/window-queue.json"
write_fake_docker "rulings,0
decisions,0"
OUT="$(PATH="$FAKEBIN:$PATH" STATE_DIR="$T/state" OPS_WINDOW_PREFLIGHT=1 bash -c "$SRC
power_lines" 2>/dev/null)"
check "window line present behind the flag" 'printf %s "$OUT" | grep -q "^window: queued 1 card"'

# --- CSV quoting: three triage rows, one per card, each field quoted --------
rm -f "$T/state/window-queue.json"
write_fake_docker "rulings,0
decisions,0
triage,\"capsule-corp/t_aaa,4\"
triage,\"sample-app/t_bbb,11\""
OUT="$(run_lines)"
check "quoted triage row parses card and count apart" \
  'printf %s "$OUT" | grep -qx "triage: capsule-corp/t_aaa failed 4x with the same error"'
check "a second quoted triage row prints too" \
  'printf %s "$OUT" | grep -qx "triage: sample-app/t_bbb failed 11x with the same error"'
check "no stray quote leaks into the digest" '! printf %s "$OUT" | grep -q "\""'

# At most three triage lines, however many cards are over the threshold.
write_fake_docker "rulings,0
decisions,0
triage,\"a/t_1,3\"
triage,\"a/t_2,3\"
triage,\"a/t_3,3\"
triage,\"a/t_4,3\""
OUT="$(run_lines)"
check "triage lines capped at three" \
  '[ "$(printf %s "$OUT" | grep -c "^triage:")" -eq 3 ]'

echo "---"; [ "$fails" -eq 0 ] && echo "test_report_power: all passed" || { echo "test_report_power: $fails failed"; exit 1; }
