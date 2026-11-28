"""EVAL-10 headline: the plain-English "you spent N min; agents did X%" line that opens
`capsule report --md`. Oracle 1: hand arithmetic on the fixture. Oracle 2: the same numbers in the
report's own JSON (human_minutes_per_day, delegation_ratio). The trigger is two weeks of series."""
import shutil
import tempfile
import unittest
from pathlib import Path

from capsule import db, human, paths, report, touches

DAY = 86400
SINCE = 20 * DAY
NO_VAULT = "/nonexistent-vault"


class Headline(unittest.TestCase):
    def md(self, window_days, cos=3):
        home = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        db.create_board(paths.board_db("t", home))
        conn = db.connect(paths.board_db("t", home))
        tid = db.new_task(conn, title="c", now=1)
        conn.commit()
        conn.close()
        for i in range(2):   # owner: two approves = 4 min
            human.write(home, "t", tid, "approve", "ok", now=SINCE + 10 + i, author="owner")
        for i in range(cos):
            human.write(home, "t", tid, "approve", "ok", now=SINCE + 20 + i, author="cos")
        doc = touches.evaluate(home, "t", since=SINCE, now=SINCE + window_days * DAY,
                               vault=NO_VAULT)
        return doc, report.render_md(doc)

    def headline(self, md):
        return next(x for x in md.splitlines() if x.startswith("HEADLINE"))

    def test_two_weeks_of_data_prints_minutes_per_week_and_agent_share(self):
        doc, md = self.md(14)
        # 4 min over 14 days = 2 min/week; cos 3 of 5 actions = 60%
        self.assertEqual(self.headline(md),
                         "HEADLINE: you spent about 2 min a week on Capsule"
                         " (estimate, last 14 days); agents did 60% of the work")
        self.assertAlmostEqual(doc["human_minutes_per_day"] * 7, 2.0)   # oracle 2
        self.assertAlmostEqual(doc["delegation_ratio"], 0.6)
        self.assertEqual(md.splitlines()[1], self.headline(md))   # right under the title

    def test_under_two_weeks_says_not_enough_data_and_no_number(self):
        doc, md = self.md(9)
        self.assertEqual(self.headline(md),
                         "HEADLINE: not enough data yet"
                         " (9 days; needs 14 for a trustworthy number)")
        self.assertNotIn("%", self.headline(md))

    def test_enough_days_but_no_actions_at_all_is_not_a_misleading_zero(self):
        doc, md = self.md(14, cos=0)
        doc["delegation_ratio"] = "unmeasured"
        self.assertIn("agents' share not measured yet", self.headline(report.render_md(doc)))


if __name__ == "__main__":
    unittest.main()
