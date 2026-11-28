"""`capsule cos ask` (notification rebuild slice 3, spec 2026-10-29 sections 3 and 4): one
plain-English decision for the owner. The text is linted (no ids, paths or commands), queued as
class `ask` under the key `needs:ask-<k>`, and an ask file records what each answer would do,
so a later listener trusts that file and never the Slack text. Text only: no buttons here.
This module also owns the one `cos` parser: `ask` is here, `brief` is capsule/cos_brief.py."""
import json
import re
import secrets
import sys
import time

from capsule import cos_brief, paths, slack

TITLE_WORDS = 8
FIELD_MAX = 300    # one field, like slack.VALUE_MAX; the whole message is capped in slack.render
ID_RE = re.compile(r"(?<![A-Za-z0-9])(?:t_[0-9a-f]{4,}|inc_\w+)", re.I)
HEX_RE = re.compile(r"\b(?=[0-9a-f]*\d)(?=[0-9a-f]*[a-f])[0-9a-f]{7,}\b", re.I)    # 5ef2bb4b
REPO_DIRS = "capsule|docs|contracts|maintenance|bin|tests|docker|launchd"
PATH_RE = re.compile(r"(?<![\w/:])(?:~|\.{1,2})?/[\w.~-]+|\b[\w.-]+/[\w./-]*\.[A-Za-z]\w{0,4}\b"
                     rf"|\b(?:{REPO_DIRS})/\S|/\*|\*/|\*\.\w|\w+://"
                     r"|\b\w+\.(?:py|sh|json|jsonl|md|toml|sql|env|db|yaml|yml|plist)\b", re.I)
COMMAND_RE = re.compile(r"`|\bpython3?\s+-m\b|\bcapsule\s+(?:human|cos|status|tick|incidents)\b"
                        r"|\bgit\s+(?:push|pull|commit|checkout|reset|merge|rebase|clone)\b"
                        r"|\b(?:launchctl|sudo|curl|chmod)\s")
LINT = (("a card or incident id", ID_RE), ("a commit or run id", HEX_RE),
        ("a path or file name", PATH_RE), ("a command", COMMAND_RE))
TEXT_FIELDS = ("title", "what", "why", "if_yes", "if_no", "pick", "because")


def add_parser(sub):
    cos = sub.add_parser("cos", help="chief-of-staff verbs: ask, brief")
    cos_sub = cos.add_subparsers(dest="cos_cmd", required=True)
    brief = cos_sub.add_parser("brief", help="board health, open items, answers while you were"
                                             " away")
    brief.add_argument("--capsule-home", default=None)
    p = cos_sub.add_parser("ask", help="queue one plain-English decision for the owner's inbox")
    p.add_argument("board")
    p.add_argument("task_id")
    for name in ("title", "what", "why", "if-yes", "if-no", "pick", "because"):
        p.add_argument(f"--{name}", required=True)
    for name in ("card-name", "project", "yes-option", "no-option", "no-reason"):
        p.add_argument(f"--{name}", default=None)
    p.add_argument("--dossier", default=None, help="the ask gate dossier id (see ask-gate check)")
    p.add_argument("--capsule-home", default=None)


def _one(text, limit=None):
    """One line; a value over `limit` is refused, never cut mid-sentence."""
    text = " ".join(str(text or "").split())
    if limit and len(text) > limit:
        raise ValueError(f"a field is longer than {limit} characters: say it shorter")
    return text


def _lint(text):
    for why, rx in LINT:
        hit = rx.search(text)
        if hit:
            raise ValueError(f"{why} in the message ({hit.group(0)!r}): say it in plain words")


def _render(args, row):
    """The inbox message body (spec section 3), linted as `notify` will send it (slack.render adds
    the glyph). Every field is one line, so a value can never add a line of its own."""
    f = {name: _one(vars(args)[name], FIELD_MAX) for name in TEXT_FIELDS}
    empty = [n for n, v in f.items() if not v]
    if empty:
        raise ValueError(f"empty field: {', '.join(empty)}")
    if len(f["title"].split()) > TITLE_WORDS:
        raise ValueError(f"the title is longer than {TITLE_WORDS} words")
    if not re.match(r"(yes|no)\b", f["pick"], re.I):
        raise ValueError("--pick must start with Yes or No")
    project = _one(args.project, FIELD_MAX) or re.sub(r"[-_]+", " ", args.board).title()
    card = _one(args.card_name, FIELD_MAX) or _one(row["short_name"] or row["title"], FIELD_MAX)
    body = "\n".join([
        f["title"], f"{project} · {card}", f"What: {f['what']}",
        f"Why it matters: {f['why']}", f"If yes: {f['if_yes']}   If no: {f['if_no']}",
        f"My pick: {f['pick'][0].upper()}{f['pick'][1:]}, because {f['because']}.",
        "[ Yes ]  [ No ]  [ Later ]"])
    _lint(slack.render("ask", body))
    return body


def _decision_options(conn, task_id):
    """{option id: label} of the card's latest `decision_requested` event (a decision.v1 the
    worker filed) newer than its last block-close event (lookout's kinds); None when the card has
    no live question with options behind its hold."""
    from capsule import lookout
    closes = (*lookout.BLOCK_CLOSE, lookout.ACCEPT_CLOSE_KIND)
    marks = ",".join("?" * len(closes))
    row = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND"
                       " kind='decision_requested' AND id > COALESCE((SELECT MAX(id) FROM"
                       f" task_events WHERE task_id=? AND kind IN ({marks})), 0)"
                       " ORDER BY id DESC LIMIT 1", (task_id, task_id, *closes)).fetchone()
    try:
        options = json.loads(row[0] or "{}").get("options") if row else None
    except (ValueError, AttributeError):
        options = None
    pairs = [(str(o["id"]), _one(o.get("label"))[:80]) for o in options or []
             if isinstance(o, dict) and o.get("id") is not None]
    return dict(pairs) or None


def _park_reason(args):
    reason = _one(args.no_reason, FIELD_MAX)
    if not reason:
        raise ValueError("this card needs --no-reason (what a No records when it parks)")
    return f"owner said no: {reason}"


def _blocked_choices(args, options):
    """A hold with a question: both answers are `unblock`, each naming the question's own option.
    A hold with none: Yes lifts it, No parks it."""
    yes, no = args.yes_option, args.no_option
    if options is None:
        if yes or no:
            raise ValueError("this card has no question with options: drop --yes-option and"
                             " --no-option")
        return {"yes": {"verb": "unblock", "text": f"owner said yes to: {_one(args.title)}"},
                "no": {"verb": "park", "text": _park_reason(args)}}
    if not (yes in options and no in options and yes != no):
        raise ValueError("--yes-option and --no-option must be two different option ids of the"
                         f" card's own question ({', '.join(options)})")
    return {k: {"verb": "unblock",
                "text": f"option {o} chosen (the question's own option {o}: \"{options[o]}\")"}
            for k, o in (("yes", yes), ("no", no))}


def _choices(args, cfg, row, options):
    """{answer: {verb, text}} for this card's state: `unblock` or `park` for a blocked card,
    `reset` or `park` for one in triage. Every text is fixed here, never taken from Slack."""
    from capsule import interventions
    status = row["status"]
    if status == "blocked":
        if row["block_kind"] == "parked":    # `unblock` refuses a parked card (blocks.park)
            raise ValueError("the card is parked: an unblock Yes would do nothing")
        return _blocked_choices(args, options)
    if status != "triage":
        raise ValueError(f"the card is {status}: only a blocked or triage card can be asked about")
    park = _park_reason(args)
    missing = interventions.routing_gap(cfg, row["branch_name"], row["assignee"])[2]
    if missing:    # the same check `reset` runs: a Yes on this card would do nothing
        raise ValueError(f"a reset Yes would do nothing: {' and '.join(missing)} missing or"
                         " unroutable on that card")
    return {"yes": {"verb": "reset", "text": f"owner said yes to: {_one(args.title)}"},
            "no": {"verb": "park", "text": park}}


def _gate(home, cfg, args):
    """The ask gate (capsule/ask_gate.py). Mode `on`: a direct ask is refused (the gate sends it).
    Mode `shadow`: an ask with no `escalate` verdict under a day old is logged `would_refuse`."""
    from capsule import ask_apply, ask_gate
    now = int(time.time())
    mode = ask_apply.effective(home, args.board, cfg, now)["mode"]
    if mode == "on" and not getattr(args, "queued", False):
        raise ValueError("the ask gate is on: the gate sends it (a direct ask would be a second"
                         " ask for one question)")
    found = [r for r in ask_gate.verdict_rows(home, args.board)
             if r.get("dossier") == args.dossier and r.get("subject") == args.task_id
             and r.get("verdict") == "escalate" and r.get("at", 0) > now - ask_gate.FRESH_S]
    if mode == "off" or found:
        return
    why = "no escalate verdict under 24 hours old for this card and --dossier"
    if mode == "on":
        raise ValueError(f"the ask gate refuses: {why} (run ask-gate check first)")
    row = {"at": now, "subject": args.task_id, "dossier": args.dossier,
           "verdict": "would_refuse", "rule": "cos_ask", "reason": why, "mode": mode,
           "would_refuse": True}
    try:
        ask_gate.log(home, args.board, row)
    except OSError as exc:      # shadow never refuses an ask, also when its log cannot be written
        print(f"capsule cos ask: ask gate log not written: {exc}", file=sys.stderr)


def _ask(home, args):
    from capsule import config, db, outbox, safe_write
    cfg = config.load(paths.resolve_config(home, args.board))
    if not ((slack.block(cfg) or {}).get("channels") or {}).get("inbox"):
        raise ValueError("slack.channels.inbox is not set: an ask never uses the old channels")
    path = paths.board_db(args.board, home)
    if not path.is_file():
        raise ValueError(f"no board {args.board!r} under {home}")
    conn = db.connect(path)
    try:
        if not outbox.slack_enabled(conn):
            raise ValueError("slack is not on for this board (the tick mirrors the setting)")
        row = conn.execute("SELECT status, block_kind, branch_name, assignee, title, short_name"
                           " FROM tasks WHERE id=?", (args.task_id,)).fetchone()
        if row is None:
            raise ValueError(f"no card {args.task_id!r} on {args.board}")
        choices = _choices(args, cfg, row, _decision_options(conn, args.task_id))
        text = _render(args, row)
        key = getattr(args, "key", None) or secrets.token_hex(8)    # the gate's send has its own
        last = conn.execute("SELECT MAX(id) FROM task_events WHERE task_id=?",
                            (args.task_id,)).fetchone()[0]
        if not getattr(args, "queued", False):     # the ask gate's own send was judged already
            _gate(home, cfg, args)
        doc = {"key": key, "title": _one(args.title), "task_id": args.task_id, "asked_by": "cos",
               "board": args.board, "after_event_id": last, "expected_status": row["status"],
               "block_kind": row["block_kind"], "choices": choices, "at": int(time.time())}
        safe_write.atomic_write(home, paths.board_dir(args.board, home) / "asks" / f"{key}.json",
                                json.dumps(doc, indent=2) + "\n", create_dirs=True)
        with db.txn(conn):
            outbox.notify(conn, "ask", text, key=f"needs:ask-{key}")
    finally:
        conn.close()
    return key


def handle(args, parser):
    if args.cos_cmd == "brief":
        return cos_brief.run(args, sys.stdout)
    home = paths.home(args.capsule_home)
    try:
        key = _ask(home, args)
    except (OSError, ValueError, KeyError) as exc:
        print(f"capsule cos ask: refused: {exc}", file=sys.stderr)
        return 1
    print(f"asked: needs:ask-{key}")
    return 0
