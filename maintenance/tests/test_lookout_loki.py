# mutation-check: targets-predate-range b957739c9 -- rename only, no behaviour pinned ::
#   Config.test_compose_mounts_are_narrow_read_only_and_guarded,
#   Labels.test_capsule_paths_get_their_jobs
"""Task 5 (The Lookout): Loki tails the Capsule logs (spec 5.13). Oracle 1: labels.lua itself,
executed by the host `lua` interpreter (the same string.match semantics fluent-bit's LuaJIT runs).
Oracle 2: the deploy-time smoke (Task 7) counts live `{job="capsule-worker"}` series (A2).

Fix round 1 (task-5-review.md): M-1 also guards DB-path uniqueness and the unchanged
legacy/vault lanes; M-2 uses a real 32-hex run id (live shape: boards/demo/spool/
<32-hex>/worker.log); M-3 renames the rotated-tick-log test; M-4 cleans up its temp file."""
import os
import re
import shutil
import subprocess
import tempfile
import unittest
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
FB = REPO / "observability" / "fluent-bit"
RUN_ID = "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6"  # live shape: 32 hex chars, no dashes
HARNESS = r'''
dofile(arg[1])
for i = 2, #arg do
  local _, _, r = add_labels("t", 0, {path = arg[i]})
  print(table.concat({arg[i], r.job, r.board, r.run_id, r.task_id, r.vault_job}, "\t"))
end
'''
_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))
from capsule.tests.public_tree import private_only  # noqa: E402
from capsule import legacy_names as _LN  # noqa: E402


def labels(*paths):
    lua = shutil.which("lua") or shutil.which("luajit")
    if lua is None:
        raise AssertionError("no lua interpreter on PATH: brew install lua (this test executes labels.lua)")
    fd, path = tempfile.mkstemp(suffix=".lua")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(HARNESS)
        out = subprocess.run([lua, path, str(FB / "labels.lua"), *paths], capture_output=True, text=True, check=True)
    finally:
        os.unlink(path)
    return {line.split("\t")[0]: tuple(line.split("\t")[1:]) for line in out.stdout.splitlines()}


def fb_inputs():
    blocks = re.split(r"(?m)^\[", (FB / "fluent-bit.conf").read_text())
    return [dict(re.findall(r"(?m)^\s+(\w+)\s+(.+?)\s*$", b)) for b in blocks if b.startswith("INPUT]")]


HAS_LUA = bool(shutil.which("lua") or shutil.which("luajit"))


class Labels(unittest.TestCase):
    def test_outbox_label_rule_reads_the_board_from_the_path(self):
        # Runs without lua: the board outbox rule must capture the board and drop the root file.
        lua = (REPO / "observability" / "fluent-bit" / "labels.lua").read_text()
        self.assertEqual(re.findall(r'"\^/capsule/[^"]*outbox[^"]*"', lua),
                         ['"^/capsule/boards/([^/]+)/outbox%.log$"'])
        self.assertIn('job, board = "capsule-outbox", ob', lua)

    @unittest.skipIf(not HAS_LUA, "no lua interpreter on PATH")
    def test_capsule_paths_get_their_jobs(self):
        got = labels(f"/capsule/boards/demo/spool/{RUN_ID}/worker.log", "/capsule/logs/tick.log",
                     "/capsule/boards/demo/outbox.log", f"/{_LN.NAME}/kanban/boards/b/logs/t_1.log",
                     "/vault-logs/nightly-2026-09-27.log")
        self.assertEqual(got[f"/capsule/boards/demo/spool/{RUN_ID}/worker.log"][:3],
                         ("capsule-worker", "demo", RUN_ID))
        self.assertEqual(got["/capsule/logs/tick.log"][0], "capsule-tick")
        outbox = got["/capsule/boards/demo/outbox.log"]
        self.assertEqual(outbox[0], "capsule-outbox")
        self.assertEqual(outbox[1], "demo")
        # Legacy lane unchanged: job and task_id both still resolve.
        legacy = got[f"/{_LN.NAME}/kanban/boards/b/logs/t_1.log"]
        self.assertEqual(legacy[0], "worker")
        self.assertEqual(legacy[3], "t_1")
        # Vault lane unchanged: job and vault_job both still resolve.
        vault = got["/vault-logs/nightly-2026-09-27.log"]
        self.assertEqual(vault[0], "vault")
        self.assertEqual(vault[4], "nightly")

    @unittest.skipIf(not HAS_LUA, "no lua interpreter on PATH")
    def test_rotated_tick_logs_are_not_tick(self):
        self.assertEqual(labels("/capsule/logs/tick-2026.log")["/capsule/logs/tick-2026.log"][0], "other")


class Config(unittest.TestCase):
    def test_three_capsule_inputs(self):
        by_tag = {i.get("Tag"): i for i in fb_inputs()}
        self.assertEqual(by_tag["capsule.worker.*"]["Path"], "/capsule/boards/*/spool/*/worker.log")
        self.assertEqual(by_tag["capsule.worker.*"]["DB"], "/fluent-bit/state/capsule-worker.db")
        # factory-autonomy Task 11 (round 2 I16): a launchd job per board writes its own log
        # (tick.log for demo, tick-capsule-corp.log for the second), and a tick can now file
        # cards on another board through boards/<board>/outbox.log (capsule/intake.py).
        self.assertEqual(by_tag["capsule.tick"]["Path"], "/capsule/logs/tick*.log")
        self.assertEqual(by_tag["capsule.outbox"]["Path"], "/capsule/boards/*/outbox.log")
        for tag in ("capsule.worker.*", "capsule.tick", "capsule.outbox"):
            self.assertEqual(by_tag[tag]["Path_Key"], "path")

    def test_all_input_db_paths_are_distinct(self):
        dbs = [i["DB"] for i in fb_inputs() if "DB" in i]
        self.assertEqual(len(dbs), len(set(dbs)), f"two tail inputs share an offsets DB: {dbs}")

    def test_run_id_is_a_loki_label(self):
        out = (FB / "fluent-bit.conf").read_text().split("[OUTPUT]")[1]
        self.assertIn("run_id=$run_id", out)

    def test_compose_mounts_are_narrow_read_only_and_guarded(self):
        text = (REPO / "observability" / "docker-compose.yml").read_text()
        fb = text.split("  fluent-bit:")[1].split("\n  grafana:")[0]
        guard = "${CAPSULE_HOME:?CAPSULE_HOME missing in observability.env}"
        for m in (f"{guard}/logs:/capsule/logs:ro", f"{guard}/boards:/capsule/boards:ro"):
            self.assertIn(m, fb)
        self.assertNotIn(f"/{_LN.NAME}", fb)                          # row 478 D8: no legacy mount
        self.assertNotRegex(fb, r"\$\{CAPSULE_HOME\}:")          # never an unguarded whole $CAPSULE_HOME
        self.assertNotRegex(fb, r"\$\{CAPSULE_HOME[^:]")         # every use of CAPSULE_HOME carries the :? guard

    def test_env_template_names_capsule_home_as_a_path(self):
        self.assertIn("CAPSULE_HOME=__HOME__/.local/share/capsule", (REPO / "observability" / "env.template").read_text())


class DeployBackfill(unittest.TestCase):
    """C-1: an observability.env that predates this change has no CAPSULE_HOME line,
    so deploy.sh must backfill it the same way it backfills INFISICAL_ENV."""

    @private_only
    def test_deploy_backfills_capsule_home_alongside_infisical_env(self):
        text = (REPO / "deploy.sh").read_text()
        self.assertIn("grep -q '^CAPSULE_HOME=' \"$OBS_ENV\"", text)
        self.assertIn("CAPSULE_HOME=%s", text)


if __name__ == "__main__":
    unittest.main()
