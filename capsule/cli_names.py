"""`python3 -m capsule names backfill <board> [--dry-run]` (visibility spec card 5)."""
import sys
import tempfile

BATCH = 25
CLOSED = ("done", "archived")
TOKENS = {}   # model tokens summed across this process's draft calls
_STR = {"type": "string"}
SCHEMA = {"type": "object", "required": ["names"], "properties": {"names": {
    "type": "array", "items": {"type": "object", "required": ["id", "name"],
                               "properties": {"id": _STR, "name": _STR}}}}}
SYSTEM = ("You name kanban tasks. For each task give a plain-English name of 3 to 6 words, no"
          " quotes, no ids, no trailing punctuation. Return every task id you were given.")


def add_parser(sub):
    p = sub.add_parser("names", help="task names and numbers (visibility spec card 5)")
    names_sub = p.add_subparsers(dest="names_cmd", required=True)
    bp = names_sub.add_parser("backfill", help="number old tasks; draft names for open ones")
    bp.add_argument("board")
    bp.add_argument("--dry-run", action="store_true", help="print 'number name <- title'; no write")
    bp.add_argument("--capsule-home", default=None)


def _plan_numbers(rows):
    """rows: (id, number, created_at). Returns {id: number} for the NULL-number rows."""
    used = {r[1] for r in rows if r[1] is not None}
    todo = sorted((r for r in rows if r[1] is None), key=lambda r: (r[2], r[0]))
    out, n = {}, 0
    for r in todo:
        n += 1
        while n in used:
            n += 1
        out[r[0]] = n
    return out


def _draft_with_haiku(items):
    """items: [(id, title)]. Returns {id: name} from one `claude -p` Haiku call."""
    from capsule.models import adapter_for
    route = {"provider": "claude", "model": "haiku", "turn_budget": 3}
    flat = [(tid, " ".join(title.split())) for tid, title in items]
    prompt = "\n".join(f"{tid}\t{title}" for tid, title in flat)
    with tempfile.TemporaryDirectory() as work:
        bundle = {"system": SYSTEM, "prompt": prompt}
        limits = {"schema": SCHEMA, "tools": "", "timeout": 300}
        res = adapter_for(route).run("namer", bundle, work, limits)
    for key, count in (res.tokens or {}).items():
        TOKENS[key] = TOKENS.get(key, 0) + count
    if res.error_class or not isinstance(res.report, dict):
        raise RuntimeError(f"name draft failed: {res.error_class or 'no structured report'}")
    named = [n for n in res.report.get("names", [])
             if isinstance(n, dict) and {"id", "name"} <= set(n)]
    return {n["id"]: " ".join(str(n["name"]).split()) for n in named}


def _plan_names(open_rows, draft=None):
    """open_rows: [(id, title)]. Keeps only 3-6 word names for ids that were asked about."""
    draft = draft or _draft_with_haiku
    out = {}
    for i in range(0, len(open_rows), BATCH):
        batch = open_rows[i:i + BATCH]
        asked = {tid for tid, _ in batch}
        for tid, name in draft(batch).items():
            if tid in asked and 3 <= len(name.split()) <= 6:
                out[tid] = name
    return out


def _numbers(conn):
    rows = conn.execute("SELECT id, number, created_at FROM tasks").fetchall()
    return _plan_numbers([(r["id"], r["number"], r["created_at"]) for r in rows])


def backfill(conn, *, dry_run=False, draft=None):
    """Returns [(id, number|None, name|None, title)] for every row that changes. Names are drafted
    first (slow); numbers are planned from a fresh read inside the write transaction."""
    from capsule import db
    rows = conn.execute("SELECT id, title, status, short_name FROM tasks").fetchall()
    open_rows = [(r["id"], r["title"]) for r in rows
                 if r["short_name"] is None and r["status"] not in CLOSED]
    names = _plan_names(open_rows, draft)

    def plan():
        numbers = _numbers(conn)
        titles = dict(conn.execute("SELECT id, title FROM tasks").fetchall())
        order = sorted((set(numbers) | set(names)) & set(titles),
                       key=lambda t: (numbers.get(t) or 10**9, t))
        return [(tid, numbers.get(tid), names.get(tid), titles[tid]) for tid in order]

    if dry_run:
        return plan()
    with db.txn(conn):
        changes = plan()
        conn.executemany("UPDATE tasks SET number=? WHERE id=? AND number IS NULL",
                         [(n, tid) for tid, n, _, _ in changes if n is not None])
        conn.executemany("UPDATE tasks SET short_name=?, name_source='draft'"
                         " WHERE id=? AND short_name IS NULL",
                         [(nm, tid) for tid, _, nm, _ in changes if nm is not None])
    return changes


def handle(args, parser):
    from capsule import db, paths
    path = paths.board_db(args.board, paths.home(args.capsule_home))
    if not path.exists():
        print(f"capsule names: no board {args.board!r} at {path}", file=sys.stderr)
        return 1
    conn = db.connect_ro(path) if args.dry_run else db.connect(path)
    try:
        changes = backfill(conn, dry_run=args.dry_run)
    except RuntimeError as exc:
        print(f"capsule names: {exc}; nothing written", file=sys.stderr)
        return 1
    finally:
        conn.close()
    if TOKENS:
        print(f"capsule names: model tokens {TOKENS}", file=sys.stderr)
    for _, number, name, title in changes:
        print(f"{number if number is not None else '-'} {name or '-'} <- {title}")
    return 0
