"""`test_tick.py` is frozen at its baseline line count, so this lives in its own file, reusing
its `TickCase`."""
from capsule import human
from capsule.tests.test_tick import TickCase


class AcceptResumesATriageCardWithNoReport(TickCase):
    def test_accept_moves_a_no_report_triage_card_to_review(self):
        self.conn.execute(
            "UPDATE tasks SET status='triage', block_kind='needs_input', block_recurrences=2 WHERE id=?",
            (self.tid,))
        # the shape a real triage-via-worktree_mismatch card has: two worktree_mismatch holds
        # never call `UPDATE task_runs SET handoff=...`, so the last implementer run's handoff
        # is NULL, not a report with status: failed.
        self.conn.execute(
            "INSERT INTO task_runs (task_id, profile, status, outcome, handoff, started_at, ended_at) VALUES"
            " (?, 'implementer', 'failed', 'failed', NULL, 10, 20)", (self.tid,))
        self.conn.commit()
        human.write(self.home, "test", self.tid, "accept", "Accepted despite the failed run.", now=2)
        self.runner.up = False
        self.tick.once()
        self.assertEqual(self.task()["status"], "review")
