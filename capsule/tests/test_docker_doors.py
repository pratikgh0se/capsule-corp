"""Card rq_5b644c43: every docker door goes through events.docker_run, so under unittest none of
them can reach the live docker (review F3 of capsule/tests-no-live-pg)."""
import ast
import functools
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "maintenance"))
import board_bridge  # noqa: E402
import db_migrate  # noqa: E402
import events  # noqa: E402
import lookout_close_stale  # noqa: E402
import merge_gate  # noqa: E402
import obs_probe  # noqa: E402

from capsule import boundary, lookout_ship, paths, preflight  # noqa: E402
from capsule.runners import docker  # noqa: E402

# Files allowed to hold a docker argv of their own, each with the reason.
ALLOWED = {
    "capsule/lander.py": "protected path; reported on the card",
    "maintenance/merge_gate.py": "an injected runner in a frozen file; reported on the card",
    "maintenance/heal_table.py": "a list of heal step names, not an argv",
    "capsule/runners/exec_mcp.py": "its own process, never loaded under unittest",
}
# Scans whole file text, so a line break after the bracket cannot dodge it.
ARGV = re.compile(r"""(?:\[|(?<![\w.])\()\s*(?:["']docker["']|(?:self|cfg)\.docker\b)""")
# A shell command string that starts with docker, as a call argument or a cmd assignment.
SHELL = re.compile(r"""(?:\b(?:system|popen|run|call|check_output|check_call|Popen|getoutput)\(|
                         \b(?:cmd|command)\s*=)\s*f?["']docker\s""", re.X)
# Files whose argv is only built, never run here: the one function allowed to hold it.
BUILT = {"capsule/runners/docker.py": "run_args"}   # start() strips argv[0] and uses docker_run


def stray_hits(rel, text):
    """`file:line` of each docker argv or shell string outside the file's BUILT function."""
    span = range(0)
    for node in ast.walk(ast.parse(text)):
        if isinstance(node, ast.FunctionDef) and node.name == BUILT.get(rel):
            span = range(node.lineno, node.end_lineno + 1)
    hits = [m for rx in (ARGV, SHELL) for m in rx.finditer(text)]
    lines = sorted(text.count("\n", 0, m.start()) + 1 for m in hits)
    return [f"{rel}:{n}" for n in lines if n not in span]


class NoDirectDockerArgv(unittest.TestCase):
    def test_no_docker_argv_outside_the_guarded_helper(self):
        files = [*ROOT.glob("capsule/*.py"), *ROOT.glob("capsule/runners/*.py"),
                 *ROOT.glob("maintenance/*.py")]
        hits = []
        for f in files:
            rel = f.relative_to(ROOT).as_posix()
            if rel not in ALLOWED:
                hits += stray_hits(rel, f.read_text())
        self.assertEqual(hits, [])

    def test_the_scan_sees_a_shell_string_that_starts_with_docker(self):
        for text in ('subprocess.run("docker ps", shell=True)', "os.system('docker rm x')",
                     'cmd = f"docker exec {n}"', 'check_output(\n  "docker info", shell=True)'):
            self.assertTrue(SHELL.search(text), text)
        for text in ('print("docker is down")', 'run("dockerfile lint")', 'x = "docker ps"'):
            self.assertFalse(SHELL.search(text), text)

    def test_built_allowance_is_the_named_function_only(self):
        body = 'def run_args():\n    return ["docker", "run"]\n'
        other = 'def start():\n    return ["docker", "ps"]\n'
        rel = "capsule/runners/docker.py"
        self.assertEqual(stray_hits(rel, body), [])
        self.assertEqual(stray_hits(rel, body + other), [f"{rel}:4"])
        self.assertEqual(stray_hits(rel, other + body.replace("run_args", "renamed")),
                         [f"{rel}:2", f"{rel}:4"])

    def test_the_scan_sees_a_direct_argv(self):
        # second oracle: the pattern matches the shapes it is meant to forbid
        for text in ('x = ["docker", "ps"]', "run(['docker', 'info'])", "r([self.docker, 'rm'])",
                     'return [\n        "docker", "run"]', 'x = (\n    "docker", "ps")'):
            self.assertTrue(ARGV.search(text), text)
        self.assertFalse(ARGV.search('x = ["dockerfile", "ps"]'))


class EveryDoorRefusesLiveDocker(unittest.TestCase):
    def setUp(self):
        self.real = Path(tempfile.mkdtemp())
        self.marker = self.real / "ran"
        (self.real / "docker").write_text(f"#!/bin/sh\ntouch {self.marker}\nexit 0\n")
        (self.real / "docker").chmod(0o755)
        link = Path(tempfile.mkdtemp())
        (link / "docker").symlink_to(self.real / "docker")
        env = {"PATH": f"{self.real}:/usr/bin:/bin", "AGENT_OPS_DOCKER_BIN": str(link),
               "AGENT_OPS_NO_SEND": "1", "HOME": str(self.real)}
        patcher = unittest.mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def refused(self, call):
        with self.assertRaisesRegex(RuntimeError, "live ops-pg"):
            call()
        self.assertFalse(self.marker.exists(), "the live docker ran")

    def test_lookout_ship_psql(self):
        self.refused(lambda: lookout_ship.psql("select 1"))

    def test_lookout_close_stale(self):
        self.refused(lambda: lookout_close_stale._psql("select 1"))

    def test_obs_probe_collectors_raise_and_ship_reports_failure(self):
        self.refused(lambda: obs_probe.collect_postgres(subprocess.run))
        self.refused(lambda: obs_probe.collect_power(subprocess.run, self.real))
        self.refused(lambda: obs_probe.collect_docker(subprocess.run))
        self.refused(lambda: obs_probe.collect_sandbox(subprocess.run))
        self.assertFalse(obs_probe.ship("x", subprocess.run).ok)    # swallows the refusal
        self.assertFalse(self.marker.exists(), "the live docker ran")

    def test_db_migrate_reports_the_refusal_and_never_runs_docker(self):
        rc, out = db_migrate._default_psql("select 1")
        self.assertEqual(rc, 1)
        self.assertIn("live ops-pg", out)
        self.assertFalse(self.marker.exists())

    def test_strong_runner_never_runs_docker(self):
        self.assertFalse(docker.StrongRunner().available())     # swallows the refusal
        self.assertFalse(self.marker.exists())
        self.refused(lambda: docker.StrongRunner().reap([]))

    def test_boundary_gap_check_refuses(self):
        db = paths.board_db("b", self.real)
        db.parent.mkdir(parents=True)
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE task_runs (ended_at)")
        conn.commit()
        conn.close()
        self.refused(lambda: boundary._gap("b", self.real))

    def test_preflight_never_runs_docker(self):
        cfg = json.loads((paths.REPO / "capsule" / "config.example.json").read_text())
        cfg["repo_url"] = str(self.real)
        (self.real / "config.json").write_text(json.dumps(cfg))
        levels = {name: lvl for lvl, name, _ in preflight.run(self.real)}
        self.assertEqual(levels["worker_image"], "FAIL")        # the refusal reads as "not present"
        self.assertFalse(self.marker.exists(), "the live docker ran")

    def test_board_bridge_health_probe_never_runs_docker(self):
        self.assertFalse(board_bridge._docker_ok())
        self.assertFalse(self.marker.exists())

    def test_the_helper_lets_an_injected_run_through(self):
        # a test's own fake run is not a door to docker, so it needs no stub dir
        seen = []
        events.docker_run(["info"], lambda argv, **kw: seen.append(argv))
        self.assertEqual(seen, [["docker", "info"]])


    # rq_e5fabacf: a wrapper around the real run, an absolute docker and merge_gate's default.
    def fake_dir(self):
        good = Path(tempfile.mkdtemp())
        (good / "docker").write_text(f"#!/bin/sh\ntouch {self.marker}\necho fake\n")
        (good / "docker").chmod(0o755)
        return good

    def test_a_wrapper_around_the_real_run_is_refused_even_with_a_genuine_fake(self):
        def wrapped(*a, **k):
            return subprocess.run(*a, **k)
        env = {"PATH": "/usr/bin:/bin", "AGENT_OPS_DOCKER_BIN": str(self.fake_dir())}
        with unittest.mock.patch.dict(os.environ, env):
            for run in (wrapped, functools.partial(subprocess.run), subprocess.run):
                self.refused(lambda: events.docker_run(["info"], run, capture_output=True))

    def test_a_wrapper_that_drops_env_and_finds_the_live_docker_on_path_is_refused(self):
        def bare(argv, **kw):
            return subprocess.run(argv)
        self.refused(lambda: events.docker_run(["info"], bare))

    def test_a_bytes_argv_and_a_shell_string_are_refused(self):
        live = str(self.real / "docker")
        for call in (lambda a, **k: subprocess.run(os.fsencode(live)),
                     lambda a, **k: subprocess.run("cd / && docker info", shell=True),
                     lambda a, **k: subprocess.run(f"echo x; {live} info", shell=True)):
            self.refused(lambda: events.docker_run(["info"], call))

    def test_a_non_docker_exec_through_an_injected_run_is_allowed(self):
        def echo(argv, **kw):
            return subprocess.run(["echo", "hi"], capture_output=True)
        self.assertEqual(events.docker_run(["x"], echo).returncode, 0)

    def test_a_tests_own_popen_mock_is_what_the_injected_run_reaches(self):
        with unittest.mock.patch("subprocess.Popen") as popen:
            events.docker_run(["x"], lambda a, **k: subprocess.Popen(["git", "status"]))
        self.assertEqual(popen.call_count, 1)

    def test_popen_is_restored_after_the_injected_run_raises(self):
        before = subprocess.Popen
        with self.assertRaises(ZeroDivisionError):
            events.docker_run(["x"], lambda a, **k: 1 / 0)
        self.assertIs(subprocess.Popen, before)

    def test_a_genuine_fake_runs_through_the_default_run(self):
        env = {"PATH": "/usr/bin:/bin", "AGENT_OPS_DOCKER_BIN": str(self.fake_dir())}
        with unittest.mock.patch.dict(os.environ, env):
            r = events.docker_run(["info"], capture_output=True, text=True)
            absolute = events.docker_run(["info"], docker=env["AGENT_OPS_DOCKER_BIN"] + "/docker",
                                         capture_output=True, text=True)
        self.assertEqual((r.stdout, absolute.stdout), ("fake\n", "fake\n"))

    def test_an_absolute_docker_that_is_the_live_one_is_refused(self):
        good = Path(tempfile.mkdtemp())
        (good / "docker").write_text("#!/bin/sh\nexit 0\n")      # a fine fake, but not the argument
        (good / "docker").chmod(0o755)
        with unittest.mock.patch.dict(os.environ, {"AGENT_OPS_DOCKER_BIN": str(good)}):
            self.refused(lambda: events.docker_run(["info"], docker=str(self.real / "docker")))
            self.refused(lambda: events.docker_run(["info"], subprocess.run,
                                                   docker=str(self.real / "docker")))

    def test_an_absolute_docker_that_is_not_live_runs(self):
        fake = Path(tempfile.mkdtemp()) / "docker"
        fake.write_text("#!/bin/sh\necho fake\n")
        fake.chmod(0o755)
        r = events.docker_run(["info"], docker=str(fake), capture_output=True, text=True)
        self.assertEqual(r.stdout, "fake\n")

    def test_merge_gate_default_runner_never_starts_a_container(self):
        self.refused(lambda: merge_gate._default_sh(["docker", "run", "--rm", "img"]))
        self.assertEqual(merge_gate._default_sh(["true"]), (0, ""))


class WithoutUnittest(unittest.TestCase):
    def test_the_door_never_raises_and_puts_docker_bin_first(self):
        code = ("import sys; sys.path.insert(0, 'maintenance'); import events\n"
                "print('unittest' in sys.modules, "
                "events.docker_env()['PATH'].startswith(events.DOCKER_BIN + ':'))")
        r = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env={"PATH": "/usr/bin:/bin"},
                           text=True, capture_output=True, timeout=60)
        self.assertEqual(r.stdout.split(), ["False", "True"], r.stderr)

    def test_a_wrapper_an_absolute_docker_and_merge_gate_just_run(self):
        fake = Path(tempfile.mkdtemp())
        (fake / "docker").write_text("#!/bin/sh\necho ran-$1\n")
        (fake / "docker").chmod(0o755)
        code = ("import sys, subprocess; sys.path.insert(0, 'maintenance')\n"
                "import events, merge_gate\n"
                "w = lambda *a, **k: subprocess.run(*a, **k)\n"
                "kw = dict(capture_output=True, text=True)\n"
                "print(events.docker_run(['a'], w, **kw).stdout.strip())\n"
                f"print(events.docker_run(['b'], docker='{fake}/docker', **kw).stdout.strip())\n"
                "print(merge_gate._default_sh(['docker', 'c']), 'unittest' in sys.modules)")
        r = subprocess.run([sys.executable, "-c", code], cwd=ROOT, text=True, capture_output=True,
                           env={"PATH": "/usr/bin:/bin", "AGENT_OPS_DOCKER_BIN": str(fake),
                                "AGENT_OPS_NO_SEND": "1", "HOME": str(fake)}, timeout=60)
        self.assertEqual(r.stdout.splitlines(),
                         ["ran-a", "ran-b", "(0, 'ran-c\\n') False"], r.stderr)


if __name__ == "__main__":
    unittest.main()
