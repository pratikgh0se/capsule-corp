"""One git clone per epic, reused across runs. Never a fresh clone per run.

Hardening: `branch`/`base` are board/epic data
a model can influence, so they are validated before ever reaching `git` (no leading `-`
so they can't be parsed as an option; no `..`; no glob/control characters), git calls use
`--end-of-options` (or `--` for clone) so a validated-but-adversarial value still can't be
read as a flag, transports are restricted to the ones we actually use, and the reused
worktree path is asserted to stay under `root` (defense in depth on top of validation).

Host escape through `.git` is closed: the clone's git dir lives outside the
workspace, at `root/.gitdirs/<slug>` (`git clone --separate-git-dir`), so neither the
container (which mounts only the workspace) nor the host `claude` process (confined to
its cwd) can write the config or hooks the host's git reads. Every host git call names
that git dir explicitly (GIT_DIR/GIT_WORK_TREE), so a `.git` a run plants in the
workspace is never consulted, and it also runs with hooks, fsmonitor, external diff and
textconv off, system/global config ignored and credential helpers cleared.
`hardening_env` carries the same hooks/fsmonitor pins for other host processes that may
run git in a workspace (the `claude` CLI). The strong containers mount the git dir READ-ONLY
at /.gitdirs/<name> (the clone's relative `.git` pointer reaches it), so
read-only git works inside them; commits happen on the host only. A reuse leaves the files a
container reads untouched (Docker Desktop's file share can serve stale entries for a file the
host deleted and recreated): `_repoint` keeps a `.git` that already holds the right pointer, and
`clone` skips `checkout` when HEAD is already on the branch (a fetch only moves refs). Never at
the host path: a Docker Desktop bind mount whose target equals its host path comes up empty
(`run_args` refuses that pattern). A private https repo needs its credentials in the URL or an
ssh remote.
"""
import fnmatch
import hashlib
import os
import re
import shutil
import stat
import subprocess
import sys
import uuid
from pathlib import Path

# docs/ZENKAI.md: the Zenkai Rules mirror; workers may not write it (Z6).
# Judge paths (redesign spec 6, worker-images spec 1, blind spec 4.4 item 7): the images a worker
# runs in, the blind harness, the meters, the reviewer planted-bug set and the interrupt table are
# the judges' own ground; some do not exist yet - the glob protects them in advance.
PROTECTED = ("contracts/**", "docs/ZENKAI.md", "**/SOUL*.md", "**/CLAUDE.md", "**/AGENTS.md",
             "capsule/intent.py", "capsule/workspace.py", "capsule/lander.py",
             "capsule/test_shard*", "capsule/shards.*",     # the sharded suite's runner and split
             "capsule/unblocked_stop.py", "capsule/engine_heal.py", "capsule/safe_write.py",
             "docker/base/**", "docker/stacks/**", "docker/IMAGES.lock",
             "docker/build-base.sh", "docker/BUILD_MIN_FREE_BYTES", "capsule/image_render.py",
             "capsule.toml",   # M137: the image tool list; only the image-request stamp edits it
             "capsule/blind/harness/**",
             "maintenance/meters.py", "calibration/**", "capsule/interrupts.json",
             # R31: the blind red check and the lint decide what lands as a blind test.
             "capsule/blind/red.py", "capsule/blind/lint.py", "capsule/blind/settle.py",
             # R35: the lander's blind run decides whether a card lands (wildcards as for sandbox).
             "capsule/lander_blind.*", "capsule/lander_blind/**",
             # The keeper's gate runs the trusted checkout's lander, so what that lander imports
             # to judge a blind run is protected too: the blind run's oracles (run.py), the gate
             # that holds a card (gate.py), the mount and image preflight (preflight.py) and the
             # audit hit's own test (incident_blind.py). A batch may not rewrite its own judge.
             "capsule/blind/run.*", "capsule/blind/run/**", "capsule/blind/gate.*",
             "capsule/blind/gate/**", "capsule/blind/preflight.*", "capsule/blind/preflight/**",
             "capsule/incident_blind.*", "capsule/incident_blind/**",
             # The lander's own gate scripts and what they import, the size baseline,
             # deploy.sh and the unblocked hook run as the owner; a worker may not rewrite them.
             "tests/mutation_check.py", "tests/size_check.py", "tests/simplicity_check.py",
             "tests/ruff_gate.py", "tests/portability_lint.py",
             "tests/size_baseline.py", "tests/size_baseline.json",
             "tests/discover_tmp.py", "tests/mutation_newsymbol.py",
             "tests/mutation_contractdata.py", "tests/mutation_info.py",
             "tests/mutation_alias.py", "tests/mutation_literals.py", "tests/mutation_env.py",
             "deploy.sh", "bin/capsule-unblocked-hook.sh", "bin/blind-wall.py",
             # The sandbox runner decides wait vs red when the container is down, and
             # the Slack listener turns a Slack reply into an action; a worker may not
             # rewrite either. The wildcards also cover a package dir or an extension module
             # that would shadow sandbox.py and the listener's trust data (an allowlist file).
             "capsule/runners/sandbox.*", "capsule/runners/sandbox/**", "capsule/slack_listen*",
             # By function ("the runner that decides wait vs fail"): docker.py's
             # available/alive/exec rc and run flags decide the same outcome, and a sys.modules
             # shim in either package __init__ would shadow the runner. Wildcards as for sandbox:
             # a package dir or an extension module would shadow the module.
             "capsule/runners/docker.*", "capsule/runners/docker/**",
             "capsule/runners/__init__.*", "capsule/__init__.*",
             # docker.py reads OPS_WORKER_IMAGE from the ops.env in this dir, so what decides
             # the dir is protected too (host.py, often edited, only delegates to it).
             "capsule/jobs_dir.*", "capsule/jobs_dir/**",
             # Same reason: StrongRunner._d -> docker_door.docker_run -> events.docker_run runs
             # every docker call the runner makes ("by function"); events.py
             # imports scrub at load, and scrub's top-level code could rebind subprocess.run,
             # which events.docker_run looks up per call, so scrub decides every docker rc.
             "capsule/docker_door.*", "capsule/docker_door/**",
             "maintenance/events.*", "maintenance/events/**",
             "maintenance/scrub.*", "maintenance/scrub/**",
             # events imports docker_engine at load; its table picks the docker CLI dir.
             "maintenance/docker_engine.*", "maintenance/docker_engine/**",
             # The host wall that keeps a session off the blind tests; a worker that
             # could edit it (or shadow it with a package dir) could turn the wall off for itself.
             "capsule/runners/host_wall.*", "capsule/runners/host_wall/**",
             # The Mac lane runner renders and starts a mac card's sandboxes.
             "capsule/runners/mac.*", "capsule/runners/mac/**",
             # The wall is applied by worker.run_one (plan) and ClaudeAdapter.run (apply): a worker
             # that could edit either could drop the wall for itself. Wildcards as for docker.
             "capsule/worker.*", "capsule/worker/**",
             "capsule/models/claude.*", "capsule/models/claude/**",
             # `adapter_for` picks the adapter that applies the wall.
             "capsule/models/__init__.*",
             # The Slack answer path, the chief-of-staff ask channel and the human verb run as
             # The owner's own voice (D225-D227); a worker may not rewrite them. Wildcards as for
             # sandbox: a package dir or an extension module would shadow the module.
             "capsule/slack_answer.*", "capsule/slack_answer/**",
             "capsule/cos_ask.*", "capsule/cos_ask/**", "capsule/human.*", "capsule/human/**",
             "capsule/human_preflight.*", "capsule/human_preflight/**",
             # human imports it to scope what a Slack answer may touch.
             "capsule/answer_scope.*", "capsule/answer_scope/**",
             # The listener's whole import closure besides the package: token, redirect, masking.
             "capsule/slack_secret.*", "capsule/slack_secret/**",
             # The answer path's own base and the schema check `human` runs: stdlib only, so the
             # decision imports nothing a worker could rewrite.
             "capsule/answer_base.*", "capsule/answer_base/**",
             "capsule/row_schemas.*", "capsule/row_schemas/**",
             "capsule/jsonschema.*", "capsule/jsonschema/**",
             # The Slack brainstorm (D229): intake routes the owner's words, the runner talks to
             # a model with no tools, and its fixed prompt says what it may not do.
             # brainstorm_store holds the thread lock and status rules both of them rely on.
             "capsule/brainstorm_intake.*", "capsule/brainstorm_intake/**",
             "capsule/brainstorm_run.*", "capsule/brainstorm_run/**",
             "capsule/brainstorm_store.*", "capsule/brainstorm_store/**",
             "capsule/prompts/brainstorm*")
# intent.py governs the second-oracle gate (Z2) - a worker
# must not be able to edit its own oracle any more than it can edit contracts/**.

# Local clone/fetch can legitimately take a while (network); everything else is local and fast.
GIT_FAST_TIMEOUT = 60
GIT_SLOW_TIMEOUT = 600

_UNSAFE_CHARS = set(" \t\n\r~^:?*[\\")
_ALLOWED_TRANSPORTS = "file:git:http:https:ssh"

# `_redact_clone_failure` reads the return code back out of `_git`'s own message
# shape (`f"...failed (rc={r.returncode}): ..."`, set in `_git` above) so it can report it without
# ever re-emitting the stderr that message also carries.
_RC_RE = re.compile(r"\(rc=(\d+)\)")

GITDIRS = ".gitdirs"   # under root, beside (never inside) each clone; `_slug` never yields it

# The worker image's scratch dir (TMPDIR, set in docker/base/Dockerfile) lives inside the
# workspace; its
# own `*` .gitignore is container-writable, so every host `add` also excludes it by pathspec
# so scratch never reaches a reviewer diff or a commit.
SCRATCH_EXCLUDE = ("--", ".", ":(exclude,top).worker-exec")
IDENTITY = ("-c", "user.name=Capsule", "-c", "user.email=capsule@localhost", "-c", "commit.gpgSign=false",
            "-c", "user.useConfigOnly=true")

# Config pinned on the command line for every host git call; command-line config beats the
# repository's own config file.
HARDENING = (
    ("core.hooksPath", "/dev/null"),
    ("core.fsmonitor", "false"),
    ("core.sshCommand", "ssh"),
    ("credential.helper", ""),
    ("protocol.ext.allow", "never"),
)


def gitdir(ws):
    """The git dir of the clone at `ws`: outside `ws`, so outside every mount of it."""
    ws = Path(ws)
    return ws.parent / GITDIRS / ws.name


def hardening_env(env):
    """`env` plus HARDENING as GIT_CONFIG_COUNT/KEY/VALUE (command-line precedence), for a
    host process that is not `_git` but may run git in a workspace."""
    env = dict(env)
    try:
        n = int(env.get("GIT_CONFIG_COUNT") or 0)
    except ValueError:
        n = 0
    for i, (key, value) in enumerate(HARDENING, start=n):
        env[f"GIT_CONFIG_KEY_{i}"], env[f"GIT_CONFIG_VALUE_{i}"] = key, value
    env["GIT_CONFIG_COUNT"] = str(n + len(HARDENING))
    return env


def _validate_ref(name, what="ref"):
    """Reject anything that could be parsed as a git option or path-escape, before it is used."""
    if not name or not isinstance(name, str):
        raise ValueError(f"empty {what}")
    if name.startswith("-"):
        raise ValueError(f"unsafe {what} (leading '-' reads as an option): {name!r}")
    if ".." in name:
        raise ValueError(f"unsafe {what} (contains '..'): {name!r}")
    if any(c in _UNSAFE_CHARS for c in name):
        raise ValueError(f"unsafe {what} (control/glob character): {name!r}")
    if name.startswith("/") or name.endswith("/") or name.endswith(".lock"):
        raise ValueError(f"unsafe {what}: {name!r}")
    return name


def _validate_repo_url(url):
    if not url or not isinstance(url, str):
        raise ValueError("empty repo url")
    if url.startswith("-"):
        raise ValueError(f"unsafe repo url (leading '-' reads as an option, sha256:{repo_url_sha256(url)})")
    if url.lower().startswith(("ext::", "fd::")):
        raise ValueError(f"disallowed git transport (sha256:{repo_url_sha256(url)})")
    return url


def _slug(branch):
    """A path-safe stand-in for `branch`. `branch` must already have passed `_validate_ref`,
    so it can't contain '..' or a leading '-'; this only narrows the character set further."""
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", branch).strip("-.")
    if not slug or slug in (".", "..") or "/" in slug or "\\" in slug:
        slug = "branch"
    return slug


def _git(*args, ws=None, timeout=GIT_FAST_TIMEOUT):
    """Run host git with HARDENING. With `ws`, the git dir is named explicitly (`gitdir(ws)`),
    never discovered from whatever `.git` the workspace holds now."""
    env = dict(os.environ)
    for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR", "GIT_OBJECT_DIRECTORY",
                "GIT_CONFIG", "GIT_CONFIG_PARAMETERS", "GIT_CONFIG_COUNT",
                # identity comes only from `commit`'s -c pins, never the operator's environment
                "GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_AUTHOR_DATE",
                "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL", "GIT_COMMITTER_DATE", "EMAIL"):
        env.pop(key, None)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env.setdefault("GIT_ALLOW_PROTOCOL", _ALLOWED_TRANSPORTS)
    if ws is not None:
        env["GIT_DIR"], env["GIT_WORK_TREE"] = str(gitdir(ws)), str(ws)
    pins = [tok for key, value in HARDENING for tok in ("-c", f"{key}={value}")]
    r = subprocess.run(["git", *pins, *args], cwd=str(ws) if ws is not None else None,
                       capture_output=True, text=True, timeout=timeout, env=env)
    if r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed (rc={r.returncode}): {(r.stderr or '').strip()}")
    return r


def _holds(dot, want):
    """True when `dot` is a regular file (never a link, FIFO or directory) whose content is exactly
    `want`. The container owns `/workspace/.git`, so it is only opened O_NOFOLLOW|O_NONBLOCK, and
    only after an `lstat` showed a regular file; anything unreadable counts as not holding it."""
    try:
        fd = os.open(dot, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as f:
            regular = stat.S_ISREG(os.fstat(f.fileno()).st_mode)
            return regular and f.read(len(want) + 1) == want.encode()
    except OSError:
        return False


def _repoint(ws, gd):
    """Make `ws/.git` the plain pointer file to `gd` again, whatever a run left there. The
    container owns `/workspace/.git`: `lstat` it, remove it (a real directory is first renamed
    out of every mount, beside the git dirs, so nothing can swap its entries while it is
    deleted), then create the file afresh with O_EXCL|O_NOFOLLOW. A FIFO or link can make this
    raise, never block. A `.git` that is already a regular file holding exactly the wanted
    pointer (`_holds`) is left untouched: Docker Desktop's file share can serve a container
    stale entries for a file the host deleted and recreated (2026-10-31: `error opening
    '/workspace/.git'` in a live container), so a stable file keeps its inode."""
    dot = ws / ".git"
    want = f"gitdir: {os.path.relpath(gd, ws)}\n"
    try:
        mode = os.lstat(dot).st_mode
    except FileNotFoundError:
        mode = None
    if mode is not None and stat.S_ISREG(mode) and _holds(dot, want):
        return
    if mode is not None and stat.S_ISDIR(mode):
        trash = gd.parent / f".trash-{uuid.uuid4().hex}"
        os.rename(dot, trash)
        shutil.rmtree(trash)
    elif mode is not None:
        os.unlink(dot)
    fd = os.open(dot, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
    with os.fdopen(fd, "w") as f:
        # Relative (`../.gitdirs/<name>`): git resolves it from the `.git` file's directory, on the
        # host and in a container with the clone at /workspace and the git dir at the matching
        # path (docker.container_git_dir). A git dir mounted at its own host path comes up empty
        # on Docker Desktop 4.43.1.
        f.write(want)


def _redact_git_failure(step, exc, repo_url):
    """`exc` (from the `git {step}` call in `clone()`) with `repo_url`, argv and stderr all
    dropped: any of those can carry a credential embedded in the
    URL, on a plain failure, a fetch that resolved `origin` back to it, or a timeout. Reports only
    the return code (or, for a timeout, its duration) and `repo_url_sha256(repo_url)`."""
    if isinstance(exc, subprocess.TimeoutExpired):
        detail = f"timed out after {exc.timeout}s"
    else:
        m = _RC_RE.search(str(exc))
        detail = f"rc={m.group(1)}" if m else "rc=?"
    return RuntimeError(f"git {step} failed ({detail}) for repo (sha256:{repo_url_sha256(repo_url)}); "
                        "stderr redacted (repo_url may carry a credential)")


def clone(repo_url, branch, root):
    """One clone per epic under root/<branch-slug> (git dir under root/.gitdirs/); reused
    (git fetch + checkout) on repeat calls."""
    _validate_ref(branch, what="branch")
    _validate_repo_url(repo_url)
    root = Path(root).absolute()
    ws = root / _slug(branch)
    if ws.resolve().parent != root.resolve():
        raise ValueError(f"unsafe workspace path (escapes root): {ws}")
    gd = gitdir(ws)
    if gd.is_dir():
        _repoint(ws, gd)
        try:
            _git("fetch", "--end-of-options", "origin", branch, ws=ws, timeout=GIT_SLOW_TIMEOUT)
        except (RuntimeError, subprocess.TimeoutExpired) as exc:
            raise _redact_git_failure("fetch", exc, repo_url) from None
        try:   # a fetch never moves the local branch (sync_to_origin does), but a checkout of the
            # branch already checked out rewrites HEAD, so it only runs when HEAD is elsewhere
            on_branch = _git("symbolic-ref", "--quiet", "HEAD", ws=ws).stdout.strip()
        except RuntimeError as exc:
            rc = _RC_RE.search(str(exc))
            if not rc or rc.group(1) != "1":   # `-q`: rc 1 is a detached HEAD, else a real error
                raise
            on_branch = ""
        if on_branch != f"refs/heads/{branch}":
            _git("checkout", "--end-of-options", branch, ws=ws)
    else:
        if ws.exists() and (not ws.is_dir() or any(ws.iterdir())):
            raise RuntimeError(f"{ws} exists without its git dir {gd} (a clone from before the "
                               "separate git dir layout?); move it aside and retry")
        gd.parent.mkdir(parents=True, exist_ok=True)
        try:
            _git("clone", f"--separate-git-dir={gd}", "--", repo_url, str(ws), timeout=GIT_SLOW_TIMEOUT)
        except (RuntimeError, subprocess.TimeoutExpired) as exc:
            raise _redact_git_failure("clone", exc, repo_url) from None
        _repoint(ws, gd)   # clone wrote an absolute pointer; the containers need the relative one
        _git("checkout", "--end-of-options", branch, ws=ws)
    return ws


def tip(ws, branch):
    """The epic branch's current tip as `clone`'s fetch just saw it (`origin/<branch>`), else the
    clone's HEAD. `clone` fetches but `checkout` never fast-forwards the local branch, so the
    clone's working tree can sit behind the host branch (a plan amended on the host
    epic branch never reaches the clone). Review inputs read here, not there."""
    _validate_ref(branch, what="branch")
    try:
        return _git("rev-parse", "--verify", "--end-of-options", f"refs/remotes/origin/{branch}^{{commit}}",
                    ws=ws).stdout.strip()
    except RuntimeError:
        return head(ws)


def _validate_path(path):
    """A repo-relative file path for `read_at`: never absolute, never an option, never `..`."""
    if not path or not isinstance(path, str) or "\0" in path or "\n" in path:
        raise ValueError(f"unsafe path: {path!r}")
    if path.startswith(("/", "-")) or ".." in Path(path).parts:
        raise ValueError(f"unsafe path: {path!r}")
    return path


def validate_untrack_path(path):
    """`path` is safe for `untrack` below (also called from `spool._apply_verb`, before a `ws`
    even exists, so a bad path is caught as `invalid` at the spool stage): relative, non-root,
    never `..`, never absolute, never under (any-case) `.git/`."""
    _validate_path(path)
    parts = Path(path).parts
    if not parts or any(p.lower() == ".git" for p in parts):
        raise ValueError(f"unsafe path (root or under .git/): {path!r}")
    return path


def untrack(ws, path):
    """`git rm --cached` (index only, working-tree file untouched) so a card's own staged noise
    does not travel through `commit`'s `git add -A` again.
    `--literal-pathspecs`: `path` is never read as pathspec magic (`*`, `:/`, ...), only the
    literal path a worker named. A path that was never tracked is a harmless no-op
    (`--ignore-unmatch`)."""
    validate_untrack_path(path)
    _git("--literal-pathspecs", "rm", "-q", "-r", "--cached", "--ignore-unmatch",
         "--end-of-options", "--", path, ws=ws)


def read_at(ws, rev, path):
    """`path`'s text in commit `rev` of the clone's git dir (host git, never the working tree a run
    could have edited), or None when `rev` has no such file."""
    _validate_ref(rev, what="rev")
    _validate_path(path)
    try:
        return _git("cat-file", "blob", "--end-of-options", f"{rev}:{path}", ws=ws).stdout
    except RuntimeError:
        return None


def head(ws):
    return _git("rev-parse", "HEAD", ws=ws).stdout.strip()


def sync_to_origin(ws):
    """Brings `ws`'s checked-out branch up to its already-fetched `origin/<branch>` when it is
    purely behind - ff-only, never a merge, rebase or reset, and never over uncommitted changes.
    `clone()` fetches on every reuse but a `checkout` of a branch already checked out never
    fast-forwards it (`tip()`'s own docstring names the gap). Called by
    `Tick._claim_and_launch` before `start_sha` is pinned - after that point a fast-forward would
    hand the reviewer origin's own commits as if they were this card's work.

    Returns None on success or when there is nothing to do (already up to date, ahead of origin,
    HEAD detached, or no `origin/<branch>` was ever fetched). Otherwise an `(error_class, reason)`
    pair: `"worktree_dirty"` when `git status --porcelain` is non-empty (`reason` lists every
    path; nothing here ever runs `checkout`/`reset`/`clean` to clear them), or `"worktree_mismatch"`
    for a genuine divergence or a failed fast-forward (`reason` names BOTH the local and origin
    shas) - never discarded by forcing through."""
    try:
        branch = _git("rev-parse", "--abbrev-ref", "HEAD", ws=ws).stdout.strip()   # HEAD: a fixed literal, no injection risk
        if not branch or branch == "HEAD":
            return None   # detached HEAD: nothing named to compare against origin
        remote = _git("rev-parse", "--verify", "--end-of-options",
                      f"refs/remotes/origin/{branch}^{{commit}}", ws=ws).stdout.strip()
    except RuntimeError:
        return None   # no remote-tracking ref was ever fetched: nothing to sync against
    local = head(ws)
    if local == remote:
        return None
    try:
        base = _git("merge-base", "--end-of-options", local, remote, ws=ws).stdout.strip()
    except RuntimeError:
        return None   # unrelated histories: leave it to the identity check to refuse this
    if base == remote:
        return None   # local is strictly AHEAD of origin (its own commits, e.g. a prior card's
                       # implementer work) - normal epic progress, nothing to sync, not a mismatch
    if base != local:
        return ("worktree_mismatch",
                f"local {local} and origin/{branch} {remote} have diverged (merge-base {base})")
    # Strictly behind: a fast-forward is the safe move ONLY over a clean tree. `merge --ff-only`
    # can itself succeed with some uncommitted changes present (when they touch no path the
    # fast-forward moves), but that is not a risk worth taking here - any uncommitted change
    # refuses the sync outright, named in full, never silently swept aside.
    dirty = _git("status", "--porcelain", "--end-of-options", ws=ws).stdout
    paths = [line[3:].strip() for line in dirty.splitlines() if line.strip()]
    if paths:
        return ("worktree_dirty",
                f"uncommitted changes at risk of a fast-forward to origin/{branch} {remote}: "
                + ", ".join(paths))
    try:
        _git("merge", "--ff-only", "--end-of-options", remote, ws=ws)
    except RuntimeError as exc:
        return ("worktree_mismatch",
                f"local {local} could not be fast-forwarded to origin/{branch} {remote}: {exc}")
    return None


_SCP_LIKE = re.compile(r"^(?:[\w.\-]+@)?[\w.\-]+:(?!//)")
_URL_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*://")


def _normalize_origin(url):
    """Make a repo URL comparable across its two sources: `git config --get
    remote.origin.url` (always exact, and a relative local path is stored resolved to absolute
    by `git clone` itself) versus `cfg["repo_url"]` as an operator typed it in config.json
    (possibly relative, possibly with a cosmetic trailing `.git` or `/`). Strips a trailing
    slash and a trailing `.git`, then, for a bare local filesystem path (no `scheme://` and not
    scp-like `[user@]host:path`; the user is optional - `host:repo` is scp-like too,
    not a local path with a colon in it), resolves it exactly as `clone()`'s own `git clone` call
    would (against the process's cwd - both run in the same tick process)."""
    s = str(url).strip().rstrip("/")
    if s.lower().endswith(".git"):
        s = s[: -len(".git")].rstrip("/") or s
    if not _URL_SCHEME.match(s) and not _SCP_LIKE.match(s):
        try:
            s = str(Path(s).expanduser().resolve())
        except OSError:
            pass
    return s


def repo_url_sha256(repo_url):
    """The sha256 hex digest of `repo_url`, normalised: `job.json` carries
    only this, never the URL itself. A private repo_url can carry embedded credentials (RUNBOOK
    "A private https:// remote needs..."); `job.json` is evidence a `worktree_mismatch` or crash
    incident hands to Korin (`incidents.from_events` -> `--add-dir`), so it must not carry the
    token even though the token already sits, unavoidably, in the clone's own git config.
    Normalising first means a relative `repo_url` in config.json still hashes to what
    `remote.origin.url` reads back as (`clone()` resolved it to absolute at clone time), and a
    cosmetic trailing `.git`/`/` never causes a false mismatch."""
    return hashlib.sha256(_normalize_origin(repo_url).encode()).hexdigest()


def why_not_worktree(ws, root, repo_url_sha256_):
    """None iff `ws` is exactly the clone `clone(repo_url, <branch>, root)` would have produced:
    not a symlink, directly under `root` (the same escape guard `clone` itself applies), the git
    dir `clone`/`_repoint` would have made for it
    (`gitdir(ws)`) exists, and that git dir's own `remote.origin.url` (read with `_git`'s pin, so
    it is this exact git dir's config, never whatever `.git` a run left in `ws`), normalised and
    hashed, matches this epic's `repo_url_sha256_` (the caller never hands this function, or
    `job.json`, the raw URL - only its hash, via `repo_url_sha256` above; normalised first, so
    a relative `repo_url` or a cosmetic trailing `.git`/`/` never causes a false mismatch).
    Otherwise, a short string naming which specific check refused it - never the
    repo_url or origin value itself, since either may carry an embedded credential and this
    string is what `worker.run_one` folds into the typed event's `error`, which reaches Korin's
    evidence.

    This deliberately does NOT re-discover the git dir with ordinary unpinned
    git. `clone()` re-`_repoint`s `ws/.git` to point at
    `gitdir(ws)` before every launch, so by the time this runs, unpinned discovery would find
    nothing an attacker could have planted since the last repoint anyway; running unpinned host
    git against whatever the *container* left at `ws/.git` also contradicts `_repoint`'s own
    documented invariant ("nothing here opens or reads what is there"). The git-dir-exists check
    plus the pinned origin check below are what actually distinguish a genuine clone from a
    planted bare repo or a foreign repo's git dir moved under our `.gitdirs` (both refuse because
    the *pinned* origin lookup reads that particular directory's config, not one discovery could
    have been tricked into finding elsewhere).

    The implementer pre-edit check: a run whose assigned workspace fails any of this is refused
    before anything runs there."""
    if not root or not repo_url_sha256_:
        return "no work_root/repo_url_sha256 on this job (predates this check, or corrupted)"
    ws = Path(ws)
    if ws.is_symlink():
        return "assigned workspace is a symlink"
    if not ws.is_dir():
        return "assigned workspace does not exist"
    try:
        root_r, ws_r = Path(root).resolve(), ws.resolve()
    except OSError:
        return "assigned workspace or work root path could not be resolved"
    if ws_r.parent != root_r:
        return f"assigned workspace is not directly under the work root ({root_r})"
    gd = gitdir(ws)
    if not gd.is_dir():
        return "assigned workspace has no separate git dir (never produced by workspace.clone)"
    try:
        origin = _git("config", "--get", "remote.origin.url", ws=ws).stdout.strip()
    except (RuntimeError, OSError, subprocess.TimeoutExpired):
        return "assigned workspace's git dir has no remote.origin.url"
    if repo_url_sha256(origin) != repo_url_sha256_:
        return ("assigned workspace's origin does not match the epic's configured repo_url - if"
                " repo_url in config.json was intentionally changed, re-clone the epic's worktree;"
                " editing config.json alone does not repoint an existing clone")
    return None


def is_worktree(ws, root, repo_url_sha256_):
    """True iff `ws` is exactly the clone the epic identified by `root`/`repo_url_sha256_` would
    have produced. See `why_not_worktree` for what each guard checks and why."""
    return why_not_worktree(ws, root, repo_url_sha256_) is None


def commit(ws, message):
    """Commit everything the run left in `ws` (tracked and untracked, `.gitignore` honoured) so the
    next card on the epic starts from it (each worker inherits the previous one's
    result through git). Returns the new HEAD, or None when there was nothing to commit. Host
    git only, with HARDENING (hooks off) and a fixed identity: global config is never read."""
    _git("add", "-A", *SCRATCH_EXCLUDE, ws=ws)
    if not _git("diff", "--cached", "--name-only", "-z", ws=ws).stdout.strip("\0"):
        return None
    _git(*IDENTITY, "commit", "-q", "--no-verify", "--no-edit", "-m", message, ws=ws)
    return head(ws)


def diff(ws, base):
    """Everything since `base`, new files included: untracked paths are first marked
    intent-to-add (index only, contents untouched) so `git diff` shows them; before this, a
    card that only created files handed its reviewer an empty diff."""
    _validate_ref(base, what="base")
    _git("add", "--all", "--intent-to-add", *SCRATCH_EXCLUDE, ws=ws)
    return _git("diff", "--no-ext-diff", "--no-textconv", "--end-of-options", base, ws=ws).stdout


def changed(ws, base):
    """Every path that differs from `base`: tracked changes plus every untracked file, ignored
    ones included (a run's own .gitignore must not hide a planted CLAUDE.md)."""
    _validate_ref(base, what="base")
    # `--name-only` is an option and must come *before* `--end-of-options` -- git stops
    # accepting options once it sees `--end-of-options`, so an option placed after it is
    # read as a revision/path instead and the whole call fails.
    # `--no-renames`: a rename out of a protected path must list the old path too (as lander.py).
    tracked = _git("diff", "--no-ext-diff", "--no-renames", "--name-only", "-z",
                   "--end-of-options", base, ws=ws).stdout
    untracked = _git("ls-files", "--others", "-z", ws=ws).stdout
    out = []
    for p in (tracked + untracked).split("\0"):
        if p and p not in out:
            out.append(p)
    return out


def restore(ws, base, paths):
    """Put each of `paths` back as it was at `base` (worktree and index); a path `base` did
    not have is removed. Used on a protected-path hit so a planted file never reaches the
    next run in this clone."""
    _validate_ref(base, what="base")
    ws = Path(ws)
    paths = [p for p in paths if p]
    if not paths:
        return
    at_base = set(_git("ls-tree", "-z", "--name-only", "--end-of-options", base, "--", *paths,
                       ws=ws).stdout.split("\0")) - {""}
    if at_base:
        _git("checkout", "--end-of-options", base, "--", *sorted(at_base), ws=ws)
    gone = [p for p in paths if p not in at_base]
    if gone:
        _git("rm", "-q", "-r", "--cached", "--ignore-unmatch", "--", *gone, ws=ws)
    for p in gone:
        target = ws / p.rstrip("/")
        if not target.absolute().parent.resolve().is_relative_to(ws.resolve()):
            continue
        if target.is_symlink() or target.is_file():
            target.unlink()
        elif target.is_dir():
            shutil.rmtree(target)


def _shadows_stdlib(path):
    """The gates run as `python3 <trusted>/tests/<x>.py`, so a new `tests/argparse.py`,
    `tests/json/__init__.py` or sourceless `tests/re.cpython-313.pyc` would import before the
    standard library and run inside every gate. Flags a tests/ entry named after a stdlib module,
    and a maintenance/ one too: docker_door puts that directory on `sys.path`."""
    parts = path.split("/")
    return (len(parts) > 1 and parts[0].casefold() in ("tests", "maintenance")
            and parts[1].split(".")[0] in sys.stdlib_module_names)


def touches_protected(paths):
    """Which of `paths` match a PROTECTED glob, checked as-is and with any `**/` prefix stripped
    (so `**/CLAUDE.md` also flags a root-level `CLAUDE.md`, which fnmatch's literal `/` would miss)."""
    flagged = []
    roots = [g[:-3].casefold() for g in PROTECTED if g.endswith("/**")]
    for p in paths:
        q = p.casefold().rstrip("/")   # a bare dir/symlink path: a glob's root or its ancestor
        if _shadows_stdlib(p) or any(r == q or r.startswith(q + "/") for r in roots):
            flagged.append(p)
            continue
        for pattern in PROTECTED:
            candidates = (pattern, pattern[3:]) if pattern.startswith("**/") else (pattern,)
            if any(fnmatch.fnmatchcase(p.casefold(), c.casefold()) for c in candidates):
                flagged.append(p)
                break
    return flagged
