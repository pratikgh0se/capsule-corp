"""One guard for tests that read files the public snapshot does not ship. The denylist exists only
in the private repo, so a missing file there still fails; only the snapshot skips."""
import importlib.util
import subprocess
import sys
import unittest

from capsule import paths

PRIVATE_TREE = (paths.REPO / "release" / "private-denylist.txt").exists()
private_only = unittest.skipUnless(PRIVATE_TREE, "private file not in public release")


def _ruff_runs():
    name = "size_check"
    spec = importlib.util.spec_from_file_location(name, paths.REPO / "tests" / f"{name}.py")
    size_check = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(paths.REPO / "tests"))  # size_check imports its sibling bare
    try:
        spec.loader.exec_module(size_check)
        cmd = size_check.ruff_cmd()
        return bool(cmd) and subprocess.run([*cmd, "--version"], capture_output=True,
                                            timeout=60).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


# Real-ruff tests: a missing ruff stays red in the private tree (Z4); only the snapshot skips.
needs_ruff = unittest.skipUnless(PRIVATE_TREE or _ruff_runs(),
                                 "ruff not available (install ruff, or run uvx once online)")
