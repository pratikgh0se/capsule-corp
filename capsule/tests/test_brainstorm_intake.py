"""M204 slice B1: brainstorm intake (D229). Oracle: fake Slack envelopes into the listener's
subprocess handler (`slack_answer._handle`), then the files on disk (meta, pending) and the outbox
rows. A temp CAPSULE_HOME and vault; no network, no real Slack, no claude, no model."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from capsule import brainstorm, brainstorm_intake, brainstorm_store as store, paths, slack_answer
from capsule.tests.test_brainstorm import BOARD, CHANNEL, NOW, Base

OWNER, TEAM, APP = "U0OWNER1", "T0TEAM1", "A0APP1"


class Intake(Base):
    def setUp(self):
        super().setUp()
        self.write_host({"slack_owner_id": OWNER, "slack_team_id": TEAM, "slack_app_id": APP})
        patcher = mock.patch.dict(os.environ, {"CAPSULE_BOARD": BOARD})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.n = 0

    def write_host(self, doc):
        paths.config_path(self.home).write_text(json.dumps(doc))

    def event(self, text="Pricing for beta", *, ts="1800000001.000100", thread_ts=None, user=OWNER,
              channel=CHANNEL, team=TEAM, app=APP, **extra):
        self.n += 1
        ev = {"type": "message", "user": user, "channel": channel, "text": text, "ts": ts, **extra}
        if thread_ts:
            ev["thread_ts"] = thread_ts
        return {"type": "event_callback", "event_id": f"Ev{self.n:04d}", "team_id": team,
                "api_app_id": app, "event": ev}

    def send(self, *args, **kw):
        return slack_answer._handle(self.home, self.event(*args, **kw))

    def snapshot(self):
        return (self.ids(), [r[:] for r in self.slack_rows()],
                {str(p.relative_to(self.home)) for p in self.home.rglob("*") if p.is_file()})

    def open_thread(self, text="Pricing for beta", ts="1800000001.000100"):
        self.assertEqual(self.send(text, ts=ts), {"ok": True})
        return self.ids()[-1]

    def pending(self, bid):
        return store.list_pending(self.home, BOARD, bid)


class Opens(Intake):
    def test_owner_top_level_post_opens_a_brainstorm_and_queues_its_text(self):
        self.assertEqual(self.send("Pricing for beta\nShould beta be free?"), {"ok": True})
        (bid,) = self.ids()
        meta = store.read_meta(self.home, BOARD, bid)
        self.assertEqual((meta["title"], meta["status"], meta["started_by"], meta["channel"],
                          meta["thread_ts"]),
                         ("Pricing for beta", "open", "owner", CHANNEL, "1800000001.000100"))
        (doc,) = self.pending(bid)
        self.assertEqual((doc["text"], doc["ts"]),
                         ("Pricing for beta\nShould beta be free?", 1800000001.0001))
        self.assertTrue((store.bdir(self.home, BOARD, bid) / "ws").is_dir())
        self.assertEqual(self.slack_rows(), [])         # the reply is the runner's job (B2)

    def test_title_is_the_first_line_cut_at_80_characters(self):
        self.send("x" * 120 + "\nsecond line")
        (bid,) = self.ids()
        self.assertEqual(store.read_meta(self.home, BOARD, bid)["title"], "x" * 80)

    def test_a_redelivered_event_opens_nothing_twice(self):
        payload = self.event()
        self.assertEqual(slack_answer._handle(self.home, payload), {"ok": True})
        self.assertEqual(slack_answer._handle(self.home, payload), {"ok": False})
        (bid,) = self.ids()
        self.assertEqual(len(self.pending(bid)), 1)

    def test_a_brainstorm_message_is_never_an_ask_answer(self):
        with mock.patch.object(slack_answer, "answer") as answer:
            self.send("yes")
            self.send("yes", ts="1800000002.000100", thread_ts="1800000001.000100")
        answer.assert_not_called()


class Replies(Intake):
    def test_a_reply_in_the_open_thread_is_queued_once(self):
        bid = self.open_thread()
        payload = self.event("Free for 3 months?", ts="1800000009.000200",
                             thread_ts="1800000001.000100")
        self.assertEqual(slack_answer._handle(self.home, payload), {"ok": True})
        self.assertEqual(slack_answer._handle(self.home, payload), {"ok": False})   # dedupe
        self.assertEqual([d["text"] for d in self.pending(bid)],
                         ["Pricing for beta", "Free for 3 months?"])
        self.assertEqual(self.ids(), [bid])

    def test_a_reply_in_an_unknown_thread_is_dropped(self):
        self.open_thread()
        before = self.snapshot()
        self.assertEqual(self.send("hello", ts="1800000009.5", thread_ts="1700000000.1"),
                         {"ok": False})
        self.assertEqual(self.snapshot(), before)

    def test_a_reply_in_a_capsule_started_thread_once_the_opener_is_delivered(self):
        bid = self.open()
        self.assertEqual(store.read_meta(self.home, BOARD, bid)["thread_ts"], "")
        self.conn.execute("INSERT INTO slack_msg (key, kind, channel, ts, state, text, posted_at,"
                          " updated_at) VALUES (?,?,?,?,?,?,?,?)",
                          (store.OPEN_KEY.format(bid), "brainstorm", CHANNEL, "1800000001.000100",
                           "open", "t", 1, 1))
        self.conn.commit()
        self.assertEqual(self.send("Yes, free", ts="1800000009.5", thread_ts="1800000001.000100"),
                         {"ok": True})
        self.assertEqual([d["text"] for d in self.pending(bid)], ["Yes, free"])

    def test_pending_cap_is_ten_per_thread(self):
        bid = self.open_thread()
        for i in range(12):
            self.send(f"m{i}", ts=f"18000001{i:02d}.000100", thread_ts="1800000001.000100")
        self.assertEqual(len(self.pending(bid)), brainstorm_intake.PENDING_MAX)

    def test_open_brainstorm_cap(self):
        with mock.patch.object(brainstorm_intake, "OPEN_MAX", 2):
            self.open_thread("a", ts="1800000001.000100")
            self.open_thread("b", ts="1800000002.000100")
            self.assertEqual(self.send("c", ts="1800000003.000100"), {"ok": False})
        self.assertEqual(len(self.ids()), 2)


class Gate(Intake):
    """Everything that must leave no trace at all."""

    def assert_nothing(self, *args, **kw):
        bid = self.open_thread("seed", ts=getattr(self, "seed_ts", f"18000{self.n + 5000}.000100"))
        before = self.snapshot()
        self.assertEqual(self.send(*args, **kw), {"ok": False})
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(len(self.pending(bid)), 1)

    def test_a_stranger_in_the_thread_gets_nothing(self):
        self.seed_ts = "1800000050.000100"
        self.assert_nothing("hi", ts="1800000060.1", thread_ts=self.seed_ts, user="U0OTHER9")

    def test_a_stranger_top_level_post_opens_nothing(self):
        self.assert_nothing("hi", user="U0OTHER9")

    def test_another_channel_is_not_the_brainstorm_channel(self):
        self.assert_nothing("hi", channel="C0ELSEWH")

    def test_another_workspace_or_app_is_refused(self):
        self.assert_nothing("hi", team="T0OTHER9")
        self.assert_nothing("hi", app="A0OTHER9")

    def test_bots_and_subtypes_are_refused(self):
        self.assert_nothing("hi", bot_id="B0BOT01")
        self.assert_nothing("hi", subtype="message_changed")

    def test_a_malformed_event_is_dropped(self):
        for text in (None, 7, ""):
            self.assert_nothing(text)
        self.assert_nothing("hi", ts="not-a-ts")

    def test_a_missing_host_id_turns_intake_off_and_logs_once(self):
        for doc in ({"slack_owner_id": OWNER, "slack_team_id": TEAM},
                    {"slack_owner_id": OWNER, "slack_app_id": APP}, {"slack_owner_id": OWNER}):
            self.write_host(doc)
            (self.home / brainstorm_intake.OFF_MARK).unlink(missing_ok=True)
            with self.assertLogs("capsule.brainstorm_intake", "WARNING") as logs:
                self.assertEqual(self.send(), {"ok": False})
                self.assertEqual(self.send(ts="1800000002.000100"), {"ok": False})
                brainstorm_intake.log.warning("sentinel")
            self.assertEqual(len(logs.records), 2)       # one off line, then the sentinel
            self.assertEqual(self.ids(), [])

    def test_a_click_from_another_workspace_is_refused_when_ids_are_set(self):
        click = {"type": "block_actions", "user": {"id": OWNER}, "team": {"id": "T0OTHER9"},
                 "api_app_id": APP, "channel": {"id": "C1"}, "message": {"ts": "5.5"},
                 "actions": [{"value": "ask:0123456789abcdef:yes"}]}
        done = slack_answer.Result(True, "", "x", "")
        with mock.patch.object(slack_answer, "answer", return_value=done) as answer:
            self.assertEqual(slack_answer._handle(self.home, click), {"ok": False})
            answer.assert_not_called()
            slack_answer._handle(self.home, dict(click, team={"id": TEAM}))
            answer.assert_called_once()

    def test_a_click_still_works_when_the_host_ids_are_not_set(self):
        self.write_host({"slack_owner_id": OWNER})
        click = {"type": "block_actions", "user": {"id": OWNER}, "channel": {"id": "C1"},
                 "message": {"ts": "5.5"}, "actions": [{"value": "ask:0123456789abcdef:yes"}]}
        with mock.patch.object(slack_answer, "answer",
                               return_value=slack_answer.Result(True, "", "x", "")) as answer:
            self.assertEqual(slack_answer._handle(self.home, click), {"ok": True})
            answer.assert_called_once()


class Limits(Intake):
    def test_a_long_post_gets_one_fixed_split_it_reply_and_nothing_else(self):
        payload = self.event("y" * (brainstorm_intake.TEXT_MAX + 1))
        self.assertEqual(slack_answer._handle(self.home, payload), {"ok": False})
        slack_answer._handle(self.home, payload)                       # redelivered
        self.assertEqual(self.ids(), [])
        ((body, key),) = self.slack_rows()
        self.assertEqual(body, brainstorm_intake.SPLIT)
        self.assertTrue(key.startswith("brainstorm-reply:"))
        row = self.conn.execute("SELECT thread_key FROM outbox").fetchone()
        self.assertEqual(row[0], f"{CHANNEL}|1800000001.000100")

    def test_exactly_4000_characters_is_accepted(self):
        self.assertEqual(self.send("z" * brainstorm_intake.TEXT_MAX), {"ok": True})

    def test_a_long_reply_in_a_thread_is_answered_with_the_split_line(self):
        bid = self.open_thread()
        self.send("w" * 4001, ts="1800000009.5", thread_ts="1800000001.000100")
        self.assertEqual(len(self.pending(bid)), 1)
        self.assertEqual([r[0] for r in self.slack_rows()], [brainstorm_intake.SPLIT])


class Done(Intake):
    def setUp(self):
        super().setUp()
        self.vault = Path(tempfile.mkdtemp())
        patcher = mock.patch.dict(os.environ, {"OBSIDIAN_VAULT_PATH": str(self.vault)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def done(self, **kw):
        return self.send("done", ts="1800000009.5", thread_ts="1800000001.000100", **kw)

    def test_done_marks_it_closed_fast_confirms_and_leaves_the_vault_to_the_runner(self):
        bid = self.open_thread()
        with mock.patch.object(brainstorm, "publish") as publish, \
                mock.patch.object(brainstorm, "close") as close:
            self.assertEqual(self.done(), {"ok": True})
        publish.assert_not_called()                # nothing slow in the listener's child
        close.assert_not_called()
        meta = store.read_meta(self.home, BOARD, bid)
        self.assertEqual((meta["status"], "note" in meta), ("closed", False))
        self.assertEqual(store.read_turns(self.home, BOARD, bid)[-1]["text"], "closed")
        self.assertEqual([r[0] for r in self.slack_rows()], [brainstorm_intake.CLOSED])
        self.assertEqual(list(self.vault.iterdir()), [])

    def test_the_runner_then_writes_the_vault_note_once(self):
        bid = self.open_thread()
        self.done()
        with mock.patch.dict("sys.modules", {"vault_git": mock.Mock()}):
            note = brainstorm.publish(self.home, BOARD, bid)
            self.assertEqual(brainstorm.publish(self.home, BOARD, bid), note)
        self.assertTrue((self.vault / note).is_file())
        self.assertIn("Pricing for beta", (self.vault / note).read_text())
        self.assertEqual(store.read_meta(self.home, BOARD, bid)["note"], note)

    def test_a_failed_close_says_so_in_the_thread(self):
        self.open_thread()
        with mock.patch.object(store, "mark_closed", side_effect=OSError("disk")):
            self.assertEqual(self.done(), {"ok": False})
        (row,) = self.slack_rows()
        self.assertIn("capsule brainstorm close", row[0])

    def test_done_is_the_exact_word_only(self):
        bid = self.open_thread()
        for text in ("done?", "I am done", "undone", "done done"):
            self.send(text, ts="1800000009.5", thread_ts="1800000001.000100")
        self.assertEqual(store.read_meta(self.home, BOARD, bid)["status"], "open")
        self.send(" Done \n", ts="1800000010.5", thread_ts="1800000001.000100")
        self.assertEqual(store.read_meta(self.home, BOARD, bid)["status"], "closed")

    def test_a_stranger_cannot_close_it(self):
        bid = self.open_thread()
        self.done(user="U0OTHER9")
        self.assertEqual(store.read_meta(self.home, BOARD, bid)["status"], "open")

    def test_a_closed_thread_takes_no_more_replies_and_a_stray_done_opens_nothing(self):
        bid = self.open_thread()
        store.update_meta(self.home, BOARD, bid, status="closed")
        before = self.snapshot()
        self.assertEqual(self.send("more", ts="1800000009.5", thread_ts="1800000001.000100"),
                         {"ok": False})
        self.assertEqual(self.send("done", ts="1800000020.5"), {"ok": False})
        self.assertEqual(self.snapshot(), before)

    def test_a_redelivered_opener_of_a_closed_thread_opens_nothing(self):
        payload = self.event()
        slack_answer._handle(self.home, payload)
        self.done()
        before = self.snapshot()
        self.assertEqual(slack_answer._handle(self.home, payload), {"ok": False})
        self.assertEqual(self.snapshot(), before)

    def test_thread_broadcast_is_a_reply_and_other_subtypes_are_not(self):
        bid = self.open_thread()
        self.assertEqual(self.send("also here", ts="1800000009.5", thread_ts="1800000001.000100",
                                   subtype="thread_broadcast"), {"ok": True})
        self.assertEqual(len(self.pending(bid)), 2)

    def test_a_bad_thread_ts_is_dropped(self):
        self.open_thread()
        before = self.snapshot()
        self.assertEqual(self.send("hi", ts="1800000009.5", thread_ts="x/../y"), {"ok": False})
        self.assertEqual(self.snapshot(), before)


class Plist(unittest.TestCase):
    def test_the_runner_job_gets_the_vault_path_like_the_tick(self):
        text = (paths.REPO / "launchd" / "templates"
                / "__PREFIX__.capsule-corp-brainstorm.plist").read_text()
        self.assertIn("<key>OBSIDIAN_VAULT_PATH</key><string>__VAULT__</string>", text)


class Lock(Intake):
    def test_update_meta_holds_a_per_thread_flock(self):
        import fcntl
        bid = self.open_thread()
        with open(store.bdir(self.home, BOARD, bid) / ".meta.lock", "a") as other, \
                store.locked(self.home, BOARD, bid):
            with self.assertRaises(BlockingIOError):
                fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)


class StoreHelpers(Base):
    def test_create_is_exclusive_and_add_pending_validates_and_dedupes(self):
        bid = "0123456789abcdef"
        store.create(self.home, BOARD, bid, {"status": "open"})
        with self.assertRaises(FileExistsError):
            store.create(self.home, BOARD, bid, {"status": "open"})
        self.assertTrue(brainstorm_intake._add_pending(self.home, BOARD, bid, "Ev1", "hi", NOW))
        self.assertFalse(brainstorm_intake._add_pending(self.home, BOARD, bid, "Ev1", "again", NOW))
        for bad in ("../x", "a/b", "", "x" * 65):
            with self.assertRaises(ValueError):
                brainstorm_intake._add_pending(self.home, BOARD, bid, bad, "hi", NOW)
        self.assertEqual(store.list_pending(self.home, BOARD, bid),
                         [{"event_id": "Ev1", "text": "hi", "ts": NOW}])


if __name__ == "__main__":
    unittest.main()
