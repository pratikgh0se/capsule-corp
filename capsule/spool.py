"""The spool: per-run request/report/decision files, validated and applied by the tick
(spec §6.2, rows 416, 420)."""
import json
import os
import re
import stat
import time
from pathlib import Path

from capsule import (blocks, db, image_requests, outbox, prior_verdicts, safe_write,
                     workspace)
from capsule.answer_base import human_dir  # noqa: F401  moved, re-exported
from capsule.db import event, txn
from capsule.jsonschema import validate

SUBDIRS = ("requests", "report", "done")


def make(root, run_id):
    """Create the run's spool; a symlink at the run dir or a subdir raises
    `safe_write.SymlinkRefused` (a worker could have planted one beside the host's writes)."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    spool = root / run_id
    for sub in SUBDIRS:
        os.close(safe_write.open_dir(root, spool / sub, create_dirs=True))
    return spool


def human_by_task(home, board, since_ts):
    """{task_id: [human file docs]} with `at` >= since_ts; a file that is unreadable, not an
    object or has a bad `at` is skipped, never fatal."""
    out = {}
    d = human_dir(home, board)
    # The tick archives handled human files into `done/`; count those too, so the
    # count does not collapse to 0 once that archiving lands.
    for src in (d, d / "done"):
        if not src.is_dir():
            continue
        for p in sorted(src.iterdir()):
            if p.name.startswith(".") or not p.name.endswith(".json"):
                continue
            try:
                doc = json.loads(p.read_text(encoding="utf-8"))
                at = int(doc.get("at") or 0)
            except (OSError, ValueError, TypeError, AttributeError):
                continue
            if at >= since_ts:
                out.setdefault(doc.get("task_id"), []).append(doc)
    return out


def _next_name(spool, sub):
    high = 0
    for d in (spool / sub, spool / "done"):
        if not d.is_dir():
            continue
        for p in d.iterdir():
            if p.stem.isdigit():
                high = max(high, int(p.stem))
    return f"{high + 1}.json"


def write_doc(spool, sub, doc):
    spool = Path(spool)
    name = _next_name(spool, sub)
    final = spool / sub / name
    safe_write.atomic_write(spool, final, json.dumps(doc, ensure_ascii=False))
    return final


def _sort_key(p):
    if p.stem.isdigit():
        return (0, int(p.stem), "")
    return (1, 0, p.name)


def pending(spool, *, now=None, settle=2):
    """Files and *.json directories, name order; skips .tmp/dotfiles and entries younger
    than settle s (no age check when settle is None: the writer has finished, Task
    10)."""
    now = now if now is not None else time.time()
    d = Path(spool)
    if not d.is_dir():
        return []
    out = []
    for p in d.iterdir():
        if p.name.startswith("."):
            continue
        # lstat: a dangling link must not raise
        if settle is not None and now - p.lstat().st_mtime < settle:
            continue
        out.append(p)
    return sorted(out, key=_sort_key)


def _record(conn, run_id, claim_lock, verb, schema, body, status, error, *, now):
    conn.execute(
        "INSERT INTO requests (run_id, claim_lock, verb, schema, body, status, error, created_at)"
        " VALUES (?,?,?,?,?,?,?,?)",
        (run_id, claim_lock, verb, schema, body, status, error, int(now)))


# Fields each verb needs beyond the request.v1 envelope
# (schema/run_id/task_id/claim_lock/verb).
REQUIRED_FIELDS = {
    "comment": ("text",),
    "block": ("kind", "text"),
    "request": ("text",),
    "artefact": ("path",),
    "unblock": (),
    "reassign": ("assignee",),
    "raise-tier": (),
    "untrack": ("path",),
}


def _check_required(verb, doc):
    if verb not in REQUIRED_FIELDS:
        raise ValueError(f"unknown verb {verb!r}")
    missing = [f for f in REQUIRED_FIELDS[verb] if f not in doc]
    if missing:
        raise ValueError(f"verb {verb!r} missing field(s) {missing}")
    image_requests.check(doc)         # M137: tool/bin only on a capability block


def _apply_verb(conn, run, doc, *, now):
    verb = doc["verb"]
    task_id = run["task_id"]
    if verb == "comment":
        conn.execute(
            "INSERT INTO task_comments (task_id, author, body, created_at) VALUES (?,?,?,?)",
            (task_id, run["profile"], doc["text"], int(now)))
    elif verb == "block" and doc.get("tool"):        # M137: a missing tool becomes an image request
        image_requests.file(conn, run, doc, now)
    elif verb == "block":
        blocks.block(conn, task_id, kind=doc["kind"], reason=doc["text"], run_pk=run["id"],
                     run_id=run["run_id"], now=now)
    elif verb == "request":
        parent = conn.execute("SELECT branch_name FROM tasks WHERE id=?", (task_id,)).fetchone()
        branch = parent["branch_name"] if parent else None
        text = doc["text"] + prior_verdicts.section(conn, None, "", doc["text"], now=now)
        db.new_task(conn, title=doc["text"][:120], body=text, status="triage",
                    request_needs=doc.get("needs"), branch=branch, parents=(task_id,), now=now)
    elif verb == "artefact":
        event(conn, task_id, "artefact", {"path": doc["path"]}, run_pk=run["id"], now=now)
    elif verb == "untrack":
        # Path safety checked here so a bad path is invalid, not a later host git crash; the
        # `git rm --cached` itself is the tick's job (mirrors `artefact`: record and move on).
        workspace.validate_untrack_path(doc["path"])
        event(conn, task_id, "untrack", {"path": doc["path"]}, run_pk=run["id"], now=now)
    elif verb == "unblock":
        target = doc.get("target_task", task_id)
        image_requests.guard_target(conn, target)
        blocks.unblock(conn, target, by="worker", now=now)
    elif verb == "reassign":
        target = doc.get("target_task", task_id)
        image_requests.guard_target(conn, target)
        conn.execute("UPDATE tasks SET assignee=? WHERE id=?", (doc["assignee"], target))
    elif verb == "raise-tier":
        target = doc.get("target_task", task_id)
        held = conn.execute("SELECT tier FROM tasks WHERE id=?", (target,)).fetchone()
        if held and held[0] == "mac":
            raise ValueError("a Mac lane card cannot be raised to the strong tier:"
                             " docker cannot build Mac apps")
        conn.execute("UPDATE tasks SET tier='strong' WHERE id=? AND tier='light'", (target,))
    else:
        raise ValueError(f"unknown verb {verb!r}")


# Schemas a file in requests/ may name; anything else (e.g. a report.v1 dropped
# there by mistake) is invalid, not silently skipped or crashed on.
REQUEST_SCHEMAS = ("request.v1", "decision.v1")


def _fix_option_ids(doc, pattern):
    """Fix each `options[].id` of `doc` (in place) that does not match `pattern`: lower-case, each
    run of other characters becomes "-", edge "-" cut, 20 characters at most; an empty or repeated
    result becomes "opt-<n>". `recommended` follows its option. Returns {old id: new id}."""
    options = doc.get("options")
    if not isinstance(options, list):
        return {}
    renamed, seen, olds = {}, set(), set()
    for n, opt in enumerate(options, 1):
        old = opt.get("id") if isinstance(opt, dict) else None
        if not isinstance(old, str):
            continue
        new = old
        if not re.match(pattern, old) or old in seen:
            new = re.sub(r"[^a-z0-9-]+", "-", old.lower()).strip("-")[:20].strip("-")
            k = n
            while not new or new in seen:
                new, k = f"opt-{k}", k + 1
        if new != old:
            opt["id"] = new
            if old not in olds:
                renamed[old] = new
        seen.add(new)
        olds.add(old)
    if isinstance(doc.get("recommended"), str) and doc["recommended"] in renamed:
        doc["recommended"] = renamed[doc["recommended"]]
    return renamed


def _validate(doc, schema):
    """(schema errors of `doc`, {old id: new id}); a decision.v1 has its option ids fixed first."""
    renamed = {}
    if doc.get("schema") == "decision.v1":
        pattern = schema["properties"]["options"]["items"]["properties"]["id"]["pattern"]
        renamed = _fix_option_ids(doc, pattern)
    return validate(doc, schema), renamed


def _note_renamed(conn, run, renamed, now):
    """One `decision_ids_renamed` event ({old: new}) when `_fix_option_ids` changed an id."""
    if renamed:
        event(conn, run["task_id"], "decision_ids_renamed",
              {"run_id": run["run_id"], "renamed_ids": renamed}, run_pk=run["id"], now=now)


def _mark_invalid(conn, run, reason, *, now):
    conn.execute("UPDATE task_runs SET error_class='invalid_output' WHERE id=?", (run["id"],))
    event(conn, run["task_id"], "invalid_document", {"reason": reason[:500]}, run_pk=run["id"], run_id=run["run_id"],
          now=now)


def _read_regular(path):
    """The bytes of `path` if it is a regular file, read from one fd opened without
    following a link or waiting on a FIFO; requests/ is mounted into the
    implementer's container, which can swap an entry at any moment,
    so there is no check-then-read by path."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)   # a link: ELOOP
    try:
        mode = os.fstat(fd).st_mode
        if stat.S_ISDIR(mode):
            raise IsADirectoryError(str(path))
        if not stat.S_ISREG(mode):
            raise ValueError("not a regular file")
        chunks = []
        while True:
            chunk = os.read(fd, 1 << 16)
            if not chunk:
                return b"".join(chunks)
            chunks.append(chunk)
    finally:
        os.close(fd)


def _archive(path, done):
    """Move `path` into `done/` under its own name, or a fresh one if an earlier entry already
    took it."""
    target, n = done / path.name, 1
    while target.exists() or target.is_symlink():
        target = done / f"{path.stem}.{n}{path.suffix}"
        n += 1
    os.replace(path, target)


def apply(conn, spool, run, *, role_verbs, schemas, now=None, settle=2):
    """`settle=None` once the run has finished (run.json written): its last files are final."""
    now = now if now is not None else time.time()
    spool = Path(spool)
    statuses = []
    for path in pending(spool / "requests", now=now, settle=settle):
        raw = None
        doc = None
        try:
            raw = _read_regular(path)
            doc = json.loads(raw.decode("utf-8"))
            if not isinstance(doc, dict):
                raise ValueError("document is not an object")
            schema_name = doc.get("schema")
            schema = schemas.get(schema_name) if isinstance(schema_name, str) else None
            if schema is None or schema_name not in REQUEST_SCHEMAS:
                raise ValueError(f"unknown or unsupported schema {schema_name!r} in requests/")
            errs, renamed = _validate(doc, schema)
            if errs:
                raise ValueError(errs[0])
            if schema_name == "request.v1":
                _check_required(doc["verb"], doc)
        except Exception as exc:
            body = raw.decode("utf-8", "replace") if raw is not None else ""
            statuses.append("invalid")
            with txn(conn):
                _record(conn, run["run_id"], run["claim_lock"],
                        doc.get("verb") if isinstance(doc, dict) else None,
                        doc.get("schema") if isinstance(doc, dict) else None,
                        body, "invalid", str(exc)[:500], now=now)
                _mark_invalid(conn, run, str(exc), now=now)
            _archive(path, spool / "done")
            continue

        verb = doc.get("verb")
        if (doc.get("run_id") != run["run_id"] or doc.get("claim_lock") != run["claim_lock"]
                or (schema_name == "request.v1" and verb not in role_verbs)):
            statuses.append("rejected")
            with txn(conn):
                _record(conn, doc.get("run_id"), doc.get("claim_lock"), verb, schema_name,
                        json.dumps(doc, ensure_ascii=False), "rejected", None, now=now)
            _archive(path, spool / "done")
            continue

        try:
            with txn(conn):
                if schema_name == "decision.v1":
                    _note_renamed(conn, run, renamed, now)
                    event(conn, run["task_id"], "decision_requested", doc, run_pk=run["id"], now=now)
                    decision_notify(conn, run["task_id"], doc, now)
                else:
                    _apply_verb(conn, run, doc, now=now)
                _record(conn, doc.get("run_id"), doc.get("claim_lock"), verb, schema_name,
                        json.dumps(doc, ensure_ascii=False), "applied", None, now=now)
        except Exception as exc:
            # The document passed schema + required-field checks but the effect itself
            # failed (e.g. blocks.block rejecting a bad kind combination).
            statuses.append("invalid")
            with txn(conn):
                _record(conn, doc.get("run_id"), doc.get("claim_lock"), verb, schema_name,
                        json.dumps(doc, ensure_ascii=False), "invalid", str(exc)[:500], now=now)
                _mark_invalid(conn, run, str(exc), now=now)
            _archive(path, spool / "done")
            continue

        statuses.append("applied")
        _archive(path, spool / "done")
    return statuses


def read_report(spool, schemas):
    spool = Path(spool)
    d = spool / "report"
    files = sorted((p for p in d.iterdir() if p.is_file() and not p.name.startswith(".")),
                    key=_sort_key) if d.is_dir() else []
    if not files:
        return None, ["no report file"]
    path = files[-1]
    try:
        doc = json.loads(path.read_bytes().decode("utf-8"))
    except Exception as exc:
        return None, [str(exc)]
    if not isinstance(doc, dict):
        return None, ["document is not an object"]
    schema = schemas.get(doc.get("schema"))
    if schema is None:
        return None, [f"unknown schema {doc.get('schema')!r}"]
    errs = validate(doc, schema)
    if errs:
        return None, errs
    return doc, []


def decision_notify(conn, task_id, doc, now, reply=None):
    """A decision.v1 is a 🔴 line (row 424: 'Decide t_ab12: option 2 recommended, reply 2');
    the `file` row keeps the whole document as before. `reply` replaces the `answer` line for a
    hold that `answer` does not release."""
    options = " · ".join(f"{o.get('id')}) {o.get('label')}" for o in doc.get("options") or [])
    rec = doc.get("recommended")
    fields = [("options", options)] + ([("recommended", rec)] if rec else [])
    outbox.notify(conn, "blocked", f"Decide {task_id}: {doc.get('question', '')}", fields=fields,
                  reply=reply or f"python3 -m capsule human {blocks._board_name(conn)} {task_id}"
                        f" answer " + (f"'{rec}'" if rec else "'<answer>'"),
                  thread_key=task_id, file_body=json.dumps(doc, ensure_ascii=False), now=now)
