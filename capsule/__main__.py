"""`python3 -m capsule tick [--once]` runs the tick loop; `run-one` is what it spawns per
run; `report`/`human` are Task 11's operator verbs (row 391); `incidents` is
Korin's record (row 456)."""
import sys

if sys.version_info < (3, 11):    # before any import below: a stock Mac's python3 is 3.9
    sys.exit("capsule needs Python 3.11 or newer (this is %d.%d): use ~/.local/bin/capsule "
             "instead of python3 -m capsule, or run ./install.sh from the clone first if you "
             "have not installed" % sys.version_info[:2])

import argparse
import contextlib
import json
import os
import shutil
import sqlite3
import subprocess
import time
import traceback
from pathlib import Path

from capsule import cli_names, cli_ops, cli_prior, cos_ask
from capsule import incident_args   # light: argparse only
from capsule import cli_intake, plan_cut, token_accountant   # light: no heavy deps at import
from capsule import auditor, golden_cli, loophole
from capsule import host, host_registry, human
from capsule.blind import cli as blind_cli
from capsule import board_tick, daily_pr, design_briefs, doctor, install, security, threat_model
from capsule import rhythm, publish
from capsule import ask_gate, brainstorm, patrol_run, stall_owner, tick_watch


def _add_basic_parsers(sub):    # every subcommand but human/incidents, which earn their own builder
    tick_p = sub.add_parser("tick", help="run the tick loop, one pass every caps.tick_seconds")
    tick_p.add_argument("--once", action="store_true", help="run one pass, print its counts, exit")
    tick_p.add_argument("--capsule-home", default=None, help="default: $CAPSULE_HOME, else ~/.local/share/capsule")
    tick_p.add_argument("--replace", action="store_true", help="row 517: SIGTERM an already-running tick and take over")
    tick_p.add_argument("--board", default=None, help="serve boards/<board>/config.json and lock it"
                                                       " single-writer; default: the legacy configured board")

    run_p = sub.add_parser("run-one", help="run one role for one run (spawned by the tick)")
    for arg in ("board_db", "spool_dir", "run_id", "role"):
        run_p.add_argument(arg)

    report_p = sub.add_parser("report", help="interventions-per-card report vs the U1 baseline (row 391)")
    report_p.add_argument("board")
    report_p.add_argument("--since", default=None,
                          help="isolate runs and interventions started/raised at or after this time (epoch seconds"
                               " or ISO date) - filters each card's own runs/human/decisions, not which cards are"
                               " listed; defaults to the board's handover-take record, if any (final review I6)")
    report_p.add_argument("--capsule-home", default=None)
    report_p.add_argument("--your-time", action="store_true",
                          help="print only the `## YOUR TIME` block (the weekly review's input)")
    fmt = report_p.add_mutually_exclusive_group()
    fmt.add_argument("--json", action="store_true", help="print the report as JSON (the default)")
    fmt.add_argument("--md", action="store_true", help="print the report as a markdown table")

    check_p = sub.add_parser("check", help="read-only preconditions for a live run: config, docker, image, claude")
    check_p.add_argument("--capsule-home", default=None)
    check_p.add_argument("--board", default=None, help="check boards/<board>/config.json; default: the legacy board")

    unblocked_p = sub.add_parser("unblocked", help="deterministic detector of unblocked, unowned work (row 470):"
                                                    " boards, branch queue, human verbs, run layers; read-only,"
                                                    " never writes")
    unblocked_p.add_argument("--json", action="store_true", help="print the report as JSON")
    unblocked_p.add_argument("--capsule-home", default=None)

    stalls_p = sub.add_parser("stalls", help="the open stalls (read-only); exit 1 at L2")
    stalls_p.add_argument("--board", default=None, help="default: the legacy configured board")
    stalls_p.add_argument("--json", action="store_true", help="print the stalls as a JSON list")
    stalls_p.add_argument("--capsule-home", default=None)

    lookout_p = sub.add_parser("lookout", help="The Lookout (spec D9): ship, status, check, silence, backfill, accept")
    lookout_p.add_argument("rest", nargs=argparse.REMAINDER)

    status_p = sub.add_parser("status", help="one glance: what runs, what needs you, what is stuck")
    status_p.add_argument("--json", action="store_true", help="print the snapshot as JSON")
    status_p.add_argument("--refs", action="store_true", help="with --json, keep internal ids")
    status_p.add_argument("--watch", action="store_true", help="redraw every few seconds")
    status_p.add_argument("--interval", type=float, default=4.0, help="--watch seconds (min 3)")
    status_p.add_argument("--capsule-home", default=None)
    _add_board_parser(sub)


def _add_board_parser(sub):
    board_p = sub.add_parser(
        "board", help="browse one board in the terminal (read-only)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="usage: board new <name> --repo <url-or-path> [--check CMD]... [--notify]\n"
               "example: capsule board new myapp --repo https://github.com/you/myapp.git"
               " --check 'npm test'\n--check replaces the default (Python) contract checks.")
    board_p.add_argument("board", nargs="?", default=None)
    board_p.add_argument("--once", action="store_true", help="print the list once and exit")
    board_p.add_argument("--capsule-home", default=None)
    board_p.add_argument("rest", nargs="*", help="`board new <name> --repo <url>`: a new board;"
                                                  " `board tick <name>`: set up its tick job")
    board_p.add_argument("--dry-run", action="store_true",
                         help="with `board tick`: print every step and change nothing")
    board_p.add_argument("--yes", action="store_true",
                         help="with `board tick`: take the defaults without asking")
    board_p.add_argument("--repo", default=None, help="with `board new`: the product repo (url or"
                                                       " absolute path)")
    board_p.add_argument("--notify", action="store_true",
                         help="with `board new`: turn Slack on for the new board")
    board_p.add_argument("--check", action="append", default=None, metavar="CMD",
                         help="with `board new`: a contract check command; repeat for several")


def _add_human_parser(sub):
    human_p = sub.add_parser("human", help="record one human intervention (R5; kind reset/unblock releases a card;"
                                           " accept sends a held card whose last run reported done to review;"
                                           " screen-ok/screen-fix answer a UI epic's first screen, row 369; amend"
                                           " edits a dispatched card's body/file_set, row 452; override skips one"
                                           " check id for one card, row 454 item 4; epic <branch> reassigns to a"
                                           " declared epic branch; park '<trigger>' holds the card until reset,"
                                           " t_91c331db; stop '<trigger>' parks after its run;"
                                           " wait <epic-branch|sha>"
                                           " holds the card until that epic or commit"
                                           " is in origin/main;"
                                           " reset lifts it sooner;"
                                           " archive '<why>' takes a card that will never"
                                           " resume out of its lane (nothing is deleted);"
                                           " close '<why>' marks work landed outside"
                                           " the factory done; assign <assignee> sets a"
                                           " ready/blocked/triage card's assignee"
                                           " to one that resolves to a routed role)")
    human_p.add_argument("board")
    human_p.add_argument("task_id")
    human_p.add_argument("kind")
    human_p.add_argument("text", nargs="+", help="the text; preview: accept|reject, then the words")
    human_p.add_argument("--tag", default=None, choices=["migration-reset", "handover-route"],
                         help="migration-reset: forced by the handover; handover-route: epic"
                              " routing at the take (needs --assignee); neither is in vs-U1")
    human_p.add_argument("--author", default=None, type=human.canonical_author,
                         choices=["owner", "cos"],
                         help="who acted; the chief of staff passes cos. Required in an agent"
                              " session (CLAUDECODE or AI_AGENT set); a person at a terminal"
                              " defaults to owner; a pipe, script or cron job without an agent"
                              " marker records cos")
    human_p.add_argument("--assignee", default=None,
                         help="epic only: the implementer assignee to set with the branch")
    human_p.add_argument("--body-file", default=None,
                         help="amend only: path to the card's replacement body (must exist, non-empty)")
    human_p.add_argument("--file-set", default=None,
                         help="amend only: replacement file_set, a comma list or a JSON "
                              "array of relative paths (full replacement, not a merge)")
    human_p.add_argument("--no-lint", action="store_true", dest="no_lint",
                         help="amend and answer: skip the narrowing lint (amend records "
                              "lint_skipped, answer starts '[scope lint skipped] ')")
    human_p.add_argument("--check", default=None, help="override only: the check id to skip in review")
    human_p.add_argument("--expect", default=None, help="check only: the card's check_expect")
    human_p.add_argument("--risk-flags", default=None, dest="risk_flags",
                         help="size only: comma-separated judges,data,public,big")
    human_p.add_argument("--ttl", default=None, help="override only: e.g. 72h (suffix s/m/h/d)")
    human_p.add_argument("--fix-card", default=None, dest="fix_card", help="override only: the linked fix card id")
    human_p.add_argument("--capsule-home", default=None)
    apply_p = sub.add_parser("human-apply", help="apply a board's queued human files now, under the"
                                                 " tick's lock; dispatches and sends nothing")
    apply_p.add_argument("--board", required=True)
    apply_p.add_argument("--capsule-home", default=None)


def _author_arg(parser):
    parser.add_argument("--author", default=None, type=human.canonical_author,
                        choices=["owner", "cos"],
                        help="required in an agent session (cos); see capsule human")


def _incident_args(ip, name):
    """The arguments of the incident commands that take their own (open, attach, fixcard, ...)."""
    if name == "open":
        ip.add_argument("--kind", required=True,
                        help="crash, hang, silent_failure, contradiction, human_intervention,"
                             " override or escaped_bug")
        ip.add_argument("--root-cause-key", required=True,
                        help="short stable slug (max 80 chars); the fingerprint hashes it")
        ip.add_argument("--description", default=None,
                        help="free prose (cause, fix reference); stored, never hashed")
        ip.add_argument("--evidence", action="append", default=[],
                        help="a path or event id; repeatable")
        ip.add_argument("--task", default=None)
        ip.add_argument("--run", default=None)
        ip.add_argument("--role", default=None)
    if name == "attach":
        ip.add_argument("--card", required=True)
    if name == "fixcard":
        ip.add_argument("--epic", required=True)
        ip.add_argument("--assignee", default=None)
    if name == "close-history":
        ip.add_argument("--list", required=True,
                        help='JSON lines {"id", "verdict": resolved|false_alarm|card_done,'
                             ' "evidence"}')
        ip.add_argument("--apply", action="store_true", help="write; default is a dry run")
        ip.add_argument("--list-sha", default=None,
                        help="sha256 of the list file, from its dry run; --apply needs it")
    if name == "app-event":
        ip.add_argument("file", nargs="?", default=None)


def _incident_ids(ip, name):
    """The id and author arguments of the commands that name one incident."""
    if name not in ("list", "open", "close-history", "app-event"):
        ip.add_argument("incident_id")
    incident_args.add(ip, name)
    if name in ("retest", "landed", "duplicate", "close"):
        _author_arg(ip)
    if name == "retest":
        ip.add_argument("new_test_id")
        ip.add_argument("--reason", required=True, help="why the test moved")
        ip.add_argument("--enforcer", default=None, help="also replace the enforcer text")
    if name == "duplicate":
        ip.add_argument("--of", required=True, help="the canonical incident id (same board)")
        ip.add_argument("--reason", default=None,
                        help="needed when the two share no fingerprint, fix card or kind+card")


def _add_incidents_parser(sub):
    inc_p = sub.add_parser("incidents", help="Korin's incident record (row 456): list, show, open, row, fixcard, close")
    inc_sub = inc_p.add_subparsers(dest="inc_cmd", required=True)
    for name, helptext in (("list", "every incident, oldest first"), ("show", "one incident's record"),
                           ("open", "open one by hand (the gauntlet, introspection): --kind, --root-cause-key, --evidence"),
                           ("row", "set the decision row the owner or architect recorded"),
                           ("fixcard", "put the fix card on an epic (--epic) and an implementer assignee"),
                           ("attach", "name the fix card: --card t_id or board:t_id"),
                           ("retest", "point a live incident at the recurrence test its fix"
                                     " landed under; closes nothing"),
                           ("landed", "a fix built outside the factory is on main: --sha"),
                           ("duplicate", "mark a live incident superseded by its canonical twin"
                                         " (--of); closes nothing"),
                           ("close", "close only if every closure check passes;"
                                     " else list what is missing"),
                           ("close-history", "close old incidents as history from a --list file"
                                             " (row 638 b); dry run unless --apply"),
                           ("app-event", "take one app_event.v1 FILE, or with no FILE"
                                         " drain the board's app-events inbox (D79)")):
        ip = inc_sub.add_parser(name, help=helptext)
        _incident_args(ip, name)
        _incident_ids(ip, name)
        if name == "list":
            ip.add_argument("--status", default=None,
                            choices=["open", "postmortem", "fix_pending", "closed", "superseded"])
        ip.add_argument("--board", default=None, help="default: the config's board")
        ip.add_argument("--capsule-home", default=None)


def _add_image_parser(sub):
    image_p = sub.add_parser("image", help="worker image tooling (R10): render a Dockerfile")
    image_sub = image_p.add_subparsers(dest="image_cmd", required=True)
    render_p = image_sub.add_parser("render", help="print the Dockerfile for capsule.toml [image];"
                                                   " runs no docker")
    render_p.add_argument("--project", default=".", help="the repo with capsule.toml and locks")
    render_p.add_argument("--lock", default=None, help="default: docker/IMAGES.lock")
    render_p.add_argument("-o", "--output", default=None, help="write here instead of stdout")
    build_p = image_sub.add_parser("build", help="build, smoke-test and record a board's image")
    build_p.add_argument("--project", required=True, help="the repo with capsule.toml")
    build_p.add_argument("--dry-run", action="store_true", help="print the plan, run nothing")
    which_p = image_sub.add_parser("which", help="print the image a board's runner would start")
    run_p = image_sub.add_parser("request-run", help="(tick step) build one image request's "
                                 "clone and write its result file; records nothing")
    run_p.add_argument("--task", required=True)
    run_p.add_argument("--timeout", type=int, default=1200, help="seconds for the docker build")
    for p in (build_p, which_p, run_p):
        p.add_argument("--board", required=True)
        p.add_argument("--capsule-home", default=None)


def _build_parser():
    parser = argparse.ArgumentParser(prog="capsule")
    sub = parser.add_subparsers(dest="cmd", required=True)
    _add_basic_parsers(sub)
    _add_human_parser(sub)
    _add_incidents_parser(sub)
    _add_image_parser(sub)
    vc_p = sub.add_parser("vault-context", help="row 626: the vault context a card would get")
    vc_p.add_argument("verb", choices=["show", "pending", "verdict"],
                      help="show: print the vault_context.v1 record (JSON) for CARD.json"
                      " ({title, body, rows, risk_flags}); the vault is $OBSIDIAN_VAULT_PATH."
                      " pending [--all]: the CoS review queue. verdict RUN_ID ITEM"
                      " kept|dropped REASON|missing PATH: record one judgement")
    vc_p.add_argument("card", nargs="*", help="show: CARD.json; verdict: its arguments")
    vc_p.add_argument("--all", action="store_true", help="pending: also unreviewed and hidden")
    vc_p.add_argument("--board", default=None)
    vc_p.add_argument("--capsule-home", default=None)
    vc_p.add_argument("--verdicts", default=None, help="default: maintenance/state/vault_eval/"
                      "verdicts.jsonl, or $CAPSULE_VAULT_VERDICTS")
    cli_intake.add_parser(sub)
    plan_cut.add_parser(sub)
    cli_prior.add_parser(sub)
    cli_ops.add_parser(sub)
    cli_names.add_parser(sub)
    cos_ask.add_parser(sub)
    golden_cli.add_parser(sub)
    loophole.add_parser(sub)
    token_accountant.add_parser(sub)
    auditor.add_parser(sub)
    host.add_parser(sub)
    blind_cli.add_parser(sub)
    doctor.add_parser(sub)
    install.add_parser(sub)
    rhythm.add_parser(sub)
    daily_pr.add_parser(sub)
    publish.add_parser(sub)
    threat_model.add_parser(sub)
    security.add_parser(sub)
    design_briefs.add_parser(sub)
    brainstorm.add_parser(sub)
    tick_watch.add_parser(sub)
    stall_owner.add_parser(sub)
    patrol_run.add_parser(sub)
    ask_gate.add_parser(sub)
    return parser


def _cmd_incidents(args, parser):
    from capsule import human, incidents_cli
    if args.inc_cmd in ("retest", "landed", "close"):    # they move the gate's inputs: refuse first
        try:
            args.author = human.cli_author(args.author)
        except ValueError as exc:
            print(f"capsule incidents {args.inc_cmd}: {exc}", file=sys.stderr)
            return 1
    return incidents_cli.main(args)


def _cmd_vault_context(args, parser):
    import json
    from capsule import vault_context, vault_context_cli
    if args.verb != "show":
        return vault_context_cli.main(args)
    path = args.card[0] if args.card else ""
    try:
        card = json.loads(open(path, encoding="utf-8").read())
    except (OSError, ValueError) as exc:
        print(f"capsule vault-context: cannot read {path}: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(vault_context.retrieve(card, {}), indent=2))
    return 0


def _cmd_image(args, parser):
    from capsule import image_build, image_render
    if args.image_cmd == "render":
        return image_render.main(args)
    return image_build.main(args)


def _cmd_lookout(args, parser):
    from capsule import lookout_cli
    return lookout_cli.main(args.rest)


def _cmd_run_one(args, parser):
    from capsule import worker
    return worker.run_one(args.board_db, args.spool_dir, args.run_id, args.role)


def _cmd_report(args, parser):
    from capsule import report, paths, touches
    capsule_home = paths.home(args.capsule_home)
    try:
        if args.your_time:
            lines = report._yt(capsule_home, args.board, int(time.time()))
            print("\n".join(["## YOUR TIME", *lines]))
            return 0
        doc = touches.evaluate(capsule_home, args.board, since=args.since)
    except (sqlite3.OperationalError, touches.NotMigrated) as exc:
        # a board with no Capsule db yet is a clean refusal,
        # not a raw sqlite traceback. So is a board still in the legacy schema.
        print(f"capsule report: no Capsule board {args.board!r} under {capsule_home} ({exc})", file=sys.stderr)
        return 1
    print(report.render_md(doc) if args.md else json.dumps(doc))
    return 0


def _cmd_check(args, parser):
    from capsule import paths, preflight
    return preflight.main(paths.home(args.capsule_home), board=args.board)


def _cmd_unblocked(args, parser):
    from capsule import paths, unblocked
    capsule_home = paths.home(args.capsule_home)
    argv2 = ["--json"] if args.json else []
    return unblocked.main(argv2, capsule_home=capsule_home)


def _cmd_stalls(args, parser):
    from capsule import stall_cli
    return stall_cli.main(args.board, args.capsule_home, args.json)


def _parse_file_set(text):
    """A JSON string array or a comma list -> clean relative paths; ValueError if bad."""
    if text.strip().startswith("["):
        try:
            items = json.loads(text)
        except ValueError:
            raise ValueError(f"--file-set is not valid JSON: {text!r}") from None
        if not isinstance(items, list) or not all(isinstance(i, str) for i in items):
            raise ValueError("--file-set JSON must be an array of strings")
    else:
        items = text.split(",")
    paths = [i.strip() for i in items]
    for p in paths:
        if (not p or any(c in p for c in "\"'[]") or p.startswith("/")
                or ".." in p.replace("\\", "/").split("/")):
            raise ValueError(f"--file-set entry {p!r} must be a non-empty relative path "
                             "with no quote, bracket or '..'")
    return paths


def _human_amend_or_assign(args, capsule_home):
    if args.kind == "amend":
        from capsule import amend as amend_mod
        try:
            file_set = _parse_file_set(args.file_set) if args.file_set is not None else None
        except ValueError as exc:
            print(f"capsule human amend: {exc}", file=sys.stderr)
            return 1
        try:
            result = amend_mod.amend(capsule_home, args.board, args.task_id, body_file=args.body_file,
                                     file_set=file_set, reason=args.text, who=args.author,
                                     lint=not args.no_lint)
        except amend_mod.AmendRefused as exc:
            print(f"capsule human amend: {exc}", file=sys.stderr)
            return 1
    else:
        from capsule import assign as assign_mod
        try:
            result = assign_mod.assign(capsule_home, args.board, args.task_id, args.text,
                                       who=args.author)
        except assign_mod.AssignRefused as exc:
            print(f"capsule human assign: {exc}", file=sys.stderr)
            return 1
    print(json.dumps(result))
    return 0


def _human_override(args, capsule_home, parser):
    from capsule import config as capsule_config
    from capsule import human, identity, override, paths
    if not (args.check and args.ttl and args.fix_card):
        parser.error("human override needs --check, --ttl and --fix-card")
    issuer = identity.resolve()
    if issuer is None:
        print("capsule human: override needs a resolvable operator identity"
              " ($CAPSULE_IDENTITY naming a file under ~/.config/capsule/identities/, mode 600)",
              file=sys.stderr)
        return 1
    # item 2: an irreversible check needs an interactive terminal to even
    # write the human file - a check `grant` itself cannot repeat, since the tick that
    # calls `grant` never has a TTY.
    try:
        cfg = capsule_config.load(paths.resolve_config(capsule_home, args.board))
        config_loaded = True
    except Exception:
        cfg, config_loaded = {}, False
    checked = override.normalize_check(args.check, cfg) or args.check
    irreversible = not config_loaded or checked in override.irreversible_checks(cfg)
    if irreversible and not (sys.stdin.isatty() and sys.stdout.isatty()):
        # this gate raises the bar for a host agent's normal
        # non-interactive shell; it does not stop a same-user process that allocates a
        # pty (`pty.spawn`, `script`, `expect`) - that stays inside the accepted host-
        # trust residual risk identity.py's docstring already names, not a new hole.
        print("capsule human: irreversible overrides require an interactive terminal;"
              " this raises the bar for host agents, it does not stop a same-user process"
              " that allocates a pty (accepted residual, see identity.py)", file=sys.stderr)
        return 1
    try:
        path = human.write(capsule_home, args.board, args.task_id, args.kind, args.text,
                           check=args.check, ttl=args.ttl, fix_card=args.fix_card, issuer=issuer,
                           author=args.author)
    except ValueError as exc:
        print(f"capsule human: {exc}", file=sys.stderr)
        return 1
    print(str(path))
    return 0


def _cmd_human(args, parser):
    from capsule import human, paths
    try:
        args.author = human.cli_author(args.author)
    except ValueError as exc:
        print(f"capsule human: {exc}", file=sys.stderr)
        return 1
    capsule_home = paths.home(args.capsule_home)
    words = [args.text] if isinstance(args.text, str) else args.text    # only preview takes several
    if len(words) > 1 and args.kind != "preview":
        parser.error("quote the text: only the preview verb takes several words")
    args.text = " ".join(words)
    kind = args.kind if args.kind in human.OWN_VERBS else "plain"
    for flag, (verbs, only, hint) in human.FLAG_USE.items():
        if vars(args).get(flag) not in (None, False) and kind not in verbs:
            parser.error(f"--{flag.replace('_', '-')} applies to {only} only; {hint}")
    if args.assignee is not None and args.kind != "epic":
        parser.error("--assignee applies to the epic verb only")
    if args.tag == "handover-route" and (args.kind != "epic" or not args.assignee):
        parser.error("--tag handover-route needs the epic verb and --assignee")
    if args.kind in ("amend", "assign"):
        return _human_amend_or_assign(args, capsule_home)
    if args.kind in ("check", "size", "depends", "workload", "runner"):
        from capsule import card_fields
        return card_fields.main(args, capsule_home)
    if args.kind == "override":
        return _human_override(args, capsule_home, parser)
    from capsule import human_preflight
    if human_preflight.check(capsule_home, args):
        return 1
    try:
        path = human.write(capsule_home, args.board, args.task_id, args.kind, args.text,
                           tag=args.tag, assignee=args.assignee, author=args.author,
                           lint=not args.no_lint)
    except ValueError as exc:   # any human.write refusal, e.g. inc_8b4a0a4b narrowing
        print(f"capsule human: {exc}", file=sys.stderr)
        return 1
    print(str(path))
    return 0


def _human_apply_check(home, board):
    """(cfg, why-not): the board must have a config naming it, a db."""
    from capsule import config, paths
    cfg, err = config.Loader(paths.config_path(home, board)).get()
    if cfg is not None and cfg["board"] != board:      # as the tick: never serve another board
        err = f"{paths.config_path(home, board)} names board {cfg['board']!r}, not {board!r}"
    elif cfg is None or not paths.board_db(board, home).exists():
        err = err or f"no board file for {board}"
    return cfg, err


def _human_apply_run(home, board, cfg):
    """The tick's `_interventions` step alone, then one line per file from its own event."""
    import contextlib
    from capsule import db, paths, spool, tick
    hdir = spool.human_dir(home, board)
    queued = {p.name for p in hdir.glob("*.json")}
    with contextlib.closing(db.connect(paths.board_db(board, home))) as conn:
        db.migrate(conn)
        tick.Tick(home, board=board)._interventions(conn, cfg, int(time.time()))
        left = sorted(queued - {p.name for p in hdir.glob("*.json")})
        print(f"processed {len(left)} human file(s)" if left else "no queued human files")
        for name in left:
            ev = conn.execute("SELECT task_id, payload FROM task_events WHERE kind='intervention'"
                              " AND json_extract(payload, '$.src')=? ORDER BY id DESC LIMIT 1",
                              (name,)).fetchone()
            if ev is None:
                print(f"  {name}: no event (unreadable file or unknown task)")
                continue
            doc = json.loads(ev["payload"])
            card = conn.execute("SELECT status FROM tasks WHERE id=?", (ev["task_id"],))
            print(f"  {name}: {ev['task_id']} {doc['kind']} -> "
                  f"{doc.get('outcome') or 'recorded'}; card now {card.fetchone()[0]}")


def _cmd_human_apply(args, parser):
    """The tick's own human-file step, once, under its single-writer lock: no dispatch, no
    outbox flush. A live tick holding the lock applies the files itself, so this refuses."""
    from capsule import paths, tick_guard
    home, board = paths.home(args.capsule_home), args.board
    cfg, err = _human_apply_check(home, board)
    if err:
        print(f"capsule human-apply: {err}", file=sys.stderr)
        return 1
    if tick_guard.held(home, board):
        print("capsule human-apply: a live tick holds this board's lock; it applies the files"
              " itself", file=sys.stderr)
        return 3
    lock = tick_guard.guard_start(home, board)      # SystemExit(3) if a tick won the race
    try:
        _human_apply_run(home, board, cfg)
    finally:
        lock.close()
    return 0


def _write_beat(path):
    """`<board dir>/tick.beat`: `{"at", "pid"}`, written once per pass."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name("tick.beat.tmp")
        tmp.write_text(json.dumps({"at": time.time(), "pid": os.getpid()}))
        os.replace(tmp, path)
    except OSError as exc:
        print(f"capsule tick: beat not written: {exc}", file=sys.stderr)


def _clocks():
    return time.time(), time.monotonic()


def _log_gap(loop, board, gap):
    """One `host_gap` event when `tick_watch.host_gap` gave a gap (the Mac slept)."""
    if gap:
        try:
            loop._events().log("host_gap", "capsule", f"host gap {gap} s", board=board,
                               payload={"gap_s": gap})
        except Exception as exc:
            print(f"capsule tick: host_gap not logged: {exc}", file=sys.stderr)


def _stale_code(loaded):
    """True when a tick-code path differs between `loaded` and the release checkout's HEAD (the
    tick-code list of maintenance/after_landing.py). A git failure is not stale."""
    from capsule import boundary, paths
    code = ["capsule/", "bin/capsule-tick.sh", "contracts/schemas/", *boundary.MAINT]
    got = subprocess.run(["git", "-C", str(paths.REPO), "diff", "--name-only", loaded, "HEAD",
                          "--", *code], text=True, capture_output=True)
    return got.returncode == 0 and bool(got.stdout.split())


def _quiet(loop, board, home):
    """The test of `quiet()` in bin/capsule-tick.sh (no open task_runs, no live lander.checking
    worker) plus no validator job in memory and no child worker. Unreadable = not quiet."""
    from capsule import paths
    if loop.children or loop.contracts.jobs:
        return False
    path = paths.board_dir(board, home) / "kanban.db"
    if not path.exists():
        return True            # a fresh board has nothing running (and mode=ro would not create it)
    db = f"file:{path}?mode=ro"
    try:
        with contextlib.closing(sqlite3.connect(db, uri=True)) as c:
            runs = c.execute("select count(*) from task_runs where ended_at is null").fetchone()[0]
            row = c.execute("select value from guard_state where key='lander.checking'").fetchone()
    except sqlite3.Error:
        return False
    if runs:
        return False
    try:
        if row:
            os.kill(int(json.loads(row[0])["pid"]), 0)
            return False       # the lander worker is alive
    except (ProcessLookupError, ValueError, KeyError, TypeError):
        return True            # a dead or garbled lander record is no landing
    except OSError:
        return False
    return True


def _stale_exit(loop, board, loaded):
    """Between passes, with no worker running: log `tick_stale_restart` and exit 75 so launchd
    (KeepAlive on failure) starts the tick on the new code. tick_watch counts only its own
    restarts, so this is no crash loop."""
    try:
        loop._events().log("tick_stale_restart", "capsule", f"tick code moved from {loaded[:12]}",
                           board=board, payload={"loaded": loaded})
    except Exception as exc:
        print(f"capsule tick: tick_stale_restart not logged: {exc}", file=sys.stderr)
    print(f"capsule tick: code changed since {loaded[:12]}, exiting 75 for launchd to restart",
          file=sys.stderr)
    return 75


def _keep_awake(pid):
    """`caffeinate -i -s -w <pid>`: no idle or system sleep while the tick lives; it ends with the
    tick. Nothing where there is no `caffeinate`."""
    exe = shutil.which("caffeinate")
    if exe:
        with contextlib.suppress(OSError):
            subprocess.Popen([exe, "-i", "-s", "-w", str(pid)], stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _cmd_tick(args, parser):
    from capsule import config, paths, tick, tick_guard, tick_watch
    home = paths.home(args.capsule_home)
    if args.board is not None:
        ok, msg = tick_guard.board_guard(home, args.board)
        if not ok:
            # exit 0, no lock: KeepAlive restarts only an unsuccessful exit (ruling Q9)
            print(f"capsule tick: {msg}", file=sys.stderr)
            return 0
    pin = args.board
    if pin is None:     # one read decides both the lock and the pin (no-board two-writer hole)
        cfg, err = config.Loader(paths.config_path(home)).get()
        if cfg is None:
            print(f"capsule tick: config unreadable, refusing rather than defaulting the board"
                  f" lock: {err}", file=sys.stderr)
            raise SystemExit(3)
        pin = cfg["board"]          # validated non-empty str (config.py:63)
    # held for the tick's whole life
    _lock = tick_guard.guard_start(home, pin, replace=args.replace)
    loop = tick.Tick(home, board=args.board, pin_board=pin)
    if not args.once:   # `<pid> <sha>` of the code this process loaded: after_landing's stale test
        sha_file = tick_guard.lock_path(home, pin).with_suffix(".sha")
        got = subprocess.run(["git", "-C", str(paths.REPO), "rev-parse", "HEAD"], text=True,
                             capture_output=True)
        sha_file.unlink(missing_ok=True)       # no record reads as stale
        loaded = got.stdout.strip() if got.returncode == 0 else None
        if loaded:
            sha_file.write_text(f"{os.getpid()} {loaded}\n")
    beat_path = paths.board_dir(pin, home) / "tick.beat"
    if not args.once:
        _keep_awake(os.getpid())      # no Mac sleep while the tick lives (G7)
        _write_beat(beat_path)
    while True:
        loop.code_note = None
        if not args.once and loaded and _stale_code(loaded):
            if _quiet(loop, pin, home):
                return _stale_exit(loop, pin, loaded)
            head = subprocess.run(["git", "-C", str(paths.REPO), "rev-parse", "--short=12", "HEAD"],
                                  text=True, capture_output=True).stdout.strip() or "unknown"
            loop.code_note = f"stale (code {loaded[:12]} vs {head}), restart waits: busy"
        t0 = _clocks()
        try:
            counts = loop.once()
        except Exception:
            traceback.print_exc()
            if args.once:
                return 1
        else:
            if args.once:
                print(json.dumps(counts))
                return 0
        _log_gap(loop, pin, tick_watch.host_gap(*t0, *_clocks()))      # the Mac slept in the pass
        _write_beat(beat_path)
        t1 = _clocks()
        time.sleep((loop.cfg or {}).get("caps", {}).get("tick_seconds", 30))
        _log_gap(loop, pin, tick_watch.host_gap(*t1, *_clocks()))      # the Mac slept in the wait


def _cmd_board(args, parser):
    if (args.dry_run or args.yes) and args.board != "tick":    # `board new` would write for real
        parser.error("--dry-run and --yes only go with `board tick <name>`")
    if args.check and args.board != "new":
        parser.error("--check only goes with `board new <name> --repo <url>`")
    if args.notify and args.board != "new":
        parser.error("--notify only goes with `board new <name> --repo <url>`")
    if args.board == "new":
        return cli_ops.handle(args, parser)
    if args.board == "tick":
        return board_tick.run(args, parser)
    if args.rest or args.repo:
        parser.error("board takes one board name (or `new <name> --repo <url>`"
                     " or `tick <name>`)")
    from capsule import boardview
    return boardview.run(args)


def _cmd_status(args, parser):
    from capsule import status_cli
    return status_cli.run(args, parser)


DISPATCH = {
    "tick": _cmd_tick,
    "run-one": _cmd_run_one,
    "report": _cmd_report,
    "check": _cmd_check,
    "unblocked": _cmd_unblocked,
    "stalls": _cmd_stalls,
    "human": _cmd_human,
    "human-apply": _cmd_human_apply,
    "incidents": _cmd_incidents,
    "image": _cmd_image,
    "vault-context": _cmd_vault_context,
    "lookout": _cmd_lookout,
    "status": _cmd_status,
    "board": _cmd_board,
    "intake": cli_intake.handle,
    "plan": plan_cut.handle,
    "prior": cli_prior.handle,
    "outbox-send": cli_ops.handle,
    "card": cli_ops.handle,
    "pause": cli_ops.handle,
    "unpark": cli_ops.handle,
    "names": cli_names.handle,
    "cos": cos_ask.handle,
    "golden": golden_cli.handle,
"loophole": loophole.handle,
    "tokens": token_accountant.handle,
    "auditor": auditor.handle,
    "host": host_registry.handle,
    "blind": blind_cli.handle,
    "doctor": doctor.handle,
    "install": install.handle,
    "rhythm": rhythm.main,
    "pr": daily_pr.handle,
    "publish": publish.handle,
    "threat-model": threat_model.handle,
    "security": security.handle,
    "design": design_briefs.handle,
    "brainstorm": brainstorm.handle,
    "tick-watch": tick_watch.handle,
    "veto": stall_owner.handle,
    "patrol": patrol_run.handle,
    "why": patrol_run.handle,
    "ask-gate": ask_gate.handle,
}


def main(argv=None):
    parser = _build_parser()
    args = parser.parse_args(argv)
    return DISPATCH[args.cmd](args, parser)


if __name__ == "__main__":
    sys.exit(main())
