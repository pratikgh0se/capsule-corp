"""R35, S25: `close` is an exit of a land-time blind hold. A card a person closed stays `done` and
never reaches main, with a failing blind test or a passing one, however many passes follow. Real
git over a bare remote, the tick's own `landing.step` (its worker run in-process, the strong
runner the stand-in of `lander_world`), the first pass holds the card, a person closes it, the
following passes find nothing. Oracle 1 is each pass's word and the card's row; oracle 2 is the
remote's `main` and the card's event log."""
import unittest
from unittest import mock

from capsule import blocks, landing
from capsule.runners import sandbox
from capsule.tests import test_landing_step
from capsule.tests.lander_world import ACCEPTANCE, BOARD, IDENT, PASSING, TESTS, Strong
from capsule.tests.test_blind_run import blind_repo, record, sh
from capsule.tests.test_lander import BRANCH

NOW = test_landing_step.NOW


class Closed(test_landing_step.Step):
    def setUp(self):
        super().setUp()
        self.conn.execute("UPDATE tasks SET body=? WHERE id=?", (ACCEPTANCE, self.tid))
        self.blind = blind_repo(self.dir, self.tid, TESTS)       # one test fails
        record(self.conn, self.tid, self.blind)
        self.cfg.update(board=BOARD, epics=[BRANCH], redesign={"blind_tests": "gate"},
                        blind_tests={"repo": str(self.blind), "adapter": "unittest"})
        self.strong = Strong()
        for patch in (mock.patch.object(sandbox, "runner_for", return_value=self.strong),
                      mock.patch.object(landing, "_spawn", self.in_process)):
            patch.start()
            self.addCleanup(patch.stop)

    def in_process(self, conn, cfg, home, now, release, action, task_id, sha=None):
        """The worker `step` would spawn, run here: the landing check itself."""
        return landing._check(conn, cfg, home, now, release, task_id, BRANCH, sha)

    def pass_at(self, now):
        return landing.step(self.conn, self.cfg, self.home, now, release=self.release)

    def retest(self, source):
        (self.blind / "cards" / self.tid / "test_it.py").write_text(source)
        sh("git", "-C", str(self.blind), "add", "-A")
        sh("git", "-C", str(self.blind), *IDENT, "commit", "-q", "-m", "tests")
        record(self.conn, self.tid, self.blind)

    def held_then_closed(self):
        """The first pass holds the card on its failing blind test; a person closes it."""
        self.assertEqual(self.pass_at(NOW + 1), "not_checked")
        self.assertEqual(self.card()["status"], "blocked")
        self.assertTrue(blocks.close(self.conn, self.tid, "landed by hand", now=NOW + 2))
        self.assertEqual(self.card()["status"], "done")

    def card(self):
        return self.conn.execute("SELECT status FROM tasks WHERE id=?", (self.tid,)).fetchone()

    def stays_closed(self, passes):
        before, events = self.remote_main(), self.kinds()
        for n in range(passes):
            self.assertEqual(self.pass_at(NOW + 10 + n), "idle")
            self.assertEqual(self.card()["status"], "done")
        self.assertEqual((self.remote_main(), self.kinds()), (before, events))

    def test_a_held_card_a_person_closed_stays_done_with_a_failing_test_for_many_passes(self):
        self.held_then_closed()
        self.stays_closed(4)
        self.assertEqual(self.strong.started and len(self.strong.started), 1)   # never run again

    def test_the_landing_passes_the_boards_name_so_the_hold_names_the_whole_amend_command(self):
        self.assertEqual(self.pass_at(NOW + 1), "not_checked")
        said = self.conn.execute("SELECT payload FROM task_events WHERE task_id=? AND"
                                 " kind='blocked' ORDER BY id DESC", (self.tid,)).fetchone()[0]
        self.assertIn(f"capsule human {BOARD} {self.tid} amend '<reason>' --body-file <path>",
                      said)

    def test_a_held_card_a_person_closed_stays_done_and_unpushed_with_a_passing_test(self):
        self.held_then_closed()
        self.retest(PASSING)
        self.stays_closed(4)
        self.assertNotEqual(self.remote_main(), self.sha)


if __name__ == "__main__":
    unittest.main()
