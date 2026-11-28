import contextlib
import io
import json
import os
import tempfile
import unittest
from argparse import Namespace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from capsule import db, paths, vault_context_cli as cli
from capsule.tests.fixtures.board_ddl import board_ddl

BOARD = "b"


class Queue(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.verdicts = self.home / "v" / "verdicts.jsonl"
        self.spool = paths.board_dir(BOARD, self.home) / "spool"
        self.db = paths.board_db(BOARD, self.home)
        self.db.parent.mkdir(parents=True)
        self.conn = db.connect(self.db)
        self.conn.executescript(board_ddl())
        db.migrate(self.conn)
        self.addCleanup(self.conn.close)
        self.conn.execute("INSERT INTO tasks (id, title, body, assignee, status, created_at)"
                          " VALUES ('t1', 'Card', ?, 'x', 'ready', 1)", ("b" * 1500,))

    def record(self, run, flags, hours=1, **kw):
        when = datetime.now(timezone.utc) - timedelta(hours=hours)
        rec = {"run_id": run, "task_id": "t1", "flags": flags, "withheld": [],
               "retrieved": when.strftime("%Y-%m-%d %H:%M"),
               "kept": [{"path": "Specs/a.md"}, {"number": 12}], **kw}
        d = self.spool / run
        d.mkdir(parents=True)
        (d / "vault_context.json").write_text(json.dumps(rec))
        os.utime(d / "vault_context.json", (when.timestamp(),) * 2)
        self.conn.execute("INSERT INTO task_runs (task_id, status, started_at, run_id)"
                          " VALUES ('t1', 'done', 1, ?)", (run,))

    def call(self, verb, *rest, all=False):
        args = Namespace(verb=verb, card=list(rest), all=all, board=BOARD,
                         capsule_home=str(self.home), verdicts=str(self.verdicts))
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli.main(args)
        return rc, out.getvalue(), err.getvalue()

    def lines(self):
        return [json.loads(x) for x in self.verdicts.read_text().splitlines()]

    def test_pending_lists_flagged_items_only(self):
        self.record("r1", ["empty"])
        self.record("r2", [])
        rc, out, _ = self.call("pending")
        self.assertEqual(rc, 0)
        self.assertIn("t1 r1 empty Specs/a.md", out)
        self.assertIn("t1 r1 empty row 12", out)
        self.assertNotIn("r2", out)

    def test_verdict_appends_line_event_and_settles_item(self):
        self.record("r1", ["contradiction"])
        self.assertEqual(self.call("verdict", "r1", "Specs/a.md", "dropped", "wrong", "era")[0], 0)
        self.assertEqual(self.call("verdict", "r1", "12", "kept")[0], 0)
        self.assertEqual(self.call("verdict", "r1", "-", "missing", "Specs/z.md")[0], 0)
        first, second, third = self.lines()
        self.assertEqual((first["verdict"], first["path"], first["reason"]),
                         ("dropped", "Specs/a.md", "wrong era"))
        self.assertEqual(first["q"], "Card\n" + "b" * 1200)
        self.assertEqual((second["verdict"], second["row"]), ("kept", 12))
        self.assertEqual((third["verdict"], third["path"]), ("missing", "Specs/z.md"))
        rows = self.conn.execute("SELECT run_id, payload FROM task_events WHERE"
                                 " kind='vault_context_verdict'").fetchall()
        self.assertEqual(len(rows), 3)
        self.assertNotIn("r1", self.call("pending")[1])

    def test_bad_verdicts_write_nothing(self):
        self.record("r1", ["empty"])
        for bad in (("nope", "Specs/a.md", "kept"), ("r1", "Specs/x.md", "kept"),
                    ("r1", "Specs/a.md", "dropped"), ("r1", "Specs/a.md", "maybe")):
            self.assertNotEqual(self.call("verdict", *bad)[0], 0)
        self.assertFalse(self.verdicts.exists())
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM task_events").fetchone()[0], 0)

    def test_event_failure_writes_no_line(self):
        self.record("r1", ["empty"])
        with mock.patch.object(db, "event", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                self.call("verdict", "r1", "12", "kept")
        self.assertFalse(self.verdicts.exists())
        self.assertEqual(self.call("verdict", "r1", "12", "kept")[0], 0)

    def test_odd_run_ids_and_empty_items_refused(self):
        self.record("r1", ["empty"])
        for bad in (("../r1", "12", "kept"), ("/x/r1", "12", "kept"), ("r1", "-", "kept"),
                    ("r1", "", "dropped", "why")):
            self.assertNotEqual(self.call("verdict", *bad)[0], 0)
        self.assertFalse(self.verdicts.exists())

    def test_expired_is_unreviewed_and_feeds_nothing(self):
        self.record("r1", ["empty"], hours=49)
        self.assertNotIn("r1", self.call("pending")[1])
        self.assertIn("unreviewed", self.call("pending", all=True)[1])
        self.assertFalse(self.verdicts.exists())

    def test_backpressure_hides_sample_only(self):
        for i in range(21):
            self.record(f"s{i}", ["sample"])
        self.record("h", ["high_stakes"])
        out = self.call("pending")[1]
        self.assertIn("h high_stakes", out)
        self.assertNotIn(" sample ", out)
        self.assertIn(" sample ", self.call("pending", all=True)[1])
        for i in range(1, 21):   # settle all but one sample: the queue is under 10 again
            for item in ("Specs/a.md", "12"):
                self.call("verdict", f"s{i}", item, "kept")
        self.assertIn("s0 sample", self.call("pending")[1])

    def test_line_is_readable_by_vault_eval_parser(self):
        self.record("r1", ["empty"])
        self.call("verdict", "r1", "12", "kept")
        v = self.lines()[0]
        target = v.get("path") or (f"row {v['row']}" if v.get("row") else None)
        self.assertEqual((target, bool(v["q"])), ("row 12", True))


if __name__ == "__main__":
    unittest.main()
