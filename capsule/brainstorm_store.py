"""The files of one Slack brainstorm (M204, D229): the layout intake (B1), the runner (B2) and
open/close (B3) share. `<home>/boards/<board>/brainstorms/<id>/` holds `meta.json` (title,
channel, thread_ts, session_id, name, status, started_by, started_at, closed_at), `turns.jsonl`
(append-only, one {ts, role, text} per line), `pending/<event_id>.json` (written O_EXCL, which
is also the dedupe) and an empty `ws/` (the runner's working directory). The thread a reply goes
to comes only from meta. Files only: no model, no network, no subprocess. Minimal on purpose
(Z8: a helper needs two callers). `create` (exclusive mkdir) is shared by open and intake.

Who writes which meta field: open/close write status, closed_at, note and the opener's fields;
`brainstorm.adopt_thread` writes channel and thread_ts once the outbox has delivered the opener;
the runner writes only session_id and session_live, always through `update_meta` (a re-read
merge, so a concurrent close is never overwritten) and never status.

Session rule: `session_id` is the Claude session's name-to-be. A Capsule-started brainstorm gets
its id at open, but no session exists until the runner's first model call. So the runner passes
`--session-id <id>` while `session_live` is falsy, sets `session_live` true after the first
successful reply, and passes `--resume <id>` from then on. It never passes `--session-id` for a
live session."""
import contextlib
import fcntl
import json
import re
import time

from capsule import paths, safe_write

ID_RE = re.compile(r"[0-9a-f]{16}\Z")
ROLES = ("owner", "assistant", "capsule")
STATUSES = ("open", "closed")
OPEN_KEY = "brainstorm:{}"      # outbox key of a Capsule-posted opener (and its slack_msg row)


def bdir(home, board, bid):
    if not ID_RE.match(str(bid)):
        raise ValueError(f"bad brainstorm id {bid!r}")
    return paths.board_dir(board, home) / "brainstorms" / bid


def create(home, board, bid, meta):
    """Make the brainstorm's directories (exclusive: FileExistsError when `bid` is taken) and
    write its meta."""
    d = bdir(home, board, bid)
    d.mkdir(parents=True)
    for sub in ("ws", "pending"):
        (d / sub).mkdir()
    write_meta(home, board, bid, meta)


def read_meta(home, board, bid):
    try:
        return json.loads((bdir(home, board, bid) / "meta.json").read_text(encoding="utf-8"))
    except OSError:
        raise ValueError(f"no brainstorm {bid} on {board}") from None


def write_meta(home, board, bid, meta):
    safe_write.atomic_write(home, bdir(home, board, bid) / "meta.json",
                            json.dumps(meta, indent=2, sort_keys=True) + "\n")


@contextlib.contextmanager
def locked(home, board, bid):
    """The thread's flock (`.meta.lock` in its directory): hold it around any read-modify-write
    of meta.json and around a status decision. Not reentrant: inside it use read_meta and
    write_meta, never update_meta or mark_closed."""
    with open(bdir(home, board, bid) / ".meta.lock", "a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        yield


def update_meta(home, board, bid, **fields):
    with locked(home, board, bid):
        meta = {**read_meta(home, board, bid), **fields}
        write_meta(home, board, bid, meta)
    return meta


def mark_closed(home, board, bid, now=None):
    """Close an open thread (status, closed_at and the closing turn, under the lock). False when
    it was not open: a second `done` changes nothing."""
    now = int(now or time.time())
    with locked(home, board, bid):
        meta = read_meta(home, board, bid)
        if meta.get("status") != "open":
            return False
        write_meta(home, board, bid, {**meta, "status": "closed", "closed_at": now})
        append_turn(home, board, bid, "capsule", "closed", ts=now)
    return True


def append_turn(home, board, bid, role, text, *, ts=None, **extra):
    """Append one turn; role is owner, assistant or capsule; `extra` adds keys (the runner's
    reply number `n`, `events`, `fixed`). One write, so a line is never torn."""
    if role not in ROLES:
        raise ValueError(f"bad turn role {role!r}")
    turn = {"ts": int(ts or time.time()), "role": role, "text": str(text), **extra}
    with safe_write.open_append(home, bdir(home, board, bid) / "turns.jsonl") as fh:
        fh.write((json.dumps(turn, ensure_ascii=False) + "\n").encode("utf-8"))
    return turn


def read_turns(home, board, bid):
    try:
        lines = (bdir(home, board, bid) / "turns.jsonl").read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    return [json.loads(line) for line in lines if line.strip()]


def list_ids(home, board):
    """Every brainstorm id on the board, sorted."""
    root = paths.board_dir(board, home) / "brainstorms"
    if not root.is_dir():
        return []
    return sorted(d.name for d in root.iterdir() if d.is_dir() and ID_RE.match(d.name))


def list_pending(home, board, bid):
    """The queued owner messages ({event_id, text, ts}), oldest first; unreadable files skipped."""
    out = []
    for f in (bdir(home, board, bid) / "pending").glob("*.json"):
        try:
            doc = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(doc, dict) and doc.get("event_id"):
            out.append(doc)
    return sorted(out, key=lambda d: (d.get("ts") or 0, str(d["event_id"])))


def clear_pending(home, board, bid, event_ids):
    """Drop the pending files of these event ids (already answered, or redelivered)."""
    for eid in event_ids:
        if not re.fullmatch(r"[\w.-]+", str(eid)):
            continue
        (bdir(home, board, bid) / "pending" / f"{eid}.json").unlink(missing_ok=True)

