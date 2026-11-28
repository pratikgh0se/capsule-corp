"""t_c04395ea: a targets-predate-range marker may wrap over `#` lines indented by 2+ spaces, so a
descriptive test id and a real reason fit the 100-column rule. Own file: test_mutation_check.py
is at its frozen size-baseline length."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mutation_check as mc


class WrappedMarker(unittest.TestCase):
    def test_a_marker_wrapped_over_indented_comment_lines_reads_as_one(self):
        top = ("# mutation-check: targets-predate-range abc1234 -- new tests for old code\n"
               "#   that the range never touched ::\n"
               "#   Add.test_a_long_descriptive_name, Add.test_b\nimport os\n")
        self.assertEqual(
            mc.extract_marker(top),
            ("abc1234", "new tests for old code that the range never touched",
             ("Add.test_a_long_descriptive_name", "Add.test_b")))

    def test_a_following_marker_line_indented_three_spaces_is_not_joined(self):
        two = ("# mutation-check: targets-predate-range abc1234 -- why :: Add.test_a\n"
               "#   mutation-check: targets-predate-range def5678 -- other :: Add.test_b\n")
        self.assertEqual(mc.extract_marker(two), ("abc1234", "why", ("Add.test_a",)))

    def test_a_bare_method_name_in_a_marker_covers_only_that_method(self):
        import tempfile
        import test_mutation_check as t
        with tempfile.TemporaryDirectory(prefix="mc-bare-") as tmp:
            root = Path(tmp)
            t.init_repo(root)
            (root / "production.py").write_text(t.PROD_FIXED)
            t.write_test(root, t.PLACEHOLDER_TEST)
            base = t.commit_all(root, "base")
            t.write_test(root, (
                f"# mutation-check: targets-predate-range {base} -- old :: test_marked\n"
                "import unittest\nfrom production import add\n\n\n"
                "class Marked(unittest.TestCase):\n"
                "    def test_marked(self):\n        self.assertEqual(add(2, 3), 5)\n"))
            head = t.commit_all(root, "bare id marker")
            ok, rows, note = mc.check_range(root, base, head)
            self.assertTrue(ok, rows)
            self.assertIn("n/a (targets predate range: old)", rows[0][2])

    def test_an_ordinary_comment_after_a_marker_is_not_absorbed(self):
        plain = ("# mutation-check: targets-predate-range abc1234 -- why :: Add.test_a\n"
                 "# an ordinary comment after it\nimport os\n")
        self.assertEqual(mc.extract_marker(plain), ("abc1234", "why", ("Add.test_a",)))


if __name__ == "__main__":
    unittest.main()
