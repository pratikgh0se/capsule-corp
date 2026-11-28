#!/usr/bin/env bash
# capsule-tick.sh - one launchd job per board (factory-autonomy spec 4.3, ruling Q9):
#   start|stop|restart|status <board> [--quiet S] [--wait S] [--no-update] [--for S]
# stop writes $DIR/tick-stop.json {by, until} (default 2 h, --for S): tick-watch leaves a stopped tick
# alone while it is valid. start and restart delete it.
# The job is $CAPSULE_LAUNCHD_PREFIX.capsule-tick-<board> (prefix: bin/lib_prefix.sh), installed by `capsule board tick <board>` (after ./install.sh) (KeepAlive on failure only).
# The tick holds $CAPSULE_HOME/tick-<b>.lock, whose text is its pid (capsule/tick_guard.py); this
# script drives launchctl and reads the lock. It never signals a process itself.
# The job runs from the release clone $CAPSULE_HOME/harness/main (C08); restart fast-forwards it after the quiet wait.
# kickstart -k does not re-read the plist: a WorkingDirectory change needs stop + start (or rerun `capsule board tick <board>`).
set -uo pipefail
REPO="$(cd -P "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
# CAPSULE_HOME rule (row 609; the same block is in every script that defaults it, = capsule/paths.py home()): explicit wins; under AGENT_OPS_NO_SEND a temp dir, never the live home.
# Known differences from tempfile.gettempdir(): TEMP/TMP are not read without TMPDIR; a missing or unwritable TMPDIR falls back to /tmp only in Python; only one trailing slash is stripped; a relative TMPDIR is not made absolute.
if [ -z "${CAPSULE_HOME:-}" ]; then
  if [ -n "${AGENT_OPS_NO_SEND:-}" ]; then CAPSULE_HOME="${TMPDIR:-/tmp}"; CAPSULE_HOME="${CAPSULE_HOME%/}/capsule-home-no-send"
  else CAPSULE_HOME="$HOME/.local/share/capsule"; fi
fi
HOME_DIR="$CAPSULE_HOME"
LCTL="${CAPSULE_LAUNCHCTL:-launchctl}"
LA_DIR="${CAPSULE_LA_DIR:-$HOME/Library/LaunchAgents}"
usage() { echo "usage: capsule-tick.sh {start|stop|restart|status} <board> [--quiet S] [--wait S] [--no-update] [--for S]" >&2; exit 2; }
CMD="${1:-}"; B="${2:-}"; [ -n "$CMD" ] && [ -n "$B" ] || usage; shift 2
QUIET=""; WAIT=900; NOUPD=""; STOPFOR=7200   # --no-update: restart leaves the release checkout where it is (tick-watch)
while [ $# -gt 0 ]; do
  case "$1" in --quiet) QUIET="$2"; shift 2 ;; --wait) WAIT="$2"; shift 2 ;; --no-update) NOUPD=1; shift ;; --for) STOPFOR="$2"; shift 2 ;; *) usage ;; esac
done
# The prefix reader needs Python 3.11+, and the plist path needs the prefix: so find this board's tick plist by
# glob first (the prefix is unknown unless the env sets it) and hand the interpreter it runs to the reader.
_pl=""; _early_gone=""
for _f in "$LA_DIR"/${CAPSULE_LAUNCHD_PREFIX:-*}.capsule-tick-"$B".plist; do
  [ -e "$_f" ] && { [ -z "$_pl" ] && _pl="$_f" || _pl="?"; }
done
if [ -n "$_pl" ] && [ "$_pl" != "?" ] && [ -z "${CAPSULE_PY:-}" ]; then
  _p0="$(/usr/libexec/PlistBuddy -c "Print :ProgramArguments:0" "$_pl" 2>/dev/null)"
  case "${_p0##*/}" in python*)
    if case "$_p0" in */*) [ -x "$_p0" ] ;; *) command -v "$_p0" >/dev/null 2>&1 ;; esac; then CAPSULE_PY="$_p0"; export CAPSULE_PY
    else _early_gone="$_p0"; fi ;; esac
fi
# start/restart with a vanished plist interpreter: the same one line interpreter_present gives, before the prefix
# reader (which could only add a second, less useful error about the missing Python).
if [ -n "$_early_gone" ]; then case "$CMD" in start|restart)
  echo "capsule-tick: the plist runs $_early_gone, which no longer exists; move $_pl aside, then run ~/.local/bin/capsule board tick $B (run ./install.sh first if that fails; the launcher pins the same Python)" >&2
  exit 1 ;; esac; fi
. "$REPO/bin/lib_prefix.sh"
PFX="$(capsule_launchd_prefix)" || {
  [ -z "$_early_gone" ] || echo "capsule-tick: the plist runs $_early_gone, which no longer exists; move $_pl aside, then run ~/.local/bin/capsule board tick $B (run ./install.sh first if that fails; the launcher pins the same Python)" >&2
  exit 1; }
LABEL="$PFX.capsule-tick-$B"; PLIST="$LA_DIR/$LABEL.plist"; DOMAIN="gui/$(id -u)"
DIR="$HOME_DIR/boards/$B"; LOCKF="$HOME_DIR/tick-$B.lock"
if [ -f "$DIR/config.json" ]; then LOG="$HOME_DIR/logs/tick-$B.log"; BOARDFLAG="--board $B"; else LOG="$HOME_DIR/logs/tick.log"; BOARDFLAG=""; fi

# The interpreter the installed plist runs (`capsule board tick` writes it in; a stock Mac's python3 is 3.9, too old
# for capsule). A plist that names /usr/bin/env, no plist or no PlistBuddy: python3.
PY="$(/usr/libexec/PlistBuddy -c "Print :ProgramArguments:0" "$PLIST" 2>/dev/null)"; GONE=""
case "${PY##*/}" in python*) { case "$PY" in */*) [ -x "$PY" ] ;; *) command -v "$PY" >/dev/null 2>&1 ;; esac; } || { GONE="$PY"; PY=python3; } ;; *) PY=python3 ;; esac
interpreter_present() {   # a plist whose python was uninstalled would crash-loop under launchd: say so, never wait
  [ -z "$GONE" ] && return 0
  echo "capsule-tick: the plist runs $GONE, which no longer exists; move $PLIST aside, then run ~/.local/bin/capsule board tick $B (run ./install.sh first if that fails; the launcher pins the same Python)" >&2
  return 1
}

pid_of() { head -n 1 "$LOCKF" 2>/dev/null | tr -dc '0-9'; }
lock_held() { (cd "$REPO" && "$PY" -c 'import sys; from capsule import tick_guard; sys.exit(0 if tick_guard.held(sys.argv[1], sys.argv[2]) else 1)' "$HOME_DIR" "$B"); }
is_tick() { local c; c="$(ps -o command= -p "$1" 2>/dev/null)"; [[ "$c" == *"-m capsule tick"* ]] && { [ -z "$BOARDFLAG" ] || [[ "$c" == *"$BOARDFLAG"* ]]; }; }
alive() { local p; p="$(pid_of)"; [ -n "$p" ] && kill -0 "$p" 2>/dev/null && is_tick "$p"; }
loaded() { "$LCTL" print "$DOMAIN/$LABEL" >/dev/null 2>&1; }
open_runs() {
  "$PY" -c 'import sqlite3,sys; c=sqlite3.connect("file:"+sys.argv[1]+"?mode=ro", uri=True)
print(c.execute("select count(*) from task_runs where ended_at is null").fetchone()[0])' "$DIR/kanban.db" 2>/dev/null || echo 0
}
checking() {   # 1 while a detached lander worker (guard_state lander.checking, G2b) is alive: a restart must not kill a landing
  "$PY" -c 'import json,os,sqlite3,sys
try:
    row = sqlite3.connect("file:"+sys.argv[1]+"?mode=ro", uri=True).execute("select value from guard_state where key = ?", ("lander.checking",)).fetchone()
    os.kill(int(json.loads(row[0])["pid"]), 0)
except (sqlite3.Error, OSError, TypeError, ValueError, KeyError):
    print(0); sys.exit(0)
print(1)' "$DIR/kanban.db" 2>/dev/null || echo 0
}
contract_live() {   # 1 while a card in review has a contract job (a tick thread, no run row; capsule.idle_lane.contract_cards): a restart kills it
  (cd "$REPO" && "$PY" -c 'import sqlite3,sys,time; from capsule import idle_lane
print(1 if idle_lane.contract_cards(sqlite3.connect("file:"+sys.argv[1]+"?mode=ro", uri=True), time.time()) else 0)' "$DIR/kanban.db" 2>/dev/null) || echo 0
}
quiet() {   # waits QUIET seconds (wall clock, not loop passes: each probe takes time) for no run, no lander worker, no contract job
  [ -z "$QUIET" ] && return 0
  local end=$((SECONDS + QUIET))
  while [ "$SECONDS" -lt "$end" ]; do
    [ "$(open_runs)" = "0" ] && [ "$(checking)" = "0" ] && [ "$(contract_live)" = "0" ] && return 0
    sleep 1
  done
  [ "$(contract_live)" = "1" ] && echo "capsule-tick: $B: a contract job was still live after the quiet wait; restarting anyway" >&2
  return 0
}
wait_live() {   # $1: a pid the new process must differ from ("" = any)
  for _ in $(seq $(( (WAIT + 15) * 10 ))); do
    local p; p="$(pid_of)"
    if [ -n "$p" ] && [ "$p" != "$1" ] && alive && lock_held; then echo "capsule-tick: $B running (pid $p)"; return 0; fi
    sleep 0.1
  done
  echo "capsule-tick: $B did not come up holding its lock; see $LOG" >&2; return 1
}

RELEASE="$HOME_DIR/harness/main"   # the tick's WorkingDirectory (C08): a plain clone, moved only by restart
status() {
  local l=false a=false k=false; loaded && l=true; alive && a=true; lock_held && k=true
  "$PY" -c 'import json,os,sys
print(json.dumps({"board": sys.argv[1], "label": sys.argv[2], "loaded": sys.argv[3]=="true",
                  "pid": int(sys.argv[4]) if sys.argv[4] else None, "alive": sys.argv[5]=="true",
                  "lock": sys.argv[6]=="true", "log": sys.argv[7], "main_sha": sys.argv[8] or None,
                  "loaded_sha": (lambda f: f[1] if len(f)==2 and f[0]==sys.argv[4] else None)(open(sys.argv[9]).read().split() if os.path.exists(sys.argv[9]) else [])}))' \
    "$B" "$LABEL" "$l" "$(pid_of)" "$a" "$k" "$LOG" "$(git -C "$RELEASE" rev-parse HEAD 2>/dev/null)" "$HOME_DIR/tick-$B.sha"
}
release_present() {   # fail closed (Z6): a missing release checkout means the installed plist cannot spawn the job
  [ -d "$RELEASE/.git" ] || [ -f "$RELEASE/.git" ] && return 0
  echo "capsule-tick: release checkout $RELEASE missing; clone it first: git clone $(git -C "$REPO" remote get-url origin 2>/dev/null || echo '<origin url>') $RELEASE" >&2
  return 1
}
update_release() {   # ff-only to origin/main; offline or diverged, nothing moves and the old tick keeps running
  [ -z "$(git -C "$RELEASE" status --porcelain 2>&1)" ] || { echo "capsule-tick: $RELEASE has uncommitted changes; not restarting" >&2; return 1; }
  git -C "$RELEASE" fetch -q origin \
    || { echo "capsule-tick: fetch from origin failed (offline?); $RELEASE not updated, not restarting" >&2; return 1; }
  git -C "$RELEASE" merge -q --ff-only origin/main \
    || { echo "capsule-tick: $RELEASE diverged from origin/main; not restarting" >&2; return 1; }
  echo "capsule-tick: release checkout at $(git -C "$RELEASE" rev-parse HEAD)"
}
stop_marker() {   # write the stop marker (stop) or delete it (start, restart)
  if [ "$1" = clear ]; then rm -f "$DIR/tick-stop.json"; return 0; fi
  mkdir -p "$DIR" && "$PY" -c 'import json,os,sys,time
json.dump({"by": sys.argv[2], "until": time.time() + int(sys.argv[3])}, open(sys.argv[1] + ".tmp", "w"))
os.replace(sys.argv[1] + ".tmp", sys.argv[1])' "$DIR/tick-stop.json" "${CAPSULE_STOP_BY:-capsule-tick.sh}" "$STOPFOR"
}
start() {
  [ -f "$PLIST" ] || { echo "capsule-tick: $PLIST not installed; run ./install.sh if you have not, then ~/.local/bin/capsule board tick $B" >&2; return 1; }
  release_present && interpreter_present || return 1
  stop_marker clear
  if alive && lock_held; then echo "capsule-tick: $B already running (pid $(pid_of))"; return 0; fi
  if loaded; then "$LCTL" kickstart "$DOMAIN/$LABEL"; else "$LCTL" bootstrap "$DOMAIN" "$PLIST"; fi
  wait_live ""
}
stop() {
  local p=""; alive && p="$(pid_of)"; stop_marker set; quiet     # the lock file keeps a dead tick's pid; never wait on a reused one
  loaded && "$LCTL" bootout "$DOMAIN/$LABEL"
  [ -z "$p" ] && return 0
  for _ in $(seq "$WAIT"); do kill -0 "$p" 2>/dev/null || { echo "capsule-tick: $B stopped"; return 0; }; sleep 1; done
  echo "capsule-tick: $B pid $p still running after ${WAIT}s (a long pass); launchd kills it at ExitTimeOut" >&2; return 1
}
restart() {
  [ -f "$PLIST" ] || { echo "capsule-tick: $PLIST not installed; run ./install.sh if you have not, then ~/.local/bin/capsule board tick $B" >&2; return 1; }
  release_present && interpreter_present || return 1
  local p; p="$(pid_of)"; stop_marker clear; quiet
  [ -n "$NOUPD" ] || update_release || return 1   # after the quiet wait: the files move only just before the kickstart
  if loaded; then "$LCTL" kickstart -k "$DOMAIN/$LABEL"; else "$LCTL" bootstrap "$DOMAIN" "$PLIST"; fi
  wait_live "$p"
}
case "$CMD" in start) start ;; stop) stop ;; restart) restart ;; status) status ;; *) usage ;; esac
