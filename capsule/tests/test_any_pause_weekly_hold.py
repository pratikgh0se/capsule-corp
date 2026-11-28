"""`dispatch.any_pause` counts a held `weekly_pause` guard (D234), so the tick heartbeat's `paused`
and the idle-lane rule read a held factory as a deliberate stop. Oracle 1: held -> True with and
without `checks`, and `idle_lane.find` is quiet. Oracle 2: not held, garbled or missing -> False."""
import tempfile
import unittest
from pathlib import Path

from capsule import db, dispatch, idle_lane, paths
from capsule.tests.test_idle_lane import BOARD, NOW, fixture


def _conn():
    p = Path(tempfile.mkdtemp()) / "board.db"
    db.create_board(p)
    return db.connect(p)


def _raw(conn, value):
    conn.execute("INSERT OR REPLACE INTO guard_state (key, value, version, updated_at)"
                 " VALUES ('weekly_pause', ?, 1, 0)", (value,))


class AnyPauseWeeklyHold(unittest.TestCase):
    def test_held_counts_with_and_without_checks(self):
        conn = _conn()
        db.guard_set(conn, "weekly_pause", {"held": True}, now=NOW)
        self.assertTrue(dispatch.any_pause(conn, NOW))
        self.assertTrue(dispatch.any_pause(conn, NOW, checks=True))

    def test_not_held_missing_or_garbled_is_unchanged(self):
        conn = _conn()
        self.assertFalse(dispatch.any_pause(conn, NOW, checks=True))
        db.guard_set(conn, "weekly_pause", {"held": False}, now=NOW)
        self.assertFalse(dispatch.any_pause(conn, NOW))
        for raw in ("not json{", "[1, 2]", "null", '"held"', "7"):
            _raw(conn, raw)
            self.assertFalse(dispatch.any_pause(conn, NOW, checks=True), raw)

    def test_account_pause_still_counts_beside_a_not_held_weekly_row(self):
        conn = _conn()
        db.guard_set(conn, "weekly_pause", {"held": False}, now=NOW)
        db.guard_set(conn, "pause:account:x", {"until": NOW + 60}, now=NOW)
        self.assertTrue(dispatch.any_pause(conn, NOW, checks=True))

    def test_idle_lane_is_quiet_while_held(self):
        home = fixture()[0]
        conn = db.connect(paths.board_db(BOARD, home))
        self.assertIsNotNone(idle_lane.find(conn, NOW))
        db.guard_set(conn, "weekly_pause", {"held": True}, now=NOW)
        self.assertIsNone(idle_lane.find(conn, NOW))


if __name__ == "__main__":
    unittest.main()
