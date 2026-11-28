#!/usr/bin/env python3
"""Automatic disk relief (decision D204): fixed rules, no AI. `run.sh disk-relief` runs it every
4 hours; it does nothing while free space on the volume holding $HOME is at or above
OPS_DISK_RELIEF_FREE_GB (default 60). Under it, the sections below run in order, free space is
re-checked after each, and the run stops once it is back above the threshold. Only things that
rebuild themselves or are provably finished go. `--dry-run` prints what would go and changes
nothing (set OPS_DISK_RELIEF_FREE_GB high to see every section on a healthy disk).
  a. storage_gc.py --apply (Capsule's own containers, volumes, images, temp dirs: its rules).
  b. Download caches through each tool's own clean command (npm, brew, go, pip), each skipped
     when the tool is missing; 120 s each, a timeout is "skipped: busy or in use", not a failure.
     The contents of Xcode DerivedData when that folder exists. (No Docker build cache, which
     is machine-wide and cannot be limited to Capsule; no uv cache, its lock is held by MCP
     servers.)
  c. Claude scratch: only top-level x.* and tmp.* folders of /private/tmp/claude-501, real
     directories (never links), our uid, nothing inside modified (or changed) within 24h.
  d. Build worktrees of this repo: a sibling capsule-corp-* folder, never the main checkout,
     capsule-corp-tests or capsule-corp-keeper-*; clean `git status` and no ignored file outside
     the allowlist (cache folders, `logs/` folders, `*.log` files; D206), HEAD on origin/main
     after a fetch (an ancestor, a named branch whose commits are all patch-equivalent to
     main's, the same tree, or a named branch whose changed files all match main: a squash
     landing), unlocked, untouched for 2 days:
     `git worktree remove` (never forced, branches are never deleted).
     A failed fetch skips the section and counts as a failure.
Every removal is one JSON line in <events.state_root()>/disk_relief.jsonl (what, id, bytes,
result). The whole job has a deadline (OPS_DISK_RELIEF_DEADLINE_S, default 1500): after it the
remaining sections are skipped. Each section is wrapped so one error never skips the floor check.
Exit 0 fine, 1 a section failed, 3 still under the hard floor (OPS_DISK_RELIEF_FLOOR_GB,
default 25) after everything: run.sh sends the one alert for that.
"""
import fnmatch
import json
import os
import shutil
import stat
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import events   # noqa: E402  docker_run() and state_root(), the doors every maintenance job uses

GB, DAY = 10**9, 86400
STORAGE_GC = Path(__file__).with_name("storage_gc.py")
SCRATCH_ROOT ="/private/tmp/claude-501"
SCRATCH_PATTERNS = ("x.*", "tmp.*")
CACHES = (("npm", "cache", "clean", "--force"),
          ("brew", "cleanup", "--prune=all", "-s"), ("go", "clean", "-modcache"),
          ("go", "clean", "-cache"), ("pip", "cache", "purge"))
KEEPER = ("capsule-corp-tests", "capsule-corp-keeper-")     # name, prefix: never removed
TOOL_TIMEOUT = 120      # seconds for every tool command and git call; the deadline caps the job
IGNORED_OK = ("__pycache__", ".ruff_cache", ".pytest_cache", "node_modules", ".mypy_cache")
MAX_CHERRY = 50


def free_bytes():
    return shutil.disk_usage(Path.home()).free


def _now():
    return time.time()


def gb(n):
    return f"{n / GB:.1f} GB"


def scan(path, seconds):
    """(True, 0) as soon as anything inside was modified within `seconds`, else (False, bytes)."""
    now, root = _now(), os.lstat(path)
    size = root.st_blocks * 512
    if now - max(root.st_mtime, root.st_ctime) < seconds:
        return True, 0
    for here, dirs, files in os.walk(path):
        for n in dirs + files:
            s = os.lstat(os.path.join(here, n))
            if now - max(s.st_mtime, s.st_ctime) < seconds:
                return True, 0
            size += s.st_blocks * 512
    return False, size


def run(cmd, timeout=TOOL_TIMEOUT, **kw):
    return subprocess.run(list(cmd), capture_output=True, text=True, timeout=timeout, **kw)


class _Relief:
    def __init__(self, apply):
        self.apply, self.bad = apply, []
        self.path = events.state_root() / "disk_relief.jsonl"
        self.deadline = time.monotonic() + float(
            os.environ.get("OPS_DISK_RELIEF_DEADLINE_S") or 1500)

    def late(self):
        return time.monotonic() >= self.deadline

    def left(self):
        return max(1.0, self.deadline - time.monotonic())

    def log(self, what, ident, nbytes, result):
        print(f"disk-relief: {what} {ident} ({nbytes} bytes) -> {result}")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a") as f:
                f.write(json.dumps({"at": datetime.now(timezone.utc).isoformat(), "what": what,
                                    "id": str(ident), "bytes": nbytes, "result": result}) + "\n")
        except OSError as e:        # a full disk must not stop the floor check
            print(f"disk-relief: log write failed: {e.__class__.__name__}")

    def would(self, what, ident, nbytes=0):
        print(f"disk-relief: would remove {what} {ident} ({nbytes} bytes)")

    def _done(self, what, ident, before, ok):
        """The log line for one command that ran; bytes = the free space it gave back."""
        self.log(what, ident, max(0, free_bytes() - before), "ran" if ok else "FAILED")
        if not ok:
            self.bad.append(f"{what}: {ident}")

    def _tool(self, cmd):
        """Run one clean command (apply) or name it (dry run)."""
        if not self.apply:
            return self.would("cache", " ".join(cmd))
        before = free_bytes()
        try:
            ok = run(cmd, timeout=min(TOOL_TIMEOUT, self.left())).returncode == 0
        except subprocess.TimeoutExpired:
            return self.log("cache", " ".join(cmd), 0, "skipped: busy or in use")
        except (OSError, subprocess.SubprocessError):
            ok = False
        self._done("cache", " ".join(cmd), before, ok)

    def _storage_gc(self):
        r = run([sys.executable, str(STORAGE_GC)] + (["--apply"] if self.apply else []),
                timeout=min(900, self.left()))
        lines = (r.stdout + r.stderr).splitlines()
        print("".join(f"storage-gc: {line}\n" for line in lines), end="")
        if self.apply:      # storage_gc logs each of its own items; this is the section's line
            self.log("storage_gc", "--apply", 0, "ran" if r.returncode == 0 else "FAILED")
        if r.returncode:
            self.bad.append("storage_gc failed")

    def _caches(self):
        for cmd in CACHES:
            if self.late():
                print(f"disk-relief: {cmd[0]} cache skipped: deadline passed")
            elif shutil.which(cmd[0]):
                self._tool(cmd)
            else:
                print(f"disk-relief: {cmd[0]} cache skipped: {cmd[0]} not installed")
        if not Path.home().is_absolute():
            return print("disk-relief: DerivedData skipped: HOME is not an absolute path")
        derived = Path.home() / "Library/Developer/Xcode/DerivedData"
        if derived.is_dir() and not derived.is_symlink():
            for e in sorted(derived.iterdir()):
                if self.late():
                    return print("disk-relief: DerivedData skipped: deadline passed")
                self.remove_tree("xcode-deriveddata", e, 0, check=_no_reason)

    def remove_tree(self, what, path, seconds, check):
        """One real directory (a file is unlinked; a link is left alone) through shutil.rmtree."""
        try:
            self._remove_checked(what, str(path), seconds, check)
        except OSError as e:
            self.log(what, path, 0, f"FAILED: {e.__class__.__name__}")
            self.bad.append(f"{what}: {path}")

    def _remove_checked(self, what, path, seconds, check):
        st = os.lstat(path)
        if stat.S_ISLNK(st.st_mode):
            return print(f"disk-relief: kept {what} {path} (a link)")
        why = check(path)
        isdir = stat.S_ISDIR(st.st_mode)
        recent, size = scan(path, seconds) if isdir else (False, st.st_blocks * 512)
        if why or recent:
            return print(f"disk-relief: kept {what} {path} ({why or 'modified recently'})")
        if not self.apply:
            return self.would(what, path, size)
        shutil.rmtree(path) if isdir else os.unlink(path)
        self.log(what, path, size, "FAILED: still there" if os.path.lexists(path) else "removed")

    def _scratch(self):
        root = Path(SCRATCH_ROOT)
        if not Path.home().is_absolute():
            return print("disk-relief: scratch skipped: HOME is not an absolute path")
        if root.is_symlink() or not root.is_dir() or root.stat().st_uid != os.getuid():
            return print(f"disk-relief: scratch skipped: {root} is not a real directory of ours")
        for e in sorted(root.iterdir()):
            if self.late():
                return print("disk-relief: scratch skipped: deadline passed")
            if any(fnmatch.fnmatchcase(e.name, p) for p in SCRATCH_PATTERNS):
                self.remove_tree("claude-scratch", e, DAY, check=_scratch_why)

    def _fetch(self, here):
        """True when origin/main is fresh; never prompts, never garbage-collects."""
        cmd = ["git", "-C", here, "-c", "core.sshCommand=ssh -o BatchMode=yes", "fetch",
               "--no-auto-gc", "--no-auto-maintenance", "origin"]
        try:
            ok = run(cmd, timeout=min(TOOL_TIMEOUT, self.left()),
                     env={**os.environ, "GIT_TERMINAL_PROMPT": "0"}).returncode == 0
        except (OSError, subprocess.SubprocessError):
            ok = False
        if not ok:
            self.bad.append("worktrees: fetch of origin failed")
            print("disk-relief: worktrees skipped: fetch of origin failed, main is unproven")
        return ok

    def _worktrees(self):
        here = str(Path(os.environ.get("DISK_RELIEF_REPO") or Path(__file__).resolve().parents[1]))
        r = run(["git", "-C", here, "worktree", "list", "--porcelain", "-z"])
        blocks = [dict(f.partition(" ")[::2] for f in b.split("\0") if f)
                  for b in r.stdout.split("\0\0") if b.strip("\0")]
        if r.returncode != 0 or not blocks or not blocks[0].get("worktree"):
            return print(f"disk-relief: worktrees skipped: {here} is not a git checkout")
        # a dry run never fetches (it would move origin/main): it judges by the last fetch
        if self.apply and not self._fetch(here):
            return
        repo = Path(blocks[0]["worktree"]).resolve()     # the first one listed is the main checkout
        for fields in blocks[1:]:
            if not fields.get("worktree"):
                print("disk-relief: worktree entry skipped: malformed block")
                continue
            if self.late():
                return print("disk-relief: worktrees skipped: deadline passed")
            try:
                self._worktree(repo, fields["worktree"], "locked" in fields)
            except (OSError, subprocess.SubprocessError, ValueError) as e:     # this one only
                print(f"disk-relief: worktree {fields['worktree']} skipped: {e.__class__.__name__}")

    def _worktree(self, repo, path, locked):
        keep, via = _worktree_why(repo, path, locked)
        recent, size = scan(path, 2 * DAY) if not keep else (False, 0)
        if keep or recent:
            return print(f"disk-relief: kept worktree {path} ({keep or 'modified within 2 days'})")
        if not self.apply:
            return self.would("worktree", f"{path} [{via}]", size)
        keep, via = _worktree_why(repo, path, locked)   # asked again right before: it may change
        if keep:
            return print(f"disk-relief: kept worktree {path} ({keep})")
        r = run(["git", "-C", str(repo), "worktree", "remove", path])
        gone = r.returncode == 0 and not os.path.lexists(path)
        self.log("worktree", path, size,
                 f"removed: {via}" if gone else "FAILED: " + r.stderr.strip()[:99])
        if not gone:
            self.bad.append(f"worktree: {path}")


def _no_reason(_path):
    return None


def _scratch_why(path):
    st = os.lstat(path)
    return ("not a real directory" if not stat.S_ISDIR(st.st_mode) else
            "other owner" if st.st_uid != os.getuid() else None)


def _ignored_ok(path):
    """True for a disposable ignored path: a cache folder, a `logs` folder or a `*.log` file."""
    parts = path.split("/")
    if parts[-1] == "":                     # git prints an ignored folder with a trailing slash
        parts.pop()
    else:
        if parts[-1].endswith(".log"):
            return True
        parts.pop()                         # a plain file: only its folders can allow it
    return any(part in IGNORED_OK or part == "logs" for part in parts)


def _status_why(git):
    """Why `git status` says the worktree holds work, or None: clean apart from cache and logs."""
    status = run(git + ["status", "--porcelain", "--ignored=matching", "-z"])
    if status.returncode != 0:
        return "uncommitted work or unreadable status"
    for entry in filter(None, status.stdout.split("\0")):
        if not entry.startswith("!! "):
            return "uncommitted work or unreadable status"
        if not _ignored_ok(entry[3:]):
            return f"ignored file outside the cache allowlist: {entry[3:]}"
    return None


def _on_main(git):
    """How HEAD is on origin/main (ancestor, same tree, patch-equivalent, content), or None."""
    if run(git + ["merge-base", "--is-ancestor", "HEAD", "origin/main"]).returncode == 0:
        return "ancestor"
    heads = [run(git + ["rev-parse", f"{ref}^{{tree}}"]) for ref in ("HEAD", "origin/main")]
    if all(h.returncode == 0 for h in heads) and heads[0].stdout.strip() == heads[1].stdout.strip():
        return "same tree"
    ref = run(git + ["symbolic-ref", "-q", "HEAD"])
    if ref.returncode != 0 or not ref.stdout.startswith("refs/heads/"):
        return None             # a detached HEAD needs strict ancestry
    base = run(git + ["merge-base", "HEAD", "origin/main"])
    if base.returncode != 0 or not base.stdout.strip():
        return None
    cherry = run(git + ["cherry", "origin/main", "HEAD"]).stdout.splitlines()
    if cherry and len(cherry) <= MAX_CHERRY and all(line.startswith("-") for line in cherry):
        return "patch-equivalent"
    return "content on main" if _content_on_main(git, base.stdout.strip()) else None


def _content_on_main(git, base):
    """True when every path the branch changed since `base` is identical on origin/main (a deleted
    path must be absent there too): the branch landed as a squash. Any git error means no."""
    names = run(git + ["diff", "--name-only", "-z", "--no-renames", base, "HEAD"])
    paths = [n for n in names.stdout.split("\0") if n]
    if names.returncode != 0 or not paths:
        return False
    for i in range(0, len(paths), 200):
        same = run(git[:1] + ["--literal-pathspecs"] + git[1:]
                   + ["diff-tree", "-r", "--quiet", "--no-renames", "origin/main", "HEAD", "--"]
                   + paths[i:i + 200])
        if same.returncode != 0:
            return False
    return True


def _worktree_why(repo, path, locked):
    """(why a worktree stays, how it is on main): one is None; every rule from decision D204."""
    real = Path(path).resolve()
    git = ["git", "-C", str(real)]
    if real == repo or not real.name.startswith("capsule-corp-") or real.parent != repo.parent:
        return "not a sibling capsule-corp-* folder", None
    if real.name == KEEPER[0] or real.name.startswith(KEEPER[1]):
        return "protected folder", None
    if locked:
        return "locked", None
    why = _status_why(git)
    if why:
        return why, None
    via = _on_main(git)
    return (None, via) if via else ("HEAD is not on origin/main", None)


def main(argv=None):
    args = argv if argv is not None else sys.argv[1:]
    relief = _Relief(apply="--dry-run" not in args)
    threshold = float(os.environ.get("OPS_DISK_RELIEF_FREE_GB") or 60) * GB
    floor = float(os.environ.get("OPS_DISK_RELIEF_FLOOR_GB") or 25) * GB
    before = free_bytes()
    if before >= threshold:
        print(f"disk-relief: enough free space ({gb(before)} free, threshold {gb(threshold)})")
        return 0
    print(f"disk-relief: {gb(before)} free, under {gb(threshold)}: " +
          ("relieving" if relief.apply else "dry run, nothing changes"))
    for section in (relief._storage_gc, relief._caches, relief._scratch, relief._worktrees):
        if relief.late():
            relief.log("deadline", section.__name__, 0, "skipped: deadline passed")
            break
        try:
            section()
            if relief.apply and free_bytes() >= threshold:
                print(f"disk-relief: back above the threshold after {section.__name__}, stopping")
                break
        except Exception as e:      # one section never skips the floor check below
            relief.bad.append(f"{section.__name__}: {e.__class__.__name__}: {e}")
    after = free_bytes()
    for reason in relief.bad:
        print(f"disk-relief: FAILED {reason}", file=sys.stderr)
    print(f"disk-relief: free before {gb(before)}, after {gb(after)}" +
          ("" if relief.apply else " (dry run)"))
    if relief.apply and after < floor:
        print(f"disk-relief: ALERT only {gb(after)} free, under the {gb(floor)} hard floor")
        return 3
    return 1 if relief.bad else 0


if __name__ == "__main__":
    sys.exit(main())
