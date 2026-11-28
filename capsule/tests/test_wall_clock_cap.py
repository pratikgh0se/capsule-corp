"""inc_b67d8566: the wall-clock cap counts from the real spawn (capsule/wall_clock.py)."""
import json
import unittest

from capsule.tests import test_tick
from capsule.tests.test_tick import IMPL_DONE


class WallClockCap(test_tick.WallClockCap):
    def test_a_review_launched_after_a_slow_validator_counts_from_its_own_spawn(self):
        """The validator's contract suite runs inside the pass; the reviewer launched after it
        counts its cap from its own spawn, not from the pass start."""
        clock, slow = [self.now], 4000
        self.live = ("reviewer",)
        self.cfg["caps"]["review_seconds"] = 1800
        self.write_cfg()
        self.tick = self.make_tick(spawn=self.spawn_live, alive=lambda pid: pid not in self.killed,
                                   clock=lambda: clock[0])
        self.tick._kill = self.killed.append
        runner_exec = self.runner.exec

        def slow_exec(name, cmd, timeout):
            clock[0] = self.now + slow
            return runner_exec(name, cmd, timeout)
        self.runner.exec = slow_exec
        self.script["implementer"] = [[IMPL_DONE]]
        self.tick.once(self.now)
        self.tick.once(self.now)                     # the review round starts
        [rev] = self.runs("reviewer")
        job = json.loads((self.spool_dir(rev["run_id"]) / "job.json").read_text())
        self.assertGreaterEqual(job["launched_at"], self.now + slow)
        self.assertEqual(self.tick.once(self.now + slow + 300)["timed_out"], 0)
        self.live = ()
        self.assertEqual(self.tick.once(self.now + slow + 1801)["timed_out"], 1)

    def test_a_stale_launched_at_never_outruns_the_journal_start(self):
        """A stale launched_at (job.json from before this fix): the journal start wins."""
        self.set_task_cap(100)
        self.tick.once(self.now)
        sp = self.spool_dir(self.runs("implementer")[0]["run_id"])
        line = {"seq": 1, "step": "start", "at": self.now + 500}
        (sp / "journal.jsonl").write_text(json.dumps(line) + "\n")
        self.assertEqual(self.tick.once(self.now + 550)["timed_out"], 0)
        self.runner.up = False
        self.assertEqual(self.tick.once(self.now + 601)["timed_out"], 1)


if __name__ == "__main__":
    unittest.main()
