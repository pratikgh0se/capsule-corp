"""Docker engine switch, docker_idle (docs/contracts/docker-engine-switch.md, section docker_idle):
the idle quit names the engine's app, user-facing text names its title. Same shape as
test_docker_idle's fake world (fake docker/osascript/ps in AGENT_OPS_DOCKER_BIN, temp state)."""
import contextlib
import io
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import docker_idle  # noqa: E402

DOCKER = """#!/bin/sh
echo "docker $*" >> "$FAKE_LOG"
case "$1" in
  ps) cat "$FAKE_DIR/containers"; exit 0;;
  compose) [ -f "$FAKE_DIR/spawn" ] && printf 'capsule-run-late\\t\\n' >> "$FAKE_DIR/containers"
    exit 0;;
esac
"""
OSA = '#!/bin/sh\necho "osascript $*" >> "$FAKE_LOG"\n'
PS = '#!/bin/sh\necho /sbin/launchd\n'
STACK = "capsule-corp-postgres\tcapsule-corp\ncapsule-corp-grafana\tcapsule-corp\n"
T0 = 1_000_000.0
MIN = 60


class World(unittest.TestCase):
    """Fake docker, osascript and ps in AGENT_OPS_DOCKER_BIN, a temp state dir, a fake clock."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="docker-engine-idle-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.bin, self.fd = self.tmp / "bin", self.tmp / "fd"
        self.bin.mkdir()
        self.fd.mkdir()
        self.log = self.tmp / "fake.log"
        self.log.write_text("")
        for name, body in (("docker", DOCKER), ("osascript", OSA), ("ps", PS)):
            (self.bin / name).write_text(body)
            (self.bin / name).chmod(0o755)
        (self.fd / "containers").write_text(STACK)
        env = {"PATH": f"{self.bin}:/usr/bin:/bin", "AGENT_OPS_DOCKER_BIN": str(self.bin),
               "AGENT_OPS_STATE_DIR": str(self.tmp / "state"),
               "CAPSULE_HOME": str(self.tmp / "home"), "FAKE_LOG": str(self.log),
               "FAKE_DIR": str(self.fd)}
        self.enterContext(mock.patch.dict(os.environ, env))
        os.environ.pop("OPS_DOCKER_IDLE_MIN", None)
        self.now, self.at = T0, 0
        self.enterContext(mock.patch.object(docker_idle, "_now", lambda: self.now))

    def calls(self):
        return self.log.read_text().splitlines()

    def acted(self):
        return [c for c in self.calls() if c.startswith(("docker compose", "osascript"))]

    def go(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = docker_idle.main(list(args))
        return rc, out.getvalue()

    def tick(self, minutes, *args):
        """Run at `minutes`; with --apply, every 5 minutes since the last run first."""
        apply = not args
        for m in range(self.at + 5, minutes, 5) if apply else ():
            self.now = T0 + m * MIN
            self.go("--apply")
        self.at, self.now = minutes, T0 + minutes * MIN
        return self.go(*(args or ("--apply",)))


class EngineIdle(World):
    engine = "desktop"
    app = "Docker"
    title = "Docker Desktop"

    def setUp(self):
        super().setUp()
        env = {"CAPSULE_DOCKER_ENGINE": self.engine}
        self.enterContext(mock.patch.dict(os.environ, env))

    def log_text(self):
        return (self.tmp / "state" / "docker_idle.jsonl").read_text()

    def test_quit_uses_the_engine_app(self):
        # contract: desktop `osascript -e 'quit app "Docker"'`; orbstack `quit app "OrbStack"`
        self.tick(0)
        self.tick(15)
        self.assertEqual(self.acted(), ["docker compose -p capsule-corp stop",
                                        f'osascript -e quit app "{self.app}"'])

    def test_log_lines_carry_the_app(self):
        # contract: log lines read `quit app "<app>"`
        self.tick(0)
        self.tick(15)
        self.assertIn(f'quit app "{self.app}"', self.log_text().replace('\\"', '"'))

    def test_stamp_written_on_success(self):
        # contract: stamp docker-idle-quit is still written on success
        self.tick(0)
        self.tick(15)
        self.assertTrue((self.tmp / "state" / "docker-idle-quit").exists())

    def test_dry_run_text_ends_with_the_title(self):
        # contract: --dry-run text desktop ends `then quit Docker Desktop`; orbstack `then quit
        # OrbStack`
        self.tick(0)
        self.tick(10)
        rc, out = self.tick(15, "--dry-run")
        self.assertEqual(rc, 0)
        self.assertTrue(out.strip().endswith(f"then quit {self.title}"), out)
        self.assertEqual(self.acted(), [])

    def test_busy_skip_log_names_the_app(self):
        # contract: busy-skip log reads `quit app "<app>"` with `skipped, busy: ...`
        (self.fd / "spawn").touch()
        self.tick(0)
        rc, out = self.tick(15)
        log = self.log_text().replace('\\"', '"')
        self.assertIn(f'quit app "{self.app}"', log)
        self.assertIn("skipped, busy", log + out)
        self.assertEqual(self.acted(), ["docker compose -p capsule-corp stop"])
        self.assertFalse((self.tmp / "state" / "docker-idle-quit").exists())

    def test_unchanged_behaviour_exit_code(self):
        # contract: "Nothing else changes (exit codes, stamp, compose stop)"
        self.tick(0)
        rc, _ = self.tick(15)
        self.assertEqual(rc, 0)


class OrbstackIdle(EngineIdle):
    engine, app, title = "orbstack", "OrbStack", "OrbStack"

    def test_the_desktop_app_is_never_quit(self):
        # intent idle-quits-the-engine-app: orbstack quits OrbStack, not Docker
        self.tick(0)
        self.tick(15)
        self.assertFalse([c for c in self.calls() if 'app "Docker"' in c])


class EngineFromStateFile(World):
    def test_state_file_selects_the_quit_app(self):
        # contract: resolution order applies (file docker-engine = orbstack, env unset)
        os.environ.pop("CAPSULE_DOCKER_ENGINE", None)
        (self.tmp / "state").mkdir(exist_ok=True)
        (self.tmp / "state" / "docker-engine").write_text("orbstack\n")
        self.tick(0)
        self.tick(15)
        self.assertEqual(self.acted()[-1], 'osascript -e quit app "OrbStack"')


if __name__ == "__main__":
    unittest.main()
