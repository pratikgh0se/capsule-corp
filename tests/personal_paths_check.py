#!/usr/bin/env python3
"""No personal paths in shipped code (PROD-11): fails when a tracked file under capsule/,
maintenance/, bin/, docker/, tests/, observability/, deploy.sh or bootstrap.sh (not *.md)
holds the maintainer's home path or GitHub owner (see PATTERN). Reads `git ls-files` only.

release/personal-allow.txt lines: `<path-glob>  <YYYY-MM-DD>  <reason>` (`#` and blank lines
are skipped). A malformed line, or an entry that no hit matches any more, fails the check."""
import argparse
import fnmatch
import re
import subprocess
import sys
from datetime import date
from pathlib import Path

PATTERN = re.compile("/Users/" + "pratik" + "|" + "pratik" + "gh0se")  # split: not a self-hit
SCOPE = ("capsule/", "maintenance/", "bin/", "docker/", "tests/", "observability/")
SCOPE_FILES = ("deploy.sh", "bootstrap.sh")
ALLOW = "release/personal-allow.txt"
ENTRY = re.compile(r"^(\S+)\s+(\d{4}-\d{2}-\d{2})\s+(\S.*)$")


def _tracked(repo):
    out = subprocess.run(["git", "ls-files", "-z"], cwd=repo, capture_output=True, text=True,
                         check=True).stdout
    return [p for p in out.split("\0") if p and not p.endswith(".md")
            and (p.startswith(SCOPE) or p in SCOPE_FILES)]


def _allow(repo):
    """(globs, problems) from the allowlist file; a missing file is an empty list."""
    path = Path(repo) / ALLOW
    globs, problems = [], []
    for n, line in enumerate(path.read_text().splitlines() if path.exists() else [], 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        m = ENTRY.match(line)
        try:
            date.fromisoformat(m.group(2)) if m else None
        except ValueError:
            m = None
        if m:
            globs.append(m.group(1))
        else:
            problems.append(f"allowlist-bad {ALLOW}:{n}")
    return globs, problems


def _scan(repo):
    """Report lines: unlisted hits, bad allowlist lines, allowlist entries nothing matches."""
    globs, report = _allow(repo)
    used = set()
    for rel in _tracked(repo):
        try:
            text = (Path(repo) / rel).read_text(errors="replace")
        except OSError:
            continue
        hit = [n for n, line in enumerate(text.splitlines(), 1) if PATTERN.search(line)]
        listed = [g for g in globs if fnmatch.fnmatchcase(rel, g)]
        if not listed:
            report += [f"personal-path {rel}:{n}" for n in hit]
        elif hit:
            used.update(listed)
    report += [f"allowlist-stale {g}" for g in globs if g not in used]
    return report


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repo", default=str(Path(__file__).resolve().parents[1]))
    report = _scan(ap.parse_args(argv).repo)
    print("\n".join(report) if report else "personal_paths_check: ok")
    return 1 if report else 0


if __name__ == "__main__":
    sys.exit(main())
