#!/usr/bin/env bash
# shard: slow
# bin/capsule-state-migrate (t_c8b3d583 U3) against temp dirs only: a copy of the script sits in a
# fixture repo, AGENT_OPS_STATE_DIR and CAPSULE_HOME point into the temp dir. Never real state.
set -u
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC_BIN="${MIGRATE_BIN:-$REPO/bin/capsule-state-migrate}"
T="$(mktemp -d "${TMPDIR:-/tmp}/state-migrate.XXXXXX")" || exit 1; BG=""
trap 'for p in $BG; do kill "$p" 2>/dev/null; done; rm -rf "$T"' EXIT
export T; fails=0; check() { if eval "$2"; then echo "PASS $1"; else echo "FAIL $1"; fails=$((fails+1)); fi; }
# Hermetic ps: the real ps, filtered to this test's own processes (a real run.sh may be live).
mkdir -p "$T/psbin"; REALPS="$(command -v ps)"
printf '#!/bin/sh\n%s "$@" | grep -F "%s"\ntrue\n' "$REALPS" "$T" > "$T/psbin/ps"; chmod +x "$T/psbin/ps"
export PATH="$T/psbin:$PATH"
perm() { if stat -c %a / >/dev/null 2>&1; then stat -c %a "$1"; else stat -f %Lp "$1"; fi; }  # GNU stat first (its -f means filesystem), else BSD/macOS
dev() { python3 -c 'import os,sys;print(os.stat(sys.argv[1]).st_dev)' "$1"; }

fixture() { # $1 = name; builds $T/$1/repo with a state dir and exports the env
  F="$T/$1"; mkdir -p "$F/repo/bin" "$F/repo/maintenance/state/jobs" "$F/home"
  cp "$SRC_BIN" "$F/repo/bin/capsule-state-migrate" 2>/dev/null
  printf '{"a":1}\n{"b":2}\n' > "$F/repo/maintenance/state/events.jsonl"
  printf '8' > "$F/repo/maintenance/state/events.offset"
  printf 'x\n' > "$F/repo/maintenance/state/jobs/one"
  OLD="$F/repo/maintenance/state"; DEST="$F/root/maint"
  export AGENT_OPS_STATE_DIR="$DEST" CAPSULE_HOME="$F/home" HOME="$F/home"
  M="$F/repo/bin/capsule-state-migrate"
}
sums() { (cd "$1" && find . -type f | sort | while read -r f; do shasum -a 256 "$f"; done); }

# happy path + oracles
fixture happy; before="$(sums "$OLD")"
out="$("$M" 2>&1)"; rc=$?
check "migrate exits 0" '[ $rc -eq 0 ]'
check "old path is a symlink to the new root" '[ -L "$OLD" ] && [ "$(readlink "$OLD")" = "$DEST" ]'
check "oracle 1: every file hash is unchanged" '[ "$(sums "$DEST")" = "$before" ]'
check "oracle 2: count and bytes match through the shim" '[ "$(find "$OLD/" -type f | wc -l | tr -d " ")" = 3 ] && [ "$(cat "$OLD/events.jsonl" "$OLD/events.offset" "$OLD/jobs/one" | wc -c | tr -d " ")" = 19 ]'
check "new root parent is mode 700" '[ "$(perm "$(dirname "$DEST")")" = 700 ]'
check "rollback command is printed" 'echo "$out" | grep -q "rollback: rm .*mv "'
"$M" >/dev/null 2>&1; rc=$?
check "second run exits 0 as a no-op" '[ $rc -eq 0 ] && [ "$(sums "$DEST")" = "$before" ]'
"$M" --verify >/dev/null 2>&1; rc=$?
check "--verify passes right after migration" '[ $rc -eq 0 ]'
echo extra > "$DEST/extra"; "$M" --verify >/dev/null 2>&1; rc=$?
check "--verify fails on an extra file (count oracle)" '[ $rc -eq 1 ]'
rm "$DEST/extra"; echo changed > "$DEST/jobs/one"; "$M" --verify >/dev/null 2>&1; rc=$?
check "--verify fails on a changed file (hash oracle)" '[ $rc -eq 1 ]'

# dry-run writes nothing
fixture dry; before="$(sums "$OLD")"
out="$("$M" --dry-run 2>&1)"; rc=$?
check "dry-run exits 0 and says so" '[ $rc -eq 0 ] && echo "$out" | grep -q "dry-run"'
check "dry-run writes nothing" '[ ! -L "$OLD" ] && [ ! -e "$DEST" ] && [ ! -e "$DEST.manifest.json" ] && [ "$(sums "$OLD")" = "$before" ]'

# tick lock: the flock is the authority, a stale pid in the file is not
fixture lock
echo "$$" > "$CAPSULE_HOME/tick-stale.lock"
"$M" >/dev/null 2>&1; rc=$?
check "a stale lock file with no flock does not refuse" '[ $rc -eq 0 ]'
fixture lock2; before="$(sums "$OLD")"
python3 -c 'import fcntl,os,sys,time
f=open(sys.argv[1],"w"); fcntl.flock(f,fcntl.LOCK_EX); f.write(str(os.getpid())); f.flush(); time.sleep(60)' "$CAPSULE_HOME/tick-x.lock" &
BG="$BG $!"; sleep 1
out="$("$M" 2>&1)"; rc=$?
check "a held tick lock refuses (exit 1) and moves nothing" '[ $rc -eq 1 ] && echo "$out" | grep -q "tick lock held" && [ ! -L "$OLD" ] && [ ! -e "$DEST" ] && [ "$(sums "$OLD")" = "$before" ]'

# maintenance jobs live: argv0 shape, the real launchd shape (/bin/bash <path>/run.sh <job>), report.sh
mkdir -p "$T/x/maintenance"; printf '#!/bin/sh\nsleep 60\n' > "$T/x/maintenance/run.sh"
cp "$T/x/maintenance/run.sh" "$T/x/maintenance/report.sh"
live_case() { # $1 = name, rest = command to start in the background
  fixture "$1"; before="$(sums "$OLD")"; name="$1"; shift; "$@" & LIVE=$!; BG="$BG $LIVE"; sleep 1
  out="$("$M" 2>&1)"; rc=$?
  check "live job ($name) refuses and moves nothing" '[ $rc -eq 1 ] && echo "$out" | grep -q "is running" && [ ! -L "$OLD" ] && [ "$(sums "$OLD")" = "$before" ]'
  pkill -P "$LIVE" 2>/dev/null; kill "$LIVE" 2>/dev/null; wait "$LIVE" 2>/dev/null
}
live_case runsh-argv0 bash -c 'exec -a "$T/maintenance/run.sh" sleep 60'
live_case runsh-bash /bin/bash "$T/x/maintenance/run.sh" sprint-sync
live_case report-bash /bin/bash "$T/x/maintenance/report.sh"

# destination not empty
fixture dest; before="$(sums "$OLD")"; mkdir -p "$DEST"; echo keep > "$DEST/mine"
out="$("$M" 2>&1)"; rc=$?
check "a non-empty destination refuses and keeps it" '[ $rc -eq 1 ] && echo "$out" | grep -q "not empty" && [ "$(cat "$DEST/mine")" = keep ] && [ ! -L "$OLD" ] && [ "$(sums "$OLD")" = "$before" ]'
fixture destempty; mkdir -p "$DEST"; "$M" >/dev/null 2>&1
check "an empty existing destination is used, not nested" '[ -f "$DEST/events.jsonl" ] && [ ! -e "$DEST/state" ]'

# cross-volume
other=""
for c in /dev/shm /dev; do
  if [ -d "$c" ] && [ "$(dev "$c")" != "$(dev "$T")" ]; then other="$c"; break; fi
done
fixture vol; before="$(sums "$OLD")"
check "a second volume exists for the cross-volume case" '[ -n "$other" ]'
export AGENT_OPS_STATE_DIR="$other/capsule-migrate-test-$$/maint"
out="$("$M" 2>&1)"; rc=$?
check "a destination on another volume refuses before writing" '[ $rc -eq 1 ] && echo "$out" | grep -q "different volumes" && [ ! -e "$other/capsule-migrate-test-$$" ] && [ "$(sums "$OLD")" = "$before" ]'

# fake mv: runs $2 (shell) first, then the real mv, then $3 (shell)
mkshim() { mkdir -p "$F/bin"; printf '#!/bin/sh\n%s\n%s "$@" || exit $?\n%s\n' "$2" "$(command -v mv)" "$3" > "$F/bin/mv"; chmod +x "$F/bin/mv"; }

# straggler: a job recreates the old path (conflicting file, new file, dotfile) after the mv
fixture strag; before="$(sums "$OLD")"
mkshim x "" "mkdir -p '$OLD/jobs' && echo late > '$OLD/events.jsonl' && echo late1 > '$OLD/jobs/one' && echo new > '$OLD/jobs/x' && echo dot > '$OLD/.hidden' && echo odd > \"$OLD/jobs/it's a b.txt\""
out="$(PATH="$F/bin:$PATH" "$M" 2>&1)"; rc=$?
check "a straggler exits 2" '[ $rc -eq 2 ]'
check "the straggler is not overwritten by the script" '[ ! -L "$OLD" ] && [ "$(cat "$OLD/events.jsonl")" = late ] && [ "$(sums "$DEST")" = "$before" ]'
check "the plan flags the conflict and names the dotfile" 'echo "$out" | grep -q "CONFLICT jobs/one" && echo "$out" | grep -q "mv -n .*\.hidden"'
check "the plan never prints rm or a bare mv" '! echo "$out" | grep "^  " | grep -Eq "(^|[ ;&])rm |mv [^-]"'
echo "$out" | grep -E "^  (cat|mkdir|find|rmdir)" | while IFS= read -r line; do bash -c "$line" >/dev/null 2>&1; done
check "following the plan keeps every migrated byte" '[ "$(cat "$DEST/jobs/one")" = x ] && [ "$(cat "$DEST/jobs/x")" = new ] && [ "$(cat "$DEST/.hidden")" = dot ]'
check "a filename with a space and a quote gives a copy-pasteable mv" '[ "$(cat "$DEST/jobs/it'"'"'s a b.txt")" = odd ] && [ ! -e "$OLD/jobs/it'"'"'s a b.txt" ]'
check "the plan appends the stray events" '[ "$(tail -n 1 "$DEST/events.jsonl")" = late ] && [ "$(wc -l < "$DEST/events.jsonl" | tr -d " ")" = 3 ]'
check "the conflicting stray survives and no shim is made" '[ "$(cat "$OLD/jobs/one")" = late1 ] && [ ! -L "$OLD" ]'

# run-mode oracles fail for real: a file changes (same size) or appears between the manifest and the mv
fixture o1; mkshim x "printf 'y\n' > '$OLD/jobs/one'" ""
out="$(PATH="$F/bin:$PATH" "$M" 2>&1)"; rc=$?
check "oracle 1 (sha256) fails a same-size change in run mode" '[ $rc -eq 1 ] && echo "$out" | grep -q "oracle 1" && ! echo "$out" | grep -q "oracle 2" && echo "$out" | grep -q "rollback: rm"'
fixture o2; mkshim x "echo new > '$OLD/added'" ""
out="$(PATH="$F/bin:$PATH" "$M" 2>&1)"; rc=$?
check "oracle 2 (count/bytes) fails an added file in run mode" '[ $rc -eq 1 ] && echo "$out" | grep -q "oracle 2" && ! echo "$out" | grep -q "oracle 1" && echo "$out" | grep -q "rollback: rm"'

# re-run hints
fixture hint; "$M" >/dev/null 2>&1; rm "$OLD"
out="$("$M" 2>&1)"; rc=$?
check "mv done but shim missing: exit 1 with the ln -s command" '[ $rc -eq 1 ] && echo "$out" | grep -q "shim missing" && echo "$out" | grep -q "ln -s"'
fixture wrong; mkdir -p "$F/elsewhere"; rm -r "$OLD"; ln -s "$F/elsewhere" "$OLD"
"$M" >/dev/null 2>&1; rc=$?
check "a symlink to some other place is not already migrated" '[ $rc -eq 1 ]'

# failed events.offset oracle
fixture off; printf '999' > "$OLD/events.offset"
"$M" >/dev/null 2>&1; rc=$?
check "an offset beyond events.jsonl fails the run (exit 1)" '[ $rc -eq 1 ] && [ -L "$OLD" ]'

if [ $fails -eq 0 ]; then echo "all passed"; else echo "$fails failed"; exit 1; fi
