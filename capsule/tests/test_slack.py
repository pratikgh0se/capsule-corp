"""The Slack channel (rows 398, 424, 425): class templates, routing, the sender over a fake
HTTP, and the outbox's retry/backoff. No test touches the network or Infisical."""
import email.message
import io
import json
import os
import stat
import tempfile
import unittest
from unittest import mock
import urllib.error
from pathlib import Path

from capsule import blocks, config, db, lease, outbox, paths, slack

# `needs_input` is deliberately absent here: the minimum valid config is the six required
# channels, and `route`/`validate` must fall back to `blocked`'s channel for it (row 425 (1)).
CHANNELS = {"blocked": "#capsule-needs-you", "paused": "#capsule-needs-you", "incident": "#capsule-alerts",
            "done": "#cc-my-app", "scheduled": "#cc-my-app", "running": "#capsule-activity"}
CHANNELS_WITH_NEEDS_INPUT = dict(CHANNELS, needs_input="#capsule-needs-you-input")
SLACK = {"channels": CHANNELS, "identity": "slack"}
T0 = 1_790_000_000


def board(name="my-app"):
    d = Path(tempfile.mkdtemp()) / name
    db.create_board(d / "kanban.db")
    return db.connect(d / "kanban.db")


def rows(conn):
    return conn.execute("SELECT * FROM outbox ORDER BY id").fetchall()


# -- templates ---------------------------------------------------------------------------

GOLDEN = {
    "blocked": (
        ("t_ab12 blocked (needs_input)", [("reason", "golden rows   stay\noff?")],
         "python3 -m capsule human my-app t_ab12 unblock|reset|accept '<answer>'"),
        "\U0001F534 t_ab12 blocked (needs_input)\n"
        "reason: golden rows stay off?\n"
        "reply: `python3 -m capsule human my-app t_ab12 unblock|reset|accept '<answer>'`"),
    "done": (
        ("Merged PR #12 my-app screens",
         [("epic", "epic/my-app-screens-epic"), ("sha", "3bef834")], None),
        "✅ Merged PR #12 my-app screens\n"
        "epic: epic/my-app-screens-epic\n"
        "sha: 3bef834"),
    "scheduled": (
        ("Brief 08:30: 3 merged, 1 waits on you", [("waiting", "t_ab12 break length")],
         "python3 -m capsule human my-app t_ab12 answer 'a'"),
        "\U0001F5D3 Brief 08:30: 3 merged, 1 waits on you\n"
        "waiting: t_ab12 break length\n"
        "reply: `python3 -m capsule human my-app t_ab12 answer 'a'`"),
    "running": (
        ("t_cd34 implementer started", [("model", "claude-sonnet-5"), ("tier", "strong"), ("budget", "80 turns")],
         None),
        "⚙️ t_cd34 implementer started\n"
        "model: claude-sonnet-5\n"
        "tier: strong\n"
        "budget: 80 turns"),
    "incident": (
        ("t_ef56 tick error: OSError", [("error", "disk full")], None),
        "\U0001F6A8 t_ef56 tick error: OSError\n"
        "error: disk full"),
    "paused": (
        ("my-app paused: rate limit on default", [("until", "2026-09-26 18:00")],
         "python3 -m capsule human my-app t_ab12 reset 'go'"),
        "⏸ my-app paused: rate limit on default\n"
        "until: 2026-09-26 18:00\n"
        "reply: `python3 -m capsule human my-app t_ab12 reset 'go'`"),
    "needs_input": (
        ("t_gh78 triage (review_infra): review gave no valid document twice",
         [("error", "guide run ab12cd34: no valid document")],
         "python3 -m capsule human my-app t_gh78 accept|reset '<answer>'"),
        "\U0001F64B t_gh78 triage (review_infra): review gave no valid document twice\n"
        "error: guide run ab12cd34: no valid document\n"
        "reply: `python3 -m capsule human my-app t_gh78 accept|reset '<answer>'`"),
    "ask": (
        ("Retry limit: count per card?", [("project", "Capsule Corp")], None),
        "\U0001F4EC Retry limit: count per card?\n"
        "project: Capsule Corp"),
    "page": (
        ("Factory stopped for 45 minutes", [("waiting", "3")], None),
        "\U0001F6D1 Factory stopped for 45 minutes\n"
        "waiting: 3"),
}


class Templates(unittest.TestCase):
    def test_golden_strings_for_every_class(self):
        self.assertEqual(set(GOLDEN), set(slack.CLASSES))
        for cls, ((headline, fields, reply), want) in GOLDEN.items():
            with self.subTest(cls=cls):
                self.assertEqual(slack.render(cls, headline, fields, reply), want)

    def test_one_glyph_per_message_as_its_first_character(self):
        glyphs = list(slack.CLASSES.values())
        for cls, (args, _) in GOLDEN.items():
            text = slack.render(cls, *args)
            self.assertTrue(text.startswith(slack.CLASSES[cls] + " "))
            self.assertEqual(sum(text.count(g) for g in glyphs), 1, cls)

    def test_at_most_three_fields_and_known_classes(self):
        with self.assertRaisesRegex(ValueError, "at most 3"):
            slack.render("done", "x", [("a", 1), ("b", 2), ("c", 3), ("d", 4)])
        with self.assertRaisesRegex(ValueError, "unknown message class"):
            slack.render("urgent", "x")
        with self.assertRaisesRegex(ValueError, "empty headline"):
            slack.render("done", "  \n ")

    def test_headline_is_one_line_and_capped(self):
        text = slack.render("running", "a\nb " + "x" * 400)
        self.assertEqual(len(text.splitlines()), 1)
        self.assertLessEqual(len(text), slack.HEADLINE_MAX + 3)
        self.assertTrue(text.endswith("…"))

    def test_a_backtick_cannot_break_the_reply_code_span(self):
        self.assertTrue(slack.render("blocked", "x", reply="echo `id`").endswith("reply: `echo 'id'`"))

    def test_one_line_flattening_for_the_file_channel(self):
        self.assertEqual(slack.one_line(slack.render(*("incident",), *GOLDEN["incident"][0])),
                         "\U0001F6A8 t_ef56 tick error: OSError | error: disk full")


class Routing(unittest.TestCase):
    def test_each_class_routes_to_its_channel(self):
        # `needs_input` is absent from CHANNELS, so it falls back to `blocked`'s channel here
        # (row 425 (1)): the same fallback `route` itself implements.
        for cls, (args, _) in GOLDEN.items():
            want = CHANNELS.get(cls, CHANNELS["blocked"])
            self.assertEqual(slack.route(SLACK, slack.render(cls, *args)), want)

    def test_needs_input_uses_its_own_channel_when_configured(self):
        slack_full = {"channels": CHANNELS_WITH_NEEDS_INPUT, "identity": "slack"}
        text = slack.render("needs_input", *GOLDEN["needs_input"][0])
        self.assertEqual(slack.route(slack_full, text), "#capsule-needs-you-input")
        self.assertEqual(slack.route(SLACK, text), "#capsule-needs-you")   # no dedicated channel: falls back

    def test_needs_input_falls_back_on_a_null_or_empty_channel_too_not_just_a_missing_key(self):
        """The fallback is on falsiness, not key presence: a `needs_input` channel that is present
        but `None` or an empty string still routes to `blocked`'s channel."""
        text = slack.render("needs_input", *GOLDEN["needs_input"][0])
        for empty in (None, ""):
            cfg = {"channels": dict(CHANNELS, needs_input=empty), "identity": "slack"}
            self.assertEqual(slack.route(cfg, text), CHANNELS["blocked"])

    def test_an_unglyphed_line_is_activity_never_a_push(self):
        self.assertEqual(slack.classify("capsule: something"), "running")
        self.assertEqual(slack.route(SLACK, "capsule: something"), "#capsule-activity")

    def test_config_requires_all_classes_except_needs_input(self):
        cfg = config.load(paths.REPO / "capsule" / "config.example.json")
        config.validate(dict(cfg, slack=SLACK))                              # needs_input absent: still valid
        config.validate(dict(cfg, slack=dict(SLACK, channels=CHANNELS_WITH_NEEDS_INPUT)))  # present: also valid
        for cls in CHANNELS:
            broken = dict(CHANNELS); broken.pop(cls)
            with self.assertRaisesRegex(ValueError, f"slack.channels.{cls}"):
                config.validate(dict(cfg, slack=dict(SLACK, channels=broken)))
        with self.assertRaisesRegex(ValueError, "slack.channels.needs_input"):
            config.validate(dict(cfg, slack=dict(SLACK, channels=dict(CHANNELS, needs_input="  "))))
        with self.assertRaisesRegex(ValueError, "unknown class"):
            config.validate(dict(cfg, slack=dict(SLACK, channels=dict(CHANNELS, urgent="#x"))))
        with self.assertRaisesRegex(ValueError, "slack.identity"):
            config.validate(dict(cfg, slack={"channels": CHANNELS}))
        with self.assertRaisesRegex(ValueError, "slack.enabled"):
            config.validate(dict(cfg, slack=dict(SLACK, enabled="yes")))

    def test_block_is_off_when_absent_or_disabled(self):
        self.assertIsNone(slack.block({}))
        self.assertIsNone(slack.block({"slack": dict(SLACK, enabled=False)}))
        self.assertEqual(slack.block({"slack": SLACK}), SLACK)


# -- the sender over a fake HTTP -------------------------------------------------------------

class FakeResponse(io.BytesIO):
    def __init__(self, doc, headers=None):
        super().__init__(json.dumps(doc).encode())
        self.headers = headers or {}


def http_error(code, headers=None):
    msg = email.message.Message()
    for k, v in (headers or {}).items():
        msg[k] = v
    return urllib.error.HTTPError(slack.API_URL, code, "err", msg, io.BytesIO(b""))


class FakeHTTP:
    """Scripted urlopen: each entry is a doc (-> 200), or an exception to raise."""

    def __init__(self, *script):
        self.script, self.calls = list(script), []

    def __call__(self, req, timeout=None):
        self.calls.append({"url": req.full_url, "timeout": timeout, "auth": req.get_header("Authorization"),
                           "body": json.loads(req.data.decode())})
        step = self.script.pop(0)
        if isinstance(step, BaseException):
            raise step
        return step if isinstance(step, FakeResponse) else FakeResponse(step)


class Tokens:
    def __init__(self, *values):
        self.values, self.calls = list(values), 0

    def __call__(self):
        self.calls += 1
        return self.values.pop(0)


class Sender(unittest.TestCase):
    ROW = {"body": GOLDEN["blocked"][1], "channel": "slack"}

    def test_posts_the_routed_escaped_text_with_the_bot_token_and_a_timeout(self):
        http = FakeHTTP({"ok": True, "ts": "1.2"})
        send = slack.Sender(SLACK, token=Tokens("xoxb-fake"), urlopen=http, timeout=7)
        self.assertEqual(send(self.ROW), "1.2")
        call = http.calls[0]
        self.assertEqual(call["url"], "https://slack.com/api/chat.postMessage")
        self.assertEqual(call["timeout"], 7)
        self.assertEqual(call["auth"], "Bearer xoxb-fake")
        self.assertEqual(call["body"]["channel"], "#capsule-needs-you")
        self.assertIn("'&lt;answer&gt;'", call["body"]["text"])
        self.assertNotIn("<answer>", call["body"]["text"])
        self.assertFalse(call["body"]["unfurl_links"])

    def test_the_token_is_fetched_once_and_reread_once_after_invalid_auth(self):
        tokens = Tokens("xoxb-old", "xoxb-new")
        http = FakeHTTP({"ok": True}, {"ok": False, "error": "invalid_auth"}, {"ok": True})
        send = slack.Sender(SLACK, token=tokens, urlopen=http)
        send(self.ROW)
        send(self.ROW)                                  # invalid_auth -> re-read -> delivered
        self.assertEqual(tokens.calls, 2)
        self.assertEqual([c["auth"] for c in http.calls], ["Bearer xoxb-old", "Bearer xoxb-old", "Bearer xoxb-new"])

    def test_invalid_auth_with_a_fresh_token_is_a_rejection(self):
        tokens = Tokens("xoxb-a", "xoxb-b")
        http = FakeHTTP({"ok": True}, {"ok": False, "error": "invalid_auth"}, http_error(401))
        send = slack.Sender(SLACK, token=tokens, urlopen=http)
        send(self.ROW)
        with self.assertRaisesRegex(slack.Rejected, "token re-read"):
            send(self.ROW)
        self.assertEqual(len(http.calls), 3)

    def test_permanent_errors_are_rejections_transient_ones_are_not(self):
        for err in ("channel_not_found", "not_in_channel", "msg_too_long", "is_archived", "invalid_blocks"):
            send = slack.Sender(SLACK, token=Tokens("xoxb-t"), urlopen=FakeHTTP({"ok": False, "error": err}))
            with self.assertRaises(slack.Rejected):
                send(self.ROW)
        send = slack.Sender(SLACK, token=Tokens("xoxb-t"), urlopen=FakeHTTP({"ok": False, "error": "internal_error"}))
        with self.assertRaises(slack.SendError) as cm:
            send(self.ROW)
        self.assertNotIsInstance(cm.exception, slack.Rejected)

    def test_a_malformed_token_is_refused_before_any_request_and_never_quoted(self):
        for bad in ("xoxb-x\nnotice: update available", "xoxp-user", " xoxb-", "Bearer xoxb-1", "xoxb-a b"):
            http = FakeHTTP()
            send = slack.Sender(SLACK, token=Tokens(bad), urlopen=http)
            with self.assertRaises(slack.SendError) as cm:
                send(self.ROW)
            self.assertEqual(str(cm.exception), "slack token unavailable: malformed")
            self.assertEqual(http.calls, [])

    def test_a_malformed_token_never_reaches_last_error(self):
        conn = board()
        outbox.add(conn, "slack", GOLDEN["blocked"][1], now=T0)
        send = slack.Sender(SLACK, token=Tokens("xoxb-x\nnotice"), urlopen=FakeHTTP())
        outbox.flush(conn, send, now=T0)
        self.assertEqual(rows(conn)[0]["last_error"], "slack token unavailable: malformed")

    def test_a_request_error_from_http_client_is_named_not_quoted(self):
        import http.client
        for exc in (ValueError("Invalid header value b'Bearer xoxb-secret'"), http.client.InvalidURL("xoxb-secret")):
            send = slack.Sender(SLACK, token=Tokens("xoxb-secret"), urlopen=FakeHTTP(exc))
            with self.assertRaises(slack.SendError) as cm:
                send(self.ROW)
            self.assertNotIn("xoxb-secret", str(cm.exception))
            self.assertTrue(str(cm.exception).startswith("slack request failed: "))


    def test_the_default_opener_refuses_redirects(self):
        import http.server, threading
        hits = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                hits.append((self.path, self.headers.get("Authorization")))
                self.send_response(302)
                self.send_header("Location", "/elsewhere")
                self.end_headers()
            do_GET = do_POST

            def log_message(self, *a):
                pass

        srv = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            send = slack.Sender(SLACK, token=Tokens("xoxb-t"), api_url=f"http://127.0.0.1:{srv.server_port}/api")
            with self.assertRaisesRegex(slack.SendError, "slack http 302"):
                send(self.ROW)
            self.assertEqual([h[0] for h in hits], ["/api"])      # never followed to /elsewhere
        finally:
            srv.shutdown(); srv.server_close()

    def test_429_raises_deferred_with_retry_after(self):
        send = slack.Sender(SLACK, token=Tokens("xoxb-t"), urlopen=FakeHTTP(http_error(429, {"Retry-After": "42"})))
        with self.assertRaises(slack.Deferred) as cm:
            send(self.ROW)
        self.assertEqual(cm.exception.retry_after, 42)

    def test_ratelimited_body_and_a_missing_header_default(self):
        send = slack.Sender(SLACK, token=Tokens("xoxb-t"), urlopen=FakeHTTP({"ok": False, "error": "ratelimited"}))
        with self.assertRaises(slack.Deferred) as cm:
            send(self.ROW)
        self.assertEqual(cm.exception.retry_after, slack.DEFAULT_RETRY_AFTER)
        self.assertEqual(slack.retry_after({"Retry-After": "999999"}), slack.MAX_RETRY_AFTER)

    def test_network_and_server_errors_are_send_errors_without_the_token(self):
        for exc, want in ((urllib.error.URLError("timed out"), "unreachable"),
                          (TimeoutError(), "unreachable"), (http_error(500), "http 500")):
            send = slack.Sender(SLACK, token=Tokens("xoxb-secret"), urlopen=FakeHTTP(exc))
            with self.assertRaisesRegex(slack.SendError, want) as cm:
                send(self.ROW)
            self.assertNotIn("xoxb-secret", str(cm.exception))

    def test_secret_token_calls_capsule_secret_and_drops_its_stderr(self):
        d = Path(tempfile.mkdtemp())
        fake = d / "capsule-secret"
        fake.write_text('#!/bin/sh\necho "leak-$CAPSULE_IDENTITY" >&2\n'
                        '[ "$1 $2 $3" = "get capsule-corp SLACK_BOT_TOKEN" ] || exit 2\n'
                        'echo "xoxb-$CAPSULE_IDENTITY"\n')
        fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
        old = os.environ.pop("CAPSULE_IDENTITY", None)
        try:
            self.assertEqual(slack.secret_token("slack", secret_bin=str(fake)), "xoxb-slack")
            os.environ["CAPSULE_IDENTITY"] = "bridge"           # an inherited identity never wins
            self.assertEqual(slack.secret_token("slack", secret_bin=str(fake)), "xoxb-slack")
            os.environ.pop("CAPSULE_IDENTITY")
            fake.write_text('#!/bin/sh\necho "notice: new version"\necho "xoxb-1"\n')
            with self.assertRaises(slack.SendError) as cm:
                slack.secret_token("slack", secret_bin=str(fake))
            self.assertEqual(str(cm.exception), "slack token unavailable: malformed")
            fake.write_text('#!/bin/sh\necho "capsule-secret: login failed secret-detail" >&2\nexit 3\n')
            with self.assertRaises(slack.SendError) as cm:
                slack.secret_token("slack", secret_bin=str(fake))
            self.assertEqual(str(cm.exception), "slack token unavailable: capsule-secret exit 3")
        finally:
            os.environ.pop("CAPSULE_IDENTITY", None)
            if old is not None:
                os.environ["CAPSULE_IDENTITY"] = old


class SenderAllowlist(unittest.TestCase):
    ROW = {"body": GOLDEN["blocked"][1], "channel": "slack"}

    def sender(self, cfg, *script, **kw):
        self.log = Path(tempfile.mkdtemp()) / "slack-debug.log"
        self.http = FakeHTTP(*script)
        return slack.Sender(cfg, token=Tokens("xoxb-fake"), urlopen=self.http, debug_log=self.log,
                            **kw)

    def test_an_allowlisted_channel_is_sent_and_logged_without_secrets(self):
        cfg = dict(SLACK, allowed_channels=list(CHANNELS.values()))
        send = self.sender(cfg, {"ok": True, "ts": "1"})
        self.assertEqual(send(self.ROW), "1")
        text = self.log.read_text()
        self.assertIn("sent\t#capsule-needs-you", text)
        self.assertNotIn("xoxb", text)
        self.assertNotIn("golden", text)

    def test_a_channel_off_the_allowlist_is_refused_before_any_http_and_logged(self):
        cfg = dict(SLACK, allowed_channels=list(CHANNELS.values()))
        send = self.sender(cfg, {"ok": True, "ts": "1"})
        stray = dict(self.ROW, thread={"channel": "#elsewhere", "ts": "9.9"})
        with self.assertRaisesRegex(slack.Rejected, "not allowlisted"):
            send(stray)
        self.assertEqual(self.http.calls, [])
        self.assertIn("refused\t#elsewhere", self.log.read_text())

    def test_a_missing_allowlist_defaults_to_the_configured_channels_only(self):
        send = self.sender(SLACK, {"ok": True, "ts": "1"})
        self.assertEqual(send(self.ROW), "1")                       # a configured channel works
        stray = dict(self.ROW, thread={"channel": "#not-configured", "ts": "9.9"})
        with self.assertRaises(slack.Rejected):
            send(stray)
        self.assertEqual(len(self.http.calls), 1)
        self.assertIn("refused\t#not-configured", self.log.read_text())

    def test_a_bad_allowlist_value_is_a_config_error(self):
        for bad in ("#a", [1], [""]):
            with self.assertRaises(ValueError):
                slack.validate(dict(SLACK, allowed_channels=bad))

    def test_an_allowlist_that_leaves_out_a_configured_channel_is_a_config_error(self):
        for short in ([], ["#capsule-needs-you"]):
            with self.assertRaisesRegex(ValueError, "allowed_channels"):
                slack.validate(dict(SLACK, allowed_channels=short))

    def test_secret_forms_are_masked_in_the_log_and_the_refusal(self):
        cfg = dict(SLACK, allowed_channels=list(CHANNELS.values()))
        for secret in ("xoxp-123-abc", "xapp-1-A0-secret", "xoxe-1-abc", "xoxc-9-z",
                       "https://hooks.slack.com/services/T/B/x", "hooks.slack.com/services/T/B/x"):
            send = self.sender(cfg)
            with self.assertRaises(slack.Rejected) as cm:
                send(dict(self.ROW, thread={"channel": secret, "ts": "1"}))
            self.assertNotIn(secret, str(cm.exception))
            self.assertNotIn(secret, self.log.read_text())

    def test_a_log_write_failure_never_fails_the_refusal(self):
        send = self.sender(SLACK)
        with self.assertRaisesRegex(slack.Rejected, "not allowlisted"):
            send(dict(self.ROW, thread={"channel": "#bad\ud800", "ts": "1"}))

    def test_a_failed_send_is_logged_with_its_secret_masked(self):
        err = urllib.error.URLError("boom xoxp-9-secret")
        send = self.sender(SLACK, err)
        with self.assertRaises(slack.SendError):
            send(self.ROW)
        text = self.log.read_text()
        self.assertIn("failed\t#capsule-needs-you", text)
        self.assertNotIn("xoxp-9-secret", text)
        self.assertIn("***", text)

    def test_a_slack_refusal_is_logged(self):
        send = self.sender(SLACK, {"ok": False, "error": "not_in_channel"})
        with self.assertRaises(slack.Rejected):
            send(self.ROW)
        self.assertRegex(self.log.read_text(), r"rejected\t#capsule-needs-you\t.*not_in_channel")

    def test_a_refused_edit_falls_back_to_a_checked_post(self):
        send = self.sender(SLACK, {"ok": True, "ts": "2"})
        row = dict(self.ROW, edit={"channel": "#elsewhere", "ts": "1"})
        self.assertEqual(send(row), "2")
        self.assertEqual(len(self.http.calls), 1)
        self.assertEqual(self.http.calls[0]["body"]["channel"], "#capsule-needs-you")
        self.assertIn("refused\t#elsewhere", self.log.read_text())

    def test_the_log_is_private_and_rotates(self):
        send = self.sender(SLACK, {"ok": True, "ts": "1"})
        self.log.parent.mkdir(exist_ok=True)
        self.log.write_text("x" * (slack.DEBUG_LOG_MAX + 1))
        send(self.ROW)
        self.assertTrue(self.log.with_name("slack-debug.log.1").exists())
        self.assertEqual(stat.S_IMODE(self.log.stat().st_mode), 0o600)
        self.assertLess(self.log.stat().st_size, 1000)

    def test_the_router_passes_its_home_to_the_sender_log(self):
        home = Path(tempfile.mkdtemp())
        router = outbox.Router(home=home).configure({"board": "b", "slack": SLACK})
        router._slack._urlopen = FakeHTTP({"ok": True, "ts": "1"})
        router._slack._token = lambda: "xoxb-fake"
        router(dict(self.ROW))
        self.assertIn("sent", (home / "slack-debug.log").read_text())


# -- the outbox: notify, retry, backoff --------------------------------------------------------

class Script:
    """A fake `send` for flush: each call pops the next outcome (None = delivered)."""

    def __init__(self, *outcomes):
        self.outcomes, self.calls = list(outcomes), []

    def __call__(self, row):
        self.calls.append((row["channel"], row["body"]))
        step = self.outcomes.pop(0) if self.outcomes else None
        if step is not None:
            raise step


class Notify(unittest.TestCase):
    def test_slack_off_writes_only_the_file_row(self):
        conn = board()
        outbox.notify(conn, "done", "Merged PR #12", fields=[("sha", "abc")], thread_key="t_1", now=T0)
        self.assertEqual([(r["channel"], r["body"]) for r in rows(conn)],
                         [("file", "✅ Merged PR #12 | sha: abc")])

    def test_slack_on_adds_the_template_row_beside_the_file_row(self):
        conn = board()
        outbox.set_slack(conn, True, now=T0)
        outbox.notify(conn, "incident", "t_1 tick error: X", fields=[("error", "e")], thread_key="t_1",
                      file_body="capsule: tick error on t_1: X: e", now=T0)
        got = [(r["channel"], r["thread_key"], r["body"]) for r in rows(conn)]
        self.assertEqual(got, [("file", "t_1", "capsule: tick error on t_1: X: e"),
                               ("slack", "t_1", "\U0001F6A8 t_1 tick error: X\nerror: e")])

    def test_a_needs_input_block_queues_the_needs_input_glyph_not_blocked(self):
        """rows 424, 425: a needs_input hold reaches Slack distinctly from a generic 🔴 blocked
        line, so it can be routed and triaged separately (#capsule-needs-you here too, since the
        board's config has no dedicated `needs_input` channel and falls back to `blocked`'s)."""
        conn = board("my-app")
        outbox.set_slack(conn, True, now=T0)
        tid = db.new_task(conn, title="t")
        lease.claim(conn, tid, ttl=900, role="r")
        blocks.block(conn, tid, kind="needs_input", reason="golden rows stay off?")
        file_row, slack_row = rows(conn)
        self.assertTrue(file_row["body"].startswith(f"capsule: {tid} blocked (needs_input): golden rows"))
        self.assertEqual(slack_row["body"],
                         f"\U0001F64B {tid} blocked (needs_input)\nreason: golden rows stay off?\n"
                         f"reply: `python3 -m capsule human my-app {tid} "
                         "unblock|reset|accept '<answer>'`")
        self.assertEqual(slack.classify(slack_row["body"]), "needs_input")
        self.assertEqual(slack.route(SLACK, slack_row["body"]), "#capsule-needs-you")   # fallback channel

    def test_a_review_infra_block_also_uses_the_needs_input_class(self):
        conn = board("my-app")
        outbox.set_slack(conn, True, now=T0)
        tid = db.new_task(conn, title="t", status="ready")
        blocks.block(conn, tid, kind="review_infra", reason="review run failed twice, no valid verdict")
        _, slack_row = rows(conn)
        self.assertTrue(slack_row["body"].startswith("\U0001F64B "))
        self.assertEqual(slack.classify(slack_row["body"]), "needs_input")
        self.assertIn("accept|reset", slack_row["body"])

    def test_a_spec_stale_block_also_uses_the_needs_input_class(self):
        """row 472 (capsule/guide-bundle): `spec_stale` is reserved as a valid block kind here so
        that branch's merge doesn't need to touch VALID_BLOCK_KINDS, and it posts 🙋 like
        needs_input/review_infra, not a generic 🔴 blocked."""
        conn = board("my-app")
        outbox.set_slack(conn, True, now=T0)
        tid = db.new_task(conn, title="t")
        blocks.block(conn, tid, kind="spec_stale", reason="spec citation is stale")
        _, slack_row = rows(conn)
        self.assertTrue(slack_row["body"].startswith("\U0001F64B "))
        self.assertEqual(slack.classify(slack_row["body"]), "needs_input")

    def test_a_blocked_kind_other_than_needs_input_or_review_infra_stays_blocked(self):
        conn = board("my-app")
        outbox.set_slack(conn, True, now=T0)
        tid = db.new_task(conn, title="t")
        lease.claim(conn, tid, ttl=900, role="r")
        blocks.block(conn, tid, kind="capability", reason="no runner")
        _, slack_row = rows(conn)
        self.assertTrue(slack_row["body"].startswith("\U0001F534 "))
        self.assertEqual(slack.classify(slack_row["body"]), "blocked")

    def test_needs_input_routes_to_its_own_channel_when_configured(self):
        conn = board("my-app")
        outbox.set_slack(conn, True, now=T0)
        tid = db.new_task(conn, title="t")
        lease.claim(conn, tid, ttl=900, role="r")
        blocks.block(conn, tid, kind="needs_input", reason="q")
        _, slack_row = rows(conn)
        slack_full = {"channels": CHANNELS_WITH_NEEDS_INPUT, "identity": "slack"}
        self.assertEqual(slack.route(slack_full, slack_row["body"]), "#capsule-needs-you-input")

    def test_the_allowlist_posts_needs_you_and_broken_kinds_only(self):
        conn = board()
        outbox.set_slack(conn, True, now=T0)
        for cls in ("needs_input", "blocked", "incident"):
            outbox.notify(conn, cls, f"t_1 {cls}", now=T0)
        posted = [slack.classify(r["body"]) for r in rows(conn) if r["channel"] == "slack"]
        self.assertEqual(posted, ["needs_input", "blocked", "incident"])

    def test_a_debug_kind_writes_the_file_row_and_no_slack_row(self):
        conn = board()
        outbox.set_slack(conn, True, now=T0)
        for cls in ("running", "done", "paused", "scheduled"):
            outbox.notify(conn, cls, f"t_1 {cls}", now=T0)
        got = rows(conn)
        self.assertEqual({r["channel"] for r in got}, {"file"})
        self.assertEqual(len(got), 4)

    def test_an_unknown_class_posts_fail_open(self):
        conn = board()
        outbox.set_slack(conn, True, now=T0)
        with mock.patch.dict(slack.CLASSES, {"novel": "\U0001F9EA"}):   # unknown to the allowlist
            outbox.notify(conn, "novel", "t_1 odd", now=T0)
        self.assertEqual([r["channel"] for r in rows(conn)], ["file", "slack"])

    def test_file_false_writes_only_the_slack_row(self):
        conn = board()
        outbox.notify(conn, "blocked", "t_1 stuck", file=False, now=T0)
        self.assertEqual(rows(conn), [])
        outbox.set_slack(conn, True, now=T0)
        outbox.notify(conn, "blocked", "t_1 stuck", file=False, now=T0)
        self.assertEqual([r["channel"] for r in rows(conn)], ["slack"])

    def test_a_template_error_never_aborts_the_caller(self):
        conn = board()
        outbox.set_slack(conn, True, now=T0)
        outbox.notify(conn, "done", "x", fields=[("a", 1), ("b", 2), ("c", 3), ("d", 4)], file_body="legacy", now=T0)
        self.assertEqual([(r["channel"], r["body"]) for r in rows(conn)], [("file", "legacy")])

    def test_the_first_screen_hold_and_its_refusal_reach_slack(self):
        conn = board("my-app")
        outbox.set_slack(conn, True, now=T0)
        tid = db.new_task(conn, title="t", branch="epic/ui")
        self.assertFalse(blocks.answer_first_screen(conn, tid, "screen-ok", "", now=T0))
        blocks.hold_first_screen(conn, tid, "epic/ui", screens=["static/timer.html"], commit="abc123", now=T0)
        got = [(r["channel"], r["body"]) for r in rows(conn)]
        # the refusal is a ⚙️ activity line: debug log only; the hold is a 🔴 and posts
        self.assertEqual([c for c, _ in got], ["file", "file", "slack"])
        self.assertTrue(got[0][1].startswith(f"capsule: {tid} screen-ok refused: "))
        want = (f"\U0001F534 {tid} first screen of epic/ui: epic held until you answer\n"
                "screens: static/timer.html\ncommit: abc123\n"
                f'reply: `python3 -m capsule human my-app {tid} screen-ok|screen-fix "<note>"`')
        self.assertEqual(got[2][1], want)
        self.assertTrue(got[1][1].startswith(f"capsule: {tid} first screen of epic/ui (row 369)"))

    def test_a_decision_without_a_recommendation_asks_for_an_answer(self):
        from capsule import spool
        conn = board("my-app")
        outbox.set_slack(conn, True, now=T0)
        spool.decision_notify(
            conn, "t_1", {"question": "Which?", "options": [{"id": "a", "label": "A"}]}, T0)
        body = conn.execute("SELECT body FROM outbox WHERE channel='slack'").fetchone()[0]
        self.assertEqual(body, "\U0001F534 Decide t_1: Which?\noptions: a) A\n"
                               "reply: `python3 -m capsule human my-app t_1 answer '<answer>'`")

    def test_set_slack_writes_only_on_change(self):
        conn = board()
        outbox.set_slack(conn, False, now=T0)
        self.assertEqual(db.guard_get(conn, outbox.SLACK_FLAG), (None, 0))
        outbox.set_slack(conn, True, now=T0); outbox.set_slack(conn, True, now=T0)
        self.assertEqual(db.guard_get(conn, outbox.SLACK_FLAG), (True, 1))


class Backoff(unittest.TestCase):
    def setUp(self):
        self.conn = board()

    def status(self):
        return [(r["channel"], r["status"], r["attempts"]) for r in rows(self.conn)]

    def test_429_holds_the_channel_for_retry_after_without_spending_an_attempt(self):
        outbox.add(self.conn, "slack", "a", now=T0); outbox.add(self.conn, "slack", "b", now=T0)
        send = Script(slack.Deferred(120))
        self.assertEqual(outbox.flush(self.conn, send, now=T0), 0)
        self.assertEqual(len(send.calls), 1)                     # b waits, the tick is not blocked
        self.assertEqual(self.status(), [("slack", "pending", 0), ("slack", "pending", 0)])
        self.assertEqual(outbox.flush(self.conn, send, now=T0 + 119), 0)
        self.assertEqual(len(send.calls), 1)                     # still inside Retry-After
        self.assertEqual(outbox.flush(self.conn, send, now=T0 + 120), 2)
        self.assertEqual(self.status(), [("slack", "delivered", 1), ("slack", "delivered", 1)])

    def test_failures_back_off_exponentially_and_reset_on_success(self):
        outbox.add(self.conn, "slack", "a", now=T0)
        send = Script(slack.SendError("slack unreachable"), slack.SendError("slack http 500"), None)
        outbox.flush(self.conn, send, now=T0)
        self.assertEqual(db.guard_get(self.conn, "outbox.backoff:slack")[0], {"until": T0 + 30, "failures": 1})
        outbox.flush(self.conn, send, now=T0 + 29)
        self.assertEqual(len(send.calls), 1)
        outbox.flush(self.conn, send, now=T0 + 30)
        self.assertEqual(db.guard_get(self.conn, "outbox.backoff:slack")[0], {"until": T0 + 90, "failures": 2})
        self.assertEqual(outbox.flush(self.conn, send, now=T0 + 90), 1)
        self.assertEqual(db.guard_get(self.conn, "outbox.backoff:slack")[0], {"until": 0, "failures": 0})
        self.assertEqual(rows(self.conn)[0]["last_error"], "slack http 500")
        self.assertEqual([outbox.backoff(n) for n in (1, 2, 3, 6, 20)], [30, 60, 120, 900, 900])

    def test_a_rejected_row_dies_alone_and_holds_nothing(self):
        outbox.add(self.conn, "slack", "bad", now=T0); outbox.add(self.conn, "slack", "good", now=T0)
        send = Script(slack.Rejected("slack error: channel_not_found"), None)
        self.assertEqual(outbox.flush(self.conn, send, now=T0), 1)
        self.assertEqual(self.status(), [("slack", "dead", 1), ("slack", "delivered", 1)])
        self.assertEqual(db.guard_get(self.conn, "outbox.backoff:slack"), (None, 0))
        self.assertEqual(rows(self.conn)[0]["last_error"], "slack error: channel_not_found")

    def test_a_held_slack_channel_never_holds_the_file_channel(self):
        outbox.add(self.conn, "slack", "s", now=T0); outbox.add(self.conn, "file", "f", now=T0)
        send = Script(slack.SendError("down"))
        self.assertEqual(outbox.flush(self.conn, send, now=T0), 1)
        self.assertEqual(self.status(), [("slack", "pending", 1), ("file", "delivered", 1)])

    def test_the_file_channel_keeps_its_immediate_retry(self):
        outbox.add(self.conn, "file", "f", now=T0)
        send = Script(OSError("disk"))
        outbox.flush(self.conn, send, now=T0)
        self.assertEqual(outbox.flush(self.conn, send, now=T0), 1)

    def test_a_slack_row_goes_dead_after_its_attempt_budget(self):
        outbox.add(self.conn, "slack", "s", now=T0)
        send = Script(*[slack.SendError("down")] * 20)
        now = T0
        for _ in range(outbox.MAX_ATTEMPTS["slack"] + 3):
            outbox.flush(self.conn, send, now=now)
            now += outbox.BACKOFF_CAP
        self.assertEqual(self.status(), [("slack", "dead", outbox.MAX_ATTEMPTS["slack"])])

    def test_at_most_the_per_pass_cap_goes_out(self):
        for i in range(outbox.SEND_CAP_PER_PASS + 5):
            outbox.add(self.conn, "slack", str(i), now=T0)
        self.assertEqual(outbox.flush(self.conn, Script(), now=T0), outbox.SEND_CAP_PER_PASS)
        self.assertEqual(outbox.flush(self.conn, Script(), now=T0), 5)


class Router(unittest.TestCase):
    def test_routes_by_channel_and_builds_the_slack_sender_once_per_config(self):
        home = Path(tempfile.mkdtemp())
        built, sent = [], []

        def factory(block):
            built.append(block)
            return lambda row: sent.append(row["body"])

        router = outbox.Router(home=home, slack_factory=factory)
        router.configure({"slack": SLACK}); router.configure({"slack": dict(SLACK)})
        router({"channel": "slack", "body": "x"}); router({"channel": "file", "body": "y"})
        self.assertEqual((len(built), sent), (1, ["x"]))
        self.assertTrue((home / "outbox.log").read_text().endswith("\ty\n"))
        router.configure({})
        with self.assertRaisesRegex(slack.SendError, "not configured"):
            router({"channel": "slack", "body": "x"})
        with self.assertRaisesRegex(ValueError, "unknown outbox channel"):
            router({"channel": "pager", "body": "x"})


class SummaryRender(unittest.TestCase):
    """A `scheduled` digest: the multi-line body, whole, behind SUMMARY_GLYPH, capped."""

    def test_keeps_every_line_behind_the_summary_glyph(self):
        text = slack.render("scheduled", "line one\n\nline two\nline three", digest=True)
        self.assertEqual(text, slack.SUMMARY_GLYPH + " line one\n\nline two\nline three")
        self.assertEqual(slack.classify(text), slack.DEFAULT_CLASS)   # route(), not classify

    def test_caps_a_long_body_with_the_truncation_tail(self):
        text = slack.render("scheduled", "x" * 5000, digest=True)
        self.assertLessEqual(len(text), len(slack.SUMMARY_GLYPH) + 1 + slack.SUMMARY_MAX)
        self.assertTrue(text.endswith("…truncated, see outbox.log"))

    def test_an_empty_body_is_refused(self):
        with self.assertRaises(ValueError):
            slack.render("scheduled", "  \n ", digest=True)

    def test_summary_is_no_longer_a_class(self):
        with self.assertRaises(ValueError):
            slack.render("summary", "a\nb")

    def test_a_plain_scheduled_line_keeps_its_glyph(self):
        text = slack.render("scheduled", "heal replay")
        self.assertTrue(text.startswith(slack.CLASSES["scheduled"]))

    def test_notify_posts_it_whole_and_files_the_full_body(self):
        conn = board()
        outbox.set_slack(conn, True, now=T0)
        body = "a\nb\nc"
        ids = outbox.notify(conn, "scheduled", body, file_body=body, digest=True, now=T0)
        got = {r["channel"]: r["body"] for r in rows(conn)}
        self.assertEqual(len(ids), 2)
        self.assertEqual(got["slack"], slack.SUMMARY_GLYPH + " a\nb\nc")
        self.assertEqual(got["file"], body)
        self.assertEqual(len(outbox.notify(conn, "scheduled", "heal replay", now=T0)), 1)


class BrainstormBlocks(unittest.TestCase):
    def test_opener_gets_open_and_queue_buttons(self):
        got = slack._answer_blocks("event", "brainstorm:" + "ab12" * 4, "hello")
        self.assertEqual([b["type"] for b in got], ["section", "actions"])
        self.assertEqual([e["value"] for e in got[1]["elements"]],
                         ["bs:" + "ab12" * 4 + ":open", "bs:" + "ab12" * 4 + ":queue"])

    def test_other_keys_and_long_text_get_no_buttons(self):
        self.assertEqual(slack._brainstorm_blocks("brainstorm:zz", "hi"), [])
        self.assertEqual(slack._brainstorm_blocks(None, "hi"), [])
        self.assertEqual(slack._brainstorm_blocks("brainstorm:" + "ab12" * 4,
                                                  "x" * (slack.SECTION_MAX + 1)), [])


if __name__ == "__main__":
    unittest.main()
