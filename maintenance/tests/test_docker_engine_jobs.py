"""Docker engine switch: run.sh jobs (heal, disk-relief) and deploy.sh run docker with the engine's
context and bin dir first (docs/contracts/docker-engine-switch.md, section Shell helper). A fake
`docker` records $DOCKER_CONTEXT and $PATH; HOME, state, spool and docker are all temp."""
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from capsule.tests.docker_engine_support import ROOT, Sandbox  # noqa: E402

SCRIPTS = tuple(p for p in ("maintenance/run.sh", "maintenance/cleanup.sh", "deploy.sh")
                if (ROOT / p).exists())   # the public copy ships no deploy.sh
PYDIR = str(Path(sys.executable).parent)
ENGINES = (("desktop", "desktop-linux"), ("orbstack", "orbstack"))


class Jobs(Sandbox):
    def setUp(self):
        super().setUp()
        self.log = self.tmp / "seen.log"
        self.maint = self.tmp / "maint"
        self.maint.mkdir()
        self.orb = self.home / ".orbstack" / "bin"
        self.orb.mkdir(parents=True)
        body = f'#!/bin/sh\necho "ctx=$DOCKER_CONTEXT path=$PATH" >> "{self.log}"\nexit 0\n'
        for d in (self.orb, self.fake):
            (d / "docker").write_text(body)
            (d / "docker").chmod(0o755)

    def seen(self):
        return self.log.read_text().splitlines() if self.log.exists() else []

    def job_env(self, engine, override, **extra):
        env = self.env(engine, override=override, RUN_SH_MAINT_DIR=str(self.maint),
                       EVENTS_SPOOL=str(self.tmp / "events.jsonl"),
                       CAPSULE_PY=str(self.tmp / "no-capsule"), OPS_ALERT_TARGET="t",
                       PATH=f"{PYDIR}:/usr/bin:/bin")
        env.update(extra)
        return env

    def run_job(self, job, script, name, engine, override):
        (self.maint / name).write_text(script)
        (self.maint / name).chmod(0o755)
        return subprocess.run(["bash", str(ROOT / "maintenance" / "run.sh"), job],
                              env=self.job_env(engine, override), capture_output=True,
                              text=True, timeout=120)

    def assert_engine(self, engine, ctx, override):
        desktop_bin = "/Applications/Docker.app/Contents/Resources/bin"
        first = str(self.fake) if override else str(self.orb if engine == "orbstack"
                                                    else desktop_bin)
        seen = self.seen()
        self.assertTrue(seen)
        for line in seen:
            self.assertTrue(line.startswith(f"ctx={ctx} path={first}:"), line)


class RunShJobs(Jobs):
    def test_heal_job_sees_the_engine(self):
        # contract: run.sh heal job: a fake docker sees the engine's context and, without the
        # override, the engine's bin dir first (orbstack: $HOME/.orbstack/bin)
        for engine, ctx in ENGINES:
            for override in (True, False):
                if engine == "desktop" and not override:
                    continue   # the real Docker.app dir would be searched first on this host
                with self.subTest(engine=engine, override=override):
                    self.log.write_text("")
                    res = self.run_job("heal", "#!/usr/bin/env bash\ndocker info\n",
                                       "heal.sh", engine, override)
                    self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
                    self.assert_engine(engine, ctx, override)

    def test_disk_relief_job_sees_the_engine(self):
        # contract: run.sh disk-relief job (same rule; its DOCKER_EXTRA_PATH goes after the bin dir)
        fake_py = "import subprocess\nsubprocess.run(['docker', 'info'])\n"
        for engine, ctx in ENGINES:
            for override in (True, False):
                if engine == "desktop" and not override:
                    continue
                with self.subTest(engine=engine, override=override):
                    self.log.write_text("")
                    res = self.run_job("disk-relief", fake_py, "disk_relief.py", engine, override)
                    self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
                    self.assert_engine(engine, ctx, override)

    def test_unknown_engine_stops_the_job_before_docker(self):
        # contract: unknown value exits 2 before any docker call
        res = self.run_job("heal", "#!/usr/bin/env bash\ndocker info\n", "heal.sh", "podman", True)
        self.assertEqual(res.returncode, 2, res.stdout + res.stderr)
        self.assertIn("podman", res.stderr)
        self.assertEqual(self.seen(), [])


NO_DEPLOY = "deploy.sh is maintainer-only (not in the public copy)"


@unittest.skipUnless((ROOT / "deploy.sh").exists(), NO_DEPLOY)
class DeployDockerCheck(Jobs):
    def deploy(self, engine, override):
        """deploy.sh --dry-run in a temp AGENT_OPS_HOME, no launchctl, docker gate open."""
        trip = self.tmp / "trip"
        trip.mkdir(exist_ok=True)
        for tool in ("launchctl", "curl"):
            (trip / tool).write_text("#!/bin/sh\nexit 99\n")
            (trip / tool).chmod(0o755)
        stub = self.tmp / "capsule-secret"
        stub.write_text("#!/bin/sh\n[ \"${1:-}\" = backend ] && echo infisical\nexit 0\n")
        stub.chmod(0o755)
        env = self.env(engine, override=override, AGENT_OPS_HOME=str(self.tmp / "ops-home"),
                       OBSIDIAN_VAULT_PATH=str(self.tmp / "vault"), AGENT_OPS_NO_LAUNCHCTL="1",
                       AGENT_OPS_NO_DOCKER="", AGENT_OPS_NO_TAILSCALE="1",
                       COMPOSE_PROJECT_NAME="capsule-corp-test", CAPSULE_SECRET_BIN=str(stub),
                       EVENTS_SPOOL=str(self.tmp / "events.jsonl"),
                       CAPSULE_ALERT_DIR=str(self.tmp / "alerting"),
                       PATH=f"{trip}:{PYDIR}:/usr/bin:/bin",
                       CAPSULE_PY=sys.executable, CAPSULE_LAUNCHD_PREFIX="dev.capsule")
        (self.tmp / "alerting").mkdir(exist_ok=True)
        return subprocess.run(["bash", str(ROOT / "deploy.sh"), "--dry-run"], env=env,
                              capture_output=True, text=True, timeout=300)

    def test_deploy_docker_check_sees_the_engine(self):
        # contract: deploy.sh's docker check sees the engine's context; orbstack without the
        # override finds the fake in $HOME/.orbstack/bin first
        for override in (True, False):
            with self.subTest(override=override):
                self.log.write_text("")
                self.deploy("orbstack", override)
                self.assert_engine("orbstack", "orbstack", override)

    def test_deploy_with_override_pins_context_for_desktop(self):
        # contract: override keeps PATH precedence while DOCKER_CONTEXT is pinned to the engine's
        self.deploy("desktop", True)
        self.assert_engine("desktop", "desktop-linux", True)

    def test_unknown_engine_fails_deploy_before_docker(self):
        # contract: unknown value exits 2 before any docker call
        res = self.deploy("podman", True)
        self.assertEqual(res.returncode, 2, res.stdout + res.stderr)
        self.assertEqual(self.seen(), [])


class NoDockerAppLiteral(unittest.TestCase):
    def test_no_docker_app_path_outside_the_table(self):
        # contract: "No `Docker.app` literal remains in these files outside the table"
        for rel in SCRIPTS:
            with self.subTest(rel):
                self.assertNotIn("/Applications/Docker.app", (ROOT / rel).read_text())


if __name__ == "__main__":
    unittest.main()
