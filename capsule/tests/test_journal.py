"""R05 step journal: one line per completed step, append-only, resumable (Z1 rows in
contracts/intent/step_journal.jsonl; Z2 oracles: the journal lines and the run's task_events)."""
import json
import time
import tempfile
import unittest
from pathlib import Path

from capsule import journal, spool, worker
from capsule.models.stub import StubAdapter
from capsule.tests.test_tick import IMPL_DONE, TickCase

STEPS = ["start", "model_start", "model", "report", "run"]


class Killed(Exception):
    pass


class KilledAfterStart(StubAdapter):
    def run(self, role, bundle, workspace, limits):
        raise Killed("process killed during the model step")


class JournalFiles(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())

    def test_append_only_existing_bytes_survive_and_symlink_is_refused(self):
        sp = spool.make(self.root, "r1")
        for name in STEPS[:3]:
            journal.append(sp, name)
        before = (sp / journal.NAME).read_bytes()
        journal.append(sp, STEPS[3])
        self.assertTrue((sp / journal.NAME).read_bytes().startswith(before))
        other = spool.make(self.root, "r2")
        (other / journal.NAME).symlink_to(sp / journal.NAME)
        with self.assertRaises(OSError):
            journal.append(other, "start")
        with self.assertRaises(OSError):
            journal.read(other)
        self.assertEqual([ln["seq"] for ln in journal.read(sp)], [1, 2, 3, 4])

    def test_torn_last_line_is_skipped_and_seq_stays_right(self):
        sp = spool.make(self.root, "r1")
        journal.append(sp, "start")
        journal.append(sp, "model")
        with (sp / journal.NAME).open("ab") as f:
            f.write(b'{"seq": 3, "ste')
        self.assertEqual([ln["seq"] for ln in journal.read(sp)], [1, 2])
        self.assertEqual(journal.append(sp, "report")["seq"], 3)
        self.assertEqual([ln["step"] for ln in journal.read(sp)], ["start", "model", "report"])

    def test_resume_at_next_is_k_plus_one(self):
        """The seq rule only (a resumed run's next line is k+1); resume handling is R12."""
        sp = spool.make(self.root, "r1")
        for k in (1, 2, 3):
            journal.append(sp, STEPS[k - 1])
            self.assertEqual(len(journal.read(sp)), k)
        self.assertEqual(journal.append(sp, STEPS[3])["seq"], 4)

    def test_lost_starts_clean(self):
        """The per-run-spool seq rule only (a new run's journal is empty); not lost handling."""
        lost = spool.make(self.root, "lost")
        journal.append(lost, "start")
        journal.append(lost, "model")
        fresh = spool.make(self.root, "fresh")
        self.assertEqual(journal.read(fresh), [])
        self.assertEqual(journal.append(fresh, "start")["seq"], 1)
        self.assertEqual(len(journal.read(lost)), 2)


class JournalThroughWorker(TickCase):
    def test_complete_run_journals_every_step_and_matches_task_events(self):
        self.script["implementer"] = [[IMPL_DONE]]
        for _ in range(3):
            self.tick.once()
            if self.runs("implementer") and self.runs("implementer")[0]["outcome"] == "completed":
                break
        impl = self.runs("implementer")[0]
        lines = journal.read(self.spool_dir(impl["run_id"]))
        self.assertEqual([ln["step"] for ln in lines], STEPS)
        self.assertEqual([ln["seq"] for ln in lines], [1, 2, 3, 4, 5])
        # second oracle: the run's own task_events say it asked for review
        kinds = [e["kind"] for e in self.conn.execute("SELECT kind, payload FROM task_events")
                 if json.loads(e["payload"] or "{}").get("run_id") == impl["run_id"]]
        self.assertIn("request_review", kinds)

    def test_kill_after_step_k_leaves_k_lines_and_resumes_at_k_plus_one(self):
        self.script["implementer"] = [[IMPL_DONE]]
        runs = []

        def killed(board_db, spool_dir, run_id, role):
            runs.append(spool_dir)
            with self.assertRaises(Killed):
                worker.run_one(board_db, spool_dir, run_id, role, adapter=KilledAfterStart([]))
        self.tick = self.make_tick(spawn=killed)
        self.tick.once()
        self.tick.once(now=int(time.time()) + 86400)    # the tick reaps the dead run
        self.assertTrue(runs)
        lines = journal.read(runs[0])
        self.assertEqual([ln["step"] for ln in lines], ["start", "model_start"])         # k = 2
        self.assertEqual(journal.append(runs[0], "model")["seq"], 3)       # resumed run: k + 1
        # second oracle: task_runs says this run did not complete, and no event for this run_id
        # asked for review (the journal has no "run" line)
        run = self.runs("implementer")[0]
        self.assertEqual(run["run_id"], runs[0].name)
        self.assertEqual(run["outcome"], "timed_out")
        sql = "SELECT payload FROM task_events WHERE kind='request_review'"
        asked = [e for e in self.conn.execute(sql)
                 if json.loads(e["payload"] or "{}").get("run_id") == run["run_id"]]
        self.assertEqual(asked, [])


if __name__ == "__main__":
    unittest.main()
