"""Z2 second oracle for the brainstorm runner (M204 B2): reconciles `turns.jsonl` against the
board's outbox rows. Standalone on purpose: files and sqlite3 only, no import of the runner, its
constants or the outbox code, so a bug there cannot hide here. `python3 -m capsule.brainstorm_check
<home> <board>` prints one problem per line and exits 1 when there is any."""
import json
import sqlite3
import sys
import time
from pathlib import Path

DAILY = 100       # restated here, not imported: D232
TURNS = 60
PREFIX = "brainstorm-reply:"


def _lines(path):
    try:
        text = Path(path).read_text(encoding="utf-8")
        return [json.loads(x) for x in text.splitlines() if x.strip()]
    except (OSError, ValueError):
        return None


def _queued(board_dir):
    out = {}
    db = board_dir / "kanban.db"
    if db.is_file():
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        for key, body, tkey in conn.execute(
                "SELECT k.dedup_key, o.body, o.thread_key FROM outbox_key k JOIN outbox o"
                " ON o.id=k.outbox_id WHERE k.dedup_key LIKE ?", (PREFIX + "%",)):
            out.setdefault(key, []).append((body, tkey))
        conn.close()
    return out


def _reply(bdir, t, rows, want):
    key = f"{PREFIX}{bdir.name}:{t.get('n')}"
    if len(rows) != 1:
        return key, f"{key}: {len(rows)} outbox rows for the turn"
    if rows[0] != (t.get("text"), want):
        return key, f"{key}: outbox body or thread differs from the turn"
    return key, None


def _one(bdir, queued, per_day, seen):
    turns = _lines(bdir / "turns.jsonl")
    if turns is None:
        return []
    meta = json.loads((bdir / "meta.json").read_text(encoding="utf-8"))
    want = f"{meta.get('channel')}|{meta.get('thread_ts')}"
    replies = [t for t in turns if t.get("role") == "capsule"]
    problems = []
    if [t.get("n") for t in replies] != list(range(1, len(replies) + 1)):
        problems.append(f"{bdir.name}: reply numbers are not 1..{len(replies)}")
    if sum(1 for t in turns if t.get("role") == "owner") > TURNS + 1:
        problems.append(f"{bdir.name}: more than {TURNS} owner turns")
    for t in replies:
        key, problem = _reply(bdir, t, queued.get(f"{PREFIX}{bdir.name}:{t.get('n')}", []), want)
        seen.add(key)
        problems += [problem] if problem else []
        if not t.get("fixed"):
            day = time.strftime("%Y-%m-%d", time.localtime(t.get("ts", 0)))
            per_day[day] = per_day.get(day, 0) + 1
    return problems


def check(home, board):
    board_dir = Path(home) / "boards" / board
    queued, per_day, seen, problems = _queued(board_dir), {}, set(), []
    for bdir in sorted((board_dir / "brainstorms").glob("[!_]*")):
        problems += _one(bdir, queued, per_day, seen)
    problems += [f"{k}: outbox row without a turn" for k in sorted(set(queued) - seen)]
    problems += [f"{d}: {n} model replies, over {DAILY}"
                 for d, n in sorted(per_day.items()) if n > DAILY]
    return problems


if __name__ == "__main__":
    found = check(sys.argv[1], sys.argv[2])
    print("\n".join(found))
    sys.exit(1 if found else 0)
