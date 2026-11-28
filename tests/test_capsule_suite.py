"""bin/capsule-suite (card t_45a84c3f): one capsule suite at a time, machine-wide, by flock.

Every test drives the real script against tiny fake suites in a temp dir with `--lock-dir`
pointing at that temp dir (never the real lock, never the real suite). The intent rows in
contracts/intent/capsule_suite.jsonl are replayed below with inputs from each row."""
import contextlib
import json
import os
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import types
import unittest
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).resolve().parent.parent / "bin" / "capsule-suite"
sys.path.insert(0, str(SCRIPT.parent / "suite_hook"))
import sharded  # noqa: E402
INTENT = Path(__file__).resolve().parent.parent / "contracts" / "intent" / "capsule_suite.jsonl"
INTENT_IDS = {"second-invocation-waits-then-succeeds", "hung-suite-is-killed-with-124",
              "killed-wrapper-leaves-no-unlocked-suite"}
PY = sys.executable
ORDER = ["A start", "A end", "B start", "B end"]


def pid_alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


class SuiteBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.log = self.dir / "log.txt"
        self.child_pid = self.dir / "child.pid"
        self.grand_pid = self.dir / "grand.pid"
        self.procs = []
        self.addCleanup(self._reap)

    def _reap(self):
        for p in self.procs:
            if p.poll() is None:
                p.kill()
                p.wait()
            p.stdout.close()
        for f in self.dir.glob("*.pid"):
            with contextlib.suppress(ValueError, ProcessLookupError):
                os.kill(int(f.read_text()), signal.SIGKILL)

    def start(self, *args, cmd=None, cwd=None):
        argv = [PY, str(SCRIPT), "--lock-dir", str(self.dir), *args]
        if cmd is not None:
            argv += ["--", *cmd]
        p = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                             cwd=cwd or self.dir)
        self.procs.append(p)
        return p

    def run_suite(self, *args, cmd=None, cwd=None, timeout=30):
        p = self.start(*args, cmd=cmd, cwd=cwd)
        out, _ = p.communicate(timeout=timeout)
        return p.returncode, out

    def logging_suite(self, name, secs):
        code = (f"import time; f=open({str(self.log)!r},'a');"
                f"f.write('{name} start\\n'); f.flush();"
                f"time.sleep({secs}); f.write('{name} end\\n'); f.close()")
        return [PY, "-c", code]

    def hanging_suite(self, secs=60):
        """Hangs inside test_hang (never echoes its name); a grandchild `sleep` lives on too."""
        code = (f"import os,subprocess,time; g=subprocess.Popen(['sleep','{secs}']);"
                f"open({str(self.grand_pid)!r},'w').write(str(g.pid));"
                f"open({str(self.child_pid)!r},'w').write(str(os.getpid()))\n"
                f"def test_hang(): time.sleep({secs})\n"
                "test_hang()")
        return [PY, "-c", code]

    def wait_for(self, cond, secs=10):
        end = time.time() + secs
        while time.time() < end:
            if cond():
                return
            time.sleep(0.05)
        self.fail("condition never became true")

    def holder(self):
        try:
            return json.loads((self.dir / "suite.lock.holder").read_text())
        except (OSError, ValueError):
            return {}

    def start_hanging_holder(self):
        self.child_pid.unlink(missing_ok=True)
        a = self.start(cmd=self.hanging_suite())
        self.wait_for(lambda: self.child_pid.exists()
                      and str(self.holder().get("child_pid")) == self.child_pid.read_text())
        return a

    # scenarios shared by the unit tests and the intent replay
    def scenario_second_waits(self, child_secs):
        a = self.start(cmd=self.logging_suite("A", child_secs))
        self.wait_for(lambda: self.log.exists() and "A start" in self.log.read_text())
        code, out = self.run_suite(cmd=self.logging_suite("B", 0.2))
        a.communicate(timeout=30)
        return code, "waiting for suite lock" in out, self.log.read_text().splitlines()

    def scenario_timeout(self, timeout, sleeps):
        code, out = self.run_suite("--timeout", str(timeout), cmd=self.hanging_suite(sleeps))
        grand = int(self.grand_pid.read_text())
        self.wait_for(lambda: not pid_alive(grand), 5)
        return code, out

    def scenario_wrapper_killed(self, signame):
        a = self.start_hanging_holder()
        os.kill(a.pid, signal.Signals["SIG" + signame])
        a.wait(timeout=10)
        return self.run_suite("--max-wait", "1", cmd=[PY, "-c", "pass"], timeout=15)


class SerializeTests(SuiteBase):
    def test_two_invocations_serialize(self):
        a = self.start(cmd=self.logging_suite("A", 1.5))
        self.wait_for(lambda: self.log.exists() and "A start" in self.log.read_text())
        b = self.start(cmd=self.logging_suite("B", 0.2))
        out_b, _ = b.communicate(timeout=30)
        a.communicate(timeout=30)
        self.assertEqual((a.returncode, b.returncode), (0, 0))
        self.assertEqual(self.log.read_text().splitlines(), ORDER)
        self.assertIn("waiting for suite lock (holder pid", out_b)
        self.assertEqual(out_b.count("waiting for suite lock"), 1)

    def test_sigterm_on_wrapper_kills_the_suite_and_frees_the_lock(self):
        a = self.start_hanging_holder()
        child = int(self.child_pid.read_text())
        a.send_signal(signal.SIGTERM)
        a.wait(timeout=10)
        self.assertEqual(a.returncode, 128 + signal.SIGTERM)
        self.wait_for(lambda: not pid_alive(child), 5)
        code, out = self.run_suite(cmd=[PY, "-c", "print('ok')"], timeout=10)
        self.assertEqual(code, 0, out)
        self.assertNotIn("waiting for suite lock", out)

    def test_sigkilled_wrapper_leaves_the_orphan_suite_holding_the_lock(self):
        code, out = self.scenario_wrapper_killed("KILL")
        self.assertEqual(code, 75, out)
        child = int(self.child_pid.read_text())
        self.assertTrue(pid_alive(child))
        os.kill(child, signal.SIGKILL)
        self.wait_for(lambda: not pid_alive(child), 5)
        code, out = self.run_suite(cmd=[PY, "-c", "print('ok')"], timeout=10)
        self.assertEqual(code, 0, out)
        self.assertNotIn("waiting for suite lock", out)

    def test_sidecar_names_the_child_pid(self):
        self.start_hanging_holder()
        self.assertEqual(self.holder()["child_pid"], int(self.child_pid.read_text()))

    def test_maintenance_lock_is_separate(self):
        self.start(cmd=self.logging_suite("A", 3))
        self.wait_for(self.log.exists)
        code, out = self.run_suite("--maintenance", cmd=[PY, "-c", "print('m')"], timeout=2.5)
        self.assertEqual(code, 0, out)
        self.assertNotIn("waiting", out)
        self.log.unlink()
        self.start("--maintenance", cmd=self.logging_suite("M", 1.5))
        self.wait_for(self.log.exists)
        out2 = self.run_suite("--maintenance", cmd=[PY, "-c", "pass"])[1]
        self.assertIn("waiting for suite lock", out2)


class OutcomeTests(SuiteBase):
    def test_exit_code_passthrough(self):
        code, _ = self.run_suite(cmd=[PY, "-c", "import sys; sys.exit(3)"])
        self.assertEqual(code, 3)

    def test_child_killed_by_signal_exits_128_plus_n(self):
        code, _ = self.run_suite(cmd=[PY, "-c", "import os,signal; os.kill(os.getpid(), 15)"])
        self.assertEqual(code, 128 + signal.SIGTERM)

    def test_timeout_kills_group_exits_124_and_dumps_the_hung_test(self):
        code, out = self.scenario_timeout(1, 60)
        self.assertEqual(code, 124, out)
        self.assertIn("in test_hang", out)
        self.assertIn("most recent call first", out)
        self.assertNotIn("Abort", out)

    def test_hook_is_silent_without_a_real_sitecustomize_and_chains_to_one(self):
        hook = str(SCRIPT.parent / "suite_hook")
        empty = self.dir / "empty"
        real = self.dir / "real"
        empty.mkdir()
        real.mkdir()
        mark = self.dir / "mark.txt"
        (real / "sitecustomize.py").write_text(f"open({str(mark)!r}, 'a').write('x')\n")
        for extra, runs in ((empty, 0), (real, 1)):
            # hide every other sitecustomize (e.g. Homebrew's) so the "none found" branch runs
            boot = ("import os, sys, importlib.util, faulthandler, signal, site\n"
                    f"keep = {[hook, str(extra)]!r}\n"
                    "sys.path[:] = [p for p in sys.path if p in keep"
                    " or not os.path.exists(os.path.join(p, 'sitecustomize.py'))]\n"
                    "site.main()")
            mark.unlink(missing_ok=True)
            r = subprocess.run([PY, "-S", "-c", boot], capture_output=True, text=True,
                               env=dict(os.environ, PYTHONPATH=f"{hook}:{extra}"))
            self.assertEqual(r.stderr, "")
            self.assertEqual(len(mark.read_text()) if mark.exists() else 0, runs)

    def test_max_wait_exits_75(self):
        self.start(cmd=self.logging_suite("A", 20))
        self.wait_for(self.log.exists)
        t0 = time.time()
        code, out = self.run_suite("--max-wait", "1", cmd=[PY, "-c", "pass"], timeout=15)
        self.assertEqual(code, 75, out)
        self.assertLess(time.time() - t0, 10)
        self.assertIn("gave up waiting for suite lock", out)

    def test_max_wait_zero_fails_fast(self):
        self.start(cmd=self.logging_suite("A", 20))
        self.wait_for(self.log.exists)
        code, out = self.run_suite("--max-wait", "0", cmd=[PY, "-c", "pass"], timeout=5)
        self.assertEqual(code, 75, out)
        self.assertNotIn("waiting for suite lock", out)

    def test_default_cwd_is_repo_root(self):
        subprocess.run(["git", "init", "-q", str(self.dir)], check=True)
        sub = self.dir / "a" / "b"
        sub.mkdir(parents=True)
        code, out = self.run_suite(cmd=[PY, "-c", "import os; print(os.getcwd())"], cwd=sub)
        self.assertEqual(code, 0)
        self.assertEqual(os.path.realpath(out.strip().splitlines()[-1]), os.path.realpath(self.dir))


def src(text):
    return textwrap.dedent(text).lstrip("\n")


OK2 = src("""
    import unittest

    class T(unittest.TestCase):
        def test_a(self): pass
        def test_b(self): pass
""")
STAMP = src("""
    import os, time, unittest
    LOG = os.path.join(os.path.dirname(__file__), 'log.txt')

    class T(unittest.TestCase):
        def test_a(self):
            open(LOG, 'a').write(f'{__name__} start {time.time()}\\n')
            time.sleep(SECS)
            open(LOG, 'a').write(f'{__name__} end {time.time()}\\n')
""")
HANG = src("""
    import os, time, unittest

    class T(unittest.TestCase):
        def test_hang(self):
            open(PIDFILE, 'w').write(str(os.getpid()))
            time.sleep(60)
""")
SKIP = src("""
    import unittest

    def setUpModule():
        raise unittest.SkipTest('x')

    class T(unittest.TestCase):
        def test_a(self): pass
""")
GEN = src("""
    import os, unittest
    p = os.path.join(os.path.dirname(__file__), 'n')
    n = int(open(p).read()) if os.path.exists(p) else 0
    open(p, 'w').write(str(n + 1))

    class T(unittest.TestCase):
        pass

    setattr(T, f'test_{n}', lambda self: None)
""")
STOP = src("""
    import unittest

    class T(unittest.TestCase):
        def run(self, result=None):
            result.stop()
        def test_b(self): pass
        def test_c(self): pass
""")
CUSTOM = src("""
    import unittest

    class S(unittest.TestSuite):
        def run(self, result, **kw): pass

    class T(unittest.TestCase):
        def test_a(self): pass

    def load_tests(loader, tests, pattern):
        return S(tests)
""")
DIE = src("""
    import os, unittest

    class T(unittest.TestCase):
        def test_a(self):
            print('dying now', flush=True)
            os._exit(0)
""")


class ShardTests(SuiteBase):
    """--shards N against stand-in modules in a temp repo; plain discovery is the oracle."""

    def setUp(self):
        super().setUp()
        self.tests = self.dir / "capsule" / "tests"
        self.tests.mkdir(parents=True)

    def module(self, name, text=OK2):
        (self.tests / f"test_{name}.py").write_text(text)

    def shards(self, n="3", *args, timeout=60):
        code, out = self.run_suite("--shards", n, *args, cwd=self.dir, timeout=timeout)
        return code, out, [x for x in out.splitlines() if x.strip()]

    def plain(self):
        r = subprocess.run([PY, "-m", "unittest", "discover", "-s", "capsule/tests", "-q"],
                           cwd=self.dir, capture_output=True, text=True)
        return r.returncode, r.stderr

    def test_the_total_is_what_plain_discovery_finds(self):
        for n in "abcde":
            self.module(n)
        code, out, lines = self.shards()
        plain_code, plain_out = self.plain()
        self.assertEqual((code, plain_code, lines[-1]), (0, 0, "OK"), out)
        self.assertIn("Ran 10 tests in", lines[-2])
        self.assertIn("Ran 10 tests in", plain_out)
        self.assertIn("10 tests in 5 modules: 3 parallel shards, 0 serial modules", out)

    def test_a_failure_and_an_import_error_in_other_shards_fail_the_run(self):
        self.module("ok")
        self.module("fail", OK2 + "    def test_f(self): self.assertEqual(1, 2)\n")
        self.module("bad", "import nonexistent_zz\n")
        code, out, lines = self.shards("3")
        self.assertEqual(code, 1, out)
        self.assertTrue(lines[-1].startswith("FAILED (failures=1, errors=1"), out)
        for text in ("test_f", "_FailedTest.test_bad", "ran 1 modules: test_fail", "replay it"):
            self.assertIn(text, out)

    def test_empty_discovery_is_5_like_plain(self):
        code, out, lines = self.shards()
        self.assertEqual((code, self.plain()[0], lines[-1]), (5, 5, "NO TESTS RAN"), out)

    def test_a_timeout_ends_with_a_timed_out_line_never_ok_and_leaves_no_shard(self):
        self.module("ok")
        pid = self.dir / "hang.pid"
        self.module("hang", HANG.replace("PIDFILE", repr(str(pid))))
        code, out, lines = self.shards("2", "--timeout", "3")
        self.assertEqual(code, 124, out)
        self.assertEqual(lines[-1], "capsule-suite: TIMED OUT after 3s")
        self.assertIn("in test_hang", out)
        self.wait_for(lambda: not pid_alive(int(pid.read_text())), 5)

    def test_a_timeout_with_busy_non_hung_shards_still_exits_124(self):
        for n in "abcd":
            self.module(n, STAMP.replace("SECS", "2"))
        code, out, lines = self.shards("2", "--timeout", "3")
        self.assertEqual((code, lines[-1]), (124, "capsule-suite: TIMED OUT after 3s"), out)
        self.assertNotIn("Traceback", out)

    def test_stopping_shards_survives_eperm_from_a_zombie_group(self):
        procs = [subprocess.Popen(["sleep", "0.5"]) for _ in range(2)]
        ctx = types.SimpleNamespace(shards=[types.SimpleNamespace(proc=p) for p in procs])
        err = PermissionError(1, "Operation not permitted")
        with mock.patch.object(os, "kill", side_effect=err), \
                mock.patch.object(os, "killpg", side_effect=err):
            sharded._stop_all(ctx, True)
        self.assertEqual([p.poll() for p in procs], [0, 0])

    def test_a_setup_module_skip_is_accounted_for(self):
        self.module("ok")
        self.module("skip", SKIP)
        code, out, lines = self.shards("2")
        self.assertEqual((code, lines[-1]), (0, "OK (skipped=1)"), out)

    def test_other_ids_with_the_same_count_fail(self):
        self.module("gen", GEN)
        code, out, lines = self.shards("2")
        self.assertEqual(code, 1, out)
        self.assertTrue(lines[-1].startswith("FAILED"), out)
        self.assertIn("listed tests never ran", out)
        self.assertIn("tests ran that discovery did not list", out)

    def test_a_stopped_shard_with_tests_unrun_fails(self):
        self.module("stop", STOP)
        code, out, _ = self.shards("2")
        self.assertEqual(code, 1, out)
        self.assertIn("was stopped early", out)
        self.assertIn("listed tests never ran", out)

    def test_a_suite_that_is_not_a_plain_testsuite_is_refused(self):
        self.module("custom", CUSTOM)
        code, out, lines = self.shards("2")
        self.assertEqual(code, 1, out)
        self.assertIn("is not a plain TestSuite", out)
        self.assertEqual(lines[-1], "capsule-suite: discovery failed, nothing ran")

    def test_a_shard_that_exits_0_without_a_result_shows_its_log(self):
        self.module("ok")
        self.module("die", DIE)
        code, out, lines = self.shards("2")
        self.assertEqual(code, 1, out)
        self.assertIn("exit 0 and no result", out)
        self.assertIn("dying now", out)
        self.assertTrue(lines[-1].startswith("FAILED"), out)

    def test_a_serial_module_runs_alone(self):
        name = sharded.SERIAL[0]
        for n in ("p1", "p2"):
            self.module(n, STAMP.replace("SECS", "1.5"))
        self.module(name[5:], STAMP.replace("SECS", "1"))
        code, out, _ = self.shards("3")
        self.assertEqual(code, 0, out)
        self.assertIn("1 serial modules", out)
        times = {}
        for line in (self.tests / "log.txt").read_text().splitlines():
            mod, edge, stamp = line.split()
            times[mod, edge] = float(stamp)
        for p in ("test_p1", "test_p2"):
            self.assertLessEqual(times[p, "end"], times[name, "start"])
        self.assertLess(abs(times["test_p1", "start"] - times["test_p2", "start"]), 1.0)

    def test_bad_stored_seconds_are_dropped_and_healed(self):
        for n in "abc":
            self.module(n)
        (self.dir / "suite-shards.json").write_text(
            '{"test_a": "x", "test_b": null, "test_c": [1], "test_d": 1' + "0" * 400
            + ', "test_e": NaN}')
        code, out, _ = self.shards("2")
        self.assertEqual(code, 0, out)
        healed = json.loads((self.dir / "suite-shards.json").read_text())
        self.assertEqual(set(healed), {"test_a", "test_b", "test_c"})
        self.assertTrue(all(isinstance(v, float) and 0 <= v < 1e6 for v in healed.values()))

    def test_stored_seconds_of_any_other_shape_do_not_crash_the_run(self):
        self.module("a")
        for text in ('{"test_a": {"x": 1}, "test_b": true, "test_c": -5, "test_d": 1e999}',
                     '[1, 2]', '"x"', 'null', '{', ''):
            (self.dir / "suite-shards.json").write_text(text)
            code, out, lines = self.shards("2")
            self.assertEqual((code, lines[-1]), (0, "OK"), (text, out))
            self.assertEqual(set(json.loads((self.dir / "suite-shards.json").read_text())),
                             {"test_a"})

    def test_an_orphan_worker_exits_by_itself_at_its_deadline(self):
        self.module("hang", HANG.replace("PIDFILE", repr(str(self.dir / "hang.pid"))))
        t0 = time.time()
        r = subprocess.run([PY, str(SCRIPT.parent / "suite_hook" / "shard.py"), "run",
                            "capsule/tests", os.devnull, "2", "test_hang"], cwd=self.dir,
                           capture_output=True, text=True, timeout=30)
        self.assertNotEqual(r.returncode, 0)
        self.assertLess(time.time() - t0, 20)
        self.assertIn("most recent call first", r.stderr)

    def test_bad_shard_values_are_refused_before_anything_runs(self):
        self.module("ok")
        for bad in ("0", "-1", "abc", "2.5", ""):
            code, out = self.run_suite("--shards", bad, cwd=self.dir)
            self.assertEqual(code, 2, (bad, out))
        code, out = self.run_suite("--shards", "2", cmd=["true"], cwd=self.dir)
        self.assertEqual(code, 2, out)
        self.assertFalse((self.dir / "suite.lock").exists())

    def test_one_shard_runs_as_plain(self):
        self.module("ok")
        code, out, lines = self.shards("1")
        self.assertEqual((code, lines[-1], "shard" in out), (0, "OK", False), out)

    def test_every_serial_name_is_a_module_on_disk(self):
        root = SCRIPT.parent.parent
        for names, folder in ((sharded.SERIAL, "capsule/tests"),
                              (sharded.SERIAL_MAINTENANCE, "maintenance/tests")):
            for name in names:
                self.assertTrue((root / folder / f"{name}.py").is_file(), f"{folder}/{name}.py")


class IntentExamples(SuiteBase):
    """Z1: contracts/intent/capsule_suite.jsonl replayed through the real script."""

    def test_examples(self):
        rows = [json.loads(line) for line in INTENT.read_text().splitlines() if line.strip()]
        self.assertEqual({r["id"] for r in rows}, INTENT_IDS)
        for row in rows:
            doc, want = row["given"]["document"], row["expect"]
            with self.subTest(row["id"]):
                if doc["case"] == "second-waits":
                    code, waited, order = self.scenario_second_waits(doc["child_secs"])
                    self.assertEqual((code, waited, order),
                                     (want["exit"], want["waited"], want["order"]))
                elif doc["case"] == "timeout":
                    code, out = self.scenario_timeout(doc["timeout"], doc["child_sleeps"])
                    grand = int(self.grand_pid.read_text())
                    self.assertEqual((code, pid_alive(grand), want["dump_names"] in out),
                                     (want["exit"], want["grandchild_alive"], True))
                elif doc["case"] == "wrapper-killed":
                    code, _ = self.scenario_wrapper_killed(doc["signal"])
                    self.assertEqual(code, want["next_run_exit"])
                else:
                    self.fail(f"unknown case {doc['case']}")


if __name__ == "__main__":
    unittest.main()
