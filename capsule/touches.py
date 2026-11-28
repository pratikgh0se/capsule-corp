"""Q2b, owner touches per landed card (evals spec Q2b, E1; rulings a-c of
the CoS)."""
import argparse
import datetime
import json
import os
import re
import sys
from pathlib import Path

from capsule import blocks, db, host, human, human_minutes, paths, report, spool

sys.path.insert(0, str(paths.REPO / "maintenance"))
import decision_log  # noqa: E402

EXCLUDED_TAGS = report.EXCLUDED_TAGS
T_ID = re.compile(r"t_[0-9a-f]{8}")
LAST_PAREN = re.compile(r"\(([^()]*)\)\s*$")
ACTION = r"(?:asked|stamped|chose|approved|ruled|ran)"


def _patterns(name=None):
    """(OPENS, ACTS, ACTOR) built from the configured owner name (`owner.name`)."""
    n = re.escape(host.owner_name() if name is None else name)
    return (re.compile(rf"^{n}\s*[:(]"), re.compile(rf"\b{n}\s+{ACTION}\b"),
            re.compile(rf"^{n}(?:\s+{ACTION}\b.*)?$", re.IGNORECASE))


ACTOR_SPLIT = re.compile(r"\s*(?:\+|/|,|&|\band\b)\s*")
C01_BAR = 1.92   # decision row 616: the C01 baseline, 46 acts over 24 landed cards
LEGACY_METRICS = {"pratik_acts": "owner_acts"}    # eval_check events written before the rename
FRESH = "COALESCE(tokens_in,0)+COALESCE(tokens_out,0)+COALESCE(tokens_cache_write,0)"
CAVEATS = [
    "human/ files count as the owner acts unless author is cos; a legacy file with no author field "
    "counts as the owner's, so chief-of-staff files written before the field existed over-count; "
    "a CoS file written without --author cos still counts. migration_resets and "
    "interventions_total exclude cos files, unlike totals.* in report.interventions",
    "decision rows count when the owner is an actor (decision row 612); a row that only mentions "
    "him does not. A row shape the actor rules do not recognise reads as no act: check a live "
    "run against the table by hand",
]


class NotMigrated(Exception):
    """The board is still in the old system's schema: no honest Q2b or Q2c can be read from it."""


def _day(ts):
    """The local calendar day of `ts`: the decision table dates its rows in the owner's day."""
    return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d")


def _is_actor_row(r, name=None):
    """True when the owner is an actor in decision row `r` (see the module docstring)."""
    opens, acts, actor = _patterns(name)
    topic, text = r.get("topic") or "", r["text"]
    m = LAST_PAREN.search(topic)
    named = m and any(actor.match(t) for t in ACTOR_SPLIT.split(m.group(1)))
    return bool(named or opens.match(topic) or opens.match(text) or acts.search(text))


def _spool_files(home, board, since_ts):
    d = spool.human_dir(home, board)
    out = []
    for src in (d, d / "done"):
        for p in sorted(src.glob("*.json")) if src.is_dir() else []:
            try:
                doc = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(doc, dict) and int(doc.get("at") or 0) >= since_ts:
                out.append((p.name, doc))
    return out


def _ratio(acts, landed, blind):
    """Touches per landed card; "insufficient" with no landed card, "unmeasured" with no data source
    at all (zero rows and zero spool files). Rows that exist but name no act give a real 0.0. A row
    shape the actor rules do not recognise is the live sanity check's job, not this guard's."""
    if not landed:
        return "insufficient"
    return "unmeasured" if blind else acts / landed


def _owner_rows(vault, since_ts):
    """({number: row} the owner acted in, count of all rows) in the window; None without a table."""
    note = decision_log.find_log(vault) if vault and Path(vault).is_dir() else None
    if note is None:
        return None
    since_day = _day(since_ts) if since_ts else ""
    out, seen, name = {}, 0, host.owner_name()    # host.json is read once per run, not per row
    for r in decision_log.parse(note.read_text(encoding="utf-8")):
        if (r["date"] or "") >= since_day:
            seen += 1
            if _is_actor_row(r, name):
                out.setdefault(r["number"], r)
    return out, seen


def _acts(files, rows):
    """({act id: set of card ids}, [row numbers attributed to the owner], migration resets)."""
    files = [(n, d) for n, d in files if not human.is_cos(d.get("author"))]   # legacy = the owner
    plain = [(n, d) for n, d in files if d.get("tag") not in EXCLUDED_TAGS]
    acts = {f"spool:{n}": {d.get("task_id")} for n, d in plain}
    for num, r in sorted(rows.items()):
        cards = set(T_ID.findall(f"{r.get('topic', '')} {r['text']}"))
        again = any(d.get("task_id") in cards and _day(int(d["at"])) == r["date"] for _, d in plain)
        if not again:
            acts[f"row:{num}"] = cards
    return acts, sorted(rows), len(files) - len(plain), len(plain)


def _add(doc, conn, home, board, since_ts, vault, now=None):
    """Put the Q2b numbers and their caveats into the report `doc`; return it."""
    found = _owner_rows(vault, since_ts)
    rows, seen = found if found is not None else (None, 0)
    files = _spool_files(home, board, since_ts)
    acts, row_nums, resets, plain = _acts(files, rows or {})
    landed = conn.execute(
        "SELECT t.id, t.title FROM tasks t WHERE t.status='done' AND EXISTS (SELECT 1 FROM"
        " task_events e WHERE e.task_id=t.id AND e.kind='completed' AND e.created_at>=? AND"
        f" {blocks.not_closed_after('t.id', 'e.id')})", (since_ts,)).fetchall()
    ids = {r["id"] for r in landed}
    n = len(landed)
    blind = not seen and not files  # no data source at all: broken parser, no table, wrong path
    on_landed = sum(1 for cards in acts.values() if cards & ids)
    fresh = [r[0] for r in conn.execute(
        f"SELECT SUM({FRESH}) FROM task_runs WHERE started_at>=? GROUP BY task_id", (since_ts,))]
    doc.update(human_minutes.measure(home, board, [d for _, d in files], since_ts, now))
    doc.update({
        "landed_cards": n, "landed_card_names": sorted(r["title"] for r in landed),
        "owner_act_ids": sorted(acts), "owner_act_rows": row_nums, "owner_acts": len(acts),
        "owner_touches_per_card": _ratio(len(acts), n, blind),
        "owner_touches_per_landed_card_only": _ratio(on_landed, n, blind),
        "chat_touches_per_card": "unmeasured",
        "interventions_total": plain, "migration_resets": resets,
        "fresh_tokens_total": sum(fresh), "fresh_tokens_max_card": max(fresh, default=0),
        "max_rework_rounds": max((c["rework_rounds"] for c in doc["cards"]), default=0),
        "caveats": CAVEATS + ([] if rows is not None else [
            "decision table not found (OBSIDIAN_VAULT_PATH): decision rows are not counted, "
            "so owner_touches_per_card under-counts"]) + (["no decision rows and no spool "
            "files in the window: owner_touches_per_card is unmeasured, not 0.0"]
                                                         if blind and n else []),
    })
    return doc


def evaluate(capsule_home, board, *, since=None, now=None, vault=None):
    """`report.interventions` plus the Q2b numbers and the `bar`: what `capsule report` prints."""
    conn = db.connect_ro(paths.board_db(board, Path(capsule_home)))
    try:
        if db.migrations_pending(conn):
            raise NotMigrated(f"board {board!r} is still in the old system's schema (run the C03"
                              " cutover migration first); a report on it would be zeros, not"
                              " a measurement")
        doc = report.interventions(capsule_home, board, since=since, now=now)
        vault = vault if vault is not None else os.environ.get("OBSIDIAN_VAULT_PATH")
        _add(doc, conn, capsule_home, board, doc["since"], vault, now)
        doc.update(_eval_state(conn, vault))
    finally:
        conn.close()
    # The historical bar (U1) and the C01 bar for Q2b: 1.92 touches per card (decision row 616).
    doc["bar"] = {**report.U1, "owner_touches_per_card": C01_BAR,
                  "owner_touches_per_card_status": "row 616"}
    return doc


def _eval_state(conn, vault=None):
    """E2: the latest `eval_check` per metric, the open eval incidents (row 617) and the weeks
    whose ledger row is still asked for (inc_1054fd7d)."""
    from capsule import eval_check    # late: eval_check imports this module
    checks = {}
    for (raw,) in conn.execute(
            "SELECT payload FROM task_events WHERE kind='eval_check' ORDER BY id"):
        p = json.loads(raw)
        name = LEGACY_METRICS.get(p["metric"], p["metric"])
        checks[name] = {k: p[k] for k in ("a", "b", "agree", "window")}
    open_ = conn.execute(
        "SELECT id, root_key, opened_at FROM incidents WHERE role='eval'"
        " AND status NOT IN ('closed', 'superseded') AND kind IN ('contradiction', 'repeat')"
        " ORDER BY opened_at, id").fetchall()
    return {"eval_checks": checks, "eval_incidents": [dict(r) for r in open_],
            "eval_ledger_due": sorted(eval_check.ledger_due(conn, vault))}


def main(argv=None):
    """`python3 -m capsule.touches BOARD [--capsule-home DIR]`: only the Q2b numbers, as JSON."""
    ap = argparse.ArgumentParser(prog="capsule.touches")
    ap.add_argument("board")
    ap.add_argument("--capsule-home", default=None)
    ap.add_argument("--since", default=None)
    args = ap.parse_args(argv)
    try:
        doc = evaluate(paths.home(args.capsule_home), args.board, since=args.since)
    except NotMigrated as exc:
        print(f"capsule.touches: {exc}", file=sys.stderr)
        return 1
    keep = ("landed_cards", "owner_acts", "owner_touches_per_card", "caveats", "bar")
    print(json.dumps({k: doc[k] for k in keep}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
