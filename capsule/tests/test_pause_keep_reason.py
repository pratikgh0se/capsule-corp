"""A usage-limit pause (`Tick._pause`) must not erase a live human pause's reason and since:
`capsule unpark --reason X` matches on that reason (cli_ops._set_pause). Z1 intent: the human
pause with a reason survives a rate limit; Z2: unpark through the real CLI is the second oracle."""
import argparse
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from capsule import cli_ops, db, paths
from capsule.tick import Tick


class PauseKeepReasonTest(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.path = paths.board_db("b1", self.home)
        db.create_board(self.path)
        self.conn = db.connect(self.path)
        self.addCleanup(self.conn.close)
        self.now = int(time.time())
        self.tick = SimpleNamespace(cfg={"board": "b1"}, _paused=Tick._paused)

    def _limit(self, until, account="default"):
        run = {"account": account, "task_id": "t1"}
        return Tick._pause(self.tick, self.conn, run, until, self.now)

    def _held(self, key="pause:board"):
        return db.guard_get(self.conn, key)[0]

    def _unpark(self, reason):
        args = argparse.Namespace(
            cmd="unpark", reason=reason, seconds=0, capsule_home=str(self.home))
        self.assertEqual(cli_ops.handle(args, None), 0)

    def test_a_rate_limit_keeps_the_human_reason_and_since(self):
        since = self.now - 100
        db.guard_set(self.conn, "pause:board",
                     {"until": self.now + 600, "reason": "maintenance", "since": since})
        self._limit(self.now + 3600)
        self.assertEqual(
            self._held(), {"until": self.now + 3600, "reason": "maintenance", "since": since})

    def test_until_is_the_max_so_a_shorter_limit_does_not_shorten_the_pause(self):
        db.guard_set(self.conn, "pause:board",
                     {"until": self.now + 9000, "reason": "maintenance", "since": self.now - 5})
        self._limit(self.now + 60)
        self.assertEqual(self._held()["until"], self.now + 9000)
        self.assertEqual(self._held()["reason"], "maintenance")

    def test_the_account_pause_keeps_its_fields_too(self):
        db.guard_set(self.conn, "pause:account:default",
                     {"until": self.now + 10, "reason": "acct", "x": 1})
        self._limit(self.now + 500)
        self.assertEqual(self._held("pause:account:default"),
                         {"until": self.now + 500, "reason": "acct", "x": 1})

    def test_unpark_with_the_reason_still_lifts_it_after_a_rate_limit(self):
        db.guard_set(self.conn, "pause:board",
                     {"until": self.now + 600, "reason": "maintenance", "since": self.now - 100})
        self._limit(self.now + 3600)
        self._unpark("maintenance")
        self.assertEqual(self._held()["until"], 0)

    def test_an_expired_human_pause_loses_its_reason_and_unpark_leaves_the_limit(self):
        db.guard_set(self.conn, "pause:board",
                     {"until": self.now - 10, "reason": "maintenance", "since": self.now - 900})
        self._limit(self.now + 3600)
        self.assertEqual(self._held(), {"until": self.now + 3600})
        self._unpark("maintenance")
        self.assertEqual(self._held(), {"until": self.now + 3600})

    def test_a_lifted_human_pause_loses_its_reason_too(self):
        db.guard_set(self.conn, "pause:board", {"until": 0, "reason": "maintenance"})
        self._limit(self.now + 600)
        self.assertEqual(self._held(), {"until": self.now + 600})

    def test_no_prior_pause_writes_only_until(self):
        self._limit(self.now + 600)
        self.assertEqual(self._held(), {"until": self.now + 600})
        self.assertEqual(self._held("pause:account:default"), {"until": self.now + 600})


if __name__ == "__main__":
    unittest.main()
