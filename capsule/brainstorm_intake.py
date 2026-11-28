"""Brainstorm intake (M204 B1, D229): route one owner message from the brainstorm channel. Called
by `slack_answer._handle` inside the `python -m capsule.slack_answer event` subprocess, never by
the listener (whose import closure stays pinned). It writes files and one outbox row: no model,
no Slack call.

Forwarded only when ALL hold: no `subtype` or `bot_id`; the payload's team and app ids equal the
host config `slack_team_id` and `slack_app_id` (either missing: intake is off, logged once);
`user` is `slack_owner_id`; `channel` is `slack.channels.brainstorm`. Then:
- a top-level post opens a brainstorm (started_by "owner", title = first line, 80 characters)
  and queues its text as the first pending message;
- a reply in a known open thread is queued as pending/<event_id>.json (O_EXCL is the dedupe);
- the word `done` (any case) in a known thread closes it (`brainstorm.close`);
- anything else is dropped silently. A brainstorm message is never an ask answer.
Caps: 4000 characters (one fixed "split it" reply, nothing queued), 10 pending per thread,
200 open brainstorms (both dropped with a log line)."""
import json
import logging
import os
import re
import secrets
import time
from pathlib import Path

from capsule import brainstorm, brainstorm_store as store, db, outbox, paths, slack_msgs
from capsule.answer_base import SLACK_ID_RE, _host_ids, source_ok  # noqa: F401  moved, re-exported

log = logging.getLogger("capsule.brainstorm_intake")

TEXT_MAX, PENDING_MAX, OPEN_MAX = 4000, 10, 200
SPLIT = "That is too long for one message. Please split it into shorter ones."
CLOSED = "Closed. The summary note is saved to the vault shortly."
CLOSE_FAILED = "I could not close that. Run `capsule brainstorm close {}` in the terminal."
TS_RE = re.compile(r"\d{1,12}\.\d{1,8}\Z")
OFF_MARK = "brainstorm-intake-off.logged"      # under the home: the off warning goes out once


def _off_once(home):
    try:
        with open(Path(home) / OFF_MARK, "x"):
            pass
    except OSError:               # already logged (FileExistsError), or nowhere to note it
        return
    log.warning("brainstorm intake is off: set slack_team_id and slack_app_id in the host config")


def _open(home, board):
    """([(id, meta)] of the open brainstorms, {(channel, thread_ts)} of the closed ones). A thread
    whose opener has since been delivered gets its channel and thread_ts filled in first."""
    out, gone = [], set()
    for bid in store.list_ids(home, board):
        try:
            meta = store.read_meta(home, board, bid)
            if meta.get("status") == "open" and not meta.get("thread_ts"):
                meta = brainstorm.adopt_thread(home, board, bid)
        except (ValueError, OSError):
            continue
        if meta.get("status") == "open":
            out.append((bid, meta))
        else:
            gone.add((meta.get("channel"), meta.get("thread_ts")))
    return out, gone


def _say(home, board, key, channel, root, text):
    """One fixed reply under (channel, root), through the outbox (once per key)."""
    conn = db.connect(paths.board_db(board, home))
    try:
        if not outbox.is_posted_or_queued(conn, key):
            outbox.add(conn, "slack", text, thread_key=f"{channel}|{root}", dedup_key=key)
    finally:
        conn.close()


def _open_new(home, board, ev, opened):
    """Create a brainstorm for the top-level post `ev`; its id."""
    title = " ".join(ev["text"].strip().splitlines()[0].split())[:brainstorm.TITLE_MAX]
    meta = {"title": title, "framing": "", "channel": ev["channel"], "thread_ts": ev["ts"],
            "session_id": "", "name": "", "started_by": "owner", "status": "open",
            "started_at": int(time.time())}
    bid = secrets.token_hex(8)
    store.create(home, board, bid, meta)
    log.info("brainstorm %s opened by the owner (%d open)", bid, len(opened) + 1)
    return bid


def _add_pending(home, board, bid, event_id, text, ts):
    """Queue one owner message as pending/<event_id>.json, created O_EXCL (the dedupe): False
    when that event id is already there. ValueError for an id that is not a plain file name.
    Private: intake is its only caller (Z8)."""
    if not re.fullmatch(r"[\w.-]{1,64}", str(event_id)):
        raise ValueError(f"bad event id {event_id!r}")
    path = store.bdir(home, board, bid) / "pending" / f"{event_id}.json"
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                     0o600)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump({"event_id": event_id, "text": text, "ts": ts}, fh, ensure_ascii=False)
    return True


def _queue(home, board, bid, ev, eid):
    if len(store.list_pending(home, board, bid)) >= PENDING_MAX:
        log.warning("brainstorm %s: pending cap reached; dropped one", bid)
        return False
    return _add_pending(home, board, bid, eid, ev["text"], float(ev["ts"]))


def _close(home, board, bid, ev, eid):
    """`done`: mark the thread closed (fast: no vault write or push here; the runner publishes the
    note on its next pass) and confirm in the thread, or say it failed."""
    try:
        store.mark_closed(home, board, bid)
        line = CLOSED
    except (OSError, ValueError) as exc:
        log.warning("brainstorm %s: close failed: %s", bid, type(exc).__name__)
        line = CLOSE_FAILED.format(bid)
    _say(home, board, f"{slack_msgs.BRAINSTORM_REPLY}closed:{eid}", ev["channel"],
         ev.get("thread_ts") or ev["ts"], line)
    return line == CLOSED


def _route(home, board, ev, eid):
    root = ev.get("thread_ts") or ev["ts"]
    top = root == ev["ts"]
    opened, gone = _open(home, board)
    bid = next((b for b, m in opened if (m.get("channel"), m.get("thread_ts"))
                == (ev["channel"], root)), None)
    word = ev["text"].strip().casefold() == "done"
    if bid is None and (not top or word or (ev["channel"], root) in gone):
        return False        # an unknown or closed thread, a stray `done`, a redelivered opener
    if word:
        return _close(home, board, bid, ev, eid)
    if len(ev["text"]) > TEXT_MAX:
        _say(home, board, f"{slack_msgs.BRAINSTORM_REPLY}split:{eid}", ev["channel"], root, SPLIT)
        return False
    if bid is None:
        if len(opened) >= OPEN_MAX:
            log.warning("brainstorm cap reached (%d open); dropped one", OPEN_MAX)
            return False
        bid = _open_new(home, board, ev, opened)
    return _queue(home, board, bid, ev, eid)


def handle(home, board, blk, payload, owner):
    """Route one `message` events_api payload; True when it opened, queued or closed. Never
    answers an ask. A stranger, another app or any other channel gets nothing at all."""
    ev = payload.get("event") or {}
    if ev.get("subtype") not in (None, "thread_broadcast") or ev.get("bot_id"):
        return False                    # a thread_broadcast is an ordinary reply (also in channel)
    ok = source_ok(home, payload)
    if ok is None:
        _off_once(home)
        return False
    chan = (blk.get("channels") or {}).get("brainstorm")
    fields = (ev.get("text"), ev.get("ts"), payload.get("event_id"))
    if not (ok and owner and ev.get("user") == owner and chan and ev.get("channel") == chan
            and all(isinstance(f, str) and f for f in fields) and TS_RE.match(ev["ts"])
            and (ev.get("thread_ts") is None or (isinstance(ev["thread_ts"], str)
                                                 and TS_RE.match(ev["thread_ts"])))):
        return False
    try:
        return _route(home, board, ev, payload["event_id"])
    except (OSError, ValueError, brainstorm.Refused) as exc:
        log.warning("brainstorm intake failed: %s", type(exc).__name__)
        return False
