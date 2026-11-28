#!/usr/bin/env python3
"""Print the term of the private denylist's rule for the retired system (argv[1] == "retired").

Retirement-guard tests use it to name that system without writing its name in the tree.
The rules load through maintenance/public_lint.py's own loader; the retired system's is the one
whose category is none of the four known ones. When release/private-denylist.txt is absent
(public checkout) it prints nothing and exits 0: the caller skips its guard. Anything else wrong
(bad usage, a denylist the loader refuses, not exactly one such rule) exits non-zero, so a broken
denylist fails the caller loudly and a new category can never silently become "retired".
"""
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DENYLIST = ROOT / "release" / "private-denylist.txt"
KNOWN = ("personal-name", "launchd-prefix", "vault", "project")

if __name__ == "__main__":
    if sys.argv[1:] != ["retired"]:
        print("usage: denylist_term.py retired", file=sys.stderr)
        sys.exit(2)
    if not DENYLIST.is_file():
        print("SKIP: no private denylist (public checkout)", file=sys.stderr)
        sys.exit(0)
    path = ROOT / "maintenance" / "public_lint.py"
    spec = importlib.util.spec_from_file_location("public_lint", path)
    lint = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(lint)
    try:
        rules, _ = lint._load_denylist(DENYLIST)
    except (OSError, ValueError) as e:
        print(f"denylist_term: {e}", file=sys.stderr)
        sys.exit(1)
    retired = [regex for category, regex, _ in rules if category not in KNOWN]
    if len(retired) != 1:
        print(f"denylist has {len(retired)} rules outside {KNOWN}, want exactly 1", file=sys.stderr)
        sys.exit(1)
    print(retired[0])
