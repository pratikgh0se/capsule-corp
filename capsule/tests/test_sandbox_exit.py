"""Row 632 b: the sandbox's exit codes are the command's own. A container that is gone is
"unavailable" (wait) only when the docker daemon is down too; with the daemon up it is the
command's doing (a candidate can `kill -TERM 1`), so its exit code stands and a red stays red.
Same fake docker and lander fixtures as test_sandbox."""
from pathlib import Path

from capsule.tests.test_sandbox import Boxed


class ExitCodesAreTheCommandsOwn(Boxed):
    def test_a_suite_that_exits_125_is_red_not_unavailable(self):
        """125 is what `docker run` itself returns; it is no sentinel here."""
        self.fake.exec_rc = 125
        result = self.check()
        self.assertEqual(result["verdict"], "red")
        self.assertEqual(self.status(result)["gates"], "fail")

    def test_a_container_and_daemon_both_gone_after_a_failing_command_is_unavailable(self):
        self.fake.exec_rc, self.fake.daemon_dies = 1, True
        result = self.check()
        self.assertEqual((result["verdict"], result["hold"]), ("wait", "lander_wait_docker"))
        self.assertEqual(self.status(result)["gates"], "wait")
        self.assertIn("died", self.detail(result, "gates"))

    def test_a_candidate_that_kills_its_own_container_is_red_not_a_wait(self):
        """`kill -TERM 1` in the box: the container is gone, the daemon is up: the exit stands."""
        self.fake.exec_rc, self.fake.alive = 1, False
        result = self.check()
        self.assertEqual(result["verdict"], "red")
        self.assertEqual(self.status(result)["gates"], "fail")

    def test_a_stop_that_times_out_still_returns_the_verdict_and_removes_the_copy(self):
        self.fake.exec_rc, self.fake.rm_rc = 1, None     # None: `docker rm` times out
        result = self.check()
        self.assertEqual(result["verdict"], "red")
        self.assertFalse(Path(self.fake.mount(":/workspace")).exists())

    def test_a_failing_command_in_a_live_container_is_red(self):
        self.fake.exec_rc = 1
        self.assertEqual(self.check()["verdict"], "red")
