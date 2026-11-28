import json
import socket
import subprocess
import tempfile
import unittest
from pathlib import Path

from capsule import db, lease
from capsule.tests.legacy_oracle import legacy, needs_legacy

HOST = socket.gethostname()


def board(tc):
    p = Path(tempfile.mkdtemp()) / "kanban.db"
    db.create_board(p)
    conn = db.connect(p)
    tc.addCleanup(conn.close)   # R28: close every connection this helper opens (ResourceWarning)
    return conn


def kinds(conn, tid):
    return [r[0] for r in conn.execute("SELECT kind FROM task_events WHERE task_id=? ORDER BY id", (tid,))]


def dead_pid():
    p = subprocess.Popen(["true"])
    p.wait()
    return p.pid


class Claiming(unittest.TestCase):
    def test_claim_sets_running_lock_and_run(self):
        conn = board(self)
        tid = db.new_task(conn, title="t", assignee="dev-frontend", now=1)
        c = lease.claim(conn, tid, ttl=900, role="dev-frontend", now=100)
        self.assertEqual(c.claim_lock, f"{HOST}:{c.run_id}")
        self.assertRegex(c.run_id, r"^[0-9a-f]{32}$")
        t = conn.execute("SELECT status, claim_lock, claim_expires, current_run_id FROM tasks").fetchone()
        self.assertEqual(tuple(t), ("running", c.claim_lock, 1000, c.run_pk))
        run = conn.execute("SELECT profile, status, run_id, claim_lock FROM task_runs").fetchone()
        self.assertEqual(tuple(run), ("dev-frontend", "running", c.run_id, c.claim_lock))
        self.assertEqual(kinds(conn, tid), ["created", "claimed"])

    def test_lost_race_returns_none_and_writes_nothing(self):
        conn = board(self)
        tid = db.new_task(conn, title="t", now=1)
        self.assertIsNotNone(lease.claim(conn, tid, ttl=900, role="r", now=100))
        self.assertIsNone(lease.claim(conn, tid, ttl=900, role="r", now=101))
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM task_runs").fetchone()[0], 1)

    def test_undone_parent_demotes_to_todo(self):
        conn = board(self)
        parent = db.new_task(conn, title="p", now=1)
        child = db.new_task(conn, title="c", parents=[parent], now=2)
        self.assertIsNone(lease.claim(conn, child, ttl=900, role="r", now=100))
        self.assertEqual(conn.execute("SELECT status FROM tasks WHERE id=?", (child,)).fetchone()[0], "todo")
        self.assertEqual(kinds(conn, child)[-1], "claim_rejected")

    def test_heartbeat_needs_the_live_lock(self):
        conn = board(self)
        tid = db.new_task(conn, title="t", now=1)
        c = lease.claim(conn, tid, ttl=900, role="r", now=100)
        self.assertFalse(lease.heartbeat(conn, tid, "other:lock", ttl=900, now=200))
        self.assertTrue(lease.heartbeat(conn, tid, c.claim_lock, ttl=900, now=200))
        row = conn.execute("SELECT claim_expires, last_heartbeat_at FROM tasks").fetchone()
        self.assertEqual(tuple(row), (1100, 200))


class StaleRelease(unittest.TestCase):
    def setUp(self):
        self.conn = board(self)
        self.tid = db.new_task(self.conn, title="t", now=1)
        self.c = lease.claim(self.conn, self.tid, ttl=10, role="r", now=100)
        self.conn.execute("UPDATE tasks SET worker_pid=4242, last_heartbeat_at=100")

    def test_live_pid_with_fresh_heartbeat_is_extended(self):
        n = lease.release_stale(self.conn, alive=lambda pid: True, kill=self.fail, ttl=10, now=200)
        self.assertEqual(n, 0)
        self.assertEqual(tuple(self.conn.execute("SELECT status, claim_expires FROM tasks").fetchone()), ("running", 210))
        self.assertEqual(kinds(self.conn, self.tid)[-1], "claim_extended")

    def test_dead_pid_is_reclaimed_to_ready(self):
        n = lease.release_stale(self.conn, alive=lambda pid: False, kill=lambda pid, run_id=None: None, ttl=10, now=200)
        self.assertEqual(n, 1)
        t = self.conn.execute("SELECT status, claim_lock, worker_pid, current_run_id FROM tasks").fetchone()
        self.assertEqual(tuple(t), ("ready", None, None, None))
        run = self.conn.execute("SELECT status, outcome, ended_at FROM task_runs").fetchone()
        self.assertEqual(tuple(run), ("reclaimed", "reclaimed", 200))
        self.assertEqual(kinds(self.conn, self.tid)[-1], "reclaimed")
        payload = json.loads(self.conn.execute("SELECT payload FROM task_events WHERE kind='reclaimed'").fetchone()[0])
        self.assertEqual(payload["run_id"], self.c.run_id)          # R25

    def test_stale_heartbeat_kills_then_reclaims(self):
        killed, state = [], {"alive": True}

        def kill(pid, run_id=None):
            killed.append(pid)
            state["alive"] = False
        n = lease.release_stale(self.conn, alive=lambda pid: state["alive"], kill=kill, ttl=10, now=100 + 3601)
        self.assertEqual((n, killed), (1, [4242]))

    def test_survivor_is_deferred(self):
        n = lease.release_stale(self.conn, alive=lambda pid: True, kill=lambda pid, run_id=None: None, ttl=10, now=100 + 3601)
        self.assertEqual(n, 0)
        self.assertEqual(self.conn.execute("SELECT claim_expires FROM tasks").fetchone()[0], 100 + 3601 + 120)
        self.assertEqual(kinds(self.conn, self.tid)[-1], "reclaim_deferred")


class LegacyOracle(unittest.TestCase):
    """Same scenario, both implementations, compare the resulting rows (spec §12.1)."""

    def scenario(self, script):
        home = Path(tempfile.mkdtemp())
        p = home / "h.db"
        head = f"from pathlib import Path\nconn = kb.connect(Path({str(p)!r}))\n"
        return legacy(head + script, home)

    @needs_legacy
    def test_undone_parent_matches(self):
        theirs = self.scenario(
            "p = kb.create_task(conn, title='p')\n"
            "c = kb.create_task(conn, title='c', parents=[p])\n"
            "conn.execute(\"UPDATE tasks SET status='ready' WHERE id=?\", (c,))\n"
            "r = kb.claim_task(conn, c, claimer='h:1')\n"
            "st = conn.execute('SELECT status FROM tasks WHERE id=?', (c,)).fetchone()[0]\n"
            "ev = conn.execute('SELECT kind FROM task_events WHERE task_id=? ORDER BY id DESC LIMIT 1', (c,)).fetchone()[0]\n"
            "print(json.dumps([r is None, st, ev]))")
        conn = board(self)
        parent = db.new_task(conn, title="p", now=1)
        child = db.new_task(conn, title="c", parents=[parent], now=2)
        ours = [lease.claim(conn, child, ttl=900, role="r") is None,
                conn.execute("SELECT status FROM tasks WHERE id=?", (child,)).fetchone()[0],
                kinds(conn, child)[-1]]
        self.assertEqual(ours, theirs)

    @needs_legacy
    def test_dead_pid_reclaim_matches(self):
        pid = dead_pid()
        theirs = self.scenario(
            "import socket\n"
            "t = kb.create_task(conn, title='t')\n"
            f"kb.claim_task(conn, t, ttl_seconds=1, claimer=socket.gethostname() + ':1')\n"
            f"conn.execute('UPDATE tasks SET worker_pid={pid}, claim_expires=1 WHERE id=?', (t,))\n"
            "n = kb.release_stale_claims(conn)\n"
            "row = conn.execute('SELECT status, claim_lock FROM tasks WHERE id=?', (t,)).fetchone()\n"
            "run = conn.execute('SELECT outcome FROM task_runs WHERE task_id=?', (t,)).fetchone()[0]\n"
            "print(json.dumps([n, row[0], row[1], run]))")
        conn = board(self)
        tid = db.new_task(conn, title="t", now=1)
        lease.claim(conn, tid, ttl=1, role="r", now=1)
        conn.execute("UPDATE tasks SET worker_pid=?", (pid,))
        n = lease.release_stale(conn, alive=lambda p: False, kill=lambda p, run_id=None: None, ttl=1, now=100)
        row = conn.execute("SELECT status, claim_lock FROM tasks").fetchone()
        run = conn.execute("SELECT outcome FROM task_runs").fetchone()[0]
        self.assertEqual([n, row[0], row[1], run], theirs)
