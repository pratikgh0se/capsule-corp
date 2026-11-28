"""Single-writer tick guard (Korin incident t_80681144, row 517; Fable's B2 ruling). Z2 (two
oracles): unit tests drive the argv parser with fake `ps` output; the lock-based tests below
drive `guard_start`/`check_row` against a real `fcntl.flock` (never faked - locks are kernel
state, not something to stub); the Integration test spawns a real process that takes the real
lock and asserts `capsule tick`/`capsule check` agree with it and with a controlled argv scan."""
import contextlib
import fcntl
import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
import unittest.mock
from pathlib import Path

from capsule import intent, paths, tick_guard


def ps_output(rows):
    """Fake `ps -Awwo pid,ppid,args` stdout: `rows` is [(pid, ppid, argv_str), ...]."""
    lines = ["  PID  PPID ARGS"] + [f"{pid} {ppid} {argv}" for pid, ppid, argv in rows]
    return "\n".join(lines) + "\n"


def fake_run(rows):
    def run(argv, **kw):
        assert argv[0] == "ps"
        return subprocess.CompletedProcess(argv, 0, ps_output(rows), "")
    return run


def hold(path, pid):
    """A real, held `fcntl.flock` on `path`, its content `pid` - the caller closes it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    f = open(path, "a+")
    fcntl.flock(f, fcntl.LOCK_EX)
    f.seek(0)
    f.truncate()
    f.write(str(pid))
    f.flush()
    return f


class LiveTicks(unittest.TestCase):
    def test_zero_ticks(self):
        self.assertEqual(tick_guard.live_ticks(fake_run([(100, 1, "python3 -m capsule run-one")])), [])

    def test_the_dash_m_form_matches(self):
        rows = [(100, 1, "python3 -m capsule run-one"), (200, 1, "python3 -m capsule tick --once")]
        self.assertEqual(tick_guard.live_ticks(fake_run(rows)), [(200, "python3 -m capsule tick --once")])

    def test_the_console_script_form_matches(self):
        rows = [(300, 1, "capsule tick")]
        self.assertEqual(tick_guard.live_ticks(fake_run(rows)), [(300, "capsule tick")])

    def test_two_ticks(self):
        rows = [(200, 1, "python3 -m capsule tick"), (201, 1, "/usr/bin/python3 -m capsule tick --once")]
        self.assertEqual([pid for pid, _ in tick_guard.live_ticks(fake_run(rows))], [200, 201])

    def test_a_shell_wrapper_mentioning_the_words_without_dash_m_does_not_match(self):
        """B2: `capsule`/`tick` appearing as two of many words in one argument is never a match -
        only the real `-m capsule tick` token sequence, or the console-script's own argv[0:2]."""
        rows = [(400, 1, "claude -p answer with capsule tick words")]
        self.assertEqual(tick_guard.live_ticks(fake_run(rows)), [])

    def test_a_non_contiguous_pair_of_tokens_does_not_match(self):
        rows = [(400, 1, "python3 -m capsule report tick-metrics")]
        self.assertEqual(tick_guard.live_ticks(fake_run(rows)), [])

    def test_the_callers_whole_ancestor_chain_is_excluded(self):
        """I2/B2: excluding only the caller's own pid is not enough - a wrapper shell that is
        itself (falsely) matched must also be dropped when it is an ancestor of the caller."""
        me = os.getpid()
        parent = 424242
        rows = [(parent, 1, "python3 -m capsule tick"), (me, parent, "python3 -m capsule check"),
                (99, 1, "python3 -m capsule tick")]
        pids = [pid for pid, _ in tick_guard.live_ticks(fake_run(rows), exclude_pid=me)]
        self.assertEqual(pids, [99])

    def test_ps_failure_returns_none_not_zero(self):
        """I1: unknown is never reported as zero."""
        def raiser(argv, **kw):
            raise OSError("ps: command not found")
        self.assertIsNone(tick_guard.live_ticks(raiser))

    def test_ps_nonzero_exit_returns_none(self):
        def run(argv, **kw):
            return subprocess.CompletedProcess(argv, 1, "", "permission denied")
        self.assertIsNone(tick_guard.live_ticks(run))


class CheckRow(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        patch = unittest.mock.patch.dict(os.environ, {"CAPSULE_HOME": str(self.home)})
        patch.start()
        self.addCleanup(patch.stop)

    def test_lock_free_and_no_argv_fails(self):
        self.assertEqual(tick_guard.check_row(self.home, board="b1", run_cmd=fake_run([])),
                         ("FAIL", "tick", "no tick (lock free)"))

    def test_lock_held_and_argv_agree_is_ok(self):
        f = hold(tick_guard.lock_path(self.home, "b1"), 4242)
        self.addCleanup(f.close)
        rows = [(4242, 1, "python3 -m capsule tick --board b1")]
        self.assertEqual(tick_guard.check_row(self.home, board="b1", run_cmd=fake_run(rows)),
                         ("ok", "tick", "lock held by pid 4242"))

    def test_lock_held_but_argv_disagrees_fails_and_names_pids(self):
        f = hold(tick_guard.lock_path(self.home, "b1"), 111)
        self.addCleanup(f.close)
        tick = "python3 -m capsule tick --board b1"
        rows = [(111, 1, tick), (222, 1, tick)]
        self.assertEqual(tick_guard.check_row(self.home, board="b1", run_cmd=fake_run(rows)),
                         ("FAIL", "tick", "lock pid 111 vs argv pids [111, 222] disagree"))

    def test_a_tick_without_board_is_not_counted_for_a_named_board(self):
        f = hold(tick_guard.lock_path(self.home, "b1"), 4242)
        self.addCleanup(f.close)
        rows = [(786, 1, "python3 -m capsule tick"),
                (4242, 1, "python3 -m capsule tick --board b1")]
        self.assertEqual(tick_guard.check_row(self.home, board="b1", run_cmd=fake_run(rows)),
                         ("ok", "tick", "lock held by pid 4242"))

    def test_a_default_board_tick_without_board_flag_is_ok(self):
        """launchd starts the tick with no --board: it serves the home's default board."""
        board = tick_guard._default_board(self.home)
        f = hold(tick_guard.lock_path(self.home, board), 5884)
        self.addCleanup(f.close)
        rows = [(5884, 1, "python -m capsule tick --replace")]
        self.assertEqual(tick_guard.check_row(self.home, run_cmd=fake_run(rows)),
                         ("ok", "tick", "lock held by pid 5884"))

    def test_another_boards_explicit_tick_is_not_counted_for_the_default_board(self):
        board = tick_guard._default_board(self.home)
        f = hold(tick_guard.lock_path(self.home, board), 5884)
        self.addCleanup(f.close)
        rows = [(5884, 1, "python -m capsule tick --replace"),
                (6000, 1, "python -m capsule tick --board other")]
        self.assertEqual(tick_guard.check_row(self.home, run_cmd=fake_run(rows)),
                         ("ok", "tick", "lock held by pid 5884"))

    def test_two_default_board_ticks_without_flag_still_fail(self):
        board = tick_guard._default_board(self.home)
        f = hold(tick_guard.lock_path(self.home, board), 5884)
        self.addCleanup(f.close)
        rows = [(5884, 1, "python -m capsule tick"), (5885, 1, "python -m capsule tick --replace")]
        self.assertEqual(tick_guard.check_row(self.home, run_cmd=fake_run(rows)),
                         ("FAIL", "tick", "lock pid 5884 vs argv pids [5884, 5885] disagree"))

    def test_board_equals_form_is_recognised(self):
        f = hold(tick_guard.lock_path(self.home, "b1"), 4242)
        self.addCleanup(f.close)
        rows = [(4242, 1, "python3 -m capsule tick --board=b1")]
        self.assertEqual(tick_guard.check_row(self.home, board="b1", run_cmd=fake_run(rows)),
                         ("ok", "tick", "lock held by pid 4242"))

    def test_lock_free_but_argv_sees_one_fails(self):
        rows = [(555, 1, "python3 -m capsule tick --board b1")]
        self.assertEqual(tick_guard.check_row(self.home, board="b1", run_cmd=fake_run(rows)),
                         ("FAIL", "tick", "lock free vs argv pids [555] disagree"))

    def test_ps_unavailable_fails_even_with_the_lock_held(self):
        """I1: `check_row` must not let a broken `ps` read as agreement."""
        f = hold(tick_guard.lock_path(self.home, "b1"), 4242)
        self.addCleanup(f.close)

        def raiser(argv, **kw):
            raise OSError("ps: not found")
        self.assertEqual(tick_guard.check_row(self.home, board="b1", run_cmd=raiser),
                         ("FAIL", "tick", "ps unavailable"))


class IntentExamples(unittest.TestCase):
    """Z1/I4: the shipped intent examples are executed through the real `check_row`, not merely
    linted for shape."""

    def test_the_shipped_examples_match_check_row(self):
        home = Path(tempfile.mkdtemp())
        patch = unittest.mock.patch.dict(os.environ, {"CAPSULE_HOME": str(home)})
        patch.start()
        self.addCleanup(patch.stop)
        seen = 0
        for ex in intent.load("tick_guard"):
            doc = ex["given"]["document"]
            if doc.get("check") != "tick_guard":
                continue                                   # board_guard's own examples, below
            seen += 1
            board = f"intent-{ex['id']}"
            if doc["lock_held"]:
                f = hold(tick_guard.lock_path(home, board), doc["lock_pid"])
                self.addCleanup(f.close)
            argv = f"python3 -m capsule tick --board {board}"
            rows = [(pid, 1, argv) for pid in doc["argv_pids"]]
            got = tick_guard.check_row(home, board=board, run_cmd=fake_run(rows))
            self.assertEqual(got, (ex["expect"]["level"], "tick", ex["expect"]["detail"]), ex["id"])
        self.assertEqual(seen, 3)   # a mis-tagged file must not silently skip every example


class GuardStart(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())

    def test_starts_clean_and_writes_the_lock_file(self):
        f = tick_guard.guard_start(self.home, "b1")
        self.addCleanup(f.close)
        path = tick_guard.lock_path(self.home, "b1")
        self.assertEqual(path.read_text().strip(), str(os.getpid()))
        # the lock is genuinely held - a second attempt from another handle must fail
        f2 = open(path, "a+")
        self.assertRaises(OSError, fcntl.flock, f2, fcntl.LOCK_EX | fcntl.LOCK_NB)
        f2.close()

    def test_refuses_when_the_lock_is_already_held(self):
        holder = hold(tick_guard.lock_path(self.home, "b1"), 9999)
        self.addCleanup(holder.close)
        with self.assertRaises(SystemExit) as ctx:
            tick_guard.guard_start(self.home, "b1")
        self.assertEqual(ctx.exception.code, 3)

    def test_replace_kills_the_holder_and_continues(self):
        path = tick_guard.lock_path(self.home, "b1")
        holder = hold(path, 9999)
        killed = []

        def kill(pid, sig):
            killed.append((pid, sig))
            holder.close()      # the old tick actually exiting releases the real flock
        f = tick_guard.guard_start(self.home, "b1", replace=True, kill=kill, sleep=lambda s: None)
        self.addCleanup(f.close)
        self.assertEqual(killed, [(9999, signal.SIGTERM)])
        self.assertEqual(path.read_text().strip(), str(os.getpid()))

    def test_replace_refuses_when_the_holder_never_exits(self):
        """B1: the timeout path must refuse, never start a second tick beside a live one."""
        path = tick_guard.lock_path(self.home, "b1")
        holder = hold(path, 9999)
        self.addCleanup(holder.close)
        clock = {"t": 0.0}
        with self.assertRaises(SystemExit) as ctx:
            tick_guard.guard_start(self.home, "b1", replace=True, kill=lambda pid, sig: None,
                                   sleep=lambda s: clock.__setitem__("t", clock["t"] + s),
                                   monotonic=lambda: clock["t"])
        self.assertEqual(ctx.exception.code, 3)
        self.assertEqual(path.read_text().strip(), "9999")   # never overwritten

    def test_replace_treats_a_process_lookup_error_as_already_exited(self):
        path = tick_guard.lock_path(self.home, "b1")
        holder = hold(path, 9999)

        def kill(pid, sig):
            holder.close()
            raise ProcessLookupError()
        f = tick_guard.guard_start(self.home, "b1", replace=True, kill=kill, sleep=lambda s: None)
        self.addCleanup(f.close)
        self.assertEqual(path.read_text().strip(), str(os.getpid()))

    def test_replace_refuses_on_permission_error(self):
        path = tick_guard.lock_path(self.home, "b1")
        holder = hold(path, 9999)
        self.addCleanup(holder.close)

        def kill(pid, sig):
            raise PermissionError()
        with self.assertRaises(SystemExit) as ctx:
            tick_guard.guard_start(self.home, "b1", replace=True, kill=kill, sleep=lambda s: None)
        self.assertEqual(ctx.exception.code, 3)


class BoardGuard(unittest.TestCase):
    """ruling Q9: a `--board` tick with no boards/<board>/config.json stays stopped."""

    def setUp(self):
        self.home = Path(tempfile.mkdtemp())

    def test_no_config_refuses_with_the_stay_stopped_message(self):
        self.assertEqual(tick_guard.board_guard(self.home, "b1"),
                         (False, "no config for board b1; nothing to serve"))

    def test_a_config_present_runs(self):
        paths.board_dir("b1", self.home).mkdir(parents=True)
        paths.config_path(self.home, "b1").write_text("{}")
        self.assertEqual(tick_guard.board_guard(self.home, "b1"), (True, None))

    def test_cli_with_no_config_exits_0_prints_the_message_and_takes_no_lock(self):
        """F1: the exit code and the absent lock file are two oracles a mutated `return 1` (or a
        deleted guard) cannot both satisfy."""
        env = dict(os.environ, CAPSULE_HOME=str(self.home), AGENT_OPS_NO_SEND="1")
        r = subprocess.run([sys.executable, "-m", "capsule", "tick", "--board", "nobody", "--once"],
                           cwd=paths.REPO, env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0)
        self.assertIn("no config for board nobody; nothing to serve", r.stderr)
        self.assertFalse(tick_guard.lock_path(self.home, "nobody").exists())


class NoBoardGuard(unittest.TestCase):
    """Task 9 round-2 re-check M2 (fix round 1, Opus review finding 1): a `capsule tick` with no
    `--board` must refuse (exit 3) rather than take `tick-default.lock` under a config it never
    actually read, and the board it locks must be the SAME read that decides the board it pins -
    never two separate `config.Loader` reads that a mid-flight edit can make disagree (the
    two-writer hole)."""

    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.valid_cfg = json.loads((paths.REPO / "capsule" / "config.example.json").read_text())
        self.valid_cfg["board"] = "b9"

    def test_cli_with_missing_config_and_no_board_exits_3_and_takes_no_lock(self):
        env = dict(os.environ, CAPSULE_HOME=str(self.home), AGENT_OPS_NO_SEND="1")
        r = subprocess.run([sys.executable, "-m", "capsule", "tick", "--once"],
                           cwd=paths.REPO, env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 3)
        self.assertIn("config unreadable", r.stderr)
        self.assertFalse((self.home / "tick-default.lock").exists())

    def test_cli_with_unreadable_config_and_no_board_exits_3_and_takes_no_lock(self):
        """F1: the exit code and the absent lock file are two oracles a mutated `return 1` (or a
        deleted guard) cannot both satisfy."""
        self.home.mkdir(parents=True, exist_ok=True)
        paths.config_path(self.home).write_text("{not json")
        env = dict(os.environ, CAPSULE_HOME=str(self.home), AGENT_OPS_NO_SEND="1")
        r = subprocess.run([sys.executable, "-m", "capsule", "tick", "--once"],
                           cwd=paths.REPO, env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 3)
        self.assertIn("config unreadable", r.stderr)
        self.assertFalse(tick_guard.lock_path(self.home).exists())
        self.assertFalse((self.home / "tick-default.lock").exists())

    def test_a_config_edited_between_the_lock_and_the_first_pass_is_refused_not_served(self):
        """Review finding 1, the actual race: the config names b9 when `_cmd_tick` decides the
        lock and the pin, then changes to b10 in the window right after the lock is taken but
        before `Tick.once()` first reads. `guard_start` is where that window opens (patched
        here to edit the file as its own side effect, the earliest point after the lock exists)
        - with the fix, `_cmd_tick` already fixed `pin_board="b9"` before that window, so
        `Tick.once()`'s own pin check refuses the b10 read (`config_error`) instead of quietly
        adopting it, which is what the pre-fix code did (`Tick(board=None)`, pin_board=None,
        self-pins to whatever it first reads)."""
        from capsule import tick_guard as tick_guard_mod
        from capsule.__main__ import main
        self.home.mkdir(parents=True, exist_ok=True)
        paths.config_path(self.home).write_text(json.dumps(self.valid_cfg))   # board b9
        real_guard_start = tick_guard_mod.guard_start

        def racy_guard_start(home, board, **kw):
            f = real_guard_start(home, board, **kw)
            paths.config_path(self.home).write_text(json.dumps(dict(self.valid_cfg, board="b10")))
            return f

        out = io.StringIO()
        with unittest.mock.patch.object(tick_guard_mod, "guard_start", racy_guard_start):
            with contextlib.redirect_stdout(out):
                rc = main(["tick", "--once", "--capsule-home", str(self.home)])
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out.getvalue())["config_error"], 1)


class Held(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())

    def test_false_with_no_lock_file(self):
        self.assertFalse(tick_guard.held(self.home, "b1"))

    def test_true_while_a_real_flock_is_held(self):
        f = hold(tick_guard.lock_path(self.home, "b1"), os.getpid())
        self.addCleanup(f.close)
        self.assertTrue(tick_guard.held(self.home, "b1"))

    def test_false_again_once_released(self):
        f = hold(tick_guard.lock_path(self.home, "b1"), os.getpid())
        fcntl.flock(f, fcntl.LOCK_UN)
        f.close()
        self.assertFalse(tick_guard.held(self.home, "b1"))


class BoardGuardIntentExamples(unittest.TestCase):
    """Z1: `board_guard`'s own intent examples, run through the real function."""

    def test_the_shipped_examples_match_board_guard(self):
        home = Path(tempfile.mkdtemp())
        seen = 0
        for ex in intent.load("tick_guard"):
            doc = ex["given"]["document"]
            if doc.get("check") != "board_guard":
                continue
            seen += 1
            board = doc["board"]
            if doc["config_exists"]:
                paths.board_dir(board, home).mkdir(parents=True, exist_ok=True)
                paths.config_path(home, board).write_text("{}")
            ok, msg = tick_guard.board_guard(home, board)
            level, detail = ("ok", "runs") if ok else ("exit0", msg)
            self.assertEqual((level, detail), (ex["expect"]["level"], ex["expect"]["detail"]), ex["id"])
        self.assertEqual(seen, 2)


class NoBoardIntentExamples(unittest.TestCase):
    """Z1 (row 545, intent round): `no_board`'s own intent examples - the board-less two-writer
    hole and its fix round 1 close (Opus review finding 1) - run through the real CLI and
    `Tick`/`guard_start`, never compared as bare strings against the jsonl."""

    @staticmethod
    def _cfg(board):
        cfg = json.loads((paths.REPO / "capsule" / "config.example.json").read_text())
        cfg["board"] = board
        return cfg

    def _run_unreadable(self, home):
        home.mkdir(parents=True, exist_ok=True)
        paths.config_path(home).write_text("{not json")
        env = dict(os.environ, CAPSULE_HOME=str(home), AGENT_OPS_NO_SEND="1")
        r = subprocess.run([sys.executable, "-m", "capsule", "tick", "--once"],
                           cwd=paths.REPO, env=env, capture_output=True, text=True, timeout=30)
        self.assertIn("config unreadable", r.stderr)   # a second, independent oracle
        level = f"exit{r.returncode}"
        detail = "no lock" if not (home / "tick-default.lock").exists() else "took default lock"
        return level, detail

    def _run_readable(self, home, board):
        home.mkdir(parents=True, exist_ok=True)
        paths.config_path(home).write_text(json.dumps(self._cfg(board)))
        env = dict(os.environ, CAPSULE_HOME=str(home), AGENT_OPS_NO_SEND="1")
        r = subprocess.run([sys.executable, "-m", "capsule", "tick", "--once"],
                           cwd=paths.REPO, env=env, capture_output=True, text=True, timeout=30)
        locked_own = (home / f"tick-{board}.lock").exists() and not (home / "tick-default.lock").exists()
        # the tick's own attempt to open the board it pinned to - its board_db path, seen only
        # in the traceback (no board db exists here) - is a second oracle, independent of the
        # lock filename, that the SAME board is what got served
        served_own = f"boards/{board}/kanban.db" in (r.stdout + r.stderr)
        level = "ok" if locked_own else "FAIL"
        detail = f"locks and serves {board}" if served_own else f"does not serve {board}"
        return level, detail

    def _run_race(self, home, board, raced_board):
        from capsule import tick_guard as tick_guard_mod
        from capsule.__main__ import main
        home.mkdir(parents=True, exist_ok=True)
        paths.config_path(home).write_text(json.dumps(self._cfg(board)))
        real_guard_start = tick_guard_mod.guard_start

        def racy_guard_start(h, b, **kw):
            f = real_guard_start(h, b, **kw)
            paths.config_path(home).write_text(json.dumps(self._cfg(raced_board)))   # the race
            return f

        out = io.StringIO()
        with unittest.mock.patch.object(tick_guard_mod, "guard_start", racy_guard_start):
            with contextlib.redirect_stdout(out):
                rc = main(["tick", "--once", "--capsule-home", str(home)])
        self.assertEqual(rc, 0)
        counts = json.loads(out.getvalue())
        served_raced = (home / "boards" / raced_board).exists()   # never created - would only
        # exist if `Tick` had actually gone looking for the raced board's db
        refused = counts.get("config_error") == 1 and not served_raced
        level = "refused" if refused else "served"
        detail = f"config_error, {raced_board} never served" if refused else "served the raced board"
        return level, detail

    def test_the_shipped_examples_match_no_board(self):
        seen = 0
        for ex in intent.load("tick_guard"):
            doc = ex["given"]["document"]
            if doc.get("check") != "no_board":
                continue
            seen += 1
            home = Path(tempfile.mkdtemp())
            scenario = doc["scenario"]
            if scenario == "unreadable":
                level, detail = self._run_unreadable(home)
            elif scenario == "readable":
                level, detail = self._run_readable(home, doc["board"])
            elif scenario == "race":
                level, detail = self._run_race(home, doc["board"], doc["raced_board"])
            else:
                self.fail(f"unknown no_board scenario {scenario!r}")
            self.assertEqual((level, detail), (ex["expect"]["level"], ex["expect"]["detail"]), ex["id"])
        self.assertEqual(seen, 3)


DUMMY_SRC = ("import fcntl, os, sys, time\n"
             "f = open(sys.argv[1], 'a+')\n"
             "fcntl.flock(f, fcntl.LOCK_EX)\n"
             "f.seek(0); f.truncate(); f.write(str(os.getpid())); f.flush()\n"
             "time.sleep(30)\n")


TICK_DEFAULT = "python3 -m capsule tick --board default"


class Integration(unittest.TestCase):
    """Row 517's own worked example: a real spawned process holds the real advisory lock."""

    def setUp(self):
        self.capsule_home = Path(tempfile.mkdtemp())
        # a readable legacy config so `_cmd_tick`'s single config read (Task 9 round-2 M2,
        # Opus review fix round 1 finding 1) lets this test reach the lock-refusal path it
        # actually exercises, instead of refusing on the config itself
        cfg = json.loads((paths.REPO / "capsule" / "config.example.json").read_text())
        cfg["board"] = "default"
        paths.config_path(self.capsule_home).write_text(json.dumps(cfg))
        self.lock_path = tick_guard.lock_path(self.capsule_home, "default")
        self.dummy = subprocess.Popen([sys.executable, "-c", DUMMY_SRC, str(self.lock_path)])
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                if int(self.lock_path.read_text().strip()) == self.dummy.pid:
                    return
            except (OSError, ValueError):
                pass
            time.sleep(0.1)
        self.fail("dummy never took the lock")

    def tearDown(self):
        self.dummy.terminate()
        try:
            self.dummy.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.dummy.kill()
            self.dummy.wait(timeout=10)

    def _env(self):
        env = dict(os.environ)
        env["CAPSULE_HOME"] = str(self.capsule_home)
        return env

    def _hermetic_ps_env(self, rows):
        """PATH with a fake `ps` reporting exactly `rows` - isolates the argv oracle from
        whatever else this host runs (this machine keeps a real tick alive, never-idle rule)."""
        fake_bin = self.capsule_home / "fakebin"
        fake_bin.mkdir(exist_ok=True)
        body = "\n".join(f"{pid} {ppid} {argv}" for pid, ppid, argv in rows)
        (fake_bin / "ps").write_text(f"#!/bin/sh\ncat <<'EOF'\n  PID  PPID ARGS\n{body}\nEOF\n")
        (fake_bin / "ps").chmod(0o755)
        env = self._env()
        env["PATH"] = f"{fake_bin}{os.pathsep}{env.get('PATH', '')}"
        return env

    def test_capsule_tick_refuses_with_exit_3(self):
        r = subprocess.run([sys.executable, "-m", "capsule", "tick", "--once"],
                           cwd=paths.REPO, env=self._env(), capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 3)
        self.assertIn(f"tick: refusing, pid {self.dummy.pid} already running", r.stderr)

    def test_capsule_check_agrees_with_the_lock_and_the_dummy(self):
        """M4: `dummy.pid` (known from `Popen`, never guessed) and the lock file's own content
        are the two independent observations - both checked against each other and against
        `capsule check`'s printed report."""
        self.assertEqual(int(self.lock_path.read_text().strip()), self.dummy.pid)
        env = self._hermetic_ps_env([(self.dummy.pid, 1, TICK_DEFAULT)])
        r = subprocess.run([sys.executable, "-m", "capsule", "check"],
                           cwd=paths.REPO, env=env, capture_output=True, text=True, timeout=30)
        self.assertIn(f"ok tick: lock held by pid {self.dummy.pid}", r.stdout)

    def test_capsule_check_returncode_1_when_the_tick_row_fails(self):
        """I3: the exit code is checked as its own, real oracle - a subprocess's actual
        returncode, never the production expression re-typed into the test."""
        env = self._hermetic_ps_env([(self.dummy.pid, 1, TICK_DEFAULT),
                                     (424242, 1, TICK_DEFAULT)])   # a stray extra "tick"
        r = subprocess.run([sys.executable, "-m", "capsule", "check"],
                           cwd=paths.REPO, env=env, capture_output=True, text=True, timeout=30)
        tick_line = next(line for line in r.stdout.splitlines() if line.startswith("FAIL tick:"))
        self.assertIn("disagree", tick_line)
        self.assertEqual(r.returncode, 1)


if __name__ == "__main__":
    unittest.main()
