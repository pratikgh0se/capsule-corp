#!/usr/bin/env bash
# bin/capsule-compose-recreate: sets COMPOSE_FILE itself, runs the recreate
# under capsule-secret as identity compose, and reports failures.
set -u
REPO="$(cd -P "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
WRAP="$REPO/bin/capsule-compose-recreate"
T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
fails=0; check() { if eval "$2"; then echo "PASS $1"; else echo "FAIL $1"; fails=$((fails+1)); fi; }
FAKEBIN="$T/bin"; mkdir -p "$FAKEBIN"; ARGV="$T/argv.log"
cat >"$FAKEBIN/docker" <<EOF
#!/usr/bin/env bash
echo "docker \$*" >>"$ARGV"; exit 0
EOF
cat >"$FAKEBIN/capsule-secret" <<EOF
#!/usr/bin/env bash
[ "\${1:-}" = backend ] && { echo infisical; exit 0; }
echo "capsule-secret \$* [ID=\$CAPSULE_IDENTITY]" >>"$ARGV"
[ "\${CS_FAIL:-}" = "1" ] && { echo "capsule-secret: login failed" >&2; exit 3; }
shift 2; [ "\$1" = "--" ] && shift; "\$@"
EOF
chmod +x "$FAKEBIN"/*
export COMPOSE_PROJECT_NAME=capsule-corp-test   # temp OBS_ENV + fake docker: never the live project (compose_live_guard)
export OBS_ENV="$T/obs.env" CAPSULE_SECRET_BIN="$FAKEBIN/capsule-secret" DOCKER_BIN="$FAKEBIN/docker"
unset COMPOSE_FILE

: >"$ARGV"; out="$(bash "$WRAP" grafana 2>&1)"; rc=$?
check "default COMPOSE_FILE, identity compose, force-recreate" '[ $rc -eq 0 ] && [ -z "$out" ] && grep -q "^capsule-secret run capsule-corp -- $FAKEBIN/docker compose --env-file $OBS_ENV -f $REPO/observability/docker-compose.yml up -d --force-recreate grafana \[ID=compose\]$" "$ARGV"'
: >"$ARGV"; out="$(COMPOSE_FILE=/foreign/compose.yml bash "$WRAP" grafana 2>&1)"; rc=$?
check "foreign COMPOSE_FILE is ignored" '[ $rc -eq 0 ] && grep -q -- "-f $REPO/observability/docker-compose.yml " "$ARGV" && ! grep -q foreign "$ARGV"'
out="$(CS_FAIL=1 bash "$WRAP" grafana 2>&1)"; rc=$?
check "failure prints first stderr line and exits 1" '[ $rc -eq 1 ] && [ "$out" = "capsule-secret: login failed" ]'
out="$(bash "$WRAP" 2>&1)"; rc=$?
check "no service is a usage error" '[ $rc -eq 2 ] && grep -q usage <<<"$out"'
if command -v zsh >/dev/null 2>&1; then
  : >"$ARGV"; zsh -fc "\"$WRAP\" grafana" >/dev/null 2>&1; rc=$?
  check "runs from zsh" '[ $rc -eq 0 ] && grep -q "force-recreate grafana" "$ARGV"'
fi

echo; if [ $fails -eq 0 ]; then echo "test_capsule_compose_recreate: all passed"; else echo "test_capsule_compose_recreate: $fails failed"; exit 1; fi
