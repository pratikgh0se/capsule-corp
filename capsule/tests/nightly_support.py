"""Shared fixtures for the quality_nightly test modules: the isolated env and the Tmp base."""
import contextlib
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "maintenance"))
import quality_nightly as qn  # noqa: E402

_BOOT_STATE = ""

_SUITE = "python3 -m unittest discover tests"


def setUpModule():
    """Isolate the suite's env here, not at import; module cleanups put back the earlier values
    (or unset them) so no other module under `unittest discover` sees ours."""
    global _BOOT_STATE
    _BOOT_STATE = tempfile.mkdtemp(prefix="nightly-state-")
    unittest.addModuleCleanup(shutil.rmtree, _BOOT_STATE, ignore_errors=True)
    env = mock.patch.dict(os.environ, CAPSULE_HOME=_BOOT_STATE, MERGE_GATE_STATE=_BOOT_STATE)
    env.start()
    unittest.addModuleCleanup(env.stop)


class Tmp(unittest.TestCase):
    """Every fixture directory is registered for cleanup, and any test that
    writes state points MERGE_GATE_STATE at its own throwaway dir."""

    def mkdtemp(self, prefix="nightly-"):
        d = tempfile.mkdtemp(prefix=prefix)
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        return d

    def own_state_dir(self):
        d = self.mkdtemp("nightly-state-")
        before = os.environ.get("MERGE_GATE_STATE")
        os.environ["MERGE_GATE_STATE"] = d
        self.addCleanup(os.environ.__setitem__, "MERGE_GATE_STATE", before or _BOOT_STATE)
        return d


def _fake_repo(case):
    """A throwaway directory standing in for a checkout: run_repo only asks
    whether the path exists - every command goes to the fake runner. Registered
    for cleanup on the calling test."""
    return case.mkdtemp("nightly-repo-")


@contextlib.contextmanager
def _repo_path(repo, root):
    """Point REPO_PATHS at a fixture so nothing in the suite can reach a real
    checkout, docker or the network."""
    paths = qn.mg._bridge().REPO_PATHS
    before = paths.get(repo)
    paths[repo] = str(root)
    try:
        yield
    finally:
        if before is None:
            paths.pop(repo, None)
        else:
            paths[repo] = before
