"""Visibility spec card 9: the 09:00 summary, the 4-hour reminder, the pause alarm. Every HTTP
call is a fake (capsule.tests.test_slack.FakeHTTP); nothing here reaches Slack."""
import unittest
from datetime import datetime

import tempfile
from pathlib import Path

from capsule import db, outbox, slack, slack_digest
from capsule.tests.test_slack import SLACK, FakeHTTP, Tokens


def guard(conn, cfg, task_id, now, counts, work):
    try:
        work()
    except Exception:
        counts["tick_errors"] += 1


def at(day, hour, minute=0):
    return datetime(2026, 10, day, hour, minute).timestamp()


class Digest(unittest.TestCase):
    def board(self, name):
        db.create_board(self.home / "boards" / name / "kanban.db")
        return db.connect(self.home / "boards" / name / "kanban.db")

    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.conn = self.board("sample-app")
        outbox.set_slack(self.conn, True, now=at(21, 8))
        self.http = FakeHTTP()
        self.sender = slack.Sender(SLACK, token=Tokens("xoxb-fake"), urlopen=self.http)

    def send(self, row):
        return self.sender(row) if row["channel"] == "slack" else None

    def run_at(self, now, conn=None, name="sample-app"):
        conn = conn or self.conn
        self.counts = {"tick_errors": 0}
        outbox.digest(conn, {"board": name}, now, self.home, guard, self.counts)
        outbox.flush(conn, self.send, now=now)

    def bodies(self):
        return [c["body"] for c in self.http.calls]

    def script(self, n=6):
        self.http.script += [{"ok": True, "ts": f"{100 + i}.1"} for i in range(n)]

    def ask(self, now, task="t_a"):
        self.script(1)
        outbox.notify(self.conn, "needs_input", f"{task} needs you", key=f"needs:{task}", now=now)
        outbox.flush(self.conn, self.send, now=now)
        self.http.calls.clear()

    def finish_task(self, now):
        tid = db.new_task(self.conn, title="Fix the login page", now=now)
        self.conn.execute("UPDATE tasks SET status='done', completed_at=? WHERE id=?", (now, tid))

    # -- summary -------------------------------------------------------------------------

    def test_summary_waits_for_nine_posts_once_and_not_twice(self):
        self.script()
        self.run_at(at(22, 8, 59))
        self.assertEqual(self.http.calls, [])
        self.run_at(at(22, 9, 1))
        self.run_at(at(22, 14))
        self.assertEqual(len(self.http.calls), 1)
        self.assertTrue(self.bodies()[0]["text"].startswith("☀️"))
        self.assertEqual(self.http.calls[0]["body"]["channel"], SLACK["channels"]["blocked"])

    def test_summary_posts_again_the_next_day(self):
        self.script()
        self.run_at(at(22, 9, 1))
        self.run_at(at(23, 9, 1))
        self.assertEqual(len(self.http.calls), 2)

    def test_quiet_day_text_counts_yesterdays_finished_only(self):
        self.script()
        self.finish_task(at(21, 15))
        self.finish_task(at(21, 23))
        self.finish_task(at(20, 12))
        self.finish_task(at(22, 8))
        self.run_at(at(22, 9, 1))
        quiet = "☀️ Quiet day: 2 finished, nothing needs you."
        self.assertEqual(self.bodies()[0]["text"], quiet)

    def test_busy_day_names_what_needs_you_and_broken(self):
        self.script()
        tid = db.new_task(self.conn, title="Fix the login page", now=at(21, 10))
        self.ask(at(21, 10), tid)
        self.http.script.append({"ok": True, "ts": "7.1"})
        outbox.notify(self.conn, "incident", "The keeper went silent", key="broken:abc",
                      now=at(21, 11))
        outbox.flush(self.conn, self.send, now=at(21, 11))
        self.http.calls.clear()
        self.script()
        self.run_at(at(22, 9, 1))
        text = next(b["text"] for b in self.bodies() if b["text"].startswith("☀️"))
        self.assertLessEqual(len(text.splitlines()), 8)
        self.assertIn("1 need you", text)
        self.assertIn("1 broken", text)
        self.assertIn("Fix the login page", text)
        self.assertIn("The keeper went silent", text)
        self.assertNotIn(tid, text)

    # -- reminder ------------------------------------------------------------------------

    def reminders(self):
        return [c["body"] for c in self.http.calls if "thread_ts" in c["body"]]

    def test_reminder_after_four_hours_in_the_day_window_once(self):
        self.ask(at(21, 10))
        self.script()
        self.run_at(at(21, 13, 59))
        self.assertEqual(self.reminders(), [])
        self.run_at(at(21, 14, 1))
        self.run_at(at(21, 16))
        got = self.reminders()
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["text"], "Still waiting on you for this one.")
        self.assertEqual(got[0]["thread_ts"], "100.1")
        self.assertEqual(got[0]["channel"], SLACK["channels"]["blocked"])

    def test_reminder_not_at_night_waits_for_nine(self):
        self.ask(at(21, 17))
        self.script()
        self.run_at(at(21, 22))
        self.run_at(at(22, 8, 59))
        self.assertEqual(self.reminders(), [])
        self.run_at(at(22, 9, 1))
        self.assertEqual(len(self.reminders()), 1)

    def test_no_reminder_once_the_ask_was_handled(self):
        self.ask(at(21, 10))
        self.script()
        outbox.finish(self.conn, "needs:t_a", "✔ Fix: handled.", at(21, 11))
        outbox.flush(self.conn, self.send, now=at(21, 11))
        self.http.calls.clear()
        self.run_at(at(21, 15))
        self.assertEqual(self.reminders(), [])

    # -- pause ---------------------------------------------------------------------------

    def pause(self, now, until):
        db.guard_set(self.conn, "pause:board", {"until": until, "reason": "x"}, now=now)

    def test_pause_over_an_hour_raises_one_red_and_clears_to_a_tick(self):
        self.script()
        self.run_at(at(21, 9, 30))                       # the summary of the day goes first
        self.http.calls.clear()
        self.pause(at(21, 10), at(21, 20))
        self.run_at(at(21, 10, 30))
        self.assertEqual(self.http.calls, [])
        self.run_at(at(21, 11, 5))
        self.run_at(at(21, 11, 10))
        reds = [b for b in self.bodies() if b["text"].startswith("\U0001F534")]
        self.assertEqual(len(reds), 1)
        self.assertIn("paused", reds[0]["text"])
        self.conn.execute("DELETE FROM guard_state WHERE key='pause:board'")
        self.conn.execute(      # a run has started since: that is what "running again" needs
            "INSERT INTO task_runs (task_id, profile, status, started_at, run_id)"
            " VALUES ('t_a', 'implementer', 'running', ?, ?)", (at(21, 11, 30), "r".ljust(32, "0")))
        self.run_at(at(21, 12))
        self.assertEqual(len(self.http.calls), 2)
        self.assertTrue(self.http.calls[1]["url"].endswith("chat.update"))
        self.assertTrue(self.bodies()[1]["text"].startswith("✔"))
        self.run_at(at(21, 12, 5))
        self.assertEqual(len(self.http.calls), 2)

    def test_two_boards_one_summary_that_counts_both(self):
        other = self.board("capsule")
        outbox.set_slack(other, True, now=at(21, 8))
        self.script()
        self.finish_task(at(21, 15))
        tid = db.new_task(other, title="Ship the thing", now=at(21, 9))
        other.execute("UPDATE tasks SET status='done', completed_at=? WHERE id=?",
                      (at(21, 16), tid))
        self.run_at(at(22, 9, 1))
        self.run_at(at(22, 9, 2), other, "capsule")
        self.assertEqual(len(self.bodies()), 1)
        self.assertIn("2 finished", self.bodies()[0]["text"])

    def test_two_boards_one_pause_one_red_saying_all_boards(self):
        other = self.board("capsule")
        outbox.set_slack(other, True, now=at(21, 8))
        for conn in (self.conn, other):
            held = {"until": at(21, 20), "since": at(21, 10)}
            db.guard_set(conn, "pause:board", held, now=at(21, 10))
        self.script()
        self.run_at(at(21, 9, 30))
        self.http.calls.clear()
        self.run_at(at(21, 11, 5))
        self.run_at(at(21, 11, 6), other, "capsule")
        self.assertEqual(len(self.http.calls), 1)
        self.assertTrue(self.bodies()[0]["text"].startswith("\U0001F534 All boards are paused"))

    def test_a_board_without_slack_msg_is_skipped_and_the_summary_posts(self):
        other = self.board("legacy")
        other.execute("DROP TABLE slack_msg")
        self.script()
        self.run_at(at(22, 9, 1))
        self.assertEqual(len(self.bodies()), 1)
        self.assertTrue(self.bodies()[0]["text"].startswith("☀️ Quiet day"))

    def test_a_step_that_raises_does_not_propagate(self):
        self.conn.execute("DROP TABLE slack_msg")
        self.run_at(at(21, 15))
        self.assertEqual(self.counts["tick_errors"], 1)

    def test_a_fallback_repost_of_an_open_ask_is_not_reminded_twice(self):
        self.ask(at(21, 10))
        self.script()
        self.run_at(at(21, 14, 1))
        self.assertEqual(len(self.reminders()), 1)
        self.conn.execute("UPDATE slack_msg SET ts='1.1'")
        self.http.script.append({"ok": False, "error": "message_not_found"})
        outbox.notify(self.conn, "needs_input", "t_a again", key="needs:t_a", now=at(21, 15))
        self.script(1)
        outbox.flush(self.conn, self.send, now=at(21, 15))
        self.http.calls.clear()
        self.run_at(at(21, 20))
        self.assertEqual(self.reminders(), [])

    def started(self, when, outcome=None, ended=None, profile="implementer"):
        self.conn.execute(
            "INSERT INTO task_runs (task_id, profile, status, outcome, started_at, ended_at,"
            " run_id) VALUES ('t_a', ?, 'running', ?, ?, ?, ?)",
            (profile, outcome, when, ended, f"{profile}{int(when)}".ljust(32, "0")))

    def lapsed_pause(self):
        self.script()
        self.run_at(at(21, 9, 30))
        self.pause(at(21, 10), at(21, 12))
        self.run_at(at(21, 11, 5))                       # over an hour: the red is up
        self.http.calls.clear()

    def test_running_again_waits_for_a_run_that_started_after_the_pause_lapsed(self):
        self.lapsed_pause()
        for minute in (1, 2, 3):                         # digest passes after the pause lapsed
            self.run_at(at(21, 12, minute))
        self.assertEqual(self.http.calls, [])
        self.started(at(21, 12, 2), "rate_limited", at(21, 12, 3))     # a retry the pause ended
        self.run_at(at(21, 12, 4))
        self.assertEqual(self.http.calls, [])
        self.started(at(21, 12, 5))                      # a run that is really going
        self.run_at(at(21, 12, 6))
        self.assertEqual([c["url"].rsplit("/", 1)[1] for c in self.http.calls], ["chat.update"])
        self.assertTrue(self.bodies()[0]["text"].startswith("\u2714"))

    def test_running_again_waits_for_the_wall_outage_to_end_too(self):
        self.lapsed_pause()
        db.guard_set(self.conn, "wall:outage:implementer",
                     {"since": at(21, 10), "last": at(21, 12, 3)}, now=at(21, 12, 3))
        self.started(at(21, 12, 5))                      # started, but the wall has not worked yet
        self.run_at(at(21, 12, 6))
        self.assertEqual(self.http.calls, [])
        self.started(at(21, 12, 7), "completed", at(21, 12, 8))     # a session ended well
        self.run_at(at(21, 12, 9))
        self.assertEqual(len(self.http.calls), 1)

    def test_summary_mentions_a_pause_over_an_hour(self):
        self.script()
        self.pause(at(22, 6), at(22, 20))
        self.run_at(at(22, 9, 1))
        self.assertIn("paused for 3 hour(s)", self.bodies()[0]["text"])


if __name__ == "__main__":
    unittest.main()
