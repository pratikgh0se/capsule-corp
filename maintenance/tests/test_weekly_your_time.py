"""M201: the Sunday weekly review carries the same YOUR TIME block `capsule report --md` prints.
`run.sh`'s `weekly)` arm captures `python3 -m capsule report <board> --your-time` (report._yt,
not a copy) and substitutes it as `__YOUR_TIME__`; this extracts those exact lines and runs them."""
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from capsule import db, paths   # noqa: E402

RUN_SH = ROOT / "maintenance" / "run.sh"
WEEKLY = ROOT / "maintenance" / "weekly.md"
from capsule.tests.public_tree import private_only  # noqa: E402


def _snippet():
    m = re.search(r'YT_RAW=.*?PROMPT_TEXT="\$\{PROMPT_TEXT//__YOUR_TIME__/\$YOUR_TIME\}"',
                  RUN_SH.read_text(encoding="utf-8"), re.S)
    assert m, "weekly)'s YT_RAW capture/substitute lines moved or changed shape"
    return m.group(0)


def _run(corp_dir, home):
    script = (f'set -u\nCAPSULE_CORP_DIR={corp_dir}\nexport CAPSULE_HOME={home}\n'
              f'RUN_SH_YT_BOARD=t\nPROMPT_TEXT="before __YOUR_TIME__ after"\n'
              f'{_snippet()}\nprintf %s "$PROMPT_TEXT"')
    env = dict(os.environ, AGENT_OPS_STATE_DIR=str(home))
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                          timeout=60, env=env)


class WeeklyYourTime(unittest.TestCase):
    @private_only
    def test_weekly_md_pastes_the_placeholder(self):
        text = WEEKLY.read_text(encoding="utf-8")
        self.assertIn("__YOUR_TIME__", text)
        self.assertIn("Check YOUR TIME by hand", text)

    def test_the_block_appears_with_every_line(self):
        home = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        db.create_board(paths.board_db("t", home))
        out = _run(ROOT, home)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertNotIn("__YOUR_TIME__", out.stdout)
        for line in ("## YOUR TIME", "- Decisions per day by class:", "- Approvals per day:",
                     "- Terminal hours:", "- Overflow:", "- Slowest: not enough data"):
            self.assertIn(line, out.stdout)

    def test_a_broken_capture_is_named_not_empty(self):
        home = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        out = _run("/nonexistent-capsule-corp-dir", home)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("capture failed (capsule report --your-time", out.stdout)
        self.assertNotIn("## YOUR TIME\n", out.stdout)


if __name__ == "__main__":
    unittest.main()
