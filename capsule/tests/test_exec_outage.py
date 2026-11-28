"""inc_0221ae04 (t_4b0fb2d5): a docker daemon lost while a strong-tier worker runs. The exec tool
tells an outage from a red command (the rule of runners/sandbox.py: container gone and daemon
down), writes `<spool>/exec_outage.json`, and a held report on that run blocks `transient` with
cause `exec_outage`; the first pass with docker back releases it. A fake docker binary only."""
import argparse
import json
import stat
import tempfile
import time
import unittest
from pathlib import Path

from capsule import blocks
from capsule.runners import exec_mcp
from capsule.tests.test_tick import IMPL_DONE, TickCase

HELD = dict(IMPL_DONE, issues=["the checks could not run"])
TEXT = "Cannot connect to the Docker daemon at unix:///var/run/docker.sock"


def fake_docker(directory, running, daemon, exec_rc=1):
    """A docker binary: `exec` fails; `inspect` says whether the container runs; `version`
    answers when the daemon does."""
    path = Path(directory) / "fake-docker"
    path.write_text(
        "#!/bin/sh\n"
        'case "$1" in\n'
        f'  exec) echo "{TEXT}" >&2; exit {exec_rc};;\n'
        f'  inspect) {"echo true; exit 0" if running else "exit 1"};;\n'
        f'  version) exit {0 if daemon else 1};;\n'
        "esac\nexit 0\n")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path)


def run_exec(spool, docker, cmd="make test"):
    cfg = argparse.Namespace(container="capsule-x", sandbox_profile=None, spool=str(spool),
                             docker=docker, dry_run=False, workspace=None)
    return exec_mcp._run_exec(cfg, cmd, 5)


class Marker(unittest.TestCase):
    def test_an_outage_writes_the_marker_at_the_spool_root_and_says_the_command_did_not_run(self):
        spool = Path(tempfile.mkdtemp())
        out = run_exec(spool, fake_docker(spool, running=False, daemon=False))
        self.assertTrue((spool / "exec_outage.json").is_file())
        self.assertFalse((spool / "requests" / "exec_outage.json").exists())
        first = out["text"].splitlines()[0]
        self.assertIn("did not run", first)
        self.assertIn("docker daemon is down", first)
        self.assertEqual(out["exit_code"], 1)

    def test_a_failed_exec_with_a_live_daemon_writes_no_marker_and_keeps_its_text(self):
        for running, daemon in ((True, True), (False, True)):
            with self.subTest(running=running):
                spool = Path(tempfile.mkdtemp())
                out = run_exec(spool, fake_docker(spool, running, daemon, exec_rc=3))
                self.assertFalse((spool / "exec_outage.json").exists())
                self.assertEqual((out["exit_code"], out["text"].strip()), (3, TEXT))

    def test_a_client_side_timeout_gets_the_same_check(self):
        spool = Path(tempfile.mkdtemp())
        docker = fake_docker(spool, running=False, daemon=False)
        cfg = argparse.Namespace(container="capsule-x", sandbox_profile=None, spool=str(spool),
                                 docker=docker, dry_run=False, workspace=None)
        self.assertTrue(exec_mcp._daemon_lost(cfg))
        self.assertFalse(exec_mcp._daemon_lost(argparse.Namespace(
            **dict(vars(cfg), docker=fake_docker(spool, True, True)))))


class ExecOutage(TickCase):
    def lose_daemon(self, report=HELD):
        """The implementer's exec fails on a dead daemon, then it reports `report`."""
        self.script["implementer"] = [[report]]

        def lost(job, sp, run):
            run_exec(sp, fake_docker(sp, running=False, daemon=False))
            self.runner.up = False                   # docker stays down until the test says so
        self.hooks["implementer"] = lost

    def pass_until(self, status, now, limit=8):
        for i in range(limit):
            self.tick.once(now + i)
            if self.task()["status"] == status:
                return now + i
        self.fail(f"never {status}: {self.task()['status']}")

    def events(self, kind):
        return [json.loads(r[0] or "{}") for r in self.conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind=? ORDER BY id",
            (self.tid, kind))]

    def test_a_docker_daemon_lost_mid_run_holds_transient_and_reruns_with_no_human_file(self):
        now = int(time.time())
        self.lose_daemon()
        self.script["implementer"].append([IMPL_DONE])
        at = self.pass_until("blocked", now)
        task = self.task()
        self.assertEqual((task["block_kind"], task["block_recurrences"]), ("transient", 1))
        self.assertEqual(self.events("blocked")[-1]["cause"], "exec_outage")
        self.tick.once(at + 1)                       # the daemon is still down: nothing released
        self.assertEqual(self.task()["status"], "blocked")
        self.runner.up = True                        # docker is back: the first pass releases it
        del self.hooks["implementer"]
        self.tick.once(at + 2)
        self.assertEqual(len(self.events("unblocked")), 1)
        self.assertIn(self.task()["status"], ("ready", "running"))
        self.assertEqual(self.events("intervention"), [])
        self.pass_until("review", at + 3)
        self.assertEqual(self.events("intervention"), [])

    def test_a_held_report_with_no_marker_stays_needs_input(self):
        self.script["implementer"] = [[HELD]]
        self.pass_until("blocked", int(time.time()))
        task = self.task()
        self.assertEqual(task["block_kind"], "needs_input")
        held = self.events("blocked")[-1]
        self.assertTrue(held["reason"].startswith("left undone / issues:"), held)
        self.assertNotIn("cause", held)

    def test_a_second_outage_on_one_card_goes_to_triage(self):
        now = int(time.time())
        self.lose_daemon()
        self.script["implementer"].append([HELD])
        at = self.pass_until("blocked", now)
        self.runner.up = True
        self.tick.once(at + 1)                       # released: ready
        self.pass_until("triage", at + 2)
        self.assertEqual(self.task()["block_recurrences"], 2)

    def test_a_clean_report_goes_to_review_marker_or_not(self):
        self.lose_daemon(report=IMPL_DONE)
        self.pass_until("review", int(time.time()))
        self.assertEqual(self.events("blocked"), [])

    def test_a_release_leaves_a_card_blocked_for_another_cause_alone(self):
        now = int(time.time())
        self.script["implementer"] = [[HELD]]
        at = self.pass_until("blocked", now)
        blocks.unblock(self.conn, self.tid, by="worker", now=at)
        blocks.block(self.conn, self.tid, kind="transient", reason="some other transient cause",
                     cause="other_cause", now=at)
        self.assertEqual(self.task()["block_kind"], "transient")
        lifts = len(self.events("unblocked"))
        self.runner.up = True                        # docker is up: only the cause filter holds it
        self.tick.once(at + 1)
        self.assertEqual(self.task()["status"], "blocked")
        self.assertEqual(len(self.events("unblocked")), lifts)

