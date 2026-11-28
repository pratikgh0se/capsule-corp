"""Shared helpers for the Docker engine switch tests (docs/contracts/docker-engine-switch.md):
engine-row accessor, temp sandbox and the shell-helper runner. Not a test module."""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "maintenance"))

DESKTOP_BIN = "/Applications/Docker.app/Contents/Resources/bin"
LIB = ROOT / "maintenance" / "lib_docker_engine.sh"
LIB_STATE = ROOT / "maintenance" / "lib_state.sh"
TABLE = {  # the contract's engine table
    "desktop": dict(context="desktop-linux", title="Docker Desktop", app="Docker",
                    app_path="/Applications/Docker.app", open_hint="open -a Docker",
                    cask="docker-desktop"),
    "orbstack": dict(context="orbstack", title="OrbStack", app="OrbStack",
                     app_path="/Applications/OrbStack.app", open_hint="open -a OrbStack",
                     cask="orbstack"),
}


class Sandbox(unittest.TestCase):
    """Temp HOME, state dir and fake docker dir; helpers to run with a controlled environment."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="docker-engine-test-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home, self.state, self.fake = self.tmp / "home", self.tmp / "state", self.tmp / "fake"
        for d in (self.home, self.state, self.fake):
            d.mkdir()
        (self.fake / "docker").write_text("#!/bin/sh\nexit 0\n")
        (self.fake / "docker").chmod(0o755)

    def engine_file(self, text):
        (self.state / "docker-engine").write_text(text)

    def env(self, engine=None, override=True, **extra):
        env = {"PATH": "/usr/bin:/bin", "HOME": str(self.home),
               "AGENT_OPS_STATE_DIR": str(self.state), "AGENT_OPS_NO_SEND": "1"}
        if override:
            env["AGENT_OPS_DOCKER_BIN"] = str(self.fake)
        if engine is not None:
            env["CAPSULE_DOCKER_ENGINE"] = engine
        env.update(extra)
        return env

    def py(self, env, fn):
        with mock.patch.dict(os.environ, env, clear=True):
            return fn()


def run_lib(case, engine=None, file_text=None, show=False, override=None, extra_path=None,
            call_export=True, expect_rc=0, state=True):
    """Source lib_state.sh then lib_docker_engine.sh in bash with a controlled env; returns the
    exported/printed variables as a dict (and the subprocess under key '_proc')."""
    env = {"PATH": "/usr/bin:/bin", "HOME": str(case.home), "AGENT_OPS_STATE_DIR": str(case.state),
           "AGENT_OPS_NO_SEND": "1"}
    if file_text is not None:
        (case.state / "docker-engine").write_text(file_text)
    if engine is not None:
        env["CAPSULE_DOCKER_ENGINE"] = engine
    if override:
        env["AGENT_OPS_DOCKER_BIN"] = override
    if extra_path:
        env["DOCKER_EXTRA_PATH"] = extra_path
    script = (f". '{LIB_STATE}'; . '{LIB}'; "
              + ("docker_path_export; " if call_export else "")
              + 'echo "::SENTINEL"; env; echo "::VARS"; '
              + "for v in DOCKER_ENGINE DOCKER_ENGINE_BIN DOCKER_ENGINE_CONTEXT "
                "DOCKER_ENGINE_TITLE DOCKER_ENGINE_APP DOCKER_ENGINE_APP_PATH "
                "DOCKER_ENGINE_OPEN_HINT DOCKER_ENGINE_CASK; "
                'do printf "%s=%s\\n" "$v" "${!v}"; done')
    proc = subprocess.run(["bash", "-c", script], env=env, capture_output=True,
                          text=True, timeout=60)
    case.assertEqual(proc.returncode, expect_rc, proc.stderr)
    out = {"_proc": proc}
    if expect_rc == 0:
        body = proc.stdout.split("::SENTINEL\n", 1)[1]
        envpart, varpart = body.split("::VARS\n", 1)
        for line in envpart.splitlines() + varpart.splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                out.setdefault(k, v)
        out["_exported"] = {ln.split("=", 1)[0] for ln in envpart.splitlines() if "=" in ln}
    return out
