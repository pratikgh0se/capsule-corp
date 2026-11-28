"""inc_df5777e3: tests/mutation_check.py prints the add-a-base-failing-test remedy
after the table when a revert cell reads "did not fail (BAD)", and only then.
Own file: tests/test_mutation_check.py is at its frozen size-baseline length."""
import contextlib
import io
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mutation_check as mc  # noqa: E402
import test_mutation_check as fx  # noqa: E402  (fixtures only; no TestCase classes imported)

REMEDY = "Add a test that fails on --base"


def run_cli_on_range(head_test):
    """Real CLI on a fixture range: buggy base, fixed production + `head_test` at head."""
    with tempfile.TemporaryDirectory(prefix="mc-fixture-") as tmp:
        root = Path(tmp)
        fx.init_repo(root)
        fx.write_empty_baseline(root)
        (root / "production.py").write_text(fx.PROD_BUGGY)
        fx.write_test(root, fx.PLACEHOLDER_TEST)
        base = fx.commit_all(root, "base")
        (root / "production.py").write_text(fx.PROD_FIXED)
        fx.write_test(root, head_test)
        head = fx.commit_all(root, "head")
        script = str(Path(__file__).resolve().parent / "mutation_check.py")
        return subprocess.run([sys.executable, script, "--base", base, "--head", head,
                               "--repo", str(root)], capture_output=True, text=True)


class RemedyOnABadRevertCell(unittest.TestCase):
    def test_cli_prints_the_add_a_base_failing_test_remedy(self):
        p = run_cli_on_range(fx.TAUTOLOGICAL_TEST)
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn(REMEDY, p.stdout, p.stderr)
        self.assertIn("`tests/test_production.py`", p.stdout)
        self.assertIn("removed name, key or path is absent", p.stdout)
        self.assertIn("Rerun the gate", p.stdout)

    def test_cli_prints_no_remedy_for_a_discriminating_test(self):
        p = run_cli_on_range(fx.DISCRIMINATING_TEST)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("| test file |", p.stdout)
        self.assertNotIn(REMEDY, p.stdout)


class RemedyOnlyOnBadRevertCells(unittest.TestCase):
    @staticmethod
    def run_main(cell):
        out = io.StringIO()
        rows = [("t.py", "t", cell, "-", "none")]
        mocks = {"dirty_tracked_paths": [], "check_range": (False, rows, None)}
        with contextlib.ExitStack() as st, contextlib.redirect_stdout(out):
            for name, rv in mocks.items():
                st.enter_context(unittest.mock.patch.object(mc, name, return_value=rv))
            for chk in (mc.size_check, mc.simplicity_check):
                st.enter_context(unittest.mock.patch.object(chk, "check", return_value=(True, "")))
            return mc.main(["--base", "a", "--head", "b"]), out.getvalue()

    def test_marker_mismatch_and_stale_marker_cells_do_not_print_the_remedy(self):
        for cell in (mc._cell("marker_mismatch", "sha"), mc._cell("marker_stale", "ids")):
            rc, out = self.run_main(cell)
            self.assertEqual(rc, 1)
            self.assertIn(cell, out)
            self.assertNotIn(REMEDY, out)

    def test_a_did_not_fail_cell_prints_the_remedy_once(self):
        rc, out = self.run_main(mc._cell(False, "-"))
        self.assertEqual(rc, 1)
        self.assertEqual(out.count(REMEDY), 1, out)


if __name__ == "__main__":
    unittest.main()
