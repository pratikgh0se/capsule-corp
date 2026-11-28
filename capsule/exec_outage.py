"""inc_0221ae04: a docker daemon lost while a strong-tier worker ran. The exec tool
(`runners/exec_mcp.py`) writes `<spool>/exec_outage.json` when a failed command met a gone
container and a daemon that did not answer. `hold` blocks a held report on such a run as
`transient` with cause `exec_outage`, not as `needs_input`: no person has anything to answer.
`release` lifts those blocks with `unblock(by="worker")` on the first pass with docker up; that
call keeps `block_recurrences`, so a second outage on one card goes to triage."""
from capsule import blocks
from capsule.runners.exec_mcp import OUTAGE_MARKER

CAUSE = "exec_outage"


def hold(conn, task_id, run, spool, judged, now):
    """Block a held report: `transient` when the run's spool holds the marker, else `needs_input`
    with the report's own reason text."""
    held = "; ".join(judged["left_undone"] + judged["issues"])
    reason = f"left undone / issues: {held}"[:2000]
    lost = (spool / OUTAGE_MARKER).is_file()
    blocks.block(conn, task_id, kind="transient" if lost else "needs_input", reason=reason,
                 cause=CAUSE if lost else None, run_pk=run["id"], run_id=run["run_id"], now=now)


def release(conn, strong_up, now):
    """On a pass with docker up, unblock each card whose newest hold is an exec outage."""
    if not strong_up:
        return []
    cards = [r[0] for r in conn.execute(
        "SELECT id FROM tasks WHERE status='blocked' AND block_kind='transient'")]
    return [tid for tid in cards if held_for_outage(conn, tid)
            and blocks.unblock(conn, tid, by="worker", now=now)]


def held_for_outage(conn, task_id):
    """True when the card's newest hold event carries the exec-outage cause."""
    row = conn.execute("SELECT json_extract(payload, '$.cause') FROM task_events WHERE task_id=?"
                       " AND kind IN ('blocked', 'block_loop_detected') ORDER BY id DESC LIMIT 1",
                       (task_id,)).fetchone()
    return bool(row) and row[0] == CAUSE
