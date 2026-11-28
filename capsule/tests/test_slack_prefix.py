"""Factory autonomy Task 12 (spec 4.7): two boards share channels, so each Slack line names its board.
Oracle 1: the posted payload. Oracle 2: the file channel line stays as it was."""
import io
import json
import tempfile
import unittest
from pathlib import Path

from capsule import outbox, slack

BLOCK = {"enabled": True, "identity": "x", "board_prefix": True,
         "channels": {c: f"C_{c}" for c in slack.CLASSES
                      if c not in (*slack.OPTIONAL_CHANNELS, *slack.INBOX_CLASSES)}}


class FakeResp(io.BytesIO):
    headers = {}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class Prefix(unittest.TestCase):
    def test_validate(self):
        slack.validate(BLOCK)
        with self.assertRaises(ValueError):
            slack.validate(dict(BLOCK, board_prefix="yes"))

    def test_the_posted_text_names_the_board_and_routing_is_unchanged(self):
        posted = []

        def urlopen(req, timeout=None):
            posted.append(json.loads(req.data.decode()))
            return FakeResp(b'{"ok": true, "ts": "1"}')
        router = outbox.Router(home=None, slack_factory=lambda blk: slack.Sender(blk, token=lambda: "xoxb-fake",
                                                                                   urlopen=urlopen))
        router.configure({"board": "capsule-corp", "slack": BLOCK})
        body = slack.render("blocked", "t_0000aaaa blocked (needs_input)", [("reason", "r")], None)
        router({"channel": "slack", "body": body, "thread_key": "t_0000aaaa"})
        self.assertTrue(posted[0]["text"].startswith("[capsule-corp] "))           # oracle 1
        self.assertEqual(posted[0]["channel"], "C_blocked")

    def test_the_file_channel_line_is_never_prefixed(self):
        d = Path(tempfile.mkdtemp())
        router = outbox.Router(home=None, log_path=d / "outbox.log")
        router.configure({"board": "capsule-corp", "slack": BLOCK})
        router({"channel": "file", "body": "capsule: t_0000aaaa blocked"})
        self.assertEqual((d / "outbox.log").read_text().split("\t", 1)[1], "capsule: t_0000aaaa blocked\n")  # oracle 2
