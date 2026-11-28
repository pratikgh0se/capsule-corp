"""Z1/Z2 for the mutation gate's environment precheck (card t_589d9eff): when the interpreter
cannot import a package the project ships, main() exits 75 (tempfail) with one message instead of
printing a table of ERROR rows; a package the change itself broke stays red (1).
Fixture repos are built fresh in tempfile; helpers come from test_mutation_check."""
import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mutation_check as mc  # noqa: E402
import mutation_env  # noqa: E402
from test_mutation_check import (  # noqa: E402
    DISCRIMINATING_TEST, PLACEHOLDER_TEST, PROD_BUGGY, PROD_FIXED, commit_all, init_repo,
    write_empty_baseline, write_test)


def make_range(root, packages):
    """A repo with `packages` ({dir: __init__.py source}) and one fix-plus-test range."""
    init_repo(root)
    write_empty_baseline(root)
    for name, src in packages.items():
        (root / name).mkdir()
        (root / name / "__init__.py").write_text(src)
    (root / "production.py").write_text(PROD_BUGGY)
    write_test(root, PLACEHOLDER_TEST)
    base = commit_all(root, "base: buggy add()")
    (root / "production.py").write_text(PROD_FIXED)
    write_test(root, DISCRIMINATING_TEST)
    return base, commit_all(root, "fix add(), cover it")


def run_main(root, base, head):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = mc.main(["--base", base, "--head", head, "--repo", str(root)])
    return rc, buf.getvalue()


class EnvironmentPrecheck(unittest.TestCase):
    def test_an_unimportable_package_exits_75_with_one_message(self):
        with tempfile.TemporaryDirectory(prefix="mc-env-") as tmp:
            root = Path(tmp)
            base, head = make_range(root, {"brokenpkg": "import no_such_dependency_xyz\n"})
            rc, out = run_main(root, base, head)
        self.assertEqual(rc, 75, out)
        self.assertIn("environment error: cannot import brokenpkg", out)
        self.assertNotIn("| test file |", out)
        self.assertNotIn("ERROR", out)

    def test_an_importable_package_proceeds_to_the_table(self):
        with tempfile.TemporaryDirectory(prefix="mc-env-") as tmp:
            root = Path(tmp)
            base, head = make_range(root, {"okpkg": "VALUE = 1\n"})
            rc, out = run_main(root, base, head)
        self.assertEqual(rc, 0, out)
        self.assertIn("| test file |", out)
        self.assertNotIn("environment error", out)

    def test_a_project_with_no_packages_has_nothing_to_import(self):
        with tempfile.TemporaryDirectory(prefix="mc-env-") as tmp:
            root = Path(tmp)
            make_range(root, {})
            self.assertEqual(mutation_env.refuse(root, "HEAD"), 0)

    def test_test_directories_are_not_the_project_packages(self):
        with tempfile.TemporaryDirectory(prefix="mc-env-") as tmp:
            root = Path(tmp)
            (root / "tests").mkdir()
            (root / "tests" / "__init__.py").write_text("import no_such_dependency_xyz\n")
            self.assertEqual(mutation_env.refuse(root, "HEAD"), 0)

    def test_a_package_the_change_broke_is_red_not_tempfail(self):
        with tempfile.TemporaryDirectory(prefix="mc-env-") as tmp:
            root = Path(tmp)
            base, _ = make_range(root, {"pkg": "VALUE = 1\n"})
            (root / "pkg" / "__init__.py").write_text("import no_such_dependency_xyz\n")
            commit_all(root, "break pkg")
            rc, out = run_main(root, base, "HEAD")
        self.assertEqual(rc, 1, out)
        self.assertIn("change error: cannot import pkg", out)

    def test_a_package_new_in_the_range_that_does_not_import_is_red(self):
        with tempfile.TemporaryDirectory(prefix="mc-env-") as tmp:
            root = Path(tmp)
            base, _ = make_range(root, {})
            (root / "newpkg").mkdir()
            (root / "newpkg" / "__init__.py").write_text("import no_such_dependency_xyz\n")
            commit_all(root, "add newpkg")
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(mutation_env.refuse(root, base), 1)

    def test_an_unreadable_base_is_red_not_tempfail(self):
        with tempfile.TemporaryDirectory(prefix="mc-env-") as tmp:
            root = Path(tmp)
            make_range(root, {"pkg": "import no_such_dependency_xyz\n"})
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(mutation_env.refuse(root, "no-such-ref"), 1)

    def test_a_directory_that_is_not_an_identifier_is_ignored(self):
        with tempfile.TemporaryDirectory(prefix="mc-env-") as tmp:
            root = Path(tmp)
            make_range(root, {"bad-name": "import no_such_dependency_xyz\n"})
            self.assertEqual(mutation_env.refuse(root, "HEAD"), 0)

    def test_a_hanging_import_is_red(self):
        with tempfile.TemporaryDirectory(prefix="mc-env-") as tmp:
            root = Path(tmp)
            base, head = make_range(root, {"slowpkg": "import time\ntime.sleep(30)\n"})
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = mutation_env.refuse(root, base, timeout=1)
        self.assertEqual(rc, 1)
        self.assertIn("import hung", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
