import json
import time
import unittest

from capsule import db, lookout


WANT = ("waiting", "owner", "blocked, no retry scheduled: unblock|reset")


def collect_cards(tc):
    """rq_07ad0af5, run end to end through collect() by test_lookout's Z1 test (`tc` is its Board
    case): a transient block reads as waiting on the owner with and without a board pause, because
    nothing unblocks it when the pause lapses. Returns each card's (phase, waiting_on, words)."""
    got = []
    for pause in (False, True):
        tid = db.new_task(tc.conn, title=f"wait {pause}", branch="epic/screens")
        tc.block(tid, "transient", reason="behind main")
        if pause:
            with db.txn(tc.conn):
                tc.conn.execute("INSERT INTO guard_state (key, value, version, updated_at)"
                                " VALUES ('pause:board', ?, 1, 0)",
                                (json.dumps({"until": time.time() + 3600}),))
        tc.collect()
        card = [s["row"] for s in tc.snaps("cap_cards") if s["row"]["task_id"] == tid][-1]
        got.append((card["phase"], card["waiting_on"], card["waiting_words"]))
    return got


class TransientWaiting(unittest.TestCase):
    def test_block_needs_action_with_or_without_a_pause(self):
        until = 1790000000
        for pause in (None, 0, until):
            for args in (("transient", "transient", "blocked", False, None, pause),
                         (None, "transient", "blocked", False, None, pause)):
                with self.subTest(args=args):
                    who, words = lookout.derive_waiting(*args)
                    self.assertEqual(who, "owner")
                    self.assertIn("unblock|reset", words)
                    self.assertNotIn("retrying automatically", words)


if __name__ == "__main__":
    unittest.main()
