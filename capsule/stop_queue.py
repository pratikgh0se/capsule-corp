"""`capsule human <board> <card> stop '<trigger>'`: park a doomed card once its current run ends.

A card in a parkable status parks at once, like `park`. A running or review card records one
`park_queued` event instead; `sweep` (once per tick pass) parks it when no run of it is live.
No run is ever signalled or killed. A later `reset` cancels the queue.
"""
import json

from capsule import blocks
from capsule.db import event

LIVE_STATUSES = ("running", "review")
# A stop ends with the park or a `stop_cancelled` (done, archived, or an applied reset).
PENDING_SQL = (
    "EXISTS (SELECT 1 FROM task_events q WHERE q.task_id=tasks.id AND q.kind='park_queued'"
    " AND q.id=(SELECT MAX(id) FROM task_events WHERE task_id=tasks.id AND kind='park_queued')"
    " AND NOT EXISTS (SELECT 1 FROM task_events e WHERE e.task_id=tasks.id AND e.id>q.id"
    " AND e.kind IN ('parked_by_human', 'stop_cancelled')))")
_PAST = {"done": "approved, landing: use close/revert", "archived": "archived: nothing to stop"}


def _trigger(conn, task_id):
    row = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='park_queued'"
                       " ORDER BY id DESC LIMIT 1", (task_id,)).fetchone()
    return json.loads(row[0]).get("trigger") if row else None


def pending(conn, task_id):
    """The queued trigger while the stop still stands, else None (PENDING_SQL decides)."""
    standing = conn.execute(f"SELECT 1 FROM tasks WHERE id=? AND {PENDING_SQL}",
                            (task_id,)).fetchone()
    return _trigger(conn, task_id) if standing else None


def cancel(conn, task_id, reason, now):
    """Write `stop_cancelled` when a stop stands on the card (an applied reset, a past status)."""
    if pending(conn, task_id) is not None:
        event(conn, task_id, "stop_cancelled", {"reason": reason}, now=now)


def request(conn, task_id, trigger, now):
    """True when the card parked now or the stop was queued; else False plus `stop_refused`."""
    row = conn.execute("SELECT status FROM tasks WHERE id=?", (task_id,)).fetchone()
    if row is None or not isinstance(trigger, str) or not trigger.strip():
        why = "no such task" if row is None else "stop needs a non-empty trigger"
    elif row["status"] in blocks.REASSIGNABLE_STATUSES:
        return blocks.park(conn, task_id, trigger, now=now)
    elif row["status"] in _PAST:    # a stop never holds the lander: done is approved
        why = _PAST[row["status"]]
    elif pending(conn, task_id) is not None:
        why = "already queued"
    else:
        event(conn, task_id, "park_queued", {"trigger": trigger, "by": "human"}, now=now)
        return True
    event(conn, task_id, "stop_refused", {"trigger": trigger, "reason": why}, now=now)
    return False


def sweep(tick, conn, cfg, now, counts):
    """Per card, guarded: park each standing stop with no live run; cancel one whose card is
    past parking (done, archived)."""
    for row in conn.execute(f"SELECT id, status FROM tasks WHERE {PENDING_SQL}").fetchall():
        tick._guarded(conn, cfg, row["id"], now, counts, lambda row=row: _settle(conn, row, now))


def _settle(conn, row, now):
    if row["status"] in _PAST:
        return cancel(conn, row["id"], f"{row['status']}: past parking", now)
    live = conn.execute("SELECT 1 FROM task_runs WHERE task_id=? AND ended_at IS NULL",
                        (row["id"],)).fetchone()
    if not live:
        blocks.park(conn, row["id"], _trigger(conn, row["id"]), now=now, queued=True)
