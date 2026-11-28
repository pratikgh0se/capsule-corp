"""A host-wall outage as the tick and its readers see it over many passes (D161): one page per
outage, a person's pause that keeps its quiet, the outage's own pause that carries its start, the
digest's "paused over an hour" line, the `paused` log line and the heartbeat (S32-S34, S37)."""
import time

from capsule import db, dispatch, slack_digest
from capsule import invalid_doc as idoc
from capsule.runners import host_wall
from capsule.tests.test_host_wall import Board
from capsule.tests.test_host_wall_close import WallCase, wall_result
from capsule.tests.test_tick import IMPL_DONE, FakeEvents


class Outage(WallCase):
    def outage(self, now, failures, step=400):
        """Tick through `failures` wall failures (two passes each: the dispatch, then the close,
        whose retry waits 300 s); yields each pass's time after the pass has run."""
        self.script["implementer"] = [[wall_result(now + step * (2 * j + 1))]
                                      for j in range(failures)]
        for i in range(2 * failures):
            self.tick.once(now + step * i)
            yield now + step * i

    def test_a_finished_session_of_a_role_the_wall_does_not_cover_does_not_end_an_outage(self):
        now = int(time.time())
        for k, at in enumerate(self.outage(now, 5)):   # a lander and a validator finish in between
            for role, n in (("lander", 1), ("validator", 2)):
                self.conn.execute(
                    "INSERT INTO task_runs (task_id, profile, status, outcome, started_at,"
                    " ended_at, run_id) VALUES (?,?,?,?,?,?,?)",
                    (self.tid, role, "completed", "completed", at, at + n,
                     f"{role}{k}".ljust(32, "0")))
        self.assertEqual(len(self.messages()), 1, self.messages())     # one page for the outage
        first = self.guard("wall:outage:implementer")["since"]
        self.assertEqual(first, now + 400)                              # the first failure

    def pause(self, now, profile="implementer", **done):
        run = {"account": None, "task_id": self.tid, "run_id": "r1", "profile": profile}
        done = dict({"error_class": "wall_unavailable", "error": "gone", "retry_after": now + 300},
                    **done)
        return idoc.pause_scope(self.conn, self.cfg["board"], run, done, now)

    def test_a_persons_pause_keeps_its_quiet_when_a_wall_failure_lands_on_it(self):
        now = int(time.time())
        db.guard_set(self.conn, "pause:board", {"until": now + 5000, "reason": "person"}, now=now)
        keys, _ = self.pause(now)
        self.assertEqual(keys, ["pause:board"])
        held = self.guard("pause:board")
        self.assertNotIn("wall", held)                          # not marked as the wall's
        self.assertEqual((held["until"], held["reason"]), (now + 5000, "person"))
        self.assertTrue(dispatch.any_pause(self.conn, now + 4000, checks=True))   # quiet past 900 s

    def test_a_persons_pause_sends_no_page_for_a_wall_failure_that_lands_on_it(self):
        now = int(time.time())
        db.guard_set(self.conn, "pause:board", {"until": now + 5000, "reason": "person"}, now=now)
        for k in range(3):
            self.pause(now + 400 * k)
        self.assertEqual(self.messages(), [])
        self.assertIsNone(self.guard("wall:outage:implementer"))     # it is not the wall's outage

    def succeeded(self, role, at):
        self.conn.execute(
            "INSERT INTO task_runs (task_id, profile, status, outcome, started_at, ended_at,"
            " run_id) VALUES (?,?,?,?,?,?,?)",
            (self.tid, role, "completed", "completed", at, at + 1, f"{role}{at}".ljust(32, "0")))

    def test_an_implementer_that_works_between_korin_only_failures_pages_once(self):
        now = int(time.time())
        self.pause(now, "korin")
        for k in (1, 2, 3):
            self.succeeded("implementer", now + 400 * k - 100)      # the implementer's wall works
            self.pause(now + 400 * k, "korin")                      # Korin's keeps failing
        self.assertEqual(len(self.messages()), 1, self.messages())
        self.assertEqual(self.guard("wall:outage:korin")["since"], now)
        self.succeeded("korin", now + 1700)                         # Korin's own wall works again
        self.pause(now + 2000, "korin")
        self.assertEqual(len(self.messages()), 2)                   # a new outage, a new page

    def test_a_second_role_failing_inside_an_open_outage_does_not_page_again(self):
        now = int(time.time())
        self.pause(now, "korin")
        self.pause(now + 400, "implementer")
        self.assertEqual(len(self.messages()), 1, self.messages())
        self.assertEqual(self.guard("wall:outage:implementer")["since"], now + 400)

    def test_a_wall_outage_marks_its_own_pause_with_the_outages_start(self):
        now = int(time.time())
        self.pause(now)
        later = now + 301                                       # the retry, after the pause ended
        self.pause(later, retry_after=later + 300)
        held = self.guard("pause:board")
        self.assertEqual((held["wall"], held["since"], held["until"]), (now, now, later + 300))

    def test_the_digests_paused_since_counts_the_whole_outage_and_its_hour_line_can_fire(self):
        now = int(time.time())
        for last in self.outage(now, 7):                        # 4800 s of failures
            pass
        since = slack_digest.paused_since(self.conn, last + 1)
        self.assertEqual(since, now + 400)                      # the first failure, not the last
        self.assertGreater(last + 1 - since, slack_digest.PAUSE_AFTER_S)
        self.assertEqual(len(self.messages()), 1)               # and still the one page

    def test_a_wall_pause_logs_its_line_with_the_reason(self):
        now = int(time.time())
        self.script["implementer"] = [[wall_result(now + 1)], [IMPL_DONE]]
        self.tick.once(now)
        self.tick.once(now + 1)
        lines = [r[0] for r in self.conn.execute(
            "SELECT body FROM outbox WHERE body LIKE '%paused:%'")]
        self.assertEqual(len(lines), 1, lines)
        self.assertIn("host wall unavailable", lines[0])
        self.assertIn("retries when the pause ends", lines[0])

    def test_the_heartbeat_says_paused_for_a_wall_outage_only_for_its_first_15_minutes(self):
        events = FakeEvents()
        beat = self.make_tick(events=events)
        self.runner.up = False
        for wall, paused in ((10, True), (dispatch.WALL_QUIET + 100, False), (None, True)):
            guard = {"until": time.time() + 5000}
            db.guard_set(self.conn, "pause:board", dict(guard, **(
                {} if wall is None else {"wall": time.time() - wall})), now=int(time.time()))
            beat.once()
            self.assertIs(events.logged[-1]["payload"]["paused"], paused, wall)


class Roles(Board):
    def test_the_roles_whose_success_ends_an_outage_are_walled_and_the_unwalled_ones_are_not(self):
        self.assertFalse(set(host_wall.HOST_ROLES) & set(host_wall.UNWALLED_ROLES))
        for role in host_wall.HOST_ROLES:
            self.assertIsInstance(self.plan(role), dict, role)       # a wall is planned for it
            self.assertNotIn("unavailable", self.plan(role), role)
        for role in host_wall.UNWALLED_ROLES:
            self.assertIsNone(self.plan(role), role)


if __name__ == "__main__":
    import unittest
    unittest.main()
