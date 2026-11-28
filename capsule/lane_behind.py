"""Z5 detector (L155, the 2026-11-02 lane that ran 22 commits behind main): a lane clone held
dirty by a long-running card is never synced, so its card is checked against old code. Detection
only: `epic_sync.sync_idle` measures each held clone against main (a private ref, throttled) and
`note`s it here; `capsule status` reads `find`. Nothing is committed or merged for the lane.

Guard `lane_behind:<branch>` holds `{behind, hours, checked}` while the base is more than
BEHIND_MAX commits behind main, or its oldest missing commit is BEHIND_HOURS or more old, and
`{}` otherwise. `find` hides a record whose check is older than STALE_S (a stopped tick must not
keep a lane red)."""
import json

from capsule import db

PREFIX = "lane_behind:"
BEHIND_MAX = 10       # commits on origin/main the lane base lacks
BEHIND_HOURS = 12     # age of the oldest such commit
CHECK_S = 10 * 60     # a held clone is fetched at most this often
TRY_PREFIX = "lane_behind_try:"
STALE_S = 60 * 60


def note(conn, branch, behind, hours, now):
    """Record (or clear) the lane's lag; only a change in the record writes."""
    lagging = behind > BEHIND_MAX or (behind > 0 and hours >= BEHIND_HOURS)
    rec = {"behind": behind, "hours": hours, "checked": now} if lagging else {}
    old = db.guard_get(conn, PREFIX + branch)[0] or {}
    if rec or old:
        db.guard_set(conn, PREFIX + branch, rec, now=now)


def find(conn, now):
    """`[{branch, behind, hours}]` for each lane currently recorded as behind, by branch."""
    out = []
    rows = conn.execute("SELECT key, value FROM guard_state WHERE key LIKE ? ORDER BY key",
                        (PREFIX + "%",))
    for key, raw in rows:
        try:
            rec = json.loads(raw)
        except ValueError:
            continue
        if isinstance(rec, dict) and rec and 0 <= now - rec.get("checked", 0) <= STALE_S:
            out.append({"branch": key[len(PREFIX):], "behind": rec["behind"],
                        "hours": rec["hours"]})
    return out
