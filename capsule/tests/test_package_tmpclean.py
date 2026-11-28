"""t_dd689940: importing the capsule.tests package installs tmpclean, so a single-module run
(`-m unittest capsule.tests.test_x`) sweeps its temp dirs too, as `discover` already does. Two
oracles (Z2): a child that only imports the package leaves $TMPDIR empty and sees a private root;
a real single-module run leaves nothing behind."""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def _child(args, base, extra=None):
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    env["TMPDIR"] = str(base)
    env.update(extra or {})
    return subprocess.run([sys.executable, *args], cwd=REPO, env=env, capture_output=True,
                          text=True, timeout=120)

    def test_a_single_maintenance_module_run_leaves_nothing(self):
        out = _child(["-m", "unittest", "maintenance.tests.test_events"], self.base)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(list(self.base.iterdir()), [])

    def test_the_maintenance_and_tests_packages_give_a_private_root(self):
        for pkg in ("maintenance.tests", "tests"):
            code = f"import {pkg}, tempfile; tempfile.mkdtemp(); print(tempfile.gettempdir())"
            out = _child(["-c", code], self.base)
            self.assertEqual(out.returncode, 0, out.stderr)
            self.assertTrue(Path(out.stdout.strip()).name.startswith("capsule-suite-"), pkg)
            self.assertEqual(list(self.base.iterdir()), [], pkg)

    def test_a_terminated_run_is_swept_and_still_dies_of_the_signal(self):
        code = ("import capsule.tests, os, signal, tempfile; tempfile.mkdtemp(); "
                "os.kill(os.getpid(), signal.SIGTERM)")
        out = _child(["-c", code], self.base)
        self.assertEqual(out.returncode, -15)
        self.assertEqual(list(self.base.iterdir()), [])

    def test_keep_tmp_leaves_the_root_and_prints_it(self):
        out = _child(["-c", "import capsule.tests"], self.base, {"CAPSULE_TEST_KEEP_TMP": "1"})
        self.assertEqual(out.returncode, 0, out.stderr)
        kept = list(self.base.iterdir())
        self.assertEqual(len(kept), 1)
        self.assertIn(str(kept[0].name), out.stderr)


CLT = "/Library/Developer/CommandLineTools"
SYSTEM_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"   # no tmpclean link in it, as in a fresh process
WHICH_AND_EXEC = ("import capsule.tests, shutil, subprocess; print(shutil.which('git')); "
                  "print(subprocess.run(['git', '--exec-path'], capture_output=True, "
                  "text=True).stdout.strip())")


class PackageImportSweeps(unittest.TestCase):
    def setUp(self):
        self._base = tempfile.TemporaryDirectory()
        self.addCleanup(self._base.cleanup)
        self.base = Path(self._base.name)

    def test_importing_the_package_gives_a_private_root_and_leaves_nothing(self):
        code = "import capsule.tests, tempfile; tempfile.mkdtemp(); print(tempfile.gettempdir())"
        out = _child(["-c", code], self.base)
        self.assertEqual(out.returncode, 0, out.stderr)
        root = Path(out.stdout.strip())
        self.assertEqual(root.parent.resolve(), self.base.resolve())
        self.assertTrue(root.name.startswith("capsule-suite-"))
        self.assertEqual(list(self.base.iterdir()), [])

    def test_a_single_module_run_leaves_nothing(self):
        out = _child(["-m", "unittest", "capsule.tests.test_dispatch"], self.base)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(list(self.base.iterdir()), [])


@unittest.skipUnless(os.access("/usr/bin/git", os.X_OK), "no /usr/bin/git")
class GitOnPath(unittest.TestCase):
    """The macOS git shim is skipped for speed only: git must run from the same exec path, under
    whichever developer dir is selected (a link that falls back to Xcode's exec path cannot read
    its config in the light-tier sandbox when DEVELOPER_DIR is the Command Line Tools)."""

    def setUp(self):
        self._base = tempfile.TemporaryDirectory()
        self.addCleanup(self._base.cleanup)

    def probe(self, **env):
        env = {"PATH": SYSTEM_PATH, **env}
        out = _child(["-c", WHICH_AND_EXEC], self._base.name, env)
        self.assertEqual(out.returncode, 0, out.stderr)
        which, exec_path = out.stdout.split("\n")[:2]
        shim = subprocess.run(["/usr/bin/git", "--exec-path"], capture_output=True, text=True,
                              env={**os.environ, **env}).stdout.strip()
        return which, exec_path, shim

    def test_the_exec_path_is_the_shims_under_the_default_developer_dir(self):
        which, exec_path, shim = self.probe()
        self.assertEqual(exec_path, shim)
        if sys.platform == "darwin" and shim.startswith("/Applications/Xcode"):
            self.assertNotEqual(which, "/usr/bin/git")   # the shim is skipped where that is safe

    def test_the_exec_path_is_the_shims_under_the_command_line_tools(self):
        if not os.access(CLT + "/usr/bin/git", os.X_OK):
            self.skipTest("no Command Line Tools")
        _, exec_path, shim = self.probe(DEVELOPER_DIR=CLT)
        self.assertEqual(exec_path, shim)
        self.assertTrue(exec_path.startswith(CLT), exec_path)

    def test_a_set_exec_path_leaves_git_as_it_is(self):
        which, exec_path, shim = self.probe(GIT_EXEC_PATH=self.probe()[2])
        self.assertEqual(which, "/usr/bin/git")
        self.assertEqual(exec_path, shim)


if __name__ == "__main__":
    unittest.main()
