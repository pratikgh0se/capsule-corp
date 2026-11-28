# mutation-check: targets-predate-range 0b4fd92f -- fake :: F.test_known, F.test_no_ws, F.test_noctr
"""Row 632 b: a candidate's own code (the board's suites, the mutation gate's tests, the suites
re-run after a push) runs in the locked-down worker container, never on the host. The lander's real
`sandbox.session` runs here over a fake docker CLI that records every argv and runs nothing, so
oracle 1 is the verdict and oracle 2 is the recorded docker argv and a marker file the candidate's
command would have written on the host. One real-docker smoke test runs only with
RUN_DOCKER_TESTS=1 (and in a child process: the unittest docker door refuses a real docker).
Also replays contracts/intent/lander_sandbox.jsonl (Z1)."""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from capsule import db, intent, landing, lander, paths
from capsule.runners import docker, sandbox
from capsule.tests import host_box, test_landing
from capsule.tests.test_lander import World, git

NOW = test_landing.NOW
DAY = 86400
MOUNTS = '[{"Mounts": [{"Source": "/w"}]}]'   # a clean container's `docker inspect`


class FakeDocker:
    """A stand-in for the docker CLI behind `StrongRunner._run`: records, runs nothing."""

    def __init__(self):
        self.calls, self.up, self.start_fails, self.exec_rc, self.hang = [], True, False, 0, False
        self.alive, self.rm_rc, self.trusted_listing = True, 0, None
        self.daemon_dies, self.probe_fails, self.gone, self.exec_rcs = False, 0, False, []

    def __call__(self, argv, **kw):
        if argv[1] == "exec" and argv[-1] == docker._mount_probe(None):
            missing = self.probe_fails > 0       # start()'s probe: no command; fails while > 0
            self.probe_fails -= missing
            return subprocess.CompletedProcess(argv, int(missing), "", "")
        self.calls.append(list(argv))
        if argv[1] in ("run", "rm"):
            self.gone = argv[1] == "rm"          # what `inspect` reports about the container
        verb = argv[1]
        if verb == "exec" and self.daemon_dies:      # the daemon goes down with the container
            self.up = self.alive = False
        if verb == "rm" and self.rm_rc is None:      # `docker rm` times out
            raise subprocess.TimeoutExpired(argv, kw.get("timeout"))
        if verb == "version" and not self.up:
            return subprocess.CompletedProcess(argv, 1, "", "daemon down")
        if verb == "run":
            src = next(a for a in argv if a.endswith(":/trusted:ro")).split(":")[0]
            self.trusted_listing = sorted(os.listdir(src))
            if self.start_fails:
                raise subprocess.CalledProcessError(1, argv, "", "Unable to find image")
        if verb == "exec" and self.hang:
            raise subprocess.TimeoutExpired(argv, kw.get("timeout"))
        if verb == "inspect":
            running = "true\n" if self.alive else "false\n"
            out = running if "-f" in argv else MOUNTS   # R33 audit: the mount list
            return subprocess.CompletedProcess(argv, int(self.gone), out, "")
        rc = {"exec": self.exec_rc, "rm": self.rm_rc}.get(verb, 0)
        if verb == "exec" and self.exec_rcs:         # a script of exit codes, one per exec
            rc = self.exec_rcs.pop(0)
        return subprocess.CompletedProcess(argv, rc, "", "")

    def verb(self, name):
        return [c for c in self.calls if c[1] == name]

    def commands(self):
        """Each exec's command, without the heal every exec starts with and the session's PREP."""
        return [c[-1].removeprefix(docker.TREE_LISTING).removeprefix(sandbox.PREP)
                for c in self.verb("exec")]

    def mount(self, suffix):
        return next(a for a in self.verb("run")[0] if a.endswith(suffix)).split(":")[0]

    def name(self):
        run = self.verb("run")[0]
        return run[run.index("--name") + 1]


class Boxed(World):
    """The real sandbox over the fake docker; the lander fixtures from test_lander."""

    def setUp(self):
        super().setUp()
        self.fake = FakeDocker()
        real = docker.StrongRunner

        def make(**kw):
            runner = real(**kw)
            runner._run, runner._sleep = self.fake, [].append    # no real backoff sleeps
            return runner

        for patch in (mock.patch.object(sandbox, "session", sandbox.REAL_SESSION),
                      mock.patch.object(docker, "StrongRunner", make),
                      mock.patch.dict(os.environ, {"OPS_WORKER_IMAGE": "img:test",
                                                   "HOST_SECRET_TOKEN": "s3cret"})):
            patch.start()
            self.addCleanup(patch.stop)
        self.put("a.txt", "work\n", "card work")
        self.marker = self.dir / "host-marker"

    def flat(self):
        return " ".join(" ".join(c) for c in self.fake.calls)


class CandidateTestsRunInTheContainer(Boxed):
    def test_a_suite_that_writes_to_the_host_leaves_no_marker_and_ran_in_the_container(self):
        self.cfg["contract_commands"] = [f"echo owned > {self.marker}; echo owned > ~/marker"]
        result = self.check()
        self.assertEqual(result["verdict"], "checked")
        self.assertFalse(self.marker.exists())
        self.assertFalse((Path.home() / "marker").exists())
        self.assertEqual(self.fake.commands()[-1], self.cfg["contract_commands"][0])
        self.assertEqual(len(self.fake.verb("run")), 1)

    def test_the_container_is_locked_down_and_inherits_nothing_from_the_host(self):
        self.check()
        run = self.fake.verb("run")[0]
        flat = " ".join(run)
        for want in ("--network=none", "--cap-drop ALL", "no-new-privileges", "--user", "--rm",
                     "--pids-limit 256", f"--cpus {sandbox.CPUS}"):
            self.assertIn(want, flat)
        envs = [run[i + 1] for i, a in enumerate(run) if a == "-e"]
        self.assertEqual(sorted(envs), sorted(["TMPDIR=/scratch", "HOME=/tmp", *sandbox.ENV]))
        self.assertNotIn("s3cret", flat)
        self.assertNotIn("docker.sock", flat)
        self.assertNotIn("--label capsule-board", flat)   # the tick's reaper never touches it

    def test_only_the_extracted_trusted_tests_tree_is_mounted_never_the_checkout(self):
        self.check()
        mounted = self.fake.mount(":/trusted:ro")
        self.assertIn("lander-box-", mounted)
        self.assertNotEqual(mounted, str(paths.REPO))
        self.assertEqual(self.fake.trusted_listing, ["tests"])
        self.assertNotIn(f"{paths.REPO}:", self.flat())

    def test_the_container_gets_a_private_copy_never_the_clone_the_host_runs_git_in(self):
        self.check()
        src = self.fake.mount(":/workspace")
        self.assertIn("lander-box-", src)
        self.assertNotIn(str(self.repo), src)

    def test_the_container_and_its_copy_are_gone_when_the_suite_is_red_and_rm_fails(self):
        self.fake.exec_rc, self.fake.rm_rc = 1, 1
        self.assertEqual(self.check()["verdict"], "red")
        self.assertEqual(self.fake.verb("rm"), [["docker", "rm", "-f", self.fake.name()]])
        self.assertFalse(Path(self.fake.mount(":/workspace")).exists())

    def test_a_timeout_is_a_red_check_not_a_hang(self):
        self.fake.hang = True
        result = self.check(timeout=5)
        self.assertEqual(result["verdict"], "red")
        self.assertIn("timed out after 5s", self.detail(result, "gates"))

    def test_the_container_lifetime_grows_with_the_commands_it_must_outlast(self):
        def lifetime(suites):
            self.fake.calls.clear()
            self.cfg["contract_commands"] = [f"echo s{n}" for n in range(suites)]
            self.check(timeout=100)
            return int(self.fake.verb("run")[0][-1])
        gates = len(self.cfg["lander"]["gates"])
        self.assertEqual(lifetime(1), 100 * (gates + 2) + sandbox.MARGIN)
        self.assertEqual(lifetime(4), 100 * (gates + 8) + sandbox.MARGIN)

    def test_a_start_that_fails_half_way_gets_one_targeted_rm_and_waits(self):
        self.fake.start_fails = True
        result = self.check()
        self.assertEqual(self.fake.verb("rm"), [["docker", "rm", "-f", self.fake.name()]])
        self.assertEqual(result["verdict"], "wait")


class GatesRouting(Boxed):
    def setUp(self):
        super().setUp()
        trusted = self.dir / "trusted"
        (trusted / "tests").mkdir(parents=True)
        for name in ("mutation_check", "size_check", "ruff_gate", "new_gate"):
            (trusted / "tests" / f"{name}.py").write_text("print('ok')\n")
        git(trusted, "init", "-q")
        git(trusted, "add", "-A")
        git(trusted, "commit", "-qm", "trusted tests")
        patch = mock.patch.object(lander.paths, "REPO", trusted)
        patch.start()
        self.addCleanup(patch.stop)
        args = "--base {base} --head {head}"
        self.cfg["lander"]["gates"] = [
            f"python3 tests/{name}.py {args}"
            for name in ("mutation_check", "size_check", "ruff_gate", "new_gate")
        ] + ["echo config-gate"]

    def test_the_mutation_gate_runs_in_the_container_from_the_trusted_mount(self):
        self.assertEqual(self.check()["verdict"], "checked")
        mutation = [c for c in self.fake.commands() if "mutation_check" in c]
        self.assertEqual(len(mutation), 1)
        self.assertTrue(mutation[0].startswith("python3 /trusted/tests/mutation_check.py --base "))
        self.assertTrue(mutation[0].endswith(" --repo /workspace"), mutation)

    def test_only_the_text_only_gates_stay_on_the_host(self):
        self.check()
        cmds = self.fake.commands()
        self.assertFalse([c for c in cmds if "size_check" in c or "ruff_gate" in c])
        self.assertIn("echo config-gate", cmds)

    def test_an_unknown_gate_script_runs_in_the_container_not_on_the_host(self):
        self.check()
        self.assertTrue([c for c in self.fake.commands()
                         if c.startswith("python3 /trusted/tests/new_gate.py")])


class SandboxDown(Boxed):
    """No docker, no worker image, a failed start: the check waits (transient, never on the
    host); a held card is a docker-tests diff."""

    def probe(self):
        self.cfg["contract_commands"] = [f"echo ran > {self.marker}"]
        self.cfg["lander"]["gates"] = [f"echo ran > {self.marker}"]
        result = self.check()
        self.assertFalse(self.marker.exists())
        self.assertEqual(self.fake.verb("exec"), [])
        self.assertEqual((result["verdict"], result["hold"]), ("wait", "lander_wait_docker"))
        self.assertEqual(self.status(result)["gates"], "wait")
        return self.detail(result, "gates")

    def test_docker_down_waits_and_never_runs_on_the_host(self):
        self.fake.up = False
        self.assertIn("docker is not available", self.probe())

    def test_a_missing_worker_image_waits_and_never_runs_on_the_host(self):
        self.fake.start_fails = True
        self.assertIn("did not start", self.probe())

    def test_the_after_push_check_reports_wait_never_red(self):
        sha = git(self.repo, "rev-parse", "HEAD")
        self.fake.up = False
        status, _ = landing._suites_red(self.cfg, self.repo, sha, 60)
        self.assertEqual(status, "wait")
        self.assertEqual(self.fake.verb("run"), [])


class DockerTestsAreTheKeepers(Boxed):
    def test_a_diff_that_would_turn_the_docker_tests_on_is_held_and_runs_nothing(self):
        self.put("docker/Dockerfile", "x\n", "card work")
        result = self.check()
        self.assertEqual((result["verdict"], result["hold"]), ("held", "needs_docker_tests"))
        self.assertIn("needs docker tests: land by keeper", self.detail(result, "suites"))
        self.assertNotIn("suite-one-ok", " ".join(self.fake.commands()))


class AfterPushCheck(Boxed, test_landing.Landing):
    def pushed(self):
        sha = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "push", "-q", "origin", f"{sha}:refs/heads/main")
        git(self.release, "fetch", "-q")
        state = {"state": "pushed", "sha": sha, "commits": [sha]}
        db.guard_set(self.conn, landing.LANDED + self.tid, state, now=NOW)
        return sha

    def landed(self):
        return db.guard_get(self.conn, landing.LANDED + self.tid)[0]

    def verify(self, now):
        return landing.verify(self.conn, self.tid, cfg=self.cfg, release=str(self.release), now=now)

    def asks(self):
        return self.conn.execute(
            "SELECT COUNT(*) FROM outbox WHERE body LIKE '%cannot run%'").fetchone()[0]

    def incidents(self):
        return [r[0] for r in self.conn.execute("SELECT kind FROM incidents")]

    def test_the_landed_commit_suites_run_through_the_sandbox(self):
        sha = git(self.repo, "rev-parse", "HEAD")
        status, _ = landing._suites_red(self.cfg, self.repo, sha, 60)
        self.assertEqual(status, "pass")
        self.assertEqual(self.fake.commands(), self.cfg["contract_commands"])
        self.assertIn("--network=none", self.flat())

    def test_verify_with_docker_down_keeps_the_card_pushed_without_incident_or_revert(self):
        sha = self.pushed()
        self.fake.up = False
        got = self.verify(NOW)
        self.assertEqual(got["outcome"], "verify_held")
        self.assertEqual(self.landed()["state"], "pushed")
        self.assertEqual(self.incidents(), [])
        self.assertEqual(git(self.remote, "rev-parse", "main"), sha)

    def test_three_days_of_verify_held_asks_once_opens_one_incident_and_never_reverts(self):
        sha = self.pushed()
        self.fake.up = False
        for day in range(4):
            self.assertEqual(self.verify(NOW + day * DAY)["outcome"], "verify_held")
            self.assertEqual(self.verify(NOW + day * DAY + 60)["outcome"], "verify_held")
        self.assertEqual((self.asks(), self.incidents()), (1, ["review_infra"]))
        self.assertEqual(self.landed()["state"], "pushed")
        self.assertEqual(git(self.remote, "rev-parse", "main"), sha)

    def test_a_young_verify_held_stays_quiet(self):
        self.pushed()
        self.fake.up = False
        self.verify(NOW)
        self.verify(NOW + 60)
        self.assertEqual((self.asks(), self.incidents()), (0, []))
        self.assertEqual(self.landed()["held_since"], NOW)

    def test_a_later_clean_verify_clears_the_held_streak(self):
        self.pushed()
        self.fake.up = False
        self.verify(NOW)
        self.verify(NOW + DAY)
        self.fake.up = True
        self.assertEqual(self.verify(NOW + 2 * DAY)["outcome"], "verified")
        self.assertNotIn("held_since", self.landed())
        self.assertNotIn("held_asked", self.landed())

    def test_a_container_and_daemon_gone_mid_verify_is_waited_on_never_reverted(self):
        sha = self.pushed()
        self.fake.exec_rc, self.fake.daemon_dies = 1, True
        self.assertEqual(self.verify(NOW)["outcome"], "verify_held")
        self.assertEqual(self.landed()["state"], "pushed")
        self.assertEqual(git(self.remote, "rev-parse", "main"), sha)

    def test_a_container_gone_with_the_daemon_up_mid_verify_is_red_and_reverts(self):
        self.pushed()
        self.fake.exec_rc, self.fake.alive = 1, False
        self.assertEqual(self.verify(NOW)["outcome"], "reverted")


class HostBoxIsTestOnly(unittest.TestCase):
    def test_the_host_stand_in_refuses_to_run_outside_a_test_process(self):
        with mock.patch.dict(sys.modules):
            del sys.modules["unittest"]
            with self.assertRaises(RuntimeError):
                host_box.session(None, {}).__enter__()


def _spy(real, direct):
    """A subprocess entry point that records a call naming the mutation gate in `direct` and swaps
    it for `true` (it would run real tests); any other call goes through. Only calls through the
    subprocess.run/Popen module attributes are seen (run, call, check_output and os.popen route
    there); os.system, os.exec*/spawn and `from subprocess import` aliases are not, and capsule/
    uses none of them."""
    def call(args, *rest, **kw):
        text = args if isinstance(args, str) else " ".join(map(str, args))
        if "mutation_check" in text:
            direct.append(text)
            args, kw = "true", {**kw, "shell": True}
        return real(args, *rest, **kw)
    return call


class IntentExamples(Boxed, test_landing.Landing):
    """Z1: contracts/intent/lander_sandbox.jsonl replayed through the real lander and landing."""

    CASES = {"candidate_suite_writes_to_host", "docker_down", "verify_docker_down",
             "candidate_exec_in_container", "docker_down_gates", "verify_docker_down_release",
             "mount_missing_once", "config_gate_exits_75"}

    def verify_replay(self, late):
        sha = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "push", "-q", "origin", f"{sha}:refs/heads/main")
        git(self.release, "fetch", "-q")
        db.guard_set(self.conn, landing.LANDED + self.tid,
                     {"state": "pushed", "sha": sha, "commits": [sha]}, now=NOW)
        self.fake.up = False
        wait = self.cfg["lander"].get("ci_wait_s", landing.CI_WAIT_S)
        # the wait is over: it asks once (one incident, not one per step) and never reverts, at
        # every later check across 30 days; the first deviation fails here, not in the last step
        steps = [NOW] + [NOW + wait + 1 + d * DAY for d in (0, 1, 3, 5, 10, 30)] * late
        for now in steps:
            got = landing.verify(self.conn, self.tid, cfg=self.cfg, release=str(self.release),
                                 now=now)
            keys = {"outcome": got["outcome"],
                    "state": db.guard_get(self.conn, landing.LANDED + self.tid)[0]["state"],
                    "release_main": "unchanged" if git(self.remote, "rev-parse", "main") == sha
                    else "moved"}
            if late:
                self.assertEqual((keys["outcome"], keys["release_main"]),
                                 ("verify_held", "unchanged"), f"at +{(now - NOW) // DAY}d")
        incidents = self.conn.execute("SELECT COUNT(*) FROM incidents").fetchone()[0]
        return {**keys, "incidents": incidents}

    def check_replay(self, case):
        command = f"echo owned > {self.marker}"
        gate_marker = self.dir / "gate-marker"
        self.cfg["contract_commands"] = [command]
        gate = ("python3 tests/mutation_check.py --base {base} --head {head}"
                if case == "docker_down_gates" else f"echo ran > {gate_marker}")
        if case == "config_gate_exits_75":     # no trusted script: its 75 is the command's own
            gate, self.fake.exec_rc = "exit 75", 75
        self.cfg["lander"]["gates"] = [gate]
        self.fake.up = not case.startswith("docker_down")
        self.fake.probe_fails = int(case == "mount_missing_once")    # the first container is bare
        host, real_sh = [], lander._sh      # every command that went through lander._sh
        direct, real_run, real_popen = [], subprocess.run, subprocess.Popen

        def record(cmd, *args):
            host.append(cmd)       # the mutation gate is only recorded: it would run real tests
            return (0, "ok") if "mutation_check" in cmd else real_sh(cmd, *args)

        with mock.patch.object(lander, "_sh", record), \
                mock.patch.object(subprocess, "run", _spy(real_run, direct)), \
                mock.patch.object(subprocess, "Popen", _spy(real_popen, direct)):
            result = self.check()
        run = (self.fake.verb("run") or [[]])[0]
        flags = [i for i, a in enumerate(run)
                 if a.startswith(("--network", "--net=")) or a == "--net"]
        before_image = flags and flags[0] < run.index("img:test")
        return {"verdict": result["verdict"],
                "host_marker": "present" if self.marker.exists() else "absent",
                "gate_marker": "present" if gate_marker.exists() else "absent",
                "docker_run": len(self.fake.verb("run")),
                "docker_exec": len(self.fake.verb("exec")),
                "host_sh": len(host) + len(direct),     # _sh calls plus direct mutation_check runs
                "candidate_exec": self.fake.commands().count(command),
                "network_flags": len(flags),
                "network_position": "before-image" if before_image else "elsewhere",
                "network": "none" if len(flags) == 1 and before_image
                and run[flags[0]] == "--network=none" else "open"}

    def replay(self, case):
        self.assertIn(case, self.CASES)
        if case.startswith("verify_docker_down"):
            return self.verify_replay(late=case == "verify_docker_down_release")
        return self.check_replay(case)

    def test_a_container_missing_its_mounts_once_is_replaced_and_the_check_still_runs(self):
        got = self.replay("mount_missing_once")
        self.assertEqual((got["verdict"], got["docker_run"], got["host_marker"]),
                         ("checked", 2, "absent"))

    def test_the_shipped_examples(self):
        rows = intent.load("lander_sandbox")
        self.assertEqual({r["id"] for r in rows}, {
            "candidate-test-runs-in-container", "sandbox-down-waits-never-host",
            "after-push-check-with-sandbox-down-is-held-never-reverted",
            "candidate-command-is-executed-in-the-container",
            "sandbox-down-never-runs-a-candidate-code-gate-on-the-host",
            "after-push-check-with-sandbox-down-never-pushes-a-revert",
            "sandbox-container-missing-its-mounts-once-is-replaced",
            "non-script-gate-exiting-75-is-red-not-a-wait"})
        seen = set()
        for row in rows:
            self.setUp()
            got = self.replay(row["given"]["document"]["case"])
            self.assertLessEqual(set(row["expect"]), set(got), row["id"])   # no unchecked key
            for key, want in row["expect"].items():
                self.assertEqual(got[key], want, f"{row['id']}: {key}")
            seen.add(row["id"])
        self.assertEqual(len(seen), 8)


class EnvRefusal(Boxed):
    """A boxed trusted gate that exits 75 is run again in a fresh container, never the same."""

    def setUp(self):
        super().setUp()
        self.cfg["lander"]["gates"] = [
            "python3 tests/mutation_check.py --base {base} --head {head}"]

    def copies(self):
        return [next(a for a in run if a.endswith(":/workspace")) for run in self.fake.verb("run")]

    def test_the_second_try_is_a_new_container_with_its_own_copy(self):
        self.fake.exec_rcs = [75, 0]
        result = self.check()
        self.assertEqual((self.status(result)["gates"], result["verdict"]), ("pass", "checked"))
        copies = self.copies()        # the lander's session, then the retry's own
        self.assertEqual((len(copies), len(set(copies))), (2, 2))

    def test_after_a_green_retry_the_suites_run_in_the_fresh_container_not_the_refused(self):
        self.fake.exec_rcs = [75, 0]
        self.check()
        refused, fresh = [run[run.index("--name") + 1] for run in self.fake.verb("run")]
        (suite,) = [c for c in self.fake.verb("exec") if c[-1].endswith("echo suite-one-ok")]
        self.assertIn(fresh, suite)
        self.assertNotIn(refused, suite)

    def test_two_refusals_on_a_clean_host_clone_are_a_wait_that_says_so(self):
        self.fake.exec_rcs = [75, 75]
        result = self.check()
        self.assertEqual((result["verdict"], result["hold"]), ("wait", "lander_wait_env"))
        self.assertTrue(self.detail(result, "gates").split("\n", 1)[1].startswith(lander.ENV_WAIT))
        self.assertNotIn("--allow-dirty", self.detail(result, "gates"))
        self.assertEqual(len(self.fake.verb("run")), 2)


class RealDocker(unittest.TestCase):
    def test_host_uid_runs(self):
        if os.environ.get("RUN_DOCKER_TESTS") != "1":    # in the body: the gate flags a decorator
            self.skipTest("real docker: RUN_DOCKER_TESTS=1")
        script = (
            "import subprocess, sys, tempfile\n"
            f"sys.path.insert(0, {str(paths.REPO)!r})\n"
            "from capsule.runners import sandbox\n"
            "with tempfile.TemporaryDirectory() as d:\n"
            "    subprocess.run(['git', 'init', '-q', d], check=True)    # a clone has a .git\n"
            "    subprocess.run(['git', '-C', d, '-c', 'user.name=capsule-test', '-c',\n"
            "                    'user.email=test@capsule.invalid', 'commit', '-q',\n"
            "                    '--allow-empty', '-m', 'init'], check=True)   # probe reads HEAD\n"
            "    with sandbox.session(d, {}, 60) as run:\n"
            "        rc, out = run('python3 -c \"import os; print(os.getuid())\"', 120)\n"
            "print(rc, out.strip())\n")
        env = {**os.environ, "OPS_WORKER_IMAGE": docker.lock_image()}
        done = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                              env=env, timeout=300)
        self.assertEqual(done.stdout.strip(), f"0 {os.getuid()}", done.stderr)


class F(unittest.TestCase):
    """fake_docker.sh must fail where a real docker would: never run a command it cannot place."""

    def _exec(self, run_first=True, mount=None):
        with tempfile.TemporaryDirectory() as tmp:
            env = {**os.environ, **host_box.docker_env(tmp)}
            docker_bin = str(Path(env["AGENT_OPS_DOCKER_BIN"]) / "docker")
            marker = Path(tmp) / "ran"
            if run_first:
                subprocess.run([docker_bin, "run", "--name", "c1", "-v", f"{mount}:/workspace"],
                               env=env, check=True)
            done = subprocess.run(
                [docker_bin, "exec", "-w", "/workspace", "c1", "sh", f"touch {marker}"],
                env=env, capture_output=True, text=True)
            return done.returncode, marker.exists()

    def test_known(self):
        """A known container and workspace run the command."""
        with tempfile.TemporaryDirectory() as ws:
            self.assertEqual(self._exec(mount=ws), (0, True))

    def test_no_ws(self):
        """A missing workspace fails and runs nothing."""
        self.assertEqual(self._exec(mount="/nonexistent-ws"), (1, False))

    def test_noctr(self):
        """An unknown container fails and runs nothing."""
        self.assertEqual(self._exec(run_first=False), (1, False))


if __name__ == "__main__":
    unittest.main()
