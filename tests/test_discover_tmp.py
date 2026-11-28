"""t_dd689940 Z1/Z2 for tests/discover_tmp.py: a single-module `discover -p` run (mutation_check's
run_discover) leaves nothing in the caller's $TMPDIR. Oracle 1 drives the helper directly; oracle 2
runs the real run_discover on a fixture module that mkdtemps (read-only tree included) and counts
what is left in a scratch $TMPDIR."""
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import discover_tmp  # noqa: E402
import mutation_check as mc  # noqa: E402

LEAKY = (
    "import os, tempfile, unittest\n\n\n"
    "class Leaky(unittest.TestCase):\n"
    "    def test_leaks(self):\n"
    "        d = tempfile.mkdtemp()\n"
    "        os.makedirs(os.path.join(d, 'ro'))\n"
    "        os.chmod(os.path.join(d, 'ro'), 0o500)\n"
    "        self.assertTrue(os.path.isdir(d))\n")


class DiscoverTmp(unittest.TestCase):
    def setUp(self):
        self._scratch = tempfile.TemporaryDirectory()
        self.addCleanup(self._scratch.cleanup)
        self.scratch = Path(self._scratch.name)
        old = os.environ.get("TMPDIR")
        self.addCleanup(lambda: os.environ.__setitem__("TMPDIR", old) if old is not None
                        else os.environ.pop("TMPDIR", None))
        os.environ["TMPDIR"] = str(self.scratch)
        tempfile.tempdir = None  # gettempdir() re-reads $TMPDIR for this test
        self.addCleanup(setattr, tempfile, "tempdir", None)

    def test_fn_sees_a_private_root_that_is_gone_and_env_restored_after(self):
        seen = discover_tmp.run(lambda: os.environ["TMPDIR"])
        self.assertNotEqual(seen, str(self.scratch))
        self.assertEqual(Path(seen).parent, self.scratch)
        self.assertEqual(list(self.scratch.iterdir()), [])
        self.assertEqual(os.environ["TMPDIR"], str(self.scratch))

    def test_root_is_swept_and_env_restored_when_fn_raises(self):
        def boom():
            os.makedirs(os.path.join(os.environ["TMPDIR"], "ro"))
            os.chmod(os.path.join(os.environ["TMPDIR"], "ro"), 0o500)
            raise RuntimeError("x")
        with self.assertRaises(RuntimeError):
            discover_tmp.run(boom)
        self.assertEqual(list(self.scratch.iterdir()), [])
        self.assertEqual(os.environ["TMPDIR"], str(self.scratch))

    def test_run_discover_of_one_module_leaves_no_temp_dirs(self):
        with tempfile.TemporaryDirectory(prefix="dt-fixture-") as repo:
            tests = Path(repo) / "tests"
            tests.mkdir()
            (tests / "test_leaky.py").write_text(LEAKY)
            rc, out = mc.run_discover(Path(repo), tests, "test_leaky.py", ["Leaky.test_leaks"])
            self.assertEqual(rc, 0, out)
            self.assertIn("OK", out)
        self.assertEqual(list(self.scratch.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
