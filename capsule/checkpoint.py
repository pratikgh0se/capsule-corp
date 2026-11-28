"""Checkpoint and restart, never in-context summarisation (spec §6.8, row 417)."""
import uuid

from capsule import blocks, lease
from capsule.db import event, txn


def chain_length(conn, run):
    """How many restarts stand behind `run` (0 for a card's first run)."""
    n, parent = 0, run["resumed_from"]
    while parent:
        n += 1
        row = conn.execute("SELECT resumed_from FROM task_runs WHERE run_id=?", (parent,)).fetchone()
        parent = row["resumed_from"] if row is not None else None
    return n


def on_report(conn, run, report, cfg, *, now):
    chain = chain_length(conn, run)
    task_id = run["task_id"]
    with txn(conn):
        lease.end_run(conn, run["id"], status="done", outcome="checkpoint",
                      summary=report["checkpoint"]["done_so_far"][:500], now=now)
        event(conn, task_id, "checkpoint", {"chain": chain}, run_pk=run["id"], run_id=run["run_id"], now=now)
        if chain >= cfg["caps"]["checkpoint_chain"]:
            blocks.block(conn, task_id, kind="capability", reason="checkpoint chain limit",
                         run_id=run["run_id"], now=now)
            return "blocked"
        task = conn.execute("SELECT claim_lock, claim_expires FROM tasks WHERE id=?", (task_id,)).fetchone()
        run_id = uuid.uuid4().hex
        run_pk = lease.start_run(
            conn, task_id, role=run["profile"], claim_lock=task["claim_lock"], expires=task["claim_expires"],
            run_id=run_id, now=now, runner=run["runner"], provider=run["provider"], account=run["account"],
            model=run["model"], resumed_from=run["run_id"], metadata=run["metadata"])
        conn.execute("UPDATE tasks SET current_run_id=? WHERE id=?", (run_pk, task_id))
        event(conn, task_id, "checkpoint_restart", {"resumed_from": run["run_id"], "chain": chain + 1},
              run_pk=run_pk, run_id=run_id, now=now)
    return "restarted"
