"""The implementer's own `notes` field (row 509 Zenkai Z7: new logic
goes into new modules)."""
from capsule.db import event, txn


def note(conn, task_id, run, report, now):
    """One `notes` event for `report`'s own `notes` field - a no-op when it carries none."""
    notes = (report.get("notes") or []) + ["host-only: " + x for x in report.get("host_only") or []]
    if notes:
        with txn(conn):
            event(conn, task_id, "notes", {"entries": notes}, run_pk=run["id"], run_id=run["run_id"], now=now)
