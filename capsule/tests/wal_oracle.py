"""Shared WAL-mode board-db fixture builder for the row 466 tests (test-support, never itself
scored by the mutation gate - `mutation_check.is_test_support_file`'s "oracle" name hint keeps
it out of both revert-mutation and assertion-weaken).

Each fixture is built by a genuinely separate writer PROCESS that exits either cleanly (the
ordinary idle-board state: sqlite auto-checkpoints and deletes `-wal`/`-shm` when the last
connection to a db closes) or crash-style (`os._exit`, no checkpoint - `-wal`/`-shm` stay on
disk with real, uncheckpointed frames). Row 466 review I2: a same-process writer/reader pair
silently shares sqlite's own unix-VFS `-shm` mapping and hides the very failures a real,
out-of-process reader hits - every fixture here is built by a subprocess so the test that opens
it afterwards is a genuine, independent reader, exactly like the real tick and a real board."""
import subprocess
import sys
from pathlib import Path

from capsule import paths

_WRITER = """
import os, sys
sys.path.insert(0, {repo!r})
from capsule import db
p = sys.argv[1]
db.create_board(p)   # its own connection closes cleanly: a real legacy+Capsule schema, checkpointed
if sys.argv[2] == "frames":
    conn = db.connect(p)
    conn.execute("PRAGMA wal_autocheckpoint=0")
    db.new_task(conn, title="pending", status="ready")
    os._exit(0)       # crash-style: no checkpoint, -wal/-shm stay with this write's real frames
elif sys.argv[2] == "empty_wal":
    conn = db.connect(p)
    db.new_task(conn, title="committed", status="ready")
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")   # frames -> the base file; -wal truncated to 0 B
    os._exit(0)       # crash-style: a clean close would still delete the now-empty -wal/-shm
"""


def make(directory, *, frames):
    """A real board db at `directory/kanban.db`: `frames=True` leaves real, uncheckpointed WAL
    frames on disk (a crash-style `os._exit` after one write); `frames=False` is the ordinary
    idle board (`create_board` alone, whose own connection closes cleanly - both sidecars gone,
    the commonest real state, row 466 review C1)."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "kanban.db"
    script = _WRITER.format(repo=str(paths.REPO))
    subprocess.run([sys.executable, "-c", script, str(path), "frames" if frames else "none"],
                   check=True, timeout=10, cwd=str(paths.REPO))
    return path


def make_empty_wal(directory):
    """A real board db, one committed+checkpointed task row, with `-wal` present but truncated
    to 0 bytes and `-shm` still present (row 466 review round 2 state 1b) - `-wal` empty but not
    absent, distinct from the plain idle state where `make(frames=False)` leaves neither sidecar
    on disk at all. Built by a separate process, same reasoning as `make` (module docstring)."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "kanban.db"
    script = _WRITER.format(repo=str(paths.REPO))
    subprocess.run([sys.executable, "-c", script, str(path), "empty_wal"],
                   check=True, timeout=10, cwd=str(paths.REPO))
    return path
