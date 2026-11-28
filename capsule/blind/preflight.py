"""Blind preflight (blind spec 2, R13): may a blind run start for this board? Returns plain-words
problems; an empty list means yes. Any problem holds the card with `blind: unavailable`, never a
silent pass. Read-only: never writes, never touches the network."""
import fnmatch
import os
import posixpath
import re
import subprocess
import tomllib
from pathlib import Path
from urllib.parse import unquote, urlparse

from capsule import config, paths, workspace
from capsule.runners import docker

# Blind spec gap 2: tests/size_check.py exports no "is production" predicate, so the
# `tester_may_read` guard is a fixed denylist of the roots the tester must never see.
PRODUCTION_ROOTS = ("capsule", "src", "lib", "bin", "tests")
TOML_NAME = "blind.toml"
# The one safe `run` line, written by `capsule blind init` and the only one preflight accepts.
# `cd /blind` first: `python3 -m` puts the cwd first on sys.path, so the cwd must be the snapshot,
# never the product's /workspace (a planted harness/ or unittest.py there would replace the runner).
# It also puts /blind on the path, so `import harness` needs no PYTHONPATH. `exec`: the shell the
# line runs under becomes the runner, so no same-user shell is left holding its pipe and exit code.
RUN_LINE = "cd /blind && exec python3 -m harness.unittest_runner --card {card} /blind/cards/{card}"
RUN_LINES = {"unittest": (RUN_LINE, RUN_LINE.replace("--card {card}", "--card {card} --list"))}
NOT_SAFE = "is not the safe form"      # in every stale-run-line problem; init keys its words on it
_WILDCARDS = set("*?[")
_REMOTE_URL = re.compile(r"^([a-z][a-z0-9+.-]*://|[\w.-]+@[\w.-]+:)", re.I)


def expand(path):
    """`~` expanded and symlinks resolved: the spelling every overlap check compares."""
    return Path(os.path.realpath(os.path.expanduser(str(path))))


def _inside(a, b):
    a, b = str(a).casefold(), str(b).casefold()
    return a == b or a.startswith(b.rstrip("/") + "/")


def _overlaps(a, b):
    """Equal, or one inside the other (a mount of either exposes part of the other)."""
    return _inside(a, b) or _inside(b, a)


def _local_repo_url(url):
    """The path a `repo_url` names when it is on this machine, else None."""
    if not isinstance(url, str) or not url:
        return None
    if url.startswith("file://"):
        return Path(unquote(urlparse(url).path))
    return None if _REMOTE_URL.match(url) else Path(os.path.expanduser(url))


def worker_mounts(cfg, board, home):
    """Every host path a worker container can see: the board directory (work root, spool, db), the
    deps mount, and the project repo when `repo_url` is a local path."""
    mounts = [paths.board_dir(board, home), Path.home() / docker.DEPS_MOUNT]
    local = _local_repo_url((cfg or {}).get("repo_url"))
    return mounts + ([local] if local else [])


def _glob_problem(glob):
    """Why a `tester_may_read` glob is refused, or None. Only plain relative paths pass: no
    whitespace, braces, `..`, `./`, `//` or a leading `.` segment (history, secrets), and a first
    segment that is neither a production root nor a wildcard that could match one. Compared
    case-folded because the host file system is. Conservative: `**/*.md` is refused too."""
    if glob.startswith(("/", "~")) or "\\" in glob or set("{}") & set(glob):
        return "must be a plain path inside the project"
    if any(c.isspace() or not c.isprintable() for c in glob):
        return "must not contain whitespace or control characters"
    if posixpath.normpath(glob) != glob:
        return "must be written canonically (no './', '//' or '..')"
    segments = glob.split("/")
    if any(s.startswith(".") for s in segments):
        return "must not reach a hidden file or directory"
    first = segments[0].casefold()
    if first in PRODUCTION_ROOTS:
        return f"names production code ({first}/)"
    if _WILDCARDS & set(first) and any(fnmatch.fnmatchcase(r, first) for r in PRODUCTION_ROOTS):
        return "its first segment could match production code; name the directory"
    return None


def block_problems(block, mounts=()):
    """Problems with a `blind_tests` block that need no repo on disk: its shape, its globs, and the
    repo path not overlapping any path a worker container mounts (`worker_mounts`)."""
    try:
        config.validate_blind_tests(block)
    except ValueError as exc:
        return [str(exc)]
    if not os.path.isabs(os.path.expanduser(block["repo"])):
        return [f"blind_tests.repo {block['repo']!r}: must be an absolute path"]
    out = []
    repo = expand(block["repo"])
    for mount in mounts:
        if _overlaps(repo, expand(mount)):
            out.append(f"the blind repo {repo} overlaps {expand(mount)}, which a worker mounts")
    for glob in block.get("tester_may_read", []):
        why = _glob_problem(glob)
        if why:
            out.append(f"tester_may_read {glob!r}: {why}")
    return out


def run_line_problem(line, adapter, where=TOML_NAME):
    """Why a `blind.toml` `run` line is not the safe form for `adapter`, or None. The line must
    equal `RUN_LINES[adapter][0]` exactly: it has no variable part (`{card}` is filled in per run,
    the image and mounts are not in it), so a wrapper, `;`, `&&`, `|`, extra words or spacing
    all fail."""
    if not isinstance(adapter, str) or adapter not in RUN_LINES:
        return f"{where}: adapter {adapter!r} has no blind runner yet, so no run line is safe"
    safe = RUN_LINES[adapter][0]
    if line == safe:
        return None
    return (f"{where}: run line {line!r} {NOT_SAFE}; change it to {safe!r}"
            " (the `exec` form `capsule blind init` writes;"
            " see docs/architecture/components/blind-path.md)")


def _head_run_problem(repo):
    """Why the `run` line in `blind.toml` at the blind repo's HEAD (the file the repo carries into a
    run) is not the safe form, or None. A repo with no commit has nothing to judge here:
    `repo_problem` already refuses it. An older commit's line does not matter. The run itself never
    executes a line from the file (`run._run_line`); this keeps the file honest for the operator."""
    if git(repo, "rev-parse", "--verify", "-q", "HEAD").returncode:
        return None
    where = f"{TOML_NAME} at HEAD"
    shown = git(repo, "show", f"HEAD:{TOML_NAME}")
    if shown.returncode:
        return f"{TOML_NAME} is not committed at HEAD of the blind repo {repo}; commit it"
    try:
        doc = tomllib.loads(shown.stdout)
    except tomllib.TOMLDecodeError as exc:
        return f"{where} unreadable: {exc}"
    run = doc.get("run")
    if not isinstance(run, str) or not run.strip():
        return f"{where}: run must be a command"
    found = run_line_problem(run, doc.get("adapter"), where)
    return found and f"{found}. Commit the change."


def toml_problems(path, board, adapter):
    """Problems with a repo's `blind.toml`: it parses, names this board and its adapter, and its
    `run` line is exactly the safe form, in the working copy and in the file at HEAD."""
    try:
        doc = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        return [f"{TOML_NAME} unreadable: {exc}"]
    out = []
    if doc.get("version") != 1:
        out.append(f"{TOML_NAME}: version must be 1")
    if doc.get("board") != board:
        out.append(f"{TOML_NAME}: board is {doc.get('board')!r}, not {board!r}")
    if doc.get("adapter") not in config.BLIND_ADAPTERS:
        out.append(f"{TOML_NAME}: adapter must be one of {config.BLIND_ADAPTERS}")
    elif doc["adapter"] != adapter:
        out.append(f"{TOML_NAME}: adapter {doc['adapter']!r} differs from the board's {adapter!r}")
    for key in ("run", "list"):
        if not isinstance(doc.get(key), str) or not doc[key].strip():
            out.append(f"{TOML_NAME}: {key} must be a command")
    run = doc.get("run")
    if isinstance(run, str) and run.strip() and doc.get("adapter") in config.BLIND_ADAPTERS:
        out += [p for p in (run_line_problem(run, doc["adapter"]),) if p]
    return out + [p for p in (_head_run_problem(Path(path).parent),) if p]


def git(repo, *args, env_extra=None, stdin=None):
    """Host git in `repo` with the hardening pins and the Capsule identity; never prompts, never
    reads the operator's git config. `env_extra` adds variables (a private GIT_INDEX_FILE), `stdin`
    is text for git's standard input. Returns the CompletedProcess."""
    env = workspace.hardening_env({k: v for k, v in os.environ.items()
                                   if not k.startswith(("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX"))})
    env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1", GIT_TERMINAL_PROMPT="0",
               **(env_extra or {}))
    return subprocess.run(["git", *workspace.IDENTITY, "-C", str(repo), *args], capture_output=True,
                          text=True, env=env, input=stdin, timeout=workspace.GIT_FAST_TIMEOUT)


def repo_problem(repo):
    """Why `repo` is not a git repository with a commit, or None (its own .git)."""
    has_git = (Path(repo) / ".git").exists()
    if not has_git or git(repo, "rev-parse", "--verify", "-q", "HEAD").returncode:
        return f"the blind repo {repo} is not a git repository with a commit"
    return None


def problems(cfg, board, mounts=()):
    """Every reason a blind run cannot start for `cfg` (a loaded board config) on `board`."""
    block = config.blind_tests(cfg)
    if block is None:
        return ["the board has no blind_tests block"]
    out = block_problems(block, mounts)
    if out:
        return out
    repo = expand(block["repo"])
    if not repo.is_dir():
        return [f"the blind repo {repo} does not exist"]
    found = repo_problem(repo)
    return ([found] if found else []) + toml_problems(repo / TOML_NAME, board, block["adapter"])
