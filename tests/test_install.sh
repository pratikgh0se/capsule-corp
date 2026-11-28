#!/usr/bin/env bash
# install.sh is only a shim: it must refuse non-macOS, find a python3 of 3.11+ (or, with a yes, install
# one through uv), and exec `python3 -m capsule install` with every argument. Run against stubs only:
# uname, python3, brew and uv are fakes first on PATH, so no real brew, uv or python is ever
# reached, nothing is installed, and the real HOME is never touched. The installer itself is
# tested in capsule/tests/test_install.py.
set -u
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
fails=0
check() { if eval "$2"; then echo "PASS $1"; else echo "FAIL $1"; fails=$((fails+1)); fi; }

BIN="$T/bin"; NEWBIN="$T/uvpy"; LOG="$T/calls.log"; mkdir -p "$BIN" "$NEWBIN" "$T/home"
stub() { printf '#!/bin/sh\n%s\n' "$2" >"$1"; chmod +x "$1"; }
# python3: `-c` is the version probe (exit $PY_OLD); anything else is logged with PYTHONPATH.
PY='if [ "$1" = -c ]; then exit "${PY_OLD:-0}"; fi; echo "python3 PYTHONPATH=$PYTHONPATH $*" >>"$LOG"'
stub "$BIN/python3" "$PY"
stub "$NEWBIN/python" 'if [ "$1" = -c ]; then exit 0; fi; echo "uvpython PYTHONPATH=$PYTHONPATH $*" >>"$LOG"'
stub "$BIN/uname" 'echo "${FAKE_OS:-Darwin}"'
stub "$BIN/brew" 'echo "brew $*" >>"$LOG"'
stub "$BIN/uv" 'echo "uv $*" >>"$LOG"; [ "$1 $2" = "python find" ] && echo "'"$NEWBIN"'/python"; exit 0'
run() { : >"$LOG"; HOME="$T/home" PATH="$BIN:/usr/bin:/bin" LOG="$LOG" "$@"; }

check "install.sh is at most 50 lines" '[ "$(wc -l <"$REPO/install.sh")" -le 50 ]'

out="$(FAKE_OS=Linux run "$REPO/install.sh" --yes 2>&1)"; rc=$?
check "non-macOS exits non-zero" '[ $rc -ne 0 ]'
check "non-macOS prints the Linux pointer" 'grep -q "docs/SETUP.md" <<<"$out"'
check "non-macOS runs nothing" '[ ! -s "$LOG" ]'

run "$REPO/install.sh" --dry-run --capsule-home "$T/h" >/dev/null 2>&1
check "good python3: execs python3 -m capsule install with every argument" \
  'grep -q "^python3 PYTHONPATH=$REPO -P -m capsule install --dry-run --capsule-home $T/h\$" "$LOG"'

# CDPATH must not redirect the shim's own folder lookup: a decoy folder of the clone's name sits
# first on CDPATH, and the shim is started by a relative path so `cd` would consult it.
mkdir -p "$T/cd/$(basename "$REPO")"
(cd "$(dirname "$REPO")" && CDPATH="$T/cd" run "$(basename "$REPO")/install.sh" --dry-run) >/dev/null 2>&1
check "CDPATH set: PYTHONPATH is still the clone, not a CDPATH folder" \
  'grep -q "^python3 PYTHONPATH=$REPO -P -m capsule install --dry-run\$" "$LOG"'

out="$(PY_OLD=1 run "$REPO/install.sh" --dry-run 2>&1)"; rc=$?
check "old python3 + --dry-run: prints WOULD and exits 0" '[ $rc -eq 0 ] && grep -q "^WOULD brew install uv" <<<"$out"'
check "old python3 + --dry-run: installs and runs nothing" '[ ! -s "$LOG" ]'

out="$(PY_OLD=1 run "$REPO/install.sh" </dev/null 2>&1)"; rc=$?
check "old python3 + no answer: exits non-zero, installs nothing" '[ $rc -ne 0 ] && ! grep -q "^brew" "$LOG"'

out="$(PY_OLD=1 run "$REPO/install.sh" <<<"n" 2>&1)"; rc=$?
check "old python3 + answer n: exits non-zero, installs nothing" '[ $rc -ne 0 ] && ! grep -q "^brew" "$LOG"'

PY_OLD=1 run "$REPO/install.sh" --yes >/dev/null 2>&1
check "old python3 + --yes: brew install uv, uv python install 3.11" \
  'grep -q "^brew install uv\$" "$LOG" && grep -q "^uv python install 3.11\$" "$LOG"'
check "old python3 + --yes: execs the uv python with the same arguments" \
  'grep -q "^uvpython PYTHONPATH=$REPO -P -m capsule install --yes\$" "$LOG"'
check "the shim never calls deploy.sh, bootstrap.sh or launchctl" \
  '! grep -Eq "deploy|bootstrap|launchctl" "$LOG" && ! grep -v "^#" "$REPO/install.sh" | grep -Eq "deploy|bootstrap|launchctl"'
check "nothing was written under the temp HOME" '[ -z "$(ls -A "$T/home")" ]'

echo "install shim: $fails failure(s)"
[ "$fails" -eq 0 ]
