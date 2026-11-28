"""M032: a card whose real diff is over `diff_lines_park` is not landed (split it or make it an
epic). The size is git's own `--numstat` against the merge base on the target branch. Helper of
`capsule.landing._candidates`; kept out of landing.py (800-line cap)."""
import sys

from capsule import blocks, config, day_branch, lander, paths
from capsule.db import event, guard_get, guard_set, transition, txn

sys.path.insert(0, str(paths.REPO / "maintenance"))
import budgets  # noqa: E402

ASKED = "lander.diffceiling:"    # per card: {sha} of the head the ceiling was already reported on
_SKIP_DIRS = ("tests/", "capsule/tests/", "maintenance/tests/")


def _counted(path):
    return not (path.startswith(_SKIP_DIRS) or path.endswith(".lock")
                or path.rsplit("/", 1)[-1] == "package-lock.json")


def _size(repo, tgt, sha):
    """Added plus removed lines of `sha` against its merge base with origin/<tgt>, test files and
    lockfiles left out. None when git cannot say (the card is then judged as before)."""
    rc, base = lander._git(repo, "merge-base", f"origin/{tgt}", sha)
    if rc:
        return None
    rc, out = lander._git(repo, "diff", "--numstat", "--no-renames", f"{base}..{sha}")
    if rc:
        return None
    total = 0
    for line in out.splitlines():
        add, rem, path = line.split("\t", 2)
        if _counted(path) and add != "-":      # `-`: a binary file has no line count
            total += int(add) + int(rem)
    return total


def _held(conn, cfg, task_id, branch, sha, now):
    """True when the card is over the ceiling and so must not land. Off (0) and an epic card
    are never held, nor a head a human already unblocked after its hold (the waiver). Over the
    ceiling, the target is fetched once and the size recomputed (a stale target must not hold).
    In land mode the hold is one event, and the card goes back to `ready` blocked `needs_input`
    (offer unblock|reset), once per head; a new head is judged again. Dry-run writes nothing."""
    limit = budgets.thresholds()["diff_lines_park"]
    if limit <= 0 or branch in config.epic_branches_from_list(cfg.get("epics")) \
            or _waived(conn, task_id, sha):
        return False
    repo, tgt = cfg["repo_url"], lander.target(cfg)
    size = _size(repo, tgt, sha)
    if size is not None and size > limit:
        day_branch.fetch(repo, tgt)
        size = _size(repo, tgt, sha)
    if size is None:
        print(f"capsule tick: diff ceiling: no size for {task_id} at {sha[:12]}; not judged",
              file=sys.stderr)
    if size is None or size <= limit:
        return False
    if cfg["lander"]["mode"] != "land" or blocks.was_closed(conn, task_id) \
            or (guard_get(conn, ASKED + task_id)[0] or {}).get("sha") == sha:
        return True
    why = f"diff size {size} over the ceiling of {limit}: split it or make it an epic"
    with txn(conn):
        event(conn, task_id, "landing", {"outcome": "diff_over_ceiling", "commit": sha,
                                         "detail": why}, now=now)
        transition(conn, task_id, "ready", frm=("done",))     # `block` takes review, ready
        blocks.block(conn, task_id, kind="needs_input", reason=why, now=now, offer="unblock|reset")
    guard_set(conn, ASKED + task_id, {"sha": sha}, now=now)
    return True


def _waived(conn, task_id, sha):
    """True when a human `unblocked` this head after its ceiling hold: the human saw the size and
    chose to land it, so the same head is not held again."""
    return conn.execute(
        "SELECT 1 FROM task_events h WHERE h.task_id=? AND h.kind='landing' AND "
        "json_extract(h.payload,'$.outcome')='diff_over_ceiling' AND "
        "json_extract(h.payload,'$.commit')=? AND EXISTS (SELECT 1 FROM task_events u WHERE "
        "u.task_id=h.task_id AND u.kind='unblocked' AND u.id>h.id)",
        (task_id, sha)).fetchone() is not None
