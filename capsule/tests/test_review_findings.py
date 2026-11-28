"""`capsule/review_findings.py` (row 362 gate): a reviewer `pass` with `findings` must be refused
before `intent.check` runs, retried once with the message, never routed to a guide ruling."""
import unittest

from capsule import review_findings, validator_report, worker
from capsule.tests.test_tick import IMPL_DONE, REV_PASS, TickCase

# Trimmed from the real reviewer document at
# <home>/.local/share/capsule/boards/my-app/spool/d9ce2b78f1064ed4aa6efa96e78ca398/report/1.json
# (t_de1466d6, run d9ce2b78f1064ed4aa6efa96e78ca398): `verdict: pass` with two genuine, non-settled-
# sounding findings still in `findings` - the exact shape that tripped the contradiction.
REV_PASS_WITH_FINDING = dict(REV_PASS, findings=[
    {"file": "static/complete.html", "line": 35, "class": "design_fidelity",
     "text": "A second per-page <style> block duplicates one already in static/timer.html; the"
             " design doc says the override lives in one file. Worth deciding before the next"
             " screen copies it a third time."},
], not_reviewed=[])

# The same round's corrected reviewer document: the finding moved into `notes` with its citation,
# `findings` empty, verdict pass - what row 362 (plus this brief) asks the reviewer to answer next.
REV_PASS_WITH_NOTE = dict(REV_PASS, findings=[], notes=[
    "static/complete.html:35 duplicate <style> block already flagged, informational (row 362)"],
    not_reviewed=[])


class GateModeKeepsPlainFails(unittest.TestCase):
    """Gate mode drops only the blind rows; a fail on a cmd `withhold` renamed stays a failure."""
    ROWS = [{"cmd": "make test", "exit": 0}, {"cmd": "blind:t1.test_x", "blind": "gate", "exit": 1}]

    def doc(self, cmd):
        return {"verdict": "fail", "assertions": [
            {"cmd": cmd, "exit": 1, "result": "fail", "excerpt": "no"},
            {"cmd": "blind:t1.test_x", "exit": 1, "result": "fail", "excerpt": "given: SECRET"}]}

    def test_a_fail_on_a_non_row_cmd_is_still_a_finding_in_gate_mode(self):
        for cmd in ("make  test", "an acceptance item"):
            doc = validator_report.withhold(self.doc(cmd), self.ROWS)
            lines = review_findings.validator_lines(doc, False, True)
            self.assertTrue(lines, cmd)
            self.assertNotIn("SECRET", " ".join(lines))
            self.assertEqual(review_findings.fallback("validator", doc),
                             ["validator: verdict fail"])

    def test_the_blind_row_alone_is_no_finding_in_gate_mode(self):
        doc = validator_report.withhold(self.doc("make test"), self.ROWS)
        doc["assertions"][0]["result"] = "pass"
        self.assertEqual(review_findings.validator_lines(doc, False, True), [])
        self.assertEqual(review_findings.fallback("validator", doc), [])

    def test_a_blind_looking_fail_in_a_job_with_no_blind_rows_is_still_a_failure(self):
        for cmd in ("`blind:x`", "Blind:x", " blind:x"):
            doc = {"verdict": "fail", "assertions": [{"cmd": cmd, "exit": 1, "result": "fail"}]}
            for outputs in ([], [{"cmd": "make test", "exit": 0}], None):
                held = validator_report.withhold(doc, outputs)
                self.assertTrue(review_findings.validator_lines(held, False, True), (cmd, outputs))
                self.assertEqual(review_findings.fallback("validator", held),
                                 ["validator: verdict fail"])


class ReviewerRoundInPrompt(unittest.TestCase):
    """inc_7879cecc: the prompt states the job input `round` that intent.check uses."""

    def test_the_reviewer_prompt_states_the_round_from_job_inputs(self):
        task = {"id": "t", "title": "t", "body": ""}

        def prompt(**extra):
            inputs = dict({"diff": "d"}, **extra)
            return worker._bundle(None, "reviewer", task, None, inputs)["prompt"]

        late = prompt(round=4)
        self.assertIn("## Review round", late)
        self.assertIn("round 4", late)
        for text in ("row 540", "notes", "safety: true"):
            self.assertIn(text, late)
        early = prompt(round=3)
        self.assertIn("round 3", early)
        self.assertNotIn("row 540", early)
        self.assertNotIn("## Review round", prompt())


class Violation(unittest.TestCase):
    """Unit test on the validator itself (Zenkai Z2's second oracle, beside
    `contracts/intent/reviewer.jsonl`'s own `pass-with-findings` pattern): real code, sharing no
    code path with `intent.check`."""

    def test_pass_with_findings_is_a_violation(self):
        self.assertTrue(review_findings.is_violation(REV_PASS_WITH_FINDING))

    def test_pass_with_empty_findings_is_clean(self):
        self.assertFalse(review_findings.is_violation(REV_PASS))

    def test_pass_with_only_out_of_set_is_clean(self):
        """`out_of_set` is a different field (`review_scope.judged_document` already moved these
        out of `findings` before this check runs); a pass carrying only `out_of_set` is not a
        violation."""
        doc = dict(REV_PASS, out_of_set=[{"file": "x", "line": 0, "class": "out_of_set", "text": "t"}])
        self.assertFalse(review_findings.is_violation(doc))

    def test_fail_with_findings_is_clean(self):
        doc = dict(REV_PASS, verdict="fail", findings=REV_PASS_WITH_FINDING["findings"])
        self.assertFalse(review_findings.is_violation(doc))

    def test_pass_with_settled_note_and_no_findings_is_clean(self):
        self.assertFalse(review_findings.is_violation(REV_PASS_WITH_NOTE))


class ReviewerPassWithFindingsTickPath(TickCase):
    """Tick-path test replaying run d9ce2b78's own reviewer document shape (`REV_PASS_WITH_FINDING`)
    through the real tick: it must be refused before `intent.check`, retried once with the row 362
    message, and never open a contradiction or a guide_ruling card."""

    def test_pass_with_findings_retries_once_never_a_contradiction(self):
        self.script["implementer"] = [[IMPL_DONE]]
        self.script["reviewer"] = [[REV_PASS_WITH_FINDING], [REV_PASS_WITH_NOTE]]
        seen = []

        def hook(job, sp, run):
            seen.append(job["inputs"].get("invalid_document_error"))
        self.hooks["reviewer"] = hook
        for _ in range(8):
            self.tick.once()
            if self.task()["status"] in ("done", "blocked", "triage"):
                break
        row = self.task()
        self.assertEqual(row["status"], "done")
        self.assertIsNone(row["block_kind"])
        reviewer_runs = self.runs("reviewer")
        self.assertEqual(len(reviewer_runs), 2)
        self.assertEqual(reviewer_runs[0]["outcome"], "invalid_document")
        self.assertEqual(reviewer_runs[0]["error_class"], "invalid_output")
        self.assertEqual(reviewer_runs[1]["outcome"], "completed")
        # oracle 1: the run's own outcome/error_class (above)
        # oracle 2: the event trail - one invalid_document, one review_retry, zero contradictions
        kinds = [r[0] for r in self.conn.execute("SELECT kind FROM task_events WHERE task_id=?", (self.tid,))]
        self.assertEqual(kinds.count("invalid_document"), 1)
        self.assertEqual(kinds.count("review_retry"), 1)
        self.assertNotIn("contradiction", kinds)
        # the retried bundle carries the exact message, naming `notes`
        self.assertIsNone(seen[0])
        self.assertIsNotNone(seen[1])
        self.assertIn("notes", seen[1])
        self.assertIn("row 362", seen[1])
        # never a guide_ruling child card (t_cdb66622/t_16f58e50/t_593dd9df's own shape)
        children = self.conn.execute(
            "SELECT id FROM tasks t JOIN task_links l ON l.child_id = t.id WHERE l.parent_id=?"
            " AND t.request_needs='role:guide'", (self.tid,)).fetchall()
        self.assertEqual(children, [])

    def test_two_pass_with_findings_in_a_row_gives_review_infra_never_a_ruling_card(self):
        """A reviewer that never fixes it - two misses in a row - still reaches the ordinary
        review_infra park (the ordinary invalid-document double-miss shape), never a guide_ruling
        loop."""
        self.script["implementer"] = [[IMPL_DONE]]
        self.script["reviewer"] = [[REV_PASS_WITH_FINDING], [REV_PASS_WITH_FINDING]]
        for _ in range(8):
            self.tick.once()
            if self.task()["status"] == "blocked":
                break
        row = self.task()
        self.assertEqual((row["status"], row["block_kind"], row["failure_count"]), ("blocked", "review_infra", 0))
        reviewer_runs = self.runs("reviewer")
        self.assertEqual(len(reviewer_runs), 2)
        self.assertEqual([r["outcome"] for r in reviewer_runs], ["invalid_document", "invalid_document"])
        kinds = [r[0] for r in self.conn.execute("SELECT kind FROM task_events WHERE task_id=?", (self.tid,))]
        self.assertNotIn("contradiction", kinds)
        children = self.conn.execute(
            "SELECT id FROM tasks t JOIN task_links l ON l.child_id = t.id WHERE l.parent_id=?"
            " AND t.request_needs='role:guide'", (self.tid,)).fetchall()
        self.assertEqual(children, [])


if __name__ == "__main__":
    unittest.main()
