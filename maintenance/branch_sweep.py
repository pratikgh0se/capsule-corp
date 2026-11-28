#!/usr/bin/env python3
"""Landed-branch sweep (decision row 643): remove local keeper/* and build/* branches whose work
is on origin/main, and the capsule-corp-* work folders that hold them. Python replacement for
the by-hand branch and worktree sweeps; nothing here is ever forced.

Landed (squash merges break ancestry): the branch tip is an ancestor of origin/main, or
`git merge-tree --write-tree origin/main <tip>` is clean and its tree equals origin/main's tree
(merging it changes nothing). Only branches under PREFIXES are candidates. Kept: main, epic/*,
wip/*, anything with "salvage" in the name, any branch named in the owners registry
($CAPSULE_HOME/queue/<repo>/owners.json) or on any card of any board (tasks.branch_name and
epic_assigned events, read-only), any branch checked out in a folder that is kept.
A work folder is removed only when it is under <repo>-*, not locked, has no changes
(untracked files count), is not on a protected branch, is not some process's current directory
(lsof; lsof failing refuses the run; no lsof installed keeps every folder), and its branch
(or detached HEAD) is landed:
`git worktree remove <path>`, never forced, after a re-check of its HEAD and status. Fresh work
stays: a branch whose newest reflog entry, or a folder whose HEAD reflog, is under 24h old or
missing. No kanban.db under the boards dir refuses the run (card protection unprovable).
Stale entries are reported; `git worktree prune` is never run. A branch goes by
`git update-ref -d refs/heads/<name> <sha>` (compare and delete) after its folder; each removal
is verified. --apply logs a "starting" JSON line before each action and a result line after
(what, id, branch, before, result) to <maintenance state root>/branch-sweep.jsonl, and
`git branch <name> <sha>` restores a branch. Without --apply nothing changes. Local only;
remotes untouched.
"""
import argparse
import functools
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import board_fence   # noqa: E402  capsule_boards_dir()
import events   # noqa: E402  state_root(): the one maintenance state dir
from capsule import paths   # noqa: E402  queue_dir(): where owners.json lives

PREFIXES = ("keeper/", "build/")
NEVER = ("main", "epic/", "wip/", "salvage")      # name starts with one of these ...
LOG = "branch-sweep.jsonl"
BASE = "origin/main"
FRESH_S = 24 * 3600      # a branch or folder touched in the last day is someone's live work
# Ignored paths that are only rebuildable caches; any other ignored path (.env, a DB) keeps it.
CACHE_OK = ("__pycache__/", "*.pyc", ".pytest_cache/", ".ruff_cache/", ".mypy_cache/",
            ".hypothesis/")


def git(cwd, *args):
    r = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, timeout=120)
    return r.returncode, r.stdout.strip()


@functools.cache
def _landed(repo, sha, tree):
    """How a commit is landed on origin/main, or None."""
    if git(repo, "merge-base", "--is-ancestor", sha, BASE)[0] == 0:
        return "ancestor of origin/main"
    rc, out = git(repo, "merge-tree", "--write-tree", BASE, sha)
    if rc == 0 and out.splitlines()[:1] == [tree]:
        return "merging it into origin/main changes nothing"
    return None


def _owners(repo):
    f = paths.queue_dir(repo.name) / "owners.json"
    if not f.exists():
        return set()
    doc = json.loads(f.read_text())
    if not isinstance(doc, dict):
        raise RuntimeError(f"{f} is not an object")
    return set(doc)


def _card_branches():
    """Every branch any card of any board names (read-only). A board it cannot read refuses."""
    names, dbs = set(), sorted(board_fence.capsule_boards_dir().glob("*/kanban.db"))
    if not dbs:
        raise RuntimeError("no kanban.db found under the boards dir (wrong CAPSULE_HOME?); "
                           "card protection cannot be checked")
    for db in dbs:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=10)
        try:
            names |= {b for (b,) in con.execute("select branch_name from tasks")}
            names |= {_epic_branch(p) for (p,) in con.execute(
                "select payload from task_events where kind='epic_assigned'")}
        finally:
            con.close()
    return names


def _protected(repo):
    """Branches named by the owners registry or any card; re-read before each removal."""
    return _owners(repo) | _card_branches()


def _epic_branch(raw):
    try:
        return json.loads(raw)["branch"]
    except (TypeError, ValueError, KeyError):
        return None


def _worktrees(repo):
    rc, out = git(repo, "worktree", "list", "--porcelain")
    if rc:
        raise RuntimeError("git worktree list failed")
    return [dict(ln.partition(" ")[::2] for ln in blk.splitlines()) for blk in out.split("\n\n")]


def _guarded(name, protected):
    return name.startswith(NEVER) or "salvage" in name or name in protected


def _cache(name):
    """True for an ignored path that is only a rebuildable cache (CACHE_OK)."""
    return any(f"/{p}" in f"/{name}" if p.endswith("/") else name.endswith(p[1:])
               for p in CACHE_OK)


def _dirty(path):
    """Why a folder is not provably disposable, or None: any change or untracked file (always
    --untracked-files=all, so status.showUntrackedFiles=no cannot hide one), or an ignored
    path (.env, a local DB, ...) that is not a cache."""
    rc, out = git(path, "status", "--porcelain", "--ignored", "--untracked-files=all")
    if rc:
        return "status failed"
    rows = [ln.split(None, 1) for ln in out.splitlines()]     # git() strips the first line's pad
    if any(code != "!!" for code, _ in rows):
        return "has changes"
    loose = [name for _, name in rows if not _cache(name)]
    return f"ignored files present: {loose[0]}" if loose else None


def _cwds():
    """Every directory some process is currently in. lsof exits 1 when it cannot read some
    process (normal on macOS), so rc 1 with output is accepted; a failure with no output refuses
    the whole run. No lsof at all (a Linux container), or one killed by a signal or exiting with
    any rc but 0 or 1, returns None: no folder can be checked, so every work folder is kept."""
    try:
        r = subprocess.run(["lsof", "-d", "cwd", "-Fn"], capture_output=True, text=True,
                           timeout=120)
    except FileNotFoundError:
        return None
    if r.returncode not in (0, 1):
        return None
    if r.returncode and not r.stdout:
        raise RuntimeError("lsof failed; cannot tell which folders are in use")
    return {ln[1:] for ln in r.stdout.splitlines() if ln.startswith("n")}


def _fresh(cwd, ref):
    """Why new work stays: the newest reflog entry of `ref` is under FRESH_S old, or there is no
    reflog to prove its age (None when it is old enough)."""
    rc, out = git(cwd, "reflog", "show", "--date=unix", "-n1", "--format=%gd", ref)
    stamp = re.search(r"@\{(\d+)\}", out) if rc == 0 else None
    if not stamp:
        return "no reflog to prove its age"
    return "touched under 24h ago" if time.time() - int(stamp.group(1)) < FRESH_S else None


def _wt_static(repo, wt, protected):
    path, branch = wt["worktree"], wt.get("branch", "").removeprefix("refs/heads/")
    real = Path(os.path.realpath(path))        # a symlink or `..` cannot lead outside or inside
    if real.parent != repo.parent or not real.name.startswith(f"{repo.name}-"):
        return f"not under {repo.name}-*"
    for flag, why in (("locked", "locked"), ("prunable", "stale entry; git worktree prune "
                                            "is not run here")):
        if flag in wt:
            return why
    if branch and _guarded(branch, protected):
        return f"branch {branch} is protected, owned or on a card"
    return None


def _wt_keep(repo, tree, wt, protected, cwds):
    """Why a work folder stays, or None to remove it."""
    path = wt["worktree"]
    why = _wt_static(repo, wt, protected)
    if why or (why := _fresh(path, "HEAD")):
        return why
    if cwds is None:
        return "lsof not available: kept"
    real = os.path.realpath(path)             # cwds are already resolved the same way
    if any(c == real or c.startswith(real + "/") for c in cwds):
        return "a process is working in it"
    if why := _dirty(path):
        return why
    return None if _landed(repo, wt["HEAD"], tree) else "not landed"


def _plan(repo, tree, protected, cwds):
    """(items to remove, [(kind, id, reason)] kept). Folders first, then branches."""
    wts, items, kept = _worktrees(repo), [], []
    cwds = None if cwds is None else {os.path.realpath(c) for c in cwds}
    for wt in wts[1:]:
        why, path = _wt_keep(repo, tree, wt, protected, cwds), wt["worktree"]
        if why:
            kept.append(("worktree", path, why))
        else:
            items.append({"what": "worktree", "id": path, "before": wt["HEAD"], "needs": None,
                          "branch": wt.get("branch", "").removeprefix("refs/heads/") or None,
                          "why": f"clean, {_landed(repo, wt['HEAD'], tree)}"})
    held = {w["branch"].removeprefix("refs/heads/"): w["worktree"] for w in wts if "branch" in w}
    _plan_branches(repo, tree, protected, held, items, kept)
    return items, kept


def _plan_branches(repo, tree, protected, held, items, kept):
    removing = {i["id"] for i in items}
    out = git(repo, "for-each-ref", "--format=%(refname:short) %(objectname)", "refs/heads")[1]
    for name, sha in (ln.split() for ln in out.splitlines()):
        if not name.startswith(PREFIXES):
            continue
        skip = ("protected, owned or on a card" if _guarded(name, protected)
                else _fresh(repo, f"refs/heads/{name}"))
        how = None if skip else _landed(repo, sha, tree)
        if skip or not how:
            kept.append(("branch", name, skip or "not landed"))
        elif held.get(name, "") not in removing | {""}:
            kept.append(("branch", name, f"checked out in {held[name]}, which stays"))
        else:
            items.append({"what": "branch", "id": name, "before": sha, "needs": held.get(name),
                          "branch": name, "why": f"landed: {how}"})


def _do_worktree(repo, item):
    path = item["id"]
    if git(path, "rev-parse", "HEAD")[1] != item["before"] or _dirty(path):   # re-check now
        return "kept: its HEAD or files changed since the check"
    rc, out = git(repo, "worktree", "remove", path)
    gone = not Path(path).exists() and path not in {w["worktree"] for w in _worktrees(repo)}
    return "removed" if rc == 0 and gone else f"failed: {out or 'not verified gone'}"[:200]


def _do(repo, item):
    """Run one removal and verify it; returns the result string."""
    if item["branch"] and _guarded(item["branch"], _protected(repo)):    # re-read right now
        return "kept: claimed since the check"
    if item["what"] == "worktree":
        return _do_worktree(repo, item)
    rc, out = git(repo, "update-ref", "-d", f"refs/heads/{item['id']}", item["before"])
    gone = git(repo, "rev-parse", "--verify", "-q", f"refs/heads/{item['id']}")[0] != 0
    return "removed" if rc == 0 and gone else f"failed: {out or 'not verified gone'}"[:200]


def _row(log, item, result):
    log.write(json.dumps({"ts": int(time.time()), "what": item["what"], "id": item["id"],
                          "branch": item["branch"], "before": item["before"],
                          "result": result}) + "\n")
    log.flush()


def _apply(repo, items, log):
    results, removed = [], set()
    for item in items:
        if item["needs"] and item["needs"] not in removed:
            res = "skipped: its work folder was not removed"
        else:
            _row(log, item, "starting")           # written before the action, so a crash shows
            res = _do(repo, item)
        removed |= {item["id"]} if res == "removed" else set()
        _row(log, item, res)
        print(f"{res}: {item['what']} {item['id']} (was {item['before'][:12]})")
        results.append(res)
    return results


def _run(repo, apply):
    if apply and git(repo, "fetch", "-q", "origin")[0]:
        raise RuntimeError("could not fetch origin")
    rc, tree = git(repo, "rev-parse", f"{BASE}^{{tree}}")
    if rc:
        raise RuntimeError(f"{BASE} not found")
    items, kept = _plan(repo, tree, _protected(repo), _cwds())
    for kind, ident, why in kept:
        print(f"kept: {kind} {ident} ({why})")
    if not apply:
        for i in items:
            print(f"would remove {i['what']} {i['id']} (was {i['before'][:12]}; {i['why']})")
        print(f"branch-sweep: dry run, nothing changed; {len(items)} to remove, "
              f"{len(kept)} kept; --apply removes")
        return 0
    state = events.state_root()
    state.mkdir(parents=True, exist_ok=True)
    with open(state / LOG, "a") as log:
        results = _apply(repo, items, log)
    bad = [r for r in results if r.startswith("failed")]      # kept/skipped are not failures
    print(f"branch-sweep: applied: {results.count('removed')} removed, "
          f"{len(results) - results.count('removed')} not, "
          f"{len(kept)} kept; log {state / LOG}")
    return 1 if bad else 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--apply", action="store_true", help="remove (default: preview only)")
    ap.add_argument("--repo", default=str(Path.home() / "CodeBase" / "capsule-corp"))
    args = ap.parse_args(argv)
    try:
        return _run(Path(args.repo).resolve(), args.apply)
    except (OSError, RuntimeError, ValueError, sqlite3.Error, subprocess.SubprocessError) as e:
        print(f"branch-sweep: refused: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
