"""The weekly process advisor (row 394; Zenkai Z4 and Z5; readiness TEAM-22).

The factory's weekly introspection: a high model reads one packet of run history and
returns an `advisor_report.v1` (contract `contracts/advisor.md`). This module is the
deterministic part around that model call, and it changes nothing on its own:

- `build_packet` folds the interventions report, the incident rows and the review
  verdicts into the packet: rework, re-parks, wasted runs by error class, review
  findings by class, incidents by kind, overrides, and the per-contract contradiction
  count (Z5's ranking).
- `check_report` holds the model's report to the schema and to the rules the schema
  cannot say: the ranking covers every contract, every override gets a review (Z4),
  and an added rule names what it removes (row 393).
- `proposal_cards` turns each proposal into a card draft. Drafts are written to a file;
  filing them is a person's step, so no card exists until the owner approves (row 393).

Everything here reads a board read-only. Calling the model is `run`, off unless
`PROCESS_ADVISOR_CMD` names a command that reads the packet on stdin and prints the report.
Weekly scheduling is a host step (RUNBOOK "Process advisor").
"""
import argparse
import json
import os
import re
import shlex
import sqlite3
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from capsule import db, jsonschema, paths, report as report_mod  # noqa: E402

FILLER = {"none", "nothing", "na", "nil", "tbd", "todo", "no", "all", "at", "any", "n", "a"}
MIN_REMOVAL_WORDS = 3   # a removal target is a phrase, not a placeholder
TOP = 10   # cards listed per packet section: the model reads a digest, not the board
TOTAL_KEYS = ("cards", "runs", "interventions_per_card", "wasted_run_rate", "merged",
              "failure_events", "contract_amendments", "spec_stale_holds")


def _contract_roles(directory=None):
    """Every contract the factory ships, minus `_style.md`: the set Z5's ranking must cover."""
    folder = Path(directory or paths.REPO / "contracts")
    return sorted(p.stem for p in folder.glob("*.md") if not p.name.startswith("_"))


def _findings_by_class(docs):
    """Count review findings per `class` across review verdict documents (any list of
    objects that carry a `class` key, so a schema that adds a list is counted too)."""
    items = [i for d in docs for v in d.values() if isinstance(v, list) for i in v]
    classes = [i["class"] for i in items if isinstance(i, dict) and i.get("class")]
    return dict(sorted(Counter(classes).items(), key=lambda kv: (-kv[1], kv[0])))


def _load_verdict_docs(directory):
    """Every `*.json` file under `directory` that parses to an object; others are skipped."""
    docs = []
    for path in sorted(Path(directory).rglob("*.json")):
        try:
            doc = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        docs += [doc] if isinstance(doc, dict) else []
    return docs


def _incident_rows(conn):
    """`(kind, role)` per incident; a board with no incidents table has none."""
    try:
        return [dict(r) for r in conn.execute("SELECT kind, role FROM incidents")]
    except sqlite3.OperationalError:   # a board migrated before row 456
        return []


def _top(cards, key, size=TOP):
    ranked = sorted(cards, key=lambda c: (-_size(c[key]), c["task_id"]))
    return [{"task_id": c["task_id"], "title": c["title"], "count": _size(c[key])}
            for c in ranked if _size(c[key])][:size]


def _size(value):
    return len(value) if isinstance(value, list) else value


def _build_packet(report, incidents, verdict_docs, roles, now):
    """The weekly packet: a plain dict, bounded, with no card body or log text in it."""
    cards, totals = report["cards"], report["totals"]
    by_kind = Counter(r["kind"] for r in incidents)
    contradictions = {role: 0 for role in roles}
    for row in incidents:
        if row["kind"] == "contradiction":
            role = row["role"] if row["role"] in contradictions else "unknown"
            contradictions[role] = contradictions.get(role, 0) + 1
    overrides = [{"id": f"{c['task_id']}:{o['check']}:{o['at']}", "state": o["state"],
                  "check": o["check"], "reason": o.get("reason"), "fix_card": o.get("fix_card")}
                 for c in cards for o in c["overrides"]]
    ranking = sorted(contradictions.items(), key=lambda kv: (-kv[1], kv[0]))
    return {
        "schema": "advisor_packet.v1", "generated_at": now, "board": report["board"],
        "since": report["since"], "totals": {k: totals[k] for k in TOTAL_KEYS},
        "rework": _top(cards, "rework_rounds"), "reparks": _top(cards, "failure_events"),
        "wasted_by_error_class": totals["wasted_by_error_class"],
        "review_findings_by_class": _findings_by_class(verdict_docs),
        "incidents_by_kind": dict(sorted(by_kind.items())),
        "repeat_incidents": report["repeat_incidents"], "overrides": overrides,
        "contradictions_total": sum(contradictions.values()),
        "contradictions_by_contract": dict(ranking), "roles": sorted(roles),
    }


def _names_removal(text):
    """True when `text` holds at least MIN_REMOVAL_WORDS real words (not placeholders)."""
    words = [w for w in re.findall(r"[a-z0-9_]+", text.lower()) if w not in FILLER]
    return len(words) >= MIN_REMOVAL_WORDS


def _check_ranking(doc, packet):
    ranking = doc["contradiction_ranking"]
    names, counts = [r["contract"] for r in ranking], [r["count"] for r in ranking]
    errors = ["contradiction_ranking lists a contract twice"] * (len(set(names)) != len(names))
    errors += ["contradiction_ranking is not in descending order of count"] * (
        counts != sorted(counts, reverse=True))
    errors += [f"contradiction_ranking misses contract {r!r} (Z5)" for r in packet["roles"]
               if r not in names]
    want = packet["contradictions_by_contract"]
    errors += [f"contradiction_ranking count for {r['contract']!r} is {r['count']}, "
               f"packet says {want[r['contract']]}"
               for r in ranking if r["contract"] in want and r["count"] != want[r["contract"]]]
    errors += [f"contradiction_ranking lists {r['contract']!r}, not a contract in the packet"
               for r in ranking if r["contract"] not in want]
    return errors


def check_report(doc, packet, schemas=None):
    """One message per problem; an empty list means the report is acceptable."""
    schema = (schemas or jsonschema.load_all(paths.SCHEMAS))["advisor_report.v1"]
    errors = jsonschema.validate(doc, schema)
    if errors:
        return errors
    errors = _check_ranking(doc, packet)
    reviewed = {o["override"] for o in doc["override_review"]}
    errors += [f"override_review misses override {o['id']!r} (Z4)"
               for o in packet["overrides"] if o["id"] not in reviewed]
    errors += [f"proposals[{n}] adds a rule and names nothing it removes (row 393)"
               for n, p in enumerate(doc["proposals"])
               if p["kind"] == "add_rule" and not _names_removal(p["removes"])]
    return errors


def _proposal_cards(doc):
    """One card draft per proposal. Drafts only: nothing here files a card."""
    return [{"title": p["card_title"], "status": "blocked", "kind": p["kind"],
             "body": (f"Process advisor proposal ({p['kind']}).\n\nTarget: {p['target']}\n\n"
                      f"Change: {p['change']}\n\nRemoves: {p['removes']}\n\n"
                      f"Evidence: {p['evidence']}\n\n"
                      "Gate: the owner approves before this card runs (row 393).")}
            for p in doc["proposals"]]


def _render_note(doc, packet):
    """The introspection note: summary, the Z5 ranking table, the Z4 override review, proposals."""
    lines = [f"# Process advisor report, board {packet['board']}", "", doc["summary"], "",
             "## Contradiction ranking", "", "| contract | contradictions |", "|---|---|"]
    lines += [f"| {r['contract']} | {r['count']} |" for r in doc["contradiction_ranking"]]
    reviews = [f"- {o['override']}: {o['verdict']} - {o['note']}" for o in doc["override_review"]]
    props = [f"- [{p['kind']}] {p['card_title']} (removes: {p['removes']})"
             for p in doc["proposals"]]
    lines += ["", "## Override review", ""] + (reviews or ["- none"])
    lines += ["", "## Proposals", ""] + (props or ["- none"])
    return "\n".join(lines) + "\n"


def packet_for_board(home, board, *, since=None, verdicts=None, now=None, roles=None):
    now = int(now if now is not None else time.time())
    doc = report_mod.interventions(home, board, since=since, now=now)
    conn = db.connect_ro(paths.board_db(board, Path(home)))
    try:
        rows = _incident_rows(conn)
    finally:
        conn.close()
    docs = _load_verdict_docs(verdicts) if verdicts else []
    return _build_packet(doc, rows, docs, roles or _contract_roles(), now)


def _ask_model(packet, env):
    """Run `PROCESS_ADVISOR_CMD` with the packet on stdin; its stdout parsed as the report."""
    cmd = env.get("PROCESS_ADVISOR_CMD", "").strip()
    if not cmd:
        raise SystemExit("process_advisor: PROCESS_ADVISOR_CMD is unset; packet written, "
                         "no model call made")
    done = subprocess.run(shlex.split(cmd), input=json.dumps(packet), capture_output=True,
                          text=True, timeout=1800)
    if done.returncode:
        raise SystemExit(f"process_advisor: the advisor command exited {done.returncode}: "
                         f"{done.stderr[-300:]}")
    try:
        return json.loads(done.stdout)
    except ValueError as exc:
        raise SystemExit(f"process_advisor: the advisor command printed no JSON: {exc}")


def run(home, board, outdir, *, since=None, verdicts=None, env=None, now=None, roles=None):
    """Packet, model command, check, outputs (`advisor-note.md`, `advisor-cards.json`). Raises
    SystemExit with a message when the command is unset, fails or its report fails the check;
    the packet is always written first and nothing else is written on a refusal."""
    env = os.environ if env is None else env
    packet = packet_for_board(home, board, since=since, verdicts=verdicts, now=now, roles=roles)
    out = Path(outdir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "advisor-packet.json").write_text(json.dumps(packet, indent=2) + "\n")
    doc = _ask_model(packet, env)
    errors = check_report(doc, packet)
    if errors:
        raise SystemExit("process_advisor: report refused:\n" + "\n".join(errors))
    note, cards = out / "advisor-note.md", out / "advisor-cards.json"
    note.write_text(_render_note(doc, packet))
    cards.write_text(json.dumps(_proposal_cards(doc), indent=2) + "\n")
    return note, cards


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("packet", "run"):
        p = sub.add_parser(name)
        p.add_argument("--home", required=True)
        p.add_argument("--board", required=True)
        p.add_argument("--since")
        p.add_argument("--verdicts")
        p.add_argument("--roles", help="comma list: after a model swap, only these contracts")
        if name == "run":
            p.add_argument("--out", required=True)
    c = sub.add_parser("check")
    c.add_argument("report")
    c.add_argument("packet")
    args = ap.parse_args(argv)
    if args.cmd == "check":
        errors = check_report(json.loads(Path(args.report).read_text()),
                              json.loads(Path(args.packet).read_text()))
        print("\n".join(errors) or "ok")
        return 1 if errors else 0
    kw = {"since": args.since, "verdicts": args.verdicts,
          "roles": args.roles.split(",") if args.roles else None}
    if args.cmd == "packet":
        print(json.dumps(packet_for_board(args.home, args.board, **kw), indent=2))
    else:
        print("\n".join(str(p) for p in run(args.home, args.board, args.out, **kw)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
