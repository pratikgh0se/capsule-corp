"""t_0080b449 (inc_8ef5d9fa): a targets-predate-range marker is valid when its sha is the gate's
--base or any ancestor of it, so one marker passes the review gate (base = the card's start) and
the lander (base = origin/main). A sha inside the range, or off the base's history, stays a
mismatch. Own file: tests/test_mutation_check.py is at its frozen size-baseline length."""
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mutation_check as mc  # noqa: E402
import test_mutation_check as fx  # noqa: E402  (fixtures only; no TestCase classes imported)


def marked(sha):
    return (f"# mutation-check: targets-predate-range {sha} -- old coverage "
            ":: Add.test_add_returns_the_sum\n" + fx.DISCRIMINATING_TEST)


def range_with_marker_at(pick):
    """check_range on base = the 2nd of two commits; the marker names `pick(first, base)`."""
    with tempfile.TemporaryDirectory(prefix="mc-fixture-") as tmp:
        root = Path(tmp)
        fx.init_repo(root)
        (root / "production.py").write_text(fx.PROD_FIXED)
        (root / "unrelated.py").write_text("def sub(a, b):\n    return a - a\n")
        fx.write_test(root, fx.PLACEHOLDER_TEST)
        first = fx.commit_all(root, "first")
        (root / "moved.txt").write_text("main moved\n")
        base = fx.commit_all(root, "main moved")
        (root / "unrelated.py").write_text("def sub(a, b):\n    return a - b\n")
        fx.write_test(root, marked(pick(first, base)))
        head = fx.commit_all(root, "fix unrelated.py; add marked tests")
        return mc.check_range(root, base, head)


class MarkerAtAnAncestorOfBase(unittest.TestCase):
    def test_a_marker_at_an_ancestor_of_base_is_not_a_mismatch(self):
        ok, rows, note = range_with_marker_at(lambda first, base: first)
        self.assertIsNone(note)
        self.assertTrue(ok, rows)
        self.assertIn("n/a (targets predate range", rows[0][2])

    def test_a_marker_at_the_base_itself_still_passes(self):
        ok, rows, note = range_with_marker_at(lambda first, base: base[:7])
        self.assertTrue(ok, rows)

    def test_a_marker_naming_a_sha_off_the_bases_history_is_a_mismatch(self):
        with tempfile.TemporaryDirectory(prefix="mc-fixture-") as tmp:
            root = Path(tmp)
            fx.init_repo(root)
            (root / "production.py").write_text(fx.PROD_FIXED)
            fx.write_test(root, fx.PLACEHOLDER_TEST)
            fx.commit_all(root, "first")
            subprocess.run(["git", "checkout", "-q", "-b", "side"], cwd=root, check=True)
            (root / "side.txt").write_text("side\n")
            side = fx.commit_all(root, "side only")
            subprocess.run(["git", "checkout", "-q", "-"], cwd=root, check=True)
            (root / "moved.txt").write_text("main moved\n")
            base = fx.commit_all(root, "main moved")
            fx.write_test(root, marked(side))
            head = fx.commit_all(root, "marked tests")
            ok, rows, note = mc.check_range(root, base, head)
            self.assertFalse(ok, rows)
            self.assertIn("marker mismatch (BAD)", rows[0][2])

    def test_a_marker_naming_a_sha_inside_the_range_is_a_mismatch(self):
        with tempfile.TemporaryDirectory(prefix="mc-fixture-") as tmp:
            root = Path(tmp)
            fx.init_repo(root)
            (root / "production.py").write_text(fx.PROD_FIXED)
            fx.write_test(root, fx.PLACEHOLDER_TEST)
            base = fx.commit_all(root, "base")
            (root / "inside.txt").write_text("work\n")
            inside = fx.commit_all(root, "card work")
            fx.write_test(root, marked(inside))
            head = fx.commit_all(root, "marked tests")
            ok, rows, note = mc.check_range(root, base, head)
            self.assertFalse(ok, rows)
            self.assertIn("marker mismatch (BAD)", rows[0][2])


class MarkerMustBeAddedInTheRange(unittest.TestCase):
    """A marker an earlier card left on main must not shield a later card's changed test."""

    def test_a_marker_left_by_an_earlier_card_is_stale_not_a_skip(self):
        with tempfile.TemporaryDirectory(prefix="mc-fixture-") as tmp:
            root = Path(tmp)
            fx.init_repo(root)
            (root / "production.py").write_text(fx.PROD_BUGGY)
            fx.write_test(root, fx.PLACEHOLDER_TEST)
            first = fx.commit_all(root, "first")
            fx.write_test(root, marked(first))
            base = fx.commit_all(root, "an earlier card added the marker")
            (root / "production.py").write_text(fx.PROD_FIXED)   # this card's subject changes
            fx.write_test(root, marked(first) + "        self.assertEqual(add(1, 1), 2)\n")
            head = fx.commit_all(root, "a later card changes the named test")
            ok, rows, note = mc.check_range(root, base, head)
            self.assertFalse(ok, rows)
            self.assertIn("marker stale (BAD)", rows[0][2])

    def test_a_marker_added_in_the_range_still_skips_the_revert_check(self):
        ok, rows, note = range_with_marker_at(lambda first, base: first)
        self.assertTrue(ok, rows)
        self.assertIn("n/a (targets predate range", rows[0][2])


if __name__ == "__main__":
    unittest.main()
