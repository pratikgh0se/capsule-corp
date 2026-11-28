"""App failures as Korin incidents (D79, D89).
Spec: docs/superpowers/specs/2026-10-29-app-incidents-design.md.

A shipped app's collector (macOS crash reports, GlitchTip, Gatus) drops one `app_event.v1` JSON
file into its product board's inbox; each becomes an incident on that board with role
`app:<name>`, under Korin's own rules (24-hour dedupe, 30-day repeat, row 456). The event's
token `app_event:<source>:<event_id>` is always in the incident's evidence, so taking the same
event twice changes nothing ("seen") and collectors need no state of their own.

An event file is untrusted input: only a small regular file is read, text fields carry no control
characters, evidence paths are limited to crash-report folders and the board's own app-events
folder, and a flood of one fingerprint adds a count, not entries."""
import json
import os
import re
import secrets
import sqlite3
import stat
import sys
import time
from pathlib import Path

from capsule import incidents
from capsule.db import txn

SCHEMA = "app_event.v1"
SOURCE_RE = re.compile(r"^app:[a-z0-9][a-z0-9-]{0,39}$")
CONTROL_RE = re.compile("[\x00-\x1f\x7f-\x9f]")      # C0, DEL and C1
# app kind -> incident kind: the existing kinds, so no `incidents.kind` widening (spec section 4)
KINDS = {"crash": "crash", "hang": "hang", "down": "hang", "error": "silent_failure"}
MAX_PER_PASS = 100
MAX_FILE = 64 * 1024            # an event is one small JSON object: bigger is refused unread
TEXT_LIMITS = {"signature": 500, "event_id": 200, "message": 1000, "app": 100, "version": 100,
               "via": 100}
EVIDENCE_ITEMS, EVIDENCE_CHARS = 10, 1024
MAX_ENTRIES = 20                # evidence entries kept per incident, whatever the flood
COUNT = "app_event_count: "     # the one line that counts every event the incident took
NOTE = "app_event_note: "
SEEN_DAYS = 30                  # `app_event_seen` keeps every taken token, past the evidence cap


def inbox(board_dir):
    """Where collectors drop event files for one board; `done/` and `rejected/` sit beside it."""
    return Path(board_dir) / "app-events" / "inbox"


def _text(doc, field):
    value = doc.get(field)
    return value if isinstance(value, str) else ""


def _problems(doc):
    """[] or one line per reason `doc` is not an `app_event.v1`."""
    if not isinstance(doc, dict):
        return ["not a JSON object"]
    evidence = doc.get("evidence", [])
    listed = isinstance(evidence, list) and all(isinstance(e, str) for e in evidence)
    checks = [
        (doc.get("schema") == SCHEMA, f"schema is not {SCHEMA!r}"),
        (SOURCE_RE.match(_text(doc, "source")), "source is not app:<name> (a-z, 0-9, -)"),
        (_text(doc, "kind") in KINDS, f"kind is not one of {', '.join(KINDS)}"),
        (_text(doc, "signature").strip(), "signature is empty"),
        (_text(doc, "event_id").strip(), "event_id is empty"),
        (listed, "evidence is not a list of strings"),
    ]
    for field, limit in TEXT_LIMITS.items():
        value = doc.get(field, "")
        checks.append((isinstance(value, (str, int, float)) and len(str(value)) <= limit,
                       f"{field} is not text of at most {limit} characters"))
    bad = [k for k, v in doc.items() if isinstance(v, str) and CONTROL_RE.search(v)]
    bad += ["evidence"] if listed and any(CONTROL_RE.search(e) for e in evidence) else []
    checks.append((not bad, f"control character in {', '.join(bad)}"))
    return [why for ok, why in checks if not ok]


def _screen(items, app_dir):
    """(kept evidence, dropped count): absolute paths whose real path is under a crash-report
    folder or this board's app-events folder, at most EVIDENCE_ITEMS of EVIDENCE_CHARS. An
    `event:` or `app_event:` string is not a path, so nobody can forge a token or an event."""
    roots = [os.path.realpath(r) for r in (
        Path.home() / "Library/Logs/DiagnosticReports", "/Library/Logs/DiagnosticReports",
        app_dir)]
    kept = []
    for item in items:
        real = os.path.realpath(item) if os.path.isabs(item) else ""
        if (len(item) <= EVIDENCE_CHARS and len(kept) < EVIDENCE_ITEMS
                and any(real.startswith(r + os.sep) for r in roots)):
            kept.append(real)       # the resolved path: what was checked is what is stored
    return kept, len(items) - len(kept)


def _settle(conn, incident_id, action, extra):
    """Bound the incident's evidence: a new incident also gets `extra`; a dedupe keeps only its
    token (already attached) and adds one to the count. Keeps MAX_ENTRIES entries and the count."""
    have = json.loads(conn.execute("SELECT evidence FROM incidents WHERE id=?",
                                   (incident_id,)).fetchone()["evidence"] or "[]")
    keep = [e for e in have if not e.startswith(COUNT)]
    count = next((int(e[len(COUNT):]) for e in have if e.startswith(COUNT)), 1)
    if action in ("opened", "repeat"):
        keep += [e for e in extra if e not in keep]
    else:
        count += 1
    conn.execute("UPDATE incidents SET evidence=? WHERE id=?", (
        json.dumps(keep[:MAX_ENTRIES] + [f"{COUNT}{count}"], ensure_ascii=False), incident_id))


def intake(conn, *, board, doc, app_dir, evidence=(), main_sha=None, now=None):
    """(incident id, action) for one event: `opened`, `deduped` or `repeat` from
    `incidents.open_incident`, or `seen` (its token is in `app_event_seen`; nothing changes). The
    token row is written in the same transaction as the incident. A malformed event raises
    ValueError naming every problem. `app_dir` is the board's app-events folder, an allowed
    evidence root beside the macOS crash-report folders; `evidence` is the caller's own (trusted)
    list, kept only on a new incident."""
    problems = _problems(doc)
    if problems:
        raise ValueError("; ".join(problems))
    token = f"app_event:{doc['source']}:{doc['event_id']}"
    kept, dropped = _screen(doc.get("evidence", []), app_dir)
    extra = [*evidence, *kept] + ([f"{NOTE}dropped {dropped} evidence item(s)"] if dropped else [])
    now = int(now if now is not None else time.time())
    with txn(conn):
        taken = conn.execute("SELECT incident_id FROM app_event_seen WHERE token=?",
                             (token,)).fetchone()
        if taken is not None:
            return taken["incident_id"], "seen"
        incident_id, action = incidents.open_incident(
            conn, board=board, kind=KINDS[doc["kind"]], key=f"{doc['kind']}: {doc['signature']}",
            role=doc["source"], evidence=[token], main_sha=main_sha, now=now)
        _settle(conn, incident_id, action, extra)
        conn.execute("INSERT INTO app_event_seen (token, incident_id, at) VALUES (?,?,?)",
                     (token, incident_id, now))
        return incident_id, action


def _prune_seen(conn, now):
    """Forget the tokens taken more than SEEN_DAYS ago (once per drain pass)."""
    with txn(conn):
        conn.execute("DELETE FROM app_event_seen WHERE at < ?", (now - SEEN_DAYS * 86400,))


def read_file(path):
    """The parsed event in `path`, or ValueError: only a regular file of at most MAX_FILE bytes,
    opened without following a link or waiting on a pipe, read at most MAX_FILE + 1 bytes."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as fh:
            if not stat.S_ISREG(os.fstat(fh.fileno()).st_mode):
                raise ValueError("not a regular file")
            raw = fh.read(MAX_FILE + 1)
        if len(raw) > MAX_FILE:
            raise ValueError(f"over {MAX_FILE} bytes")
        return json.loads(raw.decode("utf-8"))
    except (OSError, RecursionError) as exc:
        raise ValueError(f"cannot read: {exc.__class__.__name__}") from exc


def _free(folder, name):
    """A path in `folder` for `name` that does not exist yet (a suffix when it would overwrite)."""
    folder.mkdir(exist_ok=True)
    target = folder / name
    while os.path.lexists(target):
        target = folder / f"{Path(name).stem}-{secrets.token_hex(3)}{Path(name).suffix}"
    return target


def _take(conn, path, *, board, main_sha, now):
    """(folder name, incident id or None, destination) for one inbox entry. Only a database
    error escapes: anything else wrong with the entry rejects it."""
    done = _free(path.parent.parent / "done", path.name)
    try:
        if not stat.S_ISREG(os.lstat(path).st_mode):
            raise ValueError("not a regular file")
        incident_id, action = intake(conn, board=board, doc=read_file(path), main_sha=main_sha,
                                     now=now, evidence=[str(done)], app_dir=path.parent.parent)
        return done, (incident_id if action != "seen" else None)
    except sqlite3.Error:
        raise
    except Exception as exc:        # one bad file never aborts the tick (untrusted input)
        print(f"capsule app-events: rejected {path.name}: {exc}", file=sys.stderr)
        return _free(path.parent.parent / "rejected", path.name), None


def drain(conn, *, board, inbox_dir, main_sha=None, now=None):
    """Take up to MAX_PER_PASS `*.json` entries from `inbox_dir`, oldest name first: a valid one
    goes through `intake` and moves to `done/` (that path is its evidence), anything else (bad or
    invalid JSON, a link, directory, pipe, oversize file) moves to `rejected/` with one stderr
    line. A busy or locked database (OperationalError) stops the pass and leaves this and the
    remaining files for the next one; any other database error is the event's fault (rejected).
    Returns the ids opened, deduped or repeated."""
    inbox_dir = Path(inbox_dir)
    if not inbox_dir.is_dir():
        return []
    try:
        _prune_seen(conn, int(now if now is not None else time.time()))
    except sqlite3.Error as exc:
        print(f"capsule app-events: database busy, pass skipped: {exc}", file=sys.stderr)
        return []
    touched = []
    for path in sorted(inbox_dir.glob("*.json"))[:MAX_PER_PASS]:
        try:
            dest, incident_id = _take(conn, path, board=board, main_sha=main_sha, now=now)
        except sqlite3.OperationalError as exc:
            print(f"capsule app-events: database busy, pass stopped at {path.name}: {exc}",
                  file=sys.stderr)
            break
        except sqlite3.Error as exc:
            print(f"capsule app-events: rejected {path.name}: {exc}", file=sys.stderr)
            dest, incident_id = _free(inbox_dir.parent / "rejected", path.name), None
        touched += [incident_id] if incident_id else []
        try:
            os.replace(path, dest)
        except OSError as exc:      # the file stays; a re-take is "seen"
            print(f"capsule app-events: left {path.name}: {exc}", file=sys.stderr)
    return touched
