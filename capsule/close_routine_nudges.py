"""One-time clean-up, run by the CoS (never from the tick): close the open human_intervention
incidents whose source intervention is a routine cos nudge.
`python3 -m capsule.close_routine_nudges --board B`."""
import argparse
import sys

from capsule import db, incidents, paths, routine_nudges


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--board", required=True)
    ap.add_argument("--capsule-home", default=None)
    args = ap.parse_args(argv)
    board_db = paths.board_db(args.board, paths.home(args.capsule_home))
    if not board_db.exists():
        print(f"no board {args.board!r}", file=sys.stderr)
        return 1
    conn = db.connect(board_db)
    try:
        closed, left = 0, 0
        for inc in incidents.listing(conn, "open"):
            if not routine_nudges.is_routine_nudge(conn, inc):
                left += inc["kind"] == "human_intervention"
                continue
            incidents.close(conn, inc["id"], by="cos", vault="", reason=incidents.ROUTINE_REASON)
            closed += 1
    finally:
        conn.close()
    print(f"closed {closed}, left {left} (not routine cos)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
