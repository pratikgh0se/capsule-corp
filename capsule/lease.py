"""Claim, lease and heartbeat (spec §6.1 leases with fencing)."""
import socket
import time
import uuid
from collections import namedtuple

from capsule.db import event, open_parent, transition, txn

HEARTBEAT_MAX_STALE = 3600   # kanban_db.py:377
RECLAIM_DEFER_GRACE = 120    # kanban_db.py:387

Claim = namedtuple("Claim", "task_id run_pk run_id claim_lock expires")


def start_run(conn, task_id, *, role, claim_lock, expires, run_id, now, runner=None, provider=None,
              account=None, model=None, resumed_from=None, metadata=None):
    cur = conn.execute(
        "INSERT INTO task_runs (task_id, profile, status, claim_lock, claim_expires, started_at, run_id,"
        " runner, provider, account, model, resumed_from, metadata) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (task_id, role, "running", claim_lock, expires, now, run_id, runner, provider, account, model,
         resumed_from, metadata))
    return cur.lastrowid


def pinned_start_sha(conn, task_id):
    """The branch head before the card's first implementer run that recorded one, or None: a run
    row with no `start_sha` in its metadata pins nothing and is skipped. The newest run with
    `repin` true (a re-pin to the origin tip, `Tick._claim_one`) wins over the first pin."""
    query = ("SELECT json_extract(metadata,'$.start_sha') FROM task_runs WHERE task_id=?"
             " AND profile='implementer' AND json_extract(metadata,'$.start_sha') IS NOT NULL")
    row = (conn.execute(query + " AND json_extract(metadata,'$.repin') ORDER BY id DESC LIMIT 1",
                        (task_id,)).fetchone()
           or conn.execute(query + " ORDER BY id LIMIT 1", (task_id,)).fetchone())
    return row[0] if row else None


def claim(conn, task_id, *, ttl, role, host=None, now=None, run_id=None, runner=None, provider=None,
          account=None, model=None, metadata=None, tier=...):
    """`tier` (when given: `tasks.tier` as the caller read it): the claim is refused (None, the
    card stays `ready`) when an operator changed the tier since, so no run starts on a stale one."""
    now = int(now or time.time())
    run_id = run_id or uuid.uuid4().hex
    lock = f"{host or socket.gethostname()}:{run_id}"
    with txn(conn):
        if tier is not ... and not conn.execute("SELECT 1 FROM tasks WHERE id=? AND tier IS ?",
                                                (task_id, tier)).fetchone():
            return None
        if open_parent(conn, task_id) is not None:
            transition(conn, task_id, "todo", frm=("ready",))
            event(conn, task_id, "claim_rejected", {"reason": "parents_not_done"}, now=now)
            return None
        leaked = conn.execute("SELECT current_run_id FROM tasks WHERE id=? AND status='ready'",
                              (task_id,)).fetchone()
        if leaked and leaked[0] is not None:
            conn.execute(
                "UPDATE task_runs SET status='reclaimed', outcome='reclaimed',"
                " summary=COALESCE(summary, 'invariant recovery on re-claim'), ended_at=?,"
                " claim_lock=NULL, claim_expires=NULL, worker_pid=NULL WHERE id=? AND ended_at IS NULL",
                (now, leaked[0]))
        if not transition(conn, task_id, "running", frm=("ready",), where=" AND claim_lock IS NULL",
                          claim_lock=lock, claim_expires=now + ttl):
            return None
        run_pk = start_run(conn, task_id, role=role, claim_lock=lock, expires=now + ttl, run_id=run_id,
                           now=now, runner=runner, provider=provider, account=account, model=model,
                           metadata=metadata)
        conn.execute("UPDATE tasks SET current_run_id=?, started_at=COALESCE(started_at, ?)"
                     " WHERE id=?", (run_pk, now, task_id))
        event(conn, task_id, "claimed", {"lock": lock, "expires": now + ttl}, run_pk=run_pk, run_id=run_id, now=now)
    return Claim(task_id, run_pk, run_id, lock, now + ttl)


def heartbeat(conn, task_id, claim_lock, *, ttl, now=None):
    now = int(now or time.time())
    with txn(conn):
        cur = conn.execute(
            "UPDATE tasks SET claim_expires=?, last_heartbeat_at=? WHERE id=? AND status='running' AND claim_lock=?",
            (now + ttl, now, task_id, claim_lock))
        if cur.rowcount != 1:
            return False
        conn.execute(
            "UPDATE task_runs SET claim_expires=?, last_heartbeat_at=? WHERE id=(SELECT current_run_id FROM tasks WHERE id=?)",
            (now + ttl, now, task_id))
    return True


def end_run(conn, run_pk, *, status, outcome, summary=None, error=None, now=None):
    now = int(now or time.time())
    with txn(conn):
        conn.execute(
            "UPDATE task_runs SET status=?, outcome=?, summary=?, error=?, ended_at=?, claim_lock=NULL,"
            " claim_expires=NULL, worker_pid=NULL WHERE id=? AND ended_at IS NULL",
            (status, outcome, summary, error, now, run_pk))
        conn.execute("UPDATE tasks SET current_run_id=NULL WHERE current_run_id=?", (run_pk,))


def release_stale(conn, *, alive, kill, ttl, host=None, now=None):
    """Expired claims: extend for a live local worker with a fresh heartbeat, else kill and reclaim."""
    host, now = host or socket.gethostname(), int(now or time.time())
    rows = conn.execute(
        "SELECT id, claim_lock, worker_pid, last_heartbeat_at, current_run_id,"
        " (SELECT run_id FROM task_runs WHERE task_runs.id = tasks.current_run_id) AS run_uuid FROM tasks"
        " WHERE status='running' AND claim_expires IS NOT NULL AND claim_expires < ?", (now,)).fetchall()
    released = 0
    for r in rows:
        lock, pid, hb = r["claim_lock"], r["worker_pid"], r["last_heartbeat_at"]
        local = (lock or "").startswith(host + ":")
        stale = hb is not None and now - hb > HEARTBEAT_MAX_STALE
        if local and pid and alive(pid) and not stale:
            with txn(conn):
                cur = conn.execute(
                    "UPDATE tasks SET claim_expires=? WHERE id=? AND status='running' AND claim_lock IS ? AND claim_expires < ?",
                    (now + ttl, r["id"], lock, now))
                if cur.rowcount == 1:
                    event(conn, r["id"], "claim_extended", {"reason": "pid_alive", "worker_pid": pid},
                          run_pk=r["current_run_id"], run_id=r["run_uuid"], now=now)
            continue
        if local and pid and alive(pid):
            kill(pid, lock[len(host) + 1:])   # lock is "<host>:<run_id>" (local, checked above)
            if alive(pid):
                with txn(conn):
                    conn.execute("UPDATE tasks SET claim_expires=? WHERE id=? AND status='running' AND claim_lock IS ?",
                                 (now + RECLAIM_DEFER_GRACE, r["id"], lock))
                    event(conn, r["id"], "reclaim_deferred", {"reason": "ttl_expired_worker_alive", "worker_pid": pid},
                          run_pk=r["current_run_id"], run_id=r["run_uuid"], now=now)
                continue
        with txn(conn):
            if not transition(conn, r["id"], "ready", frm=("running",), claim=True,
                              where=" AND claim_lock IS ? AND claim_expires < ?",
                              params=(lock, now)):
                continue
            if r["current_run_id"] is not None:
                end_run(conn, r["current_run_id"], status="reclaimed", outcome="reclaimed",
                        error=f"stale_lock={lock}", now=now)
            event(conn, r["id"], "reclaimed", {"stale_lock": lock, "worker_pid": pid, "heartbeat_stale": stale},
                  run_pk=r["current_run_id"], run_id=r["run_uuid"], now=now)
            released += 1
    return released
