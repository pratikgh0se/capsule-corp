#!/usr/bin/env bash
# lib_compose.sh: two-phase bring-up. Phase 1 (infisical trio) from the env
# file only; phase 2 (everything) under capsule-secret run as identity
# compose; stop at the first failure; no secret ever on the docker argv.
set -u
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
fails=0; check() { if eval "$2"; then echo "PASS $1"; else echo "FAIL $1"; fails=$((fails+1)); fi; }
FAKEBIN="$T/bin"; mkdir -p "$FAKEBIN"; ARGV="$T/argv.log"
cat >"$FAKEBIN/docker" <<EOF
#!/usr/bin/env bash
echo "docker \$* [CS=\${CAPSULE_SECRET_RAN:-}]" >>"$ARGV"
# 'config --services' is what compose_services asks for: real compose answers
# with the services only, never the top-level volumes/networks maps.
case "\$*" in *"config --services"*)
  [ "\${CONFIG_FAIL:-}" = "1" ] && { echo "compose: no configuration file provided" >&2; exit 1; }
  printf 'infisical-db\ninfisical-redis\ninfisical\ngrafana\n'; exit 0 ;;
esac
[ "\${DOCKER_FAIL:-}" = "1" ] && exit 1; exit 0
EOF
cat >"$FAKEBIN/curl" <<EOF
#!/usr/bin/env bash
echo "curl \$*" >>"$ARGV"; [ "\${CURL_FAIL:-}" = "1" ] && exit 22; echo '{}'
EOF
cat >"$FAKEBIN/capsule-secret" <<EOF
#!/usr/bin/env bash
echo "capsule-secret \$* [ID=\$CAPSULE_IDENTITY]" >>"$ARGV"
[ "\${CS_FAIL:-}" = "1" ] && { echo "capsule-secret: login failed" >&2; exit 3; }
shift 2; [ "\$1" = "--" ] && shift; CAPSULE_SECRET_RAN=1 "\$@"
EOF
chmod +x "$FAKEBIN"/*
export COMPOSE_PROJECT_NAME=capsule-corp-test   # temp OBS_ENV + fake docker: never the live project (compose_live_guard)
export PATH="$FAKEBIN:$PATH" OBS_ENV="$T/obs.env" COMPOSE_FILE="$T/dc.yml" CAPSULE_SECRET_BIN="$FAKEBIN/capsule-secret" COMPOSE_WAIT_STEP_S=0 COMPOSE_WAIT_S=1
: >"$OBS_ENV"
cat >"$COMPOSE_FILE" <<'EOF'
services:
  infisical-db:
    image: postgres:16
  infisical-redis:
    image: redis
  infisical:
    image: infisical
  grafana:
    image: grafana/grafana
volumes:
  data:
EOF
. "$REPO/maintenance/lib_compose.sh"

: >"$ARGV"; out="$(compose_up_all 2>&1)"; rc=$?
check "all phases ok" '[ $rc -eq 0 ] && grep -q "compose: infisical ok" <<<"$out" && grep -q "compose: wait ok" <<<"$out" && grep -q "compose: rest ok" <<<"$out"'
check "phase 1 starts only the trio, no helper" 'grep -q "^docker compose --env-file $OBS_ENV -f $COMPOSE_FILE up -d --quiet-pull infisical-db infisical-redis infisical \[CS=\]$" "$ARGV"'
check "phase 3 runs docker under capsule-secret as compose" 'grep -q "^capsule-secret run capsule-corp -- docker compose --env-file $OBS_ENV -f $COMPOSE_FILE up -d --quiet-pull \[ID=compose\]$" "$ARGV" && grep -q "up -d --quiet-pull \[CS=1\]$" "$ARGV"'
check "phase order" '[ "$(grep -n "^docker compose\|^curl" "$ARGV" | cut -d: -f2 | awk "{print \$1}" | tr "\n" " ")" = "docker curl docker " ]'

: >"$ARGV"; out="$(DOCKER_FAIL=1 compose_up_all 2>&1)"; rc=$?
check "phase 1 failure stops everything" '[ $rc -eq 1 ] && grep -q "compose: infisical failed" <<<"$out" && ! grep -q "curl" "$ARGV"'
: >"$ARGV"; out="$(CURL_FAIL=1 compose_up_all 2>&1)"; rc=$?
check "wait timeout stops before phase 3" '[ $rc -eq 1 ] && grep -q "compose: wait failed (infisical not answering after 1s)" <<<"$out" && ! grep -q "capsule-secret" "$ARGV"'
: >"$ARGV"; out="$(CS_FAIL=1 compose_up_all 2>&1)"; rc=$?
check "helper failure fails phase 3 with its line" '[ $rc -eq 1 ] && grep -q "compose: rest failed" <<<"$out"'

# compose_services asks compose, not the YAML: the fixture's top-level
# `volumes: data:` line matches the pattern the old greps used and must not
# come back as a service.
: >"$ARGV"; svc="$(compose_services)"; rc=$?
check "compose_services asks compose itself" '[ $rc -eq 0 ] && grep -q "^docker compose --env-file $OBS_ENV -f $COMPOSE_FILE config --services \[CS=\]$" "$ARGV"'
check "compose_services lists every service and no phantom volume key" \
  '[ "$(printf "%s\n" "$svc" | tr "\n" " ")" = "infisical-db infisical-redis infisical grafana " ] && ! grep -qx data <<<"$svc"'
# Fail closed: callers must be able to tell "no services" from "could not
# ask". compose's own stderr is swallowed (noise, not an answer) but its exit
# status is the function's.
out="$(CONFIG_FAIL=1 compose_services 2>&1)"; rc=$?
check "compose_services returns compose's failure status" '[ $rc -ne 0 ]'
check "compose_services swallows compose's stderr but not its status" '[ -z "$out" ]'
# W334: deploy.sh links the config-dir maintenance entry to the
# repo; sourced through that link the default helper path must resolve to the
# repo's bin/, not to a sibling bin/ of the link's parent (heal exited 1 on it).
LINKDIR="$T/deployed"; mkdir -p "$LINKDIR"; ln -s "$REPO/maintenance" "$LINKDIR/maintenance"
via_link="$(unset CAPSULE_SECRET_BIN; COMPOSE_FILE="$T/dc.yml"; . "$LINKDIR/maintenance/lib_compose.sh"; echo "$CAPSULE_SECRET_BIN")"
check "default helper resolves to the repo bin through the deployed symlink" '[ "$via_link" = "$(cd -P "$REPO" && pwd -P)/bin/capsule-secret" ]'
echo; if [ $fails -eq 0 ]; then echo "test_lib_compose: all passed"; else echo "test_lib_compose: $fails failed"; exit 1; fi
