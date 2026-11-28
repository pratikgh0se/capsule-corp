"""tests/simplicity_calibrate.py: per-rule counts on a tiny temp repo, one known violation per
rule, a clean commit at zero, and a stable output format."""
import contextlib
import io
import shutil
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import simplicity_calibrate as cal  # noqa: E402
import test_simplicity_check as t  # noqa: E402


class CalibrateTest(unittest.TestCase):
    def setUp(self):
        self.root = t.make_repo()
        self.addCleanup(shutil.rmtree, self.root, True)
        (self.root / "a.py").write_text("x = 0\n")
        t.commit_all(self.root, "seed")

    def land(self, text, subject):
        (self.root / "a.py").write_text(text)
        return t.commit_all(self.root, subject)

    def run_cal(self, n):
        """The tool's real stdout, as lines."""
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(cal.main(["--repo", str(self.root), "--last", str(n)]), 0)
        return buf.getvalue().splitlines()

    def rule_rows(self, lines, rule):
        """The commit rows printed under 'rule N:' as (subject, hits)."""
        head = next(i for i, x in enumerate(lines) if x.startswith(f"rule {rule}:"))
        rows = []
        for x in lines[head + 1:]:
            if not x.startswith("  "):
                break
            rows.append((x.split(" ", 4)[4], int(x.split(" ")[3])))
        return rows

    def test_rule_7_long_line_counted_on_its_commit(self):
        self.land(t.LONG, "long line")
        lines = self.run_cal(2)
        self.assertEqual(self.rule_rows(lines, 7), [("long line", 1)])
        self.assertNotIn("check errors", "\n".join(lines))

    def test_rule_3_deep_nesting_counted(self):
        self.land(t.DEEP, "deep")
        self.assertEqual(self.rule_rows(self.run_cal(2), 3), [("deep", 2)])

    def test_clean_commit_has_zero_findings(self):
        self.land(t.FLAT, "flat")
        lines = self.run_cal(2)
        self.assertEqual([x for x in lines if x.startswith("  ") or "errors" in x], [])

    def test_root_commit_is_skipped(self):
        self.assertEqual([x for x in self.run_cal(5) if x.startswith("  ")], [])

    def test_output_format_is_stable(self):
        sha = self.land(t.LONG, "long line")
        out = self.run_cal(2)
        self.assertEqual(out[0], "simplicity_calibrate: last 2 first-parent commit(s) "
                                 "up to HEAD (report only)")
        self.assertEqual([x for x in out if x.startswith("rule ")],
                         [f"rule {r}: {'1' if r == 7 else '0'} flagged line(s) on "
                          f"{'1' if r == 7 else '0'} commit(s)" for r in range(1, 8)])
        self.assertIn(f"  {sha[:7]} 1 long line", out)


if __name__ == "__main__":
    unittest.main()
