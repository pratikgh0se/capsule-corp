"""Docker engine switch: docker_bin, docker_env and the live-install guard (symlinked CLIs
included).
Temp HOME, state dir and fake docker dirs only; never the live Docker."""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "maintenance"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import events  # noqa: E402
from capsule.tests.docker_engine_support import DESKTOP_BIN, Sandbox  # noqa: E402


class DockerEnv(Sandbox):
    def test_override_wins_for_bin_and_path_either_engine(self):
        # contract: AGENT_OPS_DOCKER_BIN=/x/fake, any engine: docker_bin()==override, PATH starts
        # with it
        for engine in ("desktop", "orbstack"):
            with self.subTest(engine):
                env = self.env(engine)
                self.assertEqual(self.py(env, events.docker_bin), str(self.fake))
                self.assertTrue(self.py(env, events.docker_env)["PATH"].startswith(f"{self.fake}:"))

    def test_override_still_pins_context(self):
        # contract: with AGENT_OPS_DOCKER_BIN set, DOCKER_CONTEXT is still pinned (intent
        # override-keeps-precedence)
        for engine, ctx in (("desktop", "desktop-linux"), ("orbstack", "orbstack")):
            with self.subTest(engine):
                out = self.py(self.env(engine), events.docker_env)
                self.assertEqual(out["DOCKER_CONTEXT"], ctx)

    def test_default_without_override(self):
        # contract: No override, default (orbstack): PATH starts with <HOME>/.orbstack/bin, context
        # orbstack
        env = self.env(None, override=False)
        out = self.py(env, lambda: events.docker_env(guard=False))
        self.assertTrue(out["PATH"].startswith(f"{self.home}/.orbstack/bin:"), out["PATH"])
        self.assertEqual(out["DOCKER_CONTEXT"], "orbstack")
        self.assertEqual(self.py(env, events.docker_bin), f"{self.home}/.orbstack/bin")

    def test_desktop_without_override(self):
        # contract: No override, desktop: PATH starts with the Docker.app bin dir, context
        # desktop-linux
        env = self.env("desktop", override=False)
        out = self.py(env, lambda: events.docker_env(guard=False))
        self.assertTrue(out["PATH"].startswith(DESKTOP_BIN + ":"), out["PATH"])
        self.assertEqual(out["DOCKER_CONTEXT"], "desktop-linux")
        self.assertEqual(self.py(env, events.docker_bin), DESKTOP_BIN)

    def test_orbstack_without_override_via_env(self):
        # contract: No override, orbstack: PATH starts with <HOME>/.orbstack/bin:, context orbstack
        env = self.env("orbstack", override=False)
        out = self.py(env, lambda: events.docker_env(guard=False))
        self.assertTrue(out["PATH"].startswith(f"{self.home}/.orbstack/bin:"), out["PATH"])
        self.assertEqual(out["DOCKER_CONTEXT"], "orbstack")
        self.assertEqual(self.py(env, events.docker_bin), f"{self.home}/.orbstack/bin")

    def test_orbstack_via_state_file_flips_docker_env(self):
        # intent file-flips-every-job: file orbstack, no env
        self.engine_file("orbstack\n")
        out = self.py(self.env(None, override=False), lambda: events.docker_env(guard=False))
        self.assertTrue(out["PATH"].startswith(f"{self.home}/.orbstack/bin:"), out["PATH"])
        self.assertEqual(out["DOCKER_CONTEXT"], "orbstack")

    def test_context_pin_beats_callers_context(self):
        # contract: DOCKER_CONTEXT set even when the caller's env holds a different one; intent
        # context-is-pinned
        for engine, ctx in (("desktop", "desktop-linux"), ("orbstack", "orbstack")):
            for override in (True, False):
                with self.subTest(engine=engine, override=override):
                    env = self.env(engine, override=override, DOCKER_CONTEXT="colima")
                    out = self.py(env, lambda: events.docker_env(guard=False))
                    self.assertEqual(out["DOCKER_CONTEXT"], ctx)

    def test_unknown_engine_raises(self):
        # contract: Unknown engine: docker_env() and docker_bin() raise ValueError (no override)
        env = self.env("podman", override=False)
        for fn in (lambda: events.docker_env(guard=False), events.docker_bin):
            with self.subTest(fn):
                with self.assertRaises(ValueError):
                    self.py(env, fn)

    def test_unknown_engine_with_override(self):
        # contract: with override set docker_bin() returns it but docker_env() still raises
        env = self.env("podman")
        self.assertEqual(self.py(env, events.docker_bin), str(self.fake))
        with self.assertRaises(ValueError):
            self.py(env, events.docker_env)

    def test_unknown_state_file_raises_in_docker_env(self):
        # contract: resolution row 8 reaches docker_env: no silent desktop fallback
        self.engine_file("podman")
        with self.assertRaises(ValueError):
            self.py(self.env(None, override=False), lambda: events.docker_env(guard=False))

    def test_engine_row_resolves_to_the_selected_engine(self):
        # contract: docker_engine_row() exists on events (RUNBOOK "Check" command); it names the
        # engine
        for engine in ("desktop", "orbstack"):
            with self.subTest(engine):
                row = self.py(self.env(engine), events.docker_engine_row)
                self.assertIn(engine, str(row))


class Guard(Sandbox):
    """Live-install guard under unittest (guard=True): no NO_SEND marker, all docker dirs temp."""

    def guard_env(self, engine=None, **extra):
        env = self.env(engine, **extra)
        env.pop("AGENT_OPS_NO_SEND")
        return env

    def live_orbstack(self):
        d = self.home / ".orbstack" / "bin"
        d.mkdir(parents=True)
        (d / "docker").write_text("#!/bin/sh\nexit 0\n")
        (d / "docker").chmod(0o755)
        return d / "docker"

    def link_dir(self, target):
        d = Path(tempfile.mkdtemp(dir=self.tmp))
        (d / "docker").symlink_to(target)
        return d

    def test_real_file_fake_does_not_raise_either_engine(self):
        # contract guard row 1 (real-file fake dir, engine either) + override-keeps-precedence
        # intent
        self.live_orbstack()
        for engine in ("desktop", "orbstack"):
            with self.subTest(engine):
                out = self.py(self.guard_env(engine), events.docker_env)
                self.assertTrue(out["PATH"].startswith(f"{self.fake}:"))

    def test_bare_docker_without_override_raises(self):
        # contract guard row 2: override unset, bare docker on PATH -> RuntimeError live ops-pg
        env = self.guard_env(None, override=False, PATH=f"{self.fake}:/usr/bin:/bin")
        with self.assertRaisesRegex(RuntimeError, "a test process resolved the live ops-pg"):
            self.py(env, events.docker_env)

    def test_symlink_to_orbstack_docker_raises_even_under_desktop(self):
        # contract guard row 3; intent guard-sees-symlinked-cli
        live = self.live_orbstack()
        link = self.link_dir(live)
        for engine in ("desktop", "orbstack"):
            with self.subTest(engine):
                env = self.guard_env(engine, AGENT_OPS_DOCKER_BIN=str(link))
                with self.assertRaisesRegex(RuntimeError, "live ops-pg"):
                    self.py(env, events.docker_env)

    def test_chain_of_symlinks_to_orbstack_docker_raises(self):
        # contract: "/usr/local/bin/docker -> ~/.orbstack/bin/docker -> .../docker" resolves to
        # one file
        live = self.live_orbstack()
        hop = Path(tempfile.mkdtemp(dir=self.tmp)) / "docker"
        hop.symlink_to(live)
        link = self.link_dir(hop)
        env = self.guard_env("desktop", AGENT_OPS_DOCKER_BIN=str(link))
        with self.assertRaisesRegex(RuntimeError, "live ops-pg"):
            self.py(env, events.docker_env)

    def test_symlink_to_docker_on_path_raises_under_desktop(self):
        # contract guard row 3 ("or on PATH")
        on_path = Path(tempfile.mkdtemp(dir=self.tmp))
        (on_path / "docker").write_text("#!/bin/sh\nexit 0\n")
        (on_path / "docker").chmod(0o755)
        link = self.link_dir(on_path / "docker")
        env = self.guard_env("desktop", AGENT_OPS_DOCKER_BIN=str(link),
                             PATH=f"{on_path}:/usr/bin:/bin")
        with self.assertRaisesRegex(RuntimeError, "live ops-pg"):
            self.py(env, events.docker_env)

    def test_absolute_docker_that_is_a_link_to_live_raises(self):
        # contract guard row 4: absolute docker= path whose realpath is a live engine docker
        live = self.live_orbstack()
        link = self.link_dir(live)
        for engine in ("orbstack", "desktop"):
            with self.subTest(engine):
                with self.assertRaisesRegex(RuntimeError, "live ops-pg"):
                    self.py(self.guard_env(engine), lambda: events.docker_run(
                        ["info"], docker=str(link / "docker")))

    def test_symlink_free_fake_with_orbstack_does_not_raise(self):
        # contract guard row 5: fake dir symlink-free, engine orbstack -> no raise
        self.live_orbstack()
        ran = []
        env = self.guard_env("orbstack")
        self.py(env, lambda: events.docker_run(["info"], lambda argv, **kw: ran.append(argv)))
        self.assertEqual(len(ran), 1)

    def test_override_dir_without_docker_still_raises_under_orbstack(self):
        # contract guard row 2 generalised: an override dir that holds no docker falls through to
        # live
        empty = Path(tempfile.mkdtemp(dir=self.tmp))
        env = self.guard_env("orbstack", AGENT_OPS_DOCKER_BIN=str(empty),
                             PATH=f"{self.live_orbstack().parent}:/usr/bin:/bin")
        with self.assertRaisesRegex(RuntimeError, "live ops-pg"):
            self.py(env, events.docker_env)


if __name__ == "__main__":
    unittest.main()
