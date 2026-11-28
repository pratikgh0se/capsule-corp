"""The shared tail of a contradiction's routing: the status move to the guide's triage, its
ruling request and the `contradiction` event commit in one transaction, so a
failed `spool.apply` rolls back the status move with the event."""
from capsule import intent, outbox, spool
from capsule.db import event, transition, txn



def _existing_ruling(conn, task_id, run_id):
    """A `guide_ruling` child this exact run already filed (`run_id` is unique per run, so a
    body match can only be the leftover doc of this run's own earlier, rolled-back attempt)."""
    row = conn.execute(
        "SELECT t.id FROM tasks t JOIN task_links l ON l.child_id = t.id"
        " WHERE l.parent_id=? AND t.request_needs='role:guide' AND t.body LIKE ?"
        " ORDER BY t.created_at DESC, t.rowid DESC LIMIT 1",
        (task_id, f"%run {run_id} %")).fetchone()
    return row["id"] if row else None


def _file_ruling_request(conn, sp, schemas, now, task_id, role, run, what, text=None):
    """Writes and applies the guide_ruling request doc, and returns the new child's id. A
    `text` of its own must hold `run <run id> ` (the doc this exact run filed is found by it)."""
    lock = run["claim_lock"] or run["run_id"]
    apply_run = run if run["claim_lock"] else dict(run, claim_lock=lock)
    text = text or (f"guide_ruling: {task_id} {role} run {run['run_id']} passed its schema but"
                    f" contradicts intent example(s) {what}. Precedence: {intent.PRECEDENCE}."
                    " The guide rules: document-wrong, example-wrong (a change to the intent"
                    " file through the row 393 gate) or ask-owner.")
    def apply():
        spool.apply(conn, sp, apply_run, role_verbs={"request"}, schemas=schemas, now=now,
                    settle=None)
    apply()
    existing = _existing_ruling(conn, task_id, run["run_id"])
    if existing:
        return existing
    spool.write_doc(sp, "requests", {"schema": "request.v1", "run_id": run["run_id"],
                                     "task_id": task_id, "claim_lock": lock,
                                     "verb": "request", "needs": "role:guide",
                                     "text": text[:4000]})
    apply()
    return _existing_ruling(conn, task_id, run["run_id"])


def route_contradiction(conn, task_id, role, found, run, sp, schemas, now):
    """Moves the card to the guide's triage and files its ruling request, with the
    `contradiction` event, in one transaction."""
    what = "; ".join(f"{f['id']} ({f['source']}): {', '.join(f['differs'])}" for f in found)
    ruling = None
    with txn(conn):
        moved = transition(conn, task_id, "triage", frm=("running", "review"), claim=True,
                           block_kind="guide_ruling")
        if moved:
            ruling = _file_ruling_request(conn, sp, schemas, now, task_id, role, run, what)
        event(conn, task_id, "contradiction",
              {"role": role, "examples": found, "precedence": intent.PRECEDENCE,
               "ruling_card": ruling, "routed": "guide" if moved else None},
              run_pk=run["id"], run_id=run["run_id"], now=now)
        if moved:
            outbox.notify(
                conn, "running",
                f"{task_id} contradiction ({role}): routed to the guide for a ruling",
                fields=[("request card", ruling)], thread_key=task_id,
                file_body=(
                    f"capsule: {task_id} contradiction ({role} run {run['run_id'][:8]}):"
                    f" {what} | routed to the guide for a ruling, request card {ruling}"
                    f" ({intent.PRECEDENCE})")[:1500], now=now)
