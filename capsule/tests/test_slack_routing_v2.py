"""Notification rebuild, slice 1 (spec 2026-10-29, row 637): class routing behind the per-board
`slack.routing: "v2"` setting. Unset, every posting decision is today's (a literal table, not a
re-derivation); set, only `ask` and `page` post, a page breaks quiet hours and an ask waits for
the morning. Nothing here reaches Slack: senders are list appends and `now` is injected."""
import argparse
import contextlib
import datetime
import io
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from capsule import cli_ops, db, incidents, keeper_watch, outbox, paths, seats, slack
from capsule.tests import test_korin, test_tick
from capsule.tests.test_quiet_hours import NIGHT, at
from capsule.tests.test_slack import CHANNELS, SLACK, board, rows

V2 = dict(SLACK, routing="v2")
NOVEL = {"novel": "\U0001F9EA"}      # a class the allowlist has never heard of
# main's decision per class, written out by hand: does `notify` queue a slack row?
TODAY = {"blocked": True, "needs_input": True, "incident": True, "done": False,
         "running": False, "paused": False, "scheduled": False, "novel": True}


def channels(conn):
    return [r["channel"] for r in rows(conn) if r["channel"] != "local"]


def slack_classes(conn):
    return [slack.classify(r["body"]) for r in rows(conn) if r["channel"] == "slack"]


def v2_board():
    conn = board()
    outbox.set_slack(conn, V2, now=at(12))
    return conn


class RoutingUnset(unittest.TestCase):
    def test_every_class_posts_exactly_as_it_does_today(self):
        for cls, posts in TODAY.items():
            with self.subTest(cls=cls), mock.patch.dict(slack.CLASSES, NOVEL):
                conn = board()
                outbox.set_slack(conn, SLACK, now=at(12))
                outbox.notify(conn, cls, f"t_1 {cls}", now=at(12))
                self.assertEqual(channels(conn), ["file", "slack"] if posts else ["file"])

    def test_page_changes_nothing_while_unset(self):
        conn = board()
        outbox.set_slack(conn, SLACK, now=at(12))
        outbox.notify(conn, "incident", "t_1 tick error", page=True, now=at(12))
        outbox.notify(conn, "incident", "t_2 leaked a secret credential", now=at(12))
        self.assertEqual(slack_classes(conn), ["incident", "incident"])
        self.assertFalse(outbox.routing_v2(conn))

    def test_the_morning_summary_keeps_the_needs_you_channel(self):
        body = slack.render("scheduled", "3 merged, 1 waits on you", digest=True)
        self.assertEqual(slack.route(SLACK, body), CHANNELS["blocked"])
        project = dict(V2, channels=dict(CHANNELS, done="#cc-x"))
        self.assertEqual(slack.route(project, body), "#cc-x")

    def test_the_flag_follows_the_config_and_flips_back(self):
        conn = board()
        outbox.set_slack(conn, V2, now=at(12))
        self.assertTrue(outbox.routing_v2(conn))
        outbox.set_slack(conn, SLACK, now=at(13))
        self.assertFalse(outbox.routing_v2(conn))
        outbox.set_slack(conn, None, now=at(14))
        self.assertEqual((outbox.slack_enabled(conn), outbox.routing_v2(conn)), (False, False))

    def test_only_v2_is_a_valid_routing_value(self):
        self.assertIs(slack.validate(V2), V2)
        with self.assertRaisesRegex(ValueError, "slack.routing"):
            slack.validate(dict(SLACK, routing="v3"))


class RoutingV2(unittest.TestCase):
    def test_blocked_needs_input_and_ordinary_incident_do_not_post(self):
        conn = v2_board()
        for cls in ("blocked", "needs_input", "incident"):
            outbox.notify(conn, cls, f"t_1 tick error {cls}", now=at(12))
        self.assertEqual(channels(conn), ["file"] * 3)       # the log keeps all three

    def test_ask_and_page_post_with_their_own_glyph(self):
        conn = v2_board()
        outbox.notify(conn, "ask", "Retry limit: count per card?", now=at(12))
        outbox.notify(conn, "incident", "Factory stopped", page=True, now=at(12))
        self.assertEqual(slack_classes(conn), ["ask", "page"])
        self.assertEqual(channels(conn), ["file", "slack", "file", "slack"])

    def test_an_unknown_class_posts_nothing(self):
        conn = v2_board()
        with mock.patch.dict(slack.CLASSES, NOVEL):
            outbox.notify(conn, "novel", "t_1 odd", now=at(12))
        self.assertEqual(channels(conn), ["file"])

    def test_a_page_posts_at_2300_and_an_ask_waits_until_0730(self):
        conn, sent = v2_board(), []
        outbox.notify(conn, "ask", "Retry limit: count per card?", now=at(22, 55))
        outbox.notify(conn, "blocked", "Factory down", page=True, now=at(22, 55))
        for hh, mm, day in ((23, 0, 27), (7, 29, 28), (7, 30, 28)):
            outbox.flush(conn, sent.append, now=at(hh, mm, day), quiet=NIGHT)
            sent_classes = [slack.classify(r["body"]) for r in sent if r["channel"] == "slack"]
            self.assertEqual(sent_classes, ["page"] if (hh, mm) != (7, 30) else ["page", "ask"])

    def test_an_unreadable_event_table_still_lets_a_page_through_at_night(self):
        conn, sent = v2_board(), []
        for cls in ("done", "page"):
            outbox.add(conn, "slack", slack.render(cls, f"{cls} line"), now=at(22, 55))
        with mock.patch.object(outbox, "_events", return_value=None):
            outbox.flush(conn, sent.append, now=at(23), quiet=NIGHT)
        self.assertEqual([slack.classify(r["body"]) for r in sent], ["page"])
        self.assertEqual(outbox.QUIET_FALLBACK, ("incident", "blocked", "page"))

    def test_a_security_word_in_an_incident_headline_does_not_page(self):
        conn = v2_board()
        outbox.notify(conn, "incident", "Job failed: security", now=at(12))
        outbox.notify(conn, "incident", "t_2 worker printed a Credential", now=at(12))
        self.assertEqual(channels(conn), ["file", "file"])

    def test_a_summary_posts_under_v2_and_routes_to_the_project_channel(self):
        conn = v2_board()
        outbox.notify(conn, "scheduled", "Morning: 3 merged", digest=True, now=at(12))
        self.assertEqual(channels(conn), ["file", "slack"])
        self.assertEqual(slack.route(V2, rows(conn)[-1]["body"]), CHANNELS["done"])
        outbox.notify(conn, "scheduled", "delayed replay", now=at(12))     # no marker: log only
        self.assertEqual(channels(conn), ["file", "slack", "file"])
        self.assertNotIn("scheduled", outbox.POST_CLASSES)

    def test_the_summary_goes_to_the_project_channel_and_a_page_to_needs_you(self):
        summary = slack.render("scheduled", "3 merged", digest=True)
        page = slack.render("page", "Factory stopped")
        self.assertEqual(slack.route(V2, summary), CHANNELS["done"])
        self.assertEqual(slack.route(V2, page), CHANNELS["blocked"])           # no needs_input
        both = dict(V2, channels=dict(CHANNELS, needs_input="#in"))
        self.assertEqual(slack.route(both, page), "#in")
        own = dict(both, channels=dict(both["channels"], inbox="#p"))
        self.assertEqual(slack.route(own, page), "#p")
        self.assertEqual(slack.route(own, slack.render("ask", "Retry limit?")), "#p")


class OutboxSend(unittest.TestCase):
    def run_send(self, cls, v2=True, v2_class=None, text="Docker is not running\nx", digest=False):
        home = Path(tempfile.mkdtemp())
        path = paths.board_db("b1", home)
        db.create_board(path)
        conn = db.connect(path)
        outbox.set_slack(conn, V2 if v2 else SLACK, now=at(12))
        args = argparse.Namespace(cmd="outbox-send", cls=cls, text=text, v2_class=v2_class,
                                  digest=digest, board="b1", capsule_home=str(home))
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            rc = cli_ops.handle(args, None)
        return rc, err.getvalue(), rows(conn)

    def test_outbox_send_refuses_ask(self):
        for v2 in (True, False):
            rc, err, got = self.run_send("ask", v2)
            self.assertEqual((rc, got), (1, []))
            self.assertIn("written only by `cos ask`", err)

    def test_a_page_pages_under_v2_and_is_an_incident_without_it(self):
        def posted(got):
            return [slack.classify(r["body"]) for r in got if r["channel"] == "slack"]
        self.assertEqual(posted(self.run_send("page")[2]), ["page"])
        self.assertEqual(posted(self.run_send("page", v2=False)[2]), ["incident"])

    def test_the_other_classes_are_untouched_by_the_page_mapping(self):
        _, _, got = self.run_send("incident")
        self.assertEqual(channels_of(got), ["file"])               # v2: an ordinary incident logs
        _, _, got = self.run_send("incident", v2=False)
        self.assertEqual(channels_of(got), ["file", "slack"])

    def test_outbox_send_summary_posts_and_blocked_is_log_only_under_v2(self):
        _, _, got = self.run_send("scheduled", text="Morning brief\nline 2", digest=True)
        self.assertEqual(channels_of(got), ["file", "slack"])
        self.assertTrue(got[1]["body"].startswith(slack.SUMMARY_GLYPH))
        _, _, got = self.run_send("blocked", text="Blocked tonight: 1 item")
        self.assertEqual(channels_of(got), ["file"])

    def test_the_blocked_tonight_line_is_a_summary_under_v2_and_unchanged_without_it(self):
        line = "Blocked tonight: 1 item waits on you"
        _, _, got = self.run_send("blocked", v2_class="scheduled", digest=True, text=line)
        self.assertEqual([r["body"] for r in got if r["channel"] == "slack"],
                         [f"{slack.SUMMARY_GLYPH} {line}"])
        _, _, got = self.run_send("blocked", v2=False, v2_class="scheduled", digest=True, text=line)
        self.assertEqual([r["body"] for r in got if r["channel"] == "slack"],
                         [slack.render("blocked", line)])
        report = (paths.REPO / "maintenance" / "report.sh").read_text()
        self.assertIn("MSG_CLASS=blocked MSG_V2_CLASS=scheduled tg_send", report)

    def test_report_sh_digest_and_close_are_scheduled_and_job_failed_does_not_post(self):
        report = (paths.REPO / "maintenance" / "report.sh").read_text()
        self.assertEqual(report.count("MSG_CLASS=scheduled tg_send"), 2)    # D209: digest + close
        self.assertNotIn("summary tg_send", report)
        self.assertNotIn("=summary", report)
        fail = [ln for ln in report.splitlines() if "Job failed" in ln and "tg_send" in ln]
        self.assertEqual(len(fail), 1)
        self.assertIn("MSG_CLASS=incident tg_send", fail[0])
        self.assertNotIn("MSG_V2_CLASS", fail[0])
        _, _, got = self.run_send("incident", text="Job failed: x")
        self.assertEqual(channels_of(got), ["file"])
        for v2 in (True, False):         # heal's spool replay: scheduled without --digest, log only
            _, _, got = self.run_send("scheduled", v2=v2, text="spool replay body")
            self.assertEqual(channels_of(got), ["file"])
        self.assertIn("--digest", (paths.REPO / "maintenance" / "lib_send.sh").read_text())
        for v2 in (True, False):
            rc, _, got = self.run_send("scheduled", v2=v2, text="BOARD STANDUP\nline", digest=True)
            self.assertEqual((rc, channels_of(got)), (0, ["file", "slack"]))
            self.assertTrue(got[1]["body"].startswith(slack.SUMMARY_GLYPH))

    def test_heal_sh_sends_its_docker_down_line_as_a_page_and_the_rest_as_before(self):
        text = (paths.REPO / "maintenance" / "heal.sh").read_text()
        down = [ln for ln in text.splitlines() if "Docker is not running" in ln]
        self.assertEqual(len(down), 1)
        self.assertIn("| HEAL_CLASS=page $HEAL_SEND", down[0])
        self.assertIn('outbox-send "${HEAL_CLASS:-incident}"', text)
        pages = [ln for ln in text.splitlines() if "HEAL_CLASS=page" in ln]
        self.assertEqual(len(pages), 2)      # docker down, and the patrol still silent (unit 7)
        self.assertEqual(sum("The patrol is silent" in ln for ln in pages), 1)

    def test_the_logged_out_line_pages_under_v2_and_stays_paused_without_it(self):
        def posted(got):
            return [slack.classify(r["body"]) for r in got if r["channel"] == "slack"]
        line = "Claude Code is logged out - run /login in a terminal."
        rc, _, got = self.run_send("paused", v2_class="page", text=line)
        self.assertEqual((rc, posted(got)), (0, ["page"]))
        rc, _, got = self.run_send("paused", v2=False, v2_class="page", text=line)
        self.assertEqual((rc, posted(got), channels_of(got)), (0, [], ["file"]))  # as today
        text =(paths.REPO / "maintenance" / "heal.sh").read_text()
        out = [ln for ln in text.splitlines() if "Claude Code is logged out" in ln]
        self.assertEqual(len(out), 1)
        self.assertIn("| HEAL_CLASS=paused HEAL_V2_CLASS=page $HEAL_SEND", out[0])
        self.assertIn('${HEAL_V2_CLASS:+--v2-class "$HEAL_V2_CLASS"}', text)

    def test_the_text_must_render_under_the_class_actually_sent(self):
        # blank text renders under `blocked` (headline falls back to "heal notice") but not as a
        # scheduled digest (empty body): with --v2-class scheduled under v2 it is refused,
        # otherwise queued
        blank = dict(digest=True, v2_class="scheduled", text="  \n ")
        rc, err, got = self.run_send("blocked", **blank)
        self.assertEqual((rc, got), (1, []))
        self.assertIn("scheduled: empty body", err)
        rc, _, got = self.run_send("blocked", v2=False, **blank)
        self.assertEqual((rc, channels_of(got)), (0, ["file", "slack"]))


def channels_of(got):
    return [r["channel"] for r in got]


def local(hour, minute=0):
    """Epoch of 21 Oct 2026 at `hour:minute` on this machine's clock, as slack_digest reads it."""
    return datetime.datetime(2026, 10, 21, hour, minute).timestamp()


def run_work(conn, cfg, task_id, now, counts, work):
    work()


class PageSites(unittest.TestCase):
    """The incident sites outside the tick, each on a throwaway board with routing v2 on."""

    def test_a_board_that_cannot_be_read_pages(self):
        home = Path(tempfile.mkdtemp())
        conn = db.connect(self.mkboard(home, "own"))
        outbox.set_slack(conn, V2, now=at(12))
        broken = paths.board_db("broken", home)
        broken.parent.mkdir(parents=True)
        broken.write_bytes(b"this is not a sqlite database at all" * 40)
        seats.snapshot(conn, {"board": "own"}, home, 2000)
        self.assertEqual(slack_classes(conn), ["page"])
        self.assertIn("board DB unreadable", rows(conn)[-1]["body"])

    @staticmethod
    def mkboard(home, name):
        path = paths.board_db(name, home)
        db.create_board(path)
        return path

    def test_a_repeat_incident_pages(self):
        conn = v2_board()
        first, _ = incidents.open_incident(conn, board="b", kind="crash", key="x: fatal", now=1000)
        conn.execute("UPDATE incidents SET status='closed', closed_at=? WHERE id=?", (1500, first))
        _, action = incidents.open_incident(conn, board="b", kind="crash", key="x: fatal", now=2000)
        self.assertEqual(action, "repeat")
        self.assertEqual(slack_classes(conn), ["page"])
        self.assertIn("REPEAT incident", rows(conn)[-1]["body"])

    def test_keeper_silent_pages(self):
        home = Path(tempfile.mkdtemp())
        conn = db.connect(self.mkboard(home, "capsule-corp"))
        outbox.set_slack(conn, V2, now=at(12))
        keeper_watch._outbox(home, "capsule-corp", "keeper silent 75 min with 3 waiting", "ok")
        self.assertEqual(slack_classes(conn), ["page"])
        posted = [r for r in rows(conn) if r["channel"] != "local"]
        self.assertEqual(slack.route(V2, posted[-1]["body"]), CHANNELS["blocked"])
        (local,) = conn.execute("SELECT COUNT(*) FROM outbox WHERE channel='local'").fetchone()
        self.assertEqual(local, 1)                          # one Mac page

    def test_a_pause_over_an_hour_pages(self):
        home = Path(tempfile.mkdtemp())
        conn = db.connect(self.mkboard(home, "my-app"))
        outbox.set_slack(conn, V2, now=local(8))
        db.guard_set(conn, "pause:board", {"until": local(20), "reason": "x"}, now=local(10))
        outbox.digest(conn, {"board": "my-app"}, local(11, 5), home, run_work, {})
        sent = [r["body"] for r in rows(conn) if r["channel"] == "slack"]
        pages = [body for body in sent if slack.classify(body) == "page"]
        self.assertEqual(len(pages), 1)
        self.assertIn("paused", pages[0])


class TickSites(test_tick.TickCase):
    def setUp(self):
        super().setUp()
        self.cfg["slack"] = dict(self.cfg["slack"], enabled=True, routing="v2")
        self.write_cfg()
        self.sent = []
        self.tick = self.make_tick(slack_factory=lambda block: lambda row: self.sent.append(
            (slack.route(block, row["body"]), row["body"])))

    def test_a_tick_error_on_a_card_pages(self):
        self.tick.once()          # mirrors routing v2 into the board

        def boom():
            raise RuntimeError("disk full")
        counts = {"tick_errors": 0}
        self.tick._guarded(self.conn, self.cfg, self.tid, int(time.time()), counts, boom)
        posted = [r["body"] for r in rows(self.conn) if r["channel"] == "slack"]
        self.assertEqual([slack.classify(b) for b in posted], ["page"])
        self.assertIn(f"{self.tid} tick error: RuntimeError", posted[0])

    def test_a_config_error_pages(self):
        self.tick.once()
        (self.home / "config.json").write_text("{broken")
        self.tick.once()
        [(channel, body)] = self.sent
        self.assertEqual(channel, "#capsule-needs-you")
        self.assertTrue(body.startswith("\U0001F6D1 capsule config error: keeping the last good"))


class KorinSites(test_korin.KorinCase):
    def test_korin_failing_twice_pages_and_nothing_else_posts(self):
        self.cfg["slack"] = dict(self.cfg["slack"], enabled=True, routing="v2")
        self.enable()
        sent = []
        self.tick = self.make_tick(slack_factory=lambda block: lambda row: sent.append(row["body"]))
        self.korin_docs += [lambda inc: test_korin.postmortem(inc, enforcer="review"),
                            lambda inc: test_korin.FATAL]
        self.crash_once(passes=6)
        self.assertEqual([slack.classify(b) for b in sent], ["page"])
        self.assertIn("Korin failed twice on incident", sent[0])


if __name__ == "__main__":
    unittest.main()
