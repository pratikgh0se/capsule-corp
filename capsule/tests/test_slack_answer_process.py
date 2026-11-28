"""D225: the listener's process entry in capsule/slack_answer.py (`start`, `event`) and the
seams that moved out of the listener into it. Fakes only: no Slack, no network."""
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from capsule import answer_base, db, paths, slack, slack_answer
from capsule.tests.test_slack import SLACK
from capsule.tests.test_slack_answer import OWNER, SlackAnswerCase

KEY = "0123456789abcdef"
HOME = Path("/nonexistent-home")


def run(fn, *args):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = fn(*args)
    return rc, buf.getvalue()


class Handle(unittest.TestCase):
    """`_handle` over slack_answer.answer(home, user_id, button_value= | key=, text=): faked."""

    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        path = self.home / "boards" / "b" / "kanban.db"
        db.create_board(path)
        conn = db.connect(path)
        conn.execute("INSERT INTO slack_msg (key, kind, channel, ts, state, text, posted_at,"
                     " updated_at) VALUES (?,?,?,?,?,?,?,?)",
                     (f"needs:ask-{KEY}", "needs", "C1", "5.5", "open", "t", 1, 1))
        conn.commit()
        conn.close()
        self.calls, self.posted, self.reply = [], [], "Saved."
        env = mock.patch.dict("os.environ")      # these tests drive the real-send path
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("AGENT_OPS_NO_SEND", None)

        def answer(home, user_id, **kw):
            self.calls.append((home, user_id, kw))
            return slack_answer.Result(True, "", "unblock", self.reply)

        for patcher in (mock.patch.object(slack_answer, "answer", answer),
                        mock.patch.object(slack_answer, "_block", return_value=SLACK),
                        mock.patch.object(slack, "Sender",
                                          return_value=self.posted.append)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def click(self):
        return {"type": "block_actions", "user": {"id": "U1"}, "channel": {"id": "C1"},
                "message": {"ts": "5.5"}, "actions": [{"value": f"ask:{KEY}:yes"}]}

    def reply_event(self, thread_ts="5.5"):
        ev = {"type": "message", "user": "U1", "channel": "C1", "thread_ts": thread_ts,
              "text": "no"}
        return {"type": "event_callback", "event": ev}

    def test_a_click_passes_the_user_and_the_button_value_and_posts_in_thread(self):
        self.assertEqual(slack_answer._handle(self.home, self.click()), {"ok": True})
        self.assertEqual(self.calls, [(self.home, "U1", {"button_value": f"ask:{KEY}:yes"})])
        self.assertEqual(self.posted, [{"thread": {"channel": "C1", "ts": "5.5"},
                                        "body": "Saved."}])

    def test_a_thread_reply_passes_the_ask_key_of_its_parent_message(self):
        slack_answer._handle(self.home, self.reply_event())
        self.assertEqual(self.calls, [(self.home, "U1", {"key": KEY, "text": "no"})])

    def test_a_reply_in_a_thread_that_is_not_an_ask_is_ignored(self):
        self.assertEqual(slack_answer._handle(self.home, self.reply_event("9.9")), {"ok": False})
        self.assertEqual((self.calls, self.posted), ([], []))

    def test_an_empty_reply_posts_nothing(self):
        self.reply = ""
        self.assertEqual(slack_answer._handle(self.home, self.click()), {"ok": True})
        self.assertEqual(self.posted, [])

    def test_a_failed_post_still_reports_the_answer(self):
        with mock.patch.object(slack, "Sender", side_effect=RuntimeError("x")):
            self.assertEqual(slack_answer._handle(self.home, self.click()), {"ok": True})


class Start(unittest.TestCase):
    def start(self, blk=None, exc=None, repair=None):
        patches = [mock.patch.object(slack_answer, "_block", return_value=blk, side_effect=exc),
                   mock.patch.object(slack_answer, "repair", repair or (lambda home: []))]
        with patches[0], patches[1]:
            return slack_answer._start(HOME)

    def test_slack_off_is_off_and_an_unusable_config_is_an_error(self):
        self.assertIn("off", self.start(None))
        self.assertIn("no usable slack config", self.start(exc=ValueError("bad"))["error"])

    def test_a_config_that_is_not_an_object_is_an_error_not_off(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = Path(d) / "board.json"
            cfg.write_text('["a list"]')
            with mock.patch.object(answer_base, "resolve_config", return_value=cfg):
                with self.assertRaisesRegex(ValueError, "not an object"):
                    slack_answer._block(HOME)
                self.assertIn("no usable slack config", slack_answer._start(HOME)["error"])
                with mock.patch.object(slack_answer, "repair") as repair:
                    rc, out = run(slack_answer.main, ["start"])
                self.assertEqual((rc, "error" in json.loads(out)), (1, True))
                repair.assert_not_called()

    def test_the_inbox_must_be_a_channel_id_and_allowlisted(self):
        for inbox in ("capsule-inbox", None):
            blk = dict(SLACK, channels=dict(SLACK["channels"], inbox=inbox))
            self.assertIn("answers need slack.channels.inbox", self.start(blk)["off"])

    def test_it_repairs_once_and_names_the_identity(self):
        blk = dict(SLACK, channels=dict(SLACK["channels"], inbox="C0123456789"),
                   allowed_channels=[*SLACK["channels"].values(), "C0123456789"], identity="who")
        with mock.patch.object(answer_base, "_allowed", return_value={"C0123456789"}):
            self.assertEqual(self.start(blk, repair=lambda home: ["k1", "k2"]),
                             {"identity": "who", "repaired": 2, "owner": None})

    def test_a_raising_repair_warns_and_the_start_still_succeeds(self):
        def boom(home):
            raise RuntimeError("xapp-1-secret")
        blk = dict(SLACK, channels=dict(SLACK["channels"], inbox="C0123456789"), identity="who")
        with mock.patch.object(answer_base, "_allowed", return_value={"C0123456789"}), \
                self.assertLogs("capsule.slack_answer", "WARNING") as logs:
            self.assertEqual(self.start(blk, repair=boom),
                             {"identity": "who", "repaired": 0, "owner": None})
        self.assertNotIn("xapp-1", "".join(logs.output))

    def test_the_board_config_is_loaded_not_the_legacy_one(self):
        home = Path(tempfile.mkdtemp())
        cfg = json.loads((paths.REPO / "capsule" / "config.example.json").read_text())
        cfg["board"] = "capsule-corp"
        cfg["slack"] = dict(cfg["slack"], enabled=True,
                            channels=dict(cfg["slack"]["channels"], inbox="C0123456789"))
        board = home / "boards" / "capsule-corp"
        board.mkdir(parents=True)
        (board / "config.json").write_text(json.dumps(cfg))
        with mock.patch.dict(os.environ, {"CAPSULE_BOARD": "capsule-corp"}):
            blk = slack_answer._block(home)           # no legacy home/config.json exists
        self.assertEqual(blk["channels"]["inbox"], "C0123456789")


class Main(unittest.TestCase):
    def test_start_prints_one_json_line(self):
        with mock.patch.object(slack_answer, "_start", return_value={"off": "x"}):
            self.assertEqual(run(slack_answer.main, ["start"]), (0, '{"off": "x"}\n'))

    def test_repair_by_hand_prints_the_keys_written(self):
        with mock.patch.object(slack_answer, "repair", return_value=["k1"]):
            self.assertEqual(run(slack_answer.main, ["repair"]), (0, '{"repaired": ["k1"]}\n'))

    def test_check_names_the_identity_and_never_repairs(self):
        blk = dict(SLACK, channels=dict(SLACK["channels"], inbox="C0123456789"), identity="who")
        with mock.patch.object(slack_answer, "_block", return_value=blk), \
                mock.patch.object(slack, "_allowed", return_value={"C0123456789"}), \
                mock.patch.object(slack_answer, "repair") as repair:
            rc, out = run(slack_answer.main, ["check"])
        want = {"identity": "who", "repaired": 0, "owner": None}
        self.assertEqual((rc, json.loads(out)), (0, want))
        repair.assert_not_called()

    def test_an_unknown_command_is_refused(self):
        self.assertEqual(run(slack_answer.main, ["nope"])[0], 2)

    def test_event_with_garbage_on_stdin_is_not_ok_and_never_raises(self):
        with mock.patch("sys.stdin", io.StringIO("not json")), \
                self.assertLogs("capsule.slack_answer", "WARNING"):
            self.assertEqual(run(slack_answer.main, ["event"]), (0, '{"ok": false}\n'))


class RoundTrip(SlackAnswerCase):
    def test_a_strangers_click_through_the_real_module_entry_is_not_ok(self):
        key = self.ask(self.triage)["key"]
        payload = {"type": "block_actions", "user": {"id": "U0STRANGER"},
                   "actions": [{"value": slack_answer.value(key, "yes")}]}
        env = dict(os.environ, CAPSULE_HOME=str(self.home), AGENT_OPS_NO_SEND="1")
        out = subprocess.run([sys.executable, "-m", "capsule.slack_answer", "event"],
                             cwd=paths.REPO, env=env, input=json.dumps(payload), text=True,
                             capture_output=True, timeout=60, check=False)
        self.assertEqual((out.returncode, json.loads(out.stdout)), (0, {"ok": False}))
        self.assertEqual(self.human_files(), [])

    def test_an_accepted_answer_loads_only_protected_modules(self):
        """The decision path imports only the pinned, protected closure (no slack, config, db or
        spool): a worker edit to an open module cannot change a trust decision."""
        key = self.ask(self.triage)["key"]
        code = ("import json, sys\nfrom pathlib import Path\nfrom capsule import slack_answer\n"
                "h, who, val = Path(sys.argv[1]), sys.argv[2], sys.argv[3]\n"
                "res = slack_answer.answer(h, who, button_value=val)\n"
                "print(json.dumps([res.accepted, sorted(m for m in sys.modules"
                " if m.startswith(('capsule', 'maintenance')))]))\n")
        env = dict(os.environ, CAPSULE_HOME=str(self.home), AGENT_OPS_NO_SEND="1")
        out = subprocess.run([sys.executable, "-B", "-c", code, str(self.home), OWNER,
                              slack_answer.value(key, "yes")], cwd=paths.REPO, env=env,
                             text=True, capture_output=True, timeout=60, check=False)
        accepted, loaded = json.loads(out.stdout)
        self.assertTrue(accepted, out.stderr)
        self.assertEqual(loaded, sorted(["capsule", *(f"capsule.{m}" for m in (
            "answer_base", "answer_scope", "human", "jsonschema", "row_schemas",
            "slack_answer"))]))

    PROTECTED = sorted(["capsule", *(f"capsule.{m}" for m in (
        "answer_base", "answer_scope", "human", "jsonschema", "row_schemas", "slack_answer"))])

    def event_child(self, payload):
        """Run `main(["event"])` in a child on `payload`: (rc, verdict, snapshots taken inside
        each `human.write`, whether `brainstorm_intake` was loaded at the end)."""
        code = ("import json, sys\nfrom capsule import human, slack_answer\n"
                "seen, real = [], human.write\n"
                "def spy(*a, **kw):\n"
                "    seen.append(sorted(m for m in sys.modules"
                " if m.startswith(('capsule', 'maintenance'))))\n"
                "    return real(*a, **kw)\n"
                "human.write = spy\n"
                "rc = slack_answer.main(['event'])\n"
                "print(json.dumps([rc, seen, 'capsule.brainstorm_intake' in sys.modules]))\n")
        env = dict(os.environ, CAPSULE_HOME=str(self.home), AGENT_OPS_NO_SEND="1")
        out = subprocess.run([sys.executable, "-B", "-c", code], cwd=paths.REPO, env=env,
                             input=json.dumps(payload), text=True, capture_output=True,
                             timeout=60, check=False)
        verdict, report = out.stdout.splitlines()
        return (*json.loads(report)[:1], json.loads(verdict), *json.loads(report)[1:])

    def seeded_reply(self):
        """A typed owner reply in the thread of a seeded ask (its `needs:ask-` row)."""
        key = self.ask(self.triage)["key"]
        self.conn.execute("INSERT INTO slack_msg (key, kind, channel, ts, state, text, posted_at,"
                          " updated_at) VALUES (?,?,?,?,?,?,?,?)",
                          (f"needs:ask-{key}", "needs", "C1", "5.5", "open", "t", 1, 1))
        self.conn.commit()
        ev = {"type": "message", "user": OWNER, "channel": "C1", "thread_ts": "5.5", "text": "yes"}
        return {"type": "event_callback", "event": ev}

    def test_the_event_entry_decides_before_any_lazy_import(self):
        """`main(["event"])` records the decision having loaded only the protected closure: the
        lazy `slack` / `brainstorm_intake` imports in `_handle` must stay after the decision."""
        key = self.ask(self.triage)["key"]
        payload = {"type": "block_actions", "user": {"id": OWNER},
                   "actions": [{"value": slack_answer.value(key, "yes")}]}   # no reply target
        rc, verdict, seen, _ = self.event_child(payload)
        self.assertEqual((rc, verdict, seen), (0, {"ok": True}, [self.PROTECTED]))

    def test_a_typed_thread_reply_decides_before_any_lazy_import(self):
        rc, verdict, seen, _ = self.event_child(self.seeded_reply())
        self.assertEqual((rc, verdict, seen), (0, {"ok": True}, [self.PROTECTED]))

    def test_a_non_brainstorm_event_never_loads_brainstorm_intake(self):
        """Only an event in the brainstorm channel may pull the open intake module in."""
        self.assertFalse(self.event_child(self.seeded_reply())[3])

if __name__ == "__main__":
    unittest.main()
