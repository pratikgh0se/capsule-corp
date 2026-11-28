"""inc_35435ea0 (t_dd76ebdd): a host Edit replaces a file with a new inode and Docker Desktop's
file share keeps a stale entry in the container until its directory is listed. The start probe
heals only at container start; every strong-tier exec (the worker's exec MCP tool and
`StrongRunner.exec`) now lists first, in the same `docker exec`. Fake docker or `--dry-run`
argv only, except RealDocker (RUN_DOCKER_TESTS=1, in a child process: the unittest docker door
refuses a real docker)."""
import argparse
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from capsule import bundles, paths
from capsule.runners import docker, exec_mcp


def _cfg():
    return argparse.Namespace(container="capsule-x", sandbox_profile=None, spool="/tmp/spool",
                              docker="docker", dry_run=True, workspace=None)


def _script(cmd):
    """The command script (the wrapper's `$1`) of the dry-run `docker exec` argv."""
    argv = json.loads(exec_mcp._run_exec(_cfg(), cmd, 5)["text"])
    assert argv[-4:-2] == ["-c", exec_mcp._WRAP], argv
    return argv[-1]


class ExecHeal(unittest.TestCase):
    def test_exec_lists_changed_dirs_before_the_command(self):
        # Every directory is listed, so the one holding a host-replaced file is too.
        script = _script("echo hi")
        self.assertEqual(script, docker.TREE_LISTING + "echo hi")    # the heal, then the command
        self.assertTrue(script.startswith("timeout 10 find /workspace"), script)
        self.assertIn("ls -la", script)
        self.assertLess(script.index("ls -la"), script.index("echo hi"))
        # The heal's own failure (no /workspace here) stops nothing, and the exit code and output
        # stay the user's: real sh, and the heal's `;` separator.
        heal = script.removesuffix("echo hi").removeprefix("timeout 10 ")
        done = subprocess.run(["sh", "-c", heal + "echo hi; exit 3"], capture_output=True,
                              text=True)
        self.assertEqual((done.returncode, done.stdout, done.stderr), (3, "hi\n", ""))

    def test_the_heal_lists_a_nested_dir_a_changed_file_sits_in(self):
        with tempfile.TemporaryDirectory() as tmp:
            nested = Path(tmp) / "a" / "b"
            nested.mkdir(parents=True)
            (nested / "f.txt").write_text("new\n")
            heal = docker.TREE_LISTING.removeprefix("timeout 10 ").replace("/workspace", tmp)
            seen = subprocess.run(["sh", "-c", heal.replace("ls -la", "echo").replace(
                ">/dev/null 2>&1", "")], capture_output=True, text=True).stdout.split()
            self.assertIn(str(nested), seen)

    def test_runner_exec_runs_the_tree_listing_before_the_command_in_one_exec(self):
        seen = []

        def fake(argv, **kw):
            seen.append(argv)
            return subprocess.CompletedProcess(argv, 7, "out", "err")
        runner = docker.StrongRunner()
        runner._run = fake
        self.assertEqual(runner.exec("capsule-x", "true", 5), (7, "outerr"))
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0][-1], docker.TREE_LISTING + "true")

    def test_the_implementer_brief_says_gh_and_ci_log_steps_are_host_only(self):
        self.assertIn("`gh` and CI-log steps", bundles.HARNESS_NOTE)
        self.assertLess(bundles.HARNESS_NOTE.index("`gh` and CI-log steps"),
                        bundles.HARNESS_NOTE.index("go in `host_only`"))


class RealDocker(unittest.TestCase):
    def test_a_host_replace_is_read_back_by_the_next_exec(self):
        if os.environ.get("RUN_DOCKER_TESTS") != "1":    # in the body: the gate flags a decorator
            self.skipTest("real docker: RUN_DOCKER_TESTS=1")
        script = (
            "import argparse, os, subprocess, sys, tempfile\n"
            f"sys.path.insert(0, {str(paths.REPO)!r})\n"
            "from pathlib import Path\n"
            "from capsule.runners import docker, exec_mcp\n"
            "with tempfile.TemporaryDirectory() as root:\n"
            "    ws = Path(root)\n"
            "    g = ['git', '-c', 'user.name=capsule-test', '-c', 'user.email=t@x.invalid']\n"
            "    subprocess.run(['git', 'init', '-q', str(ws)], check=True)\n"
            "    (ws / 'a' / 'b').mkdir(parents=True)\n"
            "    (ws / 'a' / 'b' / 'tracked.txt').write_text('one\\n')\n"
            "    subprocess.run([*g, '-C', str(ws), 'add', '-A'], check=True)\n"
            "    subprocess.run([*g, '-C', str(ws), 'commit', '-q', '-m', 'i'], check=True)\n"
            "    r = docker.StrongRunner()\n"
            "    name = r.start('h' * 32, ws)\n"
            "    try:\n"
            "        cfg = argparse.Namespace(container=name, sandbox_profile=None, spool=root,\n"
            "                                 docker='docker', dry_run=False, workspace=None)\n"
            "        print(exec_mcp._run_exec(cfg, 'cat a/b/tracked.txt', 30)['text'].strip())\n"
            "        tmp = ws / 'a' / 'b' / '.t.tmp'\n"
            "        tmp.write_text('replaced\\n')\n"
            "        os.replace(tmp, ws / 'a' / 'b' / 'tracked.txt')     # a new inode\n"
            "        print(exec_mcp._run_exec(cfg, 'cat a/b/tracked.txt', 30)['text'].strip())\n"
            "    finally:\n"
            "        r.stop(name)\n")
        env = {**os.environ, "OPS_WORKER_IMAGE": docker.lock_image()}
        done = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                              env=env, timeout=300)
        self.assertEqual(done.stdout.split(), ["one", "replaced"], done.stderr)


if __name__ == "__main__":
    unittest.main()
