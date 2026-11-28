#!/usr/bin/env python3
"""Public-edition export (P10a, spec "Public portable edition", unit P10).

Writes the allowlisted tree of the private repo at one commit into a destination checkout and
commits it there as "Sync from private repo". It reads the private repo (git ls-tree, git show,
git cat-file) and writes only to DEST, plus one line per commit appended to a private mapping log
(public commit -> private sha) under the maintenance state dir (events.state_root(); never in the
public tree or its history). It never pushes, never creates a remote or a repo on any host, never
touches the network (Z6: publishing is a human step).

  public_export.py SHA DEST [--repo DIR] [--denylist FILE] [--dry-run] [--no-commit]

Order: refuse if the private denylist (release/private-denylist.txt) is missing; build the public
tree in a temp dir from raw blobs (no filters, no export-subst); scan it with public_lint (personal
paths, names, other denylist words, symlinks that are absolute or leave the tree, submodules); any
hit stops the run before DEST is touched (exit 1). Then DEST gets the new files, tracked files no
longer allowlisted are deleted, and the change is committed. An export that changes nothing makes
no commit.

DEST must be missing, empty, or the root of a clean git checkout whose whole history is export-only
(refused when it shares the private repo's git dir, holds a root commit that exists in the private
repo, or has any commit not authored and committed by the export identity). Only tracked files are
ever deleted; ignored and untracked files, nested repos and anything under .git are never touched,
and a file the export would add over an existing untracked or ignored path is refused. Anything
unsafe is refused (exit 2) so a mistyped path cannot lose data or publish history.

Every git call runs without GIT_DIR, GIT_WORK_TREE, GIT_INDEX_FILE and the other redirecting
variables; the commit uses a fixed generic identity and a +0000 timestamp from the environment,
no global or system config, no hooks.

--dry-run writes nothing anywhere: it lists the allowlisted files, the scan hits, and one line
"public would change N files" (added/changed/deleted against DEST, or all files if DEST is new).
--no-commit syncs the files only: no git init, no git add, no commit, no mapping line.
Exit 0 ok, 1 scan hits, 2 bad usage or refused DEST.
"""
import argparse
import datetime
import filecmp
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import events
import public_lint

NAME, EMAIL = "capsule-export", "export@capsule.invalid"
MESSAGE = "Sync from private repo"
MAP_LOG = "public-export.log"


class Refused(Exception):
    """Bad usage or an unsafe DEST; main() prints it and exits 2."""


def _git(cwd, *args, check=True, env=None):
    return subprocess.run(["git", "-C", str(cwd), "--no-optional-locks", *args],
                          capture_output=True, check=check, env=env or public_lint.clean_env())


def _commit_env():
    """Explicit identity and dates, no user or system config, literal pathspecs."""
    when = f"{int(time.time())} +0000"
    return {**public_lint.clean_env(), "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_LITERAL_PATHSPECS": "1",
            "GIT_AUTHOR_NAME": NAME, "GIT_AUTHOR_EMAIL": EMAIL, "GIT_AUTHOR_DATE": when,
            "GIT_COMMITTER_NAME": NAME, "GIT_COMMITTER_EMAIL": EMAIL, "GIT_COMMITTER_DATE": when}


def _same(a, b):
    """Same kind, same link target or same bytes, same executable bit."""
    if a.is_symlink() or b.is_symlink():
        return a.is_symlink() and b.is_symlink() and os.readlink(a) == os.readlink(b)
    return (b.is_file() and filecmp.cmp(a, b, shallow=False)
            and bool(a.stat().st_mode & 0o100) == bool(b.stat().st_mode & 0o100))


def _tracked_files(dest):
    """Names git tracks in DEST ([] when DEST is not a repo yet): the only files we may delete."""
    if not (Path(dest) / ".git").exists():
        return []
    out = _git(dest, "ls-files", "-z").stdout.decode()
    return sorted(n for n in out.split("\0") if n)


def _diff_trees(new, files, dest):
    """(added, changed, deleted) name lists; `files` are the names in the new tree."""
    new_files = [n for n in files if os.path.lexists(Path(new) / n)]
    old = set(_tracked_files(dest))
    dest = Path(dest)
    added = [n for n in new_files if n not in old]
    changed = [n for n in new_files if n in old and not _same(Path(new) / n, dest / n)]
    deleted = sorted(old - set(new_files))
    return added, changed, deleted


def _check_write_paths(dest, added, changed):
    """Refuse a write that would land through a symlink, over a non-directory, or over an
    untracked or ignored file that DEST does not export."""
    dest = Path(dest).resolve()
    for n in added + changed:
        p = dest
        for part in Path(n).parent.parts:
            p = p / part
            if p.is_symlink() or (p.exists() and not p.is_dir()):
                raise Refused(f"{n}: DEST has a symlink or file where a directory is needed")
    for n in added:
        if os.path.lexists(dest / n):
            raise Refused(f"{n}: DEST already has an untracked or ignored file at this path")


def _check_history(dest, repo):
    """Fail closed: a git error while reading DEST's history is a refusal, not a pass."""
    try:
        roots = _git(dest, "rev-list", "--all", "--max-parents=0").stdout.decode().split()
        log = _git(dest, "log", "--all", "--format=%an <%ae>|%cn <%ce>").stdout.decode()
    except subprocess.CalledProcessError as e:
        raise Refused(f"cannot read DEST history, refusing: {(e.stderr or b'').decode().strip()}")
    for root in roots:
        if _git(repo, "cat-file", "-e", f"{root}^{{commit}}", check=False).returncode == 0:
            raise Refused(f"DEST history holds commit {root[:8]}, which exists in the private repo")
    ident = f"{NAME} <{EMAIL}>"
    for line in log.splitlines():
        if line != f"{ident}|{ident}":
            raise Refused("DEST history has a commit not made by the export identity")


def _check_dest(dest, repo):
    """Refuse a DEST that could lose data, share the private repo's history, or leak it."""
    dest, repo = Path(dest).resolve(), Path(repo).resolve()
    if dest == repo or repo in dest.parents or dest in repo.parents:
        raise Refused(f"DEST {dest} overlaps the private repo {repo}")
    if dest.exists() and not dest.is_dir():
        raise Refused(f"DEST {dest} is not a directory")
    if not dest.exists() or not any(dest.iterdir()):
        return
    top = _git(dest, "rev-parse", "--show-toplevel", check=False)
    if top.returncode != 0 or Path(top.stdout.decode().strip()).resolve() != dest:
        raise Refused(f"DEST {dest} is not empty and is not the root of a git checkout")

    def common(d):
        return Path(_git(d, "rev-parse", "--path-format=absolute", "--git-common-dir")
                    .stdout.decode().strip()).resolve()
    if common(dest) == common(repo):
        raise Refused(f"DEST {dest} shares the private repo's git directory")
    _check_history(dest, repo)
    if _git(dest, "status", "--porcelain", "--untracked-files=all").stdout.strip():
        raise Refused(f"DEST {dest} has uncommitted changes; commit or clean them first")


def sync(new, dest, added, changed, deleted):
    """Copy added and changed files into dest, delete the de-listed tracked ones, remove the
    directories that emptied. Nothing else in dest is read or written."""
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    for n in deleted:
        p = dest / n
        if p.is_symlink() or p.is_file():
            p.unlink()
        for parent in p.parents:
            if parent == dest or ".git" in parent.relative_to(dest).parts:
                break
            try:
                parent.rmdir()
            except OSError:  # not empty (ignored files, a nested repo): leave it
                break
    for n in added + changed:
        src, dst = Path(new) / n, dest / n
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.is_symlink() or dst.exists():
            dst.unlink()
        if src.is_symlink():
            os.symlink(os.readlink(src), dst)
        else:
            shutil.copy2(src, dst)


def commit(dest, names):
    """Stage exactly `names` (also when DEST's own .gitignore matches them) and commit; returns
    the new commit id, or None when nothing changed."""
    dest, env = Path(dest), _commit_env()
    if not (dest / ".git").exists():
        subprocess.run(["git", "-C", str(dest), "init", "-q", "-b", "main", "--template="],
                       capture_output=True, check=True, env=env)
    hooks = ("-c", f"core.hooksPath={os.devnull}")
    if names:
        subprocess.run(["git", "-C", str(dest), *hooks, "add", "-A", "-f",
                        "--pathspec-from-file=-", "--pathspec-file-nul"],
                       input="\0".join(names).encode(), capture_output=True, check=True, env=env)
    staged = subprocess.run(["git", "-C", str(dest), "diff", "--cached", "--name-only"],
                            capture_output=True, check=True, env=env).stdout
    if not staged.strip():
        return None
    subprocess.run(["git", "-C", str(dest), *hooks, "commit", "-q", "--no-verify", "-m", MESSAGE],
                   capture_output=True, check=True, env=env)
    return subprocess.run(["git", "-C", str(dest), "rev-parse", "HEAD"], capture_output=True,
                          check=True, env=env).stdout.decode().strip()


def _log_mapping(public_id, private_sha, n):
    """Append `time public private files` to the private state dir; a failure is only a warning
    (the commit already exists)."""
    try:
        path = events.state_root() / MAP_LOG
        path.parent.mkdir(parents=True, exist_ok=True)
        now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{now}\t{public_id}\t{private_sha}\t{n}\n")
    except OSError as e:
        print(f"public_export: could not write the mapping log: {e}", file=sys.stderr)


def _resolve(a):
    """(repo, sha, rules, line_exempt) after every refusal that needs no tree."""
    repo = Path(a.repo).resolve()
    try:
        sha = _git(repo, "rev-parse", "--verify", f"{a.sha}^{{commit}}").stdout.decode().strip()
    except subprocess.CalledProcessError:
        raise Refused(f"{a.sha} is not a commit in {repo}")
    try:
        rules, line_exempt = public_lint.load_rules(repo, a.denylist)
    except (OSError, ValueError) as e:
        raise Refused(str(e))
    _check_dest(a.dest, repo)
    return repo, sha, rules, line_exempt


def _build(repo, sha, tree, rules, line_exempt):
    """(files, hits): the public tree at sha written into `tree`, and its scan."""
    try:
        files, _ = public_lint._run(argparse.Namespace(
            repo=str(repo), ref=sha, allowlist=None, list=False, extract=str(tree)))
    except (OSError, ValueError, subprocess.CalledProcessError) as e:
        detail = (getattr(e, "stderr", None) or b"").decode().strip() or e
        raise Refused(f"cannot build the public tree at {sha}: {detail}")
    return files, public_lint._scan(tree, files, rules, line_exempt)


def _finish(a, sha, names):
    """Commit (unless --no-commit) and log the public -> private mapping."""
    n = len(names)
    if a.no_commit:
        print(f"public_export: synced {n} files from private {sha[:8]} (no commit)")
        return 0
    public_id = commit(a.dest, names)
    if public_id is None:
        print(f"public_export: no changes from private {sha[:8]}; no commit")
    else:
        _log_mapping(public_id, sha, n)
        print(f"public_export: committed '{MESSAGE}' as {public_id[:8]} ({n} files changed; "
              f"private {sha[:8]} is in the mapping log)")
    return 0


def _check_workflows(repo, sha, tree, files):
    """P10: the exported workflows must be runnable by a public clone. Fails before any commit
    naming workflow and path/line: a repo path they mention (first segment is a private top-level
    name, comments ignored) that is not in the export, a non-ubuntu runner, or a secret other
    than GITHUB_TOKEN."""
    top = set(_git(repo, "ls-tree", "--name-only", sha).stdout.decode().split("\n"))
    for wf in (n for n in files if re.fullmatch(r"\.github/workflows/[^/]+\.ya?ml", n)):
        lines = (Path(tree) / wf).read_text(encoding="utf-8").splitlines(keepends=True)
        lines = _trim_private(wf, lines)  # the trimmed copy for the public
        (Path(tree) / wf).write_text("".join(lines), encoding="utf-8")
        for line in lines:
            for problem in _line_problems(_code(line), top, tree):
                raise Refused(f"{wf}: {problem}")


def _code(line):
    """The line without its trailing `# comment` (a `#` inside quotes stays)."""
    return re.match(r"""(?:"[^"]*"|'[^']*'|[^#]|(?<=\S)#)*""", line).group(0).rstrip()


def _trim_private(wf, lines):
    """Drop lines marked `# private-only`; the marker is allowed only on a plain line inside a
    `run: |` or `run: >` block, never on a line that opens YAML structure."""
    out, block = [], None
    for x in lines:
        code, ind = _code(x), len(x) - len(x.lstrip())
        if block is not None and code and ind <= block:
            block = None
        if "# private-only" in x:
            if block is None or code.endswith((":", "|", ">")) or code.lstrip().startswith("- "):
                raise Refused(f"{wf}: `# private-only` is allowed only on a plain line in a run: "
                              f"block: {x.strip()}")
            continue
        m = re.match(r"(\s*)(- )?run:\s*[|>][+-]?$", code)
        if m:
            block = len(m.group(1)) + (2 if m.group(2) else 0)
        out.append(x)
    return out


PATH_ARG_AFTER = {"discover", "-r", "-s", "-f", "--file", "--requirement"}
PATH_EXT = (".txt", ".sh", ".py", ".toml", ".json", ".lock", ".cfg", ".ini")
# Files a workflow step creates at run time (release.yml's SBOM step), never part of the export.
BUILD_OUTPUTS = {"dist/sbom.spdx.json", "sbom.spdx.json"}


RUNNER_LABEL = r"""\[?["']?(ubuntu[\w.-]*|macos-latest|macos-\d+(-(xlarge|large))?)["']?\]?"""


def _line_problems(line, top, tree):
    """What is wrong with one comment-free workflow line, as short phrases."""
    out = []
    if re.search(r"\bsecrets\b(?!\.GITHUB_TOKEN\b)", line):
        out.append(f"uses secrets other than secrets.GITHUB_TOKEN: {line.strip()}")
    m = re.search(r"""["']?runs-on["']?\s*:\s*(\S*)""", line)
    if m and not re.fullmatch(RUNNER_LABEL, m.group(1)):
        out.append(f"runs-on {m.group(1) or '(block form)'}; "
                   "only GitHub-hosted ubuntu or macos runners are allowed")
    mapping = re.match(r"\s*(- )?(?!run:)[\w-]+:\s", line)  # a `key: value` line, not a command
    prev = ""
    for t in re.split(r"[\s=]+", re.sub(r"\$\{\{[^}]*\}\}/?", "", line)):
        t, was = t.strip("\"'[],;()"), prev
        prev = t
        dot = t.startswith("./")
        t = t[2:] if dot else t
        if not re.fullmatch(r"\w[\w.-]*(/[\w.-]+)*|\.\w[\w.-]*(/[\w.-]+)*", t.rstrip("/")):
            continue
        t = t.rstrip("/")
        if t in BUILD_OUTPUTS:
            continue
        if (dot or t.split("/")[0] in top or was in PATH_ARG_AFTER
                or (not mapping and t.endswith(PATH_EXT))):
            if not os.path.lexists(Path(tree) / t):
                out.append(f"refers to {t}, which is not in the public export")
    return out


def run(a):
    repo, sha, rules, line_exempt = _resolve(a)
    with tempfile.TemporaryDirectory() as tmp:
        tree = Path(tmp) / "tree"
        files, hits = _build(repo, sha, tree, rules, line_exempt)
        if a.dry_run:
            print("\n".join(files), file=sys.stderr)
        for name, line, cat, text in hits:
            print(f"{name}:{line}: {cat}: {text}")
        if hits:
            print(f"public_export: {len(hits)} scan hits; DEST not touched", file=sys.stderr)
            return 1
        _check_workflows(repo, sha, tree, files)
        added, changed, deleted = _diff_trees(tree, files, a.dest)
        _check_write_paths(a.dest, added, changed)
        if a.dry_run:
            print(f"public would change {len(added) + len(changed) + len(deleted)} files")
            print(f"(added {len(added)}, changed {len(changed)}, deleted {len(deleted)}) "
                  f"from private {sha[:8]}; {len(files)} public files, 0 scan hits",
                  file=sys.stderr)
            return 0
        sync(tree, a.dest, added, changed, deleted)
    return _finish(a, sha, added + changed + deleted)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("sha", help="private commit to export")
    ap.add_argument("dest", help="directory that receives the public tree")
    ap.add_argument("--repo", default=".", help="private repo (default: current directory)")
    ap.add_argument("--denylist", default=None,
                    help="private denylist file (default: release/private-denylist.txt in --repo)")
    ap.add_argument("--dry-run", action="store_true",
                    help="list files, scan, and print the change count; write nothing")
    ap.add_argument("--no-commit", action="store_true", help="sync files, do not git add/commit")
    try:
        return run(ap.parse_args(argv))
    except Refused as e:
        print(f"public_export: {e}", file=sys.stderr)
        return 2
    except subprocess.CalledProcessError as e:
        detail = (e.stderr or b"").decode().strip() or e
        print(f"public_export: git failed: {detail}", file=sys.stderr)
        return 2
    except OSError as e:
        print(f"public_export: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
