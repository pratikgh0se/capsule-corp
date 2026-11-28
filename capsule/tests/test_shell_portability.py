"""Shell portability lint (inc_456ee9dc).

GNU `stat -f` means --file-system: it prints a filesystem block to stdout and
then fails on the format string, so a `|| stat -c` fallback appends to that text.
The fallback order must be GNU first (see perm() in tests/test_deploy.sh).
"""
import re
import subprocess
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
BSD_FIRST = re.compile(r"\bstat\s+-f\b.*\|\|\s*stat\s+-c\b")


class StatOrder(unittest.TestCase):
    def test_no_bsd_first_stat_fallback(self):
        out = subprocess.run(["git", "ls-files", "*.sh"], cwd=REPO, check=True,
                             capture_output=True, text=True).stdout.split()
        self.assertTrue(out, "git ls-files '*.sh' returned no files")
        bad = []
        for rel in out:
            text = (REPO / rel).read_text(errors="replace")
            for n, line in enumerate(text.splitlines(), 1):
                if BSD_FIRST.search(line):
                    bad.append(f"{rel}:{n}")
        self.assertEqual(
            bad, [], "BSD-first `stat -f ... || stat -c`; use a GNU-first perm() helper")


if __name__ == "__main__":
    unittest.main()
