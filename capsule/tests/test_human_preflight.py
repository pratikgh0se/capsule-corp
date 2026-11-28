import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from capsule import blocks, db, human, paths, spool
from capsule.__main__ import main


class ParkedCard(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.board = paths.board_db("test", self.home)
        db.create_board(self.board)
        self.conn = db.connect(self.board)
        self.tid = db.new_task(self.conn, title="fix card", body="Implements §6.8.",
                               assignee="dev-frontend", branch="b")

    def files(self):
        d = spool.human_dir(self.home, "test")
        return sorted(p.name for p in d.iterdir()) if d.is_dir() else []

    def run_cli(self, kind, task=None, board="test", author="cos"):
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            rc = main(["human", board, task or self.tid, kind, "why", "--author", author,
                       "--capsule-home", str(self.home)])
        return rc, err.getvalue()

    def park(self):
        self.assertTrue(blocks.park(self.conn, self.tid, "reset later"))

    def test_cli_unblock_of_a_parked_card_refuses_names_reset_and_writes_no_file(self):
        self.park()
        before = self.files()
        rc, err = self.run_cli("unblock")
        self.assertEqual(rc, 1)
        self.assertIn("reset", err)
        self.assertEqual(self.files(), before)

    def test_unblock_and_accept_refuse_for_both_authors(self):
        self.park()
        for kind in ("unblock", "accept"):
            for author in ("cos", "owner"):
                with mock.patch.object(human, "_ttys", return_value=True):    # owner at a terminal
                    rc, err = self.run_cli(kind, author=author)
                self.assertEqual(rc, 1, (kind, author))
                self.assertIn("reset", err)
        self.assertEqual(self.files(), [])

    def test_reset_of_a_parked_card_writes_its_file(self):
        self.park()
        rc, _ = self.run_cli("reset")
        self.assertEqual((rc, len(self.files())), (0, 1))

    def test_unblock_of_another_hold_writes_its_file(self):
        self.conn.execute("UPDATE tasks SET status='blocked', block_kind='needs_input' "
                          "WHERE id=?", (self.tid,))
        rc, _ = self.run_cli("unblock")
        self.assertEqual((rc, len(self.files())), (0, 1))

    def test_missing_database_or_card_writes_its_file(self):
        rc, _ = self.run_cli("unblock", task="t_missing")
        self.assertEqual((rc, len(self.files())), (0, 1))
        rc, _ = self.run_cli("unblock", board="noboard")
        self.assertEqual(rc, 0)
        self.assertEqual(len(list(spool.human_dir(self.home, "noboard").iterdir())), 1)

    def test_check_writes_no_event(self):
        self.park()
        sql = "SELECT COUNT(*) FROM task_events"
        before = self.conn.execute(sql).fetchone()[0]
        self.run_cli("unblock")
        self.assertEqual(self.conn.execute(sql).fetchone()[0], before)


if __name__ == "__main__":
    unittest.main()
