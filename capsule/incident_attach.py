"""`incidents attach`: name the card that is an incident's fix, on this board or another (U2)."""
import sqlite3

from capsule import blocks, db, incident_watch, incidents, paths


def _other_board(home, old):
    """(refusal or None, note) for a fix card on another board, read read-only: it may be swapped
    only when it is archived, done, parked or gone there; the note says which."""
    board, task = incidents.split_fix_card(old)
    path = paths.board_db(board, home or paths.home())
    try:
        path.stat()
    except FileNotFoundError:
        return None, f"board {board} db missing"
    except OSError as exc:
        raise ValueError(f"fix card {old} is on board {board}, which cannot be read"
                         f" ({exc})") from exc
    try:
        other = db.connect_ro(path)
        try:
            row = other.execute("SELECT status, block_kind FROM tasks WHERE id=?",
                                (task,)).fetchone()
        finally:
            other.close()
    except sqlite3.Error as exc:
        raise ValueError(f"fix card {old} is on board {board}, which cannot be read"
                         f" ({exc})") from exc
    if row is None:
        return None, f"card absent on board {board}"
    if row["status"] in ("archived", "done"):
        return None, f"card {row['status']} on board {board}"
    if row["status"] == "blocked" and row["block_kind"] == "parked":
        return None, f"card parked on board {board}"
    return f"fix card {old} is live on board {board} (status {row['status']})", ""


def _replaceable(conn, old):
    """Why the incident's current fix card may not be swapped, or None when it may."""
    row = conn.execute("SELECT status FROM tasks WHERE id=?", (old,)).fetchone()
    runs = conn.execute("SELECT 1 FROM task_runs WHERE task_id=? LIMIT 1", (old,)).fetchone()
    if row is None or row["status"] != "blocked" or runs is not None:
        return f"fix card {old} is not a blocked card with no runs"
    return None


def attach(conn, incident_id, card, *, board, home=None, now=None):
    """`incidents attach <id> --card <t_id|board:t_id>`: name the card that is this incident's fix.
    Only for an incident with no fix card, a Korin-made one still blocked with no runs, or one on
    another board that is archived, done, parked or gone there (the re-point is recorded as evidence
    and an `incident_reattached` event, in the same transaction). Looks cards up read-only; writes
    only this board. open -> fix_pending only when the record is already
    filled, so attach alone never skips Korin. Returns the stored value."""
    inc = incidents.get(conn, incident_id)
    if inc is None:
        raise LookupError(f"no incident {incident_id!r}")
    if inc["status"] in ("closed", incident_watch.SUPERSEDED):
        raise ValueError(f"incident {incident_id} is already {inc['status']}")
    target, task = incidents.split_fix_card(card)
    stored = task if target in (None, board) else f"{target}:{task}"
    if inc.get("fix_card") == stored:
        return stored   # already this incident's fix card: nothing changes
    if incidents.fix_card_status(conn, home, stored)[0] is None:
        raise LookupError(f"card {stored} not found on board {target or board}")
    old = inc.get("fix_card")
    swap = bool(old and old != stored)
    note = ""
    if swap and ":" in old:
        why, note = _other_board(home, old)
    else:
        why = _replaceable(conn, old) if swap else None
    if why:
        raise ValueError(f"incident {incident_id} keeps its card: {why}")
    filled = all((inc.get(f) or "").strip() for f in incidents.RECORD_FIELDS)
    with db.txn(conn):
        _store(conn, inc, stored, filled, board, now)
        if note:
            _record_repoint(conn, inc, stored, note, board, now)
    if swap and not note:
        # spec 12: a swapped-out card is parked, so an unblock cannot dispatch a second fix
        blocks.park(conn, old, f"superseded by {stored} for incident {incident_id}", now=now)
    return stored


def _store(conn, inc, stored, filled, board, now):
    """The guarded write: it lands only if the incident still has the fix card and a live status
    that were read before the old board was looked at."""
    done = conn.execute(
        "UPDATE incidents SET fix_card=?, status=CASE WHEN status='open' AND ? THEN 'fix_pending'"
        " ELSE status END WHERE id=? AND fix_card IS ? AND status NOT IN ('closed', ?)",
        (stored, int(filled), inc["id"], inc.get("fix_card"), incident_watch.SUPERSEDED))
    if done.rowcount != 1:
        raise ValueError(f"incident {inc['id']} changed during the attach: unchanged")
    if ":" not in stored:
        db.event(conn, stored, "fix_card_attached",
                 {"incident": inc["id"], "card": stored, "board": board}, now=now)


def _record_repoint(conn, inc, stored, note, board, now):
    """A re-point off another board's dead card, inside the caller's transaction: an evidence line,
    plus an `incident_reattached` event owned by a card of this board (the new card, else the
    incident's own card, else the incident id; task_events has no foreign key). The old board is
    never written."""
    old = inc["fix_card"]
    line = f"fix card re-pointed {old} -> {stored}: old {note}"
    host = next((c for c in (stored, inc.get("task_id")) if c and ":" not in c and conn.execute(
        "SELECT 1 FROM tasks WHERE id=?", (c,)).fetchone()), inc["id"])
    incidents.attach(conn, inc["id"], [line])
    db.event(conn, host, "incident_reattached", {"incident": inc["id"], "old": old,
             "card": stored, "board": board, "why": note}, now=now)
