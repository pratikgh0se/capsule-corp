"""StrongRunner.start probes the bind mounts of a fresh container and retries a broken one
(Docker Desktop 4.43.1 sometimes starts containers with every -v mount missing). A fake docker
command only; never real docker."""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from capsule.runners import docker


def setUpModule():   # the runner never falls back to another image, so name one (statement 3)
    patch = mock.patch.dict(os.environ, {"OPS_WORKER_IMAGE": "stub-image:1"})
    patch.start()
    unittest.addModuleCleanup(patch.stop)


RUN = "a" * 32
NAME = docker.container_name(RUN)
MOUNTS = '[{"Mounts": [{"Source": "/w"}]}]'   # what `docker inspect` answers for a clean container


class Fake:
    """A fake docker: `probes` are the answers to successive `exec`s (a code, or an exception to
    raise), `run_raises` the same for `run`; `rm_rc` is what `docker rm` returns. It tracks
    whether the named container still exists, which `inspect` reports."""

    def __init__(self, probes=(), rm_rc=0, run_raises=()):
        self.calls, self.probes, self.rm_rc = [], list(probes), rm_rc
        self.run_raises, self.exists = list(run_raises), False

    def __call__(self, argv, **kw):
        self.calls.append(argv)
        verb = argv[1]
        rc = 0
        if verb == "run":
            self.exists = True
            if self.run_raises:
                raise self.run_raises.pop(0)
        elif verb == "exec":
            rc = self.probes.pop(0)
            if isinstance(rc, Exception):
                raise rc
        elif verb == "rm":
            rc = self.rm_rc
            self.exists = self.exists and rc != 0
        elif verb == "inspect":
            rc = 0 if self.exists else 1
        return subprocess.CompletedProcess(argv, rc, "" if rc or verb != "inspect" else MOUNTS,
                                           "No such file or directory" if rc else "")

    def verbs(self):
        return [c[1] for c in self.calls]


def _runner(fake):
    sleeps = []
    r = docker.StrongRunner()
    r._run, r._sleep = fake, sleeps.append
    return r, sleeps


class MountGuard(unittest.TestCase):
    def test_a_probe_that_passes_first_time_starts_once_and_stops_nothing(self):
        fake = Fake([0])
        r, sleeps = _runner(fake)
        self.assertEqual(r.start(RUN, Path("/h/x"), git_dir="/h/.gitdirs/x"), NAME)
        self.assertEqual(fake.verbs(), ["run", "exec", "inspect"])   # inspect: the R33 mount audit
        self.assertEqual(sleeps, [])

    def test_a_probe_that_fails_once_removes_that_container_and_returns_the_second(self):
        fake = Fake([1, 0])
        r, sleeps = _runner(fake)
        self.assertEqual(r.start(RUN, Path("/h/x"), git_dir="/h/.gitdirs/x"), NAME)
        self.assertEqual(fake.verbs(), ["run", "exec", "inspect", "stop", "rm", "inspect", "run",
                                        "exec", "inspect"])   # the audit runs on a failed probe too
        self.assertEqual(sleeps, [docker._START_BACKOFF[0]])

    def test_a_probe_that_always_fails_raises_the_start_error_after_three_tries(self):
        fake = Fake([1, 1, 1])
        r, sleeps = _runner(fake)
        with self.assertRaises(subprocess.CalledProcessError) as caught:
            r.start(RUN, Path("/h/x"), git_dir="/h/.gitdirs/x")
        self.assertIn("bind mounts missing", caught.exception.stderr)
        self.assertEqual(fake.verbs().count("run"), 3)
        self.assertEqual(fake.verbs().count("stop"), 3)   # every failed container is removed
        self.assertEqual(fake.verbs().count("rm"), 3)
        self.assertFalse(fake.exists)
        self.assertEqual(len(sleeps), 2)                  # no sleep after the last try

    def test_the_final_error_text_is_the_same_for_every_run(self):
        texts = []
        for run_id in ("a" * 32, "b" * 32):
            r, _ = _runner(Fake([1, 1, 1]))
            with self.assertRaises(subprocess.CalledProcessError) as caught:
                r.start(run_id, Path("/w"))
            texts.append(str(caught.exception))
        self.assertEqual(texts[0], texts[1])
        self.assertNotIn(NAME, texts[0])

    def test_cleanup_never_forces(self):
        fake = Fake([1, 0])
        r, _ = _runner(fake)
        r.start(RUN, Path("/w"))
        for argv in fake.calls:
            if argv[1] in ("stop", "rm"):
                self.assertNotIn("-f", argv)
                self.assertNotIn("--force", argv)
                self.assertEqual(argv[-1], NAME)

    def test_the_probe_always_looks_for_the_clones_git_not_the_baked_workspace(self):
        # A stat (`test -e`) passes on a stale Docker Desktop file-share entry; git must READ.
        # The listing that stats every entry comes first: it heals a stale entry, then git reads.
        read = "git -C /workspace rev-parse --verify HEAD"
        tree = docker.TREE_LISTING
        fake = Fake([0])
        r, _ = _runner(fake)
        r.start(RUN, Path("/w"))
        self.assertEqual(fake.calls[1][-1], f"{tree}ls -la /workspace >/dev/null && {read}")
        self.assertNotIn("test -e", fake.calls[1][-1])
        fake = Fake([0])
        r, _ = _runner(fake)
        r.start(RUN, Path("/h/w/e"), git_dir="/h/w/.gitdirs/e y")   # probes the container path
        self.assertEqual(fake.calls[1][-1], f"{tree}ls -la /workspace '/.gitdirs/e y' >/dev/null "
                         f"&& {read} && test \"$(git -C /workspace rev-parse --absolute-git-dir)\""
                         " = '/.gitdirs/e y' && test -d '/.gitdirs/e y'")
        fake = Fake([0, 0])
        r, _ = _runner(fake)
        r.start(RUN, Path("/h/w/e"), requests_dir="/h/spool/r/requests", git_dir="/h/w/.gitdirs/e")
        self.assertEqual(fake.calls[1][-1], f"{tree}ls -la /workspace /.gitdirs/e >/dev/null "
                         f"&& {read} && test \"$(git -C /workspace rev-parse --absolute-git-dir)\""
                         " = /.gitdirs/e && test -d /requests && test -d /.gitdirs/e")
        self.assertEqual(fake.calls[3][-1], "test ! -e /blind || exit 3")   # 2nd oracle
        cmd = fake.calls[1][-1]
        self.assertLess(cmd.index("ls -la"), cmd.index("rev-parse"))   # list, then read

    def test_the_probe_lists_nested_workspace_dirs_and_a_listing_error_never_fails_it(self):
        # inc_7417f9a9: a file the host rewrote in a nested dir (.github/workflows/, tests/) was
        # stale in the container; the start listing covered only /workspace itself.
        fake = Fake([0])
        r, _ = _runner(fake)
        r.start(RUN, Path("/w"))
        cmd = fake.calls[1][-1]
        listing, sep, rest = cmd.partition("; ")
        self.assertTrue(sep and rest, cmd)
        self.assertIn("find /workspace", listing)                    # the whole tree, not one level
        self.assertIn("-name .git -prune", listing)                  # never walks into .git
        self.assertIn("-type d", listing)
        self.assertIn("ls -la", listing)                             # the stat that heals an entry
        self.assertTrue(listing.startswith("timeout 10 "), listing)  # bounded, under _QUICK_TIMEOUT
        self.assertLess(10, docker._QUICK_TIMEOUT)
        self.assertLess(cmd.index("find /workspace"), cmd.index("rev-parse"))   # list, then read
        # The separator is `; `, not `&&`: a failing listing cannot fail the probe (real sh).
        done = subprocess.run(["sh", "-c", f"false{sep}true"], capture_output=True)
        self.assertEqual(done.returncode, 0)
        self.assertNotEqual(subprocess.run(["sh", "-c", "false && true"]).returncode, 0)
        # The find expression itself is valid and exits 0 on a real tree (no `timeout` on macOS).
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "a" / "b").mkdir(parents=True)
            (Path(tmp) / ".git" / "objects").mkdir(parents=True)
            real = listing.removeprefix("timeout 10 ").replace("/workspace", tmp)
            self.assertEqual(subprocess.run(["sh", "-c", real.rstrip()]).returncode, 0)
            # It reaches a nested dir (not `-maxdepth 1`) and never walks into `.git`.
            seen = subprocess.run(
                ["sh", "-c", real.replace("ls -la", "echo").replace(">/dev/null 2>&1", "")],
                capture_output=True, text=True).stdout.split()
            self.assertIn(f"{tmp}/a/b", seen)
            self.assertNotIn(f"{tmp}/.git/objects", seen)

    def test_the_backoff_sleeps_are_the_named_delays_in_order(self):
        r, sleeps = _runner(Fake([1, 1, 0]))
        r.start(RUN, Path("/w"))
        self.assertEqual(sleeps, list(docker._START_BACKOFF))
        self.assertEqual(len(docker._START_BACKOFF), docker._START_TRIES - 1)
        self.assertLess(sum(docker._START_BACKOFF), 60)

    def test_a_container_that_cannot_be_removed_stops_the_retries_at_once(self):
        fake = Fake([1, 0], rm_rc=1)
        r, sleeps = _runner(fake)
        with self.assertRaises(subprocess.CalledProcessError) as caught:
            r.start(RUN, Path("/w"))
        self.assertEqual(fake.verbs().count("run"), 1)    # no new attempt into a name conflict
        self.assertEqual(sleeps, [])
        self.assertEqual(caught.exception.cmd, docker._PROBE_CMD)

    def test_a_hung_probe_counts_as_a_failure_and_is_cleaned_up(self):
        fake = Fake([subprocess.TimeoutExpired("docker", 30), 0])
        r, _ = _runner(fake)
        self.assertEqual(r.start(RUN, Path("/w")), NAME)
        self.assertEqual(fake.verbs().count("stop"), 1)
        self.assertEqual(fake.verbs().count("rm"), 1)

    def test_a_run_that_times_out_is_cleaned_up_by_name_then_raised(self):
        fake = Fake(run_raises=[subprocess.TimeoutExpired("docker", 300)])
        r, _ = _runner(fake)
        with self.assertRaises(docker.EngineDown):    # engine-hang: a start timeout is EngineDown
            r.start(RUN, Path("/w"))
        self.assertEqual(fake.verbs(), ["run", "stop", "rm", "inspect", "info"])
        self.assertFalse(fake.exists)

    def test_a_run_docker_refuses_raises_as_before_with_nothing_to_clean(self):
        err = subprocess.CalledProcessError(125, ["run"], "", "no such image")
        fake = Fake(run_raises=[err])
        r, _ = _runner(fake)
        with self.assertRaises(subprocess.CalledProcessError) as caught:
            r.start(RUN, Path("/w"))
        self.assertIs(caught.exception, err)
        # info twice: _ensure_engine, then _engine_gone
        self.assertEqual(fake.verbs(), ["run", "info", "info"])

    def test_an_unexpected_probe_error_is_cleaned_up_by_name_then_raised(self):
        fake = Fake([ValueError("boom")])
        r, _ = _runner(fake)
        with self.assertRaises(ValueError):
            r.start(RUN, Path("/w"))
        self.assertEqual(fake.verbs(), ["run", "exec", "stop", "rm", "inspect"])
        self.assertFalse(fake.exists)


if __name__ == "__main__":
    unittest.main()
