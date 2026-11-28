"""t_b57d1693: worker.log is opened O_NOFOLLOW|O_APPEND, so a symlink planted at that path in
the spool run dir is refused by both writers (tick._spawn and worker._persist_crash_output)."""
import os
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from capsule import safe_write, tick, tick_runs, worker


class WorkerLogPlanted(unittest.TestCase):
    def setUp(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        base = Path(td.name).resolve()
        self.run = base / "run1"
        self.run.mkdir()
        self.victim = base / "victim"
        self.victim.write_text("original")

    def plant(self):
        os.symlink(self.victim, self.run / "worker.log")

    def test_spawn_appends_to_a_real_log(self):
        (self.run / "worker.log").write_text("old\n")
        with mock.patch.object(tick_runs.subprocess, "Popen") as popen:
            tick.Tick._spawn(None, "db", self.run, "r1", "implementer")
        self.assertTrue(popen.called)
        self.assertEqual((self.run / "worker.log").read_text(), "old\n")

    def test_spawn_refuses_planted_symlink(self):
        self.plant()
        with mock.patch.object(tick_runs.subprocess, "Popen") as popen:
            with self.assertRaises(safe_write.SymlinkRefused):
                tick.Tick._spawn(None, "db", self.run, "r1", "implementer")
        popen.assert_not_called()
        self.assertEqual(self.victim.read_text(), "original")

    def _crash(self):
        result = types.SimpleNamespace(raw="out", stderr="err", session_id=None)
        denials = [{"tool_name": "Bash", "tool_use_id": "u1"}]
        worker._persist_crash_output(self.run, result, denials, "why")

    def test_crash_output_appends_to_a_real_log(self):
        (self.run / "worker.log").write_text("old\n")
        self._crash()
        text = (self.run / "worker.log").read_text()
        self.assertTrue(text.startswith("old\n"))
        self.assertIn("denied tool=Bash", text)

    def test_crash_output_refuses_planted_symlink(self):
        self.plant()
        with self.assertRaises(safe_write.SymlinkRefused):
            self._crash()
        self.assertEqual(self.victim.read_text(), "original")


if __name__ == "__main__":
    unittest.main()
