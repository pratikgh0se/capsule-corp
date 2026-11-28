#!/usr/bin/env python3
"""Linux-shape lint for the pre-merge gate (LAND-07, ledger row 171): a line the range adds
to tests/*.sh, bin/* or maintenance/*.sh that uses a macOS-only construct (`stat -f`,
`sed -i ''`, `date -j`, `pbcopy`, `sw_vers`, `/usr/bin/open`) fails unless it is guarded:
that same line carries a GNU alternative (`stat -c`), a `uname` check or a `# macos-only`
marker, or the line above is a `uname ... then`/`&&` or `Darwin` check. Prints file:line;
GitHub's Linux `tests` job runs these scripts. Deleting a guard above an existing `stat -f`
is not caught: only added lines are linted."""
import argparse
import re
import subprocess
import sys

SCOPE = re.compile(r"^(tests/[^/]+\.sh|bin/[^/]+|maintenance/[^/]+\.sh)$")
MACOS_ONLY = re.compile(r"""\bstat\s+-f\b|\bsed\s+-i\s+(''|"")|\bdate\s+-j\b|\bpbcopy\b"""
                        r"""|\bsw_vers\b|/usr/bin/open\b""")
GUARD_SAME_LINE = re.compile(r"\bstat\s+-c\b|\buname\b|#\s*macos-only")
GUARD_LINE_ABOVE = re.compile(r"\buname\b.*(\bthen\b|&&)|\bDarwin\b")
HUNK = re.compile(r"^@@ -\S+ \+(\d+)(?:,(\d+))? @@")


def added_lines(repo, base, head, path):
    """(line numbers the range adds to `path`, every line of `path` at head)."""
    show = subprocess.run(["git", "show", f"{head}:{path}"], cwd=repo, capture_output=True,
                          text=True)
    diff = subprocess.run(["git", "diff", "-U0", f"{base}..{head}", "--", path], cwd=repo,
                          capture_output=True, text=True)
    if show.returncode != 0 or diff.returncode != 0:
        raise RuntimeError(f"git failed for {path}: {(show.stderr or diff.stderr).strip()}")
    lines = show.stdout.splitlines()
    added = set()
    for h in filter(None, map(HUNK.match, diff.stdout.splitlines())):
        start, count = int(h.group(1)), int(h.group(2) or 1)
        added.update(range(start, start + count))
    return added, lines


def findings(repo, base, head, path):
    """One report row per added line of `path` that is macOS-only and unguarded."""
    added, lines = added_lines(repo, base, head, path)
    hits = [n for n in sorted(added) if MACOS_ONLY.search(lines[n - 1])]
    return [f"  {path}:{n}: macOS-only construct without a GNU/uname guard: {lines[n - 1].strip()}"
            for n in hits
            if not GUARD_SAME_LINE.search(lines[n - 1])
            and not (n > 1 and GUARD_LINE_ABOVE.search(lines[n - 2]))]


def check(repo, base, head):
    names = subprocess.run(["git", "diff", "--name-only", "--diff-filter=ACMR", f"{base}..{head}"],
                           cwd=repo, capture_output=True, text=True)
    if names.returncode != 0:
        return False, f"portability_lint: git diff failed: {names.stderr.strip()}"
    files = [p for p in names.stdout.split("\n") if SCOPE.match(p)]
    try:
        hits = [h for path in files for h in findings(repo, base, head, path)]
    except RuntimeError as e:
        return False, f"portability_lint: {e}"
    if hits:
        return False, "\n".join(["portability_lint: Linux-unsafe lines added:", *hits])
    return True, f"portability_lint: ok ({len(files)} changed script(s))"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base", required=True)
    ap.add_argument("--head", required=True)
    ap.add_argument("--repo", default=".")
    a = ap.parse_args(argv)
    ok, report = check(a.repo, a.base, a.head)
    print(report)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
