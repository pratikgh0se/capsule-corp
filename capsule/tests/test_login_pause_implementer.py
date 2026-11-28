# mutation-check: targets-predate-range 4d253485 -- tick.py:678 pause route ::
#   test_an_implementer_run_that_lost_its_login_one_failure_below_the_cap_stays_out_of_triage,
#   LostLoginImplementer.test_without_the_listed_line_the_same_run_goes_to_triage
"""An implementer run that lost its login stays out of triage (t_8385cab1, inc_1c95ef4d). Z1
intent: the CLI refuses an implementer run with its own `Not logged in` line while the card is one
failure below the cap; the run is released, the card goes back to ready, and no failure, crash,
failure limit or incident follows. The contrast test removes the listed line: the same run then
costs the last failure and the card goes to triage."""
import time
import unittest.mock
from pathlib import Path

from capsule import incidents, restart
from capsule.models import claude
from capsule.tests.test_korin import KorinCase
from capsule.tests.test_tick import IMPL_DONE

FX = Path(__file__).parent / "fixtures" / "claude" / "not_logged_in.json"


class LostLoginImplementer(KorinCase):
    def kinds(self):
        sql = "SELECT kind FROM task_events WHERE task_id=? ORDER BY id"
        return [r[0] for r in self.conn.execute(sql, (self.tid,))]

    def lose_login(self, now):
        """Run the implementer with the refusal, one failure below the cap; the task row after."""
        cap = restart.failure_cap(self.cfg)
        sql = "UPDATE tasks SET failure_count=?, last_failure_error='earlier' WHERE id=?"
        self.conn.execute(sql, (cap - 1, self.tid))
        self.conn.commit()
        self.script["implementer"] = [[claude.parse(FX.read_text(), "", 1, now=now)], [IMPL_DONE]]
        self.tick.once(now)
        self.tick.once(now + 1)                              # the run closes
        return cap, self.task()

    def test_an_implementer_run_that_lost_its_login_one_failure_below_the_cap_stays_out_of_triage(
            self):
        cap, task = self.lose_login(int(time.time()))
        self.assertEqual((task["failure_count"], task["last_failure_error"]), (cap - 1, "earlier"))
        self.assertEqual(task["status"], "ready")
        for kind in ("crashed", "failed", "failure_limit"):
            self.assertNotIn(kind, self.kinds())
        self.assertEqual(incidents.listing(self.conn), [])

    def test_without_the_listed_line_the_same_run_goes_to_triage(self):
        with unittest.mock.patch.object(claude, "AUTH_LINES", ()):
            cap, task = self.lose_login(int(time.time()))
        self.assertEqual(task["status"], "triage")
        self.assertEqual(self.kinds().count("failure_limit"), 1)
        self.assertEqual(self.kinds().count("crashed"), 1)
