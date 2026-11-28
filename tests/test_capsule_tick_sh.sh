#!/usr/bin/env bash
# factory-autonomy Task 11: bin/capsule-tick.sh over a fake launchctl that runs a stub tick holding the real board lock.
set -uo pipefail
# P03: these cases assert the author machine's labels; the shipped default is dev.capsule
export CAPSULE_LAUNCHD_PREFIX=dev.capsule
REPO="$(cd "$(dirname "$0")/.." && pwd)"
fail=0; PASSED=""; pass() { echo "PASS $1"; PASSED="$PASSED|$1"; }; bad() { echo "FAIL $1"; fail=1; }
T="$(mktemp -d)"; PGC=""   # PGC: throwaway Postgres container name, set before the container starts so an abort still removes it
trap 'pkill -f "$T/stub.py" 2>/dev/null; [ -n "$PGC" ] && docker rm -fv "$PGC" >/dev/null 2>&1; rm -rf "$T"' EXIT
export HOME="$T/user"; mkdir -p "$HOME"     # never the real home, whatever a case below calls
case "$HOME" in "$T"/*) pass home-is-sandboxed ;; *) bad "home-is-sandboxed: $HOME" ;; esac
export CAPSULE_HOME="$T/home" CAPSULE_LA_DIR="$T/la" CAPSULE_LAUNCHCTL="$T/launchctl"
mkdir -p "$CAPSULE_HOME/boards/cc" "$CAPSULE_LA_DIR" "$T/state"
echo '{"board": "cc"}' > "$CAPSULE_HOME/boards/cc/config.json"
touch "$CAPSULE_LA_DIR/dev.capsule.capsule-tick-cc.plist"
cat > "$T/stub.py" <<'EOF'
import signal, sys, threading
sys.path.insert(0, sys.argv[1])
from capsule import tick_guard
board, home = sys.argv[sys.argv.index("--board") + 1], sys.argv[sys.argv.index("--capsule-home") + 1]
lk = tick_guard.guard_start(home, board, replace=True)       # tick-<board>.lock, holding this pid
stop = threading.Event(); signal.signal(signal.SIGTERM, lambda *_: stop.set())
stop.wait(120); lk.close()
EOF
# fake launchctl: loaded state is a file; the job is the stub, whose argv carries "-m capsule tick --replace --board cc"
cat > "$T/launchctl" <<EOF
#!/usr/bin/env bash
S="$T/state"; echo "\$*" >> "\$S/calls"
run() { [ -f "\$S/norun" ] && return 0; nohup python3 "$T/stub.py" "$REPO" -u -m capsule tick --replace --board cc --capsule-home "$CAPSULE_HOME" >/dev/null 2>&1 & echo \$! > "\$S/pid"; }
term() { [ -f "\$S/pid" ] && kill -TERM "\$(cat "\$S/pid")" 2>/dev/null; }
case "\$1" in
  print) [ -f "\$S/loaded" ] ;;
  bootstrap) touch "\$S/loaded"; run ;;
  bootout) rm -f "\$S/loaded"; python3 -c 'import sqlite3,sys; print(sqlite3.connect("file:"+sys.argv[1]+"?mode=ro", uri=True).execute("select count(*) from task_runs where ended_at is null").fetchone()[0])' "$CAPSULE_HOME/boards/cc/kanban.db" >"\$S/runs_at_bootout" 2>/dev/null; term ;;
  kickstart) if [ "\$2" = "-k" ]; then term; sleep 1; fi; run ;;
esac
EOF
chmod +x "$T/launchctl"
S="$REPO/bin/capsule-tick.sh"
js() { python3 -c "import json,sys; d=json.loads(sys.argv[1]); sys.exit(0 if $2 else 1)" "$1"; }

# C08: the tick runs from the release clone $CAPSULE_HOME/harness/main; origin is a local bare repo here.
G() { git -c user.name=t -c user.email=t@t -c commit.gpgSign=false "$@"; }
"$S" status cc | python3 -c 'import json,sys; sys.exit(0 if json.load(sys.stdin)["main_sha"] is None else 1)' \
  && pass status-main-sha-null-without-checkout || bad status-main-sha-null-without-checkout
# C08 (row 600 Q4) + review item 3 (Z6): start/restart fail closed and loud while the release checkout is missing, naming the clone command.
G init -q --bare "$T/origin.git"; G clone -q "$T/origin.git" "$T/dev" 2>/dev/null
echo 1 > "$T/dev/f"; G -C "$T/dev" add f; G -C "$T/dev" commit -qm one; G -C "$T/dev" branch -M main; G -C "$T/dev" push -q origin main
: > "$T/state/calls"; out="$("$S" start cc 2>&1)"; rc=$?
[ $rc -ne 0 ] && grep -q "git clone .* $CAPSULE_HOME/harness/main" <<<"$out" && ! grep -q bootstrap "$T/state/calls" \
  && pass start-refuses-without-release-checkout || bad "start-refuses-without-release-checkout: rc=$rc $out"
: > "$T/state/calls"; out="$("$S" restart cc 2>&1)"; rc=$?
[ $rc -ne 0 ] && grep -q "git clone" <<<"$out" && ! grep -q "kickstart\|bootstrap" "$T/state/calls" \
  && pass restart-refuses-without-release-checkout || bad "restart-refuses-without-release-checkout: rc=$rc $out"
G clone -q "$T/origin.git" "$CAPSULE_HOME/harness/main"; R="$CAPSULE_HOME/harness/main"
sha1="$(G -C "$R" rev-parse HEAD)"
js "$("$S" status cc)" "d['main_sha'] == '$sha1'" && pass status-main-sha || bad status-main-sha
# P08b: the harness is looked up under $CAPSULE_HOME, never under $HOME/.local/share/capsule (an empty HOME here)
mkdir -p "$T/emptyhome"
HOME="$T/emptyhome" "$S" start cc >/dev/null && { pass start; pass start-finds-harness-under-capsule-home; } || bad start
st="$("$S" status cc)"; js "$st" 'd["loaded"] and d["alive"] and d["lock"]' && pass status-live || bad "status-live: $st"
p1="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["pid"])' "$st")"
"$S" restart cc --wait 30 >/dev/null && pass restart || bad restart
st="$("$S" status cc)"; js "$st" "d['alive'] and d['lock'] and d['pid'] != $p1" && pass restart-new-pid || bad "restart-new-pid: $st"
grep -q "^kickstart -k gui/$(id -u)/dev.capsule.capsule-tick-cc$" "$T/state/calls" && pass restart-is-kickstart-k || bad restart-is-kickstart-k
"$S" stop cc --wait 30 >/dev/null && pass stop || bad stop
st="$("$S" status cc)"; js "$st" 'not d["loaded"] and not d["alive"] and not d["lock"]' && pass status-dead || bad "status-dead: $st"
"$S" start nosuchboard >/dev/null 2>&1 && bad start-uninstalled || pass start-uninstalled
# review fix 8: the "not installed" line names both ways to install the job
for verb in start restart; do
  out="$("$S" "$verb" nosuchboard 2>&1)"
  grep -q "run ./install.sh if you have not, then ~/.local/bin/capsule board tick nosuchboard" <<<"$out" \
    && pass "$verb-uninstalled-names-both-installs" || bad "$verb-uninstalled-names-both-installs: $out"
done
"$S" frobnicate cc >/dev/null 2>&1; [ $? -eq 2 ] && pass usage || bad usage

# --quiet S (row 526 item 4): stop/restart first wait up to S s for no open run (task_runs.ended_at null) in the
# board's kanban.db, then go ahead either way - a quiet wait is a courtesy, never a lock.
DB="$CAPSULE_HOME/boards/cc/kanban.db"
sql() { python3 -c 'import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); c.execute(sys.argv[2]); c.commit()' "$DB" "$1"; }
sql "create table task_runs (id integer primary key, ended_at text)"
S_STATE="$T/state"
timed() { local t0=$SECONDS; "$@" >/dev/null 2>&1; rc=$?; el=$((SECONDS - t0)); }
sql "insert into task_runs (ended_at) values (null)"
"$S" start cc >/dev/null; timed "$S" stop cc --quiet 3 --wait 30
[ $rc -eq 0 ] && [ $el -ge 3 ] && [ $el -lt 12 ] && pass quiet-waits-then-goes-ahead || bad "quiet-waits-then-goes-ahead: rc=$rc el=$el"
[ "$(cat "$S_STATE/runs_at_bootout")" = 1 ] && pass quiet-timeout-leaves-the-run-open || bad "quiet-timeout-leaves-the-run-open"
sql "update task_runs set ended_at = 'now'"
"$S" start cc >/dev/null; timed "$S" stop cc --quiet 30 --wait 30
# under 10 s, far below the 30 s quiet wait: a loaded host spends ~3 s just starting the helpers
[ $rc -eq 0 ] && [ $el -lt 10 ] && pass quiet-returns-at-once-when-idle || bad "quiet-returns-at-once-when-idle: rc=$rc el=$el"
sql "update task_runs set ended_at = null"
"$S" start cc >/dev/null
( sleep 2; sql "update task_runs set ended_at = 'now'" ) &
closer=$!
timed "$S" stop cc --quiet 30 --wait 30; wait "$closer"
[ $rc -eq 0 ] && [ $el -ge 2 ] && [ $el -lt 10 ] && [ "$(cat "$S_STATE/runs_at_bootout")" = 0 ] \
  && pass quiet-stops-once-the-last-run-ends || bad "quiet-stops-once-the-last-run-ends: rc=$rc el=$el runs=$(cat "$S_STATE/runs_at_bootout")"
sql "update task_runs set ended_at = null"
"$S" start cc >/dev/null; timed "$S" restart cc --quiet 2 --wait 30
[ $rc -eq 0 ] && [ $el -ge 2 ] && pass quiet-applies-to-restart || bad "quiet-applies-to-restart: rc=$rc el=$el"
"$S" stop cc --wait 30 >/dev/null
sql "update task_runs set ended_at = 'now'"
# G2b: a live detached lander worker (guard_state lander.checking, pid) holds --quiet like an open run; a dead pid does not.
sql "create table guard_state (key text primary key, value text)"
sleep 60 & lander=$!
sql "insert into guard_state values ('lander.checking', '{\"pid\": $lander}')"
"$S" start cc >/dev/null; timed "$S" stop cc --quiet 3 --wait 30
[ $rc -eq 0 ] && [ $el -ge 3 ] && [ $el -lt 12 ] && pass quiet-waits-for-a-live-lander-worker || bad "quiet-waits-for-a-live-lander-worker: rc=$rc el=$el"
kill "$lander"; wait "$lander" 2>/dev/null
"$S" start cc >/dev/null; timed "$S" stop cc --quiet 30 --wait 30
[ $rc -eq 0 ] && [ $el -lt 10 ] && pass quiet-ignores-a-dead-lander-worker || bad "quiet-ignores-a-dead-lander-worker: rc=$rc el=$el"

# start waits --wait + 15 s for the process (default 900 + 15 = 915 s, the tick run-one gap: a launchd tick
# that finds the lock held SIGTERMs the holder and exits 3 if it has not let go in 10 s), not the 15 s the plan
# first said (row 526 item 3, plan amended). A launchctl that never starts a tick must make start fail on that clock.
# Proved by a fake clock, not by wall time (a load of 24 stretched this 16 s wait past 25 s).
# A `sleep` first on PATH records its argument and returns at once, so the wait is the sum of
# what start asked to sleep. Giving up early or never is caught by that sum. The stub ends a
# runaway after 2000 sleeps, and perl's alarm is the 120 s hang guard (rc 142); a busy machine
# passes neither early.
touch "$T/state/norun"
mkdir -p "$T/fakeclock"; : > "$T/state/slept"
cat > "$T/fakeclock/sleep" <<EOF
#!/usr/bin/env bash
echo "\$1" >> "$T/state/slept"
[ "\$(wc -l < "$T/state/slept")" -gt 2000 ] && kill -TERM "\$PPID"
exit 0
EOF
chmod +x "$T/fakeclock/sleep"
PATH="$T/fakeclock:$PATH" perl -e 'alarm shift; exec @ARGV' 120 "$S" start cc --wait 1 \
  >/dev/null 2>&1
rc=$?
asked="$(awk '{s += $1} END {printf "%.1f", s}' "$T/state/slept")"
[ $rc -eq 1 ] && [ "$asked" = 16.0 ] && pass start-waits-wait-plus-15 \
  || bad "start-waits-wait-plus-15: rc=$rc asked=${asked}s"
rm -f "$T/state/norun"; "$S" stop cc --wait 30 >/dev/null
PLAN="$REPO/docs/superpowers/plans/2026-09-27-factory-autonomy.md"
if [ -f "$PLAN" ]; then  # the plan is private and not in the public release
grep -q 'waits `--wait` + 15 s' "$PLAN" && ! grep -q 'else `kickstart`; waits 15 s for alive' "$PLAN" \
  && pass plan-states-the-start-wait || bad plan-states-the-start-wait
else echo "skip plan-states-the-start-wait (private plan)"
fi

# C08 (row 600 Q4): restart first fast-forwards the release checkout $CAPSULE_HOME/harness/main from origin
# (a local bare repo here), refuses on divergence or a dirty tree, prints the new sha; status prints main_sha.
echo 2 > "$T/dev/f"; G -C "$T/dev" commit -qam two; G -C "$T/dev" push -q origin main
sha2="$(G -C "$T/dev" rev-parse HEAD)"
"$S" start cc >/dev/null; out="$("$S" restart cc --wait 30 2>&1)"; rc=$?
[ $rc -eq 0 ] && [ "$(G -C "$R" rev-parse HEAD)" = "$sha2" ] && grep -q "$sha2" <<<"$out" \
  && pass restart-fast-forwards-release-checkout || bad "restart-fast-forwards-release-checkout: rc=$rc $out"
echo dirty >> "$R/f"; echo 3 > "$T/dev/f"; G -C "$T/dev" commit -qam three; G -C "$T/dev" push -q origin main
sha3="$(G -C "$T/dev" rev-parse HEAD)"; : > "$T/state/calls"
out="$("$S" restart cc --wait 30 2>&1)"; rc=$?
[ $rc -ne 0 ] && [ "$(G -C "$R" rev-parse HEAD)" = "$sha2" ] && ! grep -q kickstart "$T/state/calls" \
  && pass restart-refuses-dirty-release-checkout || bad "restart-refuses-dirty-release-checkout: rc=$rc"
G -C "$R" checkout -q -- f
echo local > "$R/g"; G -C "$R" add g; G -C "$R" commit -qm local   # a commit origin does not have: divergence
: > "$T/state/calls"; out="$("$S" restart cc --wait 30 2>&1)"; rc=$?
[ $rc -ne 0 ] && [ "$(G -C "$R" rev-parse origin/main)" = "$sha3" ] && ! grep -q kickstart "$T/state/calls" \
  && grep -q "diverged" <<<"$out" && ! grep -q "fetch" <<<"$out" \
  && pass restart-refuses-diverged-release-checkout || bad "restart-refuses-diverged-release-checkout: rc=$rc $out"
# review item 7: an unreachable origin is a fetch failure, named as such (offline: nothing moves, the old tick keeps running)
G -C "$R" remote set-url origin "$T/no-such-origin.git"; : > "$T/state/calls"
out="$("$S" restart cc --wait 30 2>&1)"; rc=$?
[ $rc -ne 0 ] && ! grep -q kickstart "$T/state/calls" && grep -q "fetch from origin failed" <<<"$out" && ! grep -q "diverged" <<<"$out" \
  && pass restart-names-a-failed-fetch || bad "restart-names-a-failed-fetch: rc=$rc $out"
G -C "$R" remote set-url origin "$T/origin.git"
# review item 4: restart fast-forwards AFTER the --quiet wait, so the files never move under a tick still in a pass.
sql "update task_runs set ended_at = null"
G -C "$R" reset -q --hard "$sha3"; echo 4 > "$T/dev/f"; G -C "$T/dev" commit -qam four; G -C "$T/dev" push -q origin main
sha5="$(G -C "$T/dev" rev-parse HEAD)"; "$S" start cc >/dev/null
( sleep 2; G -C "$R" rev-parse HEAD > "$T/state/head_at_quiet_end"; sql "update task_runs set ended_at = 'now'" ) &
closer=$!; "$S" restart cc --quiet 30 --wait 30 >/dev/null 2>&1; rc=$?; wait "$closer"
[ $rc -eq 0 ] && [ "$(cat "$T/state/head_at_quiet_end")" = "$sha3" ] && [ "$(G -C "$R" rev-parse HEAD)" = "$sha5" ] \
  && pass restart-fast-forwards-after-the-quiet-wait || bad "restart-fast-forwards-after-the-quiet-wait: rc=$rc"
"$S" stop cc --wait 30 >/dev/null

# C07c (row 600): the boundary step (capsule/boundary.py) restarts a board that has a pending landing only in a run-one
# gap, through this very script and the fake launchctl; tests/boundary_oracle.py runs one real step pass.
echo 5 > "$T/dev/f"; G -C "$T/dev" commit -qam five; G -C "$T/dev" push -q origin main
sha6="$(G -C "$T/dev" rev-parse HEAD)"; "$S" start cc >/dev/null
p6="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["pid"])' "$("$S" status cc)")"
BO() { python3 "$REPO/tests/boundary_oracle.py" "$REPO" "$CAPSULE_HOME" "$T/flag.db" "$@"; }
sql "insert into task_runs (ended_at) values (null)"; : > "$T/state/calls"
out="$(BO live "$sha6")"
[ "$out" = waiting ] && ! grep -q kickstart "$T/state/calls" && [ "$(G -C "$R" rev-parse HEAD)" = "$sha5" ] \
  && pass boundary-waits-for-a-live-run || bad "boundary-waits-for-a-live-run: $out"
sql "update task_runs set ended_at = 'now'"
out="$(BO gap "$sha6")"; up=no
for _ in $(seq 150); do
  st="$("$S" status cc)"; js "$st" "d['alive'] and d['lock'] and d['pid'] != $p6" && { up=yes; break; }; sleep 0.2
done
[ "$out" = fired ] && [ "$up" = yes ] && [ "$(G -C "$R" rev-parse HEAD)" = "$sha6" ] && grep -q "^kickstart -k " "$T/state/calls" \
  && pass boundary-restarts-in-a-gap-with-a-new-pid || bad "boundary-restarts-in-a-gap-with-a-new-pid: $out up=$up"
[ "$(BO serial "$sha6")" = "fired:cc" ] && pass boundary-restarts-boards-serially || bad boundary-restarts-boards-serially
"$S" stop cc --wait 30 >/dev/null

# every per-board plist template that ships here: lint, KeepAlive on failure only, a long exit timeout, the board flag only on capsule-corp
for f in "$REPO"/launchd/templates/__PREFIX__.capsule-tick-*.plist; do
  b="${f##*/__PREFIX__.capsule-tick-}"; b="${b%.plist}"
  python3 -c 'import plistlib,sys; plistlib.load(open(sys.argv[1],"rb"))' "$f" 2>/dev/null \
    && pass "plist-parses-$b" || bad "plist-parses-$b"          # no plutil in the Linux worker image
  python3 - "$f" "$b" <<'EOF' && pass "plist-keys-$b" || bad "plist-keys-$b"
import plistlib, sys
d = plistlib.load(open(sys.argv[1], "rb"))
args = d["ProgramArguments"]
ok = (d["KeepAlive"] == {"SuccessfulExit": False} and d["ExitTimeOut"] == 900 and d["RunAtLoad"] is True
      and args[:7] == ["/usr/bin/env", "python3", "-u", "-m", "capsule", "tick", "--replace"]
      and (("--board" in args) == (sys.argv[2] == "capsule-corp")) and d["WorkingDirectory"] == "__CAPSULE_HOME__/harness/main"
      and d["EnvironmentVariables"].get("EVENTS_SPOOL") == "__CONFIG__/maintenance/state/events.jsonl")
sys.exit(0 if ok else 1)
EOF
done

# the script's own python is the one the installed plist runs (a stock Mac's python3 is 3.9); a /usr/bin/env plist keeps python3.
# The script reads the plist with /usr/libexec/PlistBuddy, which only macOS has; the gate runs this file on Linux, where it keeps python3.
SKIPPED=""
if [ "$(uname -s)" = Darwin ]; then  # macos-only: PlistBuddy
  printf '#!/bin/sh\necho used >> "%s/py-used"\nexec python3 "$@"\n' "$T" > "$T/python3.11"; chmod +x "$T/python3.11"
  PL="$CAPSULE_LA_DIR/dev.capsule.capsule-tick-cc.plist"
  python3 -c 'import plistlib,sys; plistlib.dump({"ProgramArguments": [sys.argv[2], "-u"]}, open(sys.argv[1], "wb"))' "$PL" "$T/python3.11"
  "$S" status cc >/dev/null; [ -s "$T/py-used" ] && pass script-uses-the-plists-interpreter || bad script-uses-the-plists-interpreter
  rm -f "$T/py-used"
  python3 -c 'import plistlib,sys; plistlib.dump({"ProgramArguments": ["/usr/bin/env", "python3"]}, open(sys.argv[1], "wb"))' "$PL"
  "$S" status cc >/dev/null; [ ! -e "$T/py-used" ] && pass script-keeps-python3-for-an-env-plist || bad script-keeps-python3-for-an-env-plist
  # a plist whose python is gone: start says so in one line naming the path and the fix, at once, with no launchctl call
  python3 -c 'import plistlib,sys; plistlib.dump({"ProgramArguments": [sys.argv[2], "-u"]}, open(sys.argv[1], "wb"))' "$PL" "$T/gone/python3.11"
  : > "$T/state/calls"; t0=$SECONDS
  out="$(perl -e 'alarm shift; exec @ARGV' 30 "$S" start cc --wait 20 2>&1)"; rc=$?
  [ $rc -ne 0 ] && [ "$(wc -l <<<"$out")" -eq 1 ] && grep -q "$T/gone/python3.11" <<<"$out" && grep -q "board tick cc" <<<"$out" \
    && [ $((SECONDS - t0)) -lt 10 ] && ! grep -q "bootstrap\|kickstart" "$T/state/calls" \
    && pass start-names-a-vanished-interpreter || bad "start-names-a-vanished-interpreter: rc=$rc $out"
  # a bare interpreter name is a PATH lookup: one that resolves is used, one that does not is named as gone
  mkdir -p "$T/pathbin"; cp "$T/python3.11" "$T/pathbin/python3.77"; rm -f "$T/py-used"
  python3 -c 'import plistlib,sys; plistlib.dump({"ProgramArguments": ["python3.77", "-u"]}, open(sys.argv[1], "wb"))' "$PL"
  PATH="$T/pathbin:$PATH" "$S" status cc >/dev/null 2>&1; [ -s "$T/py-used" ] && pass bare-interpreter-on-path-is-used || bad bare-interpreter-on-path-is-used
  python3 -c 'import plistlib,sys; plistlib.dump({"ProgramArguments": ["python3.78", "-u"]}, open(sys.argv[1], "wb"))' "$PL"
  out="$(perl -e 'alarm shift; exec @ARGV' 30 "$S" start cc --wait 20 2>&1)"; rc=$?
  [ $rc -ne 0 ] && [ "$(wc -l <<<"$out")" -eq 1 ] && grep -q "python3.78, which no longer exists" <<<"$out" \
    && pass bare-interpreter-missing-is-named || bad "bare-interpreter-missing-is-named: rc=$rc $out"
  : > "$PL"
else
  SKIPPED="script-uses-the-plists-interpreter|script-keeps-python3-for-an-env-plist|start-names-a-vanished-interpreter|bare-interpreter-on-path-is-used|bare-interpreter-missing-is-named"
  echo "SKIP the plist-interpreter cases (macos-only: PlistBuddy)"
fi

# dead-man SQL: static oracle
Y="$REPO/observability/grafana/provisioning/alerting/rules.yaml"
grep -q "payload->>'watch'" "$Y" && grep -q "group by board" "$Y" \
  && ! grep -q "'capsule-corp'" "$Y" && pass deadman-static || bad deadman-static   # never listed before go-live
# dead-man SQL: an aborted run must not leak the container (the EXIT trap removes it)
grep -q '^trap .*docker rm -fv "\$PGC"' "$0" && grep -q 'PGC="\$C"' "$0" && pass deadman-pg-trap-static || bad deadman-pg-trap-static
# dead-man SQL: real Postgres oracle
. "$REPO/tests/lib_docker_optin.sh"
if ! docker_optin deadman-pg; then :
elif ! docker_ready deadman-pg; then bad "deadman-pg: opted in but docker unavailable"
else
  SQL="$(python3 -c 'import re,sys; t=open(sys.argv[1]).read(); b=t[t.index("uid: capsule-tick-deadman"):]; print(re.search(r"rawSql: \"(.*?)\"", b).group(1))' "$Y")"
  C=capsule-tick-sh-pg-$$; PGC="$C"
  docker run -d --rm --label capsule=1 --label capsule.kind=test --name "$C" -e POSTGRES_PASSWORD=x \
    --mount type=volume,dst=/var/lib/postgresql/data,volume-label=capsule=1,volume-label=capsule.kind=test \
    postgres:16-alpine >/dev/null
  for _ in $(seq 60); do docker exec "$C" pg_isready -h 127.0.0.1 -U postgres >/dev/null 2>&1 && break; sleep 1; done
  docker exec -i "$C" psql -U postgres -q <<'EOF'
create table events (ts timestamptz, kind text, board text, payload jsonb not null default '{}');
insert into events values (now(), 'tick_health', 'sample-app', '{"watch": true}');
EOF
  q() { docker exec "$C" psql -U postgres -tA -c "$SQL"; }
  zero="$(q)"                                            # sample-app fresh, capsule-corp unknown: quiet
  docker exec "$C" psql -U postgres -q -c "insert into events values (now() - interval '1 hour', 'tick_health', 'capsule-corp', '{\"watch\": true}')"
  dead="$(q)"                                            # capsule-corp joined, then went silent: pages
  docker exec "$C" psql -U postgres -q -c "insert into events values (now(), 'tick_health', 'capsule-corp', '{\"watch\": false}')"
  retired="$(q)"                                         # its last heartbeat said watch false: quiet
  docker stop "$C" >/dev/null; PGC=""   # --rm: stopping removes the container and its labelled volume
  python3 -c "import sys; sys.exit(0 if float('$zero') < 1 and float('$dead') >= 59 and float('$retired') < 1 else 1)" \
    && pass deadman-pg || bad "deadman-pg: zero=$zero dead=$dead retired=$retired"
fi
# Z1 (row 526): contracts/intent/capsule_tick_sh.jsonl replayed here, no extra run - each example names a check above that passed.
python3 -c 'import json,sys; rows=[json.loads(l) for l in open(sys.argv[1]) if l.strip()]; got=(sys.argv[2]+"|"+sys.argv[3]).split("|"); sys.exit(0 if rows and all(r["expect"]["pass"] in got for r in rows) else 1)' \
  "$REPO/contracts/intent/capsule_tick_sh.jsonl" "$PASSED" "$SKIPPED" && pass intent-examples || bad intent-examples
exit $fail
