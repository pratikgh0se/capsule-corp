"""Planted-symlink surfaces of the host-side spool writers (card t_00d59a51)."""
import os
import tempfile
import unittest
from pathlib import Path

from capsule import safe_write, spool, worker


class SpoolMakeSymlink(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.root, self.elsewhere = self.d / "spool", self.d / "elsewhere"
        self.root.mkdir()
        self.elsewhere.mkdir()

    def test_refuses_symlinked_run_dir(self):
        os.symlink(self.elsewhere, self.root / "run1")
        with self.assertRaises(safe_write.SymlinkRefused):
            spool.make(self.root, "run1")
        self.assertEqual(list(self.elsewhere.iterdir()), [])

    def test_refuses_symlinked_subdir(self):
        (self.root / "run1").mkdir()
        os.symlink(self.elsewhere, self.root / "run1" / "report")
        with self.assertRaises(safe_write.SymlinkRefused):
            spool.make(self.root, "run1")

    def test_makes_and_is_idempotent(self):
        sp = spool.make(self.root / "new", "run1")
        self.assertEqual(sp, self.root / "new" / "run1")
        self.assertEqual(spool.make(self.root / "new", "run1"), sp)
        for sub in spool.SUBDIRS:
            self.assertTrue((sp / sub).is_dir())


class WorkerWritersSymlink(unittest.TestCase):
    def test_planted_temp_symlink_does_not_redirect_write(self):
        d = Path(tempfile.mkdtemp())
        victim = d / "victim.txt"
        victim.write_text("keep")
        os.symlink(victim, d / ".run.json.tmp")     # the old predictable temp name
        worker._write_json(d / "run.json", {"exit": 0})
        self.assertEqual(victim.read_text(), "keep")
        self.assertIn('"exit": 0', (d / "run.json").read_text())

    def test_write_text_ignores_planted_temp(self):
        d = Path(tempfile.mkdtemp())
        victim = d / "victim.txt"
        victim.write_text("keep")
        os.symlink(victim, d / ".cli_result.txt.tmp")
        worker._write_text(d / "cli_result.txt", "out")
        self.assertEqual(victim.read_text(), "keep")
        self.assertEqual((d / "cli_result.txt").read_text(), "out")


if __name__ == "__main__":
    unittest.main()
