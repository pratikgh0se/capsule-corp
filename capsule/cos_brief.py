"""`python3 -m capsule cos brief` (row 637, slice 4): the live half of the chief-of-staff
briefing (spec 2026-10-29, section 5). It prints the `capsule status` screen (board health,
NEEDS YOU, blocked, attention), the asks still open, and "answered while you were away".

Read-only on every board. Its one write is a `<key>.seen` marker beside each away record it
showed, made after the text is written and flushed, so a crash mid-print shows the line again.
Asks live in `boards/<board>/asks/<key>.json` and answered ones in `asks/done/<key>.json`; a
missing directory is just empty (the listener, slice 7, writes them)."""
import json
import sys
import time
from pathlib import Path

from capsule import paths

# The heavy modules (statusview pulls in blocks, tick_guard ...) load inside the functions that
# need them, so `python3 -m capsule` stays light to start. capsule/cos_ask.py owns the `cos`
# parser and calls `run`.


def _records(directory):
    """[(path, doc)] for each readable JSON object in `directory`; a missing one is empty."""
    out = []
    for p in sorted(Path(directory).glob("*.json")):
        try:
            doc = json.loads(p.read_text())
        except (OSError, ValueError):
            continue
        if isinstance(doc, dict):
            out.append((p, doc))
    return out


def _title(doc):
    from capsule import statusview
    return statusview.plain(doc.get("title") or doc.get("what") or "A question", 100)


def _happened(conn, doc):
    """What the tick did with the answer: the first `intervention` event on the card at or after
    the answer's time, as words; None when the board has none yet."""
    since = doc.get("answered_at") or 0
    for (payload,) in conn.execute("SELECT payload FROM task_events"
                                   " WHERE task_id=? AND kind='intervention' ORDER BY id",
                                   (doc.get("task_id"),)):
        try:
            p = json.loads(payload or "{}")
        except ValueError:
            continue
        if isinstance(p, dict) and (p.get("at") or 0) >= since:
            outcome = str(p.get("outcome") or "applied")
            return outcome.replace("refused:", "refused, ") if outcome != "applied" else "done"
    return None


def _away_line(doc, conn):
    from capsule import statusview
    if not doc.get("choice"):
        why = statusview.plain(doc.get("closed"), 60)
        return f"  {_title(doc)} · closed without an answer ({why})"
    when = time.strftime("%a %H:%M", time.localtime(doc.get("answered_at") or 0))
    line = (f"  {_title(doc)} · you chose {statusview.plain(doc['choice'], 40)}"
            f" ({statusview.plain(doc.get('via') or 'unknown', 20)}) · {when}")
    happened = _happened(conn, doc) if conn is not None else None
    line += f" · then: {happened or 'not applied yet'}"
    if doc.get("note"):
        line += f" · saved as a note: {statusview.plain(doc['note'], 200)}"
    return line


def _away(home, board):
    """(lines, record paths) for this board's `asks/done` records with no `.seen` marker."""
    fresh = [(p, d) for p, d in _records(paths.board_dir(board, home) / "asks" / "done")
             if not p.with_suffix(".seen").exists()]
    if not fresh:
        return [], []
    from capsule import db
    try:
        conn = db.connect_ro_tolerant(paths.board_db(board, home))
    except Exception:       # an unreadable board still gets its answers listed
        conn = None
    try:
        return [_away_line(d, conn) for _, d in fresh], [p for p, _ in fresh]
    finally:
        if conn is not None:
            conn.close()


def _open_asks(home, boards):
    return [f"  📨 {_title(d)} · {b}" for b in boards
            for _, d in _records(paths.board_dir(b, home) / "asks")]


def run(args, out):
    from capsule import ask_apply, status_cli, statusview
    home = paths.home(args.capsule_home)
    snap = statusview.snapshot(home)
    boards = [b["board"] for b in snap["boards"]]
    asks = _open_asks(home, boards)
    away, seen = [], []
    for board in boards:
        lines, marks = _away(home, board)
        away += lines
        seen += marks
    text = ["CHIEF OF STAFF BRIEF", "", status_cli.render(snap), "",
            f"📨 ASKS WAITING FOR AN ANSWER ({len(asks)})"] + (asks or ["  no asks waiting"])
    gate = ask_apply.line(home, boards, time.time() - 86400, time.time() + 1)
    if gate:
        text += ["", gate] + ask_apply.detail(
            home, boards, time.time() - 86400, time.time() + 1, "  \u2022 ")
    if away:
        text += ["", f"📬 ANSWERED WHILE YOU WERE AWAY ({len(away)})"] + away
    out.write("\n".join(text) + "\n")
    out.flush()
    for p in seen:      # only now: the lines above are on screen
        try:
            p.with_suffix(".seen").touch()
        except OSError as e:
            print(f"capsule cos brief: could not mark {p.stem} seen: {e}", file=sys.stderr)
    return 0
