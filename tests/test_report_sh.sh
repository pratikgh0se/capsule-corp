#!/usr/bin/env bash
# Pins report.sh's interpreter choice: CAPSULE_OPS_PY, else python3 (that last step is not pinned here:
# report.sh rebuilds PATH, so a fake python3 cannot be slipped in). The old interpreter variable is retired and not read.
# With no vault configured, report.sh runs the interpreter once (vault_resolve) and exits 0, so a
# logging fake interpreter shows which one was picked. Nothing is sent and no real state is touched.
set -u
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
T="$(mktemp -d "${TMPDIR:-/tmp}/report-sh.XXXXXX")"; trap 'rm -rf "$T"' EXIT
# A fake docker, found through AGENT_OPS_DOCKER_BIN (report.sh rebuilds PATH): no real docker ever runs.
mkdir -p "$T/dockerstub"; printf '#!/bin/sh\nexit 0\n' >"$T/dockerstub/docker"; chmod +x "$T/dockerstub/docker"
export AGENT_OPS_DOCKER_BIN="$T/dockerstub"
mkdir -p "$T/home" "$T/bin"
for n in py-old py-new; do
  printf '#!/bin/sh\necho "%s" >>"$RP_LOG"\n' "$n" >"$T/bin/$n"; chmod +x "$T/bin/$n"
done
export RP_LOG="$T/py.log"
OLD="$(python3 "$REPO/tests/denylist_term.py" retired)" || { echo "FAIL: denylist_term: release/private-denylist.txt is unusable"; exit 1; }
OLD_PY=""; [ -n "$OLD" ] && OLD_PY="$(printf %s "$OLD" | tr a-z A-Z)_PY"
fails=0
check() { if eval "$2"; then echo "PASS $1"; else echo "FAIL $1"; fails=$((fails+1)); fi; }
run_rp() {  # [extra env...] -> report.sh's exit code; the picked interpreter lands in $RP_LOG
  : >"$RP_LOG"
  env -i PATH="$PATH" HOME="$T/home" AGENT_OPS_DOCKER_BIN="$AGENT_OPS_DOCKER_BIN" RP_LOG="$RP_LOG" \
    MAINT_DIR="$T/maint" AGENT_OPS_STATE_DIR="$T/st" AGENT_OPS_NO_SEND=1 \
    REPORT_CAPSULE_CORP_DIR="$REPO" "$@" bash "$REPO/maintenance/report.sh" >/dev/null 2>&1
}
run_rp CAPSULE_OPS_PY="$T/bin/py-new"; rc=$?
check "CAPSULE_OPS_PY alone is used" '[ $rc -eq 0 ] && [ "$(cat "$RP_LOG")" = py-new ]'
if [ -n "$OLD_PY" ]; then
run_rp CAPSULE_OPS_PY="$T/bin/py-new" "$OLD_PY=$T/bin/py-old"; rc=$?
check "CAPSULE_OPS_PY is used and the old variable is not read" '[ $rc -eq 0 ] && [ "$(cat "$RP_LOG")" = py-new ]'
run_rp "$OLD_PY=$T/bin/py-old"; rc=$?
check "the old variable alone has no effect" '[ $rc -eq 0 ] && ! grep -q py-old "$RP_LOG"'
run_rp CAPSULE_OPS_PY= "$OLD_PY=$T/bin/py-old"; rc=$?
check "empty CAPSULE_OPS_PY does not fall through to the old variable" '[ $rc -eq 0 ] && ! grep -q py-old "$RP_LOG"'
else
echo "SKIP old-variable cases: no private denylist (public checkout)"
fi
# PATH order: report.sh rebuilds PATH before sourcing lib_config.sh, so a broken python3 first on the
# caller's PATH must not stop host.json config_dir from being resolved (traced: no vault, so it exits early).
mkdir -p "$T/po/stub" "$T/po/chome" "$T/po/home"
printf '#!/bin/sh\nexit 3\n' >"$T/po/stub/python3"; chmod +x "$T/po/stub/python3"
printf '{"config_dir": "%s/po/cfg"}\n' "$T" >"$T/po/chome/host.json"
out_po="$(env -i PATH="$T/po/stub:$PATH" HOME="$T/po/home" CAPSULE_HOME="$T/po/chome" AGENT_OPS_STATE_DIR="$T/st" \
  AGENT_OPS_NO_SEND=1 REPORT_CAPSULE_CORP_DIR="$REPO" bash -x "$REPO/maintenance/report.sh" 2>&1)"
check "PATH order: a broken python3 first on the caller's PATH still resolves host.json config_dir" \
  'grep -qF "MAINT_DIR=$T/po/cfg/maintenance" <<<"$out_po"'
[ "$fails" -eq 0 ] || { echo "$fails failure(s)"; exit 1; }
echo "all passed"
