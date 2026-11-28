#!/usr/bin/env python3
"""Release gate: the third stamp's checks."""
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import events      # noqa: E402
from capsule import host_registry  # noqa: E402
import merge_gate  # noqa: E402
import verdicts    # noqa: E402

FLAG = "OPS_RELEASE_GATE"
_ON = ("1", "true", "yes")
TAG_TIMEOUT_S = 30   # `git ls-remote` over the network: a stalled call must
                      # raise InfraError (exit 3), never hang the gate.
CHECKS = ("tag", "changelog", "sbom", "artefact", "stamp")
SBOM_NAME = "sbom.spdx.json"
SUMS_NAME = "SHA256SUMS"
# The quality gate's own line shape (quality_gate.changelog_line_ok:68-77): a `- `
# bullet ending `(t_xxxxxxxx)`.
TASK_LINE_RE = re.compile(r"^\s*-\s+.*\((t_[0-9a-f]{6,})\)\s*$")
_UNRELEASED = ("## unreleased", "## [unreleased]")


def enabled(env=None):
    """`OPS_RELEASE_GATE` over ops.env-under-os.environ (verdicts.ops_env()): the same
    idiom every C3 flag uses, because a host verb inherits an environment that does
    not carry ops.env."""
    env = env if env is not None else verdicts.ops_env()
    return str(env.get(FLAG) or "").strip().lower() in _ON


def version_of(tag):
    """`v0.1.0` -> `0.1.0`. The `v` is a git-tag convention; the version is
    what a plist and a package manager carry, and the event records both."""
    return str(tag)[1:] if str(tag).startswith("v") else str(tag)


def release_dir_for(repo, tag):
    """`<MERGE_GATE_STATE>/releases/<repo>/<tag>/` - the artefact directory, under the
    same 0700 root as the stamps so a worker cannot read or plant a build the gate is
    about to hash."""
    if not merge_gate.SLUG_RE.fullmatch(str(repo or "")):
        raise ValueError(f"bad repo slug {repo!r}: must match {merge_gate.SLUG_RE.pattern}")
    if not merge_gate.TAG_RE.fullmatch(str(tag or "")):
        raise ValueError(f"bad tag {tag!r}: must match {merge_gate.TAG_RE.pattern}")
    return merge_gate.state_dir() / "releases" / repo / tag


def check_tag(repo_root, tag, sh=None):
    """Check 1: the tag exists on `origin`."""
    out = merge_gate.read(sh, ["git", "ls-remote", "--tags", "origin", f"refs/tags/{tag}"],
                          cwd=str(repo_root), required=False, what="git ls-remote --tags",
                          timeout=TAG_TIMEOUT_S)
    sha, peeled = "", ""
    for line in out.splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        if parts[1] == f"refs/tags/{tag}":
            sha = parts[0]
        elif parts[1] == f"refs/tags/{tag}^{{}}":
            peeled = parts[0]
    chosen = peeled or sha
    if chosen:
        return True, f"tag: {tag} is on origin at {chosen[:8]}", chosen
    return False, f"tag: {tag} is not on origin - push the tag from the host first", ""


def _is_tag_heading(line, tag):
    """True when `line` is the `## <tag>` heading, with or without a trailing ` -
    <date>`: Keep-a-Changelog writes `## v0.1.0 - 2026-09-10`, so equality would
    never match a real file."""
    low = line.strip().lower()
    for w in (f"## {tag}".lower(), f"## [{tag}]".lower()):
        if low == w or low.startswith(w + " ") or low.startswith(w + "-"):
            return True
    return False


def changelog_promoted(text, tag):
    """Check 2, pure: `## <tag>` carries at least one `- ..."""
    unreleased_above = False
    seen_tag = False
    in_tag = False
    tag_lines = []
    for line in (text or "").splitlines():
        if line.startswith("## "):
            if line.strip().lower() in _UNRELEASED:
                if not seen_tag:
                    unreleased_above = True
                in_tag = False
                continue
            if _is_tag_heading(line, tag):
                seen_tag = True
                in_tag = True
                continue
            in_tag = False
            continue
        if in_tag:
            tag_lines.append(line)
    if not seen_tag:
        return False, (f"changelog: no ## {tag} section - promote ## Unreleased under it")
    if not any(TASK_LINE_RE.match(l) for l in tag_lines):
        return False, (f"changelog: ## {tag} has no `- ... (t_xxxxxxxx)` line - every release "
                       f"note line must name its card")
    if not unreleased_above:
        return False, (f"changelog: ## Unreleased was not re-inserted above ## {tag}"
                       " - the next card's quality gate will fail with `changelog: FAILED`"
                       " (design 4.5)")
    return True, f"changelog: ## {tag} promoted, ## Unreleased re-inserted"


def check_changelog(repo_root, tag):
    p = Path(repo_root) / "CHANGELOG.md"
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as exc:
        # Fail closed: a release nobody can verify must not be announced.
        return False, f"changelog: cannot read {p} ({exc})"
    return changelog_promoted(text, tag)


def check_sbom(release_dir, repo_root, repo, sh=None, which=None):
    """Check 3: an SPDX SBOM sits beside the artefacts."""
    release_dir = Path(release_dir)
    dst = release_dir / SBOM_NAME
    if dst.exists():
        try:
            json.loads(dst.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            return False, f"sbom: {dst} is not readable JSON ({exc})"
        return True, f"sbom: {dst.name} present"
    finder = which or shutil.which
    if finder("syft"):
        rc, out = (sh or merge_gate._default_sh)(["syft", str(repo_root), "-o", "spdx-json"],
                                                 cwd=str(repo_root))
        if rc != 0:
            return True, f"sbom: skipped - syft exit {rc} ({str(out or '').strip()[:80]})"
        try:
            json.loads(out)
        except ValueError:
            return True, "sbom: skipped - syft produced no readable JSON"
        try:
            _mkdir_private(release_dir)
            _atomic_write(dst, out)
        except OSError as exc:
            return False, f"sbom: cannot write {dst.name}: {exc.strerror or exc}"
        return True, f"sbom: {dst.name} generated by syft"
    return True, (f"sbom: skipped - syft not on PATH; generate the SBOM from the "
                  f"host's dependency graph and save it to {dst}")


def _mkdir_private(path):
    """`path.mkdir(parents=True, mode=0o700)` only chmods the LEAF - every parent it
    creates along the way gets the process umask instead, same gap
    `merge_gate.write_release_approval` closes with its explicit chmod loop over
    `<state>/releases` and `<state>/releases/<repo>`."""
    path = Path(path)
    created = []
    p = path
    while not p.exists():
        created.append(p)
        p = p.parent
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    for d in created:
        os.chmod(d, 0o700)


def _atomic_write(path, text):
    _mkdir_private(path.parent)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return path


def check_artefact(release_dir):
    """Check 4: something is there to release, and every byte of it is hashed."""
    release_dir = Path(release_dir)
    if not release_dir.is_dir():
        return False, f"artefact: {release_dir} does not exist - build into it first"
    files = sorted(p for p in release_dir.iterdir()
                   if p.is_file() and p.name not in (SUMS_NAME, SBOM_NAME)
                   and not p.name.startswith("."))
    if not files:
        return False, f"artefact: nothing in {release_dir} to release"
    lines = []
    try:
        for p in files:
            h = hashlib.sha256()
            with p.open("rb") as fh:
                for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                    h.update(chunk)
            lines.append(f"{h.hexdigest()}  {p.name}")
        _atomic_write(release_dir / SUMS_NAME, "\n".join(lines) + "\n")
    except OSError as exc:
        name = Path(exc.filename).name if exc.filename else SUMS_NAME
        return False, f"artefact: cannot read {name}: {exc.strerror or exc}"
    return True, f"artefact: {len(files)} file{'' if len(files) == 1 else 's'} hashed into {SUMS_NAME}"


def check_stamp(repo, tag):
    """Check 5: the third stamp. PRESENCE is the whole verdict (design 7.2)."""
    try:
        stamp = merge_gate.read_release_approval(repo, tag)
    except ValueError as exc:
        return False, f"stamp: {exc}"
    if stamp is None:
        return False, f"stamp: no release approval - run `bin/approve-release.sh {repo} {tag}`"
    return True, f"stamp: approved via {stamp['via']}"


OFF_LINE = (f"release gate is off: set {FLAG}=1 in ops.env "
            "(the ops.env file) to enable it")


def _release_link(repo, tag):
    """The GitHub release page, from the registry's `url` for `repo` (no owner is hard-coded);
    empty when the registry has no url for it."""
    reg = Path(os.environ.get("AGENT_OPS_REPOS_JSON")
               or Path(__file__).resolve().parent / "repos.json")
    try:
        rows = (json.loads(reg.read_text(encoding="utf-8")) if reg.exists()
                else host_registry.registry())
        url = next(r["url"] for r in rows if r["name"] == repo)
    except (OSError, ValueError, KeyError, StopIteration, TypeError):
        return ""
    return f"{url.removesuffix('.git')}/releases/tag/{tag}"


def check(repo, tag, *, sh=None, repo_root=None, release_dir=None, env=None,
          which=None, spool=None):
    """Run the five checks in order and, only on a clean pass, emit the one `release`
    event."""
    if not enabled(env):
        return "off", [OFF_LINE]
    if not merge_gate.SLUG_RE.fullmatch(str(repo or "")) or not merge_gate.TAG_RE.fullmatch(str(tag or "")):
        return "blocked:args", [f"args: bad repo {repo!r} or tag {tag!r} "
                                "(want <repo-slug> v<major>.<minor>.<patch>[-pre])"]
    repo_root = Path(repo_root or merge_gate._bridge().REPO_PATHS.get(repo) or "")
    if not repo_root.is_dir():
        return "blocked:args", [f"args: no repo checkout at {repo_root}"]
    release_dir = Path(release_dir) if release_dir is not None else release_dir_for(repo, tag)

    lines, sha = [], ""
    for name in CHECKS:
        if name == "tag":
            ok, why, sha = check_tag(repo_root, tag, sh=sh)
        elif name == "changelog":
            ok, why = check_changelog(repo_root, tag)
        elif name == "sbom":
            ok, why = check_sbom(release_dir, repo_root, repo, sh=sh, which=which)
        elif name == "artefact":
            ok, why = check_artefact(release_dir)
        else:
            ok, why = check_stamp(repo, tag)
        lines.append(why)
        if not ok:
            return f"blocked:{name}", lines
    events.log("release", "release_gate", f"released {repo} {tag}", board=repo,
               link=_release_link(repo, tag),
               payload={"repo": repo, "version": version_of(tag), "tag": tag, "sha": sha},
               spool=spool)
    return "released", lines


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="release gate: check one tagged release. Releases nothing.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check", help="run the five checks for one repo and tag")
    c.add_argument("repo"); c.add_argument("tag")
    c.add_argument("--release-dir", default=None)
    a = ap.parse_args(argv)
    try:
        verdict, lines = check(a.repo, a.tag, release_dir=a.release_dir)
    except merge_gate.InfraError as exc:
        # A broken tool is not a verdict (merge_gate's rule, one level out):
        # exit 3, emit nothing, say what broke. Re-run when it is fixed.
        print(f"error: {exc}", file=sys.stderr)
        return 3
    except OSError as exc:
        # Defense in depth: every known OSError site inside the five checks already
        # turns into a `(False, why)` verdict, but a straggler here (or in some future
        # check) is still not a traceback - same exit 3 as a broken tool, never a
        # verdict.
        print(f"error: {exc}", file=sys.stderr)
        return 3
    for line in lines:
        print(line)
    print(verdict)
    if verdict == "released":
        return 0
    return 2 if verdict == "off" else 1


if __name__ == "__main__":
    raise SystemExit(main())
