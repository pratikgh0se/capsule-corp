"""Evals E2: every weekly eval metric gets a second, independent
oracle."""
import argparse
import datetime
import json
import os
import sys
from pathlib import Path

from capsule import config, db, eval_tokens, human, incidents, outbox, paths, report, touches

TASK, DAY = "eval", 86400
METRICS = ("owner_acts", "interventions")
LEGACY_METRICS = touches.LEGACY_METRICS   # stored events and tolerance files before the rename
TICK_METRICS = METRICS + ("fresh_tokens",)   # the tick also checks tokens (E2b)


def load_tolerances(path=None):
    doc = json.loads(Path(path or Path(__file__).with_name("eval_check.json")).read_text("utf-8"))
    for old, new in LEGACY_METRICS.items():     # an old tolerance file keeps working
        if old in doc and new not in doc:
            doc[new] = doc.pop(old)
    return doc


def _ts(day):
    return int(datetime.datetime.strptime(day, "%Y-%m-%d").timestamp())


def _read_ledger(path):
    """({(since_ts, until_ts): {acts, landed, by}}, [malformed row notes]): strict, no guessing."""
    out, bad = {}, []
    lines = Path(path).read_text("utf-8").splitlines() if Path(path).is_file() else []
    for line in (ln.strip() for ln in lines if ln.strip().startswith("|")):
        cells = [c.strip() for c in line.strip("|").split("|")]
        if cells[0] == "week" or set("".join(cells)) <= set("-: "):
            continue
        try:
            if len(cells) != 7 or human.canonical_author(cells[5]) not in ("cos", "opus", "owner"):
                raise ValueError(line)
            out[(_ts(cells[1]), _ts(cells[2]))] = {"acts": int(cells[3]), "landed": int(cells[4]),
                                                  "by": cells[5]}
        except ValueError:
            bad.append(f"ledger row malformed: {line[:80]}")
    return out, bad


def _closed_weeks(now, since_floor, delay_days):
    """Local ISO weeks (since, until) closed `delay_days` ago, starting at or after the floor."""
    monday = datetime.date.fromtimestamp(now)
    monday -= datetime.timedelta(days=monday.weekday())
    out = []
    for k in range(52):
        until, since = (int(datetime.datetime.combine(
            monday - datetime.timedelta(weeks=j), datetime.time()).timestamp()) for j in (k, k + 1))
        if until + delay_days * DAY <= now and since >= since_floor:
            out.append((since, until))
    return out[::-1]


def _a(home, board, vault, since, until):
    lo, hi = (touches.evaluate(home, board, since=t, vault=vault) for t in (since, until))
    return {"owner_acts": len(set(lo["owner_act_ids"]) - set(hi["owner_act_ids"])),
            "interventions": lo["interventions_total"] - hi["interventions_total"]}


def _b_interventions(conn, since, until):
    skip = ",".join("?" for _ in touches.EXCLUDED_TAGS)
    # The author filter pairs with touches.py:94 (oracle A drops author == "cos"); no author counts.
    return conn.execute(
        "SELECT COUNT(*) FROM task_events WHERE kind='intervention'"
        " AND json_extract(payload,'$.at') BETWEEN ? AND ?"
        " AND LOWER(TRIM(COALESCE(json_extract(payload,'$.author'),''))) != 'cos'"
        f" AND COALESCE(json_extract(payload,'$.tag'),'') NOT IN ({skip})",
        (since, until - 1, *touches.EXCLUDED_TAGS)).fetchone()[0]


def _done(conn, metric, since, until):
    return conn.execute(
        "SELECT 1 FROM task_events WHERE kind='eval_check' AND json_extract(payload,'$.metric') IN"
        " (?, ?) AND json_extract(payload,'$.window[0]')=?"
        " AND json_extract(payload,'$.window[1]')=?",
        (metric, {v: k for k, v in LEGACY_METRICS.items()}.get(metric, metric), since,
         until)).fetchone() is not None


def _ledger_path(vault, ledger):
    return ledger or Path(vault or "/nonexistent") / "Knowledge" / report.ledger_name()


def _foreign_ledger(vault):
    """The other name's ledger file while owner.name is the default (preflight), else None."""
    from capsule import preflight      # late: preflight imports much of the package
    return preflight.foreign_ledger(vault)


def _owner_unset(conn, now, since, until, found):
    """One `eval_owner_unset` event and one outbox line a day while the check waits."""
    last = conn.execute("SELECT MAX(created_at) FROM task_events WHERE kind='eval_owner_unset'"
                        ).fetchone()[0]
    if last is not None and now < last + DAY:
        return
    with db.txn(conn):
        db.event(conn, TASK, "eval_owner_unset", {"window": [since, until], "found": found},
                 now=now)
        outbox.notify(conn, "needs_input",
                      f"owner_acts check waits: owner.name is the default 'owner' but the vault "
                      f"has '{found}'; set owner.name in host.json", now=now)


def check(conn, home, board, *, since, until, now, vault=None, ledger=None, tolerances=None,
          metrics=METRICS, write=True, projects=None):
    """The `eval_check` rows written for this window (none for a metric already checked or whose
    delay has not passed). `write=False` computes the rows and writes nothing."""
    tol = tolerances or load_tolerances()
    vault = vault if vault is not None else os.environ.get("OBSIDIAN_VAULT_PATH")
    todo = [m for m in metrics if now >= until + tol[m]["delay_days"] * DAY
            and not _done(conn, m, since, until)]
    found = _foreign_ledger(vault) if "owner_acts" in todo else None
    if found:       # owner.name unset: the check would read the wrong ledger (inc_1aac8250)
        todo.remove("owner_acts")
        if write:
            _owner_unset(conn, now, since, until, found)
    if not todo:
        return []
    a = _a(home, board, vault, since, until) if set(todo) - {"fresh_tokens"} else {}
    rows, bad = _read_ledger(_ledger_path(vault, ledger))
    out = []
    for metric in todo:
        note, b, detail, ok, cmp = "", None, None, True, a.get(metric)
        if metric == "fresh_tokens":
            tk = eval_tokens.compare(
                conn, paths.board_dir(board, home) / "spool",
                projects or paths.claude_projects_dir(), since, until, tol[metric]["unjoined"])
            a[metric], cmp, b, detail = tk["a"], tk["a_joined"], tk["b"], tk["detail"]
            ok = tk["agree_join"]    # unjoined runs answer to their own limit, not to rel
            if not ok:
                note = f"unjoined runs above {tol[metric]['unjoined']:.0%} of A"
        elif metric == "interventions":
            b = _b_interventions(conn, since, until)
        elif (since, until) in rows:
            b = rows[(since, until)]["acts"]
        else:
            note = "oracle B missing" + (f"; {bad[0]}" if bad else "")
        t = {"abs": tol[metric]["abs"], "rel": tol[metric]["rel"]}
        agree = ok and b is not None and abs(cmp - b) <= max(t["abs"], t["rel"] * b)
        out.append({"metric": metric, "a": a[metric], "b": b, "tolerance": t, "agree": agree,
                    "window": [since, until], "note": note,
                    **({"detail": detail} if detail else {})})
        if write:
            _write(conn, board, out[-1], now)
    return out


def _write(conn, board, r, now):
    with db.txn(conn):
        db.event(conn, TASK, "eval_check", {
            "metric": r["metric"], "a": r["a"], "b": r["b"], "tolerance": r["tolerance"],
            "agree": r["agree"], "window": r["window"], "note": r["note"],
            **({"detail": r["detail"]} if "detail" in r else {})}, now=now)
        if not r["agree"]:
            since, until = r["window"]
            incidents.open_incident(
                conn, board=board, kind="contradiction", role="eval", now=now,
                key=f"eval:{r['metric']}",
                evidence=[f"a={r['a']}", f"b={r['b']}", f"window={since}..{until}", r["note"]])


def ledger_due(conn, vault=None, ledger=None):
    """{(since, until): requests filed} for the weeks asked for whose ledger row is still missing
    and that have no owner_acts check (inc_1054fd7d: the report shows them, the tick repeats)."""
    rows, _ = _read_ledger(_ledger_path(vault, ledger))
    asked = {}
    for (raw,) in conn.execute("SELECT payload FROM task_events WHERE kind='eval_ledger_due'"):
        window = tuple(json.loads(raw)["window"])
        asked[window] = asked.get(window, 0) + 1
    return {w: n for w, n in asked.items() if w not in rows and not _done(conn, "owner_acts", *w)}


def _ask_ledger(conn, now, since_floor, tol, ledger):
    """An `eval_ledger_due` event and an outbox line per closed week with no owner_acts check and
    no ledger row: nudge 1 past `ledger_due_days`, nudge 2 the day before the check; no more."""
    vault = os.environ.get("OBSIDIAN_VAULT_PATH")
    rows, bad = _read_ledger(_ledger_path(vault, ledger))
    asked, days = ledger_due(conn, vault, ledger), tol["owner_acts"]
    last = {}
    for raw, at in conn.execute("SELECT payload, created_at FROM task_events"
                                " WHERE kind='eval_ledger_due'"):
        last[tuple(json.loads(raw)["window"])] = at
    for since, until in _closed_weeks(now, since_floor, days["ledger_due_days"]):
        n = asked.get((since, until), 0)
        due = days["delay_days"] - 1 if n else days["ledger_due_days"]
        if (since, until) in rows or _done(conn, "owner_acts", since, until) or n > 1 or (
                now < until + due * DAY) or now < last.get((since, until), 0) + DAY:
            continue
        dates = [datetime.date.fromtimestamp(t).isoformat() for t in (since, until)]
        note = bad[0] if bad else ""
        with db.txn(conn):
            db.event(conn, TASK, "eval_ledger_due",
                     {"window": [since, until], "note": note, "nudge": n + 1}, now=now)
            outbox.notify(
                conn, "needs_input",
                f"Ledger row due: write the week {dates[0]} to {dates[1]} in {report.ledger_name()}"
                + (" - the check runs tomorrow" if n else ""),
                fields=[("note", note)] if note else (), now=now)


def tick_step(conn, cfg, home, now, *, since_floor, write=True, ledger=None):
    """Check every closed week at or after `since_floor`; returns the checks made."""
    tol, made = load_tolerances(), []
    own = cfg["board"] == config.autonomy(cfg)["touch_board"]  # one board-less ledger
    if write and own:
        _ask_ledger(conn, now, since_floor, tol, ledger)
    for metric in TICK_METRICS:
        if metric == "owner_acts" and not own:
            continue
        for since, until in _closed_weeks(now, since_floor, tol[metric]["delay_days"]):
            made += check(conn, home, cfg["board"], since=since, until=until, now=now,
                          tolerances=tol, metrics=(metric,), write=write, ledger=ledger)
    return made


def main(argv=None):
    """Rehearse one window (`--since`/`--until`) or every closed week (neither); `--dry-run`
    writes no event and opens no incident."""
    ap = argparse.ArgumentParser(prog="capsule.eval_check")
    ap.add_argument("board")
    ap.add_argument("--since")
    ap.add_argument("--until")
    ap.add_argument("--capsule-home", default=None)
    ap.add_argument("--ledger", default=None)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    home, now = paths.home(args.capsule_home), int(datetime.datetime.now().timestamp())
    open_db = db.connect_ro if args.dry_run else db.connect
    conn = open_db(paths.board_db(args.board, home))
    try:
        if args.since and args.until:
            now_tol = {m: {**t, "delay_days": 0} for m, t in load_tolerances().items()}
            rows = check(conn, home, args.board, since=_ts(args.since), until=_ts(args.until),
                         now=now, ledger=args.ledger, write=not args.dry_run, tolerances=now_tol)
        else:
            from capsule import outcomes    # late: outcomes -> report -> touches -> here
            rows = tick_step(conn, {"board": args.board}, home, now, since_floor=outcomes.SINCE,
                             write=not args.dry_run, ledger=args.ledger)
    finally:
        conn.close()
    print(json.dumps(rows))
    return 0


if __name__ == "__main__":
    sys.exit(main())
