"""Docker engine switch: the shell helper lib_docker_engine.sh and cleanup.sh's use of it.
Temp HOME, state dir and fake docker only."""
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from capsule.tests.docker_engine_support import (  # noqa: E402
    DESKTOP_BIN, LIB, LIB_STATE, ROOT, Sandbox, run_lib)


class ShellHelper(Sandbox):
    def test_defaults(self):
        # contract Shell helper: Defaults (orbstack, DOCKER_CONTEXT exported, PATH begins with
        # the OrbStack bin)
        out = run_lib(self)
        self.assertEqual(out["DOCKER_ENGINE"], "orbstack")
        self.assertEqual(out["DOCKER_CONTEXT"], "orbstack")
        self.assertTrue(out["PATH"].startswith(f"{self.home}/.orbstack/bin:"), out["PATH"])

    def test_sourcing_alone_sets_vars_and_exports_context(self):
        # contract: "sourcing it sets the DOCKER_ENGINE_* vars and exports DOCKER_CONTEXT"
        out = run_lib(self, engine="orbstack", call_export=False)
        self.assertEqual(out["DOCKER_ENGINE"], "orbstack")
        self.assertEqual(out["DOCKER_ENGINE_APP"], "OrbStack")
        self.assertIn("DOCKER_CONTEXT", out["_exported"])
        self.assertEqual(out["DOCKER_CONTEXT"], "orbstack")

    def test_env_orbstack(self):
        # contract: CAPSULE_DOCKER_ENGINE=orbstack -> context, PATH, APP, OPEN_HINT
        out = run_lib(self, engine="orbstack")
        self.assertEqual(out["DOCKER_CONTEXT"], "orbstack")
        self.assertTrue(out["PATH"].startswith(f"{self.home}/.orbstack/bin:"), out["PATH"])
        self.assertEqual(out["DOCKER_ENGINE_APP"], "OrbStack")
        self.assertEqual(out["DOCKER_ENGINE_OPEN_HINT"], "open -a OrbStack")

    def test_file_orbstack_env_unset(self):
        # contract: file docker-engine = orbstack, env unset: same as above; intent
        # file-flips-every-job
        out = run_lib(self, file_text="orbstack\n")
        self.assertEqual(out["DOCKER_ENGINE"], "orbstack")
        self.assertEqual(out["DOCKER_CONTEXT"], "orbstack")
        self.assertTrue(out["PATH"].startswith(f"{self.home}/.orbstack/bin:"), out["PATH"])
        self.assertEqual(out["DOCKER_ENGINE_APP"], "OrbStack")
        self.assertEqual(out["DOCKER_ENGINE_OPEN_HINT"], "open -a OrbStack")

    def test_env_beats_file_and_empty_env_is_unset(self):
        # contract resolution rows 3 and 5, shell side
        got = run_lib(self, engine="desktop", file_text="orbstack")
        self.assertEqual(got["DOCKER_ENGINE"], "desktop")
        got = run_lib(self, engine="", file_text="orbstack")
        self.assertEqual(got["DOCKER_ENGINE"], "orbstack")
        self.assertEqual(run_lib(self, file_text="")["DOCKER_ENGINE"], "orbstack")
        self.assertEqual(run_lib(self, file_text="  orbstack  \n")["DOCKER_ENGINE"], "orbstack")

    def test_override_keeps_precedence_and_context_pin(self):
        # contract: AGENT_OPS_DOCKER_BIN=/x/fake: PATH begins /x/fake: for either engine; context
        # still pinned
        for engine, ctx in (("desktop", "desktop-linux"), ("orbstack", "orbstack")):
            with self.subTest(engine):
                out = run_lib(self, engine=engine, override="/x/fake")
                self.assertTrue(out["PATH"].startswith("/x/fake:"), out["PATH"])
                self.assertEqual(out["DOCKER_CONTEXT"], ctx)

    def test_extra_path(self):
        # contract: DOCKER_EXTRA_PATH=:/usr/local/go/bin -> PATH begins <bin>:/usr/local/go/bin:
        out = run_lib(self, engine="desktop", extra_path=":/usr/local/go/bin")
        self.assertTrue(out["PATH"].startswith(DESKTOP_BIN + ":/usr/local/go/bin:"), out["PATH"])
        out = run_lib(self, engine="orbstack", extra_path=":/usr/local/go/bin")
        want = f"{self.home}/.orbstack/bin:/usr/local/go/bin:"
        self.assertTrue(out["PATH"].startswith(want), out["PATH"])

    def test_callers_context_is_overridden(self):
        # contract context pin, shell side (caller's DOCKER_CONTEXT must not survive)
        env = {"PATH": "/usr/bin:/bin", "HOME": str(self.home),
               "AGENT_OPS_STATE_DIR": str(self.state), "AGENT_OPS_NO_SEND": "1",
               "DOCKER_CONTEXT": "colima", "CAPSULE_DOCKER_ENGINE": "orbstack"}
        script = f". '{LIB_STATE}'; . '{LIB}'; docker_path_export; printf %s \"$DOCKER_CONTEXT\""
        proc = subprocess.run(["bash", "-c", script], env=env, capture_output=True,
                              text=True, timeout=60)
        self.assertEqual(proc.stdout, "orbstack", proc.stderr)

    def test_unknown_env_value_exits_2_with_message(self):
        # contract: Unknown value: stderr message naming the value, exit 2 before any docker call
        out = run_lib(self, engine="podman", expect_rc=2)
        self.assertIn("podman", out["_proc"].stderr)
        self.assertNotIn("::SENTINEL", out["_proc"].stdout)

    def test_unknown_file_value_exits_2_naming_file(self):
        # contract resolution row 8, shell side
        out = run_lib(self, file_text="podman\n", expect_rc=2)
        self.assertIn("podman", out["_proc"].stderr)
        self.assertIn(str(self.state / "docker-engine"), out["_proc"].stderr)


class CleanupScript(Sandbox):
    """cleanup.sh calls docker through the engine's PATH (contract: run.sh jobs, cleanup.sh,
    deploy.sh
    see the engine's context and, without the override, the engine's bin dir first)."""

    def run_cleanup(self, engine, override):
        orb = self.home / ".orbstack" / "bin"
        orb.mkdir(parents=True, exist_ok=True)
        log = self.tmp / "seen.log"
        body = f'#!/bin/sh\necho "ctx=$DOCKER_CONTEXT path=$PATH" >> "{log}"\nexit 0\n'
        for d in (orb, self.fake):
            (d / "docker").write_text(body)
            (d / "docker").chmod(0o755)
        env = self.env(engine, override=bool(override), CLEANUP_HOME=str(self.tmp),
                       CLEANUP_MAINT_LOGS=str(self.tmp / "logs"), CLEANUP_NO_PG="1",
                       CLEANUP_NO_EVENT="1",
                       CLEANUP_EVENTS_SPOOL=str(self.tmp / "no" / "events.jsonl"),
                       CLEANUP_STATE_DIR=str(self.tmp / "cstate"),
                       CLEANUP_TMPDIR=str(self.tmp / "no-tmp"))
        (self.tmp / "logs").mkdir(exist_ok=True)
        proc = subprocess.run(["bash", str(ROOT / "maintenance" / "cleanup.sh")], env=env,
                              capture_output=True, text=True, timeout=120)
        seen = log.read_text().splitlines() if log.exists() else []
        return proc, seen

    def test_orbstack_context_and_bin_first(self):
        # contract: fake docker prints $DOCKER_CONTEXT and $PATH: engine's context, engine bin dir
        # first
        proc, seen = self.run_cleanup("orbstack", override=False)
        self.assertTrue(seen, proc.stdout + proc.stderr)
        self.assertTrue(all(s.startswith("ctx=orbstack path=" + str(self.home) + "/.orbstack/bin:")
                            for s in seen), seen)

    def test_override_dir_first_and_context_pinned(self):
        # contract: override keeps PATH precedence, context still pinned
        proc, seen = self.run_cleanup("orbstack", override=True)
        self.assertTrue(seen, proc.stdout + proc.stderr)
        self.assertTrue(all(s.startswith(f"ctx=orbstack path={self.fake}:") for s in seen), seen)

    def test_unknown_engine_exits_2_before_docker(self):
        # contract: Unknown value exits 2 before any docker call
        proc, seen = self.run_cleanup("podman", override=True)
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertIn("podman", proc.stderr)
        self.assertEqual(seen, [])



if __name__ == "__main__":
    unittest.main()
