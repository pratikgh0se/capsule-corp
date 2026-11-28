#!/usr/bin/env bash
# tests/test_cleanup.sh - cleanup.sh against a temp tree
set -euo pipefail
unset CAPSULE_JOBS_DIR   # the jobs config dir comes from each case's fake home
R="$(cd "$(dirname "$0")/.." && pwd)"; T="$(mktemp -d)"; trap 'find "$T" -delete' EXIT; pass=0; fail=0
ok(){ echo "PASS $1"; pass=$((pass+1)); }; bad(){ echo "FAIL $1"; fail=$((fail+1)); }
# M3 (row 478 b review): pin CAPSULE_HOME so a value exported by the calling
# shell (e.g. a worker whose env inherits the tick's) can never point
# cleanup.sh's Lookout log trim at a live ~/.local/share/capsule/lookout/*.log.
# Every invocation below either runs with this unset (falling back to
# cleanup.sh's $CLEANUP_HOME-relative default, itself pinned at $T) or sets
# its own explicit CAPSULE_HOME="$T/..." value.
unset CAPSULE_HOME
# t_dd689940: the test-tmp prune would otherwise sweep the real TMPDIR on every applied run below.
export CLEANUP_TMPDIR="$T/no-tmp"
mkdir -p "$T/.legacy/kanban/boards/b/logs" "$T/.legacy/profiles/p/logs" "$T/maint/logs"
echo old > "$T/.legacy/kanban/boards/b/logs/t_old.log"; touch -t 202601010000 "$T/.legacy/kanban/boards/b/logs/t_old.log"
echo new > "$T/.legacy/kanban/boards/b/logs/t_new.log"
echo old > "$T/maint/logs/nightly-2026-01-01.log"; touch -t 202601010000 "$T/maint/logs/nightly-2026-01-01.log"
head -c 60000000 /dev/zero | tr '\0' 'x' | fold -w 100 > "$T/.legacy/profiles/p/logs/agent.log"   # 60 MB live log
echo rot > "$T/.legacy/profiles/p/gateway.log.1"; touch -t 202601010000 "$T/.legacy/profiles/p/gateway.log.1"
head -c 60000000 /dev/zero | tr '\0' 'x' | fold -w 100 > "$T/maint/logs/big.log"   # 60 MB job log
mkdir -p "$T/.legacy/other"
head -c 60000000 /dev/zero | tr '\0' 'x' | fold -w 100 > "$T/.legacy/other/big.log"   # 60 MB outside the allowed truncation roots
prof_lines="$(wc -l < "$T/.legacy/profiles/p/logs/agent.log")"
before_lines="$(wc -l < "$T/.legacy/other/big.log")"
# CLEANUP_EVENTS_SPOOL is set on every invocation: without it cleanup.sh would find
# and trim the live spool at maintenance/state/events.jsonl.
NOSPOOL="$T/no-such/events.jsonl"
# CLEANUP_STATE_DIR keeps retention.json (and anything else state-y) inside the
# scratch tree - without it a real (non---dry-run) invocation would write into
# this checkout's own maintenance/state/.
STATEDIR="$T/state"
out="$(CLEANUP_HOME="$T" CLEANUP_MAINT_LOGS="$T/maint/logs" CLEANUP_NO_DOCKER=1 CLEANUP_NO_PG=1 CLEANUP_NO_EVENT=1 CLEANUP_EVENTS_SPOOL="$NOSPOOL" CLEANUP_STATE_DIR="$STATEDIR" bash "$R/maintenance/cleanup.sh" --dry-run)"
[ -f "$T/.legacy/kanban/boards/b/logs/t_old.log" ] && ok "dry-run deletes nothing" || bad "dry-run deleted"
echo "$out" | grep -q "would delete.*nightly-2026-01-01.log" && ok "dry-run lists old job log" || bad "dry-run list"
echo "$out" | grep -q "worker_logs" && bad "sizes still reports the retired worker_logs root" || ok "sizes has no worker_logs root (legacy removed)"
echo "$out" | grep -q "\.legacy" && bad "dry-run names a ~/.legacy path: $(echo "$out" | grep "\.legacy")" || ok "dry-run never names a ~/.legacy path (W313)"
CLEANUP_HOME="$T" CLEANUP_MAINT_LOGS="$T/maint/logs" CLEANUP_NO_DOCKER=1 CLEANUP_NO_PG=1 CLEANUP_NO_EVENT=1 CLEANUP_EVENTS_SPOOL="$NOSPOOL" CLEANUP_STATE_DIR="$STATEDIR" bash "$R/maintenance/cleanup.sh" >/dev/null
[ -f "$T/.legacy/kanban/boards/b/logs/t_old.log" ] && ok "old legacy worker log kept (W313)" || bad "old legacy worker log deleted"
[ -f "$T/.legacy/profiles/p/gateway.log.1" ] && ok "old legacy rotated log kept (W313)" || bad "legacy rotated log deleted"
[ -f "$T/.legacy/kanban/boards/b/logs/t_new.log" ] && ok "new worker log kept" || bad "new worker log deleted"
[ ! -f "$T/maint/logs/nightly-2026-01-01.log" ] && ok "old job log deleted" || bad "old job log kept"
[ "$(wc -l < "$T/maint/logs/big.log")" -le 10000 ] && ok "60 MB job log truncated to 10k lines" || bad "not truncated"
[ "$(wc -l < "$T/.legacy/profiles/p/logs/agent.log")" = "$prof_lines" ] && ok "60 MB legacy profile log not truncated (W313)" || bad "legacy profile log truncated"
after_lines="$(wc -l < "$T/.legacy/other/big.log")"
[ "$after_lines" = "$before_lines" ] && ok "log outside the allowed roots left untouched" || bad "log outside the allowed roots was truncated"

# --- event spool trim: keep only the bytes the shipper has not shipped yet ------
run_cleanup() {  # run_cleanup <spool> <offset> [--dry-run]
  CLEANUP_HOME="$T" CLEANUP_MAINT_LOGS="$T/maint/logs" CLEANUP_NO_DOCKER=1 CLEANUP_NO_PG=1 \
    CLEANUP_NO_EVENT=1 CLEANUP_EVENTS_SPOOL="$1" CLEANUP_EVENTS_OFFSET="$2" CLEANUP_STATE_DIR="$STATEDIR" \
    bash "$R/maintenance/cleanup.sh" ${3:-}
}
S="$T/spool/events.jsonl"; O="$T/spool/events.offset"; mkdir -p "$T/spool"
mk_spool() { printf 'shipped one\nshipped two\nfresh three\n' > "$S"; echo -n "$1" > "$O"; }

SHIPPED=$(printf 'shipped one\nshipped two\n' | wc -c | tr -d ' ')
TOTAL=$(printf 'shipped one\nshipped two\nfresh three\n' | wc -c | tr -d ' ')
KEPT=$((TOTAL - SHIPPED))

mk_spool "$SHIPPED"
out="$(run_cleanup "$S" "$O" --dry-run)"
echo "$out" | grep -q "would trim spool from $TOTAL to $KEPT bytes" && ok "dry-run reports the trim" || bad "dry-run trim line: $out"
[ "$(wc -c < "$S" | tr -d ' ')" = "$TOTAL" ] && ok "dry-run trims nothing" || bad "dry-run trimmed the spool"

mk_spool "$SHIPPED"
run_cleanup "$S" "$O" >/dev/null
[ "$(cat "$S")" = "fresh three" ] && ok "trim keeps only the unshipped bytes" || bad "trim kept: $(cat "$S")"
[ "$(cat "$O")" = "0" ] && ok "offset rewound to 0" || bad "offset is $(cat "$O")"

mk_spool "$SHIPPED"; rm -f "$O"
run_cleanup "$S" "$O" >/dev/null
[ "$(wc -c < "$S" | tr -d ' ')" = "$TOTAL" ] && ok "missing offset does not trim" || bad "trimmed without an offset"

mk_spool "$SHIPPED"; echo -n "not-a-number" > "$O"
out="$(run_cleanup "$S" "$O")"
[ "$(wc -c < "$S" | tr -d ' ')" = "$TOTAL" ] && ok "corrupt offset does not trim" || bad "trimmed on a corrupt offset"
echo "$out" | grep -q "spool not trimmed" && ok "corrupt offset says why" || bad "no reason logged"

mk_spool "$((TOTAL + 500))"
run_cleanup "$S" "$O" >/dev/null
[ "$(wc -c < "$S" | tr -d ' ')" = "$TOTAL" ] && ok "offset past the end does not trim" || bad "trimmed on an out-of-range offset"

# M9: the second stat in the live-log truncate loop (post-tail sizing) must
# never be the unguarded form; a file that vanishes between find and stat
# (e.g. a concurrent rotation) must not blow up the freed-bytes arithmetic
# under `set -u`.
grep -q 'sz=\$(filesize "\$f" 2>/dev/null || echo 0); act "truncate' "$R/maintenance/cleanup.sh" \
  && ok "truncate sz guarded against a vanished file" || bad "truncate sz unguarded"

# --- W313: nothing under ~/.legacy is ever deleted, with the retention flag unset OR 1.
# A 31-day-old session file, snapshot and a profile state.db/.env must all survive;
# retention counts no files for them.
RETDIR="$T/.legacy/profiles/ret"
mkdir -p "$RETDIR/sessions" "$RETDIR/state-snapshots"
echo old >"$RETDIR/sessions/old.json"; touch -t 202601010000 "$RETDIR/sessions/old.json"
echo old >"$RETDIR/state-snapshots/old.json"; touch -t 202601010000 "$RETDIR/state-snapshots/old.json"
echo db >"$RETDIR/state.db"; touch -t 202601010000 "$RETDIR/state.db"
echo env >"$RETDIR/.env"; touch -t 202601010000 "$RETDIR/.env"
RETSTATE="$T/retstate"
legacy_intact() { [ -f "$RETDIR/sessions/old.json" ] && [ -f "$RETDIR/state-snapshots/old.json" ] && [ -f "$RETDIR/state.db" ] && [ -f "$RETDIR/.env" ]; }

out="$(CLEANUP_HOME="$T" CLEANUP_MAINT_LOGS="$T/maint/logs" CLEANUP_NO_DOCKER=1 CLEANUP_NO_PG=1 CLEANUP_NO_EVENT=1 CLEANUP_EVENTS_SPOOL="$NOSPOOL" CLEANUP_STATE_DIR="$RETSTATE" bash "$R/maintenance/cleanup.sh")"
echo "$out" | grep -q "$RETDIR" && bad "flag unset: output names a legacy retention path" || ok "flag unset: no legacy session/snapshot path listed"
legacy_intact && ok "flag unset: legacy sessions/snapshots/state.db/.env kept" || bad "flag unset deleted a legacy file"
grep -q '"mode": "dry-run"' "$RETSTATE/retention.json" 2>/dev/null && ok "retention.json mode dry-run" || bad "retention.json mode: $(cat "$RETSTATE/retention.json" 2>/dev/null)"
grep -q '"files": 0' "$RETSTATE/retention.json" 2>/dev/null && ok "retention.json files 0" || bad "retention.json files count wrong"

CLEANUP_RETENTION_APPLY=1 CLEANUP_HOME="$T" CLEANUP_MAINT_LOGS="$T/maint/logs" CLEANUP_NO_DOCKER=1 CLEANUP_NO_PG=1 CLEANUP_NO_EVENT=1 CLEANUP_EVENTS_SPOOL="$NOSPOOL" CLEANUP_STATE_DIR="$RETSTATE" bash "$R/maintenance/cleanup.sh" >/dev/null
legacy_intact && ok "flag 1: legacy sessions/snapshots/state.db/.env kept (W313)" || bad "flag 1 deleted a legacy file"
grep -q '"mode": "applied"' "$RETSTATE/retention.json" 2>/dev/null && ok "retention.json mode applied" || bad "retention.json mode after apply: $(cat "$RETSTATE/retention.json" 2>/dev/null)"

# --- samples 30-day delete: behind OPS_RETENTION_APPLY too, same CLEANUP_NO_PG
# guard as the events delete, exercised with a fake docker on PATH so no real
# postgres is needed --------
FAKEBIN="$T/fakebin"; mkdir -p "$FAKEBIN"
ARGV_LOG="$T/docker-argv.log"
cat >"$FAKEBIN/docker" <<SH
#!/bin/sh
printf '%s\n' "\$*" >> "$ARGV_LOG"
case "\$*" in *"select count"*) echo 3 ;; esac
exit 0
SH
chmod +x "$FAKEBIN/docker"

# AGENT_OPS_DOCKER_BIN points cleanup.sh's own PATH prepend (Docker.app's CLI
# dir, added so a launchd job finds `docker`) at the fake instead - without it
# the real docker would win over $FAKEBIN and these cases would talk to the
# live capsule-corp-postgres.
out="$(PATH="$FAKEBIN:$PATH" AGENT_OPS_DOCKER_BIN="$FAKEBIN" CLEANUP_HOME="$T" CLEANUP_MAINT_LOGS="$T/maint/logs" CLEANUP_NO_DOCKER=1 CLEANUP_NO_EVENT=1 CLEANUP_EVENTS_SPOOL="$NOSPOOL" CLEANUP_STATE_DIR="$STATEDIR" bash "$R/maintenance/cleanup.sh" --dry-run)"
echo "$out" | grep -q "would delete samples older than 90 days" && ok "dry-run lists the samples delete when CLEANUP_NO_PG is unset" || bad "dry-run samples delete line missing: $out"
echo "$out" | grep -q "would apply events retention" && ok "dry-run still lists the events retention" || bad "dry-run events retention line missing"
# A full --dry-run invokes nothing at all - not even the read-only pending-rows
# count, which is why that count prints as "?" in this mode.
[ ! -s "$ARGV_LOG" ] && ok "dry-run never invokes docker" || bad "dry-run invoked docker: $(cat "$ARGV_LOG" 2>/dev/null)"
echo "$out" | grep -q "^retention: dry-run, .* / ? rows would go" && ok "dry-run reports the row count as ?" || bad "dry-run retention message: $out"

rm -f "$ARGV_LOG"
# Real run, retention flag unset: events (unflagged) still delete for real;
# samples (now flagged) stay "would" and are not touched.
out="$(PATH="$FAKEBIN:$PATH" AGENT_OPS_DOCKER_BIN="$FAKEBIN" CLEANUP_HOME="$T" CLEANUP_MAINT_LOGS="$T/maint/logs" CLEANUP_NO_DOCKER=1 CLEANUP_NO_EVENT=1 CLEANUP_EVENTS_SPOOL="$NOSPOOL" CLEANUP_STATE_DIR="$STATEDIR" bash "$R/maintenance/cleanup.sh")"
grep -q -- "-i capsule-corp-postgres psql -U ops -d ops -q -v ON_ERROR_STOP=1 -f -" "$ARGV_LOG" \
  && ok "real run still invokes docker exec psql to apply events retention" || bad "events retention docker call missing"
! grep -q "delete from samples" "$ARGV_LOG" 2>/dev/null && ok "real run, flag unset: samples not deleted" || bad "samples deleted with retention flag unset"
echo "$out" | grep -q "would delete samples older than 90 days" && ok "real run, flag unset: samples line still says would" || bad "samples would-line missing on a real run with the flag unset"
echo "$out" | grep -q "^retention: dry-run, .* / 3 rows would go" && ok "retention message reports the pending row count" || bad "retention message: $out"

rm -f "$ARGV_LOG"
# Real run, retention flag 1: samples delete runs for real.
out="$(PATH="$FAKEBIN:$PATH" AGENT_OPS_DOCKER_BIN="$FAKEBIN" CLEANUP_RETENTION_APPLY=1 CLEANUP_HOME="$T" CLEANUP_MAINT_LOGS="$T/maint/logs" CLEANUP_NO_DOCKER=1 CLEANUP_NO_EVENT=1 CLEANUP_EVENTS_SPOOL="$NOSPOOL" CLEANUP_STATE_DIR="$STATEDIR" bash "$R/maintenance/cleanup.sh")"
grep -q "exec capsule-corp-postgres psql -U ops -d ops -qc delete from samples where ts < now() - interval '90 days'" "$ARGV_LOG" \
  && ok "flag 1: real run invokes docker exec psql to delete old samples" || bad "samples delete docker call missing: $(cat "$ARGV_LOG" 2>/dev/null)"
grep -q "exec capsule-corp-postgres psql -U ops -d ops -qc delete from samples_hourly where bucket < now() - interval '730 days'" "$ARGV_LOG" \
  && ok "flag 1: real run deletes samples_hourly older than 730 days" || bad "samples_hourly delete docker call missing: $(cat "$ARGV_LOG" 2>/dev/null)"
echo "$out" | grep -q "^retention: applied, " && ok "flag 1: retention message says applied" || bad "retention message not applied: $out"

# --- G10 / M269 (decision row 140): two stores are never deleted by this job,
# and the refusal is printed every run. The sweep roots are the job logs, so the
# invoices fixture is an old .log inside a swept root: del_old() would take it.
G="$T/maint/logs/invoices"; mkdir -p "$G"
echo inv > "$G/inv-2026-01.log"; touch -t 202601010000 "$G/inv-2026-01.log"
echo ord > "$T/maint/logs/ordinary-2026-01.log"; touch -t 202601010000 "$T/maint/logs/ordinary-2026-01.log"

stat_run() {  # stat_run [--dry-run]
  CLEANUP_HOME="$T" CLEANUP_MAINT_LOGS="$T/maint/logs" CLEANUP_NO_DOCKER=1 CLEANUP_NO_PG=1 \
    CLEANUP_NO_EVENT=1 CLEANUP_EVENTS_SPOOL="$NOSPOOL" CLEANUP_STATE_DIR="$STATEDIR" \
    CLEANUP_RETENTION_APPLY=1 bash "$R/maintenance/cleanup.sh" ${1:-}
}

dry="$(stat_run --dry-run)"
echo "$dry" | grep -q "^statutory: never deleting" && ok "dry-run says it refuses" || bad "dry-run silent about the refusal"
echo "$dry" | grep -q "would.*statutory" && bad "the refusal is not a pending action" || ok "no 'would' on the refusal line"
echo "$dry" | grep -q "s.87/3" && ok "dry-run cites the statute" || bad "no statute cited"

real="$(stat_run)"
[ -f "$G/inv-2026-01.log" ] && ok "statutory .log survives del_old" || bad "statutory .log deleted"
[ ! -f "$T/maint/logs/ordinary-2026-01.log" ] && ok "an ordinary old job log is still deleted" || bad "job log sweep stopped working"
echo "$real" | grep -q "statutory: never deleting.*1 paths skipped this run" && ok "the skipped count is right" || bad "wrong skipped count: $(echo "$real" | grep statutory)"

# The guard has no off switch: an explicit override list still protects the
# built-in names, because a guard with a flag is not a guard.
over="$(CLEANUP_HOME="$T" CLEANUP_MAINT_LOGS="$T/maint/logs" CLEANUP_NO_DOCKER=1 CLEANUP_NO_PG=1 \
  CLEANUP_NO_EVENT=1 CLEANUP_EVENTS_SPOOL="$NOSPOOL" CLEANUP_STATE_DIR="$STATEDIR" \
  CLEANUP_RETENTION_APPLY=1 CLEANUP_STATUTORY_DIRS="" bash "$R/maintenance/cleanup.sh" --dry-run)"
echo "$over" | grep -q "^statutory: never deleting" && ok "an empty override still prints the refusal" || bad "override silenced the guard"

# --- fix round 1: case-insensitive, symlink-real-path-resolved matching, and a
# fail-closed refusal when a path cannot be resolved. find(1) here never follows
# a directory symlink or -type f's a symlink itself (no -L anywhere in cleanup.sh),
# so a full sweep through del_old can't exercise the bypass these
# guard against - call is_statutory() directly against the extracted guard block
# instead, the same function-extraction pattern tests/test_status.sh etc. use.
GUARD_SRC="$(sed -n '/^resolve_path()/,/^skip_statutory/p' "$R/maintenance/cleanup.sh")"

mkdir -p "$T/statreal/invoices" "$T/statlink"
echo x > "$T/statreal/invoices/r.json"
ln -s "$T/statreal/invoices" "$T/statlink/aliasdir"

mc="$(
  CLEANUP_STATUTORY_DIRS=""
  eval "$GUARD_SRC"
  is_statutory "$T/somewhere/Invoices/x.json" && echo MIXEDCASE_OK
  true
)"
ud="$(
  CLEANUP_STATUTORY_DIRS=""
  eval "$GUARD_SRC"
  is_statutory "$T/v/Focus Log.md" && echo FOCUS_OK
  is_statutory "$T/v/Todo.md" && echo TODO_OK
  true
)"
echo "$ud" | grep -q FOCUS_OK && echo "$ud" | grep -q TODO_OK && ok "app user data names (Focus Log.md, Todo.md) match the guard" || bad "app user data names bypass the guard"
echo "$mc" | grep -q MIXEDCASE_OK && ok "a mixed-case statutory dir name still matches" || bad "mixed-case bypasses the guard"

sl="$(
  CLEANUP_STATUTORY_DIRS=""
  eval "$GUARD_SRC"
  is_statutory "$T/statlink/aliasdir/r.json" && echo SYMLINK_OK
  true
)"
echo "$sl" | grep -q SYMLINK_OK && ok "a symlink resolving into a statutory dir still matches" || bad "a symlink whose target is statutory bypasses the guard"

FAKEPY="$T/fakepy"; mkdir -p "$FAKEPY"
printf '#!/bin/sh\nexit 1\n' > "$FAKEPY/python3"; chmod +x "$FAKEPY/python3"
fcout="$(
  PATH="$FAKEPY:$PATH"
  CLEANUP_STATUTORY_DIRS=""
  eval "$GUARD_SRC"
  is_statutory "$T/ordinary/not-statutory-looking.txt" && echo FAILCLOSED_OK
  true
)"
echo "$fcout" | grep -q FAILCLOSED_OK && ok "a path that fails to resolve is refused (fail closed)" || bad "an unresolvable path fell through to deletion"

# --- Lookout retention (spec 5.11): cleanup.sh pipes maintenance/retention.sql into psql.
# M4 (row 478 b review): its own fakebin dir, never the $FAKEBIN the samples-delete
# block above already populated. I1: </dev/null on every cleanup.sh call below -
# the fake docker's `cat >> "$CAP"` also answers cleanup.sh's read-only
# `select count(*) from samples` call, which otherwise inherits this script's
# stdin and hangs (or silently reads nothing) whenever stdin is not already closed.
FBRET="$T/fakebin-ret"; mkdir -p "$FBRET"; CAP="$T/psql-in.sql"
printf '#!/bin/sh\ncat >> "%s"\necho 0\n' "$CAP" > "$FBRET/docker"; chmod +x "$FBRET/docker"
PATH="$FBRET:$PATH" AGENT_OPS_DOCKER_BIN="$FBRET" CLEANUP_HOME="$T" CLEANUP_MAINT_LOGS="$T/maint/logs" CLEANUP_NO_DOCKER=1 CLEANUP_NO_EVENT=1 \
  CLEANUP_EVENTS_SPOOL="$NOSPOOL" CLEANUP_STATE_DIR="$STATEDIR" bash "$R/maintenance/cleanup.sh" </dev/null >/dev/null 2>&1
grep -q "interval '180 days'" "$CAP" && grep -q "interval '14 days'" "$CAP" && ok "retention.sql piped to psql" || bad "retention.sql not piped"

# --- Lookout log trim (M2): both ship.log and publish.log over 5 MiB trimmed to
# 10k lines; a --dry-run leaves both alone.
LK="$T/capsule/lookout"; mkdir -p "$LK"
mk_big_log() { head -c 6000000 /dev/zero | tr '\0' 'x' | fold -w 100 > "$1"; }
mk_big_log "$LK/publish.log"; mk_big_log "$LK/ship.log"
before_publish_lines="$(wc -l < "$LK/publish.log")"; before_ship_lines="$(wc -l < "$LK/ship.log")"

CAPSULE_HOME="$T/capsule" CLEANUP_HOME="$T" CLEANUP_MAINT_LOGS="$T/maint/logs" CLEANUP_NO_DOCKER=1 CLEANUP_NO_PG=1 \
  CLEANUP_NO_EVENT=1 CLEANUP_EVENTS_SPOOL="$NOSPOOL" CLEANUP_STATE_DIR="$STATEDIR" bash "$R/maintenance/cleanup.sh" --dry-run </dev/null >/dev/null 2>&1
[ "$(wc -l < "$LK/publish.log")" = "$before_publish_lines" ] && ok "dry-run leaves publish.log alone" || bad "dry-run trimmed publish.log"
[ "$(wc -l < "$LK/ship.log")" = "$before_ship_lines" ] && ok "dry-run leaves ship.log alone" || bad "dry-run trimmed ship.log"

CAPSULE_HOME="$T/capsule" CLEANUP_HOME="$T" CLEANUP_MAINT_LOGS="$T/maint/logs" CLEANUP_NO_DOCKER=1 CLEANUP_NO_PG=1 \
  CLEANUP_NO_EVENT=1 CLEANUP_EVENTS_SPOOL="$NOSPOOL" CLEANUP_STATE_DIR="$STATEDIR" bash "$R/maintenance/cleanup.sh" </dev/null >/dev/null 2>&1
[ "$(wc -l < "$LK/publish.log")" -le 10000 ] && ok "lookout publish.log trimmed" || bad "lookout publish.log not trimmed"
[ "$(wc -l < "$LK/ship.log")" -le 10000 ] && ok "lookout ship.log trimmed" || bad "lookout ship.log not trimmed"

# --- worker transcript retention (retention): compress when old AND week recorded ---
CH="$T/ch"; PJ="$T/proj/-slug"; AR="$T/arch"; mkdir -p "$PJ" "$CH/boards/b/spool"
python3 - "$CH/boards/b" <<'PY'
import json, sqlite3, sys, os
b = sys.argv[1]
db = sqlite3.connect(b + "/kanban.db")
db.execute("create table task_runs(run_id text, started_at integer)")
db.execute("create table task_events(kind text, payload text)")
for rid, sid, t in (("r_old", "s-old", 1000), ("r_unrec", "s-unrec", 9000), ("r_young", "s-young", 1000)):
    db.execute("insert into task_runs values(?,?)", (rid, t))
    os.makedirs(f"{b}/spool/{rid}"); json.dump({"session_id": sid}, open(f"{b}/spool/{rid}/run.json", "w"))
db.execute("insert into task_events values('eval_check', ?)",
           (json.dumps({"metric": "fresh_tokens", "window": [0, 5000]}),))
db.commit()
PY
for n in s-old s-unrec s-young s-chat; do { echo "line $n"; head -c 200000 /dev/zero | tr '\0' 'x'; } > "$PJ/$n.jsonl"; done
for n in s-old s-unrec s-chat; do touch -t 202601010000 "$PJ/$n.jsonl"; done
run_tr() {  # run_tr [extra env...] -- args
  env CAPSULE_HOME="$CH" CAPSULE_CLAUDE_PROJECTS_DIR="$T/proj" CLEANUP_TRANSCRIPT_ARCHIVE="$AR" \
    CLEANUP_HOME="$T" CLEANUP_MAINT_LOGS="$T/maint/logs" CLEANUP_NO_DOCKER=1 CLEANUP_NO_PG=1 \
    CLEANUP_NO_EVENT=1 CLEANUP_EVENTS_SPOOL="$NOSPOOL" CLEANUP_STATE_DIR="$STATEDIR" "$@" </dev/null
}
out="$(run_tr CLEANUP_RETENTION_APPLY=1 bash "$R/maintenance/cleanup.sh" --dry-run)"
echo "$out" | grep -q "would compress $PJ/s-old.jsonl" && ok "dry-run lists old recorded transcript" || bad "tr dry list: $out"
[ -f "$PJ/s-old.jsonl" ] && [ ! -d "$AR" ] && ok "dry-run compresses nothing" || bad "dry-run changed transcripts"
out="$(run_tr bash "$R/maintenance/cleanup.sh")"
[ -f "$PJ/s-old.jsonl" ] && ok "flag off: transcript kept" || bad "flag off removed transcript"
echo "$out" | grep -q "would compress" && ok "flag off: prints would compress" || bad "flag off print"
run_tr CLEANUP_RETENTION_APPLY=1 bash "$R/maintenance/cleanup.sh" >/dev/null
[ ! -f "$PJ/s-old.jsonl" ] && [ -s "$AR/-slug/s-old.jsonl.zst" -o -s "$AR/-slug/s-old.jsonl.xz" ] \
  && ok "old+recorded transcript archived, original removed" || bad "old+recorded not archived"
A="$(ls "$AR"/-slug/s-old.jsonl.* | head -1)"
{ case "$A" in *.zst) zstd -dc "$A" ;; *) xz -dc "$A" ;; esac; } | grep -c "line s-old" >/dev/null \
  && ok "archive decompresses" || bad "archive unreadable"
[ -f "$PJ/s-unrec.jsonl" ] && ok "unrecorded week kept" || bad "unrecorded week removed"
[ -f "$PJ/s-young.jsonl" ] && ok "young transcript kept" || bad "young transcript removed"
[ -f "$PJ/s-chat.jsonl" ] && ok "non-worker transcript untouched" || bad "non-worker transcript removed"
touch -t 202601010000 "$A"
run_tr CLEANUP_RETENTION_APPLY=1 bash "$R/maintenance/cleanup.sh" >/dev/null
[ ! -f "$A" ] && ok "archive older than 30 d deleted" || bad "expired archive kept"
# corrupt archive: a compressor that exits 0 but writes garbage must keep the original
FZ="$T/fakez"; mkdir -p "$FZ"
printf '#!/bin/sh\ncase "$*" in *-d*) cat;; *) echo garbage;; esac\n' > "$FZ/zstd"; chmod +x "$FZ/zstd"
run_tr CLEANUP_RETENTION_APPLY=1 RETENTION_ZSTD="$FZ/zstd" bash "$R/maintenance/cleanup.sh" >/dev/null
[ -f "$PJ/s-unrec.jsonl" ] || bad "setup"
python3 - "$CH/boards/b/kanban.db" <<'PY'
import json, sqlite3, sys
db = sqlite3.connect(sys.argv[1]); db.execute("insert into task_events values('eval_check', ?)",
    (json.dumps({"metric": "fresh_tokens", "window": [5000, 10000]}),)); db.commit()
PY
run_tr CLEANUP_RETENTION_APPLY=1 RETENTION_ZSTD="$FZ/zstd" bash "$R/maintenance/cleanup.sh" >/dev/null
[ -f "$PJ/s-unrec.jsonl" ] && [ ! -e "$AR/-slug/s-unrec.jsonl.zst" ] \
  && ok "corrupt archive keeps the original" || bad "corrupt archive lost the original"

# --- transcript edge cases: window end, mixed runs, crafted ids, symlink, expiry scope ---
CH="$T/ch2"; PJ="$T/proj2/-slug"; AR="$T/arch2"; mkdir -p "$PJ" "$CH/boards/b/spool" "$T/proj2/-other"
python3 - "$CH/boards/b" <<'PY'
import json, sqlite3, sys, os
b = sys.argv[1]
db = sqlite3.connect(b + "/kanban.db")
db.execute("create table task_runs(run_id text, started_at integer)")
db.execute("create table task_events(kind text, payload text)")
for rid, sid, t in (("r1", "s-ok", 1000), ("r2", "s-mix", 1000), ("r3", "s-mix", 9000),
                    ("r4", "s-edge", 5000), ("r5", "*", 1000), ("r6", "../x", 1000),
                    ("r7", "s-link", 1000)):
    db.execute("insert into task_runs values(?,?)", (rid, t))
    os.makedirs(f"{b}/spool/{rid}"); json.dump({"session_id": sid}, open(f"{b}/spool/{rid}/run.json", "w"))
db.execute("insert into task_events values('eval_check', ?)",
           (json.dumps({"metric": "fresh_tokens", "window": [0, 5000]}),))
db.commit()
PY
for n in s-ok s-mix s-edge s-chat; do echo "x $n" > "$PJ/$n.jsonl"; touch -t 202601010000 "$PJ/$n.jsonl"; done
echo real > "$T/real.jsonl"; touch -t 202601010000 "$T/real.jsonl"; ln -s "$T/real.jsonl" "$PJ/s-link.jsonl"
echo x > "$T/proj2/x.jsonl"; touch -t 202601010000 "$T/proj2/x.jsonl"   # what ../x would reach
echo x > "$T/proj2/-other/y.jsonl"; touch -t 202601010000 "$T/proj2/-other/y.jsonl"   # what * would reach
mkdir -p "$AR/-slug" "$AR/deep/n"
echo y > "$AR/-slug/young.jsonl.zst"; echo y > "$AR/-slug/old.jsonl.zst"; echo y > "$AR/-slug/notes.txt"
echo y > "$AR/deep/n/z.jsonl.zst"; echo y > "$AR/-slug/old.zst"
touch -t 202601010000 "$AR/-slug/old.jsonl.zst" "$AR/-slug/notes.txt" "$AR/deep/n/z.jsonl.zst" "$AR/-slug/old.zst"
run_tr CLEANUP_RETENTION_APPLY=1 CAPSULE_CLAUDE_PROJECTS_DIR="$T/proj2" bash "$R/maintenance/cleanup.sh" >/dev/null
[ ! -f "$PJ/s-ok.jsonl" ] && ls "$AR"/-slug/s-ok.jsonl.* >/dev/null 2>&1 \
  && ok "edge fixture: recorded worker transcript archived" || bad "edge fixture: s-ok not archived"
[ -f "$PJ/s-mix.jsonl" ] && ok "one recorded and one unrecorded run: kept" || bad "mixed-run session archived"
[ -f "$PJ/s-edge.jsonl" ] && ok "run starting at the window end: kept" || bad "window-end run archived"
[ -f "$T/proj2/x.jsonl" ] && [ -f "$T/proj2/-other/y.jsonl" ] && [ -f "$PJ/s-chat.jsonl" ] \
  && ok "crafted session ids (../x, *) reach nothing" || bad "crafted session id deleted a file"
[ -L "$PJ/s-link.jsonl" ] && [ -f "$T/real.jsonl" ] && ok "symlinked transcript kept" || bad "symlink archived"
[ ! -e "$AR/-slug/old.jsonl.zst" ] && [ -f "$AR/-slug/young.jsonl.zst" ] && [ -f "$AR/-slug/notes.txt" ] \
  && [ -f "$AR/deep/n/z.jsonl.zst" ] && [ -f "$AR/-slug/old.zst" ] \
  && ok "expiry removes only old <slug>/<sid>.jsonl.zst" || bad "expiry scope wrong"

# --- docker image keep-list with a fake docker ------------------------------------
FD="$T/fakedocker"; mkdir -p "$FD"; DL="$T/docker-calls.log"; : > "$DL"
cat > "$FD/docker" <<SH
#!/bin/sh
echo "\$*" >> "$DL"
case "\$1 \$2" in
  "image ls") case "\$*" in
    *label=capsule-layer*) echo ppp ;;   # the only labelled image: an old capsule-web tag
    *) printf '%s\n' 'aaa|postgres|16-alpine|<none>' 'bbb|capsule-corp-worker|2026-10-20-2c73797|<none>' \
    'ccc|infisical/infisical|<none>|sha256:0208' 'ddd|busybox|1|<none>' 'eee|nginx|old|<none>' \
    'fff|redis|7|<none>' 'ggg|<none>|<none>|<none>' 'hhh|nginx|keepme|<none>' \
    'iii|capsule-corp-worker|older|<none>' 'jjj|busybox|pinned|<none>' 'kkk|caddy|2|<none>' \
    'lll|capsule-proj-x|local|<none>' 'mmm|capsule-python|local|<none>' \
    'nnn|capsule-base|local|<none>' 'ooo|capsule-python|old|<none>' 'ppp|capsule-web|old|<none>' \
    'qqq|debian|trixie-20261001|<none>' 'rrr|node|26.12.1-trixie-slim|<none>' 'sss|debian|trixie-old|<none>' ;;
    esac ;;
  "ps -a") echo redis:7 ;;
esac
SH
chmod +x "$FD/docker"
mkdir -p "$T/repo/maintenance" "$T/repo/observability" "$T/repo/docker/base" "$T/repo/docker/stacks/web" "$T/none"
printf 'FROM debian:trixie-20261001@sha256:%s\nRUN true\n' "$(printf 'd%.0s' $(seq 64))" > "$T/repo/docker/base/Dockerfile"
printf 'FROM node:26.12.1-trixie-slim AS node\nFROM --platform=linux/arm64 capsule-base@sha256:%s\n' "$(printf 'a%.0s' $(seq 64))" > "$T/repo/docker/stacks/web/Dockerfile"
printf 'capsule-proj-x@sha256:%s\n' "$(printf 'c%.0s' $(seq 64))" > "$T/none/images.lock"   # the record
cp "$R/maintenance/cleanup.sh" "$R/maintenance/lib_state.sh" "$R/maintenance/lib_config.sh" "$R/maintenance/retention_transcripts.py" "$T/repo/maintenance/"
printf 'services:\n  a:\n    image: postgres:16-alpine\n  b:\n    image: infisical/infisical@sha256:0208   # pin\n  c:\n    image: docker.io/library/nginx:keepme@sha256:ff\n  d:\n    image: caddy:2@sha256:ee\n' > "$T/repo/observability/docker-compose.yml"
printf 'capsule-base@sha256:%s\ncapsule-python@sha256:%s\n' "$(printf 'a%.0s' $(seq 64))" "$(printf 'b%.0s' $(seq 64))" > "$T/repo/docker/IMAGES.lock"
run_dk() {
  env PATH="$FD:$PATH" AGENT_OPS_DOCKER_BIN="$FD" OPS_ENV_FILE="${OPSENV:-$T/no-ops.env}" CLEANUP_HOME="$T" CLEANUP_MAINT_LOGS="$T/maint/logs" CLEANUP_NO_PG=1 \
    CLEANUP_NO_EVENT=1 CLEANUP_EVENTS_SPOOL="$NOSPOOL" CLEANUP_STATE_DIR="$STATEDIR" CAPSULE_HOME="$T/none" \
    CAPSULE_CLAUDE_PROJECTS_DIR="$T/none" "$@" bash "$T/repo/maintenance/cleanup.sh" ${DKARG:-} </dev/null
}
out="$(run_dk CLEANUP_RETENTION_APPLY=1)"
echo "$out" | grep -q "docker image rm busybox:1 (ddd)" && echo "$out" | grep -q "docker image rm nginx:old (eee)" \
  && ok "unused images removed" || bad "unused images: $out"
grep -q "image rm capsule-corp-worker:older" "$DL" && ok "retired worker tags are no longer kept" || bad "old worker tag spared"
grep -q "image rm busybox:pinned" "$DL" && ok "ops.env pin ignored when absent" || bad "busybox spared wrongly"
! grep -q "image rm nginx:keepme\|image rm caddy\|image rm postgres\|image rm infisical\|image rm redis\|image rm <none>" "$DL" \
  && ok "keep-list (compose, worker pin, digest pin, container image, dangling) spared" || bad "keep-list breached: $(cat "$DL")"
! grep -q "image rm capsule-python:local\|image rm capsule-base:local" "$DL" && grep -q "image rm capsule-python:old" "$DL" \
  && ok "IMAGES.lock images (local-only) spared, an unlisted capsule-python tag still goes" || bad "lock images: $(cat "$DL")"
! grep -q "image rm debian:trixie-20261001\|image rm node:26.12.1-trixie-slim" "$DL" && grep -q "image rm debian:trixie-old" "$DL" \
  && ok "FROM refs of docker/base and docker/stacks/* are spared, another debian tag still goes" || bad "FROM bases: $(cat "$DL")"
! grep -q "image rm capsule-proj-x" "$DL" && ok "image record rows (capsule-proj-x) spared" || bad "record row removed: $(cat "$DL")"
grep -q "^image prune -f --filter label!=capsule-layer$" "$DL" && ! grep -q "^image prune -f$" "$DL" \
  && ok "dangling prune kept, but only for images without the capsule-layer label" || bad "dangling prune: $(cat "$DL")"
! grep -q "image rm capsule-web:old" "$DL" && ! echo "$out" | grep -q "capsule-web:old" \
  && ok "a capsule-layer labelled image is never listed for removal (image_gc.py owns it)" || bad "labelled image breached: $(cat "$DL")"
: > "$DL"; DKARG=--dry-run run_dk CLEANUP_RETENTION_APPLY=1 >/dev/null
! grep -q "image prune\|image rm" "$DL" && ok "dry-run still runs no prune and no rm" || bad "dry-run mutated: $(cat "$DL")"

: > "$DL"
out="$(run_dk)"
! grep -q "image rm" "$DL" && echo "$out" | grep -q "would docker image rm busybox:1" \
  && ok "flag off: image removal only printed" || bad "flag off image removal ran: $(cat "$DL")"
printf 'OPS_WORKER_IMAGE="busybox:pinned"\n' > "$T/ops.env"; : > "$DL"
OPSENV="$T/ops.env" run_dk CLEANUP_RETENTION_APPLY=1 >/dev/null
! grep -q "image rm busybox:pinned" "$DL" && ok "OPS_ENV_FILE pin honoured" || bad "ops.env pin ignored"
: > "$DL"; DKARG=--dry-run; run_dk CLEANUP_RETENTION_APPLY=1 >/dev/null; DKARG=
! grep -q "image rm\|prune" "$DL" && ok "full --dry-run with the flag on never removes or prunes" || bad "dry-run mutated docker: $(cat "$DL")"

# --- test tmp dirs (t_dd689940): old suite-pattern dirs in TMPDIR go, nothing else ---
TD="$T/fake-tmp"; mkdir -p "$TD" "$T/outside"
mkold() { mkdir -p "$TD/$1"; echo x > "$TD/$1/f"; touch -t 202601010000 "$TD/$1/f" "$TD/$1"; }
mkold tmp.AbCdEfGhIj; mkold tmpab12cd34; mkold capsule-suite-xyz; mkold mutation-check-weaken-q; mkold lander-box-abc123
mkold unrelated-old; mkold tmpTOOLONGNAME; mkdir -p "$TD/tmp.NewNewNew1"
mkdir -p "$TD/tmpro123456/sub"; touch -t 202601010000 "$TD/tmpro123456/sub/f" "$TD/tmpro123456/sub"; chmod 500 "$TD/tmpro123456/sub"; touch -t 202601010000 "$TD/tmpro123456"
mkdir -p "$T/outside/keep"; echo y > "$T/outside/keep/f"; ln -s "$T/outside/keep" "$TD/tmp.symlink01"
touch -h -t 202601010000 "$TD/tmp.symlink01"
mkold tmp.LiveDirXY1; echo fresh > "$TD/tmp.LiveDirXY1/f"    # old top mtime, a file rewritten just now
mkold tmpm0123456; chmod 000 "$TD/tmpm0123456"; touch -t 202601010000 "$TD/tmpm0123456"
tmp_run() { CLEANUP_TMPDIR="$TD" CLEANUP_HOME="$T" CLEANUP_MAINT_LOGS="$T/maint/logs" CLEANUP_NO_DOCKER=1 CLEANUP_NO_PG=1 \
  CLEANUP_NO_EVENT=1 CLEANUP_EVENTS_SPOOL="$NOSPOOL" CLEANUP_STATE_DIR="$STATEDIR" "$@" bash "$R/maintenance/cleanup.sh" ${TMPARG:-}; }
out="$(TMPARG=--dry-run tmp_run env CLEANUP_RETENTION_APPLY=1)"
[ -d "$TD/tmp.AbCdEfGhIj" ] && echo "$out" | grep -q "would delete test tmp dir .*tmp.AbCdEfGhIj" \
  && ok "test tmp: --dry-run lists old dirs, removes none" || bad "test tmp dry-run: $out"
out="$(tmp_run env CLEANUP_RETENTION_APPLY=0)"
[ -d "$TD/tmp.AbCdEfGhIj" ] && echo "$out" | grep -q "would delete test tmp dir" \
  && ok "test tmp: flag off only prints" || bad "test tmp flag off removed or silent"
tmp_run env CLEANUP_RETENTION_APPLY=1 >/dev/null
gone=1; for n in tmp.AbCdEfGhIj tmpab12cd34 capsule-suite-xyz mutation-check-weaken-q lander-box-abc123 tmpro123456; do [ -e "$TD/$n" ] && gone=0; done
[ "$gone" = 1 ] && ok "test tmp: old suite-pattern dirs removed (incl. read-only tree)" || bad "test tmp: old dirs left"
[ -d "$TD/tmp.NewNewNew1" ] && ok "test tmp: dir under 1 day kept" || bad "test tmp: new dir removed"
[ -d "$TD/unrelated-old" ] && [ -d "$TD/tmpTOOLONGNAME" ] && ok "test tmp: non-matching names kept" || bad "test tmp: non-matching removed"
[ -f "$T/outside/keep/f" ] && [ -L "$TD/tmp.symlink01" ] && ok "test tmp: symlink and its target untouched" || bad "test tmp: followed a symlink"
[ -f "$TD/tmp.LiveDirXY1/f" ] && ok "test tmp: old dir holding a fresh file is kept" || bad "test tmp: live dir removed"
[ ! -e "$TD/tmpm0123456" ] && ok "test tmp: mode-000 top dir removed" || bad "test tmp: mode-000 dir left"

echo "test_cleanup: $pass passed, $fail failed"; [ "$fail" = 0 ]
