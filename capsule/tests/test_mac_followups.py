"""Small hardening follow-ups of the Mac lane security review (rounds 4 and 5): one test per
behaviour fix."""
import contextlib
import errno
import fcntl
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from capsule import tick_guard


class LockErrors(unittest.TestCase):
    def test_contention_is_busy_and_other_flock_errors_raise(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "x.lock"
            first = tick_guard._try_acquire(path)
            self.assertIsNotNone(first)
            self.assertIsNone(tick_guard._try_acquire(path))     # held: busy, quietly
            first.close()
            with mock.patch.object(fcntl, "flock", side_effect=OSError(errno.EIO, "io")):
                with self.assertRaises(OSError):
                    tick_guard._try_acquire(path)


class BusyLaneStagesNothing(unittest.TestCase):
    def test_a_busy_mac_lane_writes_no_commit(self):
        from capsule.blind import red
        from capsule.runners import mac
        conn = mock.MagicMock()
        conn.execute.return_value.fetchone.return_value = None
        task, run = {"id": "t_1", "tier": "mac"}, {"run_id": "r" * 12, "metadata": "{}"}
        patches = [mock.patch.object(red, "_deferred", return_value=False),
                   mock.patch.object(red, "_exempt_statements", return_value=set()),
                   mock.patch.object(red.preflight, "expand", return_value="/x"),
                   mock.patch.object(red.preflight, "problems", return_value=[]),
                   mock.patch.object(red.preflight, "worker_mounts", return_value=[]),
                   mock.patch.object(red.config, "blind_tests", return_value={"repo": "/x"}),
                   mock.patch.object(red, "base_sha", return_value="abc"),
                   mock.patch.object(red, "_stage")]
        with tempfile.TemporaryDirectory() as home:
            held = mac.acquire(home)         # another mac job holds the lane
            tick = mock.Mock(home=home)
            with contextlib.ExitStack() as stack:
                mocks = [stack.enter_context(p) for p in patches]
                self.assertFalse(red._check(tick, conn, {"board": "b"}, run, task, {}, "h", 1))
            held.close()
        mocks[-1].assert_not_called()


class StartRunFailureFreesLane(unittest.TestCase):
    def test_a_failed_run_row_frees_the_lane_at_once(self):
        from capsule.blind import launch
        from capsule.runners import mac
        task = {"id": "t_1", "tier": "mac", "branch_name": "b"}
        route = {"provider": "p", "account": "a", "model": "m"}
        with tempfile.TemporaryDirectory() as home:
            tick = mock.Mock(home=home, host="h")
            with (mock.patch.object(launch.config, "blind_tests",
                                    return_value={"interface_commands": ["x"]}),
                  mock.patch.object(launch, "txn"),
                  mock.patch.object(launch.config, "run_meta", return_value={}),
                  mock.patch.object(launch.lease, "start_run", side_effect=RuntimeError("db"))):
                with self.assertRaises(RuntimeError):
                    launch._start(tick, mock.Mock(), {"caps": {"claim_ttl": 1}}, task, [], "c",
                                  route, 1)
            self.assertFalse(mac.busy(home))


class ResultCopyIgnoresLinkedParent(unittest.TestCase):
    def _run_blind(self, swap):
        from capsule.runners import host, mac
        with tempfile.TemporaryDirectory() as tmp:
            tmp = os.path.realpath(tmp)
            wt, other, out = (os.path.join(tmp, n) for n in ("w", "other", "dest"))
            for d in (wt, other, out, os.path.join(wt, host.SCRATCH)):
                os.mkdir(d)
            Path(other, "results.jsonl").write_text("not the suite's\n")

            def run(cmd, timeout):
                box = os.path.join(wt, host.SCRATCH, "out")
                if swap:
                    os.rmdir(box)
                    os.symlink(other, box)
                else:
                    Path(box, "results.jsonl").write_text("its own\n")
                return 0, "ok"
            run.wt, run.lane = wt, None

            @contextlib.contextmanager
            def session(*_args, **_kw):
                yield run
            with (mock.patch.object(mac, "_probe", return_value=(None, "/x")),
                  mock.patch.object(mac, "session", session)):
                mac.run_blind(None, tmp, tmp, out, "exec python3 /blind", 5, "results.jsonl")
            return [p.name for p in Path(out).iterdir()]

    def test_a_linked_out_dir_is_not_read_and_a_plain_one_is(self):
        self.assertEqual(self._run_blind(swap=True), [])
        self.assertEqual(self._run_blind(swap=False), ["results.jsonl"])


class DeveloperDirShape(unittest.TestCase):
    def test_only_an_xcode_app_developer_dir_gets_the_grants(self):
        from capsule.runners import seatbelt
        with tempfile.TemporaryDirectory() as tmp:
            tmp = os.path.realpath(tmp)
            good = os.path.join(tmp, "Xcode.app", "Contents", "Developer")
            os.makedirs(good)
            self.assertIn(f"(subpath \"{os.path.join(tmp, 'Xcode.app/Contents/Frameworks')}\")",
                          seatbelt.render(tmp, developer_dir=good))
            for bad in (tmp, os.path.join(tmp, "Developer")):
                os.makedirs(bad, exist_ok=True)
                with self.assertRaises(ValueError):
                    seatbelt.render(tmp, developer_dir=bad)


class ClaimTierRace(unittest.TestCase):
    def test_a_claim_on_a_stale_tier_is_refused_and_the_card_stays_ready(self):
        from capsule import db, lease
        from capsule.tests.test_lease import board
        conn = board(self)
        tid = db.new_task(conn, title="t", assignee="dev-frontend", now=1)
        conn.execute("UPDATE tasks SET tier='mac' WHERE id=?", (tid,))
        conn.commit()
        self.assertIsNone(lease.claim(conn, tid, ttl=900, role="dev-frontend", now=2, tier=None))
        self.assertEqual(conn.execute("SELECT status FROM tasks").fetchone()[0], "ready")
        self.assertIsNotNone(lease.claim(conn, tid, ttl=900, role="dev-frontend", now=3,
                                         tier="mac"))


class BusyLaneRaceKeepsTheJob(unittest.TestCase):
    def test_a_job_that_lost_the_lane_race_runs_again_without_a_second_start(self):
        from concurrent.futures import ThreadPoolExecutor
        from capsule import contract_jobs
        calls = []

        def exec_all(*args):
            calls.append(args)
            return None if len(calls) == 1 else []      # busy at first, then it ran
        jobs = contract_jobs.ContractJobs(5, 100)
        with ThreadPoolExecutor(1) as pool, mock.patch.object(contract_jobs, "_exec_all", exec_all):
            jobs.pool = pool
            task, impl = {"id": "t_1", "tier": "mac"}, {"id": 7}
            first = pool.submit(exec_all, "x")
            first.result()
            jobs.jobs[("t_1", 7)] = contract_jobs.Job("run", first, None, None, ("y",))
            with mock.patch.object(jobs, "_submit") as submit:
                self.assertIsNone(jobs.step(None, None, {}, task, impl, "ws", 1, "run"))
                submit.assert_not_called()                  # no second `contract_started`
            self.assertIn(("t_1", 7), jobs.jobs)
            self.assertEqual(jobs.jobs[("t_1", 7)].future.result(), [])   # the same job, again
            self.assertEqual(calls[-1], ("y",))


class ProbeAndRenderAgree(unittest.TestCase):
    def test_a_link_to_the_command_line_tools_is_no_xcode(self):
        from capsule.runners import mac
        with tempfile.TemporaryDirectory() as tmp:
            tmp = os.path.realpath(tmp)
            clt = os.path.join(tmp, "CommandLineTools")
            os.mkdir(clt)
            link = os.path.join(tmp, "Xcode.app")
            os.symlink(clt, link)
            app = os.path.join(tmp, "Real.app", "Contents", "Developer")
            os.makedirs(app)

            def answer(selected):
                def run(*argv):
                    return selected if argv[0] == "xcode-select" else "/usr/bin/swift"
                return run
            for bad in (link, clt):
                with mock.patch.object(mac, "_run", answer(bad)):
                    self.assertEqual(mac._probe(), (mac.NO_XCODE, None))
            with mock.patch.object(mac, "_run", answer(app)):
                self.assertEqual(mac._probe(), (None, app))


class LaneCheckOsError(unittest.TestCase):
    def test_a_lock_failure_holds_that_card_with_one_event_not_the_pass(self):
        from capsule import landing_seen
        with (mock.patch.object(landing_seen.mac, "card_is_mac", return_value=True),
              mock.patch.object(landing_seen.mac, "unavailable", return_value=None),
              mock.patch.object(landing_seen.mac, "busy", side_effect=OSError(errno.EIO, "io")),
              mock.patch.object(landing_seen.dispatch, "mac_held") as held,
              mock.patch.object(landing_seen.config, "redesign",
                                return_value={"mac_runner": True})):
            self.assertTrue(landing_seen._lane_hold(None, {}, "/h", "t_1", 5))
        self.assertEqual(held.call_count, 1)
        self.assertIn("lane check failed", held.call_args[0][2])


class KillBeforeResultCheck(unittest.TestCase):
    def test_children_are_killed_before_the_out_dir_is_checked(self):
        from capsule.runners import host, mac
        with tempfile.TemporaryDirectory() as tmp:
            tmp = os.path.realpath(tmp)
            wt, other, out = (os.path.join(tmp, n) for n in ("w", "other", "dest"))
            for d in (wt, other, out, os.path.join(wt, host.SCRATCH)):
                os.mkdir(d)
            Path(other, "results.jsonl").write_text("not the suite's\n")

            def run(cmd, timeout):
                return 0, "ok"
            run.wt, run.lane = wt, None

            def kill(path):       # a child that outlived the run swaps the dir as it dies
                box = os.path.join(wt, host.SCRATCH, "out")
                os.rmdir(box)
                os.symlink(other, box)

            @contextlib.contextmanager
            def session(*_args, **_kw):
                yield run
            with (mock.patch.object(mac, "_probe", return_value=(None, "/x")),
                  mock.patch.object(mac, "session", session),
                  mock.patch.object(host, "_kill_under", kill)):
                mac.run_blind(None, tmp, tmp, out, "exec python3 /blind", 5, "results.jsonl")
            self.assertEqual(os.listdir(out), [])


class SetupFailureAbandonsRun(unittest.TestCase):
    def test_a_failed_setup_object_abandons_the_run_row_and_frees_the_lane(self):
        from capsule.blind import launch
        from capsule.runners import mac
        task = {"id": "t_1", "tier": "mac", "branch_name": "b"}
        route = {"provider": "p", "account": "a", "model": "m"}
        with tempfile.TemporaryDirectory() as home:
            tick = mock.Mock(home=home, host="h")
            with (mock.patch.object(launch.config, "blind_tests",
                                    return_value={"interface_commands": ["x"]}),
                  mock.patch.object(launch, "txn"),
                  mock.patch.object(launch.config, "run_meta", return_value={}),
                  mock.patch.object(launch.lease, "start_run", return_value=3),
                  mock.patch.object(launch, "_pinned", side_effect=RuntimeError("db")),
                  mock.patch.object(launch, "_abandon") as abandon):
                with self.assertRaises(RuntimeError):
                    launch._start(tick, mock.Mock(), {"caps": {"claim_ttl": 1}}, task, [], "c",
                                  route, 1)
            abandon.assert_called_once()
            self.assertFalse(mac.busy(home))


class RunnerChangeRefusedInReview(unittest.TestCase):
    def test_a_card_in_review_keeps_its_runner(self):
        from capsule import card_fields, db
        from capsule.tests.test_lease import board
        conn = board(self)
        tid = db.new_task(conn, title="t", assignee="dev-frontend", now=1)
        conn.execute("UPDATE tasks SET status='review' WHERE id=?", (tid,))
        conn.commit()
        with mock.patch.object(card_fields, "_open", return_value=mock.Mock(
                execute=conn.execute, close=lambda: None)):
            with self.assertRaises(card_fields.FieldsRefused):
                card_fields._set_fields("h", "b", tid, {"runner": "mac"})
        self.assertNotEqual(conn.execute("SELECT tier FROM tasks").fetchone()[0], "mac")
