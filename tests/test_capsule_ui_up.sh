#!/usr/bin/env bash
# bin/capsule-ui-up: brings the stack up through compose, waits for health, prints the URLs.
# Fake docker, capsule-secret and curl only; no Docker, no network.
set -u
REPO="$(cd -P "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
BIN="$REPO/bin/capsule-ui-up"
T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
fails=0; check() { if eval "$2"; then echo "PASS $1"; else echo "FAIL $1"; fails=$((fails+1)); fi; }
FAKEBIN="$T/bin"; mkdir -p "$FAKEBIN"; ARGV="$T/argv.log"
cat >"$FAKEBIN/docker" <<EOT
#!/usr/bin/env bash
echo "docker \$*" >>"$ARGV"; exit 0
EOT
cat >"$FAKEBIN/capsule-secret" <<EOT
#!/usr/bin/env bash
[ "\${1:-}" = backend ] && { echo file; exit 0; }
[ "\${CS_FAIL:-}" = "1" ] && { echo "port is already allocated" >&2; exit 3; }
shift 2; [ "\$1" = "--" ] && shift; "\$@"
EOT
cat >"$FAKEBIN/curl" <<EOT
#!/usr/bin/env bash
echo "curl \$*" >>"$ARGV"; [ "\${CURL_FAIL:-}" = "1" ] && exit 7; exit 0
EOT
chmod +x "$FAKEBIN"/*
export COMPOSE_PROJECT_NAME=capsule-corp-test   # fake docker + temp OBS_ENV: never the live project
export OBS_ENV="$T/obs.env" CAPSULE_SECRET_BIN="$FAKEBIN/capsule-secret" DOCKER_BIN="$FAKEBIN/docker" CURL_BIN="$FAKEBIN/curl"
export COMPOSE_WAIT_S=2 COMPOSE_WAIT_STEP_S=0
: >"$OBS_ENV"
unset COMPOSE_FILE COMPOSE_PROFILES CC_CADDY_SECRETS

: >"$ARGV"; out="$(bash "$BIN" 2>&1)"; rc=$?
check "happy path: rc 0, compose up, health probed, grafana URL printed" '[ $rc -eq 0 ] && grep -q "docker compose --env-file $OBS_ENV -f $REPO/observability/docker-compose.yml up -d --quiet-pull$" "$ARGV" && grep -q "Host: cc.grafana.localhost" "$ARGV" && grep -qx "http://cc.grafana.localhost" <<<"$out" && ! grep -q secrets.localhost <<<"$out"'
out="$(COMPOSE_PROFILES=secrets bash "$BIN" 2>&1)"; rc=$?
check "secrets profile on also prints the secrets UI URL" '[ $rc -eq 0 ] && grep -qx "http://cc.secrets.localhost" <<<"$out"'
out="$(CS_FAIL=1 bash "$BIN" 2>&1)"; rc=$?
check "compose failure: rc 1, clear message, no URL" '[ $rc -eq 1 ] && grep -q "the stack did not start" <<<"$out" && grep -q "port is already allocated" <<<"$out" && ! grep -q "cc.grafana.localhost" <<<"$out"'
out="$(CURL_FAIL=1 bash "$BIN" 2>&1)"; rc=$?
check "health never answers: rc 1, clear message, no URL" '[ $rc -eq 1 ] && grep -q "did not answer" <<<"$out" && ! grep -q "^http://" <<<"$out"'
: >"$ARGV"; out="$(bash "$BIN" --dry-run 2>&1)"; rc=$?
check "dry-run prints the wrapped compose command, the wait and the URL, runs nothing" '[ $rc -eq 0 ] && grep -q "^would run: $FAKEBIN/capsule-secret run capsule-corp -- $FAKEBIN/docker compose --env-file $OBS_ENV -f $REPO/observability/docker-compose.yml up -d --quiet-pull$" <<<"$out" && grep -q "would wait for" <<<"$out" && grep -qx "http://cc.grafana.localhost" <<<"$out" && [ ! -s "$ARGV" ]'
out="$(COMPOSE_PROFILES=secrets bash "$BIN" --dry-run 2>&1)"; rc=$?
check "dry-run with the secrets profile also shows that phase and the secrets UI" '[ $rc -eq 0 ] && [ "$(grep -c "^would run: .*docker compose" <<<"$out")" -eq 2 ] && grep -q "^would run: $FAKEBIN/docker compose .* up -d --quiet-pull .*db" <<<"$out" && grep -qx "http://cc.secrets.localhost" <<<"$out" && [ ! -s "$ARGV" ]'
out="$(OBS_ENV="$T/nope.env" bash "$BIN" 2>&1)"; rc=$?
check "missing env file: rc 1 with a hint, no compose run" '[ $rc -eq 1 ] && grep -q "env.template" <<<"$out" && [ ! -s "$ARGV" ]'
out="$(bash "$BIN" --bogus 2>&1)"; rc=$?
check "unknown flag is a usage error" '[ $rc -eq 2 ] && grep -q usage <<<"$out"'

echo; if [ $fails -eq 0 ]; then echo "test_capsule_ui_up: all passed"; else echo "test_capsule_ui_up: $fails failed"; exit 1; fi
