#!/usr/bin/env bash
# Round-trips the real SQL against a throwaway postgres:16-alpine: applies the
# BASE schema (init.sql minus the envelope, i.e. what a live volume already
# holds), runs the migrations through db_migrate.py, then stages one batch
# through the real events.ship() SQL and checks the row count and the dedupe.
#
# Why this exists: this branch shipped two Criticals and both were "the mock
# agreed with the code". test_events.py asserts the script text against a
# hand-written CSV, and `_psql_copy` short-circuits under AGENT_OPS_NO_SEND, so
# nothing noticed that csv.writer's CRLF makes psql's script-mode COPY reject
# every single data line (final-review C1). One real container catches it.
#
# Opt-in: skips (exit 77, prints SKIP) unless RUN_DOCKER_TESTS=1; with it set and docker down it FAILS, like
# tests/test_docker_image.sh. Never touches the live capsule-corp-postgres: its own
# container name, its own random host port, removed on exit.
set -u
# CI exports AGENT_OPS_NO_DOCKER=1 for the whole shell step; db_migrate.py returns early on it,
# and every check below drives the real container through db_migrate.
unset AGENT_OPS_NO_DOCKER
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
. "$REPO/tests/lib_docker_optin.sh"
docker_optin test_migration_pg || exit 77
docker_ready test_migration_pg || exit 1
docker_reap_pgtest

CT="capsule-pgtest-migration-$$"   # not capsule-corp-*: storage_gc treats that prefix as the compose stack
TMP="$(mktemp -d)"
cleanup() { docker stop "$CT" >/dev/null 2>&1 || true; rm -rf "$TMP"; }   # --rm below: stop removes container and volume
trap cleanup EXIT

fails=0
check() { if eval "$2"; then echo "PASS $1"; else echo "FAIL $1"; fails=$((fails+1)); fi; }

# psql inside the throwaway container, tuples-only + unaligned so a count comes
# back as bare digits.
q()  { docker exec -i "$CT" psql -U ops -d ops -q -t -A -v ON_ERROR_STOP=1 -f - ; }

docker run -d --rm --label capsule=1 --label capsule.kind=test --name "$CT" \
  -e POSTGRES_USER=ops -e POSTGRES_PASSWORD=test \
  --mount type=volume,dst=/var/lib/postgresql/data,volume-label=capsule=1,volume-label=capsule.kind=test \
  -e POSTGRES_DB=ops postgres:16-alpine >/dev/null 2>&1 \
  || { echo "FAIL test_migration_pg: could not start postgres:16-alpine"; exit 1; }

# Deadline-based so a loaded machine (load avg ~30 took >60s) is not a false failure; a real failure
# still times out and prints the container state and last log lines.
PG_READY_TIMEOUT="${PG_READY_TIMEOUT:-180}"
PG_READY_PORT="${PG_READY_PORT:-5432}"
deadline=$((SECONDS + PG_READY_TIMEOUT))
until docker exec "$CT" pg_isready -h 127.0.0.1 -U ops -d ops -p "$PG_READY_PORT" >/dev/null 2>&1; do   # -h: the image's init-time server has no TCP socket, so only the final server answers
  if [ "$SECONDS" -ge "$deadline" ]; then
    echo "FAIL test_migration_pg: postgres never became ready within ${PG_READY_TIMEOUT}s"
    echo "container state: $(docker inspect -f '{{.State.Status}} exit={{.State.ExitCode}}' "$CT" 2>&1)"
    # --rm: a crashed container is already gone (state shows "No such object"); a hung one is still
    # here, so its logs are read now, before the EXIT trap stops it.
    docker logs --tail 30 "$CT" 2>&1 || echo "no logs: $CT already gone (--rm removed it)"
    exit 1
  fi
  sleep 1
done

# --- the pre-migration table: init.sql with the two envelope columns and the
# partial index stripped out, which is what a volume created before this branch
# actually holds.
# Comment lines are dropped first, so the assertion below reads the SQL and
# not init.sql's prose (which talks about both columns on purpose).
sed '/^-- The Lookout (003-lookout.sql/,$d' "$REPO/observability/postgres/init.sql" \
  | grep -v '^ *--' \
  | grep -v -e '^  v  *smallint' -e '^  event_id  *text' -e 'events_event_id' \
  > "$TMP/base.sql"
check "base schema has no envelope columns" '! grep -qE "event_id|smallint" "$TMP/base.sql"'
check "base schema still creates the events table" 'grep -q "CREATE TABLE IF NOT EXISTS events" "$TMP/base.sql"'
check "base schema has no Lookout table (so 003 really creates them)" '! grep -q "cap_cards" "$TMP/base.sql"'
q < "$TMP/base.sql" >/dev/null || { echo "FAIL test_migration_pg: base schema failed"; exit 1; }

# db_migrate.py talks to a container name and a docker PATH of its own, so
# point its psql door at ours instead of forking the module.
run_migrate() {
  AGENT_OPS_MIGRATE_ARGS="$*" python3 - <<'PY'
import os, subprocess, sys
from pathlib import Path
sys.path.insert(0, str(Path(os.environ["REPO"]) / "maintenance"))
import db_migrate

def psql(sql):
    r = subprocess.run(["docker", "exec", "-i", os.environ["CT"], "psql", "-U", "ops", "-d", "ops",
                        "-q", "-t", "-A", "-v", "ON_ERROR_STOP=1", "-f", "-"],
                       input=sql, text=True, capture_output=True, timeout=120)
    return r.returncode, (r.stdout or "") + (r.stderr or "")

args = (os.environ.get("AGENT_OPS_MIGRATE_ARGS") or "").split()
sys.exit(db_migrate.main(args, psql=psql))
PY
}
export REPO CT

# --- 1. --list / --check must report drift and write NOTHING (finding I3).
# Both 001 and 002 are pending against the base schema - db_migrate.py applies
# every *.sql under observability/postgres/migrations/ in order, not just one.
out="$(run_migrate --list 2>&1)"; rc=$?
check "--list reports 001 pending" '[ $rc -eq 0 ] && grep -q "pending 001-event-envelope.sql" <<<"$out"'
check "--list reports 002 pending" '[ $rc -eq 0 ] && grep -q "pending 002-samples.sql" <<<"$out"'
check "--list reports 003 pending" '[ $rc -eq 0 ] && grep -q "pending 003-lookout.sql" <<<"$out"'
check "--list reports 004 pending" '[ $rc -eq 0 ] && grep -q "pending 004-park-labels.sql" <<<"$out"'
check "--list reports 005 pending" '[ $rc -eq 0 ] && grep -q "pending 005-deadman-indexes.sql" <<<"$out"'
check "--list reports 006 pending" '[ $rc -eq 0 ] && grep -q "pending 006-event-envelope-cols.sql" <<<"$out"'
run_migrate --check >/dev/null 2>&1
check "--check exits 1 while a migration is pending" '[ $? -eq 1 ]'
n="$(echo "select count(*) from pg_class where relname = 'schema_migrations';" | q)"
check "--check/--list created no schema_migrations table" '[ "$n" = "0" ]'

# --- 2. apply.
out="$(run_migrate 2>&1)"; rc=$?
check "apply succeeds" '[ $rc -eq 0 ] && grep -q "applied 001-event-envelope.sql" <<<"$out" && grep -q "applied 002-samples.sql" <<<"$out"'
n="$(echo "select count(*) from schema_migrations;" | q)"
check "exactly eleven ledger rows" '[ "$n" = "11" ]'
n="$(echo "select count(*) from information_schema.columns where table_name='events' and column_name in ('event_name','run_id','trace_id','span_id');" | q)"
check "006 added the four envelope columns" '[ "$n" = "4" ]'
n="$(echo "select count(*) from information_schema.columns where table_name='events' and column_name in ('v','event_id');" | q)"
check "both envelope columns exist" '[ "$n" = "2" ]'
n="$(echo "select count(*) from pg_indexes where tablename='events' and indexname='events_event_id';" | q)"
check "the partial unique index exists" '[ "$n" = "1" ]'
n="$(echo "select to_regclass('samples');" | q)"
check "the samples table exists after migrating" '[ "$n" = "samples" ]'
n="$(echo "select count(*) from schema_migrations where name = '002-samples.sql';" | q)"
check "002-samples.sql is recorded in schema_migrations" '[ "$n" = "1" ]'

# --- 3. idempotent: a second apply changes nothing.
out="$(run_migrate 2>&1)"
check "second apply is a no-op" 'grep -q "nothing pending" <<<"$out"'
n="$(echo "select count(*) from schema_migrations;" | q)"
check "still exactly eleven ledger rows" '[ "$n" = "11" ]'
n="$(echo "select count(*) from information_schema.columns where table_name='events' and column_name in ('event_name','run_id','trace_id','span_id');" | q)"
check "006 added the four envelope columns" '[ "$n" = "4" ]'
run_migrate --check >/dev/null 2>&1
check "--check exits 0 once applied" '[ $? -eq 0 ]'

# --- 3b. 009 samples_hourly (D255): table exists, the probe's upsert fills it, and a rerun
# updates the same bucket instead of adding a row.
n="$(echo "select to_regclass('samples_hourly');" | q)"
check "009 created samples_hourly" '[ "$n" = "samples_hourly" ]'
echo "insert into samples (ts, service, name, value) values (now(), 'capsule', 'rollup.t', 2), (now(), 'capsule', 'rollup.t', 4);" | q >/dev/null
ROLLUP="$(REPO="$REPO" python3 -c "import os, sys; sys.path.insert(0, os.environ['REPO'] + '/maintenance'); import obs_probe_ship; print(obs_probe_ship.ROLLUP_SQL)")"
echo "$ROLLUP;" | q >/dev/null; echo "$ROLLUP;" | q >/dev/null
n="$(echo "select count(*) || '/' || max(n) || '/' || avg(avg_value) || '/' || avg(total_avg) || '/' || max(ticks) from samples_hourly where name = 'rollup.t';" | q)"
check "rollup upsert is idempotent and averages the hour and totals the tick" '[ "$n" = "1/2/3/6/1" ]'
echo "delete from samples where name = 'rollup.t'; delete from samples_hourly where name = 'rollup.t';" | q >/dev/null

# --- 4. one real batch through events.ship()'s own CSV and SQL. This is the C1
# guard: `ship()` builds the CSV, `_stage_script` builds the script, and psql
# reads it as a script (`-f -`) exactly as production does.
ship() {
  python3 - <<'PY'
import json, os, subprocess, sys, tempfile, time
from pathlib import Path
sys.path.insert(0, str(Path(os.environ["REPO"]) / "maintenance"))
os.environ.pop("AGENT_OPS_NO_SEND", None)
import events

d = Path(tempfile.mkdtemp())
spool, off = d / "e.jsonl", d / "e.offset"
# A row that exercises every CSV hazard at once: the delimiter, a quote, an
# embedded newline, and a unicode char.
events.log("job", "run.sh", 'a, b "c"\nd é', title="t,1", payload={"job": "x"}, spool=spool)
events.log("card", "bridge", "second row", task_id="t_1", payload={"event": "merged"}, spool=spool)

sent = {}
def copier(csv_text):
    sent["csv"] = csv_text
    r = subprocess.run(["docker", "exec", "-i", os.environ["CT"], "psql", "-U", "ops", "-d", "ops",
                        "-q", "-v", "ON_ERROR_STOP=1", "-f", "-"],
                       input=events._stage_script(csv_text), text=True, capture_output=True, timeout=60)
    if r.returncode != 0:
        print("PSQL_ERR " + (r.stderr or "").strip().replace("\n", " | "), file=sys.stderr)
    return r.returncode == 0

n = events.ship(spool=spool, offset_path=off, copier=copier)
assert "\r" not in sent.get("csv", ""), "the CSV ship() produced carries a CR"
print(n)
# replay the identical batch: ON CONFLICT (event_id) must swallow it
off.unlink()
n2 = events.ship(spool=spool, offset_path=off, copier=copier)
print(n2)
PY
}
out="$(ship 2>&1)"
check "ship() staged the batch through real psql (no CRLF rejection)" '[ "$(sed -n 1p <<<"$out")" = "2" ]'
check "psql reported no error" '! grep -q PSQL_ERR <<<"$out"'
n="$(echo "select count(*) from events;" | q)"
check "two rows landed" '[ "$n" = "2" ]'
n="$(echo "select count(*) from events where event_id is null;" | q)"
check "every new row carries an event_id" '[ "$n" = "0" ]'
n="$(echo "select count(*) from events where v = 1;" | q)"
check "every new row carries v = 1" '[ "$n" = "2" ]'
# The hazardous text survived COPY intact. `_clean` strips control characters
# but deliberately keeps \n, so the embedded newline really did travel inside a
# quoted CSV field and through COPY - which is the case the LF row terminator
# must not have flattened.
n="$(echo "select count(*) from events where text = 'a, b \"c\"' || chr(10) || 'd é';" | q)"
check "the comma/quote/newline/unicode text round-tripped byte-for-byte" '[ "$n" = "1" ]'
n="$(echo "select count(*) from events where title = 't,1';" | q)"
check "a title holding the CSV delimiter round-tripped" '[ "$n" = "1" ]'
n="$(echo "select count(*) from events where payload->>'job' = 'x';" | q)"
check "the jsonb payload round-tripped" '[ "$n" = "1" ]'

# --- 5. the replay was deduped, not doubled.
check "the replayed batch reported its rows" '[ "$(sed -n 2p <<<"$out")" = "2" ]'
n="$(echo "select count(*) from events;" | q)"
check "ON CONFLICT (event_id) deduped the replay" '[ "$n" = "2" ]'

# --- 6. a legacy row (no event_id) is still insertable and is NOT deduped away
# by the partial index - two NULLs do not conflict.
n="$(echo "insert into events (ts, kind, source, severity, payload) values (now(), 'job', 's', 'info', '{}'), (now(), 'job', 's', 'info', '{}'); select count(*) from events where event_id is null;" | q)"
check "two legacy event_id-NULL rows coexist under the partial index" '[ "$n" = "2" ]'

# --- 7. obs_probe.ship() COPY round-trip into the real samples table (Task 2
# review, Important 3). This is the same C1-shaped risk section 4 above
# guards for events: a mock `run` agreeing with obs_probe.to_csv()'s own CSV
# would never catch a delimiter/quoting bug that only a real psql COPY
# rejects. One label deliberately carries both a comma (the CSV delimiter)
# and a double quote (the CSV quote character) at once.
obs_ship() {
  python3 - <<'PY'
import datetime, os, subprocess, sys
from pathlib import Path
sys.path.insert(0, str(Path(os.environ["REPO"]) / "maintenance"))
import obs_probe

CT = os.environ["CT"]
now = datetime.datetime(2026, 9, 8, 12, 0, 0, tzinfo=datetime.timezone.utc)
samples = [
    obs_probe.Sample(ts=now.isoformat(), service="capsule", name="capsule.container.up",
                      value=1.0, labels={"container": 'capsule-corp-postgres, "prod"'}),
    obs_probe.Sample(ts=now.isoformat(), service="capsule", name="capsule.host.load1",
                      value=2.79, labels={}),
]
csv_text = obs_probe.to_csv(samples)

def run(argv, **kwargs):
    # obs_probe.ship()'s own argv aims at capsule-corp-postgres; redirect the
    # docker exec target at the throwaway container this test owns while
    # keeping the rest of the command - the psql args, the -c COPY string,
    # the CSV on stdin - exactly as ship() built it.
    real_argv = ["docker", "exec", "-i", CT] + argv[4:]
    r = subprocess.run(real_argv, input=kwargs.get("input"), text=True, capture_output=True, timeout=30)
    if r.returncode != 0:
        print("PSQL_ERR " + (r.stderr or "").strip().replace("\n", " | "), file=sys.stderr)
    return r

ok = obs_probe.ship(csv_text, run)
print("SHIP_OK" if ok else "SHIP_FAIL")
PY
}
out="$(obs_ship 2>&1)"
check "obs_probe.ship() COPY succeeded against the real samples table" 'grep -q SHIP_OK <<<"$out"'
check "psql reported no error" '! grep -q PSQL_ERR <<<"$out"'
n="$(echo "select count(*) from samples;" | q)"
check "two obs_probe samples landed via the real COPY" '[ "$n" = "2" ]'
lbl="$(printf "select labels->>'container' from samples where name='capsule.container.up';" | q)"
check "a label holding both the CSV delimiter and the CSV quote char round-tripped" '[ "$lbl" = "capsule-corp-postgres, \"prod\"" ]'
n="$(echo "select value from samples where name='capsule.host.load1';" | q)"
check "the unlabelled sample's value round-tripped" '[ "$n" = "2.79" ]'

# --- Lookout 003 (spec 5.3/5.4): tables, seed, views, v_run_cost pricing.
for t in cap_cards cap_runs cap_blocks cap_epics cap_prs cap_commits cap_incidents cap_tick prices cap_event_words; do
  n="$(echo "select to_regclass('$t');" | q)"
  check "003 created $t" '[ "$n" = "$t" ]'
done
n="$(echo "select count(*) from cap_event_words;" | q)"
check "003+004 seeded 67 event words" '[ "$n" = "67" ]'
LEGACY="$(cd "$REPO" && python3 -c 'from capsule import human; print(human.LEGACY_OWNER)')"
n="$(echo "select count(*) from cap_event_words where actor = '$LEGACY';" | q)"
check "011 moved every legacy actor row to owner" '[ "$n" = "0" ]'
n="$(echo "select count(*) from cap_event_words where actor = 'owner';" | q)"
check "011: the person's event words now say owner (10)" '[ "$n" = "10" ]'
echo "insert into cap_event_words values ('legacy_probe', 'x', '$LEGACY'); delete from cap_event_words where kind = 'legacy_probe';" | q >/dev/null 2>&1
check "011: the check still accepts the legacy actor value (read compatibility)" '[ $? -eq 0 ]'
echo "insert into cap_event_words values ('bad_probe', 'x', 'nobody');" | q >/dev/null 2>&1
check "011: the check still refuses an unknown actor" '[ $? -ne 0 ]'
n="$(echo "select count(*) from cap_event_words where kind in ('parked','parked_by_human','failure_limit');" | q)"
check "the three park kinds are labelled" '[ "$n" = "3" ]'
for v in v_floor v_waiting_on_owner v_run_cost v_card_story v_since v_factory; do
  echo "select * from $v limit 1;" | q >/dev/null 2>&1
  check "view $v selects without error" '[ $? -eq 0 ]'
done
n="$(cat <<'SQL' | q
insert into prices values ('claude-opus-5', '2026-09-01', 5, 25, 0.5, null);
insert into cap_runs (board, run_id, snap_ts, pass_id, task_id, model, started_at, tokens_in, tokens_out,
                      tokens_cache_read, era)
  values ('b', 'r1', now(), 'p', 't', 'claude-opus-5', '2026-09-20', 1000000, 1000000, 1000000, 'capsule');
select round(usd_est::numeric, 4) from v_run_cost where run_id = 'r1';
SQL
)"
check "v_run_cost prices a fixture run (5 + 25 + 0.5 USD, cache write null counts 0)" '[ "$n" = "30.5000" ]'
n="$(cat <<'SQL' | q
insert into cap_cards (board, task_id, snap_ts, pass_id, title, status, product, phase, block_kind, waiting_on)
values
 ('b', 'w1', now(), 'p', 'real ask', 'blocked', 'b', 'waiting', 'needs_input', 'owner'),
 ('b', 'w2', now(), 'p', 'stale gate', 'done', 'b', 'waiting', 'needs_input', 'owner'),
 ('b', 'w3', now(), 'p', 'parked on purpose', 'blocked', 'b', 'waiting', 'parked', 'owner'),
 ('b', 'w4', now(), 'p', 'failed too often', 'parked', 'b', 'waiting', 'failure_limit', 'owner'),
 ('b', 'w5', now(), 'p', 'retired board: thing', 'blocked', 'b', 'waiting', 'needs_input', 'owner'),
 ('b', 'w6', now(), 'p', 'screen hold', 'done', 'b', 'waiting', 'first_screen_hold', 'owner');
select string_agg(substr(item, 1, 2), ',' order by item) from v_waiting_on_owner;
delete from cap_cards where board = 'b';
SQL
)"
check "W361: v_waiting_on_owner keeps only live decisions (not done or parked)" '[ "$n" = "w1,w5,w6" ]'
n="$(echo "select source from v_since;" | q)"
check "v_since falls back to 24h with no morning brief" '[ "$n" = "fallback_24h" ]'
n="$(cat <<'SQL' | q
insert into events (v, event_id, ts, kind, source, severity, payload)
  values (1, 'm1', now() - interval '2 hours', 'job', 'run.sh', 'info', '{"job":"morning","phase":"end","exit":0}');
select source from v_since;
SQL
)"
check "v_since reads the morning brief end row" '[ "$n" = "morning" ]'
n="$(echo "select date_trunc('day', timestamptz '2026-09-27 06:45+07' at time zone 'Asia/Bangkok') at time zone 'Asia/Bangkok' = timestamptz '2026-09-27 00:00+07';" | q)"
check "A11: a 06:45 +07 row lands on its own local day" '[ "$n" = "t" ]'

# --- Lookout retention (spec 5.11): 13/15/29/31/179/181-day rows per class.
cat <<'SQL' | q >/dev/null
truncate events;
insert into events (ts, kind, source, severity, payload)
select now() - (d || ' days')::interval, k, 's', 'info', '{}'
from unnest(array[13, 15, 29, 31, 179, 181]) d, unnest(array['card', 'health', 'tick_health', 'job']) k;
SQL
q < "$REPO/maintenance/retention.sql" >/dev/null
n="$(echo "select string_agg(kind || ':' || count, ',' order by kind) from (select kind, count(*) from events group by kind) x;" | q)"
check "retention keeps card<=179, health/tick_health<=13, job<=29" '[ "$n" = "card:5,health:1,job:3,tick_health:1" ]'
# A board whose tick died: only 20- and 30-day-old heartbeats. Its newest row must survive (the dead-man
# picks boards from each board's latest tick_health), the older one must go.
cat <<'SQL' | q >/dev/null
insert into events (ts, kind, source, severity, board, payload)
select now() - (d || ' days')::interval, 'tick_health', 's', 'info', 'dead', '{}' from unnest(array[20, 30]) d;
SQL
q < "$REPO/maintenance/retention.sql" >/dev/null
n="$(echo "select count(*) || ':' || (count(*) filter (where ts < now() - interval '25 days')) from events where board = 'dead';" | q)"
check "retention keeps a dead board's newest tick_health (20 d) and deletes its older one (30 d)" '[ "$n" = "1:0" ]'

# --- Lookout alert intent examples (spec 7): seed an empty schema, run the rule's exact rawSql.
alerts_pg() {
  python3 - <<'PY'
import json, os, re, subprocess
from pathlib import Path
repo = Path(os.environ["REPO"])
text = (repo / "observability/grafana/provisioning/alerting/rules.yaml").read_text()
rules = {}
for block in re.split(r"(?m)^      - uid: ", text)[1:]:
    uid = block.split("\n", 1)[0].strip()
    m = re.search(r'rawSql: "((?:[^"\\]|\\.)*)"', block)
    if m:
        rules[uid] = (m.group(1), float(re.search(r"params: \[([\d.]+)\]", block).group(1)))
def q(sql):
    r = subprocess.run(["docker", "exec", "-i", os.environ["CT"], "psql", "-U", "ops", "-d", "ops", "-q", "-t", "-A",
                        "-v", "ON_ERROR_STOP=1", "-f", "-"], input=sql, text=True, capture_output=True, timeout=60)
    return r.returncode, r.stdout.strip(), r.stderr.strip()
for line in (repo / "contracts/intent/lookout/alerts.jsonl").read_text().splitlines():
    ex = json.loads(line)
    sql, threshold = rules[ex["rule"]]
    rc, out, err = q("begin; truncate cap_tick, cap_cards, events; " + ex["seed"] + "; select (" + sql
                     + ") ; rollback;")
    value = float(out.splitlines()[-1]) if rc == 0 and out else None
    got = "fires" if value is not None and value > threshold else "quiet"
    print(("PASS" if got == ex["expect"] else "FAIL") + " alert intent " + ex["id"] + f" (value {value}, {err[:80]})")
PY
}
out="$(alerts_pg 2>&1)"; printf '%s\n' "$out"
check "every alert intent example evaluates as expected" '! grep -qE "^FAIL|Traceback" <<<"$out" && [ "$(grep -c "^PASS alert intent" <<<"$out")" = "$(grep -c . "$REPO/contracts/intent/lookout/alerts.jsonl")" ]'

# --- Lookout shipper (spec 11.1 I1, I3, I4, I11 pg halves).
lookout_ship_pg() {
  python3 - <<'PY'
import json, os, sys
from pathlib import Path
sys.path.insert(0, os.environ["REPO"])
from capsule import lookout_ship

# ship through the real lookout_ship.psql(), with only the container name redirected.
lookout_ship.CONTAINER = os.environ["CT"]

def run(lines):
    parsed = [lookout_ship.parse(json.dumps(l)) for l in lines]
    boards = {d.get("board") for _, d in parsed if d.get("board")}
    script = lookout_ship.build_script(parsed, left={b: 0 for b in boards}, prices=None)
    rc, out = lookout_ship.psql(script, timeout=60)
    if rc:
        print("PSQL_ERR " + out.strip().replace("\n", " | "), file=sys.stderr)

ev = lambda i: {"v": 1, "t": "event", "row": {"v": 1, "event_id": f"lk{i}", "ts": "2026-09-27T05:00:00+00:00",
     "kind": "card", "source": "capsule", "board": "pomo", "task_id": "t_1", "slug": None,
     "title": 'a, b "c"\nd é', "text": None, "link": None, "severity": "info", "payload": {"event": "claimed"}}}
mark = lambda n: {"v": 1, "t": "tickmark", "board": "pomo", "snap_ts": f"2026-09-27T05:00:{n:02d}+00:00",
     "pass_id": f"h:1:{n}", "row": {"host": "h", "pid": 1, "harness_sha": "abc",
     "last_pass_at": f"2026-09-27T05:00:{n:02d}+00:00", "pass_ms": n, "lookout_ms": 1, "counts": {"n": n},
     "running": 0, "ready": 0, "blocked": 0, "paused_accounts": ["default"], "spool_bytes_unshipped": 0}}
card = lambda ts, title: {"v": 1, "t": "snap", "table": "cap_cards", "board": "pomo", "snap_ts": ts,
     "pass_id": "h:1:1", "row": {"task_id": "t_1", "title": title, "status": "ready", "product": "pomo",
     "phase": "queued", "failure_count": 0, "consecutive_failures": 0, "commits": 0, "review_rounds": 0}}
run([ev(1), ev(2), ev(3), mark(1)])                                  # I1
run([ev(1), ev(2), ev(3), mark(1)])                                  # I3 replay
run([card("2026-09-27T06:00:00+00:00", "new")])
run([card("2026-09-27T05:00:00+00:00", "old")])                      # I4 older snap
run([mark(n) for n in range(59, -1, -1)] + [mark(59)])               # I11: out of order, newest wins
PY
}
out="$(lookout_ship_pg 2>&1)"
check "lookout shipper scripts ran without a psql error" '! grep -q PSQL_ERR <<<"$out"'
n="$(echo "select count(*) from events where event_id like 'lk%';" | q)"
check "I1/I3: three event rows after a replay" '[ "$n" = "3" ]'
n="$(echo "select count(*) from events where event_id = 'lk1' and title = 'a, b \"c\"' || chr(10) || 'd é';" | q)"
check "CSV hazards in a title round-trip byte-for-byte" '[ "$n" = "1" ]'
n="$(echo "select title from cap_cards where board = 'pomo' and task_id = 't_1';" | q)"
check "I4: an older snap never overwrites a newer row" '[ "$n" = "new" ]'
n="$(echo "select pass_ms from cap_tick where board = 'pomo';" | q)"
check "I11: cap_tick equals the newest pass" '[ "$n" = "59" ]'
n="$(echo "select paused_accounts[1] from cap_tick where board = 'pomo';" | q)"
check "a JSON array lands in a text[] column" '[ "$n" = "default" ]'
n="$(echo "select count(*) from cap_tick where board = 'pomo' and last_ship_ok_at is not null;" | q)"
check "I1: last_ship_ok_at set in the same transaction" '[ "$n" = "1" ]'

# --- Fix round 1 (review C1): a real NOT NULL violation must actually carry a SQLSTATE 23xxx in
# verbose output - _recoverable()'s regex is checked against real psql here, not only the fake
# docker's canned text. A row-shaped violation (item 1) is the row-quarantine path; anything else
# (42P10 schema drift, ...) must be reproduced against a real, broken container to pin - that one
# is out of scope for a throwaway postgres:16-alpine with the correct, current schema, so this
# case pins the recoverable half only, which is what the fake docker's canned SQLSTATE stands in
# for everywhere else.
notnull_pg() {
  python3 - <<'PY'
import json, os, sys
from pathlib import Path
sys.path.insert(0, os.environ["REPO"])
from capsule import lookout_ship

lookout_ship.CONTAINER = os.environ["CT"]
bad = {"v": 1, "t": "snap", "table": "cap_cards", "board": "pomo", "snap_ts": "2026-09-27T07:00:00+00:00",
       "pass_id": "h:1:1", "row": {"task_id": "t_2", "title": "no status", "product": "pomo",
       "phase": "queued", "failure_count": 0, "consecutive_failures": 0, "commits": 0, "review_rounds": 0}}
# `status` (not null) deliberately omitted: jsonb_populate_record leaves it NULL, and the real
# table's `status text not null` constraint rejects it.
parsed = [lookout_ship.parse(json.dumps(bad))]
script = lookout_ship.build_script(parsed, left={"pomo": 0}, prices=None)
rc, out = lookout_ship.psql(script, timeout=60)
print(f"NOTNULL_RC {rc}")
print(f"NOTNULL_RECOVERABLE {lookout_ship._recoverable(out)}")
PY
}
out="$(notnull_pg 2>&1)"
check "a real NOT NULL violation exits 3 (psql's ON_ERROR_STOP code)" 'grep -q "^NOTNULL_RC 3$" <<<"$out"'
check "its real verbose SQLSTATE (23502) classifies as recoverable" 'grep -q "^NOTNULL_RECOVERABLE True$" <<<"$out"'

# --- Round 2 (review I-R2-1): a bad int/bool/array value through jsonb_populate_record raises
# 22P02 (invalid_text_representation), a letter-bearing SQLSTATE - the old digits-only regex
# missed it against real psql, the same way it missed the fake docker's canned 22P02. This pins
# the letter-bearing half against the real server; 23502 above still pins the all-digit half.
badint_pg() {
  python3 - <<'PY'
import json, os, sys
from pathlib import Path
sys.path.insert(0, os.environ["REPO"])
from capsule import lookout_ship

lookout_ship.CONTAINER = os.environ["CT"]
bad = {"v": 1, "t": "snap", "table": "cap_cards", "board": "pomo", "snap_ts": "2026-09-27T07:00:00+00:00",
       "pass_id": "h:1:1", "row": {"task_id": "t_3", "title": "bad int", "product": "pomo", "status": "todo",
       "phase": "queued", "failure_count": "not-a-number", "consecutive_failures": 0, "commits": 0,
       "review_rounds": 0}}
# `failure_count` (integer) holds a non-numeric string: jsonb_populate_record's own cast fails
# with 22P02 (invalid_text_representation), before any constraint is even checked.
parsed = [lookout_ship.parse(json.dumps(bad))]
script = lookout_ship.build_script(parsed, left={"pomo": 0}, prices=None)
rc, out = lookout_ship.psql(script, timeout=60)
print(f"BADINT_RC {rc}")
print(f"BADINT_RECOVERABLE {lookout_ship._recoverable(out)}")
PY
}
out="$(badint_pg 2>&1)"
check "a real bad-int cast exits 3 (psql's ON_ERROR_STOP code)" 'grep -q "^BADINT_RC 3$" <<<"$out"'
check "its real verbose SQLSTATE (22P02) classifies as recoverable" 'grep -q "^BADINT_RECOVERABLE True$" <<<"$out"'

[ $fails -eq 0 ] && echo "test_migration_pg: all passed" || { echo "test_migration_pg: $fails failed"; exit 1; }
