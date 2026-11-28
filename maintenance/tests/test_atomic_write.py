"""board_bridge._atomic_write_text must not write through planted symlinks (W230)."""
import os, sys, tempfile, unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("AGENT_OPS_NO_SEND", "1")

import board_bridge as bb


class AtomicWriteSymlinkTests(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.d = Path(self._td.name) / "packet"
        self.d.mkdir()
        self.outside = Path(self._td.name) / "outside.txt"
        self.outside.write_text("precious")

    def test_planted_predictable_tmp_symlink_not_written_through(self):
        target = self.d / "diff.md"
        os.symlink(self.outside, str(target) + ".tmp")
        n = bb._atomic_write_text(target, "new content")
        self.assertEqual(self.outside.read_text(), "precious")
        self.assertEqual(target.read_text(), "new content")
        self.assertFalse(target.is_symlink())
        self.assertEqual(n, len("new content"))

    def test_symlinked_target_is_replaced_not_written_through(self):
        target = self.d / "diff.md"
        os.symlink(self.outside, target)
        bb._atomic_write_text(target, "new content")
        self.assertEqual(self.outside.read_text(), "precious")
        self.assertFalse(target.is_symlink())
        self.assertEqual(target.read_text(), "new content")

    def test_normal_write_leaves_only_the_target(self):
        target = self.d / "a.md"
        bb._atomic_write_text(target, "one")
        bb._atomic_write_text(target, "two")
        self.assertEqual(target.read_text(), "two")
        self.assertEqual([p.name for p in self.d.iterdir()], ["a.md"])

    def test_temp_removed_on_failure_and_target_untouched(self):
        target = self.d / "a.md"
        target.write_text("old")
        with mock.patch.object(bb.os, "replace", side_effect=OSError("boom")):
            with self.assertRaises(OSError):
                bb._atomic_write_text(target, "new")
        self.assertEqual(target.read_text(), "old")
        self.assertEqual([p.name for p in self.d.iterdir()], ["a.md"])


class PacketRootTests(unittest.TestCase):
    """W234: with root=repo_root a symlinked packet dir is refused, nothing lands outside."""
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.repo = Path(self._td.name) / "repo"
        (self.repo / ".worktrees").mkdir(parents=True)
        self.outside = Path(self._td.name) / "outside"
        self.outside.mkdir()

    def test_symlinked_review_packets_dir_refused(self):
        os.symlink(self.outside, self.repo / ".worktrees" / "review-packets")
        target = self.repo / ".worktrees" / "review-packets" / "slug" / "branch.diff"
        with self.assertRaises(OSError):
            bb._atomic_write_text(target, "x", root=self.repo)
        self.assertEqual(list(self.outside.iterdir()), [])

    def test_symlinked_slug_dir_refused(self):
        rp = self.repo / ".worktrees" / "review-packets"
        rp.mkdir()
        os.symlink(self.outside, rp / "slug")
        with self.assertRaises(OSError):
            bb._atomic_write_text(rp / "slug" / "MANIFEST.md", "x", root=self.repo)
        self.assertEqual(list(self.outside.iterdir()), [])

    def test_normal_packet_write_creates_dirs(self):
        target = self.repo / ".worktrees" / "review-packets" / "slug" / "card" / "files" / "a.diff"
        bb._atomic_write_text(target, "ok", root=self.repo)
        self.assertEqual(target.read_text(), "ok")

    def test_stale_dotfile_temps_cleared(self):
        card = self.repo / "card"
        d = card / "files"
        d.mkdir(parents=True)
        (d / ".001-x.diff.abc123.tmp").write_text("junk")
        (d / "keep.txt").write_text("k")
        bb._reset_card_packet(self.repo, card / "MANIFEST.md", d)
        self.assertEqual([p.name for p in d.iterdir()], ["keep.txt"])


if __name__ == "__main__":
    unittest.main()
