"""A guide `document-wrong` ruling on a reviewer verdict reruns the reviewer (inc_c4473fc2).

The wrong document is the reviewer's verdict, which the implementer can neither rewrite nor edit.
A card reset would dispatch an implementer run, and that run adds a review round no guide reset
clears. So the card goes back to review on the same implementer run. The ruled run carries
`ruled_wrong` (the guide's reason); `Tick._review_round` skips it, so only the reviewer reruns."""
import json

from capsule import blocks
from capsule.db import event, transition


def apply(conn, task_id, role, run_pk, reason, now):
    """Apply `document-wrong`: a reviewer verdict marks the ruled run and moves the card from
    triage back to review; any other role resets the card for an implementer rerun."""
    if role != "reviewer":
        return blocks.reset_failures(conn, task_id, by="guide", now=now)
    conn.execute("UPDATE task_runs SET metadata=json_set(COALESCE(metadata, '{}'),"
                 " '$.ruled_wrong', ?) WHERE id=?", (reason, run_pk))
    transition(conn, task_id, "review", frm=("triage",), claim=True, block_kind=None,
               block_recurrences=0)
    floor = _request_review(conn, task_id, run_pk, now)
    _requeue_siblings(conn, task_id, run_pk, floor, now)


def _request_review(conn, task_id, run_pk, now):
    """The lifecycle event for the triage -> review move (db.py:300, replay.py:54).
    It keeps the `after_run` that `Tick._round_floor` reads."""
    impl = conn.execute("SELECT * FROM task_runs WHERE run_id=(SELECT json_extract(metadata,"
                        " '$.review_of') FROM task_runs WHERE id=?)", (run_pk,)).fetchone()
    row = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='request_review'"
                       " AND run_id=? ORDER BY id DESC LIMIT 1", (task_id, impl["id"])).fetchone()
    after = json.loads(row["payload"]).get("after_run") if row and row["payload"] else None
    event(conn, task_id, "request_review", {"by": "guide", "after_run": after}, run_pk=impl["id"],
          run_id=impl["run_id"], now=now)
    return max(impl["id"], after or 0)


def _requeue_siblings(conn, task_id, run_pk, floor, now):
    """A sibling contradiction recorded `routed: None` (the card was already routed) would slip
    into the verdict now that no reset discards it: record it `pending` so the tick routes it
    after the reviewer reruns."""
    for e in conn.execute(
            "SELECT * FROM task_events e WHERE task_id=? AND run_id>? AND run_id!=?"
            " AND kind='contradiction' AND NOT EXISTS (SELECT 1 FROM task_events l"
            " WHERE l.task_id=e.task_id AND l.id>e.id AND l.kind='contradiction'"
            " AND l.run_id=e.run_id) ORDER BY id", (task_id, floor, run_pk)).fetchall():
        p = json.loads(e["payload"] or "{}")
        if p.get("routed") is None:
            event(conn, task_id, "contradiction", dict(p, routed="pending"), run_pk=e["run_id"],
                  run_id=p.get("run_id"), now=now)


def reason(conn, task_id, floor):
    """The guide's reason on the latest reviewer run after `floor` that it ruled wrong, or None."""
    row = conn.execute(
        "SELECT json_extract(metadata, '$.ruled_wrong') FROM task_runs WHERE task_id=? AND id>?"
        " AND profile='reviewer' AND json_extract(COALESCE(metadata, '{}'), '$.ruled_wrong')"
        " IS NOT NULL ORDER BY id DESC LIMIT 1", (task_id, floor)).fetchone()
    return row[0] if row else None
