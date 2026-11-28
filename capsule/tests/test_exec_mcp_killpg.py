"""The light tier's end-of-run group kill is cleanup. On macOS a process group whose remaining
members are all zombies (exited, not yet reaped) answers EPERM instead of ESRCH, so a loaded
machine made `_run_sandboxed` raise PermissionError out of its finally block. Either answer means
nothing is left to kill, and the command's result must still come back."""
import subprocess
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from capsule.runners import exec_mcp

_REAL_POPEN = subprocess.Popen


def _echo_popen(argv, **kw):
    """The sandbox-exec argv swapped for a plain shell: this tests the kill, not the profile."""
    return _REAL_POPEN(["/bin/sh", "-c", "echo hi"], **kw)


class GroupKillAnswers(unittest.TestCase):
    def run_with_killpg_raising(self, exc):
        with tempfile.TemporaryDirectory() as wt:
            cfg = SimpleNamespace(dry_run=False, sandbox_profile="unused", workspace=wt)
            with mock.patch.object(exec_mcp.subprocess, "Popen", _echo_popen), \
                    mock.patch.object(exec_mcp.os, "killpg", side_effect=exc) as killpg:
                result = exec_mcp._run_exec(cfg, "echo hi", 30)
            killpg.assert_called_once()
            return result

    def test_eperm_from_a_zombie_only_group_still_returns_the_result(self):
        result = self.run_with_killpg_raising(PermissionError(1, "Operation not permitted"))
        self.assertEqual(result, {"exit_code": 0, "text": "hi\n"})

    def test_esrch_from_an_empty_group_still_returns_the_result(self):
        result = self.run_with_killpg_raising(ProcessLookupError(3, "No such process"))
        self.assertEqual(result, {"exit_code": 0, "text": "hi\n"})


if __name__ == "__main__":
    unittest.main()
