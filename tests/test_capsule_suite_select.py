"""bin/capsule-suite --select BASE: only the tests the diff needs; FULL keeps the discovery."""
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def git(d, *a):
    return subprocess.run(["git", "-C", d, "-c", "user.name=t", "-c", "user.email=t@t.invalid", *a],
                          check=True, capture_output=True, text=True).stdout.strip()


class Select(unittest.TestCase):
    def suite(self, change):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        d = Path(tmp.name)
        case = "import unittest\nclass T(unittest.TestCase):\n    def test_a(self):\n        pass\n"
        files = {"capsule/tests/__init__.py": "", "capsule/__init__.py": "",
                 "capsule/bar.py": "Y = 1\n", "capsule/paths.py": "P = 1\n",
                 "capsule/tests/test_bar.py": case, "capsule/tests/test_other.py": case,
                 "tests/select_tests.py": (ROOT / "tests/select_tests.py").read_text()}
        for name, text in files.items():
            (d / name).parent.mkdir(parents=True, exist_ok=True)
            (d / name).write_text(text)
        git(d, "init", "-q")
        git(d, "add", "-A")
        git(d, "commit", "-qm", "base")
        base = git(d, "rev-parse", "HEAD")
        (d / change).parent.mkdir(parents=True, exist_ok=True)
        (d / change).write_text("changed = 1\n")
        git(d, "add", "-A")
        git(d, "commit", "-qm", "change")
        out = subprocess.run([sys.executable, str(ROOT / "bin/capsule-suite"), "--select", base,
                              "--lock-dir", str(d / "lock"), "--max-wait", "0"], cwd=d,
                             capture_output=True, text=True)
        return out.returncode, out.stdout + out.stderr

    def test_a_small_diff_runs_only_its_test(self):
        rc, text = self.suite("capsule/bar.py")
        self.assertEqual(rc, 0, text)
        self.assertIn("Ran 1 test", text)

    def test_a_shared_helper_runs_the_whole_discovery(self):
        rc, text = self.suite("capsule/paths.py")
        self.assertEqual(rc, 0, text)
        self.assertIn("Ran 2 tests", text)

    def test_a_prose_only_diff_runs_nothing(self):
        rc, text = self.suite("docs/notes.md")
        self.assertEqual(rc, 0, text)
        self.assertIn("no tests selected", text)

    def test_a_markdown_file_outside_docs_runs_the_whole_discovery(self):
        rc, text = self.suite("README.md")
        self.assertEqual(rc, 0, text)
        self.assertIn("Ran 2 tests", text)


if __name__ == "__main__":
    unittest.main()
