"""Shared fake world for the after_landing tests (test_after_landing, test_after_landing_intent).

Oracle 1: the recorded calls of the fake deploy / tick (restart moves the release clone, as the
real script does). Oracle 2: real git in temp repos and the JSON log. Never live anything.
"""
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import after_landing as al  # noqa: E402

IDENT = ("-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false")


def git(repo, *args):
    r = subprocess.run(["git", "-C", str(repo), *IDENT, *args], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


def done(rc=0, out="", err=""):
    return subprocess.CompletedProcess([], rc, out, err)


class Chain(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.origin = self.tmp / "origin"
        self.checkout = self.tmp / "checkout"
        self.home = self.tmp / "home"
        self.harness = self.home / "harness" / "main"
        self.la = self.tmp / "LaunchAgents"
        self.la.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main", str(self.origin)], check=True)
        self.commit(self.origin, "README", "one")
        self.commit(self.origin, "capsule/boundary.py",
                    'MAINT = ("maintenance/events.py", "maintenance/vault_git.py")\n')
        for dest in (self.checkout, self.harness):
            subprocess.run(["git", "clone", "-q", str(self.origin), str(dest)], check=True)
        # alpha and pomo (a tick without --board) run; idle has a plist but is stopped; nolist
        # looks alive but has no plist (like an MCP-server job) and must be skipped.
        head = git(self.harness, "rev-parse", "HEAD")    # the code each running tick loaded
        self.loaded = {b: head for b in ("alpha", "pomo", "idle", "nolist")}
        self.ticks = {"alpha": 101, "pomo": 303, "idle": None, "nolist": 505}
        for b in self.ticks:
            (self.home / "boards" / b).mkdir(parents=True)
        for b in ("alpha", "pomo", "idle"):
            (self.la / f"com.t.capsule-tick-{b}.plist").write_text("x")
        env = mock.patch.dict(os.environ, {"AGENT_OPS_STATE_DIR": str(self.tmp / "state")})
        env.start()
        self.addCleanup(env.stop)
        self.calls, self.argvs = [], []
        self.deploy_err = ""
        self.deploy_rc, self.check_out, self.restart_rc, self.same_pid = 0, al.CLEAN, 0, 0
        self.check_rc, self.dry_rc, self.fail_on = 0, 0, None   # fail_on: board whose restart fails
        real = al._run
        patcher = mock.patch.object(al, "_run", lambda argv, cwd, timeout=300: self.fake(
            real, argv, cwd, timeout))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.sweeps = []       # the image sweep never reaches real docker here
        sw = mock.patch("image_sweep.sweep",
                       lambda apply, **kw: self.sweeps.append(apply) or 0)
        sw.start()
        self.addCleanup(sw.stop)

    def commit(self, repo, name, text):
        path = Path(repo) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        git(repo, "add", name)
        git(repo, "commit", "-q", "-m", f"add {name}")

    def land(self, name="capsule/x.py"):
        """A new commit on origin (the landing)."""
        self.commit(self.origin, name, "new")

    def fake(self, real, argv, cwd, timeout):
        self.argvs.append(argv)
        if argv[0] == "./deploy.sh":
            self.calls.append(("deploy", argv[1:], Path(cwd)))
            if argv[1:] == ["--check"]:
                return done(self.check_rc, self.check_out)
            if argv[1:] == ["--dry-run"]:
                return done(self.dry_rc, "WOULD  x")
            return done(self.deploy_rc, "deployed", self.deploy_err)
        if argv[0] == "bin/capsule-tick.sh" and argv[1] == "status":
            pid = self.ticks[argv[2]]
            return done(0, json.dumps({"board": argv[2], "label": f"com.t.capsule-tick-{argv[2]}",
                                       "pid": pid, "alive": pid is not None,
                                       "lock": pid is not None,
                                       "loaded_sha": self.loaded[argv[2]]}))
        if argv[0] == "bin/capsule-tick.sh":
            self.calls.append(("restart", argv[2], Path(cwd)))
            git(self.harness, "fetch", "-q", "origin")          # what update_release does
            git(self.harness, "merge", "-q", "--ff-only", "origin/main")
            self.loaded[argv[2]] = git(self.harness, "rev-parse", "HEAD")
            if not self.same_pid:
                self.ticks[argv[2]] += 1000
            return done(self.restart_rc if self.fail_on in (None, argv[2]) else 0)
        return real(argv, cwd, timeout)

    def run_main(self, *extra):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = al.main(["--checkout", str(self.checkout), "--home", str(self.home),
                          "--la-dir", str(self.la), *extra])
        return rc, out.getvalue(), err.getvalue()

    def heads(self):
        return git(self.checkout, "rev-parse", "HEAD"), git(self.harness, "rev-parse", "HEAD")

    def log_rows(self):
        path = self.tmp / "state" / "after_landing.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def restarts(self):
        return [c[1] for c in self.calls if c[0] == "restart"]
