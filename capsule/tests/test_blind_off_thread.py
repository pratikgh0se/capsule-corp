"""The blind run, off the tick thread (capsule/contract_jobs.py + capsule/blind/run.py): it runs in
the card's contract job right after its contract commands, so a tick pass that starts or polls the
job returns while the blind container is still going, its rows reach the validator exactly as the
inline run's did, and a card that leaves review stops the container and keeps nothing.

A real tick with `redesign.blind_tests` = report. The strong runner is the stand-in of
test_blind_run_tick (the real harness runs on the host); its blind `exec` blocks on a gate the test
opens, and notes which thread ran it.

The blind tester's setup is off the tick thread too: test_blind_setup_off_thread.py."""
import subprocess
import tempfile
import threading
import time
from unittest import mock

from capsule import db, incidents
from capsule.tests.test_blind_run import TESTS, Container, RunCase, blind_repo, record
from capsule.tests.test_check_gate import CheckGateCase
from capsule.tests.test_tick import IMPL_DONE, VAL_PASS

WAIT = 10        # seconds any one wait may take before the test gives up


def eventually(test, cond):
    """Wait up to WAIT seconds for `cond` (a cancel stops its container on a thread of its own)."""
    for _ in range(WAIT * 20):
        if cond():
            return
        time.sleep(0.05)
    test.fail("the condition never held")


class Events:
    """maintenance/events.py's stand-in: counts the tick heartbeats."""

    def __init__(self):
        self.beats = 0

    def log(self, kind, *args, **kw):
        self.beats += kind == "tick_health"

    def ship(self):
        pass

    def state_root(self):
        return tempfile.gettempdir()


class OffThread(CheckGateCase):
    def setUp(self):
        super().setUp()
        readme = TESTS.replace("cat hello.txt", "cat README.md").replace('"hi\\n"', '"my-app\\n"')
        self.repo = blind_repo(self.home / "blind", self.tid, readme, board=self.cfg["board"])
        record(self.conn, self.tid, self.repo)
        self.gate, self.entered, self.threads = threading.Event(), threading.Event(), []
        self.boxes, self.blind_names, self.execs, self.limit = [], [], [], None
        start, exec_ = self.runner.start, self.runner.exec

        def begin(run_id, ws, **kw):
            name = start(run_id, ws, **kw)
            if "mounts" in kw:
                box = Container(ws)
                box.start(run_id, ws, **kw)
                self.boxes.append(box)
                self.blind_names.append(name)
            return name

        def run(name, cmd, timeout):
            self.execs.append(str(cmd))
            if not (self.boxes and "harness.unittest_runner" in cmd):
                return exec_(name, cmd, timeout)
            self.threads.append(threading.current_thread())
            self.entered.set()
            if self.limit:
                raise subprocess.TimeoutExpired(cmd, timeout)
            if not self.gate.wait(WAIT):
                raise RuntimeError("the test never released the gate")
            return self.boxes[-1].exec(name, cmd, timeout)
        self.runner.start, self.runner.exec = begin, run
        self.script["validator"] = [[VAL_PASS]]
        self.script["implementer"] = [[IMPL_DONE]]
        self.cfg["redesign"] = {"blind_tests": "report"}
        self.cfg["blind_tests"] = {"repo": str(self.repo), "adapter": "unittest"}
        self.write_cfg()
        self.events = Events()
        self.tick._events_mod = self.events
        self.tick.contracts.grace = 0                  # a blocked job must not stall the pass
        self.addCleanup(self.gate.set)

    def pump(self, until, passes=100):
        for _ in range(passes):
            self.tick.once()
            if until():
                return
            time.sleep(0.05)      # the neutral checkout makes the job thread slower than a pass
        self.fail("the condition never held")

    def into_blind(self):
        self.pump(lambda: self.entered.is_set())
        self.assertTrue(self.entered.wait(WAIT))

    def finish(self):
        self.gate.set()
        for job in list(self.tick.contracts.jobs.values()):
            job.future.result(WAIT)

    def blind_rows(self):
        return sorted((o["cmd"], o["exit"]) for o in self.outputs()
                      if o["cmd"].startswith("blind:"))


class OnTheJobThread(OffThread):
    def test_a_pass_returns_while_the_blind_run_is_still_going(self):
        self.into_blind()
        passes = self.tick.pass_num
        self.tick.once()                               # polls the job: returns, the gate is shut
        self.tick.once()
        self.assertEqual(self.tick.pass_num, passes + 2)
        self.assertEqual(self.runs("validator"), [])   # the job is not done: no validator yet
        self.assertEqual(len(self.boxes), 1)           # one blind container, not one per pass
        self.assertNotEqual(self.threads[0], threading.main_thread())
        self.finish()

    def test_the_blind_run_comes_after_every_contract_command(self):
        self.into_blind()
        for cmd in self.cfg["contract_commands"]:
            self.assertIn(cmd, self.execs[:-1])        # all of them ran before the blind exec
        self.assertEqual(len(self.boxes), 1)
        self.finish()

    def test_the_heartbeat_is_written_every_pass_while_the_blind_run_goes(self):
        self.into_blind()
        before = self.events.beats
        self.tick.once()
        self.tick.once()
        self.assertEqual(self.events.beats, before + 2)
        self.finish()

    def test_the_rows_reach_the_validator_as_they_did_inline(self):
        self.into_blind()
        self.finish()
        self.pump(lambda: self.task()["status"] == "done")
        self.assertEqual(self.blind_rows(), [(f"blind:{self.tid}.test_fails", 1),
                                             (f"blind:{self.tid}.test_greets", 0)])
        cmds = [o["cmd"] for o in self.outputs()]
        self.assertEqual(cmds[:len(self.cfg["contract_commands"])], self.cfg["contract_commands"])
        after = cmds[len(self.cfg["contract_commands"]):]
        self.assertTrue(all(c.startswith("blind:") for c in after))
        self.assertEqual(self.task()["failure_count"], 0)
        self.assertIn("mode: report only", self.validator_files()[1])
        self.assertFalse(any((self.tick._spool_root(self.cfg) / r["run_id"] / "blind").exists()
                             for r in self.runs("validator")))      # the snapshot is removed

    def test_a_blind_run_over_its_time_limit_is_a_timeout_row_and_its_container_is_stopped(self):
        self.limit = True
        self.pump(lambda: self.runs("validator"))
        [(cmd, code)] = [(c, e) for c, e in self.blind_rows()]
        self.assertEqual((cmd, code), ("blind:inconclusive", 2))
        text = [o for o in self.outputs() if o["cmd"] == cmd][0]["output"]
        self.assertIn("exit code 124", text)
        self.assertIn(("stop", self.blind_names[0]), self.runner.calls)


class Cancelled(OffThread):
    def reset(self):
        self.conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (self.tid,))
        self.conn.commit()

    def test_a_card_that_leaves_review_in_the_blind_run_stops_its_container(self):
        self.into_blind()
        self.runner.calls.clear()
        self.reset()
        self.tick.once()                                # drops the job
        self.assertEqual(self.tick.contracts.jobs, {})
        eventually(self, lambda: ("stop", self.blind_names[0]) in self.runner.calls)
        self.gate.set()                                 # (the gate is still shut: not the thread)

    def test_a_cancel_does_not_wait_for_a_slow_docker(self):
        stopping, release, stop = threading.Event(), threading.Event(), self.runner.stop
        self.addCleanup(release.set)

        def slow_stop(name):                            # slow once the blind container is up
            if self.boxes:
                stopping.set()
                release.wait(30)
            stop(name)
        self.runner.stop = slow_stop
        self.into_blind()
        self.reset()
        t0 = time.monotonic()
        self.tick.once()                                # drops the job: the stop is not awaited
        self.assertLess(time.monotonic() - t0, WAIT - 2)
        self.assertTrue(stopping.wait(WAIT))            # it did run, on another thread
        self.gate.set()

    def test_nothing_is_recorded_for_the_old_round(self):
        self.into_blind()
        self.reset()
        self.tick.once()
        self.gate.set()
        time.sleep(0.5)                                 # let the old thread end
        sp = self.tick.contracts.pool
        sp.shutdown(wait=True)
        root = self.tick._spool_root(self.cfg)
        self.assertEqual([p for p in root.glob("*/blind_result.json")], [])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM incidents").fetchone()[0], 0)

    def test_a_job_dropped_before_the_blind_run_never_starts_its_container(self):
        started = threading.Event()
        hold, exec_ = threading.Event(), self.runner.exec

        def slow(name, cmd, timeout):
            started.set()
            hold.wait(WAIT)
            return exec_(name, cmd, timeout)
        self.runner.exec = slow
        self.pump(lambda: started.is_set())
        self.reset()
        self.tick.once()
        hold.set()
        self.tick.contracts.pool.shutdown(wait=True)
        self.assertEqual(self.boxes, [])


class RedCheckedElsewhere(OffThread):
    """Spec A2.3: tests red-checked on another base give an inconclusive row and a Korin incident,
    opened when the job is done and its rows are used, never for a job that was dropped."""

    def setUp(self):
        super().setUp()
        db.event(self.conn, self.tid, "blind_landed", {"start_sha": "a" * 40}, now=1)
        self.conn.commit()

    def incidents(self):
        return incidents.listing(self.conn)

    def test_one_incident_opens_when_the_job_is_done_and_no_container_starts(self):
        self.pump(lambda: self.runs("validator"))
        self.assertEqual(len(self.incidents()), 1)
        self.assertEqual(self.blind_rows(), [("blind:inconclusive", 2)])
        self.assertEqual(self.boxes, [])

    def test_a_job_dropped_before_it_is_done_leaves_no_incident(self):
        started, hold, exec_ = threading.Event(), threading.Event(), self.runner.exec

        def slow(name, cmd, timeout):
            started.set()
            hold.wait(WAIT)
            return exec_(name, cmd, timeout)
        self.runner.exec = slow
        self.addCleanup(hold.set)
        self.pump(lambda: started.is_set())
        self.assertEqual(self.incidents(), [])           # nothing at submit
        self.conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (self.tid,))
        self.conn.commit()
        self.tick.once()
        hold.set()
        self.tick.contracts.pool.shutdown(wait=True)
        self.assertEqual(self.incidents(), [])


class PoolSize(OffThread):
    def test_a_blind_board_has_a_worker_for_each_blind_run_beside_the_suites(self):
        self.pump(lambda: self.tick.contracts.pool is not None)
        caps = self.cfg["caps"]
        want = caps["max_running"] * 2 + caps.get("max_review", 3)
        self.assertEqual(self.tick.contracts.pool._max_workers, want)
        self.finish()

    def test_with_the_mode_off_the_pool_is_what_it_was(self):
        self.cfg["redesign"] = {"blind_tests": "off"}
        self.write_cfg()
        self.pump(lambda: self.tick.contracts.pool is not None)
        caps = self.cfg["caps"]
        self.assertEqual(self.tick.contracts.pool._max_workers,
                         caps["max_running"] + caps.get("max_review", 3))


class LsTreeTimeout(RunCase):
    def test_every_git_call_of_the_plan_has_a_timeout(self):
        timeouts, real = [], subprocess.run

        def spy(*args, **kw):
            timeouts.append((args[0][-4:], kw.get("timeout")))
            return real(*args, **kw)
        with mock.patch.object(subprocess, "run", spy):
            self.go(Container(self.ws))
        self.assertTrue(any("ls-tree" in a for a, _ in timeouts))
        self.assertTrue(all(t for a, t in timeouts if "ls-tree" in a))

    def test_a_git_that_hangs_is_an_unavailable_row_not_a_hang(self):
        def hang(*args, **kw):
            raise subprocess.TimeoutExpired(args[0], kw.get("timeout"))
        with mock.patch.object(subprocess, "run", hang):
            rows = self.go(Container(self.ws))
        self.assertEqual([r["cmd"] for r in rows], ["blind:unavailable"])


class SwitchOff(OffThread):
    def test_with_the_mode_off_the_job_is_the_contract_commands_alone(self):
        self.cfg["redesign"] = {"blind_tests": "off"}
        self.write_cfg()
        self.pump(lambda: self.runs("validator"))
        self.assertEqual(self.boxes, [])
        self.assertEqual(self.blind_rows(), [])
        self.assertFalse(self.entered.is_set())
