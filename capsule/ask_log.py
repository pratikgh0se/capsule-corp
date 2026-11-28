"""The ask gate's files (split out of `capsule/ask_gate.py`, no change in behaviour): the dossier
and queue files and the verdict log under `<board>/asks/`."""
import fcntl
import json

from capsule import paths, safe_write


def _store(home, board, doc, verdict_name, mode):
    """Write the dossier file; on mode `on` an escalate joins the queue as well."""
    base = paths.board_dir(board, home) / "asks"
    body = json.dumps(doc, indent=2) + "\n"
    safe_write.atomic_write(home, base / "dossiers" / f"{doc['id']}.json", body, create_dirs=True)
    if verdict_name == "escalate" and mode == "on":
        safe_write.atomic_write(home, base / "queue" / f"{doc['id']}.json", body, create_dirs=True)


def _log_path(home, board):
    return paths.board_dir(board, home) / "asks" / "verdicts.jsonl"


def log(home, board, row):
    """Append one verdict line to `<board>/asks/verdicts.jsonl` (flock; never a board event, so a
    shadow run cannot change what the board's own readers see)."""
    path = _log_path(home, board)
    path.parent.mkdir(parents=True, exist_ok=True)
    with safe_write.open_append(home, path) as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        fh.write((json.dumps(row) + "\n").encode())
        fh.flush()


def verdict_rows(home, board):
    """The board's logged verdicts, oldest first; a missing or damaged file gives what is valid."""
    try:
        lines = _log_path(home, board).read_text(errors="replace").splitlines()
    except OSError:
        return []
    rows = []
    for line in lines:
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    return [r for r in rows if isinstance(r, dict)]
