# mutation-check: targets-predate-range 4d253485 -- tick.py:1177 filter ::
#   LostLoginReview.test_a_reviewer_run_that_lost_its_login_uses_no_review_try,
#   LostLoginReview.test_without_the_listed_line_the_same_run_uses_a_review_try,
#   LostLoginReview.test_the_next_review_tick_after_the_pause_starts_one_reviewer_run
"""A reviewer run that lost its login uses no review try (t_6dd3b204, inc_f83f6aa2). Z1 intent:
the CLI refuses a reviewer run with its own `Not logged in` line, the run is released and the
board waits, and no crash, review retry, incident or review try follows; when the pause ends the
review starts again. The contrast test removes the listed line: the same run is then a try."""
import time
import unittest.mock
from pathlib import Path

from capsule import db, incidents
from capsule.models import claude
from capsule.tests.test_tick import GUIDE_OK, IMPL_DONE, REV_PASS
from capsule.tests.test_korin import KorinCase

FX = Path(__file__).parent / "fixtures" / "claude" / "not_logged_in.json"


class LostLoginReview(KorinCase):
    def kinds(self):
        sql = "SELECT kind FROM task_events WHERE task_id=? ORDER BY id"
        return [r[0] for r in self.conn.execute(sql, (self.tid,))]

    def lost_review(self, now):
        res = claude.parse(FX.read_text(), "", 1, now=now)
        self.script["reviewer"] = [[res], [REV_PASS]]
        self.script["implementer"] = [[IMPL_DONE]]
        self.script["guide"] = [[GUIDE_OK], [GUIDE_OK]]
        for step in range(3):
            self.tick.once(now + step)                      # the reviewer run closes on the third
        impl = self.runs("implementer")[0]
        return impl, self.tick._review_round(self.conn, self.tid, impl).get("reviewer", [])

    def test_a_reviewer_run_that_lost_its_login_uses_no_review_try(self):
        now = int(time.time())
        impl, tries = self.lost_review(now)
        run = self.runs("reviewer")[0]
        self.assertEqual((run["status"], run["error_class"]), ("released", "auth_required"))
        self.assertNotIn("crashed", self.kinds())
        self.assertNotIn("review_retry", self.kinds())
        self.assertEqual(incidents.listing(self.conn), [])
        self.assertEqual(tries, [])
        self.assertGreater(db.guard_get(self.conn, "pause:board")[0]["until"], now)
        self.assertEqual(self.task()["status"], "review")

    def test_without_the_listed_line_the_same_run_uses_a_review_try(self):
        now = int(time.time())
        with unittest.mock.patch.object(claude, "AUTH_LINES", ()):
            impl, tries = self.lost_review(now)
        self.assertEqual(self.kinds().count("crashed"), 1)
        self.assertEqual(tries[0]["id"], self.runs("reviewer")[0]["id"])    # the run is a try

    def test_the_next_review_tick_after_the_pause_starts_one_reviewer_run(self):
        now = int(time.time())
        self.lost_review(now)
        self.tick.once(now + 301)                            # the pause is over
        self.tick.once(now + 302)
        runs = self.runs("reviewer")
        self.assertEqual(len(runs), 2)
        self.assertNotEqual(runs[1]["outcome"], "rate_limited")
        self.assertNotIn("review_retry", self.kinds())
