"""bootstrap.lock must stay a tracked, un-ignored file (t_431a0517, row 465): bootstrap.sh sources it
directly, and a fresh `git worktree add` only materializes tracked files, so if it is ever dropped from
git or re-caught by the `*.lock` glob, every nested worktree checkout of bootstrap.sh breaks again with
no signal anywhere else in CI (test_bootstrap.sh itself is macOS-only and excluded from CI)."""
import subprocess
import unittest

from capsule import paths
from capsule.tests.public_tree import private_only


class BootstrapLockTracked(unittest.TestCase):
    @private_only
    def test_bootstrap_lock_is_tracked(self):
        rc = subprocess.run(
            ["git", "ls-files", "--error-unmatch", "bootstrap.lock"],
            cwd=paths.REPO, capture_output=True, text=True,
        ).returncode
        self.assertEqual(rc, 0, "bootstrap.lock must be tracked by git (t_431a0517)")

    def test_bootstrap_lock_is_not_gitignored(self):
        rc = subprocess.run(
            ["git", "check-ignore", "--no-index", "-q", "bootstrap.lock"],
            cwd=paths.REPO, capture_output=True, text=True,
        ).returncode
        self.assertEqual(rc, 1, "bootstrap.lock must not be caught by .gitignore's *.lock rule (t_431a0517)")


if __name__ == "__main__":
    unittest.main()
