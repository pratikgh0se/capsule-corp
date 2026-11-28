"""TEAM-23: a card with risk flags waits for `docs/design/<card-id>.md` in its epic's clone
before dev starts (`dispatch.ready`), when the board sets `"design_briefs": true`; and
`design reaches-owner`, the threshold that says which briefs reach the owner."""
import json

from capsule import card_fields, paths, workspace

BIG_FILES = 20


def missing(cfg, row, home=None):
    """The expected brief path (in the epic's clone) while the card needs a brief and the file
    is not there, else None. Pure: `held` and `unblocked.dispatcher_hold_reason` share it.
    NULL, "" and "[]" risk_flags all mean no flags."""
    if not cfg.get("design_briefs"):
        return None
    try:
        flags = json.loads(row["risk_flags"]) if row["risk_flags"] else []
    except ValueError:
        flags = [row["risk_flags"]]
    if not flags:
        return None
    clone = paths.board_dir(cfg["board"], home) / "work" / workspace._slug(row["branch_name"])
    path = clone / "docs" / "design" / f"{row['id']}.md"
    return None if path.is_file() else path


def _reaches(files, flags, reversible):
    if reversible == "no":
        return "yes", "the change cannot be undone"
    for flag in ("public", "data"):
        if flag in flags:
            return "yes", f"the card is flagged {flag}"
    if files > BIG_FILES:
        return "yes", f"{files} files is more than {BIG_FILES}"
    return "no", "reversible, no public or data flag, and at most 20 files"


def add_parser(sub):
    p = sub.add_parser("design", help="design briefs (TEAM-23)")
    d = p.add_subparsers(dest="design_cmd", required=True)
    r = d.add_parser("reaches-owner", help="yes or no: does this card's design reach the owner")
    r.add_argument("--files", required=True, type=int, help="files the card changes")
    r.add_argument("--risk", default="", help="comma list of risk flags (may be empty)")
    r.add_argument("--reversible", required=True, choices=("yes", "no"))


def handle(args, parser):
    flags = [f for f in args.risk.split(",") if f]
    if args.files < 0 or not set(flags) <= set(card_fields.RISK_FLAGS):
        known = ",".join(card_fields.RISK_FLAGS)
        parser.error(f"--files must be 0 or more and --risk from {known}")
    answer, why = _reaches(args.files, flags, args.reversible)
    print(answer)
    print(why)
    return 0
