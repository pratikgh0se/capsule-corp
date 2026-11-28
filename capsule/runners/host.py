"""Host tiers: read-only roles run on the host, no container (spec §6.6); the light tier runs
the implementer's shell under a macOS seatbelt profile (rows 373, 378, 395; ENG-12)."""
import contextlib
import ctypes
import json
import os
import signal
import subprocess
import sys
from pathlib import Path

from capsule import paths
from capsule.runners import seatbelt

SANDBOX_EXEC = "/usr/bin/sandbox-exec"
SCRATCH = ".worker-exec"                     # TMPDIR and HOME of a sandboxed command (row 357)
CLT = "/Library/Developer/CommandLineTools"  # the /usr/bin git and python3 shims resolve here


def _kill_under(wt):
    """Kill what an ended run's sandboxed shell left running. A command that calls setsid leaves
    its process group, so the exec server's group kill misses it; what it cannot change is where it
    works, so a process with its cwd or an open file under the realpath `wt` (`lsof +D`) is a
    candidate. Only a process that is itself sandboxed (libSystem `sandbox_check(pid, NULL, 0)` is
    1) is SIGKILLed: the user's own processes, a reviewer's `claude`, the tick, whatever else
    works in the worktree, are never touched, and nothing the worker writes (an argv, an env)
    can exempt it. A daemon that chdir'd away and closed every file there is out of reach; it
    still has no network. Returns the pids killed, or None when the scan could not run."""
    try:
        held = subprocess.run(["/usr/sbin/lsof", "-w", "-F", "p", "+D", wt], capture_output=True,
                              text=True, timeout=60).stdout
        sandbox_check = ctypes.CDLL("/usr/lib/libSystem.B.dylib").sandbox_check
        sandbox_check.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int]
    except (OSError, AttributeError, subprocess.SubprocessError):
        return None
    killed = []
    pids = {int(ln[1:]) for ln in held.splitlines() if ln[:1] == "p" and ln[1:].isdigit()}
    for pid in sorted(pids):
        if sandbox_check(pid, None, 0) == 1:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(pid, signal.SIGKILL)
                killed.append(pid)
    return killed


class HostRunner:
    """No container: used for roles that only read (reviewer, validator, guide, korin). Never a shell (P2)."""

    @staticmethod
    def _seatbelt_runner(tier):
        """The seatbelt tier's runner: light, or the Mac lane's for `mac`, imported here. This
        runner module is the one the sandbox runner's import closure does not follow
        (`SANDBOX_EXEMPT`), so that closure never reaches mac.py."""
        if tier == "mac":
            from capsule.runners import mac
            return mac.MacRunner()
        return LightRunner()

    def available(self):
        return True

    def limits(self, role, spool=None):
        """Read-only tools; given the run's spool, also Write, pre-approved (dontAsk) for its
        `requests/` dir only, so the role can file a request/decision document."""
        base = "Read,Glob,Grep" if role in ("reviewer", "korin") else ""   # korin reads repo + evidence (row 456)
        if spool is None:
            return {"tools": base}
        requests = Path(spool).absolute() / "requests"
        return {"tools": ",".join(t for t in (base, "Write") if t), "permission_mode": "dontAsk",
                # `//` is Claude Code's absolute-path prefix in a permission rule; Edit rules
                # cover every built-in file-writing tool, Write included
                "allowed": f"Edit(/{requests}/**)", "add_dirs": [str(requests)]}


class LightRunner:
    """Light tier (rows 373, 378): no container. The implementer's `claude` stays on the host (it
    needs the model endpoint and the keychain); the shell it runs through the exec MCP tool runs
    under `sandbox-exec` with a profile rendered for that card's worktree (exec_mcp
    `_run_sandboxed`). Off unless the board's `redesign.light_runner` switch is on: the tick sets
    `enabled` through `runners.scoped`."""

    enabled = False
    switch = "light_runner"   # the board's `redesign` switch `runners.scoped` copies into `enabled`
    tier = "light"            # the tier name in `limits` and the run's handle (Mac lane: "mac")
    home = None   # the capsule home `reap` reads spools from; None is `paths.home()`

    def __init__(self):
        # handle -> (worktree, profile text, *exec server args), from start() to limits()
        self._runs = {}

    def available(self):
        # Switch off, or no sandbox-exec: a light card waits (row 378), it never runs unsandboxed.
        return self.enabled and os.path.exists(SANDBOX_EXEC)

    def start(self, run_id, workspace_dir, requests_dir=None, git_dir=None, tmpdir=None, *,
              profile=None, server_args=()):
        """Prepare one run's sandbox; returns its handle (nothing starts, there is no container).
        `requests_dir`: unused, the host-side `claude` writes requests, never the sandboxed shell.
        `tmpdir`: unused, the strong tier's /tmp tmpfs; here /tmp is not writable, SCRATCH is.
        `git_dir`: the clone's git dir, readable so `git status` and `git diff` work, unwritable so
        commits stay host-side as in the strong tier. Network: none (no `endpoints`); the model
        endpoint needs a host-side localhost proxy (card 22), and `*:443` is never opened.
        `profile`, `server_args`: the Mac lane's own profile text and exec server arguments
        (its developer dir and SDK); the light tier passes neither."""
        wt = os.path.realpath(workspace_dir)
        # The profile denies writes to a protected path's parent directories (rename guard), which
        # also denies creating one that is missing: create them now, before the profile applies.
        # Never through a symlink: a worker-planted `capsule -> /elsewhere` would have mkdir (which
        # runs here, unsandboxed) create `capsule/blind` outside the worktree.
        for rel in (SCRATCH, *seatbelt.guarded_dirs()):
            parts = rel.split("/")
            through_link = any(os.path.islink(os.path.join(wt, *parts[:i]))
                               for i in range(1, len(parts) + 1))
            if not through_link and not os.path.exists(os.path.join(wt, rel)):
                os.mkdir(os.path.join(wt, rel))
        name = f"{self.tier}-{run_id[:12]}"
        self._runs[name] = (wt, profile or seatbelt.render(
            wt, vault_read=[git_dir] if git_dir else ()), *server_args)
        return name

    def limits(self, name, spool):
        """Same shape as the strong tier's: the exec MCP tool, here with `--sandbox-profile`."""
        wt, profile, *server_args = self._runs[name]
        spool = Path(spool).absolute()
        spool.mkdir(parents=True, exist_ok=True)
        sb = spool / "seatbelt.sb"   # in the spool, outside the worktree: the shell cannot edit it
        sb.write_text(profile, encoding="utf-8")
        (spool / "worktree").write_text(wt, encoding="utf-8")   # where `reap` finds the worktree
        mcp_path = spool / "mcp.json"
        # `-P`: the server starts with the worktree as its cwd, and without it `-m` would load a
        # worker-planted `<worktree>/capsule/runners/exec_mcp.py` first, unsandboxed.
        mcp_path.write_text(json.dumps({"mcpServers": {"capsule": {
            "command": sys.executable,
            "args": ["-P", "-m", "capsule.runners.exec_mcp", "--workspace", wt,
                     "--sandbox-profile", str(sb), "--spool", str(spool), *server_args],
            "env": {"PYTHONPATH": str(paths.REPO)}}}}))
        return {"tools": "Read,Edit,Write,Glob,Grep",
                "allowed": "mcp__capsule__exec", "permission_mode": "acceptEdits",
                "add_dirs": [str(spool / "requests")], "mcp_config": str(mcp_path),
                "tier": self.tier}   # the host wall cannot nest around this tier (host_wall.plan)

    def reap(self, live_run_ids):
        """Kill what each ended light run's shell left running under its worktree (`_kill_under`),
        the tick's counterpart of the strong tier's container reap; returns the pids killed. Every
        pass re-scans each worktree that still exists, since a child can outlive any one pass. A
        worktree a live light run still uses (by its spool marker) is left alone."""
        if not self.board:
            return []
        spools = paths.board_dir(self.board, paths.home(self.home)) / "spool"
        wts = {d.name: (d / "worktree").read_text() for d in spools.glob("*")
               if (d / "worktree").is_file()}
        in_use = {wt for run_id, wt in wts.items() if run_id in live_run_ids}
        killed = []
        for wt in sorted(set(wts.values()) - in_use):
            if os.path.isdir(wt):
                killed += _kill_under(wt) or []
        return killed
