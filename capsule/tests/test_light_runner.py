"""ENG-12: the light runner launches the implementer's shell under `sandbox-exec` (rows 373, 378,
395; spec 6.6). Oracles (Z2): the launch argv and the rendered profile on disk (static), and the
real `sandbox-exec` denying a write outside the worktree (live; the intent row
write-outside-worktree-denied, contracts/intent/light_runner.jsonl). The switch is
`redesign.light_runner`; off, `LightRunner` is unavailable exactly as before the wire-up."""
import contextlib
import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from capsule import config, db
from capsule.runners import exec_mcp, host, reap, runner_for, scoped, seatbelt
from capsule.tests import test_tick

LIVE = sys.platform == "darwin" and os.path.exists(host.SANDBOX_EXEC)


def light(cfg):
    """The light runner as the tick builds it: through `scoped`, with the board's config."""
    return scoped(runner_for, lambda: cfg)("light", "implementer")


class Fixture(unittest.TestCase):
    def setUp(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        self.root = os.path.realpath(td.name)
        self.wt = os.path.join(self.root, "wt")
        self.gitdir = os.path.join(self.root, "clone.git")
        self.spool = os.path.join(self.root, "spool")
        for d in (self.wt, self.gitdir, os.path.join(self.root, "out")):
            os.makedirs(d)
        self.runner = host.LightRunner()
        self.runner.enabled = True

    def launch(self):
        """start + limits for one run; returns (name, limits)."""
        name = self.runner.start("r" * 16, self.wt, requests_dir=f"{self.spool}/requests",
                                 git_dir=self.gitdir, tmpdir="/tmp")
        return name, self.runner.limits(name, self.spool)

    def exec(self, cmd, timeout=20):
        if not hasattr(self, "limits"):
            self.limits = self.launch()[1]
        return exec_mcp._run_exec(self.mcp_cfg(self.limits), cmd, timeout)

    def mcp_cfg(self, limits):
        args = json.loads(Path(limits["mcp_config"]).read_text())["mcpServers"]["capsule"]["args"]
        return exec_mcp._parse_args(args[3:])   # drop `-P -m capsule.runners.exec_mcp`


class SwitchOff(unittest.TestCase):
    def test_off_by_default_and_unavailable(self):
        self.assertIs(config.redesign({})["light_runner"], False)
        self.assertFalse(host.LightRunner().available())
        for cfg in ({}, {"redesign": {}}, {"redesign": {"light_runner": False}}):
            self.assertFalse(light(cfg).available(), cfg)

    def test_every_runner_still_carries_the_board(self):
        runner = scoped(runner_for, lambda: {"board": "b1"})("strong", "implementer")
        self.assertEqual(runner.board, "b1")
        self.assertIsNone(scoped(runner_for, lambda: None)("strong", "implementer").board)

    def test_on_needs_sandbox_exec(self):
        on = {"redesign": {"light_runner": True}}
        with mock.patch.object(host, "SANDBOX_EXEC", sys.executable):
            self.assertTrue(light(on).available())
        with mock.patch.object(host, "SANDBOX_EXEC", "/nonexistent/sandbox-exec"):
            self.assertFalse(light(on).available())    # a refusal, never an unsandboxed fallback

    def test_switch_is_a_validated_redesign_flag(self):
        with self.assertRaises(ValueError):
            config._validate_redesign({"light_runner": "yes"})
        config._validate_redesign({"light_runner": True})


class SwitchOn(Fixture):
    def test_launch_argv_is_sandbox_exec_with_the_rendered_profile(self):
        name, limits = self.launch()
        cfg = self.mcp_cfg(limits)
        self.assertEqual(cfg.workspace, self.wt)
        cfg.dry_run = True
        argv = json.loads(exec_mcp._run_exec(cfg, "echo hi", None)["text"])
        self.assertEqual(argv[:3], [host.SANDBOX_EXEC, "-f", cfg.sandbox_profile])
        self.assertEqual(argv[3:], ["/bin/sh", "-c", "echo hi"])
        # the profile on disk is exactly seatbelt's render for this worktree, git dir read-only
        self.assertEqual(Path(cfg.sandbox_profile).read_text(),
                         seatbelt.render(self.wt, vault_read=[self.gitdir]))
        self.assertTrue(name.startswith("light-"))
        self.assertNotIn("--container", Path(limits["mcp_config"]).read_text())

    def test_network_stays_denied_and_never_opens_443(self):
        _, limits = self.launch()
        profile = Path(self.mcp_cfg(limits).sandbox_profile).read_text()
        self.assertNotIn("network-outbound (", profile)    # no allowance at all until card 22
        self.assertNotIn("*:443", profile)
        self.assertIn("(deny default)", profile)

    def test_limits_match_the_strong_tier_shape(self):
        _, limits = self.launch()
        self.assertEqual(limits["permission_mode"], "acceptEdits")
        self.assertIn("mcp__capsule__exec", limits["allowed"])
        self.assertEqual(limits["add_dirs"], [f"{self.spool}/requests"])

    def test_guarded_parents_and_scratch_exist_before_the_profile_applies(self):
        self.launch()
        for d in (".worker-exec", "capsule", "capsule/blind", "docker", "docs"):
            self.assertTrue(os.path.isdir(os.path.join(self.wt, d)), d)

    def test_a_file_where_a_guarded_dir_would_go_is_left_alone(self):
        Path(self.wt, "docker").write_text("a file")
        self.launch()
        self.assertTrue(os.path.isfile(os.path.join(self.wt, "docker")))

    def test_unknown_name_is_refused(self):
        with self.assertRaises(KeyError):
            self.runner.limits("light-nope", self.spool)

    def test_a_planted_exec_mcp_in_the_worktree_is_never_loaded(self):
        """The exec server starts with the worktree as cwd: `-P` keeps `-m` from loading a
        worker-planted capsule/runners/exec_mcp.py (which would run unsandboxed)."""
        _, limits = self.launch()
        mcp = json.loads(Path(limits["mcp_config"]).read_text())["mcpServers"]["capsule"]
        marker = os.path.join(self.root, "planted-ran")
        for rel in ("capsule/__init__.py", "capsule/runners/__init__.py",
                    "capsule/runners/exec_mcp.py"):
            os.makedirs(os.path.dirname(os.path.join(self.wt, rel)), exist_ok=True)
            Path(self.wt, rel).write_text(f"open({marker!r}, 'w').write('x')\n")
        hello = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}) + "\n"

        def serve(args):
            return subprocess.run([mcp["command"], *args, "--dry-run"], input=hello, cwd=self.wt,
                                  env=dict(os.environ, **mcp["env"]), capture_output=True,
                                  text=True, timeout=30)
        self.assertEqual(mcp["args"][0], "-P")
        self.assertIn("capsule-exec", serve(mcp["args"]).stdout)   # the real module answered
        self.assertFalse(os.path.exists(marker))
        serve(mcp["args"][1:])                  # without -P the plant loads: the test can fail
        self.assertTrue(os.path.exists(marker))

    def test_exec_modes_are_exclusive_and_paired(self):
        prof = ["--sandbox-profile", "p", "--workspace", "w"]
        for args in (["--container", "c"], prof):
            self.assertTrue(exec_mcp._parse_args(["--spool", "s", *args]))
        for args in ([], ["--container", "c", *prof],
                     ["--container", "c", "--sandbox-profile", "p"],
                     ["--sandbox-profile", "p"], ["--workspace", "w"],
                     ["--container", "c", "--workspace", "w"]):
            with self.subTest(args=args), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    exec_mcp._parse_args(["--spool", "s", *args])

    def test_guarded_dirs_list_parents_before_children(self):
        dirs = seatbelt.guarded_dirs()
        self.assertIn("capsule/blind", dirs)
        for d in dirs:
            parent = d.rpartition("/")[0]
            self.assertTrue(not parent or dirs.index(parent) < dirs.index(d), d)

    def test_no_guarded_dir_is_created_through_a_symlink(self):
        """A worker-planted `capsule -> out` must not make the host mkdir `out/blind`."""
        out = os.path.join(self.root, "out")
        os.symlink(out, os.path.join(self.wt, "capsule"))
        self.launch()
        self.assertEqual(os.listdir(out), [])
        self.assertTrue(os.path.islink(os.path.join(self.wt, "capsule")))
        self.assertTrue(os.path.isdir(os.path.join(self.wt, "docker")))   # the others are made


class Wiring(unittest.TestCase):
    def test_reap_asks_each_up_tier_once(self):
        both, strong, light = mock.Mock(), mock.Mock(), mock.Mock()
        both.reap.return_value = ["c1"]
        self.assertEqual(reap(lambda tier, role: both, {"strong", "light"}, {"r"}), ["c1"])
        both.reap.assert_called_once_with({"r"})        # one runner for both tiers: reaped once
        strong.reap.return_value, light.reap.return_value = ["c"], [7]
        pick = {"strong": strong, "light": light}
        self.assertEqual(reap(lambda tier, role: pick[tier], {"strong", "light"}, set()), ["c", 7])
        self.assertEqual(reap(lambda tier, role: pick[tier], {"light"}, set()), [7])
        self.assertEqual(reap(lambda tier, role: pick[tier], set(), set()), [])

    def test_a_failing_strong_reap_does_not_stop_the_light_reap(self):
        strong, light = mock.Mock(), mock.Mock()
        strong.reap.side_effect = RuntimeError("docker ps failed")
        light.reap.return_value = [7]
        pick = {"strong": strong, "light": light}
        with self.assertRaisesRegex(RuntimeError, "docker ps failed"):
            reap(lambda tier, role: pick[tier], {"strong", "light"}, {"r"})
        light.reap.assert_called_once_with({"r"})


class LiveCase(Fixture):
    """Real sandbox-exec on temp dirs; a sandbox that cannot apply is a failure (Z4). Off macOS
    there is no light tier, so the case skips."""

    def setUp(self):
        if not LIVE:
            self.skipTest("light tier is unavailable off macOS (no sandbox-exec)")
        super().setUp()


class Live(LiveCase):

    def test_probe_write_outside_denied_inside_allowed(self):
        """Intent row write-outside-worktree-denied via seatbelt's own probe helper."""
        _, limits = self.launch()
        profile = Path(self.mcp_cfg(limits).sandbox_profile).read_text()
        self.assertEqual(seatbelt.probe(profile, f"touch {self.wt}/in.txt").returncode, 0)
        self.assertNotEqual(seatbelt.probe(profile, f"touch {self.root}/out/x.txt").returncode, 0)
        self.assertTrue(os.path.exists(f"{self.wt}/in.txt"))
        self.assertFalse(os.path.exists(f"{self.root}/out/x.txt"))

    def test_exec_channel_writes_inside_only(self):
        ok = self.exec("echo hi > in.txt && cat in.txt")
        self.assertEqual((ok["exit_code"], ok["text"].strip()), (0, "hi"))
        bad = self.exec(f"echo x > {self.root}/out/x.txt")
        self.assertNotEqual(bad["exit_code"], 0)
        self.assertFalse(os.path.exists(f"{self.root}/out/x.txt"))
        moved = self.exec("mkdir capsule_old && mv capsule capsule_old/x")   # rename guard
        self.assertNotEqual(moved["exit_code"], 0)

    def test_exec_env_is_scrubbed_and_tools_work(self):
        with mock.patch.dict(os.environ, {"CAPSULE_TEST_SECRET": "hunter2"}):
            out = self.exec("env; /usr/bin/git --version; /usr/bin/python3 -c 'print(6*7)'")
        self.assertEqual(out["exit_code"], 0, out["text"])
        self.assertNotIn("hunter2", out["text"])
        self.assertIn("42", out["text"])
        self.assertIn(f"TMPDIR={self.wt}/.worker-exec", out["text"])

    def test_timeout_kills_the_command(self):
        self.assertEqual(self.exec("sleep 30", 1)["exit_code"], -1)

    def test_a_localhost_connection_fails_and_nothing_arrives(self):
        """No network allowance at all: not even localhost, where a card-22 proxy will listen."""
        with socket.socket() as srv:
            srv.bind(("127.0.0.1", 0))
            srv.listen(1)
            srv.setblocking(False)
            port = srv.getsockname()[1]
            out = self.exec(f"/usr/bin/python3 -c 'import socket; "
                            f"socket.create_connection((\"127.0.0.1\", {port}), 2)'")
            self.assertNotEqual(out["exit_code"], 0)
            self.assertIn("Operation not permitted", out["text"])   # the sandbox, not a refusal
            with self.assertRaises(BlockingIOError):                  # no connection was ever made
                srv.accept()
            socket.create_connection(("127.0.0.1", port), 2).close()   # control: the listener works

    def test_a_planted_secret_cannot_be_read(self):
        """Home inside the worktree, so only the secret deny (not the default deny) can stop it."""
        for d in ("h/.config/capsule", "h/Movies"):
            os.makedirs(os.path.join(self.wt, d))
        for f in ("h/.config/capsule/key", "h/Movies/m.mov", "h/plain.txt"):
            Path(self.wt, f).write_text("s3cret")
        with mock.patch.object(Path, "home", return_value=Path(self.wt, "h")):
            self.limits = self.launch()[1]
        self.assertEqual(self.exec("cat h/plain.txt")["text"], "s3cret")
        for secret in ("h/.config/capsule/key", "h/Movies/m.mov"):
            out = self.exec(f"cat {secret}")
            self.assertNotEqual(out["exit_code"], 0, secret)
            self.assertNotIn("s3cret", out["text"])

    def test_the_git_dir_and_the_spool_are_not_writable(self):
        Path(self.gitdir, "HEAD").write_text("ref: refs/heads/x\n")
        self.assertEqual(self.exec(f"cat {self.gitdir}/HEAD")["text"], "ref: refs/heads/x\n")
        sb = Path(self.mcp_cfg(self.limits).sandbox_profile)
        before = {p: Path(p).read_text() for p in (sb, Path(self.limits["mcp_config"]))}
        for cmd in (f"echo x > {self.gitdir}/new", f"echo x >> {self.gitdir}/HEAD",
                    f"rm {self.gitdir}/HEAD", f"echo x > {sb}",
                    f"echo x > {self.limits['mcp_config']}",
                    f"rm {sb}", f"mv {sb} {sb}.old", f"cat {sb}"):
            self.assertNotEqual(self.exec(cmd)["exit_code"], 0, cmd)
        self.assertEqual({p: Path(p).read_text() for p in before}, before)
        self.assertEqual(Path(self.gitdir, "HEAD").read_text(), "ref: refs/heads/x\n")
        self.assertFalse(os.path.exists(f"{self.gitdir}/new"))


class Dispatch(test_tick.TickCase):
    """The card oracle: a light card goes through the real tick, switch on and off."""

    def dispatch(self, on):
        self.cfg["redesign"]["light_runner"] = on
        self.write_cfg()
        with db.txn(self.conn):
            self.conn.execute("UPDATE tasks SET risk_class='light', tier='light' WHERE id=?",
                              (self.tid,))
        self.tick = self.make_tick(runners=None, spawn=lambda *args: None)   # the real runners
        self.tick.once()

    def test_switch_off_leaves_the_light_card_waiting(self):
        self.dispatch(False)
        self.assertEqual(self.runs("implementer"), [])
        self.assertEqual(self.task()["status"], "ready")

    def test_switch_on_dispatches_the_worker_under_sandbox_exec(self):
        if not LIVE:
            self.skipTest("light tier is unavailable off macOS (no sandbox-exec)")
        self.dispatch(True)
        run = self.runs("implementer")[0]
        sp = self.spool_dir(run["run_id"])
        job = json.loads((sp / "job.json").read_text())
        self.assertEqual(job["limits"]["mcp_config"], str(sp / "mcp.json"))
        cfg = exec_mcp._parse_args(
            json.loads((sp / "mcp.json").read_text())["mcpServers"]["capsule"]["args"][3:])
        cfg.dry_run = True
        argv = json.loads(exec_mcp._run_exec(cfg, "true", None)["text"])
        self.assertEqual(argv[:3], [host.SANDBOX_EXEC, "-f", str(sp / "seatbelt.sb")])
        wt = os.path.realpath(job["workspace"])
        self.assertIn(f'(subpath "{wt}")', (sp / "seatbelt.sb").read_text())
        self.assertEqual(Path(sp, "worktree").read_text(), wt)


if __name__ == "__main__":
    unittest.main()
