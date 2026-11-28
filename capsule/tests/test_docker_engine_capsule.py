"""Docker engine switch, capsule side (docs/contracts/docker-engine-switch.md): doctor messages
and the
install skip rule, for both engines. Same fake worlds as test_doctor.py and test_install.py: temp
HOME,
CAPSULE_HOME and AGENT_OPS_STATE_DIR, stub tools in a temp dir; never the real Docker."""
import os
import pathlib
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from capsule import doctor, install
from capsule.tests.test_doctor import _World
from capsule.tests.test_install import _Install

ENGINES = {
    "desktop": dict(title="Docker Desktop", hint="open -a Docker", cask="docker-desktop"),
    "orbstack": dict(title="OrbStack", hint="open -a OrbStack", cask="orbstack"),
}


class _Engine:
    def engine_env(self, engine, **extra):
        return {"CAPSULE_DOCKER_ENGINE": engine, "AGENT_OPS_STATE_DIR": str(self.tmp / "state"),
                "AGENT_OPS_NO_SEND": "1", **extra}


class DoctorMessages(_Engine, _World):
    def missing(self, lines):
        return [ln for ln in lines if ln.startswith("MISSING Docker")]

    def test_timeout_message(self):
        # contract Messages: doctor timeout, both engines
        want = {"desktop": "Docker is not responding: restart Docker Desktop "
                           "(open -a Docker), then rerun",
                "orbstack": "Docker is not responding: restart OrbStack "
                            "(open -a OrbStack), then rerun"}
        for engine, text in want.items():
            with self.subTest(engine):
                hang = subprocess.TimeoutExpired("docker", 20)
                with mock.patch.object(doctor.docker_door, "docker_run", side_effect=hang):
                    rc, lines = self.doctor(env=self.engine_env(engine))
                self.assertEqual(rc, 1)
                self.assertEqual(self.missing(lines), [f"MISSING Docker daemon running -> {text}"])

    def test_missing_cli_message(self):
        # contract Messages: doctor missing CLI (OSError), both engines
        want = {"desktop": "install Docker Desktop: brew install --cask docker-desktop, "
                           "then open -a Docker",
                "orbstack": "install OrbStack: brew install --cask orbstack, then open -a OrbStack"}
        for engine, text in want.items():
            with self.subTest(engine):
                with mock.patch.object(doctor.docker_door, "docker_run",
                                       side_effect=FileNotFoundError("docker")):
                    rc, lines = self.doctor(env=self.engine_env(engine))
                self.assertEqual(rc, 1)
                self.assertEqual(self.missing(lines), [f"MISSING Docker daemon running -> {text}"])

    def test_nonzero_exit_message(self):
        # contract Messages: doctor nonzero exit, both engines
        want = {"desktop": "start Docker Desktop: open -a Docker, then rerun",
                "orbstack": "start OrbStack: open -a OrbStack, then rerun"}
        for engine, text in want.items():
            with self.subTest(engine):
                rc, lines = self.doctor(env=self.engine_env(engine, DOCKER_VERSION_RC="1"))
                self.assertEqual(rc, 1)
                self.assertEqual(self.missing(lines), [f"MISSING Docker daemon running -> {text}"])

    def test_orbstack_messages_never_name_docker_desktop(self):
        # contract: user-facing text uses the title; orbstack text carries no desktop wording
        rc, lines = self.doctor(env=self.engine_env("orbstack", DOCKER_VERSION_RC="1"))
        self.assertNotIn("Docker Desktop", "\n".join(self.missing(lines)))
        self.assertNotIn("open -a Docker,", "\n".join(self.missing(lines)))

    def test_state_file_selects_the_message_engine(self):
        # contract resolution order: file docker-engine = orbstack, env unset (intent
        # file-flips-every-job)
        state = self.tmp / "state"
        state.mkdir()
        (state / "docker-engine").write_text("orbstack\n")
        env = self.engine_env("orbstack", DOCKER_VERSION_RC="1")
        del env["CAPSULE_DOCKER_ENGINE"]
        rc, lines = self.doctor(env=env)
        self.assertEqual(self.missing(lines),
                         ["MISSING Docker daemon running -> start OrbStack: "
                          "open -a OrbStack, then rerun"])


class InstallSkipRule(_Engine, _Install):
    """The Brewfile names the default engine's cask (`orbstack`); the skip rule is keyed on the
    selected engine."""

    def setUp(self):
        super().setUp()
        (self.bin / "docker").unlink()                       # docker not found by doctor
        self.fake = self.tmp / "fake-docker"
        self.fake.mkdir()
        (self.fake / "docker").write_text("#!/bin/sh\nexit 1\n")
        (self.fake / "docker").chmod(0o755)
        self.orb_app = self.tmp / "OrbStack.app"

    def run_install(self, engine, app):
        """Install --yes with DOCKER_APP pointed at `app` (the engine's app path override)."""
        self.docker_app = app
        env = self.engine_env(engine, AGENT_OPS_DOCKER_BIN=str(self.fake))
        _, lines, _ = self.install("--yes", env=env)
        return lines

    def casks(self):
        return re.findall(r'^cask "([^"]+)"', self.brewfile_sent(), re.M)

    def brewfile_sent(self):
        from pathlib import Path
        return Path(f"{self.log}.stdin").read_text()

    def test_orbstack_with_app_present_skips_the_docker_cask(self):
        # contract Install: orbstack, OrbStack.app exists -> orbstack cask skipped
        self.orb_app.mkdir()
        lines = self.run_install("orbstack", self.orb_app)
        self.assertNotIn("orbstack", self.casks())
        self.assertTrue([ln for ln in lines if ln.startswith("skip cask orbstack")], lines)

    def test_orbstack_without_app_keeps_the_cask(self):
        # contract Install, orbstack default: the cask line stays when the app is absent
        lines = self.run_install("orbstack", self.orb_app)
        self.assertIn("orbstack", self.casks())
        self.assertFalse([ln for ln in lines if ln.startswith("skip cask orbstack")], lines)

    def test_orbstack_with_only_docker_app_does_not_skip(self):
        # contract Install: orbstack, only Docker.app exists -> orbstack cask NOT skipped;
        # intent old-docker-app-does-not-block-orbstack-install
        (self.tmp / "Docker.app").mkdir()
        lines = self.run_install("orbstack", self.orb_app)           # OrbStack.app absent
        self.assertIn("orbstack", self.casks())                      # the engine's cask line stays
        self.assertFalse([ln for ln in lines if ln.startswith("skip cask orbstack")], lines)

    def test_desktop_app_present_never_skips_the_orbstack_cask_line(self):
        # contract Install note: the Brewfile says orbstack; under desktop only the engine's own
        # cask (docker-desktop) maps to the docker tool, so the orbstack line is kept
        # (an engine-blind rule would skip it because an app exists at DOCKER_APP).
        app = self.tmp / "Docker.app"
        app.mkdir()
        lines = self.run_install("desktop", app)
        self.assertIn("orbstack", self.casks())
        self.assertFalse([ln for ln in lines if ln.startswith("skip cask orbstack")], lines)

    def test_desktop_with_docker_cli_found_keeps_the_orbstack_cask_line(self):
        # contract Install note (same reason as above): a found docker does not skip orbstack
        # under desktop; the skip rule only maps the engine's own cask to the docker tool.
        (self.bin / "docker").write_text("#!/bin/sh\nexit 0\n")
        (self.bin / "docker").chmod(0o755)
        self.docker_app = self.tmp / "Docker.app"
        env = self.engine_env("desktop", AGENT_OPS_DOCKER_BIN=str(self.bin))
        _, lines, _ = self.install("--yes", env=env)
        self.assertIn("orbstack", self.casks())

    def test_claude_cask_skip_is_unchanged_under_orbstack(self):
        # contract Install: any engine, `claude` found -> claude-code@latest skipped
        (self.bin / "claude").write_text("#!/bin/sh\nexit 0\n")
        (self.bin / "claude").chmod(0o755)
        self.run_install("orbstack", self.orb_app)
        self.assertNotIn("claude-code@latest", self.casks())

    def test_claude_cask_kept_when_claude_missing(self):
        # contract Install: unchanged rule, the other side
        (self.bin / "claude").unlink(missing_ok=True)
        self.run_install("orbstack", self.orb_app)
        self.assertIn("claude-code@latest", self.casks())


class _Plan:
    def __init__(self):
        self.said = []

    def say(self, kind, text):
        self.said.append((kind, text))


class BrewfileSkipRule(unittest.TestCase):
    """`_brewfile_without_installed_casks(plan, brewfile)` called directly, temp Brewfile."""
    BREWFILE = 'brew "uv"\ncask "orbstack"\ncask "docker-desktop"\n'

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="brewfile-skip-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.brewfile = self.tmp / "Brewfile"
        self.brewfile.write_text(self.BREWFILE)
        self.fake = self.tmp / "fake"
        self.fake.mkdir()

    def kept(self, engine, app, docker_ok=False):
        """(kept lines, plan) with the engine selected, DOCKER_APP=app, docker found or not."""
        (self.fake / "docker").write_text(f"#!/bin/sh\nexit {0 if docker_ok else 1}\n")
        (self.fake / "docker").chmod(0o755)
        env = {"CAPSULE_DOCKER_ENGINE": engine, "AGENT_OPS_STATE_DIR": str(self.tmp / "state"),
               "AGENT_OPS_DOCKER_BIN": str(self.fake), "AGENT_OPS_NO_SEND": "1",
               "PATH": (f"{self.fake}:" if docker_ok else "") + "/usr/bin:/bin",
               "HOME": str(self.tmp)}
        plan = _Plan()
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(install, "DOCKER_APP", app):
            lines = install._brewfile_without_installed_casks(plan, self.brewfile)
        text = lines if isinstance(lines, str) else "\n".join(lines)
        return [ln.strip() for ln in text.splitlines()], plan

    def present(self):
        app = self.tmp / "App.app"
        app.mkdir(exist_ok=True)
        return app

    def test_orbstack_app_exists_skips_the_orbstack_line_with_a_skip_plan_line(self):
        # contract Install: orbstack, engine app path exists -> orbstack cask skipped + `skip` line
        kept, plan = self.kept("orbstack", self.present())
        self.assertNotIn('cask "orbstack"', kept)
        self.assertIn('cask "docker-desktop"', kept)
        self.assertIn('brew "uv"', kept)
        self.assertEqual([k for k, _ in plan.said], ["skip"])
        self.assertIn("orbstack", plan.said[0][1])

    def test_orbstack_app_missing_and_docker_not_found_keeps_the_line(self):
        # contract Install: DOCKER_APP at a missing path, doctor finds no docker -> kept
        kept, plan = self.kept("orbstack", self.tmp / "missing.app")
        self.assertIn('cask "orbstack"', kept)
        self.assertEqual(plan.said, [])

    def test_docker_found_by_doctor_skips_the_engine_cask(self):
        # contract Install: skipped "when `docker` is found by doctor", whatever the app path
        kept, plan = self.kept("orbstack", self.tmp / "missing.app", docker_ok=True)
        self.assertNotIn('cask "orbstack"', kept)
        self.assertEqual([k for k, _ in plan.said], ["skip"])

    def test_desktop_follows_the_same_rule_on_its_own_cask(self):
        # contract Install: under desktop the docker-desktop line follows the same rule
        kept, plan = self.kept("desktop", self.present())
        self.assertNotIn('cask "docker-desktop"', kept)
        self.assertIn('cask "orbstack"', kept)
        self.assertEqual([k for k, _ in plan.said], ["skip"])
        kept, plan = self.kept("desktop", self.tmp / "missing.app")
        self.assertIn('cask "docker-desktop"', kept)
        self.assertEqual(plan.said, [])

    def probed(self, engine):
        seen = []
        real_exists, real_isdir = os.path.exists, os.path.isdir

        def path_exists(self_, *a, **k):
            seen.append(str(self_))
            return False

        def os_exists(path):
            seen.append(str(path))
            return False

        with mock.patch.object(pathlib.Path, "exists", path_exists), \
                mock.patch.object(pathlib.Path, "is_dir", path_exists), \
                mock.patch.object(os.path, "exists", os_exists), \
                mock.patch.object(os.path, "isdir", os_exists):
            self.kept(engine, None)
        self.assertTrue(real_exists and real_isdir)
        return seen

    def test_without_an_override_the_engine_app_path_is_probed(self):
        # contract Install: DOCKER_APP=None -> the engine's app path (orbstack:
        # /Applications/OrbStack.app, desktop: /Applications/Docker.app)
        self.assertIn("/Applications/OrbStack.app", self.probed("orbstack"))
        self.assertNotIn("/Applications/Docker.app", self.probed("orbstack"))
        self.assertIn("/Applications/Docker.app", self.probed("desktop"))
        self.assertNotIn("/Applications/OrbStack.app", self.probed("desktop"))


if __name__ == "__main__":
    unittest.main()
