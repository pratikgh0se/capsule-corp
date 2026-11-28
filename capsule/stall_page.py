"""The stall supervisor's incident and page helpers (`capsule.stall`): the one incident a stall
belongs to, and the pages of one pass (one notice per board when two or more are due)."""
import hashlib
import json
import re

from capsule import blocks, incidents, outbox

CARD_EVENT = ("held_triage", "held_blocked", "held_parked", "owner_wait", "unroutable",
              "review_deferred", "landing_stuck", "state_orphan")


def tag(parts):
    """Letters only: `incidents.normalize` masks ids and hex runs in a key."""
    digest = hashlib.sha256(",".join(map(str, parts)).encode()).hexdigest()[:8]
    return "".join(chr(ord("g") + int(c, 16)) for c in digest)


def incident(conn, cfg, it, now):
    """The incident this stall belongs to: the card's open cause incident (evidence attached),
    else one new `hang` incident. An owner wait or a park opens none."""
    card = it["class"] in CARD_EVENT
    line = f"stall: {it['class']}: {it['age_s'] // 60}m; " + "; ".join(it["evidence"])
    cause = conn.execute("SELECT id FROM incidents WHERE task_id=? AND status NOT IN"
                         " ('closed','superseded') ORDER BY opened_at DESC LIMIT 1",
                         (it["subject"],)).fetchone() if card else None
    if card and not cause:     # a recurrence or dedupe keeps the other card's task_id
        cause = conn.execute(
            "SELECT i.id FROM incidents i, json_each(i.evidence) e WHERE i.status NOT IN"
            " ('closed','superseded') AND e.value='event:' || (SELECT MAX(id) FROM task_events"
            " WHERE task_id=? AND kind IN (SELECT value FROM json_each(?))"
            " AND id > COALESCE((SELECT MAX(id) FROM task_events WHERE task_id=?1"
            " AND kind IN (SELECT value FROM json_each(?3))), 0))"
            " ORDER BY i.opened_at DESC LIMIT 1",
            (it["subject"], json.dumps(blocks.HOLD_EVENTS),
             json.dumps(blocks.LIFT_EVENTS))).fetchone()
    if cause:
        incidents.attach(conn, cause[0], [line])
        return cause[0]
    if it["class"] in ("owner_wait", "held_parked"):
        return None            # a person's hold (or a deliberate park) is no fault of Capsule
    run = conn.execute("SELECT MAX(id) FROM task_runs WHERE task_id=?", (it["subject"],)).fetchone()
    key = ("stall: unknown: " + it["subject"] if it["class"] == "no_progress"
           else f"stall: {it['class']}: {tag([it['subject'], run[0] if run else 0])}")
    board = cfg.get("board") or "board"
    return incidents.open_incident(
        conn, board=board, kind="hang", key=key, task_id=it["subject"] if card else None,
        description=f"{board}: {line}", evidence=[line], now=now)[0]


def _label(conn, d):
    """`held triage "card title" 2 h 00 min`: the title cut to 40 characters, never the card id."""
    row = conn.execute("SELECT title FROM tasks WHERE id=?", (d["it"]["subject"],)).fetchone()
    title = re.sub(r"\bt_[0-9a-f]{6,}\b", "a card", " ".join(str(row[0] if row else "").split()))
    return (f"{d['it']['class'].replace('_', ' ')} " + (f'"{title[:40]}" ' if title else "")
            + d["clock"])


def flush(conn, cfg, due, now):
    """Send one pass's pages. One page goes out as before. Two or more: each keeps its file row,
    and the board gets ONE local row (and one Slack row when any is due for Slack):
    "<board>: N stalls", the first 3 as "class "title" clock", then "+K more"."""
    if len(due) == 1:
        d = due[0]
        if d["slack"]:
            outbox.notify(conn, "incident", d["text"], page=True, sound=d["first"],
                          file_body=d["body"], now=now)
        else:
            outbox.add(conn, "local", d["text"], now=now)
        return
    for d in due:
        outbox.add(conn, "file", d["body"], now=now)
    head = [_label(conn, d) for d in due[:3]]
    more = [f"+{len(due) - 3} more"] if len(due) > 3 else []
    text = f"{cfg.get('board') or 'board'}: {len(due)} stalls. " + "; ".join(head + more)
    sound = any(d["first"] for d in due)
    if any(d["slack"] for d in due):
        outbox.notify(conn, "incident", text, page=True, sound=sound, file=False, now=now)
    else:
        outbox.add(conn, "local", text, thread_key=outbox.LOCAL_SOUND if sound else None, now=now)
