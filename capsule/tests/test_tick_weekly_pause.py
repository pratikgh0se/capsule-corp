"""D234: the tick starts no new card runs while the weekly usage meter is at its pause line."""
import contextlib
import io
import json
import os
import time
from unittest import mock

os.environ["AGENT_OPS_NO_SEND"] = "1"
from capsule import db, weekly_pause
from capsule.tests import test_blind_dispatch as bd
from capsule.tests import test_tick as tt


def reading(fraction):
    return {"fraction": fraction, "used": fraction * 100, "limit": 100.0, "week_start": 0,
            "reset_at": 604800, "pause_at": 0.90}


class WeeklyPause(tt.TickCase):
    def setUp(self):
        super().setUp()
        self.script["implementer"] = [[tt.IMPL_DONE]]
        self.fraction = 0.95
        patch = mock.patch.object(weekly_pause, "meter", lambda home, now, state_dir=None: (
            None if self.fraction is None else reading(self.fraction)))
        patch.start()
        self.addCleanup(patch.stop)

    def kinds(self, kind):
        return [json.loads(r["payload"]) for r in self.conn.execute(
            "SELECT payload FROM task_events WHERE kind=?", (kind,))]

    def guard(self):
        return db.guard_get(self.conn, "weekly_pause")[0]

    def test_over_the_line_no_card_starts_and_one_pause_event_over_three_passes(self):
        for _ in range(3):
            self.tick.once()
        self.assertEqual(self.runs(), [])
        self.assertEqual(self.task()["status"], "ready")
        self.assertEqual(self.kinds("weekly_pause"), [{"pct": 95, "reset_at": 604800}])
        self.assertEqual(self.kinds("weekly_resume"), [])
        self.assertEqual(self.guard(), {"held": True})      # the second oracle: the guard row

    def test_exactly_at_the_line_holds(self):
        self.fraction = 0.90
        self.tick.once()
        self.assertEqual(self.runs(), [])
        self.assertEqual(self.guard(), {"held": True})

    def test_a_meter_that_raises_dispatches_with_one_stderr_line_and_no_event(self):
        err = io.StringIO()
        with mock.patch.object(weekly_pause, "meter", side_effect=RuntimeError("boom")), \
                contextlib.redirect_stderr(err):
            self.tick.once()
        self.assertEqual(len(self.runs("implementer")), 1)
        self.assertEqual(self.kinds("weekly_pause") + self.kinds("weekly_resume"), [])
        self.assertIsNone(self.guard())
        self.assertEqual(err.getvalue().count("weekly pause check failed"), 1)

    def test_a_meter_that_raises_lifts_a_standing_hold(self):
        self.tick.once()
        with mock.patch.object(weekly_pause, "meter", side_effect=RuntimeError("boom")), \
                contextlib.redirect_stderr(io.StringIO()):
            self.tick.once()
        self.assertEqual(self.guard(), {"held": False})
        self.assertEqual(len(self.runs("implementer")), 1)
        self.assertEqual(self.kinds("weekly_resume"), [{"pct": None, "reset_at": None}])

    def test_a_card_in_review_still_gets_its_reviewer_while_held(self):
        self.fraction = 0.5
        self.tick.once()
        self.assertEqual(len(self.runs("implementer")), 1)
        self.fraction = 0.95
        self.tick.once()
        self.tick.once()
        self.assertEqual(self.guard(), {"held": True})
        self.assertEqual(len(self.runs("reviewer")), 1)

    def test_a_checkpoint_restart_still_launches_while_held(self):
        cut = tt.Result(None, 1, tt.TOKENS, 1, "max_turns", None, "sess-1", "")
        self.script["implementer"] = [[cut, tt.CHECKPOINT], [tt.IMPL_DONE]]
        self.fraction = 0.5
        self.tick.once()
        self.assertEqual(self.task()["status"], "running")
        self.fraction = 0.95
        self.tick.once()
        self.assertEqual(self.guard(), {"held": True})
        first, second = self.runs("implementer")[:2]
        self.assertEqual(first["outcome"], "checkpoint")
        self.assertEqual(second["resumed_from"], first["run_id"])

    def test_a_run_in_flight_is_untouched_while_held(self):
        self.tick.spawn = lambda *a: None      # the worker never finishes: the run stays in flight
        self.fraction = 0.5
        self.tick.once()
        self.assertEqual(self.task()["status"], "running")
        before = [dict(r) for r in self.runs()]
        self.fraction = 0.95
        self.tick.once()
        self.assertEqual(self.task()["status"], "running")
        self.assertEqual([dict(r) for r in self.runs()], before)

    def test_under_the_line_dispatches_with_no_event(self):
        self.fraction = 0.5
        self.tick.once()
        self.assertEqual(len(self.runs("implementer")), 1)
        self.assertEqual(self.kinds("weekly_pause") + self.kinds("weekly_resume"), [])

    def test_no_meter_signal_dispatches(self):
        self.fraction = None
        self.tick.once()
        self.assertEqual(len(self.runs("implementer")), 1)
        self.assertEqual(self.kinds("weekly_pause") + self.kinds("weekly_resume"), [])

    def test_back_under_the_line_resumes_once_and_dispatches(self):
        self.tick.once()
        self.fraction = 0.5
        self.tick.once()
        self.tick.once()
        self.assertEqual(len(self.runs("implementer")), 1)
        self.assertEqual(len(self.kinds("weekly_pause")), 1)
        self.assertEqual(self.kinds("weekly_resume"), [{"pct": 50, "reset_at": 604800}])


class BlindWhileHeld(bd.BlindBoard):
    def step_at(self, fraction):
        reading_ = reading(fraction)
        with mock.patch.object(weekly_pause, "meter", lambda *a: reading_):
            weekly_pause.held(self.conn, self.home, int(time.time()))
        self.step()

    def test_no_tester_starts_for_a_ready_card_while_held_and_one_does_after(self):
        card = self.card_with("ready")
        self.step_at(0.95)
        self.assertEqual(self.blind_runs(), [])
        self.assertEqual(db.guard_get(self.conn, "weekly_pause")[0], {"held": True})
        self.step_at(0.5)
        self.assertEqual(self.covered(), {card})
