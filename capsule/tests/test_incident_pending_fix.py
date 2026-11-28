"""A recurrence of a fix_pending incident attaches and pages, and opens none (inc_8e3ce9d2)."""
import json

from capsule import db, incident_inputs, incidents
from capsule.tests.test_incidents import DAY, T0, Base

KEY = "implementer: fatal: Not logged in"


class PendingFix(Base):
    def pend(self, closure=None):
        a, _ = self.open(key=KEY)
        self.conn.execute("UPDATE incidents SET status='fix_pending', fix_card='t_0000aaaa',"
                          " closure=? WHERE id=?", (closure, a))
        return a

    def lines(self):
        return [r[0] for r in self.conn.execute("SELECT body FROM outbox WHERE channel='file'")]

    def test_a_crash_matching_a_fix_pending_incident_on_a_later_day_attaches_and_opens_none(self):
        a = self.pend()
        b, act = self.open(key=KEY, now=T0 + 4 * DAY, evidence=["event:2"])
        self.assertEqual((b, act), (a, "recurred"))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM incidents").fetchone()[0], 1)
        self.assertIn("event:2", json.loads(self.conn.execute(
            "SELECT evidence FROM incidents WHERE id=?", (a,)).fetchone()[0]))

    def test_the_recurrence_pages_with_the_fix_card_and_the_key(self):
        a = self.pend()
        self.open(key=KEY, now=T0 + 4 * DAY)
        want = (f"RECURRED {a} while fix card t_0000aaaa is pending:"
                " implementer: fatal: not logged in")
        self.assertTrue(any(want in line for line in self.lines()), self.lines())

    def test_two_recurrences_while_the_fix_is_pending_page_once(self):
        a = self.pend()
        for day in (4, 5):
            self.assertEqual(self.open(key=KEY, now=T0 + day * DAY, evidence=[f"event:{day}"]),
                             (a, "recurred"))
        self.assertEqual(len([ln for ln in self.lines() if "RECURRED" in ln]), 1, self.lines())
        self.assertEqual(len(json.loads(self.conn.execute(
            "SELECT evidence FROM incidents WHERE id=?", (a,)).fetchone()[0])), 3)

    def test_a_wall_unavailable_recurrence_records_but_sends_no_page(self):
        wall = "wall_unavailable: exit 1: host wall down"
        a, _ = self.open(key=wall)
        self.conn.execute("UPDATE incidents SET status='fix_pending', fix_card='t_0000aaaa'"
                          " WHERE id=?", (a,))
        self.assertEqual(self.open(key=wall, now=T0 + 4 * DAY), (a, "recurred"))
        self.assertEqual([ln for ln in self.lines() if "RECURRED" in ln], [])
        self.pend()
        self.open(key=KEY, now=T0 + 4 * DAY)
        self.assertEqual(len([ln for ln in self.lines() if "RECURRED" in ln]), 1)

    def test_a_later_fix_pending_period_pages_again(self):
        a = self.pend()
        self.open(key=KEY, now=T0 + 4 * DAY)
        self.conn.execute("UPDATE incidents SET status='closed', closed_at=? WHERE id=?",
                          (T0 + 5 * DAY, a))
        b = self.pend()
        self.conn.execute("UPDATE incidents SET fix_card='t_0000bbbb' WHERE id=?", (b,))
        self.open(key=KEY, now=T0 + 9 * DAY)
        self.assertEqual(len([ln for ln in self.lines() if "RECURRED" in ln]), 2, self.lines())

    def test_the_recurrence_writes_an_incident_opened_event_on_the_card(self):
        self.pend()
        tid = db.new_task(self.conn, title="t", body="b")
        _, act = self.open(key=KEY, now=T0 + 4 * DAY, task_id=tid)
        self.assertEqual(act, "recurred")
        payloads = [json.loads(r[0]) for r in self.conn.execute(
            "SELECT payload FROM task_events WHERE kind='incident_opened' AND task_id=?", (tid,))]
        self.assertEqual([p["action"] for p in payloads], ["recurred"])

    def test_open_and_postmortem_incidents_still_open_a_new_one_on_a_later_day(self):
        for status in ("open", "postmortem"):
            self.conn.execute("DELETE FROM incidents")
            a = self.pend()
            self.conn.execute("UPDATE incidents SET status=? WHERE id=?", (status, a))
            b, act = self.open(key=KEY, now=T0 + 4 * DAY)
            self.assertEqual((act, b != a), ("opened", True), status)

    def test_a_closed_incident_within_30_days_still_opens_a_repeat(self):
        a = self.pend()
        self.conn.execute("UPDATE incidents SET status='closed', closed_at=? WHERE id=?",
                          (T0 + DAY, a))
        _, act = self.open(key=KEY, now=T0 + 10 * DAY)
        self.assertEqual(act, "repeat")

    def test_a_fix_pending_watch_does_not_absorb_the_recurrence(self):
        a = self.pend(closure="watch")
        b, act = self.open(key=KEY, now=T0 + 4 * DAY)
        self.assertNotEqual(b, a)
        self.assertEqual(act, "repeat")


class SummaryHistory(Base):
    def close_as(self, key, closure):
        a, _ = self.open(key=key)
        self.conn.execute("UPDATE incidents SET status='closed', closure=?, closed_at=?"
                          " WHERE id=?", (closure, T0, a))

    def test_summary_counts_history_closes_apart_from_fixed_and_watch(self):
        for n, closure in enumerate(("fixed", "watch", "history", "history")):
            self.close_as(f"cause {n} {'x' * n}", closure)
        got = incidents.summary(self.conn)
        self.assertEqual((got["closed_history"], got["closed"], got["total"]), (2, 4, 4))

    def test_an_empty_board_has_no_history_closes(self):
        self.assertEqual(incidents.summary(self.conn)["closed_history"], 0)


class DecisionExcerpts(Base):
    """Korin's board-event line keeps a blind card's criterion numbers, no excerpt, no test id."""

    CONTEXT = ("Criterion 2: it exits 1\n  Check blind.test_x.T.test_one\n    given: SECRETGIVEN\n"
               "    expect: SECRETEXPECT\n    observed: SECRETSEEN")

    def line(self, blind):
        tid = db.new_task(self.conn, title="t", body="b")
        if blind:
            db.event(self.conn, tid, "blind_gated", {"counts": {}})
        db.event(self.conn, tid, "decision_requested",
                 {"schema": "decision.v1", "context": self.CONTEXT})
        row = self.conn.execute(
            "SELECT * FROM task_events WHERE kind='decision_requested'").fetchone()
        return incident_inputs.event_line(self.conn, row)

    def test_a_blind_card_keeps_the_criterion_number_but_no_excerpt_or_test_id(self):
        line = self.line(True)
        self.assertNotIn("blind.test_x.T.test_one", line)
        self.assertIn("Criterion 2", line)
        for text in ("SECRETGIVEN", "SECRETEXPECT", "SECRETSEEN"):
            self.assertNotIn(text, line)

    def test_a_card_without_blind_tests_is_unchanged(self):
        line = self.line(False)
        for text in ("SECRETGIVEN", "SECRETEXPECT", "SECRETSEEN"):
            self.assertIn(text, line)
