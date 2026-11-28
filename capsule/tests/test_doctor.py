"""P07 `capsule doctor`, plus the Z1 handlers for it and for `board new` (test_board_new.py).
Doctor is read-only, so every case runs it in a fake world: a temp HOME and CAPSULE_HOME and a
PATH holding stub tools (brew, docker, claude, gh, launchctl) that log each call. Two oracles per
case: doctor's exit code and printed fix line, and a direct probe of the same fact (the stub
missing from the PATH, the stub's own exit code, the file mode, `host.effective`)."""
import collections
import contextlib
import importlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from capsule import doctor, host, intent, paths
from capsule.runners import docker
from capsule.tests.test_board_new import board_new

TOOLS = ("brew", "launchctl", "claude", "gh", "docker", "git")
STUBS = {
    "git": 'echo "git version ${GIT_VERSION:-2.50.1} (stub)"\n',
    "docker": 'echo "docker $*" >> "$STUB_LOG"\ncase "$1" in\n'
              '  version) exit "${DOCKER_VERSION_RC:-0}";;\n'
              '  image) exit "${DOCKER_IMAGE_RC:-0}";;\n'
              "esac\nexit 0\n",
    "claude": 'echo "claude $*" >> "$STUB_LOG"\n'
              'echo "{\\"loggedIn\\": ${CLAUDE_LOGGED_IN:-true}}"\n',
    "gh": 'echo "gh $*" >> "$STUB_LOG"\nexit "${GH_RC:-0}"\n',
    "brew": 'echo "brew $*" >> "$STUB_LOG"\n',
    "launchctl": 'echo "launchctl $*" >> "$STUB_LOG"\n',
}
SCRUBBED = ("CAPSULE_LAUNCHD_PREFIX", "CAPSULE_SECRETS_BACKEND", "OBSIDIAN_VAULT_PATH",
            "OPS_WORKER_IMAGE", "OPS_ENV_FILE")
USAGE = collections.namedtuple("Usage", "total used free")
SECRET = "sk_live_TOPSECRET"


class _World(unittest.TestCase):
    """A temp HOME, CAPSULE_HOME and stub bin dir; `doctor()` runs the verb in-process."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.bin, self.home, self.user = self.tmp / "bin", self.tmp / "home", self.tmp / "user"
        for d in (self.bin, self.home, self.user):
            d.mkdir()
        self.log = self.tmp / "calls.log"
        for tool in TOOLS:
            self.stub(tool)

    def stub(self, tool):
        path = self.bin / tool
        path.write_text("#!/bin/sh\n" + STUBS[tool])
        path.chmod(0o755)

    def calls(self):
        return self.log.read_text().splitlines() if self.log.exists() else []

    def doctor(self, *flags, env=None, system="Darwin", mac="15.1", free=10**12):
        """(exit code, output lines) of `capsule doctor` in this world."""
        world = {"PATH": str(self.bin), "AGENT_OPS_DOCKER_BIN": str(self.bin),
                 "HOME": str(self.user), "CAPSULE_HOME": str(self.home),
                 "STUB_LOG": str(self.log), **(env or {})}
        args = SimpleNamespace(offline="--offline" in flags, capsule_home=None)
        out = io.StringIO()
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.dict(os.environ, world))
            for name in SCRUBBED:
                os.environ.pop(name, None)
            stack.enter_context(mock.patch.object(doctor.platform, "system", return_value=system))
            stack.enter_context(mock.patch.object(doctor.platform, "mac_ver",
                                                  return_value=(mac, ("", "", ""), "")))
            stack.enter_context(mock.patch.object(doctor.shutil, "disk_usage",
                                                  return_value=USAGE(10**13, 0, free)))
            stack.enter_context(contextlib.redirect_stdout(out))
            rc = doctor.handle(args, None)
        return rc, out.getvalue().splitlines()

    def missing(self, lines):
        return [ln for ln in lines if ln.startswith("MISSING ")]


class AllMet(_World):
    def test_a_complete_world_is_exit_0_with_only_ok_lines(self):
        rc, lines = self.doctor()
        self.assertEqual(rc, 0, lines)
        self.assertEqual(self.missing(lines), [])
        self.assertEqual(lines[-1], "doctor: all prerequisites met")
        self.assertTrue(all(shutil.which(t, path=str(self.bin)) for t in TOOLS))   # oracle 2

    def test_it_prints_the_effective_host_settings_with_their_source(self):
        (self.home / "host.json").write_text(json.dumps({"launchd": {"label_prefix": "com.x"}}))
        _, lines = self.doctor()
        row = next(ln for ln in lines if ln.startswith("host.json settings:"))
        self.assertIn("launchd.label_prefix=com.x (host.json)", row)
        self.assertEqual(host.get("launchd.label_prefix", self.home, {}), "com.x")


    def test_it_never_prints_the_vault_path_only_whether_a_vault_exists(self):
        vault = self.home / "private-vault-dir"
        vault.mkdir()
        (self.home / "host.json").write_text(json.dumps({"vault": {"path": str(vault)}}))
        _, lines = self.doctor()
        self.assertNotIn("private-vault-dir", "\n".join(lines))
        row = next(ln for ln in lines if ln.startswith("host.json settings:"))
        self.assertIn("vault configured=yes, directory exists", row)

    def test_a_vault_that_is_not_configured_or_missing_is_told_apart(self):
        _, lines = self.doctor()
        row = next(ln for ln in lines if ln.startswith("host.json settings:"))
        self.assertIn("vault configured=no (default)", row)
        gone = str(self.home / "gone-vault")
        (self.home / "host.json").write_text(json.dumps({"vault": {"path": gone}}))
        _, lines = self.doctor()
        self.assertNotIn("gone-vault", "\n".join(lines))
        row = next(ln for ln in lines if ln.startswith("host.json settings:"))
        self.assertIn("vault configured=yes, directory missing", row)

    def test_the_line_never_prints_the_repo_path_or_the_decisions_note(self):
        (self.home / "host.json").write_text(json.dumps(
            {"repo": "/home/someone/secret-area/capsule-x",
             "vault": {"decision_notes": "Decisions/Private Note*.md",
                       "skill_root": "/home/someone/private-skill"}}))
        _, lines = self.doctor()
        out = "\n".join(lines)
        self.assertNotIn("/home/someone", out)
        self.assertNotIn("Private Note", out)
        self.assertNotIn("private-skill", out)
        row = next(ln for ln in lines if ln.startswith("host.json settings:"))
        self.assertIn("repo=capsule-x (host.json)", row)
        self.assertIn("vault.decision_notes=custom (host.json)", row)
        self.assertIn("vault skill_root configured=yes, directory missing (host.json)", row)


class OneMissing(_World):
    def test_each_missing_tool_is_exit_1_with_one_fix_line_naming_the_command(self):
        want = {"brew": "install Homebrew from https://brew.sh",
                "launchctl": "run Capsule on a Mac: launchctl ships with macOS",
                "claude": "brew install --cask claude-code@latest", "gh": "brew install gh",
                "git": "brew install git"}
        for tool, fix in want.items():
            with self.subTest(tool=tool):
                (self.bin / tool).unlink()
                self.assertIsNone(shutil.which(tool, path=str(self.bin)))   # oracle 2
                try:
                    rc, lines = self.doctor("--offline")
                finally:
                    self.stub(tool)
                self.assertEqual(rc, 1)
                self.assertEqual(len(self.missing(lines)), 1, lines)
                self.assertIn(f" -> {fix}", self.missing(lines)[0])

    def test_git_older_than_2_40_is_refused_and_2_40_passes(self):
        for version, ok in (("2.39.5", False), ("2.9.0", False), ("2.40.0", True),
                            ("2.50.1", True), ("3.0.1", True)):
            with self.subTest(git=version):
                rc, lines = self.doctor("--offline", env={"GIT_VERSION": version})
                self.assertEqual(rc, 0 if ok else 1, lines)
                if not ok:
                    self.assertEqual(len(self.missing(lines)), 1, lines)
                    self.assertIn("MISSING git 2.40+ ->", self.missing(lines)[0])
                    self.assertIn(f"(this is {version}): brew upgrade git", self.missing(lines)[0])

    def test_git_with_unreadable_version_is_refused(self):
        rc, lines = self.doctor("--offline", env={"GIT_VERSION": "banana"})
        self.assertEqual(rc, 1, lines)
        self.assertIn("MISSING git 2.40+ ->", self.missing(lines)[0])

    def test_docker_not_installed(self):
        err = FileNotFoundError("docker")
        with mock.patch.object(doctor.docker_door, "docker_run", side_effect=err):
            rc, lines = self.doctor()
        self.assertEqual(rc, 1)
        self.assertIn("MISSING Docker daemon running -> install OrbStack: brew install"
                      " --cask orbstack, then open -a OrbStack", lines)

    def test_docker_installed_but_the_daemon_is_down(self):
        rc, lines = self.doctor(env={"DOCKER_VERSION_RC": "1"})
        self.assertEqual(rc, 1)
        self.assertEqual(self.missing(lines), [
            "MISSING Docker daemon running -> start OrbStack: open -a OrbStack, then rerun"])
        self.assertEqual(subprocess.run([self.bin / "docker", "version"],
                                        env={"DOCKER_VERSION_RC": "1", "STUB_LOG": str(self.log)}
                                        ).returncode, 1)                       # oracle 2

    def test_docker_that_hangs_is_not_reported_as_missing(self):
        hang = subprocess.TimeoutExpired("docker", 20)
        with mock.patch.object(doctor.docker_door, "docker_run", side_effect=hang):
            rc, lines = self.doctor()
        self.assertEqual(rc, 1)
        fix = self.missing(lines)[0]
        self.assertIn("-> Docker is not responding: restart OrbStack", fix)
        self.assertNotIn("install", fix)
        self.assertTrue(shutil.which("docker", path=str(self.bin)))              # oracle 2

    def test_gh_that_hangs_suggests_offline_not_login(self):
        real = subprocess.run

        def slow_gh(cmd, *a, **kw):
            if cmd[0] == "gh":
                raise subprocess.TimeoutExpired(cmd, 20)
            return real(cmd, *a, **kw)

        with mock.patch.object(doctor.subprocess, "run", slow_gh):
            rc, lines = self.doctor()
        self.assertEqual(rc, 1)
        self.assertEqual(len(self.missing(lines)), 1, lines)
        self.assertIn("-> gh auth status timed out (offline?): rerun with --offline",
                      self.missing(lines)[0])
        self.assertEqual(self.doctor("--offline")[0], 0)                         # oracle 2

    def test_one_check_that_raises_is_one_line_and_the_rest_still_run(self):
        with mock.patch.object(doctor, "_python", side_effect=RuntimeError("boom\nline two")):
            rc, lines = self.doctor()
        self.assertEqual(rc, 1)
        self.assertEqual(self.missing(lines), [
            "MISSING Python 3.11+ -> could not check (RuntimeError: boom line two)"])
        self.assertIn("ok launchctl present", lines)
        self.assertIn("ok host.json valid", lines)
        self.assertEqual(lines[-1], "doctor: 1 missing")
        self.assertEqual(self.doctor()[0], 0)                                    # oracle 2

    def test_claude_logged_out(self):
        rc, lines = self.doctor(env={"CLAUDE_LOGGED_IN": "false"})
        self.assertEqual(rc, 1)
        self.assertEqual(self.missing(lines), [
            "MISSING claude installed and logged in -> log in with your Claude subscription:"
            " run claude, then /login"])

    def test_gh_not_logged_in_and_offline_skips_the_call(self):
        rc, lines = self.doctor(env={"GH_RC": "1"})
        self.assertEqual(rc, 1)
        self.assertEqual(self.missing(lines), [
            "MISSING gh installed and logged in (gh auth status) -> log in to GitHub:"
            " gh auth login"])
        self.assertIn("gh auth status", self.calls())
        self.log.unlink()
        rc, lines = self.doctor("--offline", env={"GH_RC": "1"})
        self.assertEqual(rc, 0, lines)
        self.assertNotIn("gh auth status", self.calls())                         # oracle 2

    def test_old_macos_and_not_macos(self):
        rc, lines = self.doctor(mac="13.6")
        self.assertEqual((rc, len(self.missing(lines))), (1, 1))
        self.assertIn("-> update macOS to 14+ (this is 13.6)", self.missing(lines)[0])
        rc, lines = self.doctor(system="Linux")
        self.assertEqual((rc, len(self.missing(lines))), (1, 1))
        self.assertIn("-> use a Mac on macOS 14 or newer", self.missing(lines)[0])

    def test_old_python(self):
        with mock.patch.object(doctor, "MIN_PYTHON", (99, 0)):
            rc, lines = self.doctor()
        self.assertEqual(rc, 1)
        self.assertEqual(self.missing(lines), [
            "MISSING Python 99.0+ -> install Python 99.0+: brew install uv && uv python"
            " install 99.0"])
        self.assertLess(sys.version_info[:2], (99, 0))                            # oracle 2

    def test_low_disk(self):
        (self.home / "images.lock").write_text("capsule-python@sha256:" + "a" * 64 + "\n")
        rc, lines = self.doctor(free=24 * 10**9)    # the recorded image is here: the 25 GB floor
        self.assertEqual(rc, 1)
        self.assertIn("-> free 1 GB more on the volume holding", self.missing(lines)[0])
        self.assertEqual(self.doctor(free=25 * 10**9)[0], 0)

    @unittest.skipIf(os.geteuid() == 0, "root can write anywhere")
    def test_capsule_home_not_writable(self):
        self.home.chmod(0o500)
        self.addCleanup(self.home.chmod, 0o700)
        rc, lines = self.doctor()
        self.assertEqual(rc, 1)
        self.assertEqual(len(self.missing(lines)), 1, lines)
        self.assertIn(f"-> make it writable: chmod u+w {self.home}", self.missing(lines)[0])
        self.assertFalse(os.access(self.home, os.W_OK))                           # oracle 2

    def test_capsule_home_not_yet_created_is_fine_but_never_created(self):
        gone = self.tmp / "fresh" / "capsule"
        rc, lines = self.doctor(env={"CAPSULE_HOME": str(gone)})
        self.assertEqual(rc, 0, lines)
        self.assertFalse(gone.parent.exists())


class HostJson(_World):
    def test_a_bad_host_json_is_missing_and_never_echoes_the_value(self):
        (self.home / "host.json").write_text(json.dumps({"secrets": {"backend": SECRET}}))
        rc, lines = self.doctor()
        self.assertEqual(rc, 1)
        self.assertEqual(len(self.missing(lines)), 1, lines)
        self.assertIn("MISSING host.json valid -> edit it so it parses (", self.missing(lines)[0])
        self.assertIn("secrets.backend", self.missing(lines)[0])
        self.assertNotIn(SECRET, "\n".join(lines))
        with self.assertRaises(host.HostError):                                   # oracle 2
            host.effective(self.home, {})


class ImageCheck(_World):
    """The worker image line reads $CAPSULE_HOME/images.lock (what build-base.sh built here),
    never the repo's docker/IMAGES.lock, and says present, buildable or not buildable."""
    ROW = "capsule-python@sha256:" + "a" * 64

    def repo(self, dockerfile=True, floor=True):
        """A repo whose own IMAGES.lock holds a different (the maintainer's) digest; `floor`: it
        has the floor file."""
        root = self.tmp / "repo"
        (root / "docker" / "stacks" / "python").mkdir(parents=True, exist_ok=True)
        (root / "docker" / "base").mkdir(exist_ok=True)
        (root / "docker" / "build-base.sh").write_text("#!/bin/sh\n")
        if floor:
            (root / "docker" / "BUILD_MIN_FREE_BYTES").write_text("40000000000\n")
        (root / "docker" / "base" / "Dockerfile").write_text("FROM scratch\n")
        if dockerfile:
            (root / doctor.STACK_DOCKERFILE).write_text("FROM scratch\n")
        (root / "docker" / "IMAGES.lock").write_text("capsule-python@sha256:" + "b" * 64 + "\n")
        return mock.patch.object(paths, "REPO", root)

    def image_line(self, env=None, record=ROW, free=10**12, tracked=False, **kw):
        """`tracked`: the maintainer's route is live (docker.LOCK_FILE is the repo's lock)."""
        if record is not None:
            (self.home / "images.lock").write_bytes(record if isinstance(record, bytes)
                                                    else (record + "\n").encode())
        lock = self.tmp / "repo" / "docker" / "IMAGES.lock" if tracked else self.tmp / "none.lock"
        with self.repo(**kw), mock.patch.object(docker, "LOCK_FILE", lock):
            rc, lines = self.doctor(env=env, free=free)
        keep = ("worker image", "free disk", "MISSING")
        return rc, [ln for ln in lines if any(k in ln for k in keep)]

    def inspected(self):
        return [c for c in self.calls() if c.startswith("docker image inspect")]

    def test_present_locally(self):
        rc, line = self.image_line(dockerfile=False)
        want = f"ok worker image present (recorded in {self.home}/images.lock)"
        self.assertEqual((rc, line[0]), (0, want))
        self.assertEqual(self.inspected(), ["docker image inspect " + self.ROW])

    def test_absent_but_buildable_says_so_and_names_the_build_command(self):
        rc, line = self.image_line(env={"DOCKER_IMAGE_RC": "1"})
        self.assertEqual(rc, 0, line)
        self.assertTrue(line[0].startswith("ok worker image not built yet, buildable here"), line)
        self.assertIn("docker/build-base.sh base && docker/build-base.sh python", line[0])
        self.assertIn("40 GB", line[0])

    def test_absent_and_not_buildable(self):
        rc, line = self.image_line(env={"DOCKER_IMAGE_RC": "1"}, dockerfile=False)
        self.assertEqual(rc, 1)
        self.assertIn("MISSING worker image buildable (docker/build-base.sh and its Dockerfiles)"
                      " -> restore docker/stacks/python/Dockerfile from the repo", line[0])

    def test_absent_and_no_floor_file_is_not_buildable_either(self):
        rc, line = self.image_line(env={"DOCKER_IMAGE_RC": "1"}, floor=False)
        self.assertEqual(rc, 1)
        self.assertIn("MISSING worker image buildable (docker/build-base.sh and its Dockerfiles)"
                      " -> restore docker/BUILD_MIN_FREE_BYTES from the repo", line[0])

    def test_no_record_is_no_image_even_though_the_repo_lock_has_a_row(self):
        rc, line = self.image_line(record=None)
        self.assertTrue(line[0].startswith("ok worker image not built yet"), line)
        self.assertEqual(self.inspected(), [])

    def test_a_bad_record_is_no_image_never_present(self):
        for bad in ("capsule-python:local", "capsule-python@sha256:" + "a" * 63, "# nothing",
                    b"\xff\xfe\n"):
            with self.subTest(record=bad):
                self.log.unlink(missing_ok=True)
                rc, line = self.image_line(record=bad)
                self.assertEqual(rc, 0, line)
                self.assertTrue(line[0].startswith("ok worker image not built yet"), line)
                self.assertEqual(self.inspected(), [])

    def test_the_repo_lock_row_is_not_an_image_the_runner_would_use(self):
        """Doctor resolves as the runner does: no pin and no record is no image, whatever the
        repo's own lock says."""
        rc, line = self.image_line(record=None, tracked=True, free=30 * 10**9)
        self.assertTrue(line[0].startswith("ok worker image not built yet"), line)
        self.assertEqual(self.inspected(), [])

    def test_the_operators_pin_is_the_image_doctor_checks(self):
        with mock.patch.object(docker, "_ops_env_image", return_value="pinned:1"):
            rc, line = self.image_line(record=None, free=30 * 10**9)
        self.assertEqual((rc, line[0]), (0, "ok worker image present (pinned by OPS_WORKER_IMAGE)"))
        self.assertEqual(self.inspected(), ["docker image inspect pinned:1"])
        self.assertIn("ok free disk 25 GB", line[-1])

    def test_doctor_checks_the_image_the_runners_own_resolution_names(self):
        """D328: one resolution (`worker_image_source`) serves the runner, `capsule check` and
        doctor, so doctor inspects what it names and labels it by its source."""
        for source, label in (("environment OPS_WORKER_IMAGE", "pinned by OPS_WORKER_IMAGE"),
                              ("ops.env OPS_WORKER_IMAGE", "pinned by OPS_WORKER_IMAGE"),
                              ("capsule-python row", f"recorded in {self.home}/images.lock")):
            with self.subTest(source=source), mock.patch.object(
                    docker, "worker_image_source", return_value=("src:1", source)):
                rc, line = self.image_line(record=None)
            self.assertEqual((rc, line[0]), (0, f"ok worker image present ({label})"))
            self.assertEqual(self.inspected()[-1], "docker image inspect src:1")

    def test_a_legacy_image_loop_value_is_a_warning_naming_the_key(self):
        d = self.home / "boards" / "web1"
        d.mkdir(parents=True)
        (d / "config.json").write_text(json.dumps({"redesign": {"image_loop": "on"}}))
        rc, lines = self.doctor()
        self.assertTrue(any("redesign.image_loop" in ln and "warning" in ln for ln in lines), lines)

    def test_the_disk_ask_is_40_gb_while_the_image_is_absent_and_25_once_it_is_here(self):
        rc, line = self.image_line(env={"DOCKER_IMAGE_RC": "1"}, free=39 * 10**9)
        self.assertEqual(rc, 1)
        self.assertIn("MISSING free disk 40 GB -> free 1 GB more on the volume holding", line[-1])
        self.assertEqual(self.image_line(env={"DOCKER_IMAGE_RC": "1"}, free=40 * 10**9)[0], 0)
        rc, line = self.image_line(free=25 * 10**9)
        self.assertEqual(rc, 0, line)
        self.assertIn("ok free disk 25 GB", line[-1])

    def test_the_build_floor_is_the_one_figure_build_base_reads(self):
        figure = (paths.REPO / "docker" / "BUILD_MIN_FREE_BYTES").read_text().strip()
        self.assertEqual(doctor.BUILD_FREE_BYTES, int(figure))
        script = (paths.REPO / "docker" / "build-base.sh").read_text()
        self.assertIn("docker/BUILD_MIN_FREE_BYTES", script)
        self.assertNotRegex(script, r"40 \* 1000|need 40 GB")   # no second copy of the number
        rc, line = self.image_line(env={"DOCKER_IMAGE_RC": "1"}, free=doctor.BUILD_FREE_BYTES - 1)
        self.assertEqual(rc, 1)
        self.assertIn(f"MISSING free disk {int(figure) // 10**9} GB", line[-1])
        self.assertEqual(self.image_line(env={"DOCKER_IMAGE_RC": "1"},
                                         free=doctor.BUILD_FREE_BYTES)[0], 0)

    def test_doctor_follows_the_floor_file_not_a_number_of_its_own(self):
        (self.tmp / "docker").mkdir()
        floor = self.tmp / "docker" / "BUILD_MIN_FREE_BYTES"
        floor.write_text("52000000000\n")
        try:
            with mock.patch.object(docker, "BUILD_FLOOR_FILE", floor):
                importlib.reload(doctor)
                self.assertEqual(doctor.BUILD_FREE_BYTES, 52 * 10**9)
        finally:
            importlib.reload(doctor)
        self.assertEqual(doctor.BUILD_FREE_BYTES, 40 * 10**9)

    def test_the_build_note_names_the_floor_files_figure(self):
        (self.tmp / "docker").mkdir()
        (self.tmp / "docker" / "BUILD_MIN_FREE_BYTES").write_text("52000000000\n")
        code = ("import sys; from pathlib import Path; "
                "from capsule import config, docker_door, paths; "   # these need the real REPO
                "paths.REPO = Path(sys.argv[1]); from capsule.runners import docker; "
                "print(docker.BUILD_NOTE)")
        out = subprocess.run([sys.executable, "-P", "-c", code, str(self.tmp)], text=True,
                             capture_output=True, cwd=paths.REPO, check=True,
                             env={**os.environ, "PYTHONPATH": str(paths.REPO)}).stdout
        self.assertEqual(out.strip(), "needs 52 GB free and takes hours")
        self.assertEqual(docker.BUILD_NOTE, "needs 40 GB free and takes hours")
        (self.tmp / "docker" / "BUILD_MIN_FREE_BYTES").write_text("4e10\n")
        out = subprocess.run([sys.executable, "-P", "-c", code, str(self.tmp)], text=True,
                             capture_output=True, cwd=paths.REPO, check=True,
                             env={**os.environ, "PYTHONPATH": str(paths.REPO)}).stdout
        self.assertEqual(out.strip(), "needs a lot of free disk and takes hours")

    def test_the_floor_file_is_read_by_one_rule_that_build_base_applies_too(self):
        """Mirrors tests/test_docker_base_image.sh, which feeds the same inputs to build-base.sh."""
        floor = self.tmp / "BUILD_MIN_FREE_BYTES"
        good = {b"40000000000\n": 40 * 10**9, b"  999999999999999 \n": 999999999999999,
                b"\n 40000000000\n\n": 40 * 10**9, b"1": 1}
        bad = (b"", b"   \n", b"4e10\n", b"040000000000\n", b"40 000\n", b"-5\n", b"+4\n",
               b"40_000_000_000\n", b"40000000000\r\n", b"40000000000\t\n",
               b"12345678901234567890\n", b"1000000000000000\n", b"40000000000\nx\n", b"0\n")
        with mock.patch.object(docker, "BUILD_FLOOR_FILE", floor):
            for raw, want in good.items():
                floor.write_bytes(raw)
                self.assertEqual(docker.build_floor(), want, raw)
            for raw in bad:
                floor.write_bytes(raw)
                self.assertIsNone(docker.build_floor(), raw)
        with mock.patch.object(docker, "BUILD_FLOOR_FILE", self.tmp / "absent"):
            self.assertIsNone(docker.build_floor())

    def test_a_missing_or_garbled_floor_file_fails_the_disk_check_and_names_the_file(self):
        floor = self.tmp / "docker" / "BUILD_MIN_FREE_BYTES"
        (self.tmp / "docker").mkdir()
        for text in (None, "forty gigabytes\n"):
            if text is not None:
                floor.write_text(text)
            try:
                with mock.patch.object(docker, "BUILD_FLOOR_FILE", floor):
                    importlib.reload(doctor)             # must not raise
                    self.assertIsNone(doctor.BUILD_FREE_BYTES)
                    rc, line = self.image_line(env={"DOCKER_IMAGE_RC": "1"})
            finally:
                importlib.reload(doctor)
            self.assertEqual(rc, 1, line)
            want = f"MISSING free disk (build floor unreadable) -> restore {floor}"
            self.assertIn(want, line[-1])
        self.assertEqual(doctor.BUILD_FREE_BYTES, 40 * 10**9)


class ReadOnly(_World):
    def snapshot(self):
        return sorted(str(p.relative_to(self.tmp)) for p in self.tmp.rglob("*")
                      if p != self.log and p.name != "calls.log")

    def test_nothing_is_written_started_or_read(self):
        secrets = self.home / "secrets.env"
        secrets.write_text(f"TOKEN={SECRET}\n")
        secrets.chmod(0)
        cfg = self.user / ".config" / "capsule"
        cfg.mkdir(parents=True)
        (cfg / "token").write_text(SECRET)
        (cfg / "token").chmod(0)
        before = self.snapshot()
        rc, lines = self.doctor()
        secrets.chmod(0o600)
        (cfg / "token").chmod(0o600)
        self.assertEqual(rc, 0, lines)
        self.assertEqual(self.snapshot(), before)
        self.assertNotIn(SECRET, "\n".join(lines))
        self.assertEqual(secrets.read_text(), f"TOKEN={SECRET}\n")
        calls = self.calls()
        self.assertFalse([c for c in calls if c.startswith(("launchctl", "brew"))], calls)
        self.assertFalse([c for c in calls if "compose" in c], calls)
        self.assertEqual({c.split()[0] for c in calls}, {"docker", "claude", "gh"})


# Z1: the handlers behind contracts/intent/doctor.jsonl.
CASES = {
    "one-missing-is-exit-1": (
        {"check": "doctor", "world": "everything present except one tool", "missing": "gh"},
        {"exit": 1, "missing_lines": 1, "line_names_the_tool": True}),
    "fix-text-names-the-command": (
        {"check": "doctor", "world": "everything present except one tool", "missing": "claude"},
        {"fix": "brew install --cask claude-code@latest"}),
    "existing-board-refused": (
        {"check": "board_new", "world": "a board with that name already has a config"},
        {"exit": 1, "refusal_reason": "exists", "refusal_names_the_board": True,
         "config_unchanged": True}),
    "new-board-runs-the-recorded-image": (
        {"check": "board_new", "world": "a new board is made with `capsule board new`"},
        {"exit": 0, "image_loop": "record"}),
    "absent-image-asks-40-gb": (
        {"check": "doctor", "world": "everything present, no image recorded or built, 35 GB free",
         "free_gb": 35},
        {"exit": 1, "image_line": "ok, says not built yet and names docker/build-base.sh base"
                                  " && docker/build-base.sh python",
         "disk_fix": "free 5 GB more on the volume holding"}),
}


class IntentCases(_World):
    def restub(self):
        for tool in TOOLS:
            self.stub(tool)

    def observe_image(self, document):
        """doctor with the image absent everywhere (no record, no tracked row) and `free_gb`."""
        lock = self.tmp / "none.lock"
        with mock.patch.object(docker, "LOCK_FILE", lock):
            rc, lines = self.doctor("--offline", env={"DOCKER_IMAGE_RC": "1"},
                                    free=document["free_gb"] * 10**9)
        line = next(ln for ln in lines if "worker image" in ln)
        disk = next(ln for ln in lines if "free disk" in ln)
        build = "docker/build-base.sh base && docker/build-base.sh python"
        fix = disk.partition(" -> ")[2].partition(" holding")[0] + " holding"
        return {"exit": rc, "disk_fix": fix,
                "image_line": "ok, says not built yet and names " + build
                if line.startswith("ok") and "not built yet" in line and build in line else line}

    def observe(self, document):
        if document["check"] == "doctor" and "free_gb" in document:
            return self.observe_image(document)
        if document["check"] == "board_new" and "new board" in document["world"]:
            r = board_new(self.home, "fresh", "--repo", "https://github.com/you/one.git")
            cfg = json.loads((self.home / "boards" / "fresh" / "config.json").read_text())
            loop = cfg.get("redesign", {}).get("image_loop", "record")   # a missing key is record
            return {"exit": r.returncode, "image_loop": loop}
        if document["check"] == "board_new":
            home = self.home
            board_new(home, "demo", "--repo", "https://github.com/you/one.git")
            path = home / "boards" / "demo" / "config.json"
            before = path.read_bytes()
            r = board_new(home, "demo", "--repo", "https://github.com/you/two.git")
            reason = r.stderr.partition("refused: ")[2].strip()
            return {"exit": r.returncode, "refusal_names_the_board": "'demo'" in r.stderr,
                    "refusal_reason": "exists" if reason.startswith("board 'demo' already exists")
                    else reason,
                    "config_unchanged": path.read_bytes() == before}
        tool = document["missing"]
        (self.bin / tool).unlink()
        rc, lines = self.doctor("--offline")
        gone = self.missing(lines)
        what = gone[0].removeprefix("MISSING ").split(" -> ", 1)[0] if len(gone) == 1 else ""
        return {"exit": rc, "missing_lines": len(gone),
                "line_names_the_tool": what.split(" ")[0] == tool,
                "fix": gone[0].split(" -> ", 1)[1] if len(gone) == 1 else ""}

    def assert_case(self, name, document, want):
        got = self.observe(document)
        self.assertTrue(intent.matches(want, got), f"{name}: wanted {want}, got {got}")

    def test_the_built_in_expectations(self):
        for name, (document, want) in CASES.items():
            with self.subTest(name):
                self.restub()
                self.assert_case(name, document, want)

    def test_the_shipped_rows(self):
        if not intent.role_path("doctor").is_file():
            self.skipTest("contracts/intent/doctor.jsonl is not shipped yet")
        rows = intent.load("doctor")
        self.assertEqual({r["id"] for r in rows}, set(CASES))
        for row in rows:
            with self.subTest(row["id"]):
                self.restub()
                self.assert_case(row["id"], row["given"]["document"], row["expect"])


class Help(unittest.TestCase):
    def test_help_describes_what_doctor_checks(self):
        r = subprocess.run([sys.executable, "-m", "capsule", "doctor", "--help"],
                           cwd=paths.REPO, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0)
        body = [x for x in r.stdout.splitlines()[1:]
                if x.strip() and not x.startswith(("options:", "optional arguments:", "  "))]
        self.assertTrue(any("prerequisite" in x for x in body), r.stdout)


if __name__ == "__main__":
    unittest.main()


def _fake_probe(swap, top="", swap_rc=0):
    """A `doctor._probe` that answers sysctl and top from text, never running either."""
    def probe(*cmd):
        out, rc = (swap, swap_rc) if cmd[0] == "sysctl" else (top, 0)
        return SimpleNamespace(stdout=out, returncode=rc)
    return probe


def _swap(used, top="", free=10**12, system="Darwin", swap_rc=0):
    text = used if " " in used else f"total = 30720.00M  used = {used}  free = 1.00M\n"
    with mock.patch.object(doctor.platform, "system", return_value=system), \
            mock.patch.object(doctor, "_probe", _fake_probe(text, top, swap_rc)), \
            mock.patch.object(doctor.shutil, "disk_usage", return_value=USAGE(10**13, 0, free)):
        return doctor.swap_check()


TOP_FSEVENTSD = ("Processes: 400 total\nCOMMAND          MEM\nfseventsd        36G\n"
                 "kernel_task      2G\n")
TOP_OTHER = "Processes: 400 total\nCOMMAND          MEM\nGoogle Chrome H  1234M+\n"


class SwapCheck(unittest.TestCase):
    def test_below_threshold_is_ok(self):
        self.assertEqual(_swap("2048.00M"), ("ok", "swap use is 2.0 GB"))

    def test_8gb_warns_and_names_the_biggest_process(self):
        level, line = _swap("8192.00M", TOP_OTHER)
        self.assertEqual(level, "warn")
        self.assertIn("the biggest memory user is Google Chrome H at 1234M", line)

    def test_20gb_fails_by_size(self):
        self.assertEqual(_swap("20480.00M", TOP_OTHER)[0], "fail")
        self.assertEqual(_swap("21.5G", TOP_OTHER)[0], "fail")

    def test_8gb_with_disk_under_25gb_fails(self):
        level, line = _swap("9000.00M", TOP_OTHER, free=15 * 10**9)
        self.assertEqual(level, "fail")
        self.assertIn("disk is under 25 GB free", line)

    def test_low_disk_with_small_swap_is_still_ok(self):
        self.assertEqual(_swap("1000.00M", free=15 * 10**9)[0], "ok")

    def test_fseventsd_gets_the_killall_advice(self):
        level, line = _swap("10240.00M", TOP_FSEVENTSD)
        self.assertEqual(level, "warn")
        self.assertIn("fseventsd holds 36G", line)
        self.assertIn("`sudo killall fseventsd`", line)
        self.assertIn("normal terminal", line)

    def test_other_process_is_named_without_killall_advice(self):
        _, line = _swap("10240.00M", TOP_OTHER)
        self.assertNotIn("killall", line)

    def test_unreadable_top_still_reports_the_swap(self):
        level, line = _swap("10240.00M", "garbage")
        self.assertEqual((level, line), ("warn", "swap use is 10.0 GB"))

    def test_unparseable_or_failed_sysctl_is_skipped(self):
        self.assertIsNone(_swap("not swap output at all"))
        self.assertIsNone(_swap("total = 1M  used = 1M", swap_rc=1))
        self.assertIsNone(_swap(""))

    def test_not_darwin_is_skipped_without_running_anything(self):
        with mock.patch.object(doctor, "_probe", side_effect=AssertionError("ran a command")):
            with mock.patch.object(doctor.platform, "system", return_value="Linux"):
                self.assertIsNone(doctor.swap_check())

    def test_a_timeout_is_skipped(self):
        with mock.patch.object(doctor.platform, "system", return_value="Darwin"), \
                mock.patch.object(doctor, "_probe",
                                  side_effect=subprocess.TimeoutExpired("x", 1)):
            self.assertIsNone(doctor.swap_check())

    def test_fail_is_a_missing_row_and_warn_is_a_warning_line_only(self):
        fail = doctor.run(swap=("fail", "swap use is 25.0 GB"))
        self.assertIn(("swap use", "swap use is 25.0 GB"), fail)
        warn = doctor.run(swap=("warn", "swap use is 9.0 GB"))
        self.assertNotIn("swap use", [what for what, _ in warn])
