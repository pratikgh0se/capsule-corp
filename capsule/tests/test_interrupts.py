# mutation-check: targets-predate-range ff625e94 -- fixture rename only, no behaviour pinned ::
#   Whitelist.test_a_word_counts_whole_and_only_on_a_line_that_asks_for_owner
"""R38 (S13a, D13): with `redesign.interrupts` on, only the four D13 events leave Slack at once;
everything else waits for the batch. Nothing reaches Slack: `send` is a list append and `now` is
injected. The event table is `capsule/interrupts.json` (a PROTECTED file).

Only the factory and budget events have real emitters today (keeper-silent, boards paused,
incident pages, the rate-limit pause); the security and irreversible entries are table rows whose
emitter is pending, so their tests use a worded line of the right class. Every other line comes
from the real call sites (Z2)."""
import datetime
import io
import json
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

from capsule import db, intent, keeper_watch, outbox, slack, slack_digest, slack_msgs
from capsule.tests.test_slack import board

ON = {"redesign": {"interrupts": True}}
ZONE = "Asia/Kolkata"


def at(hh):
    return int(datetime.datetime(2026, 10, 27, hh, tzinfo=ZoneInfo(ZONE)).timestamp())


NOON, NIGHT = at(12), at(23)
QUIET = {"quiet_hours": {"enabled": True, "timezone": ZONE}}


def real_lines():
    """{name: the rendered slack body}: the tick's error page (tick.py), keeper-silent
    (keeper_watch._outbox) and boards-paused (slack_digest._pause_line) made by the real emitters on
    a throwaway board; the rate-limit pause (tick.py) and a done line are the real templates
    (`done` and `paused` are LOG_ONLY classes: today they never post, so only `add` makes a row)."""
    home = Path(tempfile.mkdtemp())
    db.create_board(home / "boards" / "capsule-corp" / "kanban.db")
    conn = db.connect(home / "boards" / "capsule-corp" / "kanban.db")
    outbox.set_slack(conn, True)
    made = {}

    def grab(name, emit):
        emit()
        made[name] = conn.execute("SELECT body FROM outbox WHERE channel='slack'"
                                  " ORDER BY id DESC").fetchone()[0]

    grab("tick_error", lambda: outbox.notify(
        conn, "incident", "t_0123abcd tick error: OperationalError"))
    made["rate_limit"] = slack.render(     # the tick's call; `paused` is LOG_ONLY, so notify
        "paused", "capsule-corp paused: rate limit on the account",    # makes no slack row
        fields=[("until", "2026-10-27 23:00")])
    grab("keeper_silent", lambda: keeper_watch._outbox(
        home, "capsule-corp", "keeper silent 75 min with 3 waiting", "tick ok"))
    grab("boards_paused", lambda: outbox.notify(
        conn, "blocked", slack_digest._pause_line(home, "capsule-corp", NOON),
        key="broken:paused"))
    made["done_secret"] = slack.render("done", "t_0123abcd done: add secret-read guard")
    conn.close()
    shutil.rmtree(home, True)
    return made


LINES = real_lines()
# one message per D13 event: spec D13 (factory down or stuck; security or secrets; an irreversible
# action waiting; budget nearly gone). The middle two are worded lines: no emitter says them yet.
EVENTS = {
    "factory_down_or_stuck": LINES["tick_error"],
    "security_or_secrets": slack.render("needs_input", "t_0123abcd a worker read a secret file"),
    "irreversible_action_waiting": slack.render(
        "blocked", "t_0123abcd irreversible override waits on you"),
    "budget_nearly_gone": LINES["rate_limit"],
}


class Whitelist(unittest.TestCase):
    def setUp(self):
        self.conn = board()
        outbox.set_slack(self.conn, True, now=NOON)
        self.sent = []

    def send(self, row):
        self.sent.append(row["body"])

    def queue(self, text, **kw):
        return outbox.add(self.conn, "slack", text, now=NOON, **kw)

    def flush(self, cfg=ON, now=NOON):
        return outbox.flush(self.conn, self.send, now=now, quiet=cfg)

    def status(self):
        return [r["status"] for r in self.conn.execute("SELECT status FROM outbox ORDER BY id")]

    def test_a_non_whitelist_event_waits_for_the_batch(self):
        for cls, head in (("done", "t_0123abcd done: add a flag"),
                          ("blocked", "t_0123abcd needs you"),
                          ("needs_input", "t_0123abcd over the file ceiling"),
                          ("running", "t_0123abcd dispatched")):
            self.queue(slack.render(cls, head))
        self.assertEqual(self.flush(), 0)
        self.assertEqual((self.sent, self.status()), ([], ["pending"] * 4))
        self.assertEqual(self.conn.execute("SELECT MAX(attempts) FROM outbox").fetchone()[0], 0)

    def test_each_d13_event_goes_out_at_once(self):
        for event, text in EVENTS.items():
            self.queue(text)
            with self.subTest(event=event):
                self.assertEqual(self.flush(), 1)
                self.assertEqual(self.sent[-1], text)

    def test_keeper_silent_and_boards_paused_lines_go_out_at_once(self):
        for name in ("keeper_silent", "boards_paused"):
            self.queue(LINES[name])
            with self.subTest(line=name):
                self.assertEqual(self.flush(), 1)
                self.assertEqual(self.sent[-1], LINES[name])

    def test_a_done_line_whose_title_says_secret_waits(self):
        self.queue(LINES["done_secret"])
        self.assertEqual(self.flush(), 0)
        self.assertEqual(self.flush(dict(ON, **QUIET), NIGHT), 0)
        self.assertEqual(self.status(), ["pending"])

    def test_a_word_counts_whole_and_only_on_a_line_that_asks_for_owner(self):
        self.queue(slack.render("blocked", "t_0123abcd the secretary asked a question"))
        self.queue(slack.render("running", "t_0123abcd irreversible step dispatched"))
        self.queue(slack.render("scheduled", "budget review at 17:00"))
        self.assertEqual(self.flush(), 0)

    def test_every_event_in_the_table_has_a_test_message(self):
        self.assertEqual(sorted(outbox._events()), sorted(EVENTS))

    def test_a_mixed_queue_sends_only_the_event_and_the_rest_stay_pending(self):
        self.queue(LINES["done_secret"])
        self.queue(LINES["keeper_silent"])
        self.assertEqual(self.flush(), 1)
        self.assertEqual((self.sent, self.status()),
                         ([LINES["keeper_silent"]], ["pending", "delivered"]))

    def test_events_break_quiet_hours_and_the_rest_wait_even_by_day(self):
        cfg = dict(ON, **QUIET)
        self.queue(slack.render("blocked", "t_0123abcd needs you"))   # a D59 interrupt, not D13
        self.queue(LINES["rate_limit"])
        self.assertEqual(self.flush(cfg, NIGHT), 1)
        self.assertEqual(self.sent, [LINES["rate_limit"]])
        self.assertEqual(self.flush(cfg, NOON), 0)
        self.assertEqual(self.status(), ["pending", "delivered"])

    def test_the_morning_summary_is_the_batch_and_file_rows_always_go(self):
        outbox.add(self.conn, "slack", slack.SUMMARY_GLYPH + " Morning summary", now=NOON)
        outbox.add(self.conn, "file", "capsule: done", now=NOON)
        self.assertEqual(self.flush(), 2)

    def test_an_edit_or_reminder_of_a_posted_message_is_not_a_new_interrupt(self):
        key = slack_msgs.needs_key("t_0123abcd")
        self.conn.execute(
            "INSERT INTO slack_msg (key, kind, channel, ts, state, text, posted_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?)", (key, "needs", "C1", "1.0", "open", "x", NOON, NOON))
        edit = slack.render("blocked", "t_0123abcd needs you again")
        self.queue(edit, dedup_key=key)
        self.queue(slack_digest.REMINDER, dedup_key=slack_msgs.REMIND + key)
        self.queue(slack.render("blocked", "t_0456 needs you"),
                   dedup_key=slack_msgs.needs_key("t_0456"))
        self.queue(f"{slack_msgs.HANDLED} nothing open for this one",    # outbox.finish's shape
                   dedup_key=slack_msgs.needs_key("t_0999"))
        self.assertEqual(self.flush(), 2)
        self.assertEqual(self.sent, [edit, slack_digest.REMINDER])
        self.assertEqual(self.status(), ["delivered", "delivered", "pending", "delivered"])

    def test_switch_off_or_absent_changes_nothing(self):
        for cfg in (None, {}, {"redesign": {"interrupts": False}}):
            with self.subTest(cfg=cfg):
                self.conn = board()
                outbox.set_slack(self.conn, True, now=NOON)
                self.sent = []
                self.queue(LINES["done_secret"])
                self.assertEqual(self.flush(cfg), 1)


class Table(unittest.TestCase):
    def load(self, text):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "interrupts.json")
            if text is not None:
                path.write_text(text)
            with mock.patch.object(outbox, "INTERRUPTS", path), \
                    mock.patch.object(outbox, "_table_warned", False), \
                    redirect_stderr(io.StringIO()) as err:
                got = outbox._events()
                outbox._events()
                return got, err.getvalue()

    def test_a_missing_or_malformed_table_fails_open_with_one_warning(self):
        ev = {"classes": [], "words": []}
        bad = (None, "{not json", json.dumps({"events": {}}), json.dumps([1]),
               json.dumps({"events": {"a": {"classes": "incident", "words": []}}}),
               json.dumps({"events": {"a": {"classes": [], "words": [3]}}}),
               json.dumps({"events": {"a": {"classes": []}}}),
               json.dumps({"events": {"a": ev, "b": 5}}))
        for text in bad:
            with self.subTest(text=text):
                got, err = self.load(text)
                self.assertIsNone(got)
                self.assertEqual(err.count("interrupts.json unreadable"), 1)
        conn = board()
        outbox.add(conn, "slack", LINES["done_secret"], now=NOON)
        with mock.patch.object(outbox, "_events", return_value=None):
            self.assertEqual(len(outbox._releasable(
                conn, conn.execute("SELECT * FROM outbox").fetchall(), ON, NOON)), 1)

    def test_words_are_lowercased_on_load(self):
        table = {"events": {"a": {"classes": ["paused"], "words": ["Keeper Silent"]}}}
        got, err = self.load(json.dumps(table))
        self.assertEqual((got["a"]["words"], err), (["keeper silent"], ""))


class IntentExamples(unittest.TestCase):
    """contracts/intent/interrupts.jsonl, linted and replayed through the real outbox flush (Z1)."""

    def test_every_example_holds_on_the_real_code(self):
        path = intent.role_path("interrupts")
        if not path.is_file():
            self.skipTest("contracts/intent/interrupts.jsonl does not exist yet")
        self.assertEqual(intent.lint(path, None, "interrupts"), [])
        examples = intent.load("interrupts")
        self.assertEqual(len(examples), 3)                # an absent file would pass vacuously
        for ex in examples:
            with self.subTest(ex["id"]):
                doc, exp = ex["given"]["document"], ex["expect"]
                self.assertEqual(doc["check"], "interrupts")
                self.assertLessEqual(set(doc), {"check", "redesign", "row", "now_local"})
                hh, mm = map(int, doc["now_local"].split(":"))
                now = int(datetime.datetime(
                    2026, 10, 27, hh, mm, tzinfo=ZoneInfo(ZONE)).timestamp())
                conn, sent = board(), []
                outbox.set_slack(conn, True, now=now - 120)
                outbox.add(conn, doc["row"]["channel"], doc["row"]["body"], now=now - 60)
                outbox.flush(conn, sent.append, now=now, quiet={"redesign": doc["redesign"]})
                self.assertIs(bool(sent), exp["sent"])


if __name__ == "__main__":
    unittest.main()
