"""Runaway counts only the review rounds that did not pass the gates, and the stop keeps the
tripping round's failure text."""
import json
from unittest import mock

from capsule import blocks, epic_sync, restart, runaway
from capsule.tests.test_cost import BoardCase

TEXT = "Review failed:\n- protected path changed (row 395): capsule/lander.py"


class TrueRounds(BoardCase):
    def setUp(self):
        super().setUp()
        self.cfg = {"board": "b"}
        self.tid = self.card(status="review")

    def rounds(self, n, start=10):
        runs = []
        for _ in range(n):
            run = self.run_row(self.tid, start=start)
            self.conn.execute("UPDATE task_runs SET outcome='completed' WHERE id=?", (run["id"],))
            runs.append(run)
        return runs

    def task(self):
        return self.conn.execute("SELECT * FROM tasks WHERE id=?", (self.tid,)).fetchone()

    def check(self):
        return runaway.check(self.conn, self.cfg, self.tid)

    def pass_gates(self, impl):
        """`finish_gate_pass` for `impl` with the sync held (after_gate_pass False)."""
        with mock.patch.object(epic_sync, "after_gate_pass", return_value=False), \
                mock.patch.object(epic_sync.workspace, "commit", return_value="abc"), \
                mock.patch.object(epic_sync.workspace, "head", return_value="abc"):
            epic_sync.finish_gate_pass(mock.MagicMock(), self.conn, self.cfg, self.task(), "ws",
                                       [], impl, {}, impl["run_id"], 30, False, [], ())

    def test_a_round_that_passed_the_gates_does_not_count_toward_the_rounds_stop(self):
        runs = self.rounds(4)
        self.pass_gates(runs[1])
        self.assertIsNone(self.check())

    def test_four_rounds_with_no_gate_pass_still_trip(self):
        self.rounds(4)
        self.assertEqual(self.check()[0], "rounds")

    def test_a_gate_pass_before_a_human_reset_changes_nothing_after_it(self):
        old = self.rounds(1, start=5)[0]
        self.pass_gates(old)
        blocks.reset_failures(self.conn, self.tid, by="human", now=8)
        self.rounds(4, start=10)
        self.assertEqual(self.check()[0], "rounds")

    def test_the_tripping_rounds_failure_text_reaches_the_event_and_the_guide_packet(self):
        self.rounds(4)
        impl = self.run_row(self.tid, start=30)
        self.assertTrue(restart.stop(self.conn, self.cfg, self.task(), impl, 30, TEXT))
        ev = self.conn.execute("SELECT * FROM task_events WHERE task_id=? AND kind='runaway'",
                               (self.tid,)).fetchone()
        self.assertEqual(json.loads(ev["payload"])["failures"], TEXT)
        self.assertIn("tripping round: - protected path changed (row 395): capsule/lander.py",
                      runaway.packet(self.conn, ev)["findings"])

    def test_the_failure_text_is_cut_to_1500_characters(self):
        self.rounds(4)
        impl = self.run_row(self.tid, start=30)
        self.assertTrue(runaway.step(self.conn, self.cfg, self.task(), impl, 30, "x" * 3000))
        ev = self.conn.execute("SELECT payload FROM task_events WHERE kind='runaway'").fetchone()
        self.assertEqual(len(json.loads(ev[0])["failures"]), 1500)

    def test_gate_passes_never_take_the_count_below_zero_or_count_twice(self):
        run = self.rounds(1)[0]
        self.pass_gates(run)
        self.pass_gates(run)
        self.assertEqual(runaway._true_rounds(self.conn, self.tid, 0), 0)
