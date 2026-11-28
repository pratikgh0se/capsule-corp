"""`python3 -m capsule incidents list|show|open|row|fixcard|attach|retest|landed|duplicate|close`
`|close-history|app-event` (row 456, Korin; app-event: D79)."""
import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

from capsule import app_events, config, db, incident_attach, incident_history
from capsule import incident_blind_run, incident_duplicate, incident_inputs, incident_landed
from capsule import incident_recurrence, incident_repo, incidents, paths

ONE_SHOT_BUSY_MS = 10_000      # followups.md line 158: an operator's one-shot write fails fast


def _cfg(home, board=None):
    try:
        return config.load(paths.resolve_config(home, board))
    except (OSError, ValueError):
        return {}


def _close(conn, args, cfg, home):
    ks = incidents.settings(cfg)
    try:
        incidents.close(conn, args.incident_id, by="cli", home=home, quiet=args.quiet,
                        **incidents.gate_args(ks))
    except incidents.CloseRefused as exc:
        print(f"capsule incidents close: refused, {args.incident_id} is missing:", file=sys.stderr)
        for line in exc.missing:
            print(f"- {line}", file=sys.stderr)
        return 1
    print(f"{args.incident_id}: closed")
    return 0


def _retest_refusal(args, inc, tests):
    """Why `retest` must refuse this incident or these values, or None."""
    if not incident_recurrence.TEST_ID_RE.fullmatch(args.new_test_id):
        return (f"recurrence_test {args.new_test_id!r} is not a dotted unittest id or a"
                f" tests/test_<name>.sh[::<label>] id or a {incident_blind_run.BLIND_SHAPE} id"
                " (the group may hold hyphens)")
    if incident_blind_run.BLIND_RE.match(args.new_test_id) and not tests:
        return incident_blind_run.UNSET
    if not args.reason.strip():
        return "--reason is blank: say why the test moved"
    if args.enforcer is not None and not args.enforcer.strip():
        return "--enforcer is blank: leave it out to keep the old one"
    if args.new_test_id == inc["recurrence_test"]:
        return f"incident {inc['id']} already names {args.new_test_id}"
    if inc["status"] not in ("open", "postmortem", "fix_pending"):
        return (f"incident {inc['id']} is {inc['status']}: only an open, postmortem or"
                " fix_pending incident can retest")
    if inc["closure"] == "watch":
        return (f"incident {inc['id']} is a watch closure: its close gate skips the novelty"
                " check, so a retest means nothing")
    return None


def _retest(conn, args, tests):
    """`retest <id> <new_test_id> --reason`: swap `recurrence_test` (and `--enforcer`) on an open,
    postmortem or fix_pending incident, add a line to its evidence and, when it has a card on this
    board (the fix card, else its own), an `incident_retest` event. Runs no gate, closes nothing."""
    with db.txn(conn):      # BEGIN IMMEDIATE: read, check and write under one write lock
        inc = incidents.get(conn, args.incident_id)
        why = _retest_refusal(args, inc, tests)
        if why:
            raise ValueError(why)
        old = inc["recurrence_test"]
        card = next((c for c in (inc["fix_card"], inc["task_id"]) if c and conn.execute(
            "SELECT 1 FROM tasks WHERE id=?", (c,)).fetchone()), None)
        done = conn.execute(
            "UPDATE incidents SET recurrence_test=?, enforcer=COALESCE(?, enforcer) WHERE id=?"
            " AND status IN ('open','postmortem','fix_pending')",
            (args.new_test_id, args.enforcer, inc["id"]))
        if done.rowcount != 1:
            raise ValueError(f"incident {inc['id']} changed status during the retest: unchanged")
        incidents.attach(conn, inc["id"], [f"retest {old} -> {args.new_test_id} by"
                                           f" {args.author}: {args.reason}"])
        if card:
            db.event(conn, card, "incident_retest", {
                "incident": inc["id"], "old": old, "new": args.new_test_id,
                "reason": args.reason, "by": args.author})
    print(f"{inc['id']}: recurrence_test {old} -> {args.new_test_id}"
          + ("; enforcer replaced" if args.enforcer else "") + " (the gate has not run)")
    return 0


def _landed(conn, args, cfg, board, home):
    ks = incidents.settings(cfg)
    args.sha = args.sha or args.sha_flag
    if not args.sha:
        raise ValueError("landed needs the main commit sha")
    gate = {"blind_tests": ks["blind_tests"], "timeout": ks["test_timeout"]}
    checkout = incident_repo.pick(ks["main_checkout"], args.sha)    # the app repo, else Capsule's
    full = incident_landed._full_sha(checkout, args.sha)
    proven = incident_landed.preflight(conn, args.incident_id, args.test,   # no write lock held
                                       checkout=checkout, full=full, board=board,
                                       home=home, **gate)
    with db.txn(conn):      # a refused landed rolls the retest back (inner ones are savepoints)
        if args.test:
            inc = incidents.get(conn, args.incident_id)
            if inc["recurrence_test"] != args.test:
                _retest(conn, argparse.Namespace(
                    incident_id=args.incident_id, new_test_id=args.test, enforcer=None,
                    author=args.author, reason=f"fix landed outside the factory at {args.sha[:9]}"),
                    ks["blind_tests"])
        card = incident_landed.landed(conn, args.incident_id, args.sha, author=args.author,
                                      board=board, checkout=checkout, home=home,
                                      proven=proven, **gate)
    print(f"{args.incident_id}: fix card {card} landed outside the factory")
    if args.row:
        row = incident_landed.append_decision_row(conn, args.incident_id, ks["vault"],
                                                  ks["decisions_note"])
        print(f"{args.incident_id}: decision row {row}")
    return 0


def _duplicate(conn, args):
    incident_duplicate.mark(conn, args.incident_id, args.of, author=args.author,
                            reason=args.reason)
    print(f"{args.incident_id}: superseded, duplicate of {args.of}")
    return 0


def _close_history(conn, args, cfg):
    ks = incidents.settings(cfg)
    if not ks["vault"]:
        print("capsule incidents close-history: refused: no notes vault configured"
              " (host.json vault.path)", file=sys.stderr)
        return 1
    try:
        results, sha = incident_history.run(
            conn, args.list, apply=args.apply, list_sha=args.list_sha, vault=ks["vault"],
            decisions_note=ks["decisions_note"])
    except (OSError, incident_history.HistoryRefused) as exc:
        print(f"capsule incidents close-history: refused: {exc}", file=sys.stderr)
        return 1
    for outcome, text in results:
        print(f"{outcome}: {text}")
    outcomes = ("closed", "would close", "skipped", "refused")
    counts = {o: sum(r[0] == o for r in results) for o in outcomes}
    mode = "" if args.apply else " (dry run, nothing written)"
    print(f"close-history{mode}: " + ", ".join(f"{n} {o}" for o, n in counts.items() if n),
          file=sys.stderr)
    if not args.apply:
        print(f"list sha256: {sha} (apply with --apply --list-sha {sha})", file=sys.stderr)
    return 1 if counts["refused"] else 0


def _connect(board_db, ro):
    conn = db.connect_ro(board_db) if ro else db.connect(board_db)
    try:       # CAPSULE_INCIDENTS_BUSY_MS (integer ms) overrides; anything else is ignored
        ms = int(os.environ.get("CAPSULE_INCIDENTS_BUSY_MS", ONE_SHOT_BUSY_MS))
    except ValueError:
        ms = ONE_SHOT_BUSY_MS
    conn.execute(f"PRAGMA busy_timeout={ms}")
    return conn


def _explain(exc):
    """The message for a failed command: a locked board says it is busy, not a bare sqlite error."""
    if isinstance(exc, sqlite3.OperationalError) and "locked" in str(exc):
        return "the board is busy (another process holds it); retry in a moment"
    return str(exc)


def _app_event(conn, args, cfg, board, home):
    main_sha = incidents.main_head(incidents.settings(cfg)["main_checkout"])
    if args.file is None:
        ids = app_events.drain(conn, board=board, main_sha=main_sha,
                               inbox_dir=app_events.inbox(paths.board_dir(board, home)))
        print(f"app events: {len(set(ids))} incident(s) opened, deduped or repeated")
        return 0
    path = Path(args.file).resolve()
    if not path.is_file():
        print(f"capsule incidents app-event: no file {args.file!r}", file=sys.stderr)
        return 1
    inc_id, action = app_events.intake(      # a bad file: ValueError, main prints one line, rc 1
        conn, board=board, doc=app_events.read_file(path), evidence=[str(path)], main_sha=main_sha,
        app_dir=app_events.inbox(paths.board_dir(board, home)).parent)
    print(f"{inc_id} {action}")
    return 0


def _board_wide(conn, args, cfg, board, home):
    """The commands that take no incident id: `app-event` and `close-history`."""
    if args.inc_cmd == "app-event":
        return _app_event(conn, args, cfg, board, home)
    return _close_history(conn, args, cfg)


DESC_MAX = 2000
KEY_MAX = 80      # a hand-filed key is a short stable slug; prose goes in --description


def _open(conn, args, cfg, board):
    key, desc = args.root_cause_key.strip(), (args.description or "").strip()
    if not 0 < len(key) <= KEY_MAX:
        raise ValueError(f"--root-cause-key must be 1-{KEY_MAX} characters (got {len(key)}):"
                         " keep it a short stable slug; put prose in --description")
    if len(desc) > DESC_MAX:
        raise ValueError(f"--description is {len(desc)} characters; the limit is {DESC_MAX}:"
                         " shorten it or keep the detail in a vault note and cite that")
    if incident_inputs.blind_incident(args.evidence, args.role):
        desc = ""         # D161: a blind incident holds no free text
    inc_id, action = incidents.open_incident(
        conn, board=board, kind=args.kind, key=key, evidence=args.evidence,
        task_id=args.task, run_id=args.run, role=args.role, description=desc or None,
        main_sha=incidents.main_head(incidents.settings(cfg)["main_checkout"]))
    print(f"{inc_id} {action}")
    return 0


def _on_record(conn, args, cfg, board, home, rec):
    """The commands that act on one existing incident; `close` is the default."""
    if args.inc_cmd == "show":
        rec["evidence"] = json.loads(rec["evidence"] or "[]")
        print(json.dumps(rec, indent=1, ensure_ascii=False))
        return 0
    if args.inc_cmd == "row":
        if args.row == "auto":
            ks = incidents.settings(cfg)
            args.row = incident_landed.append_decision_row(conn, args.incident_id, ks["vault"],
                                                           ks["decisions_note"])
        else:
            incidents.set_decision_row(conn, args.incident_id, args.row)
        print(f"{args.incident_id}: decision row {args.row}")
        return 0
    if args.inc_cmd == "fixcard":
        card = incidents.route_fix_card(conn, cfg, args.incident_id, epic=args.epic,
                                        assignee=args.assignee)
        print(f"{args.incident_id}: fix card {card} on {args.epic}; unblock it to dispatch")
        return 0
    if args.inc_cmd in ("retest", "landed"):
        tests = incidents.settings(cfg)["blind_tests"]
        return (_retest(conn, args, tests) if args.inc_cmd == "retest"
                else _landed(conn, args, cfg, board, home))
    if args.inc_cmd == "duplicate":
        return _duplicate(conn, args)
    if args.inc_cmd == "attach":
        card = incident_attach.attach(conn, args.incident_id, args.card, board=board, home=home)
        print(f"{args.incident_id}: fix card {card}")
        return 0
    return _close(conn, args, cfg, home)


def main(args):
    home = paths.home(args.capsule_home)
    cfg = _cfg(home, args.board)
    board = args.board or cfg.get("board")
    if not board:
        print("capsule incidents: no --board and no config board", file=sys.stderr)
        return 1
    board_db = paths.board_db(board, home)
    if not board_db.exists():
        print(f"capsule incidents: no board {board!r} under {home}", file=sys.stderr)
        return 1
    ro = args.inc_cmd in ("list", "show") or (args.inc_cmd == "close-history" and not args.apply)
    conn = _connect(board_db, ro)
    try:
        if not ro:
            db.migrate(conn)          # a board from before row 456 gets the (empty) table
        if args.inc_cmd == "list":
            for r in incidents.listing(conn, args.status):
                desc = " ".join((r.get("description") or "").split())     # absent on an old board
                print(f"{r['id']}  {r['status']:<11} {r['kind']:<18} task={r['task_id'] or '-'}"
                      f" repeat_of={r['repeat_of'] or '-'} fix={r['fix_card'] or '-'} row={r['decision_row'] or '-'}"
                      f"  {r['root_key'] or ''}"
                      f"{' | ' + desc if desc else ''}"[:300])
            return 0
        if args.inc_cmd == "open":
            return _open(conn, args, cfg, board)
        if args.inc_cmd in ("close-history", "app-event"):
            return _board_wide(conn, args, cfg, board, home)
        rec = incidents.get(conn, args.incident_id)
        if rec is None:
            print(f"capsule incidents: no incident {args.incident_id!r} on {board}", file=sys.stderr)
            return 1
        return _on_record(conn, args, cfg, board, home, rec)
    except (sqlite3.OperationalError, ValueError, LookupError) as exc:
        print(f"capsule incidents: {_explain(exc)}", file=sys.stderr)
        return 1
    finally:
        conn.close()
