"""Row 624: the tick cleans a dirty epic clone no card holds, instead of the owner doing it by hand.
Back up the full diff (staged, unstaged, untracked) outside the clone, verify it, only then
discard and merge `origin/main` the way `epic_sync` does. A failed backup discards nothing."""
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from capsule import config, incidents, outbox, safe_write, workspace
from capsule.db import guard_set

# a card holds the clone while it runs, or while it is not done and has a run newer (by
# `task_runs.id`, never started_at) than the branch's latest completion: the most recent run of a
# card now done, whose gate pass or human close settled everything in the clone
# a claim lock holds only while live: `lease.release_stale`'s rule, `claim_expires` not yet past
# (no expiry recorded fails closed: it holds)
HOLDING = ("status IN ('running','review','claimed') OR (claim_lock IS NOT NULL"
           " AND (claim_expires IS NULL OR claim_expires >= ?))")
UNSETTLED = ("SELECT r.task_id FROM task_runs r JOIN tasks t ON t.id=r.task_id"
             " WHERE t.branch_name=? AND t.status NOT IN ('done','archived')"
             " AND r.profile IS NOT 'blind-tester' AND r.id >"
             " COALESCE((SELECT MAX(r2.id) FROM task_runs r2 JOIN tasks t2 ON t2.id=r2.task_id"
             " WHERE t2.branch_name=? AND t2.status='done'"
             " AND r2.profile IS NOT 'blind-tester'), 0)")
KEEP_N, KEEP_DAYS = 50, 30
_ERRORS = (RuntimeError, OSError, subprocess.SubprocessError)
NOTE_KEY = "epic_cleaned:"
ASIDE = "aside:"       # `stall_owner`: a card set aside holds no lane
ASIDE_IDS = "SELECT substr(key, 7) FROM guard_state WHERE key LIKE 'aside:%'"


def clean_idle(conn, cfg, work_root, now=None):
    """Clean every declared epic clone that is dirty with no card holding it. Never raises."""
    now = int(now or time.time())
    for branch in config.epic_branches(cfg):
        ws = Path(work_root) / workspace._slug(branch)
        try:
            if workspace.gitdir(ws).is_dir() and not held(conn, branch):
                _clean(conn, cfg, branch, ws, now)
        except _ERRORS as exc:
            print(f"capsule epic_clean: {branch}: {exc}", file=sys.stderr)


def holders(conn, branch, now=None):
    """Ids of the cards on `branch` that hold its clone: the one definition `held` and
    `dispatch.clone_dirty` share."""
    q = (f"SELECT id FROM (SELECT id FROM tasks WHERE branch_name=? AND status NOT IN"  # noqa: S608
         f" ('done','archived') AND ({HOLDING}) UNION {UNSETTLED}) WHERE id NOT IN"
         f" ({ASIDE_IDS})")
    return {r[0] for r in conn.execute(q, (branch, int(now or time.time()), branch, branch))}


def held(conn, branch, now=None):
    return bool(holders(conn, branch, now))


def owner(conn, branch):
    """The newest unsettled run's card on `branch` (the rule `holders` uses), or None."""
    row = conn.execute(f"{UNSETTLED} ORDER BY r.id DESC LIMIT 1", (branch, branch)).fetchone()
    return row[0] if row else None


LIVE = ("running", "review", "claimed")
LANE_ERRORS = (sqlite3.Error, ValueError, AttributeError, TypeError, KeyError)


def _log_lane(branch, exc):
    print(f"capsule epic_clean: lane hold {branch}: {type(exc).__name__}: {exc}", file=sys.stderr)


def lane_holds(conn, now):
    """Read-only: one dict per branch (lane) that `holders` says is held, never a second rule:
    `branch`, `holders` (every holding card: not-ready ones first, each group in card-id order;
    `holder` is the first), `live` (a running, review or claimed card holds),
    `ready` (ready cards waiting behind the holders), `all_ready` (every holder is itself a
    ready card, which dispatch can still start) and `held_s` (seconds since the lane last moved:
    the latest run end of any holder or done card). A lane that cannot be read is
    `{"branch", "unreadable": True, "error": <exception type>}` and nothing else."""
    out = []
    for (branch,) in conn.execute(
            "SELECT DISTINCT branch_name FROM tasks WHERE branch_name IS NOT NULL"
            " AND status NOT IN ('done','archived') ORDER BY branch_name").fetchall():
        try:
            ids = sorted(holders(conn, branch, now))
            lane = _lane(conn, branch, ids, now) if ids else None
        except LANE_ERRORS as exc:      # marked and logged; the other lanes still show
            _log_lane(branch, exc)
            lane = {"branch": branch, "unreadable": True, "error": type(exc).__name__}
        if lane:
            out.append(lane)
    return out


def _lane(conn, branch, ids, now):
    marks = ",".join("?" * len(ids))
    rows = conn.execute(f"SELECT * FROM tasks WHERE id IN ({marks})", ids).fetchall()
    live = [r for r in rows if r["status"] in LIVE or (
        r["claim_lock"] and (r["claim_expires"] is None or r["claim_expires"] >= now))]
    ordered = sorted(rows, key=lambda r: (r["status"] == "ready", r["id"]))
    holder = ordered[0]
    ready = conn.execute(
        f"SELECT COUNT(*) FROM tasks WHERE branch_name=? AND status='ready'"
        f" AND id NOT IN ({marks})", [branch, *ids]).fetchone()[0]
    last = conn.execute(
        "SELECT MAX(COALESCE(r.ended_at, r.started_at)) FROM task_runs r JOIN tasks t"
        f" ON t.id=r.task_id WHERE t.branch_name=? AND (t.status='done' OR t.id IN ({marks}))"
        " AND r.profile IS NOT 'blind-tester'", [branch, *ids]).fetchone()[0]
    return {"branch": branch, "holder": holder, "holders": ordered, "live": bool(live),
            "ready": ready, "all_ready": all(r["status"] == "ready" for r in rows),
            "held_s": max(0, int(now) - int(last or holder["created_at"] or now))}


def _block_reason(conn, task_id):
    """The card's newest hold reason, or None: `$.reason` of a `blocked` or `block_loop_detected`
    event, or `$.trigger` of a `parked_by_human` event."""
    row = conn.execute(
        "SELECT CASE kind WHEN 'parked_by_human' THEN json_extract(payload, '$.trigger')"
        " ELSE json_extract(payload, '$.reason') END FROM task_events WHERE task_id=?"
        " AND kind IN ('blocked', 'block_loop_detected', 'parked_by_human')"
        " ORDER BY id DESC LIMIT 1", (task_id,)).fetchone()
    return " ".join(str(row[0]).split()) if row and row[0] else None


def _reply(board, r):
    """The human verbs that release this holder: a parked card takes only `reset`."""
    verbs = "reset" if r["block_kind"] == "parked" else "amend|unblock|reset"
    return f"reply: python3 -m capsule human {board} {r['id']} {verbs} '<answer>'"


def _run(ws, *args, extra=None):
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_TERMINAL_PROMPT="0", GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
               GIT_DIR=str(workspace.gitdir(ws)), GIT_WORK_TREE=str(ws), **(extra or {}))
    return subprocess.run(["git", *args], cwd=str(ws), capture_output=True,
                          env=workspace.hardening_env(env), timeout=workspace.GIT_FAST_TIMEOUT * 4)


def _bytes(ws, *args, ok=(0,), extra=None):
    r = _run(ws, *args, extra=extra)
    if r.returncode not in ok:
        err = r.stderr.decode(errors="replace")[:300]
        raise RuntimeError(f"git {args[0]} failed (rc={r.returncode}): {err}")
    return r.stdout


def _patch(ws):
    """The whole dirty state as one `git apply`-able patch: tracked changes against HEAD, then
    each untracked file as a new-file diff."""
    out = [_bytes(ws, "diff", "--binary", "HEAD")]
    names = _bytes(ws, "ls-files", "--others", "--exclude-standard", "-z",
                   *workspace.SCRATCH_EXCLUDE).split(b"\0")
    for name in filter(None, names):
        out.append(_bytes(ws, "diff", "--no-index", "--binary", "--", os.devnull,
                          os.fsdecode(name), ok=(0, 1)))
    return b"".join(out)


def _backup(ws, dest_dir, branch, now):
    """Write (atomically, never overwriting) and verify the patch; the path, or raises."""
    data = _patch(ws)
    if not data.strip():
        raise RuntimeError("backup is empty")
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(now))
    base = f"{workspace._slug(branch)}-{stamp}"
    path, n = dest_dir / f"{base}.patch", 0
    while path.exists() or path.is_symlink():
        n += 1
        path = dest_dir / f"{base}-{n}.patch"
    safe_write.atomic_write(dest_dir.parent, path, data, create_dirs=True)   # refuses a symlink
    if path.read_bytes() != data:
        raise RuntimeError(f"backup {path} did not read back")
    with tempfile.TemporaryDirectory() as tmp:     # a throwaway index of HEAD, never the clone's
        extra = {"GIT_INDEX_FILE": os.path.join(tmp, "index")}
        _bytes(ws, "read-tree", "HEAD", extra=extra)
        _bytes(ws, "apply", "--cached", "--check", str(path), extra=extra)
    _prune(dest_dir, now)
    return path


def _prune(dest_dir, now):
    """Keep the newest KEEP_N patches and anything under KEEP_DAYS old, whichever keeps more."""
    files = sorted((p for p in dest_dir.glob("*.patch") if p.is_file() and not p.is_symlink()),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    for old in files[KEEP_N:]:
        if old.stat().st_mtime < now - KEEP_DAYS * 86400:
            old.unlink()


def set_aside(conn, cfg, branch, ws, now):
    """Back up (verified, outside the clone) then discard the clone's dirt: the backup path, or
    None after the incident and notice a failed backup or discard opens."""
    dest_dir = Path(ws).parent.parent / "epic-backups"
    try:
        path = _backup(ws, dest_dir, branch, now)
    except (*_ERRORS, subprocess.CalledProcessError) as exc:
        reason = f"epic {branch} clone is dirty, backup failed ({exc}); left untouched"
        print(f"capsule epic_clean: {reason}", file=sys.stderr)
        if not backup_failed_open(conn, branch):    # one incident + notice until closed, any day
            incidents.open_incident(conn, board=cfg["board"], kind="silent_failure",
                                    evidence=[str(ws), "stray: " + _stray(ws)],
                                    key=f"epic clean backup failed: {branch}", now=now)
            outbox.notify(conn, "incident", f"epic clone {branch} not cleaned: backup failed",
                          fields=[("error", str(exc)[:200]), ("stray", _stray(ws))],
                          file_body=f"capsule: {reason}; stray: {_stray(ws)}", now=now)
        return None
    try:
        workspace._git("reset", "-q", ws=ws)
        workspace._git("checkout", "--", ".", ws=ws)
        workspace._git("clean", "-fdq", *workspace.SCRATCH_EXCLUDE, ws=ws)
    except _ERRORS as exc:
        _incident(conn, cfg, branch, f"discard failed ({exc})", path, now)
        return None
    return path


def backup_failed_open(conn, branch):
    """True while the `epic clean backup failed: <branch>` incident is open (by its fingerprint).
    Fails open: an unreadable incidents table reads as no incident."""
    fp = incidents.fingerprint("silent_failure", f"epic clean backup failed: {branch}")
    try:
        return conn.execute("SELECT 1 FROM incidents WHERE fingerprint=? AND status"
                            " NOT IN ('closed','superseded') LIMIT 1", (fp,)).fetchone() is not None
    except sqlite3.Error:
        return False


def _stray(ws, cap=10):
    """The clone's dirty paths for an incident: the first `cap`, then "+N more"."""
    try:
        out = workspace._git("status", "--porcelain", *workspace.SCRATCH_EXCLUDE, ws=ws).stdout
        lines = [x[3:].strip() for x in out.splitlines() if x.strip()]
    except _ERRORS:
        return "unreadable"
    return ", ".join(lines[:cap]) + (f", +{len(lines) - cap} more" if len(lines) > cap else "")


def _clean(conn, cfg, branch, ws, now):
    dirty = workspace._git("status", "--porcelain", *workspace.SCRATCH_EXCLUDE, ws=ws)
    if not dirty.stdout.strip():
        return
    path = set_aside(conn, cfg, branch, ws, now)
    if path is None:
        return
    try:
        merged = _merge_main(ws)
    except _ERRORS as exc:
        return _incident(conn, cfg, branch, f"merge of origin/main failed ({exc})", path, now)
    if merged is not True:
        return _incident(conn, cfg, branch, f"merge of origin/main failed ({merged})", path, now)
    line = f"epic {branch} clone was dirty with no card on it: cleaned, backup {path}"
    guard_set(conn, f"{NOTE_KEY}{branch}:{now}", {"at": now, "text": line}, now=now)
    outbox.notify(conn, "running", f"epic clone {branch} self-cleaned",
                  fields=[("backup", str(path))], file_body=f"capsule: {line}", now=now)


def _incident(conn, cfg, branch, what, path, now):
    """The clone was backed up but the cleanup did not finish: say so, naming the backup."""
    reason = f"epic {branch} clone self-clean: {what}; backup at {path}"
    print(f"capsule epic_clean: {reason}", file=sys.stderr)
    incidents.open_incident(conn, board=cfg["board"], kind="silent_failure", evidence=[str(path)],
                            key=f"epic clean incomplete: {branch}", now=now)
    outbox.notify(conn, "incident", f"epic clone {branch} self-clean incomplete",
                  fields=[("backup", str(path)), ("error", what[:200])],
                  file_body=f"capsule: {reason}", now=now)


def _merge_main(ws):
    """True when `origin/main` is merged in (or there is none); else why it was not."""
    from capsule.epic_sync import MAIN, _settle_replayed
    try:
        workspace._git("fetch", "--end-of-options", "origin", MAIN, ws=ws,
                       timeout=workspace.GIT_SLOW_TIMEOUT)
    except (RuntimeError, subprocess.TimeoutExpired) as exc:
        if "couldn't find remote ref" in str(exc):
            return True
        return str(exc)[:200]
    r = _run(ws, *workspace.IDENTITY, "merge", "--no-ff", "--no-edit", "--end-of-options",
             f"origin/{MAIN}")
    if r.returncode == 0:
        return True
    if (workspace.gitdir(ws) / "MERGE_HEAD").exists():
        # inc_8a294da7: settle a conflict on lander-replayed commits as epic_sync does; it aborts
        # a real conflict itself
        kept, conflicts = _settle_replayed(ws)
        if kept is not None:
            return True
        if conflicts:
            return f"conflict: {' '.join(conflicts)}"[:300]
    # git writes CONFLICT lines to stdout, so an empty stderr falls back to a stdout tail
    err = r.stderr.decode(errors="replace").strip()[-150:]
    out = r.stdout.decode(errors="replace").strip()[-150:]
    return f"git merge failed (rc={r.returncode}): {err or out or 'no output'}"
