"""W234: decision_log.write_sidecar never writes through a symlinked Decisions dir
or a planted temp."""
import os, sys, tempfile, unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import decision_log

LOG = ("| # | Date | Decision | Why | Status |\n|---|---|---|---|---|\n"
       "| 1 | 2026-01-01 | a | b | open |\n")


class SidecarSymlinkTests(unittest.TestCase):
    def setUp(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        self.vault = Path(td.name) / "vault"
        self.vault.mkdir()
        self.outside = Path(td.name) / "outside"
        self.outside.mkdir()
        self.log = Path(td.name) / "log.md"
        self.log.write_text(LOG)

    def test_normal_write(self):
        self.assertTrue(decision_log.write_sidecar(self.vault, self.log))
        self.assertTrue((self.vault / "Decisions" / decision_log.SIDECAR_NAME).is_file())

    def test_symlinked_decisions_dir_not_written_through(self):
        os.symlink(self.outside, self.vault / "Decisions")
        with self.assertRaises(OSError):
            decision_log.write_sidecar(self.vault, self.log)
        self.assertEqual(list(self.outside.iterdir()), [])

    def test_planted_temp_symlink_not_written_through(self):
        (self.vault / "Decisions").mkdir()
        victim = self.outside / "v.txt"
        victim.write_text("precious")
        os.symlink(victim, self.vault / "Decisions" / "decisions.jsonl.tmp")
        decision_log.write_sidecar(self.vault, self.log)
        self.assertEqual(victim.read_text(), "precious")
        self.assertTrue((self.vault / "Decisions" / decision_log.SIDECAR_NAME).is_file())


if __name__ == "__main__":
    unittest.main()
