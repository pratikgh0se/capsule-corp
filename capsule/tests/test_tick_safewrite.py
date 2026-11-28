"""t_f6d743ff: the tick's job.json write never follows a symlink a worker planted. A worker with
its spool mounted rw can plant the old predictable temp name (.job.json.tmp) or replace its run
directory with a symlink; the write must neither follow either nor land outside the spool."""
import json
import os
import tempfile
import unittest
from pathlib import Path

from capsule import safe_write, tick


class JobJsonWrite(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name).resolve()
        self.spool = base / "spool"
        self.run = self.spool / "run1"
        self.run.mkdir(parents=True)
        self.outside = base / "outside"
        self.outside.mkdir()
        self.victim = self.outside / "victim"
        self.victim.write_text("original")

    def test_plain_write_lands_and_leaves_no_temp(self):
        tick._write_json(self.run / "job.json", {"a": 1})
        self.assertEqual(json.loads((self.run / "job.json").read_text()), {"a": 1})
        self.assertEqual([p.name for p in self.run.iterdir()], ["job.json"])

    def test_planted_symlink_at_old_temp_name_is_not_followed(self):
        os.symlink(self.victim, self.run / ".job.json.tmp")
        tick._write_json(self.run / "job.json", {"a": 1})
        self.assertEqual(self.victim.read_text(), "original")
        self.assertEqual(json.loads((self.run / "job.json").read_text()), {"a": 1})

    def test_symlinked_run_dir_is_refused_and_nothing_written_outside(self):
        self.run.rmdir()
        os.symlink(self.outside, self.run)
        with self.assertRaises(safe_write.SymlinkRefused):
            tick._write_json(self.run / "job.json", {"a": 1})
        self.assertEqual(sorted(p.name for p in self.outside.iterdir()), ["victim"])
        self.assertEqual(self.victim.read_text(), "original")


if __name__ == "__main__":
    unittest.main()
