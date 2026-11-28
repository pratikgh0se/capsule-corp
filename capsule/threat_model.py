"""`python3 -m capsule threat-model check [--repo R]` (M173, SEC-10): does the repo's
`docs/threat-model.md` carry assets, entry points and accepted risks. Reads that one file only.

A valid file has `## Assets`, `## Entry points` and `## Accepted risks`, each once. The first
two hold at least one `- ` bullet. Every non-blank line of the third is a `- ` bullet naming a
decision (lowercase `row N` or `DN`), or its only content is exactly `none recorded`. Lines in
``` fences are neither headings nor bullets. `check()` is also what `capsule doctor --repo` calls.
"""
import re
from pathlib import Path

REL = Path("docs") / "threat-model.md"
HEADINGS = ("Assets", "Entry points", "Accepted risks")
DECISION = re.compile(r"\brow \d+\b|\bD\d+\b")


def _sections(text):
    """({heading: [item]}, [duplicate headings]). An item is a `- ` bullet joined with its
    indented continuation lines, or any other non-blank line on its own."""
    found, dups, current, fenced = {}, [], None, False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            fenced = not fenced
        elif fenced or not line.strip():
            continue
        elif line.startswith("## "):
            name = line[3:].strip()
            if name in found:
                dups.append(name)
            current = found.setdefault(name, [])
        elif current is not None:
            if line[0].isspace() and current and current[-1].startswith("- "):
                current[-1] += " " + line.strip()      # a wrapped bullet is one bullet
            else:
                current.append(line.strip())
    return found, dups


def _risk_problems(items):
    """Problem texts for the accepted-risks items; none for `none recorded` alone."""
    if items == ["none recorded"]:
        return []
    out = [f"accepted risk is not a `- ` bullet: {s[:80]}" for s in items
           if not s.startswith("- ")]
    return out + [f"accepted risk names no decision (lowercase `row N` or `DN`): {s[:80]}"
                  for s in items if s.startswith("- ") and not DECISION.search(s)]


def check(repo="."):
    """One line per problem with `<repo>/docs/threat-model.md`; [] when it is valid. An empty
    `repo` means the current directory."""
    path = Path(repo or ".").expanduser() / REL
    try:
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError):
        return [f"{REL}: file missing or unreadable"]
    found, dups = _sections(text)
    problems = [f"duplicate heading: ## {name}" for name in dups]
    for name in HEADINGS:
        items = found.get(name)
        if items is None:
            problems.append(f"heading missing: ## {name}")
        elif not items:
            problems.append(f"section empty: ## {name}")
        elif name == "Accepted risks":
            problems += _risk_problems(items)
        elif not any(s.startswith("- ") for s in items):
            problems.append(f"section has no bullet: ## {name}")
    return [f"{REL}: {p}" for p in problems]


def add_parser(sub):
    p = sub.add_parser("threat-model", help="check a repo's docs/threat-model.md (M173)")
    tsub = p.add_subparsers(dest="tm_cmd", required=True)
    check_p = tsub.add_parser("check", help="exit 1 with one line per problem")
    check_p.add_argument("--repo", default=".", help="the repo to check (default: this directory)")


def handle(args, parser):
    problems = check(args.repo)
    print("\n".join(problems) if problems else "threat model ok")
    return 1 if problems else 0
