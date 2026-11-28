#!/usr/bin/env python3
"""Read-only SELECT over one Capsule board's kanban.db (row 633), for the agent-review job.

`board_query.py <board> "<select ...>"` replaces the old `Bash(sqlite3 -readonly *)` allow rule,
which was a full escape (`.system`, `.output`, `writefile()`, ATTACH). Here the board is a NAME
(`paths.board_name_problem`, never a path), the database is opened `mode=ro`, the statement must
be one SELECT, and a sqlite authorizer denies everything but SELECT, column reads and an
allow-list of plain functions (no ATTACH, PRAGMA, writes, load_extension, readfile/writefile).
Prints a TSV header and at most ROW_CAP rows. The statement is at most MAX_SQL characters and a
progress handler aborts it after BUDGET_S seconds. Exit 0 ok, 2 refused or unreadable. stdlib only.
"""
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from capsule import paths  # noqa: E402

ROW_CAP = 500
MAX_SQL = 4000      # characters
BUDGET_S = 2        # seconds of query time before the progress handler aborts it
FUNCTIONS = {"count", "sum", "total", "min", "max", "avg", "abs", "round", "length", "lower",
             "upper", "substr", "trim", "instr", "replace", "coalesce", "ifnull", "nullif",
             "typeof", "date", "time", "datetime", "julianday", "strftime", "json_extract",
             "group_concat", "like", "glob"}
OK_ACTIONS = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ}


def _authorizer(action, _arg1, name, _db, _trigger):
    if action in OK_ACTIONS:
        return sqlite3.SQLITE_OK
    if action == sqlite3.SQLITE_FUNCTION and str(name).lower() in FUNCTIONS:
        return sqlite3.SQLITE_OK
    return sqlite3.SQLITE_DENY


def _open(db, deadline):
    """The board db read-only, fenced by the authorizer and aborted past `deadline`."""
    conn = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True)
    conn.execute("PRAGMA query_only = 1")
    conn.set_authorizer(_authorizer)
    # a nonzero return aborts the running statement (a cross join cannot hog the host)
    conn.set_progress_handler(lambda: time.monotonic() > deadline, 10000)
    return conn


def _refuse(msg):
    sys.stderr.write(f"board_query: {msg}\n")
    sys.exit(2)


def main(argv):
    if len(argv) != 2:
        _refuse('usage: board_query.py <board> "<select ...>"')
    board, sql = argv
    problem = paths.board_name_problem(board)
    if problem:
        _refuse(problem)
    if not sql.lstrip().lower().startswith("select"):
        _refuse("the statement must be a SELECT")
    if len(sql) > MAX_SQL:
        _refuse(f"the statement is longer than {MAX_SQL} characters")
    db = paths.board_db(board)
    deadline = time.monotonic() + BUDGET_S
    try:
        cur = _open(db, deadline).execute(sql)     # a second statement raises
        rows = cur.fetchmany(ROW_CAP + 1)
    except (sqlite3.Error, OSError) as exc:
        if time.monotonic() > deadline:
            _refuse(f"{board}: query aborted after the {BUDGET_S} s budget")
        _refuse(f"{board}: {exc}")
    print("\t".join(d[0] for d in cur.description))
    for row in rows[:ROW_CAP]:
        print("\t".join("" if v is None else str(v).replace("\t", " ").replace("\n", " ")
                        for v in row))
    if len(rows) > ROW_CAP:
        print(f"... truncated at {ROW_CAP} rows")


if __name__ == "__main__":
    main(sys.argv[1:])
