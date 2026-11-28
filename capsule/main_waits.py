"""A commit wait that ends by itself: a card held by `capsule human <board> <task> wait <sha>` goes
back to the queue once the commit is in the board repo's main (inc_94760910). `blocks.wait` writes
the hold; the tick calls `release_and_promote`. An epic wait is not released here: a person's
`reset` lifts it (an epic can already sit in main)."""
import sqlite3
import subprocess
import sys
from pathlib import Path

from capsule import blocks, config
from capsule.db import event, txn


def _git(repo, *args, timeout=30):
    """(ok, stdout) of one git call in `repo`; ok is None on a timeout or OS error."""
    try:
        done = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                              timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None, ""
    return done.returncode == 0, done.stdout.strip()


def release(conn, cfg, *, now):
    """Lift every sha wait whose commit is in the board repo's main branch. The local `repo_url`
    holds every object, so it is read in place: no fetch, no copy. A git error keeps the card
    waiting; a sha that names no commit, or a URL `repo_url`, notifies once."""
    rows = [r for r in conn.execute(
        "SELECT id, waits_on_branch FROM tasks WHERE status='todo' AND block_kind='dependency'"
        " AND waits_on_branch IS NOT NULL").fetchall() if blocks.WAIT_SHA.fullmatch(r[1])]
    repo = Path(str(cfg.get("repo_url"))).expanduser()
    ref = "refs/heads/" + ((cfg.get("lander") or {}).get("branch") or "main")
    released = []
    for task_id, sha in rows:
        try:    # one bad card never stops the tick or the other waits
            if _check(conn, repo, ref, task_id, sha, now):
                released.append(task_id)
        except (ValueError, sqlite3.Error) as exc:
            print(f"capsule: sha wait {task_id} not checked: {exc}", file=sys.stderr)
    return released


def _check(conn, repo, ref, task_id, sha, now):
    """True when the card's hold was lifted."""
    if not repo.is_dir():
        _unknown(conn, task_id, sha, now, "sha wait needs a local repo_url")
        return False
    found = _git(repo, "rev-parse", "--verify", "-q", f"{sha}^{{commit}}")[0]
    if found is False:
        _unknown(conn, task_id, sha, now, f"{sha} names no commit in the board repo")
    return bool(found) and bool(_git(repo, "merge-base", "--is-ancestor", sha, ref)[0]) \
        and _lift(conn, repo, ref, task_id, sha, now)


def _lift(conn, repo, ref, task_id, sha, now):
    ok, main = _git(repo, "rev-parse", "--verify", ref)
    if not ok:
        return False
    with txn(conn):
        moved = conn.execute(
            "UPDATE tasks SET block_kind=NULL, waits_on_branch=NULL WHERE id=? AND status='todo'"
            " AND block_kind='dependency' AND waits_on_branch=?", (task_id, sha)).rowcount == 1
        if moved:
            event(conn, task_id, "main_wait_released", {"waits_on": sha, "main": main}, now=now)
    return moved


def _unknown(conn, task_id, sha, now, why):
    """The wait cannot be checked: say so once per sha, keep waiting."""
    seen = conn.execute("SELECT 1 FROM task_events WHERE task_id=? AND kind='main_wait_unknown'"
                        " AND json_extract(payload,'$.waits_on')=?", (task_id, sha)).fetchone()
    if not seen:
        with txn(conn):
            event(conn, task_id, "main_wait_unknown", {"waits_on": sha}, now=now)
            blocks._notify(conn, task_id, "sha wait cannot be checked",
                           f"{why}; the card keeps waiting", "reset", now)


def release_and_promote(conn, cfg, now):
    """The tick's one call: lift the waits that ended, then `blocks.promote_ready`."""
    release(conn, cfg, now=now)
    return blocks.promote_ready(conn, branches=config.epic_branches(cfg), now=now)
