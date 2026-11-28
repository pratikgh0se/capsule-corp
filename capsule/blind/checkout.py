"""The product checkout a blind test runs against, made the same way for the red check and for the
real blind run (blind spec 3.4; `red.py`, `run.py`). A test that reads its own git history (HEAD
against the brief's base sha, the last commit, the reflog, FETCH_HEAD, the age of a file) must not
be able to tell the red check from a real run, so `build` gives it a checkout with no real
history: the blobs of a tree, byte for byte (no export-subst, export-ignore, eol or filter
conversion, whatever `.gitattributes` the tree holds), one commit on one branch whose identity,
date and message are constants, one fixed mtime on every file and folder, and nothing that names
the source repo, its remote or the source commit. The accepted cost: a blind test cannot see real
history.

`worktree_tree` is the tree of a card worktree as it is now (the card's work is not committed when
the blind run starts). Git runs with a clean environment (no operator config, identity or hooks),
so nothing of the operator's leaks into the commit."""
import contextlib
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from capsule import workspace

BRANCH = "main"
NAME, EMAIL = "Capsule", "capsule@localhost"
MESSAGE = "Initial commit"
MTIME = 946684800                       # 2000-01-01T00:00:00Z: the commit's date and every mtime
TIMEOUT = 120
MAX_UNTRACKED = 5000                    # untracked, not ignored files `worktree_tree` will take
SCRATCH = ".worker-exec"                # as a worker container leaves it (worker-entrypoint.sh)
FILE, EXEC, LINK, GITLINK = b"100644", b"100755", b"120000", b"160000"


def _env(home):
    return {"PATH": os.environ.get("PATH", os.defpath), "HOME": home, "LC_ALL": "C",
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_TERMINAL_PROMPT": "0",
            "GIT_AUTHOR_NAME": NAME, "GIT_AUTHOR_EMAIL": EMAIL, "GIT_AUTHOR_DATE": f"{MTIME} +0000",
            "GIT_COMMITTER_NAME": NAME, "GIT_COMMITTER_EMAIL": EMAIL,
            "GIT_COMMITTER_DATE": f"{MTIME} +0000"}


def _run(env, where, *args, timeout=TIMEOUT, stdin=None, input=None):
    """stdout (bytes) of host git in `where` with the hardening pins and `env`; raises on a failure
    without naming `where` or the repo."""
    pins = [tok for key, value in workspace.HARDENING for tok in ("-c", f"{key}={value}")]
    done = subprocess.run(["git", *pins, "-c", "core.logAllRefUpdates=false", "-C", str(where),
                           *args], capture_output=True, env=env, timeout=timeout, stdin=stdin,
                          input=input)
    if done.returncode:
        raise RuntimeError(f"git {args[0]} failed: {done.stderr.decode(errors='replace').strip()}")
    return done.stdout


def _git(env, where, *args, **kw):
    return _run(env, where, *args, **kw).decode(errors="surrogateescape")


def _safe(path):
    """`path` (bytes, from a tree) as a relative path that cannot leave `dest` or name `.git`."""
    parts = path.split(b"/")
    if any(p in (b"", b".", b"..") or p.lower() == b".git" for p in parts):
        raise ValueError("the tree holds a path a checkout cannot have")
    return os.fsdecode(path)


def _quote(path):
    """`path` as a fast-import path: quoted when it starts with a quote or holds a newline."""
    if not (path.startswith(b'"') or b"\n" in path):
        return path
    return b'"' + path.replace(b"\\", b"\\\\").replace(b'"', b'\\"').replace(b"\n", b"\\n") + b'"'


def _entries(env, repo, tree, timeout):
    """[(mode, kind, sha, path)] of every file of `tree`, all bytes."""
    out = _run(env, repo, "ls-tree", "-r", "-z", "--full-tree", tree, timeout=timeout)
    rows = []
    for item in filter(None, out.split(b"\0")):
        head, path = item.split(b"\t", 1)
        mode, kind, sha = head.split(b" ")
        if kind == b"blob" and mode not in (EXEC, LINK):
            mode = FILE                                 # 100664 and the like: git's own 100644
        rows.append((mode, kind, sha, path))
    return rows


def _blobs(env, repo, shas, timeout):
    """(sha, data) of each blob of `shas`: `cat-file --batch` prints `<sha> blob <size>`, the
    bytes and a newline for each."""
    out = _run(env, repo, "cat-file", "--batch", input=b"".join(s + b"\n" for s in shas),
               timeout=timeout)
    at = 0
    for sha in shas:
        end = out.index(b"\n", at)
        head = out[at:end].split()
        if len(head) != 3 or head[1] != b"blob":
            raise RuntimeError("git cat-file failed: a blob of the tree is missing")
        at = end + 1 + int(head[2]) + 1
        yield sha, out[end + 1:at - 1]


def _mkdirs(dest, rel):
    """The folders of `rel` (a relative path) under `dest`, one component at a time: a component
    that is already there must be a real folder, never a link (on a case-insensitive disk `Foo ->
    /outside` and `foo/a/b` meet), so nothing is made outside `dest`."""
    here = dest
    for part in Path(rel).parts:
        here = here / part
        if not os.path.lexists(here):
            os.mkdir(here, 0o755)
        elif os.path.islink(here) or not os.path.isdir(here):
            raise ValueError("the tree holds a path that runs through a link or a file")


def _write_file(dest, mode, path, data):
    """One blob as the file, executable file or link `path` under `dest`."""
    target = dest / _safe(path)
    _mkdirs(dest, target.parent.relative_to(dest))
    if mode == LINK:
        os.symlink(os.fsdecode(data), target)
        return
    # never through a link already at the path (a case collision on a case-insensitive disk)
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)
        os.fchmod(handle.fileno(), 0o755 if mode == EXEC else 0o644)


def _materialise(env, repo, dest, rows, timeout):
    """Write the rows' files under `dest`; returns the fast-import stream of the same blobs and
    the commit's `M` lines (the blobs never touch a filter)."""
    paths = {}
    for mode, kind, sha, path in rows:
        if kind == b"commit":                       # a submodule: an empty folder, as git archive
            _mkdirs(dest, _safe(path))
        else:
            paths.setdefault(sha, []).append((mode, path))
    stream, marks = tempfile.TemporaryFile(), {}
    for n, (sha, data) in enumerate(_blobs(env, repo, list(paths), timeout), 1):
        stream.write(b"blob\nmark :%d\ndata %d\n%s\n" % (n, len(data), data))
        marks[sha] = n
        for mode, path in paths[sha]:
            _write_file(dest, mode, path, data)
    lines = [b"M %s %s %s\n" % (mode, b":%d" % marks[sha] if kind != b"commit" else sha,
                                _quote(path)) for mode, kind, sha, path in rows]
    stream.write(b"commit refs/heads/%s\ncommitter %s <%s> %d +0000\ndata %d\n%s\n%s\n" % (
        BRANCH.encode(), NAME.encode(), EMAIL.encode(), MTIME, len(MESSAGE) + 1, MESSAGE.encode(),
        b"".join(lines)))
    stream.seek(0)
    return stream


def _fix_times(dest):
    """`MTIME` on everything under `dest` (it has no `.git` yet), folders after their contents (and
    with the mode 755), and on `dest` itself last. A link is never followed: it gets the time
    itself (where the platform can) and no mode; what it points at is not touched."""
    for here, dirs, names in os.walk(dest, topdown=False):
        for name in names + dirs:
            path = os.path.join(here, name)
            link = os.path.islink(path)
            if not link and name in dirs:
                os.chmod(path, 0o755)
            if not link or os.utime in os.supports_follow_symlinks:
                os.utime(path, (MTIME, MTIME), follow_symlinks=False)
    os.utime(dest, (MTIME, MTIME))


def build(repo, treeish, dest, timeout=TIMEOUT):
    """The checkout of `treeish` (a commit or a tree) of git repo `repo` at the new folder `dest`;
    returns `dest`. `dest` holds the tree's blobs byte for byte with their executable bits. Raises,
    leaving no `dest` of its own making, when `dest` exists, `treeish` does not resolve or git
    fails; a `dest` that existed is never touched, and folders `build` made above `dest` go too."""
    repo, dest = Path(repo), Path(dest)
    made, above = [], dest.parent
    while not above.exists():
        made.append(above)
        above = above.parent
    own = False
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.mkdir(mode=0o755)              # raises FileExistsError for a dest that is there
        own = True
        _populate(repo, treeish, dest, timeout)
    except BaseException:
        if own:
            shutil.rmtree(dest, ignore_errors=True)
        for folder in made:                 # innermost first; one that holds anything stays
            with contextlib.suppress(OSError):
                folder.rmdir()
        raise
    return dest


def _populate(repo, treeish, dest, timeout):
    """The files of `treeish` in the new folder `dest`, then its git repo of one commit."""
    with tempfile.TemporaryDirectory(prefix="checkout-") as tmp:
        env = _env(tmp)
        tree = _git(env, repo, "rev-parse", "--verify", "--end-of-options",
                    f"{treeish}^{{tree}}", timeout=timeout).strip()
        rows = _entries(env, repo, tree, timeout)
        with _materialise(env, repo, dest, rows, timeout) as stream:
            _fix_times(dest)
            (Path(tmp) / "template").mkdir()
            _git(env, dest, "init", "-q", "-b", BRANCH, f"--template={tmp}/template",
                 timeout=timeout)
            _run(env, dest, "fast-import", "--quiet", stdin=stream, timeout=timeout)
        _git(env, dest, "read-tree", "HEAD", timeout=timeout)
        _git(env, dest, "update-index", "-q", "--refresh", timeout=timeout)
    os.utime(dest, (MTIME, MTIME))


class TooManyFiles(ValueError):
    """The worktree holds more untracked, not ignored files than `MAX_UNTRACKED`."""


def _names(env, ws, timeout, *args):
    return [p for p in _run(env, ws, "ls-files", "-z", *args, timeout=timeout).split(b"\0") if p]


def _check_untracked(env, ws, timeout):
    """Raises `TooManyFiles` when the worktree holds more untracked files that are not ignored
    than `MAX_UNTRACKED` (not counting `.worker-exec`)."""
    found = [p for p in _names(env, ws, timeout, "--others", "--exclude-standard")
             if p.split(b"/")[0] != SCRATCH.encode() and not p.endswith(b"/")]
    if len(found) > MAX_UNTRACKED:
        raise TooManyFiles(f"the card's worktree has {len(found)} untracked files that are not"
                           f" ignored (over {MAX_UNTRACKED}): ignore or remove them")


def worktree_tree(ws, timeout=TIMEOUT):
    """The sha of a tree of the files the card worktree `ws` holds now: tracked and untracked
    files that are not ignored, uncommitted edits and deletions in, `.worker-exec` out, as
    `git add -A` takes them (it refuses a path beyond a symbolic link and keeps its stat cache, so
    the tracked files that did not change are not read again). The tree is made in a temporary
    index in a folder of its own, a copy of the worktree's: the worktree's index, HEAD and files
    do not change (blobs are added to its object store). The git dir is `workspace.gitdir(ws)`,
    never whatever `.git` the worktree holds now. Raises `TooManyFiles` over `MAX_UNTRACKED`
    untracked files."""
    ws, gd = Path(ws), workspace.gitdir(ws)
    with tempfile.TemporaryDirectory(prefix="wtree-") as tmp:
        env = {**_env(tmp), "GIT_DIR": str(gd), "GIT_WORK_TREE": str(ws),
               "GIT_INDEX_FILE": f"{tmp}/index"}
        if (gd / "index").is_file():
            shutil.copy2(gd / "index", f"{tmp}/index")      # keeps its mtime: git's racy check
        else:
            unborn = subprocess.run(["git", "rev-parse", "-q", "--verify", "HEAD^{commit}"],
                                    env=env, cwd=ws, capture_output=True).returncode != 0
            _run(env, ws, "read-tree", *(["--empty"] if unborn else ["HEAD"]), timeout=timeout)
        _check_untracked(env, ws, timeout)
        _run(env, ws, "add", "-A", "--", ".", f":(exclude){SCRATCH}", timeout=timeout)
        return _git(env, ws, "write-tree", timeout=timeout).strip()


def layout(dest):
    """`dest` (from `build`) in the tick's clone layout, the one a container mounts: its git dir
    moved to `.gitdirs/<name>` beside it with the `.git` pointer file a clone has, and the
    `.worker-exec` scratch folder a worker leaves. Everything of it keeps the one fixed mtime.
    Returns `dest`."""
    gd = workspace.gitdir(dest)
    gd.parent.mkdir(parents=True, exist_ok=True)
    os.rename(dest / ".git", gd)
    workspace._repoint(dest, gd)
    scratch = dest / SCRATCH
    scratch.mkdir()
    scratch.chmod(0o1777)
    (scratch / ".gitignore").write_text("*\n")
    for path in (scratch / ".gitignore", scratch, dest / ".git", dest):
        os.utime(path, (MTIME, MTIME), follow_symlinks=False)
    return dest
