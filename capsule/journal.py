"""Per-run step journal: one JSON line per completed step in `<spool>/journal.jsonl`, append-only.
A resumed run's next line is seq k+1; a lost run's replacement has its own spool, so its journal
starts empty. The landing keeps one too (R12, `landing._journal`) and reads it back after a kill."""
import json
import os
import time
from pathlib import Path

from capsule import safe_write

NAME = "journal.jsonl"


def _bytes(spool):
    """The file's bytes, never following a symlink (a worker could plant one); b'' if absent."""
    try:
        fd = os.open(Path(spool) / NAME, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        return b""
    with os.fdopen(fd, "rb") as f:
        return f.read()


def read(spool):
    """The journal's lines, oldest first; [] when the run wrote none. A torn tail is skipped."""
    out = []
    for raw in _bytes(spool).split(b"\n"):
        try:
            out.append(json.loads(raw))
        except ValueError:
            continue
    return out


def append(spool, step, **fields):
    """Record that `step` completed (with any `fields` the step carries); returns its line. Opens
    for append only, one write, so a kill leaves whole lines (a torn tail is closed off with a
    newline first)."""
    data = _bytes(spool)
    line = {"seq": data.count(b"\n") + 1, "step": step, "at": int(time.time()), **fields}
    prefix = b"\n" if data and not data.endswith(b"\n") else b""
    with safe_write.open_append(Path(spool), Path(spool) / NAME) as f:
        f.write(prefix + (json.dumps(line) + "\n").encode("utf-8"))
    return line
