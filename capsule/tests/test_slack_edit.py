# mutation-check: targets-predate-range ff625e94 -- fixture rename only, no behaviour pinned ::
#   Edits.test_handled_transition_edits_to_a_tick_line
"""Visibility spec card 8: one Slack message per problem that edits itself. Every HTTP call is a
fake (capsule.tests.test_slack.FakeHTTP); nothing here reaches Slack."""
import unittest
from unittest import mock

from capsule import blocks, db, incidents, interventions, outbox, slack, slack_msgs, spool
from capsule.tests.test_slack import SLACK, T0, FakeHTTP, Tokens, board


class Edits(unittest.TestCase):
    def setUp(self):
        self.conn = board()
        outbox.set_slack(self.conn, True, now=T0)
        self.http = FakeHTTP()
        self.sender = slack.Sender(SLACK, token=Tokens("xoxb-fake"), urlopen=self.http)

    def send(self, row):
        if row["channel"] == "slack":
            return self.sender(row)

    def flush(self, now=T0):
        outbox.flush(self.conn, self.send, now=now)

    def hold(self, key, headline="t_a needs you"):
        outbox.notify(self.conn, "needs_input", headline, key=key, now=T0)

    def stored(self, key):
        return self.conn.execute("SELECT * FROM slack_msg WHERE key=?", (key,)).fetchone()

    def test_first_notify_posts_and_stores_channel_and_ts(self):
        self.http.script += [{"ok": True, "ts": "100.1"}]
        self.hold("needs:t_a")
        self.flush()
        self.assertEqual([c["url"] for c in self.http.calls], [slack.API_URL])
        row = self.stored("needs:t_a")
        self.assertEqual((row["kind"], row["ts"], row["state"]), ("needs", "100.1", "open"))
        self.assertEqual(row["channel"], SLACK["channels"]["blocked"])

    def test_same_key_again_edits_the_stored_message(self):
        self.http.script += [{"ok": True, "ts": "100.1"}, {"ok": True, "ts": "100.1"}]
        self.hold("needs:t_a")
        self.flush()
        self.hold("needs:t_a", "t_a needs you, still")
        self.flush(T0 + 60)
        call = self.http.calls[1]
        self.assertEqual(call["url"], slack.UPDATE_URL)
        self.assertEqual((call["body"]["channel"], call["body"]["ts"]),
                         (SLACK["channels"]["blocked"], "100.1"))
        self.assertIn("still", call["body"]["text"])

    def test_different_keys_never_edit_each_other(self):
        self.http.script += [{"ok": True, "ts": "1.1"}, {"ok": True, "ts": "2.2"}]
        self.hold("needs:t_a")
        self.hold("needs:t_b", "t_b needs you")
        self.flush()
        self.assertEqual([c["url"] for c in self.http.calls], [slack.API_URL, slack.API_URL])
        stored = [self.stored(k)["ts"] for k in ("needs:t_a", "needs:t_b")]
        self.assertEqual(stored, ["1.1", "2.2"])

    def test_unkeyed_rows_post_and_store_nothing(self):
        self.http.script += [{"ok": True, "ts": "9.9"}]
        outbox.notify(self.conn, "incident", "config error", now=T0)
        self.flush()
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM slack_msg").fetchone()[0], 0)

    def test_a_failed_update_falls_back_to_one_new_post_and_replaces_ts(self):
        gone = {"ok": False, "error": "message_not_found"}
        self.http.script += [{"ok": True, "ts": "100.1"}, gone, {"ok": True, "ts": "200.2"}]
        self.hold("needs:t_a")
        self.flush()
        self.hold("needs:t_a")
        self.flush(T0 + 60)
        urls = [c["url"] for c in self.http.calls]
        self.assertEqual(urls, [slack.API_URL, slack.UPDATE_URL, slack.API_URL])
        self.assertEqual(self.stored("needs:t_a")["ts"], "200.2")

    def test_handled_transition_edits_to_a_tick_line(self):
        tid = db.new_task(self.conn, title="Pick the break length", status="ready")
        blocks.block(self.conn, tid, kind="needs_input", reason="which length?", now=T0)
        self.http.script += [{"ok": True, "ts": "300.3"}, {"ok": True, "ts": "300.3"}]
        self.flush()
        doc = {"kind": "unblock", "text": "25"}
        interventions.apply(self.conn, {"board": "my-app"}, tid, doc, "a.json", "a.json", T0 + 5)
        self.flush(T0 + 60)
        call = self.http.calls[1]
        self.assertEqual((call["url"], call["body"]["ts"]), (slack.UPDATE_URL, "300.3"))
        self.assertTrue(call["body"]["text"].startswith("✔ Pick the break length"))
        self.assertTrue(call["body"]["text"].endswith("handled."))
        self.assertEqual(self.stored(f"needs:{tid}")["state"], "handled")

    def test_nothing_to_edit_when_no_message_was_ever_posted(self):
        self.assertIsNone(outbox.finish(self.conn, "broken:abc", "✔ Fixed: crash.", T0))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0], 0)

    def test_a_handled_message_is_not_edited_back_a_new_problem_posts_afresh(self):
        self.http.script += [{"ok": True, "ts": t} for t in ("1.1", "1.1", "2.2")]
        self.hold("needs:t_a")
        self.flush()
        outbox.add(self.conn, "slack", "✔ Done: handled.", dedup_key="needs:t_a", now=T0 + 1)
        self.flush(T0 + 30)
        self.hold("needs:t_a")
        self.flush(T0 + 90)
        self.assertEqual([c["url"] for c in self.http.calls][1:], [slack.UPDATE_URL, slack.API_URL])

    def test_a_worker_release_edits_it_and_a_later_ask_posts_a_new_message(self):
        tid = db.new_task(self.conn, title="Pick the break length", status="ready")
        self.http.script += [{"ok": True, "ts": t} for t in ("1.1", "1.1", "2.2")]
        blocks.block(self.conn, tid, kind="needs_input", reason="which length?", now=T0)
        self.flush()
        self.assertTrue(blocks.unblock(self.conn, tid, by="worker", now=T0 + 5))
        blocks.block(self.conn, tid, kind="needs_input", reason="and the long break?", now=T0 + 9)
        self.flush(T0 + 60)
        urls = [c["url"] for c in self.http.calls]
        self.assertEqual(urls, [slack.API_URL, slack.UPDATE_URL, slack.API_URL])
        self.assertEqual(self.stored(f"needs:{tid}")["ts"], "2.2")

    def test_a_handled_edit_with_a_dead_original_sends_nothing(self):
        self.http.script += [{"ok": False, "error": "channel_not_found"}]
        self.hold("needs:t_a")
        outbox.finish(self.conn, "needs:t_a", "✔ Done: handled.", T0 + 1)
        self.flush()
        self.assertEqual(len(self.http.calls), 1)
        status = [r["status"] for r in self.conn.execute(
            "SELECT status FROM outbox WHERE channel='slack' ORDER BY id")]
        self.assertEqual(status, ["dead", "delivered"])

    def test_a_decision_is_a_one_off_post_and_a_later_block_posts_new(self):
        tid = db.new_task(self.conn, title="Pick the break length", status="ready")
        self.http.script += [{"ok": True, "ts": "1.1"}, {"ok": True, "ts": "2.2"}]
        spool.decision_notify(self.conn, tid, {"question": "which?"}, T0)
        self.flush()
        blocks.block(self.conn, tid, kind="needs_input", reason="and then?", now=T0 + 9)
        self.flush(T0 + 60)
        urls = [c["url"] for c in self.http.calls]
        self.assertEqual(urls, [slack.API_URL, slack.API_URL])


class BrokenIncidents(unittest.TestCase):
    def test_a_repeat_incident_carries_the_broken_key_and_closing_edits_it_to_fixed(self):
        conn = board()
        outbox.set_slack(conn, True, now=T0)
        args = {"board": "b", "kind": "crash", "key": "boom"}
        first, _ = incidents.open_incident(conn, evidence=["e:1"], now=T0, **args)
        conn.execute("UPDATE incidents SET status='closed', closed_at=? WHERE id=?",
                     (T0 + 10, first))
        _, action = incidents.open_incident(conn, evidence=["e:2"], now=T0 + 20, **args)
        self.assertEqual(action, "repeat")
        fp = incidents.fingerprint("crash", "boom")
        key = conn.execute("SELECT dedup_key FROM outbox_key").fetchone()[0]
        self.assertEqual(key, slack_msgs.broken_key(fp))
        http = FakeHTTP({"ok": True, "ts": "7.7"}, {"ok": True, "ts": "7.7"})
        sender = slack.Sender(SLACK, token=Tokens("xoxb-fake"), urlopen=http)

        def send(row):
            return sender(row) if row["channel"] == "slack" else None
        outbox.flush(conn, send, now=T0 + 30)
        outbox.finish(conn, slack_msgs.broken_key(fp), "✔ Fixed: crash.", T0 + 40)
        outbox.flush(conn, send, now=T0 + 100)
        self.assertEqual(http.calls[1]["url"], slack.UPDATE_URL)
        self.assertEqual(http.calls[1]["body"]["text"], "✔ Fixed: crash.")


class CloseEdits(unittest.TestCase):
    def test_close_edits_only_when_the_last_incident_of_a_problem_closes(self):
        conn = board()
        outbox.set_slack(conn, True, now=T0)
        args = {"board": "b", "kind": "crash", "key": "boom", "evidence": ["e:1"]}
        one, _ = incidents.open_incident(conn, now=T0, **args)
        two, _ = incidents.open_incident(conn, now=T0 + 90000, **args)
        fp = incidents.fingerprint("crash", "boom")
        http = FakeHTTP({"ok": True, "ts": "7.7"}, {"ok": True, "ts": "7.7"})
        sender = slack.Sender(SLACK, token=Tokens("xoxb-fake"), urlopen=http)

        def send(row):
            return sender(row) if row["channel"] == "slack" else None
        outbox.notify(conn, "incident", "boom again", key=slack_msgs.broken_key(fp), now=T0)
        outbox.flush(conn, send, now=T0)
        conn.execute("UPDATE incidents SET status='fix_pending'")
        with mock.patch.object(incidents, "missing", return_value=[]):
            incidents.close(conn, one, vault=None, now=T0 + 100000)
            outbox.flush(conn, send, now=T0 + 100001)
            self.assertEqual(len(http.calls), 1)
            incidents.close(conn, two, vault=None, now=T0 + 100010)
            outbox.flush(conn, send, now=T0 + 100020)
        self.assertEqual(http.calls[1]["url"], slack.UPDATE_URL)
        self.assertEqual(http.calls[1]["body"]["text"], "✔ Fixed: crash.")
