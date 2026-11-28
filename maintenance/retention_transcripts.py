#!/usr/bin/env python3
"""Worker-session transcript retention for cleanup.sh (retention, the owner's ruling 2026-10-23).

Only Claude transcripts that a factory worker run owns (a session_id named in some
`<CAPSULE_HOME>/boards/*/spool/<run_id>/run.json`) are ever touched; the owner's own chats never
are. A worker transcript is compressed (zstd -19 --long, else xz -9) into the archive once it is
older than 14 days AND every run that used it started in a week whose `fresh_tokens` eval_check
row exists (the tokens oracle has read it). The archive is test-decompressed and its sha256
compared with the original before the original is removed. Archives older than 30 days go.
Transcript content is never parsed here: bytes go through the compressor and a hash, nothing else.
Any doubt (unreadable board, missing row, failed verify, symlink, vanished file) keeps the original.

usage: retention_transcripts.py HOME PROJECTS ARCHIVE APPLY(0|1)   [env: RETENTION_NOW]
Prints one line per action ("would " prefix unless APPLY=1) and a last line
`RETENTION_TRANSCRIPTS files=N bytes=B` (N actions, B original bytes compressed or archive bytes
deleted)."""
import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import time
from pathlib import Path

DAY, MIN_AGE_DAYS, ARCHIVE_DAYS = 86400, 14, 30
_SID = re.compile(r"[0-9A-Za-z-]+")
_WINDOWS = ("SELECT json_extract(payload,'$.window[0]'), json_extract(payload,'$.window[1]')"
            " FROM task_events WHERE kind='eval_check'"
            " AND json_extract(payload,'$.metric')='fresh_tokens'")


def _recorded(board):
    """({run_id: started_at}, [(since, until)]) from one board; unreadable -> nothing recorded."""
    try:
        db = sqlite3.connect(f"file:{board / 'kanban.db'}?mode=ro", uri=True)
        return dict(db.execute("SELECT run_id, started_at FROM task_runs")), db.execute(
            _WINDOWS).fetchall()
    except (sqlite3.Error, OSError):
        return {}, []


def _session_id(run_json):
    try:
        sid = json.loads(run_json.read_text("utf-8")).get("session_id")
    except (OSError, ValueError, AttributeError):
        return None
    return sid if isinstance(sid, str) and _SID.fullmatch(sid) else None


def _worker_sessions(home):
    """{session_id: True when every run that used it is in a week with a fresh_tokens row}."""
    out = {}
    for board in sorted((home / "boards").glob("*")):
        started, windows = _recorded(board)
        for rj in sorted((board / "spool").glob("*/run.json")):
            sid, t = _session_id(rj), started.get(rj.parent.name)
            if sid is None:
                continue
            ok = isinstance(t, (int, float)) and any(
                isinstance(a, (int, float)) and isinstance(b, (int, float)) and a <= t < b
                for a, b in windows)
            out[sid] = out.get(sid, True) and ok
    return out


def _sha(stream):
    h = hashlib.sha256()
    for chunk in iter(lambda: stream.read(1 << 20), b""):
        h.update(chunk)
    return h.hexdigest()


def _tool():
    z = os.environ.get("RETENTION_ZSTD") or shutil.which("zstd")
    if z:
        return [z, "-19", "--long", "-q", "-c"], [z, "-dc", "--long", "-q"], ".zst"
    x = os.environ.get("RETENTION_XZ") or shutil.which("xz")
    if x:
        return [x, "-9", "-c"], [x, "-dc"], ".xz"
    return None


def _compress_verified(src, dest, tool):
    """Compress src to dest, prove it round-trips (sha256 of the decompressed stream) and make
    it durable (fsync file and directory); True only then. Never touches src. OSError escapes."""
    part = dest.with_name(dest.name + ".partial")
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(src, "rb") as i, open(part, "wb") as o:
            if subprocess.run(tool[0], stdin=i, stdout=o).returncode != 0:
                return False
            o.flush()
            os.fsync(o.fileno())
        with open(src, "rb") as i, open(part, "rb") as j:
            want = _sha(i)
            p = subprocess.Popen(tool[1], stdin=j, stdout=subprocess.PIPE)
            if _sha(p.stdout) != want or p.wait() != 0:
                return False
        os.replace(part, dest)
        fd = os.open(dest.parent, os.O_RDONLY)
        os.fsync(fd)
        os.close(fd)
        return True
    finally:
        part.unlink(missing_ok=True)


def _archive_one(src, recorded, tool, archive, apply, say, now):
    """(actions, bytes) for one transcript; OSError escapes to the caller, which keeps it."""
    if src.is_symlink():
        print(f"keep {src}: symlink")
        return 0, 0
    if now - src.stat().st_mtime <= MIN_AGE_DAYS * DAY:
        return 0, 0
    if not recorded or tool is None:
        print(f"keep {src}: " + ("its week has no fresh_tokens row yet" if tool
                                  else "neither zstd nor xz found"))
        return 0, 0
    dest, size = archive / src.parent.name / (src.name + tool[2]), src.stat().st_size
    say(f"compress {src} ({size} b) to {dest}")
    if not apply:
        return 1, 0
    if not _compress_verified(src, dest, tool):
        print(f"keep {src}: archive failed verification")
        return 1, 0
    src.unlink()
    return 1, size


def _archive_old(sessions, projects, archive, apply, say):
    tool, n, nbytes, now = _tool(), 0, 0, float(os.environ.get("RETENTION_NOW") or time.time())
    for sid, recorded in sorted(sessions.items()):
        for src in sorted(projects.glob(f"*/{sid}.jsonl")):
            try:
                dn, db = _archive_one(src, recorded, tool, archive, apply, say, now)
            except OSError as exc:
                print(f"keep {src}: {exc}")
                continue
            n, nbytes = n + dn, nbytes + db
    return n, nbytes


def _expire(archive, apply, say):
    """Delete archives this job made (`<archive>/<slug>/<sid>.jsonl.zst|xz`) past 30 days."""
    n, nbytes, now = 0, 0, float(os.environ.get("RETENTION_NOW") or time.time())
    for arc in sorted([*archive.glob("*/*.jsonl.zst"), *archive.glob("*/*.jsonl.xz")]):
        try:
            st = arc.lstat()
        except OSError as exc:
            print(f"keep {arc}: {exc}")
            continue
        if not stat.S_ISREG(st.st_mode) or now - st.st_mtime <= ARCHIVE_DAYS * DAY:
            continue
        say(f"delete archive {arc} ({st.st_size} b)")
        try:
            if apply:
                arc.unlink()
        except OSError as exc:
            print(f"keep {arc}: {exc}")
            continue
        n, nbytes = n + 1, nbytes + st.st_size
    return n, nbytes


def run(home, projects, archive, apply):
    def say(text):
        print(("" if apply else "would ") + text)
    n1, b1 = _archive_old(_worker_sessions(home), projects, archive, apply, say)
    n2, b2 = _expire(archive, apply, say)
    print(f"RETENTION_TRANSCRIPTS files={n1 + n2} bytes={b1 + b2}")


if __name__ == "__main__":
    run(*(Path(a) for a in sys.argv[1:4]), sys.argv[4] == "1")
