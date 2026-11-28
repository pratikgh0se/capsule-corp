#!/usr/bin/env bash
# heal.sh - the 5-minute self-healing tick (spec 2026-09-08-secrets-cutover-
# design.md section 5). Pure bash, no model. Runs eight checks in order (slack-token is report-only: never sets the exit code),
# writes exactly one `infra` event per check, keeps consecutive-failure
# counters in state/heal.json, notifies only for docker-down (from the
# second consecutive failure onwards) and claude-logout (12 h throttle each),
# pauses the pipeline on a Claude logout, and flushes the spool outbox.
# Notices queue an outbox row.
# Exit 0 = every check ok or skipped behind an upstream failure; else 1.
# Invoked by run.sh (job `heal`), which exports STATE_DIR and EV (HEAL_SEND is optional).
set -u
: "${STATE_DIR:?heal: set STATE_DIR}"
: "${EV:?heal: set EV}"
. "$(dirname "${BASH_SOURCE[0]}")/lib_docker_engine.sh"   # DOCKER_ENGINE_TITLE / _OPEN_HINT for the page text
# HEAL_SEND (optional): a function/command, body on stdin -> outbox row. Default below.
: "${COMPOSE_FILE:?heal: set COMPOSE_FILE}"
# lib_compose.sh sets OBS_ENV/CAPSULE_SECRET_BIN/INFISICAL_STATUS_URL/
# COMPOSE_WAIT_S/COMPOSE_WAIT_STEP_S/DOCKER_BIN/CURL_BIN defaults - no point
# repeating them here, only heal's own knobs below.
. "$(dirname "${BASH_SOURCE[0]}")/lib_compose.sh"
: "${DOCKER_DOWN_THROTTLE_S:=43200}"
# The outbox flush always queues through `capsule outbox-send`: heal.sh defines
# its own tg_send, which queues an outbox row on the configured board (the tick
# flushes it); --to/--subject/-q do not apply there.
. "$(dirname "${BASH_SOURCE[0]}")/lib_send.sh"   # capsule_cli, CAPSULE_REPO
# HEAL_V2_CLASS=page: the line keeps HEAL_CLASS while routing v2 is off and pages once it is on.
tg_send() { capsule_cli outbox-send "${HEAL_CLASS:-incident}" "$(cat)" ${HEAL_V2_CLASS:+--v2-class "$HEAL_V2_CLASS"}; }
HEAL_SEND=tg_send
HEAL_STATE="$STATE_DIR/heal.json"
DOCKER_DOWN_STAMP="$STATE_DIR/docker-down-alerted"
# maintenance/docker_idle.py (D195) writes this when it quits Docker Desktop on purpose: Docker down
# after it is a skip, not a failure or a page, until Docker answers again (and the stamp is 2 min old,
# so a daemon still dying right after the quit does not clear it).
DOCKER_IDLE_QUIT_STAMP="$STATE_DIR/docker-idle-quit"

RC=0
filemtime() { stat -c %Y "$1" 2>/dev/null || stat -f %m "$1" 2>/dev/null || echo 0; }
# stamp_due <file> <throttle_s> - Task 5 adds a second throttle stamp
# (claude-logout), so the throttle window is a parameter, not a global.
stamp_due() { local f="$1" throttle="$2"; [ -f "$f" ] || return 0; [ $(( $(date +%s) - $(filemtime "$f") )) -ge "$throttle" ]; }
# fails_so_far <component> - the consecutive-failure count already recorded
# for that check BEFORE this tick's own record() call. Used by the docker
# check to stay silent on a first failure (see below).
fails_so_far() {
  python3 -c 'import json,sys
try: print(int(json.load(open(sys.argv[1])).get(sys.argv[2], {}).get("fails", 0) or 0))
except Exception: print(0)' "$HEAL_STATE" "$1" 2>/dev/null || echo 0
}

# heal_record <component> <ok|fail|skipped|resumed> <text>
# Updates the counter (fail increments, anything else resets) and logs one event.
heal_record() {
  local comp="$1" verdict="$2" text="$3"
  python3 - "$HEAL_STATE" "$comp" "$verdict" <<'PY'
import json, sys, datetime, pathlib
p, comp, verdict = pathlib.Path(sys.argv[1]), sys.argv[2], sys.argv[3]
try: d = json.loads(p.read_text())
except Exception: d = {}
e = d.get(comp) or {"fails": 0, "first_fail": None}
if verdict == "fail":
    e["fails"] += 1
    e["first_fail"] = e["first_fail"] or datetime.datetime.now().astimezone().isoformat(timespec="seconds")
else:
    e = {"fails": 0, "first_fail": None}
d[comp] = e; p.parent.mkdir(parents=True, exist_ok=True); p.write_text(json.dumps(d))
print(json.dumps({"component": comp, "ok": verdict != "fail", "fails": e["fails"], "first_fail": e["first_fail"]}))
PY
}
record() {  # wraps heal_record: logs the event, sets RC on fail, echoes the line
  local comp="$1" verdict="$2" text="$3" payload
  payload="$(heal_record "$comp" "$verdict" "$text")" \
    || echo "heal: WARNING heal_record failed for $comp (state not updated)" >&2
  $EV log infra heal "$comp $verdict: $text" --payload "$payload" >/dev/null 2>&1 \
    || echo "heal: WARNING event log failed for $comp" >&2
  echo "heal: $comp $verdict: $text"
  [ "$verdict" = fail ] && RC=1
  return 0
}

# 1 docker
DOCKER_OK=0
if "$DOCKER_BIN" info >/dev/null 2>&1; then
  DOCKER_OK=1
  # The recovery line only goes out if an alert actually went out: the stamp
  # is the proof. A silent first-failure tick (below) leaves no stamp, so a
  # Docker engine that finished starting between two ticks says nothing.
  if [ -f "$DOCKER_DOWN_STAMP" ]; then rm -f "$DOCKER_DOWN_STAMP"; printf '✅ Docker is back\n' | HEAL_CLASS=done $HEAL_SEND; fi
  if [ -f "$DOCKER_IDLE_QUIT_STAMP" ] && [ $(( $(date +%s) - $(filemtime "$DOCKER_IDLE_QUIT_STAMP") )) -ge 120 ]; then rm -f "$DOCKER_IDLE_QUIT_STAMP"; fi
  record docker ok "docker info answers"
elif [ -f "$DOCKER_IDLE_QUIT_STAMP" ]; then
  record docker skipped "quit by docker-idle (D195)"
else
  # Alert on the SECOND consecutive failure, not the first: after a reboot
  # the 5-minute tick regularly beats the Docker engine's own startup, and that
  # is not worth a phone buzz. fails_so_far is the count before this tick, so
  # >= 1 means this failure is at least the second in a row.
  if [ "$(fails_so_far docker)" -ge 1 ] && stamp_due "$DOCKER_DOWN_STAMP" "$DOCKER_DOWN_THROTTLE_S"; then
    printf '⚠️ Docker is not running\n\n1. Grafana, Loki, Postgres and Infisical are down with it\n2. Open %s, or: %s\n3. heal retries every 5 min (next message in 12 h if it stays down)\n' "$DOCKER_ENGINE_TITLE" "$DOCKER_ENGINE_OPEN_HINT" | HEAL_CLASS=page $HEAL_SEND
    : >"$DOCKER_DOWN_STAMP"
  fi
  record docker fail "docker info failed"
fi

# 2 infisical
# Each phase's own line is captured separately so the fail reason names the
# phase that actually broke ("compose: infisical failed (docker compose up
# exit 1)" vs "compose: wait failed (...)") instead of one guessed sentence.
INF_OK=0
if [ $DOCKER_OK = 1 ]; then
  inf_out=""
  case ",${COMPOSE_PROFILES:-}," in
    *,secrets,*)
      if inf_out="$(compose_up_infisical)" && inf_out="$(compose_wait_infisical)"; then
        INF_OK=1; record infisical ok "/api/status answers"
      else
        record infisical fail "${inf_out:-not answering after ${COMPOSE_WAIT_S}s}"
      fi ;;
    *)  # P06: no infisical backend, no infisical trio; the stack phase still runs
      INF_OK=1; record infisical skipped "secrets profile off" ;;
  esac
else record infisical skipped "docker down"; fi

# 3 stack
if [ $INF_OK = 1 ]; then
  if out="$(compose_up_rest)"; then
    # Fail closed on the service list. A failed (or empty) `config --services`
    # used to leave $want empty, the missing-service loop ran zero times, and
    # heal cheerfully recorded "every service running" for a stack it could
    # not even enumerate.
    if ! want="$(compose_services)" || [ -z "$want" ]; then
      record stack fail "cannot list services (docker compose config failed)"
    else
      running="$("$DOCKER_BIN" compose --env-file "$OBS_ENV" -f "$COMPOSE_FILE" ps --status running --format '{{.Service}}' 2>/dev/null)"
      missing=""; for s in $want; do grep -qx "$s" <<<"$running" || missing="$missing $s"; done
      if [ -z "$missing" ]; then record stack ok "every service running"; else record stack fail "not running:$missing"; fi
    fi
  else record stack fail "$out"; fi
else record stack skipped "infisical not answering"; fi

# 4 secrets
if [ $INF_OK = 1 ]; then
  if out="$(CAPSULE_IDENTITY=compose CAPSULE_SECRET_WAIT_S=0 "$CAPSULE_SECRET_BIN" check --identity-all 2>&1)"; then record secrets ok "every identity reads"
  else
    # check --identity-all prints one "capsule-secret: ok (<id>)" line per
    # PASSING identity and the failure line (from die(), on stderr) for the
    # one that isn't - a plain first-line grep over the merged output can
    # pick a passing identity's line. Skip every "ok (" line so the reason
    # names the identity that actually failed.
    fail_line="$(grep '^capsule-secret:' <<<"$out" | grep -v ' ok (' | head -1)"
    record secrets fail "${fail_line:-check failed}"
  fi
else record secrets skipped "infisical not answering"; fi

# 4b slack-token (D227): the bot token must be readable from the Keychain with Docker down, so
# this runs whatever the earlier checks said. Report-only: it never pages and never changes RC
# (a heal exit 1 would page every tick and fail the healthcheck). ok/missing, never the value.
st_lim=(); command -v perl >/dev/null 2>&1 && st_lim=(perl -e 'alarm shift; exec @ARGV' 20)
st_rc=0; ${st_lim[@]+"${st_lim[@]}"} "$CAPSULE_SECRET_BIN" keychain-check SLACK_BOT_TOKEN >/dev/null 2>&1 || st_rc=$?
st_keep=$RC
case "$st_rc" in
  0) record slack-token ok "readable from the keychain" ;;
  4) record slack-token skipped "no keychain here" ;;
  *) record slack-token fail "SLACK_BOT_TOKEN not in the keychain (RUNBOOK: Slack token in the Keychain)" ;;
esac
RC=$st_keep

# (check 5, the Telegram gateway row, is gone: Telegram retired, D229/D233; numbers kept)

# 6 claude-login
GUARD="$STATE_DIR/usage-guard.json"; LOGOUT_STAMP="$STATE_DIR/claude-logout-alerted"
: "${CLAUDE_LOGOUT_THROTTLE_S:=$DOCKER_DOWN_THROTTLE_S}"
ctl_tool=capsule   # names the tool in failure text
claude_ctl() {
  case "$1" in
    pause) capsule_cli pause --reason "claude logged out" --seconds 315360000 >/dev/null 2>&1 ;;  # no expiry
    resume) capsule_cli unpark --reason "claude logged out" >/dev/null 2>&1 ;;
  esac
}
logged_in="$(claude auth status 2>/dev/null | python3 -c 'import json,sys
try: print("yes" if json.load(sys.stdin).get("loggedIn") else "no")
except Exception: print("unknown")')"
guard_reason="$(python3 -c 'import json,sys
try: print(json.load(open(sys.argv[1])).get("reason") or "")
except Exception: print("")' "$GUARD")"
if [ "$logged_in" = no ]; then
  if stamp_due "$LOGOUT_STAMP" "$CLAUDE_LOGOUT_THROTTLE_S"; then printf 'Claude Code is logged out - run /login in a terminal. Sprint pipeline paused until you are back.\n' | HEAL_CLASS=paused HEAL_V2_CLASS=page $HEAL_SEND; : >"$LOGOUT_STAMP"; fi
  pause_note=""
  if [ "$guard_reason" != "claude logged out" ] && ! python3 -c 'import json,sys,time; d=json.load(open(sys.argv[1])); sys.exit(0 if (d.get("paused_until") or 0) > time.time() else 1)' "$GUARD" 2>/dev/null; then
    pause_rc=0; claude_ctl pause --reason "claude logged out" || pause_rc=$?
    if [ "$pause_rc" -ne 0 ]; then
      echo "heal: WARNING $ctl_tool pause failed (exit $pause_rc)" >&2
      pause_note=" ($ctl_tool pause failed)"
    fi
    # The pause call may have failed, but heal must still remember it tried
    # to pause - otherwise a dead control plane silently drops the pause
    # forever instead of retrying (the next tick sees guard_reason already
    # set and skips straight past this branch).
    python3 -c 'import json,sys,time; json.dump({"paused_until": time.time()+86400, "announced": True, "reason": "claude logged out"}, open(sys.argv[1],"w"))' "$GUARD"
    echo "$(( $(date +%s) + 86400 ))" >"$STATE_DIR/session-limit-until"
  fi
  record claude-login fail "loggedIn=false$pause_note"
elif [ "$logged_in" = yes ] && [ "$guard_reason" = "claude logged out" ]; then
  resume_rc=0; claude_ctl resume || resume_rc=$?
  if [ "$resume_rc" -ne 0 ]; then
    echo "heal: WARNING $ctl_tool resume failed (exit $resume_rc)" >&2
    # Do NOT clear the guard reason or stamps - a failed resume must retry
    # next tick, not silently strand the pipeline paused.
    record claude-login resumed "logged in again ($ctl_tool resume failed, retry next tick)"
  else
    rm -f "$LOGOUT_STAMP" "$STATE_DIR/session-limit-until"
    python3 -c 'import json,sys; json.dump({"paused_until": None, "announced": False}, open(sys.argv[1],"w"))' "$GUARD"
    record claude-login resumed "logged in again, pipeline resumed"
  fi
elif [ "$logged_in" = yes ]; then rm -f "$LOGOUT_STAMP"; record claude-login ok "loggedIn=true"
else record claude-login fail "claude auth status unreadable"; fi

# 6b tick (stall supervisor unit 4a): the watchdog of the tick. `tick-watch --apply --json` restarts a
# wedged or dead tick once and pages a stopped one or a crash loop itself; one record for all boards.
tw_rc=0; tw_out="$(capsule_cli tick-watch --apply --json 2>/dev/null)" || tw_rc=$?
tw_bad="$(printf '%s\n' "$tw_out" | python3 -c 'import json,sys
bad, res = [], []
for line in sys.stdin:
    try: d = json.loads(line)
    except ValueError: continue
    if d.get("marker") == "valid": continue   # a valid stop marker: nothing to report
    if d.get("verdict") != "ok": bad.append(d["board"] + " " + d["verdict"])
    elif d.get("resumed"): res.append(d["board"])
print(";".join(bad) + "|" + ",".join(res))' 2>/dev/null)"
if [ "$tw_rc" -ne 0 ] || [ -z "$tw_bad" ]; then record tick fail "tick-watch failed (exit $tw_rc)"
elif [ -n "${tw_bad%%|*}" ]; then record tick fail "${tw_bad%%|*}"
elif [ -n "${tw_bad#*|}" ]; then record tick resumed "tick back: ${tw_bad#*|}"
else record tick ok "every tick beats"; fi

# 6c patrol (stall supervisor unit 7): `patrol.beat` is written on each patrol run, also when nothing was
# wrong. No beat file = no patrol job here: skipped. A beat older than PATROL_STALE_S (75 min) gets one
# launchctl kickstart (the first bad run); still old on the next run is one page (6 h throttle).
PATROL_BEAT="${PATROL_BEAT:-${CAPSULE_HOME:-$HOME/.local/share/capsule}/patrol.beat}"
: "${PATROL_STALE_S:=4500}"; PATROL_STAMP="$STATE_DIR/patrol-alerted"
if [ ! -f "$PATROL_BEAT" ]; then record patrol skipped "no patrol beat (no patrol job here)"
else
  pb_age="$(python3 -c 'import json,os,sys,time
p = sys.argv[1]
try: at = json.load(open(p)).get("at")
except Exception: at = None
if not isinstance(at, (int, float)): at = os.path.getmtime(p)
print(int(time.time() - at))' "$PATROL_BEAT" 2>/dev/null || echo 0)"
  pb_prior="$(fails_so_far patrol)"
  if [ "$pb_age" -le "$PATROL_STALE_S" ]; then
    if [ "$pb_prior" -ge 1 ]; then record patrol resumed "patrol beat fresh again"; else record patrol ok "beat $((pb_age / 60)) min old"; fi
  else
    if [ "$pb_prior" -eq 0 ]; then
      pb_label="${PATROL_LABEL:-$( . "$(dirname "${BASH_SOURCE[0]}")/../bin/lib_prefix.sh" 2>/dev/null && capsule_launchd_prefix 2>/dev/null || echo dev.capsule ).capsule-corp-patrol}"
      launchctl kickstart -k "gui/$(id -u)/$pb_label" >/dev/null 2>&1 || true
    elif stamp_due "$PATROL_STAMP" 21600; then
      printf 'The patrol is silent: its beat is %s min old and a restart did not help.\n' "$((pb_age / 60))" | HEAL_CLASS=page $HEAL_SEND
      : >"$PATROL_STAMP"
    fi
    record patrol fail "patrol beat $((pb_age / 60)) min old"
  fi
fi

# 6d jobs (unit 4b): each label the installer loads must be loaded; `tick-watch --jobs` bootstraps a lost
# one unless a stop marker or the park stamp holds it. The dead-man ping (one GET, unset = nothing) is here too.
pk=""; { [ -f "$STATE_DIR/park.json" ] || [ -f "$STATE_DIR/hoi-poi.json" ]; } && pk=--parked
jb_labels="$(unset CLASSIFY_PREFIX; REPO="$CAPSULE_REPO"; . "$CAPSULE_REPO/bin/lib_jobs.sh" >/dev/null 2>&1 && printf '%s ' "${PIPELINE_JOBS[@]}")"
jb_out="$(capsule_cli tick-watch --jobs $pk --labels $jb_labels 2>/dev/null)"; jb_rc=$?
jb_bad="$(printf '%s\n' "$jb_out" | python3 -c 'import json,sys
bad, fixed = [], []
for line in sys.stdin:
    try: d = json.loads(line)
    except ValueError: continue
    (bad if d["state"] == "bootstrap-failed" else fixed if d["state"] == "bootstrapped" else []).append(d["label"])
print(",".join(bad) + "|" + ",".join(fixed))' 2>/dev/null)"
if [ "$jb_rc" -ne 0 ] || [ -z "$jb_labels" ]; then record jobs fail "jobs check failed (exit $jb_rc)"
elif [ -n "${jb_bad%%|*}" ]; then record jobs fail "not loaded, bootstrap failed: ${jb_bad%%|*}"
elif [ -n "${jb_bad#*|}" ]; then record jobs resumed "bootstrapped: ${jb_bad#*|}"
else record jobs ok "every job is loaded"; fi
capsule_cli tick-watch --ping >/dev/null 2>&1 || true

# 7 outbox
OUTBOX="$STATE_DIR/outbox"; DEAD="$OUTBOX/dead"; mkdir -p "$OUTBOX" "$DEAD"
list_outbox() { find "$OUTBOX" -maxdepth 1 -type f -name '*.msg' -exec basename {} \; | sort; }
n_files="$(list_outbox | wc -l | tr -d ' ')"
if [ "$n_files" -gt 200 ]; then
  drop=$((n_files - 200)); list_outbox | head -n "$drop" | while read -r f; do rm -f "$OUTBOX/$f"; done
  n_files="$(list_outbox | wc -l | tr -d ' ')"
  $EV log infra heal "outbox dropped $drop" --payload "{\"component\":\"outbox\",\"ok\":false,\"fails\":0,\"first_fail\":null}" >/dev/null 2>&1 || true
fi
# Dead-letter unroutable files (empty to=) before touching anything else - a
# `to=` that run.sh/report.sh spooled empty (OPS_ALERT_TARGET unset) would
# otherwise fail forever on replay, stalling every
# file queued behind it and letting the cap silently drop newer alerts.
dead=0
for f in $(list_outbox); do
  to="$(sed -n 's/^to=//p' "$OUTBOX/$f" | head -1)"
  if [ -z "$to" ]; then mv "$OUTBOX/$f" "$DEAD/$f"; dead=$((dead+1)); fi
done
n_files="$(list_outbox | wc -l | tr -d ' ')"
if [ "$n_files" -eq 0 ] && [ "$dead" -eq 0 ]; then record outbox ok "empty"
elif [ "$n_files" -eq 0 ]; then record outbox fail "$dead queued, no routable target"
else
  # No Telegram probe: replay each legacy spool file into the outbox (--to/--subject do not apply).
  flushed=0
  for f in $(list_outbox); do
    sed '1,/^$/d' "$OUTBOX/$f" | HEAL_CLASS=scheduled tg_send || break
    rm -f "$OUTBOX/$f"; flushed=$((flushed+1))
  done
  if [ -z "$(list_outbox)" ]; then record outbox ok "flushed $flushed, dead $dead"; else record outbox fail "flushed $flushed, dead $dead, $(list_outbox | wc -l | tr -d ' ') left"; fi
fi
exit $RC
