"""`capsule human-apply --board <b>`: the tick's own human-file step, run once under the tick's
single-writer lock, with no dispatch and no outbox send (the capsule-corp tick is stopped on
purpose, so a queued park or close would otherwise never take effect).
Oracle 1: the card's own state and the file's move to done/. Oracle 2: nothing else ran - no
run row, other cards untouched, and `Tick.once` / `outbox.flush` are never called."""
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from capsule import db, human, intent, outbox, paths, tick, tick_guard
from capsule.__main__ import main

BOARD = "bx"
NOW = 1_800_000_000


class HumanApply(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.conn = self.make_board(BOARD)
        self.park_id, self.close_id, self.other_id = (
            db.new_task(self.conn, title=t, assignee="dev-backend", status="ready", now=NOW - 100)
            for t in ("to park", "to close", "untouched"))
        self.conn.commit()

    def make_board(self, board, named=None):
        """A temp board with the repo's own schema; `named` is what its config says it serves."""
        paths.board_dir(board, self.home).mkdir(parents=True)
        cfg = json.loads((paths.REPO / "capsule" / "config.example.json").read_text())
        paths.config_path(self.home, board).write_text(json.dumps(dict(cfg, board=named or board)))
        db.create_board(paths.board_db(board, self.home))
        conn = db.connect(paths.board_db(board, self.home))
        self.addCleanup(conn.close)
        return conn

    def status(self, tid):
        return self.conn.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()[0]

    def states(self):
        return self.status(self.park_id), self.status(self.close_id)

    def event_count(self):
        return self.conn.execute("SELECT COUNT(*) FROM task_events").fetchone()[0]

    def queue(self):
        human.write(self.home, BOARD, self.park_id, "park", "until the move", author="cos", now=NOW)
        human.write(self.home, BOARD, self.close_id, "close", "landed by hand",
                    author="cos", now=NOW)

    def apply(self, board=BOARD):
        """(exit code, stdout, stderr) of `human-apply`; a tick pass or outbox flush fails it."""
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(tick.Tick, "once", side_effect=AssertionError("tick pass ran")), \
             mock.patch.object(outbox, "flush", side_effect=AssertionError("outbox flushed")), \
             contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(["human-apply", "--board", board, "--capsule-home", str(self.home)])
        return code, out.getvalue(), err.getvalue()

    def test_park_and_close_take_effect_and_files_move_to_done(self):
        self.queue()
        code, out, _ = self.apply()
        self.assertEqual(code, 0)
        self.assertIn("processed 2 human file(s)", out)
        self.assertIn(f"1.json: {self.park_id} park -> recorded; card now blocked", out)
        self.assertIn(f"2.json: {self.close_id} close -> applied; card now done", out)
        self.assertEqual(self.states(), ("blocked", "done"))
        hdir = paths.board_dir(BOARD, self.home) / "human"
        self.assertEqual(sorted(p.name for p in hdir.glob("*.json")), [])
        done = sorted(p.name for p in (hdir / "done").glob("*.json"))
        self.assertEqual(done, ["1.json", "2.json"])

    def test_nothing_is_dispatched_or_sent(self):
        self.queue()
        self.apply()
        self.assertEqual(self.status(self.other_id), "ready")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM task_runs").fetchone()[0], 0)
        sent = self.conn.execute(
            "SELECT COUNT(*) FROM outbox WHERE delivered_at IS NOT NULL OR attempts > 0")
        self.assertEqual(sent.fetchone()[0], 0)

    def test_refuses_while_a_tick_holds_the_lock(self):
        self.queue()
        with contextlib.closing(tick_guard.guard_start(self.home, BOARD)):
            code, out, err = self.apply()
        self.assertEqual((code, out), (3, ""))
        self.assertIn("a live tick holds this board's lock", err)
        self.assertEqual(self.states(), ("ready", "ready"))
        self.assertEqual(len(list((paths.board_dir(BOARD, self.home) / "human").glob("*.json"))), 2)

    def test_second_call_is_a_no_op(self):
        self.queue()
        self.apply()
        events = self.event_count()
        code, out, _ = self.apply()
        self.assertEqual((code, out), (0, "no queued human files\n"))
        self.assertEqual(self.event_count(), events)

    def test_unknown_board_is_refused(self):
        code, _, err = self.apply("nope")
        self.assertEqual(code, 1)
        self.assertIn("capsule human-apply:", err)

    def test_a_refused_or_unknown_file_is_never_reported_as_applied(self):
        review = db.new_task(self.conn, title="in review", assignee="dev-backend",
                             status="review", now=NOW - 100)
        human.write(self.home, BOARD, review, "close", "landed by hand", author="cos", now=NOW)
        human.write(self.home, BOARD, "t_nosuch", "park", "until never", author="cos", now=NOW)
        code, out, _ = self.apply()
        self.assertEqual((code, self.status(review)), (0, "review"))
        self.assertIn(f"1.json: {review} close -> refused:review: cannot close;"
                      " card now review", out)
        self.assertIn("2.json: no event", out)
        self.assertNotIn("applied", out)

    def test_refuses_a_config_that_names_another_board(self):
        self.make_board("by", named="other")
        human.write(self.home, "by", "t_x", "park", "until never", author="cos", now=NOW)
        code, out, err = self.apply("by")
        self.assertEqual((code, out), (1, ""))
        self.assertIn("names board 'other', not 'by'", err)
        self.assertEqual(len(list((paths.board_dir("by", self.home) / "human").glob("*.json"))), 1)

    def test_migrates_the_board_before_applying(self):
        self.queue()
        with mock.patch.object(db, "migrate", wraps=db.migrate) as migrate:
            self.apply()
        migrate.assert_called_once()

    def test_intent_examples(self):
        """Z1: every `human_apply` row in contracts/intent/tick_guard.jsonl, driven for real."""
        texts = {"park": "until the move", "close": "landed by hand"}
        seen = 0
        for ex in intent.load("tick_guard"):
            doc, expect = ex["given"]["document"], ex["expect"]
            if doc.get("check") != "human_apply":
                continue                                   # the other guards' rows are theirs
            seen += 1
            board = f"intent{seen}"
            conn = self.make_board(board)
            ids = [db.new_task(conn, title=f"card {i}", assignee="dev-backend", status=st,
                               now=NOW - 100) for i, st in enumerate(doc["statuses"])]
            for tid, kind in zip(ids, doc["queued"]):
                human.write(self.home, board, tid, kind, texts[kind], author="cos", now=NOW)
            if doc["lock_held"]:
                with contextlib.closing(tick_guard.guard_start(self.home, board)):
                    code, out, _ = self.apply(board)
            else:
                code, out, _ = self.apply(board)
            with self.subTest(ex["id"]):
                self.assertEqual(code, expect["exit"])
                statuses = [conn.execute("SELECT status FROM tasks WHERE id=?", (t,)).fetchone()[0]
                            for t in ids]
                self.assertEqual(statuses, expect["statuses"])
                left = list((paths.board_dir(board, self.home) / "human").glob("*.json"))
                self.assertEqual(len(left), expect["files_left"])
                if "task_runs" in expect:
                    runs = conn.execute("SELECT COUNT(*) FROM task_runs").fetchone()[0]
                    self.assertEqual(runs, expect["task_runs"])
                if "outbox_sent" in expect:
                    sent = conn.execute("SELECT COUNT(*) FROM outbox WHERE delivered_at IS NOT NULL"
                                        " OR attempts > 0").fetchone()[0]
                    self.assertEqual(sent, expect["outbox_sent"])
                for word in expect.get("reported", []):
                    self.assertIn(word, out)
        self.assertEqual(seen, 3)


if __name__ == "__main__":
    unittest.main()
