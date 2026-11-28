"""`capsule pause` / `unpark` against a held pause:board (C18)."""
import argparse
import tempfile
import time
import unittest
from pathlib import Path

from capsule import cli_ops, db, paths


class PauseHeldTest(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.path = paths.board_db("b1", self.home)
        db.create_board(self.path)

    def _held(self):
        conn = db.connect(self.path)
        try:
            return db.guard_get(conn, "pause:board")[0]
        finally:
            conn.close()

    def _seed(self, value):
        conn = db.connect(self.path)
        try:
            db.guard_set(conn, "pause:board", value)
        finally:
            conn.close()

    def _run(self, cmd, reason, seconds=60):
        args = argparse.Namespace(
            cmd=cmd, reason=reason, seconds=seconds, capsule_home=str(self.home))
        self.assertEqual(cli_ops.handle(args, None), 0)

    def test_shorter_pause_without_reason_is_replaced(self):
        now = int(time.time())
        self._seed({"until": now + 600})
        self._run("pause", "usage_guard", 18000)
        held = self._held()
        self.assertEqual(held["reason"], "usage_guard")
        self.assertAlmostEqual(held["until"], now + 18000, delta=5)

    def test_longer_pause_of_another_reason_stays(self):
        now = int(time.time())
        self._seed({"until": now + 90000, "reason": "incident"})
        self._run("pause", "usage_guard", 600)
        self.assertEqual(self._held(), {"until": now + 90000, "reason": "incident"})

    def test_incident_pause_is_never_replaced_or_lifted(self):
        seed = {"until": int(time.time()) + 315360000 - 10,
                "reason": "two_dispatchers", "incident": "i1"}
        self._seed(seed)
        self._run("pause", "claude logged out", 315360000)
        self.assertEqual(self._held(), seed)
        self._run("unpark", "claude logged out")
        self.assertEqual(self._held(), seed)

    def test_unpark_lifts_only_its_reason(self):
        now = int(time.time())
        self._seed({"until": now + 600, "reason": "incident"})
        self._run("unpark", "claude logged out")
        self.assertEqual(self._held()["until"], now + 600)
        self._run("unpark", "incident")
        self.assertEqual(self._held()["until"], 0)


    def test_card_history_prints_why_a_card_was_parked(self):
        import contextlib
        import io
        from capsule import blocks
        conn = db.connect(self.path)
        tid = db.new_task(conn, title="t")
        blocks.park(conn, tid, "waiting on the vendor")
        conn.close()
        out = io.StringIO()
        args = argparse.Namespace(cmd="card", board="b1", task=tid, verb="history",
                                  capsule_home=str(self.home))
        with contextlib.redirect_stdout(out):
            self.assertEqual(cli_ops.handle(args, None), 0)
        self.assertIn("parked_by_human waiting on the vendor", out.getvalue())


if __name__ == "__main__":
    unittest.main()
