"""t_89d97bec: a card a person closed stays `done` and keeps its old `completed` event, so every
reader that counts "done + completed" as landed needs the closed-after check (`blocks.was_closed`
for one card, `blocks.not_closed_after` in SQL). One case per place: outcomes, touches, cost and
the incident gate each get a card with an old `completed` event and a later `closed` one."""
import tempfile
import unittest
from pathlib import Path

from capsule import blocks, cost, db, incidents, outcomes, touches
from capsule.tests import tmpclean

T0 = 1_700_000_000


class ClosedAfterCompleted(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(tmpclean.sweep, self.dir)
        db.create_board(self.dir / "kanban.db")
        self.conn = db.connect(self.dir / "kanban.db")
        self.addCleanup(self.conn.close)

    def card(self, closed, again=False):
        """A `done` card with a `completed` event at T0, then (if `closed`) a person's close, then
        (if `again`) a second completion."""
        tid = db.new_task(self.conn, title="card", status="done")
        db.event(self.conn, tid, "completed", {"commit": "old", "gates": {"ok": True}}, now=T0)
        if closed:
            db.event(self.conn, tid, "closed", {"by": "human", "reason": "landed by hand"},
                     now=T0 + 10)
        if again:
            db.event(self.conn, tid, "completed", {"commit": "new", "gates": {"ok": True}},
                     now=T0 + 20)
        self.conn.execute("UPDATE tasks SET completed_at=? WHERE id=?", (T0 + 5, tid))
        return tid

    def test_outcomes_does_not_count_a_closed_card_with_its_old_commit(self):
        shut, open_, redone = self.card(True), self.card(False), self.card(True, again=True)
        landed = outcomes._landed(self.conn)
        self.assertNotIn(shut, landed)
        self.assertEqual(landed[open_][1], "old")
        self.assertEqual(landed[redone][1], "new")        # completed again after the close

    def test_touches_does_not_count_a_closed_card_as_landed(self):
        self.card(True)
        doc = touches._add({"cards": []}, self.conn, self.dir, "b", T0 - 1, None)
        self.assertEqual(doc["landed_cards"], 0)
        self.card(False)
        doc = touches._add({"cards": []}, self.conn, self.dir, "b", T0 - 1, None)
        self.assertEqual(doc["landed_cards"], 1)

    def test_cost_does_not_count_a_closed_card_as_landed(self):
        shut, open_ = self.card(True), self.card(False)
        ids = [r[0] for r in self.conn.execute(cost._LANDED, ("none", 20))]
        self.assertEqual(ids, [open_])
        self.assertNotIn(shut, ids)

    def test_the_incident_gate_does_not_pass_a_closed_fix_card_on_its_old_completion(self):
        shut, open_, redone = self.card(True), self.card(False), self.card(True, again=True)
        status = incidents.fix_card_status
        self.assertEqual(status(self.conn, self.dir, shut), ("done", False))
        self.assertEqual(status(self.conn, self.dir, open_), ("done", True))
        self.assertEqual(status(self.conn, self.dir, redone), ("done", True))

    def test_one_card_helper_agrees_with_the_sql(self):
        shut = self.card(True)
        self.assertTrue(blocks.was_closed(self.conn, shut))
        sql = ("SELECT COUNT(*) FROM tasks t JOIN task_events e ON e.task_id=t.id AND "
               f"e.kind='completed' WHERE t.id=? AND {blocks.not_closed_after('t.id', 'e.id')}")
        self.assertEqual(self.conn.execute(sql, (shut,)).fetchone()[0], 0)
