"""`capsule veto <board> <decision id>`: take back a decision Capsule made alone (`stall_owner`).
`veto` is the function (a test passes its own connection); `handle` is the command."""
import json

from capsule import blocks, stop_queue
from capsule.db import event, guard_set, new_task, txn


def _undo(conn, card, why, now):
    """Take the decision back wherever the card is now. A parkable card parks. A running or review
    card stops when its run ends (`stop_queue.request` queues the park). A done or archived card
    cannot park: a revert card is filed for the same lane."""
    if card["status"] in ("done", "archived"):
        tid = new_task(conn, title=f"Revert {card['id']}: {why}"[:120], status="blocked",
                 request_needs="decision",
                 body=f"The owner vetoed Capsule's decision on {card['id']} after it landed: {why}."
                 f" Undo the landed change of {card['id']} and nothing else.",
                 branch=card["branch_name"], assignee=card["assignee"], now=now)
        event(conn, tid, "blocked", {"reason": "revert card: the owner starts it", "kind":
              "needs_input"}, now=now)      # held for the owner: it does not start by itself
        return True
    if card["status"] in stop_queue.LIVE_STATUSES + ("claimed",):
        return stop_queue.request(conn, card["id"], why, now)
    return blocks.park(conn, card["id"], why, now=now)


def veto(conn, cfg, decision_id, now):
    """Undo one decision Capsule made alone, by its id.
    Park the card (a reason that starts `vetoed`; a running card stops when its run ends, a done
    card gets a revert card), or hold the first screen again. False, with nothing changed, for an
    unknown or an already vetoed id, or a card that cannot be taken back."""
    q = "SELECT task_id, payload FROM task_events WHERE kind=? AND json_extract(payload,'$.id')=?"
    row = conn.execute(q, ("auto_decision", decision_id)).fetchone()
    if row is None or conn.execute(q, ("auto_decision_vetoed", decision_id)).fetchone():
        return False
    card = conn.execute("SELECT * FROM tasks WHERE id=?", (row[0],)).fetchone()
    doc = json.loads(row[1])
    with txn(conn):
        if doc["source"] == "first_screen":
            held = blocks.first_screen(conn, card["branch_name"]) or {}
            done = held.get("state") == "ok" and guard_set(
                conn, blocks.FIRST_SCREEN_KEY.format(card["branch_name"]),
                dict(held, state="held"), now=int(now))
        else:
            done = _undo(conn, card, f"vetoed {decision_id}: {doc['option']}"
                         f" ({doc['source']}) was undone by the owner", int(now))
        if done:
            event(conn, card["id"], "auto_decision_vetoed", {"id": decision_id, "by": "human"},
                  now=int(now))
    return bool(done)


def add_parser(sub):
    p = sub.add_parser("veto", help=veto.__doc__.splitlines()[0])
    p.add_argument("board")
    p.add_argument("decision_id")
    p.add_argument("--capsule-home", default=None)


def handle(args, parser):
    import sys
    import time

    from capsule import db, paths
    path = paths.board_db(args.board, paths.home(args.capsule_home))
    if not path.is_file():
        print(f"capsule veto: no board {args.board!r}", file=sys.stderr)
        return 1
    conn = db.connect(path)
    try:
        ok = veto(conn, {"board": args.board}, args.decision_id, time.time())
        q = "SELECT 1 FROM task_events WHERE kind=? AND json_extract(payload,'$.id')=?"
        known = conn.execute(q, ("auto_decision", args.decision_id)).fetchone() is not None
        again = conn.execute(q, ("auto_decision_vetoed", args.decision_id)).fetchone() is not None
    finally:
        conn.close()
    did = args.decision_id
    msg = (f"capsule veto: no decision {did}" if not known else
           f"capsule veto: {did} is already vetoed" if again else
           f"capsule veto: the decision {did} still stands: the card could not be taken back"
           " (see its park_refused event)")
    print("vetoed" if ok else msg, file=None if ok else sys.stderr)
    return 0 if ok else 1
