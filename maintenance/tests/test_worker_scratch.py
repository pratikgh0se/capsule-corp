"""U1 tier-1, card 2 (t_cd2e6a3b): the worker scratch dir moves off
/var/tmp into the bind-mounted /workspace, because the legacy runner mounts a
--tmpfs /var/tmp:rw,noexec,nosuid,size=256m on EVERY worker container
unconditionally (the legacy runner's environments/docker.py:374-381,
_BASE_SECURITY_ARGS, combined in _build_security_args at :669-681), which
shadowed the old image-baked /var/tmp/worker-exec and made `mktemp -d` fail
inside a real worker (experiment 1, row 357). /workspace is where the legacy runner
bind-mounts the card's worktree (docker_mount_cwd_to_workspace: true) and is
never tmpfs-shadowed, so the scratch dir - and TMPDIR - move there.

This suite is pure grep/regex/subprocess-of-plain-sh - no docker needed - so
it fails even on a runner that cannot build or run the image
(tests/test_docker_image.sh is docker-gated and skips there).
"""
import os
import re
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from capsule import legacy_names as _LN  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
DOCKERFILE = REPO / "docker" / "base" / "Dockerfile"
ENTRYPOINT = REPO / "docker" / "base" / "worker-entrypoint.sh"
PROBE_SCRIPT = REPO / "tests" / "test_docker_image.sh"
LEGACY_DOCKER_PY = Path(f"~/CodeBase/{_LN.NAME}-agent/tools/environments/docker.py").expanduser()

# The exact legacy runner arg list a real worker container runs under (task-1-brief
# item 3), which tests/test_docker_image.sh's probe must use verbatim so it
# cannot drift back to testing a bare `docker run`.
LEGACY_ARG_STRINGS = [
    "--cap-drop", "ALL",
    "--cap-add", "DAC_OVERRIDE",
    "--cap-add", "CHOWN",
    "--cap-add", "FOWNER",
    "--security-opt", "no-new-privileges",
    "--tmpfs", "/tmp:rw,nosuid,size=512m",
    "--tmpfs", "/var/tmp:rw,noexec,nosuid,size=256m",
    "--tmpfs", "/run:rw,noexec,nosuid,size=64m",
    "--network", "none",
]


class DockerfileTest(unittest.TestCase):
    def setUp(self):
        self.assertTrue(DOCKERFILE.is_file(), DOCKERFILE)
        self.text = DOCKERFILE.read_text()

    def test_tmpdir_env_is_workspace_worker_exec(self):
        self.assertRegex(
            self.text,
            r"ENV\s+TMPDIR=/workspace/\.worker-exec",
            "Dockerfile must set ENV TMPDIR=/workspace/.worker-exec",
        )

    def test_no_var_tmp_tmpdir_left_behind(self):
        for line in self.text.splitlines():
            if "TMPDIR" in line:
                self.assertNotIn(
                    "/var/tmp",
                    line,
                    f"stale /var/tmp TMPDIR line: {line!r}",
                )

    def test_worker_exec_dir_env_set(self):
        self.assertRegex(
            self.text,
            r"ENV\s+WORKER_EXEC_DIR=/workspace/\.worker-exec",
            "Dockerfile must set ENV WORKER_EXEC_DIR=/workspace/.worker-exec",
        )

    def test_entrypoint_copied_and_named(self):
        self.assertIn(
            "worker-entrypoint",
            self.text,
            "Dockerfile must COPY the entrypoint script and reference it in ENTRYPOINT",
        )
        self.assertRegex(
            self.text,
            r'ENTRYPOINT\s+\["[^"]*worker-entrypoint[^"]*"',
            "Dockerfile must set ENTRYPOINT to the worker-entrypoint script",
        )
        self.assertRegex(
            self.text,
            r"COPY\s+\S*worker-entrypoint\S*\s+/usr/local/bin/",
            "Dockerfile must COPY the entrypoint script into /usr/local/bin",
        )


class EntrypointScriptStaticTest(unittest.TestCase):
    def setUp(self):
        self.assertTrue(ENTRYPOINT.is_file(), ENTRYPOINT)
        self.text = ENTRYPOINT.read_text()

    def test_ends_in_exec_argv(self):
        self.assertRegex(
            self.text.rstrip(),
            r'exec\s+"\$@"\s*$',
            'entrypoint must end in exec "$@" so `docker run <image> <cmd>` and '
            '`docker run ... <image> sleep infinity` both still work',
        )

    def test_creates_dir_mode_1777(self):
        self.assertIn("mkdir -p", self.text)
        self.assertIn("1777", self.text)

    def test_writes_gitignore_star(self):
        self.assertRegex(
            self.text,
            r"gitignore",
            "entrypoint must write a .gitignore into the scratch dir",
        )
        # The .gitignore content itself must be a bare "*" so the scratch dir
        # never shows up in `git status`/`git add -A` of the worktree. Match
        # the actual write (printf/echo '*' redirected into .gitignore), not
        # just any "*" appearing anywhere in the script (e.g. in a comment).
        self.assertRegex(
            self.text,
            r"""(printf|echo)\s+['"]?\*\\?n?['"]?\s*>[^\n]*\.gitignore""",
            "entrypoint must write a bare '*' into .gitignore, not just mention one",
        )


class EntrypointScriptRuntimeTest(unittest.TestCase):
    """Runs the entrypoint as plain sh on the host - no docker involved."""

    def setUp(self):
        self.assertTrue(ENTRYPOINT.is_file(), ENTRYPOINT)

    def test_creates_dir_and_execs_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = os.path.join(tmp, ".worker-exec")
            env = dict(os.environ, WORKER_EXEC_DIR=target)
            result = subprocess.run(
                ["sh", str(ENTRYPOINT), "sh", "-c", "echo hi"],
                env=env,
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), "hi")
            self.assertTrue(os.path.isdir(target), target)
            mode = stat.S_IMODE(os.stat(target).st_mode)
            self.assertEqual(oct(mode), oct(0o1777))
            gitignore = os.path.join(target, ".gitignore")
            self.assertTrue(os.path.isfile(gitignore), gitignore)
            self.assertEqual(Path(gitignore).read_text().strip(), "*")

    def test_uncreatable_dir_still_execs_command(self):
        # Best-effort: a worker must still start even if the scratch dir
        # cannot be created (e.g. /workspace not writable / not bind-mounted
        # in this mode). Root bypasses filesystem permission bits, so a
        # 0o000-mode parent doesn't actually block mkdir when running as
        # root (e.g. inside a root-run container) - skip rather than assert
        # a premise this process can't create.
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            self.skipTest("running as root: permission bits don't block mkdir")
        with tempfile.TemporaryDirectory() as tmp:
            locked_parent = os.path.join(tmp, "locked")
            os.mkdir(locked_parent, 0o000)
            target = os.path.join(locked_parent, "sub", ".worker-exec")
            env = dict(os.environ, WORKER_EXEC_DIR=target)
            try:
                result = subprocess.run(
                    ["sh", str(ENTRYPOINT), "sh", "-c", "echo hi"],
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
            finally:
                os.chmod(locked_parent, 0o700)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), "hi")


    def test_existing_readonly_star_gitignore_is_left_alone(self):
        # image.yml probe (c): the image bakes /workspace/.worker-exec/
        # .gitignore in at build time as root, mode 0644 (Dockerfile). A
        # non-root worker (--user uid:gid, no /workspace bind mount) can
        # never rewrite a file it doesn't own - the entrypoint must notice
        # the "*" is already there and skip the write, rather than attempt
        # it, fail with EACCES and print to stderr.
        with tempfile.TemporaryDirectory() as tmp:
            target = os.path.join(tmp, ".worker-exec")
            os.mkdir(target, 0o1777)
            gitignore = os.path.join(target, ".gitignore")
            Path(gitignore).write_text("*\n")
            os.chmod(gitignore, 0o444)
            env = dict(os.environ, WORKER_EXEC_DIR=target)
            try:
                result = subprocess.run(
                    ["sh", str(ENTRYPOINT), "sh", "-c", "echo hi"],
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
            finally:
                os.chmod(gitignore, 0o644)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stderr, "")
            self.assertEqual(result.stdout.strip(), "hi")
            self.assertEqual(Path(gitignore).read_text().strip(), "*")


class ProbeScriptDriftTest(unittest.TestCase):
    """tests/test_docker_image.sh must probe under the SAME args the legacy runner uses,
    so the probe cannot silently drift back to testing a bare `docker run`
    that a real worker never gets."""

    def setUp(self):
        self.assertTrue(PROBE_SCRIPT.is_file(), PROBE_SCRIPT)
        self.text = PROBE_SCRIPT.read_text()

    def test_contains_every_legacy_arg(self):
        for arg in LEGACY_ARG_STRINGS:
            self.assertIn(
                arg,
                self.text,
                f"tests/test_docker_image.sh is missing legacy runner arg {arg!r}",
            )

    def test_probes_workspace_worker_exec(self):
        self.assertIn("/workspace/.worker-exec", self.text)


class LegacySourceDriftTest(unittest.TestCase):
    """Optional: if the legacy runner checkout is present, parse its
    _BASE_SECURITY_ARGS list literal (regex over the text, no import - it is
    a sibling repo) and assert every --tmpfs entry there also appears in
    tests/test_docker_image.sh, so a future change to the legacy runner's own security
    args doesn't silently invalidate this probe."""

    def setUp(self):
        if not LEGACY_DOCKER_PY.is_file():
            self.skipTest(f"{LEGACY_DOCKER_PY} not present on this machine")

    def test_base_security_args_tmpfs_entries_covered(self):
        text = LEGACY_DOCKER_PY.read_text()
        m = re.search(r"_BASE_SECURITY_ARGS\s*=\s*\[(.*?)\]", text, re.DOTALL)
        self.assertIsNotNone(m, "could not find _BASE_SECURITY_ARGS in docker.py")
        body = m.group(1)
        tmpfs_entries = re.findall(r'"--tmpfs",\s*"([^"]+)"', body)
        self.assertTrue(tmpfs_entries, "found no --tmpfs entries in _BASE_SECURITY_ARGS")
        probe_text = PROBE_SCRIPT.read_text()
        for entry in tmpfs_entries:
            self.assertIn(
                entry,
                probe_text,
                f"tests/test_docker_image.sh is missing legacy runner --tmpfs {entry!r}",
            )


if __name__ == "__main__":
    unittest.main()
