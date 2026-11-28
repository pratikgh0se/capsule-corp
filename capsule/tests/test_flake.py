"""S11 (R14): the tests a card adds are rerun five times behind `redesign.flake_reruns`; a mixed
result is a finding, never a pass. Own module so test_tick.py (frozen file length) does not grow.

Z2 oracles: the per-run exit codes (and the command's own exit status, the count of runs that
failed), and the runner's parsed summary line; the planted flaky test also counts its own runs in
a file the host reads back. Nothing here touches docker or the network: the rerun command runs
in a local `sh` against a throwaway clone, with a `timeout` shim because macOS has none."""
import json
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from capsule import flake, intent, workspace
from capsule.tests import tmpclean
from capsule.tests.test_check_gate import CheckGateCase
from capsule.tests.test_tick import IMPL_DONE, VAL_PASS, git

ON = {"redesign": {"flake_reruns": True}}
FLAKY = """import unittest
from pathlib import Path


class Planted(unittest.TestCase):
    def test_every_other_run(self):
        runs = Path(__file__).with_name("runs.txt")
        n = int(runs.read_text()) + 1 if runs.exists() else 1
        runs.write_text(str(n))
        self.assertEqual(n % 2, 0)
"""
STABLE = ("import unittest\n\n\nclass Steady(unittest.TestCase):\n"
          "    def test_ok(self):\n        self.assertEqual(1 + 1, 2)\n")
CHATTY = ("import sys\nimport unittest\n\n\nclass Chatty(unittest.TestCase):\n"
          "    def test_prints(self):\n        print('to stdout')\n"
          "        sys.stderr.write('to stderr\\n')\n        self.assertEqual(1 + 1, 2)\n")
BROKEN = ("import unittest\n\n\nclass Bad(unittest.TestCase):\n"
          "    def test_never(self):\n        self.assertEqual(1, 2)\n")
OLD = """import unittest


class OldTest(unittest.TestCase):
    def test_existing(self):
        self.assertIn('a', 'abc')

    def test_edited(self):
        self.assertIn('a', 'abc')
"""
FLAKY_ID = "tests.test_flaky.Planted.test_every_other_run"
SHIM = '#!/bin/sh\nshift\nexec "$@"\n'      # `timeout <secs> cmd...` without the timeout


def write(root, rel, text):
    path = Path(root) / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def run_cmd(cmd, ws, shim):
    """The command exactly as the container would run it (sh), on this machine."""
    done = subprocess.run(["sh", "-c", cmd], cwd=ws, capture_output=True, text=True, timeout=120,
                          env={**os.environ, "PATH": f"{shim}{os.pathsep}{os.environ['PATH']}"})
    return done.returncode, done.stdout + done.stderr


def ident(cmd):
    return cmd.split()[2]


class CloneCase(unittest.TestCase):
    """A real clone (the tick's own layout) of an origin holding one old test file."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(tmpclean.sweep, self.dir)
        origin = self.dir / "origin"
        origin.mkdir()
        git(origin, "init", "-q", "-b", "main")
        write(origin, "tests/test_old.py", OLD)
        git(origin, "add", ".")
        git(origin, "commit", "-qm", "init")
        self.ws = workspace.clone(str(origin), "main", self.dir / "root")
        self.base = subprocess.run(["git", "-C", str(self.ws), "rev-parse", "HEAD"], check=True,
                                   capture_output=True, text=True).stdout.strip()
        (self.dir / "bin").mkdir()
        write(self.dir, "bin/timeout", SHIM)
        os.chmod(self.dir / "bin" / "timeout", 0o755)
        self.shim = str(self.dir / "bin")

    def commands(self, cfg=ON):
        return flake.commands(cfg, self.ws, lambda: self.base)


class Listing(CloneCase):
    def test_only_tests_whose_def_the_diff_adds_are_rerun(self):
        edited = OLD.replace("def test_edited(self):\n        self.assertIn('a', 'abc')",
                             "def test_edited(self):\n        self.assertIn('b', 'abc')")
        write(self.ws, "tests/test_old.py", edited + "\n    def test_added_method(self):\n"
                                                     "        self.assertIn('a', 'abc')\n")
        write(self.ws, "tests/test_new.py",
              "import unittest\n\n\ndef test_not_a_method():\n    pass\n\n\n"
              "class NewTest(unittest.TestCase):\n"
              "    def test_a(self):\n        pass\n\n"
              "    def helper(self):\n        pass\n\n"
              "    @unittest.skip('x')\n    def test_b(self):\n        pass\n\n\n"
              "class Plain:\n    def test_no_base(self):\n        pass\n\n\n"
              "class Obj(object):\n    def test_object_only(self):\n        pass\n")
        write(self.ws, "tests/helpers.py", "class X:\n    def test_not_in_a_test_file(self):\n"
                                           "        pass\n")
        write(self.ws, "my-tests/test_dashed.py", STABLE)           # not importable as a module
        write(self.ws, "tests/test_data.txt", "def test_text(self): pass\n")
        got = self.commands()
        self.assertEqual([ident(c) for c in got],
                         ["tests.test_new.NewTest.test_a", "tests.test_new.NewTest.test_b",
                          "tests.test_old.OldTest.test_added_method"])
        self.assertTrue(all(c.startswith(flake.PREFIX) for c in got))

    def test_a_redefined_test_is_rerun_once(self):
        write(self.ws, "tests/test_dup.py", "import unittest\n\n\nclass Dup(unittest.TestCase):\n"
                                            "    def test_dup(self):\n        pass\n\n"
                                            "    def test_dup(self):\n        pass\n")
        self.assertEqual([ident(c) for c in self.commands()], ["tests.test_dup.Dup.test_dup"])

    def test_a_broken_or_linked_test_file_is_skipped_not_fatal(self):
        write(self.ws, "tests/test_broken.py", "def (:\n")
        (self.ws / "tests" / "test_link.py").symlink_to(self.ws / "tests" / "test_old.py")
        self.assertEqual(self.commands(), [])

    def test_a_deleted_file_and_no_change_add_nothing(self):
        self.assertEqual(self.commands(), [])
        (self.ws / "tests" / "test_old.py").unlink()
        self.assertEqual(self.commands(), [])

    def test_switch_off_reads_nothing(self):
        write(self.ws, "tests/test_flaky.py", FLAKY)
        boom = mock.Mock(side_effect=AssertionError("base read with the switch off"))
        with mock.patch.object(flake.workspace, "diff") as diff:
            for cfg in ({}, {"redesign": {}}, {"redesign": {"flake_reruns": False}}):
                self.assertEqual(flake.commands(cfg, self.ws, boom), [])
        diff.assert_not_called()
        self.assertEqual(len(self.commands()), 1)                   # and on, the same file counts

    def test_more_than_the_cap_are_named_in_one_evidence_only_marker(self):
        body = "".join(f"    def test_{i:02d}(self):\n        pass\n\n" for i in range(27))
        write(self.ws, "tests/test_many.py", "import unittest\n\n\nclass Many(unittest.TestCase):\n"
                                             + body)
        got = self.commands()
        self.assertEqual(len(got), flake.MAX_TESTS + 1)
        self.assertEqual([ident(c).rsplit(".", 1)[1] for c in got[:-1]],
                         [f"test_{i:02d}" for i in range(flake.MAX_TESTS)])
        self.assertEqual(got[-1], f": flake-rerun-cap {27 - flake.MAX_TESTS} of 27"
                                  " added tests were not rerun")


class RealReruns(CloneCase):
    """The commands run for real (local sh): oracle one is the per-run exit codes and the
    command's own exit status, oracle two the runner's summary lines and the test's own count."""

    def setUp(self):
        super().setUp()
        for name, source in (("flaky", FLAKY), ("stable", STABLE), ("broken", BROKEN),
                             ("chatty", CHATTY)):
            write(self.ws, f"tests/test_{name}.py", source)
        self.out = {ident(c).split(".")[1]: run_cmd(c, self.ws, self.shim)
                    for c in self.commands()}

    def test_a_stable_test_passes_all_five(self):
        rc, out = self.out["test_stable"]
        self.assertEqual(re.findall(r"rc=(\d+) (.*)", out), [("0", "OK")] * 5)
        self.assertEqual(rc, 0)

    def test_a_stable_test_that_prints_still_reads_clean(self):
        rc, out = self.out["test_chatty"]
        self.assertEqual(re.findall(r"rc=(\d+) (.*)", out), [("0", "OK")] * 5)
        self.assertEqual((rc, self.lines("test_chatty")), (0, []))

    def test_a_timed_out_run_ends_the_loop_and_reads_unreadable(self):
        hung = self.dir / "hung"
        write(hung, "timeout", "#!/bin/sh\nexit 124\n")
        os.chmod(hung / "timeout", 0o755)
        cmd = [c for c in self.commands() if ".test_stable." in c][0]
        rc, out = run_cmd(cmd, self.ws, str(hung))
        self.assertEqual(re.findall(r"rc=(\d+)", out), ["124"])         # no second run
        self.assertEqual(rc, 1)
        (line,) = self.failures([{"cmd": cmd, "exit": rc, "output": out}])
        self.assertIn("unreadable: 0 of 5 runs", line)

    def test_a_test_that_fails_on_alternate_runs_shows_each_run(self):
        rc, out = self.out["test_flaky"]
        self.assertEqual([r for r, _ in re.findall(r"rc=(\d+) (.*)", out)],
                         ["1", "0", "1", "0", "1"])
        self.assertEqual(re.findall(r"rc=\d+ (.*)", out),
                         ["FAILED (failures=1)", "OK", "FAILED (failures=1)", "OK",
                          "FAILED (failures=1)"])
        self.assertEqual(rc, 3)                                     # the failed-run count
        self.assertEqual((self.ws / "tests" / "runs.txt").read_text(), "5")   # five real runs

    def lines(self, name):
        entry = {"cmd": [c for c in self.commands() if f".{name}." in c][0],
                 "exit": self.out[name][0], "output": self.out[name][1]}
        return self.failures([entry])

    def test_the_failures_read_the_real_output_the_same_way(self):
        lines = self.lines
        self.assertEqual(lines("test_stable"), [])
        self.assertEqual(lines("test_flaky"),
                         [f"flaky test `{FLAKY_ID}`: 2 of 5 reruns passed, exit codes"
                          " [1, 0, 1, 0, 1] (flake rerun, S11)"])
        self.assertEqual(lines("test_broken"),
                         ["test `tests.test_broken.Bad.test_never` failed all 5 reruns, exit codes"
                          " [1, 1, 1, 1, 1] (flake rerun, S11)"])

    def failures(self, outputs, cfg=ON):
        spool = self.dir / "spool"
        write(spool, "r1/job.json", json.dumps({"inputs": {"outputs": outputs}}))
        return flake.failures(cfg, None, spool, {"run_id": "r1"})


class FlakeTickBase(CheckGateCase):
    """A real tick whose fake container runs only the rerun commands, for real (local sh)."""

    def setUp(self):
        super().setUp()
        self.shim = Path(tempfile.mkdtemp())
        self.addCleanup(tmpclean.sweep, self.shim)
        write(self.shim, "timeout", SHIM)
        os.chmod(self.shim / "timeout", 0o755)
        homes, start = {}, self.runner.start

        def begin(run_id, ws, **kw):
            name = start(run_id, ws, **kw)
            homes[name] = ws
            return name

        def run(name, cmd, timeout):
            if not cmd.startswith(flake.PREFIX):
                return 0, "ok"
            return run_cmd(cmd, homes[name], str(self.shim))
        self.runner.start, self.runner.exec = begin, run
        self.tick.contracts.grace = 120     # real reruns: the job finishes inside its first pass
        self.script["implementer"] = [[IMPL_DONE]]
        self.script["validator"] = [[VAL_PASS]]

    def switch(self, on):
        self.cfg["redesign"] = {"flake_reruns": on}
        self.write_cfg()

    def implement(self, rel, source):
        self.hooks["implementer"] = lambda job, sp, run: write(job["workspace"], rel, source)

    def settle(self, tid=None):
        """The card-check replay's ticks: review, verdict, then a failed round's retry waits."""
        self.tick.once()
        self.tick.once()
        self.runner.up = False
        self.tick.once()
        self.runner.up = True

    def flake_entries(self):
        return [o for o in self.outputs() if o["cmd"].startswith(flake.PREFIX)]


class TickFlake(FlakeTickBase):
    def test_a_planted_flaky_test_fails_the_round_despite_passing_verdicts(self):
        self.switch(True)
        self.implement("tests/test_flaky.py", FLAKY)
        self.settle()
        task = self.task()
        self.assertEqual((task["status"], task["failure_count"]), ("ready", 1))
        (entry,) = self.flake_entries()
        self.assertEqual(entry["exit"], 3)
        self.assertIn(f"flaky test `{FLAKY_ID}`: 2 of 5 reruns passed", self.last_comment())

    def test_the_implementer_rerun_is_told_which_test_is_flaky(self):
        self.switch(True)
        self.implement("tests/test_flaky.py", FLAKY)
        self.script["implementer"] = [[IMPL_DONE], [IMPL_DONE]]
        for _ in range(10):
            self.tick.once()
            if len(self.runs("implementer")) == 2:
                break
        sp = self.spool_dir(self.runs("implementer")[1]["run_id"])
        findings = json.loads((sp / "job.json").read_text())["inputs"]["review_findings"]
        self.assertTrue(any(f"flaky test `{FLAKY_ID}`" in f for f in findings), findings)

    def test_a_stable_new_test_passes_all_five_and_the_card_merges(self):
        self.switch(True)
        self.implement("tests/test_stable.py", STABLE)
        self.settle()
        (entry,) = self.flake_entries()
        self.assertEqual((entry["exit"], len(re.findall(r"rc=0 OK", entry["output"]))), (0, 5))
        self.assertEqual(self.task()["status"], "done")

    def test_switch_off_adds_no_command_and_changes_nothing(self):
        self.switch(False)
        self.implement("tests/test_flaky.py", FLAKY)
        self.settle()
        self.assertEqual(self.flake_entries(), [])
        self.assertFalse([c for c in self.runner.calls
                          if c[0] == "exec" and c[2].startswith(flake.PREFIX)])
        self.assertEqual(list(self.home.rglob("runs.txt")), [])      # the test never ran
        self.assertEqual(self.task()["status"], "done")


# Z1, contracts/intent/flake_reruns.jsonl: what each example's case must observe. The cases run
# on these built-in expectations alone, so they hold before (and without) the rows file.
EXPECT = {
    "mixed-is-flaky": {"per_run_exit_codes": [1, 0, 1, 0, 1], "flagged_flaky": True,
                       "merged": False},
    "stable-passes": {"per_run_exit_codes": [0, 0, 0, 0, 0], "flagged_flaky": False,
                      "merged": True},
    "switch-off-adds-nothing": {"rerun_commands": 0, "merged": True},
}


class IntentCases(FlakeTickBase):
    def replay(self, case):
        """Observe one case on a fresh board."""
        self.tearDown()
        self.setUp()
        stable = case == "stable-passes"
        self.switch(case != "switch-off-adds-nothing")
        self.implement("tests/test_stable.py" if stable else "tests/test_flaky.py",
                       STABLE if stable else FLAKY)
        self.settle()
        entries = self.flake_entries()
        seen = {"merged": self.task()["status"] == "done"}
        if case == "switch-off-adds-nothing":
            return dict(seen, rerun_commands=len(entries))
        codes = [int(c) for c in re.findall(r"rc=(\d+)", entries[0]["output"])]
        flagged = "flaky test" in self.last_comment() if not seen["merged"] else False
        return dict(seen, per_run_exit_codes=codes, flagged_flaky=flagged)

    def test_each_case_observes_its_built_in_expectation(self):
        for case, want in EXPECT.items():
            self.assertEqual(self.replay(case), want, case)

    def test_the_shipped_rows_match_what_the_cases_observe(self):
        rows = intent.load("flake_reruns")
        if not rows:
            self.skipTest("contracts/intent/flake_reruns.jsonl is not shipped yet")
        self.assertEqual({r["id"] for r in rows}, set(EXPECT))
        for ex in rows:
            seen = self.replay(ex["given"]["document"]["case"])
            for key, value in ex["expect"].items():
                self.assertEqual(seen[key], value, f"{ex['id']}: {key}")


if __name__ == "__main__":
    unittest.main()
