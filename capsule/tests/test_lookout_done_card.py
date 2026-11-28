"""W361: a card that went done with its block event still open must not wait on the owner."""
import unittest

from capsule import db
from capsule.tests import test_lookout as tl   # module import: its cases are not re-collected


class DoneCard(tl.Board):
    def card(self, tid):
        return [s["row"] for s in self.snaps("cap_cards") if s["row"]["task_id"] == tid][-1]

    def test_done_card_with_an_unclosed_block_waits_on_nobody(self):
        tid = db.new_task(self.conn, title="t", branch="epic/screens")
        self.block(tid, "needs_input")
        with db.txn(self.conn):
            self.conn.execute("UPDATE tasks SET status='done' WHERE id=?", (tid,))
        self.collect()
        c = self.card(tid)
        self.assertEqual((c["phase"], c["waiting_on"], c["waiting_words"], c["block_kind"]),
                         ("done", None, None, None))

    def test_live_blocked_card_still_waits_on_owner(self):
        tid = db.new_task(self.conn, title="t", branch="epic/screens")
        self.block(tid, "needs_input")
        self.collect()
        c = self.card(tid)
        self.assertEqual((c["phase"], c["waiting_on"]), ("waiting", "owner"))


if __name__ == "__main__":
    unittest.main()
