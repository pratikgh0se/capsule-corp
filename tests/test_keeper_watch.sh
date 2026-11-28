#!/usr/bin/env bash
# Oracle 2 for the keeper watchdog (t_1d641f6e): bin/capsule-keeper-watch.sh over a fake CAPSULE_HOME,
# a fake board with ready cards, a fake keeper log. Never loads launchd, never sends.
set -uo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
fail=0; PASSED=""; pass() { echo "PASS $1"; PASSED="$PASSED|$1"; }; bad() { echo "FAIL $1"; fail=1; }
T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
export CAPSULE_HOME="$T/home" AGENT_OPS_NO_SEND=1 CAPSULE_KEEPER_LOG="$T/keeper-log.md"
export CAPSULE_KEEPER_BOARD=cc LEGACY_KANBAN_HOME="$T/no-legacy-home"
DB="$CAPSULE_HOME/boards/cc/kanban.db"; STATE="$CAPSULE_HOME/keeper-watch.state"
mkdir -p "$CAPSULE_HOME/boards/cc"; echo "{}" > "$CAPSULE_HOME/boards/cc/config.json"
echo '{"unblocked": {"boards": ["cc"], "repos": [], "runs": []}}' > "$CAPSULE_HOME/config.json"
python3 -c 'import sys; sys.path.insert(0, sys.argv[1]); from capsule import db
db.create_board(sys.argv[2]); c = db.connect(sys.argv[2])
[db.new_task(c, title=f"card {n}", status="ready", body="work") for n in range(3)]; c.close()' "$REPO" "$DB"
echo "last keeper line: limit until 14:00" > "$CAPSULE_KEEPER_LOG"
rows() { python3 -c 'import sqlite3,sys; print(sqlite3.connect(sys.argv[1]).execute("select count(*) from outbox").fetchone()[0])' "$DB"; }
calls() { python3 -c 'import sqlite3,sys; print(sqlite3.connect(sys.argv[1]).execute("select count(*) from outbox where channel=\"file\"").fetchone()[0])' "$DB"; }
bodies() { python3 -c 'import sqlite3,sys; print("\n".join(r[0] for r in sqlite3.connect(sys.argv[1]).execute("select body from outbox where channel=\"file\" order by id")))' "$DB"; }
age() { touch -t "$1" "$CAPSULE_KEEPER_LOG"; }
watch() { bash "$REPO/bin/capsule-keeper-watch.sh" >/dev/null 2>&1; }
NOW_MIN="$(date -v-20M +%Y%m%d%H%M)"; OLD75="$(date -v-75M +%Y%m%d%H%M)"; OLD10H="$(date -v-10H +%Y%m%d%H%M)"

age "$NOW_MIN"; watch
[ "$(rows)" = 0 ] && [ "$(calls)" = 0 ] && [ ! -f "$STATE" ] && pass fresh-log-no-page || bad fresh-log-no-page

age "$OLD75"; watch
[ "$(rows)" -ge 1 ] && [ "$(calls)" = 1 ] && [ -f "$STATE" ] && pass silent-log-pages-once || bad "silent-log-pages-once rows=$(rows) calls=$(calls)"
R1="$(rows)"; bodies | grep -q "limit until 14:00" && pass page-carries-last-line || bad page-carries-last-line

watch
[ "$(rows)" = "$R1" ] && [ "$(calls)" = 1 ] && pass same-episode-no-second-page || bad same-episode-no-second-page

# Declared pause: bin/keeper-pause 3h silences a fresh episode; an expired file pages again.
PAUSE="$CAPSULE_HOME/keeper-pause.json"; C0="$(calls)"; rm -f "$STATE"
bash "$REPO/bin/keeper-pause" 3h "test pause" >/dev/null && watch
[ "$(calls)" = "$C0" ] && [ ! -f "$STATE" ] && pass declared-pause-quiet || bad "declared-pause-quiet calls=$(calls)"
echo '{"until": "2000-01-01T00:00:00+00:00", "reason": "stale"}' > "$PAUSE"; watch
[ "$(calls)" = "$((C0 + 1))" ] && pass expired-pause-pages || bad "expired-pause-pages calls=$(calls)"
C1="$(calls)"; rm -f "$STATE"
echo '{"until": "2099-01-01T00:00:00+00:00", "reason": "forgot"}' > "$PAUSE"; touch -t "$(date -v-13H +%Y%m%d%H%M)" "$PAUSE"; watch
[ "$(calls)" = "$((C1 + 1))" ] && pass pause-cap-binds-file-mtime || bad "pause-cap-binds-file-mtime calls=$(calls)"
C2="$(calls)"; rm -f "$STATE" "$PAUSE"; bash "$REPO/bin/keeper-pause" 3h "target" >/dev/null && mv "$PAUSE" "$T/pause-target.json" && ln -s "$T/pause-target.json" "$PAUSE"; watch
[ "$(calls)" = "$((C2 + 1))" ] && pass symlinked-pause-pages || bad "symlinked-pause-pages calls=$(calls)"
rm -f "$PAUSE" "$T/pause-target.json"
# F3: a 10 h old log on wake pages once; the next firing does not escalate, one 4 h after the page does.
C3="$(calls)"; rm -f "$STATE"; age "$OLD10H"; watch; watch
[ "$(calls)" = "$((C3 + 1))" ] && ! bodies | tail -n 1 | grep -q escalation && pass wake-pages-once-no-quick-escalation || bad "wake-pages-once-no-quick-escalation calls=$(calls)"
# F2: move paged_at back 5 h (same mtime), so the escalate branch itself is taken.
python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); d["paged_at"]-=5*3600; json.dump(d, open(sys.argv[1],"w"))' "$STATE"
watch
[ "$(calls)" = "$((C3 + 2))" ] && bodies | tail -n 1 | grep -q escalation && pass four-hours-after-page-escalates || bad "four-hours-after-page-escalates calls=$(calls)"
watch
[ "$(calls)" = "$((C3 + 2))" ] && pass escalates-only-once || bad "escalates-only-once calls=$(calls)"

python3 -c 'import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); c.execute("update tasks set status=\"done\""); c.commit()' "$DB"
rm -f "$STATE"; age "$OLD75"; watch
[ "$(calls)" = "$((C3 + 2))" ] && [ ! -f "$STATE" ] && pass nothing-waiting-no-page || bad nothing-waiting-no-page

plutil -lint "$REPO/launchd/templates/__PREFIX__.capsule-corp-keeper-watch.plist" >/dev/null && pass plist-lints || bad plist-lints
grep -qx "__PREFIX__.capsule-corp-keeper-watch" "$REPO/launchd/unloaded" && pass install-only-unloaded || bad install-only-unloaded
# Z1: contracts/intent/keeper_watch.jsonl replayed here - each example names a check above that passed.
python3 -c 'import json,sys; rows=[json.loads(l) for l in open(sys.argv[1]) if l.strip()]; got=sys.argv[2].split("|"); sys.exit(0 if rows and all(r["expect"]["pass"] in got for r in rows) else 1)' \
  "$REPO/contracts/intent/keeper_watch.jsonl" "$PASSED" && pass intent-examples || bad intent-examples
exit $fail
