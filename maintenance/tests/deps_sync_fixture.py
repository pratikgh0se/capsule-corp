"""Shared fixture for the deps_sync tests (test_deps_sync.py, test_deps_sync_intent.py): a temp
cache, a temp two-repo registry, a faked subprocess and a real events spool in the temp dir."""
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

MAINT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(MAINT))

import deps_sync  # noqa: E402
import events  # noqa: E402


class FakeFetch:
    """Stands in for subprocess.run: records argv and env, drops the artifact a real pip/npm
    would, fails for any spec in `fail`. `kwargs` keeps each call's other arguments (stdin...)."""

    def __init__(self, fail=()):
        self.calls, self.kwargs, self.fail = [], [], set(fail)

    def __call__(self, cmd, env=None, **kw):
        self.calls.append((list(cmd), dict(env or {})))
        self.kwargs.append(kw)
        spec = cmd[-1]
        if spec in self.fail:
            return subprocess.CompletedProcess(cmd, 1)
        if cmd[0] == "pip":
            dest = Path(cmd[cmd.index("--dest") + 1])
            dest.mkdir(parents=True, exist_ok=True)
            (dest / (spec.split("==")[0] + "-fake.whl")).touch()
        else:
            cache = Path(env["npm_config_cache"])
            cache.mkdir(parents=True, exist_ok=True)
            (cache / ".marker").touch()
        return subprocess.CompletedProcess(cmd, 0)


class Base(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        self.t = Path(self.td.name)
        self.cache = self.t / "cache"
        self.repos = []
        self.add_repo("repo-a", "# pins\npip:httpx==0.27.2\n")
        self.add_repo("repo-b", "npm:vitest@2.1.1\n")
        env = {"AGENT_OPS_REPOS_JSON": str(self.t / "repos.json"),
               "AGENT_OPS_DEPS_CACHE": str(self.cache), "EVENTS_SPOOL": str(self.t / "ev.jsonl")}
        p = unittest.mock.patch.dict(os.environ, env)
        p.start()
        self.addCleanup(p.stop)
        p = unittest.mock.patch.object(events, "SPOOL", self.t / "ev.jsonl")
        p.start()
        self.addCleanup(p.stop)

    def add_repo(self, name, allowlist):
        d = self.t / name
        d.mkdir(exist_ok=True)
        (d / "deps-allowlist.txt").write_text(allowlist)
        self.repos = [r for r in self.repos if r["name"] != name] + [{"name": name, "path": str(d)}]
        (self.t / "repos.json").write_text(json.dumps(self.repos))

    def run_main(self, *argv, fetch=None):
        fetch = fetch or FakeFetch()
        out, err = io.StringIO(), io.StringIO()
        with unittest.mock.patch.object(deps_sync.subprocess, "run", fetch), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = deps_sync.main(list(argv))
        return rc, out.getvalue(), err.getvalue(), fetch

    def manifest(self):
        return (self.cache / "synced.txt").read_text().split("\n")[:-1]

    def sync_events(self):
        p = self.t / "ev.jsonl"
        return [json.loads(l) for l in p.read_text().splitlines()] if p.exists() else []
