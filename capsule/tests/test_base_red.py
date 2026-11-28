"""Was it already red (capsule/base_red.py): a failing contract command is re-run once on the
card's start commit; the failure text says whether the card broke it, and a branch that was red
from the start opens one incident.

Oracle 1: fake `run` callables with scripted exit codes (the commands the container would see).
Oracle 2: the rows, failure lines and the incidents table, read back."""
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from capsule import base_red, db, paths

SHA = "a" * 40
CMD = "python3 -m unittest discover -s tests"
BOARD, NOW = "t", 1_800_000_000


class Run:
    """A stand-in for the container exec: the setup always works unless `setup` says otherwise;
    the command's exit is `rc`."""

    def __init__(self, rc, setup=0, timeout=False):
        self.rc, self.setup, self.timeout, self.calls = rc, setup, timeout, []

    def __call__(self, cmd, timeout):
        self.calls.append(cmd)
        if len(self.calls) == 1:
            return self.setup, ""
        if self.timeout:
            raise subprocess.TimeoutExpired(cmd, timeout)
        return self.rc, ""


def check(run, rc=1, sha=SHA, cmd=CMD):
    return base_red.check(run, [(cmd, rc, "boom", 1)], (sha, {cmd}), "/.gitdirs/x", 30)


class Check(unittest.TestCase):
    def test_fails_on_both(self):
        run = Run(1)
        self.assertEqual(check(run), {CMD: "red"})
        self.assertIn(SHA, run.calls[0])                   # the start commit is what is extracted
        self.assertIn(CMD, run.calls[1])                   # the same command, re-run once
        self.assertEqual(len(run.calls), 2)

    def test_flaky_when_the_head_rerun_passes(self):
        self.assertEqual(check(Run(0)), {CMD: "flaky"})

    def test_a_passing_command_is_not_rerun(self):
        run = Run(0)
        self.assertEqual(check(run, rc=0), {})
        self.assertEqual(run.calls, [])

    def test_a_timeout_on_head_is_not_a_failure(self):
        run = Run(0)
        done = [(CMD, 124, "timed out after 30s", 30)]
        self.assertEqual(base_red.check(run, done, (SHA, {CMD}), "/g", 30), {})

    def test_unknown_without_a_start_sha(self):
        run = Run(1)
        self.assertEqual(check(run, sha=None), {CMD: "unknown"})
        self.assertEqual(run.calls, [])

    def test_unknown_when_the_checkout_fails_or_the_rerun_times_out(self):
        run = Run(1, setup=128)
        self.assertEqual(check(run), {CMD: "unknown"})
        self.assertEqual(len(run.calls), 1)                # the command is never run
        self.assertEqual(check(Run(1, timeout=True)), {CMD: "unknown"})

    def test_only_the_boards_commands_are_rerun(self):
        run = Run(1)
        done = [("gate cmd", 1, "x", 1)]
        self.assertEqual(base_red.check(run, done, (SHA, {CMD}), "/g", 30), {})
        self.assertEqual(run.calls, [])


def spool(verdict, outs):
    root = Path(tempfile.mkdtemp())
    (root / "r1").mkdir()
    (root / "r1" / "job.json").write_text(json.dumps({"inputs": {"outputs": outs}}))
    return root, {"run_id": "r1", "handoff": json.dumps({"verdict": verdict})}


class Lines(unittest.TestCase):
    def rows(self, result):
        return base_red.rows([{"cmd": CMD, "exit": 1, "output": "boom"}], {CMD: result}, SHA)

    def test_pre_existing_text(self):
        root, run = spool("fail", self.rows("red"))
        [line] = base_red.lines(root, run)
        self.assertIn(f"`{CMD}` also fails on the start commit {SHA[:12]}", line)
        self.assertIn("pre-existing, not caused by this card", line)

    def test_caused_by_card_text(self):
        root, run = spool("fail", self.rows("pass"))
        [line] = base_red.lines(root, run)
        self.assertIn(f"passes on the start commit {SHA[:12]}: caused by this card", line)

    def test_unknown_and_a_passing_verdict_say_nothing(self):
        self.assertEqual(base_red.lines(*spool("fail", self.rows("unknown"))), [])
        self.assertEqual(base_red.lines(*spool("pass", self.rows("red"))), [])
        self.assertEqual(base_red.lines(Path("/nowhere"), {"run_id": "r", "handoff": "{}"}), [])


class Record(unittest.TestCase):
    def setUp(self):
        home = Path(tempfile.mkdtemp())
        p = paths.board_db(BOARD, home)
        p.parent.mkdir(parents=True)
        db.create_board(p)
        self.conn = db.connect(p)
        self.tid = db.new_task(self.conn, title="card", status="review", branch="epic/x", now=NOW)
        self.task = {"id": self.tid}

    def rec(self, results):
        outs = [{"cmd": c, "exit": 1, "output": "E1\nE2\nE3", "base": {"sha": SHA, "result": r}}
                for c, r in results.items()]
        return base_red.record(self.conn, {"board": BOARD}, self.task, outs, NOW)

    def incidents(self):
        return self.conn.execute("SELECT kind, root_key, description FROM incidents").fetchall()

    def test_red_on_both_opens_one_incident_that_repeats_dedupe(self):
        self.assertEqual(len(self.rec({CMD: "red"})), 1)
        [row] = self.incidents()
        self.assertEqual(row["kind"], "silent_failure")
        self.assertEqual(row["root_key"],
                         f"silent_failure: base-red: {BOARD}: python3-m-unittest-discover-s-tests")
        for part in (CMD, SHA[:12], "E1\nE2\nE3"):
            self.assertIn(part, row["description"])
        self.rec({CMD: "red"})
        self.assertEqual(len(self.incidents()), 1)         # the same key dedupes

    def test_failing_only_on_head_opens_nothing(self):
        self.assertEqual(self.rec({CMD: "pass"}), [])
        self.assertEqual(self.incidents(), [])

    def test_one_command_the_card_broke_opens_nothing(self):
        self.assertEqual(self.rec({CMD: "red", "other": "pass"}), [])
        self.assertEqual(self.rec({CMD: "red", "other": "unknown"}), [])
        self.assertEqual(self.incidents(), [])

    def test_unknown_opens_nothing(self):
        self.assertEqual(self.rec({CMD: "unknown"}), [])
        self.assertEqual(self.incidents(), [])

    def test_a_passing_job_opens_nothing(self):
        outs = [{"cmd": CMD, "exit": 0, "output": ""}]
        self.assertEqual(base_red.record(self.conn, {"board": BOARD}, self.task, outs, NOW), [])


if __name__ == "__main__":
    unittest.main()
