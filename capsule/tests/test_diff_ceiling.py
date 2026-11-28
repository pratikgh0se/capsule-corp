"""M032 diff-ceiling: a card whose real git diff is over `diff_lines_park` is not landed; it is
told once per head. Real git over the temp remote of test_landing_step; oracle 1 is `step`'s word,
oracle 2 reads the `landing` events and the remote's `main` back with plain sql and git."""
import json
import os
import unittest
from unittest import mock

from capsule import blocks, diff_ceiling
from capsule.tests import test_landing_step
from capsule.tests.test_landing_step import NOW, git


class Ceiling(test_landing_step.Step):
    def setUp(self):
        super().setUp()
        self.cfg["lander"]["mode"] = "dry-run"
        patch = mock.patch.dict(os.environ, {"OPS_DIFF_LINES_PARK": "5"})
        patch.start()
        self.addCleanup(patch.stop)

    def events(self):
        return [json.loads(p) for (p,) in self.conn.execute(
            "SELECT payload FROM task_events WHERE kind='landing'")
            if json.loads(p)["outcome"] == "diff_over_ceiling"]

    def asks(self):
        return self.conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]

    def grow(self, lines, name="big.txt", at=NOW):
        self.sha = self.put(name, "x\n" * lines, "more work")
        self.finish(test_landing_step.PASS, at=at)

    def test_off_by_default_lands_as_before(self):
        os.environ.pop("OPS_DIFF_LINES_PARK")
        self.grow(500)
        self.assertEqual(self.step(), "dry-run")
        self.assertEqual(self.events(), [])

    def status(self):
        return self.conn.execute("SELECT status, block_kind FROM tasks WHERE id=?",
                                 (self.tid,)).fetchone()

    def test_over_the_ceiling_is_held_and_told_once_per_head(self):
        self.cfg["lander"]["mode"] = "land"
        self.grow(20)                                    # 1 + 20 lines against a ceiling of 5
        before = self.asks()
        self.assertEqual(self.step(), "idle")
        self.assertEqual(self.step(NOW + 2), "idle")
        self.assertEqual(self.checked(), [])
        [ev] = self.events()
        self.assertEqual(ev["detail"],
                         "diff size 21 over the ceiling of 5: split it or make it an epic")
        self.assertEqual(ev["commit"], self.sha)
        self.assertEqual(self.asks() - before, 1)
        self.assertEqual(tuple(self.status()), ("blocked", "needs_input"))   # visible, not stuck

    def test_a_new_head_is_judged_again(self):
        self.cfg["lander"]["mode"] = "land"
        self.grow(20)
        self.assertEqual(self.step(), "idle")
        self.grow(30, "more.txt", at=NOW + 3)
        self.assertEqual(self.step(NOW + 4), "idle")
        self.assertEqual([e["commit"] for e in self.events()][-1], self.sha)
        self.assertEqual(len(self.events()), 2)
        self.cfg["lander"]["mode"] = "dry-run"
        git(self.repo, "reset", "-q", "--hard", "HEAD~2")      # a slimmer head lands
        self.sha = git(self.repo, "rev-parse", "HEAD")
        self.finish(test_landing_step.PASS, at=NOW + 5)
        self.assertEqual(self.step(NOW + 6), "dry-run")

    def test_dry_run_writes_nothing_but_does_not_land(self):
        self.grow(20)
        before = self.asks()
        self.assertEqual(self.step(), "idle")
        self.assertEqual((self.events(), self.checked(), self.asks() - before), ([], [], 0))
        self.assertEqual(self.status()[0], "done")

    def test_a_human_unblock_on_the_same_head_waives_the_ceiling(self):
        self.cfg["lander"]["mode"] = "land"
        self.grow(20)
        self.assertEqual(self.step(), "idle")
        self.assertTrue(blocks.unblock(self.conn, self.tid, by="human", now=NOW + 2))
        self.assertEqual(self.status()[0], "ready")
        self.cfg["lander"]["mode"] = "dry-run"
        self.finish(test_landing_step.PASS, at=NOW + 3)           # the same head, completed again
        self.assertEqual(self.step(NOW + 4), "dry-run")
        self.assertEqual(len(self.events()), 1)

    def test_a_stale_target_is_fetched_before_holding(self):
        self.cfg["lander"]["mode"] = "land"
        self.grow(20)
        git(self.repo, "update-ref", "refs/remotes/origin/main", "refs/heads/main~0")
        stale = git(self.repo, "rev-parse", "origin/main")
        # the remote's main took the card's files already: only a fetch shows a small diff
        git(self.remote, "fetch", "-q", str(self.repo), f"{self.sha}:refs/heads/tmp")
        git(self.remote, "update-ref", "refs/heads/main", self.sha)
        self.assertEqual(stale, git(self.repo, "rev-parse", "origin/main"))
        self.step()
        self.assertEqual(self.events(), [])                       # not held: the size is 0 now

    def test_no_size_is_one_stderr_line_and_judged_as_before(self):
        with mock.patch("sys.stderr") as err:
            held = diff_ceiling._held(self.conn, self.cfg, self.tid, "b", "0" * 40, NOW)
        self.assertFalse(held)
        self.assertEqual(len(err.write.call_args_list[0][0][0].splitlines()), 1)

    def test_an_epic_card_is_exempt(self):
        from capsule.tests.test_lander import BRANCH
        self.cfg["epics"] = [BRANCH]
        self.grow(500)
        self.assertEqual(self.step(), "dry-run")
        self.assertEqual(self.events(), [])

    def test_at_or_under_the_ceiling_lands(self):
        self.sha = self.put("a.txt", "x\ny\nz\nw\n", "four lines")   # 1 line changed to 4: 5 total
        self.finish(test_landing_step.PASS)
        self.assertEqual(self.step(), "dry-run")

    def test_test_files_and_lockfiles_do_not_count(self):
        self.put("capsule/tests/test_x.py", "x\n" * 50, "tests")
        self.put("maintenance/tests/t.py", "x\n" * 50, "tests")
        self.put("tests/t.py", "x\n" * 50, "tests")
        self.put("uv.lock", "x\n" * 50, "lock")
        self.sha = self.put("web/package-lock.json", "x\n" * 50, "lock")
        self.finish(test_landing_step.PASS)
        self.assertEqual(diff_ceiling._size(self.repo, "main", self.sha), 1)
        self.assertEqual(self.step(), "dry-run")

    def test_the_size_comes_from_git_not_the_report(self):
        self.grow(20)
        self.assertEqual(diff_ceiling._size(self.repo, "main", self.sha), 21)
        self.assertIsNone(diff_ceiling._size(self.repo, "main", "0" * 40))


if __name__ == "__main__":
    unittest.main()
