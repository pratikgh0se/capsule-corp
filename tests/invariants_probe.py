"""Helper for tests/test_invariants.sh: run `capsule.invariants.sweep` against a temp home and
print `<incident kinds or none> <paused|running>`. argv[1] names the planted card condition."""
import os
import sys
import tempfile
from pathlib import Path

from capsule import db, invariants

home = Path(os.environ["CAPSULE_HOME"])
board = home / "boards" / "cc" / "kanban.db"
if not board.exists():
    db.create_board(board)
conn = db.connect(board)


def sweep():
    cfg = {"board": "cc", "invariants": {"enabled": True, "dev_checkout": os.environ["DEV"]}}
    invariants.sweep(conn, cfg, now=2000, cwd=tempfile.gettempdir())


CARDS = {"lost": ("running", ("completed",)), "archived": ("archived", ("completed", "archived"))}
if sys.argv[1] in CARDS:
    sweep()                                # the baseline pass: only newer history is judged
    status, kinds = CARDS[sys.argv[1]]
    conn.execute("INSERT INTO tasks (id, title, status, created_at)"
                 " VALUES ('t_00000001', 'x', ?, 1)", (status,))
    for i, kind in enumerate(kinds):
        conn.execute("INSERT INTO task_events (task_id, kind, created_at)"
                     " VALUES ('t_00000001', ?, ?)", (kind, 2 + i))
    conn.commit()
sweep()
found = sorted(r[0] for r in conn.execute("SELECT kind FROM incidents"))
paused = db.guard_get(conn, "pause:board")[0] is not None
print(",".join(found) or "none", "paused" if paused else "running")
