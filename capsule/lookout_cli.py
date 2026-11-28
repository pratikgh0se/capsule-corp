"""`python3 -m capsule lookout <verb>` (spec D9): ship, status, check, silence;
backfill and accept join this dispatcher."""
import argparse
import json
import sys

from capsule import config, paths


def main(argv):
    ap = argparse.ArgumentParser(prog="python3 -m capsule lookout")
    sub = ap.add_subparsers(dest="verb", required=True)
    for name in ("ship", "status", "check", "silence"):
        p = sub.add_parser(name)
        p.add_argument("--capsule-home", default=None)
        if name == "ship":
            p.add_argument("--full", action="store_true", help="ask the tick for a full resync, then ship")
        if name == "silence":
            p.add_argument("--for", dest="duration", default="", help="e.g. 30m, 2h (at most 12h)")
    args = ap.parse_args(argv)
    home = paths.home(args.capsule_home)
    from capsule import lookout_ship
    if args.verb == "ship":
        rc = lookout_ship.ship(home, full=args.full)
        from capsule import secret_guard_ship
        try:
            guard_rc = secret_guard_ship.ship()
        except Exception as exc:                      # never costs lookout its own rc
            print(f"secret_guard ship: {type(exc).__name__}: {exc}", file=sys.stderr)
            guard_rc = 1
        return rc or guard_rc
    if args.verb == "status":
        boards = {b: lookout_ship.status(home, b) for b in lookout_ship.spool_boards(home) if b}
        # shared spool's flat fields unchanged; every board's own under "boards"
        print(json.dumps(dict(lookout_ship.status(home), boards=boards,
                              total_unshipped_bytes=lookout_ship.unshipped(home),
                              stalled=lookout_ship.stalled(home))))
        return 0
    if args.verb == "check":
        return lookout_ship.check(home, config.load(paths.config_path(home)))
    return lookout_ship.silence(args.duration)
