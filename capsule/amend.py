"""`python3 -m capsule human <board> <task> amend --body-file <path> [--file-set a,b,c] [--no-lint]
"<reason>"` (row 452's one-off script, made a proper operator verb)."""
import calendar
import hashlib
import json
import re
import secrets
import sqlite3
import sys
import time
from pathlib import Path

from capsule import db, outbox, paths
from capsule.db import txn
from capsule.tick import REVIEW_ROLES

# defensive fallback; every review run's claim_expires is set at dispatch
REVIEW_RETRY_AFTER_DEFAULT = 300


# inc_d3c02ed5: a note with one of these phrases must say which earlier rulings stay in force
NARROWING_RE = re.compile("nothing else|change only|only these|no other change", re.IGNORECASE)
KEEPS_PREFIXES = ("Keeps:", "Supersedes:")
# inc_511e1362: obligations only the host can meet; a bare `precheck-gates.py` (HEAD base) is fine
HOST_ONLY_RE = re.compile(
    r"--base[=\s]+origin/|\bgit\s+fetch\b"
    r"|\brebas(?:e|es|ed|ing)\b.*\borigin\b|\borigin\b.*\brebas(?:e|es|ed|ing)\b"
    r"|origin/main\b.{0,20}\bsha\b|\bsha\b.{0,20}origin/main"
    r"|\bpast(?:e|es|ed|ing)\b.*\b(?:mutation_check|precheck)\b"
    r"|\b(?:mutation_check|precheck)\b.*\bpast(?:e|es|ed|ing)\b", re.IGNORECASE)
HOST_PREFIXES = ("Host:", "host_only:")


class AmendRefused(Exception):
    """A typed refusal (running card, live review run, missing/empty body file, no-op
    call, unknown task, narrowing note with no Keeps:/Supersedes: line, host-only demand
    with no Host:/host_only: prefix) - the CLI prints
    `str(exc)` and exits 1, never a traceback
    (spec §4: never a guess)."""

    def __init__(self, message, *, retry_after=None):
        super().__init__(message)
        self.retry_after = retry_after


def _live_review_run(conn, task_id):
    """The soonest-expiring still-open review run (validator/reviewer/guide, `ended_at
    IS NULL`) for `task_id`, or None."""
    return conn.execute(
        f"SELECT id, claim_expires FROM task_runs WHERE task_id=? AND profile IN"
        f" ({','.join('?' * len(REVIEW_ROLES))}) AND ended_at IS NULL"
        " ORDER BY COALESCE(claim_expires, 0) LIMIT 1", (task_id, *REVIEW_ROLES)).fetchone()


def _refuse_if_review_live(conn, task_id, now):
    live = _live_review_run(conn, task_id)
    if live is None:
        return
    retry_after = max(0, (live["claim_expires"] if live["claim_expires"] is not None
                          else now + REVIEW_RETRY_AFTER_DEFAULT) - now)
    raise AmendRefused(
        f"{task_id} has a live review run: amending mid-review would judge the round against a card"
        f" body it never saw; retry in about {retry_after}s", retry_after=retry_after)


def _read_body_file(body_file):
    path = Path(body_file)
    if not path.is_file():
        raise AmendRefused(f"--body-file {body_file}: not found")
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        raise AmendRefused(f"--body-file {body_file}: empty")
    return text


def _lint_note(old_body, new_body):
    """The note is the tail when the old body is a prefix, else the added lines."""
    old_body = old_body or ""
    old_lines = set(old_body.splitlines())
    note = (new_body[len(old_body):] if new_body.startswith(old_body)
            else "\n".join(ln for ln in new_body.splitlines() if ln not in old_lines))
    match = NARROWING_RE.search(note)
    if match is None:
        return
    if any(line.startswith(KEEPS_PREFIXES) for line in note.splitlines()):
        return
    raise AmendRefused(
        f"the appended note says {match.group(0)!r} but has no line starting with 'Keeps:' or"
        " 'Supersedes:': add one naming the earlier notes and rulings that stay in force or stop"
        " applying ('none' is valid), or pass --no-lint")


def _lint_host_only(new_body):
    """inc_511e1362: the sandbox cannot do host work, so no line of the card may demand it."""
    for line in new_body.splitlines():
        if not line.startswith(HOST_PREFIXES) and HOST_ONLY_RE.search(line):
            raise AmendRefused(
                f"the card body asks the implementer for host-only work: {line.strip()!r}. The"
                " sandbox has no network and a read-only git index: start the line with 'Host:' or"
                " 'host_only:' if the host does it, or pass --no-lint")


def _hash(text):
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def _backup(conn, board, now, backup_dir=None):
    bdir = Path(backup_dir) if backup_dir is not None else paths.backup_dir()
    bdir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(now)) + "-" + secrets.token_hex(3)
    dest = bdir / f"{board}-{stamp}.db"
    target = sqlite3.connect(str(dest))
    try:
        conn.backup(target)
    finally:
        target.close()
    # keep the new backup plus the newest 3 earlier ones; drop the rest if older than 7 days
    pat = re.compile(re.escape(board) + r"-(\d{8}T\d{6})Z-[0-9a-f]{6}\.db")
    found = []
    try:
        names = list(bdir.iterdir())
    except OSError:
        names = []
    for p in names:
        m = pat.fullmatch(p.name)
        if not m or p == dest or not p.is_file() or p.is_symlink():
            continue
        try:
            found.append((calendar.timegm(time.strptime(m.group(1), "%Y%m%dT%H%M%S")), p))
        except ValueError:
            continue
    found.sort(key=lambda t: (t[0], t[1].name), reverse=True)
    for ts, p in found[3:]:
        if now - ts <= 7 * 86400:
            continue
        try:
            p.unlink()
        except OSError as exc:
            print(f"capsule backup retention: skipped {p} ({exc})", file=sys.stderr)
            continue
        print(f"capsule backup retention: removed {p} (age {(now - ts) // 86400} days)",
              file=sys.stderr)
    return dest


def amend(capsule_home, board, task_id, *, body_file=None, file_set=None, reason, who="operator",
          now=None, backup_dir=None, lint=True):
    """`file_set`, if given, is the full replacement list (not a merge). Returns
    {"event_id": ..., "backup": <path>} on success; raises `AmendRefused` on any refusal."""
    if body_file is None and file_set is None:
        raise AmendRefused("amend needs --body-file and/or --file-set: nothing to amend")
    now = int(now if now is not None else time.time())
    new_body = _read_body_file(body_file) if body_file is not None else None

    conn = db.connect(paths.board_db(board, capsule_home))
    try:
        row = conn.execute("SELECT status, body, file_set FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row is None:
            raise AmendRefused(f"no such task {task_id!r} on board {board!r}")
        if row["status"] == "running":
            raise AmendRefused(f"{task_id} is running: a live implementer would read a moving contract")
        _refuse_if_review_live(conn, task_id, now)

        old_body = row["body"]
        old_file_set = json.loads(row["file_set"]) if row["file_set"] else []
        final_body = new_body if new_body is not None else old_body
        final_file_set = list(file_set) if file_set is not None else old_file_set
        if lint and new_body is not None:
            _lint_host_only(new_body)
            _lint_note(old_body, new_body)
        backup_path = _backup(conn, board, now, backup_dir=backup_dir)

        with txn(conn):
            # the race window: a review run (or the implementer) could have started between the
            # read above and this write - never guess which half landed, refuse cleanly instead.
            _refuse_if_review_live(conn, task_id, now)
            sets, params = ["block_recurrences=0"], []
            if new_body is not None:
                sets.append("body=?")
                params.append(final_body)
            if file_set is not None:
                sets.append("file_set=?")
                params.append(json.dumps(final_file_set))
            cur = conn.execute(
                f"UPDATE tasks SET {', '.join(sets)} WHERE id=? AND status != 'running'",
                (*params, task_id))
            if cur.rowcount != 1:
                raise AmendRefused(f"{task_id} is running: a live implementer would read a moving contract")
            payload = {
                "issuer": who, "reason": reason,
                "old_file_set": old_file_set, "new_file_set": final_file_set,
                "body_hash_before": _hash(old_body), "body_hash_after": _hash(final_body),
                "backup": str(backup_path), **({} if lint else {"lint_skipped": True}),
            }
            db.event(conn, task_id, "contract_amended", payload, now=now)
            event_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            what = [name for name, val in (("body", new_body), ("file_set", file_set))
                    if val is not None]
            outbox.add(conn, "file",
                       f"capsule: {task_id} contract amended ({'+'.join(what)}): "
                       f"{' '.join(reason[:1500].split())} | event {event_id}",
                       thread_key=task_id, now=now)
        return {"event_id": event_id, "backup": str(backup_path)}
    finally:
        conn.close()
