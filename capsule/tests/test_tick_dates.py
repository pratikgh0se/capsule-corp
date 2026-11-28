"""Tick tests do not depend on the date (main red 2026-11-02, see no_digest.py)."""
import datetime

from capsule.tests.test_tick import TickCase


class DateIndependence(TickCase):
    def test_a_tick_opens_no_eval_incident_before_or_after_a_week_closes(self):
        # Sunday noon, then Monday noon local: the Monday tick is the first that sees the week of
        # Oct 19 closed (7-day delay) and, unpatched, opens an eval contradiction incident.
        for day in ((2026, 11, 1), (2026, 11, 2)):
            with self.subTest(day=day):
                self.tick.once(now=int(datetime.datetime(*day, 12).timestamp()))
                kinds = [r[0] for r in self.conn.execute("SELECT kind FROM incidents")]
                self.assertEqual(kinds, [])
