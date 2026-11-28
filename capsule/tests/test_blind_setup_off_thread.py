"""The blind tester's setup off the tick thread (capsule/blind/launch.py): the epic clone, the
`interface_commands` and the sparse blind clone run on a pool thread, a `blind_dispatch.step` that
starts or polls them returns while they are still going, and `launch.GRACE` is 0 here (no waiting
for a fresh setup). Split from test_blind_off_thread.py for its size. The strong runner is
`BlindBoard`'s stand-in whose `exec` blocks on a gate the test opens."""
import json
import threading
import time
from unittest import mock

from capsule import paths, workspace
from capsule.blind import launch
from capsule.tests.test_blind_dispatch import BlindBoard
from capsule.tests.test_blind_off_thread import WAIT, Events, eventually


class SetupBoard(BlindBoard):
    """A running card due a blind tester whose `interface_commands` block on a gate the test opens;
    `launch.GRACE` is 0 (no waiting for a fresh setup), and the clock is the test's own."""

    def setUp(self):
        super().setUp()
        patch = mock.patch.object(launch, "GRACE", 0)
        patch.start()
        self.addCleanup(patch.stop)
        self.cfg["blind_tests"]["interface_commands"] = ["make docs"]
        self.write_cfg()
        self.gate, self.entered, self.timeouts, self.threads = (
            threading.Event(), threading.Event(), [], [])
        self.output = "ok"
        self.runner.exec = self.exec
        real_start = self.runner.start
        self.start_gate, self.start_entered, self.start_threads = (
            threading.Event(), threading.Event(), [])
        self.start_gate.set()                       # shut it to hold the tester's own container

        def start(run_id, ws, **kw):
            if kw.get("mounts"):                    # only the tester's container has mounts
                self.start_threads.append(threading.current_thread())
                self.start_entered.set()
                self.start_gate.wait(WAIT)
            return real_start(run_id, ws, **kw)
        self.runner.start = start
        self.addCleanup(self.start_gate.set)
        self.addCleanup(self.gate.set)
        self.card = self.card_with("running")
        self.events = Events()
        self.t._events_mod = self.events

    def exec(self, name, cmd, timeout):
        self.threads.append(threading.current_thread())
        self.timeouts.append(timeout)
        self.entered.set()
        if not self.gate.wait(WAIT):
            raise RuntimeError("the test never released the gate")
        return 0, self.output

    def events_of(self, kind):
        return [r[0] for r in self.conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind=?", (self.card, kind))]

    def settled(self):
        for job in list(self.t.blind_setups.values()):
            job.future.exception(WAIT)
        self.step()


class SetupOffThread(SetupBoard):
    def test_a_step_returns_while_the_setup_commands_are_running(self):
        self.step()
        self.assertTrue(self.entered.wait(WAIT))
        self.step()
        self.assertEqual(len(self.blind_runs()), 1)             # the run row holds the seat ...
        self.assertEqual(self.events_of("blind_tester_started"), [])    # ... not launched yet
        self.assertNotEqual(self.threads[0], threading.main_thread())
        self.assertEqual(len(self.t.blind_setups), 1)           # one setup, not one per step

    def test_the_tester_starts_on_the_step_after_the_setup_is_done(self):
        self.step()
        self.assertTrue(self.entered.wait(WAIT))
        self.gate.set()
        self.settled()
        self.assertEqual(len(self.events_of("blind_tester_started")), 1)
        self.assertEqual(self.t.blind_setups, {})
        [run] = self.blind_runs()
        self.assertIn("blind_clone", json.loads(run["metadata"]))
        self.assertTrue((self.t._spool_root(self.cfg) / run["run_id"] / "interface" / "commands"
                         / "0.txt").is_file())

    def test_the_testers_container_is_started_on_the_setup_thread(self):
        self.start_gate.clear()
        self.gate.set()
        self.step()
        self.assertTrue(self.start_entered.wait(WAIT))
        self.step()                                 # returns while docker start is still going
        self.assertEqual(self.events_of("blind_tester_started"), [])
        self.assertNotEqual(self.start_threads[0], threading.main_thread())
        self.start_gate.set()
        self.settled()
        self.assertEqual(len(self.events_of("blind_tester_started")), 1)
        self.assertEqual(len(self.start_threads), 1)        # one container, not a second on settle

    def test_the_heartbeat_is_written_every_pass_while_the_setup_goes(self):
        self.step()
        self.assertTrue(self.entered.wait(WAIT))
        before = self.events.beats
        self.t.once()
        self.t.once()
        self.assertEqual(self.events.beats, before + 2)
        self.assertEqual(self.events_of("blind_tester_started"), [])

    def test_the_setup_container_carries_the_runs_id_so_the_reaper_spares_it(self):
        self.step()
        self.assertTrue(self.entered.wait(WAIT))
        [run] = self.blind_runs()
        self.assertEqual(self.runner.started[0][0], run["run_id"])
        self.t.once()
        reaps = [c for c in self.runner.calls if c[0] == "reap"]
        self.assertIn(run["run_id"], reaps[-1][1])

    def test_a_run_that_ends_during_its_setup_stops_the_container(self):
        self.step()
        self.assertTrue(self.entered.wait(WAIT))
        self.conn.execute("UPDATE task_runs SET status='failed', ended_at=1 WHERE profile=?",
                          ("blind-tester",))
        self.conn.commit()
        old = self.blind_runs()[0]
        self.step()
        self.assertNotIn(old["id"], self.t.blind_setups)   # (a fresh run follows: _known)
        eventually(self, lambda: ("stop", "capsule-" + old["run_id"][:12]) in self.runner.calls)
        self.gate.set()
        self.assertEqual(self.events_of("blind_tester_started"), [])

    def test_what_a_cancelled_setup_made_is_removed_only_after_its_thread_has_stopped(self):
        real, entered, hold = launch._blind_clone, threading.Event(), threading.Event()

        def slow(*args, **kw):
            entered.set()
            hold.wait(WAIT)
            return real(*args, **kw)
        patch = mock.patch.object(launch, "_blind_clone", slow)
        patch.start()
        self.addCleanup(patch.stop)
        self.addCleanup(hold.set)
        self.step()
        job = list(self.t.blind_setups.values())[0]
        self.gate.set()
        self.assertTrue(entered.wait(WAIT))                 # the thread is making its clone
        self.conn.execute("UPDATE task_runs SET status='failed', ended_at=1 WHERE profile=?",
                          ("blind-tester",))
        self.conn.commit()
        self.step()
        spool = self.t._spool_root(self.cfg) / job.run_id
        work = paths.board_dir(self.cfg["board"], self.t.home) / "blind-work" / job.run_id
        self.assertTrue(spool.exists())                     # not removed under the thread
        removed = threading.Event()
        job.future.add_done_callback(lambda _: removed.set())   # runs after `poll`'s `_remove`
        hold.set()
        self.assertTrue(removed.wait(WAIT))
        self.assertEqual([spool.exists(), work.exists(), workspace.gitdir(work).exists()],
                         [False, False, False])

    def test_a_setup_a_restart_lost_is_failed_so_the_card_gets_a_fresh_run(self):
        self.step()
        self.assertTrue(self.entered.wait(WAIT))
        self.t.blind_setups.clear()                  # what a new tick process starts with
        self.step()
        self.assertEqual([r["outcome"] for r in self.blind_runs()][0], "prepare_failed")
        self.assertEqual(len(self.events_of("blind_tester_failed")), 1)
        self.gate.set()


class SetupThread(SetupBoard):
    def job(self):
        self.step()
        return list(self.t.blind_setups.values())[0]

    def test_a_cancel_does_not_wait_for_a_slow_docker(self):
        stopping, release, stop = threading.Event(), threading.Event(), self.runner.stop
        ended = threading.Event()
        self.addCleanup(release.set)

        def slow_stop(name):                            # slow once the run has ended
            if ended.is_set():
                stopping.set()
                release.wait(30)
            stop(name)
        self.runner.stop = slow_stop
        self.step()
        self.assertTrue(self.entered.wait(WAIT))
        self.conn.execute("UPDATE task_runs SET status='failed', ended_at=1 WHERE profile=?",
                          ("blind-tester",))
        self.conn.commit()
        ended.set()
        t0 = time.monotonic()
        self.step()
        self.assertLess(time.monotonic() - t0, WAIT - 2)
        self.assertTrue(stopping.wait(WAIT))
        self.gate.set()

    def test_a_setup_that_ended_before_its_thread_began_never_clones(self):
        clones = []
        with mock.patch.object(launch.workspace, "clone", lambda *a, **kw: clones.append(a)):
            job = launch._Setup(self.runner, 1, {"id": "t_1"}, "r" * 32, [], "h", (None, {}), None)
            job.cancel()
            with self.assertRaises(launch.blind_run._Cancelled):
                launch._work(job, self.t, self.cfg, None)
        self.assertEqual(clones, [])

    def test_the_total_starts_when_the_thread_does_not_when_the_run_was_submitted(self):
        now = [1000.0]
        seen = []
        job = launch._Setup(self.runner, 1, {"id": "t_1"}, "r" * 32, [], "h", (None, {}), None)
        now[0] += 5000                              # a long wait for a pool thread
        with mock.patch.object(launch, "time", mock.Mock(monotonic=lambda: now[0])), \
                mock.patch.object(launch, "_prepare", lambda j, *a: seen.append(j.deadline)
                                  or launch.Refusal("x")):
            launch._work(job, self.t, self.cfg, None)
        self.assertEqual(seen, [now[0] + launch.SETUP_TOTAL])


class SetupLimits(SetupBoard):
    def clocked(self, jump):
        """A clock that moves `jump` seconds while the epic clone is made."""
        self.now = [1000.0]
        real = launch.workspace.clone

        def slow(*args, **kw):
            self.now[0] += jump
            return real(*args, **kw)
        for patch in (mock.patch.object(launch, "time", mock.Mock(monotonic=lambda: self.now[0])),
                      mock.patch.object(launch.workspace, "clone", slow)):
            patch.start()
            self.addCleanup(patch.stop)

    def test_the_total_counts_from_before_the_clone_is_made(self):
        self.clocked(launch.SETUP_TOTAL - 5)
        self.gate.set()
        self.step()
        self.assertTrue(self.entered.wait(WAIT))
        self.settled()
        self.assertEqual(self.timeouts, [5])         # what the clone left, not a fresh 120 s

    def test_a_clone_that_used_the_whole_total_fails_the_run_before_any_command(self):
        self.clocked(launch.SETUP_TOTAL + 1)
        self.gate.set()
        self.step()
        self.settled()
        self.assertEqual(self.timeouts, [])
        run = self.blind_runs()[0]            # (a failed run is retried once: _known)
        self.assertEqual((run["status"], run["outcome"]), ("failed", "prepare_failed"))
        self.assertIn("total", run["error"])

    def test_one_commands_output_is_capped_and_the_cut_is_marked(self):
        self.output = "h" * launch.OUTPUT_CAP + "Traceback: boom"
        self.gate.set()
        self.step()
        self.settled()
        [run] = self.blind_runs()
        text = (self.t._spool_root(self.cfg) / run["run_id"] / "interface" / "commands"
                / "0.txt").read_text()
        self.assertEqual(len(text), launch.OUTPUT_CAP + len(launch.CUT))
        self.assertIn(launch.CUT, text)
        self.assertTrue(text.startswith("hhh"))
        self.assertTrue(text.endswith("Traceback: boom"))
        self.assertEqual(launch.OUTPUT_CAP, 64 * 1024)

    def test_a_short_output_is_kept_whole(self):
        self.gate.set()
        self.step()
        self.settled()
        [run] = self.blind_runs()
        self.assertEqual((self.t._spool_root(self.cfg) / run["run_id"] / "interface" / "commands"
                          / "0.txt").read_text(), "ok")


class SwitchedOff(SetupBoard):
    def test_with_the_mode_off_no_setup_exists(self):
        self.cfg["redesign"] = {"blind_tests": "off"}
        self.write_cfg()
        self.step()
        self.assertEqual(self.blind_runs(), [])
        self.assertNotIn("blind_setups", self.t.__dict__)


class PoolSize(SetupBoard):
    def fresh_run(self, max_review):
        """Fail the live tester run and start its fresh one under a changed `max_review`."""
        self.conn.execute("UPDATE task_runs SET status='failed', ended_at=1 WHERE profile=?",
                          ("blind-tester",))
        self.conn.commit()
        if max_review:
            self.cfg["caps"]["max_review"] = max_review
            self.write_cfg()
        self.step()
        self.step()

    def start_and_restart(self, max_review):
        self.step()
        self.assertTrue(self.entered.wait(WAIT))
        first, job = self.t.blind_setups.pool, list(self.t.blind_setups.values())[0]
        self.fresh_run(max_review)
        self.assertEqual(len(self.blind_runs()), 2)         # the second run started a setup
        return first, job

    def test_the_same_max_review_keeps_the_pool(self):
        first, _ = self.start_and_restart(0)
        self.assertIs(self.t.blind_setups.pool, first)

    def test_a_raised_max_review_grows_the_pool_and_a_setup_in_flight_still_ends(self):
        first, job = self.start_and_restart(self.cfg["caps"]["max_review"] + 1)
        self.assertEqual(self.t.blind_setups.pool._max_workers, first._max_workers + 1)
        self.gate.set()
        job.future.exception(WAIT)                          # the old pool's setup ended
        self.assertTrue(job.future.done())
