"""Task 9b (The Lookout): retention (spec 5.11). Oracle 1: the SQL and the cleanup.sh wiring.
Oracle 2: tests/test_migration_pg.sh seeds rows at 13/15/29/31/179/181 days and runs retention.sql."""
import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


class Retention(unittest.TestCase):
    def test_three_horizons(self):
        sql = (REPO / "maintenance" / "retention.sql").read_text()
        got = re.findall(r"^delete from events where (.+?) and ts < now\(\) - interval '(\d+) days';$", sql, re.M)
        self.assertEqual(got, [("kind = 'card'", "180"),
                               ("kind not in ('card', 'health', 'tick_health')", "30")])
        self.assertIn("delete from events e where e.kind in ('health', 'tick_health') "
                      "and e.ts < now() - interval '14 days' and ", sql)
        self.assertNotRegex(sql, r"(?i)delete from (cap_|prices|samples)")

    def test_file_holds_only_the_three_statements(self):
        """M1 (row 478 b review): test_three_horizons's findall() only checks the lines
        it matches, so a stray `truncate events;` or an unbounded `delete` appended
        anywhere in the file would survive that test unnoticed - only the pg oracle
        (tests/test_migration_pg.sh, SKIP in an agent session) would catch it. This is
        a closure check: every non-comment, non-blank line must be one of the three
        bounded deletes, and there must be exactly three of them."""
        sql = (REPO / "maintenance" / "retention.sql").read_text()
        stmt_lines = [ln for ln in sql.splitlines() if ln.strip() and not ln.strip().startswith("--")]
        self.assertEqual(len(stmt_lines), 3, f"expected exactly 3 statements, got: {stmt_lines}")
        for ln in stmt_lines:
            self.assertRegex(ln, r"^(with keep as materialized \(.+\) )?"
                 r"delete from events (e )?where .+ interval '\d+ days'"
                 r"( and \(e\.kind = 'health' or .+\))?;$")

    def test_each_boards_latest_tick_health_is_kept(self):
        """TEXT PIN ONLY (the pg oracle in tests/test_migration_pg.sh is the behaviour check, and
        skips in an agent session). A dead tick's board is picked by capsule-tick-deadman from its
        LATEST tick_health row; deleting that row after 14 days would drop the board from the
        watched set. The newest row per board is computed once (materialized), not per row."""
        sql = (REPO / "maintenance" / "retention.sql").read_text()
        self.assertIn("with keep as materialized (select board, max(ts) as last from events "
                      "where kind = 'tick_health' group by board) delete from events e", sql)
        self.assertIn("and (e.kind = 'health' or e.ts < (select k.last from keep k "
                      "where k.board is not distinct from e.board));", sql)

    def test_cleanup_applies_the_file_not_an_inline_delete(self):
        sh = (REPO / "maintenance" / "cleanup.sh").read_text()
        self.assertIn('-f - < "$(dirname "$0")/retention.sql"', sh)
        self.assertNotIn("delete from events where ts < now() - interval '30 days'", sh)

    def test_lookout_logs_are_trimmed(self):
        sh = (REPO / "maintenance" / "cleanup.sh").read_text()
        self.assertIn("lookout/publish.log", sh)
        self.assertIn('trim_inplace "$lf" 10000', sh)

    def test_retention_doc_names_the_three_classes(self):
        doc = (REPO / "docs" / "RETENTION.md").read_text()
        self.assertIn("card 180 d, health 14 d, every other kind 30 d", doc)


if __name__ == "__main__":
    unittest.main()
