#!/usr/bin/env bash
# heal.sh against fake docker/curl/launchctl/capsule-secret binaries and
# every row of the spec's table, state/heal.json counters,
# throttle stamps, exit codes. Scratch STATE_DIR and EVENTS_SPOOL.
set -u
unset CAPSULE_JOBS_DIR   # the jobs config dir comes from each case's fake home
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
fails=0; PASSED=""; check() { if eval "$2"; then echo "PASS $1"; PASSED="$PASSED|$1"; else echo "FAIL $1"; fails=$((fails+1)); fi; }
export AGENT_OPS_NO_SEND=1 EVENTS_SPOOL="$T/events.jsonl" STATE_DIR="$T/state" HOME="$T/home"
export EV="python3 $REPO/maintenance/events.py" OBS_ENV="$T/obs.env" COMPOSE_FILE="$T/dc.yml"
export COMPOSE_PROJECT_NAME=capsule-corp-test   # HOME is a temp dir: never the live project (compose_live_guard, inc_02b0bd3a)
mkdir -p "$STATE_DIR"; : >"$OBS_ENV"; : >"$COMPOSE_FILE"
FAKEBIN="$T/bin"; mkdir -p "$FAKEBIN"; ARGV="$T/argv.log"; SENT="$T/sent.log"
for b in docker curl launchctl claude; do
cat >"$FAKEBIN/$b" <<EOF
#!/usr/bin/env bash
echo "$b \$*" >>"$ARGV"
[ -f "$T/fail.$b" ] && exit "\$(cat "$T/fail.$b")"
case "$b" in
  docker) case "\$*" in
    *"config --services"*)   # what compose_services asks for: services only, never the top-level volumes map
      if [ -f "$T/fail.docker-config" ]; then echo "compose: no configuration file provided" >&2; exit 1; fi
      cat "$T/services.out" 2>/dev/null ;;
    *"ps --status running"*) cat "$T/ps.out" 2>/dev/null ;;
    *"infisical-db infisical-redis infisical"*) if [ -f "$T/fail.docker-trio" ]; then exit 1; fi ;;   # phase 1 (compose_up_infisical) - never blocked by the rest-phase marker below
    *"up -d --quiet-pull"*) if [ -f "$T/fail.docker-rest" ]; then exit 1; fi ;;
  esac ;;
  launchctl) case "\$1" in list) cat "$T/lctl.out" 2>/dev/null ;; esac ;;
  claude) cat "$T/claude.out" 2>/dev/null ;;
  *) echo '{}' ;;
esac
EOF
done
# capsule-secret gets its own script (not the generic template above): `run`
# must actually exec its trailing argv (mirrors lib_compose.sh's own test
# fixture) so compose_up_rest's docker call really happens and can be made to
# fail, and `check --identity-all` must be able to print a mix of passing and
# one failing identity line, which the generic per-binary blanket `fail.$b`
# marker (bail before any output) cannot simulate.
cat >"$FAKEBIN/capsule-secret" <<EOF
#!/usr/bin/env bash
[ "\${1:-}" = backend ] && { echo "\${FAKE_BACKEND:-infisical}"; exit 0; }   # P06
echo "capsule-secret \$*" >>"$ARGV"
case "\$1" in
  run)
    shift 2   # drop 'run' and the project name
    [ "\${1:-}" = "--" ] && shift
    "\$@"
    ;;
  export) printf 'A=1\n' ;;
  keychain-check)
    [ -f "$T/fail.keychain" ] && { echo "capsule-secret: not in keychain: \$2" >&2; exit 3; }
    [ -f "$T/nokeychain" ] && exit 4
    echo "capsule-secret: ok (keychain \$2)" ;;
  check)
    case "\$*" in
      *--identity-all*)
        rc=0
        for id in bridge compose personal work capsule-corp; do
          if [ -f "$T/fail.capsule-secret" ] && [ "\$id" = "compose" ]; then
            echo "capsule-secret: cannot read CAPSULE_CHECK from capsule-corp as compose" >&2
            rc=3
          else
            echo "capsule-secret: ok (\$id)"
          fi
        done
        exit \$rc ;;
      *) echo "capsule-secret: ok (compose)" ;;
    esac ;;
esac
EOF
chmod +x "$FAKEBIN"/*; export PATH="$FAKEBIN:$PATH" CAPSULE_SECRET_BIN="$FAKEBIN/capsule-secret"
export COMPOSE_WAIT_STEP_S=0 COMPOSE_WAIT_S=1 DOCKER_DOWN_THROTTLE_S=43200
# heal.sh sends and pauses through `python3 -m capsule` (capsule_cli, CAPSULE_PY overrides the
# interpreter): a fake interpreter records `outbox-send` bodies in $SENT and pause/unpark calls in
# $ARGV, and everything else runs the real python3. $T/fail.send fails sends, $T/fail.ctl fails
# pause/unpark; TG_FAIL_FROM=<n> fails sends from the nth on, so a failure can be aimed at one replay.
cat >"$FAKEBIN/fakepy" <<EOF
#!/usr/bin/env bash
case "\${4:-}" in
  outbox-send)
    n=\$(( \$(cat "$T/send_calls" 2>/dev/null || echo 0) + 1 )); echo "\$n" >"$T/send_calls"
    echo "outbox-send \$5" >>"$ARGV"
    if [ -f "$T/fail.send" ] || { [ -n "\${TG_FAIL_FROM:-}" ] && [ "\$n" -ge "\$TG_FAIL_FROM" ]; }; then exit 1; fi
    printf '%s\n' "\$6" >>"$SENT"; exit 0 ;;
  pause|unpark)
    echo "capsule \$4 \${*:5}" >>"$ARGV"
    [ -f "$T/fail.ctl" ] && exit 1; exit 0 ;;
esac
exec python3 "\$@"
EOF
chmod +x "$FAKEBIN/fakepy"; export CAPSULE_PY="$FAKEBIN/fakepy" SENT ARGV
send_fake() { cat >>"$SENT"; }; export -f send_fake   # only to prove an exported HEAL_SEND is ignored (C18 section)
printf 'loki\nfluent-bit\ngrafana\npostgres\ninfisical\ninfisical-db\ninfisical-redis\n' >"$T/ps.out"
# The compose fixture carries a top-level `volumes:` map whose `data:` line
# matches the pattern heal.sh used to grep the YAML with - proof that the
# stack check now takes its service list from compose itself (services.out,
# what the fake docker answers `config --services` with) and cannot count a
# volume as a missing service. grafana is listed first so the "missing
# service" assertion below (which checks only that grafana is named) matches
# regardless of what else drops out of ps.out at the same time.
printf 'services:\n  grafana:\n    image: x\n  infisical:\n    image: x\nvolumes:\n  data:\n' >"$COMPOSE_FILE"
printf 'grafana\ninfisical\n' >"$T/services.out"
echo '{"loggedIn": true}' >"$T/claude.out"
HEAL="$REPO/maintenance/heal.sh"
ev_last() { grep "\"component\": \"$1\"" "$EVENTS_SPOOL" | tail -1; }
comp_state() { python3 -c 'import json,sys; d=json.load(open(sys.argv[1])).get(sys.argv[2],{}); print(d.get("fails",0), d.get("first_fail"))' "$STATE_DIR/heal.json" "$1"; }

# --- all ok ---
: >"$ARGV"; out="$(bash "$HEAL" 2>&1)"; rc=$?
check "all ok exits 0" '[ $rc -eq 0 ]'
for c in docker infisical stack secrets; do
  check "event for $c is ok" 'ev_last '"$c"' | grep -q "\"text\": \"'"$c"' ok"'
done
check "heal.json has zero fails for docker" '[ "$(comp_state docker)" = "0 None" ]'
check "stack check ran compose_up_all and ps" 'grep -q "docker compose --env-file $OBS_ENV -f $COMPOSE_FILE up -d --quiet-pull infisical-db" "$ARGV" && grep -q "docker compose .* ps --status running" "$ARGV"'
check "secrets check uses identity-all with no wait" 'grep -q "capsule-secret check --identity-all" "$ARGV"'
check "slack-token check is ok and asks the keychain path" 'ev_last slack-token | grep -q "slack-token ok" && grep -q "capsule-secret keychain-check SLACK_BOT_TOKEN" "$ARGV"'
touch "$T/fail.keychain"; : >"$SENT"
bash "$HEAL" >/dev/null 2>&1; rc=$?
check "heal exits 0 when only slack-token fails" '[ $rc -eq 0 ] && [ "$(comp_state slack-token | cut -d" " -f1)" -ge 1 ]'
echo 1 >"$T/fail.docker"
bash "$HEAL" >/dev/null 2>&1
check "slack-token missing is a fail" '[ "$(comp_state slack-token | cut -d" " -f1)" -ge 2 ] && ev_last slack-token | grep -q "not in the keychain"'
check "slack-token check still runs with docker down" 'ev_last docker | grep -q "docker fail"'
check "slack-token missing never pages by itself" '! grep -qi "slack" "$SENT"'
unlink "$T/fail.keychain"; touch "$T/nokeychain"; bash "$HEAL" >/dev/null 2>&1
check "no keychain here is skipped, not failed" 'ev_last slack-token | grep -q "skipped" && [ "$(comp_state slack-token | cut -d" " -f1)" = 0 ]'
unlink "$T/nokeychain"; unlink "$T/fail.docker"; bash "$HEAL" >/dev/null 2>&1
: >"$SENT"; bash "$HEAL" >/dev/null 2>&1
check "no telegram sent when all ok" '[ ! -s "$SENT" ]'
check "stack check took its service list from compose config --services" 'grep -q "docker compose .* config --services" "$ARGV"'
check "the top-level volumes key is not counted as a missing service" '! ev_last stack | grep -q "data"'

# --- docker down: the first tick is silent (Docker Desktop may still be
# starting after a reboot), the second alerts, the third is throttled ---
echo 1 >"$T/fail.docker"; : >"$SENT"
out="$(bash "$HEAL" 2>&1)"; rc=$?
check "docker down exits 1" '[ $rc -eq 1 ]'
check "the first docker failure is silent and leaves no stamp" '[ ! -s "$SENT" ] && [ ! -f "$STATE_DIR/docker-down-alerted" ]'
check "docker fail counted with first_fail" '[ "$(comp_state docker | cut -d" " -f1)" = "1" ] && [ "$(comp_state docker | cut -d" " -f2)" != "None" ]'
for c in infisical stack secrets; do check "$c skipped when docker is down" 'ev_last '"$c"' | grep -q "\"text\": \"'"$c"' skipped"'; done
out="$(bash "$HEAL" 2>&1)"
check "the second consecutive docker failure alerts once and stamps, fails=2" '[ "$(grep -c "Docker is not running" "$SENT")" = 1 ] && [ -f "$STATE_DIR/docker-down-alerted" ] && [ "$(comp_state docker | cut -d" " -f1)" = "2" ]'
out="$(bash "$HEAL" 2>&1)"
check "third tick throttled: no second alert, fails=3" '[ "$(grep -c "Docker is not running" "$SENT")" = 1 ] && [ "$(comp_state docker | cut -d" " -f1)" = "3" ]'
rm -f "$T/fail.docker"; out="$(bash "$HEAL" 2>&1)"; rc=$?
check "docker back after an alert: recovery message, stamp gone, counter reset" '[ $rc -eq 0 ] && grep -q "Docker is back" "$SENT" && [ ! -f "$STATE_DIR/docker-down-alerted" ] && [ "$(comp_state docker)" = "0 None" ]'

# --- one failing tick that recovers before the second: nothing is sent at
# all, neither the alert nor a recovery line nobody was told about ---
echo 1 >"$T/fail.docker"; : >"$SENT"
out="$(bash "$HEAL" 2>&1)"
rm -f "$T/fail.docker"; out="$(bash "$HEAL" 2>&1)"; rc=$?
check "recovery without a preceding alert says nothing" '[ $rc -eq 0 ] && [ ! -s "$SENT" ] && [ ! -f "$STATE_DIR/docker-down-alerted" ]'

# --- docker quit on purpose by docker-idle (D195): a skip, never a page or a failure ---
echo 1 >"$T/fail.docker"; : >"$SENT"; : >"$STATE_DIR/docker-idle-quit"
out="$(bash "$HEAL" 2>&1)"; rc=$?; out2="$(bash "$HEAL" 2>&1)"; rc2=$?
check "docker down after a docker-idle quit: exit 0 twice, no page, no failure counted" '[ $rc -eq 0 ] && [ $rc2 -eq 0 ] && [ ! -s "$SENT" ] && [ ! -f "$STATE_DIR/docker-down-alerted" ] && [ "$(comp_state docker | cut -d" " -f1)" = "0" ]'
check "the docker line says skipped: quit by docker-idle" 'ev_last docker | grep -q "docker skipped: quit by docker-idle (D195)"'
rm -f "$T/fail.docker"; out="$(bash "$HEAL" 2>&1)"
check "docker answering within 2 min of the quit keeps the stamp" '[ -f "$STATE_DIR/docker-idle-quit" ]'
touch -t 202601010000 "$STATE_DIR/docker-idle-quit"; out="$(bash "$HEAL" 2>&1)"
check "docker answering with an old quit stamp clears it" '[ ! -f "$STATE_DIR/docker-idle-quit" ]'
echo 1 >"$T/fail.docker"; : >"$SENT"; out="$(bash "$HEAL" 2>&1)"; out="$(bash "$HEAL" 2>&1)"
check "a later real outage (no stamp) still pages" '[ "$(grep -c "Docker is not running" "$SENT")" = 1 ]'
rm -f "$T/fail.docker" "$STATE_DIR/docker-down-alerted"; out="$(bash "$HEAL" 2>&1)"; : >"$SENT"

# --- docker down again, then the throttle stamp ages past 12 h: alerts again ---
echo 1 >"$T/fail.docker"; : >"$SENT"
out="$(bash "$HEAL" 2>&1)"; out="$(bash "$HEAL" 2>&1)"
check "docker down again sends its first alert on the second tick" '[ "$(grep -c "Docker is not running" "$SENT")" = 1 ]'
touch -t 202601010000 "$STATE_DIR/docker-down-alerted"
out="$(bash "$HEAL" 2>&1)"
check "a stamp aged past 12h sends a second alert" '[ "$(grep -c "Docker is not running" "$SENT")" = 2 ]'
rm -f "$T/fail.docker"; out="$(bash "$HEAL" 2>&1)"; : >"$SENT"

# --- infisical not answering: stack + secrets skipped, no telegram ---
echo 22 >"$T/fail.curl"; : >"$SENT"
out="$(bash "$HEAL" 2>&1)"; rc=$?
check "infisical down exits 1, no telegram" '[ $rc -eq 1 ] && [ ! -s "$SENT" ] && ev_last infisical | grep -q "infisical fail"'
check "stack and secrets skipped" 'ev_last stack | grep -q "stack skipped" && ev_last secrets | grep -q "secrets skipped"'
# The failing phase's own line is the fail reason, so the two infisical
# failures are told apart: the wait timed out here...
check "a wait timeout names the wait phase" \
  'ev_last infisical | grep -q "infisical fail: compose: wait failed (infisical not answering after 1s)"'
rm -f "$T/fail.curl"

# ...and here the trio never came up at all.
echo 1 >"$T/fail.docker-trio"; : >"$SENT"; out="$(bash "$HEAL" 2>&1)"; rc=$?
check "a failed trio bring-up names the compose-up phase, not the wait" \
  '[ $rc -eq 1 ] && ev_last infisical | grep -q "infisical fail: compose: infisical failed (docker compose up exit 1)" && ! ev_last infisical | grep -q "wait failed"'
check "a failed trio bring-up skips stack and secrets and sends nothing" \
  '[ ! -s "$SENT" ] && ev_last stack | grep -q "stack skipped" && ev_last secrets | grep -q "secrets skipped"'
rm -f "$T/fail.docker-trio"

# --- stack: a service missing from ps ---
printf 'loki\n' >"$T/ps.out"; out="$(bash "$HEAL" 2>&1)"; rc=$?
check "missing service fails the stack check naming it" '[ $rc -eq 1 ] && ev_last stack | grep -q "stack fail: not running: grafana"'
printf 'infisical\ngrafana\n' >"$T/ps.out"

# --- P06: secrets profile off (file/env backend): no infisical trio, the stack phase still runs ---
: >"$ARGV"
out="$(COMPOSE_PROFILES= bash "$HEAL" 2>&1)"; rc=$?
check "profile off: heal exits 0 and records infisical skipped" '[ $rc -eq 0 ] && ev_last infisical | grep -q "infisical skipped"'
check "profile off: no infisical trio up, no wait; the rest still comes up" \
  '! grep -q "infisical-db infisical-redis infisical" "$ARGV" && grep -qE "up -d --quiet-pull\$" "$ARGV"'
check "profile off via the file backend (not just an explicit empty profile)" \
  ': >"$ARGV"; out="$(env -u COMPOSE_PROFILES FAKE_BACKEND=file bash "$HEAL" 2>&1)"; ev_last infisical | grep -q "infisical skipped" && ! grep -q "infisical-db infisical-redis infisical" "$ARGV"'
# --- stack: compose_up_rest itself fails, through capsule-secret's own exec of its argv ---
: >"$ARGV"
echo 1 >"$T/fail.docker-rest"
out="$(bash "$HEAL" 2>&1)"; rc=$?
check "compose_up_rest failure fails the stack check" '[ $rc -eq 1 ] && ev_last stack | grep -q "stack fail"'
check "phase 1 (direct) and phase 3 (through capsule-secret run) both actually ran docker compose up" \
  'grep -q "docker compose --env-file $OBS_ENV -f $COMPOSE_FILE up -d --quiet-pull infisical-db" "$ARGV" && grep -qE "up -d --quiet-pull\$" "$ARGV"'
rm -f "$T/fail.docker-rest"

# --- stack: `config --services` itself fails, or answers nothing. Both must
# be a stack FAIL: an empty service list makes the missing-service loop run
# zero times, which used to be recorded as "every service running" ---
: >"$ARGV"; echo 1 >"$T/fail.docker-config"
out="$(bash "$HEAL" 2>&1)"; rc=$?
check "a failed config --services fails the stack check, it never reports ok" \
  '[ $rc -eq 1 ] && ev_last stack | grep -q "stack fail: cannot list services (docker compose config failed)" && ! ev_last stack | grep -q "every service running"'
rm -f "$T/fail.docker-config"
: >"$T/services.out"; out="$(bash "$HEAL" 2>&1)"; rc=$?
check "an empty service list fails the stack check too" \
  '[ $rc -eq 1 ] && ev_last stack | grep -q "stack fail: cannot list services"'
printf 'grafana\ninfisical\n' >"$T/services.out"
printf 'infisical\ngrafana\n' >"$T/ps.out"

# --- secrets: one identity fails check --identity-all; the reason names it ---
: >"$ARGV"
echo 1 >"$T/fail.capsule-secret"
out="$(bash "$HEAL" 2>&1)"; rc=$?
check "secrets fail exits 1 and names the failing identity, not a passing one" \
  '[ $rc -eq 1 ] && ev_last secrets | grep -q "secrets fail:" && ev_last secrets | grep -q "compose"'
rm -f "$T/fail.capsule-secret"

# --- the Telegram gateway row is retired (D229/D233) ---
check "heal no longer references the telegram gateway label or log" '! grep -q "gateway-telegram\|No messaging platforms\|GATEWAY_LABEL" "$HEAL"'
: >"$ARGV"; : >"$T/lctl.out"; out="$(bash "$HEAL" 2>&1)"
check "no gateway event and no kickstart, whatever launchctl lists" '[ -z "$(ev_last gateway)" ] && ! grep -q kickstart "$ARGV"'

# --- claude login ---
echo '{"loggedIn": false}' >"$T/claude.out"; : >"$SENT"; : >"$ARGV"
out="$(bash "$HEAL" 2>&1)"; rc=$?
check "logout: exit 1, one alert, stamp, pipeline paused with reason" '[ $rc -eq 1 ] && grep -q "Claude Code is logged out - run /login in a terminal" "$SENT" && [ -f "$STATE_DIR/claude-logout-alerted" ] && grep -q "capsule pause --reason claude logged out" "$ARGV" && python3 -c "import json,sys; d=json.load(open(sys.argv[1])); assert d[\"reason\"]==\"claude logged out\" and d[\"paused_until\"]>0" "$STATE_DIR/usage-guard.json"'
out="$(bash "$HEAL" 2>&1)"
check "second logout tick: throttled, no second pause call" '[ "$(grep -c "logged out" "$SENT")" = 1 ] && [ "$(grep -c "pause --reason" "$ARGV")" = 1 ]'
echo '{"loggedIn": true}' >"$T/claude.out"; : >"$ARGV"; out="$(bash "$HEAL" 2>&1)"; rc=$?
check "login back: resumed, guard cleared, stamp gone" '[ $rc -eq 0 ] && grep -q "capsule unpark --reason claude logged out" "$ARGV" && ev_last claude-login | grep -q "claude-login resumed" && [ ! -f "$STATE_DIR/claude-logout-alerted" ] && python3 -c "import json,sys; d=json.load(open(sys.argv[1])); assert d[\"paused_until\"] is None" "$STATE_DIR/usage-guard.json"'
python3 -c 'import json,sys,time; json.dump({"paused_until": time.time()+999, "announced": True}, open(sys.argv[1],"w"))' "$STATE_DIR/usage-guard.json"
: >"$ARGV"; out="$(bash "$HEAL" 2>&1)"
check "a usage-guard pause (no reason) is never touched by heal" '! grep -q "capsule unpark" "$ARGV" && python3 -c "import json,sys; d=json.load(open(sys.argv[1])); assert d[\"paused_until\"]>0" "$STATE_DIR/usage-guard.json"'
echo '{"loggedIn": false}' >"$T/claude.out"; : >"$ARGV"; out="$(bash "$HEAL" 2>&1)"
check "logout tick during a reasonless usage-guard pause does not call pause" '! grep -q "pause --reason" "$ARGV"'
rm -f "$STATE_DIR/usage-guard.json" "$STATE_DIR/claude-logout-alerted" "$STATE_DIR/session-limit-until"

# --- claude login: claude auth status unreadable (empty/garbage output) ---
: >"$T/claude.out"; : >"$SENT"; : >"$ARGV"
out="$(bash "$HEAL" 2>&1)"
check "unknown auth status: fail recorded, no pause, no telegram" \
  'ev_last claude-login | grep -q "claude-login fail: claude auth status unreadable" && [ ! -s "$SENT" ] && ! grep -q "pause --reason" "$ARGV" && [ ! -f "$STATE_DIR/usage-guard.json" ]'

# --- claude login: the capsule pause/unpark call itself can fail; heal must
# not swallow that (still writes guard state so it doesn't re-attempt forever
# without noticing, but says so on stderr and in the event text) ---
echo '{"loggedIn": false}' >"$T/claude.out"; : >"$SENT"; : >"$ARGV"
echo 1 >"$T/fail.ctl"
err="$(bash "$HEAL" 2>&1 1>/dev/null)"
check "capsule pause failure warns on stderr and taints the fail text" \
  'grep -q "heal: WARNING capsule pause failed" <<<"$err" && ev_last claude-login | grep -q "claude-login fail: loggedIn=false (capsule pause failed)" && python3 -c "import json,sys; d=json.load(open(sys.argv[1])); assert d[\"reason\"]==\"claude logged out\"" "$STATE_DIR/usage-guard.json"'
echo '{"loggedIn": true}' >"$T/claude.out"; : >"$ARGV"
err="$(bash "$HEAL" 2>&1 1>/dev/null)"
check "capsule resume failure warns on stderr, records it, and leaves the guard reason for a retry" \
  'grep -q "heal: WARNING capsule resume failed" <<<"$err" && ev_last claude-login | grep -q "claude-login resumed: logged in again (capsule resume failed, retry next tick)" && python3 -c "import json,sys; d=json.load(open(sys.argv[1])); assert d[\"reason\"]==\"claude logged out\"" "$STATE_DIR/usage-guard.json"'
rm -f "$T/fail.ctl"; : >"$ARGV"; out="$(bash "$HEAL" 2>&1)"
check "a retried resume (capsule back up) finally clears the guard" \
  'grep -q "capsule unpark" "$ARGV" && python3 -c "import json,sys; d=json.load(open(sys.argv[1])); assert d[\"paused_until\"] is None" "$STATE_DIR/usage-guard.json"'
rm -f "$STATE_DIR/usage-guard.json" "$STATE_DIR/claude-logout-alerted" "$STATE_DIR/session-limit-until"

# --- outbox: each spool file replays into the outbox oldest first, body only (no Telegram probe, no --to/--subject) ---
mkdir -p "$STATE_DIR/outbox"
printf 'to=telegram:-100:10\nat=2026-09-08T09:10:00+07:00\nsubject=Second Brain\n\nfirst body\n' >"$STATE_DIR/outbox/1000-report.msg"
printf 'to=telegram:-100:10\nat=2026-09-08T09:20:00+07:00\nsubject=\n\nsecond body\n' >"$STATE_DIR/outbox/2000-nightly.msg"
: >"$SENT"; : >"$ARGV"; out="$(bash "$HEAL" 2>&1)"
check "outbox flushed oldest first, bodies only" '[ "$(grep -c "body" "$SENT")" = 2 ] && [ "$(grep -n "first body" "$SENT" | head -1 | cut -d: -f1)" -lt "$(grep -n "second body" "$SENT" | head -1 | cut -d: -f1)" ] && ! grep -q "delayed from\|Second Brain" "$SENT" && [ -z "$(ls "$STATE_DIR/outbox" | grep -v "^dead$")" ]'
check "outbox event says how many" 'ev_last outbox | grep -q "outbox ok: flushed 2, dead 0"'
check "outbox replay sends once per file, class scheduled, no probe" '[ "$(grep -c "^outbox-send " "$ARGV")" = 2 ] && [ "$(grep -cx "outbox-send scheduled" "$ARGV")" = 2 ]'

# --- outbox: a file with an empty to= is dead-lettered, not sent, and does
# not stall the routable files queued behind it ---
printf 'to=\nat=2026-09-08T08:00:00+07:00\nsubject=\n\nno target\n' >"$STATE_DIR/outbox/500-orphan.msg"
printf 'to=telegram:-100:10\nat=2026-09-08T09:10:00+07:00\nsubject=Second Brain\n\nfirst body\n' >"$STATE_DIR/outbox/1000-report.msg"
printf 'to=telegram:-100:10\nat=2026-09-08T09:20:00+07:00\nsubject=\n\nsecond body\n' >"$STATE_DIR/outbox/2000-nightly.msg"
: >"$SENT"; out="$(bash "$HEAL" 2>&1)"
check "an unroutable file ahead of two routable ones: routable flush in order, unroutable dead-lettered" \
  '[ "$(grep -n "first body" "$SENT" | head -1 | cut -d: -f1)" -lt "$(grep -n "second body" "$SENT" | head -1 | cut -d: -f1)" ] && [ -f "$STATE_DIR/outbox/dead/500-orphan.msg" ] && ! grep -q "no target" "$SENT" && [ -z "$(ls "$STATE_DIR/outbox" | grep -v "^dead$")" ]'
check "outbox event names both the flushed and dead counts" 'ev_last outbox | grep -q "outbox ok: flushed 2, dead 1"'
rm -rf "$STATE_DIR/outbox/dead"

# --- outbox: nothing routable at all is a fail, not a silent no-op, and
# still dead-letters the orphan rather than looping on it forever ---
printf 'to=\nat=2026-09-08T08:00:00+07:00\nsubject=\n\nnobody home\n' >"$STATE_DIR/outbox/600-orphan2.msg"
: >"$SENT"; out="$(bash "$HEAL" 2>&1)"
check "no routable target at all: dead-lettered and fails without sending" \
  '[ -f "$STATE_DIR/outbox/dead/600-orphan2.msg" ] && ev_last outbox | grep -q "outbox fail: 1 queued, no routable target" && [ ! -s "$SENT" ]'
rm -rf "$STATE_DIR/outbox/dead"

# --- outbox: nothing a failed send does may add a file to the queue -------
# (a) the first replay fails: both originals stay, nothing is added.
rm -f "$T/send_calls"
printf 'to=telegram:-100:10\nat=2026-09-08T09:10:00+07:00\nsubject=Second Brain\n\nfirst body\n' >"$STATE_DIR/outbox/1000-report.msg"
printf 'to=telegram:-100:10\nat=2026-09-08T09:20:00+07:00\nsubject=\n\nsecond body\n' >"$STATE_DIR/outbox/2000-nightly.msg"
: >"$SENT"; echo 1 >"$T/fail.send"; out="$(bash "$HEAL" 2>&1)"
check "a failed first replay leaves exactly the two originals, no third file" \
  '[ "$(ls "$STATE_DIR/outbox" | grep -v "^dead$" | tr "\n" " ")" = "1000-report.msg 2000-nightly.msg " ] && [ ! -s "$SENT" ] && ev_last outbox | grep -q "outbox fail: flushed 0, dead 0, 2 left"'
rm -f "$T/fail.send"
# (b) the SECOND replay fails (first replay ok): the flushed file is
# gone, the failed one is still there once, and nothing new was added.
rm -f "$T/send_calls"; : >"$SENT"
out="$(TG_FAIL_FROM=2 bash "$HEAL" 2>&1)"
check "a failed replay leaves exactly its own file behind, nothing duplicated" \
  '[ "$(ls "$STATE_DIR/outbox" | grep -v "^dead$" | tr "\n" " ")" = "2000-nightly.msg " ] && grep -q "first body" "$SENT" && ! grep -q "second body" "$SENT" && ev_last outbox | grep -q "outbox fail: flushed 1, dead 0, 1 left"'
rm -f "$T/send_calls"; : >"$SENT"; out="$(bash "$HEAL" 2>&1)"
check "the next tick finishes the flush from where it stopped" \
  'grep -q "second body" "$SENT" && [ -z "$(ls "$STATE_DIR/outbox" | grep -v "^dead$")" ] && ev_last outbox | grep -q "outbox ok: flushed 1, dead 0"'
rm -f "$STATE_DIR/outbox"/*.msg

for i in $(seq 1 205); do printf 'to=t\nat=2026-09-08T00:00:00+07:00\nsubject=\n\nb%s\n' "$i" >"$STATE_DIR/outbox/$((3000+i))-x.msg"; done
rm -f "$T/send_calls"; echo 1 >"$T/fail.send"; out="$(bash "$HEAL" 2>&1)"
check "over the cap: oldest dropped to 200 with one event, nothing sent while sends fail" '[ "$(ls "$STATE_DIR/outbox" | grep -v "^dead$" | wc -l | tr -d " ")" = "200" ] && [ ! -e "$STATE_DIR/outbox/3001-x.msg" ] && grep -q "outbox dropped 5" "$EVENTS_SPOOL" && ev_last outbox | grep -q "outbox fail"'
rm -f "$T/fail.send"; rm -f "$STATE_DIR"/outbox/*.msg; rm -rf "$STATE_DIR/outbox/dead"

# --- C18: pauses via guard_state and queues via outbox-send, against the real capsule CLI and a scratch board ---
unset CAPSULE_PY
rm -f "$STATE_DIR/usage-guard.json" "$STATE_DIR/claude-logout-alerted" "$STATE_DIR/session-limit-until"
export CAPSULE_HOME="$T/chome"
python3 -c 'import sys; sys.path.insert(0, sys.argv[1]); from capsule import db; db.create_board(sys.argv[2] + "/boards/b1/kanban.db")' "$REPO" "$CAPSULE_HOME"
python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); d["board"]="b1"; json.dump(d,open(sys.argv[2],"w"))' "$REPO/capsule/config.example.json" "$CAPSULE_HOME/config.json"
pause_state() { python3 -c 'import sys; sys.path.insert(0, sys.argv[1]); from capsule import db; c=db.connect(sys.argv[2]); v,_=db.guard_get(c,"pause:board"); import time; print("held" if v and v["until"]>time.time() else "free", (v or {}).get("reason"))' "$REPO" "$CAPSULE_HOME/boards/b1/kanban.db"; }
echo '{"loggedIn": false}' >"$T/claude.out"; : >"$ARGV"; out="$(bash "$HEAL" 2>&1)"
check "C18 logout: board paused with the reason" '[ "$(pause_state)" = "held claude logged out" ] && ! grep -q "capsule pause" "$ARGV"'
echo '{"loggedIn": true}' >"$T/claude.out"; : >"$ARGV"; out="$(bash "$HEAL" 2>&1)"
check "C18 login back: board unparked, event kept" '[ "$(pause_state)" = "free claude logged out" ] && ev_last claude-login | grep -q "claude-login resumed: logged in again, pipeline resumed"'
outbox_rows() { python3 -c 'import sys; sys.path.insert(0, sys.argv[1]); from capsule import db; c=db.connect(sys.argv[2]); print(c.execute("SELECT count(*) FROM outbox WHERE body LIKE ?", ("%logged out%",)).fetchone()[0])' "$REPO" "$CAPSULE_HOME/boards/b1/kanban.db"; }
rm -f "$STATE_DIR/usage-guard.json" "$STATE_DIR/claude-logout-alerted" "$STATE_DIR/session-limit-until"
echo '{"loggedIn": false}' >"$T/claude.out"; : >"$ARGV"; : >"$SENT"
rows_before="$(outbox_rows)"
out="$(HEAL_SEND=send_fake bash "$HEAL" 2>&1)"
check "C18 logout message lands as one new outbox row on b1, not via an exported HEAL_SEND" \
  '[ "$(outbox_rows)" -eq "$((rows_before + 1))" ] && ! grep -q "logged out" "$SENT"'
echo '{"loggedIn": true}' >"$T/claude.out"; out="$(bash "$HEAL" 2>&1)"
check "C18 outbox-send queues a row on the configured board" \
  'PYTHONPATH="$REPO" python3 -m capsule outbox-send paused "heal test" >/dev/null && python3 -c "import sys; sys.path.insert(0, sys.argv[1]); from capsule import db; c=db.connect(sys.argv[2]); sys.exit(0 if c.execute(\"SELECT 1 FROM outbox WHERE body LIKE ?\", (\"%heal test%\",)).fetchone() else 1)" "$REPO" "$CAPSULE_HOME/boards/b1/kanban.db"'
python3 -c 'import sys; sys.path.insert(0, sys.argv[1]); from capsule import db, outbox; c=db.connect(sys.argv[2]); outbox.set_slack(c, True)' "$REPO" "$CAPSULE_HOME/boards/b1/kanban.db"
: >"$STATE_DIR/docker-down-alerted"; out="$(bash "$HEAL" 2>&1)"
check "C18 Docker-back row carries class done, not incident" \
  'python3 -c "import sys; sys.path.insert(0, sys.argv[1]); from capsule import db; c=db.connect(sys.argv[2]); b=[r[0] for r in c.execute(\"SELECT body FROM outbox WHERE channel=? AND body LIKE ?\", (\"file\", \"%Docker is back%\"))]; k=c.execute(\"SELECT 1 FROM outbox WHERE channel=? AND body LIKE ?\", (\"slack\", \"%Docker is back%\")).fetchone(); sys.exit(0 if b and not k and all(x.startswith(\"✅\") and \"\U0001F6A8\" not in x for x in b) else 1)" "$REPO" "$CAPSULE_HOME/boards/b1/kanban.db"'
rm -f "$STATE_DIR/usage-guard.json" "$STATE_DIR/claude-logout-alerted" "$STATE_DIR/session-limit-until"
echo '{"loggedIn": false}' >"$T/claude.out"; : >"$ARGV"; out="$(bash "$HEAL" 2>&1)"
check "C18 heal's own logout pause holds past 24 h" \
  'python3 -c "import sys,time; sys.path.insert(0, sys.argv[1]); from capsule import db; c=db.connect(sys.argv[2]); v,_=db.guard_get(c,\"pause:board\"); sys.exit(0 if v[\"reason\"]==\"claude logged out\" and v[\"until\"]>time.time()+86400*30 else 1)" "$REPO" "$CAPSULE_HOME/boards/b1/kanban.db"'
echo '{"loggedIn": true}' >"$T/claude.out"; out="$(bash "$HEAL" 2>&1)"
rm -f "$STATE_DIR/outbox"/*.msg; printf 'to=telegram:-100:10\nat=2026-09-08T09:10:00+07:00\nsubject=\n\nspool replay body\n' >"$STATE_DIR/outbox/1000-x.msg"
out="$(bash "$HEAL" 2>&1)"
check "C18 spool replay: one .msg becomes an outbox row on b1, spool empty, 'outbox ok: flushed 1'" \
  '[ -z "$(ls "$STATE_DIR/outbox" | grep -v "^dead$")" ] && ev_last outbox | grep -q "outbox ok: flushed 1" && python3 -c "import sys; sys.path.insert(0, sys.argv[1]); from capsule import db; c=db.connect(sys.argv[2]); sys.exit(0 if c.execute(\"SELECT 1 FROM outbox WHERE body LIKE ?\", (\"%spool replay body%\",)).fetchone() else 1)" "$REPO" "$CAPSULE_HOME/boards/b1/kanban.db"'
check "C18 spool replay file row (log-only: no slack row, raw body, no incident glyph)" \
  'python3 -c "import sys; sys.path.insert(0, sys.argv[1]); from capsule import db; c=db.connect(sys.argv[2]); b=[r[0] for r in c.execute(\"SELECT body FROM outbox WHERE channel=? AND body LIKE ?\", (\"file\", \"%spool replay body%\"))]; k=c.execute(\"SELECT 1 FROM outbox WHERE channel=? AND body LIKE ?\", (\"slack\", \"%spool replay body%\")).fetchone(); sys.exit(0 if b and not k and all(\"\U0001F6A8\" not in x for x in b) else 1)" "$REPO" "$CAPSULE_HOME/boards/b1/kanban.db"'
long_body="$(printf 'long line %.0s' $(seq 1 25))
second line TAILMARK"
rm -f "$STATE_DIR/outbox"/*.msg; printf 'to=telegram:-100:10\nat=2026-09-08T09:10:00+07:00\nsubject=\n\n%s\n' "$long_body" >"$STATE_DIR/outbox/1001-x.msg"
out="$(bash "$HEAL" 2>&1)"
check "C18 spool replay keeps a body over 150 chars whole in the outbox row" \
  'python3 -c "import sys; sys.path.insert(0, sys.argv[1]); from capsule import db; c=db.connect(sys.argv[2]); sys.exit(0 if c.execute(\"SELECT 1 FROM outbox WHERE channel=? AND body LIKE ?\", (\"file\", \"%TAILMARK%\")).fetchone() else 1)" "$REPO" "$CAPSULE_HOME/boards/b1/kanban.db"'
rm -f "$STATE_DIR/outbox"/*.msg; printf 'to=telegram:-100:10\nat=2026-09-08T09:10:00+07:00\nsubject=\n\n\nBLANKLEAD body\n' >"$STATE_DIR/outbox/1002-x.msg"
out="$(bash "$HEAL" 2>&1)"
check "C18 spool replay: a body with a blank first line flushes" \
  '[ -z "$(ls "$STATE_DIR/outbox" | grep -v "^dead$")" ] && python3 -c "import sys; sys.path.insert(0, sys.argv[1]); from capsule import db; c=db.connect(sys.argv[2]); sys.exit(0 if c.execute(\"SELECT 1 FROM outbox WHERE body LIKE ?\", (\"%BLANKLEAD%\",)).fetchone() else 1)" "$REPO" "$CAPSULE_HOME/boards/b1/kanban.db"'
check "C18 outbox-send accepts an empty body" 'PYTHONPATH="$REPO" python3 -m capsule outbox-send paused "" >/dev/null'
check "C18 outbox-send refuses an unknown class" '! PYTHONPATH="$REPO" python3 -m capsule outbox-send nonsense "x" 2>/dev/null'
echo "" >"$T/lctl.out"; : >"$ARGV"; out="$(bash "$HEAL" 2>&1)"; rc=$?
check "no gateway event, rc 0, no launchctl list or kickstart" '! ev_last gateway | grep -q "gateway fail" && [ -z "$(ev_last gateway)" ] && [ $rc -eq 0 ] && ! grep -q "^launchctl list" "$ARGV" && ! grep -q kickstart "$ARGV"'

# Z1: contracts/intent/heal.jsonl replayed here, no extra run - each example names a check above that must have passed.
check "every contracts/intent/heal.jsonl example names a check that passed" \
  'python3 -c "import json,sys; rows=[json.loads(l) for l in open(sys.argv[1]) if l.strip()]; got=sys.argv[2].split(\"|\"); sys.exit(0 if rows and all(r[\"expect\"][\"pass\"] in got for r in rows) else 1)" "$REPO/contracts/intent/heal.jsonl" "$PASSED"'

echo; if [ $fails -eq 0 ]; then echo "test_heal: all passed"; else echo "test_heal: $fails failed"; exit 1; fi
