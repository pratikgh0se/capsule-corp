"""The ask gate, unit 3 "Apply and tell" (design 2026-11-23, sections 5 to 9). In mode `on` only:
a `decide` or `precedent` verdict lifts the owner-held card after `diligence.apply_wait_s`; an
`escalate` verdict waits in `<board>/asks/queue/` and one ask goes out through `cos_ask` when no
ask is open and fewer than `asks_per_day` went out today; a `refuse` puts one comment on the card.
Two vetoes of gate decisions in 7 days make the effective mode `shadow` and queue one ask that
says so. Everything it did is a row of `<board>/asks/verdicts.jsonl` (`applied`, `sent`, `told`,
`tripped`), so each step runs once. Modes `shadow` and `off` never reach this module's actions."""
import argparse
import hashlib
import json
import sqlite3
import sys
import time
from datetime import datetime

from capsule import answer_base, config, db, outbox, paths, safe_write, stall_owner
from capsule.ask_log import log, verdict_rows
from capsule.db import event, guard_get, guard_set, txn

DAY, WEEK = 86400, 7 * 86400
PATROL_S = 900      # one patrol interval: the stall page waits this long past `apply_wait_s`
ASK_SKIPS = ("decided alone once since the human verb", "the never list bars it")
SOURCES = {"decide": "panel", "precedent": "precedent"}
FIXED = {"title": "One decision waits for you", "what": "A card waits for your answer.",
         "why": "The lane stays blocked until you answer.", "if_yes": "The card goes on.",
         "if_no": "The card stays parked.", "pick": "Yes, read the brief first",
         "because": "the brief has the detail"}
TRIP_TEXT = ("The ask gate paused itself\nTwo of its decisions were vetoed in one week, so it"
             " only watches now and you get every question. It starts again by itself when the"
             " vetoes are more than 7 days old.")


def _ro(home, board):
    path = paths.board_db(board, home)
    return answer_base.connect_ro_tolerant(path) if path.is_file() else None


def effective(home, board, cfg, now):
    """The `diligence` block with the mode the gate uses now: `on` becomes `shadow` while
    `veto_trip` gate decisions were vetoed in the last 7 days; one ask is queued per episode."""
    dil = config.diligence(cfg)
    conn = _ro(home, board) if dil["mode"] == "on" and dil["veto_trip"] else None
    if conn is None:
        return dil
    try:
        times = [r[0] for r in conn.execute(
            "SELECT v.created_at FROM task_events v JOIN task_events d ON d.kind='auto_decision'"
            " AND json_extract(d.payload,'$.id')=json_extract(v.payload,'$.id')"
            " WHERE v.kind='auto_decision_vetoed' AND v.created_at>? AND"
            " json_extract(d.payload,'$.source') IN ('panel','precedent') ORDER BY v.created_at",
            (now - WEEK,))]
    finally:
        conn.close()
    if len(times) < dil["veto_trip"]:
        return dil
    first = times[0]
    if not any(r.get("verdict") == "tripped" and r.get("at", 0) >= first
               for r in verdict_rows(home, board)):
        base = paths.board_dir(board, home) / "asks" / "queue"
        base.mkdir(parents=True, exist_ok=True)
        (base / f"veto_trip_{first}.json").write_text(json.dumps({"kind": "veto_trip"}) + "\n")
        log(home, board, {"at": now, "subject": "ask_gate", "verdict": "tripped",
                          "reason": f"{len(times)} gate decisions vetoed in 7 days"})
    return {**dil, "mode": "shadow"}


def _local_midnight(now):
    d = datetime.fromtimestamp(now)
    return int(d.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())


def _label(home, board, dossier, pick):
    try:
        doc = json.loads((paths.board_dir(board, home) / "asks" / "dossiers"
                          / f"{dossier}.json").read_text())
    except (OSError, ValueError):
        return ""
    return next((str(o.get("label"))[:300] for o in doc.get("options") or []
                 if isinstance(o, dict) and o.get("id") == pick), "")


def _closed(row, now, why):
    return {"at": now, "subject": row.get("subject"), "dossier": row["dossier"],
            "verdict": "apply_skipped", "reason": why}


def _requeue(home, board, dossier, card):
    """Put the stored dossier in the escalate queue (the path an `escalate` verdict uses). The
    file name is the dossier id, so a repeat writes the same file. A parked card gets none."""
    if card["block_kind"] == "parked":
        return
    base = paths.board_dir(board, home) / "asks"
    try:
        body = (base / "dossiers" / f"{dossier}.json").read_text()
    except OSError:
        return
    safe_write.atomic_write(home, base / "queue" / f"{dossier}.json", body, create_dirs=True)


def _waits(conn, card):
    """The card waits for the owner now: held (triage or blocked) and an owner wait. A decision
    card the owner answered is no longer held, whatever its `request_needs` says."""
    return card["status"] in ("triage", "blocked") and stall_owner.owner_held(conn, card)


def _asks(conn, card):
    """The card needs an ask: it waits for the owner and is not parked. A parked card cannot take
    an unblock answer, so no ask is sent."""
    return _waits(conn, card) and card["block_kind"] != "parked"


def _skip_reason(conn, cfg, card, row, now):
    """Why this verdict must not lift the card now (the row is closed with it), else ''."""
    newer = conn.execute("SELECT 1 FROM task_events WHERE task_id=? AND kind='blocked' AND"
                         " created_at>?", (card["id"], row.get("at", 0))).fetchone()
    for bad, why in ((not _waits(conn, card), "the card no longer waits"),
                     (newer is not None, "the card was held again after the verdict"),
                     (stall_owner._decided_since_human(conn, card["id"]),
                      "decided alone once since the human verb"),
                     (bool(stall_owner.never(conn, cfg, card, now)), "the never list bars it")):
        if bad:
            return why
    return ""


def _apply_one(conn, cfg, home, board, row, now):
    """Lift one card for one verdict row; the row to log: `applied`, or `apply_skipped` with the
    reason. A crash after the lift is safe: the `auto_decision` of the dossier is found again."""
    card = conn.execute("SELECT * FROM tasks WHERE id=?", (row.get("subject"),)).fetchone()
    if card is None:
        return _closed(row, now, "no such card")
    made = conn.execute("SELECT json_extract(payload,'$.id') FROM task_events WHERE"
                        " kind='auto_decision' AND json_extract(payload,'$.dossier')=?",
                        (row["dossier"],)).fetchone()
    if made:
        return {"at": now, "subject": card["id"], "dossier": row["dossier"],
                "verdict": "applied", "decision": made[0], "source": SOURCES[row["verdict"]]}
    why = _skip_reason(conn, cfg, card, row, now)
    if why:
        if why in ASK_SKIPS:     # the card still waits for the owner: one ask must go out
            _requeue(home, board, row["dossier"], card)
        return _closed(row, now, why)
    pick, source = row["pick"], SOURCES[row["verdict"]]
    did = stall_owner.decision_id(conn, card["id"], now)
    label = _label(home, board, row["dossier"], row["pick"])
    undo = f"python3 -m capsule veto {board} {did}"
    key = f"stall:owner_wait:{card['id']}"
    with txn(conn):
        if not stall_owner._lift(conn, card, "release", now):
            return _closed(row, now, "the card could not be lifted")
        event(conn, card["id"], "intervention",
              {"kind": "answer", "at": now, "author": "capsule", "src": did,
               "text": f"answered {pick} by the ask gate ({source}): {label}"
                       f" (veto: {undo})"[:1000]}, now=now)
        event(conn, card["id"], "auto_decision",
              {"id": did, "class": "owner_wait", "option": pick, "source": source, "undo": undo,
               "basis": f"{source}: {row.get('reason', '')}"[:300], "label": label,
               "dossier": row["dossier"], "cites": row.get("cites") or []}, now=now)
        outbox.add(conn, "file", f"the ask gate decided: {card['title'][:80]!r} -> {pick}"
                   f" ({source}). It was reversible. To undo, veto: {undo}", now=now)
        rec = guard_get(conn, key)[0]
        if rec:      # the stall record, as `stall_owner._apply` leaves it
            guard_set(conn, key, dict(rec, level="decided", decision=did), now=now)
    return {"at": now, "subject": card["id"], "dossier": row["dossier"], "verdict": "applied",
            "decision": did, "source": source}


def _apply(home, board, cfg, dil, rows, now):
    """Apply the `on` verdicts that are due. A row from shadow mode never applies."""
    done = {r.get("dossier") for r in rows if r.get("verdict") in ("applied", "apply_skipped")}
    todo = [r for r in rows if r.get("verdict") in SOURCES and r.get("rule") != "R1"
            and r.get("mode") == "on" and r.get("pick") and r.get("dossier") not in done
            and now - r.get("at", 0) >= dil["apply_wait_s"]]
    if not todo:
        return
    conn = db.connect(paths.board_db(board, home))
    try:
        for row in todo:
            log(home, board, _apply_one(conn, cfg, home, board, row, now))
    finally:
        conn.close()


def _tell(home, board, rows, now):
    """One board comment for each refused dossier whose subject is a card."""
    told = {r.get("ref") for r in rows if r.get("verdict") == "told"}
    todo = [(r, r.get("dossier") or f"{r.get('subject')}@{r.get('at')}") for r in rows
            if r.get("verdict") == "refuse" and r.get("subject") not in (None, "ask_gate")]
    todo = [(r, k) for r, k in todo if k not in told]
    conn = db.connect(paths.board_db(board, home)) if todo else None
    if conn is None:
        return
    try:
        for r, key in todo:
            if conn.execute("SELECT 1 FROM tasks WHERE id=?", (r["subject"],)).fetchone() is None:
                continue
            with txn(conn):
                conn.execute("INSERT INTO task_comments (task_id, author, body, created_at)"
                             " VALUES (?,?,?,?)", (r["subject"], "capsule",
                             f"The ask gate refused this question: {r.get('reason', '')}. The card"
                             " stays held. Fix the dossier and ask again.", now))
            if any(t.get("verdict") == "told" and t.get("subject") == r["subject"]
                   and now - t.get("at", 0) < 86400 for t in verdict_rows(home, board)):
                _incident(conn, board, r, now)      # a second refuse for the card in 24 h
            log(home, board, {"at": now, "subject": r["subject"], "verdict": "told", "ref": key})
    finally:
        conn.close()


def _incident(conn, board, row, now):
    """One incident line when the gate refuses a card's dossier twice in 24 h (design section 7,
    `refuse`). The key is fixed, so repeats of one day fold into one incident."""
    from capsule import incidents
    try:
        incidents.open_incident(
            conn, board=board, kind="invalid_document", key="ask gate refused a dossier twice",
            task_id=row["subject"],
            evidence=[f"card {row['subject']}: ask gate refused: {row.get('reason', '')}"],
            now=now)
    except (sqlite3.Error, ValueError) as exc:     # the comment is the main record; do not stop
        print(f"capsule ask-gate: incident line failed: {exc}", file=sys.stderr)


def pending(home, board, cfg, card_id, since, now):
    """True while the gate (mode `on`) still owes the card an action: logged `decide` or
    `precedent` rows of the card at or after `since` (the hold start), with no `applied`,
    `apply_skipped`, `sent` or `dropped` row for their dossier. The wait runs from the OLDEST such
    row and lasts `apply_wait_s` plus one patrol run, so new rows never extend it. The stall page
    waits meanwhile (design B24); after that the normal paging runs."""
    dil = config.diligence(cfg)
    if dil["mode"] != "on":
        return False
    rows = verdict_rows(home, board)
    done = {r.get("dossier") for r in rows
            if r.get("verdict") in ("applied", "apply_skipped", "sent", "dropped")}
    ats = [r["at"] for r in rows if r.get("subject") == card_id and r.get("mode") == "on"
           and r.get("verdict") in ("decide", "precedent") and r.get("dossier") not in done
           and r.get("at", 0) >= (since or 0)]
    return bool(ats) and now - min(ats) < dil["apply_wait_s"] + PATROL_S


def _args(board, doc, plain, options):
    ids = [o["id"] for o in doc.get("options") or [] if isinstance(o, dict) and "id" in o]
    yes = doc.get("recommended") if doc.get("recommended") in ids else (ids[0] if ids else None)
    no = next((i for i in ids if i != yes), None)
    both = yes in options and no in options if options else False
    return argparse.Namespace(
        board=board, task_id=doc["subject"], **{k: plain[k] for k in FIXED},
        card_name=None, project=None, yes_option=yes if both else None,
        no_option=no if both else None, no_reason="the owner declined the question",
        dossier=doc["id"], queued=True, key=_ask_key(doc["id"]))


def _send_dossier(home, board, doc):
    from capsule import cos_ask
    conn = _ro(home, board)
    try:
        options = cos_ask._decision_options(conn, doc["subject"]) if conn else None
    finally:
        if conn:
            conn.close()
    plain = {**doc.get("plain", {})}
    for texts in (plain, FIXED):
        try:
            return cos_ask._ask(home, _args(board, doc, texts, options))
        except (ValueError, KeyError) as exc:
            err = str(exc)
    raise ValueError(err)


def _open_ask(home, board, rows):
    """True when an ask this step sent is still unanswered (its card still waits)."""
    subjects = {r.get("subject") for r in rows if r.get("verdict") == "sent"} - {"ask_gate"}
    conn = _ro(home, board) if subjects else None
    if conn is None:
        return False
    try:
        return any(card is not None and _asks(conn, card) for card in
                   (conn.execute("SELECT * FROM tasks WHERE id=?", (s,)).fetchone()
                    for s in subjects))
    finally:
        conn.close()


def _ask_key(name):
    """A 16-hex ask key from a stable name, so a send repeated after a crash is the same ask."""
    return hashlib.sha256(name.encode()).hexdigest()[:16]


def _send_trip(home, board, name):
    conn = db.connect(paths.board_db(board, home))
    try:
        with txn(conn):      # no `needs:ask-<16 hex>` key and no ask file: text only, no buttons
            outbox.notify(conn, "ask", TRIP_TEXT, key=f"needs:ask-{name}")
    finally:
        conn.close()
    return name


def _held(home, board, subject):
    conn = _ro(home, board)
    try:
        card = conn.execute("SELECT * FROM tasks WHERE id=?", (subject,)).fetchone()
        return card is not None and _asks(conn, card)
    finally:
        conn.close()


def _drop_why(home, board, doc):
    conn = _ro(home, board)
    try:
        card = conn.execute("SELECT block_kind FROM tasks WHERE id=?",
                            (doc.get("subject"),)).fetchone() if conn else None
    finally:
        if conn:
            conn.close()
    return ("the card is parked" if card and card["block_kind"] == "parked"
            else "the card no longer waits")


def _send_file(home, board, path, doc, rows):
    """Send one queue file; `(key, subject)`, or None when it fails (one `send_failed` row, the
    file stays queued), was sent before, or its card no longer waits (the file is removed; a
    `dropped` row says so)."""
    now = int(time.time())
    if doc.get("id") and any(r.get("verdict") == "sent" and r.get("dossier") == doc["id"]
                             for r in rows):
        path.unlink()
        return None
    if doc.get("kind") != "veto_trip" and not _held(home, board, doc.get("subject")):
        why = _drop_why(home, board, doc)
        path.unlink()
        log(home, board, {"at": now, "subject": doc.get("subject"), "dossier": doc.get("id"),
                          "verdict": "dropped", "reason": why})
        return None
    try:
        if doc.get("kind") == "veto_trip":
            return _send_trip(home, board, path.stem), "ask_gate"
        return _send_dossier(home, board, doc), doc["subject"]
    except (ValueError, KeyError, OSError) as exc:
        if not any(r.get("verdict") == "send_failed" and r.get("dossier") == doc.get("id")
                   for r in rows):
            log(home, board, {"at": now, "subject": doc.get("subject", "ask_gate"),
                              "dossier": doc.get("id"), "verdict": "send_failed",
                              "reason": str(exc)[:300]})
        return None


def _send(home, board, dil, rows, now):
    """Send the queued trip ask (text only, always), then the oldest queued dossier when none is
    open and today's cap is not used. A trip ask does not count for the cap."""
    base = paths.board_dir(board, home) / "asks" / "queue"
    files = sorted(base.glob("*.json"), key=lambda p: p.stat().st_mtime) if base.is_dir() else []
    trips = [p for p in files if p.name.startswith("veto_trip_")]
    for path in trips:
        got = _send_file(home, board, path, json.loads(path.read_text() or "{}"), rows)
        if got:
            path.unlink()
            log(home, board, {"at": now, "subject": got[1], "dossier": None,
                              "verdict": "sent", "key": got[0]})
    files = [p for p in files if p not in trips]
    today = sum(1 for r in rows if r.get("verdict") == "sent" and r.get("subject") != "ask_gate"
                and r.get("at", 0) >= _local_midnight(now))
    if not files or today >= dil["asks_per_day"] or _open_ask(home, board, rows):
        return
    for path in files:
        doc = json.loads(path.read_text() or "{}")
        got = _send_file(home, board, path, doc, rows)
        if got:
            path.unlink()
            log(home, board, {"at": now, "subject": got[1], "dossier": doc.get("id"),
                              "verdict": "sent", "key": got[0]})
            return


def step(home, board, now=None):
    """Mode `on` only: apply the decisions that are due, tell each refused asker, send one queued
    ask. Safe to repeat: every action leaves a log row. A tripped gate (effective mode `shadow`)
    only sends what is queued, such as the trip ask. Returns the effective mode."""
    now = int(now or time.time())
    cfg = config.load(paths.resolve_config(home, board))
    dil = effective(home, board, cfg, now)
    if config.diligence(cfg)["mode"] != "on" or not paths.board_db(board, home).is_file():
        return dil["mode"]
    if dil["mode"] == "on":
        _apply(home, board, cfg, dil, verdict_rows(home, board), now)
        _tell(home, board, verdict_rows(home, board), now)
    _send(home, board, dil, verdict_rows(home, board), now)
    return dil["mode"]


def line(home, boards, start, end):
    """One digest line (decided, sent, refused in the window), or '' when the gate did nothing."""
    rows = [r for b in boards for r in verdict_rows(home, b) if start <= r.get("at", 0) < end]
    dec, esc, ref = (sum(1 for r in rows if r.get("verdict") == v)
                     for v in ("applied", "sent", "told"))
    if not (dec or esc or ref):
        return ""
    return f"Ask gate: {dec} decided for you, {esc} sent to you, {ref} refused."


MAX_DETAIL = 5


def _decision(conn, row):
    """`(title, pick, undo)` of one applied row, from the card and its `auto_decision` event."""
    ev = conn.execute("SELECT payload FROM task_events WHERE kind='auto_decision' AND"
                      " json_extract(payload,'$.id')=? AND task_id=?",
                      (row.get("decision"), row.get("subject"))).fetchone()
    card = conn.execute("SELECT title FROM tasks WHERE id=?", (row.get("subject"),)).fetchone()
    if ev is None or card is None:
        return None
    doc = json.loads(ev[0])
    vetoed = conn.execute("SELECT 1 FROM task_events WHERE kind='auto_decision_vetoed' AND"
                          " json_extract(payload,'$.id')=?", (row.get("decision"),)).fetchone()
    undo = None if vetoed else doc.get("undo")
    return (card[0] or ""), doc.get("label") or doc.get("option"), undo


def detail(home, boards, start, end, bullet="- "):
    """One plain line for each gate decision in the window (title, the option picked, the veto
    command or "You vetoed this"); at most MAX_DETAIL lines, then "and N more", then a note when
    a decision has no card or record to show. Empty when the gate decided nothing. A board that
    cannot be read is skipped."""
    from capsule import statusview
    found, total = [], 0
    for board in boards:
        rows = [r for r in verdict_rows(home, board)
                if r.get("verdict") == "applied" and start <= r.get("at", 0) < end]
        total += len(rows)
        conn = None
        try:
            conn = _ro(home, board) if rows else None
            if conn is not None:
                found += [d for d in (_decision(conn, r) for r in rows) if d]
        except (sqlite3.Error, ValueError, TypeError):
            continue
        finally:
            if conn is not None:
                conn.close()
    out = [f'{bullet}{statusview.plain(t, 80)}: chose "{str(p)[:100]}". '
           + (f"To undo: {u}" if u else "You vetoed this.") for t, p, u in found[:MAX_DETAIL]]
    if len(found) > MAX_DETAIL:
        out.append(f"{bullet}and {len(found) - MAX_DETAIL} more")
    if total > len(found):
        out.append(f"{bullet}({total - len(found)} not shown: card or record missing)")
    return out
