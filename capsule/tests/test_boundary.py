"""C07c (t_c0090cf5): the boundary restart scheduling. `mark` over real git (a temp repo), `step`
over a real board db with the three effects (gap probe, restart, status) injected as recorders,
because the real ones drive launchd; the real script runs against a fake launchctl in
tests/test_capsule_tick_sh.sh (oracle 2). Oracle 1 is `step`'s return value; oracle 2 here reads
the `guard_state` flag and the outbox rows back from the board db."""
import subprocess
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from capsule import boundary, db, dispatch
from capsule.db import guard_get
from capsule.tests import test_landing, tmpclean
from capsule.tests.test_lander import git

NOW = 1_800_000_000
SHA = "a" * 40


class Mark(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(tmpclean.sweep, self.dir)
        self.repo = self.dir / "repo"
        subprocess.run(["git", "init", "-q", "-b", "main", str(self.repo)], check=True)
        self.put({"capsule/tick.py": "from capsule import outbox, tick_guard\n",
                  "capsule/outbox.py": "from capsule import slack\n",
                  "capsule/slack.py": "X = 1\n", "capsule/tick_guard.py": "X = 1\n",
                  "capsule/cli_only.py": "X = 1\n", "README.md": "a\n"})
        self.base = git(self.repo, "rev-parse", "HEAD")
        board = self.dir / "board.db"
        db.create_board(board)
        self.conn = db.connect(board)
        self.addCleanup(self.conn.close)
        self.cfg = {"board": "capsule-corp", "lander": {"restart_with": ["my-app"]}}

    def put(self, files):
        for name, text in files.items():
            (self.repo / name).parent.mkdir(parents=True, exist_ok=True)
            (self.repo / name).write_text(text)
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "c")

    def land(self, name):
        self.base = git(self.repo, "rev-parse", "HEAD")      # each landing is its own range
        self.put({name: f"changed {self.base}\n"})
        sha = git(self.repo, "rev-parse", "HEAD")
        return boundary.mark(self.conn, self.repo, self.base, sha, self.cfg, NOW), sha

    def test_a_change_to_tick_py_or_a_transitive_import_sets_the_flag_with_the_sha_and_boards(self):
        for name in ("capsule/slack.py", "capsule/tick.py"):
            marked, sha = self.land(name)
            self.assertTrue(marked, name)
            flag, _ = guard_get(self.conn, boundary.KEY)
            self.assertEqual((flag["sha"], flag["boards"]), (sha, ["capsule-corp", "my-app"]))

    def test_a_maintenance_import_counts_a_module_tick_does_not_load_does_not(self):
        self.assertTrue(self.land("maintenance/vault_git.py")[0])
        self.conn.execute("DELETE FROM guard_state")
        self.conn.commit()
        self.assertFalse(self.land("capsule/cli_only.py")[0])
        self.assertFalse(self.land("README.md")[0])
        self.assertIsNone(guard_get(self.conn, boundary.KEY)[0])

    def test_a_second_landing_keeps_the_first_since(self):
        self.land("capsule/tick.py")
        first, _ = guard_get(self.conn, boundary.KEY)
        head = git(self.repo, "rev-parse", "HEAD")
        boundary.mark(self.conn, self.repo, self.base, head, self.cfg, NOW + 99)
        self.assertEqual(guard_get(self.conn, boundary.KEY)[0]["since"], first["since"])


class Step(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(tmpclean.sweep, self.dir)
        board = self.dir / "board.db"
        db.create_board(board)
        self.conn = db.connect(board)
        self.addCleanup(self.conn.close)
        self.cfg = {"board": "capsule-corp"}
        self.fired, self.pids, self.new_code = [], {}, False
        self.gaps = {"capsule-corp": True, "my-app": True}
        self.set_flag(["capsule-corp", "my-app"])

    def set_flag(self, boards, **extra):
        flag = {"sha": SHA, "since": NOW, "boards": boards, **extra}
        db.guard_set(self.conn, boundary.KEY, flag, now=NOW)

    def flag(self):
        return guard_get(self.conn, boundary.KEY)[0]

    def step(self, now=NOW + 1, **kw):
        args = {"gap": lambda b, h: self.gaps[b], "fire": lambda h, b: self.fired.append(b),
                "status": lambda h, b: self.pids.get(b, {}), "loaded": lambda sha: self.new_code}
        return boundary.step(self.conn, self.cfg, self.dir, now, **{**args, **kw})

    def outbox(self):
        return [r[0] for r in self.conn.execute("SELECT body FROM outbox")]

    def test_no_flag_does_nothing(self):
        db.guard_set(self.conn, boundary.KEY, None, now=NOW)
        self.assertEqual((self.step(), self.fired), ("none", []))

    def test_pending_with_a_gap_restarts_this_board_first_and_only_this_board(self):
        self.pids = {"capsule-corp": {"pid": 11}}
        self.assertEqual(self.step(), "fired")
        self.assertEqual(self.fired, ["capsule-corp"])
        self.assertEqual(self.flag()["fired"]["pid"], 11)
        self.assertEqual(self.step(NOW + 5), "waiting")        # restart in flight: not fired twice
        self.assertEqual(self.fired, ["capsule-corp"])

    def test_pending_with_a_live_run_waits_and_keeps_the_flag(self):
        self.gaps["capsule-corp"] = False
        self.assertEqual((self.step(), self.fired), ("waiting", []))
        self.assertEqual(self.flag()["boards"], ["capsule-corp", "my-app"])

    def test_the_second_board_waits_for_the_new_tick_then_restarts_in_its_own_gap(self):
        fired = {"board": "capsule-corp", "at": NOW, "pid": 11}
        self.set_flag(["capsule-corp", "my-app"], fired=fired)
        self.assertEqual((self.step(NOW + 5), self.fired), ("waiting", []))   # still the old code
        self.new_code = True                                                  # the new tick runs
        self.gaps["my-app"] = False
        self.assertEqual((self.step(NOW + 9), self.fired), ("waiting", []))   # a live run
        self.assertEqual(self.flag()["boards"], ["my-app"])
        self.gaps["my-app"] = True
        self.pids = {"my-app": {"pid": 22}}
        self.assertEqual((self.step(NOW + 12), self.fired), ("fired", ["my-app"]))

    def test_the_flag_clears_when_the_last_board_is_back_on_the_new_sha_with_a_new_pid(self):
        self.new_code = True
        self.set_flag(["my-app"], fired={"board": "my-app", "at": NOW, "pid": 22})
        self.pids = {"my-app": {"alive": True, "pid": 22, "main_sha": SHA}}
        self.assertEqual(self.step(NOW + 5), "waiting")                       # same pid
        self.pids = {"my-app": {"alive": True, "pid": 33, "main_sha": "b" * 40}}
        self.assertEqual(self.step(NOW + 6), "waiting")                       # new pid, wrong sha
        self.pids = {"my-app": {"alive": True, "pid": 33, "main_sha": SHA}}
        with unittest.mock.patch.object(boundary, "_contains", return_value=True):
            self.assertEqual(self.step(NOW + 7), "done")
        self.assertIsNone(self.flag())

    def test_no_gap_in_45_minutes_writes_one_scheduled_line_and_keeps_the_flag(self):
        self.gaps["capsule-corp"] = False
        self.step(NOW + boundary.GAP_WAIT - 1)
        self.assertEqual(self.outbox(), [])
        self.step(NOW + boundary.GAP_WAIT + 1)
        self.step(NOW + boundary.GAP_WAIT + 60)
        self.assertEqual(len(self.outbox()), 1)
        self.assertIn("restart pending", self.outbox()[0])
        self.assertTrue(self.flag()["warned"])

    def test_a_restart_that_never_comes_back_asks_once_and_parks_the_flag(self):
        self.set_flag(["capsule-corp"], fired={"board": "capsule-corp", "at": NOW, "pid": 11})
        self.assertEqual(self.step(NOW + boundary.FIRE_TTL + 1), "failed")
        self.assertEqual(self.step(NOW + boundary.FIRE_TTL + 60), "failed")
        self.assertEqual(len(self.outbox()), 1)
        self.assertEqual(self.fired, [])


class RealHas(Step):
    """The real `_has` (nothing injected): it is the PROCESS (BOOT), not the disk. It runs over
    its own two-commit temp repo, not the real checkout (CI is depth 1: no `HEAD~1`)."""

    def setUp(self):
        super().setUp()
        self.repo = self.dir / "repo"
        subprocess.run(["git", "init", "-q", "-b", "main", str(self.repo)], check=True)
        for n in ("one", "two"):
            (self.repo / "f.txt").write_text(n)
            git(self.repo, "add", "-A")
            git(self.repo, "commit", "-qm", n)
        patch = unittest.mock.patch.object(boundary.paths, "REPO", self.repo)
        patch.start()
        self.addCleanup(patch.stop)

    def head(self, rev):
        return git(self.repo, "rev-parse", rev)

    def test_an_old_process_stays_waiting_after_the_checkout_moved_under_it(self):
        landed = self.head("HEAD")                     # the checkout on disk contains it now
        self.set_flag(["capsule-corp"], fired={"board": "capsule-corp", "at": NOW, "pid": 11})
        flag = self.flag() | {"sha": landed}
        db.guard_set(self.conn, boundary.KEY, flag, now=NOW)
        with unittest.mock.patch.object(boundary, "BOOT", self.head("HEAD~1")):
            got = boundary.step(self.conn, self.cfg, self.dir, NOW + 5)
        self.assertEqual(got, "waiting")
        self.assertEqual(self.flag()["boards"], ["capsule-corp"])
        self.assertIsNotNone(self.flag()["fired"])
        with unittest.mock.patch.object(boundary, "BOOT", landed):      # the new process
            self.assertEqual(boundary.step(self.conn, self.cfg, self.dir, NOW + 6), "done")


class DispatchHold(unittest.TestCase):
    def test_no_new_claims_while_this_boards_restart_is_in_flight(self):
        from capsule.tests.test_dispatch import CFG, EPIC, board, ids
        conn = board(self)
        card = db.new_task(conn, title="a", assignee="dev-backend", branch=EPIC)
        self.assertEqual(ids(dispatch.ready(conn, CFG, host_tiers={"strong"})), [card])
        fired = {"board": CFG["board"], "at": NOW, "pid": 1}
        db.guard_set(conn, boundary.KEY, {"sha": SHA, "since": NOW, "boards": [CFG["board"]],
                                          "fired": fired}, now=NOW)
        self.assertEqual(dispatch.ready(conn, CFG, host_tiers={"strong"}), [])
        db.guard_set(conn, boundary.KEY, {"sha": SHA, "since": NOW, "boards": ["other"],
                                          "fired": {**fired, "board": "other"}}, now=NOW)
        self.assertEqual(ids(dispatch.ready(conn, CFG, host_tiers={"strong"})), [card])
        db.guard_set(conn, boundary.KEY, None, now=NOW)
        self.assertEqual(ids(dispatch.ready(conn, CFG, host_tiers={"strong"})), [card])


class Gap(unittest.TestCase):
    """`_gap` asks docker for worker containers (capsule-run-id); a gate container that only
    carries capsule=1 must not hold the boundary open."""
    def gap(self, containers):
        def ps(args, **kw):
            want = args[args.index("--filter") + 1].split("=", 1)[1]
            ids = [i for i, labels in containers.items() if want in labels]
            return subprocess.CompletedProcess(args, 0, "\n".join(ids), "")
        pgrep = subprocess.CompletedProcess([], 1)
        with unittest.mock.patch.object(boundary, "open_runs", return_value=0), \
                unittest.mock.patch.object(boundary.docker_door, "docker_run", ps), \
                unittest.mock.patch.object(boundary.subprocess, "run", return_value=pgrep):
            return boundary._gap("capsule-corp", Path("/nonexistent"))

    def test_a_capsule_container_without_a_run_id_does_not_block_the_gap(self):
        self.assertTrue(self.gap({"gate1": ["capsule=1", "capsule.kind=run"]}))

    def test_a_worker_container_blocks_the_gap(self):
        self.assertFalse(self.gap({"w1": ["capsule=1", "capsule-run-id"]}))


class LandingMarks(test_landing.Landing):
    def test_a_landing_that_touches_the_tick_marks_a_restart(self):
        sha = self.put("capsule/tick.py", "X = 1\n", "tick")
        self.assertEqual(self.land()["outcome"], "landed")
        flag, _ = guard_get(self.conn, boundary.KEY)
        self.assertEqual((flag["sha"], flag["boards"]), (sha, ["b"]))

    def test_a_landing_that_does_not_touch_the_tick_marks_nothing(self):
        self.put("docs/a.md", "x\n", "doc")
        self.assertEqual(self.land()["outcome"], "landed")
        self.assertIsNone(guard_get(self.conn, boundary.KEY)[0])


class FireWaitsOutAContractCommand(unittest.TestCase):
    def quiet(self):
        with tempfile.TemporaryDirectory() as tmp, unittest.mock.patch.object(
                boundary.subprocess, "Popen") as popen:
            boundary._fire(Path(tmp), "b")
        argv = popen.call_args.args[0]
        return int(argv[argv.index("--quiet") + 1])

    def test_the_restart_quiet_wait_outlasts_one_contract_command(self):
        from capsule import tick
        longest = max(tick.CONTRACT_TIMEOUT, 1200)    # 1200: the limit this wait was sized for
        self.assertGreater(self.quiet(), longest)

    def test_a_fired_restart_is_given_the_quiet_wait_and_the_script_wait(self):
        script_wait = 900                             # capsule-tick.sh WAIT default
        self.assertGreaterEqual(boundary.FIRE_TTL, self.quiet() + script_wait)


if __name__ == "__main__":
    unittest.main()
