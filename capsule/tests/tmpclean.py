"""One private temp root per suite process, removed at exit (t_b346fd02).

The suites made ~800 temp entries per run (module-level and setUp mkdtemp with no cleanup,
read-only trees under prior/*/report). install() points $TMPDIR (so tempfile, child
processes and mktemp) at a fresh dir under the real $TMPDIR; the exit sweep makes
everything writable first, then removes it. It also points HOME, LEGACY_KANBAN_HOME, CAPSULE_HOME
and OBSIDIAN_VAULT_PATH at dirs inside that root and forces AGENT_OPS_NO_SEND=1, so no module read
at import (`Path.home()` constants in ai_usage, identity, merge_gate, ...) sees the real old home,
~/.config or ~/.local/state and suites can run in parallel (w312). Imported by test_0_tmpsweep.py
in each suite dir.
A single-module run: `-m unittest capsule.tests.test_x` imports the package, which installs this;
`discover -p` in maintenance/tests or tests/ skips the stub, so mutation_check wraps it
(tests/discover_tmp.py)."""
import atexit
import contextlib
import os
import shutil
import signal
import subprocess
import sys
import tempfile
from capsule import legacy_names as _LN  # noqa: E402

_root = None
APPLE_SHIM = "/usr/bin/git"
OVERRIDES = ("AGENT_OPS_STATE_DIR", "AGENT_OPS_BOARDS_DIR", "AGENT_OPS_DEPS_CACHE",
             "CAPSULE_BACKUP_DIR", "CAPSULE_UNBLOCKED_CACHE", "CAPSULE_CLAUDE_PROJECTS_DIR",
             "CAPSULE_CONFIG_DIR", "CAPSULE_JOBS_DIR", "CAPSULE_IDENTITY_DIR", "CAPSULE_KEEPER_LOG",
             "LEGACY_HOME", _LN.OLD_ENV_HOME)


def _writable(path):
    with contextlib.suppress(OSError):
        os.chmod(path, 0o700)


def sweep(root, pid=None):
    """Remove `root`; a no-op in a forked child (`pid` given and not ours), never raises."""
    if (pid is not None and pid != os.getpid()) or not os.path.isdir(root):
        return
    _writable(root)
    for here, dirs, _ in os.walk(root):
        for d in dirs:
            if not os.path.islink(os.path.join(here, d)):
                _writable(os.path.join(here, d))
    shutil.rmtree(root, ignore_errors=True)


def _exec_path(git):
    """`git --exec-path` of `git`; None when it fails, prints nothing or prints a relative path."""
    try:
        done = subprocess.run([git, "--exec-path"], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    out = done.stdout.strip()
    return out if done.returncode == 0 and os.path.isabs(out) else None


def real_git_dir(root):
    """A dir under `root` whose `git` is the binary macOS's /usr/bin/git shim runs, or None. The
    shim looks the developer dir up on every call: ~10 ms of a ~16 ms `git rev-parse`, and the
    tick suite alone makes ~7,400 calls. A link outside `.../usr/bin` can make git fall back to
    another install's exec path (Xcode's under DEVELOPER_DIR=Command Line Tools, which the
    light-tier sandbox cannot read), so the link is kept only if its exec path equals the shim's."""
    if "GIT_EXEC_PATH" in os.environ or shutil.which("git") != APPLE_SHIM:
        return None
    want = _exec_path(APPLE_SHIM)
    if want is None:
        return None
    real = os.path.join(os.path.dirname(os.path.dirname(want)), "bin", "git")
    if not os.access(real, os.X_OK) or os.path.realpath(real) == APPLE_SHIM:
        return None
    link_dir = os.path.join(root, "gitbin")
    os.mkdir(link_dir)
    os.symlink(real, os.path.join(link_dir, "git"))
    if _exec_path(os.path.join(link_dir, "git")) != want:
        shutil.rmtree(link_dir)
        return None
    return link_dir


def _arm_sweep(root):
    """Sweep `root` at exit, and on SIGTERM/SIGHUP (a killed run skips atexit: that left the
    capsule-suite-* roots behind). CAPSULE_TEST_KEEP_TMP=1 keeps it for debugging, path printed."""
    if os.environ.get("CAPSULE_TEST_KEEP_TMP") == "1":
        print(f"CAPSULE_TEST_KEEP_TMP=1: keeping {root}", file=sys.stderr)
        return
    pid = os.getpid()
    atexit.register(sweep, root, pid)

    def _die(signum, frame):
        sweep(root, pid)
        signal.signal(signum, signal.SIG_DFL)
        os.kill(os.getpid(), signum)

    for sig in (signal.SIGTERM, signal.SIGHUP):
        with contextlib.suppress(ValueError, OSError):   # not the main thread
            if signal.getsignal(sig) == signal.SIG_DFL:
                signal.signal(sig, _die)


def install():
    global _root
    if _root is None:
        # gettempdir() is absolute and ignores an empty $TMPDIR, so the root never lands in the cwd.
        _root = tempfile.mkdtemp(prefix="capsule-suite-", dir=tempfile.gettempdir())
        os.environ["TMPDIR"] = _root
        # tempfile caches its dir on first use; without this a gettempdir() call made before
        # install() silently sends every mkdtemp elsewhere (row 591 requires it; simplicity_check
        # rule 5 flags the assignment, row 591 outranks it for this module).
        tempfile.tempdir = _root
        _arm_sweep(_root)
        # Forced, not setdefault: an inherited real value is exactly what must not leak in.
        # The one real-home read kept: uv's offline tool cache (ruff for the gate scripts the
        # tick tests run). A tool cache, not Capsule state; a temp HOME has no ruff and no network.
        os.environ.setdefault("UV_CACHE_DIR", os.path.expanduser("~/.cache/uv"))
        home = os.path.join(_root, "home")
        os.mkdir(home)
        for name, sub in (("HOME", ""), ("LEGACY_KANBAN_HOME", _LN.NAME),
                          ("CAPSULE_HOME", "capsule"), ("OBSIDIAN_VAULT_PATH", "vault")):
            path = os.path.join(home, sub) if sub else home
            os.makedirs(path, exist_ok=True)
            os.environ[name] = path
        # An inherited switch or interpreter would escape the temp root; old names go too.
        for name in ("LEGACY_PRESENT", "CAPSULE_OPS_PY"):
            os.environ.pop(name, None)
        os.environ["AGENT_OPS_NO_SEND"] = "1"
        fast_git = real_git_dir(_root)
        if fast_git:
            os.environ["PATH"] = fast_git + os.pathsep + os.environ.get("PATH", "")
        # State-path overrides a caller may have exported: re-point, never inherit. Unset ones stay
        # unset (their defaults now derive from the temp HOME, which is what the tests expect).
        for name in OVERRIDES:
            if name in os.environ:
                os.environ[name] = os.path.join(home, "override", name.lower())
    return _root
