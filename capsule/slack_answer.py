"""D225 slice 1: turn one Slack answer (a button click or a threaded typed reply) into a decision
record. `answer()` stays pure (no network, no Slack SDK, no tokens: the caller hands in the
verified user id and the raw value or text); `main` is the listener's process entry
(`python -m capsule.slack_answer start|check|event`).

Trust boundary (spec 2026-10-29 section 7, D113, D125): Slack text is data. What an answer DOES
comes only from the ask file `cos ask` wrote (`<board>/asks/<key>.json`: choices with a fixed verb
and fixed text). This module acts on `(key, choice)` only when the user id is the one
`slack_owner_id` in the host config, the ask is still open and not stale, the choice is one the ask
offered, and the verb is `unblock`, `reset` or `park`. Every other verb is refused with "answer in
the terminal"; epic approval is the owner's GitHub merge of the PR, so Slack has no approval path.

The one write is `human.write`, after the ask is hard-linked into asks/done/<key>.json (first wins).
"""
import contextlib
import json
import logging
import os
import re
import shlex
import sqlite3
import sys
import tempfile
import time
from collections import namedtuple
from pathlib import Path

from capsule import answer_base as base, human

log = logging.getLogger("capsule.slack_answer")

VALUE_RE = re.compile(r"ask:([0-9a-f]{16}):(yes|no|later)\Z")
USER_RE = re.compile(r"[UW][A-Z0-9]{2,20}\Z")
BS_RE = re.compile(r"bs:([0-9a-f]{16}):(open|queue)\Z")    # brainstorm_store.ID_RE's shape
KEY_RE = re.compile(r"[0-9a-f]{16}\Z")
ALLOWED_VERBS = ("unblock", "reset", "park")
NOTE_MAX = 500
TERMINAL = "That one needs a chat. Answer it in the terminal."
HANDLED = "That one was already handled."
ASK_KEY_PREFIX = "needs:ask-"          # the outbox key prefix of a `cos ask` post
CHANNEL_ID_RE = re.compile(r"[CG][A-Z0-9]{6,}\Z")

# reason is "" when accepted; recorded is the verb or "nothing"; reply "" is dropped silently.
Result = namedtuple("Result", "accepted reason recorded reply")


def value(key, choice):
    """The button value: `ask:<key>:<yes|no|later>`."""
    if not (KEY_RE.match(key) and choice in ("yes", "no", "later")):
        raise ValueError("bad ask key or choice")
    return f"ask:{key}:{choice}"


def _parse_value(raw):
    """(key, choice) from a button value; ValueError for anything else."""
    m = VALUE_RE.match(raw) if isinstance(raw, str) else None
    if not m:
        raise ValueError("not an ask button value")
    return m.group(1), m.group(2)


def _parse_reply(text):
    """(choice, option letter, note) for a typed `yes|no|later|option <x>|no: <note>`, else None."""
    t = " ".join(str(text or "").split())
    m = re.match(r"(?:(yes|no|later)|option ([a-z])|no:\s*(.+))\Z", t, re.I)
    if m is None:
        return None
    word, letter, note = m.groups()
    if word:
        return word.lower(), None, None
    return ("option", letter.lower(), None) if letter else ("no", None, note[:NOTE_MAX])


def _who(user_id):
    """A Slack user id fit for a log line, else "unknown"."""
    return user_id if isinstance(user_id, str) and USER_RE.match(user_id) else "unknown"


def _owner(home):
    """The one Slack user id allowed to answer: `slack_owner_id` in the host config, else None."""
    try:
        owner = json.loads(base.config_path(home).read_text()).get("slack_owner_id")
    except (OSError, ValueError, AttributeError):
        return None
    return owner if isinstance(owner, str) and USER_RE.match(owner) else None


def _find(home, key):
    """(board, open ask path) for `key`; (board, None) if only done; else (None, None)."""
    for board_dir in sorted((Path(home) / "boards").glob("*")):
        if (board_dir / "asks" / f"{key}.json").is_file():
            return board_dir.name, board_dir / "asks" / f"{key}.json"
        if (board_dir / "asks" / "done" / f"{key}.json").is_file():
            return board_dir.name, None
    return None, None


def _stale(home, board, doc):
    """Why the ask no longer applies, or None: the card left its asked status, or a block event
    newer than the ask's watermark exists. An unreadable board is a reason."""
    try:
        with contextlib.closing(base.connect_ro_tolerant(base.board_db(board, home))) as conn:
            return _moved_on(conn, doc)
    except (sqlite3.Error, OSError):
        return "could not check the card"


def _moved_on(conn, doc):
    row = conn.execute("SELECT status FROM tasks WHERE id=?", (doc.get("task_id"),)).fetchone()
    if row is None or row[0] != doc.get("expected_status"):
        return "the card has moved on"
    kinds = (*base.BLOCK_OPEN, *base.BLOCK_CLOSE, base.ACCEPT_CLOSE_KIND)
    newer = conn.execute("SELECT 1 FROM task_events WHERE task_id=? AND id > ? AND kind IN"
                         f" ({','.join('?' * len(kinds))}) LIMIT 1",
                         (doc["task_id"], doc.get("after_event_id") or 0, *kinds)).fetchone()
    return "the card changed since it was asked" if newer else None


def _refuse(reason, reply):
    log.warning("slack answer refused: %s", reason)
    return Result(False, reason, "", reply)


def _resolve(doc, choice, letter):
    """The ask's choice picked; `option <x>` is the one whose text STARTS `option <x> chosen (`."""
    if choice != "option":
        return choice
    start = re.compile(rf"option {re.escape(letter)} chosen \(")
    hits = [k for k, c in doc["choices"].items()
            if isinstance(c, dict) and start.match(str(c.get("text")))]
    return hits[0] if len(hits) == 1 else None


def _record(home, board, path, doc, pick, user_id, via, note, now):
    """Close the ask into asks/done (first answer wins) and write the human file."""
    done = path.parent / "done" / path.name
    final = dict(doc, choice=pick, answered_by=user_id, via=via, answered_at=now)
    if note:
        final["note"] = note
    tmp = None
    try:
        done.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=done.parent, prefix=f".{path.stem}.")    # one per attempt
        with os.fdopen(fd, "w") as fh:
            fh.write(json.dumps(final, indent=2) + "\n")
        os.link(tmp, done)    # fails when it exists: a second click lands here
    except FileExistsError:
        return Result(False, "already answered", "", HANDLED)
    except OSError as exc:
        return _refuse(f"could not record: {exc}", "Could not save that. Try again.")
    finally:
        if tmp:
            Path(tmp).unlink(missing_ok=True)
    choice = doc["choices"][pick]
    try:
        _write_human(home, board, doc, choice, now)
    except (OSError, ValueError) as exc:
        done.unlink(missing_ok=True)    # nothing was answered: leave the ask open
        return _refuse(f"could not record: {exc}", "Could not save that. Try again.")
    path.unlink(missing_ok=True)
    log.info("slack answer recorded: %s %s by %s", choice["verb"], doc.get("key"), user_id)
    return Result(True, "", choice["verb"], "Saved. The assistant will act on it.")


def _write_human(home, board, doc, choice, at):
    """The one human file for an answered ask, marked `via: slack` and `ask: <key>`."""
    return human.write(home, board, doc["task_id"], choice["verb"], choice["text"], now=at,
                       author="owner", via="slack", ask=doc.get("key"))


def _has_human(home, board, key, doc, choice, at):
    """True when a human file (open or done/) is this answer's: `ask == key`, or an older one with
    the same task, verb, text and second."""
    d = base.human_dir(home, board)
    for sub in (d, d / "done"):
        for p in sub.glob("*.json") if sub.is_dir() else ():
            try:
                h = json.loads(p.read_text())
            except (OSError, ValueError):
                continue
            if isinstance(h, dict) and (h.get("ask") == key or (
                    (h.get("task_id"), h.get("kind"), h.get("text"), h.get("at"))
                    == (doc["task_id"], choice["verb"], choice["text"], at))):
                return True
    return False


def _repairable(doc, key):
    """(choice, at) when an asks/done record is a real answer whose human file may be missing."""
    if not isinstance(doc, dict):
        return None
    choices = doc.get("choices")
    choice = choices.get(doc.get("choice")) if isinstance(choices, dict) else None
    at = doc.get("answered_at")
    if not (doc.get("key") == key and doc.get("task_id") and isinstance(choice, dict)
            and choice.get("verb") in ALLOWED_VERBS and str(choice.get("text") or "").strip()
            and isinstance(at, int) and not isinstance(at, bool)
            and "closed" not in doc and doc.get("via") in ("button", "typed")
            and isinstance(doc.get("answered_by"), str) and USER_RE.match(doc["answered_by"])):
        return None
    return choice, at


def _repair_one(home, board, p):
    """True when done record `p`'s human file was written now; the orphan open ask is dropped."""
    try:
        doc = json.loads(p.read_text())
    except (OSError, ValueError):
        return False
    found = _repairable(doc, p.stem)
    if found is None:
        return False
    wrote = False
    if not _has_human(home, board, p.stem, doc, *found):
        why = _stale(home, board, doc)
        if why:
            log.warning("slack answer repair skipped %s: stale: %s", p.stem, why)
            return False
        try:
            _write_human(home, board, doc, found[0], found[1])
        except (OSError, ValueError) as exc:
            log.warning("slack answer repair failed for %s: %s", p.stem, exc)
            return False
        log.info("slack answer repaired: %s %s", found[0]["verb"], p.stem)
        wrote = True
    with contextlib.suppress(OSError):
        (p.parent.parent / p.name).unlink(missing_ok=True)
    return wrote


def repair(home):
    """Crash repair, BEFORE the listener connects: a done ask with no human file gets it now unless
    the card moved on. Never re-executes anything; idempotent. Returns the keys written."""
    return [p.stem for board_dir in sorted((Path(home) / "boards").glob("*"))
            for p in sorted((board_dir / "asks" / "done").glob("*.json"))
            if _repair_one(home, board_dir.name, p)]


def _answer(home, user_id, key, choice, letter, note, via, now):
    if not (isinstance(key, str) and KEY_RE.match(key)):
        return _refuse("bad ask key", "That question is not valid.")
    board, path = _find(home, key)
    if path is None:
        return (_refuse("already answered", HANDLED) if board
                else _refuse("no such question", "That question is gone."))
    try:
        doc = json.loads(path.read_text())
    except (OSError, ValueError):
        doc = None
    if (not isinstance(doc, dict) or doc.get("key") != key or not doc.get("task_id")
            or not isinstance(doc.get("choices"), dict)):
        return _refuse("malformed ask file", "That question is not valid.")
    if choice == "later":
        return Result(True, "", "nothing", "Okay, I will ask again later.")    # an ack only
    pick = _resolve(doc, choice, letter)
    offered = doc["choices"].get(pick) if pick else None
    if not isinstance(offered, dict):
        return _refuse("choice not offered by this question", "That is not one of the options.")
    if offered.get("verb") not in ALLOWED_VERBS:
        return _refuse(f"verb {offered.get('verb')!r} is not answerable from Slack", TERMINAL)
    if not str(offered.get("text") or "").strip():
        return _refuse("the ask has no fixed text", "That question is not valid.")
    why = _stale(home, board, doc)
    if why:
        return _refuse(f"stale: {why}", HANDLED)
    return _record(home, board, path, doc, pick, user_id, via, note, int(now))


def _apply(home, user_id, button_value, key, text, now):
    if button_value is not None:
        try:
            key, choice = _parse_value(button_value)
        except ValueError:
            return _refuse("bad button value", "That button is not valid.")
        return _answer(home, user_id, key, choice, None, None, "button", now)
    parsed = _parse_reply(text)
    if parsed is None:
        return _refuse("not in the answer grammar", TERMINAL)
    return _answer(home, user_id, key, parsed[0], parsed[1], parsed[2], "typed", now)


def answer(home, user_id, *, button_value=None, key=None, text=None, now=None):
    """Apply one Slack answer, return a Result, never raise. A click gives `button_value`; a reply
    gives `key` (from the `needs:ask-<key>` row, never the text) and `text`, refused outside the
    grammar. The owner check runs first: a stranger gets an empty reply."""
    try:
        owner = _owner(home)
        if not (isinstance(user_id, str) and owner and user_id == owner):
            log.warning("slack answer refused: user %s is not the owner", _who(user_id))
            return Result(False, "unknown user", "", "")      # dropped silently
        return _apply(home, user_id, button_value, key, text, now or time.time())
    except Exception:    # backstop: the listener must always get a Result
        log.exception("slack answer failed")
        return Result(False, "internal error", "", "Could not save that. Try again.")


def _brainstorm_click(home, user_id, raw):
    """Result for a handoff button (`bs:<id>:open|queue`, M203): owner only, intake only. It
    answers no ask, changes no decision and deletes nothing: `queue` puts the brainstorm in the
    morning brief's waiting block, `open` replies with the note and terminal lines."""
    owner = _owner(home)
    if not (isinstance(user_id, str) and owner and user_id == owner):
        log.warning("slack brainstorm click refused: user %s is not the owner", _who(user_id))
        return Result(False, "unknown user", "", "")
    hit = BS_RE.match(raw) if isinstance(raw, str) else None
    if hit is None:
        return _refuse("bad button value", "That button is not valid.")
    bid, act = hit.groups()
    found = sorted(Path(home).glob(f"boards/*/brainstorms/{bid}/meta.json"))
    if not found:
        return _refuse("unknown brainstorm", "That brainstorm is gone.")
    meta, board = json.loads(found[0].read_text(encoding="utf-8")), found[0].parents[2].name
    if act == "open":
        ws = Path(home) / "boards" / board / "brainstorms" / bid / "ws"
        note = meta.get("note") or "not written yet: it appears when the brainstorm is closed"
        sid = meta.get("session_id")
        if not sid:
            return Result(True, "", act, "That brainstorm has not started yet.")
        return Result(True, "", act, "\n".join([
            f"Note: {note}", f"cd {shlex.quote(str(ws))}",
            f"claude --resume {shlex.quote(str(sid))}", "One terminal only."]))
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "maintenance"))
    import messages    # only here: it pulls an open module in
    # the defer queue dedups on its own key, so a second click changes nothing; the brief reads it
    messages.defer_actionable(f"Brainstorm - {meta['title']}", board=board, task_id=bid)
    return Result(True, "", act, "queued for the morning brief")


def _ask_key(home, channel, ts):
    """The `cos ask` key of the message at (channel, ts) (its `needs:ask-<key>` row), else None."""
    for path in sorted((Path(home) / "boards").glob("*/kanban.db")):
        try:
            with contextlib.closing(base.connect_ro_tolerant(path)) as conn:
                row = conn.execute("SELECT key FROM slack_msg WHERE channel=? AND ts=?",
                                   (channel, ts)).fetchone()
        except sqlite3.Error:
            continue
        if row and str(row[0]).startswith(ASK_KEY_PREFIX):
            return str(row[0])[len(ASK_KEY_PREFIX):]


def _reply_target(payload):
    ev, msg = payload.get("event") or {}, payload.get("message") or {}
    if payload.get("type") == "block_actions":
        ev = {"channel": (payload.get("channel") or {}).get("id"),
              "thread_ts": msg.get("thread_ts") or msg.get("ts")}
    channel, ts = ev.get("channel"), ev.get("thread_ts")
    return (channel, ts) if channel and ts else None


def _board():
    return os.environ.get("CAPSULE_BOARD") or "capsule-corp"   # asks post from the board config


def _block(home):
    """The board's `slack` config block (None when Slack is off); raises on an unusable config."""
    cfg = json.loads(base.resolve_config(home, _board()).read_text())
    if not isinstance(cfg, dict):
        raise ValueError("config: not an object")
    return base.block(cfg)


def _block_or_none(home):
    try:
        return _block(home)
    except (OSError, ValueError, KeyError):
        return None


def _handle(home, payload):
    """Answer one payload (a click or a thread reply), post the reply: {"ok": accepted}."""
    if base.source_ok(home, payload) is False:
        return {"ok": False}                        # another workspace or app: nothing at all
    if payload.get("type") == "block_actions":
        act = (payload.get("actions") or [{}])[0]
        who, raw = (payload.get("user") or {}).get("id"), act.get("value")
        res = (_brainstorm_click(home, who, raw) if isinstance(raw, str) and raw.startswith("bs:")
               else answer(home, who, button_value=raw))
    else:
        ev = payload.get("event") or {}
        if (blk := _block_or_none(home)) and ev.get("channel") == (
                blk["channels"].get("brainstorm") or None):      # never an ask answer
            from capsule import brainstorm_intake    # only here: it pulls open modules in
            return {"ok": brainstorm_intake.handle(home, _board(), blk, payload, _owner(home))}
        key = _ask_key(home, ev.get("channel"), ev.get("thread_ts"))
        if key is None:
            return {"ok": False}                    # a thread that is not an ask: not ours
        res = answer(home, ev.get("user"), key=key, text=ev.get("text"))
    if res.reply and (target := _reply_target(payload)):
        row = {"thread": {"channel": target[0], "ts": target[1]}, "body": str(res.reply)}
        state = "answered" if res.accepted else "refused"
        try:
            if os.environ.get("AGENT_OPS_NO_SEND"):    # nothing reaches Slack: record the reply
                base.capture(_board(), home, *target, row["body"], state)
            else:
                from capsule import slack    # after the decision: it pulls open modules in
                slack.Sender(_block(home), home=home)(row)
        except Exception as exc:
            log.warning("reply failed: %s", type(exc).__name__)
    return {"ok": res.accepted}


def _start(home, fix=True):
    """{"off"|"error": why} with nothing to listen for, else repair once, name the identity."""
    try:
        blk = _block(home)
        if blk is None:
            return {"off": "slack is off in the config; nothing to listen for"}
        inbox, identity = str((blk["channels"].get("inbox") or "")).strip(), blk["identity"]
        allowed = base._allowed(blk)
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        return {"error": f"no usable slack config: {type(exc).__name__}"}
    if not CHANNEL_ID_RE.match(inbox) or inbox not in allowed:
        return {"off": "answers need slack.channels.inbox as a channel ID"}
    try:
        fixed = repair(home) if fix else []
    except Exception as exc:       # repair never raises by contract; a failure still starts us
        log.warning("start repair failed: %s", type(exc).__name__)
        fixed = []
    return {"identity": identity, "repaired": len(fixed), "owner": _owner(home)}


def main(argv=None):
    cmd, home = (sys.argv[1:] if argv is None else argv)[:1], base.home(None)
    if cmd == ["start"]:
        out = _start(home)
    elif cmd == ["check"]:                     # AGENT_OPS_NO_SEND: config checks only, no repair
        out = _start(home, fix=False)
    elif cmd == ["repair"]:                    # by hand: the start's repair alone
        out = {"repaired": repair(home)}
    elif cmd == ["event"]:
        try:
            out = _handle(home, json.load(sys.stdin))
        except Exception as exc:   # never raises: the listener reads the line, not the traceback
            log.warning("event failed: %s", type(exc).__name__)
            out = {"ok": False}
    else:
        return 2
    print(json.dumps(out), flush=True)
    return 1 if cmd == ["start"] and "error" in out else 0


if __name__ == "__main__":
    sys.exit(main())
