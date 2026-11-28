#!/usr/bin/env bash
# maintenance/lib_config.sh: the jobs config dir: env CAPSULE_JOBS_DIR > host.json config_dir >
# ${AGENT_OPS_HOME:-$HOME}/.config/capsule-corp. Each case runs in a scrubbed shell with its own home.
set -u
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
fails=0; check() { if eval "$2"; then echo "PASS $1"; else echo "FAIL $1"; fails=$((fails+1)); fi; }
# resolve <home> [VAR=value ...]: the dir a script gets after sourcing the lib
resolve() { env -u CAPSULE_JOBS_DIR -u AGENT_OPS_HOME HOME="$1" CAPSULE_HOME="$1/.local/share/capsule" "${@:2}" \
  bash -c '. "$1/maintenance/lib_config.sh"; printf "%s" "$CAPSULE_JOBS_DIR"' _ "$REPO" 2>/dev/null; }
mkdir -p "$T/plain" "$T/set/.local/share/capsule" "$T/bad/.local/share/capsule" "$T/aoh/.local/share/capsule"
printf '{"config_dir": "%s/jobs-dir"}\n' "$T" >"$T/set/.local/share/capsule/host.json"
printf '{"config_dir": "relative"}\n' >"$T/bad/.local/share/capsule/host.json"
printf '{"config_dir": "%s/from-aoh"}\n' "$T" >"$T/aoh/.local/share/capsule/host.json"
check "nothing set: the default under the home" '[ "$(resolve "$T/plain")" = "$T/plain/.config/capsule-corp" ]'
check "host.json config_dir is honoured" '[ "$(resolve "$T/set")" = "$T/jobs-dir" ]'
check "the variable beats host.json" '[ "$(resolve "$T/set" CAPSULE_JOBS_DIR="$T/env-dir")" = "$T/env-dir" ]'
check "a bad host.json value gives the default" '[ "$(resolve "$T/bad")" = "$T/bad/.config/capsule-corp" ]'
check "AGENT_OPS_HOME reads that home's host.json, not the real one" \
  '[ "$(resolve "$T/plain" AGENT_OPS_HOME="$T/aoh")" = "$T/from-aoh" ]'
check "AGENT_OPS_HOME with no host.json: its default" \
  '[ "$(resolve "$T/set" AGENT_OPS_HOME="$T/plain")" = "$T/plain/.config/capsule-corp" ]'
check "the dir is exported to children" \
  '[ "$(env -u CAPSULE_JOBS_DIR HOME="$T/plain" bash -c ". \"$REPO/maintenance/lib_config.sh\"; bash -c \"printf %s \\\$CAPSULE_JOBS_DIR\"")" = "$T/plain/.config/capsule-corp" ]'
check "an inherited CAPSULE_HOME does not beat AGENT_OPS_HOME" \
  '[ "$(resolve "$T/plain" CAPSULE_HOME="$T/set/.local/share/capsule" AGENT_OPS_HOME="$T/aoh")" = "$T/from-aoh" ]'
# stderr_of <home> [VAR=value ...]: what sourcing the lib prints on stderr
stderr_of() { env -u CAPSULE_JOBS_DIR -u CAPSULE_HOME -u AGENT_OPS_HOME HOME="$1" "${@:2}" \
  bash -c '. "$1/maintenance/lib_config.sh"' _ "$REPO" 2>&1 >/dev/null; }
mkdir -p "$T/stub"; printf '#!/bin/sh\nexit 3\n' >"$T/stub/python3"; chmod +x "$T/stub/python3"
warn="$(stderr_of "$T/set" PATH="$T/stub:$PATH")"
check "a failing lookup warns with one line" '[ "$(wc -l <<<"$warn" | tr -d " ")" = 1 ] && grep -q "config_dir lookup failed" <<<"$warn"'
check "a failing lookup still gives the default dir" \
  '[ "$(resolve "$T/set" PATH="$T/stub:$PATH")" = "$T/set/.config/capsule-corp" ]'
check "a key that is merely unset does not warn" '[ -z "$(stderr_of "$T/plain")" ]'
[ "$fails" -eq 0 ] && echo "test_lib_config: all passed" || { echo "test_lib_config: $fails failed"; exit 1; }
