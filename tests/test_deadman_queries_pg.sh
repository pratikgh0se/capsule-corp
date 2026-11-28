#!/usr/bin/env bash
# t_c671377a: the three lightened dead-man queries (backup-missing, backup-partial, disk-high;
# gateway-down was the fourth until it was retired 2026-10-30, legacy gateways retired, D133)
# must return the same alert decision as the queries they replaced, on the same
# fixture rows, and must use the 005 partial indexes. Oracle 1: the old SQL (pinned below)
# versus the new rawSql extracted from rules.yaml. Oracle 2: EXPLAIN names the 005 indexes.
# Opt-in like tests/test_alert_rules_pg.sh (RUN_DOCKER_TESTS=1). Own throwaway container.
set -u
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
. "$REPO/tests/lib_docker_optin.sh"
docker_optin test_deadman_queries_pg || exit 77
docker_ready test_deadman_queries_pg || exit 1
docker_reap_pgtest
CT="capsule-pgtest-deadman-$$"   # not capsule-corp-*: storage_gc treats that prefix as the compose stack
trap 'docker stop "$CT" >/dev/null 2>&1 || true' EXIT   # --rm below: stopping removes container and volume
fails=0
check() { if eval "$2"; then echo "PASS $1"; else echo "FAIL $1"; fails=$((fails+1)); fi; }
q() { docker exec -i "$CT" psql -U ops -d ops -q -t -A -v ON_ERROR_STOP=1 -f - ; }
docker run -d --rm --label capsule=1 --label capsule.kind=test --name "$CT" \
  -e POSTGRES_USER=ops -e POSTGRES_PASSWORD=test \
  --mount type=volume,dst=/var/lib/postgresql/data,volume-label=capsule=1,volume-label=capsule.kind=test \
  -e POSTGRES_DB=ops \
  postgres:16-alpine >/dev/null 2>&1 || { echo "FAIL could not start postgres"; exit 1; }
for _ in $(seq 1 60); do docker exec "$CT" pg_isready -h 127.0.0.1 -U ops -d ops >/dev/null 2>&1 && break; sleep 1; done
q < "$REPO/observability/postgres/init.sql" >/dev/null || { echo "FAIL init.sql"; exit 1; }
for m in "$REPO"/observability/postgres/migrations/*.sql; do
  q < "$m" >/dev/null || { echo "FAIL $(basename "$m")"; exit 1; }
done
n="$(echo "select count(*) from pg_indexes where indexname in ('events_health_ts','events_backup_job_ts');" | q)"
check "005 created both partial indexes" '[ "$n" = "2" ]'

out="$(REPO="$REPO" CT="$CT" python3 - <<'PY'
import os, re, subprocess
from pathlib import Path
text = (Path(os.environ["REPO"]) / "observability/grafana/provisioning/alerting/rules.yaml").read_text()
new, thr = {}, {}
for b in re.split(r"(?m)^      - uid: ", text)[1:]:
    uid = b.split("\n", 1)[0].strip()
    m = re.search(r'rawSql: "((?:[^"\\]|\\.)*)"', b)
    if m:
        new[uid] = m.group(1)
        thr[uid] = float(re.search(r"params: \[([\d.]+)\]", b).group(1))
# The queries these three rules carried before t_c671377a, verbatim (gateway-down retired
# 2026-10-30, D133: its rule, old query and cases are gone).
OLD = {
 "backup-missing": "select extract(epoch from now()-max(ts))/3600 from events where kind='job' and payload->>'job'='backup' and (payload->>'exit')::int=0",
 "backup-partial": "select count(*) from events where kind='backup_partial' and ts>now()-interval '26 hours'",
 "disk-high": "select (payload->>'disk_pct')::float from events where kind='health' and source <> 'capsule' order by ts desc limit 1",
}
def ev(kind, ago, src="run.sh", payload="{}"):
    return (f"insert into events (ts,kind,source,severity,payload) values "
            f"(now()-interval '{ago}','{kind}','{src}','info','{payload}');")
def H(ago, disk, src="legacy"):
    p = '{"disk_pct": %s}' % disk
    return ev("health", ago, src, p)
def B(ago, ex): return ev("job", ago, payload='{"job":"backup","exit":%d}' % ex)
FIX = {
 "backup-missing": {"fresh ok": B("2 hours", 0), "only old ok": B("40 hours", 0),
    "fresh failed, old ok": B("1 hour", 1) + B("30 hours", 0),
    "ok then failed": B("3 hours", 0) + B("1 hour", 1),
    "other job only": ev("job", "1 hour", payload='{"job":"x","exit":0}'), "empty": ""},
 "backup-partial": {"recent": ev("backup_partial", "2 hours"),
    "stale": ev("backup_partial", "30 hours"), "empty": ""},
 "disk-high": {"high": H("5 minutes", 91), "ok": H("5 minutes", 40),
    "newest wins": H("1 hour", 95) + H("5 minutes", 40),
    "capsule rows ignored": H("5 minutes", 40) + H("1 minute", 99, src="capsule"), "empty": "",
    "25h old high": H("25 hours", 91), "23h59m old high": H("23 hours 59 minutes", 91),
    "24h01m old high": H("24 hours 1 minute", 91)},
}
# Intended differences: the 1-day window turns a stale newest row into no data (stays OK).
EXPECT = {(u, n): ("fires", "no-data") for u in ("disk-high",)
          for n in ("25h old high", "24h01m old high")}
def psql(sql):
    return subprocess.run(["docker", "exec", "-i", os.environ["CT"], "psql", "-U", "ops", "-d", "ops",
        "-q", "-t", "-A", "-v", "ON_ERROR_STOP=1", "-f", "-"], input=sql, text=True,
        capture_output=True, timeout=60)
def run(sql):
    r = psql(sql)
    last = r.stdout.strip().splitlines()[-1] if r.stdout.strip() else None
    return r.returncode, last, r.stderr.strip()
def decide(v, t): return "no-data" if v in (None, "") else ("fires" if float(v) > t else "quiet")
for uid, cases in FIX.items():
    for name, seed in cases.items():
        d = {}
        for tag, sql in (("old", OLD[uid]), ("new", new[uid])):
            rc, v, err = run("begin; delete from events; " + seed + " select (" + sql + "); rollback;")
            d[tag] = decide(v, thr[uid]) if rc == 0 else "ERR " + err[:60]
        want = EXPECT.get((uid, name), (d["old"], d["old"]))
        ok = (d["old"], d["new"]) == want and not d["new"].startswith("ERR")
        print(("PASS" if ok else "FAIL") + f" {uid} / {name}: old={d['old']} new={d['new']}")
for uid in FIX:
    plan = psql("set enable_seqscan = off; explain " + new[uid] + ";").stdout
    print(f"PLAN {uid}: " + (" ".join(l.strip() for l in plan.splitlines() if "Index" in l) or "NO INDEX"))
PY
)"
printf '%s\n' "$out"
check "every old/new decision pair matches" '! grep -q "^FAIL" <<<"$out" && grep -q "^PASS" <<<"$out"'
check "backup-missing and backup-partial plans use an index" '! grep -E "^PLAN (backup-missing|backup-partial)" <<<"$out" | grep -q "NO INDEX"'
check "disk-high plan uses events_health_ts (gateway-down retired 2026-10-30, D133)" '[ "$(grep -cE "^PLAN disk-high.*events_health_ts" <<<"$out")" = "1" ]'
[ $fails -eq 0 ] && echo "test_deadman_queries_pg: all passed" || { echo "test_deadman_queries_pg: $fails failed"; exit 1; }
