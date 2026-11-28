"""maintenance/hog_check.py with injected ps, clock and kill: no real process is touched."""
import contextlib
import io
import os
import signal
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import hog_check as hc   # noqa: E402

H = 3 * 3600
CHROME = ("/Applications/Google Chrome.app/Contents/Frameworks/Google Chrome Framework.framework/"
          "Helpers/Google Chrome Helper (Renderer).app/Contents/MacOS/"
          "Google Chrome Helper (Renderer)")
CLAUDE_APP = "/Applications/Claude.app/Contents/MacOS/Claude"
CLAUDE_HELPER_APP = "/Applications/Claude.app/Contents/Frameworks/Claude Helper.app/x/claude"


def row(ppid=50, cpu=99.0, age=H, comm=CHROME):
    return (ppid, cpu, age, comm)


def flags_of(*facts):
    return lambda pid: set(facts)


def fake_run(out="", rc=0, seen=None, exc=None):
    def fake(argv, **kw):
        if seen is not None:
            seen.append(argv)
        if exc:
            raise exc
        return subprocess.CompletedProcess(argv, rc, out, "")
    return fake


@contextlib.contextmanager
def patched_run(fake):
    orig, subprocess.run = subprocess.run, fake
    try:
        yield
    finally:
        subprocess.run = orig


class Pure(unittest.TestCase):
    def test_etime(self):
        self.assertEqual(hc._etime_s("01:02"), 62)
        self.assertEqual(hc._etime_s("2:03:04"), 7384)
        self.assertEqual(hc._etime_s("1-00:00:01"), 86401)

    def test_kinds(self):
        none = flags_of()
        py = "/usr/bin/python3"
        self.assertEqual(hc.kind_of(50, CHROME, 7, none), "browser helper")
        self.assertEqual(hc.kind_of(1, py, 7, flags_of("test")), "orphaned python run")
        self.assertIsNone(hc.kind_of(1, py, 7, none))                  # a service, not a test
        self.assertIsNone(hc.kind_of(50, py, 7, flags_of("test")))     # parent alive
        self.assertIsNone(hc.kind_of(1, "/System/Library/x/Python", 7, flags_of("test")))
        self.assertIsNone(hc.kind_of(1, "/usr/libexec/Chrome Helper", 7, none))
        self.assertIsNone(hc.kind_of(1, "WindowServer", 7, none))
        self.assertIsNone(hc.kind_of(1, "/bin/capsule-tick.sh", 7, none))

    def test_browser_matched_by_bundle_not_substring(self):
        none = flags_of()
        self.assertIsNone(hc.kind_of(50, "/home/x/search/arcade/Helper", 7, none))
        self.assertIsNone(hc.kind_of(50, "/Applications/Marcus.app/Contents/Helper", 7, none))
        arc = "/Applications/Arc.app/Contents/Frameworks/Arc Helper.app/Contents/MacOS/Arc Helper"
        self.assertEqual(hc.kind_of(50, arc, 7, none), "browser helper")
        main = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
        self.assertIsNone(hc.kind_of(50, main, 7, none))

    def test_app_main_executable_never_listed(self):
        self.assertIsNone(hc.kind_of(1, CLAUDE_APP, 7, flags_of("test", "bg")))

    def test_claude_daemon_only_bare_cli_without_tty(self):
        cli = "/home/x/.local/bin/claude"
        self.assertEqual(hc.kind_of(1, cli, 7, flags_of("bg")), "idle claude helper")
        self.assertIsNone(hc.kind_of(1, cli, 7, flags_of("bg", "tty")))    # live session
        self.assertIsNone(hc.kind_of(1, cli, 7, flags_of()))               # no helper arg
        self.assertIsNone(hc.kind_of(1, CLAUDE_HELPER_APP, 7, flags_of("bg")))   # inside an .app
        self.assertIsNone(hc.kind_of(1, "/home/x/claude-other", 7, flags_of("bg")))

    def test_candidates_rules(self):
        a = {10: row(), 11: row(cpu=50.0), 12: row(age=60), 13: row(), 14: row(), 15: row()}
        b = {10: row(), 11: row(), 12: row(age=7200 - 1), 13: row(cpu=10.0), 14: row(),
             15: row()}
        got = hc.candidates(a, b, protected={14}, flags=flags_of())
        self.assertEqual([c[0] for c in got], [10, 15])  # 14 itself is protected

    def test_child_of_launchd_job_stays_eligible(self):
        both = {50: row(ppid=1, comm="/Applications/Google Chrome.app/Contents/MacOS/Chrome"),
                51: row(ppid=50)}                        # frozen renderer under a listed app
        got = hc.candidates(both, both, protected={50}, flags=flags_of())
        self.assertEqual([c[0] for c in got], [51])      # the launchd pid itself never is

    def test_launchd_pids_every_row(self):
        out = "PID\tStatus\tLabel\n123\t0\tcom.x.other\n-\t0\tcom.capsule.tick\n9\t0\ta\n"
        with patched_run(fake_run(out)):
            self.assertEqual(hc.launchd_pids(), {123, 9})

    def test_launchctl_failures_refuse(self):
        for fake in (fake_run(exc=FileNotFoundError("launchctl")), fake_run("1\t0\ta\n", rc=1),
                     fake_run("PID\tStatus\tLabel\n-\t0\tx\n")):
            with patched_run(fake), self.assertRaises(hc.Refusal):
                hc.launchd_pids()

    def test_flags_decide_without_leaking(self):
        seen = []
        with patched_run(fake_run("??  python -m unittest secret --bg", seen=seen)):
            got = hc.proc_flags(5)
        self.assertEqual((got, seen), ({"test", "bg"}, [["ps", "-o", "tty=,args=", "-p", "5"]]))
        with patched_run(fake_run("ttys001  claude -r")):
            self.assertEqual(hc.proc_flags(5), {"tty"})

    def test_never_self(self):
        me = {os.getpid(): row()}
        self.assertEqual(hc.candidates(me, me, set(), flags_of()), [])


class Run(unittest.TestCase):
    def go(self, apply, first, second, alive_seq=(True,), later=None, launchd=lambda: set()):
        lines, kills, sleeps = [], [], []
        samples = iter([first, second])
        alive = iter(alive_seq)
        after = second if later is None else later
        hc.run(apply, 60, sleeps.append, lambda: next(samples, after), launchd,
               lambda p, s: kills.append((p, s)), lambda p: next(alive, True), lines.append,
               flags_of())
        return lines, kills, sleeps

    def test_none(self):
        lines, kills, sleeps = self.go(True, {}, {})
        self.assertEqual((lines, kills, sleeps), (["hog-check: no frozen processes"], [], [60]))

    def test_preview_never_kills(self):
        lines, kills, _ = self.go(False, {10: row()}, {10: row()})
        self.assertEqual(kills, [])
        self.assertIn("pid 10 browser helper", lines[0])
        self.assertIn("would stop", lines[0])

    def test_apply_term_then_kill(self):
        lines, kills, _ = self.go(True, {10: row()}, {10: row()}, alive_seq=(True,) * 20)
        self.assertEqual(kills, [(10, signal.SIGTERM), (10, signal.SIGKILL)])
        self.assertTrue(lines[0].endswith(": killed"))

    def test_apply_term_enough(self):
        lines, kills, _ = self.go(True, {10: row()}, {10: row()}, alive_seq=(True, False))
        self.assertEqual(kills, [(10, signal.SIGTERM)])
        self.assertTrue(lines[0].endswith(": terminated"))

    def test_launchd_pid_excluded_whatever_parent(self):
        lines, kills, _ = self.go(True, {10: row()}, {10: row()}, launchd=lambda: {10})
        self.assertEqual((lines, kills), (["hog-check: no frozen processes"], []))

    def test_pid_changed_before_term_not_signalled(self):
        for later in ({10: row(comm="/bin/other")}, {10: row(age=60)}, {}):
            lines, kills, _ = self.go(True, {10: row()}, {10: row()}, later=later)
            self.assertEqual(kills, [])
            self.assertIn("changed before stop", lines[0])

    def test_pid_changed_before_kill_not_signalled(self):
        calls = iter([{10: row()}, {10: row()}, {10: row()}])    # samples, then recheck 1
        lines, kills = [], []
        gone = {10: row(age=60)}                                 # recheck 2: recycled pid
        hc.run(True, 60, lambda s: None, lambda: next(calls, gone), lambda: set(),
               lambda p, s: kills.append(s), lambda p: True, lines.append, flags_of())
        self.assertEqual(kills, [signal.SIGTERM])
        self.assertIn("changed before kill", lines[0])

    def test_launchd_refusal_at_recheck_kills_nothing(self):
        n = []

        def launchd():
            n.append(1)
            if len(n) > 1:                    # fine for the protected set, fails at the recheck
                raise hc.Refusal("no answer")
            return set()
        kills = []
        with self.assertRaises(hc.Refusal):
            hc.run(True, 60, lambda s: None, lambda: {10: row()}, launchd,
                   lambda p, s: kills.append(s), lambda p: True, lambda s: None, flags_of())
        self.assertEqual(kills, [])

    def test_launchctl_failure_refuses_before_any_listing(self):
        def launchd():
            raise hc.Refusal("no answer")
        lines = []
        with self.assertRaises(hc.Refusal):
            hc.run(True, 60, lambda s: None, lambda: {10: row()}, launchd, out=lines.append)
        self.assertEqual(lines, [])

    def test_ps_command_has_no_args_or_env(self):
        seen = []
        with patched_run(fake_run("PID PPID %CPU ELAPSED COMM\n", seen=seen)):
            hc.ps_rows()
        self.assertEqual(seen[0], ["ps", "-Ao", "pid,ppid,pcpu,etime,comm"])

    def test_main_refuses_when_ps_fails(self):
        def boom(*a, **k):
            raise hc.Refusal("boom")
        orig, hc.run = hc.run, boom
        try:
            with contextlib.redirect_stderr(io.StringIO()) as e:
                self.assertEqual(hc.main([]), 1)
        finally:
            hc.run = orig
        self.assertIn("boom", e.getvalue())


if __name__ == "__main__":
    unittest.main()
