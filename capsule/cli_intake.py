"""`python3 -m capsule intake` (factory-autonomy 3.5): file a card on a Capsule-owned board; the
board's tick ingests it next pass."""
import sys
from pathlib import Path

from capsule.card_fields import RUNNERS, WORKLOADS


def add_parser(sub):
    intake_p = sub.add_parser("intake", help="file a card on a Capsule-owned board (factory-autonomy 3.5); the"
                                             " board's tick ingests it next pass")
    intake_p.add_argument("board")
    intake_p.add_argument("--title", required=True)
    intake_p.add_argument("--body-file", required=True)
    intake_p.add_argument("--name", default=None, help="short name for the status board")
    intake_p.add_argument("--epic", default=None)
    intake_p.add_argument("--assignee", default=None)
    intake_p.add_argument("--file-set", default=None, help="comma-separated")
    intake_p.add_argument("--check", default=None, help="the card's check_cmd")
    intake_p.add_argument("--expect", default=None, help="text the check's output must contain")
    intake_p.add_argument("--size", default=None, help="the card's size_tag")
    intake_p.add_argument("--risk-flags", default=None,
                          help="judges,data,public,big")
    intake_p.add_argument("--workload", default=None, help="|".join(WORKLOADS))
    intake_p.add_argument("--runner", default=None, help="|".join(RUNNERS))
    intake_p.add_argument("--capsule-home", default=None)


def _epic_problem(home, args):
    """Why a card with --epic would not route (it would land in triage with no epic), or None."""
    if not args.epic:
        return None
    from capsule import config, paths
    try:
        cfg = config.load(paths.resolve_config(home, args.board))
    except (OSError, ValueError):
        return None     # no usable board config to judge by
    if cfg.get("board") != args.board:
        return None     # legacy fallback config of another board
    roles = cfg.get("assignees") or {}
    names = sorted(n for n, r in roles.items() if r == "implementer")
    epics = config.epic_branches(cfg)
    if args.epic not in epics:
        return f"--epic {args.epic} is not a declared epic here (one of: {', '.join(epics)})"
    if roles.get(args.assignee) != "implementer":
        return (f"--epic needs --assignee <implementer> (one of: {', '.join(names)}) "
                "or the card lands in triage with no epic")
    return None


def handle(args, parser):    # signature matches every other DISPATCH entry; parser unused here
    from capsule import card_fields, intake, paths
    home = paths.home(args.capsule_home)
    try:
        body = Path(args.body_file).read_text(encoding="utf-8")
    except OSError as exc:
        print(f"capsule intake: --body-file: {exc}", file=sys.stderr)
        return 1
    if not body.strip():
        print("capsule intake: --body-file is empty", file=sys.stderr)
        return 1
    try:
        fields = card_fields.clean({"check_cmd": args.check, "check_expect": args.expect,
                                    "size_tag": args.size, "risk_flags": args.risk_flags,
                                    "workload": args.workload, "runner": args.runner})
    except card_fields.FieldsRefused as exc:
        print(f"capsule intake: {exc}", file=sys.stderr)
        return 1
    problem = _epic_problem(home, args)
    if problem:
        print(f"capsule intake: {problem}", file=sys.stderr)
        return 1
    print(intake.enqueue(home, args.board, kind="card", fingerprint="card", title=args.title, body=body,
                         source_board="operator", source_task=None, epic=args.epic, assignee=args.assignee,
                         file_set=args.file_set.split(",") if args.file_set else None,
                         name=args.name, fields=fields))
    return 0
