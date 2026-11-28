"""inc_35cbd720: the worker journals `model_start` before the model call, and an adapter timeout
leaves its evidence and a real error line."""
import functools
import json
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from capsule import db, journal, worker
from capsule.models import Result
from capsule.models import claude
from capsule.models.stub import StubAdapter
from capsule.tests.test_tick import IMPL_DONE, TOKENS, Recording, TickCase


def worker_timeouts(case):
    """The job.json limits.timeout of an implementer run and a review run, by role."""
    case.live = ("validator",)
    case.cfg["caps"]["review_seconds"] = 1800
    case.write_cfg()
    case.set_task_cap(3600)
    case.script["implementer"] = [[IMPL_DONE]]
    case.tick.once(case.now)
    case.tick.once(case.now + 1)                 # review stage starts; the validator hangs
    jobs = {r: json.loads((case.spool_dir(case.runs(r)[0]["run_id"]) / "job.json").read_text())
            for r in ("implementer", "validator")}
    return {r: job["limits"]["timeout"] for r, job in jobs.items()}


class WorkerModelStep(TickCase):
    def test_the_journal_orders_start_model_start_model(self):
        self.script["implementer"] = [[]]
        self.tick.once()
        sp = self.spool_dir(self.runs("implementer")[0]["run_id"])
        steps = [ln["step"] for ln in journal.read(sp)]
        self.assertEqual(steps[:3], ["start", "model_start", "model"])

    def test_a_timed_out_model_call_closes_as_fatal_with_its_evidence(self):
        now = int(time.time())
        self.script["implementer"] = [[claude.parse("", "timed out", 1)]]
        self.tick.once(now)
        self.runner.up = False
        self.tick.once(now + 5000)
        row = self.runs("implementer")[0]
        sp = self.spool_dir(row["run_id"])
        run = json.loads((sp / "run.json").read_text())
        self.assertEqual(run["error_class"], "fatal")
        self.assertIn("timed out", run["error"])
        self.assertTrue((sp / "cli_result.txt").exists())
        self.assertTrue((sp / "cli_stderr.txt").exists())
        self.assertIsNotNone(row["ended_at"])
        self.assertEqual(row["outcome"], "failed")
        sql = "SELECT COUNT(*) FROM task_events WHERE kind='timed_out'"
        self.assertEqual(self.conn.execute(sql).fetchone()[0], 0)

    def test_the_worker_timeout_keeps_a_floor_of_sixty_seconds(self):
        self.conn.execute("UPDATE tasks SET max_runtime_seconds=100 WHERE id=?", (self.tid,))
        self.conn.commit()
        self.tick.once(int(time.time()))
        run_id = self.runs("implementer")[0]["run_id"]
        job = json.loads((self.spool_dir(run_id) / "job.json").read_text())
        self.assertEqual(job["limits"]["timeout"], 60)

    def test_the_checkpoint_call_gets_the_timeout_left_after_the_first_call(self):
        ticks = iter([0, 1000])
        max_turns = Result(None, 1, TOKENS, 1, "max_turns", None, "sess-1", "")
        self.script["implementer"] = [[max_turns]]
        calls = []
        recording = functools.partial(Recording, calls=calls)
        clock = SimpleNamespace(monotonic=lambda: next(ticks))
        with mock.patch("capsule.tests.test_tick.StubAdapter", recording), \
                mock.patch.object(worker, "time", clock):
            self.tick.once()
        self.assertEqual([limits["timeout"] for _, limits in calls], [3180, 2480])


class TimedOutLastStep(TickCase):
    def hang(self, journal_step):
        """Run a worker that journals `journal_step` and hangs; time it out."""

        def spawn(board_db, spool_dir, run_id, role):
            if journal_step:
                journal.append(spool_dir, journal_step)
            return SimpleNamespace(pid=51000)
        killed = []
        self.tick = self.make_tick(spawn=spawn, alive=lambda pid: pid not in killed)
        self.tick._kill = killed.append
        now = int(time.time())
        self.conn.execute("UPDATE tasks SET max_runtime_seconds=100 WHERE id=?", (self.tid,))
        self.conn.commit()
        self.tick.once(now)
        self.runner.up = False
        self.assertEqual(self.tick.once(now + 200)["timed_out"], 1)
        row = self.conn.execute("SELECT payload FROM task_events WHERE kind='timed_out'").fetchone()
        return json.loads(row[0])

    def test_the_timed_out_event_names_the_last_journal_step(self):
        self.assertEqual(self.hang("model_start")["last_step"], "model_start")

    def test_the_timed_out_event_has_a_null_last_step_without_a_journal(self):
        self.assertIsNone(self.hang(None)["last_step"])


if __name__ == "__main__":
    unittest.main()


class StubRunIds(TickCase):
    """The stub route's reports carry the run's own ids; an explicit adapter's reports
    stay as given."""

    def _spawn_and_read(self, explicit):
        seen = {}

        def spawn(board_db, sp, run_id, role):
            ro = db.connect_ro(board_db)
            run = ro.execute("SELECT * FROM task_runs WHERE run_id=?", (run_id,)).fetchone()
            ro.close()
            if explicit:
                worker.run_one(board_db, sp, run_id, role, adapter=StubAdapter([dict(IMPL_DONE)]))
            else:
                worker.run_one(board_db, sp, run_id, role)
            seen["run"] = run
            files = sorted((Path(sp) / "report").glob("*.json"))
            seen["reports"] = [json.loads(p.read_text()) for p in files]

        self.spawn = spawn
        self.tick = self.make_tick(spawn=spawn)
        self.tick.once()
        return seen

    def _route(self, *reports):
        impl = self.cfg["routing"]["implementer"]
        self.cfg["routing"]["implementer"] = dict(impl, provider="stub", adapter="stub",
                                                  reports=list(reports))
        self.write_cfg()
        self.tick = self.make_tick()

    def test_a_stub_route_report_gets_the_run_ids(self):
        self._route(dict(IMPL_DONE))
        seen = self._spawn_and_read(explicit=False)
        run = seen["run"]
        self.assertEqual(len(seen["reports"]), 1)
        report = seen["reports"][0]
        self.assertEqual((report["run_id"], report["task_id"], report["claim_lock"]),
                         (run["run_id"], run["task_id"], run["claim_lock"]))
        self.assertEqual(report["status"], "done")

    def test_an_entry_that_sets_its_own_run_id_keeps_it(self):
        self._route(dict(IMPL_DONE, run_id="mine"))
        seen = self._spawn_and_read(explicit=False)
        report = seen["reports"][0]
        self.assertEqual(report["run_id"], "mine")
        self.assertEqual(report["task_id"], seen["run"]["task_id"])

    def test_an_explicit_adapter_report_is_written_unchanged(self):
        self._route(dict(IMPL_DONE, run_id="ignored"))
        seen = self._spawn_and_read(explicit=True)
        self.assertEqual(seen["reports"], [IMPL_DONE])
