"""The tester's sparse blind clone (`capsule.blind.launch._blind_clone`), split out of
test_blind_dispatch.py for its size: the branch is made with the git dir named, not discovered."""
import contextlib
import io
import subprocess
from unittest import mock

from capsule import workspace
from capsule.blind import launch
from capsule.tests import test_blind_dispatch as tbd


class BlindClone(tbd.BlindBoard):
    def test_the_branch_is_made_with_the_git_dir_named_not_discovered(self):
        dest = self.home / "work" / "run1"
        calls = []
        real = launch.preflight.git

        def spy(repo, *args):
            calls.append(args)
            return real(repo, *args)
        with mock.patch.object(launch.preflight, "git", spy):
            launch._blind_clone(self.home / "blindrepo", dest, "t_1")
        gd = workspace.gitdir(dest)
        named = [a for a in calls if "add" in a or "commit" in a]     # init -b makes the branch
        self.assertEqual(len(named), 2)
        for call in named:
            self.assertEqual(call[:4], ("--git-dir", str(gd), "--work-tree", str(dest)))
        head = subprocess.run(["git", "--git-dir", str(gd), "symbolic-ref", "--short", "HEAD"],
                              capture_output=True, text=True).stdout.strip()
        self.assertEqual(head, "card/t_1")
        self.assertEqual((dest / ".git").read_text(), "gitdir: ../.gitdirs/run1\n")


class RefusedTesterStart(tbd.BlindBoard):
    """The runner's blind-mount refusal at the tester's container start (`launch._launch`)."""
    LEAK = "/srv/BLINDLEAK-7f3a/spool"

    class Refusing:
        def start(self, *args, **kw):
            raise RuntimeError(f"implementer-has-no-blind: {RefusedTesterStart.LEAK}"
                               " [run capsule-abc]")

    def board_text(self):
        rows = self.conn.execute(
            "SELECT payload FROM task_events UNION ALL SELECT error FROM task_runs"
            " UNION ALL SELECT summary FROM task_runs UNION ALL SELECT body FROM outbox")
        return [r[0] for r in rows if r[0]]

    def test_a_refusal_reaches_the_run_and_its_event_masked(self):
        card = self.card_with("running")
        self.t.strong = self.Refusing()
        with contextlib.redirect_stderr(io.StringIO()), contextlib.suppress(RuntimeError):
            self.step()
        (run,) = self.blind_runs()
        self.assertEqual(run["error"], "RuntimeError: implementer-has-no-blind: "
                         "path-6b70333149f8 [run capsule-abc]")
        failed = [r[0] for r in self.conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='blind_tester_failed'",
            (card,))]
        self.assertEqual(len(failed), 1)
        self.assertIn("path-6b70333149f8", failed[0])
        for text in self.board_text():
            self.assertNotIn("BLINDLEAK", text)
            self.assertNotIn("/srv/", text)

    def test_another_start_failure_keeps_its_text(self):
        class Down:
            def start(self, *args, **kw):
                raise RuntimeError("docker is down")
        self.card_with("running")
        self.t.strong = Down()
        with contextlib.redirect_stderr(io.StringIO()), contextlib.suppress(RuntimeError):
            self.step()
        (run,) = self.blind_runs()
        self.assertEqual(run["error"], "RuntimeError: docker is down")


class FailedDockerStart(tbd.BlindBoard):
    """A failed `docker run` shows each mount as `<masked>:target` on the board, never its host
    source (the argv is rewritten, not the text searched); the raw command stays with the caller."""
    SRC = "/srv/BLINDLEAK-7f3a/clone"
    CMD = ["docker", "run", "-d", "--name", "capsule-abc", "-v", f"{SRC}:/workspace:ro",
           f"-v={SRC}/x:/x", "--mount", f"type=bind,source={SRC}/y,target=/y,readonly",
           "capsule-worker:1", "sleep", "infinity"]

    class Failing:
        stderr = "docker: no"

        def start(self, *args, **kw):
            raise subprocess.CalledProcessError(125, FailedDockerStart.CMD, "", self.stderr)

    def test_masked_rewrites_every_mount_form_and_keeps_the_rest(self):
        from capsule import incident_blind
        exc = subprocess.CalledProcessError(125, self.CMD)
        text = incident_blind.masked(exc)
        self.assertNotIn("BLINDLEAK", text)
        for kept in ("<masked>:/workspace:ro", "<masked>:/x", "<masked>:/y", "capsule-abc",
                     "capsule-worker:1", "exit status 125"):
            self.assertIn(kept, text)
        self.assertEqual(self.CMD[6], f"{self.SRC}:/workspace:ro")       # the caller's argv
        slow = incident_blind.masked(subprocess.TimeoutExpired(self.CMD, 300))
        self.assertTrue("<masked>:/y" in slow and "BLINDLEAK" not in slow and "300" in slow)
        self.assertIsNone(incident_blind.masked(subprocess.CalledProcessError(1, ["git", "x"])))
        self.assertIsNone(incident_blind.masked(ValueError("-v /a:/b")))
        spaced = subprocess.CalledProcessError(1, ["docker", "run", "-v", "/a:/b", "img"])
        self.assertIn("<masked>:/b", incident_blind.masked(spaced))

    def test_masking_is_total_over_flag_shapes_sources_and_the_stderr_tail(self):
        from capsule import incident_blind

        def text(*argv, **kw):
            return incident_blind.masked(subprocess.CalledProcessError(125, list(argv), **kw))
        colon = text("docker", "run", "-v", "/a:b/c:/q:ro", "img",
                     stderr="bind source path does not exist: /a:b/c")
        self.assertIn("<masked>:/q:ro", colon)
        self.assertIn("does not exist: <masked>", colon)
        self.assertIn("exit status 125", colon)
        self.assertNotIn("b/c", colon)
        for argv in (("docker", "run", "-v/h/p:/q", "img"), ("docker", "run", "-dv", "/h/p:/q"),
                     ("docker", "-H", "tcp://x", "container", "run", "-v", "/h/p:/q", "img"),
                     ("docker", "--debug", "create", "--volume=/h/p:/q")):
            got = text(*argv)
            self.assertTrue("<masked>:/q" in got and "/h/p" not in got, argv)
        got = text("docker", "run", "--mount", "type=volume,source=vol9,target=/t,"
                   "volume-opt=type=nfs,volume-opt=device=/dev/SEC", "--mount=type=bind,src=/s/EC,"
                   "dst=/d", "img", stderr=b"no device /dev/SEC for vol9")
        for gone in ("SEC", "/s/EC"):
            self.assertNotIn(gone, got)
        self.assertTrue("<masked>:/t" in got and "<masked>:/d" in got and "no device" in got)

    def test_a_named_volume_stays_and_does_not_eat_other_words(self):
        from capsule import incident_blind
        exc = subprocess.CalledProcessError(
            125, ["docker", "run", "-v", "data:/d", "--mount", "type=volume,source=vol,target=/t",
                  "-v", "/h/x:/x", "img"], stderr="metadata of vol and /h/x")
        got = incident_blind.masked(exc)
        self.assertIn("metadata of vol and <masked>", got)
        self.assertIn("'data:/d'", got)
        self.assertIn("type=volume,source=vol,target=/t", got)
        self.assertIn("<masked>:/x", got)

    def test_every_mount_value_that_is_a_host_path_is_masked_in_both_error_types(self):
        from capsule import incident_blind
        argv = ["docker", "run", "--mount",
                "type=volume,source=vol,target=/t,volume-subpath=/srv/SUBLEAK/blind", "img"]
        for exc in (subprocess.CalledProcessError(125, argv, stderr="bad /srv/SUBLEAK/blind"),
                    subprocess.TimeoutExpired(argv, 30, stderr=b"bad /srv/SUBLEAK/blind")):
            got = incident_blind.masked(exc)
            self.assertTrue("SUBLEAK" not in got and "<masked>:/t" in got, got)
            self.assertIn("bad <masked>", got)

    def test_launch_why_keeps_its_one_argument_shape(self):
        argv = ["docker", "run", "-v", "/srv/WHYLEAK:/x", "img"]
        got = launch._why(subprocess.CalledProcessError(125, argv))
        self.assertTrue("WHYLEAK" not in got and "<masked>:/x" in got, got)

    def test_a_string_command_and_other_docker_subcommands_are_masked_too(self):
        from capsule import incident_blind
        as_text = subprocess.CalledProcessError(
            125, "docker run -v '/h/p q:/q' --mount type=bind,source=/s/EC,target=/d img")
        got = incident_blind.masked(as_text)
        self.assertTrue("<masked>:/q" in got and "<masked>:/d" in got, got)
        self.assertTrue("/h/p" not in got and "EC" not in got and "docker run" in got, got)
        self.assertNotIn("EC", incident_blind.masked(subprocess.CalledProcessError(
            125, "docker run -v '/unbalanced:/q --mount type=bind,source=/s/EC,target=/d")))
        other = subprocess.CalledProcessError(
            1, ["docker", "exec", "-v", "/h/p:/q", "c", "ls", "/srv/BLINDROOT/x"],
            stderr="no /srv/BLINDROOT/x")
        got = incident_blind.why(other, blind=("/srv/BLINDROOT",))
        self.assertTrue("BLINDROOT" not in got and "/h/p" not in got and "path-" in got, got)
        git = subprocess.CalledProcessError(1, ["git", "-v"])
        self.assertEqual(incident_blind.why(git), f"CalledProcessError: {git}")

    def test_why_logs_the_raw_command_once_only_for_a_card(self):
        from capsule import incident_blind
        exc = subprocess.CalledProcessError(125, self.CMD)
        with mock.patch("sys.stderr", new_callable=io.StringIO) as log:
            incident_blind.why(exc)
            self.assertEqual(log.getvalue(), "")
            incident_blind.why(exc, "t_1")
        self.assertEqual(log.getvalue().count(self.SRC), 3)       # one line: three mounts
        self.assertEqual(log.getvalue().count("\n"), 1)

    def test_a_failed_docker_start_reaches_the_run_and_the_board_masked(self):
        card = self.card_with("running")
        self.t.strong = self.Failing()
        blind = self.home / "blindrepo"          # the board's blind repo, not a mount source
        self.t.strong.stderr = f"cannot read {blind}/t_1/test_x.py (real {blind.resolve()})"
        with contextlib.redirect_stderr(io.StringIO()), contextlib.suppress(Exception):
            self.step()
        (run,) = self.blind_runs()
        self.assertIn("<masked>:/workspace:ro", run["error"])
        self.assertNotIn("blindrepo", run["error"])
        self.assertIn("path-", run["error"])
        self.assertIn("CalledProcessError", run["error"])
        rows = self.conn.execute(
            "SELECT payload FROM task_events UNION ALL SELECT error FROM task_runs"
            " UNION ALL SELECT body FROM outbox")
        text = "\n".join(r[0] for r in rows if r[0])
        self.assertNotIn("BLINDLEAK", text)
        self.assertNotIn("/srv/", text)
        self.assertNotIn("blindrepo", text)
        self.assertTrue(card)


class TesterStartAudit(tbd.BlindBoard):
    """`_start_container` against a scoped runner whose `blind_work` is set: the mount audit
    accepts the tester's own clone and git dir, and still refuses a sibling clone."""

    def start_with(self, mounts_of):
        import json
        from pathlib import Path
        from types import SimpleNamespace

        from capsule.runners import docker, scoped
        root = Path("/h/boards/b/blind-work")
        clone = root / "r1"

        class Runner(docker.StrongRunner):
            def start(self, run_id, ws, git_dir=None, *, allow_blind=False, **more):
                doc = [{"Mounts": [{"Type": "bind", "Source": m} for m in mounts_of(clone)]}]
                self._d = lambda *a, **kw: SimpleNamespace(returncode=0, stdout=json.dumps(doc))
                self._audit(run_id, allow_blind)
                return run_id

        runner = scoped(lambda t, r: Runner(), lambda: {"board": "b"}, Path("/h"))("strong", "x")
        self.assertEqual(runner.blind_work, root)
        job = SimpleNamespace(strong=runner, run_id="r1", watch=lambda n: None)
        return launch._start_container(job, clone, "/s/interface")

    def test_the_testers_own_clone_and_git_dir_pass_the_audit(self):
        self.assertEqual(self.start_with(
            lambda c: [str(c), str(workspace.gitdir(c)), "/s/interface"]), "r1")

    def test_a_sibling_clone_is_still_refused(self):
        with self.assertRaises(RuntimeError):
            self.start_with(lambda c: [str(c), str(c.parent / "r2")])
