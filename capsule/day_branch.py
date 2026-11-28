"""A day-branch board's life (LAND-03, Ruling 1): the one place that fetches `origin/<branch>`,
bases a landing on it and catches it up with `origin/main`.

A branch that does not exist yet is not a wait: the card is based on `origin/main` and the push
creates the branch. A branch that lacks `origin/main` gets a merge commit of it, built in a
throwaway clone and fetched back as objects only; the landing's own push then fast-forwards the
branch over that merge (published history is never rebased, nothing is forced). A conflicting
merge waits."""
import tempfile
from pathlib import Path

from capsule import lander

MISSING = "couldn't find remote ref"


def fetch(repo, branch, timeout=180):
    """(rc, text): origin/main, and origin/<branch> too on a day board. A day branch origin does
    not have yet is no failure."""
    rc, out = lander._git(repo, "fetch", "--end-of-options", "origin", "main", timeout=timeout)
    if rc or branch == "main":
        return rc, out
    rc, out = lander._git(repo, "fetch", "--end-of-options", "origin", branch, timeout=timeout)
    if rc and MISSING in out:      # deleted (merged) or never made: drop the stale tracking ref
        lander._git(repo, "update-ref", "-d", f"refs/remotes/origin/{branch}")
        return 0, ""
    return rc, out


def base(repo, branch, merge=True):
    """(sha, status, detail): what a landing on `branch` replays onto. `pass` with the sha of
    origin/<branch> (or of origin/main when the branch is missing, or a merge of main into it);
    `wait` when the catch-up merge conflicts or git cannot say."""
    main = lander._git(repo, "rev-parse", "--verify", "-q", "refs/remotes/origin/main")[1]
    tip = lander._git(repo, "rev-parse", "--verify", "-q", f"refs/remotes/origin/{branch}")[1]
    if branch == "main" or not tip:
        return main, "pass", ""
    if not merge or not main or lander._git(repo, "merge-base", "--is-ancestor", main, tip)[0] == 0:
        return tip, "pass", ""
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp) / "w"
        if (lander._git(tmp, "clone", "-q", "--no-checkout", str(repo), str(work))[0]
                or lander._git(work, "fetch", "-q", "--end-of-options", str(repo), tip, main)[0]
                or lander._git(work, "checkout", "-q", "--detach", tip)[0]):
            return "", "wait", f"needs rebase onto origin/{branch}: could not catch up"
        rc, out = lander._git(work, *lander.IDENT, "merge", "--no-edit", "--no-ff", main)
        if rc:
            files = lander._git(work, "diff", "--name-only", "--diff-filter=U")[1].split()
            why = f"conflicts in {', '.join(files)}" if files else out[-200:]
            return "", "wait", f"needs rebase onto origin/{branch}: merging main {why}"
        merged = lander._git(work, "rev-parse", "HEAD")[1]
        if lander._git(repo, "fetch", "-q", "--end-of-options", str(work), merged)[0]:
            return "", "wait", f"needs rebase onto origin/{branch}: could not keep the merge"
    return merged, "pass", ""
