# mutation-check: targets-predate-range ff625e94 -- fixture rename only, no behaviour pinned ::
#   Stalled.test_a_recent_ship_in_one_spool_does_not_mask_another_that_never_shipped
"""W208 intent: `lookout check` fails (one `job` event, exit 1, so job-failed pages) when shipping
has silently stalled: over LAG_MAX_BYTES unshipped, or bytes waiting with no good ship in
SILENCE_MAX, whether or not the config flag is on."""
import json
import time
import unittest

from capsule import lookout, lookout_ship
from capsule.tests import test_lookout_ship as ts


class Stalled(ts.ShipCase):
    def _check(self, nbytes, last_ago=None):
        d = lookout.lookout_dir(self.home)
        (d / "spool.jsonl").write_text("x" * nbytes)
        if last_ago is not None:
            last = {"ts": time.time() - last_ago, "rc": 0}
            (d / "ship.last.json").write_text(json.dumps(last))
        spool = self.home / "events.jsonl"
        rc = lookout_ship.check(self.home, {"board": "sample-app", "lookout": {"ship": False}},
                                events_spool=spool)
        lines = spool.read_text().splitlines() if spool.exists() else []
        return rc, [json.loads(line) for line in lines]

    def test_over_20_mib_unshipped_fails_with_one_job_event(self):
        rc, rows = self._check(lookout_ship.LAG_MAX_BYTES + 1, last_ago=60)
        self.assertEqual((rc, len(rows), rows[0]["payload"]["job"], rows[0]["payload"]["exit"]),
                         (1, 1, "lookout-check", 1))

    def test_bytes_waiting_with_the_last_ship_over_12h_ago_fail(self):
        self.assertEqual(self._check(100, last_ago=lookout_ship.SILENCE_MAX + 60)[0], 1)

    def test_bytes_waiting_and_never_shipped_fail(self):
        self.assertEqual(self._check(100)[0], 1)

    def test_a_recent_ship_in_one_spool_does_not_mask_another_that_never_shipped(self):
        board = lookout.lookout_dir(self.home, "sample-app")
        board.mkdir(parents=True)
        (board / "spool.jsonl").write_text("x" * 50)
        (board / "ship.last.json").write_text(json.dumps({"ts": time.time(), "rc": 0}))
        self.assertEqual(self._check(100, last_ago=None)[0], 1)

    def test_small_lag_with_a_recent_ship_is_skipped_not_failed(self):
        self.assertEqual(self._check(100, last_ago=60), (0, []))
        self.assertEqual(self.scripts(), [])                 # lag skips the Postgres comparison


if __name__ == "__main__":
    unittest.main()
