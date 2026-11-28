"""The CoS review queue for vault context (row 626, spec 7.1, 7.2, 7.4): `pending` lists flagged
retrievals, `verdict` appends one line to the eval verdicts file and one `task_events` row."""
import contextlib
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from capsule import db, paths

VERDICTS = Path(__file__).resolve().parent.parent / "maintenance/state/vault_eval/verdicts.jsonl"
EXPIRY_S = 48 * 3600
HIDE_ABOVE = 20
Q_BODY = 1200


def _board(args):
    board = args.board or os.environ.get("CAPSULE_BOARD") or "capsule-corp"
    home = paths.home(args.capsule_home)
    return paths.board_db(board, home), paths.board_dir(board, home) / "spool"


def _verdicts_path(args):
    return Path(args.verdicts or os.environ.get("CAPSULE_VAULT_VERDICTS") or VERDICTS)


def _read_verdicts(path):
    done = set()
    with contextlib.suppress(OSError, ValueError):
        for line in path.read_text(encoding="utf-8").splitlines():
            v = json.loads(line) if line.strip() else {}
            done.add((v.get("run_id"), str(v.get("item", ""))))
            done.add((v.get("run_id"), None))   # any verdict settles an item-less record
    return done


def _key(it):
    return f"row {it['number']}" if "number" in it else str(it.get("path", ""))


def _age(file, rec):
    stamp = file.stat().st_mtime
    with contextlib.suppress(KeyError, ValueError, TypeError):
        when = datetime.strptime(rec["retrieved"], "%Y-%m-%d %H:%M")
        stamp = min(stamp, when.replace(tzinfo=timezone.utc).timestamp())
    return time.time() - stamp


def _records(spool):
    for file in sorted(Path(spool).glob("*/vault_context.json")):
        try:
            rec = json.loads(file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(rec, dict) and rec.get("flags") and rec.get("run_id"):
            yield rec, _age(file, rec)


def _open_reviews(spool, done):
    """[(rec, key, expired)] for every flagged item with no verdict."""
    out = []
    for rec, age in _records(spool):
        items = [_key(it) for it in rec.get("kept", []) + rec.get("withheld", [])
                 if it.get("number") or it.get("path")] or [None]
        for key in items:
            if (rec["run_id"], key) not in done:
                out.append((rec, key, age > EXPIRY_S))
    return out


def pending(args):
    _, spool = _board(args)
    open_ = _open_reviews(spool, _read_verdicts(_verdicts_path(args)))
    quiet = len({o[0]["run_id"] for o in open_ if not o[2]}) > HIDE_ABOVE
    for rec, key, expired in open_:
        flags = rec["flags"]
        if expired and not args.all:
            continue
        if quiet and flags == ["sample"] and not args.all:
            continue
        tag = "unreviewed" if expired else ",".join(flags)
        print(f"{rec.get('task_id') or '-'} {rec['run_id']} {tag} {key or '(no items)'}")
    return 0


def _item_ok(rec, item):
    keys = {_key(it) for it in rec.get("kept", []) + rec.get("withheld", [])}
    return item in keys


def verdict(args):
    if len(args.card) < 3 or args.card[2] not in ("kept", "dropped", "missing"):
        print("capsule vault-context verdict: RUN_ID ITEM kept|dropped REASON|missing PATH",
              file=sys.stderr)
        return 2
    run_id, item, kind, extra = args.card[0], args.card[1], args.card[2], " ".join(args.card[3:])
    item = f"row {item}" if item.isdigit() else item
    if not paths.BOARD_NAME.fullmatch(run_id) or ".." in run_id:
        print(f"capsule vault-context verdict: {run_id!r} is not a run id", file=sys.stderr)
        return 2
    if kind != "missing" and item in ("", "-"):
        print(f"capsule vault-context verdict: {kind} needs an item", file=sys.stderr)
        return 2
    db_path, spool = _board(args)
    file = Path(spool) / run_id / "vault_context.json"
    try:
        rec = json.loads(file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        print(f"capsule vault-context verdict: no record for run {run_id}", file=sys.stderr)
        return 1
    if rec.get("run_id") != run_id:
        print("capsule vault-context verdict: record names another run", file=sys.stderr)
        return 1
    if kind != "missing" and not _item_ok(rec, item):
        print(f"capsule vault-context verdict: {item!r} is not in run {run_id}", file=sys.stderr)
        return 1
    if kind != "kept" and not extra:
        print(f"capsule vault-context verdict: {kind} needs a "
              f"{'reason' if kind == 'dropped' else 'path'}", file=sys.stderr)
        return 2
    target = extra if kind == "missing" else item.removeprefix("row ")
    line = {"verdict": kind, "run_id": run_id, "item": item,
            "row" if target.isdigit() else "path": int(target) if target.isdigit() else target}
    if kind == "dropped":
        line["reason"] = extra
    _write(db_path, _verdicts_path(args), rec, line)
    print(f"recorded {kind} for {item} in run {run_id}")
    return 0


def _write(db_path, out, rec, line):
    """The event row, then the JSONL line, in one transaction: a failure leaves neither."""
    with contextlib.closing(db.connect(db_path)) as conn:
        conn.execute("BEGIN IMMEDIATE")   # a lock failure lands before either write
        try:
            task = conn.execute("SELECT title, body FROM tasks WHERE id=?",
                                (rec.get("task_id"),)).fetchone()
            line["q"] = f"{task['title'] or ''}\n{(task['body'] or '')[:Q_BODY]}" if task else ""
            pk = conn.execute("SELECT id FROM task_runs WHERE run_id=?",
                              (line["run_id"],)).fetchone()
            db.event(conn, rec.get("task_id") or "", "vault_context_verdict",
                     {k: v for k, v in line.items() if k not in ("q", "run_id")},
                     run_pk=pk and pk["id"], run_id=line["run_id"])
            out.parent.mkdir(parents=True, exist_ok=True)
            with open(out, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(line, ensure_ascii=False) + "\n")
                fh.flush()
                os.fsync(fh.fileno())
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise


def main(args):
    return {"pending": pending, "verdict": verdict}[args.verb](args)
