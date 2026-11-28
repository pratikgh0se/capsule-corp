"""A decision Capsule made alone, as one more human answer in a worker brief (`stall_owner`)."""
import json


def with_decided(conn, task_id, entries):
    """`entries` (a card's human answers for a worker brief) plus one entry per decision Capsule
    made alone on the card, oldest first. Author `capsule`, kind `answer`, with the veto command.
    It is not an `intervention` event and not owner work."""
    if conn is None:
        return entries
    rows = conn.execute("SELECT created_at, payload FROM task_events WHERE task_id=? AND"
                        " kind='auto_decision' ORDER BY id", (task_id,)).fetchall()
    vetoed = {json.loads(p)["id"] for (p,) in conn.execute(
        "SELECT payload FROM task_events WHERE task_id=? AND kind='auto_decision_vetoed'",
        (task_id,))}
    own = [{"kind": "answer", "author": "capsule", "at": at,
            "text": f"decided by Capsule alone, not by the owner: option {doc['option']}"
                    f" ({doc.get('label') or doc['source']}). Veto with: {doc['undo']}"}
           for at, doc in ((r[0], json.loads(r[1])) for r in rows) if doc["id"] not in vetoed]
    return sorted([*entries, *own], key=lambda e: e.get("at") or 0)
