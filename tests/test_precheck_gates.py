"""bin/precheck-gates.py: a Z8 violation in an uncommitted change is reported, and the
repo's branch, HEAD, index and refs stay untouched, run after run. The fixture repo carries
the real mutation gate and its simplicity pass."""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "bin" / "precheck-gates.py"
REAL = ("simplicity_check.py", "size_check.py", "size_baseline.py", "size_baseline.json",
        "mutation_check.py", "mutation_newsymbol.py", "mutation_contractdata.py",
        "mutation_info.py", "mutation_alias.py", "mutation_literals.py",
        "discover_tmp.py", "mutation_env.py")


def git(cwd, *args, env=None):
    p = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, env=env)
    assert p.returncode == 0, f"git {args} failed: {p.stdout}{p.stderr}"
    return p.stdout


class PrecheckGates(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = Path(tmp.name) / "work"
        self.repo.mkdir()
        self.gitdir = Path(tmp.name) / "gitdir"
        git(self.repo, "init", "-q", "-b", "main", "--separate-git-dir", str(self.gitdir))
        git(self.repo, "config", "user.email", "t@example.com")
        git(self.repo, "config", "user.name", "T")
        (self.repo / "tests").mkdir()
        for name in REAL:
            shutil.copy(ROOT / "tests" / name, self.repo / "tests" / name)
        (self.repo / "mod.py").write_text("x = 0\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "base")
        self.base = git(self.repo, "rev-parse", "HEAD").strip()
        (self.repo / ".git").unlink()    # like the worker workspace: no .git, env names the dir
        self.env = dict(os.environ, GIT_DIR=str(self.gitdir), GIT_WORK_TREE=str(self.repo))

    def state(self):
        return [git(self.repo, *a, env=self.env) for a in (
            ("rev-parse", "HEAD"), ("for-each-ref",), ("status", "--porcelain"),
            ("stash", "list"))]

    def run_script(self):
        return subprocess.run([sys.executable, str(SCRIPT), "--base", self.base,
                               "--repo", str(self.repo)], capture_output=True, text=True,
                              env=self.env)

    def test_clean_tree_passes(self):
        p = self.run_script()
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)

    def test_worker_exec_scratch_is_ignored(self):
        scratch = self.repo / ".worker-exec"
        scratch.mkdir()
        (scratch / "x.py").write_text("y = '" + "a" * 120 + "'\n")
        p = self.run_script()
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)

    def test_nested_repo_is_skipped_without_crash(self):
        (self.repo / "inner").mkdir()
        git(self.repo / "inner", "init", "-q")    # listed as a directory: skipped, no crash
        p = self.run_script()
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)

    def test_uncommitted_violation_fails_and_repo_is_untouched(self):
        (self.repo / "mod.py").write_text("x = 0\ny = '" + "a" * 120 + "'\n")
        (self.repo / "new.py").write_text("z = '" + "b" * 120 + "'\n")
        before = self.state()
        first, second = self.run_script(), self.run_script()
        self.assertEqual(first.returncode, 1, first.stdout + first.stderr)
        self.assertIn("rule 7", first.stdout)
        self.assertIn("mod.py", first.stdout)
        self.assertIn("new.py", first.stdout)    # an untracked file reaches the snapshot
        self.assertEqual(second.returncode, 1, second.stdout + second.stderr)
        self.assertEqual(self.state(), before)

    def test_gate_timeout_returns_1_with_a_message(self):
        import contextlib
        import importlib.util
        import io
        spec = importlib.util.spec_from_file_location("precheck_gates", SCRIPT)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        real = subprocess.run

        def slow(cmd, *a, **kw):
            if cmd[0] == sys.executable:
                self.assertEqual(kw.get("timeout"), 600)
                raise subprocess.TimeoutExpired(cmd, kw["timeout"])
            return real(cmd, *a, **kw)
        out = io.StringIO()
        with unittest.mock.patch.object(mod.subprocess, "run", slow), \
                unittest.mock.patch.dict(os.environ, self.env), \
                contextlib.redirect_stdout(out):
            mod.CLEAN_ENV = {k: v for k, v in os.environ.items() if k not in mod.GIT_ENV}
            rc = mod.main(["--base", self.base, "--repo", str(self.repo)])
        self.assertEqual(rc, 1)
        self.assertIn("timed out", out.getvalue())


if __name__ == "__main__":
    unittest.main()
