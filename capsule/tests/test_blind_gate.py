"""R32 (B4a): gate mode of the blind run. `RunCase` (test_blind_run.py) runs the real harness
through a stand-in container; here `redesign.blind_tests` is `gate`. Two oracles per claim: the
rows the validator is given (job.json shape) and the `blind_gated` event the run leaves, or the
rendered blind_result.json in the spool."""
import json
import unittest
from unittest import mock

from capsule import db, incidents, interventions, review_findings
from capsule import validator_report
from capsule.blind import gate
from capsule.blind import run as blind_run
from capsule.tests.test_blind_run import TESTS, Container, RunCase, blind_repo, record

ACCEPT = "Acceptance:\n1. the greeting is printed\n"
BROKEN_SETUP = ("import unittest\n\nfrom harness import run\n\n\nclass Greeting(unittest.TestCase):"
                "\n    @classmethod\n    def setUpClass(cls):\n        raise RuntimeError('no')\n\n"
                "    def test_greets(self):\n        self.assertEqual(run('true').exit, 0)\n")
SECRET = 'TEXT = """\nthe secret line of the test\n"""\n\n\nclass Greeting'
LEAKY = TESTS.replace('run("echo no")', 'run("echo " + TEXT)').replace("class Greeting", SECRET)
SKIP = "    @unittest.skip('later')\n    def "
SKIPPING = TESTS.replace("    def test_greets", SKIP + "test_greets").replace(
    "    def test_fails", SKIP + "test_fails")
PASS_SKIP = TESTS.replace('run("echo no").exit, 1', 'run("echo no").exit, 0').replace(
    "    def test_greets", SKIP + "test_greets")


class GateCase(RunCase):
    mode = "gate"

    def eligible(self, body=ACCEPT):
        self.conn.execute("UPDATE tasks SET body=?, tier='strong' WHERE id=?", (body, self.card))

    def cfg(self, **over):
        return super().cfg(**{"epics": ["epic/x"], **over})

    def use(self, source, name):
        """Land `source` as the card's tests in a fresh blind repo, which the board then uses."""
        self.use_names(("test_greets", "test_fails"), name, source)

    def use_names(self, names, name, source=TESTS):
        self.repo = blind_repo(self.root / name, self.card, source, names=names)
        record(self.conn, self.card, self.repo)

    def landed(self, acceptance):
        db.event(self.conn, self.card, "blind_landed", {"acceptance": acceptance})

    def events(self):
        return [json.loads(r["payload"]) for r in self.conn.execute(
            "SELECT payload FROM task_events WHERE kind='blind_gated'")]


class TestScrubbedSpelling(unittest.TestCase):
    def test_a_backticked_or_upper_case_blind_cmd_loses_its_excerpt_and_expected(self):
        for cmd in ("`blind:x`", "BLIND:x", " Blind:x"):
            doc = {"assertions": [{"cmd": cmd, "excerpt": "SECRET", "expected": "SECRET"}]}
            self.assertEqual(gate.scrubbed(doc)["assertions"], [{"cmd": cmd}], cmd)


class TestRows(GateCase):
    def test_tests_are_gate_rows_and_a_failing_one_carries_its_finding_facts(self):
        rows = {r["cmd"]: r for r in self.go(Container(self.ws))}
        bad, good = rows[f"blind:{self.card}.test_fails"], rows[f"blind:{self.card}.test_greets"]
        self.assertEqual((bad["blind"], bad["result"], bad["exit"]), ("gate", "fail", 1))
        self.assertEqual(set(bad["test"]), {"id", "given", "expect", "observed"})
        self.assertEqual(bad["test"]["given"], "a user runs test_fails")
        self.assertIn("echo no -> exit 0", bad["test"]["observed"])
        self.assertEqual((good["blind"], good["result"], "test" in good), ("gate", "pass", False))

    def test_the_run_leaves_one_event_with_counts_only(self):
        self.go(Container(self.ws))
        self.assertEqual(self.events(), [{
            "counts": {"pass": 1, "fail": 1, "error": 0, "skipped": 0},
            "failing": 1, "leaks": 0, "held": "", "detail": "",
            "run_id": self.run_id}])

    def test_report_mode_rows_are_unchanged(self):
        self.mode = "report"
        rows = self.go(Container(self.ws))
        self.assertEqual({r["blind"] for r in rows}, {"report"})
        self.assertFalse(any({"result", "test"} & set(r) for r in rows))
        self.assertEqual(self.events(), [])

    def test_a_card_with_no_landed_tests_that_would_get_a_tester_is_held_pending(self):
        self.eligible()
        self.conn.execute("UPDATE tasks SET blind_commit=NULL WHERE id=?", (self.card,))
        box = Container(self.ws)
        (row,) = self.go(box)
        self.assertEqual((row["cmd"], row["blind"], row["reason"]),
                         ("blind:pending", "hold", "no blind tests yet"))
        self.assertEqual(box.started, [])
        self.assertEqual(self.events()[0]["held"], "no blind tests yet")

    def test_cards_that_would_get_no_tester_go_on_as_before(self):
        self.conn.execute("UPDATE tasks SET blind_commit=NULL WHERE id=?", (self.card,))
        for body, tier, epics in (("no acceptance here", "strong", ["epic/x"]),
                                  (ACCEPT, "light", ["epic/x"]), (ACCEPT, "strong", ["other"])):
            self.eligible(body)
            self.conn.execute("UPDATE tasks SET tier=? WHERE id=?", (tier, self.card))
            self.assertEqual(self.go(Container(self.ws), self.cfg(epics=epics)), [])

    def test_a_broken_board_holds_an_eligible_card_and_not_the_others(self):
        self.eligible()
        cfg = self.cfg(blind_tests={"repo": str(self.root / "gone"), "adapter": "unittest"})
        (row,) = self.go(Container(self.ws), cfg)
        self.assertEqual((row["cmd"], row["blind"]), ("blind:unavailable", "hold"))
        self.eligible("none")
        self.conn.execute("UPDATE tasks SET blind_commit=NULL WHERE id=?", (self.card,))
        (row,) = self.go(Container(self.ws), cfg)
        self.assertEqual((row["cmd"], row["blind"]), ("blind:unavailable", "report"))

    def test_tests_for_another_acceptance_text_are_stale_and_the_card_is_held(self):
        self.eligible()
        self.landed(["something else"])
        (row,) = self.go(Container(self.ws))
        self.assertEqual((row["cmd"], row["reason"]), ("blind:pending", "no blind tests yet"))

    def test_tests_for_the_same_acceptance_text_are_not_stale_whatever_else_the_body_says(self):
        self.eligible(ACCEPT + "\nMore prose added later.\n")
        self.landed(["the greeting is printed"])
        self.assertEqual({r["blind"] for r in self.go(Container(self.ws))}, {"gate"})

    def test_a_landed_card_is_gated_whatever_its_body_says(self):
        self.assertEqual({r["blind"] for r in self.go(Container(self.ws), self.cfg(epics=[]))},
                         {"gate"})

    def test_a_run_that_breaks_is_a_hold_never_a_pass(self):
        (row,) = self.go(Container(self.ws, boom=RuntimeError("docker is gone")))
        self.assertEqual((row["blind"], row["reason"]), ("hold", "blind tests unavailable"))

    def test_a_count_mismatch_is_inconclusive_and_held_with_an_incident(self):
        box = Container(self.ws, tamper=lambda text: text.replace("FAILED", "OK"))
        (row,) = self.go(box)
        self.assertEqual((row["cmd"], row["blind"]), ("blind:inconclusive", "hold"))
        self.assertEqual([i["kind"] for i in incidents.listing(self.conn)], ["review_infra"])
        self.assertNotIn("test", row)

    def test_skipped_only_is_a_hold(self):
        self.use(SKIPPING, "s")
        (row,) = self.go(Container(self.ws))
        self.assertEqual((row["cmd"], row["blind"], row["reason"]),
                         ("blind:skipped", "hold", "a blind test was skipped"))

    def test_one_skipped_test_among_passing_ones_is_a_hold_never_a_pass(self):
        self.use(PASS_SKIP, "one")
        (row,) = self.go(Container(self.ws))
        self.assertEqual((row["cmd"], row["blind"], row["reason"]),
                         ("blind:skipped", "hold", "a blind test was skipped"))

    def test_a_failure_beside_a_skip_is_a_fix_round_not_a_hold(self):
        self.use(TESTS.replace("    def test_greets", SKIP + "test_greets"), "fs")
        rows = {r["cmd"]: r for r in self.go(Container(self.ws))}
        self.assertEqual({r["blind"] for r in rows.values()}, {"gate"})
        self.assertEqual(rows[f"blind:{self.card}.test_greets"]["result"], "skipped")
        bad = rows[f"blind:{self.card}.test_fails"]
        self.assertEqual((bad["result"], bad["exit"], "test" in bad), ("fail", 1, True))

    def test_a_setup_failure_on_a_landed_card_is_a_failure_with_a_neutral_id(self):
        self.use_names(("test_greets", "test_fails", "test_gone"), "su", BROKEN_SETUP)
        commit = self.conn.execute("SELECT blind_commit FROM tasks WHERE id=?",
                                   (self.card,)).fetchone()[0]
        db.event(self.conn, self.card, "blind_landed", {
            "blind_commit": commit, "acceptance": [],
            "tests": [f"{self.card}.{n}" for n in ("test_greets", "test_gone")]})
        (row,) = self.go(Container(self.ws))
        self.assertEqual((row["cmd"], row["blind"], row["exit"]),
                         (f"blind:{self.card}.setup-1", "gate", 2))
        self.assertEqual(incidents.listing(self.conn), [])

    def test_a_landed_test_with_no_row_in_the_run_is_inconclusive_and_held(self):
        self.use_names(("test_greets", "test_fails", "test_gone"), "gone")
        commit = self.conn.execute("SELECT blind_commit FROM tasks WHERE id=?",
                                   (self.card,)).fetchone()[0]
        db.event(self.conn, self.card, "blind_landed", {
            "blind_commit": commit, "acceptance": [],
            "tests": [f"{self.card}.{n}" for n in ("test_greets", "test_fails", "test_gone")]})
        (row,) = self.go(Container(self.ws))
        self.assertEqual((row["cmd"], row["blind"], row["reason"]),
                         ("blind:inconclusive", "hold", "blind tests inconclusive"))
        self.assertIn("no row for 1 landed", row["output"])
        self.assertEqual([i["kind"] for i in incidents.listing(self.conn)], ["review_infra"])

    def test_a_passing_row_with_no_example_row_does_not_hide_a_dropped_landed_test(self):
        self.use_names(("test_greets", "test_gone"), "hide",
                       TESTS.replace('run("echo no").exit, 1', 'run("echo no").exit, 0'))
        commit = self.conn.execute("SELECT blind_commit FROM tasks WHERE id=?",
                                   (self.card,)).fetchone()[0]
        db.event(self.conn, self.card, "blind_landed", {
            "blind_commit": commit, "acceptance": [],
            "tests": [f"{self.card}.test_greets", f"{self.card}.test_gone"]})
        (row,) = self.go(Container(self.ws))
        self.assertEqual((row["cmd"], row["blind"]), ("blind:inconclusive", "hold"))
        self.assertIn("no row for 1 landed", row["output"])

    def test_the_rows_of_every_landed_test_are_no_problem(self):
        commit = self.conn.execute("SELECT blind_commit FROM tasks WHERE id=?",
                                   (self.card,)).fetchone()[0]
        db.event(self.conn, self.card, "blind_landed", {
            "blind_commit": commit, "acceptance": [],
            "tests": [f"{self.card}.test_greets", f"{self.card}.test_fails"]})
        self.assertEqual({r["blind"] for r in self.go(Container(self.ws))}, {"gate"})

    def test_a_runner_holder_row_is_named_neutrally_not_by_the_test_file_or_class(self):
        self.use(BROKEN_SETUP, "setup")
        rows = self.go(Container(self.ws))
        ids = sorted(r["cmd"] for r in rows)
        self.assertEqual(ids, [f"blind:{self.card}.setup-1"])
        text = json.dumps(rows) + json.dumps(self.result())
        for name in ("setUpClass", "test_it", "Greeting"):
            self.assertNotIn(name, text)
        self.assertEqual(self.events()[0]["failing"], 1)      # a count, never an id (D17)

    def test_a_failing_run_does_not_escape_when_both_the_hold_check_and_the_record_break(self):
        self.eligible()
        with mock.patch.object(blind_run, "_plan", side_effect=RuntimeError("boom")), \
                mock.patch.object(blind_run, "_held_mode", side_effect=ValueError("db")), \
                mock.patch.object(blind_run, "_record", side_effect=OSError("disk")):
            (row,) = self.go(Container(self.ws))
        self.assertEqual((row["cmd"], row["blind"], row["reason"]),
                         ("blind:unavailable", "hold", "blind tests unavailable"))


class TestFallback(unittest.TestCase):
    def test_a_fail_only_on_blind_tests_adds_no_generic_line(self):
        blind = {"assertions": [{"cmd": "blind:t_a.x", "result": "fail"}]}
        other = {"assertions": [{"cmd": "make", "result": "pass"}]}
        self.assertEqual(review_findings.fallback("validator", blind), [])
        self.assertEqual(review_findings.fallback("validator", other), ["validator: verdict fail"])
        self.assertEqual(review_findings.fallback("reviewer", {}), ["reviewer: verdict fail"])
        self.assertEqual(review_findings.fallback("validator", other, True), [])


HOLD = [{"cmd": "blind:pending", "exit": 2, "output": "no blind tests yet", "blind": "hold",
         "reason": "no blind tests yet"}]


def row(cmd, exit_code, mode="gate"):
    return {"cmd": cmd, "exit": exit_code, "output": "", "blind": mode}


def doc(verdict, *assertions, not_run=()):
    return {"verdict": verdict, "not_run": list(not_run), "assertions": [
        {"cmd": c, "exit": 0, "result": r} for c, r in assertions]}


class TestValidatorRules(unittest.TestCase):
    OUT = [row("make", 0, "none"), row("blind:t_a.ok", 0), row("blind:t_a.bad", 1)]

    def check(self, document, outputs=None):
        return validator_report.not_run_violation(document, outputs or self.OUT)

    def test_a_fail_assertion_citing_the_failing_test_is_valid(self):
        self.assertIsNone(self.check(doc("fail", ("make", "pass"), ("blind:t_a.ok", "pass"),
                                         ("blind:t_a.bad", "fail"))))

    def test_a_pass_over_a_failed_blind_test_is_a_violation(self):
        got = self.check(doc("pass", ("make", "pass"), ("blind:t_a.ok", "pass"),
                             ("blind:t_a.bad", "pass")))
        self.assertIn("blind:t_a.bad", got)
        self.assertIn("did not fail", got)

    def test_a_pass_verdict_over_a_fail_assertion_is_a_violation(self):
        self.assertIn("verdict cannot be pass", self.check(doc(
            "pass", ("make", "pass"), ("blind:t_a.ok", "pass"), ("blind:t_a.bad", "fail"))))

    def test_a_left_out_row_for_a_test_that_ran_is_a_violation(self):
        got = self.check(doc("fail", ("make", "pass"), ("blind:t_a.bad", "fail")))
        self.assertIn("left out", got)
        self.assertIn("blind:t_a.ok", got)

    def test_a_not_run_row_for_a_test_that_ran_is_a_violation(self):
        got = self.check(doc("fail", ("make", "pass"), ("blind:t_a.ok", "not_run"),
                             ("blind:t_a.bad", "fail")))
        self.assertIn("not_run", got)

    def test_a_fail_citing_a_blind_row_that_passed_or_is_no_row_is_invalid_in_every_mode(self):
        for outputs in (self.OUT, [row("make", 0, "none")], [], None):
            for cmd in ("blind:t_a.ok", "blind:t_a.nothing"):
                with self.subTest(outputs=outputs, cmd=cmd):
                    got = validator_report.not_run_violation(
                        doc("fail", ("make", "pass"), (cmd, "fail")), outputs)
                    self.assertIn("did not fail", got or "")
                    self.assertIn(cmd, got)

    def test_a_fail_citing_a_report_row_that_failed_beside_a_real_failure_stays_valid(self):
        out = [row("make", 1, "none"), row("blind:x", 1, "report")]
        self.assertIsNone(validator_report.not_run_violation(
            doc("fail", ("make", "fail"), ("blind:x", "fail")), out))

    def test_without_gate_rows_nothing_changes(self):
        self.assertIsNone(self.check(doc("pass", ("make", "pass")), [row("make", 0, "none")]))
        self.assertIsNone(self.check(doc("pass", ("make", "pass")),
                                     [row("make", 0, "none"), row("blind:x", 1, "report")]))


class SpoolCase(GateCase):
    """Helpers for the tests of what the tick reads back from the validator run's job.json."""

    def spool(self, rows, raw=None):
        """The validator run `run1`, its job.json holding `rows` (or the text `raw`; `False`: no
        file at all)."""
        self.conn.execute("INSERT INTO task_runs (task_id, profile, run_id, started_at, status)"
                          " VALUES (?, 'validator', 'run1', 1, 'done')", (self.card,))
        d = self.root / "spool" / "run1"
        d.mkdir(parents=True)
        if raw is not False:
            (d / "job.json").write_text(
                json.dumps({"inputs": {"outputs": rows}}) if raw is None else raw)
        return self.conn.execute("SELECT * FROM task_runs WHERE run_id='run1'").fetchone()

    def held(self, rows=None, body=ACCEPT, raw=None):
        """A card in review whose validator run (`rows`, default: no tests yet) is about to be
        judged; its implementer run is done, so a person's `accept` can take it back."""
        self.eligible(body)
        self.conn.execute("UPDATE tasks SET status='review' WHERE id=?", (self.card,))
        self.conn.execute("INSERT INTO task_runs (task_id, profile, run_id, started_at, status,"
                          " handoff) VALUES (?, 'implementer', 'impl1', 1, 'done', ?)",
                          (self.card, json.dumps({"status": "done"})))
        self.fix = db.new_task(self.conn, title="fix card", assignee="dev", branch="epic/x")
        self.run = self.spool(HOLD if rows is None else rows, raw)

    def verdict(self, infra=(), mode="gate", now=5, cause=None):
        task = self.conn.execute("SELECT * FROM tasks WHERE id=?", (self.card,)).fetchone()
        cfg = {**self.cfg(), "redesign": {"blind_tests": mode}}
        return gate.hold(self.conn, cfg, self.root / "spool", task, self.run, "run1", now, infra,
                         cause)

    def say(self, text, kind="accept", author="owner"):
        """A human file for the card, applied at time 6."""
        self.said = getattr(self, "said", 0) + 1
        interventions.apply(self.conn, self.cfg(), self.card,
                            {"kind": kind, "text": text, "author": author},
                            f"{self.said}.json", "file", 6)

    def status(self):
        row = self.conn.execute("SELECT status, block_kind, block_recurrences FROM tasks WHERE"
                                " id=?", (self.card,)).fetchone()
        return tuple(row)

    def kinds(self, kind):
        return [json.loads(r[0]) for r in self.conn.execute(
            "SELECT payload FROM task_events WHERE kind=?", (kind,))]


class TestConsumers(SpoolCase):
    """`gate.failures`, `hold`, `inputs` read the validator run's own job.json."""

    def test_failures_brief_has_counts_and_ids_and_full_has_the_findings(self):
        run = self.spool(self.go(Container(self.ws)))
        brief = gate.failures(self.root / "spool", run)
        self.assertEqual(brief, [f"blind tests: 1 of 2 failed"])
        (full,) = gate.failures(self.root / "spool", run, full=True)
        self.assertTrue(full.startswith(f"blind:{self.card}.test_fails failed\ngiven: a user runs"))
        self.assertEqual(gate.failures(self.root / "spool", None), [])

    def test_inputs_are_counts_and_failing_ids_for_reviewer_and_guide_only(self):
        self.spool(self.go(Container(self.ws)))
        want = {"blind": {"counts": {"pass": 1, "fail": 1, "error": 0, "skipped": 0},
                          "failing": [f"{self.card}.test_fails"]}}
        for role in ("reviewer", "guide"):
            self.assertEqual(gate.inputs(self.conn, self.root / "spool", self.card, role), want)
        for role in ("validator", "implementer"):
            self.assertEqual(gate.inputs(self.conn, self.root / "spool", self.card, role), {})

    def test_no_hold_row_no_hold(self):
        self.run = self.spool(self.go(Container(self.ws)))
        self.assertFalse(self.verdict())


if __name__ == "__main__":
    unittest.main()
