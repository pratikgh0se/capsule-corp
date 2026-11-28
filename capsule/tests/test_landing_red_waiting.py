"""red-before-review: a card is neither judged (and so published) nor landed while its red check is
waiting. A finished tester round with no `blind_red_checked` event is a waiting red check
(`red.step` takes it to a verdict later in the same pass). RedWaiting uses the real landing worker
of test_landing_step (oracles: `landing_checked` rows, `step`'s word); RealOrder drives
`Tick._review_one` and `red.step` over real git."""
import json
import unittest
from unittest import mock

from capsule import lease, tick
from capsule.blind import dispatch, settle
from capsule.db import event
from capsule.tests import test_blind_red_tick, test_landing_step
from capsule.tests.test_landing_step import NOW


class RedWaiting(test_landing_step.Step):
    def setUp(self):
        super().setUp()
        self.cfg["lander"]["mode"] = "dry-run"
        self.cfg["redesign"] = {"blind_tests": "report"}
        self.cfg["epics"] = ["main"]

    def round(self):
        """A finished tester round for the card whose red check has not run; its run row."""
        self.conn.execute(
            "INSERT INTO task_runs (task_id, profile, status, outcome, started_at, ended_at,"
            " run_id, handoff, metadata) VALUES (?,?,?,?,?,?,?,?,?)",
            (self.tid, "blind-tester", "done", "completed", 1, 2, "r1", json.dumps({}), "{}"))
        self.conn.commit()
        return self.conn.execute("SELECT * FROM task_runs WHERE run_id='r1'").fetchone()

    def checked(self):
        return [json.loads(p) for (p,) in self.conn.execute(
            "SELECT payload FROM task_events WHERE kind='landing_checked'")]

    def test_a_waiting_red_check_with_a_passing_review_does_not_land(self):
        self.round()
        self.assertEqual(self.step(), "idle")
        self.assertEqual(self.step(NOW + 2), "idle")      # nor in any later pass
        self.assertEqual(self.checked(), [])

    def test_a_red_check_that_passed_lets_the_card_go(self):
        run = self.round()
        self.assertEqual(self.step(), "idle")
        settle.record(self.conn, run, "landed", NOW + 1)
        self.conn.commit()
        self.assertEqual(self.step(NOW + 2), "dry-run")
        self.assertEqual([c["commit"] for c in self.checked()], [self.sha])

    def test_a_red_check_that_failed_is_over_the_card_goes_on_as_before(self):
        run = self.round()
        settle.record(self.conn, run, "bounced", NOW + 1, round=1)
        self.conn.commit()
        self.assertEqual(settle.unsettled(self.conn, self.tid), [])
        self.assertEqual(self.step(NOW + 2), "dry-run")
        self.assertEqual(len(self.checked()), 1)

    def test_blind_tests_off_or_no_tester_round_behaves_as_before(self):
        self.assertEqual(self.step(), "dry-run")           # on, but no round at all
        self.assertEqual(len(self.checked()), 1)

    def test_blind_tests_off_ignores_an_unprocessed_round(self):
        self.cfg["redesign"] = {"blind_tests": "off"}
        self.round()
        self.assertEqual(len(settle.unsettled(self.conn, self.tid)), 1)
        self.assertEqual(self.step(), "dry-run")

    def test_no_epic_means_red_step_never_runs_so_the_gate_does_not_hold(self):
        self.round()
        self.cfg["epics"] = []
        self.assertEqual(self.step(), "dry-run")

    def test_waiting_given_a_card_returns_only_that_cards_pending_triage(self):
        for card in (self.tid, "t_other"):
            self.conn.execute(
                "INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?,?,?,?)",
                (card, "blind_red_checked", json.dumps({"verdict": "triage_pending",
                                                        "run_id": "r1"}), 1))
        self.conn.commit()
        self.assertEqual([w["task_id"] for w in settle.waiting(self.conn, self.tid)], [self.tid])
        self.assertEqual(len(settle.waiting(self.conn)), 2)


class RealOrder(test_blind_red_tick.RedCase):
    """The tick's real order for a card in review whose review runs have all ended: `_review_one`
    (the verdict, which publishes), then `red.step`. Oracles: how often `_verdict` ran (it is the
    only thing that clones, checks and notifies), the `blind_red_checked` verdicts and the blind
    repo's `main`."""

    def setUp(self):
        super().setUp()
        self.cfg.update(epics=["main"])
        self.conn.execute("UPDATE tasks SET status='review' WHERE id=?", (self.card,))
        self.conn.commit()
        run = {"id": 1, "run_id": "r", "ended_at": 5, "handoff": "{}", "error_class": None,
               "error": None}
        self.me = mock.Mock(_reviewed_impl=lambda *a: {"id": 1},
                            _review_round=lambda *a: {role: [run] for role in tick.REVIEW_ROLES},
                            _spool_root=lambda cfg: self.root,
                            _pending_contradictions=lambda *a: [], _round_floor=lambda *a: 0)

    def pass_(self):
        """The tick's review step for the card, once."""
        task = self.conn.execute("SELECT * FROM tasks WHERE id=?", (self.card,)).fetchone()
        tick.Tick._review_one(self.me, self.conn, self.cfg, task, NOW, {"reviewed": 0})

    def verdicts(self):
        return [e["verdict"] for e in self.events("blind_red_checked")]

    def live_tester(self):
        """A tester run for the card's current text that is still running; its row id."""
        self.conn.execute(
            "INSERT INTO task_runs (task_id, profile, status, started_at, run_id, metadata)"
            " VALUES (?, 'blind-tester', 'running', 1, 'live', ?)",
            (self.card, json.dumps({"card_hash": self.hash})))
        self.conn.commit()
        return self.conn.execute("SELECT id FROM task_runs WHERE run_id='live'").fetchone()[0]

    def test_while_the_red_check_waits_no_verdict_runs_then_one_does(self):
        self.make_round()
        for _ in range(3):                                 # three passes, each with a waiting card
            self.pass_()
        self.assertEqual(self.me._verdict.call_count, 0)
        self.step()
        self.assertEqual(self.verdicts(), ["landed"])
        self.pass_()
        self.pass_()
        self.assertEqual(self.me._verdict.call_count, 2)   # the verdict is the tick's to repeat

    def test_a_running_tester_holds_the_verdict_until_it_ends(self):
        run = self.live_tester()
        self.pass_()
        self.assertEqual(self.me._verdict.call_count, 0)
        lease.end_run(self.conn, run, status="failed", outcome="failed", now=NOW, summary="x")
        self.pass_()
        self.assertEqual(self.me._verdict.call_count, 1)

    def test_a_tester_that_crashes_twice_does_not_leave_the_card_stuck(self):
        for n in range(2):
            run = self.live_tester()
            lease.end_run(self.conn, run, status="failed", outcome="failed", now=NOW, summary="x")
            self.conn.execute("UPDATE task_runs SET run_id=? WHERE id=?", (f"dead{n}", run))
        self.pass_()
        self.assertEqual(self.me._verdict.call_count, 1)

    def test_a_failing_red_check_is_handled_as_today_nothing_lands_no_second_round(self):
        self.make_round(test_blind_red_tick.PASSING)
        self.step()
        self.assertEqual(self.verdicts(), ["bounced"])
        self.assertEqual((self.events("blind_landed"), self.main()), ([], self.main0))
        self.pass_()                                       # a result is in: the card goes on
        self.assertEqual(self.me._verdict.call_count, 1)
        for who, profile in enumerate(("implementer", "validator")):
            self.conn.execute(
                "INSERT INTO task_runs (task_id, profile, status, outcome, started_at, run_id)"
                " VALUES (?,?,'done','completed',?,?)", (self.card, profile, who, f"v{who}"))
        sql = dispatch._CANDIDATES.format(marks="?")
        self.assertEqual(self.conn.execute(sql, ["main"]).fetchall(), [])   # no second tester

    def test_a_triage_pending_while_the_card_ran_is_applied_before_any_verdict(self):
        self.conn.execute("UPDATE tasks SET status='running' WHERE id=?", (self.card,))
        self.make_round(test_blind_red_tick.PASSING)
        self.step()
        self.make_round(test_blind_red_tick.PASSING)
        self.step()
        self.assertEqual(self.verdicts(), ["bounced", "triage_pending"])
        self.conn.execute("UPDATE tasks SET status='review' WHERE id=?", (self.card,))
        self.pass_()                    # the implementer finished: review runs before `release`
        self.assertEqual(self.me._verdict.call_count, 0)
        self.step()                     # the red step's `release` triages the card first
        self.assertEqual((self.verdicts()[-1], self.card_row()["status"]), ("triaged", "triage"))

    def test_blind_tests_off_leaves_the_verdict_alone(self):
        self.make_round()
        self.cfg["redesign"] = {"blind_tests": "off"}
        self.pass_()
        self.assertEqual(self.me._verdict.call_count, 1)


if __name__ == "__main__":
    unittest.main()
