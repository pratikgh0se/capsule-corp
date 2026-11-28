"""inc_6a4dcc74: an answer that releases a Korin fix card lists the main commits on its files."""
import json
import subprocess
from unittest import mock

from capsule import bundles, fix_card_drift, human, incidents
from capsule.tests import test_fix_card_drift as base

H = fix_card_drift.HEADING
REAL_REPO = fix_card_drift.paths.REPO   # MainMoved points paths.REPO at a scratch repo


class AnswerRelease(base.MainMoved):
    def answer(self, tid, text="Keep the plumbing; build only the rest."):
        human.write(self.home, "test", tid, "answer", text, now=1790000000)
        self.runner.up = False
        self.tick.once()

    def outcome(self, tid):
        q = "SELECT payload FROM task_events WHERE task_id=? AND kind='intervention'"
        return json.loads(self.conn.execute(q, (tid,)).fetchone()[0]).get("outcome")

    def test_an_answer_that_releases_a_korin_fix_card_lists_the_main_commits_on_its_files(self):
        tid = self.card()
        self.commit("guide_notes.py", "v1", "drop the hunk")
        short = subprocess.run(["git", "-C", str(self.repo), "rev-parse", "--short", "HEAD"],
                               capture_output=True, text=True).stdout.strip()
        self.answer(tid)
        body = self.body(tid)
        self.assertTrue(body.startswith(base.BODY))
        self.assertIn(H, body)
        self.assertIn(f"{short} drop the hunk", body)
        self.assertEqual(self.outcome(tid), "applied")
        events = self.amended(tid)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["issuer"], "harness")

    def test_an_answer_on_a_ready_card_stays_noted_and_the_body_is_unchanged(self):
        tid = self.card()
        self.conn.execute("UPDATE tasks SET status='ready', block_kind=NULL WHERE id=?", (tid,))
        self.commit("guide_notes.py", "v1", "drop the hunk")
        self.answer(tid)
        self.assertEqual(self.outcome(tid), "noted")
        self.assertEqual(self.body(tid), base.BODY)
        self.assertEqual(self.amended(tid), [])

    def test_an_answer_on_a_spec_stale_hold_is_refused_and_the_body_is_unchanged(self):
        tid = self.card(kind="spec_stale")
        self.commit("guide_notes.py", "v1", "drop the hunk")
        self.answer(tid)
        self.assertTrue(self.outcome(tid).startswith("refused:"))
        self.assertEqual(self.status(tid), "blocked")
        self.assertEqual(self.body(tid), base.BODY)
        self.assertEqual(self.amended(tid), [])

    def test_a_git_failure_leaves_the_body_unchanged_and_the_answer_still_releases(self):
        tid = self.card()
        self.commit("guide_notes.py", "v1", "drop the hunk")
        with mock.patch.object(fix_card_drift, "_commits", return_value=None):
            self.answer(tid)
        self.assertEqual(self.body(tid), base.BODY)
        self.assertEqual(self.status(tid), "ready")
        self.assertEqual(self.outcome(tid), "applied")

    def test_the_rerun_prompt_holds_the_answer_and_the_drift_sentence(self):
        tid = self.card()
        self.commit("guide_notes.py", "v1", "drop the hunk")
        self.answer(tid, "Keep the plumbing.")
        task = dict(self.conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone())
        said = [{"kind": "answer", "text": "Keep the plumbing.", "at": 1790000000}]
        with mock.patch.object(fix_card_drift.paths, "REPO", REAL_REPO):
            prompt = bundles.implementer(task, interventions=said)["prompt"]
        sentence = "A human answer written at this release stays in force."
        self.assertIn("Keep the plumbing.", prompt)
        self.assertIn(sentence, prompt.split("## Human answers")[0])
        self.assertIn(H, prompt)

    def test_the_answer_still_opens_a_human_intervention_incident(self):
        self.assertNotIn("answer", incidents.ROUTINE_COS_KINDS)
        tid = self.card()
        self.answer(tid)
        q = "SELECT COUNT(*) FROM incidents WHERE kind='human_intervention'"
        self.assertGreaterEqual(self.conn.execute(q).fetchone()[0], 1)
