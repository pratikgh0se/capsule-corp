"""Docker engine switch, shell messages (docs/contracts/docker-engine-switch.md, section Messages):
heal.sh's page and hoi-poi's `up` failure carry the engine's title and open hint. Both scripts run
against fake binaries in a temp tree (the same fakes as tests/test_heal.sh and test_hoi_poi.sh, cut
down); AGENT_OPS_DOCKER_BIN points at the fake dir, state and HOME are temp, nothing is sent."""
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

PAGES = {  # contract Messages table: heal.sh page line 2
    "desktop": "2. Open Docker Desktop, or: open -a Docker",
    "orbstack": "2. Open OrbStack, or: open -a OrbStack",
}
HOI = {  # contract Messages table: hoi-poi up, docker not answering
    "desktop": "hoi-poi up: FAIL docker - start Docker Desktop and run hoi-poi up again",
    "orbstack": "hoi-poi up: FAIL docker - start OrbStack and run hoi-poi up again",
}


class World(unittest.TestCase):
    def setUp(self):
        self.t = Path(tempfile.mkdtemp(prefix="docker-engine-msg-")).resolve()
        self.addCleanup(shutil.rmtree, self.t, True)
        self.bin, self.state, self.home = self.t / "bin", self.t / "state", self.t / "home"
        self.log = self.t / "log"
        for d in (self.bin, self.state, self.home, self.log):
            d.mkdir()
        self.sent, self.argv = self.t / "sent.log", self.t / "argv.log"


class HealPage(World):
    def setUp(self):
        super().setUp()
        for b in ("docker", "curl", "launchctl", "claude"):
            (self.bin / b).write_text(
                f'#!/usr/bin/env bash\necho "{b} $*" >>"{self.argv}"\n'
                + ('exit 1\n' if b == "docker" else 'echo \'{"loggedIn": true}\'\nexit 0\n'))
        (self.bin / "capsule-secret").write_text(
            '#!/usr/bin/env bash\n[ "${1:-}" = backend ] && { echo infisical; exit 0; }\nexit 0\n')
        (self.bin / "fakepy").write_text(
            '#!/usr/bin/env bash\ncase "${4:-}" in\n  outbox-send)\n'
            f'    printf \'%s\\n\' "$6" >>"{self.sent}"; exit 0 ;;\n'
            '  pause|unpark) exit 0 ;;\nesac\nexec python3 "$@"\n')
        for f in self.bin.iterdir():
            f.chmod(0o755)
        (self.t / "dc.yml").write_text("services:\n  grafana:\n    image: x\n")
        (self.t / "obs.env").write_text("")
        self.sent.write_text("")
        self.argv.write_text("")

    def heal(self, engine):
        env = {"PATH": f"{self.bin}:/usr/bin:/bin", "HOME": str(self.home),
               "AGENT_OPS_NO_SEND": "1", "AGENT_OPS_STATE_DIR": str(self.state),
               "STATE_DIR": str(self.state), "EVENTS_SPOOL": str(self.t / "events.jsonl"),
               "AGENT_OPS_DOCKER_BIN": str(self.bin), "CAPSULE_DOCKER_ENGINE": engine,
               "OBS_ENV": str(self.t / "obs.env"), "COMPOSE_FILE": str(self.t / "dc.yml"),
               "COMPOSE_PROJECT_NAME": "capsule-corp-test", "COMPOSE_WAIT_STEP_S": "0",
               "COMPOSE_WAIT_S": "1", "DOCKER_DOWN_THROTTLE_S": "43200",
               "CAPSULE_SECRET_BIN": str(self.bin / "capsule-secret"),
               "EV": f"python3 {REPO}/maintenance/events.py",
               "CAPSULE_PY": str(self.bin / "fakepy"), "SENT": str(self.sent),
               "ARGV": str(self.argv)}
        return subprocess.run(["bash", str(REPO / "maintenance" / "heal.sh")], env=env,
                              capture_output=True, text=True, timeout=120)

    def test_page_names_the_engine_on_the_second_consecutive_failure(self):
        # contract Messages: heal.sh page (second consecutive failure), line 2, both engines
        for engine, line in PAGES.items():
            with self.subTest(engine):
                shutil.rmtree(self.state)
                self.state.mkdir()
                self.sent.write_text("")
                first = self.heal(engine)
                self.assertEqual(first.returncode, 1, first.stderr)
                self.assertEqual(self.sent.read_text(), "", "first failure is silent")
                self.heal(engine)
                page = self.sent.read_text()
                self.assertIn(line, page.splitlines(), page)
                other = PAGES["orbstack" if engine == "desktop" else "desktop"]
                self.assertNotIn(other, page.splitlines())

    def test_unknown_engine_fails_loudly(self):
        # contract: unknown value exits 2 with a stderr message; no fallback to desktop
        res = self.heal("podman")
        self.assertEqual(res.returncode, 2, res.stdout + res.stderr)
        self.assertIn("podman", res.stderr)
        self.assertEqual(self.sent.read_text(), "")


class HoiPoiUp(World):
    def setUp(self):
        super().setUp()
        for b in ("launchctl", "docker", "curl", "capsule-secret", "bootstrap.sh", "run.sh"):
            (self.bin / b).write_text(
                '#!/usr/bin/env bash\nme="$(basename "$0")"\n'
                '[ "$me" = capsule-secret ] && [ "${1:-}" = backend ] '
                '&& { echo infisical; exit 0; }\n'
                f'printf \'%s\\n\' "$*" >>"{self.log}/$me.log"\n'
                'case "$me" in\n'
                f'  launchctl) [ "$1" = list ] && cat "{self.log}/launchctl.list" '
                '2>/dev/null; exit 0 ;;\n'
                f'  docker) [ "$1" = info ] && exit 1; exit 0 ;;\n'
                '  *) exit 0 ;;\nesac\n')
            (self.bin / b).chmod(0o755)
        self.agents = self.t / "launchagents"
        self.agents.mkdir()
        (self.t / "obs.env").write_text("")
        comps = "docker infisical stack secrets claude-login outbox".split()
        (self.state / "heal.json").write_text(
            json.dumps({c: {"fails": 0, "first_fail": None} for c in comps}))

    def up(self, engine):
        env = {"PATH": f"{self.bin}:/usr/bin:/bin", "HOME": str(self.home),
               "CAPSULE_LAUNCHD_PREFIX": "dev.capsule",
               "CAPSULE_LAUNCHD_UNLOADED": str(self.t / "none"),
               "FAKE_LOG": str(self.log), "LAUNCHCTL_BIN": str(self.bin / "launchctl"),
               "DOCKER_BIN": str(self.bin / "docker"), "CURL_BIN": str(self.bin / "curl"),
               "CAPSULE_SECRET_BIN": str(self.bin / "capsule-secret"),
               "BOOTSTRAP_BIN": str(self.bin / "bootstrap.sh"),
               "RUN_SH_BIN": str(self.bin / "run.sh"),
               "AGENT_OPS_STATE_DIR": str(self.state), "AGENT_OPS_NO_SEND": "1",
               "AGENT_OPS_FLAVOR": "personal", "OPS_ALERT_TARGET": "12345",
               "COMPOSE_FILE": str(REPO / "observability" / "docker-compose.yml"),
               "OBS_ENV": str(self.t / "obs.env"), "HOI_POI_OPS_ENV": "/dev/null",
               "COMPOSE_PROJECT_NAME": "capsule-corp-test", "LAUNCH_AGENTS_DIR": str(self.agents),
               "HOI_POI_WAIT_STEP_S": "0", "AGENT_OPS_DOCKER_BIN": str(self.bin),
               "CAPSULE_DOCKER_ENGINE": engine}
        return subprocess.run(["bash", str(REPO / "bin" / "hoi-poi.sh"), "up"], env=env,
                              capture_output=True, text=True, timeout=120)

    def test_docker_not_answering_names_the_engine(self):
        # contract Messages: hoi-poi up, docker not answering -> FAIL line with title, exit 4
        for engine, line in HOI.items():
            with self.subTest(engine):
                res = self.up(engine)
                out = res.stdout + res.stderr
                self.assertEqual(res.returncode, 4, out)
                self.assertIn(line, out)
                other = HOI["orbstack" if engine == "desktop" else "desktop"]
                self.assertNotIn(other, out)

    def test_unknown_engine_fails_loudly(self):
        # contract: unknown engine exits 2 with a message naming the value
        res = self.up("podman")
        self.assertEqual(res.returncode, 2, res.stdout + res.stderr)
        self.assertIn("podman", res.stderr)


if __name__ == "__main__":
    unittest.main()
