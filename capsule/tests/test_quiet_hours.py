"""M189: quiet hours enforced sender-side. `now` is always injected; nothing reaches Slack."""
import datetime
import json
import os
import time
import unittest
from unittest import mock
from zoneinfo import ZoneInfo

from capsule import config, intent, outbox, paths, slack
from capsule.tests.test_interrupts import LINES
from capsule.tests.test_slack import board

ZONE = "Asia/Kolkata"       # a test value: the real zone is the owner's to confirm
QUIET = {"quiet_hours": {"enabled": True, "timezone": ZONE}}
# the owner's rule (D59): 20:30-07:30
NIGHT = {"quiet_hours": {"enabled": True, "timezone": ZONE, "start": "20:30", "end": "07:30"}}
BASE = json.loads((paths.REPO / "capsule" / "config.example.json").read_text())


def at(hh, mm=0, day=27):
    return int(datetime.datetime(2026, 10, day, hh, mm, tzinfo=ZoneInfo(ZONE)).timestamp())


def local(hh, mm=0):
    return datetime.datetime(2026, 10, 27, hh, mm)


class QuietNow(unittest.TestCase):
    def test_window_wraps_midnight_start_inclusive_end_exclusive(self):
        for hh, mm, want in ((21, 59, False), (22, 0, True), (23, 0, True), (3, 0, True),
                             (8, 29, True), (8, 30, False), (8, 31, False), (12, 0, False)):
            with self.subTest(at=f"{hh}:{mm:02d}"):
                self.assertIs(outbox._quiet_now(local(hh, mm), QUIET), want)

    def test_off_when_missing_or_disabled(self):
        off = {"enabled": False, "timezone": ZONE}
        for cfg in ({}, None, {"quiet_hours": {}}, {"quiet_hours": off}):
            with self.subTest(cfg=cfg):
                self.assertIs(outbox._quiet_now(local(23), cfg), False)

    def test_same_day_window_and_custom_edges(self):
        cfg = {"quiet_hours": {"enabled": True, "timezone": ZONE, "start": "13:00", "end": "14:00"}}
        self.assertIs(outbox._quiet_now(local(13, 30), cfg), True)
        self.assertIs(outbox._quiet_now(local(23), cfg), False)


class Flush(unittest.TestCase):
    def setUp(self):
        self.conn = board()
        outbox.set_slack(self.conn, True, now=at(12))
        self.sent = []

    def send(self, row):
        self.sent.append((row["channel"], row["body"]))

    def queue(self, cls, now):
        text = slack.render(cls, f"{cls} line")
        outbox.add(self.conn, "slack", text, now=now)
        return text

    def status(self):
        return [r["status"] for r in self.conn.execute("SELECT status FROM outbox ORDER BY id")]

    def test_non_interrupt_waits_at_2300_and_goes_at_0831(self):
        text = self.queue("done", at(22, 55))
        self.assertEqual(outbox.flush(self.conn, self.send, now=at(23), quiet=QUIET), 0)
        self.assertEqual((self.sent, self.status()), ([], ["pending"]))
        self.assertEqual(self.conn.execute("SELECT attempts FROM outbox").fetchone()[0], 0)
        self.assertEqual(outbox.flush(self.conn, self.send, now=at(8, 31), quiet=QUIET), 1)
        self.assertEqual((self.sent, self.status()), ([("slack", text)], ["delivered"]))

    def test_every_other_class_waits_too(self):
        for cls in ("done", "scheduled", "running", "needs_input"):
            self.queue(cls, at(22, 55))
        self.assertEqual(outbox.flush(self.conn, self.send, now=at(23), quiet=QUIET), 0)
        self.assertEqual(self.status(), ["pending"] * 4)

    def test_incident_sends_at_2300(self):
        text = self.queue("incident", at(22, 55))
        self.assertEqual(outbox.flush(self.conn, self.send, now=at(23), quiet=NIGHT), 1)
        self.assertEqual(self.sent, [("slack", text)])
        self.assertEqual(outbox.QUIET_INTERRUPTS, ("incident", "page"))   # page: routing v2 only

    def test_blocked_waits_at_2300_goes_at_0731_and_at_once_at_1000(self):
        """Row 635 a: a routine needs-you line waits for the morning release like the rest."""
        text = self.queue("blocked", at(22, 55))
        self.assertEqual(outbox.flush(self.conn, self.send, now=at(23), quiet=NIGHT), 0)
        self.assertEqual((self.sent, self.status()), ([], ["pending"]))
        self.assertEqual(outbox.flush(self.conn, self.send, now=at(7, 31, day=28), quiet=NIGHT), 1)
        self.assertEqual((self.sent, self.status()), ([("slack", text)], ["delivered"]))
        later = self.queue("blocked", at(10, day=28))
        self.assertEqual(outbox.flush(self.conn, self.send, now=at(10, day=28), quiet=NIGHT), 1)
        self.assertEqual(self.sent[-1], ("slack", later))

    def test_factory_down_or_stuck_blocked_lines_still_send_at_2300(self):
        """The real emitters' lines (keeper silent, boards paused) ride the blocked class; the
        factory_down_or_stuck words of the D13 table lift them."""
        lines = [LINES["keeper_silent"], LINES["boards_paused"]]
        for text in lines:
            outbox.add(self.conn, "slack", text, now=at(22, 55))
        self.assertEqual(outbox.flush(self.conn, self.send, now=at(23), quiet=NIGHT), 2)
        self.assertEqual(self.sent, [("slack", text) for text in lines])

    def test_other_table_words_do_not_break_quiet_hours(self):
        """Row 635 a is incidents and factory down or stuck only: `security` and `secret` are D13
        words for redesign.interrupts, not for the night."""
        for text in (slack.render("blocked", "Decide t_0123abcd: pin the security scanner?"),
                     slack.render("needs_input", "t_0123abcd a worker read a secret file")):
            outbox.add(self.conn, "slack", text, now=at(22, 55))
        self.assertEqual(outbox.flush(self.conn, self.send, now=at(23), quiet=NIGHT), 0)
        self.assertEqual(self.sent, [])
        self.assertEqual([r["attempts"] for r in self.conn.execute("SELECT attempts FROM outbox")],
                         [0, 0])

    def test_a_table_without_the_factory_entry_holds_blocked_and_sends_the_page(self):
        table = {"budget_nearly_gone": {"classes": ["paused"], "words": ["budget"]}}
        self.queue("blocked", at(22, 55))
        page = self.queue("incident", at(22, 56))
        with mock.patch.object(outbox, "_events", return_value=table):
            self.assertEqual(outbox.flush(self.conn, self.send, now=at(23), quiet=NIGHT), 1)
        self.assertEqual(self.sent, [("slack", page)])

    def test_an_unreadable_event_table_falls_back_to_the_old_pass_list(self):
        """Incident and blocked pass, as before row 635 a; everything else still waits."""
        passing = [self.queue("blocked", at(22, 55)), self.queue("incident", at(22, 55))]
        self.queue("done", at(22, 55))
        with mock.patch.object(outbox, "_events", return_value=None):
            self.assertEqual(outbox.flush(self.conn, self.send, now=at(23), quiet=NIGHT), 2)
        self.assertEqual(self.sent, [("slack", text) for text in passing])
        self.assertEqual(self.status(), ["delivered", "delivered", "pending"])

    def test_a_page_sends_with_interrupts_on_at_noon_and_at_2300(self):
        """Row 638 d: `redesign.interrupts` holds nothing back from a page, day or night; a
        blocked line at 23:00 still waits (the whitelist does not list it); at noon an ask and a
        blocked line quoting 🛑 still wait."""
        cfg = dict(NIGHT, redesign={"interrupts": True})
        for hour in (12, 23):
            self.sent.clear()
            text = self.queue("page", at(hour) - 60)
            self.assertEqual(outbox.flush(self.conn, self.send, now=at(hour), quiet=cfg), 1)
            self.assertEqual(self.sent, [("slack", text)])
        held = self.queue("blocked", at(22, 55))
        self.assertEqual(outbox.flush(self.conn, self.send, now=at(23, 5), quiet=cfg), 0)
        self.assertEqual((held in [b for _, b in self.sent], self.status()[-1]),
                         (False, "pending"))
        for body in (slack.render("ask", "Which card should run next?"),
                     slack.render("blocked", "🛑 Docker is down, retry?")):
            outbox.add(self.conn, "slack", body, now=at(11, 55))
        self.assertEqual(outbox.flush(self.conn, self.send, now=at(12), quiet=cfg), 0)
        self.assertEqual(self.status()[-3:], ["pending"] * 3)

    def test_a_page_has_no_age_gate_and_is_read_by_its_leading_glyph(self):
        """Loophole hunt 2: a page queued 3 h before the flush still sends; a page that quotes
        📬 and 🔴 later in its text is still a page (leading glyph, not containment)."""
        cfg = dict(NIGHT, redesign={"interrupts": True})
        old = self.queue("page", at(20))
        quoting = slack.render("page", "Docker is down") + " (was 📬 an ask, then 🔴 blocked)"
        outbox.add(self.conn, "slack", quoting, now=at(22, 55))
        self.assertEqual(outbox.flush(self.conn, self.send, now=at(23), quiet=cfg), 2)
        self.assertEqual(self.sent, [("slack", old), ("slack", quoting)])

    def test_an_old_ask_stays_pending_at_2300_with_interrupts_on(self):
        """Loophole hunt 2: age never admits an ask; queued at 09:00, still held at 23:00."""
        cfg = dict(NIGHT, redesign={"interrupts": True})
        self.queue("ask", at(9))
        self.assertEqual(outbox.flush(self.conn, self.send, now=at(23), quiet=cfg), 0)
        self.assertEqual((self.sent, self.status()), ([], ["pending"]))
        self.assertEqual(self.conn.execute("SELECT attempts FROM outbox").fetchone()[0], 0)

    def test_an_event_word_lifts_a_blocked_line_by_its_headline_only(self):
        """Loophole hunt 2: `irreversible` is a D13 word. In the headline it sends at 23:00; on
        the second line of the same 🔴 body it does not, so the body must wait."""
        cfg = dict(NIGHT, redesign={"interrupts": True})
        lifted = slack.render("blocked", "t_0123abcd irreversible override waits on you")
        later = slack.render("blocked", "Pick the next card") + "\nirreversible override waits"
        for body in (later, lifted):
            outbox.add(self.conn, "slack", body, now=at(22, 55))
        self.assertEqual(outbox.flush(self.conn, self.send, now=at(23), quiet=cfg), 1)
        self.assertEqual((self.sent, self.status()),
                         ([("slack", lifted)], ["pending", "delivered"]))

    def test_an_ask_and_a_needs_input_line_wait_at_2300_with_interrupts_on(self):
        """Row 638 g: neither `ask` nor `needs_input` is a page or a D13 event, so both stay
        pending with no attempt used, even at night."""
        cfg = dict(NIGHT, redesign={"interrupts": True})
        for cls in ("ask", "needs_input"):
            self.queue(cls, at(22, 55))
        self.assertEqual(outbox.flush(self.conn, self.send, now=at(23), quiet=cfg), 0)
        self.assertEqual((self.sent, self.status()), ([], ["pending"] * 2))
        self.assertEqual([r["attempts"] for r in self.conn.execute("SELECT attempts FROM outbox")],
                         [0, 0])

    def test_an_unreadable_table_with_interrupts_on_keeps_the_night_fallback(self):
        """Row 638 g: with `redesign.interrupts` on and `interrupts.json` unreadable, the quiet
        window decides: at 23:00 only `QUIET_FALLBACK` classes go out, an ask waits."""
        cfg = dict(NIGHT, redesign={"interrupts": True})
        sent = [self.queue(cls, at(22, 55)) for cls in ("incident", "blocked", "page")]
        self.queue("ask", at(22, 55))
        with mock.patch.object(outbox, "_events", return_value=None):
            self.assertEqual(outbox.flush(self.conn, self.send, now=at(23), quiet=cfg), 3)
        self.assertEqual(self.sent, [("slack", text) for text in sent])
        self.assertEqual(self.status(), ["delivered"] * 3 + ["pending"])

    def test_an_incident_behind_many_held_rows_still_sends(self):
        """The hold runs before the 10-row slack cap, so held rows never crowd an interrupt out."""
        for _ in range(outbox.SEND_CAP_PER_PASS + 2):
            self.queue("needs_input", at(22, 50))
        page = self.queue("incident", at(22, 55))
        self.assertEqual(outbox.flush(self.conn, self.send, now=at(23), quiet=QUIET), 1)
        self.assertEqual(self.sent, [("slack", page)])
        self.assertEqual(self.status().count("pending"), outbox.SEND_CAP_PER_PASS + 2)

    def test_file_rows_always_send(self):
        outbox.add(self.conn, "file", "capsule: done", now=at(22, 55))
        self.assertEqual(outbox.flush(self.conn, self.send, now=at(23), quiet=QUIET), 1)

    def test_a_mixed_queue_sends_only_the_interrupt(self):
        self.queue("done", at(22, 55))
        page = self.queue("incident", at(22, 56))
        outbox.flush(self.conn, self.send, now=at(23), quiet=QUIET)
        self.assertEqual((self.sent, self.status()), ([("slack", page)], ["pending", "delivered"]))

    def test_absent_and_disabled_config_match_today_exactly(self):
        """Z2: the same queue flushed with no config, `{}`, no block, and a disabled block gives
        the same sends, statuses and times as the call with no `quiet` argument (main's call)."""
        def run(**kw):
            self.conn, self.sent = board(), []
            for cls in ("done", "incident", "running", "blocked"):
                self.queue(cls, at(22, 55))
            outbox.add(self.conn, "file", "capsule: x", now=at(22, 55))
            n = outbox.flush(self.conn, self.send, now=at(23), **kw)
            rows = [tuple(r) for r in self.conn.execute(
                "SELECT status, attempts, delivered_at FROM outbox ORDER BY id")]
            return n, self.sent, rows

        want = run()
        self.assertEqual(want[0], 5)
        for cfg in (None, {}, {"quiet_hours": {}},
                    {"quiet_hours": {"enabled": False, "timezone": ZONE}}):
            with self.subTest(cfg=cfg):
                self.assertEqual(run(quiet=cfg), want)


class Validate(unittest.TestCase):
    def cfg(self, block):
        return dict(BASE, quiet_hours=block)

    def test_valid_blocks(self):
        for block in ({}, {"enabled": False}, {"enabled": True, "timezone": ZONE},
                      {"enabled": True, "timezone": "UTC", "start": "23:30", "end": "07:00"}):
            with self.subTest(block=block):
                self.assertEqual(config.validate(self.cfg(block))["quiet_hours"], block)
        self.assertIs(config.validate(BASE), BASE)       # the example block: off, zone left empty
        bare = {k: v for k, v in BASE.items() if k != "quiet_hours"}
        self.assertIs(config.validate(bare), bare)       # no block at all is off

    def test_bad_blocks_refused(self):
        for block in ("on", [], {"enabled": "yes"}, {"enabled": True},
                      {"enabled": True, "timezone": ""}, {"timezone": "Mars/Base"},
                      {"timezone": 5}, {"start": "25:00"}, {"end": "8:30"}, {"start": 2200},
                      {"start": "08:30", "end": "08:30"}, {"surprise": 1}):
            with self.subTest(block=block), self.assertRaises(ValueError):
                config.validate(self.cfg(block))

    def test_example_config_leaves_it_off(self):
        self.assertIs(config.quiet_hours(BASE)["enabled"], False)


class IntentExamples(unittest.TestCase):
    """contracts/intent/quiet_hours.jsonl, replayed through the real outbox flush (Z1)."""

    def replay(self, doc):
        """(what was sent as (channel, body), the row's (status, attempts)) at doc's local time."""
        hh, mm = map(int, doc["now_local"].split(":"))
        zone = ZoneInfo(doc["quiet_hours"]["timezone"])
        now = int(datetime.datetime(2026, 10, 27, hh, mm, tzinfo=zone).timestamp())
        conn, sent = board(), []
        outbox.add(conn, doc["row"]["channel"], doc["row"]["body"], now=now - 60)
        outbox.flush(conn, sent.append, now=now, quiet={
            "quiet_hours": doc["quiet_hours"], "redesign": doc.get("redesign")})
        row = conn.execute("SELECT status, attempts FROM outbox").fetchone()
        return [(r["channel"], r["body"]) for r in sent], (row["status"], row["attempts"])

    def test_every_example_holds_on_the_real_code(self):
        examples = intent.load("quiet_hours")
        self.assertEqual(len({e["id"] for e in examples}), len(examples))     # ids are unique
        self.assertEqual(len(examples), 12)          # an absent file would pass vacuously
        for ex in examples:
            with self.subTest(ex["id"]):
                doc, exp = ex["given"]["document"], ex["expect"]
                self.assertLessEqual(set(exp), {"sent", "status", "attempts"})   # none unchecked
                self.assertEqual(doc["check"], "quiet_hours")
                self.assertLessEqual(set(doc), {"check", "quiet_hours", "row", "now_local",
                                                "redesign"})
                sent, state = self.replay(doc)
                self.assertEqual(sent, [(doc["row"]["channel"], doc["row"]["body"])]
                                 if exp["sent"] else [])
                self.assertEqual("status" in exp, "attempts" in exp)
                if "status" in exp:
                    self.assertEqual(state, (exp["status"], exp["attempts"]))

    def test_the_machines_own_zone_changes_no_example(self):
        """A reading of the host's local time instead of the configured zone would differ here."""
        docs = [e["given"]["document"] for e in intent.load("quiet_hours")]
        want = [self.replay(d) for d in docs]
        was = os.environ.get("TZ")
        self.addCleanup(self.restore_tz, was)
        os.environ["TZ"] = "America/New_York"
        time.tzset()
        self.assertEqual([self.replay(d) for d in docs], want)

    @staticmethod
    def restore_tz(was):
        if was is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = was
        time.tzset()


if __name__ == "__main__":
    unittest.main()
