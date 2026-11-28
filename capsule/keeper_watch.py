"""The keeper watchdog: page when the CoS
keeper loop goes silent while work waits."""
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

from capsule import db, outbox, paths, unblocked

SILENT_AFTER = 3600        # keeper-log older than this, with work waiting, is a page
ESCALATE_AFTER = 4 * 3600  # still silent this long after the first page: one more
PAUSE_FILE = "keeper-pause.json"  # <state>/keeper-pause.json: the CoS declares a deliberate pause
PAUSE_CAP = 12 * 3600      # a pause never outlasts its file's mtime by more than this
CC = Path.home() / "CodeBase" / "capsule-corp"
DEFAULT_LOG = CC / "scratchpad" / "keeper-log.md"


def decide(log_age, waiting_count, state, mtime, now):
    """(action, new_state): none | page | escalate. Pure; state: {paged_mtime, paged_at, escalated}.
    Escalation is measured from the episode's first page, not from the log's age."""
    state = dict(state or {})
    if log_age < SILENT_AFTER or waiting_count <= 0:
        return "none", state
    if state.get("paged_mtime") != mtime:
        return "page", {"paged_mtime": mtime, "paged_at": now, "escalated": False}
    if now - state.get("paged_at", now) >= ESCALATE_AFTER and not state.get("escalated"):
        state["escalated"] = True
        return "escalate", state
    return "none", state


def _pause_state(text, written, now):
    """(kind, detail) for the text of keeper-pause.json: active | expired | malformed. The CoS
    declares a deliberate pause as {"until": "<iso time>", "reason": "..."}. A pause never runs
    past `written` (the file's mtime) + 12 h, so a far-future `until` cannot silence for longer."""
    if written > now + 300:
        return "malformed", "file mtime is in the future"
    try:
        doc = json.loads(text)
        until = datetime.fromisoformat(doc["until"]).timestamp()
    except (ValueError, KeyError, TypeError, OverflowError, RecursionError) as exc:
        return "malformed", f"{type(exc).__name__}: {exc}"[:120]
    until = min(until, written + PAUSE_CAP)
    if until <= now:
        return "expired", "until has passed"
    when = datetime.fromtimestamp(until).isoformat(timespec="minutes")
    return "active", f"until {when}: {doc.get('reason', '')}"


def _read_pause(home, now):
    """_pause_state for <home>/keeper-pause.json; ("none", "") when the file is absent."""
    path = Path(home) / PAUSE_FILE
    try:
        if path.is_symlink():    # stat follows links: a rewritten target would keep the pause alive
            return "malformed", "pause file is a symlink"
        return _pause_state(path.read_text(), path.stat().st_mtime, now)
    except (OSError, ValueError) as exc:    # ValueError covers UnicodeDecodeError
        return ("none", "") if not path.exists() else ("malformed", f"unreadable: {exc}"[:120])


def _write_pause(home, duration, reason, now=None):
    """Write the pause file for `duration` ("90m", "3h"); more than 12 h is clamped to 12 h."""
    match = re.fullmatch(r"(\d+)([mh])", duration.strip())
    if not match:
        raise ValueError(f"duration must look like 90m or 3h, got {duration!r}")
    now = time.time() if now is None else now
    seconds = min(int(match[1]) * (60 if match[2] == "m" else 3600), PAUSE_CAP)
    until = datetime.fromtimestamp(now + seconds).astimezone().isoformat(timespec="seconds")
    path = Path(home) / PAUSE_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"until": until, "reason": reason}) + "\n")
    os.utime(path, (now, now))
    return path


def waiting(home, detect=unblocked.detect):
    """Unowned, dispatchable work: the same source `capsule unblocked` and the stop hook use.
    A marker's tick_restart (NEEDED/PENDING) is the owner's host action, never keeper work."""
    return sum(1 for item in detect(capsule_home=home) if item.unowned())


def heartbeat_row(log, now, home=None):
    """The `capsule check` row: ok under an hour, WARN (info level, never fails the check) after.
    With `home`, a declared pause shows as "paused until ...: reason", a bad file as a WARN."""
    level, name, detail = _heartbeat(log, now)
    kind, why = _read_pause(home, now) if home else ("none", "")
    if kind == "active":
        return "info", name, f"{detail}; paused {why}"
    if kind == "malformed":
        return "info", name, f"{detail}; WARN pause file malformed: {why}"
    return level, name, detail


def _heartbeat(log, now):
    try:
        age = now - Path(log).stat().st_mtime
    except OSError:
        return ("info", "keeper-heartbeat", f"{log} absent (no keeper log on this machine)")
    if age < SILENT_AFTER:
        return ("ok", "keeper-heartbeat", f"keeper-log updated {int(age // 60)} min ago")
    return ("info", "keeper-heartbeat", f"WARN keeper-log silent {int(age // 60)} min")


def _last_line(log):
    lines = [x for x in Path(log).read_text(errors="replace").splitlines() if x.strip()]
    return lines[-1][:200] if lines else ""


def _outbox(home, board, head, last):
    board_db = paths.board_db(board, home)
    if not board_db.exists():
        return "outbox skipped: no board db"
    fields = [("last", last or "(empty log)"), ("action", "resume or start a CoS session")]
    try:
        conn = db.connect(board_db)
        try:
            return f"outbox rows {outbox.notify(conn, 'blocked', head, fields=fields, page=True)}"
        finally:
            conn.close()
    except Exception as exc:
        return f"outbox failed: {exc}"


def run(home, board, log, now=None):
    now = time.time() if now is None else now
    state_path = Path(home) / "keeper-watch.state"
    try:
        mtime = Path(log).stat().st_mtime
    except OSError:
        print("keeper-watch: no keeper log; nothing to judge")
        return 0
    try:
        state = json.loads(state_path.read_text())
    except (OSError, ValueError):
        state = {}
    try:
        count = waiting(home)
    except Exception as exc:
        print(f"keeper-watch: cannot read waiting work ({type(exc).__name__}: {exc}); no page")
        return 0
    pause, detail = _read_pause(home, now)
    if pause == "active":
        print(f"keeper-watch: paused ({detail}); staying quiet")
        return 0
    action, state = decide(now - mtime, count, state, mtime, now)
    if action == "none":
        print(f"keeper-watch: ok ({heartbeat_row(log, now)[2]}, waiting {count})")
        return 0
    head = f"keeper silent {int((now - mtime) // 60)} min with {count} waiting"
    head += " (escalation)" if action == "escalate" else ""
    if pause == "malformed":
        head += f" (pause file malformed, not honoured: {detail})"
        print(f"keeper-watch: pause file malformed ({detail}); paging")
    last = _last_line(log)
    result = _outbox(home, board, head, last)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state))
    print(f"keeper-watch: {action}: {result}")
    return 0


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["pause"]:
        if len(argv) != 3:
            print('usage: keeper-pause <90m|3h> "reason"   (max 12h)', file=sys.stderr)
            return 2
        try:
            path = _write_pause(paths.home(), argv[1], argv[2])
        except ValueError as exc:
            print(f"keeper-pause: {exc}", file=sys.stderr)
            return 2
        print(f"keeper-pause: wrote {path}: {path.read_text().strip()}")
        return 0
    return run(paths.home(), os.environ.get("CAPSULE_KEEPER_BOARD", "capsule-corp"),
               Path(os.environ.get("CAPSULE_KEEPER_LOG", DEFAULT_LOG)))


if __name__ == "__main__":
    sys.exit(main())
