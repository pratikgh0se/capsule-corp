#!/usr/bin/env bash
# capsule-park.sh - park the pipeline for an absence, and unpark it after
# (spec docs/superpowers/specs/2026-09-10-continuity-and-data-safety-design.md
# section 7.6, decision row 137, founder's-week study section 6.4).
#
#   capsule park                   bootout every DISPATCHING plist, keep every
#                                  custodial one, stamp it, take one final
#                                  encrypted backup, print the checklist
#   capsule unpark                 bootstrap them back, clear the stamp, and
#                                  check every Potara identity still reads
#   capsule status                 parked since <date>, or "not parked"
#
# This is deliberately NOT the retired system's pause verb, which would stop heal and the
# backups - exactly backwards. During an absence the custodial jobs matter
# MORE and the productive jobs matter less: park stops the machine producing
# work nobody can approve, and leaves running everything that makes the
# return safe. Resume is this script, and that reversibility is
# the whole reason park is safe to use.
#
# It does not push anything and does not copy anything off the machine: those
# are the two steps a script must not do unattended (and row 137's off-machine
# copy is the owner's weekly external-disk backup), so they are printed as a
# checklist instead.
set -uo pipefail

: "${LAUNCHCTL_BIN:=launchctl}"          # overridable for tests
REPO="$(cd -P "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
: "${CAPSULE_SECRET_BIN:=$REPO/bin/capsule-secret}"  # same name as backup_key.sh/lib_compose.sh, overridable for tests
: "${ABSENCE_PY_BIN:=$REPO/maintenance/absence.py}"  # overridable for tests (a fake absence.py that fails)
# State root: inlined from maintenance/lib_state.sh (the deployed copy of this script has no sibling lib).
STATE_DIR="${AGENT_OPS_STATE_DIR:-}"
if [ -z "$STATE_DIR" ]; then
  if [ -n "${AGENT_OPS_NO_SEND:-}" ]; then STATE_DIR="${TMPDIR:-/tmp}"; STATE_DIR="${STATE_DIR%/}/capsule-corp-state-no-send"
  else STATE_DIR="$HOME/.local/state/capsule-corp/maint"; fi
fi
STAMP="$STATE_DIR/park.json"
UID_="$(id -u)"

# The DISPATCHING/CUSTODIAL lists and classify_check moved to
# lib_jobs.sh (spec 2026-09-12-hoi-poi-design.md section 3) so hoi-poi.sh can
# share them; this script's behaviour and test are unchanged.
. "$REPO/bin/lib_jobs.sh"

usage() { echo "usage: capsule-park.sh <park|unpark|status>" >&2; exit 2; }
say() { printf '%s\n' "$*"; }

# A failed absence.py call used to disappear behind
# `|| true` - park/unpark still finished (correctly - the tier hold is
# advisory, not the reason to abort a park), but the operator had no way to
# know the hold/clear/seen call itself failed. Now it prints the manual
# fallback command instead of nothing.
absence_call() {  # absence_call <verb...> - never fails the caller
  if ! python3 "$ABSENCE_PY_BIN" "$@" >/dev/null 2>&1; then
    say "  absence.py $* FAILED - run manually: python3 $ABSENCE_PY_BIN $*"
  fi
}

boot() {  # boot <bootout|bootstrap> <label...>
  local verb="$1"; shift
  local l plist
  for l in "$@"; do
    if [ "$verb" = bootout ]; then
      if "$LAUNCHCTL_BIN" bootout "gui/$UID_/$l"; then say "  booted out $l"
      else say "  $l was not loaded (fine)"; fi
    else
      plist="$HOME/Library/LaunchAgents/$l.plist"
      if "$LAUNCHCTL_BIN" bootstrap "gui/$UID_" "$plist"; then say "  bootstrapped $l"
      else say "  could not bootstrap $l - is $plist there? A tick job's plist comes from ./install.sh, then capsule board tick <board>; the notes-vault jobs are the maintainer's own setup"; fi
    fi
  done
}

CMD="${1:-}"; [ -n "$CMD" ] || usage; shift || true

case "$CMD" in
  park)
    [ $# -eq 0 ] || usage
    classify_check
    mkdir -p -m 700 "$STATE_DIR"
    LABELS=("${DISPATCHING[@]}")
    say "capsule park: stopping ${#LABELS[@]} dispatching jobs, keeping ${#CUSTODIAL[@]} custodial ones"
    # the stamp comes BEFORE the bootout (tick-watch must not start a parked tick); it also
    # stops status.sh and the brief looking healthy
    python3 - "$STAMP" "${LABELS[@]}" <<'PY'
import json, os, sys, time
path, *labels = sys.argv[1:]
tmp = path + ".tmp"
json.dump({"v": 1, "parked_since": time.time(), "labels": labels,
           "by": "capsule-park"}, open(tmp, "w"), indent=1)
os.chmod(tmp, 0o600); os.replace(tmp, path)
PY
    boot bootout "${LABELS[@]}"
    absence_call hold 3
    absence_call seen
    if [ "${CAPSULE_PARK_BACKUP:-1}" = 1 ]; then
      say "capsule park: one final backup"
      bash "$REPO/maintenance/run.sh" backup || say "  the final backup FAILED - fix it before you leave"
    fi
    cat <<EOF

capsule park: done. Before you go - two things this script does not push and
does not copy, because neither should happen unattended:

  [ ] push every repo with unpushed work
      and the vault before you leave
  [ ] copy the newest \$BACKUP_DIR/*.tar.gz.enc to the external disk
        (row 137: the weekly external-disk laptop backup IS the off-machine
        copy for now - a blob-storage target is parked to ~2026-11)
  [ ] check the printed key sheet is where SUCCESSION.md says it is

Still running on purpose: heal, backup, restore-verify, cleanup, obs-probe,
obs-inventory, quality-nightly, health, report, security, ollama-serve.
Resume with:  capsule-park.sh unpark
EOF
    ;;
  unpark)
    [ $# -eq 0 ] || usage
    if [ -f "$STAMP" ]; then
      # No mapfile: bash 3.2 (the macOS /bin/bash this runs on) has none.
      LABELS=()
      while IFS= read -r l; do [ -n "$l" ] && LABELS+=("$l"); done < <(python3 -c '
import json,sys
try: print("\n".join(json.load(open(sys.argv[1])).get("labels") or []))
except Exception: pass' "$STAMP")
      # a stamp written before a label was added to launchd/unloaded must not bootstrap that
      # label back: filter through lib_jobs.sh's own _loadable, as the live lists already are.
      # Also keep only labels park can boot out today (DISPATCHING): a stamp from before the gateway
      # class was retired may still name old gateway labels, which must never be bootstrapped.
      _kept=(); for l in ${LABELS[@]+"${LABELS[@]}"}; do
        _loadable "$l" || continue
        for _d in "${DISPATCHING[@]}"; do [ "$_d" = "$l" ] && { _kept+=("$l"); break; }; done
      done
      LABELS=(${_kept[@]+"${_kept[@]}"}); unset _kept
    else
      LABELS=("${DISPATCHING[@]}")
      say "capsule-park: no stamp at $STAMP - restoring the canonical dispatching list"
    fi
    [ "${#LABELS[@]}" -gt 0 ] || LABELS=("${DISPATCHING[@]}")
    say "capsule unpark: restarting ${#LABELS[@]} jobs"
    boot bootstrap "${LABELS[@]}"
    rm -f "$STAMP"
    absence_call clear
    absence_call seen
    # M222 is still open (the Infisical identity TTL is unverified). If it is
    # shorter than the park was, every custodial job now fails closed
    # unwatched - so check on the way back in rather than assume either answer.
    say "capsule unpark: checking Potara identities (M222 - the TTL is unverified)"
    if CAPSULE_IDENTITY="${CAPSULE_IDENTITY:-bridge}" "$CAPSULE_SECRET_BIN" check --identity-all; then
      say "  capsule-secret check --identity-all: ok"
    else
      say "  capsule-secret check --identity-all FAILED - re-issue the machine identities before trusting a job"
    fi
    say "capsule unpark: done."
    ;;
  status)
    [ $# -eq 0 ] || usage
    if [ -f "$STAMP" ]; then
      python3 -c '
import datetime, json, sys
d = json.load(open(sys.argv[1]))
since = datetime.datetime.fromtimestamp(d["parked_since"]).strftime("%Y-%m-%d %H:%M")
n = len([l for l in (d.get("labels") or []) if l in set(sys.argv[2:])])
print(f"parked since {since} ({n} jobs stopped)")' "$STAMP" "${DISPATCHING[@]}"
    else
      say "not parked"
    fi
    ;;
  *) usage ;;
esac
