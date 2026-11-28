#!/usr/bin/env bash
# t_6bb2badd: maintenance/grafana_prune_folders.sh never prints FAILED for a provisioned folder
# ("Capsule Corp" holds the file-provisioned alert rules; Grafana refuses to delete it), and still
# fails loudly on a real error. Uses the same stub curl as tests/test_front_door.sh; no live Grafana.
set -u
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
fails=0
check() { if eval "$2"; then echo "PASS $1"; else echo "FAIL $1"; fails=$((fails+1)); fi; }
T="$(mktemp -d)"
# run MODE: runs the script against the stub in that PRUNE_MODE; sets OUT, RC and the log $T/MODE.
run() {
  OUT="$(PRUNE_MODE="$1" PRUNE_LOG="$T/$1" CURL_BIN="$REPO/tests/fixtures/prune_curl_stub.sh" \
    CAPSULE_SECRET_BIN="$REPO/tests/fixtures/prune_secret_stub.sh" GRAFANA_URL=http://stub \
    "$REPO/maintenance/grafana_prune_folders.sh" 2>&1)"; RC=$?
}

run rules
check "provisioned alert rules: kept (provisioned), no DELETE of the folder, exit 0, no FAILED" \
  '[ $RC -eq 0 ] && ! grep -q "DELETE.*api/folders/empty3" "$T/rules" && grep -q "DELETE.*api/folders/empty1" "$T/rules" &&
   printf "%s" "$OUT" | grep -q "Capsule Corp. kept (provisioned)" && ! printf "%s" "$OUT" | grep -q FAILED'
run managed
check "folder managed by file provisioning: kept (provisioned), no DELETE of it, no FAILED" \
  '[ $RC -eq 0 ] && ! grep -q "DELETE.*api/folders/empty3" "$T/managed" &&
   printf "%s" "$OUT" | grep -q "Capsule Corp. kept (provisioned)" && ! printf "%s" "$OUT" | grep -q FAILED'
run delete-prov
check "Grafana refuses the delete as provisioned: kept (provisioned), exit 0, no FAILED" \
  '[ $RC -eq 0 ] && printf "%s" "$OUT" | grep -q "Capsule Corp. kept (provisioned)" &&
   ! printf "%s" "$OUT" | grep -q FAILED'
run delete-real
check "a real delete error still prints FAILED with the status and exits non-zero" \
  '[ $RC -ne 0 ] && printf "%s" "$OUT" | grep -q "FAILED delete of .Capsule Corp. failed (HTTP 500)" &&
   ! printf "%s" "$OUT" | grep -q "kept (provisioned)"'
run rules-err
check "alert-rule lookup failing is a FAILED line, exit non-zero, nothing deleted" \
  '[ $RC -ne 0 ] && ! grep -q DELETE "$T/rules-err" && printf "%s" "$OUT" | grep -q "prune: FAILED"'
run default
check "default run is unchanged: empty folders deleted, exit 0" \
  '[ $RC -eq 0 ] && printf "%s" "$OUT" | grep -q "Capsule Corp. deleted"'
find "$T" -delete
[ "$fails" -eq 0 ]
