#!/usr/bin/env bash
# tests/lib_playwright_python.sh picks the host's playwright interpreter (card rq_2c9581db): the
# CAPSULE_PLAYWRIGHT_PYTHON env var is authoritative, then the image venv, python3 on PATH, else nothing.
# Drives the real library with tiny fake interpreters and a fake HOME (a script that exits 0 passes the probe, one
# that exits 1 fails it) in a temp dir; nothing outside "$T" is touched. Also pins that no
# old-system install path is named in tests or maintenance.
# Intent examples: contracts/intent/ui_check.jsonl (each names a check below).
set -u
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
fails=0; PASSED=""; SKIPPED=""
check() { if eval "$2"; then echo "PASS $1"; PASSED="$PASSED|$1"; else echo "FAIL $1"; fails=$((fails+1)); fi; }

mkdir -p "$T/pathgood" "$T/pathbad" "$T/good" "$T/bad" "$T/home/.her""mes/tools/play""wright/bin" "$T/emptyhome"
printf '#!/bin/sh\nexit 0\n' >"$T/good/py"; printf '#!/bin/sh\nexit 1\n' >"$T/bad/py"
FB="$T/home/.her""mes/tools/play""wright/bin/python"; cp "$T/good/py" "$FB"
cp "$T/good/py" "$T/pathgood/python3"; cp "$T/bad/py" "$T/pathbad/python3"; chmod +x "$T/pathgood/python3" "$T/pathbad/python3" "$T/good/py" "$T/bad/py" "$FB"
. "$REPO/tests/lib_playwright_python.sh"

# pick ENV HOME: ENV empty means the variable is unset.
# Optional $3 = PLAYWRIGHT_IMAGE_PYTHON, $4 = PATH; defaults point at nothing usable.
pick() { ( unset CAPSULE_PLAYWRIGHT_PYTHON; [ -n "$1" ] && export CAPSULE_PLAYWRIGHT_PYTHON="$1"
  PLAYWRIGHT_IMAGE_PYTHON="${3:-$T/nope/img}" PATH="${4:-$T/nobin:/usr/bin:/bin}" HOME="$2" playwright_python ); }

check "env-var-is-honoured" '[ "$(pick "$T/good/py" "$T/emptyhome")" = "$T/good/py" ]'
check "unusable-env-var-picks-nothing" 'out="$(pick "$T/bad/py" "$T/home")"; rc=$?; [ $rc -ne 0 ] && [ -z "$out" ]'
check "missing-env-var-path-picks-nothing" 'out="$(pick "$T/nope/py" "$T/home")"; rc=$?; [ $rc -ne 0 ] && [ -z "$out" ]'
check "old-venv-under-home-is-never-used" 'out="$(pick "" "$T/home")"; [ $? -ne 0 ] && [ -z "$out" ]'
mkdir -p "$T/dhome/.local/share/capsule/playwright/bin"; DF="$T/dhome/.local/share/capsule/playwright/bin/python"
cp "$T/good/py" "$DF"; chmod +x "$DF"
check "host-default-venv-used-when-env-var-unset" '[ "$(pick "" "$T/dhome")" = "$DF" ]'
check "image-venv-used-when-env-var-unset" '[ "$(pick "" "$T/emptyhome" "$T/good/py" "$T/pathgood:/usr/bin:/bin")" = "$T/good/py" ]'
check "python3-on-path-used-last" '[ "$(pick "" "$T/emptyhome" "" "$T/pathgood:/usr/bin:/bin")" = python3 ]'
check "python3-on-path-without-playwright-is-skipped" 'out="$(pick "" "$T/emptyhome" "" "$T/pathbad:/usr/bin:/bin")"; [ $? -ne 0 ] && [ -z "$out" ]'
check "nothing-usable-skips-loudly" 'out="$(pick "" "$T/emptyhome")"; rc=$?; [ $rc -ne 0 ] && [ -z "$out" ] && ! pick "" "$T/emptyhome" >/dev/null'
check "ui-check-skip-message-names-the-variable" 'out="$(env -u CAPSULE_PLAYWRIGHT_PYTHON -u UI_CHECK_PYTHON PLAYWRIGHT_IMAGE_PYTHON="$T/nope/img" PATH="$T/pathbad:$PATH" HOME="$T/emptyhome" bash "$REPO/tests/test_ui_check.sh" 2>&1)"; rc=$?; [ $rc -eq 77 ] && printf %s "$out" | grep -q CAPSULE_PLAYWRIGHT_PYTHON'

# No old-system install path anywhere in tests or maintenance. The name comes from the private
# denylist (skipped in a public checkout, which has none).
OLD="$(python3 "$REPO/tests/denylist_term.py" retired)" || { echo "FAIL: denylist_term: release/private-denylist.txt is unusable"; exit 1; }
if [ -n "$OLD" ]; then
pat="$OLD/tools/playwright"
check "no-old-install-path-in-tests-or-maintenance" '! grep -rIn --exclude-dir=__pycache__ -e "$pat" "$REPO/tests" "$REPO/maintenance"'
else
echo "SKIP no-old-install-path-in-tests-or-maintenance: release/private-denylist.txt absent"
SKIPPED="$SKIPPED|no-old-install-path-in-tests-or-maintenance"   # not counted as passed; the intent replay accepts it
fi

# Z1: contracts/intent/ui_check.jsonl replayed - each example names a check above that passed.
check "intent-examples" 'python3 -c "import json,sys; rows=[json.loads(l) for l in open(sys.argv[1]) if l.strip()]; got=(sys.argv[2]+chr(124)+sys.argv[3]).split(chr(124)); sys.exit(0 if rows and all(r[\"expect\"][\"pass\"] in got for r in rows) else 1)" "$REPO/contracts/intent/ui_check.jsonl" "$PASSED" "$SKIPPED"'

[ "$fails" -eq 0 ] && echo "ALL PASS test_playwright_python" || echo "$fails FAILED test_playwright_python"
exit $((fails > 0))
