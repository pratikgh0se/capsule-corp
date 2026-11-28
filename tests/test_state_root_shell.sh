#!/usr/bin/env bash
# State root, shell side (card t_c8b3d583, U2). Every shell script that kept state under
# <checkout>/maintenance/state now resolves it through maintenance/lib_state.sh:
# $AGENT_OPS_STATE_DIR, else $HOME/.local/state/capsule-corp/maint. Oracle 1: a run from a copy
# in another directory lands its files under the scratch HOME's new root and nothing under the
# copy (listing diff). Oracle 2: a grep allow-list for the old spellings.
set -u
unset CAPSULE_JOBS_DIR   # the jobs config dir comes from each case's fake home
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OLD_HOME_NAME="$(PYTHONPATH="$REPO" python3 -c 'from capsule import legacy_names as n; print(n.LEGACY_HOME_DIRNAME)')"
T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
fails=0; check() { if eval "$2"; then echo "PASS $1"; else echo "FAIL $1"; fails=$((fails+1)); fi; }
unset AGENT_OPS_STATE_DIR AGENT_OPS_NO_SEND CLEANUP_STATE_DIR CLEANUP_EVENTS_SPOOL CLEANUP_EVENTS_OFFSET \
  CLEANUP_RETENTION_APPLY
export CLEANUP_TMPDIR="$T/no-tmp"    # t_dd689940: cleanup.sh below can never scan the real TMPDIR
H="$T/home"; ROOT="$H/.local/state/capsule-corp/maint"
mkdir -p "$H/$OLD_HOME_NAME/bin" "$T/copy/maintenance" "$T/fakebin" "$T/vault"
cp "$REPO/maintenance/cleanup.sh" "$REPO/maintenance/status.sh" "$T/copy/maintenance/"
[ -f "$REPO/maintenance/lib_state.sh" ] && cp "$REPO/maintenance/lib_state.sh" "$T/copy/maintenance/"
cp "$REPO/maintenance/lib_config.sh" "$T/copy/maintenance/"
printf '#!/usr/bin/env bash\nexit 0\n' >"$T/fakebin/launchctl"
cp "$T/fakebin/launchctl" "$T/fakebin/capsule-secret"; chmod +x "$T/fakebin/launchctl" "$T/fakebin/capsule-secret"

# capsule-park.sh, executed as the deployed copy (the old deploy folder), no env: the stamp lands in the root
cp "$REPO/bin/capsule-park.sh" "$REPO/bin/lib_jobs.sh" "$REPO/bin/lib_prefix.sh" "$H/$OLD_HOME_NAME/bin/"
CAPSULE_LAUNCHD_PREFIX=dev.capsule HOME="$H" LAUNCHCTL_BIN="$T/fakebin/launchctl" CAPSULE_SECRET_BIN="$T/fakebin/capsule-secret" \
  CAPSULE_PARK_BACKUP=0 bash "$H/$OLD_HOME_NAME/bin/capsule-park.sh" park >"$T/park.log" 2>&1
check "capsule-park.sh from the old deploy folder writes park.json under the state root" '[ -f "$ROOT/park.json" ]'
check "capsule-park.sh leaves nothing beside its deployed copy" '[ ! -e "$H/$OLD_HOME_NAME/maintenance" ] && [ ! -e "$H/.config" ]'
# status.sh (a copy elsewhere, no env) reads the very stamp capsule-park.sh wrote, through the
# capsule-park.sh beside it (../bin of the copy)
mkdir -p "$T/copy/bin"
cp "$REPO/bin/capsule-park.sh" "$REPO/bin/lib_jobs.sh" "$REPO/bin/lib_prefix.sh" "$T/copy/bin/"
sout="$(CAPSULE_LAUNCHD_PREFIX=dev.capsule HOME="$H" bash "$T/copy/maintenance/status.sh" --no-color 2>&1)"
check "status.sh with no env shows the park block from the state root" 'grep -q "^== park" <<<"$sout"'

# cleanup.sh: retention.json and the spool trimmer both default into the root
mkdir -p "$ROOT"; printf 'aa\nbb\ncc\n' >"$ROOT/events.jsonl"; printf '3' >"$ROOT/events.offset"
HOME="$H" CLEANUP_HOME="$H" CLEANUP_MAINT_LOGS="$T/logs" CLEANUP_NO_DOCKER=1 CLEANUP_NO_PG=1 CLEANUP_NO_EVENT=1 \
  bash "$T/copy/maintenance/cleanup.sh" >"$T/cleanup.log" 2>&1
check "cleanup.sh writes retention.json under the state root" '[ -f "$ROOT/retention.json" ]'
check "cleanup.sh trims the spool in the state root, not one beside itself" '[ "$(wc -c <"$ROOT/events.jsonl" | tr -d " ")" = 6 ]'
check "cleanup.sh creates nothing under its own directory" '[ ! -e "$T/copy/maintenance/state" ]'

# run.sh dry run, from a copy of the maintenance dir inside $T: the run cannot touch the checkout's own
# maintenance/state path (a migrated checkout holds a live shim there), so any state dir in the copy is a leak
rm -rf "$ROOT"; mkdir -p "$T/mcopy" "$T/skill"; cp -R "$REPO/maintenance" "$T/mcopy/maintenance"
rm -rf "$T/mcopy/maintenance/state" "$T/mcopy/maintenance/tests" "$T/mcopy/maintenance/logs"
runsh() {
  env HOME="$H" RUN_SH_MAINT_DIR="$T/mcopy/maintenance" RUN_SH_DRY_RUN=1 EVENTS_SPOOL="$T/ev.jsonl" \
    OBSIDIAN_VAULT_PATH="$T/vault" OBSIDIAN_SKILL_ROOT="$T/skill" CAPSULE_OPS_PY="${CAPSULE_OPS_PY:-python3}" "$@" bash "$T/mcopy/maintenance/run.sh" nightly >"$T/run.log" 2>&1
}
runsh
check "run.sh lands its write-guard settings under the state root with no env" '[ -f "$ROOT/write-guard.settings.json" ]'
check "run.sh creates no state dir beside itself (it ran from a copy, not the checkout)" '[ ! -e "$T/mcopy/maintenance/state" ]'
rm -rf "$ROOT"; runsh AGENT_OPS_STATE_DIR="$T/override"
check "AGENT_OPS_STATE_DIR wins in run.sh" '[ -f "$T/override/write-guard.settings.json" ] && [ ! -e "$ROOT/write-guard.settings.json" ]'
HOME="$H" AGENT_OPS_STATE_DIR="$T/override2" CLEANUP_HOME="$H" CLEANUP_MAINT_LOGS="$T/logs" CLEANUP_NO_DOCKER=1 \
  CLEANUP_NO_PG=1 CLEANUP_NO_EVENT=1 bash "$T/copy/maintenance/cleanup.sh" >/dev/null 2>&1
check "AGENT_OPS_STATE_DIR wins in cleanup.sh" '[ -f "$T/override2/retention.json" ]'

# the lib itself: default and override
check "lib_state.sh defaults to ~/.local/state/capsule-corp/maint" \
  '[ "$(env -u AGENT_OPS_STATE_DIR HOME=/h bash -c ". \"$REPO/maintenance/lib_state.sh\"; echo \$STATE_DIR")" = /h/.local/state/capsule-corp/maint ]'
check "lib_state.sh honours AGENT_OPS_STATE_DIR" \
  '[ "$(AGENT_OPS_STATE_DIR=/x HOME=/h bash -c ". \"$REPO/maintenance/lib_state.sh\"; echo \$STATE_DIR")" = /x ]'

# A script that sourced the lib (run.sh, report.sh) must not hand its children a root that beats their
# own NO_SEND temp dir: a NO_SEND child started by a real-root parent must not resolve to the real root.
child_root="$(env -u AGENT_OPS_STATE_DIR -u AGENT_OPS_NO_SEND HOME=/h TMPDIR="$T/pt/" bash -c \
  ". \"$REPO/maintenance/lib_state.sh\"; cd \"$REPO/maintenance\" && AGENT_OPS_NO_SEND=1 python3 -c 'import events; print(events.state_root())'")"
check "a NO_SEND child of a lib_state.sh script does not resolve to the real root" \
  '[ -n "$child_root" ] && [ "$child_root" != /h/.local/state/capsule-corp/maint ]'

# Parity with U1 (second oracle): lib_state.sh == events.state_root() over unset/empty/set x NO_SEND
parity_ok=1; mkdir -p "$T/pt"
PYDIR="${STATE_ROOT_PY_DIR:-$REPO/maintenance}"   # U1's events.py; a dir override lets a pre-merge run point at it
if ! grep -q '^def state_root' "$PYDIR/events.py"; then
  echo "NOTE parity oracle needs U1's events.state_root (not on this branch): rerun after U1 lands"; ns_list=""; else ns_list="0 1"; fi
for ov in unset empty set; do for ns in $ns_list; do
  e=(env -u AGENT_OPS_STATE_DIR -u AGENT_OPS_NO_SEND HOME=/h TMPDIR="$T/pt/")
  [ $ov = empty ] && e+=(AGENT_OPS_STATE_DIR=); [ $ov = set ] && e+=(AGENT_OPS_STATE_DIR=/x); [ $ns = 1 ] && e+=(AGENT_OPS_NO_SEND=1)
  sh_v="$("${e[@]}" bash -c ". \"$REPO/maintenance/lib_state.sh\"; echo \$STATE_DIR")"
  py_v="$(cd "$PYDIR" && "${e[@]}" python3 -c 'import events; print(events.state_root())')"
  [ "$sh_v" = "$py_v" ] || { parity_ok=0; echo "parity $ov NO_SEND=$ns: shell=$sh_v python=$py_v"; }
done; done
[ -z "$ns_list" ] || check "lib_state.sh equals events.state_root() for unset/empty/set x NO_SEND" '[ $parity_ok = 1 ]'
nsv="$(env -u AGENT_OPS_STATE_DIR AGENT_OPS_NO_SEND=1 HOME=/h TMPDIR="$T/pt/" bash -c ". \"$REPO/maintenance/lib_state.sh\"; echo \$STATE_DIR")"
check "NO_SEND with no override never resolves the real root" '[ "$nsv" = "$T/pt/capsule-corp-state-no-send" ]'

# capsule-park.sh cannot source the lib, so its inlined block must be byte-identical to the lib's
blk() { sed -n '/^STATE_DIR="\${AGENT_OPS_STATE_DIR:-}"/,/^fi$/p' "$1"; }
check "capsule-park.sh inlines lib_state.sh's state-root block byte for byte" \
  '[ -n "$(blk "$REPO/maintenance/lib_state.sh")" ] && [ "$(blk "$REPO/maintenance/lib_state.sh")" = "$(blk "$REPO/bin/capsule-park.sh")" ]'
# the lib exports nothing (a Python child re-resolves the same root, see the NO_SEND child check above)
exv="$(env -u AGENT_OPS_STATE_DIR HOME=/h bash -c ". \"$REPO/maintenance/lib_state.sh\"; python3 -c 'import os; print(os.environ.get(\"AGENT_OPS_STATE_DIR\"))'")"
check "lib_state.sh does not export AGENT_OPS_STATE_DIR to child processes" '[ "$exv" = None ]'

# G2 leak regression: an EMPTY override under NO_SEND through a non-dry run.sh job writes nothing under HOME
LH="$T/leakhome"; mkdir -p "$LH" "$T/leaktmp" "$T/leakmaint"
printf '#!/usr/bin/env bash\nexit 0\n' >"$T/leakmaint/deps_sync.sh"; chmod +x "$T/leakmaint/deps_sync.sh"
env HOME="$LH" TMPDIR="$T/leaktmp" AGENT_OPS_STATE_DIR= AGENT_OPS_NO_SEND=1 RUN_SH_MAINT_DIR="$T/leakmaint" \
  EVENTS_SPOOL="$T/leak-ev.jsonl" OBSIDIAN_VAULT_PATH="$T/vault" CAPSULE_OPS_PY="${CAPSULE_OPS_PY:-python3}" \
  bash "$REPO/maintenance/run.sh" deps-sync >/dev/null 2>&1
check "empty AGENT_OPS_STATE_DIR under NO_SEND leaves HOME/.local/state absent" \
  '[ ! -e "$LH/.local/state" ] && [ -f "$T/leaktmp/capsule-corp-state-no-send/jobs/deps-sync.json" ]'

# Oracle 2: any /state path that is not the root, a merge-gate/legacy-store path or an exact allow-listed line
old="$(cd "$REPO" && grep -nE '/state([/"} ]|$)' maintenance/*.sh bin/*.sh deploy.sh | grep -vE '^[^:]+:[0-9]+:[[:space:]]*#' \
  | grep -vE '\.local/state|\$STATE_DIR|merge-gate|DEPLOY_LEGACY_STATE:-\$REPO/maintenance/state|unmigrated state in|-> maintenance/state' \
  | grep -vE 'state\.db|'"${OLD_HOME_NAME//./\\.}"'|\.agents|kanban|worth keeping')"
check "no old state-dir spelling remains in the shell scripts (allow-list: comments, the NOTE)" '[ -z "$old" ]'
[ -z "$old" ] || printf '%s\n' "$old"

echo; if [ $fails -eq 0 ]; then echo "test_state_root_shell: all passed"; else echo "test_state_root_shell: $fails failed"; exit 1; fi
