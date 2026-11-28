# mutation-check: targets-predate-range b957739c9 -- rename only, no behaviour pinned ::
#   CloseStale.test_apply_appends_one_close_per_key_and_needs_me_empties
"""Task 9 (The Lookout): close the stale legacy-era "needs me" opens (spec 9.2). Oracle 1: the rows
this script appends. Oracle 2: events.needs_me() (the existing open-minus-closed reader) over the
same spool returns nothing for those keys afterwards."""
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from capsule.tests import tmpclean  # noqa: E402
tmpclean.install()

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "maintenance"))
import events  # noqa: E402
import lookout_close_stale as lcs  # noqa: E402
from capsule import legacy_names as _LN  # noqa: E402

ROWS = "t_aaa|my-app|2026-09-14 10:00:00+00|stuck\nepic-x|my-app|2026-09-20 10:00:00+00|decision\n"


def fake_psql(sql):
    fake_psql.seen = sql
    return 0, ROWS


class CloseStale(unittest.TestCase):
    def setUp(self):
        self.spool = Path(tempfile.mkdtemp()) / "events.jsonl"

    def test_dry_run_prints_and_writes_nothing(self):
        self.assertEqual(lcs.main([], psql=fake_psql, spool=self.spool), 0)
        self.assertFalse(self.spool.exists())
        self.assertIn("< '2026-09-26'", fake_psql.seen)
        self.assertIn("source not in ('capsule', 'lookout-publisher', 'lookout-migration')", fake_psql.seen)

    def test_apply_appends_one_close_per_key_and_needs_me_empties(self):
        # the opens as the old bridge wrote them, then the script's closes
        events.log("card", "bridge", "stuck", board="my-app", task_id="t_aaa",
                   payload={"event": "stuck", "state": "open"}, spool=self.spool)
        events.log("card", "bridge", "decide", board="my-app", slug="epic-x",
                   payload={"event": "decision", "state": "open"}, spool=self.spool)
        self.assertEqual(len(events.needs_me(events.read(self.spool))), 2)
        self.assertEqual(lcs.main(["--apply"], psql=fake_psql, spool=self.spool), 0)
        rows = events.read(self.spool)[2:]
        self.assertEqual([(r["source"], r["payload"]["event"], r["payload"]["state"]) for r in rows],
                         [("lookout-migration", "closed_stale", "closed")] * 2)
        self.assertEqual(rows[0]["task_id"], "t_aaa")
        self.assertEqual(rows[1]["slug"], "epic-x")
        self.assertEqual(rows[0]["payload"]["reason"], f"{_LN.NAME} era, superseded by capsule")
        self.assertEqual(events.needs_me(events.read(self.spool)), [])

    def test_psql_failure_is_exit_2_and_writes_nothing(self):
        self.assertEqual(lcs.main(["--apply"], psql=lambda sql: (1, "down"), spool=self.spool), 2)
        self.assertFalse(self.spool.exists())


if __name__ == "__main__":
    unittest.main()
