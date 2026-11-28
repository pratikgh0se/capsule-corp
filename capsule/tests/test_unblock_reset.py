"""Row 511 (t_8e00b598): a human `unblock` resets the block-loop breaker's `block_recurrences`/
`block_kind`, so a legitimate re-raise blocks again rather than parking in triage - moved out of
`test_tick.py` (row 509/Z7: file-size baseline) into its own file; shares `TickCase` with it, same
as `test_tick_metrics.py` does."""
from capsule import blocks, human
from capsule.tests.test_tick import IMPL_DONE, TickCase


class UnblockReset(TickCase):
    def test_a_needs_input_re_raise_after_a_human_unblock_blocks_not_triage(self):
        """Row 511 (t_8e00b598, incident 2): a human `unblock` zeroes block_recurrences, so the
        next legitimate needs_input hold is a fresh DECISION (`blocked`), not an instant
        `block_loop_detected` to triage - the bug this replaces used to park it at recur=2."""
        held = dict(IMPL_DONE, left_undone=["golden rows stay deferred"])
        self.script["implementer"] = [[held], [held]]
        self.tick.once()
        self.tick.once()
        human.write(self.home, "test", self.tid, "unblock", "go on", now=1)
        self.tick.once()
        self.tick.once()
        row = self.task()
        self.assertEqual((row["status"], row["block_kind"], row["block_recurrences"]), ("blocked", "needs_input", 1))
        human.write(self.home, "test", self.tid, "accept", "Accepted: rows stay deferred.", now=2)
        self.runner.up = False
        self.tick.once()
        self.assertEqual(self.task()["status"], "review")

    def test_accept_takes_a_needs_input_triage_card_reached_via_a_worker_unblock(self):
        """R5 (row 447): a needs_input hold that loops to triage is still accept-able, not just
        `blocked` - row 511 only stopped a *human* release from feeding the loop counter, so
        triage is still reachable (a worker `unblock`, or accept -> review fail -> rerun)."""
        held = dict(IMPL_DONE, left_undone=["golden rows stay deferred"])
        self.script["implementer"] = [[held], [held]]
        self.tick.once()
        self.tick.once()
        blocks.unblock(self.conn, self.tid, by="worker")
        self.tick.once()
        self.tick.once()
        row = self.task()
        self.assertEqual((row["status"], row["block_kind"], row["block_recurrences"]), ("triage", "needs_input", 2))
        human.write(self.home, "test", self.tid, "accept", "Accepted: rows stay deferred.", now=2)
        self.runner.up = False
        self.tick.once()
        self.assertEqual((self.task()["status"], self.task()["block_recurrences"]), ("review", 0))   # B1
        self.assertEqual(len(self.runs("implementer")), 2)        # no rerun
