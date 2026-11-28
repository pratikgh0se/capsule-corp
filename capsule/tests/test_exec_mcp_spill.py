"""exec_mcp._maybe_spill writes into the spool, which the worker's sandbox shares: a planted
symlink or file must never make the host write outside the spool or overwrite a file."""
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from capsule.runners import exec_mcp

BIG = "x" * (exec_mcp.SPILL_THRESHOLD + 100)


class SpillSafe(unittest.TestCase):
    def setUp(self):
        self.spool = Path(tempfile.mkdtemp())
        self.outside = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.spool, ignore_errors=True)
        self.addCleanup(shutil.rmtree, self.outside, ignore_errors=True)

    def _patch_names(self, fn):
        orig = exec_mcp._next_spill_name
        exec_mcp._next_spill_name = fn
        self.addCleanup(setattr, exec_mcp, "_next_spill_name", orig)
        return orig

    def test_normal_spill_format_and_content(self):
        out = exec_mcp._maybe_spill(BIG, self.spool)
        path = self.spool / "spill" / "1.txt"
        self.assertEqual(path.read_text(), BIG)
        self.assertEqual(oct(path.stat().st_mode & 0o777), "0o600")
        edge = exec_mcp.SPILL_EDGE
        want = f"{BIG[:edge]}\n...[{len(BIG)} chars total, spilled to {path}]...\n{BIG[-edge:]}"
        self.assertEqual(out, want)

    def test_surrogates_are_written(self):
        text = "\ud800" + BIG
        exec_mcp._maybe_spill(text, self.spool)
        data = (self.spool / "spill" / "1.txt").read_bytes()
        self.assertEqual(data, text.encode("utf-8", "surrogatepass"))

    def test_symlinked_spill_dir_is_refused(self):
        os.symlink(self.outside, self.spool / "spill")
        out = exec_mcp._maybe_spill(BIG, self.spool)
        self.assertEqual(list(self.outside.iterdir()), [])
        self.assertIn("NOT spilled", out)
        self.assertTrue(out.startswith("x" * 10) and out.endswith("x" * 10))
        self.assertLess(len(out), len(BIG))

    def test_spill_that_is_a_file_is_refused(self):
        (self.spool / "spill").write_text("keep")
        out = exec_mcp._maybe_spill(BIG, self.spool)
        self.assertIn("NOT spilled", out)
        self.assertEqual((self.spool / "spill").read_text(), "keep")

    def test_existing_file_and_symlink_names_are_never_touched(self):
        spill = self.spool / "spill"
        spill.mkdir()
        (spill / "1.txt").write_text("old")
        target = self.outside / "t.txt"
        target.write_text("target")
        os.symlink(target, spill / "2.txt")
        out = exec_mcp._maybe_spill(BIG, self.spool)
        self.assertEqual((spill / "1.txt").read_text(), "old")
        self.assertEqual(target.read_text(), "target")
        self.assertEqual((spill / "3.txt").read_text(), BIG)
        self.assertIn(f"spilled to {spill / '3.txt'}", out)

    def test_name_race_retries_then_gives_up(self):
        spill = self.spool / "spill"
        spill.mkdir()
        target = self.outside / "t.txt"
        target.write_text("target")
        taken = []

        def racing(names):
            # a worker wins the race for every name the host picks
            name = orig(names)
            os.symlink(target, spill / name)
            taken.append(name)
            return name

        orig = self._patch_names(racing)
        out = exec_mcp._maybe_spill(BIG, self.spool)
        self.assertEqual(len(taken), 5)
        self.assertIn("NOT spilled", out)
        self.assertEqual(sorted(os.listdir(spill)), sorted(taken))
        self.assertEqual(target.read_text(), "target")

    def test_spill_dir_swapped_for_symlink_after_the_check(self):
        spill = self.spool / "spill"

        def swap(_names):
            os.rename(spill, f"{spill}.moved")
            os.symlink(self.outside, spill)
            return "1.txt"

        self._patch_names(swap)
        exec_mcp._maybe_spill(BIG, self.spool)
        self.assertEqual(list(self.outside.iterdir()), [])

    def test_dangling_symlink_as_spill_is_refused(self):
        missing = self.outside / "missing"
        os.symlink(missing, self.spool / "spill")
        out = exec_mcp._maybe_spill(BIG, self.spool)
        self.assertIn("NOT spilled", out)
        self.assertFalse(missing.exists())

    def test_non_ascii_digit_name_does_not_break_spill(self):
        spill = self.spool / "spill"
        spill.mkdir()
        (spill / "².txt").write_text("planted")
        out = exec_mcp._maybe_spill(BIG, self.spool)
        self.assertEqual((spill / "1.txt").read_text(), BIG)
        self.assertIn("spilled to", out)

    def test_unwritable_spill_dir_gives_not_spilled(self):
        spill = self.spool / "spill"
        spill.mkdir()
        spill.chmod(0o500)
        self.addCleanup(spill.chmod, 0o700)
        out = exec_mcp._maybe_spill(BIG, self.spool)
        self.assertIn("NOT spilled", out)
        self.assertEqual(os.listdir(spill), [])


if __name__ == "__main__":
    unittest.main()
