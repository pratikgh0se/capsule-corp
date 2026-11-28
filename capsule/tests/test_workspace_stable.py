"""A reuse of an epic clone leaves the files a container reads untouched (Docker Desktop's file
share can serve stale entries for a file the host deleted and recreated, 2026-10-31), but still
repairs a `.git` that is wrong, a link, or anything else a run left there. Temp repos only."""
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from capsule import workspace

BRANCH = "main"


class StableReuse(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.origin = self.tmp / "origin"
        self.origin.mkdir()
        (self.origin / "README.md").write_text("one\n")
        for args in (("init", "-q", "-b", BRANCH), ("add", "."), ("commit", "-qm", "init")):
            subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t.test", *args],
                           cwd=self.origin, check=True, capture_output=True, timeout=30)
        self.root = self.tmp / "work"
        self.ws = workspace.clone(str(self.origin), BRANCH, self.root)
        self.dot = self.ws / ".git"
        self.want = self.dot.read_text()

    def _reuse(self):
        return workspace.clone(str(self.origin), BRANCH, self.root)

    def _checkouts(self):
        with mock.patch.object(workspace, "_git", side_effect=workspace._git) as spy:
            self._reuse()
        return [c for c in spy.call_args_list if c.args[0] == "checkout"]

    def test_two_reuses_leave_the_git_pointer_and_head_files_as_the_same_inodes(self):
        head = workspace.gitdir(self.ws) / "HEAD"
        before = (os.lstat(self.dot).st_ino, os.lstat(head).st_ino)
        for _ in range(2):
            self._reuse()
        self.assertEqual((os.lstat(self.dot).st_ino, os.lstat(head).st_ino), before)
        self.assertEqual(self.dot.read_text(), self.want)

    def test_a_reuse_runs_no_checkout_on_the_branch_and_one_when_detached(self):
        self.assertEqual(self._checkouts(), [])
        sha = workspace.head(self.ws)
        subprocess.run(["git", "--git-dir", str(workspace.gitdir(self.ws)), "update-ref",
                        "--no-deref", "HEAD", sha], check=True, capture_output=True, timeout=30)
        self.assertEqual(len(self._checkouts()), 1)
        self.assertEqual(workspace._git("symbolic-ref", "HEAD", ws=self.ws).stdout.strip(),
                         f"refs/heads/{BRANCH}")

    def test_a_symbolic_ref_error_that_is_not_a_detached_head_propagates(self):
        real = workspace._git
        calls = []

        def flaky(*args, **kw):
            calls.append(args[0])
            if args[0] == "symbolic-ref":
                raise RuntimeError("git symbolic-ref failed (rc=128): fatal: boom")
            return real(*args, **kw)

        with mock.patch.object(workspace, "_git", side_effect=flaky):
            with self.assertRaisesRegex(RuntimeError, "rc=128"):
                self._reuse()
        self.assertNotIn("checkout", calls)

    def test_a_fifo_at_the_git_pointer_never_blocks_holds_and_is_replaced(self):
        import threading
        self.dot.unlink()
        os.mkfifo(self.dot)
        out = []
        t = threading.Thread(target=lambda: out.append(
            (workspace._holds(self.dot, self.want), self._reuse())), daemon=True)
        t.start()
        t.join(20)
        if t.is_alive():   # unstick a reader blocked on the FIFO before failing
            os.close(os.open(self.dot, os.O_WRONLY | os.O_NONBLOCK))
            t.join(5)
            self.fail("_holds or the reuse blocked on a FIFO at ws/.git")
        self.assertEqual(out[0][0], False)
        self.assertTrue(stat.S_ISREG(os.lstat(self.dot).st_mode))
        self.assertEqual(self.dot.read_text(), self.want)

    def test_a_changed_or_wrong_pointer_is_still_rewritten(self):
        for bad in (f"gitdir: /elsewhere/{self.ws.name}\n", self.want.rstrip("\n"),
                    self.want + "x\n", ""):
            self.dot.write_text(bad)
            self._reuse()
            self.assertEqual(self.dot.read_text(), self.want, bad)
            self.assertTrue(stat.S_ISREG(os.lstat(self.dot).st_mode))

    def test_a_symlink_at_the_git_pointer_is_replaced_even_when_it_reads_right(self):
        target = self.tmp / "pointer-copy"
        target.write_text(self.want)
        self.dot.unlink()
        self.dot.symlink_to(target)
        self._reuse()
        self.assertTrue(stat.S_ISREG(os.lstat(self.dot).st_mode))   # a regular file, not the link
        self.assertEqual(self.dot.read_text(), self.want)


if __name__ == "__main__":
    unittest.main()
