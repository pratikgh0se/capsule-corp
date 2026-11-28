# mutation-check: targets-predate-range 53e8d2cb8 -- recurrence test for inc_85fada36; the fix
#   predates the range :: RoutedUnblock.test_cos_unblock_of_a_routed_fix_card_opens_no_incident,
#   RoutedUnblock.test_the_nudge_sweep_counts_a_routed_fix_card_unblock_as_routine
"""inc_85fada36: a CoS unblock of a Korin fix card hold, after `fix_card_routed`, opens no
incident. Any other unblock of a hold still opens one."""
import shutil
import tempfile
import unittest
from pathlib import Path

from capsule import db, incidents, routine_nudges

T0 = 1_790_000_000
KORIN_HOLD = {"kind": "needs_input", "incident": "inc_x",
              "reason": "Korin fix card for incident inc_x: unblock it to dispatch"}


class RoutedUnblock(unittest.TestCase):
    def setUp(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        db.create_board(tmp / "kanban.db")
        self.conn = db.connect(tmp / "kanban.db")
        self.addCleanup(self.conn.close)
        incidents.from_events(self.conn, board="b", now=T0)   # sets the watermark

    def unblock(self, author, hold, n):
        """A fresh card: hold, `fix_card_routed`, then `author`'s unblock. Incidents opened."""
        tid = db.new_task(self.conn, title=f"c{n}", assignee="dev-frontend", branch="epic/x")
        for kind, payload in (("blocked", hold), ("fix_card_routed", {"incident": "inc_x"}),
                              ("intervention", {"kind": "unblock", "file": f"u{n}.json",
                                                "src": f"u{n}.json", "author": author})):
            db.event(self.conn, tid, kind, payload, now=T0)
        return incidents.from_events(self.conn, board="b", now=T0)

    def test_cos_unblock_of_a_routed_fix_card_opens_no_incident(self):
        self.assertEqual(self.unblock("cos", KORIN_HOLD, 1), [])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM incidents").fetchone()[0], 0)
        self.assertEqual(len(self.unblock("owner", KORIN_HOLD, 2)), 1)
        other = dict(KORIN_HOLD, reason="a question for the owner")
        self.assertEqual(len(self.unblock("cos", other, 3)), 1)

    def test_the_nudge_sweep_counts_a_routed_fix_card_unblock_as_routine(self):
        """The fix: `is_routine_nudge` (the sweep of open incidents) uses the classifier rule."""
        def opened(author, n):
            self.unblock(author, KORIN_HOLD, n)
            eid, task = self.conn.execute("SELECT id, task_id FROM task_events"
                                          " WHERE kind='intervention' ORDER BY id DESC").fetchone()
            inc, _ = incidents.open_incident(
                self.conn, board="b", kind="human_intervention", key=f"unblock: {eid}",
                evidence=[f"event:{eid}"], task_id=task, now=T0)
            return incidents.get(self.conn, inc)

        self.assertTrue(routine_nudges.is_routine_nudge(self.conn, opened("cos", 1)))
        self.assertFalse(routine_nudges.is_routine_nudge(self.conn, opened("owner", 2)))
