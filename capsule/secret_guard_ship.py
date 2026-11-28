"""Ship `<state>/secret-read-guard.log` lines to ops-pg as `secret_guard` events (OBS-12 b).
One tab-separated line (UTC time, mode, tool, path as written, path resolved) is one event; the
event_id is a hash of the line, so a replay is a no-op in Postgres (`on conflict do nothing`).
Only the line is read, never the file it names. Sending is lookout_ship's: its script, its psql.
`python3 -m capsule.secret_guard_ship --help` runs it by hand."""
import argparse
import datetime
import hashlib
import os
import re
import sys
from pathlib import Path

from capsule import lookout_ship

LOG = "secret-read-guard.log"
OFFSET = "secret-read-guard.shipped"   # "<inode> <bytes shipped> <head print>", beside the log
PATH_MAX = 200
_CTRL = re.compile(r"[\x00-\x1f\x7f]")   # a NUL would make jsonb reject the whole chunk
HELP = """\
env:
  AGENT_OPS_NO_SEND=1      count only: print what would be sent, send nothing (wins over the
                           docker binary below)
  AGENT_OPS_DOCKER_BIN     the docker binary lookout_ship runs psql through (the test seam)
  AGENT_OPS_STATE_DIR      the state dir (default as maintenance/events.state_root)
"""


def _doc(raw):
    """One log line -> an `events` row, or None when the line is malformed."""
    fields = raw.split("\t")
    if len(fields) != 5:
        return None
    try:
        ts = datetime.datetime.strptime(fields[0], "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return None
    mode, tool, path = (lookout_ship.scrub.scrub(_CTRL.sub(" ", f)) for f in fields[1:4])
    return {"v": 1, "event_id": "secret_guard:" + hashlib.sha256(raw.encode()).hexdigest()[:32],
            "ts": ts.replace(tzinfo=datetime.timezone.utc).isoformat(), "kind": "secret_guard",
            "source": "secret-read-guard", "severity": "warn",
            "title": f"secret read guard {mode}: {tool}",
            "payload": {"tool": tool, "mode": mode, "path": path[:PATH_MAX]}}


def _print(head, n):
    """Fingerprint of the first min(n, 64) bytes: stable while the log only grows."""
    return hashlib.sha256(head[:min(n, 64)]).hexdigest()[:16]


def _unread(state):
    """(inode, head, start, bytes past start) of the log; start is 0 when the log is not the one
    the offset was written for (rotated, replaced: inode, size or first-bytes fingerprint differs;
    a container may reuse an inode)."""
    with open(state / LOG, "rb") as f:
        st = os.fstat(f.fileno())
        head = f.read(64)
        try:
            fields = (state / OFFSET).read_text().split()
            ino, start = int(fields[0]), int(fields[1])
            mark = fields[2] if len(fields) > 2 else _print(head, start)   # old file: no mark
        except (OSError, ValueError, IndexError):
            ino, start, mark = st.st_ino, 0, ""
        same = ino == st.st_ino and start <= st.st_size and mark in ("", _print(head, start))
        start = start if same else 0
        f.seek(start)
        return st.st_ino, head, start, f.read()


def ship(state=None):
    """Ship the log lines past the offset. Returns 0; a failed send leaves the offset alone."""
    state = Path(state) if state else lookout_ship.events.state_root()
    try:
        ino, head, start, tail = _unread(state)
    except OSError:
        return 0
    whole = tail[:tail.rfind(b"\n") + 1]             # a half-written last line waits
    lines = whole.splitlines(keepends=True)[:lookout_ship.CHUNK_ROWS]
    docs = [_doc(line.decode("utf-8", errors="replace").rstrip("\r\n")) for line in lines]
    sent = [("events", d) for d in docs if d]
    if len(docs) > len(sent):
        print(f"secret_guard ship: skipped {len(docs) - len(sent)} malformed line(s)",
              file=sys.stderr)
    if os.environ.get("AGENT_OPS_NO_SEND"):
        print(f"secret_guard ship: would send {len(sent)} event(s), not sending")
        return 0
    if sent:
        script = lookout_ship.build_script(sent, left={}, prices=[], tables=["events"])
        rc, _ = lookout_ship.psql(script)
        if rc:
            print(f"secret_guard ship: psql rc={rc}", file=sys.stderr)
            return rc
    end = start + sum(len(line) for line in lines)
    (state / OFFSET).write_text(f"{ino} {end} {_print(head, end)}")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="python3 -m capsule.secret_guard_ship", epilog=HELP,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Ship secret-read-guard log lines to ops-pg as secret_guard events.")
    ap.add_argument("--state-dir", default=None,
                    help="dir holding secret-read-guard.log (default: AGENT_OPS_STATE_DIR rule)")
    return ship(ap.parse_args(argv).state_dir)


if __name__ == "__main__":
    sys.exit(main())
