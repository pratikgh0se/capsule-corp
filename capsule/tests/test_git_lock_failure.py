"""Card t_892c388b: a failed/fatal run whose error names the read-only git dir is a typed
`implementer_note`, not a `record_failure` strike (Zenkai Z2: the event trail and the card's own
counters are the two oracles; the matcher is the real `blocks.is_git_lock_note`)."""
import json

from capsule import blocks, db, paths
from capsule.tests.test_tick import GIT_LOCK_NOTE_365, IMPL_DONE, TOKENS, Result, TickCase


class GitLockFailure(TickCase):
    def _kinds(self):
        return [r[0] for r in self.conn.execute(
            "SELECT kind FROM task_events WHERE task_id=? ORDER BY id", (self.tid,))]

    def _first_close(self, first):
        self.script["implementer"] = [[first]]
        self.tick.once()
        self.runner.up = False
        self.tick.once()

    def _assert_noted_not_struck(self):
        self.assertEqual(self.task()["failure_count"], 0)
        self.assertEqual(self.task()["status"], "ready")
        kinds = self._kinds()
        self.assertIn("implementer_note", kinds)
        self.assertNotIn("failed", kinds)
        self.assertNotIn("parked", kinds)
        note = json.loads(self.conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='implementer_note'",
            (self.tid,)).fetchone()[0])
        self.assertEqual(note["kind"], "git_dir_read_only_failed")

    def test_fatal_run_with_git_lock_error_is_a_note_not_a_failure(self):
        self._first_close(Result(None, 1, TOKENS, 1, "fatal", None, None, GIT_LOCK_NOTE_365))
        self._assert_noted_not_struck()

    def test_failed_report_with_git_lock_error_is_a_note_not_a_failure(self):
        self._first_close(dict(IMPL_DONE, status="failed", error=GIT_LOCK_NOTE_365))
        self._assert_noted_not_struck()

    def test_real_git_stderr_on_the_failure_path_is_still_a_note(self):
        """Opus F1: the raw stderr has no harmless-clause rule to apply; it was never a report."""
        stderr = ("fatal: Unable to create '/work/repo/.git/index.lock': File exists.\n\n"
                  "Another git process seems to be running in this repository. Please make sure "
                  "all processes are terminated then try again.\n"
                  "error: could not stage src/app.py")
        self.assertFalse(blocks.is_git_lock_note(stderr))   # as a report item it holds a gap
        self._first_close(dict(IMPL_DONE, status="failed", error=stderr))
        self._assert_noted_not_struck()

    def test_failed_report_with_other_error_still_counts(self):
        self._first_close(dict(IMPL_DONE, status="failed", error="tests would not pass"))
        self.assertEqual(self.task()["failure_count"], 1)
        self.assertNotIn("implementer_note", self._kinds())

    def test_repeated_git_lock_failures_fall_back_to_the_ordinary_strike(self):
        self.script["implementer"] = [
            [dict(IMPL_DONE, status="failed", error=GIT_LOCK_NOTE_365)] for _ in range(4)]
        for _ in range(12):
            self.tick.once()
            if self.task()["failure_count"]:
                break
        self.assertEqual(self.task()["failure_count"], 1)
        self.assertEqual(self._kinds().count("implementer_note"), blocks.GIT_LOCK_RETRY_LIMIT)

    def test_matcher_boundaries(self):
        self.assertTrue(blocks.is_git_lock_note(GIT_LOCK_NOTE_365))
        self.assertFalse(blocks.is_git_lock_note("legitimate read-only mount of .gitignore"))
        self.assertFalse(blocks.is_git_lock_note(""))

    def test_a_card_no_longer_running_gets_no_note_and_takes_the_normal_strike(self):
        self.conn.execute("UPDATE tasks SET status='blocked' WHERE id=?", (self.tid,))
        self.conn.commit()
        blocks.record_failure(self.conn, self.tid, reason="x", limit=3, git_lock=True)
        self.assertNotIn("implementer_note", self._kinds())
        self.assertEqual(self.task()["failure_count"], 1)

    def test_every_intent_example_matches_record_failure(self):
        """Second oracle: each contracts/intent/git_lock.jsonl row, run through the real
        matcher and `record_failure` on the case's temp DB."""
        rows = [json.loads(x) for x in (paths.INTENT / "git_lock.jsonl").read_text().splitlines()]
        self.assertEqual(len(rows), 3)
        for row in rows:
            doc, want = row["given"]["document"], row["expect"]
            self.conn.execute("UPDATE tasks SET status='running', failure_count=0 WHERE id=?",
                              (self.tid,))
            self.conn.execute("DELETE FROM task_events WHERE task_id=? AND kind='implementer_note'",
                              (self.tid,))
            for _ in range(doc["prior_git_lock_notes"]):
                db.event(self.conn, self.tid, "implementer_note",
                         {"kind": "git_dir_read_only_failed", "count": 1})
            self.conn.commit()
            blocks.record_failure(self.conn, self.tid, reason=doc["error"], limit=3,
                                  git_lock=blocks.mentions_git_lock(doc["error"]))
            added = self._kinds().count("implementer_note") - doc["prior_git_lock_notes"]
            got = {"notes_added": added, "failure_count": self.task()["failure_count"],
                   "status": self.task()["status"]}
            self.assertEqual(got, want, row["id"])
