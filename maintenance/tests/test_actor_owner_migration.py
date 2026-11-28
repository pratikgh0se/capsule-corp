"""b114: the person's actor value is 'owner'; the legacy value stays readable, never written.
Oracle 1: the migration text (check keeps the legacy value, rows move to owner, no own
begin/commit since db_migrate wraps the file). Oracle 2: tests/test_migration_pg.sh applies it."""
import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "maintenance"))
import migration_lint  # noqa: E402
from capsule import fix_card_quotes, human  # noqa: E402

PG = REPO / "observability" / "postgres"
MIG = PG / "migrations" / "011-actor-owner.sql"


class ActorOwnerMigration(unittest.TestCase):
    def test_it_follows_010_in_the_migration_order(self):
        names = sorted(p.name for p in MIG.parent.glob("*.sql"))
        self.assertIn(MIG.name, names)
        self.assertLess(names.index("010-waiting-on-owner.sql"), names.index(MIG.name))

    def test_check_accepts_both_values_and_rows_move_to_owner(self):
        text = MIG.read_text()
        values = re.search(r"check \(actor in \(([^)]*)\)\)", text).group(1)
        self.assertIn("'owner'", values)
        self.assertIn(f"'{human.LEGACY_OWNER}'", values)
        self.assertIn("update cap_event_words set actor = 'owner' where actor = '", text)
        self.assertIn("where actor = '" + human.LEGACY_OWNER + "';", text)

    def test_no_own_transaction_because_the_migrator_supplies_it(self):
        self.assertIsNone(re.search(r"(?mi)^\s*(begin|commit)\b", MIG.read_text()))

    def test_init_sql_carries_the_same_statements(self):
        lines = MIG.read_text().splitlines(True)
        body = "".join(line for line in lines if not line.startswith("--"))
        self.assertIn(body, (PG / "init.sql").read_text())

    def test_writers_never_emit_the_legacy_value_and_readers_normalise_it(self):
        legacy = human.LEGACY_OWNER
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.dict(os.environ, {"CAPSULE_OWNER_NAME": legacy}):
            human._owner_name.cache_clear()
            try:
                path = human.write(Path(tmp), "b", "t_1", "unblock", "go", now=5, author=legacy)
            finally:
                human._owner_name.cache_clear()
            self.assertEqual(json.loads(path.read_text())["author"], "owner")
            done = Path(tmp) / "done"
            done.mkdir()
            (done / "1.json").write_text(json.dumps({"task_id": "t_1", "author": legacy}))
            self.assertIn("- author: owner\n", fix_card_quotes._entry(tmp, "1.json"))

    def test_lints_and_is_reversible_by_header(self):
        self.assertEqual(migration_lint.lint(MIG.parent), [])


if __name__ == "__main__":
    unittest.main()
