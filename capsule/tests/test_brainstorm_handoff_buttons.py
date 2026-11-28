"""M203 / Slack phase 6: the handoff's two buttons. Oracles: the Block Kit the Sender would post,
the Result a fake click gets back from slack_answer._handle, and brief.waiting_block reading the
defer queue the click wrote. Fakes only: no Slack, no network, AGENT_OPS_NO_SEND."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from capsule import brainstorm, paths, slack, slack_answer
from capsule.tests.test_brainstorm import BOARD, CHANNEL, Base
from capsule.tests.test_slack_answer import OWNER

sys.path.insert(0, str(paths.REPO / "maintenance"))
import brief  # noqa: E402


class Buttons(Base):
    def test_opener_carries_cost_and_two_buttons_whose_values_name_the_brainstorm(self):
        bid = self.open(questions=3)
        [(body, key)] = self.slack_rows()
        self.assertIn("Cost: 3 open questions - ~15 min", body)
        blocks = slack._answer_blocks(slack.classify(body), key, body)
        labels = [(b["text"]["text"], b["value"]) for b in blocks[1]["elements"]]
        self.assertEqual(labels, [("Open the note", f"bs:{bid}:open"),
                                  ("Not now, queue it", f"bs:{bid}:queue")])

    def test_other_messages_get_no_brainstorm_buttons(self):
        self.assertEqual(slack._answer_blocks("blocked", "brainstorm:zz", "x"), [])
        self.assertEqual(slack._answer_blocks("blocked", f"brainstorm-reply:{'a' * 16}:1", "x"), [])


class Click(Base):
    def setUp(self):
        super().setUp()
        paths.config_path(self.home).write_text(json.dumps({"slack_owner_id": OWNER}))
        tmp = Path(tempfile.mkdtemp())
        self.seen = tmp / "notify-seen.json"
        env = {"AGENT_OPS_NO_SEND": "1", "CAPSULE_BOARD": BOARD, "AGENT_OPS_STATE_DIR": str(tmp),
               "OPS_MESSAGE_STATE": str(tmp / "budget.json"), "OPS_BUDGET_STATE": str(tmp)}
        p = mock.patch.dict(os.environ, env)
        p.start()
        self.addCleanup(p.stop)
        self.bid = self.open()

    def block(self):
        return brief.waiting_block(seen_path=self.seen, blocked=[], active_boards=[],
                                   home=self.home)

    def click(self, value, user=OWNER):
        return slack_answer._brainstorm_click(self.home, user, value)

    def test_non_owner_is_dropped_silently_and_queues_nothing(self):
        res = self.click(f"bs:{self.bid}:queue", user="U0STRANGER")
        self.assertEqual((res.accepted, res.reply), (False, ""))
        self.assertFalse(self.seen.exists())

    def test_malformed_values_are_rejected(self):
        for bad in ("bs:../x:open", f"bs:{self.bid}:delete", f"bs:{self.bid}:open\n", "bs:", None):
            res = self.click(bad)
            self.assertEqual((res.accepted, res.reply), (False, "That button is not valid."), bad)

    def test_unknown_brainstorm_id_is_refused_with_a_reply(self):
        for act in ("queue", "open"):
            res = self.click("bs:" + "0" * 16 + ":" + act)
            self.assertEqual((res.accepted, res.reason, res.reply),
                             (False, "unknown brainstorm", "That brainstorm is gone."))
        self.assertFalse(self.seen.exists())

    def test_open_replies_with_note_cd_and_resume_only(self):
        meta = brainstorm.store.read_meta(self.home, BOARD, self.bid)
        res = self.click(f"bs:{self.bid}:open")
        self.assertTrue(res.accepted)
        ws = paths.board_dir(BOARD, self.home) / "brainstorms" / self.bid / "ws"
        self.assertIn(f"cd {ws}", res.reply)
        self.assertIn(f"claude --resume {meta['session_id']}", res.reply)
        self.assertIn("Note: not written yet", res.reply)
        self.assertFalse(self.seen.exists())                       # open writes nothing
        self.assertFalse(Path(os.environ["OPS_MESSAGE_STATE"]).exists())
        self.assertFalse(Path(os.environ["OPS_BUDGET_STATE"], "brief-batch.json").exists())

    def test_open_quotes_the_session_id_and_never_prints_a_bare_resume(self):
        brainstorm.store.update_meta(self.home, BOARD, self.bid, session_id="a b;x")
        res = self.click(f"bs:{self.bid}:open")
        self.assertIn("claude --resume 'a b;x'", res.reply)
        brainstorm.store.update_meta(self.home, BOARD, self.bid, session_id="")
        res = self.click(f"bs:{self.bid}:open")
        self.assertEqual(res.reply, "That brainstorm has not started yet.")
        self.assertNotIn("--resume", res.reply)

    def test_queue_is_idempotent_and_lands_in_the_waiting_block(self):
        first = self.click(f"bs:{self.bid}:queue")
        second = self.click(f"bs:{self.bid}:queue")
        self.assertEqual((first.reply, second.reply), ("queued for the morning brief",) * 2)
        self.assertFalse(self.seen.exists())                       # no notify-seen write
        batch = json.loads(Path(os.environ["OPS_BUDGET_STATE"], "brief-batch.json").read_text())
        self.assertEqual([(r["task_id"], r["kind"]) for r in batch], [(self.bid, "waiting")])
        counts = json.loads(Path(os.environ["OPS_MESSAGE_STATE"]).read_text())
        self.assertEqual(counts["deferred"], 1)
        title = brainstorm.store.read_meta(self.home, BOARD, self.bid)["title"]
        block = self.block()
        self.assertEqual(block[0], "WAITING ON YOU - 1")
        self.assertIn(f"Brainstorm - {title} - queued", block[1])
        self.assertNotIn(self.bid, block[1])

    def test_two_rows_for_one_brainstorm_make_one_line_with_the_oldest_age(self):
        import time
        now = time.time()
        rows = [{"ts": now - h * 3600, "kind": "waiting", "board": BOARD, "task_id": self.bid,
                 "text": "Brainstorm - Plan X"} for h in (2, 5)]
        Path(os.environ["OPS_BUDGET_STATE"], "brief-batch.json").write_text(json.dumps(rows))
        block = self.block()
        self.assertEqual(len(block), 2)
        self.assertIn("open 5 h", block[1])

    def test_a_closed_brainstorm_drops_out_of_the_waiting_block(self):
        self.click(f"bs:{self.bid}:queue")
        brainstorm.store.update_meta(self.home, BOARD, self.bid, status="closed")
        self.assertEqual(self.block(), ["WAITING ON YOU - 0"])

    def test_handle_routes_a_bs_click_and_leaves_the_ask_path_alone(self):
        with mock.patch.object(slack_answer.base, "source_ok", return_value=True):
            out = slack_answer._handle(self.home, {
                "type": "block_actions", "user": {"id": "U0STRANGER"}, "channel": {"id": CHANNEL},
                "message": {"ts": "1.2"}, "actions": [{"value": f"bs:{self.bid}:open"}]})
            self.assertEqual(out, {"ok": False})
            out = slack_answer._handle(self.home, {
                "type": "block_actions", "user": {"id": OWNER}, "channel": {"id": CHANNEL},
                "message": {"ts": "1.2"}, "actions": [{"value": "ask:" + "0" * 16 + ":yes"}]})
        self.assertEqual(out, {"ok": False})     # unchanged: an unknown ask is refused


if __name__ == "__main__":
    unittest.main()
