"""t_062995b5: Z5 for the lander's deploy hold; the post-land `deploy --check`, which since row 632
b runs in the release checkout only (never in a clone of the landed sha: that was the candidate's
own deploy.sh on the host). Losing the sha-aware drift check is intentional (CoS ruling). Same
real-git world as test_landing.py; oracle 1 is the outbox and incident rows, oracle 2 is the
files the deploy scripts wrote and the release checkout's refs."""
import unittest.mock

from capsule import db, landing, lander
from capsule.tests import test_landing
from capsule.tests.test_lander import BRANCH, git

HOUR = 3600


class HoldAlarm(test_landing.Landing):
    def rows(self):
        asks = [r[0] for r in self.conn.execute(
            "SELECT body FROM outbox WHERE body LIKE '%lander hold%'")]
        return asks, self.conn.execute("SELECT COUNT(*) FROM incidents").fetchone()[0]

    def hold(self, age):
        db.guard_set(self.conn, landing.HOLD, {"reason": "DRIFT x", "task": "t_old"},
                     now=int(landing.time.time()) - age)
        self.cfg["lander"]["deploy_check"] = "echo DRIFT x; exit 1"

    def test_a_hold_older_than_two_hours_asks_once_and_opens_one_incident(self):
        self.put("a.txt", "work\n", "card work")
        self.hold(3 * HOUR)
        for _ in range(3):
            self.assertEqual(self.land()["outcome"], "held_deploy_drift")
        asks, incidents = self.rows()
        self.assertEqual((len(asks), incidents), (1, 1), asks)
        kind = self.conn.execute("SELECT kind FROM incidents").fetchone()[0]
        self.assertEqual(kind, "silent_failure")

    def test_the_tick_side_prerun_alarms_the_same_one_time(self):
        self.hold(3 * HOUR)
        for _ in range(3):
            got = landing._prerun(self.conn, self.tid, self.cfg, str(self.release), self.home)
            self.assertEqual(got, "held_deploy_drift")
        asks, incidents = self.rows()
        self.assertEqual((len(asks), incidents), (1, 1), asks)

    def test_a_young_hold_stays_quiet(self):
        self.put("a.txt", "work\n", "card work")
        self.hold(HOUR)
        for _ in range(2):
            self.assertEqual(self.land()["outcome"], "held_deploy_drift")
        self.assertEqual(self.rows(), ([], 0))

    def test_a_cleared_then_new_stale_hold_alarms_again(self):
        self.put("a.txt", "work\n", "card work")
        self.hold(3 * HOUR)
        self.land()
        db.guard_set(self.conn, landing.HOLD, None)
        self.hold(4 * HOUR)
        self.land()
        self.assertEqual(len(self.rows()[0]), 2)


class CloneCheck(test_landing.Landing):
    def test_the_post_land_check_runs_in_the_release_and_never_the_candidates_deploy_script(self):
        """Row 632 b: the landed sha's own deploy.sh is candidate code; it is never run. The
        release-only check is intentional: drift from a landed sha is the keeper's check. The
        script is check_deploy.sh because deploy.sh itself is a workspace.PROTECTED path (row 632
        a): a card editing it is held at `protected` and never reaches the push."""
        seen = self.dir / "seen"
        seen.mkdir()
        self.cfg["lander"]["deploy_check"] = "sh ./check_deploy.sh --check"
        git(self.repo, "checkout", "-q", "main")
        self.put("check_deploy.sh", f"echo release > {seen}/release\n", "release deploy script")
        git(self.repo, "push", "-q", "origin", "main")
        git(self.release, "pull", "-q")
        git(self.repo, "checkout", "-q", BRANCH)
        git(self.repo, "reset", "-q", "--hard", "main")
        head = self.put("check_deploy.sh", f"echo candidate > {seen}/candidate\n",
                        "worker edits deploy")
        release_head = git(self.release, "rev-parse", "HEAD")
        with unittest.mock.patch.object(lander.paths, "REPO", self.release):   # the advisory check
            got = self.land()
        self.assertEqual((got["outcome"], got["deploy"]), ("landed", "ok"))
        self.assertEqual(self.remote_main(), head)
        self.assertEqual((seen / "release").read_text().strip(), "release")
        self.assertFalse((seen / "candidate").exists())
        self.assertEqual(git(self.release, "rev-parse", "HEAD"), release_head)   # untouched
        self.assertEqual(git(self.release, "status", "--porcelain"), "")

    def test_a_drifting_post_land_check_still_sets_the_hold(self):
        self.cfg["lander"]["deploy_check"] = "echo DRIFT x; exit 1"
        self.put("a.txt", "work\n", "card work")
        got = self.land()
        self.assertEqual((got["outcome"], got["deploy"]), ("landed", "drift"))
        self.assertTrue(db.guard_get(self.conn, landing.HOLD)[0])


if __name__ == "__main__":
    unittest.main()
