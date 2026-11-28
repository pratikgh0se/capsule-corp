"""M072: the vault MCP read fence (OBSIDIAN_MCP_READ_ALLOW) is moot for workers by construction.
Every worker's `claude` command line carries `--strict-mcp-config`, so the user's own MCP servers
(the vault one included) are never loaded, and the only `--mcp-config` a runner hands over names
the one `capsule` exec server. Oracles (Z2): the argv `build_argv` produces, and the mcp.json each
runner writes to disk."""
import json
import tempfile
import unittest
from pathlib import Path

from capsule.models import claude
from capsule.runners import docker, host

ROUTE = {"provider": "claude", "account": "default", "model": "claude-sonnet-5", "fallback": None,
         "turn_budget": 80}
ROLES = ("implementer", "reviewer", "validator", "guide", "korin", "security-reviewer")


def argv_for(limits):
    return claude.build_argv(ROUTE, {"system": "S", "prompt": "p"}, "/w",
                             dict(limits, schema={"type": "object"}))


class WorkerMcpFence(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.spool = Path(tmp.name)

    def all_limits(self):
        light = host.LightRunner()
        light._runs["n"] = ("/wt", "(version 1)")
        yield "strong", docker.StrongRunner().limits("n", self.spool)
        yield "light", light.limits("n", self.spool)
        for role in ROLES:
            yield f"host {role}", host.HostRunner().limits(role)
            yield f"host {role} + spool", host.HostRunner().limits(role, self.spool)

    def test_every_worker_runs_strict_with_only_the_exec_mcp(self):
        for label, limits in self.all_limits():
            argv = argv_for(limits)
            self.assertIn("--strict-mcp-config", argv, label)
            configs = [argv[i + 1] for i, a in enumerate(argv) if a == "--mcp-config"]
            self.assertLessEqual(len(configs), 1, label)
            for path in configs:
                servers = json.loads(Path(path).read_text())["mcpServers"]
                self.assertEqual(list(servers), ["capsule"], label)
                self.assertIn("capsule.runners.exec_mcp", servers["capsule"]["args"], label)
            allowed = [t for t in (limits.get("allowed") or "").split(",") if t.startswith("mcp__")]
            self.assertLessEqual(set(allowed), {"mcp__capsule__exec"}, label)

    def test_a_worker_without_an_mcp_config_gets_no_mcp_at_all(self):
        # strict + no --mcp-config = zero servers; the read-only roles rely on exactly this
        argv = argv_for(host.HostRunner().limits("reviewer", self.spool))
        self.assertIn("--strict-mcp-config", argv)
        self.assertNotIn("--mcp-config", argv)


if __name__ == "__main__":
    unittest.main()
