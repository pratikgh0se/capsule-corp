# mutation-check: targets-predate-range ff625e94 -- fixture rename only, no behaviour pinned ::
#   Template.test_plist_is_keepalive_throttled_and_listed_unloaded
"""Slack Socket Mode listener and the answer buttons (D225). Nothing here opens a socket, reads a
real token or reaches Slack: connections, the URL call and `handle` are fakes."""
import io
import json
import os
import re
import subprocess
import tempfile
import threading
import unittest
from unittest import mock
from pathlib import Path

from capsule import slack, slack_listen, slack_msgs, slack_secret
from capsule.tests.public_tree import PRIVATE_TREE
from capsule.tests.test_slack import SLACK, FakeHTTP, Tokens

os.environ["AGENT_OPS_NO_SEND"] = "1"
ROOT = Path(__file__).resolve().parents[2]
ASK = ("\U0001F4EC Ship the thing?\nProj · Card\nWhat: x\nWhy: y\nIf yes: a   If no: b\n"
       "My pick: Yes, because z.\n[ Yes ]  [ No ]  [ Later ]")
KEY = "0123456789abcdef"
HOLD = ("\U0001F64B t_a needs you\nreason: stuck\nreply: `python3 -m capsule human b t_a "
        "unblock|reset|accept '<answer>'`")


class Buttons(unittest.TestCase):
    def post(self, row):
        http = FakeHTTP()
        http.script += [{"ok": True, "ts": "1.1"}]
        slack.Sender(SLACK, token=Tokens("xoxb-fake"), urlopen=http)(row)
        return http.calls[0]["body"]

    def test_ask_gets_yes_no_later_buttons_in_slack_answer_format_and_the_same_text(self):
        payload = self.post({"id": 42, "body": ASK, "dedup_key": f"needs:ask-{KEY}"})
        buttons = payload["blocks"][1]["elements"]
        self.assertEqual([b["value"] for b in buttons],
                         [f"ask:{KEY}:yes", f"ask:{KEY}:no", f"ask:{KEY}:later"])
        self.assertTrue(all(re.fullmatch(r"ask:[0-9a-f]{16}:(yes|no|later)", b["value"])
                            for b in buttons))
        self.assertEqual(payload["text"], slack.escape(ASK))

    def test_a_hold_a_keyless_ask_and_other_classes_get_no_blocks(self):
        key = {"dedup_key": f"needs:ask-{KEY}"}
        self.assertNotIn("blocks", self.post({"id": 7, "body": HOLD, "dedup_key": "needs:t_a"}))
        self.assertNotIn("blocks", self.post({"id": 1, "body": "\U0001F534 t_a blocked", **key}))
        self.assertNotIn("blocks", self.post({"id": 2, "body": ASK}))
        self.assertNotIn("blocks", self.post({"id": 2, "body": ASK, "dedup_key": "needs:ask-xyz"}))

    def edit(self, dedup):
        http = FakeHTTP()
        http.script += [{"ok": True, "ts": "1.1"}]
        row = {"id": 9, "body": f"{slack_msgs.HANDLED} done.", "dedup_key": dedup,
               "edit": {"channel": SLACK["channels"]["blocked"], "ts": "1.1"}}
        slack.Sender(SLACK, token=Tokens("xoxb-fake"), urlopen=http)(row)
        return http.calls[0]["body"]

    def test_a_handled_edit_of_an_ask_clears_the_buttons(self):
        self.assertEqual(self.edit(f"needs:ask-{KEY}")["blocks"], [])

    def test_a_handled_edit_of_any_other_message_carries_no_blocks(self):
        self.assertNotIn("blocks", self.edit("needs:t_abc123"))

    def test_an_ask_too_long_for_a_section_is_plain_text_only(self):
        body = ASK + "\n" + "x" * slack.SECTION_MAX
        row = {"id": 3, "body": body, "dedup_key": f"needs:ask-{KEY}"}
        self.assertNotIn("blocks", self.post(row))


class FakeWS:
    """Scripted connection: a str is a message, None is a quiet poll; then EOF."""

    def __init__(self, messages):
        self.messages, self.sent, self.closed = list(messages), [], False
        self.last_rx, self.pings = 0, 0

    def _send(self, op, data):                  # the only thing the listener sends itself: a ping
        self.pings += op == 0x9

    def recv(self):
        if not self.messages:
            raise slack_listen.Closed("eof")
        return self.messages.pop(0)

    def send(self, text):
        self.sent.append(json.loads(text))

    def close(self):
        self.closed = True


def env(kind, payload, eid="e1"):
    return json.dumps({"type": kind, "envelope_id": eid, "payload": payload})


CLICK = {"type": "block_actions", "user": {"id": "U1"}, "channel": {"id": "C1"},
         "message": {"ts": "5.5"}, "actions": [{"value": "42:yes"}]}
REPLY = {"type": "event_callback", "event": {"type": "message", "user": "U1", "channel": "C1",
                                             "thread_ts": "5.5", "text": "yes"}}


class Listen(unittest.TestCase):
    def listener(self, sockets, handle=None, clock=None):
        self.handled, self.sleeps = [], []
        stop = threading.Event()
        pending = list(sockets)
        done = threading.Event()

        def connect(url):
            if not pending:
                stop.set()
                raise slack_listen.Closed("no more")
            return pending.pop(0)

        def fake_handle(payload):
            self.handled.append(payload)
            done.set()
            return True

        def sleep(delay):
            self.sleeps.append(delay)

        self.stop = stop
        return slack_listen._Listener(get_url=lambda: "wss://fake/x", connect=connect,
                                     handle=handle or fake_handle, owner="U1", stop=stop,
                                     sleep=sleep, **({"clock": clock} if clock else {}))

    def test_every_envelope_is_acked_before_it_is_handled(self):
        order = []
        ws = FakeWS([json.dumps({"type": "hello"}), env("interactive", CLICK, "a1"),
                     env("events_api", REPLY, "a2"), env("slash_commands", {}, "a3")])
        orig = ws.send
        ws.send = lambda t: (order.append("ack"), orig(t))

        def handle(payload):
            order.append("handle")
            return True

        lst = self.listener([ws], handle)
        lst.run()
        self.assertEqual([m["envelope_id"] for m in ws.sent], ["a1", "a2", "a3"])
        self.assertLess(order.index("ack"), order.index("handle"))
        self.assertTrue(ws.closed)

    def test_only_clicks_and_user_messages_reach_handle(self):
        bot = json.loads(env("events_api", REPLY))
        bot["payload"]["event"]["bot_id"] = "B1"
        top = json.loads(env("events_api", REPLY))
        del top["payload"]["event"]["thread_ts"]
        other = {"type": "view_submission"}
        sub = json.loads(env("events_api", REPLY))
        sub["payload"]["event"]["subtype"] = "message_changed"
        ws = FakeWS([env("interactive", other), json.dumps(bot), json.dumps(sub),
                     json.dumps(top), env("interactive", CLICK)])
        self.listener([ws]).run()
        # a top-level user message is forwarded too (brainstorm intake): slack_answer decides
        self.assertEqual(self.handled, [top["payload"], CLICK])

    def test_a_strangers_message_spawns_no_child_but_a_click_still_does(self):
        stranger = json.loads(env("events_api", REPLY))
        stranger["payload"]["event"]["user"] = "U2STRANGER"
        broadcast = json.loads(env("events_api", REPLY))
        broadcast["payload"]["event"]["subtype"] = "thread_broadcast"
        ws = FakeWS([json.dumps(stranger), json.dumps(broadcast), env("interactive", CLICK)])
        self.listener([ws]).run()
        self.assertEqual(self.handled, [broadcast["payload"], CLICK])
        none = self.listener([FakeWS([json.dumps(json.loads(env("events_api", REPLY)))])])
        none.owner = None                      # no owner known: no message reaches a child
        none.run()
        self.assertEqual(self.handled, [])

    def test_backoff_doubles_and_resets_only_after_a_connection_stayed_up(self):
        now = [0]

        def tick():
            return now[0]

        class Long(FakeWS):                    # a hello, then a minute and a half up
            def recv(self):
                if self.messages:
                    return self.messages.pop(0)
                now[0] += 100
                raise slack_listen.Closed("eof")

        bad, brief = FakeWS([]), FakeWS([json.dumps({"type": "hello"})])
        long_ = Long([json.dumps({"type": "hello"})])
        self.listener([bad, bad, brief, long_], clock=tick).run()
        self.assertEqual(self.sleeps, [2, 4, 8, 1])

    def test_silence_is_pinged_then_declared_dead(self):
        now = [0]

        def tick():
            now[0] += 40
            return now[0]

        ws = FakeWS([None, None, None, None])
        self.listener([ws], clock=tick).run()
        self.assertGreaterEqual(ws.pings, 1)
        self.assertTrue(ws.closed)
        self.assertTrue(self.sleeps)               # the dead connection was dropped and redialled

    def test_deeply_nested_json_is_dropped_without_crashing_the_listener(self):
        ws = FakeWS(["[" * 200000, env("interactive", CLICK)])
        self.listener([ws]).run()
        self.assertEqual(self.handled, [CLICK])

    def test_a_full_queue_drops_after_the_ack(self):
        lst = self.listener([])
        for _ in range(slack_listen.QUEUE_MAX):
            lst.work.put({})
        ws = FakeWS([])
        self.assertFalse(lst._envelope(ws, env("interactive", CLICK, "q1")))
        self.assertEqual([m["envelope_id"] for m in ws.sent], ["q1"])
        self.assertEqual(lst.work.qsize(), slack_listen.QUEUE_MAX)

    def test_only_a_short_string_envelope_id_is_acked(self):
        lst = self.listener([])
        ws = FakeWS([])
        for eid in (123, ["x"], {"a": 1}, "x" * 101):
            self.assertFalse(lst._envelope(ws, env("interactive", CLICK, eid)))
        self.assertEqual(ws.sent, [])                       # skipped, no crash
        self.assertFalse(lst._envelope(ws, env("interactive", CLICK, "y" * 100)))
        self.assertEqual(ws.sent, [{"envelope_id": "y" * 100}])

    def test_a_disconnect_envelope_reconnects(self):
        first = FakeWS([json.dumps({"type": "hello"}),
                        json.dumps({"type": "disconnect", "reason": "refresh_requested"})])
        second = FakeWS([json.dumps({"type": "hello"})])
        self.listener([first, second]).run()
        self.assertTrue(first.closed and second.closed)

    def test_a_failing_handler_does_not_stop_the_listener(self):
        def boom(payload):
            raise RuntimeError("xapp-secret-token-1234")
        ws = FakeWS([env("interactive", CLICK), env("interactive", CLICK, "e2")])
        self.listener([ws], boom).run()
        self.assertEqual([m["envelope_id"] for m in ws.sent], ["e1", "e2"])

    def test_stop_ends_the_loop_cleanly(self):
        ws = FakeWS([json.dumps({"type": "hello"})])
        lst = self.listener([ws])
        lst.stop.set()
        lst.run()
        self.assertEqual(ws.sent, [])

    def test_log_masks_tokens(self):
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            slack_listen.log("bad xapp-1-ABC-123 and xoxb-999-abc")
        self.assertNotIn("xapp-1", buf.getvalue())
        self.assertNotIn("xoxb-999", buf.getvalue())

class OpenUrl(unittest.TestCase):
    def test_returns_the_wss_url_and_refuses_errors(self):
        http = FakeHTTP()
        http.script += [{"ok": True, "url": "wss://wss-primary.slack.com/link"},
                        {"ok": False, "error": "invalid_auth"}]
        url = "wss://wss-primary.slack.com/link"
        self.assertEqual(slack_listen._open_url("xapp-1-fake", http), url)
        with self.assertRaises(slack.SendError):
            slack_listen._open_url("xapp-1-fake", http)
        self.assertEqual(http.calls[0]["url"], slack_listen.OPEN_URL)

    def test_a_response_that_is_not_a_document_is_a_send_error_not_a_crash(self):
        http = FakeHTTP()
        http.script += [["not", "a", "dict"], "text"]
        for _ in range(2):
            with self.assertRaises(slack.SendError):
                slack_listen._open_url("xapp-1-fake", http)

    def test_the_app_token_is_read_through_capsule_secret_and_shape_checked(self):
        d = Path(tempfile.mkdtemp())
        fake = d / "secret"
        fake.write_text("#!/bin/sh\necho \"$3\" >&2\necho xoxb-not-an-app-token\n")
        fake.chmod(0o755)
        with self.assertRaises(slack.SendError):
            slack_secret.secret_token("who", secret_bin=str(fake), name=slack_listen.APP_TOKEN_NAME,
                                      pattern=slack_listen.APP_TOKEN_RE)
        fake.write_text("#!/bin/sh\n[ \"$3\" = SLACK_APP_TOKEN ] && echo xapp-1-ABC-123\n")
        self.assertEqual(slack_secret.secret_token("who", secret_bin=str(fake),
                                                   name=slack_listen.APP_TOKEN_NAME,
                                                   pattern=slack_listen.APP_TOKEN_RE),
                         "xapp-1-ABC-123")


def done(out="", code=0):
    return subprocess.CompletedProcess([], code, stdout=out)


class Child(unittest.TestCase):
    """`_child` runs `python -m capsule.slack_answer <cmd>`; it never raises."""

    def child(self, **kw):
        with mock.patch.object(slack_listen.subprocess, "run", **kw) as run, \
                mock.patch.object(slack_listen, "log") as log:
            doc = slack_listen._child("event", {"a": 1})
        return doc, run, [c.args[0] for c in log.call_args_list]

    def test_the_last_stdout_line_is_the_answer_and_the_payload_goes_on_stdin(self):
        doc, run, _ = self.child(return_value=done('noise\n{"ok": true}\n'))
        self.assertEqual(doc, {"ok": True})
        argv = run.call_args.args[0]
        self.assertEqual(argv[1:], ["-m", "capsule.slack_answer", "event"])
        self.assertEqual(run.call_args.kwargs["input"], '{"a": 1}')
        self.assertEqual(run.call_args.kwargs["cwd"], str(ROOT))

    def test_a_timeout_a_nonzero_exit_and_bad_output_log_and_give_none(self):
        cases = ((dict(side_effect=subprocess.TimeoutExpired("x", 1)), "timeout"),
                 (dict(return_value=done('{"ok": true}', 3)), "exit 3"),
                 (dict(return_value=done("not json")), "bad output"),
                 (dict(return_value=done("[1]")), "bad output"),
                 (dict(return_value=done("")), "bad output"))
        for kw, why in cases:
            with self.subTest(why):
                doc, _, lines = self.child(**kw)
                self.assertIsNone(doc)
                self.assertEqual(lines, [f"answer failed: {why}"])

    def test_the_childs_stderr_is_masked_before_it_reaches_ours(self):
        out = subprocess.CompletedProcess([], 0, stdout="{}", stderr="boom xapp-1-ABC-123 end\n")
        with mock.patch.object(slack_listen.subprocess, "run", return_value=out), \
                mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            slack_listen._child("event", {})
        self.assertNotIn("xapp-1", err.getvalue())
        self.assertIn("boom ***", err.getvalue())

    def test_start_has_no_stdin_payload(self):
        with mock.patch.object(slack_listen.subprocess, "run", return_value=done("{}")) as run:
            slack_listen._child("start", timeout=5)
        self.assertEqual(run.call_args.kwargs["stdin"], subprocess.DEVNULL)
        self.assertIsNone(run.call_args.kwargs["input"])


class Main(unittest.TestCase):
    """main() starts the answer child first and connects only when it says to."""

    def run_main(self, start, no_send=False):
        env_ = {"AGENT_OPS_NO_SEND": "1"} if no_send else {}
        with mock.patch.dict(os.environ, env_), \
                mock.patch.object(slack_listen, "_child", return_value=start) as child, \
                mock.patch.object(slack_listen, "_Listener") as lst, \
                mock.patch.object(slack_listen, "log") as log:
            if not no_send:
                os.environ.pop("AGENT_OPS_NO_SEND", None)
            return (slack_listen.main(), child, lst, [c.args[0] for c in log.call_args_list])

    def test_no_send_runs_only_the_check_and_never_connects(self):
        rc, child, lst, lines = self.run_main({"identity": "who", "repaired": 0}, no_send=True)
        self.assertEqual(rc, 0)
        self.assertEqual(child.call_args.args, ("check",))
        lst.assert_not_called()
        self.assertEqual(lines, ["AGENT_OPS_NO_SEND set; not connecting"])

    def test_no_send_with_slack_off_logs_the_reason_and_exits_zero(self):
        rc, child, lst, lines = self.run_main({"off": "slack is off"}, no_send=True)
        self.assertEqual((rc, lines), (0, ["slack is off"]))
        lst.assert_not_called()

    def test_off_logs_the_reason_and_exits_zero(self):
        rc, _, lst, lines = self.run_main({"off": "slack is off"})
        self.assertEqual((rc, lines), (0, ["slack is off"]))
        lst.assert_not_called()

    def test_an_unusable_config_exits_one_so_launchd_retries(self):
        rc, _, lst, lines = self.run_main({"error": "no usable slack config: OSError"})
        self.assertEqual((rc, lines), (1, ["no usable slack config: OSError"]))
        lst.assert_not_called()

    def test_a_failed_start_exits_one(self):
        for start in (None, {}, {"identity": ""}, {"identity": 7}, {"identity": ["x"]}):
            with self.subTest(start=start):
                rc, _, lst, _ = self.run_main(start)
                self.assertEqual(rc, 1)
                lst.assert_not_called()

    def test_a_good_start_logs_the_repair_count_and_runs_the_listener(self):
        rc, child, lst, lines = self.run_main({"identity": "who", "repaired": 2})
        self.assertEqual(rc, 0)
        self.assertIn("repaired 2 answered ask(s) at start", lines)
        lst.return_value.run.assert_called_once()
        self.assertEqual(child.call_args.args, ("start",))


class Template(unittest.TestCase):
    def test_plist_is_keepalive_throttled_and_listed_unloaded(self):
        plist = (ROOT / "launchd/templates/__PREFIX__.capsule-corp-slack-listen.plist").read_text()
        self.assertIn("<key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>", plist)
        self.assertRegex(plist, r"ThrottleInterval</key><integer>\d+")
        self.assertIn("capsule.slack_listen", plist)
        self.assertNotIn("xapp", plist)
        if PRIVATE_TREE:
            unloaded = (ROOT / "launchd/unloaded").read_text()
            self.assertTrue(re.search(r"^__PREFIX__\.capsule-corp-slack-listen$", unloaded, re.M))


if __name__ == "__main__":
    unittest.main()
