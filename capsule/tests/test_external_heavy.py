"""Card t_6fb6f907 (C11): `caps.external_heavy` heavy seats the tick never uses (the separate
Other-app chat dispatches its own heavy agents by hand; row 600 Q3 static split)."""
import copy
import json
import unittest

from capsule import config, db, dispatch, seats
from capsule.tests import test_landing
from capsule.tests.test_seats import CFG, SeatCase


def with_caps(**caps):
    cfg = copy.deepcopy(CFG)
    cfg["caps"].update(caps)
    return cfg


class ExternalHeavy(SeatCase):
    def setUp(self):
        super().setUp()
        self.other = self.mkboard("alt-app")      # `max_running` is per board: seats held elsewhere

    def ready(self, cfg):
        return dispatch.ready(self.own, cfg, {"strong"}, 2000, self.home)

    def test_default_is_zero_and_changes_nothing(self):
        cfg = copy.deepcopy(CFG)
        del cfg["caps"]["external_heavy"]          # the key absent: the default applies
        self.assertEqual(config.cap(cfg, "external_heavy"), 0)
        self.assertEqual(seats.limits(cfg), (2, 3))
        for _ in range(2):
            self.running_impl(self.other)
        self.card(self.own)
        self.assertEqual(len(self.ready(cfg)), 1)

    def test_with_one_external_the_third_heavy_dispatch_is_deferred(self):
        cfg = with_caps(max_heavy=3, external_heavy=1)
        self.assertEqual(seats.limits(cfg)[1], 2)
        for _ in range(2):
            self.running_impl(self.other)
        self.card(self.own)
        self.assertEqual(self.ready(cfg), [])

    def test_with_one_external_two_heavy_still_dispatch(self):
        cfg = with_caps(max_heavy=3, external_heavy=1)
        self.running_impl(self.other)
        self.card(self.own)
        self.assertEqual(len(self.ready(cfg)), 1)

    def test_a_review_start_defers_with_the_external_reason(self):
        cfg = with_caps(max_review=2, max_heavy=3, external_heavy=1)
        for _ in range(2):
            self.running_impl(self.other)
        self.card(self.own)
        task = self.own.execute("SELECT * FROM tasks LIMIT 1").fetchone()
        got = dispatch.admit_reviews(self.own, cfg, self.home, task, ["guide"], 2000)
        self.assertEqual(got, [])
        reason = self.own.execute(
            "SELECT payload FROM task_events WHERE kind='review_deferred'").fetchone()[0]
        self.assertIn("heavy_cap_external", reason)

    def test_hold_for_guide_and_korin_runs_counts_the_external_seat(self):
        """`dispatch.hold` serves guide rulings and Korin runs alike."""
        stub = type("T", (), {"home": self.home, "_paused": lambda *a: False})()
        route = {"account": "a"}
        self.running_impl(self.other)
        self.assertFalse(dispatch.hold(stub, self.own, with_caps(external_heavy=0), route, 2000))
        cfg = with_caps(max_heavy=3, external_heavy=1)
        self.assertFalse(dispatch.hold(stub, self.own, cfg, route, 2000))
        self.running_impl(self.other)
        self.assertTrue(dispatch.hold(stub, self.own, cfg, route, 2000))

    def test_without_external_seats_the_deferral_reason_stays_heavy_cap(self):
        cfg = with_caps(max_review=2, max_heavy=3, external_heavy=0)
        for _ in range(2):
            self.running_impl(self.other)
        self.card(self.own)
        task = self.own.execute("SELECT * FROM tasks LIMIT 1").fetchone()
        got = dispatch.admit_reviews(self.own, cfg, self.home, task, ["guide", "validator"], 2000)
        self.assertEqual(got, ["guide"])
        reason = self.own.execute(
            "SELECT payload FROM task_events WHERE kind='review_deferred'").fetchone()[0]
        self.assertEqual(json.loads(reason)["reason"], "heavy_cap")

    def test_external_above_max_heavy_floors_the_cap_at_zero(self):
        self.assertEqual(seats.heavy_cap(with_caps(max_heavy=3, external_heavy=5)), 0)

    def test_negative_or_non_int_is_rejected_at_load(self):
        for bad in (-1, 1.5, "1", True, None):
            with self.assertRaises(ValueError, msg=repr(bad)):
                config.validate(with_caps(external_heavy=bad))
        config.validate(with_caps(external_heavy=0))
        config.validate(with_caps(external_heavy=1))


class ExternalHeavyLanding(test_landing.Landing):
    def test_external_seat_leaves_no_heavy_seat_for_the_lander(self):
        self.put("a.txt", "work\n", "card work")
        self.cfg["caps"] = {"max_heavy": 2, "external_heavy": 1}
        other = db.new_task(self.conn, title="run", assignee="dev", status="ready")
        self.conn.execute("UPDATE tasks SET status='running' WHERE id=?", (other,))
        before = self.remote_main()
        self.assertEqual(self.land()["outcome"], "no_seat")
        self.assertEqual(self.remote_main(), before)

    def test_without_the_external_seat_the_same_board_lands(self):
        self.put("a.txt", "work\n", "card work")
        self.cfg["caps"] = {"max_heavy": 2, "external_heavy": 0}
        other = db.new_task(self.conn, title="run", assignee="dev", status="ready")
        self.conn.execute("UPDATE tasks SET status='running' WHERE id=?", (other,))
        self.assertNotEqual(self.land()["outcome"], "no_seat")


if __name__ == "__main__":
    unittest.main()
