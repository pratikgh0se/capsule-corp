"""Shared fixture for the public_export tests: a private fixture repo with an allowlist, a
DEST path, a fixture denylist and a state dir for the mapping log (never the real one)."""
import contextlib
import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import public_export  # noqa: E402

PERSONAL = "/Use" + "rs/someone/code"  # built in two parts: this file is itself in the export


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, check=True,
                          text=True).stdout.strip()


class Fixture(unittest.TestCase):
    def setUp(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        self.root = Path(td.name)
        self.repo = self.root / "private"
        self.dest = self.root / "public"
        self.deny = self.root / "denylist.txt"
        self.deny.write_text("# fixture denylist\nterm\tacme-secret\n")
        self.state = self.root / "state"  # the mapping log never lands in the real state dir
        env = mock.patch.dict(os.environ, {"AGENT_OPS_STATE_DIR": str(self.state)})
        env.start()
        self.addCleanup(env.stop)
        (self.repo / "release").mkdir(parents=True)
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "config", "user.email", "t@example.invalid")
        git(self.repo, "config", "user.name", "t")
        self.put("release/public-paths.txt", "capsule/\nREADME.md\n!capsule/private.py\n")
        self.put("capsule/a.py", "x = 1")
        self.put("capsule/private.py", "secret_stuff = 1")
        self.put("README.md", "demo")
        self.put("docs/RUNBOOK.md", "not public " + PERSONAL)
        self.sha1 = self.commit("one")

    def put(self, name, text, mode=None):
        p = self.repo / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text + "\n")
        if mode:
            p.chmod(mode)

    def commit(self, msg):
        git(self.repo, "add", "-A", "-f")
        git(self.repo, "commit", "-q", "-m", msg)
        return git(self.repo, "rev-parse", "HEAD")

    def export(self, *args, dest=None, deny=True):
        out, err = io.StringIO(), io.StringIO()
        denylist = ["--denylist", str(self.deny)] if deny else []
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = public_export.main([*args[:1], str(dest or self.dest), "--repo", str(self.repo),
                                     *denylist, *args[1:]])
        return rc, out.getvalue(), err.getvalue()

    def names(self):
        return sorted(str(p.relative_to(self.dest)) for p in self.dest.rglob("*")
                      if (p.is_file() or p.is_symlink()) and ".git" not in p.parts)
