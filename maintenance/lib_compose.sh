#!/usr/bin/env bash
# lib_compose.sh - the one way the observability Compose stack comes up
# (spec 2026-09-08-secrets-cutover-design.md section 4). Sourced by deploy.sh
# and heal.sh. Two phases because Infisical lives in the same compose file
# and cannot serve its own bootstrap: the trio starts from the env file,
# then everything starts under `capsule-secret run` as identity `compose`.
# Each phase prints one line and returns 0/1; compose_up_all stops at the
# first failure. No secret ever appears on a docker argv.
if [ -z "${OBS_ENV:-}" ]; then . "$(dirname "${BASH_SOURCE[0]}")/lib_config.sh"; OBS_ENV="$CAPSULE_JOBS_DIR/observability.env"; fi
: "${COMPOSE_FILE:?lib_compose: set COMPOSE_FILE}"
: "${CAPSULE_SECRET_BIN:=$(cd -P "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)/bin/capsule-secret}"
: "${INFISICAL_STATUS_URL:=http://localhost:8085/api/status}"
: "${COMPOSE_WAIT_S:=60}"
: "${COMPOSE_WAIT_STEP_S:=2}"
: "${DOCKER_BIN:=docker}"
: "${CURL_BIN:=curl}"

# Infisical, its db and redis sit under compose profile "secrets" (P06) and the
# cc.secrets.localhost Caddy route comes with them. Only the infisical backend turns
# them on, so a machine on it (the owner's) behaves exactly as before; an explicit
# COMPOSE_PROFILES / CC_CADDY_SECRETS from the caller wins.
if [ -z "${COMPOSE_PROFILES+x}" ]; then
  if lc_backend="$("$CAPSULE_SECRET_BIN" backend 2>/dev/null)"; then
    if [ "$lc_backend" = infisical ]; then COMPOSE_PROFILES=secrets; else COMPOSE_PROFILES=; fi
  else
    # A failure must never map to off (the old behaviour was infisical always up).
    echo "lib_compose: capsule-secret backend failed; keeping the secrets profile on" >&2
    COMPOSE_PROFILES=secrets
  fi
fi
if [ -z "${CC_CADDY_SECRETS:-}" ] && [ "$COMPOSE_PROFILES" = secrets ]; then CC_CADDY_SECRETS=/etc/caddy/routes/secrets.caddy; fi
export COMPOSE_PROFILES
[ -z "${CC_CADDY_SECRETS:-}" ] || export CC_CADDY_SECRETS

# compose_live_guard - the live project must never be driven from a non-live OBS_ENV.
# observability/docker-compose.yml fixes `name: capsule-corp` and every container_name,
# so a compose up with a temp OBS_ENV (DATA_DIR, CAPSULE_HOME, MAINT_LOGS all come from
# it) recreates the LIVE containers on empty data dirs (inc_02b0bd3a, twice 2026-10-28).
# Refuses (message on stdout, rc 1) unless the directory of OBS_ENV is the passwd home's
# the config dir (cd -P both sides; OBS_ENV unset or its dir missing refuses).
# The passwd home comes from the OS, not $HOME, so a test's HOME override cannot fake it;
# a failed lookup refuses. CAPSULE_TEST_COMPOSE_REAL_HOME is the test-only seam for the
# lookup, honoured only with AGENT_OPS_NO_SEND set.
# Bypass, for fake-docker tests ONLY: a COMPOSE_PROJECT_NAME other than capsule-corp skips
# the check when the docker that will run is a plain file (not a symlink) under the temp
# root (TMPDIR or /tmp), and that temp root is not the passwd home or inside it. A marker
# env var would not do: tests set AGENT_OPS_NO_SEND without stubbing docker. With real
# docker a non-live project still collides with the live container_names and ports.
# (OBS_ENV unset after sourcing is reachable, so the guard keeps that refusal branch.)
# Guarded: compose_up_infisical, compose_up_rest, compose_recreate, hoi-poi's compose
# stop, the rollback up. Left open: compose_services (`config`) and `ps` (read-only).
compose_real_home() {
  local u h
  if [ -n "${CAPSULE_TEST_COMPOSE_REAL_HOME:-}" ] && [ -n "${AGENT_OPS_NO_SEND:-}" ]; then
    printf '%s\n' "$CAPSULE_TEST_COMPOSE_REAL_HOME"; return 0
  fi
  u="$(id -un)"
  h="$(dscl . -read "/Users/$u" NFSHomeDirectory 2>/dev/null | sed -n 's/^NFSHomeDirectory: //p')"
  [ -n "$h" ] || h="$(getent passwd "$u" 2>/dev/null | cut -d: -f6)"
  [ -n "$h" ] || h="$(eval echo "~$u" 2>/dev/null)"
  case "$h" in /*) printf '%s\n' "$h" ;; esac   # an unexpanded ~user (unknown user) is no answer
}
compose_docker_is_fake() {  # the docker this call would run is a plain file under the temp root
  # Not a symlink (a link to real docker in a temp dir is not a fake), and a temp root that is
  # the passwd home or inside it never counts (Docker Desktop's ~/.docker/bin/docker would).
  local p d t rh
  p="$(command -v "$DOCKER_BIN" 2>/dev/null)" || p="$DOCKER_BIN"
  case "$p" in /*) ;; *) return 1 ;; esac
  [ -L "$p" ] && return 1
  d="$(cd -P "$(dirname "$p")" 2>/dev/null && pwd -P)" || return 1
  rh="$(compose_real_home)"; [ -z "$rh" ] || rh="$(cd -P "$rh" 2>/dev/null && pwd -P)"
  for t in "${TMPDIR:-/tmp}" /tmp; do
    t="$(cd -P "$t" 2>/dev/null && pwd -P)" || continue
    [ -z "$rh" ] || case "$t/" in "$rh/"*) continue ;; esac
    case "$d/" in "$t"/*) return 0 ;; esac
  done
  return 1
}
compose_live_config_dir() {  # <real home>: that home'"'"'s config dir; env is ignored on purpose (a temp dir must not pass as live)
  # Same order as capsule/jobs_dir.py (the source): host.json config_dir, else the new default if it
  # exists, else the legacy dir if it holds observability.env or ops.env, else the new default.
  local repo v legacy_rel=".config/obsidian-second-brain"
  repo="$(cd -P "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
  v="$(cd "$repo" && env -u CAPSULE_JOBS_DIR PYTHONPATH="$repo" python3 -sP -m capsule host get --only config_dir --capsule-home "$1/.local/share/capsule" 2>/dev/null)" || v=""
  if [ -z "$v" ]; then
    v="$1/.config/capsule-corp"
    if [ ! -d "$v" ] && { [ -f "$1/$legacy_rel/observability.env" ] || [ -f "$1/$legacy_rel/ops.env" ]; }; then v="$1/$legacy_rel"; fi
  fi
  printf '%s\n' "$v"
}
compose_live_guard() {
  local obs real
  if [ "${COMPOSE_PROJECT_NAME:-capsule-corp}" != capsule-corp ] && compose_docker_is_fake; then return 0; fi
  real="$(compose_real_home)"
  obs="$(cd -P "$(dirname "${OBS_ENV:-/nonexistent/x}")" 2>/dev/null && pwd -P)"
  if [ -n "$real" ] && [ -n "$obs" ] && [ "$obs" = "$(cd -P "$(compose_live_config_dir "$real")" 2>/dev/null && pwd -P)" ]; then
    return 0
  fi
  echo "FAILED compose: refusing to run the live capsule-corp stack with a non-live OBS_ENV (${OBS_ENV:-OBS_ENV unset})"
  return 1
}

compose_up_infisical() {
  local rc
  compose_live_guard || return 1
  "$DOCKER_BIN" compose --env-file "$OBS_ENV" -f "$COMPOSE_FILE" up -d --quiet-pull infisical-db infisical-redis infisical >/dev/null 2>&1
  rc=$?
  if [ "$rc" -eq 0 ]; then echo "compose: infisical ok"; else echo "compose: infisical failed (docker compose up exit $rc)"; return 1; fi
}
compose_wait_infisical() {
  local waited=0 step_advance="$COMPOSE_WAIT_STEP_S"
  # A step of 0 (tests want no real delay) must still make progress, or the
  # loop never reaches COMPOSE_WAIT_S; sleep stays instant, only the counter
  # advances by at least 1.
  [ "$step_advance" -gt 0 ] 2>/dev/null || step_advance=1
  while ! "$CURL_BIN" -fsS -m 2 "$INFISICAL_STATUS_URL" >/dev/null 2>&1; do
    if [ "$waited" -ge "$COMPOSE_WAIT_S" ]; then echo "compose: wait failed (infisical not answering after ${COMPOSE_WAIT_S}s)"; return 1; fi
    sleep "$COMPOSE_WAIT_STEP_S"; waited=$((waited + step_advance))
  done
  echo "compose: wait ok"
}
# The one tolerated failure (D153): the front-door's 127.0.0.1:80 held by Tailscale.
# compose prints progress lines before the daemon error, so compose_up_rest reports
# the last non-empty stderr line (the verdict), never line 1 and never an earlier match.
COMPOSE_PORT_RE='(^|[^0-9.])127\.0\.0\.1:80[^0-9].*(port is already allocated|address already in use)'
compose_up_rest() {
  local err rc line
  compose_live_guard || return 1
  err="$(CAPSULE_IDENTITY=compose "$CAPSULE_SECRET_BIN" run capsule-corp -- "$DOCKER_BIN" compose --env-file "$OBS_ENV" -f "$COMPOSE_FILE" up -d --quiet-pull 2>&1 >/dev/null)"
  rc=$?
  if [ "$rc" -eq 0 ]; then echo "compose: rest ok"; return 0; fi
  # `infisical run` appends its own "failed to wait for command termination: <err>" line
  # (wording checked in the installed binary's strings); drop only that exact shape, end-anchored
  # and not start-anchored (a logger prefix is fine), so compose's error is the last line.
  line="$(printf '%s\n' "$err" | grep -vE 'failed to wait for command termination: (exit status [0-9]+|signal: .*)$' | grep . | tail -n 1)"
  [ -n "$line" ] || line="exit $rc"
  echo "compose: rest failed ($line)"; return 1
}
compose_up_all() {
  # The infisical trio only exists under the secrets profile (P06).
  case ",${COMPOSE_PROFILES:-}," in
    *,secrets,*) compose_up_infisical && compose_wait_infisical || return 1 ;;
  esac
  compose_up_rest
}
# Grafana's alerting contact points read ${SLACK_WEBHOOK_*} from the container
# environment, and since the cutover (row 142) that value exists only in
# Infisical. A recreate outside the identity wrapper therefore starts Grafana
# with an empty webhook and it exits on provisioning (2026-09-11 01:52, row 153).
# Every recreate goes through here, the same door as compose_up_rest.
compose_recreate() {  # compose_recreate <service> - prints nothing on
  # success; on failure prints the fake's/real helper's first stderr line
  # (or "exit $rc" if it had none) and returns 1. Callers decide what to do
  # with that line - never `set -x` here: it would put a full expansion of
  # this function's own tracing on stderr, and tracing (not this argv,
  # which holds no secret) is how a secret-bearing expansion leaks in the
  # helpers that do hold one.
  local svc="$1" err rc
  compose_live_guard || return 1
  err="$(CAPSULE_IDENTITY=compose "$CAPSULE_SECRET_BIN" run capsule-corp -- "$DOCKER_BIN" compose --env-file "$OBS_ENV" -f "$COMPOSE_FILE" up -d --force-recreate "$svc" 2>&1 >/dev/null)"
  rc=$?
  [ "$rc" -eq 0 ] && return 0
  printf '%s\n' "$(printf '%s' "$err" | grep -m1 . || echo "exit $rc")"
  return 1
}

# compose_services - one service name per line, straight from compose itself.
# The single place that answers "what should be running": both heal.sh and
# the other compose callers use this instead of pattern-matching the YAML, which could not
# tell a service from a top-level `volumes:`/`networks:` key and would report
# phantom services as missing. No secret is involved, so this runs the plain
# docker binary rather than going through the helper.
#
# compose's own stderr is discarded (a broken compose file makes it noisy, and
# that noise is not this function's answer) but its EXIT STATUS is returned:
# every caller must fail closed on it. An empty list read as "nothing is
# missing" is exactly how "the stack is fine" gets reported for a stack nobody
# could even enumerate.
compose_services() {
  "$DOCKER_BIN" compose --env-file "$OBS_ENV" -f "$COMPOSE_FILE" config --services 2>/dev/null
}
