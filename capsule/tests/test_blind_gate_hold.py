# mutation-check: targets-predate-range b957739c9 -- fixture rename only, no behaviour pinned ::
#   TestHold.test_a_person_accept_waives_the_hold_under_the_override_rules,
#   TestHold.test_an_accept_with_no_reason_no_person_or_no_open_fix_card_waives_nothing
"""R32 (B4a) holds: `gate.hold` (plain reason, offer, a person's waiver under Z4's override rules,
the review-infra record, unreadable or malformed evidence), `gate.release` (the tests landed),
and `gate.inputs`. Same helpers and oracles as test_blind_gate.py: the rows in the
validator run's job.json, the card's status and its events."""
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from capsule import blocks, db, paths, review_findings, runaway, tick, validator_report
from capsule.blind import gate
from capsule.blind import run as blind_run
from capsule.tests.test_blind_gate import ACCEPT, SpoolCase
from capsule.tests.test_blind_run import BOARD, Container

SKIPPED = [{"cmd": "blind:skipped", "exit": 2, "output": "", "blind": "hold",
            "reason": "a blind test was skipped"}]
HOURS_72 = 72 * 3600


class TestFindings(unittest.TestCase):
    FACTS = {"id": "t_a.test_x", "given": "g", "expect": "e", "observed": "o"}

    def test_a_finding_holds_the_four_facts_and_nothing_else(self):
        self.assertEqual(review_findings.blind_finding(self.FACTS),
                         "blind:t_a.test_x failed\ngiven: g\nexpect: e\nobserved: o")

    def test_counted_never_returns_a_blind_assertion(self):
        asserts = [{"cmd": "blind:t_a.x", "result": "fail"}, {"cmd": "make", "result": "fail"},
                   {"cmd": "make2", "result": "not_run"}, {"cmd": "ok", "result": "pass"}]
        self.assertEqual([a["cmd"] for a in review_findings.counted(asserts)], ["make", "make2"])
        self.assertEqual([a["cmd"] for a in review_findings.counted(asserts, True)], ["make"])


class TestRowWithNoExit(unittest.TestCase):
    """A gate row with no `exit` recorded is not shown to have passed (missing = failing)."""
    FACTS = {"id": "t_a.x", "given": "g", "expect": "e", "observed": "o"}

    def spool(self, row):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root)
        (root / "r1").mkdir()
        (root / "r1" / "job.json").write_text(json.dumps({"inputs": {"outputs": [row]}}))
        return root

    def test_failures_count_a_gate_row_with_no_exit_as_failing(self):
        root = self.spool({"cmd": "blind:t_a.x", "blind": "gate", "result": "fail",
                           "test": self.FACTS})
        self.assertEqual(gate.failures(root, {"run_id": "r1"}),
                         ["blind tests: 1 of 1 failed"])

    def test_a_row_with_no_exit_and_no_finding_holds_the_card(self):
        rows = [{"cmd": "blind:t_a.x", "blind": "gate", "result": "fail"}]
        self.assertEqual(gate._reason(rows), gate.UNAVAILABLE)
        self.assertEqual(gate.failures(self.spool(rows[0]), {"run_id": "r1"}), [])

    def test_a_null_or_non_int_exit_fails_the_row_and_holds_a_row_with_no_finding(self):
        for bad in (None, "0", "", 0.0, [], True):
            row = {"cmd": "blind:t_a.x", "blind": "gate", "result": "pass", "exit": bad}
            self.assertEqual(gate._reason([row]), gate.UNAVAILABLE, repr(bad))
            root = self.spool(dict(row, test=self.FACTS))
            self.assertEqual(gate.failures(root, {"run_id": "r1"}),
                             ["blind tests: 1 of 1 failed"], repr(bad))
        self.assertIsNone(gate._reason([dict(row, exit=0)]))

    def test_a_pass_over_a_gate_row_with_a_null_exit_is_an_invalid_document(self):
        doc = {"verdict": "pass", "assertions": [
            {"cmd": "blind:t_a.x", "exit": 0, "result": "pass"}]}
        row = {"cmd": "blind:t_a.x", "blind": "gate", "exit": None}
        self.assertIn("did not fail", validator_report.not_run_violation(doc, [row]))

    def test_a_summary_line_with_trailing_whitespace_is_still_the_summary(self):
        for text in ("Ran 2 tests\n\nOK  \n", "Ran 2 tests\n\nOK\t\r\n"):
            self.assertEqual(blind_run._summary_line(text), "OK")


DOC = {"schema": "validator_result.v1", "verdict": "fail", "assertions": [
    {"cmd": "make", "exit": 1, "result": "fail", "excerpt": "make said no"},
    {"cmd": "blind:t_a.ok", "exit": 0, "result": "pass", "excerpt": "given: SECRET-PASS"},
    {"cmd": "blind:t_a.bad", "exit": 1, "result": "fail", "excerpt": "observed: SECRET-FAIL",
     "expected": "SECRET-EXPECT"}]}
DOC["assertions"][0]["expected"] = "make exits 0"


class TestGuideSeesNoBlindExcerpts(unittest.TestCase):
    def conn(self):
        """An empty board's db: the packet asks it whether the event is a red-exempt request."""
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        db.create_board(tmp / "board.db")
        conn = db.connect(tmp / "board.db")
        self.addCleanup(conn.close)
        return conn

    def test_scrubbed_drops_only_the_blind_excerpts_and_copies(self):
        got = gate.scrubbed(DOC)
        asserts = got["assertions"]
        self.assertEqual([a.get("excerpt") for a in asserts], ["make said no", None, None])
        self.assertEqual([a["cmd"] for a in asserts], [a["cmd"] for a in DOC["assertions"]])
        self.assertIn("SECRET-PASS", json.dumps(DOC))                 # the original is untouched
        for odd in (None, [], {"assertions": 3}, {}):
            self.assertEqual(gate.scrubbed(odd), odd)

    def test_a_contradiction_ruling_packet_has_the_document_without_them(self):
        stub = SimpleNamespace(_run=lambda conn, run_id: {"handoff": json.dumps(DOC)},
                               intent_dir=paths.REPO / "contracts" / "intent")
        ev = {"kind": "contradiction", "run_id": 1, "id": 3, "payload": json.dumps({
            "role": "validator", "examples": [], "precedence": []})}
        packet = tick.Tick._ruling_packet(stub, self.conn(), ev)
        self.assertNotIn("SECRET", json.dumps(packet))
        self.assertIn("make said no", json.dumps(packet))
        self.assertIn("blind:t_a.bad", json.dumps(packet))

    def test_a_blind_assertions_expected_text_is_dropped_too_and_other_rows_keep_theirs(self):
        stub = SimpleNamespace(_run=lambda conn, run_id: {"handoff": json.dumps(DOC)},
                               intent_dir=paths.REPO / "contracts" / "intent")
        ev = {"kind": "contradiction", "run_id": 1, "id": 3, "payload": json.dumps({
            "role": "validator", "examples": [], "precedence": []})}
        text = json.dumps(tick.Tick._ruling_packet(stub, self.conn(), ev))
        self.assertNotIn("SECRET-EXPECT", text)
        self.assertIn("make exits 0", text)

    def test_the_runaway_packet_lines_hold_a_blind_test_as_the_fixed_phrase_only(self):
        lines = runaway._lines(7, "validator", DOC)     # D161: the ids reach the guide via `inputs`
        self.assertEqual(len(lines), 2)
        self.assertNotIn("SECRET", " ".join(lines))
        self.assertIn("make said no", lines[0])
        self.assertIn(validator_report.BLIND_FINDING, lines[1])
        self.assertNotIn("t_a.bad", lines[1])


class TestSameSecondOrder(SpoolCase):
    """An implementer run and an accept (or a waiver) in the same whole second are ordered by the
    run's `claimed` event, never by the clock: a run claimed before the accept is not after it, so
    the release is honoured (one `override`), never dropped with no record."""

    def claimed(self, run_id, at):
        """An implementer run started at second `at`, with its `claimed` event now."""
        pk = self.conn.execute(
            "INSERT INTO task_runs (task_id, profile, run_id, started_at, status, handoff)"
            " VALUES (?, 'implementer', ?, ?, 'done', ?)",
            (self.card, run_id, at, json.dumps({"status": "done"}))).lastrowid
        db.event(self.conn, self.card, "claimed", {}, run_pk=pk, run_id=run_id, now=at)

    def hold(self):
        self.held()
        self.assertTrue(self.verdict())

    def accept(self):
        self.say(f"no tests will come, the fix card is {self.fix}")      # applied at second 6

    def test_a_run_claimed_before_the_accept_in_the_same_second_does_not_end_the_waiver(self):
        self.hold()
        self.claimed("same-second-before", 6)
        self.accept()
        self.assertFalse(self.verdict())
        self.assertEqual(len(self.kinds("override")), 1)

    def test_a_run_claimed_after_the_accept_in_the_same_second_ends_the_waiver(self):
        self.hold()
        self.accept()
        self.claimed("same-second-after", 6)
        self.assertTrue(self.verdict())

    def test_a_waiver_is_spent_only_by_a_run_claimed_after_its_override_event(self):
        self.held()
        self.claimed("before", 5)
        db.event(self.conn, self.card, "override", {"check": "c"}, now=5)
        self.assertFalse(gate._spent(self.conn, self.card, "c", 10 ** 9))
        self.claimed("after", 5)
        self.assertTrue(gate._spent(self.conn, self.card, "c", 10 ** 9))


class TestHold(SpoolCase):
    def why(self):
        return f"no tests will come, the fix card is {self.fix}"

    def test_hold_blocks_the_card_with_the_plain_reason_and_offers_accept_or_reset(self):
        self.eligible()
        self.conn.execute("UPDATE tasks SET blind_commit=NULL, status='review' WHERE id=?",
                          (self.card,))
        self.run = self.spool(self.go(Container(self.ws)))
        self.assertTrue(self.verdict())
        self.assertEqual(self.status()[:2], ("blocked", "needs_input"))
        (event,) = self.kinds("blocked")
        self.assertEqual(event["reason"], "no blind tests yet")
        self.assertTrue(event["cause"].startswith("blind_gate:"))
        said = " ".join(r["body"] for r in self.conn.execute("SELECT body FROM outbox"))
        self.assertIn("no blind tests yet", said)
        self.assertIn("accept|reset", said)
        self.assertNotIn("unblock", said)

    def test_a_person_accept_waives_the_hold_under_the_override_rules(self):
        self.held()
        self.assertTrue(self.verdict())
        self.say(self.why())
        self.assertEqual(self.status()[0], "review")
        self.assertFalse(self.verdict())
        self.assertFalse(self.verdict())            # honoured every time, recorded once
        (waiver,) = self.kinds("override")
        self.assertTrue(waiver["check"].startswith("blind_gate:"))
        self.assertEqual((waiver["issuer"], waiver["board"], waiver["fix_card"]),
                         ("owner", BOARD, self.fix))
        self.assertEqual(waiver["expires_at"], 6 + HOURS_72)       # the real 72 hours
        self.assertIn("no tests will come", waiver["reason"])
        self.assertEqual(self.status()[0], "review")

    def test_an_accept_with_no_reason_no_person_or_no_open_fix_card_waives_nothing(self):
        cases = (("", "owner"), ("WHY", "cos"), ("no tests, trust me", "owner"),
                 ("see t_00000000", "owner"))
        for text, author in cases:
            with self.subTest(text=text, author=author):
                self.setUp()
                self.held()
                self.assertTrue(self.verdict())
                self.say(self.why() if text == "WHY" else text, author=author)
                self.assertTrue(self.verdict())
                self.assertEqual(self.status()[:2], ("blocked", "needs_input"))
                self.assertEqual(self.kinds("override"), [])

    def test_an_accept_naming_no_fix_card_is_refused_once_with_a_plain_why(self):
        self.held()
        self.assertTrue(self.verdict())
        self.say("no tests will come")
        self.assertTrue(self.verdict())
        self.assertTrue(self.verdict())
        (refused,) = self.kinds("override_refused")
        self.assertIn("no open fix card", refused["why"])
        self.assertIn(f"python3 -m capsule human {self.cfg()['board']} {self.card} accept '",
                      refused["why"])
        self.assertIn("another open card of this board", refused["why"])

    def test_the_waiver_ends_after_72_hours(self):
        self.held()
        self.assertTrue(self.verdict())
        self.say(self.why())
        self.assertFalse(self.verdict(now=6 + HOURS_72 - 1))
        self.assertTrue(self.verdict(now=6 + HOURS_72))

    def test_the_waiver_ends_with_the_next_implementer_run(self):
        self.held()
        self.assertTrue(self.verdict())
        self.say(self.why())
        self.assertFalse(self.verdict())
        self.conn.execute("INSERT INTO task_runs (task_id, profile, run_id, started_at, status)"
                          " VALUES (?, 'implementer', 'impl2', 10, 'done')", (self.card,))
        self.assertTrue(self.verdict())

    def test_a_second_waiver_of_the_same_acceptance_text_needs_the_text_to_change(self):
        self.held()
        self.assertTrue(self.verdict())
        self.say(self.why())
        self.assertFalse(self.verdict())
        self.conn.execute("INSERT INTO task_runs (task_id, profile, run_id, started_at, status,"
                          " handoff) VALUES (?, 'implementer', 'impl2', 7, 'done', ?)",
                          (self.card, json.dumps({"status": "done"})))
        self.hold_again()
        self.say(self.why())
        self.assertTrue(self.verdict())
        self.assertEqual(len(self.kinds("override")), 1)
        (refused,) = self.kinds("override_refused")
        self.assertIn("already waived once", refused["why"])
        cmd = f"python3 -m capsule human {self.cfg()['board']} {self.card}"
        for way in ("the tests land", f"{cmd} amend '<reason>' --body-file", f"{cmd} reset",
                    f"{cmd} close"):
            self.assertIn(way, refused["why"])

    def hold_again(self):
        """The card back in review, a new validator run whose rows hold it for another reason."""
        self.conn.execute("UPDATE task_runs SET run_id='old' WHERE run_id='run1'")
        (self.root / "spool" / "run1").rename(self.root / "spool" / "old")
        self.conn.execute("UPDATE tasks SET status='review' WHERE id=?", (self.card,))
        self.run = self.spool(SKIPPED)
        self.assertTrue(self.verdict())

    def test_an_infra_failure_in_the_round_does_not_spend_the_waiver(self):
        self.held()
        self.assertTrue(self.verdict())
        self.say(self.why())
        self.assertTrue(self.verdict(infra=["reviewer run r1: no valid document"]))
        self.assertEqual(self.kinds("override"), [])
        self.say(self.why())
        self.assertFalse(self.verdict())
        self.assertEqual((len(self.kinds("override")), self.kinds("override_refused")), (1, []))

    def test_a_waiver_the_card_was_held_again_after_is_not_spent_until_a_run_follows(self):
        self.held()
        self.assertTrue(self.verdict())
        self.say(self.why())
        self.assertFalse(self.verdict())
        blocks.block(self.conn, self.card, kind="needs_input", reason="something else", now=8)
        self.say(self.why())
        self.assertFalse(self.verdict())
        self.assertEqual((len(self.kinds("override")), self.kinds("override_refused")), (2, []))

    def test_a_changed_acceptance_text_needs_a_new_accept(self):
        self.held()
        self.assertTrue(self.verdict())
        self.say(self.why())
        self.assertFalse(self.verdict())
        self.conn.execute("UPDATE tasks SET body=? WHERE id=?",
                          ("Acceptance:\n1. something else\n", self.card))
        self.assertTrue(self.verdict())
        self.say(self.why())
        self.assertFalse(self.verdict())
        self.assertEqual(len(self.kinds("override")), 2)

    def test_hold_also_records_a_review_infra_block_of_the_same_round_with_its_cause(self):
        self.held()
        lines = ["reviewer run r1: no valid document"]
        self.assertTrue(self.verdict(infra=lines, cause="invalid_document"))
        blocked = self.kinds("blocked")
        self.assertEqual((blocked[0]["kind"], blocked[0]["cause"]), ("review_infra",
                                                                     "invalid_document"))
        self.assertEqual(blocked[1]["kind"], "needs_input")
        self.assertIn("Also:", blocked[1]["reason"])
        self.assertIn("no valid document", blocked[1]["reason"])
        self.assertEqual(self.status()[:2], ("blocked", "needs_input"))

    def test_an_unreadable_job_json_holds_the_card_as_unavailable(self):
        for raw in (False, "{not json", "[1]", '{"inputs": {"outputs": 3}}'):
            with self.subTest(raw=raw):
                self.setUp()
                self.held(raw=raw)
                self.assertTrue(self.verdict())
                self.assertEqual(self.kinds("blocked")[0]["reason"], "blind tests unavailable")

    def test_a_job_json_without_outputs_is_no_hold_and_other_modes_never_hold(self):
        self.held(raw='{"inputs": {}}')
        self.assertFalse(self.verdict())
        self.assertEqual(gate.failures(self.root / "spool", self.run), [])
        self.setUp()
        self.held(raw=False)
        self.assertFalse(self.verdict(mode="report"))
        self.assertFalse(self.verdict(mode="off"))

    def test_a_malformed_failing_row_raises_nothing_and_holds_as_unavailable(self):
        rows = [{"cmd": "blind:x", "exit": 1, "blind": "gate", "test": {"id": "x"}},
                {"exit": 1, "blind": "gate", "result": "fail"}]
        self.held(rows)
        self.assertEqual(gate.failures(self.root / "spool", self.run), [])
        self.assertEqual(gate.failures(self.root / "spool", self.run, full=True), [])
        self.assertEqual(gate.inputs(self.conn, self.root / "spool", self.card, "reviewer"),
                         {"blind": {"counts": {"pass": 0, "fail": 1, "error": 0, "skipped": 0},
                                    "failing": ["x", ""]}})
        self.assertTrue(self.verdict())
        self.assertEqual(self.kinds("blocked")[0]["reason"], "blind tests unavailable")

    def test_reviewer_counts_are_this_rounds_never_an_earlier_rounds(self):
        rows = self.go(Container(self.ws))
        self.spool(rows)                             # round 1's validator run
        self.conn.execute("INSERT INTO task_runs (task_id, profile, run_id, started_at, status)"
                          " VALUES (?, 'implementer', 'impl9', 1, 'done')", (self.card,))
        self.assertEqual(gate.inputs(self.conn, self.root / "spool", self.card, "reviewer"), {})
        self.conn.execute("INSERT INTO task_runs (task_id, profile, run_id, started_at, status)"
                          " VALUES (?, 'validator', 'run2', 2, 'done')", (self.card,))
        (self.root / "spool" / "run2").mkdir()
        (self.root / "spool" / "run2" / "job.json").write_text(
            json.dumps({"inputs": {"outputs": rows[:1]}}))
        counts = gate.inputs(self.conn, self.root / "spool", self.card, "guide")["blind"]["counts"]
        self.assertEqual(sum(counts.values()), 1)

    def test_release_takes_a_card_held_by_the_gate_back_to_review_with_no_round_used(self):
        self.held()
        self.assertTrue(self.verdict())
        runs = self.conn.execute("SELECT COUNT(*) FROM task_runs").fetchone()[0]
        self.assertTrue(gate.release(self.conn, self.card, 7))
        self.assertEqual(self.status(), ("review", "needs_input", 0))
        self.assertEqual(self.kinds("blind_hold_released"), [{"reason": "no blind tests yet"}])
        self.assertEqual([e["by"] for e in self.kinds("request_review")], ["accept"])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM task_runs").fetchone()[0], runs)
        self.assertFalse(gate.release(self.conn, self.card, 8))      # nothing left to release

    def test_release_leaves_a_card_that_moved_on_since_the_hold(self):
        for later in ("request_review", "failure_limit", "unblocked"):
            with self.subTest(later=later):
                self.setUp()
                self.held()
                self.assertTrue(self.verdict())
                db.event(self.conn, self.card, later, {})
                self.conn.execute("UPDATE tasks SET status='triage' WHERE id=?", (self.card,))
                self.assertFalse(gate.release(self.conn, self.card, 9))
                self.assertEqual(self.kinds("blind_hold_released"), [])

    def test_release_leaves_every_other_hold_alone(self):
        self.held(body=ACCEPT)
        blocks.block(self.conn, self.card, kind="needs_input", reason="a person's question",
                     now=5)
        self.assertFalse(gate.release(self.conn, self.card, 7))
        self.assertEqual(self.status()[:2], ("blocked", "needs_input"))
        self.assertEqual(self.kinds("blind_hold_released"), [])


if __name__ == "__main__":
    unittest.main()
