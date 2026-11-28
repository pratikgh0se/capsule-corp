"""`human stop '<trigger>'` (followups 2026-11-01, t_9ad35af5): park a doomed card once its
current run ends, never signalling the run. Oracles: the card row and `task_events` after
`interventions.apply` / `stop_queue.sweep`, and the human spool (the CLI writer)."""
import json
import tempfile
import unittest
from pathlib import Path

from capsule import (blocks, config, db, dispatch, human, interventions, lease, paths, spool,
                     stop_queue)

CFG = config.load(paths.REPO / "capsule" / "config.example.json")


class _Tick:
    """The one Tick method `sweep` uses: run the work, count an exception, go on."""
    def _guarded(self, conn, cfg, task_id, now, counts, work, run_id=None):
        try:
            work()
        except Exception:
            counts["tick_errors"] += 1


class StopVerb(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        board = paths.board_db("t", self.home)
        db.create_board(board)
        self.conn = db.connect(board)
        self.addCleanup(self.conn.close)
        self.tid = db.new_task(self.conn, title="doomed", body="b", assignee="dev-backend",
                               branch="main")

    def status(self):
        q = "SELECT status, block_kind FROM tasks WHERE id=?"
        return self.conn.execute(q, (self.tid,)).fetchone()

    def kinds(self):
        q = "SELECT kind FROM task_events WHERE task_id=? ORDER BY id"
        return [r[0] for r in self.conn.execute(q, (self.tid,))]

    def run_live(self):
        self.conn.execute("UPDATE tasks SET status='running' WHERE id=?", (self.tid,))
        return lease.start_run(self.conn, self.tid, role="implementer", claim_lock="l",
                               expires=9e9, run_id="r1", now=1)

    def sweep(self, now, counts=None):
        counts = {"tick_errors": 0} if counts is None else counts
        stop_queue.sweep(_Tick(), self.conn, CFG, now, counts)

    def events(self, kind):
        q = "SELECT payload FROM task_events WHERE task_id=? AND kind=? ORDER BY id"
        return [json.loads(r[0]) for r in self.conn.execute(q, (self.tid, kind))]

    def reset(self):
        doc = {"kind": "reset", "text": "x", "at": 21}
        interventions.apply(self.conn, {"board": "t"}, self.tid, doc, "r.json", "r.json", 21)

    def stop(self, text="doomed: wrong file set", now=10):
        doc = {"task_id": self.tid, "kind": "stop", "text": text, "at": now}
        name = f"s{now}.json"
        interventions.apply(self.conn, {"board": "t"}, self.tid, doc, name, name, now)

    def test_running_card_parks_only_after_its_run_ends(self):
        pk = self.run_live()
        self.stop()
        self.sweep(11)
        self.assertEqual(tuple(self.status()), ("running", None))   # the run is left alone
        self.assertIn("park_queued", self.kinds())
        self.conn.execute("UPDATE task_runs SET ended_at=12 WHERE id=?", (pk,))
        self.sweep(13)
        self.assertEqual(tuple(self.status()), ("blocked", "parked"))
        self.assertEqual(self.kinds().count("parked_by_human"), 1)
        self.assertTrue(self.events("parked_by_human")[0]["queued"])

    def test_a_live_run_blocks_the_park(self):
        self.run_live()
        self.stop()
        self.sweep(11)
        self.sweep(12)
        self.assertEqual(self.status()[0], "running")
        self.assertNotIn("parked_by_human", self.kinds())

    def test_a_second_stop_is_refused_and_parks_once(self):
        pk = self.run_live()
        self.stop(now=10)
        self.stop(now=11)
        self.assertEqual(self.kinds().count("park_queued"), 1)
        self.assertEqual(self.kinds().count("stop_refused"), 1)
        self.conn.execute("UPDATE task_runs SET ended_at=12 WHERE id=?", (pk,))
        self.sweep(13)
        self.sweep(14)
        self.assertEqual(self.kinds().count("parked_by_human"), 1)

    def test_a_parkable_card_parks_at_once_and_reset_cancels_a_queued_stop(self):
        self.conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (self.tid,))
        self.stop("wait for epic X")
        self.assertEqual(tuple(self.status()), ("blocked", "parked"))
        self.conn.execute("UPDATE tasks SET status='review', block_kind=NULL WHERE id=?",
                          (self.tid,))
        pk = self.run_live()
        self.stop(now=20)
        self.assertEqual(stop_queue.pending(self.conn, self.tid), "doomed: wrong file set")
        self.reset()
        self.assertIsNone(stop_queue.pending(self.conn, self.tid))
        self.conn.execute("UPDATE tasks SET status='review' WHERE id=?", (self.tid,))
        self.conn.execute("UPDATE task_runs SET ended_at=22 WHERE id=?", (pk,))
        self.sweep(23)
        self.assertEqual(self.status()[0], "review")
        self.assertEqual(self.events("stop_cancelled"), [{"reason": "reset"}])

    def test_a_refused_reset_leaves_the_stop_standing(self):
        self.run_live()
        self.stop()
        self.conn.execute("UPDATE tasks SET status='done' WHERE id=?", (self.tid,))
        self.reset()
        self.assertNotIn("stop_cancelled", self.kinds())

    def test_a_queued_card_gone_done_or_archived_cancels_the_stop(self):
        for status in ("done", "archived"):
            with self.subTest(status=status):
                tid = db.new_task(self.conn, title=status, body="b", assignee="dev-backend",
                                  branch="main")
                self.conn.execute("UPDATE tasks SET status='running' WHERE id=?", (tid,))
                doc = {"task_id": tid, "kind": "stop", "text": "t", "at": 10}
                interventions.apply(self.conn, {"board": "t"}, tid, doc, "a.json", "a.json", 10)
                self.conn.execute("UPDATE tasks SET status=? WHERE id=?", (status, tid))
                self.sweep(11)
                q = "SELECT payload FROM task_events WHERE task_id=? AND kind='stop_cancelled'"
                got = [json.loads(r[0])["reason"] for r in self.conn.execute(q, (tid,))]
                self.assertEqual(got, [f"{status}: past parking"])
                self.sweep(12)    # settled: no second event
                self.assertEqual(len(self.conn.execute(q, (tid,)).fetchall()), 1)

    def test_a_stop_on_a_done_card_is_refused_with_its_reason_and_never_parks(self):
        self.conn.execute("UPDATE tasks SET status='done' WHERE id=?", (self.tid,))
        self.stop()
        self.assertEqual(self.events("stop_refused")[0]["reason"],
                         "approved, landing: use close/revert")
        self.assertNotIn("park_refused", self.kinds())
        self.assertEqual(self.events("intervention")[-1]["outcome"],
                         "refused:approved, landing: use close/revert")

    def test_a_stop_that_queues_is_stamped_applied(self):
        self.run_live()
        self.stop()
        self.assertEqual(self.events("intervention")[-1]["outcome"], "applied")

    def test_one_failing_card_does_not_stop_the_sweep(self):
        other = db.new_task(self.conn, title="o", body="b", assignee="dev-backend", branch="main")
        for t in (self.tid, other):
            self.conn.execute("UPDATE tasks SET status='running' WHERE id=?", (t,))
            doc = {"task_id": t, "kind": "stop", "text": "t", "at": 10}
            interventions.apply(self.conn, {"board": "t"}, t, doc, f"{t}.json", f"{t}.json", 10)
        real, counts = blocks.park, {"tick_errors": 0}

        def park(conn, task_id, *a, **kw):
            if task_id == self.tid:
                raise RuntimeError("boom")
            return real(conn, task_id, *a, **kw)
        blocks.park = park
        self.addCleanup(setattr, blocks, "park", real)
        self.sweep(11, counts)
        self.assertEqual(counts["tick_errors"], 1)
        q = "SELECT status FROM tasks WHERE id=?"
        self.assertEqual(self.conn.execute(q, (other,)).fetchone()[0], "blocked")

    def test_dispatch_query_skips_a_pending_stop_only(self):
        self.run_live()
        self.stop()
        q = f"SELECT COUNT(*) FROM tasks WHERE id=? AND NOT {stop_queue.PENDING_SQL}"
        self.assertEqual(self.conn.execute(q, (self.tid,)).fetchone()[0], 0)
        self.reset()
        self.assertEqual(self.conn.execute(q, (self.tid,)).fetchone()[0], 1)

    def test_dispatch_ready_skips_a_card_with_a_live_run_and_a_pending_stop(self):
        self.conn.execute("UPDATE tasks SET branch_name=? WHERE id=?", (CFG["epics"][0], self.tid))
        self.run_live()
        self.stop()
        self.conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (self.tid,))
        def got():
            return [r["id"] for r in dispatch.ready(self.conn, CFG, host_tiers={"strong"})]
        self.assertEqual(got(), [])
        db.event(self.conn, self.tid, "stop_cancelled", {"reason": "reset"}, now=30)   # control
        self.assertEqual(got(), [self.tid])

    def test_an_empty_trigger_is_refused_and_writes_no_file(self):
        with self.assertRaises(ValueError):
            human.write(self.home, "t", self.tid, "stop", "   ")
        self.assertEqual([p for p in spool.human_dir(self.home, "t").iterdir() if p.is_file()], [])


if __name__ == "__main__":
    unittest.main()
