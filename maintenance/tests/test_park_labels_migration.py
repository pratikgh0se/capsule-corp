"""t_853ff9d7: every park event the harness emits has its own words in the Postgres label table.
Oracle 1: the park kinds capsule/lookout.py treats as open blocks (BLOCK_OPEN).
Oracle 2: tests/test_migration_pg.sh applies 003+004 to real postgres:16-alpine."""
import re
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "maintenance"))
import migration_lint  # noqa: E402
from capsule import lookout  # noqa: E402

PG = REPO / "observability" / "postgres"
MIG = PG / "migrations" / "004-park-labels.sql"
PARKS = ("parked", "parked_by_human", "failure_limit")
ROW = r"\('([a-z_]+)', '((?:[^']|'')*)', '([a-z]+)'\)"


def rows(text):
    return {k: (w, a) for k, w, a in re.findall(ROW, text)}


class ParkLabels(unittest.TestCase):
    def test_harness_park_events_are_the_three_we_label(self):
        self.assertTrue(set(PARKS) <= set(lookout.BLOCK_OPEN))

    def test_all_three_labelled_across_003_and_004(self):
        seeded = rows((PG / "migrations" / "003-lookout.sql").read_text()) | rows(MIG.read_text())
        for kind in PARKS:
            self.assertIn(kind, seeded)
        self.assertEqual(len({seeded[k][0] for k in PARKS}), 3, "each park has its own words")

    def test_init_sql_carries_the_same_new_rows(self):
        init = rows((PG / "init.sql").read_text())
        for kind, row in rows(MIG.read_text()).items():
            self.assertEqual(init.get(kind), row)

    def test_migration_is_idempotent_and_lints(self):
        self.assertIn("on conflict (kind) do update", MIG.read_text())
        self.assertEqual(migration_lint.lint(MIG.parent), [])


if __name__ == "__main__":
    unittest.main()
