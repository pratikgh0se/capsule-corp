#!/usr/bin/env bash
# bin/capsule-own (row 470 fix round 1, M4) against a fixture $CAPSULE_HOME.
set -u
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BIN="$REPO/bin/capsule-own"
T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
fails=0; check() { if eval "$2"; then echo "PASS $1"; else echo "FAIL $1"; fails=$((fails+1)); fi; }

export CAPSULE_HOME="$T/home"
FIXTURE_REPO="$T/some-repo"
mkdir -p "$FIXTURE_REPO"
OWNERS="$CAPSULE_HOME/queue/some-repo/owners.json"

"$BIN" "$FIXTURE_REPO" "fix/thing" "agent-1" >/dev/null; rc=$?
check "claiming a branch exits 0" '[ $rc -eq 0 ]'
check "owners.json now names the branch" 'grep -q "fix/thing" "$OWNERS"'
check "owners.json records the owner" 'grep -q "agent-1" "$OWNERS"'

owner_val="$(python3 -c "import json; print(json.load(open('$OWNERS'))['fix/thing']['owner'])")"
check "the owner value round-trips exactly" '[ "$owner_val" = "agent-1" ]'

heartbeat_before="$(python3 -c "import json; print(json.load(open('$OWNERS'))['fix/thing']['heartbeat'])")"
sleep 1
"$BIN" "$FIXTURE_REPO" "fix/thing" "agent-2" >/dev/null
heartbeat_after="$(python3 -c "import json; print(json.load(open('$OWNERS'))['fix/thing']['heartbeat'])")"
check "re-claiming refreshes the heartbeat" 'python3 -c "import sys; sys.exit(0 if float(\"$heartbeat_after\") > float(\"$heartbeat_before\") else 1)"'
check "re-claiming updates the owner" 'grep -q "agent-2" "$OWNERS"'

"$BIN" "$FIXTURE_REPO" "fix/thing" "--release" >/dev/null; rc=$?
check "releasing exits 0" '[ $rc -eq 0 ]'
check "the branch entry is gone after release" '! grep -q "fix/thing" "$OWNERS"'

"$BIN" "one" "two" >/dev/null 2>"$T/err"; rc=$?
check "wrong argument count exits non-zero" '[ $rc -ne 0 ]'
check "wrong argument count prints usage" 'grep -qi usage "$T/err"'

# The detector itself must actually see what capsule-own writes.
mkdir -p "$FIXTURE_REPO"
python3 - <<PYEOF
import subprocess
subprocess.run(["git", "init", "-q", "-b", "main"], cwd="$FIXTURE_REPO", check=True)
open("$FIXTURE_REPO/f.txt", "w").write("1")
env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
import os
full_env = dict(os.environ, **env)
subprocess.run(["git", "add", "f.txt"], cwd="$FIXTURE_REPO", check=True, env=full_env)
subprocess.run(["git", "commit", "-q", "-m", "init"], cwd="$FIXTURE_REPO", check=True, env=full_env)
subprocess.run(["git", "checkout", "-q", "-b", "fix/owned"], cwd="$FIXTURE_REPO", check=True, env=full_env)
open("$FIXTURE_REPO/f.txt", "w").write("2")
subprocess.run(["git", "commit", "-q", "-am", "change"], cwd="$FIXTURE_REPO", check=True, env=full_env)
PYEOF
"$BIN" "$FIXTURE_REPO" "fix/owned" "agent-3" >/dev/null
export PYTHONPATH="$REPO${PYTHONPATH:+:$PYTHONPATH}"
owner_seen="$(python3 -c "
from pathlib import Path
from capsule import unblocked
items = unblocked.branch_items('$FIXTURE_REPO', review_notes=None, now=__import__('time').time(),
                                run_status_by_commit={})
print(items[0].owner if items else 'NONE')
")"
check "capsule unblocked's own branch adapter sees the claim capsule-own wrote" '[ "$owner_seen" = "owners.json" ]'

echo "----"
if [ "$fails" -eq 0 ]; then echo "ALL PASS"; else echo "$fails FAILED"; fi
exit "$fails"
