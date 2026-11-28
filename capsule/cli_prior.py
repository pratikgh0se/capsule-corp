"""`python3 -m capsule prior "<title>" [--body FILE]` (row 585): print the `## Prior verdicts`
block for a hand-cut card, to paste into its body before the card is created. Read-only."""
import sys
from pathlib import Path


def add_parser(sub):
    p = sub.add_parser(
        "prior", help="earlier verdicts on the same root cause as a card about to be cut (row 585)"
    )
    p.add_argument("title")
    p.add_argument("--body", default=None, help="file holding the card body")
    p.add_argument("--board", default=None)
    p.add_argument("--capsule-home", default=None)


def handle(args, parser):  # signature matches every other DISPATCH entry; parser unused here
    from capsule import config, db, paths, prior_verdicts

    home = paths.home(args.capsule_home)
    try:
        body = Path(args.body).read_text(encoding="utf-8") if args.body else ""
    except OSError as exc:
        print(f"capsule prior: --body: {exc}", file=sys.stderr)
        return 1
    try:
        cfg = config.load(paths.resolve_config(home, args.board))
    except (OSError, ValueError):
        cfg = {}
    board = args.board or cfg.get("board")
    path = paths.board_db(board, home) if board else None
    conn = db.connect_ro(path) if path is not None and path.exists() else None
    try:
        out = prior_verdicts.section(conn, cfg, args.title, body).strip()
        print(out or "no prior verdicts found")
    finally:
        if conn is not None:
            conn.close()
    return 0
