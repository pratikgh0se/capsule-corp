import io, json, os, re, shutil, stat, subprocess, sys, tempfile, unittest
from pathlib import Path
from unittest import mock
from capsule.runners import docker, exec_mcp, host, runner_for
from capsule import workspace
from capsule.models import claude
MOUNTS = '[{"Mounts": [{"Source": "/w"}]}]'   # a clean container's `docker inspect`


def _mcp(spool, *extra_flags, container="x"):
    return [sys.executable, "-m", "capsule.runners.exec_mcp",
            "--container", container, "--spool", spool, *extra_flags]


def _rpc(cmd, timeout_s=None, msg_id=1):
    args = {"cmd": cmd}
    if timeout_s is not None:
        args["timeout_s"] = timeout_s
    return {"jsonrpc": "2.0", "id": msg_id, "method": "tools/call",
            "params": {"name": "exec", "arguments": args}}

class Args(unittest.TestCase):
    def test_legacy_security_args_and_network_none(self):
        a = docker.run_args("ab" * 16, Path("/w"), "img:tag")
        for flag in ("--network=none", "no-new-privileges", "/var/tmp:rw,noexec,nosuid,size=256m", "ALL"):
            self.assertIn(flag, a)
        self.assertIn("capsule-run-id=" + "ab" * 16, a)
        self.assertIn("TMPDIR=/workspace/.worker-exec", a)
        self.assertNotIn("--privileged", a)
        self.assertIn("/tmp:rw,exec,nosuid,size=512m", a)          # only /tmp allows exec (W177b)
        self.assertIn("/run:rw,noexec,nosuid,size=64m", a)
        self.assertEqual([x for x in a if x.startswith("/tmp:") and "noexec" in x], [])
        # Path.home() works for a uid with no passwd entry
        self.assertIn("HOME=/tmp", a)

    def test_requests_dir_is_mounted_at_requests_never_at_its_host_path(self):
        """Final review I2: the implementer's exec shell can reach its spool's requests/ (and only
        that), at /requests: a same-path mount comes up empty on Docker Desktop 4.43.1."""
        a = docker.run_args("ab" * 16, Path("/w"), "img:tag", requests_dir="/h/spool/r1/requests")
        self.assertIn("/h/spool/r1/requests:/requests", a)
        self.assertNotIn("/h/spool/r1/requests:/h/spool/r1/requests", a)
        self.assertFalse([x for x in a if x.startswith("/h/spool/r1:")])

    def test_a_volume_whose_target_is_its_source_is_refused(self):
        for kw in ({"mounts": ("/a/b:/a/b:ro",)}, {"mounts": ("/s/out:/s/out",)}):
            with self.assertRaises(ValueError):
                docker.run_args("ab" * 16, Path("/w"), "img:tag", **kw)
        with self.assertRaises(ValueError):   # the workspace itself, mounted at /workspace
            docker.run_args("ab" * 16, Path("/workspace"), "img:tag")
        docker.run_args("ab" * 16, Path("/w"), "img:tag", mounts=("/s/out:/out",))

    def test_container_git_dir_refuses_anything_but_gitdirs_name(self):
        for ws, gd in (("/h/w/e", "/h/w/other/e"), ("/h/w/e", "/x/.gitdirs/f/e"),
                       ("/h/w/e", "/h/w/e/.git"), ("/w", "/g/x"), ("/h/w/e", "/.gitdirs/e")):
            with self.assertRaises(ValueError):
                docker.container_git_dir(ws, gd)
        self.assertEqual(docker.container_git_dir("/h/w/e", "/h/w/.gitdirs/e"), "/.gitdirs/e")

    def test_the_contract_container_gets_a_read_only_git_dir_and_tmpfs_tmpdir(self):
        """P6 (row 440): a suite's `git ls-files` needs the git dir (read-only, C1), and its temp
        files need POSIX semantics the Docker Desktop bind mount lacks."""
        a = docker.run_args("ab" * 16, Path("/h/work/e"), "img:tag", git_dir="/h/work/.gitdirs/e",
                            tmpdir=docker.CONTRACT_TMPDIR)
        # never at its host path: Docker Desktop 4.43.1 mounts a source at its own path empty
        self.assertIn("/h/work/.gitdirs/e:/.gitdirs/e:ro", a)
        self.assertNotIn("/h/work/.gitdirs/e:/h/work/.gitdirs/e:ro", a)
        self.assertIn("TMPDIR=/tmp", a)
        self.assertNotIn("TMPDIR=/workspace/.worker-exec", a)
        # a tmpfs, exec explicit (Docker defaults to noexec)
        self.assertIn("/tmp:rw,exec,nosuid,size=512m", a)
        plain = docker.run_args("ab" * 16, Path("/w"), "img:tag")
        self.assertFalse([x for x in plain if x.endswith(":ro") and "gitdirs" in x])

    @mock.patch.object(docker, "worker_image", lambda *a: "img:1")
    def test_start_forwards_the_contract_mounts(self):
        seen = []
        r = docker.StrongRunner()
        r._run = lambda argv, **kw: seen.append(argv) or subprocess.CompletedProcess(
            argv, 0, MOUNTS, "")
        r.start("a" * 32, Path("/h/w/e"), git_dir="/h/w/.gitdirs/e", tmpdir="/tmp")
        self.assertEqual([c[1] for c in seen], ["run", "exec", "inspect"])   # inspect: R33 audit
        self.assertIn("/h/w/.gitdirs/e:/.gitdirs/e:ro", seen[0])
        self.assertIn("TMPDIR=/tmp", seen[0])

    def test_the_container_git_dir_is_where_the_relative_pointer_reaches(self):
        ws, gd = Path("/h/work/epic-x"), Path("/h/work/.gitdirs/epic-x")
        self.assertEqual(docker.container_git_dir(ws, gd), "/.gitdirs/epic-x")
        self.assertNotEqual(docker.container_git_dir(ws, gd), str(gd))
        a = docker.run_args("ab" * 16, ws, "img:tag", git_dir=str(gd))
        mounts = [a[i + 1] for i, x in enumerate(a) if x == "-v"]
        source, target, mode = mounts[-1].split(":")
        self.assertEqual((source, target, mode), (str(gd), docker.container_git_dir(ws, gd), "ro"))
        self.assertNotEqual(source, target)


    def test_no_writable_mount_reaches_the_git_dir(self):
        """R2 (row 445): the implementer container gets the git dir too, and only read-only: no -v
        source is the git dir, inside it or above it unless the mount is `:ro` (C1)."""
        root = Path("/h/boards/b/work")
        ws, gd = root / "epic-x", root / ".gitdirs" / "epic-x"
        a = docker.run_args("ab" * 16, ws, "img:tag", requests_dir="/h/boards/b/spool/r/requests",
                            git_dir=str(gd), tmpdir=docker.CONTRACT_TMPDIR)
        mounts = [a[i + 1] for i, x in enumerate(a) if x == "-v"]
        self.assertIn(f"{gd}:/.gitdirs/epic-x:ro", mounts)
        for m in mounts:
            src = Path(m.split(":")[0])
            if gd == src or gd.is_relative_to(src) or src.is_relative_to(gd):
                self.assertTrue(m.endswith(":ro"), m)
        self.assertIn("TMPDIR=/tmp", a)


class Reap(unittest.TestCase):
    # Fix round 1: names now must equal docker.container_name(run_id) to prove ownership
    # (row 357), so the fixture uses real container-shaped names instead of "c1"/"c2".
    def test_reaps_only_own_dead_runs(self):
        calls = []
        def fake(argv, **kw):
            calls.append(argv)
            out = "capsule-r1\tr1\tb\ncapsule-r2\tr2\tb\n" if argv[1] == "ps" else ""
            return subprocess.CompletedProcess(argv, 0, out, "")
        r = docker.StrongRunner(board="b")
        r._run = fake
        self.assertEqual(r.reap({"r1"}), ["capsule-r2"])
        self.assertIn("label=capsule=1", " ".join(calls[0]))
        self.assertEqual(calls[-1][:3], ["docker", "rm", "-f"])

    def test_ignores_foreign_container_with_empty_run_id_label(self):
        """A container carrying capsule=1 but no capsule-run-id label is never touched (row 357)."""
        calls = []
        def fake(argv, **kw):
            calls.append(argv)
            out = "capsule-foreign\t\tb\n" if argv[1] == "ps" else ""
            return subprocess.CompletedProcess(argv, 0, out, "")
        r = docker.StrongRunner(board="b")
        r._run = fake
        self.assertEqual(r.reap(set()), [])
        self.assertFalse(any(c[:3] == ["docker", "rm", "-f"] for c in calls))

    def test_ignores_container_whose_name_does_not_match_its_run_id(self):
        """A capsule=1 container with a run-id label but an unrelated name proves nothing."""
        calls = []
        def fake(argv, **kw):
            calls.append(argv)
            out = "someone-elses-container\tr9\tb\n" if argv[1] == "ps" else ""
            return subprocess.CompletedProcess(argv, 0, out, "")
        r = docker.StrongRunner(board="b")
        r._run = fake
        self.assertEqual(r.reap(set()), [])
        self.assertFalse(any(c[:3] == ["docker", "rm", "-f"] for c in calls))

    def test_raises_on_ps_failure_instead_of_reporting_nothing_removed(self):
        def fake(argv, **kw):
            return subprocess.CompletedProcess(argv, 1, "", "docker daemon unreachable")
        r = docker.StrongRunner(board="b")
        r._run = fake
        with self.assertRaises(RuntimeError):
            r.reap(set())

    def test_only_counts_removed_when_rm_succeeds(self):
        def fake(argv, **kw):
            if argv[1] == "ps":
                return subprocess.CompletedProcess(argv, 0, "capsule-r2\tr2\tb\n", "")
            return subprocess.CompletedProcess(argv, 1, "", "no such container")
        r = docker.StrongRunner(board="b")
        r._run = fake
        self.assertEqual(r.reap(set()), [])

    def test_calls_pass_a_timeout(self):
        seen = []
        def fake(argv, **kw):
            seen.append(kw.get("timeout"))
            out = "capsule-r1\tr1\tb\n" if argv[1] == "ps" else ""
            return subprocess.CompletedProcess(argv, 0, out, "")
        r = docker.StrongRunner(board="b")
        r._run = fake
        r.reap({"r1"})
        self.assertTrue(seen and all(t is not None for t in seen))

    def test_reap_is_scoped_to_the_ticks_own_board(self):
        """Board A never reaps board B's or an unlabelled container."""
        calls = []
        def fake(argv, **kw):
            calls.append(argv)
            rows = "capsule-r1\tr1\tA\ncapsule-r2\tr2\tB\ncapsule-r3\tr3\t\n"
            out = rows if argv[1] == "ps" else ""
            return subprocess.CompletedProcess(argv, 0, out, "")
        r = docker.StrongRunner(board="A")
        r._run = fake
        self.assertEqual(r.reap(set()), ["capsule-r1"])
        self.assertEqual([c[-1] for c in calls if c[:3] == ["docker", "rm", "-f"]], ["capsule-r1"])
        calls.clear()
        unscoped = docker.StrongRunner()
        unscoped._run = fake
        self.assertEqual(unscoped.reap(set()), [])
        self.assertFalse(any(c[:3] == ["docker", "rm", "-f"] for c in calls))

    @mock.patch.object(docker, "worker_image", lambda *a: "img:1")
    def test_start_labels_the_container_with_its_board(self):
        seen = []
        r = docker.StrongRunner(board="A")
        r._run = lambda argv, **kw: seen.append(argv) or subprocess.CompletedProcess(
            argv, 0, MOUNTS)
        r.start("a" * 32, Path("/w"))
        self.assertIn("capsule-board=A", seen[0])
        self.assertEqual([c[1] for c in seen], ["run", "exec", "inspect"])   # inspect: R33 audit


class Stop(unittest.TestCase):
    def test_refuses_a_name_not_shaped_like_our_own(self):
        calls = []
        r = docker.StrongRunner(); r._run = lambda argv, **kw: calls.append(argv)
        with self.assertRaises(ValueError):
            r.stop("someone-elses-container")
        self.assertEqual(calls, [])

    def test_raises_on_rm_failure(self):
        def fake(argv, **kw):
            return subprocess.CompletedProcess(argv, 1, "", "no such container")
        r = docker.StrongRunner(); r._run = fake
        with self.assertRaises(RuntimeError):
            r.stop(docker.container_name("r1"))

    def test_passes_a_timeout(self):
        seen = {}
        def fake(argv, **kw):
            seen.update(kw)
            return subprocess.CompletedProcess(argv, 0, "", "")
        r = docker.StrongRunner(); r._run = fake
        r.stop(docker.container_name("r1"))
        self.assertIsNotNone(seen.get("timeout"))


class Start(unittest.TestCase):
    @mock.patch.object(docker, "worker_image", lambda *a: "img:1")
    def test_passes_a_timeout_and_surfaces_a_bad_exit(self):
        seen, verbs = {}, []
        def fake(argv, **kw):
            seen.update(kw)
            verbs.append(argv[1])
            return subprocess.CompletedProcess(argv, 0, MOUNTS, "")
        r = docker.StrongRunner(); r._run = fake
        r.start("a" * 32, Path("/w"))
        self.assertIsNotNone(seen.get("timeout"))
        self.assertEqual(verbs, ["run", "exec", "inspect"])   # inspect: the R33 mount audit

        def failing(argv, **kw):
            raise subprocess.CalledProcessError(1, argv, "", "boom")
        r2 = docker.StrongRunner(); r2._run = failing
        with self.assertRaises(subprocess.CalledProcessError):
            r2.start("a" * 32, Path("/w"))


class WorkerImage(unittest.TestCase):
    """Precedence: env, ops.env pin, the record's python row; nothing else (no image: "")."""

    def _resolve(self, ops_text, env_image=None, record=None):
        with tempfile.TemporaryDirectory() as d:
            ops = Path(d) / "ops.env"
            if ops_text is not None:
                ops.write_text(ops_text)
            if record is not None:
                (Path(d) / "images.lock").write_text(record)
            env = {"OPS_ENV_FILE": str(ops)}
            if env_image:
                env["OPS_WORKER_IMAGE"] = env_image
            with mock.patch.dict(os.environ, env):
                if not env_image:
                    os.environ.pop("OPS_WORKER_IMAGE", None)
                return docker.worker_image(Path(d))

    def test_the_ops_env_pin_beats_the_file(self):
        self.assertEqual(self._resolve("OPS_CHAT_PROFILE=x\nOPS_WORKER_IMAGE='capsule-corp-worker:1'\n"),
                         "capsule-corp-worker:1")

    def test_the_last_ops_env_assignment_wins_like_deploy_sh(self):
        self.assertEqual(self._resolve("OPS_WORKER_IMAGE='old:1'\nOPS_WORKER_IMAGE='new:2'\n"), "new:2")

    def test_the_environment_beats_ops_env(self):
        self.assertEqual(self._resolve("OPS_WORKER_IMAGE=a:1\n", env_image="b:2"), "b:2")

    def _lock(self, body, ops_text=None, env_image=None):
        return self._resolve(ops_text, env_image, record=body)

    DIGEST = "capsule-python@sha256:" + "ab" * 32

    def test_lock_row_wins(self):
        base = "capsule-base@sha256:" + "cd" * 32
        self.assertEqual(self._lock(f"# c\n{base}\n{self.DIGEST}\n"), self.DIGEST)

    def test_override_still_wins(self):
        self.assertEqual(self._lock(self.DIGEST + "\n", env_image="b:2"), "b:2")
        self.assertEqual(self._lock(self.DIGEST + "\n", ops_text="OPS_WORKER_IMAGE=a:1\n"), "a:1")

    def test_the_tracked_lock_is_never_the_image(self):
        real = (docker.paths.REPO / "docker" / "IMAGES.lock").read_text()
        self.assertRegex(real, r"(?m)^capsule-python@sha256:[0-9a-f]{64}$")
        self.assertEqual(self._resolve(None), "")   # no record: no image, whatever the repo holds

    def test_tag_without_digest_refused(self):
        with mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            self.assertEqual(self._lock("capsule-python:local\n"), "")
        self.assertEqual(err.getvalue().count("refused"), 1)

    def test_no_row_and_no_pin_is_no_image(self):
        self.assertEqual(self._lock("# no rows\n"), "")
        commented = "# OPS_WORKER_IMAGE=commented:1\n"
        self.assertEqual(self._lock("# no rows\n", ops_text=commented), "")

    def test_the_boards_project_row_beats_the_python_row(self):
        proj = "capsule-proj-bd@sha256:" + "ef" * 32
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "images.lock").write_text(f"{self.DIGEST}\n{proj}\n")
            with mock.patch.dict(os.environ, {"OPS_ENV_FILE": "/nonexistent/ops.env"}):
                os.environ.pop("OPS_WORKER_IMAGE", None)
                self.assertEqual(docker.worker_image(Path(d), "BD"), proj)
                self.assertEqual(docker.worker_image(Path(d), "other"), self.DIGEST)


class Exec(unittest.TestCase):
    def test_surfaces_exit_code_and_replaces_undecodable_bytes(self):
        def fake(argv, **kw):
            self.assertEqual(kw.get("errors"), "replace")
            return subprocess.CompletedProcess(argv, 7, "out", "err")
        r = docker.StrongRunner(); r._run = fake
        rc, text = r.exec("capsule-x", "true", 5)
        self.assertEqual(rc, 7)
        self.assertIn("out", text)

class Tiers(unittest.TestCase):
    def test_light_refuses(self):
        self.assertFalse(host.LightRunner().available())

    def test_read_only_roles_get_no_shell(self):
        self.assertEqual(host.HostRunner().limits("reviewer")["tools"], "Read,Glob,Grep")
        self.assertEqual(host.HostRunner().limits("validator")["tools"], "")
        self.assertIsInstance(runner_for("strong", "implementer"), docker.StrongRunner)
        self.assertIsInstance(runner_for("strong", "reviewer"), host.HostRunner)

class Mcp(unittest.TestCase):
    def test_lists_one_exec_tool(self):
        msgs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}]
        out = subprocess.run([sys.executable, "-m", "capsule.runners.exec_mcp", "--container", "x", "--spool", tempfile.mkdtemp(), "--dry-run"],
                             input="\n".join(map(json.dumps, msgs)) + "\n", capture_output=True, text=True, timeout=30)
        lines = [json.loads(l) for l in out.stdout.splitlines()]
        self.assertEqual([t["name"] for t in lines[1]["result"]["tools"]], ["exec"])

    def test_dry_run_returns_argv_zero_exit_and_default_timeout(self):
        out = subprocess.run(_mcp(tempfile.mkdtemp(), "--dry-run"),
                              input=json.dumps(_rpc("echo hi")) + "\n",
                              capture_output=True, text=True, timeout=30)
        resp = json.loads(out.stdout.splitlines()[0])
        result = resp["result"]
        self.assertEqual(result["exit_code"], 0)
        self.assertFalse(result["isError"])
        argv = json.loads(result["content"][0]["text"])
        self.assertIn(str(exec_mcp.DEFAULT_TIMEOUT_S), argv)
        self.assertIn("TERM", argv)    # the in-container timeout; the wrapper kills the session

    def test_unknown_tool_returns_jsonrpc_error(self):
        msg = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
               "params": {"name": "nope", "arguments": {}}}
        out = subprocess.run(_mcp(tempfile.mkdtemp(), "--dry-run"),
                              input=json.dumps(msg) + "\n", capture_output=True, text=True, timeout=30)
        resp = json.loads(out.stdout.splitlines()[0])
        self.assertEqual(resp["error"]["code"], -32602)

    def test_non_integer_timeout_rejected_without_crashing_server(self):
        payload = json.dumps(_rpc("true", timeout_s="soon", msg_id=1)) + "\n" \
                  + json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}) + "\n"
        out = subprocess.run(_mcp(tempfile.mkdtemp(), "--dry-run"),
                              input=payload, capture_output=True, text=True, timeout=30)
        lines = [json.loads(l) for l in out.stdout.splitlines()]
        self.assertEqual(lines[0]["error"]["code"], -32602)
        self.assertEqual(lines[1]["id"], 2)  # server kept serving after the bad request

    def test_malformed_json_line_returns_parse_error_and_keeps_serving(self):
        payload = "not json at all\n" + json.dumps({"jsonrpc": "2.0", "id": 9, "method": "tools/list"}) + "\n"
        out = subprocess.run(_mcp(tempfile.mkdtemp(), "--dry-run"),
                              input=payload, capture_output=True, text=True, timeout=30)
        lines = [json.loads(l) for l in out.stdout.splitlines()]
        self.assertEqual(lines[0]["error"]["code"], -32700)
        self.assertEqual(lines[1]["id"], 9)

    def test_exit_code_and_output_surfaced_via_a_fake_docker_binary(self):
        fakebin = Path(tempfile.mkdtemp())
        fake_docker = fakebin / "docker"
        fake_docker.write_text("#!/bin/sh\necho stdout-line\necho stderr-line 1>&2\nexit 3\n")
        fake_docker.chmod(fake_docker.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
        out = subprocess.run(_mcp(tempfile.mkdtemp(), "--docker", str(fake_docker)),
                              input=json.dumps(_rpc("true", timeout_s=5)) + "\n",
                              capture_output=True, text=True, timeout=30)
        resp = json.loads(out.stdout.splitlines()[0])
        result = resp["result"]
        self.assertEqual(result["exit_code"], 3)
        self.assertTrue(result["isError"])
        self.assertIn("stdout-line", result["content"][0]["text"])
        self.assertIn("stderr-line", result["content"][0]["text"])

    def test_spill_over_threshold_keeps_head_and_tail_inline(self):
        spool = Path(tempfile.mkdtemp())
        big = "x" * (exec_mcp.SPILL_THRESHOLD + 100)
        out = exec_mcp._maybe_spill(big, spool)
        self.assertIn("spilled", out)
        self.assertTrue((spool / "spill" / "1.txt").exists())
        self.assertEqual((spool / "spill" / "1.txt").read_text(), big)

class Protected(unittest.TestCase):
    def test_contract_paths_flagged(self):
        paths = ["contracts/implementer.md", "static/a.css", "legacy/profiles/dev/SOUL.md"]
        self.assertEqual(workspace.touches_protected(paths),
                         ["contracts/implementer.md", "legacy/profiles/dev/SOUL.md"])


class WorkspaceSecurity(unittest.TestCase):
    """Fix round 1 (Important #2): no git argument injection, no path traversal via the slug."""

    def test_branch_starting_with_dash_is_rejected_before_any_git_call(self):
        with mock.patch.object(workspace, "_git", side_effect=AssertionError("git must not run")):
            with self.assertRaises(ValueError):
                workspace.clone("https://example.invalid/r.git", "--upload-pack=x", tempfile.mkdtemp())

    def test_branch_containing_dotdot_is_rejected(self):
        with mock.patch.object(workspace, "_git", side_effect=AssertionError("git must not run")):
            with self.assertRaises(ValueError):
                workspace.clone("https://example.invalid/r.git", "../../etc", tempfile.mkdtemp())
            with self.assertRaises(ValueError):
                workspace.clone("https://example.invalid/r.git", "..", tempfile.mkdtemp())

    def test_repo_url_starting_with_dash_is_rejected(self):
        with mock.patch.object(workspace, "_git", side_effect=AssertionError("git must not run")):
            with self.assertRaises(ValueError):
                workspace.clone("--upload-pack=touch /tmp/pwned", "main", tempfile.mkdtemp())

    def test_repo_url_ext_transport_is_rejected(self):
        with mock.patch.object(workspace, "_git", side_effect=AssertionError("git must not run")):
            with self.assertRaises(ValueError):
                workspace.clone("ext::sh -c touch%20/tmp/pwned", "main", tempfile.mkdtemp())

    def test_base_ref_is_validated_in_diff_and_changed(self):
        with mock.patch.object(workspace, "_git", side_effect=AssertionError("git must not run")):
            with self.assertRaises(ValueError):
                workspace.diff(Path("/w"), "--output=/tmp/pwned")
            with self.assertRaises(ValueError):
                workspace.changed(Path("/w"), "--output=/tmp/pwned")

    def test_slug_never_produces_a_path_traversal(self):
        for branch in ("safe/name", "v1.2.3", "feature-x"):
            slug = workspace._slug(branch)
            self.assertNotIn("..", slug)
            self.assertNotIn("/", slug)

    def test_clone_uses_end_of_options_and_dash_dash(self):
        """The constructed argv itself carries the hardening flags, not just validation."""
        fakebin = Path(tempfile.mkdtemp())
        log = fakebin / "git.log"
        fake_git = fakebin / "git"
        fake_git.write_text(
            "#!/bin/sh\n"
            f"echo \"$@\" >> {log}\n"
            "while [ \"$1\" = -c ]; do shift 2; done\n"
            "case \"$1\" in\n"
            "  clone) mkdir -p \"${2#--separate-git-dir=}\" \"$5\" ;;\n"
            "  rev-parse) echo deadbeefdeadbeefdeadbeefdeadbeefdeadbeef ;;\n"
            "esac\n"
            "exit 0\n"
        )
        fake_git.chmod(fake_git.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
        root = Path(tempfile.mkdtemp())
        env = dict(os.environ)
        env["PATH"] = str(fakebin) + os.pathsep + env.get("PATH", "")
        with mock.patch.dict(os.environ, env, clear=True):
            ws = workspace.clone("https://example.invalid/repo.git", "main", str(root))
            self.assertEqual(workspace.head(ws), "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef")
        log_text = log.read_text()
        self.assertIn(" -- https://example.invalid/repo.git", log_text)
        self.assertIn("checkout --end-of-options main", log_text)
        for line in log_text.splitlines():   # final review C1: every call carries the pins
            self.assertTrue(line.startswith("-c core.hooksPath=/dev/null -c core.fsmonitor=false"), line)


def _real_local_repo():
    """A real git repo in tempfile.mkdtemp() -- git init, one commit, then one change.
    No remote is ever touched (local-only, per the task's rule)."""
    root = Path(tempfile.mkdtemp()) / "ws"     # the clone layout: git dir beside, not inside
    run = lambda *a: subprocess.run(["git", "-C", str(root), *a], check=True,
                                     capture_output=True, text=True, timeout=30)
    workspace.gitdir(root).parent.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", f"--separate-git-dir={workspace.gitdir(root)}", str(root)],
                   check=True, capture_output=True, text=True, timeout=30)
    run("config", "user.email", "t@t.test")
    run("config", "user.name", "t")
    (root / "a.txt").write_text("one\n")
    run("add", "a.txt")
    run("commit", "-q", "-m", "first")
    base = run("rev-parse", "HEAD").stdout.strip()
    (root / "a.txt").write_text("two\n")
    (root / "b.txt").write_text("new\n")
    run("add", "-A")
    run("commit", "-q", "-m", "second")
    return root, base


class WorkspaceHappyPath(unittest.TestCase):
    """Fix round 2: diff/changed happy paths against a real local repo, plus an argv-order
    assertion -- this is exactly the gap the round-1 regression (--name-only placed after
    --end-of-options, so git rejected every `changed()` call) slipped through."""

    def test_diff_happy_path_against_a_real_local_repo(self):
        ws, base = _real_local_repo()
        out = workspace.diff(ws, base)
        self.assertIn("a.txt", out)

    def test_changed_happy_path_against_a_real_local_repo(self):
        ws, base = _real_local_repo()
        files = workspace.changed(ws, base)
        self.assertEqual(sorted(files), ["a.txt", "b.txt"])

    def test_git_argv_never_places_an_option_after_end_of_options(self):
        """Every option token (e.g. --name-only) must appear *before* --end-of-options in
        every _git call this module makes -- git stops parsing options once it sees
        --end-of-options, so anything after it is misread as a revision/path."""
        calls = []
        real_git = workspace._git
        def spy(*args, **kw):
            calls.append(args)
            return real_git(*args, **kw)
        with mock.patch.object(workspace, "_git", side_effect=spy):
            ws, base = _real_local_repo()
            workspace.diff(ws, base)
            workspace.changed(ws, base)
        self.assertTrue(calls)
        for args in calls:
            if "--end-of-options" not in args:
                continue
            eo_index = args.index("--end-of-options")
            for i, tok in enumerate(args):
                if tok != "--end-of-options" and tok.startswith("-"):
                    self.assertLess(i, eo_index,
                                    f"option {tok!r} must precede --end-of-options in {args}")


class UntrackedChanges(unittest.TestCase):
    def test_changed_lists_untracked_and_ignored_files(self):
        """Final review I3: a file the worker never `git add`ed is still a change, even one its
        own .gitignore hides."""
        ws, base = _real_local_repo()
        (ws / "CLAUDE.md").write_text("planted\n")
        (ws / ".gitignore").write_text("AGENTS.md\n")
        (ws / "AGENTS.md").write_text("planted\n")
        files = workspace.changed(ws, base)
        self.assertIn("CLAUDE.md", files)
        self.assertIn("AGENTS.md", files)
        self.assertEqual(sorted(workspace.touches_protected(files)), ["AGENTS.md", "CLAUDE.md"])


BRANCH = "epic/x"


class HostGitHardening(unittest.TestCase):
    """Final review C1: nothing a run writes into its (container-mounted) clone may make the
    host's git calls run a command. The planted hooks and fsmonitor below each append to a
    marker file; after every host-side workspace call the marker must not exist."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.origin = self.tmp / "origin"
        self.origin.mkdir()
        g = lambda *a: subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t.test", *a],
                                      cwd=self.origin, check=True, capture_output=True, timeout=30)
        g("init", "-q", "-b", BRANCH)
        (self.origin / "README.md").write_text("one\n")
        g("add", ".")
        g("commit", "-qm", "init")
        self.root = self.tmp / "work"
        self.marker = self.tmp / "PWNED"

    def _script(self, name):
        p = self.tmp / "evil" / name
        p.parent.mkdir(exist_ok=True)
        p.write_text(f"#!/bin/sh\necho {name} >> '{self.marker}'\n")
        p.chmod(0o755)
        return p

    def _plant(self, gitdir):
        hooks = Path(gitdir) / "hooks"
        hooks.mkdir(exist_ok=True)
        for hook in ("post-checkout", "reference-transaction", "post-index-change", "pre-commit",
                     "prepare-commit-msg", "commit-msg", "post-commit"):
            shutil.copy(self._script(hook), hooks / hook)
        subprocess.run(["git", "config", "--file", str(Path(gitdir) / "config"), "core.fsmonitor",
                        str(self._script("fsmonitor"))], check=True, capture_output=True, timeout=30)

    @staticmethod
    def _gitdir(ws):
        return Path(subprocess.run(["git", "rev-parse", "--absolute-git-dir"], cwd=ws, check=True,
                                   capture_output=True, text=True, timeout=30).stdout.strip())

    def _exercise(self, ws, base):
        (ws / "README.md").write_text("two\n")      # a dirty worktree makes diff refresh the index
        self.assertEqual(workspace.clone(str(self.origin), BRANCH, self.root), ws)
        workspace.head(ws)
        workspace.diff(ws, base)
        workspace.changed(ws, base)
        (ws / "new.txt").write_text("new\n")
        self.assertTrue(workspace.commit(ws, "card"))

    def _assert_nothing_ran(self):
        self.assertFalse(self.marker.exists(), self.marker.read_text() if self.marker.exists() else "")

    def test_hooks_and_fsmonitor_in_the_clone_git_dir_never_run(self):
        ws = workspace.clone(str(self.origin), BRANCH, self.root)
        base = workspace.head(ws)
        self._plant(self._gitdir(ws))
        self._exercise(ws, base)
        self._assert_nothing_ran()

    def test_a_git_dir_planted_in_the_mounted_workspace_is_never_used(self):
        """What a container with write access to /workspace can do: swap `.git` for a repository
        of its own that carries hooks and an fsmonitor."""
        ws = workspace.clone(str(self.origin), BRANCH, self.root)
        base = workspace.head(ws)
        evil = self.tmp / "evil.git"
        shutil.copytree(self._gitdir(ws), evil)
        dot = ws / ".git"
        shutil.rmtree(dot) if dot.is_dir() and not dot.is_symlink() else dot.unlink()
        shutil.copytree(evil, dot)
        self._plant(dot)
        self._exercise(ws, base)
        self._assert_nothing_ran()

    def test_a_fifo_at_the_workspace_git_pointer_never_blocks_clone(self):
        """Fix round 2, N2: the container owns /workspace/.git and can make it a FIFO; reuse must
        replace it, never open it."""
        import threading
        ws = workspace.clone(str(self.origin), BRANCH, self.root)
        dot = ws / ".git"
        dot.unlink()
        os.mkfifo(dot)
        out = {}
        t = threading.Thread(target=lambda: out.setdefault(
            "ws", workspace.clone(str(self.origin), BRANCH, self.root)), daemon=True)
        t.start()
        t.join(20)
        if t.is_alive():   # unstick the writer blocked on the FIFO before failing
            os.close(os.open(dot, os.O_RDONLY | os.O_NONBLOCK))
            t.join(5)
            self.fail("clone() blocked on a FIFO at ws/.git")
        self.assertEqual(out["ws"], ws)
        self.assertTrue(stat.S_ISREG(os.lstat(dot).st_mode))
        self.assertEqual(dot.read_text(), f"gitdir: ../.gitdirs/{ws.name}\n")

    def test_the_relative_pointer_reaches_the_git_dir_for_host_git(self):
        """The pointer is relative (a container mounts the git dir at /.gitdirs/<name>), and
        plain host git through it, after a fresh clone and after a reuse, finds the same dir."""
        ws = workspace.clone(str(self.origin), BRANCH, self.root)
        for _ in range(2):
            self.assertEqual((ws / ".git").read_text(), f"gitdir: ../.gitdirs/{ws.name}\n")
            r = subprocess.run(["git", "-C", str(ws), "rev-parse", "--absolute-git-dir", "HEAD"],
                               check=True, capture_output=True, text=True, timeout=30)
            found, sha = r.stdout.split()
            self.assertEqual(Path(found).resolve(), workspace.gitdir(ws).resolve())
            self.assertEqual(sha, workspace.head(ws))
            workspace.clone(str(self.origin), BRANCH, self.root)

    def test_the_git_dir_lives_outside_the_mounted_workspace(self):
        ws = workspace.clone(str(self.origin), BRANCH, self.root)
        self.assertFalse(self._gitdir(ws).resolve().is_relative_to(ws.resolve()))

    def test_git_run_by_the_claude_process_uses_the_safe_git_dir(self):
        """Claude Code itself runs `git status` in its cwd on the host; a `.git` the implementer
        planted in the workspace must not be the one it reads."""
        ws = workspace.clone(str(self.origin), BRANCH, self.root)
        evil = self.tmp / "evil.git"
        shutil.copytree(self._gitdir(ws), evil)
        dot = ws / ".git"
        shutil.rmtree(dot) if dot.is_dir() and not dot.is_symlink() else dot.unlink()
        shutil.copytree(evil, dot)
        self._plant(dot)
        (ws / "README.md").write_text("two\n")
        fakebin = self.tmp / "bin"
        fakebin.mkdir()
        fake = fakebin / "claude"
        fake.write_text("#!/bin/sh\ncat >/dev/null\ngit status >/dev/null 2>&1\n"
                        "echo '{\"type\":\"result\",\"is_error\":false,\"structured_output\":{\"a\":1},\"usage\":{}}'\n")
        fake.chmod(0o755)
        route = {"provider": "claude", "account": "default", "model": "m", "fallback": None, "turn_budget": 3}
        limits = {"schema": {}, "tools": "", "git_dir": str(workspace.gitdir(ws))}
        with mock.patch.dict(os.environ, {"PATH": f"{fakebin}{os.pathsep}{os.environ['PATH']}"}):
            r = claude.ClaudeAdapter(route).run("implementer", {"system": "s", "prompt": "p"}, str(ws), limits)
        self.assertEqual(r.report, {"a": 1})
        self._assert_nothing_ran()

    def test_the_clone_is_full_not_shallow(self):
        """t_e1db2eef: the container's git dir is read-only by design (C1/P6/R2 above), so the
        implementer's only way to undo a stray change without the index is `git show HEAD:<path>
        > <path>` (real runs ad9014ed379d48828d445130b5903bd3, 66825da3011246d78f5b62c4c3f21e1c
        on t_de1466d6). That, and the host's own `read_at`/`diff`/`changed` (workspace.py), need
        full history in the clone; `clone()` (and, on reuse, `fetch`) must never pass `--depth`
        to git. Opus review, fix round 1 item 4: a local filesystem path clone ignores `--depth`
        outright, so a mutation adding it there would still pass here - `self.origin.as_uri()`
        (a real `file://` transport) is what makes the assertion discriminate."""
        g = lambda *a: subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t.test", *a],
                                      cwd=self.origin, check=True, capture_output=True, timeout=30)
        for n in ("two", "three"):
            (self.origin / "README.md").write_text(n + "\n")
            g("commit", "-aqm", n)
        origin_uri = self.origin.as_uri()
        ws = workspace.clone(origin_uri, BRANCH, self.root)
        gd = self._gitdir(ws)
        self.assertFalse((gd / "shallow").exists())
        count = lambda ref: subprocess.run(["git", "--git-dir", str(gd), "rev-list", "--count", ref],
                                           check=True, capture_output=True, text=True, timeout=30).stdout.strip()
        self.assertEqual(count("HEAD"), "3")
        # the reuse path (repoint + fetch + checkout, not a fresh clone) must stay full too. Fetch
        # updates the remote-tracking ref, not the checked-out local branch (workspace.py `tip`'s
        # own note: checkout never fast-forwards it), so the new commit is checked for reachability
        # there.
        (self.origin / "README.md").write_text("four\n")
        g("commit", "-aqm", "four")
        self.assertEqual(workspace.clone(origin_uri, BRANCH, self.root), ws)
        self.assertFalse((gd / "shallow").exists())
        self.assertEqual(count(f"refs/remotes/origin/{BRANCH}"), "4")


class HarnessCommit(unittest.TestCase):
    """Pilot-prep review: the host commit of a passed card uses the fixed harness identity (never
    the operator's config or environment) and never carries the worker scratch dir."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        origin = self.tmp / "origin"
        origin.mkdir()
        g = lambda *a: subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t.test", *a],
                                      cwd=origin, check=True, capture_output=True, timeout=30)
        g("init", "-q", "-b", BRANCH)
        (origin / "README.md").write_text("one\n")
        g("add", ".")
        g("commit", "-qm", "init")
        self.ws = workspace.clone(str(origin), BRANCH, self.tmp / "work")
        self.base = workspace.head(self.ws)

    def _log(self, fmt):
        return workspace._git("log", "-1", f"--format={fmt}", ws=self.ws).stdout.strip()

    def test_identity_is_fixed_whatever_the_operator_env_and_global_config_say(self):
        home = self.tmp / "home"
        home.mkdir()
        (home / ".gitconfig").write_text("[user]\n\tname = Alex\n\temail = me@example.com\n"
                                         "[commit]\n\tgpgSign = true\n")
        env = {"HOME": str(home), "GIT_AUTHOR_NAME": "Env", "GIT_AUTHOR_EMAIL": "env@example.com",
               "GIT_COMMITTER_NAME": "Env", "GIT_COMMITTER_EMAIL": "env@example.com", "EMAIL": "e@example.com"}
        (self.ws / "complete.html").write_text("x\n")
        with mock.patch.dict(os.environ, env):
            sha = workspace.commit(self.ws, "task 7")
        self.assertEqual(sha, workspace.head(self.ws))
        self.assertEqual(self._log("%an <%ae>|%cn <%ce>"), "Capsule <capsule@localhost>|Capsule <capsule@localhost>")
        self.assertEqual(self._log("%G?"), "N")

    def test_the_scratch_dir_never_reaches_a_diff_or_a_commit(self):
        scratch = self.ws / ".worker-exec"
        scratch.mkdir()
        (scratch / "tmp123").write_text("scratch\n")    # its own `*` .gitignore deleted by the run
        (self.ws / "complete.html").write_text("x\n")
        diff = workspace.diff(self.ws, self.base)
        self.assertIn("complete.html", diff)
        self.assertNotIn(".worker-exec", diff)
        workspace.commit(self.ws, "task 7")
        files = workspace._git("show", "--name-only", "--format=", "HEAD", ws=self.ws).stdout.split()
        self.assertEqual(files, ["complete.html"])

    def test_scratch_only_changes_commit_nothing(self):
        (self.ws / ".worker-exec").mkdir()
        (self.ws / ".worker-exec" / "f").write_text("s\n")
        self.assertIsNone(workspace.commit(self.ws, "nothing"))
        self.assertEqual(workspace.head(self.ws), self.base)


class DockerSmoke(unittest.TestCase):
    @unittest.skipUnless(os.environ.get("CAPSULE_DOCKER_TESTS") == "1", "real docker; opt in with CAPSULE_DOCKER_TESTS=1")
    def test_live_container_has_no_network(self):
        r = docker.StrongRunner()
        run_id = "t" * 32
        tmp = Path(tempfile.mkdtemp())
        name = r.start(run_id, tmp)
        try:
            code, out = r.exec(name, "getent hosts example.com || echo none", 30)
            self.assertIn("none", out)
        finally:
            r.stop(name)
