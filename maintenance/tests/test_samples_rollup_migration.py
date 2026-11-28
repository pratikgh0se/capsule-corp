"""D255: samples_hourly rollup. Oracle 1: the SQL text of 009 and the probe upsert agree with the
columns the weekly dashboard reads. Oracle 2: tests/test_migration_pg.sh applies 009 to real
postgres (Docker-gated)."""
import re
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "maintenance"))
import migration_lint  # noqa: E402
import obs_probe_ship  # noqa: E402

PG = REPO / "observability" / "postgres"
MIG = PG / "migrations" / "009-samples-rollup.sql"


def table_ddl(text):
    return re.search(r"create table if not exists samples_hourly \(.*?\n\);", text, re.S).group(0)


class RollupMigration(unittest.TestCase):
    def test_lints_and_has_a_down_line(self):
        self.assertEqual(migration_lint.lint(str(MIG.parent)), [])
        self.assertTrue(MIG.read_text().startswith("-- down: drop table if exists samples_hourly"))

    def test_table_keys_and_stats(self):
        ddl = table_ddl(MIG.read_text())
        for col in ("bucket", "service", "name", "avg_value", "min_value", "max_value", "n",
                    "total_avg", "ticks"):
            self.assertRegex(ddl, r"\n  %s " % col)
        self.assertIn("primary key (service, name, bucket)", ddl)
        self.assertNotIn("labels", ddl)

    def test_init_sql_mirrors_the_table(self):
        self.assertEqual(table_ddl((PG / "init.sql").read_text()), table_ddl(MIG.read_text()))

    def test_backfill_is_idempotent_and_covers_all_rows(self):
        m = MIG.read_text()
        backfill = m[m.index("insert into samples_hourly"):]
        self.assertIn("on conflict (service, name, bucket) do update", backfill)
        self.assertIn("avg(s),\n       count(*)", backfill)
        self.assertIn("ticks = excluded.ticks", backfill)
        self.assertIn("group by 1, 2, 3) t", backfill.replace("\n      from samples ", " "))
        self.assertNotIn("where", backfill.lower().split("on conflict")[0])

    def test_no_second_index_on_samples(self):
        self.assertNotRegex(MIG.read_text().lower(), r"create index")


class ProbeUpsert(unittest.TestCase):
    def test_upsert_matches_the_table_columns(self):
        sql = obs_probe_ship.ROLLUP_SQL
        self.assertIn("(bucket, service, name, avg_value, min_value, max_value, n, "
                      "total_avg, ticks)", sql)
        self.assertIn("GROUP BY 1, 2, 3", sql)
        self.assertNotIn("labels", sql)
        self.assertIn("avg(s), count(*) FROM", sql)
        self.assertIn("ticks = excluded.ticks", sql)
        self.assertIn("FROM (SELECT ts, service, name, sum(value) s", sql)
        self.assertIn("total_avg = excluded.total_avg", sql)


if __name__ == "__main__":
    unittest.main()
