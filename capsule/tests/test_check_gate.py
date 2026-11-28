"""M022: a card's `check_cmd` runs as a contract command behind the `check_gate` redesign switch
and a non-zero exit (or a missing `check_expect` substring) fails the round, whatever the
code-blind validator says. Own module so test_tick.py (frozen file length) does not grow.

Oracles: the validator's job.json outputs (what the container ran) and the card's status and
failure comment (what the round decided)."""
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from capsule import check_gate, db, intent
from capsule.tests.test_tick import BRANCH, GUIDE_OK, IMPL_DONE, REV_PASS, VAL_PASS, TickCase


def shell_exec(runner):
    """A fake container that really runs the card's check on the host; every other contract
    command passes as `ok` (the FakeRunner default)."""
    def run(name, cmd, timeout):
        if not isinstance(cmd, check_gate.CheckCmd):
            return 0, "ok"
        done = subprocess.run(str(cmd), shell=True, capture_output=True, text=True, timeout=30)
        return done.returncode, done.stdout + done.stderr
    runner.exec = run


class CheckGateCase(TickCase):
    def setUp(self):
        super().setUp()
        shell_exec(self.runner)
        self.script["implementer"] = [[IMPL_DONE]]

    def switch(self, on):
        self.cfg["redesign"] = {"check_gate": on}
        self.write_cfg()

    def set_check(self, cmd, expect=None, tid=None):
        self.conn.execute("UPDATE tasks SET check_cmd=?, check_expect=? WHERE id=?",
                          (cmd, expect, tid or self.tid))
        self.conn.commit()

    def settle(self, tid=None):
        """Tick until the card is done or its first round failed."""
        for _ in range(10):
            self.tick.once()
            row = self.conn.execute("SELECT status, failure_count FROM tasks WHERE id=?",
                                    (tid or self.tid,)).fetchone()
            if row["status"] == "done" or row["failure_count"]:
                break

    def validator_files(self, tid=None):
        """(job.json as a dict, bundle.json prompt) of the card's last validator run."""
        run = self.conn.execute(
            "SELECT run_id FROM task_runs WHERE task_id=? AND profile='validator' ORDER BY id DESC",
            (tid or self.tid,)).fetchone()
        sp = self.spool_dir(run["run_id"])
        return (json.loads((sp / "job.json").read_text()),
                json.loads((sp / "bundle.json").read_text())["prompt"])

    def outputs(self, tid=None):
        return self.validator_files(tid)[0]["inputs"]["outputs"]

    def check_entry(self, cmd):
        return [o for o in self.outputs() if o["cmd"] == cmd]

    def last_comment(self):
        return self.conn.execute("SELECT body FROM task_comments WHERE task_id=? ORDER BY id DESC",
                                 (self.tid,)).fetchone()["body"]


class CheckCmdRuns(CheckGateCase):
    def test_a_failing_check_records_exit_1_and_fails_the_round_despite_a_passing_validator(self):
        self.switch(True)
        self.set_check("false")
        self.script["validator"] = [[VAL_PASS]]          # a validator that wrongly passes
        self.settle()
        self.assertEqual([e["exit"] for e in self.check_entry("false")], [1])
        self.assertIn("- cmd: false\n  exit: 1", self.validator_files()[1])
        self.assertEqual(self.task()["failure_count"], 1)
        self.assertIn("check_cmd `false` exited 1", self.last_comment())

    def test_a_passing_check_records_exit_0_and_the_card_is_done(self):
        self.switch(True)
        self.set_check("true")
        self.settle()
        self.assertEqual([e["exit"] for e in self.check_entry("true")], [0])
        self.assertEqual((self.task()["status"], self.task()["failure_count"]), ("done", 0))

    def test_check_expect_is_a_required_substring_of_the_output(self):
        self.switch(True)
        self.set_check("echo hello world", "hello")
        self.settle()
        self.assertEqual(self.task()["status"], "done")

    def test_a_missing_check_expect_fails_the_round_even_on_exit_0(self):
        self.switch(True)
        self.set_check("echo hello world", "goodbye")
        self.settle()
        entry = self.check_entry("echo hello world")[0]
        self.assertEqual(entry["exit"], 0)
        self.assertIn("check_expect 'goodbye' not found", entry["output"])
        self.assertEqual(self.task()["failure_count"], 1)
        self.assertIn("check_expect 'goodbye' not in its output", self.last_comment())


class CheckGateOff(CheckGateCase):
    def test_switch_off_never_runs_the_check(self):
        self.switch(False)
        self.set_check("false")
        self.settle()
        self.assertEqual(self.check_entry("false"), [])
        self.assertEqual((self.task()["status"], self.task()["failure_count"]), ("done", 0))

    def test_a_card_without_a_check_cmd_is_unchanged_with_the_switch_on(self):
        self.switch(True)
        self.settle()
        outs = self.outputs()
        self.assertTrue(all(isinstance(o.pop("seconds"), int) for o in outs))
        self.assertEqual(outs, [{"cmd": c, "exit": 0, "output": "ok"}
                                for c in self.cfg["contract_commands"]])
        self.assertEqual(self.task()["status"], "done")


class SwitchOffIsByteIdentical(CheckGateCase):
    """Z2 second oracle: with the switch off, a card carrying a check_cmd gets the same validator
    bundle and contract outputs as an identical card with none (task and run ids normalised)."""

    def normalised(self, tid):
        job, prompt = self.validator_files(tid)
        text = json.dumps(job["inputs"], sort_keys=True) + prompt
        return re.sub(r"[0-9a-f]{32}", "RUN", text.replace(tid, "TASK"))

    def test_switch_off_bundle_matches_a_card_with_no_check(self):
        self.switch(False)
        self.settle()
        self.assertEqual(self.task()["status"], "done")
        plain = self.normalised(self.tid)
        self.script.update(implementer=[[IMPL_DONE]], validator=[[VAL_PASS]], reviewer=[[REV_PASS]],
                           guide=[[GUIDE_OK]])
        tid2 = db.new_task(self.conn, title="timer screen", body="Implements §6.8, rows 417, 420.",
                           assignee="dev-frontend", branch=BRANCH)
        self.set_check("false", "x", tid=tid2)
        self.settle(tid2)
        self.assertEqual(self.conn.execute("SELECT status FROM tasks WHERE id=?",
                                           (tid2,)).fetchone()[0], "done")
        self.assertEqual(self.normalised(tid2), plain)


class CommandsUnit(CheckGateCase):
    ON = {"redesign": {"check_gate": True}}

    def test_commands_is_empty_when_off_or_no_check(self):
        card = {"check_cmd": "pytest -q", "check_expect": "ok"}
        self.assertEqual(check_gate.commands({}, card, None, None), [])
        for blank in (None, "", "  "):
            blank_card = dict(card, check_cmd=blank)
            self.assertEqual(check_gate.commands(self.ON, blank_card, None, None), [])

    def test_commands_carries_the_expectation(self):
        card = {"check_cmd": " pytest -q ", "check_expect": " ok "}
        got = check_gate.commands(self.ON, card, None, None)
        self.assertEqual(got, ["pytest -q"])
        self.assertEqual(got[0].expect, "ok")


class RerunAndMissingEvidence(CheckGateCase):
    ON = {"redesign": {"check_gate": True}}

    def test_the_implementer_rerun_bundle_names_the_failed_check(self):
        self.switch(True)
        self.set_check("false")
        self.script["implementer"] = [[IMPL_DONE], [IMPL_DONE]]
        for _ in range(10):
            self.tick.once()
            if len(self.runs("implementer")) == 2:
                break
        sp = self.spool_dir(self.runs("implementer")[1]["run_id"])
        findings = json.loads((sp / "job.json").read_text())["inputs"]["review_findings"]
        self.assertTrue(any("check_cmd `false` exited 1" in f for f in findings), findings)

    def fake_run(self, outputs):
        """A validator run row whose spool job.json holds `outputs` (None: no file at all)."""
        spool = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, spool, ignore_errors=True)
        (spool / "r1").mkdir()
        if outputs is not None:
            (spool / "r1" / "job.json").write_text(json.dumps({"inputs": {"outputs": outputs}}))
        return spool, {"task_id": self.tid, "run_id": "r1"}

    def test_a_card_check_missing_from_the_evidence_is_a_did_not_run_failure(self):
        self.set_check("pytest -q")
        for outputs in (None, [], [{"cmd": "make test", "exit": 0, "output": "ok"}]):
            spool, run = self.fake_run(outputs)
            got = check_gate.failures(self.ON, self.conn, spool, run)
            self.assertEqual(len(got), 1, outputs)
            self.assertIn("card check did not run", got[0])
        spool, run = self.fake_run(None)
        (spool / "r1" / "job.json").write_text("{not json")        # unreadable, not just absent
        self.assertIn("did not run", check_gate.failures(self.ON, self.conn, spool, run)[0])

    def test_no_failure_line_when_off_without_a_check_or_without_a_validator_run(self):
        spool, run = self.fake_run(None)
        self.set_check("pytest -q")
        self.assertEqual(check_gate.failures({}, self.conn, spool, run), [])      # switch off
        self.assertEqual(check_gate.failures(self.ON, self.conn, spool, None), [])
        self.set_check(None)
        self.assertEqual(check_gate.failures(self.ON, self.conn, spool, run), [])  # no check_cmd

    def test_a_present_passing_check_is_no_failure(self):
        self.set_check("true")
        spool, run = self.fake_run([{"cmd": "true", "exit": 0, "output": "", "check": True}])
        self.assertEqual(check_gate.failures(self.ON, self.conn, spool, run), [])


class IntentExamples(CheckGateCase):
    """contracts/intent/check_gate.jsonl, replayed on the real tick (Z1); an unknown shape fails."""

    def example(self, n):
        """Row n; each row replays in its own test, so it gets a fresh board."""
        examples = intent.load("check_gate")
        self.assertEqual(len(examples), 2)                # an absent file would pass vacuously
        return examples[n]["given"]["document"], examples[n]["expect"]

    def test_a_failing_card_check_fails_the_round_despite_passing_verdicts(self):
        self.replay(*self.example(0))

    def test_switch_off_never_runs_the_check(self):
        self.replay(*self.example(1))

    def replay(self, doc, exp):
        self.assertEqual(doc["check"], "check_gate")
        self.assertLessEqual(set(doc), {"check", "check_gate", "check_cmd", "check_expect",
                                        "verdicts"})
        self.assertLessEqual(set(exp), {"check_exit", "check_ran", "card_state", "failure_count",
                                        "merged"})
        self.assertEqual(set(doc["verdicts"].values()), {"pass"})   # the rows script all-pass
        self.assertIn(doc["check_gate"], ("on", "off"))
        self.switch(doc["check_gate"] == "on")
        self.set_check(doc["check_cmd"], doc["check_expect"])
        self.tick.once()
        self.tick.once()
        self.runner.up = False        # the failed round's retry waits, as in the content-fail case
        self.tick.once()
        self.runner.up = True
        task = self.task()
        entry = [o for o in self.outputs() if o["cmd"] == doc["check_cmd"]]
        if "check_exit" in exp:
            self.assertEqual([e["exit"] for e in entry], [exp["check_exit"]])
        if "check_ran" in exp:
            self.assertEqual(bool(entry), exp["check_ran"])
        if "card_state" in exp:
            self.assertEqual(task["status"], exp["card_state"])
        self.assertEqual(task["failure_count"], exp["failure_count"])
        self.assertEqual(task["status"] == "done", exp["merged"])
