"""`landing._seen` and the `lander.landed:<task>` guard: the guard answers one completion (the
landed sha, or the card commit it replayed) whatever its state, so a red, reverted or refused
landing of that same commit is never retried by the tick; a new completion is checked again."""
import tempfile
import unittest
from pathlib import Path

from capsule import db, landing, landing_seen
from capsule.db import guard_set

REPO, BRANCH = "/nonexistent/repo", "capsule/t_x"
LANDED_SHA, CARD_SHA, REWORK_SHA = "a" * 40, "b" * 40, "c" * 40
STATES = ("pushed", "verified", "reverted", "revert_failed", "closed_red", "verify_refused",
          "ci_unverified", "revert_owed")


class SeenGuard(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        board = Path(tmp.name) / "kanban.db"
        db.create_board(board)
        self.conn = db.connect(board)
        self.addCleanup(self.conn.close)

    def seen(self, sha):
        return landing._seen(self.conn, "land", "t_x", sha, REPO, BRANCH)

    def test_the_answered_completion_is_seen_in_every_guard_state(self):
        for state in STATES:
            guard_set(self.conn, landing.LANDED + "t_x",
                      {"state": state, "sha": LANDED_SHA, "card": CARD_SHA}, now=1)
            self.assertEqual((state, self.seen(LANDED_SHA), self.seen(CARD_SHA)),
                             (state, True, True))

    def test_a_new_completion_is_not_seen_in_any_guard_state(self):
        for state in STATES:
            guard_set(self.conn, landing.LANDED + "t_x",
                      {"state": state, "sha": LANDED_SHA, "card": CARD_SHA}, now=1)
            self.assertEqual((state, self.seen(REWORK_SHA)), (state, False))

    def test_landing_re_exports_the_names_that_moved_to_landing_seen(self):
        self.assertIs(landing._seen, landing_seen._seen)
        self.assertIs(landing._candidates, landing_seen._candidates)
        self.assertIs(landing._flag_reverted, landing_seen._flag_reverted)
        self.assertIs(landing._out, landing_seen._out)
        self.assertIs(landing.WINDOW, landing_seen.WINDOW)


if __name__ == "__main__":
    unittest.main()
