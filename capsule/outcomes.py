"""Outcome events at +14 d (headline) and +30 d (tracked) per landed card (Evals E4)."""
import json
import re
import subprocess
import sys
from pathlib import Path

from capsule import app_events, blocks, human, incidents
from capsule.db import event, guard_get, guard_set, txn

WINDOWS = (14, 30)
DAY = 86400
SINCE = 1_792_281_600   # 2026-10-18T00:00:00Z = E4's landing day; keeper adjusts if it slips
GUARD, EVAL_GUARD = "outcomes:day", "eval_check:day"
_FIX = re.compile(r"\b(?:hot|bug)?fix(?:es|ed|ing)?\b(?![ -]cards?\b)", re.I)
_ESCAPED = "escaped_bug:%"
_NOT_DEFECTS = ("human_intervention:%", "override:%", _ESCAPED)


def _git(checkout, *args):
    try:
        res = subprocess.run(["git", "-C", str(checkout), *args], capture_output=True, text=True,
                             timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    return res.stdout if res.returncode == 0 else None


def _arrival(checkout, full):
    """Commit time at which `full` reached main's first-parent line (0 when git cannot say)."""
    if full in (_git(checkout, "rev-list", "--first-parent", "main") or "").split():
        out = _git(checkout, "show", "-s", "--format=%ct", full)
    else:
        out = _git(checkout, "log", "--first-parent", "--ancestry-path", "--reverse",
                   "--format=%ct", f"{full}..main")
    return int(out.split()[0]) if out and out.strip() else 0


def _landing(checkout, sha):
    """The commit's landing on main, or None when it is not on main."""
    full = (_git(checkout, "rev-parse", "--verify", "-q", f"{sha}^{{commit}}") or "").strip()
    if not full or _git(checkout, "merge-base", "--is-ancestor", full, "main") is None:
        return None
    out = _git(checkout, "log", "--format=%ct%x1f%s%x1f%b%x1e", f"{full}..main") or ""
    rows = (r.strip("\n").split("\x1f", 2) for r in out.split("\x1e") if r.strip())
    merged = (_git(checkout, "log", "--merges", "--ancestry-path", "--format=%H %P",
                   f"{full}..main") or "").splitlines()
    return {"sha": full, "arrival": _arrival(checkout, full),
            "commits": [(int(ct), subject, body) for ct, subject, body in rows],
            "merges": [m.split()[0] for m in merged if m.split()[2:3] == [full]]}


def _git_signals(landing, tid, end):
    found = set()
    names = [landing["sha"], *landing["merges"]]
    for ct, subject, body in landing["commits"]:
        if ct > end:
            continue
        if subject.startswith("Revert") and (tid in subject or any(n in body for n in names)):
            found.add("revert")
        elif tid in subject and _FIX.search(subject):
            found.add("followup")
    return found


def _incident_fired(conn, tid, start, end):
    skip = " AND ".join("root_key NOT LIKE ?" for _ in _NOT_DEFECTS)
    row = conn.execute(
        "SELECT 1 FROM incidents WHERE (task_id=? OR evidence LIKE ?) AND opened_at>?"
        f" AND opened_at<=? AND {skip}", (tid, f"%{tid}%", start, end, *_NOT_DEFECTS)).fetchone()
    return row is not None


def _signals(conn, tid, landed, window, landing):
    """The sorted signal names that fired for card `tid` within `window` days of `landed`."""
    end = landed + window * DAY
    found = _git_signals(landing, tid, end)
    if _incident_fired(conn, tid, landed, end):
        found.add("incident")
    return sorted(found)


def _landed(conn):
    rows = conn.execute(
        "SELECT t.id, e.created_at, e.payload FROM tasks t JOIN task_events e"
        f" ON e.task_id=t.id AND e.kind='completed' WHERE t.status='done' AND"
        f" {blocks.not_closed_after('t.id', 'e.id')} ORDER BY e.id")
    return {r[0]: (r[1], json.loads(r[2] or "{}").get("commit")) for r in rows}


def _due(conn, tid, since, now):
    have = {r[0] for r in conn.execute(
        "SELECT json_extract(payload,'$.window') FROM task_events"
        " WHERE task_id=? AND kind='outcome'", (tid,))}
    return [w for w in WINDOWS if now >= since + w * DAY and w not in have]


# Kinds that send landed work back (CoS ruling 2026-10-19).
HUMAN_FIX_KINDS = ("reset", "screen-fix", "override")


def _human_fix(files, tid, start, end):
    """The owner's corrective spool files naming `tid` inside (start, end]; handover tags never."""
    from capsule.report import EXCLUDED_TAGS   # late: report -> outcomes
    return sum(1 for _, d in files if d.get("task_id") == tid and start < int(d["at"]) <= end
               and human.canonical_author(d.get("author", "owner")) == "owner"
               and d.get("kind") in HUMAN_FIX_KINDS
               and d.get("tag") not in EXCLUDED_TAGS)


def _record(conn, board, tid, window, signals, commit, now, human_fix=0):
    with txn(conn):
        event(conn, tid, "outcome", {"window": window, "escaped": bool(signals),
                                     "signals": signals, "commit": commit,
                                     "human_fix": human_fix}, now=now)
        seen = conn.execute("SELECT 1 FROM incidents WHERE task_id=? AND root_key LIKE ?",
                            (tid, _ESCAPED)).fetchone()
        if signals and not seen:
            incidents.open_incident(
                conn, board=board, kind="escaped_bug", task_id=tid, now=now,
                key=f"{tid}: {'+'.join(signals)}",
                evidence=[f"commit:{commit}", f"outcome:{window}d"])


def step(conn, *, checkout, board, now, home):
    """Write the outcomes whose window has elapsed and have none yet; returns how many."""
    from capsule import touches   # late: touches -> report -> outcomes
    files = touches._spool_files(home, board, SINCE) if home else []
    written = 0
    for tid, (completed, sha) in _landed(conn).items():
        if completed < SINCE:
            continue
        due = _due(conn, tid, completed, now)      # arrival is never before `completed`'s window
        landing = _landing(checkout, sha) if due and sha else None
        if landing is None:
            continue
        landed = max(completed, landing["arrival"])
        for window in (w for w in due if now >= landed + w * DAY):
            signals = _signals(conn, tid, landed, window, landing)
            fixes = _human_fix(files, tid, landed, landed + window * DAY)
            _record(conn, board, tid, window, signals, landing["sha"], now, fixes)
            written += 1
    return written


def rate(conn, window):
    """Share of landed cards whose `window`-day outcome escaped; "pending" while none has one."""
    rows = conn.execute("SELECT json_extract(payload,'$.escaped') FROM task_events"
                        " WHERE kind='outcome' AND json_extract(payload,'$.window')=?",
                        (window,)).fetchall()
    return sum(r[0] for r in rows) / len(rows) if rows else "pending"


def tick_pass(conn, cfg, ks, counts, *, spool_root, human_dir, now,
              count=lambda ids: len(set(ids))):
    """The tick's incidents step: open incidents from events and from the board's app-events
    inbox (D79, a sibling of `human_dir`), then the once-a-day outcomes pass and eval-check pass
    (E2; the board home is `human_dir`'s, no home means no eval check)."""
    main_sha = incidents.main_head(ks["main_checkout"])
    ids = incidents.from_events(
        conn, board=cfg["board"], spool_root=spool_root, human_dir=human_dir,
        main_sha=main_sha, now=now)
    if human_dir:
        ids += app_events.drain(conn, board=cfg["board"], main_sha=main_sha, now=now,
                                inbox_dir=app_events.inbox(Path(human_dir).parent))
    counts["incidents"], counts["outcomes"], counts["eval_checks"] = count(ids), 0, 0
    try:
        mark, version = guard_get(conn, GUARD)
        today = {"day": now // DAY}
        if mark != today and guard_set(conn, GUARD, today, expect_version=version, now=now):
            home = Path(human_dir).parents[2] if human_dir else None
            counts["outcomes"] = step(conn, checkout=ks["main_checkout"], board=cfg["board"],
                                      now=now, home=home)
        mark, version = guard_get(conn, EVAL_GUARD)
        if human_dir and mark != today and guard_set(
                conn, EVAL_GUARD, today, expect_version=version, now=now):
            from capsule import eval_check    # late: eval_check -> touches -> report -> outcomes
            counts["eval_checks"] = len(eval_check.tick_step(
                conn, cfg, Path(human_dir).parents[2], now, since_floor=SINCE))
    except Exception as exc:
        counts["tick_errors"] += 1
        print(f"capsule tick: outcomes pass failed: {type(exc).__name__}: {exc}", file=sys.stderr)
