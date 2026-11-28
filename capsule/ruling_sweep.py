"""Idempotent ruling-card sweep."""
import json

from capsule.bundles_guide import ASKS
from capsule.db import event, transition, txn


def close_card(conn, card_id, now):
    """Close one triage ruling card as `done` and say so with a `completed` event (a
    status move with no event pages lost_card_state). True if this call closed it."""
    n = transition(conn, card_id, "done", frm=("triage",), completed_at=now)
    if n:
        event(conn, card_id, "completed", now=now)
    return bool(n)


def close_ruled(conn, now):
    """The ids of every guide_ruling triage card this pass closed as `done`."""
    cards = conn.execute(
        "SELECT t.id, l.parent_id FROM tasks t JOIN task_links l ON l.child_id=t.id"
        " WHERE t.status='triage' AND t.request_needs='role:guide'").fetchall()
    closed = []
    with txn(conn):
        for card in cards:
            if _ruled(conn, card["parent_id"], card["id"]):
                if close_card(conn, card["id"], now):
                    closed.append(card["id"])
    return closed


def _ruled(conn, task_id, card_id):
    """Whether `card_id` (a guide_ruling triage card on `task_id`) names a contradiction event
    that a later `guide_ruling` event on the same task already answered with anything but
    `ask-owner` - the guide DID rule, so the card's job is done regardless of what happened next."""
    contradiction_ids = []
    for row in conn.execute("SELECT id, payload FROM task_events WHERE task_id=? AND kind='contradiction'",
                            (task_id,)):
        if json.loads(row["payload"] or "{}").get("ruling_card") == card_id:
            contradiction_ids.append(row["id"])
    if not contradiction_ids:
        return False
    for (raw,) in conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='guide_ruling'",
                               (task_id,)):
        payload = json.loads(raw or "{}")
        if payload.get("contradiction") in contradiction_ids and payload.get("choice") not in ASKS:
            return True
    return False
