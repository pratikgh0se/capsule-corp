#!/usr/bin/env bash
# tests/test_report_vault.sh - report.sh takes the vault from the host setting (D254): the env var,
# else host.json vault.path, else none (report.sh then exits 0 before it does anything else).
# Fixtures live in a temp dir; no real vault. Only the no-vault run executes report.sh itself,
# because that one stops at the guard; the set cases test the shared resolver it sources.
set -u
unset CAPSULE_JOBS_DIR   # the jobs config dir comes from each case's fake home
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
T="$(mktemp -d "${TMPDIR:-/tmp}/report-vault.XXXXXX")"; trap 'rm -rf "$T"' EXIT
fails=0
check() { if eval "$2"; then echo "PASS $1"; else echo "FAIL $1"; fails=$((fails+1)); fi; }

# A fake docker, found through AGENT_OPS_DOCKER_BIN (report.sh rebuilds PATH): no real docker ever runs.
mkdir -p "$T/dockerstub"; printf '#!/bin/sh\nexit 0\n' >"$T/dockerstub/docker"; chmod +x "$T/dockerstub/docker"
export AGENT_OPS_DOCKER_BIN="$T/dockerstub"
mkdir -p "$T/home" "$T/chome" "$T/emptyhome" "$T/maint"
printf '{"vault": {"path": "%s"}}\n' "$T/hostvault" > "$T/chome/host.json"
printf '{}\n' > "$T/emptyhome/host.json"

check "report.sh sources the resolver and has the no-vault guard" \
  'grep -q "lib_vault.sh" "$REPO/maintenance/report.sh" && grep -q "no notes vault configured" "$REPO/maintenance/report.sh" && ! grep -qE "second-brain\}|CodeBase/obsidian" "$REPO/maintenance/report.sh"'

out="$(env -u OBSIDIAN_VAULT_PATH HOME="$T/home" CAPSULE_HOME="$T/emptyhome" REPORT_CAPSULE_CORP_DIR="$REPO" \
  MAINT_DIR="$T/maint" AGENT_OPS_STATE_DIR="$T/state" AGENT_OPS_NO_SEND=1 \
  bash "$REPO/maintenance/report.sh" 2>&1)"; rc=$?
check "neither set: report.sh exits 0 and says no vault is configured" \
  '[ $rc -eq 0 ] && grep -q "no notes vault configured" <<<"$out"'

got() { env -u OBSIDIAN_VAULT_PATH CAPSULE_HOME="$T/chome" "$@" \
  bash -c '. "$1"; vault_resolve "$2"; echo "$VAULT|$(printenv OBSIDIAN_VAULT_PATH)"' _ "$REPO/maintenance/lib_vault.sh" "$REPO"; }
check "env var wins over host.json" '[ "$(got OBSIDIAN_VAULT_PATH=/e/v)" = "/e/v|/e/v" ]'
check "host.json vault.path is used when the env var is unset" '[ "$(got)" = "$T/hostvault|$T/hostvault" ]'
check "neither set: empty and not exported" '[ "$(got CAPSULE_HOME="$T/emptyhome")" = "|" ]'

[ "$fails" -eq 0 ] && echo "test_report_vault.sh: all passed" || echo "test_report_vault.sh: $fails failed"
exit "$fails"
