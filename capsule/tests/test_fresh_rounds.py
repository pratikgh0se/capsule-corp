"""C09 (t_e1d8dff0, off-legacy plan): a fourth implementer round is already a fresh agent.

Nothing carries a session across review rounds: a rerun starts a new CLI session (no `--resume`;
that flag is only the same run's own `max_turns` checkpoint ask), and a bundle inlines a checkpoint
only for a checkpoint RESTART (`resumed_from`). These tests pin that through the real tick and
worker, so a future change that resumes a session or carries a checkpoint across rounds fails.
They reach production code: the stub adapter is the model, the tick, worker and bundles are real.
Failure parking (`failure_limit`, 3) is unchanged and pinned last.
"""
import json

from capsule import db, human, worker
from capsule.models.stub import StubAdapter
from capsule.tests.test_tick import CHECKPOINT, GUIDE_OK, IMPL_DONE, REV_FAIL, VAL_PASS, TickCase

ROUNDS = 4


class LimitsSeen(StubAdapter):
    def __init__(self, reports, seen):
        super().__init__(reports)
        self.seen = seen

    def run(self, role, bundle, workspace, limits):
        self.seen.append((role, limits))
        return super().run(role, bundle, workspace, limits)


class FreshAgentRounds(TickCase):
    def spawn(self, board_db, spool_dir, run_id, role):
        """TickCase.spawn, but the adapter records the limits the worker hands it."""
        self.seen = getattr(self, "seen", [])
        ro = db.connect_ro(board_db)
        run = ro.execute("SELECT * FROM task_runs WHERE run_id=?", (run_id,)).fetchone()
        ro.close()
        entries = self.script[role].pop(0) if self.script[role] else []
        worker.run_one(board_db, spool_dir, run_id, role,
                       adapter=LimitsSeen([self.fill(e, run) for e in entries], self.seen))

    def drive_four_rounds(self):
        """A checkpoint restart in round 1, then review-fail reruns up to round four. The failure
        limit is raised so the fourth review round is reachable (three failures park a card)."""
        self.cfg["caps"]["failure_limit"] = 9
        self.write_cfg()
        db.event(self.conn, self.tid, "guide_ruling",
                 {"choice": "document-wrong", "role": "validator", "reason": "RULING-TEXT"})
        human.write(self.home, "test", self.tid, "answer", "HUMAN-ANSWER")
        self.script["implementer"] = [[CHECKPOINT]] + [[IMPL_DONE]] * ROUNDS
        self.script["reviewer"] = [[REV_FAIL]] * ROUNDS
        self.script["validator"] = [[VAL_PASS]] * ROUNDS
        self.script["guide"] = [[GUIDE_OK]] * ROUNDS
        for _ in range(60):
            self.tick.once()
            if len(self.runs("implementer")) > ROUNDS:
                break
        self.assertEqual(len(self.runs("implementer")), ROUNDS + 1)

    def implementer_prompts(self):
        return [json.loads((self.spool_dir(r["run_id"]) / "bundle.json").read_text())["prompt"]
                for r in self.runs("implementer")]

    def test_no_review_round_rerun_resumes_a_session(self):
        self.drive_four_rounds()
        for r in self.runs("implementer"):
            job = json.loads((self.spool_dir(r["run_id"]) / "job.json").read_text())
            self.assertNotIn("resume", job["limits"], r["run_id"])
        implementer_calls = [lim for role, lim in self.seen if role == "implementer"]
        self.assertEqual(len(implementer_calls), ROUNDS + 1)
        for limits in implementer_calls:
            self.assertNotIn("resume", limits)

    def test_only_a_checkpoint_restart_inlines_a_checkpoint(self):
        self.drive_four_rounds()
        runs, prompts = self.runs("implementer"), self.implementer_prompts()
        marker = CHECKPOINT["checkpoint"]["next_steps"][0]
        self.assertTrue(runs[1]["resumed_from"])
        self.assertIn(marker, prompts[1])          # the restart carries it
        for run, prompt in zip(runs, prompts):
            if not run["resumed_from"]:
                self.assertNotIn(marker, prompt, run["run_id"])
                self.assertNotIn("half the screen", prompt, run["run_id"])

    def test_round_four_still_carries_the_cards_context_by_reference(self):
        self.drive_four_rounds()
        runs, prompts = self.runs("implementer"), self.implementer_prompts()
        fourth_run, fourth = runs[ROUNDS], prompts[ROUNDS]   # runs[0] is the checkpointed one
        self.assertIsNone(fourth_run["resumed_from"])
        self.assertIn("HUMAN-ANSWER", fourth)
        self.assertIn("RULING-TEXT", fourth)
        self.assertIn("## Review findings to fix", fourth)
        self.assertIn("## Prior runs on this card", fourth)
        job = json.loads((self.spool_dir(fourth_run["run_id"]) / "job.json").read_text())
        self.assertTrue(job["inputs"]["prior_runs"])

    def test_failure_count_three_still_parks(self):
        self.script["implementer"] = [[IMPL_DONE]] * 4
        self.script["reviewer"] = [[REV_FAIL]] * 4
        self.script["validator"] = [[VAL_PASS]] * 4
        self.script["guide"] = [[GUIDE_OK]] * 4
        for _ in range(40):
            self.tick.once()
            if self.task()["status"] == "triage":
                break
        self.assertEqual((self.task()["status"], self.task()["failure_count"]), ("triage", 3))
        self.assertEqual(len(self.runs("implementer")), 3)   # no fourth round was dispatched
