#!/usr/bin/env python3
"""verdicts.py - the host-only verdict record store (C3/M042, row 127)."""
import json
import os
import tempfile
import time
from pathlib import Path

DEFAULT_STATE = Path.home() / ".local" / "state" / "capsule-corp" / "verdicts"
V = 1
RECORD_CAP = 200
KINDS = ("gate", "quality", "intervention")
_MODES = {"0": "off", "": "off", "shadow": "shadow", "1": "on", "true": "on", "yes": "on"}


def mode(env=None):
    """"off" (no write, no read - the gate is byte-identical to pre-C3), "shadow" (write
    and mirror, never read), "on" (write, read, hold on a disagreement)."""
    raw = str((env if env is not None else os.environ).get("OPS_VERDICT_STORE") or "").strip().lower()
    return _MODES.get(raw, "off")


def ops_env():
    """ops.env's keys under the real environment's - `quality_gate._tamper_env()`'s
    idiom, shared here because every flag reader (merge_gate, quality_gate,
    decisions, repair) needs the same mapping: the gate tick and the quality hook
    inherit the old daemon's environment, which does not carry the `ops.env`
    deploy writes, so the file has to be read for the flag to mean anything in
    production."""
    try:
        import sys
        here = str(Path(__file__).resolve().parent)
        if here not in sys.path:
            sys.path.insert(0, here)
        import board_bridge
        ops = board_bridge._ops_env()
    except Exception:
        ops = {}
    return {**ops, **os.environ}


def verdict_dir(create=False):
    """Same override-or-refuse rule as merge_gate.state_dir(): under
    AGENT_OPS_NO_SEND with no OPS_VERDICT_STATE, falling through to the real
    ~/.local/state path is always a bug (the 2026-09-07 incident class)."""
    override = os.environ.get("OPS_VERDICT_STATE")
    if not override and os.environ.get("AGENT_OPS_NO_SEND"):
        raise RuntimeError("refusing to touch the default verdict dir under test: "
                            "set OPS_VERDICT_STATE explicitly")
    p = Path(override or DEFAULT_STATE)
    if create:
        p.mkdir(parents=True, exist_ok=True, mode=0o700)   # no worker may read or write it
    return p


def _board_and_task_re():
    # lazy, as intake.py imports merge_gate lazily: keeps this module import-light and
    # sidesteps any import-order cycle with merge_gate, which will gain a
    # `verdicts.newest(...)` call of its own (7.2).
    import sys
    _here = str(Path(__file__).resolve().parent)
    if _here not in sys.path:                    # L2: never grow sys.path per call
        sys.path.insert(0, _here)
    import board       # noqa: E402
    import merge_gate  # noqa: E402
    return merge_gate.SLUG_RE, board.TASK_ID_RE


def verdict_path(board, task_id):
    slug_re, task_id_re = _board_and_task_re()
    if not slug_re.fullmatch(str(board or "")):
        raise ValueError(f"bad board slug {board!r}: must match {slug_re.pattern}")
    if not task_id_re.fullmatch(str(task_id or "")):
        raise ValueError(f"bad task id {task_id!r}: must match {task_id_re.pattern}")
    return verdict_dir() / board / f"{task_id}.json"


def _empty_doc(board, task_id):
    return {"v": V, "board": board, "task_id": task_id, "records": []}


def _load(path):
    """Best-effort read: absent, unreadable or malformed all mean "start
    fresh" here (append never raises over a corrupt prior file - that is
    read()'s job to surface, not append()'s)."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(doc, dict) or doc.get("v") != V:
        return None
    return doc


def _atomic_write_json(path, doc):
    """mkstemp + fsync-free flush + chmod 0600 + os.replace, with 0700 on the two dirs
    above it (`mode=` only sets the leaf)."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    for d in (path.parent.parent, path.parent):   # mode= only sets the leaf
        os.chmod(d, 0o700)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(doc, fh)
            fh.write("\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return path


def _write_atomic(path, doc):
    verdict_dir(create=True)
    return _atomic_write_json(path, doc)


def append(board, task_id, record, *, now=None):
    """Append one verdict record and return the file's Path."""
    kind = record.get("kind")
    if kind not in KINDS:
        raise ValueError(f"bad verdict kind {kind!r}: must be one of {KINDS}")
    path = verdict_path(board, task_id)
    doc = _load(path) or _empty_doc(board, task_id)
    rec = dict(record)
    rec.setdefault("ts", float(now if now is not None else time.time()))
    records = list(doc.get("records") or [])
    records.append(rec)
    records.sort(key=lambda r: r.get("ts", 0.0))
    records = records[-RECORD_CAP:]
    doc["records"] = records
    doc["board"] = board
    doc["task_id"] = task_id
    doc["v"] = V
    doc["updated_ts"] = float(now if now is not None else time.time())
    return _write_atomic(path, doc)


def read(board, task_id):
    """The full document, or None if absent, unreadable, malformed, not a dict, carrying
    an unrecognised `v`, or naming a board/task_id that doesn't validate (the
    events.py rule: anything that isn't a clean record is treated as absent, never
    raised - only `append()` and a direct `verdict_path()` call raise on a bad id)."""
    try:
        path = verdict_path(board, task_id)
    except ValueError:
        return None
    return _load(path)


def newest(board, task_id, kind=None):
    """The record with the largest `ts`, optionally filtered to `kind`.
    None if the document is absent or no record matches."""
    doc = read(board, task_id)
    if not doc:
        return None
    records = doc.get("records") or []
    if kind is not None:
        records = [r for r in records if r.get("kind") == kind]
    if not records:
        return None
    return max(records, key=lambda r: r.get("ts", 0.0))
