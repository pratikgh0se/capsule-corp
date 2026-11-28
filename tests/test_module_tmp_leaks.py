"""Guard: a maintenance test module run on its own (`unittest discover -s maintenance/tests -p
test_x.py`, what builders and gates do) leaves nothing in the caller's $TMPDIR. Those runs never
import test_0_tmpsweep.py, so every mkdtemp of the module leaked: ~76,000 entries in $TMPDIR drove
fseventsd to 36 GB. Each module that makes temp dirs installs capsule.tests.tmpclean itself."""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULES = ("test_budgets.py", "test_brief.py", "test_decisions.py", "test_docs_sync.py",
           "test_reviewer_calibration.py", "test_board_bridge.py")


class ModuleLeavesNoTmp(unittest.TestCase):
    def test_single_module_runs_leave_tmpdir_empty(self):
        for module in MODULES:
            with self.subTest(module=module), tempfile.TemporaryDirectory(
                    ignore_cleanup_errors=True) as tmp:
                env = {k: v for k, v in os.environ.items() if k != "OBSIDIAN_SKILL_ROOT"}
                env.update(TMPDIR=tmp, AGENT_OPS_NO_SEND="1")
                done = subprocess.run(
                    [sys.executable, "-m", "unittest", "discover", "-s", "maintenance/tests",
                     "-p", module], cwd=ROOT, env=env, capture_output=True, text=True, timeout=900)
                self.assertEqual(done.returncode, 0, done.stderr[-800:])
                self.assertEqual(sorted(os.listdir(tmp)), [])


if __name__ == "__main__":
    unittest.main()
