"""The host-side exec MCP server inherits claude's cwd, which is the card's worktree, and
`python -m` puts the cwd first on sys.path, ahead of PYTHONPATH. A worker could plant
`<worktree>/capsule/runners/exec_mcp.py` and have the host run it unsandboxed. The launch line
the runner writes into mcp.json must be immune to cwd imports (-P, Python 3.11+)."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from capsule import paths
from capsule.runners import docker

MARKER = "PLANTED-EXEC-MCP-RAN"


def _plant(root):
    """A worker-written `capsule` package in the cwd, every module of which prints the marker."""
    pkg = Path(root) / "capsule" / "runners"
    pkg.mkdir(parents=True)
    for f in (Path(root) / "capsule" / "__init__.py", pkg / "__init__.py", pkg / "exec_mcp.py"):
        f.write_text(f"print({MARKER!r})\n")


class ExecMcpSafePath(unittest.TestCase):
    def test_launch_line_ignores_a_planted_cwd_package(self):
        with tempfile.TemporaryDirectory() as spool, tempfile.TemporaryDirectory() as cwd:
            mcp_config = docker.StrongRunner().limits("x", spool)["mcp_config"]
            cfg = json.loads(Path(mcp_config).read_text())
            server = cfg["mcpServers"]["capsule"]
            args = server["args"]
            flags = args[:args.index("-m")]            # the interpreter flags the runner chose
            probe = [server["command"], *flags, "-c",
                     "import capsule.runners.exec_mcp as m; print(m.__file__)"]
            env = {**os.environ, **server["env"]}
            env.pop("PYTHONSAFEPATH", None)
            _plant(cwd)
            out = subprocess.run(probe, cwd=cwd, env=env, capture_output=True, text=True,
                                 timeout=60)
            self.assertEqual(out.returncode, 0, out.stderr)
            self.assertNotIn(MARKER, out.stdout)
            real = Path(paths.REPO) / "capsule" / "runners" / "exec_mcp.py"
            self.assertEqual(Path(out.stdout.strip().splitlines()[-1]).resolve(), real.resolve())

    def test_probe_is_vulnerable_without_the_flag(self):
        """Oracle for the test above: with no flags the planted file does win."""
        with tempfile.TemporaryDirectory() as cwd:
            _plant(cwd)
            env = {**os.environ, "PYTHONPATH": str(paths.REPO)}
            env.pop("PYTHONSAFEPATH", None)
            out = subprocess.run([sys.executable, "-c", "import capsule.runners.exec_mcp"],
                                 cwd=cwd, env=env, capture_output=True, text=True, timeout=60)
            self.assertIn(MARKER, out.stdout)


if __name__ == "__main__":
    unittest.main()
