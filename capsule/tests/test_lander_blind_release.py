"""R35, S28, S29: what a person's release can land at land time. An inconclusive release lifts an
inconclusive or a skipped run. A skip release lifts a run only when it finished: at least one
passing row, a `Ran` line that the rows match (less what `skipped=N` explains), every landed test
with a row and an untouched worktree; never a timeout, a late run, all tests skipped or a summary
that disagrees with the rows. Fixtures in `lander_world`."""
import subprocess

from capsule import db, lander_blind
from capsule.blind import run as blind_run
from capsule.tests.lander_world import PASSING, Landing, Strong
from capsule.tests.test_blind_run import blind_repo, record
from capsule.tests.test_lander_blind import SKIPPING

ALL_SKIPPED = SKIPPING.replace('self.assertEqual(run("cat hello.txt").stdout, "hi\\n")',
                               'self.skipTest("later")')
TIMEOUT = subprocess.TimeoutExpired("x", 1)


def no_ran_line(text):
    return "\n".join(x for x in text.splitlines() if not x.startswith("Ran "))


class SkipRelease(Landing):
    source = PASSING

    def broken(self):
        return Strong(tamper=lambda t: t.replace("Ran 2 tests", "Ran 1 test"))

    def skip_release(self):
        self.held_for(lander_blind.SKIPPED, 200)
        self.accepted(100)

    def inconclusive_release(self):
        self.held_for(lander_blind.INCONCLUSIVE, 200)
        self.accepted(100)

    def skipping(self, source=SKIPPING):
        self.commit_tests(source)
        record(self.conn, self.card, self.blind)

    def held(self, strong):
        return self.land(strong)["verdict"] == "held"

    def test_a_skip_release_lifts_a_run_with_a_passing_row_whose_skip_count_is_off(self):
        self.skipping()
        self.skip_release()
        result = self.land(Strong(tamper=lambda t: t.replace("OK (skipped=1)", "OK")))
        self.assertEqual(result["verdict"], "checked")
        self.assertEqual(self.checks(result)["blind"]["detail"],
                         lander_blind.RELEASED + lander_blind.INCONCLUSIVE)

    def test_a_skip_release_lifts_a_skip_with_a_passing_row(self):
        self.skipping()
        self.skip_release()
        self.assertEqual(self.land(Strong())["verdict"], "checked")

    def test_a_skip_release_does_not_land_a_timeout_with_no_rows(self):
        self.skip_release()
        self.assertTrue(self.held(Strong(boom=TIMEOUT)))

    def test_a_skip_release_does_not_land_a_late_run(self):
        self.skip_release()
        self.assertTrue(self.held(Strong(late=True)))

    def test_a_skip_release_does_not_land_passing_rows_beside_a_failed_summary(self):
        self.skip_release()
        self.assertTrue(self.held(Strong(tamper=lambda t: t.replace("OK", "FAILED (failures=1)"))))

    def test_a_skip_release_does_not_land_a_landed_test_with_no_row(self):
        names = ("test_greets", "test_fails", "test_ghost")
        repo = blind_repo(self.dir / "r2", self.card, PASSING, names)    # an example, no test
        record(self.conn, self.card, repo)
        self.cfg["blind_tests"] = {"repo": str(repo), "adapter": "unittest"}
        db.event(self.conn, self.card, "blind_landed", {
            "blind_commit": blind_run._recorded_commit(self.conn, self.card),
            "card_hash": "h", "start_sha": "s", "exempt": False, "acceptance": ["the page greets"],
            "tests": [f"{self.card}.{n}" for n in names]})
        self.skip_release()
        self.assertTrue(self.held(Strong()))

    def test_a_skip_release_does_not_land_a_run_where_every_test_was_skipped(self):
        self.skipping(ALL_SKIPPED)
        self.skip_release()
        self.assertTrue(self.held(Strong()))

    def test_a_skip_release_does_not_land_two_rows_beside_ran_five(self):
        self.skip_release()
        self.assertTrue(self.held(Strong(tamper=lambda t: t.replace("Ran 2 tests", "Ran 5 tests"))))

    def test_a_skip_release_does_not_land_a_run_with_no_ran_line(self):
        self.skip_release()
        self.assertTrue(self.held(Strong(tamper=no_ran_line)))

    def test_a_skip_release_does_not_land_a_changed_worktree_with_or_without_ran_five(self):
        def plant(ws):
            (ws / "planted.txt").write_text("x")

        for tamper in (lambda t: t.replace("Ran 2 tests", "Ran 5 tests"), None):
            self.skip_release()
            self.assertTrue(self.held(Strong(after=plant, tamper=tamper)))
            self.conn.execute("DELETE FROM task_events WHERE kind IN ('blocked','intervention')")

    def test_a_skip_hold_accepted_does_not_release_an_unfinished_inconclusive_run(self):
        self.skipping()
        self.assertEqual(self.landed(Strong())["hold"], lander_blind.SKIPPED)
        self.accepted(0)
        self.commit_tests(PASSING)
        record(self.conn, self.card, self.blind)
        self.assertTrue(self.held(self.broken()))

    def test_an_inconclusive_release_lifts_the_runs_a_skip_release_cannot(self):
        runs = [Strong(boom=TIMEOUT), Strong(late=True), self.broken(),
                Strong(tamper=lambda t: t.replace("Ran 2 tests", "Ran 5 tests")),
                Strong(tamper=no_ran_line)]
        for strong in runs:
            self.inconclusive_release()
            self.assertEqual(self.land(strong)["verdict"], "checked")
            self.conn.execute("DELETE FROM task_events WHERE kind IN ('blocked','intervention')")

    def test_an_inconclusive_release_lifts_a_run_where_every_test_was_skipped(self):
        self.skipping(ALL_SKIPPED)
        self.inconclusive_release()
        self.assertEqual(self.land(Strong())["verdict"], "checked")

