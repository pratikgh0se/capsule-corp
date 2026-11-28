"""Open and close a Slack brainstorm (M204, D229). `capsule brainstorm open` queues the opener
(outbox key `brainstorm:<id>`, into `slack.channels.brainstorm`) with the D88 handoff block;
`close` (the CLI, and what intake calls when the owner types `done`) writes the vault note
`Brainstorms/<date>-<slug>-<id>.md` and pushes it, then marks the brainstorm closed. Closing is
reversible (Z6): meta status is the only switch, and the note is rewritten on a retry. The
files live in capsule/brainstorm_store.py. No model and no Slack call happens here."""
import datetime
import importlib.util
import json
import re
import secrets
import sys
import time
import uuid
from pathlib import Path

from capsule import brainstorm_store as store, config, db, host, outbox, paths, safe_write, slack

sys.path.insert(0, str(paths.REPO / "maintenance"))
import scrub  # noqa: E402  (free text leaving the board is scrubbed first)

TITLE_MAX = 80
FRAMING_MAX = 1500
FOLLOW_UP = re.compile(r"^\W*(?:follow[- ]?up|todo)\s*:\s*(.+)", re.I)
NO_FOLLOW_UP = ("- none recorded. The brainstorm cannot act (D113); any change starts from a"
                " terminal session.")


class Refused(ValueError):
    """A brainstorm that cannot be opened or closed; the message says why."""


def add_parser(sub):
    bp = sub.add_parser("brainstorm", help="open or close a Slack brainstorm thread (M204)")
    bsub = bp.add_subparsers(dest="bs_cmd", required=True)
    op = bsub.add_parser("open", help="post the opener into slack.channels.brainstorm")
    op.add_argument("title")
    op.add_argument("--framing", default="", help="the first message's body")
    op.add_argument("--task-id", default=None)
    op.add_argument("--questions", type=int, default=None,
                    help="open question count, for the handoff's cost line")
    sh = bsub.add_parser("show", help="print a brainstorm's state and turns")
    sh.add_argument("id")
    cl = bsub.add_parser("close", help="write and push the vault note, mark it closed")
    cl.add_argument("id")
    cl.add_argument("--vault", default=None,
                    help="default: the configured notes vault (host.json vault.path)")
    for p in (op, cl, sh):
        p.add_argument("--board", required=True)
        p.add_argument("--capsule-home", default=None)


def clean(text):
    """Secrets out (scrub.scrub plus the Slack token shapes), whatever the text."""
    return slack.SECRET_RE.sub("***", scrub.scrub(str(text)))


def _handoff():
    """bin/brainstorm-handoff.py (M203), loaded by path: its name has a dash."""
    path = paths.REPO / "bin" / "brainstorm-handoff.py"
    spec = importlib.util.spec_from_file_location("brainstorm_handoff", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _opener(title, framing, bid, session_id, ws, questions=None):
    """(message, session name). The handoff block, minus the `Later:` line, the button text (the
    real buttons are Block Kit, see slack._answer_blocks) and the `--session-id` start line (the
    runner starts the session, so a terminal can only resume it): the resume is by session id
    from the ws dir, where the runner's session lives."""
    mod = _handoff()
    block = mod.render(f"{title[:30]} {bid[:6]}", questions, str(ws), session_id)
    lines = [f"claude --resume {session_id}" if ln.startswith("claude --session-id") else ln
             for ln in block["message"].splitlines()
             if not ln.startswith("Later:") and ln != mod.BUTTONS]
    head = [f"{slack.BRAINSTORM_GLYPH} Brainstorm: {title}"]
    if framing:
        head.append(framing)
    head.append("Reply in this thread. Type `done` to close it. I can talk, not act: changes"
                " start from a terminal.")
    return "\n".join(head + ["", *lines]), block["name"]


def _channel(home, board):
    """The board's brainstorm channel, or Refused: no board, Slack off, or no channel key."""
    if not paths.board_db(board, home).is_file():
        raise Refused(f"no board {board!r} under {home}")
    blk = slack.block(config.load(paths.resolve_config(home, board)))
    if blk is None:
        raise Refused("slack is not on for this board")
    channel = (blk.get("channels") or {}).get("brainstorm")
    if not channel:
        raise Refused("slack.channels.brainstorm is not set on this board")
    return channel


def _queue(conn, home, board, meta, text, now):
    """Create the files and queue the opener; the dedup key is how adopt_thread finds it."""
    bid = meta.pop("id")
    store.create(home, board, bid, {"status": "open", **meta})
    first = f"{meta['title']}\n{meta['framing']}".strip()
    store.append_turn(home, board, bid, "capsule", first, ts=now)
    with db.txn(conn):
        outbox.add(conn, "slack", text, dedup_key=store.OPEN_KEY.format(bid), now=now)


def open_brainstorm(title, *, board, framing="", home=None, task_id=None, now=None,
                    questions=None):
    """Queue the opener; returns the brainstorm id. Raises Refused when the board has no
    brainstorm channel, Slack is off, or today's actionable cap (D209) is spent (the item then
    waits for the brief, as any actionable message does)."""
    home = paths.home(home)
    title = " ".join(clean(title).split())[:TITLE_MAX]
    framing = clean(framing).strip()[:FRAMING_MAX]
    if not title:
        raise Refused("empty title")
    channel = _channel(home, board)
    conn = db.connect(paths.board_db(board, home))
    try:
        if not outbox.slack_enabled(conn):
            raise Refused("slack is not on for this board (the tick mirrors the setting)")
        bid, session_id = secrets.token_hex(8), str(uuid.uuid4())
        ws = paths.board_dir(board, home) / "brainstorms" / bid / "ws"
        text, name = _opener(title, framing, bid, session_id, ws, questions)
        meta = {"id": bid, "title": title, "framing": framing, "channel": channel, "thread_ts": "",
                "session_id": session_id, "name": name, "started_by": "capsule", "task_id": task_id,
                "started_at": int(now or time.time())}
        import board_bridge
        sent = board_bridge.actionable_send(
            lambda _message: _queue(conn, home, board, meta, text, now) or True, text,
            board_name=board, task_id=task_id, what=f"Brainstorm - {title}")
        if not sent:
            raise Refused("today's actionable cap is spent; it waits for the brief")
    finally:
        conn.close()
    return bid


def _stamp(ts):
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).strftime("%Y-%m-%d %H:%M")


def _slug(title):
    return "-".join(re.findall(r"[a-z0-9]+", title.lower()))[:60].strip("-") or "untitled"


def _follow_ups(turns):
    found = [m.group(1).strip() for t in turns if t["role"] != "capsule"
             for m in (FOLLOW_UP.match(ln) for ln in t["text"].splitlines()) if m]
    return "\n".join(f"- {f}" for f in found) if found else NO_FOLLOW_UP


def render_note(meta, board, bid, turns, closed_at):
    date = _stamp(meta["started_at"])[:10]
    lines = [f"- {_stamp(t['ts'])} {t['role']}: " + clean(" / ".join(t["text"].splitlines()))
             for t in turns]
    return (
        "---\n"
        f"date: {date}\ntype: brainstorm\nstatus: closed\nbrainstorm_id: {bid}\nboard: {board}\n"
        f"title: {json.dumps(meta['title'])}\nsession_name: {meta['name']}\n"
        f"session_id: {meta['session_id']}\nstarted_by: {meta['started_by']}\n"
        f"opened_at: {_stamp(meta['started_at'])}\nclosed_at: {_stamp(closed_at)}\n"
        f"channel: {meta.get('channel', '')}\n"
        f"thread_ts: {json.dumps(str(meta.get('thread_ts', '')))}\n"
        f"created_by: capsule brainstorm\ntags: [brainstorm, {board}]\nai-first: true\n---\n\n"
        "## For future agent\n"
        f"Slack brainstorm \"{meta['title']}\" on board {board}, started by {meta['started_by']}, "
        "closed when the owner typed done (or `capsule brainstorm close`). The assistant in the "
        "thread "
        "had no tools: it could talk, never act, so everything below is conversation, not "
        f"change. Resume the same session in a terminal with `cd <board>/brainstorms/{bid}/ws` "
        f"then `claude --resume {meta['session_id']}` (one terminal only). Secrets are scrubbed "
        "from the transcript.\n\n"
        "## Transcript\n" + ("\n".join(lines) if lines else "(no messages)") + "\n\n"
        "## Proposed follow-ups (need terminal)\n" + _follow_ups(turns) + "\n")


def adopt_thread(home, board, bid):
    """meta, with channel and thread_ts filled from the delivered opener (slack_msg row of key
    `brainstorm:<id>`) when the outbox has posted it since; unchanged until then."""
    meta = store.read_meta(home, board, bid)
    if meta.get("thread_ts"):
        return meta
    conn = db.connect_ro(paths.board_db(board, home))
    try:
        row = conn.execute("SELECT channel, ts FROM slack_msg WHERE key=?",
                           (store.OPEN_KEY.format(bid),)).fetchone()
    finally:
        conn.close()
    if row is None:
        return meta
    return store.update_meta(home, board, bid, channel=row["channel"], thread_ts=row["ts"])


def show(home, board, bid):
    """The brainstorm as text: its meta, then its turns."""
    meta = adopt_thread(home, board, bid)
    keys = ("title", "status", "name", "session_id", "thread_ts")
    head = [f"{k}: {meta.get(k, '')}" for k in keys]
    turns = [f"{_stamp(t['ts'])} {t['role']}: {t['text']}"
             for t in store.read_turns(home, board, bid)]
    return "\n".join([*head, "", *turns])


def _vault(vault):
    path = Path(vault) if vault else host.vault_path()
    if path is None:
        raise Refused("no notes vault configured (host.json vault.path)")
    if not path.is_dir():
        raise Refused(f"vault {path} is not a directory")
    return path


def publish(home, board, bid, *, vault=None, git_runner=None):
    """Write the vault note of a closed thread, push it (best effort: a failed push is a
    vault_git warn event, the note stays) and record `note` in meta. Returns the note's path
    relative to the vault; a thread that already has its note returns it untouched. This is what
    the runner calls for closed-but-unpublished threads (Slack `done` only marks closed)."""
    home = paths.home(home)
    meta = store.read_meta(home, board, bid)
    if meta.get("note"):
        return meta["note"]
    if meta.get("status") != "closed":
        raise Refused(f"brainstorm {bid} is not closed")
    meta = adopt_thread(home, board, bid)
    vault = _vault(vault)
    rel = f"Brainstorms/{_stamp(meta['started_at'])[:10]}-{_slug(meta['title'])}-{bid}.md"
    note = render_note(meta, board, bid, store.read_turns(home, board, bid),
                       meta.get("closed_at") or int(time.time()))
    safe_write.atomic_write(vault, vault / rel, note, create_dirs=True)
    import vault_git
    vault_git.push(vault, [rel], f"brainstorms: close {bid}", runner=git_runner)
    store.update_meta(home, board, bid, note=rel)
    return rel


def close(home, board, bid, *, vault=None, now=None, git_runner=None):
    """The terminal's close: mark the thread closed, then publish its note now. A vault that is
    not a directory refuses before anything changes. Returns the note's path."""
    home = paths.home(home)
    store.read_meta(home, board, bid)
    _vault(vault)
    store.mark_closed(home, board, bid, now)
    return publish(home, board, bid, vault=vault, git_runner=git_runner)


def _run(args, home):
    if args.bs_cmd == "open":
        bid = open_brainstorm(args.title, board=args.board, framing=args.framing, home=home,
                              task_id=args.task_id, questions=args.questions)
        return f"opened: {bid}"
    if args.bs_cmd == "show":
        return show(home, args.board, args.id)
    return f"closed: {close(home, args.board, args.id, vault=args.vault)}"


def handle(args, parser):
    try:
        print(_run(args, paths.home(args.capsule_home)))
    except (OSError, ValueError) as exc:
        print(f"capsule brainstorm {args.bs_cmd}: refused: {exc}", file=sys.stderr)
        return 1
    return 0
