"""ENG-12 review follow-ups: what a sandboxed command leaves running, and the tick's reap of it
(exec server group kill, then LightRunner.reap by sandbox_check). Live cases need the real
`sandbox-exec`; fixtures come from test_light_runner."""
import os
import shutil
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

from capsule import paths
from capsule.runners import exec_mcp, host, runner_for, scoped
from capsule.tests.test_light_runner import LiveCase


def gone(pid, tries=50):
    """True once `pid` no longer exists (polled for tries x 0.1 s)."""
    for _ in range(tries):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.1)
    return False


class Leftovers(LiveCase):
    """What a command leaves running: the exec server's group kill, then the tick's reap."""

    RUN = "r" * 16

    def setUp(self):
        super().setUp()
        self.runner.board, self.runner.home = "b", Path(self.root, "home")
        self.spool = str(paths.board_dir("b", self.runner.home) / "spool" / self.RUN)

    def pid(self, name):
        for _ in range(100):
            if os.path.exists(os.path.join(self.wt, name)):
                return int(Path(self.wt, name).read_text())
            time.sleep(0.05)
        self.fail(f"{name} never written")

    def test_a_background_child_in_the_group_dies_with_its_command(self):
        self.assertEqual(self.exec("sleep 60 & echo $! > bg.pid")["exit_code"], 0)
        self.assertTrue(gone(self.pid("bg.pid")))

    def test_a_timeout_kills_the_grandchildren_too(self):
        self.assertEqual(self.exec("sleep 55 & echo $! > gc.pid; sleep 55", 1)["exit_code"], -1)
        self.assertTrue(gone(self.pid("gc.pid")))

    def setsid_child(self, name, argv=""):
        """A sandboxed child that left the command's group; returns its pid. It waits for the pid
        file, written after setsid: until then the child is still in the group, which the exec
        server kills when the command ends."""
        child = (f"(python3 -c 'import os,time; os.setsid(); "
                 f"open(\"{name}\", \"w\").write(str(os.getpid())); time.sleep(60)' {argv})&"
                 f" while [ ! -s {name} ]; do sleep 0.1; done")
        self.assertEqual(self.exec(child)["exit_code"], 0)    # returns at once: output is a file
        return self.pid(name)

    def test_a_setsid_child_outlives_the_command_and_dies_in_the_reap(self):
        pid = self.setsid_child("child.pid")
        self.assertEqual(self.runner.reap({self.RUN}), [])    # its run is live: left alone
        self.assertFalse(gone(pid, 3))
        self.assertIn(pid, self.runner.reap(set()))
        self.assertTrue(gone(pid))

    def test_every_pass_rescans_the_worktree(self):
        first = self.setsid_child("one.pid")
        self.assertIn(first, self.runner.reap(set()))
        second = self.setsid_child("two.pid")                 # started after the first reap
        self.assertIn(second, self.runner.reap(set()))
        self.assertTrue(gone(second))
        self.assertEqual(self.runner.reap(set()), [])
        shutil.rmtree(self.wt)                                # worktree gone: nothing to scan
        self.assertEqual(self.runner.reap(set()), [])

    def test_a_sandboxed_child_faking_a_live_workers_argv_is_still_killed(self):
        pid = self.setsid_child("fake.pid", "run-one other-live-run")
        self.assertIn(pid, self.runner.reap({"other-live-run"}))   # its own run has ended
        self.assertTrue(gone(pid))

    def test_a_worktree_a_live_run_uses_is_spared_by_its_spool_marker(self):
        pid = self.setsid_child("keep.pid")
        later = Path(self.spool).parent / "r2-live"           # a live run on the same worktree
        later.mkdir(parents=True)
        (later / "worktree").write_text(self.wt)
        self.assertEqual(self.runner.reap({"r2-live"}), [])
        self.assertFalse(gone(pid, 3))
        self.assertIn(pid, self.runner.reap(set()))

    def test_an_unsandboxed_process_in_the_worktree_survives(self):
        """The user's own process (an editor, a shell, a reviewer's claude) is never touched."""
        self.launch()
        plain = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                                 cwd=self.wt, start_new_session=True)
        self.addCleanup(plain.wait)
        self.addCleanup(plain.terminate)
        self.assertNotIn(plain.pid, self.runner.reap(set()))
        time.sleep(0.3)
        self.assertIsNone(plain.poll())

    def test_the_tick_homes_spools_are_the_ones_reaped(self):
        """`scoped` hands the tick's home to the light runner, whatever CAPSULE_HOME says."""
        tick_home = Path(self.root, "tick-home")
        runner = scoped(runner_for, lambda: {"board": "b"}, tick_home)("light", "implementer")
        sp = paths.board_dir("b", tick_home) / "spool" / "r1"
        sp.mkdir(parents=True)
        (sp / "worktree").write_text(self.wt)
        with mock.patch.dict(os.environ, {"CAPSULE_HOME": os.path.join(self.root, "elsewhere")}), \
                mock.patch.object(host, "_kill_under", return_value=[5]) as kill:
            self.assertEqual(runner.reap(set()), [5])
        kill.assert_called_once_with(self.wt)

    def test_output_is_cut_at_the_cap(self):
        self.assertEqual(exec_mcp.MAX_OUTPUT, 10 * 1024 * 1024)
        with mock.patch.object(exec_mcp, "MAX_OUTPUT", 100):
            out = self.exec("yes x | head -c 5000")
            short = self.exec("echo hi")
        self.assertEqual(out["exit_code"], 0)
        self.assertEqual(out["text"].count("x"), 50)           # "x\n" x 50 = 100 bytes kept
        self.assertTrue(out["text"].endswith("[output cut at 100 bytes]"))
        self.assertEqual(short["text"], "hi\n")


if __name__ == "__main__":
    unittest.main()
