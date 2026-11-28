"""The blind runner closes its own memory and environment to the code under test (Linux only).
Real checks run the runner as a program the way the blind run does (a real subprocess, a real
product child); the platform and failure cases drive `_protect` and the `__main__` guard with a
stand-in libc. Oracles for the attack: the product child's own report and the runner's rows."""
import os
import subprocess
import sys
import textwrap
import unittest
from unittest import mock

from capsule.blind import cli
from capsule.blind.harness import api, unittest_runner as runner
from capsule.tests.test_blind_harness import RunnerTest

LINUX = sys.platform.startswith("linux")
PROBE = """
import os, subprocess, sys
pid, addr = sys.argv[1], int(sys.argv[2])

def attempt(label, path, seek=None):
    try:
        with open(path, "rb") as f:
            if seek is not None:
                f.seek(seek)
            f.read(8)
        print(label + ": read")
    except PermissionError:
        print(label + ": PermissionError")

attempt("environ", "/proc/%s/environ" % pid)
attempt("mem", "/proc/%s/mem" % pid, addr)
try:
    os.listdir("/proc/%s/fd" % pid)
    print("fd: read")
except PermissionError:
    print("fd: PermissionError")
attempt("self", "/proc/self/environ")
print("child:", subprocess.run(["true"]).returncode)
"""
ATTACK = """
import os
import unittest

from harness import run


class Attack(unittest.TestCase):
    def test_attack(self):
        out = run("python3 probe.py %d %d" % (os.getpid(), id(os))).stdout
        print(out)
        self.assertEqual(out.split()[:__N__], __EXPECT__)
"""
CLOSED = ("environ: PermissionError mem: PermissionError fd: PermissionError"
          " self: read child: 0").split()
# only environ is compared: Yama ptrace_scope 1 (Ubuntu CI) refuses a child /proc/<pid>/mem read
OPEN = "environ: read".split()
PARENT = """
import sys
stat = open("/proc/%s/stat" % sys.argv[1]).read()
print(open("/proc/%s/comm" % stat.rsplit(")", 1)[1].split()[1]).read().strip())
"""
PARENT_TEST = """
import os
import unittest

from harness import run


class Parent(unittest.TestCase):
    def test_parent(self):
        out = run("python3 parent.py %d" % os.getpid()).stdout.strip()
        self.assertTrue(out, "parent.py printed nothing")
        self.assertTrue(out __CHECK__ ("sh", "bash", "dash"), out)
"""


class Fake:
    """A libc whose prctl returns what it is told: (set result, get result)."""

    def __init__(self, set_rc=0, get_rc=0):
        self.rcs = {runner.PR_SET_DUMPABLE: set_rc, runner.PR_GET_DUMPABLE: get_rc}
        self.calls = []

    def prctl(self, option, *args):
        self.calls.append((option, [a.value for a in args]))
        return self.rcs[option]


class Protect(unittest.TestCase):
    def test_a_system_that_is_not_linux_is_left_alone(self):
        with mock.patch.object(sys, "platform", "darwin"), \
                mock.patch("ctypes.CDLL", side_effect=AssertionError("libc touched")):
            self.assertIsNone(runner._protect())

    def test_on_linux_it_clears_the_flag_and_reads_it_back(self):
        fake = Fake()
        with mock.patch.object(sys, "platform", "linux"), \
                mock.patch("ctypes.CDLL", return_value=fake):
            self.assertIsNone(runner._protect())
        self.assertEqual([c[0] for c in fake.calls], [4, 3])
        self.assertEqual(fake.calls[0][1], [0, 0, 0, 0])

    def test_a_refused_set_is_a_problem(self):
        with mock.patch.object(sys, "platform", "linux"), \
                mock.patch("ctypes.CDLL", return_value=Fake(set_rc=-1)):
            self.assertIn("PR_SET_DUMPABLE", runner._protect())

    def test_a_set_that_did_not_stick_is_a_problem(self):
        with mock.patch.object(sys, "platform", "linux"), \
                mock.patch("ctypes.CDLL", return_value=Fake(get_rc=1)):
            self.assertIn("still set", runner._protect())

    def test_no_libc_is_a_problem_never_a_pass(self):
        with mock.patch.object(sys, "platform", "linux"), \
                mock.patch("ctypes.CDLL", side_effect=OSError("no libc")):
            self.assertIn("no libc", runner._protect())


class Program(RunnerTest):
    """The runner as a program, in a real subprocess."""

    def test_a_failed_protection_is_a_failed_setup_and_nothing_runs(self):
        self.write("test_one.py", """
            import unittest
            class One(unittest.TestCase):
                def test_ok(self):
                    pass
            """)
        code = textwrap.dedent(f"""
            import ctypes, runpy, sys
            class Libc:
                def prctl(self, *a):
                    return -1
            ctypes.CDLL = lambda *a, **k: Libc()
            sys.platform = "linux"
            sys.argv = ["harness.unittest_runner", "--card", "{self.card}", "--out", "{self.out}",
                        "{self.cards}"]
            runpy.run_module("harness.unittest_runner", run_name="__main__")
            """)
        proc = subprocess.run([sys.executable, "-c", code], cwd=self.blind, capture_output=True,
                              text=True)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("cannot protect the runner", proc.stderr)
        self.assertNotIn("Ran ", proc.stdout)
        self.assertFalse((self.out / "results.jsonl").exists())

    def linux_only(self):
        if not LINUX:
            self.skipTest("the dumpable flag is a Linux feature")

    def attack(self, expect, *, protected=True):
        self.write("probe.py", PROBE, where=self.ws)
        self.write("test_attack.py",
                   ATTACK.replace("__N__", str(len(expect))).replace("__EXPECT__", repr(expect)))
        if protected:
            return self.runner()
        line = (f"cd {self.blind} && {sys.executable} -c 'import sys;"
                f" import harness.unittest_runner as r; sys.exit(r.main())'"
                f" --card {self.card} --out {self.out} {self.cards}")
        env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
        env[api.WORKSPACE_ENV] = str(self.ws)
        proc = subprocess.run(line, shell=True, cwd=self.ws, capture_output=True, text=True,
                              env=env)
        return proc, []

    def test_the_product_cannot_read_the_runners_environ_or_mem_but_can_run_and_read_self(self):
        self.linux_only()
        proc, rows = self.attack(CLOSED)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual([(r["id"].split(".", 1)[1], r["result"]) for r in rows],
                         [("test_attack", "pass")])
        self.contract(proc, rows)

    def test_the_same_attack_succeeds_when_the_runner_is_not_protected(self):
        # the second oracle: without `_protect` the probe reads both, so the test above can fail
        self.linux_only()
        proc, _ = self.attack(OPEN, protected=False)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def parent(self, check, *, shell_stays=False):
        """The runner started by the run line under `sh -c`; the product asks who the runner's
        parent is. `shell_stays` drops the `exec` and appends `; :` (the old behaviour)."""
        self.write("parent.py", PARENT, where=self.ws)
        self.write("test_parent.py", PARENT_TEST.replace("__CHECK__", check))
        line = cli.RUN_LINES["unittest"][0].format(card=self.card)
        line = line.replace("/blind", str(self.blind)) + f" --out {self.out}"
        env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
        env[api.WORKSPACE_ENV] = str(self.ws)
        if shell_stays:
            line = line.replace("exec ", "", 1) + "; :"
        return subprocess.run(line, shell=True, cwd=self.ws,
                              capture_output=True, text=True, env=env)

    def test_the_run_line_leaves_no_shell_as_the_runners_parent(self):
        self.linux_only()
        proc = self.parent("not in")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_a_run_line_that_cannot_exec_does_leave_a_shell_parent(self):
        # the second oracle for the test above: the check can see a shell parent
        self.linux_only()
        proc = self.parent("in", shell_stays=True)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_a_failing_test_still_exits_1_through_the_run_line(self):
        self.write("test_f.py", """
            import unittest
            class F(unittest.TestCase):
                def test_fail(self):
                    self.fail("x")
            """)
        proc, rows = self.runner()
        self.assertEqual((proc.returncode, [r["result"] for r in rows]), (1, ["fail"]))

    def test_results_are_the_same_whatever_the_system(self):
        self.write("test_two.py", """
            import unittest
            class Two(unittest.TestCase):
                def test_pass(self):
                    pass
                def test_fail(self):
                    self.fail("x")
            """)
        proc, rows = self.runner()
        self.assertEqual(sorted((r["id"].split(".", 1)[1], r["result"]) for r in rows),
                         [("test_fail", "fail"), ("test_pass", "pass")])
        self.assertEqual(proc.returncode, 1)
        self.contract(proc, rows)


if __name__ == "__main__":
    unittest.main()
