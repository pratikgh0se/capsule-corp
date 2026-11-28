"""Cross-board card filing (factory-autonomy spec 3.5). The tick is its board's only writer
(spec §2), so another board never opens this board's db for writing: it drops a file under
boards/<board>/intake/, and this board's tick ingests it in step (1b). A touch or auto-amend file
must name something real on its source board, read read-only (provenance, a second oracle
beside the file itself)."""
import json
import os
import secrets
import sqlite3
import sys
import time
from pathlib import Path

from capsule import card_fields, config, db, outbox, paths, prior_verdicts
from capsule.db import event, txn

sys.path.insert(0, str(paths.REPO / "maintenance"))
import budgets  # noqa: E402

KINDS = ("human_touch", "auto_amend", "card", "follow_up", "owner_look")
WAIT_S = 86400                # a document that waits longer than this for a dependency is refused
LANDINGS = ("auto", "triage")
DEDUPE_S = 7 * 86400
FIELDS = ("intake_id", "kind", "fingerprint", "title", "body", "source_board", "source_task", "incident", "epic",
          "assignee", "file_set", "landing", "at")


def _dir(home, board):
    return paths.board_dir(board, Path(home)) / "intake"


def enqueue(home, board, *, kind, fingerprint, title, body, source_board, source_task, incident=None, epic=None,
            assignee=None, file_set=None, landing="auto", name=None, fields=None, depends_on=None,
            now=None):
    if kind not in KINDS or landing not in LANDINGS:
        raise ValueError(f"intake kind {kind!r} / landing {landing!r}")
    rid = "rq_" + secrets.token_hex(4)
    d = _dir(home, board)
    d.mkdir(parents=True, exist_ok=True)
    doc = {"intake_id": rid, "kind": kind, "fingerprint": fingerprint, "title": title[:120], "body": body,
           "source_board": source_board, "source_task": source_task, "incident": incident, "epic": epic,
           "assignee": assignee, "file_set": list(file_set or []), "landing": landing,
           "at": int(now if now is not None else time.time())}
    if name:
        doc["name"] = name
    if fields:
        doc["card_fields"] = fields
    if depends_on:
        doc["depends_on"] = list(depends_on)
    tmp = d / f".{rid}.tmp"
    tmp.write_text(json.dumps(doc, ensure_ascii=False))
    os.replace(tmp, d / f"{rid}.json")
    return rid


def card_for(conn, intake_id):
    row = conn.execute("SELECT task_id FROM task_events WHERE kind='intake_ingested'"
                       " AND json_extract(payload, '$.intake_id')=? ORDER BY id LIMIT 1", (intake_id,)).fetchone()
    if row is None:
        return None
    return conn.execute("SELECT * FROM tasks WHERE id=?", (row[0],)).fetchone()


def is_intake_card(conn, task_id):
    """A card this module created (its `created` event says `by: intake`; `db.new_task` writes the
    first `created` with `by: capsule`, so every `created` row is read, not the first)."""
    return any(json.loads(r[0] or "{}").get("by") == "intake" for r in conn.execute(
        "SELECT payload FROM task_events WHERE task_id=? AND kind='created'", (task_id,)))


def provenance(home, doc):
    """None when the file's claim checks out on its source board (read-only), else why not."""
    if doc["kind"] in ("card", "owner_look"):
        return None
    src = paths.board_db(doc["source_board"] or "", Path(home))
    if not doc["source_board"] or not src.exists():
        return f"source board {doc['source_board']!r} has no Capsule db"
    conn = db.connect_ro(src)
    try:
        if conn.execute("SELECT 1 FROM tasks WHERE id=?", (doc["source_task"],)).fetchone() is None:
            return f"no task {doc['source_task']!r} on {doc['source_board']}"
        if doc["kind"] == "follow_up":
            return None
        if doc["kind"] == "human_touch":
            ok = conn.execute("SELECT 1 FROM incidents WHERE id=? AND task_id=?",
                              (doc["incident"], doc["source_task"])).fetchone()
        else:
            ok = conn.execute("SELECT 1 FROM task_events WHERE task_id=? AND kind='contract_amended'"
                              " AND json_extract(payload, '$.by')='guide-auto'", (doc["source_task"],)).fetchone()
        return None if ok else f"no {doc['kind']} record for {doc['source_task']} on {doc['source_board']}"
    finally:
        conn.close()


def _open_same(conn, fingerprint, now):
    for (raw, task_id) in conn.execute(
            "SELECT e.payload, e.task_id FROM task_events e JOIN tasks t ON t.id=e.task_id WHERE e.kind='intake_ingested'"
            " AND e.created_at>=? AND t.status NOT IN ('done','archived') ORDER BY e.id", (now - DEDUPE_S,)):
        p = json.loads(raw or "{}")
        if p.get("fingerprint") == fingerprint and not p.get("deduped_into"):
            return task_id
    return None


class _Waiting(Exception):
    """A dependency of this document is not ingested yet: it stays in the spool."""


def _parents(conn, doc, now):
    """Task ids for `depends_on` (the fingerprints of other documents). A fingerprint not yet
    ingested: `_Waiting` (`ingest` ages it out, see `_expire`)."""
    out = []
    for fp in doc.get("depends_on") or []:
        row = conn.execute("SELECT task_id FROM task_events WHERE kind='intake_ingested'"
                           " AND json_extract(payload, '$.fingerprint')=? ORDER BY id LIMIT 1",
                           (fp,)).fetchone()
        if row is None:
            raise _Waiting(fp)
        out.append(row[0])
    return out


def _landing(cfg, doc):
    if doc["kind"] == "owner_look":
        return "blocked", doc["epic"], None
    if doc["landing"] == "triage":
        return "triage", None, None
    a = config.autonomy(cfg)
    epic, assignee = (doc["epic"], doc["assignee"]) if doc["kind"] == "card" else (a["touch_epic"], a["touch_assignee"])
    role = (cfg.get("assignees") or {}).get(assignee)
    if epic in config.epic_branches(cfg) and role == "implementer":
        return "ready", epic, assignee
    return "triage", None, None


def _inherit(conn, cfg, doc):
    """A follow_up card starts like its source card on the same board: tier, assignee, check and,
    on a declared epic branch, the branch and the routed `ready` start. Source gone or no tier:
    `{}`, the old triage landing."""
    if doc["kind"] != "follow_up" or doc["source_board"] != cfg["board"]:
        return {}
    src = conn.execute("SELECT * FROM tasks WHERE id=?", (doc["source_task"],)).fetchone()
    if src is None or not src["tier"]:
        return {}
    # the item's own assignee, epic and card_fields win (the caller merges card_fields over this)
    assignee = doc["assignee"] or src["assignee"]
    out = {"tier": src["tier"], "assignee": assignee, "check_cmd": src["check_cmd"],
           "check_expect": src["check_expect"]}
    branch = doc["epic"] or src["branch_name"]
    if branch in config.epic_branches(cfg):
        out["branch"] = branch
        if (cfg.get("assignees") or {}).get(assignee) == "implementer":
            out["status"] = "ready"
    return out


def _park(d, path, why):
    (d / "bad").mkdir(exist_ok=True)
    os.replace(path, d / "bad" / path.name)
    print(f"capsule tick: intake {path.name} refused ({why}); moved to bad/", file=sys.stderr)


def _sizing(cfg, doc):
    """M032, row 126: (comment, over) for a non-epic card. Over the files_park ceiling: the
    `budget:` reason and over=True, so the caller also sends one line. Over files_route: a soft
    note only. The card is admitted and lands where it always did (triage), never refused. A card
    on a declared epic branch is exempt: (None, False)."""
    if doc["kind"] != "card" or doc["epic"] in config.epic_branches(cfg):
        return None, False
    th = budgets.thresholds()
    n = len({f.strip() for f in doc["file_set"] if f.strip()})
    if n > th["files_park"]:
        return budgets.park_reason(f"{n} files over the ceiling of {th['files_park']}:"
                                   " split it or make it an epic"), True
    if n > th["files_route"]:
        return (f"{n} files, over the {th['files_route']}-file routing threshold: a size target,"
                " not a refusal. Splitting it is better."), False
    return None, False


def _ingest_one(conn, home, cfg, path, now):
    """One file: parse, dedupe/provenance-check, write. Returns the touched task id, or None on
    an already-ingested intake_id (idempotent on the id itself, not on the file's done/ move -
    the same id dropped a second time is still a noop). Any failure - a malformed document, a
    right-shaped-wrong-typed one, or a broken source db - raises; the caller parks the file and
    the board's tick keeps going (row 456: one bad file must never wedge a pass)."""
    doc = json.loads(path.read_text())
    if (not isinstance(doc, dict) or set(doc) - {"name", "card_fields", "depends_on"} != set(FIELDS)
            or doc["kind"] not in KINDS or doc["landing"] not in LANDINGS
            or not isinstance(doc.get("depends_on", []), list)):
        raise ValueError("bad intake document")
    if conn.execute("SELECT 1 FROM task_events WHERE kind='intake_ingested' AND json_extract(payload,'$.intake_id')=?",
                    (doc["intake_id"],)).fetchone() is not None:
        return None
    why = provenance(home, doc)
    if why:
        raise ValueError(why)
    meta = {k: doc[k] for k in ("intake_id", "kind", "fingerprint", "source_board", "source_task")}
    parents = _parents(conn, doc, now)
    own = doc["kind"] in ("card", "owner_look")      # these never merge into an open card
    same = None if own else _open_same(conn, doc["fingerprint"], now)
    if same is not None:
        with txn(conn):
            conn.execute("INSERT INTO task_comments (task_id, author, body, created_at) VALUES (?,?,?,?)",
                         (same, "capsule", f"again ({doc['intake_id']}): {doc['body'][:1500]}", now))
            event(conn, same, "intake_ingested", dict(meta, deduped_into=same), now=now)
        return same
    status, branch, assignee = _landing(cfg, doc)
    inh = _inherit(conn, cfg, doc)
    status, branch = inh.pop("status", status), inh.pop("branch", branch)
    assignee = inh.pop("assignee", assignee)
    note, over = _sizing(cfg, doc)
    body = f"{doc['body']}\n\nintake: {doc['intake_id']}; incident: {doc['incident'] or 'none'}"
    body += prior_verdicts.section(conn, cfg, doc["title"], doc["body"], now=now)
    # the `runner` field is the task's `tier` column (never its risk class)
    extra = {card_fields.COLUMN.get(k, k): v
             for k, v in card_fields.clean(doc.get("card_fields") or {}).items()}
    with txn(conn):                                   # one txn: new_task's own commit never lands alone
        tid = db.new_task(conn, title=doc["title"], body=body, status=status, branch=branch,
                          assignee=assignee, file_set=doc["file_set"],
                          short_name=doc.get("name"), now=now, parents=parents, **{**inh, **extra})
        if doc["kind"] == "owner_look":      # held for the owner; U4 asks him, no notice here
            conn.execute("UPDATE tasks SET block_kind='owner_look' WHERE id=?", (tid,))
        event(conn, tid, "created", dict(meta, by="intake"), now=now)
        event(conn, tid, "intake_ingested", meta, now=now)
        if note:
            conn.execute("INSERT INTO task_comments (task_id, author, body, created_at)"
                         " VALUES (?,?,?,?)", (tid, "capsule", note, now))
        if over:                             # stays in triage; `reset` is the way out
            reply = f"python3 -m capsule human {cfg['board']} {tid} reset '<answer>'"
            outbox.notify(conn, "needs_input", f"{tid} over the file ceiling",
                          fields=[("reason", note)], reply=reply, thread_key=tid, now=now,
                          file_body=f"capsule: {tid} over the file ceiling: {note}"
                                    f" | reply: {reply}")
        if status == "ready":                # every card that will be built unattended is announced
            outbox.notify(conn, "running", f"{tid} filed by intake ({doc['kind']}), ready on {branch}",
                          file_body=f"capsule: {tid} filed by intake {doc['intake_id']} ({doc['kind']}),"
                                    f" ready on {branch}: {doc['title']}", thread_key=tid, now=now)
    return tid


def step(conn, home, cfg, now, counts, interventions):
    """Tick steps (1) and (1b): human interventions, then this board's intake spool (spec 3.5)."""
    counts["interventions"] = interventions(conn, cfg, now)
    counts["intake"] = len(ingest(conn, home, cfg["board"], cfg, now=now))


def _once(conn, home, cfg, d, path, now, touched):
    """One file of a pass: ingest and archive it, or park it. True when a card was created or
    merged, `_Waiting` is raised to the caller when a dependency is missing."""
    try:
        tid = _ingest_one(conn, home, cfg, path, now)
    except sqlite3.OperationalError as exc:
        if "locked" in str(exc):      # transient: keep the file for the next pass
            print(f"capsule tick: intake {path.name} deferred (database is locked); retrying",
                  file=sys.stderr)
            return False
        _park(d, path, f"{type(exc).__name__}: {exc}")
        return False
    except _Waiting:
        raise
    except Exception as exc:          # any refusal parks the file; the pass never wedges
        _park(d, path, f"{type(exc).__name__}: {exc}")
        return False
    if tid is not None:
        touched.append(tid)
    (d / "done").mkdir(exist_ok=True)
    os.replace(path, d / "done" / path.name)
    return True


def _doc(path):
    """The spool document as a dict; {} when it cannot be read (it was ingested once already)."""
    try:
        doc = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return doc if isinstance(doc, dict) else {}


def _stale(doc, now):
    """True when the document is older than WAIT_S; a missing or wrong-typed `at` counts as old."""
    at = doc.get("at", now)
    return not isinstance(at, (int, float)) or now - at > WAIT_S


def _expire(d, waiting, now):
    """After the last pass: a file that waited over WAIT_S for a fingerprint that no live spool
    file carries moves to bad/, with a sibling `.reason` file that names that fingerprint. A live
    file is a spool file that is young or not waiting, so files that wait on each other expire."""
    stuck = {path for path, _ in waiting}
    carried = set()
    for path in d.glob("rq_*.json"):
        doc = _doc(path)
        if isinstance(doc.get("fingerprint"), str) and not (path in stuck and _stale(doc, now)):
            carried.add(doc["fingerprint"])
    for path, fp in waiting:
        try:
            if _stale(_doc(path), now) and fp not in carried:
                why = f"dependency {fp} was not ingested within {WAIT_S // 3600} hours"
                _park(d, path, why)
                (d / "bad" / f"{path.name}.reason").write_text(why + "\n")
        except Exception as exc:          # one odd file must not fail every tick
            if path.exists():
                _park(d, path, f"{type(exc).__name__}: {exc}")


def ingest(conn, home, board, cfg, *, now=None):
    now = int(now if now is not None else time.time())
    d = _dir(home, board)
    if not d.is_dir():
        return []
    touched, todo = [], sorted((p for p in d.glob("rq_*.json") if p.is_file()),
                               key=lambda p: (p.stat().st_mtime_ns, p.name))
    while todo:                           # repeat while a pass creates something
        waiting, progress = [], False
        for path in todo:
            try:
                progress |= _once(conn, home, cfg, d, path, now, touched)
            except _Waiting as wait:
                waiting.append((path, str(wait)))
        if not progress:
            _expire(d, waiting, now)
            break
        todo = [p for p, _ in waiting]
    return touched
