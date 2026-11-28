"""`capsule unblocked` on a weekly-held board: ready cards are `weekly-hold`, never unowned."""
import tempfile
import unittest
from pathlib import Path

from capsule import db, unblocked
from capsule.tests.test_unblocked import EPIC, caps


def _create_board(path):
    """A Capsule board db plus the `config.json` that makes `capsule unblocked` read it (D244)."""
    db.create_board(path)
    cfg = path.parent / "config.json"
    if not cfg.exists():
        cfg.write_text("{}")


class WeeklyHold(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp()) / "home"
        path = self.home / "boards" / "sample-board" / "kanban.db"
        path.parent.mkdir(parents=True)
        _create_board(path)
        self.conn = db.connect(path)
        db.new_task(self.conn, title="card 7", status="running", branch=EPIC)
        db.new_task(self.conn, title="ready on epic", status="ready", branch=EPIC)

    def tearDown(self):
        self.conn.close()

    def items(self, value):
        self.conn.execute("DELETE FROM guard_state WHERE key='weekly_pause'")
        if value is not None:
            self.conn.execute("INSERT INTO guard_state(key, value, version, updated_at)"
                              " VALUES('weekly_pause', ?, 1, 0)", (value,))
        self.conn.commit()
        return unblocked.board_items(
            "sample-board", capsule_home=self.home, now=2000,
            run_status_by_commit={}, caps=caps(1, [EPIC]))

    def test_held_board_blocks_ready_card_as_weekly_hold(self):
        items = self.items('{"held": true, "pct": 93}')
        self.assertEqual([(i.state, i.blocker, i.owner) for i in items],
                         [("blocked", "weekly-hold", "")])
        self.assertEqual(unblocked.summarize(items), (0, 0))

    def test_not_held_or_garbled_guard_is_unchanged(self):
        for value in ('{"held": false}', "not json {", "[1]", "null", None):
            with self.subTest(value=value):
                self.assertEqual([i.blocker for i in self.items(value)], ["unassigned"])


if __name__ == "__main__":
    unittest.main()
