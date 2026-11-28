"""Refuse `park` and `stop` on a clone-dirty holder, and `unblock` and `accept` on a parked
card, before `capsule human` writes a file (inc_477c1bb5).
The `unblock` and `accept` refusal is repeated in capsule/blocks.py. The clone-dirty park
check lives here only: `blocks.park` has no such refusal, so other park callers bypass it."""
import contextlib
import sys

from capsule import blocks, db, dispatch, epic_clean, paths, preview

VERBS = ("unblock", "accept")


def check(capsule_home, args):
    """Print a refusal naming `reset` and return 1, or return 0 when the file may be written.

    A missing database passes, and so does a missing card except for `archive`, which names it
    (`blocks.archive` decides again): the tick stays the authority. The read is read-only."""
    if args.kind == "preview":
        return preview.preflight(capsule_home, args)
    path = paths.board_db(args.board, capsule_home)
    if args.kind not in VERBS + ("archive", "park", "stop") or not path.is_file():
        return 0
    with contextlib.closing(db.connect_ro(path)) as conn:
        if args.kind in ("park", "stop"):
            return _park(conn, args, capsule_home)
        row = conn.execute("SELECT status, block_kind, claim_lock FROM tasks WHERE id=?",
                           (args.task_id,)).fetchone()
        blocker = blocks.archive_blocker(conn, args.task_id) if args.kind == "archive" else None
    if args.kind == "archive":
        return _archive(args, row, blocker)
    if row is None or (row["status"], row["block_kind"]) != ("blocked", "parked"):
        return 0
    print(f"capsule human: {args.task_id} is parked; `{args.kind}` cannot lift a park. "
          f"Use `reset` instead.", file=sys.stderr)
    return 1


def _archive(args, row, blocker):
    """`archive` on a card that does not exist, holds a live claim, is running or in review,
    has unfinished children or is an open incident's fix card: refused here with the
    reason, so no file is written (`blocks.archive` decides it again)."""
    live = ("running", "review", "claimed")
    why = ("no such task" if row is None else "a live claim holds it" if row["claim_lock"]
           else f"{row['status']}: cannot archive" if row["status"] in live else blocker)
    if why is None:
        return 0
    print(f"capsule human: {args.task_id}: {why}; nothing was changed", file=sys.stderr)
    return 1


def _park(conn, args, capsule_home):
    """`park` or `stop` on the holder of a dirty epic clone: refused when ready cards wait on it,
    since a parked card never runs and the lane would deadlock. It asks `dispatch.clone_dirty`, the
    dispatcher's own test, for each other ready card on the branch."""
    row = conn.execute("SELECT branch_name, status FROM tasks WHERE id=?",
                       (args.task_id,)).fetchone()
    if (row is None or row["status"] not in blocks.REASSIGNABLE_STATUSES
            or args.task_id not in epic_clean.holders(conn, row["branch_name"])):
        return 0
    work_root = dispatch.root(args.board, capsule_home)
    waiting = [r["id"] for r in conn.execute(
        "SELECT id FROM tasks WHERE branch_name=? AND status='ready' AND id!=? ORDER BY id",
        (row["branch_name"], args.task_id)).fetchall()
        if dispatch.clone_dirty(conn, row["branch_name"], r["id"], work_root)
        in (args.task_id, None)]
    if not waiting:
        return 0
    print(f"capsule human: refused: {args.task_id} holds the dirty epic clone that ready cards "
          f"{', '.join(waiting)} wait on; parking it deadlocks the lane. Reset it (it then "
          f"runs first), or archive it. Nothing was changed.", file=sys.stderr)
    return 1
