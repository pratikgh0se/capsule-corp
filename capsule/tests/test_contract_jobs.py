"""The validator's contract commands run off the tick thread (capsule/contract_jobs.py): the tick
submits a job and carries on; the validator run starts on a later pass, once the job is done.
Split out of capsule/tests/test_tick.py (docs/ZENKAI.md Z7: that file sits at its size baseline).
The strong runner is a stand-in whose exec blocks on a threading.Event."""
import json
import subprocess
import tempfile
import os
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from capsule.blind import launch, red
from capsule.contract_jobs import _exec_all
from capsule.runners import docker
from capsule.tests.test_mount_audit import Fake, listing
from capsule.tests.test_tick import IMPL_DONE, FakeRunner, TickCase

WAIT = 10        # seconds any one wait may take before the test gives up


class BlockingRunner(FakeRunner):
    def __init__(self):
        super().__init__()
        self.gate, self.entered = threading.Event(), threading.Event()

    def exec(self, name, cmd, timeout):
        self.entered.set()
        if not self.gate.wait(WAIT):
            raise RuntimeError("the test never released the gate")
        return super().exec(name, cmd, timeout)


class ContractJobsCase(TickCase):
    def setUp(self):
        super().setUp()
        self.script["implementer"] = [[IMPL_DONE]]

    def pump(self, tick, until, passes=8):
        for _ in range(passes):
            tick.once()
            if until():
                return
        self.fail("the condition never held")

    def started(self):
        rows = self.conn.execute("SELECT payload FROM task_events WHERE task_id=?"
                                 " AND kind='contract_started' ORDER BY id", (self.tid,)).fetchall()
        return [json.loads(r["payload"])["run_id"] for r in rows]

    def blocked_tick(self, **kw):
        self.runner = BlockingRunner()
        tick = self.make_tick(**kw)
        self.tick = tick
        tick.contracts.grace = 0                      # a blocked job must not stall the pass
        return tick

    def finish(self, tick):
        self.runner.gate.set()
        for job in list(tick.contracts.jobs.values()):
            job.future.result(WAIT)

    def outputs(self, run):
        job = json.loads((self.spool_dir(run["run_id"]) / "job.json").read_text())
        return job["inputs"]["outputs"]


class SubmitThenCarryOn(ContractJobsCase):
    def test_the_tick_does_not_wait_for_the_contract_commands(self):
        tick = self.blocked_tick()
        self.pump(tick, lambda: self.started())
        self.assertTrue(self.runner.entered.wait(WAIT))
        self.assertEqual(self.runs("validator"), [])      # no validator run while the job runs
        tick.once()                                       # returns at once: exec is still blocked
        self.assertEqual(self.runs("validator"), [])
        self.assertEqual(len(self.started()), 1)          # one job, not one per pass
        self.assertEqual(self.runner.calls.count(("start", self.started()[0])), 1)
        self.finish(tick)
        tick.once()
        val = self.runs("validator")
        self.assertEqual([r["run_id"] for r in val], self.started())    # the container names match
        cmds = self.cfg["contract_commands"]
        self.assertEqual([o["cmd"] for o in self.outputs(val[0])][:len(cmds)], cmds)
        self.assertEqual(tick.contracts.jobs, {})                      # the job is popped once used

    def test_a_running_job_keeps_its_container_off_the_reapers_list(self):
        tick = self.blocked_tick()
        self.pump(tick, lambda: self.started())
        tick.once()
        reaps = [c for c in self.runner.calls if c[0] == "reap"]
        self.assertIn(self.started()[0], reaps[-1][1])
        self.finish(tick)

    def test_every_output_carries_whole_seconds(self):
        self.pump(self.tick, lambda: self.runs("validator"))
        outputs = self.outputs(self.runs("validator")[0])
        self.assertTrue(outputs)
        for out in outputs:
            self.assertIsInstance(out["seconds"], int)


class Failure(ContractJobsCase):
    def test_a_job_that_raises_is_a_tick_error_then_a_fresh_job(self):
        tick, real = self.tick, self.runner.start
        tick.contracts.grace = 0

        release = threading.Event()           # a job fails only when the test lets it

        def start(run_id, ws, **kw):          # only the contract container has no requests_dir
            if "requests_dir" not in kw:
                release.wait(WAIT)
                raise RuntimeError(f"no container for {run_id}")
            return real(run_id, ws, **kw)
        self.runner.start = start

        def failed_pass(at):                 # the job fails; this pass files it
            release.set()
            for job in list(tick.contracts.jobs.values()):
                self.assertIsInstance(job.future.exception(WAIT), RuntimeError)
            tick.once(now=at)
            release.clear()
        self.pump(tick, lambda: self.started())
        t0 = int(time.time())
        failed_pass(t0)                      # infra failure: filed, never a verdict input
        self.assertEqual(self.runs("validator"), [])
        errors = self.conn.execute("SELECT payload FROM task_events WHERE task_id=?"
                                   " AND kind='tick_error'", (self.tid,)).fetchall()
        self.assertIn("RuntimeError: no container for <run>", errors[0]["payload"])
        self.assertNotIn(self.started()[0], errors[0]["payload"])    # no run id: dedupe holds
        tick.once(now=t0 + 59)               # still backing off: no new job
        self.assertEqual(len(self.started()), 1)
        tick.once(now=t0 + 61)               # the failed job is gone: a fresh one after a minute
        self.assertEqual(len(self.started()), 2)
        failed_pass(t0 + 62)                 # the same message again: no second tick_error
        tick.once(now=t0 + 62 + 61)          # the backoff doubled to 120 s
        self.assertEqual(len(self.started()), 2)
        tick.once(now=t0 + 62 + 121)
        self.assertEqual(len(self.started()), 3)
        count = self.conn.execute("SELECT COUNT(*) FROM task_events WHERE task_id=?"
                                  " AND kind='tick_error'", (self.tid,)).fetchone()[0]
        self.assertEqual(count, 1)
        release.set()                        # let the last job's thread end

    def test_a_timed_out_command_is_exit_124_with_its_seconds(self):
        runner = FakeRunner()

        def late(name, cmd, timeout):
            raise subprocess.TimeoutExpired(cmd, timeout)
        runner.exec = late
        [(cmd, rc, out, secs)] = _exec_all(runner, "r" * 32, "/ws", "/gd", ["make test"], 5)
        self.assertEqual((cmd, rc, out, secs), ("make test", 124, "timed out after 5s", 0))
        self.assertEqual(runner.calls[-1][0], "stop")


class Restart(ContractJobsCase):
    def test_a_new_tick_resubmits_the_lost_job(self):
        first = self.blocked_tick()
        self.pump(first, lambda: self.started())
        second = self.make_tick()
        second.contracts.grace = 0
        self.assertEqual(second.contracts.jobs, {})           # the old tick's jobs are not its own
        self.pump(second, lambda: len(self.started()) == 2)
        old, new = self.started()
        self.assertNotEqual(old, new)
        self.assertEqual(self.runs("validator"), [])
        self.runner.gate.set()
        for job in [*first.contracts.jobs.values(), *second.contracts.jobs.values()]:
            job.future.result(WAIT)
        second.once()
        self.assertEqual([r["run_id"] for r in self.runs("validator")], [new])


class SuiteDocker(Fake):
    """A fake docker whose mount probe (the first exec) waits for `go`, and whose suite exec
    times out unless it was handed its full timeout (a real suite takes 500 s)."""

    def __init__(self, ws):
        super().__init__(listing(ws))
        self.go, self.probed, self.timeouts = threading.Event(), threading.Event(), []

    def __call__(self, argv, **kw):
        self.timeouts.append((argv[1], kw.get("timeout")))
        if argv[1] == "exec" and argv[-1].endswith("the-suite"):
            if kw["timeout"] < 500:
                raise subprocess.TimeoutExpired(argv, kw["timeout"])
        elif argv[1] == "exec":
            self.probed.set()
            self.go.wait(WAIT)
        return super().__call__(argv, **kw)


class SharedRunnerWindow(TickCase):
    """`red._bounded` swaps `_run` and `_sleep` on the tick's strong runner for a red check; a
    contract job running on a thread in that window must keep its own, full timeouts."""

    @mock.patch.dict(os.environ, {"OPS_WORKER_IMAGE": "stub-image:1"})
    def test_the_suite_keeps_its_timeout_while_a_red_check_bounds_the_runner(self):
        ws = str(self.home / "w")
        fake = SuiteDocker(ws)
        runner = docker.StrongRunner()
        runner._run, runner._sleep = fake, lambda seconds: None
        self.tick.strong = runner
        cfg = dict(self.cfg, contract_commands=["the-suite"])
        job = self.tick.contracts._submit(self.tick, self.conn, cfg, self.task(), ws, 1, "r" * 32)
        self.assertTrue(fake.probed.wait(WAIT))
        with red._bounded(runner, time.monotonic() + 300):      # the red check's window
            fake.go.set()
            [(cmd, rc, out, secs)] = job.future.result(WAIT)
            runner._run(["docker", "ps"], timeout=1200)         # red's own call is still bounded
        self.assertEqual((cmd, rc), ("the-suite", 0))           # not exit 124
        self.assertEqual([t for verb, t in fake.timeouts if verb == "exec"][-1], 1200)
        self.assertLessEqual(fake.timeouts[-1][1], 300)         # red keeps its budget
        self.assertIs(runner._run, fake)                        # and the window restored it


class FailedDockerText(unittest.TestCase):
    def test_a_failed_docker_start_is_filed_masked_and_its_command_goes_to_the_log(self):
        import io
        from types import SimpleNamespace
        from unittest import mock
        from capsule.contract_jobs import ContractJobs
        argv = ["docker", "run", "-v", "/srv/BLINDLEAK/x:/workspace", "img"]
        job = SimpleNamespace(run_id="0123456789abcdef")
        exc = subprocess.CalledProcessError(125, argv)
        with mock.patch("sys.stderr", new_callable=io.StringIO) as log:
            with self.assertRaises(RuntimeError) as got:
                ContractJobs(60, 100)._failed("t_1", job, exc, 0)
        self.assertIn("<masked>:/workspace", str(got.exception))
        self.assertNotIn("BLINDLEAK", str(got.exception))
        self.assertEqual(log.getvalue().count("/srv/BLINDLEAK/x:/workspace"), 1)


class TimeoutRecorded(ContractJobsCase):
    def test_a_timed_out_command_is_an_event_a_failed_one_is_not(self):
        from capsule.contract_jobs import ContractJobs
        raw = [("slow", 124, "timed out after 5s", 5), ("bad", 1, "FAILED (failures=1)", 2)]
        ContractJobs._timeouts(self.conn, {"id": self.tid}, raw, 1)
        rows = self.conn.execute("SELECT payload FROM task_events WHERE task_id=? AND"
                                 " kind='contract_timeout'", (self.tid,)).fetchall()
        self.assertEqual([json.loads(r["payload"]) for r in rows],
                         [{"cmd": "slow", "seconds": 5}])


class PoolResize(TickCase):
    """The pool is sized from the board on each submit: turning `blind_tests` on (or off) while
    the tick runs changes the size from the next pass, no restart."""

    def sizes(self, *modes):
        self.tick.strong = self.runner
        jobs, caps = self.tick.contracts, self.cfg["caps"]
        base = caps["max_running"] + caps.get("max_review", 3)
        got = []
        for i, mode in enumerate(modes):
            cfg = dict(self.cfg, contract_commands=[], redesign={"blind_tests": mode})
            job = jobs._submit(self.tick, self.conn, cfg, self.task(), str(self.home / "w"), 1,
                               f"{i}" * 32)
            job.future.result(WAIT)
            got.append(jobs.pool._max_workers)
        return base, caps["max_running"], got

    def test_turning_the_mode_on_grows_the_pool_without_a_restart(self):
        base, running, got = self.sizes("off", "report", "gate", "off")
        self.assertEqual(got, [base, base + running, base + running, base])

    def test_a_resize_leaves_the_running_job_to_finish(self):
        jobs, gate = self.tick.contracts, threading.Event()
        self.tick.strong = self.runner
        cfg = dict(self.cfg, contract_commands=[], redesign={"blind_tests": "off"})
        with mock.patch("capsule.contract_jobs._exec_all", lambda *a: gate.wait(WAIT) and ["held"]):
            held = jobs._submit(self.tick, self.conn, cfg, self.task(), "w", 1, "a" * 32)
            jobs._submit(self.tick, self.conn, dict(cfg, redesign={"blind_tests": "report"}),
                         self.task(), "w", 1, "b" * 32)
            gate.set()
            self.assertEqual(held.future.result(WAIT), ["held"])


FAKE_CLI = """#!/bin/sh
{body}
"""


class CappedExec(unittest.TestCase):
    """`StrongRunner.exec` with a `cap` reads the pipe as it comes and keeps only the head."""

    def runner(self, body, cap=1000):
        cli = Path(tempfile.mkdtemp()) / "fakecli"      # not named docker: a real run, not a door
        cli.write_text(FAKE_CLI.format(body=body))
        cli.chmod(0o755)
        runner = docker.StrongRunner(docker=str(cli))
        runner.cap = cap
        return runner

    def test_fifty_megabytes_keep_only_the_head_and_the_cut_is_marked(self):
        runner = self.runner("yes 0123456789abcdef | head -c 52428800\necho err >&2\nexit 3")
        sizes = []
        real = docker._drain

        def spy(pipe, cap, kept):
            real(pipe, cap, kept)
            sizes.append(sum(map(len, kept)))
        with mock.patch.object(docker, "_drain", spy):
            code, out = runner.exec("capsule-x", "ignored", 60)
        self.assertEqual(code, 3)
        self.assertEqual(sorted(sizes),      # the kept buffers, not RSS
                         [len("err\n"), 1001 + len(docker.CUT_MIDDLE)])
        text = launch._capped(out)
        self.assertEqual(text, out[:launch.OUTPUT_CAP])           # a small cap: not cut here
        runner.cap = launch.OUTPUT_CAP
        _, out = runner.exec("capsule-x", "ignored", 60)
        self.assertIn(launch.CUT, launch._capped(out))
        self.assertEqual(launch._capped(out).count(launch.CUT), 1)
        self.assertLessEqual(len(launch._capped(out)), launch.OUTPUT_CAP + len(launch.CUT))

    def test_the_tail_of_a_long_stream_is_kept_with_the_head(self):
        runner = self.runner("head -c 5000 /dev/zero | tr '\\0' h\necho TAIL-END", cap=1000)
        _, out = runner.exec("capsule-x", "ignored", 20)
        self.assertEqual(len(out), 1001 + len(docker.CUT_MIDDLE))
        self.assertTrue(out.startswith("hhh"))
        self.assertIn(docker.CUT_MIDDLE, out)
        self.assertTrue(out.endswith("TAIL-END\n"))

    def test_crlf_output_cut_in_the_middle_is_still_marked(self):
        runner = self.runner("i=0\nwhile [ $i -lt 20000 ]; do printf 'line %05d\\r\\n' $i;"
                             " i=$((i+1)); done", cap=launch.OUTPUT_CAP)
        _, out = runner.exec("capsule-x", "ignored", 60)
        self.assertEqual(launch._capped(out).count(launch.CUT), 1)
        self.assertTrue(out.endswith("line 19999\n"))

    def test_both_streams_over_the_cap_still_come_out_within_the_cap(self):
        runner = self.runner("yes out | head -c 200000\nyes err | head -c 200000 >&2\n"
                             "echo LAST >&2", cap=launch.OUTPUT_CAP)
        _, out = runner.exec("capsule-x", "ignored", 60)
        self.assertEqual(out.count(launch.CUT), 2)
        text = launch._capped(out)
        self.assertLessEqual(len(text), launch.OUTPUT_CAP + len(launch.CUT))
        self.assertEqual(text.count(launch.CUT), 1)
        self.assertTrue(text.startswith("out\nout\n"))
        self.assertTrue(text.endswith("LAST\n"))

    def test_a_short_output_comes_back_whole_with_stderr_after_stdout(self):
        runner = self.runner("echo out\necho err >&2", cap=launch.OUTPUT_CAP)
        self.assertEqual(runner.exec("capsule-x", "ignored", 20), (0, "out\nerr\n"))

    def test_output_of_exactly_the_cap_is_not_marked_as_cut(self):
        runner = self.runner("head -c 1000 /dev/zero | tr '\\0' x", cap=1000)
        _, out = runner.exec("capsule-x", "ignored", 20)
        self.assertEqual(len(out), 1000)

    def test_the_timeout_still_kills_the_command_and_raises(self):
        runner = self.runner("echo started\nexec sleep 30")
        t0 = time.monotonic()
        with self.assertRaises(subprocess.TimeoutExpired):
            runner.exec("capsule-x", "ignored", 1)
        self.assertLess(time.monotonic() - t0, 10)

    def test_a_runner_without_a_cap_still_goes_through_its_own_run(self):
        runner = docker.StrongRunner()
        runner._run = lambda argv, **kw: subprocess.CompletedProcess(argv, 5, "o", "e")
        self.assertEqual(runner.exec("capsule-x", "x", 5), (5, "oe"))

    def test_the_snapshot_commands_of_a_blind_run_opt_in(self):
        job = mock.Mock(strong=docker.StrongRunner(), deadline=time.monotonic() + 60)
        with mock.patch.object(launch.snapshot, "take", return_value=[]):
            launch._snapshot(job, "w", "base", {"interface_commands": [], "tester_may_read": []},
                             "out")
        self.assertEqual(job.strong.cap, launch.OUTPUT_CAP)
        self.assertIsNone(docker.StrongRunner().cap)

    def test_a_stream_a_leftover_process_holds_open_is_marked_cut_and_the_rc_kept(self):
        runner = self.runner("echo done\n(sleep 20 >&2 &)\nexit 4")      # a child keeps stderr open
        t0 = time.monotonic()
        with mock.patch.object(docker, "_READER_GRACE", 1):
            code, out = runner.exec("capsule-x", "ignored", 30)
        self.assertEqual(code, 4)
        self.assertEqual(out, "done\n" + docker.CUT_OPEN)
        self.assertLess(time.monotonic() - t0, 10)

    def test_bytes_written_before_exit_survive_a_child_holding_stdout_open(self):
        # a child keeps stdout open
        runner = self.runner("echo hello\n(sleep 20 2>/dev/null &)\nexit 0")
        with mock.patch.object(docker, "_READER_GRACE", 1):
            code, out = runner.exec("capsule-x", "ignored", 30)
        self.assertEqual((code, out), (0, "hello\n" + docker.CUT_OPEN))

    def test_the_text_is_decoded_like_text_mode_with_universal_newlines(self):
        runner = self.runner("printf 'a\\r\\nb\\rc\\n'", cap=1000)
        self.assertEqual(runner.exec("capsule-x", "ignored", 20), (0, "a\nb\nc\n"))
