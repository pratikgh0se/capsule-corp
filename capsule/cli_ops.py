"""Operator verbs the self-heal tick calls once the old system is gone (C18)."""
import contextlib
import json
import os
import re
import shlex
import sys
import time
from pathlib import Path

from capsule import paths

DEFAULT_PAUSE_S = 86400
# https://host/path, ssh://[user@]host[:port]/path or user@host:path; no userinfo in an https url
# (an ssh/scp host starts with a word character: a leading `-` would read as an ssh option)
REMOTE = re.compile(r"https://[\w.-]+(:\d+)?/[\w./~-]+|ssh://([\w.-]+@)?\w[\w.-]*(:\d+)?/[\w./~-]+"
                    r"|[\w.-]+@\w[\w.-]*:[\w./~-]+")
# {1} is " --capsule-home <dir>" and {2} is "CAPSULE_HOME=<dir> " when the board is not on the
# default home (the tick script reads the environment, the python verbs take the flag)
_TICK = shlex.quote(str(paths.REPO / "bin" / "capsule-tick.sh"))
START = "{2}" + _TICK.replace("{", "{{").replace("}", "}}") + " start {0}"
# the launcher `capsule install` writes pins the Python 3.11+ it found; stock `python3` is 3.9
LAUNCHER = "~/.local/bin/capsule"
NEXT = (LAUNCHER + " check --board {0}{1}", LAUNCHER + " board tick {0}{1}", START,
        LAUNCHER + ' intake {0} --title "Add a README badge" --body-file card.md{1}')


def next_args(home):
    """(" --capsule-home <dir>", "CAPSULE_HOME=<dir> ") when `home` is not the default home
    (however it was named: a flag or the environment), else two empty strings: the pieces a
    printed `next` command needs to reach the same board from a shell that has neither."""
    if Path(home) == paths.default_home():
        return "", ""
    return f" --capsule-home {shlex.quote(str(home))}", f"CAPSULE_HOME={shlex.quote(str(home))} "


def add_parser(sub):
    p = sub.add_parser("outbox-send", help="queue one notification on the configured board")
    p.add_argument("cls", metavar="class")
    p.add_argument("text")
    p.add_argument("--board", default=None, help="default: the config's board")
    p.add_argument("--v2-class", default=None, choices=("scheduled", "page"),
                   help="the class to send instead while the board is on slack.routing v2")
    p.add_argument("--digest", action="store_true",
                   help="class scheduled: a multi-line ☀ digest body, which posts by its marker")
    p.add_argument("--capsule-home", default=None)
    p = sub.add_parser("pause", help="hold dispatch on every board (guard_state pause:board)")
    p.add_argument("--reason", required=True)
    p.add_argument("--seconds", type=int, default=DEFAULT_PAUSE_S)
    p.add_argument("--capsule-home", default=None)
    p = sub.add_parser("unpark", help="lift pause:board on every board")
    p.add_argument("--reason", default=None, help="lift only pauses that carry this reason")
    p.add_argument("--capsule-home", default=None)
    p = sub.add_parser("card", help="one card's event timeline: why it holds, parked or failed")
    p.add_argument("board")
    p.add_argument("task")
    p.add_argument("verb", choices=("history",))
    p.add_argument("--capsule-home", default=None)


def _set_pause(home, reason, seconds):
    """Compare-and-set pause:board on every board; a write that lost a race is skipped.
    Returns (boards, written). seconds=0 lifts a pause that carries `reason`."""
    from capsule import db, paths
    root = home / "boards"
    boards = sorted(d.name for d in root.iterdir() if (d / "kanban.db").is_file()) \
        if root.is_dir() else []
    now = int(time.time())
    done = 0
    for board in boards:
        conn = db.connect(paths.board_db(board, home))
        try:
            held, version = db.guard_get(conn, "pause:board")
            live = isinstance(held, dict)
            old = held.get("reason") if live else None
            until = now + seconds if seconds else 0
            if not until:
                value = {"until": 0, "reason": old} if live and reason in (None, old) else None
            elif live and (held.get("until") or 0) > now and old not in ("disk", "failure_storm") \
                    and (held["until"] >= until or old not in (None, "", reason)):
                value = None
            else:
                since = held.get("since") if live and (held.get("until") or 0) > now else None
                value = {"until": until, "reason": reason, "since": since or now}
            done += value is not None and db.guard_set(
                conn, "pause:board", value, expect_version=version)
        finally:
            conn.close()
    return len(boards), done


def _outbox_class(cls):
    """(class, page) for `outbox-send`: `page` (heal.sh's down-lines) is an incident that pages
    under routing v2; `ask` is refused, only `cos ask` may post to the inbox."""
    if cls == "ask":
        raise ValueError("class 'ask' is written only by `cos ask`")
    return ("incident", True) if cls == "page" else (cls, False)


def _outbox_plan(args, v2):
    """((class, page, headline, digest), None) for `outbox-send`, or (None, why it is refused).
    While the board is on routing v2, `--v2-class` swaps the class (e.g. blocked-tonight: a
    scheduled digest; heal's logged-out line: `page`, still `paused` while v2 is off).
    The message must render under the class actually used."""
    from capsule import slack
    head = args.text if args.digest and args.cls == "scheduled" else \
        next((l for l in args.text.splitlines() if l.strip()), "heal notice")
    try:
        cls, page = _outbox_class(args.cls)
        if args.v2_class and v2:
            cls, page, head = args.v2_class, False, args.text
        digest = args.digest and cls == "scheduled"
        slack.render(cls, head, digest=digest)
    except ValueError as exc:
        return None, exc
    return (cls, page, head, digest), None


def handle(args, parser):
    from capsule import config, db, outbox, paths
    home = paths.home(args.capsule_home)
    if args.cmd == "board":
        return _board_new(args, parser, home)
    if args.cmd == "outbox-send":
        board = args.board
        if board is None:
            try:
                board = config.load(paths.config_path(home))["board"]
            except (OSError, ValueError, KeyError) as exc:
                print(f"capsule outbox-send: no configured board: {exc}", file=sys.stderr)
                return 1
        path = paths.board_db(board, home)
        if not path.is_file():
            print(f"capsule outbox-send: no board {board!r} under {home}", file=sys.stderr)
            return 1
        conn = db.connect(path)
        try:
            plan, err = _outbox_plan(args, outbox.routing_v2(conn))
            if err:
                print(f"capsule outbox-send: refused: {err}", file=sys.stderr)
                return 1
            cls, page, head, digest = plan
            with db.txn(conn):
                ids = outbox.notify(conn, cls, head, file_body=args.text, page=page,
                                    digest=digest)
        finally:
            conn.close()
        print(f"queued {len(ids)} on {board}")
        return 0
    if args.cmd == "card":
        path = paths.board_db(args.board, home)
        if not path.is_file():
            print(f"capsule card: no board {args.board!r} at {path}", file=sys.stderr)
            return 1
        for e in db.connect(path).execute("SELECT kind, payload, created_at FROM task_events"
                                          " WHERE task_id=? ORDER BY id", (args.task,)):
            why = json.loads(e["payload"] or "{}")
            print(time.strftime("%Y-%m-%d %H:%M", time.localtime(e["created_at"])), e["kind"],
                  why.get("trigger") or why.get("reason") or "")
        return 0
    if args.cmd == "pause":
        boards, done = _set_pause(home, args.reason, args.seconds)
        print(f"paused {done} board(s): {args.reason}")
    else:
        boards, done = _set_pause(home, args.reason, 0)
        print(f"unparked {done} board(s)")
    return 0 if boards else 1


def _within(path, root):
    """True when `path` is `root` or below it, compared by file identity so an upper-cased path
    on a case-insensitive disk still counts. `root` must exist; a missing one is never matched."""
    for part in (path, *path.parents):
        try:
            if os.path.samefile(part, root):
                return True
        except OSError:
            continue
    return False


def _repo_problem(repo, home):
    """Why `repo` cannot be a new board's product repo, or None. Never echoes the value (an
    https url may carry a token)."""
    from capsule import paths, workspace
    try:
        workspace._validate_repo_url(repo)
    except ValueError as exc:
        return str(exc)
    if REMOTE.fullmatch(repo):
        return None
    if not os.path.isabs(repo):
        return "--repo must be an https:// or ssh:// url, user@host:path, or an absolute path"
    if any(ord(c) < 32 or ord(c) == 127 for c in repo):
        return "--repo path must not contain control characters"
    here = Path(os.path.realpath(repo))
    for other, label in ((home, "CAPSULE_HOME"), (paths.REPO, "the Capsule checkout")):
        there = Path(os.path.realpath(other))
        if _within(here, there) or _within(there, here):
            return f"--repo path overlaps {label} ({there})"
    return None if here.exists() else "--repo path does not exist"


def _channel(name):
    return "#cc-" + name.lower().replace(".", "-")


def _new_config(name, repo, checks=None, notify=False):
    """The example config with its board name swapped for `name`, `repo` as repo_url, one epic
    `epic/<name>`, the example's host-specific `unblocked` block dropped. Raises ValueError
    when the result is not a valid config or one of its refs would be refused at dispatch."""
    from capsule import config, paths, workspace
    example = json.loads((paths.REPO / "capsule" / "config.example.json").read_text())
    swap = re.compile(rf"(?<![A-Za-z0-9]){re.escape(example['board'])}(?![A-Za-z0-9])")
    cfg = json.loads(swap.sub(name, json.dumps(example)))
    cfg["repo_url"] = repo
    cfg["epics"] = [f"epic/{name}"]
    cfg.pop("unblocked", None)
    if checks:
        cfg["contract_commands"] = list(checks)
    cfg["slack"]["enabled"] = notify
    chans = cfg["slack"]["channels"]    # Slack channel names: lowercase, no dots
    cfg["slack"]["channels"] = {k: _channel(name) if v == f"#cc-{name}" else v
                                for k, v in chans.items()}
    if name != "capsule-corp":      # LAND-03: product boards land on a day branch, a PR a day
        cfg["lander"] = {**cfg.get("lander", {}), "branch": "capsule/daily"}
    for ref in cfg["epics"]:
        workspace._validate_ref(ref, what="epic branch")
    return config.validate(cfg)


def _board_new(args, parser, home):
    """`capsule board new <name> --repo <url>`: write config.json and an empty kanban.db."""
    from capsule import db, safe_write
    if len(args.rest) != 1 or not args.repo:
        parser.error("usage: board new <name> --repo <url>")
    name = args.rest[0]
    checks = args.check or []
    problem = paths.board_name_problem(name) or _repo_problem(args.repo, home)
    if not problem and not all(c.strip() for c in checks):
        problem = "--check needs a non-empty command"
    text = cfg = None
    if not problem:
        try:
            cfg = _new_config(name, args.repo, checks, args.notify)
            text = json.dumps(cfg, indent=2) + "\n"
        except ValueError as exc:
            problem = str(exc)
    board_dir = paths.board_dir(name, home)
    if not problem:   # the claim: whoever makes the directory owns the board
        board_dir.parent.mkdir(parents=True, exist_ok=True)
        try:
            board_dir.mkdir(exist_ok=False)
        except FileExistsError:
            problem = f"board {name!r} already exists at {board_dir}"
    if problem:
        print(f"capsule board new: refused: {problem}", file=sys.stderr)
        return 1
    cfg_path = paths.config_path(home, name)
    try:
        safe_write.atomic_write(home, cfg_path, text, create_dirs=True)
    except BaseException:
        with contextlib.suppress(OSError):
            board_dir.rmdir()
        raise
    print(f"wrote {cfg_path}")
    db_path = paths.board_db(name, home)
    try:
        db.create_board(db_path)
    except BaseException:
        print(f"capsule board new: could not create {db_path}; to retry, remove the folder"
              f" {board_dir} yourself and run board new again", file=sys.stderr)
        raise
    print(f"wrote {db_path}")
    if args.notify:
        print(f"note: Slack is on: channel {_channel(name)} must exist and the Capsule app must"
              " be invited to it")
        print("note: blocked cards and questions go to #capsule-needs-you, incidents to"
              " #capsule-alerts")
        print("note: running cards go to #capsule-activity")
    else:
        print("note: Slack is off (--notify turns it on)")
    if not checks:
        print("note: default contract checks (--check CMD replaces them):")
        for cmd in cfg["contract_commands"]:
            print("  " + cmd.strip())
    print("next:")
    flag, env = next_args(home)
    for line in NEXT:
        print("  " + line.format(name, flag, env))
    return 0
