#!/usr/bin/env bash
# bin/lib_prefix.sh: the launchd prefix resolution (D261): env > host.json launchd.label_prefix > dev.capsule.
set -u
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
fails=0; check() { if eval "$2"; then echo "PASS $1"; else echo "FAIL $1"; fails=$((fails+1)); fi; }
# resolve <capsule home> [VAR=value ...]: the prefix a script gets with that home and environment
resolve() { env -u CAPSULE_LAUNCHD_PREFIX -u AGENT_OPS_NO_SEND CAPSULE_HOME="$1" CAPSULE_LA_DIR="$T/emptyla" "${@:2}" bash -c '. "$1/bin/lib_prefix.sh"; capsule_launchd_prefix' _ "$REPO" 2>"$T/err"; }
mkdir -p "$T/emptyla" "$T/none" "$T/hj" "$T/bad" "$T/la" "$T/aoh/.local/share/capsule"
LEG=ex.legacy
printf '{"launchd": {"label_prefix": "%s"}}\n' "$LEG" >"$T/hj/host.json"
printf '{"launchd": {"label_prefix": "%s"}}\n' "$LEG" >"$T/aoh/.local/share/capsule/host.json"
printf '{"launchd": {"label_prefix": "a b/c"}}\n' >"$T/bad/host.json"
touch "$T/la/com.example.capsule-corp-heal.plist"
check "no host.json, no variable: dev.capsule" '[ "$(resolve "$T/none")" = dev.capsule ]'
check "host.json launchd.label_prefix is honoured" '[ "$(resolve "$T/hj")" = "$LEG" ]'
check "the variable beats host.json" '[ "$(resolve "$T/hj" CAPSULE_LAUNCHD_PREFIX=x.y)" = x.y ]'
mkdir -p "$T/la3"; touch "$T/la3/com.example.capsule-corp-heal.plist" "$T/la3/$LEG.capsule-corp-heal.plist"
check "a host.json prefix with its own jobs installed is honoured next to other prefixes" '[ "$(resolve "$T/hj" CAPSULE_LA_DIR="$T/la3")" = "$LEG" ]'
resolve "$T/none" CAPSULE_LA_DIR="$T/la" >/dev/null; rc=$?
check "guard: no jobs under the resolved prefix but another prefix installed -> refused, names both" '[ $rc -ne 0 ] && grep -q "use prefix com.example, none use dev.capsule" "$T/err"'
mkdir -p "$T/la2"; touch "$T/la2/com.example.capsule-corp-heal.plist" "$T/la2/dev.capsule.capsule-corp-heal.plist"
check "guard: jobs under the resolved (host.json) prefix pass even with another prefix around" '[ "$(resolve "$T/hj" CAPSULE_LA_DIR="$T/la3")" = "$LEG" ]'
resolve "$T/none" CAPSULE_LA_DIR="$T/la2" >/dev/null; rc=$?
check "ambiguous: default prefix only (no env, no host.json) and two prefixes installed -> refused, prints nothing" \
  '[ $rc -ne 0 ] && grep -q "ambiguous" "$T/err" && [ -z "$(resolve "$T/none" CAPSULE_LA_DIR="$T/la2")" ]'
check "ambiguous: the env variable settles it" '[ "$(resolve "$T/none" CAPSULE_LA_DIR="$T/la2" CAPSULE_LAUNCHD_PREFIX=dev.capsule)" = dev.capsule ]'
check "guard: the env variable is explicit and skips it" '[ "$(resolve "$T/none" CAPSULE_LA_DIR="$T/la" CAPSULE_LAUNCHD_PREFIX=x.y)" = x.y ]'
mkdir -p "$T/stub"; printf '#!/bin/sh\nexit 3\n' >"$T/stub/python3"; chmod +x "$T/stub/python3"
resolve "$T/none" PATH="$T/stub:$PATH" >/dev/null; rc=$?
check "reader cannot run (python3 fails): refused, nothing printed, no dev.capsule guess" '[ $rc -ne 0 ] && [ -z "$(resolve "$T/none" PATH="$T/stub:$PATH")" ] && grep -q "cannot run" "$T/err"'
mkdir -p "$T/nomk"; printf '#!/bin/sh\nexit 1\n' >"$T/nomk/mktemp"; chmod +x "$T/nomk/mktemp"
check "mktemp fails: refused, nothing printed" '[ -z "$(resolve "$T/none" PATH="$T/nomk:$PATH")" ] && ! resolve "$T/none" PATH="$T/nomk:$PATH" >/dev/null && grep -q "mktemp failed" "$T/err"'
resolve "$T/bad" >/dev/null; rc=$?
check "an invalid host.json value is refused: nonzero, message" '[ $rc -ne 0 ] && grep -q "invalid launchd prefix" "$T/err"'
check "an invalid host.json value prints no prefix" '[ -z "$(resolve "$T/bad")" ]'
check "a prefix outside letters, digits, dot and dash is refused" '! resolve "$T/none" "CAPSULE_LAUNCHD_PREFIX=a/b c" >/dev/null && grep -q invalid "$T/err"'
for bad in "é" "ß" "Ａ"; do
  check "a non-ASCII prefix ($bad) is refused even under a UTF-8 locale" '! LC_ALL=en_US.UTF-8 resolve "$T/none" "CAPSULE_LAUNCHD_PREFIX=$bad" >/dev/null && grep -q invalid "$T/err"'
done
check "AGENT_OPS_HOME locates host.json in the installed layout" \
  '[ "$(env -u CAPSULE_LAUNCHD_PREFIX -u CAPSULE_HOME -u AGENT_OPS_NO_SEND AGENT_OPS_HOME="$T/aoh" bash -c ". \"$REPO/bin/lib_prefix.sh\"; capsule_launchd_prefix")" = "$LEG" ]'
# public names D: the ollama-serve label computed by setup-semantic.sh and bootstrap.sh is byte-identical to the old literals
TPL="$(basename "$(ls "$REPO"/launchd/templates/*.ollama-serve.plist | head -n 1)" .plist)"; OL="${TPL%.ollama-serve}"
mkdir -p "$T/ss/maintenance"; ln -s "$REPO/bin" "$T/ss/bin"; ln -s "$REPO/launchd" "$T/ss/launchd"
{ echo '#!/usr/bin/env bash'; echo 'HOME=/h'
  grep -E '^(\. .*lib_prefix|OLLAMA_LABEL=|PLIST=)' "$REPO/maintenance/setup-semantic.sh"
  echo 'echo "$PLIST"'; echo "cat <<PL"; grep -F '<key>Label</key>' "$REPO/maintenance/setup-semantic.sh"; echo PL
  grep -o 'gui/[^"]*OLLAMA_LABEL' "$REPO/maintenance/setup-semantic.sh" | sed 's/^/echo "/; s/$/"/'; } >"$T/ss/maintenance/x.sh"
ss="$(env CAPSULE_LAUNCHD_PREFIX="$OL" HOME="$T" bash "$T/ss/maintenance/x.sh" 2>&1)"
want="/h/Library/LaunchAgents/$OL.ollama-serve.plist
  <key>Label</key><string>$OL.ollama-serve</string>
gui/$(id -u)/$OL.ollama-serve"
check "setup-semantic: plist path, Label line and kickstart target match the template's label" '[ "$ss" = "$want" ]'
check "bootstrap.sh and setup-semantic.sh hard-code no template prefix" '! grep -qF "$OL." "$REPO/bootstrap.sh" "$REPO/maintenance/setup-semantic.sh"'
olabel() { env CAPSULE_LAUNCHD_PREFIX="$1" bash -c '. "$1/bin/lib_prefix.sh"; capsule_ollama_label "$2"' _ "$REPO" "$2" 2>"$T/oerr"; }
mkdir -p "$T/norepo" "$T/obs/launchd/templates" "$T/two/launchd/templates"
touch "$T/obs/launchd/templates/a.b.obsidian-x.plist" "$T/two/launchd/templates/p.q.ollama-serve.plist" "$T/two/launchd/templates/r.s.ollama-serve.plist"
check "ollama label: the literal template name wins over a different machine prefix" '[ "$(olabel x.y "$REPO")" = "$TPL" ]'
check "ollama label: no template -> machine prefix" '[ "$(olabel x.y "$T/norepo")" = x.y.ollama-serve ]'
check "ollama label: no ollama template -> the obsidian template prefix" '[ "$(olabel x.y "$T/obs")" = a.b.ollama-serve ]'
check "ollama label: two templates -> first one, warning on stderr" '[ "$(olabel x.y "$T/two")" = p.q.ollama-serve ] && grep -q WARNING "$T/oerr"'
check "ollama label: a failing prefix makes the helper fail with no label" 'out="$(olabel "a/b" "$T/norepo")"; rc=$?; [ $rc -ne 0 ] && [ -z "$out" ]'
# A stock Mac's python3 (3.9) has no -P and is too old for capsule: the tick verbs must still resolve the prefix
# through the interpreter the board's plist runs (CAPSULE_PY), however the PATH python3 behaves.
REALPY="$(command -v python3)"; mkdir -p "$T/old" "$T/tla" "$T/thome"
printf '#!/bin/sh\necho "python3: Unknown option: -P" >&2\nexit 2\n' >"$T/old/python3"; chmod +x "$T/old/python3"
printf '#!/bin/sh\nexit 1\n' >"$T/lctl"; chmod +x "$T/lctl"
printf '<?xml version="1.0" encoding="UTF-8"?>\n<plist version="1.0"><dict><key>Label</key><string>dev.capsule.capsule-tick-cc</string><key>ProgramArguments</key><array><string>%s</string></array></dict></plist>\n' "$REALPY" >"$T/tla/dev.capsule.capsule-tick-cc.plist"
printf '<?xml version="1.0" encoding="UTF-8"?>\n<plist version="1.0"><dict><key>Label</key><string>dev.capsule.capsule-tick-cc</string><key>ProgramArguments</key><array><string>/nonexistent/python3.13</string></array></dict></plist>\n' >"$T/tla/dev.capsule.capsule-tick-gone.plist"
tick() { env -u CAPSULE_LAUNCHD_PREFIX -u CAPSULE_PY -u AGENT_OPS_NO_SEND PATH="$T/old:$PATH" HOME="$T/thome" CAPSULE_HOME="$T/thome" CAPSULE_LA_DIR="$T/tla" CAPSULE_LAUNCHCTL="$T/lctl" bash "$REPO/bin/capsule-tick.sh" "$@" 2>"$T/terr"; }
tick status cc >"$T/tout"; rc=$?
check "tick status resolves the prefix with a PATH python3 that rejects -P (interpreter from the plist)" '[ $rc -eq 0 ] && grep -q "\"main_sha\"" "$T/tout"'
tick status gone >/dev/null; rc=$?
check "tick: a vanished plist interpreter is reported, nonzero" '[ $rc -ne 0 ] && grep -q "no longer exists" "$T/terr"'
check "reader: a python older than 3.11 on PATH and no CAPSULE_PY -> refused with the 3.11 hint" \
  '! env -u CAPSULE_LAUNCHD_PREFIX PATH="$T/old:$PATH" CAPSULE_HOME="$T/none" CAPSULE_LA_DIR="$T/emptyla" bash -c ". \"$REPO/bin/lib_prefix.sh\"; capsule_launchd_prefix" >/dev/null 2>"$T/err" && grep -q "3.11" "$T/err"'
echo; [ $fails -eq 0 ] && echo "test_lib_prefix: all passed" || { echo "test_lib_prefix: $fails failed"; exit 1; }
