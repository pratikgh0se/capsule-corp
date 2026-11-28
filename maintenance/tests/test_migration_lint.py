import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import migration_lint  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
MIGRATIONS_DIR = REPO_ROOT / "observability" / "postgres" / "migrations"


class TestMigrationLint(unittest.TestCase):
    def test_lint_flags_missing_down_and_bad_irreversible_ref(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "003-good.sql").write_text("-- down\nDROP TABLE foo;\n")
            (d / "004-irrev.sql").write_text("-- irreversible: row 127\nALTER TABLE foo ADD COLUMN x int;\n")
            (d / "005-bad.sql").write_text("ALTER TABLE foo ADD COLUMN y int;\n")
            (d / "006-bad-ref.sql").write_text("-- irreversible: because I said so\nALTER TABLE foo ADD COLUMN z int;\n")
            findings = migration_lint.lint(str(d))
            named = {f.split(":")[0] for f in findings}
            self.assertEqual(named, {"005-bad.sql", "006-bad-ref.sql"})

    def test_the_real_migrations_dir_is_clean(self):
        findings = migration_lint.lint(str(MIGRATIONS_DIR))
        self.assertEqual(findings, [], findings)


if __name__ == "__main__":
    unittest.main()
