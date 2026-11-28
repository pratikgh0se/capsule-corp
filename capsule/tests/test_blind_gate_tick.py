"""R32 (B4a): a real tick with `redesign.blind_tests` = gate, the strong runner a stand-in (as in
test_blind_run_tick.py: the blind run line really runs on the host through the harness). Oracles:
the card's status and comments (what the round decided), the validator's job.json (what it was
given) and `Tick._open_findings` (what the next implementer round is told)."""
import json
from pathlib import Path

from capsule.tests import test_blind_run
from capsule.tests.test_blind_run import Container, blind_repo, record
from capsule.tests.test_check_gate import CheckGateCase
from capsule.tests.test_tick import IMPL_DONE, VAL_PASS

TESTS = test_blind_run.TESTS.replace("cat hello.txt", "cat README.md").replace(
    '"hi\\n"', '"my-app\\n"')      # the card's worktree is the tick's own clone
BODY = "Implements the timer.\n\nAcceptance:\n1. the greeting is printed\n"
PASSING = TESTS.replace('run("echo no").exit, 1', 'run("echo no").exit, 0')


class GateTick(CheckGateCase):
    def setUp(self):
        super().setUp()
        self.conn.execute("UPDATE tasks SET body=? WHERE id=?", (BODY, self.tid))
        self.conn.commit()
        self.repo = blind_repo(self.home / "blind", self.tid, TESTS, board=self.cfg["board"])
        record(self.conn, self.tid, self.repo)
        self.boxes = []
        start, exec_ = self.runner.start, self.runner.exec

        def begin(run_id, ws, **kw):
            name = start(run_id, ws, **kw)
            if "mounts" in kw:
                box = Container(ws)
                box.start(run_id, ws, **kw)
                self.boxes.append(box)
            return name

        def run(name, cmd, timeout):
            if self.boxes and "harness.unittest_runner" in cmd:
                return self.boxes[-1].exec(name, cmd, timeout)
            return exec_(name, cmd, timeout)
        self.runner.start, self.runner.exec = begin, run
        self.tick.contracts.grace = 60      # the contract job takes real time: a pass waits for it
        self.script["implementer"] = [[IMPL_DONE]]
        self.cfg["redesign"] = {"blind_tests": "gate"}
        self.cfg["blind_tests"] = {"repo": str(self.repo), "adapter": "unittest"}
        self.write_cfg()

    def document(self, verdict, bad="fail", good="pass"):
        return dict(VAL_PASS, verdict=verdict, assertions=[
            *VAL_PASS["assertions"],
            {"cmd": f"blind:{self.tid}.test_greets", "exit": 0, "result": good},
            {"cmd": f"blind:{self.tid}.test_fails", "exit": 1, "result": bad,
             "excerpt": "VALIDATOR"}])

    def comments(self):
        return [r["body"] for r in self.conn.execute(
            "SELECT body FROM task_comments WHERE task_id=?", (self.tid,))]

    def outbox(self):
        return " ".join(r["body"] for r in self.conn.execute("SELECT body FROM outbox"))

    def findings(self):
        return self.tick._open_findings(self.conn, self.tick.cfg, self.tid, [], now=1)


class FailingBlindTest(GateTick):
    def setUp(self):
        super().setUp()
        self.script["validator"] = [[self.document("pass", bad="pass")], [self.document("fail")]]
        self.settle()

    def test_a_validator_that_passes_a_failing_blind_test_is_told_its_document_is_invalid(self):
        first = self.runs("validator")[0]
        self.assertEqual(first["error_class"], "invalid_output")
        self.assertIn("did not fail failing blind test", first["error"])

    def test_the_card_fails_the_round_and_goes_back_to_ready(self):
        task = self.task()
        self.assertIn(task["status"], ("ready", "running"))     # back to the implementer
        self.assertEqual(task["failure_count"], 1)

    def test_the_implementer_learns_what_to_fix_and_never_the_test(self):
        blind = [f for f in self.findings() if f.startswith("blind:")]
        want = (f"blind:{self.tid}.test_fails failed\ngiven: a user runs test_fails\n"
                "expect: test_fails behaves\nobserved: echo no -> exit 0\nno\n")
        self.assertEqual(blind, [want])
        text = "\n".join(self.findings())
        self.assertNotIn("VALIDATOR", text)         # the validator's own words are not a finding
        self.assertNotIn("assert", text.lower())

    def test_the_comment_events_and_outbox_hold_counts_and_ids_only(self):
        self.assertEqual(self.comments()[0],
                         f"Review failed:\n- blind tests: 1 of 2 failed")
        for text in (self.outbox(), json.dumps([dict(r) for r in self.conn.execute(
                "SELECT payload FROM task_events WHERE kind='failed'")])):
            self.assertNotIn("a user runs", text)
            self.assertNotIn("echo no", text)

    def test_the_reviewer_and_guide_get_counts_and_failing_ids_only(self):
        want = {"counts": {"pass": 1, "fail": 1, "error": 0, "skipped": 0},
                "failing": [f"{self.tid}.test_fails"]}
        for role in ("reviewer", "guide"):
            sp = self.spool_dir(self.runs(role)[0]["run_id"])
            job = json.loads((sp / "job.json").read_text())
            self.assertEqual(job["inputs"]["blind"], want)
            prompt = json.loads((sp / "bundle.json").read_text())["prompt"]
            self.assertIn("Counts: pass 1, fail 1, error 0, skipped 0", prompt)
            self.assertNotIn("a user runs", prompt)


class CitationRule(GateTick):
    def test_a_fail_citing_a_blind_row_that_did_not_fail_is_invalid_even_with_blind_tests_off(self):
        self.cfg["redesign"] = {"blind_tests": "off"}
        self.write_cfg()
        bad = dict(VAL_PASS, verdict="fail", assertions=[
            *VAL_PASS["assertions"], {"cmd": f"blind:{self.tid}.test_greets", "exit": 0,
                                      "result": "fail"}])
        self.script["validator"] = [[bad], [VAL_PASS]]
        self.settle()
        first = self.runs("validator")[0]
        self.assertEqual(first["error_class"], "invalid_output")
        self.assertIn("did not fail", first["error"])
        self.assertEqual(self.task()["status"], "done")


class PassingAndHeld(GateTick):
    def test_all_blind_tests_passing_lets_the_card_land(self):
        record(self.conn, self.tid, blind_repo(self.home / "p", self.tid, PASSING,
                                               board=self.cfg["board"]))
        self.cfg["blind_tests"]["repo"] = str(self.home / "p" / "sample-app-blind")
        self.write_cfg()
        self.script["validator"] = [[self.document("pass", bad="pass")]]
        self.settle()
        self.assertEqual(self.task()["status"], "done")

    def test_a_card_with_no_landed_tests_is_held_without_a_finding(self):
        self.conn.execute("UPDATE tasks SET blind_commit=NULL WHERE id=?", (self.tid,))
        self.conn.commit()
        self.script["validator"] = [[VAL_PASS]]
        self.settle()
        task = self.task()
        self.assertEqual((task["status"], task["block_kind"], task["failure_count"]),
                         ("blocked", "needs_input", 0))
        reason = json.loads(self.conn.execute(
            "SELECT payload FROM task_events WHERE kind='blocked' AND task_id=?",
            (self.tid,)).fetchone()[0])["reason"]
        self.assertEqual(reason, "no blind tests yet")
        self.assertEqual([f for f in self.findings() if "blind" in f], [])
        self.assertEqual(self.boxes, [])                # nothing ran: no tests to run

    def test_report_mode_is_untouched_by_the_gate_code(self):
        self.cfg["redesign"] = {"blind_tests": "report"}
        self.write_cfg()
        self.script["validator"] = [[VAL_PASS]]
        self.settle()
        self.assertEqual(self.task()["status"], "done")


class WrongShapeJob(GateTick):
    """Statement 23: a validator job file of the wrong shape holds the card as unavailable and
    nothing raises out of the pass (no `tick_error` that repeats every pass)."""

    def spoil(self, text):
        spawn = self.tick.spawn

        def spoiled(board_db, spool_dir, run_id, role):
            out = spawn(board_db, spool_dir, run_id, role)
            if role == "validator":
                (Path(spool_dir) / "job.json").write_text(text)
            return out
        self.tick.spawn = spoiled

    def test_inputs_that_is_no_object_holds_the_card_and_raises_nothing(self):
        for text in ('{"inputs": 5}', '{"inputs": [1]}', '{"inputs": {"outputs": "x"}}'):
            with self.subTest(job=text):
                self.setUp()
                self.script["validator"] = [[VAL_PASS]]
                self.spoil(text)
                self.settle()
                task = self.task()
                self.assertEqual((task["status"], task["block_kind"]), ("blocked", "needs_input"))
                reason = json.loads(self.conn.execute(
                    "SELECT payload FROM task_events WHERE kind='blocked' AND task_id=?",
                    (self.tid,)).fetchone()[0])["reason"]
                self.assertEqual(reason, "blind tests unavailable")
                self.assertEqual(self.conn.execute(
                    "SELECT count(*) FROM task_events WHERE kind='tick_error'").fetchone()[0], 0)
