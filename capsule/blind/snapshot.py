"""Blind interface snapshot (blind spec 3.2, 4.4 item 4; R29): what the blind tester may know of the
product, taken host side from the card's base commit. Read-only on the repo (`git show`,
`git ls-tree`); writes only under `out`."""
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path, PurePosixPath

from capsule.blind.preflight import PRODUCTION_ROOTS, git

MAX_FILE_BYTES = 2 * 1024 * 1024
INTERFACE_SUFFIXES = (".md", ".txt", ".json")   # docs and schemas; every other file is production
INTERFACE_NAMES = ("SHA256SUMS",)


class BriefLeak(Exception):
    """A tester brief names an identifier from the epic's or the card's own diff."""

    def __init__(self, identifiers):
        self.identifiers = sorted(identifiers)
        super().__init__("the tester brief names diff identifiers: " + ", ".join(self.identifiers))


def _glob_re(glob):
    """Regex for a glob: `*` and `?` stay inside a path segment, `**` crosses `/`
    (`**/` may match nothing)."""
    out, i = [], 0
    while i < len(glob):
        if glob.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif glob.startswith("**", i):
            out.append(".*")
            i += 2
        else:
            c = glob[i]
            out.append("[^/]*" if c == "*" else "[^/]" if c == "?" else re.escape(c))
            i += 1
    return re.compile("".join(out) + r"\Z")


def _git_out(repo, *args):
    done = git(repo, *args)
    if done.returncode:
        raise RuntimeError(f"git {args[0]} failed: {done.stderr.strip()}")
    return done.stdout


def _tracked(repo, sha):
    """Paths of the regular files committed at `sha` (symlinks and submodules left out)."""
    if str(sha).startswith("-"):
        raise ValueError(f"not a commit: {sha!r}")
    listing = _git_out(repo, "ls-tree", "-r", "-z", str(sha))
    entries = (e.split("\t", 1) for e in listing.split("\0") if e)
    return [path for meta, path in entries if meta.split()[0] in ("100644", "100755")]


def _matching(repo, sha, globs):
    regexes = [_glob_re(g) for g in globs]
    return [p for p in _tracked(repo, sha) if any(r.match(p) for r in regexes)]


def _is_production(path):
    """Production code: anything but docs and schemas (`.md`, `.txt`, `.json`, `SHA256SUMS`) with
    no dot segment in the path, and anything under a production root."""
    p = PurePosixPath(path)
    interface = p.suffix in INTERFACE_SUFFIXES or p.name in INTERFACE_NAMES
    dotted = any(s.startswith(".") for s in p.parts)
    return p.parts[0] in PRODUCTION_ROOTS or not interface or dotted


def production_globs(repo, base_sha, may_read):
    """The globs in `may_read` that match any production file committed at `base_sha`."""
    production = [p for p in _tracked(repo, base_sha) if _is_production(p)]
    return [g for g in may_read if any(_glob_re(g).match(p) for p in production)]


def _copy_files(repo, sha, rels, dest):
    for rel in rels:
        if ".." in PurePosixPath(rel).parts or rel.startswith("/"):
            raise ValueError(f"refusing path {rel!r}")
        text = _git_out(repo, "show", f"{sha}:{rel}")
        if len(text.encode("utf-8")) > MAX_FILE_BYTES:
            raise ValueError(f"{rel} is over {MAX_FILE_BYTES} bytes")
        target = dest / "files" / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8", newline="")


def _run_commands(commands, dest, run_command):
    """Each command's stdout to `commands/<index>.txt`. A command that raises is recorded with
    its exit code (`CalledProcessError`) or error, never as a clean run. Returns failed indices."""
    (dest / "commands").mkdir(parents=True, exist_ok=True)
    failed = []
    for index, cmd in enumerate(commands):
        try:
            text = run_command(cmd)
        except subprocess.CalledProcessError as exc:
            text = f"{exc.stdout or ''}\n[exit {exc.returncode}]\n"
            failed.append(index)
        except Exception as exc:
            text = f"[command failed: {type(exc).__name__}: {exc}]\n"
            failed.append(index)
        (dest / "commands" / f"{index}.txt").write_text(text, encoding="utf-8")
    return failed


def _swap(work, out):
    """`work` becomes `out`. A good `out` is moved aside first and put back if the move fails."""
    aside = None
    if out.exists():
        aside = out.with_name(f".{out.name}.old-{os.getpid()}")
        shutil.rmtree(aside, ignore_errors=True)
        os.replace(out, aside)
    try:
        os.replace(work, out)
    except BaseException:
        if aside:
            os.replace(aside, out)
        raise
    if aside:
        shutil.rmtree(aside, ignore_errors=True)


def take(repo, base_sha, may_read, commands, out, run_command):
    """Write the snapshot to `out`: files matching `may_read` as committed at `base_sha` to
    `out/files/<path>`, each command's stdout to `out/commands/<index>.txt`. Refuses (ValueError,
    nothing written) a glob that matches production code. Built beside `out` and renamed into
    place, so a failure leaves no partial snapshot and a stale `out` never survives. Returns the
    indices of commands that failed."""
    out = Path(out)
    bad = production_globs(repo, base_sha, may_read)
    if bad:
        raise ValueError("may_read names production code: " + ", ".join(bad))
    out.parent.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix=f".{out.name}.", dir=out.parent))
    try:
        _copy_files(repo, base_sha, _matching(repo, base_sha, may_read), work)
        failed = _run_commands(commands, work, run_command)
        os.chmod(work, 0o755)
        _swap(work, out)
    except BaseException:
        shutil.rmtree(work, ignore_errors=True)
        raise
    return failed
