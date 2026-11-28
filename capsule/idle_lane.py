"""Z5 detector (the 2026-10-26 idle lane): a board with no live seat and a ready card the
dispatcher cannot take, for IDLE_S or longer. `capsule unblocked` and `capsule status` both read
`find`. Read-only; it reuses `dispatch`'s own hold predicate, so it cannot drift from `ready`.

"No live seat" is `seats.counts`' `heavy`: running implementers, live reviews and the lander.
A holder whose newest event is `review_deferred` is waiting for a seat, normal flow, not a hold.
So is a review holder with a live contract job (`contract_live`).

The idle clock: no card keeps a "became ready" time, so a card is ready since its newest
`task_events` row (its `created_at` if none), and the lane is idle since the later of that and the
newest run end on the board."""
import json

from capsule import db, dispatch, seats

IDLE_S = 15 * 60
LIVE_STATUSES = ("running", "review", "claimed")
CONTRACT_CAP_S = 2 * 3600   # a contract job older than this is not live (no config key exists)


def contract_live(conn, task_id, now):
    """A review holder's contract job runs: it holds no seat and persists only `contract_started`
    (payload `run_id`). Live while that event is under CONTRACT_CAP_S old, with no later
    `contract_timeout` or `contract_failed` and no `task_runs` row of that run_id (its end)."""
    row = conn.execute("SELECT id, payload, created_at FROM task_events WHERE task_id=? AND"
                       " kind='contract_started' ORDER BY id DESC LIMIT 1", (task_id,)).fetchone()
    if row is None or now - row["created_at"] > CONTRACT_CAP_S:
        return False
    run_id = (json.loads(row["payload"] or "{}") or {}).get("run_id")
    return not conn.execute(
        "SELECT 1 FROM task_events WHERE task_id=? AND id>? AND"
        " kind IN ('contract_timeout', 'contract_failed', 'contract_infra')"
        " UNION ALL SELECT 1 FROM task_runs WHERE task_id=? AND run_id=?",
        (task_id, row["id"], task_id, run_id)).fetchone()


def any_contract_live(conn, now):
    """True when any card in review has a live contract job."""
    return any(contract_live(conn, c[0], now)
               for c in conn.execute("SELECT id FROM tasks WHERE status='review'"))


CONTRACT_S = 2 * 3600      # a `contract_started` older than this with no end is a dead check


def contract_cards(conn, now):
    """Ids of cards whose contract check runs: the newest non-stall event is `contract_started`
    (so no later `contract_timeout`), under CONTRACT_S old, with no later run row. The check is a
    tick thread, not a `task_runs` row, and takes 10-30 min."""
    return [r[0] for r in conn.execute(
        "SELECT t.id FROM tasks t JOIN task_events e ON e.id=(SELECT MAX(id) FROM task_events"
        " WHERE task_id=t.id AND kind NOT LIKE 'stall_%') WHERE e.kind='contract_started'"
        " AND e.created_at>? AND NOT EXISTS (SELECT 1 FROM task_runs r WHERE r.task_id=t.id"
        " AND r.started_at>=e.created_at)", (now - CONTRACT_S,))]


def working(conn, now):
    """Work is in flight: an open run, or a live contract check (`contract_cards`)."""
    return bool(conn.execute("SELECT 1 FROM task_runs WHERE ended_at IS NULL").fetchone()
                or contract_cards(conn, now))


def _deferred(conn, task_id):
    last = conn.execute("SELECT kind FROM task_events WHERE task_id=? ORDER BY id DESC LIMIT 1",
                        (task_id,)).fetchone()
    return last is not None and last[0] == "review_deferred"


def label(holder, hold):
    """What the hold is called on screen. `hold` stays the machine key; a blocked or triage
    holder is named honestly (the clone may be clean): `branch held by <id> (blocked: <kind>)`."""
    if holder["status"] in ("blocked", "triage"):
        why = holder["block_kind"] or holder["status"]
        return f"branch held by {holder['id']} (blocked: {why})"
    return hold


def find(conn, now, work_root=None, idle_s=None):
    """`None`, or the oldest held ready card as `{"ready", "holder", "hold", "idle_s"}`: rows for
    the ready card and the card holding its clone; `hold` is `branch-busy` when the holder is live
    on the branch, else `clone-dirty`; `label` is `hold` unless the holder is blocked."""
    if seats.counts(conn, None, None, now)["heavy"] or dispatch.any_pause(conn, now, checks=True):
        return None
    last_end = conn.execute("SELECT COALESCE(MAX(ended_at), 0) FROM task_runs").fetchone()[0]
    for card in conn.execute("SELECT * FROM tasks WHERE status='ready' ORDER BY created_at, id"):
        holder_id = dispatch.clone_dirty(conn, card["branch_name"], card["id"], work_root)
        if holder_id is None or db.open_parent(conn, card["id"]) is not None:
            continue
        since = conn.execute("SELECT COALESCE(MAX(created_at), 0) FROM task_events WHERE task_id=?",
                             (card["id"],)).fetchone()[0] or card["created_at"] or now
        idle = now - max(since, last_end)
        holder = conn.execute("SELECT * FROM tasks WHERE id=?", (holder_id,)).fetchone()
        if idle < (idle_s or IDLE_S) or _deferred(conn, holder_id) or (
                holder["status"] == "review" and contract_live(conn, holder_id, now)):
            continue
        hold = "branch-busy" if holder["status"] in LIVE_STATUSES else "clone-dirty"
        return {"ready": card, "holder": holder, "idle_s": idle, "hold": hold,
                "label": label(holder, hold)}
    return None
