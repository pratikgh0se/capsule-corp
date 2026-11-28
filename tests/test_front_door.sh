#!/usr/bin/env bash
# W333 front door: cc.<name>.localhost reverse proxy (Caddy) in observability/docker-compose.yml.
# Static cases: every published port binds 127.0.0.1 (Grafana runs with anonymous Admin, so a
# 0.0.0.0 bind would be an open door), the Caddy image is pinned exactly, the Caddyfile routes the
# two names to the right services, Grafana and Infisical know their public address.
# Docker case (opt-in, RUN_DOCKER_TESTS=1): the real Caddy image validates the Caddyfile and
# routes by Host header to a throwaway upstream. Throwaway names only, never the live containers.
set -u
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
C="$REPO/observability/docker-compose.yml"; F="$REPO/observability/caddy/Caddyfile"; G="$REPO/observability/grafana/grafana.ini"
fails=0
PASSED=""
check() { if eval "$2"; then echo "PASS $1"; PASSED="$PASSED|$1"; else echo "FAIL $1"; fails=$((fails+1)); fi; }

check "every published port binds 127.0.0.1" \
  '[ "$(grep -cE "^    ports:" "$C")" -ge 5 ] && ! grep -E "^    ports:" "$C" | grep -qv "ports: \[\"127\.0\.0\.1:"'
check "front-door publishes 127.0.0.1:80 only" \
  'sed -n "/^  front-door:/,/^  infisical:/p" "$C" | grep -q "ports: \[\"127\.0\.0\.1:80:80\"\]"'
check "caddy image pinned to an exact version" \
  'grep -qE "^    image: caddy:[0-9]+\.[0-9]+\.[0-9]+-alpine$" "$C"'
check "every image: is an exact version tag or a digest (no floating major tags)" \
  '[ "$(grep -c "^    image:" "$C")" -ge 8 ] && ! grep "^    image:" "$C" | grep -vqE "image: [^ ]+(:[0-9]+\.[0-9]+(\.[0-9]+)?-?[a-z0-9.]*|@sha256:[0-9a-f]{64})( |$)"'
check "caddyfile: grafana host -> grafana:3000" \
  'tr -d "\n\t" <"$F" | grep -q "http://cc\.grafana\.localhost {reverse_proxy grafana:3000}"'
R="$REPO/observability/caddy/routes"
check "caddy route file: secrets host -> infisical:8080" \
  'tr -d "\n\t" <"$R/secrets.caddy" | grep -q "http://cc\.secrets\.localhost {reverse_proxy infisical:8080}"'
check "caddyfile: the secrets route is imported conditionally, never inline" \
  'grep -q "^import {\$CC_CADDY_SECRETS:/etc/caddy/routes/none.caddy}" "$F" && ! grep -q "infisical" <(grep -v "^#" "$F") && ! grep -q "reverse_proxy" "$R/none.caddy"'
# P06: Infisical, its db and redis only run under compose profile "secrets".
PD="$(mktemp -d)"
printf 'DATA_DIR=/tmp/x\nCAPSULE_HOME=/tmp/y\nMAINT_LOGS=/tmp/z\nINFISICAL_ENV=/dev/null\nINFISICAL_PG_PASSWORD=dummy\n' >"$PD/env"
DC="env OPS_PG_PASSWORD=x OPS_GRAFANA_PASSWORD=x OPS_TG_BOT_TOKEN=x OPS_TG_CHAT_ID=1 OPS_TG_THREAD_ID=1 docker compose --env-file $PD/env -f $C"
if command -v docker >/dev/null 2>&1 && $DC config -q >/dev/null 2>&1; then
  check "compose: default profile lists no infisical service" \
    '! $DC config --services | grep -q infisical'
  check "compose: COMPOSE_PROFILES=secrets lists infisical, its db and redis" \
    '[ "$(COMPOSE_PROFILES=secrets $DC config --services | grep -c "^infisical")" = 3 ]'
  check "compose: front-door does not depend on a profiled service" \
    '! $DC config 2>/dev/null | sed -n "/^  front-door:/,/^  [a-z-]*:$/p" | grep -q "infisical"'
else
  echo "SKIP compose profile checks (docker compose config unavailable)"
fi
rm -r "$PD"
check "caddyfile: no TLS" 'grep -q "auto_https off" "$F" && ! grep -q "https://" "$F"'
check "grafana: anonymous Viewer on, never Admin" \
  'sed -n "/^\[auth.anonymous\]/,/^\[users\]/p" "$G" | grep -q "^enabled = true" && sed -n "/^\[auth.anonymous\]/,/^\[users\]/p" "$G" | grep -q "^org_role = Viewer" && ! grep -qiE "^(org_role = Admin|viewers_can_edit *= *true)" "$G"'
check "grafana: root_url and domain are cc.grafana.localhost" \
  'grep -q "^root_url = http://cc\.grafana\.localhost/$" "$G" && grep -q "^domain = cc\.grafana\.localhost$" "$G"'
check "infisical: SITE_URL is cc.secrets.localhost in compose" \
  'grep -q "^      SITE_URL: http://cc\.secrets\.localhost$" "$C"'

# W346: Factory Overview is everyone's home page; the three empty pre-rebuild folders are pruned
# when they look empty (check-then-delete, not atomic); the committed dashboards are exactly what the builders emit.
check "grafana: default home is the provisioned Factory Overview (lookout-home)" \
  'grep -qx "default_home_dashboard_path = /var/lib/grafana/dashboards/lookout/home.json" "$G" && grep -q "\"uid\": \"lookout-home\"" "$REPO/observability/grafana/dashboards/lookout/home.json"'
check "grafana: anonymous role is still Viewer with the home path set" \
  'grep -q "^org_role = Viewer" "$G" && ! grep -qiE "^org_role = (Admin|Editor)" "$G"'
PT="$(mktemp -d)"; PL="$PT/log"
PRUNE_OUT="$(PRUNE_LOG="$PL" CURL_BIN="$REPO/tests/fixtures/prune_curl_stub.sh" CAPSULE_SECRET_BIN="$REPO/tests/fixtures/prune_secret_stub.sh" GRAFANA_URL=http://stub "$REPO/maintenance/grafana_prune_folders.sh" 2>&1)"
check "prune: deletes the empty folders" 'grep -q "DELETE.*api/folders/empty1" "$PL" && grep -q "DELETE.*api/folders/empty3" "$PL"'
check "prune: refuses a non-empty folder and never touches others" \
  '! grep -qE "DELETE.*/api/folders/(full|real)" "$PL" && printf "%s" "$PRUNE_OUT" | grep -q "Archive. is not empty"'
check "prune: the admin password never reaches an argv or the output" \
  '! grep -q "stub-pw-9f3" "$PL" && ! printf "%s" "$PRUNE_OUT" | grep -q "stub-pw-9f3"'
check "home.json: Product All expands to a valid in (select ...) that includes the tick boards" \
  'python3 -c "
import json,sys
v=[x for x in json.load(open(sys.argv[1]))[\"templating\"][\"list\"] if x[\"name\"]==\"product\"][0]
a=v[\"allValue\"].lower()
assert v[\"multi\"] and v[\"includeAll\"] and a.startswith(\"select \") and \"v_factory\" in a, a
" "$REPO/observability/grafana/dashboards/lookout/home.json"'
check "home.json: the real allValue inside the Factory tile in (...) returns the tick boards (sqlite)" \
  '[ "$(python3 "$REPO/tests/fixtures/product_allvalue_sqlite.py" "$REPO/observability/grafana/dashboards/lookout/home.json" | sort | tr "\n" " ")" = "capsule-corp sample-app " ]'
PRUNE_ENV=(CURL_BIN="$REPO/tests/fixtures/prune_curl_stub.sh" CAPSULE_SECRET_BIN="$REPO/tests/fixtures/prune_secret_stub.sh" GRAFANA_URL=http://stub)
PRUNE_ERR="$(PRUNE_MODE=err PRUNE_LOG="$PT/log2" env "${PRUNE_ENV[@]}" "$REPO/maintenance/grafana_prune_folders.sh" 2>&1)"; erc=$?
check "prune: an API error deletes nothing and fails with a FAILED line" \
  '[ $erc -ne 0 ] && ! grep -q "DELETE" "$PT/log2" && printf "%s" "$PRUNE_ERR" | grep -q "prune: FAILED"'
PRUNE_AGAIN="$(PRUNE_MODE=gone PRUNE_LOG="$PT/log3" env "${PRUNE_ENV[@]}" "$REPO/maintenance/grafana_prune_folders.sh" 2>&1)"; arc=$?
check "prune: a second run is a no-op (absent, no DELETE, exit 0)" \
  '[ $arc -eq 0 ] && ! grep -q "DELETE" "$PT/log3" && [ "$(printf "%s" "$PRUNE_AGAIN" | grep -c absent)" = 3 ]'
check "prune: wired into deploy.sh, with a skip line when Grafana is down" \
  'grep -q "maintenance/grafana_prune_folders.sh" "$REPO/deploy.sh" && grep -q "grafana empty-folder prune (grafana not answering)" "$REPO/deploy.sh"'
LOOKOUT_DASHBOARDS_OUT="$PT/lookout" DASHBOARDS_OUT="$PT/out" python3 "$REPO/observability/grafana/build_lookout.py" >/dev/null 2>&1
check "dashboards: committed lookout JSON is byte-identical to the build output" \
  'diff -r "$PT/lookout" "$REPO/observability/grafana/dashboards/lookout" >/dev/null'
# Z1: contracts/intent/grafana_prune_folders.jsonl replayed - each example names a check above that passed.
python3 -c 'import json,sys; rows=[json.loads(l) for l in open(sys.argv[1]) if l.strip()]; got=sys.argv[2].split("|"); sys.exit(0 if rows and all(r["expect"]["pass"] in got for r in rows) else 1)' \
  "$REPO/contracts/intent/grafana_prune_folders.jsonl" "$PASSED"; irc=$?
check "intent-examples" '[ $irc -eq 0 ]'
rm -r "$PT"

. "$REPO/tests/lib_docker_optin.sh"
if docker_optin test_front_door_caddy; then
  docker_ready test_front_door_caddy || exit 1
  IMG="$(sed -n 's/^    image: \(caddy:[^ ]*\)$/\1/p' "$C")"
  N="capsule-corp-front-door-test-$$"; T="$(mktemp -d)"
  trap 'docker stop "$N" "$N-up" >/dev/null 2>&1; docker network rm "$N" >/dev/null 2>&1; rm -r "$T"' EXIT
  check "caddy validates the Caddyfile" \
    'docker run --rm --label capsule=1 --label capsule.kind=test -v "$F:/etc/caddy/Caddyfile:ro" -v "$R:/etc/caddy/routes:ro" "$IMG" caddy validate --config /etc/caddy/Caddyfile >/dev/null 2>&1 && docker run --rm --label capsule=1 --label capsule.kind=test -v "$F:/etc/caddy/Caddyfile:ro" -v "$R:/etc/caddy/routes:ro" -e CC_CADDY_SECRETS=/etc/caddy/routes/secrets.caddy "$IMG" caddy validate --config /etc/caddy/Caddyfile >/dev/null 2>&1'
  # Routing: throwaway network, two upstreams named grafana and infisical, the real Caddyfile.
  docker network create "$N" >/dev/null
  echo ok-grafana >"$T/g"; echo ok-secrets >"$T/s"
  docker run -d --rm --label capsule=1 --label capsule.kind=test --name "$N-up" --network "$N" --network-alias grafana --network-alias infisical \
    -v "$T:/srv:ro" "$IMG" sh -c 'caddy file-server --listen :3000 --root /srv & exec caddy file-server --listen :8080 --root /srv' >/dev/null
  docker run -d --rm --label capsule=1 --label capsule.kind=test --name "$N" --network "$N" -p 127.0.0.1:0:80 -v "$F:/etc/caddy/Caddyfile:ro" \
    -v "$R:/etc/caddy/routes:ro" -e CC_CADDY_SECRETS=/etc/caddy/routes/secrets.caddy "$IMG" >/dev/null
  P="$(docker port "$N" 80/tcp | head -1 | sed 's/.*://')"
  sleep 3
  check "route cc.grafana.localhost reaches the grafana upstream" \
    '[ "$(curl -s -H "Host: cc.grafana.localhost" "http://127.0.0.1:$P/g")" = ok-grafana ]'
  check "route cc.secrets.localhost reaches the infisical upstream" \
    '[ "$(curl -s -H "Host: cc.secrets.localhost" "http://127.0.0.1:$P/s")" = ok-secrets ]'
  check "unknown host gets no route" \
    '[ "$(curl -s -o /dev/null -w "%{http_code}" -H "Host: cc.other.localhost" "http://127.0.0.1:$P/g")" != 200 ]'
fi
[ "$fails" -eq 0 ] && { echo "test_front_door: all passed"; exit 0; }
echo "test_front_door: $fails failed"; exit 1
