"""R13: the blind harness API (`run`, `read`, `tmp`; blind spec 3.3), called in-process against a
temp workspace."""
import os
import tempfile
import unittest
from pathlib import Path

from capsule.blind import harness
from capsule.blind.harness import api


class Api(unittest.TestCase):
    def setUp(self):
        self.ws = Path(tempfile.mkdtemp())
        (self.ws / "f.txt").write_text("hello\n")
        old = os.environ.get(api.WORKSPACE_ENV)
        os.environ[api.WORKSPACE_ENV] = str(self.ws)
        self.addCleanup(lambda: os.environ.pop(api.WORKSPACE_ENV, None) if old is None
                        else os.environ.__setitem__(api.WORKSPACE_ENV, old))
        api.take_observed()

    def test_run_executes_in_the_workspace_and_records_the_observed_triple(self):
        done = harness.run("cat f.txt; echo oops >&2; exit 3")
        self.assertEqual((done.exit, done.stdout, done.stderr), (3, "hello\n", "oops\n"))
        self.assertEqual(api.take_observed(), {"cmd": "cat f.txt; echo oops >&2; exit 3", "exit": 3,
                                               "stdout_tail": "hello", "stderr_tail": "oops"})
        self.assertIsNone(api.take_observed())       # taken once

    def test_stdin_and_env_reach_the_command(self):
        done = harness.run("cat; echo $V", stdin="in\n", env={"V": "v"})
        self.assertEqual(done.stdout, "in\nv\n")

    def test_the_product_does_not_inherit_pythonpath(self):
        os.environ["PYTHONPATH"] = "/blind"
        self.addCleanup(os.environ.pop, "PYTHONPATH", None)
        self.assertEqual(harness.run("echo [$PYTHONPATH]").stdout, "[]\n")

    def test_tails_are_bounded(self):
        harness.run("seq 1 100; python3 -c \"print('x' * 5000)\"")
        seen = api.take_observed()["stdout_tail"]
        self.assertLessEqual(len(seen), api.TAIL_CHARS)
        self.assertLessEqual(len(seen.splitlines()), api.TAIL_LINES)
        harness.run("seq 1 100")
        self.assertEqual(api.take_observed()["stdout_tail"].splitlines()[-1], "100")

    def test_timeout_kills_the_command_and_reports_124(self):
        done = harness.run("sleep 5", timeout=0.2)
        self.assertEqual(done.exit, 124)
        self.assertIn("timed out", done.stderr)

    def test_a_signal_death_stays_inside_the_schema_range(self):
        harness.run("kill -9 $$")
        self.assertGreaterEqual(api.take_observed()["exit"], -255)

    def test_read_is_confined_to_the_workspace_and_tmp_dirs(self):
        self.assertEqual(harness.read("f.txt"), "hello\n")
        scratch = harness.tmp()
        (scratch / "g").write_text("g")
        self.assertEqual(harness.read(scratch / "g"), "g")
        outside = Path(tempfile.mkdtemp()) / "o"
        outside.write_text("o")
        for path in (outside, "../" + outside.parent.name + "/o", "/etc/hosts"):
            with self.subTest(path=str(path)), self.assertRaises(ValueError):
                harness.read(path)

    def test_read_refuses_a_symlink_that_leaves_the_workspace(self):
        outside = Path(tempfile.mkdtemp()) / "o"
        outside.write_text("o")
        (self.ws / "link").symlink_to(outside)
        with self.assertRaises(ValueError):
            harness.read("link")

    def test_tmp_dirs_are_distinct_and_exist(self):
        a, b = harness.tmp(), harness.tmp()
        self.assertNotEqual(a, b)
        self.assertTrue(a.is_dir() and b.is_dir())


if __name__ == "__main__":
    unittest.main()
