"""The dedupe window is a rolling 24 hours, not a UTC calendar day (design 2026-11-28)."""
from capsule import incidents
from capsule.tests.test_incidents import DAY, Base, T0

LATE = T0 - 3600 * 2 - 1800     # 22:30 UTC the day before T0 (T0 is 01:00 UTC)


class RollingWindow(Base):
    def test_events_either_side_of_midnight_utc_are_one_incident(self):
        a, act_a = self.open(now=LATE + 3600)       # 23:30 UTC
        b, act_b = self.open(now=LATE + 7200)       # 00:30 UTC, next day
        self.assertEqual((a, act_a, act_b), (b, "opened", "deduped"))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM incidents").fetchone()[0], 1)

    def test_a_day_and_a_second_later_opens_a_new_incident(self):
        a, _ = self.open(now=T0)
        b, act = self.open(now=T0 + DAY - 1)
        self.assertEqual((a, act), (b, "deduped"))
        c, act = self.open(now=T0 + DAY)            # exactly 24 h: strict, so a new one
        self.assertNotEqual(a, c)
        self.assertEqual(act, "opened")
        self.assertEqual(len(incidents.listing(self.conn)), 2)
