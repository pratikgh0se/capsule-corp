#!/usr/bin/env bash
# hoi-poi.sh - one-switch cold start and shutdown for Capsule Corp
# (spec docs/superpowers/specs/2026-09-12-hoi-poi-design.md, Readiness
# Mandate A6 row 201).
#
#   hoi-poi up      preflight, then converge everything in order, one heal
#                    tick, print `heal N/6`, exit 0 only on 6/6
#   hoi-poi down    one heal tick, then boot out every pipeline job, stop running
#                    worker containers, compose stop;
#                    exit 0 always
#   hoi-poi status  one table, one line per component, exit 0 when everything
#                    an `up` would start is running
#   hoi-poi         the foreground supervisor: up, then a status line every
#                    60s until Ctrl-C, which runs down and exits
#
# Never touches data, apps, credentials or config apply: compose only ever
# gets `up`/`stop`, never the stack-teardown subcommand, never `-v`, never
# an image/volume cleanup command, no `rm` on state or backups, no version
# control command, no chat-send call, `bootstrap.sh` and `deploy.sh` only
# ever run `--check` (never applied), never starts Docker Desktop, never
# starts a worker container. No secret value on argv, in output, or in a
# log; a helper's own output is never echoed beyond this script's own exit
# line.
set -uo pipefail

: "${LAUNCHCTL_BIN:=launchctl}"
REPO="$(cd -P "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
: "${CAPSULE_SECRET_BIN:=$REPO/bin/capsule-secret}"
: "${BOOTSTRAP_BIN:=$REPO/bootstrap.sh}"
: "${RUN_SH_BIN:=$REPO/maintenance/run.sh}"
. "$REPO/maintenance/lib_config.sh"   # CAPSULE_JOBS_DIR
: "${HOI_POI_OPS_ENV:=$CAPSULE_JOBS_DIR/ops.env}"
# Flags only - no secret lives in ops.env since the Potara cutover (row 142).
[ -f "$HOI_POI_OPS_ENV" ] && . "$HOI_POI_OPS_ENV"
: "${COMPOSE_FILE:=$REPO/observability/docker-compose.yml}"
: "${LAUNCH_AGENTS_DIR:=$HOME/Library/LaunchAgents}"
# State root: $AGENT_OPS_STATE_DIR or ~/.local/state/capsule-corp/maint, shared with run.sh.
. "$REPO/maintenance/lib_state.sh"
. "$REPO/maintenance/lib_docker_engine.sh"   # DOCKER_ENGINE_TITLE / _OPEN_HINT for the docker FAIL line
: "${AGENT_OPS_FLAVOR:=personal}"
: "${HOI_POI_WAIT_STEP_S:=2}"
: "${HOI_POI_TICK_S:=60}"
: "${HOI_POI_MAX_TICKS:=0}"   # 0 = forever; tests only
UID_="$(id -u)"

# lib_jobs.sh's classify_check defaults its warning prefix to "capsule-park"
# (its other caller); name this tool in its own warning instead.
CLASSIFY_PREFIX=hoi-poi

# lib_jobs.sh needs REPO set (above); lib_compose.sh needs COMPOSE_FILE (above).
. "$REPO/bin/lib_jobs.sh"
. "$REPO/maintenance/lib_compose.sh"
# lib_compose.sh already sets this default; restated here (same literal, a
# no-op once set) so the egress scanner's same-file literal check
# (maintenance/tests/test_egress.py) can resolve the `status` row's own
# direct curl call to it.
: "${INFISICAL_STATUS_URL:=http://localhost:8085/api/status}"

say() { printf '%s\n' "$*"; }
row() { printf '%-11s %-6s %s\n' "$1" "$2" "$3"; }

loaded() { "$LAUNCHCTL_BIN" list 2>/dev/null | awk '{print $3}' | grep -qxF "$1"; }

# heal_field <component> - "<ok|FAIL> <age>m" from heal.json, or "- none"
# when heal.json is absent/unreadable. status never re-runs heal; it only
# reads the last tick's verdict.
heal_field() {
  python3 - "$STATE_DIR/heal.json" "$1" <<'PY'
import json, os, sys, time
p, comp = sys.argv[1], sys.argv[2]
try:
    d = json.load(open(p)); age = int((time.time() - os.path.getmtime(p)) // 60)
except Exception:
    print("- none"); sys.exit()
e = d.get(comp) or {}
st = "ok" if e.get("fails", 1) == 0 else "FAIL"
print(f"{st} {age}m")
PY
}

# heal_summary - "N/6 <age>m" from heal.json, or "-/6 none" when absent.
# A heal.json older than 10 min (2x the heal job's 5-min StartInterval,
# row 511 G4) means the job stalled rather than ran and failed, so it never
# reports N/6 - it prints -1/6, which fails the "6/6 " match everywhere this
# is read (status, up, down) however many components last passed.
heal_summary() {
  python3 - "$STATE_DIR/heal.json" <<'PY'
import json, os, sys, time
p = sys.argv[1]
try:
    d = json.load(open(p)); age = int((time.time() - os.path.getmtime(p)) // 60)
except Exception:
    print("-/6 none"); sys.exit()
# the components heal still checks; a stale key for a retired one neither counts nor breaks healthy
comps = "docker infisical stack secrets claude-login outbox".split()
ok = sum(1 for c in comps if (d.get(c) or {}).get("fails", 1) == 0)
if age > 10:
    ok = -1
print(f"{ok}/6 {age}m")
PY
}

park_labels() {  # one label per line; nothing when not parked
  [ -f "$STATE_DIR/park.json" ] || return 0
  python3 -c '
import json, sys
try:
    for l in (json.load(open(sys.argv[1])).get("labels") or []): print(l)
except Exception: pass' "$STATE_DIR/park.json"
}

# _measure - probes everything status needs exactly once and leaves the
# answers in globals, so status_rows (the table) and status_line (the
# supervisor's one-liner) never disagree and never double-probe.
_measure() {
  if "$DOCKER_BIN" info >/dev/null 2>&1; then M_DOCKER_OK=1; else M_DOCKER_OK=0; fi

  if "$CURL_BIN" -fsS -m 2 "$INFISICAL_STATUS_URL" >/dev/null 2>&1; then M_INFISICAL_OK=1; else M_INFISICAL_OK=0; fi

  M_STACK_CONF="$(compose_services 2>/dev/null)"
  M_STACK_RUNNING="$("$DOCKER_BIN" compose --env-file "$OBS_ENV" -f "$COMPOSE_FILE" ps --services --filter status=running 2>/dev/null)"
  M_STACK_N_CONF=0; [ -n "$M_STACK_CONF" ] && M_STACK_N_CONF=$(printf '%s\n' "$M_STACK_CONF" | grep -c .)
  M_STACK_N_RUN=0; [ -n "$M_STACK_RUNNING" ] && M_STACK_N_RUN=$(printf '%s\n' "$M_STACK_RUNNING" | grep -c .)
  if [ "$M_STACK_N_CONF" -gt 0 ] && [ "$M_STACK_N_CONF" -eq "$M_STACK_N_RUN" ]; then M_STACK_OK=1; else M_STACK_OK=0; fi

  read -r M_SECRETS_ST M_SECRETS_AGE <<<"$(heal_field secrets)"
  [ "$M_SECRETS_ST" = ok ] && M_SECRETS_OK=1 || M_SECRETS_OK=0

  if "$CURL_BIN" -fsS -m 2 http://localhost:11434/api/tags >/dev/null 2>&1; then M_OLLAMA_OK=1; else M_OLLAMA_OK=0; fi

  M_PARKED_LABELS="$(park_labels)"
  M_PARKED_N=0; [ -n "$M_PARKED_LABELS" ] && M_PARKED_N=$(printf '%s\n' "$M_PARKED_LABELS" | grep -c .)

  # M_PARKED_N (above) counts every label in park.json; only the labels that are
  # actually in PIPELINE_JOBS count against the jobs row's expectation.
  M_JOBS_TOTAL=${#PIPELINE_JOBS[@]}
  M_JOBS_LOADED=0
  M_JOBS_PARKED=0
  for _j in "${PIPELINE_JOBS[@]}"; do
    if printf '%s\n' "$M_PARKED_LABELS" | grep -qxF "$_j"; then
      M_JOBS_PARKED=$((M_JOBS_PARKED + 1))
      continue
    fi
    loaded "$_j" && M_JOBS_LOADED=$((M_JOBS_LOADED + 1))
  done
  M_JOBS_EXPECT=$((M_JOBS_TOTAL - M_JOBS_PARKED))
  if [ "$M_JOBS_LOADED" -eq "$M_JOBS_EXPECT" ]; then M_JOBS_OK=1; else M_JOBS_OK=0; fi

  M_HEAL_SUMMARY="$(heal_summary)"
  case "$M_HEAL_SUMMARY" in 6/6\ *) M_HEAL_OK=1 ;; *) M_HEAL_OK=0 ;; esac

  M_WORKERS_N=0
  local _w; _w="$("$DOCKER_BIN" ps -q --filter "label=capsule-run-id" 2>/dev/null)"
  [ -n "$_w" ] && M_WORKERS_N=$(printf '%s\n' "$_w" | grep -c .)

  if [ -f "$STATE_DIR/hoi-poi.json" ]; then
    M_DOWN_STAMP="$(python3 -c '
import datetime, json, sys
try:
    d = json.load(open(sys.argv[1]))
    print(datetime.datetime.fromtimestamp(d["down_since"]).strftime("%Y-%m-%d %H:%M"))
except Exception:
    print("unreadable")' "$STATE_DIR/hoi-poi.json")"
  else
    M_DOWN_STAMP="none"
  fi
}

status_rows() {
  _measure
  local rc=0
  say "hoi-poi status  $(date '+%Y-%m-%d %H:%M')"
  if [ "$M_DOCKER_OK" = 1 ]; then row docker ok "docker info answers"; else row docker FAIL "docker info did not answer"; rc=1; fi
  if [ "$M_INFISICAL_OK" = 1 ]; then row infisical ok "/api/status answers"; else row infisical FAIL "/api/status did not answer"; rc=1; fi
  if [ "$M_STACK_OK" = 1 ]; then row stack ok "$M_STACK_N_RUN/$M_STACK_N_CONF services running"; else row stack FAIL "$M_STACK_N_RUN/$M_STACK_N_CONF services running"; rc=1; fi
  if [ "$M_SECRETS_OK" = 1 ]; then row secrets ok "every identity reads (from heal.json, age ${M_SECRETS_AGE})"; else row secrets FAIL "heal.json says secrets $M_SECRETS_ST (age ${M_SECRETS_AGE})"; rc=1; fi
  if [ "$M_OLLAMA_OK" = 1 ]; then row ollama ok "api/tags answers"; else row ollama FAIL "api/tags did not answer"; rc=1; fi
  if [ "$M_JOBS_OK" = 1 ]; then
    if [ "$M_JOBS_PARKED" -gt 0 ]; then row jobs ok "$M_JOBS_LOADED/$M_JOBS_TOTAL loaded ($M_JOBS_PARKED parked)"; else row jobs ok "$M_JOBS_LOADED/$M_JOBS_TOTAL loaded"; fi
  else
    row jobs FAIL "$M_JOBS_LOADED/$M_JOBS_EXPECT loaded (want $M_JOBS_EXPECT)"; rc=1
  fi
  if [ "$M_HEAL_OK" = 1 ]; then row heal ok "$M_HEAL_SUMMARY"; else row heal FAIL "$M_HEAL_SUMMARY"; rc=1; fi
  row workers - "$M_WORKERS_N running"
  if [ -f "$STATE_DIR/park.json" ]; then
    row park - "parked ($M_PARKED_N labels)"
  else
    row park - "not parked"
  fi
  row down-stamp - "$M_DOWN_STAMP"
  return "$rc"
}

status_line() {
  _measure
  printf 'hoi-poi  %s  docker %s  stack %s/%s  jobs %s/%s  heal %s  workers %s\n' \
    "$(date +%H:%M)" \
    "$([ "$M_DOCKER_OK" = 1 ] && echo ok || echo FAIL)" \
    "$M_STACK_N_RUN" "$M_STACK_N_CONF" \
    "$M_JOBS_LOADED" "$M_JOBS_TOTAL" \
    "$M_HEAL_SUMMARY" \
    "$M_WORKERS_N"
}

bootstrap_label() {  # bootstrap_label <label> - true if bootstrapped, or
  # already loaded (launchctl exits non-zero for "already bootstrapped";
  # `loaded` afterwards is what makes that case count as ok, spec section 4.6).
  local l="$1" plist
  plist="$LAUNCH_AGENTS_DIR/$l.plist"
  "$LAUNCHCTL_BIN" bootstrap "gui/$UID_" "$plist" >/dev/null 2>&1 && return 0
  loaded "$l"
}

spool_phone() {  # spool_phone <verb> - run.sh's outbox spool format, addressed
  # to OPS_ALERT_TARGET; heal's own outbox flush (step 8) is what actually
  # sends it, through run.sh's tg_send (event trail, budget, no-chat no-op).
  # Hoi-Poi never calls the chat-send command itself. Nothing is spooled when
  # OPS_ALERT_TARGET is unset - heal would dead-letter an empty `to=`.
  [ -n "${OPS_ALERT_TARGET:-}" ] || return 0
  mkdir -p "$STATE_DIR/outbox"
  local body; body="hoi-poi $1: $(hostname) $(date +%H:%M)"
  printf 'to=%s\nat=%s\nsubject=%s\n\n%s\n' "$OPS_ALERT_TARGET" "$(date +%Y-%m-%dT%H:%M:%S%z)" "" "$body" \
    >"$STATE_DIR/outbox/$(date +%s)-hoi-poi.msg"
}

heal_tick_and_verdict() {  # runs the one heal tick, prints "heal N/6 ..."
  bash "$RUN_SH_BIN" heal
  local hs; hs="$(heal_summary)"
  say "heal $hs"
  case "$hs" in 6/6\ *) return 0 ;; *) return 10 ;; esac
}

do_up() {
  # 1. Preflight (exit 3 on refuse). bootstrap's --check already runs
  # deploy.sh --check inside it, so one call covers both; its own exit code
  # is ignored in favour of the line classification (bootstrap exits 1 on
  # DRIFT too, and DRIFT is not a reason to leave the pipeline off).
  if [ "${HOI_POI_SKIP_PREFLIGHT:-0}" != 1 ]; then
    local pf miss drift
    pf="$("$BOOTSTRAP_BIN" --check --flavor "$AGENT_OPS_FLAVOR" 2>&1)"
    # A MISS naming ollama-serve is downgraded to a warning: step 5 below
    # exists to bootstrap ollama-serve when it is not loaded, so refusing
    # on that exact MISS would refuse the one run whose job is to fix it
    # (spec defect, fixed in the spec too - see spec section 4.1).
    miss="$(printf '%s\n' "$pf" | grep '^MISS' | grep -v 'ollama-serve' || true)"
    if [ -n "$miss" ]; then
      printf '%s\n' "$miss"
      say "hoi-poi up: FAIL preflight"
      exit 3
    fi
    drift="$(printf '%s\n' "$pf" | grep '^DRIFT' || true)"
    if [ -n "$drift" ]; then
      while IFS= read -r _line; do [ -n "$_line" ] && say "WARN $_line"; done <<<"$drift"
    fi
    local ollama_miss
    ollama_miss="$(printf '%s\n' "$pf" | grep '^MISS' | grep 'ollama-serve' || true)"
    if [ -n "$ollama_miss" ]; then
      while IFS= read -r _line; do [ -n "$_line" ] && say "WARN $_line"; done <<<"$ollama_miss"
    fi
  fi

  # 2. Docker (exit 4). Hoi-Poi never starts the Docker engine, but waits up to
  # 30s for it to answer - the exact case the goal names is a just-rebooted
  # laptop, where the Docker engine is autostarting and its socket is not yet
  # accepting calls; a single unbounded attempt used to refuse immediately.
  local docker_tries=0 docker_ok=0 docker_step docker_max_tries
  docker_step="$HOI_POI_WAIT_STEP_S"
  [ "$docker_step" -gt 0 ] 2>/dev/null || docker_step=1   # 0 (tests) still bounds the loop
  docker_max_tries=$((30 / docker_step)); [ "$docker_max_tries" -ge 1 ] || docker_max_tries=1
  while [ "$docker_tries" -lt "$docker_max_tries" ]; do
    if "$DOCKER_BIN" info >/dev/null 2>&1; then docker_ok=1; break; fi
    docker_tries=$((docker_tries + 1))
    sleep "$HOI_POI_WAIT_STEP_S"
  done
  if [ "$docker_ok" != 1 ]; then
    say "hoi-poi up: FAIL docker - start $DOCKER_ENGINE_TITLE and run hoi-poi up again"
    exit 4
  fi

  # 3. Compose stack (exit 5). Infisical trio first, wait for /api/status,
  # then the rest under the compose identity. Each phase prints its own line.
  if ! compose_up_all; then
    say "hoi-poi up: FAIL compose"
    exit 5
  fi

  # 4. Secrets (exit 6). Same check as heal's check 4; the helper's own
  # output is never echoed - only this script's exit line is.
  if ! CAPSULE_IDENTITY=compose "$CAPSULE_SECRET_BIN" check --identity-all >/dev/null 2>&1; then
    say "hoi-poi up: FAIL secrets"
    exit 6
  fi

  # 5. Ollama (exit 7). A plist but an app in Hoi-Poi's eyes: bootstrapped if
  # not loaded, then verified live. Down degrades vault search to keyword
  # only, a failure on an unattended run.
  loaded "$OLLAMA_LABEL" || bootstrap_label "$OLLAMA_LABEL" >/dev/null 2>&1
  local tries=0 ollama_ok=0
  while [ "$tries" -lt 10 ]; do
    if "$CURL_BIN" -fsS -m 2 http://localhost:11434/api/tags >/dev/null 2>&1; then ollama_ok=1; break; fi
    tries=$((tries + 1))
    sleep "$HOI_POI_WAIT_STEP_S"
  done
  if [ "$ollama_ok" != 1 ]; then
    say "hoi-poi up: FAIL ollama - api/tags did not answer"
    exit 7
  fi

  classify_check
  local parked
  parked="$(park_labels)"

  # 7. Pipeline jobs (exit 9), minus the labels in state/park.json when it
  # exists.
  local j job_fail=0 job_parked_n=0
  for j in "${PIPELINE_JOBS[@]}"; do
    if printf '%s\n' "$parked" | grep -qxF "$j"; then
      job_parked_n=$((job_parked_n + 1))
      continue
    fi
    bootstrap_label "$j" || job_fail=1
  done
  if [ "$job_parked_n" -gt 0 ]; then
    say "parked: $job_parked_n dispatching jobs left off - capsule unpark to resume"
  fi
  if [ "$job_fail" != 0 ]; then
    say "hoi-poi up: FAIL jobs"
    exit 9
  fi

  # 9. Phone - spooled before the heal tick, which is what flushes it.
  spool_phone up

  # 8. One heal tick; exit 0 on 6/6, else 10.
  heal_tick_and_verdict
  local rc=$?
  rm -f "$STATE_DIR/hoi-poi.json"   # up clears any prior down-stamp
  return "$rc"
}

# do_down never fails: every step reports and continues, exit is always 0 -
# a shutdown that stops halfway to complain is worse than one that did not
# run (spec section 5).
do_down() {
  # 1. Phone, then one heal tick - so the alert goes out first,
  # and heal records a last 6/6 before the lights go off.
  spool_phone down
  bash "$RUN_SH_BIN" heal
  say "heal $(heal_summary)"

  # 2a. Down-stamp, written BEFORE the bootout so tick-watch never starts a tick we stop (a refusal
  # at step 4 removes it again). status and the brief say "down since". up clears this.
  rm -f "$STATE_DIR/hoi-poi.json.prev"
  [ -f "$STATE_DIR/hoi-poi.json" ] && cp -p "$STATE_DIR/hoi-poi.json" "$STATE_DIR/hoi-poi.json.prev"
  python3 - "$STATE_DIR/hoi-poi.json" <<'PY'
import json, os, sys, time
p = sys.argv[1]
tmp = p + ".tmp"
os.makedirs(os.path.dirname(p), exist_ok=True)
json.dump({"v": 1, "down_since": time.time(), "by": "hoi-poi"}, open(tmp, "w"))
os.chmod(tmp, 0o600); os.replace(tmp, p)
PY

  # 2. Boot out every pipeline job ("was not loaded" is fine, as in park).
  local j
  for j in "${PIPELINE_JOBS[@]}"; do
    if "$LAUNCHCTL_BIN" bootout "gui/$UID_/$j" >/dev/null 2>&1; then say "  booted out $j"
    else say "  $j was not loaded (fine)"; fi
  done

  # 3. Stop running worker containers, 20s grace. Workers are found by the runner's
  # `capsule-run-id` label, whatever image they run (layered images have no one tag). A worker
  # killed mid-run is the bridge's problem on the next up (hotfix 10): its card is held by
  # repair as any dead run is.
  local ids id guard_msg
  ids="$("$DOCKER_BIN" ps -q --filter "label=capsule-run-id" 2>/dev/null | awk 'NF' | sort -u)"
  if [ -n "$ids" ]; then
    for id in $ids; do
      "$DOCKER_BIN" stop -t 20 "$id" >/dev/null 2>&1 || say "  could not stop worker $id"
    done
  fi

  # 4. Compose stop - plain docker binary, no secret needed. Volumes stay:
  # never the teardown subcommand, never `-v`, never an image/volume cleanup.
  if ! guard_msg="$(compose_live_guard)"; then
    # remove only the stamp this run wrote: an older down stamp comes back
    stamp_note="no stamp written"
    if [ -f "$STATE_DIR/hoi-poi.json.prev" ]; then mv -f "$STATE_DIR/hoi-poi.json.prev" "$STATE_DIR/hoi-poi.json"; stamp_note="older stamp kept"
    else rm -f "$STATE_DIR/hoi-poi.json"; fi
    say "  $guard_msg"; say "hoi-poi: refused by the live-stack guard (stack not stopped, $stamp_note)"; return 1
  fi
  "$DOCKER_BIN" compose --env-file "$OBS_ENV" -f "$COMPOSE_FILE" stop >/dev/null 2>&1 \
    || say "  compose stop reported a failure (continuing)"

  rm -f "$STATE_DIR/hoi-poi.json.prev"
  say "hoi-poi: down"
  return 0
}

# shutdown_and_exit - the supervisor's one way out, whether triggered by a
# signal or (test-only) by HOI_POI_MAX_TICKS. Never re-runs up, never
# kickstarts anything: heal does that. Closing the terminal (SIGHUP) is
# treated like Ctrl-C.
shutdown_and_exit() {
  # Disarm first: a second Ctrl-C during shutdown must not re-enter this
  # (a second heal tick, a second outbox file, a second bootout sweep on
  # top of the first).
  trap '' INT TERM HUP
  say "hoi-poi: Ctrl-C, going down"
  do_down
  exit 0
}

# supervise - the foreground loop: up, then a status line every
# HOI_POI_TICK_S (default 60) until a signal, which goes down and exits.
# The supervisor never re-runs up and never kickstarts anything.
supervise() {
  do_up
  local rc=$?
  [ "$rc" -eq 0 ] || exit "$rc"
  trap shutdown_and_exit INT TERM HUP
  local ticks=0
  while :; do
    status_line
    ticks=$((ticks + 1))
    # HOI_POI_MAX_TICKS is for tests only (default 0 = forever); reaching it
    # shuts down exactly like a signal would, so a test never races a signal
    # against a "sleep 0" loop that could otherwise spin past it.
    if [ "$HOI_POI_MAX_TICKS" -gt 0 ] && [ "$ticks" -ge "$HOI_POI_MAX_TICKS" ]; then
      shutdown_and_exit
    fi
    # One-second steps, not a single long sleep: a non-terminal SIGTERM
    # (launchd at logout, a wrapper's kill) only runs bash's trap between
    # foreground commands, so a single `sleep 60` could hold the signal off
    # for up to a minute. This bounds that to ~1s.
    local _slept=0
    while [ "$_slept" -lt "$HOI_POI_TICK_S" ]; do
      sleep 1
      _slept=$((_slept + 1))
    done
  done
}

usage() { echo "usage: hoi-poi.sh <up|down|status>" >&2; exit 2; }

CMD="${1:-}"
case "$CMD" in
  status) shift || true; [ $# -eq 0 ] || usage; status_rows; exit $? ;;
  up) shift || true; [ $# -eq 0 ] || usage; do_up; exit $? ;;
  down) shift || true; [ $# -eq 0 ] || usage; do_down; exit 0 ;;
  "") supervise ;;
  *) usage ;;
esac
