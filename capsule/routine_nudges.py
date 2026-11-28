"""Which interventions are routine, so they open no `human_intervention` incident (split out of
incidents.py, which is at its size cap)."""
import json
import re

from capsule import blocks, human

# cos nudges: no incident
ROUTINE_COS_KINDS = ("assign", "reset", "epic", "close", "park", "stop", "accept", "amend", "wait",
                     "archive")
KORIN_HOLD_SQL = ("SELECT 1 FROM task_events WHERE id=(SELECT MAX(id) FROM task_events"
                  " WHERE task_id=? AND id<? AND kind IN ("   # latest hold or lift
                  + ",".join(f"'{k}'" for k in blocks.HOLD_EVENTS + blocks.LIFT_EVENTS) + "))"
                  " AND json_extract(payload, '$.kind')='needs_input'"
                  " AND json_extract(payload, '$.incident') IS NOT NULL"
                  " AND json_extract(payload, '$.reason') LIKE 'Korin fix card for incident%'")


def is_routine_intervention(conn, task_id, event_id, payload):
    """A cos nudge, or a cos unblock of a Korin fix card hold or of a lander wait that a second
    landing check can settle (`blocks.lander_wait`: the newest hold is one the lander set)."""
    kind = payload.get("kind")
    if payload.get("author") == "capsule":
        return True       # Capsule's own entry (the patrol, a decision made alone): never one
    hold = kind == "unblock" and (conn.execute(KORIN_HOLD_SQL, (task_id, event_id)).fetchone()
                                  or blocks.lander_wait(conn, task_id, event_id))
    refused = str(payload.get("outcome") or "").startswith("refused:")   # changed nothing
    return human.is_cos(payload.get("author")) and bool(
        kind in ROUTINE_COS_KINDS or hold or refused)


def is_routine_nudge(conn, incident):
    """An open human_intervention incident whose first `event:` evidence is a routine cos nudge."""
    if incident["status"] != "open" or incident["kind"] != "human_intervention":
        return False
    first = (re.findall(r"event:(\d+)", str(incident.get("evidence"))) + ["0"])[0]
    return any(is_routine_intervention(conn, r["task_id"], r["id"], json.loads(r["payload"]))
               for r in conn.execute("SELECT id, task_id, payload FROM task_events"
                                     " WHERE kind='intervention' AND id=?", (first,)))
