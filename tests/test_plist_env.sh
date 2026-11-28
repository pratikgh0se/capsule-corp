#!/usr/bin/env bash
# deploy.sh plist_env: every rendered plist carries CAPSULE_JOBS_DIR in its EnvironmentVariables
# (launchd's system python3 cannot resolve it at run time). Two oracles: plistlib on the result and
# the raw text. Only the function is taken from deploy.sh; deploy.sh itself is never run here.
set -u
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
fails=0; check() { if eval "$2"; then echo "PASS $1"; else echo "FAIL $1"; fails=$((fails+1)); fi; }
sed -n '/^plist_env() {/,/^}/p' "$REPO/deploy.sh" >"$T/fn.sh"
. "$T/fn.sh"
CAPSULE_JOBS_DIR="$T/jobs dir"
value() { python3 -c 'import plistlib,sys; d=plistlib.loads(sys.stdin.buffer.read()); print(d.get("EnvironmentVariables",{}).get("CAPSULE_JOBS_DIR",""))'; }
n=0; bad=0
for f in "$REPO"/launchd/templates/*.plist; do
  n=$((n+1))
  got="$(sed -e 's#__[A-Z_]*__#x#g' "$f" | plist_env | value 2>/dev/null)"
  [ "$got" = "$CAPSULE_JOBS_DIR" ] || { bad=$((bad+1)); echo "  no value: ${f##*/}"; }
done
check "all $n shipped plists render with CAPSULE_JOBS_DIR in EnvironmentVariables" '[ "$bad" -eq 0 ] && [ "$n" -gt 10 ]'
printf '<plist version="1.0">\n<dict>\n  <key>Label</key><string>a</string>\n</dict>\n</plist>\n' >"$T/none.plist"
check "a plist with no EnvironmentVariables gets one" \
  '[ "$(plist_env <"$T/none.plist" | value)" = "$CAPSULE_JOBS_DIR" ] && [ "$(plist_env <"$T/none.plist" | grep -c EnvironmentVariables)" = 1 ]'
printf '<plist version="1.0">\n<dict>\n  <key>EnvironmentVariables</key>\n  <dict>\n    <key>A</key><string>b</string>\n  </dict>\n</dict>\n</plist>\n' >"$T/multi.plist"
check "existing variables are kept" \
  '[ "$(plist_env <"$T/multi.plist" | python3 -c "import plistlib,sys; print(plistlib.loads(sys.stdin.buffer.read())[\"EnvironmentVariables\"][\"A\"])")" = b ]'
[ "$fails" -eq 0 ] && echo "test_plist_env: all passed" || { echo "test_plist_env: $fails failed"; exit 1; }
