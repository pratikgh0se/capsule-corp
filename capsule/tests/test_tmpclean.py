"""t_b346fd02: a suite run leaves nothing in $TMPDIR. Two oracles (Z2): a child suite that
leaks (module-level mkdtemp, mkstemp, a read-only tree) leaves 0 entries with the sweep
stub and >0 without it; and every real suite dir carries the stub, sorted first."""
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from capsule.tests import tmpclean
from capsule.tests.public_tree import private_only

REPO = Path(__file__).resolve().parents[2]
SUITES = ("capsule/tests", "maintenance/tests", "tests")
STUB = "test_0_tmpsweep.py"

LEAKER = """
    import os, tempfile
    from pathlib import Path
    d = Path(tempfile.mkdtemp(prefix="leak-"))
    (d / "report").mkdir()
    (d / "report" / "x.txt").write_text("x")
    os.chmod(d / "report", 0o500)
    os.close(tempfile.mkstemp(prefix="leak-")[0])
    import unittest
    class T(unittest.TestCase):
        def test_it(self):
            Path(tempfile.gettempdir(), "no-send.jsonl").write_text("{}")
"""
STUB_SRC = f"""
    import sys
    sys.path.insert(0, {str(REPO)!r})
    from capsule.tests import tmpclean
    tmpclean.install()
"""


TICK_CHILD = """
    import sys
    sys.path.insert(0, {repo!r})
    from pathlib import Path
    from capsule import prior_runs
    from capsule.tests.test_tick import TickCase
    class T(TickCase):
        def test_mirror(self):
            src = self.home / "spool" / "r1"
            src.mkdir(parents=True)
            (src / "worker.log").write_text("x")
            prior_runs._mirror([{{"run_id": "r1", "paths": [str(src), str(src / "worker.log")]}}],
                               self.home / "prior")
    del TickCase
"""


def _run_child(with_stub, tmp_parent):
    suite = Path(tempfile.mkdtemp(dir=tmp_parent))
    (suite / "test_leak.py").write_text(textwrap.dedent(LEAKER))
    if with_stub:
        (suite / STUB).write_text(textwrap.dedent(STUB_SRC))
    out = Path(tempfile.mkdtemp(dir=tmp_parent))
    proc = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", str(suite)],
                          env={**os.environ, "TMPDIR": str(out)}, capture_output=True, text=True)
    return proc, sorted(os.listdir(out))


class SuiteLeavesNoTemp(unittest.TestCase):
    def setUp(self):
        self.parent = tempfile.mkdtemp()
        self.addCleanup(tmpclean.sweep, self.parent)

    def test_stub_suite_leaves_zero_entries_even_read_only(self):
        proc, left = _run_child(True, self.parent)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(left, [])

    def test_tickcase_teardown_removes_read_only_prior_mirror(self):
        suite = Path(tempfile.mkdtemp(dir=self.parent))
        (suite / "test_child.py").write_text(textwrap.dedent(TICK_CHILD).format(repo=str(REPO)))
        out = Path(tempfile.mkdtemp(dir=self.parent))
        proc = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", str(suite)],
                              env={**os.environ, "TMPDIR": str(out)}, cwd=REPO,
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("Ran 1 test", proc.stderr)
        self.assertEqual(os.listdir(out), [])

    def test_control_without_stub_leaks(self):
        proc, left = _run_child(False, self.parent)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertGreaterEqual(len(left), 3)  # mkdtemp dir, mkstemp file, no-send.jsonl

    def test_every_real_suite_carries_the_stub_first(self):
        for suite in SUITES:
            names = sorted(p.name for p in (REPO / suite).glob("test*.py"))
            self.assertEqual(names[0], STUB, suite)

    def _script(self, body, tmpdir=None, cwd=None, pre=""):
        out = Path(tempfile.mkdtemp(dir=self.parent))
        env = {**os.environ, "TMPDIR": str(out) if tmpdir is None else tmpdir}
        code = f"import sys, tempfile; sys.path.insert(0, {str(REPO)!r})\n{pre}\n" \
               "from capsule.tests import tmpclean\nroot = tmpclean.install()\n" \
               + textwrap.dedent(body)
        proc = subprocess.run([sys.executable, "-c", code], env=env, cwd=cwd or self.parent,
                              capture_output=True, text=True)
        return proc, out

    def test_forked_child_exit_leaves_the_parents_root(self):
        proc, out = self._script("""
            import os
            pid = os.fork()
            if pid == 0:
                sys.exit(0)
            os.waitpid(pid, 0)
            print(os.path.isdir(root))
        """)
        self.assertEqual(proc.stdout.strip(), "True", proc.stderr)
        self.assertEqual(os.listdir(out), [])  # the parent's own exit still swept it

    def test_symlinks_out_of_the_root_are_not_followed(self):
        outside = Path(tempfile.mkdtemp(dir=self.parent))
        outside.chmod(0o755)
        proc, out = self._script(f"""
            import os
            os.symlink({str(outside)!r}, os.path.join(root, "link"))
            os.symlink("/usr/bin", os.path.join(root, "sys"))
        """)
        self.assertEqual(proc.stderr, "")
        self.assertEqual(os.listdir(out), [])
        self.assertEqual(outside.stat().st_mode & 0o777, 0o755)

    def test_root_already_removed_exits_quietly(self):
        proc, out = self._script("import shutil; shutil.rmtree(root)")
        self.assertEqual((proc.returncode, proc.stderr), (0, ""))

    def test_empty_tmpdir_never_plants_the_root_in_the_cwd(self):
        cwd = Path(tempfile.mkdtemp(dir=self.parent))
        proc, _ = self._script("print(root)", tmpdir="", cwd=cwd)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(os.listdir(cwd), [])
        self.assertFalse(Path(proc.stdout.strip()).is_relative_to(cwd))

    def test_early_gettempdir_does_not_defeat_the_redirect(self):
        proc, out = self._script("print(tempfile.mkdtemp())", pre="tempfile.gettempdir()")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(Path(proc.stdout.strip()).is_relative_to(Path(tempfile.gettempdir())))
        self.assertEqual(os.listdir(out), [])  # the root was made in the real TMPDIR and swept

    def test_each_stub_imports_from_another_cwd(self):
        for suite in SUITES:
            out = Path(tempfile.mkdtemp(dir=self.parent))
            proc = subprocess.run([sys.executable, str(REPO / suite / STUB)], cwd=self.parent,
                                  env={**os.environ, "TMPDIR": str(out)},
                                  capture_output=True, text=True)
            self.assertEqual((proc.returncode, os.listdir(out)), (0, []), suite + proc.stderr)

    def test_install_redirects_home_and_state_roots_into_the_swept_root(self):
        proc, out = self._script("""
            import os
            for k in ("HOME", "LEGACY_KANBAN_HOME", "CAPSULE_HOME", "OBSIDIAN_VAULT_PATH"):
                print(k, os.path.realpath(os.environ[k]).startswith(os.path.realpath(root)))
            print("NOSEND", os.environ["AGENT_OPS_NO_SEND"])
        """, pre="import os\nfor k in ('HOME', 'LEGACY_KANBAN_HOME', 'CAPSULE_HOME'):\n"
                 "    os.environ[k] = '/real/' + k\nos.environ['AGENT_OPS_NO_SEND'] = '0'")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        got = {k: v for k, v in (line.split()[:2] for line in proc.stdout.splitlines())}
        self.assertEqual(got, {"HOME": "True", "LEGACY_KANBAN_HOME": "True", "CAPSULE_HOME": "True",
                               "OBSIDIAN_VAULT_PATH": "True", "NOSEND": "1"})
        self.assertEqual(os.listdir(out), [])

    def test_inside_the_suite_home_is_not_the_real_one(self):
        tmpclean.install()  # idempotent; makes a run of this module alone as hermetic as discover
        self.assertTrue(os.path.realpath(os.path.expanduser("~")).startswith(
            os.path.realpath(tempfile.gettempdir())))
        self.assertTrue(os.path.realpath(os.environ["LEGACY_KANBAN_HOME"]).startswith(
            os.path.realpath(tempfile.gettempdir())))

    def test_install_repoints_exported_state_overrides_into_the_root(self):
        pre = "import os\nfor k in %r: os.environ[k] = '/real/' + k\n" % (tmpclean.OVERRIDES,)
        proc, out = self._script("""
            import os
            for k in tmpclean.OVERRIDES:
                print(k, os.path.realpath(os.environ[k]).startswith(os.path.realpath(root)))
        """, pre=pre)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        got = dict(line.split() for line in proc.stdout.splitlines())
        self.assertEqual(got, {k: "True" for k in tmpclean.OVERRIDES})


class TmpdirLayouts(unittest.TestCase):
    """inc_6bbee3e7: Verify went red on ubuntu (/tmp, no symlink).

    macOS (/var -> /private/var) passed.
    """
    MODULE = "capsule.tests.test_secret_read_guard"

    def _run(self, tmpdir):
        env = dict(os.environ, TMPDIR=str(tmpdir))
        return subprocess.run([sys.executable, "-m", "unittest", self.MODULE], cwd=REPO,
                              env=env, capture_output=True, text=True, timeout=600)

    @private_only
    def test_ci_red_module_passes_under_symlinked_and_plain_tmpdir(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as base:
            base = Path(os.path.realpath(base))
            plain = base / "plain"
            plain.mkdir()
            real = base / "real"
            real.mkdir()
            link = base / "link"
            link.symlink_to(real)
            self.assertNotEqual(os.path.realpath(link), str(link))
            for name, tmpdir in (("plain", plain), ("symlink", link)):
                with self.subTest(layout=name):
                    proc = self._run(tmpdir)
                    self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])


if __name__ == "__main__":
    unittest.main()
