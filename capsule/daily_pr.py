"""`python3 -m capsule pr daily B [--dry-run] [--capsule-home H]` (LAND-03, row 600, Ruling 1).

A product board lands each card on its own `lander.branch` (`capsule/daily` for a new board); the
harness board lands on main. Once a day this opens, or updates, the board's one draft pull request
from that branch into main through the GitHub CLI (`$CAPSULE_GH_BIN`, else `gh`); the owner
merges it.
The PR lists the task ids found in commits on origin/main..origin/<branch>, minus ids that
origin/main already names (a squash merge keeps them): git decides, the board's `landing` events
are a cross-check that only warns on stderr. Each created PR is one guard row, `daily_pr`, written
only after the CLI succeeded. Nothing here pushes, forces or touches main.
"""
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from capsule import day_branch, lander

GUARD = "daily_pr"
GITHUB = re.compile(r"(?:https://github\.com/|ssh://git@github\.com/|git@github\.com:)"
                    r"([\w.-]+/[\w.-]+?)(?:\.git)?/?")
TASK = re.compile(r"\bt_[0-9a-f]{8}\b")


def add_parser(sub):
    p = sub.add_parser("pr", help="pull requests for boards that land on their own branch")
    pr_sub = p.add_subparsers(dest="pr_cmd", required=True)
    dp = pr_sub.add_parser("daily", help="open or update the board's one draft PR into main")
    dp.add_argument("board")
    dp.add_argument("--dry-run", action="store_true", help="print the command; call gh to look")
    dp.add_argument("--capsule-home", default=None)


def _landed(conn, since):
    """{task_id: title} of cards with a `landing` event `landed` (or `already_on_main` recorded
    after a kill, which is our own push) after `since`, oldest first; a reverted card is dropped."""
    rows = conn.execute("SELECT e.task_id, t.title, e.payload FROM task_events e JOIN tasks t "
                        "ON t.id = e.task_id WHERE e.kind='landing' AND e.created_at > ? "
                        "ORDER BY e.id", (since,)).fetchall()
    cards = {}
    for task_id, title, payload in rows:
        doc = json.loads(payload or "{}")
        if doc.get("outcome") == "landed" or (doc.get("outcome") == "already_on_main"
                                              and doc.get("detail") == "recorded after a kill"):
            cards.setdefault(task_id, title)
        elif doc.get("outcome") == "reverted":
            cards.pop(task_id, None)
    return cards


def _git_cards(repo, branch):
    """Task ids in commits on origin/main..origin/<branch>, in landing order, minus any id that an
    origin/main commit message names (a revert commit's message does not count). The refs are
    refreshed first. Any git failure raises RuntimeError with git's message: a missing day branch
    is the one case that means no cards."""
    def git(*args):
        rc, out = lander._git(repo, *args, timeout=180)
        if rc:
            raise RuntimeError(f"git {args[0]} failed: {out[-300:]}")
        return out
    rc, out = day_branch.fetch(repo, branch)
    if rc:
        raise RuntimeError(f"git fetch failed: {out[-300:]}")
    if lander._git(repo, "rev-parse", "--verify", "-q", f"refs/remotes/origin/{branch}")[0]:
        return []
    main_log = git("log", "refs/remotes/origin/main", "--format=%B%x1e").split("\x1e")
    main_ids = {t for msg in main_log if "This reverts commit" not in msg
                for t in TASK.findall(msg)}
    log = git("log", "--reverse", "--format=%B%x1e",
              f"refs/remotes/origin/main..refs/remotes/origin/{branch}")
    return list(dict.fromkeys(t for t in TASK.findall(log) if t not in main_ids))


def _gh(args, repo, body=None):
    """(rc, output) of one GitHub CLI call. A github.com url becomes --repo OWNER/REPO; a local
    directory is the working directory. `body` goes in on stdin (`--body-file -`)."""
    cmd, cwd = [os.environ.get("CAPSULE_GH_BIN") or "gh", *args], None
    if match := GITHUB.fullmatch(repo):
        cmd += ["--repo", match.group(1)]
    else:
        cwd = repo
    try:
        done = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=120,
                              input=body or "")
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 127, f"could not run {cmd[0]}: {exc}"
    return done.returncode, (done.stdout if done.returncode == 0 else done.stderr
                             or done.stdout).strip()


def _open_pr(repo, branch):
    """(number or None, error text or None) of the open PR whose head is `branch`."""
    rc, out = _gh(["pr", "list", "--state", "open", "--head", branch, "--json", "number,url"],
                  repo)
    if rc:
        return None, out
    try:
        found = json.loads(out or "[]")
    except ValueError:
        return None, f"unreadable reply from the GitHub CLI: {out[:200]}"
    return (found[0]["number"] if found else None), None


def _open_db(board, home):
    """The board's DB with its schema (a fresh board has none yet)."""
    from capsule import db, paths
    path = paths.board_db(board, home)
    if path.is_file():
        probe = db.connect(path)
        has = probe.execute("SELECT 1 FROM sqlite_master WHERE name='guard_state'").fetchone()
        probe.close()
        if has:
            return db.connect(path)
    db.create_board(path)
    return db.connect(path)


def _board(board, home):
    """(branch, repo, None) of a board that lands on a day branch, else (None, None, exit code)
    after saying why."""
    from capsule import config, paths
    cfg_path = paths.config_path(home, board)
    if not paths.board_dir(board, home).is_dir() or not cfg_path.is_file():
        print(f"capsule pr daily: no board {board!r} under {home}/boards", file=sys.stderr)
        return None, None, 1
    try:
        cfg = json.loads(cfg_path.read_text())
        config.validate_lander_branch(cfg.get("lander"))
    except (OSError, ValueError) as exc:
        print(f"capsule pr daily: config error: {exc}", file=sys.stderr)
        return None, None, 1
    branch, repo = (cfg.get("lander") or {}).get("branch") or "main", str(cfg.get("repo_url"))
    if branch == "main":
        print(f"capsule pr daily: board {board} lands on main; no pull request to open")
        return None, None, 0
    if not GITHUB.fullmatch(repo) and not os.path.isdir(repo):
        print("capsule pr daily: the board's repo is neither a github.com url nor a directory",
              file=sys.stderr)
        return None, None, 1
    return branch, repo, None


def _warn(conn, ids, last, branch):
    """Stderr warnings where git's list and the `landed` events disagree (git wins)."""
    ever, since = _landed(conn, 0), _landed(conn, last)
    for tid in ids:
        if tid not in ever:
            print(f"capsule pr daily: warning: {tid} is on {branch} but has no landed event",
                  file=sys.stderr)
    for tid in since:
        if tid not in ids:
            print(f"capsule pr daily: warning: {tid} landed but is not in git on {branch}",
                  file=sys.stderr)


def _ids(repo, branch):
    """The git card list; a url board has no clone, so a throwaway one is made."""
    with tempfile.TemporaryDirectory() as tmp:
        local = repo
        if not os.path.isdir(repo):
            local = str(Path(tmp) / "c")
            rc, out = lander._git(tmp, "clone", "-q", "--no-checkout", repo, local, timeout=300)
            if rc:
                raise RuntimeError(f"git clone failed: {out[-300:]}")
        return _git_cards(local, branch)


def run(board, home, dry_run=False):
    from capsule import db
    branch, repo, code = _board(board, home)
    if branch is None:
        return code
    mark = int(time.time())        # taken before anything is read: a card landed meanwhile counts
    try:
        ids = _ids(repo, branch)
    except RuntimeError as exc:      # no list means no PR change: never a blank body
        print(f"capsule pr daily: {exc}", file=sys.stderr)
        return 1
    conn = _open_db(board, home)
    try:
        last = (db.guard_get(conn, GUARD)[0] or {}).get("at", 0)
        _warn(conn, ids, last, branch)
        number, err = _open_pr(repo, branch)
        if err is None and number is None and not ids:
            print("nothing to propose")
            return 0
        titles = {r[0]: r[1] for r in conn.execute("SELECT id, title FROM tasks")}
        text = "\n".join(f"- {t}: {' '.join(titles.get(t, '(not on this board)').split())}"
                         for t in ids) or "(no cards on the branch)"
        title = f"Capsule daily: {board} {time.strftime('%Y-%m-%d')}"
        args = (["pr", "edit", str(number), "--body-file", "-"] if number is not None else
                ["pr", "create", "--draft", "--base", "main", "--head", branch, "--title", title,
                 "--body-file", "-"])
        if err is None and dry_run:
            print("would run: " + shlex.join([os.environ.get("CAPSULE_GH_BIN") or "gh", *args]))
            print(text)
            return 0
        rc, out = (1, err) if err is not None else _gh(args, repo, text)
        if rc:
            print(f"capsule pr daily: {out}", file=sys.stderr)
            return 1
        if number is None:       # a created PR is the mark the next run counts from
            db.guard_set(conn, GUARD, {"at": mark, "pr": out[-200:]})
        print(out or f"updated PR #{number}")
        return 0
    finally:
        conn.close()


def handle(args, parser):
    from capsule import paths
    return run(args.board, paths.home(args.capsule_home), dry_run=args.dry_run)
