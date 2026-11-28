"""`python3 -m capsule human <board> <task_id> assign <assignee>`: sets a card's
assignee so `dispatch.ready` can actually route it - the verb behind the gap where a
`ready`/`blocked`/ `triage` card with no (or an unroutable) assignee was skipped by
the dispatcher forever, silently."""
import time

from capsule import config as capsule_config
from capsule import db, dispatch, outbox, paths
from capsule.amend import _backup
from capsule.db import txn

# A held or triaged card is just as unable to ever dispatch with no routable assignee as
# a ready one - both have their own release verbs (unblock/reset/accept) that never
# touch `assignee` themselves.
ASSIGNABLE_STATUSES = ("ready", "blocked", "triage")


class AssignRefused(Exception):
    """A typed refusal (bad board, bad config, missing task, wrong status, unroutable
    assignee) - the CLI prints `str(exc)` and exits 1, never a traceback."""


def assign(capsule_home, board, task_id, assignee, *, who="operator", now=None, backup_dir=None):
    """Returns `{"event_id": ..., "backup": <path>}` on success; raises `AssignRefused` on any
    refusal, always before any write (never a partial mutation)."""
    now = int(now if now is not None else time.time())

    db_path = paths.board_db(board, capsule_home)
    if not db_path.is_file():
        raise AssignRefused(f"no such board {board!r} under {capsule_home}")

    try:
        cfg = capsule_config.load(paths.config_path(capsule_home))
    except Exception as exc:
        raise AssignRefused(f"config invalid: {exc}") from exc
    resolved = dispatch.resolve_assignee(cfg, assignee)
    if resolved is None:
        known = ", ".join(sorted(cfg.get("assignees", {}).keys())) or "<none configured>"
        raise AssignRefused(f"{assignee!r} is not a key of config assignees whose value is a"
                            f" routed role (known: {known})")

    conn = db.connect(db_path)
    try:
        row = conn.execute("SELECT status, assignee FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row is None:
            raise AssignRefused(f"no such task {task_id!r} on board {board!r}")
        if row["status"] not in ASSIGNABLE_STATUSES:
            raise AssignRefused(f"{task_id} is {row['status']}: assign only applies to"
                                f" {'/'.join(ASSIGNABLE_STATUSES)}")

        backup_path = _backup(conn, board, now, backup_dir=backup_dir)

        with txn(conn):
            # the race window: another writer could have moved the card's status between
            # the read above and this write - never guess which half landed, refuse
            # cleanly instead (amend.py's own re-check pattern).
            placeholders = ",".join("?" * len(ASSIGNABLE_STATUSES))
            cur = conn.execute(
                f"UPDATE tasks SET assignee=? WHERE id=? AND status IN ({placeholders})",
                (assignee, task_id, *ASSIGNABLE_STATUSES))
            if cur.rowcount != 1:
                raise AssignRefused(f"{task_id} changed status mid-assign: refused, retry")
            payload = {"by": who, "assignee": assignee, "old_assignee": row["assignee"],
                       "backup": str(backup_path)}
            db.event(conn, task_id, "assigned", payload, now=now)
            event_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            outbox.add(conn, "file",
                       f"capsule: {task_id} assigned to {assignee!r} by {who} | event {event_id}",
                       thread_key=task_id, now=now)
        return {"event_id": event_id, "backup": str(backup_path)}
    finally:
        conn.close()
