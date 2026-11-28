"""W234: review packet writers refuse a worker-planted symlink (see safe_write)."""
import contextlib
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
os.environ.setdefault("AGENT_OPS_NO_SEND", "1")

import board_bridge as bb
from test_review_diff import _Repo


class SymlinkedSlugDir(unittest.TestCase):
    """W234: a worker-planted symlink must not steer any host unlink, mkdir or write."""

    def _snapshot(self, v):
        return sorted(str(p.relative_to(v)) for p in v.rglob("*"))

    def test_review_diff_refuses_symlinked_slug(self):
        repo = _Repo({"server.py": "x = 1\n"})
        self.addCleanup(repo.cleanup)
        with tempfile.TemporaryDirectory() as td:
            v = Path(td) / "victim"
            (v / "card" / "files").mkdir(parents=True)
            (v / "card" / "files" / "x.diff").write_text("keep")
            (v / "card" / "MANIFEST.md").write_text("keep")
            before = self._snapshot(v)
            rp = repo.root / ".worktrees" / bb.REVIEW_PACKET_DIRNAME
            rp.mkdir(parents=True)
            os.symlink(v, rp / repo.slug)
            with contextlib.suppress(OSError):
                repo.write()
            self.assertEqual(self._snapshot(v), before)
            self.assertEqual((v / "card" / "MANIFEST.md").read_text(), "keep")
            self.assertEqual((v / "card" / "files" / "x.diff").read_text(), "keep")


if __name__ == "__main__":
    unittest.main()
