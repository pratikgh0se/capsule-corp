"""M204 slice B3: opening and closing a Slack brainstorm (D229). Oracles: the opener Capsule queues,
read back from the outbox and sent through the real Slack sender against a scripted urlopen
(the channel and thread it posts to), and the vault note on disk, pushed to a local bare remote.
A temp CAPSULE_HOME and a temp vault; no network, no real Slack, no claude."""
import contextlib
import io
import json
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from capsule import brainstorm, brainstorm_store as store, db, outbox, paths, slack
from capsule.__main__ import main
from capsule.tests.test_slack import FakeHTTP
from capsule.tests.test_slack_edit import Tokens

BOARD = "sample-board"
NOW = 1_800_000_000
CHANNEL = "C0BRAIN"


def git(cwd, *args):
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t",
               GIT_COMMITTER_EMAIL="t@t")
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, env=env,
                          check=True).stdout


def runner(cmd):
    return subprocess.run(cmd, capture_output=True, text=True,
                          env=dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
                                   GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t"))


class Base(unittest.TestCase):
    def setUp(self):
        tmp = Path(tempfile.mkdtemp())
        self.home, self.vault, self.remote = tmp / "home", tmp / "vault", tmp / "remote.git"
        cfg = json.loads((paths.REPO / "capsule" / "config.example.json").read_text())
        cfg.update(board=BOARD)
        cfg["slack"] = dict(cfg["slack"], enabled=True)
        cfg["slack"]["channels"] = dict(cfg["slack"]["channels"], brainstorm=CHANNEL)
        self.cfg = cfg
        paths.board_dir(BOARD, self.home).mkdir(parents=True)
        db.create_board(paths.board_db(BOARD, self.home))
        self.conn = db.connect(paths.board_db(BOARD, self.home))
        self.addCleanup(self.conn.close)
        self.write_cfg()
        env = {"OPS_MESSAGE_STATE": str(tmp / "budget.json"), "OPS_BRIEF_V2": "0"}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)

    def write_cfg(self):
        paths.config_path(self.home, BOARD).write_text(json.dumps(self.cfg))
        outbox.set_slack(self.conn, slack.block(self.cfg), now=NOW)
        self.conn.commit()

    def slack_rows(self):
        return self.conn.execute("SELECT o.body, k.dedup_key FROM outbox o LEFT JOIN outbox_key k"
                                 " ON k.outbox_id=o.id WHERE o.channel='slack'"
                                 " ORDER BY o.id").fetchall()

    def ids(self):
        root = paths.board_dir(BOARD, self.home) / "brainstorms"
        return sorted(p.name for p in root.iterdir()) if root.is_dir() else []

    def open(self, title="Pricing for beta", framing="Should beta be free?", **kw):
        return brainstorm.open_brainstorm(title, board=BOARD, framing=framing, home=self.home,
                                          now=NOW, **kw)

    def cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = main(["brainstorm", *argv, "--board", BOARD, "--capsule-home", str(self.home)])
        return rc, out.getvalue(), err.getvalue()


class Open(Base):
    def test_opener_is_queued_under_its_key_with_the_handoff_block(self):
        bid = self.open()
        [(body, key)] = self.slack_rows()
        self.assertEqual(key, f"brainstorm:{bid}")
        meta = store.read_meta(self.home, BOARD, bid)
        name = meta["name"]
        self.assertRegex(name, rf"^pricing-for-beta-{bid[:6]}$")
        self.assertTrue(body.startswith(f"{slack.BRAINSTORM_GLYPH} Brainstorm: Pricing for beta\n"))
        self.assertIn("Should beta be free?", body)
        self.assertIn(f"claude --resume {meta['session_id']}\n", body)    # resumed from the ws dir
        self.assertIn(f"cd {store.bdir(self.home, BOARD, bid) / 'ws'}\n", body)
        self.assertNotIn("--session-id", body)
        self.assertNotIn("claude -r", body)
        self.assertIn("One terminal only", body)
        self.assertNotIn("[ Open the note ]", body)
        self.assertIn("Cost: unknown until the first turn", body)    # M203: no count at open time
        self.assertNotIn("Later:", body)
        self.assertEqual((meta["status"], meta["started_by"], meta["channel"], meta["thread_ts"]),
                         ("open", "capsule", CHANNEL, ""))
        self.assertEqual([t["role"] for t in store.read_turns(self.home, BOARD, bid)], ["capsule"])
        self.assertTrue((store.bdir(self.home, BOARD, bid) / "ws").is_dir())

    def test_sender_posts_it_to_the_brainstorm_channel_and_the_thread_is_adopted(self):
        bid = self.open()
        http = FakeHTTP({"ok": True, "ts": "1800000000.000100"})
        sender = slack.Sender(slack.block(self.cfg), token=Tokens("xoxb-fake"), urlopen=http,
                              debug_log=self.home / "debug.log")
        self.assertEqual(outbox.flush(self.conn, sender, now=NOW), 1)
        self.assertEqual(http.calls[0]["body"]["channel"], CHANNEL)
        self.assertNotIn("thread_ts", http.calls[0]["body"])
        self.assertEqual(store.read_meta(self.home, BOARD, bid)["thread_ts"], "")
        meta = brainstorm.adopt_thread(self.home, BOARD, bid)
        self.assertEqual((meta["channel"], meta["thread_ts"]), (CHANNEL, "1800000000.000100"))

    def test_unset_channel_refuses_and_queues_nothing(self):
        del self.cfg["slack"]["channels"]["brainstorm"]
        self.write_cfg()
        rc, _, err = self.cli("open", "Pricing", "--framing", "x")
        self.assertEqual(rc, 1)
        self.assertIn("slack.channels.brainstorm is not set", err)
        self.assertEqual(self.slack_rows(), [])
        self.assertEqual(self.ids(), [])

    def test_slack_off_refuses(self):
        self.cfg["slack"]["enabled"] = False
        self.write_cfg()
        with self.assertRaisesRegex(brainstorm.Refused, "slack is not on"):
            self.open()

    def test_config_on_but_guard_flag_off_refuses_and_queues_nothing(self):
        db.guard_set(self.conn, outbox.SLACK_FLAG, False, now=NOW)    # the tick has not mirrored it
        with self.assertRaisesRegex(brainstorm.Refused, r"\(the tick mirrors the setting\)"):
            self.open()
        rc, _, err = self.cli("open", "Pricing", "--framing", "x")
        self.assertEqual(rc, 1)
        self.assertIn("slack is not on for this board (the tick mirrors the setting)", err)
        self.assertEqual((self.slack_rows(), self.ids()), ([], []))

    def test_secrets_in_title_and_framing_never_reach_the_message(self):
        bid = self.open("xoxb-1234-secret", "token xapp-1-ABC-def here")
        [(body, _)] = self.slack_rows()
        self.assertNotIn("xoxb-1234", body)
        self.assertNotIn("xapp-1-ABC", body)
        self.assertNotIn("xoxb-1234", store.read_meta(self.home, BOARD, bid)["title"])

    def test_cli_open_prints_the_id(self):
        rc, out, _ = self.cli("open", "Pricing", "--framing", "x")
        bid = re.fullmatch(r"opened: ([0-9a-f]{16})\n", out).group(1)
        self.assertEqual((rc, self.ids()), (0, [bid]))

    def test_actionable_cap_counts_the_opener_and_defers_the_overflow(self):
        with mock.patch.dict(os.environ, {"OPS_BRIEF_V2": "1", "OPS_ACTIONABLE_MAX": "1"}):
            first = self.open("one")
            with self.assertRaisesRegex(brainstorm.Refused, "cap is spent"):
                self.open("two")
        self.assertEqual([k for _, k in self.slack_rows()], [f"brainstorm:{first}"])
        self.assertEqual(self.ids(), [first])
        counts = json.loads(Path(os.environ["OPS_MESSAGE_STATE"]).read_text())
        self.assertEqual((counts["actionable"], counts["deferred"]), (1, 1))

    def test_no_cap_when_the_budget_is_off(self):
        for n in range(10):
            self.open(f"t{n}")
        self.assertEqual(len(self.slack_rows()), 10)


class Config(unittest.TestCase):
    CH = {"blocked": "C1", "done": "C2", "scheduled": "C2", "running": "C3", "incident": "C4",
          "paused": "C1", "inbox": "C5"}

    def block(self, **kw):
        return dict({"channels": dict(self.CH, brainstorm="C6"), "identity": "slack"}, **kw)

    def test_brainstorm_channel_is_optional_and_accepted(self):
        self.assertEqual(slack.validate(self.block())["channels"]["brainstorm"], "C6")
        bare = slack.validate({"channels": self.CH, "identity": "slack"})
        self.assertNotIn("brainstorm", bare["channels"])

    def test_allowlist_must_cover_it(self):
        with self.assertRaisesRegex(ValueError, "leaves out 'C6'"):
            slack.validate(self.block(allowed_channels=["C1", "C2", "C3", "C4", "C5"]))
        slack.validate(self.block(allowed_channels=["C1", "C2", "C3", "C4", "C5", "C6"]))

    def test_an_unset_channel_rejects_the_post_not_the_other_channels(self):
        with self.assertRaises(slack.Rejected):
            slack.route({"channels": self.CH}, f"{slack.BRAINSTORM_GLYPH} hi")
        self.assertEqual(slack.route({"channels": self.CH}, "\U0001F534 x"), "C1")


class Close(Base):
    def setUp(self):
        super().setUp()
        git(self.vault.parent, "init", "-q", "--bare", str(self.remote))
        git(self.vault.parent, "init", "-q", "-b", "main", str(self.vault))
        (self.vault / "README.md").write_text("x\n")
        git(self.vault, "add", "-A")
        git(self.vault, "commit", "-q", "-m", "init")
        git(self.vault, "remote", "add", "origin", str(self.remote))
        git(self.vault, "push", "-q", "origin", "main")
        self.bid = self.open()
        store.append_turn(self.home, BOARD, self.bid, "owner", "Free for 3 months?", ts=NOW + 60)
        store.append_turn(self.home, BOARD, self.bid, "assistant",
                          "Probably.\nFollow-up: price the paid tier\ntoken xoxb-99-abc",
                          ts=NOW + 120)
        store.append_turn(self.home, BOARD, self.bid, "owner", "todo: ask three users",
                          ts=NOW + 180)

    def test_close_writes_scrubbed_note_pushes_it_and_marks_closed(self):
        rel = brainstorm.close(self.home, BOARD, self.bid, vault=self.vault, now=NOW + 300,
                               git_runner=runner)
        self.assertEqual(rel, f"Brainstorms/2027-01-15-pricing-for-beta-{self.bid}.md")
        text = (self.vault / rel).read_text()
        meta = store.read_meta(self.home, BOARD, self.bid)
        self.assertTrue(text.startswith("---\n"))
        for line in ("type: brainstorm", "status: closed", f"brainstorm_id: {self.bid}",
                     "ai-first: true",
                     f"session_name: {meta['name']}", f"session_id: {meta['session_id']}",
                     'title: "Pricing for beta"', "closed_at: 2027-01-15 08:05"):
            self.assertIn(line + "\n", text)
        self.assertRegex(text, r"## For future agent\nSlack brainstorm \"Pricing for beta\"")
        self.assertIn(f"claude --resume {meta['session_id']}", text)
        self.assertIn("- 2027-01-15 08:01 owner: Free for 3 months?\n", text)
        self.assertIn("assistant: Probably. / Follow-up: price the paid tier / token ***", text)
        self.assertNotIn("xoxb-99", text)
        tail = text.split("## Proposed follow-ups (need terminal)\n")[1]
        self.assertEqual(tail, "- price the paid tier\n- ask three users\n")
        self.assertEqual((meta["status"], meta["closed_at"], meta["note"]),
                         ("closed", NOW + 300, rel))
        self.assertEqual(git(self.remote, "show", f"main:{rel}"), text)      # pushed to the remote

    def test_no_follow_ups_says_so(self):
        bid = self.open("Quiet")
        rel = brainstorm.close(self.home, BOARD, bid, vault=self.vault, now=NOW + 5,
                               git_runner=runner)
        self.assertIn(brainstorm.NO_FOLLOW_UP, (self.vault / rel).read_text())

    def test_close_twice_returns_the_same_note_and_rewrites_nothing(self):
        rel = brainstorm.close(self.home, BOARD, self.bid, vault=self.vault, now=NOW + 300,
                               git_runner=runner)
        before = (self.vault / rel).read_text()
        again = brainstorm.close(self.home, BOARD, self.bid, vault=self.vault, now=NOW + 999,
                                 git_runner=runner)
        self.assertEqual((again, (self.vault / rel).read_text()), (rel, before))

    def test_missing_vault_refuses_and_leaves_it_open(self):
        with self.assertRaises(brainstorm.Refused):
            brainstorm.close(self.home, BOARD, self.bid, vault=self.vault / "nope")
        self.assertEqual(store.read_meta(self.home, BOARD, self.bid)["status"], "open")

    def test_cli_close(self):
        rc, out, _ = self.cli("close", self.bid, "--vault", str(self.vault))
        self.assertEqual(rc, 0)
        self.assertRegex(out, rf"closed: Brainstorms/[\d-]{{10}}-pricing-for-beta-{self.bid}\.md")
        rc, _, err = self.cli("close", "0" * 16, "--vault", str(self.vault))
        self.assertEqual(rc, 1)
        self.assertIn("no brainstorm", err)


class Store(Base):
    def test_layout_of_an_opened_brainstorm(self):
        bid = self.open()
        d = store.bdir(self.home, BOARD, bid)
        self.assertEqual(d, self.home / "boards" / BOARD / "brainstorms" / bid)
        self.assertEqual(sorted(p.name for p in d.iterdir()),
                         ["meta.json", "pending", "turns.jsonl", "ws"])
        self.assertEqual(list((d / "ws").iterdir()), [])

    def test_bad_ids_and_roles_are_refused(self):
        for bad in ("../x", "short", "G" * 16):
            with self.assertRaises(ValueError):
                store.bdir(self.home, BOARD, bad)
        bid = self.open()
        with self.assertRaises(ValueError):
            store.append_turn(self.home, BOARD, bid, "system", "x")
        with self.assertRaises(ValueError):
            store.read_meta(self.home, BOARD, "0" * 16)

    def test_update_meta_merges(self):
        bid = self.open()
        store.update_meta(self.home, BOARD, bid, status="closed")
        meta = store.read_meta(self.home, BOARD, bid)
        self.assertEqual((meta["status"], meta["title"]), ("closed", "Pricing for beta"))

    def test_show_prints_state_and_turns(self):
        bid = self.open()
        store.append_turn(self.home, BOARD, bid, "owner", "hello", ts=NOW + 60)
        rc, out, _ = self.cli("show", bid)
        self.assertEqual(rc, 0)
        self.assertIn("title: Pricing for beta\nstatus: open\n", out)
        self.assertIn("2027-01-15 08:01 owner: hello", out)


if __name__ == "__main__":
    unittest.main()
