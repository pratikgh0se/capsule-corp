"""Shared fakes for test_disk_relief.py and test_disk_relief_worktrees.py: a fake HOME, fake
docker/npm/uv/brew/go/pip on PATH (shell scripts that record their argv), a fake storage_gc.py, temp
scratch and temp git repos for the worktrees. Never the real home, Docker, caches or repos. Free
space is a patched function so a test says exactly how low the disk is."""
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from capsule.tests import tmpclean  # noqa: E402

tmpclean.install()
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import disk_relief  # noqa: E402

GB = 10**9
FAKE_TOOL = '#!/bin/sh\necho "$(basename "$0") $*" >> "$FAKE_LOG"\n'
FAKE_SLOW = '#!/bin/sh\necho "$(basename "$0") $*" >> "$FAKE_LOG"\nexec sleep 30\n'
FUTURE = time.time() + 10 * 86400        # the patched clock: every untouched file is 10 days old
GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t", "GIT_CONFIG_GLOBAL": os.devnull,
           "GIT_CONFIG_SYSTEM": os.devnull}


def age(path, days):
    """A change time cannot be backdated, so Base moves the clock 10 days ahead instead: whatever
    is left alone is old. This stays as the readable marker for "old"."""


def fresh(path):
    """Touch a file or a whole tree at the patched clock's now (the modification time counts)."""
    for here, dirs, files in os.walk(path):
        for n in dirs + files:
            os.utime(os.path.join(here, n), (FUTURE, FUTURE), follow_symlinks=False)
    os.utime(path, (FUTURE, FUTURE), follow_symlinks=False)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="disk-relief-test-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home, self.bin = self.tmp / "home", self.tmp / "bin"
        self.scratch, self.log = self.tmp / "claude-501", self.tmp / "fake.log"
        for d in (self.home, self.bin, self.scratch):
            d.mkdir()
        self.log.write_text("")
        for name in ("docker", "npm", "uv", "brew", "go", "pip"):
            self.fake(name, FAKE_TOOL)
        self.gc = self.fake("storage_gc.py", '#!/bin/sh\necho "storage_gc $*" >> "$FAKE_LOG"\n')
        env = {"HOME": str(self.home), "AGENT_OPS_STATE_DIR": str(self.tmp / "state"),
               "PATH": f"{self.bin}:/usr/bin:/bin", "AGENT_OPS_DOCKER_BIN": str(self.bin),
               "FAKE_LOG": str(self.log), "OPS_DISK_RELIEF_FREE_GB": "60",
               "OPS_DISK_RELIEF_FLOOR_GB": "25", **GIT_ENV}
        self.enterContext(mock.patch.dict(os.environ, env))
        self.enterContext(mock.patch.object(disk_relief, "STORAGE_GC", self.bin / "storage_gc.py"))
        self.enterContext(mock.patch.object(disk_relief, "SCRATCH_ROOT", str(self.scratch)))
        self.enterContext(mock.patch.object(disk_relief.sys, "executable", "/bin/sh"))
        self.enterContext(mock.patch.object(disk_relief, "_now", lambda: FUTURE))
        self.free(40)
        self.repo = self.tmp / "capsule-corp"          # absent until a test builds git repos
        os.environ["DISK_RELIEF_REPO"] = str(self.repo)

    def fake(self, name, body):
        p = self.bin / name
        p.write_text(body)
        p.chmod(0o755)
        return p

    def free(self, gigabytes, after_marker=None, then=None):
        """Free space in GB; with after_marker, `then` GB once the fake log contains it."""
        def free_bytes():
            hit = after_marker and after_marker in self.log.read_text()
            return int((then if hit else gigabytes) * GB)
        self.enterContext(mock.patch.object(disk_relief, "free_bytes", free_bytes))

    def calls(self):
        return self.log.read_text().splitlines()

    def jsonl(self):
        p = self.tmp / "state" / "disk_relief.jsonl"
        return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []

    def go(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = disk_relief.main(list(args))
        return rc, out.getvalue()

    # -- git: an origin, the main checkout and sibling worktrees -------------------------------
    def git(self, cwd, *a):
        r = subprocess.run(["git", "-C", str(cwd), *a], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.strip()

    def make_repo(self):
        origin = self.tmp / "origin.git"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main", str(self.repo)], check=True)
        (self.repo / "f").write_text("1")
        self.git(self.repo, "add", "f")
        self.git(self.repo, "commit", "-q", "-m", "one")
        self.git(self.repo, "remote", "add", "origin", str(origin))
        self.git(self.repo, "push", "-q", "origin", "main")

    def add_wt(self, name, old=True):
        path = self.tmp / name
        self.git(self.repo, "worktree", "add", "-q", "--detach", str(path), "main")
        if not old:
            fresh(path)
        return path
