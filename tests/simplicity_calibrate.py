"""Report-only calibration for tests/simplicity_check.py (Z8, docs/ZENKAI.md, plan Task 5).

Runs the real check over each of the last N first-parent commits (parent..commit) and prints,
per rule, how many added lines it flagged and on which commits. Changes no gate, threshold or
baseline, reads only through git, and exits 0 unless the arguments are bad."""
import argparse
import collections
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import simplicity_check as simp  # noqa: E402

RULES = range(1, 8)
ROW = re.compile(r"^  (?P<path>.+?):(?P<line>\d+): rule (?P<rule>\d+): .* \[(?P<origin>\w+), "
                 r"(?P<sev>\w+)\]$")


def _git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True
                          ).stdout.strip()


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--head", default="HEAD")
    ap.add_argument("--last", type=int, default=20, help="how many commits (default 20)")
    ap.add_argument("--repo", default=".")
    a = ap.parse_args(argv)
    # distinct (rule, path, line) per commit: an ast and a ruff finding on one line count once
    per_rule = collections.defaultdict(list)
    errors = []
    for row in _git(a.repo, "log", "--first-parent", "--format=%h %s", f"-{a.last}",
                    a.head).splitlines():
        sha, subject = row.split(" ", 1)
        parents = _git(a.repo, "rev-list", "--parents", "-n", "1", sha).split()[1:]
        if not parents:
            continue
        ok, report = simp.check(a.repo, parents[0], sha)
        rows = [m for m in map(ROW.match, report.splitlines()) if m]
        errors += [sha] if not ok and not rows else []
        flagged = {(int(m["rule"]), m["path"], m["line"]) for m in rows}
        for rule, hits in collections.Counter(r for r, _, _ in flagged).items():
            per_rule[rule].append((sha, subject, hits))
    print(f"simplicity_calibrate: last {a.last} first-parent commit(s) up to {a.head} "
          "(report only)")
    for rule in RULES:
        hits = per_rule.get(rule, [])
        print(f"rule {rule}: {sum(h for _, _, h in hits)} flagged line(s) on {len(hits)} commit(s)")
        for sha, subject, h in hits:
            print(f"  {sha} {h} {subject}")
    if errors:
        print("check errors on: " + " ".join(errors))
    return 0


if __name__ == "__main__":
    sys.exit(main())
