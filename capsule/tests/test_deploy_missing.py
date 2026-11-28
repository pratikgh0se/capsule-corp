"""A public clone has no deploy.sh: the default deploy check says so, naming the config key,
instead of a shell 'No such file' line. A board's own `deploy_check` and a present deploy.sh run
as before."""
import shutil
import tempfile
import unittest
from pathlib import Path

from capsule import landing


class DeployMissing(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)

    def test_the_default_check_without_deploy_sh_drifts_with_the_clear_line(self):
        drift, tail = landing._deploy({}, self.dir)
        self.assertTrue(drift)
        self.assertEqual(tail, landing.NO_DEPLOY)
        self.assertIn("lander.deploy_check", tail)

    def test_a_board_check_runs_even_without_deploy_sh(self):
        cfg = {"lander": {"deploy_check": "echo fine"}}
        self.assertEqual(landing._deploy(cfg, self.dir)[0], False)

    def test_a_present_deploy_sh_runs(self):
        (self.dir / "deploy.sh").write_text("#!/bin/sh\necho ok\n")
        (self.dir / "deploy.sh").chmod(0o755)
        self.assertEqual(landing._deploy({}, self.dir), (False, "ok"))

    def test_a_board_check_that_fails_still_drifts_with_its_own_output(self):
        cfg = {"lander": {"deploy_check": "echo DRIFT; exit 1"}}
        drift, tail = landing._deploy(cfg, self.dir)
        self.assertTrue(drift)
        self.assertIn("DRIFT", tail)
        self.assertNotEqual(tail, landing.NO_DEPLOY)


if __name__ == "__main__":
    unittest.main()
