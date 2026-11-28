"""`python3 -m capsule auditor packet --board B [--window START END] [--prior FILE] [--out FILE]`
(EVAL-06, contracts/auditor.md): the deterministic audit packet the weekly auditor reads.

One board, one window (epoch seconds, default the 7 days ending now), read-only. A source the
board does not have (no table, a writer that never wrote, an unreadable board) puts its metric
name in `missing` and leaves it out of `metrics`; a source that exists and recorded nothing in
the window is 0. `worse` compares against an earlier packet (`--prior`): higher is worse for
every metric but `landed`."""
import json
import sqlite3
import sys
import time
from pathlib import Path

from capsule import db, paths, safe_write, spool

SCHEMA = "audit_packet.v1"
WEEK = 7 * 86400
STUCK_AFTER = 48 * 3600
ROUNDS_TRIGGER = 30
LOWER_IS_WORSE = ("landed",)
FRESH = "COALESCE(tokens_in,0) + COALESCE(tokens_out,0) + COALESCE(tokens_cache_write,0)"
# one review round per implementer run that completed or was sent to review (review_scope's rule)
ROUND_RUNS = ("SELECT COUNT(*) FROM task_runs WHERE profile='implementer' AND started_at>=?"
              " AND started_at<? AND (outcome='completed' OR id IN"
              " (SELECT run_id FROM task_events WHERE kind='request_review'))")
STUCK = ("SELECT COUNT(*) FROM tasks t WHERE t.status NOT IN ('done','archived')"
         " AND MAX(t.created_at, COALESCE((SELECT MAX(created_at) FROM task_events ev"
         " WHERE ev.task_id=t.id AND ev.created_at<=?1), 0)) < ?1 - ?2")


class _Absent(Exception):
    """The source for a metric is not there: it goes in `missing`, never as 0."""


def _one(conn, sql, *params):
    if conn is None:
        raise _Absent()
    return conn.execute(sql, params).fetchone()


def _landed(conn, start, end, _ctx):
    return _one(conn, "SELECT COUNT(*) FROM tasks WHERE status='done' AND completed_at>=?"
                " AND completed_at<?", start, end)[0]


def _tokens(conn, start, end, _ctx):
    if _one(conn, "SELECT 1 FROM task_runs WHERE tokens_in IS NOT NULL OR tokens_out IS NOT NULL"
            " OR tokens_cache_write IS NOT NULL LIMIT 1") is None:
        raise _Absent()     # no run on the board ever wrote tokens: a quiet week is not 0
    return _one(conn, f"SELECT COALESCE(SUM({FRESH}), 0) FROM task_runs WHERE started_at>=?"
                " AND started_at<?", start, end)[0]


def _review_rounds(conn, start, end, _ctx):
    return _one(conn, ROUND_RUNS, start, end)[0]


def _interventions(_conn, start, end, ctx):
    if not spool.human_dir(ctx["home"], ctx["board"]).is_dir():
        raise _Absent()
    by_task = spool.human_by_task(ctx["home"], ctx["board"], start)
    return sum(1 for docs in by_task.values() for d in docs if int(d.get("at") or 0) < end)


def _incidents(conn, start, end, _ctx):
    return _one(conn, "SELECT COUNT(*) FROM incidents WHERE opened_at>=? AND opened_at<?",
                start, end)[0]


def _flaky_tests(conn, start, end, _ctx):
    if _one(conn, "SELECT 1 FROM task_events WHERE kind IN ('landing_checked','landing')"
            " LIMIT 1") is None:     # the lander never ran: no flaky event is not zero flakes
        raise _Absent()
    return _one(conn, "SELECT COUNT(*) FROM task_events WHERE kind='landing_flaky'"
                " AND created_at>=? AND created_at<?", start, end)[0]


def _stuck_cards(conn, _start, end, _ctx):
    return _one(conn, STUCK, end, STUCK_AFTER)[0]


COUNTERS = (("landed", _landed), ("tokens", _tokens), ("review_rounds", _review_rounds),
            ("interventions", _interventions), ("incidents", _incidents),
            ("flaky_tests", _flaky_tests), ("stuck_cards", _stuck_cards))


def _measure(fn, conn, start, end, ctx):
    try:
        return fn(conn, start, end, ctx)
    except (_Absent, sqlite3.Error, OSError, ValueError):
        return _Absent


def _metrics(conn, start, end, ctx):
    """({metric: value}, [missing metric names]) for one window."""
    metrics, missing = {}, []
    for name, fn in COUNTERS:
        value = _measure(fn, conn, start, end, ctx)
        if value is _Absent:
            missing.append(name)
        else:
            metrics[name] = value
    if "landed" in metrics and "tokens" in metrics:
        landed = metrics["landed"]
        metrics["cost_per_card"] = metrics["tokens"] / landed if landed else None
    else:
        missing.append("cost_per_card")
    return metrics, missing


def _repeats(conn, start, end):
    """Incidents opened in the window that repeat an earlier one (incidents.open_incident
    stamps `repeat_of` when the fingerprint was closed within 30 days); None when unreadable."""
    if conn is None:
        return None
    try:
        rows = conn.execute(
            "SELECT id, repeat_of, kind FROM incidents WHERE opened_at>=? AND opened_at<?"
            " AND repeat_of IS NOT NULL ORDER BY opened_at, id", (start, end)).fetchall()
    except sqlite3.Error:
        return None
    return [{"incident": r[0], "repeat_of": r[1], "kind": r[2]} for r in rows]


def _worse(metrics, prior_metrics):
    out = []
    for name in sorted(set(metrics) & set(prior_metrics)):
        before, after = prior_metrics[name], metrics[name]
        if not all(isinstance(v, (int, float)) for v in (before, after)):
            continue    # a null cost_per_card (nothing landed) compares with nothing
        if (after < before) if name in LOWER_IS_WORSE else (after > before):
            out.append({"metric": name, "before": before, "after": after})
    return out


def _open_board(path):
    try:
        return db.connect_ro_tolerant(path)
    except sqlite3.Error:
        return None


def _packet(board, home, start, end, prior):
    conn = _open_board(paths.board_db(board, home))
    ctx = {"home": home, "board": board}
    try:
        metrics, missing = _metrics(conn, start, end, ctx)
        repeats = _repeats(conn, start, end)
        since = prior["window"][1] if prior else start
        rounds = _measure(_review_rounds, conn, since, end, ctx)
    finally:
        if conn is not None:
            conn.close()
    if repeats is None:
        repeats = []
        missing.append("repeats")
    trigger = "rounds" if rounds is not _Absent and rounds >= ROUNDS_TRIGGER else "weekly"
    return {"schema": SCHEMA, "board": board, "window": [start, end], "trigger": trigger,
            "metrics": metrics, "repeats": repeats, "missing": sorted(missing),
            "prior_window": prior["window"] if prior else None,
            "worse": _worse(metrics, prior["metrics"]) if prior else []}


def add_parser(sub):
    p = sub.add_parser("auditor", help="the weekly factory audit's deterministic packet (EVAL-06)")
    verbs = p.add_subparsers(dest="auditor_cmd", required=True)
    pk = verbs.add_parser("packet", help="one board, one window; read-only")
    pk.add_argument("--board", required=True)
    pk.add_argument("--capsule-home", default=None)
    pk.add_argument("--window", nargs=2, type=int, metavar=("START", "END"),
                    help="epoch seconds; default the 7 days ending now")
    pk.add_argument("--prior", default=None, help="an earlier packet (JSON file)")
    pk.add_argument("--out", default=None, help="write here atomically; default stdout")


def _load_prior(path):
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(doc, dict) or doc.get("schema") != SCHEMA:
        raise ValueError(f"not an {SCHEMA} packet")
    window = doc.get("window")
    if not isinstance(doc.get("metrics"), dict) or not (
            isinstance(window, list) and len(window) == 2
            and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in window)):
        raise ValueError("`window` must be 2 numbers and `metrics` an object")
    return doc


def handle(args, parser):     # signature matches every other DISPATCH entry; parser unused here
    start, end = args.window or (int(time.time()) - WEEK, int(time.time()))
    if start > end:
        print("capsule auditor packet: --window START must not be after END", file=sys.stderr)
        return 2
    try:
        prior = _load_prior(args.prior) if args.prior else None
    except (OSError, ValueError) as exc:
        print(f"capsule auditor packet: --prior: {exc}", file=sys.stderr)
        return 2
    packet = _packet(args.board, paths.home(args.capsule_home), start, end, prior)
    text = json.dumps(packet, indent=2, sort_keys=True) + "\n"
    if not args.out:
        sys.stdout.write(text)
        return 0
    out = Path(args.out).resolve()
    try:
        safe_write.atomic_write(out.parent, out, text)
    except OSError as exc:
        print(f"capsule auditor packet: --out: {exc}", file=sys.stderr)
        return 1
    return 0
