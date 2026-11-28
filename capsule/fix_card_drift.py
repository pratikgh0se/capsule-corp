"""A Korin fix card that main has overtaken says so in its body at release (inc_43e0584d)."""
import json
import re
import subprocess

from capsule import bundles, fix_card_quotes, paths, prior_verdicts
from capsule.db import event, txn

HEADING = "## Main moved since this card was filed"
MAX_LINES, MAX_SUBJECT, TIMEOUT = 10, 160, 10
_SHA = re.compile(r"[0-9a-f]{7,40}")
_A = "Main has commits on files this card names. Check main first."
KEPT = "A human answer written at this release stays in force."
_B = "Do not build a part twice that already landed:"
INTRO = f"{_A} {KEPT} {_B}"
CLOSE = "End of the main-moved list."
# heading, intro (KEPT optional: older cards lack it), the `- ` lines section() wrote, then the
# fixed closing line; text after it stays
_OLD = re.compile(r"\n\n^" + re.escape(HEADING) + r"$\n\n^" + re.escape(_A) + "(?: "
                  + re.escape(KEPT) + ")? " + re.escape(_B) + r"$(?:\n- [^\n]*)+\n^"
                  + re.escape(CLOSE) + r"$", re.M)


def _commits(main_sha, files):
    """["<sha> <subject>"] from main_sha..main on `files`; None on any git failure."""
    cmd = ["git", "-C", str(paths.REPO), "log", "--format=%h %s", f"-n{MAX_LINES + 1}",
           f"{main_sha}..main", "--", *files]
    try:
        done = subprocess.run(cmd, capture_output=True, text=True, timeout=TIMEOUT, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout.splitlines() if done.returncode == 0 else None


def section(body, main_sha):
    """The section text for `body`; "" when no path or no commit; None on git failure."""
    above = _OLD.sub("", body).split("\n## Prior verdicts")[0]
    above = above.split("\n" + fix_card_quotes.HEADING)[0]   # quoted human text names no files
    files = list(dict.fromkeys(prior_verdicts._PATH.findall(above)))
    if not _SHA.fullmatch(main_sha or ""):
        return None
    if not files:
        return ""
    lines = _commits(main_sha, files)
    if lines is None:
        return None
    if not lines:
        return ""
    out = [f"- {prior_verdicts._clean(line, MAX_SUBJECT)}" for line in lines[:MAX_LINES]]
    if len(lines) > MAX_LINES:
        out.append("- more commits not listed")
    return f"\n\n{HEADING}\n\n{INTRO}\n" + "\n".join(out) + f"\n{CLOSE}"


def _amend(conn, task_id, now):
    q = "SELECT id, main_sha FROM incidents WHERE fix_card=? AND main_sha IS NOT NULL"
    inc = conn.execute(q + " ORDER BY opened_at ASC, id LIMIT 1", (task_id,)).fetchone()
    row = conn.execute("SELECT body, file_set, status FROM tasks WHERE id=?", (task_id,)).fetchone()
    if inc is None or row is None or row["status"] not in ("ready", "todo"):
        return
    old = row["body"] or ""
    fresh = section(old, inc["main_sha"])
    if fresh is None:
        return
    new = _OLD.sub("", old) + fresh
    if new == old:
        return
    files = json.loads(row["file_set"]) if row["file_set"] else []
    with txn(conn):
        conn.execute("UPDATE tasks SET body=? WHERE id=?", (new, task_id))
        event(conn, task_id, "contract_amended",
              {"issuer": "harness", "reason": f"main moved since {inc['id']} was filed",
               "old_file_set": files, "new_file_set": files,
               "body_hash_before": bundles.card_hash(old),
               "body_hash_after": bundles.card_hash(new)}, now=now)


def note_release(conn, task_id, now):
    """Call inside the release txn, after the release applied. Never refuses the release."""
    try:
        _amend(conn, task_id, now)
    except Exception:  # noqa: BLE001 - advice only
        return
