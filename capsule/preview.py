"""The owner look (gate F1, board side). A preview card is a `blocked` task with block kind
`owner_look` (filed by plan cut, with its slice cards as parents). When its parents are done,
the tick asks the owner once. Only the owner accepts; the owner or the CoS may reject. A reject
files one triage fix card that the preview card waits on, then the owner is asked again.
Verbs: `capsule human <board> <card> preview accept|reject '<text>'` (human file kind `preview`).
The ordinary reset, unblock, accept and close verbs never move a preview card (`guard`)."""
import contextlib
import sys

from capsule import blocks, db, human, outbox, paths, slack_msgs
from capsule.db import event, new_task, open_parent, txn

KIND = "owner_look"
ALLOWED = ("preview", "answer", "amend", "note")   # the only verbs a preview card takes
PAGE_AFTER = 3          # rejects of one card after which the next ask is a page
_STATE = ("preview_asked", "preview_rejected")


def is_look(row):
    return row is not None and row["status"] == "blocked" and row["block_kind"] == KIND


def _count(conn, task_id, kind):
    return conn.execute("SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind=?",
                        (task_id, kind)).fetchone()[0]


def asked(conn, task_id):
    """True when the newest ask is not yet answered by a reject."""
    row = conn.execute("SELECT kind FROM task_events WHERE task_id=? AND kind IN (?,?)"
                       " ORDER BY id DESC LIMIT 1", (task_id, *_STATE)).fetchone()
    return row is not None and row[0] == "preview_asked"


def split(text):
    verb, _, note = (text or "").strip().partition(" ")
    return verb.lower(), note.strip()


def refusal(conn, task_id, verb, note, author):
    """One plain line saying why the verb must change nothing, else None. `conn` may be None
    (the writer checks the words and the author only; the tick checks the card too)."""
    author = human.canonical_author(author)
    if verb not in ("accept", "reject"):
        return "preview takes accept or reject, then the words"
    if verb == "accept" and author != "owner":
        return "only the owner may accept a preview"
    if author not in ("owner", "cos"):
        return "only the owner or the CoS may reject a preview"
    if verb == "reject" and not note:
        return "reject needs the words that say what is wrong"
    if conn is None:
        return None
    row = conn.execute("SELECT status, block_kind FROM tasks WHERE id=?", (task_id,)).fetchone()
    if not is_look(row):
        return f"{task_id} is not a preview card that waits for a look"
    if not asked(conn, task_id):
        return f"{task_id} has not been shown yet: its cards are open"
    if verb == "accept" and open_parent(conn, task_id) is not None:
        return f"{task_id} cannot be accepted: a card before it is open again"
    return None


def preflight(capsule_home, args):
    """The writer's check: print the refusal and return 1, or return 0 (nothing was written)."""
    verb, note = split(args.text)
    path = paths.board_db(args.board, capsule_home)
    if path.is_file():
        with contextlib.closing(db.connect_ro(path)) as conn:
            why = refusal(conn, args.task_id, verb, note, args.author)
    else:
        why = refusal(None, args.task_id, verb, note, args.author)
    if why:
        print(f"capsule human: {why}; nothing was changed", file=sys.stderr)
    return 1 if why else 0


def _stamp(conn, intervention_id, outcome):
    conn.execute("UPDATE task_events SET payload=json_set(COALESCE(payload, '{}'), '$.outcome', ?)"
                 " WHERE id=?", (outcome, intervention_id))


def guard(conn, task_id, kind, intervention_id):
    """True (and the intervention stamped refused) when an ordinary verb aims at a preview card."""
    if kind in ALLOWED:
        return False
    row = conn.execute("SELECT status, block_kind FROM tasks WHERE id=?", (task_id,)).fetchone()
    if not is_look(row):
        return False
    _stamp(conn, intervention_id, f"refused:{kind} cannot move a preview card; use preview")
    return True


def handle(conn, task_id, kind, doc, intervention_id, now):
    """True when the tick has dealt with this file: the `preview` verb, or an ordinary verb that a
    preview card refuses (one transaction with the intervention event)."""
    if kind == "preview":
        _apply(conn, task_id, doc, intervention_id, now)
        return True
    return guard(conn, task_id, kind, intervention_id)


def _apply(conn, task_id, doc, intervention_id, now):
    verb, note = split(doc.get("text"))
    why = refusal(conn, task_id, verb, note, doc.get("author"))
    if why:
        _stamp(conn, intervention_id, f"refused:{why}")
        return
    by = human.canonical_author(doc.get("author"))
    if verb == "accept":
        db.transition(conn, task_id, "done", frm=("blocked",), claim=True, block_kind=None,
                      completed_at=now)
        event(conn, task_id, "preview_accepted", {"by": by, "note": note}, now=now)
        event(conn, task_id, "completed", {"outside": True, "author": by}, now=now)
    else:
        _reject(conn, task_id, by, note, now)
    blocks._handled(conn, task_id, now)
    for n in range(PAGE_AFTER, _count(conn, task_id, "preview_rejected") + 1):    # page asks too
        outbox.finish(conn, f"{slack_msgs.needs_key(task_id)}:page{n}",
                      f"{slack_msgs.HANDLED} {task_id}: handled.", now)
    _stamp(conn, intervention_id, "applied")


def _reject(conn, task_id, by, note, now):
    card = conn.execute("SELECT title, branch_name FROM tasks WHERE id=?", (task_id,)).fetchone()
    fix = new_task(conn, title=f"Fix after owner look: {card['title']}"[:120], status="triage",
                   branch=card["branch_name"], now=now,
                   body=f"The owner looked at the preview {task_id} and rejected it:\n{note}")
    conn.execute("INSERT INTO task_links (parent_id, child_id) VALUES (?,?)", (fix, task_id))
    event(conn, task_id, "preview_rejected",
          {"by": by, "text": note, "fix_card": fix, "count": _count(conn, task_id, _STATE[1]) + 1},
          now=now)


def step(conn, cfg, now):
    """Ask the owner once for each preview card whose parents are all done and not yet shown."""
    for row in conn.execute("SELECT id, body FROM tasks WHERE status='blocked' AND block_kind=?"
                            " ORDER BY id", (KIND,)).fetchall():
        if open_parent(conn, row["id"]) is None and not asked(conn, row["id"]):
            _ask(conn, cfg, row, now)


def _ask(conn, cfg, row, now):
    tid = row["id"]
    rejects = _count(conn, tid, "preview_rejected")
    base = f"python3 -m capsule human {cfg['board']} {tid} preview"
    reply = f"{base} accept '<note>' | {base} reject '<what is wrong>'"
    page = rejects >= PAGE_AFTER
    head = (f"{tid} preview rejected {rejects} times: the project needs your decision"
            if page else f"{tid} owner preview: look now")
    key = slack_msgs.needs_key(tid) + (f":page{rejects}" if page else "")
    look = row["body"] or ""
    with txn(conn):
        event(conn, tid, "preview_asked", {"count": rejects}, now=now)
        outbox.notify(conn, "ask" if outbox.routing_v2(conn) else "needs_input", head,
                      fields=[("look at", look[:1500])], reply=reply, thread_key=tid, page=page,
                      key=key, file_body=f"capsule: {head}: {' '.join(look.split())[:1500]}"
                                f" | reply: {reply}", now=now)
