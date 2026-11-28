#!/usr/bin/env bash
# The shell scripts that default CAPSULE_HOME follow the same rule as capsule/paths.py home() (row 609
# pattern): CAPSULE_HOME wins; else under AGENT_OPS_NO_SEND a temp dir (<tmp>/capsule-home-no-send), never
# the live ~/.local/share/capsule; else that default. Fake HOME and TMPDIR only; never the real home.
# Oracles: (1) each script's own `# CAPSULE_HOME rule` block, extracted and run, must print what
# python's paths.home() prints in the same env; (2) capsule-own and capsule-corp.zsh run for real.
set -u
unset CAPSULE_JOBS_DIR   # the jobs config dir comes from each case's fake home
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
fails=0; check() { if eval "$2"; then echo "PASS $1"; else echo "FAIL $1"; fails=$((fails+1)); fi; }
FH="$T/fakehome"; FT="$T/faketmp"; mkdir -p "$FH" "$FT"
TMPSLASH="$FT/"   # macOS TMPDIR carries a trailing slash; python's gettempdir() drops it
NOSEND_HOME="$FT/capsule-home-no-send"
LIVE="$FH/.local/share/capsule"
OLD_HOME_NAME="$(PYTHONPATH="$REPO" python3 -c 'from capsule import legacy_names as n; print(n.LEGACY_HOME_DIRNAME)')"
SCRIPTS="maintenance/backup.sh maintenance/cleanup.sh bin/capsule-own bin/capsule-keeper-watch.sh
bin/capsule-unblocked-hook.sh bin/capsule-secret bin/capsule-state-migrate bin/capsule-tick.sh docker/build-base.sh"

# home_of ENV... : the home each side resolves in a clean env plus the given assignments.
py_home() { (cd "$REPO" && env -i PATH="$PATH" HOME="$FH" TMPDIR="$TMPSLASH" "$@" python3 -c 'from capsule import paths; print(paths.home())'); }
rule_home() { # rule_home SCRIPT ENV... : run the script's extracted rule block, print CAPSULE_HOME
  local s="$1"; shift
  local blk; blk="$(awk '/^# CAPSULE_HOME rule/{f=1} f{print} f&&/^fi$/{exit}' "$REPO/$s")"
  [ -n "$blk" ] || { echo "NO-RULE-BLOCK"; return; }
  env -i PATH="$PATH" HOME="$FH" TMPDIR="$TMPSLASH" "$@" bash -c "set -u; HOME_ROOT=\"\$HOME\"; H=\"\$HOME\"
$blk
printf %s \"\$CAPSULE_HOME\""
}

for s in $SCRIPTS; do
  check "$s: marker set, no CAPSULE_HOME -> the temp home" '[ "$(rule_home "$s" AGENT_OPS_NO_SEND=1)" = "$NOSEND_HOME" ]'
  check "$s: marker set, empty CAPSULE_HOME -> the temp home" '[ "$(rule_home "$s" AGENT_OPS_NO_SEND=1 CAPSULE_HOME=)" = "$NOSEND_HOME" ]'
  check "$s: CAPSULE_HOME set wins over the marker" '[ "$(rule_home "$s" AGENT_OPS_NO_SEND=1 CAPSULE_HOME="$T/explicit")" = "$T/explicit" ]'
  check "$s: neither set -> the unchanged default" '[ "$(rule_home "$s")" = "$LIVE" ]'
  check "$s: matches python paths.home() under the marker" '[ "$(rule_home "$s" AGENT_OPS_NO_SEND=1)" = "$(py_home AGENT_OPS_NO_SEND=1)" ]'
  check "$s: matches python paths.home() with neither set" '[ "$(rule_home "$s")" = "$(py_home)" ]'
done
# The default home is spelled exactly once per script, inside the rule block's else-branch: no other line names it.
for s in $SCRIPTS bin/capsule-corp.zsh; do
  check "$s: only the rule block's else-branch names local/share/capsule" '[ "$(grep -c "local/share/capsule\([^-]\|$\)" "$REPO/$s")" = 1 ] && grep "local/share/capsule\([^-]\|$\)" "$REPO/$s" | grep -q "else .*local/share/capsule"'
done

# capsule-own for real: under the marker its queue lands in the temp home, nothing under the fake live home.
mkdir -p "$T/some-repo"
env -u CAPSULE_HOME HOME="$FH" TMPDIR="$TMPSLASH" AGENT_OPS_NO_SEND=1 "$REPO/bin/capsule-own" "$T/some-repo" fix/x agent-1 >/dev/null 2>&1; rc=$?
check "capsule-own under the marker exits 0" '[ $rc -eq 0 ]'
check "capsule-own under the marker writes the temp home" '[ -f "$NOSEND_HOME/queue/some-repo/owners.json" ]'
check "capsule-own under the marker leaves the live home absent" '[ ! -e "$LIVE" ]'
env -u CAPSULE_HOME -u AGENT_OPS_NO_SEND HOME="$FH" TMPDIR="$TMPSLASH" "$REPO/bin/capsule-own" "$T/some-repo" fix/y agent-2 >/dev/null 2>&1
check "capsule-own with neither set writes the default home (fake)" '[ -f "$LIVE/queue/some-repo/owners.json" ]'
rm -rf "$LIVE"

# capsule-corp.zsh _ops_db for real: the temp home's board wins under the marker; a live-home copy is ignored.
zdb() { env -u CAPSULE_HOME -u AGENT_OPS_NO_SEND HOME="$FH" TMPDIR="$TMPSLASH" "$@" zsh -c "source '$REPO/bin/capsule-corp.zsh'; _ops_db capsule-corp"; }
mkdir -p "$LIVE/boards/capsule-corp" "$NOSEND_HOME/boards/capsule-corp"; : >"$LIVE/boards/capsule-corp/kanban.db"
check "zsh _ops_db under the marker ignores the live copy" '[ "$(zdb AGENT_OPS_NO_SEND=1)" = "$FH/$OLD_HOME_NAME/kanban/boards/capsule-corp/kanban.db" ]'
: >"$NOSEND_HOME/boards/capsule-corp/kanban.db"
check "zsh _ops_db under the marker reads the temp home" '[ "$(zdb AGENT_OPS_NO_SEND=1)" = "$NOSEND_HOME/boards/capsule-corp/kanban.db" ]'
check "zsh _ops_db with neither set reads the default home" '[ "$(zdb A_NOOP=1)" = "$LIVE/boards/capsule-corp/kanban.db" ]'
check "zsh _ops_db: CAPSULE_HOME wins over the marker" '[ "$(zdb AGENT_OPS_NO_SEND=1 CAPSULE_HOME="$LIVE")" = "$LIVE/boards/capsule-corp/kanban.db" ]'

[ "$fails" -eq 0 ] && echo "all passed" || { echo "$fails failed"; exit 1; }
