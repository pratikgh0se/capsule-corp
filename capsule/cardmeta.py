"""A board card's own `builds: <repo>:<branch>` line (row 492): names the branch a worker is
building the card on, so `capsule.unblocked` can read that branch's `owners.json` heartbeat
as ownership. Its own module so the parser can be tested apart from the detector."""
import re

BUILDS_RE = re.compile(r"(?im)^\s*builds:\s*(.+?)\s*$")


def parse_builds(text):
    """`(repo, branch)` from `text`'s last `builds: <repo>:<branch>` line (mirrors
    `unblocked.parse_waits`'s "last match wins"), split on the first `:`; `None` when there is
    no such line, or the line has no `:` or an empty repo/branch on either side of it."""
    if not text:
        return None
    matches = BUILDS_RE.findall(text)
    if not matches:
        return None
    repo, sep, branch = matches[-1].partition(":")
    repo, branch = repo.strip(), branch.strip()
    return (repo, branch) if sep and repo and branch else None
