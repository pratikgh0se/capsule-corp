"""Row 540 gate (`capsule/review_findings.py` `refuse`, inc_c8367f0f): a late non-safety reviewer
`fail` is refused once with the row 540 text, never consuming a round."""
import json
import subprocess
import unittest

from capsule import blocks, review_findings
from capsule.tests.test_review_findings import REV_PASS_WITH_NOTE
from capsule.tests.test_tick import IMPL_DONE, REV_FAIL, REV_PASS, TickCase

SAFETY_FAIL = dict(REV_FAIL, findings=[
    {"file": "app.py", "line": 3, "class": "security", "text": "secret logged", "safety": True}])
SECURITY_CLASS_FAIL = dict(REV_FAIL, findings=[
    {"file": "app.py", "line": 3, "class": "security", "text": "secret logged"}])


class IsLateFail(unittest.TestCase):
    def test_a_security_class_finding_without_the_flag_is_not_late(self):
        for rnd in (4, 5, 6):
            self.assertFalse(review_findings._is_late_fail(SECURITY_CLASS_FAIL, rnd))

    def test_predicate(self):
        for rnd in (4, 5, 6):
            self.assertTrue(review_findings._is_late_fail(REV_FAIL, rnd))
            self.assertFalse(review_findings._is_late_fail(SAFETY_FAIL, rnd))
        self.assertFalse(review_findings._is_late_fail(REV_FAIL, 3))
        self.assertFalse(review_findings._is_late_fail(REV_PASS, 6))
        self.assertFalse(review_findings._is_late_fail(dict(REV_FAIL, findings=[]), 6))

    def test_a_mixed_document_with_one_safety_finding_is_not_late(self):
        mixed = dict(REV_FAIL, findings=REV_FAIL["findings"] + SAFETY_FAIL["findings"])
        self.assertFalse(review_findings._is_late_fail(mixed, 6))


class LateReviewFail(TickCase):
    def seed_rounds(self, n):
        """`n` earlier implementer rounds, so the next implementer run opens round n + 1."""
        tip = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.home / "origin", check=True,
                             capture_output=True, text=True).stdout.strip()
        for i in range(n):
            self.conn.execute(
                "INSERT INTO task_runs (task_id, profile, status, outcome, handoff, metadata,"
                " started_at, ended_at)"
                " VALUES (?, 'implementer', 'done', 'completed', '{}', ?, ?, ?)",
                (self.tid, json.dumps({"start_sha": tip}), i + 1, i + 1))
        self.conn.commit()

    def drive(self, reviewer, seed):
        self.seed_rounds(seed)
        self.script["implementer"] = [[IMPL_DONE]]
        self.script["reviewer"] = reviewer
        seen = []
        self.hooks["reviewer"] = lambda job, sp, run: seen.append(
            (job["inputs"].get("round"), job["inputs"].get("invalid_document_error")))
        for _ in range(10):
            self.tick.once()
            if self.task()["status"] in ("done", "blocked", "triage"):
                break
        return seen

    def kinds(self):
        rows = self.conn.execute("SELECT kind FROM task_events WHERE task_id=?", (self.tid,))
        return [r[0] for r in rows]

    def test_a_round_6_non_safety_fail_is_refused_once_and_trips_no_runaway(self):
        seen = self.drive([[REV_FAIL], [REV_PASS_WITH_NOTE]], 5)
        self.assertEqual(self.task()["status"], "done")
        runs = self.runs("reviewer")
        self.assertEqual(runs[0]["outcome"], "invalid_document")
        self.assertEqual(runs[0]["error_class"], "invalid_output")
        self.assertEqual(seen[0], (6, None))
        kinds = self.kinds()
        self.assertEqual(kinds.count("invalid_document"), 1)
        self.assertEqual(kinds.count("review_retry"), 1)
        self.assertNotIn("contradiction", kinds)
        self.assertNotIn("runaway", kinds)
        error = seen[1][1]
        for text in ("row 540", "notes", "safety: true", "decision.v1"):
            self.assertIn(text, error)

    def runaway_trigger(self):
        row = self.conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='runaway'",
            (self.tid,)).fetchone()
        return json.loads(row[0]).get("trigger") if row else None

    def test_a_second_round_6_fail_is_not_refused_and_the_runaway_fires(self):
        seen = self.drive([[REV_FAIL], [REV_FAIL]], 5)
        self.assertEqual(seen[0][0], 6)
        self.assertEqual(self.kinds().count("invalid_document"), 1)
        self.assertEqual(self.runaway_trigger(), "owner_rounds")
        self.assertNotEqual(self.task()["block_kind"], "review_infra")

    def test_a_retry_after_a_crash_is_not_refused_and_is_not_review_infra(self):
        seen = self.drive([[], [REV_FAIL]], 5)
        self.assertEqual(seen[0][0], 6)
        self.assertNotIn("invalid_document", self.kinds())
        self.assertEqual(self.runaway_trigger(), "owner_rounds")
        self.assertNotEqual(self.task()["block_kind"], "review_infra")

    def test_a_fail_after_an_accept_of_review_infra_is_refused_as_a_fresh_try(self):
        self.seed_rounds(5)
        self.script["implementer"] = [[IMPL_DONE]]
        self.script["reviewer"] = [[], [], [REV_FAIL], [REV_PASS_WITH_NOTE]]
        for _ in range(20):
            self.tick.once()
            if self.task()["block_kind"] == "review_infra":
                break
        self.assertEqual(self.task()["block_kind"], "review_infra")
        self.assertTrue(blocks.accept(self.conn, self.tid))
        for _ in range(10):
            self.tick.once()
            if self.task()["status"] in ("done", "blocked", "triage"):
                break
        self.assertEqual(self.kinds().count("invalid_document"), 1)
        self.assertNotIn("runaway", self.kinds())

    def test_a_second_round_4_fail_opens_a_contradiction(self):
        seen = self.drive([[REV_FAIL], [REV_FAIL]], 3)
        self.assertEqual(seen[0][0], 4)
        kinds = self.kinds()
        self.assertEqual(kinds.count("invalid_document"), 1)
        self.assertIn("contradiction", kinds)
        self.assertNotEqual(self.task()["block_kind"], "review_infra")

    def assert_safety_fail_not_refused(self, seed):
        seen = self.drive([[SAFETY_FAIL]], seed)
        self.assertEqual(seen[0][0], seed + 1)
        self.assertNotIn("invalid_document", self.kinds())

    def test_a_security_class_fail_is_not_refused_at_round_6(self):
        seen = self.drive([[SECURITY_CLASS_FAIL]], 5)
        self.assertEqual(seen[0][0], 6)
        self.assertNotIn("invalid_document", self.kinds())

    def test_a_safety_fail_is_not_refused_at_round_4(self):
        self.assert_safety_fail_not_refused(3)

    def test_a_safety_fail_is_not_refused_at_round_5(self):
        self.assert_safety_fail_not_refused(4)

    def test_a_safety_fail_is_not_refused_at_round_6(self):
        self.assert_safety_fail_not_refused(5)

    def test_a_round_3_fail_is_not_refused(self):
        seen = self.drive([[REV_FAIL]], 2)
        self.assertEqual(seen[0][0], 3)
        self.assertNotIn("invalid_document", self.kinds())


if __name__ == "__main__":
    unittest.main()
