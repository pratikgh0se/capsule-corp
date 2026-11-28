"""The timed Slack voice (visibility spec section 6, card 9): a morning summary and a
reminder."""
import json
import os
import sqlite3
import sys
from datetime import datetime, time as dtime, timedelta
from functools import partial
from pathlib import Path

from capsule import ask_apply, db, invalid_doc, outbox, slack_msgs, statusview
from capsule.runners import host_wall

DAY_START, DAY_END = 9, 21
REMIND_AFTER_S, PAUSE_AFTER_S = 4 * 3600, 3600
REMINDER = "Still waiting on you for this one."
MAX_LINES, MAX_NEEDS, MAX_BROKEN = 8, 3, 2


def step(conn, cfg, now, home, guard, counts, *, local=datetime.fromtimestamp):
    """Never breaks the tick pass: a failure is recorded through the tick's own `guard`."""
    work = partial(_run, conn, cfg["board"], now, home, local)
    guard(conn, cfg, "slack_digest", now, counts, work)


def claim(home, name):
    """True for the one caller that creates the marker `<home>/digest/<name>` (O_EXCL)."""
    folder = Path(home, "digest")
    folder.mkdir(parents=True, exist_ok=True)
    try:
        os.close(os.open(folder / name, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
    except FileExistsError:
        return False
    return True


def each_board(home, fn):
    """[fn(name, read-only connection)] for every board under `home`. A board that cannot be
    read (no `slack_msg` yet, busy, corrupt) is skipped with a line on stderr; connections close."""
    out = []
    for path in sorted(Path(home, "boards").glob("*/kanban.db")):
        ro = None
        try:
            ro = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
            ro.row_factory = sqlite3.Row
            out.append(fn(path.parent.name, ro))
        except sqlite3.Error as exc:
            print(f"capsule slack_digest: board {path.parent.name} skipped: {exc}", file=sys.stderr)
        finally:
            if ro is not None:
                ro.close()
    return out


def _run(conn, board, now, home, local):
    if not outbox.slack_enabled(conn):
        return
    when = local(now)
    by_day = DAY_START <= when.hour < DAY_END
    if by_day:
        remind(conn, now)
    marker = f"summary-{when.date().isoformat()}"
    if when.hour >= DAY_START and not Path(home, "digest", marker).exists() and claim(home, marker):
        try:
            outbox.add(conn, "slack", summary(home, now, when), now=now)
        except Exception:
            Path(home, "digest", marker).unlink()      # nothing was queued: let the next pass retry
            raise
    key = slack_msgs.broken_key(f"paused:{board}")      # a pause over an hour: one red, then a tick
    since = paused_since(conn, now)
    if since is not None and now - since > PAUSE_AFTER_S:
        if by_day and not outbox.is_posted_or_queued(conn, key) and claim(home, f"pause-{since}"):
            outbox.notify(conn, "blocked", _pause_line(home, board, now),
                          key=key, page=True, now=now)
    elif _running_again(conn, now):
        outbox.finish(conn, key, f"{slack_msgs.HANDLED} The {board} board is running again.", now)


def _running_again(conn, now):
    """True once a run has started since the last pause ended (one the pause itself ended with
    `rate_limited` is no start) and no host wall outage is still on. The digest runs before
    dispatch, so the pass after a lapsed pause is not "running again" until a session works."""
    value, _ = db.guard_get(conn, "pause:board")
    until = value.get("until", 0) if isinstance(value, dict) else 0
    return conn.execute("SELECT 1 FROM task_runs WHERE started_at>=? AND COALESCE(outcome, '')"
                        " != 'rate_limited' LIMIT 1", (until,)).fetchone() is not None and not any(
        invalid_doc.outage_open(conn, role, now) for role in host_wall.HOST_ROLES)


def _is_paused(name, conn, now):
    return paused_since(conn, now) is not None


def _pause_line(home, board, now):
    held = each_board(home, partial(_is_paused, now=now))
    what = "All boards are" if all(held) else f"Work on the {board} board is"
    return f"{what} paused. Nothing new starts until it is lifted. Nobody is on it yet"


def remind(conn, now):
    """One thread reply per stale open `needs:` message; `reminded_at` makes it once."""
    for row in conn.execute(
            "SELECT key FROM slack_msg WHERE kind='needs' AND state='open' AND reminded_at IS NULL"
            " AND posted_at<=?", (now - REMIND_AFTER_S,)).fetchall():
        outbox.add(conn, "slack", REMINDER, dedup_key=slack_msgs.REMIND + row["key"], now=now)
        conn.execute("UPDATE slack_msg SET reminded_at=? WHERE key=?", (now, row["key"]))


def paused_since(conn, now):
    """When this board's `pause:board` was first held (its `since`, else the guard's last write),
    or None when it is not paused."""
    value, _ = db.guard_get(conn, "pause:board")
    row = conn.execute("SELECT updated_at FROM guard_state WHERE key='pause:board'").fetchone()
    if not isinstance(value, dict) or value.get("until", 0) <= now or row is None:
        return None
    return int(value.get("since") or row[0])


def _named(conn, key):
    task = conn.execute("SELECT * FROM tasks WHERE id=?", (key.split(":", 1)[1],)).fetchone()
    if task is None:
        return None
    number = statusview._number(task)
    return f"{statusview._name(task)} #{number}" if number else statusview._name(task)


def _gather(name, c, now, start, end):
    """One board's (finished yesterday, open asks as (count, names), broken lines, pause)."""
    done = c.execute("SELECT COUNT(*) FROM tasks WHERE status='done' AND completed_at>=? AND"
                     " completed_at<?", (start, end)).fetchone()[0]
    keys = [r["key"] for r in c.execute(
        "SELECT key FROM slack_msg WHERE kind='needs' AND state='open' ORDER BY posted_at")]
    broken = [r["text"] for r in c.execute(
        "SELECT text FROM slack_msg WHERE kind='broken' AND state='open' AND key NOT LIKE"
        " 'broken:paused:%' ORDER BY posted_at")]
    since = paused_since(c, now)
    long = since is not None and now - since >= PAUSE_AFTER_S
    held = [(name, int((now - since) // 3600))] if long else []
    return done, (len(keys), [_named(c, k) for k in keys[:MAX_NEEDS]]), broken, held


def _cleaned(name, c, start, end):
    """The self-cleaned epic clone lines (row 624) written yesterday, from `guard_state`."""
    rows = c.execute("SELECT value FROM guard_state WHERE key LIKE 'epic_cleaned:%' AND"
                     " updated_at>=? AND updated_at<? ORDER BY updated_at", (start, end)).fetchall()
    return [json.loads(r[0])["text"] for r in rows]


def summary(home, now, when):
    """The one morning message across every board under `home`."""
    start = datetime.combine(when.date() - timedelta(days=1), dtime.min).timestamp()
    end = datetime.combine(when.date(), dtime.min).timestamp()
    got = each_board(home, partial(_gather, now=now, start=start, end=end))
    done = sum(g[0] for g in got)
    need_n = sum(g[1][0] for g in got)
    names = [n for g in got for n in g[1][1] if n][:MAX_NEEDS]
    broken = [t for g in got for t in g[2]]
    held = [h for g in got for h in g[3]]
    cleaned = [t for ls in each_board(home, partial(_cleaned, start=start, end=end)) for t in ls]
    gate_boards = sorted(p.parent.name for p in Path(home, "boards").glob("*/kanban.db"))
    gate = ask_apply.line(home, gate_boards, start, end)
    if not (need_n or broken or held or cleaned or gate):
        return f"☀️ Quiet day: {done} finished, nothing needs you."
    lines = [f"☀️ Yesterday: {done} finished, "
             + (f"{need_n} need you" if need_n else "nothing needs you") + ", "
             + (f"{len(broken)} broken" if broken else "nothing broken") + "."]
    lines += [f"• {n}" for n in names]
    if need_n > len(names):
        lines.append(f"…and {need_n - len(names)} more waiting on you.")
    for text in broken[:MAX_BROKEN]:
        lines.append("• " + statusview.plain(text.splitlines()[0].lstrip("🔴🚨 "), 120))
    for name, hours in held:
        lines.append(f"The {name} board has been paused for {max(hours, 1)} hour(s).")
    lines += [f"• {statusview.plain(t, 160)}" for t in cleaned[:MAX_BROKEN]]
    lines = lines[:MAX_LINES]
    if gate:      # the gate lines come after the cap, so a busy day cannot cut the undo commands
        lines += [gate] + ask_apply.detail(home, gate_boards, start, end, "\u2022 ")
    return "\n".join(lines)
