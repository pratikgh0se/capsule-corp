"""Unit suite for capsule.safe_write: atomic_write, open_dir, open_append."""
import os
import stat
import tempfile
import unittest
from pathlib import Path

from capsule import safe_write as sw


class Base(unittest.TestCase):
    def setUp(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        self.root = Path(td.name).resolve()
        self.out = self.root / "outside"
        self.out.mkdir()
        self.run_dir = self.root / "run"
        self.run_dir.mkdir()


class AtomicWriteTest(Base):
    def test_writes_str_and_bytes_and_returns_length(self):
        self.assertEqual(sw.atomic_write(self.run_dir, self.run_dir / "a", "hi"), 2)
        self.assertEqual(sw.atomic_write(self.run_dir, self.run_dir / "b", b"\x00\x01"), 2)
        self.assertEqual((self.run_dir / "a").read_text(), "hi")
        self.assertEqual((self.run_dir / "b").read_bytes(), b"\x00\x01")

    def test_idempotent_overwrite_leaves_no_temp(self):
        for text in ("one", "two", "two"):
            sw.atomic_write(self.run_dir, self.run_dir / "f", text)
        self.assertEqual((self.run_dir / "f").read_text(), "two")
        self.assertEqual([p.name for p in self.run_dir.iterdir()], ["f"])

    def test_file_mode_is_0644_minus_umask(self):
        old = os.umask(0o022)
        self.addCleanup(os.umask, old)
        sw.atomic_write(self.run_dir, self.run_dir / "f", "x")
        self.assertEqual(stat.S_IMODE((self.run_dir / "f").stat().st_mode), 0o644)

    def test_create_dirs_makes_nested_dirs_0755(self):
        old = os.umask(0o022)
        self.addCleanup(os.umask, old)
        target = self.run_dir / "x" / "y" / "f"
        sw.atomic_write(self.run_dir, target, "v", create_dirs=True)
        self.assertEqual(target.read_text(), "v")
        self.assertEqual(stat.S_IMODE((self.run_dir / "x").stat().st_mode), 0o755)

    def test_missing_parent_without_create_dirs_raises(self):
        with self.assertRaises(FileNotFoundError):
            sw.atomic_write(self.run_dir, self.run_dir / "no" / "f", "v")

    def test_symlinked_subdir_refused(self):
        os.symlink(self.out, self.run_dir / "sub")
        with self.assertRaises(sw.SymlinkRefused):
            sw.atomic_write(self.run_dir, self.run_dir / "sub" / "f", "v", create_dirs=True)
        self.assertEqual(list(self.out.iterdir()), [])

    def test_symlinked_final_path_is_replaced_not_followed(self):
        victim = self.out / "victim"
        victim.write_text("keep")
        os.symlink(victim, self.run_dir / "f")
        sw.atomic_write(self.run_dir, self.run_dir / "f", "new")
        self.assertEqual(victim.read_text(), "keep")
        self.assertFalse((self.run_dir / "f").is_symlink())
        self.assertEqual((self.run_dir / "f").read_text(), "new")

    def test_planted_temp_symlinks_are_not_followed(self):
        victim = self.out / "victim"
        victim.write_text("keep")
        real = os.urandom
        names = iter([b"\x01" * 6, b"\x02" * 6])
        for b in (b"\x01" * 6,):
            os.symlink(victim, self.run_dir / f".f.{b.hex()}.tmp")
        os.urandom = lambda n: next(names)
        self.addCleanup(setattr, os, "urandom", real)
        sw.atomic_write(self.run_dir, self.run_dir / "f", "new")
        self.assertEqual(victim.read_text(), "keep")
        self.assertEqual((self.run_dir / "f").read_text(), "new")

    def test_all_temp_names_planted_refused(self):
        real = os.urandom
        os.urandom = lambda n: b"\x07" * 6
        self.addCleanup(setattr, os, "urandom", real)
        os.symlink(self.out / "nowhere", self.run_dir / (".f." + "07" * 6 + ".tmp"))
        with self.assertRaises(sw.SymlinkRefused):
            sw.atomic_write(self.run_dir, self.run_dir / "f", "v")

    def test_path_outside_root_or_dotdot_or_root_itself_refused(self):
        for bad in (self.out / "f", self.run_dir / ".." / "f", self.run_dir):
            with self.assertRaises(OSError):
                sw.atomic_write(self.run_dir, bad, "v")


class OpenDirTest(Base):
    def test_returns_fd_for_root_and_subdir(self):
        (self.run_dir / "d").mkdir()
        for p in (self.run_dir, self.run_dir / "d"):
            fd = sw.open_dir(self.run_dir, p)
            self.addCleanup(os.close, fd)
            self.assertTrue(stat.S_ISDIR(os.fstat(fd).st_mode))

    def test_symlinked_component_refused(self):
        os.symlink(self.out, self.run_dir / "d")
        with self.assertRaises(sw.SymlinkRefused):
            sw.open_dir(self.run_dir, self.run_dir / "d")

    def test_file_component_refused(self):
        (self.run_dir / "d").write_text("x")
        with self.assertRaises(sw.SymlinkRefused):
            sw.open_dir(self.run_dir, self.run_dir / "d")

    def test_create_dirs_creates_and_is_idempotent(self):
        for _ in range(2):
            os.close(sw.open_dir(self.run_dir, self.run_dir / "a" / "b", create_dirs=True))
        self.assertTrue((self.run_dir / "a" / "b").is_dir())

    def test_missing_without_create_dirs_raises(self):
        with self.assertRaises(FileNotFoundError):
            sw.open_dir(self.run_dir, self.run_dir / "a")


class OpenAppendTest(Base):
    def test_creates_and_appends(self):
        for text in (b"one\n", b"two\n"):
            with sw.open_append(self.run_dir, self.run_dir / "worker.log") as fh:
                fh.write(text)
        self.assertEqual((self.run_dir / "worker.log").read_bytes(), b"one\ntwo\n")

    def test_symlinked_leaf_refused_and_target_untouched(self):
        victim = self.out / "victim"
        victim.write_text("keep")
        os.symlink(victim, self.run_dir / "worker.log")
        with self.assertRaises(sw.SymlinkRefused):
            sw.open_append(self.run_dir, self.run_dir / "worker.log")
        self.assertEqual(victim.read_text(), "keep")

    def test_symlinked_parent_refused(self):
        os.symlink(self.out, self.run_dir / "sub")
        with self.assertRaises(sw.SymlinkRefused):
            sw.open_append(self.run_dir, self.run_dir / "sub" / "worker.log")
        self.assertEqual(list(self.out.iterdir()), [])

    def test_path_outside_root_refused(self):
        with self.assertRaises(OSError):
            sw.open_append(self.run_dir, self.out / "worker.log")


if __name__ == "__main__":
    unittest.main()
