#!/usr/bin/env bash
# bin/capsule-unblocked-hook.sh against a fixture Capsule home. Never touches the real
# the legacy home, ~/.local/share/capsule, or ~/.cache/capsule.
set -u
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HOOK="$REPO/bin/capsule-unblocked-hook.sh"
T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
fails=0; PASSED=""; check() { if eval "$2"; then PASSED="$PASSED|$1"; echo "PASS $1"; else echo "FAIL $1"; fails=$((fails+1)); fi; }

FIXTURE_HOME="$T/capsule-home"
FIXTURE_LEGACY="$T/legacy-home"     # deliberately empty: no board ever resolves here
CACHE="$T/cache/unblocked.json"
mkdir -p "$FIXTURE_HOME" "$FIXTURE_LEGACY"
cat >"$FIXTURE_HOME/config.json" <<EOF
{"unblocked": {"boards": ["capsule-corp"], "repos": [], "runs": []}}
EOF

export CAPSULE_HOME="$FIXTURE_HOME" LEGACY_KANBAN_HOME="$FIXTURE_LEGACY"
export CAPSULE_UNBLOCKED_CACHE="$CACHE"
export OBSIDIAN_VAULT_PATH="$T/no-vault" AGENT_OPS_NO_SEND=1 SKIP_DOCKER_IMAGE_TEST=1

export REPO FIXTURE_HOME
export PYTHONPATH="$REPO${PYTHONPATH:+:$PYTHONPATH}"

# row 470 fix round 1, I6: the hook only acts when the hook input's own `cwd` is under a
# configured repo - point it at $REPO itself (a real, allowed repo) for every "normal"
# call below, and exercise the "outside any repo" / "worker" cases separately.
export CAPSULE_UNBLOCKED_REPOS="$REPO"
IN_REPO_JSON='{"cwd": "'"$REPO"'"}'
IN_REPO_STOP_JSON='{"cwd": "'"$REPO"'", "stop_hook_active": false}'

# --- 1. empty board: session-start reports zero, stop stays silent -------------------
mkdir -p "$FIXTURE_HOME/boards/capsule-corp"
echo '{}' >"$FIXTURE_HOME/boards/capsule-corp/config.json"    # a configured board is the only kind read (D244)
python3 -c "
import os
from capsule import db
db.create_board(os.environ['FIXTURE_HOME'] + '/boards/capsule-corp/kanban.db')
"

out="$(echo "$IN_REPO_JSON" | "$HOOK" session-start 2>"$T/err1")"; rc=$?
check "session-start exits 0 on an empty board" '[ $rc -eq 0 ]'
check "session-start reports UNBLOCKED 0 UNOWNED 0" 'grep -q "UNBLOCKED 0 UNOWNED 0" <<<"$out"'

rm -f "$CACHE"
# D155: idle builder seats alone now block, so hold all 5 seats busy for this nothing-unowned case.
for b in 1 2 3 4 5; do "$REPO/bin/capsule-own" "$T/seatrepo" "build/sec1-$b" implementer >/dev/null; done
stop_out="$(echo "$IN_REPO_STOP_JSON" | "$HOOK" stop 2>"$T/err2")"; rc=$?
rm -f "$FIXTURE_HOME"/queue/*/owners.json
check "stop exits 0 on an empty board" '[ $rc -eq 0 ]'
check "stop prints nothing when nothing is unowned" '[ -z "$stop_out" ]'

# --- 2. one unowned ready card: session-start lists it, stop blocks ------------------
rm -f "$CACHE"
python3 -c "
import os
from capsule import db
p = os.environ['FIXTURE_HOME'] + '/boards/capsule-corp/kanban.db'
conn = db.connect(p)
db.new_task(conn, title='an unowned ready card', status='ready')
conn.close()
"
out2="$(echo "$IN_REPO_JSON" | "$HOOK" session-start 2>"$T/err3")"; rc=$?
check "session-start exits 0 with an unowned card" '[ $rc -eq 0 ]'
check "session-start lists the unowned card" 'grep -q "an unowned ready card" <<<"$out2"'
check "session-start UNOWNED count is 1" 'grep -q "UNOWNED 1" <<<"$out2"'

rm -f "$CACHE"
stop_out2="$(echo "$IN_REPO_STOP_JSON" | "$HOOK" stop 2>"$T/err4")"; rc=$?
check "stop exits 0 with an unowned card" '[ $rc -eq 0 ]'
check "stop prints a block decision" 'grep -q "\"decision\": *\"block\"" <<<"$stop_out2"'
check "stop names the unowned count in its reason" 'grep -q "1 unowned items" <<<"$stop_out2"'

# with no CAPSULE_UNBLOCKED_REPOS the default repo list does not cover an unlisted app
FAKE_HOME="$T/fakehome"; mkdir -p "$FAKE_HOME/CodeBase/other-app"
other_json='{"cwd": "'"$FAKE_HOME"'/CodeBase/other-app"}'
rm -f "$CACHE"
other_out="$(echo "$other_json" | env -u CAPSULE_UNBLOCKED_REPOS HOME="$FAKE_HOME" "$HOOK" session-start 2>"$T/err9s")"; rc=$?
check "session-start exits 0 for a cwd under ~/CodeBase/other-app by default, even with an unowned card" '[ $rc -eq 0 ]'
check "session-start prints nothing for a cwd under ~/CodeBase/other-app by default, even with an unowned card" '[ -z "$other_out" ]'
rm -f "$CACHE"
rm -f "$FIXTURE_HOME/unblocked-stop.json"
other_stop="$(echo "$other_json" | env -u CAPSULE_UNBLOCKED_REPOS HOME="$FAKE_HOME" "$HOOK" stop 2>"$T/err10s")"; rc=$?
check "stop exits 0 for a cwd under ~/CodeBase/other-app by default, even with an unowned card" '[ $rc -eq 0 ]'
check "stop prints nothing for a cwd under ~/CodeBase/other-app by default, even with an unowned card" '[ -z "$other_stop" ]'

# public release: the default repo list comes from host.json `repos`, never a baked-in private name
mkdir -p "$FAKE_HOME/CodeBase/capsule-corp" "$FAKE_HOME/CodeBase/sample-app"
printf '{"repos": {"capsule-corp": "%s/CodeBase/capsule-corp"}}\n' "$FAKE_HOME" >"$FIXTURE_HOME/host.json"
rm -f "$FIXTURE_HOME/unblocked-stop.json"
sample_stop="$(echo '{"cwd": "'"$FAKE_HOME"'/CodeBase/sample-app", "stop_hook_active": false}' | env -u CAPSULE_UNBLOCKED_REPOS HOME="$FAKE_HOME" "$HOOK" stop 2>"$T/err10p")"; rc=$?
check "stop prints nothing for a cwd that host.json repos does not list" '[ $rc -eq 0 ] && [ -z "$sample_stop" ]'
cc_stop="$(echo '{"cwd": "'"$FAKE_HOME"'/CodeBase/capsule-corp", "stop_hook_active": false}' | env -u CAPSULE_UNBLOCKED_REPOS HOME="$FAKE_HOME" "$HOOK" stop 2>"$T/err10c")"; rc=$?
check "stop blocks for a cwd under a host.json repos path" 'grep -q "\"decision\": *\"block\"" <<<"$cc_stop"'
rm -f "$FIXTURE_HOME/host.json"
rm -f "$FIXTURE_HOME/unblocked-stop.json"
# no host.json, no vault: the built-in default is this checkout only
mkdir -p "$T/sibling"
nohost() { rm -f "$FIXTURE_HOME/unblocked-stop.json"; echo '{"cwd": "'"$1"'", "stop_hook_active": false}' | env -u CAPSULE_UNBLOCKED_REPOS -u OBSIDIAN_VAULT_PATH "$HOOK" stop 2>/dev/null; }
check "no host.json: stop blocks under this checkout" 'grep -q "\"decision\": *\"block\"" <<<"$(nohost "$REPO")"'
check "no host.json: stop is silent under a sibling directory" '[ -z "$(nohost "$T/sibling")" ]'
rm -f "$FIXTURE_HOME/unblocked-stop.json"

# --- 3. the 60s cache is actually used on session-start, but the stop path never
#        touches it at all (row 470 fix round 1, C1) ----------------------------------
SPY_LOG="$T/spy.log"; SPY="$T/bin/python3-spy"; mkdir -p "$T/bin"
cat >"$SPY" <<EOF
#!/usr/bin/env bash
printf '%s\n' "\$*" >> "$SPY_LOG"
exec python3 "\$@"
EOF
chmod +x "$SPY"
rm -f "$CACHE" "$SPY_LOG"
echo "$IN_REPO_JSON" | CAPSULE_UNBLOCKED_PYTHON="$SPY" "$HOOK" session-start >/dev/null 2>/dev/null
echo "$IN_REPO_JSON" | CAPSULE_UNBLOCKED_PYTHON="$SPY" "$HOOK" session-start >/dev/null 2>/dev/null
detector_calls="$(grep -c -- '-m capsule unblocked' "$SPY_LOG" || true)"
format_calls="$(grep -c -- '"items"' "$SPY_LOG" || true)"
check "two session-start calls inside the cache TTL run the detector itself only once" '[ "$detector_calls" -eq 1 ]'
check "the cheap formatting step still runs each session-start call" '[ "$format_calls" -eq 2 ]'

rm -f "$CACHE" "$SPY_LOG"
echo "$IN_REPO_STOP_JSON" | CAPSULE_UNBLOCKED_PYTHON="$SPY" "$HOOK" stop >/dev/null 2>/dev/null
echo "$IN_REPO_STOP_JSON" | CAPSULE_UNBLOCKED_PYTHON="$SPY" "$HOOK" stop >/dev/null 2>/dev/null
stop_detector_calls="$(grep -c -- '-m capsule unblocked' "$SPY_LOG" || true)"
check "two stop calls each run the detector live (C1: no cache on the stop path)" '[ "$stop_detector_calls" -eq 2 ]'
check "the stop path never wrote the session-start cache file" '[ ! -f "$CACHE" ]'

# --- 4. a broken detector never fails the session ------------------------------------
FAKE_PY="$T/bin/python3-broken"
cat >"$FAKE_PY" <<'EOF'
#!/usr/bin/env bash
exit 7
EOF
chmod +x "$FAKE_PY"
rm -f "$CACHE"
out4="$(echo "$IN_REPO_JSON" | CAPSULE_UNBLOCKED_PYTHON="$FAKE_PY" "$HOOK" session-start 2>"$T/err5")"; rc=$?
check "session-start exits 0 even when the detector cannot run at all" '[ $rc -eq 0 ]'
stop_out4="$(echo "$IN_REPO_STOP_JSON" | CAPSULE_UNBLOCKED_PYTHON="$FAKE_PY" "$HOOK" stop 2>"$T/err6")"; rc=$?
check "stop exits 0 even when the detector cannot run at all" '[ $rc -eq 0 ]'
check "stop prints nothing (never blocks) when the detector cannot run" '[ -z "$stop_out4" ]'

# --- 5. bad usage is a warning, not a failure ----------------------------------------
echo '{}' | "$HOOK" bogus-mode >/dev/null 2>"$T/err7"; rc=$?
check "an unknown mode still exits 0" '[ $rc -eq 0 ]'
check "an unknown mode warns on stderr" 'grep -qi usage "$T/err7"'

# --- 6. row 470 fix round 1, C1: stop_hook_active silences a repeat stop call --------
rm -f "$CACHE" "$SPY_LOG"
active_json='{"cwd": "'"$REPO"'", "stop_hook_active": true}'
active_out="$(echo "$active_json" | CAPSULE_UNBLOCKED_PYTHON="$SPY" "$HOOK" stop 2>"$T/err8")"; rc=$?
check "stop exits 0 when stop_hook_active is true" '[ $rc -eq 0 ]'
check "stop prints nothing when stop_hook_active is true" '[ -z "$active_out" ]'
check "stop_hook_active never even runs the detector" '! grep -q -- "-m capsule unblocked" "$SPY_LOG"'

# --- 7. row 470 fix round 1, I6: a cwd outside every configured repo is a no-op ------
outside_json='{"cwd": "'"$T"'"}'
out_outside="$(echo "$outside_json" | "$HOOK" session-start 2>"$T/err9")"; rc=$?
check "session-start exits 0 for a cwd outside every configured repo" '[ $rc -eq 0 ]'
check "session-start prints nothing for a cwd outside every configured repo" '[ -z "$out_outside" ]'
stop_outside="$(echo "$outside_json" | "$HOOK" stop 2>"$T/err10")"; rc=$?
check "stop exits 0 for a cwd outside every configured repo" '[ $rc -eq 0 ]'
check "stop prints nothing for a cwd outside every configured repo" '[ -z "$stop_outside" ]'

no_cwd_out="$(echo '{}' | "$HOOK" session-start 2>"$T/err11")"; rc=$?
check "session-start exits 0 with no cwd in the hook input at all" '[ $rc -eq 0 ]'
check "session-start prints nothing with no cwd in the hook input at all" '[ -z "$no_cwd_out" ]'

# --- 8. row 470 fix round 1, I6: CAPSULE_WORKER=1 is a no-op regardless of cwd -------
worker_out="$(echo "$IN_REPO_JSON" | CAPSULE_WORKER=1 "$HOOK" session-start 2>"$T/err12")"; rc=$?
check "session-start exits 0 under CAPSULE_WORKER=1" '[ $rc -eq 0 ]'
check "session-start prints nothing under CAPSULE_WORKER=1" '[ -z "$worker_out" ]'
worker_stop_out="$(echo "$IN_REPO_STOP_JSON" | CAPSULE_WORKER=1 "$HOOK" stop 2>"$T/err13")"; rc=$?
check "stop exits 0 under CAPSULE_WORKER=1" '[ $rc -eq 0 ]'
check "stop prints nothing under CAPSULE_WORKER=1" '[ -z "$worker_stop_out" ]'

# --- 9. row 558: stop blocks only when the unowned set changed (wiring only - the full
# unchanged/changed/re-nag/corrupt-state/I1/I2/M4 logic is the unit-tested oracle in
# capsule/tests/test_unblocked_stop.py; this just proves the hook wires it end to end
# against a real board). The board still carries the one unowned ready card from section 2.
STATE="$FIXTURE_HOME/unblocked-stop.json"; rm -rf "$STATE"
first_stop="$(echo "$IN_REPO_STOP_JSON" | "$HOOK" stop 2>"$T/err14")"; rc=$?
check "row 558: first stop with no prior state still blocks" \
    '[ $rc -eq 0 ] && grep -q "\"decision\": *\"block\"" <<<"$first_stop"'
check "row 558: the state file holds a pairs/blocked_at record" \
    'grep -q "\"pairs\"" "$STATE" && grep -q "\"blocked_at\"" "$STATE"'
state_after_first="$(cat "$STATE")"

unchanged_stop="$(echo "$IN_REPO_STOP_JSON" | "$HOOK" stop 2>"$T/err15")"; rc=$?
check "row 558: an immediate repeat with the same set does not block, state untouched" \
    '[ $rc -eq 0 ] && [ -z "$unchanged_stop" ] && [ "$(cat "$STATE")" = "$state_after_first" ]'

python3 -c "import os; from capsule import db; c = db.connect(os.environ['FIXTURE_HOME'] + '/boards/capsule-corp/kanban.db'); db.new_task(c, title='a second unowned ready card', status='ready'); c.close()"
changed_stop="$(echo "$IN_REPO_STOP_JSON" | "$HOOK" stop 2>"$T/err16")"; rc=$?
check "row 558: a new unowned item blocks again" \
    '[ $rc -eq 0 ] && grep -q "\"decision\": *\"block\"" <<<"$changed_stop"'

# I2 (fail-first: reproduces the reviewer's finding against fix round 1, which stayed
# silent here): state path is a directory, not a file - read+write both fail.
rm -rf "$STATE"; mkdir -p "$STATE"
i2_stop="$(echo "$IN_REPO_STOP_JSON" | "$HOOK" stop 2>"$T/err17")"; rc=$?
check "row 558 I2: an unwritable state path still blocks (fail loud, not silent)" \
    '[ $rc -eq 0 ] && grep -q "\"decision\": *\"block\"" <<<"$i2_stop"'
# R2-M1 (fail-first): the failed rename above (tmp -> a directory) must not leave its
# `*.tmp` behind in CAPSULE_HOME.
leftover_tmp="$(find "$FIXTURE_HOME" -maxdepth 1 -name '*.tmp')"
check "R2-M1: the failed rename leaves no *.tmp file in CAPSULE_HOME" '[ -z "$leftover_tmp" ]'
rm -rf "$STATE"

# --- 10. R2-M2: non-JSON detector output warns audibly and still exits 0 -------------
# Only the detector invocation itself (`$PY -m capsule unblocked --json`) is stubbed to
# garbage output; every other $PY use (cwd parsing, the row 558 gate's own heredoc) still
# runs real python3, so this exercises exactly the "detector output is not JSON" branch.
NONJSON_PY="$T/bin/python3-nonjson"
cat >"$NONJSON_PY" <<'EOF'
#!/usr/bin/env bash
[ "${1:-}" = "-P" ] && shift
if [ "${1:-}" = "-m" ] && [ "${2:-}" = "capsule" ]; then
    echo "not json at all"
    exit 0
fi
exec python3 "$@"
EOF
chmod +x "$NONJSON_PY"
# D155: non-JSON output is a failed detector (pair gate skipped, seat gate still runs), so hold
# all 5 builder seats busy to keep stdout empty here.
for b in 1 2 3 4 5; do "$REPO/bin/capsule-own" "$T/seatrepo" "build/sec10-$b" implementer >/dev/null; done
nonjson_stop="$(echo "$IN_REPO_STOP_JSON" | CAPSULE_UNBLOCKED_PYTHON="$NONJSON_PY" "$HOOK" stop 2>"$T/err18")"; rc=$?
rm -f "$FIXTURE_HOME"/queue/*/owners.json
check "R2-M2: non-JSON detector output still exits 0" '[ $rc -eq 0 ]'
check "R2-M2: non-JSON detector output prints nothing to stdout" '[ -z "$nonjson_stop" ]'
check "R2-M2: non-JSON detector output warns audibly on stderr" \
    'grep -q "detector output is not JSON; skipping the unowned items check" "$T/err18"'

# --- 11. owner 2026-10-18: stop fires only when a seat is free ----------------------
# Seats = fresh `bin/capsule-own` implementer claims under $CAPSULE_HOME/queue/*/owners.json
# (fixture home only). The board still holds unowned ready cards from sections 2 and 9.
OWN="$REPO/bin/capsule-own"; QUEUE="$FIXTURE_HOME/queue"
# D155: the default cap is 5; these cases pin 3 first (later flags win) so the 2/3, 3/3 arithmetic stays.
seat_stop() { rm -f "$STATE"; echo "$IN_REPO_STOP_JSON" | "$HOOK" stop --heavy-cap 3 "$@" 2>"$T/err_seat"; }
blocks() { grep -q '"decision": *"block"' <<<"$1"; }
for b in a b; do "$OWN" "$T/fakerepo" "build/seat-$b" implementer >/dev/null; done
seat_out="$(seat_stop)"
check "seats: 2 of 3 heavy seats busy still blocks (a seat is free)" 'blocks "$seat_out"'
"$OWN" "$T/fakerepo" capsule/keeper merge-keeper >/dev/null
seat_out="$(seat_stop)"
check "seats: 2 implementer claims plus the merge-keeper still block (a seat is free)" 'blocks "$seat_out"'
"$OWN" "$T/fakerepo" build/seat-c implementer >/dev/null
seat_out="$(seat_stop)"
check "seats: 3 of 3 heavy seats busy stays silent" '[ -z "$seat_out" ]'
seat_out="$(seat_stop --heavy-cap 4)"
check "seats: --heavy-cap 4 makes 3 busy seats leave one free, blocks" 'blocks "$seat_out"'
"$OWN" "$T/fakerepo" build/seat-c --release >/dev/null
"$OWN" "$T/otherrepo" build/seat-d implementer >/dev/null
seat_out="$(seat_stop)"
check "seats: claims in a second repo count toward the same 3 seats" '[ -z "$seat_out" ]'
python3 - "$QUEUE/otherrepo/owners.json" <<'PY'
import json, sys, time
p = sys.argv[1]; d = json.load(open(p))
d["build/seat-d"]["heartbeat"] = time.time() - 4 * 3600
json.dump(d, open(p, "w"))
PY
seat_out="$(seat_stop)"
check "seats: a 4h-old claim is stale (default 3h) and frees its seat, blocks" 'blocks "$seat_out"'
seat_out="$(seat_stop --max-age-hours 5)"
check "seats: --max-age-hours 5 counts the 4h-old claim as busy, silent" '[ -z "$seat_out" ]'
seat_out="$(seat_stop --heavy-cap notanumber)"
check "seats: a bad argument warns and falls back to the default cap" \
    'grep -q "heavy-cap" "$T/err_seat" && blocks "$seat_out"'
"$OWN" "$T/otherrepo" t_abc123 implementer >/dev/null
"$OWN" "$T/otherrepo" build/seat-d implementer >/dev/null
seat_out="$(seat_stop)"
check "seats: a run claiming its branch and its card id is one seat (3 runs, 1 free)" '[ -z "$seat_out" ]'
"$OWN" "$T/otherrepo" build/seat-d --release >/dev/null
seat_out="$(seat_stop)"
check "seats: a card-id claim is an alias of a run, not a seat of its own, blocks" 'blocks "$seat_out"'
seat_out="$(seat_stop --heavy-cap 0)"
check "seats: --heavy-cap 0 is rejected (warn, default 3), never a silent off switch" \
    'grep -q "heavy-cap" "$T/err_seat" && blocks "$seat_out"'
rm -f "$QUEUE"/*/owners.json
python3 - "$QUEUE/fakerepo/owners.json" <<'PY'
import json, sys, time
json.dump({"capsule/future": {"owner": "implementer", "heartbeat": time.time() + 30 * 86400}}, open(sys.argv[1], "w"))
PY
seat_out="$(seat_stop --heavy-cap 1)"
check "seats: a future heartbeat is invalid and holds no seat, blocks" 'blocks "$seat_out"'
rm -f "$QUEUE"/*/owners.json
seat_out="$(seat_stop)"
check "seats: no claims at all blocks as before" 'blocks "$seat_out"'

# --- 12. D155: idle builder seats (build/* implementer claims) block even with nothing unowned ---
# A second, empty fixture home: no board card, so only the seat signal can block.
H2="$T/home2"; mkdir -p "$H2/boards/capsule-corp"
cp "$FIXTURE_HOME/config.json" "$H2/config.json"
echo '{}' >"$H2/boards/capsule-corp/config.json"
CAPSULE_HOME="$H2" python3 -c "
import os
from capsule import db
db.create_board(os.environ['CAPSULE_HOME'] + '/boards/capsule-corp/kanban.db')
"
S2="$H2/unblocked-seats-stop.json"; Q2="$H2/queue"
cap_stop() { echo "$IN_REPO_STOP_JSON" | CAPSULE_HOME="$H2" "$HOOK" stop "$@" 2>"$T/err_cap"; }
own2() { CAPSULE_HOME="$H2" "$OWN" "$T/fakerepo" "$@" >/dev/null; }
for b in a b; do own2 "build/s-$b" implementer; done
d_out="$(cap_stop)"
check "D155: 2 of 5 builder seats busy and nothing unowned blocks with the BUILDER SEATS line" \
    'blocks "$d_out" && grep -q "BUILDER SEATS: 2/5 busy - start independent builds (D154) or state why not" <<<"$d_out" && ! grep -q "UNBLOCKED WORK" <<<"$d_out"'
check "D155: the block keeps the decision/reason JSON shape" \
    'python3 -c "import json,sys; d=json.loads(sys.argv[1]); sys.exit(0 if set(d)=={\"decision\",\"reason\"} and d[\"decision\"]==\"block\" else 1)" "$d_out"'
d_out="$(cap_stop)"
check "D155: an immediate repeat with the same idle seats is rate limited, silent" '[ -z "$d_out" ]'
python3 - "$S2" <<'PY'
import json, sys, time
d = json.load(open(sys.argv[1])); d["blocked_at"] = time.time() - 31 * 60
json.dump(d, open(sys.argv[1], "w"))
PY
d_out="$(cap_stop)"
check "D155: idle seats block again after 30 minutes unchanged" 'blocks "$d_out"'
for b in c d; do own2 "build/s-$b" implementer; done
own2 build/s-e implementer
d_out="$(cap_stop)"
check "D155: 5 of 5 builder seats busy is silent" '[ -z "$d_out" ]'
own2 build/s-e --release; own2 capsule/factory-run implementer
rm -f "$S2"    # the 5/5 stop kept the earlier state (rate limit); start clean to read the 4/5 text
d_out="$(cap_stop)"
check "D155: a non-build branch claim does not count as a builder seat (4/5 blocks)" \
    'blocks "$d_out" && grep -q "BUILDER SEATS: 4/5 busy" <<<"$d_out"'
rm -f "$S2"; d_out="$(echo "$IN_REPO_STOP_JSON" | CAPSULE_WORKER=1 CAPSULE_HOME="$H2" "$HOOK" stop 2>"$T/err_cap")"
check "D155: CAPSULE_WORKER=1 stays silent with idle builder seats" '[ -z "$d_out" ]'
d_out="$(echo "$outside_json" | CAPSULE_HOME="$H2" "$HOOK" stop 2>"$T/err_cap")"
check "D155: a cwd outside the configured repos stays silent with idle builder seats" '[ -z "$d_out" ]'
d_out="$(cap_stop --heavy-cap 4)"
check "D155: --heavy-cap 4 with 4 build seats busy is silent" '[ -z "$d_out" ]'
rm -f "$Q2"/*/owners.json "$S2"
d_out="$(cap_stop)"
check "D155: 0 of 5 builder seats busy blocks and says 0/5" 'grep -q "BUILDER SEATS: 0/5 busy" <<<"$d_out"'

# Host-exec fix: the hook's stdin Python runs isolated (-I), so a json.py planted in the
# session's own directory (the hook's cwd) never runs. The planted module logs a line, then fails.
PLANT="$T/plant"; mkdir -p "$PLANT"
printf 'open(%s, "a").write("ran\\n")\nraise ImportError("planted")\n' "'$T/plant-marker'" >"$PLANT/json.py"
(cd "$PLANT" && echo "$IN_REPO_JSON" | "$HOOK" session-start >/dev/null 2>&1)
(cd "$PLANT" && echo "$IN_REPO_STOP_JSON" | "$HOOK" stop >/dev/null 2>&1)
check "planted json.py: a json.py in the session directory never runs in the hook" \
    '[ ! -e "$T/plant-marker" ]'
check "planted json.py: the hook still parses its input and session-start reports" \
    '(cd "$PLANT" && echo "$IN_REPO_JSON" | "$HOOK" session-start 2>/dev/null) | grep -q "capsule unblocked:"'

# Z1 (owner 2026-10-18): contracts/intent/capsule_unblocked_hook.jsonl replayed here - each example names a check above that passed.
check "intent examples all name a passing check" \
    'python3 -c '"'"'import json,sys; rows=[json.loads(l) for l in open(sys.argv[1]) if l.strip()]; got=sys.argv[2].split("|"); sys.exit(0 if rows and all(r["expect"]["pass"] in got for r in rows) else 1)'"'"' "$REPO/contracts/intent/capsule_unblocked_hook.jsonl" "$PASSED"'
# Row 632 / cwd-import sweep: the two planted json.py examples (capsule_unblocked_hook_cwd.jsonl).
check "cwd intent examples all name a passing check" \
    'python3 -c '"'"'import json,sys; rows=[json.loads(l) for l in open(sys.argv[1]) if l.strip()]; got=sys.argv[2].split("|"); sys.exit(0 if rows and all(r["expect"]["pass"] in got for r in rows) else 1)'"'"' "$REPO/contracts/intent/capsule_unblocked_hook_cwd.jsonl" "$PASSED"'

echo "----"
if [ "$fails" -eq 0 ]; then echo "ALL PASS"; else echo "$fails FAILED"; fi
exit "$fails"
