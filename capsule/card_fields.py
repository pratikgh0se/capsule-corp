"""Card fields (R04): check_cmd / check_expect / size_tag / risk_flags / workload / runner on
`tasks`, dependencies in `task_links` (parent = dependency). NULL everywhere is today's behaviour.
The operator verbs `capsule human <board> <task> check|size|depends|workload|runner` write them;
a worker report never does (report.v1 has no such key and its spool path never reads one)."""
import json
import re
import sys
import time

from capsule import db, paths
from capsule.db import txn

WORKLOADS = ("upkeep", "product", "research")
RISK_FLAGS = ("judges", "data", "public", "big")
SIZE_RE = re.compile(r"^[A-Za-z0-9_.-]{1,20}$")
RUNNERS = ("strong", "light", "mac")    # the runner tiers a card may name (D305)
FIELDS = ("check_cmd", "check_expect", "size_tag", "risk_flags", "workload", "runner")
COLUMN = {"runner": "tier"}              # the one field whose `tasks` column has another name


class FieldsRefused(Exception):
    """A typed refusal; the CLI prints `str(exc)` and exits 1."""


VALID = {"check_cmd": bool, "check_expect": bool, "workload": WORKLOADS.__contains__,
         "runner": RUNNERS.__contains__,
         "size_tag": lambda v: isinstance(v, str) and bool(SIZE_RE.match(v)),
         "risk_flags": lambda v: bool(v) and set(v) <= set(RISK_FLAGS)}


def clean(fields):
    """Validated values for the keys given (risk_flags as a sorted list); raises FieldsRefused."""
    out = {}
    for key, val in fields.items():
        if key not in FIELDS:
            raise FieldsRefused(f"unknown card field {key!r}")
        if val is None:
            continue
        if key == "risk_flags":
            val = sorted({f.strip() for f in val.split(",")} if isinstance(val, str) else set(val))
        elif key in ("check_cmd", "check_expect"):
            val = val.strip() if isinstance(val, str) else ""
        if not VALID[key](val):
            raise FieldsRefused(f"{key} {val!r} is not valid (workload {'|'.join(WORKLOADS)};"
                                f" risk_flags {'|'.join(RISK_FLAGS)}; size_tag {SIZE_RE.pattern};"
                                f" runner {'|'.join(RUNNERS)})")
        out[key] = val
    return out


def _open(capsule_home, board):
    db_path = paths.board_db(board, capsule_home)
    if not db_path.is_file():
        raise FieldsRefused(f"no such board {board!r} under {capsule_home}")
    return db.connect(db_path)


def _set_fields(capsule_home, board, task_id, fields, *, who="operator", now=None):
    """Set `fields` on a card; returns the card's values read back from the row."""
    values = clean(fields)
    if not values:
        raise FieldsRefused("nothing to set")
    conn = _open(capsule_home, board)
    try:
        row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row is None:
            raise FieldsRefused(f"no such task {task_id!r} on board {board!r}")
        sets = ", ".join(f"{COLUMN.get(k, k)}=?" for k in values)
        stored = [json.dumps(v) if isinstance(v, list) else v for v in values.values()]
        # The runner is guarded in the write itself: a tick claiming between a read and this
        # UPDATE cannot slip a runner change onto a running card (rowcount 0 refuses).
        guard = " AND status NOT IN ('running', 'review')" if "runner" in values else ""
        with txn(conn):
            done = conn.execute(f"UPDATE tasks SET {sets} WHERE id=?{guard}",  # noqa: S608
                                (*stored, task_id))   # column names are the fixed FIELDS
            if done.rowcount == 0:
                raise FieldsRefused(
                    f"{task_id} is {row['status']}: its runner cannot change until it has left"
                    " the run and review stages")
            db.event(conn, task_id, "card_fields_set",
                     {"by": who, "old": {k: _value(row, k) for k in values}, "new": values},
                     now=int(now if now is not None else time.time()))
        return read_fields(conn, task_id)
    finally:
        conn.close()


def _value(row, key):
    """A card's stored value for a field (a NULL runner tier reads as `strong`)."""
    value = row[COLUMN.get(key, key)]
    return (value or "strong") if key == "runner" else value


def read_fields(conn, task_id):
    row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    out = {k: _value(row, k) for k in FIELDS}
    out["passes"] = row["passes"]
    out["depends_on"] = [r[0] for r in conn.execute(
        "SELECT parent_id FROM task_links WHERE child_id=? ORDER BY parent_id", (task_id,))]
    return out


def _depends(capsule_home, board, task_id, parent_id, *, who="operator", now=None):
    """`parent_id` becomes a dependency of `task_id` (a `task_links` row); returns read_fields."""
    conn = _open(capsule_home, board)
    try:
        for tid in (task_id, parent_id):
            if conn.execute("SELECT 1 FROM tasks WHERE id=?", (tid,)).fetchone() is None:
                raise FieldsRefused(f"no such task {tid!r} on board {board!r}")
        if conn.execute("SELECT 1 FROM task_links WHERE parent_id=? AND child_id=?",
                        (parent_id, task_id)).fetchone() is not None:
            return read_fields(conn, task_id)               # already linked: no write, no event
        if conn.execute(                                     # parent is task_id or below it
                "WITH RECURSIVE anc(id) AS (SELECT ? UNION SELECT l.parent_id FROM task_links l"
                " JOIN anc ON l.child_id = anc.id) SELECT 1 FROM anc WHERE id = ?",
                (parent_id, task_id)).fetchone() is not None:
            raise FieldsRefused(f"{parent_id} -> {task_id} would make a dependency cycle")
        with txn(conn):
            conn.execute("INSERT INTO task_links (parent_id, child_id) VALUES (?,?)",
                         (parent_id, task_id))
            db.event(conn, task_id, "card_fields_set",
                     {"by": who, "old": {"depends_on": None}, "new": {"depends_on": parent_id}},
                     now=int(now if now is not None else time.time()))
        return read_fields(conn, task_id)
    finally:
        conn.close()


def main(args, capsule_home):
    """`capsule human` verbs check '<cmd>' [--expect X] | size <tag> [--risk-flags a,b] |
    workload <w> | runner <tier> | depends <id>: prints the card's fields read back, 1 on a
    refusal. `runner` is refused while the card is running or in review: a contract job pins its
    host (docker or Mac) when it is submitted (contract B3.6)."""
    fields = {"check": {"check_cmd": args.text, "check_expect": args.expect},
              "size": {"size_tag": args.text, "risk_flags": args.risk_flags},
              "workload": {"workload": args.text}, "runner": {"runner": args.text}}
    try:
        if args.kind == "depends":
            result = _depends(capsule_home, args.board, args.task_id, args.text, who=args.author)
        else:
            result = _set_fields(capsule_home, args.board, args.task_id, fields[args.kind],
                                 who=args.author)
    except FieldsRefused as exc:
        print(f"capsule human {args.kind}: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result))
    return 0
