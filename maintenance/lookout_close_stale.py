#!/usr/bin/env python3
"""lookout_close_stale.py - close the old-system-era "needs me" opens that will never close
(spec 9.2)."""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
_ROOT = str(Path(__file__).resolve().parents[1])
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
from capsule import legacy_names, paths  # noqa: E402
import events  # noqa: E402

CUTOFF = "2026-09-26"
SQL = ("with rows as (select coalesce(nullif(task_id, ''), nullif(slug, '')) as key, board, ts,"
       " payload->>'state' as state, payload->>'event' as event from events where kind = 'card'"
       " and payload->>'state' in ('open', 'closed')"
       " and source not in ('capsule', 'lookout-publisher', 'lookout-migration')),"
       " last as (select distinct on (key) key, board, ts, state, event from rows where key is not null"
       " order by key, ts desc)"
       f" select key, coalesce(board, ''), ts, coalesce(event, '') from last where state = 'open' and ts < '{CUTOFF}'"
       " order by ts;")


def _psql(sql):
    r = events.docker_run(["exec", "-i", paths.PG_CONTAINER, "psql", "-U", "ops", "-d", "ops",
                           "-q", "-t", "-A", "-F", "|", "-v", "ON_ERROR_STOP=1", "-f", "-"],
                          input=sql, text=True, capture_output=True, timeout=60)
    return r.returncode, r.stdout


def open_keys(psql=None):
    rc, out = (psql or _psql)(SQL)
    if rc != 0:
        raise RuntimeError(out.strip()[:200])
    keys = []
    for line in out.splitlines():
        if line.count("|") >= 3:
            key, board, ts, event = line.split("|", 3)
            keys.append({"key": key, "board": board or None, "ts": ts, "event": event})
    return keys


def main(argv=None, psql=None, spool=None):
    ap = argparse.ArgumentParser(prog="lookout_close_stale.py")
    ap.add_argument("--apply", action="store_true", help="append the closing rows (default: print only)")
    args = ap.parse_args(argv)
    try:
        keys = open_keys(psql)
    except RuntimeError as exc:
        print(f"lookout_close_stale: {exc}", file=sys.stderr)
        return 2
    for k in keys:
        print(f"{'close' if args.apply else 'would close'} {k['board']} {k['key']} (open since {k['ts']}, {k['event']})")
        if args.apply:
            is_task = k["key"].startswith("t_")
            events.log("card", "lookout-migration", f"closed stale old-era open {k['key']}",
                       board=k["board"],
                       task_id=k["key"] if is_task else None, slug=None if is_task else k["key"],
                       payload={"event": "closed_stale", "state": "closed",
                                "reason": legacy_names.STALE_CLOSE_REASON}, spool=spool)
    print(f"lookout_close_stale: {len(keys)} open keys {'closed' if args.apply else 'found (dry run)'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
