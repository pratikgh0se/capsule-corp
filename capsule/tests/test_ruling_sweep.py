# mutation-check: targets-predate-range b957739c9 -- old ask verb read from its constant ::
#   StuckRulingCards.test_a_ruling_stored_with_the_old_ask_verb_leaves_the_card_open
"""Tick-path test for capsule/ruling_sweep.py (t_99136481, review round 1 finding 1): the live
board's own stuck shape (t_e28654ce blocked `contradiction_loop`, ruling cards t_0086cfd4 and
t_1dd60f55 still `triage` past events 5280/5346's applied `document-wrong` rulings) reproduced
directly in a fresh board, then run through a real `tick.once()`."""
import json

from capsule import bundles, db
from capsule.tests.test_tick import TickCase


class StuckRulingCards(TickCase):
    def _stuck_ruling(self, n):
        """One contradiction/guide_ruling cycle on `self.tid`: a triage guide_ruling card the
        guide DID rule `document-wrong` on (mirrors events 5280/5346), left open the way the
        loop-cap branch used to leave it (returning before the ruling-card close)."""
        card = db.new_task(self.conn, title=f"guide_ruling: round {n}", status="triage",
                           request_needs="role:guide", parents=(self.tid,))
        cid = self.conn.execute(
            "INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?, 'contradiction', ?, 0)",
            (self.tid, json.dumps({"role": "reviewer", "ruling_card": card, "routed": "guide"}))).lastrowid
        db.event(self.conn, self.tid, "guide_ruling",
                 {"choice": "document-wrong", "contradiction": cid, "applied": True}, now=0)
        self.conn.commit()
        return card

    def test_stuck_ruling_cards_close_and_a_second_pass_is_a_no_op(self):
        card_a, card_b = self._stuck_ruling(1), self._stuck_ruling(2)
        self.conn.execute("UPDATE tasks SET status='blocked', block_kind='contradiction_loop' WHERE id=?",
                          (self.tid,))
        self.conn.commit()
        self.runner.up = False
        self.tick.once()
        status = lambda ids: {r["id"]: r["status"] for r in self.conn.execute(
            f"SELECT id, status FROM tasks WHERE id IN ({','.join('?' * len(ids))})", ids)}
        self.assertEqual(status([card_a, card_b]), {card_a: "done", card_b: "done"})
        parent = self.conn.execute("SELECT status, block_kind FROM tasks WHERE id=?", (self.tid,)).fetchone()
        self.assertEqual((parent["status"], parent["block_kind"]), ("blocked", "contradiction_loop"))
        self.tick.once()                                    # a second pass: still done, nothing re-closes
        self.assertEqual(status([card_a, card_b]), {card_a: "done", card_b: "done"})
        for card in (card_a, card_b):                       # C15b: the close is an event, once
            kinds = [r["kind"] for r in self.conn.execute(
                "SELECT kind FROM task_events WHERE task_id=?", (card,))]
            self.assertEqual(kinds.count("completed"), 1, kinds)

    def test_a_ruling_card_with_no_ruling_yet_is_left_alone(self):
        card = db.new_task(self.conn, title="guide_ruling: round 1", status="triage",
                           request_needs="role:guide", parents=(self.tid,))
        self.conn.execute(
            "INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?, 'contradiction', ?, 0)",
            (self.tid, json.dumps({"role": "reviewer", "ruling_card": card, "routed": "guide"})))
        self.conn.execute("UPDATE tasks SET status='triage', block_kind='guide_ruling' WHERE id=?", (self.tid,))
        self.conn.commit()
        self.runner.up = False
        self.tick.once()
        self.assertEqual(self.conn.execute("SELECT status FROM tasks WHERE id=?", (card,)).fetchone()["status"],
                         "triage")

    def test_a_ruling_stored_with_the_old_ask_verb_leaves_the_card_open(self):
        card = db.new_task(self.conn, title="guide_ruling: round 1", status="triage",
                           request_needs="role:guide", parents=(self.tid,))
        doc = json.dumps({"role": "reviewer", "ruling_card": card, "routed": "guide"})
        cid = self.conn.execute(
            "INSERT INTO task_events (task_id, kind, payload, created_at)"
            " VALUES (?, 'contradiction', ?, 0)", (self.tid, doc)).lastrowid
        db.event(self.conn, self.tid, "guide_ruling",
                 {"choice": bundles.LEGACY_ASK, "contradiction": cid, "applied": False}, now=0)
        self.conn.execute("UPDATE tasks SET status='triage', block_kind='guide_ruling'"
                          " WHERE id=?", (self.tid,))
        self.conn.commit()
        self.runner.up = False
        self.tick.once()
        row = self.conn.execute("SELECT status FROM tasks WHERE id=?", (card,)).fetchone()
        self.assertEqual(row["status"], "triage")
