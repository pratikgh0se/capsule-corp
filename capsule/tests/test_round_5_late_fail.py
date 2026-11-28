# mutation-check: targets-predate-range 409fc415f -- recurrence test for inc_79a8436f; the fix
#   predates the range :: Round5LateFail.test_round_5_non_safety_fail_retries_once
"""inc_79a8436f: at round 5 a reviewer `fail` with no `safety: true` finding is refused once
(row 540) and opens no contradiction."""
import json
import subprocess

from capsule import review_findings
from capsule.tests.test_review_findings import REV_PASS_WITH_NOTE
from capsule.tests.test_tick import IMPL_DONE, REV_FAIL, TickCase


class Round5LateFail(TickCase):
    def test_round_5_non_safety_fail_retries_once(self):
        self.assertTrue(review_findings._is_late_fail(REV_FAIL, 5))
        tip = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.home / "origin", check=True,
                             capture_output=True, text=True).stdout.strip()
        for i in range(4):     # four earlier implementer rounds: the next reviewer is round 5
            self.conn.execute(
                "INSERT INTO task_runs (task_id, profile, status, outcome, handoff, metadata,"
                " started_at, ended_at)"
                " VALUES (?, 'implementer', 'done', 'completed', '{}', ?, ?, ?)",
                (self.tid, json.dumps({"start_sha": tip}), i + 1, i + 1))
        self.conn.commit()
        self.script["implementer"] = [[IMPL_DONE]]
        self.script["reviewer"] = [[REV_FAIL], [REV_PASS_WITH_NOTE]]
        rounds = []
        self.hooks["reviewer"] = lambda job, sp, run: rounds.append(job["inputs"].get("round"))
        for _ in range(10):
            self.tick.once()
            if self.task()["status"] in ("done", "blocked", "triage"):
                break
        self.assertEqual(rounds[0], 5)
        kinds = [r[0] for r in self.conn.execute(
            "SELECT kind FROM task_events WHERE task_id=?", (self.tid,))]
        self.assertEqual(kinds.count("invalid_document"), 1)
        self.assertNotIn("contradiction", kinds)
        self.assertEqual(self.runs("reviewer")[0]["outcome"], "invalid_document")
        self.assertEqual(self.task()["status"], "done")
