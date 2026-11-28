"""Outgoing notifications: durable queue plus the channel senders (spec §6.5, rows 398,
424, 425)."""
import datetime
import functools
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from capsule import config, db, paths, slack, slack_msgs
from capsule.db import txn

SLACK_FLAG = "outbox.slack"
BACKOFF_KEY = "outbox.backoff:{}"
BACKOFF_CHANNELS = ("slack",)
BACKOFF_BASE = 30
BACKOFF_CAP = 900
SEND_CAP_PER_PASS = 10        # with the 5 s timeout: a pass waits on Slack for at most ~50 s
# Slack allowlist (visibility spec section 6, card 7): only classes that need the owner or
# say something is broken post to Slack.
LOG_ONLY_CLASSES = ("running", "done", "paused", "scheduled")
# `slack.routing: "v2"` (spec 2026-10-29, slice 1): a positive list. Everything else, `blocked`,
# `needs_input` and `incident` included, keeps its `file` row and never posts; an unknown class
# posts nothing. Unset, `LOG_ONLY_CLASSES` rules and an unlisted class posts, as before.
POST_CLASSES = ("ask", "page")      # the ☀ digest (`scheduled`) posts by its marker
ROUTING_FLAG = "outbox.routing"
MAX_ATTEMPTS = {"slack": 12}      # ~2.5 h of backed-off retries before a row goes dead
# D13 (M189, row 635 a): the slack class that always breaks quiet hours: `incident`, the page. A
# `blocked` line waits for the morning unless `_interrupt` lifts it with a word of a QUIET_EVENTS
# entry (factory down or stuck: keeper silent, boards paused). The quiet window reads that entry
# of interrupts.json even with `redesign.interrupts` off; an unreadable table falls back to
# QUIET_FALLBACK, the pass list before row 635 a.
QUIET_INTERRUPTS = ("incident", "page")      # `page` only exists under routing v2
QUIET_EVENTS = ("factory_down_or_stuck",)
# `page` joins the fallback for routing v2. That widens it while routing is unset too, which is
# harmless only because no 🛑 row exists while it is unset.
QUIET_FALLBACK = ("incident", "blocked", "page")
# classes that ask for the owner: a table word may lift a line of these classes, never a done line
ASKING = ("blocked", "needs_input", "incident")
# D13 (redesign spec): with `redesign.interrupts` on, only these events leave at once; the rest
# wait for the batch. The table is a PROTECTED file (a worker may not widen its own whitelist).
# Today only factory_down_or_stuck (incident pages, keeper-silent, boards paused) has real Slack
# emitters; budget_nearly_gone (paused is log-only), security_or_secrets and
# irreversible_action_waiting are table entries whose emitter is pending. The quiet window reads
# the factory_down_or_stuck entry even with the switch off (QUIET_EVENTS above).
INTERRUPTS = Path(__file__).with_name("interrupts.json")
LOCALTIME = Path("/etc/localtime")      # `timezone: "system"` reads this link (macOS)
_zone_warned = False
_table_warned = False


def add(conn, channel, body, *, thread_key=None, dedup_key=None, now=None):
    now = int(now or time.time())
    with txn(conn):
        cur = conn.execute(
            "INSERT INTO outbox (channel, thread_key, body, created_at) VALUES (?,?,?,?)",
            (channel, thread_key, body, now))
        if dedup_key:     # a side table: outbox_row.v1 (contracts/) allows no extra column
            conn.execute("INSERT INTO outbox_key (outbox_id, dedup_key) VALUES (?,?)",
                         (cur.lastrowid, dedup_key))
        return cur.lastrowid


def _flag(conn, key):
    """A mirrored boolean guard value; anything but JSON `true` (unset, not JSON, wrong type) is
    False, the safe default (no Slack post). Non-JSON warns once per call, naming the key."""
    try:
        value, _ = db.guard_get(conn, key)
    except ValueError:
        print(f"capsule outbox: guard_state {key} is not valid JSON; treating it as false",
              file=sys.stderr)
        return False
    return value is True


def slack_enabled(conn):
    return _flag(conn, SLACK_FLAG)


def routing_v2(conn):
    return _flag(conn, ROUTING_FLAG)


def set_slack(conn, on, *, now=None):
    """The tick's per-pass mirror of `cfg["slack"]` (a bool, or the board's `slack` block, which
    also carries `routing`); each flag is written only when it changes."""
    if slack_enabled(conn) != bool(on):
        db.guard_set(conn, SLACK_FLAG, bool(on), now=now)
    if routing_v2(conn) != slack.is_v2(on):
        db.guard_set(conn, ROUTING_FLAG, slack.is_v2(on), now=now)


def digest(*args):
    """The tick's one call into the morning summary / reminders (capsule/slack_digest.py, which
    imports this module, so it is loaded here on first use)."""
    from capsule import slack_digest
    slack_digest.step(*args)


def notify(conn, cls, headline, *, fields=(), reply=None, thread_key=None, file_body=None,
           file=True, key=None, page=False, digest=False, sound=True, now=None):
    """One message of class `cls`: the `file` row (`file_body` or the template on one
    line; none when `file=False`, except a log-only class always has one) and, when
    Slack is on for this board and `cls` is not in `LOG_ONLY_CLASSES`, the `slack`
    row with the template; `key` (slack_msgs.needs_key / broken_key) rides on that
    row so a later notify with the same key edits the posted message. Under routing v2 only
    `POST_CLASSES` post: a `page=True` line (the factory is down or stuck) becomes class `page`;
    `page` changes nothing when v2 is off. `digest=True` on class `scheduled` renders the ☀ body
    (the daily digest, evening close); that marker, not the class, lets it post, so a plain
    `scheduled` line (heal replay, the boundary notice) stays log-only. `page=True` also writes
    one `local` row (the headline, plain: a Mac notification; `sound=False` for none; none when
    `key` already has a row, as the page then only edits its Slack message), with Slack
    off and routing unset."""
    v2 = routing_v2(conn)
    if v2 and page:
        cls = "page"
    try:
        text = slack.render(cls, headline, fields, reply, digest=digest)
    except ValueError as exc:
        print(f"capsule outbox: {cls} line not rendered: {exc}", file=sys.stderr)
        text = None
    ids = []
    again = bool(key) and conn.execute(
        "SELECT 1 FROM outbox_key WHERE dedup_key=?", (key,)).fetchone() is not None
    post = cls in POST_CLASSES if v2 else cls not in LOG_ONLY_CLASSES   # off: unlisted posts
    if cls == "scheduled" and text is not None and text.startswith(slack.SUMMARY_GLYPH):
        post = True      # the ☀ digest
    if (file or not post) and (file_body is not None or text is not None):
        ids.append(add(conn, "file", slack.one_line(text) if file_body is None else file_body,
                       thread_key=thread_key, now=now))
    if text is not None and post and slack_enabled(conn):
        ids.append(add(conn, "slack", text, thread_key=thread_key, dedup_key=key, now=now))
    if page and not again:       # a keyed repeat only edits the Slack message: no new notice
        ids.append(add(conn, "local", " ".join(headline.split()),
                       thread_key=LOCAL_SOUND if sound else None, now=now))
    return ids


def _held_until(conn, channel):
    value, _ = db.guard_get(conn, BACKOFF_KEY.format(channel))
    return value if isinstance(value, dict) else {"until": 0, "failures": 0}


def _hold(conn, channel, until, failures, now):
    db.guard_set(conn, BACKOFF_KEY.format(channel), {"until": int(until), "failures": failures}, now=now)


def backoff(failures):
    return min(BACKOFF_BASE * 2 ** max(failures - 1, 0), BACKOFF_CAP)


def _with_edit(conn, row, now):
    """A keyed row as a dict carrying the message it should edit (edit None: post a new one).
    None when the row is a `✔` with nothing open to edit (its original died, or it was already
    handled): that row is marked delivered here and nothing is sent."""
    key = conn.execute("SELECT dedup_key FROM outbox_key WHERE outbox_id=?",
                       (row["id"],)).fetchone()
    if key is None:
        return row
    req = dict(row, dedup_key=key[0], edit=slack_msgs.edit_target(conn, key[0]))
    if key[0].startswith(slack_msgs.BRAINSTORM_REPLY):   # D229: thread_key "<channel>|<ts>"
        channel, _, ts = (row["thread_key"] or "").partition("|")
        if not (channel and ts):
            with txn(conn):
                conn.execute("UPDATE outbox SET status='dead', last_error='no thread target'"
                             " WHERE id=?", (row["id"],))
            return None
        req["edit"], req["thread"] = None, {"channel": channel, "ts": ts}
        return req
    remind = key[0].startswith(slack_msgs.REMIND)
    if remind:      # a thread reply under the message it reminds about; none left open: drop it
        req["thread"] = slack_msgs.edit_target(conn, key[0][len(slack_msgs.REMIND):])
    if (remind and req["thread"] is None) or (
            req["edit"] is None and req["body"].startswith(slack_msgs.HANDLED)):
        with txn(conn):
            conn.execute("UPDATE outbox SET status='delivered', delivered_at=? WHERE id=?",
                         (now, row["id"]))
        return None
    return req


def _remember(conn, req, now):
    """Store where a keyed row landed (the sender leaves `slack_channel` and `slack_ts` on
    `req`); a `✔` text closes the message."""
    landed = isinstance(req, dict) and req.get("slack_ts") and req.get("slack_channel")
    if not landed or req.get("thread"):
        return
    key, text = req["dedup_key"], req["body"]
    state = "handled" if text.startswith(slack_msgs.HANDLED) else "open"
    conn.execute(
        "INSERT INTO slack_msg (key, kind, channel, ts, state, text, posted_at, updated_at)"
        " VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET channel=excluded.channel,"
        " ts=excluded.ts, state=excluded.state, text=excluded.text, updated_at=excluded.updated_at,"
        " reminded_at=CASE WHEN slack_msg.state='handled' THEN NULL ELSE slack_msg.reminded_at END,"
        " posted_at=CASE WHEN slack_msg.state='handled' THEN excluded.posted_at"
        " ELSE slack_msg.posted_at END",
        (key, key.split(":", 1)[0], req["slack_channel"], req["slack_ts"], state, text, now, now))


def is_posted_or_queued(conn, key):
    """True when `key` has an open posted message or a row still waiting to be sent."""
    queued = conn.execute("SELECT 1 FROM outbox_key k JOIN outbox o ON o.id=k.outbox_id"
                          " WHERE k.dedup_key=? AND o.status='pending' LIMIT 1", (key,)).fetchone()
    return queued is not None or slack_msgs.edit_target(conn, key) is not None


def finish(conn, key, line, now):
    """Enqueue the `✔` edit for `key` when its message is posted or still queued; else nothing."""
    if not is_posted_or_queued(conn, key):
        return None
    return add(conn, "slack", line, dedup_key=key, now=now)


def _minutes(hhmm):
    hh, mm = hhmm.split(":")
    return int(hh) * 60 + int(mm)


def _quiet_now(now_local, cfg):
    """True when `now_local` (a datetime already in the configured zone) falls in the
    `quiet_hours` window [start, end), which may wrap midnight. False when off."""
    cur = config.quiet_hours(cfg)
    if not cur["enabled"]:
        return False
    at = now_local.hour * 60 + now_local.minute
    start, end = _minutes(cur["start"]), _minutes(cur["end"])
    return start <= at < end if start < end else at >= start or at < end


def _quiet_zone(name):
    """The ZoneInfo for `quiet_hours.timezone`. "system" re-reads the OS zone on every call (the
    LOCALTIME link target after `zoneinfo/`, else a valid $TZ) so the window follows the Mac
    when it travels; None when neither resolves, warned once (the caller then sends: fail open)."""
    global _zone_warned
    if name != config.SYSTEM_ZONE:
        return ZoneInfo(name)
    try:
        linked = os.readlink(LOCALTIME).partition("zoneinfo/")[2]
    except OSError:
        linked = ""
    for cand in (linked, os.environ.get("TZ", "")):
        try:
            zone = ZoneInfo(cand)
        except (ZoneInfoNotFoundError, ValueError, OSError):
            continue
        _zone_warned = False             # a later failure warns again
        return zone
    if not _zone_warned:
        _zone_warned = True
        print("capsule outbox: quiet_hours.timezone 'system' did not resolve; "
              "quiet hours are off until it does", file=sys.stderr)
    return None


def _events():
    """{event: {"classes": [...], "words": [...]}} from `interrupts.json`, words lowercased; None
    when the file is missing or malformed (a list that is not all strings counts), warned once
    (the caller then falls back: quiet hours with QUIET_FALLBACK, everything by day)."""
    global _table_warned
    try:
        events = json.loads(INTERRUPTS.read_text(encoding="utf-8"))["events"]
        ok = isinstance(events, dict) and bool(events) and all(
            isinstance(e, dict) and all(
                isinstance(e.get(k), list) and all(isinstance(x, str) for x in e[k])
                for k in ("classes", "words")) for e in events.values())
    except (OSError, ValueError, KeyError, TypeError):
        ok = False
    if ok:
        return {n: {"classes": e["classes"], "words": [w.lower() for w in e["words"]]}
                for n, e in events.items()}
    if not _table_warned:
        _table_warned = True
        print(f"capsule outbox: {INTERRUPTS.name} unreadable; the whitelist is off and quiet "
              "hours use the fallback list (incident, blocked, page) until it is",
              file=sys.stderr)
    return None


def _interrupt(body, events):
    """True when the slack message `body` is a D13 event: its class is listed for an event, or it
    asks for the owner (`ASKING` classes only: a done or running line is never lifted by a word)
    and its headline (first line) has one of the event's words as a whole word."""
    cls, head = slack.classify(body), body.partition("\n")[0].lower()
    return any(cls in ev["classes"] or (cls in ASKING and any(
        re.search(rf"\b{re.escape(w)}\b", head) for w in ev["words"])) for ev in events.values())


def _already_sent(conn, row):
    """True when the row edits, or replies under, a message that is already posted and open."""
    key = conn.execute("SELECT dedup_key FROM outbox_key WHERE outbox_id=?",
                       (row["id"],)).fetchone()
    return key is not None and slack_msgs.edit_target(
        conn, key[0].removeprefix(slack_msgs.REMIND)) is not None


def _passes(conn, row, events):
    """With the whitelist on: a D13 event, a `page` (row 638 d: at any hour), the morning summary
    (the batch itself), a `✔` edit or a row that edits a message already posted."""
    body = row["body"]
    return (_interrupt(body, events) or slack.classify(body) == "page"
            or body.startswith((slack.SUMMARY_GLYPH, slack_msgs.HANDLED))
            or _already_sent(conn, row))


def _releasable(conn, rows, cfg, now):
    """A brainstorm reply (D229) always goes: the owner is talking to it, at any hour."""
    talk = {r["id"] for r in rows if r["channel"] == "slack" and conn.execute(
        "SELECT 1 FROM outbox_key WHERE outbox_id=? AND dedup_key LIKE ?",
        (r["id"], slack_msgs.BRAINSTORM_REPLY + "%")).fetchone()}
    rest = _releasable_rest(conn, [r for r in rows if r["id"] not in talk], cfg, now)
    return [r for r in rows if r["id"] in talk or r["id"] in {x["id"] for x in rest}]


def _releasable_rest(conn, rows, cfg, now):
    """`rows` minus the slack rows that must wait. With `redesign.interrupts` on, only what
    `_passes` lets through goes, at any hour. Otherwise unchanged unless the quiet window is on
    at epoch `now`; file rows, the `QUIET_INTERRUPTS` and the `QUIET_EVENTS` lines (`_interrupt`)
    pass. An unreadable event table falls back to the old pass list (`QUIET_FALLBACK`)."""
    events = _events() if config.redesign(cfg)["interrupts"] else None
    if events:
        return [r for r in rows if r["channel"] != "slack" or _passes(conn, r, events)]
    cur = config.quiet_hours(cfg)
    zone = _quiet_zone(cur["timezone"]) if cur["enabled"] else None   # None: send
    if zone is None or not _quiet_now(datetime.datetime.fromtimestamp(now, zone), cfg):
        return rows
    table = _events()
    night = {k: v for k, v in (table or {}).items() if k in QUIET_EVENTS}
    passing = QUIET_INTERRUPTS if table else QUIET_FALLBACK
    return [r for r in rows if r["channel"] != "slack"
            or slack.classify(r["body"]) in passing or _interrupt(r["body"], night)]


def flush(conn, send, *, max_attempts=5, now=None, quiet=None):
    now = int(now or time.time())       # `quiet`: the board config (_releasable)
    delivered = 0
    ceiling = max([max_attempts] + [MAX_ATTEMPTS.get(c, max_attempts) for c in BACKOFF_CHANNELS])
    rows = _releasable(conn, conn.execute(
        "SELECT * FROM outbox WHERE status='pending' AND attempts < ? ORDER BY id",
        (ceiling,)).fetchall(), quiet, now)
    state, sent = {}, {}
    for row in rows:
        ch = row["channel"]
        limit = MAX_ATTEMPTS.get(ch, max_attempts)
        if row["attempts"] >= limit:
            continue
        gated = ch in BACKOFF_CHANNELS
        if gated:
            if ch not in state:
                state[ch] = _held_until(conn, ch)
            if state[ch]["until"] > now or sent.get(ch, 0) >= SEND_CAP_PER_PASS:
                continue
            sent[ch] = sent.get(ch, 0) + 1
        req = _with_edit(conn, row, now)
        if req is None:
            continue
        with txn(conn):
            attempts = row["attempts"] + 1
            conn.execute("UPDATE outbox SET attempts=? WHERE id=?", (attempts, row["id"]))
        try:
            send(req)
        except slack.Deferred as exc:
            # Slack asked us to wait: not the message's fault, so the attempt is given back.
            with txn(conn):
                conn.execute("UPDATE outbox SET attempts=?, last_error=? WHERE id=?",
                             (row["attempts"], str(exc)[:500], row["id"]))
            if gated:
                state[ch] = {"until": now + exc.retry_after, "failures": state[ch]["failures"]}
                _hold(conn, ch, state[ch]["until"], state[ch]["failures"], now)
        except slack.Rejected as exc:
            # Slack refused this one message for good: only this row dies, the channel is not held.
            with txn(conn):
                conn.execute("UPDATE outbox SET status='dead', last_error=? WHERE id=?",
                             (str(exc)[:500], row["id"]))
        except Exception as exc:
            status = "dead" if attempts >= limit else "pending"
            with txn(conn):
                conn.execute("UPDATE outbox SET status=?, last_error=? WHERE id=?",
                             (status, str(exc)[:500], row["id"]))
            if gated:
                failures = state[ch]["failures"] + 1
                state[ch] = {"until": now + backoff(failures), "failures": failures}
                _hold(conn, ch, state[ch]["until"], failures, now)
        else:
            with txn(conn):
                conn.execute("UPDATE outbox SET status='delivered', delivered_at=? WHERE id=?",
                             (now, row["id"]))
                _remember(conn, req, now)
            delivered += 1
            if gated and (state[ch]["failures"] or state[ch]["until"]):
                state[ch] = {"until": 0, "failures": 0}
                _hold(conn, ch, 0, 0, now)
    return delivered


def send(row, *, home=None, path=None):
    """The `file` channel sender: appends `<ts>\\t<body>` to `path`, or `$CAPSULE_HOME/outbox.log` when
    `path` is not given."""
    target = path or (home or paths.home()) / "outbox.log"
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "a", encoding="utf-8") as fh:
        fh.write(f"{int(time.time())}\t{row['body']}\n")


LOCAL_SOUND = "sound"       # a `local` row with this thread_key plays a sound (the first page only)
LOCAL_TIMEOUT = 10
OSASCRIPT = "/usr/bin/osascript"


def send_local(row, *, home=None, path=None):
    """The `local` channel sender: one macOS notification (`osascript`, 10 s limit). Best effort:
    a missing tool, a non-Mac host or a failed command is one stderr line, never an exception, so
    the row counts as delivered and the next page (30 min later) is the retry. Under
    `AGENT_OPS_NO_SEND` no command runs; one JSON line goes to `local-no-send.jsonl` beside the
    board's `outbox.log` (or in the home)."""
    sound = row["thread_key"] == LOCAL_SOUND
    if os.environ.get("AGENT_OPS_NO_SEND"):
        base = Path(path).parent if path else (home or paths.home())
        target = base / "local-no-send.jsonl"
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "a", encoding="utf-8") as fh:
            note = {"at": int(time.time()), "body": row["body"], "sound": sound}
            fh.write(json.dumps(note) + "\n")
        return
    if sys.platform != "darwin":
        return
    script = ["on run argv", 'display notification (item 1 of argv) with title "Capsule"'
              + (' sound name "Glass"' if sound else ""), "end run"]
    cmd = [OSASCRIPT] + [a for line in script for a in ("-e", line)] + [row["body"][:300]]
    try:
        done = subprocess.run(cmd, capture_output=True, timeout=LOCAL_TIMEOUT, check=False)
        if done.returncode:
            print(f"capsule outbox: local notice failed rc={done.returncode}", file=sys.stderr)
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"capsule outbox: local notice failed: {type(exc).__name__}", file=sys.stderr)


class Router:
    """The tick's `send`: one sender per channel. The Slack sender is rebuilt only when the
    `slack` block changes, so its token is fetched once per process, not once per row."""

    def __init__(self, *, home=None, log_path=None, slack_factory=None):
        self.home = home
        self.log_path = log_path
        self._factory = slack_factory or functools.partial(slack.Sender, home=home)
        self._slack = None
        self._slack_cfg = None

    def configure(self, cfg):
        block = slack.block(cfg)
        if block is not None and block.get("board_prefix"):
            block = dict(block, prefix=f"[{cfg['board']}] ")
        if block != self._slack_cfg:
            self._slack_cfg = block
            self._slack = None if block is None else self._factory(block)
        return self

    def __call__(self, row):
        if row["channel"] == "file":
            return send(row, home=self.home, path=self.log_path)
        if row["channel"] == "local":
            return send_local(row, home=self.home, path=self.log_path)
        if row["channel"] == "slack":
            if self._slack is None:
                raise slack.SendError("slack channel not configured")
            return self._slack(row)
        raise ValueError(f"unknown outbox channel {row['channel']!r}")
