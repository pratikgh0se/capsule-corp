"""Brainstorm runner (M204 slice B2, D229/D232): answers the owner's Slack brainstorm threads.

Layout (shared with intake and open/close): `<home>/boards/<board>/brainstorms/<id>/` holds
`meta.json`, the append-only `turns.jsonl` and `pending/<event_id>.json` (written by the
listener's intake). One run takes the one-instance lock, then for each open brainstorm batches
its pending messages into ONE model call and queues the reply through the outbox
(key `brainstorm-reply:<id>:<n>`, thread target from meta only).

The model gets no tools, no MCP, no `--add-dir`, an empty `ws/` and the fixed prompt
`capsule/prompts/brainstorm.md`. Owner text is data inside `<owner_message>` tags. The notes
vault is searched read-only by Capsule's own vault-context lookup and the top notes are pasted in
as data; a lookup failure just means no notes. Run: `python3 -m capsule.brainstorm_run`
(launchd job `capsule-brainstorm`, listed in `launchd/unloaded`)."""
import fcntl
import json
import os
import re
import sys
import time
import uuid
from pathlib import Path

from capsule import brainstorm, db, outbox, paths, safe_write, slack, slack_msgs, vault_context
from capsule import brainstorm_store as store
from capsule.models import adapter_for


DAILY_CAP, TURN_CAP, WEEK_PAUSE_PCT = 100, 60, 90     # D232; spec; owner decision 2026-11-03
REPLY_MAX, NOTES_MAX, TEXT_MAX = 3000, 3, 12000
PROMPT = Path(__file__).with_name("prompts") / "brainstorm.md"
ROUTE = {"provider": "claude", "account": "default", "model": "claude-sonnet-5", "fallback": None,
         "turn_budget": 2, "vault_read": "none"}
SCHEMA = {"type": "object", "additionalProperties": False, "required": ["reply"],
          "properties": {"reply": {"type": "string", "maxLength": REPLY_MAX}}}
TIMEOUT = 180
LINES = {
    "daily": "I have reached today's limit of brainstorm replies. I will pick this up tomorrow.",
    "turns": "This brainstorm has reached its limit of 60 turns. Say `done` to close it, then "
             "continue in the terminal or start a new one.",
    "paused": "Capsule is paused until the weekly usage limit resets. I will answer after that.",
}


def _read(path):
    try:
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) else None


def _write(path, doc):
    safe_write.atomic_write(Path(path).parent, path, json.dumps(doc, sort_keys=True))


def _fence(text):
    """Owner or note text cannot close the data tags the prompt opens."""
    return re.sub(r"<(/?\s*(?:owner_message|vault_notes))", r"‹\1", text, flags=re.I)


def _vault_notes(text, cfg):
    """The top few notes for `text` as one delimited block, read-only, via the existing
    vault-context lookup (D33/row 626). Any failure is no notes."""
    try:
        first = text.strip().splitlines()[0] if text.strip() else ""
        rec = vault_context.retrieve({"title": first[:200], "body": text}, cfg)
        notes = [k for k in rec["kept"] if k.get("path")][:NOTES_MAX]
        return vault_context.render({**rec, "kept": notes, "withheld": []}) if notes else ""
    except Exception:
        return ""


def _build_prompt(owner_text, notes):
    block = f"<vault_notes>\n{_fence(notes)}\n</vault_notes>\n\n" if notes else ""
    return f"{block}<owner_message>\n{_fence(owner_text)}\n</owner_message>\n"


def week_pct():
    """Weekly usage percent, or None when unknown (usage_guard.usage_window)."""
    try:
        import usage_guard
        return usage_guard.usage_window()[2]
    except Exception:
        return None


class _Run:
    """One pass. `adapter` (a StubAdapter in tests), `lookup`, `usage` and `flush` are the seams."""

    def __init__(self, home, board, *, adapter=None, lookup=_vault_notes, usage=week_pct,
                 flush=None, clock=time.time):
        self.home, self.board, self.clock = Path(home), board, clock
        self.root = paths.board_dir(board, self.home) / "brainstorms"
        self.cfg = _read(paths.config_path(self.home, board)) or {}
        self.cfg.setdefault("board", board)
        self.adapter = adapter or adapter_for(
            {**ROUTE, **(self.cfg.get("routing") or {}).get("brainstorm", {})})
        self.lookup, self.usage, self.flush_fn = lookup, usage, flush
        self.conn = None

    def day(self):
        return time.strftime("%Y-%m-%d", time.localtime(self.clock()))

    def used_today(self):
        doc = _read(self.root / "_day.json") or {}
        return doc.get("n", 0) if doc.get("date") == self.day() else 0

    def _count(self):
        _write(self.root / "_day.json", {"date": self.day(), "n": self.used_today() + 1})

    def _queue(self, bid, meta, turn):
        key = f"{slack_msgs.BRAINSTORM_REPLY}{bid}:{turn['n']}"
        # any row of this key, sent or dead too: is_posted_or_queued would let a delivered turn
        # be queued again
        if self.conn.execute("SELECT 1 FROM outbox_key WHERE dedup_key=?", (key,)).fetchone():
            return
        target = f"{meta.get('channel') or ''}|{meta.get('thread_ts') or ''}"
        outbox.add(self.conn, "slack", turn["text"], thread_key=target, dedup_key=key,
                   now=self.clock())

    def _say(self, bid, meta, turns, text, **extra):
        n = sum(1 for t in turns if t.get("role") == "capsule" and "n" in t) + 1
        turn = store.append_turn(self.home, self.board, bid, "capsule", text,
                                 ts=self.clock(), n=n, **extra)
        turns.append(turn)
        self._queue(bid, meta, turn)

    def _line(self, bid, meta, turns, limit):
        """The limit's one fixed line, once: nothing if the last reply already was it."""
        last = next((t for t in reversed(turns) if t.get("role") == "capsule"), {})
        if last.get("fixed") != limit:
            self._say(bid, meta, turns, LINES[limit], fixed=limit)

    def _repair(self, bid, meta, turns):    # a crash between turns.jsonl and the outbox
        for t in turns:
            if t.get("role") == "capsule" and "n" in t:
                self._queue(bid, meta, t)

    def _backoff(self, bdir, fail=None):
        """True while a failed call is cooling down; `fail` records one more failure."""
        path, doc = bdir / "_fail.json", _read(bdir / "_fail.json") or {"n": 0, "until": 0}
        if fail:
            n = doc["n"] + 1
            _write(path, {"n": n, "until": self.clock() + min(60 * 2 ** n, 3600)})
            return True
        return doc["until"] > self.clock()

    def one(self, bid):
        """One brainstorm. A closed one is never answered (its pending stays), but its vault note
        is written and pushed once (the Slack `done` only marks it closed); one whose opener has
        no thread yet waits for a later pass."""
        meta = store.read_meta(self.home, self.board, bid)
        if meta.get("status") == "closed" and not meta.get("note"):
            return self._publish(bid)
        if meta.get("status") != "open":
            return
        meta = brainstorm.adopt_thread(self.home, self.board, bid)
        if not meta.get("thread_ts"):
            return
        turns = store.read_turns(self.home, self.board, bid)
        self._repair(bid, meta, turns)
        seen = {e for t in turns if t.get("role") == "owner" for e in t.get("events", ())}
        pend = store.list_pending(self.home, self.board, bid)
        store.clear_pending(self.home, self.board, bid,
                            [d["event_id"] for d in pend if d["event_id"] in seen])  # redelivered
        pend = [d for d in pend if d["event_id"] not in seen]
        if not pend:
            return
        ids = [d["event_id"] for d in pend]
        text = "\n\n".join(str(d.get("text") or "") for d in pend).strip()[:TEXT_MAX]
        n = sum(1 for t in turns if t.get("role") == "owner") + 1
        if n > TURN_CAP:
            turns.append(store.append_turn(self.home, self.board, bid, "owner", text,
                                           ts=self.clock(), n=n, events=ids))
            self._line(bid, meta, turns, "turns")
            return store.clear_pending(self.home, self.board, bid, ids)
        pct = self.usage()
        if pct is not None and pct >= WEEK_PAUSE_PCT:
            return self._line(bid, meta, turns, "paused")       # pending stays for after the reset
        if self.used_today() >= DAILY_CAP:
            return self._line(bid, meta, turns, "daily")        # pending stays for tomorrow
        bdir = store.bdir(self.home, self.board, bid)
        if self._backoff(bdir):
            return
        self._model(bid, bdir, meta, turns, (n, text, ids))

    def _publish(self, bid):
        try:
            brainstorm.publish(self.home, self.board, bid)
        except Exception as exc:     # no vault, a bad note: the next pass retries
            print(f"brainstorm {bid}: note not saved ({type(exc).__name__}: {exc})",
                  file=sys.stderr)

    def _call(self, bdir, meta, bundle):
        """The model call; (result, session id used). A first call that failed after the CLI had
        saved the session leaves the id "already in use": that call is retried once as a resume,
        and the session counts as live."""
        sid = meta.get("session_id") or str(uuid.uuid4())
        live = bool(meta.get("session_live"))
        limits = {"schema": SCHEMA, "tools": "", "timeout": TIMEOUT}
        res = self.adapter.run("brainstorm", bundle, str(bdir / "ws"),
                               {**limits, "resume" if live else "session_id": sid})
        if res.error_class and not live and "already in use" in f"{res.stderr} {res.raw}":
            res = self.adapter.run("brainstorm", bundle, str(bdir / "ws"),
                                   {**limits, "resume": sid})
        return res, sid

    def _model(self, bid, bdir, meta, turns, owner):
        n, text, ids = owner
        (bdir / "ws").mkdir(exist_ok=True)
        bundle = {"system": PROMPT.read_text(encoding="utf-8"),
                  "prompt": _build_prompt(text, self.lookup(text, self.cfg))}
        res, sid = self._call(bdir, meta, bundle)
        reply = res.report.get("reply") if isinstance(res.report, dict) else None
        ok = isinstance(reply, str) and not res.error_class
        reply = brainstorm.clean(reply).strip()[:REPLY_MAX] if ok else ""
        if not reply:
            why = res.error_class or "empty"
            print(f"brainstorm {bid}: no reply ({why})", file=sys.stderr)
            self._backoff(bdir, fail=True)
            return
        with store.locked(self.home, self.board, bid):
            fresh = store.read_meta(self.home, self.board, bid)
            if fresh.get("status") != "open":      # closed during the call: post nothing
                return
            turns.append(store.append_turn(self.home, self.board, bid, "owner", text,
                                           ts=self.clock(), n=n, events=ids))
            self._say(bid, meta, turns, reply)
            self._count()
            # only the runner's own fields, merged onto the fresh read, under the lock
            store.write_meta(self.home, self.board, bid,
                             {**fresh, "session_id": res.session_id or sid, "session_live": True})
        store.clear_pending(self.home, self.board, bid, ids)
        (bdir / "_fail.json").unlink(missing_ok=True)

    def go(self):
        """Every open brainstorm once; then flush the outbox. Returns the ids visited."""
        if not self.root.is_dir():
            return []
        self.conn = db.connect(paths.board_db(self.board, self.home))
        try:
            ids = store.list_ids(self.home, self.board)
            for i in ids:
                self.one(i)
            self.flush()
        finally:
            self.conn.close()
        return ids

    def flush(self):
        if self.flush_fn is not None:
            return self.flush_fn(self.conn, self.cfg)
        if slack.block(self.cfg) is not None:
            try:
                router = outbox.Router(home=self.home).configure(self.cfg)
                outbox.flush(self.conn, router, quiet=self.cfg)
            except Exception as exc:     # the next pass retries; a flush never stops the runner
                print(f"brainstorm: flush failed: {type(exc).__name__}", file=sys.stderr)


def locked(home, board):
    """The one-instance lock (non-blocking flock); the handle, or None when another run holds it."""
    root = paths.board_dir(board, home) / "brainstorms"
    root.mkdir(parents=True, exist_ok=True)
    fh = open(root / "_run.lock", "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return None
    return fh


def main(argv=None):
    board = os.environ.get("CAPSULE_BOARD", "capsule-corp")
    home = paths.home()
    lock = locked(home, board)
    if lock is None:
        print("brainstorm: another run holds the lock", file=sys.stderr)
        return 0
    with lock:
        _Run(home, board).go()
    return 0


if __name__ == "__main__":
    sys.exit(main())
