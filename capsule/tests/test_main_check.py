"""`python3 -m capsule check --board other` must resolve `other`'s own config, not the (here,
deliberately absent) legacy one."""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from capsule import paths


class CheckBoard(unittest.TestCase):
    def test_check_dash_dash_board_reads_the_named_boards_config(self):
        home = Path(tempfile.mkdtemp())   # no home/config.json: the legacy path is unreadable
        cfg = json.loads((paths.REPO / "capsule" / "config.example.json").read_text())
        cfg["board"] = "other"
        p = paths.config_path(home, "other")
        p.parent.mkdir(parents=True)
        p.write_text(json.dumps(cfg))
        r = subprocess.run([sys.executable, "-m", "capsule", "check", "--board", "other",
                           "--capsule-home", str(home)], cwd=paths.REPO, capture_output=True, text=True, timeout=60)
        self.assertIn(str(p), r.stdout)          # oracle 1: the resolved config path is this board's own
        self.assertIn("board other", r.stdout)   # oracle 2: the loaded config's own board name


if __name__ == "__main__":
    unittest.main()
