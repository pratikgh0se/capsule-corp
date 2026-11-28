#!/usr/bin/env bash
# hoi-poi.sh against fake launchctl/docker/curl/capsule-secret/bootstrap/run.sh.
# Never calls a real binary, never touches ~, never sends.
set -u
unset CAPSULE_JOBS_DIR   # the jobs config dir comes from each case's fake home
# P03: these cases use the dev.capsule test prefix, except the notes-vault and ollama-serve labels, whose templates hard-code the legacy prefix
export CAPSULE_LAUNCHD_PREFIX=dev.capsule
perm() { if stat -c %a / >/dev/null 2>&1; then stat -c %a "$1"; else stat -f %Lp "$1"; fi; }
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
# hermetic: the committed launchd/unloaded (now listing the automation gateway, C02) must not shape these checks
export CAPSULE_LAUNCHD_UNLOADED="$T/no-unloaded-file"
# the obsidian-*/ollama-serve labels carry the overlay prefix lib_jobs reads off the templates
OP="$(bash -c 'REPO="$1"; . "$1/bin/lib_jobs.sh"; printf %s "$_OP"' _ "$REPO")"
fails=0; check() { if eval "$2"; then echo "PASS $1"; else echo "FAIL $1"; fails=$((fails+1)); fi; }

# --- lib_jobs ---
out="$(bash -c 'REPO="$1"; . "$1/bin/lib_jobs.sh"; printf "%s\n" "${PIPELINE_JOBS[@]}"' _ "$REPO")"
check "PIPELINE_JOBS excludes ollama-serve" '! grep -q ollama-serve <<<"$out"'
expected_n="$(bash -c 'REPO="$1"; . "$1/bin/lib_jobs.sh"; echo $(( ${#DISPATCHING[@]} + ${#CUSTODIAL[@]} - 1 ))' _ "$REPO")"
check "PIPELINE_JOBS has every dispatching and custodial job" '[ "$(wc -l <<<"$out" | tr -d " ")" = "'"$expected_n"'" ]'
out="$(bash -c 'REPO="$1"; . "$1/bin/lib_jobs.sh"; classify_check' _ "$REPO")"
check "every plist in launchd/templates is classified" '[ -z "$out" ]'

# --- hoi-poi.sh fakes: launchctl/docker/curl/capsule-secret/bootstrap.sh/run.sh ---
mkdir -p "$T/bin" "$T/state"
for b in launchctl docker curl capsule-secret bootstrap.sh run.sh; do
  cat >"$T/bin/$b" <<'EOF2'
#!/usr/bin/env bash
me="$(basename "$0")"
[ "$me" = capsule-secret ] && [ "${1:-}" = backend ] && { echo infisical; exit 0; }  # P06
printf '%s\n' "$*" >>"$FAKE_LOG/$me.log"
# all.log: every fake's call, in invocation order, prefixed with its own
# name - the down-ordering assertion (heal before any bootout) needs a
# single shared timeline; per-binary .log files alone can't show interleaving.
printf '%s %s\n' "$me" "$*" >>"$FAKE_LOG/all.log"
# capsule-secret has two independent call shapes (compose_up_rest's `run ...
# -- <cmd>` wrapper, and the standalone `check --identity-all` secrets
# check), so its own rc override is split from the generic one below - a
# single capsule-secret.rc would fail both the same way, and the "secrets
# check fails but the stack stays up" test needs to fail only the latter.
if [ "$me" = capsule-secret ]; then
  if [ "$1" = run ]; then
    shift
    while [ $# -gt 0 ] && [ "$1" != "--" ]; do shift; done
    shift
    exec "$@"
  fi
  f="$FAKE_LOG/capsule-secret-check.rc"; [ -f "$f" ] && exit "$(cat "$f")"
  exit 0
fi
# docker-info-fail-n: "docker info" fails exactly N times (decrementing a
# counter file), then succeeds - the bounded-retry test (fix wave 1, finding
# 4) needs "fails a few times then recovers", which a flat rc file can't do.
if [ "$me" = docker ] && [ "$1" = info ] && [ -f "$FAKE_LOG/docker-info-fail-n" ]; then
  n="$(cat "$FAKE_LOG/docker-info-fail-n")"
  if [ "$n" -gt 0 ]; then
    echo $((n - 1)) >"$FAKE_LOG/docker-info-fail-n"
    exit 1
  fi
  rm -f "$FAKE_LOG/docker-info-fail-n"
fi
f="$FAKE_LOG/$me.rc"; [ -f "$f" ] && exit "$(cat "$f")"
case "$me" in
  launchctl) [ "$1" = list ] && { cat "$FAKE_LOG/launchctl.list" 2>/dev/null; }; exit 0 ;;
  docker) case "$*" in "compose"*"config --services") printf 'infisical\ngrafana\nloki\n' ;;
                      "compose"*" ps "*|"ps"*) cat "$FAKE_LOG/docker.ps" 2>/dev/null ;; esac; exit 0 ;;
  bootstrap.sh) cat "$FAKE_LOG/bootstrap.out" 2>/dev/null; exit 0 ;;
  run.sh) [ -f "$FAKE_LOG/heal.json.next" ] && cp "$FAKE_LOG/heal.json.next" "$AGENT_OPS_STATE_DIR/heal.json"; cat "$FAKE_LOG/heal.out" 2>/dev/null; exit 0 ;;
esac
exit 0
EOF2
  chmod +x "$T/bin/$b"
done
export COMPOSE_PROJECT_NAME=capsule-corp-test   # temp OBS_ENV + fake docker: never the live project (compose_live_guard)
export FAKE_LOG="$T/log"; mkdir -p "$FAKE_LOG"
export LAUNCHCTL_BIN="$T/bin/launchctl" DOCKER_BIN="$T/bin/docker" CURL_BIN="$T/bin/curl" \
       CAPSULE_SECRET_BIN="$T/bin/capsule-secret" BOOTSTRAP_BIN="$T/bin/bootstrap.sh" RUN_SH_BIN="$T/bin/run.sh" \
       AGENT_OPS_STATE_DIR="$T/state" AGENT_OPS_NO_SEND=1 AGENT_OPS_FLAVOR=personal OPS_ALERT_TARGET=12345 \
       COMPOSE_FILE="$REPO/observability/docker-compose.yml" OBS_ENV="$T/obs.env" HOI_POI_OPS_ENV=/dev/null
: >"$T/obs.env"
six_ok() { python3 -c 'import json,sys; print(json.dumps({c:{"fails":0,"first_fail":None} for c in "docker infisical stack secrets claude-login outbox".split()}))' >"$1"; }
all_up() {  # every fake answers "running"
  rm -f "$FAKE_LOG"/*.rc; six_ok "$T/state/heal.json"
  printf '%s\n' "${PIPELINE_JOBS_LIST[@]}" "$OP.ollama-serve" | sed 's/^/1\t0\t/' >"$FAKE_LOG/launchctl.list"
  printf 'infisical\ngrafana\nloki\n' >"$FAKE_LOG/docker.ps"; : >"$FAKE_LOG/docker.rc"; rm -f "$FAKE_LOG/docker.rc"
}
PIPELINE_JOBS_LIST=(); while IFS= read -r l; do PIPELINE_JOBS_LIST+=("$l"); done < <(bash -c 'REPO="$1"; . "$1/bin/lib_jobs.sh"; printf "%s\n" "${PIPELINE_JOBS[@]}"' _ "$REPO")

# --- status (Task 2) ---
all_up; out="$(bash "$REPO/bin/hoi-poi.sh" status 2>&1)"; rc=$?
check "status exits 0 when everything is up" '[ $rc -eq 0 ]'
check "status prints one row per component" 'for r in docker infisical stack secrets ollama jobs heal workers park down-stamp; do grep -q "^$r " <<<"$out" || exit 1; done'
check "status has no gateways row and names no ai.example label (gateway class retired, D229)" '! grep -qi "gateway" <<<"$out" && ! grep -q "ai.example" <<<"$out"'
check "status reads heal from heal.json, never runs heal" '! [ -f "$FAKE_LOG/run.sh.log" ]'
echo 1 >"$FAKE_LOG/docker.rc"; out="$(bash "$REPO/bin/hoi-poi.sh" status 2>&1)"; rc=$?
check "status exits 1 when docker is down" '[ $rc -eq 1 ] && grep -q "^docker *FAIL" <<<"$out"'
rm -f "$FAKE_LOG/docker.rc"

# age_heal_json <minutes> - backdate state/heal.json's mtime by exactly
# <minutes>*60 seconds (row 511 G4: a stalled heal tick, not a failing one -
# the file's own last verdict is still 6/6, only its age is the problem).
age_heal_json() { python3 -c '
import os, sys, time
t = time.time() - int(sys.argv[1]) * 60
os.utime(sys.argv[2], (t, t))' "$1" "$T/state/heal.json"; }

all_up; age_heal_json 11; out="$(bash "$REPO/bin/hoi-poi.sh" status 2>&1)"; rc=$?
check "status FAILs heal when heal.json is 11 min old (past the 10 min boundary)" \
  '[ $rc -eq 1 ] && grep -q "^heal *FAIL" <<<"$out"'
all_up; age_heal_json 10; out="$(bash "$REPO/bin/hoi-poi.sh" status 2>&1)"; rc=$?
check "status still passes heal at exactly the 10 min boundary (not yet older than)" \
  '[ $rc -eq 0 ] && grep -q "^heal *ok" <<<"$out"'
all_up

# --- up (Task 3) ---
mkdir -p "$T/launchagents"
export LAUNCH_AGENTS_DIR="$T/launchagents" HOI_POI_WAIT_STEP_S=0
reset_logs() { rm -f "$FAKE_LOG"/*.log "$FAKE_LOG"/*.rc "$FAKE_LOG"/*-check.rc "$FAKE_LOG/heal.json.next"; rm -rf "$T/state/outbox"; }
heal_next_ok() { six_ok "$FAKE_LOG/heal.json.next"; }
heal_next_5of6() { python3 -c 'import json
comps = "docker infisical stack secrets claude-login outbox".split()
d = {c: {"fails": 0, "first_fail": None} for c in comps}
d["outbox"] = {"fails": 1, "first_fail": "x"}
print(json.dumps(d))' >"$FAKE_LOG/heal.json.next"; }

all_up; reset_logs; heal_next_ok
printf 'MISS   node not on PATH\n' >"$FAKE_LOG/bootstrap.out"
out="$(bash "$REPO/bin/hoi-poi.sh" up 2>&1)"; rc=$?
check "up: preflight MISS refuses with exit 3" '[ $rc -eq 3 ] && grep -q "MISS   node not on PATH" <<<"$out"'
check "up: a preflight refusal calls nothing else" '[ ! -f "$FAKE_LOG/docker.log" ]'

all_up; reset_logs; heal_next_ok
printf 'DRIFT  deploy --check\n' >"$FAKE_LOG/bootstrap.out"
out="$(bash "$REPO/bin/hoi-poi.sh" up 2>&1)"; rc=$?
check "up: preflight DRIFT warns and continues" '[ $rc -eq 0 ] && grep -q "WARN" <<<"$out" && grep -q "DRIFT" <<<"$out"'
check "up: bootstrap.sh is called with --check --flavor personal" 'grep -qF -- "--check --flavor personal" "$FAKE_LOG/bootstrap.sh.log"'

all_up; reset_logs; heal_next_ok
: >"$FAKE_LOG/bootstrap.out"
out="$(bash "$REPO/bin/hoi-poi.sh" up 2>&1)"; rc=$?
check "up: exits 0 on a clean run" '[ $rc -eq 0 ]'
check "up: compose sees the Infisical trio before the rest" \
  'grep -n "infisical-db infisical-redis infisical" "$FAKE_LOG/docker.log" | head -1 | cut -d: -f1 > "$T/l1"
   grep -n -- "up -d --quiet-pull *$" "$FAKE_LOG/docker.log" | head -1 | cut -d: -f1 > "$T/l2"
   [ -s "$T/l1" ] && [ -s "$T/l2" ] && [ "$(cat "$T/l1")" -lt "$(cat "$T/l2")" ]'
check "up: each PIPELINE_JOBS label is bootstrapped exactly once" \
  'ok=1; for l in "${PIPELINE_JOBS_LIST[@]}"; do
     n="$(grep -c "bootstrap gui/.*$l\.plist\$" "$FAKE_LOG/launchctl.log")"
     [ "$n" = 1 ] || { ok=0; break; }
   done; [ "$ok" = 1 ]'
check "up: the fake run.sh was called with exactly heal" '[ "$(cat "$FAKE_LOG/run.sh.log")" = "heal" ]'
check "up: one outbox file with to=OPS_ALERT_TARGET and hoi-poi up: body" \
  'f="$(find "$T/state/outbox" -maxdepth 1 -type f -name "*-hoi-poi.msg")"; [ "$(echo "$f" | wc -l | tr -d " ")" = 1 ] && grep -q "^to=12345$" "$f" && grep -q "hoi-poi up:" "$f"'

all_up; reset_logs; heal_next_ok; : >"$FAKE_LOG/bootstrap.out"
out="$(OPS_ALERT_TARGET= bash "$REPO/bin/hoi-poi.sh" up 2>&1)"; rc=$?
check "up: no outbox file when OPS_ALERT_TARGET is unset" '[ -z "$(find "$T/state/outbox" -maxdepth 1 -type f -name "*-hoi-poi.msg" 2>/dev/null)" ]'

all_up; reset_logs; heal_next_ok; : >"$FAKE_LOG/bootstrap.out"
echo 1 >"$FAKE_LOG/capsule-secret-check.rc"
out="$(bash "$REPO/bin/hoi-poi.sh" up 2>&1)"; rc=$?
check "up: a secrets failure exits 6" '[ $rc -eq 6 ]'
check "up: the stack is left up on a secrets failure" 'grep -qF "infisical-db infisical-redis infisical" "$FAKE_LOG/docker.log"'
rm -f "$FAKE_LOG/capsule-secret-check.rc"

all_up; reset_logs; heal_next_ok; : >"$FAKE_LOG/bootstrap.out"
mkdir -p "$T/state"; printf '{"labels":["%s.obsidian-sprint-sync"]}' "$OP" >"$T/state/park.json"
out="$(bash "$REPO/bin/hoi-poi.sh" up 2>&1)"; rc=$?
check "up: a parked label is not bootstrapped" '! grep -q "$OP.obsidian-sprint-sync" "$FAKE_LOG/launchctl.log"'
check "up: the parked count is printed" 'grep -q "parked: 1" <<<"$out"'
rm -f "$T/state/park.json"

# A park.json stamp written before the gateway class was retired may still name the old gateway
# labels: `up` must not bootstrap them (they are in no list) and must still succeed.
all_up; reset_logs; heal_next_ok; : >"$FAKE_LOG/bootstrap.out"
printf '{"labels":["ai.example.gateway-slack","ai.example.gateway-automation"]}' >"$T/state/park.json"
out="$(bash "$REPO/bin/hoi-poi.sh" up 2>&1)"; rc=$?
check "up: a stale stamp naming retired gateway labels bootstraps none of them" \
  '! grep -q "ai.example.gateway" "$FAKE_LOG/launchctl.log"'
check "up: a stale gateway stamp does not fail up and prints no gateway line" '[ $rc -eq 0 ] && ! grep -qi "gateway" <<<"$out"'
rm -f "$T/state/park.json"

all_up; reset_logs; heal_next_5of6; : >"$FAKE_LOG/bootstrap.out"
out="$(bash "$REPO/bin/hoi-poi.sh" up 2>&1)"; rc=$?
check "up: a heal tick short of 6/6 exits 10" '[ $rc -eq 10 ] && grep -q "heal 5/6" <<<"$out"'

all_up; reset_logs; heal_next_ok; : >"$FAKE_LOG/bootstrap.out"
out="$(bash "$REPO/bin/hoi-poi.sh" up 2>&1)"; rc=$?
check "up: a full heal tick exits 0 and prints heal 6/6" '[ $rc -eq 0 ] && grep -q "heal 6/6" <<<"$out"'

# drift guard: hoi-poi gates `up` on exactly heal_table.ORDER minus slack-token (report-only in
# heal.sh) and tick (`up` runs before the ticks start; tick-watch restarts a dead tick itself)
check "hoi-poi heal_summary components == heal_table.ORDER minus slack-token, tick and jobs" \
  'python3 - "$REPO" <<PY
import re, sys
sys.path.insert(0, sys.argv[1] + "/maintenance")
import heal_table
src = open(sys.argv[1] + "/bin/hoi-poi.sh").read()
comps = re.search(r"^comps = \"([^\"]+)\"", src, re.M).group(1).split()
sys.exit(0 if sorted(comps) == sorted(c for c in heal_table.ORDER if c not in ("slack-token", "tick", "jobs")) else 1)
PY'

# a stale, failing gateway key left in a live heal.json (the check is retired) neither counts nor breaks healthy
all_up; reset_logs; heal_next_ok; : >"$FAKE_LOG/bootstrap.out"
python3 -c 'import json,sys; p=sys.argv[1]; d=json.load(open(p)); d["gateway"]={"fails":9,"first_fail":"x"}; json.dump(d,open(p,"w"))' "$FAKE_LOG/heal.json.next"
out="$(bash "$REPO/bin/hoi-poi.sh" up 2>&1)"; rc=$?
check "up: a stale failing gateway key in heal.json still reads heal 6/6" '[ $rc -eq 0 ] && grep -q "heal 6/6" <<<"$out"'

all_up; reset_logs; heal_next_ok
out="$(HOI_POI_SKIP_PREFLIGHT=1 bash "$REPO/bin/hoi-poi.sh" up 2>&1)"; rc=$?
check "up: HOI_POI_SKIP_PREFLIGHT=1 skips the preflight call" '[ $rc -eq 0 ] && [ ! -f "$FAKE_LOG/bootstrap.sh.log" ]'

# Fix wave 1, finding 6: a MISS naming ollama-serve is a warning, not a
# refusal (step 5 exists to fix exactly that); a genuine MISS still refuses.
all_up; reset_logs; heal_next_ok
printf 'MISS   ollama-serve not loaded (deploy.sh registers it)\n' >"$FAKE_LOG/bootstrap.out"
out="$(bash "$REPO/bin/hoi-poi.sh" up 2>&1)"; rc=$?
check "up: an ollama-serve MISS is downgraded to a warning, not a refusal" \
  '[ $rc -eq 0 ] && grep -q "WARN.*ollama-serve" <<<"$out"'
all_up; reset_logs; heal_next_ok
printf 'MISS   ollama-serve not loaded (deploy.sh registers it)\nMISS   node not on PATH\n' >"$FAKE_LOG/bootstrap.out"
out="$(bash "$REPO/bin/hoi-poi.sh" up 2>&1)"; rc=$?
check "up: a genuine MISS alongside an ollama-serve MISS still refuses (3)" \
  '[ $rc -eq 3 ] && grep -q "MISS   node not on PATH" <<<"$out" && ! [ -f "$FAKE_LOG/docker.log" ]'

# Fix wave 1, finding 4: docker step 2 retries up to a bound instead of a
# single unbounded attempt - a just-rebooted host's Docker Desktop can take
# several seconds to start answering. The fake's docker-info-fail-n counter
# makes "docker info" fail exactly N times before succeeding.
all_up; reset_logs; heal_next_ok; : >"$FAKE_LOG/bootstrap.out"
echo 3 >"$FAKE_LOG/docker-info-fail-n"
out="$(bash "$REPO/bin/hoi-poi.sh" up 2>&1)"; rc=$?
check "up: docker step retries and still succeeds within the bound" '[ $rc -eq 0 ]'
rm -f "$FAKE_LOG/docker-info-fail-n"
all_up; reset_logs; heal_next_ok; : >"$FAKE_LOG/bootstrap.out"
echo 999 >"$FAKE_LOG/docker-info-fail-n"
out="$(bash "$REPO/bin/hoi-poi.sh" up 2>&1)"; rc=$?
check "up: docker step still gives up and exits 4 if docker never answers" '[ $rc -eq 4 ]'
rm -f "$FAKE_LOG/docker-info-fail-n"

# A stale stamp naming retired gateway labels neither counts against the jobs row nor adds a gateways row.
all_up; reset_logs; heal_next_ok
printf '{"labels":["ai.example.gateway-slack","ai.example.gateway-automation"]}' >"$T/state/park.json"
out="$(bash "$REPO/bin/hoi-poi.sh" status 2>&1)"; rc=$?
check "status: a stale gateway stamp keeps jobs ok and shows no gateways row" \
  '[ $rc -eq 0 ] && grep -q "^jobs *ok" <<<"$out" && ! grep -qi "gateway" <<<"$out"'
rm -f "$T/state/park.json"
check "hoi-poi.sh carries no gateway metric or loop" '! grep -qE "GATEWAYS|M_GW_|gateways" "$REPO/bin/hoi-poi.sh" "$REPO/bin/lib_jobs.sh" "$REPO/bin/capsule-park.sh"'

# --- down (Task 4) ---
all_up; reset_logs
printf 'abc123\ndef456\n' >"$FAKE_LOG/docker.ps"
heal_next_ok
out="$(bash "$REPO/bin/hoi-poi.sh" down 2>&1)"; rc=$?
check "down: exits 0" '[ $rc -eq 0 ]'
check "down: the heal tick runs before any bootout" \
  'first_heal_line="$(grep -n "^run\.sh heal$" "$FAKE_LOG/all.log" | head -1 | cut -d: -f1)"
   first_bootout_line="$(grep -n "^launchctl bootout" "$FAKE_LOG/all.log" | head -1 | cut -d: -f1)"
   [ -n "$first_heal_line" ] && { [ -z "$first_bootout_line" ] || [ "$first_heal_line" -lt "$first_bootout_line" ]; }'
check "down: every PIPELINE_JOBS label is booted out" \
  'ok=1; for l in "${PIPELINE_JOBS_LIST[@]}"; do grep -qF "bootout gui/$(id -u)/$l" "$FAKE_LOG/launchctl.log" || { ok=0; break; }; done; [ "$ok" = 1 ]'
check "down: no gateway label is booted out (the class is retired)" \
  '! grep -qF "ai.example" "$FAKE_LOG/launchctl.log"'
check "down: ollama-serve is never booted out" '! grep -q "$OP.ollama-serve" "$FAKE_LOG/launchctl.log"'
check "down: every worker container listed is stopped" \
  'grep -qE "stop.*abc123" "$FAKE_LOG/docker.log" && grep -qE "stop.*def456" "$FAKE_LOG/docker.log"'
# Workers are found by the runner's capsule-run-id label, never by an image tag: the layered
# images have no one tag.
check "down: finds workers by the capsule-run-id label" \
  'grep -qF "label=capsule-run-id" "$FAKE_LOG/docker.log"'
check "down: never filters workers by an image tag" \
  '! grep -qF "ancestor=" "$FAKE_LOG/docker.log"'
check "down: compose stop runs, never down/-v/prune/rm" \
  'grep -qE "compose.*stop" "$FAKE_LOG/docker.log" &&
   ! grep -qE "compose.* down" "$FAKE_LOG/docker.log" &&
   ! grep -qE " -v " "$FAKE_LOG/docker.log" &&
   ! grep -qi "prune" "$FAKE_LOG/docker.log" &&
   ! grep -qE "rm " "$FAKE_LOG/docker.log"'
check "down: the stamp is 0600 with down_since" \
  '[ "$(perm "$T/state/hoi-poi.json")" = "600" ] && python3 -c "import json; assert \"down_since\" in json.load(open(\"$T/state/hoi-poi.json\"))"'

all_up; reset_logs; heal_next_ok; : >"$FAKE_LOG/docker.ps"
echo 3 >"$FAKE_LOG/launchctl.rc"
out="$(bash "$REPO/bin/hoi-poi.sh" down 2>&1)"; rc=$?
check "down: a failing bootout does not stop the sequence, exit is still 0" \
  '[ $rc -eq 0 ] && grep -qE "compose.*stop" "$FAKE_LOG/docker.log"'
rm -f "$FAKE_LOG/launchctl.rc"

# inc_02b0bd3a: temp OBS_ENV against the live project (real HOME): compose stop is refused, no stamp.
all_up; reset_logs; heal_next_ok; : >"$FAKE_LOG/docker.ps"; rm -f "$T/state/hoi-poi.json"
out="$(env -u COMPOSE_PROJECT_NAME bash "$REPO/bin/hoi-poi.sh" down 2>&1)"; rc=$?
check "down: the live project with a temp OBS_ENV is refused: no compose stop, no stamp" \
  'grep -q "refused by the live-stack guard" <<<"$out" && ! grep -qE "compose.*stop" "$FAKE_LOG/docker.log" && [ ! -e "$T/state/hoi-poi.json" ]'

all_up; reset_logs; heal_next_ok; : >"$FAKE_LOG/docker.ps"
out="$(bash "$REPO/bin/hoi-poi.sh" down 2>&1)"; rc=$?
check "down: an outbox file with a hoi-poi down: body" \
  'f="$(find "$T/state/outbox" -maxdepth 1 -type f -name "*-hoi-poi.msg")"; [ -n "$f" ] && grep -q "hoi-poi down:" "$f"'

# --- supervisor (Task 5) ---
all_up; reset_logs; heal_next_ok; : >"$FAKE_LOG/docker.ps"
sup_out="$T/sup.out"
HOI_POI_TICK_S=0 HOI_POI_MAX_TICKS=2 bash "$REPO/bin/hoi-poi.sh" >"$sup_out" 2>&1 &
sup_pid=$!
waited=0
while :; do
  n="$(grep -c '^hoi-poi  ' "$sup_out" 2>/dev/null)"; n="${n:-0}"
  [ "$n" -ge 2 ] && break
  [ "$waited" -ge 50 ] && break
  sleep 0.1; waited=$((waited + 1))
done
kill -INT "$sup_pid" 2>/dev/null
wait "$sup_pid" 2>/dev/null
sup_content="$(cat "$sup_out")"
check "supervisor: exactly two status lines" '[ "$(grep -c "^hoi-poi  " <<<"$sup_content")" = 2 ]'
# "going down" is printed as soon as the shutdown starts - down's own output
# (heal tick, bootouts, "hoi-poi: down") follows it, so it is not the LAST
# line; the requirement (spec section 7) is that it is printed at all, once
# the supervisor decides to shut down.
check "supervisor: prints going down before running down" 'grep -q "going down" <<<"$sup_content"'
check "supervisor: down ran (compose stop reached docker.log)" 'grep -qE "compose.*stop" "$FAKE_LOG/docker.log"'

# The MAX_TICKS=2 run above proves the two-status-lines/shutdown SHAPE, but
# it can pass even if the `trap ... INT TERM HUP` line were deleted or moved
# out of supervise() entirely, because shutdown_and_exit is what MAX_TICKS
# itself calls. This second run uses HOI_POI_MAX_TICKS=0 (never fires on its
# own) and HOI_POI_TICK_S=5 (paced real one-second sleeps, per the fix-wave
# sleep-1xN change) so the only way "going down" and a compose stop appear
# is a real signal reaching the installed trap - proving the wiring, not
# just the shape.
#
# Sends SIGTERM, not SIGINT, and this is deliberate, not a shortcut: a
# background job (this test necessarily runs the supervisor via `&`, since
# it must keep driving the test script while the supervisor runs) has
# SIGINT and SIGQUIT force-set to IGNORE by the shell at fork time, and
# bash's own docs are explicit that a signal already SIG_IGN "upon entry to
# the shell cannot be trapped or reset" - no `trap` line inside hoi-poi.sh,
# correct or not, can change that. Verified directly: a minimal `trap f INT;
# sleep 1` script backgrounded with `&` and sent SIGINT via `kill -INT`
# never runs the trap and runs to completion; the identical script sent
# SIGTERM fires it immediately. This is a property of backgrounding a job
# from a shell, not a hoi-poi.sh defect - Ctrl-C at a real terminal sends
# SIGINT to the whole foreground process group, which was never
# backgrounded, so it is unaffected. TERM (what launchd and a wrapper's
# `kill` actually send) and HUP exercise the identical trap/shutdown code
# path as INT inside shutdown_and_exit, so this still proves the trap
# fires and reaches `down` for real.
all_up; reset_logs; heal_next_ok; : >"$FAKE_LOG/docker.ps"
sup2_out="$T/sup2.out"
HOI_POI_TICK_S=5 HOI_POI_MAX_TICKS=0 bash "$REPO/bin/hoi-poi.sh" >"$sup2_out" 2>&1 &
sup2_pid=$!
waited=0
while :; do
  n="$(grep -c '^hoi-poi  ' "$sup2_out" 2>/dev/null)"; n="${n:-0}"
  [ "$n" -ge 1 ] && break
  [ "$waited" -ge 100 ] && break
  sleep 0.1; waited=$((waited + 1))
done
kill -TERM "$sup2_pid" 2>/dev/null
wait "$sup2_pid" 2>/dev/null
sup2_content="$(cat "$sup2_out")"
check "supervisor: a real SIGTERM fires the installed trap (MAX_TICKS=0)" \
  'grep -q "going down" <<<"$sup2_content"'
check "supervisor: the real SIGTERM's down reaches compose stop" \
  'grep -qE "compose.*stop" "$FAKE_LOG/docker.log"'

# Negative greps over bin/hoi-poi.sh: no forbidden command ever appears, and
# every bootstrap.sh call carries --check. The plan's literal
# `bootstrap\.sh[^-]*$` regex also flags the header comment's own prose
# naming the constraint ("... `bootstrap.sh`/`deploy.sh` without `--check`")
# and the `BOOTSTRAP_BIN` default-path assignment, neither of which is an
# invocation - so this checks invocation sites (`$BOOTSTRAP_BIN` calls)
# directly instead of grepping the literal string "bootstrap.sh".
# the retired system's send verb comes from the private denylist (none in a public checkout)
OLD_SYS="$(python3 "$REPO/tests/denylist_term.py" retired 2>/dev/null)"
SEND_PAT="git |rm -rf"; [ -n "$OLD_SYS" ] && SEND_PAT="$OLD_SYS send|$SEND_PAT"
check "hoi-poi.sh never sends chat or touches git/rm -rf" \
  '! grep -qE "$SEND_PAT" "$REPO/bin/hoi-poi.sh"'
# Every real docker/compose invocation in this repo is
# "$DOCKER_BIN" compose --env-file ... -f ... <verb>" - a literal "compose
# down" or a space-delimited " -v " never matches that shape, so a later
# "tear the stack down properly" edit adding `compose ... down -v` would
# slip past a check written that way. Check instead for a down/-v/prune
# token on any line that also names docker/compose at all.
check "no line combines docker/compose with a down/-v/prune token" \
  'bad=$(grep -n "compose\|\$DOCKER_BIN" "$REPO/bin/hoi-poi.sh" \
     | grep -E "[[:space:]]down([[:space:]]|$)|[[:space:]]-v([[:space:]]|$)|prune"); \
   [ -z "$bad" ]'
check "every \$BOOTSTRAP_BIN call carries --check" \
  'bad=$(grep -n "\$BOOTSTRAP_BIN" "$REPO/bin/hoi-poi.sh" | grep -v -- "--check"); [ -z "$bad" ]'
check "hoi-poi.sh never invokes deploy.sh directly (bootstrap.sh --check covers it)" \
  '! grep -qE "(\\\$DEPLOY|[^.-]bash[^.]*deploy\.sh)" "$REPO/bin/hoi-poi.sh"'

# Fix wave 1, finding 12: hoi-poi.sh names itself, not capsule-park, in
# lib_jobs.sh's unclassified-plist warning.
check "hoi-poi.sh sets its own CLASSIFY_PREFIX before sourcing lib_jobs.sh" \
  'prefix_line="$(grep -n "CLASSIFY_PREFIX=hoi-poi" "$REPO/bin/hoi-poi.sh" | head -1 | cut -d: -f1)"
   source_line="$(grep -nF "REPO/bin/lib_jobs.sh\"" "$REPO/bin/hoi-poi.sh" | head -1 | cut -d: -f1)"
   [ -n "$prefix_line" ] && [ -n "$source_line" ] && [ "$prefix_line" -lt "$source_line" ]'
# Direct functional check: source lib_jobs.sh with CLASSIFY_PREFIX pre-set
# and REPO pointed at a scratch tree holding one unclassified plist.
mkdir -p "$T/scratch/launchd/templates" "$T/scratch/bin"
cp "$REPO/bin/lib_jobs.sh" "$REPO/bin/lib_prefix.sh" "$T/scratch/bin/"
: >"$T/scratch/launchd/templates/dev.capsule.totally-unclassified.plist"
out="$(bash -c 'REPO="$1"; CLASSIFY_PREFIX=hoi-poi; . "$1/bin/lib_jobs.sh"; classify_check' _ "$T/scratch")"
check "classify_check honours a pre-set CLASSIFY_PREFIX" 'grep -q "^hoi-poi: WARNING" <<<"$out"'

# State root (t_c8b3d583): hoi-poi and run.sh both take STATE_DIR from maintenance/lib_state.sh.
check "AGENT_OPS_STATE_DIR defaults to run.sh's own state dir convention" \
  'grep -qF "lib_state.sh" "$REPO/bin/hoi-poi.sh" && grep -qF ".local/state/capsule-corp/maint" "$REPO/maintenance/lib_state.sh"'

printf '%s.obsidian-weekly\n' "$OP" >"$T/unloaded"
out="$(CAPSULE_LAUNCHD_UNLOADED="$T/unloaded" bash -c 'REPO="$1"; . "$1/bin/lib_jobs.sh"; printf "%s\n" "${DISPATCHING[@]}"; classify_check' _ "$REPO")"
check "an unloaded label is dropped from DISPATCHING but still classified" '! grep -q obsidian-weekly <<<"$out" && ! grep -qi unclassified <<<"$out"'

# row 526 (card t_1758e0bd) item 2: an unloaded-file line with a trailing space, tab or CR (an editor on
# CRLF, a stray blank) still names its label - lib_jobs trims it, the way deploy.sh does.
printf '%s.obsidian-weekly \r\ndev.capsule.capsule-tick-capsule-corp\t\n  # indented comment\n' "$OP" >"$T/unloaded-ws"
lj() { CAPSULE_LAUNCHD_UNLOADED="$T/unloaded-ws" bash -c 'REPO="$1"; . "$1/bin/lib_jobs.sh"; eval "$2"' _ "$REPO" "$1"; }
check "an unloaded label with a trailing space and CR is dropped from DISPATCHING" '! lj '"'"'printf "%s\n" "${DISPATCHING[@]}"'"'"' | grep -q obsidian-weekly'
check "an unloaded label with a trailing tab is dropped from DISPATCHING" '! lj '"'"'printf "%s\n" "${DISPATCHING[@]}"'"'"' | grep -q capsule-tick-capsule-corp'
check "UNLOADED_LABELS holds exactly the two bare labels (no CR, tab or comment)" \
  '[ "$(lj '"'"'printf "[%s]" "${UNLOADED_LABELS[@]}"'"'"')" = "[$OP.obsidian-weekly][dev.capsule.capsule-tick-capsule-corp]" ]'
check "a whitespace-padded unloaded label is still classified" '[ -z "$(lj classify_check)" ]'
# a fixture copy with a neutral board (sample-app): never the real repos.json or the export-excluded templates
FX="$T/fx"; mkdir -p "$FX/launchd" && cp -R "$REPO/maintenance" "$FX/maintenance" && cp -R "$REPO/bin" "$FX/bin" && cp -R "$REPO/launchd/templates" "$FX/launchd/templates" && ln -s "$REPO/capsule" "$FX/capsule"
: >"$FX/launchd/templates/__PREFIX__.capsule-tick-sample-app.plist"
printf '[{"name":"a","board":"capsule-corp"},{"name":"b","board":"sample-app"}]' >"$T/repos.json"
all_up; reset_logs; heal_next_ok; : >"$FAKE_LOG/bootstrap.out"
AGENT_OPS_REPOS_JSON="$T/repos.json" CAPSULE_LAUNCHD_UNLOADED="$T/unloaded-ws" bash "$FX/bin/hoi-poi.sh" up >/dev/null 2>&1
check "hoi-poi up never bootstraps a whitespace-padded unloaded label" \
  'grep -q "bootstrap gui/.*dev.capsule.capsule-tick-sample-app.plist" "$FAKE_LOG/launchctl.log" && ! grep -q "capsule-tick-capsule-corp" "$FAKE_LOG/launchctl.log"'

echo; [ $fails -eq 0 ] && echo "test_hoi_poi: all passed" || { echo "test_hoi_poi: $fails failed"; exit 1; }
