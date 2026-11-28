"""Synthetic boards for the evals metrics (evals spec 2.1, card t_946c2952).

`build(kind, home)` creates the board BY CODE (no db file is checked in) and returns a `Built`:
the real board under `home` (tasks, runs, events, incidents, `human/` spool files) plus the raw
rows that live outside the board: `decision_rows` (the vault decision table, in
`decision_log.parse` shape), `cos_log` and `git_subjects`. `expected.json` beside this file
holds the hand-stated numbers per board.
Kinds: `good_board`, `bad_board`, `empty_board` (zero landed cards).
"""

import json
from collections import namedtuple
from pathlib import Path

from capsule import db, human, incidents, paths

BOARD = "evalfix"
T0 = 1_000_000
NEXT_DAY = "1970-01-13"  # another day, for the act that is not a double record
DAY = "1970-01-12"  # the day T0 falls on
HERE = Path(__file__).resolve().parent
Built = namedtuple("Built", "home board ids decision_rows cos_log git_subjects")


def expected():
    return json.loads((HERE / "expected.json").read_text(encoding="utf-8"))


def _card(conn, name, ids, *, runs=1, tokens=(1000, 500, 500), landed=True, outcome="completed"):
    """One card `name`: `runs` implementer runs of `tokens` (in, out, cache write) each."""
    n = len(ids)
    tid = db.new_task(conn, title=name, now=T0 + n)
    ids[name] = tid
    for i in range(runs):
        conn.execute(
            "INSERT INTO task_runs (task_id, profile, status, outcome, started_at, ended_at,"
            " tokens_in,"
            " tokens_out, tokens_cache_write) VALUES (?, 'implementer', 'done', ?, ?, ?, ?, ?, ?)",
            (
                tid,
                outcome if i == runs - 1 else "changes_requested",
                T0 + n + i * 10,
                T0 + n + i * 10 + 5,
                *tokens,
            ),
        )
    if landed:
        db.event(conn, tid, "completed", {"commit": f"c{n:03d}", "gates": "ok"}, now=T0 + 500 + n)
        conn.execute(
            "UPDATE tasks SET status='done', completed_at=? WHERE id=?", (T0 + 500 + n, tid)
        )
    return tid


def _spool(home, conn, ids, name, kind, text, at, *, tag=None, archive=True):
    """A human spool file plus the `intervention` event, as the tick leaves them: the file moved to
    `human/done/` (unless `archive` is False: still pending) and the event naming both names."""
    human.write(home, BOARD, ids[name], kind, text, now=at, tag=tag)
    hdir = paths.board_db(BOARD, home).parent / "human"
    src = max(hdir.glob("*.json"), key=lambda p: int(p.stem)).name
    if archive:
        (hdir / "done").mkdir(exist_ok=True)
        (hdir / src).rename(hdir / "done" / src)
    payload = {"kind": kind, "text": text, "at": at, "file": src, "src": src}
    db.event(conn, ids[name], "intervention", {**payload, **({"tag": tag} if tag else {})}, now=at)


def _row(number, topic, text, date=DAY):
    """A decision row in `maintenance/decision_log.parse` shape: no actor, no cards field. As in the
    real table, who acted is in the Topic cell's parenthetical or the text's opening, and cards are
    `t_` ids in the text. `topic` is the raw cell, `title` the cell without its trailing paren."""
    title = topic.rsplit(" (", 1)[0] if topic.endswith(")") else topic
    return {"number": number, "topic": topic, "title": title, "date": date, "text": text,
            "status": "decided"}


def _incidents(conn):
    first, _ = incidents.open_incident(
        conn, board=BOARD, kind="crash", key="tick segfault", now=T0 + 100
    )
    conn.execute("UPDATE incidents SET status='closed', closed_at=? WHERE id=?", (T0 + 200, first))
    incidents.open_incident(conn, board=BOARD, kind="crash", key="tick segfault", now=T0 + 300)
    incidents.open_incident(conn, board=BOARD, kind="crash", key="disk full", now=T0 + 400)


def _good(home, conn, ids):
    for i in range(10):
        _card(conn, f"g{i}", ids)
    for i in range(4):
        _spool(home, conn, ids, f"g{i}", "approve", f"ok g{i}", T0 + 600 + i)
    rows = [_row(800 + i, f"GO (Alex, {DAY})", f"GO {ids[f'g{i}']}") for i in range(4, 10)]
    return rows, [f"{ids[f'g{i}']} LANDED" for i in range(10)], ["capsule: g0 (" + ids["g0"] + ")"]


def _bad(home, conn, ids):
    _card(conn, "b0", ids)
    _card(conn, "b1", ids, runs=5)
    _card(conn, "b2", ids, tokens=(60000, 30000, 10000))
    _card(conn, "b3", ids)
    _card(conn, "b4", ids)
    _card(conn, "b5", ids, landed=False, outcome="failed")
    conn.execute("UPDATE tasks SET status='blocked' WHERE id=?", (ids["b5"],))
    _card(conn, "b6", ids, runs=0, landed=False)  # closed by hand: done, no completed event
    conn.execute("UPDATE tasks SET status='done', completed_at=? WHERE id=?", (T0 + 900, ids["b6"]))
    _spool(home, conn, ids, "b1", "reset", "stuck", T0 + 600)  # 1.json
    _spool(home, conn, ids, "b1", "clarify", "which?", T0 + 601)  # 2.json
    _spool(home, conn, ids, "b5", "unblock", "go", T0 + 602, archive=False)  # 3.json, still pending
    _spool(home, conn, ids, "b3", "approve", "ok", T0 + 603)  # 4.json
    _spool(home, conn, ids, "b5", "reset", "handover", T0 + 604, tag="migration-reset")  # 5.json
    _incidents(conn)
    i = ids
    rows = [
        _row(701, f"GO (Alex, {DAY})", f"GO for {i['b0']} and {i['b4']}"),
        _row(702, f"stamp wait (chief of staff, {DAY})", f"{i['b3']} awaiting-alex-stamp"),
        _row(703, f"GO (Alex + Fable, {DAY})", f"GO {i['b2']}"),  # a joint row: one act
        _row(704, DAY, f"Alex approved {i['b3']}"),  # the act of 4.json again; no topic actor
        _row(707, NEXT_DAY, f"Alex: approve {i['b1']} again", NEXT_DAY),  # opening "Alex:"
        _row(
            706,
            f"filing (chief of staff, {DAY})",
            f"Chief of staff under Alex's delegation: filed {i['b0']}, needs Alex",
        ),
    ]
    log = [f"{i[n]} LANDED" for n in ("b0", "b1", "b2", "b4")] + [
        f"worktree hook LIVE {i['b3']} completed"
    ]
    return rows, log, [f'Revert "capsule: b0 ({i["b0"]})"', f"capsule: b1 ({i['b1']})"]


def _empty(home, conn, ids):
    _card(conn, "e0", ids, landed=False, outcome="failed")
    _card(conn, "e1", ids, landed=False, outcome="failed")
    return [], [], []


def build(kind, home):
    """Create `kind` under `home`; the caller owns `home` (a temp dir)."""
    board_db = paths.board_db(BOARD, Path(home))
    db.create_board(board_db)
    conn = db.connect(board_db)
    ids = {}
    try:
        rows, log, git = {"good_board": _good, "bad_board": _bad, "empty_board": _empty}[kind](
            home, conn, ids
        )
    finally:
        conn.close()
    return Built(Path(home), BOARD, ids, rows, log, git)
