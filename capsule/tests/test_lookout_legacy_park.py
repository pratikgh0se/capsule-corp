"""t_3e108a93: pre-rename `parked` events still read right on the live board."""
import unittest

from capsule import db, lookout
from capsule.tests.test_lookout import Board


class LegacyPark(Board):
    def test_legacy_parked_event_by_human_still_reads_as_a_human_park(self):
        """t_3e108a93: rows written before the `parked` -> `parked_by_human` rename must keep
        their open block row through a full resync, with the human words (not failure-limit)."""
        tid = db.new_task(self.conn, title="old", branch="epic/screens")
        db.event(self.conn, tid, "parked", {"by": "human", "trigger": "legacy hold"})
        self.collect()
        self.col.sent = {}
        self.collect()
        rows = [s["row"] for s in self.snaps("cap_blocks") if s["row"]["task_id"] == tid]
        self.assertTrue(rows)
        self.assertIsNone(rows[-1]["closed_at"])
        card = [s["row"] for s in self.snaps("cap_cards") if s["row"]["task_id"] == tid][-1]
        self.assertEqual(card["waiting_on"], "owner")
        self.assertEqual(card["waiting_words"], "you parked it: legacy hold")
        # the new name keeps working beside the legacy one
        new = db.new_task(self.conn, title="new", branch="epic/screens")
        db.event(self.conn, new, "parked_by_human", {"trigger": "fresh hold"})
        self.collect()
        card = [s["row"] for s in self.snaps("cap_cards") if s["row"]["task_id"] == new][-1]
        self.assertEqual((card["block_kind"], card["waiting_words"]),
                         ("parked_by_human", "you parked it: fresh hold"))

    def test_legacy_parked_event_without_by_keeps_failure_words(self):
        tid = db.new_task(self.conn, title="old", branch="epic/screens")
        db.event(self.conn, tid, "parked", {"reason": "tests failed"})
        self.collect()
        card = [s["row"] for s in self.snaps("cap_cards") if s["row"]["task_id"] == tid][-1]
        self.assertEqual(card["waiting_words"], "failed too often, reset it")

    def test_a_full_resync_reships_an_idle_waiting_card_as_owner(self):
        tid = db.new_task(self.conn, title="idle", branch="epic/screens")
        db.event(self.conn, tid, "parked_by_human", {"trigger": "hold"})

        def shipped():
            return [s["row"] for s in self.snaps("cap_cards") if s["row"]["task_id"] == tid]
        self.collect()
        self.assertEqual(len(shipped()), 1)
        self.collect()   # nothing touched the card: an ordinary pass ships nothing for it
        self.assertEqual(len(shipped()), 1)
        self.col.sent[("cap_cards", tid)] = "stored-before-the-rename"
        self.col.passes = lookout.RESYNC_EVERY - 1   # the next pass is the hourly resync
        self.collect()
        rows = shipped()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[-1]["waiting_on"], "owner")


if __name__ == "__main__":
    unittest.main()
