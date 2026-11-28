"""Host seats shared by every board under $CAPSULE_HOME/boards."""
import os
import socket
import sqlite3

from capsule import config, db, incidents, outbox, paths

REVIEW_PROFILES = ("reviewer", "validator", "guide", "korin", "blind-tester", "security")
NOTICE_KEY, NOTICE_EVERY = "caps:unreadable_notice", 3600
HOLD_KEY, HOLD_INCIDENT_AFTER = "caps:unreadable_hold", 3600   # one incident per hold
HOLD_MAX_GAP = 600       # samples further apart than this do not prove the board stayed unreadable
LANDER_TTL = 3600     # a `lander.busy` flag older than this is a dead lander (capsule/landing.py)


def heavy_cap(cfg):
    """`caps.max_heavy` less `caps.external_heavy` (seats an outside chat uses), never below 0."""
    return max(config.cap(cfg, "max_heavy") - config.cap(cfg, "external_heavy"), 0)


def limits(cfg):
    return config.cap(cfg, "max_review"), heavy_cap(cfg)


def implementer_count(conn, now):
    """Genuinely running cards: `status='running'` and the claim not yet expired (the one
    definition `dispatch.running_count` and the capacity blocker use)."""
    return conn.execute(
        "SELECT COUNT(*) FROM tasks WHERE status='running'"
        " AND (claim_expires IS NULL OR claim_expires >= ?)", (now,)).fetchone()[0]


def _pid_alive(pid):
    """The tick's own test for a worker it did not spawn (`Tick._alive`): `kill(pid, 0)`."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _run_alive(pid, lock):
    """False only for a run provably gone: a local worker whose recorded
    pid is dead."""
    if pid is None or not (lock or "").startswith(socket.gethostname() + ":"):
        return True
    return _pid_alive(pid)


def _tally(conn, now):
    marks = ",".join("?" * len(REVIEW_PROFILES))
    runs = conn.execute(
        "SELECT worker_pid, claim_lock, profile FROM task_runs"
        f" WHERE ended_at IS NULL AND profile IN ({marks})", REVIEW_PROFILES).fetchall()
    live = [r[2] for r in runs if _run_alive(r[0], r[1])]
    busy, _ = db.guard_get(conn, "lander.busy")      # the lander (C07b) holds one heavy seat
    lander = bool(busy and busy.get("busy") and now - busy["at"] < LANDER_TTL)   # a crash expires
    return implementer_count(conn, now), len(live), int(lander), live.count("blind-tester")


def _tally_ro(path, now):
    other = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    try:
        return _tally(other, now)
    finally:
        other.close()


def _other_boards(home, board):
    root = home / "boards"
    if not root.is_dir():
        return []
    return sorted(d.name for d in root.iterdir() if d.name != board and (d / "kanban.db").is_file())


def counts(conn, home, board, now):
    """{implementers, review, heavy, testers, unreadable: [board names]} over this board (`conn`)
    and, when `home` is given, every other board's `kanban.db`. `testers` is the live blind-tester
    runs, already inside `review`."""
    tally = list(_tally(conn, now))
    unreadable = []
    for name in _other_boards(home, board) if home is not None else []:
        try:
            tally = [a + b for a, b in zip(tally, _tally_ro(paths.board_db(name, home), now))]
        except (sqlite3.Error, OSError):
            unreadable.append(name)
    impl, review, lander, testers = tally
    return {"implementers": impl, "review": review, "heavy": impl + review + lander,
            "landers": lander, "testers": testers, "unreadable": unreadable}


def _track_hold(conn, board, unreadable, now):
    """Fail-closed stays, but a hold that lasts HOLD_INCIDENT_AFTER opens one `silent_failure`
    incident (the tick has no error to report). A board that reads again clears the clock."""
    held, _ = db.guard_get(conn, HOLD_KEY)
    if not unreadable:
        if held:
            db.guard_set(conn, HOLD_KEY, {}, now=now)
        return
    if not held or now - held["seen"] > HOLD_MAX_GAP:
        held = {"since": now}                  # a gap between samples restarts the clock
    held["seen"] = now
    if "incident" not in held and now - held["since"] >= HOLD_INCIDENT_AFTER:
        names = ", ".join(unreadable)
        held["incident"], _ = incidents.open_incident(
            conn, board=board, kind="silent_failure", role="caps", now=now,
            key=f"seat caps held: board DB unreadable ({names})",
            evidence=[f"board(s) {names} unreadable since {held['since']}; work held"])
    db.guard_set(conn, HOLD_KEY, held, now=now)


def snapshot(conn, cfg, home, now):
    """`counts` for this board's config, plus the hourly `paused` line for an unreadable board."""
    got = counts(conn, home, cfg["board"], now)
    _track_hold(conn, cfg["board"], got["unreadable"], now)
    if got["unreadable"]:
        last, _ = db.guard_get(conn, NOTICE_KEY)
        if last is None or now - last["at"] >= NOTICE_EVERY:
            with db.txn(conn):
                outbox.notify(conn, "incident", f"{cfg['board']} held: board DB unreadable",
                              fields=[("boards", ", ".join(got["unreadable"]))], page=True, now=now)
                db.guard_set(conn, NOTICE_KEY, {"at": now}, now=now)
    return got
