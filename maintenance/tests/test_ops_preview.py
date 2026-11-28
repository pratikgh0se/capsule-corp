"""ops_preview's Z1 intent rows (row 643): `run.sh ops-preview` against a scratch maintenance dir
holding the real ops_preview.py next to fake branch_sweep.py and storage_gc.py, so no real branch,
container or temp dir is looked at and nothing is ever sent (the sender is a recording fake)."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
MAINT = REPO / "maintenance"
sys.path.insert(0, str(REPO))

FAKE = """import os, sys
argv = os.path.basename(sys.argv[0]) + " " + " ".join(sys.argv[1:])
open(os.environ["OP_ARGV_LOG"], "a").write(argv + "\\n")
print("{summary}")
sys.exit({exit})
"""
BRANCH = FAKE.format(summary="branch-sweep: dry run, nothing changed; 3 to remove, 5 kept",
                     exit=0)
STORAGE = FAKE.format(summary="storage-gc: volume: 2 chosen (20 bytes), 4 kept\\n"
                              "storage-gc: images: 1048576 bytes by image_gc.py (rc 0)",
                      exit='int(os.environ.get("OP_FAIL", "0"))')


class IntentRows(unittest.TestCase):
    """Z1 (row 643): contracts/intent/ops_preview.jsonl, replayed once the CoS has stamped it, the
    same pattern as test_storage_gc_tmp.IntentRows. The built-in cases run with or without it."""
    CASES = {"normal-run-previews-only": "normal", "second-run-within-47h-skipped": "skipped",
             "failed-script-no-stamp-retries": "failed"}

    def setUp(self):
        self.fresh_fixture()

    def fresh_fixture(self):
        """A new scratch dir, state and logs: each replay starts from nothing stamped."""
        self.tmp = Path(tempfile.mkdtemp(prefix="ops-preview-test."))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.maint, self.state = self.tmp / "maint", self.tmp / "state"
        self.maint.mkdir()
        for name in ("ops_preview.py", "events.py", "scrub.py", "docker_engine.py"):
            shutil.copy(MAINT / name, self.maint / name)
        (self.maint / "branch_sweep.py").write_text(BRANCH)
        (self.maint / "storage_gc.py").write_text(STORAGE)
        cap = self.tmp / "fakecap"
        cap.write_text('#!/usr/bin/env bash\nprintf "%s\\n" "$*" >>"$OP_SEND_LOG"\n')
        cap.chmod(0o755)
        for d in ("vault", "home"):
            (self.tmp / d).mkdir()
        self.argv_log, self.send_log = self.tmp / "argv.log", self.tmp / "send.log"
        self.argv_log.write_text("")
        self.send_log.write_text("")

    def run_job(self, fail=False):
        drop = ("AGENT_OPS_NO_SEND", "CAPSULE_OPS_PY")
        env = {k: v for k, v in os.environ.items() if k not in drop}
        env.update(RUN_SH_MAINT_DIR=str(self.maint), AGENT_OPS_STATE_DIR=str(self.state),
                   EVENTS_SPOOL=str(self.tmp / "events.jsonl"),
                   CAPSULE_PY=str(self.tmp / "fakecap"),
                   OP_ARGV_LOG=str(self.argv_log), OP_SEND_LOG=str(self.send_log),
                   OBSIDIAN_VAULT_PATH=str(self.tmp / "vault"), HOME=str(self.tmp / "home"),
                   OPS_ALERT_TARGET="test-chat", CAPSULE_OPS_PY=sys.executable,
                   OP_FAIL="1" if fail else "0")
        return subprocess.run(["bash", str(MAINT / "run.sh"), "ops-preview"], env=env,
                              capture_output=True).returncode

    def started(self):
        """The scripts started since the log was last cleared, as 'name args' lines."""
        lines = self.argv_log.read_text().splitlines()
        self.argv_log.write_text("")
        return lines

    def replay(self, case):
        if case == "normal":
            rc, argv = self.run_job(), self.started()
            sent = self.send_log.read_text()
            sends = [ln for ln in sent.splitlines() if ln.startswith("-m capsule outbox-send")]
            return {"exit": rc, "apply_args": sum("--apply" in ln for ln in argv),
                    "dated_files": len(list((self.state / "ops-preview").glob("*-20*.txt"))),
                    "count_lines_sent": sent.count(" preview: "),
                    "non_summary_sends": sum(not ln.startswith("-m capsule outbox-send scheduled ")
                                             for ln in sends)}
        if case == "skipped":
            self.run_job()
            self.started()
            rc = self.run_job()
            stamp = json.loads((self.state / "jobs" / "ops-preview.json").read_text())
            return {"exit": rc, "scripts_run_second": len(self.started()),
                    "skipped": int(bool(stamp["skipped"]))}
        rc = self.run_job(fail=True)
        self.started()
        failed = int("storage gc preview: FAILED" in self.send_log.read_text())
        stamped = int((self.state / "ops-preview.stamp").exists())
        self.run_job()
        return {"exit": rc, "stamp_written": stamped, "failed_line": failed,
                "next_run_scripts": len(self.started())}

    def test_the_built_in_cases_hold_without_the_file(self):
        self.assertEqual(self.replay("normal"),
                         {"exit": 0, "apply_args": 0, "dated_files": 2, "count_lines_sent": 2,
                          "non_summary_sends": 0})

    def test_a_second_run_inside_47h_starts_nothing(self):
        self.assertEqual(self.replay("skipped"),
                         {"exit": 0, "scripts_run_second": 0, "skipped": 1})

    def test_a_failed_script_leaves_no_stamp_and_retries(self):
        self.assertEqual(self.replay("failed"),
                         {"exit": 1, "stamp_written": 0, "failed_line": 1, "next_run_scripts": 2})

    def test_the_intent_rows_hold_when_the_file_exists(self):
        from capsule import intent
        rows = intent.load("ops_preview")
        self.assertEqual({r["id"] for r in rows}, set(self.CASES))
        for row in rows:
            with self.subTest(row["id"]):
                self.fresh_fixture()
                got = self.replay(self.CASES[row["id"]])
                # "detail" is the prose for people, not a measured value
                want = {k: v for k, v in row["expect"].items() if k != "detail"}
                # a row naming fewer keys than the replay measures cannot pass partially
                self.assertEqual(got.keys(), want.keys())
                self.assertEqual(got, want)


if __name__ == "__main__":
    unittest.main()
