"""Which repo holds one incident's fix: the board's `korin.main_checkout`, else the Capsule repo.
A fix for an app board can land in Capsule's own code (D26). Nothing is stored: the repo is found
again each time from the fix sha, which `incidents landed` keeps in the incident's evidence."""
import re
import subprocess
from pathlib import Path

from capsule import paths

LANDED_RE = re.compile(r"done at ([0-9a-f]{40}) by ")


def _git(checkout, *args):
    try:
        res = subprocess.run(["git", "-C", str(Path(checkout).expanduser()), *args],
                             capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return 1, ""
    return res.returncode, res.stdout.strip()


def has_commit(checkout, sha):
    return _git(checkout, "rev-parse", "--verify", "-q", f"{sha}^{{commit}}")[0] == 0


def _repos(primary):
    return [Path(primary).expanduser()] + [r for r in [paths.REPO]
                                           if r != Path(primary).expanduser()]


def pick(primary, sha):
    """The first of `primary`, then the Capsule repo, that holds `sha`; ValueError names both."""
    repos = _repos(primary)
    for repo in repos:
        if has_commit(repo, sha):
            return repo
    raise ValueError(f"commit {sha!r} not found in " + " or ".join(str(r) for r in repos))


def base_for(incident, checkout):
    """The incident's `main_sha`; when `checkout` holds no such commit (it was stamped in the other
    repo), the last main commit in `checkout` before the incident opened."""
    base = incident.get("main_sha")
    if not base or has_commit(checkout, base) or not incident.get("opened_at"):
        return base
    rc, out = _git(checkout, "rev-list", "-1", "--first-parent",
                   f"--before={int(incident['opened_at'])}", "main")
    return out if rc == 0 and out else base


def gate(incident, primary):
    """(checkout, base) the recurrence proof uses for this incident: the repo of the fix sha that
    `landed` recorded in the evidence (else `primary`), and the base in that repo (None when that
    repo does not hold it: the novelty proof then refuses)."""
    found = LANDED_RE.findall(str(incident.get("evidence") or ""))
    try:
        checkout = pick(primary, found[-1]) if found else primary
    except ValueError:
        checkout = primary
    base = base_for(incident, checkout)
    return checkout, base if base and has_commit(checkout, base) else None
