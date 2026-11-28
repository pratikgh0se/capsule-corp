#!/usr/bin/env bash
# Round 472 re-review (R1, HIGH): a rule's rawSql was a type error from the day it was written
# (`ts > extract(epoch from now()) - 600`, comparing `events.ts` - a `timestamptz`
# (observability/postgres/init.sql) - against a numeric epoch expression; Postgres rejects
# `timestamptz > numeric` outright at evaluation time) and every test on it was string-shape only
# (a substring/regex check on the YAML text), so nothing ever actually asked Postgres whether the
# SQL type-checks. This script closes that gap directly: every `rawSql` query in
# observability/grafana/provisioning/alerting/rules.yaml, extracted straight from the file (not
# retyped by hand here), is executed against a throwaway postgres:16-alpine loaded with the real
# `observability/postgres/init.sql` schema (empty table - every query here type-checks with zero
# rows; none of them need seeded data to prove their SQL is well-typed).
#
# Opt-in: skips (exit 77, prints SKIP) unless RUN_DOCKER_TESTS=1; with it set and docker down it FAILS, like tests/test_docker_image.sh
# and tests/test_migration_pg.sh. Never touches the live capsule-corp-postgres: its own container
# name, its own random host port, removed on exit.
set -u
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
. "$REPO/tests/lib_docker_optin.sh"
docker_optin test_alert_rules_pg || exit 77
docker_ready test_alert_rules_pg || exit 1
docker_reap_pgtest

CT="capsule-pgtest-alert-rules-$$"   # not capsule-corp-*: storage_gc treats that prefix as the compose stack
cleanup() { docker stop "$CT" >/dev/null 2>&1 || true; }   # --rm below: stopping removes container and volume
trap cleanup EXIT

fails=0
check() { if eval "$2"; then echo "PASS $1"; else echo "FAIL $1"; fails=$((fails+1)); fi; }

q() { docker exec -i "$CT" psql -U ops -d ops -q -t -A -v ON_ERROR_STOP=1 -f - ; }

docker run -d --rm --label capsule=1 --label capsule.kind=test --name "$CT" \
  -e POSTGRES_USER=ops -e POSTGRES_PASSWORD=test \
  --mount type=volume,dst=/var/lib/postgresql/data,volume-label=capsule=1,volume-label=capsule.kind=test \
  -e POSTGRES_DB=ops postgres:16-alpine >/dev/null 2>&1 \
  || { echo "FAIL test_alert_rules_pg: could not start postgres:16-alpine"; exit 1; }

for _ in $(seq 1 60); do   # -h: the image's init-time server has no TCP socket, so only the final server answers
  docker exec "$CT" pg_isready -h 127.0.0.1 -U ops -d ops >/dev/null 2>&1 && break
  sleep 1
done
docker exec "$CT" pg_isready -h 127.0.0.1 -U ops -d ops >/dev/null 2>&1 \
  || { echo "FAIL test_alert_rules_pg: postgres never became ready"; exit 1; }

q < "$REPO/observability/postgres/init.sql" >/dev/null \
  || { echo "FAIL test_alert_rules_pg: init.sql failed to apply"; exit 1; }
# card-stuck reads `samples`, created by a migration, not by init.sql
for m in "$REPO"/observability/postgres/migrations/*.sql; do
  q < "$m" >/dev/null \
    || { echo "FAIL test_alert_rules_pg: $(basename "$m") failed to apply"; exit 1; }
done

# Extract every (uid, rawSql) pair straight from rules.yaml - both the `rawSql: |` block-scalar
# form and the `rawSql: "..."` single-line form - and print `uid<TAB>base64(sql)` one per line so
# the shell loop below never has to worry about embedded quotes/newlines.
pairs="$(python3 - "$REPO/observability/grafana/provisioning/alerting/rules.yaml" <<'PY'
import base64, re, sys

text = open(sys.argv[1]).read()
blocks = re.split(r"(?=      - uid: )", text)
for b in blocks:
    m = re.match(r"      - uid: (\S+)", b)
    if not m:
        continue
    uid = m.group(1)
    for sm in re.finditer(r'rawSql:\s*\|\n((?:.+\n)+?)\s*(?:- refId:|\Z)', b):
        sql = sm.group(1)
        print(uid + "\t" + base64.b64encode(sql.encode()).decode())
    for sm in re.finditer(r'rawSql:\s*"((?:[^"\\]|\\.)*)"', b):
        sql = sm.group(1).replace('\\"', '"')
        print(uid + "\t" + base64.b64encode(sql.encode()).decode())
PY
)"

n_pairs="$(printf '%s\n' "$pairs" | grep -c .)"
# the Loki rule (loki-ingest-deadman) has no SQL; every ops-pg rule has exactly one rawSql
n_uids="$(grep -c 'datasourceUid: ops-pg' "$REPO/observability/grafana/provisioning/alerting/rules.yaml")"
check "found exactly one rawSql query per rule ($n_uids rules)" '[ "$n_pairs" -eq "$n_uids" ]'

while IFS=$'\t' read -r uid b64; do
  [ -z "$uid" ] && continue
  sql="$(printf '%s' "$b64" | base64 -d)"
  out="$(printf '%s' "$sql" | q 2>&1)"; rc=$?
  check "rule $uid: rawSql type-checks against the real events schema" '[ "$rc" -eq 0 ]'
  if [ "$rc" -ne 0 ]; then
    echo "  -> $out" | head -3
  fi
done <<<"$pairs"

[ $fails -eq 0 ] && echo "test_alert_rules_pg: all passed" || { echo "test_alert_rules_pg: $fails failed"; exit 1; }
