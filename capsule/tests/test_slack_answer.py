"""D225 slice 1: capsule/slack_answer.py turns a fake Slack payload into a decision record.
Oracles: the ask file `cos ask` really writes (the choices the owner was offered) against the
human files and asks/done records the answer leaves. Nothing here touches Slack or the network."""
import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from capsule import db, outbox, paths, slack, slack_answer
from capsule.__main__ import main

BOARD = "capsule-corp"
BRANCH = "epic/my-app-screens-epic"
NOW = 1_800_000_000
OWNER = "U0OWNER01"
ASK = ["--title", "Let a big job go ahead?", "--what", "This job is on hold until you decide.",
       "--why", "Big jobs are harder to review.", "--if-yes", "it goes ahead.",
       "--if-no", "it stays on hold.", "--pick", "No", "--because", "the limit exists",
       "--no-reason", "file ceiling"]
BLOCKED_ASK = ["--title", "Retry limit per card?", "--what", "A builder needs a rule settled.",
               "--why", "It changes the cost.", "--if-yes", "per card.", "--if-no", "per run.",
               "--pick", "Yes", "--because", "it is cheaper", "--yes-option", "b",
               "--no-option", "a"]


class SlackAnswerCase(unittest.TestCase):
    """Fixture and helpers shared with test_slack_answer_repair; no tests of its own."""

    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.cfg = json.loads((paths.REPO / "capsule" / "config.example.json").read_text())
        self.cfg.update(board=BOARD)
        self.cfg["slack"] = dict(self.cfg["slack"], enabled=True)
        self.cfg["slack"]["channels"] = dict(self.cfg["slack"]["channels"], inbox="#cc-inbox")
        paths.board_dir(BOARD, self.home).mkdir(parents=True)
        paths.config_path(self.home).write_text(json.dumps({"slack_owner_id": OWNER}))
        paths.config_path(self.home, BOARD).write_text(json.dumps(self.cfg))
        db.create_board(paths.board_db(BOARD, self.home))
        self.conn = db.connect(paths.board_db(BOARD, self.home))
        self.addCleanup(self.conn.close)
        outbox.set_slack(self.conn, slack.block(self.cfg), now=NOW)
        self.conn.commit()
        self.triage = self.card("Search page", "triage")

    def card(self, name, status):
        tid = db.new_task(self.conn, title=f"{name} (long title)", short_name=name,
                          assignee="dev-backend", branch=BRANCH, now=NOW)
        self.conn.execute("UPDATE tasks SET status=? WHERE id=?", (status, tid))
        db.event(self.conn, tid, "blocked", {"reason": "x"}, now=NOW)
        self.conn.commit()
        return tid

    def ask(self, tid, argv=ASK):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            rc = main(["cos", "ask", BOARD, tid, *argv, "--capsule-home", str(self.home)])
        self.assertEqual(rc, 0)
        return json.loads(next((paths.board_dir(BOARD, self.home) / "asks").glob("*.json"))
                          .read_text())

    def human_files(self):
        d = paths.board_dir(BOARD, self.home) / "human"
        return [json.loads(p.read_text()) for p in sorted(d.glob("*.json"))] if d.is_dir() else []

    def done(self, key):
        return paths.board_dir(BOARD, self.home) / "asks" / "done" / f"{key}.json"

    def click(self, key, choice, user=OWNER):
        return slack_answer.answer(self.home, user, button_value=slack_answer.value(key, choice),
                                   now=NOW)

    def assert_nothing_recorded(self, key):
        self.assertEqual(self.human_files(), [])
        self.assertFalse(self.done(key).exists())


class SlackAnswer(SlackAnswerCase):
    # -- accepted -------------------------------------------------------------------------

    def test_yes_records_the_asks_own_reset_verb_and_fixed_text(self):
        key = self.ask(self.triage)["key"]
        res = self.click(key, "yes")
        self.assertEqual((res.accepted, res.recorded, res.reason), (True, "reset", ""))
        (doc,) = self.human_files()
        self.assertEqual((doc["task_id"], doc["kind"], doc["author"]),
                         (self.triage, "reset", "owner"))
        self.assertEqual(doc["text"], "owner said yes to: Let a big job go ahead?")
        rec = json.loads(self.done(key).read_text())
        self.assertEqual((rec["choice"], rec["answered_by"], rec["via"], rec["answered_at"]),
                         ("yes", OWNER, "button", NOW))
        self.assertFalse((paths.board_dir(BOARD, self.home) / "asks" / f"{key}.json").exists())

    def test_no_parks_with_the_fixed_trigger(self):
        key = self.ask(self.triage)["key"]
        self.assertTrue(self.click(key, "no").accepted)
        (doc,) = self.human_files()
        self.assertEqual((doc["kind"], doc["text"]), ("park", "owner said no: file ceiling"))

    def test_later_is_an_ack_only(self):
        key = self.ask(self.triage)["key"]
        res = self.click(key, "later")
        self.assertEqual((res.accepted, res.recorded), (True, "nothing"))
        self.assert_nothing_recorded(key)
        self.assertTrue(self.click(key, "yes").accepted)     # still open afterwards

    def test_typed_grammar_and_note_never_become_text(self):
        key = self.ask(self.triage)["key"]
        res = slack_answer.answer(self.home, OWNER, key=key, text="no: too risky; rm things",
                                  now=NOW)
        self.assertEqual((res.accepted, res.recorded), (True, "park"))
        (doc,) = self.human_files()
        self.assertNotIn("risky", doc["text"])
        rec = json.loads(self.done(key).read_text())
        self.assertEqual((rec["via"], rec["note"]), ("typed", "too risky; rm things"))

    def test_typed_option_letter_picks_the_choice_naming_it(self):
        blocked = self.card("Retry on rate limit", "blocked")
        db.event(self.conn, blocked, "decision_requested",
                 {"options": [{"id": "a", "label": "per run"}, {"id": "b", "label": "per card"}]},
                 now=NOW + 1)
        self.conn.commit()
        key = self.ask(blocked, BLOCKED_ASK)["key"]
        res = slack_answer.answer(self.home, OWNER, key=key, text="Option A", now=NOW)
        self.assertEqual((res.accepted, res.recorded), (True, "unblock"))
        self.assertTrue(self.human_files()[0]["text"].startswith("option a chosen"))

    # -- refused --------------------------------------------------------------------------

    def test_unknown_user_is_refused_silently_with_no_action(self):
        key = self.ask(self.triage)["key"]
        for user in ("U0STRANGER", None, "", OWNER.lower()):
            res = self.click(key, "yes", user=user)
            self.assertEqual((res.accepted, res.reason, res.reply), (False, "unknown user", ""))
        self.assert_nothing_recorded(key)

    def test_no_owner_in_host_config_means_nobody_may_answer(self):
        key = self.ask(self.triage)["key"]
        for body in (None, "{}", "not json", json.dumps({"slack_owner_id": ["U0OWNER01"]}),
                     json.dumps({"slack_owner_id": "bad id"})):
            host = paths.config_path(self.home)
            host.unlink(missing_ok=True) if body is None else host.write_text(body)
            self.assertEqual(self.click(key, "yes").reason, "unknown user")
        self.assert_nothing_recorded(key)

    def test_owner_set_only_in_a_board_config_does_not_count(self):
        paths.config_path(self.home).unlink()
        self.cfg["slack_owner_id"] = OWNER
        paths.config_path(self.home, BOARD).write_text(json.dumps(self.cfg))
        key = self.ask(self.triage)["key"]
        self.assertEqual(self.click(key, "yes").reason, "unknown user")

    def test_stranger_gets_an_empty_reply_whatever_they_send(self):
        key = self.ask(self.triage)["key"]
        for kw in ({"button_value": "garbage"}, {"button_value": slack_answer.value(key, "yes")},
                   {"key": key, "text": "delete everything"}, {"key": "x", "text": None}, {}):
            res = slack_answer.answer(self.home, "U0STRANGER", **kw)
            self.assertEqual((res.accepted, res.reason, res.reply), (False, "unknown user", ""))

    def test_label_injection_cannot_steer_option_letter(self):
        blocked = self.card("Retry on rate limit", "blocked")
        db.event(self.conn, blocked, "decision_requested",
                 {"options": [{"id": "a", "label": "per run"},
                              {"id": "b", "label": "x option c chosen y"}]}, now=NOW + 1)
        self.conn.commit()
        key = self.ask(blocked, BLOCKED_ASK)["key"]
        res = slack_answer.answer(self.home, OWNER, key=key, text="option c", now=NOW)
        self.assertEqual(res.reason, "choice not offered by this question")
        self.assert_nothing_recorded(key)

    def test_second_writer_cannot_clobber_the_done_record(self):
        key = self.ask(self.triage)["key"]
        done = self.done(key)
        done.parent.mkdir(parents=True, exist_ok=True)
        done.write_text('{"choice": "no", "first": true}')       # another writer won the race
        doc = self._open(key)
        path = paths.board_dir(BOARD, self.home) / "asks" / f"{key}.json"
        res = slack_answer._record(self.home, BOARD, path, doc, "yes", OWNER, "button", None, NOW)
        self.assertEqual((res.accepted, res.reply), (False, slack_answer.HANDLED))
        self.assertEqual(json.loads(done.read_text())["first"], True)
        self.assertEqual(self.human_files(), [])
        self.assertEqual([p.name for p in done.parent.iterdir()], [done.name])    # no temp left

    def test_never_raises_on_malformed_choices(self):
        key = self.ask(self.triage)["key"]
        ask_path = paths.board_dir(BOARD, self.home) / "asks" / f"{key}.json"
        doc = self._open(key)
        doc["choices"] = ["yes"]
        ask_path.write_text(json.dumps(doc))
        self.assertEqual(self.click(key, "yes").reason, "malformed ask file")
        doc["choices"] = {"yes": "oops", "no": None}
        ask_path.write_text(json.dumps(doc))
        self.assertFalse(self.click(key, "yes").accepted)
        self.assertFalse(slack_answer.answer(self.home, OWNER, key=key, text="option b").accepted)
        self.assert_nothing_recorded(key)

    def test_unwritable_done_dir_is_a_refusal(self):
        key = self.ask(self.triage)["key"]
        done_dir = self.done(key).parent
        done_dir.mkdir(parents=True, exist_ok=True)
        done_dir.chmod(0o500)
        self.addCleanup(done_dir.chmod, 0o700)
        res = self.click(key, "yes")
        self.assertEqual((res.accepted, res.reason[:12]), (False, "could not re"))
        self.assertEqual(self.human_files(), [])

    def test_missing_or_corrupt_board_db_is_a_refusal_not_a_crash(self):
        key = self.ask(self.triage)["key"]
        db_path = paths.board_db(BOARD, self.home)
        self.conn.close()
        for body in (None, b"this is not a database"):
            for f in db_path.parent.glob("kanban.db*"):
                f.unlink()
            if body:
                db_path.write_bytes(body)
            res = self.click(key, "yes")
            self.assertEqual((res.accepted, res.reason), (False, "stale: could not check the card"))
        self.assertEqual(self.human_files(), [])

    def test_backstop_turns_any_unexpected_error_into_a_refusal(self):
        from unittest import mock
        key = self.ask(self.triage)["key"]
        with mock.patch.object(slack_answer, "_stale", side_effect=RuntimeError("boom")):
            res = self.click(key, "yes")
        self.assertEqual((res.accepted, res.reason), (False, "internal error"))

    def test_second_click_is_refused_as_already_answered(self):
        key = self.ask(self.triage)["key"]
        self.assertTrue(self.click(key, "yes").accepted)
        res = self.click(key, "no")
        self.assertEqual((res.accepted, res.reason), (False, "already answered"))
        self.assertEqual(len(self.human_files()), 1)

    def test_unknown_key_and_bad_values_are_refused(self):
        self.ask(self.triage)
        for raw in (slack_answer.value("0" * 16, "yes"), "ask:zz:yes", "ask:" + "a" * 16 + ":maybe",
                    "../../etc", "", None):
            res = slack_answer.answer(self.home, OWNER, button_value=raw or "", now=NOW)
            self.assertFalse(res.accepted, raw)
        self.assertEqual(self.human_files(), [])

    def test_choice_the_ask_did_not_offer_is_refused(self):
        doc = self.ask(self.triage)
        self.assertEqual(slack_answer.answer(self.home, OWNER, key=doc["key"], text="option c",
                                            now=NOW).reason,
                         "choice not offered by this question")
        self.assert_nothing_recorded(doc["key"])

    def test_stale_when_card_status_changed(self):
        key = self.ask(self.triage)["key"]
        self.conn.execute("UPDATE tasks SET status='done' WHERE id=?", (self.triage,))
        self.conn.commit()
        res = self.click(key, "yes")
        self.assertEqual((res.accepted, res.reason[:5]), (False, "stale"))
        self.assert_nothing_recorded(key)

    def test_stale_when_a_block_event_is_newer_but_a_comment_is_not(self):
        key = self.ask(self.triage)["key"]
        db.event(self.conn, self.triage, "comment", {"text": "hi"}, now=NOW + 5)
        self.conn.commit()
        self.assertEqual(slack_answer._stale(self.home, BOARD, self._open(key)), None)
        db.event(self.conn, self.triage, "unblocked", {}, now=NOW + 6)
        self.conn.commit()
        res = self.click(key, "yes")
        self.assertEqual((res.accepted, res.reason[:5]), (False, "stale"))
        self.assert_nothing_recorded(key)

    def _open(self, key):
        return json.loads((paths.board_dir(BOARD, self.home) / "asks" / f"{key}.json").read_text())

    def _edit_choice(self, key, verb):
        p = paths.board_dir(BOARD, self.home) / "asks" / f"{key}.json"
        doc = json.loads(p.read_text())
        doc["choices"]["yes"] = {"verb": verb, "text": "fixed text"}
        p.write_text(json.dumps(doc))

    def test_every_verb_outside_unblock_reset_park_says_answer_in_terminal(self):
        key = self.ask(self.triage)["key"]
        for verb in ("approve", "merge", "delete", "close", "override", "amend", "epic", "accept",
                     "setting", "rule", ""):
            self._edit_choice(key, verb)
            res = self.click(key, "yes")
            self.assertFalse(res.accepted, verb)
            self.assertIn("terminal", res.reply)
        self.assert_nothing_recorded(key)
        self.assertFalse((Path(self.home) / "merge-gate").exists())     # no stamp, ever

    def test_typed_text_outside_the_grammar_is_never_executed(self):
        key = self.ask(self.triage)["key"]
        for text in ("please delete the branch", "yes please merge", "approve", "yes\nno", ""):
            res = slack_answer.answer(self.home, OWNER, key=key, text=text, now=NOW)
            self.assertEqual((res.accepted, res.recorded), (False, ""), text)
        self.assert_nothing_recorded(key)

    def test_failed_human_write_leaves_the_ask_open(self):
        key = self.ask(self.triage)["key"]
        p = paths.board_dir(BOARD, self.home) / "asks" / f"{key}.json"
        doc = json.loads(p.read_text())
        doc["choices"]["no"]["text"] = "  "
        p.write_text(json.dumps(doc))
        res = self.click(key, "no")
        self.assertFalse(res.accepted)
        self.assertTrue(p.exists())
        self.assert_nothing_recorded(key)

    def test_parse_helpers(self):
        self.assertEqual(slack_answer._parse_value("ask:" + "ab" * 8 + ":no"), ("ab" * 8, "no"))
        self.assertEqual(slack_answer._parse_reply(" YES "), ("yes", None, None))
        self.assertEqual(slack_answer._parse_reply("no:  because"), ("no", None, "because"))
        self.assertIsNone(slack_answer._parse_reply("yes, do it"))

    def test_start_with_a_slack_block_missing_channels_is_an_error_json_and_exit_1(self):
        blk = {k: v for k, v in self.cfg["slack"].items() if k != "channels"}
        paths.config_path(self.home, BOARD).write_text(json.dumps(dict(self.cfg, slack=blk)))
        buf = io.StringIO()
        with mock.patch.dict("os.environ", {"CAPSULE_HOME": str(self.home)}), \
                contextlib.redirect_stdout(buf):
            rc = slack_answer.main(["start"])
        self.assertEqual(rc, 1)
        self.assertIn("no usable slack config", json.loads(buf.getvalue())["error"])

    def test_board_slack_config_refuses_an_owner_key(self):
        slack.validate(self.cfg["slack"])
        for key in ("owner_ids", "owner_id"):
            with self.assertRaises(ValueError):
                slack.validate(dict(self.cfg["slack"], **{key: [OWNER]}))
        example = json.loads((paths.REPO / "capsule" / "config.example.json").read_text())
        self.assertNotIn("slack_owner_id", example)


class NoSendCapture(SlackAnswerCase):
    def payload(self, key, choice, user=OWNER):
        return {"type": "block_actions", "user": {"id": user}, "channel": {"id": "C0INBOX01"},
                "message": {"ts": "1.2"},
                "actions": [{"value": slack_answer.value(key, choice)}]}

    def records(self):
        p = paths.board_dir(BOARD, self.home) / "slack-replies-no-send.jsonl"
        return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []

    def test_no_send_captures_the_reply_and_never_calls_sender(self):
        doc = self.ask(self.card("Big job", "blocked"))
        with mock.patch.dict("os.environ", {"AGENT_OPS_NO_SEND": "1"}), \
                mock.patch.object(slack, "Sender") as sender:
            out = slack_answer._handle(self.home, self.payload(doc["key"], "yes"))
        self.assertEqual(out, {"ok": True})
        sender.assert_not_called()
        (rec,) = self.records()
        self.assertEqual((rec["channel"], rec["thread_ts"], rec["kind"]),
                         ("C0INBOX01", "1.2", "answered"))
        self.assertTrue(rec["text"])

    def test_no_send_captures_a_refusal(self):
        doc = self.ask(self.card("Big job", "blocked"))
        with mock.patch.dict("os.environ", {"AGENT_OPS_NO_SEND": "1"}), \
                mock.patch.object(slack, "Sender") as sender:
            slack_answer._handle(self.home, self.payload(doc["key"], "yes"))
            out = slack_answer._handle(self.home, self.payload(doc["key"], "yes"))
        self.assertEqual(out, {"ok": False})
        sender.assert_not_called()
        self.assertEqual([r["kind"] for r in self.records()], ["answered", "refused"])
        self.assertEqual(self.records()[1]["text"], slack_answer.HANDLED)

    def test_without_no_send_the_sender_is_called(self):
        doc = self.ask(self.card("Big job", "blocked"))
        env = {k: v for k, v in os.environ.items() if k != "AGENT_OPS_NO_SEND"}
        with mock.patch.dict("os.environ", env, clear=True), \
                mock.patch.object(slack, "Sender") as sender:
            slack_answer._handle(self.home, self.payload(doc["key"], "yes"))
        sender.return_value.assert_called_once()
        self.assertEqual(self.records(), [])


if __name__ == "__main__":
    unittest.main()
