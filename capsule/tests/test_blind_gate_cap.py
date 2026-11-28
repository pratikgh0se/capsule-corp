"""M110 and M115: a real tick with `redesign.blind_tests` = gate. The second verifier round that
still has failing blind tests holds the card with one decision for the owner (criterion beside
check) and sends no third implementer round; round 1 failures behave as before; `off` and `advisory`
(`report`) change nothing. The acceptance line (`gate.inputs` rows through `text.counts_parts`)
reads green, red (K of N failing) or none. Oracles: the card's status, its events, the outbox."""
import json
from unittest import mock

from capsule import interventions
from capsule.blind import gate, text as blind_text
from capsule.tests.test_blind_gate_tick import BODY, PASSING, GateTick
from capsule.tests.test_blind_run import blind_repo, record
from capsule.tests.test_tick import IMPL_DONE

FAILING_ID = "test_fails"


class CapCase(GateTick):
    def setUp(self):
        super().setUp()
        self.script["implementer"] = [[IMPL_DONE], [IMPL_DONE], [IMPL_DONE]]
        self.script["validator"] = [[self.document("pass", bad="pass")], [self.document("fail")],
                                    [self.document("fail")], [self.document("fail")]]
        self.conn.execute("UPDATE tasks SET body=? WHERE id=?",
                          (BODY + "2. the failure is reported\n", self.tid))
        self.conn.execute(
            "INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?,?,?,?)",
            (self.tid, "blind_tests_reported", json.dumps({"covered": [
                {"statement": 2, "tests": [f"{self.tid}.{FAILING_ID}"]},
                {"statement": 1, "tests": [f"{self.tid}.test_greets"]}],
                "not_testable": [], "question": ""}), 1))
        self.conn.commit()

    def run_until(self, done):
        for _ in range(40):
            self.tick.once()
            if done():
                return
        self.fail("the card never reached the expected state")

    def events(self, kind):
        return [json.loads(r[0] or "{}") for r in self.conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind=? ORDER BY id",
            (self.tid, kind))]

    def implementers(self):
        return len(self.runs("implementer"))

    def settle_round_one(self):
        # CheckGateCase.settle gives up after 10 ticks; under CPU load the rounds need more wall
        # time, so wait on the same state (round 1 failed, or done) for up to 40 ticks.
        self.run_until(lambda: self.task()["status"] == "done" or self.task()["failure_count"])

    def settle_two_rounds(self):
        self.run_until(lambda: self.task()["status"] in ("blocked", "done"))
        self.assertNotEqual(self.task()["status"], "triage")

    def human(self, kind, text="amend"):
        interventions.apply(self.conn, self.tick.cfg, self.tid,
                            {"kind": kind, "text": text, "author": "owner"}, f"{kind}.json",
                            f"{kind}.json", 1_000)


class SecondRoundCap(CapCase):
    def test_answer_and_unblock_do_not_start_a_third_round(self):
        self.settle_two_rounds()
        self.assertEqual(self.task()["block_kind"], "spec_stale")
        self.human("answer")
        self.human("unblock", "go on")
        for _ in range(8):
            self.tick.once()
        task = self.task()
        self.assertEqual((task["status"], task["block_kind"]), ("blocked", "spec_stale"))
        self.assertEqual(self.implementers(), 2)

    def test_the_reply_line_names_amend_and_close_not_answer(self):
        self.settle_two_rounds()
        row = self.conn.execute("SELECT body FROM outbox WHERE body LIKE '%amend%'").fetchone()
        self.assertIsNotNone(row)
        (doc,) = self.events("decision_requested")
        labels = " ".join(o["label"] for o in doc["options"])
        self.assertIn("amend '<reason>' --body-file <path>", labels)
        self.assertIn("close '<why>'", labels)
        self.assertNotIn(" answer ", labels)

    def test_a_reset_during_review_does_not_crash_the_cap_and_counts_no_round(self):
        self.settle_two_rounds()
        run = self.conn.execute("SELECT * FROM task_runs WHERE task_id=? AND profile='validator'"
                                " ORDER BY id DESC", (self.tid,)).fetchone()
        impl = self.runs("implementer")[-1]
        root = self.tick._spool_root(self.tick.cfg)
        self.assertEqual(gate._prior_rounds(self.conn, root, self.tid, run), 1)
        self.conn.execute("INSERT INTO task_events (task_id, kind, payload, created_at) VALUES"
                          " (?, 'failure_reset', '{}', ?)", (self.tid, run["started_at"] + 50))
        self.assertEqual(gate._prior_rounds(self.conn, root, self.tid, run), 0)
        self.assertFalse(gate.cap(self.conn, self.tick.cfg, root, self.task(), run,
                                  impl["run_id"], 1_000))

    def test_a_round_with_no_valid_verdict_is_no_failing_round_and_does_not_cap(self):
        self.settle_two_rounds()
        with mock.patch.object(gate, "cap", return_value=True) as cap:
            args = (self.conn, self.tick.cfg, self.tick._spool_root(self.tick.cfg), self.task())
            self.assertFalse(gate.hold(*args, None, "r1", 1, failures=["blind"],
                                       infra=["validator run r: no valid document"]))
            cap.assert_not_called()
            self.assertTrue(gate.hold(*args, {"error_class": None}, "r1", 1, failures=["blind"],
                                      infra=["reviewer run r: no report file"]))

    def test_a_validator_run_with_invalid_output_is_no_failing_round(self):
        self.settle_two_rounds()
        run = dict(self.conn.execute("SELECT * FROM task_runs WHERE task_id=? AND"
                                     " profile='validator' ORDER BY id DESC",
                                     (self.tid,)).fetchone(), error_class="invalid_output")
        root = self.tick._spool_root(self.tick.cfg)
        self.assertFalse(gate.cap(self.conn, self.tick.cfg, root, self.task(), run, "r1", 1))

    def test_a_retried_validator_run_counts_one_round(self):
        self.settle_round_one()          # round 1: an invalid document, then its retry
        self.assertGreaterEqual(len(self.runs("validator")), 2)
        self.assertEqual(self.task()["failure_count"], 1)
        self.assertEqual(self.events("decision_requested"), [])

    def test_round_one_failure_still_goes_back_to_the_implementer(self):
        self.settle_round_one()
        task = self.task()
        self.assertEqual(task["failure_count"], 1)
        self.assertEqual(self.events("decision_requested"), [])

    def test_round_two_failure_holds_the_card_with_one_decision_and_no_third_round(self):
        self.settle_two_rounds()
        task = self.task()
        self.assertEqual((task["status"], task["block_kind"]), ("blocked", "spec_stale"))
        self.assertEqual(self.implementers(), 2)
        (doc,) = self.events("decision_requested")
        self.assertEqual(doc["schema"], "decision.v1")
        self.assertEqual(doc["task_id"], self.tid)
        self.assertEqual(sorted(o["id"] for o in doc["options"]), ["amend", "close"])
        self.assertEqual(doc["recommended"], "amend")

    def test_the_decision_shows_the_criterion_beside_the_check(self):
        self.settle_two_rounds()
        (doc,) = self.events("decision_requested")
        ctx = doc["context"]
        self.assertIn("Criterion 2: the failure is reported", ctx)
        self.assertIn(f"Check {self.tid}.{FAILING_ID}", ctx)
        self.assertLess(ctx.index("Criterion 2"), ctx.index(f"Check {self.tid}.{FAILING_ID}"))
        for fact in ("given: a user runs test_fails", "expect: test_fails behaves",
                     "observed: echo no -> exit 0"):
            self.assertIn(fact, ctx)
        self.assertNotIn("test_greets", ctx)        # a passing test is not in the decision

    def test_later_ticks_raise_no_second_decision_and_no_second_hold(self):
        self.settle_two_rounds()
        for _ in range(5):
            self.tick.once()
        self.assertEqual(len(self.events("decision_requested")), 1)
        held = [e for e in self.events("blocked") if str(e.get("cause", "")).startswith(
            "blind_cap:")]
        self.assertEqual(len(held), 1)
        self.assertEqual(self.conn.execute(
            "SELECT count(*) FROM outbox WHERE body LIKE '%decision.v1%'").fetchone()[0], 1)

    def test_a_pass_in_round_two_carries_on(self):
        record(self.conn, self.tid, blind_repo(self.home / "p", self.tid, PASSING,
                                               board=self.cfg["board"]))
        self.cfg["blind_tests"]["repo"] = str(self.home / "p" / "sample-app-blind")
        self.write_cfg()
        self.script["validator"] = [[self.document("fail")], [self.document("pass", bad="pass")]]
        self.settle_two_rounds()
        self.assertEqual(self.task()["status"], "done")
        self.assertEqual(self.events("decision_requested"), [])

    def test_the_decision_never_repeats_a_withheld_test_line(self):
        self.settle_two_rounds()
        (doc,) = self.events("decision_requested")
        self.assertNotIn("VALIDATOR", json.dumps(doc))
        for line in doc["context"].splitlines():
            self.assertLessEqual(len(line), 220)


class SwitchOffOrAdvisory(CapCase):
    def run_mode(self, mode):
        self.cfg["redesign"] = {"blind_tests": mode}
        self.write_cfg()
        self.script["validator"] = [[self.document("fail")] for _ in range(4)]
        self.run_until(lambda: self.task()["status"] in ("blocked", "done")
                       or self.implementers() >= 3)

    def check_no_cap(self, mode):
        self.run_mode(mode)
        self.assertEqual(self.events("decision_requested"), [])
        self.assertEqual([e for e in self.events("blocked") if str(
            e.get("cause", "")).startswith("blind_cap:")], [])

    def test_off_holds_nothing_and_raises_no_decision(self):
        self.check_no_cap("off")

    def test_report_mode_holds_nothing_and_raises_no_decision(self):
        self.check_no_cap("report")

    def test_off_writes_no_acceptance_line_anywhere(self):
        self.run_mode("off")
        prompts = [(self.spool_dir(r["run_id"]) / "bundle.json").read_text()
                   for role in ("reviewer", "guide") for r in self.runs(role)]
        self.assertNotIn("acceptance:", " ".join(prompts))
        self.assertNotIn("acceptance:", self.outbox())


class AcceptanceLine(CapCase):
    def line(self, blind):
        return blind_text.counts_parts(blind)[0].splitlines()[-1]

    def test_green_red_and_none(self):
        self.assertEqual(self.line({"counts": {"pass": 2, "fail": 0}, "failing": []}),
                         "acceptance: green")
        self.assertEqual(self.line({"counts": {"pass": 1, "fail": 2}, "failing": ["a", "b"]}),
                         "acceptance: red (2 of 3 failing)")
        self.assertEqual(self.line({"counts": {}, "failing": []}), "acceptance: none")

    def test_a_gate_round_shows_the_line_to_reviewer_and_guide(self):
        self.settle()
        for role in ("reviewer", "guide"):
            sp = self.spool_dir(self.runs(role)[0]["run_id"])
            prompt = json.loads((sp / "bundle.json").read_text())["prompt"]
            self.assertEqual(prompt.count("acceptance: "), 1)
            self.assertIn("acceptance: red (1 of 2 failing)", prompt)

    def test_a_card_with_no_acceptance_statements_reads_none(self):
        self.conn.execute("UPDATE tasks SET body=?, blind_commit=NULL WHERE id=?",
                          ("Implements the timer.", self.tid))
        self.conn.commit()
        self.script["validator"] = [[self.document("pass", bad="pass")]]
        self.settle()
        runs = [r for role in ("reviewer", "guide") for r in self.runs(role)]
        self.assertTrue(runs)
        for run in runs:
            prompt = json.loads((self.spool_dir(run["run_id"]) / "bundle.json").read_text())
            self.assertIn("acceptance: none", prompt["prompt"])
            self.assertNotIn("Counts: .", prompt["prompt"])

    def test_a_card_with_no_landed_tests_reads_none(self):
        self.conn.execute("UPDATE tasks SET blind_commit=NULL WHERE id=?", (self.tid,))
        self.conn.commit()
        self.script["validator"] = [[self.document("pass", bad="pass")]]
        self.settle()
        roles = [self.runs(r) for r in ("reviewer", "guide")]
        for runs in roles:
            for run in runs:
                prompt = (self.spool_dir(run["run_id"]) / "bundle.json").read_text()
                self.assertIn("acceptance: none", prompt)
